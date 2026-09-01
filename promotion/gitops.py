"""Thin, auditable wrappers over the git CLI.

Only plumbing that the promotion needs, so that every git invocation the pipeline
makes is visible in one file. Nothing here targets a protected branch; the guard
in :mod:`promotion.guards` enforces that for pushes.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .errors import E_GIT, PromotionError

# git accepts long command lines, but pathological inventories should not be the
# thing that breaks a release.
_BATCH = 100


def _batched(items: list[str], size: int = _BATCH):
    for i in range(0, len(items), size):
        yield items[i : i + size]


@dataclass
class CompletedGit:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str


class Git:
    """Runs git in ``cwd`` against remote ``remote``."""

    def __init__(self, cwd: Path, remote: str = "origin") -> None:
        self.cwd = Path(cwd)
        self.remote = remote

    # -- core ---------------------------------------------------------------

    def run(self, *args: str, check: bool = True) -> CompletedGit:
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"
        env.setdefault("GIT_ADVICE", "0")
        proc = subprocess.run(  # noqa: S603 - fixed executable, no shell
            ["git", *args],
            cwd=str(self.cwd),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        result = CompletedGit(
            args=list(args),
            returncode=proc.returncode,
            stdout=(proc.stdout or "").strip(),
            stderr=(proc.stderr or "").strip(),
        )
        if check and result.returncode != 0:
            raise PromotionError(
                E_GIT,
                f"git {' '.join(args)} failed with exit code {result.returncode}.",
                details=[line for line in result.stderr.splitlines() if line.strip()],
            )
        return result

    def out(self, *args: str) -> str:
        return self.run(*args).stdout

    def run_bytes(self, *args: str) -> bytes:
        """Like :meth:`run` but returns stdout verbatim -- no decoding, no strip.

        Needed wherever a byte-exact comparison matters; :meth:`run` strips
        surrounding whitespace and would hide a trailing-newline mismatch.
        """
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"
        proc = subprocess.run(  # noqa: S603 - fixed executable, no shell
            ["git", *args],
            cwd=str(self.cwd),
            env=env,
            capture_output=True,
        )
        if proc.returncode != 0:
            raise PromotionError(
                E_GIT,
                f"git {' '.join(args)} failed with exit code {proc.returncode}.",
                details=[
                    line
                    for line in (proc.stderr or b"")
                    .decode("utf-8", "replace")
                    .splitlines()
                    if line.strip()
                ],
            )
        return proc.stdout or b""

    def read_index_text(self, path: str) -> str:
        """The exact staged content of ``path``, as it will be committed."""
        return self.run_bytes("show", f":{path}").decode("utf-8")

    def read_file_text(self, rev: str, path: str) -> str:
        """Read a UTF-8 file from ``rev`` without checking it out."""
        return self.run_bytes("show", f"{rev}:{path}").decode("utf-8")

    def read_file_bytes(self, rev: str, path: str) -> bytes:
        """Read a file blob from ``rev`` without decoding it."""
        return self.run_bytes("show", f"{rev}:{path}")

    # -- inspection ---------------------------------------------------------

    def fetch(self) -> None:
        self.run("fetch", "--no-tags", "--prune", self.remote)

    def remote_heads(self) -> dict[str, str]:
        """Map branch name -> sha for every head on the remote."""
        heads: dict[str, str] = {}
        for line in self.out("ls-remote", "--heads", self.remote).splitlines():
            sha, _, ref = line.partition("\t")
            if ref.startswith("refs/heads/"):
                heads[ref[len("refs/heads/") :]] = sha.strip()
        return heads

    def remote_branch_sha(self, branch: str) -> str:
        """Resolve the remote-tracking ref for ``branch`` to a commit sha."""
        return self.out("rev-parse", "--verify", f"refs/remotes/{self.remote}/{branch}")

    def object_type(self, rev: str, path: str) -> str | None:
        """``blob``/``tree``/... for ``rev:path``, or ``None`` when absent."""
        result = self.run("cat-file", "-t", f"{rev}:{path}", check=False)
        if result.returncode != 0:
            return None
        return result.stdout or None

    def current_branch(self) -> str:
        return self.out("rev-parse", "--abbrev-ref", "HEAD")

    def head_sha(self) -> str:
        return self.out("rev-parse", "HEAD")

    def staged_changes(self) -> list[tuple[str, str]]:
        """``(status, path)`` for everything staged relative to HEAD."""
        return self._name_status("diff", "--cached", "--name-status", "-z", "HEAD")

    def changes_between(self, base: str, head: str = "HEAD") -> list[tuple[str, str]]:
        """``(status, path)`` for the change set from ``base`` to ``head``."""
        return self._name_status(
            "diff", "--no-renames", "--name-status", "-z", base, head
        )

    def working_changes_from(self, base: str) -> list[tuple[str, str]]:
        """Changes from ``base`` represented by the current index/worktree."""
        return self._name_status(
            "diff", "--no-renames", "--name-status", "-z", base
        )

    def index_paths(self) -> list[str]:
        """All repository paths currently represented by the index."""
        return [
            path
            for path in self.run_bytes("ls-files", "-z").decode("utf-8").split("\0")
            if path
        ]

    def _name_status(self, *args: str) -> list[tuple[str, str]]:
        """Parse NUL-delimited output from ``git diff --name-status``."""
        raw = self.out(*args)
        if not raw:
            return []
        fields = [f for f in raw.split("\0") if f]
        changes: list[tuple[str, str]] = []
        i = 0
        while i < len(fields):
            status = fields[i]
            # Renames/copies carry two paths; the promotion never generates them,
            # but reporting both keeps the unexpected-change guard honest.
            if status[:1] in {"R", "C"} and i + 2 < len(fields):
                changes.append((status, fields[i + 2]))
                i += 3
            else:
                changes.append((status, fields[i + 1]))
                i += 2
        return changes

    # -- mutation (local only) ---------------------------------------------

    def checkout_existing_branch(self, name: str, start_sha: str) -> None:
        """Check out a local branch at an already-validated remote commit."""
        self.run("checkout", "-q", "-B", name, start_sha)

    def checkout_paths_from(self, rev: str, paths: list[str]) -> None:
        for chunk in _batched(paths):
            self.run("checkout", rev, "--", *chunk)

    def remove_paths(self, paths: list[str]) -> None:
        for chunk in _batched(paths):
            self.run("rm", "--quiet", "--", *chunk)

    def add_paths(self, paths: list[str]) -> None:
        for chunk in _batched(paths):
            self.run("add", "--", *chunk)

    def commit(self, message: str, author: str | None = None) -> str:
        args = ["commit", "--quiet", "-m", message]
        if author:
            args += ["--author", author]
        self.run(*args)
        return self.head_sha()

    # -- remote mutation ---------------------------------------------------

    def push_existing_branch(self, branch: str) -> None:
        """Push the checked-out temporary branch without force-overwriting it."""
        self.run("push", self.remote, f"HEAD:refs/heads/{branch}")

    def push_new_branch(self, sha: str, branch: str) -> None:
        """Create ``branch`` at ``sha``, failing atomically if it already exists."""
        self.run(
            "push",
            f"--force-with-lease=refs/heads/{branch}:",
            self.remote,
            f"{sha}:refs/heads/{branch}",
        )
