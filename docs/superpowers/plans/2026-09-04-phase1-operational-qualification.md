# Cierre operativo de la Fase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminar el último warning de infraestructura de tests y demostrar la robustez de Fase 1 contra el laboratorio Docker disponible.

**Architecture:** El cambio queda confinado a la frontera de tests de composición. El contenedor Mosquitto se configura con una estrategia de espera estructurada y el runtime/adapters permanecen sin cambios. Las pruebas live se ejecutan desde variables locales ya existentes y su evidencia se separa de la qualification física.

**Tech Stack:** Python 3.12, pytest, pytest-asyncio, Testcontainers 4.15, Docker, DomoAI adapters y gates de Import Linter.

**Spec:** `docs/superpowers/specs/2026-09-04-phase1-operational-qualification-design.md`

## Global Constraints

- Mantener el runtime fail-closed y no cambiar autoridad, policy, admission ni retry físico.
- No imprimir ni persistir secretos de `dev/lab/.env`.
- Mantener los smokes live opt-in cuando el test requiera un endpoint externo o una escritura de laboratorio.
- Preservar cambios previos del workspace y no ejecutar operaciones Git destructivas.
- Usar `TMPDIR=/dev/shm` para pytest cuando la ejecución requiera evitar locks temporales en OneDrive.

---

### Task 1: Readiness estructurado del broker real

**Files:**
- Modify: `tests/composition/test_zigbee2mqtt_broker_composition.py:32-62`

**Interfaces:**
- Consumes: `DockerContainer.waiting_for` y `LogMessageWaitStrategy` de Testcontainers 4.15.
- Produces: fixture `mosquitto_port` sin uso de `wait_for_logs` ni `DeprecationWarning`.

- [x] **Step 1: Write the failing test**

Ejecutar el test existente con warnings deprecados tratados como errores:

```bash
TMPDIR=/dev/shm uv run pytest -q \
  tests/composition/test_zigbee2mqtt_broker_composition.py \
  -W error::DeprecationWarning
```

Resultado RED esperado: el fixture falla en `wait_for_logs(...)` con el
warning deprecado de Testcontainers.

- [x] **Step 2: Write minimal implementation**

Cambiar el import a:

```python
from testcontainers.core.wait_strategies import LogMessageWaitStrategy
```

Configurar el contenedor antes de `start()`:

```python
container = (
    DockerContainer("eclipse-mosquitto:1.6.15")
    .with_exposed_ports(1883)
    .waiting_for(
        LogMessageWaitStrategy("mosquitto version")
        .with_startup_timeout(30)
    )
)
container.start()
```

Eliminar la llamada posterior a `wait_for_logs` y conservar el `finally` que
detiene el contenedor.

- [x] **Step 3: Run the focused test**

```bash
TMPDIR=/dev/shm uv run pytest -q \
  tests/composition/test_zigbee2mqtt_broker_composition.py \
  -W error::DeprecationWarning
```

Resultado esperado: `1 passed`, sin warnings.

### Task 2: Qualification live del laboratorio disponible

**Files:**
- Read: `dev/lab/.env`
- Test: `tests/integration/test_zigbee2mqtt_smoke.py`
- Test: `tests/integration/test_modbus_smoke.py`
- Test: `tests/integration/test_matter_server_smoke.py`
- Test: `tests/integration/test_home_assistant_provider_runtime.py`
- Test: `tests/integration/test_home_assistant_provider_hil_smoke.py`

**Interfaces:**
- Consumes: endpoints y token del laboratorio local cargados solo en el
  entorno del proceso.
- Produces: evidencia de discovery/runtime round-trip con limpieza de
  adapters y runtime.

- [x] **Step 1: Run configured live qualification**

```bash
set -a
. dev/lab/.env
set +a
TMPDIR=/dev/shm uv run pytest -q \
  tests/integration/test_zigbee2mqtt_smoke.py \
  tests/integration/test_modbus_smoke.py \
  tests/integration/test_matter_server_smoke.py \
  tests/integration/test_home_assistant_provider_runtime.py \
  tests/integration/test_home_assistant_provider_hil_smoke.py
```

No se deben copiar valores del `.env` a logs, artefactos ni documentación.

- [x] **Step 2: Run real broker composition**

```bash
TMPDIR=/dev/shm uv run pytest -q \
  tests/composition/test_zigbee2mqtt_broker_composition.py
```

Resultado esperado: `1 passed` con Docker accesible.

### Task 3: Regression and composition gates

**Files:**
- Read: `src/domoai/runtime/operational_metrics.py`
- Read: `src/domoai/application/runtime_factory.py`
- Test: `tests/composition/test_composition_observability_composition.py`
- Test: `tests/integration/test_runtime_event_consumer.py`
- Modify: `docs/auditoria-fase-1-robustez-operativa.md`
- Modify: `docs/auditoria-domoai.md`

**Interfaces:**
- Consumes: runtime → metrics → MCP, event/audit outbox, adapter live
  boundaries y los checks del proyecto.
- Produces: matriz de evidencia exacta, riesgos residuales y criterio de
  salida actualizado.

- [x] **Step 1: Run focused resilience and contract tests**

```bash
TMPDIR=/dev/shm uv run pytest -q \
  tests/composition/test_adapter_liveness_composition.py \
  tests/composition/test_adapter_unbounded_reconnect_composition.py \
  tests/composition/test_bounded_runtime_workers_composition.py \
  tests/composition/test_composition_observability_composition.py \
  tests/composition/test_serialized_storage_composition.py \
  tests/integration/test_runtime_event_consumer.py \
  tests/integration/test_persistence_lifecycle.py \
  tests/integration/test_runtime_lifecycle.py
```

- [x] **Step 2: Run project gates**

```bash
TMPDIR=/dev/shm uv run pytest -q
uv run ruff check .
uv run mypy src
uv run python scripts/check_architecture_contracts.py
uv run python scripts/check_runtime_contract_docs.py
uv run lint-imports
project-composition-check "$(cat .ai/project-name)"
```

- [x] **Step 3: Update Markdown with observed results**

Registrar solo resultados obtenidos. Marcar el warning de Testcontainers como
cerrado y conservar como gates externos HIL físico, coordinación distribuida,
exportación remota de métricas y cualquier endpoint live no habilitado.

### Task 4: Composition review and final proof

**Files:**
- Read: final diff and changed test fixture.
- Modify: `docs/auditoria-fase-1-robustez-operativa.md`

**Interfaces:**
- Consumes: resultados de tests, arquitectura, contratos y composición.
- Produces: Composition Review Report y estado final de Fase 1.

- [x] **Step 1: Verify the change surface**

```bash
git diff --check
git status --short
rg -n 'wait_for_logs|LogMessageWaitStrategy' \
  tests/composition/test_zigbee2mqtt_broker_composition.py
```

- [x] **Step 2: Refresh structural evidence**

```bash
graphify . --update --no-viz --code-only
```

- [x] **Step 3: Record final verdict**

El veredicto será `PASS` para los gates automatizables y `PASS WITH RISKS`
si permanecen únicamente qualification física, coordinación distribuida o
exportación remota fuera del alcance de esta implementación.
