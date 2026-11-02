# Auditoría de concurrencia, idempotencia y recuperación

## EXEC-001 — Carrera que permite duplicar un despacho físico

**Severidad:** crítica
**Estado:** cerrado (2026-09-10)
**Evidencia:** [`executor.py:479-549`](../../src/domoai/application/executor.py#L479), [`coordination.py:31-81`](../../src/domoai/persistence/coordination.py#L31), [`coordination.py:112-159`](../../src/domoai/persistence/coordination.py#L112)

El executor reclama una `PhysicalIntent` y bloquea únicamente si su estado es `UNKNOWN`. El repositorio implementa `INSERT ... ON CONFLICT DO NOTHING` y después devuelve la fila existente.

### Secuencia vulnerable

1. El plan A crea la clave `(household, idempotency_key)` en estado `PREPARED`.
2. El plan B, distinto y concurrente, usa la misma clave.
3. La inserción de B no crea una fila nueva, pero `claim()` devuelve la intención de A en estado `PREPARED`.
4. A y B superan la comprobación y ambos pueden llegar al adapter físico.

La implementación anterior tenía esa carrera. La implementación actual conserva
la secuencia como contexto histórico y añade una reclamación durable explícita
antes de cualquier llamada física.

### Impacto

Una acción como encender, desbloquear, mover, cargar o detener un dispositivo puede ejecutarse dos veces. El ledger deja de ser una garantía de “un intento físico por clave”.

### Cierre aplicado

- Se añadió la transición durable a `DISPATCHING` con `owner_id`,
  `execution_attempt_id` y `fencing_epoch`, protegida por la unicidad de
  `(household_id, idempotency_key)`.
- Solo `ACQUIRED` puede llegar al adapter; `IN_FLIGHT`, `REPLAY` y `CONFLICT`
  no realizan despacho físico. Una clave ya vinculada a otro `plan_id` o
  `command_id` se rechaza como conflicto no reintentable.
- El settlement es CAS y exige identidad de owner, attempt, epoch y estado;
  una liquidación obsoleta no puede sobrescribir al ganador.
- La recuperación de una marca incierta conserva la evidencia y la mueve a
  `UNKNOWN` sin redispatch automático.

### Verificación de cierre

- La carrera SQLite entre dos conexiones y dos planes distintos se repitió 100
  veces: cada repetición produjo exactamente `ACQUIRED` y `CONFLICT`.
- Se verificaron replay terminal sin nueva llamada al adapter, recuperación de
  `DISPATCHING` y settlement stale sin overwrite.
- La suite completa terminó con `2261 passed, 18 skipped`; la suite focalizada
  final del cambio con `85 passed`.
- `ruff`, `mypy`, Import Linter y `project-composition-check domoai` pasaron.
  La composición PostgreSQL pasó sus 2 tests contra un servidor real de
  Testcontainers.
- La revisión de composición quedó registrada en
  [`composition-review.md`](../../specs/207-exclusive-physical-dispatch/composition-review.md).

## REC-001 — Recuperación global en un runtime multi-host

**Severidad:** alta
**Estado:** cerrado — 2026-09-10
**Evidencia:** [`recovery.py:10-82`](../../src/domoai/application/recovery.py#L10), [`repositories.py:654-930`](../../src/domoai/persistence/repositories.py#L654), [`coordination.py:161-236`](../../src/domoai/persistence/coordination.py#L161), [`runtime_factory.py:1392-1428`](../../src/domoai/application/runtime_factory.py#L1392)

`PlanRecoveryService` ya no trata todo `EXECUTING` como huérfano. El runtime
persiste el deployment y la evidencia no secreta de la lease al reclamar un
plan; la recuperación multi-host consulta únicamente el deployment actual y
acepta solo leases expiradas de un epoch menor que el fencing epoch actual. La
transición a `UNKNOWN` se protege con CAS sobre estado y lease esperada. Las
filas legacy sin evidencia se dejan intactas en multi-host y se audita el
salto.

`PhysicalIntentRepository.recover_inflight()` aplica el mismo alcance a las
intenciones físicas: deployment explícito y epoch inferior al actual, con
estado/epoch condicionados en el `UPDATE`. `build_runtime()` pasa el deployment
y el epoch después de adquirir ownership y la lease externa. En single-host,
el camino sin fencing conserva la recuperación de filas legacy y vincula su
deployment local.

### Impacto

Un host que reinicia puede marcar como huérfano el trabajo que otro host todavía está ejecutando. En el caso físico, esto puede convertir una intención viva en `UNKNOWN`, provocando detenciones falsas, reintentos ambiguos o pérdida de trazabilidad.

### Cierre recomendado

- Asociar planes e intenciones a deployment, lease ID y fencing epoch.
- Recuperar solo filas cuyo lease haya expirado de forma verificable.
- Usar una transición CAS condicionada al propietario anterior.
- Añadir una prueba de dos hosts con un plan activo durante el reinicio del segundo.

### Verificación de cierre

- La prueba de dos epochs/deployments recupera exactamente el plan expirado del
  deployment actual y deja intactos el plan vigente, el foreign plan y el plan
  legacy sin evidencia multi-host.
- La prueba del ledger deja intactas las intenciones del deployment ajeno y del
  epoch actual, y transforma una sola intención antigua.
- Suite focalizada de recuperación, persistencia, multihost, executor y runtime:
  `157 passed`.
- La regresión completa posterior al lote terminó con `2295 passed, 18 skipped`.
  La puerta de composición terminó con arquitectura `4 kept, 0 broken`, `606
  passed, 18 skipped` y `Composition checks: PASS`.
- Ruff, mypy (`8 source files` en el lote), Import Linter (`4 kept, 0 broken`)
  y `git diff --check` quedaron verdes. La revisión transversal está en
  [`specs/211-scope-multi-host/composition-review.md`](../../specs/211-scope-multi-host/composition-review.md).

### Impacto y límites

- Un host no puede marcar trabajo de otro deployment ni trabajo con lease
  vigente/current epoch como huérfano.
- El executor conserva la identidad de lease para que un cleanup tardío no
  sobrescriba al ganador.
- Las filas legacy sin evidencia no se adivinan en multi-host; requieren una
  operación explícita antes de ser recuperadas.

## EVT-001 — Outbox sin reclamación exclusiva y sin worker de producción

**Severidad:** media-alta
**Estado:** cerrado — 2026-09-10
**Evidencia:** [`audit_outbox.py:27-104`](../../src/domoai/persistence/audit_outbox.py#L27), [`repositories.py:564-699`](../../src/domoai/persistence/repositories.py#L564), [`runtime_lifecycle.py:13-65`](../../src/domoai/application/runtime_lifecycle.py#L13), [`runtime_factory.py:629-840`](../../src/domoai/application/runtime_factory.py#L629)

El dispatcher reclama primero las filas pendientes o con lease expirado, usando
owner, lease ID, expiración y una actualización CAS. La finalización y el
reintento solo aceptan la identidad que ganó la reclamación. La entrega recibe
el `event_id` durable, que es la clave de idempotencia para el destino; el
semántica resultante es at-least-once ante un crash después de entregar y antes
del CAS.

`RuntimeComposition` crea el dispatcher sobre el lane de auditoría y
`RuntimeLifecycle` arranca `domoai-audit-outbox-dispatcher` junto al runtime y
lo cancela antes de cerrar el almacenamiento. El destino puede inyectarse; por
defecto se usa un publisher de logging que solo emite ID, tipo y sujeto, sin
payload ni credenciales. SQLite y PostgreSQL tienen los campos de lease y sus
índices equivalentes.

### Impacto

La reclamación concurrente ya no ejecuta dos callbacks activos para la misma
fila. Un destino externo todavía debe deduplicar por `event_id` si acepta el
evento antes de que el proceso muera, y el logger por defecto no sustituye un
archivo externo de auditoría de largo plazo.

### Verificación de cierre

- RED: cuatro fallos reproducían los contratos ausentes.
- Outbox/lifecycle/runtime/schema focalizada: `13 passed`; suite afectada:
  `59 passed`.
- Composition gate: `610 passed, 18 skipped`, arquitectura `4 kept, 0 broken`
  y `Composition checks: PASS`.
- Ruff, mypy (`5 source files`), Import Linter (`4 kept, 0 broken`) y
  `git diff --check` quedaron verdes. Revisión transversal:
  [`specs/212-claim-audit-outbox/composition-review.md`](../../specs/212-claim-audit-outbox/composition-review.md).
