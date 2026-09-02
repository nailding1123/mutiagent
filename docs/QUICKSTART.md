# MultiAgent 快速上手

MultiAgent 是一个本地 Web 工作台，把 Claude Code 和 Codex CLI 放进同一个群聊中。你可以让双方共享上下文、分别回答问题，也可以让双方并行实现同一个功能，再选择最终方案。

## 1. 安装并启动

使用前请准备：

- Python 3.9 或更高版本；
- Git（A/B 并行开发需要）；
- 已安装并登录 Claude Code 和 Codex CLI。

macOS / Linux：

```bash
./install.sh
multiagent -C /path/to/your-project
```

Windows PowerShell：

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
multiagent -C C:\path\to\your-project
```

启动后会打开本地 Web 工作台。关闭最后一个 Web 页面会自动停止本地服务；刷新页面会保留短暂的重连时间。

## 2. 创建群聊并发送第一条消息

1. 点击“新建任务”。
2. 直接建立空群聊。
3. 在底部群聊输入框输入消息。
4. 输入 `@` 选择 Claude Code、Codex 或全部成员，然后点击“发送消息”。

建立群聊时不需要预先填写文档或提示词；需要时可以在消息输入框中粘贴图片、拖放文件或点击“附件”。

## 3. 选择谁来回答

| 写法 | 作用 |
| --- | --- |
| `@Claude 分析这个报错` | 只让 Claude Code 回答 |
| `@Codex 检查测试覆盖` | 只让 Codex 回答 |
| `@all 讨论两个方案的差异` | 让双方同时回答 |
| 不写 `@` | 使用设置中的默认响应者 |

用户、Claude Code 和 Codex 的消息都会进入共同上下文。后续 Agent 可以直接引用前面的讨论，不需要重复粘贴背景。

## 4. 讨论、单 Agent 执行与 A/B 执行

### 普通讨论

适合分析、解释、评审和方案比较：

```text
@Claude 解释这个错误，先不要修改代码
@Codex 检查当前实现并运行测试
@all 讨论这两个方案的优缺点
```

### 单 Agent 执行

明确使用“执行”或 `/exec`：

```text
@Claude 执行：修复这个问题并验证
@Codex /exec 修改页面布局
```

文件是否需要修改由原生 Agent 自己判断。完成后消息气泡会显示工作区、修改文件、增删行数和 Diff。

### A/B 双 Agent 并行执行

当你希望 Claude Code 和 Codex 分别实现同一个任务时，使用：

```text
@all 执行：分别实现这个功能
```

系统会：

1. 从同一个 Git 快照创建两个隔离 Worktree；
2. 让 Claude Code 和 Codex 并行执行；
3. 分别显示两个 Agent 的回复和 Diff；
4. 允许先预览方案 A 或 B；
5. 你点击“采用此方案”后，才把选中的修改写入主工作区。

预览是临时的，不等于正式采用。应用前如果主工作区发生变化，系统会停止应用并保留恢复补丁。

## 5. 权限审批

当原生 Agent 请求执行命令、修改文件、访问工作区外资源或补充信息时，Web 页面会弹出审批窗口。

可用操作通常包括：

- 允许一次；
- 本次会话允许（如果原生 Agent 支持）；
- 拒绝并继续思考其他办法；
- 拒绝并停止当前回复。

审批只会暂停发起请求的 Agent，群聊中的另一个 Agent 可以继续工作。

## 6. 查看和管理回复

每条 Agent 回复底部都有操作按钮：

- “上下文”：控制这条回复是否加入后续共同上下文；
- “复制”：复制消息原文；
- “引用”：把回复带回输入框；
- “重试”：删除旧回复并重新生成；
- “继续”：从当前回复继续生成。

用户消息可以编辑后重新发送。按 `Esc` 可以撤回最近一条已发送的用户消息，撤回后消息会回到输入框；撤回时只停止正在回复该消息的 Agent。

如果 Agent 修改了代码，回复中的变更卡片可以查看 Diff。确认不需要这些改动时，可以使用“回撤改动”；系统会在工作区仍匹配安全基线时执行回撤，避免覆盖用户的新修改。

## 7. 设置建议

打开“设置”可以调整：

- 默认响应者：Claude Code、Codex 或双方；
- Git 写入隔离：默认开启；关闭后并发写入会显示“等待工作区租约”；
- 自动压缩共同上下文；
- Claude Code 权限模式，包括原生 `Auto mode`；
- Claude/Codex 模型顺序、超时和备用模型；
- Codex GPT 模型思考强度；
- 主题、流式回复和浏览器通知。

设置保存后会立即影响当前会话的可变策略和等待中的工作区租约；已经启动的原生进程不会被强行迁移，下一条消息会使用新设置。

## 8. 常见状态说明

### 等待工作区租约

通常表示关闭了 Git 写入隔离，另一个 Agent 正占用主工作区。当前 Agent 会等待租约释放；打开设置中的“Git 写入隔离”可以让后续并发写入使用临时 Worktree。

### A/B 候选待选择

两个候选已完成，主工作区尚未正式采用任何方案。先预览 A、B，再点击“采用此方案”或“放弃全部方案”。

### 应用冲突

主工作区在对比期间发生了变化。系统不会强制覆盖，可以先查看变化、让 Agent 评估冲突，或让 Agent 基于当前主工作区重新实现。

### 任务已中断

本轮 Agent 已停止。A/B 候选会同步标记为中断，临时预览和 Worktree 会安全清理，输入框恢复可用。

## 9. 界面示例

### 群聊协作

![群聊协作](images/group-chat-current.png)

### A/B 执行中

![A/B 执行中](images/comparison-running-current.png)

### A/B 结果预览与采用

![A/B 结果预览与采用](images/comparison-review-current.png)

### Agent 设置

![Agent 设置](images/agent-settings-current.png)

### 单 Agent 变更审查

![变更审查](images/change-review-current.png)

