# Auditoría integral del código DomoAI

**Fecha:** 2026-09-09
**Modalidad:** auditoría inicial de solo lectura + remediación verificable
**Cobertura:** seguridad, autorización, ejecución física, persistencia, APIs, conectores, despliegue, CI y operaciones.

## Resultado ejecutivo

La auditoría inicial encontró 22 ítems de riesgo. La remediación posterior
cerró los 22 hallazgos con cambios y evidencia ejecutable. No quedan riesgos
aceptados ni hallazgos abiertos en los informes de esta auditoría.

Se corrigieron los bloqueantes de despacho físico y autoridad verificada, la
autorización completa de Management/ORTools, SSRF y allowlists, OAuth remoto,
recuperación y outbox multi-host, límites de transportes/API, idempotencia
REST, despliegue reproducible y hardening operativo del stack. La preparación
de producción sigue dependiendo de certificados, DNS/firewall, proveedores
externos, hardware y cualificaciones fuera del repositorio.

## Informes por área

- [Autorización, identidad y scopes](01-autorizacion-y-scopes.md)
- [Concurrencia, idempotencia y recuperación](02-concurrencia-idempotencia-recuperacion.md)
- [SSRF, OAuth, transportes y límites](03-ssrf-oauth-y-transportes.md)
- [API, contratos y resiliencia](04-api-contratos-y-resiliencia.md)
- [Despliegue, CI y operaciones](05-despliegue-ci-operaciones.md)
- [Verificación, evidencias y limitaciones](06-verificacion-y-limitaciones.md)

## Prioridad recomendada

### P0 — bloqueantes auditados

Sin hallazgos abiertos. `EXEC-001`, `AUTH-001`, `AUTH-002` y `AUTH-003` están
cerrados con pruebas de autoridad, exclusividad física y rutas de mutación.

### P1 — exposición externa

Sin hallazgos abiertos. Las restricciones SSRF, OAuth, recuperación,
transportes, APIs y límites están cerradas según sus informes y gates.

### P2 — endurecimiento operativo

OPS-001 a OPS-006 están cerrados. El laboratorio MQTT queda limitado a
loopback por defecto y su exposición en redes compartidas exige el overlay TLS
explícito; esta composición sigue siendo exclusivamente de laboratorio y no un
despliegue productivo.

## Estado de cambios

La remediación aplicó cambios de código, tests, despliegue, CI y documentación
manteniendo intactos los cambios preexistentes no relacionados del árbol de
trabajo. Cada cierre conserva su evidencia en el informe por área y en el
`composition-review.md` de su spec.
