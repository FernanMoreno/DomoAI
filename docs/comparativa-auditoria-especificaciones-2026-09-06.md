# Comparativa: auditoría original, Specs ejecutadas y estado actual

**Fecha:** 2026-09-06  
**Propósito:** evitar reevaluar DomoAI como si fuese el prototipo descrito al
inicio. Esta comparación separa los hallazgos de la auditoría inicial, los
incrementos Spec Kit que los trataron y la evidencia que existe hoy.

## Base comparada

- Auditoría consolidada: [auditoria-domoai.md](auditoria-domoai.md) y sus
  fases 0–4.
- Implementación actual: `src/domoai/`, schemas v1, despliegue y laboratorios.
- Planificación ejecutada: 193 directorios de `specs/`; 181 contienen
  `tasks.md`. En los checklists hay **3 tareas no marcadas**, todas externas:
  Spec 133 (HIL real), Spec 140 (ampliación de dependencias reales) y Spec 141
  (consumer contract con provider desplegado independiente).
- Evidencia: tests de unidad/contrato/integración/composición, laboratorio,
  preflight de Fase 4 y comprobaciones de arquitectura.

Una casilla marcada no se considera por sí sola una prueba de producción. Para
esta comparación, **cerrado en software** significa que hay código y regresión
local; **cualificado en laboratorio** incluye una dependencia o gemelo
reproducible; **bloqueado externo** requiere equipo, servicio o proveedor que
no existe dentro del repositorio.

## Del diseño propuesto a lo construido

| Propuesta inicial | Estado actual | Matiz importante |
|---|---|---|
| Runtime universal por debajo de MCP | Implementado: registry, StateStore, policy, admission, executor, scheduler y adapters están fuera de MCP. | MCP no es el bus domótico ni el dueño del estado. |
| Dos MCP lógicos: domótica y OR-Tools | Implementado como un **servidor MCP unificado** con ambos catálogos y un runtime común. | Es una mejora de seguridad: dos procesos públicos no deben crear dos autoridades físicas. |
| Modelo universal de dispositivos/capabilities | Implementado en dominio, Provider SDK y adapters de HA, Matter, Zigbee2MQTT, KNX y Modbus. | La ontología es extensible; no equivale aún a cobertura nativa de todos los fabricantes. |
| DSL de optimización, no Python arbitrario | Implementado y versionado en `OptimizationScenario`, CP-SAT, evidencia, explicación y comparación. | El resultado continúa siendo proposal-only y pasa validación de plan después. |
| Skills que combinan MCPs | Catálogo portable de nueve Skills y contratos de validación. | Las Skills no tienen privilegio: toda mutación cruza la misma frontera MCP/plan. |
| Plan separado de ejecución | Implementado con preview/prepare, policy, approval, admission, executor y readback. | La ejecución física de riesgo continúa fail-closed si falta qualification. |
| Automatización sin LLM/Internet | Implementada como `LocalAutomationEngine` durable. | Es acotada y consentida; no pretende ser un motor general no auditado. |

## Brechas exactas frente al objetivo arquitectónico original

La afirmación de que no quedaba funcionalidad pendiente era cierta respecto al
backlog marcado en `tasks.md`, pero es demasiado fuerte frente a la visión
literal del objetivo. Esta es la comparación que debe usarse para decidir el
próximo producto:

| Elemento del objetivo | Estado | Interpretación |
|---|---|---|
| MCP como interfaz de agentes, no como driver de protocolos | Cumplido | El runtime y los adapters están por debajo de FastMCP; `EventConsumer` y `StateStore` procesan estado sin LLM. |
| Dos MCP, Domotics y OR-Tools | Cumplido lógicamente, no como dos endpoints públicos | Se publican en un solo `FastMCP` y comparten `DeviceRegistry` y `PlanService`. Es una decisión deliberada para que no aparezcan dos autoridades de ejecución. Si se exige separar procesos/endpoints, eso sí queda por diseñar sin duplicar el runtime. |
| Herramientas semánticas de lectura, ejecución, escena y scheduling | Cumplido con una API más segura | Existen discovery/state/history/resources, `execute_plan`, escenas y schedules. No existe `execute_command` directo: se reemplaza por `preview_command` → `prepare_command` → aprobación/admission → `execute_plan`. Tampoco existen `get_device`/`get_devices` como tools porque se ofrecen mediante `discover_devices` y resources. |
| Resources MCP de devices, areas, capabilities, energy y policies | Cumplido | Están publicados los URI `domotics://…` previstos, más runtime, metrics y commissioning. |
| Prompts MCP | Implementado en software | El servidor unificado registra prompts de descubrimiento, diagnóstico y preparación de energía. Sólo orientan el procedimiento: no crean `plan_id`, no ejecutan ni conceden autoridad. |
| UDM con ID, área, fabricante, modelo, protocolo y capabilities | Cumplido | `Device` incluye esos campos; capabilities tienen tipo, unidad, rango, comandos, constraints y garantías. |
| Home Assistant como mega-adapter | Cumplido | Provider/adapter HA absorbe entidades, registry y servicios HA. Hue, Shelly o Tuya llegan a DomoAI vía HA cuando están integrados allí. |
| Matter, Zigbee2MQTT, KNX y Modbus | Cumplido | Hay adapters y mappers concretos bajo una frontera común. |
| MQTT genérico para ESP/dispositivos arbitrarios | Implementado en software | `GenericMqttAdapter`, mapping v1 estricto, codec tipado, aliases semánticos, rangos/unidades, idempotencia/readback, factory, fixture y composición Mosquitto. Firmware, broker operativo y hardware siguen siendo qualification externa. |
| Adapters nativos Hue, Shelly, Tuya, inversores, HVAC o EV | Fuera de objetivo | No se crearán adapters públicos específicos. Home Assistant y otros conectores internos alimentan el mismo adaptador universal cuando exista una ruta semántica válida. |
| DSL CP-SAT para energía/EV/solar/batería/clima | Cumplido | `OptimizationScenario`, proveedores OMIE/Open-Meteo, perfiles térmicos y energy context producen propuestas explicables y validadas. |
| Skills portables core y extensiones por host | Parcial | Las nueve Skills core existen y son validadas; `skills/claude/`, `skills/codex/` y `skills/generic-mcp/` ya documentan wrappers que referencian el core. Quedan kits de instalación/configuración host-específicos, sin duplicar autoridad. |
| Event bus interno como capa explícita | Parcial/deliberadamente local | Hay consumer, streams, colas bounded, cursores y StateStore; no hay un bus externo general (NATS/Kafka/Redis Streams). No es necesario para una vivienda single-writer y no debe añadirse sin demanda medida. |

Por tanto, el núcleo operativo propuesto está construido. No queda como deuda
crear adapters directos de fabricantes: contradice el objetivo del producto.
Lo que puede evolucionar son los conectores internos, el modelo universal y
los kits host-specific opcionales. La decisión sobre un event bus externo sigue
siendo una gate medida. La cobertura MQTT genérica y los prompts están cerrados
en software; firmware, broker, providers y hardware concretos siguen siendo
gates externas. Ninguna ampliación debe permitir una ruta que evite plan,
policy, approval, admission y readback.

## Hallazgos A-001–A-020 de la auditoría anterior

Los 20 hallazgos originales no quedaron simplemente documentados: los Specs
178 y 179 los convirtieron en trabajo de cierre y regresión. La tabla siguiente
resume la comparación, sin repetir el detalle por línea de la Fase 0.

| Grupo de hallazgos | Riesgo inicial | Desarrollo posterior | Estado de comparación |
|---|---|---|---|
| A-001, A-008–A-010 | Estado regresivo, stale, no finito o memoria/persistencia divergentes. | `StateStore` candidate/commit durable, cursores, freshness y schemas estrictos; Specs 124, 132, 176, 177, 178 y 179. | Cerrado en software; fuente física sigue siendo objeto de readback/qualification. |
| A-002–A-006 | Lease no liberado, evidencia no canónica, grants quemados y recurrencia no idempotente. | `PlanExecutor`, `ExecutionAdmission`, approval reservations, bundle saga, scheduler/recovery y Specs 156, 178, 179. | Cerrado en software y SQLite; no prueba una autoridad distribuida real. |
| A-007, A-013–A-014 | Preview que escribía, token ambiguo y authority recurrente incoherente. | Preview/prepare explícitos, token lifecycle hash-only y authority temporal; Specs 159, 172, 173, 178, 185. | Cerrado en software/contrato. |
| A-011–A-012, A-015–A-017 | Provider/adapters frágiles, resultados parciales y auditoría perdible. | SDK/conformance, lifecycle HA, CompositeAdapter detallado, audit outbox; Specs 139, 160, 174, 180, 181. | Cerrado en laboratorio/software; ampliar proveedores reales permanece abierto. |
| A-018–A-019 | Ownership falsificable y recovery sólo al arranque. | Capability opaca de aggregate, ledger y reconciliación periódica; Specs 156, 178, 185. | Cerrado para el runtime single-writer. |
| A-020 | `NaN`/infinito alcanzaba CP-SAT. | Validación de escenario y límites solver; Specs 143 y 179. | Cerrado en software. |

## Comparación por fases reales

### Fase 0 — seguridad e integridad

La auditoría original identificó A-001–A-020. El desarrollo posterior no sólo
los corrigió: añadió cierre de autoridad física, admission, reservations de
approval, outbox, validación semántica y persistencia de metadatos. Las Specs
178 y 179 están cerradas y las regresiones referidas desde la auditoría de Fase
0 cubren fallos, reinicio y duplicados.

**Estado:** cerrado técnicamente con riesgo residual de despliegue físico y
multi-host, no con un "PASS" absoluto de producción.

### Fase 1 — robustez operativa

Specs 180 y 181 consolidan observabilidad acotada, lifecycle/reconnect,
backpressure y qualification de laboratorio. La implementación posterior de
event-driven refresh, métricas remotas opt-in y preflight es consistente con
esa fase, no una sustitución de las garantías de Fase 0.

**Estado:** cerrado para software y process-lab. La auditoría es correcta al
no convertir un skip HIL o una fixture en certificación de hardware.

### Fase 2 — producto agentic

El contraste más fuerte con la auditoría inicial es que los componentes de
producto ya existen: Specs 182 (catálogo de Skills), 183 (DSL explicable), 184
(automatización local), 189 (historial) y 190 (cierre de fase). Esto cumple la
separación prevista: MCP aporta acciones, Skill aporta procedimiento y la DSL
evita código de solver generado por el agente.

**Estado:** cerrado en producto/software. La extensión de nuevos Skills o
drivers es evolución de catálogo, no una deuda de seguridad del núcleo.

### Fase 3 — identidad y escalabilidad

Specs 185, 191, 192, 193 y 194 ampliaron identidad por tenant/hogar/principal,
ACL, backups, PostgreSQL, etcd/fencing y el laboratorio multi-host. El código
contiene esa base y la configuración falla cerrada cuando falta coordinación
externa o capacidad de fencing.

**Estado:** foundations y qualification de laboratorio cerradas; active-active
productivo sigue deliberadamente no habilitado. La Spec 192 declara el diseño
externo completo, pero no tiene `tasks.md`; conviene tratar su aceptación como
evidencia de código/tests, no como checklist trazable.

### Fase 4 — diferenciación y qualification

Specs 186, 187 y 195 añadieron garantías de capability, commissioning,
privacidad, gemelo digital y preflight. El reporte actual distingue software y
laboratorio de tres gates físicas: batería HIL, provider independiente y
commissioning de protocolo live.

**Estado:** software/laboratorio cualificados; físico bloqueado externamente.
La honestidad de esta separación es una fortaleza del desarrollo posterior.

## Discrepancias documentales detectadas

1. [`PENDIENTES.md`](PENDIENTES.md) está fechado el 2026-08-17 y conserva
   afirmaciones de v1 que fueron superadas por Specs 178–195. Debe etiquetarse
   como histórico o actualizarse; no debe usarse para priorizar el estado
   actual.
2. La auditoría consolidada usa resultados de test fechados. Son evidencia
   válida de aquella ejecución, pero no sustituyen una suite fresca sobre un
   worktree con cambios sin confirmar.
3. Existe documentación profunda por fase, pero el worktree agrupa muchos
   cambios de subsistemas y datos SQLite/WAL. Antes de publicar o desplegar,
   hay que revisar y atomizar el diff; el estado del árbol no representa una
   única release verificable.

## Veredicto comparativo

La auditoría anterior acertó en la dirección y en los riesgos. Los desarrollos
posteriores han cerrado sus hallazgos técnicos y han llevado la propuesta más
allá de la idea inicial: runtime único, gateway compartido, autoridad e
identidad, automatización local, DSL explicable, catálogo de Skills, gemelo
digital, commissioning y preflight.

Lo que falta no es "crear los dos MCP" ni reabrir las correcciones A-001–A-020.
Lo que queda es obtener evidencia fuera del repositorio: HIL real (Spec 133),
escenarios adicionales con dependencias reales (Spec 140), contrato de un
provider desplegado independiente (Spec 141), y sólo después decidir si existe
una necesidad operacional demostrada de habilitar active-active.
