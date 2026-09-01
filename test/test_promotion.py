"""End-to-end tests for user-created staging branch promotions."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from promotion.errors import (
    E_DUP_PATH,
    E_MISSING_SOURCE,
    E_PROMOTION_FILE_MISSING,
    E_PROTECTED_BRANCH,
    E_STAGING_SOURCE_MISMATCH,
    E_UNEXPECTED_CHANGE,
    PromotionError,
)
from promotion.gitops import Git
from promotion.__main__ import _summarise_success
from promotion.pr import RecordingBackend
from promotion.promote import promote


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode:
        raise AssertionError(
            f"git {' '.join(args)} failed:\n{completed.stdout}\n{completed.stderr}"
        )
    return completed.stdout.strip()


def _write(root: Path, relative_path: str, content: str) -> None:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _remote_sha(remote: Path, branch: str) -> str:
    return _git(remote, "rev-parse", f"refs/heads/{branch}")


def _remote_text(remote: Path, branch: str, path: str) -> str:
    return _git(remote, "show", f"{branch}:{path}")


def _remote_branches(remote: Path) -> set[str]:
    return set(_git(remote, "for-each-ref", "--format=%(refname:short)", "refs/heads").splitlines())


def _make_repository(
    tmp_path: Path,
    promotion_text: str | None,
    *,
    deployment_target: str = "PSUP",
    prepopulate_temporary_branch: bool = False,
    unexpected_temporary_file: bool = False,
    manual_files: dict[str, str] | None = None,
    manual_deletes: list[str] | None = None,
    staging_workflows_list: str | None = None,
) -> tuple[Path, Path]:
    """Create all three routes plus a temporary branch for ``deployment_target``."""
    remote = tmp_path / "remote.git"
    author = tmp_path / "author"
    runner = tmp_path / "runner"
    _git(tmp_path, "init", "--bare", str(remote))
    _git(tmp_path, "init", str(author))
    _git(author, "config", "user.name", "Promotion test")
    _git(author, "config", "user.email", "promotion-test@example.invalid")

    _write(
        author,
        "promotion.config.json",
        json.dumps(
            {
                "environments": {
                    "MASTER": {
                        "source": "dev_collaboration",
                        "target": "master",
                        "create_release_branch": False,
                    },
                    "PSUP": {
                        "source": "master",
                        "target": "psup",
                        "create_release_branch": True,
                    },
                    "PROD": {
                        "source": "psup",
                        "target": "prod",
                        "create_release_branch": True,
                    },
                },
                "protected_branches": ["dev_collaboration", "master", "psup", "prod"],
                "workflow_path_pattern": "workflows/**",
                "workflows_list_file": "workflows_list.txt",
            }
        ),
    )
    _write(author, "file1", "base version\n")
    _write(author, "file2", "base version\n")
    _write(author, "file3", "base version\n")
    _write(author, "workflows/old.json", '{"workflow": "old"}\n')
    _write(author, "workflows_list.txt", "old.json\n")
    _git(author, "add", ".")
    _git(author, "commit", "-m", "Base promotion content")
    _git(author, "branch", "-M", "master")
    _git(author, "remote", "add", "origin", str(remote))
    _git(author, "push", "-u", "origin", "master")

    _git(author, "checkout", "-b", "psup")
    _git(author, "push", "-u", "origin", "psup")
    _git(author, "checkout", "-b", "prod")
    _git(author, "push", "-u", "origin", "prod")

    _git(author, "checkout", "master")
    _write(author, "file1", "master version\n")
    _write(author, "file2", "master version\n")
    _write(author, "file3", "master version\n")
    _write(author, "workflows/new.json", '{"workflow": "new"}\n')
    _write(author, "workflows/pinaki/zyz.json", '{"workflow": "nested"}\n')
    _git(author, "add", ".")
    _git(author, "commit", "-m", "Master promotion content")
    _git(author, "push")

    _git(author, "checkout", "psup")
    _write(author, "file1", "psup version\n")
    _write(author, "workflows/psup.json", '{"workflow": "psup"}\n')
    _git(author, "add", ".")
    _git(author, "commit", "-m", "PSUP promotion content")
    _git(author, "push")

    _git(author, "checkout", "-b", "dev_collaboration", "master")
    _write(author, "file1", "dev version\n")
    _write(author, "file2", "dev version\n")
    _write(author, "file3", "dev version\n")
    _write(author, "workflows/dev.json", '{"workflow": "dev"}\n')
    _git(author, "add", ".")
    _git(author, "commit", "-m", "Development collaboration content")
    _git(author, "push", "-u", "origin", "dev_collaboration")

    targets = {"MASTER": "master", "PSUP": "psup", "PROD": "prod"}
    sources = {"MASTER": "dev", "PSUP": "master", "PROD": "psup"}
    _git(author, "checkout", "-b", "reltest_30_08_2026", targets[deployment_target])
    if promotion_text is not None:
        _write(author, "promotion.txt", promotion_text)
    if prepopulate_temporary_branch:
        _write(author, "file1", f"{sources[deployment_target]} version\n")
        _write(author, "file2", f"{sources[deployment_target]} version\n")
        _write(author, "file3", f"{sources[deployment_target]} version\n")
    if unexpected_temporary_file:
        _write(author, "unexpected.txt", "not approved\n")
    for path, content in (manual_files or {}).items():
        _write(author, path, content)
    for path in manual_deletes or []:
        (author / path).unlink()
    if staging_workflows_list is not None:
        _write(author, "workflows_list.txt", staging_workflows_list)
    if (promotion_text is not None or prepopulate_temporary_branch
            or unexpected_temporary_file or manual_files or manual_deletes
            or staging_workflows_list is not None):
        _git(author, "add", ".")
        _git(author, "commit", "-m", "Prepare temporary promotion branch")
    _git(author, "push", "-u", "origin", "reltest_30_08_2026")

    _git(tmp_path, "clone", "--branch", "reltest_30_08_2026", str(remote), str(runner))
    _git(runner, "config", "user.name", "Promotion test")
    _git(runner, "config", "user.email", "promotion-test@example.invalid")
    return remote, runner


def _run_promotion(
    runner: Path, deployment_target: str = "PSUP", messages: list[str] | None = None
) -> tuple[object, RecordingBackend]:
    backend = RecordingBackend()
    result = promote(
        repo_root=runner,
        deployment_target=deployment_target,
        staging_branch="reltest_30_08_2026",
        git=Git(runner),
        pr_backend=backend,
        now=datetime(2026, 8, 30, 10, 20, 30, tzinfo=timezone.utc),
        log=(messages.append if messages is not None else lambda _message: None),
    )
    return result, backend


def test_valid_promotion_txt_updates_the_existing_staging_branch_and_pr(tmp_path: Path) -> None:
    remote, runner = _make_repository(tmp_path, "file1\nfile2\nfile3\n")

    result, backend = _run_promotion(runner)

    assert result.staging_branch == "reltest_30_08_2026"
    assert result.source_branch == "master"
    assert result.release_branch == "release/30_08_2026_10_20_30_psup"
    assert result.pr is not None
    assert result.pr.head == "reltest_30_08_2026"
    assert result.pr.base == "release/30_08_2026_10_20_30_psup"
    assert len(backend.created) == 1
    assert _remote_text(remote, result.staging_branch, "file1") == "master version"
    assert _remote_text(remote, result.staging_branch, "file2") == "master version"
    assert _remote_text(remote, result.staging_branch, "file3") == "master version"
    assert _remote_text(remote, result.release_branch, "file1") == "psup version"
    assert _remote_branches(remote) == {
        "dev_collaboration",
        "master",
        "prod",
        "psup",
        "reltest_30_08_2026",
        "release/30_08_2026_10_20_30_psup",
    }


def test_prepopulated_temporary_branch_still_creates_release_pr(tmp_path: Path) -> None:
    remote, runner = _make_repository(
        tmp_path,
        "file1\nfile2\nfile3\n",
        prepopulate_temporary_branch=True,
    )
    before = _remote_sha(remote, "reltest_30_08_2026")

    result, backend = _run_promotion(runner)

    assert result.commit_sha == before
    assert _remote_sha(remote, result.staging_branch) == before
    assert result.pr is not None
    assert result.pr.head == "reltest_30_08_2026"
    assert result.pr.base == "release/30_08_2026_10_20_30_psup"
    assert len(backend.created) == 1
    assert result.release_branch in _remote_branches(remote)


def test_master_promotes_from_dev_collaboration_directly_to_master(tmp_path: Path) -> None:
    remote, runner = _make_repository(
        tmp_path, "file1\nfile2\nfile3\n", deployment_target="MASTER"
    )

    result, backend = _run_promotion(runner, "MASTER")

    assert result.source_branch == "dev_collaboration"
    assert result.target_branch == "master"
    assert result.release_branch is None
    assert result.pr is not None
    assert result.pr.head == "reltest_30_08_2026"
    assert result.pr.base == "master"
    assert len(backend.created) == 1
    assert _remote_text(remote, result.staging_branch, "file1") == "dev version"
    assert not any(branch.startswith("release/") for branch in _remote_branches(remote))
    assert "Release branch" not in result.pr.body


def test_master_workflow_promotion_rebuilds_workflows_list(tmp_path: Path) -> None:
    remote, runner = _make_repository(
        tmp_path, "workflows/dev.json\n", deployment_target="MASTER"
    )

    result, _ = _run_promotion(runner, "MASTER")

    assert _remote_text(remote, result.staging_branch, "workflows/dev.json") == '{"workflow": "dev"}'
    assert _remote_text(remote, result.staging_branch, "workflows_list.txt") == "old.json\ndev.json"
    assert result.release_branch is None


def test_master_delete_is_prepared_on_temporary_branch_without_release(tmp_path: Path) -> None:
    remote, runner = _make_repository(
        tmp_path, "DELETE|workflows/old.json\n", deployment_target="MASTER"
    )

    result, _ = _run_promotion(runner, "MASTER")

    missing = subprocess.run(
        ["git", "show", f"{result.staging_branch}:workflows/old.json"],
        cwd=remote,
        capture_output=True,
        text=True,
    )
    assert missing.returncode != 0
    assert result.pr is not None and result.pr.base == "master"
    assert result.release_branch is None


def test_master_preserves_additional_temporary_branch_changes_with_warning(tmp_path: Path) -> None:
    remote, runner = _make_repository(
        tmp_path,
        "file1\n",
        deployment_target="MASTER",
        unexpected_temporary_file=True,
    )
    messages: list[str] = []
    result, _ = _run_promotion(runner, "MASTER", messages)

    assert _remote_text(remote, result.staging_branch, "unexpected.txt") == "not approved"
    assert result.additional_staging_changes == [("A", "unexpected.txt")]
    assert any("WARNING: Additional staging changes" in message for message in messages)
    assert not any(branch.startswith("release/") for branch in _remote_branches(remote))


def test_master_cannot_be_used_as_temporary_branch(tmp_path: Path) -> None:
    _, runner = _make_repository(tmp_path, "file1\n", deployment_target="MASTER")

    with pytest.raises(PromotionError) as caught:
        promote(
            repo_root=runner,
            deployment_target="MASTER",
            staging_branch="master",
            git=Git(runner),
            pr_backend=RecordingBackend(),
        )

    assert caught.value.code == E_PROTECTED_BRANCH


def test_prod_retains_the_generated_release_branch_path(tmp_path: Path) -> None:
    remote, runner = _make_repository(
        tmp_path, "file1\n", deployment_target="PROD"
    )

    result, _ = _run_promotion(runner, "PROD")

    assert result.source_branch == "psup"
    assert result.target_branch == "prod"
    assert result.release_branch == "release/30_08_2026_10_20_30_prod"
    assert result.pr is not None
    assert result.pr.base == "release/30_08_2026_10_20_30_prod"
    assert _remote_text(remote, result.staging_branch, "file1") == "psup version"


def test_missing_promotion_txt_does_not_commit_push_or_create_a_pr(tmp_path: Path) -> None:
    remote, runner = _make_repository(tmp_path, None)
    before = _remote_sha(remote, "reltest_30_08_2026")
    backend = RecordingBackend()

    with pytest.raises(PromotionError) as caught:
        promote(
            repo_root=runner,
            deployment_target="PSUP",
            staging_branch="reltest_30_08_2026",
            git=Git(runner),
            pr_backend=backend,
        )

    assert caught.value.code == E_PROMOTION_FILE_MISSING
    assert "promotion.txt was not found in temporary branch 'reltest_30_08_2026'" in caught.value.message
    assert _remote_sha(remote, "reltest_30_08_2026") == before
    assert backend.created == []


def test_master_missing_source_file_fails_before_temporary_branch_is_modified(tmp_path: Path) -> None:
    remote, runner = _make_repository(
        tmp_path, "file1\ndoes/not/exist.txt\n", deployment_target="MASTER"
    )
    before = _remote_sha(remote, "reltest_30_08_2026")
    backend = RecordingBackend()

    with pytest.raises(PromotionError) as caught:
        promote(
            repo_root=runner,
            deployment_target="MASTER",
            staging_branch="reltest_30_08_2026",
            git=Git(runner),
            pr_backend=backend,
        )

    assert caught.value.code == E_MISSING_SOURCE
    assert _remote_sha(remote, "reltest_30_08_2026") == before
    assert backend.created == []


def test_duplicate_promotion_txt_entry_fails_before_the_staging_branch_is_modified(tmp_path: Path) -> None:
    remote, runner = _make_repository(tmp_path, "file1\nfile1\n")
    before = _remote_sha(remote, "reltest_30_08_2026")

    with pytest.raises(PromotionError) as caught:
        _run_promotion(runner)

    assert caught.value.code == E_DUP_PATH
    assert _remote_sha(remote, "reltest_30_08_2026") == before


def test_workflow_promotion_rebuilds_workflows_list_on_the_staging_branch(tmp_path: Path) -> None:
    remote, runner = _make_repository(tmp_path, "workflows/new.json\n")

    result, _ = _run_promotion(runner)

    assert _remote_text(remote, result.staging_branch, "workflows/new.json") == '{"workflow": "new"}'
    assert _remote_text(remote, result.staging_branch, "workflows_list.txt") == "old.json\nnew.json"


@pytest.mark.parametrize("target", ["PSUP", "PROD"])
def test_source_only_workflows_are_not_added_to_workflows_list(
    tmp_path: Path, target: str
) -> None:
    """Only promotion.txt and pre-existing staging changes may add entries."""
    remote, runner = _make_repository(
        tmp_path, "file1\n", deployment_target=target
    )

    result, _ = _run_promotion(runner, target)

    # MASTER has workflows/new.json and workflows/pinaki/zyz.json; PSUP has
    # workflows/psup.json.  Neither source-only workflow belongs in this run.
    assert _remote_text(remote, result.staging_branch, "workflows_list.txt") == "old.json"
    assert result.workflows_list_entries is None


def test_nested_workflow_is_stored_relative_to_workflows_directory(tmp_path: Path) -> None:
    remote, runner = _make_repository(tmp_path, "workflows/pinaki/zyz.json\n")

    result, _ = _run_promotion(runner)

    assert _remote_text(remote, result.staging_branch, "workflows_list.txt") == (
        "old.json\npinaki/zyz.json"
    )
    assert result.workflows_list_entries == ["old.json", "pinaki/zyz.json"]


def test_delete_from_promotion_txt_is_applied_to_the_existing_staging_branch(tmp_path: Path) -> None:
    remote, runner = _make_repository(tmp_path, "DELETE|workflows/old.json\n")

    result, _ = _run_promotion(runner)

    missing = subprocess.run(
        ["git", "show", f"{result.staging_branch}:workflows/old.json"],
        cwd=remote,
        capture_output=True,
        text=True,
    )
    assert missing.returncode != 0


@pytest.mark.parametrize("target", ["MASTER", "PSUP"])
def test_source_equivalent_manual_staging_change_is_preserved(tmp_path: Path, target: str) -> None:
    source_value = "dev version\n" if target == "MASTER" else "master version\n"
    remote, runner = _make_repository(
        tmp_path, "file1\n", deployment_target=target, manual_files={"file1": source_value}
    )
    before = _remote_sha(remote, "reltest_30_08_2026")

    result, _ = _run_promotion(runner, target)

    assert result.commit_sha == before
    assert _remote_sha(remote, result.staging_branch) == before


@pytest.mark.parametrize("target", ["MASTER", "PSUP"])
def test_manual_staging_mismatch_fails_before_push(tmp_path: Path, target: str) -> None:
    remote, runner = _make_repository(
        tmp_path, "file1\n", deployment_target=target, manual_files={"file1": "user version\n"}
    )
    before = _remote_sha(remote, "reltest_30_08_2026")

    with pytest.raises(PromotionError) as caught:
        _run_promotion(runner, target)

    assert caught.value.code == E_STAGING_SOURCE_MISMATCH
    assert _remote_sha(remote, "reltest_30_08_2026") == before


def test_additional_manual_deletion_is_preserved(tmp_path: Path) -> None:
    remote, runner = _make_repository(
        tmp_path, "file2\n", manual_deletes=["file1"]
    )
    result, _ = _run_promotion(runner)

    missing = subprocess.run(
        ["git", "show", f"{result.staging_branch}:file1"],
        cwd=remote,
        capture_output=True,
        text=True,
    )
    assert missing.returncode != 0
    assert ("D", "file1") in result.additional_staging_changes


def test_declared_manual_deletion_is_preserved(tmp_path: Path) -> None:
    remote, runner = _make_repository(
        tmp_path, "DELETE|workflows/old.json\n", manual_deletes=["workflows/old.json"]
    )

    result, _ = _run_promotion(runner)

    assert result.commit_sha == _remote_sha(remote, result.staging_branch)


def test_workflow_list_normalizes_and_deduplicates_existing_entries(tmp_path: Path) -> None:
    remote, runner = _make_repository(
        tmp_path,
        "workflows/new.json\n",
        staging_workflows_list=(
            " ./workflows/old.json \nworkflows\\old.json\n"
            "/workflows//old.json\nworkflows/psup.json\n"
        ),
    )

    result, _ = _run_promotion(runner)

    assert _remote_text(remote, result.staging_branch, "workflows_list.txt") == (
        "old.json\npsup.json\nnew.json"
    )


def test_invalid_workflow_list_path_is_not_reinterpreted(tmp_path: Path) -> None:
    _, runner = _make_repository(
        tmp_path, "workflows/new.json\n", staging_workflows_list="workflow/new.json\n"
    )

    with pytest.raises(PromotionError) as caught:
        _run_promotion(runner)

    assert caught.value.code == "E_WFLIST_SYNC"


def test_psup_preserves_additional_manual_application_file(tmp_path: Path) -> None:
    remote, runner = _make_repository(
        tmp_path, "file1\n", manual_files={"config/debug.yml": "debug: true\n"}
    )
    messages: list[str] = []
    result, _ = _run_promotion(runner, "PSUP", messages)

    assert _remote_text(remote, result.staging_branch, "config/debug.yml") == "debug: true"
    assert ("A", "config/debug.yml") in result.additional_staging_changes
    assert ("A", "config/debug.yml") in result.changes
    assert any("config/debug.yml" in message for message in messages)


def test_promotion_file_is_permitted_metadata(tmp_path: Path) -> None:
    _, runner = _make_repository(tmp_path, "file1\n", deployment_target="PSUP")

    result, _ = _run_promotion(runner, "PSUP")

    assert result.pr is not None


def test_unlisted_staging_workflow_is_preserved_and_listed_once(tmp_path: Path) -> None:
    remote, runner = _make_repository(
        tmp_path,
        "file2\n",
        manual_files={"workflows/manual.json": '{"workflow": "manual"}\n'},
        staging_workflows_list=(
            "old.json\n./workflows/manual.json\nworkflows//manual.json\n"
        ),
    )
    messages: list[str] = []

    result, _ = _run_promotion(runner, "PSUP", messages)

    assert _remote_text(remote, result.staging_branch, "workflows/manual.json") == (
        '{"workflow": "manual"}'
    )
    assert _remote_text(remote, result.staging_branch, "workflows_list.txt") == (
        "old.json\nmanual.json"
    )
    assert ("A", "workflows/manual.json") in result.additional_staging_changes
    assert any("workflows/manual.json" in message for message in messages)


def test_multiple_additional_staging_changes_are_in_pr_body_and_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, runner = _make_repository(
        tmp_path,
        "file2\n",
        manual_files={
            "config/manual.yml": "manual: true\n",
            "Notebooks/test.ipynb": "{}\n",
        },
    )
    result, _ = _run_promotion(runner)
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))

    _summarise_success(result)

    text = summary.read_text(encoding="utf-8")
    assert result.pr is not None
    assert "Additional staging changes (2)" in result.pr.body
    assert "config/manual.yml" in result.pr.body
    assert "Notebooks/test.ipynb" in result.pr.body
    assert "## Additional staging changes" in text
    assert "config/manual.yml" in text
    assert "Notebooks/test.ipynb" in text
