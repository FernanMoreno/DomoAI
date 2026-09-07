# Docker HA Qualification Lab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:subagent-driven-development` (recommended) or
> `superpowers:executing-plans` to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove the infrastructure portion of multi-host qualification against
a disposable Docker topology without creating production authority.

**Architecture:** A three-member etcd cluster provides both DomoAI
coordination and Patroni's DCS. Three Patroni PostgreSQL nodes expose a
synchronous primary through HAProxy. A local JSONL bridge simulates final-hop
epoch enforcement, and a lab runner verifies qualification before and after a
controlled primary interruption. Lab evidence is explicitly unable to qualify
the production runtime.

**Tech Stack:** Docker Compose, etcd v3, PostgreSQL 16, Patroni, HAProxy,
Python 3.12, pytest/Testcontainers.

**Spec:** `docs/superpowers/specs/2026-09-05-docker-ha-qualification-lab-design.md`

## Global Constraints

- Docker assets are `NOT FOR PRODUCTION`; no host ports by default.
- Production qualification retains HTTPS/mTLS and rejects lab evidence.
- The only permitted disruption is stopping a container labelled as lab.
- The lab must clean up project-scoped containers and volumes on completion.
- Active-active remains unsupported.

---

### Task 1: Lab provenance contract

**Files:**
- Modify: `src/domoai/domain/multihost_qualification.py`
- Modify: `src/domoai/application/runtime_factory.py`
- Modify: `scripts/export_schemas.py`
- Test: `tests/contract/test_multihost_qualification_contract.py`
- Test: `tests/composition/test_multihost_production_gate.py`

**Interfaces:**
- Produces `qualification_environment: Literal["production", "lab"]`.
- `MultiHostQualificationEvidence.qualifies(...)` returns false unless the
  evidence has production provenance.

- [X] Write a failing contract test that a passed `lab` evidence cannot qualify.
- [X] Run the contract test and observe the missing provenance behavior.
- [X] Add provenance with production default, include it in the digest/schema,
  and reject it in `qualifies` when it is `lab`.
- [X] Run contract and production-gate tests; regenerate schemas.

### Task 2: Disposable synchronous PostgreSQL HA topology

**Files:**
- Create: `deploy/multihost/qualification/patroni/Dockerfile`
- Create: `deploy/multihost/qualification/patroni/patroni.yml`
- Create: `deploy/multihost/qualification/haproxy.cfg`
- Modify: `deploy/multihost/qualification/compose.yaml`
- Test: `tests/contract/test_multihost_qualification_docs.py`

**Interfaces:**
- Consumes `docker compose -f deploy/multihost/qualification/compose.yaml`.
- Produces services `etcd-1..3`, `postgres-1..3`, and `postgres-writer`.

- [X] Add a failing topology contract test for three Patroni nodes, HAProxy and
  an explicit non-production marker.
- [X] Build a local Patroni image from PostgreSQL 16 and configure strict
  synchronous replication through the existing three-member etcd DCS.
- [X] Add HAProxy writer health checks targeting Patroni's leader endpoint.
- [X] Validate `docker compose config` and the topology contract test.

### Task 3: Lab fencing bridge and qualification runner

**Files:**
- Create: `deploy/multihost/qualification/gateway_fencing_lab.py`
- Create: `scripts/run_multihost_qualification_lab.sh`
- Test: `tests/integration/test_multihost_qualification_lab.py`

**Interfaces:**
- The bridge reads one `GatewayFencingProbeRequest` JSONL line and emits one
  matching `GatewayFencingProbeResult`; it accepts strictly increasing epochs
  per scope.
- The runner exits non-zero on unavailable quorum/HA/fencing and emits only
  `qualification_environment="lab"` evidence.

- [X] Write a failing integration test for a passed lab qualification against
  three distinct etcd members, an HAProxy writer and the bridge.
- [X] Implement the stateful lab bridge without shell evaluation or secrets.
- [X] Implement a project-scoped shell runner with health deadlines, compose
  cleanup trap, direct DomoAI runner invocation, and sanitized evidence path.
- [X] Run the integration test with Docker; it must skip only when Docker is
  unavailable.

### Task 4: Controlled failover exercise and operator documentation

**Files:**
- Modify: `scripts/run_multihost_qualification_lab.sh`
- Modify: `deploy/multihost/README.md`
- Modify: `docs/auditoria-fase-3-escalabilidad-identidad.md`
- Test: `tests/integration/test_multihost_qualification_lab.py`

**Interfaces:**
- The script identifies the current Patroni leader, stops only that service,
  waits for HAProxy to reach a different writable primary, then reruns
  qualification.

- [X] Write a failing integration assertion that primary identity changes and
  the post-failover evidence is passed but lab-scoped.
- [X] Add controlled failover with bounded polling and a non-zero failure exit.
- [X] Document invocation, cleanup, lab limitations and the remaining physical
  HIL requirement.
- [X] Run targeted tests, Docker compose validation, Ruff, mypy and docs checks.

### Task 5: Cross-system verification

**Files:**
- Modify: `specs/193-multihost-production-qualification/tasks.md`

- [X] Run the focused contract, integration and composition tests.
- [X] Run `project-composition-check "$(cat .ai/project-name)"` and full pytest.
- [X] Review final diff and record that physical HIL remains external.
