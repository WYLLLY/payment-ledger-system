#!/bin/sh
# Executed only on a fresh PostgreSQL volume by the official image.
set -eu
psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --set ON_ERROR_STOP=1 <<'SQL'
\getenv migration_password MIGRATION_DB_PASSWORD
\getenv database_name POSTGRES_DB
CREATE ROLE payment_migrator LOGIN NOSUPERUSER NOCREATEDB CREATEROLE NOINHERIT
    NOREPLICATION NOBYPASSRLS PASSWORD :'migration_password';
ALTER DATABASE :"database_name" OWNER TO payment_migrator;
SQL
