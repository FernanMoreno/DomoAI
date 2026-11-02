# Auditoría de API, contratos y resiliencia

## API-001 — Cuerpo JSON de Management sin límite

**Severidad:** media
**Estado:** cerrado — 2026-09-10
**Evidencia:** [`management/api.py:56-92`](../../src/domoai/management/api.py#L56), [`management/api.py:758-834`](../../src/domoai/management/api.py#L758), [`management/api.py:1043-1052`](../../src/domoai/management/api.py#L1043), [`gateway.py:81-83`](../../src/domoai/mcp/gateway.py#L81), [`test_management_body_limits.py`](../../tests/security/test_management_body_limits.py), [`test_management_body_limits_contract.py`](../../tests/contract/test_management_body_limits_contract.py), [`test_management_body_limits_composition.py`](../../tests/composition/test_management_body_limits_composition.py)

`_json_body()` ya no llama a `request.json()`: comprueba el `Content-Length`
antes de parsear y, para cuerpos chunked, acumula solo hasta el límite y
rechaza el primer chunk excedente. Tras el parseo aplica límites recursivos de
profundidad (`32`), claves (`1000`) y longitud de string (`64 KiB`). El gateway
propaga `Settings.mcp_max_request_body_size` —por defecto `4 MiB`— a la API de
Management. Los rechazos devuelven `413` con categorías estables y registran
solo método/path/límite, sin guardar ni devolver el cuerpo.

### Impacto

Una petición autenticada con un cuerpo grande puede forzar consumo elevado de memoria, especialmente en endpoints de ETS, OAuth, reparaciones o configuración.

### Verificación de cierre

- Suite enfocada de límites, seguridad, contrato, Management y composición:
  `44 passed`.
- Ruff: `All checks passed!`; mypy: `Success: no issues found in 245 source
  files`.
- Import Linter: `4 kept, 0 broken`.
- Gate global: `project-composition-check domoai` con `619 passed, 18 skipped`;
  arquitectura `4 kept, 0 broken`; PASS.
- Graphify post-cambio: `193 nodes, 500 edges`; consulta conectó la
  configuración, gateway, API, lector JSON y tests de límite.
- Revisión transversal:
  [`specs/219-bound-management-json/composition-review.md`](../../specs/219-bound-management-json/composition-review.md).

## API-002 — Contrato documental de filtrado no coincide con ORTools

**Severidad:** media-alta
**Estado:** cerrado — 2026-09-10 (cubierto por AUTH-003)
**Evidencia:** [`unified-mcp.md:51-55`](../unified-mcp.md#L51), [`ortools_server.py:66-170`](../../src/domoai/mcp/ortools_server.py#L66), [`ortools_server.py:292-485`](../../src/domoai/mcp/ortools_server.py#L292), [`test_ortools_authority.py`](../../tests/unit/mcp/test_ortools_authority.py), [`test_ortools_mcp_contract.py:248-285`](../../tests/contract/test_ortools_mcp_contract.py#L248)

La implementación ORTools ya aplica `AuthorityPolicy` a escenarios y a las
proyecciones de resultados antes de validar, optimizar o devolver alternativas.
La documentación sobre claims verificados y filtrado por scope coincide ahora
con el camino ejecutable. Este hallazgo es el duplicado API de AUTH-003.

### Impacto

El contrato público induce a operadores e integradores a asumir un aislamiento que no existe en todas las herramientas.

### Verificación de cierre

- Las pruebas unitarias y contractuales cubren viewer autorizado, tenant,
  household, dispositivo, área, capacidad, alternativas y rechazo antes del
  worker; la evidencia completa queda registrada en AUTH-003.
- Suite focalizada de AUTH-003: `23 passed`.
- Gate global más reciente: `619 passed, 18 skipped`; arquitectura `4 kept,
  0 broken`; PASS.
- Revisión transversal:
  [`specs/210-enforce-verified-authority/composition-review.md`](../../specs/210-enforce-verified-authority/composition-review.md).

## API-003 — Idempotencia REST solo en memoria

**Severidad:** media
**Estado:** cerrado — 2026-09-10
**Evidencia:** [`rest/recipe_adapter.py:34-79`](../../src/domoai/adapters/rest/recipe_adapter.py#L34), [`rest/recipe_adapter.py:160-215`](../../src/domoai/adapters/rest/recipe_adapter.py#L160), [`rest/recipe_adapter.py:235-309`](../../src/domoai/adapters/rest/recipe_adapter.py#L235), [`test_rest_recipe_idempotency.py`](../../tests/security/test_rest_recipe_idempotency.py), [`test_rest_idempotency_contract.py`](../../tests/contract/test_rest_idempotency_contract.py), [`test_rest_idempotency_composition.py`](../../tests/composition/test_rest_idempotency_composition.py)

El adapter propaga cada clave de comando en `Idempotency-Key` para delegar la
deduplicación de reinicios e instancias al proveedor REST. La caché local deja
de ser un `set` ilimitado: usa TTL monotónico de una hora y LRU de `1024`
entradas por defecto, con política validada y propagada por
`RestRecipeTransport`. Solo se recuerda una clave después de write y readback
confirmado; fallos y mismatches no quedan marcados como ejecutados.

### Impacto

Un retry tras reinicio puede repetir una escritura REST y un proceso de larga vida puede acumular memoria indefinidamente.

### Verificación de cierre

- Suite REST de seguridad, contrato, integración y composición: `13 passed`.
- La prueba de dos adapters frescos demuestra una sola escritura en un
  provider que honra `Idempotency-Key`; la caché local demuestra capacidad,
  LRU, TTL y ausencia de cacheado ante readback mismatch.
- Ruff: `All checks passed!`; mypy: `Success: no issues found in 245 source
  files`.
- Import Linter: `4 kept, 0 broken`.
- Gate global: `project-composition-check domoai` con `620 passed, 18 skipped`;
  arquitectura `4 kept, 0 broken`; PASS.
- Graphify post-cambio: `162 nodes, 428 edges`; consulta conectó adapter,
  builder, streaming, readback y tests de provider.
- Revisión transversal:
  [`specs/220-bound-rest-idempotency/composition-review.md`](../../specs/220-bound-rest-idempotency/composition-review.md).

El límite restante es contractual: un proveedor remoto que ignore
`Idempotency-Key` no puede ofrecer exactly-once tras reinicio mediante una
caché descartable; debe cualificarse antes de habilitar escrituras físicas.

## API-004 — Cobertura de adversarios insuficiente

**Severidad:** media
**Estado:** cerrado — 2026-09-10

La cobertura adversarial queda trazada a pruebas ejecutables. La nueva matriz
[`test_management_api.py:561-655`](../../tests/integration/test_management_api.py#L561)
recorre las 18 rutas mutadoras actuales (POST, PATCH y DELETE), usando dos
tokens válidos pero insuficientes (`read`/`VIEWER` y `plan`/`PLANNER`). Cada
caso exige `403`, evento `management_authorization_rejected`, ausencia de
material bearer y cero llamadas mutadoras al manager. Las rutas POST de solo
lectura —discovery, previews, OAuth discovery y validación— quedan fuera de
la matriz por contrato y conservan su prueba positiva.

El resto de categorías observadas tiene evidencia nominal:

- ORTools fuera de tenant, household, área, dispositivo y capacidad:
  [`test_ortools_authority.py`](../../tests/unit/mcp/test_ortools_authority.py),
  [`test_ortools_mcp_contract.py:248-285`](../../tests/contract/test_ortools_mcp_contract.py#L248).
- Dos planes con la misma clave y propietario preservado:
  [`test_phase3_multihost_ledger.py:161-190`](../../tests/integration/test_phase3_multihost_ledger.py#L161).
- Recuperación multi-host que no toca el dispatching del epoch/lease vigente:
  [`test_phase3_multihost_ledger.py:101-158`](../../tests/integration/test_phase3_multihost_ledger.py#L101),
  y fencing de host obsoleto en
  [`test_phase3_multihost_fencing.py`](../../tests/composition/test_phase3_multihost_fencing.py).
- Dos dispatchers concurrentes y lease outbox vigente/expirado:
  [`test_audit_outbox.py:88-185`](../../tests/integration/test_audit_outbox.py#L88).
- `$ref` remoto, endpoints internos y resolución local:
  [`test_rest_recipe_safety.py:47-130`](../../tests/security/test_rest_recipe_safety.py#L47).
- OAuth remoto sobre HTTP:
  [`test_oauth_preflight_safety.py:37-50`](../../tests/security/test_oauth_preflight_safety.py#L37),
  [`test_oauth_metadata_discovery_safety.py:65-81`](../../tests/security/test_oauth_metadata_discovery_safety.py#L65).
- Colas, cuerpos y respuestas por encima de límites:
  [`test_matter_transport_safety.py`](../../tests/security/test_matter_transport_safety.py),
  [`test_management_body_limits.py`](../../tests/security/test_management_body_limits.py),
  [`test_rest_recipe_connector.py:186-267`](../../tests/integration/test_rest_recipe_connector.py#L186),
  [`test_multihost_lab_v2_recovery.py:30-48`](../../tests/integration/test_multihost_lab_v2_recovery.py#L30).

### Verificación de cierre

- Suite adversarial focalizada: `96 passed`.
- Ruff: `All checks passed!`; mypy: `Success: no issues found in 245 source
  files`.
- Import Linter: `4 kept, 0 broken`.
- Gate global: `project-composition-check domoai` con `656 passed, 18 skipped`
  y arquitectura `4 kept, 0 broken`; PASS.
- Graphify post-cambio en modo code-only: `10.666 nodos`, `30.927 aristas`,
  `670 comunidades`; la consulta focal conectó middleware, política,
  manager, roles y la matriz de pruebas. La extracción semántica de 147
  documentos no se ejecutó por ausencia de API key.
- Revisión transversal:
  [`specs/221-adversarial-audit-coverage/composition-review.md`](../../specs/221-adversarial-audit-coverage/composition-review.md).

No quedan categorías de API-004 abiertas dentro del alcance auditable local.
Permanece la limitación ya declarada de infraestructura externa/laboratorio:
los tests skipped no sustituyen una cualificación real de proveedores,
coordinadores o dispositivos.
