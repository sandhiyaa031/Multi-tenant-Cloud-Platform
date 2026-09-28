FROM python:3.11-slim

WORKDIR /app

# Install native dependencies required for psycopg3 and other libraries if needed
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the backend code, including worker.py and app module
COPY . /app/

# Environment variables for postgres connection, defaulting to the docker network aliases
ENV PG_HOST=postgres
ENV PG_PORT=5432
ENV PG_USER=postgres
ENV PG_PASSWORD=postgres
ENV PG_DB=postgres
ENV DB_ROLE=dbpilot_app

# The worker script runs in the foreground explicitly
CMD ["python", "-u", "worker.py"]
