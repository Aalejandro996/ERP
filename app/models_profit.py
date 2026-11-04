"""Integración de SOLO LECTURA con Profit Plus: el conector que corre en la red del cliente envía cada 15 minutos una foto
de artículos, existencias por almacén, precios de venta y clientes; Contax la guarda aquí y el bot de WhatsApp la usa como
catálogo. Nada se escribe ni se envía a Profit Plus. Las tablas de empresa quedan aisladas por organización (RLS, migración 012)."""
from sqlalchemy import CheckConstraint

from .extensions import db
from .models import _ahora

Q = db.Numeric(18, 4)


class ProfitPlataforma(db.Model):
    """API del conector para toda la plataforma (una fila). Solo la edita el Maestro."""
    __tablename__ = "profit_plataforma"
    id = db.Column(db.Integer, primary_key=True, default=1)
    activa = db.Column(db.Boolean, nullable=False, default=True, server_default=db.text("true"))
    url_publica = db.Column(db.Text)                     # la que se escribe en el conector.ini (vacía = APP_BASE_URL)
    url_instalador = db.Column(db.Text)                  # descarga del contax_profit.exe compilado (opcional)
    intervalo_min = db.Column(db.Integer, nullable=False, default=15, server_default=db.text("15"))
    max_articulos = db.Column(db.Integer, nullable=False, default=20000, server_default=db.text("20000"))
    max_clientes = db.Column(db.Integer, nullable=False, default=50000, server_default=db.text("50000"))
    actualizado = db.Column(db.DateTime(timezone=True), default=_ahora, onupdate=_ahora, nullable=False, server_default=db.text("now()"))
    actualizado_por = db.Column(db.Text)
    __table_args__ = (CheckConstraint("id = 1", name="ck_profit_plataforma_unica"),
                      CheckConstraint("intervalo_min BETWEEN 5 AND 1440", name="ck_profit_intervalo"))


class ProfitConexion(db.Model):
    """Conector de Profit Plus de una empresa. El secreto con que el conector firma va cifrado (Fernet)."""
    __tablename__ = "profit_conexion"
    empresa_id = db.Column(db.ForeignKey("empresas.id"), primary_key=True)
    conector_id = db.Column(db.Text, nullable=False, unique=True)     # público: viaja en cada petición del conector
    secreto_cifrado = db.Column(db.Text, nullable=False)
    activo = db.Column(db.Boolean, nullable=False, default=True)
    almacenes = db.Column(db.JSON, nullable=False, default=list)       # co_alma que cuentan para la existencia ([] = todos los de venta)
    lista_precio = db.Column(db.Text, nullable=False, default="01")    # co_precio de saArtPrecio
    restar_comprometido = db.Column(db.Boolean, nullable=False, default=True)
    solo_con_existencia = db.Column(db.Boolean, nullable=False, default=False)   # el bot ofrece solo lo que hay
    disponibles = db.Column(db.JSON, nullable=False, default=dict)     # almacenes y listas de precio que informó el conector
    ultima_sync = db.Column(db.DateTime(timezone=True))
    ultimo_contacto = db.Column(db.DateTime(timezone=True))
    ultimo_estado = db.Column(db.Text)                                 # ok | error
    ultimo_error = db.Column(db.Text)
    agente_version = db.Column(db.Text)
    agente_equipo = db.Column(db.Text)
    n_articulos = db.Column(db.Integer, nullable=False, default=0)
    n_clientes = db.Column(db.Integer, nullable=False, default=0)
    creado = db.Column(db.DateTime(timezone=True), default=_ahora, nullable=False)


class ProfitArticulo(db.Model):
    __tablename__ = "profit_articulos"
    empresa_id = db.Column(db.ForeignKey("empresas.id"), primary_key=True)
    codigo = db.Column(db.Text, primary_key=True)          # co_art
    nombre = db.Column(db.Text, nullable=False)            # art_des
    linea = db.Column(db.Text)                             # co_lin
    alicuota = db.Column(db.String(1), nullable=False, default="G")
    precio = db.Column(Q, nullable=False, default=0)       # de la lista elegida, en su moneda
    moneda = db.Column(db.String(3), nullable=False, default="VES")
    existencia = db.Column(Q, nullable=False, default=0)   # suma de los almacenes elegidos (actual − comprometido)
    por_almacen = db.Column(db.JSON, nullable=False, default=dict)
    __table_args__ = (CheckConstraint("alicuota IN ('G','R','A','E') AND moneda IN ('VES','USD','EUR')", name="ck_profit_art"),)


class ProfitCliente(db.Model):
    __tablename__ = "profit_clientes"
    empresa_id = db.Column(db.ForeignKey("empresas.id"), primary_key=True)
    codigo = db.Column(db.Text, primary_key=True)          # co_cli
    nombre = db.Column(db.Text, nullable=False)
    rif = db.Column(db.Text, index=True)
    direccion = db.Column(db.Text)
    telefonos = db.Column(db.Text)
    whatsapp = db.Column(db.Text, index=True)              # primer teléfono normalizado (58412…), para reconocer al que escribe
    email = db.Column(db.Text)


class ProfitSincronizacion(db.Model):
    """Bitácora técnica de cada envío del conector (lo ven la empresa y, en conteos, el Maestro)."""
    __tablename__ = "profit_sincronizaciones"
    id = db.Column(db.BigInteger, primary_key=True)
    empresa_id = db.Column(db.ForeignKey("empresas.id"), nullable=False, index=True)
    ts = db.Column(db.DateTime(timezone=True), default=_ahora, nullable=False)
    resultado = db.Column(db.Text, nullable=False)          # ok | error | ping
    articulos = db.Column(db.Integer, nullable=False, default=0)
    clientes = db.Column(db.Integer, nullable=False, default=0)
    duracion_ms = db.Column(db.Integer)
    detalle = db.Column(db.Text)
    ip = db.Column(db.Text)
