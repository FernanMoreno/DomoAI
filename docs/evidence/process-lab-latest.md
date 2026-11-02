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

## Actualización — 2026-09-09

Se cargó el perfil ignorado `dev/lab/.env` sin copiar sus valores a la
evidencia. El launcher `dev/lab/knx-gateway/run-wsl.sh` inició `knxd` nativo y
publicó el endpoint local `3672`.

La batería de smoke tests live de lectura y descubrimiento se ejecutó con:

```bash
set -a
. dev/lab/.env
set +a
DOMOAI_LIVE_BATTERY_KNX_GATEWAY_ENABLE=1 \
uv run pytest -q \
  tests/integration/test_home_assistant_provider_smoke.py \
  tests/integration/test_zigbee2mqtt_smoke.py \
  tests/integration/test_matter_server_smoke.py \
  tests/integration/test_knx_smoke.py \
  tests/integration/test_knx_gateway_live.py \
  tests/integration/test_modbus_smoke.py
```

Resultado: `6 passed in 19.28s`. Home Assistant, Zigbee2MQTT, Matter, KNX,
KNX gateway/túnel y Modbus quedaron verificados contra sus servicios locales.
Esta ejecución no realizó comandos de actuadores. En ese momento,
`test_knx_hil_smoke.py` y los recorridos HIL permanecían pendientes de
confirmación operativa explícita antes de enviar `turn_on`/`turn_off`, incluso
en el perfil virtual.

## Actualización HIL — 2026-09-09

Con autorización operativa explícita se ejecutaron los dos recorridos HIL de
comando sobre los perfiles virtuales del laboratorio, cargando `dev/lab/.env`
sin registrar sus secretos:

```bash
set -a
. dev/lab/.env
set +a
uv run pytest -q tests/integration/test_knx_hil_smoke.py
uv run pytest -q \
  tests/integration/test_home_assistant_provider_hil_smoke.py::test_home_assistant_provider_hil_command_round_trip
```

Resultados:

- KNX Virtual: `1 passed in 10.49s`.
- Home Assistant virtual: `1 passed in 8.57s`.

Ambos recorridos completaron `turn_on → readback → turn_off`; el cleanup
garantizado del test se ejecutó correctamente. No se habilitó el HIL de
batería real, que requiere una confirmación y un perfil de seguridad
independientes.
