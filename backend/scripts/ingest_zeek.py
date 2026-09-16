import asyncio
import gzip
import json
import os
import glob
import time
import zipfile
import shutil
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv
import psycopg

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / "backend" / ".env")

PG_PASSWORD = os.getenv("PG_PASSWORD", "postgres")
PG_USER     = os.getenv("PG_USER", "postgres")
PG_HOST     = os.getenv("PG_HOST", "localhost")
PG_PORT     = os.getenv("PG_PORT", "5432")
PG_DB       = os.getenv("PG_DB", "postgres")
CONN_STR    = f"postgresql://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{PG_DB}"

CREATE_SCHEMA_SQL = """
CREATE SCHEMA IF NOT EXISTS research;
CREATE TABLE IF NOT EXISTS research.ctu_conn_log (
    uid TEXT PRIMARY KEY,
    ts TIMESTAMPTZ NOT NULL,
    id_orig_h INET NOT NULL,
    id_orig_p INT NOT NULL,
    id_resp_h INET NOT NULL,
    id_resp_p INT NOT NULL,
    proto TEXT,
    service TEXT,
    duration DOUBLE PRECISION,
    orig_ip_bytes BIGINT,
    resp_ip_bytes BIGINT,
    conn_state TEXT,
    orig_pkts BIGINT,
    resp_pkts BIGINT,
    source_geo TEXT,
    source_date DATE,
    ingestion_batch TEXT
);
CREATE INDEX IF NOT EXISTS idx_ctu_conn_orig_h_ts ON research.ctu_conn_log(id_orig_h, ts);
"""

def parse_record(line: str, geo: str, date_str: str, batch: str):
    try:
        record = json.loads(line)
        ts_raw = record.get('ts')
        ts = datetime.fromtimestamp(float(ts_raw), tz=timezone.utc) if ts_raw else None
        uid = record.get('uid')
        if not ts or not uid: return None

        duration = record.get('duration')
        duration = float(duration) if duration is not None else None
        
        return (
            uid, ts,
            record.get('id.orig_h', '0.0.0.0'),
            int(record.get('id.orig_p', 0)),
            record.get('id.resp_h', '0.0.0.0'),
            int(record.get('id.resp_p', 0)),
            record.get('proto'),
            record.get('service'),
            duration,
            record.get('orig_ip_bytes'),
            record.get('resp_ip_bytes'),
            record.get('conn_state'),
            record.get('orig_pkts'),
            record.get('resp_pkts'),
            geo, date_str, batch
        )
    except Exception:
        return None

def extract_subset(geo_name):
    print(f"Extracting {geo_name} all days from ZIP...")
    zip_paths = glob.glob(str(PROJECT_ROOT / "dataset" / "**" / f"Honeypot-Cloud-DigitalOcean-{geo_name}.zip"), recursive=True)
    if not zip_paths:
        raise Exception(f"{geo_name} ZIP file not found!")
    
    zip_path = zip_paths[0]
    out_dir = PROJECT_ROOT / "dataset" / f"tmp_{geo_name.lower()}"
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    extracted_files = []
    with zipfile.ZipFile(zip_path, 'r') as z:
        for info in z.infolist():
            if "conn." in info.filename and "log.gz" in info.filename and "conn-summary" not in info.filename:
                # IMPORTANT: Skip Mac OS X resource forks (._conn.log.gz)
                if "/._" in info.filename or "__MACOSX" in info.filename:
                    continue
                filename = Path(info.filename).name
                if filename.startswith("._"):
                    continue
                if info.file_size == 0:
                    continue
                
                # Derive date for attribution
                date_str = ""
                for part in Path(info.filename).parts:
                    if part.startswith("2024-"):
                        date_str = part
                        break
                
                if not date_str:
                    continue

                dest_name = f"{date_str}_{filename}".replace(":", "_")
                dest_path = out_dir / dest_name
                
                with z.open(info.filename) as source, open(dest_path, "wb") as target:
                    shutil.copyfileobj(source, target)
                extracted_files.append((dest_path, date_str))
                
    print(f"Extracted {len(extracted_files)} matching conn files to {out_dir}")
    return extracted_files, out_dir

async def ingest_targeted_geos():
    geos_to_ingest = ["Geo-1", "Geo-2", "Geo-3", "Geo-4"]
    
    # 1. Database connect
    print("Connecting to database...")
    conn = await psycopg.AsyncConnection.connect(CONN_STR)
    async with conn.cursor() as cur:
        await cur.execute(CREATE_SCHEMA_SQL)
        await conn.commit()
    
    total_parsed_all = 0
    total_rejected_all = 0
    total_duplicates_all = 0
    start_time_all = time.time()

    for geo_name in geos_to_ingest:
        print(f"\\n--- Processing {geo_name} ---")
        # Extract files
        files_to_process, out_dir = extract_subset(geo_name)
        
        # Idempotency guard
        print(f"Running idempotency cleanup for {geo_name}...")
        async with conn.cursor() as cur:
            await cur.execute(f"DELETE FROM research.ctu_conn_log WHERE source_geo = '{geo_name}'")
            await conn.commit()
            
        # Ingest
        batch_id = datetime.now().isoformat()
        total_parsed = 0
        
        print(f"Ingesting {len(files_to_process)} chunks for {geo_name}...")
        async with conn.cursor() as cur:
            for filepath, date_str in files_to_process:
                try:
                    async with cur.copy("""
                        COPY research.ctu_conn_log (
                            uid, ts, id_orig_h, id_orig_p, id_resp_h, id_resp_p,
                            proto, service, duration, orig_ip_bytes, resp_ip_bytes,
                            conn_state, orig_pkts, resp_pkts, source_geo, source_date, ingestion_batch
                        ) FROM STDIN
                    """) as copy:
                        with gzip.open(filepath, 'rt') as gz:
                            for line in gz:
                                line = line.strip()
                                if not line or not line.startswith('{'):
                                    continue
                                row = parse_record(line, geo_name, date_str, batch_id)
                                if row:
                                    await copy.write_row(row)
                                    total_parsed += 1
                                    total_parsed_all += 1
                                else:
                                    total_rejected_all += 1
                except psycopg.errors.UniqueViolation as e:
                    await conn.rollback()
                    total_duplicates_all += 1
                    pass 
                    
            await conn.commit()
        
        print(f"Successfully ingested {total_parsed:,} rows for {geo_name}")
        shutil.rmtree(out_dir)
        print(f"Cleaned up temporary directory {out_dir}")
        
    elapsed = time.time() - start_time_all
    print(f"\\n=== FULL INGESTION COMPLETE ===")
    print(f"Total Rows Inserted : {total_parsed_all:,}")
    print(f"Rows Rejected       : {total_rejected_all:,}")
    print(f"Duplicate Files skip: {total_duplicates_all:,}")
    print(f"Elapsed Time        : {elapsed:.2f}s")
    print(f"Throughput          : {total_parsed_all/max(1, elapsed):.0f} rows/s")
    
    await conn.close()
    print("Database connection closed and temporary files cleaned up.")

if __name__ == '__main__':
    import sys
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(ingest_targeted_geos())
