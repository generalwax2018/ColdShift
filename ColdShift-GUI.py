#!/usr/bin/env python3
"""
ColdShift-GUI.py
Selective process offloading - GUI application (Python + CustomTkinter)

Shows system memory and running processes, highlights potential offload candidates.
Does NOT modify or suspend any processes. Safe to run.

Requires:
    pip install psutil customtkinter
"""

import customtkinter as ctk
import psutil
import threading
from datetime import datetime
from tkinter import ttk, messagebox

class ColdShiftApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("ColdShift - Process Offloading")
        self.geometry("1100x700")
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.min_ram = ctk.IntVar(value=50)
        self.top_n = ctk.IntVar(value=40)
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

        self.candidate_label = ctk.CTkLabel(self.mem_frame, text="Candidate RAM: --", text_color="#4ade80")
        self.candidate_label.pack()

        # Process list
        list_frame = ctk.CTkFrame(self)
        list_frame.pack(fill="both", expand=True, padx=10, pady=5)

        columns = ("pid", "name", "ram_mb", "cpu_sec", "status", "candidate")
        self.tree = ttk.Treeview(list_frame, columns=columns, show="headings", height=20)

        self.tree.heading("pid", text="PID")
        self.tree.heading("name", text="Process Name")
        self.tree.heading("ram_mb", text="RAM (MB)")
        self.tree.heading("cpu_sec", text="CPU (sec)")
        self.tree.heading("status", text="Status")
        self.tree.heading("candidate", text="Candidate")

        self.tree.column("pid", width=80, anchor="center")
        self.tree.column("name", width=300)
        self.tree.column("ram_mb", width=100, anchor="e")
        self.tree.column("cpu_sec", width=100, anchor="e")
        self.tree.column("status", width=100, anchor="center")
        self.tree.column("candidate", width=100, anchor="center")

        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)

        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # Style for candidates
        style = ttk.Style()
        style.configure("Treeview", font=("Segoe UI", 10), rowheight=25)
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"))

        # Status bar
        self.status_label = ctk.CTkLabel(self, text="Ready", anchor="w")
        self.status_label.pack(fill="x", padx=10, pady=5)

        self.refresh_job = None

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

        # Run in thread to keep UI responsive
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
            candidate_ram = 0.0
            min_ram = self.min_ram.get()
            top_n = self.top_n.get()
            show_all = self.show_all.get()

            for proc in psutil.process_iter(['pid', 'name', 'memory_info', 'cpu_times', 'status']):
                try:
                    info = proc.info
                    ram_mb = info['memory_info'].rss / (1024 * 1024) if info['memory_info'] else 0
                    if show_all or ram_mb >= min_ram:
                        cpu_sec = 0
                        if info['cpu_times']:
                            cpu_sec = info['cpu_times'].user + info['cpu_times'].system
                        is_candidate = ram_mb >= 100 and cpu_sec < 10
                        if is_candidate:
                            candidate_ram += ram_mb
                        processes.append({
                            "pid": info['pid'],
                            "name": (info['name'] or "unknown")[:40],
                            "ram_mb": round(ram_mb, 1),
                            "cpu_sec": round(cpu_sec, 1),
                            "status": info['status'] or "unknown",
                            "candidate": "YES" if is_candidate else ""
                        })
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue

            processes.sort(key=lambda x: x["ram_mb"], reverse=True)
            processes = processes[:top_n]

            # Update UI on main thread
            self.after(0, lambda: self._update_ui(total_gb, used_gb, free_gb, percent, processes, candidate_ram))

        except Exception as e:
            self.after(0, lambda: messagebox.showerror("Error", str(e)))

    def _update_ui(self, total, used, free, percent, processes, candidate_ram):
        self.mem_label.configure(
            text=f"System Memory:  {used} GB used / {total} GB total  ({percent}%)   |   Free: {free} GB"
        )
        self.candidate_label.configure(
            text=f"Potential offload candidates: {round(candidate_ram, 1)} MB"
        )

        # Clear tree
        for item in self.tree.get_children():
            self.tree.delete(item)

        for p in processes:
            tag = "candidate" if p["candidate"] == "YES" else "normal"
            self.tree.insert("", "end", values=(
                p["pid"], p["name"], p["ram_mb"], p["cpu_sec"], p["status"], p["candidate"]
            ), tags=(tag,))

        self.tree.tag_configure("candidate", background="#14532d", foreground="#86efac")
        self.tree.tag_configure("normal", background="", foreground="")

        self.status_label.configure(text=f"Last updated: {datetime.now().strftime('%H:%M:%S')}  |  {len(processes)} processes shown")

if __name__ == "__main__":
    app = ColdShiftApp()
    app.mainloop()
