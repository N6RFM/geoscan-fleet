#!/usr/bin/env python3
"""
End-to-end sanity check for the fleet, meant to be run before every session
(and after any .grc/.yaml edit) to catch exactly the class of bugs that
have bitten us before: typos in satellites.yaml, freq/nfreq mismatches
between satellites.yaml and a .grc, network_socket_pdu silently reverting
to TCP_SERVER, missing decoder files, stale/uncompiled .py files, port
collisions, missing TLE entries.

Usage:
    python3 preflight.py            # static checks only (fast, safe, no hardware)
    python3 preflight.py --live     # also briefly launches each flowgraph
                                     # to confirm it starts and connects
                                     # correctly (uses real SDR hardware)
    python3 preflight.py --live --only GEOSCAN-2   # live-test one satellite
"""

import argparse
import os
import shutil
import socket
import subprocess
import sys
import time
import yaml

CONFIG_PATH = "satellites.yaml"
SCHEDULE_PATH = "schedule.yaml"

PASS, FAIL, WARN = "PASS", "FAIL", "WARN"
results = []


def check(name, ok, detail="", level=None):
    status = level or (PASS if ok else FAIL)
    results.append((status, name, detail))
    marker = {"PASS": " ok ", "FAIL": "FAIL", "WARN": "warn"}[status]
    line = f"[{marker}] {name}"
    if detail:
        line += f" - {detail}"
    print(line)
    return ok


def slug_of(name):
    return name.lower().replace("-", "")


def static_checks(cfg_path):
    print("=== Static config checks ===")
    if not check("satellites.yaml exists", os.path.exists(cfg_path)):
        return None
    try:
        with open(cfg_path) as f:
            cfg = yaml.safe_load(f)
        check("satellites.yaml parses as YAML", True)
    except yaml.YAMLError as e:
        check("satellites.yaml parses as YAML", False, str(e))
        return None

    gs = cfg.get("ground_station", {})
    for key in ("lat", "lon", "alt_m"):
        val = gs.get(key)
        check(f"ground_station.{key} is numeric", isinstance(val, (int, float)),
              f"got {val!r}")

    check("tle_file is set", bool(cfg.get("tle_file")))
    tle_path = cfg.get("tle_file", "")
    tle_exists = os.path.exists(tle_path) and os.path.getsize(tle_path) > 0
    check(f"TLE file exists and is non-empty ({tle_path})", tle_exists)

    check("rig_port is set", isinstance(cfg.get("rig_port"), int))
    has_rotor = "rot_host" in cfg and "rot_port" in cfg
    if has_rotor:
        check("rot_port is set", isinstance(cfg.get("rot_port"), int))
    else:
        check("rotor control configured", True, "none configured - running without antenna control", level=WARN)

    for tool, needed in (("rigctld", True), ("rotctld", has_rotor)):
        found = shutil.which(tool) is not None
        if needed:
            check(f"{tool} is installed", found,
                  "" if found else f"install libhamlib-utils (or equivalent) - {tool} not on PATH")

    for pkg in ("yaml", "skyfield"):
        try:
            __import__(pkg)
            check(f"python package '{pkg}' importable", True)
        except ImportError:
            check(f"python package '{pkg}' importable", False,
                  "pip install skyfield pyyaml --break-system-packages")

    sats = cfg.get("satellites", [])
    check("at least one satellite configured", len(sats) > 0)

    all_norads, all_ports = set(), {}
    tle_names = set()
    if tle_exists:
        with open(tle_path) as f:
            lines = [l.strip() for l in f if l.strip()]
        for i in range(0, len(lines) - 2, 3):
            try:
                norad = int(lines[i + 1][2:7])
                tle_names.add(norad)
            except (ValueError, IndexError):
                pass

    for sat in sats:
        name = sat.get("name", "<unnamed>")
        if not sat.get("enabled", True):
            print(f"--- {name} (disabled - skipping) ---")
            continue
        print(f"--- {name} ---")

        for field in ("norad", "freq_hz", "script", "min_elev_deg"):
            check(f"{name}.{field} present", field in sat)

        uses_relay = "producer_port" in sat or "consumer_port" in sat
        if uses_relay:
            for field in ("producer_port", "consumer_port"):
                check(f"{name}.{field} present", field in sat)
        else:
            check(f"{name}: recording-only, no relay configured", True)

        freq_ok = isinstance(sat.get("freq_hz"), (int, float))
        check(f"{name}.freq_hz is numeric", freq_ok, f"got {sat.get('freq_hz')!r}")

        norad = sat.get("norad")
        if norad is not None:
            dup = norad in all_norads
            check(f"{name}.norad ({norad}) is unique", not dup)
            all_norads.add(norad)
            check(f"{name}.norad ({norad}) found in TLE file", norad in tle_names)

        for port_field in ("producer_port", "consumer_port"):
            p = sat.get(port_field)
            if p is not None:
                dup = p in all_ports
                check(f"{name}.{port_field} ({p}) is unique across fleet",
                      not dup, "" if not dup else f"also used by {all_ports.get(p)}")
                all_ports[p] = f"{name}.{port_field}"

        script = sat.get("script", "")
        grc_path = script.replace(".py", ".grc") if script else ""
        py_exists = os.path.exists(script) if script else False
        grc_exists = os.path.exists(grc_path) if grc_path else False
        check(f"{name}: {grc_path} exists", grc_exists)
        check(f"{name}: {script} exists (compiled)", py_exists,
              "" if py_exists else f"run: grcc {grc_path}")
        if py_exists and grc_exists and os.path.getmtime(grc_path) > os.path.getmtime(script):
            check(f"{name}: .py is up to date with .grc", False,
                  f"{grc_path} was edited after {script} was generated - re-run grcc")

        if grc_exists:
            check_grc(name, grc_path, sat)

    return cfg


def check_grc(name, grc_path, sat):
    try:
        with open(grc_path) as f:
            grc = yaml.safe_load(f)
    except yaml.YAMLError as e:
        check(f"{name}: {grc_path} parses as YAML", False, str(e))
        return
    blocks = {b["name"]: b for b in grc.get("blocks", [])}

    for var in ("freq", "nfreq"):
        if var in blocks:
            grc_val = blocks[var]["parameters"].get("value")
            try:
                match = int(float(grc_val)) == int(float(sat["freq_hz"]))
            except (TypeError, ValueError):
                match = False
            check(f"{name}: {var} in .grc matches satellites.yaml freq_hz",
                  match, f".grc={grc_val}  yaml={sat.get('freq_hz')}")

    sock_block = blocks.get("network_socket_pdu_0")
    uses_relay = "producer_port" in sat or "consumer_port" in sat
    if uses_relay:
        if sock_block:
            sock_type = sock_block["parameters"].get("type", "")
            check(f"{name}: network_socket_pdu type is TCP_CLIENT",
                  "TCP_CLIENT" in str(sock_type), f"got {sock_type!r}")
            sock_port = sock_block["parameters"].get("port")
            try:
                port_match = int(sock_port) == int(sat["producer_port"])
            except (TypeError, ValueError):
                port_match = False
            check(f"{name}: network_socket_pdu port matches producer_port",
                  port_match, f".grc={sock_port}  yaml={sat.get('producer_port')}")
        else:
            check(f"{name}: has a network_socket_pdu block", False,
                  "relay connection missing entirely")
    elif sock_block:
        check(f"{name}: network_socket_pdu present but no producer_port/consumer_port "
              f"configured", False,
              "recording-only satellite has a live relay connection in its .grc - "
              "either add producer_port/consumer_port to satellites.yaml, or remove "
              "the network_socket_pdu block if this is intentionally recording-only")

    dec_block = blocks.get("satellites_satellite_decoder_0")
    if dec_block:
        dec_file = dec_block["parameters"].get("file", "").strip("'\"")
        if dec_file:
            check(f"{name}: decoder file exists ({dec_file})", os.path.exists(dec_file))
        else:
            check(f"{name}: decoder file left blank (relying on norad lookup)", True, level=WARN)

    sink_block = blocks.get("filerepeater_AdvFileSink_0")
    if sink_block:
        record_on_start = str(sink_block["parameters"].get("recordOnStart", "")).lower()
        check(f"{name}: recordOnStart is True", record_on_start == "true")


def live_check(cfg, only=None, duration=8):
    print("\n=== Live checks (launches real flowgraphs briefly) ===")
    for sat in cfg["satellites"]:
        if only and sat["name"] != only:
            continue
        name = sat["name"]
        if not sat.get("enabled", True):
            print(f"--- {name} (disabled - skipping) ---")
            continue
        print(f"--- {name} live test ---")

        uses_relay = "producer_port" in sat or "consumer_port" in sat

        if not uses_relay:
            # recording-only satellite - nothing to bind or connect to, just
            # confirm the flowgraph itself starts and doesn't crash
            proc = subprocess.Popen([sys.executable, "-u", sat["script"]],
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True)
            time.sleep(min(duration, 8))
            alive = proc.poll() is None
            if not alive:
                out = proc.stdout.read() if proc.stdout else ""
                check(f"{name}: flowgraph did not crash", False,
                      out.strip().splitlines()[-1] if out.strip() else "exited early")
            else:
                check(f"{name}: flowgraph did not crash", True)
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
            time.sleep(2)
            continue

        # stand in for relay.py's producer-side listener, unless relay.py
        # is already running and already owns this port - in that case,
        # just confirm the flowgraph doesn't crash; we can't also verify
        # the connection without relay.py exposing that itself.
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        port_owned_by_relay = False
        try:
            listener.bind(("127.0.0.1", sat["producer_port"]))
            listener.listen(1)
            listener.settimeout(duration)
        except OSError as e:
            if "Address already in use" in str(e):
                check(f"{name}: producer_port {sat['producer_port']} already bound "
                      f"(relay.py appears to be running)", True, level=WARN)
                port_owned_by_relay = True
            else:
                check(f"{name}: can bind test listener on producer_port {sat['producer_port']}",
                      False, str(e))
                continue

        proc = subprocess.Popen([sys.executable, "-u", sat["script"]],
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True)

        if port_owned_by_relay:
            # can't verify the connection itself without relay.py's
            # cooperation - just give the flowgraph a moment, then check
            # it's still alive.
            time.sleep(min(duration, 5))
            check(f"{name}: connection check skipped (stop relay.py to test this directly)",
                  True, level=WARN)
        else:
            connected = False
            try:
                conn, _ = listener.accept()
                connected = True
                conn.close()
            except socket.timeout:
                pass
            finally:
                listener.close()
            check(f"{name}: flowgraph connects to relay as TCP_CLIENT", connected,
                  "" if connected else "no connection arrived within timeout - "
                                         "check network_socket_pdu type/port")

        alive = proc.poll() is None
        if not alive:
            out = proc.stdout.read() if proc.stdout else ""
            check(f"{name}: flowgraph did not crash", False,
                  out.strip().splitlines()[-1] if out.strip() else "exited early")
        else:
            check(f"{name}: flowgraph did not crash", True)
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        time.sleep(2)  # let the SDR device release before the next satellite


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true",
                     help="also briefly launch each flowgraph (uses real SDR hardware)")
    ap.add_argument("--only", help="with --live, test only this satellite name")
    ap.add_argument("--duration", type=int, default=8,
                     help="seconds to wait for the relay connection in --live mode")
    args = ap.parse_args()

    cfg = static_checks(CONFIG_PATH)

    if os.path.exists(SCHEDULE_PATH) and cfg:
        print("\n=== schedule.yaml checks ===")
        try:
            with open(SCHEDULE_PATH) as f:
                sched = yaml.safe_load(f)
            check("schedule.yaml parses as YAML", True)
            known = {s["norad"] for s in cfg["satellites"]}
            for p in sched.get("passes", []):
                check(f"schedule entry for {p.get('name')} has a known norad",
                      p.get("norad") in known)
        except yaml.YAMLError as e:
            check("schedule.yaml parses as YAML", False, str(e))

    if args.live and cfg:
        live_check(cfg, only=args.only, duration=args.duration)

    n_fail = sum(1 for s, _, _ in results if s == FAIL)
    n_warn = sum(1 for s, _, _ in results if s == WARN)
    n_pass = sum(1 for s, _, _ in results if s == PASS)
    print(f"\n{n_pass} passed, {n_warn} warnings, {n_fail} failed.")
    print("Preflight finished.")
    sys.stdout.flush()
    sys.exit(1 if n_fail else 0)


if __name__ == "__main__":
    main()
