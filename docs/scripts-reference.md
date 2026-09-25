# groundtrack: Scripts Reference

[← back to README](../README.md)

## What each script does

**`setup_station.py`** - one-time/occasional wizard for ground-station-level
settings (lat/lon/alt, TLE source, `rig_port`). Leaves your satellite list
untouched. Add a new satellite to the fleet with:
```
python3 plan_passes.py --add-satellite
```
(only adds the config entry - you still need to create its `flowgraphs/<name>.grc`).

**`plan_passes.py`** - predicts every pass for every configured satellite
over a lookahead window (`--hours`, default 24) using Skyfield, and writes
them to `schedule.yaml` with an `approved:` flag per pass:
```
python3 plan_passes.py --hours 24                # list + approve all by default
python3 plan_passes.py --hours 48 --interactive   # y/n prompt per pass
```
Or hand-edit `schedule.yaml` afterward, setting `approved: false` on
anything you don't want recorded. Nothing is ever recorded on a pass that
isn't in this approved list.

Note: if two satellites' approved passes overlap, only one can actually
record (single SDR, no pre-emption). `plan_passes.py` detects this
automatically after you approve/reject: with `--interactive` it prompts
you to actively choose which one to keep (or both, or neither); without
it, it prints a clear warning listing every conflict rather than staying
silent, and leaves the existing first-started-wins behavior in place.

**`run_passes.py`** - the executor. Reads `schedule.yaml`, and for each
approved pass: waits for wall-clock AOS, launches that satellite's
flowgraph, feeds it Doppler-corrected frequency and (if configured) points
the rotor, and stops the flowgraph at the scheduled LOS (or earlier, if
live elevation drops below `min_elev_deg` first - a safety net for TLE
drift since planning). Run with `--verbose` to print every update:
```
python3 run_passes.py --verbose
```
Refuses to start a second instance against the same folder (a
`run_passes.lock` file, checked against the running PID - a stale lock
left over from a crash is detected and cleared automatically). Set
`notify: true` in `satellites.yaml` for desktop notifications at AOS/LOS
via `notify-send` (silently does nothing if `notify-send` isn't available,
e.g. on a headless box).

**`relay.py`** - persistent process, independent of any pass, that lets
your downstream KISS decoder hold one stable connection across every
AOS/LOS cycle instead of reconnecting every pass. Enables TCP keepalive
(`SO_KEEPALIVE`) on every consumer connection so a genuinely dead link
gets detected and cleaned up rather than left as a zombie. See "The
relay" below.

**`doctor.py`** - one command to check the environment (right folder,
stray processes, occupied ports) plus everything `preflight.py` checks.
See "doctor.py" above.

**`preflight.py`** - static config checks (freq/port/decoder-file
correctness) and an optional `--live` mode that briefly launches each
flowgraph for real. See "Preflight checks" above.

**`test_downstream.py`** - pushes frames through `relay.py` to your
connected decoder(s) for every satellite, automatically using real
captured frames if any exist yet or synthetic ones if not. See "Sending
test frames" above.

**`show_queue.py`** - reads `schedule.yaml` and prints the approved pass
queue (satellite, AOS, LOS, duration, max elevation), independent of
whether `run_passes.py` is running. See "Checking the pass queue" above.

**`toggle_satellite.py`** - enable or disable a satellite without
deleting its config:
```
python3 toggle_satellite.py --list
python3 toggle_satellite.py --disable GEOSCAN-1
python3 toggle_satellite.py --enable SCIONX
```
A disabled satellite is skipped everywhere - `relay.py` won't bind its
ports, `run_passes.py`/`plan_passes.py` won't schedule or execute passes
for it, `preflight.py` reports it as skipped rather than checking it.
Its full entry stays in `satellites.yaml` untouched, so re-enabling it
later needs no reconfiguration at all. See "Adding a satellite" below.

**`update_tle.py`** - refreshes the TLE file from a base of Celestrak
groups (default: `cubesat` + `amateur`), with individual satellites
added on top by catalog number for anything not covered by those groups:
```
python3 update_tle.py
python3 update_tle.py --extra-catnr 69880
python3 update_tle.py --check-only    # report coverage/age, don't download
```
Validates the download before overwriting the real file, and reports
exactly which configured satellites are missing afterward rather than
failing silently later inside `plan_passes.py`.

**`vet_grc.py`** - vets a `.grc` for the specific bugs this project has
actually hit, all stemming from hand-editing in GRC (especially
copy/paste, which GRC handles by silently appending `_0`, `_0_0`, etc.
to avoid a name collision, without warning you it happened):
```
python3 vet_grc.py                          # every flowgraphs/*.grc
python3 vet_grc.py flowgraphs/asrtussdv.grc  # one specific file
python3 vet_grc.py --fix flowgraphs/asrtussdv.grc
```
Checks for: a block type that should only ever appear once existing
twice; a block carrying GRC's double-suffix copy/paste signature even
when only one instance survives; `gpredict_doppler` present anywhere
(always wrong for this toolkit, see below); `rig_freq_poller` missing
when live Doppler tracking is expected; `osmosdr_source` re-tuning real
hardware instead of holding a fixed frequency; `options.id` not matching
the filename; a leftover Qt waterfall. `--fix` only ever applies to the
first two - pure renames, nothing structural - and always shows a diff
before writing plus keeps a `.bak` of the original; everything else
still means opening GRC. See [Adding a satellite](adding-satellites.md) for why this stays this narrowly scoped.

**`tcp_bridge.py`** - `relay.py`'s mirror-image, for a satellite whose
flowgraph runs its own `TCP_SERVER` instead of connecting out as a
client (see [The tcp_bridge](architecture.md) in the architecture doc for the full picture):
```
python3 tcp_bridge.py --verbose
```
Same ownership rules as `relay.py` - standalone, never auto-started,
start once and leave running for a session. Skips disabled satellites.
Multiple satellites sharing one downstream app can share one
`bridge_port`; each still gets its own independent, self-retrying
upstream connection.

## doctor.py - one command to check the environment

Run this first, any time something feels off, or as a habit before a
session:
```
python3 doctor.py
```
It checks, in order: which folder you're actually running from (and flags
if it's inside Trash - a real issue we hit once), whether duplicate copies
of this fleet folder exist elsewhere on disk, which fleet-related
processes are currently running and from where, which of the fleet's
ports are free vs. already occupied (and by what), whether any compiled
flowgraph files have landed at the repo root instead of `flowgraphs/` -
`grcc` always writes its output to the current directory, ignoring the
`.grc`'s own folder, for both the main flowgraph and a separate companion
file per embedded Python block - and a live snapshot of what's actually
installed (Python, OS, GNU Radio, gr-satellites, pyyaml, skyfield,
Hamlib) - see [Environment](environment.md) for what's actually confirmed
working, since none of these are checked against a required minimum
here. It then runs `preflight.py`'s full config checks automatically -
so `doctor.py` is a strict superset of `preflight.py`; you can run
either, but `doctor.py` catches a wider class of problems (like an
orphaned process from a since-deleted folder silently holding a port,
which `preflight.py` alone has no way to see).

```
python3 doctor.py --fix
```
Actually removes/moves the stray compiled files it finds - deleting
disposable embedded-block companions, and either deleting a stray main
flowgraph `.py` (if the correct copy already exists in `flowgraphs/`) or
moving it into place (if it doesn't). Safe regardless: nothing else in
this toolkit ever reads these files from the repo root, so cleaning them
up can't break anything that was working. `--fix` is stripped out before
anything else is passed through to `preflight.py`, so it won't cause an
"unrecognized arguments" error there.

Anything else after `doctor.py` on the command line is passed straight
through to `preflight.py`, so `python3 doctor.py --live` works too.

For a quick glance instead of the full report - TLE age, whether
rigctld/rotctld/relay.py are up, and time until the next approved pass:
```
python3 doctor.py --status
```

