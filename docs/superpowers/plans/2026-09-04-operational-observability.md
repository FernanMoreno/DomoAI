# Plan: Observabilidad operativa de runtime

**Fecha:** 2026-09-04
**Feature:** `specs/180-operational-observability/`
**Diseño aprobado:** `docs/superpowers/specs/2026-09-04-operational-observability-design.md`

## Objetivo

Cerrar los pendientes observables de la Fase 1 con un acumulador process-local,
bounded y tolerante a fallos, integrado en el runtime y expuesto por el
recurso MCP existente.

## Secuencia ejecutable

1. Especificación, contrato y baseline de la feature.
2. Tests RED para el acumulador y sus límites.
3. Implementación del acumulador sin dependencias de aplicación.
4. Tests RED e instrumentación de executor, StateStore, ApprovalStore y bundle commit/recovery.
5. Integración con `RuntimeComposition` y `RuntimeMetricsCollector`.
6. Tests de contrato/composición y actualización de auditorías Markdown.
7. Suite completa, static checks, Graphify, composition check y revisión final.

## Stop condition

La Fase 1 queda completa cuando las señales aprobadas aparecen en
`domotics://metrics`, sus productores reales están cubiertos, no se alteran
autoridad ni fail-closed, y todos los gates del proyecto pasan. No se
introducen Prometheus/OpenTelemetry, persistencia de métricas ni cambios de
policy.

## Verificación

Se conservarán los resultados exactos de cada comando en la auditoría de Fase
1 y se marcarán las tareas de `specs/180-operational-observability/tasks.md`
solo después de obtener evidencia fresca.
