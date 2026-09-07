# Digital twin qualification

- Status: `passed`
- Scope: `digital_twin`
- Run: `twin-187`
- Seed: `187`
- Plant digest: `837c82b7d9071ba4782d2201918a07b1c60dd6a956a3b6ebb8e87fbab02ca98c`
- Trace digest: `be7598b1360c40ae2f4e39d0c2720af1ea41b11b9b1cecaa60a4e5972428d7ff`

## Coverage

| Surface | Items |
|---|---|
| Adapters | fixture, home_assistant, knx, matter, modbus, zigbee2mqtt |
| Domains | battery, climate, cover, environment, ev, light, power, solar, switch, water |
| Checks | audit, automation, discovery, faults, idempotency, identity, normalization, optimization, privacy, product, readback, recovery, routing, scheduler |

## Checks

| Check | Status | Code | Details |
|---|---|---|---|
| `audit` | `passed` | `` | `{"event_types":["automation_fired","automation_rule_created","command_execution_outcome","discovery_completed","plan_approved","plan_execution_completed","plan_execution_started","plan_validated"],"events":158,"missing":[]}` |
| `automation` | `passed` | `` | `{"evaluations":1}` |
| `discovery` | `passed` | `` | `{"devices":15}` |
| `faults` | `passed` | `` | `{"event_ordering":true,"rejected":{"partial_failure":true,"rejected":true},"stale_states":2,"unavailable_states":2}` |
| `idempotency` | `passed` | `` | `{"writes_added":1}` |
| `identity` | `passed` | `` | `{"missing":[]}` |
| `normalization` | `passed` | `` | `{"adapters":["fixture","home_assistant","knx","matter","modbus","zigbee2mqtt"],"devices":15}` |
| `optimization` | `passed` | `` | `{"status":"optimal"}` |
| `privacy` | `passed` | `` | `{"records":1}` |
| `product` | `passed` | `` | `{"next_step":"validate_and_request_approval_before_execution"}` |
| `readback` | `passed` | `` | `{"readbacks":34,"routes":34}` |
| `recovery` | `passed` | `` | `{"snapshots":31}` |
| `routing` | `passed` | `` | `{"routes_exercised":34}` |
| `scheduler` | `passed` | `` | `{"result":[{"outcome":"executed","plan_id":"twin-scheduler-187"}]}` |

## Invariant violations

None.

This report proves the software closed loop on a deterministic virtual plant. It is not physical commissioning or HIL evidence.
