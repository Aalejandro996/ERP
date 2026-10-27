"""Fases 2-3: cartera, inventario, fiscal Venezuela, bancos y auditoría. Dinero NUMERIC, fechas DATE, FKs por empresa."""
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint

from .extensions import db
from .models import _ahora

M = db.Numeric(18, 2)
Q = db.Numeric(18, 4)
CUENTAS_CFG = ["cxc", "cxp", "ventas", "compras_gastos", "iva_debito_fiscal", "iva_credito_fiscal", "caja_banco",
               "inventario", "costo_venta", "gasto_incobrables", "provision_incobrables", "gasto_islr", "islr_por_pagar",
               "igtf_por_pagar", "iva_retenido_por_clientes"]


class ConfigAsientos(db.Model):
    __tablename__ = "config_asientos"
    empresa_id = db.Column(db.ForeignKey("empresas.id"), primary_key=True)
    generar_automatico = db.Column(db.Boolean, nullable=False, default=True)
    for _c in CUENTAS_CFG:
        locals()[f"cuenta_{_c}"] = db.Column(db.Text)
    del _c

    def como_dict(self):
        return {f"cuenta_{c}": getattr(self, f"cuenta_{c}") for c in CUENTAS_CFG}


def _doc(prefijo, tercero):
    class _Doc(db.Model):
        __abstract__ = True
        id = db.Column(db.Integer, primary_key=True)
        empresa_id = db.Column(db.ForeignKey("empresas.id"), nullable=False, index=True)
        numero_documento = db.Column(db.Text, nullable=False)
        fecha_emision = db.Column(db.Date, nullable=False)
        fecha_vencimiento = db.Column(db.Date, nullable=False)
        monto = db.Column(M, nullable=False)
        saldo_pendiente = db.Column(M, nullable=False)
        estado = db.Column(db.Text, nullable=False, default="Pendiente")
        asiento_id = db.Column(db.BigInteger)
    return _Doc


class DocumentoCxC(_doc("cxc", "deudor"), db.Model):
    __tablename__ = "documentos_cxc"
    deudor_nombre = db.Column(db.Text, nullable=False)
    deudor_rif = db.Column(db.Text)
    __table_args__ = (CheckConstraint("saldo_pendiente >= 0 AND saldo_pendiente <= monto AND monto > 0", name="ck_cxc_saldo"),
                      UniqueConstraint("empresa_id", "numero_documento"))


class CobroCxC(db.Model):
    __tablename__ = "cobros_cxc"
    id = db.Column(db.Integer, primary_key=True)
    documento_id = db.Column(db.ForeignKey("documentos_cxc.id"), nullable=False, index=True)
    fecha = db.Column(db.Date, nullable=False)
    monto = db.Column(M, nullable=False)
    referencia = db.Column(db.Text)
    asiento_id = db.Column(db.BigInteger)
    __table_args__ = (CheckConstraint("monto > 0", name="ck_cobro_monto"),)


class DocumentoCxP(_doc("cxp", "proveedor"), db.Model):
    __tablename__ = "documentos_cxp"
    proveedor_nombre = db.Column(db.Text, nullable=False)
    proveedor_rif = db.Column(db.Text)
    # Datos fiscales de la factura del proveedor: alimentan el libro de compras (Art. 75 RLIVA)
    numero_control = db.Column(db.Text)
    tipo_documento = db.Column(db.Text, nullable=False, default="FAC")   # FAC | NC | ND
    documento_afectado = db.Column(db.Text)
    exento = db.Column(M, nullable=False, default=0)
    base_imponible = db.Column(M, nullable=False, default=0)
    alicuota = db.Column(db.Numeric(6, 2), nullable=False, default=0)
    monto_iva = db.Column(M, nullable=False, default=0)
    es_importacion = db.Column(db.Boolean, nullable=False, default=False)
    __table_args__ = (CheckConstraint("saldo_pendiente >= 0 AND saldo_pendiente <= monto AND monto > 0", name="ck_cxp_saldo"),
                      UniqueConstraint("empresa_id", "numero_documento"))


class PagoCxP(db.Model):
    __tablename__ = "pagos_cxp"
    id = db.Column(db.Integer, primary_key=True)
    documento_id = db.Column(db.ForeignKey("documentos_cxp.id"), nullable=False, index=True)
    fecha = db.Column(db.Date, nullable=False)
    monto = db.Column(M, nullable=False)
    referencia = db.Column(db.Text)
    asiento_id = db.Column(db.BigInteger)
    __table_args__ = (CheckConstraint("monto > 0", name="ck_pago_monto"),)


class TramoProvision(db.Model):  # política de provisión PROPIA de cada organización
    __tablename__ = "tramos_provision"
    id = db.Column(db.Integer, primary_key=True)
    organizacion_id = db.Column(db.ForeignKey("organizaciones.id"), nullable=False, index=True)
    dias_desde = db.Column(db.Integer, nullable=False)
    dias_hasta = db.Column(db.Integer, nullable=False)
    porcentaje = db.Column(db.Numeric(6, 2), nullable=False)
    __table_args__ = (CheckConstraint("dias_desde <= dias_hasta AND porcentaje BETWEEN 0 AND 100", name="ck_tramo"),)


class Producto(db.Model):
    __tablename__ = "productos"
    empresa_id = db.Column(db.ForeignKey("empresas.id"), primary_key=True)
    codigo = db.Column(db.Text, primary_key=True)
    nombre = db.Column(db.Text, nullable=False)
    metodo_costeo = db.Column(db.Text, nullable=False, default="Promedio")
    cantidad_actual = db.Column(Q, nullable=False, default=0)
    costo_promedio_actual = db.Column(Q, nullable=False, default=0)
    precio_venta = db.Column(Q, nullable=False, default=0)          # sugerido al facturar (moneda de la empresa)
    alicuota_iva = db.Column(db.String(1), nullable=False, default="G")
    __table_args__ = (CheckConstraint("metodo_costeo IN ('Promedio','PEPS') AND cantidad_actual >= 0", name="ck_producto"),
                      CheckConstraint("alicuota_iva IN ('G','R','A','E') AND precio_venta >= 0", name="ck_producto_venta"))


class MovInventario(db.Model):
    __tablename__ = "movimientos_inventario"
    id = db.Column(db.BigInteger, primary_key=True)
    empresa_id = db.Column(db.Integer, nullable=False)
    producto_codigo = db.Column(db.Text, nullable=False)
    tipo = db.Column(db.Text, nullable=False)
    cantidad = db.Column(Q, nullable=False)
    costo_unitario = db.Column(Q, nullable=False)
    fecha = db.Column(db.Date, nullable=False)
    referencia = db.Column(db.Text)
    __table_args__ = (CheckConstraint("tipo IN ('Entrada','Salida') AND cantidad > 0", name="ck_movinv"),
                      ForeignKeyConstraint(["empresa_id", "producto_codigo"], ["productos.empresa_id", "productos.codigo"]))


class LotePeps(db.Model):
    __tablename__ = "lotes_peps"
    id = db.Column(db.BigInteger, primary_key=True)
    empresa_id = db.Column(db.Integer, nullable=False)
    producto_codigo = db.Column(db.Text, nullable=False)
    fecha = db.Column(db.Date, nullable=False)
    cantidad_restante = db.Column(Q, nullable=False)
    costo_unitario = db.Column(Q, nullable=False)
    __table_args__ = (CheckConstraint("cantidad_restante >= 0", name="ck_lote"),
                      ForeignKeyConstraint(["empresa_id", "producto_codigo"], ["productos.empresa_id", "productos.codigo"]))


# ---- Parámetros fiscales GLOBALES (los mantiene el Administrador; no inventamos valores legales en el código) ----
class UnidadTributaria(db.Model):
    __tablename__ = "unidad_tributaria"
    id = db.Column(db.Integer, primary_key=True)
    valor_bs = db.Column(db.Numeric(18, 4), nullable=False)
    fecha_vigencia = db.Column(db.Date, nullable=False)
    gaceta_referencia = db.Column(db.Text)
    __table_args__ = (CheckConstraint("valor_bs > 0", name="ck_ut"),)


class TarifaISLR(db.Model):
    __tablename__ = "tarifa_islr_juridicas"
    id = db.Column(db.Integer, primary_key=True)
    desde_ut = db.Column(db.Numeric(18, 4), nullable=False)
    hasta_ut = db.Column(db.Numeric(18, 4), nullable=False)
    porcentaje = db.Column(db.Numeric(6, 2), nullable=False)
    sustraendo_ut = db.Column(db.Numeric(18, 4), nullable=False, default=0)


class ConceptoRetencionISLR(db.Model):
    __tablename__ = "conceptos_retencion_islr"
    id = db.Column(db.Integer, primary_key=True)
    concepto = db.Column(db.Text, nullable=False)
    codigo_seniat = db.Column(db.Text)
    porcentaje_juridica = db.Column(db.Numeric(6, 2), nullable=False)
    porcentaje_natural = db.Column(db.Numeric(6, 2), nullable=False)
    sustraendo_ut = db.Column(db.Numeric(18, 4), nullable=False, default=0)
    monto_minimo_ut = db.Column(db.Numeric(18, 4), nullable=False, default=0)


class IndiceINPC(db.Model):
    __tablename__ = "indice_inpc"
    id = db.Column(db.Integer, primary_key=True)
    valor_indice = db.Column(db.Numeric(38, 10), nullable=False)  # base dic-2007 = 100: hoy son miles de millones
    fecha = db.Column(db.Date, nullable=False, unique=True)
    referencia = db.Column(db.Text)


class AlicuotaImpuesto(db.Model):
    """Alícuotas vigentes (IVA general/reducida/adicional de lujo, IGTF). Las fija el Maestro según Gaceta Oficial."""
    __tablename__ = "alicuotas_impuesto"
    id = db.Column(db.Integer, primary_key=True)
    codigo = db.Column(db.Text, nullable=False)  # G | R | A | IGTF
    porcentaje = db.Column(db.Numeric(6, 2), nullable=False)
    fecha_vigencia = db.Column(db.Date, nullable=False)
    gaceta_referencia = db.Column(db.Text)
    __table_args__ = (CheckConstraint("codigo IN ('G','R','A','IGTF') AND porcentaje >= 0 AND porcentaje < 100", name="ck_alicuota"),
                      UniqueConstraint("codigo", "fecha_vigencia"))


class TasaCambio(db.Model):
    __tablename__ = "tasa_cambio"
    id = db.Column(db.Integer, primary_key=True)
    fecha = db.Column(db.Date, nullable=False, unique=True)              # fecha de vigencia publicada por el BCV
    tasa_usd_ves = db.Column(db.Numeric(18, 6), nullable=False)
    tasa_eur_ves = db.Column(db.Numeric(18, 6))
    fuente = db.Column(db.Text)                                          # "BCV vía DolarAPI" o "Manual (Maestro)"


class RetencionIVA(db.Model):
    __tablename__ = "retenciones_iva"
    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.ForeignKey("empresas.id"), nullable=False, index=True)
    rif_contribuyente = db.Column(db.Text, nullable=False)
    periodo_impositivo = db.Column(db.String(6), nullable=False)
    fecha_documento = db.Column(db.Date, nullable=False)
    tipo_operacion = db.Column(db.Text, nullable=False)
    tipo_documento = db.Column(db.Text, nullable=False)
    rif_proveedor = db.Column(db.Text, nullable=False)
    numero_documento = db.Column(db.Text, nullable=False)
    numero_control = db.Column(db.Text, nullable=False)
    monto_documento = db.Column(M, nullable=False)
    base_imponible = db.Column(M, nullable=False)
    monto_iva = db.Column(M, nullable=False)
    numero_documento_afectado = db.Column(db.Text, default="0")
    numero_comprobante = db.Column(db.Text)
    monto_exento = db.Column(M, default=0)
    alicuota = db.Column(M, nullable=False)
    numero_expediente = db.Column(db.Text, default="0")


class RetencionISLR(db.Model):
    __tablename__ = "retenciones_islr"
    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.ForeignKey("empresas.id"), nullable=False, index=True)
    fecha = db.Column(db.Date, nullable=False)
    concepto = db.Column(db.Text, nullable=False)
    codigo_seniat = db.Column(db.Text)
    beneficiario_nombre = db.Column(db.Text, nullable=False)
    beneficiario_rif = db.Column(db.Text, nullable=False)
    tipo_beneficiario = db.Column(db.Text, nullable=False)
    monto_pagado = db.Column(M, nullable=False)
    porcentaje_aplicado = db.Column(db.Numeric(6, 2), nullable=False)
    sustraendo_bs = db.Column(M, default=0)
    monto_retenido = db.Column(M, nullable=False)
    numero_comprobante = db.Column(db.Text)


class DocumentoFiscal(db.Model):  # libros de compra / venta
    __tablename__ = "documentos_fiscales"
    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.ForeignKey("empresas.id"), nullable=False, index=True)
    tipo_documento = db.Column(db.Text, nullable=False)  # 'Compra' | 'Venta'
    rif_tercero = db.Column(db.Text, nullable=False)
    razon_social_tercero = db.Column(db.Text, nullable=False)
    numero_factura = db.Column(db.Text, nullable=False)
    numero_control = db.Column(db.Text)
    fecha = db.Column(db.Date, nullable=False)
    base_imponible = db.Column(M, nullable=False)
    porcentaje_iva = db.Column(db.Numeric(6, 2), nullable=False, default=0)
    monto_iva = db.Column(M, nullable=False, default=0)
    total_operacion = db.Column(M, nullable=False)
    retencion_iva = db.Column(M, default=0)
    retencion_islr = db.Column(M, default=0)
    __table_args__ = (CheckConstraint("tipo_documento IN ('Compra','Venta')", name="ck_docfiscal_tipo"),)


class PartidaConciliacion(db.Model):
    __tablename__ = "partidas_conciliacion_fiscal"
    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.ForeignKey("empresas.id"), nullable=False, index=True)
    ejercicio_fiscal = db.Column(db.String(4), nullable=False)
    concepto = db.Column(db.Text, nullable=False)
    categoria = db.Column(db.Text)
    tipo = db.Column(db.Text, nullable=False)
    monto = db.Column(M, nullable=False)
    __table_args__ = (CheckConstraint("tipo IN ('Suma','Resta') AND monto >= 0", name="ck_partida"),)


class RebajaISLR(db.Model):
    __tablename__ = "rebajas_islr"
    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.ForeignKey("empresas.id"), nullable=False, index=True)
    ejercicio_fiscal = db.Column(db.String(4), nullable=False)
    concepto = db.Column(db.Text, nullable=False)
    monto_bs = db.Column(M, nullable=False)


class ExtractoBancario(db.Model):
    __tablename__ = "extracto_bancario"
    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.ForeignKey("empresas.id"), nullable=False, index=True)
    fecha = db.Column(db.Date, nullable=False)
    descripcion = db.Column(db.Text)
    monto = db.Column(M, nullable=False)
    conciliado = db.Column(db.Boolean, nullable=False, default=False)
    categoria_flujo = db.Column(db.Text, nullable=False, default="Operacion")


class Hallazgo(db.Model):
    __tablename__ = "hallazgos_auditoria"
    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.ForeignKey("empresas.id"), nullable=False, index=True)
    titulo = db.Column(db.Text, nullable=False)
    descripcion = db.Column(db.Text)
    cedula_ref = db.Column(db.Text)
    cuenta_codigo = db.Column(db.Text)
    severidad = db.Column(db.Text, nullable=False)
    estado = db.Column(db.Text, nullable=False, default="Abierto")
    responsable = db.Column(db.Text)
    auditor = db.Column(db.Text, nullable=False)
    creado_en = db.Column(db.DateTime(timezone=True), default=_ahora)
    cerrado_en = db.Column(db.DateTime(timezone=True))
    comentario_cierre = db.Column(db.Text)


class PapelTrabajo(db.Model):
    __tablename__ = "papeles_trabajo"
    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.ForeignKey("empresas.id"), nullable=False, index=True)
    cedula_tipo = db.Column(db.Text, nullable=False)
    cuenta_codigo = db.Column(db.Text)
    titulo = db.Column(db.Text, nullable=False)
    contenido = db.Column(db.Text)
    auditor = db.Column(db.Text, nullable=False)
    fecha = db.Column(db.DateTime(timezone=True), default=_ahora)
