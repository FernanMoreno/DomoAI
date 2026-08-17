# DomoAI Virtual Domotics Lab

Este laboratorio simula las fronteras de protocolo que consume DomoAI. No
simula radios Zigbee ni sustituye el commissioning Matter, ETS/KNX o la
validación con hardware.

## Runner local reproducible

El runner `domoai-lab` centraliza las operaciones seguras del laboratorio y no
requiere una instalación domótica real:

```bash
uv run domoai-lab up
uv run domoai-lab status
uv run domoai-lab smoke
uv run domoai-lab down
```

El arranque por defecto cubre MQTT/Zigbee2MQTT y Modbus. Los perfiles
opcionales se activan explícitamente:

```bash
uv run domoai-lab up --services mqtt zigbee2mqtt modbus homeassistant
uv run domoai-lab up --services matter-server
```

`dev/lab/.env` es opcional, está ignorado por Git y solo se carga en el
subproceso de Docker para `up`, `status` y `down`. `domoai-lab smoke` no carga
ese archivo, elimina las variables `DOMOAI_*` del proceso hijo y ejecuta solo
fixtures locales; por tanto no activa OMIE, Open-Meteo, Home Assistant live,
Matter commissioning ni KNX/IP.

Para detener y borrar volúmenes de forma explícita:

```bash
uv run domoai-lab down --volumes
```

Si `docker` no está en `PATH`, usa `--docker-bin` o `DOMOAI_DOCKER_BIN`. El
runner valida los servicios antes de lanzar Compose y nunca imprime valores
del archivo de entorno.

## MVP local: MQTT/Zigbee2MQTT + Modbus TCP

Requisitos: Docker Compose y `uv`.

Si esta distro WSL no expone `docker` en el `PATH` aunque Docker Desktop esté
integrado, el daemon puede usarse mediante el ejecutable Windows:

```bash
export DOMOAI_DOCKER_BIN="/mnt/c/Program Files/Docker/Docker/resources/bin/docker.exe"
"$DOMOAI_DOCKER_BIN" compose -f dev/lab/compose.yaml up -d --build mqtt zigbee2mqtt modbus
```

En una instalación con integración WSL normal, sustituye
`"$DOMOAI_DOCKER_BIN" compose` por `docker compose`.

```bash
docker compose -f dev/lab/compose.yaml up -d --build mqtt zigbee2mqtt modbus
docker compose -f dev/lab/compose.yaml ps
```

Endpoints desde el host:

- Mosquitto/fake Zigbee2MQTT: `mqtt://127.0.0.1:1883`.
- PyModbus simulator: `127.0.0.1:1502`.
- PyModbus web console: `http://127.0.0.1:8081`.

Configura DomoAI en la shell, sin guardar secretos:

```bash
export DOMOAI_ZIGBEE2MQTT_URL="mqtt://127.0.0.1:1883"
export DOMOAI_ZIGBEE2MQTT_BASE_TOPIC="zigbee2mqtt"
export DOMOAI_MODBUS_HOST="127.0.0.1"
export DOMOAI_MODBUS_PORT="1502"
export DOMOAI_MODBUS_CONFIG_PATH="dev/lab/configs/modbus.json"

uv run pytest -q tests/integration/test_zigbee2mqtt_smoke.py \
  tests/integration/test_modbus_smoke.py
```

El fake Zigbee2MQTT publica `bridge/state`, `bridge/devices`, estados
retenidos, disponibilidad y acepta únicamente payloads `/set` de los campos
que cada dispositivo virtual ya expone. El broker de desarrollo no tiene
autenticación; para probar autenticación, configura el broker fuera del
default y usa `DOMOAI_MQTT_USERNAME`/`DOMOAI_MQTT_PASSWORD` solo en el entorno.

## Reproducir estados

Publicar un estado de luz desde el host:

```bash
docker compose -f dev/lab/compose.yaml exec mqtt \
  mosquitto_pub -h mqtt -t zigbee2mqtt/living_room/main_light/set \
  -m '{"state":"OFF","brightness":80}'
```

Consultar los topics retenidos:

```bash
docker compose -f dev/lab/compose.yaml exec mqtt \
  mosquitto_sub -h mqtt -t 'zigbee2mqtt/#' -v -C 8
```

La consola HTTP de PyModbus permite observar y cambiar puntos para escenarios
de polling, disponibilidad y codecs. El mapping DomoAI está en
`configs/modbus.json`; sus offsets son PDU y no etiquetas `40001`.

## Home Assistant en el mismo stack

Para arrancar Home Assistant junto con MQTT/Zigbee2MQTT y Modbus, usa el
mismo proyecto Compose y su perfil explícito:

```bash
docker compose -f dev/lab/compose.yaml --profile homeassistant up -d --build \
  mqtt zigbee2mqtt modbus homeassistant
docker compose -f dev/lab/compose.yaml ps
```

Todos los servicios comparten la red del proyecto `domoai-lab`. Desde el host,
Home Assistant queda en `http://127.0.0.1:8123`; desde otro contenedor del
laboratorio, la URL es `http://homeassistant:8123`.

En el primer arranque abre `http://127.0.0.1:8123`, completa el onboarding y
genera un token long-lived local. El token se configura solo en la shell y no
se guarda en el repositorio:

```bash
export DOMOAI_HOME_ASSISTANT_URL="http://127.0.0.1:8123"
export DOMOAI_HOME_ASSISTANT_TOKEN="<set-locally>"
uv run pytest -q tests/integration/test_home_assistant_smoke.py
```

Para validar además la ruta Provider SDK → runtime → MCP, activa el provider
explícitamente y ejecuta el smoke read-only:

```bash
set -a
. dev/lab/.env
set +a
export DOMOAI_HOME_ASSISTANT_PROVIDER=1
uv run pytest -q tests/integration/test_home_assistant_provider_smoke.py
```

Este test no ejecuta comandos ni escribe estados; comprueba health, discovery,
StateStore y `discover_devices`/`get_state` semánticos.

El directorio montado conserva el onboarding y los estados del laboratorio,
pero sus artefactos generados (`.storage`, base de datos, logs y metadatos) se
ignoran explícitamente en Git. Puedes cargar el archivo local ignorado
`dev/lab/.env` antes de ejecutar DomoAI:

```bash
set -a
. dev/lab/.env
set +a
uv run pytest -q tests/integration/test_home_assistant_smoke.py
```

No sincronices ni compartas ese archivo: contiene credenciales. Si el resto del
laboratorio ya está arrancado, puedes iniciar solo este perfil con
`--profile homeassistant up -d homeassistant`.

## Otros perfiles manuales

Matter Server + Matter.js virtual light:

```bash
docker compose -f dev/lab/compose.yaml --profile matter up -d --build matter-server
export DOMOAI_MATTER_SERVER_URL="ws://127.0.0.1:5580/ws"
```

El perfil arranca Matter Server y un nodo On/Off Matter.js sintético en la
misma red Docker. `--enable-test-net-dcl` es obligatorio para este nodo de
desarrollo y queda limitado al perfil virtual; no debe copiarse a producción.

La primera vez, abre `http://127.0.0.1:5580/` y commissiona el dispositivo con
el pairing code que muestran los logs de `matter-device`:

```bash
docker compose -f dev/lab/compose.yaml --profile matter logs matter-device \
  | grep "manual pairing code"
```

Después valida la ruta real DomoAI → Matter Server → nodo comisionado:

```bash
DOMOAI_MATTER_SERVER_URL="ws://127.0.0.1:5580/ws" \
  uv run pytest -q tests/integration/test_matter_server_smoke.py
```

El smoke es opt-in y requiere que el nodo esté comisionado. El compose crea
el nodo virtual, pero no lo commissiona automáticamente para evitar cambios
de estado ocultos.

KNX Virtual/ETS está documentado en [`knx-virtual.md`](knx-virtual.md). Requiere
Windows o una VM, una interfaz KNXnet/IP accesible y confirmación de las
direcciones del proyecto en `configs/knx-virtual.json`.

## Reset y diagnóstico

```bash
docker compose -f dev/lab/compose.yaml logs -f zigbee2mqtt modbus
docker compose -f dev/lab/compose.yaml down -v
```

Si Docker no está disponible, la suite determinista de DomoAI y
`tests/integration/test_virtual_lab_assets.py` siguen siendo ejecutables. Los
smoke tests live deben quedar omitidos hasta que el servicio correspondiente
esté realmente accesible.
