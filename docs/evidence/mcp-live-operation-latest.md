# Evidencia de MCP y operación live del laboratorio

**Fecha:** 2026-09-04
**Clasificación:** `process_lab`
**Endpoint:** `http://127.0.0.1:8124/mcp`
**Alcance:** laboratorio local; actuadores virtuales únicamente.

## Resultado ejecutivo

El gateway MCP Streamable HTTP está montado y fue usado desde un cliente MCP
real. La conexión expuso 32 tools y 8 resources. El inventario descubierto
contuvo 25 dispositivos y los cinco adapters configurados quedaron
conectados: Home Assistant, Zigbee2MQTT, Matter, KNX y Modbus. El proceso del
gateway permanece levantado en `127.0.0.1:8124` para nuevas operaciones del
laboratorio.

La prueba de disponibilidad sostenida duró 90 segundos. En todas las
muestras los cinco adapters permanecieron conectados y la frescura fue
`current`. `/healthz` respondió `200`; `/readyz` solo mantuvo el bloqueo
esperado `physical_actuator_not_qualified`, porque la batería está
`software-qualified` y no existe evidencia HIL física.

## Operaciones reales a través del MCP

Cada mutación siguió `prepare_plan` → `execute_plan` con postcondición de
`power`, lectura de confirmación y consulta inmediata `get_state`. En una
ejecución limpia del gateway se probaron las seis rutas operativas:

| Adapter | Dispositivo | Secuencia | Resultado |
|---|---|---|---|
| KNX | `living_room.main-light` | `on → off` | 2 × `confirmed_success`, readback actual |
| Modbus | `living_room.main-light-2` | `on → off` | 2 × `confirmed_success`, readback actual |
| Zigbee2MQTT | `unassigned.living-room-main-light` | `on → off` | 2 × `confirmed_success`, readback actual |
| Matter | `unassigned.node-matter-onoff-light` | `on → off` | 2 × `confirmed_success`, readback actual |
| Zigbee2MQTT | `unassigned.garden-pump` | `on → off` | 2 × `confirmed_success`, readback actual |
| Home Assistant | `unassigned.virtual-living-room-switch` | `on → off` | 2 × `confirmed_success`, readback actual |

El contador live del runtime quedó en 12 comandos confirmados, 0 fallidos,
0 rechazados, 0 indisponibles, 0 desconocidos y 0 mismatches de readback.
El estado final MCP de los seis actuadores probados fue `power=false`,
`status=current` y sin diagnósticos. Como comprobación fuera del MCP, la
bobina Modbus quedó `false` y MQTT retained publicó `OFF` para la bomba y la
luz Zigbee (con brillo `127` conservado en la luz).

El gateway devolvió `/healthz=200`. `/readyz=503` únicamente por el bloqueo
intencionado `physical_actuator_not_qualified`; no es un fallo de los cinco
adapters de laboratorio, que quedaron conectados y frescos.

No se encendieron ni se escribieron batería, cargador EV, térmico u otros
actuadores críticos. Esos dispositivos permanecen sometidos a la política de
qualification y a sus gates de seguridad.

## Incidencia encontrada y corregida

Durante la primera ejecución se observó que una reconexión del adapter HA
marcaba sus snapshots como `unavailable`, pero el diagnóstico de recuperación
no llevaba un código tipado y el consumidor no rehidrataba el estado. El REST
de HA y su WebSocket fueron verificados de forma independiente como
funcionales; el fallo estaba en la frontera `CompositeAdapter` →
`RuntimeEventConsumer`.

La corrección:

- emite `source_reconnected` tras una reconexión real del child adapter;
- hace una lectura de recuperación limitada al adapter señalado;
- reactiva las rutas solo si esa lectura devuelve evidencia válida;
- conserva el estado fail-closed y audita `source_reconnect_failed` si la
  recuperación no puede confirmarse.

En la validación final también se corrigieron y verificaron tres fronteras
adicionales:

- HA ya no trata un timeout de inactividad del WebSocket como desconexión;
- Zigbee2MQTT espera un evento de readback posterior al comando y el entorno
  Docker se reconstruyó para incluir `/get`;
- `StateStore` serializa mutaciones y descarta observaciones cursorless más
  antiguas, sin ocultar transiciones nuevas a `stale`/`unavailable`; una
  cancelación solo se propaga después de instalar un candidato cuya escritura
  durable ya terminó.

El cierre controlado del gateway no deja traceback y una cancelación durante
todo el arranque libera el ownership parcial del runtime. La metadata de
revisión/fingerprint también hace rollback si falla su persistencia, y una
invocación concurrente que no obtiene el claim no puede marcar `UNKNOWN` el
plan del ganador. Zigbee2MQTT serializa refreshes y rechaza una segunda orden
live mientras haya un readback pendiente.

Regresiones añadidas y verificadas:

```text
uv run pytest -q                                      1788 passed, 18 skipped
project-composition-check domoai                     503 passed, 18 skipped
uv run ruff check src tests                            All checks passed
uv run mypy src                                       162 files, no issues
check_runtime_contract_docs.py                        coherent
export_schemas.py                                     73 schemas exported
git diff --check                                      PASS
```

## Relación con las gates del proyecto

- El gemelo digital determinista pasó los seis perfiles (`fixture`, Home
  Assistant, KNX, Modbus, Matter y Zigbee2MQTT), 10 dominios y 14 checks:
  [`digital-twin-latest.md`](digital-twin-latest.md).
- La cualificación de proceso Docker/KNX Virtual/Home Assistant pasó smoke,
  composición live y las pruebas etiquetadas HIL de proceso:
  [`process-lab-latest.md`](process-lab-latest.md).
- La inspección MCP de commissioning no encontró candidatos físicos ni creó
  autoridad.

## Veredicto

El circuito software → runtime → MCP → adapters virtuales → readback →
persistencia y recuperación está operativo y probado contra el laboratorio.
Esto no equivale a un proyecto completo para producción física: siguen fuera
de esta evidencia el commissioning de hardware real, HIL de batería/EV,
firmware, radio Zigbee, cableado/instalación, autenticación de despliegue
productivo y observabilidad remota/distribuida.
