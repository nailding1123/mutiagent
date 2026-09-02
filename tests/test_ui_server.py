from __future__ import annotations

import base64
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from multiagent_cli.bridge_models import (
    AgentEvent,
    AgentRunResult,
    LEGACY_GROUP_CHAT_AGENT_A_IDENTITY,
    LEGACY_GROUP_CHAT_AGENT_B_IDENTITY,
    NativeInteractionOption,
    NativeInteractionRequest,
)
from multiagent_cli.bridge_config import resolve_bridge_settings
from multiagent_cli.run_store import RunStore
from multiagent_cli import ui_server
from multiagent_cli.ui_server import (
    UIError,
    LocalUIHTTPServer,
    UISession,
    UISessionManager,
    make_request_handler,
)


class FakeChatAdapter:
    def __init__(self, name: str, results: list[AgentRunResult]) -> None:
        self.display_name = name
        self.results = iter(results)
        self.calls: list[dict[str, Any]] = []
        self.stop_requested = False

    def run(self, prompt: str, **kwargs: Any) -> AgentRunResult:
        self.calls.append({"prompt": prompt, **kwargs})
        return next(self.results)

    def request_stop(self) -> None:
        self.stop_requested = True


class UIServerTests(unittest.TestCase):
    def test_message_rollback_blocks_workspace_drift_and_can_retry(self) -> None:
        class WritingAdapter(FakeChatAdapter):
            def run(self, prompt: str, **kwargs: Any) -> AgentRunResult:
                self.calls.append({"prompt": prompt, **kwargs})
                (Path(kwargs["workspace"]) / "agent.txt").write_text(
                    "agent change",
                    encoding="utf-8",
                )
                return next(self.results)

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "repo"
            workspace.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
            subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=workspace, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=workspace, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=workspace, check=True)
            (workspace / "tracked.txt").write_text("base", encoding="utf-8")
            (workspace / ".multiagent.json").write_text(
                json.dumps({"claude": {"command": "/bin/echo"}, "codex": {"command": "/bin/echo"}}),
                encoding="utf-8",
            )
            subprocess.run(["git", "add", "."], cwd=workspace, check=True)
            subprocess.run(["git", "commit", "-qm", "initial"], cwd=workspace, check=True)
            manager = UISessionManager(
                store=RunStore(Path(directory) / "state"),
                default_workspace=workspace,
            )
            adapter = WritingAdapter(
                "Claude",
                [AgentRunResult("Claude", "修改完成")],
            )
            with patch(
                "multiagent_cli.runtime.make_adapters",
                return_value={"claude": adapter, "codex": FakeChatAdapter("Codex", [])},
            ):
                started = manager.start_task(
                    {"task": "@Claude 执行：修改文件", "workspace": str(workspace)}
                )
                self._wait_for_status(
                    manager,
                    started["id"],
                    "ready",
                    message_count=2,
                )
                ready = manager.store.get(started["id"]) or {}
                reply = ready["group_chat"]["messages"][-1]
                self.assertEqual(reply["changes"]["rollback"]["status"], "available")

                user_file = workspace / "user.txt"
                user_file.write_text("keep me", encoding="utf-8")
                conflict = manager.rollback_chat_message(started["id"], reply["id"])
                self.assertEqual(conflict["rollback"]["status"], "conflict")
                self.assertTrue((workspace / "agent.txt").exists())
                self.assertTrue(user_file.exists())

                user_file.unlink()
                rolled_back = manager.rollback_chat_message(started["id"], reply["id"])
                agent_exists_after_rollback = (workspace / "agent.txt").exists()

            persisted = manager.store.get(started["id"]) or {}

        self.assertEqual(rolled_back["rollback"]["status"], "rolled_back")
        self.assertFalse(agent_exists_after_rollback)
        persisted_reply = persisted["group_chat"]["messages"][-1]
        self.assertEqual(persisted_reply["changes"]["rollback"]["status"], "rolled_back")

    def test_comparison_manager_apply_persists_selected_candidate(self) -> None:
        class WritingAdapter(FakeChatAdapter):
            def run(self, prompt: str, **kwargs: Any) -> AgentRunResult:
                self.calls.append({"prompt": prompt, **kwargs})
                (Path(kwargs["workspace"]) / f"{self.display_name.lower().replace(' ', '_')}.txt").write_text(
                    self.display_name,
                    encoding="utf-8",
                )
                return next(self.results)

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "repo"
            workspace.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
            subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=workspace, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=workspace, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=workspace, check=True)
            (workspace / "tracked.txt").write_text("base", encoding="utf-8")
            (workspace / ".multiagent.json").write_text(
                json.dumps({"claude": {"command": "/bin/echo"}, "codex": {"command": "/bin/echo"}}),
                encoding="utf-8",
            )
            subprocess.run(["git", "add", "."], cwd=workspace, check=True)
            subprocess.run(["git", "commit", "-qm", "initial"], cwd=workspace, check=True)
            manager = UISessionManager(
                store=RunStore(Path(directory) / "state"),
                default_workspace=workspace,
            )
            claude = WritingAdapter("Claude", [AgentRunResult("Claude", "A")])
            codex = WritingAdapter("Codex", [AgentRunResult("Codex", "B")])
            with patch(
                "multiagent_cli.runtime.make_adapters",
                return_value={"claude": claude, "codex": codex},
            ):
                started = manager.start_task({"task": "@all 执行：分别实现", "workspace": str(workspace)})
                self._wait_for_status(manager, started["id"], "ready", message_count=3)
                before = manager.store.get(started["id"]) or {}
                comparison_id = before["group_chat"]["comparison"]["id"]

                with self.assertRaisesRegex(UIError, "ID 不匹配"):
                    manager.apply_comparison(
                        started["id"],
                        "codex",
                        "comparison-wrong",
                    )
                self.assertFalse((workspace / "codex.txt").exists())

                result = manager.apply_comparison(
                    started["id"],
                    "codex",
                    comparison_id,
                )

            persisted = manager.store.get(started["id"]) or {}
            selected_exists = (workspace / "codex.txt").is_file()
            other_exists = (workspace / "claude.txt").exists()

        self.assertEqual(result["comparison"]["id"], comparison_id)
        self.assertEqual(result["comparison"]["status"], "applied")
        self.assertEqual(persisted["group_chat"]["comparison"]["selected_agent"], "codex")
        self.assertFalse(other_exists)
        self.assertTrue(selected_exists)

    def test_comparison_manager_previews_finished_candidate_while_peer_runs(self) -> None:
        class WritingAdapter(FakeChatAdapter):
            def run(self, prompt: str, **kwargs: Any) -> AgentRunResult:
                self.calls.append({"prompt": prompt, **kwargs})
                (Path(kwargs["workspace"]) / f"{self.display_name.lower()}.txt").write_text(
                    self.display_name,
                    encoding="utf-8",
                )
                return next(self.results)

        class BlockingWritingAdapter(FakeChatAdapter):
            def __init__(self, name: str) -> None:
                super().__init__(name, [])
                self.started = threading.Event()
                self.release = threading.Event()

            def run(self, prompt: str, **kwargs: Any) -> AgentRunResult:
                self.calls.append({"prompt": prompt, **kwargs})
                (Path(kwargs["workspace"]) / "codex.txt").write_text(
                    self.display_name,
                    encoding="utf-8",
                )
                self.started.set()
                self.release.wait(3)
                return AgentRunResult(self.display_name, "B")

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "repo"
            workspace.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
            subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=workspace, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=workspace, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=workspace, check=True)
            (workspace / "tracked.txt").write_text("base", encoding="utf-8")
            # Configuration resolution happens before the adapter factory is
            # patched below. Use harmless local commands so this test remains
            # independent of whether native CLIs are installed in CI.
            (workspace / ".multiagent.json").write_text(
                json.dumps({
                    "claude": {"command": "/bin/echo"},
                    "codex": {"command": "/bin/echo"},
                }),
                encoding="utf-8",
            )
            subprocess.run(["git", "add", "."], cwd=workspace, check=True)
            subprocess.run(["git", "commit", "-qm", "initial"], cwd=workspace, check=True)
            manager = UISessionManager(
                store=RunStore(Path(directory) / "state"),
                default_workspace=workspace,
            )
            codex = BlockingWritingAdapter("Codex")
            with patch(
                "multiagent_cli.runtime.make_adapters",
                return_value={
                    "claude": WritingAdapter("Claude", [AgentRunResult("Claude", "A")]),
                    "codex": codex,
                },
            ):
                started = manager.start_task(
                    {"task": "@all 执行：分别实现", "workspace": str(workspace)}
                )
                self.assertTrue(codex.started.wait(2))
                deadline = time.monotonic() + 3
                comparison = None
                while time.monotonic() < deadline:
                    record = manager.store.get(started["id"]) or {}
                    comparison = (record.get("group_chat") or {}).get("comparison")
                    if (
                        isinstance(comparison, dict)
                        and comparison.get("status") == "running"
                        and comparison.get("candidates", {}).get("claude", {}).get("status") == "ready"
                    ):
                        break
                    time.sleep(0.01)
                self.assertIsInstance(comparison, dict)
                comparison_id = comparison["id"]

                previewed = manager.preview_comparison(
                    started["id"],
                    "claude",
                    comparison_id,
                )
                self.assertEqual(previewed["comparison"]["status"], "previewing")
                self.assertTrue((workspace / "claude.txt").is_file())
                self.assertFalse((workspace / "codex.txt").exists())

                codex.release.set()
                self._wait_for_status(manager, started["id"], "ready", message_count=3)
                manager.discard_comparison(started["id"], comparison_id)

        self.assertFalse((workspace / "claude.txt").exists())

    def test_non_git_comparison_is_rejected_before_creating_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "plain"
            workspace.mkdir()
            manager = UISessionManager(
                store=RunStore(Path(directory) / "state"),
                default_workspace=workspace,
            )
            with patch(
                "multiagent_cli.runtime.make_adapters",
                return_value={
                    "claude": FakeChatAdapter("Claude", []),
                    "codex": FakeChatAdapter("Codex", []),
                },
            ), patch(
                "multiagent_cli.bridge_config._discover_executable",
                side_effect=AssertionError("CLI discovery should not run before Git validation"),
            ):
                with self.assertRaisesRegex(UIError, "需要 Git 工作区"):
                    manager.start_task(
                        {
                            "task": "@all 执行：分别修复",
                            "workspace": str(workspace),
                        }
                    )
            self.assertEqual(manager.store.list(), [])

    def test_agent_sidebar_only_animates_agents_active_in_current_turn(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("node is required for the frontend status test")
        script_path = Path(ui_server.__file__).with_name("web") / "app.js"
        harness = r"""
const fs = require('fs');
const source = fs.readFileSync(process.argv[1], 'utf8');
function extract(name, nextName) {
  const start = source.indexOf(`function ${name}`);
  const end = source.indexOf(`\nfunction ${nextName}`, start);
  if (start < 0 || end < 0) throw new Error(`missing ${name}`);
  return source.slice(start, end);
}
function escapeHtml(value) { return String(value || ''); }
function agentKeyFromName(value) { return String(value || '').toLowerCase(); }
eval(extract('statusKey', 'statusLabel'));
eval(extract('fallbackAgentDetail', 'activeAgentFallbackDetail'));
eval(extract('activeAgentFallbackDetail', 'inactiveAgentFallbackDetail'));
eval(extract('inactiveAgentFallbackDetail', 'renderNativeInteraction'));
eval(extract('currentAgentTurnState', 'fallbackAgentDetail'));
eval(extract('renderAgentStatus', 'fallbackAgentDetail'));

const session = {
  active_agents: ['claude'],
  agent_events: {
    claude: {status: 'working', safe_summary: 'Claude 正在处理'},
    codex: {status: 'working', safe_summary: 'Codex 上一轮已处理'},
  },
  native_interactions: [],
};
const claudeContainer = {};
const codexContainer = {};
const claudeDot = {};
const codexDot = {};
renderAgentStatus(claudeContainer, claudeDot, 'claude', 'Claude Code', session, 'running');
renderAgentStatus(codexContainer, codexDot, 'codex', 'Codex', session, 'running');
if (claudeDot.className !== 'status-dot status-running') throw new Error('active Agent did not animate');
if (codexDot.className === 'status-dot status-running') throw new Error('inactive Agent still animates');
if (codexDot.className !== 'status-dot status-waiting') throw new Error('inactive Agent is not static waiting');

const startingSession = {active_agents: ['codex'], agent_events: {}, native_interactions: []};
const startingContainer = {};
const startingDot = {};
renderAgentStatus(startingContainer, startingDot, 'codex', 'Codex', startingSession, 'running');
if (startingDot.className !== 'status-dot status-running') throw new Error('active Agent without first event did not show starting state');
if (!startingContainer.innerHTML.includes('已加入本轮，等待首个状态更新')) throw new Error('active Agent fallback text is ambiguous');
if (startingContainer.innerHTML.includes('安全进度事件')) throw new Error('legacy ambiguous security-event wording remained');

const staleSession = {
  active_agents: [],
  agent_events: {codex: {status: 'completed', safe_summary: 'Codex · 已生成上一轮最终输出'}},
  native_interactions: [],
  group_chat: {messages: [
    {id: 'm1', role: 'user', sender: 'user', recipients: ['claude'], recalled: false},
  ]},
};
const staleContainer = {};
const staleDot = {};
renderAgentStatus(staleContainer, staleDot, 'codex', 'Codex', staleSession, 'running');
if (!staleContainer.innerHTML.includes('未参与本轮回复')) throw new Error('stale directed-turn status was shown as current');
if (staleContainer.innerHTML.includes('上一轮最终输出')) throw new Error('previous-turn event leaked into current status');

const repliedSession = {
  active_agents: [],
  agent_events: {codex: {status: 'completed', safe_summary: 'Codex · 已生成本轮最终输出'}},
  native_interactions: [],
  group_chat: {messages: [
    {id: 'm2', role: 'user', sender: 'user', recipients: ['claude', 'codex'], recalled: false},
    {id: 'm3', role: 'assistant', sender: 'codex', reply_to: 'm2', recalled: false},
  ]},
};
const repliedContainer = {};
const repliedDot = {};
renderAgentStatus(repliedContainer, repliedDot, 'codex', 'Codex', repliedSession, 'running');
if (!repliedContainer.innerHTML.includes('已生成本轮最终输出')) throw new Error('current completed reply was hidden');
"""
        completed = subprocess.run(
            [node, "-e", harness, str(script_path)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_comparison_review_falls_back_to_persisted_record_when_live_session_lags(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("node is required for the frontend comparison test")
        script_path = Path(ui_server.__file__).with_name("web") / "app.js"
        harness = r"""
const fs = require('fs');
const source = fs.readFileSync(process.argv[1], 'utf8');
const start = source.indexOf('function groupChatState');
const end = source.indexOf('\nasync function applyComparison', start);
if (start < 0 || end < 0) throw new Error('groupChatState was not found');
global.state = {
  detail: {
    session: {group_chat: {
      messages: [],
      comparison: {id: 'comparison-test', status: 'running', candidates: {
        claude: {status: 'running'}, codex: {status: 'running'},
      }},
    }},
    record: {group_chat: {
      comparison: {
        id: 'comparison-test',
        status: 'review',
        candidates: {claude: {status: 'ready'}, codex: {status: 'no_changes'}},
      },
    }},
  },
};
eval(source.slice(start, end));
const chat = groupChatState();
if (chat.comparison?.id !== 'comparison-test') {
  throw new Error('persisted review comparison was hidden by stale live session state');
}
if (chat.comparison.status !== 'review') throw new Error('review status was not preserved');
"""
        completed = subprocess.run(
            [node, "-e", harness, str(script_path)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_comparison_candidate_replies_remain_normal_bubbles_and_controls_follow_them(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("node is required for the frontend comparison feed test")
        script_path = Path(ui_server.__file__).with_name("web") / "app.js"
        harness = r"""
const fs = require('fs');
const source = fs.readFileSync(process.argv[1], 'utf8');
const start = source.indexOf('function renderGroupChat');
const end = source.indexOf('\nfunction comparisonMarkup', start);
if (start < 0 || end < 0) throw new Error('renderGroupChat was not found');

const state = {currentId: 'run-1', messageTexts: new Map()};
const messages = [
  {id: 'm1', sender: 'user', role: 'user', content: '@all 执行：分别实现', recipients: ['claude', 'codex'], action: 'execute'},
  {id: 'm2', sender: 'claude', role: 'assistant', content: 'Claude result', reply_to: 'm1', action: 'execute'},
  {id: 'm3', sender: 'codex', role: 'assistant', content: 'Codex result', reply_to: 'm1', action: 'execute'},
  {id: 'm4', sender: 'user', role: 'user', content: '后续消息', recipients: ['claude'], action: 'discuss'},
];
function groupChatState() { return {messages, comparison: {
  id: 'comparison-1', status: 'review', candidates: {
    claude: {status: 'ready', response_message_id: 'm2'},
    codex: {status: 'ready', response_message_id: 'm3'},
  },
}}; }
function reconcilePendingChatMessages() { return []; }
function dedupeGroupChatMessages(value) { return value; }
function groupChatReplyKey(message) { return message.role === 'assistant' ? `${message.sender}:${message.reply_to}` : ''; }
function reconcileStreamBuffers() {}
function streamTextByAgent() { return new Map(); }
function orderGroupChatMessages(value) { return value; }
function agentName(agent) { return agent; }
function messageCard(name, role, result) {
  return `<article data-feed-key="${result.feed_key}">${name}:${result.final_text}</article>`;
}
function comparisonMarkup(comparison) {
  return `<section data-feed-key="comparison-${comparison.id}">controls</section>`;
}
let rendered = [];
function patchFeed(_runId, entries) { rendered = entries; }
function appendStreamToFeed() {}

eval(source.slice(start, end));
renderGroupChat({id: 'run-1', status: 'ready'}, {status: 'ready', agent_events: {}});
const keys = rendered.map((entry) => entry.key);
for (const expected of ['msg-m1', 'msg-m2', 'msg-m3', 'msg-m4', 'comparison-comparison-1']) {
  if (!keys.includes(expected)) throw new Error(`${expected} was omitted from the feed`);
}
if (keys.indexOf('comparison-comparison-1') !== keys.indexOf('msg-m3') + 1) {
  throw new Error('comparison controls must follow the candidate reply bubbles');
}
if (keys.indexOf('msg-m4') < keys.indexOf('comparison-comparison-1')) {
  throw new Error('later messages must render below the comparison controls');
}
"""
        completed = subprocess.run(
            [node, "-e", harness, str(script_path)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_new_chat_dialog_creates_an_empty_chat_without_prompt_or_documents(self) -> None:
        web_root = Path(ui_server.__file__).with_name("web")
        html = (web_root / "index.html").read_text(encoding="utf-8")
        script = (web_root / "app.js").read_text(encoding="utf-8")
        submit_start = script.index("async function submitTask")
        submit_end = script.index("\nasync function submitQuickTask", submit_start)
        submit_source = script[submit_start:submit_end]
        open_start = script.index("function openNewTask")
        open_end = script.index("\nfunction addTaskFiles", open_start)
        open_source = script[open_start:open_end]

        self.assertIn("直接建立空群聊", html)
        self.assertNotIn("添加参考文档或图片", html)
        self.assertIn("task: ''", submit_source)
        self.assertIn("attachments: []", submit_source)
        self.assertNotIn("encodeTaskFiles('task')", submit_source)
        self.assertNotIn("restoreNewTaskDraft()", open_source)

    def test_applied_comparison_hides_cleaned_worktree_controls(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("node is required for the frontend comparison-state test")
        script_path = Path(ui_server.__file__).with_name("web") / "app.js"
        harness = r"""
const fs = require('fs');
const source = fs.readFileSync(process.argv[1], 'utf8');
const start = source.indexOf('function comparisonMarkup');
const end = source.indexOf('\nconst feedHtmlCache', start);
if (start < 0 || end < 0) throw new Error('comparisonMarkup was not found');
function escapeHtml(value) { return String(value || ''); }
function agentName(value) { return value === 'claude' ? 'Claude Code' : 'Codex'; }
function changeSummaryMarkup() { return ''; }
const state = {detail: {session: {workspace: '/main'}, record: {workspace: '/main'}}};
eval(source.slice(start, end));
const html = comparisonMarkup({
  id: 'comparison-applied', status: 'applied', selected_agent: 'claude',
  candidates: {
    claude: {status: 'ready', apply_status: 'applied', cleaned: true, workspace: '/tmp/claude', preview_commands: ['cd /tmp/claude']},
    codex: {status: 'ready', apply_status: 'discarded', cleaned: true, workspace: '/tmp/codex', preview_commands: ['cd /tmp/codex']},
  },
}, 'run-1');
if (!html.includes('已采用 Claude Code 方案')) throw new Error('selected Agent was not shown');
if ((html.match(/临时工作区已删除/g) || []).length !== 2) throw new Error('cleaned worktrees were not explained');
if (html.includes('如何查看实现效果')) throw new Error('stale worktree guide remained after apply');
if (html.includes('data-comparison-action="copy-path" data-comparison-agent="claude">复制路径')) throw new Error('stale path copy control remained after apply');
if (!html.includes('路径已清理')) throw new Error('cleaned path state was not visible');
"""
        completed = subprocess.run(
            [node, "-e", harness, str(script_path)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_running_comparison_allows_preview_of_finished_candidate_only(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("node is required for the frontend comparison-state test")
        script_path = Path(ui_server.__file__).with_name("web") / "app.js"
        harness = r"""
const fs = require('fs');
const source = fs.readFileSync(process.argv[1], 'utf8');
const start = source.indexOf('function comparisonMarkup');
const end = source.indexOf('\nconst feedHtmlCache', start);
if (start < 0 || end < 0) throw new Error('comparisonMarkup was not found');
function escapeHtml(value) { return String(value || ''); }
function agentName(value) { return value === 'claude' ? 'Claude Code' : 'Codex'; }
function changeSummaryMarkup() { return ''; }
const state = {detail: {session: {workspace: '/main'}, record: {workspace: '/main'}}};
eval(source.slice(start, end));
const html = comparisonMarkup({
  id: 'comparison-running', status: 'running',
  candidates: {
    claude: {status: 'ready', workspace: '/tmp/claude', preview_commands: ['cd /tmp/claude']},
    codex: {status: 'running', workspace: '/tmp/codex'},
  },
}, 'run-1');
const claudePreview = 'data-comparison-action="preview" data-comparison-agent="claude"';
const codexPreview = 'data-comparison-action="preview" data-comparison-agent="codex"';
if (!html.includes('已完成 · 可预览')) throw new Error('finished candidate did not show preview state');
if (!html.includes(claudePreview)) throw new Error('finished candidate preview button missing');
if (html.includes(`${claudePreview} disabled`)) throw new Error('finished candidate preview button was disabled');
if (!html.includes(`${codexPreview} disabled`)) throw new Error('running candidate preview button was enabled');
if (!html.includes('另一个 Agent 完成后，才可以正式采用方案')) throw new Error('running comparison guidance missing');
"""
        completed = subprocess.run(
            [node, "-e", harness, str(script_path)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_conflicted_comparison_pauses_apply_until_workspace_recheck(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("node is required for the frontend comparison-state test")
        script_path = Path(ui_server.__file__).with_name("web") / "app.js"
        harness = r"""
const fs = require('fs');
const source = fs.readFileSync(process.argv[1], 'utf8');
const start = source.indexOf('function comparisonMarkup');
const end = source.indexOf('\nconst feedHtmlCache', start);
if (start < 0 || end < 0) throw new Error('comparisonMarkup was not found');
function escapeHtml(value) { return String(value || ''); }
function agentName(value) { return value === 'claude' ? 'Claude Code' : 'Codex'; }
function changeSummaryMarkup() { return ''; }
const state = {detail: {session: {workspace: '/main'}, record: {workspace: '/main'}}};
eval(source.slice(start, end));
const html = comparisonMarkup({
  id: 'comparison-conflict', status: 'conflict',
  candidates: {
    claude: {status: 'ready', workspace: '/tmp/claude', preview_commands: ['cd /tmp/claude']},
    codex: {status: 'ready', workspace: '/tmp/codex', preview_commands: ['cd /tmp/codex']},
  },
  recovery_patch: '/tmp/recovery.patch',
}, 'run-1');
if (!html.includes('主工作区发生了变化')) throw new Error('conflict guidance missing');
if (!html.includes('重新检查主工作区')) throw new Error('recheck action missing');
if (!html.includes('重新检查后采用')) throw new Error('conflict apply label missing');
if (!html.includes('data-comparison-action="apply" data-comparison-agent="claude" disabled')) throw new Error('Claude apply was not paused');
if (!html.includes('data-comparison-action="apply" data-comparison-agent="codex" disabled')) throw new Error('Codex apply was not paused');
"""
        completed = subprocess.run(
            [node, "-e", harness, str(script_path)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_conflicted_comparison_offers_agent_assessment_without_enabling_apply(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("node is required for the frontend comparison-state test")
        script_path = Path(ui_server.__file__).with_name("web") / "app.js"
        harness = r"""
const fs = require('fs');
const source = fs.readFileSync(process.argv[1], 'utf8');
const start = source.indexOf('function comparisonMarkup');
const end = source.indexOf('\nconst feedHtmlCache', start);
if (start < 0 || end < 0) throw new Error('comparisonMarkup was not found');
function escapeHtml(value) { return String(value || ''); }
function agentName(value) { return value === 'claude' ? 'Claude Code' : 'Codex'; }
function changeSummaryMarkup() { return ''; }
const state = {detail: {session: {workspace: '/main'}, record: {workspace: '/main'}}};
eval(source.slice(start, end));
const html = comparisonMarkup({
  id: 'comparison-assess', status: 'conflict',
  candidates: {
    claude: {status: 'ready', workspace: '/tmp/claude', preview_commands: ['cd /tmp/claude'], conflict_assessment: {status: 'completed', decision: 'safe', confidence: 'high', summary: '文件不重叠', files: ['tracked.txt'], checks: ['git apply --check']}},
    codex: {status: 'ready', workspace: '/tmp/codex', preview_commands: ['cd /tmp/codex']},
  },
}, 'run-1');
if (!html.includes('让 Claude Code 评估')) throw new Error('Claude assessment button missing');
if (!html.includes('让 Claude Code 解决冲突')) throw new Error('Claude conflict resolution button missing');
if (!html.includes('Agent 判断：可以继续安全检查')) throw new Error('assessment result missing');
if (!html.includes('data-comparison-action="assess" data-comparison-agent="codex"')) throw new Error('Codex assessment button missing');
        if (!html.includes('data-comparison-action="resolve" data-comparison-agent="codex"')) throw new Error('Codex conflict resolution button missing');
if (!html.includes('data-comparison-action="apply" data-comparison-agent="claude" disabled')) throw new Error('assessment bypassed apply safety gate');
"""
        completed = subprocess.run(
            [node, "-e", harness, str(script_path)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_comparison_conflict_operation_returns_progress_state_before_completion(self) -> None:
        class ComparisonAdapter(FakeChatAdapter):
            def run(self, prompt: str, **kwargs: Any) -> AgentRunResult:
                self.calls.append({"prompt": prompt, **kwargs})
                if kwargs.get("mode") == "write":
                    (Path(kwargs["workspace"]) / f"{self.display_name}.txt").write_text(
                        "candidate", encoding="utf-8"
                    )
                return next(self.results)

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "repo"
            workspace.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=workspace, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=workspace, check=True)
            (workspace / "tracked.txt").write_text("base", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt"], cwd=workspace, check=True)
            subprocess.run(["git", "commit", "-qm", "initial"], cwd=workspace, check=True)
            claude = ComparisonAdapter("Claude", [AgentRunResult("Claude", "A"), AgentRunResult("Claude", "{\"decision\":\"safe\"}")])
            codex = ComparisonAdapter("Codex", [AgentRunResult("Codex", "B")])
            from multiagent_cli.group_chat import GroupChatEngine
            engine = GroupChatEngine(
                resolve_bridge_settings({"claude": {"command": "/bin/echo"}, "codex": {"command": "/bin/echo"}}, workspace=workspace),
                {"claude": claude, "codex": codex},
            )
            engine.ask("@all 执行：分别实现")
            (workspace / "tracked.txt").write_text("user change", encoding="utf-8")
            engine.apply_comparison("claude")
            manager = UISessionManager(store=RunStore(Path(directory) / "state"), default_workspace=workspace)
            manager.store.start(task="x", workspace=workspace, run_id="operation-run")
            session = UISession(run_id="operation-run", task="x", workspace=workspace, notify=manager.publish)
            session.bind_chat_engine(engine)
            manager._reserve_session(session)
            comparison_id = engine.comparison()["id"]

            started = manager.start_comparison_operation("operation-run", "claude", comparison_id, "assess")
            self.assertEqual(started["comparison"]["operation"]["status"], "running")
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                current = engine.comparison() or {}
                if "operation" not in current:
                    break
                time.sleep(0.01)
            self.assertNotIn("operation", engine.comparison() or {})

    def test_terminal_comparison_clears_composer_status_hint(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("node is required for the frontend comparison-state test")
        script_path = Path(ui_server.__file__).with_name("web") / "app.js"
        harness = r"""
const fs = require('fs');
const source = fs.readFileSync(process.argv[1], 'utf8');
const start = source.indexOf('function updateComparisonComposeHint');
const end = source.indexOf('\nfunction removePendingChatMessage', start);
if (start < 0 || end < 0) throw new Error('updateComparisonComposeHint was not found');
const input = {value: ''};
const hint = {
  textContent: '',
  dataset: {},
  classList: {toggle(_name, hidden) { hint.hidden = hidden; }},
};
const el = {comparisonComposeHint: hint, quickTaskInput: input};
let comparison = null;
function currentComparison() { return comparison; }
function agentName(value) { return value === 'claude' ? 'Claude Code' : 'Codex'; }
function isComparisonExecutionRequest() { return false; }
eval(source.slice(start, end));

comparison = {status: 'review'};
updateComparisonComposeHint();
if (hint.hidden || !hint.textContent.includes('候选待选择')) {
  throw new Error('review state should keep the actionable composer hint');
}
for (const status of ['applied', 'discarded']) {
  comparison = {status};
  updateComparisonComposeHint();
  if (!hint.hidden || hint.textContent !== '') {
    throw new Error(`${status} state left a stale composer hint`);
  }
}
"""
        completed = subprocess.run(
            [node, "-e", harness, str(script_path)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_composer_paste_uses_files_fallback_and_renders_image_chip(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("node is required for the frontend paste test")
        script_path = Path(ui_server.__file__).with_name("web") / "app.js"
        harness = r"""
const fs = require('fs');
const source = fs.readFileSync(process.argv[1], 'utf8');
function extract(startMarker, endMarker) {
  const start = source.indexOf(startMarker);
  const end = source.indexOf(endMarker, start);
  if (start < 0 || end < 0) throw new Error(`missing ${startMarker}`);
  return source.slice(start, end);
}

class BrowserFile {
  constructor(parts, name, options = {}) {
    this.name = name;
    this.type = options.type || '';
    this.size = parts.reduce((total, part) => total + Number(part.size || 0), 0);
    this.lastModified = 0;
  }
}
global.File = BrowserFile;
const calls = [];
function addTaskFiles(files, target) { calls.push({files: Array.from(files), target}); }
function showToast() {}
const el = {taskInput: {}, quickTaskInput: {}};
eval(extract('function handleComposerPaste', '\nfunction resizeComposer'));

const pasted = {name: '', type: 'image/png', size: 42, lastModified: 7};
let prevented = false;
handleComposerPaste({
  currentTarget: el.quickTaskInput,
  clipboardData: {items: [], files: [pasted]},
  preventDefault() { prevented = true; },
});
if (!prevented || calls.length !== 1 || calls[0].target !== 'composer') {
  throw new Error('clipboardData.files fallback was not accepted');
}
if (!calls[0].files[0].name.endsWith('.png')) {
  throw new Error('unnamed screenshot did not receive a png filename');
}

const pasted2 = {name: '', type: 'image/png', size: 43, lastModified: 8};
handleComposerPaste({
  currentTarget: el.quickTaskInput,
  clipboardData: {items: [{kind: 'file', getAsFile: () => pasted2}], files: [pasted2]},
  preventDefault() {},
});
if (calls.length !== 2 || calls[1].files.length !== 1) throw new Error('clipboard file was added twice');

// A second paste event carrying the same image immediately afterwards must
// not create another attachment chip.
handleComposerPaste({
  currentTarget: el.quickTaskInput,
  clipboardData: {items: [], files: [pasted2]},
  preventDefault() {},
});
if (calls.length !== 2) throw new Error('duplicate paste event was not ignored');

class Node {
  constructor() {
    this.children = [];
    this.dataset = {};
    this.classList = {toggle() {}, add() {}, contains() { return false; }};
  }
  append(...children) {
    if (children.some((child) => child === undefined)) throw new Error('undefined child');
    this.children.push(...children);
  }
  replaceChildren() { this.children = []; }
  setAttribute() {}
}
global.document = {createElement: () => new Node()};
const state = {newTaskFiles: [], composerFiles: [calls[0].files[0]]};
el.documentList = new Node();
el.composerAttachmentList = new Node();
function isImageFile() { return true; }
function appendImageThumbnail(parent) { parent.append(new Node()); }
function formatBytes() { return '42 B'; }
eval(extract('function renderTaskFiles', '\nasync function encodeTaskFiles'));
renderTaskFiles();
if (el.composerAttachmentList.children.length !== 1) {
  throw new Error('image attachment chip did not render');
}
"""
        completed = subprocess.run(
            [node, "-e", harness, str(script_path)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_settings_ui_normalizes_legacy_builtin_identities(self) -> None:
        values = ui_server._config_for_ui(
            {
                "group_chat_identities": {
                    "agent_a": LEGACY_GROUP_CHAT_AGENT_A_IDENTITY,
                    "agent_b": LEGACY_GROUP_CHAT_AGENT_B_IDENTITY,
                }
            }
        )

        self.assertEqual(
            values["group_chat_identities"]["agent_a"],
            values["group_chat_identities"]["agent_b"],
        )

    def test_settings_ui_exposes_worktree_toggle_with_true_default(self) -> None:
        values = ui_server._config_for_ui({})
        self.assertTrue(values["worktree"])
        self.assertFalse(ui_server._config_for_ui({"worktree": False})["worktree"])

        script = (Path(ui_server.__file__).with_name("web") / "app.js").read_text(
            encoding="utf-8"
        )
        html = (Path(ui_server.__file__).with_name("web") / "index.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("settings-worktree-enabled", html)
        self.assertIn("values.worktree !== false", script)
        self.assertIn("worktree: get('settings-worktree-enabled').checked", script)

    def test_saving_worktree_setting_updates_live_group_chat_engine(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            config_path = workspace / ".multiagent.json"
            config_path.write_text(
                json.dumps({
                    "worktree": False,
                    "claude": {"command": "/bin/echo"},
                    "codex": {"command": "/bin/echo"},
                }),
                encoding="utf-8",
            )
            manager = UISessionManager(
                store=RunStore(workspace / "state"),
                default_workspace=workspace,
            )
            manager.store.start(
                task="live",
                workspace=workspace,
                run_id="live-worktree-setting",
            )
            from multiagent_cli.group_chat import GroupChatEngine

            engine = GroupChatEngine(
                resolve_bridge_settings(
                    json.loads(config_path.read_text(encoding="utf-8")),
                    workspace=workspace,
                    config_path=config_path,
                ),
                {
                    "claude": FakeChatAdapter("Claude", []),
                    "codex": FakeChatAdapter("Codex", []),
                },
            )
            session = UISession(
                run_id="live-worktree-setting",
                task="live",
                workspace=workspace,
                notify=manager.publish,
            )
            session.status = "running"
            session.bind_chat_engine(engine)
            manager._reserve_session(session)
            loaded = manager.get_settings()
            values = loaded["values"]
            values["worktree"] = True
            manager.save_settings({
                "workspace": str(workspace),
                "revision": loaded["revision"],
                "values": values,
            })

        self.assertTrue(engine.settings.worktree)

    def test_retry_reconciliation_removes_fast_server_reply_loading_bubble(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("node is required for the frontend reconciliation test")
        script_path = Path(ui_server.__file__).with_name("web") / "app.js"
        harness = r"""
const fs = require('fs');
const source = fs.readFileSync(process.argv[1], 'utf8');
function extract(name, nextName) {
  const start = source.indexOf(`function ${name}`);
  const end = source.indexOf(`\nfunction ${nextName}`, start);
  if (start < 0 || end < 0) throw new Error(`missing ${name}`);
  return source.slice(start, end);
}
const state = { pendingChatMessages: new Map() };
const ACTIVE_RUN_STATUSES = new Set(['running', 'awaiting_interaction']);
eval(extract('reconcilePendingChatMessages', 'groupChatMessageKey'));
eval(extract('groupChatReplyKey', 'dedupeGroupChatMessages'));
eval(extract('dedupeGroupChatMessages', 'orderGroupChatMessages'));
state.pendingChatMessages.set('run-1', [{
  client_id: 'client-1',
  delivery_status: 'sending',
  server_user_id: 'user-1',
  server_message_count: 2,
  retry_of: 'old-reply',
  expected_recipients: ['claude'],
  waiting_recipients: ['claude'],
}]);
const messages = [
  { id: 'user-1', sender: 'user', role: 'user', content: 'question' },
  { id: 'old-reply', sender: 'claude', role: 'assistant', reply_to: 'user-1', content: 'old' },
  { id: 'new-reply', sender: 'claude', role: 'assistant', reply_to: 'user-1', retry_of: 'old-reply', content: 'new' },
];
const remaining = reconcilePendingChatMessages('run-1', messages, 'running');
if (remaining.length !== 0) throw new Error('loading bubble was retained');
const deduped = dedupeGroupChatMessages([...messages, {...messages[2]}]);
if (deduped.filter((message) => message.id === 'new-reply').length !== 1) {
  throw new Error('retry replies were not deduplicated');
}
"""
        completed = subprocess.run(
            [node, "-e", harness, str(script_path)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_detail_refresh_reconciles_stale_stream_buffers(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("node is required for the frontend stream test")
        script_path = Path(ui_server.__file__).with_name("web") / "app.js"
        harness = r"""
const fs = require('fs');
const source = fs.readFileSync(process.argv[1], 'utf8');
function extract(name, nextName) {
  const start = source.indexOf(`function ${name}`);
  const end = source.indexOf(`\nfunction ${nextName}`, start);
  if (start < 0 || end < 0) throw new Error(`missing ${name}`);
  return source.slice(start, end);
}
const state = { streamBuffers: new Map([
  ['run-1:claude:turn-1', 'final claude text'],
  ['run-1:codex:turn-1', 'live codex text'],
  ['other:claude:turn-1', 'other run'],
]) };
const ACTIVE_RUN_STATUSES = new Set(['running', 'awaiting_interaction']);
eval(extract('streamBufferAgents', 'clearAgentStreamBuffers'));
eval(extract('clearAgentStreamBuffers', 'agentEventIsTerminal'));
eval(extract('agentEventIsTerminal', 'reconcileStreamBuffers'));
eval(extract('reconcileStreamBuffers', 'streamTextByAgent'));

if (!pendingReplyIsFinalizing({codex: {kind: 'text', status: 'completed'}}, 'codex')) {
  throw new Error('completed final output was not recognized as finalizing');
}
if (pendingReplyIsFinalizing({codex: {kind: 'text', status: 'completed'}}, 'codex', ['claude'])) {
  throw new Error('stale Codex final output was treated as the current turn');
}
if (!pendingReplyIsFinalizing({codex: {kind: 'text', status: 'completed'}}, 'codex', ['codex'])) {
  throw new Error('current Codex final output was not recognized');
}
if (pendingReplyIsFinalizing({codex: {kind: 'progress', status: 'working'}}, 'codex')) {
  throw new Error('working progress was incorrectly recognized as finalizing');
}

reconcileStreamBuffers(
  'run-1',
  [{sender: 'codex'}],
  'running',
  {
    claude: {kind: 'metric', status: 'completed'},
    codex: {kind: 'progress', status: 'working'},
  },
);
if (state.streamBuffers.has('run-1:claude:turn-1')) {
  throw new Error('completed Claude preview was retained');
}
if (!state.streamBuffers.has('run-1:codex:turn-1')) {
  throw new Error('pending Codex preview was removed');
}
if (!state.streamBuffers.has('other:claude:turn-1')) {
  throw new Error('another run was modified');
}

state.streamBuffers.set('run-1:claude:turn-2', 'reconnected stream');
reconcileStreamBuffers(
  'run-1',
  [],
  'running',
  {claude: {kind: 'progress', status: 'working'}},
);
if (!state.streamBuffers.has('run-1:claude:turn-2')) {
  throw new Error('active reconnected stream was removed');
}

reconcileStreamBuffers('run-1', [], 'ready', {});
if ([...state.streamBuffers.keys()].some((key) => key.startsWith('run-1:'))) {
  throw new Error('terminal run retained stale previews');
}
"""
        completed = subprocess.run(
            [node, "-e", harness, str(script_path)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_native_interaction_round_trip_does_not_block_other_session_state(self) -> None:
        published: list[tuple[str, Any]] = []
        with tempfile.TemporaryDirectory() as directory:
            session = UISession(
                run_id="native-run",
                task="task",
                workspace=Path(directory),
                notify=lambda kind, run_id, extra=None: published.append((kind, extra)),
            )
            request = NativeInteractionRequest(
                id="provider-1",
                source="Claude",
                kind="command_approval",
                title="请求执行命令",
                command="pytest -q",
                options=(
                    NativeInteractionOption("approve", "允许一次"),
                    NativeInteractionOption("cancel", "拒绝并停止"),
                ),
            )
            result: list[Any] = []
            worker = threading.Thread(
                target=lambda: result.append(session.wait_for_native_interaction(request)),
                daemon=True,
            )
            worker.start()
            deadline = time.monotonic() + 1
            while not session.to_dict()["native_interactions"] and time.monotonic() < deadline:
                time.sleep(0.01)
            public_id = session.to_dict()["native_interactions"][0]["id"]
            self.assertEqual(session.to_dict()["status"], "awaiting_interaction")
            session.submit_native_interaction(public_id, {"action": "approve"})
            worker.join(1)
            self.assertFalse(worker.is_alive())
            self.assertEqual(result[0].action, "approve")
            self.assertEqual(session.to_dict()["native_interactions"], [])
            self.assertIn(("native_interaction", {"interaction_id": public_id}), published)

    def test_terminal_chat_turn_hides_and_cancels_late_native_interaction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = UISession(
                run_id="native-late",
                task="task",
                workspace=Path(directory),
                notify=lambda *_args: None,
            )
            session.status = "running"
            token = session.begin_chat_turn("turn-1", ("claude",))
            request = NativeInteractionRequest(
                id="provider-late",
                source="Claude",
                kind="command_approval",
                title="请求执行命令",
                command="pytest -q",
                options=(NativeInteractionOption("approve", "允许一次"),),
            )
            result: list[Any] = []
            worker = threading.Thread(
                target=lambda: result.append(session.wait_for_native_interaction(request)),
                daemon=True,
            )
            worker.start()
            deadline = time.monotonic() + 1
            while not session.to_dict()["native_interactions"] and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(session.to_dict()["native_interactions"])
            session.finish_chat_turn(state={}, status="ready", token=token)
            self.assertEqual(session.to_dict()["native_interactions"], [])
            worker.join(1)
            self.assertFalse(worker.is_alive())
            self.assertEqual(result[0].action, "cancel")

    def test_new_chat_turn_clears_previous_live_agent_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = UISession(
                run_id="turn-status-reset",
                task="task",
                workspace=Path(directory),
                notify=lambda *_args: None,
            )
            session.on_event(AgentEvent(
                "Claude", "text", "上一轮最终回复", status="completed",
                step_id="group_chat_turn_1_claude",
            ))
            session.on_event(AgentEvent(
                "Codex", "progress", "上一轮处理中", status="working",
                step_id="group_chat_turn_1_codex",
            ))
            self.assertIn("claude", session.to_dict()["agent_events"])
            self.assertIn("codex", session.to_dict()["agent_events"])

            session.begin_chat_turn("turn-2", ("claude", "codex"))
            live_events = session.to_dict()["agent_events"]
            self.assertNotIn("claude", live_events)
            self.assertNotIn("codex", live_events)

            session.on_event(AgentEvent(
                "Codex", "text", "本轮最终回复", status="completed",
                step_id="group_chat_turn_2_codex",
            ))
        self.assertEqual(
            session.to_dict()["agent_events"]["codex"]["step_id"],
            "group_chat_turn_2_codex",
        )

    def test_live_chat_turn_exposes_message_mapping_for_refresh_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = UISession(
                run_id="refresh-live",
                task="测试刷新恢复",
                workspace=Path(directory),
                notify=lambda *_args, **_kwargs: None,
            )
            token = session.begin_chat_turn("turn-1", ("claude", "codex"))
            session.bind_chat_turn_message(token, "m-user")
            live = session.to_dict()

            self.assertEqual(
                live["active_chat_turns"],
                [{"message_id": "m-user", "agents": ["claude", "codex"]}],
            )

            session.finish_chat_turn(state={}, status="ready", token=token)
            self.assertEqual(session.to_dict()["active_chat_turns"], [])

    def test_refresh_rebuilds_loading_bubbles_from_persisted_active_turn(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("node is required for the frontend refresh recovery test")
        script_path = Path(ui_server.__file__).with_name("web") / "app.js"
        harness = r"""
const fs = require('fs');
const source = fs.readFileSync(process.argv[1], 'utf8');
const start = source.indexOf('function renderGroupChat');
const end = source.indexOf('\nfunction comparisonMarkup', start);
if (start < 0 || end < 0) throw new Error('renderGroupChat was not found');
const state = {currentId: 'run-1', messageTexts: new Map(), pendingChatMessages: new Map(), streamBuffers: new Map(), feedPinnedToBottom: true};
const messages = [{id: 'm-user', sender: 'user', role: 'user', content: '@all 继续处理', recipients: ['claude', 'codex'], action: 'discuss'}];
function groupChatState() { return {messages, comparison: null}; }
function reconcilePendingChatMessages() { return []; }
function dedupeGroupChatMessages(value) { return value; }
function groupChatReplyKey() { return ''; }
function orderGroupChatMessages(serverMessages, pendingUsers, pendingReplies) { return [...serverMessages, ...pendingUsers, ...pendingReplies]; }
function reconcileStreamBuffers() {}
function streamTextByAgent() { return new Map(); }
function comparisonMarkup() { return ''; }
function appendStreamToFeed() {}
function patchFeed(_runId, entries) { globalThis.rendered = entries; }
function renderTimeline() {}
function messageCard(name, role, result) { return `<article>${name}:${result.final_text}</article>`; }
function agentName(agent) { return agent; }
function escapeHtml(value) { return String(value || ''); }
function changeSummaryMarkup() { return ''; }
function attachmentMarkup() { return ''; }
function pendingReplyIsFinalizing() { return false; }
eval(source.slice(start, end));
renderGroupChat({id: 'run-1', status: 'running'}, {
  status: 'running',
  active_agents: ['claude', 'codex'],
  active_chat_turns: [{message_id: 'm-user', agents: ['claude', 'codex']}],
  agent_events: {},
});
const keys = rendered.map((entry) => entry.key);
if (!keys.includes('msg-m-user')) throw new Error('user message disappeared after refresh');
if (!keys.includes('msg-active-run-1-m-user-claude')) throw new Error('Claude loading bubble was not rebuilt');
if (!keys.includes('msg-active-run-1-m-user-codex')) throw new Error('Codex loading bubble was not rebuilt');
"""
        completed = subprocess.run(
            [node, "-e", harness, str(script_path)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_finishing_one_turn_keeps_other_agent_approval_alive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = UISession(
                run_id="native-concurrent",
                task="task",
                workspace=Path(directory),
                notify=lambda *_args: None,
            )
            claude_token = session.begin_chat_turn("claude-turn", ("claude",))
            codex_token = session.begin_chat_turn("codex-turn", ("codex",))
            requests = {
                "Claude": NativeInteractionRequest(
                    id="claude-provider",
                    source="Claude",
                    kind="command_approval",
                    title="Claude 请求执行命令",
                    options=(NativeInteractionOption("approve", "允许一次"),),
                ),
                "Codex": NativeInteractionRequest(
                    id="codex-provider",
                    source="Codex",
                    kind="command_approval",
                    title="Codex 请求执行命令",
                    options=(NativeInteractionOption("approve", "允许一次"),),
                ),
            }
            results: dict[str, list[Any]] = {"Claude": [], "Codex": []}
            workers = [
                threading.Thread(
                    target=lambda source=source: results[source].append(
                        session.wait_for_native_interaction(requests[source])
                    ),
                    daemon=True,
                )
                for source in requests
            ]
            for worker in workers:
                worker.start()
            deadline = time.monotonic() + 1
            while len(session.to_dict()["native_interactions"]) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(len(session.to_dict()["native_interactions"]), 2)
            session.finish_chat_turn(state={}, status="ready", token=claude_token)
            visible = session.to_dict()["native_interactions"]
            self.assertEqual(len(visible), 1)
            self.assertEqual(visible[0]["source"], "Codex")
            public_codex_id = visible[0]["id"]
            session.submit_native_interaction(public_codex_id, {"action": "approve"})
            for worker in workers:
                worker.join(1)
            self.assertTrue(all(not worker.is_alive() for worker in workers))
            self.assertEqual(results["Claude"][0].action, "cancel")
            self.assertEqual(results["Codex"][0].action, "approve")
            session.finish_chat_turn(state={}, status="ready", token=codex_token)

    def test_stopping_session_releases_pending_native_interaction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = UISession(
                run_id="native-stop",
                task="task",
                workspace=Path(directory),
                notify=lambda *_args: None,
            )
            request = NativeInteractionRequest(
                id="provider-1",
                source="Codex",
                kind="permission_approval",
                title="需要权限",
                options=(NativeInteractionOption("cancel", "拒绝并停止"),),
            )
            result: list[Any] = []
            worker = threading.Thread(
                target=lambda: result.append(session.wait_for_native_interaction(request)),
                daemon=True,
            )
            session.status = "running"
            worker.start()
            deadline = time.monotonic() + 1
            while not session.to_dict()["native_interactions"] and time.monotonic() < deadline:
                time.sleep(0.01)
            session.request_stop()
            worker.join(1)
            self.assertFalse(worker.is_alive())
            self.assertEqual(result[0].action, "cancel")

    def test_group_chat_allows_another_agent_while_first_is_replying(self) -> None:
        claude_started = threading.Event()
        release_claude = threading.Event()

        class SlowChatAdapter(FakeChatAdapter):
            def run(self, prompt: str, **kwargs: Any) -> AgentRunResult:
                self.calls.append({"prompt": prompt, **kwargs})
                claude_started.set()
                release_claude.wait(2)
                return next(self.results)

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / ".multiagent.json").write_text(
                json.dumps(
                    {
                        "claude": {"command": "/bin/echo"},
                        "codex": {"command": "/bin/echo"},
                    }
                ),
                encoding="utf-8",
            )
            claude = SlowChatAdapter(
                "Claude",
                [AgentRunResult("Claude", "慢回答", session_id="ca")],
            )
            codex = FakeChatAdapter(
                "Codex",
                [AgentRunResult("Codex", "先回答", session_id="cb")],
            )
            manager = UISessionManager(
                store=RunStore(workspace / "state"),
                default_workspace=workspace,
            )
            with patch(
                "multiagent_cli.runtime.make_adapters",
                return_value={"claude": claude, "codex": codex},
            ):
                started = manager.start_task({})
                run_id = started["id"]
                manager.send_chat_message(run_id, {"message": "@Claude 分析一下"})
                self.assertTrue(claude_started.wait(1))
                manager.send_chat_message(run_id, {"message": "@Codex 回答这个问题"})
                deadline = time.monotonic() + 1
                while not codex.calls and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(len(codex.calls), 1)
                self.assertEqual((manager.store.get(run_id) or {})["status"], "running")
                release_claude.set()
                self._wait_for_status(manager, run_id, "ready", message_count=4)

        self.assertEqual(len(claude.calls), 1)
        self.assertEqual(len(codex.calls), 1)

    def test_group_chat_rejects_a_second_turn_for_the_same_busy_agent(self) -> None:
        claude_started = threading.Event()
        release_claude = threading.Event()

        class SlowChatAdapter(FakeChatAdapter):
            def run(self, prompt: str, **kwargs: Any) -> AgentRunResult:
                self.calls.append({"prompt": prompt, **kwargs})
                claude_started.set()
                release_claude.wait(2)
                return next(self.results)

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / ".multiagent.json").write_text(
                json.dumps(
                    {
                        "claude": {"command": "/bin/echo"},
                        "codex": {"command": "/bin/echo"},
                    }
                ),
                encoding="utf-8",
            )
            claude = SlowChatAdapter(
                "Claude",
                [AgentRunResult("Claude", "慢回答", session_id="ca")],
            )
            manager = UISessionManager(
                store=RunStore(workspace / "state"),
                default_workspace=workspace,
            )
            with patch(
                "multiagent_cli.runtime.make_adapters",
                return_value={
                    "claude": claude,
                    "codex": FakeChatAdapter("Codex", []),
                },
            ):
                run_id = manager.start_task({})["id"]
                manager.send_chat_message(run_id, {"message": "@Claude 分析一下"})
                self.assertTrue(claude_started.wait(1))
                with self.assertRaisesRegex(UIError, "Claude 正在回复"):
                    manager.send_chat_message(run_id, {"message": "@Claude 再回答一个"})
                release_claude.set()
                self._wait_for_status(manager, run_id, "ready", message_count=2)

    def test_serve_ui_reuses_an_existing_compatible_service(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            with (
                patch.object(ui_server, "ui_is_running", return_value=True),
                patch.object(
                    ui_server,
                    "select_ui_workspace",
                    return_value=True,
                ) as select_workspace,
                patch.object(ui_server.webbrowser, "open") as open_browser,
                patch.object(ui_server, "LocalUIHTTPServer") as http_server,
            ):
                result = ui_server.serve_ui(
                    workspace=workspace,
                    store=RunStore(workspace / "state"),
                    port=8765,
                    open_browser=True,
                    quiet=True,
                )

        self.assertEqual(result, 0)
        select_workspace.assert_called_once_with(
            "http://127.0.0.1:8765/",
            workspace,
        )
        open_browser.assert_called_once_with("http://127.0.0.1:8765/")
        http_server.assert_not_called()

    def test_group_chat_routes_mentions_and_persists_shared_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / ".multiagent.json").write_text(
                json.dumps(
                    {
                        "claude": {"command": "/bin/echo"},
                        "codex": {"command": "/bin/echo"},
                    }
                ),
                encoding="utf-8",
            )
            claude = FakeChatAdapter(
                "Claude",
                [AgentRunResult("Claude", "Claude 初始回答", session_id="ca")],
            )
            codex = FakeChatAdapter(
                "Codex",
                [
                    AgentRunResult("Codex", "Codex 初始回答", session_id="cb"),
                    AgentRunResult("Codex", "Codex 审核意见", session_id="cb"),
                ],
            )
            manager = UISessionManager(
                store=RunStore(workspace / "state"),
                default_workspace=workspace,
            )
            with patch(
                "multiagent_cli.runtime.make_adapters",
                return_value={"claude": claude, "codex": codex},
            ):
                started = manager.start_task({"task": "分别给出初始方案"})
                run_id = started["id"]
                self._wait_for_status(manager, run_id, "ready")
                manager.send_chat_message(
                    run_id,
                    {"message": "@Codex 请审核 Claude 的初始回答"},
                )
                self._wait_for_status(manager, run_id, "ready", message_count=5)

            record = manager.store.get(run_id)

        self.assertEqual(record["status"], "ready")
        self.assertEqual(len(record["group_chat"]["messages"]), 5)
        self.assertEqual(len(claude.calls), 1)
        self.assertEqual(len(codex.calls), 2)
        self.assertIn("Claude 初始回答", codex.calls[1]["prompt"])
        self.assertIn("@Codex 请审核", codex.calls[1]["prompt"])

    def test_agent_reply_context_choice_persists_and_controls_future_prompts(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / ".multiagent.json").write_text(
                json.dumps(
                    {
                        "claude": {"command": "/bin/echo"},
                        "codex": {"command": "/bin/echo"},
                    }
                ),
                encoding="utf-8",
            )
            claude = FakeChatAdapter(
                "Claude",
                [AgentRunResult("Claude", "PRIVATE_AGENT_REPLY", session_id="ca")],
            )
            codex = FakeChatAdapter(
                "Codex",
                [AgentRunResult("Codex", "已检查", session_id="cb")],
            )
            manager = UISessionManager(
                store=RunStore(workspace / "state"),
                default_workspace=workspace,
            )
            with patch(
                "multiagent_cli.runtime.make_adapters",
                return_value={"claude": claude, "codex": codex},
            ):
                started = manager.start_task({"task": "@Claude 给出候选回答"})
                run_id = started["id"]
                self._wait_for_status(manager, run_id, "ready", message_count=2)
                record = manager.store.get(run_id) or {}
                reply_id = record["group_chat"]["messages"][-1]["id"]

                session = manager.session(run_id)
                self.assertIsNotNone(session)
                active_token = session.begin_chat_turn("context-active")
                with self.assertRaisesRegex(UIError, "Agent 正在回复"):
                    manager.set_chat_message_context(run_id, reply_id, False)
                session.finish_chat_turn(
                    state=session.chat_engine().to_dict(),
                    token=active_token,
                )

                changed = manager.set_chat_message_context(
                    run_id,
                    reply_id,
                    False,
                )
                manager.send_chat_message(run_id, {"message": "@Codex 继续检查"})
                self._wait_for_status(manager, run_id, "ready", message_count=4)

                persisted = manager.store.get(run_id) or {}
                manager.set_chat_message_context(run_id, reply_id, True)
                restored = manager.store.get(run_id) or {}

        self.assertFalse(changed["message"]["include_in_context"])
        self.assertFalse(
            persisted["group_chat"]["messages"][1]["include_in_context"]
        )
        self.assertNotIn("PRIVATE_AGENT_REPLY", codex.calls[0]["prompt"])
        self.assertNotIn(
            "include_in_context",
            restored["group_chat"]["messages"][1],
        )

    def test_recall_chat_message_persists_placeholder_and_hides_replies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / ".multiagent.json").write_text(
                json.dumps(
                    {
                        "claude": {"command": "/bin/echo"},
                        "codex": {"command": "/bin/echo"},
                    }
                ),
                encoding="utf-8",
            )
            manager = UISessionManager(
                store=RunStore(workspace / "state"),
                default_workspace=workspace,
            )
            with patch(
                "multiagent_cli.runtime.make_adapters",
                return_value={
                    "claude": FakeChatAdapter(
                        "Claude",
                        [AgentRunResult("Claude", "需要撤回的回答", session_id="ca")],
                    ),
                    "codex": FakeChatAdapter("Codex", []),
                },
            ):
                started = manager.start_task({"task": "@Claude 原始问题"})
                run_id = started["id"]
                self._wait_for_status(manager, run_id, "ready", message_count=2)
                before = manager.store.get(run_id) or {}
                user_id = before["group_chat"]["messages"][0]["id"]

                result = manager.recall_chat_message(run_id, user_id)
                persisted = manager.store.get(run_id) or {}
                messages = persisted["group_chat"]["messages"]

        self.assertTrue(result["message"]["recalled"])
        self.assertEqual(messages[0]["content"], "消息已撤回")
        self.assertTrue(messages[0]["recalled"])
        self.assertTrue(messages[1]["hidden"])
        self.assertTrue(messages[1]["recalled"])

    def test_recalled_message_uses_lightweight_notice_instead_of_bubble(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("node is required for the frontend recall test")
        script_path = Path(ui_server.__file__).with_name("web") / "app.js"
        harness = r"""
const fs = require('fs');
const source = fs.readFileSync(process.argv[1], 'utf8');
const start = source.indexOf('function recalledMessageMarkup');
const end = source.indexOf('\nfunction messageCard', start);
if (start < 0 || end < 0) throw new Error('recalledMessageMarkup was not found');
function escapeHtml(value) { return String(value || ''); }
function formatEventTime() { return '10:21:15'; }
eval(source.slice(start, end));
const html = recalledMessageMarkup('msg-m1', '2026-09-02T10:21:15Z');
if (!html.includes('message-recalled-notice')) throw new Error('recall notice class missing');
if (!html.includes('你撤回了一条消息')) throw new Error('recall notice text missing');
if (html.includes('message-row')) throw new Error('recalled message still renders as a full bubble');
"""
        completed = subprocess.run(
            [node, "-e", harness, str(script_path)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_recall_chat_message_stops_an_active_agent_turn(self) -> None:
        class BlockingAdapter(FakeChatAdapter):
            def __init__(self, name: str) -> None:
                super().__init__(name, [])
                self.started = threading.Event()
                self.release = threading.Event()
                self.stop_requested = False

            def request_stop(self) -> None:
                self.stop_requested = True
                self.release.set()

            def run(self, prompt: str, **kwargs: Any) -> AgentRunResult:
                self.calls.append({"prompt": prompt, **kwargs})
                self.started.set()
                self.release.wait(3)
                if self.stop_requested:
                    raise KeyboardInterrupt
                return AgentRunResult(self.display_name, "意外完成")

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / ".multiagent.json").write_text(
                json.dumps({
                    "claude": {"command": "/bin/echo"},
                    "codex": {"command": "/bin/echo"},
                }),
                encoding="utf-8",
            )
            manager = UISessionManager(
                store=RunStore(workspace / "state"),
                default_workspace=workspace,
            )
            claude = BlockingAdapter("Claude")
            with patch(
                "multiagent_cli.runtime.make_adapters",
                return_value={
                    "claude": claude,
                    "codex": FakeChatAdapter("Codex", []),
                },
            ):
                started = manager.start_task({"task": "@Claude 这条消息稍后撤回"})
                self.assertTrue(claude.started.wait(2))
                record = manager.store.get(started["id"]) or {}
                user_id = record["group_chat"]["messages"][0]["id"]
                result = manager.recall_chat_message(started["id"], user_id)
                self.assertTrue(claude.stop_requested)
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    current = manager.store.get(started["id"]) or {}
                    if current.get("status") in {"ready", "interrupted", "failed"}:
                        break
                    time.sleep(0.01)
                self.assertIn(
                    (manager.store.get(started["id"]) or {}).get("status"),
                    {"ready", "interrupted", "failed"},
                )

        self.assertTrue(result["message"]["recalled"])

    def test_recall_chat_message_only_stops_agent_serving_that_message(self) -> None:
        class BlockingAdapter(FakeChatAdapter):
            def __init__(self, name: str) -> None:
                super().__init__(name, [])
                self.started = threading.Event()
                self.release = threading.Event()
                self.stop_requested = False

            def request_stop(self) -> None:
                self.stop_requested = True
                self.release.set()

            def run(self, prompt: str, **kwargs: Any) -> AgentRunResult:
                self.calls.append({"prompt": prompt, **kwargs})
                self.started.set()
                self.release.wait(3)
                if self.stop_requested:
                    raise KeyboardInterrupt
                return AgentRunResult(self.display_name, "正常完成")

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / ".multiagent.json").write_text(
                json.dumps({
                    "claude": {"command": "/bin/echo"},
                    "codex": {"command": "/bin/echo"},
                }),
                encoding="utf-8",
            )
            manager = UISessionManager(
                store=RunStore(workspace / "state"),
                default_workspace=workspace,
            )
            claude = BlockingAdapter("Claude")
            codex = BlockingAdapter("Codex")
            with patch(
                "multiagent_cli.runtime.make_adapters",
                return_value={"claude": claude, "codex": codex},
            ):
                started = manager.start_task({"task": ""})
                manager.send_chat_message(
                    started["id"],
                    {"message": "@Claude 第一条待撤回"},
                )
                self.assertTrue(claude.started.wait(2))
                manager.send_chat_message(
                    started["id"],
                    {"message": "@Codex 第二条保留"},
                )
                self.assertTrue(codex.started.wait(2))
                record = manager.store.get(started["id"]) or {}
                first_user_id = record["group_chat"]["messages"][0]["id"]
                manager.recall_chat_message(started["id"], first_user_id)
                self.assertTrue(claude.stop_requested)
                self.assertFalse(codex.stop_requested)
                codex.release.set()
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    current = manager.store.get(started["id"]) or {}
                    if current.get("status") in {"ready", "failed", "interrupted"}:
                        break
                    time.sleep(0.01)

        self.assertFalse(codex.stop_requested)

    def test_retry_replaces_old_reply_while_continue_keeps_current_reply(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / ".multiagent.json").write_text(
                json.dumps(
                    {
                        "claude": {"command": "/bin/echo"},
                        "codex": {"command": "/bin/echo"},
                    }
                ),
                encoding="utf-8",
            )
            claude = FakeChatAdapter(
                "Claude",
                [
                    AgentRunResult("Claude", "旧回复", session_id="ca-old"),
                    AgentRunResult("Claude", "替代回复", session_id="ca-new"),
                    AgentRunResult("Claude", "继续内容", session_id="ca-new"),
                ],
            )
            manager = UISessionManager(
                store=RunStore(workspace / "state"),
                default_workspace=workspace,
            )
            with patch(
                "multiagent_cli.runtime.make_adapters",
                return_value={
                    "claude": claude,
                    "codex": FakeChatAdapter("Codex", []),
                },
            ):
                started = manager.start_task({"task": "@Claude 原问题"})
                run_id = started["id"]
                self._wait_for_status(manager, run_id, "ready", message_count=2)
                first_record = manager.store.get(run_id) or {}
                old_reply = first_record["group_chat"]["messages"][-1]

                manager.send_chat_message(
                    run_id,
                    {
                        "message": "原问题",
                        "retry_of": old_reply["id"],
                        "retry_mode": "regenerate",
                        "agent": "claude",
                    },
                )
                self._wait_for_status(manager, run_id, "ready", message_count=3)
                replaced_record = manager.store.get(run_id) or {}
                replaced_messages = replaced_record["group_chat"]["messages"]
                replacement = next(
                    message
                    for message in replaced_messages
                    if message.get("role") == "assistant"
                )

                manager.send_chat_message(
                    run_id,
                    {
                        "message": "原问题",
                        "retry_of": replacement["id"],
                        "retry_mode": "continue",
                        "agent": "claude",
                    },
                )
                self._wait_for_status(manager, run_id, "ready", message_count=5)
                continued_record = manager.store.get(run_id) or {}

        replaced_assistants = [
            message
            for message in replaced_messages
            if message.get("role") == "assistant"
        ]
        continued_assistants = [
            message
            for message in continued_record["group_chat"]["messages"]
            if message.get("role") == "assistant"
        ]
        self.assertNotIn(
            old_reply["id"],
            [message["id"] for message in replaced_messages],
        )
        self.assertEqual(
            [message["content"] for message in replaced_assistants],
            ["替代回复"],
        )
        self.assertEqual(
            [message["content"] for message in continued_assistants],
            ["替代回复", "继续内容"],
        )

    def test_group_chat_can_start_empty_and_wait_for_first_message(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / ".multiagent.json").write_text(
                json.dumps(
                    {
                        "claude": {"command": "/bin/echo"},
                        "codex": {"command": "/bin/echo"},
                    }
                ),
                encoding="utf-8",
            )
            claude = FakeChatAdapter(
                "Claude",
                [AgentRunResult("Claude", "收到第一条消息", session_id="ca")],
            )
            codex = FakeChatAdapter("Codex", [])
            manager = UISessionManager(
                store=RunStore(workspace / "state"),
                default_workspace=workspace,
            )
            with patch(
                "multiagent_cli.runtime.make_adapters",
                return_value={"claude": claude, "codex": codex},
            ):
                started = manager.start_task({})
                run_id = started["id"]
                initial_record = manager.store.get(run_id) or {}

                self.assertEqual(started["status"], "ready")
                self.assertEqual(initial_record["status"], "ready")
                self.assertEqual(initial_record["task"], "")
                self.assertEqual(initial_record["display_task"], "群聊协作")
                self.assertEqual(initial_record["group_chat"]["messages"], [])
                self.assertEqual(claude.calls, [])
                self.assertEqual(codex.calls, [])

                manager.send_chat_message(run_id, {"message": "@Claude 你好"})
                self._wait_for_status(manager, run_id, "ready", message_count=2)

            record = manager.store.get(run_id) or {}

        self.assertEqual(len(record["group_chat"]["messages"]), 2)
        self.assertEqual(len(claude.calls), 1)
        self.assertEqual(codex.calls, [])

    def test_rename_run_changes_display_title_but_preserves_original_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = RunStore(workspace / "state")
            store.start(
                task="原始需求内容",
                display_task="原名称",
                workspace=workspace,
                run_id="rename-run",
            )
            manager = UISessionManager(store=store, default_workspace=workspace)
            session = UISession(
                run_id="rename-run",
                task="原名称",
                workspace=workspace,
                notify=manager.publish,
            )
            manager._reserve_session(session)

            renamed = manager.rename_run("rename-run", "  新的\n任务名称  ")
            record = store.get("rename-run") or {}

            self.assertEqual(renamed["display_task"], "新的 任务名称")
            self.assertEqual(record["display_task"], "新的 任务名称")
            self.assertEqual(record["task"], "原始需求内容")
            self.assertEqual(session.to_dict()["task"], "新的 任务名称")
            with self.assertRaisesRegex(UIError, "任务名称不能为空"):
                manager.rename_run("rename-run", "  ")
            with self.assertRaisesRegex(UIError, "不能超过 200"):
                manager.rename_run("rename-run", "长" * 201)

    def test_group_chat_single_agent_execution_writes_target_workspace(self) -> None:
        class WritingChatAdapter(FakeChatAdapter):
            def run(self, prompt: str, **kwargs: Any) -> AgentRunResult:
                self.calls.append({"prompt": prompt, **kwargs})
                workspace = Path(kwargs["workspace"])
                (workspace / f"{self.display_name.lower()}.txt").write_text(
                    self.display_name,
                    encoding="utf-8",
                )
                return next(self.results)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "repo"
            workspace.mkdir()
            (workspace / ".multiagent.json").write_text(
                json.dumps(
                    {
                        "claude": {"command": "/bin/echo"},
                        "codex": {"command": "/bin/echo"},
                    }
                ),
                encoding="utf-8",
            )
            subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
            subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=workspace, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.com"],
                cwd=workspace,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Test"],
                cwd=workspace,
                check=True,
            )
            subprocess.run(["git", "add", "."], cwd=workspace, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "initial"],
                cwd=workspace,
                check=True,
            )
            claude = WritingChatAdapter(
                "Claude",
                [AgentRunResult("Claude", "Claude 已执行", session_id="ca-write")],
            )
            codex = WritingChatAdapter(
                "Codex",
                [AgentRunResult("Codex", "Codex 已执行", session_id="cb-write")],
            )
            manager = UISessionManager(
                store=RunStore(root / "state" / "runs"),
                default_workspace=workspace,
            )
            with patch(
                "multiagent_cli.runtime.make_adapters",
                return_value={"claude": claude, "codex": codex},
            ):
                started = manager.start_task({"task": "@Claude 执行：生成结果文件"})
                run_id = started["id"]
                self._wait_for_status(manager, run_id, "ready", message_count=2)

            record = manager.store.get(run_id) or {}
            chat = record.get("group_chat") or {}
            self.assertTrue((workspace / "claude.txt").is_file())
            self.assertFalse((workspace / "codex.txt").exists())
            self.assertEqual(claude.calls[0]["mode"], "write")
            self.assertEqual(codex.calls, [])
            self.assertEqual(
                [message.get("action") for message in chat["messages"]],
                ["execute", "execute"],
            )
            changes = chat["messages"][-1]["changes"]
            self.assertTrue(changes["available"])
            self.assertEqual(changes["file_count"], 1)
            self.assertEqual(changes["additions"], 1)
            self.assertEqual(changes["deletions"], 0)
            self.assertEqual(changes["files"][0]["path"], "claude.txt")
            self.assertIn("+Claude", changes["files"][0]["patch"])

    @staticmethod
    def _wait_for_status(
        manager: UISessionManager,
        run_id: str,
        status: str,
        *,
        message_count: int = 0,
    ) -> None:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            record = manager.store.get(run_id) or {}
            messages = (record.get("group_chat") or {}).get("messages") or []
            if record.get("status") == status and len(messages) >= message_count:
                return
            time.sleep(0.01)
        raise AssertionError(f"群聊未进入 {status} 状态")

    def test_new_task_saves_uploaded_documents_and_builds_agent_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / ".multiagent.json").write_text(
                json.dumps(
                    {
                        "claude": {"command": "/bin/echo"},
                        "codex": {"command": "/bin/echo"},
                    }
                ),
                encoding="utf-8",
            )
            store = RunStore(workspace / "state")
            manager = UISessionManager(store=store, default_workspace=workspace)
            content = b"uploaded requirements"

            with patch.object(UISessionManager, "_run_session"):
                started = manager.start_task(
                    {
                        "task": "根据文档实现功能",
                        "attachments": [
                            {
                                "name": "requirements.md",
                                "size": len(content),
                                "content_type": "text/markdown",
                                "data": base64.b64encode(content).decode("ascii"),
                            }
                        ],
                    }
                )

            session = manager.session(started["id"])
            attachment = started["attachments"][0]
            attachment_path = Path(attachment["path"])

            self.assertEqual(started["task"], "根据文档实现功能")
            self.assertEqual(attachment["name"], "requirements.md")
            self.assertEqual(attachment_path.read_bytes(), content)
            if sys.platform != "win32":
                self.assertEqual(attachment_path.stat().st_mode & 0o777, 0o400)
            mirror_path = Path(attachment["workspace_path"])
            self.assertEqual(mirror_path.read_bytes(), content)
            self.assertIn(str(mirror_path), session.agent_task)
            self.assertNotIn(str(attachment_path), session.agent_task)
            self.assertIn("请先读取", session.agent_task)
            self.assertNotIn("data", attachment)

    def test_pasted_images_are_accepted_but_svg_stays_rejected(self) -> None:
        """Raster uploads back the paste-a-screenshot flow; svg must stay out
        because serving it inline would be a same-origin XSS document."""

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / ".multiagent.json").write_text(
                json.dumps(
                    {
                        "claude": {"command": "/bin/echo"},
                        "codex": {"command": "/bin/echo"},
                    }
                ),
                encoding="utf-8",
            )
            manager = UISessionManager(
                store=RunStore(workspace / "state"),
                default_workspace=workspace,
            )
            png = b"\x89PNG\r\n\x1a\n fake raster bytes"

            with patch.object(UISessionManager, "_run_session"):
                started = manager.start_task(
                    {
                        "task": "看这张截图",
                        "attachments": [
                            {
                                "name": "paste-2026-08-12T09-30-00.png",
                                "size": len(png),
                                "content_type": "image/png",
                                "data": base64.b64encode(png).decode("ascii"),
                            }
                        ],
                    }
                )

            stored = started["attachments"][0]

            self.assertEqual(stored["name"], "paste-2026-08-12T09-30-00.png")
            self.assertEqual(Path(stored["path"]).read_bytes(), png)

            with self.assertRaisesRegex(UIError, "不支持的文档格式"):
                manager.start_task(
                    {
                        "task": "这个不该被接受",
                        "attachments": [
                            {
                                "name": "payload.svg",
                                "size": 4,
                                "content_type": "image/svg+xml",
                                "data": base64.b64encode(b"<svg").decode("ascii"),
                            }
                        ],
                    }
                )

    def test_mid_chat_upload_keeps_earlier_attachments_of_the_same_run(self) -> None:
        """The per-run upload directory already exists once a task-level
        document was stored, so a later chat upload must extend it instead of
        wiping it (and must not silently overwrite a same-named file)."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "_attachments"
            first = ui_server._save_uploaded_documents(
                root,
                "run-1",
                [
                    {
                        "name": "shot.png",
                        "size": 5,
                        "content_type": "image/png",
                        "data": base64.b64encode(b"first").decode("ascii"),
                    }
                ],
            )
            second = ui_server._save_uploaded_documents(
                root,
                "run-1",
                [
                    {
                        "name": "shot.png",
                        "size": 6,
                        "content_type": "image/png",
                        "data": base64.b64encode(b"second").decode("ascii"),
                    }
                ],
            )

            self.assertEqual(Path(first[0]["path"]).read_bytes(), b"first")
            self.assertNotEqual(second[0]["name"], first[0]["name"])
            self.assertEqual(Path(second[0]["path"]).read_bytes(), b"second")

            # A rejected later upload must leave the existing files untouched.
            with self.assertRaises(UIError):
                ui_server._save_uploaded_documents(
                    root,
                    "run-1",
                    [
                        {
                            "name": "notes.md",
                            "size": 2,
                            "content_type": "text/markdown",
                            "data": base64.b64encode(b"ok").decode("ascii"),
                        },
                        {
                            "name": "bad.sh",
                            "size": 4,
                            "content_type": "text/plain",
                            "data": base64.b64encode(b"exit").decode("ascii"),
                        },
                    ],
                )

            self.assertEqual(Path(first[0]["path"]).read_bytes(), b"first")
            self.assertEqual(Path(second[0]["path"]).read_bytes(), b"second")
            self.assertFalse((root / "run-1" / "notes.md").exists())

    def test_accumulated_chat_attachments_stay_addressable_past_the_per_message_cap(
        self,
    ) -> None:
        """The per-message cap is 5, but a chat accumulates uploads over many
        turns. Re-applying the per-message cap when reading the record back
        would drop the newest entries, and an attachment absent from the record
        can no longer be downloaded."""

        stored = [
            {
                "name": f"file-{index}.png",
                "path": f"/tmp/run/file-{index}.png",
                "size": 3,
                "content_type": "image/png",
            }
            for index in range(ui_server.MAX_UPLOAD_FILES + 3)
        ]

        kept = ui_server._stored_attachments(stored)

        self.assertEqual(len(kept), len(stored))
        self.assertEqual(kept[-1]["name"], stored[-1]["name"])

        overflowing = [
            {
                "name": f"file-{index}.png",
                "path": f"/tmp/run/file-{index}.png",
                "size": 3,
                "content_type": "image/png",
            }
            for index in range(ui_server.MAX_RUN_ATTACHMENTS + 5)
        ]

        bounded = ui_server._stored_attachments(overflowing)

        # Growth is bounded, and it is the most recent uploads that survive.
        self.assertEqual(len(bounded), ui_server.MAX_RUN_ATTACHMENTS)
        self.assertEqual(bounded[-1]["name"], overflowing[-1]["name"])

    def test_chat_message_attachments_are_recorded_and_given_to_the_agent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / ".multiagent.json").write_text(
                json.dumps(
                    {
                        "claude": {"command": "/bin/echo"},
                        "codex": {"command": "/bin/echo"},
                    }
                ),
                encoding="utf-8",
            )
            claude = FakeChatAdapter(
                "Claude",
                [
                    AgentRunResult("Claude", "第一轮", session_id="ca"),
                    AgentRunResult("Claude", "看到图了", session_id="ca"),
                ],
            )
            codex = FakeChatAdapter(
                "Codex", [AgentRunResult("Codex", "第一轮", session_id="cb")]
            )
            manager = UISessionManager(
                store=RunStore(workspace / "state"),
                default_workspace=workspace,
            )
            png = b"\x89PNG\r\n\x1a\n bytes"
            with patch(
                "multiagent_cli.runtime.make_adapters",
                return_value={"claude": claude, "codex": codex},
            ):
                started = manager.start_task({"task": "开始"})
                run_id = started["id"]
                self._wait_for_status(manager, run_id, "ready")
                manager.send_chat_message(
                    run_id,
                    {
                        "message": "@Claude 看这张图",
                        "attachments": [
                            {
                                "name": "screen.png",
                                "size": len(png),
                                "content_type": "image/png",
                                "data": base64.b64encode(png).decode("ascii"),
                            }
                        ],
                    },
                )
                self._wait_for_status(manager, run_id, "ready", message_count=5)

            record = manager.store.get(run_id)
            names = [item["name"] for item in record["attachments"]]
            prompt = claude.calls[-1]["prompt"]
            user_messages = [
                message
                for message in record["group_chat"]["messages"]
                if message["role"] == "user"
            ]

        # The record must own the upload, otherwise the download route refuses it.
        self.assertIn("screen.png", names)
        # The agent gets the absolute path and reads the image with its own tool.
        self.assertIn("screen.png", prompt)
        self.assertIn("请先读取", prompt)
        self.assertEqual(
            [item["name"] for item in user_messages[-1]["attachments"]],
            ["screen.png"],
        )

    def test_attachment_prompt_points_agents_at_the_workspace_mirror(self) -> None:
        """Agents are sandboxed to the workspace, so the prompt must reference
        the in-workspace mirror copy while the record keeps the store path
        (the download route authorizes against the record)."""

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / ".multiagent.json").write_text(
                json.dumps(
                    {
                        "claude": {"command": "/bin/echo"},
                        "codex": {"command": "/bin/echo"},
                    }
                ),
                encoding="utf-8",
            )
            claude = FakeChatAdapter(
                "Claude", [AgentRunResult("Claude", "收到", session_id="ca")]
            )
            codex = FakeChatAdapter(
                "Codex", [AgentRunResult("Codex", "收到", session_id="cb")]
            )
            manager = UISessionManager(
                store=RunStore(workspace / "state"),
                default_workspace=workspace,
            )
            png = b"\x89PNG\r\n\x1a\n mirror me"
            with patch(
                "multiagent_cli.runtime.make_adapters",
                return_value={"claude": claude, "codex": codex},
            ):
                started = manager.start_task(
                    {
                        "task": "@Claude 读这张图",
                        "attachments": [
                            {
                                "name": "shot.png",
                                "size": len(png),
                                "content_type": "image/png",
                                "data": base64.b64encode(png).decode("ascii"),
                            }
                        ],
                    }
                )
                self._wait_for_status(manager, started["id"], "ready")

            record = manager.store.get(started["id"])
            stored = record["attachments"][0]
            prompt = claude.calls[-1]["prompt"]
            mirror = (
                workspace / ".multiagent" / "attachments" / started["id"] / "shot.png"
            )

            # The mirror copy exists inside the workspace with the same bytes.
            self.assertTrue(mirror.is_file())
            self.assertEqual(mirror.read_bytes(), png)
            # The prompt hands the agent the in-workspace path it can open.
            self.assertIn(str(mirror.resolve()), prompt)
            # The record stays on the store path so the download route keeps
            # working and no sandbox escape is implied.
            self.assertIn(str(manager.attachments_root.resolve()), stored["path"])
            self.assertNotIn(".multiagent", stored["path"])

    def test_delete_run_removes_the_workspace_attachment_mirror(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = RunStore(workspace / "state")
            manager = UISessionManager(store=store, default_workspace=workspace)
            record = store.start(
                task="带镜像的群聊",
                workspace=workspace,
                run_id="mirror-delete",
            )
            mirror = (
                workspace / ".multiagent" / "attachments" / record["id"]
            )
            mirror.mkdir(parents=True)
            (mirror / "shot.png").write_bytes(b"bytes")
            store.update(record["id"], status="complete", archived=True)

            manager.delete_run(record["id"])

            self.assertFalse(mirror.exists())

    def test_workspace_mirror_failure_does_not_break_the_upload(self) -> None:
        """Mirroring is best-effort: an unwritable workspace dir must not
        fail the upload, and the prompt falls back to the original path."""

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            attachments = [
                {
                    "name": "shot.png",
                    "path": str(workspace / "original.png"),
                    "size": 3,
                    "content_type": "image/png",
                }
            ]
            (workspace / "original.png").write_bytes(b"png")

            text = ui_server._task_with_attachments("看图", attachments)

            self.assertIn(str(workspace / "original.png"), text)

    def test_upload_rejects_unsafe_document_format(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / ".multiagent.json").write_text(
                json.dumps(
                    {
                        "claude": {"command": "/bin/echo"},
                        "codex": {"command": "/bin/echo"},
                    }
                ),
                encoding="utf-8",
            )
            manager = UISessionManager(
                store=RunStore(workspace / "state"),
                default_workspace=workspace,
            )
            with self.assertRaisesRegex(UIError, "不支持的文档格式"):
                manager.start_task(
                    {
                        "task": "不要运行附件",
                        "attachments": [
                            {
                                "name": "payload.sh",
                                "size": 4,
                                "content_type": "text/plain",
                                "data": base64.b64encode(b"exit").decode("ascii"),
                            }
                        ],
                    }
                )

    def test_manager_deletes_only_archived_run_and_its_attachments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = RunStore(workspace / "state")
            manager = UISessionManager(store=store, default_workspace=workspace)
            record = store.start(
                task="待删除任务",
                workspace=workspace,
                run_id="delete-archived",
            )
            attachment_directory = manager.attachments_root / record["id"]
            attachment_directory.mkdir(parents=True)
            (attachment_directory / "notes.txt").write_text("notes", encoding="utf-8")

            with self.assertRaisesRegex(UIError, "只能删除已归档"):
                manager.delete_run(record["id"])
            store.update(record["id"], status="complete", archived=True)
            deleted = manager.delete_run(record["id"])

            self.assertEqual(deleted["id"], record["id"])
            self.assertIsNone(store.get(record["id"]))
            self.assertFalse(attachment_directory.exists())

    def test_public_sessions_and_records_hide_internal_error_details(self) -> None:
        secret_error = "failed at /private/project with token sk-example-secret"
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = RunStore(workspace / "state")
            store.start(
                task="失败任务",
                workspace=workspace,
                run_id="failed-secret",
            )
            store.update("failed-secret", status="failed", error=secret_error)
            manager = UISessionManager(store=store, default_workspace=workspace)
            session = UISession(
                run_id="live-secret",
                task="活动失败任务",
                workspace=workspace,
                notify=manager.publish,
            )
            session.fail(secret_error)

            summary = manager.list_runs()[0]
            detail = manager.run_detail("failed-secret")
            live = session.to_dict()

        self.assertEqual(summary["error"], "群聊处理失败")
        self.assertEqual(detail["record"]["error"], "群聊处理失败")
        self.assertEqual(live["error"], "群聊处理失败")
        self.assertNotIn("sk-example-secret", json.dumps(detail))

    def test_resume_uses_saved_snapshot_when_config_file_was_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = RunStore(workspace / "state")
            store.start(
                task="恢复任务",
                workspace=workspace,
                run_id="resume-snapshot",
                settings_snapshot={
                    "config_path": str(workspace / "removed.json"),
                    "resolved_config": {
                        "claude": {"command": "/bin/echo"},
                        "codex": {"command": "/bin/echo"},
                    },
                },
            )
            store.update("resume-snapshot", status="failed")
            manager = UISessionManager(store=store, default_workspace=workspace)

            with patch.object(UISessionManager, "_run_session"):
                session = manager.start_task({"resume_id": "resume-snapshot"})

            self.assertEqual(session["id"], "resume-snapshot")
            self.assertEqual(session["status"], "running")

    def test_resume_rejects_path_like_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            manager = UISessionManager(
                store=RunStore(workspace / "state"),
                default_workspace=workspace,
            )
            with self.assertRaisesRegex(UIError, "任务 ID 格式无效"):
                manager.start_task({"resume_id": "../outside"})

    def test_manager_reserves_one_active_writer_per_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            manager = UISessionManager(
                store=RunStore(workspace / "state"),
                default_workspace=workspace,
            )
            first = UISession(
                run_id="first",
                task="first",
                workspace=workspace,
                notify=manager.publish,
            )
            second = UISession(
                run_id="second",
                task="second",
                workspace=workspace,
                notify=manager.publish,
            )

            manager._reserve_session(first)
            with self.assertRaisesRegex(UIError, "已有正在运行"):
                manager._reserve_session(second)

    def test_manager_refuses_shutdown_until_active_tasks_finish(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            manager = UISessionManager(
                store=RunStore(workspace / "state"),
                default_workspace=workspace,
            )
            session = UISession(
                run_id="active-shutdown",
                task="运行中任务",
                workspace=workspace,
                notify=manager.publish,
            )
            manager._reserve_session(session)

            with self.assertRaisesRegex(UIError, "仍有 1 个任务正在运行"):
                manager.ensure_shutdown_safe()

            session.status = "complete"
            manager.ensure_shutdown_safe()

    def test_last_web_client_release_shuts_down_idle_service(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            manager = UISessionManager(
                store=RunStore(workspace / "state"),
                default_workspace=workspace,
            )
            shutdown = threading.Event()
            manager.bind_shutdown_callback(shutdown.set)
            with patch.object(ui_server, "CLIENT_DISCONNECT_GRACE_SECONDS", 0.01):
                self.assertEqual(manager.claim_client("browser-1")["clients"], 1)
                self.assertEqual(manager.release_client("browser-1")["clients"], 0)
                self.assertTrue(shutdown.wait(1))

    def test_reclaimed_web_client_cancels_pending_shutdown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            manager = UISessionManager(
                store=RunStore(workspace / "state"),
                default_workspace=workspace,
            )
            shutdown = threading.Event()
            manager.bind_shutdown_callback(shutdown.set)
            with patch.object(ui_server, "CLIENT_DISCONNECT_GRACE_SECONDS", 0.05):
                manager.claim_client("browser-1")
                manager.release_client("browser-1")
                manager.claim_client("browser-1")
                self.assertFalse(shutdown.wait(0.15))

    def test_manager_archives_only_finished_runs_and_can_restore_them(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = RunStore(workspace / "state")
            store.start(
                task="归档任务",
                workspace=workspace,
                run_id="archive-run",
            )
            manager = UISessionManager(store=store, default_workspace=workspace)
            live = UISession(
                run_id="archive-run",
                task="归档任务",
                workspace=workspace,
                notify=manager.publish,
            )
            manager._reserve_session(live)

            with self.assertRaisesRegex(UIError, "运行中的任务不能归档"):
                manager.set_archived("archive-run", True)

            live.finish(0, store.update("archive-run", status="complete"))
            archived = manager.set_archived("archive-run", True)
            self.assertTrue(archived["archived"])
            self.assertTrue(archived["archived_at"])
            self.assertTrue(manager.list_runs()[0]["archived"])

            restored = manager.set_archived("archive-run", False)
            self.assertFalse(restored["archived"])
            self.assertEqual(restored["archived_at"], "")

    def test_settings_round_trip_validates_and_preserves_unknown_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            config_path = workspace / ".multiagent.json"
            config_path.write_text(
                json.dumps(
                    {
                        "custom_extension": {"enabled": True},
                        "group_chat_execution": False,
                        "consensus": True,
                        "verification": {"commands": [["python3", "-m", "unittest"]]},
                        "api_key": "never-return-this-value",
                        "claude": {
                            "command": "/bin/echo",
                            "custom_agent_field": "keep-me",
                        },
                        "codex": {"command": "/bin/echo"},
                    }
                ),
                encoding="utf-8",
            )
            manager = UISessionManager(
                store=RunStore(workspace / "state"),
                default_workspace=workspace,
            )
            loaded = manager.get_settings()
            self.assertNotIn("never-return-this-value", json.dumps(loaded))
            values = loaded["values"]
            values["group_chat_default_agent"] = "codex"
            self.assertNotIn("group_chat_execution", values)
            values["group_chat_identities"]["agent_a"] = "群聊 Claude 身份"
            values["group_chat_identities"]["agent_b"] = "群聊 Codex 身份"
            values["context_compaction"] = {
                "enabled": True,
                "threshold_tokens": 12000,
                "target_tokens": 6000,
                "recent_messages": 6,
            }
            values["claude"]["model"] = "claude-test"
            values["codex"]["model"] = "codex-test"
            values["ui"] = {
                "theme": "ocean",
                "show_archived": True,
                "compact_sidebar": True,
            }

            values["ui"]["theme"] = "unknown"
            with self.assertRaisesRegex(UIError, "界面主题必须"):
                manager.save_settings(
                    {
                        "workspace": str(workspace),
                        "revision": loaded["revision"],
                        "values": values,
                    }
                )
            values["ui"]["theme"] = "ocean"

            values["context_compaction"]["target_tokens"] = 12000
            with self.assertRaisesRegex(UIError, "target_tokens"):
                manager.save_settings(
                    {
                        "workspace": str(workspace),
                        "revision": loaded["revision"],
                        "values": values,
                    }
                )
            values["context_compaction"]["target_tokens"] = 6000

            saved = manager.save_settings(
                {
                    "workspace": str(workspace),
                    "revision": loaded["revision"],
                    "values": values,
                }
            )
            persisted = json.loads(config_path.read_text(encoding="utf-8"))

            self.assertEqual(saved["values"]["group_chat_default_agent"], "codex")
            self.assertNotIn("group_chat_execution", saved["values"])
            self.assertEqual(Path(saved["source_path"]), config_path.resolve())
            self.assertEqual(persisted["custom_extension"], {"enabled": True})
            self.assertNotIn("consensus", persisted)
            self.assertNotIn("verification", persisted)
            self.assertEqual(persisted["api_key"], "never-return-this-value")
            self.assertEqual(persisted["claude"]["custom_agent_field"], "keep-me")
            self.assertEqual(persisted["group_chat_default_agent"], "codex")
            self.assertEqual(
                persisted["context_compaction"],
                {
                    "enabled": True,
                    "threshold_tokens": 12000,
                    "target_tokens": 6000,
                    "recent_messages": 6,
                },
            )
            self.assertNotIn("group_chat_execution", persisted)
            self.assertEqual(
                persisted["group_chat_identities"]["agent_a"],
                "群聊 Claude 身份",
            )
            self.assertEqual(persisted["ui"]["theme"], "ocean")
            self.assertTrue(persisted["ui"]["compact_sidebar"])
            if sys.platform != "win32":
                self.assertEqual(config_path.stat().st_mode & 0o777, 0o600)
            defaults = manager.get_settings(defaults=True)
            self.assertEqual(defaults["values"]["ui"]["theme"], "paper")
            self.assertFalse(defaults["values"]["ui"]["compact_sidebar"])
            self.assertTrue(defaults["values"]["context_compaction"]["enabled"])
            self.assertEqual(
                defaults["values"]["group_chat_identities"]["agent_a"],
                defaults["values"]["group_chat_identities"]["agent_b"],
            )

            with self.assertRaisesRegex(UIError, "已被其他程序修改"):
                manager.save_settings(
                    {
                        "workspace": str(workspace),
                        "revision": loaded["revision"],
                        "values": values,
                    }
                )

    def test_ui_preferences_can_be_saved_without_other_form_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            config_path = workspace / ".multiagent.json"
            config_path.write_text(
                json.dumps(
                    {
                        "external_setting": "keep",
                        "custom_extension": {"keep": True},
                        "ui": {"show_archived": True},
                    }
                ),
                encoding="utf-8",
            )
            manager = UISessionManager(
                store=RunStore(workspace / "state"),
                default_workspace=workspace,
            )

            saved = manager.save_ui_preferences(
                {
                    "workspace": str(workspace),
                    "ui": {
                        "theme": "botanical",
                        "show_archived": False,
                        "compact_sidebar": True,
                    },
                }
            )
            persisted = json.loads(config_path.read_text(encoding="utf-8"))

        self.assertEqual(saved["values"]["ui"]["theme"], "botanical")
        self.assertEqual(persisted["external_setting"], "keep")
        self.assertEqual(persisted["custom_extension"], {"keep": True})
        self.assertFalse(persisted["ui"]["show_archived"])
        self.assertTrue(persisted["ui"]["compact_sidebar"])
        self.assertEqual(persisted["ui"]["theme"], "botanical")

        with tempfile.TemporaryDirectory() as directory:
            manager = UISessionManager(
                store=RunStore(Path(directory) / "state"),
                default_workspace=Path(directory),
            )
            with self.assertRaisesRegex(UIError, "界面主题必须"):
                manager.save_ui_preferences(
                    {"workspace": directory, "ui": {"theme": "unknown"}}
                )
            with self.assertRaisesRegex(UIError, "界面开关必须"):
                manager.save_ui_preferences(
                    {"workspace": directory, "ui": {"compact_sidebar": 1}}
                )
            saved = manager.save_ui_preferences(
                {"workspace": directory, "ui": {"browser_notifications": True}}
            )
            self.assertTrue(saved["values"]["ui"]["browser_notifications"])

    def _stream_session(
        self,
        workspace: Path,
        enabled: bool,
    ) -> tuple[UISession, list[tuple[str, Any]]]:
        published: list[tuple[str, Any]] = []

        def notify(kind: str, run_id: str, extra: Any = None) -> None:
            published.append((kind, extra))

        session = UISession(
            run_id="stream-run",
            task="task",
            workspace=workspace,
            notify=notify,
            stream_gate=lambda: enabled,
        )
        return session, published

    def test_streaming_is_off_until_the_interface_toggle_enables_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session, published = self._stream_session(Path(directory), False)
            session.on_event(AgentEvent("Claude", "progress", "hidden text"))

        self.assertEqual(published, [("event", None)])

    def test_stream_updates_publish_increments_not_repeated_full_text(self) -> None:
        """Both CLI parsers re-emit the whole current block on every update, so
        the session must publish only the unsent tail."""

        with tempfile.TemporaryDirectory() as directory:
            session, published = self._stream_session(Path(directory), True)

            session.on_event(AgentEvent("Claude", "progress", "Hello"))
            first = published[-1][1]
            session.on_event(AgentEvent("Claude", "progress", "Hello world"))
            second = published[-1][1]

            # Codex emits agent_message twice (item.updated then item.completed).
            before = len(published)
            session.on_event(AgentEvent("Claude", "progress", "Hello world"))
            duplicate = published[-1][1]
            after = len(published)

            session.on_event(AgentEvent("Claude", "progress", "Next block"))
            fresh = published[-1][1]

        self.assertEqual(first["stream_text"], "Hello")
        self.assertEqual(first["source"], "Claude")
        self.assertEqual(first["step_id"], "")
        self.assertEqual(second["stream_text"], " world")
        self.assertIsNone(duplicate)
        self.assertEqual(after, before + 1)
        self.assertEqual(fresh["stream_text"], "\n\nNext block")

    def test_stream_buffer_resets_between_turns(self) -> None:
        """Without a reset the next turn's identical opening text would be
        mistaken for an already-sent prefix and swallowed."""

        with tempfile.TemporaryDirectory() as directory:
            session, published = self._stream_session(Path(directory), True)
            session.on_event(AgentEvent("Claude", "progress", "First turn"))
            session.finish_chat_turn(state={}, status="ready")
            session.on_event(AgentEvent("Claude", "progress", "First turn"))
            after_finish = published[-1][1]

            session.begin_chat_turn()
            session.on_event(AgentEvent("Claude", "progress", "First turn"))
            after_begin = published[-1][1]

        self.assertEqual(after_finish["stream_text"], "First turn")
        self.assertEqual(after_begin["stream_text"], "First turn")

    def test_streaming_never_exposes_non_stream_kinds_or_touches_the_record(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session, published = self._stream_session(Path(directory), True)
            persisted: list[dict[str, Any]] = []
            session.bind_record_persistence(
                lambda event: persisted.append(event.to_dict(safe=True))
            )
            session.on_event(AgentEvent("Claude", "tool", "cat /etc/passwd"))
            tool_extra = published[-1][1]
            session.on_event(AgentEvent("Claude", "progress", "raw model text"))

        self.assertIsNone(tool_extra)
        # The record keeps the sanitized view even while SSE carries raw text.
        self.assertNotIn("raw model text", json.dumps(persisted, ensure_ascii=False))
        self.assertNotIn("cat /etc/passwd", json.dumps(persisted, ensure_ascii=False))

    def test_tool_activity_is_visible_without_enabling_text_streaming(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session, published = self._stream_session(Path(directory), False)
            session.on_event(
                AgentEvent(
                    "Codex",
                    "tool",
                    "pytest -q",
                    safe_summary="Codex · 正在执行命令",
                    metadata={
                        "activity_type": "command",
                        "tool_name": "Bash",
                        "command": "pytest -q",
                    },
                )
            )

        self.assertIsNone(published[-1][1])
        self.assertEqual(session.events[-1]["activity"]["title"], "执行命令")
        self.assertEqual(session.events[-1]["activity"]["detail"], "pytest -q")

    def test_stream_preference_defaults_off_and_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            config_path = workspace / ".multiagent.json"
            missing = ui_server._stream_preference(workspace)

            config_path.write_text(
                json.dumps({"ui": {"stream_model_text": True}}), encoding="utf-8"
            )
            enabled = ui_server._stream_preference(workspace)

            config_path.write_text("{ not json", encoding="utf-8")
            broken = ui_server._stream_preference(workspace)

            config_path.write_text(
                json.dumps({"ui": {"stream_model_text": "yes"}}), encoding="utf-8"
            )
            non_bool = ui_server._stream_preference(workspace)

        self.assertFalse(missing)
        self.assertTrue(enabled)
        self.assertFalse(broken)
        self.assertFalse(non_bool)

    def test_saving_the_stream_toggle_applies_without_a_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            manager = UISessionManager(
                store=RunStore(workspace / "state"),
                default_workspace=workspace,
            )
            self.assertFalse(manager._stream_enabled())

            saved = manager.save_ui_preferences(
                {"workspace": str(workspace), "ui": {"stream_model_text": True}}
            )
            persisted = json.loads(
                (workspace / ".multiagent.json").read_text(encoding="utf-8")
            )

            self.assertTrue(manager._stream_enabled())
            self.assertTrue(saved["values"]["ui"]["stream_model_text"])
            self.assertTrue(persisted["ui"]["stream_model_text"])

            manager.save_ui_preferences(
                {"workspace": str(workspace), "ui": {"stream_model_text": False}}
            )
            self.assertFalse(manager._stream_enabled())

            with self.assertRaisesRegex(UIError, "界面开关必须"):
                manager.save_ui_preferences(
                    {"workspace": str(workspace), "ui": {"stream_model_text": 1}}
                )

    def test_token_api_key_is_stored_privately_and_never_returned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            state_root = workspace / "state"
            manager = UISessionManager(
                store=RunStore(state_root),
                default_workspace=workspace,
            )
            loaded = manager.get_settings()
            values = loaded["values"]
            values["token_api"]["enabled"] = True
            # This test exercises credential persistence, not executable
            # discovery. CI intentionally does not install either native CLI.
            values["claude"]["command"] = [sys.executable]
            values["codex"]["command"] = [sys.executable]
            values["claude"]["models"] = [
                "claude-opus-5",
                "gemini-3.5-flash",
            ]
            values["codex"]["models"] = ["gpt-5.6-sol", "gpt-5.5"]

            saved = manager.save_settings(
                {
                    "workspace": str(workspace),
                    "revision": loaded["revision"],
                    "values": values,
                    "token_api_key": "company-private-key-7890",
                }
            )
            config_text = (workspace / ".multiagent.json").read_text(encoding="utf-8")
            credentials_path = state_root / "_credentials" / "token_api.json"
            response_text = json.dumps(saved, ensure_ascii=False)
            credentials_mode = credentials_path.stat().st_mode & 0o777

        self.assertNotIn("company-private-key-7890", config_text)
        self.assertNotIn("company-private-key-7890", response_text)
        self.assertTrue(saved["token_api_credentials"]["configured"])
        self.assertEqual(saved["token_api_credentials"]["masked"], "••••7890")
        self.assertEqual(saved["values"]["claude"]["models"][1], "gemini-3.5-flash")
        self.assertEqual(saved["values"]["codex"]["models"][1], "gpt-5.5")
        if sys.platform != "win32":
            self.assertEqual(credentials_mode, 0o600)

    def test_directory_browser_lists_children_and_workspace_shortcuts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            child = workspace / "child-project"
            child.mkdir(parents=True)
            manager = UISessionManager(
                store=RunStore(root / "state"),
                default_workspace=workspace,
            )

            listing = manager.browse_directories(str(workspace))

        self.assertEqual(listing["path"], str(workspace.resolve()))
        self.assertIn(
            {"name": "child-project", "path": str(child.resolve())},
            listing["directories"],
        )
        self.assertIn(str(workspace.resolve()), {
            item["path"] for item in listing["shortcuts"]
        })

    def test_local_http_server_serves_health_history_and_static_ui(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            selected_workspace = workspace / "opened-project"
            selected_workspace.mkdir()
            store = RunStore(workspace / "state")
            record = store.start(
                task="检查界面",
                workspace=workspace,
                run_id="run-http",
            )
            store.update(record["id"], status="complete")
            manager = UISessionManager(store=store, default_workspace=workspace)
            static_root = Path(__file__).resolve().parents[1] / "multiagent_cli" / "web"
            try:
                server = LocalUIHTTPServer(
                    ("127.0.0.1", 0),
                    make_request_handler(manager, static_root),
                )
            except PermissionError:
                self.skipTest("当前沙箱禁止绑定本机测试端口")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                health = json.loads(urlopen(f"{base}/api/health", timeout=2).read())
                workspace_request = Request(
                    f"{base}/api/workspace",
                    data=json.dumps(
                        {"workspace": str(selected_workspace)}
                    ).encode("utf-8"),
                    headers={"Content-Type": "application/json", "Origin": base},
                    method="POST",
                )
                selected = json.loads(urlopen(workspace_request, timeout=2).read())
                switched_health = json.loads(
                    urlopen(f"{base}/api/health", timeout=2).read()
                )
                runs = json.loads(urlopen(f"{base}/api/runs", timeout=2).read())
                settings = json.loads(
                    urlopen(f"{base}/api/settings", timeout=2).read()
                )
                theme_request = Request(
                    f"{base}/api/settings/interface",
                    data=json.dumps(
                        {
                            "workspace": settings["workspace"],
                            "ui": {
                                "theme": "ocean",
                                "show_archived": True,
                            },
                        }
                    ).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "Origin": base,
                    },
                    method="POST",
                )
                settings = json.loads(urlopen(theme_request, timeout=2).read())
                directories = json.loads(
                    urlopen(
                        f"{base}/api/directories?path={workspace}",
                        timeout=2,
                    ).read()
                )
                html = urlopen(f"{base}/", timeout=2).read().decode("utf-8")
                script = urlopen(f"{base}/app.js", timeout=2).read().decode("utf-8")
                style = urlopen(f"{base}/app.css", timeout=2).read().decode("utf-8")
                detail = json.loads(
                    urlopen(f"{base}/api/runs/run-http", timeout=2).read()
                )
                rename_request = Request(
                    f"{base}/api/runs/run-http/rename",
                    data=json.dumps({"title": "界面检查已重命名"}).encode("utf-8"),
                    headers={"Content-Type": "application/json", "Origin": base},
                    method="POST",
                )
                renamed = json.loads(urlopen(rename_request, timeout=2).read())
                renamed_detail = json.loads(
                    urlopen(f"{base}/api/runs/run-http", timeout=2).read()
                )
                archive_request = Request(
                    f"{base}/api/runs/run-http/archive",
                    data=json.dumps({"archived": True}).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "Origin": base,
                    },
                    method="POST",
                )
                archived = json.loads(urlopen(archive_request, timeout=2).read())
                runs_after_archive = json.loads(
                    urlopen(f"{base}/api/runs", timeout=2).read()
                )
                delete_request = Request(
                    f"{base}/api/runs/run-http",
                    headers={"Origin": base},
                    method="DELETE",
                )
                deleted = json.loads(urlopen(delete_request, timeout=2).read())
                settings["values"]["claude"]["command"] = "/bin/echo"
                settings["values"]["codex"]["command"] = "/bin/echo"
                settings["values"]["ui"]["compact_sidebar"] = True
                settings_request = Request(
                    f"{base}/api/settings",
                    data=json.dumps(
                        {
                            "workspace": settings["workspace"],
                            "revision": settings["revision"],
                            "values": settings["values"],
                        }
                    ).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "Origin": base,
                    },
                    method="POST",
                )
                saved_settings = json.loads(
                    urlopen(settings_request, timeout=2).read()
                )
                active = UISession(
                    run_id="live-http",
                    task="可停止任务",
                    workspace=workspace,
                    notify=manager.publish,
                )
                active.bind_stop_handler(lambda: None)
                manager._reserve_session(active)
                interaction_started = threading.Event()
                interaction_result: list[Any] = []

                def wait_for_interaction() -> None:
                    interaction_started.set()
                    interaction_result.append(
                        active.wait_for_native_interaction(
                            NativeInteractionRequest(
                                id="provider-http",
                                source="Codex",
                                kind="command_approval",
                                title="请求执行命令",
                                command="python3 -m unittest",
                                options=(
                                    NativeInteractionOption("approve", "允许一次"),
                                    NativeInteractionOption("cancel", "拒绝并停止"),
                                ),
                            )
                        )
                    )

                interaction_worker = threading.Thread(
                    target=wait_for_interaction,
                    daemon=True,
                )
                interaction_worker.start()
                self.assertTrue(interaction_started.wait(1))
                deadline = time.monotonic() + 1
                interaction_id = ""
                while not interaction_id and time.monotonic() < deadline:
                    requests = active.to_dict()["native_interactions"]
                    interaction_id = requests[0]["id"] if requests else ""
                    if not interaction_id:
                        time.sleep(0.01)
                interaction_request = Request(
                    f"{base}/api/sessions/live-http/interactions/{interaction_id}",
                    data=json.dumps({"action": "approve"}).encode("utf-8"),
                    headers={"Content-Type": "application/json", "Origin": base},
                    method="POST",
                )
                interaction_reply = json.loads(
                    urlopen(interaction_request, timeout=2).read()
                )
                interaction_worker.join(1)
                stop_request = Request(
                    f"{base}/api/sessions/live-http/stop",
                    data=b"{}",
                    headers={"Content-Type": "application/json", "Origin": base},
                    method="POST",
                )
                stopped = json.loads(urlopen(stop_request, timeout=2).read())
                request = Request(
                    f"{base}/api/tasks",
                    data=json.dumps({"task": "x"}).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "Origin": "https://malicious.example",
                    },
                    method="POST",
                )
                with self.assertRaises(HTTPError) as rejected:
                    urlopen(request, timeout=2)
                self.assertEqual(rejected.exception.code, 403)
                active.status = "complete"
                shutdown_request = Request(
                    f"{base}/api/shutdown",
                    data=b"{}",
                    headers={"Content-Type": "application/json", "Origin": base},
                    method="POST",
                )
                shutdown = json.loads(urlopen(shutdown_request, timeout=2).read())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

        self.assertTrue(health["ok"])
        self.assertEqual(selected["workspace"], str(selected_workspace.resolve()))
        self.assertEqual(
            switched_health["workspace"],
            str(selected_workspace.resolve()),
        )
        self.assertEqual(runs["runs"][0]["id"], "run-http")
        self.assertIn("MultiAgent 工作台", html)
        self.assertIn("<strong>Claude Code</strong>", html)
        self.assertIn('<strong>Claude Code</strong><small>群聊协作</small>', html)
        self.assertIn('<strong>Codex</strong><small>群聊协作</small>', html)
        self.assertNotIn("需求分析与协作", html)
        self.assertNotIn("工程实现与验证", html)
        self.assertNotIn("<strong>Claude</strong>", html)
        self.assertIn("新建任务", html)
        self.assertIn('data-context-action="rename"', html)
        self.assertIn('id="rename-run-dialog"', html)
        self.assertIn("已归档", html)
        self.assertIn('id="settings-dialog"', html)
        self.assertIn('id="native-interaction-dialog"', html)
        self.assertIn('id="stop-task-button"', html)
        self.assertIn('id="shutdown-ui-button"', html)
        self.assertIn('id="settings-workspace-browse"', html)
        self.assertIn('id="settings-group-chat-default-agent"', html)
        self.assertIn('id="settings-context-compaction-enabled"', html)
        self.assertIn('id="settings-context-compaction-threshold"', html)
        self.assertIn('id="settings-context-compaction-target"', html)
        self.assertIn('id="settings-context-compaction-recent"', html)
        self.assertIn('id="settings-token-api-key"', html)
        self.assertIn('id="settings-claude-model-order"', html)
        self.assertIn('id="settings-codex-model-order"', html)
        self.assertIn('id="settings-codex-reasoning-effort"', html)
        self.assertIn('id="settings-claude-permission-mode"', html)
        self.assertIn("Auto mode（原生自动判断）", html)
        self.assertIn('id="settings-group-chat-agent-a-identity"', html)
        self.assertIn('id="settings-group-chat-agent-b-identity"', html)
        self.assertLess(
            html.index('id="settings-claude-model-order"'),
            html.index('id="settings-group-chat-agent-a-identity"'),
        )
        self.assertLess(
            html.index('id="settings-codex-model-order"'),
            html.index('id="settings-group-chat-agent-b-identity"'),
        )
        self.assertEqual(html.count('class="agent-identity-field"'), 2)
        self.assertIn('name="settings-theme"', html)
        self.assertIn('value="ocean"', html)
        self.assertIn('value="graphite"', html)
        self.assertIn('value="botanical"', html)
        self.assertIn('id="composer-mention-menu"', html)
        self.assertIn('data-mention="@Claude"', html)
        self.assertIn('data-mention="@Codex"', html)
        self.assertIn('data-mention="@all"', html)
        self.assertIn('id="run-timeline"', html)
        self.assertIn('id="run-timeline-count"', html)
        self.assertIn("API Key", html)
        self.assertNotIn(">New Task<", html)
        self.assertNotIn(">Run details<", html)
        self.assertIn("formnovalidate", html)
        self.assertIn('href="./app.css"', html)
        self.assertNotIn("function renderKanban", script)
        self.assertNotIn(".kanban-board", style)
        self.assertIn("return 'Claude Code';", script)
        self.assertIn("const role = '群聊协作';", script)
        self.assertNotIn("需求分析与协作伙伴", script)
        self.assertNotIn("工程实现与验证协作伙伴", script)
        self.assertIn("function renderTimeline", script)
        self.assertIn("function renderActivityEvent", script)
        self.assertIn("function coalesceActivityEvents", script)
        self.assertIn("function activityStepKey", script)
        self.assertIn("function normalizedActivityStatus", script)
        self.assertIn("terminalByStep", script)
        self.assertIn("activity.result_detail", script)
        self.assertIn("activity-card", script)
        self.assertIn("实时回复预览", html)
        self.assertNotIn("function eventMessage", script)
        self.assertNotIn("phase.includes('review')", script)
        self.assertIn("function renderDirectFileNotice", script)
        self.assertIn("function shutdownUiService", script)
        self.assertIn("section.addEventListener('toggle'", script)
        self.assertIn("function claimWebClient", script)
        self.assertIn("function releaseWebClient", script)
        self.assertIn("window.addEventListener('pagehide'", script)
        self.assertIn("function loadWorkspaceDirectory", script)
        self.assertIn("function renderModelOrder", script)
        self.assertIn("dragHandle.addEventListener('dragstart'", script)
        self.assertNotIn("不能直接接入的模型与原因", html)
        self.assertNotIn("function renderModelCompatibility", script)
        self.assertIn("function handleComposerKeydown", script)
        self.assertIn("function updateMentionMenu", script)
        self.assertIn("function insertMention", script)
        self.assertIn("function changeSummaryMarkup", script)
        self.assertIn("function changeFileMarkup", script)
        self.assertIn("function queuePendingChatMessage", script)
        self.assertIn("function optimisticChatRecipients", script)
        self.assertIn("function reconcilePendingChatMessages", script)
        self.assertIn("function replyLoadingMarkup", script)
        self.assertIn("function refreshDefaultWorkspace", script)
        self.assertIn("function openRunRename", script)
        self.assertIn("function renderNativeInteraction", script)
        self.assertEqual(interaction_reply["native_interactions"], [])
        self.assertEqual(interaction_result[0].action, "approve")
        self.assertIn("function submitRunRename", script)
        self.assertIn("function updateNewTaskMode", script)
        self.assertIn("function applyTheme", script)
        self.assertIn("function saveInterfacePreferences", script)
        self.assertIn("function currentInterfaceSettings", script)
        self.assertIn("/api/settings/interface", script)
        self.assertIn("saveInterfacePreferences({ show_archived:", script)
        self.assertIn("saveInterfacePreferences({ compact_sidebar:", script)
        self.assertIn("saveInterfacePreferences(defaults.values?.ui || {})", script)
        self.assertIn("document.body.dataset.theme = theme", script)
        self.assertIn("el.taskInput.required = false", script)
        self.assertNotIn("第一条消息（可选）", script)
        self.assertIn("直接建立空群聊", html)
        self.assertNotIn("添加参考文档或图片", html)
        self.assertIn("update.type === 'workspace'", script)
        self.assertIn("run.workspace === workspace", script)
        self.assertIn("scrollChatToBottom", script)
        self.assertIn("!event.shiftKey", script)
        self.assertIn("requestSubmit(el.quickTaskSubmit)", script)
        self.assertIn("newTaskFiles: []", script)
        self.assertIn("composerFiles: []", script)
        self.assertIn("addTaskFiles(el.composerFileInput.files, 'composer')", script)
        self.assertIn("addTaskFiles(el.documentInput.files, 'task')", script)
        self.assertIn("function previewUrlFor", script)
        self.assertIn("appendImageThumbnail", script)
        self.assertIn(".composer-attachment.is-image", style)
        self.assertIn("if (!image)", script)
        self.assertIn(".composer-attachment-thumb", style)
        self.assertIn("if (!task && state.composerFiles.length) task = '请查看并分析附件。'", script)
        self.assertNotIn("el.quickTaskInput.value = '请查看附件图片。'", script)
        self.assertNotIn("el.quickAttach.classList.toggle('hidden', groupChat)", script)
        self.assertIn("attachments.map((item) => ({", script)
        self.assertIn(".message-row.message-user", style)
        self.assertIn(".message-row.message-claude", style)
        self.assertIn(".message-row.message-codex", style)
        self.assertIn(".change-summary", style)
        self.assertIn(".diff-preview", style)
        self.assertIn(".message-row.message-pending", style)
        self.assertIn(".message-row.message-loading", style)
        self.assertIn(".message-row.message-failed", style)
        self.assertIn(".message-row.message-recalled", style)
        self.assertIn("message.failure_reason", script)
        self.assertIn("failureReason === 'timeout' ? '响应超时'", script)
        self.assertIn("failureReason === 'model_incompatible' ? '模型不兼容'", script)
        self.assertIn("orderGroupChatMessages", script)
        self.assertIn("reply_to: turn.server_user_id || turn.client_id", script)
        self.assertIn("max-width: min(78%, 820px)", style)
        self.assertIn("width: fit-content", style)
        self.assertIn(".message-row.message-claude.message-loading .message-main", style)
        self.assertIn("@keyframes reply-bounce", style)
        self.assertIn(".composer-attachment-list", style)
        self.assertIn("settings-browser-notifications", html)
        self.assertIn("function notifyBrowser", script)
        self.assertIn("selectRun(update.run_id);", script)
        self.assertIn("正在等待你的权限决定或补充信息", script)
        self.assertIn("function dedupeGroupChatMessages", script)
        self.assertIn("function groupChatReplyKey", script)
        self.assertIn("serverMessages.slice(optimistic.server_message_count)", script)
        self.assertIn("!serverReplyKeys.has(groupChatReplyKey(reply))", script)
        self.assertNotIn("if (optimistic.delivery_status === 'sending') return true", script)
        self.assertIn("replacedMessageIds", script)
        self.assertIn("delivery_status: 'sending'", script)
        self.assertIn("state.currentId === runId", script)
        self.assertIn("state.detail.session = acceptedSession", script)
        self.assertIn("旧回复已删除，正在重新生成", script)
        self.assertIn("node.remove();", script)
        self.assertIn("function streamTextByAgent", script)
        self.assertIn("data-loading-agent", script)
        self.assertIn("loadingNode?.dataset.feedKey", script)
        self.assertIn("loading && !streaming", script)
        self.assertIn("function saveDraftNow", script)
        self.assertIn("data-message-edit", script)
        self.assertIn("data-message-retry", script)
        self.assertIn("data-message-context", script)
        self.assertIn("data-message-recall", script)
        self.assertIn("function recalledMessageMarkup", script)
        self.assertIn("message-recalled-notice", style)
        self.assertIn("function recallMessage", script)
        self.assertIn("function latestRecallableUserMessage", script)
        self.assertIn("event.key === 'Escape'", script)
        self.assertIn("el.quickTaskInput.value = restoredText", script)
        self.assertIn("function toggleMessageContext", script)
        self.assertIn("include_in_context", script)
        self.assertIn("未加入共同上下文", script)
        self.assertIn("detailRequestSequence", script)
        self.assertIn("requestSequence !== state.detailRequestSequence", script)
        self.assertIn(".message-tools", style)
        self.assertIn(".message-attachment-image", style)
        self.assertIn('id="image-lightbox"', html)
        self.assertIn("data-image-lightbox", script)
        self.assertIn("function openImageLightbox", script)
        self.assertIn("message-attachment-dimensions", style)
        self.assertIn("header-button-label", style)
        self.assertIn("settings-advanced", html)
        self.assertIn('<details class="settings-advanced" open>', html)
        self.assertIn('<details class="settings-guidance" open>', html)
        self.assertIn("settings-advanced-body", style)
        self.assertIn("settings-guidance", html)
        self.assertIn("settings-guidance-grid", style)
        self.assertIn(".markdown-body .code-block", style)
        self.assertIn(".feed-jump", style)
        self.assertIn(".message-streaming", style)
        self.assertIn('body[data-theme="ocean"]', style)
        self.assertIn('body[data-theme="graphite"]', style)
        self.assertIn('body[data-theme="botanical"]', style)
        self.assertEqual(directories["path"], str(workspace.resolve()))
        self.assertEqual(detail["record"]["status"], "complete")
        self.assertEqual(renamed["record"]["display_task"], "界面检查已重命名")
        self.assertEqual(
            renamed_detail["record"]["display_task"],
            "界面检查已重命名",
        )
        self.assertEqual(renamed_detail["record"]["task"], "检查界面")
        self.assertTrue(archived["record"]["archived"])
        self.assertTrue(runs_after_archive["runs"][0]["archived"])
        self.assertEqual(deleted["record"]["id"], "run-http")
        self.assertIsNone(manager.store.get("run-http"))
        self.assertTrue(saved_settings["values"]["ui"]["compact_sidebar"])
        self.assertTrue(saved_settings["values"]["ui"]["show_archived"])
        self.assertEqual(saved_settings["values"]["ui"]["theme"], "ocean")
        self.assertEqual(stopped["status"], "stopping")
        self.assertTrue(shutdown["ok"])

    def test_attachment_route_serves_images_inline_and_documents_as_download(
        self,
    ) -> None:
        """Inline rendering backs the chat thumbnail. The content type must come
        from the validated extension, never from the uploader's declared type,
        and non-raster uploads must never be renderable in the page."""

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = RunStore(workspace / "state")
            store.start(
                task="附件下载",
                workspace=workspace,
                run_id="run-files",
            )
            manager = UISessionManager(store=store, default_workspace=workspace)
            png = b"\x89PNG\r\n\x1a\n raster"
            saved = ui_server._save_uploaded_documents(
                manager.attachments_root,
                "run-files",
                [
                    {
                        "name": "shot.png",
                        "size": len(png),
                        # A lying content_type must not decide how we serve it.
                        "content_type": "text/html",
                        "data": base64.b64encode(png).decode("ascii"),
                    },
                    {
                        "name": "notes.md",
                        "size": 5,
                        "content_type": "text/markdown",
                        "data": base64.b64encode(b"notes").decode("ascii"),
                    },
                ],
            )
            store.update("run-files", attachments=saved)
            static_root = Path(__file__).resolve().parents[1] / "multiagent_cli" / "web"
            try:
                server = LocalUIHTTPServer(
                    ("127.0.0.1", 0),
                    make_request_handler(manager, static_root),
                )
            except PermissionError:
                self.skipTest("当前沙箱禁止绑定本机测试端口")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                inline = urlopen(
                    f"{base}/api/runs/run-files/attachments/shot.png?inline=1",
                    timeout=2,
                )
                inline_body = inline.read()
                download = urlopen(
                    f"{base}/api/runs/run-files/attachments/shot.png",
                    timeout=2,
                )
                download_body = download.read()
                document = urlopen(
                    f"{base}/api/runs/run-files/attachments/notes.md",
                    timeout=2,
                )
                document_body = document.read()
                with self.assertRaises(HTTPError) as inline_document:
                    urlopen(
                        f"{base}/api/runs/run-files/attachments/notes.md?inline=1",
                        timeout=2,
                    )
                with self.assertRaises(HTTPError) as traversal:
                    urlopen(
                        f"{base}/api/runs/run-files/attachments/"
                        "..%2F..%2Fstate%2Findex.json",
                        timeout=2,
                    )
                with self.assertRaises(HTTPError) as unknown_run:
                    urlopen(
                        f"{base}/api/runs/run-missing/attachments/shot.png",
                        timeout=2,
                    )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

        self.assertEqual(inline_body, png)
        self.assertEqual(inline.headers["Content-Type"], "image/png")
        self.assertTrue(inline.headers["Content-Disposition"].startswith("inline"))
        self.assertEqual(inline.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(download_body, png)
        self.assertTrue(
            download.headers["Content-Disposition"].startswith("attachment")
        )
        # The lying "text/html" the uploader declared must never come back.
        self.assertEqual(download.headers["Content-Type"], "image/png")
        self.assertEqual(document_body, b"notes")
        # Non-raster uploads are opaque bytes, never a renderable type.
        self.assertEqual(
            document.headers["Content-Type"], "application/octet-stream"
        )
        self.assertEqual(inline_document.exception.code, 404)
        self.assertEqual(traversal.exception.code, 404)
        self.assertEqual(unknown_run.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
