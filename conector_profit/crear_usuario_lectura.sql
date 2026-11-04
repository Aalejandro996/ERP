-- Usuario de SOLO LECTURA para el conector de Contax. Ejecútelo en SQL Server Management Studio como administrador (sa o similar).
-- 1) Cambie la contraseña y el nombre de la base de datos de Profit Plus (la de la EMPRESA, p. ej. DEMOA o EMPRESA01).
-- 2) El usuario solo puede hacer SELECT sobre las 6 tablas que lee el conector. No puede modificar, borrar ni ver otras tablas.

USE [master];
IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = 'contax_lectura')
    CREATE LOGIN [contax_lectura] WITH PASSWORD = 'Cambie-Esta-Clave#2026', CHECK_POLICY = ON, DEFAULT_DATABASE = [master];
GO

USE [NOMBRE_DE_LA_EMPRESA_EN_PROFIT];   -- ← cambie aquí
IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = 'contax_lectura')
    CREATE USER [contax_lectura] FOR LOGIN [contax_lectura];
GRANT SELECT ON dbo.saArticulo      TO [contax_lectura];
GRANT SELECT ON dbo.saStockAlmacen  TO [contax_lectura];
GRANT SELECT ON dbo.saArtPrecio     TO [contax_lectura];
GRANT SELECT ON dbo.saTipoPrecio    TO [contax_lectura];
GRANT SELECT ON dbo.saAlmacen       TO [contax_lectura];
GRANT SELECT ON dbo.saCliente       TO [contax_lectura];
DENY INSERT, UPDATE, DELETE, EXECUTE, ALTER TO [contax_lectura];
GO

-- Verificación (debe devolver solo permisos SELECT y DENY):
-- SELECT * FROM fn_my_permissions(NULL, 'DATABASE');   -- ejecutado como contax_lectura
