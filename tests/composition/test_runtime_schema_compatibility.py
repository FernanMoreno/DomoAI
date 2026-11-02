import json
from datetime import UTC, datetime

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.runtime_factory import build_runtime
from domoai.application.state_service import StateService
from domoai.config.settings import Settings
from domoai.domain.models import AuthorityContext, Command, Plan
from domoai.mcp.domotics_server import DomoticsMcpContext, create_domotics_server
from domoai.optimizer.ports import OptimizationResult
from tests.contract.test_domotics_mcp_contract import structured
from tests.integration.test_runtime_schema_compatibility import historical_payload

pytestmark = pytest.mark.composition


def runtime_settings(tmp_path, **overrides):
    return Settings(
        database_path=tmp_path / "runtime.sqlite",
        connection_master_key_path=tmp_path / "key",
        bootstrap_manifest_path=tmp_path / "bootstrap.json",
        commissioning_manifest_path=tmp_path / "commissioning.json",
        modbus_profile_catalog_path=tmp_path / "modbus",
        mcp_deployment_id="deployment",
        **overrides,
    )


def mcp_context(runtime):
    return DomoticsMcpContext(
        discovery=runtime.discovery,
        state_service=StateService(runtime.state_store),
        facade=runtime.facade,
        registry=runtime.registry,
        policies=[],
        plan_repository=runtime.plan_repository,
        plans=runtime.plans,
        scheduler=runtime.scheduler,
        local_automation=runtime.local_automation,
        approval_store=runtime.approval_store,
    )


def light_plan(runtime, identity="proposal"):
    device = next(item for item in runtime.registry.devices if item.type.value == "light")
    return Plan(
        id=identity,
        commands=[
            {
                "id": identity + "-command",
                "device_id": device.id,
                "command": "turn_on",
                "idempotency_key": identity + "-intent",
            }
        ],
    )


@pytest.mark.asyncio
async def test_optimization_service_reselects_nested_and_cleared_result_versions(tmp_path) -> None:
    from domoai.application.optimization_service import OptimizationService
    from domoai.optimizer.cp_sat import CpSatOptimizer

    runtime = await build_runtime(runtime_settings(tmp_path), adapter=SimulatedHomeAdapter())
    try:
        service = OptimizationService(
            runtime.registry, runtime.plan_service, CpSatOptimizer(runtime.registry)
        )
        result = OptimizationResult(
            scenario_id="scenario", status="feasible", solver="fixture", plan=light_plan(runtime)
        )
        validated = service.validate_proposal(result)
        assert validated.schema_version == "v2"
        assert validated.plan.schema_version == "v2"
        OptimizationResult.model_validate(validated.model_dump(mode="json"))
        battery = validated.model_copy(
            update={"constraint_summary": {"slots": [{"battery_charge_kw": 1}]}}
        )
        cleared = service.validate_proposal(battery)
        assert cleared.schema_version == "v1"
        assert cleared.plan is None and not cleared.plans
        OptimizationResult.model_validate(cleared.model_dump(mode="json"))
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_real_mcp_invalidates_historical_cache_and_lists_scoped_diagnostics(tmp_path) -> None:
    runtime = await build_runtime(runtime_settings(tmp_path), adapter=SimulatedHomeAdapter())
    try:
        context = mcp_context(runtime)
        context.plans["historical"] = light_plan(runtime, "historical")
        raw = historical_payload()

        def seed():
            runtime.database.connection.execute(
                "INSERT INTO plans(id,payload,status,updated_at) VALUES(?,?,?,?)",
                ("historical", raw, "ready", datetime.now(UTC).isoformat()),
            )
            runtime.database.connection.execute(
                "INSERT INTO scheduled_plans VALUES(?,?,?,?,?)",
                (
                    "historical",
                    datetime.now(UTC).isoformat(),
                    "pending",
                    raw,
                    datetime.now(UTC).isoformat(),
                ),
            )
            runtime.database.connection.commit()

        await runtime.storage.run(seed)
        server = create_domotics_server(context)
        execution = structured(
            await server.call_tool(
                "execute_plan", {"plan_id": "historical", "validation_digest": "old-digest"}
            )
        )
        missing = structured(
            await server.call_tool(
                "execute_plan", {"plan_id": "absent", "validation_digest": "old-digest"}
            )
        )
        assert execution == missing
        assert "historical" not in context.plans
        listed = structured(await server.call_tool("list_scheduled_plans", {}))
        assert listed["compatibility"]["records"] == [
            {
                "record_kind": "scheduled_plan",
                "record_id": "historical",
                "reason": "revalidation_required",
            }
        ]
        assert "payload" not in json.dumps(listed)
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_real_mcp_rejects_foreign_tenant_with_same_household(tmp_path) -> None:
    runtime = await build_runtime(runtime_settings(tmp_path), adapter=SimulatedHomeAdapter())
    try:
        server = create_domotics_server(mcp_context(runtime))
        proposal = light_plan(runtime).model_copy(
            update={"authority": AuthorityContext(tenant_id="foreign", household_id="default")}
        )
        response = structured(
            await server.call_tool("prepare_plan", {"plan": proposal.model_dump(mode="json")})
        )
        assert response["error"]["code"] == "insufficient_scope"
        legacy = structured(
            await server.call_tool(
                "prepare_plan", {"plan": light_plan(runtime).model_dump(mode="json")}
            )
        )
        assert "error" not in legacy
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_protected_legacy_rule_cannot_create_new_versioned_occurrence(tmp_path) -> None:
    from datetime import timedelta

    from domoai.domain.automation import AutomationConsent, AutomationEvent, AutomationRule

    runtime = await build_runtime(runtime_settings(tmp_path), adapter=SimulatedHomeAdapter())
    try:
        now = datetime.now(UTC)
        rule = AutomationRule(
            id="legacy-rule",
            name="Legacy",
            scope="home",
            expires_at=now + timedelta(hours=1),
            status="enabled",
            plan_template=light_plan(runtime),
            trigger={
                "type": "time",
                "time_of_day": now.time().replace(tzinfo=None),
                "timezone": "UTC",
            },
        )
        consent = AutomationConsent(
            approval_id="legacy-consent",
            principal_id="operator",
            scope="home",
            rule_digest=rule.definition_digest,
            approved_at=now,
            expires_at=now + timedelta(hours=1),
        )
        # Authentic pre-upgrade standing consent is seeded without invoking new admission.
        await runtime.automation_rule_repository.save(rule, consent)
        result = await runtime.local_automation.handle_event(
            AutomationEvent(
                event_id="tick", event_type="time", occurred_at=now, target_rule_id=rule.id
            )
        )
        assert result[0].status == "blocked"
        assert result[0].reason == "revalidation_required"
        assert runtime.adapter.calls == []
        stored = await runtime.automation_rule_repository.get(rule.id)
        assert stored.rule.definition_digest == rule.definition_digest
        assert stored.consent == consent
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["direct", "schedule", "bundle"])
async def test_current_definition_survives_claim_readback_and_restart(tmp_path, route) -> None:
    from datetime import timedelta

    from domoai.application.bundle_commit import (
        BundleCommitRequest,
        BundleCommitRequestMember,
        bundle_approval_digest,
    )
    from domoai.application.coordination import DeterministicLeaseCoordinator
    from domoai.domain.errors import DomainError
    from domoai.runtime.approval_store import ApprovalAssertion, OperatorPrincipal

    class FencedAdapter(SimulatedHomeAdapter):
        supports_fencing = True

    coordinator = DeterministicLeaseCoordinator()
    adapter = FencedAdapter()
    settings = runtime_settings(tmp_path, multi_host_enabled=True)
    runtime = await build_runtime(settings, adapter=adapter, lease_coordinator=coordinator)
    try:
        now = datetime.now(UTC)
        if route == "direct":
            device = next(item for item in runtime.registry.devices if item.type.value == "cover")
            draft = runtime.plan_service.create_plan(
                "golden",
                [
                    Command(
                        id="command",
                        device_id=device.id,
                        command="open",
                        risk_class="confirm",
                        idempotency_key="golden-intent",
                    )
                ],
            )
        else:
            draft = light_plan(runtime, "golden")
        if route == "schedule":
            draft = draft.model_copy(update={"execute_at": now})
        validated = runtime.plan_service.validate(draft)
        if route == "direct":
            principal = OperatorPrincipal("operator", "oidc:mfa", "session")
            grant = runtime.approval_store.issue_authenticated(
                validated,
                principal=principal,
                assertion=ApprovalAssertion(
                    principal=principal,
                    plan_id=validated.id,
                    validation_digest=validated.validation.digest,
                    nonce="golden-consent",
                    approved_at=now,
                    expires_at=now + timedelta(minutes=2),
                ),
            )
            grant = runtime.approval_store.consume(grant.approval_id, validated)
            validated = runtime.plan_service.approve(validated, grant=grant)
        await runtime.plan_repository.save(validated)
        definition = validated.definition_digest
        validation = validated.validation.digest
        approval = validated.approval.model_dump(mode="json") if validated.approval else None
        if route == "schedule":
            await runtime.scheduler.schedule(validated)
            await runtime.scheduler.run_due(now=datetime.now(UTC))
        elif route == "bundle":
            members = [
                BundleCommitRequestMember(plan_id=validated.id, validation_digest=validation)
            ]
            await runtime.bundle_commit_service.commit(
                BundleCommitRequest(
                    scenario_id="golden",
                    bundle_digest=bundle_approval_digest("golden", members),
                    members=members,
                )
            )
        else:
            await runtime.facade.execute_plan(validated)
        stored = await runtime.plan_repository.get(validated.id)
        assert stored.schema_version == "v2"
        assert stored.definition_digest == definition
        assert stored.validation.digest == validation
        assert (stored.approval.model_dump(mode="json") if stored.approval else None) == approval
        assert stored.execution_lease is not None
        assert stored.status.value == "completed"
        assert len(adapter.calls) == 1
        intent_key = stored.commands[0].idempotency_key
        intent = await runtime.physical_intent_repository.get(
            household_id="default", idempotency_key=intent_key
        )
        assert intent.status.value == "confirmed"
    finally:
        await runtime.close()
    restarted_adapter = FencedAdapter()
    restarted = await build_runtime(
        settings, adapter=restarted_adapter, lease_coordinator=coordinator
    )
    try:
        stored = await restarted.plan_repository.get("golden")
        assert stored.definition_digest == definition and stored.validation.digest == validation
        with pytest.raises(DomainError):
            await restarted.facade.execute_plan(stored)
        latest = await restarted.physical_intent_repository.get(
            household_id="default", idempotency_key=intent_key
        )
        assert latest == intent
        assert restarted_adapter.calls == []
    finally:
        await restarted.close()


def old_reader_process(kind, target):
    import subprocess
    import sys

    from tests.fixtures.runtime_schema_compatibility import FIXTURES

    script = """
import asyncio, sys, types
from pathlib import Path
path = Path(sys.argv[1]); kind = sys.argv[2]; target = sys.argv[3]
module = types.ModuleType("schema253_pre_floor"); module.__file__ = str(path)
sys.modules[module.__name__] = module
exec(compile(path.read_text(), str(path), "exec"), module.__dict__)
async def run():
    database = (module.SQLiteDatabase(Path(target)) if kind == "sqlite"
                else module.PostgresDatabase(target))
    try:
        await database.open_existing()
        row = database.connection.execute(
            "SELECT minimum_reader FROM runtime_contract_floor"
        ).fetchone()
        print(row[0])
    finally:
        await database.close()
asyncio.run(run())
"""
    return subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(FIXTURES / f"pre_floor_{kind}.py.txt"),
            kind,
            str(target),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.asyncio
async def test_actual_old_sqlite_process_ignores_marker_and_os_access_excludes_it(tmp_path) -> None:
    import sqlite3

    from domoai.persistence.sqlite import SQLiteDatabase

    path = tmp_path / "excluded.sqlite"
    database = SQLiteDatabase(path)
    await database.initialize()
    database.connection.execute("UPDATE runtime_contract_floor SET minimum_reader=999")
    database.connection.commit()
    await database.close()
    old = old_reader_process("sqlite", path)
    assert old.returncode == 0 and old.stdout.strip() == "999"
    with pytest.raises(RuntimeError, match="runtime contract floor"):
        await database.open_existing()
    # Disposable OS access test. Production requires separate service identities
    # and restart exclusion; a marker or this test does not establish that gate.
    original_mode = path.stat().st_mode & 0o777
    path.chmod(0)
    try:
        excluded = old_reader_process("sqlite", path)
        assert excluded.returncode != 0
        assert "unable to open database" in excluded.stderr
    finally:
        path.chmod(original_mode)
    with sqlite3.connect(path) as raw:
        assert raw.execute("SELECT minimum_reader FROM runtime_contract_floor").fetchone()[0] == 999


@pytest.mark.asyncio
async def test_actual_old_postgres_process_ignores_marker_and_revoked_login_excludes_it() -> None:
    import psycopg
    from psycopg.conninfo import make_conninfo
    from testcontainers.community.postgres import PostgresContainer

    from domoai.persistence.postgres import PostgresDatabase

    with PostgresContainer(
        "postgres:16-alpine",
        username="domoai",
        password="domoai-test",
        dbname="domoai",
        driver=None,
    ) as container:
        dsn = container.get_connection_url(driver=None)
        database = PostgresDatabase(dsn)
        await database.initialize()
        await database.close()
        with psycopg.connect(dsn) as raw:
            raw.execute("INSERT INTO schema_migrations VALUES('postgres-schema-002','historical')")
            raw.execute("UPDATE runtime_contract_floor SET minimum_reader=999")
            raw.execute("CREATE ROLE old_reader LOGIN PASSWORD 'disposable-old-reader'")
            raw.execute("GRANT CONNECT ON DATABASE domoai TO old_reader")
            raw.execute("GRANT USAGE ON SCHEMA public TO old_reader")
            raw.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO old_reader")
        old_dsn = make_conninfo(dsn, user="old_reader", password="disposable-old-reader")
        old = old_reader_process("postgres", old_dsn)
        assert old.returncode == 0 and old.stdout.strip() == "999"
        with psycopg.connect(dsn) as raw:
            raw.execute("ALTER ROLE old_reader NOLOGIN")
        excluded = old_reader_process("postgres", old_dsn)
        assert excluded.returncode != 0
        assert "not permitted to log in" in excluded.stderr
        with psycopg.connect(dsn) as raw:
            assert (
                raw.execute("SELECT minimum_reader FROM runtime_contract_floor").fetchone()[0]
                == 999
            )


@pytest.mark.asyncio
async def test_real_mcp_rule_consent_is_bound_to_final_template_version(tmp_path) -> None:
    from datetime import timedelta

    from domoai.domain.automation import AutomationEvent, AutomationRule
    from domoai.runtime.approval_store import ApprovalAssertion, OperatorPrincipal

    runtime = await build_runtime(runtime_settings(tmp_path), adapter=SimulatedHomeAdapter())
    try:
        now = datetime.now(UTC)
        template = runtime.plan_service.bind_definition(light_plan(runtime, "rule-template"))
        rule = AutomationRule(
            id="current-rule",
            name="Current",
            scope="home",
            plan_template=template,
            expires_at=now + timedelta(hours=1),
            trigger={
                "type": "time",
                "time_of_day": now.time().replace(tzinfo=None),
                "timezone": "UTC",
            },
        )
        validated = runtime.plan_service.validate(template)
        principal = OperatorPrincipal("operator", "oidc:mfa", "session")
        grant = runtime.approval_store.issue_authenticated(
            validated,
            principal=principal,
            recurrence_digest=rule.definition_digest,
            assertion=ApprovalAssertion(
                principal=principal,
                plan_id=validated.id,
                validation_digest=validated.validation.digest,
                recurrence_digest=rule.definition_digest,
                nonce="standing-consent",
                approved_at=now,
                expires_at=now + timedelta(minutes=2),
            ),
        )
        server = create_domotics_server(mcp_context(runtime))
        response = structured(
            await server.call_tool(
                "create_local_automation_rule",
                {"rule": rule.model_dump(mode="json"), "approval_id": grant.approval_id},
            )
        )
        assert "error" not in response, response
        record = await runtime.automation_rule_repository.get(rule.id)
        assert record.rule.schema_version == "v2"
        assert record.rule.definition_digest == record.consent.rule_digest == rule.definition_digest
        outcome = await runtime.local_automation.handle_event(
            AutomationEvent(
                event_id="tick", event_type="time", occurred_at=now, target_rule_id=rule.id
            )
        )
        assert outcome[0].status == "executed"
        occurrence = await runtime.plan_repository.get(outcome[0].plan_id)
        assert occurrence.schema_version == "v2"
        assert len(runtime.adapter.calls) == 1
    finally:
        await runtime.close()


def retained_old_reader(kind, target):
    import subprocess
    import sys

    from tests.fixtures.runtime_schema_compatibility import FIXTURES

    script = """
import asyncio, os, sys, types
from pathlib import Path
path = Path(sys.argv[1]); kind = sys.argv[2]; target = sys.argv[3]
module = types.ModuleType("schema253_retained_old"); module.__file__ = str(path)
sys.modules[module.__name__] = module
exec(compile(path.read_text(), str(path), "exec"), module.__dict__)
async def run():
    database = (module.SQLiteDatabase(Path(target)) if kind == "sqlite"
                else module.PostgresDatabase(target))
    await database.open_existing()
    backend = (database.connection.execute("SELECT pg_backend_pid()").fetchone()[0]
               if kind == "postgres" else os.getpid())
    print(backend, flush=True)
    try:
        for command in sys.stdin:
            row = database.connection.execute(
                "SELECT minimum_reader FROM runtime_contract_floor").fetchone()
            print(row[0], flush=True)
    finally:
        await database.close()
asyncio.run(run())
"""
    return subprocess.Popen(
        [
            sys.executable,
            "-u",
            "-c",
            script,
            str(FIXTURES / f"pre_floor_{kind}.py.txt"),
            kind,
            str(target),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def old_process_line(process, *, read=False):
    import select

    if read:
        process.stdin.write("read\n")
        process.stdin.flush()
    readable, _, _ = select.select([process.stdout], [], [], 30)
    assert readable, "old process did not respond within its bounded deadline"
    return process.stdout.readline().strip()


@pytest.mark.asyncio
async def test_retained_sqlite_descriptor_requires_old_process_termination(tmp_path) -> None:
    from domoai.persistence.sqlite import SQLiteDatabase

    path = tmp_path / "retained.sqlite"
    supported = SQLiteDatabase(path)
    await supported.initialize()
    process = retained_old_reader("sqlite", path)
    mode = path.stat().st_mode & 0o777
    try:
        assert int(old_process_line(process)) == process.pid
        # Pre-floor binaries can still read the P04 floor without enforcing it.
        assert old_process_line(process, read=True) == "4"
        path.chmod(0)
        # Permission changes cannot revoke an already-open SQLite descriptor.
        assert old_process_line(process, read=True) == "4"
        process.terminate()
        process.wait(timeout=10)
        assert process.poll() is not None
        restarted = old_reader_process("sqlite", path)
        assert restarted.returncode != 0 and "unable to open database" in restarted.stderr
        assert (
            supported.connection.execute(
                "SELECT minimum_reader FROM runtime_contract_floor"
            ).fetchone()[0]
            == 4
        )
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        path.chmod(mode)
        await supported.close()


@pytest.mark.asyncio
async def test_retained_postgres_session_requires_backend_termination_after_nologin() -> None:
    import psycopg
    from psycopg.conninfo import make_conninfo
    from testcontainers.community.postgres import PostgresContainer

    from domoai.persistence.postgres import PostgresDatabase

    with PostgresContainer(
        "postgres:16-alpine",
        username="domoai",
        password="domoai-test",
        dbname="domoai",
        driver=None,
    ) as container:
        dsn = container.get_connection_url(driver=None)
        supported = PostgresDatabase(dsn)
        await supported.initialize()
        with psycopg.connect(dsn) as raw:
            raw.execute("INSERT INTO schema_migrations VALUES('postgres-schema-002','historical')")
            raw.execute("CREATE ROLE retained_old LOGIN PASSWORD 'disposable-old-reader'")
            raw.execute("GRANT CONNECT ON DATABASE domoai TO retained_old")
            raw.execute("GRANT USAGE ON SCHEMA public TO retained_old")
            raw.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO retained_old")
        old_dsn = make_conninfo(dsn, user="retained_old", password="disposable-old-reader")
        process = retained_old_reader("postgres", old_dsn)
        try:
            backend_pid = int(old_process_line(process))
            # Pre-floor binaries can still read the P04 floor without enforcing it.
            assert old_process_line(process, read=True) == "4"
            with psycopg.connect(dsn) as raw:
                raw.execute("ALTER ROLE retained_old NOLOGIN")
            # NOLOGIN protects only future sessions; revoke the retained session too.
            assert old_process_line(process, read=True) == "4"
            with psycopg.connect(dsn) as raw:
                assert raw.execute("SELECT pg_terminate_backend(%s)", (backend_pid,)).fetchone()[0]
            assert old_process_line(process, read=True) == ""
            process.wait(timeout=10)
            assert process.returncode != 0
            restarted = old_reader_process("postgres", old_dsn)
            assert restarted.returncode != 0 and "not permitted to log in" in restarted.stderr
            assert (
                supported.connection.execute(
                    "SELECT minimum_reader FROM runtime_contract_floor"
                ).fetchone()[0]
                == 4
            )
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            await supported.close()


@pytest.mark.asyncio
async def test_real_cp_sat_process_to_mcp_selects_current_nested_schema(tmp_path) -> None:
    from domoai.application.optimization_service import OptimizationService
    from domoai.application.process_optimization_worker import ProcessOptimizationWorker
    from domoai.mcp.ortools_server import OrtoolsMcpContext, create_ortools_server
    from domoai.optimizer.cp_sat import CpSatOptimizer
    from tests.contract.test_ortools_mcp_contract import scenario_for

    adapter = SimulatedHomeAdapter()
    runtime = await build_runtime(runtime_settings(tmp_path), adapter=adapter)
    worker = ProcessOptimizationWorker(runtime.registry)
    try:
        service = OptimizationService(
            runtime.registry, runtime.plan_service, CpSatOptimizer(runtime.registry)
        )
        server = create_ortools_server(
            OrtoolsMcpContext(
                registry=runtime.registry,
                plan_service=runtime.plan_service,
                optimization_service=service,
                optimization_worker=worker,
            )
        )
        device = next(item for item in runtime.registry.devices if item.type.value == "light")
        result = structured(
            await server.call_tool(
                "optimize_scenario",
                {
                    "scenario": scenario_for(device.id).model_dump(mode="json"),
                    "validate_proposal": True,
                },
            )
        )
        parsed = OptimizationResult.model_validate(result)
        assert parsed.solver == "cp-sat"
        assert parsed.schema_version == "v2"
        assert parsed.plan is not None
        for proposal in [parsed.plan, *parsed.plans]:
            assert proposal.schema_version == "v2"
            assert proposal.deployment_id == "deployment"
            assert proposal.execution_lease is None
        assert not adapter.calls
    finally:
        worker.close()
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("restriction", ["device_ids", "area_ids", "capabilities"])
async def test_partial_authority_list_diagnostics_are_constant_without_household_query(
    tmp_path, monkeypatch, restriction
):
    from datetime import timedelta
    from unittest.mock import AsyncMock

    from mcp.server.auth.provider import AccessToken

    from domoai.domain.automation import AutomationConsent, AutomationRule
    from domoai.domain.models import PrincipalRole

    adapter = SimulatedHomeAdapter()
    runtime = await build_runtime(runtime_settings(tmp_path), adapter=adapter)
    try:
        allowed = next(d for d in runtime.registry.devices if d.type.value == "light")
        forbidden = next(
            d
            for d in runtime.registry.devices
            if d.type.value == "switch" and d.area_id != allowed.area_id
        )
        values = {
            "device_ids": [allowed.id],
            "area_ids": [allowed.area_id],
            "capabilities": ["turn_on"],
        }
        authority = AuthorityContext(
            principal_id="limited",
            roles=[PrincipalRole.VIEWER],
            **{restriction: values[restriction]},
        )
        token = AccessToken(
            token="fixture",
            client_id="limited",
            scopes=["read"],
            claims={"authority": authority.model_dump(mode="json")},
        )
        now = datetime.now(UTC)
        templates = {}
        for identity, device, command in (
            ("compatible-allowed", allowed, "turn_on"),
            ("compatible-forbidden", forbidden, "turn_off"),
        ):
            templates[identity] = runtime.plan_service.create_plan(
                identity,
                [
                    Command(
                        id=identity, device_id=device.id, command=command, idempotency_key=identity
                    )
                ],
            )
        hidden = templates["compatible-forbidden"].model_dump(mode="json")
        hidden["schema_version"] = "v1"

        def seed(count):
            connection = runtime.database.connection
            connection.execute("DELETE FROM scheduled_plans")
            connection.execute("DELETE FROM automation_rules")
            for identity, template in templates.items():
                connection.execute(
                    "INSERT INTO scheduled_plans VALUES(?,?,?,?,?)",
                    (
                        identity,
                        now.isoformat(),
                        "pending",
                        template.model_dump_json(),
                        now.isoformat(),
                    ),
                )
                rule = AutomationRule(
                    id=identity,
                    name=identity,
                    trigger={
                        "type": "state_changed",
                        "device_id": template.commands[0].device_id,
                        "capability": "on",
                    },
                    plan_template=template,
                    scope="scope",
                    expires_at=now + timedelta(days=1),
                )
                consent = AutomationConsent(
                    approval_id="grant",
                    principal_id="owner",
                    scope="scope",
                    rule_digest=rule.definition_digest,
                    approved_at=now,
                    expires_at=now + timedelta(days=1),
                )
                connection.execute(
                    "INSERT INTO automation_rules VALUES(?,?,?,?,?,?,?)",
                    (
                        identity,
                        rule.model_dump_json(),
                        consent.model_dump_json(),
                        "enabled",
                        None,
                        None,
                        now.isoformat(),
                    ),
                )
            raw_rule = rule.model_dump(mode="json")
            raw_rule["schema_version"] = "v1"
            raw_rule["plan_template"]["schema_version"] = "v1"
            for index in range(count):
                identity = f"history-{index:03}"
                connection.execute(
                    "INSERT INTO scheduled_plans VALUES(?,?,?,?,?)",
                    (
                        identity,
                        now.isoformat(),
                        "pending",
                        json.dumps(hidden | {"id": identity}),
                        now.isoformat(),
                    ),
                )
                connection.execute(
                    "INSERT INTO automation_rules VALUES(?,?,?,?,?,?,?)",
                    (
                        identity,
                        json.dumps(raw_rule | {"id": identity}),
                        consent.model_dump_json(),
                        "enabled",
                        None,
                        None,
                        now.isoformat(),
                    ),
                )
            connection.commit()

        context = mcp_context(runtime)
        repositories = [runtime.scheduler.repository, runtime.local_automation.repository]
        spies = []
        for repository in repositories:
            spy = AsyncMock(wraps=repository.compatibility_diagnostics)
            monkeypatch.setattr(repository, "compatibility_diagnostics", spy)
            spies.append(spy)
        server = create_domotics_server(context)
        tools = ["list_scheduled_plans", "list_local_automation_rules"]
        unavailable = {
            "status": "unavailable",
            "reason": "whole_household_authority_required",
            "records": [],
            "next_offset": None,
        }
        observed = []
        for count in (0, 1, 101):
            await runtime.storage.run(lambda count=count: seed(count))
            with monkeypatch.context() as patch:
                patch.setattr(
                    "mcp.server.auth.middleware.auth_context.get_access_token",
                    lambda token=token: token,
                )
                responses = [
                    structured(await server.call_tool(tool, {"compatibility_offset": 73}))
                    for tool in tools
                ]
            assert all(response["compatibility"] == unavailable for response in responses)
            assert [p["plan_id"] for p in responses[0]["plans"]] == ["compatible-allowed"]
            assert [r["rule_id"] for r in responses[1]["rules"]] == ["compatible-allowed"]
            observed.append(responses)
        assert observed[0] == observed[1] == observed[2]
        for spy in spies:
            spy.assert_not_awaited()
        full = AccessToken(
            token="fixture",
            client_id="full",
            scopes=["read"],
            claims={
                "authority": AuthorityContext(
                    principal_id="full", roles=[PrincipalRole.VIEWER]
                ).model_dump(mode="json")
            },
        )
        with monkeypatch.context() as patch:
            patch.setattr("mcp.server.auth.middleware.auth_context.get_access_token", lambda: full)
            for tool in tools:
                ids = []
                offset = 0
                while offset is not None:
                    result = structured(
                        await server.call_tool(tool, {"compatibility_offset": offset})
                    )
                    page = result["compatibility"]
                    ids.extend(record["record_id"] for record in page["records"])
                    offset = page["next_offset"]
                assert ids == [f"history-{i:03}" for i in range(101)]
        assert not adapter.calls
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool", ["execute_plan", "request_approval", "schedule_plan", "commit_or_schedule_bundle"]
)
async def test_active_historical_lookup_matches_absence_after_authority_revocation(
    tmp_path, monkeypatch, tool
):
    from mcp.server.auth.provider import AccessToken

    from domoai.application.bundle_commit import BundleCommitRequestMember, bundle_approval_digest
    from domoai.domain.models import PrincipalRole

    adapter = SimulatedHomeAdapter()
    runtime = await build_runtime(runtime_settings(tmp_path), adapter=adapter)
    try:
        context = mcp_context(runtime)
        context.bundle_commit_service = runtime.bundle_commit_service
        template = runtime.plan_service.bind_definition(light_plan(runtime)).model_dump(mode="json")
        originals = {}

        def seed():
            for identity, status, tenant in (
                ("foreign-pending", "ready", "foreign"),
                ("foreign-terminal", "completed", "foreign"),
                ("local-pending", "ready", "default"),
            ):
                payload = json.loads(json.dumps(template))
                payload.update(id=identity, status=status, schema_version="v1")
                payload["authority"]["tenant_id"] = tenant
                raw = json.dumps(payload)
                originals[identity] = raw
                runtime.database.connection.execute(
                    "INSERT INTO plans(id,payload,status,updated_at) VALUES(?,?,?,?)",
                    (identity, raw, status, datetime.now(UTC).isoformat()),
                )
            runtime.database.connection.commit()

        await runtime.storage.run(seed)
        server = create_domotics_server(context)
        responses = []
        for restriction in ({}, {"device_ids": ["revoked-device"]}):
            authority = AuthorityContext(
                principal_id="operator", roles=[PrincipalRole.OPERATOR], **restriction
            )
            token = AccessToken(
                token="fixture",
                client_id="operator",
                scopes=["mutate"],
                claims={"authority": authority.model_dump(mode="json")},
            )
            with monkeypatch.context() as patch:
                patch.setattr(
                    "mcp.server.auth.middleware.auth_context.get_access_token",
                    lambda token=token: token,
                )
                for identity in [*originals, "absent"]:
                    if identity in originals:
                        context.plans[identity] = runtime.plan_service.bind_definition(
                            light_plan(runtime, identity)
                        )
                    args = {"plan_id": identity, "validation_digest": "historical-digest"}
                    if tool == "schedule_plan":
                        args["execute_at"] = datetime.now(UTC).isoformat()
                    elif tool == "commit_or_schedule_bundle":
                        member = BundleCommitRequestMember(
                            plan_id=identity, validation_digest="historical-digest"
                        )
                        args = {
                            "scenario_id": "scenario",
                            "members": [member.model_dump(mode="json")],
                            "bundle_digest": bundle_approval_digest("scenario", [member]),
                        }
                    responses.append(structured(await server.call_tool(tool, args)))
                    assert identity not in context.plans
        assert all(response == responses[-1] for response in responses)
        assert responses[-1]["error"]["code"] == "validation_error"
        assert responses[-1]["error"]["details"] == {}
        from unittest.mock import AsyncMock

        from mcp.server.fastmcp.exceptions import ToolError

        with monkeypatch.context() as patch:
            patch.setattr(
                context.plan_repository,
                "get",
                AsyncMock(side_effect=OSError("injected storage outage")),
            )
            with pytest.raises(ToolError, match="injected storage outage"):
                await server.call_tool(tool, args)
        stored = await runtime.storage.run(
            lambda: runtime.database.connection.execute(
                "SELECT id,payload FROM plans ORDER BY id"
            ).fetchall()
        )
        assert dict(stored) == originals
        assert not adapter.calls
        assert (
            await runtime.storage.run(
                lambda: runtime.database.connection.execute(
                    "SELECT COUNT(*) FROM bundle_commits"
                ).fetchone()[0]
            )
            == 0
        )
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_bundle_receipt_replay_retains_terminal_result_and_requires_member_authority(
    tmp_path, monkeypatch
):
    from mcp.server.auth.provider import AccessToken

    from domoai.application.bundle_commit import BundleCommitRequestMember, bundle_approval_digest
    from domoai.domain.models import PrincipalRole

    adapter = SimulatedHomeAdapter()
    runtime = await build_runtime(runtime_settings(tmp_path), adapter=adapter)
    try:
        context = mcp_context(runtime)
        context.bundle_commit_service = runtime.bundle_commit_service
        server = create_domotics_server(context)
        plan = runtime.plan_service.validate(light_plan(runtime, "receipt-member"))
        await runtime.plan_repository.save(plan)
        member = BundleCommitRequestMember(
            plan_id=plan.id, validation_digest=plan.validation.digest
        )
        args = {
            "scenario_id": "receipt",
            "bundle_digest": bundle_approval_digest("receipt", [member]),
            "members": [member.model_dump(mode="json")],
        }
        first = structured(await server.call_tool("commit_or_schedule_bundle", args))
        second = structured(await server.call_tool("commit_or_schedule_bundle", args))
        assert first["status"] == "completed"
        assert first == second
        assert (await runtime.plan_repository.get(plan.id)).status.value == "completed"
        before = await runtime.storage.run(
            lambda: runtime.database.connection.execute(
                "SELECT payload FROM bundle_commits"
            ).fetchall()
        )
        token = AccessToken(
            token="fixture",
            client_id="revoked",
            scopes=["mutate"],
            claims={
                "authority": AuthorityContext(
                    principal_id="revoked",
                    roles=[PrincipalRole.OWNER],
                    device_ids=["another-device"],
                ).model_dump(mode="json")
            },
        )
        with monkeypatch.context() as patch:
            patch.setattr(
                "mcp.server.auth.middleware.auth_context.get_access_token",
                lambda token=token: token,
            )
            rejected = structured(await server.call_tool("commit_or_schedule_bundle", args))
        assert rejected["error"]["code"] == "insufficient_scope"
        assert "bundle_commit_id" not in rejected
        assert (
            await runtime.storage.run(
                lambda: runtime.database.connection.execute(
                    "SELECT payload FROM bundle_commits"
                ).fetchall()
            )
            == before
        )
        assert len(adapter.calls) == 1
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_bundle_foreign_canonical_receipt_is_not_disclosed(tmp_path):
    from domoai.application.bundle_commit import BundleCommitRequestMember, bundle_approval_digest
    from domoai.domain.models import BundleCommit, BundleMemberCommit, PlanStatus

    adapter = SimulatedHomeAdapter()
    runtime = await build_runtime(runtime_settings(tmp_path), adapter=adapter)
    try:
        context = mcp_context(runtime)
        context.bundle_commit_service = runtime.bundle_commit_service
        foreign = runtime.plan_service.validate(
            runtime.plan_service.create_plan(
                "foreign-member",
                light_plan(runtime, "foreign-member").commands,
                authority=AuthorityContext(tenant_id="foreign"),
            )
        ).model_copy(update={"status": PlanStatus.COMPLETED})
        await runtime.plan_repository.save(foreign)
        member = BundleCommitRequestMember(
            plan_id=foreign.id, validation_digest=foreign.validation.digest
        )
        digest = bundle_approval_digest("foreign-receipt", [member])
        receipt = BundleCommit(
            authority=foreign.authority,
            id="foreign-receipt",
            scenario_id="foreign-receipt",
            bundle_digest=digest,
            status="completed",
            members=[
                BundleMemberCommit(
                    plan_id=foreign.id,
                    validation_digest=foreign.validation.digest,
                    status="executed",
                    execution_status="confirmed_success",
                )
            ],
        )
        await runtime.bundle_commit_service.bundle_repository.save(receipt)
        before = await runtime.storage.run(
            lambda: runtime.database.connection.execute(
                "SELECT payload FROM bundle_commits"
            ).fetchall()
        )
        server = create_domotics_server(context)
        response = structured(
            await server.call_tool(
                "commit_or_schedule_bundle",
                {
                    "scenario_id": receipt.scenario_id,
                    "bundle_digest": digest,
                    "members": [member.model_dump(mode="json")],
                },
            )
        )
        assert response["error"]["code"] == "insufficient_scope"
        assert "bundle_commit_id" not in response
        assert (
            await runtime.storage.run(
                lambda: runtime.database.connection.execute(
                    "SELECT payload FROM bundle_commits"
                ).fetchall()
            )
            == before
        )
        assert not adapter.calls
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("with_policy", [True, False])
async def test_canonical_lists_reject_foreign_tenant_on_same_household_and_device(
    tmp_path, monkeypatch, with_policy
):
    from datetime import timedelta

    from mcp.server.auth.provider import AccessToken

    from domoai.domain.automation import AutomationConsent, AutomationRule
    from domoai.domain.models import PrincipalRole

    adapter = SimulatedHomeAdapter()
    runtime = await build_runtime(runtime_settings(tmp_path), adapter=adapter)
    try:
        now = datetime.now(UTC)
        records = []
        for tenant in ("default", "foreign"):
            template = runtime.plan_service.create_plan(
                tenant + "-canonical",
                light_plan(runtime, tenant).commands,
                authority=AuthorityContext(tenant_id=tenant, household_id="default"),
            )
            rule = AutomationRule(
                authority=template.authority,
                id=template.id,
                name=tenant,
                trigger={
                    "type": "state_changed",
                    "device_id": template.commands[0].device_id,
                    "capability": "on",
                },
                plan_template=template,
                scope="scope",
                expires_at=now + timedelta(days=1),
            )
            consent = AutomationConsent(
                approval_id="grant",
                principal_id="owner",
                scope=rule.scope,
                rule_digest=rule.definition_digest,
                approved_at=now,
                expires_at=now + timedelta(days=1),
            )
            records.append((template, rule, consent))

        def seed():
            for template, rule, consent in records:
                runtime.database.connection.execute(
                    "INSERT INTO scheduled_plans VALUES(?,?,?,?,?)",
                    (
                        template.id,
                        now.isoformat(),
                        "pending",
                        template.model_dump_json(),
                        now.isoformat(),
                    ),
                )
                runtime.database.connection.execute(
                    "INSERT INTO automation_rules VALUES(?,?,?,?,?,?,?)",
                    (
                        rule.id,
                        rule.model_dump_json(),
                        consent.model_dump_json(),
                        "enabled",
                        None,
                        None,
                        now.isoformat(),
                    ),
                )
            runtime.database.connection.commit()

        await runtime.storage.run(seed)
        context = mcp_context(runtime)
        if not with_policy:
            context.authority_policy = None
        server = create_domotics_server(context)
        token = AccessToken(
            token="fixture",
            client_id="viewer",
            scopes=["read"],
            claims={
                "authority": AuthorityContext(
                    principal_id="viewer", roles=[PrincipalRole.VIEWER]
                ).model_dump(mode="json")
            },
        )
        for supplied in (token, None):
            with monkeypatch.context() as patch:
                patch.setattr(
                    "mcp.server.auth.middleware.auth_context.get_access_token",
                    lambda supplied=supplied: supplied,
                )
                scheduled = structured(await server.call_tool("list_scheduled_plans", {}))
                rules = structured(await server.call_tool("list_local_automation_rules", {}))
            assert {
                "plans": [p["plan_id"] for p in scheduled["plans"]],
                "rules": [r["rule_id"] for r in rules["rules"]],
            } == {"plans": ["default-canonical"], "rules": ["default-canonical"]}
            assert "foreign-canonical" not in json.dumps([scheduled, rules])
        assert await runtime.storage.run(
            lambda: runtime.database.connection.execute(
                "SELECT payload FROM scheduled_plans ORDER BY plan_id"
            ).fetchall()
        ) == [(template.model_dump_json(),) for template, _, _ in records]
        assert not adapter.calls
    finally:
        await runtime.close()
