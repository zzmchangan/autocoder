"""
AutoCoder Worker
通过 OpenCode HTTP API 执行编码任务

OpenCode (anomalyco/opencode) 以 client-server 架构运行:
- `opencode serve` 启动后台 HTTP 服务
- 通过 REST API 创建 session、发送消息、等待完成
- 每个子任务使用独立 session，避免上下文污染
"""

import json
import logging
import os
import shutil
import subprocess
import time

import requests

logger = logging.getLogger("autocoder.worker")


class WorkerError(Exception):
    pass


class Worker:
    """OpenCode 执行器 — 管理 opencode serve 进程并通过 HTTP API 控制"""

    def __init__(
        self,
        command: str = "opencode",
        host: str = "127.0.0.1",
        port: int = 4096,
        model: str = "glm-4-plus",
        timeout: int = 600,
    ):
        self.command = command
        self.host = host
        self.port = port
        self.model = model
        self.timeout = timeout
        self.base_url = f"http://{host}:{port}"
        self._process: subprocess.Popen | None = None

    # ── 服务生命周期 ──────────────────────────────────────

    def start_server(self, project_path: str) -> None:
        """启动 opencode serve 后台进程"""
        # 检查 opencode 是否可用
        opencode_path = shutil.which(self.command)
        if not opencode_path:
            raise WorkerError(
                f"找不到 opencode 命令: {self.command}\n"
                "请安装: npm install -g @anthropic-ai/opencode"
            )

        env = os.environ.copy()
        env["OPENCODE_PERMISSION"] = "allow-all"
        env["OPENCODE_DISABLE_AUTOUPDATE"] = "true"
        env["OPENCODE_DISABLE_TERMINAL_TITLE"] = "true"

        logger.info(f"启动 opencode serve ({self.host}:{self.port})")
        self._process = subprocess.Popen(
            [self.command, "serve", "--hostname", self.host, "--port", str(self.port)],
            cwd=project_path,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        # 等待服务就绪
        self._wait_for_health(max_wait=30)

    def stop_server(self) -> None:
        """停止 opencode serve 进程"""
        if self._process and self._process.poll() is None:
            logger.info("停止 opencode serve")
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
            self._process = None

    def is_running(self) -> bool:
        """检查服务是否运行中"""
        if not self._process or self._process.poll() is not None:
            return False
        try:
            resp = requests.get(f"{self.base_url}/global/health", timeout=2)
            return resp.status_code == 200
        except requests.RequestException:
            return False

    # ── 任务执行 ──────────────────────────────────────────

    def execute(self, task: str) -> dict:
        """
        执行单个编码任务。
        1. 创建新 session
        2. 发送任务消息
        3. 等待完成
        4. 返回结果

        返回: {"success": bool, "output": str, "duration": float}
        """
        start = time.time()

        try:
            # 创建新 session
            session_id = self._create_session()
            logger.info(f"创建 session: {session_id}")

            # 发送任务
            self._send_message(session_id, task)

            # 等待完成
            output = self._wait_for_completion(session_id)

            duration = time.time() - start
            return {
                "success": True,
                "output": output,
                "duration": round(duration, 1),
                "session_id": session_id,
            }

        except Exception as e:
            duration = time.time() - start
            return {
                "success": False,
                "output": str(e),
                "duration": round(duration, 1),
            }

    # ── HTTP API 调用 ─────────────────────────────────────

    def _create_session(self) -> str:
        """创建新的 OpenCode session"""
        resp = requests.post(
            f"{self.base_url}/session",
            json={"title": "autocoder-task"},
            timeout=10,
        )
        if resp.status_code != 200:
            raise WorkerError(f"创建 session 失败: {resp.status_code} {resp.text}")
        data = resp.json()
        return data.get("id") or data.get("sessionId")

    def _send_message(self, session_id: str, content: str) -> None:
        """发送任务消息到 session"""
        resp = requests.post(
            f"{self.base_url}/session/{session_id}/message",
            json={"content": content},
            timeout=30,
        )
        if resp.status_code not in (200, 201):
            raise WorkerError(f"发送消息失败: {resp.status_code} {resp.text}")

    def _wait_for_completion(self, session_id: str, poll_interval: float = 3.0) -> str:
        """轮询等待 session 完成"""
        deadline = time.time() + self.timeout
        last_parts = []
        last_log = 0

        while time.time() < deadline:
            try:
                resp = requests.get(
                    f"{self.base_url}/session/{session_id}",
                    timeout=10,
                )
            except requests.RequestException:
                # 短暂网络错误，重试
                time.sleep(2)
                continue

            if resp.status_code != 200:
                raise WorkerError(f"查询 session 失败: {resp.status_code}")

            data = resp.json()
            status = data.get("status", "")

            # 收集 assistant 的输出
            parts = []
            for msg in data.get("messages", []):
                if msg.get("role") == "assistant":
                    for part in msg.get("parts", []):
                        if part.get("type") == "text":
                            parts.append(part.get("text", ""))

            if parts != last_parts:
                last_parts = parts

            # 每 15 秒输出一次状态
            now = time.time()
            if now - last_log > 15:
                elapsed = int(now - deadline + self.timeout)
                logger.info(f"OpenCode 执行中... 已用时 {elapsed}s, 状态: {status}")
                last_log = now

            # 检查是否完成
            if status in ("idle", "completed", "done"):
                return "\n".join(parts) if parts else "(无输出)"

            time.sleep(poll_interval)

        # 超时
        return "\n".join(last_parts) if last_parts else "(执行超时，无输出)"

    def _wait_for_health(self, max_wait: float = 30) -> None:
        """等待 HTTP 服务就绪"""
        deadline = time.time() + max_wait
        while time.time() < deadline:
            try:
                resp = requests.get(f"{self.base_url}/global/health", timeout=2)
                if resp.status_code == 200:
                    logger.info("opencode serve 就绪")
                    return
            except requests.RequestException:
                pass

            # 检查进程是否已退出
            if self._process and self._process.poll() is not None:
                stderr = self._process.stderr.read().decode() if self._process.stderr else ""
                raise WorkerError(f"opencode serve 启动失败: {stderr[:500]}")

            time.sleep(1)

        raise WorkerError(f"opencode serve 在 {max_wait}s 内未就绪")

    # ── 清理 ──────────────────────────────────────────────

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.stop_server()
