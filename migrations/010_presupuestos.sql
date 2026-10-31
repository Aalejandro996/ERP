-- 010: presupuestos de cada empresa a sus clientes (manuales o por el bot de WhatsApp). Idempotente.
ALTER TABLE whatsapp_config ADD COLUMN IF NOT EXISTS presupuesto_modo text NOT NULL DEFAULT 'automatico';
ALTER TABLE presupuestos_venta ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS p_org ON presupuestos_venta;
CREATE POLICY p_org ON presupuestos_venta USING (empresa_id IN (SELECT id FROM empresas WHERE organizacion_id = app_org()));
