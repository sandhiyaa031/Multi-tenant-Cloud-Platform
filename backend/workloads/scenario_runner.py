import asyncio
import aiohttp
import time
import argparse

# Config
API_URL = "http://localhost:8000"
ANALYTICAL_WORKER_COUNT = 60
INTERACTIVE_WORKER_COUNT = 15

async def get_token() -> str:
    """Acquires a valid JWT for Org A to authorize the siege load."""
    async with aiohttp.ClientSession() as session:
        resp = await session.post(f"{API_URL}/api/auth/dev_login", json={"tenant_name": "org_a"})
        data = await resp.json()
        return data["token"]

async def run_interactive_client(client_id: int, token: str):
    """
    Simulates a standard Cloud GUI Analyst.
    Fires off a request every ~200ms. We expect these to remain <100ms response time.
    """
    headers = {"Authorization": f"Bearer {token}"}
    async with aiohttp.ClientSession(headers=headers) as session:
        for i in range(100):
            start = time.perf_counter()
            try:
                # Target one of the highly active honeypot scanner IPs present in the Zeek data
                async with session.get(f"{API_URL}/api/investigate?ip=220.158.217.20", timeout=5.0) as resp:
                    status = resp.status
            except Exception as e:
                pass
            await asyncio.sleep(0.2)

async def run_analytical_client(client_id: int, initial_delay: float, token: str):
    """
    Simulates the Cloud Auto-Scale Fan-Out.
    Waits for the fan-out wave, then aggressively hammers heavy queries.
    """
    await asyncio.sleep(initial_delay)
    print(f"[SCALE] Analytical Container {client_id} booted. Hitting DB.")
    
    headers = {"Authorization": f"Bearer {token}"}
    async with aiohttp.ClientSession(headers=headers) as session:
        start = time.perf_counter()
        try:
            async with session.get(f"{API_URL}/api/aggregate?hours=24", timeout=60.0) as resp:
                status = resp.status
                latency = time.perf_counter() - start
                print(f"[AGG {client_id}] Resolved in {latency:.1f}s | Status {status}")
        except Exception as e:
            print(f"[AGG {client_id}] Failed: {str(e)}")

async def simulate_storm():
    print("=== STARTING CONNECTION STORM SIMULATION ===")
    
    try:
        token = await get_token()
    except Exception:
        print("ERROR: Could not get JWT Auth. Is the FastAPI server running?")
        return
        
    tasks = []
    # 1. Start baseline Interactive workload (steady state analysts)
    for i in range(INTERACTIVE_WORKER_COUNT):
        tasks.append(asyncio.create_task(run_interactive_client(i, token)))
        
    # 2. Wait 3 seconds to establish healthy L_slo baseline history
    print("Establishing baseline...")
    await asyncio.sleep(3)
    
    # 3. Trigger Fan-Out! (Auto-scaler kicking in)
    # 60 analytical queries spawned over 5 seconds (12 per second)
    print("!!! CLOUD AUTOSCALE FAN-OUT TRIGGERED !!!")
    for a in range(ANALYTICAL_WORKER_COUNT):
        delay = (a / ANALYTICAL_WORKER_COUNT) * 5.0
        tasks.append(asyncio.create_task(run_analytical_client(a, initial_delay=delay, token=token)))
        
    await asyncio.gather(*tasks)
    print("=== SIMULATION COMPLETE ===")

if __name__ == "__main__":
    asyncio.run(simulate_storm())
