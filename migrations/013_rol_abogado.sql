-- 013: roles «Abogado» (revisa y aprueba los documentos legales) y «Director» (Directores Generales: autorizan cada documento). Como el Maestro, no pertenece a ninguna
-- organización cliente y no ve datos de clientes (RLS). Idempotente; la 002, que corre en cada despliegue, ya trae estas reglas.
ALTER TABLE usuarios DROP CONSTRAINT IF EXISTS ck_usuarios_org;
ALTER TABLE usuarios ADD CONSTRAINT ck_usuarios_org CHECK ((rol IN ('Maestro','Abogado','Director')) = (organizacion_id IS NULL));
ALTER TABLE usuarios DROP CONSTRAINT IF EXISTS ck_usuarios_rol;
ALTER TABLE usuarios ADD CONSTRAINT ck_usuarios_rol CHECK (rol IN ('Maestro','Abogado','Director','Administrador','Contador','Auxiliar','Auditor Financiero','Cajero',
  'Gerente General','Gerente Administrativo','Analista Fiscal','Tesorero','Analista de Cuentas por Cobrar','Analista de Cuentas por Pagar',
  'Comprador','Almacenista','Facturador','Vendedor','Auditor Interno','Consulta'));
