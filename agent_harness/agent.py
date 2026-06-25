from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_harness.logging import TraceLogger
from agent_harness.states import AgentState
from agent_harness.tools import ToolRegistry, ToolResult


@dataclass
class AgentRunResult:
    # main.py 最终打印这个结果，作为一次 Agent 运行的摘要。
    run_id: str
    final_state: str
    steps: int
    error: str | None = None


class StepLimitExceeded(RuntimeError):
    # max_steps 防止状态机因为 bug 或重复失败陷入无限循环。
    pass


class DemoAgent:
    # 这是 Demo 的核心 Agent。注意：这里没有真实 LLM，决策流程是为了稳定演示而写死的规则。
    def __init__(
        self,
        *,
        task: str,
        workspace_root: Path,
        registry: ToolRegistry,
        logger: TraceLogger,
        max_steps: int = 20,
    ) -> None:
        self.task = task
        self.workspace_root = workspace_root
        # Agent 不直接调用工具函数，而是通过 ToolRegistry 做查找、校验和执行。
        self.registry = registry
        # 状态变化和工具调用都会通过 logger 写入 logs/trace.jsonl。
        self.logger = logger
        self.max_steps = max_steps
        self.run_id = uuid.uuid4().hex[:12]
        self.step_id = 0
        self.final_state = AgentState.RECEIVED
        # 用于重复失败检测：记录上一次测试失败指纹和连续次数。
        self.last_fingerprint: str | None = None
        self.repeat_count = 0

    def run(self) -> AgentRunResult:
        # 主流程就是一条固定剧本：读文件 -> 跑测试 -> 规则修复 -> 再跑测试。
        try:
            # 1. 接收任务、加载上下文、规划。这里是状态展示，不调用外部模型。
            self._transition(AgentState.RECEIVED, args={"task": self.task})
            self._transition(AgentState.CONTEXT_LOADING)
            self._transition(AgentState.PLANNING)

            # 2. 选择 read_file，读取待修复代码。
            self._transition(AgentState.TOOL_SELECTING, args={"tool": "read_file"})
            read_result = self._call_tool("read_file", {"path": "buggy_code.py"})
            if not read_result.success:
                # 连文件都读不到，后续无法继续，直接 FAILED。
                return self._finish(AgentState.FAILED, read_result.error)

            self._transition(AgentState.OBSERVING)
            # 3. 第一次运行测试，暴露当前 bug。
            test_result = self._run_tests()
            if test_result.success:
                # 如果初始代码已经通过测试，就不需要修复，直接 DONE。
                return self._finish(AgentState.DONE)

            if self._is_repeated_failure(test_result):
                # 同一个失败连续出现，说明盲目重试价值不大，转人工确认。
                return self._finish(AgentState.NEED_CONFIRMATION, "same test failure repeated")

            # 4. 测试失败后进入重试/修复阶段。
            self._transition(AgentState.RETRYING)
            fixed = self._fix_add_function(read_result.stdout)
            if fixed is None:
                # 规则没有识别出安全修复点，不做危险猜测，交给人工确认。
                return self._finish(AgentState.NEED_CONFIRMATION, "cannot identify safe edit")

            # 5. 选择 write_file，把规则修复后的内容写回 workspace。
            self._transition(AgentState.TOOL_SELECTING, args={"tool": "write_file"})
            write_result = self._call_tool(
                "write_file",
                {"path": "buggy_code.py", "content": fixed},
            )
            if not write_result.success:
                # 写文件失败通常表示路径、安全或文件系统问题，直接 FAILED。
                return self._finish(AgentState.FAILED, write_result.error)

            # 6. 修复后重新运行测试。
            self._transition(AgentState.TESTING)
            test_result = self._call_tool("run_tests", {"timeout_seconds": 10})
            self._transition(
                AgentState.OBSERVING,
                success=test_result.success,
                error=test_result.error,
            )
            if test_result.success:
                # 修复后的测试通过，任务完成。
                return self._finish(AgentState.DONE)

            if self._is_repeated_failure(test_result):
                # 修复后仍然出现相同失败，说明规则修复无效，停止自动尝试。
                return self._finish(AgentState.FAILED, "same test failure repeated twice")
            return self._finish(AgentState.FAILED, test_result.error or "tests failed")
        except StepLimitExceeded as exc:
            # 超过最大步数也要失败退出，避免状态机无限循环。
            return AgentRunResult(
                run_id=self.run_id,
                final_state=AgentState.FAILED.value,
                steps=self.step_id,
                error=str(exc),
            )

    def _run_tests(self) -> ToolResult:
        # run_tests 的包装流程：先记录工具选择，再调用工具，再把失败指纹写入观察日志。
        self._transition(AgentState.TOOL_SELECTING, args={"tool": "run_tests"})
        result = self._call_tool("run_tests", {"timeout_seconds": 10})
        fingerprint = self._fingerprint(result)
        self._transition(
            AgentState.OBSERVING,
            success=result.success,
            error=None if result.success else f"error_fingerprint={fingerprint}",
        )
        return result

    def _transition(
        self,
        state: AgentState,
        *,
        args: dict[str, Any] | None = None,
        success: bool | None = None,
        error: str | None = None,
    ) -> None:
        # 所有状态切换都先走步数保护，再写 trace 日志。
        self._next_step()
        self.final_state = state
        self.logger.log(
            run_id=self.run_id,
            step_id=self.step_id,
            state=state.value,
            args=args,
            success=success,
            error=error,
            duration_ms=0,
        )

    def _call_tool(self, name: str, args: dict[str, Any]) -> ToolResult:
        # Agent 调工具的唯一入口：真正的查找、参数校验和执行由 ToolRegistry 完成。
        self._next_step()
        result = self.registry.call(name, args)
        # 工具调用日志和状态切换日志分开记录，便于复盘“选了什么工具、结果如何”。
        self.logger.log(
            run_id=self.run_id,
            step_id=self.step_id,
            state=AgentState.TOOL_RUNNING.value,
            tool_name=name,
            args=args,
            success=result.success,
            error=result.error,
            duration_ms=result.duration_ms,
        )
        return result

    def _next_step(self) -> None:
        # step_id 是简单的防死循环计数器，每次状态切换或工具调用都会增加。
        if self.step_id >= self.max_steps:
            raise StepLimitExceeded(f"exceeded max steps: {self.max_steps}")
        self.step_id += 1

    def _finish(self, state: AgentState, error: str | None = None) -> AgentRunResult:
        # 统一收口最终状态，main.py 只需要看 AgentRunResult。
        self._transition(state, success=state == AgentState.DONE, error=error)
        return AgentRunResult(
            run_id=self.run_id,
            final_state=state.value,
            steps=self.step_id,
            error=error,
        )

    def _fix_add_function(self, content: str) -> str | None:
        # Demo 的“Agent 决策”是规则模拟：只识别 return a - b，并替换为 return a + b。
        # 这里不是 LLM 推理，目的是让演示稳定可复现。
        fixed = re.sub(
            r"return\s+a\s*-\s*b",
            "return a + b",
            content,
            count=1,
        )
        if fixed == content:
            return None
        return fixed

    def _fingerprint(self, result: ToolResult) -> str:
        # 失败指纹用于判断“是不是同一个错误一直重复出现”。
        combined = "\n".join([result.stdout, result.stderr, result.error or ""])
        # 去掉容易变化的地址和耗时，减少同类错误被误判为不同错误的概率。
        normalized = re.sub(r"0x[0-9a-fA-F]+", "0xADDR", combined)
        normalized = re.sub(r"\d+\.\d+s", "N.Ns", normalized)
        normalized = "\n".join(line.strip() for line in normalized.splitlines() if line.strip())
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]

    def _is_repeated_failure(self, result: ToolResult) -> bool:
        # 连续两次相同 fingerprint 就认为自动重试没有意义。
        fingerprint = self._fingerprint(result)
        if fingerprint == self.last_fingerprint:
            self.repeat_count += 1
        else:
            self.last_fingerprint = fingerprint
            self.repeat_count = 1
        return self.repeat_count >= 2
