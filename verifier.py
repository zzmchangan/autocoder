"""
AutoCoder 验证器
git checkpoint/rollback + build/test 执行
"""

import subprocess
import os


class VerifierError(Exception):
    pass


class Verifier:
    """验证器：git 安全网 + 构建/测试执行"""

    def __init__(self, build_cmd: str | None = None, test_cmd: str | None = None):
        self.build_cmd = build_cmd
        self.test_cmd = test_cmd

    # ── Git 操作 ──────────────────────────────────────────

    @staticmethod
    def check_git_clean(project_path: str) -> bool:
        """检查工作区是否干净"""
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=project_path,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip() == ""

    @staticmethod
    def is_git_repo(project_path: str) -> bool:
        """检查是否是 git 仓库"""
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=project_path,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0

    @staticmethod
    def init_repo(project_path: str) -> None:
        """初始化 git 仓库并创建首次提交"""
        subprocess.run(
            ["git", "init"],
            cwd=project_path,
            capture_output=True,
            text=True,
            check=True,
        )
        # 创建占位文件并首次提交，确保有 HEAD
        placeholder = os.path.join(project_path, ".gitkeep")
        with open(placeholder, "w") as f:
            f.write("")
        subprocess.run(
            ["git", "add", "-A"],
            cwd=project_path,
            capture_output=True,
            text=True,
            check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "autocoder: initial commit", "--no-gpg-sign"],
            cwd=project_path,
            capture_output=True,
            text=True,
            check=True,
        )

    def checkpoint(self, project_path: str, task_label: str = "task") -> str:
        """
        创建 git checkpoint，返回 commit hash。
        如果工作区无变更，返回当前 HEAD hash。
        """
        self._git(project_path, "add", "-A")

        # 检查是否有东西要提交
        status = self._git(project_path, "status", "--porcelain")
        if status.strip():
            self._git(
                project_path,
                "commit", "-m", f"autocoder: checkpoint before {task_label}",
                "--no-gpg-sign",
            )

        # 返回当前 commit hash
        result = self._git(project_path, "rev-parse", "HEAD")
        return result.strip()

    def rollback(self, project_path: str, commit_hash: str) -> None:
        """回滚到指定 commit"""
        self._git(project_path, "reset", "--hard", commit_hash)
        # 清除未跟踪文件
        self._git(project_path, "clean", "-fd")

    def get_diff(self, project_path: str) -> str:
        """获取自上次 checkpoint 以来的变更"""
        # 统计信息
        stat = self._git(project_path, "diff", "HEAD~1", "--stat")
        # 完整 diff
        full_diff = self._git(project_path, "diff", "HEAD~1")

        result = f"=== Diff Stats ===\n{stat}\n\n=== Full Diff ===\n{full_diff}"
        return _truncate(result, 8000)

    def get_changed_files(self, project_path: str) -> list[str]:
        """获取变更的文件列表"""
        output = self._git(project_path, "diff", "--name-only", "HEAD~1")
        return [f for f in output.strip().split("\n") if f]

    # ── 构建和测试 ────────────────────────────────────────

    def build(self, project_path: str) -> tuple[bool, str]:
        """执行构建命令，返回 (成功?, 输出)"""
        if not self.build_cmd:
            return True, "(未配置构建命令，跳过)"
        return self._run_command(self.build_cmd, project_path, timeout=120)

    def test(self, project_path: str) -> tuple[bool, str]:
        """执行测试命令，返回 (成功?, 输出)"""
        if not self.test_cmd:
            return True, "(未配置测试命令，跳过)"
        return self._run_command(self.test_cmd, project_path, timeout=300)

    # ── 内部方法 ──────────────────────────────────────────

    @staticmethod
    def _git(project_path: str, *args: str) -> str:
        """执行 git 命令，返回 stdout"""
        result = subprocess.run(
            ["git"] + list(args),
            cwd=project_path,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise VerifierError(f"git {args[0]} 失败: {result.stderr.strip()}")
        return result.stdout

    @staticmethod
    def _run_command(
        cmd: str, cwd: str, timeout: int = 120
    ) -> tuple[bool, str]:
        """执行 shell 命令"""
        try:
            result = subprocess.run(
                cmd,
                shell=True,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            output = result.stdout + result.stderr
            return result.returncode == 0, _truncate(output, 2000)
        except subprocess.TimeoutExpired:
            return False, f"命令超时 ({timeout}s): {cmd}"
        except Exception as e:
            return False, f"命令执行失败: {e}"


def _truncate(text: str, max_len: int) -> str:
    """截断过长文本"""
    if len(text) <= max_len:
        return text
    half = max_len // 2
    return text[:half] + f"\n\n... (截断，共 {len(text)} 字符) ...\n\n" + text[-half:]
