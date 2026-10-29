"""Administración de la ORGANIZACIÓN (Administrador del cliente): usuarios por invitación, asignación de empresas,
políticas propias (provisión de incobrables) y licencia. Nada aquí ve ni toca datos de otra organización."""
import secrets
from datetime import datetime, timezone

from flask import current_app, flash, g, redirect, request, url_for
from sqlalchemy import func, select

from . import correo
from .auditoria import registrar
from .auth.routes import enviar_invitacion
from .extensions import db
from .models import ROLES_CLIENTE, Empresa, Equipo, Organizacion, Usuario, UsuarioEmpresa, _ahora
from .models2 import TramoProvision
from .planes import MODULOS, PLANES, modulos_de, nombre_plan
from .security import hashear
from .web import F, campo, intentar, pagina, ruta

h = lambda v: campo("accion", "", "hidden", valor=v)


def _usuario_de_mi_org(uid) -> Usuario:
    u = db.session.get(Usuario, uid)
    if u is None or u.organizacion_id != g.usuario.organizacion_id: raise ValueError("Usuario inexistente")
    return u


def _empresa_de_mi_org(eid) -> Empresa:
    e = db.session.get(Empresa, eid)
    if e is None or e.organizacion_id != g.usuario.organizacion_id: raise ValueError("Empresa inexistente")
    return e


@ruta("/admin/usuarios", "admin", ("GET", "POST"))
def admin_usuarios():
    org_id = g.usuario.organizacion_id
    org = db.session.get(Organizacion, org_id)
    if request.method == "POST":
        a = F["accion"]
        def crear():
            if F["rol"] not in ROLES_CLIENTE: raise ValueError("Rol inválido")
            activos = db.session.execute(select(func.count()).select_from(Usuario).where(Usuario.organizacion_id == org_id, Usuario.activo.is_(True))).scalar()
            tope = int((g.licencia or {}).get("max_usuarios") or 0)
            if activos >= tope: raise ValueError(f"Su licencia permite {tope} usuario(s) activo(s)")
            email, nombre = F["email"].strip().lower(), F["username"].strip()
            if "@" not in email: raise ValueError("Correo inválido")
            if db.session.execute(select(Usuario.id).where((Usuario.username == nombre) | (Usuario.email == email))).first():
                raise ValueError("El usuario o el correo no están disponibles")
            u = Usuario(organizacion_id=org_id, username=nombre, email=email, email_verificado=False, rol=F["rol"],
                        password_hash=hashear(secrets.token_urlsafe(32)), debe_cambiar_clave=False)  # clave inutilizable hasta aceptar la invitación
            db.session.add(u); db.session.flush()
            enviar_invitacion(u, org.nombre, g.usuario.username)
            registrar(g.usuario.username, "USUARIO_INVITADO", f"{nombre} rol={F['rol']}"); db.session.commit()
        def estado():
            u = _usuario_de_mi_org(int(F["uid"]))
            if u.id == g.usuario.id: raise ValueError("No puede desactivarse a sí mismo")
            u.activo = not u.activo; u.sesion_version += 1
            registrar(g.usuario.username, "USUARIO_ACTIVO" if u.activo else "USUARIO_DESACTIVADO", u.username); db.session.commit()
        def asignar():
            u, e = _usuario_de_mi_org(int(F["uid"])), _empresa_de_mi_org(int(F["eid"]))
            ex = db.session.get(UsuarioEmpresa, (u.id, e.id))
            if a == "asignar" and ex is None: db.session.add(UsuarioEmpresa(usuario_id=u.id, empresa_id=e.id))
            if a == "quitar" and ex is not None: db.session.delete(ex)
            registrar(g.usuario.username, "EMPRESA_" + a.upper(), f"usuario={u.username} empresa={e.rif}"); db.session.commit()
        def reset():
            u = _usuario_de_mi_org(int(F["uid"]))
            if a == "reset_mfa":
                u.mfa_activo, u.mfa_secreto_cifrado, u.mfa_ultimo_contador = False, None, 0
                u.sesion_version += 1
                if u.email_verificado: correo.enviar_seguro(u.email, "MFA restablecido", f"El administrador restableció la verificación en dos pasos de «{u.username}». Deberá configurarla al entrar.")
            elif a == "reinvitar":
                if u.email_verificado and u.mfa_activo: raise ValueError("El usuario ya activó su cuenta; use «Enviar recuperación»")
                enviar_invitacion(u, org.nombre, g.usuario.username)
            elif a == "recuperar":
                if not u.email_verificado: raise ValueError("El usuario no tiene correo verificado; reenvíe la invitación")
                from .auth.routes import enviar_recuperacion
                enviar_recuperacion(u, "El administrador de su organización solicitó restablecer la contraseña de su cuenta.")
            registrar(g.usuario.username, a.upper(), u.username); db.session.commit()
        acciones = {"crear": crear, "estado": estado, "asignar": asignar, "quitar": asignar, "reset_mfa": reset, "reinvitar": reset, "recuperar": reset}
        if a not in acciones: flash("Acción inválida"); return redirect(request.url)
        intentar(acciones[a], "Hecho"); return redirect(request.url)
    us = db.session.execute(select(Usuario).where(Usuario.organizacion_id == org_id).order_by(Usuario.username)).scalars().all()
    asign = {}
    for ue, e in db.session.execute(select(UsuarioEmpresa, Empresa).join(Empresa, Empresa.id == UsuarioEmpresa.empresa_id).where(Empresa.organizacion_id == org_id)):
        asign.setdefault(ue.usuario_id, []).append(e.razon_social)
    emps = [(e.id, e.razon_social) for e in db.session.execute(select(Empresa).where(Empresa.organizacion_id == org_id).order_by(Empresa.razon_social)).scalars()]
    sel = lambda: campo("uid", "Usuario", "select", opciones=[(u.id, u.username) for u in us])
    estado_u = lambda u: "Activo" if u.activo and u.email_verificado and u.mfa_activo else ("Desactivado" if not u.activo else ("Invitado" if not u.email_verificado else "Sin MFA"))
    return pagina("Usuarios", None, [
        dict(tabla=dict(cols=["Usuario", "Correo", "Rol", "Estado", "Empresas asignadas", "Último acceso"], filas=[
            dict(cells=[u.username, u.email or "—", u.rol, estado_u(u), ", ".join(asign.get(u.id, [])) or ("Todas" if u.rol == "Administrador" else "—"),
                        u.ultimo_login.strftime("%Y-%m-%d %H:%M") if u.ultimo_login else "—"]) for u in us])),
        dict(titulo="Invitar usuario", nota="Se envía un enlace al correo (72 h) para que el usuario cree su propia contraseña; luego deberá activar MFA. Nadie más conoce su clave.",
             form=dict(action=request.path, boton="Enviar invitación", campos=[h("crear"), campo("username", "Usuario"), campo("email", "Correo", "email"),
                                                                              campo("rol", "Rol", "select", opciones=[(r, r) for r in ROLES_CLIENTE])])),
        dict(titulo="Asignar empresa", form=dict(action=request.path, boton="Asignar", campos=[h("asignar"), sel(), campo("eid", "Empresa", "select", opciones=emps)])),
        dict(titulo="Quitar empresa", form=dict(action=request.path, boton="Quitar", campos=[h("quitar"), sel(), campo("eid", "Empresa", "select", opciones=emps)])),
        dict(titulo="Activar / desactivar", nota="Desactivar cierra de inmediato todas sus sesiones.", form=dict(action=request.path, boton="Cambiar estado", campos=[h("estado"), sel()])),
        dict(titulo="Reenviar invitación", form=dict(action=request.path, boton="Reenviar", campos=[h("reinvitar"), sel()])),
        dict(titulo="Enviar enlace de recuperación de contraseña", form=dict(action=request.path, boton="Enviar", campos=[h("recuperar"), sel()])),
        dict(titulo="Restablecer MFA", nota="Quita el segundo factor; el usuario deberá configurarlo de nuevo.", form=dict(action=request.path, boton="Restablecer MFA", campos=[h("reset_mfa"), sel()]))])


@ruta("/admin/organizacion", "admin", ("GET", "POST"))
def admin_organizacion():
    """Políticas propias de la organización: cada cliente trabaja a su manera sin afectar a los demás."""
    org_id = g.usuario.organizacion_id
    if request.method == "POST":
        def guardar():
            if F["accion"] == "tramo":
                db.session.add(TramoProvision(organizacion_id=org_id, dias_desde=int(F["desde"]), dias_hasta=int(F["hasta"]), porcentaje=F["pct"]))
            elif F["accion"] == "borrar_tramo":
                t = db.session.get(TramoProvision, int(F["tid"]))
                if t is None or t.organizacion_id != org_id: raise ValueError("Tramo inexistente")
                db.session.delete(t)
            else: raise ValueError("Acción inválida")
            registrar(g.usuario.username, "POLITICA_" + F["accion"].upper(), F.get("pct", "")); db.session.commit()
        intentar(guardar, "Guardado"); return redirect(request.url)
    org = db.session.get(Organizacion, org_id)
    tr = db.session.execute(select(TramoProvision).where(TramoProvision.organizacion_id == org_id).order_by(TramoProvision.dias_desde)).scalars().all()
    return pagina("Mi organización", None, [
        dict(pares=[("Organización", org.nombre), ("RIF", org.rif), ("Plan", nombre_plan(g.licencia)), ("Módulos", ", ".join(MODULOS[m] for m in sorted(g.modulos)) or "—")]),
        dict(titulo="Tramos de provisión de incobrables", nota="Política contable propia (VEN-NIF / NIIF 9): días de vencimiento → % de provisión.",
             tabla=dict(cols=["Desde (días)", "Hasta (días)", "%"], num=[0, 1, 2], accion=True, filas=[dict(cells=[t.dias_desde, t.dias_hasta, t.porcentaje],
                        form=dict(action=request.path, boton="Eliminar", campos=[h("borrar_tramo"), campo("tid", "", "hidden", valor=t.id)])) for t in tr])),
        dict(titulo="Nuevo tramo", form=dict(action=request.path, boton="Agregar", campos=[h("tramo"), campo("desde", "Desde días", "number"), campo("hasta", "Hasta días", "number"), campo("pct", "%", "number")]))])


@ruta("/admin/equipos", "admin", ("GET", "POST"))
def admin_equipos():
    """Equipos (navegadores) autorizados. Revocar uno cierra al instante las sesiones abiertas en él."""
    org_id = g.usuario.organizacion_id
    if request.method == "POST":
        def revocar():
            e = db.session.get(Equipo, int(F["qid"]))
            if e is None or e.organizacion_id != org_id: raise ValueError("Equipo inexistente")
            e.revocado_en = _ahora()
            registrar(g.usuario.username, "EQUIPO_REVOCADO", f"equipo={e.id}"); db.session.commit()
        intentar(revocar, "Equipo revocado"); return redirect(request.url)
    eqs = db.session.execute(select(Equipo).where(Equipo.organizacion_id == org_id).order_by(Equipo.revocado_en.is_not(None), Equipo.ultimo_uso.desc())).scalars().all()
    tope = (g.licencia or {}).get("max_equipos")
    activos = sum(e.revocado_en is None for e in eqs)
    return pagina("Equipos autorizados", None, [
        dict(kpis=[("Equipos en uso", activos), ("Permitidos por su licencia", tope or "Sin límite")]),
        dict(nota="Cada navegador desde el que se entra a su organización cuenta como un equipo. Si llega al tope, revoque los que ya no use.",
             tabla=dict(cols=["#", "Equipo / navegador", "IP", "Autorizado", "Último uso", "Estado"], accion=True, filas=[
                 dict(cells=[e.id, e.nombre[:70], e.ip or "", e.creado_en.strftime("%Y-%m-%d"), e.ultimo_uso.strftime("%Y-%m-%d %H:%M"), "Revocado" if e.revocado_en else "Activo"],
                      form=dict(action=request.path, boton="Revocar", campos=[campo("qid", "", "hidden", valor=e.id)]) if e.revocado_en is None else None) for e in eqs]))])


def _orden_de_mi_org(adm, ref):
    org = str(g.usuario.organizacion_id)
    o = next((x for x in adm.ordenes(org=org) if x["ref"] == ref), None)
    if o is None: raise ValueError("Orden no encontrada")
    return o


@ruta("/admin/licencia", "admin", ("GET", "POST"))
def admin_licencia():
    from .licensing.admin import ErrorAdmin
    from . import tasas as SFT
    from .tiempo import hoy_ve
    gestor = current_app.extensions["gestor_licencia"](g.usuario.organizacion_id)
    adm_f = current_app.extensions.get("admin_licencias")
    org = db.session.get(Organizacion, g.usuario.organizacion_id)
    if request.method == "POST":
        a = F["accion"]
        if a in ("activar", "renovar"):
            try:
                est = gestor.activar(F["clave"]) if a == "activar" else gestor.renovar()
            except (OSError, ValueError) as e:
                est = None; flash(f"No se pudo contactar al servidor de licencias: {e}")
            if est is not None and est.valida and str(est.claims.get("org", org.id)) != str(org.id):
                gestor.almacen.guardar({}); est.valida, est.motivo = False, "Esta clave pertenece a otra organización"
            if est is not None:
                registrar(g.usuario.username, "LICENCIA_" + a.upper(), "ok" if est.valida else est.motivo); db.session.commit()
                flash("Licencia validada correctamente" if est.valida else est.motivo)
            return redirect(url_for("web.admin_licencia"))
        try:
            if adm_f is None: raise ValueError("La compra en línea no está habilitada; contacte a Gigadevs.")
            adm = adm_f()
            if a == "comprar":
                moneda = F["moneda"]
                tasa = SFT.tasa_vigente(hoy_ve(), "USD") if moneda == "VES" else None
                if moneda == "VES" and not tasa: raise ValueError("No hay tasa BCV cargada; intente más tarde.")
                plan, meses = F["oferta"].split(":", 1)
                tasa = tasa or SFT.tasa_vigente(hoy_ve(), "USD")  # también en USDT: la factura lleva el equivalente en Bs
                o = adm.crear_orden(organizacion_ref=str(org.id), cliente=org.nombre, correo=g.usuario.email, plan=plan, meses=int(meses), moneda=moneda,
                                    tasa_bcv=str(tasa) if tasa else None, cliente_rif=org.rif)
                registrar(g.usuario.username, "ORDEN_LICENCIA", f"{o['ref']} {plan} {meses}m {o['monto']} {moneda}"); db.session.commit()
                return redirect(url_for("web.admin_licencia", orden=o["ref"]))
            if a == "verificar":
                _orden_de_mi_org(adm, F["ref"])  # solo órdenes de SU organización
                r = adm.verificar(F["ref"])
                flash("Pago confirmado: la clave de licencia va camino a su correo." if r.get("estado") == "entregada"
                      else ("La orden venció sin pago." if r.get("estado") == "vencida" else "Aún no vemos el pago en Binance. Si lo acaba de enviar, espere unos minutos."))
                return redirect(url_for("web.admin_licencia", orden=F["ref"]))
            if a == "informar":
                adm.informar_pago(F["ref"], organizacion_ref=str(org.id), referencia=F["referencia"].strip(), banco=F.get("banco", ""), telefono=F.get("telefono", ""))
                registrar(g.usuario.username, "PAGO_INFORMADO", F["ref"]); db.session.commit()
                flash("Pago informado. Cuando se verifique recibirá la licencia por correo.")
                return redirect(url_for("web.admin_licencia", orden=F["ref"]))
            raise ValueError("Acción inválida")
        except (ErrorAdmin, ValueError, OSError) as e:
            db.session.rollback(); flash(str(e)); return redirect(url_for("web.admin_licencia"))
    est = gestor.estado()
    pares = [("Estado", "VÁLIDA" if est.valida else f"NO VÁLIDA — {est.motivo}")]
    if est.claims:
        mods = modulos_de(est.claims)
        pares += [("Titular", est.claims["cliente"]), ("Plan", nombre_plan(est.claims)), ("Módulos incluidos", ", ".join(MODULOS[m] for m in sorted(mods))),
                  ("Máx. usuarios activos", est.claims["max_usuarios"]), ("Máx. empresas", est.claims["max_empresas"]),
                  ("Equipos permitidos", est.claims.get("max_equipos") or "Sin límite"),
                  ("Licencia vigente hasta", datetime.fromtimestamp(est.claims.get("vence_licencia", est.claims["exp"]), timezone.utc).strftime("%Y-%m-%d"))]
    secs = [dict(pares=pares)]
    # ---- muro de pago
    if adm_f is not None:
        try:
            adm = adm_f(); precios, ordenes = adm.precios(), adm.ordenes(org=str(org.id))
        except (ErrorAdmin, ValueError, OSError) as e:
            precios, ordenes = [], []; flash(f"Tienda de licencias no disponible: {e}")
        ref = request.args.get("orden", "")
        o = next((x for x in ordenes if x["ref"] == ref), None)
        if o:
            if o["estado"] == "entregada":
                secs.append(dict(titulo=f"Orden {o['ref']}: pagada", nota=f"La clave de licencia se envió a {g.usuario.email}. Péguela abajo en «Activar licencia»."))
            elif o["proveedor"] == "binance":
                exacto = o.get("monto_esperado") or o["monto"]
                if o["estado"] == "vencida":
                    secs.append(dict(titulo=f"Orden {o['ref']}: vencida", nota="No se recibió el pago a tiempo. Si ya pagó, contacte a soporte con el ID de la transacción; si no, cree una orden nueva."))
                else:
                    secs.append(dict(titulo=f"Orden {o['ref']}: pagar {exacto} USDT", pares=[("Monto EXACTO a enviar", f"{exacto} USDT"), ("Estado", o["estado"].replace("_", " "))],
                                     nota=(o.get("instrucciones") or "") + " La verificación es automática (cada pocos minutos): al confirmarse, la clave llega a "
                                          f"{g.usuario.email} una sola vez.",
                                     form=dict(action=request.path, boton="Ya pagué: verificar ahora", campos=[campo("accion", "", "hidden", valor="verificar"), campo("ref", "", "hidden", valor=o["ref"])])))
            else:
                secs.append(dict(titulo=f"Orden {o['ref']}: Bs {o['monto']}", nota=(current_app.config.get("PAGO_MOVIL_INSTRUCCIONES") or "")
                                 + " Haga el pago móvil por el monto exacto e informe la referencia. Un operador lo verificará y recibirá la clave por correo.",
                                 form=dict(action=request.path, boton="Informar pago", campos=[campo("accion", "", "hidden", valor="informar"), campo("ref", "", "hidden", valor=o["ref"]),
                                           campo("referencia", "N° de referencia"), campo("banco", "Banco de origen", req=False), campo("telefono", "Teléfono de origen", req=False)])
                                 if o["estado"] == "pendiente" else None))
        if precios:
            ofertas = [(f"{p['plan']}:{p['meses']}", f"{PLANES[p['plan']]['nombre']} · {p['meses']} mes(es) · {p['monto_usd']} USD · {p['max_usuarios']} usuarios, "
                                                    f"{p['max_empresas']} empresas, {p['max_equipos']} equipos") for p in precios]
            secs.append(dict(titulo="Comprar o renovar licencia", nota="USDT por Binance (verificación automática) o bolívares por pago móvil a la tasa BCV del día. La clave llega por correo una sola vez.",
                             form=dict(action=request.path, boton="Continuar al pago", campos=[campo("accion", "", "hidden", valor="comprar"), campo("oferta", "Plan", "select", opciones=ofertas),
                                       campo("moneda", "Pagar en", "select", opciones=[("USDT", "USDT (Binance)"), ("VES", "Bolívares (pago móvil)")])])))
        if ordenes:
            secs.append(dict(titulo="Mis órdenes", tabla=dict(cols=["Orden", "Plan", "Meses", "Moneda", "Monto", "Estado"], num=[4], filas=[
                dict(cells=[x["ref"], x["plan"], x["meses"], x["moneda"], x["monto"], x["estado"].replace("_", " ")], url=url_for("web.admin_licencia", orden=x["ref"])) for x in ordenes])))
    planes = [dict(cells=[p["nombre"], ", ".join(MODULOS[m] for m in sorted(p["modulos"]))]) for p in PLANES.values()]
    secs += [dict(titulo="Activar licencia", nota="Pegue la clave que recibió por correo. Cada clave se vincula a su organización.",
                  form=dict(action=request.path, boton="Activar", campos=[h("activar"), campo("clave", "Clave de licencia")])),
             dict(titulo="Renovar ahora", nota="Trae de inmediato los cambios de plan o de topes hechos por el proveedor.", form=dict(action=request.path, boton="Renovar", campos=[h("renovar")])),
             dict(titulo="Planes disponibles", tabla=dict(cols=["Plan", "Módulos"], filas=planes))]
    return pagina("Licencia", None, secs)
