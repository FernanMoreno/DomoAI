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

### Integración Home Assistant (Provider)

`HomeAssistantProvider` es la única integración de Home Assistant del
runtime, expuesta mediante `HomeAssistantProviderAdapter`, que implementa
el mismo `AdapterPort` que el resto de adapters. Se activa configurando:

```bash
export DOMOAI_HOME_ASSISTANT_URL="http://home-assistant.local:8123"
export DOMOAI_HOME_ASSISTANT_TOKEN="<long-lived-access-token>"
```

El factory crea un único objeto provider, lo registra en `ProviderRegistry` y
lo entrega al bridge. El bridge reutiliza el `AdapterSnapshot` normalizado del
provider, de modo que `DeviceRegistry`, `StateStore`, `PlanExecutor` y MCP no
conocen la API de Home Assistant ni abren una segunda conexión.

> Un adapter previo (`HomeAssistantAdapter`, implementación directa de
> `AdapterPort` sin pasar por la capa Provider SDK) coexistió como default
> reversible durante el rollout de Provider (Spec 018) hasta ser retirado
> por convergencia deliberada (Spec 081, P2 #9 del re-audit
> 2026-08-19) — su lógica de traducción de comandos y routing era un
> superconjunto exacto duplicado de forma independiente, riesgo de drift
> semántico que ya se había materializado (Provider ganó capacidades de
> energía/batería que el adapter clásico nunca tuvo).

Las métricas energéticas requieren mapping explícito en un documento local v1
seleccionado por `DOMOAI_HOME_ASSISTANT_MAPPING_PATH`; no se infieren por nombre.
El bridge mantiene las rutas por entidad para que un dispositivo físico con
varias entidades conserve comandos y `SourceRef` exactos.

Un binding de `battery.capacity` debe declarar además una atestación nominal
estructurada (`vendor_documentation` o `installer_attestation`, referencia,
modelo, attester y timestamp). El provider la conserva en la medición
canónica; el bridge de energía rechaza capacidad medida sin esa provenance.
La atestación es auditable pero no criptográfica: no se descargan URLs ni se
verifican claims durante el runtime, y nunca concede autorización de escritura.

Para una composición despachable, el host debe suministrar además una
`NominalCapacityTrustPolicy` server-owned con coincidencias exactas para el
tipo de evidencia, el attester y la referencia revisada. El SDK no puede
derivar esa policy de texto del proveedor ni de una entidad descubierta.
`StateStoreBatteryProvider` la aplica únicamente al consumo de capacidad
`provider_measurement` por un perfil con actuador; un perfil analysis-only y
la capacidad estática `provider_config` conservan sus rutas explícitas sin
convertirse por ello en autorización física. Esta policy tampoco es firma
criptográfica ni activa wiring automático en `build_runtime`.

La composición física debe utilizar el agregado tipado
`DispatchableBatteryBinding`: enlaza provider/device, `BatteryProfile`,
capacidad, actuador, feedback y la ruta canónica de reconciliación SOC. El
host lo entrega explícitamente a
`StateStoreBatteryProvider.from_binding()`; el SDK no crea ese binding a
partir de nombres, áreas o entidades descubiertas. La disponibilidad y
unicidad de las rutas continúan bajo `DeviceRegistry` y la validación de
escenario.

### Rutas HA explícitas para batería despachable (Spec 109)

La configuración v1 puede declarar `battery_dispatch_bindings` como un mapa
credential-free de rutas estáticas. Cada binding fija el `device_id` de
Home Assistant, las entidades exactas de `battery.soc` (`%`),
`battery.power` (`kW`) y `battery.capacity`, y tres rutas de comando
`charge`/`discharge`/`stop` con su `entity_id` y `provider_command`.

La carga exige que la entidad de capacidad ya tenga un
`battery_capacity_bindings` compatible y que su `device_id` coincida. Se
rechazan campos desconocidos, IDs que no cumplen el formato de entidad HA,
comandos vacíos/duplicados y referencias cruzadas. No se aceptan tokens,
payloads de servicio ni aliases por nombre, área o modelo.

Este binding solo declara el lado HA del futuro wiring. No comprueba que las
entidades existan en una instancia viva, que sean escribibles, que exista una
ruta única en `DeviceRegistry` o que el readback de potencia/SOC converja.
`build_runtime` lo parsea como configuración inerte: no instala
`StateStoreBatteryProvider`, no crea `BatteryActuator` y no llama servicios.
La composición posterior debe convertirlo explícitamente al contrato
canónico de Spec 108 después de discovery, policy, safety y readback.

Contrato detallado: [`specs/109-home-assistant-battery-route-binding/contracts/home-assistant-battery-route-binding.md`](../specs/109-home-assistant-battery-route-binding/contracts/home-assistant-battery-route-binding.md).

### Validación read-only de rutas HA (Spec 110)

Después de obtener un snapshot mediante `await provider.snapshot()`, el host
puede ejecutar `provider.validate_battery_dispatch_routes(snapshot)`. La
validación comprueba, sin refrescar ni escribir, que las entidades declaradas
por Spec 109 pertenecen al `device_id` exacto, que SOC/potencia/capacidad son
actuales, numéricas y tienen las unidades/semánticas esperadas, que la
capacidad conserva `device_class=energy_storage` y que charge/discharge/stop
apuntan a capabilities escribibles que exponen los comandos declarados.

Además, cada comando debe ser traducible por el perfil de escritura existente
del provider. Esto evita aceptar una capability `writable` que el provider
todavía no puede convertir en una acción HA. La comprobación usa únicamente el
traductor puro local: no consulta el registro de servicios ni llama a HA. Las
rutas con parámetros (`set_position`, `set_brightness` o `set_temperature`)
requieren un contrato explícito de parámetros y no pasan este binding v1 sin
ese valor.

El validator no prueba comandos llamando a Home Assistant, no elige entidades
alternativas y no crea `BatteryActuator`, `EnergyContext` ni
`StateStoreBatteryProvider`. Un éxito es únicamente un gate de disponibilidad
de ruta; policy, aprobación, Safety Kernel, preconditions y readback siguen
siendo gates posteriores.

Contrato detallado: [`specs/110-home-assistant-battery-route-validation/contracts/home-assistant-battery-route-validation.md`](../specs/110-home-assistant-battery-route-validation/contracts/home-assistant-battery-route-validation.md).

La Spec 111 añade el gate de traducibilidad. El binding de batería acepta en
esta fase únicamente rutas cuyo comando ya tenga todos sus parámetros en el
contrato; una capability declarada pero no traducible falla cerrado con
`HomeAssistantMappingConfigurationError`.

### Rutas numéricas y smoke HIL del inversor (Spec 114)

Una ruta de batería puede declarar explícitamente `number.set_value` y una
transformación `as_is`, `negate` o `zero`. `as_is` transmite el valor kW
recibido, `negate` transmite su negación y `zero` transmite cero para parada.
El provider proyecta la capability de comando `battery_control` en las
entidades configuradas, sin crear telemetría. Para rutas numéricas exige
unidad `kW`, límites finitos y que el dispositivo acepte cero. Una ruta legacy
de cover/switch que reciba un valor numérico se rechaza antes de llamar a
Home Assistant; nunca se descarta silenciosamente el setpoint.

El test `test_home_assistant_inverter_hil_power_and_soc_converge` es un smoke
físico opt-in. Requiere URL/token, mapping, perfil/evidencia, ID canónico,
binding, dirección, potencia acotada, token de operador y la confirmación
literal `I_UNDERSTAND_REAL_BATTERY_HIL`. Ejecuta un único probe aprobado,
lee potencia y SOC con lecturas nuevas hasta obtener dos observaciones
estables y siempre intenta `stop` en `finally`. El laboratorio virtual no
certifica este escenario y la suite normal lo omite.

### Composición explícita HA → binding canónico (Spec 112)

El host puede convertir una declaración HA validada en el agregado canónico
mediante `compose_home_assistant_dispatchable_battery_binding(...)`. La función
recibe el provider, el `AdapterSnapshot` ya obtenido, el ID del binding, el
`canonical_device_id` resuelto por el `DeviceRegistry`, un `BatteryProfile`
server-owned, `BatteryCapacityEvidence` y, cuando procede, la
`NominalCapacityTrustPolicy`.

Antes de construir el agregado reutiliza la validación de rutas y comprueba que
los tres comandos del `BatteryActuator`, su dispositivo, feedback
`battery.power`/`kW`, reconciliación `battery.soc`, evidencia de capacidad y
capability writable común coinciden con el documento HA. La ambigüedad o
cualquier mismatch falla cerrado.

El `device_id` del resultado es el ID canónico del runtime, no el `device_id`
físico de Home Assistant. El binding HA conserva la identidad de fuente para
validar las entidades, mientras `StateStore`/`Plan` usan el ID canónico. El
resultado es únicamente un `DispatchableBatteryBinding`. La función no
instala `StateStoreBatteryProvider`, no modifica `build_runtime`, no persiste,
no refresca Home Assistant y no llama servicios. El host debe decidir
explícitamente cuándo pasar el agregado a
`StateStoreBatteryProvider.from_binding()` después de sus gates de registry,
policy, safety y readback.

### Instalación explícita en el runtime (Spec 113)

Una vez completadas esas gates, un host confiable puede entregar el agregado a
`build_runtime` mediante `dispatchable_battery_binding=...`. El composition
root crea el `StateStoreBatteryProvider` usando el `StateStore` de ese runtime
y lo pasa al `ComposedEnergyContextProvider` existente. La construcción es
inercial: no refresca Home Assistant, no llama servicios y no escribe hardware.

Si no se entrega el binding, el runtime conserva el comportamiento previo sin
batería. Si se entrega con `energy_live=False`, falla cerrado antes de abrir
SQLite o conectar el adapter. La presencia de
`battery_dispatch_bindings` en el mapping HA por sí sola nunca activa este
wiring.

### Perfil de escritura Home Assistant

`HomeAssistantProvider` traduce comandos canónicos a servicios REST de Home
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

El set `_executed_idempotency_keys` que usan los adapters incluidos
(Home Assistant, Matter, Zigbee2MQTT, Modbus, KNX) es supresión de
duplicados **best-effort y local al proceso**: vive en memoria, se
reinicia en cada restart y no se comparte entre procesos. No es la
barrera de seguridad autoritativa. Esa barrera es el claim de ejecución
atómico y persistente en `PlanRepository.claim_for_execution` (ver
Spec 057), que garantiza que un plan sólo puede reclamarse una vez
incluso ante ejecutores concurrentes o un restart. El set del adapter
es una optimización adicional para detectar duplicados rápido dentro
de la misma sesión de proceso, no un sustituto de esa garantía.

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
[`HomeAssistantProviderAdapter`](../src/domoai/adapters/home_assistant/provider_adapter.py),
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

Las implementaciones pueden aceptar el `ExecutionContext` opcional como
segundo argumento de `execute()`. El runtime siempre lo proporciona y el
adapter debe reenviarlo a su provider/transport para conservar
`plan_id`, `execution_attempt_id` y `adapter_request_id`. Las llamadas directas
sin contexto siguen siendo válidas durante la migración; el harness de
conformance ejercita la ruta context-aware para detectar adapters que aún no
pueden participar en correlación end-to-end.

Los smoke tests live siguen siendo opt-in: necesitan gateway, mapping,
credenciales o servicios reales según el protocolo.

## Compatibilidad

Los cambios de modelos canónicos requieren actualizar los esquemas de
`schemas/v1/`, sus pruebas de contrato y esta guía. La versión `v1` no se
reutiliza para cambiar el significado de un campo; una incompatibilidad
semántica requiere una nueva versión de contrato y un adapter/migración
explícitos.
