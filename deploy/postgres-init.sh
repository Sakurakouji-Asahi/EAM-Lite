#!/bin/sh
set -eu

case "$EAM_DATABASE_NAME" in (*[!A-Za-z0-9_]*|'') echo "invalid database name" >&2; exit 1;; esac
case "$EAM_MIGRATION_USER" in (*[!A-Za-z0-9_]*|'') echo "invalid migration role" >&2; exit 1;; esac
case "$EAM_RUNTIME_USER" in (*[!A-Za-z0-9_]*|'') echo "invalid runtime role" >&2; exit 1;; esac

migration_password=$(cat "$EAM_MIGRATION_PASSWORD_FILE")
runtime_password=$(cat "$EAM_RUNTIME_PASSWORD_FILE")

psql --set=ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
  --set=migration_user="$EAM_MIGRATION_USER" \
  --set=runtime_user="$EAM_RUNTIME_USER" \
  --set=migration_password="$migration_password" \
  --set=runtime_password="$runtime_password" \
  --set=database_name="$EAM_DATABASE_NAME" <<'EOSQL'
SELECT format(
  'CREATE ROLE %I LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT',
  :'migration_user', :'migration_password'
) WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'migration_user') \gexec
SELECT format(
  'CREATE ROLE %I LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT',
  :'runtime_user', :'runtime_password'
) WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'runtime_user') \gexec
SELECT format('CREATE DATABASE %I OWNER %I', :'database_name', :'migration_user')
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'database_name') \gexec
SELECT format('REVOKE ALL ON DATABASE %I FROM PUBLIC', :'database_name') \gexec
SELECT format('GRANT CONNECT ON DATABASE %I TO %I', :'database_name', :'runtime_user') \gexec
EOSQL

psql --set=ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$EAM_DATABASE_NAME" \
  --set=migration_user="$EAM_MIGRATION_USER" \
  --set=runtime_user="$EAM_RUNTIME_USER" <<'EOSQL'
SELECT format('ALTER SCHEMA public OWNER TO %I', :'migration_user') \gexec
SELECT format('GRANT USAGE ON SCHEMA public TO %I', :'runtime_user') \gexec
SELECT format(
  'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO %I',
  :'migration_user', :'runtime_user'
) \gexec
SELECT format(
  'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO %I',
  :'migration_user', :'runtime_user'
) \gexec
EOSQL
