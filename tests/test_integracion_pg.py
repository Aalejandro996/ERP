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
        for m in ("001_integridad.sql", "002_multicliente_facturacion.sql", "003_maestro_equipos_compras.sql", "004_tasas_inpc.sql", "005_retirar_org_ficticia.sql", "006_monedas_empresa.sql", "007_compras_whatsapp.sql", "008_bienvenida_migracion_cuadre.sql", "009_whatsapp_wasender.sql", "010_presupuestos.sql"):
            db.session.execute(text(pathlib.Path("migrations", m).read_text(encoding="utf-8"))); db.session.commit()
    with owner.connect() as c:
        c.execute(text("GRANT USAGE ON SCHEMA public TO erp_app"))
        c.execute(text("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO erp_app"))
        c.execute(text("GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO erp_app"))
        c.execute(text("GRANT EXECUTE ON FUNCTION pg_control_system() TO erp_app"))
        c.execute(text("GRANT EXECUTE ON FUNCTION maestro_resumen_organizaciones(), maestro_cadenas_bitacora(), whatsapp_destino(text) TO erp_app"))
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
    assert r.status_code == 403 and "equipo".encode() in r.data and b2.get("/").status_code == 200 and b"Probar gratis" in b2.get("/").data   # fuera: portada pública

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
    # Divisas de la empresa: sin habilitar USDT se rechaza; al habilitarla se factura en USDT con la tasa del dólar
    r = emitir(a, eid_a, moneda="USDT", tasa="36.5", l1_desc="X", l1_cant="1", l1_precio="1", p1_forma="Otro", p1_moneda="USDT", p1_monto="1.16")
    assert "no tiene habilitado".encode() in r.data
    a.post(f"/e/{eid_a}/facturacion/series", data={"accion": "monedas", "monedas": ["USD", "EUR", "USDT"]})
    assert dueño(f"SELECT monedas FROM empresas WHERE id={eid_a}") == "USD,EUR,USDT"
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
    assert re.search(r'title="No incluido en su plan"><span class="ico"[^>]*>[^<]*</span>Libro diario', b.get(f"/e/{eid_b}/facturacion").get_data(as_text=True))               # menú bloqueado por plan
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
    # Precios de referencia del mercado VE, combos personalizados (mensual/anual según módulos) y demo de 72 h
    pagos = root.get("/plataforma/pagos").get_data(as_text=True)
    assert "Combos personalizados" in pagos and "All in One" in pagos and "69.00" in pagos               # precio de referencia sembrado
    r = root.post("/plataforma/pagos", data={"accion": "combo", "nombre": "Tienda + Contable", "modulos": ["facturacion", "contabilidad"], "meses": "12",
                                            "usuarios": "3", "empresas": "1", "equipos": "3"}, follow_redirects=True)
    assert "Tienda + Contable" in r.get_data(as_text=True) and "430.00" in r.get_data(as_text=True), re.findall(r'role="alert">([^<]+)', r.get_data(as_text=True))
    tienda = b.get("/admin/licencia").get_data(as_text=True)
    assert "Combo Tienda + Contable" in tienda and "Anual" in tienda
    cid = re.search(r'value="COMBO:(\d+)"', tienda).group(1)
    r = b.post("/admin/licencia", data={"accion": "comprar", "oferta": f"COMBO:{cid}", "moneda": "VES"})
    assert "15695.00".encode() in b.get(r.headers["Location"]).data                                   # 430 USD × 36,5
    r = root.post("/plataforma/licencias", data={"accion": "demo", "org": org_b}, follow_redirects=True)
    assert "Demo de 72 h emitida".encode() in r.data and "Demo 72 h" in entorno["correos_lic"][-1][1], re.findall(r'role="alert">([^<]+)', r.get_data(as_text=True))
    r = b.post("/admin/licencia", data={"accion": "demo"}, follow_redirects=True)
    assert "ya utilizó su demo".encode() in r.data                                                      # una sola demo por organización
    planes[org_b] = ("DEMO", 2, 1)
    assert "DEMO · quedan".encode() in b.get("/").data
    planes[org_b] = ("CASH", 10, 5, 1)
    # Facturación Gigadevs: sin configurar la licencia se entregó igual; se configura y se factura la orden pendiente
    assert b"Licencias pagadas sin factura" in root.get("/plataforma/facturas").data
    root.post("/plataforma/facturas", data={"accion": "emisor", "razon_social": "Gigadevs Corp", "rif": "J-50000000-1", "domicilio": "Caracas",
                                            "contribuyente_especial": "0", "iva_pct": "16", "serie": "A", "control_prefijo": "00",
                                            "imprenta_razon_social": "Imprenta Digital SA", "imprenta_rif": "J-44444444-4", "imprenta_providencia": "SENIAT/2026/1"})
    root.post("/plataforma/facturas", data={"accion": "rango", "desde": "1", "hasta": "50", "fecha": "2026-10-01"})
    n = len(entorno["correos_lic"])
    r = root.post("/plataforma/facturas", data={"accion": "cliente", "org": org_b}, follow_redirects=True)   # solo se elige el cliente
    assert "construida".encode() in r.data and len(entorno["correos_lic"]) == n + 1 and entorno["correos_lic"][-1][0] == "b@beta.test"
    r = root.get("/plataforma/facturas/ejemplo")
    assert r.status_code == 200 and "EJEMPLO".encode() in r.data and b"gigadevs.png" in r.data
    r = root.get("/plataforma/facturas/1")
    assert r.status_code == 200 and b"FACTURA" in r.data and b"gigadevs.png" in r.data and b"J-33333333-3" in r.data
    assert "script-src 'none'" in r.headers["Content-Security-Policy"]
    r = root.post("/plataforma/facturas", data={"accion": "cliente", "org": org_b}, follow_redirects=True)
    assert "no tiene ventas pendientes".encode() in r.data
    assert a.get("/plataforma/facturas").status_code == 403
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


def _org_con_admin(entorno, nombre, rif, usuario):
    """Organización con su Administrador ya activado (correo verificado y MFA): entra con clave + TOTP."""
    from app import totp
    from app.extensions import db
    from app.models import Organizacion, Usuario
    from app.security import hashear
    with entorno["app_owner"].app_context():
        org = Organizacion(nombre=nombre, rif=rif, correo_contacto=f"{usuario}@x.test"); db.session.add(org); db.session.flush()
        sec = totp.nuevo_secreto()
        db.session.add(Usuario(organizacion_id=org.id, username=usuario, email=f"{usuario}@x.test", email_verificado=True, rol="Administrador",
                               password_hash=hashear(CLAVE), debe_cambiar_clave=False, mfa_activo=True,
                               mfa_secreto_cifrado=entorno["app_owner"].extensions["cifrador"].cifrar(sec)))
        db.session.commit(); oid = org.id
    c = _cliente(entorno["app"])
    c.post("/login", data={"usuario": usuario, "clave": CLAVE})
    assert c.post("/mfa/verificar", data={"codigo": totp.codigo(sec)}).status_code == 302
    return oid, c


def test_contabilidad_compras_y_bot_de_whatsapp(entorno):
    import hashlib, hmac, io, json
    from sqlalchemy import text
    app, owner = entorno["app"], entorno["owner"]
    hoy = hoy_ve().isoformat()
    with owner.connect() as c:   # parámetros legales (los carga el Maestro)
        for cod, pct in (("G", "16"), ("R", "8"), ("A", "15"), ("IGTF", "3")):
            c.execute(text("INSERT INTO alicuotas_impuesto(codigo, porcentaje, fecha_vigencia) VALUES (:c, :p, '2025-06-01') ON CONFLICT DO NOTHING"), {"c": cod, "p": pct})
        c.execute(text("INSERT INTO tasa_cambio(fecha, tasa_usd_ves, tasa_eur_ves, fuente) VALUES (:f, 40, 44, 'prueba') ON CONFLICT DO NOTHING"), {"f": hoy})
        c.commit()
    tasa = owner.connect().execute(text("SELECT tasa_usd_ves FROM tasa_cambio WHERE fecha=:f"), {"f": hoy}).scalar()
    org, a = _org_con_admin(entorno, "Delta Comercial", "J-44444444-4", "adminD")
    _, x = _org_con_admin(entorno, "Intrusa SA", "J-55555555-5", "adminX")
    a.post("/", data={"razon_social": "Delta Comercial CA", "rif": "J-44444444-4", "spe": "0", "direccion": "Av. Bolívar, Valencia"})
    eid = owner.connect().execute(text("SELECT id FROM empresas WHERE rif='J-44444444-4'")).scalar()
    alerta = lambda r: re.findall(r'role="alert">([^<]+)', r.get_data(as_text=True))

    # --- Plan de cuentas: el cliente sube el que ya tiene (CSV de su sistema anterior) y además carga el modelo VEN-NIF
    csv_ = "Código;Descripción;Naturaleza\n1.1.01;Caja;Activo\n1.1.02;Bancos;Activo\n1.1.07;Inventario;Activo\n2.1.01;Proveedores;Pasivo\n4.1.01;Ventas;Ingreso\n6.1.01;Sueldos;Gasto\n¿?;malo;x\n"
    r = a.post(f"/e/{eid}/cuentas", data={"accion": "importar", "modo": "omitir", "archivo": (io.BytesIO(csv_.encode("cp1252")), "plan.csv")},
               content_type="multipart/form-data", follow_redirects=True)
    assert any("6 cuenta(s) nueva(s)" in m for m in alerta(r)) and any("código inválido" in m for m in alerta(r)), alerta(r)
    r = a.post(f"/e/{eid}/cuentas", data={"accion": "importar", "modo": "actualizar", "texto": "1.1.01;Caja principal;Activo"}, follow_redirects=True)
    assert any("1 actualizada" in m for m in alerta(r))
    r = a.post(f"/e/{eid}/cuentas", data={"accion": "modelo"}, follow_redirects=True)
    assert "Caja principal".encode() in r.data and "Capital social".encode() in r.data                 # el modelo no pisó lo importado
    assert a.get(f"/e/{eid}/cuentas?plantilla=1").data.startswith("﻿codigo;".encode())

    # --- Contabilidad en el orden del escritorio: cada módulo responde y las pestañas enlazan a sus hermanos
    a.post(f"/e/{eid}/asientos", data={"fecha": hoy, "concepto": "Venta de contado", "c1": "1.1.01", "d1": "500", "c2": "4.1.01", "h2": "500"})
    a.post(f"/e/{eid}/asientos", data={"fecha": hoy, "concepto": "Pago de sueldos", "c1": "6.1.01", "d1": "200", "c2": "1.1.02", "h2": "200"})
    for ruta in ("cuentas", "asientos", "mayor", "diario", "balance-general", "comprobacion", "resultados", "dashboard", "fiscal/libros", "bancos", "divisas", "cierres",
                 "fiscal/islr", "fiscal/retenciones", "cxc", "cxp", "cxp/libro-compras", "compras/ordenes", "compras/proveedores", "inventario", "whatsapp/pedidos", "whatsapp/bot"):
        assert a.get(f"/e/{eid}/{ruta}").status_code == 200, ruta
    html = a.get(f"/e/{eid}/balance-general").get_data(as_text=True)
    assert 'class="pestanas"' in html and f"/e/{eid}/dashboard" in html and "CUADRA" in html
    assert html.index("Catálogo de cuentas") < html.index("Asientos contables") < html.index("Libro mayor") < html.index("Libro diario") < html.index("Conciliación bancaria")
    dash = a.get(f"/e/{eid}/dashboard").get_data(as_text=True)
    assert "<svg" in dash and "Ingresos " + hoy[:7] in dash and "500" in dash
    assert "Pago de sueldos" in a.get(f"/e/{eid}/diario").get_data(as_text=True)
    conv = a.get(f"/e/{eid}/divisas?monto=10&de=USD").get_data(as_text=True)
    assert "Equivalente en bolívares" in conv and str((tasa * 10).quantize(Decimal("0.01"))) in conv
    assert a.get(f"/e/{eid}/reportes").status_code == 302

    # --- Proveedor y orden de compra (modelo de la factura de Gigadevs con los datos de la empresa y del proveedor)
    a.post(f"/e/{eid}/inventario", data={"accion": "producto", "codigo": "A001", "nombre": "Harina PAN 1kg", "metodo": "Promedio", "precio": "45.5", "alicuota": "G"})
    a.post(f"/e/{eid}/inventario", data={"accion": "entrada", "codigo": "A001", "cantidad": "10", "costo": "40", "fecha": hoy, "pago": "Contado"})
    r = a.post(f"/e/{eid}/compras/proveedores", data={"rif": "j-66666666-6", "razon_social": "Distribuidora Seis CA", "whatsapp": "0414-1234567", "email": "ventas@seis.test"}, follow_redirects=True)
    assert "Proveedor guardado".encode() in r.data, alerta(r)
    pid = owner.connect().execute(text("SELECT id FROM proveedores WHERE rif='J-66666666-6'")).scalar()
    r = a.post(f"/e/{eid}/compras/ordenes", data={"destinatario": f"P:{pid}", "fecha": hoy, "moneda": "USD", "condicion_pago": "Crédito 15 días",
                                                   "l1_cod": "A001", "l1_cant": "100", "l1_precio": "1.10", "l1_alic": "G",
                                                   "l2_desc": "Flete", "l2_cant": "1", "l2_precio": "20", "l2_alic": "E"})
    assert r.status_code == 302 and "/compras/ordenes/" in r.headers["Location"], alerta(a.get(f"/e/{eid}/compras/ordenes"))
    oc_url = r.headers["Location"]
    doc = a.get(oc_url); t = doc.get_data(as_text=True)
    assert "ORDEN DE COMPRA" in t and "OC-000001" in t and "Delta Comercial CA" in t and "J-44444444-4" in t and "Distribuidora Seis CA" in t
    bs = lambda v: f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    assert "147,60" in t and "17,60" in t and bs((Decimal("147.60") * tasa).quantize(Decimal("0.01"))) in t   # 110 + IVA 16 % + 20 exento
    assert "script-src 'self'" in doc.headers["Content-Security-Policy"]
    publico = re.search(r"https://erp\.test(/oc/[\w.\-]+)", t).group(1)
    anon = _cliente(app)
    assert "Distribuidora Seis CA".encode() in anon.get(publico).data and anon.get(publico[:-3] + "xyz").status_code == 404
    assert x.get(oc_url).status_code == 404                                         # otra organización no la ve

    # --- Bot de WhatsApp: configuración, webhook firmado, pedido de cliente y confirmación del proveedor
    enviados = []
    app.extensions["whatsapp_transporte"] = lambda url, cuerpo, cab: (enviados.append(json.loads(cuerpo)) or (200, b'{"messages":[{"id":"wamid.out%d"}]}' % len(enviados)))
    r = a.post(f"/e/{eid}/whatsapp/bot", data={"accion": "guardar", "phone_number_id": "1098765", "token": "TOKEN-META", "app_secret": "APPSECRET", "activo": "1"}, follow_redirects=True)
    assert "Configuración guardada".encode() in r.data, alerta(r)
    cfg = owner.connect().execute(text("SELECT clave_webhook, verify_token, token_cifrado FROM whatsapp_config WHERE empresa_id=:e"), {"e": eid}).one()
    assert "TOKEN-META" not in cfg[2]                                               # cifrado en reposo
    hook = f"/wa/{cfg[0]}"
    assert anon.get(hook, query_string={"hub.mode": "subscribe", "hub.verify_token": cfg[1], "hub.challenge": "42"}).data == b"42"
    assert anon.get(hook, query_string={"hub.mode": "subscribe", "hub.verify_token": "malo", "hub.challenge": "42"}).status_code == 403
    assert anon.get("/wa/clave-inexistente").status_code == 404
    def aviso(tel, texto, wamid):
        cuerpo = json.dumps({"entry": [{"changes": [{"value": {"contacts": [{"wa_id": tel, "profile": {"name": "Ana"}}],
                                                               "messages": [{"from": tel, "id": wamid, "type": "text", "text": {"body": texto}}]}}]}]}).encode()
        return anon.post(hook, data=cuerpo, content_type="application/json",
                         headers={"X-Hub-Signature-256": "sha256=" + hmac.new(b"APPSECRET", cuerpo, hashlib.sha256).hexdigest()})
    assert anon.post(hook, data=b"{}", content_type="application/json", headers={"X-Hub-Signature-256": "sha256=" + "0" * 64}).status_code == 403
    for i, msg in enumerate(["Hola", "1", "1", "A001 3", "3"]):
        assert aviso("584241112233", msg, f"wamid.c{i}").status_code == 200
    aviso("584241112233", "3", "wamid.c4")                                          # aviso repetido por Meta: no duplica
    assert "Pedido recibido" in enviados[-1]["text"]["body"] and enviados[-1]["to"] == "584241112233"
    ped = owner.connect().execute(text("SELECT numero, tipo, total, estado FROM pedidos WHERE empresa_id=:e"), {"e": eid}).all()
    assert [tuple(p) for p in ped] == [(1, "venta", Decimal("136.50"), "Nuevo")]
    # la empresa envía la OC por WhatsApp y el proveedor (reconocido por su número) la confirma desde el chat
    r = a.post(oc_url + "/accion", data={"accion": "whatsapp"}, follow_redirects=True)
    assert "enviada por WhatsApp".encode() in r.data and enviados[-1]["to"] == "584141234567" and "CONFIRMAR 1" in enviados[-1]["text"]["body"]
    aviso("584141234567", "CONFIRMAR 1", "wamid.p1")
    assert owner.connect().execute(text("SELECT estado FROM ordenes_compra WHERE empresa_id=:e"), {"e": eid}).scalar() == "Confirmada"
    # el proveedor envía una oferta; la empresa la convierte en orden de compra
    for i, msg in enumerate(["2", "A001 200 1,05", "FIN"]): aviso("584141234567", msg, f"wamid.o{i}")
    oferta = owner.connect().execute(text("SELECT id FROM pedidos WHERE tipo='compra' AND empresa_id=:e"), {"e": eid}).scalar()
    r = a.post(f"/e/{eid}/whatsapp/pedidos", data={"accion": "oc", "pid": oferta, "moneda": "USD"}, follow_redirects=True)
    assert "OC-000002".encode() in r.data, alerta(r)
    # pedido de cliente: confirmar y avisar
    pv = owner.connect().execute(text("SELECT id FROM pedidos WHERE tipo='venta' AND empresa_id=:e"), {"e": eid}).scalar()
    a.post(f"/e/{eid}/whatsapp/pedidos", data={"accion": "estado", "pid": pv, "estado": "Confirmado", "avisar": "1"})
    assert "fue confirmado" in enviados[-1]["text"]["body"]
    # recibir la OC 1: la harina entra al inventario al costo en Bs (1,10 USD × tasa BCV)
    r = a.post(oc_url + "/accion", data={"accion": "recibir"}, follow_redirects=True)
    assert "1 producto(s) entraron al inventario".encode() in r.data, r.data[-600:]
    assert owner.connect().execute(text("SELECT cantidad_actual, costo_promedio_actual FROM productos WHERE empresa_id=:e AND codigo='A001'"), {"e": eid}).one() == (Decimal("110"), ((Decimal(400) + Decimal("1.10") * tasa * 100) / 110).quantize(Decimal("0.0001")))
    # aislamiento: la otra organización no ve pedidos, chats ni la configuración del bot
    assert x.get(f"/e/{eid}/whatsapp/pedidos").status_code == 404 and x.get(f"/e/{eid}/compras/ordenes").status_code == 404
    with app.app_context():
        from app import rls
        from app.extensions import db
        from flask import g
        with app.test_request_context():
            g.usuario = None; rls.fijar_org(db, org + 1000)
            assert db.session.execute(text("SELECT count(*) FROM pedidos")).scalar() == 0 and db.session.execute(text("SELECT count(*) FROM whatsapp_config")).scalar() == 0


def test_tutorial_migracion_cuadre_pdf_e_invitaciones(entorno):
    import hashlib, hmac, io, json
    import pytest as _pt
    from sqlalchemy import text
    app, owner = entorno["app"], entorno["owner"]
    hoy = hoy_ve().isoformat()
    uno = lambda sql, **p: owner.connect().execute(text(sql), p).scalar()
    alerta = lambda r: re.findall(r'role="alert">([^<]+)', r.get_data(as_text=True))
    org, a = _org_con_admin(entorno, "Epsilon Market", "J-77777777-7", "adminE")

    # --- Tutorial del primer ingreso: empresa con logo → módulos → órdenes de compra → listo
    assert a.get("/").headers["Location"].endswith("/bienvenida")
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    r = a.post("/bienvenida?paso=1", data={"accion": "empresa", "razon_social": "Epsilon Market CA", "rif": "J-77777777-7", "direccion": "Calle 5, Maracay",
                                          "telefono": "0243-5550000", "correo": "info@epsilon.test", "spe": "0", "logo": (io.BytesIO(png), "logo.png")},
               content_type="multipart/form-data")
    assert "paso=2" in r.headers["Location"], alerta(a.get("/bienvenida"))
    eid = uno("SELECT id FROM empresas WHERE rif='J-77777777-7'")
    assert uno("SELECT logo_mime FROM empresas WHERE id=:e", e=eid) == "image/png" and a.get(f"/e/{eid}/logo").data == png
    r = a.post(f"/bienvenida?paso=1&eid={eid}", data={"accion": "empresa", "razon_social": "x", "logo": (io.BytesIO(b"GIF89a"), "x.gif")},
               content_type="multipart/form-data", follow_redirects=True)
    assert any("PNG, JPG o WebP" in m for m in alerta(r))
    a.post(f"/bienvenida?paso=2&eid={eid}", data={"accion": "modulos", "modulos": ["facturacion", "inventario", "administrativo"]})
    a.post(f"/bienvenida?paso=3&eid={eid}", data={"accion": "oc", "oc_condicion_pago": "Contado", "oc_lugar_entrega": "Galpón 3", "oc_numero_inicial": "500"})
    assert "está lista" in a.get(f"/bienvenida?paso=4&eid={eid}").get_data(as_text=True)
    assert a.get("/").status_code == 200 and uno("SELECT bienvenida_completada FROM organizaciones WHERE id=:o", o=org)
    # módulos por empresa: contabilidad apagada en esta empresa → fuera del menú y 403, aunque el plan la incluya
    assert a.get(f"/e/{eid}/asientos").status_code == 403
    menu = a.get(f"/e/{eid}/inventario").get_data(as_text=True)
    assert "Catálogo de cuentas" not in menu and 'class="nav-modulo' in menu and "Órdenes de compra" in menu
    a.post(f"/e/{eid}/empresa", data={"accion": "modulos", "modulos": ["facturacion", "inventario", "administrativo", "contabilidad"]})
    assert a.get(f"/e/{eid}/asientos").status_code == 200

    # --- Migración desde Profit Plus (Excel con varias hojas): leer → validar → cargar
    openpyxl = _pt.importorskip("openpyxl")
    wb = openpyxl.Workbook(); wb.remove(wb.active)
    for nombre, filas in {"Clientes": [["co_cli", "cli_des", "rif", "direc1", "telefonos"], ["C1", "Comercial Beta CA", "J123456789", "Maracay", "0414"],
                                       ["C2", "Malo", "123", "", ""]],
                          "Articulos": [["co_art", "art_des", "stock_act", "cos_pro_un", "prec_vta1", "tipo_imp"], ["A001", "Harina PAN 1kg", "50", "40", "45,50", "1"],
                                        ["B002", "Aceite 1L", "0", "0", "120", "1"]],
                          "CxC": [["nro_doc", "co_cli", "cli_des", "fec_emis", "fec_venc", "monto_net", "saldo"], ["F-900", "C1", "Comercial Beta CA", "01/09/2026", "01/10/2026", "1000", "600"]]}.items():
        ws = wb.create_sheet(nombre)
        for f in filas: ws.append(f)
    buf = io.BytesIO(); wb.save(buf)
    r = a.post(f"/e/{eid}/migracion", data={"archivo": (io.BytesIO(buf.getvalue()), "profit.xlsx")}, content_type="multipart/form-data")
    assert "/migracion/" in r.headers["Location"], alerta(a.get(f"/e/{eid}/migracion"))
    ms = owner.connect().execute(text("SELECT id, tipo, sistema FROM migraciones WHERE empresa_id=:e ORDER BY id"), {"e": eid}).all()
    assert [(t, s) for _, t, s in ms] == [("clientes", "Profit Plus"), ("productos", "Profit Plus"), ("cxc", "Profit Plus")]
    vista = a.get(f"/e/{eid}/migracion/{ms[0][0]}").get_data(as_text=True)
    assert "✔ Nuevo" in vista and "✘ Error" in vista and "J-12345678-9" in vista
    assert uno("SELECT count(*) FROM clientes_facturacion WHERE empresa_id=:e", e=eid) == 0          # nada se carga sin validar
    for mid, _, _ in ms:
        r = a.post(f"/e/{eid}/migracion/{mid}", data={"accion": "aplicar", "modo": "omitir"}, follow_redirects=True)
        assert any("Migración aplicada" in m for m in alerta(r)), alerta(r)
    assert uno("SELECT count(*) FROM clientes_facturacion WHERE empresa_id=:e", e=eid) == 1
    assert uno("SELECT cantidad_actual FROM productos WHERE empresa_id=:e AND codigo='A001'", e=eid) == Decimal("50")
    assert uno("SELECT count(*) FROM asientos WHERE empresa_id=:e", e=eid) == 0                        # los saldos migrados no generan asientos solos
    r = a.post(f"/e/{eid}/migracion", data={"archivo": (io.BytesIO(buf.getvalue()), "profit.xlsx")}, content_type="multipart/form-data", follow_redirects=True)
    assert "↺ Ya existe" in r.get_data(as_text=True)                                                    # una segunda carga no duplica

    # --- Cuadre: configurar cuentas (plan modelo), asiento de apertura de lo migrado y cuadre de inventario/CxC
    a.post(f"/e/{eid}/cuentas", data={"accion": "modelo"})
    a.post(f"/e/{eid}/config-asientos", data={"cuenta_caja_banco": "1.1.02", "cuenta_cxc": "1.1.04", "cuenta_inventario": "1.1.07", "cuenta_iva_credito_fiscal": "1.1.08",
                                              "cuenta_iva_debito_fiscal": "2.1.02", "cuenta_cxp": "2.1.01", "cuenta_ventas": "4.1.01", "cuenta_costo_venta": "5.1.01",
                                              "cuenta_compras_gastos": "6.1.04"})
    c = a.get(f"/e/{eid}/cuadre").get_data(as_text=True)
    assert "Saldos iniciales migrados" in c and "2000.00" in c and "600" in c                          # 50 × 40 de inventario
    r = a.post(f"/e/{eid}/cuadre", data={"accion": "apertura", "fecha": hoy, "contrapartida": "3.1.01"}, follow_redirects=True)
    assert any("apertura" in m for m in alerta(r)), alerta(r)
    c = a.get(f"/e/{eid}/cuadre").get_data(as_text=True)
    filas = re.findall(r"<tr class=\"([^\"]*)\"><td class=\"\">(Inventario a hoy|Cuentas por cobrar a hoy)", c)
    assert len(filas) == 2 and all(cl == "" for cl, _ in filas), filas                                   # ✔ cuadran

    # --- Orden de compra (numeración desde 500, valores de la empresa) recibida CON la factura del proveedor → libro de compras
    a.post(f"/e/{eid}/compras/proveedores", data={"rif": "J-66666666-6", "razon_social": "Distribuidora Seis CA"})
    pid = uno("SELECT id FROM proveedores WHERE empresa_id=:e", e=eid)
    r = a.post(f"/e/{eid}/compras/ordenes", data={"destinatario": f"P:{pid}", "fecha": hoy, "moneda": "VES", "l1_cod": "A001", "l1_cant": "10", "l1_precio": "40", "l1_alic": "G"})
    oc_url = r.headers["Location"]
    t = a.get(oc_url).get_data(as_text=True)
    assert "OC-000500" in t and "Contado" in t and "Galpón 3" in t and "data:image/png;base64" in t
    r = a.post(oc_url + "/accion", data={"accion": "recibir", "factura": "000123", "control": "00-000456"}, follow_redirects=True)
    assert b"libro de compras" in r.data, r.data[-400:]
    lc = a.get(f"/e/{eid}/cxp/libro-compras?periodo={hoy[:7]}").get_data(as_text=True)
    assert "000123" in lc and "00-000456" in lc and "64.00" in lc and "F-900" not in lc                 # IVA 16 % de 400; lo migrado no entra
    assert uno("SELECT count(*) FROM documentos_cxp WHERE empresa_id=:e AND asiento_id IS NOT NULL", e=eid) == 1
    c = a.get(f"/e/{eid}/cuadre").get_data(as_text=True)
    assert re.search(r'<tr class=""><td class="">IVA crédito fiscal del mes', c) and re.search(r'<tr class=""><td class="">Inventario a hoy', c)

    # --- WhatsApp: el cliente envía su orden de compra en PDF → el bot factura con lo disponible y responde cómo pagar
    canvas = _pt.importorskip("reportlab.pdfgen.canvas")
    def pdf(lineas):
        b = io.BytesIO(); cv = canvas.Canvas(b); y = 800
        for l in lineas: cv.drawString(40, y, l); y -= 18
        cv.save(); return b.getvalue()
    a.post(f"/e/{eid}/facturacion/series", data={"accion": "serie", "serie": "B", "prefijo": "00", "desde": "1", "hasta": "50", "imprenta": "Imprenta Digital SA",
                                                 "imprenta_rif": "J-44444444-4", "providencia": "SENIAT/INTI/2025/0001", "fecha": "2026-01-01"})
    sid = uno("SELECT id FROM series_facturacion WHERE empresa_id=:e", e=eid)
    enviados, archivos = [], {}
    app.extensions["whatsapp_transporte"] = lambda url, cuerpo, cab: (enviados.append(json.loads(cuerpo)) or (200, b'{"messages":[{"id":"wE%d"}]}' % len(enviados)))
    app.extensions["whatsapp_descarga"] = lambda mid: (archivos[mid], "application/pdf")
    r = a.post(f"/e/{eid}/whatsapp/bot", data={"accion": "guardar", "phone_number_id": "555", "token": "T", "app_secret": "S2", "activo": "1",
                                               "instrucciones_pago": "Pague en caja: Calle 5, Maracay, de 8 a 5.", "factura_automatica": "0", "serie_id": str(sid)}, follow_redirects=True)
    assert "Configuración guardada".encode() in r.data, alerta(r)
    hook = "/wa/" + uno("SELECT clave_webhook FROM whatsapp_config WHERE empresa_id=:e", e=eid)
    anon = _cliente(app)
    def documento(wamid, mid):
        cuerpo = json.dumps({"entry": [{"changes": [{"value": {"contacts": [{"wa_id": "584149990000", "profile": {"name": "Comercial Beta"}}],
                 "messages": [{"from": "584149990000", "id": wamid, "type": "document", "document": {"id": mid, "mime_type": "application/pdf", "filename": "OC.pdf"}}]}}]}]}).encode()
        return anon.post(hook, data=cuerpo, content_type="application/json", headers={"X-Hub-Signature-256": "sha256=" + hmac.new(b"S2", cuerpo, hashlib.sha256).hexdigest()})
    archivos["111"] = pdf(["ORDEN DE COMPRA N° 778", "Comercial Beta CA - RIF J-12345678-9", "A001 Harina PAN 1kg 80 UND", "Aceite 1L 3", "Galletas Maria 2"])
    assert documento("wamid.pdf1", "111").status_code == 200
    resp = enviados[-1]["text"]["body"]
    assert "Orden de venta N°" in resp and "Pague en caja" in resp and "pidió 80" in resp and "/ov/" in resp and "Galletas" in resp
    ov = re.search(r"https://erp\.test(/ov/[\w.\-]+)", resp).group(1)
    assert "ORDEN DE VENTA" in anon.get(ov).get_data(as_text=True)
    assert uno("SELECT count(*) FROM documentos_venta WHERE empresa_id=:e", e=eid) == 0
    a.post(f"/e/{eid}/whatsapp/bot", data={"accion": "guardar", "phone_number_id": "555", "activo": "1", "factura_automatica": "1", "serie_id": str(sid), "dias_credito": "7",
                                           "instrucciones_pago": "Pague en caja: Calle 5, Maracay, de 8 a 5."})
    archivos["222"] = pdf(["Orden de compra N° 779", "RIF: J-12345678-9", "A001 Harina PAN 1kg 5"])
    documento("wamid.pdf2", "222")
    resp = enviados[-1]["text"]["body"]
    assert "Factura N° B00000001" in resp, resp
    # la factura del bot quedó en el libro de ventas, descargó el inventario y creó su cuenta por cobrar
    lv = a.get(f"/e/{eid}/facturacion/libro-ventas?periodo={hoy[:7]}").get_data(as_text=True)
    assert "Comercial Beta CA" in lv
    assert uno("SELECT cantidad_actual FROM productos WHERE empresa_id=:e AND codigo='A001'", e=eid) == Decimal("55")   # 50 + 10 (OC) − 5
    assert uno("SELECT count(*) FROM documentos_cxc WHERE empresa_id=:e AND deudor_rif='J-12345678-9' AND origen='manual'", e=eid) == 1
    assert uno("SELECT estado FROM pedidos WHERE empresa_id=:e AND referencia_cliente='779'", e=eid) == "Facturado"

    # --- Misma empresa conectada por WasenderAPI (WhatsApp por QR): aviso con su secreto, respuesta por /send-message
    ws_env = []
    def ws_t(metodo, url, cuerpo, cab):
        ws_env.append((url, json.loads(cuerpo) if cuerpo else None, cab)); return 200, b'{"success":true,"data":{"msgId":%d}}' % len(ws_env)
    app.extensions["wasender_transporte"] = ws_t
    r = a.post(f"/e/{eid}/whatsapp/bot", data={"accion": "guardar", "proveedor": "wasender", "token": "WS-KEY", "app_secret": "WS-SECRET", "activo": "1",
                                               "numero_visible": "+58 414 0000000", "factura_automatica": "1", "serie_id": str(sid),
                                               "instrucciones_pago": "Pague en caja: Calle 5, Maracay, de 8 a 5."}, follow_redirects=True)
    assert "Configuración guardada".encode() in r.data and "WasenderAPI (WhatsApp por código QR)".encode() in r.data, alerta(r)
    assert anon.get(hook).status_code == 200                                                           # WasenderAPI no usa el reto de Meta
    def ws_aviso(m, firma="WS-SECRET"):
        return anon.post(hook, data=json.dumps({"event": "messages.received", "data": {"messages": m}}).encode(), content_type="application/json",
                         headers={"X-Webhook-Signature": firma})
    hola = {"key": {"id": "WS1", "fromMe": False, "remoteJid": "584125550000@s.whatsapp.net"}, "pushName": "Luis", "message": {"conversation": "Hola"}}
    assert ws_aviso(hola, "otra").status_code == 403
    assert ws_aviso(hola).status_code == 200
    assert ws_env[-1][0] == "https://www.wasenderapi.com/api/send-message" and ws_env[-1][1]["to"] == "+584125550000" and ws_env[-1][2]["Authorization"] == "Bearer WS-KEY"
    archivos["WS2"] = pdf(["Orden de compra N° 780", "RIF J-12345678-9", "A001 Harina PAN 1kg 2"])
    ws_aviso({"key": {"id": "WS2", "fromMe": False, "remoteJid": "584149990000@s.whatsapp.net"},
              "message": {"documentMessage": {"mimetype": "application/pdf", "fileName": "OC780.pdf"}}})
    assert "Factura N° B00000002" in ws_env[-1][1]["text"], ws_env[-1][1]["text"]
    assert uno("SELECT cantidad_actual FROM productos WHERE empresa_id=:e AND codigo='A001'", e=eid) == Decimal("53")

    # --- Presupuestos de la empresa: manual, por el bot (automático y manual), aceptación por WhatsApp y facturación
    cli = uno("SELECT id FROM clientes_facturacion WHERE empresa_id=:e AND rif='J-12345678-9'", e=eid)
    r = a.post(f"/e/{eid}/presupuestos", data={"cliente": cli, "telefono": "584149990000", "validez": "10", "l1_cod": "A001", "l1_cant": "2", "l2_desc": "Flete", "l2_cant": "1", "l2_precio": "15"})
    assert "/presupuestos/" in r.headers["Location"], alerta(a.get(f"/e/{eid}/presupuestos"))
    pr_url = r.headers["Location"]
    t = a.get(pr_url).get_data(as_text=True)
    assert "PRESUPUESTO" in t and "PR-000001" in t and "Comercial Beta CA" in t and "91,00" in t and "Epsilon Market CA" in t
    r = a.post(pr_url, data={"accion": "whatsapp"}, follow_redirects=True)
    assert "enviado por WhatsApp" in r.get_data(as_text=True) and "Presupuesto N° 1" in ws_env[-1][1]["text"]
    pv_pub = re.search(r"https://erp\.test(/pv/[\w.\-]+)", ws_env[-1][1]["text"]).group(1)
    assert "PR-000001" in anon.get(pv_pub).get_data(as_text=True)
    luis = lambda txt, i: ws_aviso({"key": {"id": f"L{i}", "fromMe": False, "remoteJid": "584125550000@s.whatsapp.net"}, "pushName": "Luis", "message": {"conversation": txt}})
    for i, txt in enumerate(["1", "A001 3", "6"]): luis(txt, i)
    assert "Presupuesto N° 2" in ws_env[-1][1]["text"] and "/pv/" in ws_env[-1][1]["text"]                      # modo automático
    luis("ACEPTO 2", 3)
    assert uno("SELECT estado FROM presupuestos_venta WHERE empresa_id=:e AND numero=2", e=eid) == "Aceptado"
    a.post(f"/e/{eid}/whatsapp/bot", data={"accion": "guardar", "proveedor": "wasender", "activo": "1", "factura_automatica": "1", "serie_id": str(sid), "presupuesto_modo": "manual"})
    for i, txt in enumerate(["A001 1", "presupuesto"], start=4): luis(txt, i)
    assert "Un asesor lo revisará" in ws_env[-1][1]["text"]
    assert uno("SELECT estado FROM presupuestos_venta WHERE empresa_id=:e AND numero=3", e=eid) == "Borrador"
    r = a.post(pr_url, data={"accion": "facturar", "serie_id": str(sid), "dias": "5"}, follow_redirects=True)
    assert "Factura N° B00000003" in r.get_data(as_text=True), r.get_data(as_text=True)[-800:]
    assert uno("SELECT estado FROM presupuestos_venta WHERE empresa_id=:e AND numero=1", e=eid) == "Facturado"
    nuevo_pc = a.get(f"/e/{eid}/presupuestos/nuevo").get_data(as_text=True)
    assert "Productos y servicios" in nuevo_pc and 'id="datos-presupuesto-cliente"' in nuevo_pc and "Comercial Beta CA" in nuevo_pc

    # --- Maestro: cliente que no se registró en 72 h → reenviar invitación o eliminarlo
    from app import totp
    from app.extensions import db
    from app.models import Usuario
    from app.security import hashear
    with entorno["app_owner"].app_context():
        sec = totp.nuevo_secreto()
        db.session.add(Usuario(username="root2", email="root2@prov.test", email_verificado=True, rol="Maestro", password_hash=hashear(CLAVE),
                               debe_cambiar_clave=False, mfa_activo=True, mfa_secreto_cifrado=entorno["app_owner"].extensions["cifrador"].cifrar(sec)))
        db.session.commit()
    root = _cliente(app)
    root.post("/login", data={"usuario": "root2", "clave": CLAVE}); root.post("/mfa/verificar", data={"codigo": totp.codigo(sec)})
    root.post("/plataforma", data={"accion": "crear", "nombre": "Zeta Olvidadiza", "rif": "J-88888888-8", "username": "adminZ", "email": "z@zeta.test"})
    with owner.connect() as cx:
        cx.execute(text("UPDATE tokens_correo SET expira_en = now() - interval '1 hour' WHERE usuario_id = (SELECT id FROM usuarios WHERE username='adminZ')")); cx.commit()
    p = root.get("/plataforma").get_data(as_text=True)
    assert "VENCIDA (72 h sin registrarse)" in p and "Clientes que no se registraron" in p
    oz = uno("SELECT id FROM organizaciones WHERE rif='J-88888888-8'")
    r = root.post("/plataforma", data={"accion": "reinvitar", "oid": oz}, follow_redirects=True)
    assert any("Invitación reenviada a z@zeta.test" in m for m in alerta(r)) and "Pendiente (vence" in r.get_data(as_text=True)
    r = root.post("/plataforma", data={"accion": "eliminar", "oid": org, "confirmar": "J-77777777-7"}, follow_redirects=True)
    assert any("ya activó" in m for m in alerta(r))                                                     # quien trabaja no se borra
    r = root.post("/plataforma", data={"accion": "eliminar", "oid": oz, "confirmar": "J-00000000-0"}, follow_redirects=True)
    assert any("RIF" in m for m in alerta(r)) and uno("SELECT count(*) FROM organizaciones WHERE id=:o", o=oz) == 1
    r = root.post("/plataforma", data={"accion": "eliminar", "oid": oz, "confirmar": "J-88888888-8"}, follow_redirects=True)
    assert any("eliminado" in m for m in alerta(r)) and uno("SELECT count(*) FROM organizaciones WHERE id=:o", o=oz) == 0
    assert uno("SELECT count(*) FROM usuarios WHERE username='adminZ'") == 0

    # --- Maestro: presupuesto de Gigadevs con precios vigentes (anual) + servicio, envío por correo y actualización de precios
    assert "Precios vigentes (mensual y anual)" in root.get("/plataforma/presupuestos").get_data(as_text=True)
    r = root.post("/plataforma/presupuestos", data={"accion": "crear", "cliente_nombre": "General de Suministros Orituco", "cliente_correo": "compras@orituco.test",
                                                    "periodo": "anual", "items": ["plan:ALL_IN_ONE"], "validez": "15", "condiciones": "Pago 50/50",
                                                    "l1_desc": "Implementación y migración", "l1_cant": "1", "l1_precio": "100", "l1_alic": "G"})
    assert "/plataforma/presupuestos/" in r.headers["Location"], alerta(root.get("/plataforma/presupuestos"))
    t = root.get(r.headers["Location"]).get_data(as_text=True)
    assert "PRESUPUESTO" in t and "P-000001" in t and "General de Suministros Orituco" in t and "All in One" in t and "806,00" in t and "Conta" in t
    n = len(entorno["correos_lic"])
    root.post(r.headers["Location"], data={"accion": "enviar"})
    assert len(entorno["correos_lic"]) == n + 1 and entorno["correos_lic"][-1][0] == "compras@orituco.test"
    root.post("/plataforma/presupuestos", data={"accion": "precio", "plan": "CASH", "meses": "12", "monto": "260"})
    assert "260.00" in root.get("/plataforma/presupuestos").get_data(as_text=True)

    # --- Nuevo presupuesto (formulario por secciones), contrato desde el presupuesto aceptado y firma electrónica del cliente
    nuevo = root.get("/plataforma/presupuestos/nuevo").get_data(as_text=True)
    for ruta_m, texto in (("/plataforma", "Nuevo cliente"), ("/plataforma/licencias", "Emitir licencia"), ("/plataforma/pagos", "Nuevo combo personalizado")):
        t = root.get(ruta_m).get_data(as_text=True)
        assert 'class="px px-asistente"' in t and texto in t, ruta_m
    assert "Del catálogo" in nuevo and 'id="datos-presupuesto"' in nuevo and "plan:ALL_IN_ONE" in nuevo
    px_url = r.headers["Location"]
    root.post(px_url, data={"accion": "Aceptado"})
    assert "Armar contrato" in root.get(px_url).get_data(as_text=True)
    form = root.get("/plataforma/contratos").get_data(as_text=True)
    assert "Armar contrato" in form and "General de Suministros Orituco" in form
    r = root.post("/plataforma/contratos", data={"presupuesto_id": re.search(r"/presupuestos/(\d+)", px_url).group(1), "cliente_nombre": "General de Suministros Orituco, C.A.",
                                                 "cliente_rif": "J-31234567-8", "cliente_domicilio": "Altagracia de Orituco, Guárico", "cliente_correo": "compras@orituco.test",
                                                 "representante": "Ana Pérez", "representante_ci": "V-12345678", "representante_cargo": "Presidenta",
                                                 "objeto": "Licencia Contax All in One por 12 meses", "forma_pago": "100% al firmar", "inicio": "2026-11-01", "meses": "12",
                                                 "opcionales": ["soporte", "arbitraje"], "soporte_mensual": "36"})
    assert "/plataforma/contratos/" in r.headers["Location"], alerta(root.get("/plataforma/contratos"))
    ct_url = r.headers["Location"]
    t = root.get(ct_url).get_data(as_text=True)
    for ref in ("CONTRATO DE LICENCIA DE USO", "C-000001", "LOCDOFT", "ONCDOFT", "Delitos Informáticos", "Mensajes de Datos", "SENIAT", "CEDCA", "Ana Pérez", "31/10/2027"):
        assert ref in t, ref
    n = len(entorno["correos_lic"])
    root.post(ct_url, data={"accion": "enviar"})
    assert len(entorno["correos_lic"]) == n + 1 and entorno["correos_lic"][-1][0] == "compras@orituco.test"
    firma = re.search(r"https://erp\.test(/contrato/[\w.\-]+)", entorno["correos_lic"][-1][1]).group(1)
    cli = _cliente(app)
    pagina_firma = cli.get(firma).get_data(as_text=True)
    assert "Aceptación electrónica" in pagina_firma and "Ana Pérez" in pagina_firma
    assert "Marque la casilla" in cli.post(firma, data={"nombre": "Ana Pérez", "ci": "V-12345678"}, follow_redirects=True).get_data(as_text=True)
    r = cli.post(firma, data={"nombre": "Ana Pérez", "ci": "V-12345678", "acepto": "1"}, follow_redirects=True)
    assert "ACEPTADO ELECTRÓNICAMENTE" in r.get_data(as_text=True) and "Aceptación electrónica" not in r.get_data(as_text=True)
    assert "coincide con la huella firmada" in root.get(ct_url).get_data(as_text=True)
    assert cli.get(firma[:-4] + "abcd").status_code == 404
    # portada pública de Contax: funciones, planes con los precios vigentes del catálogo y enlaces legales
    portada = _cliente(app).get("/").get_data(as_text=True)
    for txt in ("Factura, lleva tu contabilidad y vende por WhatsApp", "Bot de WhatsApp", "Planes", "All in One", "$69", "Iniciar sesión", "/legal/terminos"):
        assert txt in portada, txt
    # páginas legales públicas
    for pag, titulo in (("terminos", "Términos y condiciones"), ("privacidad", "Política de privacidad"), ("cookies", "Política de cookies")):
        t = _cliente(app).get(f"/legal/{pag}").get_data(as_text=True)
        assert titulo in t and "Gigadevs Corp" in t
    assert "__Host-erp" in _cliente(app).get("/legal/cookies").get_data(as_text=True)
    assert "Términos y condiciones" in _cliente(app).get("/login").get_data(as_text=True)
