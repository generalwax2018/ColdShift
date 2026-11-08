#!/usr/bin/env python3
"""
ColdShift-GUI.py
Selective process offloading - GUI application (Python + CustomTkinter)

Shows system memory and running processes, highlights processes that are
safe to offload with little or no consequence.
Does NOT modify or suspend any processes. Safe to run.

Requires:
    pip install psutil customtkinter
"""

import customtkinter as ctk
import psutil
import threading
from datetime import datetime
from tkinter import ttk, messagebox

# Processes that are generally safe to page/offload when idle
SAFE_OFFLOAD_NAMES = {
    "chrome", "msedge", "firefox", "brave", "opera",
    "discord", "slack", "teams", "zoom", "skype",
    "spotify", "vlc", "wmplayer", "itunes",
    "explorer", "searchhost", "runtimebroker", "dllhost",
    "notepad", "wordpad", "calc", "mspaint",
    "onedrive", "dropbox", "googledrive",
    "steam", "epicgameslauncher",
}

# Processes we should never treat as offload candidates
CRITICAL_NAMES = {
    "system", "registry", "smss", "csrss", "wininit", "services",
    "lsass", "winlogon", "dwm", "audiodg", "fontdrvhost",
    "svchost", "explorer",  # explorer is borderline; handled carefully
}

class ColdShiftApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("ColdShift - Safe Process Offloading")
        self.geometry("1200x720")
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.min_ram = ctk.IntVar(value=30)
        self.top_n = ctk.IntVar(value=50)
        self.show_all = ctk.BooleanVar(value=False)
        self.auto_refresh = ctk.BooleanVar(value=False)

        self.create_widgets()
        self.refresh_data()

    def create_widgets(self):
        # Top frame - controls
        control_frame = ctk.CTkFrame(self)
        control_frame.pack(fill="x", padx=10, pady=5)

        ctk.CTkLabel(control_frame, text="Min RAM (MB):").pack(side="left", padx=5)
        ctk.CTkEntry(control_frame, textvariable=self.min_ram, width=60).pack(side="left", padx=5)

        ctk.CTkLabel(control_frame, text="Top N:").pack(side="left", padx=5)
        ctk.CTkEntry(control_frame, textvariable=self.top_n, width=60).pack(side="left", padx=5)

        ctk.CTkCheckBox(control_frame, text="Show All", variable=self.show_all).pack(side="left", padx=10)
        ctk.CTkCheckBox(control_frame, text="Auto Refresh (10s)", variable=self.auto_refresh, command=self.toggle_auto).pack(side="left", padx=10)

        ctk.CTkButton(control_frame, text="Refresh", command=self.refresh_data).pack(side="left", padx=10)

        # System memory frame
        self.mem_frame = ctk.CTkFrame(self)
        self.mem_frame.pack(fill="x", padx=10, pady=5)

        self.mem_label = ctk.CTkLabel(self.mem_frame, text="System Memory: loading...", font=ctk.CTkFont(size=14, weight="bold"))
        self.mem_label.pack(pady=5)

        self.candidate_label = ctk.CTkLabel(self.mem_frame, text="Safe to offload: --", text_color="#4ade80", font=ctk.CTkFont(size=13, weight="bold"))
        self.candidate_label.pack()

        # Process list
        list_frame = ctk.CTkFrame(self)
        list_frame.pack(fill="both", expand=True, padx=10, pady=5)

        columns = ("pid", "name", "ram_mb", "cpu_sec", "status", "safe")
        self.tree = ttk.Treeview(list_frame, columns=columns, show="headings", height=22)

        self.tree.heading("pid", text="PID")
        self.tree.heading("name", text="Process Name")
        self.tree.heading("ram_mb", text="RAM (MB)")
        self.tree.heading("cpu_sec", text="CPU (sec)")
        self.tree.heading("status", text="Status")
        self.tree.heading("safe", text="Safe to Offload")

        self.tree.column("pid", width=70, anchor="center")
        self.tree.column("name", width=280)
        self.tree.column("ram_mb", width=90, anchor="e")
        self.tree.column("cpu_sec", width=90, anchor="e")
        self.tree.column("status", width=90, anchor="center")
        self.tree.column("safe", width=140, anchor="center")

        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)

        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        style = ttk.Style()
        style.configure("Treeview", font=("Segoe UI", 10), rowheight=26)
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"))

        # Status bar
        self.status_label = ctk.CTkLabel(self, text="Ready", anchor="w")
        self.status_label.pack(fill="x", padx=10, pady=5)

        self.refresh_job = None

    def is_safe_to_offload(self, name: str, ram_mb: float, cpu_sec: float) -> bool:
        """Return True if the process is a good candidate to offload with little consequence."""
        name_l = name.lower()

        # Never touch critical system processes
        for crit in CRITICAL_NAMES:
            if crit in name_l:
                return False

        # Known safe background apps that are currently idle
        for safe in SAFE_OFFLOAD_NAMES:
            if safe in name_l and cpu_sec < 15 and ram_mb >= 40:
                return True

        # Generic high-RAM, low-activity processes that aren't system
        if ram_mb >= 80 and cpu_sec < 8 and "svchost" not in name_l:
            return True

        return False

    def toggle_auto(self):
        if self.auto_refresh.get():
            self.schedule_refresh()
        else:
            if self.refresh_job:
                self.after_cancel(self.refresh_job)
                self.refresh_job = None

    def schedule_refresh(self):
        if self.auto_refresh.get():
            self.refresh_data()
            self.refresh_job = self.after(10000, self.schedule_refresh)

    def refresh_data(self):
        self.status_label.configure(text="Refreshing...")
        self.update_idletasks()
        thread = threading.Thread(target=self._load_data, daemon=True)
        thread.start()

    def _load_data(self):
        try:
            mem = psutil.virtual_memory()
            total_gb = round(mem.total / (1024**3), 2)
            used_gb = round(mem.used / (1024**3), 2)
            free_gb = round(mem.available / (1024**3), 2)
            percent = mem.percent

            processes = []
            safe_ram = 0.0
            min_ram = self.min_ram.get()
            top_n = self.top_n.get()
            show_all = self.show_all.get()

            for proc in psutil.process_iter(['pid', 'name', 'memory_info', 'cpu_times', 'status']):
                try:
                    info = proc.info
                    ram_mb = info['memory_info'].rss / (1024 * 1024) if info['memory_info'] else 0
                    if show_all or ram_mb >= min_ram:
                        cpu_sec = 0.0
                        if info['cpu_times']:
                            cpu_sec = info['cpu_times'].user + info['cpu_times'].system

                        name = info['name'] or "unknown"
                        safe = self.is_safe_to_offload(name, ram_mb, cpu_sec)
                        if safe:
                            safe_ram += ram_mb

                        processes.append({
                            "pid": info['pid'],
                            "name": name[:45],
                            "ram_mb": round(ram_mb, 1),
                            "cpu_sec": round(cpu_sec, 1),
                            "status": info['status'] or "unknown",
                            "safe": "SAFE" if safe else ""
                        })
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue

            processes.sort(key=lambda x: (x["safe"] != "SAFE", -x["ram_mb"]))
            processes = processes[:top_n]

            self.after(0, lambda: self._update_ui(total_gb, used_gb, free_gb, percent, processes, safe_ram))

        except Exception as e:
            self.after(0, lambda: messagebox.showerror("Error", str(e)))

    def _update_ui(self, total, used, free, percent, processes, safe_ram):
        self.mem_label.configure(
            text=f"System Memory:  {used} GB used / {total} GB total  ({percent}%)   |   Free: {free} GB"
        )
        self.candidate_label.configure(
            text=f"Safe to offload (low consequence): {round(safe_ram, 1)} MB"
        )

        for item in self.tree.get_children():
            self.tree.delete(item)

        for p in processes:
            tag = "safe" if p["safe"] == "SAFE" else "normal"
            self.tree.insert("", "end", values=(
                p["pid"], p["name"], p["ram_mb"], p["cpu_sec"], p["status"], p["safe"]
            ), tags=(tag,))

        self.tree.tag_configure("safe", background="#14532d", foreground="#86efac")
        self.tree.tag_configure("normal", background="", foreground="")

        self.status_label.configure(
            text=f"Last updated: {datetime.now().strftime('%H:%M:%S')}  |  {len(processes)} processes shown  |  Green = safe to offload"
        )

if __name__ == "__main__":
    app = ColdShiftApp()
    app.mainloop()
