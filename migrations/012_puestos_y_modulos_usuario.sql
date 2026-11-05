-- 012: más puestos de trabajo (cada uno con su matriz de permisos en app/security.py) y módulos por usuario.
-- Los roles existentes conservan su nombre: ningún usuario cambia de permisos. Idempotente.
ALTER TABLE usuarios DROP CONSTRAINT IF EXISTS ck_usuarios_rol;
ALTER TABLE usuarios ADD CONSTRAINT ck_usuarios_rol CHECK (rol IN ('Maestro','Administrador','Contador','Auxiliar','Auditor Financiero','Cajero',
  'Gerente General','Gerente Administrativo','Analista Fiscal','Tesorero','Analista de Cuentas por Cobrar','Analista de Cuentas por Pagar',
  'Comprador','Almacenista','Facturador','Vendedor','Auditor Interno','Consulta'));
-- Módulos a los que el administrador da acceso a cada usuario, separados por comas. NULL = todos los que traiga el plan.
ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS modulos text;
