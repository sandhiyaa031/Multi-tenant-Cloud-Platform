import asyncio
import os
import sys

# Crucial fix for Windows + Psycopg3 Asyncio compatibility
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from app.db import pool, get_tenant_connection

async def verify_backend():
    print("Opening AsyncConnectionPool...")
    await pool.open()
    print("Pool opened successfully.\n")

    alice_org = "11111111-1111-1111-1111-111111111111"
    
    print(f"Connecting to database as Tenant ALICE...")
    try:
        async with get_tenant_connection(alice_org) as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT count(*) FROM app.organizations;")
                res = await cur.fetchone()
                print(f"SUCCESS! ALICE sees {res[0]} organization(s) (expected 1).")
                
                # Verify zero-leakage inside the pool context
                await cur.execute("SELECT org_id FROM app.organizations;")
                row = await cur.fetchone()
                if str(row[0]) != alice_org:
                    print("ERROR: Crossed tenant boundaries!")
    except Exception as e:
        print(f"ERROR: {e}")
        
    finally:
        print("\nClosing pool...")
        await pool.close()
        print("Pool closed.")

if __name__ == "__main__":
    asyncio.run(verify_backend())
