# P0.1h platform soak: decision rules (written and hashed before the run; not to be changed once it starts)

Purpose: find out whether the laptop's effective CPU speed degrades under sustained load of about DBPilot's CPU demand, and
if so whether the evidence points to thermal/power limiting, AC/charger instability, host contention or hybrid-core placement.
Completely separate from DBPilot research experiments: no workload, P0.1e/g, twin, trap or canary run during it. DBPilot's
containers are left untouched and idle. Nothing is changed: no processor limits, cooling, Docker resources, CPU pinning,
Windows settings or hardware. Read-only telemetry only.

## Design
Phases: IDLE 120 s (canaries only) -> SOAK 2100 s = 35 min (5 busy-loop burners on vCPUs 9,10,11,20,21 + the canaries)
-> REST 600 s (canaries only) -> REBURN 300 s (burners again). Total about 52 min. 5 burners + 2 canaries + the idle stack is
about 5.5-6 busy vCPUs (DBPilot at L30-L40 used about 4-5).
Speed canary (two copies, on spare vCPUs 22 and 23): a fixed workload of 500,000 shell-loop iterations (about 1.1 s idle),
timed with /proc/uptime (10 ms resolution), repeated every ~4 s; the log is elapsed milliseconds (higher = slower).
Host telemetry every ~2 s (hostlog.ps1): AC line state and battery %, per-logical-processor Actual Frequency, % Performance
Limit and Performance Limit Flags, thermal-zone temperature and Throttle Reasons, Power Meter, hypervisor per-LP idle time
(placement), VM and root-partition virtual-processor run time (host/non-VM CPU), battery charge/discharge power, free memory
and vmmemWSL working set (every ~10 s).
Start guard: abort (do not start) unless AC is Online and the power-mode overlay is Best performance. Charger wattage is not
exposed by Windows; the user reads it from the adapter label (recorded in the report).
The user is asked to leave the machine idle during the run; any host activity is measured, not prevented.

## Definitions (analysis script analyze.py implements exactly this)
- Baseline = the loaded early state: soak seconds 60-300. Idle speed is reported but is NOT the baseline (turbo and core
  sharing legitimately make a loaded CPU slower than an idle one).
- 60-s windows from soak minute 5 to the end of the soak (minutes 6-35).
- D1 CANARY DEGRADED iff, for either canary, the 60-s median elapsed time exceeds 1.10x the baseline median for at least 3
  consecutive windows. Hysteresis: REBURN median (reburn seconds 60-300) must be within +/-10% of the baseline.
- D2 FREQUENCY/LIMIT: mean Actual Frequency of the busy logical processors (hypervisor busy > 50%) falls more than 7% below
  its baseline for >= 3 consecutive windows; OR performance-limit evidence appears (share of busy-LP samples with
  % Performance Limit < 100 or Limit Flags != 0 reaches >= 25% when the baseline was < 10%, OR any thermal Throttle Reason
  is non-zero) for >= 3 consecutive windows. Temperature and power are reported alongside.
- D3 AC STABLE iff every AC sample during the whole run is Online (any non-Online sample is reported with its time).
- D4 HOST CONTENTION CHANGED iff the root-partition (non-VM) CPU rises >= 0.75 core-equivalents above its baseline for >= 3
  consecutive windows.
- D5 PLACEMENT CHANGED iff the share of busy logical-processor time on SMT-sibling threads plus E-cores shifts by more than 15
  percentage points from baseline for >= 3 consecutive windows.

## Attribution (applied mechanically)
- D1 not degraded -> H_none: the platform does not slow under this synthetic load (the earlier variability is then specific
  to the DBPilot stack or needs DB-like load; report that and do not conclude the hardware is at fault).
- D1 degraded and (D2 frequency or limit) -> thermal/power limiting (thermal if throttle reasons or temperature rise; power/
  adapter if limit flags with AC instability).
- D1 degraded, D2 clean, D4 -> host contention.
- D1 degraded, D2 clean, D4 clean, D5 -> hybrid-core placement.
- D1 degraded, none of D2/D4/D5 -> unexplained by the instrumented factors.
SUITABLE for controlled DBPilot experiments in the current configuration iff D1 not degraded AND D3 AC stable AND hysteresis
within +/-10%. Otherwise not suitable as configured.
A run is reported as is: no repetition or parameter change after it starts. A tool fault is disclosed and the run repeated only
if the fault, not the machine, caused the data loss.
