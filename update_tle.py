#!/usr/bin/env python3
"""
Refresh the TLE file used by plan_passes.py/run_passes.py.

Builds the file from a base of one or more Celestrak groups (default:
cubesat + amateur - between them, covering most satellites this kind of
station is likely to track), merged together and de-duplicated. On top
of that base, any specific satellites not covered by those groups (too
new, uncoordinated, or simply in a different group entirely) can be
added individually by catalog number - exactly the situation SCIONX
was in: not in "amateur" or "cubesat", but with a stable, fetchable
NORAD id once you know to ask for it by number instead of by group.

Validates the result before overwriting the real file - a failed/empty/
error-page download would otherwise silently replace good TLE data with
garbage, breaking pass planning for the whole fleet until someone
happened to notice. Also checks every satellite currently configured in
satellites.yaml actually has an entry in the freshly downloaded set.

Usage:
    python3 update_tle.py
        # fetches the default groups (cubesat, amateur), merges them

    python3 update_tle.py --group cubesat --group amateur --group active
        # fetch whatever specific set of groups you want instead

    python3 update_tle.py --extra-catnr 69880
        # default groups, plus one specific satellite by catalog number
        # that isn't in either of them

    python3 update_tle.py --url "https://example.org/my-own-tle-source.txt"
        # merge in a completely custom source alongside the groups

    python3 update_tle.py --check-only
        # just report current file's age and satellite coverage, don't download
"""

import argparse
import os
import shutil
import sys
import tempfile
import time
import urllib.request
import yaml

CONFIG_PATH = "satellites.yaml"
DEFAULT_GROUPS = ["cubesat", "amateur"]
GROUP_URL = "https://celestrak.org/NORAD/elements/gp.php?GROUP={group}&FORMAT=tle"
CATNR_URL = "https://celestrak.org/NORAD/elements/gp.php?CATNR={catnr}&FORMAT=tle"


def load_cfg():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def parse_norads(tle_text):
    """Return the set of NORAD ids present in a TLE text blob."""
    lines = [l.rstrip("\n") for l in tle_text.splitlines() if l.strip()]
    norads = set()
    for i in range(0, len(lines) - 2, 3):
        line1 = lines[i + 1]
        if not line1.startswith("1 "):
            continue
        try:
            norads.add(int(line1[2:7]))
        except ValueError:
            continue
    return norads


def report_coverage(cfg, norads, label):
    configured = {(s["name"], s["norad"]) for s in cfg.get("satellites", [])}
    missing = [(name, norad) for name, norad in configured if norad not in norads]
    print(f"{label}: {len(norads)} satellite(s) in TLE file, "
          f"{len(configured)} configured in {CONFIG_PATH}")
    if missing:
        print(f"  MISSING from TLE file:")
        for name, norad in sorted(missing):
            print(f"    {name} (norad {norad})")
    else:
        print(f"  all configured satellites present.")
    return missing


def fetch(url, label):
    """Download one source, returning (raw_text, norads) or (None, set()) on failure."""
    print(f"Downloading {label} ({url}) ...")
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            data = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  WARNING: fetch failed for {label}: {e} - skipping.")
        return None, set()
    norads = parse_norads(data)
    if not norads:
        print(f"  WARNING: no parseable TLE data for {label} "
              f"({len(data)} bytes received) - skipping.")
        return None, set()
    print(f"  {label}: {len(norads)} satellite(s).")
    return data, norads


def merge_in(combined_data, combined_norads, new_data, new_norads, label):
    """Append only the norads not already present, returning the updated (data, norads)."""
    dupes = new_norads & combined_norads
    if dupes:
        print(f"  {label}: {len(dupes)} satellite(s) already present, skipping those; "
              f"{len(new_norads) - len(dupes)} new.")
    if new_norads - combined_norads:
        combined_data = combined_data.rstrip("\n") + "\n" + new_data.strip("\n") + "\n"
    return combined_data, combined_norads | new_norads


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--group", action="append", default=None, metavar="NAME",
                     help=f"Celestrak group to include in the base set - repeatable "
                          f"(default: {' + '.join(DEFAULT_GROUPS)})")
    ap.add_argument("--url", action="append", default=[], metavar="URL",
                     help="an additional custom TLE source URL to merge in alongside "
                          "the groups - repeatable")
    ap.add_argument("--extra-catnr", action="append", type=int, default=[], metavar="NORAD",
                     help="also fetch this satellite individually by catalog number and "
                          "merge it in, for satellites no group/URL above covers - repeatable")
    ap.add_argument("--check-only", action="store_true",
                     help="just report current file's age and satellite coverage, don't download")
    args = ap.parse_args()
    groups = args.group if args.group is not None else DEFAULT_GROUPS

    cfg = load_cfg()
    tle_path = cfg["tle_file"]

    if args.check_only:
        if not os.path.exists(tle_path):
            sys.exit(f"{tle_path} does not exist.")
        age_h = (time.time() - os.path.getmtime(tle_path)) / 3600
        print(f"{tle_path}: {age_h:.1f} hour(s) old")
        with open(tle_path) as f:
            norads = parse_norads(f.read())
        missing = report_coverage(cfg, norads, "Current file")
        sys.exit(1 if missing else 0)

    combined_data, combined_norads = "", set()

    for group in groups:
        data, norads = fetch(GROUP_URL.format(group=group), f"group '{group}'")
        if data:
            combined_data, combined_norads = merge_in(
                combined_data, combined_norads, data, norads, f"group '{group}'")

    for url in args.url:
        data, norads = fetch(url, "custom URL")
        if data:
            combined_data, combined_norads = merge_in(
                combined_data, combined_norads, data, norads, "custom URL")

    if not combined_norads:
        sys.exit(f"No usable TLE data from any source - refusing to overwrite {tle_path}. "
                  f"The existing file is untouched.")

    for catnr in args.extra_catnr:
        data, norads = fetch(CATNR_URL.format(catnr=catnr), f"catalog number {catnr}")
        if data:
            if catnr not in norads:
                print(f"  WARNING: fetched data for {catnr} but couldn't confirm that "
                      f"exact norad is in it - merging anyway, double check the result.")
            combined_data, combined_norads = merge_in(
                combined_data, combined_norads, data, norads, f"catalog number {catnr}")

    missing = report_coverage(cfg, combined_norads, "\nCombined result")

    # write to a temp file first, then move into place, so a crash or
    # interruption partway through can never leave tle_file half-written
    tle_dir = os.path.dirname(os.path.abspath(tle_path)) or "."
    fd, tmp_path = tempfile.mkstemp(dir=tle_dir, prefix=".tle_download_")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(combined_data)
        shutil.move(tmp_path, tle_path)
    except Exception:
        os.unlink(tmp_path)
        raise

    print(f"Wrote {tle_path} ({len(combined_norads)} satellite(s) total).")
    if missing:
        print(f"\nWARNING: {len(missing)} configured satellite(s) still missing (see above). "
              f"Their pass planning will fail until this is resolved - add the group they're "
              f"actually in via --group, or fetch them individually with --extra-catnr.")
        sys.exit(1)


if __name__ == "__main__":
    main()
