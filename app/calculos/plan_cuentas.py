"""Importación de un plan de cuentas que el cliente YA tiene (CSV, TXT, Excel .xlsx o texto pegado).

Acepta las exportaciones típicas de otros sistemas: con o sin encabezado, separado por «;», «,», tabulador o «|».
Columnas reconocidas por nombre (en cualquier orden): código, nombre/descripción, tipo/naturaleza, categoría NIIF 18,
monetaria. Sin encabezado se asume: código, nombre[, tipo]. Si falta el tipo se deduce del texto o del primer dígito del
código (1 Activo, 2 Pasivo, 3 Patrimonio, 4 Ingreso, 5-9 Gasto/Costo), que es la convención VEN-NIF habitual.
Sin dependencias de Flask: se prueba de forma aislada."""
import csv
import io
import re
import unicodedata

TIPOS = ("Activo", "Pasivo", "Patrimonio", "Ingreso", "Gasto")
CATEGORIAS = ("Operacion", "Inversion", "Financiamiento", "Impuestos", "Discontinuadas")
RE_CODIGO = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.\-_]{0,39}$")
MAX_FILAS = 5000


class ErrorImportacion(ValueError):
    pass


def _norm(t) -> str:
    t = unicodedata.normalize("NFKD", str(t or "")).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", t).strip().lower()


_ALIAS = {
    "codigo": ("codigo", "cod", "cuenta", "code", "nro cuenta", "numero de cuenta", "n cuenta", "no cuenta", "codigo cuenta", "cta"),
    "nombre": ("nombre", "descripcion", "denominacion", "name", "nombre de la cuenta", "nombre cuenta", "detalle", "titulo"),
    "tipo": ("tipo", "naturaleza", "clase", "grupo", "tipo de cuenta", "type"),
    "categoria": ("categoria", "categoria niif 18", "niif 18", "niif18", "categoria niif"),
    "monetaria": ("monetaria", "partida monetaria", "es monetaria", "monetary"),
}


def tipo_de(texto, codigo) -> str | None:
    t = _norm(texto)
    for clave, tipo in (("activo", "Activo"), ("pasivo", "Pasivo"), ("patrimonio", "Patrimonio"), ("capital", "Patrimonio"),
                        ("ingreso", "Ingreso"), ("venta", "Ingreso"), ("gasto", "Gasto"), ("costo", "Gasto"), ("egreso", "Gasto")):
        if t.startswith(clave): return tipo
    if t in ("a", "1"): return "Activo"
    if t in ("p", "2"): return "Pasivo"
    d = (str(codigo).strip() or " ")[0]
    return {"1": "Activo", "2": "Pasivo", "3": "Patrimonio", "4": "Ingreso"}.get(d, "Gasto" if d in "56789" else None)


def _monetaria(valor, nombre) -> bool:
    v = _norm(valor)
    if v in ("si", "s", "1", "true", "x", "yes"): return True
    if v in ("no", "n", "0", "false"): return False
    n = _norm(nombre)
    return any(k in n for k in ("caja", "banco", "por cobrar", "por pagar", "efectivo", "efectos", "prestamo", "documentos por"))


def _categoria(valor, nombre, tipo) -> str:
    v = _norm(valor)
    for c in CATEGORIAS:
        if v and _norm(c) == v: return c
    if tipo in ("Ingreso", "Gasto"):
        n = _norm(nombre)
        if "impuesto sobre la renta" in n or n.startswith("islr"): return "Impuestos"
        if any(k in n for k in ("interes", "financier", "comisiones bancarias", "diferencia en cambio", "diferencial cambiario")): return "Financiamiento"
    return "Operacion"


def leer_filas(contenido: bytes | str, nombre_archivo: str = "") -> list[list[str]]:
    """Devuelve las filas crudas del archivo (xlsx o texto delimitado)."""
    if isinstance(contenido, bytes) and (nombre_archivo.lower().endswith(".xlsx") or contenido[:2] == b"PK"):
        try:
            import openpyxl
        except ImportError as e:  # pragma: no cover
            raise ErrorImportacion("Para importar Excel instale openpyxl, o guarde el archivo como CSV") from e
        try:
            wb = openpyxl.load_workbook(io.BytesIO(contenido), read_only=True, data_only=True)
        except Exception as e:  # noqa: BLE001 — archivo dañado o no es Excel
            raise ErrorImportacion("No se pudo leer el archivo Excel") from e
        hoja = wb.worksheets[0]
        filas = [["" if c is None else (str(int(c)) if isinstance(c, float) and c.is_integer() else str(c)) for c in fila]
                 for fila in hoja.iter_rows(values_only=True)]
        wb.close()
        return filas
    if isinstance(contenido, bytes):
        for enc in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                contenido = contenido.decode(enc); break
            except UnicodeDecodeError:
                continue
    texto = contenido.replace("\r\n", "\n").replace("\r", "\n")
    muestra = "\n".join(texto.split("\n")[:20])
    delim = max((";", ",", "\t", "|"), key=lambda d: muestra.count(d))
    if not any(d in muestra for d in (";", ",", "\t", "|")):  # «1.1.01 Caja» sin delimitador: código y nombre por el primer espacio
        return [ln.strip().split(None, 1) for ln in texto.split("\n") if ln.strip()]
    return list(csv.reader(io.StringIO(texto), delimiter=delim))


def interpretar(filas: list[list[str]]) -> tuple[list[dict], list[str]]:
    """Filas crudas → (cuentas válidas, errores por línea). No toca la base de datos."""
    filas = [[str(c).strip() for c in f] for f in filas if any(str(c).strip() for c in f)]
    if not filas: raise ErrorImportacion("El archivo está vacío")
    if len(filas) > MAX_FILAS: raise ErrorImportacion(f"Máximo {MAX_FILAS} cuentas por importación")
    cab = [_norm(c) for c in filas[0]]
    idx = {}
    for campo, alias in _ALIAS.items():
        for i, c in enumerate(cab):
            if c in alias and i not in idx.values(): idx[campo] = i; break
    con_cabecera = "codigo" in idx and "nombre" in idx
    if con_cabecera:
        filas = filas[1:]
    else:
        idx = {"codigo": 0, "nombre": 1, "tipo": 2}
    cuentas, errores, vistos = [], [], set()
    for n, f in enumerate(filas, start=2 if con_cabecera else 1):
        g = lambda k: f[idx[k]] if k in idx and idx[k] < len(f) else ""
        cod, nom = g("codigo").strip(), re.sub(r"\s+", " ", g("nombre")).strip()
        if not cod and not nom: continue
        if not RE_CODIGO.match(cod): errores.append(f"Línea {n}: código inválido «{cod[:30]}»"); continue
        if not nom: errores.append(f"Línea {n}: la cuenta {cod} no tiene nombre"); continue
        if cod in vistos: errores.append(f"Línea {n}: código {cod} repetido en el archivo"); continue
        tipo = tipo_de(g("tipo"), cod) if g("tipo") else tipo_de("", cod)
        if tipo is None: errores.append(f"Línea {n}: no se pudo deducir el tipo de la cuenta {cod}; agregue la columna «tipo»"); continue
        vistos.add(cod)
        cuentas.append(dict(codigo=cod, nombre=nom[:200], tipo=tipo, categoria_niif18=_categoria(g("categoria"), nom, tipo),
                            es_partida_monetaria=_monetaria(g("monetaria"), nom)))
    return cuentas, errores


PLANTILLA_CSV = ("codigo;nombre;tipo;categoria_niif18;monetaria\n"
                 "1;ACTIVO;Activo;Operacion;No\n1.1.01;Caja principal;Activo;Operacion;Si\n2.1.01;Proveedores nacionales;Pasivo;Operacion;Si\n"
                 "4.1.01;Ventas de mercancía;Ingreso;Operacion;No\n5.2.01;Intereses bancarios;Gasto;Financiamiento;No\n")

# Plan de cuentas modelo VEN-NIF (PYME) para quien no tiene uno: estructura de 3 niveles, editable luego.
MODELO_VEN_NIF = """1;ACTIVO
1.1;ACTIVO CORRIENTE
1.1.01;Caja
1.1.02;Bancos nacionales
1.1.03;Bancos en moneda extranjera
1.1.04;Cuentas por cobrar comerciales
1.1.05;Estimación para cuentas incobrables
1.1.06;Otras cuentas por cobrar
1.1.07;Inventario de mercancías
1.1.08;IVA crédito fiscal
1.1.09;IVA retenido por clientes
1.1.10;Anticipos de ISLR y retenciones
1.1.11;Gastos pagados por anticipado
1.2;ACTIVO NO CORRIENTE
1.2.01;Mobiliario y equipos
1.2.02;Vehículos
1.2.03;Equipos de computación
1.2.04;Depreciación acumulada
1.2.05;Activos intangibles (software)
2;PASIVO
2.1;PASIVO CORRIENTE
2.1.01;Cuentas por pagar a proveedores
2.1.02;IVA débito fiscal
2.1.03;Retenciones de IVA por enterar
2.1.04;Retenciones de ISLR por enterar
2.1.05;IGTF por enterar
2.1.06;Sueldos y prestaciones por pagar
2.1.07;Aportes parafiscales por pagar (IVSS, FAOV, INCES)
2.1.08;ISLR por pagar
2.1.09;Préstamos bancarios a corto plazo
2.2;PASIVO NO CORRIENTE
2.2.01;Préstamos bancarios a largo plazo
2.2.02;Provisión para prestaciones sociales
3;PATRIMONIO
3.1.01;Capital social
3.1.02;Reserva legal
3.1.03;Resultados acumulados
3.1.04;Resultado del ejercicio
4;INGRESOS
4.1.01;Ventas de mercancía
4.1.02;Ingresos por servicios
4.1.03;Devoluciones y descuentos en ventas
4.2.01;Otros ingresos
4.2.02;Ganancia en diferencia en cambio
5;COSTOS
5.1.01;Costo de ventas
6;GASTOS
6.1.01;Sueldos y salarios
6.1.02;Aportes patronales
6.1.03;Alquileres
6.1.04;Servicios públicos
6.1.05;Honorarios profesionales
6.1.06;Publicidad y mercadeo
6.1.07;Depreciación
6.1.08;Gasto por cuentas incobrables
6.1.09;Impuestos municipales (actividades económicas)
6.2.01;Comisiones bancarias
6.2.02;Intereses bancarios
6.2.03;Pérdida en diferencia en cambio
6.2.04;IGTF pagado
6.3.01;Impuesto sobre la renta"""
