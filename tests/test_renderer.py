from __future__ import annotations

import io
import json
import time
import unittest

from multiagent_cli.bridge_models import AgentEvent, AgentRunResult, BridgeOutcome
from multiagent_cli.renderer import ConsoleRenderer, _safe_progress_text


class RendererTests(unittest.TestCase):
    def test_clear_screen_only_writes_to_a_tty(self) -> None:
        class TtyStringIO(io.StringIO):
            def isatty(self) -> bool:
                return True

        tty_output = TtyStringIO()
        ConsoleRenderer(color=False, stream=tty_output).clear_screen()
        self.assertEqual(tty_output.getvalue(), "\033[2J\033[H")

        redirected_output = io.StringIO()
        ConsoleRenderer(color=False, stream=redirected_output).clear_screen()
        self.assertEqual(redirected_output.getvalue(), "")

    def test_fixed_tui_uses_alternate_screen_and_renders_shared_tasks(self) -> None:
        class TtyStringIO(io.StringIO):
            def isatty(self) -> bool:
                return True

        output = TtyStringIO()
        renderer = ConsoleRenderer(
            color=False, stream=output, width=72, tui=True
        )
        renderer.begin_run("run-1")
        renderer.event(
            AgentEvent(
                "Bridge",
                "collaboration",
                json.dumps(
                    {
                        "tasks": [
                            {
                                "id": "plan",
                                "title": "提出实施方案",
                                "owner": "claude",
                                "status": "in_progress",
                            }
                        ],
                        "issues": [],
                        "messages": [],
                    },
                    ensure_ascii=False,
                ),
            )
        )
        renderer.close()

        rendered = output.getvalue()
        self.assertIn("\033[?1049h", rendered)
        self.assertIn("提出实施方案", rendered)
        self.assertIn("\033[?1049l", rendered)

    def test_welcome_screen_combines_claude_and_codex_styles(self) -> None:
        output = io.StringIO()
        renderer = ConsoleRenderer(color=False, stream=output, width=80)

        renderer.welcome(
            version="0.5.0",
            workspace="/tmp/example",
            executor="claude",
            review_rounds=2,
            consensus=False,
        )

        rendered = output.getvalue()
        self.assertIn(">_  MultiAgent", rendered)
        self.assertIn("---  *  ---", rendered)
        self.assertIn("│   >_   │", rendered)
        self.assertIn("CLAUDE CODE", rendered)
        self.assertIn("CODEX CLI", rendered)
        self.assertIn("⇄", rendered)
        self.assertIn("/tmp/example", rendered)
        self.assertIn("Agent A     Claude", rendered)
        self.assertIn("Agent B     Codex", rendered)
        self.assertIn("执行协调    claude", rendered)
        self.assertIn("方案共识    关闭", rendered)

    def test_hides_agent_commands_but_keeps_phases_and_final_messages(self) -> None:
        output = io.StringIO()
        renderer = ConsoleRenderer(color=False, stream=output, width=72)

        renderer.event(AgentEvent("Bridge", "phase", "Claude 独立提出方案"))
        renderer.event(
            AgentEvent(
                "Claude",
                "tool",
                'Read: {"file_path":"src/auth/service.py","limit":200}',
            )
        )
        renderer.event(AgentEvent("Claude", "text", "需求理解\n- 保持 API 兼容"))

        rendered = output.getvalue()
        self.assertIn("01  Claude 独立提出方案", rendered)
        self.assertNotIn("src/auth/service.py", rendered)
        self.assertIn("Claude · 回复", rendered)
        self.assertIn("│ 需求理解", rendered)

    def test_verbose_mode_reveals_progress_commands_and_results(self) -> None:
        output = io.StringIO()
        renderer = ConsoleRenderer(
            color=False, verbose=True, stream=output, width=72
        )

        renderer.event(AgentEvent("Claude", "progress", "正在检查项目结构"))
        renderer.event(
            AgentEvent(
                "Claude",
                "tool",
                'Read: {"file_path":"src/auth/service.py"}',
            )
        )
        renderer.event(AgentEvent("Claude", "tool_result", "completed"))

        rendered = output.getvalue()
        self.assertIn("Claude · 中间过程", rendered)
        self.assertIn("正在检查项目结构", rendered)
        self.assertIn("src/auth/service.py", rendered)
        self.assertIn("completed", rendered)

    def test_safe_progress_summarizes_tools_without_leaking_paths(self) -> None:
        class TtyStringIO(io.StringIO):
            def isatty(self) -> bool:
                return True

        output = TtyStringIO()
        renderer = ConsoleRenderer(color=False, stream=output, width=80, tui=False)
        renderer.begin_run("run-1")
        renderer.event(AgentEvent("Bridge", "phase", "Claude 独立提出方案"))
        renderer.event(
            AgentEvent(
                "Claude",
                "tool",
                'Read: {"file_path":"/secret/监控+关注功能优化升级.pdf"}',
            )
        )
        time.sleep(0.25)
        renderer.close()

        rendered = output.getvalue()
        self.assertIn("Claude · 正在读取 PDF", rendered)
        self.assertNotIn("/secret/监控+关注功能优化升级.pdf", rendered)

    def test_safe_progress_can_be_disabled(self) -> None:
        class TtyStringIO(io.StringIO):
            def isatty(self) -> bool:
                return True

        output = TtyStringIO()
        renderer = ConsoleRenderer(
            color=False, stream=output, width=80, tui=False, progress=False
        )
        renderer.begin_run("run-1")
        renderer.event(AgentEvent("Bridge", "phase", "Claude 独立提出方案"))
        renderer.event(
            AgentEvent(
                "Claude",
                "tool",
                'Read: {"file_path":"/secret/监控+关注功能优化升级.pdf"}',
            )
        )
        renderer.close()

        rendered = output.getvalue()
        self.assertIn("Claude 独立提出方案", rendered)
        self.assertNotIn("正在读取 PDF", rendered)
        self.assertNotIn("/secret/监控+关注功能优化升级.pdf", rendered)

    def test_tui_renders_safe_progress_status(self) -> None:
        class TtyStringIO(io.StringIO):
            def isatty(self) -> bool:
                return True

        output = TtyStringIO()
        renderer = ConsoleRenderer(color=False, stream=output, width=80, tui=True)
        renderer.begin_run("run-1")
        renderer.event(AgentEvent("Bridge", "phase", "Claude 独立提出方案"))
        renderer.event(
            AgentEvent(
                "Claude",
                "tool",
                'Read: {"file_path":"/secret/监控+关注功能优化升级.pdf"}',
            )
        )
        renderer.close()

        rendered = output.getvalue()
        self.assertIn("Claude · 正在读取 PDF", rendered)
        self.assertNotIn("/secret/监控+关注功能优化升级.pdf", rendered)

    def test_tui_renders_parallel_agents_with_independent_statuses(self) -> None:
        class TtyStringIO(io.StringIO):
            def isatty(self) -> bool:
                return True

        output = TtyStringIO()
        renderer = ConsoleRenderer(color=False, stream=output, width=88, tui=True)
        renderer.begin_run("run-parallel")
        renderer.event(
            AgentEvent(
                "Bridge",
                "phase",
                "Claude 与 Codex 并行生成初始方案和需求分析",
            )
        )
        renderer.event(
            AgentEvent(
                "Claude",
                "lifecycle",
                "waiting_model",
                status="waiting_model",
                elapsed_seconds=2.0,
                safe_summary="Claude · 等待模型响应",
            )
        )
        renderer.event(
            AgentEvent(
                "Codex",
                "lifecycle",
                "waiting_model",
                status="waiting_model",
                elapsed_seconds=3.0,
                safe_summary="Codex · 等待模型响应",
            )
        )
        renderer.event(
            AgentEvent(
                "Claude",
                "tool",
                "Bash: pytest",
                elapsed_seconds=10.0,
            )
        )
        renderer.event(
            AgentEvent(
                "Codex",
                "tool",
                'Read: {"file_path":"/secret/design.md"}',
                elapsed_seconds=11.0,
            )
        )

        rendered = output.getvalue()
        self.assertIn("Claude · 正在执行检查 · 00:10", rendered)
        self.assertIn("Codex · 正在读取文件 · 00:11", rendered)
        self.assertNotIn("/secret/design.md", rendered)

        renderer.event(
            AgentEvent(
                "Claude",
                "lifecycle",
                "completed",
                status="completed",
                elapsed_seconds=12.0,
                safe_summary="Claude · 已完成本轮响应",
            )
        )
        self.assertTrue(renderer._has_active_agents())
        self.assertIsNotNone(renderer._activity_thread)
        self.assertIn("✓ Claude · 已完成本轮响应", output.getvalue())
        self.assertIn("Codex · 正在读取文件", output.getvalue())

        renderer.event(
            AgentEvent(
                "Codex",
                "lifecycle",
                "completed",
                status="completed",
                elapsed_seconds=14.0,
                safe_summary="Codex · 已完成本轮响应",
            )
        )
        self.assertFalse(renderer._has_active_agents())
        self.assertIsNone(renderer._activity_thread)
        renderer.close()

    def test_tui_prioritizes_live_agents_and_avoids_repeated_full_clears(self) -> None:
        class TtyStringIO(io.StringIO):
            def isatty(self) -> bool:
                return True

        output = TtyStringIO()
        renderer = ConsoleRenderer(color=False, stream=output, width=88, tui=True)
        renderer.begin_run("run-layout")
        renderer.event(
            AgentEvent(
                "Bridge",
                "collaboration",
                json.dumps(
                    {
                        "tasks": [
                            {
                                "id": "plan",
                                "title": "提出实施方案",
                                "owner": "claude",
                                "status": "in_progress",
                            },
                            {
                                "id": "review",
                                "title": "独立审查",
                                "owner": "codex",
                                "status": "pending",
                            },
                        ],
                        "issues": [],
                        "requirements": [],
                        "messages": [],
                    },
                    ensure_ascii=False,
                ),
            )
        )
        renderer.event(AgentEvent("Bridge", "phase", "并行规划"))
        renderer.event(
            AgentEvent(
                "Claude",
                "lifecycle",
                "waiting",
                status="waiting_model",
                safe_summary="Claude · 等待模型响应",
            )
        )
        renderer.event(
            AgentEvent(
                "Codex",
                "lifecycle",
                "waiting",
                status="waiting_model",
                safe_summary="Codex · 等待模型响应",
            )
        )

        rendered = output.getvalue()
        last_frame = rendered.rsplit("\033[H", 1)[-1]
        self.assertLess(last_frame.index("实时 Agent"), last_frame.index("任务进度"))
        self.assertIn("已完成 0/2", last_frame)
        self.assertIn("[░░░", last_frame)
        self.assertEqual(rendered.count("\033[2J"), 1)
        renderer.close()

    def test_tui_bounds_large_reply_preview_while_other_agent_runs(self) -> None:
        class TtyStringIO(io.StringIO):
            def isatty(self) -> bool:
                return True

        output = TtyStringIO()
        renderer = ConsoleRenderer(color=False, stream=output, width=80, tui=True)
        renderer.begin_run("run-preview")
        renderer.event(AgentEvent("Bridge", "phase", "并行规划"))
        renderer.event(
            AgentEvent(
                "Codex",
                "lifecycle",
                "waiting",
                status="waiting_model",
                safe_summary="Codex · 等待模型响应",
            )
        )
        renderer.event(AgentEvent("Claude", "text", "x" * 20_000))

        self.assertLess(len(renderer._tui_last_reply), 300)
        renderer.close()

    def test_collaboration_confirmation_prints_both_plans_after_leaving_tui(self) -> None:
        class TtyStringIO(io.StringIO):
            def isatty(self) -> bool:
                return True

        output = TtyStringIO()
        renderer = ConsoleRenderer(color=False, stream=output, width=80, tui=True)
        renderer.begin_run("run-plan")

        renderer.collaboration_confirmation(
            AgentRunResult(
                "Claude",
                "Agent A 方案\n1. 修改 service.py",
            ),
            AgentRunResult("Codex", "Agent B 方案\n1. 增加失败路径测试"),
            (
                AgentRunResult("Claude", "A 对 B 的修改意见"),
                AgentRunResult("Codex", "B 对 A 的修改意见"),
            ),
            AgentRunResult("Claude", "双方统一方案：修改并增加测试"),
            None,
            0,
        )

        rendered = output.getvalue()
        self.assertIn("Agent A · Claude · 独立方案", rendered)
        self.assertIn("Agent B · Codex · 独立方案", rendered)
        self.assertIn("修改 service.py", rendered)
        self.assertIn("增加失败路径测试", rendered)
        self.assertIn("双方统一方案", rendered)
        self.assertIn("[e] 执行统一方案", rendered)
        self.assertIn("[t] 单独给某个 Agent 提要求", rendered)
        self.assertIn("[d] 导出最终技术文档", rendered)

    def test_tui_outcome_prints_plan_when_human_gate_is_disabled(self) -> None:
        class TtyStringIO(io.StringIO):
            def isatty(self) -> bool:
                return True

        output = TtyStringIO()
        renderer = ConsoleRenderer(color=False, stream=output, width=80, tui=True)
        renderer.begin_run("run-no-gate")

        renderer.outcome(
            BridgeOutcome(
                task="完成任务",
                executor="claude",
                execution_result=AgentRunResult("Claude", "实施完成"),
                unified_proposal=AgentRunResult(
                    "Claude", "双方统一的完整实施方案"
                ),
                agent_proposals=(
                    AgentRunResult("Claude", "Agent A 独立方案"),
                    AgentRunResult("Codex", "Agent B 独立方案：先补齐失败路径"),
                ),
                cross_reviews=(
                    AgentRunResult("Claude", "A 对 B 的修改意见"),
                    AgentRunResult("Codex", "B 对 A 的修改意见"),
                ),
            )
        )

        rendered = output.getvalue()
        self.assertIn("Agent A · Claude · 独立方案", rendered)
        self.assertIn("Agent B · Codex · 独立方案", rendered)
        self.assertIn("先补齐失败路径", rendered)
        self.assertIn("Claude · 交叉审核", rendered)
        self.assertIn("Codex · 交叉审核", rendered)
        self.assertIn("双方统一方案", rendered)
        self.assertIn("双方统一的完整实施方案", rendered)
        self.assertIn("Claude · 执行结果", rendered)
        self.assertIn("双方独立方案      2 份", rendered)

    def test_safe_progress_classifier_uses_generic_file_and_check_labels(self) -> None:
        self.assertEqual(
            _safe_progress_text(AgentEvent("Codex", "tool", "pytest")),
            "Codex · 正在执行检查",
        )
        self.assertEqual(
            _safe_progress_text(AgentEvent("Claude", "tool", "Grep: {}")),
            "Claude · 正在检查文件",
        )

    def test_structured_review_is_not_printed_as_raw_json(self) -> None:
        output = io.StringIO()
        renderer = ConsoleRenderer(color=False, stream=output, width=80)
        raw = (
            '{"verdict":"request_changes","requirements_covered":["正常登录"],'
            '"findings":[{"severity":"P1","file":"auth.py","line":42,'
            '"requirement":"释放锁","problem":"异常路径泄漏",'
            '"evidence":"提前返回","suggestion":"使用 finally"}]}'
        )

        renderer.event(AgentEvent("Codex", "text", raw))

        rendered = output.getvalue()
        self.assertIn("Codex · 结构化验收", rendered)
        self.assertIn("REQUEST CHANGES", rendered)
        self.assertIn("[P1] auth.py:42", rendered)
        self.assertIn("建议：使用 finally", rendered)
        self.assertNotIn('{"verdict"', rendered)

    def test_structured_consensus_is_rendered_as_five_readable_checks(self) -> None:
        output = io.StringIO()
        renderer = ConsoleRenderer(color=False, stream=output, width=80)
        raw = json.dumps(
            {
                "protocol": "multiagent.consensus.v1",
                "verdict": "revise",
                "criteria": {
                    "requirements": True,
                    "architecture": True,
                    "failure_paths": False,
                    "compatibility": True,
                    "testing": False,
                },
                "agreements": ["保持接口"],
                "remaining_disagreements": ["异常恢复策略"],
                "required_revisions": ["增加失败路径测试"],
            },
            ensure_ascii=False,
        )

        renderer.event(AgentEvent("Codex", "text", raw))

        rendered = output.getvalue()
        self.assertIn("Codex · 方案共识", rendered)
        self.assertIn("✓ 需求边界", rendered)
        self.assertIn("! 异常路径", rendered)
        self.assertIn("异常恢复策略", rendered)
        self.assertNotIn('"protocol"', rendered)

    def test_native_logs_are_hidden_unless_verbose(self) -> None:
        quiet_output = io.StringIO()
        verbose_output = io.StringIO()
        event = AgentEvent("Codex", "log", "WARNING: noisy native message")

        ConsoleRenderer(color=False, stream=quiet_output).event(event)
        ConsoleRenderer(color=False, verbose=True, stream=verbose_output).event(event)

        self.assertEqual(quiet_output.getvalue(), "")
        self.assertIn("noisy native message", verbose_output.getvalue())

    def test_collects_agent_call_and_token_metrics(self) -> None:
        renderer = ConsoleRenderer(color=False, stream=io.StringIO())
        renderer.begin_run("run-1")
        renderer.event(
            AgentEvent(
                "Claude",
                "metric",
                '{"duration_seconds":1.2,"input_tokens":100,"output_tokens":25}',
            )
        )

        summary = renderer.summary()

        self.assertEqual(summary["agent_calls"], {"Claude": 1})
        self.assertEqual(summary["agent_durations"], {"Claude": 1.2})
        self.assertEqual(summary["input_tokens"], 100)
        self.assertEqual(summary["output_tokens"], 25)

    def test_task_detail_renders_recent_safe_event_timeline(self) -> None:
        output = io.StringIO()
        renderer = ConsoleRenderer(color=False, stream=output, width=100)

        renderer.task_detail(
            {
                "id": "run-1",
                "status": "running",
                "phase": "proposal_complete",
                "executor": "claude",
                "workspace": "/tmp/project",
                "task": "完成任务",
                "technical_document": "/tmp/project/multiagent-docs/run-1.md",
                "events": [
                    {
                        "timestamp": "2026-07-31T03:04:05.000+00:00",
                        "source": "Claude",
                        "status": "completed",
                        "step_id": "proposal",
                        "safe_summary": "Claude · 已完成本轮响应",
                        "elapsed_seconds": 65.2,
                    }
                ],
            }
        )

        rendered = output.getvalue()
        self.assertIn("最近事件", rendered)
        self.assertIn("Claude [completed]", rendered)
        self.assertIn("proposal", rendered)
        self.assertIn("1分05秒", rendered)
        self.assertIn("multiagent-docs/run-1.md", rendered)

    def test_markdown_table_is_aligned_and_raw_separator_is_hidden(self) -> None:
        output = io.StringIO()
        renderer = ConsoleRenderer(color=False, stream=output, width=76)
        table = """| 检查项 | 主方案 | 结论 |
|:---|:---|---:|
| API 兼容性 | 保持原接口 | 通过 |
| 异常路径 | 登录超时后的锁没有释放，需要增加 finally 处理 | 修订 |"""

        renderer.event(AgentEvent("Codex", "text", table))

        rendered = output.getvalue()
        self.assertIn("┌", rendered)
        self.assertIn("┬", rendered)
        self.assertIn("API 兼容性", rendered)
        self.assertIn("登录超时后的锁没有释放", rendered)
        self.assertNotIn("|:---", rendered)

    def test_wide_table_falls_back_to_records(self) -> None:
        output = io.StringIO()
        renderer = ConsoleRenderer(color=False, stream=output, width=60)
        table = """| A | B | C | D | E | F | G |
|---|---|---|---|---|---|---|
| 1 | 2 | 3 | 4 | 5 | 6 | 7 |"""

        renderer.event(AgentEvent("Claude", "text", table))

        rendered = output.getvalue()
        self.assertIn("已转换为逐条记录", rendered)
        self.assertIn("A: 1", rendered)
        self.assertNotIn("|---", rendered)


if __name__ == "__main__":
    unittest.main()
