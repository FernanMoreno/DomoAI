# Preparación de ambas auditorías con AI_WORKFLOW

**Estado: preparación previa a implementación; no se ha desarrollado código.** Las auditorías originales se conservan como evidencia. Los defectos continúan abiertos y las funcionalidades continúan pendientes.

Entradas: [auditoría técnica](../auditoria-integral-2026-09-10/README.md) y [auditoría de producto](../auditoria-producto-2026-09-10/README.md). Autoridad: [AI_WORKFLOW.md](../../AI_WORKFLOW.md). Usuario exige aplicar el proceso y detenerse antes de desarrollar.

## Secuencia aplicada

1. Inspeccionar git status y preservar trabajo existente. Técnico: `bug-significant`; producto: `significant`.
2. Decisión de conocimiento: consultar restricciones durables de saga 087 y autonomía temporal en vault compartido; conservar runtime único, resultados parciales/desconocidos y separación entre simulación y hardware.
3. Crear un paquete Spec Kit por incremento: spec → plan → tasks. Se ejecutaron los scripts reales setup-plan/setup-tasks con `SPECIFY_FEATURE_DIRECTORY`; sin ramas ni commits nuevos.
4. Consultar Graphify y calcular vecinos por paquete. Inspeccionar fuente después, contrastar límites y registrar hashes.
5. Para los 12 defectos: systematic-debugging, reproducción nueva y causa raíz. Para producto: este paso de bugs no aplica; son requisitos nuevos, no fallos reproducidos.
6. Registrar decisión TDD por paquete. Ningún test nuevo ni corrección implementada.
7. Analizar consistencia documental, dependencias y cobertura. Revisar diff y preservación antes de entregar esta preparación.

La cronología real está en [secuencia.jsonl](secuencia.jsonl). Los checks históricos de las auditorías no se usan para acreditar el nuevo orden ni una corrección. Las refinaciones documentales posteriores a inspección se registran, sin mutación de implementación.

## Paquetes

| ID | Especificación | Ejecución futura | Dependencias base |
|---|---|---|---|
| A-01 | [Aislamiento de ocurrencias horarias](../../specs/228-time-rule-isolation/spec.md) | [Plan](../../specs/228-time-rule-isolation/plan.md) · [Tareas](../../specs/228-time-rule-isolation/tasks.md) | — |
| A-02 | [Cancelación anterior al despacho](../../specs/229-queued-dispatch-cancellation/spec.md) | [Plan](../../specs/229-queued-dispatch-cancellation/plan.md) · [Tareas](../../specs/229-queued-dispatch-cancellation/tasks.md) | — |
| A-03 | [Cierre durable después de ACK](../../specs/230-intent-terminal-transitions/spec.md) | [Plan](../../specs/230-intent-terminal-transitions/plan.md) · [Tareas](../../specs/230-intent-terminal-transitions/tasks.md) | — |
| A-04 | [Readiness de tareas obligatorias](../../specs/231-named-runtime-readiness/spec.md) | [Plan](../../specs/231-named-runtime-readiness/plan.md) · [Tareas](../../specs/231-named-runtime-readiness/tasks.md) | — |
| A-05 | [Administración explícita de conexiones](../../specs/232-scoped-connection-administration/spec.md) | [Plan](../../specs/232-scoped-connection-administration/plan.md) · [Tareas](../../specs/232-scoped-connection-administration/tasks.md) | — |
| A-06 | [Estados parciales de Home Assistant](../../specs/233-ha-partial-state-normalization/spec.md) | [Plan](../../specs/233-ha-partial-state-normalization/plan.md) · [Tareas](../../specs/233-ha-partial-state-normalization/tasks.md) | — |
| A-07 | [Áreas visibles según autoridad](../../specs/234-scoped-area-inventory/spec.md) | [Plan](../../specs/234-scoped-area-inventory/plan.md) · [Tareas](../../specs/234-scoped-area-inventory/tasks.md) | A-05 |
| A-08 | [Disponibilidad efectiva de capacidades](../../specs/235-effective-route-coverage/spec.md) | [Plan](../../specs/235-effective-route-coverage/plan.md) · [Tareas](../../specs/235-effective-route-coverage/tasks.md) | A-07 |
| A-09 | [Demanda base en contexto energético](../../specs/236-composed-base-demand/spec.md) | [Plan](../../specs/236-composed-base-demand/plan.md) · [Tareas](../../specs/236-composed-base-demand/tasks.md) | — |
| A-10 | [Comparación de baseline sin actuación](../../specs/237-passive-scenario-comparison/spec.md) | [Plan](../../specs/237-passive-scenario-comparison/plan.md) · [Tareas](../../specs/237-passive-scenario-comparison/tasks.md) | — |
| A-11 | [Autoconsumo con origen energético](../../specs/238-solar-flow-provenance/spec.md) | [Plan](../../specs/238-solar-flow-provenance/plan.md) · [Tareas](../../specs/238-solar-flow-provenance/tasks.md) | A-09 |
| A-12 | [Coherencia monetaria](../../specs/239-energy-currency-integrity/spec.md) | [Plan](../../specs/239-energy-currency-integrity/plan.md) · [Tareas](../../specs/239-energy-currency-integrity/tasks.md) | A-10 |
| P-01 | [Puesta en marcha doméstica](../../specs/240-household-onboarding/spec.md) | [Plan](../../specs/240-household-onboarding/plan.md) · [Tareas](../../specs/240-household-onboarding/tasks.md) | A-05, A-06, A-08 |
| P-02 | [Perfil doméstico durable](../../specs/241-household-preferences/spec.md) | [Plan](../../specs/241-household-preferences/plan.md) · [Tareas](../../specs/241-household-preferences/tasks.md) | P-01 |
| P-03 | [Tareas domésticas persistentes](../../specs/242-durable-household-tasks/spec.md) | [Plan](../../specs/242-durable-household-tasks/plan.md) · [Tareas](../../specs/242-durable-household-tasks/tasks.md) | P-02, P-04, A-02, A-03 |
| P-04 | [Consentimiento comprensible](../../specs/243-household-consent/spec.md) | [Plan](../../specs/243-household-consent/plan.md) · [Tareas](../../specs/243-household-consent/tasks.md) | P-02, A-02, A-05 |
| P-05 | [Estado diario y avisos](../../specs/244-daily-home-status/spec.md) | [Plan](../../specs/244-daily-home-status/plan.md) · [Tareas](../../specs/244-daily-home-status/tasks.md) | P-03, P-08, A-07 |
| P-06 | [Coste y ahorro medido](../../specs/245-measured-energy-savings/spec.md) | [Plan](../../specs/245-measured-energy-savings/plan.md) · [Tareas](../../specs/245-measured-energy-savings/tasks.md) | P-02, A-09, A-10, A-11, A-12 |
| P-07 | [Replanificación doméstica](../../specs/246-adaptive-household-planning/spec.md) | [Plan](../../specs/246-adaptive-household-planning/plan.md) · [Tareas](../../specs/246-adaptive-household-planning/tasks.md) | P-03, P-04, P-06, P-08, A-04 |
| P-08 | [Prioridad manual y conflictos](../../specs/247-manual-priority/spec.md) | [Plan](../../specs/247-manual-priority/plan.md) · [Tareas](../../specs/247-manual-priority/tasks.md) | P-03, A-01, A-02 |
| P-09 | [Rutinas por intención](../../specs/248-household-routines/spec.md) | [Plan](../../specs/248-household-routines/plan.md) · [Tareas](../../specs/248-household-routines/tasks.md) | P-03, P-04, P-08, A-01 |
| P-10 | [Clientes MCP y evaluación](../../specs/249-agent-client-qualification/spec.md) | [Plan](../../specs/249-agent-client-qualification/plan.md) · [Tareas](../../specs/249-agent-client-qualification/tasks.md) | A-05, A-07 |
| P-11 | [Cobertura verificable](../../specs/250-device-coverage-evidence/spec.md) | [Plan](../../specs/250-device-coverage-evidence/plan.md) · [Tareas](../../specs/250-device-coverage-evidence/tasks.md) | A-08 |
| P-12 | [Operación, privacidad y traspaso](../../specs/251-household-operations/spec.md) | [Plan](../../specs/251-household-operations/plan.md) · [Tareas](../../specs/251-household-operations/tasks.md) | P-02, P-03, P-04, P-05, P-06 |

Hay 24 paquetes (228–251), 336 tareas futuras y 135 requisitos funcionales trazados. Todos los checkboxes de tasks.md están pendientes. Cada paquete incluye investigación, modelo, contrato, validación futura y decisión TDD; los técnicos incluyen diagnóstico.

## Orden de entrega propuesto

Primero cerrar correcciones técnicas de ejecución/autoridad/readiness y después las energéticas; antes de cualquier piloto actuador, verificar las correcciones aplicables, sin confundir preparación con autorización física.

P-10 **mínimo** (conexión/lectura) y P-11 **mínimo** (cobertura) preceden a P-01. Después P-02 → P-04 → P-03 → P-08 → P-05/P-09. P-06 consume P-02 y A-09…A-12. P-07 consume P-03/P-04/P-06/P-08. P-12 integra recuperación de los nuevos datos. P-10 **completo** espera P-03/P-04; nunca se hace depender P-01 de ese cierre completo. P-11 expansión requiere evidencia propia y no bloquea cobertura mínima. [Reutilización y contratos supersedidos](reutilizacion.md).

## Evidencia y límites

- [Impacto Graphify por paquete](graphify-impact.json) y [consulta](graphify-query.txt). Consulta amplia truncada; vecinos completos calculados por archivo, sin inferir exhaustividad del texto truncado.
- [Inventario de fuente y hashes](source-inspection.json), [causas raíz](diagnostico.md) y [ejecuciones individuales](reproducciones.json).
- [Análisis Spec Kit](analisis.md) y [verificación documental](verificacion.md).

Las reproducciones asertan anomalías conocidas: exit 0 confirma que el defecto **persiste**. No hay ciclo RED/GREEN de corrección. No se ha probado hardware ni nuevos clientes reales.

## Punto de parada obligatorio

`implementation` está pendiente por instrucción del usuario. Por tanto `tests-run → composition-check → composition-review → diff-review → graphify-refresh-decision → knowledge-persist-decision → verification` **de implementación** permanece pendiente y no genera signoff. Las verificaciones documentales de esta entrega no sustituyen esos gates.

Al autorizar desarrollo, seleccionar paquete y revalidar fuente/dependencias antes de TDD. Ejecutar Import Linter, contratos, integración y `project-composition-check "$(cat .ai/project-name)"` cuando corresponda, con dependencias reales aisladas si mocks pueden ocultar el fallo. Aplicar system-composition-review tras esas pruebas, no solo revisión de archivos individuales. No anunciar ningún gate que no haya terminado realmente.

No se persiste una copia de specs/logs/Graphify en Obsidian: repositorio conserva planificación pendiente; las restricciones durables consultadas ya existen en el vault. No hay nueva decisión arquitectónica demostrada que requiera duplicarla.

## Continuación de precisiones

[Perfil de replanificación](../../specs/246-adaptive-household-planning/contracts/pilot-profile.md), [matriz de clientes](../../specs/249-agent-client-qualification/contracts/qualification-matrix.md) y [operación del piloto](../../specs/251-household-operations/contracts/pilot-operations.md) concretan U-01/U-02/U-03. Validación empírica sigue pendiente. Véase [seguimiento](refinamiento.md).
