#!/usr/bin/env python3
"""
Edit an existing satellite's fields in satellites.yaml - and, where it's
unambiguous, keep its .grc in sync automatically the same way
add_satellite.py does when a satellite is first created.

Only touches fields you actually pass. Leaves everything else alone.

Usage:
    python3 edit_satellite.py NAME --norad 12345
    python3 edit_satellite.py NAME --freq 437443000
    python3 edit_satellite.py NAME --min-elev 20
    python3 edit_satellite.py NAME --producer-port 9107 --consumer-port 8107
    python3 edit_satellite.py NAME --enabled
    python3 edit_satellite.py NAME --disabled

Does NOT currently support editing extra_outputs - that still needs
hand-editing satellites.yaml directly (and the .grc, if the block itself
needs to change). A satellite using extra_outputs is flagged so you
don't forget to check it after any edit here.
"""

import argparse
import os
import sys
import yaml

CONFIG_PATH = "satellites.yaml"


def slug_for(sat):
    return sat.get("script", "").replace("flowgraphs/", "").replace(".py", "")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("name", help="satellite name to edit, exactly as it appears in satellites.yaml")
    ap.add_argument("--norad", type=int, default=None)
    ap.add_argument("--freq", type=int, default=None, help="downlink frequency in Hz")
    ap.add_argument("--min-elev", type=float, default=None)
    ap.add_argument("--producer-port", type=int, default=None)
    ap.add_argument("--consumer-port", type=int, default=None)
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--enabled", action="store_true")
    group.add_argument("--disabled", action="store_true")
    args = ap.parse_args()

    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    sats = cfg.get("satellites", [])

    sat = next((s for s in sats if s["name"] == args.name), None)
    if sat is None:
        names = ", ".join(s["name"] for s in sats)
        sys.exit(f"No satellite named {args.name!r} in {CONFIG_PATH}. Configured: {names}")

    changes = []

    if args.norad is not None and args.norad != sat.get("norad"):
        if any(s is not sat and s.get("norad") == args.norad for s in sats):
            sys.exit(f"NORAD {args.norad} is already used by another satellite - refusing")
        changes.append(f"norad: {sat.get('norad')} -> {args.norad}")
        sat["norad"] = args.norad

    if args.freq is not None and args.freq != sat.get("freq_hz"):
        changes.append(f"freq_hz: {sat.get('freq_hz')} -> {args.freq}")
        sat["freq_hz"] = args.freq

    if args.min_elev is not None and args.min_elev != sat.get("min_elev_deg"):
        changes.append(f"min_elev_deg: {sat.get('min_elev_deg')} -> {args.min_elev}")
        sat["min_elev_deg"] = args.min_elev

    for port_field, arg_val in (("producer_port", args.producer_port),
                                 ("consumer_port", args.consumer_port)):
        if arg_val is not None and arg_val != sat.get(port_field):
            if any(s is not sat and s.get(port_field) == arg_val for s in sats):
                sys.exit(f"{port_field} {arg_val} is already used by another satellite - refusing")
            changes.append(f"{port_field}: {sat.get(port_field)} -> {arg_val}")
            sat[port_field] = arg_val

    if args.enabled:
        if sat.get("enabled", True) is not True:
            changes.append("enabled: false -> true")
        sat["enabled"] = True
    elif args.disabled:
        if sat.get("enabled", True) is not False:
            changes.append("enabled: true -> false")
        sat["enabled"] = False

    if not changes:
        print(f"No changes given for {args.name} - nothing to do.")
        return

    with open(CONFIG_PATH, "w") as f:
        yaml.dump(cfg, f, sort_keys=False, default_flow_style=False)

    print(f"Updated {args.name} in {CONFIG_PATH}:")
    for c in changes:
        print(f"  {c}")

    # keep the .grc in sync for the two fields that live there too,
    # the same way add_satellite.py sets them on creation
    grc_path = f"flowgraphs/{slug_for(sat)}.grc"
    if (args.norad is not None or args.freq is not None) and os.path.exists(grc_path):
        with open(grc_path) as f:
            grc = yaml.safe_load(f)
        blocks = {b["name"]: b for b in grc.get("blocks", [])}
        grc_changed = False

        if args.freq is not None:
            for var in ("freq", "nfreq"):
                if var in blocks:
                    blocks[var]["parameters"]["value"] = str(args.freq)
                    grc_changed = True

        if args.norad is not None:
            dec = blocks.get("satellites_satellite_decoder_0")
            if dec:
                dec["parameters"]["norad"] = str(args.norad)
                grc_changed = True
            sub = blocks.get("satellites_telemetry_submit_0")
            if sub:
                sub["parameters"]["norad"] = str(args.norad)
                grc_changed = True

        if grc_changed:
            with open(grc_path, "w") as f:
                yaml.dump(grc, f, sort_keys=False, default_flow_style=False)
            print(f"\nAlso updated {grc_path} to match - re-run: grcc {grc_path}")

    if sat.get("extra_outputs"):
        print(f"\nNote: {args.name} has extra_outputs declared - this script doesn't "
              f"touch those. If anything here affects them, check satellites.yaml and "
              f"the .grc's matching blocks by hand.")


if __name__ == "__main__":
    main()
