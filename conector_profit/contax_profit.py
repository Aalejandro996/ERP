"""Conector Contax ↔ Profit Plus (SOLO LECTURA) — corre en el servidor de Profit Plus del cliente (Windows).

Lee de SQL Server artículos, existencias por almacén, precios de venta y clientes con un usuario que solo tiene SELECT,
y envía la «foto» a Contax por HTTPS (solo conexiones de SALIDA: no hay que abrir puertos). Nunca escribe en Profit Plus.
Qué almacenes y qué lista de precios se toman lo decide la empresa en Contax (Clientes → Profit Plus); este programa
lo recibe en cada conexión. La conexión a SQL Server vive SOLO en contax_profit.ini, en este equipo.

Uso:
  contax_profit --probar          prueba SQL Server y Contax, muestra conteos
  contax_profit --una-vez         sincroniza ahora (si toca) y termina — es lo que ejecuta la tarea programada
  contax_profit --forzar          sincroniza ahora aunque no haya pasado el intervalo
  contax_profit --instalar-tarea  crea la tarea programada de Windows (cada 5 min revisa; sincroniza según el intervalo de Contax)
  contax_profit --quitar-tarea
Requiere Python 3.10+ y pyodbc (o use contax_profit.exe, que ya los trae) y el «ODBC Driver 17/18 for SQL Server»."""
import argparse
import configparser
import gzip
import hashlib
import hmac
import json
import logging
import logging.handlers
import os
import platform
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

VERSION = "1.0.0"
CARPETA = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
INI = CARPETA / "contax_profit.ini"
ESTADO = CARPETA / "contax_profit_estado.json"
log = logging.getLogger("contax_profit")

# Consultas para Profit Plus 2K12 (12.x). Se pueden reemplazar en la sección [consultas] del .ini (otras versiones).
# Todas son SELECT: el usuario de lectura no tiene ningún otro permiso.
CONSULTAS_2K12 = {
    "almacenes": "SELECT RTRIM(co_alma) AS co_alma, RTRIM(des_alma) AS des_alma, noventa FROM saAlmacen",
    "listas": "SELECT RTRIM(co_precio) AS co_precio, RTRIM(des_precio) AS des_precio FROM saTipoPrecio",
    "articulos": "SELECT RTRIM(co_art) AS co_art, RTRIM(art_des) AS art_des, RTRIM(co_lin) AS co_lin, RTRIM(tipo_imp) AS tipo_imp "
                 "FROM saArticulo WHERE anulado = 0",
    "stock": "SELECT RTRIM(co_art) AS co_art, RTRIM(co_alma) AS co_alma, RTRIM(tipo) AS tipo, SUM(stock) AS stock "
             "FROM saStockAlmacen GROUP BY co_art, co_alma, tipo",
    "precios": "SELECT RTRIM(co_art) AS co_art, RTRIM(ISNULL(co_alma, '')) AS co_alma, monto, RTRIM(ISNULL(co_mone, '')) AS co_mone, desde "
               "FROM saArtPrecio WHERE Inactivo = 0 AND RTRIM(co_precio) = ? AND desde <= GETDATE() AND (hasta IS NULL OR hasta >= GETDATE())",
    "clientes": "SELECT RTRIM(co_cli) AS co_cli, RTRIM(cli_des) AS cli_des, RTRIM(rif) AS rif, RTRIM(CAST(direc1 AS varchar(400))) AS direc1, "
                "RTRIM(telefonos) AS telefonos, RTRIM(email) AS email FROM saCliente WHERE inactivo = 0",
}


class ErrorConector(Exception):
    pass


# ====================================================================== lógica pura (se prueba sin SQL Server)
def _d(v) -> Decimal:
    try:
        return Decimal(str(v if v is not None else 0))
    except (InvalidOperation, ValueError):
        return Decimal(0)


def moneda(co_mone) -> str:
    m = str(co_mone or "").strip().upper().replace("$", "S")
    return "USD" if m.startswith(("US", "DOL")) else "EUR" if m.startswith("EU") else "VES"


def elegir_precio(filas, almacenes):
    """De las filas vigentes de la lista elegida para UN artículo: la general (sin almacén) más reciente; si no hay, la de un
    almacén elegido; si no, cualquiera. filas: [(co_alma, monto, co_mone, desde)]."""
    if not filas: return None
    orden = lambda f: f[3] or datetime.min
    for grupo in ([f for f in filas if not f[0]], [f for f in filas if f[0] in almacenes] if almacenes else [], filas):
        if grupo: return max(grupo, key=orden)
    return None


def construir_foto(almacenes_rows, listas_rows, articulos_rows, stock_rows, precios_rows, clientes_rows, config, alicuotas,
                   tipo_actual="ACT", tipo_comprometido="COM") -> dict:
    """Arma la foto que espera Contax (app/profit.py) a partir de las filas leídas de SQL Server."""
    elegidos = set(config.get("almacenes") or [])
    de_venta = {r[0] for r in almacenes_rows if not r[2]}
    cuentan = elegidos or de_venta or {r[1] for r in stock_rows}
    stock = {}
    for co_art, co_alma, tipo, cant in stock_rows:
        if co_alma not in cuentan: continue
        a = stock.setdefault(co_art, {}).setdefault(co_alma, [Decimal(0), Decimal(0)])
        if tipo == tipo_actual: a[0] += _d(cant)
        elif tipo == tipo_comprometido: a[1] += _d(cant)
    precios = {}
    for co_art, co_alma, monto, co_mone, desde in precios_rows:
        precios.setdefault(co_art, []).append((co_alma or "", monto, co_mone, desde))
    restar = config.get("restar_comprometido", True)
    articulos = []
    for co_art, art_des, co_lin, tipo_imp in articulos_rows:
        p = elegir_precio(precios.get(co_art), elegidos)
        por = {alm: (v[0] - (v[1] if restar else 0)) for alm, v in (stock.get(co_art) or {}).items()}
        articulos.append({"codigo": co_art, "nombre": art_des, "linea": co_lin, "alicuota": alicuotas.get(str(tipo_imp or "").strip(), "G"),
                          "precio": str(_d(p[1])) if p else "0", "moneda": moneda(p[2]) if p else "VES",
                          "existencia": str(max(sum(por.values(), Decimal(0)), Decimal(0))),
                          "por_almacen": {k: str(v) for k, v in sorted(por.items())}})
    clientes = [{"codigo": r[0], "nombre": r[1], "rif": r[2], "direccion": r[3], "telefonos": r[4], "email": r[5]} for r in clientes_rows]
    return {"version": VERSION, "equipo": platform.node()[:60], "esquema": "2k12",
            "disponibles": {"almacenes": [{"codigo": r[0], "nombre": r[1]} for r in almacenes_rows],
                            "listas_precio": [{"codigo": r[0], "nombre": r[1]} for r in listas_rows]},
            "config_aplicada": {"almacenes": sorted(cuentan), "lista_precio": config.get("lista_precio", "01")},
            "articulos": articulos, "clientes": clientes}


def firmar(secreto: str, fecha: int, cuerpo: bytes) -> str:
    msg = f"{int(fecha)}.{hashlib.sha256(cuerpo).hexdigest()}".encode()
    return "sha256=" + hmac.new(secreto.encode(), msg, hashlib.sha256).hexdigest()


def toca_sincronizar(estado: dict, ahora: float) -> bool:
    intervalo = int((estado.get("config") or {}).get("intervalo_min") or 15)
    return ahora - float(estado.get("ultima_sync") or 0) >= intervalo * 60 - 30


# ====================================================================== configuración y estado local
def leer_ini(ruta=INI) -> configparser.ConfigParser:
    if not ruta.exists(): raise ErrorConector(f"No existe {ruta}. Descárguelo en Contax → Clientes → Profit Plus y cópielo junto a este programa.")
    cfg = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=(";",))
    cfg.read(ruta, encoding="utf-8-sig")
    for sec, claves in (("contax", ("url", "conector", "clave")), ("sqlserver", ("servidor", "base_datos"))):
        for k in claves:
            if not cfg.get(sec, k, fallback="").strip(): raise ErrorConector(f"Falta «{k}» en la sección [{sec}] de {ruta.name}")
    if not cfg.get("contax", "url").startswith("https://") and "localhost" not in cfg.get("contax", "url"):
        raise ErrorConector("La URL de Contax debe empezar por https://")
    return cfg


def leer_estado() -> dict:
    try:
        return json.loads(ESTADO.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def guardar_estado(e: dict):
    tmp = ESTADO.with_suffix(".tmp"); tmp.write_text(json.dumps(e, ensure_ascii=False, indent=1), encoding="utf-8"); tmp.replace(ESTADO)


# ====================================================================== SQL Server (solo SELECT)
def conectar_sql(cfg):
    s = cfg["sqlserver"]
    try:
        import pyodbc
    except ImportError as e:
        raise ErrorConector("Falta pyodbc: instálelo con «pip install pyodbc» o use contax_profit.exe") from e
    driver = s.get("driver", "").strip()
    if not driver:
        disponibles = [d for d in pyodbc.drivers() if "SQL Server" in d]
        preferidos = [d for d in ("ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server", "SQL Server Native Client 11.0", "SQL Server") if d in disponibles]
        if not preferidos: raise ErrorConector("No hay un driver ODBC de SQL Server instalado (instale «ODBC Driver 18 for SQL Server» de Microsoft)")
        driver = preferidos[0]
    partes = [f"DRIVER={{{driver}}}", f"SERVER={s['servidor']}", f"DATABASE={s['base_datos']}", "ApplicationIntent=ReadOnly", "APP=Contax conector Profit"]
    if s.get("usuario", "").strip(): partes += [f"UID={s['usuario']}", f"PWD={s.get('contrasena', '')}"]
    else: partes.append("Trusted_Connection=yes")
    if "18" in driver or "17" in driver:
        partes += [f"Encrypt={'yes' if s.getboolean('cifrar', fallback=False) else 'no'}", "TrustServerCertificate=yes"]
    try:
        cn = pyodbc.connect(";".join(partes), timeout=15, readonly=True)
    except pyodbc.Error as e:
        raise ErrorConector(f"No se pudo conectar a SQL Server: {str(e)[:300]}") from e
    return cn


def consultar(cn, sql, *params):
    cur = cn.cursor()
    try:
        cur.execute(sql, *params)
        return [tuple(r) for r in cur.fetchall()]
    finally:
        cur.close()


def leer_profit(cfg, config_contax) -> dict:
    q = {k: cfg.get("consultas", k, fallback=v) for k, v in CONSULTAS_2K12.items()}
    alicuotas = {k.strip(): v.strip().upper() for k, v in (cfg["alicuotas"].items() if cfg.has_section("alicuotas") else [("1", "G"), ("2", "R"), ("3", "A"), ("7", "E")])}
    cn = conectar_sql(cfg)
    try:
        t0 = time.monotonic()
        try: listas = consultar(cn, q["listas"])
        except Exception:  # noqa: BLE001 — la tabla de listas es opcional
            listas = []
        datos = dict(almacenes_rows=consultar(cn, q["almacenes"]), listas_rows=listas, articulos_rows=consultar(cn, q["articulos"]),
                     stock_rows=consultar(cn, q["stock"]), precios_rows=consultar(cn, q["precios"], config_contax.get("lista_precio", "01")),
                     clientes_rows=consultar(cn, q["clientes"]))
        log.info("Profit leído en %.1f s: %d artículos, %d clientes", time.monotonic() - t0, len(datos["articulos_rows"]), len(datos["clientes_rows"]))
    finally:
        cn.close()
    return construir_foto(**datos, config=config_contax, alicuotas=alicuotas,
                          tipo_actual=cfg.get("profit", "stock_actual", fallback="ACT"), tipo_comprometido=cfg.get("profit", "stock_comprometido", fallback="COM"))


# ====================================================================== Contax (HTTPS, solo salida)
def llamar(cfg, ruta, datos: dict, comprimir=False, timeout=120) -> dict:
    cuerpo = json.dumps(datos, ensure_ascii=False, default=str).encode()
    cab = {"Content-Type": "application/json", "User-Agent": f"contax-profit/{VERSION}", "X-Contax-Conector": cfg.get("contax", "conector").strip()}
    if comprimir: cuerpo, cab["Content-Encoding"] = gzip.compress(cuerpo, 6), "gzip"
    fecha = int(time.time())
    cab["X-Contax-Fecha"], cab["X-Contax-Firma"] = str(fecha), firmar(cfg.get("contax", "clave").strip(), fecha, cuerpo)
    req = urllib.request.Request(cfg.get("contax", "url").rstrip("/") + ruta, cuerpo, cab, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (URL https del .ini)
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        motivo = {401: "el ID o la clave del conector no son válidos (descargue de nuevo contax_profit.ini)", 402: "la licencia de Contax no incluye el bot",
                  503: "la integración está pausada en la plataforma"}.get(e.code)
        try: motivo = motivo or json.loads(e.read() or b"{}").get("error")
        except ValueError: pass
        raise ErrorConector(f"Contax respondió {e.code}: {motivo or 'error'}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ErrorConector(f"No se pudo contactar a Contax ({getattr(e, 'reason', e)})") from e


def ping(cfg, error=None) -> dict:
    return llamar(cfg, "/api/profit/v1/ping", {"version": VERSION, "equipo": platform.node()[:60], **({"error": str(error)[:400]} if error else {})}, timeout=30)


def sincronizar(cfg, forzar=False) -> bool:
    estado = leer_estado()
    if not forzar and not toca_sincronizar(estado, time.time()):
        log.debug("Aún no toca sincronizar"); return False
    r = ping(cfg)
    config = r.get("config") or {}
    estado["config"] = config
    if not config.get("activo", True):
        log.info("El conector está pausado en Contax"); estado["ultima_sync"] = time.time(); guardar_estado(estado); return False
    try:
        foto = leer_profit(cfg, config)
    except ErrorConector as e:
        try: ping(cfg, error=e)                          # que la empresa lo vea en Contax
        except ErrorConector: pass
        raise
    r = llamar(cfg, "/api/profit/v1/sync", foto, comprimir=True)
    estado.update(config=r.get("config") or config, ultima_sync=time.time(), ultimo_resultado=f"{r.get('articulos')} artículos, {r.get('clientes')} clientes")
    guardar_estado(estado)
    log.info("Sincronizado: %s", estado["ultimo_resultado"])
    return True


# ====================================================================== tarea programada de Windows
def _comando():
    return f'"{sys.executable}"' + ("" if getattr(sys, "frozen", False) else f' "{Path(__file__).resolve()}"')


def instalar_tarea():
    if os.name != "nt": raise ErrorConector("La tarea programada es para Windows. En Linux use cron: */5 * * * * python3 contax_profit.py --una-vez")
    subprocess.run(["schtasks", "/Create", "/F", "/TN", "Contax Profit Plus", "/SC", "MINUTE", "/MO", "5", "/RU", "SYSTEM",
                    "/TR", f"{_comando()} --una-vez"], check=True)
    print("Tarea «Contax Profit Plus» creada: revisa cada 5 minutos y sincroniza según el intervalo configurado en Contax.")


def quitar_tarea():
    subprocess.run(["schtasks", "/Delete", "/F", "/TN", "Contax Profit Plus"], check=True)


# ====================================================================== principal
def configurar_log():
    log.setLevel(logging.INFO)
    h = logging.handlers.RotatingFileHandler(CARPETA / "contax_profit.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s")); log.addHandler(h)
    s = logging.StreamHandler(); s.setFormatter(logging.Formatter("%(message)s")); log.addHandler(s)


def probar(cfg):
    print(f"Conector Contax ↔ Profit Plus {VERSION}")
    r = ping(cfg)
    print("✔ Contax: conexión y clave correctas. Configuración:", json.dumps(r.get("config"), ensure_ascii=False))
    foto = leer_profit(cfg, r.get("config") or {})
    print(f"✔ SQL Server: {len(foto['articulos'])} artículos, {sum(1 for a in foto['articulos'] if Decimal(a['existencia']) > 0)} con existencia, "
          f"{sum(1 for a in foto['articulos'] if Decimal(a['precio']) > 0)} con precio, {len(foto['clientes'])} clientes")
    print("  Almacenes:", ", ".join(f"{a['codigo']} {a['nombre']}" for a in foto["disponibles"]["almacenes"]) or "—")
    print("  Listas de precio:", ", ".join(f"{a['codigo']} {a['nombre']}" for a in foto["disponibles"]["listas_precio"]) or "—")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Conector Contax ↔ Profit Plus (solo lectura)")
    g = ap.add_mutually_exclusive_group()
    for op in ("--probar", "--una-vez", "--forzar", "--instalar-tarea", "--quitar-tarea"): g.add_argument(op, action="store_true")
    a = ap.parse_args(argv)
    configurar_log()
    try:
        if a.instalar_tarea: return instalar_tarea() or 0
        if a.quitar_tarea: return quitar_tarea() or 0
        cfg = leer_ini()
        if a.probar: return probar(cfg) or 0
        if a.una_vez or a.forzar: sincronizar(cfg, forzar=a.forzar); return 0
        while True:                                     # sin opciones: modo continuo (p. ej. como servicio con NSSM)
            try: sincronizar(cfg)
            except ErrorConector as e: log.error("%s", e)
            time.sleep(60)
    except ErrorConector as e:
        log.error("%s", e); return 2
    except subprocess.CalledProcessError as e:
        log.error("No se pudo crear/quitar la tarea (ejecute como Administrador): %s", e); return 3


if __name__ == "__main__":
    sys.exit(main())
