# Addendum 1 (tool fault, before any measurement)
The first launch (2026-10-06T17:33Z) ran results/p01e/run.py as a script from /results, which puts /results/p01e on
sys.path instead of /srv, so the unchanged workload package could not be imported (ModuleNotFoundError). The run
exited after 3 s having performed NO measurement; its artifacts are kept in results/p01e/toolfault_attempt1/.
Fix: the container invocation in runall.sh gained "-e PYTHONPATH=/srv". Nothing else changed.
runall.sh hash 4a9d85c014f91f0f (listed in criteria.md) is superseded by: d7d88e78c1f77603.
criteria.md itself is unmodified (hash 0b35db4c46511a1e); all criteria, profiles, run.py and analyze.py hashes stand.
Disclosure: the L10 start-state snapshot of attempt 1 was taken, no load was applied; the new L10 run takes its own.
