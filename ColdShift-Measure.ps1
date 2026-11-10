# ColdShift-Measure.ps1
# Selective process offloading - Measurement script
# Lists processes, shows private memory and recent CPU activity, and
# classifies offload candidates.
# Does NOT modify or suspend any processes. Safe to run.

param(
    [int]$MinRAM_MB = 50,          # Only show processes using at least this much RAM
    [int]$TopN = 30,               # Show this many processes
    [switch]$ShowAll,              # Show all processes (ignore MinRAM_MB)
    [double]$IntervalSec = 3       # Seconds to watch CPU activity
)

if ($IntervalSec -le 0) { throw "IntervalSec must be greater than 0" }
if ($TopN -le 0) { throw "TopN must be greater than 0" }

# Known background apps that tolerate being paged out when idle (exact names).
$SafeNames = @(
    'chrome','msedge','firefox','brave','opera','vivaldi',
    'discord','slack','teams','ms-teams','zoom','skype',
    'spotify','vlc','wmplayer','itunes',
    'notepad','wordpad','mspaint','calc','calculatorapp',
    'onedrive','dropbox','googledrivefs',
    'steam','steamwebhelper','epicgameslauncher'
)

# Never offload candidates: OS core, shell, security, VMs, databases.
$CriticalNames = @(
    'system','idle','registry','memory compression','secure system',
    'smss','csrss','wininit','services','lsass','lsaiso','winlogon','dwm',
    'audiodg','fontdrvhost','svchost','spoolsv','conhost','sihost','ctfmon',
    'taskhostw',
    'explorer','searchhost','searchindexer','startmenuexperiencehost',
    'shellexperiencehost','runtimebroker','dllhost','taskmgr',
    'msmpeng','mssense','nissrv','securityhealthservice',
    'securityhealthsystray','smartscreen',
    'vmmem','vmmemwsl','vmwp','vmcompute','vmms','vmware-vmx',
    'virtualboxvm','vboxheadless','com.docker.backend','dockerd',
    'sqlservr','postgres','mysqld','mongod','redis-server','oracle'
)

$IdleCpuPct  = 1.0    # below this (% of one core, recent) counts as idle
$SafeMinMB   = 40
$ReviewMinMB = 80

Write-Host "ColdShift Process Measurement" -ForegroundColor Cyan
Write-Host "============================" -ForegroundColor Cyan
Write-Host "Machine: $env:COMPUTERNAME" -ForegroundColor Gray
Write-Host "Time:    $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" -ForegroundColor Gray
Write-Host ""

# System memory
$os = Get-CimInstance Win32_OperatingSystem
$totalRAM_GB = [math]::Round($os.TotalVisibleMemorySize / 1MB, 2)
$availRAM_GB = [math]::Round($os.FreePhysicalMemory / 1MB, 2)
$usedRAM_GB  = [math]::Round($totalRAM_GB - $availRAM_GB, 2)

Write-Host "System Memory:" -ForegroundColor Yellow
Write-Host "  Total:     $totalRAM_GB GB"
Write-Host "  Used:      $usedRAM_GB GB"
Write-Host "  Available: $availRAM_GB GB"
Write-Host ""

# Foreground window owner
$fgPid = 0
try {
    Add-Type -Namespace ColdShift -Name Win32 -ErrorAction Stop -MemberDefinition @'
[DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
[DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);
'@
    $fgRaw = [uint32]0
    [void][ColdShift.Win32]::GetWindowThreadProcessId([ColdShift.Win32]::GetForegroundWindow(), [ref]$fgRaw)
    $fgPid = [int]$fgRaw
} catch { }

# First CPU sample (cumulative seconds per process)
Write-Host "Watching CPU activity for $IntervalSec s..." -ForegroundColor Gray
$cpuBefore = @{}
foreach ($p in Get-Process) {
    if ($null -ne $p.CPU) { $cpuBefore["$($p.Id)|$($p.StartTime)"] = $p.CPU }
}
$watch = [System.Diagnostics.Stopwatch]::StartNew()
Start-Sleep -Milliseconds ([int]($IntervalSec * 1000))

# Second sample
$processes = Get-Process | Where-Object { $_.Id -ne 0 }
$elapsed = $watch.Elapsed.TotalSeconds

# Private working set per PID (excludes shared pages)
$privateWS = @{}
try {
    Get-CimInstance Win32_PerfRawData_PerfProc_Process -ErrorAction Stop | ForEach-Object {
        $privateWS[[int]$_.IDProcess] = $_.WorkingSetPrivate
    }
} catch { }

$fgName = $null
if ($fgPid -ne 0) {
    $fgProc = $processes | Where-Object { $_.Id -eq $fgPid } | Select-Object -First 1
    if ($fgProc) { $fgName = $fgProc.ProcessName.ToLower() }
}

$processList = foreach ($p in $processes) {
    $name = $p.ProcessName.ToLower()

    $exact = $privateWS.ContainsKey($p.Id)
    $bytes = if ($exact) { $privateWS[$p.Id] } else { $p.WorkingSet64 }
    $ramMB = [math]::Round($bytes / 1MB, 1)
    if (-not $ShowAll -and ($p.WorkingSet64 / 1MB) -lt $MinRAM_MB) { continue }

    # Recent CPU as % of one core. $null = could not be measured.
    $cpuPct = $null
    $key = "$($p.Id)|$($p.StartTime)"
    if ($null -ne $p.CPU -and $cpuBefore.ContainsKey($key) -and $elapsed -gt 0) {
        $delta = [math]::Max(0, $p.CPU - $cpuBefore[$key])
        $cpuPct = [math]::Round($delta / $elapsed * 100, 1)
    }

    $class = ''
    $reason = ''
    if ($CriticalNames -contains $name) {
        $class = 'PROTECT'; $reason = 'system / protected process'
    } elseif ($p.SessionId -eq 0 -or $null -eq $p.CPU -or $p.Id -eq $PID) {
        $class = 'PROTECT'; $reason = 'service or inaccessible process'
    } elseif ($p.Id -eq $fgPid -or ($fgName -and $name -eq $fgName)) {
        $class = 'ACTIVE'; $reason = 'foreground app'
    } elseif ($null -eq $cpuPct) {
        $reason = 'started during measurement'
    } elseif ($cpuPct -ge $IdleCpuPct) {
        $class = 'ACTIVE'; $reason = "busy ($cpuPct% CPU)"
    } elseif ($SafeNames -contains $name) {
        if ($ramMB -ge $SafeMinMB) { $class = 'SAFE'; $reason = 'known background app, idle' }
        else { $reason = 'idle, but too small to matter' }
    } elseif ($ramMB -ge $ReviewMinMB) {
        $class = 'REVIEW'; $reason = 'idle, unknown app: check before offloading'
    }

    [PSCustomObject]@{
        PID    = $p.Id
        Name   = $p.ProcessName
        RAM_MB = $ramMB
        Exact  = $exact
        CPU    = $cpuPct
        Class  = $class
        Reason = $reason
        Rank   = switch ($class) { 'SAFE' { 0 } 'REVIEW' { 1 } 'ACTIVE' { 2 } 'PROTECT' { 4 } default { 3 } }
    }
}

$safeMB   = [math]::Round((($processList | Where-Object Class -eq 'SAFE'   | Measure-Object RAM_MB -Sum).Sum), 1)
$reviewMB = [math]::Round((($processList | Where-Object Class -eq 'REVIEW' | Measure-Object RAM_MB -Sum).Sum), 1)

# Candidates first, then by memory descending
$sorted = $processList |
    Sort-Object -Property @{Expression = 'Rank'; Descending = $false}, @{Expression = 'RAM_MB'; Descending = $true} |
    Select-Object -First $TopN

Write-Host ""
Write-Host "Processes, candidates first:" -ForegroundColor Yellow
Write-Host ("{0,-8} {1,-30} {2,10} {3,7}  {4,-8} {5}" -f "PID", "Name", "Mem_MB", "CPU_%", "Class", "Reason")
Write-Host ("{0,-8} {1,-30} {2,10} {3,7}  {4,-8} {5}" -f "---", "----", "------", "-----", "-----", "------")

foreach ($proc in $sorted) {
    $color = switch ($proc.Class) { 'SAFE' { 'Green' } 'REVIEW' { 'Yellow' } 'PROTECT' { 'DarkGray' } default { 'White' } }
    $memText = if ($proc.Exact) { "$($proc.RAM_MB)" } else { "$($proc.RAM_MB)~" }
    $cpuText = if ($null -eq $proc.CPU) { '-' } else { "$($proc.CPU)" }
    $shortName = if ($proc.Name.Length -gt 30) { $proc.Name.Substring(0, 30) } else { $proc.Name }
    Write-Host ("{0,-8} {1,-30} {2,10} {3,7}  {4,-8} {5}" -f $proc.PID, $shortName, $memText, $cpuText, $proc.Class, $proc.Reason) -ForegroundColor $color
}

Write-Host ""
Write-Host "SAFE   (known background apps, idle): $safeMB MB" -ForegroundColor Green
Write-Host "REVIEW (unknown apps, idle):          $reviewMB MB" -ForegroundColor Yellow
Write-Host ""
Write-Host "Notes:" -ForegroundColor Gray
Write-Host "  - This script only measures. It does not suspend, trim, or kill anything." -ForegroundColor Gray
Write-Host "  - Mem_MB is the private working set. '~' means it could not be read and" -ForegroundColor Gray
Write-Host "    the full working set (which includes shared pages) is shown instead." -ForegroundColor Gray
Write-Host "  - CPU_% is recent use over the watch interval, as % of one core." -ForegroundColor Gray
Write-Host "  - Run with -ShowAll to see every process; -MinRAM_MB 100 -TopN 50 to adjust filters." -ForegroundColor Gray
