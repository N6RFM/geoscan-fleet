#!/usr/bin/env python3
"""
Pass planner: predicts upcoming passes for every satellite in
satellites.yaml and lets you approve/reject each one before it's ever
recorded. Writes the result to schedule.yaml, which run_passes.py reads.

Usage:
    python3 plan_passes.py                    # next 24h, list + write, all approved
    python3 plan_passes.py --hours 48
    python3 plan_passes.py --interactive       # prompt y/n per pass
    python3 plan_passes.py --add-satellite     # append a new satellite entry
"""

import argparse
import yaml
from datetime import datetime, timezone
from skyfield.api import load, wgs84, EarthSatellite

CONFIG_PATH = "satellites.yaml"
SCHEDULE_PATH = "schedule.yaml"


def parse_iso(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def load_config():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(cfg, f, sort_keys=False, default_flow_style=False)


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


def find_passes(sat, observer, t0, t1, min_elev_deg):
    """Yields (aos, los, max_elev_deg) for every pass of `sat` above
    min_elev_deg between t0 and t1."""
    times, events = sat.find_events(observer, t0, t1, altitude_degrees=min_elev_deg)
    aos = None
    max_el = None
    culm_t = None
    for t, ev in zip(times, events):
        if ev == 0:          # rise
            aos, max_el, culm_t = t, None, None
        elif ev == 1:        # culminate
            el, _, _ = (sat - observer).at(t).altaz()
            max_el, culm_t = el.degrees, t
        elif ev == 2 and aos is not None:  # set
            yield aos, t, max_el if max_el is not None else 0.0
            aos = None


def find_overlaps(passes):
    """Yield (a, b) for every pair of currently-approved passes, sorted by
    aos, whose [aos, los) windows overlap - the case where the single SDR
    can only actually record whichever one starts first."""
    approved = sorted((p for p in passes if p["approved"]), key=lambda p: p["aos"])
    for i in range(len(approved) - 1):
        a, b = approved[i], approved[i + 1]
        if b["aos"] < a["los"]:
            yield a, b


def resolve_overlaps(passes, interactive):
    """Detect overlapping approved passes and either let the user actively
    choose which one to keep (interactive), or print a clear warning so it's
    not discovered silently later (non-interactive) - see the overlap
    caveat this replaces."""
    conflicts = list(find_overlaps(passes))
    if not conflicts:
        return

    print(f"\n{len(conflicts)} overlap(s) among approved passes - only one "
          f"satellite can record at a time (single SDR, no pre-emption):\n")
    for a, b in conflicts:
        if not a["approved"] or not b["approved"]:
            continue  # already resolved by an earlier conflict in this same run
        overlap_s = (parse_iso(a["los"]) - parse_iso(b["aos"])).total_seconds()
        print(f"  {a['name']:<10} {a['aos']} -> {a['los']}  (max el {a['max_elevation_deg']})")
        print(f"  {b['name']:<10} {b['aos']} -> {b['los']}  (max el {b['max_elevation_deg']})")
        print(f"  -> {overlap_s:.0f}s overlap. Without a choice, {a['name']} "
              f"wins (starts first) and {b['name']} will be skipped or "
              f"badly truncated.")

        if not interactive:
            print(f"  Re-run with --interactive to choose, or hand-edit "
                  f"{SCHEDULE_PATH} to set one side's approved: false.\n")
            continue

        while True:
            choice = input(f"  Keep which? [1] {a['name']}  [2] {b['name']}  "
                            f"[3] both anyway  [4] neither: ").strip()
            if choice == "1":
                b["approved"] = False
                break
            elif choice == "2":
                a["approved"] = False
                break
            elif choice == "3":
                break
            elif choice == "4":
                a["approved"] = False
                b["approved"] = False
                break
            else:
                print("  Please enter 1, 2, 3, or 4.")
        print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=24.0,
                     help="lookahead window in hours (default 24)")
    ap.add_argument("--interactive", action="store_true",
                     help="prompt y/n for each pass instead of approving all")
    ap.add_argument("--add-satellite", action="store_true",
                     help="interactively append a new satellite to satellites.yaml and exit")
    args = ap.parse_args()

    cfg = load_config()

    if args.add_satellite:
        add_satellite(cfg)
        return

    ts = load.timescale()
    gs = cfg["ground_station"]
    observer = wgs84.latlon(gs["lat"], gs["lon"], gs["alt_m"])
    sat_cfgs = {c["norad"]: c for c in cfg["satellites"]}
    tles = load_tles(cfg["tle_file"], set(sat_cfgs))

    missing = set(sat_cfgs) - set(tles)
    if missing:
        print(f"WARNING: no TLE for norad(s) {missing}")

    t0 = ts.now()
    t1 = ts.tt_jd(t0.tt + args.hours / 24.0)

    all_passes = []
    for norad, sat_cfg in sat_cfgs.items():
        sat = tles.get(norad)
        if sat is None:
            continue
        for aos, los, max_el in find_passes(sat, observer, t0, t1, sat_cfg["min_elev_deg"]):
            all_passes.append({
                "name": sat_cfg["name"],
                "norad": int(norad),
                "aos": aos.utc_iso(),
                "los": los.utc_iso(),
                "max_elevation_deg": float(round(max_el, 1)),
                "approved": True,
            })

    all_passes.sort(key=lambda p: p["aos"])

    print(f"\n{len(all_passes)} pass(es) in the next {args.hours:.0f}h "
          f"above each satellite's min_elev_deg:\n")
    for i, p in enumerate(all_passes):
        print(f"  [{i:2d}] {p['aos']}  ->  {p['los']}   "
              f"{p['name']:<12} max el {p['max_elevation_deg']:5.1f} deg")

    if args.interactive:
        for p in all_passes:
            ans = input(f"Record {p['name']} at {p['aos']} "
                         f"(max el {p['max_elevation_deg']})? [Y/n] ").strip().lower()
            p["approved"] = not ans.startswith("n")

    resolve_overlaps(all_passes, args.interactive)

    with open(SCHEDULE_PATH, "w") as f:
        yaml.dump({"passes": all_passes}, f, sort_keys=False, default_flow_style=False)

    n_approved = sum(p["approved"] for p in all_passes)
    print(f"\nWrote {SCHEDULE_PATH}: {n_approved}/{len(all_passes)} approved.")
    if not args.interactive:
        print(f"Edit {SCHEDULE_PATH} directly (set approved: false) to skip any of them, "
              f"or rerun with --interactive to be asked about each one.")


def add_satellite(cfg):
    name = input("Satellite name (e.g. GEOSCAN-3): ").strip()
    norad = int(input("NORAD ID: ").strip())
    freq_hz = int(input("Downlink frequency (Hz): ").strip())
    script = input(f"Flowgraph script path [flowgraphs/{name.lower().replace('-', '')}.py]: ").strip() \
        or f"flowgraphs/{name.lower().replace('-', '')}.py"
    min_elev = float(input("Minimum elevation to record (deg) [15]: ").strip() or "15")
    n_existing = len(cfg.get("satellites", []))
    producer_port = 9101 + n_existing
    consumer_port = 8101 + n_existing
    cfg.setdefault("satellites", []).append({
        "name": name, "norad": norad, "freq_hz": freq_hz, "script": script,
        "min_elev_deg": min_elev, "producer_port": producer_port,
        "consumer_port": consumer_port,
    })
    save_config(cfg)
    print(f"Added {name}: producer_port={producer_port}, consumer_port={consumer_port}")
    print("Remember to also create flowgraphs/<name>.grc for it.")


if __name__ == "__main__":
    main()
