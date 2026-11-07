"""Administración de la ORGANIZACIÓN (Administrador del cliente): usuarios por invitación, asignación de empresas,
políticas propias (provisión de incobrables) y licencia. Nada aquí ve ni toca datos de otra organización."""
import secrets
from datetime import datetime, timezone
from decimal import Decimal

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


def _modulos_elegidos(elegidos, del_plan) -> str | None:
    """Módulos marcados → texto para la BD. Todos los del plan = NULL (también tendrá los que el plan sume después)."""
    m = sorted({x for x in elegidos if x in del_plan})
    if not m: raise ValueError("Elija al menos un módulo")
    return None if set(m) >= set(del_plan) else ",".join(m)


@ruta("/admin/usuarios", "admin", ("GET", "POST"))
def admin_usuarios():
    from .security import PUESTOS, modulos_usuario
    org_id = g.usuario.organizacion_id
    org = db.session.get(Organizacion, org_id)
    del_plan = [m for m in MODULOS if m in g.modulos] or list(MODULOS)
    emps_org = db.session.execute(select(Empresa).where(Empresa.organizacion_id == org_id).order_by(Empresa.razon_social)).scalars().all()
    if request.method == "POST":
        a = F["accion"]
        def guardar():
            """Invita a un usuario nuevo o, si ya existe en la organización, lo mantiene y actualiza su puesto, módulos y empresas."""
            rol = F["rol"]
            if rol not in ROLES_CLIENTE: raise ValueError("Puesto inválido")
            email, nombre = (F.get("email") or "").strip().lower(), (F.get("username") or "").strip()
            if not nombre or "@" not in email: raise ValueError("Indique el usuario y un correo válido")
            # Sin la lista de módulos (formularios anteriores o API) se conservan todos los del plan
            modulos = None if rol == "Administrador" or not F.get("con_modulos") else _modulos_elegidos(F.getlist("modulos"), del_plan)
            elegidas = {int(x) for x in F.getlist("empresas") if x.isdigit()}
            if not elegidas <= {e.id for e in emps_org}: raise ValueError("Empresa inexistente")
            por_nombre = db.session.execute(select(Usuario).where(Usuario.username == nombre)).scalar_one_or_none()
            por_correo = db.session.execute(select(Usuario).where(Usuario.email == email)).scalar_one_or_none()
            if por_nombre and por_correo and por_nombre.id != por_correo.id: raise ValueError("Ese usuario y ese correo pertenecen a cuentas distintas")
            u = por_nombre or por_correo
            if u is not None and u.organizacion_id != org_id: raise ValueError("El usuario o el correo no están disponibles")
            if u is None:
                activos = db.session.execute(select(func.count()).select_from(Usuario).where(Usuario.organizacion_id == org_id, Usuario.activo.is_(True))).scalar()
                tope = int((g.licencia or {}).get("max_usuarios") or 0)
                if activos >= tope: raise ValueError(f"Su licencia permite {tope} usuario(s) activo(s)")
                u = Usuario(organizacion_id=org_id, username=nombre, email=email, email_verificado=False, rol=rol, modulos=modulos,
                            password_hash=hashear(secrets.token_urlsafe(32)), debe_cambiar_clave=False)  # clave inutilizable hasta aceptar la invitación
                db.session.add(u); db.session.flush()
                enviar_invitacion(u, org.nombre, g.usuario.username); nuevo = True
            else:
                if u.id == g.usuario.id and rol != "Administrador": raise ValueError("No puede quitarse a sí mismo el puesto de Administrador")
                if u.rol != rol or (u.modulos or None) != modulos: u.sesion_version += 1   # los permisos nuevos rigen de inmediato
                u.rol, u.modulos = rol, modulos
                if not u.email_verificado:
                    if (u.email or "") != email: u.email = email
                    enviar_invitacion(u, org.nombre, g.usuario.username)
                nuevo = False
            for e in emps_org:   # empresas asignadas = las marcadas
                ex = db.session.get(UsuarioEmpresa, (u.id, e.id))
                if e.id in elegidas and ex is None: db.session.add(UsuarioEmpresa(usuario_id=u.id, empresa_id=e.id))
                if e.id not in elegidas and ex is not None: db.session.delete(ex)
            registrar(g.usuario.username, "USUARIO_INVITADO" if nuevo else "USUARIO_ACTUALIZADO",
                      f"{u.username} rol={rol} modulos={modulos or 'todos'} empresas={sorted(elegidas)}"); db.session.commit()
            return (f"Invitación enviada a {email}." if nuevo else
                    f"«{u.username}» ya existía: se mantuvo su cuenta y se actualizaron su puesto, módulos y empresas"
                    + (" (se le reenvió la invitación)." if not u.email_verificado else "."))
        def estado():
            u = _usuario_de_mi_org(int(F["uid"]))
            if u.id == g.usuario.id: raise ValueError("No puede desactivarse a sí mismo")
            u.activo = not u.activo; u.sesion_version += 1
            registrar(g.usuario.username, "USUARIO_ACTIVO" if u.activo else "USUARIO_DESACTIVADO", u.username); db.session.commit()
            return "Usuario " + ("activado" if u.activo else "desactivado: se cerraron sus sesiones")
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
            return "Hecho"
        acciones = {"crear": guardar, "guardar": guardar, "estado": estado, "reset_mfa": reset, "reinvitar": reset, "recuperar": reset}
        if a not in acciones: flash("Acción inválida"); return redirect(request.url)
        intentar(acciones[a], lambda msg: msg); return redirect(url_for("web.admin_usuarios"))
    us = db.session.execute(select(Usuario).where(Usuario.organizacion_id == org_id).order_by(Usuario.username)).scalars().all()
    asign = {}
    for ue, e in db.session.execute(select(UsuarioEmpresa, Empresa).join(Empresa, Empresa.id == UsuarioEmpresa.empresa_id).where(Empresa.organizacion_id == org_id)):
        asign.setdefault(ue.usuario_id, []).append(e)
    sel = lambda: campo("uid", "Usuario", "select", opciones=[(u.id, f"{u.username} · {u.rol}") for u in us])
    estado_u = lambda u: "Activo" if u.activo and u.email_verificado and u.mfa_activo else ("Desactivado" if not u.activo else ("Invitado" if not u.email_verificado else "Sin MFA"))
    nombres_mod = lambda u: "Todos" if u.rol == "Administrador" or not u.modulos else ", ".join(MODULOS[m] for m in sorted(modulos_usuario(u)))
    # Editar: ?editar=<id> precarga el formulario con los datos del usuario
    ed = next((u for u in us if str(u.id) == request.args.get("editar", "")), None)
    puestos = [(r, r) for r in ROLES_CLIENTE]   # la descripción de cada puesto está en la tabla «Puestos disponibles»
    return pagina("Usuarios", None, [
        dict(kpis=[("Usuarios", len(us)), ("Activos", sum(estado_u(u) == "Activo" for u in us)), ("Invitados", sum(estado_u(u) == "Invitado" for u in us)),
                   ("Cupo de la licencia", int((g.licencia or {}).get("max_usuarios") or 0))]),
        dict(titulo="Usuarios de la organización", nota="Pulse un usuario para cambiar su puesto, sus módulos o sus empresas.",
             tabla=dict(cols=["Usuario", "Correo", "Puesto", "Módulos", "Estado", "Empresas", "Último acceso"], filas=[
            dict(cells=[u.username, u.email or "—", u.rol, nombres_mod(u), estado_u(u),
                        ", ".join(e.razon_social for e in asign.get(u.id, [])) or ("Todas" if u.rol == "Administrador" else "—"),
                        u.ultimo_login.strftime("%Y-%m-%d %H:%M") if u.ultimo_login else "—"],
                 url=url_for("web.admin_usuarios", editar=u.id)) for u in us])),
        dict(titulo=f"Editar a «{ed.username}»" if ed else "Invitar o actualizar usuario", abierto=True,
             nota="Si el usuario o el correo ya existen en su organización, se mantiene la cuenta y se actualizan su puesto, sus módulos y sus empresas. "
                  "Si es nuevo, recibe un enlace (72 h) para crear su contraseña y activar la verificación en dos pasos. "
                  "El puesto fija qué puede hacer; los módulos limitan dónde. El Administrador siempre tiene todo.",
             form=dict(action=request.path, boton="Guardar cambios" if ed else "Enviar invitación", campos=[h("guardar"), campo("con_modulos", "", "hidden", valor="1"),
                 campo("username", "Usuario", valor=ed.username if ed else None), campo("email", "Correo", "email", valor=ed.email if ed else None),
                 campo("rol", "Puesto o tipo de usuario", "select", opciones=puestos, valor=ed.rol if ed else "Auxiliar"),
                 campo("modulos", "Módulos con acceso", "checks", req=False, opciones=[(m, MODULOS[m]) for m in del_plan],
                       valor=sorted(modulos_usuario(ed) & set(del_plan)) if ed else del_plan),
                 campo("empresas", "Empresas asignadas (el Administrador ve todas)", "checks", req=False, opciones=[(e.id, e.razon_social) for e in emps_org],
                       valor=[e.id for e in asign.get(ed.id, [])] if ed else [])])),
        *([dict(titulo="Abogada y Directores Generales", clase="maestro-contexto",
                nota="Estos roles no se invitan desde aquí: son de la plataforma, no de una empresa. La abogada (con su Inpreabogado y su firma) "
                     "y los dos Directores Generales se registran desde la consola Maestro → Contax → Legal → Documentos legales, en las "
                     "secciones «Registrar abogada» y «Registrar Director General». Cada uno recibe su invitación por correo y luego sube su firma.")]
          if _es_gigadevs(org_id) else []),
        dict(titulo="Puestos disponibles", tabla=dict(cols=["Puesto", "Qué puede hacer"], filas=[dict(cells=[r, PUESTOS.get(r, "")]) for r in ROLES_CLIENTE])),
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
                bs = moneda == "VES" or moneda.startswith("VES:")
                tasa = SFT.tasa_vigente(hoy_ve(), "USD") if bs else None
                if bs and not tasa: raise ValueError("No hay tasa BCV cargada; intente más tarde.")
                plan, meses = F["oferta"].split(":", 1)
                tasa = tasa or SFT.tasa_vigente(hoy_ve(), "USD")  # también en USDT: la factura lleva el equivalente en Bs
                pedido = dict(combo_id=int(meses)) if plan == "COMBO" else dict(plan=plan, meses=int(meses))
                o = adm.crear_orden(organizacion_ref=str(org.id), cliente=org.nombre, correo=g.usuario.email, moneda=moneda,
                                    tasa_bcv=str(tasa) if tasa else None, cliente_rif=org.rif, **pedido)
                registrar(g.usuario.username, "ORDEN_LICENCIA", f"{o['ref']} {plan} {meses} {o['monto']} {moneda}"); db.session.commit()
                return redirect(url_for("web.admin_licencia", orden=o["ref"]))
            if a == "demo":
                adm.solicitar_demo(organizacion_ref=str(org.id), cliente=org.nombre, correo=g.usuario.email, cliente_rif=org.rif, autor=g.usuario.username)
                registrar(g.usuario.username, "DEMO_SOLICITADA", "72 h facturación+inventario"); db.session.commit()
                flash(f"Demo de 72 horas emitida: la clave llegó a {g.usuario.email}. Péguela en «Activar licencia».")
                return redirect(url_for("web.admin_licencia"))
            if a == "verificar":
                _orden_de_mi_org(adm, F["ref"])  # solo órdenes de SU organización
                r = adm.verificar(F["ref"])
                flash("Pago confirmado: la clave de licencia va camino a su correo." if r.get("estado") == "entregada"
                      else ("La orden venció sin pago." if r.get("estado") == "vencida"
                            else ("Recibimos el pago pero el monto no coincide: lo revisará un operador." if r.get("estado") == "por_verificar"
                                  else (f"Ávila Pay: {r['avilapay']}. Si acaba de pagar, espere un minuto." if r.get("avilapay")
                                        else "Aún no vemos el pago en Binance. Si lo acaba de enviar, espere unos minutos."))))
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
            adm = adm_f(); precios, ordenes, combos = adm.precios(), adm.ordenes(org=str(org.id)), adm.combos(activos=True)["combos"]
            formas = [(m["codigo"], m["nombre"]) for m in adm.metodos_pago()]
        except (ErrorAdmin, ValueError, OSError) as e:
            precios, ordenes, combos, formas = [], [], [], []; flash(f"Tienda de licencias no disponible: {e}")
        ref = request.args.get("orden", "")
        o = next((x for x in ordenes if x["ref"] == ref), None)
        if o and o["proveedor"] in ("avilapay", "avilapay_usd") and o["estado"] in ("pendiente", "pagada"):
            try:   # al volver de Ávila Pay se consulta el cobro en el acto (no se espera al aviso)
                if adm.verificar(o["ref"]).get("estado") != o["estado"]:
                    o = next((x for x in adm.ordenes(org=str(org.id)) if x["ref"] == ref), o)
            except (ErrorAdmin, ValueError, OSError):
                pass
        if o:
            if o["estado"] == "entregada":
                secs.append(dict(titulo=f"Orden {o['ref']}: pagada", nota=f"La clave de licencia se envió a {g.usuario.email}. Péguela abajo en «Activar licencia»."))
            elif o["proveedor"] in ("avilapay", "avilapay_usd"):
                url_pago = str(o.get("checkout_url") or "")
                sim = "US$" if o["proveedor"] == "avilapay_usd" else "Bs"
                aj = Decimal(str(o.get("ajuste") or 0))
                desglose = (f" Precio {sim} {Decimal(str(o['monto'])) - aj:,.2f} + ajuste por forma de pago {sim} {aj:,.2f} = total {sim} {Decimal(str(o['monto'])):,.2f}."
                            if aj > 0 else "")
                if o["estado"] == "vencida":
                    secs.append(dict(titulo=f"Orden {o['ref']}: vencida", nota="El enlace de pago venció sin que se recibiera el pago. Cree una orden nueva abajo; "
                                                                              "si ya pagó, contacte a soporte con la referencia bancaria."))
                elif o["estado"] == "por_verificar":
                    secs.append(dict(titulo=f"Orden {o['ref']}: pago en revisión", nota="Recibimos un pago cuyo monto no coincide con la orden. Un operador lo revisará "
                                                                                       f"y, al confirmarlo, la clave llegará a {g.usuario.email}."))
                else:
                    secs.append(dict(titulo=f"Orden {o['ref']}: pagar {sim} {o['monto']}",
                                     nota=(o.get("instrucciones") or "") + desglose + f" Al confirmarse, la clave llega a {g.usuario.email} una sola vez.",
                                     enlace=dict(url=url_pago, texto=f"Pagar {sim} {o['monto']} con Ávila Pay") if url_pago.startswith("https://comercio.avilapay.com/") else None))
                    secs.append(dict(titulo="¿Ya pagó?", abierto=True, nota="Si ya completó el pago y la orden sigue esperando, consulte su estado ahora mismo.",
                                     form=dict(action=request.path, boton="Verificar mi pago", boton_clase="sec",
                                               campos=[campo("accion", "", "hidden", valor="verificar"), campo("ref", "", "hidden", valor=o["ref"])])))
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
        periodo = lambda m: {1: "Mensual", 12: "Anual"}.get(int(m), f"{m} meses")
        if precios or combos:
            ofertas = [(f"{p['plan']}:{p['meses']}", f"{PLANES[p['plan']]['nombre']} · {periodo(p['meses'])} · {p['monto_usd']} USD · {p['max_usuarios']} usuarios, "
                                                    f"{p['max_empresas']} empresas, {p['max_equipos']} equipos") for p in precios if p["plan"] in PLANES]
            ofertas += [(f"COMBO:{c['id']}", f"Combo {c['nombre']} ({', '.join(MODULOS.get(m, m) for m in c['modulos'].split(','))}) · {periodo(c['meses'])} · "
                                             f"{c['monto_usd']} USD · {c['max_usuarios']} usuarios, {c['max_empresas']} empresas") for c in combos]
            secs.append(dict(titulo="Comprar o renovar licencia", nota="Dólares (Binance, Zelle, tarjeta internacional, Apple Pay o Google Pay) o bolívares a la tasa BCV del día. Según la forma de pago puede aplicarse un ajuste, "
                                  "que verá antes de pagar. La verificación es automática y la clave llega por correo una sola vez.",
                             form=dict(action=request.path, boton="Continuar al pago", campos=[campo("accion", "", "hidden", valor="comprar"), campo("oferta", "Plan", "select", opciones=ofertas),
                                       campo("moneda", "Forma de pago", "select", opciones=formas or [("USDT", "USDT (Binance)"), ("VES", "Bolívares (pago móvil)")])])))
        if not (est.claims or {}).get("plan") or (est.claims or {}).get("plan") == "DEMO" or not est.valida:
            secs.append(dict(titulo="Probar gratis 72 horas", nota="Demo de los módulos de Facturación e Inventario (2 usuarios, 1 empresa). Una sola vez por organización; "
                                                                   "la clave llega a su correo y vence 72 horas después de emitida.",
                             form=dict(action=request.path, boton="Solicitar demo de 72 h", campos=[h("demo")])))
        if ordenes:
            secs.append(dict(titulo="Mis órdenes", tabla=dict(cols=["Orden", "Plan", "Meses", "Moneda", "Monto", "Estado"], num=[4], filas=[
                dict(cells=[x["ref"], x["plan"], x["meses"], x["moneda"], x["monto"], x["estado"].replace("_", " ")], url=url_for("web.admin_licencia", orden=x["ref"])) for x in ordenes])))
    planes = [dict(cells=[p["nombre"], ", ".join(MODULOS[m] for m in sorted(p["modulos"])) or "A su medida: elija los módulos con Gigadevs"]) for p in PLANES.values()]
    secs += [dict(titulo="Activar licencia", nota="Pegue la clave que recibió por correo. Cada clave se vincula a su organización.",
                  form=dict(action=request.path, boton="Activar", campos=[h("activar"), campo("clave", "Clave de licencia")])),
             dict(titulo="Renovar ahora", nota="Trae de inmediato los cambios de plan o de topes hechos por el proveedor.", form=dict(action=request.path, boton="Renovar", campos=[h("renovar")])),
             dict(titulo="Planes disponibles", tabla=dict(cols=["Plan", "Módulos"], filas=planes))]
    return pagina("Licencia", None, secs)


def _es_gigadevs(org_id) -> bool:
    """¿Es esta la organización de Gigadevs (GIGADEVS_USUARIO)? Solo ahí tiene sentido el aviso del módulo Legal."""
    try:
        from .web_plataforma import _usuario_gigadevs
        _u, org = _usuario_gigadevs()
        return org is not None and org.id == org_id
    except Exception:  # noqa: BLE001 — un aviso nunca debe romper la página
        return False
