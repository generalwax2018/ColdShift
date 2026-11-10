#!/usr/bin/env python3
"""
coldshift_actions.py
Offload actions, focus sessions, and saved settings for ColdShift.

THIS MODULE CHANGES OTHER PROCESSES. Everything it does is reversible:

  Gentle mode: lower the memory priority of background apps (Windows
               reclaims their pages first) and trim their working sets.
               Apps keep running and may pull memory back.
  Full mode:   also suspend them, so the memory stays freed until they
               are resumed. Suspended apps are frozen: no notifications,
               and chat apps may need to reconnect afterwards.

Safety:
  - Every process touched is written to a state file BEFORE it is touched,
    so a crash can never leave apps frozen: recover_stale() resumes them on
    the next start, and `ColdShift-Measure.py --resume-all` does it by hand.
  - Only apps where every process is idle and none is protected are picked.
  - The priority and trim calls are Windows-only. Elsewhere they do nothing.
"""

import json
import os
import sys
import threading
import time
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Tuple

import psutil

from coldshift_core import (ACTIVE, IDLE_CPU_PCT, PROTECT, SAFE, ProcRow,
                            normalize_name)

IS_WIN = sys.platform == "win32"

GENTLE = "gentle"
FULL = "full"

# Windows memory priority levels (5 is the default and the maximum).
MEMORY_PRIORITY_LOW = 2
MEMORY_PRIORITY_NORMAL = 5

DEFAULT_CONFIG = {
    "mode": GENTLE,
    "auto_focus": False,   # start focus mode when a focus app is running
    "auto_diagnose": False,  # save a memory report when RAM is nearly full
    "focus_apps": [],      # apps that need the RAM (never offloaded)
    "offload_apps": [],    # extra apps that are fine to offload when idle
    "protect_apps": [],    # extra apps that must never be offloaded
}


# --------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------

def app_dir() -> str:
    if IS_WIN and os.environ.get("APPDATA"):
        base = os.path.join(os.environ["APPDATA"], "ColdShift")
    else:
        base = os.path.join(os.path.expanduser("~"), ".config", "coldshift")
    os.makedirs(base, exist_ok=True)
    return base


def _path(name: str) -> str:
    return os.path.join(app_dir(), name)


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(_path("config.json"), "r", encoding="utf-8") as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            for key, default in DEFAULT_CONFIG.items():
                if isinstance(saved.get(key), type(default)):
                    cfg[key] = saved[key]
    except (OSError, ValueError):
        pass
    if cfg["mode"] not in (GENTLE, FULL):
        cfg["mode"] = GENTLE
    for key in ("focus_apps", "offload_apps", "protect_apps"):
        cfg[key] = sorted({normalize_name(n) for n in cfg[key] if isinstance(n, str)})
    return cfg


def save_config(cfg: dict) -> None:
    _write_json(_path("config.json"), cfg)


def _write_json(path: str, data) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def log_action(text: str) -> None:
    try:
        with open(_path("actions.log"), "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  {text}\n")
    except OSError:
        pass


# --------------------------------------------------------------------------
# Windows process calls
# --------------------------------------------------------------------------

if IS_WIN:
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.CloseHandle.restype = wintypes.BOOL
    _k32.K32EmptyWorkingSet.argtypes = [wintypes.HANDLE]
    _k32.K32EmptyWorkingSet.restype = wintypes.BOOL
    _k32.SetProcessInformation.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                           ctypes.c_void_p, wintypes.DWORD]
    _k32.SetProcessInformation.restype = wintypes.BOOL

    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_SET_INFORMATION = 0x0200
    _PROCESS_QUERY_INFORMATION = 0x0400
    _PROCESS_MEMORY_PRIORITY = 0  # PROCESS_INFORMATION_CLASS.ProcessMemoryPriority

    class _MemoryPriorityInfo(ctypes.Structure):
        _fields_ = [("MemoryPriority", wintypes.ULONG)]


def trim_working_set(pid: int) -> bool:
    """Ask Windows to move the process's pages out of its working set."""
    if not IS_WIN:
        return False
    handle = _k32.OpenProcess(_PROCESS_QUERY_INFORMATION | _PROCESS_SET_QUOTA,
                              False, pid)
    if not handle:
        return False
    try:
        return bool(_k32.K32EmptyWorkingSet(handle))
    finally:
        _k32.CloseHandle(handle)


def set_memory_priority(pid: int, level: int) -> bool:
    """Set how readily Windows reclaims this process's memory (1-5)."""
    if not IS_WIN:
        return False
    handle = _k32.OpenProcess(_PROCESS_SET_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        info = _MemoryPriorityInfo(level)
        return bool(_k32.SetProcessInformation(
            handle, _PROCESS_MEMORY_PRIORITY, ctypes.byref(info),
            ctypes.sizeof(info)))
    finally:
        _k32.CloseHandle(handle)


def _same_process(pid: int, create_time: float) -> Optional[psutil.Process]:
    """Return the process only if the PID still belongs to the same program."""
    try:
        proc = psutil.Process(pid)
        if abs(proc.create_time() - create_time) < 1.0:
            return proc
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        pass
    return None


# --------------------------------------------------------------------------
# Choosing what to offload
# --------------------------------------------------------------------------

Target = Tuple[int, str, float]  # pid, name, create_time


FULL_APP_CPU_PCT = 10.0  # an app using more than this in total is in use


def pick_targets(rows: Iterable[ProcRow], mode: str = FULL) -> List[Target]:
    """Pick what to offload from a full (unfiltered) sample.

    Gentle mode never freezes anything, so each idle SAFE process is picked
    on its own. A browser with one busy tab still gives up its idle tabs.

    Full mode suspends, so it works on whole apps: an app is picked only if
    it has a SAFE process, is not in the foreground, uses little CPU in
    total, and has no process doing heavy I/O. Its protected processes are
    left running; everything else in the app is suspended together.
    """
    rows = list(rows)
    if mode != FULL:
        return [(p.pid, p.name, p.create_time) for p in rows if p.label == SAFE]

    by_app: Dict[str, List[ProcRow]] = {}
    for row in rows:
        by_app.setdefault(normalize_name(row.name), []).append(row)

    targets: List[Target] = []
    for procs in by_app.values():
        if not any(p.label == SAFE for p in procs):
            continue
        if any(p.foreground for p in procs):
            continue
        if sum(p.cpu_pct or 0.0 for p in procs) >= FULL_APP_CPU_PCT:
            continue
        # ACTIVE with little CPU means it was flagged for disk/network I/O.
        if any(p.label == ACTIVE and (p.cpu_pct or 0.0) < IDLE_CPU_PCT
               for p in procs):
            continue
        targets.extend((p.pid, p.name, p.create_time) for p in procs
                       if p.label != PROTECT)
    return targets


def running_app_names() -> set:
    names = set()
    for proc in psutil.process_iter(["name"]):
        try:
            names.add(normalize_name(proc.info["name"]))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return names


# --------------------------------------------------------------------------
# Focus session
# --------------------------------------------------------------------------

def _available_mb() -> float:
    return psutil.virtual_memory().available / (1024 * 1024)


class FocusSession:
    """Offloads background apps and can put everything back."""

    def __init__(self):
        self._lock = threading.RLock()
        self._entries: Dict[int, dict] = {}
        self.mode: Optional[str] = None
        self.summary: Optional[dict] = None

    @property
    def active(self) -> bool:
        return bool(self._entries)

    def pids(self) -> set:
        with self._lock:
            return set(self._entries)

    def suspended_pids(self) -> set:
        with self._lock:
            return {pid for pid, e in self._entries.items() if e["suspended"]}

    def _save_state(self) -> None:
        path = _path("state.json")
        try:
            if self._entries:
                _write_json(path, list(self._entries.values()))
            elif os.path.exists(path):
                os.remove(path)
        except OSError:
            pass

    def start(self, targets: List[Target], mode: str,
              settle_seconds: float = 2.0) -> dict:
        """Offload the targets. Returns a summary including memory freed."""
        with self._lock:
            before = _available_mb()
            self.mode = mode
            own = os.getpid()

            # Record intent first, so a crash mid-way is recoverable.
            for pid, name, create_time in targets:
                if pid == own or pid in self._entries:
                    continue
                self._entries[pid] = {
                    "pid": pid, "name": name, "create_time": create_time,
                    "suspended": mode == FULL, "deprioritized": True,
                }
            self._save_state()

            done, failed = 0, 0
            for pid in list(self._entries):
                entry = self._entries[pid]
                proc = _same_process(pid, entry["create_time"])
                if proc is None:
                    del self._entries[pid]
                    failed += 1
                    continue
                try:
                    if mode == FULL:
                        proc.suspend()
                    entry["deprioritized"] = set_memory_priority(
                        pid, MEMORY_PRIORITY_LOW)
                    trim_working_set(pid)
                    done += 1
                except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                    try:
                        proc.resume()
                    except (psutil.Error, OSError):
                        pass
                    del self._entries[pid]
                    failed += 1
            self._save_state()
            apps = sorted({normalize_name(e["name"])
                           for e in self._entries.values()})

        # Trimmed pages take a moment to leave physical memory.
        time.sleep(settle_seconds)
        after = _available_mb()

        self.summary = {
            "mode": mode, "apps": apps, "processes": done, "failed": failed,
            "before_mb": round(before), "after_mb": round(after),
            "freed_mb": round(after - before),
        }
        log_action(f"focus start ({mode}): {done} processes in "
                   f"{len(apps)} apps [{', '.join(apps)}], {failed} skipped, "
                   f"available {before:.0f} -> {after:.0f} MB "
                   f"({after - before:+.0f} MB)")
        return self.summary

    def _restore(self, entry: dict) -> None:
        proc = _same_process(entry["pid"], entry["create_time"])
        if proc is None:
            return
        try:
            if entry.get("suspended"):
                proc.resume()
            if entry.get("deprioritized"):
                set_memory_priority(entry["pid"], MEMORY_PRIORITY_NORMAL)
        except (psutil.Error, OSError):
            pass

    def resume_app(self, name: str) -> int:
        """Bring back every process of one app. Returns how many."""
        target = normalize_name(name)
        with self._lock:
            pids = [pid for pid, e in self._entries.items()
                    if normalize_name(e["name"]) == target]
            for pid in pids:
                self._restore(self._entries.pop(pid))
            if pids:
                self._save_state()
                log_action(f"resumed app: {target} ({len(pids)} processes)")
            return len(pids)

    def resume_pid_app(self, pid: int) -> int:
        """Bring back the app that owns this PID, if we offloaded it."""
        with self._lock:
            entry = self._entries.get(pid)
            return self.resume_app(entry["name"]) if entry else 0

    def end(self) -> int:
        """Bring everything back. Returns how many processes."""
        with self._lock:
            count = len(self._entries)
            for entry in self._entries.values():
                self._restore(entry)
            self._entries.clear()
            self._save_state()
            self.mode = None
            if count:
                log_action(f"focus end: {count} processes restored")
            return count


def recover_stale() -> int:
    """Resume anything a previous run left offloaded (e.g. after a crash)."""
    path = _path("state.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            entries = json.load(f)
    except (OSError, ValueError):
        return 0

    count = 0
    for entry in entries if isinstance(entries, list) else []:
        try:
            proc = _same_process(int(entry["pid"]), float(entry["create_time"]))
            if proc is None:
                continue
            if entry.get("suspended"):
                proc.resume()
            if entry.get("deprioritized"):
                set_memory_priority(proc.pid, MEMORY_PRIORITY_NORMAL)
            count += 1
        except (KeyError, TypeError, ValueError, psutil.Error, OSError):
            continue
    try:
        os.remove(path)
    except OSError:
        pass
    if count:
        log_action(f"recovery: {count} processes restored from a previous run")
    return count
