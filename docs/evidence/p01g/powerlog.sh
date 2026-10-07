#!/bin/bash
# usage: powerlog.sh <level> ; logs AC / battery / power-mode every 20 s until results/p01g/stop_<level> exists. Read-only.
cd "D:/college/PROJECTS-SEM 5/dbpilot/Multi-tenant-Cloud-Platform"
out=results/p01g/power_L$1.log
while [ ! -f results/p01g/stop_$1 ]; do
  line=$(powershell.exe -NoProfile -Command "Add-Type -AssemblyName System.Windows.Forms; \$p=[System.Windows.Forms.SystemInformation]::PowerStatus; \$k=Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Power\User\PowerSchemes'; '{0} line={1} battery={2:N0}% charge={3} overlayAC={4}' -f (Get-Date).ToUniversalTime().ToString('o'), \$p.PowerLineStatus, (\$p.BatteryLifePercent*100), \$p.BatteryChargeStatus, \$k.ActiveOverlayAcPowerScheme" 2>&1 | tr -d '\r')
  echo "$line" >> "$out"
  sleep 20
done
