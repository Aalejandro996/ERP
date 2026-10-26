"""CLI del PROVEEDOR (alternativa a la consola del Maestro; útil para el arranque y emergencias).

  python -m licensing_server.cli keygen                       # par Ed25519 (guarde la privada fuera del repositorio)
  python -m licensing_server.cli crear "Cliente SA" --plan ALL_IN_ONE --dias 365 --usuarios 5 --empresas 10 --equipos 5
  python -m licensing_server.cli plan ID CASH                 # ERP_CONTABLE | CASH | ERP_ADMIN | ALL_IN_ONE
  python -m licensing_server.cli estado ID suspendida|revocada|activa
  python -m licensing_server.cli liberar ID                   # permite activar en otra instalación
  python -m licensing_server.cli precio ALL_IN_ONE 12 240 --usuarios 10 --empresas 5 --equipos 10
  python -m licensing_server.cli listar
Variable: LICENSE_DB_URL o DATABASE_URL (por defecto sqlite:///licencias.db)
"""
import argparse
import base64
import os
import sys
import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, ".")
from licensing_server.server import PLANES, Almacen  # noqa: E402


def main():
    p = argparse.ArgumentParser(); sp = p.add_subparsers(dest="cmd", required=True)
    sp.add_parser("keygen"); sp.add_parser("listar")
    c = sp.add_parser("crear"); c.add_argument("cliente"); c.add_argument("--dias", type=int, default=365)
    c.add_argument("--usuarios", type=int, default=5); c.add_argument("--empresas", type=int, default=10); c.add_argument("--instancias", type=int, default=1)
    c.add_argument("--equipos", type=int); c.add_argument("--plan", choices=PLANES, required=True)
    pl = sp.add_parser("plan"); pl.add_argument("id", type=int); pl.add_argument("plan", choices=PLANES)
    e = sp.add_parser("estado"); e.add_argument("id", type=int); e.add_argument("estado", choices=["activa", "suspendida", "revocada"])
    l = sp.add_parser("liberar"); l.add_argument("id", type=int)
    pr = sp.add_parser("precio"); pr.add_argument("plan", choices=PLANES); pr.add_argument("meses", type=int); pr.add_argument("usd")
    pr.add_argument("--usuarios", type=int, default=5); pr.add_argument("--empresas", type=int, default=2); pr.add_argument("--equipos", type=int, default=5)
    a = p.parse_args()
    if a.cmd == "keygen":
        k = Ed25519PrivateKey.generate()
        priv = k.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
        pub = k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        b = lambda x: base64.urlsafe_b64encode(x).rstrip(b"=").decode()
        print(f"LICENSE_PRIVATE_KEY (SOLO servidor de licencias, secreto): {b(priv)}\nLICENSE_PUBLIC_KEY  (va en el ERP):  {b(pub)}"); return
    st = Almacen(os.environ.get("LICENSE_DB_URL") or os.environ.get("DATABASE_URL") or "sqlite:///licencias.db")
    if a.cmd == "crear":
        lid, clave = st.crear_licencia(a.cliente, a.dias, a.usuarios, a.empresas, a.instancias, a.plan, a.equipos)
        print(f"Licencia #{lid}. Clave (se muestra UNA sola vez): {clave}")
    elif a.cmd == "plan": st.modificar(a.id, {"plan": a.plan}); print("Plan actualizado (llega en la próxima renovación)")
    elif a.cmd == "estado": st.modificar(a.id, {"estado": a.estado}); print("Estado actualizado")
    elif a.cmd == "liberar": st.liberar(a.id); print("Instalaciones liberadas")
    elif a.cmd == "precio": st.fijar_precio(a.plan, a.meses, a.usd, a.usuarios, a.empresas, a.equipos); print("Precio guardado")
    else:
        for r in st.listar():
            print(f"#{r['id']:<4} {r['cliente'][:28]:28} {r['plan'] or 'LEGADO':12} {r['estado']:10} inst={r['en_uso']}/{r['max_instancias']} "
                  f"usuarios≤{r['max_usuarios']} empresas≤{r['max_empresas']} equipos≤{r['max_equipos'] or '∞'} vence={time.strftime('%Y-%m-%d', time.gmtime(r['vence_en']))}")


if __name__ == "__main__":
    main()
