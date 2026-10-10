import zipfile
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from deerflow.config.paths import Paths
from deerflow.sandbox import tools
from deerflow.skills.storage.langfuse_skill_storage import LangfuseSkillStorage

CONTENT = "---\nname: cloud-support\ndescription: Cloud troubleshooting\n---\nCheck the incident timeline.\n"


@pytest.fixture
def remote(tmp_path, monkeypatch):
    monkeypatch.setattr("deerflow.config.paths.get_paths", lambda: Paths(base_dir=tmp_path / "home"))
    client = MagicMock()
    storage = LangfuseSkillStorage(user_id="alice", host_path=str(tmp_path / "skills"), client=client)
    archive = tmp_path / "cloud.skill"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("cloud-support/SKILL.md", CONTENT)
    return storage, client, archive


@pytest.mark.asyncio
async def test_remote_install_keeps_filesystem_phases_off_loop(remote, monkeypatch):
    storage, client, archive = remote
    client.get_prompt.return_value = None

    async def scan(*args, **kwargs):
        pass

    monkeypatch.setattr("deerflow.skills.installer._scan_skill_archive_contents_or_raise", scan)
    result = await storage.ainstall_skill_from_archive(archive)
    assert result["success"] is True
    client.create_prompt.assert_called_once()


@pytest.mark.asyncio
async def test_remote_read_without_sandbox_stays_off_loop(remote, monkeypatch):
    storage, client, _ = remote
    client.get_prompt.return_value = SimpleNamespace(prompt=CONTENT, config={})
    client.api.prompts.list.return_value = SimpleNamespace(data=[SimpleNamespace(name="deerflow/skills/public/cloud-support")], meta=SimpleNamespace(total_pages=1))
    monkeypatch.setattr("deerflow.skills.storage.get_or_new_user_skill_storage", lambda *args, **kwargs: storage)
    monkeypatch.setattr("deerflow.skills.loading.is_backend_skill_path", lambda path: path.startswith("/mnt/skills/"))
    monkeypatch.setattr(tools, "_is_disabled_skill_path", lambda *args, **kwargs: False)
    monkeypatch.setattr("deerflow.config.extensions_config.ExtensionsConfig.from_file", lambda: SimpleNamespace(is_skill_enabled=lambda *args: True))

    def no_sandbox(*args, **kwargs):
        pytest.fail("Skill read acquired a sandbox")

    monkeypatch.setattr(tools, "ensure_sandbox_initialized_async", no_sandbox)
    monkeypatch.setattr(tools, "ensure_sandbox_initialized", no_sandbox)
    runtime = SimpleNamespace(context={"user_id": "alice"}, state={})
    assert await tools.read_file_tool.coroutine(runtime, "/mnt/skills/public/cloud-support/SKILL.md") == CONTENT
    assert runtime.state == {}
