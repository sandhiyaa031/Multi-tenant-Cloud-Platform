#!/usr/bin/env bash
# Creates .env from .env.example with random secrets, for a machine that has none.
#
#   scripts/make_env.sh
#
# It never overwrites an existing .env. The demo administrator's password is generated too and printed
# once, because it is how you sign in; every other secret is written to .env and not shown.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -e .env ]; then
  echo ".env already exists; leaving it alone. Delete it first if you really want a new one (that also needs fresh data volumes)." >&2
  exit 1
fi

random() {   # <bytes> -> URL-safe random text
  if command -v python3 >/dev/null 2>&1; then python3 -c "import secrets,sys; print(secrets.token_urlsafe(int(sys.argv[1])))" "$1"
  elif command -v python >/dev/null 2>&1; then python -c "import secrets,sys; print(secrets.token_urlsafe(int(sys.argv[1])))" "$1"
  else openssl rand -base64 "$1" | tr '+/' '-_' | tr -d '=\n'; echo; fi
}

admin_password="$(random 15)"
umask 077
while IFS= read -r line; do
  case "$line" in
    JWT_SECRET=*)          echo "JWT_SECRET=$(random 48)" ;;
    DEMO_ADMIN_PASSWORD=*) echo "DEMO_ADMIN_PASSWORD=${admin_password}" ;;
    *PASSWORD=*|TWIN_TOKEN=*)
      name="${line%%=*}"
      echo "${name}=$(random 24)" ;;
    *) echo "$line" ;;
  esac
done < .env.example > .env

chmod 600 .env 2>/dev/null || true
echo ".env written."
echo "Sign in at http://localhost:5173 as admin@demo.dbpilot.dev with password: ${admin_password}"
echo "It is also in .env as DEMO_ADMIN_PASSWORD. .env is git-ignored; do not commit or share it."
