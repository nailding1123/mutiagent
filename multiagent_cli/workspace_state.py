from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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
    """Capture tracked and untracked files without changing the user's index."""

    resolved = workspace.resolve()
    root_result = _git(resolved, "rev-parse", "--show-toplevel")
    if root_result.returncode != 0 or not root_result.stdout.strip():
        return WorkspaceChangeBaseline(False, reason="当前工作区不是 Git 仓库，无法生成逐文件变更预览")
    repository = Path(root_result.stdout.strip()).resolve()
    try:
        relative = resolved.relative_to(repository)
    except ValueError:
        return WorkspaceChangeBaseline(False, reason="工作区不在 Git 仓库根目录内")
    pathspec = relative.as_posix() if relative.parts else "."
    try:
        tree = _write_workspace_tree(repository, pathspec)
    except OSError as exc:
        return WorkspaceChangeBaseline(False, str(repository), pathspec, reason=f"无法记录执行前工作区：{exc}")
    return WorkspaceChangeBaseline(True, str(repository), pathspec, tree)


def summarize_workspace_changes(workspace: Path, baseline: WorkspaceChangeBaseline | None) -> dict[str, Any]:
    """Return task-scoped file counts, line counts and bounded unified diffs."""

    if baseline is None or not baseline.available or not baseline.tree:
        return _unavailable_change_summary(baseline.reason if baseline is not None else "缺少执行前工作区快照")
    current = capture_change_baseline(workspace)
    if not current.available:
        return _unavailable_change_summary(current.reason)
    if Path(current.repository).resolve() != Path(baseline.repository).resolve() or current.pathspec != baseline.pathspec:
        return _unavailable_change_summary("执行前后的 Git 工作区不一致")

    repository = Path(baseline.repository)
    numstat = _git_repository(repository, "diff", "--numstat", "-z", "--no-renames", baseline.tree, current.tree, "--", baseline.pathspec)
    if numstat.returncode != 0:
        return _unavailable_change_summary("Git 无法计算执行后的文件差异")
    added_paths = _diff_paths(repository, baseline.tree, current.tree, baseline.pathspec, "A")
    deleted_paths = _diff_paths(repository, baseline.tree, current.tree, baseline.pathspec, "D")
    rows: list[tuple[str, int | None, int | None]] = []
    additions = deletions = 0
    for raw in numstat.stdout.split("\0"):
        if not raw:
            continue
        fields = raw.split("\t", 2)
        if len(fields) != 3:
            continue
        added = int(fields[0]) if fields[0].isdigit() else None
        removed = int(fields[1]) if fields[1].isdigit() else None
        if added is not None:
            additions += added
        if removed is not None:
            deletions += removed
        rows.append((fields[2], added, removed))
    rows.sort(key=lambda item: item[0].casefold())
    visible = rows[:MAX_CHANGED_FILES]
    remaining = MAX_TOTAL_PATCH_CHARS
    files: list[dict[str, Any]] = []
    truncated = len(rows) > len(visible)
    for path, added, removed in visible:
        patch_result = _git_repository(repository, "diff", "--no-ext-diff", "--no-renames", "--unified=3", baseline.tree, current.tree, "--", path)
        patch = patch_result.stdout if patch_result.returncode == 0 else ""
        limit = min(MAX_FILE_PATCH_CHARS, max(0, remaining))
        if len(patch) > limit:
            patch = patch[:limit].rstrip() + "\n… diff 预览已截断 …"
            truncated = True
        remaining -= min(len(patch), limit)
        files.append({
            "path": _display_change_path(path, baseline.pathspec),
            "status": "added" if path in added_paths else "deleted" if path in deleted_paths else "modified",
            "additions": added,
            "deletions": removed,
            "binary": added is None or removed is None,
            "patch": patch,
            "patch_truncated": len(patch) > limit,
        })
    return {
        "available": True,
        "file_count": len(rows),
        "additions": additions,
        "deletions": deletions,
        "files": files,
        "truncated": truncated,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


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
        added = _git_repository(repository, "add", "-A", "--", pathspec, environment=environment)
        if added.returncode != 0:
            raise OSError(added.stderr.strip() or "git add 失败")
        tree = _git_repository(repository, "write-tree", environment=environment)
        value = tree.stdout.strip()
        if tree.returncode != 0 or _GIT_OBJECT_RE.fullmatch(value) is None:
            raise OSError(tree.stderr.strip() or "git write-tree 失败")
        return value


def _diff_paths(repository: Path, before: str, after: str, pathspec: str, status: str) -> set[str]:
    result = _git_repository(repository, "diff", "--name-only", "-z", "--no-renames", f"--diff-filter={status}", before, after, "--", pathspec)
    return {path for path in result.stdout.split("\0") if path} if result.returncode == 0 else set()


def _display_change_path(path: str, pathspec: str) -> str:
    if pathspec == ".":
        return path
    prefix = f"{pathspec.rstrip('/')}/"
    return path[len(prefix):] if path.startswith(prefix) else path


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
        return subprocess.run(["git", *args], cwd=str(workspace), capture_output=True, text=True, encoding="utf-8", errors="replace", check=False, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(["git", *args], 1, "", str(exc))


def _git_repository(repository: Path, *args: str, environment: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(["git", *args], cwd=str(repository), env=environment, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(["git", *args], 1, "", str(exc))
