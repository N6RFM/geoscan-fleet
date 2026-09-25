# groundtrack

Unattended GNU Radio ground station automation for tracking multiple
satellites on a single shared SDR and rotor. Originally built for the
GEOSCAN cubesat constellation; now satellite-agnostic - any mix of
decode-and-relay satellites (built on
[gr-satellites](https://github.com/daniestevez/gr-satellites)) and
recording-only satellites (raw IQ capture, no decoder required) can
share the same schedule, the same hardware, and the same toolkit. Also
uses Hamlib and Skyfield.

Point it at your ground station's coordinates and SDR, and it predicts
upcoming passes for review/approval, then for each approved pass:
retunes for live Doppler shift via a Hamlib `rigctld`, points an antenna
rotor via `rotctld`, records an IQ file, and - for satellites configured
with a downstream decoder - forwards decoded KISS frames through a relay
that stays connected across every pass - no manual tuning, no
re-clicking "Engage" in Gpredict, no reconnecting your decoder every
AOS. Satellites with no decoder yet just record raw IQ for later
analysis; nothing else in the toolkit requires one.

**What it assumes you already have:** GNU Radio 3.10+ with an SDR
supported by `gr-osmosdr` (developed against an Airspy), `gr-satellites`
installed (only needed for satellites that decode - recording-only
satellites don't need it), Hamlib (`rigctld`/`rotctld`), Python 3 with
`skyfield` and `pyyaml`, and (optionally) a Hamlib-compatible antenna
rotor and its serial interface.

**What it doesn't do:** demodulation/decoding itself (that's
`gr-satellites`, for satellites that use it), or drive a rotor/radio
that Hamlib doesn't already support.

## Quick start

```
git clone git@github.com:n6rfm/groundtrack.git
cd groundtrack
cp satellites.example.yaml satellites.yaml
python3 doctor.py
```

See "One-time setup" below for the full walkthrough, and "Troubleshooting"
near the end if something doesn't come back clean.

## How the pieces fit together

Every satellite runs a GNU Radio flowgraph - that part's universal.
Everything after it is optional and varies per satellite; no single
downstream tool is required for all of them:

```
GNU Radio flowgraph
(one process per pass, launched fresh
 at AOS by run_passes.py, exits at LOS)
        |
        |-- decode-and-relay satellites (GEOSCAN-1..6) --------------------
        |     |
        |     v
        |   relay.py                        SatsDecoder
        |   (one long-running process   --> (stays open, one tab per
        |    per satellite, started         satellite, connected once -
        |    once per session - only        this is what GEOSCAN happens
        |    for satellites configured       to feed; a different relay-
        |    to use it)                       using satellite could feed
        |                                     a different decoder entirely,
        |                                     since relay.py is protocol-
        |                                     agnostic - see below)
        |
        |-- direct-connection satellites (ASRTU-1_SSDV, BY70-4) -----------
        |     |
        |     v
        |   extra_outputs: TCP/ZeroMQ straight to whatever app is
        |   listening (an SSDV image viewer, a telemetry upload agent) -
        |   relay.py never involved at all
        |
        |-- recording-only satellites (SCIONX) -----------------------------
              |
              v
            raw IQ to disk only - no relay, no live decoder, no
            extra_outputs, nothing downstream but a file
```

Both `relay.py` and any live downstream decoder are per-satellite
choices, not fixed parts of the architecture - and even among satellites
that *do* use `relay.py`, `SatsDecoder` specifically is just what
GEOSCAN's flowgraphs happen to feed today, not something the toolkit
requires. A different relay-using satellite could just as easily feed an
entirely different decoder application.

**The flowgraph** is generated per satellite from that satellite's
`.grc` file. For satellites with a decoder, it demodulates the live
signal and hands decoded frames to a few destinations at once - a
`.kss` file on disk, a console printout, and a live network connection.
For recording-only satellites, it just tunes, corrects for Doppler, and
writes raw IQ to disk - no decoder, no KISS output, no network
connection at all.

**A downstream decoder** sits at the end of that network connection, for
satellites configured to use one. For GEOSCAN, that's
[SatsDecoder](https://github.com/baskiton/SatsDecoder) - an existing,
general-purpose open-source project, not something written for this
project, and not a fixed part of this toolkit's architecture. It decodes
frames for a range of amateur/university cubesat protocols - GEOSCAN,
USP (Unified SPUTNIX), AX.25, and CSP (Cubesat Space Protocol) among
them - via YAML satellite definitions, and gives a persistent
per-satellite tab with a live history of decoded frames over a KISS TCP
link. See [its own supported-protocols list](https://github.com/baskiton/SatsDecoder)
for the full and current set, since it grows independently of this
project. It's what GEOSCAN's flowgraphs happen to feed today - not a
requirement `relay.py` or anything else in this toolkit imposes. Earlier
issues found in it during this project's development (a false-disconnect
bug, and a KISS-timestamp
`OverflowError`) have both since been fixed in upstream's `nightly`
branch - confirmed directly against commit `2112e3f` ("#7 catch
overflow error when parsing KISS-timestamp"), sitting on top of the
`d94ff8e` refactor that introduced it. Run from `nightly`, not `main`;
it's unclear as of this writing when or whether these land in `main`.

**A different satellite can feed a completely different downstream
consumer, on either side of `relay.py`, using an entirely different
protocol - `relay.py` doesn't know or care.** It has no KISS awareness
at all: it's a pure byte-forwarding TCP proxy, nothing more. Whatever
arrives on a satellite's producer port gets forwarded verbatim to its
consumer port, with no parsing, no framing logic, and no protocol
knowledge of any kind - proven directly by the byte-for-byte fidelity
test earlier in this project's development, which showed `relay.py`
forwards exactly what it receives regardless of whether those bytes are
correctly KISS-framed, unframed, or something else entirely. That's
precisely why the `kiss_encode_pdu` bug (below) was possible in the
first place: `relay.py` was never responsible for that framing, and
nothing about it would have complained either way. A satellite's `.grc`
could just as easily feed `relay.py` a custom telemetry protocol, raw
samples, or anything else TCP can carry, to a downstream consumer that
has nothing to do with KISS or SatsDecoder at all - the only real
requirement is that whatever sits on both ends of a given port pair
agrees with each other, the same way a network cable doesn't need to
understand the packets running through it.

**`relay.py`** sits between the flowgraph and its downstream consumer,
for satellites that use both, because they have very different
lifecycles. The flowgraph is short-lived - `run_passes.py` launches it
fresh at every AOS and it exits at LOS, every pass, every satellite,
independently. A decoder GUI like SatsDecoder, by contrast, is meant to
be left open with each satellite's tab connected once and left alone; it
doesn't expect the far end of that connection to disappear and reappear
every few minutes. Without something in between, either the downstream
side would need to detect and reconnect around every single pass
boundary itself, or the flowgraph would need to somehow wait for it to
be listening before it could start. `relay.py` decouples the two
entirely: it's a single long-running process per satellite, started once
at the beginning of a session, that the short-lived flowgraph connects
*out* to as a client at every AOS, and that the downstream side connects
*in* to once and leaves alone. Either side can restart independently - a
flowgraph crashing mid-pass, or a decoder tab getting disconnected -
without the other one needing to know or care. Its TCP keepalive and
per-consumer diagnostic logging (see "The relay" section below) exist
specifically to make that long-lived middle process itself trustworthy,
since if it ever silently died or lost track of a connection, both
sides would be depending on a link that no longer worked without either
one finding out.

**Recording-only satellites skip the last two pieces entirely.** A
satellite with no `producer_port`/`consumer_port` in `satellites.yaml`
has no relay involvement and no decoder requirement - `relay.py` skips
it, `preflight.py`'s relay-specific checks skip it, and its flowgraph
just records raw IQ for you to analyze or decode later, whenever a
decoder for it exists. This is a first-class, fully supported mode, not
a workaround - see "Adding a satellite" below.

**Using the relay is a real choice with a real tradeoff, not just "on
for satellites with a decoder, off for satellites without one."**
Skipping it entirely is simplest when there's genuinely nothing waiting
on the other end yet - one less process to run, one less thing that can
go wrong. But the moment something *is* meant to consume frames live,
during the pass rather than after it, going through `relay.py` buys you
real robustness that a flowgraph connecting straight to a decoder
doesn't have: the decoder can hold one stable connection across many
AOS/LOS cycles instead of reconnecting every pass, either side can
restart independently without the other needing to notice, and TCP
keepalive plus diagnostic logging catch a dead link rather than leaving
a silent zombie connection. Bypass the relay for a satellite that does
have a live downstream consumer, and you take on all of that yourself -
the decoder now needs to detect and reconnect around every pass boundary
on its own, and a flowgraph that dies mid-pass just leaves the decoder
hanging with no signal anything went wrong.

## Folder layout

```
groundtrack/
├── satellites.yaml       # ground station + per-satellite settings (edit this)
├── schedule.yaml          # generated by plan_passes.py - approved pass list
├── setup_station.py       # wizard: sets lat/lon/alt, TLE source, rig port
├── plan_passes.py         # predicts passes, lets you approve/reject them
├── run_passes.py          # executor: launches flowgraphs at AOS, feeds Doppler+rotor
├── relay.py                # persistent TCP relay, for satellites configured to use one
├── show_queue.py            # prints the approved pass queue from schedule.yaml
├── doctor.py                # one command: environment + config sanity check
├── preflight.py             # config checks + optional --live flowgraph launch
├── test_downstream.py       # pushes test frames through the relay to your decoder(s)
├── send_test_frames.py      # single-satellite version test_downstream.py builds on
├── locate_decoders.py       # finds & patches decoder .yml paths in your .grc files
├── add_satellite.py         # adds a new satellite's config entry (you build the .grc)
├── edit_satellite.py        # edits an existing satellite's fields (and extra_outputs)
├── toggle_satellite.py      # enable/disable a satellite without deleting its config
├── update_tle.py            # refreshes tle_file from a base of Celestrak groups + extras
├── ci_check.py              # portable checks - what CI runs on every push
├── regen_all.sh             # grcc every .grc, then run preflight.py
├── Makefile                 # make build / check / status / doctor
├── requirements.txt
├── .github/workflows/ci.yml # runs ci_check.py on every push/PR
├── flowgraphs/
│   ├── kiss_encode_pdu.py  (reference copy - GRC embeds this as text in each .grc)
│   ├── geoscan1.grc / .py  (decode-and-relay satellite; .py generated by you, see Setup)
│   ├── geoscan2.grc / .py
│   ├── geoscan3.grc / .py
│   ├── geoscan4.grc / .py
│   ├── geoscan5.grc / .py
│   ├── geoscan6.grc / .py
│   ├── scionx.grc / .py    (recording-only satellite - no decoder, no relay, see below)
│   └── asrtussdv.grc / .py (recording-only, with extra_outputs - two direct-connection
│                             consumers bypassing relay.py entirely, see below)
├── groundtrack_gui.py     # optional GUI over the CLI tools - see GUI.md
├── delete_satellite.py     # removes a satellite's config entry (files untouched)
├── vet_grc.py              # vets a .grc for known copy/paste and Doppler bugs
└── tle/
    └── amateur.txt         # generated by update_tle.py - gitignored, not committed
```

## GUI (optional)

`groundtrack_gui.py` gives you a table of every satellite plus buttons
for the actions above - add/edit/enable/disable/delete, run checks,
refresh TLEs, plan and execute passes. It's entirely optional: every
action is a real subprocess call to the exact same script and flags
documented in this README, so the CLI tools work identically whether or
not you ever open the GUI. See [GUI.md](GUI.md) for the full picture,
including why a few buttons open their own terminal window instead of
running inline, and how to manage `extra_outputs` (below) through the
Edit dialog rather than by hand-editing YAML.

```
python3 groundtrack_gui.py
```

## One-time setup

1. **Clone the repo and create your personal config:**
   ```
   git clone git@github.com:n6rfm/groundtrack.git
   cd groundtrack
   cp satellites.example.yaml satellites.yaml
   ```
   `satellites.yaml` is gitignored on purpose - it holds your ground
   station's coordinates and is yours alone; `satellites.example.yaml` is
   the version-controlled template everyone starts from.

2. **System packages** (Debian/Ubuntu):
   ```
   sudo apt install libhamlib-utils
   ```
   This provides `rigctld`/`rigctl` and `rotctld`/`rotctl`. Confirm with
   `rigctld --version`.

3. **Python packages** - these are NOT apt packages, install with pip:
   ```
   pip install skyfield pyyaml --break-system-packages
   ```
   (or use a venv if you'd rather not pass `--break-system-packages`).

4. **Generate the flowgraph scripts.** The `.grc` files in this repo were
   authored outside GNU Radio Companion, so the runnable `.py` files don't
   exist yet. On your machine, with GNU Radio and your SDR driver
   installed, open each `.grc` and generate it (or `./regen_all.sh` for
   all of them at once). Also check, per file:
   - decode-and-relay satellites: the decoder block
     (`satellites_satellite_decoder`) points `file:` at your real decoder
     definition, and `network_socket_pdu` goes through `kiss_encode_pdu`
     (see "Why every `.grc` needs a `kiss_encode_pdu` block" below)
   - the `AdvFileSink` block's `basedir` points somewhere sensible for IQ
     output, and `recordOnStart` is `True` if you want unattended
     automatic recording

5. **Run the station setup wizard** to fill in `satellites.yaml`'s
   ground-station section:
   ```
   python3 setup_station.py
   ```
   Asks for latitude/longitude/altitude, your TLE source, and the shared
   rig port. It can also fetch the TLE file for you at the end.

6. **Fetch a TLE file** if you skipped that in step 5:
   ```
   python3 update_tle.py
   ```
   Fetches a base of Celestrak's `cubesat` and `amateur` groups by
   default - between them, covering most satellites this kind of station
   is likely to track - merges them, and validates that every satellite
   currently in `satellites.yaml` is actually covered. If one isn't (too
   new, uncoordinated, or simply in a different group), add it
   individually by NORAD ID rather than needing a whole separate TLE
   source:
   ```
   python3 update_tle.py --extra-catnr 69880
   ```
   Refresh this daily (cron), and re-run `plan_passes.py` after each
   refresh - stale TLEs drift AOS/LOS times and Doppler accuracy.

## The full end-to-end test recipe (no real pass, no test files required)

Everything below works with a freshly-set-up fleet - none of it requires a
satellite to have ever actually been received yet:

```
python3 doctor.py              # environment sanity: right folder? stray processes? free ports?
python3 preflight.py --live    # config correctness + each flowgraph launches without crashing
python3 relay.py &             # (or: nohup python3 relay.py > relay.log 2>&1 &)
python3 test_downstream.py     # pushes frames through the relay to your decoder(s)
```

If all four of those come back clean, every layer of the system - config,
flowgraph startup, relay connectivity, and decoder connectivity - has been
verified, and the only thing left untested is the live RF/SDR path itself,
which only a real pass (or `preflight.py --live`'s brief hardware launch)
touches.

## doctor.py - one command to check the environment

Run this first, any time something feels off, or as a habit before a
session:
```
python3 doctor.py
```
It checks, in order: which folder you're actually running from (and flags
if it's inside Trash - a real issue we hit once), whether duplicate copies
of this fleet folder exist elsewhere on disk, which fleet-related
processes are currently running and from where, and which of the fleet's
ports are free vs. already occupied (and by what). It then runs
`preflight.py`'s full config checks automatically - so `doctor.py` is a
strict superset of `preflight.py`; you can run either, but `doctor.py`
catches a wider class of problems (like an orphaned process from a
since-deleted folder silently holding a port, which `preflight.py` alone
has no way to see).

Anything after `doctor.py` on the command line is passed straight through
to `preflight.py`, so `python3 doctor.py --live` works too.

For a quick glance instead of the full report - TLE age, whether
rigctld/rotctld/relay.py are up, and time until the next approved pass:
```
python3 doctor.py --status
```

## Sending test frames to your decoder (without waiting for a pass)

**`test_downstream.py` is the one to run — it works with zero setup, no test files required.** For every configured satellite it automatically checks whether a real captured `.kss` file exists yet; if so it replays real frames, and if not (the normal situation before a satellite's first real pass) it transparently falls back to synthetic frames instead. You never need to decide which mode to use or locate a file yourself:

```
python3 relay.py &
python3 test_downstream.py
```

That tests every satellite in `satellites.yaml` in one pass. To test just one:
```
python3 test_downstream.py --satellite GEOSCAN-1 --count 5
```

Point each satellite's decoder tab at its `consumer_port` (from `satellites.yaml`) before running this, and watch its history pane for new entries as frames arrive. Synthetic frames will very likely show up as CRC/parse errors in the decoder - that's expected, since the payload is garbage; what matters is that the frame *count* goes up at all, confirming the relay→decoder wire and KISS framing work correctly end to end.

Once a satellite has actually completed a real pass and has a non-empty `.kss` file on disk, `test_downstream.py` will automatically start using those real frames instead - no flag to remember, no path to look up.

**`send_test_frames.py` still exists underneath** for single-satellite, explicit control (e.g. forcing synthetic even if a real capture exists, or pointing at a specific file):
```
python3 send_test_frames.py --satellite GEOSCAN-2 --replay /home/YOUR_USERNAME/Desktop/GEOSCAN-2.kss --count 5
python3 send_test_frames.py --satellite GEOSCAN-2 --synthetic 5
```

## Preflight checks (run this before every session)

`preflight.py` catches exactly the class of bugs we've hit already - typos
in `satellites.yaml`, freq/nfreq mismatches against a `.grc`,
`network_socket_pdu` silently reverting to `TCP_SERVER`, missing decoder
files, uncompiled/stale `.py` files, port collisions, satellites missing
from the TLE file - all without waiting for a real pass to expose them.

```
python3 preflight.py
```

Run this after any edit to `satellites.yaml` or any `.grc`, and as a habit
before starting `run_passes.py` for a session. It exits non-zero if
anything failed, so it's also cron/CI-friendly if you want to wire it into
your daily TLE-refresh routine.

For a deeper check that actually launches each flowgraph briefly (using
real SDR hardware) to confirm it starts without crashing and correctly
connects out to the relay:
```
python3 preflight.py --live                    # tests every satellite, ~10s each
python3 preflight.py --live --only GEOSCAN-2    # just one
```
This stands in for `relay.py` temporarily (so it doesn't need to already
be running) and just checks the connection arrives and the process
doesn't crash - it doesn't require a real satellite to be overhead, since
it's testing the pipeline's wiring, not decoding a real signal.

## Daily workflow

```
# 1. refresh TLEs (cron this)
python3 update_tle.py

# 2. sanity-check everything before touching hardware/schedules
python3 doctor.py

# 3. plan: predict upcoming passes and approve/reject them
python3 plan_passes.py --hours 24 --interactive

# 4. start the persistent relay (leave running - only needs restarting if it dies;
#    only needed if at least one configured satellite uses one)
nohup python3 relay.py > relay.log 2>&1 &

# 5. start your antenna rotor daemon, pointed at your real hardware
rotctld -m 607 -r /dev/ttyUSB2 &

# 6. connect a decoder tab/instance for each decode-and-relay satellite,
#    at its own consumer_port, and leave them open - see "The relay"
#    below for why this matters and how to check it's actually done
#    correctly. Recording-only satellites need nothing here.

# 7. execute: waits for AOS, launches flowgraphs, drives Doppler + rotor
python3 run_passes.py --verbose
```

Steps 3-6 only need to be started once per session (they're long-running);
step 1-2 is the thing to repeat daily as TLEs update. Redirecting relay.py's
output to a log file (rather than `python3 relay.py &` directly) keeps your
terminal prompt clean instead of its startup messages interleaving with it -
check on it anytime with `tail -f relay.log`.

### Checking the pass queue

`run_passes.py` doesn't have a `--status` or `--list` flag; it just prints
the next pass as it runs. To see the full approved queue at any time -
whether or not `run_passes.py` is currently running:
```
python3 show_queue.py
```
This reads `schedule.yaml` directly and prints every `approved` pass,
sorted by AOS, with a status column (`past` / `ACTIVE` / `NEXT` /
`upcoming`), duration, and max elevation.

To also see unapproved/rejected passes (e.g. ones `plan_passes.py`'s
overlap resolution dropped):
```
python3 show_queue.py --all
```
`schedule.yaml`'s pass fields as of this writing: `name`, `norad`, `aos`,
`los`, `max_elevation_deg`, `approved`. If `plan_passes.py`'s schema ever
changes, update the key lists near the top of `show_queue.py` to match.

### Starting from an uncertain state

The sequence above assumes a clean slate. If you're not sure what's still
running from an earlier session - a previous `relay.py` you forgot about,
a stale `rotctld`, a leftover debugging process - don't guess and don't
just try to start everything again on top of it. Kill it all, verify it's
actually gone, then rebuild step by step with a checkpoint after each one.
This is slower than the Daily workflow above, on purpose - it's for
recovering from confusion, not for a normal day.

**0. Kill everything:**
```
pkill -f relay.py
pkill -f run_passes.py
pkill -f SatsDecoder
pkill -f "flowgraphs/"    # every satellite's compiled flowgraph, whatever they're named
pkill -f rotctld
pkill -f rigctld
```
"No process found" for any of these is fine - it just means that one
wasn't running.

**Verify it's actually clean before rebuilding anything:**
```
python3 doctor.py
```
Check two things specifically: "Fleet-related processes currently
running" should say `none found`, and every port doctor.py lists for
your currently-enabled satellites should say `free`. Don't move on
until both are true - if anything still shows up, that's a process the
kill list above didn't catch, and it needs its own `kill <PID>` before
continuing.

**1-2. Refresh TLEs, then sanity-check config:**
```
python3 update_tle.py
python3 doctor.py
```
Confirm `update_tle.py` reports every configured satellite covered, with
no `MISSING from TLE file` lines. Confirm `doctor.py` ends in `0 failed` -
if `.grc` files were touched since the last session (GNU Radio Companion
re-saving them counts), you may see a "`.py` is up to date with `.grc`"
failure here; fix with `./regen_all.sh` before continuing.

**3. Plan passes:**
```
python3 plan_passes.py --hours 24 --interactive
```
Before approving, you'll get a chance to actively resolve any overlaps
between different satellites - see the note under `plan_passes.py` below.
This also fully regenerates `schedule.yaml`, so any satellite you've
since disabled with `toggle_satellite.py` simply won't have entries in
it anymore - nothing to prune by hand.

**4. Start the relay** (skip this step entirely if every currently-enabled
satellite is recording-only), **then confirm it before moving on:**
```
nohup python3 relay.py > relay.log 2>&1 &
cat relay.log
```
If `cat` shows nothing, wait a second and run it again - backgrounding
sometimes returns your prompt before the startup banner prints. You're
looking for a `producer :.../consumer :... (TCP keepalive on)` line for
each satellite that uses the relay, and no `OSError: ... address already
in use` traceback. If you see that traceback, something from step 0
wasn't actually killed - go back to `doctor.py`, don't just retry the
same command.

**5. Start the rotor, then confirm it before moving on:**
```
rotctld -m 607 -r /dev/ttyUSB1   # match your actual device
python3 doctor.py
```
Check the antenna rotor port line specifically (`rot_port` in
`satellites.yaml`, default 4533) - it should show `IN USE` with your
`rotctld` PID, not `free`.

**6. Connect a decoder tab for each decode-and-relay satellite, then
confirm from the relay's side:**
Point each tab at `127.0.0.1` and that satellite's own `consumer_port`,
then check:
```
tail -20 relay.log
```
You want one `consumer connected` line per satellite that uses the
relay - not fewer, and not the same satellite twice while another is
missing.

**7. Execute:**
```
python3 run_passes.py --verbose
```
On startup it should print the number of approved passes loaded and
identify the next one by name and AOS time. If a pass should be starting
soon and nothing happens, that means it's still counting down - it won't
launch a flowgraph until wall-clock AOS actually arrives.

**`add_satellite.py`** - adds a new satellite's config entry to
`satellites.yaml`:
```
python3 add_satellite.py --name GEOSCAN-3 --norad 64893 --freq 435742000
```
For a satellite with no decoder yet, or one whose outputs will connect
directly via `extra_outputs` rather than through the relay:
```
python3 add_satellite.py --name SCIONX --norad 69880 --freq 437500000 --record-only
```
Auto-assigns the next free `producer_port`/`consumer_port` (unless
`--record-only`, or overridden with `--producer-port`/`--consumer-port`),
refuses a NORAD or port collision with another configured satellite, and
tells you exactly what's still needed afterward. **Only ever touches
`satellites.yaml` - never generates or modifies a `.grc`.** Building
`flowgraphs/<name>.grc` is a manual step in GRC every time, same as
`edit_satellite.py` below and every other `.grc`-shaped thing in this
toolkit - see "Why nothing here touches `.grc` files" under "Adding a
satellite" for why. `plan_passes.py --add-satellite` does roughly the
same job through an interactive prompt instead of flags, though without
`add_satellite.py`'s port-collision and duplicate-NORAD checks.

**`edit_satellite.py`** - updates an already-configured satellite's
fields, and its `extra_outputs` list, without touching anything you
don't explicitly pass:
```
python3 edit_satellite.py GEOSCAN-1 --freq 435970000
python3 edit_satellite.py GEOSCAN-1 --enabled
python3 edit_satellite.py ASRTU-1_SSDV --extra-output-name ssdv_viewer \
    --extra-output-protocol tcp_server \
    --extra-output-block network_socket_pdu_0 --extra-output-port 9985
```
Only ever touches `satellites.yaml` - refuses a NORAD or port collision
with another satellite, and replaces (rather than duplicates) an
`extra_outputs` entry when you re-add one with the same name. If a
change here needs the `.grc` updated to match (a new frequency, say),
that's a separate manual step in GRC; the script says so when it applies.
See "Adding a satellite" below for the full `extra_outputs` picture.

**`ci_check.py`** - the portable subset of `preflight.py`'s checks that
can run with no GNU Radio, no Hamlib, and no real TLE file - what runs in
CI on every push. Safe to run locally too, any time:
```
python3 ci_check.py
```

**`regen_all.sh`** / **`Makefile`** - `./regen_all.sh` (or `make build`)
regenerates every `flowgraphs/*.grc` into its `.py` via `grcc`, then runs
`preflight.py` - one command instead of remembering to `grcc` each file
individually. Other targets: `make check`, `make check-live`,
`make status`, `make doctor`.

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
still means opening GRC. See "Why nothing here touches `.grc` files"
below for why this stays this narrowly scoped.

## How Doppler control works

- `run_passes.py` starts **one** `rigctld -m 1 -t <rig_port>` (Hamlib's
  Dummy rig backend - just a frequency register, no real hardware behind
  it) when it launches, and keeps it running for the whole session,
  independent of which satellite is active.
- `run_passes.py` is the only thing that ever *sets* frequency on it
  (`F <hz>`, computed live from the TLE via Skyfield).
- Whichever satellite's flowgraph is currently running *polls* that same
  rigctld for the current frequency (`f`) via an embedded Python block
  (`rig_freq_poller_0`) and feeds it into a GRC variable (`freq`) via a
  `Message Pair to Variable` block.
- Because there's exactly one rigctld for the whole fleet and only one
  flowgraph is ever alive at a time (single SDR), there's no per-satellite
  port to manage - every `.grc` points at the same `127.0.0.1:<rig_port>`.
- This is genuine Hamlib, not a custom protocol, so a real copy of
  Gpredict's radio control window can point at the same address (as a NET
  rigctl rig) purely for a visual read-out if you want one.

**What actually happens to that `freq` variable once it updates** - this
is the part worth understanding fully, since it's easy to assume Doppler
correction means re-tuning the SDR hardware itself, and that's not what
happens here. Look at `osmosdr_source_0`'s own tuned frequency in any
working `.grc` (`geoscan1.grc` is the reference): its value is a fixed
formula, `nfreq - offset` - built only from `nfreq` (the satellite's
*nominal*, unchanging frequency) and `offset` (a fixed DC-avoidance
offset, `50e3` Hz by convention). **The SDR hardware is tuned exactly
once, at flowgraph startup, and never retuned for the rest of the
pass** - deliberately, since continuously re-tuning real hardware
mid-pass risks PLL relock glitches and settling delays at exactly the
moment you can least afford to lose samples. All the actual Doppler
tracking happens one stage later, in the Signal Source block
(`analog_sig_source_x_0_0`) feeding a `Multiply` block: its frequency is
`-(freq-nfreq+offset)+BFO` - it *does* reference the live `freq`
variable, and continuously recalculates the mixing frequency to shift
the signal by exactly the live Doppler offset, entirely in software,
after the fixed-frequency hardware has already captured it. As long as
the shifted signal stays inside the low-pass filter's passband (25 kHz
by convention here, comfortably wider than the few-kHz shift a typical
LEO pass produces at these frequencies), this works correctly and avoids
ever touching the hardware mid-pass at all.

**The one real pitfall, confirmed the hard way on a real satellite:**
`rig_freq_poller` is not the only block that can feed a `Message Pair to
Variable` block, and the other common one looks superficially similar
but does something completely different. `gpredict_doppler` is part of
the same OOT module and produces the same kind of message output - but
it's a *passive listener*, waiting for an actual instance of the
Gpredict application to connect to it and push frequency updates over
Gpredict's own native rig-control protocol. This toolkit never plays
that role - `run_passes.py` only ever pushes frequency *into* `rigctld`
as a Hamlib client; it never connects *out* as a Gpredict client to
anything. A flowgraph wired with `gpredict_doppler` instead of
`rig_freq_poller` will run with no errors, tune correctly at pass start,
and then silently receive zero Doppler correction for the entire pass -
the console just shows `[doppler] Waiting for connection on:
127.0.0.1:<port>` for the whole thing, since nothing ever will connect.
**Always use `rig_freq_poller`, matching `geoscan1.grc`'s wiring exactly**
- if a satellite's `.grc` was built by copying an unrelated gr-gpredict
example rather than an existing satellite in this fleet, check this
specifically before trusting a pass's Doppler correction.

## Antenna control

`run_passes.py` optionally drives a rotor through `rotctld`, the same
pattern as Doppler - one persistent client connection fed by whichever
satellite is active. Unlike `rigctld`, you start `rotctld` yourself, since
it needs your actual serial port and rotor model:
```
rotctld -m 607 -r /dev/ttyUSB2
```
(no `-t` needed - `rotctld` defaults to port 4533, matching `rot_port` in
`satellites.yaml`.) Start it before or any time up to the first pass - the
client reconnects automatically once it's up.

To run with no rotor at all, remove `rot_host`/`rot_port` from
`satellites.yaml`; `run_passes.py` prints a note and skips antenna control.

Az/el updates are only sent when position has moved more than 1 degree
since the last command, to avoid flooding the rotor - adjust
`min_move_deg` in `Rotctld.point()` inside `run_passes.py` if your rotor
wants finer or coarser steps.

## Why every `.grc` needs a `kiss_encode_pdu` block

**Every flowgraph's `network_socket_pdu` block must go through the
`kiss_encode_pdu` embedded Python block before reaching the socket, not
be wired directly to the satellite decoder's `out` port.** Without it,
frames arrive at SatsDecoder corrupted or missing entirely, even though
the same pass looks completely normal in the console and in the `.kss`
file on disk.

**Root cause:** `satellites_satellite_decoder`'s PDU output is raw,
unframed payload bytes - nothing more. `satellites_kiss_file_sink`
applies real KISS framing (`0xC0` FEND delimiters, with `0xDB`/`0xDC`/
`0xDD` escaping) internally when it writes those bytes to a `.kss` file,
which is why every `.kss` file this station has ever produced parses
correctly. `network_socket_pdu`, however, is a generic core GNU Radio
block with no knowledge of KISS at all - it just writes whatever PDU
bytes it's handed straight to the TCP socket, unframed. So the exact
same PDU stream gets framed on the file path and left completely
unframed on the live network path, from the same decoder, at the same
instant.

**Why this was so hard to catch:** SatsDecoder's `kiss_read_stream()`
looks for a `0xC0` byte to find a frame boundary. If a byte chunk has no
`0xC0` in it at all, that function's own logic silently discards it as
"garbage" - no error, no warning, frame count just doesn't move. When
frames arrive spaced far enough apart in time, each one *sometimes*
happens to land in its own TCP read anyway and gets discarded quietly,
which is easy to miss. But under a fast burst (e.g. mid-image
transmission, dozens of frames in a couple of seconds), the OS coalesces
several unframed PDUs into one `recv()`, and SatsDecoder's parser can
mis-lock onto some byte pattern inside that blob and misreport it as one
giant, garbage frame - which is what actually happened during the
2026-09-20 19:27 GEOSCAN-2 pass: a single "frame" reported as
`INNOSAT16`, `1144` bytes long, when it should have been dozens of
separate 72-byte GEOSCAN frames. Confirmed directly: repeating the exact
same session prefix appeared every **72 bytes**, back to back, with zero
`0xC0` delimiters anywhere between them, for as long as the burst
lasted - proof the frames were real and correctly decoded, just never
framed for the network.

**The fix:** `kiss_encode_pdu` (kept in this repo at
`flowgraphs/kiss_encode_pdu.py` for reference and diffing, since GRC
embeds its actual code as text inside the `.grc` file itself) wraps each
PDU exactly the way `kiss_file_sink` already does - FEND byte, KISS
command byte, escaped payload, FEND byte - before it reaches
`network_socket_pdu`. It changes nothing else: the decoder's connections
to Print Timestamp, KISS File Sink, and Telemetry Submit are untouched;
only the network path gets the extra hop.

```
satellite_decoder (out)  --------------------------->  kiss_file_sink       (already framed internally)
                          --------------------------->  print_timestamp / telemetry_submit
                          ---> kiss_encode_pdu (in/out) ---> network_socket_pdu (pdus)   <- the fix
```

**Verifying it's actually working**, without needing a live pass or any
hardware: wire `KISS File Source` (reading any real `.kss` file this
station has already captured) through `kiss_encode_pdu` into
`network_socket_pdu`, run it, and connected SatsDecoder tabs should show
individual, correctly-sized frames appearing one at a time. Removing
`kiss_encode_pdu` from that same test rig reliably reproduces the exact
original failure - one giant misparsed frame, then nothing further -
confirming the block is what fixes it rather than something else
changing at the same time.

**Every decode-and-relay satellite's `.grc` needs this** - all of them
share the same `satellite_decoder` -> `network_socket_pdu` pattern, so
any of them can have this gap. Add `kiss_encode_pdu` to each, then
`./regen_all.sh` to recompile all of them at once. Recording-only
satellites don't have `network_socket_pdu` at all, so this doesn't apply
to them.

## The relay (for your downstream KISS decoder)

**Ownership: `relay.py` is a fully separate, standalone process. Nothing
else in this toolkit starts it for you, ever.** Not `run_passes.py`, not
`preflight.py`, not `test_downstream.py`. You start it once yourself at
the beginning of a session and leave it running - it's designed to
outlive every individual pass, which is the entire reason it exists (your
downstream decoder holds one stable connection across many AOS/LOS
cycles instead of reconnecting every time). If you already started it
earlier in your terminal session and never stopped it, it's still running
right now and every script below will use that same instance - you do not
need to restart it before every test.

| Process | Started by | Lifetime |
|---|---|---|
| `rigctld` | `run_passes.py`, automatically | same as `run_passes.py` |
| `rotctld` | you, manually | independent - you start/stop it |
| `relay.py` | you, manually | independent - persists across every pass |
| each `geoscanN.py` flowgraph | `run_passes.py`, one at a time | only alive during that satellite's AOS-to-LOS |

`preflight.py --live` and `test_downstream.py` both *use* `relay.py` if
it's already running (see Troubleshooting below for what happens if it
isn't) - neither one launches it.

Each `.grc` connects *out* to `relay.py` (`network_socket_pdu`, TCP_CLIENT)
instead of opening its own listening KISS port. `relay.py` opens two ports
per satellite (see `producer_port`/`consumer_port` in `satellites.yaml`):

- `producer_port` (9101-9104) - internal; only the currently-running
  flowgraph connects here. Nothing else should touch these.
- `consumer_port` (8101-8104) - point your decoder at these. This side
  never drops, even across LOS - only the producer side comes and goes as
  passes start and stop.

**Your downstream decoder needs one persistent connection PER SATELLITE
that uses the relay, all open at the same time - not one connection you
re-point between passes.** The relay serves every relay-using satellite's
ports simultaneously and continuously (it opens all of them - one
producer, one consumer, per satellite - in the same event loop at
startup, with no sequencing or switching between them at all). A single
decoder connection can only ever be on one port, so it will only ever
see one satellite's frames, no matter which satellite is actually
overhead at the time.

Concretely, in a tabbed decoder app like `SatsDecoder-linux`: open one
**tab per relay-using satellite**, each pointed at its own fixed
`consumer_port` (from `satellites.yaml`), and leave them all connected
indefinitely:

```
Tab "geoscan-1"  ->  127.0.0.1 : 8101   (connect once, leave open forever)
Tab "geoscan-2"  ->  127.0.0.1 : 8102   (connect once, leave open forever)
```

If your decoder app can't hold multiple independent connections in one
instance, run it as multiple separate OS processes instead, each with a
different Port set in its own window:
```
/path/to/decoder-binary &
/path/to/decoder-binary &
```

Once every tab is connected, nothing needs to be touched again between
passes - whichever satellite is actually overhead automatically lights
up its own tab/instance; the others just sit idle until it's their turn.
Verify they're actually connected by checking `relay.log` for one
`consumer connected` line per relay-using satellite that persists rather
than connect-then-disconnect.

Start `relay.py` and `run_passes.py` in either order - the flowgraph's
socket client retries until the relay is listening, and the relay's
consumer side accepts connections immediately even with no satellite up
(it'll just be silent until a pass starts).

To find which port a given satellite uses, either check `satellites.yaml`
directly, or just read `relay.py`'s own startup output:
```
[GEOSCAN-1] producer :9101  consumer :8101
[GEOSCAN-2] producer :9102  consumer :8102
```
(only satellites configured to use the relay show up here - recording-
only satellites are silently skipped, with a line saying so). Any port
number works as long as it's free and both ends (this config, and your
decoder's connection settings) agree on it - 8101/8102/... is just a
convenient, memorable scheme, not a requirement.

## Adding a satellite

**Why nothing here touches `.grc` files.** Earlier versions of
`add_satellite.py` generated a new satellite's `.grc` automatically from
an existing one as a template. That repeatedly proved fragile in ways
GRC's own editor doesn't have - across three different satellites,
real bugs surfaced: a stray internal `id` field left mismatched with the
filename, `--record-only` leaving decoder/relay blocks present-but-
unconfigured instead of actually removing them, and an orphaned
`kiss_encode_pdu` block left disconnected on the canvas because it's an
embedded Python block sharing a generic type identifier with every other
embedded block. Each was fixable, but the pattern itself - a script
trying to safely manipulate GNU Radio's nested block-graph structure -
kept finding new ways to fail quietly. `satellites.yaml` is a simple flat
config format; a `.grc` is a complex graph GRC itself already knows how
to edit correctly. So the scope boundary is now firm: **every script
here only ever reads or writes `satellites.yaml`. Building or editing a
`.grc` - a brand new satellite, or a decoder/relay/`extra_outputs` block
inside one that already exists - is always a manual step in GRC.**
Copying an existing satellite's `.grc` as a starting point and adapting
it is a perfectly reasonable way to do that.

**`vet_grc.py`** is the one narrow, deliberate exception worth being
precise about, since it's easy to mistake for a reversal of the rule
above rather than a careful exception to it. Read-only inspection was
never in question - checking a `.grc`'s content is exactly what
`preflight.py` already does, and `vet_grc.py` extends that with checks
for the specific bugs this project has actually hit: GRC's own
copy/paste-collision renaming (a block ending up named
`network_socket_pdu_0_0` instead of `network_socket_pdu_0`), and the
`gpredict_doppler`-instead-of-`rig_freq_poller` mistake described below.
Its `--fix` flag *does* write to a `.grc`, but only for the two cases
that are a pure rename with no structural change at all - a block's own
name, or the flowgraph's `options.id` - never anything requiring a
decision about wiring. It refuses outright if a rename would collide
with a block that already exists, since that's a genuine duplicate
needing a human decision, not a misnamed single instance. Every fix
shows a diff before writing and keeps a `.bak` of the original. Missing
`rig_freq_poller`, a leftover waterfall, an incorrect `osmosdr_source`
tuning formula - none of that is something `--fix` will ever attempt;
those still mean opening GRC.

**Decode-and-relay** (has a `gr-satellites` decoder definition, feeds a
downstream decoder GUI over KISS):
```
python3 add_satellite.py --name GEOSCAN-3 --norad 64893 --freq 435742000
```

**Recording-only** (no decoder yet, or intentionally none - just raw IQ
to disk for later analysis):
```
python3 add_satellite.py --name SCIONX --norad 69880 --freq 437500000 --record-only
```
`--record-only` just means the config entry gets no
`producer_port`/`consumer_port` - that absence is what tells `relay.py`
and `preflight.py` this satellite has no relay involvement. The `.grc`
itself - no decoder, no KISS sink, no network block, `recordOnStart:
True`, no waterfall - is still yours to build in GRC; `scionx.grc` is a
working example to copy from.

**Direct-connection outputs** (`extra_outputs`) - for a satellite with a
second live output that a specific downstream app connects to directly,
completely bypassing `relay.py`. This is a different situation from the
relay entirely: `relay.py` is a plain TCP byte-forwarder with no
protocol awareness of its own - it exists to decouple a short-lived
flowgraph from a long-lived KISS decoder connection, a real problem for
raw TCP. A protocol like ZeroMQ PUB/SUB already solves that same
decouple-producer-from-consumer problem natively (a SUB socket can
connect, disconnect, and reconnect independently at any time, no relay
needed), so routing it through `relay.py` would be solving a problem
that protocol doesn't actually have. `asrtussdv.grc` is the working
example: its `network_socket_pdu` block (`TCP_SERVER`, listening
directly for a dedicated SSDV image viewer to connect in) and its
`zeromq_pub_msg_sink` block (publishing telemetry for a separate upload
agent to subscribe to) both bypass the relay this way.
```yaml
  - name: ASRTU-1_SSDV
    norad: 61781
    freq_hz: 436210000
    script: flowgraphs/asrtussdv.py
    min_elev_deg: 15
    extra_outputs:
      - name: ssdv_viewer
        protocol: tcp_server
        block: network_socket_pdu_0
        port: 9985
      - name: telemetry_upload_agent
        protocol: zeromq_pub
        block: zeromq_pub_msg_sink_0
        address: "tcp://127.0.0.1:5556"
```
`preflight.py` validates each entry against the real `.grc` the same way
it validates `producer_port` - confirming the named block exists, and
that its port (for `tcp_server`/`tcp_client`) or address (for
`zeromq_pub`) actually matches. `add_satellite.py` doesn't create these
for you - use `edit_satellite.py` (below) or the GUI's Edit dialog,
which manage this list properly in `satellites.yaml`, including copying
an existing satellite's outputs as a starting point when a new satellite
shares the same downstream app (as ASRTU-1_SSDV and BY70-4 both do).
Either way, the `.grc`'s actual block - its name, port, or address -
still has to be built or edited separately in GRC to match; nothing here
touches `.grc` content.

Either way, finish with:
```
grcc flowgraphs/<name>.grc      # or ./regen_all.sh for everything at once
python3 update_tle.py           # auto-covers every configured satellite, no flags needed
python3 preflight.py
```

**Editing a satellite** already in `satellites.yaml` - NORAD, frequency,
min elevation, relay ports, enabled state, or its `extra_outputs` list:
```
python3 edit_satellite.py GEOSCAN-1 --freq 435970000
python3 edit_satellite.py GEOSCAN-1 --producer-port 9110 --consumer-port 8110

python3 edit_satellite.py ASRTU-1_SSDV --extra-output-name ssdv_viewer \
    --extra-output-protocol tcp_server \
    --extra-output-block network_socket_pdu_0 --extra-output-port 9985
python3 edit_satellite.py ASRTU-1_SSDV --remove-extra-output ssdv_viewer
```
Only touches the fields you actually pass, and only ever `satellites.yaml`
- never the `.grc`. If a change here (a new frequency, a corrected
NORAD) needs the `.grc` to match, that's still your own separate edit in
GRC; the script prints a note when that applies. Refuses a NORAD or port
collision with another configured satellite rather than silently
creating one. Adding an `extra_outputs` entry with a name that already
exists on that satellite replaces it rather than duplicating it, so
re-running the same command with a corrected value is safe.

**Pausing a satellite** without deleting its hard-won config - sharing
one SDR across satellites you don't all want active at once is the
normal case, not an edge case:
```
python3 toggle_satellite.py --list
python3 toggle_satellite.py --disable GEOSCAN-1
python3 toggle_satellite.py --enable GEOSCAN-1
```
A disabled satellite is invisible to `relay.py`, `run_passes.py`,
`plan_passes.py`, and `preflight.py`'s live checks - as if it weren't in
`satellites.yaml` at all - while its full config sits untouched, ready
to re-enable with no reconfiguration. Re-run `plan_passes.py` after
toggling anything, since it fully regenerates `schedule.yaml` from
whatever's currently enabled - a disabled satellite's old approved
passes simply won't be in the new file, nothing to prune by hand.

## Satellite data

Frequencies, ports, and every other per-satellite setting live in
`satellites.yaml` - that file is the single source of truth, not this
README. Pull current downlink frequencies from db.satnogs.org before a
real pass regardless of what's already configured; drift of several kHz
between checks isn't unusual.

For a satellite with no decoder yet, no `producer_port`/`consumer_port`
is needed at all - see "Adding a satellite" below.

## Verifying Doppler and rotor control are working

**From inside the script:** `python3 run_passes.py --verbose` prints a
line every second while a pass is active:
```
[GEOSCAN-2] el= 31.5 az=214.3  freq=436,158,412 Hz (doppler -1,588 Hz)
```
Nothing printing while a satellite should be up? Check `schedule.yaml` -
the pass may not be approved, or its window may not actually cover "now"
(stale schedule - rerun `plan_passes.py`).

**From outside the script**, independent of `run_passes.py` - useful to
confirm rigctld/rotctld are alive even with no pass active:
```
printf 'f\n' | nc 127.0.0.1 4532      # Doppler - current commanded freq
printf 'p\n' | nc 127.0.0.1 4533      # Rotor - current commanded az/el
```
or, with Hamlib's own clients:
```
rigctl -m 2 -r 127.0.0.1:4532 f
rotctl -m 2 -r 127.0.0.1:4533 p
```
Query twice a few seconds apart *during an active pass* - the numbers
should change each time; outside a pass they'll sit at whatever idle
value the daemon started with (rigctld's Dummy backend defaults to
145000000 Hz), which is expected, not broken.

Be careful testing the rotor manually with a real `P <az> <el>` command -
unlike rigctld's Dummy backend, `rotctld` is driving real hardware and
will actually move the antenna.

"Connection refused" on either port means that daemon isn't running or
isn't listening where `satellites.yaml` says - check with
`ps aux | grep rigctld` / `ps aux | grep rotctld`.

**No pass due for hours and want to test sooner?** Temporarily lower one
satellite's `min_elev_deg` in `satellites.yaml` (e.g. to `0`), rerun
`plan_passes.py --hours 2`, and you'll likely get a near-term low pass to
watch trigger end-to-end. Remember to put the threshold back afterward.

## Troubleshooting

Symptoms we've actually hit, in the order worth checking:

**`Connection refused` from `test_downstream.py` or when connecting your
decoder to a consumer port**
`relay.py` isn't running (or died). Check with `ps aux | grep relay.py`,
or just run `python3 doctor.py` - it lists exactly what's holding each
fleet port, and if a port shows `free` when it should show `relay.py`,
that's your answer. Start it: `nohup python3 relay.py > relay.log 2>&1 &`

**Real passes happen with signal/frames visible in the console, but
nothing shows up in your downstream decoder**
Almost certainly: only one satellite's consumer port has a decoder
connected to it, and a different satellite's pass just happened. The
relay serves every relay-using satellite's consumer port simultaneously
and continuously - it never switches which port is "active." Your
decoder needs **one persistent connection per relay-using satellite, all
open at once** - not one connection you re-point before each pass. See
"The relay" above for the exact setup. Verify with `tail -f relay.log`:
you should see one `consumer connected` line per relay-using satellite
that persists,
not one connection that comes and goes.

**Frames arrive but your decoder can't parse them / shows garbage**
First check you're not just looking at *synthetic* test frames
(`test_downstream.py` sends these automatically when no real capture
exists yet - they're garbage payload by design, not a bug). If you
expected real captured data, double check your decoder's **Port** field
matches the satellite you think you're testing - `satellites.yaml` maps
each satellite to a specific `consumer_port` (8101=GEOSCAN-1,
8102=GEOSCAN-2, etc.), and pointing a "GEOSCAN-2" decoder tab at port 8101
will silently show you GEOSCAN-1's traffic instead, with no error at all.
This exact mixup happened once already in this setup - the tab was
labeled correctly but the port field wasn't updated to match.

**A pass looks completely normal in the console and in the `.kss` file,
but SatsDecoder shows one giant garbage frame (e.g. misparsed as
`INNOSAT16`, hundreds of bytes long) and then nothing further for the
rest of the pass**
This is the missing-KISS-framing bug, not a relay or SatsDecoder
problem - see "Why every `.grc` needs a `kiss_encode_pdu` block" above
for the full explanation. Confirm by checking whether that satellite's
`.grc` has `kiss_encode_pdu` between the decoder and `network_socket_pdu`;
if it's wired directly, that's the cause. Fix: add the block, `grcc` (or
`./regen_all.sh` for everything), and re-verify with the KISS File
Source -> `kiss_encode_pdu` -> Socket PDU test rig described in that
section before trusting the next live pass.

**`SatsDecoder` shows "Connection lost" mid-pass, decoder tab drops with
signal still visibly active**
This was a real bug in `SatsDecoder` itself, not this toolkit -
`kiss_read_stream()` returned the same falsy value (`b''`) for both "no
data yet" and "connection actually closed," so a perfectly ordinary empty
KISS frame could get misread as a dropped connection partway through a
pass. Fixed upstream in commit `d94ff8e` ("KISS reader refact"), first
available in the `nightly` tag - update with:
```
cd ~/SatsDecoder
git fetch --tags
git checkout nightly
```
(if you have a local `nightly` tag already cached from before this fix
landed, `git fetch --tags` won't move it - `git tag -d nightly` first,
then re-fetch, or you'll silently stay on the old code.)
`relay.py`'s TCP keepalive is a separate, complementary safeguard against
a genuinely stalled connection - it does not fix this specific bug, so
update `SatsDecoder` rather than relying on keepalive alone.

**Terminal looks "stuck" after backgrounding `relay.py`**
It isn't - `python3 relay.py &` sometimes prints its startup lines a
moment *after* bash already returned your prompt, which looks like a
hang but isn't one (press Enter and the prompt reappears clean). Avoid
the confusion entirely by redirecting its output instead of leaving it
attached to your terminal: `nohup python3 relay.py > relay.log 2>&1 &`,
then `tail -f relay.log` whenever you want to check on it.

**A port that should be free shows `IN USE` and you don't know why**
Run `python3 doctor.py` - it identifies the exact PID and command holding
every fleet port. This has caught real orphaned processes before (e.g. a
`rigctld` left running from a folder that had since been moved to Trash,
still holding port 4532 indefinitely). Kill the specific PID it reports,
or `pkill -f <name>` for a broader cleanup.

**Config edits don't seem to take effect**
`.grc` files are source; `run_passes.py` and `preflight.py` execute the
compiled `.py` next to them. Any edit to a `.grc` needs
`grcc flowgraphs/geoscanN.grc` before it does anything. `preflight.py`
flags this automatically ("`.py` is up to date with `.grc`").

**`network_socket_pdu` reverts to `TCP_SERVER` after editing/regenerating
a `.grc` in GNU Radio Companion**
This has happened more than once - something about re-adding or resolving
that block in GRC resets it to its default. There's no permanent fix on
our side for GRC's behavior, so make checking a habit:
`python3 preflight.py` explicitly checks this on every satellite and will
fail loudly if it's reverted, rather than you discovering it mid-pass.

**Multiple copies of the `fleet` folder causing confusion (Trash, Downloads,
Desktop all having their own)**
`python3 doctor.py`'s second section lists every folder under your home
directory containing both `satellites.yaml` and `run_passes.py`, with each
one's last-modified time, so you can see at a glance which is real and
which are stale duplicates worth deleting.

**`TypeError` or YAML parse errors when running `run_passes.py` or
`plan_passes.py`**
Almost always a stray value in `satellites.yaml` - a quoted number
(`freq_hz: "436160000"`), a typo (`freq_hz: 436160=3000`), or similar.
`preflight.py` validates every field's type explicitly and will name the
exact satellite and field at fault rather than you hunting through a
traceback.

**Not sure if something is a real problem at all**
Start with `python3 doctor.py` every time - it's a strict superset of
`preflight.py` and was specifically built to catch the "environment"
class of problems (wrong folder, stale process, occupied port) that
config-only checks can't see.

## Known caveats

- **Single SDR**: only one flowgraph runs at a time (highest elevation
  wins among approved, in-window passes; a running satellite is not
  pre-empted). Extending to multiple SDRs means tracking more than one
  `(rig, active_proc)` pair in `run_passes.py`.
- **rigctld's Dummy backend response format**: the poller inside each
  `.grc` expects `f\n` to return a bare number - confirmed working, but
  worth re-checking if you ever change Hamlib versions.
- **Doppler sign/magnitude**: sanity-check `doppler_hz()` in
  `run_passes.py` against a known pass before trusting a real recording.
- **Rotor update throttling**: `Rotctld.point()` only sends a new
  position when az or el has moved `>= 5.0` degrees, or `>= 5.0` seconds
  have passed since the last command actually sent (whichever comes
  first) - tuned to cut down command spam during a fast overhead pass
  without ever going silent for long. Both are keyword defaults
  (`min_move_deg`, `min_interval_s`) on `point()` if your rotor's actual
  slew rate ever calls for different values.
- Hand-authored `.grc` blocks (`epy_block`s, `network_socket_pdu`) were
  written outside GNU Radio Companion - open each block's properties
  dialog once after import to let GRC regenerate anything it flags.
- **`SatsDecoder` version**: use the `nightly` tag or later (commit
  `d94ff8e`+). Anything at or before release `0.3.6` has the mid-pass
  false-disconnect bug described in Troubleshooting.
- **`kiss_encode_pdu` is required on every decode-and-relay satellite's
  `.grc`**: `satellites_satellite_decoder`'s PDU output is unframed;
  `network_socket_pdu` has no KISS awareness of its own. Without
  `kiss_encode_pdu` between them, frames reach SatsDecoder unframed and
  get silently dropped or misparsed. See "Why every `.grc` needs a
  `kiss_encode_pdu` block" above. Check this first on any newly-added
  decode-and-relay satellite's `.grc`, since it's easy to copy the
  decoder wiring but forget this one extra hop. Doesn't apply to
  recording-only satellites - they have no `network_socket_pdu` at all.

## Authors

- N6RFM
- Claude (Anthropic)

## License

MIT - see LICENSE.
