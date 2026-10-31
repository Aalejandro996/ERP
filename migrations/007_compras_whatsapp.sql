-- 007: órdenes de compra, proveedores y bot de WhatsApp (contactos, mensajes, pedidos). Idempotente.
-- Ejecutar con el rol DUEÑO tras db.create_all() (lo hace scripts/inicializar.py).
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['proveedores','ordenes_compra','lineas_orden_compra','whatsapp_config','whatsapp_contactos','whatsapp_mensajes','pedidos'] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS p_org ON %I', t);
    EXECUTE format('CREATE POLICY p_org ON %I USING (empresa_id IN (SELECT id FROM empresas WHERE organizacion_id = app_org()))', t);
  END LOOP;
END $$;

-- El webhook de WhatsApp llega SIN usuario: esta función (SECURITY DEFINER) traduce la clave secreta de la URL a su
-- empresa/organización, solo si el número está activo y la organización también. Devuelve únicamente esos dos ids;
-- después la petición fija app.org_id y todo lo demás pasa por RLS como cualquier otra.
CREATE OR REPLACE FUNCTION whatsapp_destino(p_clave text)
RETURNS TABLE(empresa_id integer, organizacion_id integer)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT w.empresa_id, e.organizacion_id
    FROM whatsapp_config w JOIN empresas e ON e.id = w.empresa_id JOIN organizaciones o ON o.id = e.organizacion_id
   WHERE w.clave_webhook = p_clave AND w.activo AND o.estado = 'activa'
$$;
REVOKE ALL ON FUNCTION whatsapp_destino(text) FROM PUBLIC;
