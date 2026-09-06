# Cualificación del laboratorio de proceso

**Fecha:** 2026-09-04
**Clasificación:** `process_lab`
**No es:** evidencia `hardware` ni HIL físico.

## Preflight

- `knxd` estaba escuchando en `127.0.0.1:3672` y `127.0.0.1:3673`.
- El laboratorio Docker estaba saludable para MQTT, Zigbee2MQTT, Modbus,
  Home Assistant, Matter, batería, EV y térmico.
- Las flags de ejecución física/HIL de batería permanecieron desactivadas.

## Puertas ejecutadas

### Smoke local

```bash
uv run domoai-lab smoke
```

Resultado: `80 passed, 1 skipped`.

### Adapters y composición live de proceso

```bash
DOMOAI_LIVE_BATTERY_KNX_GATEWAY_ENABLE=1 \
DOMOAI_LIVE_MCP_KNX_BATTERY_ENABLE=1 \
DOMOAI_LIVE_COMPOSITION_ENABLE=1 \
uv run pytest -q \
  tests/integration/test_knx_gateway_live.py \
  tests/integration/test_live_mcp_knx_battery_e2e.py \
  tests/integration/test_live_multi_adapter_composition.py \
  tests/integration/test_home_assistant_provider_smoke.py \
  tests/integration/test_zigbee2mqtt_smoke.py \
  tests/integration/test_modbus_smoke.py \
  tests/integration/test_matter_server_smoke.py
```

Resultado: `7 passed`, repetido dos veces consecutivas.

La composición cubrió discovery y readback de Home Assistant, Zigbee2MQTT,
KNX, Modbus y Matter; el flujo MCP → scheduler → KNX → batería virtual →
readback → persistencia; y el retorno seguro de los actuadores a cero/apagado.

### Recorridos de proceso etiquetados HIL

```bash
uv run pytest -q \
  tests/integration/test_knx_hil_smoke.py \
  tests/integration/test_home_assistant_provider_hil_smoke.py
```

Resultado: `2 passed, 1 skipped`. El resultado corresponde a KNX Virtual y
Home Assistant del laboratorio de proceso. El caso omitido exige identidad,
perfil, dirección, potencia de prueba y confirmación explícita de batería
real; no se convierte en pass mediante el simulador.

## Incidencia corregida

El smoke live de Home Assistant seleccionaba `states[0]`, que en la instancia
real era un sensor histórico `invalid`; `get_state(allow_stale=False)` lo
filtraba correctamente. El test ahora selecciona un snapshot `current`, sin
relajar el filtrado fail-closed de producción.

## Límite de la evidencia

Este artefacto demuestra composición contra servicios de proceso y dispositivos
virtuales locales. No demuestra cableado, firmware, radio, instalación
eléctrica, ETS con hardware físico ni qualification HIL de batería/EV.
