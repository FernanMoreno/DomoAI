# Explicit local Home Assistant lab bootstrap

## Goal

Make the opt-in `lab` bootstrap behave identically when Home Assistant is
discovered automatically or explicitly configured as the allowlisted local
endpoint. In both cases, an authenticated reachable local HA instance may
select only the repository-owned lab mapping and operational binding assets
when those paths are omitted.

## Safety boundary

- Explicit paths always win.
- Remote, non-allowlisted, unauthenticated, or unreachable HA never selects
  lab actuator assets.
- The selected binding remains `software-qualified`; this does not create HIL
  evidence or enable production battery dispatch.
- `dev/lab/**` and local credentials remain untouched.

## TDD sequence

1. Add a failing bootstrap regression for explicit local HA URL + token with
   omitted mapping/battery/EV paths.
2. Implement the smallest allowlisted endpoint helper and reuse the existing
   lab asset selection path.
3. Run focused unit/bootstrap/composition tests, then repository gates.
4. Update Spec 167 convergence status and durable Obsidian context with the
   root cause, evidence and remaining physical qualification boundary.

## Stop condition

Stop after explicit and automatic lab bootstrap paths produce the same
secret-free manifest/settings result, non-lab paths remain unchanged, and all
focused and repository verification gates pass. Do not alter physical
qualification semantics or lab assets.

## Verification record (2026-09-02)

- The new explicit-local-HA regression and the existing bootstrap/composition
  checks pass: `19 passed, 1 skipped` in the focused set.
- A real runtime built from the lab environment with mapping/profile/EV paths
  omitted selected the repository-owned lab assets, reported
  `battery_qualification=software-qualified`, and completed startup control
  reconciliation. No secret was printed or persisted in the manifest.
- The full repository composition gate passes: `1553 passed, 18 skipped, 1
  warning`. Live checks remain opt-in because HIL hardware is not present.
