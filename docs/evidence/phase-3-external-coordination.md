# Evidencia Fase 3 — coordinación externa

Fecha: 2026-09-05

## Resultado de implementación

La integración aprobada queda implementada con etcd como autoridad de lease y
fencing, y PostgreSQL como control plane compartido. El runtime conserva SQLite
single-writer por defecto y solo selecciona los proveedores externos cuando la
configuración multi-host está completa.

| Gate | Estado |
|---|---|
| Lease etcd v3 JSON, CAS y epoch monotónico | PASS |
| Renovación, release y rechazo de token stale | PASS |
| Fallo de red/TLS y endpoints no HTTPS fail-closed | PASS |
| PostgreSQL schema/repositorios compartidos | PASS |
| Idempotencia de intentos físicos y outbox durable | PASS |
| Migración SQLite→PostgreSQL con rollback, conteos y digest | PASS |
| Adapter sin capacidad explícita de fencing | RECHAZA ARRANQUE |
| Active-passive productivo | PENDIENTE: HIL y gateway físico cualificado |
| Active-active | BLOCKED por diseño |

## Pruebas ejecutadas

- Tests focalizados de coordinación, backend, runtime, documentación y
  migración: `21 passed`.
- Tests contra contenedores reales de etcd y PostgreSQL, incluida migración:
  PASS cuando Docker está disponible.
- `project-composition-check "$(cat .ai/project-name)"`: `520 passed, 18
  skipped`; arquitectura `4 kept, 0 broken`.
- Suite completa: `1847 passed, 18 skipped`.
- Ruff, mypy, import-linter, documentación contractual y `git diff --check`:
  PASS.

## Límites de la evidencia

El contenedor etcd usado por la prueba es un nodo desechable de integración,
no una cualificación de quorum de producción. Tampoco se ejecutó HIL contra un
gateway físico real ni se verificó una topología PostgreSQL HA con sus backups
y certificados operativos. Por ello no se habilita active-passive productivo
ni active-active: el último punto físico todavía debe validar
`fencing_epoch` y superar pruebas de takeover, partición, crash y recuperación.
