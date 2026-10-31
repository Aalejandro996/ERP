"""Migración de datos desde otros sistemas venezolanos (Galac, Profit Plus) o cualquier Excel/CSV. Lógica PURA.

«Inteligente» significa:
  * busca la fila de encabezados aunque el reporte traiga títulos arriba (primeras 15 filas);
  * reconoce las columnas por su nombre en Profit Plus (co_cli, cli_des, co_art, art_des, stock_act, prec_vta1…), en Galac
    («Código del Cliente», «Nombre del Proveedor», «Número de RIF»…) o genéricas, en cualquier orden;
  * deduce QUÉ contiene cada hoja (clientes, proveedores, productos, plan de cuentas, cuentas por cobrar o por pagar) y de qué
    sistema viene; el nombre de la hoja o del archivo desempata (p. ej. «Clientes.xlsx»);
  * normaliza RIF/cédula, montos en formato venezolano (1.234,56), fechas dd/mm/aaaa y alícuotas de IVA;
  * marca cada fila como válida o con su error, para que el cliente VALIDE antes de cargar."""
import csv
import io
import re
import unicodedata
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from . import plan_cuentas as PC

TIPOS = {"clientes": "Clientes", "proveedores": "Proveedores", "productos": "Productos e inventario inicial", "plan_cuentas": "Plan de cuentas",
         "cxc": "Cuentas por cobrar (saldos)", "cxp": "Cuentas por pagar (saldos)"}
SISTEMAS = ("Galac", "Profit Plus", "Excel / otro")


def n(t) -> str:
    t = unicodedata.normalize("NFKD", str(t or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[\s_.:#°º/]+", " ", t).strip().lower()


_TERCERO = {
    "codigo": ("codigo", "cod", "co cli", "co prov", "codigo del cliente", "codigo del proveedor", "codigo cliente", "codigo proveedor", "cliente id", "id"),
    "rif": ("rif", "numero de rif", "nro rif", "rif ci", "rif cedula", "cedula rif", "ci rif", "cedula", "c i", "rif o ci", "n rif", "documento de identidad"),
    "nombre": ("nombre", "razon social", "cli des", "prov des", "nombre del cliente", "nombre del proveedor", "nombre o razon social", "descripcion", "cliente", "proveedor", "denominacion"),
    "direccion": ("direccion", "direc1", "direccion fiscal", "domicilio fiscal", "domicilio", "direccion 1"),
    "telefono": ("telefono", "telefonos", "tlf", "telf", "celular", "movil", "telefono 1"),
    "email": ("email", "e mail", "correo", "correo electronico", "mail"),
}
CAMPOS = {
    "clientes": _TERCERO,
    "proveedores": _TERCERO,
    "productos": {
        "codigo": ("codigo", "co art", "codigo del articulo", "codigo articulo", "cod articulo", "articulo", "sku", "referencia", "codigo producto", "codigo del producto"),
        "nombre": ("descripcion", "art des", "nombre", "descripcion del articulo", "producto", "nombre del producto", "articulo descripcion"),
        "existencia": ("existencia", "stock act", "stock", "cantidad", "existencia actual", "inventario", "disponible", "saldo actual"),
        "costo": ("costo", "cos pro un", "costo promedio", "costo unitario", "ult cos un", "costo ultimo", "costo prom"),
        "precio": ("precio", "prec vta1", "precio de venta", "precio venta", "precio sin iva", "precio 1", "pvp", "precio unitario"),
        "alicuota": ("alicuota", "tipo imp", "tipo de iva", "iva", "alicuota iva", "impuesto", "tasa iva"),
    },
    "cxc": {
        "numero": ("numero", "nro doc", "documento", "numero de documento", "factura", "nro factura", "numero factura", "n documento", "num doc"),
        "rif": _TERCERO["rif"], "nombre": ("cliente", "cli des", "nombre del cliente", "razon social", "nombre", "deudor"),
        "emision": ("fecha", "fec emis", "fecha de emision", "emision", "fecha emision", "fecha documento"),
        "vencimiento": ("vencimiento", "fec venc", "fecha de vencimiento", "fecha vencimiento", "vence"),
        "monto": ("monto", "monto net", "total", "monto total", "importe", "monto documento"),
        "saldo": ("saldo", "saldo pendiente", "pendiente", "por cobrar", "saldo actual", "deuda"),
    },
}
CAMPOS["cxp"] = dict(CAMPOS["cxc"], nombre=("proveedor", "prov des", "nombre del proveedor", "razon social", "nombre", "acreedor"),
                     saldo=("saldo", "saldo pendiente", "pendiente", "por pagar", "saldo actual", "deuda"),
                     control=("numero de control", "nro control", "control", "n control"))
CAMPOS["plan_cuentas"] = {k: PC._ALIAS[k] for k in ("codigo", "nombre", "tipo")}
OBLIGATORIOS = {"clientes": ("rif", "nombre"), "proveedores": ("rif", "nombre"), "productos": ("codigo", "nombre"),
                "plan_cuentas": ("codigo", "nombre"), "cxc": ("numero", "nombre"), "cxp": ("numero", "nombre")}
PISTAS = {"clientes": ("cliente", "cli"), "proveedores": ("proveedor", "prov"), "productos": ("art", "producto", "inventario", "stock"),
          "plan_cuentas": ("cuenta", "plan", "catalogo"), "cxc": ("cobrar", "cxc"), "cxp": ("pagar", "cxp")}
PROFIT = {"co cli", "cli des", "co prov", "prov des", "co art", "art des", "stock act", "prec vta1", "cos pro un", "direc1", "fec emis", "fec venc", "nro doc", "monto net", "tipo imp"}


# ------------------------------------------------------------------ lectura
def leer_hojas(contenido: bytes, nombre_archivo: str) -> list[tuple[str, list[list[str]]]]:
    """[(nombre de hoja, filas)]. Excel: todas las hojas con datos. CSV/TXT: una «hoja» con el nombre del archivo."""
    if nombre_archivo.lower().endswith((".xlsx", ".xlsm")) or contenido[:2] == b"PK":
        try:
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(contenido), read_only=True, data_only=True)
        except Exception as e:  # noqa: BLE001
            raise PC.ErrorImportacion("No se pudo leer el archivo Excel (¿está dañado o protegido con clave?)") from e
        out = []
        for ws in wb.worksheets:
            filas = [[_celda(c) for c in fila] for fila in ws.iter_rows(values_only=True)]
            filas = [f for f in filas if any(x.strip() for x in f)]
            if filas: out.append((ws.title, filas[:PC.MAX_FILAS + 20]))
        wb.close()
        return out
    if nombre_archivo.lower().endswith(".xls"):
        raise PC.ErrorImportacion("Guarde el archivo de Excel 97-2003 (.xls) como .xlsx o CSV e intente de nuevo")
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            texto = contenido.decode(enc); break
        except UnicodeDecodeError:
            continue
    texto = texto.replace("\r\n", "\n").replace("\r", "\n")
    muestra = "\n".join(texto.split("\n")[:30])
    delim = max((";", ",", "\t", "|"), key=muestra.count)
    filas = [f for f in csv.reader(io.StringIO(texto), delimiter=delim) if any(x.strip() for x in f)]
    return [(re.sub(r"\.\w+$", "", nombre_archivo), filas[:PC.MAX_FILAS + 20])] if filas else []


def _celda(c) -> str:
    if c is None: return ""
    if isinstance(c, datetime): return c.date().isoformat()
    if isinstance(c, date): return c.isoformat()
    if isinstance(c, float) and c.is_integer(): return str(int(c))
    return str(c).strip()


# ------------------------------------------------------------------ detección
def _mapear(cab, campos) -> dict:
    cabn, idx = [n(c) for c in cab], {}
    for campo, alias in campos.items():
        for i, c in enumerate(cabn):
            if i not in idx.values() and (c in alias or any(c == a for a in alias)):
                idx[campo] = i; break
    return idx


def detectar(filas, pista="") -> dict:
    """Encuentra fila de encabezados, tipo de datos y sistema de origen. Devuelve {fila_cab, tipo, sistema, columnas, puntaje}."""
    pista = n(pista)
    mejor = None
    for r, fila in enumerate(filas[:15]):
        for tipo, campos in CAMPOS.items():
            idx = _mapear(fila, campos)
            if not all(k in idx for k in OBLIGATORIOS[tipo]): continue
            puntos = len(idx) * 10 + sum(5 for p in PISTAS[tipo] if p in pista)
            cabn = " ".join(n(c) for c in fila)
            puntos += sum(3 for p in PISTAS[tipo] if re.search(rf"\b{p}", cabn))
            if tipo == "plan_cuentas" and "tipo" not in idx and not any(p in pista for p in PISTAS["plan_cuentas"]): puntos -= 15
            if tipo in ("cxc", "cxp") and not ({"monto", "saldo"} & set(idx)): continue
            if mejor is None or puntos > mejor["puntaje"]:
                mejor = dict(fila_cab=r, tipo=tipo, columnas=idx, puntaje=puntos)
    if mejor is None: return dict(fila_cab=None, tipo=None, sistema="Excel / otro", columnas={}, puntaje=0)
    cab = {n(c) for c in filas[mejor["fila_cab"]]}
    mejor["sistema"] = "Profit Plus" if cab & PROFIT else ("Galac" if any(c.startswith(("codigo del", "nombre del", "numero de rif")) for c in cab) else "Excel / otro")
    return mejor


# ------------------------------------------------------------------ normalización
def rif(valor) -> str:
    """RIF (J-12345678-9) o cédula (V-12345678). Acepta «J123456789», «j-12345678-9», «12345678» (cédula venezolana)."""
    v = re.sub(r"[\s.\-]", "", str(valor or "")).upper()
    if re.fullmatch(r"[VEJPGC]\d{9}", v): return f"{v[0]}-{v[1:9]}-{v[9]}"
    if re.fullmatch(r"[VE]?\d{6,8}", v): return f"{v[0] if v[0] in 'VE' else 'V'}-{v.lstrip('VE')}"
    raise ValueError(f"RIF/cédula inválido «{str(valor)[:20]}»")


def numero(valor, defecto=None):
    t = str(valor or "").strip().replace(" ", "").replace("Bs", "").replace("$", "")
    if not t: return defecto
    if "," in t and "." in t: t = t.replace(".", "").replace(",", ".") if t.rfind(",") > t.rfind(".") else t.replace(",", "")
    elif "," in t: t = t.replace(",", ".")
    try:
        d = Decimal(t)
    except InvalidOperation as e:
        raise ValueError(f"Número inválido «{valor}»") from e
    if not d.is_finite(): raise ValueError(f"Número inválido «{valor}»")
    return d


def fecha(valor, defecto=None):
    t = str(valor or "").strip()[:19]
    if not t: return defecto
    if re.fullmatch(r"\d{5}", t): return date(1899, 12, 30) + timedelta(days=int(t))        # número de serie de Excel
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%Y-%m-%d %H:%M:%S", "%d.%m.%Y"):
        try:
            return datetime.strptime(t, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Fecha inválida «{t}» (use dd/mm/aaaa)")


def alicuota(valor) -> str:
    v = n(valor)
    if v in ("", "g", "general", "16", "16 00", "1", "gravado"): return "G"
    if v in ("r", "reducida", "8", "8 00", "2"): return "R"
    if v in ("a", "lujo", "adicional", "31", "15", "3"): return "A"
    if v in ("e", "exento", "exenta", "0", "0 00", "no gravado", "4"): return "E"
    raise ValueError(f"Alícuota de IVA desconocida «{valor}»")


def normalizar(tipo, filas, fila_cab, columnas) -> list[dict]:
    """[{datos, estado: ok|error, obs}] — una entrada por fila de datos, en el orden del archivo."""
    if tipo == "plan_cuentas":
        cs, errores = PC.interpretar(filas[fila_cab:])
        return [dict(datos={**c, "es_partida_monetaria": c["es_partida_monetaria"]}, estado="ok", obs="") for c in cs] + \
               [dict(datos={}, estado="error", obs=e) for e in errores]
    out, vistos = [], set()
    clave = {"clientes": "rif", "proveedores": "rif", "productos": "codigo", "cxc": "numero", "cxp": "numero"}[tipo]
    for k, f in enumerate(filas[fila_cab + 1:], start=fila_cab + 2):
        g = lambda c: (f[columnas[c]] if c in columnas and columnas[c] < len(f) else "").strip()
        if not any(x.strip() for x in f): continue
        d, obs = {}, []
        try:
            if tipo in ("clientes", "proveedores"):
                d = dict(rif=rif(g("rif")), nombre=g("nombre")[:200], direccion=g("direccion")[:300] or None, telefono=g("telefono")[:60] or None,
                         email=(g("email")[:120] or None), codigo=g("codigo")[:40] or None)
                if not d["nombre"]: raise ValueError("Falta el nombre o razón social")
                if d["email"] and "@" not in d["email"]: obs.append("correo ignorado (inválido)"); d["email"] = None
            elif tipo == "productos":
                d = dict(codigo=g("codigo")[:40], nombre=g("nombre")[:200], existencia=str(numero(g("existencia"), Decimal(0))),
                         costo=str(numero(g("costo"), Decimal(0))), precio=str(numero(g("precio"), Decimal(0))), alicuota=alicuota(g("alicuota")))
                if not d["codigo"] or not d["nombre"]: raise ValueError("Falta el código o la descripción")
                if Decimal(d["existencia"]) < 0: raise ValueError("Existencia negativa")
                if Decimal(d["existencia"]) > 0 and Decimal(d["costo"]) <= 0: obs.append("sin costo: la existencia entra a costo 0")
            else:
                monto, saldo = numero(g("monto")), numero(g("saldo"))
                monto, saldo = monto if monto is not None else saldo, saldo if saldo is not None else monto
                em = fecha(g("emision"), date.today())
                d = dict(numero=g("numero")[:40], nombre=g("nombre")[:200], rif=rif(g("rif")) if g("rif") else None, emision=em.isoformat(),
                         vencimiento=fecha(g("vencimiento"), em).isoformat(), monto=str(monto), saldo=str(saldo))
                if tipo == "cxp": d["control"] = g("control")[:30] or None
                if not d["numero"] or not d["nombre"]: raise ValueError("Falta el número de documento o el tercero")
                if monto is None or monto <= 0: raise ValueError("Monto inválido")
                if saldo < 0 or saldo > monto: raise ValueError("El saldo debe estar entre 0 y el monto")
                if saldo == 0: raise ValueError("Documento sin saldo pendiente (no se migra)")
            if d[clave] in vistos: raise ValueError(f"Repetido en el archivo ({d[clave]})")
            vistos.add(d[clave])
            out.append(dict(fila=k, datos=d, estado="ok", obs="; ".join(obs)))
        except ValueError as e:
            out.append(dict(fila=k, datos={c: g(c) for c in columnas}, estado="error", obs=str(e)))
    return out
