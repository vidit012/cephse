#!/usr/bin/env bash
set -euo pipefail

# setup_database.sh
# Create a PostgreSQL database, user, and load schema from postgres/schema.sql
# Usage: sudo bash scripts/setup_database.sh [--db-name NAME] [--db-user USER] [--db-pass PASS] [--schema PATH] [--force]

DB_NAME="tiering"
DB_USER="tiering_user"
DB_PASS=""
SCHEMA_PATH="schema.sql"
FORCE=0

print_usage() {
  cat <<EOF
Usage: sudo bash $0 [--db-name NAME] [--db-user USER] [--db-pass PASS] [--schema PATH] [--force]

Defaults:
  --db-name tiering
  --db-user tiering_user
  --schema postgres/schema.sql

Options:
  --db-pass PASS   : Provide DB user password (or set env TIERING_DB_PASS)
  --force          : Drop and recreate the database if it exists

Examples:
  sudo bash $0 --db-pass 'S3cureP@ssw0rd'
  sudo bash $0 --force --db-pass 'S3cureP@ssw0rd' --schema ./postgres/schema.sql
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --db-name) DB_NAME="$2"; shift 2;;
    --db-user) DB_USER="$2"; shift 2;;
    --db-pass) DB_PASS="$2"; shift 2;;
    --schema) SCHEMA_PATH="$2"; shift 2;;
    --force) FORCE=1; shift;;
    -h|--help) print_usage; exit 0;;
    *) echo "Unknown arg: $1"; print_usage; exit 1;;
  esac
done

if [[ -z "${DB_PASS}" ]]; then
  DB_PASS=${TIERING_DB_PASS:-}
fi

if [[ -z "${DB_PASS}" ]]; then
  echo -n "Enter password for new DB user ${DB_USER}: "
  read -s DB_PASS
  echo
fi

if ! command -v psql >/dev/null 2>&1; then
  echo "psql command not found. Install PostgreSQL client tools and retry." >&2
  exit 1
fi

if [[ ! -f "${SCHEMA_PATH}" ]]; then
  echo "Schema file not found at ${SCHEMA_PATH}" >&2
  exit 1
fi

echo "Checking for existing database '${DB_NAME}'..."
DB_EXISTS=$(sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'")

if [[ "${DB_EXISTS}" == "1" ]]; then
  if [[ ${FORCE} -eq 1 ]]; then
    echo "Dropping existing database ${DB_NAME} (force)"
    sudo -u postgres dropdb --if-exists "${DB_NAME}"
  else
    echo "Database ${DB_NAME} already exists. Use --force to replace it or run the script on a fresh system." >&2
    exit 1
  fi
fi

echo "Creating database ${DB_NAME} and role ${DB_USER} (or updating password if role exists)..."

# Create or update role
sudo -u postgres psql -v ON_ERROR_STOP=1 <<SQL
DO
\$do\$
BEGIN
   IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${DB_USER}') THEN
      CREATE ROLE ${DB_USER} LOGIN PASSWORD '${DB_PASS}';
   ELSE
      ALTER ROLE ${DB_USER} WITH PASSWORD '${DB_PASS}';
   END IF;
END
\$do\$;
SQL

echo "Creating database owned by ${DB_USER}..."
sudo -u postgres createdb -O "${DB_USER}" "${DB_NAME}" || true

echo "Loading schema from ${SCHEMA_PATH} into ${DB_NAME} (single transaction, will abort on error)..."
sudo -u postgres psql -v ON_ERROR_STOP=1 --single-transaction -f "${SCHEMA_PATH}" "${DB_NAME}"

echo "Granting privileges to ${DB_USER}..."
sudo -u postgres psql -v ON_ERROR_STOP=1 -d "${DB_NAME}" <<SQL
GRANT USAGE ON SCHEMA public TO ${DB_USER};
GRANT ALL ON ALL TABLES IN SCHEMA public TO ${DB_USER};
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO ${DB_USER};
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO ${DB_USER};
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO ${DB_USER};
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO ${DB_USER};
SQL

echo "Database ${DB_NAME} setup complete."
echo "You can verify with: sudo -u postgres psql ${DB_NAME} -c \"SELECT tablename FROM pg_tables WHERE schemaname='public';\""

exit 0
