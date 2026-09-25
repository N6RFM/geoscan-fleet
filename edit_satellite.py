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

    # extra_outputs - a satellite with a second live output that a
    # specific downstream app connects to directly, bypassing relay.py
    # (see check_extra_outputs() in preflight.py for the full rationale).
    # Adding with a name that already exists on this satellite replaces
    # that entry rather than duplicating it, so re-running the same
    # command with a corrected value is safe.
    python3 edit_satellite.py NAME --extra-output-name ssdv_viewer \\
        --extra-output-protocol tcp_server \\
        --extra-output-block network_socket_pdu_0 --extra-output-port 9985

    python3 edit_satellite.py NAME --extra-output-name telemetry_upload_agent \\
        --extra-output-protocol zeromq_pub \\
        --extra-output-block zeromq_pub_msg_sink_0 \\
        --extra-output-address tcp://127.0.0.1:5556

    python3 edit_satellite.py NAME --remove-extra-output ssdv_viewer
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

    ap.add_argument("--extra-output-name", default=None,
                     help="add/replace an extra_outputs entry with this name")
    ap.add_argument("--extra-output-protocol", choices=["tcp_server", "tcp_client", "zeromq_pub"],
                     default=None)
    ap.add_argument("--extra-output-block", default=None,
                     help="the block's exact name in the .grc, e.g. network_socket_pdu_0")
    ap.add_argument("--extra-output-port", type=int, default=None,
                     help="for tcp_server/tcp_client protocols")
    ap.add_argument("--extra-output-address", default=None,
                     help="for zeromq_pub protocol, e.g. tcp://127.0.0.1:5556")
    ap.add_argument("--remove-extra-output", default=None, metavar="NAME",
                     help="remove the named extra_outputs entry")

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

    if args.remove_extra_output:
        existing = sat.get("extra_outputs", [])
        before = len(existing)
        sat["extra_outputs"] = [e for e in existing if e.get("name") != args.remove_extra_output]
        after = len(sat["extra_outputs"])
        if after == before:
            sys.exit(f"No extra_output named {args.remove_extra_output!r} on {args.name}")
        changes.append(f"removed extra_output '{args.remove_extra_output}'")
        if not sat["extra_outputs"]:
            del sat["extra_outputs"]

    if args.extra_output_name:
        if not args.extra_output_protocol or not args.extra_output_block:
            sys.exit("--extra-output-name needs --extra-output-protocol and "
                      "--extra-output-block too")
        if args.extra_output_protocol == "zeromq_pub" and not args.extra_output_address:
            sys.exit("protocol zeromq_pub needs --extra-output-address")
        if args.extra_output_protocol in ("tcp_server", "tcp_client") and args.extra_output_port is None:
            sys.exit(f"protocol {args.extra_output_protocol} needs --extra-output-port")

        entry = {
            "name": args.extra_output_name,
            "protocol": args.extra_output_protocol,
            "block": args.extra_output_block,
        }
        if args.extra_output_protocol == "zeromq_pub":
            entry["address"] = args.extra_output_address
        else:
            entry["port"] = args.extra_output_port

        existing = sat.setdefault("extra_outputs", [])
        replaced = False
        for i, e in enumerate(existing):
            if e.get("name") == args.extra_output_name:
                existing[i] = entry
                replaced = True
                break
        if not replaced:
            existing.append(entry)
        changes.append(f"{'replaced' if replaced else 'added'} extra_output "
                        f"'{args.extra_output_name}'")

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

    if args.extra_output_name or args.remove_extra_output:
        print(f"\nNote: this only updates satellites.yaml. If the .grc's actual block "
              f"(name, port, or address) needs to change too, edit that separately - "
              f"this doesn't touch .grc content for extra_outputs.")


if __name__ == "__main__":
    main()
