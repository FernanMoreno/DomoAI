# Evidencia Fase 2 — producto agentic

Fecha: 2026-09-05

## Gates software locales

| Gate | Evidencia | Estado |
|---|---|---|
| Comparación pública | `compare_scenarios`, worker bounded, diff sin fabricación | PASS |
| Escena segura | `execute_scene` → digest → plan/policy → bundle → admission → readback | PASS |
| Skills portables | 9 contratos v4 validados por `load_core_catalog()` | PASS |
| Paridad MCP | catálogo/revisión/digest equivalentes entre clientes | PASS |
| Scopes y reconexión | identidad request-local, scope read-only, cancelación y reconnect | PASS |

Comandos focales:

```bash
uv run pytest -q \
  tests/contract/test_ortools_mcp_contract.py \
  tests/unit/optimizer/test_counterfactual_async.py \
  tests/integration/test_phase2_scene_execution.py \
  tests/composition/test_phase2_agentic_composition.py \
  tests/contract/test_skill_catalog.py \
  tests/integration/test_mcp_gateway_http.py
```

## Gate externo físico

Estado: **BLOCKED / FAIL-CLOSED hasta commissioning real**.

La evidencia local usa fixtures, gemelo digital y HIL de proceso. No demuestra
por sí sola identidad de hardware, límites eléctricos, fencing, lease,
readback físico ni seguridad de una instalación. Para pasar el gate se debe
aportar, fuera de CI local:

1. dispositivo físico identificado y firmware registrado;
2. capability/route cualificada y scope de autoridad;
3. HIL atendido con límites y lease/fencing;
4. write de prueba segura y readback independiente;
5. evidencia persistida y ligada a la revisión/digest ejecutado.

Mientras falte cualquiera de esos elementos, `scheduled`, proposal o fixture
success no se presentan como acción física completada.
