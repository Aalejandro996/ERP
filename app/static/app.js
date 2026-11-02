/* Comportamiento mínimo del cliente (la CSP prohíbe scripts en línea). El servidor valida y recalcula todo. */
(function () {
  "use strict";
  document.addEventListener("click", function (e) {
    if (e.target.closest("[data-imprimir]")) window.print();
  });
  document.addEventListener("submit", function (e) {
    var msg = e.target.getAttribute("data-confirmar");
    if (msg && !window.confirm(msg)) e.preventDefault();
    var b = e.target.querySelector("button:not([type=button])");
    if (b && !e.defaultPrevented) setTimeout(function () { b.disabled = true; }, 0); // evita doble envío (doble factura)
  });

  // Barra de áreas: en pantallas angostas se desplaza para que el área activa quede a la vista
  var areaActiva = document.querySelector(".areas .area.activo");
  if (areaActiva) areaActiva.parentNode.scrollLeft = areaActiva.offsetLeft - (areaActiva.parentNode.clientWidth - areaActiva.offsetWidth) / 2;

  // Formularios compactos: «Agregar línea» muestra la siguiente fila oculta de la tabla de líneas
  document.addEventListener("click", function (e) {
    var b = e.target.closest("[data-mas-linea]");
    if (!b) return;
    var oculta = b.previousElementSibling.querySelector("tr[data-extra][hidden]");
    if (oculta) { oculta.hidden = false; var c = oculta.querySelector("input,select"); if (c) c.focus(); }
    if (!b.previousElementSibling.querySelector("tr[data-extra][hidden]")) b.hidden = true;
  });

  // Editor de asientos (como IQ_Contable): una fila de controles agrega líneas a la tabla; los totales se ven en verde
  // cuando cuadran. Al guardar se numeran las líneas c1/d1/h1… El servidor vuelve a validar todo.
  document.querySelectorAll("form[data-asiento]").forEach(function (f) {
    var q = function (k) { return f.querySelector('[data-l="' + k + '"]'); };
    var cuenta = q("cuenta"), debe = q("debe"), haber = q("haber"), cuerpo = q("lineas"), aviso = q("aviso"), tot = q("totales"), vacio = q("vacio");
    var n = function (v) { var x = parseFloat(String(v || "").replace(",", ".")); return isFinite(x) ? x : 0; };
    var fmt = function (x) { return x.toLocaleString("es-VE", { minimumFractionDigits: 2, maximumFractionDigits: 2 }); };
    var avisar = function (t) { aviso.textContent = t || ""; };
    function totales() {
      var d = 0, h = 0;
      cuerpo.querySelectorAll("tr").forEach(function (tr) { var i = tr.querySelectorAll("input[type=number]"); d += n(i[0].value); h += n(i[1].value); });
      tot.innerHTML = "DEBE: " + fmt(d) + " &nbsp;|&nbsp; HABER: " + fmt(h);
      tot.className = "asiento-tot" + (d === 0 && h === 0 ? "" : Math.round((d - h) * 100) === 0 ? " ok" : " no");
      vacio.hidden = cuerpo.children.length > 0;
      return [d, h];
    }
    function celda(tr, html) { var td = document.createElement("td"); if (html !== undefined) td.textContent = html; tr.appendChild(td); return td; }
    function monto(nombre, v, etiqueta) {
      var i = document.createElement("input"); i.type = "number"; i.step = "0.01"; i.min = "0"; i.placeholder = "0,00"; i.value = v ? v.toFixed(2) : "";
      i.setAttribute("aria-label", etiqueta); i.dataset.col = nombre; return i;
    }
    function agregar() {
      var d = n(debe.value), h = n(haber.value);
      if (!cuenta.value) return avisar("Seleccione una cuenta.");
      if ((d === 0 && h === 0) || (d > 0 && h > 0)) return avisar("Una línea lleva monto al debe o al haber (mayor que cero), pero no a ambos.");
      var op = cuenta.options[cuenta.selectedIndex].text, tr = document.createElement("tr"), c = document.createElement("input");
      c.type = "hidden"; c.value = cuenta.value; c.dataset.col = "c";
      var td = celda(tr); td.appendChild(c); td.appendChild(document.createTextNode(cuenta.value));
      celda(tr, op.replace(cuenta.value, "").trim());
      celda(tr).appendChild(monto("d", d, "Debe")); celda(tr).appendChild(monto("h", h, "Haber"));
      tr.children[2].className = tr.children[3].className = "n";
      cuerpo.appendChild(tr);
      debe.value = haber.value = ""; cuenta.value = ""; cuenta.focus(); avisar(""); totales();
    }
    q("agregar").addEventListener("click", agregar);
    [debe, haber].forEach(function (i) { i.addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); agregar(); } }); });
    cuerpo.addEventListener("click", function (e) { var tr = e.target.closest("tr"); if (tr && e.target.tagName !== "INPUT") tr.classList.toggle("sel"); });
    q("eliminar").addEventListener("click", function () {
      var sel = cuerpo.querySelectorAll("tr.sel");
      if (!sel.length) return avisar("Haga clic en la línea que desea eliminar de la tabla.");
      sel.forEach(function (tr) { tr.remove(); }); avisar(""); totales();
    });
    f.addEventListener("input", function (e) { if (cuerpo.contains(e.target)) totales(); });
    f.addEventListener("submit", function (e) {
      var filas = Array.prototype.filter.call(cuerpo.children, function (tr) { var i = tr.querySelectorAll("input[type=number]"); return n(i[0].value) || n(i[1].value); });
      var t = totales();
      if (filas.length < 2) { e.preventDefault(); e.stopImmediatePropagation(); return avisar("El asiento necesita al menos 2 líneas con monto."); }
      if (Math.round((t[0] - t[1]) * 100) !== 0) { e.preventDefault(); e.stopImmediatePropagation(); return avisar("El total del debe (" + fmt(t[0]) + ") no coincide con el del haber (" + fmt(t[1]) + ")."); }
      Array.prototype.forEach.call(cuerpo.children, function (tr) { tr.querySelectorAll("input").forEach(function (i) { i.removeAttribute("name"); }); });
      filas.forEach(function (tr, k) {
        var i = tr.querySelectorAll("input");
        i[0].name = "c" + (k + 1); i[1].name = "d" + (k + 1); i[2].name = "h" + (k + 1);
      });
    }, true);
    totales();
  });

  var nodo = document.getElementById("datos-factura");
  if (!nodo) return;
  var datos = JSON.parse(nodo.textContent), form = document.getElementById("form-factura");
  var num = function (v) { var n = parseFloat(String(v || "").replace(",", ".")); return isFinite(n) ? n : 0; };
  var fmt = function (n) { return n.toLocaleString("es-VE", { minimumFractionDigits: 2, maximumFractionDigits: 2 }); };
  var pct = function (a) {
    var al = datos.alicuotas;
    if (a === "E") return 0;
    if (a === "A") return num(al.G) + num(al.A);
    return num(al[a]);
  };

  var sel = document.getElementById("cliente-sel");
  if (sel) sel.addEventListener("change", function () {
    var c = datos.clientes[sel.value];
    if (!c) return;
    document.getElementById("cliente_rif").value = sel.value;
    document.getElementById("cliente_nombre").value = c[0];
    document.getElementById("cliente_direccion").value = c[1];
    document.getElementById("cliente_telefono").value = c[2];
  });

  form.querySelectorAll(".sel-prod").forEach(function (s) {
    s.addEventListener("change", function () {
      var p = datos.productos[s.value], fila = s.closest("tr");
      if (!p) return;
      var i = fila.getAttribute("data-linea");
      form.elements["l" + i + "_desc"].value = p[0];
      if (!num(form.elements["l" + i + "_cant"].value)) form.elements["l" + i + "_cant"].value = 1;
      form.elements["l" + i + "_precio"].value = p[1];
      form.elements["l" + i + "_alic"].value = p[2];
      recalcular();
    });
  });

  function recalcular() {
    var monedaSel = document.getElementById("moneda"), tasaIn = document.getElementById("tasa");
    var tasa = monedaSel && monedaSel.value !== "VES" ? num(tasaIn && tasaIn.value) : 1;
    var base = 0, exento = 0, iva = 0;
    form.querySelectorAll("tr[data-linea]").forEach(function (fila) {
      var i = fila.getAttribute("data-linea");
      var neto = num(form.elements["l" + i + "_cant"].value) * num(form.elements["l" + i + "_precio"].value) * (100 - num(form.elements["l" + i + "_dpct"].value)) / 100;
      var bs = Math.round(neto * tasa * 100) / 100, a = form.elements["l" + i + "_alic"].value;
      fila.querySelector(".sub").textContent = neto ? fmt(bs) : "—";
      if (a === "E") exento += bs; else { base += bs; iva += bs * pct(a) / 100; }
    });
    var put = function (k, v) { var el = document.querySelector('[data-r="' + k + '"]'); if (el) el.textContent = fmt(v); };
    put("base", base); put("exento", exento); put("iva", iva); put("total", base + exento + iva);
  }
  var monedaIn = document.getElementById("moneda"), tasaCampo = document.getElementById("tasa");
  if (monedaIn && tasaCampo && datos.tasas) monedaIn.addEventListener("change", function () {
    var t = datos.tasas[monedaIn.value];
    if (t) tasaCampo.value = t;  // tasa BCV del día de la moneda elegida (USD o EUR)
  });
  form.addEventListener("input", recalcular);
  form.addEventListener("change", recalcular);

  var cond = document.getElementById("condicion"), pagos = document.getElementById("sec-pagos"), venc = document.getElementById("lbl-venc");
  function verCondicion() {
    if (!cond) return;
    var credito = cond.value === "Credito";
    if (pagos) pagos.classList.toggle("oculto", credito);
    if (venc) venc.classList.toggle("oculto", !credito);
  }
  if (cond) cond.addEventListener("change", verCondicion);
  verCondicion(); recalcular();
})();

/* Nuevo presupuesto (Maestro): precios del catálogo según el período y totales en vivo. El servidor recalcula todo. */
(function () {
  "use strict";
  var nodo = document.getElementById("datos-presupuesto");
  if (!nodo) return;
  var d = JSON.parse(nodo.textContent), form = document.getElementById("form-presupuesto");
  var num = function (v) { var n = parseFloat(String(v || "").replace(",", ".")); return isFinite(n) ? n : 0; };
  var usd = function (n) { return "$" + n.toLocaleString("es-VE", { minimumFractionDigits: 2, maximumFractionDigits: 2 }); };
  var periodo = function () { var r = form.querySelector('input[name="periodo"]:checked'); return r ? r.value : "anual"; };
  function calcular() {
    var p = periodo() === "mensual" ? "mensual" : "anual", iva = num(d.iva), gravado = 0, exento = 0;
    form.querySelectorAll(".px-precio").forEach(function (el) {
      var it = d.items[el.getAttribute("data-clave")] || {}, v = it.fijo || it[p];
      el.textContent = v ? usd(num(v)) + (it.fijo ? "" : (p === "mensual" ? " /mes" : " /año")) : "sin precio " + p;
    });
    form.querySelectorAll('input[name="items"]:checked').forEach(function (c) {
      var it = d.items[c.value] || {}; gravado += num(it.fijo || it[p]) * 100 / (100 + iva);
    });
    form.querySelectorAll("tr[data-servicio]").forEach(function (f) {
      var i = f.getAttribute("data-servicio"), m = num(form.elements["l" + i + "_cant"].value) * num(form.elements["l" + i + "_precio"].value);
      f.querySelector(".px-monto").textContent = m ? usd(m) : "—";
      if (form.elements["l" + i + "_alic"].value === "E") exento += m; else gravado += m;
    });
    var ivaM = gravado * iva / 100;
    var put = function (k, v) { var el = form.querySelector('[data-r="' + k + '"]'); if (el) el.textContent = usd(v); };
    put("sub", gravado + exento); put("iva", ivaM); put("total", gravado + exento + ivaM);
  }
  var org = document.getElementById("px-org");
  if (org) org.addEventListener("change", function () {
    var o = org.options[org.selectedIndex];
    if (!o.value) return;
    document.getElementById("px-nombre").value = o.getAttribute("data-nombre");
    document.getElementById("px-rif").value = o.getAttribute("data-rif");
    document.getElementById("px-correo").value = o.getAttribute("data-correo");
  });
  form.addEventListener("input", calcular); form.addEventListener("change", calcular); calcular();
})();

/* Asistentes por pasos: el resumen lateral repite lo elegido; los cálculos (p. ej. precio sugerido de un combo) son orientativos. */
(function () {
  "use strict";
  document.querySelectorAll("form.px-asistente").forEach(function (form) {
    var calc = form.getAttribute("data-calculo") ? JSON.parse(form.getAttribute("data-calculo")) : null;
    var num = function (v) { var n = parseFloat(String(v || "").replace(",", ".")); return isFinite(n) ? n : 0; };
    function valor(nombre) {
      var els = form.querySelectorAll('[name="' + nombre + '"]');
      if (!els.length) return "";
      var e = els[0];
      if (e.type === "radio" || e.type === "checkbox") {
        var t = []; els.forEach(function (x) { if (x.checked) t.push(x.getAttribute("data-titulo") || x.value); });
        return t.join(", ");
      }
      if (e.tagName === "SELECT") return e.value ? e.options[e.selectedIndex].text : "";
      return e.value;
    }
    function actualizar() {
      form.querySelectorAll("[data-eco]").forEach(function (b) { b.textContent = valor(b.getAttribute("data-eco")) || "—"; });
      if (calc && calc.tipo === "combo") {
        var meses = num((form.querySelector('[name="meses"]:checked') || {}).value || 1), suma = 0;
        form.querySelectorAll('[name="modulos"]:checked').forEach(function (m) { suma += num(calc.precios[m.value]); });
        var total = suma * meses; if (meses >= 12) total = total * (100 - num(calc.descuento)) / 100;
        var manual = num((form.querySelector('[name="monto"]') || {}).value);
        var el = form.querySelector("[data-calc]");
        if (el) el.textContent = "$" + (manual || Math.round(total)).toLocaleString("es-VE", { minimumFractionDigits: 2 }) + (manual ? "" : " (sugerido)");
      }
    }
    form.addEventListener("input", actualizar); form.addEventListener("change", actualizar); actualizar();
  });
})();

/* Nuevo presupuesto de la empresa: precio e IVA del inventario y totales en bolívares en vivo. El servidor recalcula todo. */
(function () {
  "use strict";
  var nodo = document.getElementById("datos-presupuesto-cliente");
  if (!nodo) return;
  var d = JSON.parse(nodo.textContent), form = document.getElementById("form-pc");
  var num = function (v) { var n = parseFloat(String(v || "").replace(",", ".")); return isFinite(n) ? n : 0; };
  var bs = function (n) { return "Bs " + n.toLocaleString("es-VE", { minimumFractionDigits: 2, maximumFractionDigits: 2 }); };
  var pct = function (a) { return a === "E" ? 0 : (a === "A" ? num(d.alicuotas.G) + num(d.alicuotas.A) : num(d.alicuotas[a])); };
  var NOM = { G: "General", R: "Reducida", A: "Lujo", E: "Exento" };
  function calcular() {
    var exento = 0, base = 0, iva = 0;
    form.querySelectorAll("tr[data-linea]").forEach(function (f) {
      var i = f.getAttribute("data-linea"), cod = form.elements["l" + i + "_cod"].value, p = d.productos[cod];
      var a = p ? p[2] : "G", precio = form.elements["l" + i + "_precio"].value !== "" ? num(form.elements["l" + i + "_precio"].value) : (p ? num(p[1]) : 0);
      var m = num(form.elements["l" + i + "_cant"].value) * precio;
      var activa = !!(cod || form.elements["l" + i + "_desc"].value.trim());
      f.querySelector(".px-monto").textContent = activa && m ? bs(m) : "—";
      f.querySelector(".pc-alic").textContent = activa ? NOM[a] + (pct(a) ? " " + pct(a) + "%" : "") : "—";
      if (!activa) return;
      if (a === "E") exento += m; else { base += m; iva += m * pct(a) / 100; }
    });
    var put = function (k, v) { var el = form.querySelector('[data-r="' + k + '"]'); if (el) el.textContent = v; };
    put("exento", bs(exento)); put("base", bs(base)); put("iva", bs(iva)); put("total", bs(exento + base + iva));
    put("cliente", form.elements.nombre.value || "—");
  }
  form.querySelectorAll(".pc-prod").forEach(function (s) {
    s.addEventListener("change", function () {
      var p = d.productos[s.value], i = s.closest("tr").getAttribute("data-linea");
      if (!p) return;
      form.elements["l" + i + "_precio"].placeholder = num(p[1]).toFixed(2);
      if (!num(form.elements["l" + i + "_cant"].value)) form.elements["l" + i + "_cant"].value = 1;
    });
  });
  var sel = document.getElementById("pc-cliente");
  sel.addEventListener("change", function () {
    var c = d.clientes[sel.value]; if (!c) return;
    ["nombre", "rif", "direccion", "telefono", "email"].forEach(function (k, j) { document.getElementById("pc-" + k).value = c[j] || ""; });
    calcular();
  });
  form.addEventListener("input", calcular); form.addEventListener("change", calcular); calcular();
})();
