from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections.abc import Callable, Mapping
from datetime import datetime, timezone

from .adapters import BaseCLIAdapter
from .bridge_models import (
    AgentEvent,
    AgentRunResult,
    BridgeCancelled,
    BridgeError,
    BridgeOutcome,
    BridgeSettings,
    ConsensusLimitReached,
    PlanDecision,
)
from .checkpoints import WorkflowCheckpoint
from .collaboration import CollaborationState
from .consensus import EVIDENCE_CONSENSUS_PROTOCOL, ConsensusDecision, parse_consensus_decision
from .reviews import format_review_for_revision, parse_review_decision
from .verification import (
    format_verification_results,
    run_verifications,
    verifications_passed,
)
from .workspace_state import (
    capture_change_baseline,
    capture_workspace,
    current_workspace_fingerprint,
    format_snapshot,
    summarize_workspace_changes,
    workspace_fingerprint_matches,
)


EventCallback = Callable[[AgentEvent], None]
PlanConfirmation = Callable[..., PlanDecision]
CheckpointCallback = Callable[[WorkflowCheckpoint], None]


INDEPENDENT_PROPOSAL_PROMPT = """你是对等协作中的 {agent_label}（{agent_name}）。请以只读方式独立分析需求和当前工作区，此阶段不要修改任何文件，也不要猜测另一位 Agent 会采用什么方案。

请独立输出完整方案，至少包含：
1. 对需求目标、边界和非目标的理解；
2. 需要修改的组件或文件及原因；
3. 数据流、控制流或接口变化；
4. 关键风险、兼容性和异常场景；
5. 可验证的验收标准和测试计划；
6. 当前仍需假设或澄清的事项。

原始需求：
<task>
{task}
</task>"""


CROSS_REVIEW_PROMPT = """你是对等协作中的 {auditor_label}（{auditor_name}）。你已独立完成自己的方案，现在请交叉审核 {candidate_label}（{candidate_name}）的独立方案，不要修改任何文件。你新增的需求和争议 ID 必须使用 `{id_prefix}-REQ-*`、`{id_prefix}-ISSUE-*` 前缀，避免与对方台账冲突。

重点比较双方需求理解、遗漏场景、无依据扩张、架构适配、失败路径、兼容性、安全性、数据一致性和测试计划。审核必须指出可以直接反馈给对方的修改意见；接受对方方案不代表放弃自己的平等质疑权。

只输出一个 JSON 对象，不要使用 Markdown 代码块或额外文字。每条需求和争议必须有稳定 ID；evidence 必须引用方案章节、工作区文件或可执行测试，不能只写“已考虑”：
{{
  "protocol": "multiagent.consensus.v2",
  "proposal_version": 1,
  "verdict": "accept 或 revise",
  "criteria": {{
    "requirements": true,
    "architecture": true,
    "failure_paths": true,
    "compatibility": true,
    "testing": true
  }},
  "requirements": [
    {{
      "id": "{id_prefix}-REQ-001",
      "text": "可独立验收的原始需求",
      "covered": true,
      "evidence": ["候选方案：测试计划第 2 项", "tests/test_example.py::test_case"]
    }}
  ],
  "issues": [
    {{
      "id": "{id_prefix}-ISSUE-001",
      "severity": "P0、P1、P2 或 P3",
      "requirement": "{id_prefix}-REQ-001",
      "problem": "具体分歧或风险",
      "status": "open、resolved 或 wont_fix",
      "resolution": "解决方式或拒绝理由",
      "evidence": ["src/example.py:42", "方案：失败路径"]
    }}
  ],
  "agreements": ["双方已经一致的事项"],
  "remaining_disagreements": ["尚未解决的具体分歧"],
  "required_revisions": ["对方下一轮必须完成的调整"]
}}

只有五项 criteria 全部为 true、所有 requirements 都 covered 且有 evidence、所有 P0/P1 issue 都 resolved 且有 evidence，并且 remaining_disagreements 和 required_revisions 均为空时，verdict 才能是 accept；否则必须是 revise。

原始需求：
<task>
{task}
</task>

你自己的独立方案：
<own_proposal>
{own_proposal}
</own_proposal>

对方的独立方案：
<candidate_proposal>
{candidate_proposal}
</candidate_proposal>"""


CROSS_REVIEW_REPAIR_SUFFIX = """

你上一次的交叉审核内容如下，但没有通过机器可读协议校验：
<previous_invalid_review>
{previous_review}
</previous_invalid_review>

这一步只修复格式，不重新审核，也不能删除、弱化或改变上一次提出的实质问题。请把所有结论转换成上面要求的单个 `multiagent.consensus.v2` JSON 对象：
- Markdown 条目必须分别映射到 requirements、issues、agreements、remaining_disagreements 或 required_revisions；
- “待解决/未解决”映射为 open，“已解决”映射为 resolved，“不修复”映射为 wont_fix；
- 每个 requirement 和 issue 都必须保留非空 evidence 数组；
- 不能确定的 criteria 填 false，存在任何未解决问题时 verdict 必须为 revise；
- 不要输出 Markdown 代码块、前言、解释或 JSON 之外的任何文字。
"""


UNIFIED_PLAN_PROMPT = """你是本轮临时方案整合者，不拥有高于另一位 Agent 的决策权。请保持只读，综合双方独立方案与双向交叉审核，形成一份供双方共同审核的完整统一方案。

要求：
1. 明确列出已经达成的一致结论；
2. 对每项交叉审核意见说明采纳方式，拒绝时给出代码或需求证据；
3. 不要只输出差异，必须给出可直接实施的完整方案；
4. 保留需求映射、风险、失败路径、验收标准和测试计划；
5. 标注仍未解决的争议，不能用模糊措辞伪造共识。

原始需求：
<task>{task}</task>

Agent A 独立方案：
<agent_a_proposal>{agent_a_proposal}</agent_a_proposal>

Agent B 独立方案：
<agent_b_proposal>{agent_b_proposal}</agent_b_proposal>

Agent A 对 Agent B 的审核：
<agent_a_review>{agent_a_review}</agent_a_review>

Agent B 对 Agent A 的审核：
<agent_b_review>{agent_b_review}</agent_b_review>"""


CONSENSUS_REVIEW_PROMPT = """你是对等协作中的 {auditor_label}（{auditor_name}）。请只读审核双方正在协商的统一方案 v{proposal_version}。你与当前方案整合者拥有同等否决权。

只输出以下结构的 JSON 对象，proposal_version 必须是 {proposal_version}，不要输出 Markdown 代码块或额外文字：
{{
  "protocol": "multiagent.consensus.v2",
  "proposal_version": {proposal_version},
  "verdict": "accept 或 revise",
  "criteria": {{
    "requirements": true,
    "architecture": true,
    "failure_paths": true,
    "compatibility": true,
    "testing": true
  }},
  "requirements": [{{
    "id": "REQ-001",
    "text": "可独立验收的需求",
    "covered": true,
    "evidence": ["统一方案章节或工作区证据"]
  }}],
  "issues": [{{
    "id": "ISSUE-001",
    "severity": "P0、P1、P2 或 P3",
    "requirement": "REQ-001",
    "problem": "具体争议",
    "status": "open、resolved 或 wont_fix",
    "resolution": "解决方式",
    "evidence": ["文件、方案章节或测试证据"]
  }}],
  "agreements": ["双方已一致的事项"],
  "remaining_disagreements": [],
  "required_revisions": []
}}

只有五项 criteria 全部为 true、全部需求有证据、所有 P0/P1 争议已解决且 remaining_disagreements、required_revisions 都为空时才能 accept；否则必须 revise。共享状态中已有的未解决争议不得省略；解决后仍要用原 ID 返回，并填写 resolution 和 evidence。

原始需求：
<task>{task}</task>

统一方案 v{proposal_version}：
<unified_proposal>{unified_proposal}</unified_proposal>"""


CONSENSUS_REVISION_PROMPT = """你是本轮临时方案整合者。上一轮由另一位 Agent 审核；现在轮换由你根据反馈修订共同方案。请保持只读，不要修改文件。

要求：
1. 逐项处理有依据的审核意见；
2. 对不采纳意见给出当前代码或需求证据；
3. 不要只输出差异，重新输出可以直接实施的完整统一方案；
4. 保留明确的验收标准、失败路径和测试计划；
5. 不得删除未解决争议来伪造共识。

原始需求：
<task>{task}</task>

当前统一方案：
<unified_proposal>{unified_proposal}</unified_proposal>

上一轮审核反馈：
<consensus_review>{consensus_review}</consensus_review>

双方独立方案与交叉审核摘要：
<planning_context>{planning_context}</planning_context>"""


USER_PLAN_REVISION_PROMPT = """你是本轮临时方案整合者。请保持只读，根据用户反馈修改双方统一方案，不要只输出差异。

原始需求：<task>{task}</task>
当前统一方案：<unified_proposal>{unified_proposal}</unified_proposal>
用户反馈：<user_feedback>{user_feedback}</user_feedback>"""


TARGETED_AGENT_REVISION_PROMPT = """你是对等协作中的 {agent_label}（{agent_name}）。用户在方案确认阶段专门向你提出了要求。请保持只读，由你独立判断如何把该要求纳入当前双方统一方案，不要修改文件，也不要把这项定向要求冒充成另一位 Agent 的意见。

请输出修订后的完整统一方案，而不是只输出差异。方案必须继续保留需求映射、风险、失败路径、验收标准和测试计划；若要求与原始需求或已有证据冲突，请明确标出冲突及处理理由。

原始需求：<task>{task}</task>
当前统一方案：<unified_proposal>{unified_proposal}</unified_proposal>
用户对 {agent_label} 的定向要求：<user_feedback>{user_feedback}</user_feedback>"""


IMPLEMENT_PROMPT = """你是本阶段的执行协调 Agent。这个角色只表示当前由你持有工作区写权限，不代表你在方案决策上高于另一位 Agent。请按照双方统一方案在当前工作区实际完成任务。

要求：
1. 检查项目约定、现有代码和 Git 状态；
2. 使用原生 CLI 工具实际修改代码，不要只给建议；
3. 保留与任务无关的用户改动，不要重置、覆盖或提交 Git；
4. 运行与风险相称的测试或检查；
5. 最后说明修改内容、测试结果、未采纳意见及原因。

原始需求：
<task>{task}</task>

双方统一方案：
<unified_proposal>{unified_proposal}</unified_proposal>

双向交叉审核与共识记录：
<review_context>{review_context}</review_context>"""


DIRECT_EXECUTION_PROMPT = """你是本阶段的执行协调 Agent。当前已关闭双 Agent 方案协作，请直接在工作区完成任务。

检查项目约定、现有代码和 Git 状态；实际修改代码并运行必要测试；保留无关的用户改动；不要重置或提交 Git。最后说明修改和测试结果。

原始需求：
<task>{task}</task>"""


REVIEW_PROMPT = """你是本阶段的对等代码验收 Agent。请以只读方式检查当前工作区，不要修改任何文件。

请结合原始需求、双方统一方案、交叉审核与共识记录、执行总结、任务前 Git 基线、当前 Git diff 和桥接器真实验证日志进行验收。

先逐项判断实现是否满足验收标准，再检查错误、回归、安全、兼容性、数据一致性和失败路径。忽略不影响交付的纯风格问题。

只输出一个 JSON 对象，不要使用 Markdown 代码块或额外文字：
{{
  "verdict": "approve 或 request_changes",
  "requirements_covered": ["已验证的验收项"],
  "findings": [
    {{
      "severity": "P0、P1、P2 或 P3",
      "file": "文件路径，没有则为空字符串",
      "line": 123,
      "requirement": "对应需求或验收项",
      "problem": "具体问题",
      "evidence": "代码或验证证据",
      "suggestion": "可执行修订建议"
    }}
  ]
}}

P0/P1 必须阻止通过；没有问题时 findings 必须为空数组。

原始需求：
<task>{task}</task>

双方统一方案：
<proposal>{proposal}</proposal>

交叉审核与共识记录：
<planning_context>{planning_context}</planning_context>

执行协调 Agent 实施总结：
<execution_summary>{execution_summary}</execution_summary>

任务开始前的工作区基线：
<baseline>{baseline}</baseline>

桥接器独立验证结果：
<verification>{verification}</verification>"""


REVISION_PROMPT = """对等验收 Agent 和桥接器验证给出了以下反馈。你当前持有修订阶段写权限；请检查工作区，修复所有成立的问题并重新运行必要测试，不要只解释，也不要覆盖无关用户改动。

原始需求：
<task>{task}</task>

双方方案与审核上下文：
<planning_context>{planning_context}</planning_context>

审查反馈：
<review>{review}</review>

桥接器独立验证：
<verification>{verification}</verification>"""


class BridgeOrchestrator:
    def __init__(
        self,
        settings: BridgeSettings,
        adapters: Mapping[str, BaseCLIAdapter],
    ) -> None:
        self.settings = settings
        self.adapters = adapters
        if set(adapters) != {"claude", "codex"}:
            raise ValueError("桥接器必须同时提供 claude 和 codex 适配器")

    def run(
        self,
        task: str,
        *,
        on_event: EventCallback | None = None,
        confirm_plan: PlanConfirmation | None = None,
        checkpoint: WorkflowCheckpoint | None = None,
        on_checkpoint: CheckpointCallback | None = None,
    ) -> BridgeOutcome:
        task = task.strip()
        if not task:
            raise ValueError("任务不能为空")
        if (
            self.settings.planning_collaboration
            and self.settings.plan_approval
            and confirm_plan is None
        ):
            raise BridgeError("启用了方案确认，但调用方没有提供确认处理器")

        agent_a_name = "claude"
        agent_b_name = "codex"
        agent_a = self.adapters[agent_a_name]
        agent_b = self.adapters[agent_b_name]
        executor_name = self.settings.executor
        validator_name = "codex" if executor_name == "claude" else "claude"
        executor_adapter = self.adapters[executor_name]
        validator_adapter = self.adapters[validator_name]

        if checkpoint is None:
            baseline = capture_workspace(self.settings.workspace)
            change_baseline = capture_change_baseline(self.settings.workspace)
            collaboration = CollaborationState.create(
                agent_a=agent_a_name,
                agent_b=agent_b_name,
                planning_collaboration=self.settings.planning_collaboration,
                executor=executor_name,
            )
            state = WorkflowCheckpoint(
                task=task,
                workspace=str(self.settings.workspace),
                executor=executor_name,
                baseline=baseline,
                change_baseline=change_baseline,
                collaboration=collaboration,
            )
            self._checkpoint(state, "initialized", on_checkpoint, on_event)
        else:
            state = checkpoint
            baseline = state.baseline or capture_workspace(self.settings.workspace)
            if state.change_baseline is None and not state.implementation_complete:
                state.change_baseline = capture_change_baseline(
                    self.settings.workspace
                )
            collaboration = state.collaboration
            _ensure_equal_collaboration_tasks(collaboration, executor_name)
            if state.workspace_fingerprint and not workspace_fingerprint_matches(
                state.workspace_fingerprint, self.settings.workspace
            ):
                raise BridgeError(
                    "工作区在上一个精确检查点之后发生变化，已拒绝自动续跑；"
                    "请检查任务工作区，确认改动后重新开始或恢复原状态"
                )
            self._event(
                on_event,
                "Bridge",
                "warning",
                f"已从精确检查点恢复：{state.phase}",
            )
            self._emit_collaboration(on_event, collaboration)
        self._collaboration = collaboration

        if baseline.is_dirty:
            self._event(
                on_event,
                "Bridge",
                "warning",
                "工作区在任务开始前已有未提交改动，验收 Agent 将收到 baseline。",
            )

        proposal_a = state.artifact("proposal_a")
        proposal_b = state.artifact("proposal_b")
        cross_review_a = state.artifact("cross_review_a")
        cross_review_b = state.artifact("cross_review_b")
        unified_proposal = state.artifact("unified_proposal")
        consensus_reviews = _checkpoint_consensus_reviews(state)
        latest_consensus_review = (
            consensus_reviews[-1] if consensus_reviews else None
        )

        if self.settings.planning_collaboration:
            if proposal_a is None or proposal_b is None:
                proposal_a, proposal_b = self._run_initial_planning(
                    task=task,
                    agent_a=agent_a,
                    agent_b=agent_b,
                    proposal_a=proposal_a,
                    proposal_b=proposal_b,
                    state=state,
                    on_event=on_event,
                    on_checkpoint=on_checkpoint,
                )

            if cross_review_a is None or cross_review_b is None:
                cross_review_a, cross_review_b = self._run_cross_reviews(
                    task=task,
                    agent_a=agent_a,
                    agent_b=agent_b,
                    proposal_a=proposal_a,
                    proposal_b=proposal_b,
                    cross_review_a=cross_review_a,
                    cross_review_b=cross_review_b,
                    state=state,
                    on_event=on_event,
                    on_checkpoint=on_checkpoint,
                )
            cross_review_a, cross_review_b = self._repair_invalid_cross_reviews(
                task=task,
                agent_a=agent_a,
                agent_b=agent_b,
                proposal_a=proposal_a,
                proposal_b=proposal_b,
                cross_review_a=cross_review_a,
                cross_review_b=cross_review_b,
                state=state,
                on_event=on_event,
                on_checkpoint=on_checkpoint,
            )
            self._assert_valid_cross_reviews(cross_review_a, cross_review_b)

            if unified_proposal is None:
                unified_proposal = self._synthesize_unified_proposal(
                    task=task,
                    integrator=executor_adapter,
                    integrator_name=executor_name,
                    proposal_a=proposal_a,
                    proposal_b=proposal_b,
                    cross_review_a=cross_review_a,
                    cross_review_b=cross_review_b,
                    state=state,
                    on_event=on_event,
                    on_checkpoint=on_checkpoint,
                )

            if self.settings.consensus:
                (
                    unified_proposal,
                    latest_consensus_review,
                    consensus_reviews,
                ) = self._reach_consensus(
                    task=task,
                    unified_proposal=unified_proposal,
                    proposal_a=proposal_a,
                    proposal_b=proposal_b,
                    cross_review_a=cross_review_a,
                    cross_review_b=cross_review_b,
                    on_event=on_event,
                    state=state,
                    on_checkpoint=on_checkpoint,
                )
            else:
                collaboration.set_task(
                    "plan-review",
                    "skipped",
                    evidence="快速协作模式：已完成双向交叉审核",
                )
                self._sync_collaboration(state, on_checkpoint, on_event)

            while (
                self.settings.plan_approval
                and confirm_plan is not None
                and not state.plan_approved
            ):
                decision = confirm_plan(
                    proposal_a,
                    proposal_b,
                    (cross_review_a, cross_review_b),
                    unified_proposal,
                    latest_consensus_review,
                    state.plan_revisions,
                )
                if decision.action == "approve":
                    state.plan_approved = True
                    self._checkpoint(
                        state, "plan_approved", on_checkpoint, on_event
                    )
                    break
                if decision.action == "cancel":
                    raise BridgeCancelled("用户在实施前取消了任务")
                if decision.action == "interrupt":
                    raise KeyboardInterrupt
                if decision.action not in {"revise", "targeted_revision"}:
                    raise ValueError(f"未知方案确认操作：{decision.action}")
                if state.plan_revisions >= self.settings.max_plan_revisions:
                    raise BridgeCancelled("方案修订次数已达到上限，任务未实施")
                state.plan_revisions += 1
                revision_adapter = executor_adapter
                revision_agent_name = executor_name
                revision_session_id = unified_proposal.session_id
                revision_prompt = USER_PLAN_REVISION_PROMPT.format(
                    task=task,
                    unified_proposal=unified_proposal.final_text,
                    user_feedback=decision.feedback or "请按审查意见修订",
                )
                phase_text = f"{executor_adapter.display_name} 临时整合用户反馈"
                if decision.action == "targeted_revision":
                    if decision.target_agent not in {agent_a_name, agent_b_name}:
                        raise ValueError(
                            f"未知定向 Agent：{decision.target_agent or '未指定'}"
                        )
                    revision_agent_name = decision.target_agent
                    revision_adapter = self.adapters[revision_agent_name]
                    target_proposal = (
                        proposal_a
                        if revision_agent_name == agent_a_name
                        else proposal_b
                    )
                    revision_session_id = target_proposal.session_id
                    agent_label = (
                        "Agent A"
                        if revision_agent_name == agent_a_name
                        else "Agent B"
                    )
                    revision_prompt = TARGETED_AGENT_REVISION_PROMPT.format(
                        agent_label=agent_label,
                        agent_name=revision_adapter.display_name,
                        task=task,
                        unified_proposal=unified_proposal.final_text,
                        user_feedback=decision.feedback,
                    )
                    phase_text = (
                        f"{revision_adapter.display_name} 处理用户对 {agent_label} 的定向要求"
                    )
                self._phase(
                    on_event,
                    phase_text,
                    step_id="user_plan_revision",
                )
                unified_proposal = revision_adapter.run(
                    self._identity_prompt(
                        revision_agent_name,
                        revision_prompt,
                    ),
                    workspace=self.settings.workspace,
                    mode="read",
                    session_id=revision_session_id,
                    on_event=on_event,
                    step_id="user_plan_revision",
                )
                if decision.action == "targeted_revision":
                    collaboration.post(
                        "user",
                        revision_agent_name,
                        "instruction",
                        decision.feedback,
                    )
                state.set_artifact("unified_proposal", unified_proposal)
                next_version = collaboration.proposal_version + 1
                collaboration.consensus_round = 0
                collaboration.set_canonical_proposal(
                    unified_proposal.final_text,
                    author=revision_agent_name,
                    version=next_version,
                )
                collaboration.post(
                    revision_agent_name,
                    (
                        agent_b_name
                        if revision_agent_name == agent_a_name
                        else agent_a_name
                    ),
                    "revision",
                    unified_proposal.final_text,
                )
                self._checkpoint(
                    state, "user_plan_revision_complete", on_checkpoint, on_event
                )
                if self.settings.consensus:
                    (
                        unified_proposal,
                        latest_consensus_review,
                        consensus_reviews,
                    ) = self._reach_consensus(
                        task=task,
                        unified_proposal=unified_proposal,
                        proposal_a=proposal_a,
                        proposal_b=proposal_b,
                        cross_review_a=cross_review_a,
                        cross_review_b=cross_review_b,
                        on_event=on_event,
                        state=state,
                        on_checkpoint=on_checkpoint,
                    )

            if not self.settings.plan_approval:
                state.plan_approved = True
            execution_result = state.artifact("execution_result")
            if not state.implementation_complete or execution_result is None:
                collaboration.set_task("implementation", "in_progress")
                self._sync_collaboration(state, on_checkpoint, on_event)
                self._phase(
                    on_event,
                    f"{executor_adapter.display_name} 获得本阶段写权限并开始实施",
                    step_id="implementation",
                )
                execution_result = executor_adapter.run(
                    self._identity_prompt(
                        executor_name,
                        IMPLEMENT_PROMPT.format(
                            task=task,
                            unified_proposal=unified_proposal.final_text,
                            review_context=_planning_context(
                                proposal_a,
                                proposal_b,
                                cross_review_a,
                                cross_review_b,
                                latest_consensus_review,
                            ),
                        ),
                    ),
                    workspace=self.settings.workspace,
                    mode="write",
                    on_event=on_event,
                    step_id="implementation",
                )
                state.set_artifact("execution_result", execution_result)
                state.implementation_complete = True
                collaboration.set_task(
                    "implementation",
                    "done",
                    evidence=_result_evidence(execution_result),
                )
                collaboration.post(
                    executor_name,
                    validator_name,
                    "evidence",
                    execution_result.final_text,
                )
                self._checkpoint(
                    state, "implementation_complete", on_checkpoint, on_event
                )
        else:
            execution_result = state.artifact("execution_result")
            if not state.implementation_complete or execution_result is None:
                collaboration.set_task("implementation", "in_progress")
                self._sync_collaboration(state, on_checkpoint, on_event)
                self._phase(
                    on_event,
                    f"{executor_adapter.display_name} 开始实施",
                    step_id="implementation",
                )
                execution_result = executor_adapter.run(
                    self._identity_prompt(
                        executor_name, DIRECT_EXECUTION_PROMPT.format(task=task)
                    ),
                    workspace=self.settings.workspace,
                    mode="write",
                    on_event=on_event,
                    step_id="implementation",
                )
                state.set_artifact("execution_result", execution_result)
                state.implementation_complete = True
                collaboration.set_task(
                    "implementation",
                    "done",
                    evidence=_result_evidence(execution_result),
                )
                collaboration.post(
                    executor_name,
                    validator_name,
                    "evidence",
                    execution_result.final_text,
                )
                self._checkpoint(
                    state, "implementation_complete", on_checkpoint, on_event
                )

        cross_reviews = tuple(
            item for item in (cross_review_a, cross_review_b) if item is not None
        )
        agent_proposals = tuple(
            item for item in (proposal_a, proposal_b) if item is not None
        )
        unified_proposal_text = (
            unified_proposal.final_text
            if unified_proposal
            else "未启用双方方案协作"
        )
        planning_context = _planning_context(
            proposal_a,
            proposal_b,
            cross_review_a,
            cross_review_b,
            latest_consensus_review,
        )
        verification_history = state.verifications
        review_runs = state.reviews
        review_decisions = state.review_decisions

        if state.phase == "complete":
            return BridgeOutcome(
                task=task,
                executor=executor_name,
                execution_result=execution_result,
                reviews=tuple(review_runs),
                review_decisions=tuple(review_decisions),
                verifications=tuple(verification_history),
                baseline=baseline,
                final_snapshot=capture_workspace(self.settings.workspace),
                approved=state.approved,
                collaboration=collaboration,
                agent_proposals=agent_proposals,
                cross_reviews=cross_reviews,
                unified_proposal=unified_proposal,
                consensus_reviews=tuple(consensus_reviews),
            )

        if self.settings.review_rounds == 0:
            verification = state.pending_verification
            if not verification:
                collaboration.set_task("verification", "in_progress")
                self._sync_collaboration(state, on_checkpoint, on_event)
                verification = self._verify(on_event)
                state.pending_verification = list(verification)
                verification_history.extend(verification)
            collaboration.set_task(
                "verification",
                "done" if verifications_passed(verification) else "failed",
                evidence=_verification_evidence(verification),
            )
            collaboration.set_task("code-review", "skipped")
            final_snapshot = capture_workspace(self.settings.workspace)
            approved = verifications_passed(verification) if verification else None
            state.pending_verification = []
            state.approved = approved
            self._checkpoint(state, "complete", on_checkpoint, on_event)
            return BridgeOutcome(
                task=task,
                executor=executor_name,
                execution_result=execution_result,
                verifications=tuple(verification_history),
                baseline=baseline,
                final_snapshot=final_snapshot,
                approved=approved,
                collaboration=collaboration,
                agent_proposals=agent_proposals,
                cross_reviews=cross_reviews,
                unified_proposal=unified_proposal,
                consensus_reviews=tuple(consensus_reviews),
            )

        approved = state.approved is True
        revised_after_last_review = False
        for round_index in range(state.review_cursor, self.settings.review_rounds):
            verification = state.pending_verification
            if not verification:
                collaboration.set_task("verification", "in_progress")
                self._sync_collaboration(state, on_checkpoint, on_event)
                verification = self._verify(on_event)
                state.pending_verification = list(verification)
                verification_history.extend(verification)
                collaboration.set_task(
                    "verification",
                    "done" if verifications_passed(verification) else "failed",
                    evidence=_verification_evidence(verification),
                )
                self._checkpoint(
                    state,
                    f"verification_{round_index + 1}_complete",
                    on_checkpoint,
                    on_event,
                )

            if len(review_runs) > round_index:
                review = review_runs[round_index]
                decision = review_decisions[round_index]
            else:
                collaboration.set_task("code-review", "in_progress")
                self._sync_collaboration(state, on_checkpoint, on_event)
                self._phase(
                    on_event,
                    f"{validator_adapter.display_name} 进行第 {round_index + 1} 轮结构化代码验收",
                    step_id=f"code_review_{round_index + 1}",
                )
                review = validator_adapter.run(
                    self._identity_prompt(
                        validator_name,
                        REVIEW_PROMPT.format(
                            task=task,
                            proposal=unified_proposal_text,
                            planning_context=planning_context,
                            execution_summary=execution_result.final_text,
                            baseline=format_snapshot(baseline),
                            verification=format_verification_results(verification),
                        ),
                    ),
                    workspace=self.settings.workspace,
                    mode="read",
                    on_event=on_event,
                    step_id=f"code_review_{round_index + 1}",
                )
                decision = parse_review_decision(review.final_text)
                review_runs.append(review)
                review_decisions.append(decision)
                collaboration.apply_code_review(decision, round_index + 1)
                collaboration.post(
                    validator_name, executor_name, "review", review.final_text
                )
                collaboration.set_task(
                    "code-review",
                    "done" if decision.verdict == "approve" else "blocked",
                    evidence=_result_evidence(review),
                )
                self._checkpoint(
                    state,
                    f"code_review_{round_index + 1}_complete",
                    on_checkpoint,
                    on_event,
                )

            if decision.verdict == "approve" and verifications_passed(verification):
                approved = True
                state.approved = True
                state.review_cursor = round_index + 1
                state.pending_verification = []
                revised_after_last_review = False
                self._checkpoint(
                    state,
                    f"code_review_{round_index + 1}_approved",
                    on_checkpoint,
                    on_event,
                )
                break

            collaboration.set_task("implementation", "in_progress")
            self._sync_collaboration(state, on_checkpoint, on_event)
            self._phase(
                on_event,
                f"{executor_adapter.display_name} 根据审查与验证结果修订",
                step_id=f"code_revision_{round_index + 1}",
            )
            execution_result = executor_adapter.run(
                self._identity_prompt(
                    executor_name,
                    REVISION_PROMPT.format(
                        task=task,
                        planning_context=planning_context,
                        review=format_review_for_revision(decision, review.final_text),
                        verification=format_verification_results(verification),
                    ),
                ),
                workspace=self.settings.workspace,
                mode="write",
                session_id=execution_result.session_id,
                on_event=on_event,
                step_id=f"code_revision_{round_index + 1}",
            )
            state.set_artifact("execution_result", execution_result)
            collaboration.post(
                executor_name,
                validator_name,
                "revision",
                execution_result.final_text,
            )
            collaboration.set_task(
                "implementation",
                "done",
                evidence=_result_evidence(execution_result),
            )
            collaboration.set_task("code-review", "pending")
            state.review_cursor = round_index + 1
            state.pending_verification = []
            state.approved = False
            revised_after_last_review = True
            self._checkpoint(
                state,
                f"code_revision_{round_index + 1}_complete",
                on_checkpoint,
                on_event,
            )

        if (
            self.settings.final_review
            and (revised_after_last_review or state.review_cursor >= self.settings.review_rounds)
            and not approved
            and not state.final_review_complete
        ):
            verification = state.pending_verification
            if not verification:
                collaboration.set_task("verification", "in_progress")
                self._sync_collaboration(state, on_checkpoint, on_event)
                verification = self._verify(on_event)
                state.pending_verification = list(verification)
                verification_history.extend(verification)
                collaboration.set_task(
                    "verification",
                    "done" if verifications_passed(verification) else "failed",
                    evidence=_verification_evidence(verification),
                )
                self._checkpoint(
                    state,
                    "final_verification_complete",
                    on_checkpoint,
                    on_event,
                )
            collaboration.set_task("code-review", "in_progress")
            self._sync_collaboration(state, on_checkpoint, on_event)
            self._phase(
                on_event,
                f"{validator_adapter.display_name} 进行最终结构化验收",
                step_id="final_review",
            )
            final_review = validator_adapter.run(
                self._identity_prompt(
                    validator_name,
                    REVIEW_PROMPT.format(
                        task=task,
                        proposal=unified_proposal_text,
                        planning_context=planning_context,
                        execution_summary=execution_result.final_text,
                        baseline=format_snapshot(baseline),
                        verification=format_verification_results(verification),
                    ),
                ),
                workspace=self.settings.workspace,
                mode="read",
                on_event=on_event,
                step_id="final_review",
            )
            decision = parse_review_decision(final_review.final_text)
            review_runs.append(final_review)
            review_decisions.append(decision)
            collaboration.apply_code_review(decision, state.review_cursor + 1)
            collaboration.post(
                validator_name,
                executor_name,
                "review",
                final_review.final_text,
            )
            collaboration.set_task(
                "code-review",
                "done" if decision.verdict == "approve" else "failed",
                evidence=_result_evidence(final_review),
            )
            approved = decision.verdict == "approve" and verifications_passed(verification)
            state.approved = approved
            state.final_review_complete = True
            state.pending_verification = []
            self._checkpoint(
                state, "final_review_complete", on_checkpoint, on_event
            )
        elif state.final_review_complete:
            approved = state.approved is True

        final_snapshot = capture_workspace(self.settings.workspace)
        state.approved = approved
        self._checkpoint(state, "complete", on_checkpoint, on_event)
        return BridgeOutcome(
            task=task,
            executor=executor_name,
            execution_result=execution_result,
            reviews=tuple(review_runs),
            review_decisions=tuple(review_decisions),
            verifications=tuple(verification_history),
            baseline=baseline,
            final_snapshot=final_snapshot,
            approved=approved,
            collaboration=collaboration,
            agent_proposals=agent_proposals,
            cross_reviews=cross_reviews,
            unified_proposal=unified_proposal,
            consensus_reviews=tuple(consensus_reviews),
        )

    def _run_initial_planning(
        self,
        *,
        task: str,
        agent_a: BaseCLIAdapter,
        agent_b: BaseCLIAdapter,
        proposal_a: AgentRunResult | None,
        proposal_b: AgentRunResult | None,
        state: WorkflowCheckpoint,
        on_event: EventCallback | None,
        on_checkpoint: CheckpointCallback | None,
    ) -> tuple[AgentRunResult, AgentRunResult]:
        if proposal_a is None and proposal_b is None:
            state.collaboration.set_task("plan", "in_progress")
            state.collaboration.set_task("requirements", "in_progress")
            self._sync_collaboration(state, on_checkpoint, on_event)
            self._phase(
                on_event,
                f"{agent_a.display_name} 与 {agent_b.display_name} 并行独立提出方案",
                step_id="initial_planning",
            )
            event_callback = self._locked_event_callback(on_event)
            errors: list[Exception] = []
            with ThreadPoolExecutor(
                max_workers=2,
                thread_name_prefix="multiagent-initial-plan",
            ) as executor:
                futures = {
                    executor.submit(
                        agent_a.run,
                        self._identity_prompt(
                            "claude",
                            INDEPENDENT_PROPOSAL_PROMPT.format(
                                task=task,
                                agent_label="Agent A",
                                agent_name=agent_a.display_name,
                            ),
                            share_state=False,
                        ),
                        workspace=self.settings.workspace,
                        mode="read",
                        on_event=event_callback,
                        step_id="proposal_a",
                    ): "proposal_a",
                    executor.submit(
                        agent_b.run,
                        self._identity_prompt(
                            "codex",
                            INDEPENDENT_PROPOSAL_PROMPT.format(
                                task=task,
                                agent_label="Agent B",
                                agent_name=agent_b.display_name,
                            ),
                            share_state=False,
                        ),
                        workspace=self.settings.workspace,
                        mode="read",
                        on_event=event_callback,
                        step_id="proposal_b",
                    ): "proposal_b",
                }
                for future in as_completed(futures):
                    artifact = futures[future]
                    try:
                        result = future.result()
                    except Exception as exc:
                        errors.append(exc)
                        continue
                    if artifact == "proposal_a":
                        proposal_a = self._record_independent_proposal(
                            result,
                            artifact="proposal_a",
                            task_id="plan",
                            sender="claude",
                            recipient="codex",
                            state=state,
                            on_event=event_callback,
                            on_checkpoint=on_checkpoint,
                        )
                    else:
                        proposal_b = self._record_independent_proposal(
                            result,
                            artifact="proposal_b",
                            task_id="requirements",
                            sender="codex",
                            recipient="claude",
                            state=state,
                            on_event=event_callback,
                            on_checkpoint=on_checkpoint,
                        )
            if errors:
                raise errors[0]
            if proposal_a is None or proposal_b is None:
                raise BridgeError("双方独立方案并行生成未返回完整结果")
            return proposal_a, proposal_b

        if proposal_a is None:
            state.collaboration.set_task("plan", "in_progress")
            self._sync_collaboration(state, on_checkpoint, on_event)
            self._phase(
                on_event,
                f"{agent_a.display_name} 以 Agent A 身份独立提出方案",
                step_id="proposal_a",
            )
            proposal_a = agent_a.run(
                self._identity_prompt(
                    "claude",
                    INDEPENDENT_PROPOSAL_PROMPT.format(
                        task=task,
                        agent_label="Agent A",
                        agent_name=agent_a.display_name,
                    ),
                    share_state=False,
                ),
                workspace=self.settings.workspace,
                mode="read",
                on_event=on_event,
                step_id="proposal_a",
            )
            proposal_a = self._record_independent_proposal(
                proposal_a,
                artifact="proposal_a",
                task_id="plan",
                sender="claude",
                recipient="codex",
                state=state,
                on_event=on_event,
                on_checkpoint=on_checkpoint,
            )

        if proposal_b is None:
            state.collaboration.set_task("requirements", "in_progress")
            self._sync_collaboration(state, on_checkpoint, on_event)
            self._phase(
                on_event,
                f"{agent_b.display_name} 以 Agent B 身份独立提出方案",
                step_id="proposal_b",
            )
            proposal_b = agent_b.run(
                self._identity_prompt(
                    "codex",
                    INDEPENDENT_PROPOSAL_PROMPT.format(
                        task=task,
                        agent_label="Agent B",
                        agent_name=agent_b.display_name,
                    ),
                    share_state=False,
                ),
                workspace=self.settings.workspace,
                mode="read",
                on_event=on_event,
                step_id="proposal_b",
            )
            proposal_b = self._record_independent_proposal(
                proposal_b,
                artifact="proposal_b",
                task_id="requirements",
                sender="codex",
                recipient="claude",
                state=state,
                on_event=on_event,
                on_checkpoint=on_checkpoint,
            )

        return proposal_a, proposal_b

    def _record_independent_proposal(
        self,
        proposal: AgentRunResult,
        *,
        artifact: str,
        task_id: str,
        sender: str,
        recipient: str,
        state: WorkflowCheckpoint,
        on_event: EventCallback | None,
        on_checkpoint: CheckpointCallback | None,
    ) -> AgentRunResult:
        state.set_artifact(artifact, proposal)
        state.collaboration.set_task(
            task_id, "done", evidence=_result_evidence(proposal)
        )
        state.collaboration.post(
            sender, recipient, "proposal", proposal.final_text
        )
        self._checkpoint(state, f"{artifact}_complete", on_checkpoint, on_event)
        return proposal

    def _cross_review_prompt(
        self,
        *,
        task: str,
        name: str,
        adapter: BaseCLIAdapter,
        candidate_adapter: BaseCLIAdapter,
        own_proposal: AgentRunResult,
        candidate_proposal: AgentRunResult,
        previous_review: str = "",
    ) -> str:
        prompt = CROSS_REVIEW_PROMPT.format(
            task=task,
            auditor_label="Agent A" if name == "claude" else "Agent B",
            auditor_name=adapter.display_name,
            candidate_label="Agent B" if name == "claude" else "Agent A",
            candidate_name=candidate_adapter.display_name,
            id_prefix="A" if name == "claude" else "B",
            own_proposal=own_proposal.final_text,
            candidate_proposal=candidate_proposal.final_text,
        )
        if previous_review:
            prompt += CROSS_REVIEW_REPAIR_SUFFIX.format(
                previous_review=previous_review
            )
        return self._identity_prompt(name, prompt)

    def _run_cross_reviews(
        self,
        *,
        task: str,
        agent_a: BaseCLIAdapter,
        agent_b: BaseCLIAdapter,
        proposal_a: AgentRunResult,
        proposal_b: AgentRunResult,
        cross_review_a: AgentRunResult | None,
        cross_review_b: AgentRunResult | None,
        state: WorkflowCheckpoint,
        on_event: EventCallback | None,
        on_checkpoint: CheckpointCallback | None,
    ) -> tuple[AgentRunResult, AgentRunResult]:
        missing: dict[str, tuple[BaseCLIAdapter, str, AgentRunResult, AgentRunResult]] = {}
        if cross_review_a is None:
            missing["cross_review_a"] = (agent_a, "claude", proposal_a, proposal_b)
            state.collaboration.set_task("cross-review-a", "in_progress")
        if cross_review_b is None:
            missing["cross_review_b"] = (agent_b, "codex", proposal_b, proposal_a)
            state.collaboration.set_task("cross-review-b", "in_progress")
        self._sync_collaboration(state, on_checkpoint, on_event)
        self._phase(
            on_event,
            f"{agent_a.display_name} 与 {agent_b.display_name} 并行交叉审核独立方案",
            step_id="cross_review",
        )
        event_callback = self._locked_event_callback(on_event)
        results: dict[str, AgentRunResult] = {}
        errors: list[Exception] = []
        with ThreadPoolExecutor(
            max_workers=len(missing),
            thread_name_prefix="multiagent-cross-review",
        ) as executor:
            futures = {}
            for artifact, (adapter, name, own, candidate) in missing.items():
                futures[
                    executor.submit(
                        adapter.run,
                        self._cross_review_prompt(
                            task=task,
                            name=name,
                            adapter=adapter,
                            candidate_adapter=(
                                agent_b if name == "claude" else agent_a
                            ),
                            own_proposal=own,
                            candidate_proposal=candidate,
                        ),
                        workspace=self.settings.workspace,
                        mode="read",
                        session_id=own.session_id,
                        on_event=event_callback,
                        step_id=artifact,
                    )
                ] = artifact
            for future in as_completed(futures):
                try:
                    results[futures[future]] = future.result()
                except Exception as exc:
                    errors.append(exc)
        if errors:
            raise errors[0]
        cross_review_a = cross_review_a or results.get("cross_review_a")
        cross_review_b = cross_review_b or results.get("cross_review_b")
        if cross_review_a is None or cross_review_b is None:
            raise BridgeError("双向交叉审核未返回完整结果")
        for artifact, result, sender, recipient, task_id in (
            ("cross_review_a", cross_review_a, "claude", "codex", "cross-review-a"),
            ("cross_review_b", cross_review_b, "codex", "claude", "cross-review-b"),
        ):
            if state.artifact(artifact) is None:
                state.set_artifact(artifact, result)
                state.collaboration.post(sender, recipient, "review", result.final_text)
                decision = parse_consensus_decision(result.final_text)
                valid_audit = _is_current_evidence_decision(decision)
                if valid_audit:
                    state.collaboration.apply_consensus(decision, 0)
                    state.collaboration.accepted = False
                state.collaboration.set_task(
                    task_id,
                    "done" if valid_audit else "blocked",
                    evidence=_result_evidence(result),
                )
                self._checkpoint(
                    state, f"{artifact}_complete", on_checkpoint, event_callback
                )
        return cross_review_a, cross_review_b

    def _repair_invalid_cross_reviews(
        self,
        *,
        task: str,
        agent_a: BaseCLIAdapter,
        agent_b: BaseCLIAdapter,
        proposal_a: AgentRunResult,
        proposal_b: AgentRunResult,
        cross_review_a: AgentRunResult,
        cross_review_b: AgentRunResult,
        state: WorkflowCheckpoint,
        on_event: EventCallback | None,
        on_checkpoint: CheckpointCallback | None,
    ) -> tuple[AgentRunResult, AgentRunResult]:
        audits = {
            "cross_review_a": (
                cross_review_a,
                agent_a,
                agent_b,
                "claude",
                proposal_a,
                proposal_b,
                "codex",
                "cross-review-a",
            ),
            "cross_review_b": (
                cross_review_b,
                agent_b,
                agent_a,
                "codex",
                proposal_b,
                proposal_a,
                "claude",
                "cross-review-b",
            ),
        }
        invalid = {
            artifact: values
            for artifact, values in audits.items()
            if not _is_current_evidence_decision(
                parse_consensus_decision(values[0].final_text)
            )
        }
        if not invalid:
            return cross_review_a, cross_review_b

        for values in invalid.values():
            state.collaboration.set_task(values[-1], "in_progress")
        self._sync_collaboration(state, on_checkpoint, on_event)
        names = "、".join(values[0].agent for values in invalid.values())
        self._phase(
            on_event,
            f"{names} 的交叉审核格式不完整，正在自动修复结构化证据",
            step_id="cross_review_repair",
        )
        event_callback = self._locked_event_callback(on_event)
        repaired: dict[str, AgentRunResult] = {}
        errors: list[Exception] = []
        with ThreadPoolExecutor(
            max_workers=len(invalid),
            thread_name_prefix="multiagent-cross-review-repair",
        ) as executor:
            futures = {}
            for artifact, values in invalid.items():
                (
                    previous,
                    adapter,
                    candidate_adapter,
                    name,
                    own,
                    candidate,
                    _recipient,
                    _task_id,
                ) = values
                futures[
                    executor.submit(
                        adapter.run,
                        self._cross_review_prompt(
                            task=task,
                            name=name,
                            adapter=adapter,
                            candidate_adapter=candidate_adapter,
                            own_proposal=own,
                            candidate_proposal=candidate,
                            previous_review=previous.final_text,
                        ),
                        workspace=self.settings.workspace,
                        mode="read",
                        session_id=previous.session_id,
                        on_event=event_callback,
                        step_id=f"{artifact}_repair",
                    )
                ] = artifact
            for future in as_completed(futures):
                try:
                    repaired[futures[future]] = future.result()
                except Exception as exc:
                    errors.append(exc)

        results = {
            "cross_review_a": cross_review_a,
            "cross_review_b": cross_review_b,
        }
        repair_failed = False
        for artifact, values in invalid.items():
            candidate = repaired.get(artifact)
            if candidate is None:
                state.collaboration.set_task(values[-1], "blocked")
                repair_failed = True
                continue
            decision = parse_consensus_decision(candidate.final_text)
            if not _is_current_evidence_decision(decision) or not _preserves_review_ids(
                values[0].final_text,
                decision,
            ):
                state.collaboration.set_task(values[-1], "blocked")
                repair_failed = True
                continue
            (
                _previous,
                _adapter,
                _candidate_adapter,
                sender,
                _own,
                _candidate,
                recipient,
                task_id,
            ) = values
            results[artifact] = candidate
            state.set_artifact(artifact, candidate)
            state.collaboration.post(
                sender,
                recipient,
                "review",
                candidate.final_text,
            )
            state.collaboration.apply_consensus(decision, 0)
            state.collaboration.accepted = False
            state.collaboration.set_task(
                task_id,
                "done",
                evidence=_result_evidence(candidate),
            )
            self._checkpoint(
                state,
                f"{artifact}_format_repaired",
                on_checkpoint,
                event_callback,
            )
        if repair_failed:
            self._sync_collaboration(state, on_checkpoint, event_callback)
        if errors:
            raise errors[0]
        return results["cross_review_a"], results["cross_review_b"]

    @staticmethod
    def _assert_valid_cross_reviews(
        cross_review_a: AgentRunResult,
        cross_review_b: AgentRunResult,
    ) -> None:
        invalid = [
            result.agent
            for result in (cross_review_a, cross_review_b)
            if not _is_current_evidence_decision(
                parse_consensus_decision(result.final_text)
            )
        ]
        if invalid:
            raise BridgeError(
                f"{'、'.join(invalid)} 的交叉审核不符合结构化证据协议"
                "（已自动尝试格式修复）"
            )

    def _synthesize_unified_proposal(
        self,
        *,
        task: str,
        integrator: BaseCLIAdapter,
        integrator_name: str,
        proposal_a: AgentRunResult,
        proposal_b: AgentRunResult,
        cross_review_a: AgentRunResult,
        cross_review_b: AgentRunResult,
        state: WorkflowCheckpoint,
        on_event: EventCallback | None,
        on_checkpoint: CheckpointCallback | None,
    ) -> AgentRunResult:
        state.collaboration.set_task("unified-plan", "in_progress")
        self._sync_collaboration(state, on_checkpoint, on_event)
        self._phase(
            on_event,
            f"{integrator.display_name} 临时整合双方统一方案 v1",
            step_id="unified_proposal",
        )
        unified = integrator.run(
            self._identity_prompt(
                integrator_name,
                UNIFIED_PLAN_PROMPT.format(
                    task=task,
                    agent_a_proposal=proposal_a.final_text,
                    agent_b_proposal=proposal_b.final_text,
                    agent_a_review=cross_review_a.final_text,
                    agent_b_review=cross_review_b.final_text,
                ),
            ),
            workspace=self.settings.workspace,
            mode="read",
            session_id=(
                proposal_a.session_id if integrator_name == "claude" else proposal_b.session_id
            ),
            on_event=on_event,
            step_id="unified_proposal",
        )
        state.set_artifact("unified_proposal", unified)
        state.collaboration.set_canonical_proposal(
            unified.final_text,
            author=integrator_name,
            version=1,
        )
        state.collaboration.post(
            integrator_name,
            "codex" if integrator_name == "claude" else "claude",
            "revision",
            unified.final_text,
        )
        state.collaboration.set_task(
            "unified-plan", "done", evidence=_result_evidence(unified)
        )
        self._checkpoint(state, "unified_proposal_complete", on_checkpoint, on_event)
        return unified

    def _reach_consensus(
        self,
        *,
        task: str,
        unified_proposal: AgentRunResult,
        proposal_a: AgentRunResult,
        proposal_b: AgentRunResult,
        cross_review_a: AgentRunResult,
        cross_review_b: AgentRunResult,
        on_event: EventCallback | None,
        state: WorkflowCheckpoint,
        on_checkpoint: CheckpointCallback | None,
    ) -> tuple[
        AgentRunResult,
        AgentRunResult,
        tuple[AgentRunResult, ...],
    ]:
        collaboration = state.collaboration
        reviews = list(_checkpoint_consensus_reviews(state))
        planning_context = _planning_context(
            proposal_a,
            proposal_b,
            cross_review_a,
            cross_review_b,
            None,
        )
        while True:
            version = max(collaboration.proposal_version, 1)
            author_name = _agent_key(unified_proposal.agent)
            if author_name not in {"claude", "codex"}:
                author_name = self.settings.executor
            auditor_name = "codex" if author_name == "claude" else "claude"
            author = self.adapters[author_name]
            auditor = self.adapters[auditor_name]
            review_key = f"consensus_review_v{version}"
            consensus_audit = state.artifact(review_key)
            if consensus_audit is None:
                next_round = collaboration.consensus_round + 1
                if next_round > self.settings.max_consensus_rounds:
                    raise ConsensusLimitReached(
                        _consensus_limit_message(collaboration)
                    )
                collaboration.set_task("plan-review", "in_progress")
                self._sync_collaboration(state, on_checkpoint, on_event)
                self._phase(
                    on_event,
                    (
                        f"{auditor.display_name} 审核统一方案 v{version} "
                        f"· 共识轮次 {next_round}/{self.settings.max_consensus_rounds}"
                    ),
                    step_id=review_key,
                )
                consensus_audit = auditor.run(
                    self._identity_prompt(
                        auditor_name,
                        CONSENSUS_REVIEW_PROMPT.format(
                            task=task,
                            auditor_label=(
                                "Agent A" if auditor_name == "claude" else "Agent B"
                            ),
                            auditor_name=auditor.display_name,
                            proposal_version=version,
                            unified_proposal=unified_proposal.final_text,
                        ),
                    ),
                    workspace=self.settings.workspace,
                    mode="read",
                    session_id=(
                        proposal_a.session_id
                        if auditor_name == "claude"
                        else proposal_b.session_id
                    ),
                    on_event=on_event,
                    step_id=review_key,
                )
                state.set_artifact(review_key, consensus_audit)
                reviews.append(consensus_audit)
                decision = parse_consensus_decision(consensus_audit.final_text)
                if not _is_current_evidence_decision(decision):
                    raise BridgeError(
                        f"{auditor.display_name} 的统一方案审核不符合共识协议"
                    )
                if decision.proposal_version != version:
                    raise BridgeError(
                        f"{auditor.display_name} 审核的方案版本为 "
                        f"v{decision.proposal_version}，当前版本是 v{version}"
                    )
                collaboration.apply_consensus(decision, next_round)
                collaboration.approve_canonical(author_name, unified_proposal.final_text)
                if decision.accepted:
                    collaboration.approve_canonical(
                        auditor_name, unified_proposal.final_text
                    )
                collaboration.accepted = (
                    decision.accepted
                    and collaboration.has_unanimous_approval({"claude", "codex"})
                    and not collaboration.blocking_issues
                )
                collaboration.post(
                    auditor_name,
                    author_name,
                    "review",
                    consensus_audit.final_text,
                )
                collaboration.set_task(
                    "plan-review",
                    "done" if collaboration.accepted else "blocked",
                    evidence=_result_evidence(consensus_audit),
                )
                self._checkpoint(
                    state, f"{review_key}_complete", on_checkpoint, on_event
                )
            else:
                decision = parse_consensus_decision(consensus_audit.final_text)
                if (
                    not _is_current_evidence_decision(decision)
                    or decision.proposal_version != version
                ):
                    raise BridgeError("检查点中的统一方案审核无效或版本不一致")
                if consensus_audit not in reviews:
                    reviews.append(consensus_audit)

            if collaboration.accepted and collaboration.has_unanimous_approval(
                {"claude", "codex"}
            ):
                collaboration.set_task(
                    "plan-review",
                    "done",
                    evidence=(
                        f"proposal v{version} approved by claude and codex "
                        f"digest={collaboration.proposal_digest[:12]}"
                    ),
                )
                self._phase(
                    on_event,
                    (
                        f"{author.display_name} 与 {auditor.display_name} "
                        f"已共同批准统一方案 v{version}"
                    ),
                    step_id="consensus",
                )
                self._checkpoint(state, "consensus_reached", on_checkpoint, on_event)
                return unified_proposal, consensus_audit, tuple(reviews)

            if collaboration.consensus_round >= self.settings.max_consensus_rounds:
                raise ConsensusLimitReached(_consensus_limit_message(collaboration))

            next_version = version + 1
            state.consensus_revisions += 1
            collaboration.set_task("unified-plan", "in_progress")
            self._sync_collaboration(state, on_checkpoint, on_event)
            self._phase(
                on_event,
                (
                    f"{auditor.display_name} 接棒整合统一方案 v{next_version} "
                    "· 双方轮换发言"
                ),
                step_id=f"consensus_revision_v{next_version}",
            )
            unified_proposal = auditor.run(
                self._identity_prompt(
                    auditor_name,
                    CONSENSUS_REVISION_PROMPT.format(
                        task=task,
                        unified_proposal=unified_proposal.final_text,
                        consensus_review=consensus_audit.final_text,
                        planning_context=planning_context,
                    ),
                ),
                workspace=self.settings.workspace,
                mode="read",
                session_id=(
                    proposal_a.session_id
                    if auditor_name == "claude"
                    else proposal_b.session_id
                ),
                on_event=on_event,
                step_id=f"consensus_revision_v{next_version}",
            )
            state.set_artifact("unified_proposal", unified_proposal)
            collaboration.set_canonical_proposal(
                unified_proposal.final_text,
                author=auditor_name,
                version=next_version,
            )
            collaboration.post(
                auditor_name,
                author_name,
                "revision",
                unified_proposal.final_text,
            )
            collaboration.set_task(
                "unified-plan", "done", evidence=_result_evidence(unified_proposal)
            )
            self._checkpoint(
                state,
                f"consensus_revision_v{next_version}_complete",
                on_checkpoint,
                on_event,
            )

    def _identity_prompt(
        self,
        agent_name: str,
        prompt: str,
        *,
        share_state: bool = True,
    ) -> str:
        identity = (
            self.settings.agent_a_identity
            if agent_name == "claude"
            else self.settings.agent_b_identity
        )
        identity_block = (
            "<multiagent_identity>\n"
            f"{identity}\n"
            "该身份在本次会话中持续有效；当前阶段的具体权限和任务以下文为准。\n"
            "</multiagent_identity>"
        )
        collaboration = getattr(self, "_collaboration", None)
        if share_state and isinstance(collaboration, CollaborationState):
            shared = (
                "<multiagent_shared_state>\n"
                f"{collaboration.shared_context()}\n"
                "</multiagent_shared_state>"
            )
            return f"{identity_block}\n\n{shared}\n\n{prompt}"
        return f"{identity_block}\n\n{prompt}"

    def _checkpoint(
        self,
        state: WorkflowCheckpoint,
        phase: str,
        callback: CheckpointCallback | None,
        on_event: EventCallback | None,
    ) -> None:
        state.phase = phase
        state.updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if state.implementation_complete and state.change_baseline is not None:
            state.change_summary = summarize_workspace_changes(
                self.settings.workspace,
                state.change_baseline,
            )
        state.workspace_fingerprint = current_workspace_fingerprint(
            self.settings.workspace
        )
        if callback:
            callback(state)
        # Persist before notifying UI subscribers so their follow-up GET cannot
        # observe the previous task-board state.
        self._emit_collaboration(on_event, state.collaboration)
        if on_event:
            on_event(
                AgentEvent(
                    "Bridge",
                    "checkpoint",
                    phase,
                    status="completed",
                    step_id=phase,
                    safe_summary=f"已保存检查点：{phase}",
                    metadata={"phase": phase},
                )
            )
        self._raise_if_stopping()

    def _sync_collaboration(
        self,
        state: WorkflowCheckpoint,
        callback: CheckpointCallback | None,
        on_event: EventCallback | None,
    ) -> None:
        """Persist and publish task-board transitions without advancing phase."""
        state.updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if callback:
            callback(state)
        self._emit_collaboration(on_event, state.collaboration)
        self._raise_if_stopping()

    @staticmethod
    def _emit_collaboration(
        on_event: EventCallback | None,
        collaboration: CollaborationState,
    ) -> None:
        if on_event:
            import json

            on_event(
                AgentEvent(
                    "Bridge",
                    "collaboration",
                    json.dumps(collaboration.to_dict(), ensure_ascii=False),
                    status="updated",
                    safe_summary="共享任务与争议状态已更新",
                )
            )

    def _verify(self, on_event: EventCallback | None):
        if self.settings.verification_commands:
            self._phase(
                on_event,
                "Bridge 运行独立验证命令",
                step_id="verification",
            )
        return run_verifications(
            self.settings.verification_commands,
            workspace=self.settings.workspace,
            on_event=on_event,
            should_stop=self._stop_requested,
        )

    def _stop_requested(self) -> bool:
        return any(
            bool(getattr(adapter, "stop_requested", False))
            for adapter in self.adapters.values()
        )

    def _raise_if_stopping(self) -> None:
        if self._stop_requested():
            raise KeyboardInterrupt

    @staticmethod
    def _phase(
        on_event: EventCallback | None,
        text: str,
        *,
        step_id: str = "",
    ) -> None:
        if on_event:
            on_event(
                AgentEvent(
                    "Bridge",
                    "phase",
                    text,
                    status="in_progress",
                    step_id=step_id,
                    safe_summary=text,
                )
            )

    @staticmethod
    def _locked_event_callback(
        on_event: EventCallback | None,
    ) -> EventCallback | None:
        if on_event is None:
            return None
        lock = threading.Lock()

        def emit(event: AgentEvent) -> None:
            with lock:
                on_event(event)

        return emit

    @staticmethod
    def _event(
        on_event: EventCallback | None,
        source: str,
        kind: str,
        text: str,
    ) -> None:
        if on_event:
            on_event(AgentEvent(source, kind, text))


def _result_evidence(result: AgentRunResult) -> str:
    session = f"session={result.session_id}" if result.session_id else "session=unknown"
    return f"{result.agent} {session}"


def _verification_evidence(results) -> str:
    if not results:
        return "未配置验证命令"
    passed = sum(result.passed for result in results)
    return f"verification {passed}/{len(results)} passed"


def _agent_key(display_name: str) -> str:
    lowered = display_name.strip().lower()
    if "claude" in lowered:
        return "claude"
    if "codex" in lowered:
        return "codex"
    return ""


def _is_current_evidence_decision(decision: ConsensusDecision) -> bool:
    return (
        decision.valid
        and decision.structured
        and decision.protocol == EVIDENCE_CONSENSUS_PROTOCOL
        and bool(decision.requirements)
        and all(requirement.evidence for requirement in decision.requirements)
        and all(
            issue.evidence
            and (issue.status != "resolved" or bool(issue.resolution))
            for issue in decision.issues
        )
    )


def _preserves_review_ids(
    previous_review: str,
    repaired_decision: ConsensusDecision,
) -> bool:
    previous_ids = {
        match.upper()
        for match in re.findall(
            r"\b[AB]-(?:REQ|ISSUE)-[A-Z0-9_-]+\b",
            previous_review,
            flags=re.IGNORECASE,
        )
    }
    repaired_ids = {
        item.id.upper()
        for item in (*repaired_decision.requirements, *repaired_decision.issues)
    }
    return previous_ids <= repaired_ids


def _checkpoint_consensus_reviews(
    state: WorkflowCheckpoint,
) -> tuple[AgentRunResult, ...]:
    pairs: list[tuple[int, AgentRunResult]] = []
    for name, result in state.artifacts.items():
        prefix = "consensus_review_v"
        if not name.startswith(prefix):
            continue
        suffix = name[len(prefix) :]
        if suffix.isdigit():
            pairs.append((int(suffix), result))
    pairs.sort(key=lambda item: item[0])
    return tuple(result for _, result in pairs)


def _planning_context(
    proposal_a: AgentRunResult | None,
    proposal_b: AgentRunResult | None,
    cross_review_a: AgentRunResult | None,
    cross_review_b: AgentRunResult | None,
    consensus_review: AgentRunResult | None,
) -> str:
    sections = (
        ("Agent A 独立方案", proposal_a),
        ("Agent B 独立方案", proposal_b),
        ("Agent A 对 Agent B 的审核", cross_review_a),
        ("Agent B 对 Agent A 的审核", cross_review_b),
        ("最新统一方案审核", consensus_review),
    )
    return "\n\n".join(
        f"## {title}\n{result.final_text}"
        for title, result in sections
        if result is not None
    ) or "未启用双 Agent 方案协作"


def _consensus_limit_message(collaboration: CollaborationState) -> str:
    blockers = collaboration.blocking_issues
    details = "；".join(
        f"{issue.id}[{issue.severity}] {issue.problem}" for issue in blockers[:3]
    )
    suffix = f"；未解决阻塞：{details}" if details else ""
    return (
        "对等方案协商已走完 max_consensus_rounds，双方仍未批准同一方案版本；"
        f"任务未进入实施阶段{suffix}"
    )


def _ensure_equal_collaboration_tasks(
    collaboration: CollaborationState,
    executor: str,
) -> None:
    validator = "codex" if executor == "claude" else "claude"
    tasks = (
        ("plan", "Agent A 独立提出方案", "claude", "proposal_a"),
        ("requirements", "Agent B 独立提出方案", "codex", "proposal_b"),
        ("cross-review-a", "Agent A 审核 Agent B 方案", "claude", "cross_review"),
        ("cross-review-b", "Agent B 审核 Agent A 方案", "codex", "cross_review"),
        ("unified-plan", "整合双方统一方案", "both", "unified_plan"),
        ("plan-review", "双方确认同一方案版本", "both", "consensus"),
        ("implementation", "按统一方案实施", executor, "implementation"),
        ("verification", "运行独立验证", "bridge", "verification"),
        ("code-review", "对等 Agent 验收实现与证据", validator, "review"),
    )
    for task_id, title, owner, phase in tasks:
        if task_id not in collaboration.tasks:
            collaboration.add_task(task_id, title, owner, phase=phase)
