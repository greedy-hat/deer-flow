"""Backend text reads share the agent's trusted discovery allowlist."""

from contextvars import ContextVar

available_skill_names: ContextVar[set[str] | None] = ContextVar("available_skill_names", default=None)


def backend_skills_enabled() -> bool:
    from deerflow.config import get_app_config
    from deerflow.reflection import resolve_class
    from deerflow.skills.storage.skill_storage import SkillStorage

    try:
        cls = resolve_class(get_app_config().skills.use, SkillStorage)
        return getattr(cls, "text_only", False) is True
    except (FileNotFoundError, ValueError, AttributeError, ImportError):
        return False


def is_backend_skill_path(path: str) -> bool:
    from deerflow.config import get_app_config

    if not backend_skills_enabled():
        return False
    config = get_app_config().skills
    root = config.container_path.rstrip("/")
    return path == root or path.startswith(root + "/")


def read_backend_skill(runtime, path: str) -> str:
    import posixpath

    from deerflow.runtime.user_context import resolve_runtime_user_id
    from deerflow.skills.container_registry import build_container_path_registry
    from deerflow.skills.storage import get_or_new_user_skill_storage

    storage = get_or_new_user_skill_storage(resolve_runtime_user_id(runtime))
    skill = build_container_path_registry(storage).get(path)
    allowed = available_skill_names.get()
    if posixpath.normpath(path) != path or skill is None or not skill.enabled or (allowed is not None and skill.name not in allowed):
        raise PermissionError("Skill is unavailable for this agent.")
    return storage.read_skill_content(skill.skill_file)
