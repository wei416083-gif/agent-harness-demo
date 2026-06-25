from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from agent_harness.safety import (
    SafetyError,
    assert_command_is_safe,
    resolve_workspace_path,
    truncate_output,
)


InputSchema = dict[str, dict[str, Any]]
ToolHandler = Callable[[dict[str, Any]], "ToolResult"]


@dataclass(frozen=True)
class ToolSpec:
    # 工具名，Agent 通过这个名字在 ToolRegistry 中查找工具。
    name: str
    # 给人看的工具说明，当前 Demo 不做自动选择评分。
    description: str
    # 最小参数 schema，用于调用前校验必填字段和基础类型。
    input_schema: InputSchema
    # 权限级别目前是元信息，没有在源码里做真实权限分层控制。
    permission_level: str
    # timeout 上限会参与参数校验；run_tests/bash 还会传给 subprocess.run。
    timeout_seconds: int
    # 是否需要调用方显式 approved=True；当前主要用于 bash。
    require_approval: bool


@dataclass
class ToolResult:
    # 工具是否成功。Agent 后续主要根据它决定进入 DONE、重试或失败。
    success: bool
    # 标准输出，成功或失败时都可能有内容。
    stdout: str = ""
    # 标准错误，通常用于测试失败或命令失败排查。
    stderr: str = ""
    # 统一错误信息。参数校验、权限、安全检查、命令失败都会落到这里。
    error: str | None = None
    # 工具执行耗时毫秒数，由 ToolRegistry.call 统一计算。
    duration_ms: int = 0


class Tool:
    # Tool 把“工具说明书”和“实际执行函数”绑定在一起。
    def __init__(self, spec: ToolSpec, handler: ToolHandler) -> None:
        self.spec = spec
        self.handler = handler


class ToolRegistry:
    # ToolRegistry 是工具注册中心：Agent 只按名字调用，不直接依赖具体函数。
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        # 注册时用 ToolSpec.name 作为 key，防止同名工具覆盖。
        if tool.spec.name in self._tools:
            raise ValueError(f"tool already registered: {tool.spec.name}")
        self._tools[tool.spec.name] = tool

    def find(self, name: str) -> Tool:
        # 按工具名查找具体 Tool；找不到会抛错，随后由调用链处理。
        try:
            return self._tools[name]
        except KeyError as exc:
            raise KeyError(f"unknown tool: {name}") from exc

    def call(self, name: str, args: dict[str, Any]) -> ToolResult:
        # 工具调用总入口：查找工具 -> 参数校验 -> 权限确认 -> 执行函数 -> 统一包装结果。
        tool = self.find(name)
        started = time.perf_counter()
        try:
            self._validate_args(tool.spec, args)
            # require_approval 目前只检查调用参数里是否有 approved=True。
            if tool.spec.require_approval and not args.get("approved", False):
                raise PermissionError(f"tool requires approval: {name}")
            result = tool.handler(args)
        except Exception as exc:
            # 参数校验、安全检查、工具内部异常都会变成失败的 ToolResult，避免 Agent 崩溃。
            return ToolResult(
                success=False,
                error=str(exc),
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
        # 这里重新计算总耗时，覆盖工具内部可能设置的 duration_ms。
        result.duration_ms = int((time.perf_counter() - started) * 1000)
        # stdout/stderr/error 统一截断，避免日志或终端输出过大。
        result.stdout = truncate_output(result.stdout)
        result.stderr = truncate_output(result.stderr)
        if result.error:
            result.error = truncate_output(result.error)
        return result

    @staticmethod
    def _validate_args(spec: ToolSpec, args: dict[str, Any]) -> None:
        # 最小 schema 校验：未知参数、缺少必填参数、基础类型错误都会失败。
        allowed = set(spec.input_schema)
        extra = set(args) - allowed
        if extra:
            raise ValueError(f"{spec.name}: unknown args: {sorted(extra)}")

        for field, rules in spec.input_schema.items():
            required = rules.get("required", False)
            expected = rules.get("type")
            if required and field not in args:
                raise ValueError(f"{spec.name}: missing required arg: {field}")
            if field not in args:
                continue
            if expected == "string" and not isinstance(args[field], str):
                raise TypeError(f"{spec.name}: {field} must be a string")
            if expected == "boolean" and not isinstance(args[field], bool):
                raise TypeError(f"{spec.name}: {field} must be a boolean")
            if expected == "integer" and not isinstance(args[field], int):
                raise TypeError(f"{spec.name}: {field} must be an integer")
            if field == "timeout_seconds":
                # timeout_seconds 不能超过 ToolSpec 中声明的上限。
                if args[field] <= 0:
                    raise ValueError(f"{spec.name}: timeout_seconds must be positive")
                if args[field] > spec.timeout_seconds:
                    raise ValueError(
                        f"{spec.name}: timeout_seconds exceeds limit {spec.timeout_seconds}"
                    )


def create_default_registry(workspace_root: Path) -> ToolRegistry:
    # main.py 调用这个函数创建默认工具集，所有工具都绑定到同一个 workspace。
    workspace_root.mkdir(parents=True, exist_ok=True)
    registry = ToolRegistry()

    def read_file(args: dict[str, Any]) -> ToolResult:
        # 文件读取前先解析 workspace 内路径，防止读取项目外文件。
        path = resolve_workspace_path(workspace_root, args["path"])
        return ToolResult(success=True, stdout=path.read_text(encoding="utf-8"))

    def write_file(args: dict[str, Any]) -> ToolResult:
        # 文件写入同样必须限制在 workspace 内。
        path = resolve_workspace_path(workspace_root, args["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(args["content"], encoding="utf-8")
        # 成功时只返回简短摘要，真实文件内容不重复写到 stdout。
        return ToolResult(success=True, stdout=f"wrote {path.name}")

    def run_tests(args: dict[str, Any]) -> ToolResult:
        # run_tests 使用固定命令，避免把任意 shell 字符串交给测试工具。
        timeout = int(args.get("timeout_seconds", 10))
        started = time.perf_counter()
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "-q"],
            cwd=workspace_root,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        # pytest 返回码为 0 表示测试通过，否则封装为失败 ToolResult。
        return ToolResult(
            success=completed.returncode == 0,
            stdout=completed.stdout,
            stderr=completed.stderr,
            error=None if completed.returncode == 0 else "pytest failed",
            duration_ms=int((time.perf_counter() - started) * 1000),
        )

    def bash(args: dict[str, Any]) -> ToolResult:
        command = args["command"]
        # bash 是可选受限工具：执行前先做危险命令片段拦截。
        assert_command_is_safe(command)
        timeout = int(args.get("timeout_seconds", 5))
        # 这里的 cwd 限制在 workspace，但这不是完整沙箱；只是最小执行边界示例。
        completed = subprocess.run(
            command,
            cwd=workspace_root,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=True,
            check=False,
        )
        return ToolResult(
            success=completed.returncode == 0,
            stdout=completed.stdout,
            stderr=completed.stderr,
            error=None if completed.returncode == 0 else f"exit code {completed.returncode}",
        )

    # 以下注册表把工具名映射到具体函数，Agent 后续只通过工具名调用。
    registry.register(
        Tool(
            ToolSpec(
                name="read_file",
                description="Read a UTF-8 file inside the demo workspace.",
                input_schema={"path": {"type": "string", "required": True}},
                permission_level="read",
                timeout_seconds=2,
                require_approval=False,
            ),
            read_file,
        )
    )
    registry.register(
        Tool(
            ToolSpec(
                name="write_file",
                description="Write a UTF-8 file inside the demo workspace.",
                input_schema={
                    "path": {"type": "string", "required": True},
                    "content": {"type": "string", "required": True},
                },
                permission_level="write",
                timeout_seconds=2,
                require_approval=False,
            ),
            write_file,
        )
    )
    registry.register(
        Tool(
            ToolSpec(
                name="run_tests",
                description="Run pytest in the demo workspace.",
                input_schema={"timeout_seconds": {"type": "integer", "required": False}},
                permission_level="execute",
                timeout_seconds=10,
                require_approval=False,
            ),
            run_tests,
        )
    )
    registry.register(
        Tool(
            ToolSpec(
                name="bash",
                description="Run a restricted shell command in the demo workspace.",
                input_schema={
                    "command": {"type": "string", "required": True},
                    "timeout_seconds": {"type": "integer", "required": False},
                    "approved": {"type": "boolean", "required": False},
                },
                permission_level="execute",
                timeout_seconds=5,
                require_approval=True,
            ),
            bash,
        )
    )

    return registry
