-- 011: el bot se conecta por OpenWA (open-wa.org): cada empresa vincula SU WhatsApp escaneando un QR, como WhatsApp Web.
-- La pasarela (URL y clave de API) es una sola para toda la plataforma y la configura el Maestro. Idempotente.
ALTER TABLE whatsapp_config ALTER COLUMN token_cifrado DROP NOT NULL;          -- con OpenWA la empresa no guarda token propio
ALTER TABLE whatsapp_config ADD COLUMN IF NOT EXISTS sesion_id text;           -- id de la sesión en la pasarela OpenWA
ALTER TABLE whatsapp_config ADD COLUMN IF NOT EXISTS sesion_estado text;       -- último estado informado (qr_ready, ready, …)
ALTER TABLE whatsapp_config ADD COLUMN IF NOT EXISTS sesion_actualizada timestamptz;
ALTER TABLE whatsapp_config DROP CONSTRAINT IF EXISTS ck_wa_proveedor;
ALTER TABLE whatsapp_config ADD CONSTRAINT ck_wa_proveedor CHECK (proveedor IN ('meta','wasender','openwa'));

-- Pasarela de la plataforma (una fila). No pertenece a ninguna organización: solo la consola Maestro la edita y la clave va
-- cifrada (Fernet). Las empresas nunca la ven: el ERP la usa en su nombre y solo sobre la sesión de cada una.
CREATE TABLE IF NOT EXISTS whatsapp_pasarela (
    id integer PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    url text NOT NULL,
    api_key_cifrada text NOT NULL,
    activa boolean NOT NULL DEFAULT true,
    actualizado timestamptz NOT NULL DEFAULT now(),
    actualizado_por text
);
ALTER TABLE whatsapp_pasarela ALTER COLUMN activa SET DEFAULT true;          -- por si la creó antes el modelo (create_all)
ALTER TABLE whatsapp_pasarela ALTER COLUMN actualizado SET DEFAULT now();

-- La consola Maestro lista las sesiones de la pasarela y necesita saber de qué empresa es cada una. RLS no le deja ver
-- empresas: esta función devuelve SOLO razón social y organización de los ids pedidos (nada de sus libros).
CREATE OR REPLACE FUNCTION maestro_empresas_whatsapp(p_ids integer[])
RETURNS TABLE(id integer, razon_social text, organizacion text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT e.id, e.razon_social, o.nombre FROM empresas e JOIN organizaciones o ON o.id = e.organizacion_id WHERE e.id = ANY(p_ids)
$$;
REVOKE ALL ON FUNCTION maestro_empresas_whatsapp(integer[]) FROM PUBLIC;
