"""Safety assertions that back the BRD's non-negotiable invariants.

Section 14: the automation must never push to master, psup or prod.
Sections 6 and 20: the change set must contain nothing the user did not request,
beyond the one permitted ``workflows_list.txt`` rebuild.
"""

from __future__ import annotations

from .config import Config
from .errors import (
    E_NO_CHANGES,
    E_PROTECTED_BRANCH,
    E_STAGING_SOURCE_MISMATCH,
    E_UNEXPECTED_CHANGE,
    PromotionError,
)
from .inventory import DELETE, Inventory


def assert_push_allowed(branch: str, cfg: Config) -> None:
    """Refuse to push to a source-of-truth branch."""
    if cfg.is_protected(branch):
        raise PromotionError(
            E_PROTECTED_BRANCH,
            f"Refusing to push to protected branch {branch!r}.",
            details=[
                "The pipeline only ever pushes the user-supplied temporary branch "
                "or a generated release branch.",
            ],
            remedy="Select a non-protected temporary branch and check "
            "'protected_branches' in promotion.config.json.",
        )


def assert_changes_expected(
    changes: list[tuple[str, str]],
    requested_paths: list[str],
    cfg: Config,
    allow_workflows_list: bool,
    *,
    additional_allowed_paths: list[str] | None = None,
    require_changes: bool = True,
) -> None:
    """Verify the staged change set is exactly within the permitted path set."""
    if not changes:
        if not require_changes:
            return
        raise PromotionError(
            E_NO_CHANGES,
            "The temporary branch produces no change against the release baseline.",
            details=[
                "Every requested file is already identical on the release baseline.",
            ],
            remedy="Confirm the temporary branch contains the intended promotion "
            "changes, then re-run.",
        )

    allowed = set(requested_paths)
    allowed.update(additional_allowed_paths or [])
    if allow_workflows_list:
        allowed.add(cfg.workflows_list_file)
    unexpected = sorted({path for _, path in changes if path not in allowed})
    if unexpected:
        raise PromotionError(
            E_UNEXPECTED_CHANGE,
            f"{len(unexpected)} file(s) changed that were not requested.",
            details=unexpected,
            remedy="No Pull Request was created and no protected branch was modified.",
        )


def validate_staging_changes(
    *,
    git: object,
    changes: list[tuple[str, str]],
    inventory: Inventory,
    cfg: Config,
    source_rev: str,
    staging_rev: str,
    metadata_paths: set[str],
) -> tuple[set[str], set[str], list[tuple[str, str]]]:
    """Authorize pre-existing temporary-branch changes before any checkout.

    A listed path is admissible only when it is byte-for-byte the approved
    source version (or is a declared deletion). An unlisted workflow is also
    allowed only when it already exists with identical content on the approved
    source branch. Metadata is deliberately excluded from this comparison.
    ``git`` is structural here to avoid coupling guards to the command wrapper.
    """
    entry_by_path = {entry.path: entry for entry in inventory.entries}
    additional_changes = sorted(
        (status, path)
        for status, path in changes
        if path not in metadata_paths and path not in entry_by_path
    )

    mismatches: list[str] = []
    for status, path in additional_changes:
        if not cfg.is_workflow_path(path):
            continue
        if status[:1] == "D":
            mismatches.append(
                f"{path}: staging workflow deletion must be declared in promotion.txt"
            )
            continue
        source_kind = git.object_type(source_rev, path)
        staging_kind = git.object_type(staging_rev, path)
        if source_kind != "blob" or staging_kind != "blob":
            mismatches.append(
                f"{path}: staging workflow does not exist as a file in both the "
                "temporary and approved source branches"
            )
            continue
        if git.read_file_bytes(staging_rev, path) != git.read_file_bytes(source_rev, path):
            mismatches.append(
                f"{path}: staging workflow content does not match approved source "
                f"'{source_rev.rsplit('/', 1)[-1]}'"
            )

    manually_prepared_promotes: set[str] = set()
    manually_prepared_deletes: set[str] = set()
    for status, path in changes:
        if path in metadata_paths or path not in entry_by_path:
            continue
        entry = entry_by_path[path]
        if entry.action == DELETE:
            if status[:1] != "D":
                mismatches.append(
                    f"{path}: declared DELETE but is not deleted in the temporary branch"
                )
            else:
                manually_prepared_deletes.add(path)
            continue

        if status[:1] == "D":
            mismatches.append(
                f"{path}: declared for promotion but is deleted in the temporary branch"
            )
            continue

        source_kind = git.object_type(source_rev, path)
        staging_kind = git.object_type(staging_rev, path)
        if source_kind != "blob" or staging_kind != "blob":
            mismatches.append(
                f"{path}: does not exist as a file in both the temporary and "
                "approved source branches"
            )
            continue
        if git.read_file_bytes(staging_rev, path) != git.read_file_bytes(source_rev, path):
            mismatches.append(
                f"{path}: temporary-branch content does not match approved source "
                f"'{source_rev.rsplit('/', 1)[-1]}'"
            )
            continue
        manually_prepared_promotes.add(path)

    if mismatches:
        raise PromotionError(
            E_STAGING_SOURCE_MISMATCH,
            "Validation failed: manually prepared temporary-branch files do not "
            "match the approved promotion source.",
            details=mismatches,
            remedy="Reset the listed files to the approved source content, or "
            "remove them from promotion.txt and the temporary branch.",
        )
    return manually_prepared_promotes, manually_prepared_deletes, additional_changes
