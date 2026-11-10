#!/usr/bin/env python3
"""
coldshift_drives.py
Drive detection, speed testing, and page file placement for ColdShift.

Windows pages offloaded memory to the page file, and the page file's
location is a system-wide setting: it cannot be chosen per process. This
module finds the drives, measures how fast they are at the kind of reads
paging needs (small random reads), recommends where the page file should
live, and can apply that choice.

Detection, testing, and recommending are read-only (the speed test writes
one temporary file and deletes it). apply_plan() and restore_backup()
CHANGE WINDOWS SETTINGS: they need administrator rights, take effect after
a restart, and save the original settings first so they can be restored.
"""

import json
import mmap
import os
import random
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import psutil

from coldshift_actions import IS_WIN, app_dir, log_action

FAST = "Fast"
OK = "OK"
SLOW = "Too slow"
UNKNOWN = "Not tested"

# Small random reads per second, one at a time (what paging-in looks like).
FAST_IOPS = 5000
OK_IOPS = 1000

BENCH_FILE_MB = 256
BENCH_SECONDS = 2.0
BLOCK = 4096

MIN_PAGEFILE_MB = 1024
MIN_TOTAL_MB = 4096
MAX_TOTAL_MB = 65536
SYSTEM_STUB_MB = 1024          # small page file kept on C: for crash dumps
KEEP_FREE_FRACTION = 0.15      # never plan into the last 15% of a drive...
KEEP_FREE_MB = 20 * 1024       # ...or the last 20 GB, whichever is larger
KEEP_FREE_MAX_MB = 50 * 1024   # but never hold back more than 50 GB

_NO_WINDOW = 0x08000000 if IS_WIN else 0


@dataclass
class Drive:
    letter: str
    model: str = ""
    bus: str = ""            # NVMe, SATA, USB, ...
    media: str = ""          # SSD, HDD, or "" when Windows doesn't say
    size_gb: float = 0.0
    free_gb: float = 0.0
    fs: str = ""
    system: bool = False
    read_iops: Optional[float] = None
    write_mbps: Optional[float] = None
    test_note: str = ""

    @property
    def kind(self) -> str:
        parts = [p for p in (self.bus, self.media) if p and p != "Unspecified"]
        return " ".join(parts) or "Unknown"

    @property
    def removable(self) -> bool:
        return self.bus.upper() in {"USB", "SD", "MMC", "1394"}

    @property
    def rating(self) -> str:
        if self.read_iops is not None:
            if self.read_iops >= FAST_IOPS:
                return FAST
            return OK if self.read_iops >= OK_IOPS else SLOW
        if self.media.upper() == "HDD":
            return SLOW
        return UNKNOWN

    @property
    def usable_mb(self) -> int:
        """Space we are willing to plan a page file into."""
        free_mb = self.free_gb * 1024
        reserve = min(max(self.size_gb * 1024 * KEEP_FREE_FRACTION, KEEP_FREE_MB),
                      KEEP_FREE_MAX_MB)
        return max(0, int(free_mb - reserve))

    def blocked_reason(self) -> str:
        """Why this drive can't hold the page file ('' if it can)."""
        if self.removable:
            return "removable drive"
        if self.fs and self.fs.upper() != "NTFS":
            return f"{self.fs} file system (needs NTFS)"
        if self.rating == SLOW:
            return "too slow for paging"
        if self.rating == UNKNOWN and self.media.upper() != "SSD" \
                and self.bus.upper() != "NVME":
            return "unknown speed: run the speed test first"
        if self.usable_mb < MIN_PAGEFILE_MB:
            return "not enough free space"
        return ""

    def speed_text(self) -> str:
        if self.read_iops is None:
            return self.test_note or "-"
        text = f"{self.read_iops:,.0f} reads/s"
        if self.write_mbps:
            text += f", {self.write_mbps:,.0f} MB/s write"
        return text


@dataclass
class PageFileInfo:
    automatic: bool = True
    settings: List[dict] = field(default_factory=list)  # name, initial, maximum
    usage: List[dict] = field(default_factory=list)     # name, allocated, current, peak
    known: bool = False

    @property
    def peak_mb(self) -> int:
        return sum(int(u.get("peak") or 0) for u in self.usage)

    def describe(self) -> str:
        if not self.known:
            return "Current page file: could not be read on this system."
        if not self.usage:
            return "Current page file: none."
        parts = [f"{u['name']} ({int(u.get('allocated') or 0):,} MB, "
                 f"peak use {int(u.get('peak') or 0):,} MB)" for u in self.usage]
        mode = "managed by Windows" if self.automatic else "custom"
        return f"Current page file ({mode}): " + "; ".join(parts)


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------

_DETECT_PS = r"""
$ErrorActionPreference = 'SilentlyContinue'
$sys = $env:SystemDrive.TrimEnd(':')
$pds = @(Get-PhysicalDisk)
$drives = foreach ($p in (Get-Partition | Where-Object { "$($_.DriveLetter)" -match '^[A-Za-z]$' })) {
    $d  = Get-Disk -Number $p.DiskNumber
    $pd = $pds | Where-Object { "$($_.DeviceId)" -eq "$($p.DiskNumber)" } | Select-Object -First 1
    $v  = Get-Volume -DriveLetter $p.DriveLetter
    [pscustomobject]@{
        letter  = "$($p.DriveLetter)".ToUpper()
        model   = "$($d.FriendlyName)"
        bus     = "$($d.BusType)"
        media   = "$($pd.MediaType)"
        size_gb = [math]::Round($v.Size / 1GB, 1)
        free_gb = [math]::Round($v.SizeRemaining / 1GB, 1)
        fs      = "$($v.FileSystem)"
        system  = ("$($p.DriveLetter)" -eq $sys)
    }
}
$cs = Get-CimInstance Win32_ComputerSystem
$settings = @(Get-CimInstance Win32_PageFileSetting | ForEach-Object {
    @{ name = $_.Name; initial = $_.InitialSize; maximum = $_.MaximumSize } })
$usage = @(Get-CimInstance Win32_PageFileUsage | ForEach-Object {
    @{ name = $_.Name; allocated = $_.AllocatedBaseSize; current = $_.CurrentUsage; peak = $_.PeakUsage } })
[pscustomobject]@{
    drives   = @($drives)
    pagefile = @{ automatic = [bool]$cs.AutomaticManagedPagefile; settings = $settings; usage = $usage }
} | ConvertTo-Json -Depth 5 -Compress
"""


def _run_powershell(args: List[str], timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["powershell", "-NoProfile", "-NonInteractive"] + args,
                          capture_output=True, text=True, timeout=timeout,
                          creationflags=_NO_WINDOW)


def _as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _fallback_drives() -> List[Drive]:
    """psutil-only listing: no model or drive type."""
    drives = []
    for part in psutil.disk_partitions(all=False):
        try:
            use = psutil.disk_usage(part.mountpoint)
        except OSError:
            continue
        letter = part.mountpoint[:1].upper() if IS_WIN else part.mountpoint
        system = (IS_WIN and letter == os.environ.get("SystemDrive", "C:")[:1].upper())
        drives.append(Drive(letter=letter, size_gb=round(use.total / 1024**3, 1),
                            free_gb=round(use.free / 1024**3, 1),
                            fs=part.fstype.upper(), system=system,
                            bus="USB" if "removable" in part.opts else ""))
    return drives


def detect() -> Tuple[List[Drive], PageFileInfo]:
    """Find fixed drives and the current page file setup."""
    if IS_WIN:
        try:
            proc = _run_powershell(["-Command", _DETECT_PS])
            data = json.loads(proc.stdout)
            drives = []
            seen = set()
            for d in _as_list(data.get("drives")):
                letter = str(d.get("letter", "")).upper()
                if not re.fullmatch(r"[A-Z]", letter) or letter in seen:
                    continue
                seen.add(letter)
                drives.append(Drive(
                    letter=letter, model=str(d.get("model") or ""),
                    bus=str(d.get("bus") or ""), media=str(d.get("media") or ""),
                    size_gb=float(d.get("size_gb") or 0),
                    free_gb=float(d.get("free_gb") or 0),
                    fs=str(d.get("fs") or ""), system=bool(d.get("system"))))
            pf = data.get("pagefile") or {}
            info = PageFileInfo(automatic=bool(pf.get("automatic")),
                                settings=_as_list(pf.get("settings")),
                                usage=_as_list(pf.get("usage")), known=True)
            if drives:
                return sorted(drives, key=lambda d: d.letter), info
            return _fallback_drives(), info
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    return _fallback_drives(), PageFileInfo()


# --------------------------------------------------------------------------
# Speed test
# --------------------------------------------------------------------------

def _bench_path(drive: Drive) -> Optional[str]:
    """A writable spot on the drive for the temporary test file."""
    root = f"{drive.letter}:\\" if IS_WIN else drive.letter
    name = f"ColdShift-speedtest-{os.getpid()}.tmp"
    candidates = [root]
    tmp = tempfile.gettempdir()
    if IS_WIN:
        if tmp[:1].upper() == drive.letter:
            candidates.insert(0, tmp)
    else:
        candidates.append(tmp)
    for folder in candidates:
        path = os.path.join(folder, name)
        try:
            with open(path, "wb"):
                pass
            return path
        except OSError:
            continue
    return None


def _random_reads_windows(path: str, size: int, seconds: float) -> float:
    import ctypes
    from ctypes import wintypes

    class _Overlapped(ctypes.Structure):
        _fields_ = [("Internal", ctypes.c_void_p), ("InternalHigh", ctypes.c_void_p),
                    ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
                    ("hEvent", wintypes.HANDLE)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                wintypes.HANDLE]
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                             ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    k32.ReadFile.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]

    generic_read, share_all, open_existing = 0x80000000, 0x7, 3
    no_buffering = 0x20000000   # bypass the Windows file cache
    handle = k32.CreateFileW(path, generic_read, share_all, None, open_existing,
                             no_buffering, None)
    if handle in (None, wintypes.HANDLE(-1).value):
        raise OSError(ctypes.get_last_error(), "could not open the test file")

    buf = mmap.mmap(-1, BLOCK)      # page-aligned, as unbuffered reads require
    cbuf = (ctypes.c_char * BLOCK).from_buffer(buf)
    got = wintypes.DWORD()
    blocks = size // BLOCK
    count = 0
    try:
        start = time.perf_counter()
        while time.perf_counter() - start < seconds:
            offset = random.randrange(blocks) * BLOCK
            ov = _Overlapped(None, None, offset & 0xFFFFFFFF, offset >> 32, None)
            if not k32.ReadFile(handle, cbuf, BLOCK, ctypes.byref(got),
                                ctypes.byref(ov)):
                raise OSError(ctypes.get_last_error(), "read failed")
            count += 1
        elapsed = time.perf_counter() - start
    finally:
        k32.CloseHandle(handle)
        del cbuf
        buf.close()
    return count / elapsed


def _random_reads_posix(path: str, size: int, seconds: float) -> float:
    flags = os.O_RDONLY | getattr(os, "O_DIRECT", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        fd = os.open(path, os.O_RDONLY)   # file system without direct I/O
    buf = mmap.mmap(-1, BLOCK)
    blocks = size // BLOCK
    count = 0
    try:
        start = time.perf_counter()
        while time.perf_counter() - start < seconds:
            os.preadv(fd, [buf], random.randrange(blocks) * BLOCK)
            count += 1
        elapsed = time.perf_counter() - start
    finally:
        os.close(fd)
        buf.close()
    return count / elapsed


def speed_test(drive: Drive, file_mb: int = BENCH_FILE_MB,
               seconds: float = BENCH_SECONDS) -> Drive:
    """Measure small random reads and sequential write. Updates the drive."""
    drive.read_iops = drive.write_mbps = None
    if drive.removable:
        drive.test_note = "skipped (removable)"
        return drive
    if drive.free_gb * 1024 < file_mb * 4:
        drive.test_note = "skipped (low free space)"
        return drive
    path = _bench_path(drive)
    if path is None:
        drive.test_note = "could not write a test file"
        return drive
    try:
        chunk = os.urandom(8 * 1024 * 1024)
        size = file_mb * 1024 * 1024
        start = time.perf_counter()
        with open(path, "wb") as f:
            written = 0
            while written < size:
                f.write(chunk)
                written += len(chunk)
            f.flush()
            os.fsync(f.fileno())
        drive.write_mbps = round(file_mb / max(time.perf_counter() - start, 1e-6), 1)

        reader = _random_reads_windows if IS_WIN else _random_reads_posix
        drive.read_iops = round(reader(path, size, seconds), 0)
        drive.test_note = ""
    except OSError as exc:
        drive.read_iops = None
        drive.test_note = f"test failed ({exc.strerror or exc})"
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    return drive


# --------------------------------------------------------------------------
# Recommendation and plan
# --------------------------------------------------------------------------

def needed_total_mb(offloadable_mb: float, peak_pagefile_mb: float) -> int:
    """How much page file the current workload calls for, in MB."""
    demand = max(offloadable_mb, peak_pagefile_mb) * 1.5
    total = max(MIN_TOTAL_MB, demand)
    total = int(-(-total // 1024) * 1024)    # round up to a whole GB
    return min(total, MAX_TOTAL_MB)


def _speed_key(d: Drive):
    order = {FAST: 0, OK: 1, UNKNOWN: 2, SLOW: 3}[d.rating]
    bus = 0 if d.bus.upper() == "NVME" else 1
    return (order, -(d.read_iops or 0), bus, d.letter)


def recommend(drives: List[Drive], needed_mb: int) -> Tuple[List[str], str]:
    """Pick drive letters: the fastest one, plus more only if space runs out."""
    eligible = sorted((d for d in drives if not d.blocked_reason()), key=_speed_key)
    if not eligible:
        return [], ("No drive qualifies for the page file right now. Run the "
                    "speed test, or free up space on an SSD.")
    picked, covered = [], 0
    for d in eligible:
        picked.append(d.letter)
        covered += d.usable_mb
        if covered >= needed_mb:
            break
    gb = needed_mb / 1024
    if len(picked) == 1:
        text = (f"Recommended: a {gb:.0f} GB page file on {picked[0]}: alone. "
                f"It is the fastest suitable drive and has the room, so a "
                f"second page file would add nothing.")
    else:
        text = (f"Recommended: split {gb:.0f} GB across "
                f"{' and '.join(p + ':' for p in picked)}. No single drive has "
                f"enough spare room for what your apps need.")
    if covered < needed_mb:
        text += (f" Even together they only cover {covered / 1024:.0f} GB of "
                 f"the {gb:.0f} GB target.")
    if any(d.rating == UNKNOWN for d in eligible if d.letter in picked):
        text += " Run the speed test to confirm."
    return picked, text


def build_plan(drives: List[Drive], selected: List[str], needed_mb: int,
               keep_system_stub: bool = True) -> List[dict]:
    """Turn a selection into page file entries. Raises ValueError if unsafe."""
    by_letter = {d.letter: d for d in drives}
    chosen = []
    for letter in dict.fromkeys(selected):
        d = by_letter.get(letter)
        if d is None:
            raise ValueError(f"{letter}: is not a known drive")
        reason = d.blocked_reason()
        if reason:
            raise ValueError(f"{letter}: can't hold the page file: {reason}")
        chosen.append(d)
    if not chosen:
        raise ValueError("Select at least one drive")

    # Fastest drives take as much as they can hold; the rest spills over.
    plan, remaining = [], needed_mb
    for d in sorted(chosen, key=_speed_key):
        if remaining <= 0:
            share = MIN_PAGEFILE_MB
        else:
            share = min(remaining, d.usable_mb, MAX_TOTAL_MB)
        share = max(MIN_PAGEFILE_MB, int(share // 256 * 256))
        if share > d.usable_mb:
            raise ValueError(f"{d.letter}: does not have enough free space")
        maximum = max(share, min(share * 2, d.usable_mb, MAX_TOTAL_MB))
        plan.append({"name": f"{d.letter}:\\pagefile.sys", "initial": share,
                     "maximum": int(maximum)})
        remaining -= share

    system = next((d for d in drives if d.system), None)
    if (keep_system_stub and system and system.letter not in
            {p["name"][0] for p in plan}):
        if system.free_gb * 1024 > SYSTEM_STUB_MB * 4:
            plan.append({"name": f"{system.letter}:\\pagefile.sys",
                         "initial": SYSTEM_STUB_MB, "maximum": SYSTEM_STUB_MB,
                         "stub": True})
    validate_plan(plan)
    return plan


def validate_plan(plan: List[dict]) -> None:
    if not plan:
        raise ValueError("The plan has no page file in it")
    seen = set()
    for p in plan:
        if not re.fullmatch(r"[A-Z]:\\pagefile\.sys", str(p.get("name", ""))):
            raise ValueError(f"Bad page file name: {p.get('name')!r}")
        if p["name"] in seen:
            raise ValueError(f"{p['name']} is listed twice")
        seen.add(p["name"])
        initial, maximum = p.get("initial"), p.get("maximum")
        if not (isinstance(initial, int) and isinstance(maximum, int)):
            raise ValueError("Page file sizes must be whole numbers of MB")
        if not (MIN_PAGEFILE_MB <= initial <= maximum <= MAX_TOTAL_MB):
            raise ValueError(f"Bad size for {p['name']}: {initial}-{maximum} MB")
    if sum(p["initial"] for p in plan) < MIN_TOTAL_MB:
        raise ValueError(f"Total page file would be under {MIN_TOTAL_MB} MB")


def describe_plan(plan: List[dict]) -> str:
    lines = []
    for p in plan:
        note = "  (small, kept for crash dumps)" if p.get("stub") else ""
        lines.append(f"{p['name']}: {p['initial']:,} MB, can grow to "
                     f"{p['maximum']:,} MB{note}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Applying (Windows, administrator, takes effect after restart)
# --------------------------------------------------------------------------

_APPLY_TEMPLATE = r"""param([string]$ResultPath)
# Generated by ColdShift. Sets the Windows page file configuration.
$ErrorActionPreference = 'Stop'
$result = @{ ok = $false; error = '' }
$automatic = __AUTOMATIC__
$plan = @(__PLAN__)
try {
    $cs = Get-CimInstance Win32_ComputerSystem
    if ($automatic) {
        Set-CimInstance -InputObject $cs -Property @{ AutomaticManagedPagefile = $true }
    } else {
        if ($cs.AutomaticManagedPagefile) {
            Set-CimInstance -InputObject $cs -Property @{ AutomaticManagedPagefile = $false }
        }
        Get-CimInstance Win32_PageFileSetting | Remove-CimInstance
        foreach ($pf in $plan) {
            New-CimInstance -ClassName Win32_PageFileSetting -Property @{ Name = $pf.Name } | Out-Null
            $setting = Get-CimInstance Win32_PageFileSetting | Where-Object { $_.Name -eq $pf.Name }
            Set-CimInstance -InputObject $setting -Property @{
                InitialSize = [uint32]$pf.Initial; MaximumSize = [uint32]$pf.Maximum }
        }
    }
    $result.ok = $true
} catch {
    $result.error = "$_"
    # Never leave the machine without a page file: hand control back to Windows.
    try {
        Set-CimInstance -InputObject (Get-CimInstance Win32_ComputerSystem) `
            -Property @{ AutomaticManagedPagefile = $true }
        $result.error += ' (page file handed back to Windows to manage)'
    } catch { }
}
$result | ConvertTo-Json | Set-Content -Path $ResultPath -Encoding UTF8
"""


def build_apply_script(plan: Optional[List[dict]]) -> str:
    """PowerShell that applies a plan, or restores Windows-managed if None."""
    if plan is None:
        return (_APPLY_TEMPLATE.replace("__AUTOMATIC__", "$true")
                .replace("__PLAN__", ""))
    validate_plan(plan)
    entries = ", ".join(
        f"@{{ Name = '{p['name']}'; Initial = {p['initial']}; Maximum = {p['maximum']} }}"
        for p in plan)
    return (_APPLY_TEMPLATE.replace("__AUTOMATIC__", "$false")
            .replace("__PLAN__", entries))


def is_admin() -> bool:
    if not IS_WIN:
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _ps_quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _run_apply(plan: Optional[List[dict]]) -> Tuple[bool, str]:
    if not IS_WIN:
        return False, "Page file settings can only be changed on Windows."
    script = os.path.join(app_dir(), "apply_pagefile.ps1")
    result = os.path.join(app_dir(), "apply_pagefile_result.json")
    with open(script, "w", encoding="utf-8") as f:
        f.write(build_apply_script(plan))
    if os.path.exists(result):
        os.remove(result)

    try:
        if is_admin():
            _run_powershell(["-ExecutionPolicy", "Bypass", "-File", script,
                             "-ResultPath", result], timeout=120)
        else:
            # Ask Windows for elevation (UAC prompt), then wait for it.
            inner = ", ".join(_ps_quote(a) for a in [
                "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                f'"{script}"', "-ResultPath", f'"{result}"'])
            _run_powershell(["-Command",
                             f"Start-Process powershell -Verb RunAs -Wait "
                             f"-WindowStyle Hidden -ArgumentList {inner}"],
                            timeout=300)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"Could not run the change: {exc}"

    try:
        with open(result, "r", encoding="utf-8-sig") as f:
            outcome = json.load(f)
    except (OSError, ValueError):
        return False, ("Nothing was changed. The administrator prompt was "
                       "declined, or the change could not start.")
    finally:
        for path in (script, result):
            try:
                os.remove(path)
            except OSError:
                pass
    if outcome.get("ok"):
        return True, ""
    return False, str(outcome.get("error") or "unknown error")


def _backup_path() -> str:
    return os.path.join(app_dir(), "pagefile_backup.json")


def has_backup() -> bool:
    return os.path.exists(_backup_path())


def apply_plan(plan: List[dict], current: PageFileInfo) -> Tuple[bool, str]:
    """Apply a page file plan. Saves the original settings the first time."""
    validate_plan(plan)
    if current.known and not has_backup():
        with open(_backup_path(), "w", encoding="utf-8") as f:
            json.dump({"automatic": current.automatic,
                       "settings": current.settings}, f, indent=2)
    ok, error = _run_apply(plan)
    names = ", ".join(f"{p['name']} {p['initial']}-{p['maximum']} MB" for p in plan)
    log_action(f"page file change {'applied' if ok else 'FAILED'}: {names}"
               + (f" ({error})" if error else ""))
    return ok, error


def restore_backup() -> Tuple[bool, str]:
    """Put the page file settings back to what they were before ColdShift."""
    try:
        with open(_backup_path(), "r", encoding="utf-8") as f:
            backup = json.load(f)
    except (OSError, ValueError):
        return False, "No saved original settings were found."

    plan = None
    if not backup.get("automatic"):
        plan = []
        for s in _as_list(backup.get("settings")):
            initial, maximum = int(s.get("initial") or 0), int(s.get("maximum") or 0)
            if initial <= 0 or maximum <= 0:
                plan = None     # "system managed size" entries: let Windows manage
                break
            plan.append({"name": str(s.get("name", "")).replace("/", "\\"),
                         "initial": initial, "maximum": maximum})
        if plan is not None:
            for p in plan:
                p["name"] = p["name"][:1].upper() + p["name"][1:3] + "pagefile.sys"
            try:
                validate_plan(plan)
            except ValueError:
                plan = None
    ok, error = _run_apply(plan)
    if ok:
        try:
            os.remove(_backup_path())
        except OSError:
            pass
    log_action(f"page file restore {'applied' if ok else 'FAILED'}"
               + (f" ({error})" if error else ""))
    return ok, error
