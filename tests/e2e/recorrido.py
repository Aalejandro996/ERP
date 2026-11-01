"""Recorrido visual de todas las pantallas con Playwright (escritorio y móvil).

  python tests/e2e/servidor_demo.py 5055 &        # primero, el servidor con datos de muestra
  python tests/e2e/recorrido.py [http://127.0.0.1:5055]
Por cada pantalla del menú (cliente y Maestro) revisa: respuesta sin error, consola sin errores (CSP incluida), sin
desplazamiento horizontal y campos de formulario alineados (misma altura y misma base en cada fila). Guarda capturas
en tests/e2e/capturas/ y termina con código 1 si algo falla.
"""
import json
import pathlib
import re
import sys
import time

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from app import totp  # noqa: E402

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:5055"
DIR = pathlib.Path(__file__).with_name("capturas")
ACC = json.loads(pathlib.Path(__file__).with_name(".accesos.json").read_text())
VISTAS = {"escritorio": dict(width=1440, height=900), "movil": dict(width=390, height=844)}

# Campos de una misma fila: misma altura y misma base (ignora casillas de verificación)
REVISAR = """() => {
  const fallas = [];
  const doc = document.documentElement;
  if (doc.scrollWidth > doc.clientWidth + 1) fallas.push(`desplazamiento horizontal: ${doc.scrollWidth} > ${doc.clientWidth}`);
  document.querySelectorAll('form .grid').forEach((grid, gi) => {
    const filas = {};
    grid.querySelectorAll(':scope > label > input:not([type=checkbox]):not([type=hidden]), :scope > label > select, :scope > label > textarea').forEach(el => {
      const r = el.getBoundingClientRect();
      if (!r.width) return;
      const fila = Math.round(el.closest('label').getBoundingClientRect().top);
      (filas[fila] = filas[fila] || []).push({ alto: Math.round(r.height), base: Math.round(r.bottom), ancho: r.width, tag: el.tagName });
    });
    Object.values(filas).forEach(f => {
      const sinArea = f.filter(x => x.tag !== 'TEXTAREA');
      if (new Set(sinArea.map(x => x.alto)).size > 1) fallas.push(`grid ${gi}: alturas distintas ${sinArea.map(x => x.alto)}`);
      if (new Set(sinArea.map(x => x.base)).size > 1) fallas.push(`grid ${gi}: campos desalineados (bases ${sinArea.map(x => x.base)})`);
    });
  });
  document.querySelectorAll('input, select, textarea, button').forEach(el => {
    if (el.closest('.tabla-wrap')) return;                 // las tablas anchas se desplazan dentro de su contenedor
    const r = el.getBoundingClientRect();
    if (r.width && r.right > doc.clientWidth + 1) fallas.push(`${el.tagName} ${el.name || ''} se sale de la pantalla`);
  });
  return fallas;
}"""


def entrar(pagina, usuario):
    """El servidor rechaza un código TOTP ya usado (la siembra acaba de usar el actual): se reintenta con el siguiente."""
    pagina.goto(BASE + "/login")
    pagina.fill("input[name=usuario]", usuario); pagina.fill("input[name=clave]", ACC["clave"]); pagina.click("form button")
    for _ in range(3):
        pagina.fill("input[name=codigo]", totp.codigo(ACC[usuario])); pagina.click("form button"); pagina.wait_for_load_state()
        if "/mfa/" not in pagina.url: return
        time.sleep(31 - time.time() % 30)
    raise SystemExit(f"No se pudo iniciar sesión como {usuario}")


ABRIR = "el => { const d = el.closest('details'); if (d) d.open = true; }"   # secciones plegadas: se abren como lo haría el usuario


def interacciones(pag):
    """Lo que el usuario hace con las manos: importar su plan de cuentas y registrar un asiento."""
    fallas, eid = [], ACC["eid"]
    pag.goto(f"{BASE}/e/{eid}/cuentas")
    boton = pag.locator("button", has_text="Importar plan").first
    boton.evaluate(ABRIR)
    csv = "Código;Nombre\n1.1.05;Caja chica\n6.1.01;Gastos de oficina\n".encode()
    pag.set_input_files("input[name=archivo]", files=[dict(name="plan.csv", mimeType="text/csv", buffer=csv)])
    boton.click(); pag.wait_for_load_state()
    if "1.1.05" not in pag.inner_text("main"): fallas.append("importar plan: la cuenta importada no aparece")
    pag.goto(f"{BASE}/e/{eid}/asientos")
    def linea(cuenta, debe="", haber=""):
        pag.select_option("[data-l=cuenta]", cuenta); pag.fill("[data-l=debe]", debe); pag.fill("[data-l=haber]", haber); pag.click("[data-l=agregar]")
    linea("1.1.05", debe="150"); linea("1.1.01", haber="100")
    if "no" not in (pag.get_attribute("[data-l=totales]", "class") or ""): fallas.append("asiento: el total descuadrado no se marca en rojo")
    linea("1.1.02", haber="999")                                           # línea equivocada: se selecciona y se elimina
    pag.click("tbody[data-l=lineas] tr:nth-child(3) td:nth-child(2)"); pag.click("[data-l=eliminar]")
    linea("1.1.02", haber="50")
    if "ok" not in (pag.get_attribute("[data-l=totales]", "class") or ""): fallas.append("asiento: el total cuadrado no se marca en verde")
    pag.fill("input[name=concepto]", "Reposición de caja chica"); pag.click("text=Guardar asiento completo"); pag.wait_for_load_state()
    if "registrado" not in pag.inner_text("main"): fallas.append("asiento: no se registró")
    pag.goto(f"{BASE}/e/{eid}/asientos?apertura=1")
    if pag.locator("tbody[data-l=lineas] tr").count() < 2: fallas.append("asiento de apertura: no cargó las cuentas de la empresa")
    print(("✗ " if fallas else "✓ ") + "demo interacciones (importar plan, asiento con agregar/eliminar línea, apertura)")
    return fallas


def enlaces(pagina):
    hrefs = pagina.eval_on_selector_all("aside a[href]", "as => as.map(a => a.getAttribute('href'))")
    return sorted({h for h in hrefs if h.startswith("/") and h != "/" + "logout"})


def main():
    DIR.mkdir(exist_ok=True)
    problemas = []
    with sync_playwright() as p:
        nav = p.chromium.launch()
        for usuario, inicio in (("demo", f"/e/{ACC['eid']}/cuentas"), ("root", "/plataforma")):
            ctx = nav.new_context(viewport=VISTAS["escritorio"]); pag = ctx.new_page()
            errores = []
            pag.on("console", lambda m: errores.append(m.text) if m.type == "error" else None)
            pag.on("pageerror", lambda e: errores.append(str(e)))
            entrar(pag, usuario)
            if usuario == "demo": problemas += [f"demo interacciones: {f}" for f in interacciones(pag)]
            pag.goto(BASE + inicio)
            rutas = [inicio] + [r for r in enlaces(pag) if r != inicio] + (["/plataforma/facturas/ejemplo"] if usuario == "root" else [])
            for vista, tam in VISTAS.items():
                pag.set_viewport_size(tam)
                for ruta in rutas:
                    errores.clear()
                    resp = pag.goto(BASE + ruta); pag.wait_for_load_state("networkidle")
                    nombre = f"{usuario}_{vista}_{re.sub(r'[^a-z0-9]+', '_', ruta.lower()).strip('_') or 'inicio'}.png"
                    pag.screenshot(path=str(DIR / nombre), full_page=True)
                    fallas = ([f"HTTP {resp.status}"] if resp.status >= 400 else []) + [f"consola: {e}" for e in errores] + pag.evaluate(REVISAR)
                    print(("✗ " if fallas else "✓ ") + f"{usuario:4} {vista:10} {ruta}")
                    problemas += [f"{usuario} {vista} {ruta}: {f}" for f in fallas]
            ctx.close()
        nav.close()
    print(f"\n{len(problemas)} problema(s)")
    for x in problemas: print("  -", x)
    sys.exit(1 if problemas else 0)


if __name__ == "__main__":
    main()
