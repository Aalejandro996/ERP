"""Plantilla HTML de los correos de ERP (estilo de la aplicación: negro #191919, verde lima #87e64b, fondo claro).

Los clientes de correo no aceptan hojas de estilo ni SVG: todo va en estilos en línea, maquetado con tablas y con el
logo en PNG. Cada correo lleva además una versión de texto plano equivalente. Todo valor dinámico se escapa."""
from html import escape

FONDO, TINTA, TEXTO, SUAVE, LINEA, LIMA, LIMA_TINTA = "#f4f4f2", "#191919", "#303030", "#707070", "#e6e6e3", "#87e64b", "#1a4200"
FUENTE = "'Inter','Segoe UI',Helvetica,Arial,sans-serif"


def html(titulo, parrafos, base_url, nombre="ERP", lema="", boton=None, destacado=None, filas=(), nota=None, pie=None):
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
    {e(pie)}<br><strong style="color:{TINTA}">{e(nombre)}</strong> · un producto de Gigadevs Corp · Venezuela
  </td></tr>
</table></td></tr></table></body></html>"""


def texto(titulo, parrafos, boton=None, destacado=None, filas=(), nota=None, nombre="ERP"):
    """Versión de texto plano equivalente (para clientes sin HTML y filtros antispam)."""
    partes = [titulo, "=" * len(titulo), "", *[p + "\n" for p in parrafos]]
    if destacado: partes += [f"{destacado[0]}: {destacado[1]}", ""]
    if filas: partes += [f"  {k}: {v}" for k, v in filas] + [""]
    if boton: partes += [f"{boton[0]}:", boton[1], ""]
    if nota: partes += [nota, ""]
    partes += ["—", f"{nombre} · un producto de Gigadevs Corp"]
    return "\n".join(partes)
