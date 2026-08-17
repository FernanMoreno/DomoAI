# KNX Virtual / ETS profile

This profile is intentionally outside the Docker stack. KNX Virtual and ETS
run on Windows or a Windows VM, while DomoAI may run in Linux/WSL.

## Network checklist

1. Create the virtual project in ETS and use the group addresses in
   `configs/knx-virtual.json`, or replace the JSON with the addresses actually
   created by ETS.
2. Ensure the Windows/VM NIC is reachable from the DomoAI host.
3. Configure KNX Virtual's KNXnet/IP interface for that NIC and the default
   tunnelling port `3671`.
4. Allow the required UDP/TCP traffic through the VM and host firewall.
5. Set `DOMOAI_KNX_GATEWAY_HOST` to the VM address and run the read-only smoke.

KNX Virtual's router mode may require a `router.txt` file containing
`<interface-ip>:3671`, according to the KNX Association support instructions.
Do not expose KNXnet/IP directly to the public internet.

## Run

```bash
export DOMOAI_KNX_GATEWAY_HOST="192.168.1.200"
export DOMOAI_KNX_CONFIG_PATH="dev/lab/configs/knx-virtual.json"
uv run pytest -q tests/integration/test_knx_smoke.py
```

The addresses in the JSON are a reference only until they are confirmed in
ETS/KNX Virtual. The smoke must remain skipped when the VM is unavailable.

## Current validation boundary (2026-08-17)

The local KNX contract and fixture checks pass (`23 passed, 1 skipped` when
including the live smoke). The live smoke is honestly skipped in this checkout:
`DOMOAI_KNX_*` is unset, no KNX/ETS process is present, and no local UDP/TCP
listener is bound to port `3671`. The `xknx` package being installed does not
constitute a gateway.

This closes the fixture-first/local acceptance described by Specs 006 and 019;
live `connect()`/`discover()` remains externally blocked until a KNX Virtual/ETS
gateway and its confirmed group-address mapping are supplied. Never infer the
gateway address or replace the reference mapping from fixture data.
