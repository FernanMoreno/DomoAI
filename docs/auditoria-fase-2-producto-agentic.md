# Auditoría Fase 2 — Producto agentic

**Objetivo:** convertir las capacidades del runtime en una experiencia agentic potente, portable y segura para Claude Code, Codex y otros hosts MCP.
**Dependencia:** Fase 0 estable y Fase 1 operable.

## 1. Estado actual del producto

La superficie pública es un MCP unificado con herramientas semánticas de:

- discovery;
- consulta de estado;
- contexto energético;
- validación de comandos y planes;
- aprobación;
- ejecución;
- validación, optimización y explicación de escenarios.

Existe una Skill portable de energía en [`skills/core/optimize-home-energy/SKILL.md`](../skills/core/optimize-home-energy/SKILL.md:1). La Skill utiliza el mismo runtime para leer contexto, solicitar propuestas, validar y comprometer bundles.

Esta dirección es correcta: **MCP define qué puede hacerse; Skill define cuándo, con qué orden y con qué condiciones debe hacerse.**

## 2. A-007 — Semántica read-only incorrecta — cerrado en Fase 0

### Evidencia

El hallazgo queda cerrado en Fase 0. `preview_command` y `preview_plan` son
read-only y no llaman a `_persist_validated_plan`; `prepare_command` y
`prepare_plan` declaran mutación y persisten. `validate_command` y
`validate_plan` se conservan como aliases persistentes legacy con anotación de
mutación explícita.

### Riesgo de producto

Un cliente MCP puede cachear o repetir una tool read-only esperando ausencia de efectos laterales. También puede producirse colisión de IDs o sobreescritura de validaciones de diferentes intenciones.

### Opciones

**Decisión aplicada:** separar:

```text
preview_*       -> no escribe, resultado efímero
prepare_*       -> persiste digest, policy y estado
validate_*      -> nombre reservado para contrato explícito
```

Si se mantiene la escritura en `validate_*`, deben cambiarse las anotaciones, documentarse el efecto y añadirse `request_id`, `definition_digest` e idempotencia.

## 3. A-014 — Autoridad recurrente y documentación — cerrado en Fase 0

La especificación y el contrato se alinearon con el modelo conservador: toda
automatización persistente necesita consentimiento standing explícito, incluido
un template `READY`; una ejecución única `READY` sigue sin necesitar approval.

Debe tomarse una decisión de producto, no dejarla implícita:

- **Modelo conservador aplicado:** toda automatización persistente necesita
  consentimiento standing explícito vinculado a template, principal, scope,
  digest y expiración.
- El modelo graduado queda descartado para evitar que la duración indefinida
  de una automatización se interprete como una operación única de bajo riesgo.

La decisión debe reflejarse simultáneamente en spec, código, nombres de tests, Skill y documentación para clientes MCP.

## 4. Catálogo de Skills recomendado

La Skill energética actual debería ser la primera de una familia:

| Skill | Función | Riesgo especial |
|---|---|---|
| `optimize-ev-charging` | Cargar EV por deadline, tarifa y potencia. | No superar potencia contratada ni corriente del cargador. |
| `thermal-comfort` | Coordinar HVAC, temperatura, presencia y coste. | Límites de temperatura y anti-ciclos. |
| `solar-self-consumption` | Consumir solar local y desplazar cargas. | No interpretar previsión como producción confirmada. |
| `battery-arbitrage` | Cargar/descargar batería con límites de SOC. | Qualification y feedback físico obligatorio. |
| `night-mode` | Luces, persianas, clima y seguridad. | Nunca desbloquear ni desarmar alarmas implícitamente. |
| `vacation-mode` | Protección, simulación de presencia y ahorro. | Requiere scope y expiración claros. |
| `device-diagnostics` | Diagnóstico de salud y causa probable. | No transformar diagnóstico en escritura automática. |
| `commission-new-device` | Descubrimiento y verificación de capabilities. | Discovery no equivale a autoridad física. |

Cada Skill debe declarar:

- tools y resources permitidos;
- datos mínimos de entrada;
- freshness requerido;
- acciones prohibidas;
- confirmation policy;
- límites de retry;
- criterio de éxito;
- rollback o estado `UNKNOWN`.

## 5. DSL de optimización

El escenario actual ya modela horizonte, cargas, EV, confort, constraints, objetivos y SOC terminal. Para convertirlo en una interfaz de producto debe añadirse:

- `schema_version` y migraciones compatibles;
- `scenario_id` y `definition_digest`;
- unidades estrictas y valores finitos;
- origen, timestamp y confianza de cada forecast;
- restricciones duras frente a preferencias blandas;
- explicación de restricciones activas;
- explicación de inviabilidad;
- soluciones alternativas y comparación de escenarios;
- límite de tiempo del solver;
- calidad mínima aceptable;
- presupuesto de coste y máximo de importación;
- trazabilidad desde cada slot hasta el plan generado.

OR-Tools debe seguir siendo una capa de propuesta. El resultado debe convertirse en un `Plan` validado por policy y admission antes de cualquier ejecución.

## 6. Experiencia multiagente

Claude Code y Codex pueden compartir el gateway. El producto debe hacer visible:

- identidad del cliente MCP;
- vivienda y área en scope;
- estado de la autoridad física;
- qué acciones requieren operator approval;
- qué contexto es stale o inválido;
- por qué una propuesta no puede ejecutarse;
- qué agent creó o modificó un schedule.

El agente nunca debe recibir secretos ni interpretar una descripción de runtime como permiso de escritura.

## 7. Automatización sin LLM

Para ser una plataforma de domótica y no solo un copiloto, hace falta un motor local para reglas rutinarias:

```text
evento/state change
        ↓
trigger tipado
        ↓
policy + cooldown + condición
        ↓
plan pequeño
        ↓
executor seguro
```

El LLM debe intervenir en intención ambigua, optimización, diagnóstico y explicación. Una desconexión de Internet no debe apagar las automatizaciones locales aprobadas.

## 8. Contrato de Skills

Cada Skill debería poder validarse automáticamente antes de publicarse. El contrato mínimo recomendado es:

```yaml
name: optimize-ev-charging
version: 1
required_context:
  - energy_context
  - ev_capability
allowed_tools:
  - get_energy_context
  - validate_scenario
  - optimize_scenario
  - validate_plan
  - commit_or_schedule_bundle
forbidden_tools:
  - direct_adapter_call
freshness:
  state_max_age_seconds: 60
approval:
  required_for: [physical_mutation]
failure_mode: stop_and_report
```

El contrato debe verificar que una Skill no introduce llamadas directas a adapters, no consume aprobaciones de otro scope y no puede transformar una propuesta en una acción sin pasar por el runtime.

## 9. Implementación realizada en esta fase

### 9.1 Contrato y catálogo de Skills

El validador local de [`src/domoai/skills/validator.py`](../src/domoai/skills/validator.py:1)
mantiene compatibilidad con v1–v3 y añade contrato v4. v4 normaliza:

- `required_context`;
- `allowed_tools` y `forbidden_tools`;
- `state_max_age_seconds`;
- `approval_required_for`;
- `failure_mode`.

Las mutaciones v4 requieren `operator_approval` antes de la operación física,
y el validador exige que no existan rutas directas a adapter, vendor API o
solver. [`src/domoai/skills/catalog.py`](../src/domoai/skills/catalog.py:1)
valida en orden fijo el catálogo inicial:

- `optimize-home-energy` — propuesta y bundle aprobado;
- `device-diagnostics` — diagnóstico read-only;
- `commission-new-device` — descubrimiento read-only.

Evidencia: `tests/contract/test_skill_contract.py`,
`tests/contract/test_skill_catalog.py` y
`tests/integration/test_core_skill.py`.

### 9.2 DSL versionado y explicación

[`scenario_definition_digest()`](../src/domoai/optimizer/scenario.py:203)
calcula una identidad `sha256:` canónica sobre el escenario normalizado,
independiente del orden de campos. `OptimizationResult` la conserva de forma
aditiva y el servicio/MCP la adjuntan también a resultados inválidos, timeout e
infeasible. `validate_scenario` devuelve la misma identidad.

`explain_solution` ahora proyecta de forma acotada `definition_digest`,
`proposal_count`, alternativas, satisfacción de restricciones duras,
violaciones blandas, confianza de forecast y `next_step`. Un estado no exitoso
no genera `proposal`, y la explicación nunca concede autoridad de ejecución.
Cada alternativa incluye `objective_values`, `constraint_effects` y
`forecast_assumptions`; el resultado puede aportar evidencia específica por
`plan_id` y, cuando solo existe evidencia común del solve, la proyección lo
mantiene explícitamente como evidencia compartida y acotada.
La procedencia existente de `EnergyContext` (`source_revision`,
`observed_at`, unidades y confidence) se conserva como evidencia de entrada;
no se copian payloads de proveedor.

Evidencia: `tests/unit/optimizer/test_scenario_identity.py`,
`tests/unit/test_optimization_explanation.py`,
`tests/unit/application/test_optimization_service.py` y
`tests/contract/test_ortools_mcp_contract.py`. Los schemas v1 fueron
regenerados; se añadieron también los schemas de automatización local.

### 9.3 Automatización local sin LLM

[`src/domoai/domain/automation.py`](../src/domoai/domain/automation.py:1)
define triggers `state_changed` y `time`, condiciones estrictas, acciones
limitadas a diez comandos, scope, cooldown, expiración, consentimiento y
evaluación. [`LocalAutomationEngine`](../src/domoai/application/local_automation.py:1):

1. recibe eventos tipados después de actualizar el estado canónico;
2. rechaza eventos stale, condiciones no satisfechas, consentimiento vencido o
   digest/scope incompatibles;
3. reclama atómicamente `(rule_id, event_id)` en SQLite;
4. crea un plan nuevo, lo valida y solo ejecuta si queda `READY`;
5. deja en `blocked` una policy que requiere confirmación y proyecta
   `unknown` ante fallos de ejecución.

La migración [`013_local_automation.sql`](../src/domoai/persistence/migrations/013_local_automation.sql:1)
conserva lifecycle, consentimiento y último claim tras reinicio. El callback
del `RuntimeEventConsumer` se ejecuta únicamente después de la ingestión de
estado; las reglas horarias se evalúan desde el sweep existente del scheduler.
MCP expone `create_local_automation_rule`, `update_local_automation_rule`,
`list_local_automation_rules` y `set_local_automation_status`. La creación y
actualización exigen una aprobación standing cuyo `recurrence_digest` coincide
con el digest de la regla; actualizar requiere un `approval_id` nuevo, sustituye
el consentimiento anterior, invalida sus claims de evento y audita
`automation_rule_updated`. El listado no expone `approval_id`.

La frontera de eventos normaliza los snapshots ingeridos a identidad canónica
antes de invocar el motor, también para adaptadores que solo entregan
identificadores de transporte. Las evidencias `UNAVAILABLE` o sin valor actual
se descartan como triggers; no se convierten en una coincidencia con `None`.
Las reglas horarias se prueban por minuto local y el identificador de evento se
construye en UTC para que el cooldown y la deduplicación sobrevivan a reinicios
y cambios DST.

Evidencia: `tests/unit/domain/test_automation.py`,
`tests/unit/application/test_local_automation.py`,
`tests/integration/test_local_automation_persistence.py`,
`tests/integration/test_runtime_event_consumer.py` y
`tests/contract/test_local_automation_contract.py`.

## 10. Evaluación de evolución posterior

La coordinación distribuida, pools de workers y exportación remota de métricas
siguen fuera de esta fase. La evaluación concreta y sus precondiciones están en
[`docs/evaluacion-coordinacion-distribuida-pools-metricas.md`](evaluacion-coordinacion-distribuida-pools-metricas.md:1).
La Fase 2 mantiene ejecución local, límites bounded y sin dependencia de
Internet/LLM para las reglas ya aprobadas.

## 11. Criterio de salida de la fase

La fase queda cerrada cuando las tools declaran sus efectos, el catálogo mínimo
de Skills pasa validación offline, el DSL tiene identidad y explicación
estructurada, y las automatizaciones locales tipadas pasan por consentimiento,
persistencia, cooldown, expiración, executor y auditoría sin depender del
agente.

## 12. Specs y revisión de composición

Los artefactos Spec Kit base de esta fase están en:

- [`specs/182-skill-contract-catalog/spec.md`](../specs/182-skill-contract-catalog/spec.md:1), [`plan.md`](../specs/182-skill-contract-catalog/plan.md:1), [`tasks.md`](../specs/182-skill-contract-catalog/tasks.md:1) y checklist.
- [`specs/183-optimization-dsl-explainability/spec.md`](../specs/183-optimization-dsl-explainability/spec.md:1), [`plan.md`](../specs/183-optimization-dsl-explainability/plan.md:1), [`tasks.md`](../specs/183-optimization-dsl-explainability/tasks.md:1) y checklist.
- [`specs/184-local-automation/spec.md`](../specs/184-local-automation/spec.md:1), [`plan.md`](../specs/184-local-automation/plan.md:1), [`tasks.md`](../specs/184-local-automation/tasks.md:1) y checklist.

### Composition Review Report

- **Subsistemas modificados:** contrato/catálogo de Skills, dominio y
  explicación del optimizador, dominio de automatización, SQLite/repositorios,
  consumidor de eventos, scheduler, composición de runtime y herramientas MCP.
- **Vecinos revisados:** `DeviceRegistry`, `DiscoveryService`, `StateStore`,
  `PlanService`, `ExecutionAdmission`, `ApprovalStore`, `PlanExecutor`,
  `RuntimeLifecycle` y exportador de schemas.
- **Invariantes comprobados:** estado canónico antes del trigger; acciones solo
  mediante `PlanService`/executor; digest y scope ligados al consentimiento;
  claim CAS de `(rule_id, event_id)`; cooldown/expiración persistentes; errores
  de ejecución como `UNKNOWN`; resultados del solver siempre proposal-only.
- **Arquitectura:** `uv run python scripts/check_architecture_contracts.py`,
  `uv run lint-imports` y `project-composition-check domoai`: 4 contratos
  conservados, 0 rotos.
- **Escenarios:** éxito, duplicado/replay, cooldown, estado stale o no
  disponible, consentimiento vencido, validación bloqueada, ejecución
  desconocida, reinicio SQLite, trigger horario y evento de adaptador con
  identidad de transporte, actualización de regla con consentimiento nuevo,
  invalidación de claims y rechazo de aprobación reutilizada. La compuerta de
  composición ejecutó 479 pruebas y
  dejó 18 omitidas por dependencias opcionales.
- **Verificación global:** `uv run pytest -q` terminó con `1685 passed, 18
  skipped`; las pruebas de convergencia y sus contratos terminaron con `23
  passed`.
- **Dependencias reales:** SQLite temporal se usó en las pruebas de migración,
  persistencia y reinicio; no hay cola distribuida ni servicio remoto dentro
  del alcance de esta fase.
- **Incidencias corregidas:** listas de catálogo MCP desactualizadas en dos
  contratos globales, falta de normalización canónica para eventos no-KNX y
  las brechas T012/T016 de conformidad; quedaron cubiertas por regresiones.
- **Riesgos residuales:** HIL físico, coordinación distribuida, pools y
  exportación remota de métricas siguen fuera de alcance; 18 pruebas opcionales
  permanecen omitidas cuando no existe su infraestructura.
- **Veredicto de composición:** **PASS WITH RISKS**; los riesgos de interacción
  quedan acotados a las evoluciones posteriores y a la cualificación física.

## 13. Validación posterior de conformidad con los specs

La revisión posterior contrastó los requisitos funcionales con el código real y
detectó dos brechas de convergencia, T012 y T016. Ambas fueron implementadas con
pruebas rojas previas, pruebas de contrato y cobertura de persistencia:

- `specs/183-optimization-dsl-explainability/tasks.md` — T012 cerrado:
  `OptimizationExplanation.alternatives` expone para cada elemento bounded sus
  objetivos comparables, efectos de restricciones y supuestos de forecast.
  `OptimizationResult.alternative_evidence` permite aportar métricas
  específicas por `plan_id`; la explicación usa evidencia común solo como
  fallback explícito y acotado.
- `specs/184-local-automation/tasks.md` — T016 cerrado:
  `update_local_automation_rule` exige un nuevo standing approval enlazado al
  digest de la nueva definición. La persistencia sustituye consentimiento y
  digest, borra claims de evento de la versión anterior y el motor registra
  `automation_rule_updated` sin approval IDs ni secretos.

Las tareas T012 y T016 están marcadas completas en sus `tasks.md`. Verificación
final: `23 passed` en las pruebas de convergencia, `1685 passed, 18 skipped` en
la suite completa, `479 passed, 18 skipped` en composición, Ruff y mypy sin
errores, lint-imports con 4 contratos conservados y 0 rotos, checks de
arquitectura/runtime coherentes, y 63 schemas exportados correctamente.

## 14. Addendum — producto agentic conectado a producción — 2026-09-05

### 14.1 Qué está resuelto

El producto agentic ya tiene el patrón correcto: el agente descubre el modelo
canónico, obtiene estado y contexto energético, solicita una propuesta
solver-neutral, la explica y la entrega al boundary de plan, policy,
approval, commit y readback. Las Skills core declaran bindings semánticos y
prohíben llamadas directas a adapters, vendors o solver code.

La paridad MCP entre clientes y la operación in-process demuestran que el
contrato no depende de un modelo concreto. Esto no equivale todavía a haber
conectado Claude Code y Codex a dos despliegues productivos ni a haber
operado dispositivos físicos desde ambos.

### 14.2 Brechas de producto frente a la visión completa

| Capacidad | Situación | Trabajo pendiente |
|---|---|---|
| Descubrimiento semántico | Implementado con `discover_devices`, estado, energía, runtime y capabilities. | Completar la cobertura de dominios y la ontología compartida cuando aparezcan nuevos tipos de dispositivo. |
| Historial | Implementado `get_history` read-only con almacenamiento histórico, filtros, retención, aislamiento por hogar y privacidad state export/delete. | Añadir agregaciones agentic y comparación temporal solo con contrato de producto explícito. |
| Escenas | Los planes y bundles cubren secuencias validadas. | Añadir `execute_scene` solo si se define su modelo, digest, policy y readback; no crear un atajo al executor. |
| Alternativas | `OptimizationResult` y explicación contienen alternativas internas. | Exponer comparación de escenarios como tool MCP pública si forma parte del producto. |
| Skills | Energía, diagnóstico y commissioning core existen. | Añadir y validar Skills independientes de EV, confort, solar, vacaciones y noche; mantenerlas host-agnostic. |
| Clientes | Existe un MCP general y pruebas de paridad multi-agente. | Probar dos clientes reales contra el mismo gateway autenticado, con scopes y reconexión. |
| Hardware | El agente opera fixtures y gemelo digital. | Validar que sus intenciones llegan a dispositivos físicos cualificados con límites y readback. |

### 14.3 Criterios de aceptación adicionales

- Un agente no debe necesitar conocer fabricante, protocolo, endpoint, cluster,
  registro o entity ID para operar una capability autorizada.
- Una propuesta de OR-Tools debe seguir siendo analysis-only hasta atravesar
  validación, policy, approval y admission.
- Dos clientes MCP deben ver la misma revisión canónica y no poder forjar
  autoridad entre tenants u hogares.
- Una Skill debe detenerse ante estado stale, capability no cualificada,
  approval ausente o fencing inválido.
- Las Skills nuevas deben pasar el validador de contrato portable y no
  duplicar reglas de seguridad en el host.
- Las pruebas de cliente deben cubrir reconexión, timeout, replay, cambio de
  digest y error de backend sin saltar al adapter.

### 14.4 Límite explícito

El producto agentic está listo como software local verificable, no como
autonomía física productiva. La experiencia de usuario solo puede anunciar
“acción realizada” cuando exista readback físico confirmado; una simulación,
un proposal solver o un resultado `scheduled` no deben presentarse como
ejecución completada.

## 15. Verificación de arranque de Fase 2 — 2026-09-05

La implementación existente se revalidó antes de continuar con nuevas
capacidades:

```text
contratos Skills, catálogo, DSL, explicación y automatización local   83 passed
Ruff, mypy, arquitectura, documentación e Import Linter              PASS
suite completa                                                       1799 passed, 18 skipped
```

La fase queda iniciada sobre base software funcional. La siguiente evolución
puede abordar escenas seguras, comparación pública de escenarios, nuevas Skills
o clientes MCP reales. No se añade un atajo al executor ni se presenta
simulación como actuación física.

## 16. Primer incremento implementado — historial agentic — 2026-09-05

`get_history` queda disponible como tool MCP de solo lectura. La ruta persiste
muestras aceptadas junto al snapshot actual y los metadatos de `StateStore` en
la misma transacción SQLite; consulta por dispositivo, capability, ventana
temporal y límite; y usa `privacy_retention_days` para purgar histórico.

La privacidad de `state` exporta y elimina también el histórico del hogar
solicitante. El repositorio no acepta un hogar arbitrario desde la tool, y una
consulta no ejecuta discovery, refresh, adapter, plan ni mutación de estado.

## 17. Verificación del incremento — 2026-09-05

```text
historial + privacidad + contratos MCP + compatibilidad runtime   57 passed
project-composition-check                                       508 passed, 18 skipped
Ruff, mypy, Import Linter, docs contract check, diff --check     PASS
suite completa                                                  1804 passed, 18 skipped
```

La regresión del scheduler quedó corregida: la siguiente ocurrencia se calcula
desde el instante real del barrido, no desde el instante programado ya vencido.

## 18. Cierre software de las brechas agentic — 2026-09-05

Se cerraron las brechas locales de comparación, escenas, Skills y paridad MCP:

- `compare_scenarios` expone comparación contrafactual bounded/read-only. Usa
  el worker limitado, valida el escenario contra el registro canónico y no
  inventa diffs cuando baseline o variación son inviables.
- `execute_scene` recibe una escena ordenada con `scene_digest`, revisión,
  `bundle_digest` y miembros validados. Verifica scope y revisión y delega a
  `BundleCommitService`; la idempotencia sigue siendo la del agregado bundle y
  el resultado incluye estado/readback por miembro.
- El catálogo pasa a nueve Skills v4 portables: energía, EV, confort térmico,
  solar, batería, noche, vacaciones, diagnóstico y commissioning. Las nuevas
  Skills declaran contexto, tools/resources permitidos, frescura, approval,
  fallo `UNKNOWN` y rutas prohibidas.
- El probe de clientes exige también `compare_scenarios`; las pruebas de
  gateway cubren clientes autenticados concurrentes, identidad/scope por
  request, cancelación, reconexión y paridad de revisión/catalogue digest.

Evidencia focal:

```text
compare + counterfactual async + Skills v4                  PASS
execute_scene: idempotencia, digest y stale revision        2 passed
gateway client parity/scope/reconnect                        PASS
```

El gate externo sigue siendo explícito: sin hardware cualificado, identidad
de dispositivo, HIL atendido, fencing y readback físico independiente, la
comprobación de commissioning permanece bloqueada/fail-closed. Fixtures y
gemelo digital prueban composición de software, no commissioning productivo.
