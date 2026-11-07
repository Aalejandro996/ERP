"""Documentos legales de Gigadevs: versiones por sistema, aprobación de la abogada y registro de aceptaciones.

- Cada documento (contrato, términos, privacidad, cookies) de cada sistema tiene versiones. Una versión nace como borrador desde la
  plantilla (plantillas_legales.py), se edita, pasa a revisión y solo rige cuando la abogada autorizada la aprueba. Al aprobarse
  queda fija: su texto y su huella SHA-256 no cambian; un cambio posterior crea una versión nueva.
- La abogada se registra con su nombre, cédula, número de Inpreabogado y su firma (imagen). La aprobación guarda quién, cuándo,
  la huella del texto y la huella de su firma.
- Cada aceptación (Contax, Surtia, Quincena VE…) se guarda contra la versión exacta, con quién aceptó, de qué organización o
  cuenta, la IP, el navegador y la hora. Las aceptaciones son de solo inserción y van encadenadas por SHA-256: alterar o borrar
  una rompe la cadena y se detecta."""
import base64
import hashlib
import json
import re

from sqlalchemy import Column, Float, Integer, MetaData, String, Table, Text, UniqueConstraint, func, insert, select, update

from . import plantillas_legales as PL

md = MetaData()
documentos = Table(
    "lic_legal_documentos", md,
    Column("id", Integer, primary_key=True), Column("sistema", String(40), nullable=False), Column("tipo", String(20), nullable=False),
    Column("version", Integer, nullable=False), Column("titulo", Text, nullable=False), Column("secciones", Text, nullable=False),   # JSON
    Column("huella", String(64), nullable=False), Column("estado", String(12), nullable=False, default="borrador"),
    # borrador → revision → aprobado (rige) → reemplazado (rigió hasta que se aprobó otra versión)
    Column("observaciones", Text), Column("creado_por", Text), Column("creado_en", Float, nullable=False), Column("actualizado_en", Float, nullable=False),
    Column("abogado_id", Integer), Column("aprobado_en", Float), Column("aprobacion", String(64)),
    UniqueConstraint("sistema", "tipo", "version", name="uq_legal_documento_version"))
abogados = Table(
    "lic_legal_abogados", md,
    Column("id", Integer, primary_key=True), Column("usuario", String(80), nullable=False, unique=True),   # usuario de Contax (rol Abogado)
    Column("nombre", Text, nullable=False), Column("cedula", String(20), nullable=False), Column("inpreabogado", String(20), nullable=False),
    Column("correo", Text), Column("firma", Text), Column("firma_huella", String(64)),                      # firma: PNG en base64
    Column("activo", Integer, nullable=False, default=1), Column("registrado_en", Float, nullable=False), Column("actualizado_en", Float, nullable=False))
aceptaciones = Table(
    "lic_legal_aceptaciones", md,
    Column("id", Integer, primary_key=True), Column("documento_id", Integer, nullable=False), Column("sistema", String(40), nullable=False),
    Column("tipo", String(20), nullable=False), Column("version", Integer, nullable=False), Column("huella", String(64), nullable=False),
    Column("usuario", String(254), nullable=False),                  # quién aceptó (usuario o correo)
    Column("nombre", Text), Column("sujeto", String(200)),           # organización/cuenta: RIF o correo
    Column("organizacion", Text), Column("rol", String(40)), Column("ip", String(64)), Column("agente", Text),
    Column("origen", String(40), nullable=False), Column("aceptado_en", Float, nullable=False),
    Column("prev", String(64), nullable=False), Column("cadena", String(64), nullable=False))

SISTEMAS = tuple(PL.PERFILES)
TIPOS = tuple(PL.TIPOS)
# Qué documentos acepta cada quien al ingresar: el responsable (administrador o titular) acepta además el contrato
REQUERIDOS = {"responsable": ("contrato", "terminos", "privacidad"), "usuario": ("terminos", "privacidad")}
RE_CI = re.compile(r"^[VE]-?\d{5,9}$")
RE_INPRE = re.compile(r"^\d{3,7}$")


def instalar(eng):
    md.create_all(eng)


def _secciones_validas(secciones):
    if not isinstance(secciones, list) or not secciones: raise ValueError("El documento necesita al menos una sección")
    limpias = []
    for s in secciones:
        t, ps = (s.get("titulo"), s.get("parrafos")) if isinstance(s, dict) else (s[0], s[1])
        t = str(t or "").strip()[:200]
        ps = [str(p).strip()[:6000] for p in (ps or []) if str(p).strip()]
        if t and ps: limpias.append((t, ps))
    if not limpias: raise ValueError("El documento necesita al menos una sección con texto")
    return limpias


def _dict(fila):
    d = dict(fila._mapping)
    if "secciones" in d: d["secciones"] = [{"titulo": t, "parrafos": ps} for t, ps in json.loads(d["secciones"])]
    if "firma" in d: d["tiene_firma"] = bool(d.pop("firma"))
    return d


class Legal:
    def __init__(self, st):
        self.st = st
        instalar(st.eng)
        self.sembrar()

    # ------------------------------------------------------------------ documentos
    def sembrar(self):
        """Primera versión (borrador) de cada documento desde su plantilla, si aún no existe ninguna."""
        ahora = self.st.reloj()
        with self.st.eng.begin() as c:
            for s in SISTEMAS:
                for t in TIPOS:
                    if c.execute(select(documentos.c.id).where(documentos.c.sistema == s, documentos.c.tipo == t)).first(): continue
                    secs = PL.plantilla(s, t); tit = PL.titulo(s, t)
                    c.execute(insert(documentos).values(sistema=s, tipo=t, version=1, titulo=tit, secciones=json.dumps(secs, ensure_ascii=False),
                                                        huella=PL.huella(tit, secs), estado="borrador", creado_por="plantilla", creado_en=ahora,
                                                        actualizado_en=ahora))

    def listar(self, sistema=None):
        q = select(documentos.c.id, documentos.c.sistema, documentos.c.tipo, documentos.c.version, documentos.c.titulo, documentos.c.huella,
                   documentos.c.estado, documentos.c.actualizado_en, documentos.c.aprobado_en, documentos.c.abogado_id, documentos.c.observaciones
                   ).order_by(documentos.c.sistema, documentos.c.tipo, documentos.c.version.desc())
        if sistema: q = q.where(documentos.c.sistema == sistema)
        with self.st.eng.connect() as c:
            return [dict(r._mapping) for r in c.execute(q)]

    def documento(self, did):
        with self.st.eng.connect() as c:
            fila = c.execute(select(documentos).where(documentos.c.id == int(did))).first()
            if fila is None: raise LookupError("Documento inexistente")
            d = _dict(fila)
            if d.get("abogado_id"):
                ab = c.execute(select(abogados).where(abogados.c.id == d["abogado_id"])).first()
                d["abogado"] = {k: ab._mapping[k] for k in ("nombre", "cedula", "inpreabogado", "firma", "firma_huella")} if ab else None
        return d

    def vigente(self, sistema, tipo):
        with self.st.eng.connect() as c:
            fila = c.execute(select(documentos.c.id).where(documentos.c.sistema == sistema, documentos.c.tipo == tipo, documentos.c.estado == "aprobado")
                             .order_by(documentos.c.version.desc()).limit(1)).first()
        return self.documento(fila.id) if fila else None

    def guardar(self, did, secciones, autor, observaciones=None):
        """Edita un borrador o una versión en revisión. Una versión aprobada no se toca: se crea una versión nueva a partir de ella."""
        secs = _secciones_validas(secciones)
        ahora = self.st.reloj()
        with self.st.eng.begin() as c:
            d = c.execute(select(documentos).where(documentos.c.id == int(did)).with_for_update()).first()
            if d is None: raise LookupError("Documento inexistente")
            valores = dict(secciones=json.dumps(secs, ensure_ascii=False), huella=PL.huella(d.titulo, secs), actualizado_en=ahora,
                           observaciones=(str(observaciones).strip()[:2000] or None) if observaciones is not None else d.observaciones)
            if d.estado in ("borrador", "revision"):
                c.execute(update(documentos).where(documentos.c.id == d.id).values(estado="borrador", **valores))
                return d.id
            borrador = c.execute(select(documentos.c.id).where(documentos.c.sistema == d.sistema, documentos.c.tipo == d.tipo,
                                                               documentos.c.estado.in_(("borrador", "revision")))).first()
            if borrador: raise ValueError("Ya hay una versión en preparación de este documento: edítela a ella")
            ver = (c.execute(select(func.max(documentos.c.version)).where(documentos.c.sistema == d.sistema, documentos.c.tipo == d.tipo)).scalar() or 0) + 1
            return c.execute(insert(documentos).values(sistema=d.sistema, tipo=d.tipo, version=ver, titulo=d.titulo, estado="borrador",
                                                       creado_por=str(autor)[:80], creado_en=ahora, **valores).returning(documentos.c.id)).scalar()

    def a_revision(self, did):
        with self.st.eng.begin() as c:
            if not c.execute(update(documentos).where(documentos.c.id == int(did), documentos.c.estado == "borrador").values(estado="revision")).rowcount:
                raise ValueError("Solo un borrador se envía a revisión")

    def devolver(self, did, observaciones, usuario_abogado):
        self.abogado(usuario_abogado)
        with self.st.eng.begin() as c:
            if not c.execute(update(documentos).where(documentos.c.id == int(did), documentos.c.estado == "revision")
                             .values(estado="borrador", observaciones=str(observaciones or "").strip()[:2000] or None)).rowcount:
                raise ValueError("Solo se devuelve un documento en revisión")

    def aprobar(self, did, usuario_abogado):
        """La abogada aprueba la versión en revisión: rige desde ya y la versión anterior queda «reemplazada»."""
        ab = self.abogado(usuario_abogado)
        if not ab["activo"]: raise ValueError("La abogada no está activa")
        if not ab["tiene_firma"]: raise ValueError("Registre su firma antes de aprobar documentos")
        ahora = self.st.reloj()
        with self.st.eng.begin() as c:
            d = c.execute(select(documentos).where(documentos.c.id == int(did)).with_for_update()).first()
            if d is None: raise LookupError("Documento inexistente")
            if d.estado != "revision": raise ValueError("Solo se aprueba un documento enviado a revisión")
            aprobacion = hashlib.sha256(f"{d.huella}|{ab['inpreabogado']}|{ab['firma_huella']}|{ahora}".encode()).hexdigest()
            c.execute(update(documentos).where(documentos.c.sistema == d.sistema, documentos.c.tipo == d.tipo, documentos.c.estado == "aprobado")
                      .values(estado="reemplazado"))
            c.execute(update(documentos).where(documentos.c.id == d.id).values(estado="aprobado", abogado_id=ab["id"], aprobado_en=ahora,
                                                                                aprobacion=aprobacion, actualizado_en=ahora))
        return self.documento(did)

    # ------------------------------------------------------------------ abogada
    def abogado(self, usuario):
        with self.st.eng.connect() as c:
            fila = c.execute(select(abogados).where(abogados.c.usuario == str(usuario))).first()
        if fila is None: raise LookupError("No hay una abogada registrada con ese usuario")
        return _dict(fila)

    def abogados(self):
        with self.st.eng.connect() as c:
            return [_dict(r) for r in c.execute(select(abogados).order_by(abogados.c.nombre))]

    def guardar_abogado(self, usuario, nombre, cedula, inpreabogado, correo=None, activo=True):
        nombre = str(nombre or "").strip()[:120]
        cedula = str(cedula or "").strip().upper().replace(".", "")
        inpre = str(inpreabogado or "").strip().replace(".", "")
        if len(nombre) < 5: raise ValueError("Indique el nombre completo de la abogada")
        if not RE_CI.match(cedula): raise ValueError("Cédula inválida (V-12345678)")
        if not RE_INPRE.match(inpre): raise ValueError("Número de Inpreabogado inválido (solo números)")
        ahora = self.st.reloj()
        with self.st.eng.begin() as c:
            if c.execute(select(abogados.c.id).where(abogados.c.usuario == str(usuario))).first():
                c.execute(update(abogados).where(abogados.c.usuario == str(usuario)).values(nombre=nombre, cedula=cedula, inpreabogado=inpre,
                                                                                         correo=correo, activo=1 if activo else 0, actualizado_en=ahora))
            else:
                c.execute(insert(abogados).values(usuario=str(usuario)[:80], nombre=nombre, cedula=cedula, inpreabogado=inpre, correo=correo,
                                                  activo=1 if activo else 0, registrado_en=ahora, actualizado_en=ahora))
        return self.abogado(usuario)

    def guardar_firma(self, usuario, png: bytes):
        if not png or len(png) > 300 * 1024 or png[:8] != b"\x89PNG\r\n\x1a\n": raise ValueError("La firma debe ser una imagen PNG de hasta 300 KB")
        self.abogado(usuario)
        with self.st.eng.begin() as c:
            c.execute(update(abogados).where(abogados.c.usuario == str(usuario)).values(
                firma=base64.b64encode(png).decode(), firma_huella=hashlib.sha256(png).hexdigest(), actualizado_en=self.st.reloj()))

    # ------------------------------------------------------------------ aceptaciones
    def pendientes(self, sistema, usuario, perfil="usuario"):
        """Documentos vigentes (aprobados) que esta persona aún no aceptó en su versión actual."""
        if sistema not in SISTEMAS: raise ValueError("Sistema inválido")
        out = []
        with self.st.eng.connect() as c:
            for t in REQUERIDOS.get(perfil, REQUERIDOS["usuario"]):
                v = c.execute(select(documentos.c.id, documentos.c.titulo, documentos.c.version, documentos.c.huella)
                              .where(documentos.c.sistema == sistema, documentos.c.tipo == t, documentos.c.estado == "aprobado")
                              .order_by(documentos.c.version.desc()).limit(1)).first()
                if v is None: continue
                if c.execute(select(aceptaciones.c.id).where(aceptaciones.c.documento_id == v.id, aceptaciones.c.usuario == str(usuario))).first(): continue
                out.append({"id": v.id, "tipo": t, "titulo": v.titulo, "version": v.version, "huella": v.huella})
        return out

    def aceptar(self, sistema, documento_ids, usuario, origen, nombre=None, sujeto=None, organizacion=None, rol=None, ip=None, agente=None):
        """Registra la aceptación de cada documento vigente indicado. Solo se aceptan versiones aprobadas y vigentes del sistema."""
        ahora, registradas = self.st.reloj(), []
        with self.st.eng.begin() as c:
            prev = c.execute(select(aceptaciones.c.cadena).order_by(aceptaciones.c.id.desc()).limit(1).with_for_update()).scalar() or "0" * 64
            for did in documento_ids:
                d = c.execute(select(documentos).where(documentos.c.id == int(did))).first()
                if d is None or d.sistema != sistema or d.estado != "aprobado": raise ValueError("Solo se acepta la versión vigente de los documentos del sistema")
                if c.execute(select(aceptaciones.c.id).where(aceptaciones.c.documento_id == d.id, aceptaciones.c.usuario == str(usuario))).first(): continue
                fila = dict(documento_id=d.id, sistema=sistema, tipo=d.tipo, version=d.version, huella=d.huella, usuario=str(usuario)[:254],
                            nombre=(str(nombre)[:200] if nombre else None), sujeto=(str(sujeto)[:200] if sujeto else None),
                            organizacion=(str(organizacion)[:200] if organizacion else None), rol=(str(rol)[:40] if rol else None),
                            ip=(str(ip)[:64] if ip else None), agente=(str(agente)[:300] if agente else None), origen=str(origen)[:40],
                            aceptado_en=ahora, prev=prev)
                fila["cadena"] = prev = hashlib.sha256("|".join(str(fila[k]) for k in (
                    "prev", "documento_id", "huella", "usuario", "sujeto", "ip", "aceptado_en")).encode()).hexdigest()
                registradas.append(c.execute(insert(aceptaciones).values(**fila).returning(aceptaciones.c.id)).scalar())
        return registradas

    def aceptaciones(self, sistema=None, limite=500):
        q = select(aceptaciones).order_by(aceptaciones.c.id.desc()).limit(limite)
        if sistema: q = q.where(aceptaciones.c.sistema == sistema)
        with self.st.eng.connect() as c:
            return [dict(r._mapping) for r in c.execute(q)]

    def cadena_integra(self):
        prev = "0" * 64
        with self.st.eng.connect() as c:
            for f in c.execute(select(aceptaciones).order_by(aceptaciones.c.id)):
                d = dict(f._mapping)
                esperado = hashlib.sha256("|".join(str(d[k]) for k in ("prev", "documento_id", "huella", "usuario", "sujeto", "ip", "aceptado_en"))
                                          .encode()).hexdigest()
                if d["prev"] != prev or d["cadena"] != esperado: return False
                prev = d["cadena"]
        return True


def registrar_rutas(app, st, lg, cuerpo, sistema_autenticado):
    from flask import jsonify, request

    @app.get("/admin/v1/legal/documentos")
    def l_documentos():
        return jsonify(documentos=lg.listar(request.args.get("sistema") or None), cadena_integra=lg.cadena_integra())

    @app.get("/admin/v1/legal/documentos/<int:did>")
    def l_documento(did):
        return jsonify(documento=lg.documento(did))

    @app.put("/admin/v1/legal/documentos/<int:did>")
    def l_guardar(did):
        d = cuerpo(); nuevo = lg.guardar(did, d.get("secciones"), d.get("autor", ""), d.get("observaciones"))
        with st.eng.begin() as c: st.log(c, None, "legal_documento_editado", f"#{did}→#{nuevo} por {d.get('autor', '')}")
        return jsonify(id=nuevo)

    @app.post("/admin/v1/legal/documentos/<int:did>/revision")
    def l_revision(did):
        lg.a_revision(did); return jsonify(ok=True)

    @app.post("/admin/v1/legal/documentos/<int:did>/devolver")
    def l_devolver(did):
        d = cuerpo(); lg.devolver(did, d.get("observaciones"), d.get("abogado")); return jsonify(ok=True)

    @app.post("/admin/v1/legal/documentos/<int:did>/aprobar")
    def l_aprobar(did):
        d = cuerpo(); doc = lg.aprobar(did, d.get("abogado"))
        with st.eng.begin() as c: st.log(c, None, "legal_documento_aprobado", f"#{did} {doc['sistema']}/{doc['tipo']} v{doc['version']} por {d.get('abogado')}")
        return jsonify(documento=doc)

    @app.get("/admin/v1/legal/vigente/<sistema>/<tipo>")
    def l_vigente(sistema, tipo):
        return jsonify(documento=lg.vigente(sistema, tipo))

    @app.get("/admin/v1/legal/abogados")
    def l_abogados():
        return jsonify(abogados=lg.abogados())

    @app.put("/admin/v1/legal/abogados/<usuario>")
    def l_guardar_abogado(usuario):
        d = cuerpo(); return jsonify(abogado=lg.guardar_abogado(usuario, d.get("nombre"), d.get("cedula"), d.get("inpreabogado"), d.get("correo"),
                                                                  d.get("activo", True)))

    @app.put("/admin/v1/legal/abogados/<usuario>/firma")
    def l_firma(usuario):
        d = cuerpo()
        try:
            png = base64.b64decode(str(d.get("png") or ""), validate=True)
        except (ValueError, TypeError) as e:
            raise ValueError("Firma inválida") from e
        lg.guardar_firma(usuario, png); return jsonify(ok=True)

    @app.get("/admin/v1/legal/pendientes/<sistema>")
    def l_pendientes(sistema):
        return jsonify(pendientes=lg.pendientes(sistema, request.args.get("usuario", ""), request.args.get("perfil", "usuario")))

    @app.post("/admin/v1/legal/aceptar/<sistema>")
    def l_aceptar(sistema):
        d = cuerpo()
        ids = lg.aceptar(sistema, d.get("documentos") or [], d.get("usuario"), d.get("origen") or sistema, d.get("nombre"), d.get("sujeto"),
                         d.get("organizacion"), d.get("rol"), d.get("ip"), d.get("agente"))
        return jsonify(aceptaciones=ids)

    @app.get("/admin/v1/legal/aceptaciones")
    def l_aceptaciones():
        return jsonify(aceptaciones=lg.aceptaciones(request.args.get("sistema") or None))

    # ---------------------------------------------------------------- los sistemas (Surtia, Quincena VE) piden y registran la aceptación
    @app.get("/sync/v1/<pid>/legal/pendientes")
    def s_legal_pendientes(pid):
        if not sistema_autenticado(pid): return jsonify(message="No autorizado"), 401
        pend = lg.pendientes(pid, request.args.get("usuario", ""), request.args.get("perfil", "usuario"))
        return jsonify(pendientes=[{**p, "documento": lg.documento(p["id"])} for p in pend])

    @app.post("/sync/v1/<pid>/legal/aceptar")
    def s_legal_aceptar(pid):
        if not sistema_autenticado(pid): return jsonify(message="No autorizado"), 401
        d = cuerpo()
        ids = lg.aceptar(pid, d.get("documentos") or [], d.get("usuario"), pid, d.get("nombre"), d.get("sujeto"), d.get("organizacion"),
                         d.get("rol"), d.get("ip"), d.get("agente"))
        return jsonify(aceptaciones=ids), 201
