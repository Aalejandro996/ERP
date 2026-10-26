-- Integridad a nivel de BASE DE DATOS (aunque alguien salte la aplicación). Ejecutar tras crear tablas.
CREATE EXTENSION IF NOT EXISTS citext;

-- 1) Partida doble: al confirmar la transacción, cada asiento debe cuadrar.
CREATE OR REPLACE FUNCTION chk_asiento_cuadrado() RETURNS trigger AS $$
DECLARE d numeric; h numeric; n int; aid bigint := COALESCE(NEW.asiento_id, OLD.asiento_id);
BEGIN
  SELECT COALESCE(SUM(debe),0), COALESCE(SUM(haber),0), COUNT(*) INTO d, h, n FROM movimientos WHERE asiento_id = aid;
  IF n < 2 OR d <> h THEN
    RAISE EXCEPTION 'Asiento % descuadrado o incompleto (debe=%, haber=%, lineas=%)', aid, d, h, n;
  END IF;
  RETURN NULL;
END $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_asiento_cuadrado ON movimientos;
CREATE CONSTRAINT TRIGGER trg_asiento_cuadrado AFTER INSERT ON movimientos
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION chk_asiento_cuadrado();

-- 2) Inmutabilidad: asientos, movimientos y bitácora no se editan ni se borran.
CREATE OR REPLACE FUNCTION bloquear_mod() RETURNS trigger AS $$
BEGIN RAISE EXCEPTION 'Tabla % es de solo-inserción (use contra-asiento)', TG_TABLE_NAME; END $$ LANGUAGE plpgsql;
DO $$ DECLARE t text; BEGIN
  FOREACH t IN ARRAY ARRAY['asientos','movimientos','audit_log','movimientos_inventario'] LOOP
    EXECUTE format('DROP TRIGGER IF EXISTS trg_inmutable ON %I', t);
    EXECUTE format('CREATE TRIGGER trg_inmutable BEFORE UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION bloquear_mod()', t);
  END LOOP;
END $$;

-- 3) Mínimo privilegio: la app NO es dueña de las tablas ni puede alterar el esquema.
-- CREATE ROLE erp_app LOGIN PASSWORD '...';  (ejecutar con el rol dueño)
-- GRANT SELECT, INSERT ON asientos, movimientos, audit_log, movimientos_inventario TO erp_app;
-- GRANT SELECT, INSERT, UPDATE, DELETE ON usuarios, empresas, usuario_empresa, cuentas, periodos_cerrados, licencia_local, config_asientos,
--   documentos_cxc, cobros_cxc, documentos_cxp, pagos_cxp, tramos_provision, productos, lotes_peps, unidad_tributaria,
--   tarifa_islr_juridicas, conceptos_retencion_islr, indice_inpc, tasa_cambio, retenciones_iva, retenciones_islr,
--   documentos_fiscales, partidas_conciliacion_fiscal, rebajas_islr, extracto_bancario, hallazgos_auditoria, papeles_trabajo TO erp_app;
-- GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO erp_app;
