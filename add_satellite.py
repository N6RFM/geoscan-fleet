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


def generate_grc(template_path, out_path, name, norad, freq_hz, producer_port):
    with open(template_path) as f:
        data = yaml.safe_load(f)
    blocks = data["blocks"]

    find_block(blocks, "freq")["parameters"]["value"] = str(freq_hz)
    find_block(blocks, "nfreq")["parameters"]["value"] = str(freq_hz)

    dec = find_block(blocks, "satellites_satellite_decoder_0")
    dec["parameters"]["norad"] = str(norad)
    dec["parameters"]["file"] = ""  # blank - relies on norad auto-lookup until you set a real path

    sub = find_block(blocks, "satellites_telemetry_submit_0")
    sub["parameters"]["norad"] = str(norad)

    sink = find_block(blocks, "filerepeater_AdvFileSink_0")
    sink["parameters"]["basefile"] = slugify(name)

    kiss = find_block(blocks, "satellites_kiss_file_sink_0")
    kiss["parameters"]["file"] = f"/home/YOUR_USERNAME/Desktop/{name}.kss"

    sock = find_block(blocks, "network_socket_pdu_0")
    sock["parameters"]["port"] = str(producer_port)
    sock["parameters"]["type"] = "TCP_CLIENT"  # explicit, in case the template ever regresses

    wf = None
    for b in blocks:
        if b["id"] == "qtgui_waterfall_sink_x":
            wf = b
            break
    if wf:
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

    producer_port, consumer_port = next_free_ports(cfg)

    generate_grc(args.template, grc_path, args.name, args.norad, args.freq, producer_port)

    cfg.setdefault("satellites", []).append({
        "name": args.name,
        "norad": args.norad,
        "freq_hz": args.freq,
        "script": f"flowgraphs/{slug}.py",
        "min_elev_deg": args.min_elev,
        "producer_port": producer_port,
        "consumer_port": consumer_port,
    })
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(cfg, f, sort_keys=False, default_flow_style=False)

    print(f"Generated {grc_path} from {args.template}")
    print(f"Added {args.name} to {CONFIG_PATH}: "
          f"producer_port={producer_port}, consumer_port={consumer_port}")
    print(f"\nStill needed:")
    print(f"  1. Point {grc_path}'s decoder file at a real GEOSCAN-*.yml "
          f"(currently blank - relying on norad auto-lookup)")
    print(f"  2. grcc {grc_path}")
    print(f"  3. python3 preflight.py")


if __name__ == "__main__":
    main()
