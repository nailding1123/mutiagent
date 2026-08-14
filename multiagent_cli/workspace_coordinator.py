from __future__ import annotations

"""Coordinate concurrent Agent access to one target workspace.

Reads are deliberately not serialized: an Agent that only needs to inspect the
workspace can use the main checkout even while another Agent is writing it.
There can be at most one isolated write Worktree.  A second writer starts from
a snapshot of the main checkout, and its uncommitted diff is applied back to
the main checkout when the lease ends.  The temporary snapshot commit is an
unreachable Git object; no user-visible commit or branch is created.
"""

import os
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class WorkspaceCoordinatorError(RuntimeError):
    """The requested isolated workspace could not be prepared or merged."""


@dataclass
class WorkspaceLease:
    coordinator: "WorkspaceCoordinator"
    workspace: Path
    target_workspace: Path
    owner: str
    access: str
    worktree_root: Path | None = None
    base_commit: str = ""
    _released: bool = False

    @property
    def isolated(self) -> bool:
        return self.worktree_root is not None

    def release(self) -> dict[str, Any]:
        if self._released:
            return {"isolated": self.isolated, "merged": True, "error": ""}
        self._released = True
        return self.coordinator.release(self)


@dataclass
class _WorkspaceState:
    condition: threading.Condition
    main_writer: str | None = None
    worktree_owner: str | None = None
    worktree_root: Path | None = None
    worktree_base: str = ""


class WorkspaceCoordinator:
    """Grant read access freely and serialize writes with one optional Worktree."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._states: dict[str, _WorkspaceState] = {}

    def acquire(
        self,
        workspace: Path,
        *,
        owner: str,
        access: str,
    ) -> WorkspaceLease:
        target = workspace
        if access != "write":
            return WorkspaceLease(self, target, target, owner, "read")

        repository, pathspec = _repository_context(target)
        key = str(target)
        with self._lock:
            state = self._states.get(key)
            if state is None:
                state = _WorkspaceState(threading.Condition(self._lock))
                self._states[key] = state

            while True:
                # The first writer owns the real checkout.  A second, distinct
                # writer gets the only isolated Worktree while that lease is
                # active; all other writes wait for one of the two to finish.
                if state.main_writer is None and state.worktree_owner is None:
                    state.main_writer = owner
                    return WorkspaceLease(self, target, target, owner, "write")

                if (
                    repository is not None
                    and state.main_writer is not None
                    and state.worktree_owner is None
                    and state.main_writer != owner
                ):
                    worktree_root, base_commit = _create_worktree(
                        repository,
                        pathspec,
                    )
                    state.worktree_owner = owner
                    state.worktree_root = worktree_root
                    state.worktree_base = base_commit
                    isolated_workspace = (
                        worktree_root
                        if pathspec == "."
                        else worktree_root / Path(pathspec)
                    )
                    return WorkspaceLease(
                        self,
                        isolated_workspace,
                        target,
                        owner,
                        "write",
                        worktree_root=worktree_root,
                        base_commit=base_commit,
                    )

                if repository is None and state.main_writer is not None:
                    raise WorkspaceCoordinatorError(
                        "主工作区正在被另一个 Agent 写入；当前目录不是 Git 仓库，"
                        "无法创建隔离 Worktree，请稍后重试。"
                    )

                state.condition.wait()

    def release(self, lease: WorkspaceLease) -> dict[str, Any]:
        if lease.access != "write":
            return {"isolated": False, "merged": True, "error": ""}

        key = str(lease.target_workspace)
        with self._lock:
            state = self._states.get(key)
            if state is None:
                return {"isolated": lease.isolated, "merged": False, "error": "协调状态不存在"}

            if not lease.isolated:
                if state.main_writer == lease.owner:
                    state.main_writer = None
                    state.condition.notify_all()
                self._drop_state_if_idle(key, state)
                return {"isolated": False, "merged": True, "error": ""}

            # Do not apply the isolated patch while the main writer is still
            # changing the checkout. The other Agent may continue working in
            # its Worktree while this waits, but there is never a second
            # Worktree or a second main writer.
            while state.main_writer is not None:
                state.condition.wait()

            error = ""
            merged = True
            patch = ""
            try:
                patch = _worktree_patch(lease)
                if patch:
                    _apply_patch(lease.target_workspace, patch)
            except WorkspaceCoordinatorError as exc:
                merged = False
                recovery = _save_recovery_patch(
                    lease.target_workspace,
                    lease.owner,
                    patch,
                )
                error = str(exc)
                if recovery is not None:
                    error += f" 完整修改已保存为补丁：{recovery}"
            finally:
                _remove_worktree(lease.worktree_root)
                if state.worktree_owner == lease.owner:
                    state.worktree_owner = None
                    state.worktree_root = None
                    state.worktree_base = ""
                    state.condition.notify_all()
                self._drop_state_if_idle(key, state)
            return {"isolated": True, "merged": merged, "error": error}

    def _drop_state_if_idle(self, key: str, state: _WorkspaceState) -> None:
        if state.main_writer is None and state.worktree_owner is None:
            self._states.pop(key, None)


def _repository_context(workspace: Path) -> tuple[Path | None, str]:
    workspace = workspace.resolve()
    result = _git(workspace, "rev-parse", "--show-toplevel")
    if result.returncode != 0 or not result.stdout.strip():
        return None, "."
    repository = Path(result.stdout.strip()).resolve()
    head = _git(repository, "rev-parse", "--verify", "HEAD")
    if head.returncode != 0 or not head.stdout.strip():
        return None, "."
    try:
        relative = workspace.relative_to(repository)
    except ValueError:
        return None, "."
    return repository, relative.as_posix() if relative.parts else "."


def _create_worktree(repository: Path, pathspec: str) -> tuple[Path, str]:
    base_commit = _snapshot_commit(repository, pathspec)
    temporary = Path(tempfile.mkdtemp(prefix="multiagent-worktree-"))
    # git worktree add wants to create the target directory itself.
    temporary.rmdir()
    result = _git(repository, "worktree", "add", "--detach", str(temporary), base_commit)
    if result.returncode != 0:
        shutil.rmtree(temporary, ignore_errors=True)
        raise WorkspaceCoordinatorError(
            f"无法创建隔离 Worktree：{result.stderr.strip() or 'git worktree add 失败'}"
        )
    return temporary, base_commit


def _snapshot_commit(repository: Path, pathspec: str) -> str:
    """Make an ephemeral commit that includes current tracked/untracked files."""

    head = _git(repository, "rev-parse", "HEAD")
    if head.returncode != 0 or not head.stdout.strip():
        raise WorkspaceCoordinatorError("当前 Git 仓库没有可用于 Worktree 的提交")
    with tempfile.TemporaryDirectory(prefix="multiagent-snapshot-index-") as directory:
        index = Path(directory) / "index"
        environment = os.environ.copy()
        environment["GIT_INDEX_FILE"] = str(index)
        read = _git(repository, "read-tree", "HEAD", environment=environment)
        if read.returncode != 0:
            raise WorkspaceCoordinatorError("无法建立 Worktree 快照索引")
        added = _git(repository, "add", "-A", "--", pathspec, environment=environment)
        if added.returncode != 0:
            raise WorkspaceCoordinatorError(
                f"无法捕获当前工作区：{added.stderr.strip() or 'git add 失败'}"
            )
        tree = _git(repository, "write-tree", environment=environment)
        if tree.returncode != 0 or not tree.stdout.strip():
            raise WorkspaceCoordinatorError("无法写入 Worktree 快照")
        commit = _git(
            repository,
            "commit-tree",
            tree.stdout.strip(),
            "-p",
            head.stdout.strip(),
            environment={
                **environment,
                "GIT_AUTHOR_NAME": "MultiAgent",
                "GIT_AUTHOR_EMAIL": "multiagent@localhost",
                "GIT_COMMITTER_NAME": "MultiAgent",
                "GIT_COMMITTER_EMAIL": "multiagent@localhost",
            },
            input_text="MultiAgent temporary workspace snapshot\n",
        )
        if commit.returncode != 0 or not commit.stdout.strip():
            raise WorkspaceCoordinatorError("无法创建 Worktree 快照")
        return commit.stdout.strip()


def _worktree_patch(lease: WorkspaceLease) -> str:
    if not lease.worktree_root or not lease.base_commit:
        return ""
    pathspec = "."
    repository, detected = _repository_context(lease.target_workspace)
    if repository is not None:
        pathspec = detected
    with tempfile.TemporaryDirectory(prefix="multiagent-worktree-index-") as directory:
        index = Path(directory) / "index"
        environment = os.environ.copy()
        environment["GIT_INDEX_FILE"] = str(index)
        read = _git(
            lease.worktree_root,
            "read-tree",
            lease.base_commit,
            environment=environment,
        )
        if read.returncode != 0:
            raise WorkspaceCoordinatorError("无法读取 Worktree 快照")
        added = _git(
            lease.worktree_root,
            "add",
            "-A",
            "--",
            pathspec,
            environment=environment,
        )
        if added.returncode != 0:
            raise WorkspaceCoordinatorError("无法收集 Worktree 修改")
        result = _git(
            lease.worktree_root,
            "diff",
            "--cached",
            "--binary",
            "--no-ext-diff",
            lease.base_commit,
            "--",
            pathspec,
            environment=environment,
        )
        if result.returncode != 0:
            raise WorkspaceCoordinatorError(
                f"无法读取 Worktree 修改：{result.stderr.strip() or 'git diff 失败'}"
            )
        return result.stdout


def _apply_patch(workspace: Path, patch: str) -> None:
    repository, _ = _repository_context(workspace)
    target = repository or workspace
    result = _git(
        target,
        "apply",
        "--binary",
        "--whitespace=nowarn",
        input_text=patch,
    )
    if result.returncode == 0:
        return
    # Retry with Git's 3-way merger when the direct patch no longer matches the
    # main writer's result. --3way stages its output, so restore the caller's
    # index afterwards without touching working files.
    with tempfile.TemporaryDirectory(prefix="multiagent-main-index-") as directory:
        saved_index = Path(directory) / "index"
        index = _git(target, "rev-parse", "--git-path", "index")
        index_path: Path | None = (
            Path(index.stdout.strip())
            if index.returncode == 0 and index.stdout.strip()
            else None
        )
        if index_path is not None and not index_path.is_absolute():
            index_path = target / index_path
        if index_path is not None and index_path.is_file():
            shutil.copyfile(index_path, saved_index)
        merged = _git(
            target,
            "apply",
            "--3way",
            "--binary",
            "--whitespace=nowarn",
            input_text=patch,
        )
        if saved_index.is_file() and index_path is not None:
            shutil.copyfile(saved_index, index_path)
        if merged.returncode != 0:
            raise WorkspaceCoordinatorError(
                "Worktree 修改无法自动合并到主工作区；为避免覆盖主工作区改动，"
                "本次隔离修改未合并。"
            )


def _remove_worktree(worktree_root: Path | None) -> None:
    if worktree_root is None:
        return
    repository, _ = _repository_context(worktree_root)
    if repository is not None:
        _git(repository, "worktree", "remove", "--force", str(worktree_root))
    shutil.rmtree(worktree_root, ignore_errors=True)


def _save_recovery_patch(workspace: Path, owner: str, patch: str) -> Path | None:
    """Keep an isolated writer's complete diff when automatic merge fails."""

    if not patch:
        return None
    repository, _ = _repository_context(workspace)
    if repository is None:
        return None
    common = _git(repository, "rev-parse", "--git-common-dir")
    if common.returncode != 0 or not common.stdout.strip():
        return None
    common_dir = Path(common.stdout.strip())
    if not common_dir.is_absolute():
        common_dir = repository / common_dir
    safe_owner = "".join(
        char if char.isalnum() or char in {"-", "_"} else "-"
        for char in owner
    ).strip("-") or "agent"
    recovery_dir = common_dir / "multiagent-recovery"
    recovery = recovery_dir / f"{safe_owner}.patch"
    try:
        recovery_dir.mkdir(parents=True, exist_ok=True)
        recovery.write_text(patch, encoding="utf-8")
        return recovery.resolve()
    except OSError:
        return None


def _git(
    cwd: Path,
    *args: str,
    environment: dict[str, str] | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            env=environment,
            input=input_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=45,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(["git", *args], 1, "", str(exc))
