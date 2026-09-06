# Auditoría integral de DomoAI

**Fecha:** 2026-09-04

**Alcance:** estado actual del worktree, código fuente, tests, contratos, persistencia, runtime, adapters, MCP, optimización, despliegue y documentación.

**Tipo:** auditoría estática, estructural y ejecutable.
**Resultado:** la auditoría identificó 20 hallazgos. A-001 — A-020 están
implementados y verificados como cierre técnico de Fase 0 en el worktree; la
qualification de hardware, la coordinación distribuida y la autonomía física
siguen bloqueadas por gates posteriores.

La observabilidad operativa pendiente de la Fase 1 también quedó cerrada
técnicamente en este worktree: [`auditoria-fase-1-robustez-operativa.md`](auditoria-fase-1-robustez-operativa.md)
registra las señales, pruebas y riesgos residuales. Este cierre no convierte
las métricas process-local en evidencia durable ni habilita control físico.

La operación live del gateway MCP también quedó verificada desde un cliente
MCP real contra el laboratorio, con ciclos de encendido/apagado y readback en
los cinco adapters configurados. La evidencia detallada, incluyendo el
diagnóstico y corrección de recuperación tras reconexión, está en
[`docs/evidence/mcp-live-operation-latest.md`](evidence/mcp-live-operation-latest.md).

## 1. Resumen ejecutivo

DomoAI ya tiene una base arquitectónica excepcional para convertirse en una plataforma agentic de domótica: modelo semántico universal, adapters aislados, runtime compartido, MCP unificado, políticas de seguridad, aprobación explícita, ejecutor con readback, persistencia SQLite, HIL y optimización proposal-only con OR-Tools.

La principal conclusión es que el proyecto no necesita empezar por añadir más protocolos. Antes debe cerrar las garantías de integridad y autoridad que separan un prototipo avanzado de una plataforma capaz de controlar hardware de forma autónoma:

1. orden temporal y deduplicación de eventos;
2. liberación incondicional de leases y estado `UNKNOWN` tras fallos ambiguos;
3. validación criptográfica y referencial de evidencias de bundles;
4. transacciones o sagas compensables para aprobaciones y schedules;
5. admission uniforme para automatizaciones recurrentes;
6. idempotencia y consistencia entre memoria y persistencia;
7. validación estricta de valores, estados y disponibilidad.

Estos contratos ya están cerrados en Fase 0 y permanecen bajo regresión. La
autonomía física no se habilita hasta completar qualification de hardware y
coordinación operativa real.

No se identificó un P0 evidente. Sí existen varios hallazgos P1/P2 que deben resolverse antes de habilitar control autónomo de baterías, cargadores EV, cerraduras, puertas o cualquier actuador de riesgo.

## 2. Arquitectura observada

La arquitectura actual se aproxima a este modelo:

```text
Agentes: Claude Code, Codex y otros clientes MCP
                         |
                  MCP unificado
          domótica semántica + optimizer
                         |
                  Runtime compartido
   registry | StateStore | policy | admission | executor
                         |
        CompositeAdapter / Provider SDK / scheduler
                         |
 HA | Matter | MQTT/Zigbee2MQTT | KNX | Modbus | SDK
                         |
                    dispositivos
```

La decisión de exponer un solo runtime de autoridad es correcta. No conviene crear dos servidores públicos independientes que puedan ejecutar acciones por caminos distintos. La separación entre `domotics` y `optimizer` debe ser lógica y de herramientas, no de autoridad física.

OR-Tools está correctamente limitado a generar, validar y explicar propuestas. Nunca debe poder llamar directamente a un adapter, consumir aprobaciones o ejecutar comandos físicos.

## 3. Fortalezas confirmadas

- **Modelo universal de dispositivos:** el agente trabaja con tipos y capacidades semánticas, no con endpoints específicos de fabricantes.
- **Frontera de adapters:** Home Assistant, Matter, KNX, Modbus y Zigbee2MQTT se traducen antes de alcanzar el dominio.
- **MCP semántico:** la superficie evita crear miles de tools específicas por dispositivo.
- **Separación plan/ejecución:** la validación, policy, approval, freshness, safety y executor están diferenciados.
- **Autoridad compartida:** el gateway permite varios agentes sobre un único estado, scheduler y ownership.
- **Ejecución física prudente:** existe readback, control de takeover y posibilidad de representar estados desconocidos.
- **Optimización acotada:** el escenario energético se valida y se propone, pero no actúa por sí mismo.
- **Laboratorio reproducible:** existen fixtures y perfiles para HA, MQTT, Zigbee2MQTT, Modbus, Matter y KNX.
- **Contratos versionados:** los modelos Pydantic y los JSON Schema forman una frontera explícita.
- **Documentación operativa:** [README.md](../README.md), [docs/contracts.md](contracts.md) y [docs/PENDIENTES.md](PENDIENTES.md) describen gran parte de la arquitectura real.

## 4. Hallazgos consolidados

Las auditorías detalladas están separadas por fase:

- [Fase 0 — Seguridad e integridad](auditoria-fase-0-seguridad-integridad.md)
- [Fase 1 — Robustez operativa](auditoria-fase-1-robustez-operativa.md)
- [Fase 2 — Producto agentic](auditoria-fase-2-producto-agentic.md)
- [Fase 3 — Escalabilidad e identidad](auditoria-fase-3-escalabilidad-identidad.md)
- [Fase 4 — Ventaja diferencial](auditoria-fase-4-ventaja-diferencial.md)

| ID | Prioridad | Estado actual | Hallazgo | Referencia principal |
|---|---:|---|---|---|
| A-001 | P1 | Cerrado en Fase 0 | Eventos fuera de orden pueden regresar el estado canónico. | [`state_store.py`](../src/domoai/runtime/state_store.py:120) |
| A-002 | P1 | Cerrado en Fase 0 | Una excepción puede impedir liberar un lease de control. | [`executor.py`](../src/domoai/application/executor.py:214) |
| A-003 | P1/P2 | Cerrado en Fase 0 | La evidencia de dependencia generada por scheduler no cumple el modelo canónico. | [`scheduler.py`](../src/domoai/application/scheduler.py:341) |
| A-004 | P1/P2 | Cerrado en Fase 0 | Aprobaciones consumidas antes de completar la saga pueden quedar quemadas. | [`bundle_commit.py`](../src/domoai/application/bundle_commit.py:177) |
| A-005 | P1/P2 | Cerrado en Fase 0 | La creación recurrente no atraviesa el mismo admission que la ejecución única. | [`domotics_server.py`](../src/domoai/mcp/domotics_server.py:774) |
| A-006 | P1/P2 | Cerrado en Fase 0 | Crear schedules recurrentes no es idempotente. | [`domotics_server.py`](../src/domoai/mcp/domotics_server.py:810) |
| A-007 | P2 | Cerrado en Fase 0 | Herramientas declaradas read-only escribían validaciones persistentes. | [`domotics_server.py`](../src/domoai/mcp/domotics_server.py:178) |
| A-008 | P2 | Cerrado en Fase 0 | `get_state(allow_stale=False)` puede devolver snapshots inválidos. | [`state_service.py`](../src/domoai/application/state_service.py:13) |
| A-009 | P2 | Cerrado en Fase 0 | Snapshots aceptan `NaN`, estados incoherentes y valores no finitos. | [`models.py`](../src/domoai/domain/models.py:221) |
| A-010 | P2 | Cerrado en Fase 0 | StateStore modifica memoria antes de confirmar persistencia. | [`state_store.py`](../src/domoai/runtime/state_store.py:128) |
| A-011 | P2 | Cerrado en Fase 0 | El SDK aceptaba providers síncronos que fallaban al invocarse. | [`provider_sdk.py`](../src/domoai/runtime/provider_sdk.py:188) |
| A-012 | P2/P3 | Cerrado en Fase 0 | Home Assistant carecía de lifecycle de clientes y deadlines completos. | [`client.py`](../src/domoai/adapters/home_assistant/client.py:104) |
| A-013 | P2 | Cerrado en Fase 0 | Había ambigüedad en hashes de tokens y fechas sin timezone. | [`auth.py`](../src/domoai/mcp/auth.py:17) |
| A-014 | P2 | Cerrado en Fase 0 | La documentación de autoridad recurrente no coincidía completamente con el código. | [`spec.md`](../specs/145-recurring-intent-authority/spec.md:18) |
| A-015 | P2/P3 | Cerrado en Fase 0 + regresión Fase 1 | CompositeAdapter podía devolver estados parciales y perder capacidades cuando una referencia devolvía varias. | [`composite_adapter.py`](../src/domoai/runtime/composite_adapter.py:245) |
| A-016 | P2/P3 | Cerrado en Fase 0 | `bool("false")` registraba como disponible una entidad no disponible. | [`registry.py`](../src/domoai/runtime/registry.py:177) |
| A-017 | P2/P3 | Cerrado en Fase 0 | La auditoría asíncrona podía perder eventos críticos ante caída del proceso. | [`events.py`](../src/domoai/runtime/events.py:133) |
| A-018 | P2/P3 | Cerrado en Fase 0 | `aggregate_owner: bool` era una autoridad interna fácilmente falsificable. | [`execution_admission.py`](../src/domoai/application/execution_admission.py:96) |
| A-019 | P2/P3 | Cerrado en Fase 0 | La recuperación de sagas se concentraba en el arranque. | [`recovery.py`](../src/domoai/application/recovery.py:22) |
| A-020 | P2/P3 | Cerrado en Fase 0 | Algunos números del escenario aceptaban infinito y llegaban a CP-SAT. | [`scenario.py`](../src/domoai/optimizer/scenario.py:101) |

## 5. Auditoría ejecutable

Se ejecutó la suite sobre el worktree auditado:

```text
uv run pytest -q                         1642 passed, 18 skipped, 1 warning
uv run pytest --cov=domoai ...            86% coverage (medición anterior; no repetida en este cierre)
uv run ruff check .                      All checks passed
uv run mypy src                           Success: 142 source files
uv run lint-imports                      4 contracts kept, 0 broken
uv run python scripts/check_architecture_contracts.py
                                          Architecture contracts kept
uv run python scripts/check_runtime_contract_docs.py
                                          Runtime contracts coherent
git diff --check                         No whitespace errors
project-composition-check                474 passed, 18 skipped, 1 warning
graphify . --update --no-viz --code-only 6572 nodes, 21924 edges
```

La suite verde demuestra que el comportamiento cubierto funciona. No demuestra por sí sola seguridad ante todos los escenarios de hardware real, transportes provider-specific, coordinación distribuida, sockets silenciosos o una interrupción externa de SQLite.

Las áreas con más riesgo y menor cobertura deben recibir pruebas adicionales: Home Assistant, Matter transport, bundle commit, control takeover, scheduler, auditoría y HIL.

No se ejecutó un análisis específico de dependencias con `bandit`, `pip-audit`, `semgrep` o `trivy` porque esas herramientas no están instaladas en el entorno.

### 5.1 Actualización de Fase 0

El detalle de implementación, pruebas de aceptación y riesgos residuales está
en [la auditoría de Fase 0](auditoria-fase-0-seguridad-integridad.md). El cierre
incluye preview/prepare MCP, lifecycle y deadlines, outbox crítica,
capabilities de aggregate, reconciliación periódica, tokens estrictos y
autoridad standing conservadora. Los límites de hardware real y coordinación
distribuida siguen siendo gates posteriores.

### 5.2 Actualización de Fase 1

La señal operativa se integró en el colector existente sin alterar los campos
previos: command latency/outcome, readback mismatch, calidad por fuente,
cursor gap/replay/resync, leases, approvals, bundles y límites de cardinalidad.
La implementación, el contrato MCP, la revisión de composición y los
resultados exactos están en [la auditoría de Fase 1](auditoria-fase-1-robustez-operativa.md).
Los contadores son diagnósticos y se reinician al reiniciar el proceso; el
AuditLog y los repositories siguen siendo la fuente durable. La qualification
automatizable contra el laboratorio pasó para MQTT/Zigbee2MQTT, Modbus, Matter,
Home Assistant Provider y el gateway KNX Virtual/ETS/knxd activo. La composición
multi-adapter completa pasó después de corregir en `CompositeAdapter` la pérdida
del segundo snapshot cuando una misma referencia devuelve varias capacidades.
El broker Testcontainers quedó migrado a la estrategia estructurada y sin
warnings deprecados. HIL físico, coordinación distribuida y exportación remota
de métricas permanecen como gates externas o evolución posterior.

## 6. Orden recomendado de actuación

### Antes de cualquier autonomía física

1. Mantener el cierre de Fase 0 bajo regresión y ampliar qualification con providers/hardware reales.
2. Mantener A-001 — A-020 bajo regresión antes de afirmar madurez operativa completa.
3. Añadir pruebas de replay, duplicación, crash, rollback, timeout y estado desconocido contra dependencias reales.
4. Mantener batería, EV, cerraduras y puertas en fail-closed hasta superar las gates de qualification reales.

### Antes de producción multi-adapter

1. Repetir la qualification de A-011, A-012, A-015, A-017 y A-019 con
   dependencias reales de producción; la matriz del laboratorio virtual ya
   cubre las fronteras disponibles, pero no sustituye hardware físico.
2. Mantener la coordinación single-writer y los pools bounded process-local
   hasta que las métricas justifiquen escalar; la evaluación de la evolución
   está en [coordinación, pools y métricas remotas](evaluacion-coordinacion-distribuida-pools-metricas.md).
3. Probar fallos con dependencias reales mediante Testcontainers o hardware
   virtual equivalente; el broker real y la matriz live disponible ya están
   ejecutados y documentados en Fase 1.

### Evolución de producto

1. Consumir los contratos de preview/prepare y standing authority de Fase 0.
2. Versionar el DSL de optimización y añadir explicaciones de soluciones.
3. Crear Skills de EV, clima, batería, solar, noche, vacaciones y diagnóstico.
4. Añadir un motor de automatización local que funcione sin agente ni Internet.

### Evolución de plataforma

1. Identidad multiusuario y multi-home.
2. ACL por vivienda, área, dispositivo y capability.
3. Conectores internos para inversores, baterías, EV y HVAC detrás del
   adaptador universal; no adapters públicos específicos.
4. Commissioning físico verificable y observabilidad de producción.

## 7. Criterio final de madurez

DomoAI podrá considerarse listo para control autónomo cuando pueda demostrar, con pruebas reproducibles y evidencia durable, que:

- un evento viejo nunca vence a uno nuevo sin una política explícita;
- toda autoridad se libera aunque falle cualquier dependencia;
- ninguna aprobación se consume sin commit o compensación;
- repetir cualquier request no duplica acciones ni schedules;
- un crash después de una escritura produce `UNKNOWN`, no éxito ficticio;
- memoria, persistencia, auditoría y estado físico pueden reconciliarse;
- el runtime funciona con agentes desconectados para automatizaciones locales;
- todo adapter informa capabilities y disponibilidad con tipos estrictos;
- una propuesta de IA nunca puede saltarse policy, admission o safety.

## 8. Readiness consolidada — 2026-09-04

La comprobación final de software, MCP, autenticación, gemelo digital,
laboratorio y gates externas queda consolidada en
[`evidence/production-readiness-latest.md`](evidence/production-readiness-latest.md).
El resultado es `SOFTWARE_AND_PROCESS_LAB_VERIFIED`: el proyecto está completo
en el alcance implementado y reproducible; el preflight estático del perfil
local pasa, pero la activación productiva aún requiere los secretos/endpoints
externos y `/readyz` permanece en 503 por la ausencia de qualification HIL
física. No se han fabricado credenciales,
hardware ni evidencia de fencing distribuido para convertir esas gates en PASS.

## 9. Addendum de readiness multi-host y cualificación física — 2026-09-05

### 9.1 Veredicto actualizado

La arquitectura software central está implementada: MCP es la interfaz
semántica de los agentes, el runtime concentra la autoridad, los adapters
traducen protocolos, el modelo de dispositivos es canónico, OR-Tools produce
propuestas y las Skills no pueden saltarse policy, approval, admission ni
executor.

El proyecto todavía no satisface la definición completa de plataforma
domótica productiva multi-host y físicamente cualificada. Las brechas restantes
son gates de infraestructura, coordinación distribuida y evidencia física; no
son un fallo del gemelo digital ni de las pruebas locales.

| Gate | Estado | Qué existe | Qué falta para cerrarla |
|---|---|---|---|
| Runtime semántico | PASS | [`unified_server.py`](../src/domoai/mcp/unified_server.py) expone Domotics y OR-Tools sobre un runtime compartido. | Si se requieren dos procesos MCP independientes, habría que definir su despliegue; la separación de autoridad debe seguir siendo única. |
| Modelo universal y adaptador | PASS | `Device`, `Capability`, `SourceRef`, `CompositeAdapter` y SDK v1 cubren la frontera universal sobre HA, Matter, Zigbee2MQTT, KNX, Modbus y fixture. | Evolucionar la ontología y los conectores internos; no crear adapters públicos específicos por fabricante. |
| Plan, policy y seguridad | PASS | Plan → policy → approval/admission → executor → readback está implementado y probado. | Propagar una autoridad distribuida con fencing cuando haya más de un host. |
| Gemelo digital | PASS software | La matriz digital cubre seis perfiles, diez dominios y fallos reproducibles. | Cualificación independiente de firmware, radio, cableado, timing y comportamiento físico. |
| Hardware y commissioning | BLOCKED_EXTERNAL | Existen modelos de commissioning y qualification fail-closed. | Banco real, pruebas atendidas, evidencias firmadas y HIL de batería/EV/HVAC/actuadores de riesgo. |
| Multi-host | NOT_ENABLED | Ownership single-writer, SQLite local, colas bounded y workers process-local. | Lease externo, fencing monotónico, estado compartido, failover, idempotencia entre hosts y pruebas de partición. |
| Métricas remotas | PASS por instancia | `GET /metrics` protegido, bounded y opt-in. | `instance_id`, histórico, agregación multi-réplica, alertas y semántica de reinicio. |
| Producción | PARTIAL | Compose, Caddy, auth hash-only y preflight estático. | Secretos/endpoints reales, TLS, DNS, firewall, preflight de red, backups/restores y operación sostenida. |

### 9.2 Trabajo requerido para cumplir la visión completa

#### A. Cualificación física

1. Montar un banco reproducible con Home Assistant, Matter/Thread,
   Zigbee2MQTT, KNX/ETS/knxd y Modbus reales.
2. Identificar hardware, modelo, firmware, cableado, configuración y límites
   antes de habilitar escrituras.
3. Ejecutar por adapter descubrimiento, lectura, escritura, readback,
   desconexión, stale, reconexión, duplicado, replay, restart y fallo parcial.
4. Repetir la matriz sobre batería, EV, HVAC y cualquier actuador de riesgo.
5. Archivar evidencia firmada con `run_id`, hardware, firmware, configuración,
   operador, timestamps, logs y digests.
6. Hacer que solo esa evidencia permita pasar `/readyz` de
   `physical_actuator_not_qualified`.

#### B. Coordinación multi-host

1. Elegir explícitamente active-passive o active-active por hogar.
2. Incorporar un proveedor externo de lease/ownership.
3. Emitir un fencing token monotónico y exigirlo en cada escritura física.
4. Rechazar tokens antiguos en gateway y adapters después de un takeover.
5. Compartir estado, outbox, ledger de ejecución y resultados de idempotencia.
6. Particionar una cola distribuida por hogar, manteniendo separada la lane
   de autoridad física de optimización, auditoría y tareas best-effort.
7. Probar split-brain, partición, caída durante escritura, takeover, replay y
   recuperación sin doble actuación.
8. Agregar métricas de todas las instancias con histórico y alertas.

#### C. Activación productiva

Hay que sustituir placeholders por secretos reales, certificados, DNS,
firewall y endpoints operativos; ejecutar `deployment preflight --network`;
probar backup/restore, rotación, recuperación, carga y SLOs en el entorno
real. Las pruebas omitidas por infraestructura deben dejar de ser `skip` y
convertirse en evidencia trazable.

### 9.3 Criterio maestro de cierre

La visión completa solo puede marcarse como cerrada cuando:

- dos hosts no puedan escribir simultáneamente en la misma casa;
- un fencing token antiguo sea rechazado en todos los caminos físicos;
- un replay entre hosts produzca un único efecto;
- exista evidencia HIL firmada para todos los actuadores de riesgo;
- cada adapter tenga command/readback sobre infraestructura real;
- `/readyz` pase por qualification física, no por el gemelo digital;
- las métricas se agreguen históricamente;
- el despliegue real supere el preflight de red y las pruebas de recuperación.

La trazabilidad detallada se mantiene en
[`production-readiness-latest.md`](evidence/production-readiness-latest.md),
[`evaluacion-coordinacion-distribuida-pools-metricas.md`](evaluacion-coordinacion-distribuida-pools-metricas.md),
el pendiente HIL de [Spec 133](../specs/133-battery-hil-certification/tasks.md)
y las gates condicionales de [Spec 140](../specs/140-real-composition-tests/tasks.md)
y [Spec 141](../specs/141-provider-contract-tests/tasks.md).

### 9.4 Ejecución de Fase 1 — 2026-09-05

El cierre operativo se revalidó contra el workspace actual: broker Mosquitto
`1 passed`, qualification live `6 passed, 1 skipped`, regresión completa
`1799 passed, 18 skipped` y `project-composition-check` `504 passed, 18 skipped`.
Ruff, mypy, arquitectura, documentación de contratos e Import Linter pasaron.
La evidencia estructural incremental quedó en Graphify con `7509` nodos,
`23187` aristas y `454` comunidades. El resultado es `PASS WITH RISKS`: HIL
físico, fencing multi-host, histórico agregado y dependencias productivas
siguen fuera de cierre local.
