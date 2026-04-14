"""
AutoCoder 配置管理
支持 YAML 文件 + 环境变量覆盖
"""

import os
import re
from pathlib import Path

import yaml


# ── 默认配置 ──────────────────────────────────────────────
_DEFAULTS = {
    "supervisor": {
        "api_key": "",
        "base_url": "https://open.bigmodel.cn/api/paas/v4/",
        "model": "glm-5.1",
    },
    "worker": {
        "model": "glm-5.1",
        "host": "127.0.0.1",
        "port": 4096,
        "timeout": 600,
        "command": "opencode",  # opencode 可执行文件路径
    },
    "verifier": {
        "build_cmd": None,
        "test_cmd": None,
    },
    "general": {
        "max_retries": 3,
        "stop_on_failure": False,
        "project_path": ".",
    },
}


def _resolve_env_vars(value):
    """递归替换 ${VAR} 格式的环境变量引用"""
    if isinstance(value, str):
        def _replace(m):
            return os.environ.get(m.group(1), m.group(0))
        return re.sub(r"\$\{(\w+)\}", _replace, value)
    if isinstance(value, dict):
        return {k: _resolve_env_vars(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_env_vars(v) for v in value]
    return value


def _deep_merge(base, override):
    """深度合并字典，override 覆盖 base"""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


class Config:
    """AutoCoder 配置对象"""

    def __init__(self, data: dict):
        self._data = data

    # ── 便捷属性 ──────────────────────────────────────────
    @property
    def supervisor_api_key(self) -> str:
        return self._data["supervisor"]["api_key"]

    @property
    def supervisor_base_url(self) -> str:
        return self._data["supervisor"]["base_url"]

    @property
    def supervisor_model(self) -> str:
        return self._data["supervisor"]["model"]

    @property
    def worker_model(self) -> str:
        return self._data["worker"]["model"]

    @property
    def worker_host(self) -> str:
        return self._data["worker"]["host"]

    @property
    def worker_port(self) -> int:
        return int(self._data["worker"]["port"])

    @property
    def worker_timeout(self) -> int:
        return int(self._data["worker"]["timeout"])

    @property
    def worker_command(self) -> str:
        return self._data["worker"]["command"]

    @property
    def build_cmd(self) -> str | None:
        return self._data["verifier"]["build_cmd"]

    @property
    def test_cmd(self) -> str | None:
        return self._data["verifier"]["test_cmd"]

    @property
    def max_retries(self) -> int:
        return int(self._data["general"]["max_retries"])

    @property
    def stop_on_failure(self) -> bool:
        return bool(self._data["general"]["stop_on_failure"])

    @property
    def project_path(self) -> str:
        return os.path.abspath(self._data["general"]["project_path"])

    @property
    def worker_url(self) -> str:
        return f"http://{self.worker_host}:{self.worker_port}"

    @property
    def raw(self) -> dict:
        return self._data

    # ── 工作目录 ──────────────────────────────────────────
    @property
    def state_dir(self) -> Path:
        """中间状态目录 (.autocoder/)"""
        p = Path(self.project_path) / ".autocoder"
        p.mkdir(exist_ok=True)
        return p

    def __repr__(self):
        # 隐藏 api_key
        safe = _deep_merge(self._data, {})
        if safe.get("supervisor", {}).get("api_key"):
            key = safe["supervisor"]["api_key"]
            safe["supervisor"]["api_key"] = key[:8] + "..." if len(key) > 8 else "***"
        return f"Config({safe})"


def load_config(config_path: str | None = None) -> Config:
    """
    加载配置。优先级: 环境变量 > config.yaml > 默认值
    """
    # 从默认值开始
    data = _deep_merge({}, _DEFAULTS)

    # 如果有 YAML 文件则合并
    if config_path and Path(config_path).exists():
        with open(config_path, "r", encoding="utf-8") as f:
            file_data = yaml.safe_load(f) or {}
        data = _deep_merge(data, file_data)

    # 解析 ${ENV} 引用
    data = _resolve_env_vars(data)

    # 环境变量直接覆盖关键字段
    if env_key := os.environ.get("GLM_API_KEY"):
        data["supervisor"]["api_key"] = env_key
    if env_base := os.environ.get("GLM_BASE_URL"):
        data["supervisor"]["base_url"] = env_base
    if env_model := os.environ.get("GLM_MODEL"):
        data["supervisor"]["model"] = env_model
    if env_path := os.environ.get("AUTOCODER_PROJECT_PATH"):
        data["general"]["project_path"] = env_path

    return Config(data)
