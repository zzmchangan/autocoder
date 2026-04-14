"""
AutoCoder 监督者
GLM 驱动的任务规划、代码审查和决策模块
每次调用都是无状态的，不累积对话历史
"""

import json
import os
from pathlib import Path

from openai import OpenAI

# ── Prompt 模板 ──────────────────────────────────────────

PLAN_SYSTEM = """你是一个资深软件架构师。你的任务是将用户的编码目标拆分为可独立执行的子任务。

要求:
1. 每个子任务必须是明确的、可由编码Agent独立完成的指令
2. 子任务之间按依赖顺序排列
3. 每个子任务应明确要操作的文件或目录
4. 单个任务最多涉及 5 个文件
5. 总任务数控制在 3-8 个
6. 每个任务标注风险等级: low / medium / high

严格输出以下 JSON 格式，不要输出任何其他内容:
{
  "summary": "一句话概述整体计划",
  "tasks": [
    {
      "id": 1,
      "task": "具体的任务描述，要足够详细让编码Agent能独立完成",
      "target_files": ["file1", "file2"],
      "risk": "low",
      "depends_on": []
    }
  ]
}"""

REVIEW_SYSTEM = """你是一个严格的代码审查员。你需要审查编码Agent完成的代码变更。

审查维度:
1. **目标完成度**: 变更是否正确完成了任务目标？
2. **正确性**: 是否引入了新的bug或逻辑错误？
3. **代码质量**: 代码风格、命名、结构是否合格？
4. **完整性**: 是否有遗漏的修改？

严格输出以下 JSON 格式，不要输出任何其他内容:
{
  "passed": true,
  "score": 85,
  "issues": ["issue1", "issue2"],
  "feedback": "详细说明。如果不通过，必须包含具体的修复建议和正确的代码示例。"
}

passed 为 true 时 score 必须 >= 60，为 false 时 score 必须 < 60。
issues 列出发现的问题，没有问题时为空数组。"""

SUMMARY_SYSTEM = """你是一个项目管理者。请根据任务执行结果生成一份简洁的最终报告。
用中文输出，包含: 完成情况、关键变更、遗留问题、后续建议。"""


class SupervisorError(Exception):
    pass


class Supervisor:
    """GLM 监督者 — 无状态规划与审查"""

    def __init__(self, api_key: str, base_url: str, model: str):
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.model = model

    # ── 项目分析 ──────────────────────────────────────────

    def analyze_project(self, project_path: str) -> str:
        """收集项目上下文，控制在 4000 字符内"""
        parts = []

        # 目录树 (3 层深度)
        tree = self._get_tree(project_path, max_depth=3)
        parts.append(f"项目结构:\n{tree}")

        # 关键配置文件
        key_files = [
            "README.md", "CLAUDE.md", "package.json", "Cargo.toml",
            "pom.xml", "build.gradle", "*.csproj", "*.sln",
            "go.mod", "requirements.txt", "pyproject.toml",
        ]
        for name in key_files:
            path = self._find_file(project_path, name)
            if path:
                content = self._read_head(path, max_chars=500)
                parts.append(f"\n--- {os.path.relpath(path, project_path)} ---\n{content}")

        context = "\n".join(parts)
        return _truncate(context, 4000)

    # ── 任务规划 ──────────────────────────────────────────

    def plan_tasks(self, goal: str, project_context: str) -> list[dict]:
        """
        将目标拆分为子任务。无状态调用。
        返回: [{"id": 1, "task": "...", "target_files": [...], "risk": "low"}, ...]
        """
        user_msg = (
            f"## 编码目标\n{goal}\n\n"
            f"## 项目上下文\n{project_context}"
        )

        response = self._call(PLAN_SYSTEM, user_msg, temperature=0.2)
        data = self._parse_json(response)

        if "tasks" not in data:
            raise SupervisorError(f"规划结果缺少 tasks 字段: {response[:500]}")

        return data["tasks"]

    # ── 代码审查 ──────────────────────────────────────────

    def review(
        self,
        task: str,
        diff: str,
        build_result: str,
        test_result: str,
    ) -> dict:
        """
        审查任务执行结果。无状态调用。
        返回: {"passed": bool, "score": int, "issues": list, "feedback": str}
        """
        user_msg = (
            f"## 任务描述\n{task}\n\n"
            f"## 构建结果\n{build_result}\n\n"
            f"## 测试结果\n{test_result}\n\n"
            f"## 代码变更 (git diff)\n{diff}"
        )

        response = self._call(REVIEW_SYSTEM, user_msg, temperature=0.1)
        result = self._parse_json(response)

        # 确保必要字段
        result.setdefault("passed", False)
        result.setdefault("score", 0)
        result.setdefault("issues", [])
        result.setdefault("feedback", "")

        return result

    # ── 最终报告 ──────────────────────────────────────────

    def summarize(self, results: list[dict]) -> str:
        """生成最终报告"""
        parts = []
        for r in results:
            status = "PASS" if r["status"] == "passed" else "FAIL"
            parts.append(
                f"- 任务 {r['task']['id']}: [{status}] "
                f"{r['task']['task'][:60]} "
                f"(score: {r.get('review', {}).get('score', 'N/A')}, "
                f"attempts: {r.get('attempts', 1)})"
            )

        user_msg = (
            f"## 任务执行结果\n"
            + "\n".join(parts)
            + f"\n\n通过: {sum(1 for r in results if r['status'] == 'passed')}/{len(results)}"
        )

        return self._call(SUMMARY_SYSTEM, user_msg, temperature=0.3)

    # ── 内部方法 ──────────────────────────────────────────

    def _call(self, system: str, user: str, temperature: float = 0.1) -> str:
        """无状态 API 调用"""
        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=temperature,
            )
            return resp.choices[0].message.content
        except Exception as e:
            raise SupervisorError(f"GLM API 调用失败: {e}") from e

    @staticmethod
    def _parse_json(text: str) -> dict:
        """从模型输出中提取 JSON"""
        # 尝试直接解析
        text = text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # 尝试提取 ```json ... ``` 块
        import re
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass

        # 尝试找第一个 { 到最后一个 }
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end])
            except json.JSONDecodeError:
                pass

        raise SupervisorError(f"无法解析 JSON 输出: {text[:500]}")

    @staticmethod
    def _get_tree(path: str, max_depth: int = 3, _prefix: str = "", _depth: int = 0) -> str:
        """生成目录树"""
        if _depth >= max_depth:
            return ""
        lines = []
        try:
            entries = sorted(os.listdir(path))
        except PermissionError:
            return ""
        # 过滤隐藏目录和常见忽略项
        skip = {".git", "node_modules", "__pycache__", ".vs", "bin", "obj",
                ".autocoder", ".claude"}
        for entry in entries:
            if entry.startswith(".") and entry not in {".github", ".env"}:
                continue
            if entry in skip:
                continue
            full = os.path.join(path, entry)
            if os.path.isdir(full):
                lines.append(f"{_prefix}{entry}/")
                sub = Supervisor._get_tree(full, max_depth, _prefix + "  ", _depth + 1)
                if sub:
                    lines.append(sub)
            else:
                lines.append(f"{_prefix}{entry}")
        return "\n".join(lines)

    @staticmethod
    def _find_file(project_path: str, pattern: str) -> str | None:
        """查找关键文件"""
        import glob
        if "*" in pattern:
            matches = glob.glob(os.path.join(project_path, pattern))
            return matches[0] if matches else None
        path = os.path.join(project_path, pattern)
        return path if os.path.exists(path) else None

    @staticmethod
    def _read_head(path: str, max_chars: int = 500) -> str:
        """读取文件前 N 字符"""
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read(max_chars)
            return content
        except Exception:
            return ""


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    half = max_len // 2
    return text[:half] + f"\n... (截断 {len(text)} 字符) ...\n" + text[-half:]
