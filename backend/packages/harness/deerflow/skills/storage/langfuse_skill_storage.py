"""Text-only skills versioned as Langfuse text prompts.

Local files are disposable snapshots for existing parsers and exports. They
are never a fallback when Langfuse is unavailable and contain no support files.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from deerflow.constants import DEFAULT_SKILLS_CONTAINER_PATH
from deerflow.skills.storage.user_scoped_skill_storage import UserScopedSkillStorage
from deerflow.skills.types import SKILL_MD_FILE, SkillCategory


class LangfuseSkillStorage(UserScopedSkillStorage):
    text_only = True

    def __init__(self, host_path=None, container_path=DEFAULT_SKILLS_CONTAINER_PATH, app_config=None, *, user_id=None, client=None):
        super().__init__(user_id=user_id or "default", host_path=host_path, container_path=container_path, app_config=app_config)
        from deerflow.config.skills_config import SkillsConfig

        config = app_config.skills if app_config is not None else SkillsConfig()
        self._prefix = config.langfuse_prefix
        self._label = config.langfuse_label
        self._public_only = user_id is None
        if client is None:
            from langfuse import get_client

            if not os.getenv("LANGFUSE_PUBLIC_KEY") or not os.getenv("LANGFUSE_SECRET_KEY"):
                raise ValueError("Langfuse skills require LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY on the Gateway.")
            client = get_client()
        self._client = client
        # Namespace snapshots separately from operator-owned skills and other projects.
        namespace = hashlib.sha256(f"{os.getenv('LANGFUSE_BASE_URL', os.getenv('LANGFUSE_HOST', ''))}/{os.getenv('LANGFUSE_PUBLIC_KEY', '')}/{self._prefix}/{self._label}".encode()).hexdigest()[:24]
        self._host_root = self._paths.base_dir / "langfuse-skills" / namespace
        self._user_custom_root = self._host_root / "users" / self.user_id / "custom"

    def _prompt_name(self, category: SkillCategory, name: str) -> str:
        name = self.validate_skill_name(name)
        scope = f"custom/{self.user_id}" if category == SkillCategory.CUSTOM else "public"
        return f"{self._prefix}/{scope}/{name}"

    def _get_prompt(self, prompt_name: str, *, version=None):
        from langfuse.api import NotFoundError

        try:
            return self._client.get_prompt(prompt_name, type="text", version=version, label=None if version is not None else self._label, cache_ttl_seconds=0)
        except NotFoundError:
            return None

    def _content(self, prompt, name: str) -> str:
        if prompt is None or prompt.config.get("deleted", False):
            raise FileNotFoundError(f"Skill '{name}' not found.")
        if not isinstance(prompt.prompt, str):
            raise ValueError("Skills must be Langfuse text prompts.")
        self.validate_skill_markdown_content(name, prompt.prompt)
        return prompt.prompt

    def _snapshot(self, category: SkillCategory, name: str, content: str) -> Path:
        from deerflow.skills.projection import skill_projection_read_lock

        target = self.get_custom_skill_file(name) if category == SkillCategory.CUSTOM else self._host_root / "public" / name / SKILL_MD_FILE
        # Projection rebuilds rediscover skills while holding the mutation lock.
        if target.is_file() and target.read_text(encoding="utf-8") == content:
            return target
        # Snapshot refresh must not recursively rebuild a projection/catalog.
        with skill_projection_read_lock(self):
            if target.is_file() and target.read_text(encoding="utf-8") == content:
                return target
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=target.parent, delete=False) as temp:
                temporary = Path(temp.name)
                temp.write(content)
            try:
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
        return target

    def _iter_skill_files(self):
        scopes = [(SkillCategory.PUBLIC, f"{self._prefix}/public/", self._host_root / "public")]
        if not self._public_only:
            scopes.append((SkillCategory.CUSTOM, f"{self._prefix}/custom/{self.user_id}/", self._user_custom_root))
        # Langfuse's name filter is exact, so paginate metadata and filter scopes
        # before fetching any bodies. Yield public first to preserve custom shadowing.
        names = []
        page = 1
        while True:
            response = self._client.api.prompts.list(label=self._label, page=page, limit=100)
            names.extend(entry.name for entry in response.data)
            if page >= response.meta.total_pages:
                break
            page += 1
        for category, prefix, root in scopes:
            for prompt_name in names:
                if not prompt_name.startswith(prefix):
                    continue
                name = prompt_name[len(prefix) :]
                self.validate_skill_name(name)
                prompt = self._get_prompt(prompt_name)
                if prompt is None or prompt.config.get("deleted", False):
                    continue
                content = self._content(prompt, name)
                yield category, root, self._snapshot(category, name, content)

    def read_skill_content(self, skill_file: Path) -> str:
        resolved = self.validate_skill_file_path(skill_file)
        for category, root in ((SkillCategory.PUBLIC, self._host_root / "public"), (SkillCategory.CUSTOM, self._user_custom_root)):
            if category == SkillCategory.CUSTOM and self._public_only:
                continue
            try:
                relative = resolved.relative_to(root.resolve())
            except ValueError:
                continue
            if len(relative.parts) == 2 and relative.name == SKILL_MD_FILE:
                name = self.validate_skill_name(relative.parts[0])
                return self._content(self._get_prompt(self._prompt_name(category, name)), name)
        raise ValueError("Skill path must identify a registered text skill.")

    def read_custom_skill(self, name: str) -> str:
        return self._content(self._get_prompt(self._prompt_name(SkillCategory.CUSTOM, name)), name)

    def custom_skill_exists(self, name: str) -> bool:
        prompt = self._get_prompt(self._prompt_name(SkillCategory.CUSTOM, name))
        return prompt is not None and not prompt.config.get("deleted", False)

    def public_skill_exists(self, name: str) -> bool:
        prompt = self._get_prompt(self._prompt_name(SkillCategory.PUBLIC, name))
        return prompt is not None and not prompt.config.get("deleted", False)

    def ensure_custom_skill_is_editable(self, name: str) -> None:
        if not self.custom_skill_exists(name):
            raise FileNotFoundError(f"Custom skill '{name}' not found.")

    def save_custom_skill(self, name: str, content: str, *, history_meta: dict | None = None) -> None:
        self.validate_skill_markdown_content(name, content)
        self._publish(name, content, history_meta or {"action": "edit", "new_content": content})
        self._snapshot(SkillCategory.CUSTOM, name, content)

    def _publish(self, name: str, content: str, history: dict, *, deleted=False) -> None:
        if self._public_only:
            raise ValueError("Custom skills require a user-scoped storage.")
        # ponytail: concurrent edits use last-label-update wins; add conflict detection if multi-editor workflows require it.
        self._client.create_prompt(
            name=self._prompt_name(SkillCategory.CUSTOM, name),
            type="text",
            prompt=content,
            labels=[self._label],
            config={"deleted": deleted, "history": {"ts": datetime.now(UTC).isoformat(), **history}},
        )

    def write_custom_skill(self, name: str, relative_path: str, content: str) -> None:
        if relative_path != SKILL_MD_FILE:
            raise ValueError("Text skills support only SKILL.md; scripts and supporting files are not allowed.")
        self.save_custom_skill(name, content)

    def ensure_safe_support_path(self, *args, **kwargs):
        raise ValueError("Text skills support only SKILL.md; scripts and supporting files are not allowed.")

    def delete_custom_skill(self, name: str, *, history_meta: dict | None = None) -> None:
        previous = self.read_custom_skill(name)
        self._publish(name, previous, {**(history_meta or {}), "action": "delete", "prev_content": previous, "new_content": None}, deleted=True)
        # Keep the immutable remote history; remove only the local snapshot.
        from deerflow.skills.projection import skill_projection_read_lock

        with skill_projection_read_lock(self):
            self.get_custom_skill_file(name).unlink(missing_ok=True)

    def append_history(self, name: str, record: dict) -> None:
        # Rejected rollbacks record the attempt without publishing rejected text.
        prompt = self._get_prompt(self._prompt_name(SkillCategory.CUSTOM, name))
        self._publish(name, self._content(prompt, name), record)

    def custom_skill_history_exists(self, name: str) -> bool:
        return self._get_prompt(self._prompt_name(SkillCategory.CUSTOM, name)) is not None

    def read_history(self, name: str) -> list[dict]:
        prompt_name = self._prompt_name(SkillCategory.CUSTOM, name)
        # Fetch immutable version IDs; versions may have gaps after operator deletions.
        response = self._client.api.prompts.list(name=prompt_name, limit=100)
        entry = next((entry for entry in response.data if entry.name == prompt_name), None)
        if entry is None:
            return []
        records = []
        previous = None
        for version in sorted(entry.versions):
            prompt = self._get_prompt(prompt_name, version=version)
            if prompt is None:
                continue
            record = prompt.config.get("history") or {"action": "langfuse_edit", "prev_content": previous, "new_content": prompt.prompt}
            records.append({**record, "version": version})
            previous = None if prompt.config.get("deleted", False) else prompt.prompt
        return records

    def prepare_custom_skill_export(self, name: str) -> None:
        self._snapshot(SkillCategory.CUSTOM, name, self.read_custom_skill(name))

    async def ainstall_skill_from_archive(self, archive_path: str | Path) -> dict:
        from deerflow.skills.installer import SkillAlreadyExistsError, _scan_skill_archive_contents_or_raise, resolve_skill_dir_from_archive, safe_extract_skill_archive, scan_archive_preflight_or_raise
        from deerflow.skills.validation import _validate_skill_frontmatter

        def prepare(tmp):
            path = Path(archive_path)
            if path.suffix != ".skill":
                raise ValueError("File must have .skill extension")
            scan_archive_preflight_or_raise(path, app_config=self._app_config)
            with zipfile.ZipFile(path) as archive:
                safe_extract_skill_archive(archive, Path(tmp))
            directory = resolve_skill_dir_from_archive(Path(tmp))
            if any(item.relative_to(directory).as_posix() != SKILL_MD_FILE for item in directory.rglob("*")):
                raise ValueError("Text skills support only SKILL.md; remove all scripts and supporting files.")
            valid, message, name = _validate_skill_frontmatter(directory)
            if not valid:
                raise ValueError(message)
            self.validate_skill_name(name)
            if self.custom_skill_exists(name):
                raise SkillAlreadyExistsError(f"Skill '{name}' already exists")
            return directory, name, (directory / SKILL_MD_FILE).read_text(encoding="utf-8")

        tmp = await asyncio.to_thread(tempfile.mkdtemp)
        try:
            directory, name, content = await asyncio.to_thread(prepare, tmp)
            await _scan_skill_archive_contents_or_raise(directory, name, app_config=self._app_config)
            await asyncio.to_thread(self.save_custom_skill, name, content, history_meta={"action": "install", "prev_content": None, "new_content": content})
        finally:
            await asyncio.wait_for(asyncio.to_thread(self._cleanup_install_tmp, tmp), timeout=5.0)
        return {"success": True, "skill_name": name, "message": f"Skill '{name}' installed successfully"}
