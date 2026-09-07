# Fase 00 — Base técnica, arquitectura y evidencia

[Índice](README.md) · [Siguiente: automatizaciones](fase-01-automatizaciones-autoridad-ejecucion.md)

Estado: desarrollo documental de la auditoría de 2026-09-06. Hallazgo propietario: U-11. Dependencia de entrada: ninguna.

## 1. Objetivo

Establecer una base que permita saber qué software se evaluó, dónde vive cada responsabilidad y qué conclusiones siguen vigentes. La fase evita que otra sesión confunda documentación de intención, código implementado, tests verdes y qualification física.

El resultado esperado es una matriz de trazabilidad mantenible. No requiere una reescritura de la arquitectura ni dividir archivos sólo por superar un número de líneas.

## 2. Baseline observada

- SHA de referencia: `d6c19e0b80631ce6095b7f6bc45a0036003cee78`, con numerosos cambios locales.
- Inventario anterior: 203 archivos en `src/domoai`, 354 en `tests`, 195 documentos `spec.md`.
- Suite anterior: 1.941 aprobadas y 18 omitidas; composición global: 531 aprobadas y 18 omitidas. Hay solapamiento entre ambas ejecuciones: no sumar 1.941 + 531 como casos diferentes.
- Ruff, mypy, comprobación de arquitectura, Import Linter y coherencia documental pasaron.
- Build de sdist/wheel pasó, aunque U-12 detectó un defecto de distribución del catálogo.
- Graphify contenía 8.279 nodos. Su consulta amplia era orientativa y estaba truncada.

No se ha recalculado esa baseline al dividir la documentación. Cualquier implementación posterior necesita una nueva referencia de estado.

## 3. Mapa de responsabilidades

| Área | Fuente principal | Responsabilidad y frontera |
|---|---|---|
| Dominio | `src/domoai/domain/models.py`, `domain/automation.py` | Tipos, restricciones y contratos; independiente de transporte. |
| Frontera pública | `mcp/unified_server.py`, `mcp/domotics_server.py`, `mcp/ortools_server.py` | Registro MCP y adaptación de entradas/salidas. |
| Composición | `application/runtime_factory.py`, `mcp/configured.py` | Construye objetos compartidos y selecciona perfiles. |
| Lifecycle | `application/runtime_lifecycle.py`, `runtime_ownership.py` | Inicio, tareas, cierre y ownership. |
| Semántica | `runtime/registry.py`, `application/discovery_service.py` | Inventario, identidad y rutas. |
| Estado | `runtime/state_store.py`, `persistence/repositories.py` | Observaciones, versiones, orden y durabilidad. |
| Ejecución | `application/plan_service.py`, `execution_admission.py`, `executor.py` | Validación, autoridad y efectos físicos. |
| Productores automáticos | `application/local_automation.py`, `scheduler.py` | Materializan planes sin un agente conectado. |
| Protocolos | `adapters/`, `runtime/composite_adapter.py` | Traducción interna al contrato universal. |
| Energía | `optimizer/`, `application/optimization_service.py` | Propuestas y contexto; sin autoridad de actuador. |
| Operación | `admin/`, `deploy/`, `lab/`, `hil/` | Instalación, diagnóstico y evidencia por entorno. |

Las rutas abreviadas de la tabla son relativas a `src/domoai/`, salvo `deploy/`. Consultar [arquitectura de la auditoría original](../auditoria-integral-universalidad-2026-09-06.md#4-arquitectura-real-y-propiedad-del-estado).

## 4. Contratos arquitectónicos que deben conservarse

1. El dominio no depende de adapters, MCP ni persistencia.
2. Los conectores de protocolo no dependen entre sí.
3. La raíz de composición inyecta dependencias; no sustituye los controles del executor.
4. Los contextos domótico y energético comparten registry y plan service.
5. Cada proceso operativo tiene ownership y lifecycle definidos.
6. El solver produce propuestas; no llama a actuadores.
7. Las Skills no crean una autoridad alternativa.
8. Toda fuente nueva declara qué capacidades no puede representar.

Los controles de imports demuestran estructura de dependencias, no propagación correcta de identidad o resultados. U-01 a U-03 muestran precisamente ese límite.

## 5. U-11: concentración de responsabilidades

En la auditoría se midieron 2.037 líneas en `repositories.py`, 1.824 en `domotics_server.py`, 1.623 en `runtime_factory.py` y 1.335 en `executor.py`. Son señales para localizar responsabilidades mezcladas, no evidencia de que todos esos módulos estén mal.

Antes de extraer código, identificar una causa concreta: proyección de estados duplicada, construcción de planes con defaults no visibles o un contrato que varios consumidores deben preservar. Una extracción justificada debe mantener firmas o documentar su transición, impedir nuevas dependencias ascendentes y conservar la prueba de comportamiento antes/después.

La fase 01 puede necesitar consolidar una regla de materialización; no debe convertirse por ello en una refactorización de todos los repositorios.

## 6. Tareas documentales y de comprobación propuestas

### F00-T01 — Identificar el estado evaluado

- [ ] Registrar SHA, rama, archivos modificados y archivos nuevos relevantes sin copiar secretos.
- [ ] Separar cambios previos de cambios de la fase ejecutada.
- [ ] Asociar evidencia al contenido del worktree, no únicamente al SHA base.
- [ ] Registrar Python, versión instalada de DomoAI y dependencias críticas.

Aceptación: otra sesión puede identificar si está observando el mismo estado o una revisión distinta.

### F00-T02 — Reconciliar fuentes de estado

- [ ] Contrastar `docs/program-status-2026-09-06.md` con U-01–U-13.
- [ ] Vincular las tareas nuevas a su especificación cuando se autorice implementación.
- [ ] Conservar cierres anteriores como evidencia histórica, sin extrapolarlos al programa completo.
- [ ] Documentar la falta de `tasks.md` de la baseline 196 sin inferir que su código no existe.

Aceptación: cada afirmación de cierre tiene una prueba o una limitación explícita.

### F00-T03 — Revisar el mapa de fronteras

- [ ] Consultar Graphify antes de cambios significativos y comprobar rutas contra fuente.
- [ ] Identificar todos los productores de planes y consumidores de resultados.
- [ ] Registrar propietario de cada estado, transacción, caché y tarea en segundo plano.
- [ ] Anotar dónde termina una transacción local y empieza una operación externa.

Aceptación: la revisión de una fase conoce sus vecinos aguas arriba y abajo.

## 7. Comprobaciones ejecutables futuras

Desde la raíz, en un entorno preparado:

```bash
git status --short
git rev-parse HEAD
uv run --frozen python scripts/check_architecture_contracts.py
uv run --frozen lint-imports
uv run --frozen python scripts/check_runtime_contract_docs.py
```

No generar esquemas sobre un árbol sucio sólo para comprobar frescura sin preservar primero el trabajo existente. Tampoco reconstruir Graphify por un cambio puramente documental en esta serie.

## 8. Registro de cierre

- [ ] Todos los U-01–U-13 tienen fase propietaria, evidencia y estado.
- [ ] Ninguna tarea de ampliación se presenta como defecto ya reproducido.
- [ ] No se confunden recuentos de archivos, tests y cobertura de líneas.
- [ ] El índice documenta dependencias y decisiones pendientes.
- [ ] Las afirmaciones de funcionamiento están ligadas a perfiles y entornos.
- [ ] Se preserva un MCP, un contrato y una autoridad física.

Salida hacia fase 01: baseline, fuentes del ciclo de planes, reproducciones anteriores y lista de invariantes. Riesgo residual: el worktree puede cambiar después de capturar la evidencia; cada cierre posterior debe declarar su referencia.
