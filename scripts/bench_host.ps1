# Host-side helper of scripts/run_benchmark.sh. It does two things for as long as a run lasts:
#   - asks Windows not to sleep while this process is alive (a per-process request; no power
#     setting is changed, and the request ends with the process);
#   - writes the power source to a CSV every 5 seconds (epoch,AC|BATTERY), so that a repetition
#     during which the charger dropped out can be found and excluded.
# It stops when the stop file appears.
param(
    [Parameter(Mandatory = $true)][string]$Out,
    [Parameter(Mandatory = $true)][string]$StopFile
)

Add-Type -Namespace Bench -Name Power -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("kernel32.dll")]
public static extern uint SetThreadExecutionState(uint esFlags);
'@
# ES_CONTINUOUS | ES_SYSTEM_REQUIRED: stay awake until this thread says otherwise or exits.
[void][Bench.Power]::SetThreadExecutionState([uint32]"0x80000001")

"epoch,source" | Out-File -FilePath $Out -Encoding ascii
while (-not (Test-Path $StopFile)) {
    $status = (Get-CimInstance Win32_Battery).BatteryStatus
    $source = if ($null -eq $status -or $status -eq 2) { "AC" } else { "BATTERY" }
    $epoch = [math]::Round(([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()) / 1000.0, 1)
    "$epoch,$source" | Out-File -FilePath $Out -Encoding ascii -Append
    Start-Sleep -Seconds 5
}
[void][Bench.Power]::SetThreadExecutionState([uint32]"0x80000000")
