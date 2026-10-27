-- 004: INPC con precisión suficiente (base dic-2007 = 100 ya supera 10^9) y tasa BCV del euro con su fuente.
-- Idempotente. Ejecutar con el rol DUEÑO tras db.create_all() (lo hace scripts/inicializar.py).
ALTER TABLE indice_inpc ALTER COLUMN valor_indice TYPE numeric(38,10);
ALTER TABLE tasa_cambio ADD COLUMN IF NOT EXISTS tasa_eur_ves numeric(18,6);
ALTER TABLE tasa_cambio ADD COLUMN IF NOT EXISTS fuente text;
ALTER TABLE tasa_cambio DROP CONSTRAINT IF EXISTS ck_tasa_positiva;
ALTER TABLE tasa_cambio ADD CONSTRAINT ck_tasa_positiva CHECK (tasa_usd_ves > 0 AND (tasa_eur_ves IS NULL OR tasa_eur_ves > 0));
ALTER TABLE indice_inpc DROP CONSTRAINT IF EXISTS ck_inpc_positivo;
ALTER TABLE indice_inpc ADD CONSTRAINT ck_inpc_positivo CHECK (valor_indice > 0);
