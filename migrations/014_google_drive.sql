-- 014: Google Drive de Gigadevs (conexión OAuth y registro de lo que ya se subió). Tablas de plataforma, sin datos de clientes. Idempotente.
CREATE TABLE IF NOT EXISTS drive_conexion (
    id integer PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    correo text NOT NULL,
    refresh_token_cifrado text NOT NULL,
    conectado_por text NOT NULL,
    conectado_en timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS drive_archivos (
    clave text PRIMARY KEY,
    archivo_id text NOT NULL,
    ruta text NOT NULL,
    subido_en timestamptz NOT NULL DEFAULT now()
);
