# Auditoría de SSRF, OAuth, transportes y límites

## NET-001 — Resolución de `$ref` remoto durante validación JSON Schema

**Severidad:** alta
**Estado:** cerrado — 2026-09-10
**Evidencia:** [`openapi_profile.py:49-111`](../../src/domoai/adapters/rest/openapi_profile.py#L49), [`openapi_profile.py:139-153`](../../src/domoai/adapters/rest/openapi_profile.py#L139), [`openapi_profile.py:257-298`](../../src/domoai/adapters/rest/openapi_profile.py#L257)

El perfil REST ahora rechaza durante su construcción cualquier `$ref`,
`$dynamicRef` o `$recursiveRef` que no sea un fragmento local. Los fragmentos
locales se reescriben a un URI sintético en memoria, se comprueban contra el
documento OpenAPI suministrado y se validan con un `Registry` que no tiene
retriever de red. Los schemas inline y los refs locales válidos se conservan.

La prueba de seguridad intercepta `urllib.request.urlopen`: los refs remotos
son rechazados con cero peticiones y los refs locales se validan con cero
peticiones. Los refs locales no resueltos fallan al construir el perfil.

### Impacto

La validación de respuestas ya no puede usar la red para adquirir schemas, por
lo que el documento de receta no puede convertir la validación en SSRF.

### Verificación de cierre

- Suite REST unit/integration/security/contract: `13 passed`.
- Ruff y mypy (`2 source files`) quedaron verdes.
- Import Linter: `4 kept, 0 broken`; `git diff --check` quedó verde.
- Revisión transversal:
  [`specs/213-block-remote-json/composition-review.md`](../../specs/213-block-remote-json/composition-review.md).

## NET-002 — Allowlist de hosts vacía por defecto

**Severidad:** alta
**Estado:** cerrado — 2026-09-10
**Evidencia:** [`connector_recipes.py:102-129`](../../src/domoai/application/connector_recipes.py#L102), [`connector_recipes.py:136-189`](../../src/domoai/application/connector_recipes.py#L136), [`connector_recipes.py:195-312`](../../src/domoai/application/connector_recipes.py#L195), [`test_connector_recipe_safety.py`](../../tests/security/test_connector_recipe_safety.py)

`RecipeConnectionProvider` conserva la política de destinos como configuración
propiedad del despliegue y la propaga tanto a `probe` como a `validate` antes de
construir un adapter. La validación falla cerrado cuando no hay hosts ni CIDR
declarados (`recipe_network_policy_missing`), acepta únicamente hosts exactos o
IPs dentro de CIDR explícitos y puede restringir puertos y esquemas. Las
excepciones para loopback o redes privadas solo funcionan cuando la declaración
de despliegue las incluye explícitamente. La validación no resuelve DNS ni abre
conexiones.

### Impacto

Los recipes de MQTT, Modbus, KNX o Matter pueden apuntar a IPs privadas, servicios de loopback o endpoints de metadata. El problema se combina con una API de administración que no aplica scopes suficientemente estrictos.

### Verificación de cierre

- Suite provider/security/composition/contract: `13 passed`.
- Ruff y mypy sobre los archivos modificados quedaron verdes.
- El análisis previo de Import Linter quedó verde (`4 kept, 0 broken`); la
  verificación combinada final del lote se ejecutará tras OAUTH-001.
- Graphify refrescado: `144 nodes, 392 edges`; consulta final: `187 nodes` y
  `110` relacionados con el cambio.
- Revisión transversal:
  [`specs/214-require-explicit-connector/composition-review.md`](../../specs/214-require-explicit-connector/composition-review.md).

## OAUTH-001 — OAuth remoto permite HTTP sin cifrar

**Severidad:** alta
**Estado:** cerrado — 2026-09-10
**Evidencia:** [`oauth_urls.py:10-28`](../../src/domoai/security/oauth_urls.py#L10), [`oauth_preflight.py:216-221`](../../src/domoai/application/oauth_preflight.py#L216), [`oauth.py:31-42`](../../src/domoai/adapters/home_assistant/oauth.py#L31), [`oauth.py:112-207`](../../src/domoai/adapters/home_assistant/oauth.py#L112), [`test_oauth_preflight.py`](../../tests/unit/application/test_oauth_preflight.py), [`test_oauth.py`](../../tests/unit/adapters/home_assistant/test_oauth.py), [`test_oauth_metadata_discovery_safety.py`](../../tests/security/test_oauth_metadata_discovery_safety.py)

La política compartida exige HTTPS para endpoints remotos de autorización,
token, revocación, redirect y metadata. Solo permite HTTP en los hosts de
loopback explícitos `localhost`, `127.0.0.1` y `::1`; también rechaza
credenciales, fragments y URLs sin host sin realizar I/O. El cliente aplica la
misma política a endpoints recibidos desde metadata y configura
`follow_redirects=False` para metadata, token y revocación, por lo que una
respuesta 3xx falla sin seguir el destino.

### Impacto

El authorization code, refresh token o access token puede quedar expuesto a observadores de red o sufrir manipulación.

### Verificación de cierre

- Suite OAuth unit/security/contract: `39 passed`.
- Ruff: `All checks passed!`.
- mypy sobre los tres archivos fuente modificados: `Success: no issues found`.
- Import Linter: `Analyzed 245 files, 926 dependencies`; `4 kept, 0 broken`.
- Graphify refrescado sobre las fronteras OAuth: `95 nodes, 247 edges`; la
  consulta de impacto localizó `101` nodos y `validate_oauth_url` quedó
  relacionado con preflight, configuración y metadata.
- Revisión transversal:
  [`specs/215-enforce-secure-oauth/composition-review.md`](../../specs/215-enforce-secure-oauth/composition-review.md).

## RES-001 — Colas y frames de Matter/KNX sin backpressure

**Severidad:** media-alta
**Estado:** cerrado — 2026-09-10
**Evidencia:** [`matter/transport.py:110-159`](../../src/domoai/adapters/matter/transport.py#L110), [`matter/transport.py:215-274`](../../src/domoai/adapters/matter/transport.py#L215), [`knx/transport.py:185-219`](../../src/domoai/adapters/knx/transport.py#L185), [`knx/transport.py:342-407`](../../src/domoai/adapters/knx/transport.py#L342), [`test_matter_transport_safety.py`](../../tests/security/test_matter_transport_safety.py), [`test_knx_transport_safety.py`](../../tests/security/test_knx_transport_safety.py)

Matter configura un límite finito y configurable de frame (`1 MiB` por
defecto) y una cola de eventos finita (`1000` por defecto). KNX usa la misma
capacidad finita configurable para sus eventos. Cuando la cola está llena,
ambos transportes descartan el evento nuevo de forma síncrona e incrementan un
contador observable; la profundidad, capacidad y drops se pueden consultar sin
exponer payloads. La cola de eventos de Matter sigue separada de las futures de
respuestas, de modo que un flood no bloquea la respuesta a un comando.

### Impacto

Un servidor comprometido o un dispositivo ruidoso puede forzar crecimiento de memoria hasta degradar o detener el gateway.

### Verificación de cierre

- Tests Matter/KNX de seguridad, contratos y composición: `78 passed`.
- Gate global de composición: `613 passed, 18 skipped`; arquitectura `4 kept,
  0 broken`; PASS.
- Ruff y mypy sobre los dos transportes modificados quedaron verdes.
- Graphify post-cambio: `306 nodes, 782 edges`; consulta final: `426 nodes` y
  `234` relacionados con la ruta de backpressure.
- Revisión transversal:
  [`specs/216-bound-matter-and/composition-review.md`](../../specs/216-bound-matter-and/composition-review.md).

## RES-002 — REST comprueba el límite después de materializar la respuesta

**Severidad:** media
**Estado:** cerrado — 2026-09-10
**Evidencia:** [`rest/recipe_adapter.py:212-258`](../../src/domoai/adapters/rest/recipe_adapter.py#L212), [`test_rest_recipe_connector.py`](../../tests/integration/test_rest_recipe_connector.py), [`test_rest_response_stream_contract.py`](../../tests/contract/test_rest_response_stream_contract.py)

`RestRecipeAdapter._request_json()` usa ahora el contexto de streaming del
cliente propietario. Comprueba redirect/status y un `Content-Length` declarado
antes de leer; para cuerpos chunked acumula solo chunks fijos y rechaza el
primer chunk que superaría `max_response_bytes`, sin acceder a
`response.content`. El contexto de la respuesta se cierra y el `AsyncClient`
del caller permanece abierto.

### Verificación de cierre

- Suite REST security/integration/contract/composition: `18 passed`.
- Gate global de composición: `617 passed, 18 skipped`; arquitectura `4 kept,
  0 broken`; PASS.
- Ruff, mypy e Import Linter (`4 kept, 0 broken`) quedaron verdes.
- Graphify post-cambio: `98 nodes, 266 edges`; consulta final: `123 nodes` y
  `99` relacionados con stream, límites y ownership.
- Revisión transversal:
  [`specs/217-stream-rest-response-limits/composition-review.md`](../../specs/217-stream-rest-response-limits/composition-review.md).

## SECRET-001 — Filtro de secretos incompleto en texto público

**Severidad:** media
**Estado:** cerrado — 2026-09-10
**Evidencia:** [`connector_recipes.py:17-35`](../../src/domoai/domain/connector_recipes.py#L17), [`connector_recipes.py:404-446`](../../src/domoai/domain/connector_recipes.py#L404), [`test_connector_recipes.py:148-179`](../../tests/unit/domain/test_connector_recipes.py#L148), [`test_connector_recipe_safety.py:36-58`](../../tests/security/test_connector_recipe_safety.py#L36), [`test_connector_recipe_contract.py:81-107`](../../tests/contract/test_connector_recipe_contract.py#L81), [`test_recipe_secret_safety_composition.py`](../../tests/composition/test_recipe_secret_safety_composition.py)

La validación de texto público conserva los patrones de claves/valores y URLs
con credenciales, y añade detección de valores Bearer token-shaped y parsing
recursivo de strings JSON. Así se bloquean Bearer/JWT y cabeceras JSON
`Authorization` anidadas en display names, templates, paths y discovery
options. Los errores usan únicamente la categoría `contains secret material` y
no interpolan el valor detectado. Palabras descriptivas como `Bearer protocol`
y los templates aprobados continúan siendo válidos.

### Verificación de cierre

- Suite enfocada de dominio, seguridad, contrato, scaffold y composición:
  `40 passed`.
- Ruff: `All checks passed!`; mypy: `Success: no issues found in 1 source
  file`.
- Import Linter: `4 kept, 0 broken`.
- Gate global: `project-composition-check domoai` con `618 passed, 18 skipped`;
  arquitectura `4 kept, 0 broken`; PASS.
- Graphify post-cambio: `176 nodes, 418 edges`; consulta de impacto: `206
  nodes`, conectando el modelo, consumidores y pruebas nuevas.
- Revisión transversal:
  [`specs/218-harden-recipe-secret-detection/composition-review.md`](../../specs/218-harden-recipe-secret-detection/composition-review.md).
