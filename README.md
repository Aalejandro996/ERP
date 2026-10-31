# Contax® — *Tu empresa en orden. Tu fisco al día.*

> Antes «ERP». Contax es una marca registrada de Gigadevs Corp; se mantienen los colores y el diseño.

Una marca registrada de Gigadevs Corp. Flask + PostgreSQL, multi‑cliente, por planes de licencia. Repositorio **privado**.

Contabilidad VEN‑NIF, administración, inventario, **facturación venezolana** y auditoría financiera en una sola plataforma.
Cada cliente (organización) trabaja aislado: la aplicación lo verifica en cada petición y PostgreSQL lo impone con
Row‑Level Security. Los módulos que ve cada cliente dependen del **plan firmado en su licencia**:

| Plan | Módulos |
|---|---|
| `ERP_CONTABLE` | Contabilidad y fiscal · Administración · Auditoría |
| `CASH` | Facturación · Inventario |
| `ERP_ADMIN` | Administración · Inventario |
| `ALL_IN_ONE` | Todo |
| `PERSONALIZADO` | Combo a medida: los módulos que elija el Maestro (se vende mensual o anual) |
| `DEMO` | Facturación · Inventario, **72 horas**, una vez por organización |

Órdenes de compra y proveedores están disponibles con Administración, Inventario o Facturación; el bot de WhatsApp con Facturación o Inventario.

## Instalación
1. `python -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt`
2. Copie `.env.example` a `.env` y complete los secretos, `APP_BASE_URL`, el SMTP y la licencia.
3. Con el rol **dueño** de la base: `python scripts/inicializar.py` — crea/actualiza tablas, aplica
   `migrations/001…007` (RLS) y crea el primer **Maestro** (proveedor).
4. Cree el rol `erp_app` y aplique los `GRANT` comentados al final de `002_multicliente_facturacion.sql`.
   La app **debe** conectar como `erp_app`: RLS no aplica al dueño de las tablas.
5. Opcional: `python scripts/inicializar.py --alicuotas-referencia` carga IVA 16/8/15 e IGTF 3 como referencia; verifíquelos.
6. `gunicorn wsgi:app` detrás de Nginx con HTTPS.

## Despliegue en Render (proyecto «ERP»)
`render.yaml` crea el proyecto **ERP** con `erp-web` (clientes + consola Maestro), `erp-licencias` (licencias, muro de pago,
webhook de Binance), `erp-db` (PostgreSQL 16, sin acceso desde Internet) y `erp-cache` (Key Value para límites de tasa).
Dashboard → New → Blueprint → este repositorio, y complete las variables marcadas (claves Ed25519, Fernet, Brevo,
URLs públicas, Maestro, Binance Pay). En cada despliegue `scripts/inicializar.py --no-interactivo` aplica las migraciones,
prepara el rol limitado `erp_app` y, la primera vez, invita al Maestro por correo.

## Usuario Maestro (proveedor)
Consola en `/plataforma`: clientes (alta con invitación, suspensión), **licencias** (emitir con plan, usuarios, empresas,
**equipos** e instalaciones; modificar; extender; suspender/revocar; liberar instalaciones; regenerar y reenviar la clave;
historial de eventos), **pagos y precios** (catálogo, órdenes USDT/Bs, confirmar pagos móviles), **base de datos**
(salud, tamaños, conteos por cliente sin ver su contenido, integridad de bitácoras, purga) y parámetros legales.
La clave de una licencia **solo** viaja por correo (Brevo, remitente Gigadevs Corp) y se guarda como SHA‑256.

## Precios, combos y demo
- Al arrancar, el servidor de licencias siembra **una vez** precios de referencia (USD) del mercado venezolano, mensuales y anuales
  (anual = 10 meses): Cash 25/250, ERP Administrativo 23/230, ERP Contable 45/450, All in One 69/690, y un precio mensual por módulo
  (Contabilidad 25, Facturación 18, Administración 15, Auditoría 12, Inventario 12). Solo se agregan donde falta precio: lo que el
  Maestro ya fijó no cambia. Ajústelos en Maestro → Pagos y precios.
- **Combos personalizados** (Maestro → Pagos y precios): nombre, módulos, período mensual o anual, topes y precio (vacío = se calcula
  según los módulos que incluye). El cliente los ve en su tienda junto a los planes.
- **Demo de 72 h** de Facturación + Inventario: el Administrador la pide en Organización → Licencia (una vez) o el Maestro la emite.

## WhatsApp, pedidos y órdenes de compra
Administración → **Bot de WhatsApp** admite dos conexiones: **WasenderAPI** (WhatsApp por código QR: se escanea con el WhatsApp Business del teléfono, se cargan la clave de API de la sesión y el secreto de webhook, y se pega en WasenderAPI la URL del webhook que muestra Contax; no oficial, WhatsApp puede bloquear números con envíos masivos) o la **Cloud API oficial de Meta**: cargue el Phone Number ID, el token permanente y el App Secret de su app de Meta (WhatsApp Business
Cloud API) y pegue en Meta la URL del webhook y el token de verificación que muestra la pantalla (campo `messages`). Los clientes piden
por el chat (catálogo con precios e inventario) y los proveedores confirman sus **órdenes de compra** o envían ofertas. Las órdenes de
compra (Administración → Órdenes de compra) siguen el modelo de la factura de Gigadevs con los datos de la empresa emisora y del
proveedor o cliente, y se envían por correo o WhatsApp.

## Plan de cuentas existente
Contabilidad → Catálogo de cuentas → «¿Ya tiene su plan de cuentas? Impórtelo»: Excel (.xlsx), CSV/TXT o texto pegado. También hay una
plantilla CSV y un plan modelo VEN-NIF.

## Compra de licencias (muro de pago)
El Administrador de un cliente, en Organización → Licencia, elige plan y período y paga:
- **USDT por Binance (cuenta personal):** cada orden tiene un **monto único** (p. ej. 25,0371 USDT) que el cliente envía a
  nuestro Binance Pay ID (o por la red configurada). Cada 2 minutos `erp-licencias` lee el historial de la cuenta con una
  clave de **solo lectura** (`/sapi/v1/pay/transactions`, opcional `/sapi/v1/capital/deposit/hisrec`), empareja monto exacto
  + ventana de tiempo y entrega la licencia sola. Una transacción de Binance paga como máximo una orden; las órdenes vencen
  a las 24 h. Si el cliente envía otro monto, el Maestro lo confirma a mano.
- **Bolívares por pago móvil** a la tasa BCV del día: informa la referencia y el Maestro la confirma.
En ambos casos la clave llega una sola vez a su correo.

## Flujo de alta de un cliente
1. Maestro → **Clientes** → «Nuevo cliente»: se crea la organización y se envía una invitación por correo a su administrador.
2. Maestro → Licencias → «Emitir licencia» (o el cliente la compra en línea): la clave se envía por correo.
3. El administrador acepta la invitación (crea su clave), activa MFA (QR + 10 códigos de respaldo) y activa la licencia.
4. El administrador invita a su equipo (cada usuario crea su clave y su MFA; nadie más la conoce) y asigna empresas.

## Actualizar una instalación existente
`python scripts/inicializar.py` sobre la base actual: todos los usuarios, empresas, provisiones y la bitácora pasan a una
«Organización principal» (cambie su nombre/RIF), la licencia anterior pasa a esa organización y los usuarios, al entrar,
registran y verifican su correo y activan MFA. Las licencias sin plan conservan los módulos que ya tenían (todo menos
facturación) hasta que se reemitan con `licensing_server.cli plan "Cliente" ALL_IN_ONE`. Cree el Maestro con
`python scripts/inicializar.py --crear-maestro`.

## Migrar datos del sistema de escritorio (IQ_Contable)
```
python scripts/premigracion_check.py sistema_contable.db
python scripts/migrar_sqlite_a_pg.py sistema_contable.db --org-rif J-12345678-9 --org-nombre "Cliente SA"            # ENSAYO
python scripts/migrar_sqlite_a_pg.py sistema_contable.db --org-rif J-12345678-9 --org-nombre "Cliente SA" --ejecutar
```

## Licencias (solo el proveedor)
```
python -m licensing_server.cli keygen
python -m licensing_server.cli crear "Cliente SA" --plan ALL_IN_ONE --dias 365 --usuarios 5 --empresas 10
python -m licensing_server.cli plan "Cliente SA" CASH      # cambio de plan: llega en la próxima renovación (≤ 7 días)
python -m licensing_server.cli listar | estado | liberar
LICENSE_PRIVATE_KEY=... gunicorn licensing_server.wsgi:app
```

## Pruebas
- `pytest` (o `python run_tests.py`): pruebas unitarias.
- Integración de punta a punta contra PostgreSQL (¡borra la base indicada!):
  `ERP_TEST_DATABASE_URL=postgresql+psycopg://postgres:CLAVE@localhost:5432/erp_pruebas pytest tests/test_integracion_pg.py`

Ver `docs/ARQUITECTURA.md` para seguridad, normativa aplicada y alcance verificado.
