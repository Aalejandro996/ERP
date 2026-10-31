"""Migración desde otros sistemas: preparar (borrador para validar) → aplicar (escribir en las tablas definitivas).
Los saldos iniciales migrados (inventario, CxC, CxP) NO generan asientos al cargarse ni entran a los libros de compras/ventas
del período (ya están en los libros del sistema anterior); el contador los lleva a la contabilidad con un único asiento de
apertura desde «Cuadre de libros»."""
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from .auditoria import registrar
from .calculos import migracion as MG
from .extensions import db
from .models import Cuenta
from .models2 import DocumentoCxC, DocumentoCxP, Producto
from .models_compras import Migracion, Proveedor
from .models_fact import ClienteFact
from .tiempo import hoy_ve

REF_SALDO_INICIAL = "Saldo inicial (migración)"


def preparar(eid, u, contenido: bytes, nombre_archivo: str, tipo=None, sistema=None) -> list[Migracion]:
    """Lee el archivo (todas sus hojas), detecta qué contiene cada una y deja un BORRADOR por hoja para validar."""
    hojas = MG.leer_hojas(contenido, nombre_archivo)
    if not hojas: raise ValueError("El archivo no tiene datos")
    out = []
    for hoja, filas in hojas:
        det = MG.detectar(filas, f"{nombre_archivo} {hoja}")
        t = tipo or det["tipo"]
        if t is None:
            if len(hojas) == 1: raise ValueError("No se reconocieron las columnas. Indique qué contiene el archivo o use la plantilla.")
            continue
        if t != det["tipo"]:   # el usuario forzó el tipo: se vuelve a mapear con los nombres de ESE tipo
            det = next((dict(fila_cab=r, columnas=MG._mapear(f, MG.CAMPOS[t])) for r, f in enumerate(filas[:15])
                        if all(k in MG._mapear(f, MG.CAMPOS[t]) for k in MG.OBLIGATORIOS[t])), None) or {}
            if not det: raise ValueError(f"La hoja «{hoja}» no tiene las columnas mínimas para {MG.TIPOS[t]}: {', '.join(MG.OBLIGATORIOS[t])}")
            det["sistema"] = MG.detectar(filas)["sistema"]
        m = Migracion(empresa_id=eid, archivo=nombre_archivo[:200], hoja=hoja[:80], sistema=sistema or det["sistema"], tipo=t, creado_por=u.username,
                      filas=dict(columnas={k: filas[det["fila_cab"]][v] for k, v in det["columnas"].items()},
                                 filas=MG.normalizar(t, filas, det["fila_cab"], det["columnas"])))
        marcar_existentes(eid, m)
        db.session.add(m); out.append(m)
    if not out: raise ValueError("Ninguna hoja tiene columnas reconocibles")
    registrar(u.username, "MIGRACION_PREPARADA", f"empresa={eid} archivo={nombre_archivo[:60]} hojas={len(out)}")
    db.session.commit()
    return out


_CLAVE = {"clientes": "rif", "proveedores": "rif", "productos": "codigo", "plan_cuentas": "codigo", "cxc": "numero", "cxp": "numero"}


def _existentes(eid, tipo) -> set:
    q = {"clientes": select(ClienteFact.rif).where(ClienteFact.empresa_id == eid), "proveedores": select(Proveedor.rif).where(Proveedor.empresa_id == eid),
         "productos": select(Producto.codigo).where(Producto.empresa_id == eid), "plan_cuentas": select(Cuenta.codigo).where(Cuenta.empresa_id == eid),
         "cxc": select(DocumentoCxC.numero_documento).where(DocumentoCxC.empresa_id == eid),
         "cxp": select(DocumentoCxP.numero_documento).where(DocumentoCxP.empresa_id == eid)}[tipo]
    return set(db.session.execute(q).scalars())


def marcar_existentes(eid, m: Migracion):
    ya = _existentes(eid, m.tipo)
    filas = []
    for f in m.filas["filas"]:
        f = dict(f)
        if f["estado"] in ("ok", "existe"):
            existe = f["datos"].get(_CLAVE[m.tipo]) in ya
            f["estado"] = "existe" if existe else "ok"
        filas.append(f)
    m.filas = {**m.filas, "filas": filas}


def resumen(m: Migracion) -> dict:
    c = {"ok": 0, "existe": 0, "error": 0}
    for f in m.filas["filas"]: c[f["estado"]] = c.get(f["estado"], 0) + 1
    return c


def aplicar(eid, u, m: Migracion, actualizar=False, excluir=()) -> str:
    """Carga las filas válidas (y, si se pide, actualiza las que ya existían). `excluir`: números de fila que el cliente
    desmarcó al validar. Todo en UNA transacción: o entra todo lo válido o nada."""
    from .servicios import entrada_inventario
    if m.estado != "Borrador": raise ValueError("Esta migración ya fue procesada")
    marcar_existentes(eid, m)
    nuevos = act = 0
    hoy = hoy_ve()
    filas = [f for f in m.filas["filas"] if f["estado"] in ("ok", "existe") and f.get("fila") not in excluir]
    if m.tipo == "plan_cuentas":
        from .web import _guardar_cuentas
        cs = [f["datos"] for f in filas if f["estado"] == "ok" or actualizar]
        nuevos, act = _guardar_cuentas(eid, cs, actualizar) if cs else (0, 0)
    for f in filas if m.tipo != "plan_cuentas" else []:
        d, existe = f["datos"], f["estado"] == "existe"
        if existe and not actualizar: continue
        if m.tipo in ("clientes", "proveedores"):
            Modelo = ClienteFact if m.tipo == "clientes" else Proveedor
            x = db.session.execute(select(Modelo).where(Modelo.empresa_id == eid, Modelo.rif == d["rif"])).scalar_one_or_none() or Modelo(empresa_id=eid, rif=d["rif"])
            x.razon_social = d["nombre"]
            for k in ("direccion", "telefono", "email"):
                if d.get(k): setattr(x, k, d[k])
            db.session.add(x)
        elif m.tipo == "productos":
            p = db.session.get(Producto, (eid, d["codigo"]))
            if p is None:
                db.session.add(Producto(empresa_id=eid, codigo=d["codigo"], nombre=d["nombre"], metodo_costeo="Promedio",
                                        precio_venta=Decimal(d["precio"]), alicuota_iva=d["alicuota"])); db.session.flush()
                if Decimal(d["existencia"]) > 0:
                    entrada_inventario(eid, u, d["codigo"], Decimal(d["existencia"]), Decimal(d["costo"]), hoy, REF_SALDO_INICIAL, commit=False, contabilizar=False)
            else:
                p.nombre, p.precio_venta, p.alicuota_iva = d["nombre"], Decimal(d["precio"]) or p.precio_venta, d["alicuota"]
        else:
            if existe: continue                     # un documento ya registrado no se pisa
            comun = dict(empresa_id=eid, numero_documento=d["numero"], fecha_emision=date.fromisoformat(d["emision"]), fecha_vencimiento=date.fromisoformat(d["vencimiento"]),
                         monto=Decimal(d["monto"]), saldo_pendiente=Decimal(d["saldo"]), estado="Pendiente",
                         origen="migracion")
            if m.tipo == "cxc": db.session.add(DocumentoCxC(deudor_nombre=d["nombre"], deudor_rif=d.get("rif"), **comun))
            else: db.session.add(DocumentoCxP(proveedor_nombre=d["nombre"], proveedor_rif=d.get("rif"), numero_control=d.get("control"), base_imponible=Decimal(d["monto"]), **comun))
        if existe: act += 1
        else: nuevos += 1
    m.estado = "Aplicada"
    m.resultado = f"{nuevos} registro(s) nuevo(s), {act} actualizado(s), {resumen(m)['error']} con error no cargado(s)"
    registrar(u.username, "MIGRACION_APLICADA", f"empresa={eid} migracion={m.id} {m.tipo} {m.resultado}")
    db.session.commit()
    return m.resultado
