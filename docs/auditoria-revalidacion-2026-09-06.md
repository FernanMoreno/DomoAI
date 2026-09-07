# Revalidación arquitectónica de DomoAI — 2026-09-06

## Alcance y método

Esta revalidación contrasta la arquitectura objetivo de *Universal Domotics
Runtime + MCP semántico + optimización proposal-only* contra el worktree que
se encuentra disponible el 2026-09-06. No reemplaza las auditorías por fase:
consolida su lectura con el código actual y deja claro el nivel de evidencia de
cada afirmación.

Superficie inspeccionada: 180 módulos Python de `src/domoai`, 324 módulos de
prueba, 82 schemas JSON v1, configuración, despliegue, Skills y evidencias de
laboratorio. Se usó Graphify para localizar las fronteras y la lectura del
código para confirmar los recorridos importantes. El worktree contiene un
conjunto amplio de cambios sin confirmar; por ello esta auditoría describe el
estado del árbol, no una versión Git publicable ni una garantía de que todos
esos cambios pertenezcan a un único incremento atómico.

## Dictamen ejecutivo

La dirección arquitectónica propuesta es correcta y está materializada en gran
medida. DomoAI no usa MCP como bus de sensores: MCP es el borde agente y el
runtime conserva estado, eventos, políticas, planificación, autoridad y
ejecución. El optimizador no puede escribir sobre los adapters. La separación
que conviene conservar no es dos autoridades físicas, sino dos catálogos
lógicos de herramientas sobre un único runtime.

El proyecto es apto para continuar una provisión controlada y para operar el
laboratorio/software cualificado. No debe declararse apto para autonomía física
productiva: faltan pruebas independientes de commissioning y hardware para los
actuadores de riesgo, y el despliegue activo-activo sigue explícitamente
inhabilitado.

## Arquitectura verificada

```text
clientes MCP (Codex, Claude y compatibles)
                 |
          FastMCP unificado
      domótica semántica + OR-Tools
                 |
      PlanService / policy / admission
                 |
       executor / readback / auditoría
                 |
 registry + StateStore + event consumer + scheduler
                 |
       CompositeAdapter / Provider SDK
                 |
 HA | Matter | Zigbee2MQTT | KNX | Modbus | fixtures
```

| Afirmación de diseño | Evidencia de código | Resultado |
|---|---|---|
| Un modelo universal evita APIs de fabricante al agente. | `domain/models.py` define `Device`, `Capability`, `Command` y `StateSnapshot`; los mappers de cada adapter traducen antes de llegar al dominio. | Confirmado. |
| MCP no es el runtime ni un bus de eventos. | `mcp/unified_server.py` registra tools sobre contextos compartidos; `application/event_consumer.py`, `runtime/state_store.py` y `application/local_automation.py` resuelven estado/eventos sin requerir LLM. | Confirmado. |
| Domótica y optimización comparten una sola autoridad física. | `UnifiedMcpContext.__post_init__` exige el mismo registry y `PlanService`; `create_unified_server()` publica ambos catálogos en una conexión. | Confirmado. |
| OR-Tools es proposal-only. | `mcp/ortools_server.py` sólo valida, optimiza, explica, resume y compara; el resultado se entrega como `Plan` para validación posterior. | Confirmado. |
| Un plan no salta policy, consentimiento y admission. | `PlanService`, `ExecutionAdmission`, `PlanExecutor`, `DynamicSafetyGuard`, `SafetyKernel` y el readback forman el camino de escritura. | Confirmado, con gates físicas pendientes. |
| Los conectores internos permanecen sustituibles sin multiplicar la superficie pública. | Provider SDK v1, manifests/conformance y `CompositeAdapter`; la arquitectura bloquea imports cruzados de protocolos y mantiene un único adaptador universal. | Confirmado. |

## Hallazgos decisivos

1. **Mantener un único servidor público es preferible a dos procesos MCP que
   ejecuten por separado.** El servidor unificado ya protege contra la deriva:
   ambos contextos deben compartir registry y servicio de planes. Si se
   publicaran dos endpoints por ergonomía, ambos deben delegar al mismo
   deployment, persistencia, approval store, fencing y executor; nunca deben
   abrir adapters distintos.
2. **La DSL de optimización es el contrato adecuado.** `OptimizationScenario`
   y CP-SAT encapsulan horizonte, objetivos, cargas y restricciones. Exponer
   Python arbitrario anularía validación, límites de horizonte, determinismo,
   trazabilidad y separación de autoridad.
3. **El modelo de capabilities es el activo central.** La normalización de HA,
   Matter, KNX, Modbus y Zigbee2MQTT converge en el mismo `DeviceRegistry` y
   valida unidades, rangos, escritura y garantías de capability antes de
   ejecutar.
4. **La seguridad está en la ruta correcta.** Las tools distinguen preview de
   prepare; ejecución/schedule atraviesan admission; la autoridad se limita por
   tenant/hogar/área/dispositivo/capability/operación; las acciones de alto
   riesgo requieren confirmación y los actuadores energético-físicos requieren
   binding server-owned y readback.
5. **La automatización rutinaria puede sobrevivir al agente.**
   `LocalAutomationEngine` persiste reglas, consentimiento, deduplicación y
   cooldown. Es una base válida para que los eventos no hagan un viaje
   sensor → LLM → sensor.

## Riesgos y límites que no deben ocultarse

| Riesgo | Estado observado | Decisión de auditoría |
|---|---|---|
| Batería, EV, HVAC, cerraduras y otras escrituras físicas de riesgo | El preflight conserva HIL, provider externo y commissioning live como `blocked_external_dependency`. | Fail-closed hasta evidencia independiente reproducible. |
| Active-active / failover distribuido | Hay contratos de lease/fencing y laboratorios; producción exige etcd, PostgreSQL, mTLS y adapter fencing-aware. | Mantener single-writer por deployment. |
| Verdad del estado físico | StateStore ordena cursores, persiste antes de instalar y el executor aplica freshness/readback, pero la radio, cableado y firmware reales no están cualificados. | No traducir éxito de fixture a éxito físico. |
| Evolución del catálogo MCP | Hay 27 tools domóticas y 5 de optimización, semánticas pero ya densas. | Agrupar procedimientos complejos en Skills; evitar tools vendor-specific y duplicados de ciclo de vida. |
| Worktree no atómico | Hay cambios rastreados y no rastreados de múltiples subsistemas y datos SQLite/WAL modificados. | Revisar y dividir cambios por incremento antes de commit o despliegue. |

## Verificaciones frescas de esta revalidación

```text
uv run python scripts/check_architecture_contracts.py  PASS
uv run lint-imports                                    4 contratos kept, 0 broken
uv run python scripts/check_runtime_contract_docs.py   PASS
```

Los contratos confirmados son: núcleo de dominio independiente, adapters de
protocolo independientes entre sí, capas de composición con dependencias hacia
abajo y paquetes hermanos acíclicos. La suite completa se debe interpretar
junto con el resultado que produzca la ejecución del worktree actual; ningún
resultado histórico sustituye esa evidencia.

## Fases de ejecución y documentos de detalle

La planificación y la auditoría detallada se mantienen separadas para que una
fase posterior no relaje gates anteriores:

1. [Fase 0 — seguridad e integridad](auditoria-fase-0-seguridad-integridad.md):
   autoridad, aprobaciones, orden de estado, persistencia y auditoría.
2. [Fase 1 — robustez operativa](auditoria-fase-1-robustez-operativa.md):
   deadlines, liveness, recovery, métricas y laboratorio.
3. [Fase 2 — producto agentic](auditoria-fase-2-producto-agentic.md):
   Skills, DSL, propuesta/explicación, escenas y automatización local.
4. [Fase 3 — escalabilidad e identidad](auditoria-fase-3-escalabilidad-identidad.md):
   tenant/hogar/principal, persistencia compatible, coordinación y fencing.
5. [Fase 4 — ventaja diferencial](auditoria-fase-4-ventaja-diferencial.md):
   qualification de capabilities, commissioning, privacidad y producto.

El índice [Auditoría integral](auditoria-domoai.md) mantiene la matriz de
hallazgos y sus referencias de implementación. La secuencia obligatoria para
acciones físicas sigue siendo: propuesta → validación → policy/approval →
admission → ejecución → readback → auditoría; una Skill o un cliente MCP no
obtiene ninguna excepción a ese recorrido.
