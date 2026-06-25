from __future__ import annotations

from enum import Enum


class AgentState(str, Enum):
    # Agent 收到用户任务，还没有开始加载上下文。
    RECEIVED = "RECEIVED"
    # 预留的上下文加载阶段，本 Demo 中用于展示状态机链路。
    CONTEXT_LOADING = "CONTEXT_LOADING"
    # 规划阶段。本 Demo 不调用 LLM，而是用固定规则模拟计划。
    PLANNING = "PLANNING"
    # 即将选择某个工具，日志里会记录准备使用的工具名。
    TOOL_SELECTING = "TOOL_SELECTING"
    # 工具实际执行中，read_file/write_file/run_tests/bash 都会走这个状态日志。
    TOOL_RUNNING = "TOOL_RUNNING"
    # Agent 观察工具结果，用于决定下一步。
    OBSERVING = "OBSERVING"
    # 修复后重新测试的阶段。
    TESTING = "TESTING"
    # 测试失败后准备重试或修复的阶段。
    RETRYING = "RETRYING"
    # Agent 无法安全自动推进，需要人工确认。
    NEED_CONFIRMATION = "NEED_CONFIRMATION"
    # 任务成功完成。
    DONE = "DONE"
    # 任务失败结束。
    FAILED = "FAILED"
