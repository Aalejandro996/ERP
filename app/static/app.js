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
