FROM postgres:18

# HypoPG provides hypothetical indexes for the planner what-if tier (T1).
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-18-hypopg \
    && rm -rf /var/lib/apt/lists/*

COPY initdb/ /docker-entrypoint-initdb.d/
COPY replica-entrypoint.sh /usr/local/bin/replica-entrypoint.sh
RUN chmod +x /usr/local/bin/replica-entrypoint.sh /docker-entrypoint-initdb.d/*.sh
