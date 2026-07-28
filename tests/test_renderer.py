from __future__ import annotations

import io
import json
import unittest

from multiagent_cli.bridge_models import AgentEvent
from multiagent_cli.renderer import ConsoleRenderer


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
            lead="claude",
            reviewer="codex",
            review_rounds=2,
            consensus=False,
        )

        rendered = output.getvalue()
        self.assertIn(">_  MutiAgent", rendered)
        self.assertIn("---  *  ---", rendered)
        self.assertIn("│   >_   │", rendered)
        self.assertIn("CLAUDE CODE", rendered)
        self.assertIn("CODEX CLI", rendered)
        self.assertIn("⇄", rendered)
        self.assertIn("/tmp/example", rendered)
        self.assertIn("主 Agent    claude", rendered)
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
                "protocol": "mutiagent.consensus.v1",
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
        self.assertEqual(summary["input_tokens"], 100)
        self.assertEqual(summary["output_tokens"], 25)

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
