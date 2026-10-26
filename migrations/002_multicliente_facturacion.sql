-- 002: multi-cliente (organizaciones), licencias por plan, seguridad por correo y facturación.
-- Idempotente. Ejecutar con el rol DUEÑO tras db.create_all() (lo hace scripts/inicializar.py).
-- En una instalación existente: TODOS los usuarios, empresas y la bitácora pasan a una "Organización principal",
-- con lo que el sistema sigue funcionando igual para ese cliente.

-- 1) Columnas nuevas en tablas existentes ------------------------------------------------------------------------
ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS organizacion_id integer REFERENCES organizaciones(id);
ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS email citext;
ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS email_verificado boolean NOT NULL DEFAULT false;
ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS sesion_version integer NOT NULL DEFAULT 1;
ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS ultimo_ip text;
CREATE UNIQUE INDEX IF NOT EXISTS usuarios_email_key ON usuarios(email);
CREATE INDEX IF NOT EXISTS ix_usuarios_organizacion_id ON usuarios(organizacion_id);
ALTER TABLE empresas ADD COLUMN IF NOT EXISTS organizacion_id integer REFERENCES organizaciones(id);
ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS organizacion_id integer;
ALTER TABLE tramos_provision ADD COLUMN IF NOT EXISTS organizacion_id integer REFERENCES organizaciones(id);
ALTER TABLE config_asientos ADD COLUMN IF NOT EXISTS cuenta_igtf_por_pagar text;
ALTER TABLE config_asientos ADD COLUMN IF NOT EXISTS cuenta_iva_retenido_por_clientes text;
ALTER TABLE productos ADD COLUMN IF NOT EXISTS precio_venta numeric(18,4) NOT NULL DEFAULT 0;
ALTER TABLE productos ADD COLUMN IF NOT EXISTS alicuota_iva varchar(1) NOT NULL DEFAULT 'G';

-- 2) Adaptación de los datos existentes a una organización -------------------------------------------------------
DO $$
DECLARE org integer;
BEGIN
  IF EXISTS (SELECT 1 FROM empresas WHERE organizacion_id IS NULL)
     OR EXISTS (SELECT 1 FROM usuarios WHERE organizacion_id IS NULL AND rol <> 'Maestro')
     OR EXISTS (SELECT 1 FROM tramos_provision WHERE organizacion_id IS NULL)
     OR EXISTS (SELECT 1 FROM audit_log WHERE organizacion_id IS NULL) THEN
    SELECT id INTO org FROM organizaciones ORDER BY id LIMIT 1;
    IF org IS NULL THEN
      INSERT INTO organizaciones(nombre, rif, estado, creado_en)
      VALUES ('Organización principal', 'J-00000000-0', 'activa', now()) RETURNING id INTO org;
      RAISE NOTICE 'Creada la Organización principal (id %). Actualice su nombre y RIF.', org;
    END IF;
    UPDATE empresas SET organizacion_id = org WHERE organizacion_id IS NULL;
    UPDATE usuarios SET organizacion_id = org WHERE organizacion_id IS NULL AND rol <> 'Maestro';
    UPDATE tramos_provision SET organizacion_id = org WHERE organizacion_id IS NULL;
    -- La bitácora es de solo-inserción: se suspende el trigger SOLO para asignar la organización (no cambia el hash).
    IF EXISTS (SELECT 1 FROM audit_log WHERE organizacion_id IS NULL) AND NOT EXISTS (SELECT 1 FROM audit_log WHERE organizacion_id IS NOT NULL) THEN
      ALTER TABLE audit_log DISABLE TRIGGER trg_inmutable;
      UPDATE audit_log SET organizacion_id = org WHERE organizacion_id IS NULL;
      ALTER TABLE audit_log ENABLE TRIGGER trg_inmutable;
    END IF;
    -- La licencia única anterior pasa a ser la licencia de esta organización.
    INSERT INTO licencias_organizacion(organizacion_id, datos)
      SELECT org, datos FROM licencia_local WHERE id = 1 AND datos::text <> '{}'
      ON CONFLICT (organizacion_id) DO NOTHING;
  END IF;
END $$;
-- Los usuarios existentes deberán registrar y verificar su correo al entrar (la app los guía) y activar MFA.

ALTER TABLE empresas ALTER COLUMN organizacion_id SET NOT NULL;
ALTER TABLE tramos_provision ALTER COLUMN organizacion_id SET NOT NULL;
CREATE INDEX IF NOT EXISTS ix_empresas_organizacion_id ON empresas(organizacion_id);
ALTER TABLE empresas DROP CONSTRAINT IF EXISTS empresas_rif_key;  -- el RIF pasa a ser único POR organización
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_empresa_org_rif') THEN
    ALTER TABLE empresas ADD CONSTRAINT uq_empresa_org_rif UNIQUE (organizacion_id, rif);
  END IF;
END $$;
ALTER TABLE usuarios DROP CONSTRAINT IF EXISTS ck_usuarios_rol;
ALTER TABLE usuarios ADD CONSTRAINT ck_usuarios_rol CHECK (rol IN ('Maestro','Administrador','Contador','Auxiliar','Auditor Financiero','Cajero'));
ALTER TABLE usuarios DROP CONSTRAINT IF EXISTS ck_usuarios_org;
ALTER TABLE usuarios ADD CONSTRAINT ck_usuarios_org CHECK ((rol = 'Maestro') = (organizacion_id IS NULL));
ALTER TABLE productos DROP CONSTRAINT IF EXISTS ck_producto_venta;
ALTER TABLE productos ADD CONSTRAINT ck_producto_venta CHECK (alicuota_iva IN ('G','R','A','E') AND precio_venta >= 0);

-- 3) Facturación: documentos de solo-inserción (solo cambian estado/cobro/retención/anulación) --------------------
CREATE OR REPLACE FUNCTION docventa_inmutable() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'Los documentos de venta no se borran (anúlelos)'; END IF;
  IF (to_jsonb(NEW) - ARRAY['estado','saldo_pendiente','iva_retenido','comprobante_retencion','anulado_en','anulado_por','motivo_anulacion'])
     IS DISTINCT FROM (to_jsonb(OLD) - ARRAY['estado','saldo_pendiente','iva_retenido','comprobante_retencion','anulado_en','anulado_por','motivo_anulacion']) THEN
    RAISE EXCEPTION 'Un documento de venta emitido no puede modificarse (emita una nota de crédito o débito)';
  END IF;
  IF OLD.estado = 'Anulado' AND NEW.estado <> 'Anulado' THEN RAISE EXCEPTION 'Un documento anulado no puede reactivarse'; END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_docventa_inmutable ON documentos_venta;
CREATE TRIGGER trg_docventa_inmutable BEFORE UPDATE OR DELETE ON documentos_venta FOR EACH ROW EXECUTE FUNCTION docventa_inmutable();
DO $$ DECLARE t text; BEGIN
  FOREACH t IN ARRAY ARRAY['lineas_documento_venta','pagos_documento_venta'] LOOP
    EXECUTE format('DROP TRIGGER IF EXISTS trg_inmutable ON %I', t);
    EXECUTE format('CREATE TRIGGER trg_inmutable BEFORE UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION bloquear_mod()', t);
  END LOOP;
END $$;

-- 4) Row-Level Security: una organización NUNCA ve filas de otra, aunque la aplicación tuviera un error ----------------
-- La app fija app.org_id en cada transacción (app/rls.py). Vacío = sin organización = no ve datos de negocio.
CREATE OR REPLACE FUNCTION app_org() RETURNS integer LANGUAGE sql STABLE AS
  $$ SELECT NULLIF(current_setting('app.org_id', true), '')::integer $$;

ALTER TABLE empresas ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS p_org ON empresas;
CREATE POLICY p_org ON empresas USING (organizacion_id = app_org());

DO $$ DECLARE t text; BEGIN
  FOREACH t IN ARRAY ARRAY['cuentas','asientos','movimientos','periodos_cerrados','config_asientos','documentos_cxc','documentos_cxp',
                           'productos','movimientos_inventario','lotes_peps','retenciones_iva','retenciones_islr','documentos_fiscales',
                           'partidas_conciliacion_fiscal','rebajas_islr','extracto_bancario','hallazgos_auditoria','papeles_trabajo',
                           'clientes_facturacion','series_facturacion','documentos_venta','lineas_documento_venta','pagos_documento_venta',
                           'usuario_empresa'] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS p_org ON %I', t);
    EXECUTE format('CREATE POLICY p_org ON %I USING (empresa_id IN (SELECT id FROM empresas WHERE organizacion_id = app_org()))', t);
  END LOOP;
END $$;
ALTER TABLE cobros_cxc ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS p_org ON cobros_cxc;
CREATE POLICY p_org ON cobros_cxc USING (documento_id IN (SELECT id FROM documentos_cxc));
ALTER TABLE pagos_cxp ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS p_org ON pagos_cxp;
CREATE POLICY p_org ON pagos_cxp USING (documento_id IN (SELECT id FROM documentos_cxp));
ALTER TABLE tramos_provision ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS p_org ON tramos_provision;
CREATE POLICY p_org ON tramos_provision USING (organizacion_id = app_org());
-- Bitácora: cada organización lee solo su cadena (la de plataforma = NULL); insertar se permite (lo hace la app).
ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS p_org ON audit_log;
DROP POLICY IF EXISTS p_org_ins ON audit_log;
CREATE POLICY p_org ON audit_log FOR SELECT USING (organizacion_id IS NOT DISTINCT FROM app_org());
CREATE POLICY p_org_ins ON audit_log FOR INSERT WITH CHECK (true);

-- 5) Mínimo privilegio (ejecutar con el rol dueño; la app conecta como erp_app, NO dueño, para que RLS aplique) ----
-- CREATE ROLE erp_app LOGIN PASSWORD '...';
-- GRANT SELECT, INSERT ON asientos, movimientos, audit_log, movimientos_inventario, lineas_documento_venta, pagos_documento_venta TO erp_app;
-- GRANT SELECT, INSERT, UPDATE ON documentos_venta TO erp_app;   -- el trigger limita qué columnas cambian
-- GRANT SELECT, INSERT, UPDATE, DELETE ON organizaciones, usuarios, empresas, usuario_empresa, cuentas, periodos_cerrados, licencia_local,
--   licencias_organizacion, tokens_correo, codigos_recuperacion_mfa, historial_claves, config_asientos, documentos_cxc, cobros_cxc,
--   documentos_cxp, pagos_cxp, tramos_provision, productos, lotes_peps, unidad_tributaria, tarifa_islr_juridicas, conceptos_retencion_islr,
--   indice_inpc, tasa_cambio, alicuotas_impuesto, retenciones_iva, retenciones_islr, documentos_fiscales, partidas_conciliacion_fiscal,
--   rebajas_islr, extracto_bancario, hallazgos_auditoria, papeles_trabajo, clientes_facturacion, series_facturacion TO erp_app;
-- GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO erp_app;
-- GRANT EXECUTE ON FUNCTION pg_control_system() TO erp_app;   -- huella de la instalación (licencias)
