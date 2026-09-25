#!/usr/bin/env python3
"""
Vets a .grc file for the specific classes of bugs this project has
actually hit, repeatedly, from hand-editing in GRC - especially from
copy/pasting blocks, which GRC handles by silently appending _0, _0_0,
etc. to the new block's name to avoid a collision, rather than warning
you it happened.

By default, purely read-only. --fix applies ONLY the two issues that are
a pure rename with no structural change at all - a block's name, or the
flowgraph's own options.id - never anything that requires deciding which
of two duplicate blocks to keep, or how to rewire a missing mechanism.
Those still need a human decision in GRC; --fix refuses to touch them.
Every fix shows a diff and is applied only after that, with a .bak of
the original written first.

Usage:
    python3 vet_grc.py flowgraphs/geoscan1.grc
    python3 vet_grc.py                          # vets every flowgraphs/*.grc
    python3 vet_grc.py --fix flowgraphs/asrtussdv.grc
"""

import difflib
import glob
import os
import re
import sys
import yaml

CHECKS_PASSED = 0
CHECKS_FAILED = 0
CHECKS_WARNED = 0

SHOULD_BE_SINGULAR = {
    "network_socket_pdu", "satellites_satellite_decoder",
    "satellites_telemetry_submit", "satellites_kiss_file_sink",
    "satellites_print_timestamp", "satellites_hexdump_sink",
    "analog_sig_source_x", "blocks_multiply_xx", "osmosdr_source",
    "low_pass_filter", "filerepeater_AdvFileSink",
}


def check(label, ok, detail="", level="fail"):
    global CHECKS_PASSED, CHECKS_FAILED, CHECKS_WARNED
    if ok:
        CHECKS_PASSED += 1
        print(f"[ ok ] {label}")
    elif level == "warn":
        CHECKS_WARNED += 1
        print(f"[warn] {label}" + (f" - {detail}" if detail else ""))
    else:
        CHECKS_FAILED += 1
        print(f"[FAIL] {label}" + (f" - {detail}" if detail else ""))


def find_double_suffixed(blocks):
    found = []
    for b in blocks:
        if b["id"] not in SHOULD_BE_SINGULAR:
            continue
        suffix = b["name"][len(b["id"]):]
        numeric_parts = [p for p in suffix.split("_") if p.isdigit()]
        if len(numeric_parts) >= 2:
            found.append(b["name"])
    return found


def find_name_id_mismatches(blocks):
    """A block's name should always start with its own type (id) - GRC's
    own auto-naming guarantees this, but a manual rename in the canvas
    (cleaning up naming conventions, say) can produce a name that doesn't
    actually match the block's real type at all, usually from a typo
    (blocks_multiply_x_0 instead of blocks_multiply_xx_0, say - easy to
    drop a character while retyping). Neither the duplicate-count nor the
    double-suffix check catches this - it's a different kind of mistake,
    not a copy/paste artifact, so it needs its own check."""
    found = []
    for b in blocks:
        if b["id"] in ("parameter", "variable", "variable_qtgui_range",
                       "variable_low_pass_filter_taps", "note", "options"):
            continue
        if not b["name"].startswith(b["id"]):
            found.append((b["name"], b["id"]))
    return found


def vet(path, grc=None):
    print(f"--- {path} ---")
    if grc is None:
        with open(path) as f:
            grc = yaml.safe_load(f)
    blocks = grc.get("blocks", [])

    type_names = {}
    for b in blocks:
        if b["id"] in SHOULD_BE_SINGULAR:
            type_names.setdefault(b["id"], []).append(b["name"])
    any_duplicates = False
    for block_id, names in type_names.items():
        if len(names) > 1:
            any_duplicates = True
            check(f"exactly one '{block_id}' block", False,
                  f"found {len(names)}: {names} - this needs a human decision in "
                  f"GRC about which one to keep and how its wiring should look; "
                  f"--fix won't touch this")
    if not any_duplicates:
        check("no duplicate instances of any block type that should be singular", True)

    double_suffixed = find_double_suffixed(blocks)
    if double_suffixed:
        for name in double_suffixed:
            check(f"'{name}' has a normal single-suffix name", False,
                  f"a double numeric suffix like this is GRC's own telltale for "
                  f"'a block with this name already existed, so I renamed the "
                  f"pasted one' - almost always means this block (or its wiring) "
                  f"came from copying a DIFFERENT file's block, not from adding "
                  f"one fresh from the palette in this file. Fixable with --fix.")
    else:
        check("no block names show GRC's double-suffix copy/paste signature", True)

    mismatches = find_name_id_mismatches(blocks)
    if mismatches:
        for name, block_id in mismatches:
            check(f"'{name}' name matches its own type", False,
                  f"this block's type is '{block_id}', but its name doesn't even "
                  f"start with that - likely a typo from a manual rename (e.g. "
                  f"dropping a character while retyping a longer type name)")
    else:
        check("every block's name matches its own type", True)

    has_gpredict_doppler = any(b["id"] == "gpredict_doppler" for b in blocks)
    check("no gpredict_doppler block present", not has_gpredict_doppler,
          "gpredict_doppler passively waits for a real Gpredict application to "
          "connect to it - nothing in this toolkit ever does that, so this "
          "silently produces zero Doppler correction for an entire pass, with "
          "no error at all. Replace with rig_freq_poller in GRC, matching "
          "geoscan1.grc - not fixable automatically, this is a real rewire.")

    has_freq_var = any(b["id"] == "parameter" and b["name"] == "freq" for b in blocks)
    has_rig_poller = any(b["id"] == "epy_block" and "rig_freq_poller" in b["name"]
                          for b in blocks)
    if has_freq_var:
        check("rig_freq_poller present to drive the live freq variable",
              has_rig_poller,
              "a 'freq' parameter exists (implying live Doppler tracking is "
              "expected) but nothing found that actually polls rigctld to feed "
              "it - check what's actually updating this variable")

    for b in blocks:
        if b["id"] == "osmosdr_source":
            freq0 = str(b["parameters"].get("freq0", ""))
            check(f"{b['name']}: hardware tuning frequency is fixed (nfreq-offset), "
                  f"not the live-updating freq variable",
                  "nfreq" in freq0 and freq0.replace(" ", "").count("freq") <= 2,
                  f"freq0={freq0!r} - re-tuning real hardware mid-pass risks PLL "
                  f"relock glitches; Doppler correction should happen entirely in "
                  f"the Signal Source mixing stage instead, see README")

    slug = os.path.splitext(os.path.basename(path))[0]
    opts_id = grc.get("options", {}).get("parameters", {}).get("id", "")
    check(f"options id ({opts_id!r}) matches the filename ({slug!r})",
          opts_id == slug,
          "grcc names its output after this field, not the .grc filename - a "
          "mismatch here is exactly why a compile can silently land in the "
          "wrong place under the wrong name. Fixable with --fix.")

    has_waterfall = any(b["id"] == "qtgui_waterfall_sink_x" for b in blocks)
    check(f"no Qt waterfall block (real CPU cost for nothing watching it unattended)",
          not has_waterfall, level="warn")

    print()


def apply_fixes(path):
    """Only ever a pure rename: a block's own name field, or options.id.
    Never restructures wiring, removes a block, or resolves a genuine
    duplicate - those need a human decision in GRC, and this refuses to
    guess at them.

    Edits the raw text directly with targeted replacements, rather than
    parsing and re-dumping the whole file through PyYAML - a full
    round-trip doesn't preserve GRC's own flow-style formatting
    (coordinate: [x, y], connections as [a, b, c, d] on one line each),
    which would turn every single line into a spurious diff and defeat
    the entire point of showing one for review."""
    with open(path) as f:
        original_text = f.read()
    grc = yaml.safe_load(original_text)
    blocks = grc.get("blocks", [])

    lines = original_text.splitlines(keepends=True)
    changes = []
    existing_names = {b["name"] for b in blocks}

    for old_name in find_double_suffixed(blocks):
        block = next(b for b in blocks if b["name"] == old_name)
        new_name = f"{block['id']}_0"
        if new_name in existing_names and new_name != old_name:
            print(f"REFUSING to rename '{old_name}' -> '{new_name}': that name "
                  f"already exists on another block. This is a genuine duplicate, "
                  f"not just a misnamed single instance - resolve which one to "
                  f"keep in GRC directly.")
            continue

        name_line_pattern = re.compile(rf"^(- name: |  name: ){re.escape(old_name)}$")
        word_pattern = re.compile(rf"\b{re.escape(old_name)}\b")
        n_replaced = 0
        for i, line in enumerate(lines):
            stripped = line.rstrip("\n")
            if name_line_pattern.match(stripped):
                lines[i] = line.replace(old_name, new_name)
                n_replaced += 1
            elif stripped.strip().startswith("- [") and word_pattern.search(stripped):
                # a flow-style connection line, e.g. "- [old_name, '0', other, '1']"
                lines[i] = word_pattern.sub(new_name, line)
                n_replaced += 1
        changes.append(f"renamed block '{old_name}' -> '{new_name}' "
                        f"({n_replaced} line(s) changed)")

    slug = os.path.splitext(os.path.basename(path))[0]
    opts_id = grc.get("options", {}).get("parameters", {}).get("id", "")
    if opts_id != slug:
        in_options = False
        for i, line in enumerate(lines):
            if line.startswith("options:"):
                in_options = True
                continue
            if in_options and line.startswith("blocks:"):
                break
            if in_options and re.match(rf"^\s*id:\s*{re.escape(opts_id)}\s*$", line):
                lines[i] = line.replace(opts_id, slug)
                changes.append(f"options.id: {opts_id!r} -> {slug!r}")
                break

    if not changes:
        print(f"{path}: nothing to fix.")
        return

    new_text = "".join(lines)

    print(f"=== proposed changes to {path} ===")
    for c in changes:
        print(f"  - {c}")
    print()
    print("=== diff ===")
    diff = difflib.unified_diff(
        original_text.splitlines(keepends=True), lines,
        fromfile=f"{path} (before)", tofile=f"{path} (after)")
    sys.stdout.writelines(diff)
    print()

    backup_path = path + ".bak"
    with open(backup_path, "w") as f:
        f.write(original_text)
    with open(path, "w") as f:
        f.write(new_text)
    print(f"Applied. Original backed up to {backup_path}.")
    print(f"Now re-run grcc on this file, then vet_grc.py again to confirm.\n")


def main():
    args = sys.argv[1:]
    fix = "--fix" in args
    if fix:
        args.remove("--fix")

    if args:
        paths = args
    else:
        paths = sorted(glob.glob("flowgraphs/*.grc"))
        if not paths:
            sys.exit("No flowgraphs/*.grc found - run from the repo root, "
                      "or pass a specific .grc path.")

    for path in paths:
        if not os.path.exists(path):
            print(f"--- {path} --- FILE NOT FOUND\n")
            continue
        if fix:
            apply_fixes(path)
        else:
            vet(path)

    if not fix:
        print(f"{CHECKS_PASSED} passed, {CHECKS_WARNED} warnings, {CHECKS_FAILED} failed.")
        sys.exit(1 if CHECKS_FAILED else 0)


if __name__ == "__main__":
    main()
