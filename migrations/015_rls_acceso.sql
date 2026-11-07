-- 015: RLS también en las tablas de acceso (usuarios, organizaciones, tokens, códigos MFA, historial de claves, licencias).
-- Hasta ahora las protegía solo el código porque, al iniciar sesión, aún no se sabe la organización. Ahora:
--   * app.usuario_id = la persona que usa (o intenta usar) la cuenta; app.maestro = '1' solo para el Maestro.
--   * Las búsquedas previas a la sesión (usuario, correo, enlace) pasan por funciones SECURITY DEFINER que solo devuelven un número.
-- Idempotente.
CREATE OR REPLACE FUNCTION app_usuario() RETURNS integer LANGUAGE sql STABLE AS
$$ SELECT NULLIF(current_setting('app.usuario_id', true), '')::integer $$;
CREATE OR REPLACE FUNCTION app_maestro() RETURNS boolean LANGUAGE sql STABLE AS
$$ SELECT coalesce(current_setting('app.maestro', true), '') = '1' $$;

-- Búsquedas previas a la sesión: solo devuelven el número de usuario (o de organización)
CREATE OR REPLACE FUNCTION acceso_usuario_por_nombre(valor text) RETURNS integer LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS
$$ SELECT id FROM usuarios WHERE username = valor::citext $$;
CREATE OR REPLACE FUNCTION acceso_usuario_por_correo(valor text) RETURNS integer LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS
$$ SELECT id FROM usuarios WHERE email = valor::citext $$;
CREATE OR REPLACE FUNCTION acceso_usuario_por_token(hash text, prop text) RETURNS integer LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS
$$ SELECT usuario_id FROM tokens_correo WHERE token_hash = hash AND proposito = prop $$;
-- ¿Ese usuario o correo ya existe en OTRA cuenta? (para avisar sin revelar de quién es)
CREATE OR REPLACE FUNCTION acceso_organizacion_de(nombre text, correo text) RETURNS TABLE(usuario_id integer, organizacion_id integer)
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS
$$ SELECT id, organizacion_id FROM usuarios WHERE username = nombre::citext OR email = correo::citext $$;
-- Organización actual de una cuenta (para que nadie se mueva solo a otra organización)
CREATE OR REPLACE FUNCTION acceso_org_de_usuario(uid integer) RETURNS integer LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS
$$ SELECT organizacion_id FROM usuarios WHERE id = uid $$;
REVOKE ALL ON FUNCTION acceso_org_de_usuario(integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION acceso_usuario_por_nombre(text), acceso_usuario_por_correo(text), acceso_usuario_por_token(text, text),
                       acceso_organizacion_de(text, text) FROM PUBLIC;

ALTER TABLE usuarios ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS p_acceso ON usuarios;
CREATE POLICY p_acceso ON usuarios
  USING (app_maestro() OR id = app_usuario() OR (app_org() IS NOT NULL AND organizacion_id = app_org()))
  -- la propia cuenta se puede actualizar (clave, MFA, último acceso) pero nunca cambiar de organización
  WITH CHECK (app_maestro() OR (app_org() IS NOT NULL AND organizacion_id = app_org())
              OR (id = app_usuario() AND organizacion_id IS NOT DISTINCT FROM acceso_org_de_usuario(app_usuario())));

ALTER TABLE organizaciones ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS p_acceso ON organizaciones;
CREATE POLICY p_acceso ON organizaciones
  USING (app_maestro() OR id = app_org() OR id = (SELECT u.organizacion_id FROM usuarios u WHERE u.id = app_usuario()))
  WITH CHECK (app_maestro() OR id = app_org());

ALTER TABLE licencias_organizacion ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS p_acceso ON licencias_organizacion;
CREATE POLICY p_acceso ON licencias_organizacion USING (app_maestro() OR organizacion_id = app_org()) WITH CHECK (app_maestro() OR organizacion_id = app_org());

-- Enlaces de correo y códigos de respaldo: la persona, el administrador de su organización (invitaciones) o el Maestro
DO $$ DECLARE t text; BEGIN
  FOREACH t IN ARRAY ARRAY['tokens_correo','codigos_recuperacion_mfa'] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS p_acceso ON %I', t);
    EXECUTE format('CREATE POLICY p_acceso ON %I USING (app_maestro() OR usuario_id = app_usuario() OR usuario_id IN '
                   '(SELECT id FROM usuarios WHERE app_org() IS NOT NULL AND organizacion_id = app_org())) '
                   'WITH CHECK (app_maestro() OR usuario_id = app_usuario() OR usuario_id IN '
                   '(SELECT id FROM usuarios WHERE app_org() IS NOT NULL AND organizacion_id = app_org()))', t);
  END LOOP;
END $$;
-- Historial de claves: solo la propia persona (o el Maestro)
ALTER TABLE historial_claves ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS p_acceso ON historial_claves;
CREATE POLICY p_acceso ON historial_claves USING (app_maestro() OR usuario_id = app_usuario()) WITH CHECK (app_maestro() OR usuario_id = app_usuario());
