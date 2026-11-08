# ColdShift-Measure.ps1
# Selective process offloading - Measurement script
# Lists processes, shows memory usage, and highlights potential offload candidates.
# Does NOT modify or suspend any processes. Safe to run.

param(
    [int]$MinRAM_MB = 50,          # Only show processes using at least this much RAM
    [int]$TopN = 30,               # Show top N processes by memory
    [switch]$ShowAll               # Show all processes (ignore MinRAM_MB)
)

Write-Host "ColdShift Process Measurement" -ForegroundColor Cyan
Write-Host "============================" -ForegroundColor Cyan
Write-Host "Machine: $env:COMPUTERNAME" -ForegroundColor Gray
Write-Host "Time:    $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" -ForegroundColor Gray
Write-Host ""

# Get all processes with relevant properties
$processes = Get-Process | Where-Object { $_.Id -ne 0 } | Select-Object -Property Id, ProcessName, WorkingSet64, CPU, StartTime, Responding

# Calculate total physical memory info
$os = Get-CimInstance Win32_OperatingSystem
$totalRAM_GB = [math]::Round($os.TotalVisibleMemorySize / 1MB, 2)
$freeRAM_GB  = [math]::Round($os.FreePhysicalMemory / 1MB, 2)
$usedRAM_GB  = [math]::Round($totalRAM_GB - $freeRAM_GB, 2)

Write-Host "System Memory:" -ForegroundColor Yellow
Write-Host "  Total: $totalRAM_GB GB"
Write-Host "  Used:  $usedRAM_GB GB"
Write-Host "  Free:  $freeRAM_GB GB"
Write-Host ""

# Convert and filter
$processList = foreach ($p in $processes) {
    $ramMB = [math]::Round($p.WorkingSet64 / 1MB, 1)
    if ($ShowAll -or $ramMB -ge $MinRAM_MB) {
        [PSCustomObject]@{
            PID       = $p.Id
            Name      = $p.ProcessName
            RAM_MB    = $ramMB
            CPU_Sec   = if ($p.CPU) { [math]::Round($p.CPU, 1) } else { 0 }
            Responding= $p.Responding
            StartTime = $p.StartTime
        }
    }
}

# Sort by RAM descending and take top N
$sorted = $processList | Sort-Object RAM_MB -Descending | Select-Object -First $TopN

Write-Host "Top Processes by RAM Usage (min $MinRAM_MB MB):" -ForegroundColor Yellow
Write-Host ("{0,-8} {1,-30} {2,10} {3,10} {4}" -f "PID", "Name", "RAM_MB", "CPU_Sec", "Status")
Write-Host ("{0,-8} {1,-30} {2,10} {3,10} {4}" -f "---", "----", "------", "-------", "------")

$totalCandidateRAM = 0
foreach ($proc in $sorted) {
    $status = if ($proc.Responding) { "OK" } else { "NotResp" }
    $color = "White"
    # Simple heuristic: high RAM + low CPU = potential offload candidate
    if ($proc.RAM_MB -ge 100 -and $proc.CPU_Sec -lt 10) {
        $color = "Green"
        $totalCandidateRAM += $proc.RAM_MB
        $status += " *"
    }
    Write-Host ("{0,-8} {1,-30} {2,10} {3,10} {4}" -f $proc.PID, $proc.Name, $proc.RAM_MB, $proc.CPU_Sec, $status) -ForegroundColor $color
}

Write-Host ""
Write-Host "Potential offload candidates (high RAM, low CPU) marked with * in green." -ForegroundColor Green
Write-Host "Estimated RAM in candidate processes: $([math]::Round($totalCandidateRAM, 1)) MB" -ForegroundColor Green
Write-Host ""
Write-Host "Notes:" -ForegroundColor Gray
Write-Host "  - This script only measures. It does not suspend, trim, or kill anything." -ForegroundColor Gray
Write-Host "  - Run with -ShowAll to see every process." -ForegroundColor Gray
Write-Host "  - Run with -MinRAM_MB 100 -TopN 50 to adjust filters." -ForegroundColor Gray
Write-Host "  - Next step: add classification rules and safe offload actions." -ForegroundColor Gray
