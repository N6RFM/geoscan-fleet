#!/usr/bin/env python3
"""
GEOSCAN fleet pass EXECUTOR.

Unlike the earlier version, this no longer decides on its own which passes
to record - it only acts on schedule.yaml, which plan_passes.py generates
and which you (or --interactive) have approved in advance. For each
approved pass it:

  - waits for wall-clock time to reach `aos`
  - launches the matching flowgraph subprocess
  - feeds it live Doppler-corrected frequency via the shared rigctld
  - stops it at `los` (or earlier, as a safety net, if the satellite's
    actual elevation drops back below min_elev_deg first - e.g. because
    the TLE has drifted since planning)

Run `python3 plan_passes.py` first to (re)generate schedule.yaml.
"""

import subprocess
import socket
import time
import sys
import atexit
import argparse
import shutil
import yaml
import os
import errno
from datetime import datetime, timezone
from skyfield.api import load, wgs84, EarthSatellite

CONFIG_PATH = "satellites.yaml"
SCHEDULE_PATH = "schedule.yaml"
SPEED_OF_LIGHT = 299792458.0
LOCK_PATH = "run_passes.lock"


def notify(title, message):
    """Best-effort desktop notification - silently does nothing if
    notify-send isn't available (e.g. a headless/remote box with no
    display), or if notifications aren't enabled in satellites.yaml."""
    try:
        subprocess.run(["notify-send", title, message], timeout=2,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass


def acquire_lock():
    """Refuses to start a second run_passes.py against the same folder -
    two instances would fight over rigctld/rotctld and the SDR."""
    if os.path.exists(LOCK_PATH):
        with open(LOCK_PATH) as f:
            old_pid = f.read().strip()
        try:
            os.kill(int(old_pid), 0)  # signal 0: just checks it exists
            alive = True
        except (OSError, ValueError) as e:
            alive = isinstance(e, OSError) and e.errno == errno.EPERM
        if alive:
            sys.exit(f"run_passes.py already running (PID {old_pid}, lock file "
                      f"{LOCK_PATH}). Kill it first, or delete {LOCK_PATH} if it's "
                      f"stale (e.g. after a crash).")
        else:
            print(f"Stale lock file found (PID {old_pid} is not running) - removing it.")

    with open(LOCK_PATH, "w") as f:
        f.write(str(os.getpid()))
    atexit.register(lambda: os.path.exists(LOCK_PATH) and os.remove(LOCK_PATH))


def load_yaml(path):
    with open(path) as f:
        return yaml.safe_load(f)


def load_tles(path, wanted_norads):
    sats = {}
    with open(path) as f:
        lines = [l.strip() for l in f if l.strip()]
    ts = load.timescale()
    for i in range(0, len(lines), 3):
        name, l1, l2 = lines[i], lines[i + 1], lines[i + 2]
        sat = EarthSatellite(l1, l2, name, ts)
        if sat.model.satnum in wanted_norads:
            sats[sat.model.satnum] = sat
    return sats


def parse_iso(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def format_countdown(td):
    total = int(td.total_seconds())
    if total < 0:
        total = 0
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours:02d}:{mins:02d}:{secs:02d}"
    return f"{hours:02d}:{mins:02d}:{secs:02d}"


def clear_line():
    # \x1b[K clears from cursor to end of line regardless of terminal width -
    # avoids the wrapping artifacts a fixed-width space-padded clear causes
    # when the terminal is narrower than the padding.
    sys.stdout.write("\r\x1b[K")


def write_status(text):
    cols = shutil.get_terminal_size(fallback=(100, 24)).columns
    sys.stdout.write("\r" + text[:cols - 1] + "\x1b[K")
    sys.stdout.flush()


def elevation_deg(sat, observer, t):
    el, az, _ = (sat - observer).at(t).altaz()
    return el.degrees, az.degrees


def range_km(sat, observer, t):
    return (sat - observer).at(t).distance().km


def doppler_hz(sat, observer, t, freq_hz, dt=0.5):
    ts = load.timescale()
    t2 = ts.tt_jd(t.tt + dt / 86400.0)
    range_rate_km_s = (range_km(sat, observer, t2) - range_km(sat, observer, t)) / dt
    return -freq_hz * (range_rate_km_s * 1000.0) / SPEED_OF_LIGHT


class Rotctld:
    """Client for a rotctld YOU start separately, pointed at your real rotor
    (or its Dummy backend for testing). This class does not launch rotctld -
    unlike rigctld's Dummy backend, a rotor daemon needs your actual serial
    port and rotor model, which only you can supply."""

    def __init__(self, host, port):
        self.host, self.port = host, port
        self.sock = None
        self.last = None  # (az, el) last commanded, to avoid chatter

    def _connect(self):
        self.sock = socket.create_connection((self.host, self.port), timeout=3)

    def point(self, az_deg, el_deg, min_move_deg=1.0):
        if self.last is not None:
            daz = abs(az_deg - self.last[0])
            delv = abs(el_deg - self.last[1])
            if daz < min_move_deg and delv < min_move_deg:
                return
        try:
            if self.sock is None:
                self._connect()
            self.sock.sendall(f"P {az_deg:.1f} {el_deg:.1f}\n".encode())
            self.sock.settimeout(0.5)
            self.sock.recv(64)
            self.last = (az_deg, el_deg)
        except OSError:
            self.sock = None  # reconnect next call


class Rigctld:
    def __init__(self, port):
        self.port = port
        self.proc = subprocess.Popen(["rigctld", "-m", "1", "-t", str(port)])
        atexit.register(self.stop)
        time.sleep(1)
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)

    def set_freq(self, freq_hz):
        try:
            self.sock.sendall(f"F {int(round(freq_hz))}\n".encode())
            self.sock.settimeout(0.5)
            self.sock.recv(64)
        except OSError:
            self.sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
            self.sock.sendall(f"F {int(round(freq_hz))}\n".encode())

    def stop(self):
        try:
            self.sock.close()
        except OSError:
            pass
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true",
                     help="print each Doppler/rotor update while a pass is active")
    args = ap.parse_args()

    acquire_lock()

    cfg = load_yaml(CONFIG_PATH)
    notify_enabled = bool(cfg.get("notify", False))
    schedule = load_yaml(SCHEDULE_PATH)
    gs = cfg["ground_station"]
    observer = wgs84.latlon(gs["lat"], gs["lon"], gs["alt_m"])
    sat_cfgs = {c["norad"]: c for c in cfg["satellites"]}
    for norad, sc in sat_cfgs.items():
        try:
            sc["freq_hz"] = float(sc["freq_hz"])
        except (TypeError, ValueError):
            sys.exit(f"satellites.yaml: {sc['name']}'s freq_hz ({sc['freq_hz']!r}) "
                      f"isn't a valid number - check for stray quotes around it.")
    tles = load_tles(cfg["tle_file"], set(sat_cfgs))

    passes = [p for p in schedule["passes"] if p.get("approved")]
    for p in passes:
        p["aos_dt"] = parse_iso(p["aos"])
        p["los_dt"] = parse_iso(p["los"])
    passes.sort(key=lambda p: p["aos_dt"])

    print(f"{len(passes)} approved pass(es) loaded from {SCHEDULE_PATH}.")
    if not passes:
        print("Nothing approved - run plan_passes.py (optionally --interactive) first.")
        return

    rig = Rigctld(cfg["rig_port"])
    rot = None
    if "rot_host" in cfg and "rot_port" in cfg:
        rot = Rotctld(cfg["rot_host"], cfg["rot_port"])
        print(f"Antenna control enabled: rotctld at {cfg['rot_host']}:{cfg['rot_port']}")
    else:
        print("No rot_host/rot_port in satellites.yaml - antenna will not be steered.")
    ts = load.timescale()

    active_pass = None
    active_proc = None
    is_tty = sys.stdout.isatty()
    last_status_print = None  # for non-tty: only print countdown periodically

    print("Executor running. Ctrl-C to stop.")
    try:
        while True:
            now = datetime.now(timezone.utc)
            t = ts.now()

            if active_pass is not None:
                sat_cfg = sat_cfgs[active_pass["norad"]]
                sat = tles.get(active_pass["norad"])
                try:
                    if active_proc.poll() is not None:
                        if is_tty:
                            clear_line()
                        print(f"[{sat_cfg['name']}] flowgraph exited early "
                              f"(code {active_proc.returncode}) - check its output above. "
                              f"Abandoning this pass; rotor/Doppler stopped for it.")
                        active_pass, active_proc = None, None
                        time.sleep(1)
                        continue

                    el_deg, az_deg = elevation_deg(sat, observer, t)
                    past_los = now >= active_pass["los_dt"]
                    below_elev = el_deg < sat_cfg["min_elev_deg"]
                    if past_los or below_elev:
                        if is_tty:
                            clear_line()
                        print(f"[{sat_cfg['name']}] LOS ({'scheduled' if past_los else 'elevation safety net'})")
                        if notify_enabled:
                            notify("Satellite pass ended", f"{sat_cfg['name']} - LOS")
                        active_proc.terminate()
                        try:
                            active_proc.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            active_proc.kill()
                        active_pass, active_proc = None, None
                    else:
                        dop = doppler_hz(sat, observer, t, sat_cfg["freq_hz"])
                        corrected = sat_cfg["freq_hz"] + dop
                        rig.set_freq(corrected)
                        if rot is not None:
                            rot.point(az_deg, el_deg)
                        if args.verbose:
                            remaining = format_countdown(active_pass["los_dt"] - now)
                            print(f"[{sat_cfg['name']}] el={el_deg:5.1f} az={az_deg:5.1f}  "
                                  f"freq={corrected:,.0f} Hz (doppler {dop:+.0f} Hz)  "
                                  f"LOS in {remaining}")
                except Exception as e:
                    if is_tty:
                        clear_line()
                    print(f"[{sat_cfg['name']}] ERROR during pass ({e!r}) - "
                          f"abandoning this pass rather than crashing the scheduler. "
                          f"Killing its flowgraph so it doesn't record silently forever.")
                    try:
                        active_proc.terminate()
                        active_proc.wait(timeout=10)
                    except Exception:
                        pass
                    active_pass, active_proc = None, None

            if active_pass is None:
                for p in passes:
                    if p["aos_dt"] <= now < p["los_dt"]:
                        if is_tty:
                            clear_line()
                        sat_cfg = sat_cfgs[p["norad"]]
                        print(f"[{sat_cfg['name']}] AOS - launching {sat_cfg['script']}")
                        if notify_enabled:
                            notify("Satellite pass starting", f"{sat_cfg['name']} - AOS")
                        active_proc = subprocess.Popen([sys.executable, "-u", sat_cfg["script"]])
                        active_pass = p
                        time.sleep(3)  # let the flowgraph come up before polling rigctld
                        break

            if active_pass is None:
                upcoming = [p for p in passes if p["aos_dt"] > now]
                if upcoming:
                    nxt = upcoming[0]
                    sat_cfg = sat_cfgs[nxt["norad"]]
                    status = (f"Next pass: {sat_cfg['name']} in "
                              f"{format_countdown(nxt['aos_dt'] - now)} "
                              f"(AOS {nxt['aos']}, max el {nxt.get('max_elevation_deg', '?')} deg)")
                else:
                    status = "No further approved passes in schedule.yaml."

                if is_tty:
                    write_status(status)
                elif last_status_print is None or (now - last_status_print).total_seconds() >= 60:
                    print(status)
                    last_status_print = now

            time.sleep(1)
    except KeyboardInterrupt:
        if is_tty:
            clear_line()
        if active_proc is not None:
            active_proc.terminate()
            try:
                active_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                active_proc.kill()
        rig.stop()
        print("Stopped.")


if __name__ == "__main__":
    main()
