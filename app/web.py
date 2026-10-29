"""Interfaz web (Fases 2-4). Cada ruta: autenticación → permiso (RBAC) → empresa autorizada (BD) → lógica → PRG."""
import re
from datetime import date
from decimal import Decimal

from functools import wraps

from flask import Blueprint, current_app, flash, g, redirect, render_template, request, url_for
from werkzeug.local import LocalProxy
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from . import servicios as S
from .calculos import estados
from .calculos.fiscal import ErrorFiscal
from .contabilidad import service as contab
from .contabilidad.validadores import ErrorAsiento
from .extensions import db
from .tiempo import hoy_ve
from .models import Asiento, Cuenta, Empresa, Movimiento, PeriodoCerrado, UsuarioEmpresa
from .models2 import (CUENTAS_CFG, ConfigAsientos, DocumentoCxC, DocumentoCxP, ExtractoBancario, Hallazgo, MovInventario,
                      PapelTrabajo)
from .security import empresa_autorizada, requiere, tiene_permiso
from .auditoria import verificar_cadena

bp = Blueprint("web", __name__)


# ---------------------------------------------------------------- helpers
def campo(n, l, t="text", req=True, **kw):
    return dict(n=n, l=l, t=t, req=req, **kw)


def hoy(): return hoy_ve().isoformat()


POR_PAGINA = 25


def paginar(tabla, clave):
    """Pagina una tabla de pagina.html con el parámetro ?<clave>=N (o =todo para imprimir completa). Las filas de
    total (clase "total") se repiten al pie de cada página. Devuelve la tabla con sus filas visibles y el paginador."""
    filas = list(tabla.get("filas") or [])
    fijas = [f for f in filas if (f.get("clase") or "") == "total"]
    datos = [f for f in filas if (f.get("clase") or "") != "total"]
    if len(datos) <= POR_PAGINA: return {**tabla, "filas": filas}
    paginas = -(-len(datos) // POR_PAGINA)
    pedido = request.args.get(clave, "1")
    args = {k: v for k, v in request.args.items() if k != clave}

    def url(n): return url_for(request.endpoint, **(request.view_args or {}), **args, **{clave: n})
    if pedido == "todo":
        return {**tabla, "filas": filas, "pag": {"texto": f"{len(datos)} registros", "todo": True, "primera": url(1)}}
    actual = min(max(int(pedido) if pedido.isdigit() else 1, 1), paginas)
    ini = (actual - 1) * POR_PAGINA
    numeros = sorted({1, paginas, *range(max(1, actual - 2), min(paginas, actual + 2) + 1)})
    enlaces, previo = [], 0
    for n in numeros:
        if n - previo > 1: enlaces.append({"gap": True})
        enlaces.append({"n": n, "url": url(n), "actual": n == actual}); previo = n
    return {**tabla, "filas": datos[ini:ini + POR_PAGINA] + fijas,
            "pag": {"texto": f"{ini + 1}–{min(ini + POR_PAGINA, len(datos))} de {len(datos)}", "enlaces": enlaces,
                    "ant": url(actual - 1) if actual > 1 else None, "sig": url(actual + 1) if actual < paginas else None, "todas": url("todo")}}


def pagina(titulo, eid, secciones, acciones=None):
    emp = db.session.get(Empresa, eid) if eid else None
    secciones = [({**s, "tabla": paginar(s["tabla"], f"pag{i}" if i else "pag")} if isinstance(s, dict) and s.get("tabla") else s)
                 for i, s in enumerate(secciones)]
    return render_template("pagina.html", titulo=titulo, empresa=emp, secciones=secciones, acciones=acciones or [])


RIF_RE = re.compile(r"^[VEJPGC]-?\d{8}-?\d$")


def validar_rif(valor, obligatorio=True):
    """RIF venezolano: letra (V, E, J, P, G, C) + 8 dígitos + dígito verificador. Se normaliza a J-12345678-9."""
    v = (valor or "").strip().upper().replace(" ", "")
    if not v and not obligatorio: return ""
    if not RIF_RE.match(v): raise ValueError(f"RIF inválido: «{valor}» (formato J-12345678-9)")
    v = v.replace("-", "")
    return f"{v[0]}-{v[1:9]}-{v[9]}"


def intentar(fn, ok):
    """Ejecuta una operación; los errores de negocio se muestran, los de BD nunca filtran detalles técnicos."""
    try:
        res = fn(); flash(ok(res) if callable(ok) else ok); return True
    except (ErrorAsiento, ErrorFiscal, ValueError) as e:
        db.session.rollback(); flash(str(e))
    except SQLAlchemyError as e:
        db.session.rollback(); current_app.logger.warning("Operación rechazada por la BD: %s", str(getattr(e, "orig", e)).splitlines()[0][:300])
        flash("La operación fue rechazada por las reglas de integridad de la base de datos.")
    return False


def mis_empresas():
    q = select(Empresa).where(Empresa.organizacion_id == g.usuario.organizacion_id).order_by(Empresa.razon_social)
    if g.usuario.rol != "Administrador":
        q = q.join(UsuarioEmpresa, UsuarioEmpresa.empresa_id == Empresa.id).where(UsuarioEmpresa.usuario_id == g.usuario.id)
    return db.session.execute(q).scalars().all()


def ruta(patron, permiso, metodos=("GET",)):
    def deco(f):
        @bp.route(patron, methods=list(metodos), endpoint=f.__name__)
        @requiere(permiso)
        @wraps(f)
        def interior(*a, **kw):
            if "eid" in kw: empresa_autorizada(kw["eid"])
            return f(*a, **kw)
        return interior
    return deco


F = LocalProxy(lambda: request.form)  # valores del formulario (se resuelve en cada petición)


@bp.app_context_processor
def ayudantes_plantilla():
    return {"puede": lambda p: g.usuario is not None and tiene_permiso(g.usuario, p),
            "mis_empresas": lambda: mis_empresas() if g.usuario is not None and g.usuario.organizacion_id else [],
            "url_empresa": url_empresa}


# ---------------------------------------------------------------- panel y empresas
def url_empresa(eid):
    """Primera pantalla de la empresa que el plan y el rol permiten."""
    for permiso, endpoint in (("asientos.ver", "web.asientos"), ("facturacion.ver", "web.facturas"), ("cartera.ver", "web.cxc"),
                              ("inventario.ver", "web.inventario"), ("auditoria.ver", "web.auditoria")):
        if tiene_permiso(g.usuario, permiso): return url_for(endpoint, eid=eid)
    return None


def limite_licencia(clave):
    return int((g.licencia or {}).get(clave) or 0)


@bp.route("/", methods=["GET", "POST"])
def inicio():
    if g.usuario is None: return redirect(url_for("auth.login"))
    if g.usuario.rol == "Maestro": return redirect(url_for("web.plataforma"))
    if request.method == "POST":
        if g.usuario.rol != "Administrador": return ("", 403)
        def alta():
            org = g.usuario.organizacion_id
            n = db.session.execute(select(db.func.count()).select_from(Empresa).where(Empresa.organizacion_id == org)).scalar()
            if n >= limite_licencia("max_empresas"): raise ValueError(f"Su licencia permite {limite_licencia('max_empresas')} empresa(s)")
            rif = validar_rif(F["rif"])
            if db.session.execute(select(Empresa.id).where(Empresa.organizacion_id == org, Empresa.rif == rif)).first():
                raise ValueError("Ya existe una empresa con ese RIF en su organización")
            e = Empresa(organizacion_id=org, razon_social=F["razon_social"].strip(), rif=rif, direccion=F.get("direccion"), telefono=F.get("telefono"),
                        es_sujeto_pasivo_especial=F.get("spe") == "1")
            db.session.add(e); db.session.flush()
            for cod, nom, tipo in (("1", "ACTIVO", "Activo"), ("2", "PASIVO", "Pasivo"), ("3", "PATRIMONIO", "Patrimonio"), ("4", "INGRESOS", "Ingreso"), ("5", "GASTOS", "Gasto")):
                db.session.add(Cuenta(empresa_id=e.id, codigo=cod, nombre=nom, tipo=tipo))
            from .auditoria import registrar
            registrar(g.usuario.username, "EMPRESA_CREADA", f"{e.rif} {e.razon_social}"); db.session.commit()
        if intentar(alta, "Empresa creada (con cuentas raíz). Asigne usuarios en Administración → Usuarios."):
            return redirect(url_for("web.inicio"))
    emps = mis_empresas()
    secs = [dict(titulo="Empresas a su cargo", tarjetas=[dict(titulo=e.razon_social, sub=f"RIF {e.rif}" + (" · Contribuyente especial" if e.es_sujeto_pasivo_especial else ""),
                                                             url=url_empresa(e.id)) for e in emps], vacio="Aún no tiene empresas asignadas.")]
    if g.usuario.rol == "Administrador":
        secs.append(dict(titulo="Nueva empresa", nota=f"Su plan permite hasta {limite_licencia('max_empresas')} empresas.", form=dict(action=url_for("web.inicio"), boton="Crear empresa", campos=[
            campo("razon_social", "Razón social"), campo("rif", "RIF (J-12345678-9)"), campo("direccion", "Domicilio fiscal", req=False), campo("telefono", "Teléfono", req=False),
            campo("spe", "¿Sujeto pasivo especial?", "select", opciones=[("0", "No"), ("1", "Sí")])])))
    return pagina("Panel", None, secs)


# ---------------------------------------------------------------- cuentas
@ruta("/e/<int:eid>/cuentas", "asientos.ver", ("GET", "POST"))
def cuentas(eid):
    if request.method == "POST":
        if not tiene_permiso(g.usuario, "cuentas.editar"): return ("", 403)
        def alta():
            db.session.add(Cuenta(empresa_id=eid, codigo=F["codigo"].strip(), nombre=F["nombre"].strip(), tipo=F["tipo"],
                                  categoria_niif18=F["categoria"], es_partida_monetaria=F.get("monetaria") == "1"))
            db.session.commit()
        if intentar(alta, "Cuenta creada"): return redirect(request.url)
    cs = db.session.execute(select(Cuenta).where(Cuenta.empresa_id == eid).order_by(Cuenta.codigo)).scalars()
    return pagina("Plan de cuentas", eid, [
        dict(tabla=dict(cols=["Código", "Nombre", "Tipo", "NIIF 18", "Monetaria"], filas=[dict(cells=[c.codigo, c.nombre, c.tipo, c.categoria_niif18, "Sí" if c.es_partida_monetaria else "No"]) for c in cs])),
        dict(titulo="Nueva cuenta", form=dict(action=request.path, boton="Crear", campos=[
            campo("codigo", "Código"), campo("nombre", "Nombre"),
            campo("tipo", "Tipo", "select", opciones=[(t, t) for t in ("Activo", "Pasivo", "Patrimonio", "Ingreso", "Gasto")]),
            campo("categoria", "Categoría NIIF 18", "select", opciones=[(c, c) for c in estados.CATEGORIAS]),
            campo("monetaria", "¿Partida monetaria?", "select", opciones=[("0", "No"), ("1", "Sí")])]))])


# ---------------------------------------------------------------- diario
@ruta("/e/<int:eid>/asientos", "asientos.ver", ("GET", "POST"))
def asientos(eid):
    if request.method == "POST":
        if not tiene_permiso(g.usuario, "asientos.crear"): return ("", 403)
        lineas = [(F.get(f"c{i}", ""), F.get(f"d{i}") or 0, F.get(f"h{i}") or 0) for i in range(1, 7) if F.get(f"c{i}", "").strip()]
        if intentar(lambda: contab.crear_asiento(eid, g.usuario, F["fecha"], F["concepto"], lineas), lambda a: f"Asiento N°{a} registrado"):
            return redirect(request.url)
    cods = [(c, f"{c} {n}") for c, n in db.session.execute(select(Cuenta.codigo, Cuenta.nombre).where(Cuenta.empresa_id == eid).order_by(Cuenta.codigo))]
    rows = db.session.execute(select(Asiento, Movimiento).join(Movimiento, Movimiento.asiento_id == Asiento.id)
                              .where(Asiento.empresa_id == eid).order_by(Asiento.id.desc(), Movimiento.id).limit(300)).all()
    filas = []
    for a, m in rows:
        f = dict(cells=[a.id, a.fecha, a.concepto, m.cuenta_codigo, m.debe, m.haber])
        filas.append(f)
    anul = dict(titulo="Anular asiento (contra-asiento)", nota="Los asientos no se borran ni se editan: se anulan con un contra-asiento.",
                form=dict(action=url_for("web.anular", eid=eid), boton="Anular", campos=[campo("asiento_id", "N° de asiento", "number"), campo("fecha", "Fecha del contra-asiento", "date", valor=hoy()), campo("motivo", "Motivo")]))
    nuevo = dict(titulo="Nuevo asiento", nota="Hasta 6 líneas. Debe y haber deben cuadrar.", form=dict(action=request.path, boton="Registrar", campos=[
        campo("fecha", "Fecha", "date", valor=hoy()), campo("concepto", "Concepto")] + sum(
        [[campo(f"c{i}", f"Cuenta {i}", "select", req=i <= 2, opciones=[("", "—")] + cods), campo(f"d{i}", "Debe", "number", req=False), campo(f"h{i}", "Haber", "number", req=False)] for i in range(1, 7)], [])))
    out = [nuevo, dict(titulo="Libro diario (últimos movimientos)", tabla=dict(cols=["N°", "Fecha", "Concepto", "Cuenta", "Debe", "Haber"], num=[4, 5], filas=filas))]
    if tiene_permiso(g.usuario, "asientos.anular"): out.insert(1, anul)
    return pagina("Libro diario", eid, out)


@ruta("/e/<int:eid>/asientos/anular", "asientos.anular", ("POST",))
def anular(eid):
    intentar(lambda: contab.anular_asiento(eid, g.usuario, int(F["asiento_id"]), F["fecha"], F["motivo"]), lambda a: f"Contra-asiento N°{a} registrado")
    return redirect(url_for("web.asientos", eid=eid))


# ---------------------------------------------------------------- períodos, cierre y configuración
@ruta("/e/<int:eid>/cierres", "asientos.ver", ("GET", "POST"))
def periodos(eid):
    if request.method == "POST":
        if not tiene_permiso(g.usuario, "periodos.cerrar"): return ("", 403)
        if F.get("accion") == "periodo": intentar(lambda: S.cerrar_periodo(eid, g.usuario, F["mes"]), "Período cerrado")
        else: intentar(lambda: S.ejecutar_cierre(eid, g.usuario, F["fecha"], F["cuenta_patrimonio"]), lambda a: f"Asiento de cierre N°{a} registrado")
        return redirect(request.url)
    cer = db.session.execute(select(PeriodoCerrado.mes_anio).where(PeriodoCerrado.empresa_id == eid).order_by(PeriodoCerrado.mes_anio)).scalars()
    return pagina("Cierres contables", eid, [
        dict(titulo="Períodos cerrados", tabla=dict(cols=["Mes"], filas=[dict(cells=[m]) for m in cer])),
        dict(titulo="Cerrar período", form=dict(action=request.path, boton="Cerrar", campos=[campo("accion", "", "hidden", valor="periodo"), campo("mes", "Mes (AAAA-MM)")])),
        dict(titulo="Cierre de ejercicio (refundición)", form=dict(action=request.path, boton="Ejecutar cierre", campos=[campo("accion", "", "hidden", valor="cierre"), campo("fecha", "Fecha de cierre", "date", valor=hoy()), campo("cuenta_patrimonio", "Cuenta de patrimonio (código)")])),
        dict(titulo="Cuentas para asientos automáticos", nota="Si están completas, CxC/CxP/Inventario generan su asiento en la misma transacción.",
             form=dict(action=url_for("web.config_asientos", eid=eid), boton="Guardar", campos=[campo(f"cuenta_{c}", c.replace("_", " "), req=False, valor=getattr(db.session.get(ConfigAsientos, eid), f"cuenta_{c}", "") if db.session.get(ConfigAsientos, eid) else "") for c in CUENTAS_CFG]))])


@ruta("/e/<int:eid>/config-asientos", "config.contable", ("POST",))
def config_asientos(eid):
    def guardar():
        c = db.session.get(ConfigAsientos, eid) or ConfigAsientos(empresa_id=eid)
        validas = set(db.session.execute(select(Cuenta.codigo).where(Cuenta.empresa_id == eid)).scalars())
        for k in CUENTAS_CFG:
            v = (F.get(f"cuenta_{k}") or "").strip() or None
            if v and v not in validas: raise ErrorAsiento(f"La cuenta {v} no existe en esta empresa")
            setattr(c, f"cuenta_{k}", v)
        db.session.add(c); db.session.commit()
    intentar(guardar, "Configuración guardada")
    return redirect(url_for("web.periodos", eid=eid))


# ---------------------------------------------------------------- CxC / CxP
def _lista_cartera(eid, Modelo, nombre_attr, tipo):
    docs = db.session.execute(select(Modelo).where(Modelo.empresa_id == eid).order_by(Modelo.fecha_vencimiento)).scalars()
    endpoint = "web.cobro" if tipo == "cxc" else "web.pago"
    return [dict(cells=[d.id, getattr(d, nombre_attr), d.numero_documento, d.fecha_emision, d.fecha_vencimiento, d.monto, d.saldo_pendiente, d.estado],
                 form=dict(action=url_for(endpoint, eid=eid, did=d.id), boton="Cobrar" if tipo == "cxc" else "Pagar", campos=[campo("monto", "Monto", "number"), campo("fecha", "Fecha", "date", valor=hoy()), campo("ref", "Ref.", req=False)])
                 if d.saldo_pendiente > 0 else None) for d in docs]


def _alta_cartera(eid, tipo, tercero_attr):
    d = {k: F.get(k) for k in ("numero_documento", "fecha_emision", "fecha_vencimiento", "monto", "base_imponible", "monto_iva", tercero_attr, tercero_attr.replace("nombre", "rif"),
                               "numero_control", "exento", "alicuota", "tipo_documento", "documento_afectado", "importacion")}
    alta = S.registrar_cxc if tipo == "cxc" else S.registrar_cxp
    return intentar(lambda: alta(eid, g.usuario, d), lambda r: r[1])


@ruta("/e/<int:eid>/cxc", "cartera.ver", ("GET", "POST"))
def cxc(eid):
    if request.method == "POST":
        if not tiene_permiso(g.usuario, "cartera.operar"): return ("", 403)
        _alta_cartera(eid, "cxc", "deudor_nombre"); return redirect(request.url)
    det, tot = S.aging_y_provision(eid, hoy_ve())
    return pagina("Cuentas por cobrar", eid, [
        dict(tabla=dict(cols=["ID", "Deudor", "Documento", "Emisión", "Vence", "Monto", "Saldo", "Estado"], num=[5, 6], accion=True, filas=_lista_cartera(eid, DocumentoCxC, "deudor_nombre", "cxc"))),
        dict(titulo="Nuevo documento por cobrar", form=dict(action=request.path, boton="Registrar", campos=[
            campo("deudor_nombre", "Deudor"), campo("deudor_rif", "RIF", req=False), campo("numero_documento", "N° documento"),
            campo("fecha_emision", "Emisión", "date", valor=hoy()), campo("fecha_vencimiento", "Vencimiento", "date"), campo("monto", "Monto total", "number"),
            campo("base_imponible", "Base imponible", "number", req=False), campo("monto_iva", "IVA", "number", req=False)])),
        dict(titulo=f"Antigüedad de saldos y provisión de incobrables (total {tot})", tabla=dict(cols=["ID", "Deudor", "Documento", "Días vencido", "Saldo", "% prov.", "Provisión"], num=[3, 4, 5, 6], filas=[dict(cells=list(r)) for r in det])),
        dict(titulo="Registrar provisión", form=dict(action=url_for("web.provision", eid=eid), boton="Contabilizar provisión", campos=[campo("fecha", "Fecha", "date", valor=hoy())]))])


@ruta("/e/<int:eid>/cxc/provision", "cartera.operar", ("POST",))
def provision(eid):
    intentar(lambda: S.aplicar_provision_incobrables(eid, g.usuario, F["fecha"]), lambda r: f"Asiento N°{r[0]} por {r[1]}")
    return redirect(url_for("web.cxc", eid=eid))


@ruta("/e/<int:eid>/cxc/<int:did>/cobro", "cartera.operar", ("POST",))
def cobro(eid, did):
    intentar(lambda: S.registrar_cobro(eid, g.usuario, did, F["fecha"], F["monto"], F.get("ref")), "Cobro registrado")
    return redirect(url_for("web.cxc", eid=eid))


@ruta("/e/<int:eid>/cxp", "cartera.ver", ("GET", "POST"))
def cxp(eid):
    if request.method == "POST":
        if not tiene_permiso(g.usuario, "cartera.operar"): return ("", 403)
        _alta_cartera(eid, "cxp", "proveedor_nombre"); return redirect(request.url)
    return pagina("Cuentas por pagar", eid, [
        dict(tabla=dict(cols=["ID", "Proveedor", "Documento", "Emisión", "Vence", "Monto", "Saldo", "Estado"], num=[5, 6], accion=True, filas=_lista_cartera(eid, DocumentoCxP, "proveedor_nombre", "cxp"))),
        dict(titulo="Nuevo documento por pagar", form=dict(action=request.path, boton="Registrar", campos=[
            campo("proveedor_nombre", "Proveedor"), campo("proveedor_rif", "RIF"), campo("tipo_documento", "Documento", "select", opciones=[("FAC", "Factura"), ("NC", "Nota de crédito"), ("ND", "Nota de débito")]),
            campo("numero_documento", "N° de factura"), campo("numero_control", "N° de control"), campo("documento_afectado", "Factura afectada (NC/ND)", req=False),
            campo("fecha_emision", "Emisión", "date", valor=hoy()), campo("fecha_vencimiento", "Vencimiento", "date"), campo("monto", "Total con IVA", "number"),
            campo("exento", "Exento / no sujeto", "number", req=False), campo("base_imponible", "Base imponible", "number", req=False), campo("alicuota", "Alícuota %", "number", req=False),
            campo("monto_iva", "IVA (crédito fiscal)", "number", req=False), campo("importacion", "Tipo de compra", "select", opciones=[("0", "Nacional"), ("1", "Importación")])]))],
        acciones=[dict(url=url_for("web.libro_compras", eid=eid), texto="Libro de compras")])


@ruta("/e/<int:eid>/cxp/libro-compras", "cartera.ver")
def libro_compras(eid):
    import csv
    import io
    from flask import Response
    periodo = request.args.get("periodo") or hoy()[:7]
    if len(periodo) != 7 or periodo[4] != "-" or not periodo.replace("-", "").isdigit(): flash("Período inválido"); periodo = hoy()[:7]
    filas, tot = S.libro_compras(eid, periodo)
    cols = ["N°", "Fecha", "RIF", "Proveedor", "Tipo de compra", "Doc.", "N° documento", "N° control", "Doc. afectado", "Total con IVA", "Exento",
            "Base imponible", "% alícuota", "IVA crédito fiscal", "IVA retenido", "Comprobante"]
    claves = ["n", "fecha", "rif", "nombre", "tipo_compra", "tipo", "numero", "control", "afectado", "total", "exento", "base", "alicuota", "iva", "retenido", "comprobante"]
    total = ["", "", "", "TOTALES", "", "", "", "", "", tot["total"], tot["exento"], tot["base"], "", tot["iva"], tot["retenido"], ""]
    if request.args.get("formato") == "csv":
        buf = io.StringIO(); w = csv.writer(buf, delimiter=";")
        w.writerow(cols)
        for f in filas: w.writerow([str(f[k]) for k in claves])
        w.writerow([str(x) for x in total])
        from .auditoria import registrar
        registrar(g.usuario.username, "LIBRO_COMPRAS_EXPORTADO", f"empresa={eid} periodo={periodo}"); db.session.commit()
        return Response("\ufeff" + buf.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f'attachment; filename="libro_compras_{periodo}.csv"'})
    return pagina("Libro de compras", eid, [
        dict(titulo="Período", form=dict(action=request.path, metodo="get", boton="Ver", campos=[campo("periodo", "Mes", "month", valor=periodo)])),
        dict(titulo=f"Libro de compras {periodo}", nota="Art. 75 del Reglamento de la Ley de IVA. Generado desde Cuentas por pagar; el IVA retenido sale de las retenciones de IVA registradas (Prov. SNAT/2015/0049).",
             tabla=dict(cols=cols, num=[9, 10, 11, 12, 13, 14], ancha=True, filas=[dict(cells=[f[k] for k in claves]) for f in filas] + [dict(cells=total, clase="total")])),
        dict(pares=[("Crédito fiscal del período (Bs)", tot["iva"]), ("IVA retenido a proveedores (Bs)", tot["retenido"]), ("Compras exentas (Bs)", tot["exento"])])],
        acciones=[dict(url=url_for("web.libro_compras", eid=eid, periodo=periodo, formato="csv"), texto="Exportar CSV", primaria=True), dict(url=url_for("web.cxp", eid=eid), texto="Volver")])


@ruta("/e/<int:eid>/cxp/<int:did>/pago", "cartera.operar", ("POST",))
def pago(eid, did):
    intentar(lambda: S.registrar_pago(eid, g.usuario, did, F["fecha"], F["monto"], F.get("ref")), "Pago registrado")
    return redirect(url_for("web.cxp", eid=eid))


# ---------------------------------------------------------------- bancos (extracto y conciliación)
@ruta("/e/<int:eid>/bancos", "cartera.ver", ("GET", "POST"))
def bancos(eid):
    if request.method == "POST":
        if not tiene_permiso(g.usuario, "cartera.operar"): return ("", 403)
        if F["accion"] == "linea":
            def alta():
                cat = F["categoria"]
                if cat not in ("Operacion", "Inversion", "Financiamiento"): raise ValueError("Categoría de flujo inválida")
                from .contabilidad.validadores import parsear_fecha
                m = Decimal(str(F["monto"]))  # puede ser negativo (egreso): se valida como número, nunca como float
                db.session.add(ExtractoBancario(empresa_id=eid, fecha=parsear_fecha(F["fecha"]), descripcion=F.get("descripcion"), monto=m.quantize(Decimal("0.01")), categoria_flujo=cat))
                db.session.commit()
            intentar(alta, "Movimiento bancario registrado")
        else:
            def marcar():
                x = db.session.execute(select(ExtractoBancario).where(ExtractoBancario.id == int(F["lid"]), ExtractoBancario.empresa_id == eid)).scalar_one_or_none()
                if x is None: raise ValueError("Movimiento no encontrado")
                x.conciliado = not x.conciliado; db.session.commit()
            intentar(marcar, "Estado de conciliación actualizado")
        return redirect(request.url)
    ls = db.session.execute(select(ExtractoBancario).where(ExtractoBancario.empresa_id == eid).order_by(ExtractoBancario.fecha.desc(), ExtractoBancario.id.desc()).limit(300)).scalars().all()
    pend = sum((x.monto for x in ls if not x.conciliado), Decimal(0))
    return pagina("Bancos y conciliación", eid, [
        dict(titulo=f"Extracto bancario — pendiente de conciliar: {pend}", tabla=dict(cols=["ID", "Fecha", "Descripción", "Monto", "Flujo", "Conciliado"], num=[3], accion=True, filas=[
            dict(cells=[x.id, x.fecha, x.descripcion, x.monto, x.categoria_flujo, "Sí" if x.conciliado else "No"], form=dict(action=request.path, boton="Conciliado Sí/No", campos=[campo("accion", "", "hidden", valor="marcar"), campo("lid", "", "hidden", valor=x.id)])) for x in ls])),
        dict(titulo="Registrar movimiento del extracto", nota="Use monto negativo para egresos. La categoría alimenta el estado de flujo de efectivo.", form=dict(action=request.path, boton="Registrar", campos=[
            campo("accion", "", "hidden", valor="linea"), campo("fecha", "Fecha", "date", valor=hoy()), campo("descripcion", "Descripción"), campo("monto", "Monto", "number"),
            campo("categoria", "Flujo", "select", opciones=[("Operacion", "Operación"), ("Inversion", "Inversión"), ("Financiamiento", "Financiamiento")])]))])


# ---------------------------------------------------------------- inventario
@ruta("/e/<int:eid>/inventario", "inventario.ver", ("GET", "POST"))
def inventario(eid):
    if request.method == "POST":
        if not tiene_permiso(g.usuario, "inventario.operar"): return ("", 403)
        a = F["accion"]
        if a == "producto": intentar(lambda: S.alta_producto(eid, g.usuario, F["codigo"], F["nombre"], F["metodo"], F.get("precio") or 0, F.get("alicuota", "G")), "Producto registrado")
        elif a == "entrada": intentar(lambda: S.entrada_inventario(eid, g.usuario, F["codigo"], F["cantidad"], F["costo"], F["fecha"], F.get("ref", ""), F.get("pago") == "Credito"), "Entrada registrada")
        elif a == "salida": intentar(lambda: S.salida_inventario(eid, g.usuario, F["codigo"], F["cantidad"], F["fecha"], F.get("ref", "")), lambda r: f"Salida registrada, costo {r[0]}")
        return redirect(request.url)
    tot, det = S.valor_inventario(eid)
    prods = [(c, c) for c, *_ in det]
    h = lambda v: campo("accion", "", "hidden", valor=v)
    return pagina("Inventario", eid, [
        dict(titulo=f"Existencias (valor total {tot})", tabla=dict(cols=["Código", "Producto", "Método", "Cantidad", "Valor"], num=[3, 4], filas=[dict(cells=list(r)) for r in det])),
        dict(titulo="Nuevo producto", form=dict(action=request.path, boton="Crear", campos=[h("producto"), campo("codigo", "Código"), campo("nombre", "Nombre"), campo("metodo", "Costeo", "select", opciones=[("Promedio", "Promedio ponderado"), ("PEPS", "PEPS")]),
            campo("precio", "Precio de venta (Bs)", "number", req=False), campo("alicuota", "IVA", "select", opciones=[("G", "General"), ("R", "Reducida"), ("A", "Lujo (general + adicional)"), ("E", "Exento (E)")])])),
        dict(titulo="Entrada", form=dict(action=request.path, boton="Registrar entrada", campos=[h("entrada"), campo("codigo", "Producto", "select", opciones=prods), campo("cantidad", "Cantidad", "number"), campo("costo", "Costo unitario", "number"), campo("fecha", "Fecha", "date", valor=hoy()), campo("pago", "Forma de pago", "select", opciones=[("Contado", "Contado"), ("Credito", "Crédito")]), campo("ref", "Referencia", req=False)])),
        dict(titulo="Salida (costo de venta)", form=dict(action=request.path, boton="Registrar salida", campos=[h("salida"), campo("codigo", "Producto", "select", opciones=prods), campo("cantidad", "Cantidad", "number"), campo("fecha", "Fecha", "date", valor=hoy()), campo("ref", "Referencia", req=False)])),
        dict(titulo="Kardex (últimos 200)", tabla=dict(cols=["Fecha", "Producto", "Tipo", "Cantidad", "Costo unit.", "Referencia"], num=[3, 4], filas=[dict(cells=[m.fecha, m.producto_codigo, m.tipo, m.cantidad, m.costo_unitario, m.referencia]) for m in db.session.execute(select(MovInventario).where(MovInventario.empresa_id == eid).order_by(MovInventario.id.desc()).limit(200)).scalars()]))])


# ---------------------------------------------------------------- reportes
@ruta("/e/<int:eid>/reportes", "reportes.ver")
def reportes(eid):
    fecha = request.args.get("fecha") or hoy()
    try: rows = contab.saldos(eid, hasta=date.fromisoformat(fecha))
    except ValueError: flash("Fecha inválida"); rows, fecha = contab.saldos(eid), hoy()
    b, r = estados.balance(rows), estados.estado_resultados(rows, fecha)
    cp, ta, td = estados.cambios_patrimonio(rows)
    sec = [dict(titulo="Fecha de corte", form=dict(action=request.path, metodo="get", boton="Ver", campos=[campo("fecha", "Hasta", "date", valor=fecha)])),
           dict(titulo=f"Balance general — {'CUADRA' if b['cuadra'] else 'NO CUADRA'}", pares=[("Total activos", b["total_activos"]), ("Pasivo + patrimonio + utilidad", b["total_pasivo_patrimonio"]), ("Utilidad del período", b["utilidad"])]),
           dict(titulo="Activos", tabla=dict(cols=["Cuenta", "Nombre", "Saldo"], num=[2], filas=[dict(cells=list(x)) for x in b["activos"]])),
           dict(titulo="Pasivos", tabla=dict(cols=["Cuenta", "Nombre", "Saldo"], num=[2], filas=[dict(cells=list(x)) for x in b["pasivos"]])),
           dict(titulo="Patrimonio", tabla=dict(cols=["Cuenta", "Nombre", "Saldo"], num=[2], filas=[dict(cells=list(x)) for x in b["patrimonio"]]))]
    if r["normativa"] == "NIC 1":
        sec.append(dict(titulo="Estado de resultados (NIC 1)", pares=[("Ingresos", r["total_ingresos"]), ("Gastos", r["total_gastos"]), ("Utilidad del ejercicio", r["utilidad_del_ejercicio"])]))
    else:
        sec.append(dict(titulo="Estado de resultados (NIIF 18)", pares=[("Resultado operativo", r["resultado_operativo"]), ("Antes de financiamiento e impuestos", r["resultado_antes_financiamiento_impuestos"]), ("Resultado del período", r["resultado_del_periodo"])]))
    sec.append(dict(titulo="Cambios en el patrimonio", tabla=dict(cols=["Cuenta", "Nombre", "Aumentos", "Disminuciones", "Saldo"], num=[2, 3, 4], filas=[dict(cells=list(x)) for x in cp])))
    ini = request.args.get("desde") or f"{fecha[:4]}-01-01"
    try: fl = S.flujo_efectivo(eid, date.fromisoformat(ini), date.fromisoformat(fecha)); sec.append(dict(titulo="Flujo de efectivo (extracto bancario)", pares=[(k.replace("_", " ").capitalize(), v) for k, v in fl.items()]))
    except ValueError: pass
    return pagina("Reportes financieros", eid, sec)


@ruta("/e/<int:eid>/mayor", "reportes.ver")
def mayor(eid):
    """Libro mayor y balance de comprobación (Código de Comercio, Arts. 32-35; presentación según VEN-NIF)."""
    hasta = request.args.get("hasta") or hoy()
    desde = request.args.get("desde") or f"{hasta[:4]}-01-01"
    cuenta = (request.args.get("cuenta") or "").strip()
    try: d, h = date.fromisoformat(desde), date.fromisoformat(hasta)
    except ValueError: flash("Fechas inválidas"); d, h = date(hoy_ve().year, 1, 1), hoy_ve()
    rows = contab.saldos(eid, hasta=h, desde=d)
    td, th = sum((r["debe"] for r in rows), Decimal(0)), sum((r["haber"] for r in rows), Decimal(0))
    cods = [("", "— Seleccione —")] + [(r["codigo"], f"{r['codigo']} {r['nombre']}") for r in rows]
    sec = [dict(titulo="Filtro", form=dict(action=request.path, metodo="get", boton="Consultar", campos=[campo("desde", "Desde", "date", valor=desde), campo("hasta", "Hasta", "date", valor=hasta),
                                                                                                         campo("cuenta", "Cuenta (mayor analítico)", "select", req=False, opciones=cods, valor=cuenta)])),
           dict(titulo=f"Balance de comprobación — {'CUADRA' if td == th else 'NO CUADRA'}", tabla=dict(cols=["Cuenta", "Nombre", "Tipo", "Debe", "Haber", "Saldo"], num=[3, 4, 5], filas=[
               dict(cells=[r["codigo"], r["nombre"], r["tipo"], r["debe"], r["haber"], r["debe"] - r["haber"]]) for r in rows if r["debe"] or r["haber"]] + [dict(cells=["", "TOTALES", "", td, th, td - th], clase="total")]))]
    if cuenta:
        movs = db.session.execute(select(Asiento.id, Asiento.fecha, Asiento.concepto, Movimiento.debe, Movimiento.haber).join(Movimiento, Movimiento.asiento_id == Asiento.id)
                                  .where(Asiento.empresa_id == eid, Movimiento.cuenta_codigo == cuenta, Asiento.fecha >= d, Asiento.fecha <= h).order_by(Asiento.fecha, Asiento.id)).all()
        saldo, filas = Decimal(0), []
        for aid, f, c, de, ha in movs:
            saldo += de - ha; filas.append(dict(cells=[f, aid, c, de, ha, saldo]))
        sec.append(dict(titulo=f"Mayor analítico de la cuenta {cuenta}", tabla=dict(cols=["Fecha", "Asiento", "Concepto", "Debe", "Haber", "Saldo"], num=[3, 4, 5], filas=filas)))
    return pagina("Libro mayor", eid, sec)


# ---------------------------------------------------------------- auditoría
@ruta("/e/<int:eid>/auditoria", "auditoria.ver", ("GET", "POST"))
def auditoria(eid):
    if request.method == "POST":
        if not tiene_permiso(g.usuario, "auditoria.crear"): return ("", 403)
        if F["accion"] == "hallazgo": intentar(lambda: S.crear_hallazgo(eid, g.usuario, F["titulo"], F.get("descripcion"), F.get("cedula"), F.get("cuenta"), F["severidad"]), "Hallazgo registrado")
        elif F["accion"] == "cerrar": intentar(lambda: S.cerrar_hallazgo(eid, g.usuario, int(F["hid"]), F["comentario"]), "Hallazgo cerrado")
        elif F["accion"] == "papel":
            if F["cedula_tipo"] not in CEDULAS: return ("", 400)
            db.session.add(PapelTrabajo(empresa_id=eid, cedula_tipo=F["cedula_tipo"], cuenta_codigo=F.get("cuenta"), titulo=F["titulo"], contenido=F.get("contenido"), auditor=g.usuario.username))
            from .auditoria import registrar
            registrar(g.usuario.username, "PAPEL_TRABAJO", f"empresa={eid} {F['titulo'][:80]}"); db.session.commit(); flash("Papel de trabajo guardado")
        return redirect(request.url)
    h = lambda v: campo("accion", "", "hidden", valor=v)
    hall = db.session.execute(select(Hallazgo).where(Hallazgo.empresa_id == eid).order_by(Hallazgo.id.desc())).scalars().all()
    pap = db.session.execute(select(PapelTrabajo).where(PapelTrabajo.empresa_id == eid).order_by(PapelTrabajo.id.desc()).limit(100)).scalars()
    return pagina("Auditoría", eid, [
        dict(titulo="Pruebas automáticas de integridad", tabla=dict(cols=["Severidad", "Hallazgo", "Detalle", "Cédula"], filas=[dict(cells=list(x)) for x in S.verificar_integridad(eid)])),
        dict(titulo="Hallazgos", tabla=dict(cols=["ID", "Severidad", "Título", "Estado", "Auditor", "Cierre"], accion=True, filas=[dict(cells=[x.id, x.severidad, x.titulo, x.estado, x.auditor, x.comentario_cierre or ""], form=dict(action=request.path, boton="Cerrar", campos=[h("cerrar"), campo("hid", "", "hidden", valor=x.id), campo("comentario", "Comentario")]) if x.estado != "Cerrado" and tiene_permiso(g.usuario, "auditoria.crear") else None) for x in hall])),
        dict(titulo="Nuevo hallazgo", form=dict(action=request.path, boton="Registrar", campos=[h("hallazgo"), campo("titulo", "Título"), campo("severidad", "Severidad", "select", opciones=[(s, s) for s in ("Crítica", "Alta", "Media", "Baja")]), campo("cedula", "Cédula ref.", req=False), campo("cuenta", "Cuenta", req=False), campo("descripcion", "Descripción", "textarea", req=False)])),
        dict(titulo="Papeles de trabajo", tabla=dict(cols=["Fecha", "Cédula", "Título", "Auditor"], filas=[dict(cells=[p.fecha.date(), p.cedula_tipo, p.titulo, p.auditor]) for p in pap])),
        dict(titulo="Nuevo papel de trabajo", nota="Cédulas según NIA 230 (documentación de auditoría): sumaria, analítica, de ajustes, de hallazgos.",
             form=dict(action=request.path, boton="Guardar", campos=[h("papel"), campo("cedula_tipo", "Tipo de cédula", "select", opciones=[(c, c) for c in CEDULAS]), campo("titulo", "Título"), campo("cuenta", "Cuenta", req=False), campo("contenido", "Contenido", "textarea", req=False)])),
        _seccion_bitacora()])


CEDULAS = ("Cédula sumaria", "Cédula analítica", "Cédula de ajustes y reclasificaciones", "Cédula de hallazgos", "Programa de auditoría", "Carta de gerencia", "Otra")


def _seccion_bitacora():
    from .models import AuditLog
    ok, rota = verificar_cadena()
    logs = db.session.execute(select(AuditLog).where(AuditLog.organizacion_id == g.usuario.organizacion_id).order_by(AuditLog.id.desc()).limit(200)).scalars()
    return dict(titulo=f"Bitácora de la organización — cadena {'ÍNTEGRA' if ok else f'ROTA en el registro {rota}'}", nota="Registro inalterable encadenado por SHA-256 (últimos 200 eventos).",
                tabla=dict(cols=["Fecha (UTC)", "Usuario", "IP", "Acción", "Detalle"], filas=[dict(cells=[l.ts.strftime("%Y-%m-%d %H:%M:%S"), l.usuario, l.ip or "", l.accion, (l.detalles or "")[:120]]) for l in logs]))


from . import web_admin, web_facturacion, web_fiscal, web_plataforma  # noqa: E402,F401  (registran sus rutas en el mismo blueprint)
