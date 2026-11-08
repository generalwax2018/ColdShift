#!/usr/bin/env python3
"""
ColdShift-Measure.py
Selective process offloading - Measurement script (Python)

Lists processes, shows memory usage, and highlights potential offload candidates.
Does NOT modify or suspend any processes. Safe to run.

Requires: psutil
    pip install psutil
"""

import argparse
import psutil
from datetime import datetime

def get_system_memory():
    mem = psutil.virtual_memory()
    return {
        "total_gb": round(mem.total / (1024**3), 2),
        "used_gb": round(mem.used / (1024**3), 2),
        "free_gb": round(mem.available / (1024**3), 2),
        "percent": mem.percent,
    }

def get_process_info(min_ram_mb=50, top_n=30, show_all=False):
    processes = []
    for proc in psutil.process_iter(['pid', 'name', 'memory_info', 'cpu_times', 'status']):
        try:
            info = proc.info
            ram_mb = info['memory_info'].rss / (1024 * 1024) if info['memory_info'] else 0
            if show_all or ram_mb >= min_ram_mb:
                cpu_sec = 0
                if info['cpu_times']:
                    cpu_sec = info['cpu_times'].user + info['cpu_times'].system
                processes.append({
                    "pid": info['pid'],
                    "name": info['name'] or "unknown",
                    "ram_mb": round(ram_mb, 1),
                    "cpu_sec": round(cpu_sec, 1),
                    "status": info['status'] or "unknown",
                })
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    # Sort by RAM descending
    processes.sort(key=lambda x: x["ram_mb"], reverse=True)
    return processes[:top_n]

def main():
    parser = argparse.ArgumentParser(description="ColdShift process measurement")
    parser.add_argument("--min-ram", type=int, default=50, help="Minimum RAM in MB to show (default 50)")
    parser.add_argument("--top", type=int, default=30, help="Number of top processes to show (default 30)")
    parser.add_argument("--all", action="store_true", help="Show all processes (ignore min-ram)")
    args = parser.parse_args()

    print("ColdShift Process Measurement (Python)")
    print("======================================")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    # System memory
    sys_mem = get_system_memory()
    print("System Memory:")
    print(f"  Total: {sys_mem['total_gb']} GB")
    print(f"  Used:  {sys_mem['used_gb']} GB ({sys_mem['percent']}%)")
    print(f"  Free:  {sys_mem['free_gb']} GB")
    print()

    # Processes
    procs = get_process_info(args.min_ram, args.top, args.all)

    print(f"Top Processes by RAM Usage (min {args.min_ram} MB):")
    print(f"{'PID':<8} {'Name':<30} {'RAM_MB':>10} {'CPU_Sec':>10} {'Status'}")
    print(f"{'---':<8} {'----':<30} {'------':>10} {'-------':>10} {'------'}")

    candidate_ram = 0.0
    for p in procs:
        status = p["status"]
        marker = ""
        # Heuristic: high RAM + low CPU = potential offload candidate
        if p["ram_mb"] >= 100 and p["cpu_sec"] < 10:
            marker = " *"
            candidate_ram += p["ram_mb"]
            # Color not easily available in plain print; marker is enough
        print(f"{p['pid']:<8} {p['name']:<30} {p['ram_mb']:>10} {p['cpu_sec']:>10} {status}{marker}")

    print()
    print("Potential offload candidates (high RAM, low CPU) marked with *.")
    print(f"Estimated RAM in candidate processes: {round(candidate_ram, 1)} MB")
    print()
    print("Notes:")
    print("  - This script only measures. It does not suspend, trim, or kill anything.")
    print("  - Install dependency: pip install psutil")
    print("  - Use --all to show every process, --min-ram 100 --top 50 to adjust filters.")
    print("  - Next step: add classification rules and safe offload actions.")

if __name__ == "__main__":
    main()
