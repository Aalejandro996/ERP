"""Módulo de facturación (Venezuela). Base normativa: Providencia Administrativa SNAT/2011/00071 (normas generales de
emisión de facturas y otros documentos), Ley y Reglamento del IVA (libro de ventas, Art. 70-72 RLIVA), Ley de IGTF
(percepción sobre pagos en divisas por Sujetos Pasivos Especiales) y Providencia SNAT/2024/000102 (documentos
digitales mediante imprenta digital autorizada). Los documentos son de solo-inserción: solo cambian los campos de
estado/cobro (lo hace cumplir un trigger, migración 002). Anular no borra: deja el documento con estado "Anulado".
"""
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint

from .extensions import db
from .models import _ahora

M = db.Numeric(18, 2)
Q = db.Numeric(18, 4)
TIPOS_DOC = ("FAC", "NC", "ND")
NOMBRE_DOC = {"FAC": "Factura", "NC": "Nota de crédito", "ND": "Nota de débito"}


class ClienteFact(db.Model):
    """Catálogo de adquirentes de cada empresa (para no reescribir RIF/dirección en cada factura)."""
    __tablename__ = "clientes_facturacion"
    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.ForeignKey("empresas.id"), nullable=False, index=True)
    rif = db.Column(db.Text, nullable=False)
    razon_social = db.Column(db.Text, nullable=False)
    direccion = db.Column(db.Text)
    telefono = db.Column(db.Text)
    email = db.Column(db.Text)
    es_contribuyente_especial = db.Column(db.Boolean, nullable=False, default=False)  # agente de retención de IVA
    __table_args__ = (UniqueConstraint("empresa_id", "rif"),)


class SerieFacturacion(db.Model):
    """Numeración consecutiva y única por serie (Art. 13 num. 3 Prov. 0071) y rango de números de control asignado
    por la imprenta (digital) autorizada. Se bloquea la fila (FOR UPDATE) al emitir: nunca hay saltos ni duplicados."""
    __tablename__ = "series_facturacion"
    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.ForeignKey("empresas.id"), nullable=False, index=True)
    serie = db.Column(db.Text, nullable=False, default="")  # "" = sin serie
    siguiente_fac = db.Column(db.BigInteger, nullable=False, default=1)
    siguiente_nc = db.Column(db.BigInteger, nullable=False, default=1)
    siguiente_nd = db.Column(db.BigInteger, nullable=False, default=1)
    control_prefijo = db.Column(db.Text, nullable=False, default="00")
    control_siguiente = db.Column(db.BigInteger, nullable=False)
    control_hasta = db.Column(db.BigInteger, nullable=False)
    imprenta_razon_social = db.Column(db.Text, nullable=False)
    imprenta_rif = db.Column(db.Text, nullable=False)
    imprenta_providencia = db.Column(db.Text, nullable=False)   # N° y fecha de la providencia que autoriza a la imprenta
    fecha_asignacion = db.Column(db.Date, nullable=False)       # fecha de asignación del rango de control
    activa = db.Column(db.Boolean, nullable=False, default=True)
    __table_args__ = (UniqueConstraint("empresa_id", "serie"),
                      CheckConstraint("control_siguiente >= 1 AND control_siguiente <= control_hasta + 1", name="ck_serie_control"))


class DocumentoVenta(db.Model):
    __tablename__ = "documentos_venta"
    id = db.Column(db.BigInteger, primary_key=True)
    empresa_id = db.Column(db.ForeignKey("empresas.id"), nullable=False, index=True)
    serie_id = db.Column(db.ForeignKey("series_facturacion.id"), nullable=False)
    tipo = db.Column(db.Text, nullable=False)
    serie = db.Column(db.Text, nullable=False, default="")
    numero = db.Column(db.BigInteger, nullable=False)
    numero_control = db.Column(db.Text, nullable=False)
    fecha_emision = db.Column(db.Date, nullable=False, index=True)
    emitido_en = db.Column(db.DateTime(timezone=True), default=_ahora, nullable=False)  # hora de emisión
    # Adquirente (copia fija: si luego cambia el catálogo, el documento emitido NO cambia)
    cliente_rif = db.Column(db.Text, nullable=False)
    cliente_nombre = db.Column(db.Text, nullable=False)
    cliente_direccion = db.Column(db.Text)
    cliente_telefono = db.Column(db.Text)
    # Moneda: la operación puede expresarse en divisas, pero el documento muestra el equivalente en Bs a la tasa BCV
    moneda = db.Column(db.String(4), nullable=False, default="VES")
    tasa_bcv = db.Column(db.Numeric(18, 6), nullable=False, default=1)
    condicion = db.Column(db.Text, nullable=False)  # Contado | Credito
    fecha_vencimiento = db.Column(db.Date)
    # Totales en Bs (moneda funcional, base de libros y declaraciones)
    exento = db.Column(M, nullable=False, default=0)
    base_g = db.Column(M, nullable=False, default=0)
    iva_g = db.Column(M, nullable=False, default=0)
    base_r = db.Column(M, nullable=False, default=0)
    iva_r = db.Column(M, nullable=False, default=0)
    base_a = db.Column(M, nullable=False, default=0)
    iva_a = db.Column(M, nullable=False, default=0)
    pct_g = db.Column(db.Numeric(6, 2), nullable=False, default=0)
    pct_r = db.Column(db.Numeric(6, 2), nullable=False, default=0)
    pct_a = db.Column(db.Numeric(6, 2), nullable=False, default=0)  # adicional de lujo (se suma a la general)
    descuento = db.Column(M, nullable=False, default=0)
    total = db.Column(M, nullable=False)              # base + IVA (sin IGTF)
    total_divisa = db.Column(M)                       # total en la moneda de la operación (si no es VES)
    igtf_pct = db.Column(db.Numeric(6, 2), nullable=False, default=0)
    igtf_base = db.Column(M, nullable=False, default=0)
    igtf = db.Column(M, nullable=False, default=0)
    total_a_pagar = db.Column(M, nullable=False)      # total + IGTF
    # Referencias
    documento_afectado_id = db.Column(db.ForeignKey("documentos_venta.id"))  # NC/ND → factura
    motivo = db.Column(db.Text)
    observaciones = db.Column(db.Text)
    asiento_id = db.Column(db.BigInteger)
    cxc_id = db.Column(db.ForeignKey("documentos_cxc.id"))
    creado_por = db.Column(db.ForeignKey("usuarios.id"), nullable=False)
    hash_prev = db.Column(db.String(64), nullable=False)
    hash = db.Column(db.String(64), nullable=False)    # cadena por empresa: un documento alterado o suprimido se detecta
    # ---- únicos campos modificables (trigger) ----
    estado = db.Column(db.Text, nullable=False, default="Emitido")  # Emitido | Anulado
    saldo_pendiente = db.Column(M, nullable=False, default=0)
    iva_retenido = db.Column(M, nullable=False, default=0)          # retención practicada por el cliente (Prov. 0049)
    comprobante_retencion = db.Column(db.Text)
    anulado_en = db.Column(db.DateTime(timezone=True))
    anulado_por = db.Column(db.ForeignKey("usuarios.id"))
    motivo_anulacion = db.Column(db.Text)
    __table_args__ = (
        UniqueConstraint("empresa_id", "tipo", "serie", "numero", name="uq_docventa_numero"),
        UniqueConstraint("empresa_id", "numero_control", name="uq_docventa_control"),
        UniqueConstraint("id", "empresa_id", name="uq_docventa_empresa"),
        CheckConstraint("tipo IN ('FAC','NC','ND')", name="ck_docventa_tipo"),
        CheckConstraint("condicion IN ('Contado','Credito')", name="ck_docventa_condicion"),
        CheckConstraint("estado IN ('Emitido','Anulado')", name="ck_docventa_estado"),
        CheckConstraint("moneda IN ('VES','USD','EUR','USDT') AND tasa_bcv > 0", name="ck_docventa_moneda"),
        CheckConstraint("total >= 0 AND total_a_pagar >= total AND saldo_pendiente >= 0 AND saldo_pendiente <= total_a_pagar", name="ck_docventa_montos"),
        CheckConstraint("(tipo = 'FAC') = (documento_afectado_id IS NULL)", name="ck_docventa_afectado"),
    )


class LineaDocumentoVenta(db.Model):
    __tablename__ = "lineas_documento_venta"
    id = db.Column(db.BigInteger, primary_key=True)
    documento_id = db.Column(db.BigInteger, nullable=False, index=True)
    empresa_id = db.Column(db.Integer, nullable=False)
    producto_codigo = db.Column(db.Text)
    descripcion = db.Column(db.Text, nullable=False)
    cantidad = db.Column(Q, nullable=False)
    precio_unitario = db.Column(Q, nullable=False)    # en la moneda del documento
    descuento_pct = db.Column(db.Numeric(6, 2), nullable=False, default=0)
    alicuota = db.Column(db.String(1), nullable=False)  # G | R | A | E  — "(E)" exento se indica en la factura
    base_bs = db.Column(M, nullable=False)
    iva_bs = db.Column(M, nullable=False)
    costo_unitario = db.Column(Q)                     # costo de venta aplicado (para devoluciones)
    __table_args__ = (
        ForeignKeyConstraint(["documento_id", "empresa_id"], ["documentos_venta.id", "documentos_venta.empresa_id"]),
        CheckConstraint("cantidad > 0 AND precio_unitario >= 0 AND descuento_pct BETWEEN 0 AND 100 AND alicuota IN ('G','R','A','E')", name="ck_linea_venta"),
    )


class PagoDocumentoVenta(db.Model):
    __tablename__ = "pagos_documento_venta"
    id = db.Column(db.BigInteger, primary_key=True)
    documento_id = db.Column(db.BigInteger, nullable=False, index=True)
    empresa_id = db.Column(db.Integer, nullable=False)
    fecha = db.Column(db.Date, nullable=False)
    forma = db.Column(db.Text, nullable=False)
    moneda = db.Column(db.String(4), nullable=False)
    monto = db.Column(M, nullable=False)               # en la moneda del pago, aplicado al documento (sin IGTF)
    monto_bs = db.Column(M, nullable=False)
    igtf_bs = db.Column(M, nullable=False, default=0)
    referencia = db.Column(db.Text)
    __table_args__ = (
        ForeignKeyConstraint(["documento_id", "empresa_id"], ["documentos_venta.id", "documentos_venta.empresa_id"]),
        CheckConstraint("monto > 0 AND monto_bs > 0 AND moneda IN ('VES','USD','EUR','USDT')", name="ck_pago_venta"),
    )
