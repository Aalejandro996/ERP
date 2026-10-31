# ERP Web — Arquitectura, seguridad, normativa y licenciamiento

## 1. Defectos del sistema original detectados (y corregidos)
| # | Hallazgo en el original (Tkinter + SQLite) | Gravedad | Corrección |
|---|---|---|---|
| 1 | **Balances mezclaban empresas**: las consultas unían `movimientos` solo por código de cuenta. Verificado con el propio código: con dos empresas que usan la cuenta `1.1`, el balance de la Empresa A mostraba Caja = 5.100 cuando lo real era 100 | **Crítica** | Saldos calculados por empresa; FKs compuestas (empresa, cuenta) y (empresa, asiento) en la BD |
| 2 | Usuario `admin`/`admin123` creado automáticamente | Alta | Sin usuarios por defecto; la migración invalida esa clave; MFA obligatorio |
| 3 | Documento (CxC/CxP) guardado y asiento intentado después: un fallo dejaba datos a medias | Alta | Documento + asiento + bitácora en UNA transacción |
| 4 | Valores fiscales **de ejemplo** sembrados (INPC 100→115, conceptos de retención "EJEMPLO") que podían usarse en una declaración real | Alta | La migración no los copia; el Administrador carga los reales |
| 5 | Dinero en `REAL` (float); descuadres por redondeo | Media | `NUMERIC` + `Decimal`; los floats se rechazan |
| 6 | Cuadre del asiento solo validado en la interfaz | Media | Validación en servicio + trigger diferido en PostgreSQL |
| 7 | Asientos y bitácora editables/borrables | Media | Solo-inserción (triggers); anulación por contra-asiento; bitácora con cadena de hashes |
| 8 | Todos los usuarios veían todas las empresas; permisos por índice de pestaña | Media | Asignación usuario↔empresa verificada en BD en cada petición + matriz RBAC en servidor |
| 9 | Licencia en JSON plano, `device_id` por wmic/MAC, gracia con reloj local | Media | Token Ed25519 atado a la instalación, vigencia firmada, anti-retroceso de reloj |
| 10 | Bug menor: `registrar_cobro_cxc` devolvía 2 valores en una ruta y 3 en otras | Baja | Reescrito con excepciones tipadas |

## 2. Multi‑cliente: ningún dato cruza entre organizaciones
- **Modelo:** `Organización` (cliente) → empresas, usuarios, políticas (tramos de provisión), licencia y bitácora propias.
  El RIF de una empresa es único *por organización*: dos firmas pueden llevar la misma empresa sin enterarse.
- **Aplicación:** cada ruta verifica en la BD que la empresa sea de la organización del usuario (y, salvo Administrador, que esté asignada); responde 404 a lo ajeno.
- **Base de datos (defensa en profundidad):** Row‑Level Security en todas las tablas de negocio con `app.org_id`, fijado por transacción (`app/rls.py`).
  Aunque un error de programación olvide un filtro, PostgreSQL no devuelve filas de otro cliente. Probado con el rol `erp_app` (no dueño).
- **Maestro (proveedor):** administra clientes y parámetros legales globales; **no** puede ver libros ni facturas de nadie (rol y RLS).
- **Parámetros legales globales** (UT, tarifa ISLR, retenciones, INPC, tasa BCV, alícuotas IVA/IGTF) solo los edita el Maestro: un cliente no puede alterar los de otro.

## 3. Seguridad implementada
- **Acceso:** Argon2id, bloqueo 5 intentos/15 min (con aviso por correo), mensaje genérico y tiempo constante, sesión nueva al autenticar, límites de tasa,
  política de claves (≥12, 3 clases de caracteres, sin usuario/correo, lista de comunes) e historial (no reutilizar las 5 últimas).
- **Correo verificado obligatorio:** alta por **invitación** (enlace de 72 h; el usuario crea su propia clave, el administrador nunca la conoce),
  verificación de correo para usuarios existentes, **recuperación de contraseña** (30 min, un uso, respuesta genérica) y alertas
  de nuevo acceso, bloqueo, cambio de clave y uso de códigos de respaldo. Tokens guardados como SHA‑256; enlaces construidos con `APP_BASE_URL` (no con `Host`).
- **MFA TOTP obligatorio para todos los roles:** QR generado en el servidor (SVG), 10 códigos de respaldo de un uso (SHA‑256), anti‑reutilización de códigos, secreto cifrado (Fernet).
- **Sesión:** CSRF en todo POST, cookie `__Host-` Secure/HttpOnly/SameSite, 30 min de inactividad y 8 h máximo; cambiar clave, restablecer MFA, desactivar
  al usuario o suspender la organización **cierra todas sus sesiones** (`sesion_version`).
- **Cabeceras:** CSP estricta (sin scripts ni estilos en línea), HSTS, `X-Frame-Options`, `Permissions-Policy`, COOP/CORP, `no-store`; errores sin trazas.
- **Autorización:** RBAC (`Maestro, Administrador, Contador, Auxiliar, Auditor Financiero, Cajero`) **intersectado con el plan** de la licencia.
- **Integridad:** asientos, movimientos, kardex, bitácora y documentos de venta son de solo‑inserción (triggers); bitácora y facturación encadenadas por SHA‑256
  (la auditoría verifica ambas cadenas); bloqueos de fila en numeración, cobros e inventario.

## 4. Licenciamiento por plan
Cada organización activa su propia clave. El servidor de licencias firma (Ed25519) un token con `plan`, topes de usuarios activos y empresas,
y vigencia de 7 días con renovación automática. El plan **no se puede ampliar** desde el cliente ni editando la BD (va firmado).
Sin licencia válida la organización falla cerrado (el Administrador ve solo la pantalla de licencia). Las opciones de menú fuera del plan se muestran bloqueadas.
Licencias emitidas antes de los planes (sin claim `plan`) conservan contabilidad, administración, auditoría e inventario hasta su reemisión.

## 5. Normativa venezolana aplicada
| Área | Norma | Cómo se aplica |
|---|---|---|
| Facturación | Prov. SNAT/2011/00071 | Numeración consecutiva y única por serie; N° de control del rango asignado por imprenta autorizada (datos de la imprenta y providencia en el documento); denominación, datos de emisor y adquirente, fecha y hora, descripción, cantidades, precios, "(E)" en exentos, base e IVA por alícuota, total; NC/ND con referencia a la factura afectada y motivo; anulación sin borrar ni reutilizar el número |
| Documentos digitales | Prov. SNAT/2024/000102 | Rango de control de imprenta digital registrable por serie; hash encadenado por documento |
| IVA | LIVA y su Reglamento (Art. 70‑72) | Alícuotas general, reducida y adicional de lujo (parametrizadas por Gaceta); libro de ventas mensual con columnas por alícuota, NC/ND, factura afectada, retenciones; exportación CSV |
| Retención IVA | Prov. SNAT/2015/0049 | Registro del comprobante emitido por el cliente agente de retención (rebaja CxC y asiento) y TXT de retenciones (módulo fiscal) |
| IGTF | Ley de IGTF y providencias de agentes de percepción | La empresa Sujeto Pasivo Especial percibe IGTF sobre los pagos en divisas, aparte del total, con su asiento |
| Divisas | BCV / Prov. 0071 | Operaciones en USD/EUR con equivalente en bolívares a la tasa BCV del día; contabilidad en Bs (moneda funcional) |
| ISLR | LISLR, Decreto 1.808 | Tarifa N° 2, conciliación fiscal, rebajas, ajuste por inflación (simplificado), retenciones |
| Contabilidad | Código de Comercio (Arts. 32‑35), VEN‑NIF | Diario inalterable, libro mayor y balance de comprobación, estados financieros NIC 1 / NIIF 18, cierres |
| Auditoría | NIA (adoptadas por la FCCPV) | Pruebas de integridad, hallazgos, papeles de trabajo por tipo de cédula (NIA 230), bitácora consultable por el auditor, segregación de funciones |
| Fecha/hora legal | — | America/Caracas, independiente de la zona del servidor |

## 6. Módulos
Facturación (facturas, NC/ND, cobros, retenciones recibidas, series y control, clientes, libro de ventas, impresión/PDF) ·
Contabilidad (plan de cuentas, diario, mayor, comprobación, estados, cierres, configuración de asientos automáticos) · Fiscal (ISLR, retenciones, libros) ·
Administración (CxC con antigüedad y provisión, CxP, bancos y flujo de efectivo) · Inventario (Promedio/PEPS, kardex, precio y alícuota de venta) ·
Auditoría · Organización (usuarios por invitación, políticas, licencia) · Plataforma (clientes, parámetros legales, bitácora).
Integración según plan: una factura descarga inventario y guarda su costo (si hay Inventario), genera asiento de venta, de costo y de IGTF y alimenta
el libro fiscal (si hay Contabilidad) y crea su CxC (si hay Administración) — todo en una transacción.

## 6 bis. Maestro, equipos y muro de pago
- **Maestro:** usuario del proveedor (sin organización). Gestiona clientes, licencias (plan, usuarios, empresas, equipos,
  instalaciones, vigencia, estado), precios, órdenes de pago y la salud de la base de datos. Ve conteos por cliente mediante
  funciones `SECURITY DEFINER` acotadas; nunca el contenido de sus libros (RLS).
- **API administrativa del servidor de licencias:** cada petición firmada con HMAC‑SHA256 (método, ruta, cuerpo, hora y
  nonce de un uso; ventana de 5 min). Repetir, alterar o falsificar una petición devuelve 401.
- **Equipos:** cada navegador autorizado de una organización se registra (cookie HttpOnly, solo su SHA‑256 en BD). La licencia
  fija el tope (`max_equipos`); al superarlo el acceso se rechaza aunque clave y MFA sean correctos. Revocar un equipo cierra
  sus sesiones al instante.
- **Pagos:** el monto lo fija el catálogo del servidor (el cliente solo elige plan y período). USDT con Binance Pay: firma
  HMAC‑SHA512 hacia Binance; los avisos se verifican con el certificado RSA de Binance y, además, la orden se **vuelve a
  consultar** en Binance (estado PAID, moneda y monto exactos) antes de entregar. La entrega es idempotente (una licencia y un
  correo por orden aunque el aviso se repita). Bolívares: pago móvil con referencia validada y confirmación del Maestro.
- **Entrega única:** la clave se genera al entregar, se envía una vez por Brevo y se guarda solo su SHA‑256. Si se pierde, el
  Maestro genera una nueva y la anterior deja de funcionar.
- **Libro de compras** (Art. 75 RLIVA) generado desde CxP con número de control, exento, base, alícuota, IVA, retenciones e importaciones.

## 6 ter. Navegación, plan de cuentas, compras, WhatsApp, combos y demo
- **Navegación** (`app/menu.py`): mismo orden y ubicación de módulos que el sistema de escritorio (`main.py`): Catálogo de cuentas →
  Asientos → Libro mayor → Libro diario → Balance general → Balance de comprobación → Estado de resultados → Dashboard → Libros fiscales →
  Conciliación bancaria → Divisas → Configuración y cierres; luego Fiscal Venezuela, Administración y Auditoría. Lo que en escritorio
  eran pestañas internas (`sub_notebook`) son pestañas arriba de la página. Una sola definición alimenta menú y pestañas.
- **Plan de cuentas existente:** importación desde Excel (.xlsx), CSV/TXT o texto pegado (`app/calculos/plan_cuentas.py`): columnas por
  nombre en cualquier orden, tipo deducido del primer dígito si falta, monetarias y categoría NIIF 18 inferidas, errores por línea, sin
  duplicar ni cambiar el tipo de cuentas con movimientos. Plan modelo VEN‑NIF para quien no tiene uno.
- **Órdenes de compra:** emitidas por la empresa (emisor) a un proveedor o cliente, con el mismo modelo visual de la factura de Gigadevs;
  IVA por alícuota vigente, divisas con equivalente BCV, envío por correo o WhatsApp, enlace firmado de 60 días para el destinatario,
  confirmación por el proveedor desde el chat y recepción que da entrada al kardex al costo en Bs.
- **Bot de WhatsApp** (número propio de cada empresa, por la Cloud API de Meta o por WasenderAPI —QR, no oficial; aviso verificado con `X-Webhook-Signature`, adjuntos por `/decrypt-media`—): clientes ven el catálogo y hacen pedidos; proveedores ven,
  confirman o rechazan sus órdenes de compra y envían ofertas que se convierten en OC. Webhook con URL secreta por empresa, verificación
  `X-Hub-Signature-256`, deduplicación por id de mensaje, token y App Secret cifrados (Fernet). El webhook llega sin usuario: la función
  `whatsapp_destino()` (SECURITY DEFINER) traduce la clave a su organización y desde ahí todo pasa por RLS. Solo atiende si la licencia
  incluye Facturación o Inventario.
- **Precios:** catálogo mensual y anual sembrado una vez con precios de referencia del mercado venezolano (solo donde falta precio; nunca
  pisa lo fijado por el Maestro). **Combos personalizados:** módulos a elección, mensual o anual; el precio puede calcularse solo a partir
  del precio mensual por módulo (anual = 10 de 12 meses). El cliente los compra en su tienda y recibe una licencia PERSONALIZADA con
  exactamente esos módulos.
- **Demo de 72 h** (plan `DEMO`: Facturación + Inventario, 2 usuarios, 1 empresa, 2 equipos): la pide el Administrador una sola vez
  (por organización, correo y RIF) o la emite el Maestro; el servidor fija la vigencia a 72 h exactas y el ERP muestra el tiempo restante.

## 7. Despliegue
Render (`render.yaml`, proyecto «ERP»): `erp-web` y `erp-licencias` (Gunicorn tras el proxy de Render, `ProxyFix` para la IP
real), PostgreSQL sin acceso público (la app hace `SET ROLE erp_app`), Key Value para límites de tasa. Alternativa propia:
Nginx (TLS 1.2+) → Gunicorn (`wsgi:app`) → PostgreSQL en red privada; Redis para límites de tasa; SMTP con STARTTLS
(Brevo, SES, Workspace) y SPF/DKIM/DMARC en el dominio; backups cifrados con restauración probada; logs a destino externo.
Servidor de licencias aparte (`licensing_server.wsgi:app`). Antes de producción: pentest (OWASP ZAP) y `pip-audit`.

## 8. Qué está verificado y qué no
**Ejecutado y aprobado (actualización):** 45 pruebas. Incluyen 14 del servidor de licencias y el muro de pago (firma admin
válida/repetida/vencida/alterada, inyección SQL, entrega única por correo, rotación de clave, Binance con aviso RSA falso,
viejo o con monto distinto, pago móvil con confirmación y doble clic). La integración cubre además: límite de equipos,
libro de compras, consola Maestro (licencias, pagos, BD) y compra en bolívares de punta a punta. También se simuló el
despliegue de Render (dueño sin superusuario, `inicializar --no-interactivo` dos veces, `SET ROLE` sin DDL ni UPDATE
sobre la bitácora, servidor de licencias en PostgreSQL sin acceso del ERP a sus tablas).
**Ejecutado y aprobado (versión anterior):** 39 pruebas unitarias (licencias, partida doble, cálculos fiscales/inventario/cartera, TOTP, cifrado, planes, cálculo de facturas,
IGTF y asientos de facturación) y **una prueba de integración de punta a punta contra PostgreSQL 16 real** con la app conectada como `erp_app`:
Maestro con MFA → alta de 2 clientes → invitación por correo → creación de clave y MFA con QR → planes ALL_IN_ONE y CASH → factura en USD con IGTF,
inventario y asientos → factura a crédito con CxC y cobro → NC con devolución → rango de control agotado y nuevo rango → anulación con reverso de
inventario y costo → libro de ventas CSV → partida doble cuadrada → bitácora íntegra → cliente B con la misma empresa sin conflicto, sin acceso a
contabilidad (403) ni a datos de A (404) → RLS verificado directamente en SQL → triggers de inmutabilidad → recuperación de contraseña que cierra
sesiones. También se probaron: la actualización de una base de la versión anterior (usuarios/empresas/bitácora adaptados, migración idempotente),
el ETL contra una base creada con el `inicializar_bd()` real del sistema de escritorio, y la interfaz en navegador (escritorio y móvil).
**No verificado / pendiente:** envío SMTP real (probado con el backend de consola); homologación del software ante el SENIAT (Prov. 0102 exige que
el proveedor la tramite y que el rango de control lo asigne una imprenta digital autorizada); la estructura del TXT de retenciones y las alícuotas deben
contrastarse con la normativa vigente; el ajuste por inflación sigue siendo simplificado; no hay libro de compras generado desde CxP ni PDF nativo
(se imprime/guarda como PDF desde el navegador); la conciliación bancaria sigue siendo manual.
Binance Pay se probó con un simulador de su API (firma y verificación reales); falta probarlo con credenciales de comercio
reales. El remitente `aalejandro996@icloud.com` en Brevo puede ir a spam: iCloud publica DMARC y Brevo no puede firmar por
ese dominio; se recomienda un remitente de un dominio propio de Gigadevs autenticado en Brevo (SPF/DKIM).
