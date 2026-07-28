# MutiAgent Bridge

在同一个终端里，让 Claude Code 与 Codex CLI 共同完成一个开发任务：主 Agent 负责提出方案和修改代码，副 Agent 独立理解需求、审查方案和验收实现，主 Agent 再根据反馈修订。

当前版本：`1.1.0` · Python：`>= 3.9` · 运行时依赖：无

> MutiAgent 是原生 CLI 的编排层，不是新的模型 API 客户端。认证、模型访问、文件工具和沙箱仍由 Claude Code 与 Codex CLI 自己负责。

## 目录

- [项目定位](#项目定位)
- [快速开始](#快速开始)
- [协作流程](#协作流程)
- [常用运行模式](#常用运行模式)
- [命令参考](#命令参考)
- [配置](#配置)
- [证据化共识与共享任务板](#证据化共识与共享任务板)
- [Worktree 隔离与交付](#worktree-隔离与交付)
- [断点恢复与运行记录](#断点恢复与运行记录)
- [终端界面与输出](#终端界面与输出)
- [质量评测](#质量评测)
- [权限与安全](#权限与安全)
- [项目结构](#项目结构)
- [开发与测试](#开发与测试)
- [限制与故障排查](#限制与故障排查)

## 项目定位

MutiAgent 解决的是“两个成熟编码 Agent 如何围绕同一项需求形成闭环”，而不是重新实现一个编码 Agent。

它提供以下能力：

- 在同一工作区顺序调用 Claude Code 和 Codex CLI。
- 在副 Agent 看到主方案前，先让它独立分析同一需求，减少迎合主方案的倾向。
- 将方案审查、争议、需求覆盖和证据保存为结构化状态。
- 可选地反复执行“主 Agent 修订方案 → 副 Agent 复审”，直到达成证据化共识。
- 用户确认方案后才进入写入阶段；也可在自动化环境中显式跳过确认。
- 独立运行配置好的测试、lint、类型检查或构建命令，并把真实结果交给副 Agent 验收。
- 使用 Git worktree 隔离任务，保存阶段检查点，并从最后一个完整成功阶段恢复。
- 汇总历史完成率、验收率、验证率、问题严重级别、耗时和 Token。

它当前不是：

- 任意数量、任意厂商 Agent 的通用群聊框架；正式桥接流程固定为 Claude Code 与 Codex CLI 两个角色。
- 并行写代码系统；两个 Agent 按阶段顺序工作，不会同时修改同一工作区。
- 自动合并器；worktree 中的结果由用户检查、提交和合并。
- API Key 托管器；正式入口不会读取 `agents.json` 或代管密钥。

### 正式桥接与早期直连 API 代码

项目中保留了早期的直连 API 实验实现，主要位于 `client.py`、`config.py`、`models.py` 和 `orchestrator.py`，相关测试也仍然存在。它与当前正式命令不是同一条执行链：

- `mutiagent` / `multiagent` → `multiagent_cli.cli:main` → 原生 Claude Code、Codex CLI 桥接。
- `agents.json` → 仅属于早期直连 API 实验，不会被当前 `mutiagent` 命令加载。
- 当前桥接配置使用 `.mutiagent.json`、用户级 `config.json` 或 `bridge.json`。

因此，遇到旧版直连 API 的 `404 Route Not Found` 时，不应继续修改 `agents.json` 来驱动当前桥接器；应先运行 `mutiagent doctor` 检查两套原生 CLI。

## 快速开始

### 1. 准备环境

需要：

- Python 3.9 或更高版本。
- 已安装并完成认证的 Claude Code 命令 `claude`。
- 已安装并完成认证的 Codex CLI 命令 `codex`。
- 如需 `--worktree`，目标项目还必须是已有提交且工作区干净的 Git 仓库。

MutiAgent 优先从 `PATH` 查找命令，也会检查这些常见位置：

- Claude Code：`~/.local/bin/claude`
- Codex CLI：`~/Applications/ChatGPT.app/Contents/Resources/codex`
- Codex CLI：`/Applications/ChatGPT.app/Contents/Resources/codex`

找不到时可以在配置中显式填写可执行文件路径。

### 2. 安装命令

开发环境推荐可编辑安装：

```bash
python3 -m pip install -e /path/to/multiagent1
```

项目同时注册了两个等价命令：

```text
mutiagent    推荐名称，本文统一使用
multiagent   拼写兼容别名
```

当前仓库也提供轻量启动器 `bin/mutiagent`。若仓库固定在现有路径，可将它链接到用户命令目录：

```bash
mkdir -p ~/.local/bin
ln -s /Users/baolu_ding/Desktop/multiagent1/bin/mutiagent ~/.local/bin/mutiagent
```

`bin/mutiagent` 内含本机项目绝对路径；移动仓库后应重新安装包，或同步修改启动器。

### 3. 检查环境

先进行不调用模型的快速检查：

```bash
mutiagent --check
```

再检查 CLI、认证、工作区、状态目录和模型配置：

```bash
mutiagent doctor
```

`doctor` 默认不发起模型请求。只有显式增加下面的参数才会分别发送一次最小探测请求：

```bash
mutiagent doctor --probe-models
```

### 4. 在任意项目中运行

```bash
cd /path/to/your-project
mutiagent "修复当前失败的测试，并审查改动"
```

默认会在实施前请求人工确认方案。脚本、管道或其他非交互环境必须显式允许自动确认：

```bash
mutiagent --yes "修复当前失败的测试，并审查改动"
```

不提供任务时进入统一交互终端：

```bash
mutiagent
```

## 协作流程

默认主 Agent 为 Claude，副 Agent 为 Codex。使用 `--lead codex` 可以交换角色。

```text
1. 捕获任务开始前的 Git / 文件状态
2. 主 Agent 在只读模式下提出实施方案
3. 副 Agent 在未看到主方案时独立解析需求和风险
4. 副 Agent 比较两份理解并审查主方案
5. [可选] 主 Agent 修订方案，副 Agent 复审，直到共识或达到上限
6. [默认] 用户批准、要求修订或取消方案
7. 主 Agent 在写入模式下实施
8. Bridge 直接执行配置好的确定性验证命令
9. 副 Agent 读取当前工作区，并结合需求、方案、基线和验证日志做结构化验收
10. 如需修改，主 Agent 恢复原会话修订，再进入下一轮验收
11. 保存最终状态、质量指标、共享任务板和可恢复检查点
```

几个容易混淆的概念：

- **方案共识**发生在写代码之前，默认关闭。它判断双方是否对需求、架构、失败路径、兼容性和测试计划形成了有证据的共同结论。
- **代码审查**发生在实施之后，默认 1 轮。它判断实际实现是否满足需求，并使用真实验证结果作为证据。
- **人工方案门禁**默认开启。即使双方达成共识，仍由用户决定执行、要求修订或取消。
- **最终审查**默认开启。最后一次主 Agent 修订后，副 Agent 会再验收一次，避免以未复核的改动结束。

## 常用运行模式

### 默认：方案预审 + 一轮代码审查

```bash
mutiagent "完成任务"
```

### Codex 实施，Claude 审查

```bash
mutiagent --lead codex "完成任务"
```

### 开启方案自动共识

```bash
mutiagent --consensus "完成任务"
```

副 Agent 不接受时，会执行“主 Agent 修订 → 副 Agent 复审”，最多运行 `max_consensus_rounds` 次。仍未达成共识时任务停止，不进入写入阶段。

### 调整代码审查轮数

```bash
mutiagent --rounds 2 "完成任务"
```

`--rounds 0` 会关闭实施后的常规代码审查，但仍保留需求和方案预审：

```bash
mutiagent --rounds 0 "完成任务"
```

### 单 Agent 模式

```bash
mutiagent --no-requirement-review --rounds 0 "完成任务"
```

这会跳过副 Agent 的需求分析、方案审查和代码审查。它适合低风险任务或对照评测，但不再具备多 Agent 质量门禁。

### Worktree 隔离

```bash
mutiagent --worktree "完成任务"
```

### 指定工作区和配置

```bash
mutiagent -C /path/to/project \
  --config /path/to/bridge.json \
  "完成任务"
```

## 命令参考

### 运行参数

| 参数 | 作用 | 默认值 |
| --- | --- | --- |
| `-C, --workspace PATH` | 指定目标工作区 | 当前目录 |
| `-c, --config PATH` | 显式指定桥接配置 | 自动发现 |
| `--lead claude\|codex` | 选择主写入 Agent | `claude` |
| `--rounds N` | 最大代码审查/修订轮数，允许 `0` | `1` |
| `--consensus` | 开启方案自动协商 | 关闭 |
| `--no-consensus` | 覆盖配置并关闭方案自动协商 | — |
| `-y, --yes` | 跳过人工方案确认 | 关闭 |
| `--no-requirement-review` | 跳过独立需求分析和方案预审 | 关闭 |
| `--no-final-review` | 最后一次修订后不追加最终审查 | 关闭 |
| `--worktree` / `--no-worktree` | 开启/覆盖关闭 Git worktree 隔离 | 关闭 |
| `--tui` / `--no-tui` | 强制启用/禁用固定 TUI | TTY 中自动启用 |
| `--show-details` | 显示中间文本、工具命令和原生日志 | 隐藏 |
| `--verbose-events` | `--show-details` 的兼容名称 | 隐藏 |
| `--plain` | 禁用 ANSI 颜色 | 自动检测 |
| `--check` | 只检查 CLI 路径与版本 | — |
| `--probe-models` | 与 `doctor` 一起实际探测模型 | 关闭 |
| `--version` | 显示版本 | — |

`--no-requirement-review` 不能与 `--consensus` 同时使用。

### 管理命令

```bash
# 在目标项目生成 .mutiagent.json；已存在时不会覆盖
mutiagent init

# 环境诊断
mutiagent doctor
mutiagent doctor --probe-models

# 历史和任务中心
mutiagent history
mutiagent tasks
mutiagent task <run-id>

# 隔离任务的路径、差异和清理
mutiagent task path <run-id>
mutiagent task diff <run-id>
mutiagent task discard <run-id> --force

# 恢复最近任务或指定任务
mutiagent resume
mutiagent resume <run-id>

# 离线质量报告
mutiagent eval
mutiagent eval --json
```

### 交互命令

运行 `mutiagent` 进入交互终端后可使用：

```text
/lead claude|codex       切换主 Agent
/consensus on|off        开启或关闭方案自动协商
/details on|off          显示或隐藏内部执行详情
/rounds N                设置代码审查轮数
/model claude|codex NAME 设置模型，NAME 可为 default
/timeout SECONDS         同时设置两个 Agent 的单次调用超时
/history                 查看任务历史
/tasks                   查看任务中心与当前阶段
/task RUN_ID             查看共享任务、争议和 worktree
/eval                    查看历史质量评测
/worktree on|off         设置后续新任务是否隔离
/resume [run-id]         恢复历史任务
/retry                   重新运行上一项需求
/doctor                  检查 CLI、认证与工作区
/status                  查看当前配置
/help                    查看命令
/exit                    退出
```

原生 CLI 调用失败时，交互终端会提供：重试、交换主副角色、展开详情后重试或保存并退出。

## 配置

配置不是必需的。可先在目标项目生成模板：

```bash
mutiagent -C /path/to/project init
```

### 查找顺序

配置按以下优先级加载，找到第一项后停止：

1. `--config /path/to/config.json`
2. 环境变量 `MUTIAGENT_CONFIG`
3. 目标工作区中的 `.mutiagent.json`
4. `~/.config/mutiagent/config.json`
5. MutiAgent 仓库根目录中的 `bridge.json`
6. 内置默认值

命令行参数会覆盖配置文件。恢复任务时优先恢复记录中的运行快照，再由本次显式传入的 `--lead`、`--rounds`、`--consensus` 等参数覆盖。

完整示例见 [`bridge.example.json`](bridge.example.json)：

```json
{
  "lead": "claude",
  "requirement_review": true,
  "consensus": false,
  "max_consensus_rounds": 3,
  "plan_approval": true,
  "max_plan_revisions": 2,
  "review_rounds": 1,
  "final_review": true,
  "worktree": false,
  "identities": {
    "lead": "你是主 Agent，负责方案、实施和响应审查。",
    "reviewer": "你是独立副 Agent，负责建议方案、风险检查和验收。"
  },
  "verification": {
    "timeout": 300,
    "commands": [
      {
        "name": "unit-tests",
        "command": [
          "python3",
          "-m",
          "unittest",
          "discover",
          "-s",
          "tests",
          "-v"
        ]
      },
      {
        "name": "lint",
        "command": ["ruff", "check", "."],
        "timeout": 120
      }
    ]
  },
  "claude": {
    "command": "/absolute/path/to/claude",
    "model": null,
    "timeout": 900,
    "extra_args": []
  },
  "codex": {
    "command": "/absolute/path/to/codex",
    "model": null,
    "timeout": 900,
    "extra_args": []
  }
}
```

若 CLI 已能被自动发现，应删除 `command` 字段，避免机器迁移后路径失效。

### 字段说明

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `lead` | `claude` / `codex` | `claude` | 主写入 Agent；另一方自动成为 Reviewer |
| `requirement_review` | boolean | `true` | 是否执行独立需求解析和方案审查 |
| `consensus` | boolean | `false` | 是否自动修订方案直到证据化共识 |
| `max_consensus_rounds` | integer ≥ 1 | `3` | 方案自动修订次数上限 |
| `plan_approval` | boolean | `true` | 写代码前是否要求人工批准方案 |
| `max_plan_revisions` | integer ≥ 0 | `2` | 人工要求修订方案的次数上限 |
| `review_rounds` | integer ≥ 0 | `1` | 实施后的代码审查/修订轮数 |
| `final_review` | boolean | `true` | 最后一次修订后是否再审查一次 |
| `worktree` | boolean | `false` | 是否默认创建隔离 worktree |
| `identities.lead` | string | 内置身份 | 注入每个主 Agent 请求的职责契约 |
| `identities.reviewer` | string | 内置身份 | 注入每个副 Agent 请求的职责契约 |
| `verification.timeout` | number > 0 | `300` | 验证命令默认超时，单位秒 |
| `verification.commands` | array | `[]` | Bridge 独立执行的确定性检查 |
| `claude.command` | string / string[] | 自动发现 | Claude Code 启动命令 |
| `codex.command` | string / string[] | 自动发现 | Codex CLI 启动命令 |
| `claude.model` / `codex.model` | string / `null` | `null` | `null` 表示沿用原生 CLI 默认模型 |
| `claude.timeout` / `codex.timeout` | number > 0 | `900` | 单次 Agent 调用超时，单位秒 |
| `claude.extra_args` / `codex.extra_args` | string[] | `[]` | 追加到原生 CLI 的参数 |

验证命令和 CLI 命令都可以写成字符串或字符串数组。字符串会通过 `shlex.split` 拆分，但验证命令**不经过 shell**，因此不会解释管道、重定向、`&&`、变量展开或命令替换。复杂检查应写入项目脚本，再将脚本作为单个命令调用。

### 模型与认证

`model: null` 表示不覆盖原生 CLI 的模型选择。若使用网关或中转服务，应按 Claude Code 与 Codex CLI 各自支持的方式配置环境变量或认证，不要把密钥写入桥接配置。

正式桥接器：

- 不读取 API Key。
- 不把一个厂商的 Key 转换给另一个 CLI。
- 不负责判断中转站支持哪些模型名称。
- 只把配置的 `model` 传给对应原生 CLI。

## 证据化共识与共享任务板

### 为什么先独立分析需求

副 Agent 的第一次需求分析请求不会包含主方案。这样它可以独立识别遗漏的约束、兼容性问题、失败路径和测试缺口。之后才让它比较双方理解并审查主方案。

### 共识如何判断

新会话使用 `mutiagent.consensus.v2` 结构化协议。共识不是依据“同意”“看起来可以”等自然语言，而是同时满足：

1. `requirements`：需求理解完整。
2. `architecture`：方案结构可实施且职责清晰。
3. `failure_paths`：异常、边界和恢复路径已覆盖。
4. `compatibility`：兼容性、已有行为和用户改动得到保护。
5. `testing`：验证方法能够证明结果。
6. 每条 `REQ-*` 都有覆盖记录和证据。
7. 所有 P0/P1 `ISSUE-*` 都已解决且附有证据。
8. 不存在剩余分歧或必需修订项。

任何一项失败、响应格式无效或证据不足，都按“尚未达成共识”处理。为了防止通过省略问题来制造假共识，后续回复未提及的未解决争议会继续保留在共享状态中。旧版 v1 和 `SOLUTION_VERDICT` 响应仍可解析，但新请求会要求 v2 格式。

### 共享任务板

每个运行默认维护这些任务：

```text
plan            主 Agent 提出只读方案
requirements    副 Agent 独立解析需求
plan-review     副 Agent 审查方案与争议
implementation  主 Agent 实施
verification    Bridge 执行确定性验证
code-review     副 Agent 验收实现
```

任务状态包括：`pending`、`in_progress`、`blocked`、`done`、`failed`、`skipped`。

Agent 间消息使用结构化类型保存：`proposal`、`analysis`、`review`、`revision`、`evidence` 和 `status`。提示词中的共享上下文只携带压缩后的近期消息，完整产物、需求台账和争议台账保存在运行检查点中。

## Worktree 隔离与交付

Worktree 模式适合让 Agent 的修改与当前开发目录隔离：

```bash
mutiagent --worktree "完成任务"
```

### 创建条件

目标工作区必须：

- 位于 Git 仓库中。
- 仓库至少有一个提交。
- `git status --porcelain` 为空。

MutiAgent 会创建：

- 分支：`mutiagent/<run-id>`
- worktree：默认位于 `~/.local/state/mutiagent/worktrees/<repo-hash>/<run-id>/`

如果 `-C` 指向仓库内的子目录，新 worktree 会保留对应的相对子目录作为 Agent 工作区。

### 查看结果

```bash
mutiagent tasks
mutiagent task <run-id>
mutiagent task path <run-id>
mutiagent task diff <run-id>
```

`task diff` 同时显示已跟踪和未跟踪文件。任务完成后 worktree 会保留，MutiAgent 不会自动提交、合并或删除。

### 交付建议

先取得任务目录并检查：

```bash
cd "$(mutiagent task path <run-id>)"
git status
git diff
```

确认后可在隔离分支中自行提交，再回到原仓库合并 `mutiagent/<run-id>`。提交和合并策略由项目维护者决定，MutiAgent 不会代替用户执行。

### 放弃任务

```bash
mutiagent task discard <run-id> --force
```

该操作会删除 worktree，并强制删除对应的 `mutiagent/<run-id>` 分支。没有 `--force` 时，如果 worktree 仍有改动，MutiAgent 会拒绝删除。

## 断点恢复与运行记录

### 保存位置

运行记录默认保存在：

```text
~/.local/state/mutiagent/runs/<run-id>.json
```

可以用环境变量 `MUTIAGENT_STATE_DIR` 覆盖运行记录目录。启用 worktree 时，隔离目录会创建在该目录同级的 `worktrees/` 下。

运行 ID 形如：

```text
20260728-153000-a1b2c3
```

状态目录权限会设置为 `0700`，记录文件为 `0600`。记录采用临时文件替换方式更新，降低写入中断造成的损坏风险。

### 保存内容

运行记录包含：

- 原始任务、工作区、主 Agent 和配置快照。
- Claude 会话 ID 与 Codex thread ID。
- 各阶段的最终回复、结构化审查和验证结果。
- 共享任务、消息、需求台账和争议台账。
- worktree 路径、分支和源工作区。
- 阶段检查点、质量指标、耗时与 CLI 返回的 Token 使用量。

这些内容可能包含需求、代码片段、文件路径和 Agent 输出，应按敏感开发数据对待，不要公开上传运行记录目录。

### 恢复方式

```bash
mutiagent resume
mutiagent resume <run-id>
```

恢复不是重新执行整项任务。Bridge 会从最后一个**完整成功阶段**继续，例如：

- 主方案已经完成 → 不再重复生成主方案。
- 方案审查已经完成 → 从共识修订或人工确认继续。
- 代码审查已完成、主 Agent 尚未修订 → 从修订阶段继续。
- 确定性验证已完成 → 不重复运行已确认完成的检查。

### 工作区指纹

恢复前会验证任务、主 Agent、工作区路径和内容指纹：

- Git 工作区记录分支、HEAD、状态、diff，以及未跟踪文件内容或元数据。
- 非 Git 工作区记录递归文件状态。
- `.git`、`.mutiagent` 和 `__pycache__` 等内部目录不会参与普通文件扫描。

如果检查点之后工作区被外部修改，恢复会拒绝继续，避免把未知变化误认为某个 Agent 已完成的结果。目前没有“强制忽略指纹继续”的选项，应先人工检查任务目录和差异。

检查点只在一次完整 Agent 回合或验证步骤成功后推进。如果 Agent 在写入过程中超时或崩溃，半完成修改可能已经存在，但不会被标记为成功阶段；此时需要人工检查，不能依赖自动恢复猜测状态。

## 终端界面与输出

### 开场页

不带任务进入交互模式时，MutiAgent 会先清屏，再显示结合 Claude Code 与 Codex 风格的开场页、工作区、主副 Agent、审查轮数和共识状态。

一次性命令、管道和重定向不会清屏，也不会插入开场页。

### 固定 TUI

真实 TTY 中执行任务时默认启用固定 TUI，展示：

- 当前阶段和总耗时。
- Claude / Codex 调用次数与 Token。
- 共享任务状态。
- 未解决的 P0/P1 问题。
- 已收集证据数量和当前阻塞原因。

需要人工确认方案、发生失败或输出最终结果时，会退出固定界面并打印可保存的正常终端文本。

```bash
mutiagent --tui "完成任务"
mutiagent --no-tui "完成任务"
```

### 默认隐藏执行细节

默认只显示阶段状态和各 Agent 的最终回复。中间思考文本、工具调用、执行命令、命令结果和原生事件日志会被折叠。排障时可临时展开：

```bash
mutiagent --show-details "完成任务"
```

在交互终端中使用：

```text
/details on
/details off
```

### 表格显示

Renderer 会按中英文实际显示宽度渲染 Markdown 表格：

- 长单元格在列内折行。
- 终端过窄或表格超过 6 列时转成逐条记录。
- 不希望颜色和 ANSI 样式时使用 `--plain`。

## 质量评测

每次运行会记录：

- 是否完成、是否通过最终验收。
- 确定性验证通过数与总数。
- P0、P1、P2、P3 发现数量。
- 需求覆盖数量、开放阻塞项和共识结论。
- Agent 调用耗时与原生 CLI 返回的 Token。

离线汇总不会调用模型：

```bash
mutiagent eval
mutiagent eval --json
```

报告会按三种模式分组：

- `solo`：单 Agent。
- `review`：双 Agent 方案/代码审查。
- `consensus`：启用方案共识。

输出包括样本数、完成率、已评测运行的验收率、验证率、严重级别分布、平均耗时和平均 Token。样本较少时应把它视为趋势参考，而不是“Agent 越多质量必然越高”的证明。

## 权限与安全

### 原生 CLI 权限

- 主方案、需求分析和方案审查使用只读或 plan 模式。
- 实施和修订阶段只有主 Agent 使用工作区写入模式。
- Codex 以 `--ask-for-approval never` 运行；只读阶段使用 `read-only` 沙箱，写入阶段使用 `workspace-write`。
- Claude 只读阶段使用 `--permission-mode plan`，写入阶段使用 `--permission-mode acceptEdits`。
- 不使用 `dangerously-skip` 或其他绕过沙箱的参数。
- 两个 Agent 顺序执行，不会并发写同一文件。
- 提示词要求保留用户原有且与任务无关的改动，不执行 Git commit。

### 人工门禁

当 `plan_approval: true` 且启用了方案预审时，写入前会让用户选择：

- `e`：执行方案。
- `r`：提供反馈并要求修订方案。
- `c`：取消任务。

非交互环境无法回答门禁，因此必须使用 `--yes`，或在配置中明确设置 `plan_approval: false`。

### 密钥和数据

- MutiAgent 正式桥接入口不读取或保存 API Key。
- Claude Code 和 Codex CLI 使用自己的登录状态、环境变量、用户配置和项目指令。
- 不要将 Key 写入 `.mutiagent.json`、`bridge.json` 或运行记录。
- 早期实验用的 `agents.json` 已被 `.gitignore` 忽略；它不属于正式桥接配置，也不应提交。
- 运行记录虽然限制了文件权限，仍可能包含敏感代码上下文，应自行纳入备份和清理策略。

## 项目结构

```text
multiagent1/
├── bin/mutiagent                 本地轻量启动器
├── bridge.example.json           正式桥接配置示例
├── pyproject.toml                包信息与 mutiagent/multiagent 命令入口
├── multiagent_cli/
│   ├── cli.py                    参数、交互终端、doctor、任务管理
│   ├── bridge_config.py          正式桥接配置发现与校验
│   ├── bridge_models.py          桥接数据结构与默认身份
│   ├── adapters.py               Claude/Codex 命令构造与 JSON 事件解析
│   ├── bridge_orchestrator.py    方案、共识、实施、验证、审查状态机
│   ├── consensus.py              证据化共识协议解析
│   ├── collaboration.py          共享任务板、消息、需求与争议台账
│   ├── reviews.py                结构化代码审查协议
│   ├── verification.py           确定性验证命令执行
│   ├── workspace_state.py        Git/非 Git 快照与内容指纹
│   ├── checkpoints.py            阶段检查点序列化与恢复校验
│   ├── run_store.py              运行记录持久化
│   ├── worktrees.py              Git worktree 创建、diff 与清理
│   ├── quality.py                历史质量统计
│   ├── renderer.py               TUI、卡片、表格和可读输出
│   ├── client.py                 早期直连 API 实验客户端
│   ├── config.py                 早期直连 API 配置解析
│   ├── models.py                 早期直连 API 数据结构
│   └── orchestrator.py           早期直连 API 串行编排
└── tests/                        无模型调用的单元测试
```

### 原生 CLI 协议

桥接器使用机器可读模式调用：

```text
Claude Code: claude -p --output-format stream-json --verbose
Codex CLI:   codex ... exec --json
```

Claude 的 `session_id` 用于 `--resume`，Codex 的 `thread_id` 用于 `exec resume`。桥接器解析 JSON/JSONL 事件，将最终回复、会话 ID、工具事件、耗时和 Token 归一化，再交给统一状态机和 Renderer。

它不会嵌套两套原生全屏 TUI，也不会依靠抓取 ANSI 文本来判断结果。

## 开发与测试

### 运行测试

测试使用假的 CLI 或固定事件，不会调用 Claude、Codex 或产生模型费用：

```bash
python3 -m unittest discover -s tests -v
```

快速语法检查：

```bash
python3 -m compileall -q multiagent_cli tests
```

### 本地运行源码

无需安装也可以从仓库根目录执行：

```bash
python3 -m multiagent_cli --help
python3 -m multiagent_cli --check
```

### 代码职责边界

新增功能时优先遵守：

- 原生 CLI 差异放在 `adapters.py`，不要泄漏到编排状态机。
- 协作阶段和恢复边界放在 `bridge_orchestrator.py` / `checkpoints.py`。
- 结构化协议解析与终端展示分离。
- 确定性验证由 Bridge 自己执行，不让 Agent 只用文字宣称“测试通过”。
- 任何新检查点都必须能序列化，并有工作区指纹保护。

## 限制与故障排查

### 当前限制

- 正式桥接只支持 Claude Code 与 Codex CLI 两个 Agent，不支持配置任意 Agent 数量。
- 工作流按阶段顺序执行，不支持两个 Agent 同时写一个工作区。
- worktree 结果不会自动提交、挑选或合并。
- 独立验证只在配置 `verification.commands` 后运行；Bridge 不会猜测项目测试命令。
- Token 统计依赖原生 CLI 的 JSON 事件；CLI 未返回 usage 时不会估算费用。
- 恢复只能从完整成功阶段继续，不能安全续接半完成的写入回合。
- Git 基线会提供给 Reviewer，但不会自动从最终 diff 中减去任务开始前的补丁；复杂脏工作区建议先清理或启用 worktree。
- 固定 TUI 面向常见现代终端；日志采集和 CI 建议使用 `--no-tui --plain`。

### 找不到 Claude 或 Codex CLI

```bash
mutiagent --check
which claude
which codex
```

如果命令不在 `PATH` 或常见位置，在配置中填写绝对路径：

```json
{
  "claude": {"command": "/absolute/path/to/claude"},
  "codex": {"command": "/absolute/path/to/codex"}
}
```

### 认证或模型通道失败

```bash
mutiagent doctor
mutiagent doctor --probe-models
```

`503 No available channel for model ...` 通常表示原生 CLI 所使用的网关当前没有该模型通道，不是 Bridge JSON 结构错误。先把对应 `model` 设为 `null` 验证原生默认模型，或改成服务商实际支持的模型名。

`404 Route Not Found` 如果来自早期直连 API 实验，通常是 Base URL 与接口协议不匹配。正式桥接不自行拼接模型 API 路由；应直接确认 `claude` 和 `codex` 命令能否单独工作。

### 非交互运行提示无法确认方案

增加：

```bash
mutiagent --yes "完成任务"
```

或在可信自动化环境的配置中设置：

```json
{"plan_approval": false}
```

### Worktree 创建失败

检查：

```bash
git rev-parse --show-toplevel
git log -1 --oneline
git status --short
```

仓库必须已有提交且状态干净。源工作区的未提交改动不会自动带入隔离任务。

### Resume 拒绝工作区指纹

这表示检查点之后文件、分支、HEAD、diff 或未跟踪文件发生变化。先查看：

```bash
mutiagent task <run-id>
mutiagent task path <run-id>
mutiagent task diff <run-id>
```

确认真实状态后重新发起任务，或手工完成/撤销残留修改。目前不提供绕过指纹的强制恢复，以避免在未知状态上继续写入。

### 没有执行测试

默认 `verification.commands` 为空。运行：

```bash
mutiagent init
```

然后在项目 `.mutiagent.json` 中加入准确的测试、lint、类型检查或构建命令。验证结果才会被保存并作为副 Agent 验收证据。

### 输出不适合日志或终端显示异常

```bash
mutiagent --no-tui --plain "完成任务"
```

排查原生事件时再增加 `--show-details`。正常使用建议保持详情折叠，避免命令与中间事件淹没最终结论。
