from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from deerflow.skills.storage.langfuse_skill_storage import LangfuseSkillStorage


def markdown(name="cloud-support", body="Check the incident timeline."):
    return f"---\nname: {name}\ndescription: Cloud troubleshooting\n---\n{body}\n"


@pytest.fixture
def storage(tmp_path, monkeypatch):
    from deerflow.config.paths import Paths

    monkeypatch.setattr("deerflow.config.paths.get_paths", lambda: Paths(base_dir=tmp_path / "home"))
    client = MagicMock()
    store = LangfuseSkillStorage(user_id="alice", host_path=str(tmp_path / "skills"), client=client)
    return store, client


def test_save_is_one_remote_version_with_history(storage):
    store, client = storage
    record = {"action": "edit", "prev_content": "old", "new_content": markdown()}
    store.save_custom_skill("cloud-support", markdown(), history_meta=record)
    kwargs = client.create_prompt.call_args.kwargs
    assert kwargs["name"] == "deerflow/skills/custom/alice/cloud-support"
    assert kwargs["prompt"] == markdown()
    assert kwargs["labels"] == ["production"]
    assert kwargs["config"]["history"]["prev_content"] == "old"
    client.create_prompt.assert_called_once()


def test_failed_publish_does_not_change_local_snapshot(storage):
    store, client = storage
    target = store.get_custom_skill_file("cloud-support")
    target.parent.mkdir(parents=True)
    target.write_text("old", encoding="utf-8")
    client.create_prompt.side_effect = RuntimeError("unavailable")
    with pytest.raises(RuntimeError):
        store.save_custom_skill("cloud-support", markdown())
    assert target.read_text(encoding="utf-8") == "old"


def test_discovery_is_scoped_and_does_not_include_local_packages(storage, monkeypatch):
    store, client = storage
    client.api.prompts.list.return_value = SimpleNamespace(
        data=[
            SimpleNamespace(name="deerflow/skills/public/cloud-support"),
            SimpleNamespace(name="deerflow/skills/custom/bob/private-skill"),
        ],
        meta=SimpleNamespace(total_pages=1),
    )
    client.get_prompt.return_value = SimpleNamespace(prompt=markdown(), config={}, version=3)
    monkeypatch.setattr("deerflow.config.extensions_config.ExtensionsConfig.from_file", lambda: SimpleNamespace(is_skill_enabled=lambda *args: True))
    skills = store.load_skills(enabled_only=True)
    assert [s.name for s in skills] == ["cloud-support"]
    assert all("bob" not in call.args[0] for call in client.get_prompt.call_args_list)
    assert skills[0].skill_file.read_text(encoding="utf-8") == markdown()


def test_remote_outage_fails_closed(storage):
    store, client = storage
    client.api.prompts.list.side_effect = RuntimeError("unavailable")
    with pytest.raises(RuntimeError):
        store.load_skills()


def test_scripts_and_support_files_are_rejected(storage):
    store, client = storage
    with pytest.raises(ValueError, match="SKILL.md"):
        store.write_custom_skill("cloud-support", "scripts/run.sh", "echo unsafe")
    client.create_prompt.assert_not_called()


def test_delete_preserves_versions_as_tombstone(storage):
    store, client = storage
    client.get_prompt.return_value = SimpleNamespace(prompt=markdown(), config={}, version=1)
    store.delete_custom_skill("cloud-support")
    assert client.create_prompt.call_args.kwargs["config"]["deleted"] is True
    client.api.prompts.delete.assert_not_called()


def test_remote_skill_path_cannot_escape_scope(storage):
    store, client = storage
    with pytest.raises(ValueError):
        store.read_skill_content(Path("/tmp/outside/SKILL.md"))
    client.get_prompt.assert_not_called()


def test_history_includes_external_versions_and_deleted_content(storage):
    store, client = storage
    client.api.prompts.list.return_value = SimpleNamespace(data=[SimpleNamespace(name="deerflow/skills/custom/alice/cloud-support", versions=[1, 3, 4])])
    versions = {
        1: SimpleNamespace(prompt=markdown(body="First"), config={}),
        3: SimpleNamespace(prompt=markdown(body="Second"), config={}),
        4: SimpleNamespace(prompt=markdown(body="Second"), config={"deleted": True, "history": {"action": "delete", "prev_content": markdown(body="Second"), "new_content": None}}),
    }
    client.get_prompt.side_effect = lambda *args, **kwargs: versions[kwargs["version"]]
    history = store.read_history("cloud-support")
    assert [r["version"] for r in history] == [1, 3, 4]
    assert history[1]["prev_content"] == markdown(body="First")
    assert history[2]["prev_content"] == markdown(body="Second")


def test_pagination_does_not_drop_later_skills(storage):
    store, client = storage
    client.api.prompts.list.side_effect = [
        SimpleNamespace(data=[], meta=SimpleNamespace(total_pages=2)),
        SimpleNamespace(data=[SimpleNamespace(name="deerflow/skills/public/cloud-support")], meta=SimpleNamespace(total_pages=2)),
    ]
    client.get_prompt.return_value = SimpleNamespace(prompt=markdown(), config={})
    assert len(list(store._iter_skill_files())) == 1
    assert [c.kwargs["page"] for c in client.api.prompts.list.call_args_list] == [1, 2]
    assert all("name" not in c.kwargs for c in client.api.prompts.list.call_args_list)


def test_deleted_and_unpublished_skills_are_not_discovered(storage):
    from langfuse.api import NotFoundError

    store, client = storage
    client.api.prompts.list.return_value = SimpleNamespace(data=[SimpleNamespace(name="deerflow/skills/public/cloud-support")], meta=SimpleNamespace(total_pages=1))
    client.get_prompt.return_value = SimpleNamespace(prompt=markdown(), config={"deleted": True})
    assert list(store._iter_skill_files()) == []
    client.get_prompt.side_effect = NotFoundError(body={})
    assert list(store._iter_skill_files()) == []


def test_snapshots_are_stable_and_sandboxes_get_empty_views(storage):
    from deerflow.skills.projection import ensure_skill_projections
    from deerflow.skills.types import SkillCategory

    store, client = storage
    path = store._snapshot(SkillCategory.CUSTOM, "cloud-support", markdown())
    before = path.stat().st_mtime_ns
    store._snapshot(SkillCategory.CUSTOM, "cloud-support", markdown())
    assert path.stat().st_mtime_ns == before
    views = ensure_skill_projections(store)
    for root in (views.public, views.custom, views.legacy, views.integrations):
        assert list(root.iterdir()) == []
    client.api.prompts.list.assert_not_called()


@pytest.mark.asyncio
async def test_read_skill_without_any_sandbox_and_keep_range_semantics(storage, monkeypatch):
    from deerflow.sandbox import tools
    from deerflow.skills.types import SkillCategory

    store, client = storage
    client.get_prompt.return_value = SimpleNamespace(prompt=markdown(), config={})
    path = store._snapshot(SkillCategory.CUSTOM, "cloud-support", markdown())
    from deerflow.skills.parser import parse_skill_file

    skill = parse_skill_file(path, SkillCategory.CUSTOM)
    monkeypatch.setattr(store, "load_skills", lambda **kwargs: [skill])
    monkeypatch.setattr("deerflow.skills.storage.get_or_new_user_skill_storage", lambda *args, **kwargs: store)
    monkeypatch.setattr("deerflow.skills.loading.is_backend_skill_path", lambda path: path.startswith("/mnt/skills/"))
    monkeypatch.setattr(tools, "_is_disabled_skill_path", lambda *args, **kwargs: False)

    def no_sandbox(*args, **kwargs):
        pytest.fail("Skill loading acquired a sandbox")

    monkeypatch.setattr(tools, "ensure_sandbox_initialized", no_sandbox)
    monkeypatch.setattr(tools, "ensure_sandbox_initialized_async", no_sandbox)
    runtime = SimpleNamespace(context={"user_id": "alice"}, state={})
    virtual_path = "/mnt/skills/custom/cloud-support/SKILL.md"
    assert await tools.read_file_tool.coroutine(runtime, virtual_path) == markdown()
    assert await tools.read_file_tool.coroutine(runtime, virtual_path, start_line=2, end_line=3) == "name: cloud-support\ndescription: Cloud troubleshooting"
    assert "exceeds" in await tools.read_file_tool.coroutine(runtime, virtual_path, start_line=99)
    assert "Error:" in await tools.read_file_tool.coroutine(runtime, "/mnt/skills/custom/cloud-support/scripts/run.sh")


def test_agent_allowlist_denies_host_read(storage, monkeypatch):
    from deerflow.skills.loading import available_skill_names, read_backend_skill
    from deerflow.skills.parser import parse_skill_file
    from deerflow.skills.types import SkillCategory

    store, client = storage
    path = store._snapshot(SkillCategory.CUSTOM, "cloud-support", markdown())
    skill = parse_skill_file(path, SkillCategory.CUSTOM)
    monkeypatch.setattr(store, "load_skills", lambda **kwargs: [skill])
    monkeypatch.setattr("deerflow.skills.storage.get_or_new_user_skill_storage", lambda *args, **kwargs: store)
    token = available_skill_names.set(set())
    try:
        with pytest.raises(PermissionError):
            read_backend_skill(SimpleNamespace(context={"user_id": "alice"}), "/mnt/skills/custom/cloud-support/SKILL.md")
    finally:
        available_skill_names.reset(token)
    client.get_prompt.assert_not_called()


@pytest.mark.asyncio
async def test_text_archive_with_script_is_rejected_before_publish(storage, tmp_path):
    import zipfile

    store, client = storage
    path = tmp_path / "bad.skill"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("cloud-support/SKILL.md", markdown())
        archive.writestr("cloud-support/scripts/run.sh", "echo unsafe")
    with pytest.raises(ValueError, match="only SKILL.md"):
        await store.ainstall_skill_from_archive(path)
    client.create_prompt.assert_not_called()


def test_export_refreshes_from_remote_text(storage):
    import zipfile

    from deerflow.skills.export import build_skill_export, export_manifest

    store, client = storage
    client.get_prompt.return_value = SimpleNamespace(prompt=markdown(), config={})
    manifest = export_manifest(store, "cloud-support")
    archive = build_skill_export(store, "cloud-support", manifest["revision"])
    try:
        with zipfile.ZipFile(archive.file) as zipped:
            assert zipped.namelist() == ["cloud-support/", "cloud-support/SKILL.md"]
            assert zipped.read("cloud-support/SKILL.md").decode() == markdown()
    finally:
        archive.file.close()


def test_storage_uses_real_sdk_request_contract(storage):
    import json

    import httpx
    from langfuse import Langfuse

    store, _ = storage
    name = "deerflow/skills/custom/alice/cloud-support"
    requests = []
    payload = {"name": name, "type": "text", "prompt": markdown(), "version": 1, "labels": ["production"], "tags": [], "config": {}}

    def respond(request):
        requests.append(request)
        if request.method == "POST":
            body = json.loads(request.content)
            assert body["name"] == name
            assert body["labels"] == ["production"]
            assert body["config"]["history"]["action"] == "edit"
            return httpx.Response(200, json=payload)
        if request.url.path == "/api/public/v2/prompts":
            # The API name filter is exact, not a prefix search.
            assert "name" not in request.url.params
            return httpx.Response(
                200,
                json={
                    "data": [{"name": name, "type": "text", "versions": [1], "labels": ["production"], "tags": [], "lastUpdatedAt": "2026-10-08T00:00:00Z", "lastConfig": {}}],
                    "meta": {"page": 1, "limit": 100, "totalItems": 1, "totalPages": 1},
                },
            )
        assert request.url.path.endswith("/" + name)
        assert request.url.params["label"] == "production"
        return httpx.Response(200, json=payload)

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        client = Langfuse(public_key="pk-lf-cloud-skills-contract", secret_key="sk-lf-test", base_url="https://langfuse.invalid", httpx_client=http, tracing_enabled=False)
        store._client = client
        try:
            store.save_custom_skill("cloud-support", markdown())
            assert store.read_custom_skill("cloud-support") == markdown()
            assert len(list(store._iter_skill_files())) == 1
        finally:
            client.shutdown()
    assert [r.method for r in requests] == ["POST", "GET", "GET", "GET"]


def test_factory_selects_langfuse_for_public_and_user_scopes(storage, monkeypatch):
    from deerflow.config.skills_config import SkillsConfig
    from deerflow.skills.storage import get_or_new_skill_storage, get_or_new_user_skill_storage, reset_skill_storage

    _, client = storage
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
    monkeypatch.setattr("langfuse.get_client", lambda: client)
    config = SimpleNamespace(skills=SkillsConfig(use="deerflow.skills.storage.langfuse_skill_storage:LangfuseSkillStorage", langfuse_prefix="cloud/knowledge", langfuse_label="staging"))
    try:
        public = get_or_new_skill_storage(app_config=config)
        private = get_or_new_user_skill_storage("alice", app_config=config)
        assert isinstance(public, LangfuseSkillStorage) and public._public_only
        assert isinstance(private, LangfuseSkillStorage) and not private._public_only
        private.save_custom_skill("cloud-support", markdown())
        assert client.create_prompt.call_args.kwargs["name"] == "cloud/knowledge/custom/alice/cloud-support"
        assert client.create_prompt.call_args.kwargs["labels"] == ["staging"]
    finally:
        reset_skill_storage()


@pytest.mark.asyncio
async def test_parallel_agents_keep_independent_allowlists(monkeypatch):
    import asyncio

    from langchain_core.messages import ToolMessage

    from deerflow.sandbox.middleware import SandboxMiddleware
    from deerflow.skills.loading import available_skill_names

    first = SandboxMiddleware(available_skills={"first"})
    second = SandboxMiddleware(available_skills={"second"})
    request = SimpleNamespace(runtime=SimpleNamespace(state={}, context={}))

    async def handler(_request):
        before = available_skill_names.get()
        await asyncio.sleep(0)
        assert available_skill_names.get() == before
        return ToolMessage(content=next(iter(before)), tool_call_id="read")

    results = await asyncio.gather(first.awrap_tool_call(request, handler), second.awrap_tool_call(request, handler))
    assert [result.content for result in results] == ["first", "second"]
    assert available_skill_names.get() is None


def test_restricted_agent_does_not_acquire_sandbox_for_text_skills(monkeypatch):
    from deerflow.sandbox.middleware import SandboxMiddleware

    monkeypatch.setattr("deerflow.skills.loading.backend_skills_enabled", lambda: True)

    def no_sandbox():
        pytest.fail("Text skills requested a sandbox provider")

    monkeypatch.setattr("deerflow.sandbox.middleware.get_sandbox_provider", no_sandbox)
    middleware = SandboxMiddleware(available_skills={"cloud-support"})
    assert middleware._prepare_agent_skill_projection("thread", user_id="alice") is None


def test_skill_authoring_prompt_uses_self_contained_text():
    from deerflow.agents.lead_agent.prompt import _build_skill_evolution_section

    prompt = _build_skill_evolution_section(True)
    assert 'path="scripts/run.py"' not in prompt
    assert "self-contained instructions and knowledge" in prompt
