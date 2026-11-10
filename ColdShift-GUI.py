#!/usr/bin/env python3
"""
ColdShift-GUI.py
Selective process offloading - GUI application (Python + CustomTkinter)

Shows system memory and running processes, highlights idle background apps,
and can offload them (Focus mode) so RAM goes to the app that needs it.

Nothing is changed until you press Start Focus or turn on Auto focus.
Everything Focus mode does is undone by End Focus, by closing the window,
or automatically on the next start if ColdShift was killed.

Requires:
    pip install psutil customtkinter
"""

import threading
import time
from datetime import datetime
from tkinter import TclError, messagebox, ttk

import customtkinter as ctk

import psutil

import coldshift_diag as diag_mod
import coldshift_drives as drives_mod
from coldshift_actions import (FULL, GENTLE, FocusSession, load_config,
                               pick_targets, recover_stale, running_app_names,
                               save_config)
from coldshift_core import (ACTIVE, PROTECT, REVIEW, SAFE, Sampler,
                            foreground_pid, normalize_name, sort_rows,
                            system_memory, total_mb)

PRIME_SECONDS = 1.0     # first refresh watches CPU this long for a baseline
TICK_MS = 500           # how often to check the foreground window
TRIGGER_EVERY = 6       # check for focus apps every N ticks (3 seconds)
PRESSURE_EVERY = 10     # check for full RAM every N ticks (5 seconds)
PRESSURE_PERCENT = 95   # auto-capture a memory report at or above this
PRESSURE_GAP_SECONDS = 120
PRESSURE_MAX_REPORTS = 10
OFFLOADED = "OFFLOADED"
LEAK = "LEAK"

MODE_LABELS = {
    GENTLE: "Gentle (deprioritize + trim)",
    FULL: "Full (suspend + trim)",
}
LABEL_TO_MODE = {v: k for k, v in MODE_LABELS.items()}


class ColdShiftApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("ColdShift - Safe Process Offloading")
        self.geometry("1320x760")
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.config_data = load_config()
        self.session = FocusSession()
        self.sampler = Sampler()
        self.sampler_lock = threading.Lock()

        self.min_ram = ctk.StringVar(value="30")
        self.top_n = ctk.StringVar(value="50")
        self.show_all = ctk.BooleanVar(value=False)
        self.auto_refresh = ctk.BooleanVar(value=False)
        self.mode_var = ctk.StringVar(value=MODE_LABELS[self.config_data["mode"]])
        self.auto_focus = ctk.BooleanVar(value=self.config_data["auto_focus"])

        self.loading = False
        self.focus_busy = False
        self.offloadable_mb = 0.0   # SAFE + REVIEW from the latest refresh
        self.drives_window = None
        self.diag_window = None
        self.auto_diag = ctk.BooleanVar(value=self.config_data["auto_diagnose"])
        self.diag_busy = False
        self.diag_last = 0.0
        self.diag_count = 0
        self.focus_was_auto = False
        self.auto_blocked = False   # user ended focus while a focus app ran
        self.refresh_job = None
        self.tick_count = 0
        self.closing = False

        self.create_widgets()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        recovered = recover_stale()
        if recovered:
            self.focus_label.configure(
                text=f"Restored {recovered} processes left offloaded by a previous run.")

        self._apply_rules()
        self.refresh_data()
        self.after(TICK_MS, self._tick)

    # ------------------------------------------------------------------ UI

    def create_widgets(self):
        # Top frame - view controls
        control_frame = ctk.CTkFrame(self)
        control_frame.pack(fill="x", padx=10, pady=5)

        ctk.CTkLabel(control_frame, text="Min RAM (MB):").pack(side="left", padx=5)
        ctk.CTkEntry(control_frame, textvariable=self.min_ram, width=60).pack(side="left", padx=5)

        ctk.CTkLabel(control_frame, text="Top N:").pack(side="left", padx=5)
        ctk.CTkEntry(control_frame, textvariable=self.top_n, width=60).pack(side="left", padx=5)

        ctk.CTkCheckBox(control_frame, text="Show All", variable=self.show_all).pack(side="left", padx=10)
        ctk.CTkCheckBox(control_frame, text="Auto Refresh (10s)", variable=self.auto_refresh,
                        command=self.toggle_auto).pack(side="left", padx=10)

        ctk.CTkButton(control_frame, text="Refresh", command=self.refresh_data).pack(side="left", padx=10)
        ctk.CTkButton(control_frame, text="Drives / Page file...", width=150,
                      command=self.open_drives).pack(side="right", padx=10)
        ctk.CTkButton(control_frame, text="Where is my RAM?", width=140,
                      command=self.open_diag).pack(side="right", padx=0)

        # Focus frame - actions
        focus_frame = ctk.CTkFrame(self)
        focus_frame.pack(fill="x", padx=10, pady=5)

        self.focus_button = ctk.CTkButton(focus_frame, text="Start Focus", width=120,
                                          command=self.toggle_focus)
        self.focus_button.pack(side="left", padx=(10, 5), pady=6)

        ctk.CTkOptionMenu(focus_frame, variable=self.mode_var, width=230,
                          values=list(MODE_LABELS.values()),
                          command=lambda _v: self._save_settings()).pack(side="left", padx=5)

        ctk.CTkCheckBox(focus_frame, text="Auto focus when a focus app runs",
                        variable=self.auto_focus,
                        command=self._on_auto_focus_changed).pack(side="left", padx=10)

        ctk.CTkLabel(focus_frame, text="Selected app:").pack(side="left", padx=(15, 5))
        ctk.CTkButton(focus_frame, text="Focus app", width=90,
                      command=lambda: self.set_rule("focus_apps")).pack(side="left", padx=3)
        ctk.CTkButton(focus_frame, text="Always offload", width=110,
                      command=lambda: self.set_rule("offload_apps")).pack(side="left", padx=3)
        ctk.CTkButton(focus_frame, text="Always protect", width=110,
                      command=lambda: self.set_rule("protect_apps")).pack(side="left", padx=3)
        ctk.CTkButton(focus_frame, text="Clear rule", width=90,
                      command=lambda: self.set_rule(None)).pack(side="left", padx=3)

        # System memory frame
        self.mem_frame = ctk.CTkFrame(self)
        self.mem_frame.pack(fill="x", padx=10, pady=5)

        self.mem_label = ctk.CTkLabel(self.mem_frame, text="System Memory: loading...",
                                      font=ctk.CTkFont(size=14, weight="bold"))
        self.mem_label.pack(pady=5)

        self.candidate_label = ctk.CTkLabel(self.mem_frame, text="Safe to offload: --",
                                            text_color="#4ade80",
                                            font=ctk.CTkFont(size=13, weight="bold"))
        self.candidate_label.pack()

        self.focus_label = ctk.CTkLabel(self.mem_frame, text="Focus mode: off",
                                        font=ctk.CTkFont(size=12))
        self.focus_label.pack(pady=(2, 5))

        # Process list
        list_frame = ctk.CTkFrame(self)
        list_frame.pack(fill="both", expand=True, padx=10, pady=5)

        columns = ("pid", "name", "mem_mb", "cpu_pct", "label", "growth", "reason")
        self.tree = ttk.Treeview(list_frame, columns=columns, show="headings",
                                 height=20, selectmode="browse")

        self.tree.heading("pid", text="PID")
        self.tree.heading("name", text="Process Name")
        self.tree.heading("mem_mb", text="Private RAM (MB)")
        self.tree.heading("cpu_pct", text="Recent CPU (%)")
        self.tree.heading("label", text="Class")
        self.tree.heading("growth", text="Growth since first seen")
        self.tree.heading("reason", text="Why")

        self.tree.column("pid", width=70, anchor="center")
        self.tree.column("name", width=240)
        self.tree.column("mem_mb", width=120, anchor="e")
        self.tree.column("cpu_pct", width=110, anchor="e")
        self.tree.column("label", width=100, anchor="center")
        self.tree.column("growth", width=200, anchor="center")
        self.tree.column("reason", width=320)

        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)

        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        style = ttk.Style()
        style.configure("Treeview", font=("Segoe UI", 10), rowheight=26)
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"))

        self.tree.tag_configure(SAFE, background="#14532d", foreground="#86efac")
        self.tree.tag_configure(REVIEW, background="#713f12", foreground="#fde68a")
        self.tree.tag_configure(OFFLOADED, background="#1e3a8a", foreground="#bfdbfe")
        self.tree.tag_configure(LEAK, background="#7f1d1d", foreground="#fecaca")
        self.tree.tag_configure(PROTECT, foreground="#9ca3af")
        self.tree.tag_configure(ACTIVE, background="", foreground="")

        # Status bar
        self.status_label = ctk.CTkLabel(self, text="Ready", anchor="w")
        self.status_label.pack(fill="x", padx=10, pady=5)

    # ------------------------------------------------------------ settings

    @staticmethod
    def _read_int(var, default, minimum):
        """Read a number from an entry box; fall back if empty or invalid."""
        try:
            return max(minimum, int(float(var.get().strip())))
        except (ValueError, TclError):
            return default

    def _mode(self):
        return LABEL_TO_MODE.get(self.mode_var.get(), GENTLE)

    def _save_settings(self):
        self.config_data["mode"] = self._mode()
        self.config_data["auto_focus"] = bool(self.auto_focus.get())
        self.config_data["auto_diagnose"] = bool(self.auto_diag.get())
        try:
            save_config(self.config_data)
        except OSError as exc:
            self.status_label.configure(text=f"Could not save settings: {exc}")

    def _apply_rules(self):
        """Push the user's lists into the sampler (main thread only)."""
        self.sampler.extra_safe = frozenset(self.config_data["offload_apps"])
        self.sampler.extra_protect = frozenset(self.config_data["protect_apps"])
        self.sampler.focus_apps = frozenset(self.config_data["focus_apps"])

    def _on_auto_focus_changed(self):
        self.auto_blocked = False
        self._save_settings()
        if self.auto_focus.get() and not self.config_data["focus_apps"]:
            messagebox.showinfo(
                "Auto focus",
                "Pick the app that needs the RAM first: select it in the list "
                "and press 'Focus app'.")

    def set_rule(self, list_name):
        """Put the selected app on one rule list (or none)."""
        selection = self.tree.selection()
        if not selection:
            messagebox.showinfo("Select an app", "Select a process in the list first.")
            return
        name = normalize_name(str(self.tree.item(selection[0])["values"][1]))
        for key in ("focus_apps", "offload_apps", "protect_apps"):
            names = set(self.config_data[key])
            names.discard(name)
            if key == list_name:
                names.add(name)
            self.config_data[key] = sorted(names)
        self._apply_rules()
        self._save_settings()
        text = {"focus_apps": "a focus app", "offload_apps": "always offload when idle",
                "protect_apps": "always protected", None: "back to default rules"}[list_name]
        self.status_label.configure(text=f"{name}: {text}")
        self.refresh_data()

    # ------------------------------------------------------------- refresh

    def toggle_auto(self):
        if self.auto_refresh.get():
            self.schedule_refresh()
        elif self.refresh_job:
            self.after_cancel(self.refresh_job)
            self.refresh_job = None

    def schedule_refresh(self):
        if self.auto_refresh.get():
            self.refresh_data()
            self.refresh_job = self.after(10000, self.schedule_refresh)

    def refresh_data(self):
        if self.loading or self.closing:
            return  # a refresh is already running
        self.loading = True

        # Tk variables must be read on the main thread, not in the worker.
        show_all = bool(self.show_all.get())
        min_mb = 0 if show_all else self._read_int(self.min_ram, 30, 0)
        top_n = self._read_int(self.top_n, 50, 1)

        self.status_label.configure(text="Refreshing...")
        threading.Thread(target=self._load_data, args=(min_mb, top_n), daemon=True).start()

    def _sample(self, min_mb):
        """Take a sample with a usable CPU baseline. Worker threads only."""
        with self.sampler_lock:
            since = self.sampler.seconds_since_sample
            if since is None:
                self.sampler.sample()
                time.sleep(PRIME_SECONDS)
            elif since < PRIME_SECONDS:
                time.sleep(PRIME_SECONDS - since)
            return self.sampler.sample(min_mb=min_mb,
                                       always_include=frozenset(self.session.pids()))

    def _load_data(self, min_mb, top_n):
        try:
            rows = self._sample(min_mb)
            mem = system_memory()
            safe_mb = total_mb(rows, SAFE)
            review_mb = total_mb(rows, REVIEW)
            offloaded = self.session.pids()   # keep these at the top of the list
            ordered = sorted(sort_rows(rows), key=lambda r: r.pid not in offloaded)
            shown = ordered[:top_n]
            self.after(0, lambda: self._update_ui(mem, shown, safe_mb, review_mb))
        except Exception as exc:
            msg = str(exc)  # capture now: exc is cleared when the block ends
            self.after(0, lambda: self._show_error(msg))

    def _show_error(self, msg):
        self.loading = False
        self.focus_busy = False
        self.status_label.configure(text="Something failed")
        messagebox.showerror("Error", msg)

    def _update_ui(self, mem, rows, safe_mb, review_mb):
        self.loading = False
        if self.closing:
            return
        self.mem_label.configure(
            text=f"System Memory:  {mem['used_gb']} GB used / {mem['total_gb']} GB total  "
                 f"({mem['percent']}%)   |   Available: {mem['available_gb']} GB"
        )
        self.candidate_label.configure(
            text=f"Safe to offload: {safe_mb} MB   |   Needs review: {review_mb} MB"
        )
        self.offloadable_mb = safe_mb + review_mb

        selected_pid = None
        selection = self.tree.selection()
        if selection:
            selected_pid = self.tree.item(selection[0])["values"][0]

        offloaded = self.session.pids()
        suspended = self.session.suspended_pids()

        self.tree.delete(*self.tree.get_children())
        for r in rows:
            label, reason, tag = r.label, r.reason, r.label
            if r.pid in offloaded:
                label = tag = OFFLOADED
                reason = ("suspended and trimmed (click its window to resume)"
                          if r.pid in suspended else "deprioritized and trimmed")
            elif r.growing:
                tag = LEAK
            mem_text = f"{r.mem_mb}" if r.mem_exact else f"~{r.mem_mb}"
            cpu_text = "-" if r.cpu_pct is None else f"{r.cpu_pct}"
            item = self.tree.insert("", "end", values=(
                r.pid, r.name[:45], mem_text, cpu_text, label, r.growth_text, reason
            ), tags=(tag,))
            if r.pid == selected_pid:
                self.tree.selection_set(item)

        self.status_label.configure(
            text=f"Last updated: {datetime.now().strftime('%H:%M:%S')}  |  "
                 f"{len(rows)} shown  |  Green = safe, amber = review, blue = offloaded, "
                 f"red = keeps growing (possible leak)  |  ~ = working set"
        )

    def open_diag(self):
        if self.diag_window is not None and self.diag_window.winfo_exists():
            self.diag_window.focus()
            self.diag_window.run()
            return
        self.diag_window = DiagWindow(self)

    def _auto_diag_worker(self):
        try:
            _text, path = diag_mod.run()
            msg = f"RAM nearly full: memory report saved to {path}" if path else \
                "RAM nearly full: memory report could not be saved"
        except Exception as exc:
            msg = f"Memory report failed: {exc}"
        def done():
            self.diag_busy = False
            if not self.closing:
                self.status_label.configure(text=msg)
        self.after(0, done)

    def open_drives(self):
        if self.drives_window is not None and self.drives_window.winfo_exists():
            self.drives_window.focus()
            return
        self.drives_window = DrivesWindow(self, self.offloadable_mb)

    # --------------------------------------------------------------- focus

    def toggle_focus(self):
        if self.session.active:
            self.end_focus(manual=True)
        else:
            self.start_focus(auto=False)

    def start_focus(self, auto):
        if self.session.active or self.focus_busy or self.closing:
            return
        mode = self._mode()
        if not auto and mode == FULL:
            ok = messagebox.askokcancel(
                "Full focus mode",
                "Idle background apps will be SUSPENDED until you end focus mode "
                "or click one of their windows.\n\n"
                "While suspended they are frozen: no notifications, and chat apps "
                "may need to reconnect afterwards.\n\nContinue?")
            if not ok:
                return
        self.focus_busy = True
        self.focus_was_auto = auto
        self._apply_rules()
        self.focus_button.configure(state="disabled", text="Working...")
        self.focus_label.configure(text="Focus mode: measuring and offloading...")
        threading.Thread(target=self._focus_worker, args=(mode,), daemon=True).start()

    def _focus_worker(self, mode):
        try:
            rows = self._sample(0)          # every process, for whole-app decisions
            targets = pick_targets(rows, mode)
            summary = self.session.start(targets, mode) if targets else None
            self.after(0, lambda: self._focus_started(summary))
        except Exception as exc:
            msg = str(exc)
            self.after(0, lambda: self._show_error(msg))
            self.after(0, self._sync_focus_button)

    def _focus_started(self, summary):
        self.focus_busy = False
        self._sync_focus_button()
        if not summary or not summary["processes"]:
            self.focus_label.configure(
                text="Focus mode: nothing to offload right now (no idle background apps).")
            return
        freed = summary["freed_mb"]
        apps = summary["apps"]
        names = ", ".join(apps[:4]) + (f" +{len(apps) - 4} more" if len(apps) > 4 else "")
        self.focus_label.configure(
            text=f"Focus mode ON ({summary['mode']}): {summary['processes']} processes "
                 f"offloaded ({names})  |  available RAM "
                 f"{summary['before_mb']} -> {summary['after_mb']} MB ({freed:+d} MB so far)")
        self.refresh_data()

    def end_focus(self, manual):
        count = self.session.end()
        if manual and self.focus_was_auto:
            self.auto_blocked = True   # don't restart until the focus app exits
        self.focus_was_auto = False
        self._sync_focus_button()
        self.focus_label.configure(text=f"Focus mode: off ({count} processes restored)")
        self.refresh_data()

    def _sync_focus_button(self):
        self.focus_button.configure(
            state="normal",
            text="End Focus" if self.session.active else "Start Focus")

    # ---------------------------------------------------------- background

    def _tick(self):
        """Light periodic checks: auto-resume and auto-focus."""
        if self.closing:
            return
        try:
            # Auto-resume: the user switched to an app we suspended.
            if self.session.active:
                pid = foreground_pid()
                if pid and self.session.resume_pid_app(pid):
                    self.status_label.configure(text="Resumed the app you switched to.")
                    if not self.session.active:
                        self._sync_focus_button()
                        self.focus_label.configure(text="Focus mode: off (everything resumed)")

            self.tick_count += 1
            if (self.tick_count % PRESSURE_EVERY == 0 and self.auto_diag.get()
                    and not self.diag_busy and self.diag_count < PRESSURE_MAX_REPORTS
                    and time.monotonic() - self.diag_last >= PRESSURE_GAP_SECONDS
                    and psutil.virtual_memory().percent >= PRESSURE_PERCENT):
                self.diag_busy = True
                self.diag_last = time.monotonic()
                self.diag_count += 1
                threading.Thread(target=self._auto_diag_worker, daemon=True).start()
            if self.tick_count % TRIGGER_EVERY == 0 and self.auto_focus.get():
                focus_apps = set(self.config_data["focus_apps"])
                running = bool(focus_apps & running_app_names()) if focus_apps else False
                if running and not self.session.active and not self.auto_blocked:
                    self.start_focus(auto=True)
                elif not running:
                    self.auto_blocked = False
                    if self.session.active and self.focus_was_auto and not self.focus_busy:
                        self.end_focus(manual=False)
        except Exception as exc:
            self.status_label.configure(text=f"Background check failed: {exc}")
        self.after(TICK_MS, self._tick)

    def on_close(self):
        self.closing = True
        try:
            self.session.end()   # never leave apps suspended
        finally:
            self.destroy()


class DiagWindow(ctk.CTkToplevel):
    """Shows the 'where is my RAM' report and saves it to a file."""

    def __init__(self, master):
        super().__init__(master)
        self.app = master
        self.title("ColdShift - Where is my RAM?")
        self.geometry(f"960x640+{master.winfo_x() + 40}+{master.winfo_y() + 40}")
        self.transient(master)
        self.busy = False

        top = ctk.CTkFrame(self)
        top.pack(fill="x", padx=12, pady=(12, 6))
        self.run_button = ctk.CTkButton(top, text="Measure again", command=self.run)
        self.run_button.pack(side="left", padx=5, pady=6)
        ctk.CTkCheckBox(top, text=f"Save a report automatically when RAM reaches {PRESSURE_PERCENT}%",
                        variable=master.auto_diag,
                        command=master._save_settings).pack(side="left", padx=15)
        ctk.CTkButton(top, text="Close", width=80, command=self.destroy).pack(side="right", padx=5)

        self.path_label = ctk.CTkLabel(self, text="", anchor="w", text_color="#9ca3af")
        self.path_label.pack(fill="x", padx=14)

        self.text = ctk.CTkTextbox(self, font=ctk.CTkFont(family="Consolas", size=12), wrap="word")
        self.text.pack(fill="both", expand=True, padx=12, pady=(4, 12))

        self.after(250, self._bring_to_front)
        self.run()

    def _bring_to_front(self):
        try:
            if self.winfo_exists():
                self.lift()
                self.focus_force()
        except TclError:
            pass

    def _show(self, text):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", text)
        self.text.configure(state="disabled")

    def run(self):
        if self.busy:
            return
        self.busy = True
        self.run_button.configure(state="disabled")
        self._show("Measuring (a few seconds)...")
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        try:
            text, path = diag_mod.run()
        except Exception as exc:
            text, path = f"The measurement failed: {exc}", ""

        def done():
            try:
                if not self.winfo_exists():
                    return
            except TclError:
                return
            self.busy = False
            self.run_button.configure(state="normal")
            self._show(text)
            self.path_label.configure(text=f"Saved to {path}" if path else "Report was not saved.")
        try:
            self.after(0, done)
        except (TclError, RuntimeError):
            pass


class DrivesWindow(ctk.CTkToplevel):
    """Detect drives, test their speed, and choose where the page file goes."""

    def __init__(self, master, offloadable_mb):
        super().__init__(master)
        self.title("ColdShift - Drives and Page File")
        # Open over the main window (same monitor), and in front of it.
        self.geometry(f"980x560+{master.winfo_x() + 40}+{master.winfo_y() + 40}")
        self.transient(master)
        self.after(250, self._bring_to_front)
        self.offloadable_mb = offloadable_mb
        self.drives = []
        self.pagefile = drives_mod.PageFileInfo()
        self.needed_mb = drives_mod.MIN_TOTAL_MB
        self.checks = {}           # letter -> BooleanVar
        self.busy = False
        self.keep_stub = ctk.BooleanVar(value=True)

        self.info_label = ctk.CTkLabel(self, text="Detecting drives...", anchor="w",
                                       justify="left", wraplength=940)
        self.info_label.pack(fill="x", padx=12, pady=(12, 4))

        self.table = ctk.CTkFrame(self)
        self.table.pack(fill="x", padx=12, pady=6)

        self.recommend_label = ctk.CTkLabel(self, text="", anchor="w", justify="left",
                                            wraplength=940, text_color="#4ade80")
        self.recommend_label.pack(fill="x", padx=12, pady=4)

        self.plan_label = ctk.CTkLabel(self, text="", anchor="w", justify="left",
                                       wraplength=940, font=ctk.CTkFont(family="Consolas", size=12))
        self.plan_label.pack(fill="x", padx=12, pady=4)

        ctk.CTkCheckBox(self, text="Keep a small page file on the Windows drive (needed for crash dumps)",
                        variable=self.keep_stub, command=self._update_plan).pack(anchor="w", padx=12, pady=4)

        buttons = ctk.CTkFrame(self)
        buttons.pack(fill="x", padx=12, pady=8)
        self.test_button = ctk.CTkButton(buttons, text="Test drive speed", command=self.run_speed_test)
        self.test_button.pack(side="left", padx=5, pady=6)
        self.rec_button = ctk.CTkButton(buttons, text="Use recommendation", command=self.use_recommendation)
        self.rec_button.pack(side="left", padx=5)
        self.apply_button = ctk.CTkButton(buttons, text="Apply...", fg_color="#b45309",
                                          hover_color="#92400e", command=self.apply)
        self.apply_button.pack(side="left", padx=5)
        self.restore_button = ctk.CTkButton(buttons, text="Restore original settings",
                                            command=self.restore)
        self.restore_button.pack(side="left", padx=5)
        ctk.CTkButton(buttons, text="Close", width=80, command=self.destroy).pack(side="right", padx=5)

        self.note_label = ctk.CTkLabel(
            self, anchor="w", justify="left", wraplength=940, text_color="#9ca3af",
            text="Windows decides which page file each app's memory goes to, so this sets where "
                 "offloaded memory can land, not which app goes where. Changes need administrator "
                 "rights and take effect after a restart. The speed test writes a temporary "
                 f"{drives_mod.BENCH_FILE_MB} MB file on each drive and deletes it.")
        self.note_label.pack(fill="x", padx=12, pady=(4, 12))

        self._set_busy(True)
        threading.Thread(target=self._detect_worker, daemon=True).start()

    # -- helpers

    def _bring_to_front(self):
        if self._alive():
            self.lift()
            self.focus_force()

    def _alive(self):
        try:
            return bool(self.winfo_exists())
        except TclError:
            return False

    def _post(self, fn):
        """Run fn on the UI thread if this window still exists."""
        def run():
            if self._alive():
                fn()
        try:
            self.after(0, run)
        except (TclError, RuntimeError):
            pass

    def _set_busy(self, busy):
        self.busy = busy
        state = "disabled" if busy else "normal"
        for button in (self.test_button, self.rec_button, self.apply_button):
            button.configure(state=state)
        self.restore_button.configure(
            state="normal" if (not busy and drives_mod.has_backup()) else "disabled")

    def _selected(self):
        return [letter for letter, var in self.checks.items() if var.get()]

    # -- detection

    def _detect_worker(self):
        try:
            found, pagefile = drives_mod.detect()
        except Exception as exc:
            msg = str(exc)
            self._post(lambda: self.info_label.configure(text=f"Could not detect drives: {msg}"))
            return
        self._post(lambda: self._detected(found, pagefile))

    def _detected(self, found, pagefile):
        self.drives, self.pagefile = found, pagefile
        self.needed_mb = drives_mod.needed_total_mb(self.offloadable_mb, pagefile.peak_mb)
        self.info_label.configure(
            text=f"{pagefile.describe()}\n"
                 f"Your idle apps hold about {self.offloadable_mb / 1024:.1f} GB that could be offloaded. "
                 f"Target page file size: {self.needed_mb / 1024:.0f} GB.")
        self._set_busy(False)
        self._build_table()
        self.use_recommendation()

    def _build_table(self):
        keep = {letter: var.get() for letter, var in self.checks.items()}
        for child in self.table.winfo_children():
            child.destroy()
        self.checks = {}
        headers = ["Use", "Drive", "Type", "Free", "Speed", "Rating"]
        for col, text in enumerate(headers):
            ctk.CTkLabel(self.table, text=text, font=ctk.CTkFont(weight="bold")).grid(
                row=0, column=col, padx=10, pady=4, sticky="w")
        for row, d in enumerate(self.drives, start=1):
            blocked = d.blocked_reason()
            var = ctk.BooleanVar(value=bool(keep.get(d.letter)) and not blocked)
            self.checks[d.letter] = var
            ctk.CTkCheckBox(self.table, text="", width=24, variable=var,
                            state="disabled" if blocked else "normal",
                            command=self._update_plan).grid(row=row, column=0, padx=10, pady=3, sticky="w")
            name = f"{d.letter}:  {d.model[:34]}" + ("  (Windows)" if d.system else "")
            rating = d.rating if not blocked else f"{d.rating}: {blocked}"
            color = {"Fast": "#4ade80", "OK": "#fde68a", "Too slow": "#fca5a5"}.get(d.rating, "#d1d5db")
            cells = [name, d.kind, f"{d.free_gb:.0f} of {d.size_gb:.0f} GB", d.speed_text(), rating]
            for col, text in enumerate(cells, start=1):
                ctk.CTkLabel(self.table, text=text,
                             text_color=color if col == 5 else None).grid(
                    row=row, column=col, padx=10, pady=3, sticky="w")

    # -- recommendation and plan

    def use_recommendation(self):
        picked, text = drives_mod.recommend(self.drives, self.needed_mb)
        for letter, var in self.checks.items():
            var.set(letter in picked)
        self.recommend_label.configure(text=text)
        self._update_plan()

    def _current_plan(self):
        return drives_mod.build_plan(self.drives, self._selected(), self.needed_mb,
                                     bool(self.keep_stub.get()))

    def _update_plan(self):
        try:
            plan = self._current_plan()
            self.plan_label.configure(text="Plan:\n" + drives_mod.describe_plan(plan))
        except ValueError as exc:
            self.plan_label.configure(text=f"Plan: {exc}")

    # -- speed test

    def run_speed_test(self):
        if self.busy:
            return
        self._set_busy(True)
        self.recommend_label.configure(text="Testing drive speed (a few seconds per drive)...")
        threading.Thread(target=self._speed_worker, daemon=True).start()

    def _speed_worker(self):
        for d in self.drives:
            try:
                drives_mod.speed_test(d)
            except Exception as exc:
                d.test_note = f"test failed ({exc})"
        self._post(self._speed_done)

    def _speed_done(self):
        self._set_busy(False)
        self._build_table()
        self.use_recommendation()

    # -- apply / restore

    def apply(self):
        if self.busy:
            return
        try:
            plan = self._current_plan()
        except ValueError as exc:
            messagebox.showerror("Page file", str(exc), parent=self)
            return
        ok = messagebox.askokcancel(
            "Change the page file?",
            "This changes a Windows system setting:\n\n"
            f"{drives_mod.describe_plan(plan)}\n\n"
            "Any other page files are removed. Windows will ask for administrator "
            "approval. Nothing changes until you restart the PC.\n\n"
            "Your current settings are saved first, and 'Restore original settings' "
            "puts them back.", parent=self)
        if not ok:
            return
        self._set_busy(True)
        threading.Thread(target=self._apply_worker, args=(plan,), daemon=True).start()

    def _apply_worker(self, plan):
        try:
            ok, error = drives_mod.apply_plan(plan, self.pagefile)
        except Exception as exc:
            ok, error = False, str(exc)
        self._post(lambda: self._change_done(ok, error, "Page file settings saved."))

    def restore(self):
        if self.busy:
            return
        if not messagebox.askokcancel(
                "Restore page file settings?",
                "Put the page file back the way it was before ColdShift changed it? "
                "Windows will ask for administrator approval, and it takes effect "
                "after a restart.", parent=self):
            return
        self._set_busy(True)
        threading.Thread(target=self._restore_worker, daemon=True).start()

    def _restore_worker(self):
        try:
            ok, error = drives_mod.restore_backup()
        except Exception as exc:
            ok, error = False, str(exc)
        self._post(lambda: self._change_done(ok, error, "Original page file settings restored."))

    def _change_done(self, ok, error, success_text):
        self._set_busy(False)
        if ok:
            messagebox.showinfo("Page file", f"{success_text}\n\nRestart the PC for it to take effect.",
                                parent=self)
        else:
            messagebox.showerror("Page file", f"The change did not go through.\n\n{error}", parent=self)


if __name__ == "__main__":
    app = ColdShiftApp()
    app.mainloop()
