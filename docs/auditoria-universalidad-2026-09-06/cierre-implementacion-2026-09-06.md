# Cierre de implementación de las fases de universalidad

Fecha: 2026-09-06. Referencia: `d6c19e0b80631ce6095b7f6bc45a0036003cee78` más el worktree local consolidado en `DomoAI`.

Este documento registra la ejecución de las fases 00–08 descritas en el índice. La auditoría integral y sus
hallazgos originales permanecen sin reescritura. El worktree ya contenía cambios funcionales y documentos antes de
esta ejecución; se conservaron y se revisaron junto con los cambios de cierre.

## Dictamen

**PASS WITH RISKS** para el perfil de software local, fixture y laboratorio Docker disponible en este entorno.

El resultado no es una certificación de cualquier vivienda. Las pruebas físicas/HIL que requieren equipo,
credenciales o gateways reales siguen omitidas, y el perfil de autenticación publicado es bearer estático
aprovisionado por el operador, sin servidor OAuth integrado.

## Resultado por fase

| Fase | Resultado ejecutado | Hallazgos y límites |
|---|---|---|
| 00 | Baseline, mapa de fronteras, Graphify y gates de arquitectura verificados. | U-11 queda como riesgo de concentración documentado; se extrajo la proyección compartida que tenía una causa funcional concreta. |
| 01 | Automatizaciones locales y recurrentes conservan autoridad, derivan una clave por ocurrencia y proyectan rechazo, fallo, parcial, desconocido y vacío sin declararlos éxito. | El consentimiento, fencing, readback y qualification física siguen siendo gates independientes. |
| 02 | Gateway valida Host/Origin antes del 405 de SSE desactivado; ClientSession recibe errores MCP con `isError`; `tools/list` publica modelos anidados; el perfil bearer está documentado. | Hosts que exijan OAuth deben usar una capa externa o una futura implementación cualificada. |
| 03 | Exportación paginada con cursor firmado y redacción; borrado por hogar en una transacción; invalidación de caché; purga de histórico también para hogares inactivos. | Backups antiguos pueden reintroducir datos y su custodia continúa siendo responsabilidad del despliegue. |
| 04 | `domotics://coverage` y `docs/adapter-coverage.md` exponen operaciones, comandos, límites, garantías, disponibilidad y fuentes por capability. | La matriz muestra cobertura semántica por perfil; no convierte un protocolo presente en soporte universal ni en autoridad física. |
| 05 | Reconciliación, commissioning, identidad, cambios de inventario y preflight verificados con fixtures y laboratorio. | Determinar si un relé controla una carga sensible requiere evidencia de instalación o intervención atendida. |
| 06 | Contextos energéticos, restricciones, explicación, worker, escenarios y composición de propuestas verificados. | Ahorro estimado, tarifa/forecast y parámetros de batería/EV/HVAC no equivalen a medición ni qualification física. |
| 07 | Las nueve Skills se incluyen en el wheel y el catálogo carga desde recursos empaquetados; las imágenes Docker de despliegue y laboratorio copian el catálogo. | La compatibilidad de cada host comercial requiere una prueba propia; no se asignaron nombres comerciales a fixtures. |
| 08 | Gates de software, contratos, integración, composición, rendimiento y laboratorio multi-host ejecutados; el runner Docker cubre carrera, partición, recuperación, outbox, restore y límites. | La qualification atendida de hardware queda bloqueada por dependencias externas identificadas. |

## Cambios funcionales principales

- `src/domoai/application/execution_projection.py` centraliza la proyección de outcomes y sus consumidores locales y
  recurrentes la usan con autoridad de la regla y del consentimiento.
- `src/domoai/application/recurrence.py`, `local_automation.py` y `scheduler.py` derivan identidades deterministas
  por hogar, definición, ocurrencia y miembro del plan.
- `src/domoai/mcp/errors.py`, `domotics_server.py`, `resources.py` y `gateway.py` alinean errores, esquemas,
  cobertura y orden de validación con el protocolo público.
- `src/domoai/application/privacy.py`, `src/domoai/persistence/privacy.py`, `domain/privacy.py` y
  `runtime/state_store.py` implementan páginas acotadas, cursores firmados, borrado transaccional, caché coherente
  y purga de histórico.
- `src/domoai/skills/catalog.py`, `skills/validator.py`, `pyproject.toml`, `deploy/Dockerfile` y el Dockerfile del
  laboratorio hacen operable el catálogo fuera del checkout.
- La matriz semántica y el perfil de autenticación están publicados en `docs/adapter-coverage.md`,
  `docs/unified-mcp.md` y `deploy/gateway.env.example`.

## Evidencia ejecutada

Desde la raíz del repositorio:

```text
uv run --frozen ruff check .                         PASS
uv run --frozen mypy src                           PASS
python scripts/check_architecture_contracts.py      PASS
lint-imports                                        4 kept, 0 broken
python scripts/check_runtime_contract_docs.py       PASS
project-composition-check "$(cat .ai/project-name)" 536 passed, 18 skipped; PASS
uv run --frozen pytest tests/unit tests/contract tests/integration tests/composition tests/performance -q -rs
                                                     1962 passed, 18 skipped
```

Tras recuperar Docker, la prueba focalizada del laboratorio multi-host, incluidos Postgres, ETCD, MQTT y los
runners Docker de carrera y recuperación, terminó con `13 passed`. La carga aislada del wheel desde otro directorio
validó el catálogo de nueve Skills.

Los skips corresponden a HIL/live de Home Assistant, KNX, batería, EV, Matter, Modbus, Zigbee2MQTT, OMIE,
Open-Meteo, composición multi-adapter y bootstrap del laboratorio. Cada caso mantiene su motivo en la salida de
pytest; no se cuentan como aprobación.

## Límites y siguiente evidencia necesaria

La evidencia actual demuestra el comportamiento del código, fixtures, gemelo y dependencias de laboratorio
disponibles. Para elevar un perfil a `cualificado_perfil` todavía se necesita registrar hardware/modelo/firmware,
mapping, rutas de feedback, responsable, condiciones de prueba y criterio de interrupción, y ejecutar los casos HIL
correspondientes. La rotación de bearer está cubierta por el ciclo administrativo local; el gateway no anuncia un
flujo OAuth que no implemente.

## Consolidación de worktrees

`DomoAI` queda como ubicación canónica. La rama `feat/approval-authority-jit` ya estaba contenida en la historia de
la rama principal; se integraron 40 cambios rastreados sin solapamiento y 3 archivos nuevos. En 52 rutas solapadas y
14 variantes sin rastrear prevaleció la versión más avanzada que ya estaba en `DomoAI`; las versiones de respaldo se
conservaron fuera del repositorio durante la revisión. Los otros worktrees permanecen intactos para permitir una
revisión o recuperación posterior.

No se hizo commit, push, merge de Git ni limpieza destructiva de cambios ajenos del worktree.
