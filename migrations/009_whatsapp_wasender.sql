-- 009: el bot puede conectarse por la Cloud API de Meta o por WasenderAPI (WhatsApp por QR). Idempotente.
ALTER TABLE whatsapp_config ADD COLUMN IF NOT EXISTS proveedor text NOT NULL DEFAULT 'meta';
ALTER TABLE whatsapp_config ADD COLUMN IF NOT EXISTS api_url text;
ALTER TABLE whatsapp_config ALTER COLUMN phone_number_id DROP NOT NULL;
ALTER TABLE whatsapp_config DROP CONSTRAINT IF EXISTS ck_wa_proveedor;
-- Las migraciones se repiten en cada despliegue: esta lista NUNCA debe ser más corta que la de migraciones posteriores
-- (la 011 agregó 'openwa'); si lo fuera, una fila ya existente rompería el despliegue.
ALTER TABLE whatsapp_config ADD CONSTRAINT ck_wa_proveedor CHECK (proveedor IN ('meta','wasender','openwa'));
