"""Autenticación: contraseña (Argon2id) → correo verificado → MFA TOTP obligatorio → cambio de clave si procede.
Incluye invitaciones por correo, recuperación de contraseña, códigos de respaldo y alertas de nuevo acceso."""
import base64
import secrets
import time
from datetime import timedelta

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, session, url_for
from sqlalchemy import func, select, update

from .. import correo, totp
from ..auditoria import registrar
from ..extensions import db, limiter
from ..models import CodigoRecuperacion, Equipo, Organizacion, TokenCorreo, Usuario, _ahora
from ..rls import fijar_org
from ..security import MFA_OBLIGATORIO, clave_reutilizada, fijar_clave, hashear, politica_clave, sha256, verificar_clave

bp = Blueprint("auth", __name__)
MAX_INTENTOS, BLOQUEO = 5, timedelta(minutes=15)
MSG_GENERICO = "Usuario o contraseña incorrectos"  # mismo mensaje exista o no el usuario
MSG_RECUPERAR = "Si el correo corresponde a una cuenta activa, recibirá un enlace en unos minutos."
VIGENCIA = {"verificar": timedelta(hours=24), "invitacion": timedelta(hours=72), "recuperar": timedelta(minutes=30)}
N_CODIGOS = 10

RUTAS_PUBLICAS = {"auth.login", "auth.logout", "auth.recuperar", "auth.recuperar_token", "auth.invitacion", "auth.correo_confirmar", "static"}
RUTAS_GATE = RUTAS_PUBLICAS | {"auth.verificar_correo", "auth.mfa_configurar", "auth.mfa_codigos", "auth.mfa_verificar", "auth.cambiar_clave"}


# ---------------------------------------------------------------- tokens por correo
def crear_token(u, proposito, email) -> str:
    db.session.execute(update(TokenCorreo).where(TokenCorreo.usuario_id == u.id, TokenCorreo.proposito == proposito,
                                                 TokenCorreo.usado_en.is_(None)).values(usado_en=_ahora()))  # invalida enlaces previos
    crudo = secrets.token_urlsafe(32)
    db.session.add(TokenCorreo(usuario_id=u.id, proposito=proposito, token_hash=sha256(crudo), email=email,
                               expira_en=_ahora() + VIGENCIA[proposito]))
    return crudo


def consumir_token(crudo, proposito, marcar=True):
    if not crudo or len(crudo) > 100: return None
    t = db.session.execute(select(TokenCorreo).where(TokenCorreo.token_hash == sha256(crudo), TokenCorreo.proposito == proposito)
                           .with_for_update()).scalar_one_or_none()
    if t is None or t.usado_en is not None or t.expira_en < _ahora(): return None
    if marcar: t.usado_en = _ahora()
    return t


def enviar_invitacion(u, org_nombre, quien):
    tok = crear_token(u, "invitacion", u.email)
    correo.enviar(u.email, "Invitación de acceso",
                  f"{quien} le dio acceso a {org_nombre} con el usuario «{u.username}» y el rol {u.rol}.\n\n"
                  f"Para activar su cuenta y crear su contraseña abra este enlace (vence en 72 horas):\n"
                  f"{correo.url_absoluta(url_for('auth.invitacion', token=tok))}\n\n"
                  "Al primer ingreso deberá configurar la verificación en dos pasos (MFA).")


def enviar_verificacion(u, email):
    tok = crear_token(u, "verificar", email)
    correo.enviar(email, "Verifique su correo",
                  f"Para confirmar que este correo pertenece al usuario «{u.username}» abra este enlace (vence en 24 horas):\n"
                  f"{correo.url_absoluta(url_for('auth.correo_confirmar', token=tok))}")


# ---------------------------------------------------------------- sesión y compuertas
@bp.before_app_request
def cargar_usuario():
    g.usuario, g.org_rls = None, None
    uid = session.get("uid")
    if not uid:
        return None
    u = db.session.get(Usuario, uid)
    org = db.session.get(Organizacion, u.organizacion_id) if u and u.organizacion_id else None
    vencida = time.time() - session.get("t0", 0) > current_app.config["SESION_ABSOLUTA_S"]
    if (u is None or not u.activo or session.get("sv") != u.sesion_version or vencida
            or (u.organizacion_id and (org is None or org.estado != "activa"))):
        session.clear()  # desactivado, clave/MFA cambiados, sesión vencida u organización suspendida: fuera de inmediato
        return redirect(url_for("auth.login")) if request.endpoint not in RUTAS_PUBLICAS else None
    g.usuario, g.organizacion = u, org
    fijar_org(db, u.organizacion_id)
    if session.get("eq") and db.session.execute(select(Equipo.id).where(Equipo.id == session["eq"], Equipo.revocado_en.is_(None))).first() is None:
        session.clear()  # equipo revocado por el administrador: la sesión de ese equipo termina de inmediato
        return redirect(url_for("auth.login"))
    session.permanent = True  # renueva la ventana de inactividad
    if request.endpoint in RUTAS_GATE:
        return None
    if not u.email_verificado:
        return redirect(url_for("auth.verificar_correo"))
    if u.rol in MFA_OBLIGATORIO and not u.mfa_activo:
        return redirect(url_for("auth.mfa_configurar"))
    if u.debe_cambiar_clave:
        return redirect(url_for("auth.cambiar_clave"))
    return None


def _equipo(u):
    """Equipo autorizado: reconoce el de la cookie o registra uno nuevo si la licencia lo permite.
    Devuelve (equipo, token_nuevo) o lanza PermissionError si se alcanzó el tope de equipos."""
    fijar_org(db, u.organizacion_id)
    crudo = request.cookies.get(current_app.config["EQUIPO_COOKIE"], "")
    eq = db.session.execute(select(Equipo).where(Equipo.token_hash == sha256(crudo), Equipo.organizacion_id == u.organizacion_id,
                                                 Equipo.revocado_en.is_(None))).scalar_one_or_none() if 20 <= len(crudo) <= 100 else None
    if eq is not None:
        eq.ultimo_uso, eq.ip = _ahora(), request.remote_addr
        return eq, None
    est = current_app.extensions["gestor_licencia"](u.organizacion_id).estado()
    tope = int((est.claims or {}).get("max_equipos") or 0)
    activos = db.session.execute(select(func.count()).select_from(Equipo).where(Equipo.organizacion_id == u.organizacion_id, Equipo.revocado_en.is_(None))).scalar()
    if tope and activos >= tope:
        registrar(u.username, "EQUIPO_RECHAZADO", f"tope={tope} ip={request.remote_addr}", org=u.organizacion_id); db.session.commit()
        raise PermissionError(f"Su licencia permite {tope} equipo(s) y ya están en uso. Pida al administrador que revoque uno en Organización → Equipos.")
    crudo = secrets.token_urlsafe(32)
    eq = Equipo(organizacion_id=u.organizacion_id, token_hash=sha256(crudo), nombre=request.user_agent.string[:160] or "Navegador",
                ip=request.remote_addr, autorizado_por=u.id)
    db.session.add(eq); db.session.flush()
    registrar(u.username, "EQUIPO_AUTORIZADO", f"equipo={eq.id} ip={request.remote_addr}", org=u.organizacion_id)
    if u.email_verificado:
        correo.enviar_seguro(u.email, "Nuevo equipo autorizado", f"Se autorizó un equipo nuevo para su organización al entrar como «{u.username}» "
                             f"desde la IP {request.remote_addr} ({request.user_agent.string[:120]}).\nSi no fue usted, avise al administrador para revocarlo.")
    return eq, crudo


def _abrir_sesion(u):
    eq, token_eq = None, None
    if u.organizacion_id:
        try:
            eq, token_eq = _equipo(u)
        except PermissionError as e:
            session.clear(); flash(str(e)); return render_template("auth.html", vista="login"), 403
    session.clear()  # sesión nueva: evita fijación de sesión
    session["uid"], session["sv"], session["t0"] = u.id, u.sesion_version, time.time()
    if eq is not None: session["eq"] = eq.id
    ip = request.remote_addr
    if u.email and u.email_verificado and u.ultimo_ip and u.ultimo_ip != ip:
        correo.enviar_seguro(u.email, "Nuevo inicio de sesión",
                             f"Se inició sesión con su usuario «{u.username}» desde la IP {ip} el {_ahora():%Y-%m-%d %H:%M} UTC "
                             f"({request.user_agent.string[:120]}).\nSi no fue usted, cambie su contraseña y avise al administrador.")
    u.ultimo_ip, u.ultimo_login = ip, _ahora()
    db.session.commit()
    resp = redirect("/")
    if token_eq:
        resp.set_cookie(current_app.config["EQUIPO_COOKIE"], token_eq, max_age=400 * 86400, secure=current_app.config["SESSION_COOKIE_SECURE"],
                        httponly=True, samesite="Lax", path="/")
    return resp


@bp.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per minute; 50 per hour", methods=["POST"])
def login():
    if request.method == "POST":
        nombre = (request.form.get("usuario") or "").strip()[:254]
        clave = request.form.get("clave") or ""
        campo = Usuario.email if "@" in nombre else Usuario.username
        u = db.session.execute(select(Usuario).where(campo == nombre)).scalar_one_or_none()
        ahora = _ahora()
        org_id = u.organizacion_id if u else None
        if u and u.bloqueado_hasta and u.bloqueado_hasta > ahora:
            registrar(nombre, "LOGIN_BLOQUEADO", org=org_id); db.session.commit()
            flash(MSG_GENERICO); return render_template("auth.html", vista="login"), 401
        ok, rehash = verificar_clave(clave, u.password_hash if u else None)
        org_ok = u is not None and (u.organizacion_id is None or (db.session.get(Organizacion, u.organizacion_id).estado == "activa"))
        if ok and u.activo and org_ok:
            if rehash:
                u.password_hash = hashear(clave)  # PBKDF2 legado -> Argon2id de forma transparente
            u.intentos_fallidos, u.bloqueado_hasta = 0, None
            if u.mfa_activo:  # 2.º factor pendiente: aún NO hay sesión autenticada
                db.session.commit()
                session.clear(); session["pre_mfa"], session["pre_mfa_ts"] = u.id, time.time()
                return redirect(url_for("auth.mfa_verificar"))
            registrar(u.username, "LOGIN_OK", org=org_id)
            return _abrir_sesion(u)
        if u:
            u.intentos_fallidos += 1
            if u.intentos_fallidos >= MAX_INTENTOS:
                u.bloqueado_hasta, u.intentos_fallidos = ahora + BLOQUEO, 0
                if u.email and u.email_verificado:
                    correo.enviar_seguro(u.email, "Cuenta bloqueada temporalmente",
                                         f"Se bloqueó el usuario «{u.username}» 15 minutos por {MAX_INTENTOS} intentos fallidos (IP {request.remote_addr}).")
        registrar(nombre or "?", "LOGIN_FALLIDO", org=org_id); db.session.commit()
        flash(MSG_GENERICO); return render_template("auth.html", vista="login"), 401
    return render_template("auth.html", vista="login")


@bp.post("/logout")
def logout():
    if g.usuario: registrar(g.usuario.username, "LOGOUT"); db.session.commit()
    session.clear()
    return redirect(url_for("auth.login"))


# ---------------------------------------------------------------- correo
@bp.route("/correo/verificar", methods=["GET", "POST"])
@limiter.limit("5 per 10 minutes", methods=["POST"])
def verificar_correo():
    if g.usuario is None: return redirect(url_for("auth.login"))
    u = g.usuario
    if u.email_verificado: return redirect("/")
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()[:254]
        if "@" not in email or "." not in email.split("@")[-1] or " " in email:
            flash("Correo inválido")
        elif db.session.execute(select(Usuario.id).where(Usuario.email == email, Usuario.id != u.id)).first():
            flash("Ese correo no puede usarse. Indique otro.")  # no se confirma a quién pertenece
        else:
            enviar_verificacion(u, email)
            registrar(u.username, "CORREO_VERIFICACION_ENVIADA"); db.session.commit()
            flash("Le enviamos un enlace de verificación. Ábralo desde su correo para continuar.")
        return redirect(url_for("auth.verificar_correo"))
    return render_template("auth.html", vista="verificar_correo", email=u.email or "")


@bp.get("/correo/confirmar/<token>")
def correo_confirmar(token):
    t = consumir_token(token, "verificar")
    if t is None:
        flash("El enlace no es válido o ya venció. Solicite uno nuevo."); return redirect(url_for("auth.login"))
    u = db.session.get(Usuario, t.usuario_id)
    if db.session.execute(select(Usuario.id).where(Usuario.email == t.email, Usuario.id != u.id)).first():
        db.session.commit(); flash("Ese correo ya está en uso."); return redirect(url_for("auth.login"))
    u.email, u.email_verificado = t.email, True
    registrar(u.username, "CORREO_VERIFICADO", org=u.organizacion_id); db.session.commit()
    flash("Correo verificado.")
    return redirect("/" if g.usuario else url_for("auth.login"))


@bp.route("/invitacion/<token>", methods=["GET", "POST"])
@limiter.limit("10 per 10 minutes", methods=["POST"])
def invitacion(token):
    t = consumir_token(token, "invitacion", marcar=False)
    if t is None:
        flash("La invitación no es válida o ya venció. Pida al administrador que la reenvíe."); return redirect(url_for("auth.login"))
    u = db.session.get(Usuario, t.usuario_id)
    if request.method == "POST":
        c1, c2 = request.form.get("nueva", ""), request.form.get("repetir", "")
        err = politica_clave(c1, u.username, u.email or "") or (None if c1 == c2 else "Las contraseñas no coinciden")
        if err:
            flash(err)
        else:
            consumir_token(token, "invitacion")
            fijar_clave(u, c1)
            u.email, u.email_verificado, u.debe_cambiar_clave = t.email, True, False
            registrar(u.username, "INVITACION_ACEPTADA", org=u.organizacion_id); db.session.commit()
            session.clear()
            flash("Cuenta activada. Inicie sesión; a continuación configurará la verificación en dos pasos.")
            return redirect(url_for("auth.login"))
    return render_template("auth.html", vista="invitacion", usuario=u.username)


@bp.route("/recuperar", methods=["GET", "POST"])
@limiter.limit("5 per 15 minutes", methods=["POST"])
def recuperar():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()[:254]
        u = db.session.execute(select(Usuario).where(Usuario.email == email)).scalar_one_or_none() if "@" in email else None
        if u and u.activo and u.email_verificado:
            tok = crear_token(u, "recuperar", u.email)
            registrar(u.username, "RECUPERACION_SOLICITADA", org=u.organizacion_id); db.session.commit()
            correo.enviar_seguro(u.email, "Restablecer contraseña",
                                 f"Recibimos una solicitud para restablecer la contraseña de «{u.username}».\n"
                                 f"Enlace (vence en 30 minutos, un solo uso):\n{correo.url_absoluta(url_for('auth.recuperar_token', token=tok))}\n\n"
                                 "Si no lo solicitó, ignore este mensaje: su contraseña no cambia.")
        else:
            verificar_clave("relleno", None)  # tiempo similar exista o no la cuenta
        flash(MSG_RECUPERAR)
        return redirect(url_for("auth.login"))
    return render_template("auth.html", vista="recuperar")


@bp.route("/recuperar/<token>", methods=["GET", "POST"])
@limiter.limit("10 per 15 minutes", methods=["POST"])
def recuperar_token(token):
    t = consumir_token(token, "recuperar", marcar=False)
    if t is None:
        flash("El enlace no es válido o ya venció."); return redirect(url_for("auth.recuperar"))
    u = db.session.get(Usuario, t.usuario_id)
    if request.method == "POST":
        c1, c2 = request.form.get("nueva", ""), request.form.get("repetir", "")
        err = politica_clave(c1, u.username, u.email or "") or (None if c1 == c2 else "Las contraseñas no coinciden")
        if not err and clave_reutilizada(u, c1): err = "No puede reutilizar una de sus últimas contraseñas"
        if err:
            flash(err)
        else:
            consumir_token(token, "recuperar")
            fijar_clave(u, c1); u.debe_cambiar_clave = False
            registrar(u.username, "CLAVE_RESTABLECIDA_CORREO", org=u.organizacion_id); db.session.commit()
            correo.enviar_seguro(u.email, "Contraseña cambiada", f"La contraseña de «{u.username}» se cambió y se cerraron todas sus sesiones.")
            session.clear()
            flash("Contraseña actualizada. Inicie sesión (se le pedirá su código MFA).")
            return redirect(url_for("auth.login"))
    return render_template("auth.html", vista="restablecer")


@bp.route("/cambiar-clave", methods=["GET", "POST"])
def cambiar_clave():
    if g.usuario is None: return redirect(url_for("auth.login"))
    u = g.usuario
    if request.method == "POST":
        actual, nueva = request.form.get("actual", ""), request.form.get("nueva", "")
        if not verificar_clave(actual, u.password_hash)[0]:
            flash("La contraseña actual no es correcta")
        elif (err := politica_clave(nueva, u.username, u.email or "")):
            flash(err)
        elif clave_reutilizada(u, nueva):
            flash("No puede reutilizar una de sus últimas contraseñas")
        else:
            fijar_clave(u, nueva); u.debe_cambiar_clave = False
            registrar(u.username, "CLAVE_CAMBIADA"); db.session.commit()
            session["sv"] = u.sesion_version  # esta sesión sigue; las demás quedan cerradas
            if u.email_verificado: correo.enviar_seguro(u.email, "Contraseña cambiada", f"La contraseña de «{u.username}» se cambió. Se cerraron sus otras sesiones.")
            flash("Contraseña actualizada")
            return redirect("/")
    return render_template("auth.html", vista="cambiar_clave")


# ---------------------------------------------------------------- MFA
def _generar_codigos(u) -> list[str]:
    db.session.query(CodigoRecuperacion).filter_by(usuario_id=u.id).delete()
    codigos = []
    for _ in range(N_CODIGOS):
        c = base64.b32encode(secrets.token_bytes(6)).decode()[:10]
        c = f"{c[:5]}-{c[5:]}"
        codigos.append(c)
        db.session.add(CodigoRecuperacion(usuario_id=u.id, codigo_hash=sha256(c)))
    return codigos


def _usar_codigo_respaldo(u, ingresado) -> bool:
    c = ingresado.strip().upper().replace(" ", "")
    if len(c) == 10: c = f"{c[:5]}-{c[5:]}"
    fila = db.session.execute(select(CodigoRecuperacion).where(CodigoRecuperacion.usuario_id == u.id, CodigoRecuperacion.codigo_hash == sha256(c),
                                                               CodigoRecuperacion.usado_en.is_(None)).with_for_update()).scalar_one_or_none()
    if fila is None: return False
    fila.usado_en = _ahora()
    return True


@bp.route("/mfa/verificar", methods=["GET", "POST"])
@limiter.limit("5 per minute", methods=["POST"])
def mfa_verificar():
    uid = session.get("pre_mfa")
    if not uid or time.time() - session.get("pre_mfa_ts", 0) > 300:
        session.clear(); return redirect(url_for("auth.login"))
    u = db.session.get(Usuario, uid)
    if request.method == "POST" and u:
        ingresado = (request.form.get("codigo") or "").strip()
        secreto = current_app.extensions["cifrador"].descifrar(u.mfa_secreto_cifrado)
        c = totp.verificar(secreto, ingresado, u.mfa_ultimo_contador)
        if c is not None:
            u.mfa_ultimo_contador = c
            registrar(u.username, "LOGIN_OK_MFA", org=u.organizacion_id)
            return _abrir_sesion(u)
        if len(ingresado) > 6 and _usar_codigo_respaldo(u, ingresado):
            registrar(u.username, "LOGIN_OK_CODIGO_RESPALDO", org=u.organizacion_id)
            if u.email_verificado: correo.enviar_seguro(u.email, "Código de respaldo usado", f"Se usó un código de respaldo MFA para entrar como «{u.username}».")
            return _abrir_sesion(u)
        registrar(u.username, "MFA_FALLIDO", org=u.organizacion_id); db.session.commit()
        flash("Código incorrecto")
    return render_template("auth.html", vista="mfa_verificar")


def _qr_svg(uri: str) -> str | None:
    try:
        import segno
    except ImportError:
        return None
    return segno.make(uri, error="m").svg_data_uri(scale=5, border=2)


@bp.route("/mfa/configurar", methods=["GET", "POST"])
def mfa_configurar():
    if g.usuario is None: return redirect(url_for("auth.login"))
    u = g.usuario
    if not u.email_verificado: return redirect(url_for("auth.verificar_correo"))
    if u.mfa_activo: return redirect("/")
    if request.method == "GET" or "mfa_tmp" not in session:
        session["mfa_tmp"] = totp.nuevo_secreto()
    secreto = session["mfa_tmp"]
    if request.method == "POST":
        c = totp.verificar(secreto, (request.form.get("codigo") or "").strip())
        if c is None:
            flash("Código incorrecto: revise la hora de su teléfono e intente de nuevo")
        else:
            u.mfa_secreto_cifrado = current_app.extensions["cifrador"].cifrar(secreto)
            u.mfa_activo, u.mfa_ultimo_contador = True, c
            session["codigos_mfa"] = _generar_codigos(u)
            registrar(u.username, "MFA_ACTIVADO"); db.session.commit()
            session.pop("mfa_tmp", None)
            correo.enviar_seguro(u.email, "Verificación en dos pasos activada", f"Se activó el MFA del usuario «{u.username}».")
            return redirect(url_for("auth.mfa_codigos"))
    uri = totp.uri_otpauth(secreto, u.username, current_app.config["APP_NOMBRE"])
    return render_template("auth.html", vista="mfa_configurar", secreto=secreto, uri=uri, qr=_qr_svg(uri))


@bp.route("/mfa/codigos", methods=["GET", "POST"])
def mfa_codigos():
    """Muestra UNA vez los códigos de respaldo; también permite regenerarlos validando un código TOTP."""
    if g.usuario is None or not g.usuario.mfa_activo: return redirect(url_for("auth.login"))
    if request.method == "POST":
        u = g.usuario
        secreto = current_app.extensions["cifrador"].descifrar(u.mfa_secreto_cifrado)
        c = totp.verificar(secreto, (request.form.get("codigo") or "").strip(), u.mfa_ultimo_contador)
        if c is None:
            flash("Código incorrecto"); return redirect(url_for("auth.mfa_codigos"))
        u.mfa_ultimo_contador = c
        session["codigos_mfa"] = _generar_codigos(u)
        registrar(u.username, "MFA_CODIGOS_REGENERADOS"); db.session.commit()
        return redirect(url_for("auth.mfa_codigos"))
    codigos = session.pop("codigos_mfa", None)
    restantes = db.session.query(CodigoRecuperacion).filter_by(usuario_id=g.usuario.id, usado_en=None).count()
    return render_template("auth.html", vista="mfa_codigos", codigos=codigos, restantes=restantes)


@bp.get("/cuenta")
def cuenta():
    if g.usuario is None: abort(401)
    restantes = db.session.query(CodigoRecuperacion).filter_by(usuario_id=g.usuario.id, usado_en=None).count()
    return render_template("auth.html", vista="cuenta", restantes=restantes)
