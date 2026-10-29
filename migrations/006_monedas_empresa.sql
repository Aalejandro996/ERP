-- 006: divisas por empresa (USD, EUR, USDT) y USDT como moneda de facturación y de cobro. Idempotente.
ALTER TABLE empresas ADD COLUMN IF NOT EXISTS monedas text NOT NULL DEFAULT 'USD';
ALTER TABLE documentos_venta ALTER COLUMN moneda TYPE varchar(4);
ALTER TABLE pagos_documento_venta ALTER COLUMN moneda TYPE varchar(4);
ALTER TABLE documentos_venta DROP CONSTRAINT IF EXISTS ck_docventa_moneda;
ALTER TABLE documentos_venta ADD CONSTRAINT ck_docventa_moneda CHECK (moneda IN ('VES','USD','EUR','USDT') AND tasa_bcv > 0);
ALTER TABLE pagos_documento_venta DROP CONSTRAINT IF EXISTS ck_pago_venta;
ALTER TABLE pagos_documento_venta ADD CONSTRAINT ck_pago_venta CHECK (monto > 0 AND monto_bs > 0 AND moneda IN ('VES','USD','EUR','USDT'));
ALTER TABLE empresas DROP CONSTRAINT IF EXISTS ck_empresa_monedas;
ALTER TABLE empresas ADD CONSTRAINT ck_empresa_monedas CHECK (monedas ~ '^((USD|EUR|USDT)(,(USD|EUR|USDT))*)?$');
