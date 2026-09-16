import glob
import gzip
import time
from pathlib import Path

def get_unique_files(pattern_filter):
    """Find files in the dataset folder that match the specific substring filters."""
    # We use a broad glob and then python filtering because Windows glob handles ** slowly/poorly
    all_files = glob.glob('dataset/**/conn*.log.gz', recursive=True)
    all_files = [f for f in all_files if "conn-summary" not in f]
    
    filtered_files = []
    for f in all_files:
        if pattern_filter(f):
            filtered_files.append(f)
            
    # Deduplicate extracted files
    unique_files = {}
    for f in filtered_files:
        name = Path(f).name
        # To avoid identical files across different dates (unlikely, but just in case), key by date and name
        parts = Path(f).parts
        date_str = ""
        for p in parts:
            if p.startswith("2024-"):
                date_str = p
                break
        unique_files[date_str + "_" + name] = f
        
    return list(unique_files.values())

def count_lines(files):
    count = 0
    size_bytes = 0
    for f in files:
        size_bytes += Path(f).stat().st_size
        with gzip.open(f, 'rt') as gz:
            for line in gz:
                if line.startswith('{'):
                    count += 1
    return count, size_bytes

print("Scanning candidates...")

# Candidate 1: Geo-1, first 10 days of May (2024-05-01 to 2024-05-10)
days_10 = [f"2024-05-{str(i).zfill(2)}" for i in range(1, 11)]
c1_files = get_unique_files(lambda x: "Geo-1" in x and any(d in x for d in days_10))

# Candidate 2: Geo-1, 30 days (all of May 2024)
days_30 = [f"2024-05-{str(i).zfill(2)}" for i in range(1, 32)]
c2_files = get_unique_files(lambda x: "Geo-1" in x and any(d in x for d in days_30))

# Candidate 3: Geo-1, 65 days (May and June roughly)
days_65 = days_30 + [f"2024-06-{str(i).zfill(2)}" for i in range(1, 31)] + [f"2024-07-{str(i).zfill(2)}" for i in range(1, 6)]
c3_files = get_unique_files(lambda x: "Geo-1" in x and any(d in x for d in days_65))

# Candidate 4: All Geos, 2024-05-10
c4_files = get_unique_files(lambda x: "2024-05-10" in x)

print(f"Files | C1: {len(c1_files)}, C2: {len(c2_files)}, C3: {len(c3_files)}, C4: {len(c4_files)}")

t0 = time.time()
c1_rows, c1_size = count_lines(c1_files)
print(f"Candidate 1 (Geo-1, 10 days) : {c1_rows:,} rows, {c1_size/1e6:.1f} MB (files), took {time.time()-t0:.1f}s")

t0 = time.time()
c4_rows, c4_size = count_lines(c4_files)
print(f"Candidate 4 (All Geo, 1 day) : {c4_rows:,} rows, {c4_size/1e6:.1f} MB (files), took {time.time()-t0:.1f}s")

if c2_files:
    t0 = time.time()
    c2_rows, c2_size = count_lines(c2_files)
    print(f"Candidate 2 (Geo-1, 30 days) : {c2_rows:,} rows, {c2_size/1e6:.1f} MB (files), took {time.time()-t0:.1f}s")
else:
    print("Candidate 2 (Geo-1, 30 days): No files found.")

# Let's count DB size of the existing 12k rows to extrapolate.
import psycopg
import asyncio
import os
from dotenv import load_dotenv

async def verify_db():
    load_dotenv('backend/.env')
    conn = await psycopg.AsyncConnection.connect(
        f"postgresql://{os.getenv('PG_USER')}:{os.getenv('PG_PASSWORD')}@{os.getenv('PG_HOST')}:{os.getenv('PG_PORT')}/{os.getenv('PG_DB')}"
    )
    cur = conn.cursor()
    await cur.execute("SELECT pg_total_relation_size('research.ctu_conn_log')")
    size_bytes = (await cur.fetchone())[0]
    await conn.close()
    print(f"\\nDB Storage Size for existing 12k rows: {size_bytes / 1024:.1f} KB")

import sys
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
asyncio.run(verify_db())
