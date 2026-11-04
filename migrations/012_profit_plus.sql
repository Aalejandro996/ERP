-- 012: integración de SOLO LECTURA con Profit Plus (conector en la red del cliente → Contax) y monitoreo del bot en la
-- consola Maestro. Idempotente. Ejecutar con el rol DUEÑO tras db.create_all() (lo hace scripts/inicializar.py).
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['profit_conexion','profit_articulos','profit_clientes','profit_sincronizaciones'] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS p_org ON %I', t);
    EXECUTE format('CREATE POLICY p_org ON %I USING (empresa_id IN (SELECT id FROM empresas WHERE organizacion_id = app_org()))', t);
  END LOOP;
END $$;

-- API del conector para toda la plataforma (una fila; solo la edita el Maestro).
CREATE TABLE IF NOT EXISTS profit_plataforma (
    id integer PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    activa boolean NOT NULL DEFAULT true,
    url_publica text,
    url_instalador text,
    intervalo_min integer NOT NULL DEFAULT 15 CHECK (intervalo_min BETWEEN 5 AND 1440),
    max_articulos integer NOT NULL DEFAULT 20000,
    max_clientes integer NOT NULL DEFAULT 50000,
    actualizado timestamptz NOT NULL DEFAULT now(),
    actualizado_por text
);
ALTER TABLE profit_plataforma ADD COLUMN IF NOT EXISTS url_instalador text;
ALTER TABLE profit_plataforma ALTER COLUMN actualizado SET DEFAULT now();
CREATE INDEX IF NOT EXISTS ix_profit_sinc_empresa_ts ON profit_sincronizaciones (empresa_id, ts DESC);

-- El conector llega SIN usuario: esta función traduce su id público a empresa/organización, solo si la conexión, la empresa
-- y la organización están activas. Devuelve únicamente los ids; después la petición fija app.org_id y todo pasa por RLS.
CREATE OR REPLACE FUNCTION profit_destino(p_conector text)
RETURNS TABLE(empresa_id integer, organizacion_id integer)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT p.empresa_id, e.organizacion_id
    FROM profit_conexion p JOIN empresas e ON e.id = p.empresa_id JOIN organizaciones o ON o.id = e.organizacion_id
   WHERE p.conector_id = p_conector AND o.estado = 'activa'
$$;
REVOKE ALL ON FUNCTION profit_destino(text) FROM PUBLIC;

-- Consola Maestro → «Monitoreo del bot»: SOLO conteos y estados técnicos por empresa (ni mensajes, ni clientes, ni precios).
CREATE OR REPLACE FUNCTION maestro_monitor_bot()
RETURNS TABLE(empresa_id integer, razon_social text, organizacion text, proveedor text, sesion_estado text, bot_activo boolean,
              recibidos_24h bigint, enviados_24h bigint, fallidos_24h bigint, ultimo_mensaje timestamptz, pedidos_24h bigint,
              profit_activo boolean, profit_ultima_sync timestamptz, profit_ultimo_contacto timestamptz, profit_estado text,
              profit_error text, profit_articulos integer, profit_version text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT e.id, e.razon_social, o.nombre, w.proveedor, w.sesion_estado, w.activo,
         (SELECT count(*) FROM whatsapp_mensajes m WHERE m.empresa_id = e.id AND m.direccion = 'in'  AND m.ts > now() - interval '24 hours'),
         (SELECT count(*) FROM whatsapp_mensajes m WHERE m.empresa_id = e.id AND m.direccion = 'out' AND m.ts > now() - interval '24 hours' AND m.resultado = 'ok'),
         (SELECT count(*) FROM whatsapp_mensajes m WHERE m.empresa_id = e.id AND m.direccion = 'out' AND m.ts > now() - interval '24 hours' AND m.resultado IS DISTINCT FROM 'ok'),
         (SELECT max(m.ts) FROM whatsapp_mensajes m WHERE m.empresa_id = e.id),
         (SELECT count(*) FROM pedidos pe WHERE pe.empresa_id = e.id AND pe.creado_en > now() - interval '24 hours'),
         p.activo, p.ultima_sync, p.ultimo_contacto, p.ultimo_estado, left(p.ultimo_error, 200), p.n_articulos, p.agente_version
    FROM empresas e JOIN organizaciones o ON o.id = e.organizacion_id
    LEFT JOIN whatsapp_config w ON w.empresa_id = e.id
    LEFT JOIN profit_conexion p ON p.empresa_id = e.id
   WHERE w.empresa_id IS NOT NULL OR p.empresa_id IS NOT NULL
   ORDER BY o.nombre, e.razon_social
$$;
REVOKE ALL ON FUNCTION maestro_monitor_bot() FROM PUBLIC;
