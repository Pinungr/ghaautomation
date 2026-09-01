"""Safe, deterministic maintenance of ``workflows_list.txt``."""

from __future__ import annotations

import re

from .config import Config
from .errors import E_WFLIST_SYNC, PromotionError

_WORKFLOWS_PREFIX = "workflows/"


def _relative_path(repository_path: str, cfg: Config) -> str:
    """Convert a repository workflow path to its list-file representation."""
    if not repository_path.startswith(_WORKFLOWS_PREFIX) or not cfg.is_workflow_path(
        repository_path
    ):
        raise PromotionError(
            E_WFLIST_SYNC,
            f"{cfg.workflows_list_file} cannot safely represent this workflow path.",
            details=[repository_path],
            remedy="Use repository workflow paths below workflows/.",
        )
    return repository_path[len(_WORKFLOWS_PREFIX) :]


def _normalize_entry(raw: str, cfg: Config) -> str | None:
    """Normalize one legacy/full or canonical/relative list entry."""
    entry = raw.strip()
    if not entry:
        return None
    entry = entry.replace("\\", "/")
    while entry.startswith("./"):
        entry = entry[2:]
    entry = re.sub(r"/+", "/", entry)
    if entry.startswith("/"):
        entry = entry.lstrip("/")
    if entry.startswith(_WORKFLOWS_PREFIX):
        return _relative_path(entry, cfg)

    # ``workflow/...`` was previously an unsafe near-miss for ``workflows/...``.
    # Continue to reject it rather than silently changing its meaning now that
    # stored entries are relative to the workflows directory.
    if entry.startswith("workflow/") or not cfg.is_workflow_path(
        _WORKFLOWS_PREFIX + entry
    ):
        raise PromotionError(
            E_WFLIST_SYNC,
            f"{cfg.workflows_list_file} contains an entry that cannot be safely normalized.",
            details=[raw],
            remedy="Use a path relative to workflows/, such as example.json or "
            "team/example.json.",
        )
    return entry


def desired_content(
    existing: str,
    required_workflow_paths: list[str],
    available_workflow_paths: list[str],
    cfg: Config,
) -> str | None:
    """Merge existing entries with workflows that exist in final staging content."""
    if not required_workflow_paths:
        return None
    ordered: list[str] = []
    seen: set[str] = set()
    available_entries = {
        _relative_path(repository_path, cfg)
        for repository_path in available_workflow_paths
    }
    for raw in existing.splitlines():
        path = _normalize_entry(raw, cfg)
        if path is not None and path in available_entries and path not in seen:
            seen.add(path)
            ordered.append(path)
    for repository_path in required_workflow_paths:
        path = _relative_path(repository_path, cfg)
        if path not in seen:
            seen.add(path)
            ordered.append(path)
    return "\n".join(ordered) + "\n"


def verify(actual: str, expected: str, cfg: Config) -> None:
    """Confirm the index holds the exact normalized merge."""
    if actual != expected:
        raise PromotionError(
            E_WFLIST_SYNC,
            f"{cfg.workflows_list_file} was not synchronized correctly.",
            details=["file content differs from the normalized expected content"],
            remedy="This indicates a bug in the promotion pipeline; no Pull "
            "Request was created and no protected branch was modified.",
        )
