# AutoCoder

全自动编码 Agent — GLM 监督 + OpenCode 执行 + Git 安全网。

## 它解决什么问题

AI 编码工具（Claude Code、OpenCode）需要每步人工确认，无法连续自主工作。AutoCoder 引入 **GLM 监督者** 实现完全自动化：

- GLM 负责任务规划、代码审查、通过/失败决策
- OpenCode 负责实际编码执行
- Git 提供 checkpoint/rollback 安全网
- 失败自动回滚并带反馈重试，直到通过或重试耗尽

## 架构

```
用户目标
   │
   ▼
┌──────────────────────────────────────────────┐
│  Python 编排器 (autocoder.py)                 │
│                                              │
│  GLM 规划 ─► 拆分为 3~8 个子任务              │
│                                              │
│  循环每个子任务:                               │
│    git checkpoint ─► OpenCode 执行 ─► 验证    │
│    GLM 审查 ─► PASS(下一任务) / FAIL(回滚重试) │
│                                              │
│  生成最终报告                                 │
└──────────────────────────────────────────────┘
```

## 上下文防爆设计

自动 Agent 的核心难题是上下文窗口爆炸。AutoCoder 采用三层隔离：

| 层级 | 策略 | 效果 |
|------|------|------|
| Worker | 每任务独立 OpenCode session | 任务间上下文不累积 |
| Supervisor | 每次 API 调用无状态（不保留对话历史） | 审查次数不影响上下文 |
| 状态 | 中间结果写入 `.autocoder/` 目录 | 按需读取，不占用上下文 |

数据流（无累积）：

```
plan_tasks()    → JSON → plan.json         → API 结束，上下文释放
execute(task_1) → 新 session → OpenCode    → 结果写入 task_1_result.json
review(task_1)  → 读 result → 单次 API     → 返回 pass/fail
execute(task_2) → 新 session → ...         → 上下文大小恒定
```

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

需要 Python 3.10+，以及 [OpenCode](https://github.com/anomalyco/opencode) 已安装并在 PATH 中。

### 2. 配置

复制 `config.yaml` 并修改：

```yaml
supervisor:
  api_key: ${GLM_API_KEY}          # 或直接写 key
  base_url: "https://open.bigmodel.cn/api/paas/v4/"
  model: "glm-4-plus"

worker:
  model: "glm-4-plus"
  port: 4096
  timeout: 600

verifier:
  build_cmd: "dotnet build"        # 项目构建命令，null 跳过
  test_cmd: "dotnet test"          # 项目测试命令，null 跳过
```

环境变量覆盖（优先级最高）：

```bash
export GLM_API_KEY="your-key"
export GLM_MODEL="glm-4-plus"
export AUTOCODER_PROJECT_PATH="/path/to/project"
```

### 3. 运行

```bash
# 基本用法
python autocoder.py "你的编码目标" --config config.yaml

# 跳过确认，全自动
python autocoder.py "给 README 添加安装说明" --yes

# 从上次中断处恢复
python autocoder.py "..." --resume
```

## CLI 参数

| 参数 | 缩写 | 说明 |
|------|------|------|
| `goal` | — | 编码目标描述（必填） |
| `--config` | `-c` | 配置文件路径，默认 `config.yaml` |
| `--yes` | `-y` | 跳过所有确认提示 |
| `--resume` | `-r` | 从上次中断恢复（复用已有计划） |
| `--verbose` | `-v` | 显示详细日志 |

## 执行流程

```
$ python autocoder.py "将项目中所有 float 替换为 LFloat" --config config.yaml

============================================================
AutoCoder — 全自动编码 Agent
============================================================
项目: /path/to/project
监督者: glm-4-plus
执行者: opencode serve (glm-4-plus)
目标: 将项目中所有 float 替换为 LFloat

[1/4] 分析项目结构...
  项目上下文: 3200 字符

[2/4] 规划任务...
  计划摘要:
    [1] ! 扫描 Lockstep.Core 中所有使用 float/double 的文件
        files: LMath.cs, PhysicsSystem.cs, ...
    [2] !! 替换 LMath.cs 中的浮点运算为定点数
        files: LMath.cs
    [3] ! 替换物理系统中的浮点参数
        files: PhysicsSystem.cs, CollisionHelper.cs

  确认执行? [Y/n]

[3/4] 启动 OpenCode 服务...
  opencode serve 就绪

  [1/3] 执行: 扫描 Lockstep.Core 中所有使用 float/double 的文件
    OpenCode 执行中... (12s)
    验证中... build=OK, test=OK
    GLM 审查中... PASS (score: 92)

  [2/3] 执行: 替换 LMath.cs 中的浮点运算为定点数
    OpenCode 执行中... (28s)
    验证中... build=FAIL, test=OK
    GLM 审查中... FAIL (score: 35)
    反馈: LFloat 不能隐式转换自 double，需要使用 LFloat.FromDouble()
    已回滚
    重试 (2/3): 替换 LMath.cs 中的浮点运算为定点数
    OpenCode 执行中... (35s)
    验证中... build=OK, test=OK
    GLM 审查中... PASS (score: 88)

  [3/3] 执行: 替换物理系统中的浮点参数
    OpenCode 执行中... (18s)
    验证中... build=OK, test=OK
    GLM 审查中... PASS (score: 90)

[4/4] 停止 OpenCode 服务...

============================================================
最终报告
============================================================
通过: 3/3, 失败: 0
  [OK] 任务 1: 扫描 Lockstep.Core 中... (score: 92, attempts: 1)
  [OK] 任务 2: 替换 LMath.cs 中的... (score: 88, attempts: 2)
  [OK] 任务 3: 替换物理系统中的... (score: 90, attempts: 1)
```

## 文件说明

```
autocoder/
├── autocoder.py       主入口，编排循环 + CLI
├── supervisor.py      GLM 监督者：项目分析、任务规划、代码审查、报告生成
├── worker.py          OpenCode 执行器：管理 opencode serve 进程，HTTP API 交互
├── verifier.py        验证器：git checkpoint/rollback、构建/测试执行
├── config.py          配置加载：YAML + 环境变量，支持 ${VAR} 引用
├── config.yaml        示例配置文件
├── requirements.txt   Python 依赖
└── README.md          本文件
```

## 安全机制

1. **Git 强制** — 项目必须是 git 仓库，且工作区干净（或手动确认）
2. **每步 Checkpoint** — 每个子任务执行前自动 commit 当前状态
3. **失败自动回滚** — 审查不通过时 `git reset --hard` 恢复到 checkpoint
4. **超时保护** — 每个任务有超时限制（默认 600 秒），防止卡死
5. **人工确认** — 默认展示计划后等待确认，`--yes` 可跳过

## 中间状态

运行过程中自动创建 `.autocoder/` 目录保存状态：

```
.autocoder/
├── plan.json              任务规划结果
├── project_context.json   项目上下文
├── task_1_result.json     任务 1 执行结果
├── task_2_result.json     任务 2 执行结果
├── results.json           所有任务结果汇总
└── summary.json           最终报告
```

配合 `--resume` 可以从中断处恢复，跳过已完成的规划步骤。

## 配置参考

```yaml
supervisor:
  api_key: ${GLM_API_KEY}           # GLM API key
  base_url: "https://open.bigmodel.cn/api/paas/v4/"  # API 地址
  model: "glm-4-plus"               # 监督者模型

worker:
  model: "glm-4-plus"               # OpenCode 使用的执行模型
  host: "127.0.0.1"                 # opencode serve 监听地址
  port: 4096                        # opencode serve 端口
  timeout: 600                      # 单任务超时秒数
  command: "opencode"               # opencode 可执行文件路径

verifier:
  build_cmd: null                   # 构建命令，null 跳过
  test_cmd: null                    # 测试命令，null 跳过

general:
  max_retries: 3                    # 单任务最大重试次数
  stop_on_failure: false            # 失败后是否停止全部任务
  project_path: "."                 # 项目路径
```

构建/测试命令示例：

```yaml
# C# / .NET
verifier:
  build_cmd: "dotnet build"
  test_cmd: "dotnet test"

# Node.js
verifier:
  build_cmd: "npm run build"
  test_cmd: "npm test"

# Python
verifier:
  build_cmd: null
  test_cmd: "pytest"

# Rust
verifier:
  build_cmd: "cargo build"
  test_cmd: "cargo test"
```

## 要求

- Python 3.10+
- Git
- [OpenCode](https://github.com/anomalyco/opencode) (anomalyco/opencode, TypeScript 版)
- GLM API Key（[智谱开放平台](https://open.bigmodel.cn/)获取）
