# Contratos públicos

## Fuente única y serialización

Los modelos Pydantic de
[`src/domoai/domain/models.py`](../src/domoai/domain/models.py) y
[`src/domoai/optimizer/`](../src/domoai/optimizer/) son la fuente de verdad.
Los JSON Schema publicados en [`schemas/v1/`](../schemas/v1/) se regeneran con
`uv run python scripts/export_schemas.py` y se validan en las pruebas de
contrato.

Los modelos usan `schema_version: "v1"`, timestamps con zona horaria y
`extra="forbid"`. Por tanto, una entrada desconocida no se ignora
silenciosamente: produce un error de validación seguro.

## Superficie MCP v1

El servidor stdio local registra estas tools semánticas:

| Tool | Efecto |
| --- | --- |
| `discover_devices` | Lee o refresca el inventario canónico; admite `area_id`, tipos y `refresh`. |
| `get_state` | Lee estados acotados por dispositivos/capacidades. |
| `get_energy_context` | Lee un horizonte completo de tarifas, solar y batería opcional mediante un provider tipado. |
| `validate_command` | Valida un comando sin invocar el adapter. |
| `validate_plan` | Aplica capacidades, políticas, revisión y digest a un plan. |
| `execute_plan` | Ejecuta un plan validado, con digest y aprobación cuando corresponda. |

Resources de solo lectura:

```text
domotics://areas
domotics://capabilities
domotics://devices
domotics://energy
domotics://policies
```

Las respuestas estructuradas incluyen `schema_version` y, cuando procede,
`runtime_revision`. Las operaciones de mutación pasan por la frontera de plan
y política; no existe una tool MCP por fabricante.

## Planes, políticas y ejecución

Un `Plan` contiene entre 1 y 50 `Command`. Cada comando identifica
`device_id`, `command`, valor/unidad opcionales, precondiciones, `risk_class` e
`idempotency_key`. La validación devuelve:

- `status`: `valid`, `invalid`, `stale` o `requires_confirmation`;
- `runtime_revision` y `digest` para detectar cambios entre preview y execute;
- decisiones de política y errores tipados (`ErrorDetail`).

El digest debe coincidir en `execute_plan`. Si cambia el inventario, estado,
política o expiración del plan, el runtime exige validación de nuevo. Las
acciones sensibles no se autoaprueban: requieren un `approval` explícito y el
executor produce un `ExecutionOutcome` por comando, con estado anterior,
posterior y error sanitizado.

Los errores MCP siguen la forma:

```json
{
  "error": {
    "code": "validation_error",
    "message": "Input does not satisfy the v1 contract",
    "retryable": false,
    "details": {}
  }
}
```

No se incluyen tokens, cabeceras de autorización ni secretos en errores o
eventos de auditoría.

## Runtime multi-adapter

La configuración acepta varias fuentes completas a la vez. Cada adapter
mantiene su `adapter_id`; el `CompositeAdapter` los coordina y anota esa
procedencia en las lecturas, eventos y diagnósticos internos. El MCP continúa
recibiendo únicamente `Device`, `Capability`, `StateSnapshot` y planes
canónicos.

La identidad sigue el modelo dispositivo/entidad de Home Assistant: varias
entidades pueden pertenecer a un dispositivo físico, pero cada una conserva su
`SourceRef`. Las claves estables (`identifiers`/`connections`) permiten
mantener la identidad dentro de una fuente; un `canonical_id` explícito es el
único mecanismo de enlace entre adapters. Los nombres y áreas nunca fusionan
dispositivos por sí solos.

Antes de ejecutar, el registry resuelve cada comando a una única ruta
`(canonical_device, capability, command) → (adapter_id, source_entity_id)`.
`route_ambiguous`, `route_not_found`, `source_unavailable` y referencias
desconocidas son errores de validación y producen cero escrituras. No existe
failover automático entre rutas ambiguas. Una fuente caída se marca no
disponible y las contribuciones de las fuentes sanas permanecen operativas.

## Provider SDK v1

El Provider SDK es la frontera para futuras fuentes de telemetría y comandos,
no un segundo modelo de dispositivos. Reutiliza `DeviceType`, `Capability`,
`SourceRef` y `ExecutionStatus` del dominio canónico y añade:

- `ProviderManifest`: identidad, protocolo, versión de paquete, roles
  (`telemetry`, `commands`) y capacidades semánticas no sensibles.
- `DeviceDescriptor`: identidad local del proveedor, metadatos, claves de
  identidad, conexiones y topología antes de una posible fusión canónica.
- `Measurement`: métrica canónica, unidad, valor, calidad, timestamps con zona
  horaria y `SourceRef` cuyo adapter coincide con `provider_id`.
- `ProviderCommand`/`ProviderExecutionResult`: traducción acotada con
  idempotencia, sin registros Modbus, topics MQTT, entidades HA o credenciales.
- `ProviderRegistry`: orden estable por ID, routing por rol, resultados
  parciales y diagnósticos sanitizados cuando un proveedor falla.

Un proveedor puede ser solo de lectura, solo de comandos o combinado. MCP,
OR-Tools, `StateStore` y `AdapterPort` siguen viendo contratos semánticos. La
implementación concreta `HomeAssistantProvider` reutiliza el cliente
autenticado existente y la normalización semántica del adapter clásico: agrupa
entidades por `device_id`, consulta el registro de entidades por WebSocket
cuando el payload de estados no trae ese dato, conserva metadatos de identidad
y traduce únicamente comandos declarados.

El runtime puede activar este provider de forma opt-in con
`DOMOAI_HOME_ASSISTANT_PROVIDER=1`. En ese modo registra el mismo objeto en
`ProviderRegistry` y lo envuelve en `HomeAssistantProviderAdapter`, que satisface
`AdapterPort` y alimenta el `DeviceRegistry`, `StateStore`, ejecución y MCP
existentes. Así se mantiene una única conexión y una única ruta semántica. Sin
el switch, el factory conserva el `HomeAssistantAdapter` clásico.

La [documentación del registro de dispositivos de Home Assistant](https://developers.home-assistant.io/docs/device_registry_index/)
sirve como referencia para preservar identificadores, conexiones,
fabricante/modelo y relaciones `via_device`, pero no autoriza a inferir por sí
sola la semántica energética de una entidad.

Los sensores energéticos requieren un mapping explícito por entidad y
capacidad, por ejemplo `sensor.pv_power` + `power` → `energy.pv.power`.
El provider no adivina si un sensor de potencia representa fotovoltaica, red,
consumo, batería o vehículo. La REST API proporciona estados y servicios; la
WebSocket API proporciona `state_changed` y el registro de entidades. El token
queda exclusivamente en el cliente de transporte.

Los schemas publicados son `provider-manifest`, `device-descriptor`,
`measurement`, `provider-command`, `provider-execution-result`,
`provider-diagnostic`, `provider-discovery-result` y
`provider-collection-result` bajo [`schemas/v1/`](../schemas/v1/). El contrato
completo y el alcance diferido están reflejados en los schemas publicados y
en los tests de contrato del repositorio.

## Optimización

`OptimizationScenario` es una DSL intermedia, no código Python ejecutable. Sus
componentes son `horizon`, `loads`, `constraints`, `objectives`, `inputs`,
`assumptions` y un límite de solver. La validación semántica comprueba que el
dispositivo/capacidad/comando existen, que las unidades son compatibles y que
las cargas caben en el horizonte.

`OptimizationResult` devuelve `status` (`optimal`, `feasible`, `infeasible`,
`invalid`, `timeout` o `unknown`), diagnóstico, objetivos y un `Plan` de
propuesta. El solver nunca llama a un adapter: el plan se valida de nuevo y
se ejecuta únicamente mediante el runtime y sus políticas.

Cuando el escenario incluye `energy_context`, el dominio valida una serie
completa y ordenada por slot, con `W`/`kW` para potencia y `kWh` para energía y
SOC. El modelo acotado calcula por slot carga, solar, importación/exportación
de red y batería; impide carga y descarga simultáneas y devuelve evidencia en
`constraint_summary.slots`. Soporta `minimize_energy_cost`,
`minimize_peak_import` y `maximize_solar_self_consumption`, además del
`minimize_start` existente. Un contexto inválido, una restricción imposible o
un timeout no contienen `plan`.

El provider `EnergyContextProvider` es una frontera de solo lectura. En v1 la
implementación de aceptación es `StaticEnergyContextProvider`, por lo que no
se requieren tarifas, meteorología, solar ni batería live. La tool MCP
`get_energy_context` recibe solo un `Horizon` y devuelve un envelope con
`runtime_revision`; no expone protocolos, registros ni permisos de escritura.

La implementación compuesta `ComposedEnergyContextProvider` mantiene esa misma
frontera y recibe tres puertos independientes: `TariffProvider`,
`SolarForecastProvider` y `BatteryProvider` opcional. Cada proveedor devuelve
un resultado v1 con `Horizon`, `source_id`, `source_revision` y
`observed_at`; el composer comprueba alineación de slots, frescura y
timestamps antes de construir el `EnergyContext`. El revision combinado es
determinista y los fallos se serializan como `EnergyProviderDiagnostic`, sin
excepción original, URL, cabecera ni credencial.

Un proveedor live futuro debe traducir sus unidades y semántica antes de
devolver el modelo canónico. Por ejemplo, [OMIE documenta periodos de 15
minutos](https://www.omie.es/en/market-results/daily/daily-market/day-ahead-price?scope=daily)
y precios de mercado que pueden ser negativos, por lo que `TariffPoint`
admite valores negativos y el adaptador debe convertir EUR/MWh a EUR/kWh. La
[API pública de REE](https://www.ree.es/es/datos/apidatos) es una fuente REST de
solo lectura con rangos temporales y agregación; sus detalles de transporte
no deben aparecer en MCP. [Open-Meteo](https://open-meteo.com/en/docs) expone
radiación solar en W/m²: no equivale automáticamente a potencia fotovoltaica
en kW, así que un proveedor debe aplicar parámetros de instalación explícitos
o usar una fuente de producción ya convertida.

El primer wrapper concreto es `OmieTariffProvider`. Lee, cuando el host lo
inyecta explícitamente, el fichero público `marginalpdbc_YYYYMMDD.v`, valida
los 92/96/100 periodos correspondientes al día local usando intervalos UTC,
selecciona `MarginalES` y convierte EUR/MWh a EUR/kWh. El cliente HTTP y el
parser son inyectables/testeables; un fallo de red o de formato se convierte
en `EnergyProviderError` sin cuerpo ni URL. El runtime live opt-in puede
componerlo con `OpenMeteoSolarProvider`; el modo por defecto sigue sin crear
clientes de red.

`OpenMeteoSolarProvider` solicita `global_tilted_irradiance` en `minutely_15`
y timestamps UNIX, y convierte W/m² mediante
`installed_kwp × irradiance / 1000 × performance_ratio`, con límite AC
opcional del inversor. El provider valida cobertura temporal exacta antes de
devolver kW canónicos y no expone detalles de Open-Meteo a MCP u OR-Tools. La
configuración de ambos providers exige `DOMOAI_ENERGY_LIVE=1`; si está ausente
o es falso, el runtime conserva fixtures/providers locales y no realiza red.
El contrato se valida con los tests de provider OMIE/Open-Meteo y sus smoke
tests independientes.

La instalación solar puede configurarse una sola vez mediante el schema
`solar-installation-profile` publicado:

```bash
export DOMOAI_ENERGY_LIVE=1
export DOMOAI_TARIFF_PROVIDER=omie
export DOMOAI_SOLAR_PROVIDER=open_meteo
export DOMOAI_SOLAR_PROFILE_PATH=config/solar-profile.json
```

El perfil persistente contiene únicamente metadatos físicos y procedencia;
`config/solar-profile.example.json` documenta su forma. Las variables solares
individuales existentes siguen funcionando como fallback de compatibilidad,
pero no pueden combinarse con el perfil. OMIE y Open-Meteo continúan
recogiéndose dinámicamente al solicitar el contexto energético. El límite
entre ambas clases de datos evita que el runtime invente kWp, orientación o
rendimiento cuando no están disponibles. La fuente JSON es reemplazable por
una futura fuente de Home Assistant o inversor sin cambiar MCP.

## Orquestación del skill de energía

El skill portable `optimize-home-energy` declara la secuencia y el proveedor
semántico de cada operación, sin fijar nombres de servidores:

| Operación | Rol | Tool | Modo |
| --- | --- | --- | --- |
| `discover_devices` | `domotics` | `discover_devices` | lectura |
| `get_state` | `domotics` | `get_state` | lectura |
| `get_energy_context` | `domotics` | `get_energy_context` | lectura |
| `optimize_scenario` | `ortools` | `optimize_scenario` | propuesta |
| `validate_plan` | `domotics` | `validate_plan` | validación |
| `explain_solution` | `ortools` | `explain_solution` | lectura |
| `operator_approval` | `operator` | `request_approval` | aprobación |
| `execute_plan` | `domotics` | `execute_plan` | mutación |

El workflow coordina ambos MCP mediante puertos semánticos inyectados. En v2
lee `get_energy_context` antes de construir la propuesta y detiene el flujo si
falta el contexto o su revisión no coincide. No es un MCP adicional, no llama
adapters ni `OptimizerPort` directamente y no
autoriza su propio plan. La validación final, revisión, digest, aprobación y
postcondiciones permanecen en el Domotics Runtime. Las entradas desconocidas,
estado obsoleto, propuestas no factibles, cambios de revisión/digest y
respuestas malformadas detienen el workflow antes de ejecutar.

Para proteger una reanudación sin añadir una tool MCP, el router del host
expone además la revisión runtime actual como metadato de solo lectura. El
workflow la compara con la revisión validada antes de solicitar `execute_plan`.

El adapter nativo Zigbee2MQTT mantiene el mismo contrato `AdapterPort` y no
añade herramientas MCP. Su contrato de topics, mapeo de capacidades,
conversión de brillo, disponibilidad e idempotencia está cubierto por los
tests de contrato.

El adapter nativo Matter Server mantiene el mismo contrato y consume snapshots
de nodos/endpoints y eventos por WebSocket. Su perfil de schema 13, mapeo de
clusters, comandos acotados, compatibilidad y límites de seguridad está
documentado en los schemas y tests de contrato del adapter.

El adapter nativo KNX mantiene `AdapterPort` y usa un mapping local estricto de
entidades, direcciones de grupo y DPTs. Su contrato v1 cubre `1.001` para power,
`5.001` para brightness, `9.001` para temperature, `9.007` para humidity y
`1.018` para occupancy. El transporte live basado en xknx queda aislado detrás
de `KnxTransport`; los tests usan `InMemoryKnxTransport`.

KNX no añade tools MCP ni permite que el agente envíe group-value reads/writes
arbitrarios. Los telegramas de direcciones no configuradas son diagnósticos, y
las mutaciones pasan por `PlanService`, `PolicyEngine`, `PlanExecutor`,
idempotencia y postcondición de readback. La especificación y el formato de
mapping están cubiertos por los schemas y tests de contrato del adapter.

El adapter nativo Modbus mantiene `AdapterPort` y usa un mapping local estricto
de unit IDs, áreas, offsets PDU y codecs. Su perfil v1 cubre coils/discrete
inputs para booleanos, holding/input registers para números, y las capacidades
semánticas de power, brightness, temperature, humidity y occupancy. El polling
se realiza dentro de `subscribe_events()` y solo emite cambios o diagnósticos;
el transporte live basado en PyModbus queda aislado detrás de
`ModbusTransport`, mientras los tests usan `InMemoryModbusTransport`.

Modbus no añade tools MCP ni permite al agente solicitar registros o function
codes arbitrarios. Las escrituras solo pueden llegar a coils de power o
holding registers de brightness declarados, después de pasar por
`PlanService`, `PolicyEngine`, `PlanExecutor`, idempotencia y postcondición de
readback. La especificación, el mapping y el contrato están cubiertos por los
schemas y tests de contrato del adapter.

## Versionado

- Añadir campos opcionales o nuevos valores documentados puede entrar en una
  versión menor compatible, junto con pruebas y schemas regenerados.
- Cambiar el significado, tipo, unidad, obligatoriedad o semántica de un
  campo requiere una nueva versión mayor (`v2`) y una estrategia de migración.
- No se deben sobrescribir schemas históricos: cada versión vive en su propio
  directorio (`schemas/v1`, `schemas/v2`, ...).
- Toda modificación de contrato debe actualizar `spec.md`, `plan.md`,
  `tasks.md`, pruebas de contrato y esta documentación.

## Verificación

La validación local completa está descrita en el README y en los tests del
repositorio.
Como mínimo, antes de publicar un cambio de contrato se ejecutan:

```bash
uv run pytest -q tests/contract
uv run python scripts/export_schemas.py
uv run ruff check .
uv run mypy src tests scripts
```
