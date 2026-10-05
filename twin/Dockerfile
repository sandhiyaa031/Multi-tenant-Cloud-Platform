# The experimentation plane: PostgreSQL binaries identical to the data plane's,
# plus the agent that clones, replays and measures.
FROM postgres:18 AS base

RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-18-hypopg python3 python3-venv util-linux \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/agent \
    && /opt/agent/bin/pip install --no-cache-dir "fastapi>=0.115,<1" "uvicorn>=0.30,<1" \
       "psycopg[binary,pool]>=3.2,<4" "numpy>=2,<3" "pydantic>=2.7,<3"

WORKDIR /srv
COPY twin/agent ./agent
COPY core/dbpilot_core ./dbpilot_core

# Owned by postgres so the named volume mounted here is writable by the agent.
RUN mkdir -p /twin && chown postgres:postgres /twin
USER postgres
ENV PATH="/opt/agent/bin:${PATH}"
EXPOSE 8080
ENTRYPOINT []

# The same image plus a test runner: `docker compose run --rm twin-test`.
FROM base AS test
USER root
RUN /opt/agent/bin/pip install --no-cache-dir "pytest>=8,<9" "httpx>=0.27,<1"
USER postgres
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"]

# Last, so that a plain `docker build` produces the agent and not the test runner.
FROM base AS runtime
CMD ["uvicorn", "agent.app:app", "--host", "0.0.0.0", "--port", "8080"]
