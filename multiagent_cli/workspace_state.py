from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .bridge_models import WorkspaceSnapshot


MAX_DIFF_CHARS = 200_000
MAX_FINGERPRINT_FILE_BYTES = 10_000_000
FINGERPRINT_PROTOCOL = "v2"
MAX_CHANGED_FILES = 500
MAX_FILE_PATCH_CHARS = 80_000
MAX_TOTAL_PATCH_CHARS = 500_000
_GIT_OBJECT_RE = re.compile(r"^[0-9a-f]{40,64}$")


@dataclass(frozen=True)
class WorkspaceChangeBaseline:
    """A lightweight Git tree representing the workspace before one write turn."""

    available: bool
    repository: str = ""
    pathspec: str = "."
    tree: str = ""
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "repository": self.repository,
            "pathspec": self.pathspec,
            "tree": self.tree,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: object) -> "WorkspaceChangeBaseline | None":
        if not isinstance(data, dict) or not isinstance(data.get("available"), bool):
            return None
        tree = str(data.get("tree", ""))
        if tree and _GIT_OBJECT_RE.fullmatch(tree) is None:
            return None
        return cls(
            available=data["available"],
            repository=str(data.get("repository", "")),
            pathspec=str(data.get("pathspec", ".")) or ".",
            tree=tree,
            reason=str(data.get("reason", "")),
        )


def capture_change_baseline(workspace: Path) -> WorkspaceChangeBaseline:
    """Record the current tracked and untracked workspace state as a Git tree.

    A temporary index is used, so the user's staging area and working files are
    not changed. Git may create unreachable blob/tree objects, but no ref,
    branch, commit, or worktree is created.
    """

    resolved = workspace.resolve()
    root_result = _git(resolved, "rev-parse", "--show-toplevel")
    if root_result.returncode != 0 or not root_result.stdout.strip():
        return WorkspaceChangeBaseline(
            available=False,
            reason="当前工作区不是 Git 仓库，无法生成逐文件变更预览",
        )
    repository = Path(root_result.stdout.strip()).resolve()
    try:
        relative = resolved.relative_to(repository)
    except ValueError:
        return WorkspaceChangeBaseline(
            available=False,
            reason="工作区不在 Git 仓库根目录内",
        )
    pathspec = relative.as_posix() if relative.parts else "."
    try:
        tree = _write_workspace_tree(repository, pathspec)
    except OSError as exc:
        return WorkspaceChangeBaseline(
            available=False,
            repository=str(repository),
            pathspec=pathspec,
            reason=f"无法记录执行前工作区：{exc}",
        )
    return WorkspaceChangeBaseline(
        available=True,
        repository=str(repository),
        pathspec=pathspec,
        tree=tree,
    )


def summarize_workspace_changes(
    workspace: Path,
    baseline: WorkspaceChangeBaseline | None,
) -> dict[str, Any]:
    """Return JSON-ready, task-scoped file statistics and unified diffs."""

    if baseline is None or not baseline.available or not baseline.tree:
        return _unavailable_change_summary(
            baseline.reason if baseline is not None else "缺少执行前工作区快照"
        )
    current = capture_change_baseline(workspace)
    if not current.available:
        return _unavailable_change_summary(current.reason)
    if (
        Path(current.repository).resolve() != Path(baseline.repository).resolve()
        or current.pathspec != baseline.pathspec
    ):
        return _unavailable_change_summary("执行前后的 Git 工作区不一致")

    repository = Path(baseline.repository)
    numstat = _git_repository(
        repository,
        "diff",
        "--numstat",
        "-z",
        "--no-renames",
        baseline.tree,
        current.tree,
        "--",
        baseline.pathspec,
    )
    if numstat.returncode != 0:
        return _unavailable_change_summary("Git 无法计算执行后的文件差异")

    added_paths = _diff_paths(
        repository, baseline.tree, current.tree, baseline.pathspec, "A"
    )
    deleted_paths = _diff_paths(
        repository, baseline.tree, current.tree, baseline.pathspec, "D"
    )
    rows: list[tuple[str, int | None, int | None]] = []
    total_additions = 0
    total_deletions = 0
    for raw in numstat.stdout.split("\0"):
        if not raw:
            continue
        fields = raw.split("\t", 2)
        if len(fields) != 3:
            continue
        additions = int(fields[0]) if fields[0].isdigit() else None
        deletions = int(fields[1]) if fields[1].isdigit() else None
        path = fields[2]
        if additions is not None:
            total_additions += additions
        if deletions is not None:
            total_deletions += deletions
        rows.append((path, additions, deletions))

    rows.sort(key=lambda item: item[0].casefold())
    visible_rows = rows[:MAX_CHANGED_FILES]
    remaining_patch_chars = MAX_TOTAL_PATCH_CHARS
    files: list[dict[str, Any]] = []
    any_patch_truncated = False
    for path, additions, deletions in visible_rows:
        patch_result = _git_repository(
            repository,
            "diff",
            "--no-ext-diff",
            "--no-renames",
            "--unified=3",
            baseline.tree,
            current.tree,
            "--",
            path,
        )
        patch = patch_result.stdout if patch_result.returncode == 0 else ""
        patch_limit = min(MAX_FILE_PATCH_CHARS, max(0, remaining_patch_chars))
        patch_truncated = len(patch) > patch_limit
        if patch_truncated:
            patch = patch[:patch_limit].rstrip() + "\n… diff 预览已截断 …"
        remaining_patch_chars -= min(len(patch), patch_limit)
        any_patch_truncated = any_patch_truncated or patch_truncated
        status = "added" if path in added_paths else "deleted" if path in deleted_paths else "modified"
        files.append(
            {
                "path": _display_change_path(path, baseline.pathspec),
                "status": status,
                "additions": additions,
                "deletions": deletions,
                "binary": additions is None or deletions is None,
                "patch": patch,
                "patch_truncated": patch_truncated,
            }
        )

    return {
        "available": True,
        "file_count": len(rows),
        "additions": total_additions,
        "deletions": total_deletions,
        "files": files,
        "truncated": len(rows) > len(visible_rows) or any_patch_truncated,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


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
                if any(
                    part in {".git", ".multiagent", ".mutiagent", "__pycache__"}
                    for part in relative.parts
                ):
                    continue
            except OSError:
                continue
            _update_file_fingerprint(digest, root, relative)
    return digest.hexdigest()


def current_workspace_fingerprint(workspace: Path) -> str:
    """Create a content-sensitive fingerprint with two Git subprocesses.

    The legacy implementation remains available for old checkpoints, but new
    checkpoints avoid collecting three separate diffs plus an untracked-file
    listing every time the workflow advances.
    """

    workspace = workspace.resolve()
    status = _git(
        workspace,
        "status",
        "--porcelain=v2",
        "--branch",
        "-z",
        "--untracked-files=all",
    )
    if status.returncode != 0:
        snapshot = WorkspaceSnapshot(is_git_repo=False)
        return f"{FINGERPRINT_PROTOCOL}:{workspace_fingerprint(snapshot, workspace)}"

    digest = hashlib.sha256()
    digest.update(b"multiagent-workspace-v2\0")
    digest.update(status.stdout.encode("utf-8", errors="surrogateescape"))

    metadata = _git(workspace, "rev-parse", "--show-toplevel", "--git-path", "index")
    metadata_lines = [line for line in metadata.stdout.splitlines() if line]
    repository_root = workspace
    if metadata.returncode == 0 and metadata_lines:
        repository_root = Path(metadata_lines[0]).resolve()
    if metadata.returncode == 0 and len(metadata_lines) >= 2:
        index_path = Path(metadata_lines[1])
        if not index_path.is_absolute():
            index_path = workspace / index_path
        _update_path_fingerprint(digest, index_path, "<git-index>", force_content=True)

    for relative in _git_status_paths(status.stdout):
        _update_path_fingerprint(digest, repository_root / relative, relative)
    return f"{FINGERPRINT_PROTOCOL}:{digest.hexdigest()}"


def workspace_fingerprint_matches(expected: str, workspace: Path) -> bool:
    """Compare both current v2 and pre-v2 checkpoint fingerprints."""

    if expected.startswith(f"{FINGERPRINT_PROTOCOL}:"):
        return current_workspace_fingerprint(workspace) == expected
    legacy = workspace_fingerprint(capture_workspace(workspace), workspace)
    return legacy == expected


def _git_status_paths(output: str) -> list[str]:
    records = output.split("\0")
    paths: set[str] = set()
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record or record.startswith("# "):
            continue
        if record.startswith("1 "):
            parts = record.split(" ", 8)
            if len(parts) == 9:
                paths.add(parts[8])
        elif record.startswith("2 "):
            parts = record.split(" ", 9)
            if len(parts) == 10:
                paths.add(parts[9])
            if index < len(records) and records[index]:
                paths.add(records[index])
                index += 1
        elif record.startswith("u "):
            parts = record.split(" ", 10)
            if len(parts) == 11:
                paths.add(parts[10])
        elif record.startswith("? "):
            paths.add(record[2:])
    return sorted(paths)


def _update_file_fingerprint(digest, root: Path, relative: Path) -> None:
    _update_path_fingerprint(digest, root / relative, str(relative))


def _update_path_fingerprint(
    digest,
    path: Path,
    label: str,
    *,
    force_content: bool = False,
) -> None:
    try:
        stat = path.lstat()
    except OSError:
        digest.update(label.encode("utf-8", errors="replace"))
        digest.update(b"\0missing")
        return
    digest.update(label.encode("utf-8", errors="replace"))
    digest.update(str(stat.st_size).encode("ascii"))
    if path.is_symlink():
        try:
            digest.update(path.readlink().as_posix().encode("utf-8", errors="replace"))
        except OSError:
            digest.update(str(stat.st_mtime_ns).encode("ascii"))
    elif path.is_file() and (force_content or stat.st_size <= MAX_FINGERPRINT_FILE_BYTES):
        try:
            with path.open("rb") as file:
                for chunk in iter(lambda: file.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError:
            digest.update(str(stat.st_mtime_ns).encode("ascii"))
    else:
        digest.update(str(stat.st_mtime_ns).encode("ascii"))


def _write_workspace_tree(repository: Path, pathspec: str) -> str:
    with tempfile.TemporaryDirectory(prefix="multiagent-git-index-") as directory:
        temporary_index = Path(directory) / "index"
        index_result = _git_repository(repository, "rev-parse", "--git-path", "index")
        if index_result.returncode == 0 and index_result.stdout.strip():
            source_index = Path(index_result.stdout.strip())
            if not source_index.is_absolute():
                source_index = repository / source_index
            if source_index.is_file():
                shutil.copyfile(source_index, temporary_index)
        environment = os.environ.copy()
        environment["GIT_INDEX_FILE"] = str(temporary_index)
        added = _git_repository(
            repository,
            "add",
            "-A",
            "--",
            pathspec,
            environment=environment,
        )
        if added.returncode != 0:
            raise OSError(added.stderr.strip() or "git add 失败")
        tree = _git_repository(
            repository,
            "write-tree",
            environment=environment,
        )
        value = tree.stdout.strip()
        if tree.returncode != 0 or _GIT_OBJECT_RE.fullmatch(value) is None:
            raise OSError(tree.stderr.strip() or "git write-tree 失败")
        return value


def _diff_paths(
    repository: Path,
    before: str,
    after: str,
    pathspec: str,
    status: str,
) -> set[str]:
    result = _git_repository(
        repository,
        "diff",
        "--name-only",
        "-z",
        "--no-renames",
        f"--diff-filter={status}",
        before,
        after,
        "--",
        pathspec,
    )
    if result.returncode != 0:
        return set()
    return {path for path in result.stdout.split("\0") if path}


def _display_change_path(path: str, pathspec: str) -> str:
    if pathspec == ".":
        return path
    prefix = f"{pathspec.rstrip('/')}/"
    return path[len(prefix) :] if path.startswith(prefix) else path


def _unavailable_change_summary(reason: str) -> dict[str, Any]:
    return {
        "available": False,
        "file_count": 0,
        "additions": 0,
        "deletions": 0,
        "files": [],
        "truncated": False,
        "reason": reason or "无法生成文件变更预览",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def _git(workspace: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(workspace),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(["git", *args], 1, "", str(exc))


def _git_repository(
    repository: Path,
    *args: str,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(repository),
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(["git", *args], 1, "", str(exc))
