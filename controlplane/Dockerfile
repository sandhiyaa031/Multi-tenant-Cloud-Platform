FROM python:3.12-slim

WORKDIR /srv
COPY controlplane/requirements.txt controlplane/requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements-dev.txt

COPY controlplane/ .
# Shared with the twin agent: the typed action space and the safety gate.
COPY core/dbpilot_core ./dbpilot_core
COPY core/tests ./core_tests

RUN useradd --system --no-create-home dbpilot
USER dbpilot

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
