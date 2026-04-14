"""AutoCoder 集成测试 - 不依赖外部 API"""
import sys
import os
import time
import tempfile
import shutil

sys.path.insert(0, os.path.dirname(__file__))

from autocoder import log, Spinner, ts, save_state, load_state
from config import load_config
from verifier import Verifier, VerifierError
from supervisor import Supervisor

passed = 0
failed = 0

def test(name, fn):
    global passed, failed
    try:
        fn()
        log(f"  PASS: {name}")
        passed += 1
    except Exception as e:
        log(f"  FAIL: {name} -> {e}")
        failed += 1


# ── 测试1: log 和 Spinner ──
def test_log():
    log("log 输出正常")

def test_spinner():
    with Spinner("旋转动画测试..."):
        time.sleep(1.0)
    log("Spinner 结束")

def test_timestamp():
    t = ts()
    assert len(t) == 8, f"时间戳格式错误: {t}"
    assert ":" in t

# ── 测试2: 状态持久化 ──
def test_state():
    tmp = os.path.join(tempfile.gettempdir(), "ac_test_state")
    os.makedirs(tmp, exist_ok=True)
    try:
        from pathlib import Path
        save_state(Path(tmp), "test.json", {"key": "value", "num": 42})
        data = load_state(Path(tmp), "test.json")
        assert data["key"] == "value"
        assert data["num"] == 42
        assert load_state(Path(tmp), "nonexist.json") is None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

# ── 测试3: Config 加载 ──
def test_config():
    cfg = load_config("config.yaml")
    assert cfg.supervisor_model == "glm-5.1", f"got {cfg.supervisor_model}"
    assert cfg.worker_model == "glm-5.1", f"got {cfg.worker_model}"
    assert bool(cfg.supervisor_api_key), "api_key should be set"
    assert "bigmodel.cn" in cfg.supervisor_base_url

# ── 测试4: Verifier 完整流程 ──
def test_verifier():
    tmp = os.path.join(tempfile.gettempdir(), "ac_test_verifier")
    if os.path.exists(tmp):
        shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)

    v = Verifier(build_cmd=None, test_cmd=None)

    # init
    v.init_repo(tmp)
    assert v.is_git_repo(tmp), "should be git repo"
    assert v.check_git_clean(tmp), "should be clean"

    # checkpoint
    cp1 = v.checkpoint(tmp, "test1")
    assert len(cp1) == 40, f"commit hash: {cp1}"

    # write + checkpoint
    with open(os.path.join(tmp, "main.rs"), "w") as f:
        f.write("fn main() {}")
    cp2 = v.checkpoint(tmp, "write-file")

    # diff
    diff = v.get_diff(tmp)
    assert "main.rs" in diff, f"diff missing main.rs: {diff[:200]}"

    # changed files
    files = v.get_changed_files(tmp)
    assert "main.rs" in files, f"changed files: {files}"

    # rollback
    v.rollback(tmp, cp1)
    assert not os.path.exists(os.path.join(tmp, "main.rs")), "should be rolled back"

    # build/test skip
    ok, out = v.build(tmp)
    assert ok and "跳过" in out
    ok, out = v.test(tmp)
    assert ok and "跳过" in out

    shutil.rmtree(tmp, ignore_errors=True)

# ── 测试5: Verifier build 命令 ──
def test_verifier_build():
    v = Verifier(build_cmd="echo hello_build", test_cmd="echo hello_test")
    ok, out = v.build(".")
    assert ok, f"build should pass: {out}"
    ok, out = v.test(".")
    assert ok, f"test should pass: {out}"

def test_verifier_build_fail():
    v = Verifier(build_cmd="exit 1", test_cmd=None)
    ok, out = v.build(".")
    assert not ok, "should fail"

# ── 测试6: Supervisor JSON 解析 ──
def test_supervisor_json():
    # 直接解析
    assert Supervisor._parse_json('{"a": 1}') == {"a": 1}

    # 带 markdown 代码块
    assert Supervisor._parse_json('```json\n{"b": 2}\n```') == {"b": 2}

    # 混杂文字
    assert Supervisor._parse_json('以下是结果:\n{"c": 3}\n结束') == {"c": 3}

# ── 运行所有测试 ──
log("=" * 50)
log("AutoCoder 集成测试")
log("=" * 50)

test("log 输出", test_log)
test("Spinner 动画", test_spinner)
test("时间戳格式", test_timestamp)
test("状态持久化", test_state)
test("Config 加载", test_config)
test("Verifier 完整流程", test_verifier)
test("Verifier build 命令", test_verifier_build)
test("Verifier build 失败", test_verifier_build_fail)
test("Supervisor JSON 解析", test_supervisor_json)

log("=" * 50)
log(f"结果: {passed} passed, {failed} failed")
log("=" * 50)

sys.exit(1 if failed > 0 else 0)
