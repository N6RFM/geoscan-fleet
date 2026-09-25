#!/usr/bin/env python3
"""
Adds a new satellite to the fleet: generates its .grc from an existing one
as a template (default: flowgraphs/geoscan1.grc), auto-picks the next free
producer/consumer ports, and appends the config entry to satellites.yaml.

This does the full job - plan_passes.py --add-satellite only adds the
config entry and leaves you to hand-build the .grc yourself.

Usage:
    python3 add_satellite.py --name GEOSCAN-3 --norad 64881 --freq 435530000
    python3 add_satellite.py --name GEOSCAN-3 --norad 64881 --freq 435530000 \
        --template flowgraphs/geoscan2.grc --min-elev 20

After running this you still need to:
  1. Point the new .grc's decoder file at a real GEOSCAN-*.yml (or clear
     it to rely on norad-based auto-lookup)
  2. grcc flowgraphs/<new>.grc
  3. python3 preflight.py to confirm everything lines up
"""

import argparse
import os
import yaml

CONFIG_PATH = "satellites.yaml"


def slugify(name):
    return name.lower().replace("-", "").replace(" ", "")


def find_block(blocks, name):
    for b in blocks:
        if b["name"] == name:
            return b
    raise KeyError(f"block {name!r} not found in template")


def next_free_ports(cfg):
    used_producer = {s["producer_port"] for s in cfg.get("satellites", [])}
    used_consumer = {s["consumer_port"] for s in cfg.get("satellites", [])}
    prod = 9101
    while prod in used_producer:
        prod += 1
    cons = 8101
    while cons in used_consumer:
        cons += 1
    return prod, cons


def generate_grc(template_path, out_path, name, norad, freq_hz, producer_port, record_only=False):
    with open(template_path) as f:
        data = yaml.safe_load(f)
    blocks = data["blocks"]

    find_block(blocks, "freq")["parameters"]["value"] = str(freq_hz)
    find_block(blocks, "nfreq")["parameters"]["value"] = str(freq_hz)

    if not record_only:
        dec = find_block(blocks, "satellites_satellite_decoder_0")
        dec["parameters"]["norad"] = str(norad)
        dec["parameters"]["file"] = ""  # blank - relies on norad auto-lookup until you set a real path

        sub = find_block(blocks, "satellites_telemetry_submit_0")
        sub["parameters"]["norad"] = str(norad)

        kiss = find_block(blocks, "satellites_kiss_file_sink_0")
        kiss["parameters"]["file"] = f"/home/YOUR_USERNAME/Desktop/{name}.kss"

        sock = find_block(blocks, "network_socket_pdu_0")
        sock["parameters"]["port"] = str(producer_port)
        sock["parameters"]["type"] = "TCP_CLIENT"  # explicit, in case the template ever regresses
    else:
        # actually strip the decoder/KISS/telemetry/relay wiring, not just
        # skip setting their parameters - a record-only satellite has no
        # decoder and no relay involvement at all, and leaving these
        # blocks present-but-unconfigured means they silently keep
        # whatever the TEMPLATE satellite's own values were (its decoder
        # file, its producer_port), which preflight.py correctly flags as
        # a real, live relay connection nobody actually configured
        strip_names = set()
        for block_id in ("satellites_satellite_decoder", "satellites_telemetry_submit",
                          "satellites_kiss_file_sink", "network_socket_pdu",
                          "satellites_print_timestamp", "satellites_hexdump_sink"):
            for b in blocks:
                if b["id"] == block_id:
                    strip_names.add(b["name"])
        # kiss_encode_pdu is an embedded Python block (id: epy_block, same
        # as every other embedded block) - it only exists to feed
        # network_socket_pdu, which is already being stripped above, so
        # it can't be matched by id the same way; matched by name prefix
        # instead, since that's the one thing distinguishing it
        for b in blocks:
            if b["id"] == "epy_block" and b["name"].startswith("kiss_encode_pdu"):
                strip_names.add(b["name"])
        if strip_names:
            blocks[:] = [b for b in blocks if b["name"] not in strip_names]
            data["connections"] = [c for c in data.get("connections", [])
                                    if not any(n in strip_names for n in c)]

    sink = find_block(blocks, "filerepeater_AdvFileSink_0")
    sink["parameters"]["basefile"] = slugify(name)
    if record_only:
        sink["parameters"]["recordOnStart"] = "True"

    wf = None
    for b in blocks:
        if b["id"] == "qtgui_waterfall_sink_x":
            wf = b
            break
    if wf:
        if record_only:
            # no downstream tab watches this during automated recording -
            # same reasoning as the fix applied to every GEOSCAN flowgraph:
            # a live FFT+render loop is real, measured CPU cost for a
            # window nobody's looking at unattended
            blocks[:] = [b for b in blocks if b["name"] != wf["name"]]
            wf_name = wf["name"]
            data["connections"] = [c for c in data.get("connections", [])
                                    if wf_name not in c]
        else:
            wf["parameters"]["name"] = name

    slug = slugify(name)
    data["options"]["parameters"]["id"] = slug
    data["options"]["parameters"]["title"] = name

    with open(out_path, "w") as f:
        yaml.dump(data, f, sort_keys=False, default_flow_style=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True, help="e.g. GEOSCAN-3")
    ap.add_argument("--norad", required=True, type=int)
    ap.add_argument("--freq", required=True, type=int, help="downlink frequency in Hz")
    ap.add_argument("--min-elev", type=float, default=15.0)
    ap.add_argument("--template", default="flowgraphs/geoscan1.grc",
                     help="existing .grc to base the new one on (default: geoscan1.grc)")
    ap.add_argument("--record-only", action="store_true",
                     help="no decoder/KISS/telemetry/relay blocks - just RF front end to "
                          "Advanced File Sink for raw IQ recording, no downstream decoder needed")
    ap.add_argument("--disabled", action="store_true",
                     help="add with enabled: false, so it's configured but not yet scheduled "
                          "(toggle on later with toggle_satellite.py --enable)")
    args = ap.parse_args()

    if not os.path.exists(args.template):
        raise SystemExit(f"Template not found: {args.template}")

    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)

    if any(s["norad"] == args.norad for s in cfg.get("satellites", [])):
        raise SystemExit(f"NORAD {args.norad} is already configured - refusing to add a duplicate")

    slug = slugify(args.name)
    grc_path = f"flowgraphs/{slug}.grc"
    if os.path.exists(grc_path):
        raise SystemExit(f"{grc_path} already exists - refusing to overwrite")

    producer_port, consumer_port = (None, None) if args.record_only else next_free_ports(cfg)

    generate_grc(args.template, grc_path, args.name, args.norad, args.freq, producer_port,
                 record_only=args.record_only)

    new_entry = {
        "name": args.name,
        "norad": args.norad,
        "freq_hz": args.freq,
        "script": f"flowgraphs/{slug}.py",
        "min_elev_deg": args.min_elev,
    }
    if not args.record_only:
        new_entry["producer_port"] = producer_port
        new_entry["consumer_port"] = consumer_port
    if args.disabled:
        new_entry["enabled"] = False
    cfg.setdefault("satellites", []).append(new_entry)
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(cfg, f, sort_keys=False, default_flow_style=False)

    print(f"Generated {grc_path} from {args.template}")
    if args.record_only:
        print(f"Added {args.name} to {CONFIG_PATH} as recording-only "
              f"(no producer_port/consumer_port - no relay involvement)")
    else:
        print(f"Added {args.name} to {CONFIG_PATH}: "
              f"producer_port={producer_port}, consumer_port={consumer_port}")
    if args.disabled:
        print(f"Added as disabled - enable later with: "
              f"python3 toggle_satellite.py --enable {args.name}")
    print(f"\nStill needed:")
    if not args.record_only:
        print(f"  1. Point {grc_path}'s decoder file at a real GEOSCAN-*.yml "
              f"(currently blank - relying on norad auto-lookup)")
        print(f"  2. grcc {grc_path}")
        print(f"  3. python3 preflight.py")
    else:
        print(f"  1. grcc {grc_path}")
        print(f"  2. python3 preflight.py")


if __name__ == "__main__":
    main()
