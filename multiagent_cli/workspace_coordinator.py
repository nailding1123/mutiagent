from __future__ import annotations

"""Coordinate concurrent Agent access to one target workspace.

Reads are deliberately not serialized: an Agent that only needs to inspect the
workspace can use the main checkout even while another Agent is writing it.
For ordinary Git workspaces there can be at most one isolated write Worktree.
A second writer starts from a snapshot of the main checkout, and its
uncommitted diff is applied back to the main checkout when the lease ends.
Explicit dual-Agent comparison creates one retained Worktree per candidate
from one shared snapshot and never applies automatically. Non-Git workspaces
have no safe snapshot/merge primitive, so native Agents share the target
workspace. Temporary Git snapshots are unreachable; no user-visible commit or
branch is created.
"""

import os
import shlex
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .workspace_state import (
    WorkspaceChangeBaseline,
    capture_change_baseline,
    summarize_workspace_changes,
)


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
    """Coordinate ordinary leases and retained A/B comparison Worktrees."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._states: dict[str, _WorkspaceState] = {}

    def validate_comparison_workspace(self, workspace: Path) -> None:
        """Require a Git checkout with a real HEAD for A/B isolation."""

        repository, _pathspec = _repository_context(workspace)
        if repository is None:
            raise WorkspaceCoordinatorError(
                "双 Agent 对比执行需要 Git 工作区，以便隔离和回滚。"
                "当前目录无法创建 Worktree，请先初始化 Git 仓库或改用单 Agent 执行。"
            )

    def prepare_comparison(
        self,
        workspace: Path,
        owners: tuple[str, ...],
        *,
        comparison_id: str,
    ) -> dict[str, Any]:
        """Create one detached Worktree per candidate from one shared snapshot."""

        self.validate_comparison_workspace(workspace)
        repository, pathspec = _repository_context(workspace)
        assert repository is not None
        baseline = capture_change_baseline(workspace)
        if not baseline.available or not baseline.tree:
            raise WorkspaceCoordinatorError(
                baseline.reason or "无法记录双 Agent 对比执行的工作区基线"
            )
        base_commit = _snapshot_commit(repository, pathspec)
        candidates: dict[str, dict[str, Any]] = {}
        created_roots: list[Path] = []
        try:
            for owner in owners:
                worktree_root = _create_worktree_from_base(
                    repository,
                    base_commit,
                    prefix=f"multiagent-{comparison_id}-{owner}-",
                )
                created_roots.append(worktree_root)
                candidate_workspace = (
                    worktree_root
                    if pathspec == "."
                    else worktree_root / Path(pathspec)
                )
                candidate_baseline = capture_change_baseline(candidate_workspace)
                if not candidate_baseline.available or not candidate_baseline.tree:
                    raise WorkspaceCoordinatorError(
                        candidate_baseline.reason
                        or f"无法记录 {owner} 候选工作区基线"
                    )
                quoted = shlex.quote(str(candidate_workspace))
                candidates[owner] = {
                    "workspace": str(candidate_workspace),
                    "worktree_root": str(worktree_root),
                    "base_commit": base_commit,
                    "baseline": candidate_baseline.to_dict(),
                    "status": "running",
                    "changes": None,
                    "response_message_id": "",
                    "preview_commands": [
                        f"cd {quoted}",
                        "git status --short",
                        "git diff --stat",
                        "git diff",
                        "git diff --check",
                    ],
                    "apply_status": "pending",
                    "error": "",
                    "cleaned": False,
                }
        except BaseException:
            for root in created_roots:
                _remove_worktree(root)
            raise
        return {
            "id": comparison_id,
            "status": "running",
            "trigger_message_id": "",
            "created_at": _timestamp(),
            "base": {
                **baseline.to_dict(),
                "commit": base_commit,
                "created_at": _timestamp(),
            },
            "selected_agent": None,
            "candidates": candidates,
            "error": "",
            "recovery_patch": "",
        }

    def candidate_workspace(
        self,
        comparison: dict[str, Any],
        agent: str,
    ) -> Path:
        candidate = _comparison_candidate(comparison, agent)
        workspace = Path(str(candidate.get("workspace") or "")).resolve()
        if not workspace.is_dir():
            raise WorkspaceCoordinatorError(f"{agent} 候选工作区已经不存在")
        return workspace

    def collect_candidate_diff(
        self,
        comparison: dict[str, Any],
        agent: str,
    ) -> dict[str, Any]:
        candidate = _comparison_candidate(comparison, agent)
        baseline = WorkspaceChangeBaseline.from_dict(candidate.get("baseline"))
        workspace = self.candidate_workspace(comparison, agent)
        if baseline is None:
            raise WorkspaceCoordinatorError(f"{agent} 候选缺少有效工作区基线")
        return summarize_workspace_changes(workspace, baseline)

    def collect_conflict_context(
        self,
        comparison: dict[str, Any],
        agent: str,
    ) -> dict[str, Any]:
        """Collect bounded main/candidate diffs for an Agent safety review.

        The returned context is informational only.  It deliberately captures
        the current main checkout and the retained candidate Worktree without
        applying either one, so an Agent can assess a conflict while the
        deterministic tree guard in :meth:`apply_candidate` remains the final
        authority.
        """

        target_workspace, base = _comparison_target(comparison)
        current = capture_change_baseline(target_workspace)
        if not current.available:
            raise WorkspaceCoordinatorError(
                current.reason or "无法读取冲突时的主工作区状态"
            )
        baseline = _baseline_from_comparison_base(base)
        main_changes = summarize_workspace_changes(target_workspace, baseline)
        candidate_changes = self.collect_candidate_diff(comparison, agent)
        candidate_workspace = self.candidate_workspace(comparison, agent)
        candidate_current = capture_change_baseline(candidate_workspace)
        if not candidate_current.available:
            raise WorkspaceCoordinatorError(
                candidate_current.reason or "无法读取候选工作区状态"
            )
        drift_files = _workspace_drift(
            target_workspace,
            str(base.get("tree") or ""),
            current,
            str(base.get("pathspec") or "."),
        )
        return {
            "main_workspace": str(target_workspace),
            "candidate_workspace": str(candidate_workspace),
            "base_tree": str(base.get("tree") or ""),
            "main_tree": current.tree,
            "candidate_tree": candidate_current.tree,
            "changed_files": drift_files,
            "main_changes": main_changes,
            "candidate_changes": candidate_changes,
        }

    def prepare_conflict_resolution(
        self,
        comparison: dict[str, Any],
        agent: str,
    ) -> dict[str, Any]:
        """Create a fresh candidate Worktree from the current main checkout.

        Conflict resolution must start from what the user currently has, not
        from the stale A/B base. The Agent can then re-implement the selected
        candidate's intent without asking Git to blindly replay an old patch.
        """

        target_workspace, _base = _comparison_target(comparison)
        repository, pathspec = _repository_context(target_workspace)
        if repository is None:
            raise WorkspaceCoordinatorError("冲突重做需要可用的 Git 工作区")
        target_baseline = capture_change_baseline(target_workspace)
        if not target_baseline.available or not target_baseline.tree:
            raise WorkspaceCoordinatorError(
                target_baseline.reason or "无法记录冲突重做的主工作区基线"
            )
        base_commit = _snapshot_commit(repository, pathspec)
        root = _create_worktree_from_base(
            repository,
            base_commit,
            prefix=f"multiagent-resolution-{comparison.get('id', 'comparison')}-{agent}-",
        )
        candidate_workspace = root if pathspec == "." else root / Path(pathspec)
        baseline = capture_change_baseline(candidate_workspace)
        if not baseline.available or not baseline.tree:
            _remove_worktree(root)
            raise WorkspaceCoordinatorError("无法记录冲突重做候选工作区基线")
        return {
            "workspace": str(candidate_workspace),
            "worktree_root": str(root),
            "base_commit": base_commit,
            "baseline": baseline.to_dict(),
            "target_baseline": target_baseline.to_dict(),
        }

    def discard_conflict_resolution(self, resolution: object) -> None:
        if not isinstance(resolution, dict):
            return
        root = str(resolution.get("worktree_root") or "")
        if root:
            _remove_worktree(Path(root))

    def apply_candidate(
        self,
        comparison: dict[str, Any],
        agent: str,
    ) -> dict[str, Any]:
        """Apply exactly one candidate when the main checkout still matches base."""

        candidate = _comparison_candidate(comparison, agent)
        if candidate.get("status") not in {"ready", "no_changes"}:
            raise WorkspaceCoordinatorError("只有已完成的候选方案可以采用")
        target_workspace, base = _comparison_target(comparison)
        target = Path(str(base.get("repository"))).resolve()
        current = capture_change_baseline(target_workspace)
        preview = comparison.get("preview") if isinstance(comparison.get("preview"), dict) else {}
        preview_agent = str(preview.get("active_agent") or "")
        selected_tree = _candidate_tree(candidate)
        current_is_selected = bool(selected_tree and current.tree == selected_tree)
        if current_is_selected:
            rollback = _save_rollback_metadata(
                target_workspace,
                _baseline_from_comparison_base(base),
                current,
                f"{comparison.get('id', 'comparison')}-{agent}",
            )
            self.cleanup_comparison(comparison)
            return {
                "applied": True,
                "error": "",
                "recovery_patch": "",
                "rollback": rollback,
            }
        if preview_agent and preview_agent != agent:
            active_candidate = _comparison_candidate(comparison, preview_agent)
            active_tree = _candidate_tree(active_candidate)
            if not active_tree or not current.available or current.tree != active_tree:
                raise WorkspaceCoordinatorError(
                    "主工作区在预览期间发生了变化，已停止切换以避免覆盖现有修改。"
                )
            _apply_patch(target_workspace, _comparison_patch(active_candidate, target_workspace), reverse=True)
            current = capture_change_baseline(target_workspace)
        resolution = candidate.get("resolution")
        if isinstance(resolution, dict):
            resolution_base = str(resolution.get("base_tree") or "")
            result_tree = str(resolution.get("result_tree") or "")
            if not resolution_base or not result_tree:
                raise WorkspaceCoordinatorError("Agent 冲突重做缺少完整工作区快照")
            if not current.available or current.tree != resolution_base:
                patch = _tree_diff_patch(
                    Path(str(base.get("repository") or target_workspace)),
                    resolution_base,
                    result_tree,
                    str(base.get("pathspec") or "."),
                )
                recovery = _save_recovery_patch(
                    target_workspace,
                    f"{comparison.get('id', 'comparison')}-{agent}-resolution",
                    patch,
                )
                return {
                    "applied": False,
                    "error": "主工作区在 Agent 冲突重做后又发生了变化，已停止应用。",
                    "recovery_patch": str(recovery or ""),
                    "changed_files": _workspace_drift(
                        target_workspace,
                        resolution_base,
                        current,
                        str(base.get("pathspec") or "."),
                    ),
                }
            patch = _tree_diff_patch(
                Path(str(base.get("repository") or target_workspace)),
                resolution_base,
                result_tree,
                str(base.get("pathspec") or "."),
            )
            if patch:
                checked = _git(
                    target,
                    "apply",
                    "--check",
                    "--binary",
                    "--whitespace=nowarn",
                    input_text=patch,
                )
                if checked.returncode != 0:
                    recovery = _save_recovery_patch(
                        target_workspace,
                        f"{comparison.get('id', 'comparison')}-{agent}-resolution",
                        patch,
                    )
                    return {
                        "applied": False,
                        "error": "Agent 冲突重做后的补丁未通过 Git 安全校验，已停止应用。",
                        "recovery_patch": str(recovery or ""),
                        "changed_files": [],
                    }
                applied = _git(
                    target,
                    "apply",
                    "--binary",
                    "--whitespace=nowarn",
                    input_text=patch,
                )
                if applied.returncode != 0:
                    recovery = _save_recovery_patch(
                        target_workspace,
                        f"{comparison.get('id', 'comparison')}-{agent}-resolution",
                        patch,
                    )
                    return {
                        "applied": False,
                        "error": "Agent 冲突重做后的补丁应用失败，已停止应用。",
                        "recovery_patch": str(recovery or ""),
                        "changed_files": [],
                    }
            after = capture_change_baseline(target_workspace)
            rollback = _save_rollback_metadata(
                target_workspace,
                _baseline_from_comparison_base(base),
                after,
                f"{comparison.get('id', 'comparison')}-{agent}-resolution",
            )
            self.cleanup_comparison(comparison)
            return {
                "applied": True,
                "error": "",
                "recovery_patch": "",
                "rollback": rollback,
            }
        if not current.available or current.tree != str(base.get("tree") or ""):
            patch = _comparison_patch(candidate, target_workspace)
            recovery = _save_recovery_patch(
                target_workspace,
                f"{comparison.get('id', 'comparison')}-{agent}",
                patch,
            )
            return {
                "applied": False,
                "error": (
                    "主工作区在对比期间发生了变化，已停止应用以避免覆盖现有修改。"
                    "候选补丁已保留，请先处理主工作区变化后再重试。"
                ),
                "recovery_patch": str(recovery or ""),
                "changed_files": _workspace_drift(
                    target_workspace,
                    str(base.get("tree") or ""),
                    current,
                    str(base.get("pathspec") or "."),
                ),
            }
        patch = _comparison_patch(candidate, target_workspace)
        if patch:
            result = _git(
                target,
                "apply",
                "--binary",
                "--whitespace=nowarn",
                input_text=patch,
            )
            if result.returncode != 0:
                recovery = _save_recovery_patch(
                    target_workspace,
                    f"{comparison.get('id', 'comparison')}-{agent}",
                    patch,
                )
                return {
                    "applied": False,
                    "error": "候选补丁无法安全应用到主工作区，已停止应用。",
                    "recovery_patch": str(recovery or ""),
                    "changed_files": _workspace_drift(
                        target_workspace,
                        str(base.get("tree") or ""),
                        current,
                        str(base.get("pathspec") or "."),
                    ),
                }
        after = capture_change_baseline(target_workspace)
        rollback = _save_rollback_metadata(
            target_workspace,
            _baseline_from_comparison_base(base),
            after,
            f"{comparison.get('id', 'comparison')}-{agent}",
        )
        self.cleanup_comparison(comparison)
        return {
            "applied": True,
            "error": "",
            "recovery_patch": "",
            "rollback": rollback,
        }

    def recheck_comparison(self, comparison: dict[str, Any]) -> dict[str, Any]:
        """Revalidate the main checkout after an application conflict."""

        target_workspace, base = _comparison_target(comparison)
        current = capture_change_baseline(target_workspace)
        if not current.available:
            return {
                "status": "conflict",
                "safe": False,
                "error": current.reason or "无法读取主工作区状态。",
            }
        preview = comparison.get("preview") if isinstance(comparison.get("preview"), dict) else {}
        active_agent = str(preview.get("active_agent") or "")
        if active_agent:
            active_candidate = _comparison_candidate(comparison, active_agent)
            active_tree = _candidate_tree(active_candidate)
            if active_tree and current.tree == active_tree:
                return {"status": "previewing", "safe": True, "error": ""}
            if current.tree != str(base.get("tree") or ""):
                return {
                    "status": "conflict",
                    "safe": False,
                    "error": "主工作区仍有变化，请先处理这些变化后再重新检查。",
                }
            preview["active_agent"] = ""
            preview["main_tree"] = current.tree
        if current.tree == str(base.get("tree") or ""):
            return {"status": "review", "safe": True, "error": ""}
        return {
            "status": "conflict",
            "safe": False,
            "error": "主工作区仍有变化，请先处理这些变化后再重新检查。",
            "changed_files": _workspace_drift(
                target_workspace,
                str(base.get("tree") or ""),
                current,
                str(base.get("pathspec") or "."),
            ),
        }

    def rollback_patch(self, metadata: object) -> dict[str, Any]:
        """Safely reverse one previously recorded workspace change.

        The patch is only applied when the workspace still has the exact tree
        that was recorded immediately after the Agent turn. This conservative
        check prevents a rollback from removing unrelated user changes.
        """

        if not isinstance(metadata, dict) or metadata.get("available") is not True:
            return {
                "status": "unavailable",
                "rolled_back": False,
                "error": "这条 Agent 回复没有可回撤的完整补丁。",
                "recovery_patch": "",
            }
        workspace_text = str(metadata.get("workspace") or "")
        patch_text = str(metadata.get("path") or "")
        before_tree = str(metadata.get("before_tree") or "")
        after_tree = str(metadata.get("after_tree") or "")
        if not workspace_text or not patch_text or not before_tree or not after_tree:
            return {
                "status": "unavailable",
                "rolled_back": False,
                "error": "回撤记录不完整，无法安全操作。",
                "recovery_patch": patch_text,
            }
        workspace = Path(workspace_text).expanduser().resolve()
        patch = Path(patch_text).expanduser().resolve()
        if not workspace.is_dir() or not patch.is_file():
            return {
                "status": "unavailable",
                "rolled_back": False,
                "error": "主工作区或回撤补丁已经不存在。",
                "recovery_patch": patch_text,
            }
        repository, pathspec = _repository_context(workspace)
        if repository is None or pathspec != str(metadata.get("pathspec") or "."):
            return {
                "status": "unavailable",
                "rolled_back": False,
                "error": "回撤记录对应的 Git 工作区已经发生变化。",
                "recovery_patch": patch_text,
            }
        if Path(str(metadata.get("repository") or "")).resolve() != repository:
            return {
                "status": "unavailable",
                "rolled_back": False,
                "error": "回撤记录不属于当前主工作区。",
                "recovery_patch": patch_text,
            }
        if not _is_within_git_common_dir(repository, patch):
            return {
                "status": "unavailable",
                "rolled_back": False,
                "error": "回撤补丁路径不受信任，已停止操作。",
                "recovery_patch": patch_text,
            }
        current = capture_change_baseline(workspace)
        if not current.available or current.tree != after_tree:
            return {
                "status": "conflict",
                "rolled_back": False,
                "error": (
                    "主工作区在 Agent 完成后发生了变化，已停止回撤以避免覆盖现有修改。"
                    "原始回撤补丁已保留，请先处理主工作区变化。"
                ),
                "recovery_patch": patch_text,
            }
        try:
            patch_content = patch.read_text(encoding="utf-8")
            checked = _git(
                repository,
                "apply",
                "--check",
                "--binary",
                "--whitespace=nowarn",
                input_text=patch_content,
            )
            if checked.returncode != 0:
                raise WorkspaceCoordinatorError(
                    checked.stderr.strip() or "Git 无法校验回撤补丁"
                )
            applied = _git(
                repository,
                "apply",
                "--binary",
                "--whitespace=nowarn",
                input_text=patch_content,
            )
            if applied.returncode != 0:
                raise WorkspaceCoordinatorError(
                    applied.stderr.strip() or "Git 无法应用回撤补丁"
                )
        except (OSError, WorkspaceCoordinatorError) as exc:
            return {
                "status": "conflict",
                "rolled_back": False,
                "error": f"回撤补丁无法安全应用：{exc}",
                "recovery_patch": patch_text,
            }
        restored = capture_change_baseline(workspace)
        if not restored.available or restored.tree != before_tree:
            return {
                "status": "conflict",
                "rolled_back": False,
                "error": "回撤后工作区校验未通过，已停止继续操作；请检查当前文件。",
                "recovery_patch": patch_text,
            }
        return {
            "status": "rolled_back",
            "rolled_back": True,
            "error": "",
            "recovery_patch": patch_text,
        }

    def save_rollback(
        self,
        workspace: Path,
        before: WorkspaceChangeBaseline | None,
        after: WorkspaceChangeBaseline | None,
        owner: str,
    ) -> dict[str, Any] | None:
        """Persist a rollback patch for a completed write turn."""

        return _save_rollback_metadata(workspace, before, after, owner)

    def preview_candidate(
        self,
        comparison: dict[str, Any],
        agent: str,
    ) -> dict[str, Any]:
        """Temporarily materialize one candidate in the main checkout.

        This is deliberately a reversible patch switch rather than source-code
        commenting. Arbitrary projects may contain binary files, generated
        sources, deleted files, or syntax where comments are not valid.
        """

        candidate = _comparison_candidate(comparison, agent)
        if candidate.get("status") not in {"ready", "no_changes"}:
            raise WorkspaceCoordinatorError("只有已完成的候选方案可以预览")
        target_workspace, base = _comparison_target(comparison)
        current = capture_change_baseline(target_workspace)
        if not current.available:
            raise WorkspaceCoordinatorError(current.reason or "无法读取主工作区状态")
        candidate_tree = _candidate_tree(candidate)
        if not candidate_tree:
            raise WorkspaceCoordinatorError("候选方案缺少可预览的工作区快照")
        preview = comparison.get("preview")
        if not isinstance(preview, dict):
            preview = {"active_agent": "", "main_tree": str(base.get("tree") or "")}
            comparison["preview"] = preview
        active_agent = str(preview.get("active_agent") or "")
        if active_agent == agent and current.tree == candidate_tree:
            return {"previewed": True, "agent": agent, "error": ""}

        if active_agent:
            active_candidate = _comparison_candidate(comparison, active_agent)
            active_tree = _candidate_tree(active_candidate)
            if not active_tree or current.tree != active_tree:
                raise WorkspaceCoordinatorError(
                    "主工作区在预览期间发生了变化，已停止切换以避免覆盖现有修改。"
                )
            active_patch = _comparison_patch(active_candidate, target_workspace)
            if active_patch:
                _apply_patch(target_workspace, active_patch, reverse=True)
        elif current.tree != str(base.get("tree") or ""):
            raise WorkspaceCoordinatorError(
                "主工作区在对比期间发生了变化，已停止预览以避免覆盖现有修改。"
            )

        target_patch = _comparison_patch(candidate, target_workspace)
        if target_patch:
            _apply_patch(target_workspace, target_patch)
        after = capture_change_baseline(target_workspace)
        if not after.available or after.tree != candidate_tree:
            raise WorkspaceCoordinatorError("候选方案未能完整应用到主工作区，已停止预览。")
        preview["active_agent"] = agent
        preview["main_tree"] = candidate_tree
        preview["updated_at"] = _timestamp()
        return {"previewed": True, "agent": agent, "error": ""}

    def cleanup_comparison(self, comparison: dict[str, Any]) -> None:
        """Remove candidate Worktrees without changing the main checkout."""

        candidates = comparison.get("candidates")
        if not isinstance(candidates, dict):
            return
        for candidate in candidates.values():
            if not isinstance(candidate, dict):
                continue
            root_text = str(candidate.get("worktree_root") or "")
            if root_text:
                _remove_worktree(Path(root_text))
            candidate["cleaned"] = True

    def discard_comparison(self, comparison: dict[str, Any]) -> None:
        preview = comparison.get("preview") if isinstance(comparison.get("preview"), dict) else {}
        active_agent = str(preview.get("active_agent") or "")
        if active_agent:
            target_workspace, base = _comparison_target(comparison)
            current = capture_change_baseline(target_workspace)
            active_candidate = _comparison_candidate(comparison, active_agent)
            active_tree = _candidate_tree(active_candidate)
            if not current.available or not active_tree or current.tree != active_tree:
                raise WorkspaceCoordinatorError(
                    "主工作区在预览期间发生了变化，无法安全恢复原始状态；候选工作区仍保留。"
                )
            active_patch = _comparison_patch(active_candidate, target_workspace)
            if active_patch:
                _apply_patch(target_workspace, active_patch, reverse=True)
            restored = capture_change_baseline(target_workspace)
            if not restored.available or restored.tree != str(base.get("tree") or ""):
                raise WorkspaceCoordinatorError("无法恢复预览前的主工作区状态；候选工作区仍保留。")
            preview["active_agent"] = ""
            preview["main_tree"] = restored.tree
        self.cleanup_comparison(comparison)

    def recover_comparison(self, comparison: object) -> dict[str, Any] | None:
        """Validate persisted candidate paths without ever touching main files."""

        if not isinstance(comparison, dict) or not isinstance(comparison.get("id"), str):
            return None
        recovered = dict(comparison)
        recovered["candidates"] = {
            str(agent): dict(candidate)
            for agent, candidate in (comparison.get("candidates") or {}).items()
            if isinstance(agent, str) and isinstance(candidate, dict)
        }
        if recovered.get("status") in {"applied", "discarded"}:
            return recovered
        for agent, candidate in recovered["candidates"].items():
            workspace = Path(str(candidate.get("workspace") or ""))
            if not workspace.is_dir():
                candidate["status"] = "unavailable"
                candidate["error"] = "候选 Worktree 已不存在"
                candidate["cleaned"] = True
                continue
            if candidate.get("status") == "running":
                try:
                    changes = self.collect_candidate_diff(recovered, agent)
                    candidate["changes"] = changes
                    file_count = changes.get("file_count")
                    has_changes = (
                        isinstance(file_count, int)
                        and not isinstance(file_count, bool)
                        and file_count > 0
                    ) or bool(changes.get("files"))
                    candidate["status"] = (
                        "unavailable"
                        if changes.get("available") is False
                        else "ready"
                        if has_changes
                        else "no_changes"
                    )
                    candidate["error"] = (
                        str(changes.get("reason") or "无法生成候选变更预览")
                        if candidate["status"] == "unavailable"
                        else ""
                    )
                except WorkspaceCoordinatorError as exc:
                    candidate["error"] = str(exc)
                    candidate["status"] = "unavailable"
        if recovered.get("status") == "conflict":
            try:
                target_workspace, base = _comparison_target(recovered)
                current = capture_change_baseline(target_workspace)
                recovered["changed_files"] = _workspace_drift(
                    target_workspace,
                    str(base.get("tree") or ""),
                    current,
                    str(base.get("pathspec") or "."),
                )
            except WorkspaceCoordinatorError:
                recovered["changed_files"] = []
        if recovered.get("status") in {"running", "applying"}:
            recovered["status"] = "review"
        return recovered

    def acquire(
        self,
        workspace: Path,
        *,
        owner: str,
        access: str,
        isolate: bool = True,
    ) -> WorkspaceLease:
        target = workspace
        if access != "write":
            return WorkspaceLease(self, target, target, owner, "read")

        repository, pathspec = _repository_context(target)
        if repository is None:
            # MultiAgent no longer predicts whether a native Agent will write.
            # Without Git there is no Worktree isolation available, so keep
            # independent Agent turns non-blocking and let their native rules
            # decide how to use the shared workspace.
            return WorkspaceLease(self, target, target, owner, "shared-write")
        key = str(target)
        with self._lock:
            state = self._states.get(key)
            if state is None:
                state = _WorkspaceState(threading.Condition(self._lock))
                self._states[key] = state

            while True:
                if not isolate:
                    # A shared checkout cannot safely host overlapping writes.
                    # Queue the second writer instead of creating a Worktree
                    # whose patch may conflict with the first writer later.
                    if state.main_writer is None and state.worktree_owner is None:
                        state.main_writer = owner
                        return WorkspaceLease(self, target, target, owner, "write")
                    state.condition.wait()
                    continue
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
            rollback: dict[str, Any] | None = None
            try:
                patch = _worktree_patch(lease)
                if patch:
                    before = capture_change_baseline(lease.target_workspace)
                    _apply_patch(lease.target_workspace, patch)
                    after = capture_change_baseline(lease.target_workspace)
                    rollback = _save_rollback_metadata(
                        lease.target_workspace,
                        before,
                        after,
                        lease.owner,
                    )
                else:
                    rollback = None
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
            return {
                "isolated": True,
                "merged": merged,
                "error": error,
                "rollback": rollback if merged else None,
            }

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
    return _create_worktree_from_base(repository, base_commit), base_commit


def _create_worktree_from_base(
    repository: Path,
    base_commit: str,
    *,
    prefix: str = "multiagent-worktree-",
) -> Path:
    temporary = Path(tempfile.mkdtemp(prefix=prefix))
    # git worktree add wants to create the target directory itself.
    temporary.rmdir()
    result = _git(repository, "worktree", "add", "--detach", str(temporary), base_commit)
    if result.returncode != 0:
        shutil.rmtree(temporary, ignore_errors=True)
        raise WorkspaceCoordinatorError(
            f"无法创建隔离 Worktree：{result.stderr.strip() or 'git worktree add 失败'}"
        )
    return temporary


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


def _comparison_candidate(
    comparison: dict[str, Any],
    agent: str,
) -> dict[str, Any]:
    candidates = comparison.get("candidates")
    candidate = candidates.get(agent) if isinstance(candidates, dict) else None
    if not isinstance(candidate, dict):
        raise WorkspaceCoordinatorError(f"找不到 {agent} 候选方案")
    return candidate


def _comparison_target(
    comparison: dict[str, Any],
) -> tuple[Path, dict[str, Any]]:
    base = comparison.get("base") if isinstance(comparison.get("base"), dict) else {}
    repository_text = str(base.get("repository") or "")
    target = Path(repository_text).expanduser().resolve() if repository_text else None
    if target is None or not target.is_dir():
        raise WorkspaceCoordinatorError("对比任务的主工作区已经不存在")
    pathspec = str(base.get("pathspec") or ".")
    target_workspace = target if pathspec == "." else target / Path(pathspec)
    if not target_workspace.is_dir():
        raise WorkspaceCoordinatorError("对比任务的主工作区路径已经不存在")
    return target_workspace, base


def _candidate_tree(candidate: dict[str, Any]) -> str:
    stored = str(candidate.get("result_tree") or "")
    if stored:
        return stored
    workspace_text = str(candidate.get("workspace") or "")
    if not workspace_text:
        return ""
    baseline = capture_change_baseline(Path(workspace_text))
    return baseline.tree if baseline.available else ""


def _workspace_drift(
    workspace: Path,
    before_tree: str,
    current: WorkspaceChangeBaseline,
    pathspec: str,
) -> list[dict[str, str]]:
    """Return the exact files that changed since a comparison baseline."""

    if not before_tree or not current.available or not current.tree:
        return []
    repository, detected = _repository_context(workspace)
    if repository is None or detected != pathspec:
        return []
    result = _git(
        repository,
        "diff",
        "--name-status",
        "-z",
        "--no-renames",
        before_tree,
        current.tree,
        "--",
        pathspec,
    )
    if result.returncode != 0:
        return []
    fields = [field for field in result.stdout.split("\0") if field]
    changed: list[dict[str, str]] = []
    index = 0
    while index < len(fields):
        status = fields[index]
        index += 1
        if index >= len(fields):
            break
        path = fields[index]
        index += 1
        changed.append({"status": status, "path": _display_drift_path(path, pathspec)})
        if status.startswith("R") or status.startswith("C"):
            if index < len(fields):
                index += 1
    return changed[:100]


def _display_drift_path(path: str, pathspec: str) -> str:
    if pathspec == ".":
        return path
    prefix = f"{pathspec.rstrip('/')}/"
    return path[len(prefix):] if path.startswith(prefix) else path


def _baseline_from_comparison_base(base: dict[str, Any]) -> WorkspaceChangeBaseline:
    baseline = WorkspaceChangeBaseline.from_dict(base)
    if baseline is None:
        return WorkspaceChangeBaseline(False, reason="对比任务缺少有效主工作区基线")
    return baseline


def _save_rollback_metadata(
    workspace: Path,
    before: WorkspaceChangeBaseline | None,
    after: WorkspaceChangeBaseline | None,
    owner: str,
) -> dict[str, Any] | None:
    """Persist an exact after->before patch and its tree guards."""

    if (
        before is None
        or after is None
        or not before.available
        or not after.available
        or not before.tree
        or not after.tree
        or before.repository != after.repository
        or before.pathspec != after.pathspec
        or before.tree == after.tree
    ):
        return None
    repository = Path(before.repository)
    result = _git(
        repository,
        "diff",
        "--binary",
        "--no-ext-diff",
        "--no-renames",
        after.tree,
        before.tree,
        "--",
        before.pathspec,
    )
    if result.returncode != 0 or not result.stdout:
        return None
    path = _save_recovery_patch(
        workspace,
        f"{owner}-rollback-{_timestamp()}",
        result.stdout,
    )
    if path is None:
        return None
    return {
        "available": True,
        "status": "available",
        "path": str(path),
        "workspace": str(workspace.resolve()),
        "repository": before.repository,
        "pathspec": before.pathspec,
        "before_tree": before.tree,
        "after_tree": after.tree,
        "created_at": _timestamp(),
    }


def _is_within_git_common_dir(repository: Path, candidate: Path) -> bool:
    common = _git(repository, "rev-parse", "--git-common-dir")
    if common.returncode != 0 or not common.stdout.strip():
        return False
    common_dir = Path(common.stdout.strip())
    if not common_dir.is_absolute():
        common_dir = repository / common_dir
    try:
        candidate.relative_to(common_dir.resolve())
    except ValueError:
        return False
    return True


def _comparison_patch(candidate: dict[str, Any], target_workspace: Path) -> str:
    root_text = str(candidate.get("worktree_root") or "")
    base_commit = str(candidate.get("base_commit") or "")
    if not root_text or not base_commit:
        raise WorkspaceCoordinatorError("候选方案缺少 Worktree 恢复信息")
    lease = WorkspaceLease(
        coordinator=WorkspaceCoordinator(),
        workspace=Path(str(candidate.get("workspace") or root_text)),
        target_workspace=target_workspace,
        owner="comparison",
        access="write",
        worktree_root=Path(root_text),
        base_commit=base_commit,
    )
    return _worktree_patch(lease)


def _tree_diff_patch(
    repository: Path,
    before_tree: str,
    after_tree: str,
    pathspec: str,
) -> str:
    if not before_tree or not after_tree or before_tree == after_tree:
        return ""
    result = _git(
        repository,
        "diff",
        "--binary",
        "--no-ext-diff",
        "--no-renames",
        before_tree,
        after_tree,
        "--",
        pathspec,
    )
    if result.returncode != 0:
        raise WorkspaceCoordinatorError(
            result.stderr.strip() or "无法生成冲突重做补丁"
        )
    return result.stdout


def _apply_patch(workspace: Path, patch: str, *, reverse: bool = False) -> None:
    if not patch:
        return
    repository, _ = _repository_context(workspace)
    target = repository or workspace
    direction = ["-R"] if reverse else []
    result = _git(
        target,
        "apply",
        *direction,
        "--binary",
        "--whitespace=nowarn",
        input_text=patch,
    )
    if result.returncode == 0:
        return
    if reverse:
        raise WorkspaceCoordinatorError(
            "无法恢复预览前的主工作区状态；候选工作区仍保留。"
        )
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


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
