"""Servidor local de demostración para el recorrido visual con Playwright (tests/e2e/recorrido.py).

  ERP_TEST_DATABASE_URL=postgresql+psycopg://postgres:CLAVE@localhost:55432/erp python tests/e2e/servidor_demo.py [puerto]
¡La base indicada se BORRA! Crea un Maestro y una organización con datos de muestra y escribe los accesos de prueba
(usuario, clave y secreto TOTP, todos ficticios) en tests/e2e/.accesos.json para que el recorrido inicie sesión.
"""
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
os.environ.setdefault("FLASK_ENV", "testing")
import tests.test_integracion_pg as T  # noqa: E402  (prepara las variables de entorno de prueba)

ACCESOS = pathlib.Path(__file__).with_name(".accesos.json")


def sembrar(entorno):
    from app import totp
    from app.extensions import db
    from app.models import Organizacion, Usuario
    from app.security import hashear
    app, own = entorno["app"], entorno["app_owner"]
    cif = own.extensions["cifrador"]
    sec_root, sec_demo = totp.nuevo_secreto(), totp.nuevo_secreto()
    with own.app_context():
        db.session.add(Usuario(username="root", email="root@demo.test", email_verificado=True, rol="Maestro", password_hash=hashear(T.CLAVE),
                               debe_cambiar_clave=False, mfa_activo=True, mfa_secreto_cifrado=cif.cifrar(sec_root)))
        org = Organizacion(nombre="Demo Contadores", rif="J-11111111-1", estado="activa"); db.session.add(org); db.session.flush()
        db.session.add(Usuario(username="demo", email="demo@demo.test", email_verificado=True, rol="Administrador", organizacion_id=org.id,
                               password_hash=hashear(T.CLAVE), debe_cambiar_clave=False, mfa_activo=True, mfa_secreto_cifrado=cif.cifrar(sec_demo)))
        db.session.commit()

    def entrar(usuario, secreto):
        c = app.test_client(use_cookies=True)
        c.post("/login", data={"usuario": usuario, "clave": T.CLAVE}); c.post("/mfa/verificar", data={"codigo": totp.codigo(secreto)})
        return c
    root = entrar("root", sec_root)
    hoy = T.hoy_ve().isoformat()
    for cod, pct in (("G", "16"), ("R", "8"), ("A", "15"), ("IGTF", "3")):
        root.post("/plataforma/fiscal", data={"accion": "alicuota", "codigo": cod, "valor": pct, "fecha": "2026-01-01"})
    root.post("/plataforma/fiscal", data={"accion": "tasa", "valor": "36.5", "eur": "40,25", "fecha": hoy})
    d = entrar("demo", sec_demo)
    d.post("/", data={"razon_social": "Comercial Demo, C.A.", "rif": "J-22222222-2", "direccion": "Caracas", "telefono": "0212-0000000", "spe": "0"})
    from sqlalchemy import text
    with entorno["owner"].connect() as c:      # como dueño: fuera de una sesión, RLS no deja ver filas
        eid = c.execute(text("SELECT id FROM empresas ORDER BY id LIMIT 1")).scalar()
    cuentas = [("1.1.01", "Caja", "Activo", "1"), ("1.1.02", "Bancos", "Activo", "1"), ("1.1.03", "Cuentas por cobrar", "Activo", "1"),
               ("1.1.04", "Inventario de mercancía", "Activo", "0"), ("2.1.01", "Cuentas por pagar", "Pasivo", "1"), ("2.1.02", "IVA débito fiscal", "Pasivo", "1"),
               ("2.1.03", "IGTF por pagar", "Pasivo", "1"), ("3.1.01", "Capital social", "Patrimonio", "0"), ("3.1.02", "Resultados acumulados", "Patrimonio", "0"),
               ("4.1.01", "Ventas", "Ingreso", "0"), ("5.1.01", "Costo de ventas", "Gasto", "0"), ("5.2.01", "Sueldos", "Gasto", "0")]
    cuentas += [(f"5.2.{i:02d}", f"Gasto operativo {i}", "Gasto", "0") for i in range(2, 22)]   # suficientes para paginar
    for cod, nom, tipo, mon in cuentas:
        d.post(f"/e/{eid}/cuentas", data={"codigo": cod, "nombre": nom, "tipo": tipo, "categoria": "Operacion", "monetaria": mon})
    for i, (c1, c2, monto, concepto) in enumerate([("1.1.02", "3.1.01", "50000", "Aporte de capital"), ("1.1.04", "1.1.02", "12000", "Compra de mercancía"),
                                                   ("1.1.01", "4.1.01", "8000", "Venta de contado"), ("5.2.01", "1.1.02", "3000", "Pago de nómina")]):
        d.post(f"/e/{eid}/asientos", data={"fecha": hoy, "concepto": concepto, "c1": c1, "d1": monto, "c2": c2, "h2": monto})
    ACCESOS.write_text(json.dumps({"clave": T.CLAVE, "root": sec_root, "demo": sec_demo, "eid": eid}))
    return app


if __name__ == "__main__":
    if not T.URL: sys.exit("Defina ERP_TEST_DATABASE_URL (la base se borra)")
    entorno = T.crear_entorno()
    app = sembrar(entorno)
    puerto = int(sys.argv[1]) if len(sys.argv) > 1 else 5055
    app.config.update(SESSION_COOKIE_SECURE=False, WTF_CSRF_ENABLED=True, APP_BASE_URL=f"http://127.0.0.1:{puerto}")
    print(f"ERP de demostración en http://127.0.0.1:{puerto}", flush=True)
    app.run("127.0.0.1", puerto, debug=False, use_reloader=False)
