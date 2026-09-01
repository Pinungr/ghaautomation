"""Promotion orchestration -- the BRD section 6 end-to-end sequence.

The user creates the temporary branch before dispatch. The promotion inventory
is read from that branch, requested files are read from ``origin/<source>``,
and the supplied branch is updated. A generated release branch is created from
the configured target and becomes the Pull Request base.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol

from . import config as config_mod
from . import guards, inventory as inventory_mod, workflows_list
from .config import Config, Environment
from .errors import (
    E_BAD_DELETE,
    E_BRANCH_EXISTS,
    E_BRANCH_MISSING,
    E_GIT,
    E_MISSING_SOURCE,
    E_NO_STAGING_BRANCH,
    E_NOT_A_FILE,
    E_PROMOTION_FILE_MISSING,
    PromotionError,
)
from .gitops import Git
from .inventory import Inventory
from .pr import GhCliBackend, PullRequest, RecordingBackend, render_body, render_title

TIMESTAMP_FORMAT = "%d_%m_%Y_%H_%M_%S"
PROMOTION_FILENAME = "promotion.txt"


class PrBackend(Protocol):
    def create(self, pr: PullRequest) -> str: ...


@dataclass(frozen=True)
class PromotionResult:
    environment: str
    source_branch: str
    target_branch: str
    staging_branch: str
    release_branch: str | None
    base_sha: str
    timestamp: str
    commit_sha: str | None
    changes: list[tuple[str, str]] = field(default_factory=list)
    workflows_list_entries: list[str] | None = None
    additional_staging_changes: list[tuple[str, str]] = field(default_factory=list)
    pr_url: str = ""
    pr: PullRequest | None = None
    dry_run: bool = False


def make_timestamp(
    now: datetime | None = None, tz: timezone = timezone.utc
) -> str:
    """Audit identifier, ``DD_MM_YYYY_HH_MM_SS`` (BRD sections 5 and 16).

    Stamped in ``tz`` so PR titles and audit records read against the wall clock
    of whoever dispatched the run. Runners are UTC, so without this the value
    trails local time by the UTC offset and looks stale.
    """
    moment = now or datetime.now(tz)
    return moment.astimezone(tz).strftime(TIMESTAMP_FORMAT)


def _preflight_paths(
    git: Git,
    inv: Inventory,
    env: Environment,
    base_sha: str,
    staging_branch: str,
) -> None:
    """Validate every requested path against the repository (section 15).

    Each category reports *all* offending paths, so one re-run can fix them all.
    """
    source_rev = f"refs/remotes/{git.remote}/{env.source}"

    missing: list[str] = []
    not_a_file: list[str] = []
    for entry in inv.promotes:
        kind = git.object_type(source_rev, entry.path)
        if kind is None:
            missing.append(f"{entry.location}: {entry.path}")
        elif kind != "blob":
            not_a_file.append(f"{entry.location}: {entry.path} (is a {kind})")

    if missing:
        raise PromotionError(
            E_MISSING_SOURCE,
            f"{len(missing)} requested file(s) do not exist on the source branch "
            f"'{env.source}'.",
            details=missing,
            remedy=f"Confirm the paths exist on '{env.source}' and are spelled "
            f"exactly as in the repository, then re-run.",
        )
    if not_a_file:
        raise PromotionError(
            E_NOT_A_FILE,
            f"{len(not_a_file)} requested path(s) are not files.",
            details=not_a_file,
            remedy="List individual file paths. Directories are not promoted as "
            "a unit.",
        )

    bad_deletes: list[str] = []
    for entry in inv.deletes:
        kind = git.object_type(base_sha, entry.path)
        if kind is None:
            bad_deletes.append(
                f"{entry.location}: {entry.path} (not present on "
                f"the target baseline for temporary branch '{staging_branch}')"
            )
        elif kind != "blob":
            bad_deletes.append(
                f"{entry.location}: {entry.path} (is a {kind} on "
                f"the target baseline for temporary branch '{staging_branch}')"
            )

    if bad_deletes:
        raise PromotionError(
            E_BAD_DELETE,
            f"{len(bad_deletes)} DELETE path(s) cannot be deleted.",
            details=bad_deletes,
            remedy="A DELETE path must be an existing file on the target baseline "
            f"for temporary branch '{staging_branch}'.",
        )


def promote(
    *,
    repo_root: Path,
    deployment_target: str | None,
    staging_branch: str | None,
    release_description: str | None = None,
    cfg: Config | None = None,
    git: Git | None = None,
    pr_backend: PrBackend | None = None,
    now: datetime | None = None,
    run_url: str | None = None,
    dry_run: bool = False,
    log: Callable[[str], None] = lambda _msg: None,
) -> PromotionResult:
    repo_root = Path(repo_root)
    cfg = cfg or config_mod.load(repo_root)
    git = git or Git(repo_root)
    backend: PrBackend = pr_backend or (
        RecordingBackend() if dry_run else GhCliBackend(cwd=repo_root)
    )

    # 1. Resolve the route and validate the supplied branch name.
    env = cfg.resolve(deployment_target)
    if not staging_branch or not staging_branch.strip():
        raise PromotionError(
            E_NO_STAGING_BRANCH,
            "No temporary branch was supplied.",
            remedy="Re-run the workflow and select the user-created temporary branch "
            "that contains promotion.txt.",
        )
    staging_branch = config_mod.validate_branch_name(
        staging_branch.strip(), "staging_branch"
    )

    # 2. Refresh remote refs.
    git.fetch()

    dirty = git.out("status", "--porcelain", "--untracked-files=no")
    if dirty:
        raise PromotionError(
            E_GIT,
            "The checkout has uncommitted changes; refusing to build a "
            "promotion on top of them.",
            details=dirty.splitlines(),
        )

    # 3. The configured route and supplied temporary branch must exist remotely.
    heads = git.remote_heads()
    absent = [b for b in (env.source, env.target, staging_branch) if b not in heads]
    if absent:
        raise PromotionError(
            E_BRANCH_MISSING,
            f"Required branch(es) do not exist on the remote: "
            f"{', '.join(absent)}.",
            details=[
                f"{b} ({'temporary branch input' if b == staging_branch else f'environments.{env.name}'})"
                for b in absent
            ],
            remedy="Create the user-supplied temporary branch or correct "
            f"{config_mod.CONFIG_FILENAME}.",
        )

    # 4. Capture the release baseline and validate the supplied temporary branch.
    guards.assert_push_allowed(staging_branch, cfg)
    staging_rev = f"refs/remotes/{git.remote}/{staging_branch}"
    staging_sha = git.remote_branch_sha(staging_branch)
    base_sha = git.remote_branch_sha(env.target)
    log(f"Temporary branch: {staging_branch} @ {staging_sha}")
    log(f"Release baseline: {env.target} @ {base_sha}")

    # 5. Read and validate the root inventory before changing the checkout.
    if git.object_type(staging_rev, PROMOTION_FILENAME) != "blob":
        raise PromotionError(
            E_PROMOTION_FILE_MISSING,
            f"{PROMOTION_FILENAME} was not found in temporary branch '{staging_branch}'.",
            remedy=f"Add {PROMOTION_FILENAME} at the repository root of "
            f"'{staging_branch}', commit it, and re-run.",
        )
    inv = inventory_mod.parse(git.read_file_text(staging_rev, PROMOTION_FILENAME), cfg)
    log(
        f"Promoting {len(inv.entries)} path(s) to {env.name}: "
        f"{env.source} -> {env.target} through {staging_branch} "
        f"({len(inv.promotes)} to promote, {len(inv.deletes)} to delete)"
    )

    # 6. Select the PR base strategy. MASTER routes directly to its configured
    # target; PSUP and PROD retain the generated release-branch workflow.
    timestamp = make_timestamp(now, cfg.timestamp_tz)
    release_branch: str | None = None
    pr_base = env.target
    if env.create_release_branch:
        release_branch = f"release/{timestamp}_{env.slug}"
        if release_branch in heads:
            raise PromotionError(
                E_BRANCH_EXISTS,
                f"The generated release branch '{release_branch}' already exists.",
                remedy="Re-run the workflow; a fresh release branch name will be "
                "generated from the new execution timestamp.",
            )
        guards.assert_push_allowed(release_branch, cfg)
        pr_base = release_branch
        log(f"Release branch: {release_branch}")
    else:
        log(f"PR target: {pr_base} (no release branch for {env.name})")

    # 7. Repository-level validation, still before any write.
    _preflight_paths(git, inv, env, base_sha, staging_branch)
    staging_changes = git.changes_between(base_sha, staging_sha)
    preserved_promotes, preserved_deletes, additional_staging_changes = guards.validate_staging_changes(
        git=git,
        changes=staging_changes,
        inventory=inv,
        cfg=cfg,
        source_rev=f"refs/remotes/{git.remote}/{env.source}",
        staging_rev=staging_rev,
        metadata_paths={PROMOTION_FILENAME, cfg.workflows_list_file},
    )
    if additional_staging_changes:
        log("WARNING: Additional staging changes detected that are not listed in promotion.txt.")
        log("WARNING: They will be preserved and included in the Pull Request for reviewer validation:")
        for _status, path in additional_staging_changes:
            log(f"::warning::Additional staging file not listed in promotion.txt: {path}")

    # 8. Check out the existing temporary branch at the fetched remote commit.
    git.checkout_existing_branch(staging_branch, staging_sha)

    # 9. Apply source files to the same temporary branch.
    paths_to_copy = [path for path in inv.promote_paths if path not in preserved_promotes]
    paths_to_delete = [path for path in inv.delete_paths if path not in preserved_deletes]
    if paths_to_copy:
        git.checkout_paths_from(f"refs/remotes/{git.remote}/{env.source}", paths_to_copy)
        log(f"Applied {len(paths_to_copy)} file(s) from {env.source}")
    if paths_to_delete:
        git.remove_paths(paths_to_delete)
        log(f"Deleted {len(paths_to_delete)} file(s)")

    # 10. Maintain the workflow list from explicitly promoted workflows and
    # workflows that already differ on the staging branch.  Do not infer entries
    # from the source branch: it can contain unrelated workflows that are not
    # part of this promotion.
    intended_changes = git.working_changes_from(base_sha)
    final_non_deleted_paths = {
        path for status, path in intended_changes if status[:1] != "D"
    }
    staging_workflow_paths = [
        path
        for status, path in staging_changes
        if status[:1] != "D" and cfg.is_workflow_path(path)
    ]
    allowed_workflow_paths = [
        *inv.workflow_promote_paths,
        *staging_workflow_paths,
    ]
    required_workflow_paths = [
        path
        for path in dict.fromkeys(allowed_workflow_paths)
        if path in final_non_deleted_paths
    ]
    existing_list = ""
    if git.object_type("HEAD", cfg.workflows_list_file) == "blob":
        existing_list = git.read_index_text(cfg.workflows_list_file)
    available_workflow_paths = [
        path for path in git.index_paths() if cfg.is_workflow_path(path)
    ]
    desired = workflows_list.desired_content(
        existing_list,
        required_workflow_paths,
        available_workflow_paths,
        cfg,
    )
    list_entries: list[str] | None = None
    if desired is None:
        log(
            f"{cfg.workflows_list_file}: unchanged (no workflow paths in the final PR)"
        )
    else:
        list_path = repo_root / cfg.workflows_list_file
        list_path.parent.mkdir(parents=True, exist_ok=True)
        list_path.write_text(desired, encoding="utf-8", newline="\n")
        git.add_paths([cfg.workflows_list_file])
        # Verify what will actually be committed, not the working-tree bytes:
        # on Windows checkouts autocrlf can make those differ.
        workflows_list.verify(git.read_index_text(cfg.workflows_list_file), desired, cfg)
        list_entries = desired.splitlines()
        log(f"{cfg.workflows_list_file}: synchronized with {len(list_entries)} entry(ies)")

    # 11. Nothing outside the requested set may have changed (sections 6, 20).
    staged_changes = git.staged_changes()
    guards.assert_changes_expected(
        staged_changes,
        inv.all_paths,
        cfg,
        allow_workflows_list=desired is not None,
        require_changes=False,
    )

    # 12. Commit temporary-branch changes when required. A user-created branch
    # may already contain the exact approved source files; it can still be
    # promoted if its full diff against the release baseline is safe.
    if staged_changes:
        commit_message = "\n".join(
            [
                f"Promote {len(inv.entries)} path(s) to {env.name} [{timestamp}]",
                "",
                f"Source branch: {env.source}",
                f"Target branch: {env.target}",
                f"Temporary branch: {staging_branch}",
                f"Release baseline commit: {base_sha}",
                f"Inventory: {PROMOTION_FILENAME}",
            ]
        )
        commit_sha = git.commit(commit_message)
        log(f"Committed {commit_sha} with {len(staged_changes)} change(s)")
    else:
        commit_sha = git.head_sha()
        log("Temporary branch already contains the requested source files")

    # 13. The PR may carry preserved user staging changes, but the automation
    # itself may only create requested paths and its known metadata files.
    changes = git.changes_between(base_sha, commit_sha)
    guards.assert_changes_expected(
        changes,
        inv.all_paths,
        cfg,
        allow_workflows_list=(
            desired is not None
            or any(path == cfg.workflows_list_file for _status, path in staging_changes)
        ),
        additional_allowed_paths=[
            PROMOTION_FILENAME,
            *(path for _status, path in additional_staging_changes),
        ],
    )

    pull = PullRequest(
        title=render_title(env.name, timestamp),
        body=render_body(
            env_name=env.name,
            source_branch=env.source,
            target_branch=env.target,
            base_sha=base_sha,
            timestamp=timestamp,
            staging_branch=staging_branch,
            release_branch=release_branch,
            changes=changes,
            requested_promotes=inv.promote_paths,
            requested_deletes=inv.delete_paths,
            workflows_list_file=cfg.workflows_list_file,
            workflows_list_entries=list_entries,
            additional_staging_changes=additional_staging_changes,
            release_description=release_description,
            run_url=run_url,
        ),
        base=pr_base,
        head=staging_branch,
    )

    result_kwargs = dict(
        environment=env.name,
        source_branch=env.source,
        target_branch=env.target,
        staging_branch=staging_branch,
        release_branch=release_branch,
        base_sha=base_sha,
        timestamp=timestamp,
        commit_sha=commit_sha,
        changes=changes,
        workflows_list_entries=list_entries,
        additional_staging_changes=additional_staging_changes,
        pr=pull,
    )

    if dry_run:
        log("Dry run: stopping before any push.")
        return PromotionResult(**result_kwargs, pr_url="", dry_run=True)

    # 14. Publish the temporary branch and, where configured, its release base.
    git.push_existing_branch(staging_branch)
    log(f"Pushed {staging_branch}")
    if release_branch is not None:
        git.push_new_branch(base_sha, release_branch)
        log(f"Created {release_branch} from {env.target}")

    pr_url = backend.create(pull)
    log(f"Pull Request: {pr_url}")

    return PromotionResult(**result_kwargs, pr_url=pr_url)
