"""Plantilla HTML de los correos de Contax (estilo de la aplicación: negro #191919, verde lima #87e64b, fondo claro).

Los clientes de correo no aceptan hojas de estilo ni SVG: todo va en estilos en línea, maquetado con tablas y con el
logo en PNG. Cada correo lleva además una versión de texto plano equivalente. Todo valor dinámico se escapa."""
from html import escape

FONDO, TINTA, TEXTO, SUAVE, LINEA, LIMA, LIMA_TINTA = "#f4f4f2", "#191919", "#303030", "#707070", "#e6e6e3", "#87e64b", "#1a4200"
FUENTE = "'Inter','Segoe UI',Helvetica,Arial,sans-serif"


def html(titulo, parrafos, base_url, nombre="Contax", lema="", boton=None, destacado=None, filas=(), nota=None, pie=None):
    """titulo: encabezado. parrafos: textos (se escapan). boton: (texto, url). destacado: (etiqueta, valor) en un recuadro
    (p. ej. una clave). filas: [(etiqueta, valor)] en tabla de datos. nota: aviso de seguridad al final."""
    e = escape
    logo = f"{base_url.rstrip('/')}/static/marca/icono-192.png"
    cuerpo = "".join(f'<p style="margin:0 0 16px;font-size:15px;line-height:1.6;color:{TEXTO}">{e(p)}</p>' for p in parrafos)
    if destacado:
        cuerpo += (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:8px 0 20px"><tr>'
                   f'<td style="background:{TINTA};border-radius:12px;padding:18px 20px">'
                   f'<div style="font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:#a3a3a3">{e(destacado[0])}</div>'
                   f'<div style="font-family:Menlo,Consolas,monospace;font-size:20px;font-weight:700;color:{LIMA};margin-top:6px;word-break:break-all">{e(destacado[1])}</div>'
                   f'</td></tr></table>')
    if filas:
        cuerpo += f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 20px;border-top:1px solid {LINEA}">'
        for k, v in filas:
            cuerpo += (f'<tr><td style="padding:10px 0;border-bottom:1px solid {LINEA};font-size:14px;color:{SUAVE}">{e(k)}</td>'
                       f'<td align="right" style="padding:10px 0;border-bottom:1px solid {LINEA};font-size:14px;font-weight:600;color:{TINTA}">{e(v)}</td></tr>')
        cuerpo += "</table>"
    if boton:
        cuerpo += (f'<table role="presentation" cellpadding="0" cellspacing="0" style="margin:8px 0 24px"><tr>'
                   f'<td style="background:{LIMA};border-radius:10px">'
                   f'<a href="{e(boton[1])}" style="display:inline-block;padding:14px 26px;font-family:{FUENTE};font-size:15px;font-weight:700;'
                   f'color:{LIMA_TINTA};text-decoration:none">{e(boton[0])}</a></td></tr></table>'
                   f'<p style="margin:0 0 16px;font-size:12px;line-height:1.5;color:{SUAVE}">Si el botón no funciona, copie este enlace en su navegador:<br>'
                   f'<span style="color:{TINTA};word-break:break-all">{e(boton[1])}</span></p>')
    if nota:
        cuerpo += (f'<div style="margin-top:8px;padding:14px 16px;background:{FONDO};border-radius:10px;font-size:13px;line-height:1.5;color:{SUAVE}">'
                   f'{e(nota)}</div>')
    pie = pie or "Mensaje automático: no responda a este correo. Si no reconoce esta acción, contacte de inmediato al administrador de su organización."
    return f"""<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light"><title>{e(titulo)}</title></head>
<body style="margin:0;padding:0;background:{FONDO};font-family:{FUENTE}">
<span style="display:none;max-height:0;overflow:hidden;opacity:0">{e(parrafos[0] if parrafos else titulo)}</span>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{FONDO}"><tr><td align="center" style="padding:32px 12px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:580px">
  <tr><td style="background:{TINTA};border-radius:16px 16px 0 0;padding:22px 28px">
    <table role="presentation" cellpadding="0" cellspacing="0"><tr>
      <td style="padding-right:12px"><img src="{e(logo)}" width="36" height="36" alt="{e(nombre)}" style="display:block;border:0;border-radius:9px"></td>
      <td><div style="font-size:20px;font-weight:800;color:#ffffff;letter-spacing:-.02em">{e(nombre)}</div>
          <div style="font-size:12px;color:#a3a3a3">{e(lema)}</div></td>
    </tr></table>
  </td></tr>
  <tr><td style="height:4px;background:{LIMA};font-size:0;line-height:0">&nbsp;</td></tr>
  <tr><td style="background:#ffffff;padding:32px 28px;border:1px solid {LINEA};border-top:0;border-radius:0 0 16px 16px">
    <h1 style="margin:0 0 18px;font-size:22px;line-height:1.3;color:{TINTA};font-weight:700">{e(titulo)}</h1>
    {cuerpo}
  </td></tr>
  <tr><td style="padding:20px 8px;text-align:center;font-size:12px;line-height:1.6;color:{SUAVE}">
    {e(pie)}<br><strong style="color:{TINTA}">{e(nombre)}</strong>® · una marca de Gigadevs Corp · Venezuela
  </td></tr>
</table></td></tr></table></body></html>"""


def texto(titulo, parrafos, boton=None, destacado=None, filas=(), nota=None, nombre="Contax"):
    """Versión de texto plano equivalente (para clientes sin HTML y filtros antispam)."""
    partes = [titulo, "=" * len(titulo), "", *[p + "\n" for p in parrafos]]
    if destacado: partes += [f"{destacado[0]}: {destacado[1]}", ""]
    if filas: partes += [f"  {k}: {v}" for k, v in filas] + [""]
    if boton: partes += [f"{boton[0]}:", boton[1], ""]
    if nota: partes += [nota, ""]
    partes += ["—", f"{nombre}® · una marca de Gigadevs Corp"]
    return "\n".join(partes)


def _bs(v) -> str:
    from decimal import Decimal
    return f"{Decimal(str(v)):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def factura_html(f: dict, base_url: str, intro=None) -> str:
    """Factura de Gigadevs (venta de licencias) lista para correo: tablas y estilos en línea, logos de Contax y Gigadevs."""
    e, em = escape, f["emisor"]
    base = base_url.rstrip("/")
    divisa = f["moneda"] != "VES"
    filas = ""
    for l in f["lineas"]:
        filas += (f'<tr><td style="padding:10px 8px;border-bottom:1px solid {LINEA};font-size:13px;color:{TEXTO}">{e(l["descripcion"])}{" (E)" if l["alicuota"] == "E" else ""}</td>'
                  f'<td align="right" style="padding:10px 8px;border-bottom:1px solid {LINEA};font-size:13px">{e(str(l["cantidad"]).rstrip("0").rstrip(".") or "1")}</td>'
                  f'<td align="right" style="padding:10px 8px;border-bottom:1px solid {LINEA};font-size:13px">{e(_bs(l["precio"]))}</td>'
                  f'<td align="right" style="padding:10px 8px;border-bottom:1px solid {LINEA};font-size:13px;font-weight:600">{e(_bs(l["base_bs"]))}</td></tr>')
    tot = [("Monto exento (E)", f["exento"])] if float(f["exento"]) else []
    tot += [(f"Base imponible alícuota general {e(str(f['iva_pct']).rstrip('0').rstrip('.'))}%", f["base"]), (f"IVA {e(str(f['iva_pct']).rstrip('0').rstrip('.'))}%", f["iva"])]
    totales = "".join(f'<tr><td style="padding:6px 8px;font-size:13px;color:{SUAVE}">{k}</td><td align="right" style="padding:6px 8px;font-size:13px">{e(_bs(v))}</td></tr>' for k, v in tot)
    totales += (f'<tr><td style="padding:10px 8px;border-top:2px solid {TINTA};font-size:16px;font-weight:800;color:{TINTA}">TOTAL (Bs)</td>'
                f'<td align="right" style="padding:10px 8px;border-top:2px solid {TINTA};font-size:16px;font-weight:800;color:{TINTA}">{e(_bs(f["total"]))}</td></tr>')
    if divisa and f.get("total_divisa"):
        totales += f'<tr><td style="padding:6px 8px;font-size:13px;color:{SUAVE}">Total equivalente ({e(f["moneda"])})</td><td align="right" style="padding:6px 8px;font-size:13px">{e(_bs(f["total_divisa"]))}</td></tr>'
    anulada = f.get("estado") == "Anulada"
    return f"""<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light">
<title>Factura {e(f['numero_completo'])}</title></head><body style="margin:0;padding:0;background:{FONDO};font-family:{FUENTE}">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{FONDO}"><tr><td align="center" style="padding:28px 10px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:680px">
 <tr><td style="background:{TINTA};border-radius:16px 16px 0 0;padding:20px 26px">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
   <td valign="middle"><table role="presentation" cellpadding="0" cellspacing="0"><tr>
     <td style="padding-right:10px"><img src="{e(base)}/static/marca/icono-192.png" width="38" height="38" alt="Contax" style="display:block;border:0;border-radius:9px"></td>
     <td><div style="font-size:20px;font-weight:800;color:#fff">Conta<span style="color:#87e64b">x</span></div><div style="font-size:11px;color:#a3a3a3">Tu empresa en orden. Tu fisco al día.</div></td></tr></table></td>
   <td align="right" valign="middle"><img src="{e(base)}/static/marca/gigadevs.png" width="58" height="58" alt="Gigadevs Corp" style="display:block;border:0"></td>
  </tr></table></td></tr>
 <tr><td style="height:4px;background:{LIMA};font-size:0;line-height:0">&nbsp;</td></tr>
 <tr><td style="background:#ffffff;padding:26px;border:1px solid {LINEA};border-top:0;border-radius:0 0 16px 16px">
  {f'<p style="margin:0 0 18px;font-size:14px;line-height:1.6;color:{TEXTO}">{e(intro)}</p>' if intro else ''}
  {'<div style="margin:0 0 16px;padding:10px 14px;border:2px solid #c8372d;border-radius:10px;color:#c8372d;font-weight:800;text-align:center">FACTURA ANULADA</div>' if anulada else ''}
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-bottom:2px solid {TINTA};padding-bottom:12px;margin-bottom:14px"><tr>
   <td valign="top" style="font-size:13px;line-height:1.55;color:{TEXTO}"><div style="font-size:16px;font-weight:800;color:{TINTA}">{e(em['razon_social'])}</div>
     RIF: <b>{e(em['rif'])}</b><br>Domicilio fiscal: {e(em['domicilio'])}{('<br>Teléfono: ' + e(em['telefono'])) if em.get('telefono') else ''}
     {('<br><b>Contribuyente Especial</b>') if em.get('contribuyente_especial') in ('1', 1, 'True') else ''}</td>
   <td valign="top" align="right" style="font-size:13px;line-height:1.6;color:{TEXTO}"><div style="font-size:20px;font-weight:800;letter-spacing:.04em;color:{TINTA}">FACTURA</div>
     N° <b>{e(f['numero_completo'])}</b><br><span style="display:inline-block;border:2px solid {TINTA};border-radius:8px;padding:2px 8px;font-weight:700">N° de Control: {e(f['numero_control'])}</span>
     <br>Fecha: {e('/'.join(reversed(f['fecha'].split('-'))))}</td></tr></table>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:16px"><tr>
   <td valign="top" width="50%" style="background:{FONDO};border-radius:10px;padding:12px;font-size:13px;line-height:1.55;color:{TEXTO}"><b>Adquirente</b><br>{e(f['cliente_nombre'])}<br>RIF/C.I.: {e(f['cliente_rif'])}{('<br>' + e(f['cliente_direccion'])) if f.get('cliente_direccion') else ''}</td>
   <td width="12"></td>
   <td valign="top" width="50%" style="background:{FONDO};border-radius:10px;padding:12px;font-size:13px;line-height:1.55;color:{TEXTO}"><b>Condiciones</b><br>Pago: Contado{(' · ' + e(f['forma_pago'])) if f.get('forma_pago') else ''}<br>
     Moneda: {e(f['moneda'])}{(' · Tasa BCV: ' + e(_bs(f['tasa_bcv'])) + ' Bs/' + e(f['moneda']) + (' (tasa oficial del dólar)' if f['moneda'] == 'USDT' else '')) if divisa else ''}{('<br>Orden: ' + e(f['orden_ref'])) if f.get('orden_ref') else ''}</td></tr></table>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
   <th align="left" style="padding:8px;font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:{SUAVE};border-bottom:1px solid {LINEA}">Descripción</th>
   <th align="right" style="padding:8px;font-size:11px;text-transform:uppercase;color:{SUAVE};border-bottom:1px solid {LINEA}">Cant.</th>
   <th align="right" style="padding:8px;font-size:11px;text-transform:uppercase;color:{SUAVE};border-bottom:1px solid {LINEA}">Precio unit. ({e(f['moneda'])})</th>
   <th align="right" style="padding:8px;font-size:11px;text-transform:uppercase;color:{SUAVE};border-bottom:1px solid {LINEA}">Monto (Bs)</th></tr>{filas}</table>
  <table role="presentation" cellpadding="0" cellspacing="0" align="right" style="margin-top:12px;min-width:300px">{totales}</table>
  <div style="clear:both"></div>
  <p style="margin:22px 0 0;font-size:11px;line-height:1.55;color:{SUAVE};border-top:1px dashed #bbb;padding-top:10px">
   {('Operación pactada en ' + e(f['moneda']) + '; el equivalente en bolívares se calculó a la tasa oficial del BCV vigente en la fecha de emisión. ') if divisa else ''}
   Imprenta autorizada: {e(em['imprenta_razon_social'])} · RIF {e(em['imprenta_rif'])} · {e(em['imprenta_providencia'])} · Números de control asignados hasta
   {e(em['control_prefijo'])}-{int(em['control_hasta'] or 0):08d}{(' el ' + e(em['fecha_asignacion'])) if em.get('fecha_asignacion') else ''}.<br>Huella del documento: {e(f['hash'][:16])}</p>
 </td></tr>
 <tr><td style="padding:18px 8px;text-align:center;font-size:12px;line-height:1.6;color:{SUAVE}">Conserve este correo: es su factura. Para cualquier consulta responda al soporte de Gigadevs Corp.<br>
   <strong style="color:{TINTA}">Contax</strong>® · una marca de Gigadevs Corp · Venezuela</td></tr>
</table></td></tr></table></body></html>"""


def factura_texto(f: dict) -> str:
    em = f["emisor"]
    lineas = "\n".join(f"  - {l['descripcion']}  x{l['cantidad']}  Bs {_bs(l['base_bs'])}" for l in f["lineas"])
    return (f"FACTURA N° {f['numero_completo']} · N° de Control {f['numero_control']} · Fecha {f['fecha']}\n"
            f"Emisor: {em['razon_social']} · RIF {em['rif']} · {em['domicilio']}\n"
            f"Adquirente: {f['cliente_nombre']} · RIF/C.I. {f['cliente_rif']}\n\n{lineas}\n\n"
            f"Base imponible: Bs {_bs(f['base'])}\nIVA {f['iva_pct']}%: Bs {_bs(f['iva'])}\nTOTAL: Bs {_bs(f['total'])}"
            + (f"\nEquivalente: {f['moneda']} {_bs(f['total_divisa'])} (tasa BCV {_bs(f['tasa_bcv'])})" if f["moneda"] != "VES" and f.get("total_divisa") else "")
            + f"\n\nImprenta autorizada: {em['imprenta_razon_social']} · RIF {em['imprenta_rif']} · {em['imprenta_providencia']}\n"
              "Conserve este correo: es su factura.\n—\nContax® · una marca de Gigadevs Corp")


def orden_compra_html(oc: dict, base_url: str, intro=None, titulo="ORDEN DE COMPRA", etiqueta="Documento de compra", pie=None, aviso=None) -> str:
    """Orden de compra con el MISMO modelo de la factura de Gigadevs, pero emitida por la empresa del cliente:
    emisor = la empresa (razón social, RIF, domicilio), destinatario = el proveedor o cliente, y sus condiciones."""
    e, em, d = escape, oc["emisor"], oc["destinatario"]
    base = base_url.rstrip("/")
    mon = oc["moneda"]
    filas = ""
    for l in oc["lineas"]:
        filas += (f'<tr><td style="padding:10px 8px;border-bottom:1px solid {LINEA};font-size:13px;color:{TEXTO}">{(e(l["codigo"]) + " · ") if l.get("codigo") else ""}{e(l["descripcion"])}{" (E)" if l["alicuota"] == "E" else ""}</td>'
                  f'<td align="right" style="padding:10px 8px;border-bottom:1px solid {LINEA};font-size:13px">{e(l["cantidad"])}{(" " + e(l["unidad"])) if l.get("unidad") else ""}</td>'
                  f'<td align="right" style="padding:10px 8px;border-bottom:1px solid {LINEA};font-size:13px">{e(_bs(l["precio"]))}</td>'
                  f'<td align="right" style="padding:10px 8px;border-bottom:1px solid {LINEA};font-size:13px;font-weight:600">{e(_bs(l["monto"]))}</td></tr>')
    tot = ([("Monto exento (E)", oc["exento"])] if float(oc["exento"]) else []) + [("Base imponible", oc["base"]), ("IVA", oc["iva"])]
    totales = "".join(f'<tr><td style="padding:6px 8px;font-size:13px;color:{SUAVE}">{k}</td><td align="right" style="padding:6px 8px;font-size:13px">{e(_bs(v))}</td></tr>' for k, v in tot)
    totales += (f'<tr><td style="padding:10px 8px;border-top:2px solid {TINTA};font-size:16px;font-weight:800;color:{TINTA}">TOTAL ({e(mon)})</td>'
                f'<td align="right" style="padding:10px 8px;border-top:2px solid {TINTA};font-size:16px;font-weight:800;color:{TINTA}">{e(_bs(oc["total"]))}</td></tr>')
    if mon != "VES":
        totales += f'<tr><td style="padding:6px 8px;font-size:13px;color:{SUAVE}">Equivalente en Bs (tasa BCV {e(_bs(oc["tasa_bcv"]))})</td><td align="right" style="padding:6px 8px;font-size:13px">{e(_bs(oc["total_bs"]))}</td></tr>'
    sello = {"Anulada": "#c8372d", "Rechazada": "#c8372d"}.get(oc["estado"])
    fecha = lambda f: "/".join(reversed(str(f).split("-"))) if f else "—"
    cond = [("Fecha de entrega", fecha(oc.get("fecha_entrega"))), ("Condición de pago", oc.get("condicion_pago") or "—"),
            ("Lugar de entrega", oc.get("lugar_entrega") or em.get("direccion") or "—"), ("Moneda", mon)]
    return f"""<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light">
<title>{e(titulo.capitalize())} {e(oc['numero_txt'])} · {e(em['razon_social'])}</title></head><body style="margin:0;padding:0;background:{FONDO};font-family:{FUENTE}">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{FONDO}"><tr><td align="center" style="padding:28px 10px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:680px">
 <tr><td style="background:{TINTA};border-radius:16px 16px 0 0;padding:20px 26px">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
   <td valign="middle"><table role="presentation" cellpadding="0" cellspacing="0"><tr>{f'<td style="padding-right:12px"><img src="{em["logo"]}" alt="" style="display:block;max-height:46px;max-width:120px;border-radius:8px;background:#fff;padding:3px"></td>' if em.get('logo') else ''}
     <td><div style="font-size:20px;font-weight:800;color:#fff">{e(em['razon_social'])}</div><div style="font-size:12px;color:#a3a3a3">RIF {e(em['rif'])}</div></td></tr></table></td>
   <td align="right" valign="middle"><div style="font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:#a3a3a3">{e(etiqueta)}</div>
     <div style="font-size:18px;font-weight:800;color:{LIMA}">{e(titulo)}</div></td></tr></table></td></tr>
 <tr><td style="height:4px;background:{LIMA};font-size:0;line-height:0">&nbsp;</td></tr>
 <tr><td style="background:#ffffff;padding:26px;border:1px solid {LINEA};border-top:0;border-radius:0 0 16px 16px">
  {f'<p style="margin:0 0 18px;font-size:14px;line-height:1.6;color:{TEXTO}">{e(intro)}</p>' if intro else ''}
  {f'<div style="margin:0 0 16px;padding:10px 14px;border:2px solid {sello};border-radius:10px;color:{sello};font-weight:800;text-align:center">ORDEN {e(oc["estado"].upper())}</div>' if sello else ''}
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-bottom:2px solid {TINTA};padding-bottom:12px;margin-bottom:14px"><tr>
   <td valign="top" style="font-size:13px;line-height:1.55;color:{TEXTO}"><div style="font-size:16px;font-weight:800;color:{TINTA}">{e(em['razon_social'])}</div>
     RIF: <b>{e(em['rif'])}</b>{('<br>Domicilio fiscal: ' + e(em['direccion'])) if em.get('direccion') else ''}{('<br>Teléfono: ' + e(em['telefono'])) if em.get('telefono') else ''}
     {('<br><b>Contribuyente Especial</b>') if em.get('contribuyente_especial') else ''}</td>
   <td valign="top" align="right" style="font-size:13px;line-height:1.6;color:{TEXTO}"><div style="font-size:20px;font-weight:800;letter-spacing:.04em;color:{TINTA}">{e(titulo)}</div>
     N° <b>{e(oc['numero_txt'])}</b><br><span style="display:inline-block;border:2px solid {TINTA};border-radius:8px;padding:2px 8px;font-weight:700">Estado: {e(oc['estado'])}</span>
     <br>Fecha: {e(fecha(oc['fecha']))}</td></tr></table>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:16px"><tr>
   <td valign="top" width="50%" style="background:{FONDO};border-radius:10px;padding:12px;font-size:13px;line-height:1.55;color:{TEXTO}"><b>{'Proveedor' if oc['tipo_destinatario'] == 'proveedor' else 'Cliente'}</b><br>{e(d['nombre'])}<br>RIF/C.I.: {e(d['rif'])}
     {('<br>' + e(d['direccion'])) if d.get('direccion') else ''}{('<br>Tel.: ' + e(d['telefono'])) if d.get('telefono') else ''}{('<br>' + e(d['email'])) if d.get('email') else ''}</td>
   <td width="12"></td>
   <td valign="top" width="50%" style="background:{FONDO};border-radius:10px;padding:12px;font-size:13px;line-height:1.55;color:{TEXTO}"><b>Condiciones</b>
     {''.join(f'<br>{k}: {e(str(v))}' for k, v in cond)}{(' · Tasa BCV: ' + e(_bs(oc['tasa_bcv'])) + ' Bs/' + e(mon)) if mon != 'VES' else ''}</td></tr></table>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
   <th align="left" style="padding:8px;font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:{SUAVE};border-bottom:1px solid {LINEA}">Descripción</th>
   <th align="right" style="padding:8px;font-size:11px;text-transform:uppercase;color:{SUAVE};border-bottom:1px solid {LINEA}">Cant.</th>
   <th align="right" style="padding:8px;font-size:11px;text-transform:uppercase;color:{SUAVE};border-bottom:1px solid {LINEA}">Precio unit. ({e(mon)})</th>
   <th align="right" style="padding:8px;font-size:11px;text-transform:uppercase;color:{SUAVE};border-bottom:1px solid {LINEA}">Monto ({e(mon)})</th></tr>{filas}</table>
  <table role="presentation" cellpadding="0" cellspacing="0" align="right" style="margin-top:12px;min-width:300px">{totales}</table>
  <div style="clear:both"></div>
  {f'<p style="margin:18px 0 0;font-size:13px;line-height:1.55;color:{TEXTO}"><b>Observaciones:</b> {e(oc["observaciones"])}</p>' if oc.get('observaciones') else ''}
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:34px"><tr>
   <td width="45%" style="border-top:1px solid {TINTA};padding-top:6px;font-size:12px;color:{SUAVE};text-align:center">Elaborado por: {e(oc.get('creado_por') or '')}</td><td></td>
   <td width="45%" style="border-top:1px solid {TINTA};padding-top:6px;font-size:12px;color:{SUAVE};text-align:center">Aceptado por el {'proveedor' if oc['tipo_destinatario'] == 'proveedor' else 'cliente'}</td></tr></table>
  <p style="margin:22px 0 0;font-size:11px;line-height:1.55;color:{SUAVE};border-top:1px dashed #bbb;padding-top:10px">
   {e(aviso) if aviso else 'Esta orden de compra no es una factura ni sustituye la documentación fiscal: la factura debe emitirse a nombre de ' + e(em['razon_social']) + ', RIF ' + e(em['rif']) + ', con el N° de esta orden como referencia.'}{(' Montos en ' + e(mon) + '; el equivalente en bolívares se calculó a la tasa oficial del BCV del día de emisión.') if mon != 'VES' else ''}</p>
 </td></tr>
 <tr><td style="padding:18px 8px;text-align:center;font-size:12px;line-height:1.6;color:{SUAVE}">{e(pie) if pie else 'Para confirmar esta orden responda por WhatsApp <b>CONFIRMAR ' + e(str(oc['numero'])) + '</b> o escriba a ' + e(em.get('email') or em['razon_social']) + '.'}<br>
   Generado con <strong style="color:{TINTA}">Contax</strong>® · una marca de Gigadevs Corp · <img src="{e(base)}/static/marca/icono-192.png" width="14" height="14" alt="" style="vertical-align:-2px;border:0;border-radius:3px"></td></tr>
</table></td></tr></table></body></html>"""


def _usd(v) -> str:
    return "$" + _bs(v)


def presupuesto_html(p: dict, em: dict, base_url: str, intro=None) -> str:
    """Presupuesto de Gigadevs con el formato de Contax: cabecera negra con el logo de Contax y de Gigadevs, franja lima,
    emisor y cliente, líneas con su detalle, subtotal, IVA y total en USD, vigencia y condiciones."""
    e = escape
    base = base_url.rstrip("/")
    filas = ""
    for l in p["lineas"]:
        det = f'<div style="font-size:12px;color:{SUAVE};margin-top:3px;line-height:1.45">{e(l["detalle"])}</div>' if l.get("detalle") else ""
        filas += (f'<tr><td style="padding:12px 8px;border-bottom:1px solid {LINEA};font-size:13px;color:{TEXTO}"><b>{e(l["descripcion"])}</b>{" (E)" if l["alicuota"] == "E" else ""}{det}</td>'
                  f'<td align="right" style="padding:12px 8px;border-bottom:1px solid {LINEA};font-size:13px;vertical-align:top">{e(l["cantidad"])}</td>'
                  f'<td align="right" style="padding:12px 8px;border-bottom:1px solid {LINEA};font-size:13px;vertical-align:top">{e(_usd(l["precio"]))}</td>'
                  f'<td align="right" style="padding:12px 8px;border-bottom:1px solid {LINEA};font-size:13px;font-weight:700;vertical-align:top">{e(_usd(l["monto"]))}</td></tr>')
    pct = str(p["iva_pct"]).rstrip("0").rstrip(".")
    totales = "".join(f'<tr><td style="padding:6px 8px;font-size:13px;color:{SUAVE}">{k}</td><td align="right" style="padding:6px 8px;font-size:13px">{e(_usd(v))}</td></tr>'
                      for k, v in (("Subtotal (sin IVA)", p["subtotal"]), (f"IVA {pct}%", p["iva"])))
    totales += (f'<tr><td style="padding:10px 8px;border-top:2px solid {TINTA};font-size:17px;font-weight:800;color:{TINTA}">TOTAL (USD)</td>'
                f'<td align="right" style="padding:10px 8px;border-top:2px solid {TINTA};font-size:17px;font-weight:800;color:{TINTA}">{e(_usd(p["total"]))}</td></tr>')
    fecha = lambda f: "/".join(reversed(str(f).split("-")))
    periodo = {"mensual": "Mensual", "anual": "Anual (12 meses)", "unico": "Pago único"}.get(p.get("periodo"), p.get("periodo") or "—")
    cond = (p.get("condiciones") or "").strip()
    cond_html = "".join(f'<li style="margin:3px 0">{e(x.strip("-• "))}</li>' for x in cond.splitlines() if x.strip())
    sello = {"Aceptado": "#188038", "Rechazado": "#c8372d", "Anulado": "#c8372d"}.get(p.get("estado"))
    return f"""<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light">
<title>Presupuesto {e(p['numero_txt'])} · {e(p['cliente_nombre'])}</title></head><body style="margin:0;padding:0;background:{FONDO};font-family:{FUENTE}">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{FONDO}"><tr><td align="center" style="padding:28px 10px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:720px">
 <tr><td style="background:{TINTA};border-radius:16px 16px 0 0;padding:20px 26px">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
   <td valign="middle"><table role="presentation" cellpadding="0" cellspacing="0"><tr>
     <td style="padding-right:10px"><img src="{e(base)}/static/marca/icono-192.png" width="40" height="40" alt="Contax" style="display:block;border:0;border-radius:9px"></td>
     <td><div style="font-size:21px;font-weight:800;color:#fff">Conta<span style="color:{LIMA}">x</span><sup style="font-size:10px">®</sup></div><div style="font-size:11px;color:#a3a3a3">Tu empresa en orden. Tu fisco al día.</div></td></tr></table></td>
   <td align="right" valign="middle"><img src="{e(base)}/static/marca/gigadevs.png" width="58" height="58" alt="Gigadevs Corp" style="display:block;border:0"></td>
  </tr></table></td></tr>
 <tr><td style="height:4px;background:{LIMA};font-size:0;line-height:0">&nbsp;</td></tr>
 <tr><td style="background:#ffffff;padding:26px;border:1px solid {LINEA};border-top:0;border-radius:0 0 16px 16px">
  {f'<p style="margin:0 0 18px;font-size:14px;line-height:1.6;color:{TEXTO}">{e(intro)}</p>' if intro else ''}
  {f'<div style="margin:0 0 16px;padding:10px 14px;border:2px solid {sello};border-radius:10px;color:{sello};font-weight:800;text-align:center">PRESUPUESTO {e(p["estado"].upper())}</div>' if sello else ''}
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-bottom:2px solid {TINTA};padding-bottom:12px;margin-bottom:14px"><tr>
   <td valign="top" style="font-size:13px;line-height:1.55;color:{TEXTO}"><div style="font-size:16px;font-weight:800;color:{TINTA}">{e(em.get('razon_social') or 'Gigadevs Corp')}</div>
     {('RIF: <b>' + e(em['rif']) + '</b><br>') if em.get('rif') else ''}{e(em.get('domicilio') or 'Venezuela')}{('<br>Tel.: ' + e(em['telefono'])) if em.get('telefono') else ''}{('<br>' + e(em['correo'])) if em.get('correo') else ''}</td>
   <td valign="top" align="right" style="font-size:13px;line-height:1.6;color:{TEXTO}"><div style="font-size:22px;font-weight:800;letter-spacing:.04em;color:{TINTA}">PRESUPUESTO</div>
     N° <b>{e(p['numero_txt'])}</b><br>Fecha: {e(fecha(p['fecha']))}<br><span style="display:inline-block;border:2px solid {TINTA};border-radius:8px;padding:2px 8px;font-weight:700">Válido hasta {e(fecha(p['valido_hasta']))}</span></td></tr></table>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:16px"><tr>
   <td valign="top" width="50%" style="background:{FONDO};border-radius:10px;padding:12px;font-size:13px;line-height:1.55;color:{TEXTO}"><b>Cliente</b><br>{e(p['cliente_nombre'])}
     {('<br>RIF: ' + e(p['cliente_rif'])) if p.get('cliente_rif') else ''}{('<br>' + e(p['cliente_direccion'])) if p.get('cliente_direccion') else ''}
     {('<br>Atención: ' + e(p['atencion'])) if p.get('atencion') else ''}{('<br>' + e(p['cliente_correo'])) if p.get('cliente_correo') else ''}{('<br>Tel.: ' + e(p['cliente_telefono'])) if p.get('cliente_telefono') else ''}</td>
   <td width="12"></td>
   <td valign="top" width="50%" style="background:{FONDO};border-radius:10px;padding:12px;font-size:13px;line-height:1.55;color:{TEXTO}"><b>Condiciones</b><br>Período: {e(periodo)}<br>Moneda: USD
     <br>Pago en USDT (Binance Pay) o en bolívares a la tasa BCV del día de pago</td></tr></table>
  {f'<div style="font-size:16px;font-weight:800;color:{TINTA};margin:4px 0 8px">{e(p["titulo"])}</div>' if p.get('titulo') else ''}
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
   <th align="left" style="padding:8px;font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:{SUAVE};border-bottom:1px solid {LINEA}">Descripción</th>
   <th align="right" style="padding:8px;font-size:11px;text-transform:uppercase;color:{SUAVE};border-bottom:1px solid {LINEA}">Cant.</th>
   <th align="right" style="padding:8px;font-size:11px;text-transform:uppercase;color:{SUAVE};border-bottom:1px solid {LINEA}">Precio unit.</th>
   <th align="right" style="padding:8px;font-size:11px;text-transform:uppercase;color:{SUAVE};border-bottom:1px solid {LINEA}">Monto</th></tr>{filas}</table>
  <table role="presentation" cellpadding="0" cellspacing="0" align="right" style="margin-top:12px;min-width:320px">{totales}</table>
  <div style="clear:both"></div>
  {f'<div style="margin-top:20px;font-size:13px;color:{TEXTO}"><b>Condiciones de la propuesta</b><ul style="margin:6px 0 0;padding-left:18px;line-height:1.5">{cond_html}</ul></div>' if cond_html else ''}
  <p style="margin:22px 0 0;font-size:11px;line-height:1.55;color:{SUAVE};border-top:1px dashed #bbb;padding-top:10px">
   Este presupuesto no es una factura. Al aceptarlo, Gigadevs Corp emite la factura fiscal correspondiente y, para licencias, envía la clave de activación por correo.
   Precios en dólares estadounidenses; el IVA se calcula a la alícuota vigente.</p>
 </td></tr>
 <tr><td style="padding:18px 8px;text-align:center;font-size:12px;line-height:1.6;color:{SUAVE}">Para aceptar, responda este correo o escríbanos por WhatsApp.<br>
   <strong style="color:{TINTA}">Contax</strong>® · una marca registrada de Gigadevs Corp · Venezuela</td></tr>
</table></td></tr></table></body></html>"""


def presupuesto_texto(p: dict, em: dict) -> str:
    lineas = "\n".join(f"  - {l['descripcion']}  x{l['cantidad']}  {_usd(l['monto'])}" for l in p["lineas"])
    return (f"PRESUPUESTO {p['numero_txt']} · Fecha {p['fecha']} · Válido hasta {p['valido_hasta']}\n"
            f"Emisor: {em.get('razon_social') or 'Gigadevs Corp'}{(' · RIF ' + em['rif']) if em.get('rif') else ''}\n"
            f"Cliente: {p['cliente_nombre']}{(' · RIF ' + p['cliente_rif']) if p.get('cliente_rif') else ''}\n\n{lineas}\n\n"
            f"Subtotal: {_usd(p['subtotal'])}\nIVA {p['iva_pct']}%: {_usd(p['iva'])}\nTOTAL: {_usd(p['total'])}\n—\nContax® · una marca de Gigadevs Corp")


def contrato_html(c: dict, clausulas, em: dict, base_url: str, extra="") -> str:
    """Contrato de licencia y servicios con el formato de Contax. `extra`: HTML adicional (formulario de aceptación, barra)."""
    import time as _t
    e = escape
    base = base_url.rstrip("/")
    fecha = lambda f: "/".join(reversed(str(f).split("-"))) if f else "—"
    cuerpo = "".join(f'<h3 style="margin:22px 0 6px;font-size:14px;letter-spacing:.02em;color:{TINTA}">{e(t)}</h3>'
                     + "".join(f'<p style="margin:0 0 8px;font-size:13px;line-height:1.65;color:{TEXTO};text-align:justify">{e(p)}</p>' for p in ps)
                     for t, ps in clausulas)
    firmado = c.get("estado") == "Firmado"
    constancia = (f'<div style="margin-top:18px;padding:14px;border:2px solid #188038;border-radius:12px;background:#eef9e6;font-size:12px;line-height:1.6;color:{TEXTO}">'
                  f'<b style="color:#188038">ACEPTADO ELECTRÓNICAMENTE</b> por {e(c.get("firma_nombre") or "")}, C.I. {e(c.get("firma_ci") or "")}, '
                  f'el {e(_t.strftime("%d/%m/%Y %H:%M UTC", _t.gmtime(float(c["firmado_en"]))))} desde la IP {e(c.get("firma_ip") or "—")}.<br>'
                  f'Huella SHA-256 del texto aceptado: <code style="word-break:break-all">{e(c.get("firma_hash") or "")}</code><br>'
                  'Mensaje de datos con validez conforme al Decreto con Fuerza de Ley sobre Mensajes de Datos y Firmas Electrónicas.</div>') if firmado else ""
    sello = {"Anulado": "#c8372d"}.get(c.get("estado"))
    return f"""<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light">
<title>Contrato {e(c['numero_txt'])} · {e(c['cliente_nombre'])}</title></head><body style="margin:0;padding:0;background:{FONDO};font-family:{FUENTE}">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{FONDO}"><tr><td align="center" style="padding:28px 10px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:760px">
 <tr><td style="background:{TINTA};border-radius:16px 16px 0 0;padding:20px 26px">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
   <td valign="middle"><table role="presentation" cellpadding="0" cellspacing="0"><tr>
     <td style="padding-right:10px"><img src="{e(base)}/static/marca/icono-192.png" width="40" height="40" alt="Contax" style="display:block;border:0;border-radius:9px"></td>
     <td><div style="font-size:21px;font-weight:800;color:#fff">Conta<span style="color:{LIMA}">x</span><sup style="font-size:10px">®</sup></div><div style="font-size:11px;color:#a3a3a3">Tu empresa en orden. Tu fisco al día.</div></td></tr></table></td>
   <td align="right" valign="middle"><img src="{e(base)}/static/marca/gigadevs.png" width="58" height="58" alt="Gigadevs Corp" style="display:block;border:0"></td>
  </tr></table></td></tr>
 <tr><td style="height:4px;background:{LIMA};font-size:0;line-height:0">&nbsp;</td></tr>
 <tr><td style="background:#ffffff;padding:30px;border:1px solid {LINEA};border-top:0;border-radius:0 0 16px 16px">
  {f'<div style="margin:0 0 16px;padding:10px 14px;border:2px solid {sello};border-radius:10px;color:{sello};font-weight:800;text-align:center">CONTRATO ANULADO</div>' if sello else ''}
  <div style="text-align:center;border-bottom:2px solid {TINTA};padding-bottom:14px;margin-bottom:16px">
   <div style="font-size:21px;font-weight:800;letter-spacing:.03em;color:{TINTA}">CONTRATO DE LICENCIA DE USO Y PRESTACIÓN DE SERVICIOS</div>
   <div style="font-size:13px;color:{SUAVE};margin-top:4px">N° <b style="color:{TINTA}">{e(c['numero_txt'])}</b> · Presupuesto {e(c.get('presupuesto_txt') or '—')} · Vigencia {e(fecha(c['inicio']))} al {e(fecha(c['fin']))}</div></div>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:6px"><tr>
   <td valign="top" width="50%" style="background:{FONDO};border-radius:10px;padding:12px;font-size:13px;line-height:1.55;color:{TEXTO}"><b>GIGADEVS</b><br>{e(em.get('razon_social') or 'Gigadevs Corp')}
     {('<br>RIF ' + e(em['rif'])) if em.get('rif') else ''}<br>{e(em.get('domicilio') or 'Venezuela')}{('<br>' + e(em['correo'])) if em.get('correo') else ''}</td>
   <td width="12"></td>
   <td valign="top" width="50%" style="background:{FONDO};border-radius:10px;padding:12px;font-size:13px;line-height:1.55;color:{TEXTO}"><b>EL CLIENTE</b><br>{e(c['cliente_nombre'])}
     <br>RIF {e(c.get('cliente_rif') or '—')}<br>{e(c.get('cliente_domicilio') or '—')}<br>Rep.: {e(c.get('representante') or '—')}, C.I. {e(c.get('representante_ci') or '—')}</td></tr></table>
  {cuerpo}
  <p style="margin:18px 0 0;font-size:13px;line-height:1.65;color:{TEXTO}">Se hacen dos ejemplares de un mismo tenor y a un solo efecto, o se acepta electrónicamente, en la fecha indicada.</p>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:40px"><tr>
   <td width="45%" style="border-top:1px solid {TINTA};padding-top:6px;font-size:12px;color:{SUAVE};text-align:center">Por GIGADEVS<br>{e(em.get('razon_social') or 'Gigadevs Corp')}</td><td></td>
   <td width="45%" style="border-top:1px solid {TINTA};padding-top:6px;font-size:12px;color:{SUAVE};text-align:center">Por EL CLIENTE<br>{e(c.get('representante') or '')} · C.I. {e(c.get('representante_ci') or '')}</td></tr></table>
  {constancia}
  {extra}
  <p style="margin:22px 0 0;font-size:11px;line-height:1.55;color:{SUAVE};border-top:1px dashed #bbb;padding-top:10px">Textos legales versión {e(c.get('version_legal') or '')}.
   Forman parte de este contrato el presupuesto {e(c.get('presupuesto_txt') or '')}, los Términos y condiciones y la Política de privacidad de Contax® publicados en {e(base)}/legal.</p>
 </td></tr>
 <tr><td style="padding:18px 8px;text-align:center;font-size:12px;line-height:1.6;color:{SUAVE}"><strong style="color:{TINTA}">Contax</strong>® · una marca registrada de Gigadevs Corp · Venezuela</td></tr>
</table></td></tr></table></body></html>"""
