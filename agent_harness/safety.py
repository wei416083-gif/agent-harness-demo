from __future__ import annotations

from pathlib import Path


MAX_OUTPUT_CHARS = 4000


class SafetyError(ValueError):
    # 安全检查失败时抛这个异常，ToolRegistry.call 会把它包装成失败 ToolResult。
    pass


def resolve_workspace_path(workspace_root: Path, user_path: str) -> Path:
    # 把用户传入路径解析成绝对路径，并确保它仍然位于 workspace 内。
    root = workspace_root.resolve()
    candidate = Path(user_path)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()

    try:
        # relative_to 成功说明 resolved 没有逃出 workspace。
        resolved.relative_to(root)
    except ValueError as exc:
        raise SafetyError(f"path escapes workspace: {user_path}") from exc

    return resolved


def truncate_output(value: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    # 工具输出统一截断，避免一次命令产生超大 stdout/stderr。
    if len(value) <= limit:
        return value
    return value[:limit] + f"\n...[truncated to {limit} chars]"


def assert_command_is_safe(command: str) -> None:
    # 这是最小危险命令拦截，不是完整 shell 沙箱。
    lowered = " ".join(command.lower().split())
    blocked_fragments = [
        "rm -rf",
        "shutdown",
        "reboot",
        "mkfs",
        "git reset --hard",
    ]
    for fragment in blocked_fragments:
        if fragment in lowered:
            raise SafetyError(f"blocked dangerous command: {fragment}")
