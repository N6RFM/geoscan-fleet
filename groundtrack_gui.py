#!/usr/bin/env python3
"""
groundtrack_gui.py - rough prototype: a simple GUI over the existing
groundtrack scripts.

Deliberately does NOT modify, import, or depend on the internals of any
existing script. It only does two things:
  1. Reads satellites.yaml directly (read-only) and does cheap, obvious
     gap checks itself (does the .grc exist? the .py? is the .py older
     than the .grc?) for instant table feedback.
  2. Shells out to the real, unmodified scripts for everything else -
     preflight.py for the full/authoritative check, toggle_satellite.py
     to enable/disable, add_satellite.py to add, update_tle.py to
     refresh. Whatever each one prints goes straight into the output
     pane, unparsed and unmodified.

If this prototype is abandoned, nothing about the real toolkit needs to
be undone - this file can just be deleted.

Run from the repo root, same as every other script here:
    python3 groundtrack_gui.py
"""

import os
import subprocess
import sys
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, filedialog

import yaml

CONFIG_PATH = "satellites.yaml"


def load_satellites():
    if not os.path.exists(CONFIG_PATH):
        return []
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f) or {}
    return cfg.get("satellites", [])


def slug_for(sat):
    # mirrors add_satellite.py's own slugify closely enough for display
    # purposes - this is a rough read-only guess, not authoritative
    return sat.get("script", "").replace("flowgraphs/", "").replace(".py", "")


def gap_check(sat):
    """Cheap, obvious checks done directly - not a replacement for
    preflight.py, just instant table feedback before running it."""
    slug = slug_for(sat)
    grc_path = f"flowgraphs/{slug}.grc"
    py_path = f"flowgraphs/{slug}.py"

    problems = []
    if not sat.get("script"):
        problems.append("no script: field")
    if not os.path.exists(grc_path):
        problems.append(f"missing {grc_path}")
    elif not os.path.exists(py_path):
        problems.append(f"missing {py_path} (never grcc'd)")
    elif os.path.getmtime(py_path) < os.path.getmtime(grc_path):
        problems.append(f"{py_path} older than {grc_path} (stale, needs grcc)")

    uses_relay = "producer_port" in sat or "consumer_port" in sat
    if uses_relay and ("producer_port" not in sat or "consumer_port" not in sat):
        problems.append("has only one of producer_port/consumer_port")

    return problems


def find_terminal():
    """Return the first available terminal emulator's launch command
    prefix, or None if none of the common ones are installed."""
    import shutil
    candidates = [
        ("gnome-terminal", ["gnome-terminal", "--"]),
        ("konsole", ["konsole", "-e"]),
        ("xfce4-terminal", ["xfce4-terminal", "-e"]),
        ("x-terminal-emulator", ["x-terminal-emulator", "-e"]),
        ("xterm", ["xterm", "-e"]),
    ]
    for binary, prefix in candidates:
        if shutil.which(binary):
            return prefix
    return None


class GroundtrackGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("groundtrack - fleet overview (rough prototype)")
        self.geometry("900x600")

        self._build_scroll_container()
        self.repo_root = os.getcwd()
        self._build_table()
        self._build_actions()
        self._build_output()
        self.refresh()

    def _build_scroll_container(self):
        """Wraps everything below in a scrollable area with a slider on
        the right edge of the window, so the full layout (table, five
        button rows, output pane) stays reachable even if the window
        ends up shorter than its content on a smaller screen."""
        outer = ttk.Frame(self)
        outer.pack(fill="both", expand=True)

        canvas = tk.Canvas(outer, highlightthickness=0)
        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)

        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        # this is what every _build_* method below actually adds widgets to
        self.content = ttk.Frame(canvas)
        content_window = canvas.create_window((0, 0), window=self.content, anchor="nw")

        def on_content_resize(event):
            canvas.configure(scrollregion=canvas.bbox("all"))
        self.content.bind("<Configure>", on_content_resize)

        def on_canvas_resize(event):
            # stretch the inner frame to the canvas's full width, so
            # LabelFrames/buttons don't just hug the left edge
            canvas.itemconfig(content_window, width=event.width)
        canvas.bind("<Configure>", on_canvas_resize)

        def on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        canvas.bind_all("<MouseWheel>", on_mousewheel)          # Windows/macOS
        canvas.bind_all("<Button-4>", lambda e: canvas.yview_scroll(-1, "units"))  # Linux
        canvas.bind_all("<Button-5>", lambda e: canvas.yview_scroll(1, "units"))   # Linux

    def _build_table(self):
        columns = ("name", "norad", "freq", "enabled", "mode", "gaps")
        self.tree = ttk.Treeview(self.content, columns=columns, show="headings", height=10)
        headings = {
            "name": "Satellite", "norad": "NORAD", "freq": "Freq (Hz)",
            "enabled": "Enabled", "mode": "Mode", "gaps": "Gaps found",
        }
        widths = {"name": 110, "norad": 70, "freq": 100, "enabled": 70,
                  "mode": 110, "gaps": 350}
        for col in columns:
            self.tree.heading(col, text=headings[col])
            self.tree.column(col, width=widths[col], anchor="w")
        self.tree.tag_configure("hasgaps", background="#ffe4e4")
        self.tree.tag_configure("disabled", foreground="#888888")
        self.tree.pack(fill="x", padx=8, pady=8)

    def _build_actions(self):
        row1 = ttk.LabelFrame(self.content, text="Satellite management")
        row1.pack(fill="x", padx=8, pady=(4, 4))
        ttk.Button(row1, text="Refresh", command=self.refresh).pack(side="left")
        ttk.Button(row1, text="Enable selected",
                   command=lambda: self.toggle(True)).pack(side="left", padx=4)
        ttk.Button(row1, text="Disable selected",
                   command=lambda: self.toggle(False)).pack(side="left")
        ttk.Button(row1, text="Regenerate .grc for selected",
                   command=self.regen_selected).pack(side="left", padx=4)
        ttk.Button(row1, text="Delete selected",
                   command=self.delete_selected).pack(side="left")
        ttk.Button(row1, text="Add satellite...",
                   command=self.add_satellite_dialog).pack(side="left", padx=4)

        row2 = ttk.LabelFrame(self.content, text="Checks and maintenance")
        row2.pack(fill="x", padx=8, pady=4)
        ttk.Button(row2, text="Run preflight.py (full check)",
                   command=self.run_preflight).pack(side="left")
        ttk.Button(row2, text="Run doctor.py",
                   command=self.run_doctor).pack(side="left", padx=4)

        row3 = ttk.LabelFrame(self.content, text="TLE data")
        row3.pack(fill="x", padx=8, pady=4)
        ttk.Button(row3, text="Run update_tle.py",
                   command=self.run_update_tle).pack(side="left")

        row4 = ttk.LabelFrame(self.content, text="Pass scheduler")
        row4.pack(fill="x", padx=8, pady=4)
        ttk.Button(row4, text="Show schedule",
                   command=self.show_schedule).pack(side="left")
        ttk.Button(row4, text="Plan passes (auto-approve)",
                   command=self.plan_passes_auto).pack(side="left", padx=4)
        ttk.Button(row4, text="Plan passes (interactive, new window)",
                   command=self.plan_passes_interactive).pack(side="left")

        row5 = ttk.LabelFrame(self.content, text="Relay")
        row5.pack(fill="x", padx=8, pady=4)
        ttk.Button(row5, text="Start relay.py (new window, verbose)",
                   command=self.start_relay).pack(side="left")

        row6 = ttk.LabelFrame(self.content, text="Execution")
        row6.pack(fill="x", padx=8, pady=(4, 8))
        ttk.Button(row6, text="Start run_passes.py (new window)",
                   command=self.start_run_passes).pack(side="left")

    def _build_output(self):
        header = ttk.Frame(self.content)
        header.pack(fill="x", padx=8)
        ttk.Label(header, text="Output from the last command run:").pack(side="left")
        ttk.Button(header, text="Clear", command=self.clear_output).pack(side="right")
        ttk.Button(header, text="Copy", command=self.copy_output).pack(
            side="right", padx=8)
        ttk.Button(header, text="Save to file...",
                   command=self.save_output).pack(side="right", padx=4)

        output_frame = ttk.Frame(self.content)
        output_frame.pack(fill="both", expand=True, padx=8, pady=(4, 8))

        scrollbar = ttk.Scrollbar(output_frame, orient="vertical")
        scrollbar.pack(side="right", fill="y")

        self.output = tk.Text(output_frame, height=18, bg="#111", fg="#ddd",
                               font=("Courier", 10), wrap="word",
                               yscrollcommand=scrollbar.set)
        self.output.pack(side="left", fill="both", expand=True)
        scrollbar.config(command=self.output.yview)

    def clear_output(self):
        self.output.delete("1.0", tk.END)

    def copy_output(self):
        text = self.output.get("1.0", tk.END)
        self.clipboard_clear()
        self.clipboard_append(text)

    def save_output(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
            initialfile="groundtrack_output.txt")
        if not path:
            return
        with open(path, "w") as f:
            f.write(self.output.get("1.0", tk.END))

    def log(self, text):
        self.output.delete("1.0", tk.END)
        self.output.insert(tk.END, text)

    def run_cmd(self, args):
        """Run an existing script exactly as you'd type it - never
        touches its source, just captures what it prints.

        Inserts -u (unbuffered) right after the interpreter when the
        command is a python invocation. Without it, a script's stdout
        switches from line-buffered to fully block-buffered the moment
        it's a pipe rather than a real terminal (exactly what
        capture_output=True creates) - if that script itself launches a
        nested subprocess (like doctor.py calling preflight.py), the
        outer script's own buffered output can sit unflushed while the
        inner one runs, producing scrambled ordering or, in doctor.py's
        case, output that never completes at all. -u forces immediate
        flushing regardless of whether stdout is a terminal or a pipe."""
        if args and args[0] == sys.executable and "-u" not in args:
            args = [args[0], "-u"] + args[1:]
        self.log(f"$ {' '.join(args)}\n\n(running...)\n")
        self.update_idletasks()
        try:
            result = subprocess.run(args, capture_output=True, text=True,
                                     timeout=120)
            output = result.stdout + result.stderr
        except Exception as e:
            output = f"Failed to run: {e}"
        self.log(f"$ {' '.join(args)}\n\n{output}")
        return output

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        sats = load_satellites()
        if not sats and not os.path.exists(CONFIG_PATH):
            self.log(f"No {CONFIG_PATH} found in the current directory.\n"
                      f"Run this GUI from the repo root, same as every other script.")
            return
        for sat in sats:
            name = sat.get("name", "?")
            enabled = sat.get("enabled", True)
            uses_relay = "producer_port" in sat or "consumer_port" in sat
            mode = "decode+relay" if uses_relay else "recording-only"
            problems = gap_check(sat)
            gaps_text = "; ".join(problems) if problems else "none"

            tags = []
            if problems:
                tags.append("hasgaps")
            if not enabled:
                tags.append("disabled")

            self.tree.insert("", "end", iid=name, values=(
                name, sat.get("norad", "?"), sat.get("freq_hz", "?"),
                "yes" if enabled else "no", mode, gaps_text,
            ), tags=tags)

    def selected_name(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("No selection", "Select a satellite in the table first.")
            return None
        return sel[0]

    def toggle(self, enable):
        name = self.selected_name()
        if not name:
            return
        flag = "--enable" if enable else "--disable"
        self.run_cmd([sys.executable, "toggle_satellite.py", flag, name])
        self.refresh()

    def regen_selected(self):
        name = self.selected_name()
        if not name:
            return
        sats = load_satellites()
        sat = next((s for s in sats if s["name"] == name), None)
        if not sat:
            return
        grc_path = f"flowgraphs/{slug_for(sat)}.grc"
        if not os.path.exists(grc_path):
            messagebox.showerror("Missing .grc", f"{grc_path} doesn't exist - "
                                  f"nothing to regenerate. Use add_satellite.py first.")
            return
        self.run_cmd(["grcc", grc_path])
        self.refresh()

    def delete_selected(self):
        name = self.selected_name()
        if not name:
            return
        confirmed = messagebox.askyesno(
            "Delete satellite",
            f"Remove {name}'s entry from {CONFIG_PATH}?\n\n"
            f"Its .grc/.py files are NOT deleted by this - only the config "
            f"entry goes away.")
        if not confirmed:
            return
        self.run_cmd([sys.executable, "delete_satellite.py", name, "--yes"])
        self.refresh()

    def run_preflight(self):
        self.run_cmd([sys.executable, "preflight.py"])

    def run_doctor(self):
        self.run_cmd([sys.executable, "doctor.py"])

    def spawn_in_terminal(self, cmd):
        """Launch cmd (a list, e.g. [sys.executable, 'relay.py', '--verbose'])
        detached in its own terminal window. Explicitly cd's to the repo
        root first and holds the window open after the process exits,
        whether that exit is normal or a crash - without both of these,
        a terminal emulator like gnome-terminal can silently start in the
        wrong directory (its client/server model doesn't reliably inherit
        this process's cwd) and close the instant the command inside it
        exits, hiding any real error before you can read it."""
        import shlex
        prefix = find_terminal()
        if prefix is None:
            messagebox.showwarning(
                "No terminal emulator found",
                "Couldn't find gnome-terminal, konsole, xfce4-terminal, "
                "x-terminal-emulator, or xterm on this system.\n\n"
                "Run this manually instead, in your own terminal:\n\n"
                f"  cd {self.repo_root}\n  {' '.join(cmd)}")
            return False

        quoted_cmd = " ".join(shlex.quote(c) for c in cmd)
        shell_line = (
            f"cd {shlex.quote(self.repo_root)} && {quoted_cmd}; "
            f"echo; echo '--- process exited (see above for any error) ---'; "
            f"read -p 'Press Enter to close this window...'"
        )
        try:
            subprocess.Popen(prefix + ["bash", "-c", shell_line])
            self.log(f"Launched in a new window (in {self.repo_root}):\n"
                     f"$ {' '.join(cmd)}\n\n"
                     f"This GUI does not control it from here - use that "
                     f"window's own terminal to watch it, read any error, "
                     f"or Ctrl-C it. The window stays open after it exits "
                     f"so you can actually see what happened.")
            return True
        except Exception as e:
            messagebox.showerror("Failed to launch", str(e))
            return False

    def start_relay(self):
        """relay.py is a persistent process that runs indefinitely and
        never exits on its own - same reasoning as run_passes.py, this
        needs its own detached terminal, not a captured/blocking call
        that would freeze the GUI the moment it's clicked."""
        self.spawn_in_terminal([sys.executable, "relay.py", "--verbose"])

    def start_run_passes(self):
        """run_passes.py waits indefinitely for AOS and never exits on its
        own - running it the same way as preflight.py/doctor.py would
        freeze the whole GUI until it was killed. It needs its own
        detached process with its own visible terminal instead, so you
        can watch its live output and Ctrl-C it independently."""
        self.spawn_in_terminal([sys.executable, "run_passes.py", "--verbose"])

    def run_update_tle(self):
        sats = load_satellites()
        args = [sys.executable, "update_tle.py"]
        for sat in sats:
            norad = sat.get("norad")
            if norad:
                args += ["--extra-catnr", str(norad)]
        self.run_cmd(args)
        self.refresh()

    def show_schedule(self):
        self.run_cmd([sys.executable, "show_queue.py"])

    def plan_passes_auto(self):
        hours = simpledialog.askinteger(
            "Plan passes", "Hours ahead to predict:", initialvalue=24, minvalue=1)
        if not hours:
            return
        self.run_cmd([sys.executable, "plan_passes.py", "--hours", str(hours)])

    def plan_passes_interactive(self):
        """--interactive prompts y/n per pass on stdin - same reasoning
        as run_passes.py, this needs a real terminal, not a captured
        subprocess the GUI can't feed keystrokes into."""
        hours = simpledialog.askinteger(
            "Plan passes (interactive)", "Hours ahead to predict:",
            initialvalue=24, minvalue=1)
        if not hours:
            return
        cmd = [sys.executable, "plan_passes.py", "--hours", str(hours), "--interactive"]
        if self.spawn_in_terminal(cmd):
            self.log(self.output.get("1.0", tk.END) +
                     "\nApprove/reject passes there, then use 'Show schedule' "
                     "here once you're done.")

    def add_satellite_dialog(self):
        name = simpledialog.askstring("Add satellite", "Name (e.g. GEOSCAN-7):")
        if not name:
            return
        norad = simpledialog.askstring("Add satellite", "NORAD id:")
        if not norad:
            return
        freq = simpledialog.askstring("Add satellite", "Downlink frequency (Hz):")
        if not freq:
            return
        record_only = messagebox.askyesno(
            "Recording-only?",
            "Recording-only (no decoder yet, just raw IQ)?\n\n"
            "Choose 'No' for a normal decode-and-relay satellite.")

        args = [sys.executable, "add_satellite.py", "--name", name,
                "--norad", norad, "--freq", freq]
        if record_only:
            args += ["--template", "flowgraphs/geoscan1.grc", "--record-only"]
        self.run_cmd(args)
        self.refresh()


if __name__ == "__main__":
    GroundtrackGUI().mainloop()
