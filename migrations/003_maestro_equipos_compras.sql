-- 003: equipos autorizados por licencia, datos fiscales de compras (libro de compras) y funciones del Maestro.
-- Idempotente. Ejecutar con el rol DUEÑO tras db.create_all() (lo hace scripts/inicializar.py).

ALTER TABLE documentos_cxp ADD COLUMN IF NOT EXISTS numero_control text;
ALTER TABLE documentos_cxp ADD COLUMN IF NOT EXISTS tipo_documento text NOT NULL DEFAULT 'FAC';
ALTER TABLE documentos_cxp ADD COLUMN IF NOT EXISTS documento_afectado text;
ALTER TABLE documentos_cxp ADD COLUMN IF NOT EXISTS exento numeric(18,2) NOT NULL DEFAULT 0;
ALTER TABLE documentos_cxp ADD COLUMN IF NOT EXISTS base_imponible numeric(18,2) NOT NULL DEFAULT 0;
ALTER TABLE documentos_cxp ADD COLUMN IF NOT EXISTS alicuota numeric(6,2) NOT NULL DEFAULT 0;
ALTER TABLE documentos_cxp ADD COLUMN IF NOT EXISTS monto_iva numeric(18,2) NOT NULL DEFAULT 0;
ALTER TABLE documentos_cxp ADD COLUMN IF NOT EXISTS es_importacion boolean NOT NULL DEFAULT false;
ALTER TABLE documentos_cxp DROP CONSTRAINT IF EXISTS ck_cxp_fiscal;
ALTER TABLE documentos_cxp ADD CONSTRAINT ck_cxp_fiscal CHECK (tipo_documento IN ('FAC','NC','ND') AND exento >= 0 AND base_imponible >= 0 AND monto_iva >= 0);

-- Equipos: aislados por organización como el resto
ALTER TABLE equipos ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS p_org ON equipos;
CREATE POLICY p_org ON equipos USING (organizacion_id = app_org());

-- Resumen para el Maestro: SOLO conteos por organización (nunca contenido). SECURITY DEFINER con search_path fijo.
CREATE OR REPLACE FUNCTION maestro_resumen_organizaciones()
RETURNS TABLE(organizacion_id integer, empresas bigint, usuarios bigint, asientos bigint, documentos_venta bigint, equipos bigint)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT o.id,
         (SELECT count(*) FROM empresas e WHERE e.organizacion_id = o.id),
         (SELECT count(*) FROM usuarios u WHERE u.organizacion_id = o.id AND u.activo),
         (SELECT count(*) FROM asientos a JOIN empresas e ON e.id = a.empresa_id WHERE e.organizacion_id = o.id),
         (SELECT count(*) FROM documentos_venta d JOIN empresas e ON e.id = d.empresa_id WHERE e.organizacion_id = o.id),
         (SELECT count(*) FROM equipos q WHERE q.organizacion_id = o.id AND q.revocado_en IS NULL)
  FROM organizaciones o
$$;
REVOKE ALL ON FUNCTION maestro_resumen_organizaciones() FROM PUBLIC;

-- Verificación de integridad de TODAS las cadenas de bitácora (devuelve solo organización, eslabones y si está íntegra)
CREATE OR REPLACE FUNCTION maestro_cadenas_bitacora()
RETURNS TABLE(organizacion_id integer, eslabones bigint, ultimo timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT a.organizacion_id, count(*), max(a.ts) FROM audit_log a GROUP BY a.organizacion_id
$$;
REVOKE ALL ON FUNCTION maestro_cadenas_bitacora() FROM PUBLIC;
