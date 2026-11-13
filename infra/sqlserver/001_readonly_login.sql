-- ---------------------------------------------------------------------------
-- Gaia read-only login for the target SQL Server database.
--
-- OWNER-EXECUTED, ONCE PER RESTORED DATABASE. Gaia never runs this.
-- Replace <strong-password> and <DatabaseName> before executing.
--
--   docker exec -i sql1 /opt/mssql-tools18/bin/sqlcmd \
--     -S localhost -U sa -P "$MSSQL_SA_PASSWORD" -C -i /dev/stdin \
--     < infra/sqlserver/001_readonly_login.sql
-- ---------------------------------------------------------------------------

USE [<DatabaseName>];
GO

IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = 'gaia_ro')
BEGIN
    CREATE LOGIN [gaia_ro]
        WITH PASSWORD = N'<strong-password>',
             CHECK_POLICY = OFF,
             CHECK_EXPIRATION = OFF;
END
GO

IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = 'gaia_ro')
BEGIN
    CREATE USER [gaia_ro] FOR LOGIN [gaia_ro];
END
GO

-- SELECT on every table and view.
ALTER ROLE [db_datareader] ADD MEMBER [gaia_ro];
GO

-- Metadata visibility: sys.objects, sys.columns, extended properties, definitions.
GRANT VIEW DEFINITION TO [gaia_ro];
GO

-- DMV access for row counts, sizes and index usage.
GRANT VIEW DATABASE STATE TO [gaia_ro];
GO

-- Defence in depth: block writes without blocking access.
-- IMPORTANT: never DENY CONTROL (or CONNECT) at database scope. CONTROL
-- implies CONNECT, so a DENY CONTROL silently locks the login out of the
-- database and every connection fails with Msg 916 / "Cannot open database".
DENY INSERT, UPDATE, DELETE, ALTER, REFERENCES TO [gaia_ro];
GO

PRINT 'gaia_ro provisioned on <DatabaseName>.';
GO
