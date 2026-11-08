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
  - Highlights processes that are safe to offload with little consequence
  - Filters and auto-refresh
  - Measurement only (does not modify processes)

## Setup (development)

```bash
pip install -r requirements.txt
```

## Run from source

```bash
# GUI (recommended)
python ColdShift-GUI.py

# CLI
python ColdShift-Measure.py
```

## Build a standalone .exe (Windows)

1. Double-click `build_exe.bat`  
   **or** run it from a command prompt:

   ```bat
   build_exe.bat
   ```

2. When it finishes, the single-file executable is here:

   ```
   dist\ColdShift.exe
   ```

You can copy `ColdShift.exe` to any Windows PC and run it. No Python or extra installs required.

## Status

Early stage — measurement and classification layer first. Offload actions coming next.

## Platform

Windows first (tested). Linux/macOS should work from source with psutil.

## Next Steps

- Better classification rules and user protect/offload lists
- Safe offload actions (working set trim, suspend)
- Config persistence
- Tray icon / background mode
