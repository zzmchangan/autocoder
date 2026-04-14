"""
AutoCoder — 全自动编码 Agent
GLM 监督 + OpenCode 执行 + Git 安全网

用法:
    python autocoder.py "目标描述" --config config.yaml
    python autocoder.py "重构 LMath" --config config.yaml --yes
    python autocoder.py "修复 bug" --config config.yaml --max-retries 2
"""

import argparse
import io
import json
import logging
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from config import load_config
from supervisor import Supervisor, SupervisorError
from verifier import Verifier, VerifierError
from worker import Worker, WorkerError

# Windows 控制台 UTF-8 支持
if sys.platform == "win32":
    os.system("")  # 启用 ANSI 转义
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True)

# ── 日志配置 ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("autocoder")


def ts() -> str:
    """当前时间戳 HH:MM:SS"""
    return datetime.now().strftime("%H:%M:%S")


def log(msg: str):
    """实时输出，强制刷新"""
    print(f"[{ts()}] {msg}", flush=True)


class Spinner:
    """在后台线程显示旋转动画，表示正在等待"""

    def __init__(self, message: str):
        self.message = message
        self._stop = threading.Event()
        self._thread = None

    def __enter__(self):
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *args):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)
        # 清除当前行
        print(f"\r{'':80}\r", end="", flush=True)

    def _spin(self):
        chars = "|/-\\"
        idx = 0
        while not self._stop.is_set():
            elapsed = time.monotonic() - self._start if hasattr(self, '_start') else 0
            print(f"\r[{ts()}] {self.message} {chars[idx % 4]}", end="", flush=True)
            idx += 1
            self._stop.wait(0.5)

    def update(self, message: str):
        self.message = message


# ── 状态持久化 ────────────────────────────────────────────

def save_state(state_dir: Path, filename: str, data: dict) -> None:
    """保存中间状态到文件"""
    path = state_dir / filename
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_state(state_dir: Path, filename: str) -> dict | None:
    """加载中间状态"""
    path = state_dir / filename
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


# ── 主编排循环 ────────────────────────────────────────────

def run(goal: str, config_path: str | None, yes: bool, resume: bool) -> None:
    """主编排循环"""

    # 1. 加载配置
    cfg = load_config(config_path)
    project_path = cfg.project_path
    log("=" * 60)
    log("AutoCoder — 全自动编码 Agent")
    log("=" * 60)
    log(f"项目: {project_path}")
    log(f"监督者: {cfg.supervisor_model}")
    log(f"执行者: opencode serve ({cfg.worker_model})")
    log(f"目标: {goal}")
    log("")

    # 初始化组件
    verifier = Verifier(build_cmd=cfg.build_cmd, test_cmd=cfg.test_cmd)
    supervisor = Supervisor(
        api_key=cfg.supervisor_api_key,
        base_url=cfg.supervisor_base_url,
        model=cfg.supervisor_model,
    )
    worker = Worker(
        command=cfg.worker_command,
        host=cfg.worker_host,
        port=cfg.worker_port,
        model=cfg.worker_model,
        timeout=cfg.worker_timeout,
    )

    # 2. 检查并准备环境
    if not os.path.exists(project_path):
        log(f"项目目录不存在，自动创建: {project_path}")
        os.makedirs(project_path, exist_ok=True)

    if not verifier.is_git_repo(project_path):
        log(f"初始化 git 仓库: {project_path}")
        verifier.init_repo(project_path)
    else:
        log("git 仓库就绪")

    if not verifier.check_git_clean(project_path):
        log("WARNING: 工作区不干净，建议先 git stash 或 commit")
        if not yes:
            ans = input("继续? [y/N] ").strip().lower()
            if ans != "y":
                sys.exit(0)

    # 3. 分析项目
    log("[1/4] 分析项目结构...")
    with Spinner("GLM 正在分析项目结构..."):
        context = supervisor.analyze_project(project_path)
    save_state(cfg.state_dir, "project_context.json", {"context": context})
    log(f"  -> 项目上下文: {len(context)} 字符")

    # 4. 规划任务 (支持 resume)
    tasks = None
    if resume:
        state = load_state(cfg.state_dir, "plan.json")
        if state and "tasks" in state:
            tasks = state["tasks"]
            log(f"[2/4] 从上次恢复，共 {len(tasks)} 个任务")

    if tasks is None:
        log("[2/4] GLM 正在规划任务...")
        with Spinner("GLM 正在拆分任务..."):
            try:
                tasks = supervisor.plan_tasks(goal, context)
            except SupervisorError as e:
                log(f"ERROR: 规划失败: {e}")
                sys.exit(1)
        save_state(cfg.state_dir, "plan.json", {"goal": goal, "tasks": tasks})

    log(f"  -> 计划: {len(tasks)} 个子任务")
    for t in tasks:
        risk = t.get("risk", "low")
        risk_tag = {"low": "[low]", "medium": "[MED]", "high": "[HIGH]"}.get(risk, "[?]")
        log(f"     {t['id']}. {risk_tag} {t['task'][:70]}")

    # 5. 确认执行
    if not yes:
        ans = input("\n确认执行? [Y/n] ").strip().lower()
        if ans in ("n", "no"):
            log("已取消")
            sys.exit(0)

    # 6. 启动 Worker
    log("[3/4] 启动 OpenCode 服务...")
    with Spinner("等待 opencode serve 就绪..."):
        try:
            worker.start_server(project_path)
        except WorkerError as e:
            log(f"ERROR: {e}")
            sys.exit(1)
    log("  -> OpenCode 服务已启动")

    # 7. 执行任务循环
    results = []
    try:
        for i, task in enumerate(tasks):
            result = _execute_task(
                task=task,
                task_index=i,
                total=len(tasks),
                supervisor=supervisor,
                worker=worker,
                verifier=verifier,
                project_path=project_path,
                state_dir=cfg.state_dir,
                max_retries=cfg.max_retries,
            )
            results.append(result)
            save_state(cfg.state_dir, "results.json", results)

            if result["status"] == "failed" and cfg.stop_on_failure:
                log("!! 任务失败且 stop_on_failure=True，停止执行")
                break

    finally:
        # 8. 停止 Worker
        log("[4/4] 停止 OpenCode 服务...")
        worker.stop_server()

    # 9. 生成报告
    log("=" * 60)
    log("最终报告")
    log("=" * 60)

    passed = sum(1 for r in results if r["status"] == "passed")
    failed = len(results) - passed
    log(f"通过: {passed}/{len(results)}, 失败: {failed}")

    for r in results:
        icon = "OK" if r["status"] == "passed" else "FAIL"
        score = r.get("review", {}).get("score", "N/A")
        log(f"  [{icon}] 任务 {r['task']['id']}: {r['task']['task'][:60]} (score: {score}, attempts: {r.get('attempts', 1)})")

    # GLM 生成最终总结
    if cfg.supervisor_api_key:
        try:
            with Spinner("GLM 生成总结..."):
                summary = supervisor.summarize(results)
            log(f"--- AI 总结 ---\n{summary}")
        except SupervisorError:
            pass

    save_state(cfg.state_dir, "summary.json", {
        "goal": goal, "results": results,
        "passed": passed, "failed": failed,
    })

    sys.exit(1 if failed > 0 else 0)


def _execute_task(
    task: dict,
    task_index: int,
    total: int,
    supervisor: Supervisor,
    worker: Worker,
    verifier: Verifier,
    project_path: str,
    state_dir: Path,
    max_retries: int,
) -> dict:
    """执行单个任务（含重试逻辑）"""
    task_id = task["id"]
    task_desc = task["task"]
    current_task = task_desc
    attempts = 0

    for attempt in range(max_retries):
        attempts += 1
        prefix = f"[{task_index+1}/{total}]"

        if attempt > 0:
            log(f"{prefix} 重试 ({attempt+1}/{max_retries}): {task_desc[:60]}")
        else:
            log("")
            log(f"{prefix} 任务: {task_desc[:70]}")

        # checkpoint
        try:
            checkpoint = verifier.checkpoint(project_path, f"task-{task_id}-attempt-{attempt+1}")
            log(f"  -> checkpoint: {checkpoint[:8]}")
        except VerifierError as e:
            log(f"  -> checkpoint 失败: {e}")
            return {"task": task, "status": "failed", "attempts": attempts, "error": str(e)}

        # 执行
        with Spinner(f"  -> OpenCode 执行中..."):
            result = worker.execute(current_task)
        duration = result["duration"]
        log(f"  -> OpenCode 完成 ({duration}s)")

        if not result["success"]:
            log(f"  -> 执行失败: {result['output'][:200]}")
            try:
                verifier.rollback(project_path, checkpoint)
                log(f"  -> 已回滚")
            except VerifierError:
                pass
            current_task = f"{task_desc}\n\n上次尝试失败: {result['output'][:500]}\n请修正以上问题。"
            continue

        # 验证
        diff = ""
        build_ok, build_out = True, "(跳过)"
        test_ok, test_out = True, "(跳过)"

        try:
            diff = verifier.get_diff(project_path)
        except VerifierError as e:
            diff = f"(获取 diff 失败: {e})"

        if cfg_build := verifier.build_cmd:
            log(f"  -> 运行构建: {cfg_build}")
            build_ok, build_out = verifier.build(project_path)
            log(f"  -> 构建: {'OK' if build_ok else 'FAIL'}")

        if cfg_test := verifier.test_cmd:
            log(f"  -> 运行测试: {cfg_test}")
            test_ok, test_out = verifier.test(project_path)
            log(f"  -> 测试: {'OK' if test_ok else 'FAIL'}")

        # GLM 审查
        with Spinner(f"  -> GLM 审查中..."):
            try:
                review = supervisor.review(current_task, diff, build_out, test_out)
            except SupervisorError as e:
                review = {"passed": False, "score": 0, "issues": [str(e)], "feedback": str(e)}

        score = review.get("score", 0)
        passed = review.get("passed", False)
        icon = "PASS" if passed else "FAIL"
        log(f"  -> GLM 审查: {icon} (score: {score})")

        if passed and build_ok and test_ok:
            save_state(state_dir, f"task_{task_id}_result.json", {
                "task": task, "status": "passed", "attempts": attempts,
                "duration": duration, "review": review,
            })
            if review.get("issues"):
                for issue in review["issues"][:3]:
                    log(f"     注意: {issue}")
            return {
                "task": task, "status": "passed", "attempts": attempts,
                "duration": duration, "review": review,
            }
        else:
            feedback = review.get("feedback", "未知原因")
            log(f"  -> 反馈: {feedback[:150]}")
            try:
                verifier.rollback(project_path, checkpoint)
                log(f"  -> 已回滚")
            except VerifierError as e:
                log(f"  -> 回滚失败: {e}")

            current_task = (
                f"{task_desc}\n\n"
                f"--- 上次尝试失败 (attempt {attempt+1}) ---\n"
                f"GLM 审查反馈:\n{feedback}\n\n"
                f"发现的问题:\n"
                + "\n".join(f"- {i}" for i in review.get("issues", []))
                + "\n\n请修正以上所有问题。"
            )

    log(f"  -> !! 重试 {max_retries} 次后仍失败")
    return {
        "task": task, "status": "failed", "attempts": attempts,
        "review": review if "review" in dir() else {},
    }


# ── CLI ──────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="AutoCoder — GLM 监督 + OpenCode 执行的全自动编码 Agent",
    )
    parser.add_argument("goal", help="编码目标描述")
    parser.add_argument("--config", "-c", default="config.yaml", help="配置文件路径")
    parser.add_argument("--yes", "-y", action="store_true", help="跳过确认")
    parser.add_argument("--resume", "-r", action="store_true", help="从上次中断恢复")
    parser.add_argument("--verbose", "-v", action="store_true", help="详细日志")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    run(args.goal, args.config, args.yes, args.resume)


if __name__ == "__main__":
    main()
