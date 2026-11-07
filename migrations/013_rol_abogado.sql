-- 013: rol «Abogado» de Gigadevs (revisa y aprueba los documentos legales). Como el Maestro, no pertenece a ninguna
-- organización cliente y no ve datos de clientes (RLS). Idempotente; la 002, que corre en cada despliegue, ya trae estas reglas.
ALTER TABLE usuarios DROP CONSTRAINT IF EXISTS ck_usuarios_org;
ALTER TABLE usuarios ADD CONSTRAINT ck_usuarios_org CHECK ((rol IN ('Maestro','Abogado')) = (organizacion_id IS NULL));
ALTER TABLE usuarios DROP CONSTRAINT IF EXISTS ck_usuarios_rol;
ALTER TABLE usuarios ADD CONSTRAINT ck_usuarios_rol CHECK (rol IN ('Maestro','Abogado','Administrador','Contador','Auxiliar','Auditor Financiero','Cajero',
  'Gerente General','Gerente Administrativo','Analista Fiscal','Tesorero','Analista de Cuentas por Cobrar','Analista de Cuentas por Pagar',
  'Comprador','Almacenista','Facturador','Vendedor','Auditor Interno','Consulta'));
