from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from .bridge_models import BridgeError


@dataclass(frozen=True)
class WorktreeRecord:
    source_workspace: Path
    workspace: Path
    worktree_root: Path
    repository_root: Path
    branch: str
    base_head: str

    def to_dict(self) -> dict[str, str]:
        return {
            "source_workspace": str(self.source_workspace),
            "workspace": str(self.workspace),
            "worktree_root": str(self.worktree_root),
            "repository_root": str(self.repository_root),
            "branch": self.branch,
            "base_head": self.base_head,
        }

    @classmethod
    def from_dict(cls, data: object) -> "WorktreeRecord | None":
        if not isinstance(data, dict):
            return None
        values = {
            name: data.get(name)
            for name in (
                "source_workspace",
                "workspace",
                "repository_root",
                "branch",
                "base_head",
            )
        }
        if not all(isinstance(value, str) and value for value in values.values()):
            return None
        worktree_root = data.get("worktree_root", values["workspace"])
        if not isinstance(worktree_root, str) or not worktree_root:
            return None
        return cls(
            source_workspace=Path(str(values["source_workspace"])),
            workspace=Path(str(values["workspace"])),
            worktree_root=Path(worktree_root),
            repository_root=Path(str(values["repository_root"])),
            branch=str(values["branch"]),
            base_head=str(values["base_head"]),
        )


class WorktreeManager:
    """Creates one isolated Git worktree per MutiAgent task."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser()

    def create(
        self,
        source_workspace: Path,
        run_id: str,
        *,
        require_clean: bool = True,
    ) -> WorktreeRecord:
        source_workspace = source_workspace.expanduser().resolve()
        repository_root = self._repository_root(source_workspace)
        if require_clean:
            dirty = self._git(repository_root, "status", "--porcelain=v1").stdout.strip()
            if dirty:
                raise BridgeError(
                    "启用 worktree 隔离时源仓库必须保持干净；"
                    "请先处理未提交改动，或使用 --no-worktree"
                )
        head_result = self._git(repository_root, "rev-parse", "HEAD")
        if head_result.returncode != 0 or not head_result.stdout.strip():
            raise BridgeError("当前 Git 仓库还没有可用于创建 worktree 的提交")
        base_head = head_result.stdout.strip()
        repo_key = hashlib.sha256(str(repository_root).encode("utf-8")).hexdigest()[:12]
        worktree_root = self.root / repo_key / run_id
        relative_workspace = source_workspace.relative_to(repository_root)
        workspace = worktree_root / relative_workspace
        branch = f"mutiagent/{_safe_branch(run_id)}"
        if worktree_root.exists():
            raise BridgeError(f"任务 worktree 已存在：{worktree_root}")
        worktree_root.parent.mkdir(parents=True, exist_ok=True)
        completed = self._git(
            repository_root,
            "worktree",
            "add",
            "-b",
            branch,
            str(worktree_root),
            base_head,
            timeout=60,
        )
        if completed.returncode != 0:
            raise BridgeError(
                "无法创建任务 worktree："
                + (completed.stderr or completed.stdout or "未知 Git 错误").strip()
            )
        return WorktreeRecord(
            source_workspace=source_workspace,
            workspace=workspace,
            worktree_root=worktree_root,
            repository_root=repository_root,
            branch=branch,
            base_head=base_head,
        )

    def diff(self, record: WorktreeRecord) -> str:
        completed = self._git(
            record.worktree_root,
            "diff",
            "--no-ext-diff",
            "--binary",
            record.base_head,
            "--",
            timeout=30,
        )
        if completed.returncode != 0:
            raise BridgeError(
                "无法读取任务差异："
                + (completed.stderr or completed.stdout or "未知 Git 错误").strip()
            )
        sections = [completed.stdout]
        untracked = self._git(
            record.worktree_root,
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
        )
        if untracked.returncode == 0:
            for relative in sorted(item for item in untracked.stdout.split("\0") if item):
                path = record.worktree_root / relative
                extra = self._git(
                    record.worktree_root,
                    "diff",
                    "--no-index",
                    "--binary",
                    "--",
                    "/dev/null",
                    str(path),
                    timeout=30,
                )
                if extra.returncode in {0, 1} and extra.stdout:
                    sections.append(extra.stdout)
        return "".join(sections)

    def status(self, record: WorktreeRecord) -> str:
        completed = self._git(
            record.worktree_root, "status", "--short", "--branch", timeout=15
        )
        if completed.returncode != 0:
            raise BridgeError(
                "无法读取 worktree 状态："
                + (completed.stderr or completed.stdout or "未知 Git 错误").strip()
            )
        return completed.stdout.strip()

    def discard(self, record: WorktreeRecord, *, force: bool = False) -> None:
        if not record.worktree_root.exists():
            return
        if not force and self._git(
            record.worktree_root, "status", "--porcelain=v1"
        ).stdout.strip():
            raise BridgeError("任务 worktree 仍有改动；确认放弃时请使用 --force")
        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        args.append(str(record.worktree_root))
        completed = self._git(record.repository_root, *args, timeout=60)
        if completed.returncode != 0:
            raise BridgeError(
                "无法移除任务 worktree："
                + (completed.stderr or completed.stdout or "未知 Git 错误").strip()
            )
        # The task branch is owned by MutiAgent and is safe to delete only after
        # the explicit discard command removed its worktree.
        self._git(record.repository_root, "branch", "-D", record.branch, timeout=30)

    def _repository_root(self, workspace: Path) -> Path:
        completed = self._git(workspace, "rev-parse", "--show-toplevel")
        if completed.returncode != 0 or not completed.stdout.strip():
            raise BridgeError("--worktree 只能用于已有提交的 Git 仓库")
        return Path(completed.stdout.strip()).resolve()

    @staticmethod
    def _git(
        workspace: Path,
        *args: str,
        timeout: float = 15,
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                ["git", *args],
                cwd=str(workspace),
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return subprocess.CompletedProcess(["git", *args], 1, "", str(exc))


def _safe_branch(run_id: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", run_id).strip(".-")
    return cleaned or "task"
