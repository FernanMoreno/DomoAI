# Evidencia Fase 3B — coordinación multi-host

Fecha: 2026-09-05

## Resultado

La fundación provider-neutral está implementada y fail-closed:

| Gate | Estado |
|---|---|
| Lease por tenant/hogar/deployment | PASS en coordinador determinista de tests |
| Epoch monotónico y rechazo stale | PASS |
| Enforcement antes del adapter | PASS |
| Ledger idempotente y recuperación UNKNOWN | PASS |
| Cola FIFO bounded por hogar | PASS |
| Identidad de instancia y fencing metrics | PASS |
| Histórico local bounded | PASS |
| `multi_host_enabled` sin provider externo | RECHAZA ARRANQUE |
| Active-active productivo | BLOCKED: falta coordinador externo cualificado |

## Pruebas de composición

Cubiertas: carrera de adquisición, takeover tras expiry, epoch antiguo,
replay de `idempotency_key`, contexto de fencing hasta adapter, cola aislada
por hogar, recuperación de intenciones en vuelo, histórico bounded y
ausencia de secretos en Prometheus.

El coordinador determinista no representa disponibilidad ni fencing de
producción. La siguiente gate requiere un proveedor externo real, seguridad de
servicio, backend/gateway que valide epochs y pruebas de partición/HIL.

## Revisión estructural

Se consultó Graphify para localizar los límites de runtime, coordinación,
persistencia, métricas y composición, y se contrastaron sus resultados con el
código fuente. El refresco completo posterior a los cambios alcanzó la
extracción AST pero quedó detenido en el resolvedor de rutas de Graphify; no se
usa ese refresco incompleto como evidencia de corrección.
