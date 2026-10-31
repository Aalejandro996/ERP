-- 008: tutorial inicial, datos/logo/módulos por empresa, valores de órdenes de compra, migración desde otros sistemas,
-- facturación automática del bot y cuadre de libros. Idempotente. Ejecutar con el rol DUEÑO tras db.create_all().
ALTER TABLE organizaciones ADD COLUMN IF NOT EXISTS bienvenida_completada boolean NOT NULL DEFAULT false;
-- quien ya trabaja (tiene empresas) no ve el tutorial
UPDATE organizaciones o SET bienvenida_completada = true WHERE NOT bienvenida_completada AND EXISTS (SELECT 1 FROM empresas e WHERE e.organizacion_id = o.id);
ALTER TABLE empresas ADD COLUMN IF NOT EXISTS correo text;
ALTER TABLE empresas ADD COLUMN IF NOT EXISTS logo bytea;
ALTER TABLE empresas ADD COLUMN IF NOT EXISTS logo_mime text;
ALTER TABLE empresas ADD COLUMN IF NOT EXISTS modulos text;
ALTER TABLE empresas ADD COLUMN IF NOT EXISTS oc_condicion_pago text;
ALTER TABLE empresas ADD COLUMN IF NOT EXISTS oc_lugar_entrega text;
ALTER TABLE empresas ADD COLUMN IF NOT EXISTS oc_observaciones text;
ALTER TABLE empresas ADD COLUMN IF NOT EXISTS oc_numero_inicial integer NOT NULL DEFAULT 1;
ALTER TABLE empresas DROP CONSTRAINT IF EXISTS ck_empresa_logo;
ALTER TABLE empresas ADD CONSTRAINT ck_empresa_logo CHECK (logo IS NULL OR octet_length(logo) <= 307200);
ALTER TABLE documentos_cxc ADD COLUMN IF NOT EXISTS origen text NOT NULL DEFAULT 'manual';
ALTER TABLE documentos_cxp ADD COLUMN IF NOT EXISTS origen text NOT NULL DEFAULT 'manual';
ALTER TABLE ordenes_compra ADD COLUMN IF NOT EXISTS cxp_id integer;
ALTER TABLE whatsapp_config ADD COLUMN IF NOT EXISTS instrucciones_pago text;
ALTER TABLE whatsapp_config ADD COLUMN IF NOT EXISTS factura_automatica boolean NOT NULL DEFAULT false;
ALTER TABLE whatsapp_config ADD COLUMN IF NOT EXISTS serie_id integer;
ALTER TABLE whatsapp_config ADD COLUMN IF NOT EXISTS dias_credito integer NOT NULL DEFAULT 0;
ALTER TABLE whatsapp_config ADD COLUMN IF NOT EXISTS usuario_id integer REFERENCES usuarios(id);
ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS documento_venta_id bigint;
ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS referencia_cliente text;
ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS rif text;
ALTER TABLE pedidos DROP CONSTRAINT IF EXISTS ck_pedido_estado;
ALTER TABLE pedidos ADD CONSTRAINT ck_pedido_estado CHECK (estado IN ('Nuevo','Confirmado','Despachado','Rechazado','Convertido','Facturado'));

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['migraciones','asientos_origen'] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS p_org ON %I', t);
    EXECUTE format('CREATE POLICY p_org ON %I USING (empresa_id IN (SELECT id FROM empresas WHERE organizacion_id = app_org()))', t);
  END LOOP;
END $$;
