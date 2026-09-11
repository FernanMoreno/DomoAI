# Auditoría: detección y conexión genérica de conectores

Fecha: 2026-09-09  
Alcance: comprobar si `discover`, `probe`, `validate`, `build_adapter` y
`diagnose` pueden conectar y detectar cualquier producto o protocolo sin lógica
específica.

## 1. Dictamen ejecutivo

**DomoAI tiene un contrato genérico de extensión, pero todavía no tiene un
motor universal de detección y traducción sin código.**

Los cinco métodos permiten incorporar un proveedor al ciclo de onboarding. No
contienen, por sí mismos, conocimiento suficiente para:

- encontrar un producto que no anuncia ningún servicio;
- hablar un protocolo que DomoAI no conoce;
- interpretar el significado de sus mensajes, registros o características;
- deducir qué acciones son seguras;
- traducir sus operaciones al modelo semántico que usa el agente.

La arquitectura actual sí permite añadir un producto nuevo de manera aislada:
se instala el paquete, aparece en el catálogo, se configura mediante la UI y
el proveedor construye un `AdapterPort`. A partir de ahí el agente recibe
dispositivos, estados y comandos normalizados, no operaciones arbitrarias del
fabricante.

La conclusión técnicamente correcta es:

> **Cualquier proveedor puede registrarse; cualquier protocolo puede tener un
> conector; ningún producto desconocido se puede detectar, interpretar y
> controlar automáticamente sin una receta o implementación específica.**

## 2. Dos descubrimientos diferentes

| Nivel | Qué descubre | Contrato | Resultado |
|---|---|---|---|
| Onboarding/provider | Gateway, broker, interfaz o endpoint al que conectarse | Cinco métodos del proveedor | `ConnectionCandidate` |
| Runtime/adapter | Dispositivos, entidades y capacidades detrás de la conexión | `AdapterPort.discover()` | `AdapterSnapshot` |

Detectar un broker MQTT no implica saber que un topic representa una persiana
ni qué payload debe publicarse para moverla. Detectar una interfaz KNX tampoco
revela por sí solo qué dirección de grupo corresponde a una luz.

## 3. Evidencia en el código actual

### 3.1 El contrato de cinco métodos es una frontera

`ConnectionProviderPort` solo exige los cinco métodos async:

- [`connection_ports.py:58`](/mnt/c/users/ferna/onedrive/escritorio/domoai/src/domoai/runtime/connection_ports.py:58)

El loader comprueba el manifiesto, la versión del contrato y la presencia de
los métodos. No puede comprobar que el proveedor entienda un producto real:

- [`connection_provider_loader.py:33`](/mnt/c/users/ferna/onedrive/escritorio/domoai/src/domoai/runtime/connection_provider_loader.py:33)
- [`connection_provider_loader.py:66`](/mnt/c/users/ferna/onedrive/escritorio/domoai/src/domoai/runtime/connection_provider_loader.py:66)

### 3.2 El scaffold generado es deliberadamente incompleto

La clase base devuelve valores seguros, pero neutros:

- `discover()` devuelve una lista vacía;
- `probe()` devuelve estado desconocido/necesita configuración;
- `validate()` devuelve inválido;
- `build_adapter()` lanza `provider_adapter_not_implemented`;
- `diagnose()` refleja el diagnóstico de validación.

Esto está en:

- [`connection_provider_base.py:36`](/mnt/c/users/ferna/onedrive/escritorio/domoai/src/domoai/runtime/connection_provider_base.py:36)
- [`connection_provider_base.py:70`](/mnt/c/users/ferna/onedrive/escritorio/domoai/src/domoai/runtime/connection_provider_base.py:70)

Generar el paquete crea una plantilla comprobable y honesta; no crea soporte
automático para el producto.

### 3.3 La conexión real pertenece al adapter

`build_adapter()` debe devolver un `AdapterPort`, que es el componente que
conoce el protocolo y ofrece al runtime:

- `connect()` y `disconnect()`;
- `discover()` de dispositivos;
- `read_state()`;
- `execute()`;
- `subscribe_events()`;
- `health()`.

Contrato:

- [`ports.py:26`](/mnt/c/users/ferna/onedrive/escritorio/domoai/src/domoai/runtime/ports.py:26)

El `ConnectionManager` valida, persiste y activa el adapter. Después,
`ManagedAdapterSet` lo conecta y lo integra con `CompositeAdapter`:

- [`connection_manager.py:163`](/mnt/c/users/ferna/onedrive/escritorio/domoai/src/domoai/application/connection_manager.py:163)
- [`connection_manager.py:221`](/mnt/c/users/ferna/onedrive/escritorio/domoai/src/domoai/application/connection_manager.py:221)
- [`managed_adapter_set.py:41`](/mnt/c/users/ferna/onedrive/escritorio/domoai/src/domoai/runtime/managed_adapter_set.py:41)

### 3.4 El agente recibe una representación semántica

`CompositeAdapter` mezcla inventarios y estados de los adapters y enruta
comandos según el registro de dispositivos. El agente usa herramientas MCP
generales:

- `discover_devices`;
- `get_state`;
- `preview_command` / `prepare_command`;
- `preview_plan` / `prepare_plan`;
- `execute_plan`.

Evidencia:

- [`composite_adapter.py:199`](/mnt/c/users/ferna/onedrive/escritorio/domoai/src/domoai/runtime/composite_adapter.py:199)
- [`composite_adapter.py:242`](/mnt/c/users/ferna/onedrive/escritorio/domoai/src/domoai/runtime/composite_adapter.py:242)
- [`composite_adapter.py:314`](/mnt/c/users/ferna/onedrive/escritorio/domoai/src/domoai/runtime/composite_adapter.py:314)
- [`domotics_server.py:352`](/mnt/c/users/ferna/onedrive/escritorio/domoai/src/domoai/mcp/domotics_server.py:352)
- [`domotics_server.py:501`](/mnt/c/users/ferna/onedrive/escritorio/domoai/src/domoai/mcp/domotics_server.py:501)
- [`domotics_server.py:647`](/mnt/c/users/ferna/onedrive/escritorio/domoai/src/domoai/mcp/domotics_server.py:647)
- [`domotics_server.py:826`](/mnt/c/users/ferna/onedrive/escritorio/domoai/src/domoai/mcp/domotics_server.py:826)

Esto evita que el agente se salte policy, consentimiento, readback o auditoría.
También implica que la traducción del producto debe existir antes.

## 4. Investigación externa

La investigación se realizó el 2026-09-09 usando especificaciones y
documentación primaria.

### Descubrimiento de red

DNS-SD define cómo encontrar instancias mediante registros PTR, SRV y TXT, y
mDNS permite hacerlo en el enlace local. El resultado principal es una
instancia, host, puerto y metadatos anunciados; no una ontología de
dispositivos ni un mapa de comandos:

- [RFC 6763 — DNS-Based Service Discovery](https://datatracker.ietf.org/doc/html/rfc6763)
- [RFC 6762 — Multicast DNS](https://datatracker.ietf.org/doc/html/rfc6762)

Home Assistant trata mDNS, SSDP, DHCP, MQTT, Bluetooth, HomeKit, USB y otros
mecanismos como métodos separados de descubrimiento:

- [Home Assistant — device discovery](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/discovery/)
- [Home Assistant — network discovery](https://developers.home-assistant.io/docs/network_discovery/)

### MQTT

MQTT es publish/subscribe y es agnóstico al contenido del payload. Conectarse
al broker no dice qué topics existen ni qué significa cada mensaje:

- [OASIS MQTT Version 5.0](https://docs.oasis-open.org/mqtt/mqtt/v5.0/mqtt-v5.0.html)

MQTT Discovery de Home Assistant reduce el trabajo porque añade una convención
de topics, payloads, componentes e identificadores. Esa convención no forma
parte de MQTT en general:

- [Home Assistant MQTT Discovery](https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery)

### Modbus y KNX

Modbus define funciones y transacciones request/reply, pero el significado de
un registro depende del fabricante, modelo, escala, endianess, tipo de dato y
mapa de registros:

- [Modbus Application Protocol Specification V1.1b3](https://modbus.org/docs/Modbus_Application_Protocol_V1_1b3.pdf)

KNXnet/IP puede transportar telegramas y descubrir interfaces, pero la
interpretación funcional necesita proyecto ETS, direcciones de grupo y tipos
de punto de datos. KNX explica que el significado de «una luz encendida» no
va incluido en el telegrama:

- [KNX — interfaces y KNXnet/IP](https://support.knx.org/hc/en-us/articles/360012026220-Interfaces)
- [KNX — interoperability and semantics](https://www.knx.org/news/knx-iot-future-lies-ip)

### Matter, OpenAPI y WebSocket

Matter estandariza el descubrimiento de dispositivos comisionables, pero aún
requiere commissioning seguro y adaptación de clusters, atributos y comandos:

- [Matter 1.1 Core Specification](https://csa-iot.org/wp-content/uploads/2023/05/22-27349-002_matter-1-1-core-specification.pdf)

OpenAPI reduce el código para APIs HTTP descritas formalmente, pero no define
la semántica domótica, unidades, límites físicos o acciones seguras:

- [OpenAPI Specification](https://spec.openapis.org/oas/latest.html)

WebSocket define handshake y framing sobre TCP; los mensajes de aplicación
siguen siendo específicos del producto:

- [RFC 6455 — The WebSocket Protocol](https://datatracker.ietf.org/doc/html/rfc6455)

### OAuth

OAuth Authorization Server Metadata descubre endpoints y capacidades del
servidor de autorización. No crea el `client_id`, no concede scopes y no
sustituye el registro de la aplicación:

- [RFC 8414 — OAuth 2.0 Authorization Server Metadata](https://datatracker.ietf.org/doc/html/rfc8414)

## 5. Matriz de universalidad

| Familia | Genérico posible | Sigue siendo específico |
|---|---|---|
| HTTP REST | URL, TLS, autenticación, OpenAPI y operaciones HTTP | Rutas, payloads, unidades, límites y semántica |
| MQTT | Broker, TLS, topics, QoS y discovery conocido | Payloads, comandos, readback y disponibilidad |
| WebSocket | Handshake, TLS y framing | Tipos de mensajes y eventos |
| Modbus TCP/RTU | Transporte y function codes | Registros, escalas, unidades, endianess y permisos |
| KNX/IP | Interfaz y tunnelling/routing | ETS, direcciones, DPT y semántica |
| Matter | Discovery y commissioning estándar | Seguridad, clusters, atributos y adaptación local |
| mDNS/DNS-SD/SSDP/DHCP | Host, servicio y puerto | Identidad del producto y configuración de sesión |
| BLE/Zigbee/Thread/USB/serial | Transporte si existe implementación | Pairing, perfiles, permisos y modelo del fabricante |
| Propietario | Solo después de crear un driver | Casi todo el protocolo y su semántica |

## 6. Hallazgos priorizados

### CON-001 — P1: el contrato no implementa autodetección universal

El loader valida forma y compatibilidad del proveedor; el scaffold base retorna
estados neutros. No existe una fase que pruebe de forma segura todos los
transportes, puertos, credenciales y convenciones.

**Recomendación:** presentar el scaffold como kit de integración y conservar el
estado `incomplete`/`unsupported` hasta que exista un adapter real.

### CON-002 — P1: provider discovery y device discovery son distintos

Implementar `ConnectionProvider.discover()` solo encuentra candidatos de
conexión. El agente no recibe dispositivos hasta que el adapter activo
implemente su propio `discover`, inventario, estado y eventos.

**Recomendación:** exigir en el checker una prueba candidate → setup → adapter
→ inventory → state → command → readback.

### CON-003 — P1: conectar el transporte no aporta semántica

Un socket abierto, un HTTP 200, una respuesta MQTT o una interfaz KNX visible
solo demuestran conectividad parcial. No demuestran que se pueda controlar el
producto correctamente.

**Recomendación:** exigir un mapping versionado de identidad, capacidad,
unidad, rango, comando, error y confirmación. Si falta, activar debe fallar
cerrado.

### CON-004 — P1: falta un registro común de estrategias de discovery

La manifest declara si soporta discovery, pero no describe una estrategia
concreta como mDNS, SSDP, DHCP, MQTT Discovery, Matter, BLE, USB, serial o
manual.

**Recomendación:** crear estrategias reutilizables que devuelvan candidatos,
con timeouts, límites de red y diagnósticos. Nunca deben inventar capacidades.

### CON-005 — P2: un formulario de endpoint no cubre todos los transportes

MQTT necesita broker y topics; Modbus puede ser TCP o serial; KNX usa
tunnelling/routing; Matter tiene commissioning; BLE, Thread, Zigbee y USB
tienen pairing y permisos distintos.

**Recomendación:** modelar `transport_family`, `discovery_strategy`,
`auth_scheme`, `endpoint_fields` y `commissioning_requirements`.

### CON-006 — P2: las sondas genéricas pueden tener efectos operativos

Probar puertos y endpoints puede filtrar información, activar pairing, tocar
un equipo equivocado o incumplir rate limits.

**Recomendación:** separar probes read-only de commissioning/control; aplicar
allowlist de red, timeouts, límites de payload, cancelación, redacción y
consentimiento para operaciones con efecto.

## 7. Solución recomendada para crear conectores fáciles

### Nivel A — Receta declarativa

Para protocolos con convenciones suficientes, el autor aporta manifest y
mapping, y DomoAI reutiliza el adapter genérico:

```yaml
connector_id: acme-mqtt
transport_family: mqtt
device_type: light
discovery_strategies: [manual, mqtt_discovery]
auth_schemes: [username_password, tls_certificate]
capabilities:
  - capability_id: power
    kind: boolean
    state_topic: devices/{device_id}/state
    command:
      name: power
      command_topic: devices/{device_id}/set
      payload_template: '{value}'
      readback_topic: devices/{device_id}/state
```

### Nivel B — Transporte conocido y perfil de producto

El transporte puede ser común, pero el autor entrega un perfil:

- Modbus: registros, funciones, endianess, escalas y unidades;
- KNX: direcciones de grupo, DPT y roles de lectura/escritura;
- REST: OpenAPI, rutas, payloads, autenticación y operaciones permitidas;
- WebSocket: mensajes, payloads y correlación del readback.

### Nivel C — Protocolo propietario

El proveedor implementa los cinco métodos y crea un adapter propio. El agente
continúa viendo solo el contrato semántico de DomoAI. Este es el límite normal
entre una plataforma de integración y un protocolo privado.

## 8. Contrato mínimo para marcar un conector como listo

El checker debería exigir:

1. Manifest sin secretos y con familia de transporte.
2. Estrategia de discovery o setup manual explícito.
3. Identidad estable del gateway y de cada dispositivo.
4. Credenciales fuera de endpoint/options y resolución segura.
5. `connect`, `disconnect`, `discover`, `read_state`, `execute`, eventos y
   `health` del adapter.
6. Mapping de capacidades con unidades, rangos, permisos y errores.
7. Readback para comandos que pretendan estar confirmados.
8. Diagnósticos sanitizados y accionables.
9. Tests de credencial inválida, timeout, reconexión, dispositivo desconocido
   y comando rechazado.
10. Fixture o prueba realista, indicando qué cualificación física queda fuera.

## 9. Plan recomendado

### P0 — Corregir la promesa

- Llamar al scaffold «kit de integración», no «autodetector universal».
- Separar provider discovery de adapter discovery en documentación y UI.
- Mantener `incomplete` hasta que exista implementación funcional.

### P1 — Plataforma de recipes

- `DiscoveryStrategyRegistry`: manual, mDNS/DNS-SD, SSDP, DHCP, MQTT y Matter.
- `TransportAdapterRegistry`: REST/OpenAPI, MQTT, WebSocket, Modbus y KNX.
- `CapabilityMapping` versionado y validado.
- Generador que pregunte por transporte, discovery, auth y mapping.
- Tests de candidate → adapter → inventory → command → readback.

### P2 — Operación y evidencia

- Descubrimiento multi-interfaz y diagnóstico de bloqueos multicast.
- Rate limits, timeouts, cancelación y límites de payload por estrategia.
- Perfiles por fabricante/modelo/firmware.
- Qualification física por instalación.

## 10. Veredicto final

**Los cinco métodos no pueden conectar y detectar cualquier cosa por sí solos.**
Son la interfaz correcta para desacoplar proveedores y uniformar el onboarding.
Para conseguir una experiencia realmente fácil hacen falta recetas declarativas
y adapters genéricos por familia de transporte, manteniendo un mapping
semántico y código propietario cuando el producto lo requiera.

La afirmación defendible para DomoAI es:

> **DomoAI puede registrar cualquier conector que cumpla el contrato y exponer
> al agente cualquier producto que el conector traduzca al modelo semántico
> común. La detección y traducción automáticas dependen de metadatos
> interoperables o de un perfil específico del producto.**

## 11. Trazabilidad

Fuentes internas inspeccionadas:

- `src/domoai/runtime/connection_ports.py`
- `src/domoai/runtime/connection_provider_base.py`
- `src/domoai/runtime/connection_provider_loader.py`
- `src/domoai/application/connection_providers.py`
- `src/domoai/application/connection_manager.py`
- `src/domoai/runtime/managed_adapter_set.py`
- `src/domoai/runtime/composite_adapter.py`
- `src/domoai/runtime/ports.py`
- `src/domoai/mcp/domotics_server.py`

La estructura de dependencias se contrastó mediante Graphify y las
afirmaciones importantes se verificaron contra el código fuente. Esta
auditoría no certifica la compatibilidad física de ningún fabricante ni
reemplaza una prueba de laboratorio.
