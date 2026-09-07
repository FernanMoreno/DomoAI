# Auditoría Fase 1 — Robustez operativa

**Objetivo:** que el runtime permanezca acotado, observable y recuperable ante fallos de adapters, sockets, brokers, scheduler, SQLite, procesos y redes.
**Dependencia:** Fase 0 completada o con gates explícitas de contención.

## 1. A-011 — Provider SDK y métodos síncronos

### Evidencia

El registro solo comprueba que los métodos sean `callable` en [`provider_sdk.py`](../src/domoai/runtime/provider_sdk.py:188). Después, `_call_async` siempre hace `await callable_method(...)`.

Un provider válido desde el punto de vista del registro puede devolver una lista normal en `discover` y fallar al intentar esperarla.

### Riesgo

- fallo tardío y poco claro durante discovery;
- provider aparentemente registrado pero no operativo;
- comportamiento diferente entre fixtures y providers reales;
- dificultad para distinguir error de contrato de error de infraestructura.

### Recomendación

Elegir una política única:

- rechazar en el registro todo método que no sea coroutine function; o
- permitir métodos sync y ejecutarlos con `asyncio.to_thread`, documentando límites de cancelación y thread safety.

La primera opción es más segura para providers de I/O.

### Pruebas

- provider sync rechazado con diagnóstico claro;
- provider async aceptado;
- excepción del provider convertida en diagnóstico estable;
- cancelación no deja tareas huérfanas.

## 2. A-012 — Home Assistant: deadlines y lifecycle

### Evidencia

El cliente WebSocket de Home Assistant abre con `open_timeout`, pero el bucle de `recv()` no tiene deadline de operación en [`client.py`](../src/domoai/adapters/home_assistant/client.py:104). Además, cada operación REST crea un `httpx.AsyncClient` independiente.

### Riesgo

- discovery bloqueado indefinidamente por socket silencioso;
- fuga o churn de conexiones;
- latencia alta con refresh frecuente;
- dificultad para cerrar limpiamente el runtime.

### Recomendación

- un `AsyncClient` propiedad del lifecycle del adapter;
- límites de conexiones y keep-alive configurados;
- `asyncio.timeout` para auth, command, receive y subscription;
- watchdog de WebSocket y reconexión acotada;
- métricas de retries, reconnects y timeouts;
- cierre probado en `Runtime.close`.

### Pruebas

Simular auth bloqueada, `recv()` silencioso, reconnect continuo y cierre durante una operación. El adapter debe terminar en tiempo acotado y publicar salud degradada.

## 3. A-015 — Resultados parciales del CompositeAdapter

### Evidencia

La lectura compuesta agrupaba los snapshots por `(adapter_id, external_id)`.
Cuando un adapter devolvía varias capacidades para la misma referencia física,
el último snapshot sobrescribía los anteriores. KNX devolvía `power` y
`brightness`, pero el composite retenía solo `brightness`; el executor no podía
confirmar el postcondition de `power`.

El caso se reprodujo con la composición live y se trazó hasta
[`composite_adapter.py`](../src/domoai/runtime/composite_adapter.py:245), no
hasta el transporte KNX.

### Riesgo

Una lectura compuesta puede aparentar éxito aunque falte precisamente el dispositivo importante. La capa superior no sabe si el dato es actual, heredado o fallido.

### Corrección aplicada

`EntityReadResult` mantiene el snapshot primario compatible y añade
`snapshots: tuple[StateSnapshot, ...]`. La clave interna incluye también
`capability`, `CompositeAdapter.read_state()` aplana todas las capacidades y
`DiscoveryService.refresh_state()` persiste el conjunto completo. La referencia
continúa teniendo un único estado de error cuando el adapter no devuelve
ningún snapshot:

```text
EntityReadResult {
  ref,
  snapshot | null,
  snapshots: tuple[StateSnapshot, ...],
  status: success | stale | unavailable | invalid,
  error_code,
  source_revision
}
```

Una refresh fallida sigue marcando la referencia concreta como degradada, no
deja la ausencia implícita hasta el siguiente timer de stale. El test de
regresión verifica dos capacidades devueltas para una única referencia y la
composición KNX confirma el readback real de `power`.

## 4. A-016 — Normalización estricta de disponibilidad

### Evidencia

[`registry.py`](../src/domoai/runtime/registry.py:294) usa `bool(entity.get("available", True))`. En Python, `bool("false")` es `True`.

### Recomendación

Aceptar únicamente `bool` real. Cualquier string, número o null debe producir diagnóstico de descriptor inválido. La conformance del SDK no debe ser la única defensa: el registry también es una frontera de confianza.

## 5. A-017 — Entrega durable de auditoría

### Evidencia

El sistema conserva contadores y una ventana acotada en [`events.py`](../src/domoai/runtime/events.py:98), pero la ruta asíncrona de `SerializedRepositoryProxy` puede aceptar el trabajo sin entregar un future de confirmación.

### Decisión de diseño necesaria

No todos los eventos tienen que bloquear una operación física. Sí deben tener garantía diferente los eventos de:

- autorización emitida o consumida;
- ejecución física iniciada, completada o desconocida;
- emergency stop;
- recuperación de bundle;
- cambio de policy.

Para esos eventos debe existir outbox durable, secuencia y reintento. Los eventos informativos pueden seguir siendo best-effort.

## 6. A-019 — Recuperación continua

### Evidencia

La recuperación de bundles se realiza principalmente al iniciar el runtime. En el scheduler, la ejecución del plan y el registro del outcome son pasos separados en [`scheduler.py`](../src/domoai/application/scheduler.py:494).

Un fallo entre ambos puede dejar una fila ejecutada y un miembro de bundle en estado pendiente hasta el siguiente arranque.

### Recomendación

Añadir reconciliación periódica e idempotente:

```text
durable intent -> physical evidence -> outcome -> projection
```

La reconciliación debe inspeccionar leases expirados, outcomes sin proyección, schedules ejecutados sin miembro completado y comandos con resultado desconocido. Nunca debe volver a ejecutar físicamente sin una prueba explícita de que la operación no ocurrió.

## 7. Observabilidad que falta

El runtime tiene métricas y diagnósticos básicos, pero para producción necesita como mínimo:

- `command_latency_ms` por adapter y capability;
- `readback_mismatch_total`;
- snapshots stale/invalid/unavailable por fuente;
- gaps, duplicates y replay de eventos;
- leases activos, expirados y fallidos al liberar;
- approval reserve/consume/release;
- bundles partial/unknown/recovered;
- colas, backpressure, reconnects y timeouts;
- correlación `request_id → plan_id → bundle_id → command_id`.

El cierre actual usa un acumulador process-local y no introduce
Prometheus/OpenTelemetry; una exportación futura deberá seguir siendo
diagnóstica y no una dependencia para ejecutar de forma segura.

## 8. Pruebas de resiliencia y qualification

- Testcontainers para el broker Mosquitto: cerrado con `LogMessageWaitStrategy`,
  timeout de startup de 30 segundos y `1 passed` bajo
  `-W error::DeprecationWarning`.
- Los escenarios deterministas de timeout, conexión perdida, mensajes
  duplicados, restart, límites de cola, cancelación, storage y event consumer
  pasan en la matriz focalizada de la sección 12.
- La qualification live del laboratorio Docker pasa para MQTT/Zigbee2MQTT,
  Modbus, Matter y Home Assistant Provider, incluidos discovery y el
  round-trip virtual de Home Assistant.
- HIL con hardware real sigue siendo una gate externa antes de habilitar
  producción; el test de batería física permanece opt-in y correctamente
  bloqueado sin confirmación y parámetros del operador.

## 9. Secuencia de implementación recomendada

1. Normalizar lifecycle y deadlines de todos los adapters.
2. Definir resultados parciales por referencia.
3. Endurecer el registry y el Provider SDK.
4. Añadir outbox para eventos de autoridad.
5. Añadir reconciliación periódica de bundles y schedules.
6. Instrumentar métricas y correlación de traces.
7. Ejecutar pruebas de timeout, cancelación, restart y hardware virtual.

## 10. Criterio de salida de la fase

La fase termina cuando una caída o timeout siempre produce un estado observable, durable y recuperable; ningún socket puede bloquear indefinidamente; los errores parciales se asignan a entidades concretas; y la auditoría de autoridad tiene entrega durable o un estado de pérdida explícito.

## 11. Cierre de observabilidad operativa

**Fecha de cierre técnico:** 2026-09-04

El diseño aprobado quedó implementado en [`operational_metrics.py`](../src/domoai/runtime/operational_metrics.py:55) y conectado al runtime mediante [`runtime_factory.py`](../src/domoai/application/runtime_factory.py:698). `RuntimeOperationalMetrics` es process-local, thread-safe, no autoritativo y limita las series de comandos a 256 combinaciones de `adapter_id` y capability. Las etiquetas no seguras, outcomes desconocidos y latencias no finitas se descartan con `telemetry_failure_total`; el overflow permanece visible sin crear cardinalidad ilimitada.

### Señales cerradas

| Señal | Implementación | Evidencia |
|---|---|---|
| Latencia y outcome de command | [`PlanExecutor`](../src/domoai/application/executor.py:430) mide cada dispatch y conserva la correlación `adapter_request_id`. | `test_adapter_dispatch_latency_and_outcome_are_projected` |
| Mismatch de readback | Solo el readback `CURRENT` que no cumple el postcondition suma el contador. | `test_only_current_contradictory_readback_counts_as_mismatch` |
| Calidad por fuente | El colector proyecta `current/stale/unavailable/invalid` por `source_ref.adapter_id` junto a `stale_state_count`. | `test_snapshot_has_every_key_with_defaults_on_a_fresh_runtime` |
| Cursor gap/replay/resync | [`StateStore.save_many`](../src/domoai/runtime/state_store.py:273) cuenta cada decisión sin aceptar regresión. | `test_cursor_decisions_are_projected_to_operational_metrics` |
| Leases | Acquire, expiry, release confirmado/fallido y autoridad desconocida se registran en [`executor.py`](../src/domoai/application/executor.py:251). | `test_lease_is_released_and_plan_becomes_unknown_after_execution_exception`, `test_unconfirmed_release_marks_successful_physical_attempt_unknown` |
| Approvals | Reserve, consume, release y rechazo se registran en [`approval_store.py`](../src/domoai/runtime/approval_store.py:397). | `test_approval_transitions_are_projected_to_operational_metrics` |
| Bundles | Completed, partial, unknown y recovered se proyectan en [`bundle_commit.py`](../src/domoai/application/bundle_commit.py:608). | `test_commit_records_later_failure_as_partial_and_is_idempotent`, `test_recovery_marks_in_progress_bundle_unknown_without_replay` |
| Colas/reconnect/storage/liveness | Se conservan las métricas existentes del `CompositeAdapter`, event consumer, scheduler, storage y audit; no se duplican. | `test_runtime_metrics_correlate_storage_state_event_and_scheduler_health` |
| Correlación | `ExecutionContext` y eventos auditados mantienen request/plan/attempt/command/adapter request; el bloque operativo no copia secretos ni sustituye el audit trail. | `test_adapter_receives_the_outcome_correlation_context`, `test_operational_metrics_contract_is_stable_and_json_safe` |

La proyección MCP conserva el contrato anterior y añade `operational` en
[`RuntimeMetricsCollector`](../src/domoai/application/metrics.py:171), expuesto
por [`domotics://metrics`](../src/domoai/mcp/domotics_server.py:1110). La lectura
no dispara comandos, no escribe persistencia y el acumulador no puede cambiar
admission, policy, safety, leases ni readback.

### 11.1 Exportación remota autenticada

El pendiente de exportación pull quedó implementado de forma opt-in en
[Spec 188](../specs/188-remote-operational-metrics/spec.md). El endpoint
`GET /metrics` reutiliza el verificador bearer hash-only del gateway, devuelve
solo familias Prometheus allowlisted y limita la exposición a 262144 bytes por
defecto. La ruta nunca ejecuta comandos, cambia autoridad ni escribe
auditoría; los fallos de snapshot/render devuelven 503 genérico. Caddy reenvía
`/metrics` junto a MCP/readiness y mantiene el fallback 404.

La cobertura está en `tests/unit/mcp/test_remote_metrics.py`,
`tests/contract/test_remote_metrics_contract.py` y
`tests/integration/test_remote_metrics_live.py`. Histórico remoto, OTLP push,
agregación multi-instancia y fencing siguen fuera de la fase.

## 12. Verificación ejecutable

Resultados consolidados del cierre operativo ejecutado el 2026-09-04:

```text
tests/composition/test_zigbee2mqtt_broker_composition.py -W error::DeprecationWarning
                                                          1 passed
qualification live MQTT/Modbus/Matter/HA Provider          6 passed, 1 skipped
qualification KNX gateway + read-only smoke                2 passed
composition multi-adapter live (KNX + 4 adapters)           1 passed
tests focalizadas de resiliencia/lifecycle/storage/events 35 passed
TMPDIR=/dev/shm uv run pytest -q                         1654 passed, 18 skipped
uv run ruff check .                                      All checks passed
uv run mypy src                                          Success: no issues found in 143 source files
uv run python scripts/check_architecture_contracts.py    architecture contracts kept
uv run python scripts/check_runtime_contract_docs.py     runtime contract documentation is coherent
uv run lint-imports                                      4 contracts kept, 0 broken
project-composition-check "$(cat .ai/project-name)"     474 passed, 18 skipped
graphify . --update --no-viz --code-only                 6671 nodes, 21703 edges, 352 communities
```

La sesión KNX se recuperó sin alterar el contrato fail-closed: Windows tenía
`KV.exe` escuchando en UDP/3671, pero `dev/lab/.env` conservaba la dirección
antigua `172.26.80.1`. En la topología WSL mirrored actual, el upstream real
responde en `127.0.0.1:3671`; se arrancó el `knxd` nativo con ese host y se
publicó el endpoint DomoAI en `127.0.0.1:3672`. El gateway de descubrimiento y
el smoke read-only pasaron, y la composición multi-adapter completa también
pasó tras corregir la pérdida de capacidades de A-015. No se relajó el gate de
salud ni se inició el perfil Docker experimental. Los defaults del launcher WSL,
del bridge de batería y del smoke live quedaron alineados con ese endpoint
local, manteniendo la posibilidad de sobrescribirlos en WSL NAT.

La suite ya no emite la deprecación de `wait_for_logs` en el escenario real de
Mosquitto/Testcontainers. La limitación de Graphify para SQL
(`tree_sitter_sql` no instalado) afecta solo a 11 migraciones y no al análisis
AST Python.

## 13. Revisión de composición

**Resultado:** PASS WITH RISKS.

- `RuntimeOperationalMetrics` solo depende de la biblioteca estándar y queda
  en `runtime`; aplicación, MCP y composición dependen hacia abajo de él.
- `build_runtime` crea una instancia única y la entrega a StateStore,
  ApprovalStore, PlanExecutor, BundleCommit/Recovery y el colector; los
  constructores legacy conservan dependencia opcional.
- Los productores registran después de cruzar sus fronteras reales; un fallo
  de métricas no se propaga a ejecución, persistencia, recovery ni cierre.
- El snapshot está desacoplado de authority y contiene valores JSON finitos;
  las fuentes durables siguen siendo StateStore, repositories y AuditLog.
- Los tests unitarios, contrato, composición y `project-composition-check`
  cubren el flujo runtime → componentes → MCP.

La envoltura auxiliar `project-composition-review domoai codex` tenía un
defecto en el launcher global: `--sandbox` y `--ask-for-approval` se pasaban
dos veces. Se corrigió `/home/felni/.project-launcher.sh` en las rutas de
revisión y configuración, y `bash -n` confirmó su sintaxis. Un smoke test del
wrapper instalado confirmó que Codex recibe exactamente una pareja de ambas
opciones y que la invocación termina sin el error de argumentos duplicados.
La revisión de composición de esta fase queda respaldada por la inspección
manual, los cuatro contratos de Import Linter, los tests de composición y los
504 tests del check del proyecto.

Riesgos residuales aceptados: los contadores reinician con el proceso y no son
historial durable; la qualification de hardware físico sigue requiriendo una
gate HIL atendida; y la coordinación distribuida, los pools externos y la
agregación remota de métricas quedan fuera del alcance local. Si la sesión
externa KNX Virtual/ETS/knxd no está iniciada, la suite live debe volver a
quedar bloqueada o saltarse por su gate explícita, nunca marcarse como verde.
La limitación de Graphify para SQL permanece como advertencia de herramienta
(`tree_sitter_sql` no instalado), no como fallo del runtime.

## 14. Criterio de salida

Los pendientes automatizables de la Fase 1 quedan cerrados técnicamente: el
snapshot expone latencias, outcomes, mismatches, calidad por fuente,
cursores, leases, approvals y bundles; los contadores se producen en sus
fronteras reales; la readiness real del broker no usa APIs deprecadas; A-015
conserva todas las capacidades por referencia; la qualification KNX y la
composición multi-adapter disponibles pasan; y todos los gates de código,
contrato, composición y documentación ejecutables pasan. Permanecen como
gates externas HIL físico, disponibilidad atendida de KNX Virtual/ETS/knxd,
coordinación distribuida, histórico y agregación remota de métricas. El runtime
continúa fail-closed y no se habilita autonomía física no cualificada.

La evaluación de coordinación distribuida, pools y exportación remota queda
registrada en [el documento de evolución posterior](evaluacion-coordinacion-distribuida-pools-metricas.md).

La última regresión transversal posterior a Spec 188, ejecutada el 2026-09-05,
queda registrada en
[`evidence/production-readiness-latest.md`](evidence/production-readiness-latest.md):
`1799 passed, 18 skipped`, Ruff limpio, mypy sin errores, preflight estático
PASS y Compose resolviendo la configuración local sin iniciar contenedores.

## 15. Addendum — robustez productiva multi-adapter y multi-host — 2026-09-05

### 15.1 Estado comprobado

La robustez software está cubierta por el runtime event consumer, las colas
bounded de `CompositeAdapter`, los workers limitados, el readback y las
métricas operativas. El gemelo digital y el laboratorio de proceso ejercitan
fallos de disponibilidad, stale, duplicado, orden, timeout, reconexión y
fallo parcial sin necesitar servicios externos.

La robustez pendiente para producción no puede demostrarse solo con esas
fixtures. El documento de readiness registra que `/metrics` es pull protegido
por instancia y que el runtime sigue siendo single-writer.

### 15.2 Brechas específicas

| Superficie | Implementado | Pendiente para cierre productivo |
|---|---|---|
| Adapters reales | Contratos y proyecciones para HA, Matter, Zigbee2MQTT, KNX, Modbus y fixture. | Ejecutar cada perfil contra su servicio/bus/dispositivo real y conservar logs de command/readback. |
| KNX | Gate de laboratorio y configuración fail-closed. | Sesión atendida KNX Virtual/ETS/knxd y posterior evidencia de bus físico si se declara hardware qualified. |
| HIL | Runner y qualification model. | Batería, EV, HVAC y actuadores de riesgo reales; el skip no es un PASS. |
| Colas y workers | Bounded y aislados dentro del proceso. | Benchmark sostenido multi-host, cola durable, afinidad por hogar y recuperación de trabajos. |
| Ownership | Single-writer local. | Lease externo, fencing, takeover y pruebas de partición. |
| Observabilidad | `domotics://metrics` y `GET /metrics` por instancia. | Histórico, agregación, `instance_id`, alertas y métricas de fencing/replicas. |
| Dependencias | SQLite real y algunas pruebas live opt-in. | Broker, buses y servicios desplegados de forma reproducible; eliminar skips justificados por infraestructura. |

### 15.3 Pruebas de aceptación adicionales

1. Ejecutar un soak test de varias horas por adapter con desconexiones y
   reconexiones controladas.
2. Ejecutar una carga representativa de discovery, refresh, auditoría,
   scheduler, ejecución y CP-SAT registrando p50/p95/p99, overload, memoria,
   CPU y tiempo de cierre.
3. Simular dos hosts, una partición y un takeover; verificar cero doble
   escritura y resultados idempotentes.
4. Reiniciar cada dependencia durante una orden y comprobar `UNKNOWN`,
   recuperación y ausencia de confirmaciones ficticias.
5. Validar que las métricas no bloquean ni alteran la operación cuando el
   collector está caído, reiniciándose o saturado.
6. Ejecutar `deployment preflight --network` con TLS, DNS, firewall, HA,
   broker y KNX disponibles.

### 15.4 Pendientes trazables

La tarea abierta de dependencias reales está en [Spec 140](../specs/140-real-composition-tests/tasks.md);
la HIL de batería está en [Spec 133](../specs/133-battery-hil-certification/tasks.md).
La exportación Prometheus opt-in de Spec 188 está cerrada para una instancia,
pero histórico, agregación multi-réplica, pools y fencing siguen siendo una
evolución de Fase 3, no un resultado de esta fase.

## 16. Verificación ejecutada — 2026-09-05

La ejecución de cierre de esta fase produjo evidencia fresca:

```text
broker Mosquitto con warnings-as-errors             1 passed
qualification live configurada                     6 passed, 1 skipped
resiliencia/lifecycle focalizada                   38 passed
suite completa                                     1799 passed, 18 skipped
project-composition-check                          504 passed, 18 skipped
Ruff, mypy, arquitectura, docs e Import Linter     PASS
Graphify incremental                               7509 nodos, 23187 aristas, 454 comunidades
```

El skip corresponde a una gate HIL externa; no se convirtió en PASS. Graphify
mantiene la advertencia conocida de `tree_sitter_sql` ausente para migraciones
SQL, sin impacto en la validación Python ni en los contratos ejecutables.
