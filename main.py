from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from agent_harness.agent import DemoAgent
from agent_harness.logging import TraceLogger
from agent_harness.tools import create_default_registry


# reset-demo 使用的标准初始代码：这里故意保留 bug，方便每次重新演示“失败 -> 修复 -> 通过”。
# 这个函数故意写错，用来制造 Demo 的初始失败场景。
# Agent 后续会把 a - b 修复成 a + b。
BUGGY_CODE = """# 这个函数故意写错，用来制造 Demo 的初始失败场景。
# Agent 后续会把 a - b 修复成 a + b。
def add(a, b):
    return a - b
"""

# reset-demo 使用的标准测试代码：这个断言会暴露 add 函数里的减法 bug。
TEST_CODE = """from buggy_code import add


def test_add():
    # 初始代码里 add(1, 2) 会返回 -1，所以第一次 pytest 会失败。
    # Agent 修复为加法后，这个测试会通过。
    assert add(1, 2) == 3
"""


def parse_args() -> argparse.Namespace:
    # 入口层只接受两种模式：执行任务，或重置 Demo。二者互斥，避免一次命令做两件事。
    parser = argparse.ArgumentParser(description="Minimal Agent Harness demo")
    group = parser.add_mutually_exclusive_group(required=True)
    # --task 是正常演示入口，会创建 Agent 并进入状态机。
    group.add_argument("--task", help="Task for the demo agent")
    # --reset-demo 只负责恢复演示环境，不会启动 Agent 状态机。
    group.add_argument(
        "--reset-demo",
        action="store_true",
        help="Reset workspace demo files and remove generated logs/caches",
    )
    return parser.parse_args()


def reset_demo(project_root: Path) -> None:
    # workspace 是 Agent 工具能读写的演示工作区。
    workspace_root = project_root / "workspace"
    workspace_root.mkdir(parents=True, exist_ok=True)
    # 恢复故意写错的业务代码和对应测试，让 Demo 每次都能从失败场景开始。
    (workspace_root / "buggy_code.py").write_text(BUGGY_CODE, encoding="utf-8")
    (workspace_root / "test_buggy_code.py").write_text(TEST_CODE, encoding="utf-8")

    # 清理上次运行留下的 trace 日志，避免新旧演示记录混在一起。
    trace_path = project_root / "logs" / "trace.jsonl"
    if trace_path.exists():
        trace_path.unlink()
    if trace_path.parent.exists() and not any(trace_path.parent.iterdir()):
        trace_path.parent.rmdir()

    cleanup_names = {"__pycache__", ".pytest_cache"}
    for base in (project_root / "agent_harness", workspace_root):
        if not base.exists():
            continue
        # 只清理 Python/pytest 运行缓存，不删除源码文件。
        for path in base.rglob("*"):
            if path.is_dir() and path.name in cleanup_names:
                shutil.rmtree(path)


def main() -> int:
    args = parse_args()
    # 所有路径都从 main.py 所在目录推导，保证从任意工作目录启动时仍能找到项目文件。
    project_root = Path(__file__).resolve().parent
    workspace_root = project_root / "workspace"
    log_path = project_root / "logs" / "trace.jsonl"

    # 重置模式直接返回，不创建 logger/registry/agent，也不会写 trace。
    if args.reset_demo:
        reset_demo(project_root)
        print("Demo reset complete.")
        print(f"workspace: {workspace_root}")
        print("buggy_code.py restored to bug version.")
        return 0

    # 正常任务模式：先准备可观测性，再准备工具注册表，最后创建 Agent。
    logger = TraceLogger(log_path)
    registry = create_default_registry(workspace_root)
    agent = DemoAgent(
        task=args.task,
        workspace_root=workspace_root,
        registry=registry,
        logger=logger,
    )

    # Agent Harness 的主流程入口：状态机、工具调用、失败检测都从这里开始。
    result = agent.run()
    print(f"run_id: {result.run_id}")
    print(f"task: {args.task}")
    print(f"final_state: {result.final_state}")
    print(f"steps: {result.steps}")
    print(f"log: {log_path}")
    if result.error:
        print(f"error: {result.error}")
    # CLI 的退出码跟最终状态绑定，方便脚本或 CI 判断 Demo 是否成功。
    return 0 if result.final_state == "DONE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
