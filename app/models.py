"""Modelo de datos (fase 1: núcleo contable). Dinero = NUMERIC, fechas = DATE, integridad en la BD."""
from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.dialects.postgresql import CITEXT

from .extensions import db

ROLES = ("Maestro", "Administrador", "Contador", "Auxiliar", "Auditor Financiero", "Cajero")
ROLES_CLIENTE = ROLES[1:]  # el Maestro es del proveedor de la plataforma y no pertenece a ninguna organización


def _ahora():
    return datetime.now(timezone.utc)


class Organizacion(db.Model):
    """Cliente de la plataforma (tenant). Todo dato de negocio cuelga de una organización y nunca se cruza con otra:
    lo garantiza la aplicación (filtros + verificación por petición) y PostgreSQL (Row-Level Security, migración 002)."""
    __tablename__ = "organizaciones"
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.Text, nullable=False)
    rif = db.Column(db.Text, nullable=False, unique=True)
    correo_contacto = db.Column(CITEXT)
    estado = db.Column(db.Text, nullable=False, default="activa")
    creado_en = db.Column(db.DateTime(timezone=True), default=_ahora)
    __table_args__ = (CheckConstraint("estado IN ('activa','suspendida')", name="ck_org_estado"),)


class Usuario(db.Model):
    __tablename__ = "usuarios"
    id = db.Column(db.Integer, primary_key=True)
    organizacion_id = db.Column(db.ForeignKey("organizaciones.id"), index=True)  # NULL solo para Maestro
    username = db.Column(CITEXT, unique=True, nullable=False)
    email = db.Column(CITEXT, unique=True)
    email_verificado = db.Column(db.Boolean, nullable=False, default=False)
    sesion_version = db.Column(db.Integer, nullable=False, default=1)  # +1 al cambiar clave/MFA: cierra todas las sesiones
    ultimo_ip = db.Column(db.Text)
    password_hash = db.Column(db.Text, nullable=False)  # argon2id, o "legacy$pbkdf2$..." hasta el 1.er login
    rol = db.Column(db.Text, nullable=False)
    activo = db.Column(db.Boolean, nullable=False, default=True)
    debe_cambiar_clave = db.Column(db.Boolean, nullable=False, default=True)
    mfa_activo = db.Column(db.Boolean, nullable=False, default=False)
    mfa_secreto_cifrado = db.Column(db.Text)            # TOTP cifrado con Fernet (nunca en claro)
    mfa_ultimo_contador = db.Column(db.BigInteger, nullable=False, default=0)  # anti-reutilización de códigos
    intentos_fallidos = db.Column(db.Integer, nullable=False, default=0)
    bloqueado_hasta = db.Column(db.DateTime(timezone=True))
    ultimo_login = db.Column(db.DateTime(timezone=True))
    creado_en = db.Column(db.DateTime(timezone=True), default=_ahora)
    empresas = db.relationship("UsuarioEmpresa", back_populates="usuario", cascade="all, delete-orphan")
    organizacion = db.relationship("Organizacion")
    __table_args__ = (CheckConstraint(f"rol IN {ROLES}", name="ck_usuarios_rol"),
                      CheckConstraint("(rol = 'Maestro') = (organizacion_id IS NULL)", name="ck_usuarios_org"))


class Empresa(db.Model):  # = clientes_contables del sistema original
    __tablename__ = "empresas"
    id = db.Column(db.Integer, primary_key=True)
    organizacion_id = db.Column(db.ForeignKey("organizaciones.id"), nullable=False, index=True)
    razon_social = db.Column(db.Text, nullable=False)
    rif = db.Column(db.Text, nullable=False)  # único POR organización: dos clientes pueden llevar la misma empresa sin enterarse
    direccion = db.Column(db.Text)
    telefono = db.Column(db.Text)
    es_sujeto_pasivo_especial = db.Column(db.Boolean, nullable=False, default=False)
    monedas = db.Column(db.Text, nullable=False, default="USD")   # divisas con las que factura la empresa (además del bolívar): USD,EUR,USDT

    def divisas(self) -> list[str]:
        return [m for m in ("USD", "EUR", "USDT") if m in (self.monedas or "").split(",")]
    creado_en = db.Column(db.DateTime(timezone=True), default=_ahora)
    __table_args__ = (UniqueConstraint("organizacion_id", "rif", name="uq_empresa_org_rif"),)


class UsuarioEmpresa(db.Model):
    """Asignación explícita usuario↔empresa. En el original, TODO usuario veía TODAS las empresas."""
    __tablename__ = "usuario_empresa"
    usuario_id = db.Column(db.ForeignKey("usuarios.id", ondelete="CASCADE"), primary_key=True)
    empresa_id = db.Column(db.ForeignKey("empresas.id", ondelete="CASCADE"), primary_key=True)
    usuario = db.relationship("Usuario", back_populates="empresas")


class Cuenta(db.Model):
    __tablename__ = "cuentas"
    empresa_id = db.Column(db.ForeignKey("empresas.id"), primary_key=True)
    codigo = db.Column(db.Text, primary_key=True)
    nombre = db.Column(db.Text, nullable=False)
    tipo = db.Column(db.Text, nullable=False)
    categoria_niif18 = db.Column(db.Text, nullable=False, default="Operacion")
    es_partida_monetaria = db.Column(db.Boolean, nullable=False, default=False)


class Asiento(db.Model):
    __tablename__ = "asientos"
    id = db.Column(db.BigInteger, primary_key=True)
    empresa_id = db.Column(db.ForeignKey("empresas.id"), nullable=False, index=True)
    fecha = db.Column(db.Date, nullable=False, index=True)
    concepto = db.Column(db.Text, nullable=False)
    creado_por = db.Column(db.ForeignKey("usuarios.id"), nullable=False)
    creado_en = db.Column(db.DateTime(timezone=True), default=_ahora, nullable=False)
    revierte_a = db.Column(db.ForeignKey("asientos.id"))  # anulación = contra-asiento, nunca DELETE
    movimientos = db.relationship("Movimiento", back_populates="asiento")
    __table_args__ = (UniqueConstraint("id", "empresa_id", name="uq_asiento_empresa"),)


class Movimiento(db.Model):
    __tablename__ = "movimientos"
    id = db.Column(db.BigInteger, primary_key=True)
    asiento_id = db.Column(db.BigInteger, nullable=False, index=True)
    empresa_id = db.Column(db.Integer, nullable=False)
    cuenta_codigo = db.Column(db.Text, nullable=False)
    debe = db.Column(db.Numeric(18, 2), nullable=False, default=0)
    haber = db.Column(db.Numeric(18, 2), nullable=False, default=0)
    asiento = db.relationship("Asiento", back_populates="movimientos")
    __table_args__ = (
        CheckConstraint("debe >= 0 AND haber >= 0 AND NOT (debe > 0 AND haber > 0)", name="ck_mov_montos"),
        # FKs compuestas: un movimiento solo puede apuntar a asiento y cuenta de SU MISMA empresa
        ForeignKeyConstraint(["asiento_id", "empresa_id"], ["asientos.id", "asientos.empresa_id"]),
        ForeignKeyConstraint(["empresa_id", "cuenta_codigo"], ["cuentas.empresa_id", "cuentas.codigo"]),
    )


class PeriodoCerrado(db.Model):
    __tablename__ = "periodos_cerrados"
    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.ForeignKey("empresas.id"), nullable=False)
    mes_anio = db.Column(db.String(7), nullable=False)  # 'AAAA-MM'
    cerrado_por = db.Column(db.ForeignKey("usuarios.id"), nullable=False)
    __table_args__ = (UniqueConstraint("empresa_id", "mes_anio"),)


class AuditLog(db.Model):
    """Bitácora encadenada por hash: alterar o borrar una fila rompe la cadena y se detecta."""
    __tablename__ = "audit_log"
    id = db.Column(db.BigInteger, primary_key=True)
    organizacion_id = db.Column(db.Integer, index=True)  # una cadena de hashes por organización (NULL = plataforma)
    ts = db.Column(db.DateTime(timezone=True), default=_ahora, nullable=False)
    usuario = db.Column(db.Text, nullable=False)
    ip = db.Column(db.Text)
    accion = db.Column(db.Text, nullable=False)
    detalles = db.Column(db.Text)
    hash_prev = db.Column(db.String(64), nullable=False)
    hash = db.Column(db.String(64), nullable=False)
    __table_args__ = {"implicit_returning": False}  # sin RETURNING: RLS permite insertar en cadenas que no se pueden leer


class LicenciaLocal(db.Model):
    __tablename__ = "licencia_local"
    id = db.Column(db.Integer, primary_key=True)
    datos = db.Column(db.JSON, nullable=False, default=dict)   # (heredado: licencia única de instalación)
    instance_secret = db.Column(db.String(64), nullable=False)  # base de la huella de instalación


class LicenciaOrganizacion(db.Model):
    """Licencia de CADA cliente: {"key","token","last_seen"}. El plan y los topes salen del token firmado."""
    __tablename__ = "licencias_organizacion"
    organizacion_id = db.Column(db.ForeignKey("organizaciones.id", ondelete="CASCADE"), primary_key=True)
    datos = db.Column(db.JSON, nullable=False, default=dict)


class Equipo(db.Model):
    """Equipo (navegador) autorizado de una organización. La licencia fija cuántos puede haber (claim max_equipos).
    El identificador vive en una cookie HttpOnly; en la BD solo su SHA-256."""
    __tablename__ = "equipos"
    id = db.Column(db.Integer, primary_key=True)
    organizacion_id = db.Column(db.ForeignKey("organizaciones.id", ondelete="CASCADE"), nullable=False, index=True)
    token_hash = db.Column(db.String(64), nullable=False, unique=True)
    nombre = db.Column(db.Text, nullable=False)
    ip = db.Column(db.Text)
    autorizado_por = db.Column(db.ForeignKey("usuarios.id", ondelete="SET NULL"))
    creado_en = db.Column(db.DateTime(timezone=True), default=_ahora, nullable=False)
    ultimo_uso = db.Column(db.DateTime(timezone=True), default=_ahora, nullable=False)
    revocado_en = db.Column(db.DateTime(timezone=True))


class TokenCorreo(db.Model):
    """Enlaces enviados por correo (verificación, invitación, recuperación). Se guarda solo el SHA-256, son de un uso y caducan."""
    __tablename__ = "tokens_correo"
    id = db.Column(db.BigInteger, primary_key=True)
    usuario_id = db.Column(db.ForeignKey("usuarios.id", ondelete="CASCADE"), nullable=False, index=True)
    proposito = db.Column(db.Text, nullable=False)
    token_hash = db.Column(db.String(64), nullable=False, unique=True)
    email = db.Column(CITEXT, nullable=False)
    expira_en = db.Column(db.DateTime(timezone=True), nullable=False)
    usado_en = db.Column(db.DateTime(timezone=True))
    __table_args__ = (CheckConstraint("proposito IN ('verificar','invitacion','recuperar')", name="ck_token_proposito"),)


class CodigoRecuperacion(db.Model):
    """Códigos de respaldo del MFA (10, de un solo uso, guardados como SHA-256)."""
    __tablename__ = "codigos_recuperacion_mfa"
    id = db.Column(db.BigInteger, primary_key=True)
    usuario_id = db.Column(db.ForeignKey("usuarios.id", ondelete="CASCADE"), nullable=False, index=True)
    codigo_hash = db.Column(db.String(64), nullable=False)
    usado_en = db.Column(db.DateTime(timezone=True))


class HistorialClave(db.Model):
    """Últimas contraseñas (hash Argon2id) para impedir su reutilización."""
    __tablename__ = "historial_claves"
    id = db.Column(db.BigInteger, primary_key=True)
    usuario_id = db.Column(db.ForeignKey("usuarios.id", ondelete="CASCADE"), nullable=False, index=True)
    password_hash = db.Column(db.Text, nullable=False)
    creado_en = db.Column(db.DateTime(timezone=True), default=_ahora, nullable=False)
