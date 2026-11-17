-- ---------------------------------------------------------------------------
-- Gaia read-only role for a PostgreSQL target.
--
-- OWNER-EXECUTED on a real target (Gaia never runs this there); run by
-- infra/pg-target/seed.py for the development target only.
--
--   psql "$ADMIN_URL" -v ro_user=gaia_ro -v ro_password='<strong-password>' \
--        -v dbname=<database> -f infra/pg-target/readonly_role.sql
--
-- PostgreSQL privileges are purely additive (there is no DENY), so read-only
-- means: the role owns nothing, holds no INSERT/UPDATE/DELETE/TRUNCATE grant,
-- and cannot CREATE in the database or in any schema. `make check-target`
-- proves exactly that, and additionally attempts a DDL statement in a
-- READ WRITE transaction that must fail.
--
-- `default_transaction_read_only` is set as well, but it is a convenience,
-- not a security boundary: any session may turn it off. The privileges are
-- the boundary.
--
-- Shared clusters: prefer these per-schema grants. On a dedicated cluster,
-- `GRANT pg_read_all_data TO gaia_ro` (PostgreSQL 14+) is the simpler
-- equivalent of SQL Server's db_datareader, but it spans every database.
-- ---------------------------------------------------------------------------

-- The role must exist:  CREATE ROLE gaia_ro LOGIN;
ALTER ROLE :"ro_user" WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
    PASSWORD :'ro_password';
ALTER ROLE :"ro_user" SET default_transaction_read_only = on;

GRANT CONNECT ON DATABASE :"dbname" TO :"ro_user";
REVOKE CREATE ON DATABASE :"dbname" FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;   -- default since PostgreSQL 15

GRANT USAGE ON SCHEMA crm, hr, product, store, sales, finance, legacy, rpt TO :"ro_user";
GRANT SELECT ON ALL TABLES IN SCHEMA crm, hr, product, store, sales, finance, legacy, rpt
    TO :"ro_user";
ALTER DEFAULT PRIVILEGES IN SCHEMA crm, hr, product, store, sales, finance, legacy, rpt
    GRANT SELECT ON TABLES TO :"ro_user";

-- Statistics visibility (pg_stat_*, pg_stat_statements), the analogue of
-- SQL Server's VIEW DATABASE STATE.
GRANT pg_read_all_stats TO :"ro_user";
