# PRD: Message Gateway

## Problem Statement

Las empresas chilenas que necesitan integrar mensajería SMS y WhatsApp en sus aplicaciones enfrentan dos problemas principales:

1. **Facturación internacional**: Twilio, Vonage, AWS y otros proveedores facturan en USD sin RUT chileno, obligando a las empresas a hacer autofacturación de servicios digitales extranjeros para recuperar el IVA — un proceso tedioso que muchas PYMES ni siquiera saben que existe.
2. **Fragmentación de proveedores**: No existe una API unificada que abstraiga SMS y WhatsApp con una sola integración, factura local chilena y precios predecibles.

Como resultado, los desarrolladores chilenos pierden tiempo integrando múltiples APIs y lidiando con contabilidad internacional, en vez de construir producto.

## Solution

**Message Gateway** es una plataforma CPaaS chilena que ofrece una API unificada para enviar SMS y WhatsApp, con:

- Una sola integración REST para ambos canales
- Facturación mensual en CLP con factura electrónica SII (vía Flow)
- Dashboard web con API Keys, uso en tiempo real y reportes
- Planes mensuales con precio predecible (Starter / Growth / Enterprise)
- Webhooks para delivery receipts asíncronos

## User Stories

1. Como desarrollador chileno, quiero registrarme con mi RUT y correo, para obtener una API Key y empezar a enviar mensajes inmediatamente.
2. Como desarrollador, quiero enviar un SMS con un solo POST a `/v1/messages`, para no tener que integrar múltiples APIs de operadores.
3. Como desarrollador, quiero enviar un mensaje WhatsApp con el mismo endpoint (cambiando el parámetro `channel`), para unificar la lógica de envío.
4. Como desarrollador, quiero consultar el estado de un mensaje con `GET /v1/messages/{id}`, para saber si llegó correctamente.
5. Como desarrollador, quiero configurar un webhook para recibir delivery reports, para actualizar el estado en mi sistema en tiempo real.
6. Como desarrollador, quiero crear y gestionar plantillas WhatsApp desde la API, para cumplir con los requisitos de Meta.
7. Como desarrollador, quiero enviar mensajes en lote con `POST /v1/messages/batch`, para campañas masivas.
8. Como dueño de PYME, quiero ver en un dashboard cuántos mensajes envié en el mes, para controlar costos.
9. Como dueño de PYME, quiero recibir una factura electrónica SII mensual con desglose, para mi contabilidad.
10. Como dueño de PYME, quiero elegir entre planes Starter, Growth o Enterprise, para ajustarme a mi volumen.
11. Como dueño de PYME, quiero pagar con Webpay (vía Flow), para usar mi medio de pago habitual.
12. Como desarrollador, quiero regenerar mi API Key si se compromete, desde el dashboard.
13. Como desarrollador, quiero ver el historial de mensajes enviados con sus estados, para debugging.
14. Como administrador de la plataforma, quiero ver el uso agregado de todos los clientes, para operaciones.
15. Como administrador, quiero configurar el markup por cliente, para ofrecer planes personalizados.
16. Como desarrollador, quiero recibir una respuesta inmediata con un `message_id` al enviar, para no bloquear mi flujo.
17. Como desarrollador, quiero una documentación clara de la API con ejemplos en Python y cURL, para integrar rápido.
18. Como dueño de PYME, quiero descargar un reporte mensual con detalle de mensajes, para auditoría.
19. Como desarrollador, quiero que la plataforma haga fallback automático entre proveedores si uno falla, para alta disponibilidad.
20. Como administrador, quiero métricas de latencia y tasas de entrega por proveedor, para optimizar costos.

## Implementation Decisions

### Arquitectura

- **Monorepo** con `backend/` (FastAPI) y `frontend/` (Angular NgModules + Tailwind).
- API REST versionada (`/v1/messages`, `/v1/templates`, `/v1/webhooks`, etc.).
- Envío asíncrono: el endpoint acepta el mensaje, lo encola en Redis (Arq), lo envía en background, y notifica vía webhook al cliente.
- Adapter Pattern para proveedores: `BaseProvider` con implementaciones concretas para Meta WhatsApp Cloud API y un agregador SMS (Mensajería.cl o equivalente SMPP).
- Fee engine configurable por cliente: costo real del proveedor + markup fijo o porcentual.

### Stack técnico

| Capa | Tecnología |
|---|---|
| API | FastAPI + SQLAlchemy Async |
| DB | PostgreSQL (RDS en AWS sa-east-1) |
| Cola | Redis + Arq |
| Frontend | Angular (NgModules) + Tailwind CSS |
| Pago/Facturación | Flow API |
| SMS Provider | Agregador local chileno (Mensajería.cl o similar) |
| WhatsApp Provider | Meta Cloud API directa (Graph v22.0) |
| Hosting | AWS Santiago (sa-east-1) |
| Autenticación | API Key + bcrypt hash en DB |
| Webhooks | Eventos vía POST al cliente configurado |

### Modelo de datos (tablas core)

- `clientes`: id, name, email, rut, plan, api_key_hash, status, created_at
- `planes`: id, name, price_clp, msg_limit, extra_msg_price
- `mensajes`: id, client_id, provider, channel, to_number, body, status, provider_msg_id, cost_clp, fee_clp, created_at
- `webhooks`: id, client_id, url, events, active, created_at
- `facturas`: id, client_id, period_start, period_end, total_msgs, total_cost, total_fee, flow_invoice_id, status, due_date
- `plantillas_whatsapp`: id, client_id, template_id (Meta), name, language, status

### API Endpoints (MVP)

- `POST /v1/messages` — enviar mensaje
- `GET /v1/messages/{id}` — consultar estado
- `POST /v1/messages/batch` — envío masivo
- `POST /v1/webhooks` — configurar webhook
- `GET /v1/webhooks` — listar webhooks
- `POST /v1/templates` — crear plantilla WhatsApp
- `GET /v1/templates` — listar plantillas
- `PUT /v1/templates/{id}` — actualizar plantilla
- `POST /v1/auth/register` — registro + API Key
- `POST /v1/auth/login` — login dashboard
- `GET /v1/balance` — consumo del mes
- `GET /v1/invoices` — historial facturas

### Proveedores

- **WhatsApp**: Meta Cloud API directa mediante WABA propia ("Message Gateway"). Todos los clientes comparten el número de la plataforma en el MVP.
- **SMS**: Agregador chileno local vía API REST/SMPP con factura CLP. Twilio como fallback internacional.
- Cada provider implementa la interfaz `BaseProvider` con métodos `send()` y `get_status()`.

### Planes de precio

- **Starter**: 1.000 msgs/mes por CLP 19.990, extra CLP 25 c/u
- **Growth**: 10.000 msgs/mes por CLP 79.990, extra CLP 18 c/u
- **Enterprise**: Volumen ilimitado, precio a medida

### Seguridad (MVP)

- HTTPS obligatorio
- API Key con bcrypt hash
- Logs sin números de teléfono en texto plano
- Rate limiting por API Key (Redis)
- Cifrado en reposo para datos sensibles (números)

## Testing Decisions

- **Unitarios**: Fee engine, adapter router, auth logic, rate limiter
- **Integración**: Cada provider adapter con mock HTTP (responses simulados de Meta y agregador SMS)
- **E2E**: Flujo completo registro → envío → webhook con proveedores reales (limitado a staging)
- **Principio**: Solo probar comportamiento externo, no implementación interna. Los tests deben poder ejecutarse sin conexión a internet mockeando HTTP.
- Coverage target mínimo: 80% en backend

## Out of Scope

- **Email channel**: No incluido en el MVP. Postergado para fase 2.
- **Embedded Signup (WhatsApp propio por cliente)**: Se usará WABA compartida en el MVP. Embedded Signup se evaluará cuando haya clientes que exijan su propio número.
- **Call center / Voz**: Fuera del roadmap inicial.
- **App móvil nativa**: Solo dashboard web por ahora.
- **Integración WhatsApp con template messaging interactivo (botones, listas)**: Se soportarán en la API pero la UI del dashboard para crearlos será básica.
- **Auto-escalado multi-región**: Un solo deploy en sa-east-1 para el MVP.
- **Onboarding automático vía Flow**: El MVP tendrá registro + pago manual asistido, el flujo 100% autoservicio con Flow va en fase 2.

## Further Notes

- La WABA compartida requiere un número telefónico verificado en Meta Business. Se recomienda registrar un número 56 9 XXXX XXX dedicado exclusivamente a la plataforma.
- Para el agregador SMS, se debe negociar contrato SMPP o API REST con un operador local. Mensajería.cl y GTMTech son opciones viables. El contrato debe incluir factura electrónica SII mensual.
- La integración con Flow debe emitir factura electrónica automáticamente al momento del pago. Flow soporta DTE (Documento Tributario Electrónico) para facturación chilena.
- Se recomienda tener un ambiente staging independiente en AWS para pruebas con proveedores reales sin afectar producción.
