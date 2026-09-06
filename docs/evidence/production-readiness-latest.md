# Evidencia de readiness operativa

**Fecha:** 2026-09-05

**Resultado:** `SOFTWARE_AND_PROCESS_LAB_VERIFIED`

Este documento consolida las comprobaciones ejecutadas después del cierre de
las fases 0–4. No convierte un entorno local en un despliegue productivo ni
convierte un gemelo digital en qualification HIL física.

## Estado comprobado

| Puerta | Resultado | Evidencia |
|---|---|---|
| Gateway MCP local | PASS | `http://127.0.0.1:8124/healthz` devuelve 200; `/readyz` devuelve 503 únicamente por `physical_actuator_not_qualified`. |
| MCP real | PASS | Cliente Streamable HTTP: 32 tools, runtime revision presente, adapter compuesto conectado y event consumer vivo. |
| Operación multi-adapter | PASS | 12 comandos live previos, seis rutas de actuador, todos con `confirmed_success`, readback actual y estado final apagado. |
| Métricas operativas | PASS | 12 éxitos, 0 fallos/rechazos/indisponibles/desconocidos, 0 mismatches, 0 overflow y 0 fallos de telemetría. |
| Auth de gateway | PASS en perfil local provisionado | 28 tests HTTP/contrato/auth; cuatro clientes autenticados comparten un runtime canónico. |
| Ciclo de tokens | PASS | Rotación atómica, autenticación del bearer recién emitido, bearer ausente del fichero, revocación efectiva tras reload. El bearer no se registró en este informe. |
| Preflight estático | PASS | `deploy/gateway.env` y `deploy/clients.json` locales pasan environment, auth, compose, proxy y referenced-files; no se imprimen secretos. |
| Gemelo digital | PASS | `twin-187`, seis adapters, diez dominios, catorce checks; detalle en [`digital-twin-latest.md`](digital-twin-latest.md). |
| Laboratorio de proceso | PASS | `uv run domoai-lab smoke`: 80 passed, 1 skipped. Detalle en [`process-lab-latest.md`](process-lab-latest.md). |
| HIL físico | BLOQUEADO | No hay batería/EV/cableado/firmware/radio físico disponible para aportar evidencia independiente. |
| Active-active/fencing distribuido | NO HABILITADO | El runtime conserva un único owner por deployment; no se añade una segunda autoridad sin proveedor de lease y fencing monotónico. |
| Exportación remota | PASS en software/configuración, opt-in | Spec 188 añade `GET /metrics` Prometheus pull, bearer hash-only, límite de 262144 bytes y proxy Caddy; no hay agregación histórica ni multi-réplica. |

## Auth de producción: verificación reproducible

La superficie de producción ya está implementada en el código y en el
despliegue:

- `DOMOAI_MCP_CLIENT_TOKEN_FILE` es obligatorio para un bind no-loopback.
- El preflight exige HTTPS, fichero de tokens usable, montaje read-only del
  fichero y frontera Caddy válida.
- `TokenFileManager` solo persiste hashes SHA-256 y escribe con permisos
  `0600` y replace atómico.
- `StaticBearerTokenVerifier.reload()` conserva el conjunto anterior si el
  nuevo documento es inválido.
- El bearer solo se entrega una vez al canal administrativo de rotación y no
  se copia al repositorio, auditoría, respuesta del probe ni métricas.

El perfil local ya tiene los ficheros ignorados y pasa el preflight estático.
La activación real aún requiere sustituir el placeholder de
`DOMOAI_HOME_ASSISTANT_TOKEN` y aportar certificados/TLS, DNS, firewall y
endpoints operativos. No se han creado credenciales ficticias en Git para
forzar un PASS.

## Exportación remota de métricas

La implementación de Spec 188 queda en [`remote_metrics.py`](../../src/domoai/mcp/remote_metrics.py)
y [`unified_server.py`](../../src/domoai/mcp/unified_server.py):

- `DOMOAI_MCP_METRICS_ENABLED=false` por defecto; al habilitarlo exige el
  fichero de bearer del gateway.
- `GET /metrics` valida el bearer con el mismo `StaticBearerTokenVerifier`,
  devuelve exposición Prometheus bounded y falla cerrado ante snapshot/render
  inválido o exceso de tamaño.
- El proxy Caddy solo reenvía las rutas MCP, health/readiness y metrics; el
  fallback continúa devolviendo 404.
- 25 tests enfocados, incluida una prueba de runtime real, cubren disabled/401/200,
  ausencia de secretos, límite duro y proxy.

## Coordinación, pools y métricas

La evaluación ejecutada no justifica active-active ni un pool distribuido:

- El MCP live observó `event_consumer_alive=true`, `adapter_connected=true`,
  `telemetry_failure_total=0` y `series_overflow_total=0`.
- Una carga local de 100 operaciones sobre `SerializedStorageExecutor` con
  cola bounded de 32 produjo 98 completadas, 2 rechazos por overload, 0
  timeouts, 0 errores y `p95_wait_seconds=0.016188`.
- El caso del solver de 50 cargas pasó; su ejecución medida por el test quedó
  bajo el deadline interno de 5 segundos, aunque el proceso pytest completo
  tardó 8.24 s por importación y arranque.

Estos datos sirven para ajustar límites locales y demostrar backpressure. No
son una prueba de partición de red, fencing, failover ni carga multi-host.
La decisión sigue siendo single-writer hasta que exista una necesidad medida
de réplica, backlog sostenido o retención histórica remota.

## Verificación final — 2026-09-05

```text
uv run pytest -q                                      1799 passed, 18 skipped
uv run ruff check .                                   All checks passed
uv run mypy src                                       Success: no issues found in 163 files
uv run python scripts/check_architecture_contracts.py architecture contracts kept
uv run python scripts/check_runtime_contract_docs.py  runtime contract documentation is coherent
docker compose --env-file deploy/gateway.env -f deploy/compose.yaml config --quiet  PASS
project-composition-check "$(cat .ai/project-name)" 504 passed, 18 skipped
graphify . --update --no-viz --code-only              7505 nodes, 23213 edges, 453 communities
```

El preflight y Compose se verificaron sin arrancar contenedores ni contactar
dependencias físicas. Graphify emitió la advertencia conocida de 14 migraciones
SQL sin `tree_sitter_sql`; no afecta a la extracción Python ni a las gates del
runtime. Durante la primera pasada global se aisló un flake preexistente en el
fixture de horizonte del workflow al cruzar un cambio de minuto; el helper se
estabilizó por proceso sin relajar la validación estricta del proveedor y la
regresión final pasó completa.

## Gates que permanecen abiertas

1. Sustituir el placeholder de `DOMOAI_HOME_ASSISTANT_TOKEN` en el perfil
   local, aportar certificados, secretos externos y referencias host-side;
   ejecutar después:

   ```bash
   uv run domoai-admin deployment preflight
   uv run domoai-admin deployment preflight --network
   ```

2. Ejecutar commissioning atendido con hardware identificado y conservar
   evidencia independiente de batería/EV/HVAC/actuadores de riesgo. La
   evidencia digital y de laboratorio actual no satisface esa gate.

3. Si la operación requiere varias réplicas o histórico remoto, abrir una
   especificación separada para lease externo, fencing monotónico,
   idempotencia entre réplicas, pool por hogar y agregación remota.

## Veredicto

El proyecto está completo en su alcance de software, contratos, seguridad,
identidad, producto y laboratorio reproducible. Está listo para continuar a
provisión controlada, pero no debe declararse listo para autonomía física
productiva hasta cerrar las gates externas anteriores.
