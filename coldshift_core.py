#!/usr/bin/env python3
"""
coldshift_core.py
Shared measurement and classification logic for ColdShift (CLI + GUI).

Measurement only. Nothing in here modifies, trims, or suspends a process.

Key points:
  - Activity is RECENT CPU use (delta between two samples), not the
    cumulative CPU time since the process started.
  - Memory is private memory (USS) where the OS lets us read it, so shared
    pages are not counted once per process. Falls back to working set (RSS).
  - Names are matched exactly (case-insensitive, ".exe" stripped), never by
    substring.
  - Only known background apps are ever marked SAFE. Unknown idle processes
    are marked REVIEW and are not counted in the "safe to offload" total.
"""

import os
import sys
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import psutil

# Classification labels
SAFE = "SAFE"        # known background app, idle, not in the foreground
REVIEW = "REVIEW"    # unknown app that is idle and large: user decides
ACTIVE = "ACTIVE"    # in use right now (foreground or recent CPU)
PROTECT = "PROTECT"  # never an offload candidate
NONE = ""            # too small to matter, or not measured yet

# Known background apps that tolerate being paged out when idle.
SAFE_OFFLOAD_NAMES = {
    "chrome", "msedge", "firefox", "brave", "opera", "vivaldi",
    "discord", "slack", "teams", "ms-teams", "zoom", "skype",
    "spotify", "vlc", "wmplayer", "itunes",
    "notepad", "wordpad", "mspaint",
    "calc", "calculatorapp",
    "onedrive", "dropbox", "googledrivefs",
    "steam", "steamwebhelper", "epicgameslauncher",
}

# Never offload candidates: OS core, shell, security, VMs, databases.
CRITICAL_NAMES = {
    # Windows core
    "system", "system idle process", "registry", "memory compression",
    "secure system", "smss", "csrss", "wininit", "services", "lsass",
    "lsaiso", "winlogon", "dwm", "audiodg", "fontdrvhost", "svchost",
    "spoolsv", "conhost", "sihost", "ctfmon", "taskhostw",
    # Shell and system UI
    "explorer", "searchhost", "searchindexer", "startmenuexperiencehost",
    "shellexperiencehost", "runtimebroker", "dllhost", "taskmgr",
    # Security
    "msmpeng", "mssense", "nissrv", "securityhealthservice",
    "securityhealthsystray", "smartscreen",
    # Virtual machines and containers
    "vmmem", "vmmemwsl", "vmwp", "vmcompute", "vmms", "vmware-vmx",
    "virtualboxvm", "vboxheadless", "qemu-system-x86_64",
    "com.docker.backend", "dockerd",
    # Databases
    "sqlservr", "postgres", "mysqld", "mongod", "redis-server", "oracle",
    # Unix core (when run from source on Linux/macOS)
    "systemd", "init", "launchd", "kernel_task", "windowserver",
}

SYSTEM_ACCOUNTS = {"system", "local service", "network service", "root"}

IDLE_CPU_PCT = 1.0      # below this (% of one core, recent) counts as idle
SAFE_MIN_MB = 40.0      # known background app must hold at least this much
REVIEW_MIN_MB = 80.0    # unknown idle app must hold at least this much
BUSY_IO_BPS = 256 * 1024  # recent disk/network I/O above this counts as busy

# Growth tracking (possible leak): flagged when a process has been watched
# for at least GROWTH_MIN_MINUTES and grew by both thresholds below.
GROWTH_MIN_MINUTES = 10.0
GROWTH_MIN_MB = 150.0
GROWTH_MIN_RATIO = 0.30


def normalize_name(name: Optional[str]) -> str:
    n = (name or "unknown").strip().lower()
    return n[:-4] if n.endswith(".exe") else n


def foreground_pid() -> Optional[int]:
    """PID that owns the foreground window (Windows only), else None."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value or None
    except Exception:
        return None


def classify(name: str, mem_mb: float, cpu_pct: Optional[float],
             is_foreground: bool, system_owned: bool,
             io_bps: Optional[float] = None,
             extra_safe=frozenset(), extra_protect=frozenset(),
             focus_apps=frozenset()) -> Tuple[str, str]:
    """Return (label, reason) for one process.

    extra_safe / extra_protect / focus_apps are the user's own lists of
    normalized names. Built-in protection always wins over extra_safe.
    """
    n = normalize_name(name)
    if n in CRITICAL_NAMES:
        return PROTECT, "system / protected process"
    if system_owned:
        return PROTECT, "runs as a system account"
    if n in focus_apps:
        return PROTECT, "your focus app"
    if n in extra_protect:
        return PROTECT, "on your protect list"
    if is_foreground:
        return ACTIVE, "foreground app"
    if cpu_pct is None:
        return NONE, "measuring activity..."
    if cpu_pct >= IDLE_CPU_PCT:
        return ACTIVE, f"busy ({cpu_pct:.1f}% CPU)"
    if io_bps is not None and io_bps >= BUSY_IO_BPS:
        return ACTIVE, f"busy (disk/network I/O, {io_bps / 1024:.0f} KB/s)"
    if n in extra_safe:
        if mem_mb >= SAFE_MIN_MB:
            return SAFE, "on your offload list, idle"
        return NONE, "idle, but too small to matter"
    if n in SAFE_OFFLOAD_NAMES:
        if mem_mb >= SAFE_MIN_MB:
            return SAFE, "known background app, idle"
        return NONE, "idle, but too small to matter"
    if mem_mb >= REVIEW_MIN_MB:
        return REVIEW, "idle, unknown app: check before offloading"
    return NONE, ""


@dataclass
class ProcRow:
    pid: int
    name: str
    mem_mb: float
    mem_exact: bool            # True = private memory, False = working set
    cpu_pct: Optional[float]   # recent CPU, % of one core; None on 1st sample
    label: str
    reason: str
    create_time: float = 0.0
    growth_mb: float = 0.0     # change in memory since first seen
    growth_min: float = 0.0    # minutes since first seen
    growing: bool = False      # steady, large growth: possible leak
    foreground: bool = False   # belongs to the app the user is using

    @property
    def growth_text(self) -> str:
        if self.growth_min < 5 or abs(self.growth_mb) < 50:
            return ""
        text = f"{self.growth_mb:+.0f} MB in {self.growth_min:.0f} min"
        return f"{text}  LEAK?" if self.growing else text


def system_memory() -> Dict[str, float]:
    mem = psutil.virtual_memory()
    gb = 1024 ** 3
    return {
        "total_gb": round(mem.total / gb, 2),
        "used_gb": round((mem.total - mem.available) / gb, 2),
        "available_gb": round(mem.available / gb, 2),
        "percent": mem.percent,
    }


class Sampler:
    """Takes process snapshots and derives recent CPU use between them."""

    def __init__(self):
        self._prev: Dict[Tuple[int, float], float] = {}
        self._prev_io: Dict[Tuple[int, float], float] = {}
        self._prev_time: Optional[float] = None
        self._first_seen: Dict[Tuple[int, float], Tuple[float, float]] = {}
        self._self_pid = os.getpid()
        # User rule lists (normalized names). Set these before sampling.
        self.extra_safe = frozenset()
        self.extra_protect = frozenset()
        self.focus_apps = frozenset()

    @property
    def seconds_since_sample(self) -> Optional[float]:
        if self._prev_time is None:
            return None
        return time.monotonic() - self._prev_time

    @property
    def primed(self) -> bool:
        return self._prev_time is not None

    def sample(self, min_mb: float = 0.0,
               always_include=frozenset()) -> List[ProcRow]:
        """Snapshot processes at or above min_mb, plus any PIDs listed in
        always_include (e.g. offloaded ones, which shrink once trimmed)."""
        now = time.monotonic()
        elapsed = (now - self._prev_time) if self._prev_time else None
        current: Dict[Tuple[int, float], float] = {}
        current_io: Dict[Tuple[int, float], float] = {}
        raw = []
        wall = time.time()

        fg_pid = foreground_pid()
        fg_name = None

        attrs = ["pid", "name", "memory_info", "cpu_times", "create_time",
                 "username", "io_counters"]
        for proc in psutil.process_iter(attrs):
            try:
                info = proc.info
                pid = info["pid"]
                name = info["name"] or "unknown"
                if pid == fg_pid:
                    fg_name = normalize_name(name)

                # No CPU times means access denied: we can't judge it.
                times = info["cpu_times"]
                cpu_total = (times.user + times.system) if times else None
                key = (pid, info["create_time"] or 0.0)
                if cpu_total is not None:
                    current[key] = cpu_total

                cpu_pct = None
                if (elapsed and elapsed > 0 and cpu_total is not None
                        and key in self._prev):
                    delta = max(0.0, cpu_total - self._prev[key])
                    cpu_pct = delta / elapsed * 100.0

                io_bps = None
                io = info.get("io_counters")
                if io is not None:
                    io_total = float(io.read_bytes + io.write_bytes)
                    current_io[key] = io_total
                    if elapsed and elapsed > 0 and key in self._prev_io:
                        io_bps = max(0.0, io_total - self._prev_io[key]) / elapsed

                meminfo = info["memory_info"]
                rss_mb = meminfo.rss / (1024 * 1024) if meminfo else 0.0
                if rss_mb < min_mb and pid not in always_include:
                    continue

                # Private memory is slower to read, so only do it for
                # processes that passed the size filter.
                mem_mb, exact = rss_mb, False
                try:
                    mem_mb = proc.memory_full_info().uss / (1024 * 1024)
                    exact = True
                except (psutil.AccessDenied, psutil.NoSuchProcess,
                        psutil.ZombieProcess, AttributeError, OSError):
                    pass

                user = info["username"]
                if user is None:
                    system_owned = True  # can't read owner: not ours
                else:
                    system_owned = (user.split("\\")[-1].lower()
                                    in SYSTEM_ACCOUNTS)
                if pid == self._self_pid or cpu_total is None:
                    system_owned = True

                raw.append((pid, name, mem_mb, exact, cpu_pct, system_owned,
                            io_bps, key))
            except (psutil.NoSuchProcess, psutil.AccessDenied,
                    psutil.ZombieProcess):
                continue

        self._prev = current
        self._prev_io = current_io
        self._prev_time = now
        # Forget growth history for processes that have exited.
        self._first_seen = {k: v for k, v in self._first_seen.items()
                            if k in current}

        rows = []
        for (pid, name, mem_mb, exact, cpu_pct, system_owned, io_bps,
             key) in raw:
            # Every process of the foreground app counts as in use
            # (e.g. all chrome.exe renderers when Chrome is focused).
            is_fg = pid == fg_pid or (fg_name is not None
                                      and normalize_name(name) == fg_name)
            label, reason = classify(name, mem_mb, cpu_pct, is_fg,
                                     system_owned, io_bps, self.extra_safe,
                                     self.extra_protect, self.focus_apps)

            first_time, first_mb = self._first_seen.setdefault(
                key, (wall, mem_mb))
            growth_mb = mem_mb - first_mb
            growth_min = (wall - first_time) / 60.0
            growing = (growth_min >= GROWTH_MIN_MINUTES
                       and growth_mb >= GROWTH_MIN_MB
                       and growth_mb >= first_mb * GROWTH_MIN_RATIO)

            rows.append(ProcRow(pid, name, round(mem_mb, 1), exact,
                                None if cpu_pct is None else round(cpu_pct, 1),
                                label, reason, key[1], round(growth_mb, 1),
                                round(growth_min, 1), growing, is_fg))
        return rows


_ORDER = {SAFE: 0, REVIEW: 1, ACTIVE: 2, NONE: 3, PROTECT: 4}


def sort_rows(rows: List[ProcRow]) -> List[ProcRow]:
    """Candidates first, then by memory descending."""
    return sorted(rows, key=lambda r: (_ORDER.get(r.label, 3), -r.mem_mb))


def total_mb(rows: List[ProcRow], label: str) -> float:
    return round(sum(r.mem_mb for r in rows if r.label == label), 1)
