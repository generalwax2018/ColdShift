#!/usr/bin/env python3
"""
coldshift_diag.py
"Where is my RAM?" report for ColdShift.

ColdShift can only free memory held by ordinary apps. When RAM is full and
the apps don't add up, the memory is somewhere else: pages waiting to be
written to the page file, graphics memory spilling into system RAM, the
memory compression store, or kernel pools. This module measures those and
says which one it is, in plain language.

Read-only. It runs one PowerShell query for Windows performance counters
and saves the report as a text file in a "diagnostics" folder next to the
program.
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import psutil

from coldshift_actions import IS_WIN
from coldshift_core import normalize_name

GB = 1024 ** 3
_NO_WINDOW = 0x08000000 if IS_WIN else 0

_COUNTERS = [
    r"\Memory\Modified Page List Bytes",
    r"\Memory\Standby Cache Normal Priority Bytes",
    r"\Memory\Standby Cache Reserve Bytes",
    r"\Memory\Standby Cache Core Bytes",
    r"\Memory\Free & Zero Page List Bytes",
    r"\Memory\Pool Nonpaged Bytes",
    r"\Memory\Pool Paged Resident Bytes",
    r"\Memory\Pages Input/sec",
    r"\Paging File(_Total)\% Usage",
    r"\Process(_Total)\Working Set - Private",
    r"\Process(Memory Compression)\Working Set",
    r"\Process(Memory Compression)\% Processor Time",
    r"\Processor Information(_Total)\% Processor Performance",
    r"\GPU Adapter Memory(*)\Shared Usage",
    r"\GPU Adapter Memory(*)\Dedicated Usage",
    r"\GPU Process Memory(*)\Shared Usage",
    r"\GPU Process Memory(*)\Dedicated Usage",
]

_COUNTER_PS = (
    "$ErrorActionPreference = 'SilentlyContinue'\n"
    "$paths = @(" + ", ".join("'" + c + "'" for c in _COUNTERS) + ")\n"
    "$set = Get-Counter -Counter $paths -SampleInterval 1 -MaxSamples 1\n"
    "$samples = @($set.CounterSamples | Where-Object { $_.CookedValue -gt 0 } | "
    "ForEach-Object { @{ p = $_.Path; v = [double]$_.CookedValue } })\n"
    "$gpus = @(Get-CimInstance Win32_VideoController | ForEach-Object { $_.Name })\n"
    "@{ counters = $samples; gpus = $gpus } | ConvertTo-Json -Depth 4 -Compress\n"
)


def _as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _read_counters() -> Tuple[Dict[str, float], List[str]]:
    """Windows performance counters as {lowercase path without machine: value}."""
    if not IS_WIN:
        return {}, []
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _COUNTER_PS],
            capture_output=True, text=True, timeout=60, creationflags=_NO_WINDOW)
        data = json.loads(proc.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}, []
    counters = {}
    for item in _as_list(data.get("counters")):
        try:
            path = str(item["p"]).lower()
            path = re.sub(r"^\\\\[^\\]+", "", path)     # drop \\machine
            counters[path] = float(item["v"])
        except (KeyError, TypeError, ValueError):
            continue
    return counters, [str(g) for g in _as_list(data.get("gpus")) if g]


def _performance_info() -> Optional[dict]:
    """Commit charge and kernel pools from Windows, in bytes."""
    if not IS_WIN:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class _PerfInfo(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD),
                        ("CommitTotal", ctypes.c_size_t),
                        ("CommitLimit", ctypes.c_size_t),
                        ("CommitPeak", ctypes.c_size_t),
                        ("PhysicalTotal", ctypes.c_size_t),
                        ("PhysicalAvailable", ctypes.c_size_t),
                        ("SystemCache", ctypes.c_size_t),
                        ("KernelTotal", ctypes.c_size_t),
                        ("KernelPaged", ctypes.c_size_t),
                        ("KernelNonpaged", ctypes.c_size_t),
                        ("PageSize", ctypes.c_size_t),
                        ("HandleCount", wintypes.DWORD),
                        ("ProcessCount", wintypes.DWORD),
                        ("ThreadCount", wintypes.DWORD)]

        info = _PerfInfo()
        info.cb = ctypes.sizeof(info)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        if not psapi.GetPerformanceInfo(ctypes.byref(info), info.cb):
            return None
        page = info.PageSize
        return {"commit": info.CommitTotal * page,
                "commit_limit": info.CommitLimit * page,
                "commit_peak": info.CommitPeak * page,
                "kernel_paged": info.KernelPaged * page,
                "kernel_nonpaged": info.KernelNonpaged * page}
    except Exception:
        return None


def _apps() -> Tuple[List[dict], Dict[int, str]]:
    """Per-app totals: committed memory, memory in RAM, process count."""
    by_app: Dict[str, dict] = {}
    names: Dict[int, str] = {}
    for proc in psutil.process_iter(["pid", "name", "memory_info"]):
        try:
            info = proc.info
            mem = info["memory_info"]
            if mem is None:
                continue
            name = info["name"] or "unknown"
            names[info["pid"]] = name
            key = normalize_name(name)
            app = by_app.setdefault(key, {"name": name, "commit": 0, "ram": 0, "procs": 0})
            # Windows: "private" is committed private memory (RAM + page file).
            app["commit"] += getattr(mem, "private", None) or mem.rss
            app["ram"] += mem.rss
            app["procs"] += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return sorted(by_app.values(), key=lambda a: -a["commit"]), names


def collect() -> dict:
    """Gather everything. Takes a couple of seconds on Windows."""
    vm = psutil.virtual_memory()
    counters, gpus = _read_counters()
    perf = _performance_info()
    apps, names = _apps()

    def c(path: str) -> float:
        return counters.get(path.lower(), 0.0)

    gpu_shared = sum(v for p, v in counters.items()
                     if p.startswith("\\gpu adapter memory(") and p.endswith("\\shared usage"))
    gpu_dedicated = sum(v for p, v in counters.items()
                        if p.startswith("\\gpu adapter memory(") and p.endswith("\\dedicated usage"))

    gpu_by_pid: Dict[int, dict] = {}
    for path, value in counters.items():
        m = re.match(r"\\gpu process memory\(pid_(\d+)_.*\)\\(shared|dedicated) usage$", path)
        if m:
            entry = gpu_by_pid.setdefault(int(m.group(1)), {"shared": 0.0, "dedicated": 0.0})
            entry[m.group(2)] += value
    gpu_apps: Dict[str, dict] = {}
    for pid, usage in gpu_by_pid.items():
        name = names.get(pid, f"pid {pid}")
        entry = gpu_apps.setdefault(name, {"name": name, "shared": 0.0, "dedicated": 0.0})
        entry["shared"] += usage["shared"]
        entry["dedicated"] += usage["dedicated"]

    try:
        freq = psutil.cpu_freq()
    except Exception:
        freq = None

    data = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total": vm.total, "available": vm.available,
        "in_use": vm.total - vm.available, "percent": vm.percent,
        "perf": perf,
        "have_counters": bool(counters),
        "apps_private_ws": c(r"\Process(_Total)\Working Set - Private"),
        "compression": c(r"\Process(Memory Compression)\Working Set"),
        "compression_cpu": c(r"\Process(Memory Compression)\% Processor Time"),
        "modified": c(r"\Memory\Modified Page List Bytes"),
        "standby": (c(r"\Memory\Standby Cache Normal Priority Bytes")
                    + c(r"\Memory\Standby Cache Reserve Bytes")
                    + c(r"\Memory\Standby Cache Core Bytes")),
        "free": c(r"\Memory\Free & Zero Page List Bytes"),
        "nonpaged": c(r"\Memory\Pool Nonpaged Bytes") or (perf or {}).get("kernel_nonpaged", 0),
        "paged_resident": c(r"\Memory\Pool Paged Resident Bytes"),
        "hard_faults": c(r"\Memory\Pages Input/sec"),
        "pagefile_pct": c(r"\Paging File(_Total)\% Usage"),
        "cpu_perf_pct": c(r"\Processor Information(_Total)\% Processor Performance"),
        "cpu_freq": (freq.current, freq.max) if freq else None,
        "gpu_shared": gpu_shared, "gpu_dedicated": gpu_dedicated,
        "gpus": gpus,
        "gpu_apps": sorted(gpu_apps.values(), key=lambda a: -(a["shared"] + a["dedicated"]))[:6],
        "apps": apps[:12],
    }
    data["findings"] = findings(data)
    return data


def findings(d: dict) -> List[str]:
    """Plain-language conclusions, most important first."""
    out = []
    total = d["total"] or 1
    in_use = d["in_use"]
    perf = d.get("perf")

    if d["percent"] < 80:
        out.append("RAM is not under pressure right now, so this is a baseline. "
                   "Run it again while the PC is sluggish.")

    if perf and perf["commit_limit"]:
        ratio = perf["commit"] / perf["commit_limit"]
        if ratio >= 0.90:
            out.append(
                f"Windows is nearly out of committable memory ({perf['commit'] / GB:.1f} of "
                f"{perf['commit_limit'] / GB:.1f} GB, {ratio:.0%}). The page file is too small "
                f"for this workload: apps can stall or crash, and Windows cannot move idle "
                f"memory out of RAM. Make the page file larger or let Windows manage its size.")
        elif perf["commit_peak"] / perf["commit_limit"] >= 0.95:
            out.append(
                f"Committed memory has peaked at {perf['commit_peak'] / GB:.1f} of the "
                f"{perf['commit_limit'] / GB:.1f} GB limit since boot. The page file has been "
                f"too small at some point: make it larger or let Windows manage its size.")

    if d["gpu_shared"] >= 2 * GB:
        top = d["gpu_apps"][0]["name"] if d["gpu_apps"] else "a game or app"
        out.append(
            f"Graphics memory is spilling into system RAM: {d['gpu_shared'] / GB:.1f} GB of "
            f"shared GPU memory is in use, mostly by {top}. The graphics card's own memory is "
            f"full (or the game is running on integrated graphics). Lower texture quality or "
            f"resolution, and check the game is using the dedicated graphics card.")

    if d["modified"] >= 2 * GB:
        out.append(
            f"{d['modified'] / GB:.1f} GB of RAM is waiting to be written to the page file. "
            f"Windows has pushed idle memory out, but it cannot leave RAM until it is written, "
            f"so the page file is too slow, too small, or full.")

    if d["compression"] >= 2 * GB or d["compression_cpu"] >= 50:
        out.append(
            f"Windows is compressing memory hard ({d['compression'] / GB:.1f} GB store, "
            f"{d['compression_cpu']:.0f}% of a core). This is the sluggishness and part of the "
            f"heat: the workload needs more RAM than the PC has.")

    if d["nonpaged"] >= 2 * GB:
        out.append(
            f"The kernel non-paged pool is {d['nonpaged'] / GB:.1f} GB, which is unusually "
            f"large and usually means a driver is leaking memory. Update graphics, network "
            f"and storage drivers.")

    if d["apps"]:
        top = d["apps"][0]
        if top["commit"] >= 0.4 * total:
            out.append(
                f"{top['name']} has committed {top['commit'] / GB:.1f} GB "
                f"({top['commit'] / total:.0%} of your RAM), with {top['ram'] / GB:.1f} GB of it "
                f"in RAM right now. That one app is most of the demand.")

    if d["hard_faults"] >= 1000:
        out.append(f"The PC is reading {d['hard_faults']:,.0f} pages per second back from disk, "
                   f"which is heavy paging and causes stutter.")

    if 0 < d["cpu_perf_pct"] < 80:
        out.append(f"The processor is running at {d['cpu_perf_pct']:.0f}% of its rated speed, "
                   f"which suggests thermal or power throttling.")

    if d["have_counters"] and d["apps_private_ws"]:
        known = (d["apps_private_ws"] + d["modified"] + d["nonpaged"]
                 + d["paged_resident"] + d["gpu_shared"])
        offloadable_share = d["apps_private_ws"] / max(in_use, 1)
        if d["percent"] >= 85 and offloadable_share < 0.5:
            out.append(
                f"Only {d['apps_private_ws'] / GB:.1f} of the {in_use / GB:.1f} GB in use is "
                f"ordinary app memory, so offloading idle apps cannot fix this on its own.")
        if in_use - known >= 4 * GB:
            out.append(
                f"About {(in_use - known) / GB:.1f} GB is not explained by these measurements "
                f"(shared or mapped memory, or driver-locked memory).")
    elif IS_WIN:
        out.append("Windows performance counters could not be read, so the breakdown is "
                   "incomplete.")

    if not out:
        out.append("Nothing unusual found.")
    return out


def format_report(d: dict) -> str:
    def gb(value) -> str:
        return f"{value / GB:6.1f}"

    lines = [f"ColdShift memory report   {d['time']}", ""]
    lines.append(f"RAM:    {d['total'] / GB:.1f} GB total | {d['in_use'] / GB:.1f} GB in use "
                 f"({d['percent']:.0f}%) | {d['available'] / GB:.1f} GB available")
    perf = d.get("perf")
    if perf:
        lines.append(f"Commit: {perf['commit'] / GB:.1f} of {perf['commit_limit'] / GB:.1f} GB "
                     f"({perf['commit'] / max(perf['commit_limit'], 1):.0%}) | peak "
                     f"{perf['commit_peak'] / GB:.1f} GB")
    if d["pagefile_pct"]:
        lines.append(f"Page file: {d['pagefile_pct']:.0f}% used")
    if d["gpus"]:
        lines.append("Graphics: " + "; ".join(d["gpus"]))

    lines += ["", "FINDINGS"]
    lines += [f"  - {text}" for text in d["findings"]]

    if d["have_counters"]:
        known = (d["apps_private_ws"] + d["modified"] + d["nonpaged"]
                 + d["paged_resident"] + d["gpu_shared"])
        lines += ["", "WHERE THE IN-USE RAM IS (GB, approximate; some categories overlap)"]
        rows = [
            ("Apps, private memory in RAM", d["apps_private_ws"]),
            ("  of which memory compression store", d["compression"]),
            ("Waiting to be written to the page file", d["modified"]),
            ("Graphics memory held in system RAM", d["gpu_shared"]),
            ("Kernel non-paged pool", d["nonpaged"]),
            ("Kernel paged pool in RAM", d["paged_resident"]),
            ("Other / not explained", max(0.0, d["in_use"] - known)),
        ]
        lines += [f"  {label:<42}{gb(value)}" for label, value in rows]
        lines += [f"  {'(Standby cache, reusable, not in use)':<42}{gb(d['standby'])}",
                  f"  {'(Free)':<42}{gb(d['free'])}"]

    lines += ["", "BIGGEST APPS (GB)                         committed   in RAM   processes"]
    for app in d["apps"]:
        lines.append(f"  {app['name'][:38]:<40}{gb(app['commit'])}   {gb(app['ram'])}   "
                     f"{app['procs']:>6}")

    if d["gpu_apps"]:
        lines += ["", f"GRAPHICS MEMORY BY APP (GB)               dedicated   shared     "
                      f"(all apps: {d['gpu_dedicated'] / GB:.1f} dedicated, "
                      f"{d['gpu_shared'] / GB:.1f} shared)"]
        for app in d["gpu_apps"]:
            lines.append(f"  {app['name'][:38]:<40}{gb(app['dedicated'])}   {gb(app['shared'])}")

    lines += ["", "ACTIVITY"]
    if d["have_counters"]:
        lines.append(f"  Pages read back from disk per second:  {d['hard_faults']:,.0f}")
        lines.append(f"  Memory compression CPU (% of a core):  {d['compression_cpu']:.0f}")
        if d["cpu_perf_pct"]:
            lines.append(f"  Processor speed (% of rated):          {d['cpu_perf_pct']:.0f}")
    if d["cpu_freq"] and d["cpu_freq"][1]:
        lines.append(f"  Processor frequency (MHz):             {d['cpu_freq'][0]:.0f} "
                     f"of {d['cpu_freq'][1]:.0f}")
    return "\n".join(lines) + "\n"


def report_dir() -> str:
    base = os.path.dirname(os.path.abspath(sys.argv[0] or "."))
    path = os.path.join(base, "diagnostics")
    os.makedirs(path, exist_ok=True)
    return path


def save_report(text: str) -> str:
    name = f"memory-{datetime.now().strftime('%Y%m%d-%H%M%S')}.txt"
    path = os.path.join(report_dir(), name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def run() -> Tuple[str, str]:
    """Collect, format, and save. Returns (report text, saved path or '')."""
    text = format_report(collect())
    try:
        return text, save_report(text)
    except OSError:
        return text, ""
