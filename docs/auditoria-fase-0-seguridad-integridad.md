# Auditoría Fase 0 — Seguridad e integridad

**Objetivo:** garantizar que ninguna decisión agentic pueda basarse en estado regresivo, autoridad retenida, aprobación incompleta o datos físicamente imposibles.

**Prioridad:** obligatoria antes de autonomía sobre hardware.
**Estado:** Fase 0 técnica completa para A-001 — A-020. El cierre cubre los
contratos del runtime, persistencia, autoridad, MCP e identidad descritos en
este documento. No habilita por sí sola autonomía sobre hardware real: la
qualification de providers, el hardware físico y la coordinación distribuida
siguen siendo gates posteriores y permanecen `fail-closed`.

## 1. Alcance

Esta fase cubre:

- orden de eventos y verdad temporal del estado;
- valores y estados válidos;
- persistencia y recuperación;
- leases, takeover y ejecución segura;
- evidencia de bundles y precondiciones;
- aprobación, commit y compensación;
- idempotencia de acciones y automatizaciones.

## 2. A-001 — Eventos fuera de orden

### Evidencia

[`StateStore.save`](../src/domoai/runtime/state_store.py:120) escribe `_source_snapshots[source_key]` sin comprobar una secuencia, cursor o versión monotónica. El consumidor de eventos entrega snapshots embebidos directamente al discovery en [`event_consumer.py`](../src/domoai/application/event_consumer.py:187).

La consecuencia reproducida es:

```text
snapshot nuevo: 22.0
snapshot antiguo: 18.0
estado final: 18.0
```

El simple `observed_at` no basta: un broker puede entregar tarde una medición antigua, y una reconexión puede repetir mensajes ya aplicados.

### Riesgo

- precondiciones evaluadas contra un valor anterior;
- optimización basada en SOC, potencia o temperatura incorrectos;
- re-ejecución de eventos durante reconnect;
- aumento falso de `state_version`;
- divergencia entre el estado físico y el canónico.

### Corrección requerida

Introducir una identidad de orden por fuente:

```text
source_id + stream_id + epoch + sequence
```

El Store debe persistir el último cursor aceptado y rechazar secuencias anteriores o duplicadas. Ante un salto de secuencia debe marcar el stream como degradado y solicitar resync. Si un provider no tiene secuencia, debe definir una política explícita de confianza y no presentarlo como equivalente a una fuente ordenada.

### Pruebas de aceptación

- antiguo después de nuevo no cambia la vista canónica;
- duplicado es idempotente;
- replay después de restart no regresa el estado;
- cambio de epoch exige resync;
- gaps generan diagnóstico y no ejecución autónoma;
- dos fuentes en conflicto conservan `INVALID` según el contrato actual.

## 3. A-002 — Liberación de lease ante excepciones

### Evidencia

El executor adquiere takeover/lease en [`executor.py`](../src/domoai/application/executor.py:214) y libera normalmente en [`executor.py`](../src/domoai/application/executor.py:568). El flujo completo entre ambos puntos no está cubierto por un `try/finally` exterior.

Excepciones en persistencia, emergency stop, dynamic safety, settle, adapter o auditoría pueden saltar fuera antes de alcanzar la liberación normal.

### Riesgo

Un lease retenido puede impedir operaciones posteriores, ocultar quién tiene la autoridad y dejar un runtime vivo en una situación ambigua. El supervisor puede recuperarlo en algunos casos, pero esa recuperación no sustituye a un contrato local de liberación incondicional.

### Corrección requerida

El patrón debe ser:

```python
lease = await acquire()
try:
    return await execute_with_readback()
finally:
    await release_idempotently(lease)
```

Si liberar falla, el runtime debe persistir/auditar `UNKNOWN_AUTHORITY`, intentar stop seguro y bloquear nuevas mutaciones hasta reconciliación.

### Pruebas de aceptación

Inyectar excepciones después de:

1. adquirir el lease;
2. escribir físicamente;
3. guardar outcome;
4. ejecutar emergency stop;
5. settle del plan.

En todos los casos debe comprobarse release, stop cuando corresponda y estado durable inequívoco.

## 4. A-003 — Evidencia de dependencia de bundles

### Evidencia

El modelo [`ExecutionDependencyEvidence`](../src/domoai/domain/models.py:549) requiere plan predecesor, command IDs, estado confirmado, timestamp y digest. Sin embargo, el scheduler guarda en [`scheduler.py`](../src/domoai/application/scheduler.py:341) solo plan, estado y `state_versions`.

El gate de [`scheduler.py`](../src/domoai/application/scheduler.py:316) y `ExecutionAdmission` comprueba principalmente que `details` sea un diccionario y que `status` sea `confirmed_success`.

### Riesgo

La evidencia generada por el sistema no valida contra su propio contrato. Una fila manipulada o un caller interno puede fabricar una evidencia mínima con estado exitoso y satisfacer la dependencia.

### Corrección requerida

- usar un único builder de evidencia;
- serializar y volver a validar el modelo canónico;
- calcular digest desde campos canónicos;
- comprobar que el outcome existe en repository;
- ligar evidencia a bundle, miembro, comandos y versiones de estado exactas;
- rechazar cualquier evidencia parcial o de origen desconocido.

### Pruebas de aceptación

- la evidencia creada por scheduler pasa `model_validate`;
- modificar cualquier campo rompe el digest;
- cambiar plan, bundle, command ID o estado invalida la dependencia;
- una evidencia con solo `status` nunca autoriza el miembro siguiente.

## 5. A-004 — Saga de aprobación y commit

### Evidencia

`BundleCommitService` persiste el agregado y consume aprobaciones secuencialmente en [`bundle_commit.py`](../src/domoai/application/bundle_commit.py:177). En `schedule_plan`, la aprobación se consume antes de completar scheduler y persistencia en [`domotics_server.py`](../src/domoai/mcp/domotics_server.py:546).

### Riesgo

Un error de SQLite, scheduler o respuesta puede consumir un grant aunque el commit no haya terminado. El usuario debe aprobar de nuevo y el sistema puede quedar con estado parcial.

### Corrección requerida

Usar estados explícitos:

```text
grant:        pending -----------------> consumed
                  \
reservation:       reserved -> committed
                              \\-> released
```

La reserva debe ser atómica con el commit lógico o existir una saga durable con compensación. El consumo definitivo solo debe ocurrir cuando el efecto autorizado esté comprometido.

### Pruebas de aceptación

Fallar después de cada side effect y verificar que no se pierde autorización ni se duplica el commit. Reiniciar durante cada transición y comprobar recovery.

## 6. A-005 y A-006 — Automatizaciones recurrentes

### Evidencia

`schedule_recurring_plan` no llama al mismo `_admit_mcp_operation` que el flujo one-shot en [`domotics_server.py`](../src/domoai/mcp/domotics_server.py:774). El ID se forma con la hora actual en [`domotics_server.py`](../src/domoai/mcp/domotics_server.py:810), por lo que un retry genera otro schedule.

La ejecución futura usa un occurrence ID distinto y puede escapar de la pertenencia original del bundle.

### Riesgo

- schedules creados sin la misma garantía de ownership;
- miembros de bundles ejecutados fuera de orden;
- duplicación de cargas, calentadores o carga EV;
- automatizaciones persistentes sin una autoridad clara.

### Corrección requerida

Crear una operación de admission propia para standing automation que exija:

- template validado y versionado;
- policy y digest persistidos;
- principal propietario;
- scope explícito de la recurrencia;
- exclusión de miembros de bundles gestionados por otra saga;
- `idempotency_key` o digest único de intención.

### Pruebas de aceptación

- retry de creación devuelve el mismo schedule;
- cambiar el template produce un nuevo digest y requiere nueva autoridad;
- cancelar y recrear no deja dos schedules activos;
- un miembro de bundle no puede ejecutarse mediante ruta recurrente paralela.

## 7. A-008 y A-009 — Verdad de estado y números

### Evidencia

[`state_service.py`](../src/domoai/application/state_service.py:13) filtra `stale` y `unavailable`, pero no `invalid` cuando `allow_stale=False`.

[`models.py`](../src/domoai/domain/models.py:221) permite `NaN`, `Infinity`, `CURRENT` sin valor y combinaciones de estado/valor incoherentes.

### Riesgo

La capa de consulta puede parecer usable aunque la evidencia sea inválida. Además, las rutas JSON pueden serializar valores no estándar de manera distinta según el modo de Pydantic o el serializer utilizado.

### Corrección requerida

- validar finitud con `math.isfinite`;
- usar tipos estrictos donde corresponda;
- definir invariantes de `status` y `value`;
- excluir `invalid` de lecturas fresh-only;
- devolver diagnóstico estructurado de por qué falta estado;
- usar serialización JSON estricta (`allow_nan=False`).

## 8. A-010 — Persistencia split-brain

### Evidencia

`StateStore` actualiza memoria, versión y snapshots antes de `await _persist` en [`state_store.py`](../src/domoai/runtime/state_store.py:128).

Se reprodujo que una persistencia fallida deja el nuevo estado visible en memoria, aunque el reinicio lo pierda.

### Corrección requerida

Elegir una de estas estrategias explícitas:

1. persistir y confirmar antes de publicar en memoria;
2. rollback completo si la persistencia falla;
3. dirty queue durable con retry y estado de durabilidad visible;
4. bloquear validación/ejecución mientras la verdad durable esté degradada.

## 9. Secuencia de implementación recomendada

1. Implementar cursor/epoch/sequence por fuente.
2. Añadir invariantes estrictas a `StateSnapshot` y a todos los escenarios numéricos.
3. Hacer transaccional la actualización de StateStore.
4. Encerrar toda ejecución con release idempotente y estado `UNKNOWN`.
5. Unificar builder/verifier de evidencias.
6. Convertir aprobación y scheduling en operaciones idempotentes y compensables.
7. Añadir admission específica para standing automation.

## 9.1 Estado de ejecución del bloque A-002 — A-006

| Hallazgo | Estado final | Evidencia de cierre |
|---|---|---|
| A-002 | Implementado y verificado | `PlanExecutor` libera la autoridad en la frontera exterior de excepción; un release no confirmado persiste `UNKNOWN` y bloquea nuevas adquisiciones hasta reconciliación. [`executor.py`](../src/domoai/application/executor.py:581), [`control_takeover.py`](../src/domoai/runtime/control_takeover.py:150), [`test_executor_authority.py`](../tests/unit/runtime/test_executor_authority.py:1). |
| A-003 | Implementado y verificado | `ExecutionDependencyEvidence` es tipada, canónica y digestada; scheduler/admission exigen outcome confirmado persistido y command IDs exactos. [`models.py`](../src/domoai/domain/models.py:592), [`scheduler.py`](../src/domoai/application/scheduler.py:316), [`test_physical_authority_closure_composition.py`](../tests/composition/test_physical_authority_closure_composition.py:1). |
| A-004 | Implementado y verificado | Ledger SQLite `approval_reservations` con reserva, commit de la reserva junto al consumo del grant, release, batch atómico y recovery de bundles. [`010_approval_reservations.sql`](../src/domoai/persistence/migrations/010_approval_reservations.sql:1), [`approval_store.py`](../src/domoai/runtime/approval_store.py:407), [`test_durable_approval_store.py`](../tests/integration/test_durable_approval_store.py:1). |
| A-005 | Implementado y verificado | `STANDING_AUTOMATION`, plantilla/policy/owner/digests persistidos, autoridad vinculada a cada ocurrencia y rechazo de miembros de bundle. [`execution_admission.py`](../src/domoai/application/execution_admission.py:1), [`domotics_server.py`](../src/domoai/mcp/domotics_server.py:817). |
| A-006 | Implementado y verificado | ID determinista por digest de intención, retry idempotente, conflicto ante cambios y reactivación segura tras cancelación. [`scheduler.py`](../src/domoai/application/scheduler.py:623), [`011_recurring_authority.sql`](../src/domoai/persistence/migrations/011_recurring_authority.sql:1), [`test_scheduler.py`](../tests/unit/runtime/test_scheduler.py:710). |

La implementación queda trazada en [la especificación](../specs/178-phase0-authority-integrity/spec.md),
[el plan](../specs/178-phase0-authority-integrity/plan.md) y [las tareas](../specs/178-phase0-authority-integrity/tasks.md).

## 9.2 Evidencia ejecutable y límites

El gate final del bloque se ejecutó el 2026-09-04:

```text
uv run pytest -q                         1642 passed, 18 skipped, 1 warning
project-composition-check domoai         474 passed, 18 skipped, 1 warning
uv run ruff check .                      All checks passed
uv run mypy src                           Success: no issues in 142 files
uv run lint-imports                      4 contracts kept, 0 broken
check_architecture_contracts.py          Architecture contracts kept
check_runtime_contract_docs.py           Runtime contracts coherent
graphify . --update --no-viz --code-only 6572 nodes, 21924 edges
git diff --check                         No whitespace errors
```

También se probaron crash/exception, release ambiguo, evidencia manipulada,
reserva durante restart, compensación, retry duplicado, conflicto de template,
expiración y rechazo de miembros de bundle. El warning restante es la
deprecación conocida de `wait_for_logs` en el test de Mosquitto.

Riesgos residuales explícitos:

- no se ha hecho qualification contra hardware o transportes provider-specific;
- la API interna compatible `Scheduler.schedule_recurring(..., authority=None)`
  sigue permitiendo schedules legacy sin payload de standing authority; la
  ruta MCP pública sí exige admission y autoridad persistida;
- SQLite protege el runtime único, pero no sustituye coordinación distribuida
  entre gateways ni una transacción con un sistema de aprobaciones externo.

## 9.3 Registro aprobado de pendientes y decisión de recurrencia

El 2026-09-04 se aprobó cerrar los pendientes restantes como un único programa
trazado en [Spec 179](../specs/179-audit-pending-closure/spec.md), con cuatro
bloques secuenciales:

| Bloque | Hallazgos | Criterio de cierre |
|---|---|---|
| Fronteras deterministas | A-011, A-016, A-020 | providers async estrictos, disponibilidad booleana estricta y ningún valor no finito llega al solver |
| Operación acotada | A-012, A-015 | lifecycle/deadlines HA y resultado/diagnóstico por entidad |
| Autoridad durable | A-017, A-018, A-019 | outbox crítica, capability emitida por aggregate y reconciliación periódica sin retry físico ambiguo |
| Producto e identidad | A-007, A-013, A-014 | preview/prepare explícitos, tokens unívocos/temporales y autoridad standing coherente |

La decisión funcional para A-014 es el modelo conservador: toda automatización
standing necesita aprobación explícita vinculada al digest de recurrencia,
owner, scope y expiración; cada occurrence conserva la revalidación JIT y no
hereda aprobación ciegamente. Un plan `READY` puede ejecutarse como operación
única sin approval, pero crear una automatización persistente es una autoridad
distinta y sí requiere consentimiento standing.

El programa no amplía el alcance a active-active, qualification de hardware
real, catálogo completo de Skills ni autonomía productiva. Esos límites siguen
siendo gates posteriores, aunque los contratos del runtime queden endurecidos.

## 9.4 Cierre de A-007 y A-011 — A-020

El 2026-09-04 se implementaron y verificaron los cuatro bloques aprobados. La
separación de `preview_*` y `prepare_*` es explícita; `validate_*` queda como
alias persistente de compatibilidad y ahora declara correctamente que escribe.

| Hallazgo | Estado final | Evidencia de cierre |
|---|---|---|
| A-007 | Implementado y verificado | `preview_command`/`preview_plan` no persisten; `prepare_command`/`prepare_plan` persisten con anotación de mutación. [`domotics_server.py`](../src/domoai/mcp/domotics_server.py:380), [`test_domotics_mcp_contract.py`](../tests/contract/test_domotics_mcp_contract.py:273). |
| A-011 | Implementado y verificado | El SDK exige métodos coroutine/async-generator según el rol y emite diagnósticos estables. [`provider_sdk.py`](../src/domoai/runtime/provider_sdk.py:204), [`test_provider_sdk.py`](../tests/contract/test_provider_sdk.py:201). |
| A-012 | Implementado y verificado | Home Assistant reutiliza el cliente HTTP, limita REST/auth/receive/send/reconnect y cierra lifecycle de forma idempotente. [`client.py`](../src/domoai/adapters/home_assistant/client.py:16), [`test_home_assistant_client.py`](../tests/contract/test_home_assistant_client.py:74). |
| A-013 | Implementado y verificado | Tokens con `client_id` y `token_hash` únicos, expiración timezone-aware y reload atómico que conserva el estado anterior si el nuevo fichero es inválido. [`auth.py`](../src/domoai/mcp/auth.py:18), [`test_gateway_auth.py`](../tests/unit/mcp/test_gateway_auth.py:68). |
| A-014 | Implementado y verificado | Standing automation exige aprobación explícita ligada al digest de recurrencia incluso para un template `READY`; cada occurrence conserva JIT revalidation. [`domotics_server.py`](../src/domoai/mcp/domotics_server.py:877), [`spec.md`](../specs/145-recurring-intent-authority/spec.md:1). |
| A-015 | Implementado y verificado | Las lecturas compuestas devuelven resultado por referencia y proyectan faltantes/errores como `unavailable` con diagnóstico auditable. [`composite_adapter.py`](../src/domoai/runtime/composite_adapter.py:235), [`discovery_service.py`](../src/domoai/application/discovery_service.py:183). |
| A-016 | Implementado y verificado | Availability solo acepta `bool` real y no coerciona strings como `"false"`. [`registry.py`](../src/domoai/runtime/registry.py:300), [`test_registry_reconciliation.py`](../tests/unit/runtime/test_registry_reconciliation.py:217). |
| A-017 | Implementado y verificado | Eventos críticos se escriben en outbox secuenciada, con payload redactado, idempotencia y acknowledgement bloqueante acotado; el dispatcher reintenta sin duplicar. [`012_audit_outbox.sql`](../src/domoai/persistence/migrations/012_audit_outbox.sql:1), [`audit_outbox.py`](../src/domoai/persistence/audit_outbox.py:1), [`test_audit_outbox.py`](../tests/integration/test_audit_outbox.py:1). |
| A-018 | Implementado y verificado | La admisión rechaza `aggregate_owner` y solo acepta capability opaca emitida por el bundle, ligada a miembro/digest, expirable y single-use. [`execution_admission.py`](../src/domoai/application/execution_admission.py:99), [`test_execution_admission.py`](../tests/unit/application/test_execution_admission.py:168). |
| A-019 | Implementado y verificado | El scheduler ejecuta reconciliación periódica de planes y bundles; las transiciones `UNKNOWN` son condicionales e idempotentes y no reintentan side effects ambiguos. [`recovery.py`](../src/domoai/application/recovery.py:22), [`runtime_factory.py`](../src/domoai/application/runtime_factory.py:976). |
| A-020 | Implementado y verificado | Los límites numéricos de escenarios y entradas solver rechazan `NaN` e infinitos antes de CP-SAT. [`scenario.py`](../src/domoai/optimizer/scenario.py:32), [`test_scenario_limits.py`](../tests/unit/optimizer/test_scenario_limits.py:74). |

La evidencia cuantitativa final de la sección 9.2 corresponde a la suite, los
gates estáticos, composición y Graphify ejecutados sobre este worktree el
2026-09-04.

## 9.5 Composition Review Report

- **Subsistemas modificados:** dominio y contratos, StateStore/runtime,
  scheduler y recovery, executor/admission/control takeover, BundleCommit y
  approval ledger, persistencia/audit outbox, MCP/auth, Provider SDK, adapters
  Home Assistant/CompositeAdapter, optimizer/CP-SAT y documentación operativa.
- **Vecinos revisados:** composición raíz, repositorios SQLite y migraciones,
  consumidores de eventos, gateway MCP, rutas de scheduler y ejecución,
  adapters/proveedores y escenarios de integración con Testcontainers.
- **Invariantes comprobadas:** autoridad única, ownership de bundle, capability
  opaca y single-use, digest/idempotencia, ACK durable de eventos críticos,
  recovery condicional, límites/deadlines, lifecycle idempotente y separación
  preview/read-only frente a prepare/mutación.
- **Gates ejecutados:** Import Linter (4 contratos), arquitectura, contratos de
  runtime, suite completa (1.642 tests), composición/integración/contratos
  (474 tests), exportación de 57 schemas, Graphify y `git diff --check`.
- **Dependencias reales:** la suite de composición usó sus dependencias
  desechables configuradas, incluido Mosquitto mediante Testcontainers; los
  límites de hardware físico y coordinación distribuida no son simulables de
  forma equivalente en este worktree.
- **Fallos encontrados:** la primera ejecución directa de pytest no tenía el
  entry point `domoai-mcp` en `PATH`; se reprodujo mediante `uv run`, donde el
  contrato sobre el protocolo pasó. No quedó ningún fallo de producto.
- **Riesgos residuales:** qualification provider/hardware real, active-active o
  coordinación entre gateways, y dependencias externas de aprobación/SQLite.

**Veredicto:** `PASS WITH RISKS`. La Fase 0 técnica A-001 — A-020 queda
cerrada, pero esos riesgos siguen bloqueando autonomía productiva sobre
hardware hasta las gates posteriores.

## 9.6 Validación live final y correcciones de consistencia

El 2026-09-04 se ejecutó una validación final contra el gateway MCP real en
`http://127.0.0.1:8124/mcp`, con cliente MCP Streamable HTTP, laboratorio
Docker/KNX Virtual y comprobación directa de los transportes. Se probaron seis
rutas de actuador — Home Assistant, KNX, Modbus, Matter y dos dispositivos
Zigbee2MQTT— mediante `prepare_plan` → `execute_plan` → `get_state`, con 12
acciones confirmadas, 0 fallos, 0 rechazos, 0 indisponibles, 0 desconocidos y
0 mismatches de readback. Todos terminaron apagados y `current`.

La evidencia completa está en
[`mcp-live-operation-latest.md`](evidence/mcp-live-operation-latest.md). La
bobina Modbus y los mensajes MQTT retained se verificaron fuera de la API MCP,
por lo que el resultado no depende únicamente de la proyección del gateway.

Durante este cierre se corrigieron los siguientes hallazgos operativos:

- `StateStore` serializa las mutaciones y protege la verdad pública contra
  observaciones cursorless antiguas; las transiciones degradadas nuevas se
  aceptan siempre para conservar el comportamiento fail-closed.
- El wrapper del gateway espera la limpieza real aunque reciba cancelaciones
  repetidas, observa excepciones de cierre y su entrypoint suprime el
  `KeyboardInterrupt` controlado sin traceback.
- `runtime_factory` libera adapters, recursos y ownership si una cancelación
  interrumpe cualquier punto de la construcción después de adquirir el lease;
  el wrapper conserva la limpieza aunque la cancelación se repita.
- HA no reconecta por inactividad normal del WebSocket; Zigbee2MQTT espera el
  evento de readback posterior al comando y el fake de laboratorio se
  reconstruyó con soporte `/get`; refreshes concurrentes se serializan y una
  segunda orden live se rechaza si su readback anterior sigue pendiente.
- El gateway entra en `try/finally` antes de `start()` y el executor solo
  marca `UNKNOWN` o libera autoridad si esa invocación obtuvo el claim durable;
  un competidor rechazado no puede alterar el plan ganador.

La verificación final quedó en:

```text
uv run pytest -q                                      1788 passed, 18 skipped
project-composition-check domoai                     503 passed, 18 skipped
uv run ruff check src tests                            All checks passed
uv run mypy src                                       162 files, no issues
check_runtime_contract_docs.py                        coherent
export_schemas.py                                     73 schemas exported
git diff --check                                      PASS
```

El gate `/readyz` continúa devolviendo `503` solo por
`physical_actuator_not_qualified`. Es un bloqueo deliberado: esta evidencia
prueba el sistema software y el laboratorio, no commissioning físico,
firmware/radio/cableado, HIL físico de batería/EV, autenticación productiva ni
coordinación/observabilidad remota distribuida.

## 10. Criterio de salida de la fase

Para el alcance A-001 — A-020, el criterio técnico se supera:
las pruebas de replay, crash, duplicación, rollback, idempotencia y validación
estricta pasan; el evidence model canónico es el único aceptado en las nuevas
fronteras; y una operación física no continúa con autoridad o estado ambiguos.
La habilitación de autonomía productiva sigue condicionada a los riesgos
residuales y a las fases posteriores indicadas arriba.

## 11. Addendum — seguridad para hardware real y varios hosts — 2026-09-05

### 11.1 Contribución de Fase 0

Fase 0 deja protegida la frontera local de autoridad: estado monotónico,
persistencia durable, aprobación ligada a digest, idempotencia, leases locales,
readback y estados `UNKNOWN` o `fail-closed`. También impide que una evidencia
de gemelo digital cree por sí sola autoridad física.

Esto es condición necesaria, pero no suficiente, para una plataforma
multi-host y físicamente cualificada. El riesgo pendiente es que la autoridad
local no incorpora todavía un fencing epoch compartido entre hosts ni una
prueba de que el hardware real rechaza una orden de un owner antiguo.

### 11.2 Brechas de seguridad pendientes

| Riesgo | Situación actual | Corrección necesaria |
|---|---|---|
| Split-brain | SQLite/POSIX protege un host y un fichero local. | Lease externo por hogar y fencing monotónico obligatorio en cada escritura. |
| Orden antigua en vuelo | La autoridad local puede quedar incierta durante una partición externa. | Token de epoch validado por gateway/adapter y stop o `UNKNOWN` al perder lease. |
| Replay entre réplicas | La idempotencia actual está ligada al runtime/almacenamiento local. | Ledger compartido de `idempotency_key` y resultado único entre hosts. |
| Evidencia falsa de hardware | La evidencia digital está correctamente etiquetada como `digital_twin`. | Commissioning atendido, evidencia firmada y binding físico por dispositivo. |
| Actuador no cualificado | `/readyz` permanece bloqueado con `physical_actuator_not_qualified`. | Solo abrir readiness después de HIL y pruebas de límites/readback reales. |

### 11.3 Criterios de aceptación adicionales

- Dos hosts que reclamen el mismo `household_id` deben dejar solo uno con
  permiso de escritura.
- Un comando con epoch anterior debe ser rechazado aunque el socket del
  adapter siga abierto.
- La pérdida de lease durante una orden latched debe detenerla o dejarla en
  `UNKNOWN`, nunca confirmarla implícitamente.
- Un replay en otro host debe devolver el resultado durable anterior sin
  repetir la mutación.
- Una qualification digital, aunque sea verde, debe seguir siendo incapaz de
  crear un grant físico.
- Cada evidencia HIL debe estar ligada al dispositivo, capability, firmware,
  operador, ventana temporal y digest de configuración.

### 11.4 Dependencias de salida

La implementación local de Fase 0 no debe ampliarse con un lock distribuido
improvisado. El diseño de lease, fencing, migración del ledger y recuperación
debe abrir una especificación propia antes de activar más de un owner. La
qualification física depende del banco descrito en [Spec 133](../specs/133-battery-hil-certification/tasks.md)
y de las gates de adapters reales de Fase 1 y Fase 4.
