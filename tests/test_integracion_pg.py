"""Integración de punta a punta contra PostgreSQL REAL (se omite si no hay base de pruebas).

  ERP_TEST_DATABASE_URL=postgresql+psycopg://postgres:CLAVE@localhost:5432/erp_pruebas pytest tests/test_integracion_pg.py
¡La base indicada se BORRA! Úsela solo para pruebas. La app corre con el rol limitado `erp_app` para que RLS aplique.
Cubre: plataforma → alta de clientes → invitación por correo → MFA → planes de licencia → facturación con inventario,
contabilidad, CxC, IGTF, notas y anulación → aislamiento entre clientes (app y RLS) → inmutabilidad → recuperación de clave.
"""
import os
import re
from app.tiempo import hoy_ve
from decimal import Decimal

import pytest

URL = os.environ.get("ERP_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="Defina ERP_TEST_DATABASE_URL para la prueba de integración")

if URL:
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    import base64
    _pub = Ed25519PrivateKey.generate().public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    for k, v in {"SECRET_KEY": "x" * 48, "DATABASE_URL": URL, "FIELD_ENCRYPTION_KEY": Fernet.generate_key().decode(),
                 "LICENSE_SERVER_URL": "https://lic.test", "LICENSE_PUBLIC_KEY": base64.urlsafe_b64encode(_pub).rstrip(b"=").decode(),
                 "APP_BASE_URL": "https://erp.test", "MAIL_BACKEND": "console", "FLASK_ENV": "testing"}.items():
        os.environ.setdefault(k, v)

APP_PW = "AppPruebas#2026x"
CLAVE = "Clave-Segura#2026"


class Licencia:
    def __init__(self, plan, usuarios=10, empresas=5, equipos=None):
        from app.licensing.client import EstadoLicencia
        self.e = EstadoLicencia(True, claims={"cliente": "Prueba", "plan": plan, "max_usuarios": usuarios, "max_empresas": empresas, "exp": 4102444800,
                                              **({"max_equipos": equipos} if equipos else {})})
    def estado(self): return self.e


@pytest.fixture(scope="module")
def entorno():
    from sqlalchemy import create_engine, text
    from config import Config
    from app import create_app
    from app.extensions import db
    import pathlib
    owner = create_engine(URL, isolation_level="AUTOCOMMIT")
    with owner.connect() as c:
        c.execute(text("DROP SCHEMA public CASCADE")); c.execute(text("CREATE SCHEMA public"))
        if not c.execute(text("SELECT 1 FROM pg_roles WHERE rolname='erp_app'")).first():
            c.execute(text(f"CREATE ROLE erp_app LOGIN PASSWORD '{APP_PW}'"))
    class CfgOwner(Config):
        TESTING = True; TASAS_AUTOMATICAS = False; WTF_CSRF_ENABLED = False; RATELIMIT_ENABLED = False; SESSION_COOKIE_NAME = "s"; PRODUCCION = False
    app_owner = create_app(CfgOwner)
    with app_owner.app_context():
        db.session.execute(text("CREATE EXTENSION IF NOT EXISTS citext")); db.session.commit()
        db.create_all()
        for m in ("001_integridad.sql", "002_multicliente_facturacion.sql", "003_maestro_equipos_compras.sql", "004_tasas_inpc.sql", "005_retirar_org_ficticia.sql"):
            db.session.execute(text(pathlib.Path("migrations", m).read_text(encoding="utf-8"))); db.session.commit()
    with owner.connect() as c:
        c.execute(text("GRANT USAGE ON SCHEMA public TO erp_app"))
        c.execute(text("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO erp_app"))
        c.execute(text("GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO erp_app"))
        c.execute(text("GRANT EXECUTE ON FUNCTION pg_control_system() TO erp_app"))
        c.execute(text("GRANT EXECUTE ON FUNCTION maestro_resumen_organizaciones(), maestro_cadenas_bitacora() TO erp_app"))
    app_url = re.sub(r"//[^@]+@", f"//erp_app:{APP_PW}@", URL)
    class CfgApp(CfgOwner):
        SQLALCHEMY_DATABASE_URI = app_url
    app = create_app(CfgApp)
    planes = {}
    app.extensions["gestor_licencia"] = lambda org: Licencia(*planes.get(org, ("ALL_IN_ONE",)))
    # Servidor de licencias real en proceso (SQLite) con correo capturado: la consola del Maestro lo usa por su API firmada
    import tempfile
    from app.licensing.admin import ClienteAdmin
    from licensing_server.server import create_app as crear_srv
    correos_lic = []
    srv = crear_srv("sqlite:///" + os.path.join(tempfile.mkdtemp(), "lic.db"), Ed25519PrivateKey.generate(), secreto_admin="z" * 40,
                    correo=lambda d, a, t, **k: correos_lic.append((d, t)), config={"pago_movil": "Banco Prueba 0414-0000000"})
    cli_srv = srv.test_client()
    def transporte(metodo, url, cuerpo, cab):
        r = cli_srv.open(url.split("lic.test", 1)[1], method=metodo, data=cuerpo, headers=cab); return r.status_code, r.data
    app.config["PAGO_MOVIL_INSTRUCCIONES"] = "Banco Prueba 0414-0000000, RIF J-00000000-0."
    app.extensions["admin_licencias"] = lambda: ClienteAdmin("https://lic.test", "z" * 40, transporte=transporte)
    return dict(app=app, owner=owner, planes=planes, app_owner=app_owner, correos_lic=correos_lic)


def _cliente(app):
    return app.test_client(use_cookies=True)


def _mfa_desde_html(html):
    return re.search(r"<code>([A-Z2-7]{20,})</code>", html).group(1)


def _ultimo_enlace(ruta):
    from app import correo
    for m in reversed(correo.ENVIADOS):
        x = re.search(r"https://erp\.test(/" + ruta + r"/[\w-]+)", m.get_content())
        if x: return x.group(1)
    raise AssertionError("no se envió el correo esperado")


def _login_total(c, usuario, clave):
    """Invitación ya aceptada: login → (gate) configurar MFA → códigos de respaldo."""
    from app import totp
    r = c.post("/login", data={"usuario": usuario, "clave": clave})
    assert r.status_code == 302, r.data[:300]
    r = c.get("/mfa/configurar"); assert r.status_code == 200
    secreto = _mfa_desde_html(r.get_data(as_text=True))
    assert b"data:image/svg+xml" in r.data                        # QR generado en el servidor
    r = c.post("/mfa/configurar", data={"codigo": totp.codigo(secreto)})
    assert r.headers["Location"].endswith("/mfa/codigos")
    r = c.get("/mfa/codigos"); assert r.data.count(b"<span>") >= 10
    return secreto


def test_flujo_completo(entorno):
    from sqlalchemy import select, text
    from app import totp
    from app.extensions import db
    from app.models import Organizacion, Usuario
    from app.models2 import DocumentoCxC, Producto
    from app.models_fact import DocumentoVenta
    from app.security import hashear
    app, owner, planes = entorno["app"], entorno["owner"], entorno["planes"]

    # --- Maestro (creado por inicializar.py en la vida real) ---
    with entorno["app_owner"].app_context():
        sec_root = totp.nuevo_secreto()
        db.session.add(Usuario(username="root", email="root@prov.test", email_verificado=True, rol="Maestro", password_hash=hashear(CLAVE),
                               debe_cambiar_clave=False, mfa_activo=True, mfa_secreto_cifrado=entorno["app_owner"].extensions["cifrador"].cifrar(sec_root)))
        db.session.commit()
    root = _cliente(app)
    r = root.post("/login", data={"usuario": "root", "clave": CLAVE}); assert r.headers["Location"].endswith("/mfa/verificar")
    r = root.post("/mfa/verificar", data={"codigo": "000000"}); assert b"incorrecto" in r.data
    r = root.post("/mfa/verificar", data={"codigo": totp.codigo(sec_root)}); assert r.status_code == 302
    assert root.get("/").headers["Location"].endswith("/plataforma")
    # Un redespliegue con eventos del Maestro en la bitácora NO debe inventar una «Organización principal»
    import pathlib as _pl
    with owner.connect() as c:
        assert c.execute(text("SELECT count(*) FROM audit_log WHERE organizacion_id IS NULL")).scalar() > 0
        c.execute(text(_pl.Path("migrations/002_multicliente_facturacion.sql").read_text(encoding="utf-8")))
        assert c.execute(text("SELECT count(*) FROM organizaciones")).scalar() == 0
        # …y si ya existía la ficticia (caso real en producción), la 005 la retira y devuelve los eventos a la plataforma
        c.execute(text("ALTER TABLE audit_log DISABLE TRIGGER trg_inmutable"))
        oid = c.execute(text("INSERT INTO organizaciones(nombre, rif, estado, creado_en) VALUES ('Organización principal','J-00000000-0','activa',now()) RETURNING id")).scalar()
        c.execute(text("UPDATE audit_log SET organizacion_id = :o WHERE organizacion_id IS NULL"), {"o": oid})
        c.execute(text("ALTER TABLE audit_log ENABLE TRIGGER trg_inmutable"))
        c.execute(text(_pl.Path("migrations/005_retirar_org_ficticia.sql").read_text(encoding="utf-8")))
        assert c.execute(text("SELECT count(*) FROM organizaciones")).scalar() == 0
        assert c.execute(text("SELECT count(*) FROM audit_log WHERE organizacion_id IS NOT NULL")).scalar() == 0
    assert "ÍNTEGRA".encode() in root.get("/plataforma/bitacora").data
    for cod, pct in (("G", "16"), ("R", "8"), ("A", "15"), ("IGTF", "3")):
        root.post("/plataforma/fiscal", data={"accion": "alicuota", "codigo": cod, "valor": pct, "fecha": "2026-01-01"})
    root.post("/plataforma/fiscal", data={"accion": "tasa", "valor": "36.5", "eur": "40,25", "fecha": hoy_ve().isoformat()})
    # INPC real del BCV (base dic-2007 = 100): miles de millones, también con formato venezolano
    r = root.post("/plataforma/fiscal", data={"accion": "inpc", "valor": "6.734.512.876,45", "fecha": "2025-12-01", "ref": "BCV"}, follow_redirects=True)
    assert "Parámetro guardado".encode() in r.data, re.findall(r'role="alert">([^<]+)', r.get_data(as_text=True))
    r = root.post("/plataforma/fiscal", data={"accion": "inpc", "valor": "7000000000", "fecha": "2025-12-01"}, follow_redirects=True)
    assert "Ya hay un INPC".encode() in r.data
    assert "6734512876.45".encode() in root.get("/plataforma/fiscal").data

    # --- Alta de dos clientes con su administrador por invitación ---
    root.post("/plataforma", data={"accion": "crear", "nombre": "Alfa Contadores", "rif": "J-11111111-1", "username": "adminA", "email": "a@alfa.test"})
    inv_a = _ultimo_enlace("invitacion")
    root.post("/plataforma", data={"accion": "crear", "nombre": "Bodega Beta", "rif": "J-33333333-3", "username": "adminB", "email": "b@beta.test"})
    inv_b = _ultimo_enlace("invitacion")
    with app.app_context():
        org_a = db.session.execute(select(Organizacion.id).where(Organizacion.rif == "J-11111111-1")).scalar()
        org_b = db.session.execute(select(Organizacion.id).where(Organizacion.rif == "J-33333333-3")).scalar()
    planes[org_a], planes[org_b] = ("ALL_IN_ONE",), ("CASH", 10, 5, 1)          # B: un solo equipo permitido

    a, b = _cliente(app), _cliente(app)
    assert a.post(inv_a, data={"nueva": "corta", "repetir": "corta"}).status_code == 200            # política de clave
    assert a.post(inv_a, data={"nueva": CLAVE, "repetir": CLAVE}).status_code == 302
    assert a.post(inv_a, data={"nueva": CLAVE, "repetir": CLAVE}).status_code == 302                # enlace de un solo uso → redirige a login
    b.post(inv_b, data={"nueva": CLAVE + "b", "repetir": CLAVE + "b"})
    _login_total(a, "adminA", CLAVE)
    sec_b = _login_total(b, "b@beta.test", CLAVE + "b")                                            # login con correo
    # Límite de equipos: B ya usa su único equipo; un segundo navegador queda fuera aunque la clave y el MFA sean correctos
    b2 = _cliente(app)
    b2.post("/login", data={"usuario": "adminB", "clave": CLAVE + "b"})
    import time as _t
    r = b2.post("/mfa/verificar", data={"codigo": totp.codigo(sec_b, _t.time() + 30)})
    assert r.status_code == 403 and "equipo".encode() in r.data and b2.get("/").status_code == 302

    # --- Cliente A (ALL_IN_ONE): empresa, cuentas, inventario, serie ---
    r = a.post("/", data={"razon_social": "Comercial Uno CA", "rif": "j-22222222-2", "spe": "1"}, follow_redirects=True)
    assert "Empresa creada".encode() in r.data, re.findall(r'role="alert">([^<]+)', r.get_data(as_text=True))
    dueño = lambda sql: owner.connect().execute(text(sql)).scalar()                                  # el dueño no está sujeto a RLS
    eid_a = dueño("SELECT id FROM empresas WHERE rif='J-22222222-2'")
    for cod, nom, tipo in (("1.1", "Caja y bancos", "Activo"), ("1.2", "Cuentas por cobrar", "Activo"), ("1.3", "Inventario", "Activo"),
                           ("2.1", "IVA débito fiscal", "Pasivo"), ("2.2", "IGTF por pagar", "Pasivo"), ("4.1", "Ventas", "Ingreso"), ("5.1", "Costo de ventas", "Gasto")):
        a.post(f"/e/{eid_a}/cuentas", data={"codigo": cod, "nombre": nom, "tipo": tipo, "categoria": "Operacion", "monetaria": "0"})
    a.post(f"/e/{eid_a}/config-asientos", data={"cuenta_caja_banco": "1.1", "cuenta_cxc": "1.2", "cuenta_inventario": "1.3", "cuenta_iva_debito_fiscal": "2.1",
                                               "cuenta_igtf_por_pagar": "2.2", "cuenta_ventas": "4.1", "cuenta_costo_venta": "5.1", "cuenta_cxp": "1.1"})
    a.post(f"/e/{eid_a}/inventario", data={"accion": "producto", "codigo": "P1", "nombre": "Producto uno", "metodo": "Promedio", "precio": "10", "alicuota": "G"})
    a.post(f"/e/{eid_a}/inventario", data={"accion": "entrada", "codigo": "P1", "cantidad": "10", "costo": "5", "fecha": hoy_ve().isoformat(), "pago": "Contado"})
    r = a.post(f"/e/{eid_a}/facturacion/series", data={"accion": "serie", "serie": "A", "prefijo": "00", "desde": "1", "hasta": "3", "imprenta": "Imprenta Digital SA",
                                                      "imprenta_rif": "J-44444444-4", "providencia": "SENIAT/INTI/2025/0001", "fecha": "2026-01-01"})
    sid = dueño("SELECT id FROM series_facturacion")
    assert a.get(f"/e/{eid_a}/facturacion/nueva").status_code == 200

    def emitir(cliente, eid, **extra):
        base = {"serie_id": sid, "fecha": hoy_ve().isoformat(), "moneda": "VES", "condicion": "Contado", "cliente_rif": "V-12345678-9",
                "cliente_nombre": "Cliente </script> Prueba", "guardar_cliente": "1"}
        return cliente.post(f"/e/{eid}/facturacion/nueva", data={**base, **extra})

    # Factura 1: contado en USD con producto (inventario + costo) y pago Zelle → IGTF (empresa SPE)
    r = emitir(a, eid_a, moneda="USD", tasa="36.5", l1_prod="P1", l1_desc="Producto uno", l1_cant="2", l1_precio="10", l1_alic="G",
               p1_forma="Zelle", p1_moneda="USD", p1_monto="23.20")
    assert r.status_code == 302 and "/facturacion/" in r.headers["Location"], r.get_data(as_text=True)[:500]
    with app.app_context():
        from app.rls import fijar_org
        with app.test_request_context():
            fijar_org(db, org_a)
            d1 = db.session.execute(select(DocumentoVenta).order_by(DocumentoVenta.id)).scalars().first()
            assert (d1.numero, d1.numero_control, d1.total, d1.igtf) == (1, "00-00000001", Decimal("846.80"), Decimal("25.40"))
            assert d1.total_a_pagar == Decimal("872.20") and d1.asiento_id
            assert db.session.get(Producto, (eid_a, "P1")).cantidad_actual == Decimal("8")
    assert b"FACTURA" in a.get(f"/e/{eid_a}/facturacion/{d1.id}").data
    assert b"\\u003c/script\\u003e" in a.get(f"/e/{eid_a}/facturacion/nueva").data                  # JSON embebido escapado

    # Factura 2: crédito en Bs → CxC; cobro parcial; anulación rechazada por tener cobro
    r = emitir(a, eid_a, condicion="Credito", vencimiento=hoy_ve().isoformat(), l1_desc="Servicio", l1_cant="1", l1_precio="100", l1_alic="E")
    did2 = int(r.headers["Location"].rsplit("/", 1)[1])
    a.post(f"/e/{eid_a}/facturacion/{did2}/cobro", data={"fecha": hoy_ve().isoformat(), "forma": "Transferencia", "moneda": "VES", "monto": "40"})
    r = a.post(f"/e/{eid_a}/facturacion/{did2}/anular", data={"motivo": "error"}, follow_redirects=True)
    assert "nota de crédito".encode() in r.data
    # NC sobre la factura 1 con devolución al inventario
    r = a.post(f"/e/{eid_a}/facturacion/nueva", data={"tipo": "NC", "afectado": d1.id, "serie_id": sid, "fecha": hoy_ve().isoformat(), "motivo": "Devolución",
                                                     "reintegrar": "1", "l1_prod": "P1", "l1_desc": "Producto uno", "l1_cant": "1", "l1_precio": "10", "l1_alic": "G"})
    assert r.status_code == 302, r.get_data(as_text=True)[:400]
    # Rango de control agotado (1..3 ya usados)
    r = emitir(a, eid_a, l1_desc="X", l1_cant="1", l1_precio="1", p1_forma="Efectivo", p1_moneda="VES", p1_monto="1.16")
    assert r.status_code == 200 and "agotó".encode() in r.data
    # Nuevo rango de control (no puede reutilizar números) y anulación el mismo día: inventario y costo se revierten
    a.post(f"/e/{eid_a}/facturacion/series", data={"accion": "rango", "sid": sid, "desde": "2", "hasta": "9", "fecha": hoy_ve().isoformat()})
    assert dueño("SELECT control_hasta FROM series_facturacion") == 3
    a.post(f"/e/{eid_a}/facturacion/series", data={"accion": "rango", "sid": sid, "desde": "4", "hasta": "9", "fecha": hoy_ve().isoformat()})
    r = emitir(a, eid_a, l1_prod="P1", l1_desc="Producto uno", l1_cant="3", l1_precio="365", l1_alic="G", p1_forma="Efectivo", p1_moneda="VES", p1_monto="1270.20")
    did4 = int(r.headers["Location"].rsplit("/", 1)[1])
    r = a.post(f"/e/{eid_a}/facturacion/{did4}/anular", data={"motivo": "Error en el cliente"}, follow_redirects=True)
    assert b"Documento anulado" in r.data and b"ANULADA" in r.data
    assert dueño(f"SELECT numero_control FROM documentos_venta WHERE id={did4}") == "00-00000004"
    with app.test_request_context():
        fijar_org(db, org_a)
        assert db.session.get(Producto, (eid_a, "P1")).cantidad_actual == Decimal("9")
        cxc = db.session.execute(select(DocumentoCxC)).scalars().one()
        assert cxc.saldo_pendiente == Decimal("60.00")
        cuadre = db.session.execute(text("SELECT SUM(debe) - SUM(haber) FROM movimientos")).scalar()
        assert cuadre == 0
    csv = a.get(f"/e/{eid_a}/facturacion/libro-ventas?formato=csv&periodo={hoy_ve():%Y-%m}")
    assert csv.status_code == 200 and b"00-00000002" in csv.data
    assert "ÍNTEGRA".encode() in a.get(f"/e/{eid_a}/auditoria").data
    assert a.get(f"/e/{eid_a}/mayor?cuenta=4.1").status_code == 200

    # --- Cliente B (CASH): misma empresa (mismo RIF) sin conflicto; sin contabilidad; no ve a A ---
    r = b.post("/", data={"razon_social": "Comercial Uno CA", "rif": "J-22222222-2", "spe": "0"})
    eid_b = dueño("SELECT max(id) FROM empresas")
    assert eid_b != eid_a
    assert b.get(f"/e/{eid_b}/facturacion").status_code == 200
    b.post(f"/e/{eid_b}/facturacion/series", data={"accion": "serie", "serie": "", "prefijo": "00", "desde": "1", "hasta": "100", "imprenta": "Imprenta Digital SA",
                                                  "imprenta_rif": "J-44444444-4", "providencia": "SENIAT/INTI/2025/0002", "fecha": "2026-01-01"})
    sid_b = dueño(f"SELECT id FROM series_facturacion WHERE empresa_id={eid_b}")
    r = b.post(f"/e/{eid_b}/facturacion/nueva", data={"serie_id": sid_b, "fecha": hoy_ve().isoformat(), "moneda": "VES", "condicion": "Contado", "cliente_rif": "V-87654321-0",
                                                     "cliente_nombre": "Consumidor", "l1_desc": "Venta", "l1_cant": "1", "l1_precio": "100", "l1_alic": "R",
                                                     "p1_forma": "Pago móvil", "p1_moneda": "VES", "p1_monto": "108"})
    assert r.status_code == 302, r.get_data(as_text=True)[-600:]
    assert dueño(f"SELECT asiento_id IS NULL AND numero = 1 AND numero_control = '00-00000001' FROM documentos_venta WHERE empresa_id={eid_b}")  # CASH: sin contabilidad
    assert b.get(f"/e/{eid_b}/asientos").status_code == 403                                         # no está en su plan
    assert b.get(f"/e/{eid_b}/auditoria").status_code == 403
    assert b.get(f"/e/{eid_a}/facturacion").status_code == 404                                      # empresa de otro cliente
    assert b.get(f"/e/{eid_a}/facturacion/{d1.id}").status_code == 404
    assert b"Comercial Uno" in b.get("/").data
    assert b'title="No incluido en su plan">Libro diario' in b.get(f"/e/{eid_b}/facturacion").data                # menú bloqueado por plan
    assert root.get(f"/e/{eid_a}/asientos").status_code == 403                                      # el Maestro no ve libros
    assert a.get("/plataforma").status_code == 403

    # --- RLS: la base de datos misma filtra por organización (rol erp_app, no dueño) ---
    from sqlalchemy import create_engine
    app_eng = create_engine(app.config["SQLALCHEMY_DATABASE_URI"])
    with app_eng.begin() as c:
        cuenta = lambda: c.execute(text("SELECT count(*) FROM documentos_venta")).scalar()
        assert cuenta() == 0                                                                       # sin organización
        c.execute(text("SELECT set_config('app.org_id', :v, true)"), {"v": str(org_b)}); assert cuenta() == 1
        assert c.execute(text("SELECT count(*) FROM empresas")).scalar() == 1
        c.execute(text("SELECT set_config('app.org_id', :v, true)"), {"v": str(org_a)}); assert cuenta() == 4
    # --- Inmutabilidad: ni el dueño puede alterar un documento emitido ---
    with owner.connect() as c:
        with pytest.raises(Exception, match="no puede modificarse"):
            c.execute(text("UPDATE documentos_venta SET total = 1 WHERE id = :i"), {"i": d1.id})
        with pytest.raises(Exception, match="no se borran"):
            c.execute(text("DELETE FROM documentos_venta WHERE id = :i"), {"i": d1.id})

    # --- Libro de compras desde CxP ---
    a2 = _cliente(app)  # (la sesión de «a» sigue viva; se usa para registrar)
    r = a.post(f"/e/{eid_a}/cxp", data={"proveedor_nombre": "Proveedor Uno", "proveedor_rif": "J-77777777-7", "tipo_documento": "FAC", "numero_documento": "000123",
                                        "numero_control": "00-000999", "fecha_emision": hoy_ve().isoformat(), "fecha_vencimiento": hoy_ve().isoformat(),
                                        "monto": "126", "exento": "10", "base_imponible": "100", "alicuota": "16", "monto_iva": "16", "importacion": "0"}, follow_redirects=True)
    assert b"Documento registrado" in r.data, re.findall(r'role="alert">([^<]+)', r.get_data(as_text=True))
    r = a.post(f"/e/{eid_a}/cxp", data={"proveedor_nombre": "P", "proveedor_rif": "J-77777777-7", "numero_documento": "9", "fecha_emision": hoy_ve().isoformat(),
                                        "fecha_vencimiento": hoy_ve().isoformat(), "monto": "100", "base_imponible": "90", "monto_iva": "16"}, follow_redirects=True)
    assert "debe ser igual".encode() in r.data                                                      # base + IVA + exento ≠ total
    csv = a.get(f"/e/{eid_a}/cxp/libro-compras?formato=csv&periodo={hoy_ve():%Y-%m}")
    assert b"00-000999" in csv.data and b"126.00" in csv.data
    assert b.get(f"/e/{eid_b}/cxp/libro-compras").status_code == 403                                # CASH no incluye Administración

    # --- Consola Maestro: licencias, pagos y base de datos ---
    for ruta in ("/plataforma/licencias", "/plataforma/pagos", "/plataforma/base-datos"):
        assert root.get(ruta).status_code == 200, ruta
    assert b"Row-Level Security activo" in root.get("/plataforma/base-datos").data
    assert a.get("/plataforma/licencias").status_code == 403
    r = root.post("/plataforma/licencias", data={"accion": "crear", "org": org_a, "plan": "ALL_IN_ONE", "dias": "365", "usuarios": "5", "empresas": "2", "equipos": "3"},
                  follow_redirects=True)
    assert "una sola vez".encode() in r.data and entorno["correos_lic"][-1][0] == "a@alfa.test"
    assert "Clave de licencia: ERP-" in entorno["correos_lic"][-1][1] and b"ERP-" not in root.get("/plataforma/licencias").data.split(b"Emitir licencia")[0]
    root.post("/plataforma/pagos", data={"accion": "precio", "plan": "CASH", "meses": "1", "monto": "20", "usuarios": "3", "empresas": "1", "equipos": "2"})
    # El administrador de B compra en bolívares; el Maestro confirma y la clave llega por correo una sola vez
    r = b.post("/admin/licencia", data={"accion": "comprar", "oferta": "CASH:1", "moneda": "VES"})
    ref = r.headers["Location"].split("orden=")[1]
    assert b"Banco Prueba" in b.get(r.headers["Location"]).data and "730.00".encode() in b.get(r.headers["Location"]).data   # 20 USD × 36,5
    b.post("/admin/licencia", data={"accion": "informar", "ref": ref, "referencia": "00998877", "banco": "Banesco"})
    n = len(entorno["correos_lic"])
    root.post("/plataforma/pagos", data={"accion": "confirmar", "ref": ref})
    root.post("/plataforma/pagos", data={"accion": "confirmar", "ref": ref})                       # doble clic: no reenvía
    assert len(entorno["correos_lic"]) == n + 1 and entorno["correos_lic"][-1][0] == "b@beta.test"
    assert a.post("/admin/licencia", data={"accion": "informar", "ref": ref, "referencia": "1234"}).status_code == 302   # otra organización: rechazado
    with owner.connect() as c:
        assert c.execute(text("SELECT count(*) FROM equipos")).scalar() == 2

    # --- Recuperación de contraseña: enlace por correo y cierre de todas las sesiones ---
    otro = _cliente(app)
    otro.post("/recuperar", data={"email": "a@alfa.test"})
    enlace = _ultimo_enlace("recuperar")
    assert otro.post(enlace, data={"nueva": CLAVE, "repetir": CLAVE}).status_code == 200            # no reutilizar
    assert otro.post(enlace, data={"nueva": "Otra-Clave#2026z", "repetir": "Otra-Clave#2026z"}).status_code == 302
    assert a.get("/").headers["Location"].endswith("/login")                                        # sesión anterior invalidada
    r = otro.post("/recuperar", data={"email": "nadie@x.test"}); assert r.status_code == 302         # respuesta genérica
