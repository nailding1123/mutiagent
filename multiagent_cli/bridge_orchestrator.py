from __future__ import annotations

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
    PlanDecision,
)
from .checkpoints import WorkflowCheckpoint
from .collaboration import CollaborationState
from .consensus import parse_consensus_decision
from .reviews import format_review_for_revision, parse_review_decision
from .verification import (
    format_verification_results,
    run_verifications,
    verifications_passed,
)
from .workspace_state import (
    capture_workspace,
    format_snapshot,
    workspace_fingerprint,
)


EventCallback = Callable[[AgentEvent], None]
PlanConfirmation = Callable[
    [AgentRunResult, AgentRunResult, AgentRunResult, int], PlanDecision
]
CheckpointCallback = Callable[[WorkflowCheckpoint], None]


PROPOSAL_PROMPT = """你是这次开发任务的主方案 Agent。请以只读方式分析需求和当前工作区，此阶段不要修改任何文件。

请输出具体方案，至少包含：
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


INDEPENDENT_REQUIREMENT_PROMPT = """你是独立的需求分析 Agent。此时不要假设或猜测另一位 Agent 会采用什么方案，也不要修改任何文件。

仅根据原始需求和当前工作区，独立输出：
1. 用户真正要达成的目标；
2. 明确约束、隐含约束、边界和非目标；
3. 关键用户场景、异常路径、兼容性、安全性和数据一致性风险；
4. 可逐项验证的验收标准；
5. 你建议采用的独立解决方案，包括组件、数据流和测试策略；
6. 必须向用户澄清的歧义。如果没有歧义也请明确说明。

原始需求：
<task>
{task}
</task>"""


PROPOSAL_REVIEW_PROMPT = """你是方案审查 Agent。你已经独立分析过需求，现在请把独立分析与主 Agent 的方案逐项比较，不要修改任何文件。

重点检查需求理解差异、遗漏场景、无依据扩张、架构适配、失败路径、兼容性、安全性、数据一致性以及测试计划是否足以证明完成。

只输出一个 JSON 对象，不要使用 Markdown 代码块或额外文字。每条需求和争议必须有稳定 ID；evidence 必须引用方案章节、工作区文件或可执行测试，不能只写“已考虑”：
{{
  "protocol": "mutiagent.consensus.v2",
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
      "id": "REQ-001",
      "text": "可独立验收的原始需求",
      "covered": true,
      "evidence": ["主方案：测试计划第 2 项", "tests/test_example.py::test_case"]
    }}
  ],
  "issues": [
    {{
      "id": "ISSUE-001",
      "severity": "P0、P1、P2 或 P3",
      "requirement": "REQ-001",
      "problem": "具体分歧或风险",
      "status": "open、resolved 或 wont_fix",
      "resolution": "解决方式或拒绝理由",
      "evidence": ["src/example.py:42", "方案：失败路径"]
    }}
  ],
  "agreements": ["双方已经一致的事项"],
  "remaining_disagreements": ["尚未解决的具体分歧"],
  "required_revisions": ["主 Agent 下一轮必须完成的调整"]
}}

只有五项 criteria 全部为 true、所有 requirements 都 covered 且有 evidence、所有 P0/P1 issue 都 resolved 且有 evidence，并且 remaining_disagreements 和 required_revisions 均为空时，verdict 才能是 accept；否则必须是 revise。

原始需求：
<task>
{task}
</task>

你的独立需求分析：
<requirement_analysis>
{requirement_analysis}
</requirement_analysis>

主 Agent 的解决方案：
<proposal>
{proposal}
</proposal>"""


PROPOSAL_REVISION_PROMPT = """请保持只读，不要修改文件。根据独立需求分析、方案审查和用户反馈，重新输出一份完整的修订方案与验收标准。

原始需求：
<task>{task}</task>

独立需求分析：
<requirement_analysis>{requirement_analysis}</requirement_analysis>

方案审查：
<proposal_review>{proposal_review}</proposal_review>

用户反馈：
<user_feedback>{user_feedback}</user_feedback>"""


CONSENSUS_REVISION_PROMPT = """你正在与另一位 Agent 协商开发方案。请保持只读，不要修改文件。

请读取副 Agent 的独立方案和最新审查意见，对自己的方案逐项调整：
1. 采纳有依据的意见并落实到完整方案中；
2. 对不采纳的意见给出基于当前代码的具体理由；
3. 不要只输出差异，重新输出一份可以直接实施的完整方案；
4. 保留明确的验收标准和测试计划。

原始需求：
<task>{task}</task>

副 Agent 的独立需求分析与建议方案：
<requirement_analysis>{requirement_analysis}</requirement_analysis>

副 Agent 对当前主方案的审查：
<proposal_review>{proposal_review}</proposal_review>"""


IMPLEMENT_PROMPT = """你是这次开发任务的主执行 Agent。请综合原始需求、修订后的方案、独立需求分析和方案审查，在当前工作区实际完成任务。

要求：
1. 检查项目约定、现有代码和 Git 状态；
2. 使用原生 CLI 工具实际修改代码，不要只给建议；
3. 保留与任务无关的用户改动，不要重置、覆盖或提交 Git；
4. 运行与风险相称的测试或检查；
5. 最后说明修改内容、测试结果、未采纳意见及原因。

原始需求：
<task>{task}</task>

主方案：
<proposal>{proposal}</proposal>

独立需求分析：
<requirement_analysis>{requirement_analysis}</requirement_analysis>

方案审查：
<proposal_review>{proposal_review}</proposal_review>"""


DIRECT_LEAD_PROMPT = """你是这次开发任务的主执行 Agent。请直接在当前工作区完成任务。

检查项目约定、现有代码和 Git 状态；实际修改代码并运行必要测试；保留无关的用户改动；不要重置或提交 Git。最后说明修改和测试结果。

原始需求：
<task>{task}</task>"""


REVIEW_PROMPT = """你是独立代码验收 Agent。请以只读方式检查当前工作区，不要修改任何文件。

请结合原始需求、独立需求分析、主方案、方案审查、主 Agent 实施总结、任务前 Git 基线、当前 Git diff 和桥接器真实验证日志进行验收。

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

独立需求分析：
<requirement_analysis>{requirement_analysis}</requirement_analysis>

主方案：
<proposal>{proposal}</proposal>

方案审查：
<proposal_review>{proposal_review}</proposal_review>

主 Agent 实施总结：
<lead_summary>{lead_summary}</lead_summary>

任务开始前的工作区基线：
<baseline>{baseline}</baseline>

桥接器独立验证结果：
<verification>{verification}</verification>"""


REVISION_PROMPT = """代码 Reviewer 和桥接器验证给出了以下反馈。请检查当前工作区，修复所有成立的问题并重新运行必要测试；不要只解释，不要覆盖无关用户改动。

原始需求：
<task>{task}</task>

独立需求分析：
<requirement_analysis>{requirement_analysis}</requirement_analysis>

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
            self.settings.requirement_review
            and self.settings.plan_approval
            and confirm_plan is None
        ):
            raise BridgeError("启用了方案确认，但调用方没有提供确认处理器")

        lead_name = self.settings.lead
        reviewer_name = "codex" if lead_name == "claude" else "claude"
        lead = self.adapters[lead_name]
        reviewer = self.adapters[reviewer_name]

        if checkpoint is None:
            baseline = capture_workspace(self.settings.workspace)
            collaboration = CollaborationState.create(
                lead=lead_name,
                reviewer=reviewer_name,
                requirement_review=self.settings.requirement_review,
            )
            state = WorkflowCheckpoint(
                task=task,
                workspace=str(self.settings.workspace),
                lead=lead_name,
                baseline=baseline,
                collaboration=collaboration,
            )
            self._checkpoint(state, "initialized", on_checkpoint, on_event)
        else:
            state = checkpoint
            baseline = state.baseline or capture_workspace(self.settings.workspace)
            collaboration = state.collaboration
            current = workspace_fingerprint(
                capture_workspace(self.settings.workspace), self.settings.workspace
            )
            if state.workspace_fingerprint and current != state.workspace_fingerprint:
                raise BridgeError(
                    "工作区在上一个精确检查点之后发生变化，已拒绝自动续跑；"
                    "请检查任务 worktree，确认改动后重新开始或恢复原状态"
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
                "工作区在任务开始前已有未提交改动，Reviewer 将收到 baseline。",
            )

        proposal = state.artifact("proposal")
        requirement_analysis = state.artifact("requirement_analysis")
        proposal_review = state.artifact("proposal_review")
        if self.settings.requirement_review:
            if proposal is None:
                collaboration.set_task("plan", "in_progress")
                self._phase(on_event, f"{lead.display_name} 独立提出只读方案")
                proposal = lead.run(
                    self._identity_prompt("lead", PROPOSAL_PROMPT.format(task=task)),
                    workspace=self.settings.workspace,
                    mode="read",
                    on_event=on_event,
                )
                state.set_artifact("proposal", proposal)
                collaboration.proposal_version = max(
                    collaboration.proposal_version, 1
                )
                collaboration.set_task(
                    "plan", "done", evidence=_result_evidence(proposal)
                )
                collaboration.post(
                    lead_name, reviewer_name, "proposal", proposal.final_text
                )
                self._checkpoint(state, "proposal_complete", on_checkpoint, on_event)

            if requirement_analysis is None:
                collaboration.set_task("requirements", "in_progress")
                self._phase(on_event, f"{reviewer.display_name} 独立解析需求")
                requirement_analysis = reviewer.run(
                    self._identity_prompt(
                        "reviewer",
                        INDEPENDENT_REQUIREMENT_PROMPT.format(task=task),
                        share_state=False,
                    ),
                    workspace=self.settings.workspace,
                    mode="read",
                    on_event=on_event,
                )
                state.set_artifact("requirement_analysis", requirement_analysis)
                collaboration.set_task(
                    "requirements",
                    "done",
                    evidence=_result_evidence(requirement_analysis),
                )
                collaboration.post(
                    reviewer_name,
                    lead_name,
                    "analysis",
                    requirement_analysis.final_text,
                )
                self._checkpoint(
                    state, "requirement_analysis_complete", on_checkpoint, on_event
                )

            if proposal_review is None:
                collaboration.set_task("plan-review", "in_progress")
                self._phase(
                    on_event,
                    f"{reviewer.display_name} 比较需求理解并审查主方案",
                )
                proposal_review = reviewer.run(
                    self._identity_prompt(
                        "reviewer",
                        PROPOSAL_REVIEW_PROMPT.format(
                            task=task,
                            requirement_analysis=requirement_analysis.final_text,
                            proposal=proposal.final_text,
                        ),
                    ),
                    workspace=self.settings.workspace,
                    mode="read",
                    session_id=requirement_analysis.session_id,
                    on_event=on_event,
                )
                state.set_artifact("proposal_review", proposal_review)
                decision = parse_consensus_decision(proposal_review.final_text)
                if decision.valid and decision.structured:
                    collaboration.apply_consensus(decision, state.consensus_revisions)
                collaboration.set_task(
                    "plan-review",
                    "done" if decision.accepted else "blocked",
                    evidence=_result_evidence(proposal_review),
                )
                collaboration.post(
                    reviewer_name,
                    lead_name,
                    "review",
                    proposal_review.final_text,
                )
                self._checkpoint(
                    state, "proposal_review_complete", on_checkpoint, on_event
                )

            if self.settings.consensus:
                proposal, proposal_review, state.consensus_revisions = (
                    self._reach_consensus(
                        task=task,
                        lead=lead,
                        reviewer=reviewer,
                        requirement_analysis=requirement_analysis,
                        proposal=proposal,
                        proposal_review=proposal_review,
                        revisions_used=state.consensus_revisions,
                        on_event=on_event,
                        state=state,
                        on_checkpoint=on_checkpoint,
                    )
                )

            while (
                self.settings.plan_approval
                and confirm_plan is not None
                and not state.plan_approved
            ):
                decision = confirm_plan(
                    proposal,
                    requirement_analysis,
                    proposal_review,
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
                if decision.action != "revise":
                    raise ValueError(f"未知方案确认操作：{decision.action}")
                if state.plan_revisions >= self.settings.max_plan_revisions:
                    raise BridgeCancelled("方案修订次数已达到上限，任务未实施")
                state.plan_revisions += 1
                self._phase(on_event, f"{lead.display_name} 根据反馈修订方案")
                proposal = lead.run(
                    self._identity_prompt(
                        "lead",
                        PROPOSAL_REVISION_PROMPT.format(
                            task=task,
                            requirement_analysis=requirement_analysis.final_text,
                            proposal_review=proposal_review.final_text,
                            user_feedback=decision.feedback or "请按审查意见修订",
                        ),
                    ),
                    workspace=self.settings.workspace,
                    mode="read",
                    session_id=proposal.session_id,
                    on_event=on_event,
                )
                state.set_artifact("proposal", proposal)
                collaboration.proposal_version += 1
                collaboration.post(
                    lead_name, reviewer_name, "revision", proposal.final_text
                )
                self._checkpoint(
                    state, "user_plan_revision_complete", on_checkpoint, on_event
                )
                self._phase(on_event, f"{reviewer.display_name} 复审修订方案")
                proposal_review = reviewer.run(
                    self._identity_prompt(
                        "reviewer",
                        PROPOSAL_REVIEW_PROMPT.format(
                            task=task,
                            requirement_analysis=requirement_analysis.final_text,
                            proposal=proposal.final_text,
                        ),
                    ),
                    workspace=self.settings.workspace,
                    mode="read",
                    session_id=(
                        proposal_review.session_id
                        or requirement_analysis.session_id
                    ),
                    on_event=on_event,
                )
                state.set_artifact("proposal_review", proposal_review)
                consensus_decision = parse_consensus_decision(
                    proposal_review.final_text
                )
                if consensus_decision.valid and consensus_decision.structured:
                    collaboration.apply_consensus(
                        consensus_decision, state.consensus_revisions
                    )
                collaboration.post(
                    reviewer_name,
                    lead_name,
                    "review",
                    proposal_review.final_text,
                )
                self._checkpoint(
                    state, "user_plan_review_complete", on_checkpoint, on_event
                )
                if self.settings.consensus:
                    proposal, proposal_review, state.consensus_revisions = (
                        self._reach_consensus(
                            task=task,
                            lead=lead,
                            reviewer=reviewer,
                            requirement_analysis=requirement_analysis,
                            proposal=proposal,
                            proposal_review=proposal_review,
                            revisions_used=state.consensus_revisions,
                            on_event=on_event,
                            state=state,
                            on_checkpoint=on_checkpoint,
                        )
                    )

            if not self.settings.plan_approval:
                state.plan_approved = True
            lead_result = state.artifact("lead_result")
            if not state.implementation_complete or lead_result is None:
                collaboration.set_task("implementation", "in_progress")
                self._phase(on_event, f"{lead.display_name} 按确认方案开始实施")
                lead_result = lead.run(
                    self._identity_prompt(
                        "lead",
                        IMPLEMENT_PROMPT.format(
                            task=task,
                            proposal=proposal.final_text,
                            requirement_analysis=requirement_analysis.final_text,
                            proposal_review=proposal_review.final_text,
                        ),
                    ),
                    workspace=self.settings.workspace,
                    mode="write",
                    on_event=on_event,
                )
                state.set_artifact("lead_result", lead_result)
                state.implementation_complete = True
                collaboration.set_task(
                    "implementation",
                    "done",
                    evidence=_result_evidence(lead_result),
                )
                collaboration.post(
                    lead_name, reviewer_name, "evidence", lead_result.final_text
                )
                self._checkpoint(
                    state, "implementation_complete", on_checkpoint, on_event
                )
        else:
            lead_result = state.artifact("lead_result")
            if not state.implementation_complete or lead_result is None:
                collaboration.set_task("implementation", "in_progress")
                self._phase(on_event, f"{lead.display_name} 开始实施")
                lead_result = lead.run(
                    self._identity_prompt(
                        "lead", DIRECT_LEAD_PROMPT.format(task=task)
                    ),
                    workspace=self.settings.workspace,
                    mode="write",
                    on_event=on_event,
                )
                state.set_artifact("lead_result", lead_result)
                state.implementation_complete = True
                collaboration.set_task(
                    "implementation",
                    "done",
                    evidence=_result_evidence(lead_result),
                )
                collaboration.post(
                    lead_name, reviewer_name, "evidence", lead_result.final_text
                )
                self._checkpoint(
                    state, "implementation_complete", on_checkpoint, on_event
                )

        proposal_text = proposal.final_text if proposal else "未启用主方案预审"
        analysis_text = (
            requirement_analysis.final_text
            if requirement_analysis
            else "未启用独立需求分析"
        )
        proposal_review_text = (
            proposal_review.final_text if proposal_review else "未启用方案比较审查"
        )
        verification_history = state.verifications
        review_runs = state.reviews
        review_decisions = state.review_decisions

        if state.phase == "complete":
            return BridgeOutcome(
                task=task,
                lead=lead_name,
                lead_result=lead_result,
                proposal=proposal,
                requirement_analysis=requirement_analysis,
                proposal_review=proposal_review,
                reviews=tuple(review_runs),
                review_decisions=tuple(review_decisions),
                verifications=tuple(verification_history),
                baseline=baseline,
                final_snapshot=capture_workspace(self.settings.workspace),
                approved=state.approved,
                collaboration=collaboration,
            )

        if self.settings.review_rounds == 0:
            verification = state.pending_verification
            if not verification:
                collaboration.set_task("verification", "in_progress")
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
                lead=lead_name,
                lead_result=lead_result,
                proposal=proposal,
                requirement_analysis=requirement_analysis,
                proposal_review=proposal_review,
                verifications=tuple(verification_history),
                baseline=baseline,
                final_snapshot=final_snapshot,
                approved=approved,
                collaboration=collaboration,
            )

        approved = state.approved is True
        revised_after_last_review = False
        for round_index in range(state.review_cursor, self.settings.review_rounds):
            verification = state.pending_verification
            if not verification:
                collaboration.set_task("verification", "in_progress")
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
                self._phase(
                    on_event,
                    f"{reviewer.display_name} 进行第 {round_index + 1} 轮结构化代码验收",
                )
                review = reviewer.run(
                    self._identity_prompt(
                        "reviewer",
                        REVIEW_PROMPT.format(
                            task=task,
                            requirement_analysis=analysis_text,
                            proposal=proposal_text,
                            proposal_review=proposal_review_text,
                            lead_summary=lead_result.final_text,
                            baseline=format_snapshot(baseline),
                            verification=format_verification_results(verification),
                        ),
                    ),
                    workspace=self.settings.workspace,
                    mode="read",
                    on_event=on_event,
                )
                decision = parse_review_decision(review.final_text)
                review_runs.append(review)
                review_decisions.append(decision)
                collaboration.apply_code_review(decision, round_index + 1)
                collaboration.post(
                    reviewer_name, lead_name, "review", review.final_text
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

            self._phase(on_event, f"{lead.display_name} 根据审查与验证结果修订")
            lead_result = lead.run(
                self._identity_prompt(
                    "lead",
                    REVISION_PROMPT.format(
                        task=task,
                        requirement_analysis=analysis_text,
                        review=format_review_for_revision(decision, review.final_text),
                        verification=format_verification_results(verification),
                    ),
                ),
                workspace=self.settings.workspace,
                mode="write",
                session_id=lead_result.session_id,
                on_event=on_event,
            )
            state.set_artifact("lead_result", lead_result)
            collaboration.post(
                lead_name, reviewer_name, "revision", lead_result.final_text
            )
            collaboration.set_task(
                "implementation",
                "done",
                evidence=_result_evidence(lead_result),
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
            self._phase(on_event, f"{reviewer.display_name} 进行最终结构化验收")
            final_review = reviewer.run(
                self._identity_prompt(
                    "reviewer",
                    REVIEW_PROMPT.format(
                        task=task,
                        requirement_analysis=analysis_text,
                        proposal=proposal_text,
                        proposal_review=proposal_review_text,
                        lead_summary=lead_result.final_text,
                        baseline=format_snapshot(baseline),
                        verification=format_verification_results(verification),
                    ),
                ),
                workspace=self.settings.workspace,
                mode="read",
                on_event=on_event,
            )
            decision = parse_review_decision(final_review.final_text)
            review_runs.append(final_review)
            review_decisions.append(decision)
            collaboration.apply_code_review(decision, state.review_cursor + 1)
            collaboration.post(
                reviewer_name, lead_name, "review", final_review.final_text
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
            lead=lead_name,
            lead_result=lead_result,
            proposal=proposal,
            requirement_analysis=requirement_analysis,
            proposal_review=proposal_review,
            reviews=tuple(review_runs),
            review_decisions=tuple(review_decisions),
            verifications=tuple(verification_history),
            baseline=baseline,
            final_snapshot=final_snapshot,
            approved=approved,
            collaboration=collaboration,
        )

    def _reach_consensus(
        self,
        *,
        task: str,
        lead: BaseCLIAdapter,
        reviewer: BaseCLIAdapter,
        requirement_analysis: AgentRunResult,
        proposal: AgentRunResult,
        proposal_review: AgentRunResult,
        revisions_used: int,
        on_event: EventCallback | None,
        state: WorkflowCheckpoint,
        on_checkpoint: CheckpointCallback | None,
    ) -> tuple[AgentRunResult, AgentRunResult, int]:
        if (
            revisions_used > 0
            and state.phase == f"consensus_revision_{revisions_used}_complete"
        ):
            state.collaboration.set_task("plan-review", "in_progress")
            self._phase(
                on_event,
                f"{reviewer.display_name} 恢复复审第 {revisions_used} 次自动修订方案",
            )
            proposal_review = reviewer.run(
                self._identity_prompt(
                    "reviewer",
                    PROPOSAL_REVIEW_PROMPT.format(
                        task=task,
                        requirement_analysis=requirement_analysis.final_text,
                        proposal=proposal.final_text,
                    ),
                ),
                workspace=self.settings.workspace,
                mode="read",
                session_id=(
                    proposal_review.session_id or requirement_analysis.session_id
                ),
                on_event=on_event,
            )
            state.set_artifact("proposal_review", proposal_review)
            recovered_decision = parse_consensus_decision(proposal_review.final_text)
            if recovered_decision.valid and recovered_decision.structured:
                state.collaboration.apply_consensus(
                    recovered_decision, revisions_used
                )
            state.collaboration.post(
                "codex" if self.settings.lead == "claude" else "claude",
                self.settings.lead,
                "review",
                proposal_review.final_text,
            )
            state.collaboration.set_task(
                "plan-review",
                "done" if recovered_decision.accepted else "blocked",
                evidence=_result_evidence(proposal_review),
            )
            self._checkpoint(
                state,
                f"consensus_review_{revisions_used}_complete",
                on_checkpoint,
                on_event,
            )
        while True:
            decision = parse_consensus_decision(proposal_review.final_text)
            if not decision.valid:
                raise BridgeError(
                    f"{reviewer.display_name} 的方案审查不符合共识协议，"
                    "无法确认双方是否一致"
                )
            if decision.structured:
                state.collaboration.apply_consensus(decision, revisions_used)
            elif decision.accepted:
                state.collaboration.accepted = not state.collaboration.blocking_issues
            if decision.accepted and state.collaboration.accepted:
                state.collaboration.set_task(
                    "plan-review",
                    "done",
                    evidence=f"proposal v{decision.proposal_version} accepted",
                )
                self._phase(
                    on_event,
                    f"{lead.display_name} 与 {reviewer.display_name} 已达成方案共识",
                )
                self._checkpoint(
                    state, "consensus_reached", on_checkpoint, on_event
                )
                return proposal, proposal_review, revisions_used
            if revisions_used >= self.settings.max_consensus_rounds:
                raise BridgeError(
                    "方案自动协商达到上限仍未达成共识，任务未进入实施阶段；"
                    "请调整需求或提高 max_consensus_rounds"
                )

            revisions_used += 1
            state.consensus_revisions = revisions_used
            state.collaboration.set_task("plan", "in_progress")
            self._phase(
                on_event,
                f"{lead.display_name} 根据副 Agent 方案进行第 "
                f"{revisions_used} 次自动修订",
            )
            proposal = lead.run(
                self._identity_prompt(
                    "lead",
                    CONSENSUS_REVISION_PROMPT.format(
                        task=task,
                        requirement_analysis=requirement_analysis.final_text,
                        proposal_review=proposal_review.final_text,
                    ),
                ),
                workspace=self.settings.workspace,
                mode="read",
                session_id=proposal.session_id,
                on_event=on_event,
            )
            state.set_artifact("proposal", proposal)
            state.collaboration.proposal_version += 1
            state.collaboration.post(
                self.settings.lead,
                "codex" if self.settings.lead == "claude" else "claude",
                "revision",
                proposal.final_text,
            )
            state.collaboration.set_task(
                "plan", "done", evidence=_result_evidence(proposal)
            )
            self._checkpoint(
                state,
                f"consensus_revision_{revisions_used}_complete",
                on_checkpoint,
                on_event,
            )

            state.collaboration.set_task("plan-review", "in_progress")
            self._phase(
                on_event,
                f"{reviewer.display_name} 复审第 {revisions_used} 次自动修订方案",
            )
            proposal_review = reviewer.run(
                self._identity_prompt(
                    "reviewer",
                    PROPOSAL_REVIEW_PROMPT.format(
                        task=task,
                        requirement_analysis=requirement_analysis.final_text,
                        proposal=proposal.final_text,
                    ),
                ),
                workspace=self.settings.workspace,
                mode="read",
                session_id=(
                    proposal_review.session_id or requirement_analysis.session_id
                ),
                on_event=on_event,
            )
            state.set_artifact("proposal_review", proposal_review)
            decision = parse_consensus_decision(proposal_review.final_text)
            if decision.valid and decision.structured:
                state.collaboration.apply_consensus(decision, revisions_used)
            state.collaboration.post(
                "codex" if self.settings.lead == "claude" else "claude",
                self.settings.lead,
                "review",
                proposal_review.final_text,
            )
            state.collaboration.set_task(
                "plan-review",
                "done" if decision.accepted else "blocked",
                evidence=_result_evidence(proposal_review),
            )
            self._checkpoint(
                state,
                f"consensus_review_{revisions_used}_complete",
                on_checkpoint,
                on_event,
            )

    def _identity_prompt(
        self,
        role: str,
        prompt: str,
        *,
        share_state: bool = True,
    ) -> str:
        identity = (
            self.settings.lead_identity
            if role == "lead"
            else self.settings.reviewer_identity
        )
        identity_block = (
            "<mutiagent_identity>\n"
            f"{identity}\n"
            "该身份在本次会话中持续有效；当前阶段的具体权限和任务以下文为准。\n"
            "</mutiagent_identity>"
        )
        collaboration = getattr(self, "_collaboration", None)
        if share_state and isinstance(collaboration, CollaborationState):
            shared = (
                "<mutiagent_shared_state>\n"
                f"{collaboration.shared_context()}\n"
                "</mutiagent_shared_state>"
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
        state.workspace_fingerprint = workspace_fingerprint(
            capture_workspace(self.settings.workspace), self.settings.workspace
        )
        self._emit_collaboration(on_event, state.collaboration)
        if callback:
            callback(state)

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
                )
            )

    def _verify(self, on_event: EventCallback | None):
        if self.settings.verification_commands:
            self._phase(on_event, "Bridge 运行独立验证命令")
        return run_verifications(
            self.settings.verification_commands,
            workspace=self.settings.workspace,
            on_event=on_event,
        )

    @staticmethod
    def _phase(on_event: EventCallback | None, text: str) -> None:
        if on_event:
            on_event(AgentEvent("Bridge", "phase", text))

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
