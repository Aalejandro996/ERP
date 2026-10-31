"""Tutorial de bienvenida (primer ingreso con licencia activa) y ficha de la empresa: datos básicos, dirección fiscal,
teléfono, correo, logo, módulos que trabaja la empresa y valores por defecto de sus órdenes de compra."""
import base64

from flask import Response, abort, flash, g, redirect, request, url_for
from sqlalchemy import select

from .auditoria import registrar
from .extensions import db
from .models import Empresa, Organizacion
from .planes import MODULOS
from .security import empresa_autorizada, requiere
from .web import F, aplicar_datos_empresa, bp, campo, crear_empresa, intentar, mis_empresas, pagina, ruta

h = lambda v: campo("accion", "", "hidden", valor=v)
PASOS = ("Datos de la empresa", "Módulos a trabajar", "Órdenes de compra", "¡Listo!")


def logo_data_uri(e) -> str | None:
    return f"data:{e.logo_mime};base64,{base64.b64encode(e.logo).decode()}" if e and e.logo else None


def _campos_datos(e=None, con_rif=True):
    v = lambda k: getattr(e, k) if e is not None else None
    return ([campo("razon_social", "Razón social", valor=v("razon_social"))] + ([campo("rif", "RIF (J-12345678-9)")] if con_rif else []) +
            [campo("direccion", "Dirección fiscal (domicilio fiscal)", valor=v("direccion")), campo("telefono", "Teléfono", valor=v("telefono")),
             campo("correo", "Correo de la empresa", "email", req=False, valor=v("correo")),
             campo("spe", "¿Contribuyente especial (sujeto pasivo especial)?", "select", opciones=[("0", "No"), ("1", "Sí")], valor="1" if v("es_sujeto_pasivo_especial") else "0"),
             campo("logo", "Logo (PNG, JPG o WebP, máx. 300 KB)", "file", req=False, accept="image/png,image/jpeg,image/webp")])


def _campos_modulos(e):
    del_plan = sorted(g.modulos_licencia if g.get("modulos_licencia") else g.modulos)
    actuales = sorted(e.modulos_empresa(del_plan)) if e else del_plan
    return [campo("modulos", "Módulos que se usarán en esta empresa", "checks", req=False, opciones=[(m, MODULOS[m]) for m in del_plan], valor=actuales)]


def _guardar_modulos(e):
    del_plan = g.modulos_licencia if g.get("modulos_licencia") else g.modulos
    elegidos = [m for m in F.getlist("modulos") if m in del_plan]
    if not elegidos: raise ValueError("Elija al menos un módulo")
    e.modulos = None if set(elegidos) == set(del_plan) else ",".join(sorted(elegidos))


def _campos_oc(e):
    return [campo("oc_condicion_pago", "Condición de pago por defecto", valor=e.oc_condicion_pago or "Crédito 15 días"),
            campo("oc_lugar_entrega", "Lugar de entrega por defecto", req=False, valor=e.oc_lugar_entrega or e.direccion),
            campo("oc_numero_inicial", "Primer N° de orden de compra", "number", valor=e.oc_numero_inicial or 1),
            campo("oc_observaciones", "Nota fija al pie de cada orden (opcional)", "textarea", req=False, valor=e.oc_observaciones)]


def _guardar_oc(e):
    e.oc_condicion_pago = (F.get("oc_condicion_pago") or "").strip()[:120] or None
    e.oc_lugar_entrega = (F.get("oc_lugar_entrega") or "").strip()[:300] or None
    e.oc_observaciones = (F.get("oc_observaciones") or "").strip()[:1000] or None
    n = int(F.get("oc_numero_inicial") or 1)
    if not 1 <= n <= 99_999_999: raise ValueError("Número inicial inválido")
    e.oc_numero_inicial = n


@bp.route("/bienvenida", methods=["GET", "POST"])
@requiere("admin")
def bienvenida():
    """Tutorial del primer ingreso: 1) crear la empresa (datos, dirección fiscal, teléfono, logo) 2) elegir sus módulos
    3) configurar las órdenes de compra 4) próximos pasos (plan de cuentas, migración, facturación, WhatsApp, usuarios)."""
    if g.usuario.rol != "Administrador" or not g.licencia: abort(403)
    org = db.session.get(Organizacion, g.usuario.organizacion_id)
    paso = request.values.get("paso", "1")
    eid = request.values.get("eid", "")
    e = None
    if eid.isdigit():
        empresa_autorizada(int(eid)); e = db.session.get(Empresa, int(eid))
    elif paso != "1" or mis_empresas():
        e = (mis_empresas() or [None])[0]
    if request.method == "POST":
        a = F.get("accion")
        if a == "omitir":
            org.bienvenida_completada = True; registrar(g.usuario.username, "TUTORIAL_OMITIDO", ""); db.session.commit()
            return redirect(url_for("web.inicio"))
        res = {}
        def hacer():
            if a == "empresa":
                if e is None: res["e"] = crear_empresa(F, request.files)
                else: aplicar_datos_empresa(e, F, request.files); db.session.commit(); res["e"] = e
            elif a == "modulos": _guardar_modulos(e); db.session.commit(); res["e"] = e
            elif a == "oc":
                _guardar_oc(e); org.bienvenida_completada = True
                registrar(g.usuario.username, "TUTORIAL_COMPLETADO", e.rif); db.session.commit(); res["e"] = e
            else: raise ValueError("Acción inválida")
        siguiente = {"empresa": "2", "modulos": "3", "oc": "4"}.get(a, paso)
        if intentar(hacer, {"empresa": "Empresa guardada", "modulos": "Módulos guardados", "oc": "Órdenes de compra configuradas"}.get(a, "Hecho")):
            return redirect(url_for("web.bienvenida", paso=siguiente, eid=res["e"].id))
        return redirect(request.url)
    n = int(paso) if paso in ("1", "2", "3", "4") else 1
    if n > 1 and e is None: return redirect(url_for("web.bienvenida", paso=1))
    pasos = dict(pasos=[(t, "hecho" if i + 1 < n else ("actual" if i + 1 == n else "")) for i, t in enumerate(PASOS)])
    secs = [pasos]
    if n == 1:
        secs.append(dict(titulo="Paso 1 · ¿Con qué empresa va a trabajar?", abierto=True,
                         nota="Estos datos salen en sus facturas y órdenes de compra: razón social, RIF, dirección fiscal y teléfono. Agregue su logo para personalizar los documentos.",
                         form=dict(action=url_for("web.bienvenida", paso=1, eid=e.id if e else None), boton="Guardar y continuar", archivo=True,
                                   campos=[h("empresa")] + _campos_datos(e, con_rif=e is None))))
        if e is not None and e.logo: secs.insert(1, dict(logo=logo_data_uri(e)))
    elif n == 2:
        secs.append(dict(titulo=f"Paso 2 · Módulos que usará {e.razon_social}", abierto=True,
                         nota="Su plan incluye estos módulos. Desmarque los que esta empresa no usará: el menú se simplifica y nadie podrá entrar a ellos en esta empresa. Puede cambiarlo cuando quiera.",
                         form=dict(action=url_for("web.bienvenida", paso=2, eid=e.id), boton="Guardar y continuar", campos=[h("modulos")] + _campos_modulos(e))))
    elif n == 3:
        secs.append(dict(titulo="Paso 3 · Órdenes de compra", abierto=True,
                         nota="Valores que se proponen en cada orden de compra a sus proveedores (los puede cambiar en cada orden). La orden lleva su logo, RIF y dirección fiscal.",
                         form=dict(action=url_for("web.bienvenida", paso=3, eid=e.id), boton="Guardar y terminar", campos=[h("oc")] + _campos_oc(e))))
    else:
        mods = e.modulos_empresa(g.modulos)
        t = lambda titulo, sub, ep, mod=None: dict(titulo=titulo, sub=sub, url=url_for(ep, eid=e.id)) if (mod is None or mod in mods) else None
        tarjetas = [x for x in (
            t("📝 Plan de cuentas", "Importe el que ya tiene (Excel/CSV) o cargue el modelo VEN-NIF", "web.cuentas", "contabilidad"),
            t("📥 Migrar datos", "Clientes, proveedores, productos y saldos desde Galac, Profit Plus o Excel", "web.migracion"),
            t("🔢 Facturación", "Registre su serie y el rango de números de control de su imprenta", "web.series", "facturacion"),
            t("🤖 Bot de WhatsApp", "Conecte el WhatsApp de la empresa para recibir pedidos", "web.whatsapp_config", "facturacion" if "facturacion" in mods else "inventario"),
            t("🏢 Datos de la empresa", "Logo, dirección, módulos y órdenes de compra", "web.empresa_datos")) if x]
        tarjetas.append(dict(titulo="👥 Invitar a su equipo", sub="Contador, auxiliar, cajero o auditor, cada uno con su clave y MFA", url=url_for("web.admin_usuarios")))
        secs.append(dict(titulo=f"¡{e.razon_social} está lista! Próximos pasos recomendados", tarjetas=tarjetas))
    if n < 4:
        secs.append(dict(nota="¿Prefiere hacerlo después? Puede volver al tutorial desde el Panel.",
                         form=dict(action=url_for("web.bienvenida"), boton="Omitir tutorial", campos=[h("omitir")])))
    return pagina("Bienvenido(a): configuremos su empresa", None, secs)


@ruta("/e/<int:eid>/empresa", "admin", ("GET", "POST"))
def empresa_datos(eid):
    e = db.session.get(Empresa, eid)
    if request.method == "POST":
        a = F.get("accion")
        def hacer():
            if a == "datos": aplicar_datos_empresa(e, F, request.files)
            elif a == "modulos": _guardar_modulos(e)
            elif a == "oc": _guardar_oc(e)
            else: raise ValueError("Acción inválida")
            registrar(g.usuario.username, "EMPRESA_" + a.upper(), e.rif); db.session.commit()
        intentar(hacer, "Guardado"); return redirect(request.url)
    secs = []
    if e.logo: secs.append(dict(logo=logo_data_uri(e)))
    secs += [dict(pares=[("Razón social", e.razon_social), ("RIF", e.rif), ("Dirección fiscal", e.direccion or "—"), ("Teléfono", e.telefono or "—"),
                         ("Módulos activos", ", ".join(MODULOS[m] for m in sorted(g.modulos)) or "—")]),
             dict(titulo="Datos básicos y logo", form=dict(action=request.path, boton="Guardar", archivo=True, campos=[h("datos")] + _campos_datos(e, con_rif=False)
                  + ([campo("quitar_logo", "¿Quitar el logo actual?", "select", opciones=[("0", "No"), ("1", "Sí")])] if e.logo else []))),
             dict(titulo="Módulos de esta empresa", form=dict(action=request.path, boton="Guardar", campos=[h("modulos")] + _campos_modulos(e))),
             dict(titulo="Valores de las órdenes de compra", form=dict(action=request.path, boton="Guardar", campos=[h("oc")] + _campos_oc(e)))]
    return pagina("Datos de la empresa", eid, secs)


@bp.route("/e/<int:eid>/logo")
def empresa_logo(eid):
    if g.usuario is None: abort(401)
    empresa_autorizada(eid)
    e = db.session.get(Empresa, eid)
    if not e.logo: abort(404)
    return Response(e.logo, mimetype=e.logo_mime, headers={"Cache-Control": "private, max-age=300"})
