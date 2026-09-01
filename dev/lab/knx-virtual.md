# KNX Virtual / ETS profile

This profile is intentionally outside the Docker stack. KNX Virtual and ETS
run on Windows or a Windows VM, while DomoAI may run in Linux/WSL.

## External prerequisites

KNX Virtual is free, but its official download requires a MyKNX account. The
official installation guidance requires Windows with the latest ETS installed
on the same machine. Use only the [KNX Virtual download page](https://www.knx.org/professionals/knx-virtual)
and the [official installation guide](https://support.knx.org/hc/en-us/articles/4502160238354-Download-installation);
never add MyKNX credentials or downloaded binaries to this repository.

KNX Virtual/ETS is installed on Windows and the live lab uses `knxd` in WSL as
the single upstream client. Until those prerequisites are available, the live
smoke remains intentionally skipped.

## Network checklist

1. Create the virtual project in ETS and use the group addresses in
   `configs/knx-virtual.json`, or replace the JSON with the addresses actually
   created by ETS.
2. Ensure the Windows/VM NIC is reachable from the DomoAI host.
3. Configure KNX Virtual's KNXnet/IP interface for that NIC and the default
   tunnelling port `3671`.
4. Allow the required UDP/TCP traffic through the Windows and WSL firewalls.
5. Start the WSL `knxd` launcher, then set `DOMOAI_KNX_GATEWAY_HOST` to the
   current WSL address and run the opt-in live smoke.

KNX Virtual's router mode may require a `router.txt` file containing
`<interface-ip>:3671`, according to the KNX Association support instructions.
Do not expose KNXnet/IP directly to the public internet.

## Run

```bash
export DOMOAI_KNX_GATEWAY_HOST="$(ip route get 172.26.80.1 | awk '{for (i=1; i<=NF; i++) if ($i==\"src\") {print $(i+1); exit}}')"
export DOMOAI_KNX_GATEWAY_PORT="3672"
export DOMOAI_KNX_ROUTE_BACK="0"
export DOMOAI_KNX_CONFIG_PATH="dev/lab/configs/knx-virtual.json"
DOMOAI_LIVE_BATTERY_KNX_ENABLE=1 \
uv run pytest -q tests/integration/test_live_battery_lab_composition.py \
  -k knx_virtual_connection
```

The addresses in the JSON are a reference only until they are confirmed in
ETS/KNX Virtual. The smoke must remain skipped when the VM is unavailable.

For the virtual battery facade, after starting the single WSL gateway use the
supervised bridge path:

```bash
uv run domoai-lab up --services mqtt battery knx-bridge
uv run domoai-lab status --services mqtt battery knx-bridge
```

The bridge is ready only after it has received the complete retained battery
state and the supervisor has independently read every configured battery state
group. A stale or malformed status is never considered ready. Do not start the
Compose `knx-gateway` profile in parallel with WSL `knxd`.

## Fixture validation boundary

The local KNX contract and fixture checks remain deterministic and do not
replace live evidence. The live path is opt-in and requires KNX Virtual/ETS on
Windows, WSL `knxd`, the current WSL address in ETS and the confirmed group
mapping. Never infer the gateway address or replace the reference mapping from
fixture data.

## Live validation (2026-08-30)

The real composition was validated from WSL against KNX Virtual on Windows:

```text
ETS 4/0/0 = 1 kW
  -> WSL knxd :3672
  -> KNX Virtual :3671
  -> host bridge
  -> domoai/battery/power/set = 1
  -> battery state power_kw = 1.0, mode = charging
  -> KNX feedback 4/0/2 = 1.0, 4/0/1 = 5, 4/0/3 = 10
```

The stable path is WSL-native `knxd`, not the Compose `knx-gateway` profile.
The latter is retained only for experiments with a KNX/IP server that supports
the required NAT behavior. In this Windows/WSL setup the Docker path was
observed dropping KNXnet/IP tunnelling acknowledgements, while the native path
passed the complete live battery composition. WSL addresses can change after a
restart; always launch `dev/lab/knx-gateway/run-wsl.sh` so it discovers the
current address, then point ETS and DomoAI to that address on `3672/UDP`.

## Virtual battery facade

La batería no vive dentro de ETS: el modelo dinámico vive en el servicio
`battery` de Docker y KNX Virtual actúa como una fachada KNX real sobre ese
estado. El proyecto ETS debe enlazar estas direcciones:

| Señal | Grupo | DPT | Unidad |
| --- | --- | --- | --- |
| potencia feedback | `4/0/2` | `9.024` | kW, carga positiva |
| potencia command | `4/0/0` | `9.024` | kW, descarga negativa |
| SOC | `4/0/1` | `13.013` | kWh |
| capacidad | `4/0/3` | `13.013` | kWh |

El mapping DomoAI correspondiente es `configs/knx-battery-virtual.json`.
KNX Virtual sigue ejecutándose en Windows y conserva su interfaz upstream en
`172.26.80.1:3671`. `knxd` en WSL es el único cliente de esa interfaz y
publica `3672` para ETS y DomoAI. Configura ETS a la IP actual de WSL en
`3672/UDP`, conserva `1.0.255` como dirección individual y no conectes dos
clientes directamente a `3671`.

Como las direcciones de la batería no tienen un dispositivo KNX asociado en el
proyecto ETS actual, el bridge host-side responde también a sus
`GroupValueRead` con el último estado MQTT retenido. Así `discover()` y el
readback son deterministas, sin fingir que ETS ha cualificado un inversor.

```bash
dev/lab/knx-gateway/run-wsl.sh

DOMOAI_KNX_GATEWAY_HOST="$(ip route get 172.26.80.1 | awk '{for (i=1; i<=NF; i++) if ($i=="src") {print $(i+1); exit}}')" \
DOMOAI_KNX_GATEWAY_PORT=3672 \
DOMOAI_KNX_ROUTE_BACK=0 \
uv run domoai-lab up --services mqtt battery knx-bridge
```

El puerto `1503` de la batería es Modbus TCP y no sustituye al puerto KNX.
Esta fachada permite probar discovery, readback, comandos y fallos de
transporte, pero su resultado sigue siendo evidencia de laboratorio, no
`hil-qualified`.
