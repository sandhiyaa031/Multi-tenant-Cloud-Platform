param([string]$Out, [string]$Stop, [int]$MaxSeconds = 4000)
# Read-only host telemetry for the P0.1h soak. One JSON line every ~2 s. Changes nothing.
Add-Type -AssemblyName System.Windows.Forms
$want = @(
  '\Processor Information(*)\Actual Frequency',
  '\Processor Information(*)\% Performance Limit',
  '\Processor Information(*)\Performance Limit Flags',
  '\Processor Information(_Total)\% Processor Time',
  '\Thermal Zone Information(*)\Temperature',
  '\Thermal Zone Information(*)\High Precision Temperature',
  '\Thermal Zone Information(*)\Throttle Reasons',
  '\Power Meter(*)\Power',
  '\Hyper-V Hypervisor Logical Processor(*)\% Idle Time',
  '\Hyper-V Hypervisor Virtual Processor(*)\% Total Run Time',
  '\Hyper-V Hypervisor Root Virtual Processor(*)\% Total Run Time'
)
$ok = @()
foreach ($c in $want) { try { $null = Get-Counter -Counter $c -ErrorAction Stop; $ok += $c } catch { Add-Content -Path ($Out + '.missing') -Value $c } }
$start = Get-Date; $n = 0
Get-Counter -Counter $ok -SampleInterval 2 -Continuous | ForEach-Object {
  $n++
  $row = [ordered]@{ t = $_.Timestamp.ToUniversalTime().ToString('o') }
  $ps = [System.Windows.Forms.SystemInformation]::PowerStatus
  $row.ac = [string]$ps.PowerLineStatus; $row.batt = [math]::Round($ps.BatteryLifePercent * 100)
  $c = [ordered]@{}
  foreach ($s in $_.CounterSamples) {
    $p = $s.Path -replace '^\\\\[^\\]+', ''
    $c[$p] = [math]::Round($s.CookedValue, 2)
  }
  $row.c = $c
  if ($n % 5 -eq 1) {
    $os = Get-CimInstance Win32_OperatingSystem
    $row.freeMB = [math]::Round($os.FreePhysicalMemory / 1KB)
    $vm = Get-Process vmmemWSL -ErrorAction SilentlyContinue; if ($vm) { $row.vmmemMB = [math]::Round($vm.WorkingSet64 / 1MB) }
  }
  if ($n % 5 -eq 1) {
    try { $b = Get-CimInstance -Namespace root\wmi -ClassName BatteryStatus -ErrorAction Stop; $row.battW = [ordered]@{ online = $b.PowerOnline; chargeMW = $b.ChargeRate; dischargeMW = $b.DischargeRate } } catch {}
  }
  ($row | ConvertTo-Json -Compress -Depth 5) | Add-Content -Path $Out -Encoding ASCII
  if ((Test-Path $Stop) -or (((Get-Date) - $start).TotalSeconds -gt $MaxSeconds)) { exit 0 }
}
