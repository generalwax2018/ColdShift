#!/usr/bin/env python3
"""
ColdShift-Measure.py
Selective process offloading - Measurement script (Python)

Lists processes, shows private memory and recent CPU activity, and
classifies offload candidates.
Does NOT modify or suspend any processes. Safe to run.

Requires: psutil
    pip install psutil
"""

import argparse
import time
from datetime import datetime

from coldshift_actions import load_config, recover_stale
from coldshift_core import (REVIEW, SAFE, Sampler, sort_rows, system_memory,
                            total_mb)


def main():
    parser = argparse.ArgumentParser(description="ColdShift process measurement")
    parser.add_argument("--min-ram", type=int, default=50,
                        help="Minimum RAM in MB to show (default 50)")
    parser.add_argument("--top", type=int, default=30,
                        help="Number of processes to show (default 30)")
    parser.add_argument("--all", action="store_true",
                        help="Show all processes (ignore min-ram)")
    parser.add_argument("--interval", type=float, default=3.0,
                        help="Seconds to watch CPU activity (default 3)")
    parser.add_argument("--resume-all", action="store_true",
                        help="Resume apps left offloaded by a crashed ColdShift, then exit")
    parser.add_argument("--diagnose", action="store_true",
                        help="Print and save a 'where is my RAM' report, then exit")
    args = parser.parse_args()
    if args.diagnose:
        import coldshift_diag
        text, path = coldshift_diag.run()
        print(text)
        if path:
            print(f"Saved to {path}")
        return
    if args.resume_all:
        print(f"Restored {recover_stale()} processes.")
        return
    if args.interval <= 0:
        parser.error("--interval must be greater than 0")
    if args.top <= 0:
        parser.error("--top must be greater than 0")

    print("ColdShift Process Measurement (Python)")
    print("======================================")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    sys_mem = system_memory()
    print("System Memory:")
    print(f"  Total:     {sys_mem['total_gb']} GB")
    print(f"  Used:      {sys_mem['used_gb']} GB ({sys_mem['percent']}%)")
    print(f"  Available: {sys_mem['available_gb']} GB")
    print()

    print(f"Watching CPU activity for {args.interval:g}s...")
    sampler = Sampler()
    cfg = load_config()              # the lists you set up in the GUI
    sampler.extra_safe = frozenset(cfg["offload_apps"])
    sampler.extra_protect = frozenset(cfg["protect_apps"])
    sampler.focus_apps = frozenset(cfg["focus_apps"])
    sampler.sample()                 # baseline
    time.sleep(args.interval)
    min_mb = 0 if args.all else args.min_ram
    rows = sampler.sample(min_mb=min_mb)

    safe_mb = total_mb(rows, SAFE)
    review_mb = total_mb(rows, REVIEW)
    shown = sort_rows(rows)[:args.top]

    print()
    print(f"Processes (min {min_mb} MB), candidates first:")
    print(f"{'PID':<8} {'Name':<30} {'Mem_MB':>10} {'CPU_%':>7}  {'Class':<8} Reason")
    print(f"{'---':<8} {'----':<30} {'------':>10} {'-----':>7}  {'-----':<8} ------")
    for r in shown:
        mem = f"{r.mem_mb}{'' if r.mem_exact else '~'}"
        cpu = "-" if r.cpu_pct is None else f"{r.cpu_pct}"
        print(f"{r.pid:<8} {r.name[:30]:<30} {mem:>10} {cpu:>7}  {r.label:<8} {r.reason}")

    print()
    print(f"SAFE   (known background apps, idle): {safe_mb} MB")
    print(f"REVIEW (unknown apps, idle):          {review_mb} MB")
    print()
    print("Notes:")
    print("  - This script only measures. It does not suspend, trim, or kill anything.")
    print("  - Mem_MB is private memory. '~' means it could not be read and the")
    print("    working set (which includes shared pages) is shown instead.")
    print("  - CPU_% is recent use over the watch interval, as % of one core.")
    print("  - Run as administrator to see processes owned by other accounts.")


if __name__ == "__main__":
    main()
