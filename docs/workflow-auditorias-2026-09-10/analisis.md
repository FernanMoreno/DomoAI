# Análisis Spec Kit

Análisis cruzado de spec.md, plan.md y tasks.md, publicado tras revisión de solo lectura. Los 24 check-prerequisites reales terminaron con exit 0; resultado en speckit-prerequisites.json. Cobertura significa trazabilidad documental, no requisito implementado.

| ID | Categoría | Severidad | Ubicación | Resultado / acción futura |
|---|---|---|---|---|
| U-01 | Parámetros operativos | Media | P-07 research.md / T002 | Diseño concretado en P-07 contracts/pilot-profile.md. Calibración con replay y cumplimiento de cuotas durables pendientes; no hay valor universal demostrado. |
| U-02 | Cualificación externa | Media | P-10 spec.md / T001–T006 | Matriz y regla de bloqueo por versión ausente concretadas en P-10 contracts/qualification-matrix.md. Filas Codex CLI/Claude Code/genérico siguen pendientes; ninguna compatibilidad real nueva acreditada. |
| U-03 | Operación | Media | P-12 research.md / T002 | Retención, restauración aislada, revocación posterior al backup y medición RPO/RTO concretadas en P-12 contracts/pilot-operations.md. Ensayo real pendiente; compatibilidad con retención única e inmutabilidad explicitada. |

No se detecta infracción constitucional en los contratos propuestos. Propiedad runtime, autorización, aislamiento, auditoría y pruebas primero están explícitos. No hay requisito sin referencia a tarea. T001–T003 y T012–T014 son tareas habilitadoras/de verificación, justificadas por workflow y SC-003; no son trabajo huérfano.

Las convenciones A-01 (DST) y A-11 (origen/almacenamiento final), supersesiones A-05/A-09 y división mínima/completa P-10 quedaron explícitas antes de esta revisión. Planes siguen siendo propuestas: ningún resultado de análisis sustituye las pruebas de implementación.

## Métricas

24 paquetes; 135 requisitos; 336 tareas pendientes; cobertura de referencias 100%; 0 ciclos en DAG con hitos; 0 conflictos críticos identificados; 3 precisiones de diseño concretadas; 3 validaciones empíricas pendientes. Repetición de gates/seguridad entre paquetes es intencionada porque se cierran independientemente, no subsistemas duplicados.

## Cobertura

| Requisito | Tarea presente | IDs | Estado |
|---|---|---|---|
| A-01/FR-001 | Sí | T004/T007 | Pendiente de implementación |
| A-01/FR-002 | Sí | T005/T007 | Pendiente de implementación |
| A-01/FR-003 | Sí | T006/T007 | Pendiente de implementación |
| A-01/FR-004 | Sí | T008/T011 | Pendiente de implementación |
| A-01/FR-005 | Sí | T004–T006/T011 | Pendiente de implementación |
| A-02/FR-001 | Sí | T004/T007 | Pendiente de implementación |
| A-02/FR-002 | Sí | T005/T007 | Pendiente de implementación |
| A-02/FR-003 | Sí | T006/T007 | Pendiente de implementación |
| A-02/FR-004 | Sí | T008/T011 | Pendiente de implementación |
| A-02/FR-005 | Sí | T004–T006/T011 | Pendiente de implementación |
| A-03/FR-001 | Sí | T004/T007 | Pendiente de implementación |
| A-03/FR-002 | Sí | T005/T007 | Pendiente de implementación |
| A-03/FR-003 | Sí | T006/T007 | Pendiente de implementación |
| A-03/FR-004 | Sí | T008/T011 | Pendiente de implementación |
| A-03/FR-005 | Sí | T004–T006/T011 | Pendiente de implementación |
| A-04/FR-001 | Sí | T004/T007 | Pendiente de implementación |
| A-04/FR-002 | Sí | T005/T007 | Pendiente de implementación |
| A-04/FR-003 | Sí | T006/T007 | Pendiente de implementación |
| A-04/FR-004 | Sí | T008/T011 | Pendiente de implementación |
| A-04/FR-005 | Sí | T004–T006/T011 | Pendiente de implementación |
| A-05/FR-001 | Sí | T004/T007 | Pendiente de implementación |
| A-05/FR-002 | Sí | T005/T007 | Pendiente de implementación |
| A-05/FR-003 | Sí | T006/T007 | Pendiente de implementación |
| A-05/FR-004 | Sí | T008/T011 | Pendiente de implementación |
| A-05/FR-005 | Sí | T004–T006/T011 | Pendiente de implementación |
| A-06/FR-001 | Sí | T004/T007 | Pendiente de implementación |
| A-06/FR-002 | Sí | T005/T007 | Pendiente de implementación |
| A-06/FR-003 | Sí | T006/T007 | Pendiente de implementación |
| A-06/FR-004 | Sí | T008/T011 | Pendiente de implementación |
| A-06/FR-005 | Sí | T004–T006/T011 | Pendiente de implementación |
| A-07/FR-001 | Sí | T004/T007 | Pendiente de implementación |
| A-07/FR-002 | Sí | T005/T007 | Pendiente de implementación |
| A-07/FR-003 | Sí | T006/T007 | Pendiente de implementación |
| A-07/FR-004 | Sí | T008/T011 | Pendiente de implementación |
| A-07/FR-005 | Sí | T004–T006/T011 | Pendiente de implementación |
| A-08/FR-001 | Sí | T004/T007 | Pendiente de implementación |
| A-08/FR-002 | Sí | T005/T007 | Pendiente de implementación |
| A-08/FR-003 | Sí | T006/T007 | Pendiente de implementación |
| A-08/FR-004 | Sí | T008/T011 | Pendiente de implementación |
| A-08/FR-005 | Sí | T004–T006/T011 | Pendiente de implementación |
| A-09/FR-001 | Sí | T004/T007 | Pendiente de implementación |
| A-09/FR-002 | Sí | T005/T007 | Pendiente de implementación |
| A-09/FR-003 | Sí | T006/T007 | Pendiente de implementación |
| A-09/FR-004 | Sí | T008/T011 | Pendiente de implementación |
| A-09/FR-005 | Sí | T004–T006/T011 | Pendiente de implementación |
| A-10/FR-001 | Sí | T004/T007 | Pendiente de implementación |
| A-10/FR-002 | Sí | T005/T007 | Pendiente de implementación |
| A-10/FR-003 | Sí | T006/T007 | Pendiente de implementación |
| A-10/FR-004 | Sí | T008/T011 | Pendiente de implementación |
| A-10/FR-005 | Sí | T004–T006/T011 | Pendiente de implementación |
| A-11/FR-001 | Sí | T004/T007 | Pendiente de implementación |
| A-11/FR-002 | Sí | T005/T007 | Pendiente de implementación |
| A-11/FR-003 | Sí | T006/T007 | Pendiente de implementación |
| A-11/FR-004 | Sí | T008/T011 | Pendiente de implementación |
| A-11/FR-005 | Sí | T004–T006/T011 | Pendiente de implementación |
| A-12/FR-001 | Sí | T004/T007 | Pendiente de implementación |
| A-12/FR-002 | Sí | T005/T007 | Pendiente de implementación |
| A-12/FR-003 | Sí | T006/T007 | Pendiente de implementación |
| A-12/FR-004 | Sí | T008/T011 | Pendiente de implementación |
| A-12/FR-005 | Sí | T004–T006/T011 | Pendiente de implementación |
| P-01/FR-001 | Sí | T004/T007 | Pendiente de implementación |
| P-01/FR-002 | Sí | T005/T007 | Pendiente de implementación |
| P-01/FR-003 | Sí | T006/T007 | Pendiente de implementación |
| P-01/FR-004 | Sí | T008/T011 | Pendiente de implementación |
| P-01/FR-005 | Sí | T004–T006/T011 | Pendiente de implementación |
| P-01/FR-006 | Sí | T009 | Pendiente de implementación |
| P-02/FR-001 | Sí | T004/T007 | Pendiente de implementación |
| P-02/FR-002 | Sí | T005/T007 | Pendiente de implementación |
| P-02/FR-003 | Sí | T006/T007 | Pendiente de implementación |
| P-02/FR-004 | Sí | T008/T011 | Pendiente de implementación |
| P-02/FR-005 | Sí | T004–T006/T011 | Pendiente de implementación |
| P-02/FR-006 | Sí | T009 | Pendiente de implementación |
| P-03/FR-001 | Sí | T004/T007 | Pendiente de implementación |
| P-03/FR-002 | Sí | T005/T007 | Pendiente de implementación |
| P-03/FR-003 | Sí | T006/T007 | Pendiente de implementación |
| P-03/FR-004 | Sí | T008/T011 | Pendiente de implementación |
| P-03/FR-005 | Sí | T004–T006/T011 | Pendiente de implementación |
| P-03/FR-006 | Sí | T009 | Pendiente de implementación |
| P-04/FR-001 | Sí | T004/T007 | Pendiente de implementación |
| P-04/FR-002 | Sí | T005/T007 | Pendiente de implementación |
| P-04/FR-003 | Sí | T006/T007 | Pendiente de implementación |
| P-04/FR-004 | Sí | T008/T011 | Pendiente de implementación |
| P-04/FR-005 | Sí | T004–T006/T011 | Pendiente de implementación |
| P-04/FR-006 | Sí | T009 | Pendiente de implementación |
| P-05/FR-001 | Sí | T004/T007 | Pendiente de implementación |
| P-05/FR-002 | Sí | T005/T007 | Pendiente de implementación |
| P-05/FR-003 | Sí | T006/T007 | Pendiente de implementación |
| P-05/FR-004 | Sí | T008/T011 | Pendiente de implementación |
| P-05/FR-005 | Sí | T004–T006/T011 | Pendiente de implementación |
| P-05/FR-006 | Sí | T009 | Pendiente de implementación |
| P-06/FR-001 | Sí | T004/T007 | Pendiente de implementación |
| P-06/FR-002 | Sí | T005/T007 | Pendiente de implementación |
| P-06/FR-003 | Sí | T006/T007 | Pendiente de implementación |
| P-06/FR-004 | Sí | T008/T011 | Pendiente de implementación |
| P-06/FR-005 | Sí | T004–T006/T011 | Pendiente de implementación |
| P-06/FR-006 | Sí | T009 | Pendiente de implementación |
| P-07/FR-001 | Sí | T004/T007 | Pendiente de implementación |
| P-07/FR-002 | Sí | T005/T007 | Pendiente de implementación |
| P-07/FR-003 | Sí | T006/T007 | Pendiente de implementación |
| P-07/FR-004 | Sí | T008/T011 | Pendiente de implementación |
| P-07/FR-005 | Sí | T004–T006/T011 | Pendiente de implementación |
| P-07/FR-006 | Sí | T009 | Pendiente de implementación |
| P-08/FR-001 | Sí | T004/T007 | Pendiente de implementación |
| P-08/FR-002 | Sí | T005/T007 | Pendiente de implementación |
| P-08/FR-003 | Sí | T006/T007 | Pendiente de implementación |
| P-08/FR-004 | Sí | T008/T011 | Pendiente de implementación |
| P-08/FR-005 | Sí | T004–T006/T011 | Pendiente de implementación |
| P-08/FR-006 | Sí | T009 | Pendiente de implementación |
| P-09/FR-001 | Sí | T004/T007 | Pendiente de implementación |
| P-09/FR-002 | Sí | T005/T007 | Pendiente de implementación |
| P-09/FR-003 | Sí | T006/T007 | Pendiente de implementación |
| P-09/FR-004 | Sí | T008/T011 | Pendiente de implementación |
| P-09/FR-005 | Sí | T004–T006/T011 | Pendiente de implementación |
| P-09/FR-006 | Sí | T009 | Pendiente de implementación |
| P-10/FR-001 | Sí | T004/T007 | Pendiente de implementación |
| P-10/FR-002 | Sí | T009/T010 | Pendiente de implementación |
| P-10/FR-003 | Sí | T006/T007 | Pendiente de implementación |
| P-10/FR-004 | Sí | T008/T011 | Pendiente de implementación |
| P-10/FR-005 | Sí | T004–T006/T011 | Pendiente de implementación |
| P-10/FR-006 | Sí | T009/T010 | Pendiente de implementación |
| P-11/FR-001 | Sí | T004/T007 | Pendiente de implementación |
| P-11/FR-002 | Sí | T005/T007 | Pendiente de implementación |
| P-11/FR-003 | Sí | T006/T007 | Pendiente de implementación |
| P-11/FR-004 | Sí | T008/T011 | Pendiente de implementación |
| P-11/FR-005 | Sí | T004–T006/T011 | Pendiente de implementación |
| P-11/FR-006 | Sí | T009 | Pendiente de implementación |
| P-12/FR-001 | Sí | T004/T007 | Pendiente de implementación |
| P-12/FR-002 | Sí | T005/T007 | Pendiente de implementación |
| P-12/FR-003 | Sí | T006/T007 | Pendiente de implementación |
| P-12/FR-004 | Sí | T008/T011 | Pendiente de implementación |
| P-12/FR-005 | Sí | T004–T006/T011 | Pendiente de implementación |
| P-12/FR-006 | Sí | T009 | Pendiente de implementación |

## Siguiente paso

Mantener parada antes de desarrollar. Cuando se autorice un paquete, revalidar evidencia y concretar precisiones que le afecten en T001/T002, ejecutar RED/GREEN y gates de composición. No ejecutar speckit-implement desde esta entrega. Si cambia un contrato significativo, actualizar spec → plan → tasks y repetir análisis/impacto antes de código.

## Continuación: FR-007

| Requisito | Tarea presente | IDs | Estado |
|---|---|---|---|
| P-07/FR-007 | Sí | T002/T009 | Diseño definido; implementación/calibración pendientes |
| P-10/FR-007 | Sí | T002/T004/T009 | Matriz definida; filas pendientes |
| P-12/FR-007 | Sí | T002/T009 | Procedimiento definido; ensayo pendiente |

Los resultados históricos anteriores permanecen en secuencia.jsonl. No se reinterpreta cualificación pendiente como realizada.
