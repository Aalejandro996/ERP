"""Compras (proveedores y órdenes de compra) y canal de WhatsApp (bot, contactos, mensajes y pedidos).
Todo cuelga de una empresa y queda aislado por organización con Row-Level Security (migración 007)."""
from sqlalchemy import CheckConstraint, UniqueConstraint

from .extensions import db
from .models import _ahora

M = db.Numeric(18, 2)
Q = db.Numeric(18, 4)
ESTADOS_OC = ("Emitida", "Enviada", "Confirmada", "Recibida", "Rechazada", "Anulada")
ESTADOS_PEDIDO = ("Nuevo", "Confirmado", "Despachado", "Rechazado", "Convertido")


class Proveedor(db.Model):
    __tablename__ = "proveedores"
    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.ForeignKey("empresas.id"), nullable=False, index=True)
    rif = db.Column(db.Text, nullable=False)
    razon_social = db.Column(db.Text, nullable=False)
    direccion = db.Column(db.Text)
    telefono = db.Column(db.Text)
    email = db.Column(db.Text)
    whatsapp = db.Column(db.Text)          # solo dígitos, con código de país (58412…)
    contacto = db.Column(db.Text)
    __table_args__ = (UniqueConstraint("empresa_id", "rif", name="uq_proveedor_empresa_rif"),)


class OrdenCompra(db.Model):
    """Orden de compra emitida POR la empresa (emisor) A un proveedor o cliente (destinatario). Los datos del destinatario
    se copian al emitir: la orden no cambia si luego cambia la ficha del proveedor."""
    __tablename__ = "ordenes_compra"
    id = db.Column(db.BigInteger, primary_key=True)
    empresa_id = db.Column(db.ForeignKey("empresas.id"), nullable=False, index=True)
    numero = db.Column(db.Integer, nullable=False)
    fecha = db.Column(db.Date, nullable=False)
    fecha_entrega = db.Column(db.Date)
    tipo_destinatario = db.Column(db.Text, nullable=False, default="proveedor")
    dest_rif = db.Column(db.Text, nullable=False)
    dest_nombre = db.Column(db.Text, nullable=False)
    dest_direccion = db.Column(db.Text)
    dest_telefono = db.Column(db.Text)
    dest_email = db.Column(db.Text)
    dest_whatsapp = db.Column(db.Text)
    moneda = db.Column(db.String(4), nullable=False, default="VES")
    tasa_bcv = db.Column(db.Numeric(18, 6), nullable=False, default=1)
    condicion_pago = db.Column(db.Text)
    lugar_entrega = db.Column(db.Text)
    observaciones = db.Column(db.Text)
    exento = db.Column(M, nullable=False, default=0)
    base = db.Column(M, nullable=False, default=0)
    iva = db.Column(M, nullable=False, default=0)
    total = db.Column(M, nullable=False, default=0)          # en la moneda de la orden
    total_bs = db.Column(M, nullable=False, default=0)
    estado = db.Column(db.Text, nullable=False, default="Emitida")
    origen = db.Column(db.Text, nullable=False, default="manual")
    pedido_id = db.Column(db.BigInteger)
    motivo = db.Column(db.Text)
    creado_por = db.Column(db.Text, nullable=False)
    creado_en = db.Column(db.DateTime(timezone=True), default=_ahora, nullable=False)
    enviado_en = db.Column(db.DateTime(timezone=True))
    lineas = db.relationship("LineaOrdenCompra", order_by="LineaOrdenCompra.id", cascade="all, delete-orphan")
    __table_args__ = (UniqueConstraint("empresa_id", "numero", name="uq_oc_empresa_numero"),
                      CheckConstraint(f"estado IN {ESTADOS_OC}", name="ck_oc_estado"),
                      CheckConstraint("tipo_destinatario IN ('proveedor','cliente')", name="ck_oc_destinatario"),
                      CheckConstraint("moneda IN ('VES','USD','EUR','USDT') AND tasa_bcv > 0 AND total >= 0", name="ck_oc_montos"))


class LineaOrdenCompra(db.Model):
    __tablename__ = "lineas_orden_compra"
    id = db.Column(db.BigInteger, primary_key=True)
    orden_id = db.Column(db.ForeignKey("ordenes_compra.id", ondelete="CASCADE"), nullable=False, index=True)
    empresa_id = db.Column(db.ForeignKey("empresas.id"), nullable=False)
    codigo = db.Column(db.Text)              # código de producto del inventario (opcional): al recibir entra al kardex
    descripcion = db.Column(db.Text, nullable=False)
    unidad = db.Column(db.Text)
    cantidad = db.Column(Q, nullable=False)
    precio = db.Column(Q, nullable=False)    # unitario sin IVA, en la moneda de la orden
    alicuota = db.Column(db.String(1), nullable=False, default="G")
    monto = db.Column(M, nullable=False)
    __table_args__ = (CheckConstraint("cantidad > 0 AND precio >= 0 AND alicuota IN ('G','R','A','E')", name="ck_linea_oc"),)


class WhatsappConfig(db.Model):
    """Número de WhatsApp Business (Cloud API de Meta) de una empresa. Token y secreto cifrados en reposo (Fernet)."""
    __tablename__ = "whatsapp_config"
    empresa_id = db.Column(db.ForeignKey("empresas.id"), primary_key=True)
    phone_number_id = db.Column(db.Text, nullable=False)
    numero_visible = db.Column(db.Text)
    token_cifrado = db.Column(db.Text, nullable=False)
    app_secret_cifrado = db.Column(db.Text, nullable=False)
    verify_token = db.Column(db.Text, nullable=False)
    clave_webhook = db.Column(db.Text, nullable=False, unique=True)   # parte secreta de la URL del webhook
    bienvenida = db.Column(db.Text)
    activo = db.Column(db.Boolean, nullable=False, default=True)
    actualizado = db.Column(db.DateTime(timezone=True), default=_ahora)


class WhatsappContacto(db.Model):
    __tablename__ = "whatsapp_contactos"
    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.ForeignKey("empresas.id"), nullable=False, index=True)
    telefono = db.Column(db.Text, nullable=False)
    nombre = db.Column(db.Text)
    tipo = db.Column(db.Text, nullable=False, default="nuevo")
    rif = db.Column(db.Text)
    estado = db.Column(db.JSON, nullable=False, default=dict)   # conversación en curso (paso, carrito, oferta)
    ultimo_mensaje = db.Column(db.DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("empresa_id", "telefono", name="uq_wa_contacto"),
                      CheckConstraint("tipo IN ('nuevo','cliente','proveedor','bloqueado')", name="ck_wa_tipo"))


class WhatsappMensaje(db.Model):
    __tablename__ = "whatsapp_mensajes"
    id = db.Column(db.BigInteger, primary_key=True)
    empresa_id = db.Column(db.ForeignKey("empresas.id"), nullable=False, index=True)
    contacto_id = db.Column(db.ForeignKey("whatsapp_contactos.id"), index=True)
    telefono = db.Column(db.Text, nullable=False)
    direccion = db.Column(db.String(3), nullable=False)        # in | out
    texto = db.Column(db.Text, nullable=False)
    wamid = db.Column(db.Text, unique=True)                    # id de Meta: un mensaje entrante se procesa una sola vez
    resultado = db.Column(db.Text)                             # envío: ok / error
    ts = db.Column(db.DateTime(timezone=True), default=_ahora, nullable=False)


class Pedido(db.Model):
    """Pedido recibido por WhatsApp: de un cliente (venta) o una oferta/cotización de un proveedor (compra)."""
    __tablename__ = "pedidos"
    id = db.Column(db.BigInteger, primary_key=True)
    empresa_id = db.Column(db.ForeignKey("empresas.id"), nullable=False, index=True)
    numero = db.Column(db.Integer, nullable=False)
    tipo = db.Column(db.Text, nullable=False)                  # venta | compra
    origen = db.Column(db.Text, nullable=False, default="whatsapp")
    contacto_id = db.Column(db.ForeignKey("whatsapp_contactos.id"))
    telefono = db.Column(db.Text)
    nombre = db.Column(db.Text)
    lineas = db.Column(db.JSON, nullable=False, default=list)  # [{codigo, descripcion, cantidad, precio}]
    total = db.Column(M, nullable=False, default=0)            # Bs (precio de venta del inventario) o el ofertado
    nota = db.Column(db.Text)
    estado = db.Column(db.Text, nullable=False, default="Nuevo")
    orden_compra_id = db.Column(db.BigInteger)
    creado_en = db.Column(db.DateTime(timezone=True), default=_ahora, nullable=False)
    __table_args__ = (UniqueConstraint("empresa_id", "numero", name="uq_pedido_empresa_numero"),
                      CheckConstraint("tipo IN ('venta','compra')", name="ck_pedido_tipo"),
                      CheckConstraint(f"estado IN {ESTADOS_PEDIDO}", name="ck_pedido_estado"))
