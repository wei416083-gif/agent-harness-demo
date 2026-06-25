# Agent Harness Demo

这是一个用于演示 Agent Harness 基础执行链路的 Python CLI 项目。项目重点展示 Agent 执行任务时常见的 harness 能力：状态机、Tool Registry、参数校验、工具调用日志、失败重试、重复失败检测，以及最小沙箱执行思路。

项目暂未接入真实大模型 API，当前使用规则流程模拟 Agent 决策，便于本地稳定复现。

## 快速运行

1. 安装依赖：

```bash
pip install -r requirements.txt
```

2. 重置 Demo 到初始 bug 状态：

```bash
python main.py --reset-demo
```

3. 运行 Agent：

```bash
python main.py --task "fix failing test"
```

成功时终端会输出：

```text
final_state: DONE
```

运行日志写入：

```text
logs/trace.jsonl
```

## 演示流程

初始文件 `workspace/buggy_code.py` 中故意写错：

```python
def add(a, b):
    return a - b
```

测试文件 `workspace/test_buggy_code.py` 断言：

```python
assert add(1, 2) == 3
```

运行 `python main.py --task "fix failing test"` 后，当前规则流程会执行：

1. 进入 `RECEIVED`、`CONTEXT_LOADING`、`PLANNING`
2. 选择并调用 `read_file` 读取 `buggy_code.py`
3. 选择并调用 `run_tests` 运行 pytest
4. 观察失败结果并生成 `error_fingerprint`
5. 进入 `RETRYING`
6. 选择并调用 `write_file`，把 `return a - b` 改为 `return a + b`
7. 再次运行 `run_tests`
8. 测试通过，进入 `DONE`

如需重新演示，执行：

```bash
python main.py --reset-demo
```

## 项目结构

```text
main.py
requirements.txt
README.md
agent_harness/
  __init__.py
  agent.py        # 脚本化 Agent、状态转移、重试和重复失败检测
  logging.py      # JSONL trace logger、敏感字段脱敏
  safety.py       # workspace 路径限制、危险命令拦截、输出截断
  states.py       # AgentState 枚举
  tools.py        # ToolSpec、ToolRegistry、工具实现、参数校验
workspace/
  buggy_code.py
  test_buggy_code.py
logs/
  trace.jsonl     # 运行后自动生成，不需要提交
```

## 状态机说明

状态定义在 `agent_harness/states.py`：

- `RECEIVED`
- `CONTEXT_LOADING`
- `PLANNING`
- `TOOL_SELECTING`
- `TOOL_RUNNING`
- `OBSERVING`
- `TESTING`
- `RETRYING`
- `NEED_CONFIRMATION`
- `DONE`
- `FAILED`

`DemoAgent` 在 `agent_harness/agent.py` 中实现状态转移。每次状态变化都会写入 `logs/trace.jsonl`。`max_steps=20` 用于避免无限循环。

## Tool Registry 说明

`agent_harness/tools.py` 提供：

- `ToolSpec`
- `Tool`
- `ToolResult`
- `ToolRegistry`

每个工具都有：

- `name`
- `description`
- `input_schema`
- `permission_level`
- `timeout_seconds`
- `require_approval`

调用工具前会根据 `input_schema` 做参数校验，包括必填字段、未知字段、基础类型和 timeout 上限。

已注册工具：

- `read_file`：读取 workspace 内文件
- `write_file`：写入 workspace 内文件
- `run_tests`：在 workspace 中执行 `python -m pytest -q`
- `bash`：执行受限 shell 命令，默认需要 approval

## 日志说明

每次状态变化和工具调用都会写入 `logs/trace.jsonl`。每行是一个 JSON 对象，字段包括：

- `run_id`
- `step_id`
- `state`
- `tool_name`
- `args`
- `success`
- `error`
- `duration_ms`
- `timestamp`

`args` 中 key 包含 `token`、`password`、`secret` 时会被脱敏为 `***REDACTED***`。

## 重复失败检测说明

当 `run_tests` 失败时，Agent 会根据 `stdout`、`stderr` 和错误信息生成 `error_fingerprint`：

- 对输出做简单归一化
- 使用 SHA-256
- 取前 16 位作为 fingerprint

如果同一个 `error_fingerprint` 连续出现 2 次，Agent 会进入 `FAILED` 或 `NEED_CONFIRMATION`，避免无限重复执行同一个失败路径。

## 沙箱与安全限制说明

这是最小 Demo，不是完整生产沙箱。它展示以下基础安全边界：

- 所有文件路径都通过 `resolve_workspace_path` 限制在 `workspace/` 目录内
- 文件工具不能读写 workspace 外部路径
- `bash` 工具会拦截危险命令片段：
  - `rm -rf`
  - `shutdown`
  - `reboot`
  - `mkfs`
  - `git reset --hard`
- 命令执行带 timeout
- `stdout`、`stderr`、`error` 最多保留 4000 字符
- `bash` 工具默认 `require_approval=True`

当前实现侧重演示核心链路，生产环境还需要补充容器隔离、资源限制、网络策略和更完整的权限模型。

## 设计点说明

1. Agent 执行任务如何建模  
   `DemoAgent` 使用显式状态机记录从接收任务、加载上下文、规划、选择工具、执行工具、观察结果、测试、重试到完成或失败的全过程。

2. 工具如何注册、选择和调用  
   `ToolRegistry` 负责注册工具和按名称查找工具；`ToolSpec` 描述工具元数据；调用前做参数校验；调用结果统一为 `ToolResult`。

3. 如何处理失败、重试和停止条件  
   `run_tests` 失败后生成 `error_fingerprint`；同一 fingerprint 连续出现 2 次即停止；同时设置 `max_steps=20` 防止无限循环。

4. 如何体现安全边界和可观测性  
   文件访问限制在 `workspace/`；受限 `bash` 拦截危险命令并带 timeout；输出截断；所有状态变化和工具调用写入 `logs/trace.jsonl`，并对敏感字段脱敏。