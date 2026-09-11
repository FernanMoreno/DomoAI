# Verificación, evidencias y limitaciones

## Metodología

La auditoría inicial desplegó cuatro agentes de análisis en paralelo, con
alcance de solo lectura. Después se añadieron ciclos de remediación
contract-first y verificaciones ejecutables por hallazgo:

- Seguridad de conectores, recipes, SSRF y secretos.
- Persistencia, estado, concurrencia, leases e idempotencia.
- APIs, contratos, autorización y aislamiento de tenants.
- Docker, Compose, CI, healthchecks y operación.

Sus resultados se contrastaron con el código fuente, pruebas específicas y reproducciones locales controladas.

## Gates ejecutados

| Comprobación | Resultado |
|---|---|
| `uv run ruff check .` | PASS — `All checks passed!` |
| `uv run mypy src` | PASS — 245 ficheros, sin errores |
| `uv lock --check` | PASS |
| Suite unitaria con cobertura | PASS — 1230 passed, 63% baseline; gate CI `--cov-fail-under=60` |
| `uv run pytest -q` | PASS — 2412 passed, 18 skipped |
| Contratos focales de remediación | PASS — 12 passed en OPS-006; los gates anteriores permanecen verdes |
| Helper Docker obligatorio | PASS — 2 passed, 0 skipped; Mosquitto real + Caddy real |
| `project-composition-check domoai` | PASS — 660 passed, 18 skipped |
| `docker compose ... config` para deploy | PASS |
| Contratos/composición de OPS-006 | PASS — broker autenticado en loopback y overlay TLS real |
| `git diff --check` | PASS |

Los tests verdes prueban la regresión funcional y los contratos adversariales
añadidos. Los skips restantes pertenecen a cualificaciones externas/lab no
incluidas en el smoke obligatorio; por eso CI exige Docker y cero skips en el
job representativo.

## Reproducciones confirmadas

### Scope de autoridad

Un `AuthorityContext` con restricciones de área, dispositivo y capacidad fue vinculado a un plan con autoridad por defecto. Las listas resultantes quedaron vacías, demostrando que el binding sustituye el ámbito verificado.

### SSRF en `$ref`

Un servidor HTTP local controlado recibió una petición durante la validación de un schema JSON remoto:

```text
remote_requests 1
```

### Allowlist vacía

La validación de un endpoint `169.254.169.254` con `allowed_hosts=frozenset()` no generó diagnóstico.

### OAuth HTTP

Un token endpoint remoto `http://...` no fue rechazado por preflight; quedó en estado `manual_required`.

## Graphify

Se actualizó el grafo vigente en modo código-only:

- 10.692 nodos.
- 30.954 aristas.
- 683 comunidades.

Graphify no pudo extraer 21 ficheros SQL porque falta el parser opcional
`tree_sitter_sql`. La extracción documental completa requiere una API
semántica no configurada; por eso los cambios de Caddy/Compose/CI/docs se
verificaron directamente contra sus fuentes y comandos reales.

## Revisión de composición

La revisión manual cubrió las fronteras entre:

```text
token -> autoridad -> plan -> executor -> physical intent -> adapter
                         |                    |
                         v                    v
                     recovery              outbox
                         |
                  leases/fencing
```

Las invariantes históricamente incumplidas quedaron cubiertas por los cierres
de AUTH/EXEC/REC/NET/RES/API/OPS. Se mantienen como riesgos operativos las
dependencias externas indicadas abajo, no como hallazgos de código abiertos.

El comando automático `project-composition-review domoai codex` requiere una
TUI y no es utilizable en este entorno (`stdin is not a terminal`). El gate
ejecutable de composición terminó correctamente y cada spec contiene la
revisión manual requerida con diff, Graphify, source inspection y escenarios.

## Limitaciones

- No se probaron proveedores OAuth o dispositivos externos reales.
- No se ejecutó un escaneo CVE completo de dependencias contra un feed externo.
- Dieciocho tests aparecen como skipped en la suite de composición/integración;
  el job obligatorio cubre la dependencia Docker representativa sin skips.
- `OPS-006` está cerrado: la composición base no expone plaintext fuera de
  loopback, y el overlay de red compartida exige TLS, certificado, clave y
  autenticación.

## Veredicto

**PASS con remediaciones verificadas / no es certificación de una instalación
externa o de hardware.**

No queda una prioridad de código abierta en este expediente. Los siguientes
pasos operativos son provisionar certificados/CA, DNS/firewall, secretos y
calificaciones de proveedores/hardware según el entorno real.
