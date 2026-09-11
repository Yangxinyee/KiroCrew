"""The member task board shares the real ledger behind an owner and generation gate."""

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner

from kiro_crew import member_memory_auth, members, work_ledger
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.dashboard.handlers import member_work
from kiro_crew.dashboard.routes import agents as member_routes
from kiro_crew.member_memory_auth import bind_private_session_store
from kiro_crew.memory_stores import provision_member_memory

MEMBER = "Reviewer"
SLUG = "reviewer"


@pytest.fixture
def member_app(monkeypatch):
    cfg = KiroCrewConfig.load()
    cfg.agents[MEMBER] = KiroCrewAgentConfig(kiro_agent="kirocrew")
    store = provision_member_memory(cfg, MEMBER)
    cfg.save()
    key = members.member_slot_key(SLUG, store)

    # Exercise real ownership records and readers; publication atomicity is
    # covered separately and the host may lack renameat2 (older Linux libc).
    def publish_fixture(staging, destination):
        assert not destination.exists()
        staging.rename(destination)

    with monkeypatch.context() as publication:
        publication.setattr(member_memory_auth, "_publish_private_binding_dir", publish_fixture)
        bind_private_session_store(f"dashboard:{key}", store)
    members.write_dm_binding(SLUG, member=MEMBER, slot_key=key, memory_store=store)
    slot = SimpleNamespace(
        key=key,
        mode=members.DM_SLOT_MODE,
        agent=MEMBER,
        memory_store=store,
        running=False,
        _lock=asyncio.Lock(),
    )
    slots = {key: slot}
    app = web.Application()

    @web.middleware
    async def internal_identity(request, handler):
        if request.headers.get("X-Test-Internal"):
            request["internal_auth"] = True
        return await handler(request)

    app.middlewares.append(internal_identity)
    app["state"] = SimpleNamespace(owner_id="", _slots=slots, get_slot=slots.get)
    member_routes.register(app)
    monkeypatch.setattr(KiroCrewConfig, "load", staticmethod(lambda: cfg))
    return as_owner(app), cfg, slot, f"/api/members/{SLUG}/work?member={MEMBER}&slot={key}"


@pytest.mark.asyncio
async def test_owner_capture_and_worker_reports_share_the_same_record(member_app):
    app, _, slot, url = member_app
    async with TestClient(TestServer(app)) as client:
        response = await client.get(url)
        assert response.status == 200, await response.text()
        assert (await response.json())["items"] == []
        response = await client.post(
            url, json={"title": "Review the change", "criteria": "All checks pass"}
        )
        assert response.status == 201, await response.text()
        item = (await response.json())["item"]
        assert item["state"] == "open"
        assert item["status"] is None
        assert item["worker_session_key"] is None
        assert item["acceptance"] == {
            "kind": "human_approval",
            "description": "All checks pass",
        }
        work_ledger.apply_conductor_action(
            slot.key, "bind", item_id=item["item_id"], worker_session_key="worker"
        )
        binding = work_ledger.read_binding("worker")
        assert binding == (slot.key, item["item_id"])
        work_ledger.apply_worker_report(
            *binding, status="done", summary="Checks passed", artifacts={"result": "report.txt"}
        )
        response = await client.get(url)
        payload = await response.json()
        assert response.status == 200, payload
        assert "accept_batch" not in payload
        assert payload["slot_key"] == slot.key
        assert payload["items"][0]["summary"] == "Checks passed"
        assert payload["items"][0]["status"] == "done"
        assert payload["items"][0]["state"] == "open"
        assert payload["items"][0]["events"][-1]["kind"] == "report"


@pytest.mark.asyncio
async def test_worker_credentials_are_redacted_without_breaking_json(member_app):
    app, _, slot, url = member_app
    async with TestClient(TestServer(app)) as client:
        created = await client.post(url, json={"title": "Review", "criteria": "Checks pass"})
        item = (await created.json())["item"]
        work_ledger.apply_conductor_action(
            slot.key, "bind", item_id=item["item_id"], worker_session_key="worker"
        )
        work_ledger.apply_worker_report(
            slot.key,
            item["item_id"],
            status="progress",
            summary='Keep "quoted", bracketed } text',
            artifacts={"aws_secret_access_key": "example", "report": 'notes "quoted".txt'},
        )
        response = await client.get(url)
        assert response.status == 200, await response.text()
        row = (await response.json())["items"][0]
        assert row["artifacts"]["aws_secret_access_key"] == "[REDACTED]"
        assert row["artifacts"]["report"] == 'notes "quoted".txt'
        assert row["summary"] == 'Keep "quoted", bracketed } text'


@pytest.mark.asyncio
async def test_deep_acceptance_remains_readable_and_redacted(member_app):
    app, _, slot, url = member_app
    nested: Any = {"aws_secret_access_key": "example", "evidence": 'Keep "quoted" text'}
    for _ in range(600):
        nested = [nested]
    work_ledger.ensure_conductor(slot.key, goal="Review safely")
    work_ledger.apply_conductor_action(
        slot.key,
        "create",
        title="Nested acceptance",
        acceptance={"kind": "human_approval", "evidence": nested},
    )
    async with TestClient(TestServer(app)) as client:
        response = await client.get(url)
        assert response.status == 200, await response.text()
        evidence = (await response.json())["items"][0]["acceptance"]["evidence"]
        for _ in range(600):
            assert len(evidence) == 1
            evidence = evidence[0]
        assert evidence == {
            "aws_secret_access_key": "[REDACTED]",
            "evidence": 'Keep "quoted" text',
        }


@pytest.mark.asyncio
async def test_redacted_artifact_labels_preserve_every_entry(member_app):
    app, _, slot, url = member_app
    credentials = ["ghp_" + letter * 36 for letter in ("a", "b")]
    marker = member_work.redact(credentials[0])
    assert marker != credentials[0]
    async with TestClient(TestServer(app)) as client:
        created = await client.post(url, json={"title": "Review", "criteria": "Checks pass"})
        item = (await created.json())["item"]
        work_ledger.apply_conductor_action(
            slot.key, "bind", item_id=item["item_id"], worker_session_key="worker"
        )
        work_ledger.apply_worker_report(
            slot.key,
            item["item_id"],
            status="progress",
            summary="Reports",
            artifacts={credentials[0]: "first", credentials[1]: "second", marker: "third"},
        )
        response = await client.get(url)
        assert response.status == 200, await response.text()
        row = (await response.json())["items"][0]
        assert set(row["artifacts"]) == {marker, f"{marker} (2)", f"{marker} (3)"}
        assert sorted(row["artifacts"].values()) == ["[REDACTED]", "[REDACTED]", "third"]
        text = await response.text()
        assert not any(secret in text for secret in credentials)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers,status",
    [
        ({"X-Test-User": "other-person"}, 403),
        ({"X-Test-App": "some-app"}, 404),
        ({"X-Test-Internal": "1"}, 403),
    ],
)
async def test_non_owner_cannot_read_or_create(member_app, headers, status):
    app, _, slot, url = member_app
    async with TestClient(TestServer(app)) as client:
        assert (await client.get(url, headers=headers)).status == status
        assert (await client.post(url, data="{", headers=headers)).status == status
    assert work_ledger.read_conductor(slot.key) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["hint", "member", "generation", "live-agent", "private"])
async def test_stale_or_foreign_identity_is_not_a_ledger_selector(member_app, mismatch):
    app, cfg, slot, url = member_app
    if mismatch == "hint":
        url = url.replace(f"slot={slot.key}", "slot=some-other-session")
    elif mismatch == "member":
        cfg.agents["reviewer"] = cfg.agents[MEMBER]
        url = url.replace(f"member={MEMBER}", "member=reviewer")
    elif mismatch == "generation":
        members.write_dm_binding(SLUG, member=MEMBER, slot_key=members.member_slot_key(SLUG))
    elif mismatch == "live-agent":
        slot.agent = "Someone else"
    else:
        slot.memory_store = "default"
    async with TestClient(TestServer(app)) as client:
        for response in (
            await client.get(url),
            await client.post(url, json={"title": "wrong", "criteria": "wrong"}),
        ):
            assert response.status == 409, await response.text()
    assert work_ledger.read_conductor(slot.key) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"title": "", "criteria": "test"},
        {"title": "task", "criteria": " "},
        {"title": "x" * 201, "criteria": "test"},
        {"title": "task", "criteria": "x" * 4001},
        {"title": "task", "criteria": "test", "state": "accepted"},
    ],
)
async def test_invalid_capture_cannot_write_ledger_fields(member_app, body):
    app, _, slot, url = member_app
    async with TestClient(TestServer(app)) as client:
        assert (await client.post(url, json=body)).status == 400
    assert work_ledger.read_conductor(slot.key) is None


@pytest.mark.asyncio
async def test_revalidate_member_after_awaited_read(member_app, monkeypatch):
    app, _, slot, url = member_app
    work_ledger.ensure_conductor(slot.key)
    original = member_work.read_ledger_snapshot

    async def renamed(*args):
        payload = await original(*args)
        slot.agent = "Renamed"
        return payload

    monkeypatch.setattr(member_work, "read_ledger_snapshot", renamed)
    async with TestClient(TestServer(app)) as client:
        assert (await client.get(url)).status == 409


@pytest.mark.asyncio
async def test_unreadable_ledger_is_an_error_not_an_empty_board(member_app, monkeypatch):
    app, _, _, url = member_app

    def unavailable(*args, **kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr(work_ledger, "read_conductor", unavailable)
    async with TestClient(TestServer(app)) as client:
        response = await client.get(url)
        assert response.status == 503
        assert (await response.json())["code"] == "member_work_unavailable"


@pytest.mark.asyncio
@pytest.mark.usefixtures("healthy_host_memory")
async def test_private_member_dispatch_binds_before_first_turn_and_reports(member_app, monkeypatch):
    """Real spawn admission and HTTP work routes, with only the LLM replaced."""
    from unittest.mock import AsyncMock

    from test_subagent import _mock_ctx_builder_auto_spawn, _mock_sessions

    from kiro_crew import context
    from kiro_crew.dashboard.handlers import _shared, messaging
    from kiro_crew.dashboard.handlers import work_ledger as work_routes
    from kiro_crew.subagent import SubagentManager
    from kiro_crew.subagent_persistence import read_run_memory_store

    app, _, slot, url = member_app
    parent = f"dashboard:{slot.key}"
    state = app["state"]
    state.conversation_log = None
    state._restricted_keys = set()
    slot.workspace = "default"
    slot.is_restricted = False
    sessions = _mock_sessions()
    sessions.get_approval_policy.return_value = "auto"
    ctx = _mock_ctx_builder_auto_spawn()
    ctx.conversation_log = None
    manager = SubagentManager(sessions=sessions, ctx_builder=ctx)
    state.subagents = manager
    monkeypatch.setattr(manager, "_should_use_session_sharing", lambda info: False)
    monkeypatch.setattr(context, "prepare_store_vectors", AsyncMock())

    def publish_fixture(staging, destination):
        assert not destination.exists()
        staging.rename(destination)

    monkeypatch.setattr(member_memory_auth, "_publish_private_binding_dir", publish_fixture)

    async def verified_scope(request):
        # Stand in for the authenticated MCP process envelope; all store and
        # delegation checks below still resolve real protected ownership files.
        key = request.headers.get("X-Test-Verified-Session")
        store = await asyncio.to_thread(context.store_of_session, None, key) if key else None
        return _shared.MemberScope(key, True, store)

    monkeypatch.setattr(_shared, "member_request_scope", verified_scope)
    app.router.add_post("/api/spawn", messaging.api_spawn)
    app.router.add_get("/api/work-ledger/brief", work_routes.api_work_brief)
    app.router.add_post("/api/work-ledger/report", work_routes.api_work_report)
    app.router.add_post("/api/work-ledger/record", work_routes.api_work_ledger_record)

    def headers(key):
        return {"X-Test-Internal": "1", "X-Session-Key": key, "X-Test-Verified-Session": key}

    async with TestClient(TestServer(app)) as client:
        created = await client.post(
            url, json={"title": "Verify task", "criteria": "Report evidence"}
        )
        assert created.status == 201, await created.text()
        item_id = (await created.json())["item"]["item_id"]
        observed = []
        provider = sessions.get_or_create.return_value[0]
        provider.context_window_tokens = lambda: 0
        provider.context_used_tokens = lambda: 0

        async def stream(*args, **kwargs):
            worker = sessions.get_or_create.call_args.args[0]
            assert work_ledger.read_binding(worker) == (slot.key, item_id)
            assert read_run_memory_store(worker.split(":", 1)[1]) == slot.memory_store
            assert context.store_of_session(None, worker) == slot.memory_store
            brief = await client.get("/api/work-ledger/brief", headers=headers(worker))
            assert brief.status == 200, await brief.text()
            for status in ("progress", "blocked", "done"):
                report = await client.post(
                    "/api/work-ledger/report",
                    headers=headers(worker),
                    json={"status": status, "summary": f"Worker reports {status}"},
                )
                assert report.status == 200, await report.text()
                board = await (await client.get(url)).json()
                observed.append((board["items"][0]["status"], board["items"][0]["state"]))
            if False:
                yield

        provider.stream.side_effect = stream
        response = await client.post(
            "/api/spawn",
            headers=headers(parent),
            json={
                "task": "Read the brief and report",
                "parent_session": parent,
                "work_item_id": item_id,
            },
        )
        assert response.status == 200, await response.text()
        try:
            await asyncio.wait_for(asyncio.gather(*list(manager._tasks.values())), timeout=10)
        finally:
            await manager.cancel_all()
        info = next(iter(manager._agents.values()))
        assert info.error == "", info.error
        assert observed == [("progress", "open"), ("blocked", "open"), ("done", "open")]
        assert info.memory_store == slot.memory_store
        duplicate = await client.post(
            "/api/spawn",
            headers=headers(parent),
            json={"task": "Duplicate", "parent_session": parent, "work_item_id": item_id},
        )
        assert duplicate.status == 409, await duplicate.text()
        assert (await duplicate.json())["code"] == "already_bound"
        for action in (
            {"action": "verdict", "verdict": "pass"},
            {"action": "close", "state": "accepted"},
        ):
            accepted = await client.post(
                "/api/work-ledger/record",
                headers=headers(parent),
                json={"item_id": item_id, **action},
            )
            assert accepted.status == 200, await accepted.text()
        board = await (await client.get(url)).json()
        assert board["items"][0]["state"] == "accepted"
        borrowed = headers(parent)
        borrowed["X-Session-Key"] = "subagent:" + info.id
        refused = await client.get("/api/work-ledger/brief", headers=borrowed)
        assert refused.status == 403
        assert (await refused.json())["code"] == "member_session_unverified"


@pytest.mark.asyncio
async def test_archive_retains_reports_and_releases_active_board_capacity(member_app):
    app, _, slot, url = member_app
    query = url.split("?", 1)[1]
    async with TestClient(TestServer(app)) as client:
        created = await client.post(
            url, json={"title": "Finished work", "criteria": "Owner accepts"}
        )
        item_id = (await created.json())["item"]["item_id"]
        archive_url = f"/api/members/{SLUG}/work/{item_id}/archive?{query}"
        refused = await client.post(archive_url, json={})
        assert refused.status == 409
        work_ledger.apply_conductor_action(slot.key, "close", item_id=item_id, state="accepted")
        archived = await client.post(archive_url, json={})
        assert archived.status == 200, await archived.text()
        assert (await (await client.get(url)).json())["items"] == []
        history = await client.get(f"/api/members/{SLUG}/work/archive?{query}")
        assert history.status == 200, await history.text()
        rows = (await history.json())["items"]
        assert [item["item_id"] for item in rows] == [item_id]
        assert rows[0]["state"] == "accepted"
        assert rows[0]["events"][-1]["kind"] == "close"
        stale = await client.get(f"/api/members/{SLUG}/work/archive?member={MEMBER}&slot=old")
        assert stale.status == 409


def test_task_dispatch_uses_strict_identity_on_the_wire(monkeypatch):
    from unittest.mock import Mock

    from kiro_crew import mcp_core
    from kiro_crew.mcp_tools.spawn import spawn_run

    post = Mock(return_value={"id": "abcdef12"})
    monkeypatch.setattr(mcp_core, "_resolve_session_key", lambda: "wrong-parent")
    monkeypatch.setattr(mcp_core, "require_strict_session_key", lambda *a, **kw: ("member-own", ""))
    monkeypatch.setattr(mcp_core, "_post", post)
    result = spawn_run("spawn_run", {"task": "Report progress", "work_item_id": "it_12345678"})
    assert "abcdef12" in result
    post.assert_called_once_with(
        "/api/spawn",
        {
            "task": "Report progress",
            "agent": "",
            "parent_session": "member-own",
            "work_item_id": "it_12345678",
        },
        session_key="member-own",
    )
    post.reset_mock()
    assert "requires one task" in spawn_run(
        "spawn_run", {"tasks": ["one", "two"], "work_item_id": "it_12345678"}
    )
    post.assert_not_called()
    monkeypatch.setattr(
        mcp_core, "require_strict_session_key", lambda *a, **kw: ("", "Identity unavailable")
    )
    assert (
        spawn_run("spawn_run", {"task": "one", "work_item_id": "it_12345678"})
        == "Identity unavailable"
    )
    post.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.usefixtures("healthy_host_memory")
async def test_task_closed_during_spawn_approval_never_starts_provider(member_app):
    from test_subagent import _mock_ctx_builder, _mock_sessions

    from kiro_crew.subagent import SubagentManager

    _, _, slot, _ = member_app
    work_ledger.ensure_conductor(slot.key)
    item_id = work_ledger.apply_conductor_action(
        slot.key, "create", title="May be cancelled", acceptance={"kind": "human_approval"}
    )["item"].item_id
    sessions = _mock_sessions()
    sessions.get_approval_policy.return_value = ""
    ctx = _mock_ctx_builder()
    ctx.conversation_log = None
    approval_started = asyncio.Event()
    allow = asyncio.Event()

    async def approve(*args):
        approval_started.set()
        await asyncio.wait_for(allow.wait(), timeout=5)
        return True

    manager = SubagentManager(sessions=sessions, ctx_builder=ctx, on_spawn_approval=approve)
    info = manager.spawn(
        "Execute after approval",
        parent_session_key=f"dashboard:{slot.key}",
        memory_store=slot.memory_store,
        work_item_id=item_id,
    )
    assert info is not None
    try:
        await asyncio.wait_for(approval_started.wait(), timeout=5)
        assert work_ledger.read_binding(f"subagent:{info.id}") is None
        work_ledger.apply_conductor_action(slot.key, "close", item_id=item_id, state="abandoned")
        allow.set()
        await asyncio.wait_for(asyncio.gather(*list(manager._tasks.values())), timeout=10)
        sessions.get_or_create.assert_not_awaited()
        assert info.done and "abandoned" in info.error
        assert work_ledger.read_binding(f"subagent:{info.id}") is None
    finally:
        allow.set()
        await manager.cancel_all()
