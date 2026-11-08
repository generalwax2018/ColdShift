# ColdShift

Selective process offloading app.

**Goal:** Measure running processes, identify good candidates to move to a fast SSD, and free physical DDR5 for intensive workloads.

## Overview

ColdShift helps reclaim physical RAM by:
1. Measuring memory usage and activity of running processes
2. Classifying them as Protect / Offload / Leave alone
3. Moving low-priority processes to a fast secondary drive (SSD)
4. Keeping high-priority workloads in DDR5

## Current Features

- **CLI measurement**: `ColdShift-Measure.py`
- **GUI application**: `ColdShift-GUI.py` (CustomTkinter)
  - Live system memory display
  - Process list sorted by RAM
  - Highlights potential offload candidates (high RAM + low CPU)
  - Filters and auto-refresh
  - Measurement only (does not modify processes)

## Setup

```bash
pip install -r requirements.txt
```

## Run

```bash
# GUI (recommended)
python ColdShift-GUI.py

# CLI
python ColdShift-Measure.py
```

## Status

Early stage — measurement and classification layer first. Offload actions coming next.

## Platform

Windows first (tested). Linux/macOS should work with psutil.

## Next Steps

- Better classification rules and user protect/offload lists
- Safe offload actions (working set trim, suspend)
- Config persistence
- Tray icon / background mode
