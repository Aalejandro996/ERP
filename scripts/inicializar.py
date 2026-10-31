"""Arranque de una instalación nueva, actualización de una existente y cuentas de plataforma.

  python scripts/inicializar.py                         # tablas + migraciones 001-003 + rol limitado + 1.er Maestro (interactivo)
  python scripts/inicializar.py --no-interactivo        # igual, para despliegues (Render preDeploy): el Maestro se crea con
                                                        #   MAESTRO_USUARIO / MAESTRO_EMAIL y recibe una INVITACIÓN por correo
  python scripts/inicializar.py --crear-maestro         # agrega otro Maestro
  python scripts/inicializar.py --reset-admin USUARIO   # fija una clave temporal y quita su MFA
  python scripts/inicializar.py --alicuotas-referencia  # carga IVA 16/8/15 e IGTF 3 como REFERENCIA (verifíquelos)
Se ejecuta con el usuario DUEÑO de la base. Si DB_ROL_APLICACION está definido (p. ej. erp_app), crea ese rol sin
privilegios de dueño, le da los permisos mínimos y hace al dueño miembro: la app hará SET ROLE y RLS aplicará.
"""
import getpass
import os
import pathlib
import re
import secrets
import sys
from datetime import date

sys.path.insert(0, ".")
REFERENCIA = [("G", "16"), ("R", "8"), ("A", "15"), ("IGTF", "3")]
MIGRACIONES = ("001_integridad.sql", "002_multicliente_facturacion.sql", "003_maestro_equipos_compras.sql", "004_tasas_inpc.sql",
               "005_retirar_org_ficticia.sql", "006_monedas_empresa.sql", "007_compras_whatsapp.sql", "008_bienvenida_migracion_cuadre.sql")
SOLO_INSERCION = ("asientos", "movimientos", "audit_log", "movimientos_inventario", "lineas_documento_venta", "pagos_documento_venta")


def pedir_clave(usuario):
    from app.security import politica_clave
    while True:
        c1, c2 = getpass.getpass("Contraseña temporal: "), getpass.getpass("Repita la contraseña: ")
        err = politica_clave(c1, usuario) or (None if c1 == c2 else "Las contraseñas no coinciden")
        if not err: return c1
        print("  ✗", err)


def crear_maestro(app, db, interactivo=True):
    from app.auth.routes import enviar_invitacion
    from app.models import Usuario
    from app.security import hashear
    if interactivo:
        nombre = input("Usuario Maestro (proveedor de la plataforma): ").strip()
        email = input("Correo del Maestro: ").strip().lower()
    else:
        nombre, email = os.environ.get("MAESTRO_USUARIO", "").strip(), os.environ.get("MAESTRO_EMAIL", "").strip().lower()
        if not (nombre and "@" in email):
            print("AVISO: no hay Maestro. Defina MAESTRO_USUARIO y MAESTRO_EMAIL y vuelva a desplegar."); return
    if interactivo:
        db.session.add(Usuario(username=nombre, email=email, email_verificado=True, rol="Maestro", organizacion_id=None,
                               password_hash=hashear(pedir_clave(nombre)), debe_cambiar_clave=True))
        db.session.commit(); print(f"Maestro «{nombre}» creado: cambiará la clave y activará MFA en el primer acceso."); return
    u = Usuario(username=nombre, email=email, email_verificado=False, rol="Maestro", organizacion_id=None,
                password_hash=hashear(secrets.token_urlsafe(32)), debe_cambiar_clave=False)  # clave inutilizable hasta aceptar
    db.session.add(u); db.session.flush()
    try:
        with app.test_request_context():
            enviar_invitacion(u, "la plataforma ERP (consola Maestro)", "El instalador")
    except OSError as e:  # sin correo no hay forma de activar la cuenta: no se crea y se reintenta en el próximo despliegue
        db.session.rollback()
        print(f"AVISO: no se pudo enviar la invitación al Maestro ({e}). Revise BREVO_API_KEY y MAIL_FROM y vuelva a desplegar.")
        return
    db.session.commit(); print(f"Maestro «{nombre}» creado; se envió la invitación a {email}.")


def reinvitar_maestro(app, db):
    """El Maestro que aún no activó su cuenta recibe una invitación nueva si la anterior venció, o si se pide con
    MAESTRO_REENVIAR_INVITACION=1 (quite la variable después: cada despliegue con ella reenvía y anula el enlace previo)."""
    from app.auth.routes import enviar_invitacion
    from app.models import TokenCorreo, Usuario, _ahora
    forzar = os.environ.get("MAESTRO_REENVIAR_INVITACION") == "1"
    for u in db.session.query(Usuario).filter_by(rol="Maestro", email_verificado=False, activo=True):
        vigente = db.session.query(TokenCorreo).filter(TokenCorreo.usuario_id == u.id, TokenCorreo.proposito == "invitacion",
                                                       TokenCorreo.usado_en.is_(None), TokenCorreo.expira_en > _ahora()).count()
        if vigente and not forzar:
            print(f"Maestro «{u.username}» pendiente de activar; su invitación sigue vigente."); continue
        try:
            with app.test_request_context():
                enviar_invitacion(u, "la plataforma ERP (consola Maestro)", "Gigadevs Corp")
            db.session.commit(); print(f"Invitación reenviada al Maestro «{u.username}» ({u.email}).")
        except OSError as e:
            db.session.rollback(); print(f"AVISO: no se pudo reenviar la invitación a «{u.username}» ({e}).")


def preparar_rol(db, rol):
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", rol): sys.exit("DB_ROL_APLICACION inválido")
    t = db.text
    db.session.execute(t(f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{rol}') THEN CREATE ROLE {rol} NOLOGIN; END IF; END $$"))
    db.session.execute(t(f"GRANT {rol} TO CURRENT_USER"))
    db.session.execute(t(f"GRANT USAGE ON SCHEMA public TO {rol}"))
    db.session.execute(t(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {rol}"))
    db.session.execute(t(f"REVOKE UPDATE, DELETE ON {', '.join(SOLO_INSERCION)} FROM {rol}"))
    db.session.execute(t(f"REVOKE DELETE ON documentos_venta FROM {rol}"))
    # Las tablas del servidor de licencias (si comparten base) quedan FUERA del alcance del ERP
    for (tabla,) in db.session.execute(t("SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename LIKE 'lic\\_%'")):
        db.session.execute(t(f'REVOKE ALL ON "{tabla}" FROM {rol}'))
    db.session.execute(t(f"GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO {rol}"))
    db.session.execute(t(f"GRANT EXECUTE ON FUNCTION maestro_resumen_organizaciones(), maestro_cadenas_bitacora(), whatsapp_destino(text) TO {rol}"))
    db.session.commit()
    print(f"Rol limitado «{rol}» listo (la app hará SET ROLE {rol}; RLS aplica).")


def main():
    from config import Config
    from app import create_app
    from app.extensions import db
    from app.models import Usuario
    from app.security import hashear
    rol = os.environ.get("DB_ROL_APLICACION", "")

    class Dueño(Config):
        DB_ROL_APLICACION = ""  # las migraciones corren como dueño
    app = create_app(Dueño)
    with app.app_context():
        if "--reset-admin" in sys.argv:
            nombre = sys.argv[sys.argv.index("--reset-admin") + 1]
            u = db.session.query(Usuario).filter_by(username=nombre).first()
            if not u: sys.exit(f"No existe el usuario {nombre}")
            u.password_hash, u.debe_cambiar_clave, u.intentos_fallidos, u.bloqueado_hasta = hashear(pedir_clave(nombre)), True, 0, None
            u.mfa_activo, u.mfa_secreto_cifrado, u.mfa_ultimo_contador = False, None, 0
            u.sesion_version += 1
            db.session.commit(); print("Listo: deberá cambiar la clave y reconfigurar MFA al entrar."); return
        if "--crear-maestro" in sys.argv:
            crear_maestro(app, db); return
        if "--alicuotas-referencia" in sys.argv:
            from app.models2 import AlicuotaImpuesto
            for cod, pct in REFERENCIA:
                db.session.add(AlicuotaImpuesto(codigo=cod, porcentaje=pct, fecha_vigencia=date.today(),
                                                gaceta_referencia="Valor de referencia — VERIFICAR contra la Gaceta Oficial vigente"))
            db.session.commit(); print("Alícuotas de referencia cargadas. Verifíquelas en Maestro → Parámetros legales."); return
        db.session.execute(db.text("CREATE EXTENSION IF NOT EXISTS citext")); db.session.commit()
        db.create_all()
        for m in MIGRACIONES:
            db.session.execute(db.text(pathlib.Path("migrations", m).read_text(encoding="utf-8"))); db.session.commit()
            print(f"Aplicada migrations/{m}")
        if rol: preparar_rol(db, rol)
        from app import tasas
        print("Tasa BCV (DolarAPI):", "actualizada" if tasas.actualizar_si_falta() else "sin cambios o no disponible")
        if db.session.query(Usuario).filter_by(rol="Maestro").count() == 0:
            crear_maestro(app, db, interactivo="--no-interactivo" not in sys.argv)
        else:
            reinvitar_maestro(app, db)
        print("Base de datos lista.")


if __name__ == "__main__":
    main()
