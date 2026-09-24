# Contributing

Thanks for taking a look at this. It's a fairly niche, hardware-dependent
project (GNU Radio + a real SDR + Hamlib + an actual antenna rotor), so
most contributions will fall into one of a few categories below.

## Before you open a PR

Run the checks that don't need real hardware:

```
python3 ci_check.py
```

This validates every `.grc`'s config against `satellites.example.yaml`
(frequency agreement, correct relay wiring, no stray `TCP_SERVER`
regressions) and checks that every script still compiles. It's the same
check that runs in CI on every push - if it fails locally, it'll fail
there too.

If you have the actual hardware (GNU Radio, an SDR, Hamlib) and can test
against it:
```
python3 doctor.py
python3 preflight.py --live
```

## Kinds of contributions

**Adding a satellite**: use `add_satellite.py` rather than hand-editing a
`.grc` - it keeps the generated flowgraph consistent with the others and
avoids the kind of manual-edit drift (frequency mismatches, the
`network_socket_pdu` type reverting) that's bitten this project before.
```
python3 add_satellite.py --name GEOSCAN-N --norad <norad> --freq <hz>
```
For a satellite with no decoder yet, add `--record-only` - it strips the
decoder/KISS/telemetry/relay wiring and adds the config entry with no
relay ports at all, rather than a decode-and-relay flowgraph you'd need
to manually gut afterward.

**Pausing a satellite**: `toggle_satellite.py --disable NAME` /
`--enable NAME` rather than commenting out or deleting its
`satellites.yaml` entry - keeps the config intact and re-enabling later
needs no reconfiguration.

**Fixing a bug**: if it's something `preflight.py`/`doctor.py`/`ci_check.py`
*could* have caught but didn't, consider adding a check for it rather than
just fixing the instance - that's how most of the current checks came to
exist.

**Supporting different hardware** (a different SDR, a different rotor
brand): as long as it's Hamlib-compatible for the rotor side and
`gr-osmosdr`-compatible for the SDR side, this should mostly already work
without code changes - if it doesn't, that's a bug worth reporting.

## Style

Plain, readable Python over cleverness. No new external dependencies
without a good reason - the goal is something a ham with a Saturday
afternoon can read end to end.

## Reporting a bug

Use the issue template. The most useful thing you can include is the
output of `python3 doctor.py` - it captures the environment/process/port
state that's caused most of the real issues in this project's history.
