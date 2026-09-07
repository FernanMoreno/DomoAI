# Cobertura del adaptador universal y sus conectores

Este inventario describe la cobertura actual del adaptador universal semántico
de DomoAI. La superficie del producto tiene un único MCP y un único contrato
de dispositivo; las filas de protocolos son conectores internos de transporte,
no adapters públicos ni tools específicas para fabricantes. No es una lista de
dispositivos certificados ni concede autoridad de ejecución.
La disponibilidad de una ruta, el permiso del cliente, la aprobación humana y
la qualification física son gates distintos.

## Categorías de evidencia

| Categoría | Significado |
|---|---|
| `native` | El runtime mantiene un conector interno que traduce el protocolo al contrato del adaptador universal. |
| `via_home_assistant` | DomoAI usa la integración de Home Assistant como frontera del proveedor; el fabricante subyacente no es un adapter directo de DomoAI. |
| `plugin` / `internal_extension` | El SDK permite extender conectores internos conformes; no crea una superficie MCP ni una autoridad paralela. |
| `fixture_or_simulation` | La ruta está cubierta por fixture, gemelo o laboratorio reproducible; no es hardware certificado. |
| `unavailable` | No existe actualmente un conector o una configuración completa para esa ruta. |
| `physically_qualified` | Hay evidencia atendida y vigente para un perfil de hardware, firmware y rutas concretas. Esta categoría no se asigna por tests locales. |

## Integraciones mantenidas

| Conector interno o frontera | Protocolo | Cobertura | Discovery/estado | Escritura semántica | Evidencia y límites |
|---|---|---|---|---|---|
| `home_assistant` | Home Assistant REST/WebSocket | `via_home_assistant` | Entidades, registro de dispositivos y eventos normalizados. | Sólo rutas y capabilities explícitamente soportadas por el provider. | Home Assistant puede integrar Hue, Shelly, Tuya u otros fabricantes, pero DomoAI no los considera adapters directos ni físicamente cualificados por ello. |
| `matter` | Matter Server/WebSocket | `native` | Descriptor, capabilities y estado Matter normalizados. | Sólo comandos traducibles y validados por el adapter. | Hay fixture/laboratorio; commissioning con dispositivo físico es una gate externa. |
| `zigbee2mqtt` | MQTT con convención Zigbee2MQTT | `native` | Discovery, exposes, estado y eventos Zigbee2MQTT. | Publicación semántica con idempotencia y readback cuando el stream lo permite. | No es MQTT genérico para ESP o topics arbitrarios. Hardware/radio real requiere qualification independiente. |
| `mqtt` | MQTT declarativo | `native` | Devices y estados de los topics declarados en mapping v1. | Sólo capabilities con command topic declarado; la ruta sigue el plan/runtime. | Mapping estricto y fixture local; broker, firmware y hardware requieren qualification independiente. |
| `knx` | KNX/IP | `native` | Mapping de group addresses y eventos KNX. | Sólo bindings declarados y tipos DPT soportados. | El laboratorio KNX Virtual no certifica bus, cableado o dispositivos físicos. |
| `modbus` | Modbus TCP con mapping | `native` | Puntos y estados declarados por mapping. | Sólo registros/points definidos, tipados y validados. | Fixtures y laboratorio no sustituyen perfil, cableado, unidad o equipo físico real. |
| `fixture` | Simulación determinista | `fixture_or_simulation` | Inventario y estados reproducibles para desarrollo y contratos. | Ejecución simulada con readback determinista. | Nunca se publica como provider concreto del gateway configurado. |
| Universal Connector SDK | Paquete `AdapterPort` conforme | `plugin` / `internal_extension` | Una extensión puede declarar discovery y capabilities mediante manifest. | Sólo tras conformance, routing, policy y runtime composition. | No hay un adapter público de fabricante; un manifest no concede autoridad ni qualification. |
| MQTT dinámico no declarado | MQTT genérico | `unavailable` | No se descubren topics arbitrarios sin mapping. | No existe. | La ruta generic MQTT requiere mapping v1; no acepta autodiscovery ni payload scripting. |
| Hue, Shelly, Tuya, ESPHome e inversores directos | APIs o protocolos de fabricante | `unavailable` como conector nativo | Pueden aparecer indirectamente si Home Assistant los integra. | No existe una superficie ni un adapter público por fabricante. | No forman parte del objetivo: se conectan mediante Home Assistant o mediante futuros conectores internos bajo el mismo contrato universal. |

## Ciclo seguro del adaptador universal

Los agentes MCP operan capacidades semánticas, no endpoints de fabricante. La
lectura usa resources, `discover_devices`, `get_state` y `get_history`. Un
cambio físico no usa `set_state` ni `execute_command` directo:

```text
preview_command / preview_plan
        ↓
prepare_command / prepare_plan
        ↓
policy + aprobación cuando aplique + admission
        ↓
execute_plan o schedule_plan
        ↓
conector interno → adaptador universal → readback → StateStore + audit
```

El optimizador produce propuestas de `Plan`; no llama adapters. Las Skills y
los prompts MCP pueden explicar o encadenar este flujo, pero no cambian sus
gates. Una ruta visible en este inventario puede seguir bloqueada por
disponibilidad, identidad ambigua, estado stale, policy, consentimiento o
evidencia física insuficiente.

## Regla de qualification

La categoría `physically_qualified` sólo puede publicarse para un perfil
identificado de conector, dispositivo, firmware, mapping y evidencia vigente.
Una fixture, una prueba de contrato, un broker de laboratorio o una integración
de Home Assistant no elevan por sí mismos ninguna ruta a esa categoría.

## Matriz semántica publicada

`domotics://coverage` mantiene una fila por dispositivo y capability. Cada fila
incluye `operations.read`, `operations.write`, los comandos que el mapper
reconoce, límites/unidad, garantías de confirmación y los IDs de las fuentes
activas. El estado `unavailable` significa que el registro semántico no tiene
una ruta de fuente activa; no se interpreta como una escritura potencial.

| Operación | Evidencia mínima | Resultado público si falta |
|---|---|---|
| Lectura | Capability legible y fuente disponible | `read: false` o `status: unavailable`; no se fabrica estado |
| Escritura | Capability escribible, comando reconocido y ruta disponible | `write: false`, comando ausente o validación rechazada antes del adapter |
| Confirmación | Feedback legible, garantía y tolerancia declaradas | Resultado `unknown` cuando el feedback no confirma |
| Unidad/rango | Unidad, límites o enum del capability | Unidad incompatible o valor fuera de rango rechazado |
| Fuente/protocolo | Mapper y route conformes | Diagnóstico `unsupported`/`unavailable`; no se crea tool por fabricante |

La matriz describe cobertura del modelo semántico. Policy, admission,
commissioning y qualification física siguen siendo gates independientes.
