import zipfile
from unittest.mock import MagicMock

import pytest

from deerflow.config.paths import Paths
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
