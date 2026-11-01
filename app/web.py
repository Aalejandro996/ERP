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
from .menu import menu_empresa, pestanas

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


RE_LINEA = re.compile(r"^l(\d+)_\w+$")
LINEAS_VISIBLES, MAX_CAMPOS_A_LA_VISTA = 2, 8
TIPOS_OPCIONALES = ("text", "number", "date", "email", "tel", "textarea", "url")


def compactar(form):
    """Formularios cortos para el cliente: las líneas repetidas (l1_…, l2_…) van en una tabla que muestra solo
    LINEAS_VISIBLES filas y crece con «Agregar línea»; en formularios largos los campos opcionales de texto se pliegan
    en «Más datos (opcional)». Los selectores y los obligatorios siguen a la vista. El envío no cambia."""
    lineas, resto = {}, []
    for c in form.get("campos") or []:
        m = RE_LINEA.match(c["n"]) if c["t"] != "hidden" else None
        (lineas.setdefault(int(m[1]), []) if m else resto).append(c)
    out = dict(form)
    if len(lineas) >= 2:
        nums = sorted(lineas)
        cols = [re.sub(r"\s\d+(?=\s|$)", "", c["l"]).strip() for c in lineas[nums[0]]]
        out["lineas"] = dict(cols=cols, filas=[dict(campos=lineas[i], extra=k >= LINEAS_VISIBLES and not any(c.get("req") and c["t"] != "select" for c in lineas[i]))
                                               for k, i in enumerate(nums)])
    else:
        resto = form.get("campos") or []
    visibles = [c for c in resto if c["t"] != "hidden"]
    opcionales = [c for c in visibles if not c.get("req") and c["t"] in TIPOS_OPCIONALES]
    if len(visibles) > MAX_CAMPOS_A_LA_VISTA and 3 <= len(opcionales) < len(visibles):
        ids = {id(c) for c in opcionales}
        resto, out["opcionales"] = [c for c in resto if id(c) not in ids], opcionales
    out["campos"] = resto
    return out


def pagina(titulo, eid, secciones, acciones=None):
    emp = db.session.get(Empresa, eid) if eid else None
    secciones = [({**s, "tabla": paginar(s["tabla"], f"pag{i}" if i else "pag")} if isinstance(s, dict) and s.get("tabla") else s)
                 for i, s in enumerate(secciones)]
    secciones = [({**s, "form": compactar(s["form"])} if isinstance(s, dict) and s.get("form") else s) for s in secciones]
    return render_template("pagina.html", titulo=titulo, empresa=emp, secciones=secciones, acciones=acciones or [],
                           pestanas=pestanas(eid) if emp is not None else [])


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
        protegida = requiere(permiso)(f)

        @bp.route(patron, methods=list(metodos), endpoint=f.__name__)
        @wraps(f)
        def interior(*a, **kw):
            if "eid" in kw and getattr(g, "usuario", None) is not None and g.usuario.organizacion_id is not None:
                empresa_autorizada(kw["eid"])
                # Módulos que trabaja ESTA empresa (elegidos en el tutorial o en «Datos de la empresa»), dentro del plan:
                # el permiso se evalúa después, así un módulo apagado en la empresa responde 403 aunque el plan lo traiga.
                g.modulos_licencia = g.get("modulos_licencia", g.modulos)
                g.modulos = db.session.get(Empresa, kw["eid"]).modulos_empresa(g.modulos_licencia)
            return protegida(*a, **kw)
        return interior
    return deco


F = LocalProxy(lambda: request.form)  # valores del formulario (se resuelve en cada petición)


@bp.app_context_processor
def ayudantes_plantilla():
    return {"puede": lambda p: g.usuario is not None and tiene_permiso(g.usuario, p),
            "mis_empresas": lambda: mis_empresas() if g.usuario is not None and g.usuario.organizacion_id else [],
            "url_empresa": url_empresa, "menu_empresa": menu_empresa}


# ---------------------------------------------------------------- panel y empresas
def url_empresa(eid):
    """Primera pantalla de la empresa que el plan y el rol permiten."""
    for permiso, endpoint in (("asientos.ver", "web.asientos"), ("facturacion.ver", "web.facturas"), ("cartera.ver", "web.cxc"),
                              ("inventario.ver", "web.inventario"), ("auditoria.ver", "web.auditoria")):
        if tiene_permiso(g.usuario, permiso): return url_for(endpoint, eid=eid)
    return None


def limite_licencia(clave):
    return int((g.licencia or {}).get(clave) or 0)


LOGO_MAX = 300 * 1024


def leer_logo(archivo):
    """Logo de la empresa: PNG, JPEG o WebP de hasta 300 KB (se reconoce por su contenido, no por la extensión)."""
    if archivo is None or not archivo.filename: return None
    datos = archivo.read(LOGO_MAX + 1)
    if len(datos) > LOGO_MAX: raise ValueError("El logo no puede superar 300 KB")
    if datos[:8] == b"\x89PNG\r\n\x1a\n": return datos, "image/png"
    if datos[:3] == b"\xff\xd8\xff": return datos, "image/jpeg"
    if datos[:4] == b"RIFF" and datos[8:12] == b"WEBP": return datos, "image/webp"
    raise ValueError("El logo debe ser una imagen PNG, JPG o WebP")


def aplicar_datos_empresa(e, form, archivos):
    """Datos básicos, dirección fiscal, teléfono, correo, contribuyente especial y logo (alta, tutorial y edición)."""
    e.razon_social = (form.get("razon_social") or e.razon_social or "").strip()[:200]
    if not e.razon_social: raise ValueError("Indique la razón social")
    for k in ("direccion", "telefono", "correo"):
        if k in form: setattr(e, k, (form.get(k) or "").strip()[:300] or None)
    if e.correo and "@" not in e.correo: raise ValueError("Correo de la empresa inválido")
    if "spe" in form: e.es_sujeto_pasivo_especial = form.get("spe") == "1"
    logo = leer_logo(archivos.get("logo") if archivos else None)
    if logo: e.logo, e.logo_mime = logo
    if form.get("quitar_logo") == "1": e.logo = e.logo_mime = None


def crear_empresa(form, archivos=None) -> Empresa:
    org = g.usuario.organizacion_id
    n = db.session.execute(select(db.func.count()).select_from(Empresa).where(Empresa.organizacion_id == org)).scalar()
    if n >= limite_licencia("max_empresas"): raise ValueError(f"Su licencia permite {limite_licencia('max_empresas')} empresa(s)")
    rif = validar_rif(form["rif"])
    if db.session.execute(select(Empresa.id).where(Empresa.organizacion_id == org, Empresa.rif == rif)).first():
        raise ValueError("Ya existe una empresa con ese RIF en su organización")
    e = Empresa(organizacion_id=org, rif=rif)
    aplicar_datos_empresa(e, form, archivos)
    db.session.add(e); db.session.flush()
    for cod, nom, tipo in (("1", "ACTIVO", "Activo"), ("2", "PASIVO", "Pasivo"), ("3", "PATRIMONIO", "Patrimonio"), ("4", "INGRESOS", "Ingreso"), ("5", "GASTOS", "Gasto")):
        db.session.add(Cuenta(empresa_id=e.id, codigo=cod, nombre=nom, tipo=tipo))
    from .auditoria import registrar
    registrar(g.usuario.username, "EMPRESA_CREADA", f"{e.rif} {e.razon_social}"); db.session.commit()
    return e


def portada():
    """Página principal pública de Contax. Los precios salen del catálogo vigente (servidor de licencias); si no responde,
    se muestran los de referencia."""
    from decimal import Decimal
    from .planes import MODULOS, PLANES
    ref = {"CASH": ("25.00", "250.00", "3 usuarios · 1 empresa"), "ERP_ADMIN": ("23.00", "230.00", "5 usuarios · 2 empresas"),
           "ERP_CONTABLE": ("45.00", "450.00", "5 usuarios · 5 empresas"), "ALL_IN_ONE": ("69.00", "690.00", "10 usuarios · 5 empresas")}
    precios = {}
    adm = current_app.extensions.get("admin_licencias")
    try:
        for p in (adm().precios() if adm else []):
            x = precios.setdefault(p["plan"], {})
            x["mensual" if p["meses"] == 1 else "anual" if p["meses"] == 12 else "otro"] = p["monto_usd"]
            x["topes"] = f"{p['max_usuarios']} usuarios · {p['max_empresas']} empresas"
    except Exception:  # noqa: BLE001 — la portada no depende del servidor de licencias
        precios = {}
    fmt = lambda v: f"{Decimal(str(v)):,.0f}".replace(",", ".") if v is not None and Decimal(str(v)) == Decimal(str(v)).to_integral() else (f"{Decimal(str(v)):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") if v else None)
    planes = []
    for k in ("CASH", "ERP_ADMIN", "ERP_CONTABLE", "ALL_IN_ONE"):
        x = precios.get(k) or dict(zip(("mensual", "anual", "topes"), ref[k]))
        planes.append(dict(nombre=PLANES[k]["nombre"].replace(" — Facturación", ""), modulos=", ".join(MODULOS[m] for m in sorted(PLANES[k]["modulos"])),
                           mensual=fmt(x.get("mensual")), anual=fmt(x.get("anual")), topes=x.get("topes") or ref[k][2], destacado=k == "ALL_IN_ONE"))
    return render_template("portada.html", planes=planes)


@bp.route("/", methods=["GET", "POST"])
def inicio():
    if g.usuario is None: return portada()
    if g.usuario.rol == "Maestro": return redirect(url_for("web.plataforma"))
    if request.method == "GET" and g.usuario.rol == "Administrador" and g.licencia:
        from .models import Organizacion
        org = db.session.get(Organizacion, g.usuario.organizacion_id)
        if not org.bienvenida_completada and not mis_empresas():
            return redirect(url_for("web.bienvenida"))   # primera vez con licencia activa: tutorial guiado
    if request.method == "POST":
        if g.usuario.rol != "Administrador": return ("", 403)
        if intentar(lambda: crear_empresa(F, request.files), "Empresa creada (con cuentas raíz). Asigne usuarios en Administración → Usuarios."):
            return redirect(url_for("web.inicio"))
    emps = mis_empresas()
    secs = [dict(titulo="Empresas a su cargo", tarjetas=[dict(titulo=e.razon_social, sub=f"RIF {e.rif}" + (" · Contribuyente especial" if e.es_sujeto_pasivo_especial else ""),
                                                             url=url_empresa(e.id)) for e in emps], vacio="Aún no tiene empresas asignadas.")]
    if g.usuario.rol == "Administrador":
        secs.append(dict(titulo="Nueva empresa", nota=f"Su plan permite hasta {limite_licencia('max_empresas')} empresas. Para una guía paso a paso use el tutorial.",
                         form=dict(action=url_for("web.inicio"), boton="Crear empresa", archivo=True, campos=[
            campo("razon_social", "Razón social"), campo("rif", "RIF (J-12345678-9)"), campo("direccion", "Domicilio fiscal", req=False), campo("telefono", "Teléfono", req=False),
            campo("correo", "Correo", "email", req=False), campo("logo", "Logo (PNG/JPG, máx. 300 KB)", "file", req=False, accept="image/png,image/jpeg,image/webp"),
            campo("spe", "¿Sujeto pasivo especial?", "select", opciones=[("0", "No"), ("1", "Sí")])])))
    return pagina("Panel", None, secs, acciones=[dict(url=url_for("web.bienvenida"), texto="Tutorial de configuración")] if g.usuario.rol == "Administrador" else None)


# ---------------------------------------------------------------- catálogo de cuentas (con importación)
def _guardar_cuentas(eid, cuentas, actualizar):
    """Crea las cuentas nuevas; con `actualizar`, en las existentes cambia nombre/categoría/monetaria (nunca el tipo si tienen movimientos)."""
    existentes = {c.codigo: c for c in db.session.execute(select(Cuenta).where(Cuenta.empresa_id == eid)).scalars()}
    con_mov = set(db.session.execute(select(Movimiento.cuenta_codigo).where(Movimiento.empresa_id == eid).distinct()).scalars())
    nuevas = act = 0
    for c in cuentas:
        x = existentes.get(c["codigo"])
        if x is None:
            db.session.add(Cuenta(empresa_id=eid, **c)); nuevas += 1
        elif actualizar:
            x.nombre, x.categoria_niif18, x.es_partida_monetaria = c["nombre"], c["categoria_niif18"], c["es_partida_monetaria"]
            if c["codigo"] not in con_mov: x.tipo = c["tipo"]
            act += 1
    from .auditoria import registrar
    registrar(g.usuario.username, "PLAN_CUENTAS_IMPORTADO", f"empresa={eid} nuevas={nuevas} actualizadas={act}"); db.session.commit()
    return nuevas, act


@ruta("/e/<int:eid>/cuentas", "asientos.ver", ("GET", "POST"))
def cuentas(eid):
    from .calculos import plan_cuentas as PC
    if request.method == "POST":
        if not tiene_permiso(g.usuario, "cuentas.editar"): return ("", 403)
        a = F.get("accion", "alta")
        if a == "alta":
            def alta():
                cod = F["codigo"].strip()
                if not PC.RE_CODIGO.match(cod): raise ValueError("Código inválido")
                if db.session.get(Cuenta, (eid, cod)): raise ValueError(f"La cuenta {cod} ya existe")
                db.session.add(Cuenta(empresa_id=eid, codigo=cod, nombre=F["nombre"].strip(), tipo=F["tipo"],
                                      categoria_niif18=F["categoria"], es_partida_monetaria=F.get("monetaria") == "1"))
                db.session.commit()
            intentar(alta, "Cuenta creada")
        elif a == "editar":
            def editar():
                c = db.session.get(Cuenta, (eid, F["codigo"]))
                if c is None: raise ValueError("Cuenta inexistente")
                if F.get("nombre", "").strip(): c.nombre = F["nombre"].strip()[:200]
                if F.get("categoria") in estados.CATEGORIAS: c.categoria_niif18 = F["categoria"]
                if F.get("monetaria") in ("0", "1"): c.es_partida_monetaria = F["monetaria"] == "1"
                db.session.commit()
            intentar(editar, "Cuenta actualizada")
        elif a == "eliminar":
            def eliminar():
                c = db.session.get(Cuenta, (eid, F["codigo"]))
                if c is None: raise ValueError("Cuenta inexistente")
                if db.session.execute(select(Movimiento.id).where(Movimiento.empresa_id == eid, Movimiento.cuenta_codigo == c.codigo).limit(1)).first():
                    raise ValueError("La cuenta tiene movimientos: no se puede eliminar")
                cfg = db.session.get(ConfigAsientos, eid)
                if cfg and c.codigo in {getattr(cfg, f"cuenta_{k}") for k in CUENTAS_CFG}: raise ValueError("La cuenta está configurada para asientos automáticos")
                db.session.delete(c); db.session.commit()
            intentar(eliminar, "Cuenta eliminada")
        elif a in ("importar", "modelo"):
            def importar():
                if a == "modelo":
                    filas = PC.leer_filas(PC.MODELO_VEN_NIF)
                else:
                    arch = request.files.get("archivo")
                    datos = arch.read(PC.MAX_FILAS * 300) if arch and arch.filename else b""
                    filas = PC.leer_filas(datos, arch.filename if arch else "") if datos else PC.leer_filas(F.get("texto") or "")
                cs, errores = PC.interpretar(filas)
                if not cs: raise ValueError("No se encontró ninguna cuenta válida. " + " · ".join(errores[:5]))
                n, act = _guardar_cuentas(eid, cs, F.get("modo") == "actualizar")
                for e in errores[:10]: flash(e)
                return n, act, len(errores)
            intentar(importar, lambda r: f"Plan de cuentas importado: {r[0]} cuenta(s) nueva(s), {r[1]} actualizada(s)" + (f", {r[2]} línea(s) con error (ver abajo)" if r[2] else ""))
        return redirect(request.url)
    if request.args.get("plantilla"):
        from flask import Response
        return Response("﻿" + PC.PLANTILLA_CSV, mimetype="text/csv", headers={"Content-Disposition": 'attachment; filename="plantilla_plan_de_cuentas.csv"'})
    cs = db.session.execute(select(Cuenta).where(Cuenta.empresa_id == eid).order_by(Cuenta.codigo)).scalars().all()
    con_mov = set(db.session.execute(select(Movimiento.cuenta_codigo).where(Movimiento.empresa_id == eid).distinct()).scalars())
    nivel = lambda c: c.codigo.count(".") or c.codigo.count("-")  # sangría por nivel (1 · 1.1 · 1.1.01)
    secs = [dict(kpis=[("Cuentas", len(cs)), ("Con movimientos", len(con_mov))] + [(t, sum(c.tipo == t for c in cs)) for t in ("Activo", "Pasivo", "Ingreso", "Gasto")]),
            dict(titulo="Catálogo de cuentas", tabla=dict(cols=["Código", "Nombre", "Tipo", "Categ. NIIF 18", "¿Monetaria?"], filas=[
                dict(cells=[c.codigo, (" " * min(nivel(c), 4)) + c.nombre, c.tipo, c.categoria_niif18, "Sí" if c.es_partida_monetaria else "No"]) for c in cs]))]
    if tiene_permiso(g.usuario, "cuentas.editar"):
        tipos = [(t, t) for t in ("Activo", "Pasivo", "Patrimonio", "Ingreso", "Gasto")]
        cats = [(c, c) for c in estados.CATEGORIAS]
        cods = [(c.codigo, f"{c.codigo} {c.nombre}") for c in cs]
        secs += [
            dict(titulo="¿Ya tiene su plan de cuentas? Impórtelo", nota="Suba el archivo exportado de su sistema anterior (Excel .xlsx, CSV o TXT) o pegue las líneas. "
                      "Se reconocen las columnas código, nombre, tipo, categoría NIIF 18 y monetaria; si falta el tipo se deduce del primer dígito "
                      "(1 Activo, 2 Pasivo, 3 Patrimonio, 4 Ingreso, 5-9 Costos/Gastos). Las cuentas se crean automáticamente; las que ya existen no se duplican.",
                 form=dict(action=request.path, boton="Importar plan de cuentas", archivo=True, campos=[campo("accion", "", "hidden", valor="importar"),
                           campo("archivo", "Archivo (.xlsx, .csv, .txt)", "file", req=False, accept=".xlsx,.csv,.txt"),
                           campo("texto", "…o pegue aquí las cuentas (una por línea: código;nombre;tipo)", "textarea", req=False, filas=6, max=400000,
                                 ph="1.1.01;Caja;Activo\n1.1.02;Bancos;Activo"),
                           campo("modo", "Si la cuenta ya existe", "select", opciones=[("omitir", "Dejarla como está"), ("actualizar", "Actualizar nombre y categoría")])])),
            dict(titulo="¿No tiene uno? Use el plan modelo VEN-NIF", nota="Carga un catálogo PYME de 3 niveles (caja, bancos, IVA crédito/débito, retenciones, IGTF, "
                      "parafiscales, costos y gastos) que luego puede editar. Solo agrega las cuentas que falten.",
                 form=dict(action=request.path, boton="Cargar plan modelo", confirmar="¿Cargar el plan de cuentas modelo VEN-NIF en esta empresa?",
                           campos=[campo("accion", "", "hidden", valor="modelo")])),
            dict(titulo="Nueva cuenta", form=dict(action=request.path, boton="Guardar", campos=[campo("accion", "", "hidden", valor="alta"),
                campo("codigo", "Código"), campo("nombre", "Nombre"), campo("tipo", "Tipo", "select", opciones=tipos),
                campo("categoria", "Categoría NIIF 18 (solo Ingreso/Gasto)", "select", opciones=cats),
                campo("monetaria", "Partida monetaria (Caja/Bancos/CxC/CxP: no se ajusta por inflación)", "select", opciones=[("0", "No"), ("1", "Sí")])])),
            dict(titulo="Modificar cuenta", nota="El código y el tipo no cambian (los asientos dependen de ellos).", form=dict(action=request.path, boton="Actualizar", campos=[
                campo("accion", "", "hidden", valor="editar"), campo("codigo", "Cuenta", "select", opciones=cods), campo("nombre", "Nuevo nombre", req=False),
                campo("categoria", "Categoría NIIF 18", "select", req=False, opciones=[("", "— sin cambio —")] + cats),
                campo("monetaria", "¿Monetaria?", "select", req=False, opciones=[("", "— sin cambio —"), ("0", "No"), ("1", "Sí")])])),
            dict(titulo="Eliminar cuenta", nota="Solo cuentas sin movimientos ni uso en asientos automáticos.", form=dict(action=request.path, boton="Eliminar",
                confirmar="¿Eliminar la cuenta seleccionada?", campos=[campo("accion", "", "hidden", valor="eliminar"),
                                                                      campo("codigo", "Cuenta", "select", opciones=[x for x in cods if x[0] not in con_mov])]))]
    if len(cs) <= 5 and len(secs) > 3:  # empresa recién creada (solo cuentas raíz): lo primero es traer su plan de cuentas
        secs = [secs[0], secs[2], secs[3], secs[1]] + secs[4:]
    return pagina("Catálogo de cuentas", eid, secs, acciones=[dict(url=url_for("web.cuentas", eid=eid, plantilla=1), texto="Descargar plantilla CSV")])


# ---------------------------------------------------------------- asientos contables
@ruta("/e/<int:eid>/asientos", "asientos.ver", ("GET", "POST"))
def asientos(eid):
    if request.method == "POST":
        if not tiene_permiso(g.usuario, "asientos.crear"): return ("", 403)
        lineas = [(F.get(f"c{i}", ""), F.get(f"d{i}") or 0, F.get(f"h{i}") or 0) for i in range(1, LINEAS_ASIENTO + 1) if F.get(f"c{i}", "").strip()]
        if intentar(lambda: contab.crear_asiento(eid, g.usuario, F["fecha"], F["concepto"], lineas), lambda a: f"Asiento N°{a} registrado"):
            return redirect(request.url)
    cods = [(c, f"{c} {n}") for c, n in db.session.execute(select(Cuenta.codigo, Cuenta.nombre).where(Cuenta.empresa_id == eid).order_by(Cuenta.codigo))]
    ult = db.session.execute(select(Asiento).where(Asiento.empresa_id == eid).order_by(Asiento.id.desc()).limit(15)).scalars().all()
    tot = dict(db.session.execute(select(Movimiento.asiento_id, db.func.sum(Movimiento.debe)).where(Movimiento.asiento_id.in_([a.id for a in ult] or [0]))
                                  .group_by(Movimiento.asiento_id)).all())
    out = []
    if tiene_permiso(g.usuario, "asientos.crear"):
        concepto, lineas, nota = "", [], "Agregue cada línea con su cuenta y su monto al debe o al haber. El total del debe debe ser igual al del haber; los períodos cerrados no admiten asientos."
        if request.args.get("apertura"):
            concepto, lineas = "Asiento de apertura de operaciones", _lineas_apertura(eid)
            nota = ("Se cargaron las cuentas típicas de apertura de su catálogo: escriba los saldos iniciales y agregue las que falten."
                    + (" Atención: esta empresa ya tiene asientos; el asiento de apertura solo se usa al comenzar, los saldos pasan solos de un año al siguiente."
                       if ult else ""))
        out.append(dict(titulo="Nuevo asiento", nota=nota, asiento=dict(action=request.path, cuentas=cods, fecha=hoy(), concepto=concepto, lineas=lineas,
                                                                         apertura=url_for("web.asientos", eid=eid, apertura=1))))
    if tiene_permiso(g.usuario, "asientos.anular"):
        out.append(dict(titulo="Anular asiento (contra-asiento)", nota="Los asientos no se borran ni se editan: se anulan con un contra-asiento.",
                        form=dict(action=url_for("web.anular", eid=eid), boton="Anular", campos=[campo("asiento_id", "N° de asiento", "number"),
                                  campo("fecha", "Fecha del contra-asiento", "date", valor=hoy()), campo("motivo", "Motivo")])))
    out.append(dict(titulo="Últimos asientos", tabla=dict(cols=["N°", "Fecha", "Concepto", "Total"], num=[3], filas=[
        dict(cells=[a.id, a.fecha, a.concepto, tot.get(a.id, 0)], url=url_for("web.diario", eid=eid, desde=a.fecha, hasta=a.fecha)) for a in ult])))
    return pagina("Asientos contables", eid, out)


LINEAS_ASIENTO = 40
APERTURA = (("caja",), ("banco",), ("inventario", "mercanc"), ("cuentas por cobrar", "clientes"), ("cuentas por pagar", "proveedores"), ("capital",))


def _lineas_apertura(eid):
    """Como el «asiento de apertura» de IQ_Contable, pero con las cuentas reales de la empresa (la de mayor detalle que
    coincida con caja, bancos, inventario, CxC, CxP y capital) y sin montos inventados: el usuario escribe sus saldos."""
    cuentas = db.session.execute(select(Cuenta.codigo, Cuenta.nombre, Cuenta.tipo).where(Cuenta.empresa_id == eid,
                                 Cuenta.tipo.in_(("Activo", "Pasivo", "Patrimonio"))).order_by(Cuenta.codigo)).all()
    elegidas = []
    for claves in APERTURA:
        hay = [c for c in cuentas if any(k in c.nombre.lower() for k in claves) and c.codigo not in {e[0] for e in elegidas}]
        if hay:
            c = max(hay, key=lambda x: (x.codigo.count("."), len(x.codigo)))   # la cuenta de movimiento, no la de grupo
            elegidas.append((c.codigo, c.nombre, "", ""))
    return elegidas


@ruta("/e/<int:eid>/asientos/anular", "asientos.anular", ("POST",))
def anular(eid):
    intentar(lambda: contab.anular_asiento(eid, g.usuario, int(F["asiento_id"]), F["fecha"], F["motivo"]), lambda a: f"Contra-asiento N°{a} registrado")
    return redirect(url_for("web.asientos", eid=eid))


def _rango(defecto_desde_anio=True):
    hasta = request.args.get("hasta") or hoy()
    desde = request.args.get("desde") or (f"{hasta[:4]}-01-01" if defecto_desde_anio else f"{hasta[:7]}-01")
    try: return date.fromisoformat(desde), date.fromisoformat(hasta)
    except ValueError:
        flash("Fechas inválidas"); return date(hoy_ve().year, 1, 1), hoy_ve()


@ruta("/e/<int:eid>/diario", "asientos.ver")
def diario(eid):
    """Libro diario (Código de Comercio, Art. 32-34): asientos en orden cronológico con sus partidas."""
    d, h = _rango(False)
    rows = db.session.execute(select(Asiento, Movimiento, Cuenta.nombre).join(Movimiento, Movimiento.asiento_id == Asiento.id)
                              .join(Cuenta, (Cuenta.empresa_id == Movimiento.empresa_id) & (Cuenta.codigo == Movimiento.cuenta_codigo))
                              .where(Asiento.empresa_id == eid, Asiento.fecha >= d, Asiento.fecha <= h).order_by(Asiento.fecha, Asiento.id, Movimiento.id)).all()
    filas, previo, td, th = [], None, Decimal(0), Decimal(0)
    for a, m, nom in rows:
        nuevo = a.id != previo
        filas.append(dict(cells=[a.id if nuevo else "", a.fecha if nuevo else "", a.concepto if nuevo else "", m.cuenta_codigo, nom, m.debe or "", m.haber or ""]))
        previo = a.id; td += m.debe; th += m.haber
    filas.append(dict(cells=["", "", "TOTALES", "", "", td, th], clase="total"))
    return pagina("Libro diario", eid, [
        dict(titulo="Período", form=dict(action=request.path, metodo="get", boton="Consultar", campos=[campo("desde", "Desde", "date", valor=d.isoformat()), campo("hasta", "Hasta", "date", valor=h.isoformat())])),
        dict(titulo=f"Libro diario del {d:%d/%m/%Y} al {h:%d/%m/%Y} — {'CUADRA' if td == th else 'NO CUADRA'}",
             tabla=dict(cols=["N°", "Fecha", "Concepto", "Cuenta", "Nombre", "Debe", "Haber"], num=[5, 6], filas=filas))])


# ---------------------------------------------------------------- períodos, cierre y configuración
@ruta("/e/<int:eid>/cierres", "asientos.ver", ("GET", "POST"))
def periodos(eid):
    if request.method == "POST":
        if not tiene_permiso(g.usuario, "periodos.cerrar"): return ("", 403)
        if F.get("accion") == "periodo": intentar(lambda: S.cerrar_periodo(eid, g.usuario, F["mes"]), "Período cerrado")
        else: intentar(lambda: S.ejecutar_cierre(eid, g.usuario, F["fecha"], F["cuenta_patrimonio"]), lambda a: f"Asiento de cierre N°{a} registrado")
        return redirect(request.url)
    cer = db.session.execute(select(PeriodoCerrado.mes_anio).where(PeriodoCerrado.empresa_id == eid).order_by(PeriodoCerrado.mes_anio)).scalars()
    return pagina("Configuración y cierres", eid, [
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


# ---------------------------------------------------------------- libro mayor, balances, resultados, dashboard, divisas
@ruta("/e/<int:eid>/reportes", "reportes.ver")
def reportes(eid):  # enlace anterior: los estados financieros ahora tienen una página cada uno
    return redirect(url_for("web.balance_general", eid=eid, **request.args))


def _corte():
    fecha = request.args.get("fecha") or hoy()
    try: return date.fromisoformat(fecha)
    except ValueError: flash("Fecha inválida"); return hoy_ve()


def _form_corte(f):
    return dict(titulo="Fecha de corte", form=dict(action=request.path, metodo="get", boton="Ver", campos=[campo("fecha", "Al", "date", valor=f.isoformat())]))


@ruta("/e/<int:eid>/mayor", "reportes.ver")
def mayor(eid):
    """Libro mayor analítico por cuenta (Código de Comercio, Arts. 32-35)."""
    d, h = _rango()
    rows = contab.saldos(eid, hasta=h, desde=d)
    cuenta = (request.args.get("cuenta") or "").strip() or next((r["codigo"] for r in rows if r["debe"] or r["haber"]), "")
    cods = [(r["codigo"], f"{r['codigo']} {r['nombre']}") for r in rows]
    sec = [dict(titulo="Filtro", form=dict(action=request.path, metodo="get", boton="Consultar", campos=[campo("cuenta", "Cuenta", "select", opciones=cods, valor=cuenta),
                                                                                                      campo("desde", "Desde", "date", valor=d.isoformat()), campo("hasta", "Hasta", "date", valor=h.isoformat())]))]
    if cuenta:
        ant = next((r for r in contab.saldos(eid, hasta=d - __import__("datetime").timedelta(days=1)) if r["codigo"] == cuenta), None)
        saldo = (ant["debe"] - ant["haber"]) if ant else Decimal(0)
        movs = db.session.execute(select(Asiento.id, Asiento.fecha, Asiento.concepto, Movimiento.debe, Movimiento.haber).join(Movimiento, Movimiento.asiento_id == Asiento.id)
                                  .where(Asiento.empresa_id == eid, Movimiento.cuenta_codigo == cuenta, Asiento.fecha >= d, Asiento.fecha <= h).order_by(Asiento.fecha, Asiento.id)).all()
        filas, td, th = [dict(cells=["", "", "Saldo anterior", "", "", saldo])], Decimal(0), Decimal(0)
        for aid, f, c, de, ha in movs:
            saldo += de - ha; td += de; th += ha; filas.append(dict(cells=[f, aid, c, de, ha, saldo]))
        filas.append(dict(cells=["", "", "TOTALES / SALDO FINAL", td, th, saldo], clase="total"))
        nom = dict(cods).get(cuenta, cuenta)
        sec.append(dict(titulo=f"Mayor de la cuenta {nom}", tabla=dict(cols=["Fecha", "Asiento", "Concepto", "Debe", "Haber", "Saldo"], num=[3, 4, 5], filas=filas)))
    return pagina("Libro mayor", eid, sec)


@ruta("/e/<int:eid>/comprobacion", "reportes.ver")
def comprobacion(eid):
    d, h = _rango()
    rows = contab.saldos(eid, hasta=h, desde=d)
    td, th = sum((r["debe"] for r in rows), Decimal(0)), sum((r["haber"] for r in rows), Decimal(0))
    sd = sum((r["debe"] - r["haber"] for r in rows if r["debe"] > r["haber"]), Decimal(0))
    sa = sum((r["haber"] - r["debe"] for r in rows if r["haber"] > r["debe"]), Decimal(0))
    return pagina("Balance de comprobación", eid, [
        dict(titulo="Período", form=dict(action=request.path, metodo="get", boton="Consultar", campos=[campo("desde", "Desde", "date", valor=d.isoformat()), campo("hasta", "Hasta", "date", valor=h.isoformat())])),
        dict(titulo=f"Sumas y saldos — {'CUADRA' if td == th else 'NO CUADRA'}", tabla=dict(cols=["Cuenta", "Nombre", "Tipo", "Debe", "Haber", "Saldo deudor", "Saldo acreedor"], num=[3, 4, 5, 6], filas=[
            dict(cells=[r["codigo"], r["nombre"], r["tipo"], r["debe"], r["haber"], r["debe"] - r["haber"] if r["debe"] > r["haber"] else "",
                        r["haber"] - r["debe"] if r["haber"] > r["debe"] else ""], url=url_for("web.mayor", eid=eid, cuenta=r["codigo"], desde=d, hasta=h))
            for r in rows if r["debe"] or r["haber"]] + [dict(cells=["", "TOTALES", "", td, th, sd, sa], clase="total")]))])


@ruta("/e/<int:eid>/balance-general", "reportes.ver")
def balance_general(eid):
    f = _corte()
    rows = contab.saldos(eid, hasta=f)
    b = estados.balance(rows)
    cp, ta, td = estados.cambios_patrimonio(rows)
    t = lambda xs: dict(cols=["Cuenta", "Nombre", "Saldo"], num=[2], filas=[dict(cells=list(x)) for x in xs] + [dict(cells=["", "TOTAL", sum((x[2] for x in xs), Decimal(0))], clase="total")])
    sec = [_form_corte(f),
           dict(kpis=[("Total activos", b["total_activos"]), ("Pasivo + patrimonio + resultado", b["total_pasivo_patrimonio"]), ("Resultado del período", b["utilidad"]),
                      ("Ecuación contable", "CUADRA" if b["cuadra"] else "NO CUADRA")]),
           dict(titulo="Activos", tabla=t(b["activos"])), dict(titulo="Pasivos", tabla=t(b["pasivos"])),
           dict(titulo="Patrimonio", tabla=t(b["patrimonio"] + [("", "Resultado del período (no cerrado)", b["utilidad"])])),
           dict(titulo="Estado de cambios en el patrimonio", tabla=dict(cols=["Cuenta", "Nombre", "Aumentos", "Disminuciones", "Saldo"], num=[2, 3, 4],
                                                                         filas=[dict(cells=list(x)) for x in cp] + [dict(cells=["", "TOTALES", ta, td, ta - td], clase="total")]))]
    try:
        fl = S.flujo_efectivo(eid, date(f.year, 1, 1), f)
        sec.append(dict(titulo=f"Flujo de efectivo {f.year} (desde el extracto bancario)", pares=[(k.replace("_", " ").capitalize(), v) for k, v in fl.items()]))
    except ValueError: pass
    return pagina("Balance general", eid, sec)


@ruta("/e/<int:eid>/resultados", "reportes.ver")
def resultados(eid):
    d, h = _rango()
    r = estados.estado_resultados(contab.saldos(eid, hasta=h, desde=d), h.isoformat())
    sec = [dict(titulo="Período", form=dict(action=request.path, metodo="get", boton="Ver", campos=[campo("desde", "Desde", "date", valor=d.isoformat()), campo("hasta", "Hasta", "date", valor=h.isoformat())]))]
    if r["normativa"] == "NIC 1":
        sec += [dict(kpis=[("Ingresos", r["total_ingresos"]), ("Gastos y costos", r["total_gastos"]), ("Utilidad del ejercicio", r["utilidad_del_ejercicio"])]),
                dict(titulo="Ingresos", tabla=dict(cols=["Cuenta", "Nombre", "Monto"], num=[2], filas=[dict(cells=list(x)) for x in r["ingresos"]] + [dict(cells=["", "TOTAL INGRESOS", r["total_ingresos"]], clase="total")])),
                dict(titulo="Costos y gastos", tabla=dict(cols=["Cuenta", "Nombre", "Monto"], num=[2], filas=[dict(cells=list(x)) for x in r["gastos"]] + [dict(cells=["", "TOTAL GASTOS", r["total_gastos"]], clase="total")])),
                dict(nota="Presentación NIC 1 (VEN-NIF). Desde el 01/01/2027 se presenta con las categorías de la NIIF 18.")]
    else:
        sec.append(dict(kpis=[("Resultado operativo", r["resultado_operativo"]), ("Antes de financiamiento e impuestos", r["resultado_antes_financiamiento_impuestos"]),
                              ("Resultado del período", r["resultado_del_periodo"])]))
        for cat, xs in r["categorias"].items():
            if xs: sec.append(dict(titulo=f"Categoría {cat} (NIIF 18)", tabla=dict(cols=["Cuenta", "Nombre", "Tipo", "Monto"], num=[3], filas=[dict(cells=list(x)) for x in xs])))
    return pagina("Estado de resultados", eid, sec)


def _grafico_barras(meses, series, nombres):
    """Barras agrupadas en SVG (sin scripts; tooltip nativo con <title>). Barras finas, extremo superior redondeado 4px,
    2px de separación entre barras, una sola escala. Colores por clase CSS (validados para daltonismo)."""
    ancho, alto, izq, techo, base = 760, 260, 64, 14, 228
    tope = max([v for s in series for v in s] + [Decimal(1)])
    paso = Decimal(10) ** (len(str(int(tope))) - 1)
    tope = ((tope / paso).to_integral_value(rounding="ROUND_CEILING")) * paso
    esc = lambda v: float(base - (base - techo) * float(v) / float(tope))
    n, k = len(meses), len(series)
    grupo = (ancho - 8 - izq) / max(n, 1)
    w = min(18.0, (grupo * 0.7 - 2 * (k - 1)) / k)
    barras, etiquetas = [], []
    for i, mes in enumerate(meses):
        x0 = izq + grupo * i + (grupo - (w * k + 2 * (k - 1))) / 2
        etiquetas.append((round(izq + grupo * i + grupo / 2, 1), mes[5:] + "/" + mes[2:4]))
        for j, s in enumerate(series):
            v = s[i]; x = x0 + j * (w + 2); y = esc(v); r = min(4.0, w / 2, base - y)
            d = (f"M{x:.1f},{base}V{y + r:.1f}Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f}H{x + w - r:.1f}Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f}V{base}Z" if v > 0 else "")
            barras.append(dict(x=round(x, 1), w=round(w, 1), d=d, serie=j + 1, tip=f"{nombres[j]} {mes}: Bs {v:,.2f}"))
    guias = [(round(esc(tope * q / 4), 1), f"{float(tope * q / 4):,.0f}") for q in range(5)]
    return dict(ancho=ancho, alto=alto, izq=izq, techo=techo, base=base, barras=barras, etiquetas=etiquetas, guias=guias, series=nombres,
                titulo=" vs ".join(nombres) + " por mes", nota="Montos en bolívares por mes. Pase el cursor sobre una barra para ver su valor; el detalle está en la tabla de abajo.")


@ruta("/e/<int:eid>/dashboard", "reportes.ver")
def dashboard(eid):
    """Tablero gráfico: indicadores clave y evolución mensual de ingresos y gastos (últimos 12 meses)."""
    hoy_d = hoy_ve()
    b = estados.balance(contab.saldos(eid, hasta=hoy_d))
    ini = date(hoy_d.year - 1 + (hoy_d.month // 12), hoy_d.month % 12 + 1, 1)
    mes = db.func.to_char(Asiento.fecha, "YYYY-MM")
    q = (select(mes, Cuenta.tipo, db.func.sum(Movimiento.debe), db.func.sum(Movimiento.haber))
         .join(Movimiento, Movimiento.asiento_id == Asiento.id)
         .join(Cuenta, (Cuenta.empresa_id == Movimiento.empresa_id) & (Cuenta.codigo == Movimiento.cuenta_codigo))
         .where(Asiento.empresa_id == eid, Asiento.fecha >= ini, Asiento.fecha <= hoy_d, Cuenta.tipo.in_(("Ingreso", "Gasto")), Asiento.concepto.notlike("Cierre de Ejercicio%"))
         .group_by(mes, Cuenta.tipo))
    meses = [f"{(ini.year + (ini.month - 1 + i) // 12):04d}-{(ini.month - 1 + i) % 12 + 1:02d}" for i in range(12)]
    ing, gas = {m: Decimal(0) for m in meses}, {m: Decimal(0) for m in meses}
    for m, tipo, de, ha in db.session.execute(q):
        if m in ing:
            if tipo == "Ingreso": ing[m] += ha - de
            else: gas[m] += de - ha
    s_ing, s_gas = [max(ing[m], Decimal(0)) for m in meses], [max(gas[m], Decimal(0)) for m in meses]
    pasivo = sum((x[2] for x in b["pasivos"]), Decimal(0)); patrimonio = sum((x[2] for x in b["patrimonio"]), Decimal(0)) + b["utilidad"]
    from .models2 import DocumentoCxC, DocumentoCxP
    cxc = db.session.execute(select(db.func.coalesce(db.func.sum(DocumentoCxC.saldo_pendiente), 0)).where(DocumentoCxC.empresa_id == eid)).scalar()
    cxp = db.session.execute(select(db.func.coalesce(db.func.sum(DocumentoCxP.saldo_pendiente), 0)).where(DocumentoCxP.empresa_id == eid)).scalar()
    return pagina("Dashboard gráfico", eid, [
        dict(kpis=[("Activos", b["total_activos"]), ("Pasivos", pasivo), ("Patrimonio", patrimonio), ("Resultado acumulado", b["utilidad"])]),
        dict(kpis=[("Ingresos 12 meses", sum(s_ing)), ("Gastos 12 meses", sum(s_gas)), ("Por cobrar", cxc), ("Por pagar", cxp)]),
        dict(titulo="Ingresos vs gastos — últimos 12 meses", grafico=_grafico_barras(meses, [s_ing, s_gas], ["Ingresos", "Gastos"])),
        dict(titulo="Detalle mensual", tabla=dict(cols=["Mes", "Ingresos", "Gastos", "Resultado"], num=[1, 2, 3], filas=[
            dict(cells=[m, ing[m], gas[m], ing[m] - gas[m]]) for m in meses] + [dict(cells=["TOTAL", sum(ing.values()), sum(gas.values()), sum(ing.values()) - sum(gas.values())], clase="total")]))])


@ruta("/e/<int:eid>/divisas", "asientos.ver")
def divisas(eid):
    """Tasas oficiales del BCV (USD/EUR) y conversor de divisas, como el módulo «Divisas & Moneda» del escritorio."""
    from .models2 import TasaCambio
    from .tasas import tasa_vigente
    hoy_d = hoy_ve()
    usd, eur = tasa_vigente(hoy_d, "USD"), tasa_vigente(hoy_d, "EUR")
    sec = [dict(kpis=[("Tasa BCV USD (Bs)", usd or "—"), ("Tasa BCV EUR (Bs)", eur or "—"), ("Fecha", hoy_d.strftime("%d/%m/%Y"))])]
    monto, de = request.args.get("monto"), request.args.get("de", "USD")
    if monto:
        try:
            m = Decimal(monto.replace(",", ".")); tasas_ = {"USD": usd, "EUR": eur, "USDT": usd, "VES": Decimal(1)}
            if not m.is_finite() or de not in tasas_ or not tasas_[de]: raise ValueError
            bs = (m * Decimal(str(tasas_[de]))).quantize(Decimal("0.01"))
            res = [("Equivalente en bolívares", bs)] + [(f"Equivalente en {x}", (bs / Decimal(str(tasas_[x]))).quantize(Decimal("0.01"))) for x in ("USD", "EUR") if tasas_[x] and x != de]
            sec.append(dict(titulo=f"Conversión de {m} {de}", pares=res))
        except (ArithmeticError, ValueError):
            flash("Monto o moneda inválidos")
    sec += [dict(titulo="Conversor", nota="Usa la tasa oficial del BCV vigente hoy (USDT se valora a la tasa del dólar).",
                 form=dict(action=request.path, metodo="get", boton="Convertir", campos=[campo("monto", "Monto", "number", valor=monto),
                           campo("de", "Moneda", "select", opciones=[("USD", "Dólares (USD)"), ("EUR", "Euros (EUR)"), ("USDT", "USDT"), ("VES", "Bolívares (Bs)")], valor=de)])),
            dict(titulo="Historial de tasas BCV", tabla=dict(cols=["Vigencia", "USD", "EUR", "Fuente"], num=[1, 2], filas=[
                dict(cells=[x.fecha, x.tasa_usd_ves, x.tasa_eur_ves or "—", x.fuente or "Manual"])
                for x in db.session.execute(select(TasaCambio).order_by(TasaCambio.fecha.desc()).limit(60)).scalars()]))]
    return pagina("Divisas y moneda", eid, sec)


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


from . import web_admin, web_compras, web_cuadre, web_empresa, web_facturacion, web_fiscal, web_migracion, web_plataforma, web_presupuestos, web_contratos  # noqa: E402,F401  (registran sus rutas en el mismo blueprint)
