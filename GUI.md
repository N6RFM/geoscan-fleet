# groundtrack GUI (rough prototype)

`groundtrack_gui.py` is a simple Tkinter GUI over the existing
command-line tools - a way to see the whole fleet at a glance and run
common actions with a click, without needing to remember every script's
exact flags.

**It is explicitly a rough prototype, not a replacement for the CLI
tools.** If it's ever abandoned, deleting `groundtrack_gui.py` and
`delete_satellite.py` undoes the entire thing - nothing else in the repo
depends on either file, and nothing about how the CLI tools work changes
whether or not this GUI exists alongside them.

## The one rule everything else follows

**The GUI never edits `satellites.yaml` (or any other config) directly,
and never reimplements a script's logic.** Every action is a real
subprocess call to the exact same script and flags you'd type at the
terminal yourself - one code path for "how do I add a satellite," not
two that can quietly drift apart from each other over time. The only
things the GUI does entirely on its own are read-only: parsing
`satellites.yaml` to build the table, and checking file existence/
modification times for the "Gaps found" column.

Concretely:

| Button | What it actually runs |
|---|---|
| Enable / Disable selected | `toggle_satellite.py --enable/--disable NAME` |
| Delete selected | `delete_satellite.py NAME --yes` |
| Regenerate .grc for selected | `grcc flowgraphs/<name>.grc` |
| Add satellite... | `add_satellite.py --name ... --norad ... --freq ...` (plus `--template`/`--record-only` if you choose recording-only) |
| Run preflight.py / doctor.py | exactly those, captured and shown in the output pane |
| Run update_tle.py | `update_tle.py` with `--extra-catnr <norad>` appended for every currently configured satellite, so nothing needs the base Celestrak groups to happen to cover it |
| Show schedule | `show_queue.py` |
| Plan passes (auto-approve) | `plan_passes.py --hours N` |

## Why some buttons open a new terminal window instead

Three actions never exit on their own, or need real keyboard input the
GUI has no way to provide:

- **`relay.py`** - a persistent process, meant to be started once and
  left running for an entire session
- **`run_passes.py`** - waits indefinitely for AOS and keeps running
  until you stop it
- **`plan_passes.py --interactive`** - prompts for a y/n answer per pass
  on stdin

Running any of these the same way as `preflight.py` (a blocking,
output-captured subprocess call) would freeze the entire GUI - Tkinter's
event loop can't do anything else while waiting on a process that never
finishes, and it has no way to type an answer into one that's waiting on
stdin. Instead, these three are launched as fully detached processes in
their own terminal window (`gnome-terminal`, `konsole`,
`xfce4-terminal`, `x-terminal-emulator`, or `xterm` - whichever is found
first), so you can watch live output and Ctrl-C them independently of
the GUI. If none of those terminal emulators are installed, the GUI
tells you the exact command to run by hand instead of failing silently.

**The GUI has no idea what state these are in once launched.** It
doesn't know if `relay.py` is still running, crashed, or was closed -
that's a real limitation of the detached-process approach, traded
deliberately for never risking a frozen GUI. `doctor.py`'s own
process-detection (already built, already used from the CLI) would be
the natural way to surface "is this actually running" back in the GUI
later, if that becomes worth doing.

## The "Gaps found" column

Three cheap checks run automatically, done directly in Python with no
subprocess call at all, so they update instantly on every refresh:

- does `flowgraphs/<name>.grc` exist?
- does `flowgraphs/<name>.py` exist? (has it ever been `grcc`'d?)
- is the `.py` *older* than the `.grc`? (stale - needs a fresh `grcc`)
- does the satellite have exactly one of `producer_port`/`consumer_port`
  rather than both or neither? (a malformed relay config)

These are deliberately narrow and not a replacement for `preflight.py`'s
full check - they exist purely to make the table itself useful without
needing to click anything first. Any satellite showing gaps here is
worth a "Run preflight.py" click for the complete picture.

## Known limitations (it's a rough prototype - being upfront about this)

- No confirmation dialog before Enable/Disable fires - it runs the
  moment you click
- The table's slug-guessing (turning a satellite's `script:` field back
  into its likely `.grc`/`.py` names) is a rough approximation of
  `add_satellite.py`'s real slugify logic, close enough for display but
  not authoritative
- No live status for anything launched in a new window (see above)
- Copy/Save only capture the output pane's current contents - there's no
  running log across multiple commands

## Running it

```
cd groundtrack
python3 groundtrack_gui.py
```

Same as every other script here: run it from the repo root, since it
reads `satellites.yaml` and calls the other scripts using relative
paths.
