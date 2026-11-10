# ColdShift

Selective process offloading app.

**Goal:** Measure running processes, identify good candidates to push out of physical RAM (to the page file on a fast SSD), and keep DDR5 free for intensive workloads.

## Overview

ColdShift helps reclaim physical RAM by:
1. Measuring private memory and recent activity of running processes
2. Classifying them as Protect / Active / Safe / Review
3. Offloading idle background apps on demand or automatically (Focus mode)
4. Keeping high-priority workloads in DDR5

## Current Features

- **CLI measurement**: `ColdShift-Measure.py` (and a PowerShell equivalent, `ColdShift-Measure.ps1`)
- **GUI application**: `ColdShift-GUI.py` (CustomTkinter)
  - Live system memory display
  - Process list with offload candidates first
  - Filters and auto-refresh
  - Focus mode, per-app rules, and growth (possible leak) tracking
- **Shared logic**: `coldshift_core.py` (measurement, classification) and `coldshift_actions.py` (offload actions, settings), `coldshift_drives.py` (drive detection, speed test, page file)
- Nothing is changed until you press **Start Focus** or turn on **Auto focus**. The CLI and the PowerShell script only measure.

## Focus mode

Focus mode offloads every app that is idle and classified `SAFE`, so the RAM goes to the app you are about to use. An app is only picked when all of its processes are idle and none is protected, so a half-busy app is never half-frozen.

| Mode | What it does | Trade-off |
|---|---|---|
| **Gentle** (default) | Lowers the apps' memory priority, so Windows reclaims their pages first, and trims their working sets. | Apps keep running and can pull memory back. |
| **Full** | Also suspends the apps, so the memory stays freed. | Suspended apps are frozen: no notifications, and chat apps may need to reconnect. |

- **End Focus** or closing ColdShift restores everything.
- **Click-to-resume:** switching to an offloaded app's window resumes that app within half a second. Apps that live only in the tray have no window to click, so use End Focus for those.
- **Auto focus:** select the app that needs the RAM, press **Focus app**, and tick **Auto focus**. Focus mode starts when that app is running and ends when it exits.
- **Rules:** select a process and press **Always offload**, **Always protect**, or **Clear rule**. Built-in protection always wins over "Always offload".
- **Crash safety:** every process is recorded in a state file before it is touched. If ColdShift is killed, the next start restores them, or run `python ColdShift-Measure.py --resume-all`.
- **Results:** the before/after available RAM for every action is appended to `actions.log`. Trimmed memory leaves RAM over several seconds, so the first figure is a floor.

Settings, state, and the log live in `%APPDATA%\ColdShift` (`~/.config/coldshift` elsewhere). Suspending apps that run elevated needs ColdShift to run as administrator; otherwise they are skipped. The priority and trim calls are Windows-only.

## Drives and page file

Offloaded memory goes to the Windows page file, so the drive holding the page file decides how fast an offloaded app comes back. **Drives / Page file...** in the GUI:

1. **Detects** every drive with its type (NVMe, SATA SSD, hard disk), free space, and the current page file setup.
2. **Tests speed** on request: small random reads (what paging-in looks like) and sequential write. It writes one temporary 256 MB file per drive and deletes it.
3. **Recommends** a size from how much memory your idle apps hold and the page file's past peak use, then picks the fastest suitable drive. It only suggests a second drive when the first lacks the room.
4. Lets you **tick one drive or several** and shows the exact plan before anything is changed.
5. **Applies** the plan when you confirm. This needs administrator approval (a UAC prompt) and takes effect after a restart. The original settings are saved first, and **Restore original settings** puts them back.

Limits and safeguards:

- Windows chooses which page file each app's memory goes to. ColdShift sets where the page files are, not which app uses which.
- Hard disks, removable drives, non-NTFS drives, and drives short on free space cannot be selected.
- A small page file is kept on the Windows drive by default so crash dumps still work.
- If applying fails part-way, the page file is handed back to Windows to manage, so the PC is never left without one.
- `Restore original settings` recreates custom sizes exactly. If the original was Windows-managed, or used sizes ColdShift's checks reject, it restores to Windows-managed.

## Growth tracking

While the GUI is open, each process's private memory is compared with when it was first seen. A row turns red and shows `LEAK?` once it has been watched for 10 minutes and has grown by at least 150 MB and 30%. Restarting that app is usually the fix. Turn on Auto Refresh so the history keeps building.

## How processes are classified

| Class | Meaning |
|---|---|
| `SAFE` | Known background app (browser, chat, launcher...), idle right now, not in the foreground. Counted in the "safe to offload" total. |
| `REVIEW` | Idle and large, but not on the known list. You decide. Not counted as safe. |
| `ACTIVE` | The foreground app (all of its processes) or anything that used CPU recently. |
| `PROTECT` | OS core, shell, security tools, VMs, databases, and anything running as a system account. Never a candidate. |

Details:

- **Activity is recent CPU use and disk/network I/O**, measured as the change between two samples and shown as a percentage of one core. Cumulative CPU time since launch is not used, because it flags a just-launched game as idle and a long-idle browser as busy.
- **Memory is private memory**, so shared pages are not counted once per process. When it cannot be read (access denied), the working set is shown instead and marked with `~`.
- **Names are matched exactly** (case-insensitive, `.exe` ignored), never by substring.
- The lists and thresholds live at the top of `coldshift_core.py`.

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
python ColdShift-Measure.py --interval 5 --min-ram 100 --top 50
```

Run as administrator to see processes owned by other accounts.

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

Note: unsigned PyInstaller one-file executables are often flagged by antivirus and EDR, and that will get worse once offload actions (suspending and trimming other processes) are added. Plan on code signing, or a one-folder build, before distributing.

## Status

Early stage. Measurement, classification, Focus mode, rules, and growth tracking are in. Detecting audio playback or calls is not: an app that is playing audio is normally skipped because it is using CPU, but that is a proxy, not a guarantee.

## Platform

Windows first. Linux/macOS run from source with psutil, without foreground-window detection.

## Next Steps

- Hard working-set caps for specific apps
- Pressure-aware rules (only act when available memory is low)
- Audio/call detection before suspending
- Startup audit
- Tray icon / background mode
