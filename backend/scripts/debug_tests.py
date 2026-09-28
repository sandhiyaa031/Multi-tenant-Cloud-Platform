import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/../..")
sys.path.insert(0, "./backend/")
from tests import test_replica_routing as trr
import traceback

print("1. Connectivity")
try: trr.test_replica_connectivity()
except Exception: traceback.print_exc()

print("2. Lag")
try: trr.test_lag_measurement()
except Exception: traceback.print_exc()

print("3. Routing budget")
try: trr.test_routing_within_budget()
except Exception: traceback.print_exc()

print("4. Stale fallback")
try: trr.test_stale_fallback()
except Exception: traceback.print_exc()

print("5. Primary fallback")
try: trr.test_primary_fallback_unavailable()
except Exception: traceback.print_exc()

print("6. Tenant isolation")
try: trr.test_tenant_isolation_on_replica()
except Exception: traceback.print_exc()

print("7. Read only")
try: trr.test_replica_read_only()
except Exception: traceback.print_exc()

print("DONE ALL")
