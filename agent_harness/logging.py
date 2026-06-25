from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SENSITIVE_KEY_PARTS = ("token", "password", "secret")


def _redact(value: Any) -> Any:
    # 日志脱敏：递归处理 dict/list，避免 token/password/secret 原样落盘。
    if isinstance(value, dict):
        redacted = {}
        for key, inner in value.items():
            if any(part in key.lower() for part in SENSITIVE_KEY_PARTS):
                redacted[key] = "***REDACTED***"
            else:
                redacted[key] = _redact(inner)
        return redacted
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


class TraceLogger:
    # TraceLogger 负责把状态变化和工具调用写入 logs/trace.jsonl。
    def __init__(self, path: Path) -> None:
        self.path = path
        # 日志目录不存在时自动创建，Agent 运行时不需要提前手动建 logs/。
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def log(
        self,
        *,
        run_id: str,
        step_id: int,
        state: str,
        tool_name: str | None = None,
        args: dict[str, Any] | None = None,
        success: bool | None = None,
        error: str | None = None,
        duration_ms: int | None = None,
    ) -> None:
        # 每条 trace 是一行 JSON，方便后续按 run_id/step_id 做回放分析。
        record = {
            "run_id": run_id,
            "step_id": step_id,
            "state": state,
            "tool_name": tool_name,
            "args": _redact(args or {}),
            "success": success,
            "error": error,
            "duration_ms": duration_ms,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        # 追加写入 JSONL：状态机每推进一步或工具每调用一次都会多一行。
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
