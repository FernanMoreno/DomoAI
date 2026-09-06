# DomoAI — Estado de programa y pendientes verificables

**Actualizado:** 2026-09-06  
**Fuente de estado:** código del worktree, contratos, auditoría por fases,
evidencia de laboratorio y [comparativa de auditoría/especificaciones](comparativa-auditoria-especificaciones-2026-09-06.md).

Este documento es un índice de estado versionable, no una copia de todas las
tareas de Spec Kit. Distingue deliberadamente entre trabajo cerrado en
software, capacidad prevista y evidencia que depende de un entorno externo.

## Estado actual

- El runtime semántico, el gateway MCP unificado, la policy, el plan/executor,
  los adapters Home Assistant, Matter, Zigbee2MQTT, KNX y Modbus, el
  optimizador proposal-only, las Skills core y la automatización local existen
  en el árbol actual.
- Los hallazgos A-001–A-020 de la auditoría original están cerrados en código
  y regresión; su detalle permanece en `docs/auditoria-fase-*.md`.
- Los contratos de arquitectura mantienen dominio independiente, adapters
  independientes y composición descendente.
- Software, fixtures y process-lab no equivalen a qualification física.

## Programa MCP universal

El programa aprobado está en
[universal-domotics-coverage-design.md](superpowers/specs/2026-09-06-universal-domotics-coverage-design.md)
y su plan en
[universal-domotics-program.md](superpowers/plans/2026-09-06-universal-domotics-program.md).

| Fase | Estado | Propósito |
|---|---|---|
| 0 — [`196-universal-mcp-baseline`](../specs/196-universal-mcp-baseline/spec.md) | Implementada en software | Contrato de MCP único, inventario de cobertura, prompts/resources y documentación bajo regresión. |
| 1 — Prompts y resources de cobertura | Implementada | Prompts MCP seguros y `domotics://coverage` sin cambiar autoridad. |
| 2 — MQTT genérico declarativo | Implementada en software | Mapping v1 estricto, codec tipado, aliases semánticos, rangos/unidades, idempotencia, readback, identity-conflict, factory, fixture y broker Mosquitto cubiertos; qualification física permanece externa. |
| 3 — Adapters específicos de fabricante | Fuera de objetivo | No se crearán adapters públicos por fabricante/protocolo; cualquier conector nuevo será interno y consumirá el contrato del adaptador universal. |
| 4 — Skills y hosts | Parcial | Core portable y wrappers Claude/Codex/generic MCP publicados; quedan kits de instalación/configuración. |
| 5 — Event fabric | Gate de decisión | Medir el event consumer local antes de especificar cualquier bus externo. |
| 6 — Qualification | Parcial/external | Cualificar perfiles físicos concretos tras evidencia atendida. |

La interfaz pública sigue siendo un único MCP general y un único adaptador
semántico universal. Los clientes compatibles
comparten runtime, registry, plan service, scheduler, approval store y executor;
no se abrirá un segundo camino de ejecución para Claude, Codex, otro host o un
fabricante.

## Cobertura de integraciones

La cobertura de cada adapter, su evidencia y sus límites está en
[adapter-coverage.md](adapter-coverage.md). Una categoría describe una ruta
semántica; no concede permiso ni sustituye policy, consentimiento, admission,
readback o auditoría.

## Gates externas abiertas

Las siguientes tareas no se pueden cerrar sólo editando código local:

1. [Spec 133 — Battery HIL certification](../specs/133-battery-hil-certification/tasks.md):
   ejecutar la prueba atendida sobre inversor/batería real y archivar evidencia
   del perfil, firmware, comandos, readback y tolerancias.
2. [Spec 140 — Real composition tests](../specs/140-real-composition-tests/tasks.md):
   ampliar escenarios contra dependencias reales a medida que se incorporen
   protocolos/despliegues.
3. [Spec 141 — Provider contract tests](../specs/141-provider-contract-tests/tasks.md):
   verificar contrato consumidor/proveedor cuando exista un provider desplegado
   e independientemente versionado.

Un skip por hardware, broker, provider o credenciales ausentes se registra como
`blocked_external_dependency`; nunca como PASS de producción.

## Cómo interpretar la evidencia

- La suite, contratos y gemelo digital prueban comportamiento de software
  cubierto en el worktree que se ejecutó.
- El laboratorio reproducible prueba procesos y dependencias concretas, no
  cableado, firmware, radio ni comportamiento de todos los fabricantes.
- La qualification física pertenece a un perfil identificado y caduca ante
  cambios de dispositivo, mapping, firmware o evidencia.
- El modo active-active permanece deshabilitado hasta contar con coordinación,
  fencing y evidencia de partición/HIL; no es un atajo para escalar una casa.

## Próxima acción

Mantener bajo regresión el endpoint MCP único y cerrar las verificaciones de
composición de las fases ya implementadas. La validación restante que no puede
cerrarse sólo con código local es la qualification de perfiles físicos,
brokers, providers y dispositivos concretos descrita en los gates externos.
