# Adapter SDK

Los adapters son la frontera entre protocolos/fabricantes y el runtime
semántico. El agente, las skills y el optimizador no deben importar SDKs de
Home Assistant, Matter, MQTT u otro proveedor: solo consumen los modelos
canónicos y `AdapterPort`.

La misma regla se aplica a las fuentes de optimización. Un proveedor de tarifas,
previsión solar o batería implementa únicamente su puerto de lectura y entrega
un resultado `v1` canónico; no expone sus clientes HTTP/MQTT, credenciales ni
funciones de escritura al optimizador. `ComposedEnergyContextProvider` es el
único punto que combina esas fuentes y las presenta como `EnergyContextProvider`
al MCP Domotics y al skill. La guía de composición está en
[`docs/contracts.md`](contracts.md).

## Contrato mínimo

La interfaz está definida en
[`src/domoai/runtime/ports.py`](../src/domoai/runtime/ports.py):

| Método | Responsabilidad | Regla de implementación |
| --- | --- | --- |
| `connect()` | Abrir la sesión con el sistema externo | Debe fallar con `ConnectionError` si no está disponible. |
| `disconnect()` | Liberar sesión y recursos | Debe ser seguro llamarlo al finalizar un smoke test. |
| `discover()` | Obtener entidades, estados, áreas y fuentes no soportadas | Devuelve `AdapterSnapshot`; no genera IDs de agente. |
| `read_state(source_refs)` | Leer estado acotado de fuentes conocidas | Devuelve `StateSnapshot` con timestamps, unidad, disponibilidad y `SourceRef`. |
| `execute(command)` | Traducir un comando semántico al protocolo externo | Devuelve solo un acuse (`AdapterExecutionAck`); aceptación no equivale a éxito confirmado. |
| `subscribe_events()` | Producir cambios del sistema externo | Emite `SourceEvent`; el runtime decide cómo persistirlos. |
| `health()` | Exponer disponibilidad | Devuelve `AdapterHealth`, sin filtrar credenciales. |

El adapter debe exponer un `adapter_id` estable. El runtime usa ese valor junto
con `external_id` para construir `SourceRef` y mantener trazabilidad.

## Dispositivo físico, entidad y ruta

El runtime sigue la separación de Home Assistant: un dispositivo de origen
puede exponer varias entidades, y cada entidad puede aportar una o más
capacidades. El `device_id` de origen identifica esa agrupación; `entity_id`
identifica el endpoint exacto. Los `identifiers`, `connections` y `via_device`
del registro de Home Assistant se conservan como evidencia interna de
identidad/topología cuando el adapter los proporciona.

El registry crea un dispositivo canónico y una tabla de rutas por capacidad:

```text
(canonical_device, capability, command)
              ↓
       adapter_id + source_entity_id
```

Los nombres, áreas y etiquetas no son claves de identidad. Dos adapters solo
se fusionan mediante una relación explícita (`canonical_id`); no hay fuzzy
matching. Si una capacidad tiene más de una ruta escribible, la validación
devuelve `route_ambiguous` antes de tocar hardware. Si la fuente está caída,
devuelve `source_unavailable` y no hace failover implícito.

`CompositeAdapter` coordina cero, uno o varios adapters. El modo sin fuentes
sigue usando el fixture; con una fuente se conserva el adapter directo; con
varias se agregan discovery, lecturas, eventos, salud y ejecución bajo el
mismo `AdapterPort`. La composición añade atribución de fuente y diagnósticos,
no una superficie MCP nueva.

## Transformación semántica

`discover()` trabaja con datos propios del protocolo. La normalización hacia
`Device`, `Capability` y `StateSnapshot` pertenece al mapper del adapter y al
runtime:

1. conservar el identificador externo y el proveedor en `SourceRef`;
2. asignar un ID canónico estable (`[a-z0-9_.-]`) en el registry;
3. declarar capacidades legibles/escribibles, tipo, unidad, límites y comandos;
4. conservar entidades no disponibles, marcando el estado como `unavailable`;
5. representar lo que no se puede mapear en `unsupported_sources`.

El mapper de Home Assistant es la referencia actual para `light`, `switch`,
`cover`, `climate` y `sensor`. Un nuevo adapter no debe inventar comandos
vendor-specific en la superficie MCP; si una capacidad no tiene equivalente
canónico, debe quedar explícitamente sin soporte.

### Integración opt-in del Home Assistant Provider

`HomeAssistantProvider` puede entrar en el runtime mediante
`HomeAssistantProviderAdapter`, que implementa el mismo `AdapterPort` que los
adapters clásicos. Se activa únicamente con:

```bash
export DOMOAI_HOME_ASSISTANT_PROVIDER=1
export DOMOAI_HOME_ASSISTANT_URL="http://home-assistant.local:8123"
export DOMOAI_HOME_ASSISTANT_TOKEN="<long-lived-access-token>"
```

El factory crea un único objeto provider, lo registra en `ProviderRegistry` y
lo entrega al bridge. El bridge reutiliza el `AdapterSnapshot` normalizado del
provider, de modo que `DeviceRegistry`, `StateStore`, `PlanExecutor` y MCP no
conocen la API de Home Assistant ni abren una segunda conexión. Sin el switch,
se conserva `HomeAssistantAdapter`.

Las métricas energéticas requieren mapping explícito en un documento local v1
seleccionado por `DOMOAI_HOME_ASSISTANT_MAPPING_PATH`; no se infieren por nombre.
El bridge mantiene las rutas por entidad para que un dispositivo físico con
varias entidades conserve comandos y `SourceRef` exactos.

### Perfil de escritura Home Assistant

`HomeAssistantAdapter` traduce comandos canónicos a servicios REST de Home
Assistant:

| Comando semántico | Servicio |
| --- | --- |
| `turn_on`, `turn_off`, `toggle` | `light.*` o `switch.*` con el mismo servicio |
| `set_brightness` | `light.turn_on` + `brightness_pct` |
| `set_position` | `cover.set_cover_position` + `position` |
| `open`, `close`, `stop` | `cover.open_cover`, `cover.close_cover`, `cover.stop_cover` |
| `set_temperature` | `climate.set_temperature` + `temperature` |

La llamada usa `Authorization: Bearer ...` únicamente dentro del cliente HTTP,
rechaza respuestas no válidas y convierte fallos de conectividad en
`ConnectionError`. El adapter conserva las claves de idempotencia aceptadas
durante su ciclo de vida y devuelve un `SourceRef` para el readback del
executor.

## Ejecución segura

El flujo obligatorio es:

```text
Command → PlanService → PolicyEngine → PlanExecutor → AdapterPort
                                                        ↓
                                      readback + postcondition
```

El adapter nunca recibe órdenes directamente desde MCP, una skill o el
optimizador. Cada `Command` requiere `idempotency_key`; un adapter debe tratar
la repetición de esa clave como una operación ya procesada o rechazarla de
forma determinista. El `PlanExecutor` comprueba el estado posterior antes de
emitir `confirmed_success` y audita cada resultado.

Errores de conexión deben expresarse como `ConnectionError` para que el
runtime produzca `adapter_unavailable` y permita reintento. Un rechazo del
proveedor se devuelve como `AdapterExecutionAck(accepted=False, message=...)`;
no se deben propagar tokens, cabeceras ni cuerpos sin sanitizar.

## Pruebas y checklist de extensión

Un adapter nuevo debe incluir, como mínimo:

- fixture determinista de descubrimiento y estado;
- pruebas de IDs estables y trazabilidad a `SourceRef`;
- pruebas de disponibilidad/stale sin eliminar entidades;
- prueba de idempotencia y rechazo de comandos inválidos;
- prueba de readback/postcondition para comandos seguros;
- smoke test opt-in que solo use descubrimiento, lectura y una orden inocua.

La implementación de referencia es
[`HomeAssistantAdapter`](../src/domoai/adapters/home_assistant/adapter.py),
y el fixture local es
[`SimulatedHomeAdapter`](../src/domoai/adapters/fixtures/simulated_home.py).
El [runtime event consumer](../src/domoai/runtime/event_consumer.py) refresca
el estado canónico cuando llegan eventos y marca el cache como `stale` si se
pierde la conexión.

## Adapter nativo Zigbee2MQTT

La implementación [`Zigbee2MqttAdapter`](../src/domoai/adapters/zigbee2mqtt/adapter.py)
usa el contrato MQTT documentado por Zigbee2MQTT y un transporte inyectable.
Su perfil v1 cubre:

- `light` y `switch`: `turn_on`, `turn_off`, `toggle`;
- `light.brightness`: conversión canónica 0–100 ↔ Zigbee2MQTT 0–254;
- lecturas `temperature`, `humidity` y `occupancy`;
- discovery desde `bridge/devices`, estado, disponibilidad y `bridge/event`.

El transporte live usa `aiomqtt`; los tests usan
[`InMemoryMqttTransport`](../src/domoai/adapters/zigbee2mqtt/transport.py),
por lo que no necesitan broker ni hardware. El adapter no ofrece publicación
arbitraria, pairing, eliminación, OTA, grupos ni administración del bridge.
La configuración live y los límites están cubiertos por los tests de contrato
y el quickstart del laboratorio virtual.

## Adapter nativo Matter Server

La implementación [`MatterServerAdapter`](../src/domoai/adapters/matter/adapter.py)
usa Matter Server como frontera de control y su API WebSocket compatible con
Python. El transporte live mantiene una sola tarea lectora, valida el rango de
schema antes de descubrir y correlaciona las respuestas por `message_id`.
[`InMemoryMatterTransport`](../src/domoai/adapters/matter/transport.py) permite
probar el mismo contrato sin servidor ni hardware.

El perfil v1 cubre:

- endpoints `On/Off Light`, `Dimmable Light`, `On/Off Plug-in Unit` y
  `Dimmable Plug-in Unit`;
- `turn_on`, `turn_off`, `toggle` y `set_brightness` mediante clusters On/Off
  y Level Control;
- lecturas de temperatura, humedad y ocupación como capacidades de solo
  lectura;
- eventos de nodo, atributos, disponibilidad, eliminación y diagnósticos
  sanitizados.

La configuración live requiere `DOMOAI_MATTER_SERVER_URL` con esquema `ws://`
o `wss://` y permite ajustar `DOMOAI_MATTER_TIMEOUT_SECONDS`. No se exponen
commissioning, fabric administration, grupos, OTA, clusters vendor-specific,
lecturas/escrituras arbitrarias ni bindings directos de CHIP. La guía ejecutable
y la matriz de aceptación están cubiertos por los tests de contrato y el
quickstart del laboratorio virtual.

## Adapter nativo KNX/IP

Para desarrollo sin hardware, el perfil KNX Virtual/ETS y su mapping de
referencia están en [`dev/lab/knx-virtual.md`](../dev/lab/knx-virtual.md).


La implementación [`KnxAdapter`](../src/domoai/adapters/knx/adapter.py) usa un
mapping explícito de direcciones de grupo y DPTs. No intenta descubrir entidades
desde telegramas arbitrarios: solo proyecta entidades declaradas en el
documento `v1`. El transporte fixture
[`InMemoryKnxTransport`](../src/domoai/adapters/knx/transport.py) permite probar
el mismo contrato sin gateway.

El perfil v1 cubre:

- `light` y `switch`: `turn_on` y `turn_off` mediante DPT `1.001`;
- `light.brightness`: `set_brightness` mediante DPT `5.001`;
- lecturas `temperature` (`9.001`), `humidity` (`9.007`) y `occupancy` (`1.018`);
- eventos solo para direcciones configuradas, diagnósticos sanitizados,
  idempotencia y readback a través del runtime.

La conexión live se implementa con [`xknx`](https://xknx.io/) únicamente dentro
de `XknxTransport`; `runtime/`, MCP, skills y OR-Tools no importan xknx ni
conocen telegramas. Requiere `DOMOAI_KNX_GATEWAY_HOST` y
`DOMOAI_KNX_CONFIG_PATH` juntos. Puede ejecutarse junto con Home Assistant,
Zigbee2MQTT, Matter Server y Modbus; las rutas ambiguas se rechazan en el
runtime. La guía ejecutable y el formato completo están en el quickstart del
laboratorio y en los tests de contrato.

## Adapter nativo Modbus TCP

La implementación [`ModbusAdapter`](../src/domoai/adapters/modbus/adapter.py)
usa un mapping local estricto de entidades, unit IDs y puntos Modbus. No hace
scanning ni autodiscovery: solo proyecta entidades declaradas en el documento
`v1`. El transporte fixture
[`InMemoryModbusTransport`](../src/domoai/adapters/modbus/transport.py)
permite probar discovery, polling, disponibilidad, codec y comandos sin
controlador ni red.

El perfil v1 cubre:

- `light` y `switch`: `turn_on` y `turn_off` mediante coils booleanos;
- `light.brightness`: `set_brightness` mediante holding registers con
  conversión explícita;
- lecturas `temperature`, `humidity` y `occupancy` como capacidades de solo
  lectura;
- codecs `bool`, `uint16`, `int16` y `float32`, con orden de bytes/palabras,
  escala, offset y validación sin clamping;
- polling serializado, eventos solo cuando cambia el valor, diagnósticos
  sanitizados, idempotencia y readback a través del runtime.

La conexión live usa `PyModbusTcpTransport` únicamente dentro del adapter y
requiere `DOMOAI_MODBUS_HOST` y `DOMOAI_MODBUS_CONFIG_PATH` juntos. Permite
ajustar `DOMOAI_MODBUS_PORT`, `DOMOAI_MODBUS_TIMEOUT_SECONDS` y
`DOMOAI_MODBUS_POLL_INTERVAL_SECONDS`; puede coexistir con Home Assistant,
Zigbee2MQTT, Matter Server y KNX. RTU/ASCII, TLS, scanning,
funciones vendor-specific y registros arbitrarios quedan fuera de v1. La
guía ejecutable y el contrato están cubiertos por el quickstart del laboratorio
y los tests de contrato.

## Third-party Adapter SDK v1

El SDK [`domoai.adapters.sdk`](../src/domoai/adapters/sdk/__init__.py) permite
registrar un adapter externo sin modificar `runtime/`, MCP ni OR-Tools. El
paquete externo publica un `AdapterManifest` y una factory que devuelve el
`AdapterPort` existente:

```python
from domoai.adapters.sdk import AdapterRegistration


def domoai_adapter() -> AdapterRegistration:
    return AdapterRegistration(manifest=MANIFEST, factory=MyAdapter)
```

El host puede registrar explícitamente la factory o descubrirla de forma
opt-in mediante el grupo Python `domoai.adapters`. Descubrir entry points no
instala paquetes ni conecta dispositivos automáticamente; los errores de carga
se convierten en diagnósticos sanitizados y los ids duplicados se rechazan.

`AdapterManifest` declara la versión `v1`, protocolo, tipos de dispositivo,
capacidades canónicas, comandos, límites y funciones opcionales. El registry
puede comparar esas declaraciones con un `AdapterSnapshot` y distinguir
capacidades `supported`, `unsupported` y `optional`.

`ConformanceHarness` ejecuta pruebas locales deterministas sobre el mismo
`AdapterPort`: lifecycle, discovery estable, identidad y `SourceRef`,
disponibilidad, timestamps, eventos, un comando seguro con readback e
idempotencia. Nunca ejecuta commissioning, pairing, firmware, comandos
vendor-specific ni acciones restringidas. La guía completa está en esta
documentación y el contrato serializable en
[`schemas/v1/adapter-manifest.schema.json`](../schemas/v1/adapter-manifest.schema.json).

Los smoke tests live siguen siendo opt-in: necesitan gateway, mapping,
credenciales o servicios reales según el protocolo.

## Compatibilidad

Los cambios de modelos canónicos requieren actualizar los esquemas de
`schemas/v1/`, sus pruebas de contrato y esta guía. La versión `v1` no se
reutiliza para cambiar el significado de un campo; una incompatibilidad
semántica requiere una nueva versión de contrato y un adapter/migración
explícitos.
