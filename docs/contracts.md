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

El único servidor stdio local registra estas tools semánticas:

| Tool | Efecto |
| --- | --- |
| `discover_devices` | Lee o refresca el inventario canónico; admite `area_id`, tipos y `refresh`. |
| `get_state` | Lee estados acotados por dispositivos/capacidades. |
| `get_energy_context` | Lee un horizonte completo de tarifas, solar y batería opcional mediante un provider tipado. |
| `validate_command` | Valida un comando sin invocar el adapter. |
| `validate_plan` | Aplica capacidades, políticas, revisión y digest a un plan. |
| `request_approval` | Emite un `ApprovalGrant` de un solo uso, ligado al digest del plan. Único origen válido de una aprobación. |
| `execute_plan` | Ejecuta un plan validado. Si requiere confirmación, exige un `approval_id` emitido por `request_approval`; ya no acepta un objeto de aprobación construido por el caller. |
| `validate_scenario` | Valida un escenario de optimización contra dispositivos y capacidades canónicas. |
| `optimize_scenario` | Produce una propuesta determinista sin ejecutar comandos físicos. |
| `explain_solution` | Explica una propuesta tipada sin cambiar el estado del runtime. |

Resources de solo lectura:

```text
domotics://areas
domotics://capabilities
domotics://devices
domotics://energy
domotics://policies
```

## Runtime safety hardening (2026-08-18)

Cierra el trust boundary MCP-agente descrito en
`specs/021-runtime-safety-hardening/`:

- `PolicyEngine.evaluate` ya no confía en `Command.risk_class` del caller.
  Un `RiskClassifier` server-side clasifica `(device, capability, command)`
  de forma independiente; el riesgo efectivo es el máximo entre lo que
  clasifica el runtime y lo que envía el caller, así una política solo puede
  escalar el riesgo, nunca rebajarlo.
- `build_runtime()` carga `Settings.policy_config_path` (TOML,
  `load_policy_file`) en producción en vez de construir un `PolicyEngine([])`
  vacío; si no hay archivo configurado, se registra un evento de auditoría
  `policy_default_applied` en vez de arrancar en silencio sin políticas.
- `execute_plan` ya no acepta un objeto `approval` construido por el
  caller. `PlanService.approve()`/`DomoticsFacade.approve_plan()` requieren
  un `ApprovalGrant`, que solo puede emitir `ApprovalStore.issue()` a través
  de la nueva tool `request_approval`. El grant es de un solo uso y está
  ligado al `plan_id`/`validation_digest`.

Ver `specs/021-runtime-safety-hardening/contracts/mcp-tools.md` para el
contrato detallado de `request_approval`/`execute_plan`.

## Dependencias granulares de plan (2026-08-18)

Cierra `specs/022-granular-plan-dependencies/`: `PlanService` ya no invalida
un plan validado por un `runtime_revision` global único.

- `StateStore.runtime_revision` (inventory revision) ahora solo avanza
  cuando `DiscoveryService.refresh()` detecta un cambio real en el
  conjunto de dispositivos/capacidades/disponibilidad — no en cada pasada
  de discovery.
- `StateStore` trackea una versión por `(device_id, capability)`, que solo
  avanza cuando ese valor/status concreto cambia.
- `ValidationResult` gana un campo aditivo `dependencies: PlanDependencies
  | None`, con `inventory_revision`, `policy_revision` y los
  `state_versions` exactos que el plan usó (uno por comando resuelto).
- `PlanService.assert_executable()` compara solo esas dependencias
  concretas, no una cadena global. Un sensor no relacionado ya no invalida
  planes; un cambio real en lo que el plan depende sí sigue marcándolo
  `STALE_PLAN`.
- `ValidationResult.runtime_revision` mantiene su forma/significado
  anterior (compatibilidad con `EnergySkillWorkflow`); `dependencies` es
  aditivo, no lo reemplaza.

## Event pipeline incremental (2026-08-18)

Cierra `specs/023-incremental-event-pipeline/`: `RuntimeEventConsumer` ya no
hace `DiscoveryService.refresh()` completo por cada evento.

- Eventos `kind="state_changed"` (todos los adapters ya emiten este kind
  solo para entidades ya conocidas) toman un camino barato: leen valores
  actuales vía `AdapterPort.read_state()` sobre los `SourceRef` ya conocidos
  de ese adapter en el registry, sin re-descubrir identidad/capacidades.
- Cualquier otro kind (`availability_changed`, `device_membership_changed`,
  `metadata_changed`, `adapter_diagnostic`, o uno no reconocido) sigue
  disparando `discovery.refresh()` completo exactamente como antes — la
  ruta rápida es un allowlist, nunca un denylist, para no crear un punto
  ciego de detección de inventario.
- `read_state()` devuelve `device_id` como el external_id crudo del
  adapter, no el canonical id del registry; `_apply_state_only` lo remapea
  vía `registry.canonical_id_for_source(...)` antes de guardar, igual que
  ya hacía `PlanExecutor._readback`.
- No hay cambios en adapters, `SourceEvent` ni contrato MCP.

## Registry reconciliation (2026-08-18)

Cierra `specs/024-registry-reconciliation/`: `DeviceRegistry.apply_snapshot`
ya no es puramente aditivo.

- Tras aplicar un snapshot, se reconcilia: para cada adapter_id
  "autoritativo" esta ronda (no reportado como `failure` en
  `snapshot.unsupported_sources` — misma señal que ya usa
  `DiscoveryService.refresh()` para `mark_source_unavailable`), cualquier
  `SourceRef` conocido de ese adapter que no apareció en esta ronda se
  elimina (su ruta también); si un dispositivo se queda sin `source_refs`,
  se elimina del registry entero.
- Un adapter reportado como fallido/desconectado nunca pierde sus
  dispositivos por reconciliación — solo la marca de no disponible ya
  existente. Un dispositivo respaldado por dos adapters distintos sobrevive
  perdiendo solo uno.
- `self._identity_to_canonical` nunca se toca en la reconciliación, así que
  la misma identidad reaparecida más tarde recupera el mismo canonical ID.
- Sin nuevo estado de lifecycle en `Device`/`AvailabilityStatus`: un
  dispositivo reconciliado simplemente desaparece del registry.
- Se añadió `tests/unit/runtime/__init__.py` (faltaba) para que los tests de
  ese directorio puedan importar `tests.fixtures.*` de forma independiente.

## Persistencia de state/inventory (2026-08-18)

Cierra `specs/025-state-inventory-persistence/`: `devices` y
`state_snapshots` ya se leen/escriben en SQLite.

- Nuevo `DeviceRepository` (reutiliza la tabla `devices` ya en el allowlist
  de `SQLiteJsonRepository`) y `StateSnapshotRepository` (dedicado, PK
  compuesta `(device_id, capability)`), en `persistence/repositories.py`.
- `DiscoveryService.refresh()` persiste el inventario y estado actuales tras
  cada rediscovery exitosa (ya acotada a eventos de tipo inventario por
  P1.2/P1.3, no por cada `state_changed`), y **borra** de `devices`
  cualquier id que ya no esté en el registry — necesario para que un
  dispositivo reconciliado (Spec 024) no reaparezca tras un reinicio (bug
  real capturado por el propio test de esta feature: un simple upsert no
  bastaba).
- `build_runtime()` carga lo persistido ANTES de conectar cualquier
  adapter: `DeviceRegistry.load_persisted()` puebla solo `_devices` y
  `_source_entity_ids` (legible, pero sin rutas — `resolve_command_route`
  devuelve `route_not_found` hasta que una discovery real reconfirme el
  dispositivo esta sesión); `StateStore.load_persisted()` fuerza
  `status=STALE` en todo lo restaurado, sin importar qué se persistió.
- No se persiste telemetría histórica completa, solo el último valor por
  `(device_id, capability)` (mismo shape que la tabla existente). No hay
  migración nueva.

## Arranque degradado y reconexión (2026-08-18)

Cierra `specs/026-degraded-startup-reconnect/`: `build_runtime()` ya no
aborta el proceso si el adapter no conecta o la primera discovery falla.

- `build_runtime()` envuelve `connect()` + primer `discovery.refresh()` en
  un único `try/except (ConnectionError, OSError)`; si falla, audita
  `runtime_started_degraded` y sigue construyendo el resto de la
  composición — lo ya cargado desde persistencia (Spec 025) sigue legible.
- `RuntimeEventConsumer.run()` (el loop de reconexión real, lanzado por
  `run_stdio()`) ahora consulta `adapter.health()` en cada iteración; si no
  está conectado, llama `connect()` y luego `discovery.refresh()` antes de
  volver a `subscribe_events()` — nunca llama `connect()` de más mientras
  el adapter ya está sano (varios adapters no son seguros de reconectar dos
  veces sin motivo).
- El delay fijo de 1s pasa a backoff exponencial acotado (por defecto hasta
  60s), que se resetea a su valor inicial tras cada reconexión exitosa.
- `CompositeAdapter` no se modifica: su tolerancia a fallo parcial en
  `connect()`/`discover()` ya era correcta y se reutiliza tal cual.
- Sin cambios en cómo se rechaza un comando contra una fuente no conectada
  (`ConnectionError` → `ExecutionStatus.UNAVAILABLE` en `PlanExecutor`, ya
  existente).

## Corrección del optimizador energético (2026-08-18)

Cierra `specs/027-energy-optimizer-correctness/`: cuatro defectos
verificados en el solve path CP-SAT (`optimizer/cp_sat.py`).

- **Signo invertido en `maximize_solar_self_consumption`**: el coeficiente
  se aplicaba a `grid_export` con la polaridad equivocada — con la
  declaración esperada (`direction="maximize"`) el solver en realidad
  MAXIMIZABA la exportación en vez de minimizarla. Corregido con una
  negación adicional específica de este objetivo (comentario en el código
  explica por qué `grid_export` tiene polaridad inversa al nombre del
  objetivo). Probado con un escenario dorado (solar solo en un slot de
  dos) con única respuesta óptima posible.
- **Prioridad lexicográfica real**: `_optimize_energy` ya no combina todos
  los objetivos en una única suma ponderada. Ahora agrupa por `priority`,
  resuelve cada nivel en orden, y fija el valor alcanzado como cota antes
  de optimizar el siguiente nivel — un objetivo de mayor prioridad nunca
  se sacrifica por uno de menor prioridad. Objetivos con la misma
  `priority` se siguen combinando en un único nivel ponderado.
- **Soft constraints reales**: un constraint `hard=False` de un tipo
  soportado gana una variable de violación no-negativa, minimizada como el
  nivel de MÁS alta prioridad (antes que cualquier objetivo declarado por
  el usuario) — nunca bloquea la factibilidad. `constraint_summary["soft_violations"]`
  ya no es un `[]` hardcodeado: reporta `type`/`slot`/`amount` de cada
  violación real.
- **`EnergyContext.base_load_forecast`** (nuevo, aditivo, `None` por
  defecto = cero en todo el horizonte): consumo base de la casa,
  incorporado a la ecuación de balance y por tanto a todo constraint/coste
  que dependa de ella.
- **Diferido explícitamente, no implementado aquí**: constraint de SOC
  terminal de batería (evita que el optimizador vacíe la batería al final
  del horizonte por un ahorro marginal) — mismo audit original, epic
  separado del backlog.

Evidencia: `380 → 387 passed, 8 skipped`. Ruff y mypy limpios.

## Evidencia y reproducibilidad del solver (2026-08-18)

Cierra `specs/028-solver-evidence-reproducibility/`: `OptimizationResult`
ganó un campo aditivo `solver_evidence: SolverEvidence | None`
(`optimizer/ports.py`) — antes nada reportaba cómo se llegó a un resultado,
solo el resultado mismo.

- **`SolverEvidence`**: `solver_name` (`"cp-sat"`), `solver_version`
  (`ortools.__version__` real cargado en el proceso), `num_search_workers`/
  `random_seed` (leídos de `solver.parameters` tras configurarlo — hoy
  siempre `1`/`0`, sin cambios de comportamiento), `wall_time_seconds`
  (suma de `solver.WallTime()` de cada tier resuelto), `tiers: list[SolvedTier]`
  y `scenario_fingerprint`.
- **`SolvedTier`**: `priority` (la prioridad declarada del tier, o `None`
  para el tier implícito de violación de soft constraints, que no tiene
  prioridad de usuario), `terms` (nombres de objetivos, o tipos de
  constraint soft, contenidos en ese tier) y `achieved_value` (el valor
  entero crudo de CP-SAT que `_solve_tiers` ya usa para fijar la cota del
  tier — no reescalado a unidad física, ver `research.md` de la spec para
  el porqué).
- **`scenario_fingerprint`**: `sha256(scenario.model_dump_json())` del
  `OptimizationScenario` exacto resuelto — permite verificar
  criptográficamente que dos resultados vienen de la misma entrada antes de
  compararlos por reproducibilidad.
- `solver_evidence` es `None` para cualquier resultado que nunca completó un
  solve (escenario inválido, infactible o timeout) — nunca se fabrica
  evidencia de un cómputo que no ocurrió.
- Cambio puramente aditivo: `build_result(...)` recibe `solver_evidence`
  como keyword opcional (`None` por defecto), así que todo call site
  existente sigue funcionando sin cambios. El campo viaja automáticamente a
  través de MCP porque `OptimizationResult` ya se serializa entero.
- Probado con un test de reproducibilidad real: la misma escenario resuelta
  dos veces desde instancias de optimizador independientes produce `plan`,
  `objective_values`, `tiers` (con cada `achieved_value`) y
  `scenario_fingerprint` idénticos — `wall_time_seconds` queda
  explícitamente excluido de la comparación por ser reloj de pared legítimo.
- Dos tests de paridad MCP preexistentes (`test_ortools_mcp_parity.py`,
  `test_unified_mcp_compatibility.py`) comparaban resultados byte a byte
  entre dos clientes independientes y ya excluían `created_at`/
  `validated_at` por ser reloj de pared; se les añadió excluir también
  `solver_evidence.wall_time_seconds` por la misma razón — actualización
  legítima de un test que ya normalizaba campos de reloj de pared, no un
  fallo de diseño.
- **Diferido explícitamente, no implementado aquí** (mismo audit original,
  epic separado del backlog): persistir esta evidencia en un audit log
  durable (`AuditEventRepository`).

Evidencia: `387 → 394 passed, 8 skipped`. Ruff y mypy limpios.

## Conflictos de identidad cross-adapter auditados (2026-08-18)

Cierra `specs/029-cross-adapter-identity-conflicts/`. El enlace explícito
`canonical_id` entre adapters ya era correcto e intencional (documentado
arriba, en "Runtime multi-adapter": las claves estables `identifiers`/
`connections` solo mantienen identidad DENTRO de una fuente; `canonical_id`
explícito es el único mecanismo de enlace entre adapters — sin cambios en
esta spec). El hueco estaba en lo que pasaba DESPUÉS de un merge
cross-adapter exitoso:

- **`DeviceRegistry.diagnostics`** (`canonical_type_conflict`,
  `capability_metadata_conflict`, `source_entity_rejected`) se calculaba
  correctamente pero nadie lo leía nunca — invisible en la práctica y sin
  límite de crecimiento. Nuevo `DeviceRegistry.drain_diagnostics()`
  (pop-all) llamado por `DiscoveryService.refresh()` justo después de
  `apply_snapshot(...)`, auditando cada diagnóstico como
  `registry_identity_conflict` exactamente una vez por ciclo.
- **Conflicto de estado mismo-ciclo entre adapters**: `CompositeAdapter.discover()`
  concatena los `source_states` de todos los sub-adapters conectados;
  `DiscoveryService._record_states` los guardaba vía `StateStore.save()`
  solo por `(device_id, capability)` — si dos adapters fusionados por
  `canonical_id` reportaban la MISMA capability con valores distintos en el
  mismo ciclo, el segundo `save()` sobrescribía el primero en silencio, sin
  diagnóstico ni evento de auditoría, sin regla de desempate documentada —
  asimétrico con `resolve_command_route`, que ya detecta y bloquea esta
  misma situación (`ambiguous_route`) en el lado de escritura. Corregido:
  `_record_states` detecta, dentro de su propio bucle por ciclo, cuando más
  de una fuente distinta reporta un valor para el mismo
  `(device_id, capability)` y discrepan (misma igualdad `(value, status)`
  que `StateStore.save()` ya usa para decidir si versionar); audita
  `state_source_conflict` con ambas fuentes, ambos valores y cuál se
  retuvo, antes de dejar que el orden last-write-wins existente proceda sin
  cambios.
- Cambio puramente observacional: qué valor queda cacheado y qué ruta se
  ejecuta no cambian en absoluto — solo la visibilidad de los conflictos.
- **Hallazgo real durante la verificación**: el fixture compartido
  `tests/fixtures/multi_adapter.py` (reutilizado por specs 008+ desde hace
  tiempo) ya tenía un `canonical_type_conflict` latente e invisible dentro
  de un solo adapter — la entidad `light.main_brightness` deriva
  `semantic_type="sensor"` (no tiene capability "power") mientras
  `light.main_power` del mismo dispositivo compartido deriva `"light"` —
  quedó expuesto por primera vez al activar esta observabilidad; el test
  de "cero conflictos" de esta spec usa una combinación de fixture distinta
  para evitarlo, sin tocar el fixture compartido (fuera de alcance).
- Faltaba `tests/unit/application/__init__.py` (mismo gap de colección
  standalone que Spec 024 encontró en `tests/unit/runtime/`); añadido.

Evidencia: `394 → 401 passed, 8 skipped`. Ruff y mypy limpios.

## Preconditions aplicadas en ejecución (2026-08-18)

Cierra `specs/030-precondition-enforcement/`. `Precondition(device_id,
capability, expected)` y `Command.preconditions` ya estaban completamente
modelados desde antes, pero se leían en cero sitios de
`plan_service.py`/`executor.py` — TOCTOU real: un caller podía declarar
"solo ejecuta esto si battery.soc == 60" y no cambiaba absolutamente nada
sobre si/cuándo el comando se ejecutaba.

- `PlanExecutor.execute()` ahora evalúa, justo antes de llamar al adapter
  por cada comando (no al validar el plan, no una sola vez al principio —
  freshest read point posible, verificado leyendo la posición donde ya se
  capturaba `before_state`), cada `Precondition` contra el valor actual en
  `StateStore`. Satisfecha solo si existe un snapshot y su `value` iguala
  exactamente a `expected`; ausencia de estado se trata igual que
  desacuerdo (nunca se asume satisfecho).
- Si alguna precondition falla, el adapter NUNCA se llama para ese
  comando: se registra un `ExecutionOutcome` `REJECTED` con
  `error.code=precondition_failed`, `retryable=True` (el mundo puede
  cambiar y volver a satisfacerla) y todas las preconditions fallidas
  listadas — por el mismo camino (`outcome_repository`, audit) que
  cualquier otro outcome.
- Comandos dentro del mismo plan ven los efectos confirmados de comandos
  anteriores del mismo plan: verificado con un test de dos comandos donde
  el segundo depende del capability que el primero acaba de confirmar vía
  `_readback` (que ya guardaba en `StateStore` antes de esta spec).
- Un plan donde todos los comandos fallan sus preconditions completa sin
  excepción y sin ninguna llamada a adapter.
- Cambio puramente aditivo: comandos sin preconditions declaradas se
  comportan exactamente igual que antes (verificado, cero regresión en
  los tests preexistentes de `test_plan_lifecycle.py`).
- **Diferido explícitamente, no implementado aquí**: operadores de
  comparación/rango en preconditions (`battery.soc >= 50`) — solo
  igualdad exacta por ahora, epic separado.

Evidencia: `401 → 407 passed, 8 skipped`. Ruff y mypy limpios.

## Lifecycle de ejecución atómico y evidencia preservada (2026-08-18)

Cierra `specs/031-execution-lifecycle-atomicity/`. Dos huecos reales de
corrección de ejecución detectados por el segundo audit del usuario:

- **Claim no atómico**: `PlanExecutor.execute()` escribía `EXECUTING`
  sin comprobar antes el estado persistido del plan — una segunda
  llamada `execute()` (retry tras timeout, dos callers en carrera) podía
  reclamar y re-ejecutar un plan ya `EXECUTING` o ya terminal, reenviando
  todos los comandos a los adapters. Corregido: cuando hay
  `plan_repository` configurado, `execute()` lee el estado persistido
  ANTES de `assert_executable()`; si ya está `EXECUTING` o en cualquier
  estado terminal, se rechaza con `ErrorCode.INVALID_TRANSITION` sin
  llamar a ningún adapter. Verificado que un read-then-write sin `await`
  de I/O real entre medias ya es atómico para el modelo de concurrencia
  real de este runtime hoy (un proceso, una conexión SQLite compartida,
  `sqlite3` síncrono envuelto en `async def`) — locking multi-proceso
  real queda diferido a SQLite hardening (P2.6, aparte).
- **`execution_outcomes` destruía evidencia previa en un retry**:
  `ExecutionOutcomeRepository.save()` hacía `ON CONFLICT DO UPDATE` —
  un segundo save del mismo `(plan_id, command_id)` sobrescribía en
  silencio la evidencia del primer intento. Corregido de forma
  puramente aditiva: nueva tabla `execution_attempts` (INSERT-only,
  `attempt_id` autoincrement) que `save()` también escribe en cada
  llamada, sin tocar `execution_outcomes` ni ningún lector existente.
  Nuevo `list_attempts_for_plan(plan_id)` devuelve el historial completo
  en orden; `list_for_plan(plan_id)` sigue devolviendo solo el más
  reciente, sin cambios.
- Cambio de infraestructura mínimo necesario: `SQLiteDatabase.initialize()`
  pasó de ejecutar un único `SCHEMA_PATH` hardcodeado a ejecutar cada
  `*.sql` en `migrations/` en orden alfabético — sin tabla de versiones
  ni framework de migraciones (eso sigue siendo P2.2, diferido); solo lo
  mínimo para que pueda existir un segundo archivo de migración aditivo
  (`CREATE TABLE IF NOT EXISTS`).
- Cambio puramente aditivo: sin `plan_repository` configurado, o para la
  primera ejecución de un plan, el comportamiento es idéntico a antes.
- **Diferido explícitamente, no implementado aquí**: persistencia de
  idempotency keys por-adapter a través de reinicios (hoy solo en
  memoria en los 6 adapters — confirmado por grep) — el claim atómico ya
  cierra el disparador más probable en la práctica (una llamada
  `execute()` duplicada) antes de que llegue a un adapter una segunda
  vez; locking SQLite multi-proceso real (P2.6).

Evidencia: `407 → 413 passed, 8 skipped`. Ruff y mypy limpios.

## Scheduling temporal de planes (2026-08-18)

Cierra `specs/032-plan-scheduling/`, el tercer y último P0 restante del
segundo audit del usuario. CP-SAT elegía el slot correcto (Spec 027) pero
el Plan resultante no tenía semántica temporal — `execute_plan` ejecutaba
ya, no en el slot elegido.

- **`Plan.execute_at: datetime | None`** (aditivo). `PlanExecutor.execute()`
  gana un guard, el PRIMERO de todos (antes incluso del claim atómico de
  la Spec 031): si `execute_at` está en el futuro, rechaza con
  `ErrorCode.NOT_YET_DUE` sin tocar ningún adapter. Sin `execute_at`,
  comportamiento idéntico a siempre.
- **Splitting multi-slot real, no shortcut con pérdida de información**:
  cuando una optimización asigna loads a slots distintos, `_proposal_plan`
  los agrupa por su `execute_at` calculado y emite un `Plan` por cada
  tiempo distinto (cada uno internamente consistente — nunca mezcla
  comandos de tiempos distintos). Nuevo `OptimizationResult.plans: list[Plan]`;
  `.plan` se mantiene como el primero (más temprano) para compatibilidad
  total con todo caller existente de un único load.
- **`Scheduler` persistente** (`src/domoai/runtime/scheduler.py`, nueva
  tabla `scheduled_plans` INSERT/UPDATE por fila, no historial —
  distinto de `execution_attempts` de la Spec 031): mantiene planes
  aprobados esperando su hora, sobrevive a un reinicio (verificado con un
  test que reconstruye repository/scheduler contra el mismo fichero
  SQLite), y aplica una ventana de gracia acotada — un plan vencido más
  allá de la ventana se marca `missed` y se audita (`schedule_missed`),
  NUNCA se ejecuta tarde en silencio ("nunca ejecutar todo lo perdido
  automáticamente", advertencia explícita del audit). Un sweep completo
  usa un único `now` consistente para todas las filas.
- La ejecución real, cuando llega la hora, pasa siempre por el mismo
  `PlanExecutor.execute()` sin modificar — policy, aprobación, claim
  atómico (Spec 031), preconditions (Spec 030) e idempotencia se aplican
  exactamente igual que a una ejecución inmediata. El Scheduler nunca
  llama a un adapter directamente.
- Nuevas tools MCP: `schedule_plan`, `cancel_scheduled_plan`,
  `reschedule_plan`, `list_scheduled_plans` (mismo patrón de
  digest/aprobación que `execute_plan`). `DomoticsMcpContext.scheduler`
  es opcional (`None` en despliegues sin persistencia, p. ej. el fixture
  server). El loop de fondo (`Scheduler.run()`) sigue el mismo patrón ya
  establecido por `RuntimeEventConsumer.run()` (Spec 023/026), arrancado
  en `run_stdio()`.
- **Fallout real detectado y corregido**: tres tests preexistentes
  hardcodeaban la lista exacta de tools MCP (`test_domotics_mcp_contract.py`,
  `test_unified_mcp_contract.py`, `test_home_assistant_provider_runtime.py`)
  — actualizados con las 4 tools nuevas. Dos tests de paridad MCP
  (`test_ortools_mcp_parity.py`, `test_unified_mcp_compatibility.py`) ya
  normalizaban `created_at`/`wall_time_seconds` antes de comparar dos
  clientes byte a byte; se les añadió normalizar también
  `plans[].created_at`/`plans[].validation.validated_at` por la misma
  razón (reloj de pared).
- **Deuda técnica cerrada de paso**: `schemas/v1/` llevaba desde las
  Specs 027/028 sin regenerar (`base_load_forecast`, `solver_evidence`
  nunca se habían reflejado); `scripts/export_schemas.py` ejecutado,
  diff puramente aditivo (328 líneas, 5 archivos), sin romper nada.
- **Diferido explícitamente, no implementado aquí** (backlog P3.1,
  "scheduler avanzado"): horarios recurrentes, política DST más allá de
  usar timestamps timezone-aware, scheduling más fino que por-Plan
  (per-Command), y conectar el state machine de la skill energética
  portable al nuevo camino de scheduling (sigue usando `execute_plan`
  inmediato hoy; el scheduling lo inicia el caller/host que elija usar
  las tools nuevas).

Evidencia: `413 → 433 passed, 8 skipped`. Ruff y mypy limpios.

## Pipeline de CI en GitHub (2026-08-18)

Cierra `specs/033-ci-pipeline/`, el primer ítem de P2 (production
hardening). No existía `.github/workflows/` en absoluto — todos los
gates de calidad (ruff, mypy strict, suite completa, `uv.lock`) se
verificaban solo a mano, cada vez, por el propio agente.

- Nuevo `.github/workflows/ci.yml`, disparado en `pull_request` y
  `push` a `main`, con 9 jobs INDEPENDIENTES (sin `needs:` entre ellos,
  para que cada categoría de fallo sea atribuible por separado en la
  lista de checks del PR): `lock-check` (`uv lock --check`), `lint`
  (`ruff check .`), `typecheck` (`mypy src`), cuatro jobs de test que
  reutilizan el split ya existente `tests/{unit,contract,integration,performance}`
  (sin inventar una categorización nueva), `package` (`uv build` +
  import de los dos entrypoints `domoai-mcp`/`domoai-lab`), y
  `schema-check` (`export_schemas.py` + `git diff --exit-code
  schemas/v1/` — automatiza literalmente la misma secuencia manual que
  esta sesión usó para detectar y corregir el drift de las Specs
  027/028 en la Spec 032).
- Reporte de cobertura (`pytest-cov`, ya instalado) en el job
  `test-unit`, subido como artifact — sin umbral numérico obligatorio en
  este incremento: un umbral es una decisión de política que le
  corresponde al dueño del repo, no algo que inventar en silencio.
- Verificado localmente que la lógica de `schema-check` (regenerar +
  diff) es idempotente: dos ejecuciones consecutivas de
  `export_schemas.py` producen cero diferencias entre sí — el diff
  contra `HEAD` que se observa ahora mismo es solo ruido de que esta
  sesión entera sigue sin ningún commit (`30a43bc` sigue siendo HEAD),
  no un fallo del mecanismo.
- **Incidente real durante la verificación, corregido en el momento**:
  al simular deliberadamente una violación de ruff para probar el
  aislamiento de checks, se usó `git checkout -- src/domoai/domain/errors.py`
  para revertir — pero como NINGÚN archivo de esta sesión (021-032)
  estaba comprometido a git todavía, ese comando borró trabajo real no
  comprometido (los `ErrorCode.PRECONDITION_FAILED`/`NOT_YET_DUE` de las
  Specs 030/031). Detectado inmediatamente por mypy (`ErrorCode` sin
  esos atributos) y corregido restaurando las dos líneas exactas antes
  de continuar; verificado con la suite completa (`433 passed, 8
  skipped`) que no quedó ningún daño. Lección aplicada de inmediato para
  el resto de la verificación: nunca más `git checkout --` sobre
  archivos con cambios no comprometidos de la sesión; usar `Edit` para
  simular y deshacer roturas deliberadas.
- **Diferido explícitamente, no implementado aquí** (fuera del alcance
  de un cambio de código): configurar branch protection en GitHub para
  exigir estos checks antes de mergear — es un ajuste administrativo del
  repositorio que solo un admin puede aplicar desde la UI/API de
  GitHub, no algo que un workflow YAML pueda activar por sí mismo.
  Escaneo de vulnerabilidades/secretos y SBOM (P2.9, aparte). Umbral
  numérico de cobertura (decisión de política pendiente).

Evidencia: `uv lock --check`, `ruff check .`, `mypy src` y las cuatro
suites de test verificadas localmente comando por comando, coincidiendo
exactamente con cada job — `433 passed, 8 skipped` total, sin cambios de
comportamiento (esta spec no modifica ningún archivo fuente, solo añade
`.github/workflows/ci.yml`).

## Tracking real de migraciones de esquema (2026-08-18)

Cierra `specs/034-schema-migration-tracking/`, segundo ítem de P2.
`SQLiteDatabase.initialize()` ejecutaba TODOS los ficheros de migración
en CADA arranque, sin ningún ledger de qué ya se había aplicado —
funcionaba hoy solo por accidente, porque las tres migraciones
existentes eran `CREATE TABLE IF NOT EXISTS` (tolera re-ejecución). El
primer `ALTER TABLE`/backfill real habría roto en el segundo arranque
tras añadirse.

- Nueva tabla `schema_migrations (filename TEXT PRIMARY KEY, applied_at
  TEXT NOT NULL)`, añadida a `001_initial.sql` (única ubicación posible
  sin problema del huevo-y-la-gallina: tiene que existir antes de poder
  consultarse a sí misma).
- `SQLiteDatabase.initialize()`: bootstrap incondicional e idempotente
  de `schema_migrations`, lee qué ficheros ya están aplicados, y solo
  ejecuta + registra los que faltan, en orden, uno a uno (commit tras
  cada fichero, no una transacción gigante) — una migración se aplica
  exactamente una vez, para siempre, por fichero de base de datos.
- Nuevo parámetro `migrations_dir` opcional (por defecto el directorio
  real) — permite testear la lógica real contra un directorio temporal
  sin fabricar un cambio de esquema falso en producción.
- Probado con datos reales: una base con solo `001_initial.sql`
  registrado (simulando un despliegue antiguo), con un `Plan` insertado
  vía `PlanRepository`, reabierta con el set de migraciones completo —
  el `Plan` original sigue íntegro Y las tablas de migraciones
  posteriores (`scheduled_plans`) ya son usables.
- Probado con una sentencia genuinamente no-idempotente: directorio de
  migraciones temporal con un `ALTER TABLE ... ADD COLUMN` real —
  inicializar la misma base dos veces seguidas no falla ni duplica la
  columna. Este es el caso concreto que el mecanismo anterior no podía
  sobrevivir.
- Sin renumerar `002_execution_attempts.sql`/`004_scheduled_plans.sql`
  (el hueco en 003 se mantiene — renumerar migraciones ya "aplicadas en
  la práctica" sería exactamente el tipo de drift sin versionar que
  esta spec existe para evitar).
- **Diferido explícitamente, no implementado aquí**: mecanismo de
  rollback/down-migration (sin necesidad expresada para el modelo
  local-first de un solo desarrollador); documento de política de
  compatibilidad semver para `schemas/v1/`; librería de migraciones
  externa (sigue siendo `sqlite3` stdlib puro).

Evidencia: `433 → 438 passed, 8 skipped`. Ruff y mypy limpios.

Las respuestas estructuradas incluyen `schema_version` y, cuando procede,
`runtime_revision`. Las operaciones de mutación pasan por la frontera de plan
y política; no existe una tool MCP por fabricante.

## TLS en el transporte MQTT (2026-08-18)

Cierra `specs/035-mqtt-tls/`, tercer ítem de P2. `AiomqttTransport`
(`src/domoai/adapters/zigbee2mqtt/transport.py`) construía su
`aiomqtt.Client` sin ningún parámetro TLS — toda conexión MQTT era
en claro, credenciales incluidas. `runtime_factory.py` rechazaba
`mqtts://` explícitamente con un `ValueError`. La librería `aiomqtt`
2.5.1 ya exponía `tls_context`/`tls_params`/`tls_insecure` en su
`Client.__init__` — solo faltaba la conexión desde el lado de DomoAI.

- `AiomqttTransport` gana `tls`, `ca_cert_path`, `client_cert_path`,
  `client_key_path`, `tls_insecure` — `connect()` construye un
  `ssl.SSLContext` vía `_build_tls_context()` solo cuando `tls=True`
  (CA propia opcional → certificado de cliente opcional para mTLS →
  override inseguro aplicado al final) y lo pasa como `tls_context=` a
  `aiomqtt.Client`. Con `tls=False` (por defecto) el comportamiento
  es idéntico al anterior.
- `Settings` gana `mqtt_ca_cert_path`, `mqtt_client_cert_path`,
  `mqtt_client_key_path`, `mqtt_tls_insecure` (nuevas env vars
  `DOMOAI_MQTT_CA_CERT_PATH`, `DOMOAI_MQTT_CLIENT_CERT_PATH`,
  `DOMOAI_MQTT_CLIENT_KEY_PATH`, `DOMOAI_MQTT_TLS_INSECURE`) —
  certificado de cliente y clave deben configurarse juntos, mismo
  patrón de validación por tupla que ya existía para KNX/Modbus y para
  `mqtt_password`/`mqtt_username`.
- `runtime_factory.py`: la rama `mqtts://` ya no lanza — puerto por
  defecto 8883 (distinto del 1883 de `mqtt://`), TLS habilitado, los
  cuatro settings nuevos se pasan al transporte.
- `mqtt_tls_insecure` es **opt-in, por defecto `False`** — desactivarlo
  deshabilita verificación de certificado y de hostname
  (`check_hostname=False`, `verify_mode=ssl.CERT_NONE`); probado que el
  valor por defecto deja la verificación activa y que el flag, cuando
  se activa, gana incluso si hay una CA propia configurada (combinación
  válida para laboratorios locales, no un error).
- **Fuera de alcance, documentado en el spec**: TLS para Modbus TCP y
  KNX/IP (sin variante TLS de uso común / KNX Data Secure es un
  mecanismo distinto, no TLS); Home Assistant y Matter Server ya llevan
  TLS a nivel de esquema de URL (`https://`/`wss://`) vía sus propias
  librerías cliente, sin hueco equivalente que cerrar; gestión de ciclo
  de vida de certificados (rotación/renovación).

Evidencia: `438 → 451 passed, 8 skipped`. Ruff y mypy limpios.

## Consulta del audit trail (2026-08-18)

Cierra `specs/036-audit-trail-query/`, cuarto ítem de P2. DomoAI ya
registraba un audit trail rico, persistente y con secretos redactados
(`AuditEventRepository.append_event()`, sink de `AuditLog` para cada
servicio del runtime) — pero `list_all()`, el único método de lectura
existente, no tenía ningún caller fuera de su propia clase (`grep -rn
"list_all" src/domoai` restringido a audit no encontró ningún sitio de
llamada real). El audit trail era de solo escritura en la práctica:
nada exponía "qué pasó" sin abrir el fichero SQLite directamente con un
cliente SQL crudo.

- Nuevo `AuditEventRepository.list_events(*, event_type=None,
  subject_id=None, since=None, limit=100)` — filtrado y acotado en la
  capa SQL (`WHERE`/`LIMIT`, no fetch-y-descarta en Python), orden más
  reciente primero (`ORDER BY created_at DESC, id DESC`), límite
  siempre acotado a `min(limit, 500)` sin importar lo que pida el
  caller. `list_all()` queda intacto (lo siguen usando cuatro tests de
  integración existentes como lector de historial completo).
- Nueva tool MCP de solo lectura `list_audit_events`
  (`readOnlyHint=True, destructiveHint=False`, mismo patrón que
  `discover_devices`/`get_state`) con parámetros opcionales
  `event_type`, `subject_id`, `since`, `limit`.
- `DomoticsMcpContext` gana `audit_repository: AuditEventRepository |
  None = None` (mismo patrón que `energy_context_provider`/`scheduler`)
  — `None` en el path fixture en memoria sin persistencia, poblado
  desde `runtime.audit_repository` en `build_configured_server()`. Sin
  repositorio configurado, la tool devuelve un `error_envelope`
  claramente distinto de una respuesta vacía, nunca un crash.
- Probado que el filtrado por `event_type`/`subject_id` (individual y
  combinado) realmente reduce resultados, que `since` excluye eventos
  en o antes del timestamp dado, que el límite por defecto es 100 y el
  tope duro de 500 se respeta incluso pidiendo un límite absurdamente
  alto, y el path `None` de la tool MCP.
- **Fuera de alcance, con justificación explícita**: logging estructurado
  a stdout/stderr (segundo hueco real, verificado — cero usos de
  `import logging` en `src/domoai` — pero es una preocupación distinta,
  telemetría en vivo del proceso, no historial retrospectivo
  consultable); métricas/tracing (Prometheus/OpenTelemetry, sin
  requisito nombrado); política de retención/poda del audit trail (real
  pero ortogonal a "poder leer lo que ya existe", sin problema
  observado todavía en el modelo local-first de un solo desarrollador).

Evidencia: `451 → 462 passed, 8 skipped`. Ruff y mypy limpios.

## Backpressure de eventos en CompositeAdapter (2026-08-19)

Cierra `specs/037-composite-adapter-backpressure/`, quinto ítem de P2.
`CompositeAdapter._event_stream()` (`src/domoai/runtime/composite_adapter.py`)
fusiona los streams de eventos de todos los adapters conectados en una
única `asyncio.Queue()` compartida construida SIN `maxsize` — un event
storm de cualquier fuente conectada (un mesh Zigbee reconectando, un
broker MQTT reproduciendo un backlog grande de mensajes retained, un
dispositivo mal comportado) hacía crecer la cola sin límite, consumiendo
memoria indefinidamente. Cero cobertura de test existía sobre
`_event_stream()`/`subscribe_events()` de `CompositeAdapter` bajo
cualquier condición de carga.

- Nuevo parámetro `CompositeAdapter(..., event_queue_max_size=1000)` —
  se convierte en el `maxsize` de la cola interna (antes ilimitada).
  Nuevo `Settings.composite_event_queue_max_size` (env var
  `DOMOAI_COMPOSITE_EVENT_QUEUE_MAX_SIZE`), enhebrado en el único punto
  de construcción de `CompositeAdapter` en `create_adapter()`.
- Cada tarea `pump(adapter)` usa `queue.put_nowait(...)` en vez de
  `await queue.put(...)`; en `asyncio.QueueFull` el evento se
  DESCARTA (nunca se bloquea, nunca se reintenta) y el drop se registra
  en la lista `self._diagnostics` ya existente, con la misma forma que
  `_record_failure()` (`event_type`, `adapter_id`, `message`).
  Deliberadamente NO se emite un nuevo `SourceEvent` por cada drop —
  `RuntimeEventConsumer._apply_event()` ya trata cualquier kind
  distinto de `state_changed` como disparador de un `discovery.refresh()`
  completo, así que emitir un diagnóstico por cada drop durante un storm
  crearía un bucle de retroalimentación real (más diagnósticos → más
  refreshes caros → drenado más lento → más drops).
- El drop es no-bloqueante y por-`put` individual (no una cola aparte
  por adapter): un adapter que inunda la cola compartida absorbe la
  mayoría de sus propios drops por simple probabilidad (intenta muchos
  más `put`s que uno bien comportado), sin necesitar sub-colas
  aisladas ni scheduling de fairness — probado explícitamente que un
  segundo adapter bien comportado NO queda completamente silenciado
  durante el flood de otro (al menos algunos de sus eventos llegan),
  sin reclamar fairness perfecto.
- Probado: tráfico normal por debajo del umbral se entrega íntegro, sin
  drops, sin cambio de comportamiento; un burst mayor al umbral
  configurado resulta en como mucho `event_queue_max_size` eventos
  entregados de ese burst, el resto descartado y registrado
  individualmente por `adapter_id`; drops de dos adapters simultáneos
  se registran distinguibles entre sí; ningún `SourceEvent` con
  `kind="adapter_diagnostic"` se emite como consecuencia de un drop; el
  umbral es honrado end-to-end desde `Settings` hasta
  `create_adapter()`.
- **Fuera de alcance, con justificación explícita en el spec**:
  sub-colas por-adapter o fairness scheduling ponderado (diseño
  materialmente mayor al que exige el hueco verificado — crecimiento
  de memoria sin límite); conectar `CompositeAdapter.diagnostics` a una
  superficie de lectura (MCP tool, audit log) — hueco real,
  pre-existente, de la misma forma que el que cerró la Spec 036 para el
  audit trail, pero pertenece naturalmente a P2.7 (health model
  unificado), no a este fix; backpressure en el path de un solo adapter
  (sin `CompositeAdapter` de por medio) — ya tiene backpressure natural
  vía `await` directo, sin cola intermedia que desbordar; rate-limiting
  o desconexión automática de una fuente persistentemente ruidosa —
  decisión de política más grande, no requerida para cerrar el hueco
  concreto de esta spec.

Evidencia: `462 → 470 passed, 8 skipped`. Ruff y mypy limpios.

## SQLite hardening (2026-08-19)

Cierra `specs/038-sqlite-hardening/`, sexto ítem de P2.
`SQLiteDatabase.initialize()` conectaba sin ningún `PRAGMA` — sin WAL,
sin `busy_timeout` explícito. `ExecutionOutcomeRepository.save()`
(única escritura multi-statement de todo `repositories.py` — el resto
hace un `execute()` por `commit()`, atómico por construcción) nunca
llamaba a `rollback()` si el segundo `execute()` fallaba, dejando la
conexión compartida y de larga vida con una escritura a medias sin
confirmar.

- `SQLiteDatabase.initialize()` ejecuta `PRAGMA journal_mode=WAL` y
  `PRAGMA busy_timeout={ms}` justo tras conectar, antes de las
  migraciones. Nuevo `SQLiteDatabase(path, busy_timeout_ms=5000)` —
  default explícito que preserva el comportamiento de facto actual
  (Python ya defaulteaba `timeout=5.0s` en `sqlite3.connect()`, pero
  como efecto secundario no documentado, no como valor deliberado).
  Nuevo `Settings.sqlite_busy_timeout_ms` (env var
  `DOMOAI_SQLITE_BUSY_TIMEOUT_MS`), enhebrado en el único punto de
  construcción de `SQLiteDatabase` en `build_runtime()`.
- `ExecutionOutcomeRepository.save()`: el segundo `execute()`
  (`execution_attempts`) envuelto en `try/except sqlite3.Error:
  rollback(); raise` — deshace también el primer insert
  (`execution_outcomes`), re-lanza la excepción original sin cambiar
  su tipo, deja la conexión limpia para la siguiente operación.
- Probado: `PRAGMA journal_mode` reporta `"wal"` tras `initialize()`;
  `PRAGMA busy_timeout` reporta el default (5000) y un valor custom
  configurado; el fallo del segundo insert deja CERO filas de ambas
  tablas (no solo la segunda); la excepción sigue siendo
  `sqlite3.IntegrityError`; una `save()` posterior no relacionada
  funciona con normalidad tras el fallo; el caso exitoso inserta ambas
  filas igual que antes.
- Técnica de test: `sqlite3.Connection` es un tipo C inmutable —
  `monkeypatch.setattr(sqlite3.Connection, "execute", ...)` falla con
  `TypeError: cannot set 'execute' attribute of immutable type`.
  Los tests intercambian `SQLiteDatabase._connection` por un proxy
  delegante pequeño que falla solo el `execute()` dirigido a
  `execution_attempts`.
- **Fuera de alcance, con justificación explícita**: convertir otros
  métodos de repositorio a transacciones explícitas (todos los demás
  ya son atómicos por construcción, un solo `execute()` por
  `commit()`); un context manager genérico `with database.transaction():`
  (infraestructura especulativa para una segunda escritura
  multi-statement que no existe todavía); benchmarking de WAL o
  stress-testing multi-proceso (fuera de escala local-first); cambiar
  la arquitectura de conexión única de larga vida (cambio
  arquitectónico mayor no requerido por los huecos concretos
  verificados).

Evidencia: `470 → 479 passed, 8 skipped`. Ruff y mypy limpios.

## Health model unificado por-adapter (2026-08-19)

Cierra `specs/039-unified-adapter-health/`, séptimo ítem de P2, y el
límite documentado y diferido explícitamente en la Spec 026 ("Límite
documentado: `CompositeAdapter.health()` reporta conectado con
cualquier sub-adapter vivo, así que una caída parcial del composite no
dispara este reconnect — health model unificado por-adapter es P2.7,
aparte"). `CompositeAdapter.health()` colapsaba el health individual de
cada sub-adapter conectado en un único booleano agregado `connected =
any(...)` — verdadero si AL MENOS UN sub-adapter está vivo, sin
importar cuántos otros estén caídos. Dos consecuencias reales
verificadas: (1) sin visibilidad de CUÁL adapter concreto está caído
en un despliegue multi-adapter (cero referencias a `AdapterHealth` bajo
`src/domoai/mcp`); (2) un hueco funcional real —
`RuntimeEventConsumer.run()` solo intenta `connect()` cuando
`health.connected` es falso; con la semántica "al menos uno vivo", un
despliegue con 1 de N adapters caído reporta `connected=True` para
siempre mientras quede otro vivo, así que el bucle de reconexión nunca
se dispara para ese adapter muerto — se queda desconectado en silencio
y para siempre hasta que todos los demás también mueran o el proceso
se reinicie.

- `AdapterHealth` (`src/domoai/domain/models.py`) gana
  `components: list[AdapterHealth] | None = None` — aditivo, `None`
  para cada implementación `health()` de un solo adapter (ninguna de
  las seis cambia). `CompositeAdapter.health()` ya calculaba el
  `AdapterHealth` de cada sub-adapter vía `asyncio.gather()` y lo
  descartaba tras reducirlo a dos escalares — ahora incluye esa lista
  ya calculada como `components`, sin gather adicional, sin I/O nuevo.
- `connected`/`message` de `CompositeAdapter.health()` mantienen su
  cálculo y significado agregado exactos de hoy ("al menos uno vivo")
  — sin redefinir semántica existente, deliberadamente, para no
  ambigüar el otro lector existente de `.connected`
  (`adapters/sdk/conformance.py`).
- `RuntimeEventConsumer.run()`: condición de reconexión extendida de
  `if not health.connected` a `degraded = not health.connected or
  (health.components is not None and any(not c.connected for c in
  health.components)); if degraded:`. Para un adapter único,
  `components` siempre es `None`, así que la cláusula añadida es
  siempre falsa y la condición se reduce exactamente a la de hoy —
  cambio de comportamiento cero para el caso no-composite, por
  construcción, no solo por test. Reconectar sigue significando
  `self.adapter.connect()`, que para `CompositeAdapter` ya reintenta
  TODOS los sub-adapters vía su `asyncio.gather()` existente, sin
  cambios — mismo mecanismo ya aceptado por el diseño de reconexión de
  la Spec 026 para el caso de fallo total.
- Probado: con todos los sub-adapters sanos, `components` los lista
  todos conectados; con uno caído de varios, `connected` agregado
  sigue en `True` (semántica sin cambios) mientras `components`
  identifica el `adapter_id` concreto caído; con dos caídos
  simultáneamente, ambos identificables individualmente, no fusionados
  en una sola señal; `RuntimeEventConsumer.run()` ahora SÍ llama a
  `connect()` ante un fallo parcial (la regresión exacta nombrada en
  la nota de la Spec 026); NO llama a `connect()` cuando todos los
  componentes están sanos (sin reconexión espuria); los doce tests
  preexistentes de `test_runtime_event_consumer.py` (añadidos en la
  Spec 026 y desde entonces) siguen pasando sin modificar — prueba de
  regresión cero para el caso de un solo adapter.
- **Fuera de alcance, con justificación explícita en el spec**:
  exponer el health por-adapter vía una tool MCP (mismo hueco real que
  la Spec 036 cerró para el audit trail, pero pertenece a una spec
  separada, no bundleada aquí); redefinir qué significa `connected` en
  el composite (p.ej. a "todos vivos") — rechazado explícitamente para
  no ambigüar el otro lector existente; reconexión selectiva por-adapter
  (reintentar solo el sub-adapter concreto caído en vez de todo el
  composite) — `connect()` del composite ya reintenta todos
  incondicionalmente, comportamiento existente ya aceptado por el
  diseño de la Spec 026; una abstracción de "modelo de salud unificado"
  más amplia (historial, métricas, alerting) — sin requisito nombrado
  por ninguna auditoría.

Evidencia: `479 → 484 passed, 8 skipped`. Ruff y mypy limpios.

## Failure injection sistemático (2026-08-19)

Cierra `specs/040-systematic-failure-injection/`, octavo ítem de P2.
`PlanExecutor.execute()` (`src/domoai/runtime/executor.py`) era el
ÚNICO sitio en todo el código que capturaba `ConnectionError` a secas
— verificado con `grep -rn "except (ConnectionError" src/domoai`
(cada adapter, `composite_adapter.py`, `event_consumer.py`,
`runtime_factory.py` capturan consistentemente la tupla
`(ConnectionError, OSError, TimeoutError)`) contra `grep -rn "except
ConnectionError" src/domoai` (solo dos sitios, ambos en
`executor.py`). Bug real y severo, no solo inconsistencia de estilo:
`PlanExecutor.execute()` persiste `status=EXECUTING` ANTES del bucle
de comandos y solo persiste un status terminal DESPUÉS de que el
bucle completo termine sin excepción sin capturar. Un `OSError` o
`TimeoutError` desde un adapter (ambos, fallos reales que CADA
implementación de adapter real ya lanza desde su propia capa de
transporte) durante `execute()` o el `read_state()` del readback no
era capturado — se propagaba sin capturar, saltándose tanto el
registro del outcome del comando como el guardado final de status
terminal. Combinado con el guard atómico de reclamación de la Spec
031 (`EXECUTING` está en `_NON_CLAIMABLE_STATUSES`), un solo
`OSError`/`TimeoutError` dejaba ese plan atascado en `EXECUTING` para
siempre — sin outcome registrado, sin evento de auditoría
`plan_execution_completed`, con una excepción sin capturar propagando
hacia quien haya llamado a `execute()`. Confirmado como no testeado en
absoluto: `grep -rn "TimeoutError\|OSError"
tests/integration/test_plan_lifecycle.py` devolvía cero resultados —
el hueco era invisible porque cada test de failure-injection existente
usaba `ConnectionError` específicamente para simular "adapter caído",
nunca los otros dos tipos que los adapters reales realmente lanzan.

- Ambos sitios `except ConnectionError` en `executor.py` ampliados a
  `except (ConnectionError, OSError, TimeoutError)` — sin ningún otro
  cambio de lógica, solo alinea con el patrón ya establecido en todo
  el resto del código.
- Nuevo `FailureInjectingAdapter`
  (`tests/fixtures/failure_injection.py`) — implementación completa de
  `AdapterPort` (los siete métodos), construible con
  `fail: dict[str, BaseException]` para inyectar cualquier excepción
  en cualquier método concreto; cada método no configurado tiene éxito
  trivial por defecto. Reemplaza la necesidad de escribir una clase de
  adapter-que-falla ad-hoc por cada test nuevo (patrón disperso
  existente en `test_multi_adapter_runtime.py`,
  `test_runtime_event_consumer.py`, `test_composite_adapter_health.py`).
- Corrección real durante la implementación, documentada
  explícitamente en `tasks.md`: la garantía original del spec
  ("el plan queda reintentable") era imprecisa —
  `PlanStatus.UNKNOWN` YA estaba deliberadamente en
  `_NON_CLAIMABLE_STATUSES` desde la Spec 031 (un outcome incierto no
  debe reintentarse automáticamente, para no arriesgar una doble
  actuación sobre un dispositivo) — así que ni siquiera `ConnectionError`
  permite reintento automático por el mismo `plan_id` hoy. La garantía
  real y precisa que este fix entrega: `OSError`/`TimeoutError` ahora
  alcanzan el MISMO estado terminal bien definido (`UNKNOWN`, con
  outcome registrado y evento de auditoría) que `ConnectionError` ya
  alcanza — no un estado nuevo de "atascado en EXECUTING para siempre
  sin ningún outcome registrado y una excepción sin capturar".
- Probado: `OSError`/`TimeoutError` desde `execute()` y desde el
  `read_state()` del readback ya no dejan el plan atascado en
  `EXECUTING`; el outcome se registra correctamente
  (`UNAVAILABLE`/`UNKNOWN` según el punto de fallo, código
  `adapter_unavailable`); `ConnectionError` produce exactamente el
  mismo estado terminal `PlanStatus.UNKNOWN` (test de regresión); el
  fixture solo falla el método configurado, no todos. Verificación
  empírica adicional: se revirtió temporalmente el fix, se confirmó
  que exactamente los 4 tests dirigidos fallaban con la excepción sin
  capturar, y se restauró el fix confirmando que los 24 tests del
  fichero pasan.
- **Fuera de alcance, con justificación explícita en el spec**:
  migrar las clases de adapter-que-falla ad-hoc existentes al nuevo
  fixture compartido (limpieza futura válida, no requerida para
  cerrar el bug verificado); añadir manejo de `OSError`/`TimeoutError`
  en `adapters/sdk/conformance.py` (valida el contrato de manejo de
  errores del AUTOR del adapter, preocupación distinta al boundary de
  `PlanExecutor` que esta spec cierra); inyección de fallos
  aleatoria/chaos-engineering, simulación de fallos a nivel de red —
  sin requisito nombrado por ninguna auditoría.

Evidencia: `484 → 490 passed, 8 skipped`. Ruff y mypy limpios.

## Supply-chain security en CI (2026-08-19)

Cierra `specs/041-supply-chain-security/`, noveno y último ítem de P2.
Las tres GitHub Actions que usa `.github/workflows/ci.yml` en sus 9
jobs (`actions/checkout@v4`, `astral-sh/setup-uv@v3`,
`actions/upload-artifact@v4`) estaban fijadas a un tag de versión
mutable, no a un commit inmutable — verificado con `gh api
repos/actions/checkout/git/ref/tags/v4` etc. Un tag mutable es un
vector de ataque de supply-chain real y bien documentado (la
recomendación #1 de la propia guía de hardening de GitHub Actions y
el check "Pinned-Dependencies" de OpenSSF Scorecard): si el
repositorio de una de esas actions fuera comprometido, un commit
malicioso publicado bajo el tag existente se descargaría en silencio
en cada run futuro de CI, sin ningún cambio de código en DomoAI que
revisar. Segundo hueco verificado: no existía `.github/dependabot.yml`
en ningún sitio del repo (`command find .github -type f` solo
encontraba `ci.yml`), así que ni los SHAs fijados (una vez fijados) ni
las dependencias Python de `uv.lock` recibían nunca propuestas de
actualización automatizadas. Tercer hueco verificado: ningún job de CI
ni mecanismo alguno escaneaba el conjunto de dependencias bloqueado
contra vulnerabilidades conocidas — `uvx pip-audit --local` ejecutado
contra el entorno real sincronizado confirmó cero vulnerabilidades
conocidas hoy (confirmado, no asumido) — esta spec añade el mecanismo
de detección que faltaba, no corrige una vulnerabilidad ya presente.

- Las tres actions fijadas a su commit SHA exacto y ya resuelto,
  obtenido directamente vía `gh api` contra los repositorios reales
  (`actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4`,
  `astral-sh/setup-uv@caf0cab7a618c569241d31dcd442f54681755d39 # v3`,
  `actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02 # v4`)
  — con comentario de versión legible preservado. Detalle no trivial:
  el tag `v3` de `astral-sh/setup-uv` es un tag anotado (`object.type
  == "tag"`, no `"commit"`), así que su `object.sha` es el SHA del
  propio objeto tag, no del commit — dereferenciado correctamente vía
  `gh api repos/astral-sh/setup-uv/git/tags/<sha-del-tag>` para
  obtener el commit real. Aplicado en las 9 ocurrencias de
  `checkout`/`setup-uv` (una por job) y la única ocurrencia de
  `upload-artifact` (solo en `test-unit`); cero cambio de
  comportamiento en ningún job — verificado con diff completo del
  fichero, solo cambian las líneas `uses:` y se añade un job nuevo.
- Nuevo `.github/dependabot.yml`: dos entradas `updates`, ecosistemas
  `github-actions` y `pip`, cadencia semanal, sin auto-merge (las
  propuestas quedan para revisión humana, comportamiento por defecto
  de GitHub sin configuración adicional).
- Nuevo job `dependency-audit` en `ci.yml`: mismo patrón de setup que
  cada otro job (`uv sync`), ejecuta `uvx pip-audit --local` contra el
  entorno sincronizado — falla el job si encuentra cualquier
  vulnerabilidad conocida. `pip-audit` invocado vía `uvx` (no añadido
  a `pyproject.toml`) — herramienta de solo-CI, no dependencia del
  proyecto en tiempo de ejecución ni desarrollo.
- Probado: grep confirma cero referencias `@v4`/`@v3` mutables
  restantes en todo el fichero; el fichero sigue siendo YAML válido;
  `dependabot.yml` es YAML válido con ambos ecosistemas presentes;
  `uv sync && uvx pip-audit --local` ejecutado de verdad contra el
  entorno real sincronizado de este repositorio, reportando su
  resultado real actual ("No known vulnerabilities found", 57
  paquetes verificados). `uv.lock` ya pinaba versiones exactas con
  hash para cada dependencia (277 entradas `hash = ` confirmadas) —
  correcto ya de antes, sin cambios necesarios ahí.
- **Fuera de alcance, con justificación explícita en el spec**:
  generación de SBOM (práctica adyacente real pero sin hueco nombrado
  por ninguna auditoría); firma/attestation de artefactos de build
  (Sigstore/cosign — DomoAI no publica artefactos firmados en ningún
  registro hoy, nada que firmar todavía); pinning de dependencias
  transitivas más allá de lo que `uv.lock` ya hace (ya correcto);
  auto-merge de PRs de Dependabot (decisión de política del dueño del
  repo, no un cambio de código unilateral); escaneo de vulnerabilidades
  en ecosistemas no-Python (el repo no tiene ninguno hoy).

Evidencia: `490 passed, 8 skipped` (sin cambios — esta spec no toca
código Python). Ruff y mypy limpios.

## Planificación recurrente con revalidación por-ocurrencia (2026-08-19)

Cierra `specs/042-recurring-plan-scheduling/`, primer ítem de P3. Gap
verificado leyendo `Scheduler` (Spec 032) completo: solo soporta una
ejecución por `Plan` — `mark_executed(plan_id)` lo mueve a un estado
terminal `executed` y `grep -ri "recur|RRULE|cron|repeat" src/domoai/
runtime/scheduler.py src/domoai/domain/models.py` no devolvía nada. Un
operador que quiere "esto todos los días a las 22:00" no tenía forma
de expresarlo: tendría que recrear y reprogramar un `Plan` nuevo a
mano tras cada ejecución, para siempre.

Decisión de diseño confirmada con el usuario vía AskUserQuestion antes
de escribir el spec: cada ocurrencia de un schedule recurrente se
revalida contra el estado real del runtime inmediatamente antes de
ejecutar — nunca se valida una vez y se reproduce ciegamente. Esto es
un requisito directo del Principio III de la constitución
(capacidades/política pueden cambiar entre ocurrencias) y tiene una
consecuencia de seguridad concreta: una ocurrencia cuyos comandos
resulten `REQUIRES_CONFIRMATION` o inválidos en el momento de revalidar
NUNCA se auto-aprueba — se salta y se audita, y la recurrencia sigue
hasta su siguiente ocurrencia programada en vez de romperse.

- Nuevo modelo `RecurrenceRule` (`time_of_day`, `timezone` IANA,
  `days_of_week` opcional — `None` = diario), en
  `src/domoai/domain/models.py`.
- Nueva función pura `next_occurrence(rule, after) -> datetime` en
  `src/domoai/runtime/recurrence.py`, usando `zoneinfo` de la stdlib
  (sin dependencia nueva). Calcula siempre en hora local del
  `timezone` de la regla y convierte a UTC solo al final — nunca al
  revés — para que "22:00 hora local" siga siendo 22:00 hora local
  cruzando un cambio de horario de verano/invierno, no un salto
  silencioso de una hora en UTC. Probado contra fechas reales de
  cambio de DST en `Europe/Madrid` (spring-forward 2026-03-29 y
  fall-back 2026-10-25) — ambas verifican que la hora local resultante
  sigue siendo exactamente 22:00.
- Nueva tabla `recurring_schedules` (migración
  `005_recurring_schedules.sql`), completamente independiente de
  `scheduled_plans` — mismo patrón `payload TEXT` JSON-serializado ya
  usado por `ScheduledPlanRepository`. Nuevo
  `RecurringScheduleRepository` (`create`/`get`/`list_active`/
  `advance`/`cancel`) en `src/domoai/persistence/repositories.py`.
- `Scheduler` extendido (no modificado) con
  `schedule_recurring`/`cancel_recurring`/`list_recurring`/
  `run_due_recurring`, en `src/domoai/runtime/scheduler.py`. El nuevo
  `recurring_repository` es un parámetro keyword-only con default
  `None`, así que los 4 call sites existentes de `Scheduler(...)`
  siguen funcionando sin modificar. `run_due_recurring()` es un método
  nuevo y paralelo a `run_due()` (Spec 032), no una fusión — mantiene
  el camino one-shot ya probado completamente intacto. Por cada
  schedule activo vencido: construye un `Plan` fresco con
  `id=f"{schedule_id}@{execute_at.isoformat()}"` (único por
  construcción, no colisiona con el guard de reclamo de Spec 031 al
  ser siempre un id nuevo), llama `PlanService.validate()` contra el
  estado real; si el resultado es `PlanStatus.READY` ejecuta vía
  `PlanExecutor.execute()`; si no (`REQUIRES_CONFIRMATION` o
  inválido), audita `recurring_occurrence_skipped` con
  `payload.reason` (`"requires_confirmation"` o `"invalid"`) — un solo
  tipo de evento para ambos casos, por decisión explícita del spec.
  Cualquiera que sea el resultado, calcula la siguiente ocurrencia y
  hace `advance(...)`, así que un salto nunca rompe la recurrencia.
  `Scheduler.run()` ahora llama a `run_due()` y `run_due_recurring()`
  en cada iteración del poll loop.
- `build_runtime()` en `runtime_factory.py` construye
  `RecurringScheduleRepository` y la pasa a `Scheduler(...)`; expuesta
  también en `RuntimeComposition.recurring_schedule_repository`.
- Tres tools MCP nuevas en `domotics_server.py`:
  `schedule_recurring_plan` (toma un `plan_id` ya validado vía
  `validate_plan`, descarta su validación obsoleta y usa solo
  `.commands` como plantilla — coherente con la decisión de revalidar
  cada ocurrencia), `cancel_recurring_schedule`,
  `list_recurring_schedules`. Mismo patrón de
  `mutation_annotations`/`read_annotations` que las tools de
  scheduling one-shot existentes. Listas de nombres de tools
  hardcodeadas actualizadas en `test_domotics_mcp_contract.py`,
  `test_unified_mcp_contract.py` y
  `test_home_assistant_provider_runtime.py`.
- Probado: `next_occurrence` correcto en spring-forward, fall-back,
  restricción por `days_of_week`, y garantía "siempre estrictamente
  después de `after`" (`tests/unit/runtime/test_recurrence.py`, 4
  tests). Un schedule recurrente de riesgo SAFE se auto-ejecuta y
  avanza sin doble-ejecución antes de su siguiente hora; una ocurrencia
  cuyo dispositivo ya no existe se salta como inválida; una ocurrencia
  con `risk_class=CONFIRM` se salta, nunca se auto-aprueba, y queda
  auditada con `reason="requires_confirmation"`; tras ese salto la
  recurrencia sigue funcionando con normalidad en su siguiente hora;
  cancelar detiene toda ocurrencia futura (`tests/unit/runtime/
  test_scheduler.py`, 7 tests nuevos). Los 6 tests one-shot existentes
  en ese mismo fichero siguen pasando sin ninguna modificación —
  cero regresión a Spec 032.
- **Fuera de alcance, con justificación explícita en el spec**: EV/HVAC
  como modelos de dominio de primera clase, tarifas de exportación,
  incertidumbre de forecast, degradación de batería, Provider SDK
  externo — ítems P3 separados e independientes, sin dependencia de
  recurrencia. Expresiones de recurrencia complejas tipo RRULE
  (mensual/anual, fechas de excepción) — `time_of_day` + `days_of_week`
  opcional cubre los patrones reales de automatización del hogar sin
  inventar una gramática de calendario que nadie ha pedido. Reintentar
  o recuperar automáticamente una ocurrencia saltada — el
  comportamiento seguro es saltarla y seguir con el horario, no
  inventar un mecanismo de retry/backfill con sus propias implicaciones
  de seguridad. Editar un schedule recurrente existente in-place —
  este spec soporta crear y cancelar; cambiar su regla se asume
  cancelar-y-recrear por ahora, una operación más pequeña y segura que
  una API de mutación in-place.

Evidencia: `500 passed, 8 skipped` (490 → 500). Ruff y mypy limpios
(93 ficheros fuente).

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

## Operabilidad del laboratorio y compatibilidad MCP

`domoai-lab` es una herramienta de desarrollo, no otro runtime domótico ni un
bus MCP. Su `LabRunner` solo acepta las operaciones `up`, `status`, `down` y
`smoke`, valida una allowlist de servicios y construye argv para Docker
Compose sin evaluar shell. El archivo local `dev/lab/.env` solo se inyecta al
subproceso Compose; el smoke determinista lo excluye expresamente junto con
todas las variables `DOMOAI_*`.

El perfil `smoke` selecciona pruebas fixture de las cinco fronteras locales:
Home Assistant, MQTT/Zigbee2MQTT, Modbus, Matter y KNX. No es evidencia de
commissioning ni sustituye los smoke tests live. La configuración real de
OMIE/Open-Meteo, un nodo Matter comisionado y un gateway KNX permanecen fuera
del camino determinista.

`domoai.mcp.compat.ensure_fastmcp_settings_ready()` es una corrección estrecha
de compatibilidad para el `Settings.lifespan` genérico de FastMCP cuando la
versión instalada de Pydantic lo deja incompleto. Se invoca antes de construir
el servidor unificado, no modifica modelos de dominio, no crea una ruta MCP
alternativa y no instala filtros globales de warnings. Si el SDK ya
está completo, la función no hace nada.

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

## EV charging y HVAC comfort como conceptos de primera clase (2026-08-19)

Cierra `specs/043-ev-charging-and/`, segundo ítem de P3. Gap
verificado leyendo `scenario.py`/`cp_sat.py` completos: hasta hoy EV y
HVAC solo podían expresarse como `Load` genérico — un único bloque
contiguo de potencia fija elegido por `_start_variables()`/
`_active_load_terms()` (`cp_sat.py:298-332`). Sin semántica propia
para ninguna de las dos clases de dispositivo: un cargador EV necesita
una potencia de carga variable por slot que acumula hacia un estado de
carga objetivo antes de un deadline — estructuralmente idéntico a
cómo `BatteryProfile` ya modela el SOC de la batería
(`cp_sat.py:133-153,176-189`, IntVars de SOC por slot encadenadas por
una ecuación de balance con eficiencia), no un bloque de potencia
fija. Un dispositivo climático necesita "estar activo lo suficiente
dentro de una ventana," no "un único bloque contiguo." Verificado que
no existe modelado térmico/ambiental en absoluto en el repo
(`grep -rniE "ambient|thermal|outdoor_temp|comfort" src/domoai` sin
resultados) — una simulación térmica RC completa sería
desproporcionada sin ningún andamiaje existente, así que esta spec
deliberadamente no la añade.

- Nuevo `EVChargingLoad` (`capacity_kwh`, `initial_soc_kwh`,
  `target_soc_kwh`, `max_charge_kw`, `min_charge_kw` opcional,
  `deadline_slot`, `charge_efficiency`) en `scenario.py` — valida su
  dominio SOC igual que `BatteryProfile.validate_state_domain`. Nuevo
  `ComfortLoad` (`earliest_slot`, `deadline_slot`, `min_active_slots`,
  `power`/`power_unit`) — valida en construcción que la ventana
  contiene al menos `min_active_slots` slots (`ValidationError`
  inmediata si no, antes de llegar siquiera al solver).
  `OptimizationScenario.ev_loads`/`comfort_loads` (aditivos,
  default vacío); la comprobación de unicidad de ids de
  `validate_loads` ahora cubre las tres listas combinadas.
- `validate_scenario()` extendida con las mismas comprobaciones de
  dispositivo/capacidad/comando/unidad que ya aplicaban a `Load`
  (factorizadas en `_validate_device_capability_command`, reutilizada
  por las tres clases de carga), comprobación de encaje en el
  horizonte, y `energy_context_required` si `ev_loads`/`comfort_loads`
  no está vacío — ninguna de las dos clases nuevas tiene sentido sin
  el contexto energético, porque el SOC y la ecuación de balance de
  red solo existen dentro de `_optimize_energy()` (`_optimize_legacy()`
  no se toca en absoluto).
- `cp_sat.py`, `_optimize_energy()`: cadena de IntVars de SOC por EV
  (idéntica al patrón de la batería), variable de carga por slot
  acotada en `[0, max_charge_kw]` (o `{0} ∪ [min_charge_kw,
  max_charge_kw]` vía el mismo patrón booleano
  `charging`/`OnlyEnforceIf` que ya usa la batería, si
  `min_charge_kw > 0`), restricción dura
  `soc[deadline_slot] >= target_soc_kwh`. Por cada `ComfortLoad`:
  BoolVars independientes por slot SOLO dentro de su ventana (sin la
  restricción "exactamente un bloque contiguo" de `Load`),
  `sum(vars) >= min_active_slots`. El término `active_load` del bucle
  por slot (que alimenta la ecuación de balance de red Y
  `_add_energy_constraints`, incluyendo `max_house_power`) se extiende
  con la suma de cargas EV y potencia de confort activa — ambas
  restricciones existentes cubren automáticamente las dos clases
  nuevas sin ningún cambio adicional, porque ya eran genéricas sobre
  "lo que sea que `active_load` contenga."
- `_proposal_plan()` gana dos parámetros opcionales
  (`ev_charge_slots`/`comfort_active_slots`, default `None`) que
  añaden `Command`s ya construidos al mismo `groups` que agrupa por
  `execute_at` (reutiliza sin cambios el splitting multi-slot de la
  Spec 032). El call site de `_optimize_legacy()` no pasa ninguno de
  los dos — su salida queda comprobadamente idéntica.
  `constraint_summary["slots"]` gana `ev_charge_kw`/`comfort_power_kw`
  por slot (antes solo se calculaban internamente, no se exponían) —
  necesario para que la invariante de balance sea auditable con las
  dos clases nuevas presentes, no solo internamente correcta.
- Probado: un EV con objetivo alcanzable llega al SOC objetivo en su
  deadline; un objetivo físicamente inalcanzable (potencia/tiempo
  insuficiente) reporta `INFEASIBLE`, nunca un plan que sub-entrega en
  silencio; un EV ya en su objetivo resuelve trivialmente; un
  `ComfortLoad` se activa en al menos `min_active_slots` de su
  ventana, no necesariamente contiguos; una ventana imposible se
  rechaza en construcción (`ValidationError`); la potencia de confort
  respeta `max_house_power` igual que cualquier otra carga (probado
  forzando una infeasibilidad real, no solo verificando aritmética);
  EV + confort + carga genérica + batería coexisten sin que ninguno
  se salte `max_house_power`; los 24 tests preexistentes del solver
  (`test_energy_optimization.py`, `test_optimization_fixtures.py`,
  `test_energy_optimizer_targets.py`) siguen pasando sin ninguna
  modificación — cero regresión.
- **Fuera de alcance, con justificación explícita en el spec**: V2G
  (descarga vehículo-a-red) — esta spec solo añade carga
  unidireccional; `BatteryProfile` ya tiene el patrón de descarga para
  extender si algún día se nombra una necesidad real. Simulación
  térmica RC completa para HVAC — sin andamiaje existente,
  desproporcionado. Arbitraje de potencia compartida entre múltiples
  cargadores EV más allá de `max_house_power` — la restricción
  genérica ya cubre la potencia agregada de todas las cargas
  incluyendo EVs. Editar/cancelar un plan de carga EV o confort en
  curso — mismo tratamiento que cualquier otra carga hoy (un escenario
  nuevo se resuelve desde cero). Integración a nivel de adapter con un
  cargador EV o termostato real — fuera de alcance; el plan propuesto
  ya fluye por el boundary Command/Plan/PlanExecutor existente sin
  cambios.

Evidencia: `500 passed, 8 skipped` (500 → 507, 7 tests nuevos). Ruff y
mypy limpios (93 ficheros fuente). `schemas/v1/
optimization-scenario.schema.json` regenerado — solo cambio aditivo
(dos nuevas definiciones de modelo, dos nuevos campos opcionales en
`OptimizationScenario`), ningún campo existente cambia de forma.

## Tarifa de exportación como ingreso en el optimizador (2026-08-19)

Cierra `specs/044-export-tariff-revenue/`, tercer ítem de P3. Gap
verificado leyendo `cp_sat.py` completo: `context.tariffs` se lee
exactamente dos veces, ambas contra cantidades de importación
(`grid_import`/`import_kw`) — `command grep -rn "\.tariffs\b"
src/domoai --include="*.py"` lo confirma. La energía exportada tenía
valor económico cero en todo el optimizador: `minimize_energy_cost`
solo sumaba `grid_import * precio`, y el `energy_cost` reportado nunca
restaba ingresos por exportación, aunque `max_grid_export` y
`grid_export` ya existían como cantidad física.

- Nuevo campo aditivo `EnergyContext.export_tariffs: list[TariffPoint]
  | None = None`, estructuralmente idéntico a `tariffs` — reutiliza el
  mismo modelo `TariffPoint`, sin modelo nuevo. `validate_series`
  extendida para cubrirlo cuando está presente, con el mismo bucle
  genérico de cobertura-por-slot ya usado para `base_load_forecast`.
- `_objective_terms()`, rama `minimize_energy_cost`: nuevo término
  simétrico al de importación pero de signo opuesto
  (`-sign * weight * export_tariffs[slot].price_per_kwh * horas *
  OBJECTIVE_SCALE * grid_export[slot]`), añadido SOLO cuando
  `context.export_tariffs is not None` — con el campo ausente el
  término literalmente no se genera, objetivo matemáticamente
  idéntico a antes.
- Bucle de reporte tras resolver: nuevo `export_revenue` acumulado
  igual que `energy_cost` pero sobre `export_kw`; `objective_values
  ["energy_cost"]` pasa a ser neto (`energy_cost - export_revenue`,
  que es `energy_cost` sin cambios cuando `export_revenue` es `0.0`
  por defecto); nuevo `objective_values["export_revenue"]` expuesto
  por separado — el ingreso nunca queda oculto dentro de una única
  cifra combinada, sirviendo directamente la auditabilidad exigida
  por el Principio III.
- `ComposedEnergyContextProvider` gana un parámetro opcional
  `export_tariffs: TariffProvider | None = None`, con el mismo
  tratamiento ya existente para `battery` (opcional, se resuelve solo
  si está presente, se valida horizonte/frescura con los mismos
  helpers genéricos). Reutiliza el protocolo `TariffProvider` tal
  cual — sin protocolo nuevo, cualquier `TariffProvider` existente
  (incluido `StaticTariffProvider`) ya sirve para tarifa de
  exportación sin código adicional. Detalle de compatibilidad
  encontrado y corregido durante la implementación: la primera versión
  añadía siempre un segmento `export_tariff:none` a `source_revision`
  incluso sin tarifa de exportación configurada, rompiendo un test
  existente (`test_energy_providers.py::...endswith("battery:none")`)
  — corregido para solo añadir el segmento cuando la tarifa de
  exportación está realmente presente, preservando el `source_revision`
  exacto de todo escenario existente.
- Probado: una tarifa de exportación con exportación real reduce el
  coste neto reportado frente a un escenario idéntico sin ella; un
  precio de exportación más alto en un slot desplaza la exportación
  hacia ese slot cuando hacerlo es gratis; el ingreso de exportación
  se reporta como valor propio, distinto del coste neto; un escenario
  sin tarifa de exportación reporta `export_revenue == 0.0`
  exactamente; una serie de tarifa de exportación que no cubre el
  horizonte se rechaza igual que ya se rechazaba `tariffs`; los tests
  preexistentes de los tres ficheros del solver siguen pasando sin
  ninguna modificación — cero regresión. Un test de contrato MCP
  (`test_energy_context_mcp.py`) que enumeraba explícitamente el
  conjunto completo de campos de `EnergyContext` sí necesitó
  actualizarse para incluir `export_tariffs` — actualización esperada
  de un contrato que ya declaraba la forma exacta del modelo, no una
  regresión.
- **Fuera de alcance, con justificación explícita en el spec**: cablear
  una fuente de datos real de tarifa de exportación (proveedor de
  feed-in tariff concreto, API de contrato de comercializadora) — esta
  spec añade el modelo semántico y el puerto de proveedor opcional,
  mismo patrón aditivo ya usado para `base_load_forecast`; una fuente
  live puede añadirse después sin cambiar este contrato. Ningún modelo
  de precios nuevo (tarifas escalonadas, topes contractuales, umbrales
  de autoconsumo) — la tarifa de exportación usa la misma forma de
  serie por slot que la de importación, sin inventar semántica nueva.

Evidencia: `507 passed, 8 skipped` (507 → 512, 5 tests nuevos). Ruff y
mypy limpios (93 ficheros fuente). `schemas/v1/energy-context.schema.json`
y `schemas/v1/optimization-scenario.schema.json` regenerados — ambos
cambios puramente aditivos (un campo opcional nuevo cada uno).

## Incertidumbre de pronóstico solar y de carga base (2026-08-19)

`SolarForecastPoint`/`BaseLoadPoint` (`src/domoai/optimizer/energy.py`)
solo declaraban un valor puntual, sin forma de expresar confianza ni de
pedirle al solver que se cubra ante error de pronóstico (verificado:
`solar_powers`/`base_load_powers` en `cp_sat.py:126-131` leían siempre
`point.power` directamente). Alcance confirmado con el usuario vía
AskUserQuestion — ambas partes requeridas en una sola spec:

- Nuevo modelo `ConfidenceBand` (`low`, `high`) reutilizado como campo
  opcional `confidence` en ambos puntos, con cobertura todo-o-nada por
  serie (igual patrón que las series ya existentes) y validación de
  que el punto cae dentro de su propia banda. Ausente o parcial: mismo
  comportamiento de hoy o `ValidationError` en construcción.
- Nuevo flag `OptimizationScenario.conservative` (`bool`, por defecto
  `False`). Activo y con bandas presentes: el solver sustituye, en el
  único punto donde `solar_powers`/`base_load_powers` se construyen,
  el valor puntual por el extremo pesimista (`confidence.low` para
  solar, `confidence.high` para carga base) — cero código adicional en
  cada restricción consumidora, mismo patrón de "un solo punto de
  sustitución, consumidor genérico" ya usado en la spec 043 para
  `active_load`. Activo sin bandas en una serie requerida: rechazado en
  `validate_scenario` con diagnóstico `conservative_mode_requires_confidence`,
  sin intentar resolver.
- `objective_values["conservative_mode_active"]` y
  `constraint_summary["forecast_confidence"]` (`solar_bounded`,
  `base_load_bounded`) hacen el resultado auto-descriptivo sin releer
  el escenario de entrada.
- Cero regresión verificada: banda presente + modo conservador apagado
  produce plan y coste idénticos a no tener banda; flag ausente o
  `False` es idéntico a hoy independientemente de las bandas.
- **Fuera de alcance, con justificación explícita en el spec**: cómo se
  calculan las bandas (percentiles concretos, calibración estadística)
  — se tratan como límites opacos suministrados por el proveedor
  externo. Seguimiento de error de pronóstico en tiempo real o
  reoptimización disparada por desviación — no solicitado por el ítem
  de backlog. Tarifas y perfil de batería sin cambios — el ítem acota
  la incertidumbre a solar y carga base únicamente. Control por-serie
  o por-slot del modo conservador — un único flag por escenario,
  suficiente para el alcance pedido.

Evidencia: `512 passed, 8 skipped` (512 → 519, 7 tests nuevos). Ruff y
mypy limpios (93 ficheros fuente). `schemas/v1/energy-context.schema.json`,
`schemas/v1/optimization-scenario.schema.json`,
`schemas/v1/solar-forecast-point.schema.json` y
`schemas/v1/solar-forecast-series.schema.json` regenerados — los cuatro
diffs puramente aditivos (un `$defs/ConfidenceBand` nuevo y un campo
opcional `confidence`/`conservative` cada uno).

## Coste de degradación de batería (2026-08-19)

Quinto y último ítem de P3 — con esta spec, P3 queda completo. El
ciclado de batería era gratis en el objetivo del optimizador
(verificado: `_objective_terms()` solo precia `grid_import`/
`grid_export`, sin ningún término de `charge`/`discharge`), pese a que
toda batería real se desgasta con el throughput acumulado. Alcance
confirmado con el usuario vía AskUserQuestion — ambas partes
requeridas en una sola spec, mismo patrón de decisión que las specs
043/045:

- Reporte de throughput SIEMPRE presente cuando hay batería, sin
  configuración adicional: `objective_values["battery_throughput_kwh"]`
  se calcula sumando `(charge_kw + discharge_kw) * resolution_hours`
  por slot en el mismo bucle post-solve que ya calculaba
  `energy_cost`/`export_revenue` — sin variables nuevas del solver, las
  cantidades de carga/descarga ya se extraían para
  `constraint_summary["slots"]`. `0.0` cuando no hay batería.
- Nuevo campo opcional `BatteryProfile.degradation_cost_per_kwh`
  (`ge=0`). Al estar en `BatteryProfile` y no en un nivel superior, el
  requisito "no se puede configurar coste de desgaste sin batería" es
  una garantía del sistema de tipos, no una regla de validación en
  tiempo de ejecución.
- Cuando está configurado y es mayor que cero, `_objective_terms()`/
  `_solve_tiers()` reciben `charge_variables`/`discharge_variables`
  como nuevos parámetros de solo palabra clave (con valor por defecto
  `[]`, sin afectar ninguna otra rama del objetivo) y añaden un término
  proporcional al throughput dentro de `minimize_energy_cost` — el
  solver ahora prefiere ciclar menos la batería cuando ciclar no aporta
  otro beneficio, y sigue ciclando cuando el beneficio (p. ej. un
  spread de tarifa grande) supera el coste de desgaste.
- `objective_values["battery_degradation_cost"]` reportado por
  separado; `objective_values["energy_cost"]` pasa a netear
  `import_cost - export_revenue + battery_degradation_cost` (sin
  cambio cuando `battery_degradation_cost` es `0.0` por defecto),
  mismo patrón exacto que la spec 044 usó para `export_revenue`.
- Un coste de desgaste de `0.0` se trata igual que ausente — no se
  rechaza, y no distorsiona el objetivo (coeficiente cero).
- Cero regresión verificada explícitamente: ausencia del campo produce
  plan y coste idénticos a hoy; el reporte de throughput por sí solo no
  cambia ninguna decisión del solver.
- **Fuera de alcance, con justificación explícita en el spec**: fade de
  capacidad o seguimiento de estado de salud entre ejecuciones — dentro
  de un único horizonte acotado la capacidad se sigue tratando como
  constante, exactamente igual que hoy; solo se añade el coste
  económico del ciclado. Throughput de la batería del EV (spec 043) —
  la economía de desgaste de un vehículo pertenece a su propietario, no
  al optimizador doméstico. Cómo se deriva la cifra de coste por kWh
  (datos de fabricante, coste de reemplazo / vida útil en ciclos) — se
  trata como un valor opaco suministrado por la persona usuaria.

Evidencia: `519 → 526 passed, 8 skipped` (7 tests nuevos). Ruff y mypy
limpios (93 ficheros fuente). `schemas/v1/battery-profile.schema.json`,
`schemas/v1/battery-state.schema.json`,
`schemas/v1/energy-context.schema.json` y
`schemas/v1/optimization-scenario.schema.json` regenerados — los
cuatro diffs puramente aditivos (un campo opcional
`degradation_cost_per_kwh` cada uno). **P3 completo**: Specs 042, 043,
044, 045 y 046 cerradas.

## Reloj virtual del runtime (2026-08-19)

Primer ítem de un nuevo tier **P4** (plataforma/ecosistema), tras
completar P3 esta misma sesión. El control de tiempo estaba disperso e
inconsistente: 45 puntos del repositorio leían `datetime.now(UTC)`
directamente; algunos componentes ya aceptaban un `now` inyectable
por-llamada (`Scheduler.run_due`/`run_due_recurring`,
`StateStore.mark_stale`), pero otros con decisiones igual de
dependientes del tiempo no tenían ningún punto de inyección
(`PlanService.create_plan`, `PlanService.assert_executable`,
`PlanExecutor.execute`). Sin una única fuente de tiempo compartida, no
hay forma de controlar de forma determinista cuándo un plan expira,
cuándo se ejecuta, o cuándo un estado se considera obsoleto — bloqueo
duro para los ítems futuros de P4 (replay determinista, digital twin,
HIL, shadow mode), todos los cuales necesitan correr el runtime contra
tiempo controlable en vez de solo tiempo real.

- Nuevo puerto `Clock` (`src/domoai/runtime/clock.py`, `Protocol` con
  un único método `now() -> datetime`, mismo estilo que
  `AdapterPort`/el resto de puertos en `ports.py`). Dos
  implementaciones: `SystemClock` (envoltorio directo de
  `datetime.now(UTC)`, sin lógica añadida) y `FixedClock` (tiempo
  mutable, con `set()` para avanzarlo).
- `StateStore`, `PlanService`, `PlanExecutor` y `Scheduler` ganan un
  parámetro `clock: Clock | None = None` de solo palabra clave,
  guardado como `self.clock = clock or SystemClock()` — mismo patrón
  aditivo de inyección de dependencias ya usado repetidamente en esta
  sesión (`recurring_repository` en la Spec 042,
  `plan_repository`/`outcome_repository` en `PlanExecutor`). Los
  `datetime.now(UTC)` de los cinco puntos identificados pasan a
  `self.clock.now()`.
- Los parámetros `now: datetime | None` ya existentes en
  `run_due`/`run_due_recurring`/`mark_stale` se conservan intactos —
  solo cambia su fallback interno de `now or datetime.now(UTC)` a
  `now or self.clock.now()`, sin romper ningún caller existente.
- `build_runtime` (`runtime_factory.py`) construye un único `Clock`
  (`SystemClock()` por defecto) y lo pasa a los cuatro componentes —
  wiring centralizado, no configuración por componente.
- Cero regresión verificada explícitamente: sin configurar un reloj,
  comportamiento idéntico a hoy en los 533 tests. Un test combinado
  (`test_single_fixed_clock_drives_every_timing_decision_consistently`)
  prueba que un único `FixedClock` avanzado una vez es observado
  consistentemente por expiración de plan, scheduling, timing de
  ejecución y staleness de estado a la vez — durante su desarrollo se
  detectó y corrigió un fallo real de wiring: si solo se inyecta el
  reloj en el `Scheduler` pero no en el `PlanExecutor` que este usa
  internamente, el executor sigue comparando contra tiempo real,
  confirmando por qué la spec documenta el wiring correcto como
  responsabilidad del caller en vez de intentar prevenirlo
  programáticamente.
- Fuera de alcance, con justificación explícita en el spec: timestamps
  de telemetría de adaptador (`received_at`/`observed_at` en
  `StateSnapshot`, `Measurement`, muestras KNX/Modbus/Matter/
  Zigbee2MQTT) — representan momentos de observación de eventos
  reales del mundo externo, no decisiones del runtime, y siguen en
  tiempo real. El `now: Callable[[], datetime]` ya existente en
  `ComposedEnergyContextProvider` — vive en el optimizador, tiene su
  propio punto de inyección, y no se migra en esta spec. Replay
  determinista, digital twin, HIL y shadow mode en sí mismos — esta
  spec solo entrega la base de control de tiempo que esos ítems
  futuros de P4 necesitarán; ninguno se empieza a implementar aquí.

Evidencia: `528 → 533 passed, 8 skipped` (5 tests nuevos, más 2 de
`test_clock.py` ya contados en el salto previo de 526→528). Ruff y
mypy limpios (94 ficheros fuente). Sin cambio de schema — dependencia
interna del runtime, sin contrato público afectado.

## Replay determinista de planes (2026-08-19)

Segundo ítem de P4, elegido por el usuario tras completar la Spec 047
(virtual clock). Depurar la ejecución de un plan concreto exigía hoy
reproducirlo en vivo a una hora de reloj no controlada, o leer eventos
resumidos del audit trail (`plan_validated`, `command_execution_outcome`,
`plan_execution_completed`) que capturan estado/contadores pero no
bastan para volver a ejecutar el mismo plan. `PlanRepository` ya
persiste el `Plan` completo; la Spec 047 ya añadió `Clock`. Nada
combinaba ambos.

- Nuevo `PlanReplayer` (`src/domoai/runtime/replay.py`) y `ReplayResult`
  (estado repetido, `outcomes` por comando, notas de reconstrucción
  incompleta). `replay(plan_id)` carga el plan persistido, construye un
  `DeviceRegistry`/`StateStore`/`PlanService`/`PlanExecutor` totalmente
  aislados, y re-valida + re-ejecuta contra un `FixedClock` anclado al
  propio `execute_at`/`created_at` del plan — el resultado no depende
  de cuándo se pide el replay.
- **Corrección real de diseño durante la implementación**: el plan
  original proponía reconstruir el registry vía
  `DeviceRegistry.load_persisted(...)` (igual que `build_runtime`).
  La implementación descubrió, leyendo el propio docstring del método,
  que `load_persisted` deja las rutas de comando (`_routes`) vacías a
  propósito — solo `apply_snapshot` (un discovery real) las reconstruye
  — así que cualquier comando replayado habría fallado siempre con
  `route_not_found`, sin relación con si el dispositivo "existía".
  `build_runtime` nunca lo sufre porque siempre encadena
  `discovery.refresh()` contra el adapter EN VIVO justo después. Como
  el replay no puede tocar nunca un adapter real (FR-003), la
  corrección fue construir el registry vía `DiscoveryService` contra
  un `SimulatedHomeAdapter` fresco — el mismo mecanismo, pero apuntado
  al fixture determinista. Consecuencia asumida explícitamente: el
  replay opera dentro del universo de dispositivos del fixture, no
  necesariamente los dispositivos de producción originales del plan.
- Por eso el reporte de reconstrucción incompleta (`incomplete_reconstruction_notes`)
  es más importante de lo previsto: cualquier dispositivo del plan que
  no exista en el fixture se reporta explícitamente, en vez de fallar
  en silencio o fingir fidelidad que no existe.
- Aislamiento verificado como propiedad dura, no best-effort: el
  adapter, `AuditLog`, `PolicyEngine`, registry y state store del
  replay son instancias nuevas y locales a cada llamada — nunca
  comparten estado con el runtime en vivo, y `replay()` nunca llama a
  `save()` en ningún repositorio. Tres tests dedicados prueban que el
  adapter en vivo no recibe llamadas, el state store en vivo no
  cambia, y el plan persistido no se modifica.
- Replay repetible: llamar `replay()` dos veces sobre el mismo plan
  produce resultados idénticos — verificado directamente por test.
- Fuera de alcance, con justificación explícita en el spec: exposición
  vía tool MCP o CLI (esta spec entrega solo el mecanismo subyacente,
  igual que la Spec 047 no añadió CLI para `Clock`); historial de
  estado por-punto-en-el-tiempo (no existe tal histórico en la capa de
  persistencia actual — los snapshots se sobrescriben, no se
  versionan); replay masivo o programado.

Evidencia: `533 → 541 passed, 8 skipped` (8 tests nuevos). Ruff y
mypy limpios (95 ficheros fuente). Sin cambio de schema — nuevo
módulo interno del runtime, sin contrato público nuevo.

## Orquestación del skill de energía

El skill portable `optimize-home-energy` declara la secuencia y el rol de la
única conexión MCP general, sin fijar nombres de servidores:

| Operación | Rol | Tool | Modo |
| --- | --- | --- | --- |
| `discover_devices` | `mcp` | `discover_devices` | lectura |
| `get_state` | `mcp` | `get_state` | lectura |
| `get_energy_context` | `mcp` | `get_energy_context` | lectura |
| `optimize_scenario` | `mcp` | `optimize_scenario` | propuesta |
| `validate_plan` | `mcp` | `validate_plan` | validación |
| `explain_solution` | `mcp` | `explain_solution` | lectura |
| `operator_approval` | `operator` | `request_approval` | aprobación |
| `execute_plan` | `mcp` | `execute_plan` | mutación |

El workflow usa una sola conexión MCP mediante un puerto semántico inyectado.
En v2 lee `get_energy_context` antes de construir la propuesta y detiene el
flujo si falta el contexto o su revisión no coincide. No es un MCP adicional,
no llama adapters ni `OptimizerPort` directamente y no
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
