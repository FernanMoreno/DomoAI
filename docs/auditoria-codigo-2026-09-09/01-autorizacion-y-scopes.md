# Auditoría de autorización, identidad y scopes

## AUTH-001 — El plan puede reemplazar el ámbito verificado

**Severidad:** crítica
**Estado:** cerrado — 2026-09-10
**Evidencia:** [`authority.py:140-203`](../../src/domoai/application/authority.py#L140), [`domotics_server.py:189-201`](../../src/domoai/mcp/domotics_server.py#L189)

`AuthorityPolicy.bind_plan()` recibe una autoridad obtenida del token y un `Plan` que contiene una autoridad declarada por el llamante. El binding ahora construye una autoridad efectiva a partir de los claims verificados: tenant, principal, roles y membresía de hogares se conservan desde el token; las listas declaradas solo pueden ser subconjuntos; y las listas vacías heredan el scope verificado. El contexto MCP pasa además el mapa canónico dispositivo→área del `DeviceRegistry`.

Una declaración que amplíe área, dispositivo, capacidad u operación, introduzca un wildcard no verificado, o no pueda resolver un dispositivo dentro de un área restringida, termina con `INSUFFICIENT_SCOPE` antes de validar o persistir el plan.

### Verificación ejecutable

Las pruebas cubren herencia de scopes restringidos, narrowing válido, ampliación por cada dimensión, wildcard de operaciones, tenant/principal/roles no confiables y resolución de áreas:

```text
uv run pytest -q tests/unit/application/test_authority.py \
  tests/contract/test_phase3_identity_contract.py \
  tests/composition/test_phase3_identity_composition.py \
  tests/contract/test_domotics_mcp_contract.py \
  tests/integration/test_mcp_client_parity.py
62 passed
```

La regresión completa terminó con `2270 passed, 18 skipped`; la puerta
`project-composition-check "$(cat .ai/project-name)"` terminó con arquitectura
`4 kept, 0 broken`, `597 passed, 18 skipped` y `Composition checks: PASS`.
La revisión transversal está en
[`specs/208-authority-scope-binding/composition-review.md`](../../specs/208-authority-scope-binding/composition-review.md).

### Impacto

Puede ampliarse el ámbito de una operación o persistirse un plan cuya autoridad ya no representa los claims originales. El riesgo es especialmente importante si el plan se aprueba o ejecuta en otra petición o por otro componente.

### Cierre aplicado

- No se copian permisos del plan sobre los claims verificados.
- El scope declarado solo puede restringir el del token; el vacío hereda.
- El área real de cada dispositivo se valida con el registro canónico y falla
  cerrado si no está disponible.
- Las pruebas contractuales, unitarias, de composición, integración y las
  puertas arquitectónicas quedaron verdes.

## AUTH-002 — Management valida autenticación, pero no autorización

**Severidad:** alta
**Estado:** cerrado — 2026-09-10
**Evidencia:** [`gateway.py:58-87`](../../src/domoai/mcp/gateway.py#L58), [`auth.py:71-125`](../../src/domoai/management/auth.py#L71), [`api.py:84-133`](../../src/domoai/management/api.py#L84), [`api.py:778-806`](../../src/domoai/management/api.py#L778)

El gateway conserva el `AccessToken` verificado y pasa al Management una política
de despliegue derivada de tenant y household. El middleware construye un
`ManagementPrincipal` no secreto, clasifica cada petición como `read` o
`mutate`, y ejecuta primero el control de scope y después
`AuthorityPolicy.authorize()` para tenant, household, rol y operación.

Las credenciales ausentes o inválidas devuelven 401. Un token autenticado sin
scope, rol, tenant, household u operación suficientes devuelve 403 antes de
invocar el handler. GET/HEAD/OPTIONS, discovery, previews y validación de
conexiones son lecturas explícitas; las demás rutas inseguras, incluidas las
desconocidas, requieren mutación.

### Impacto

El camino de Management ya no convierte la verificación en un booleano ni
permite que un token de lectura alcance mutaciones o que un token válido de otro
tenant/household llegue al handler.

### Verificación ejecutable

Las pruebas HTTP cubren 401, token de lectura bloqueado antes de la mutación,
tenant/household incorrectos, matriz de rutas, fail-closed para rutas inseguras
desconocidas, compatibilidad estática y auditoría sin bearer:

```text
uv run pytest -q tests/unit/management/test_auth.py \
  tests/integration/test_management_api.py \
  tests/unit/mcp/test_gateway_auth.py tests/unit/mcp/test_gateway.py
42 passed, 1 warning
```

La regresión completa terminó con `2279 passed, 18 skipped`. La puerta
`project-composition-check "$(cat .ai/project-name)"` terminó con arquitectura
`4 kept, 0 broken`, `604 passed, 18 skipped` y `Composition checks: PASS`.
Ruff, mypy (`244 source files`), Import Linter (`4 kept, 0 broken`) y
`git diff --check` quedaron verdes. La revisión transversal está en
[`specs/209-management-route-authorization/composition-review.md`](../../specs/209-management-route-authorization/composition-review.md).

### Cierre aplicado

- Se propaga el `AccessToken` y se preserva su autoridad efectiva sin exponer el
  bearer.
- Se declara y aplica la matriz de permisos por método y ruta antes del
  handler.
- Se reaplica tenant, household, rol y operación mediante `AuthorityPolicy`.
- Las decisiones autorizadas y rechazadas se auditan con actor, ruta, scope y
  autoridad no secreta.
- Se conservan los caminos de loopback y token estático.

## AUTH-003 — Herramientas ORTools sin política de autoridad

**Severidad:** alta
**Estado:** cerrado — 2026-09-10
**Evidencia:** [`ortools_server.py:66-170`](../../src/domoai/mcp/ortools_server.py#L66), [`ortools_server.py:292-485`](../../src/domoai/mcp/ortools_server.py#L292), [`configured.py:176-186`](../../src/domoai/mcp/configured.py#L176)

El contexto ORTools recibe ahora la `AuthorityPolicy` del despliegue. Cada
petición obtiene la autoridad verificada de `current_authority()` (o el
contexto local de servicio para fixtures sin transporte), extrae los recursos
de cargas, estados y actuadores, y comprueba dispositivo, área canónica,
capacidad, tenant, household y operación `inspect` antes de validación,
optimización o comparación. Las proyecciones de explicación y resumen
revalidan todos los planes, incluidas alternativas y postcondiciones, sin
confiar en la autoridad embebida en el resultado.

La documentación declara que las respuestas se filtran al scope autenticado y
el contrato queda aplicado en este camino: [`unified-mcp.md:51-55`](../unified-mcp.md#L51).

### Impacto

Un cliente autenticado ya no puede consultar, optimizar ni proyectar escenarios
o resultados fuera de su ámbito. El control permanece de solo lectura y no
concede autoridad de ejecución ni llama a adaptadores.

### Verificación ejecutable

Las pruebas cubren viewer autorizado, dispositivo/área/capacidad fuera de
ámbito, tenant/household distintos, variaciones de comparación, planes
alternativos no autorizados, ausencia de worker y propagación desde el builder
configurado:

```text
uv run pytest -q \
  tests/unit/mcp/test_ortools_authority.py \
  tests/contract/test_ortools_mcp_contract.py \
tests/integration/test_ortools_mcp_fixtures.py \
  tests/integration/test_ortools_mcp_parity.py \
  tests/composition/test_phase3_identity_composition.py
23 passed
```

La regresión completa terminó con `2289 passed, 18 skipped`. La puerta
`project-composition-check "$(cat .ai/project-name)"` terminó con arquitectura
`4 kept, 0 broken`, `605 passed, 18 skipped` y `Composition checks: PASS`.
Ruff, mypy (`244 source files`), Import Linter (`4 kept, 0 broken`) y
`git diff --check` quedaron verdes. La revisión transversal está en
[`specs/210-enforce-verified-authority/composition-review.md`](../../specs/210-enforce-verified-authority/composition-review.md).

### Cierre aplicado

- Se propaga `AuthorityPolicy` desde la configuración al contexto ORTools.
- Se autoriza el conjunto completo de recursos contra claims verificados y el
  registro canónico antes del worker.
- Se protegen también explicación, resumen y todas las alternativas de un
  resultado suministrado por el cliente.
- Se preservan contratos v1, paridad de transportes, worker acotado y ausencia
  de llamadas a adaptadores en el camino de propuesta.
