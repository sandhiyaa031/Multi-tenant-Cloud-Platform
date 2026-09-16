import httpx
import asyncio
import time
import uuid

async def test_cloud_flow():
    base_url = "http://127.0.0.1:8000"
    
    async with httpx.AsyncClient() as client:
        print("1. Authenticating as Tenant 'Alice' (or Registering)...")
        # Try registering first
        res = await client.post(f"{base_url}/api/auth/register", json={"org_name": "Alice Corp"})
        if res.status_code == 409:
            # Already exists, just login
            res = await client.post(f"{base_url}/api/auth/login", json={"org_name": "Alice Corp"})
            
        token = res.json()["token"]
        tenant_id = res.json()["org_id"]
        headers = {"Authorization": f"Bearer {token}"}
        
        print(f"-> Logged in successfully. Tenant ID: {tenant_id}")
        
        # 2. Submit a Heavy Analytical Job to trigger the Cloud Elastic Tier
        print("\\n2. Submitting Analytical Payload (hours=72)...")
        res = await client.post(f"{base_url}/api/analytical/submit?hours=72", headers=headers)
        data = res.json()
        print("Response:", data)
        assert data["routing_decision"] == "OFFLOADED", "API Gateway failed to route offload."
        job_id = data["job_id"]
        
        # 3. Poll for Completion
        print(f"\\n3. Polling Job Tracker for {job_id}...")
        while True:
            res = await client.get(f"{base_url}/api/analytical/jobs/{job_id}", headers=headers)
            status = res.json()
            print(f"[{time.strftime('%X')}] Status: {status['status']}")
            
            if status['status'] in ['COMPLETED', 'FAILED']:
                print("\\nFINAL CLOUD JOB RESULT:")
                for k, v in status.items():
                    print(f"{k}: {v}")
                break
                
            await asyncio.sleep(1.0)
            
if __name__ == "__main__":
    if __import__("sys").platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(test_cloud_flow())
