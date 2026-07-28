from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from .bridge_models import WorkspaceSnapshot


MAX_DIFF_CHARS = 200_000
MAX_FINGERPRINT_FILE_BYTES = 10_000_000


def capture_workspace(workspace: Path) -> WorkspaceSnapshot:
    inside = _git(workspace, "rev-parse", "--is-inside-work-tree")
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return WorkspaceSnapshot(is_git_repo=False)

    branch = _git(workspace, "branch", "--show-current").stdout.strip()
    head = _git(workspace, "rev-parse", "HEAD").stdout.strip()
    status = _git(workspace, "status", "--porcelain=v1", "--untracked-files=all").stdout
    unstaged = _git(workspace, "diff", "--no-ext-diff", "--").stdout
    staged = _git(workspace, "diff", "--cached", "--no-ext-diff", "--").stdout
    diff = f"# STAGED\n{staged}\n# UNSTAGED\n{unstaged}"
    if len(diff) > MAX_DIFF_CHARS:
        diff = f"{diff[:MAX_DIFF_CHARS]}\n… baseline diff truncated …"
    return WorkspaceSnapshot(
        is_git_repo=True,
        branch=branch,
        head=head,
        status=status.strip(),
        diff=diff.strip(),
    )


def format_snapshot(snapshot: WorkspaceSnapshot) -> str:
    if not snapshot.is_git_repo:
        return "当前工作区不是 Git 仓库，无法区分任务前后的 Git 改动。"
    status = snapshot.status or "clean"
    return (
        f"branch: {snapshot.branch or '(detached)'}\n"
        f"head: {snapshot.head or '(no commit)'}\n"
        f"status before task:\n{status}\n"
        "以下 baseline diff 在任务开始前已存在，不应归因于本次 Agent：\n"
        f"{snapshot.diff or '(empty)'}"
    )


def workspace_fingerprint(
    snapshot: WorkspaceSnapshot,
    workspace: Path | None = None,
) -> str:
    """Stable guard used to detect edits made after the last checkpoint."""
    payload = "\0".join(
        (
            "git" if snapshot.is_git_repo else "plain",
            snapshot.branch,
            snapshot.head,
            snapshot.status,
            snapshot.diff,
        )
    )
    digest = hashlib.sha256(payload.encode("utf-8", errors="replace"))
    if snapshot.is_git_repo and workspace is not None:
        untracked = _git(
            workspace,
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
        )
        if untracked.returncode == 0:
            for relative_text in sorted(
                item for item in untracked.stdout.split("\0") if item
            ):
                _update_file_fingerprint(
                    digest, workspace.resolve(), Path(relative_text)
                )
    elif workspace is not None:
        root = workspace.resolve()
        for path in sorted(root.rglob("*")):
            try:
                relative = path.relative_to(root)
                if any(part in {".git", ".mutiagent", "__pycache__"} for part in relative.parts):
                    continue
            except OSError:
                continue
            _update_file_fingerprint(digest, root, relative)
    return digest.hexdigest()


def _update_file_fingerprint(digest, root: Path, relative: Path) -> None:
    path = root / relative
    try:
        stat = path.stat()
    except OSError:
        return
    digest.update(str(relative).encode("utf-8", errors="replace"))
    digest.update(str(stat.st_size).encode("ascii"))
    if path.is_file() and stat.st_size <= MAX_FINGERPRINT_FILE_BYTES:
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(str(stat.st_mtime_ns).encode("ascii"))
    else:
        digest.update(str(stat.st_mtime_ns).encode("ascii"))


def _git(workspace: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(workspace),
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(["git", *args], 1, "", str(exc))
