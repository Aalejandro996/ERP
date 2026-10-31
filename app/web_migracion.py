"""Empresa → Migrar datos de otro sistema (Galac, Profit Plus, Excel). Subir → revisar y validar → confirmar."""
from flask import flash, g, redirect, request, url_for
from sqlalchemy import select

from . import servicios_migracion as SM
from .calculos import migracion as MG
from .calculos.plan_cuentas import ErrorImportacion
from .extensions import db
from .models_compras import Migracion
from .web import F, campo, intentar, pagina, ruta

h = lambda v: campo("accion", "", "hidden", valor=v)
COLUMNAS_VISTA = {"clientes": ("rif", "nombre", "direccion", "telefono", "email"), "proveedores": ("rif", "nombre", "direccion", "telefono", "email"),
                  "productos": ("codigo", "nombre", "existencia", "costo", "precio", "alicuota"),
                  "plan_cuentas": ("codigo", "nombre", "tipo", "categoria_niif18"),
                  "cxc": ("numero", "nombre", "rif", "emision", "vencimiento", "monto", "saldo"), "cxp": ("numero", "nombre", "rif", "emision", "vencimiento", "monto", "saldo")}
ESTADO_TXT = {"ok": "✔ Nuevo", "existe": "↺ Ya existe", "error": "✘ Error"}


@ruta("/e/<int:eid>/migracion", "admin", ("GET", "POST"))
def migracion(eid):
    if request.method == "POST":
        arch = request.files.get("archivo")
        res = {}
        def subir():
            if arch is None or not arch.filename: raise ValueError("Elija el archivo exportado de su sistema anterior")
            tipo = F.get("tipo") or None
            if tipo and tipo not in MG.TIPOS: raise ValueError("Tipo inválido")
            sistema = F.get("sistema") if F.get("sistema") in MG.SISTEMAS else None
            res["ms"] = SM.preparar(eid, g.usuario, arch.read(4 * 1024 * 1024), arch.filename, tipo, sistema)
            return res["ms"]
        try:
            ok = intentar(subir, lambda ms: f"Archivo leído: {len(ms)} hoja(s) reconocida(s). Revise y valide antes de cargar.")
        except ErrorImportacion as e:
            ok = False; flash(str(e))
        if ok: return redirect(url_for("web.migracion_ver", eid=eid, mid=res["ms"][0].id))
        return redirect(request.url)
    ms = db.session.execute(select(Migracion).where(Migracion.empresa_id == eid).order_by(Migracion.id.desc()).limit(100)).scalars().all()
    return pagina("Migrar datos de otro sistema", eid, [
        dict(titulo="1 · Suba lo que exportó de su sistema anterior", abierto=True,
             nota="Galac, Profit Plus, Saint, Valery o cualquier Excel/CSV: clientes, proveedores, artículos con existencia y costo, plan de cuentas y "
                  "documentos pendientes por cobrar o por pagar. Un Excel con varias hojas se lee completo. El sistema reconoce las columnas, detecta "
                  "qué contiene cada hoja y de qué sistema viene, y NO carga nada hasta que usted lo valide.",
             form=dict(action=request.path, boton="Leer y revisar", archivo=True, campos=[
                 campo("archivo", "Archivo (.xlsx, .csv, .txt)", "file", accept=".xlsx,.xlsm,.csv,.txt"),
                 campo("sistema", "Sistema de origen", "select", req=False, opciones=[("", "Detectar automáticamente")] + [(x, x) for x in MG.SISTEMAS]),
                 campo("tipo", "Contenido", "select", req=False, opciones=[("", "Detectar automáticamente")] + list(MG.TIPOS.items()))])),
        dict(titulo="Cómo exportar", nota="Profit Plus: Reportes → Clientes / Proveedores / Artículos (stock) / Cuentas por cobrar, «Exportar a Excel». "
             "Galac: Menú Informes → seleccione el listado → botón Exportar (Excel). Saldos: exporte solo los documentos con saldo pendiente. "
             "Los saldos iniciales migrados no entran a los libros de compras/ventas del mes (ya están en su sistema anterior) y se llevan a la "
             "contabilidad con un asiento de apertura desde Contabilidad → Cuadre de libros."),
        dict(titulo="Migraciones", tabla=dict(cols=["#", "Fecha", "Archivo", "Hoja", "Sistema", "Contenido", "Estado", "Resultado"], filas=[
            dict(cells=[m.id, m.creado_en.strftime("%Y-%m-%d %H:%M"), m.archivo, m.hoja or "", m.sistema, MG.TIPOS.get(m.tipo, m.tipo), m.estado, m.resultado or ""],
                 url=url_for("web.migracion_ver", eid=eid, mid=m.id)) for m in ms]))])


@ruta("/e/<int:eid>/migracion/<int:mid>", "admin", ("GET", "POST"))
def migracion_ver(eid, mid):
    m = db.session.execute(select(Migracion).where(Migracion.id == mid, Migracion.empresa_id == eid)).scalar_one_or_none()
    if m is None: return redirect(url_for("web.migracion", eid=eid))
    if request.method == "POST":
        a = F.get("accion")
        if a == "aplicar":
            excluir = {int(x) for x in F.getlist("excluir") if x.isdigit()}
            intentar(lambda: SM.aplicar(eid, g.usuario, m, F.get("modo") == "actualizar", excluir), lambda r: "Migración aplicada: " + r)
        elif a == "descartar":
            def descartar():
                if m.estado != "Borrador": raise ValueError("Solo se descartan borradores")
                m.estado = "Descartada"; db.session.commit()
            intentar(descartar, "Migración descartada")
        return redirect(request.url)
    r = SM.resumen(m)
    cols = COLUMNAS_VISTA[m.tipo]
    pend = [m2 for m2 in db.session.execute(select(Migracion).where(Migracion.empresa_id == eid, Migracion.estado == "Borrador", Migracion.id != m.id)).scalars()]
    secs = [dict(kpis=[("Filas leídas", len(m.filas["filas"])), ("Nuevas (se cargarán)", r["ok"]), ("Ya existen", r["existe"]), ("Con error", r["error"])]),
            dict(pares=[("Archivo", f"{m.archivo} · hoja «{m.hoja}»"), ("Sistema detectado", m.sistema), ("Contenido detectado", MG.TIPOS[m.tipo]),
                        ("Columnas reconocidas", ", ".join(f"{k} ← «{v}»" for k, v in m.filas.get("columnas", {}).items())), ("Estado", m.estado)]),
            dict(titulo="Revise fila por fila", nota="✔ se carga · ↺ ya existe en la empresa (se omite o se actualiza según elija) · ✘ no se carga (corrija el archivo y súbalo de nuevo).",
                 tabla=dict(cols=["Fila", "Estado", *[c.capitalize() for c in cols], "Observación"], ancha=True, filas=[
                     dict(cells=[f.get("fila", ""), ESTADO_TXT[f["estado"]], *[f["datos"].get(c, "") if f["datos"].get(c) is not None else "" for c in cols], f.get("obs", "")],
                          clase="anulado" if f["estado"] == "error" else "") for f in m.filas["filas"]]))]
    if m.estado == "Borrador":
        secs += [dict(titulo="2 · Validar y cargar", abierto=True, nota=f"Se cargarán {r['ok']} registro(s) nuevo(s). Si encontró una fila que no quiere cargar, indique su número.",
                      form=dict(action=request.path, boton="Confirmar y cargar", confirmar="¿Cargar los datos validados en esta empresa?", campos=[h("aplicar"),
                                campo("modo", "Registros que ya existen", "select", opciones=[("omitir", "Omitirlos"), ("actualizar", "Actualizar sus datos (nombre, dirección, precio…)")]),
                                campo("excluir", "Filas a excluir (opcional)", "checks", req=False, opciones=[(str(f["fila"]), str(f["fila"])) for f in m.filas["filas"] if f["estado"] != "error" and f.get("fila")][:200])])),
                 dict(titulo="Descartar", form=dict(action=request.path, boton="Descartar esta carga", campos=[h("descartar")]))]
    acciones = [dict(url=url_for("web.migracion", eid=eid), texto="← Migraciones")] + \
               [dict(url=url_for("web.migracion_ver", eid=eid, mid=x.id), texto=f"Hoja «{x.hoja}» ({MG.TIPOS[x.tipo]})") for x in pend[:5]]
    return pagina(f"Migración #{m.id} · {MG.TIPOS[m.tipo]}", eid, secs, acciones=acciones)
