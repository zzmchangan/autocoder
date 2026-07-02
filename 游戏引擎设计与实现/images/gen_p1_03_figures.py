# -*- coding: utf-8 -*-
"""为《游戏引擎》P1-03 (引擎的子系统全景) 生成一组 PNG 示意图。
图内英文标注, 正文中文解释。运行: python gen_p1_03_figures.py
配色沿用全书固定: RED/GREEN/BLUE/AMBER/GREY。
"""
import os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, FancyArrowPatch, FancyBboxPatch, Circle

plt.rcParams.update({
    "font.family": "DejaVu Sans",
    "font.size": 12.5,
    "axes.linewidth": 1.2,
    "figure.dpi": 150,
    "savefig.dpi": 150,
    "savefig.bbox": "tight",
    "savefig.facecolor": "white",
    "savefig.pad_inches": 0.15,
})

OUT = os.path.dirname(os.path.abspath(__file__))

RED   = (0.86, 0.18, 0.18)
GREEN = (0.20, 0.70, 0.32)
BLUE  = (0.20, 0.45, 0.88)
AMBER = (0.95, 0.70, 0.18)
GREY  = (0.78, 0.78, 0.80)
DGREY = (0.62, 0.62, 0.66)
INK   = (0.12, 0.12, 0.12)
SOFT  = (0.95, 0.95, 0.97)
PINK  = (0.97, 0.86, 0.88)
LBLUE = (0.90, 0.94, 1.0)
LGREEN= (0.90, 0.95, 0.90)
LAMBER= (1.0, 0.93, 0.80)


def clean_ax(ax):
    for s in ("top", "right", "bottom", "left"):
        ax.spines[s].set_visible(False)
    ax.set_xticks([]); ax.set_yticks([])


def box(ax, x, y, w, h, text, fc=SOFT, ec=INK, fs=12, weight="normal", tc=INK, lw=1.4):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.02,rounding_size=0.08",
                                facecolor=fc, edgecolor=ec, linewidth=lw))
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center",
            fontsize=fs, weight=weight, color=tc)


def arrow(ax, p1, p2, color=INK, lw=1.6, style="-|>", rad=0.0):
    cs = f"arc3,rad={rad}" if rad else "arc3,rad=0"
    ax.add_artist(FancyArrowPatch(p1, p2, arrowstyle=style, color=color,
                                  lw=lw, connectionstyle=cs, mutation_scale=16))


# ============================================================
# fig-p1_03_01 : 一帧时间线 + 各子系统落在哪段
# 一条横轴 = 16ms 一帧, input / fixed-update(sim) / update / render
# 四段, 把 7 个子系统标进对应段。
# ============================================================
def fig_frame_timeline():
    fig, ax = plt.subplots(figsize=(13.5, 6.8))

    # 主时间轴
    y_axis = 3.6
    seg = [
        ("INPUT",     0.2, 1.5, LBLUE,   "Input\n(keyboard / mouse / gamepad)"),
        ("FIXED UPDATE\n(physics sim)", 1.8, 5.0, LGREEN, "Physics  /  AI  /  game rules\n(fixed timestep, runs 0..N times)"),
        ("UPDATE",    6.9, 3.0, LAMBER,  "Scripts / Animation\n(variable timestep)"),
        ("RENDER",   10.0, 3.6, PINK,    "Render submit\n(extract -> prepare -> queue)"),
    ]
    for name, x0, w, fc, sub in seg:
        ax.add_patch(FancyBboxPatch((x0, y_axis), w, 1.5,
                     boxstyle="round,pad=0.02,rounding_size=0.06",
                     facecolor=fc, edgecolor=INK, linewidth=1.5))
        ax.text(x0 + w / 2, y_axis + 1.12, name, ha="center", va="center",
                fontsize=11.5, weight="bold", color=INK)
        ax.text(x0 + w / 2, y_axis + 0.45, sub, ha="center", va="center",
                fontsize=9.2, color=DGREY)

    # 横轴底线 + 帧边界
    ax.annotate("", xy=(13.8, y_axis - 0.35), xytext=(0.0, y_axis - 0.35),
                arrowprops=dict(arrowstyle="-|>", color=INK, lw=1.8))
    ax.text(0.0, y_axis - 0.75, "frame start", fontsize=10, color=DGREY)
    ax.text(13.7, y_axis - 0.75, "frame end (16 ms @ 60 FPS)", fontsize=10,
            color=DGREY, ha="right")

    # 段间分隔虚线
    for xb in (1.65, 6.75, 9.85):
        ax.plot([xb, xb], [y_axis - 0.2, y_axis + 1.7], color=DGREY,
                linewidth=1.0, linestyle="--")

    # 上方: 7 个子系统标签 + 落在哪段(小色块)
    sub_y = 6.2
    ax.text(6.9, 7.15, "7 subsystems and where they run in one frame",
            ha="center", fontsize=13, weight="bold", color=INK)
    subs = [
        ("Input",    1.0,  LBLUE),
        ("Physics",  4.3,  LGREEN),
        ("Scripts",  8.4,  LAMBER),
        ("Animation",8.9,  LAMBER),
        ("Audio",    6.2,  LGREEN),  # 事件驱动, 多在 fixed/update 收尾
        ("Assets",   3.0,  GREY),    # 异步, 不在主路径(虚框)
        ("Render",  11.8,  PINK),
    ]
    for name, x, col in subs:
        is_async = (name == "Assets")
            # 异步资源用虚框
        if is_async:
            ax.add_patch(Rectangle((x - 0.55, sub_y - 0.05), 1.1, 0.55,
                         facecolor="white", edgecolor=DGREY, linewidth=1.4,
                         linestyle="--"))
            ax.text(x, sub_y + 0.22, name, ha="center", va="center",
                    fontsize=9.8, color=DGREY)
            ax.text(x, sub_y - 0.30, "async", ha="center", va="center",
                    fontsize=7.8, color=DGREY, style="italic")
        else:
            ax.add_patch(FancyBboxPatch((x - 0.6, sub_y - 0.05), 1.2, 0.55,
                         boxstyle="round,pad=0.01,rounding_size=0.04",
                         facecolor=col, edgecolor=INK, linewidth=1.1))
            ax.text(x, sub_y + 0.22, name, ha="center", va="center",
                    fontsize=9.8, color=INK)

    # 引线: 子系统色块 -> 对应段
    def link(x_top, x_bot, col):
        ax.plot([x_top, x_top], [sub_y - 0.05, y_axis + 1.65], color=col,
                linewidth=1.0, alpha=0.0)  # 占位, 不画显眼线, 靠颜色对应

    # 额外说明: 事件驱动
    ax.text(6.9, 1.6, "audio & network are EVENT-DRIVEN (not on the main hot path)",
            ha="center", fontsize=10.5, color=BLUE, style="italic")
    ax.text(6.9, 1.05, "assets load ASYNC on I/O threads (never block the 16 ms budget)",
            ha="center", fontsize=10.5, color=DGREY, style="italic")

    # 固定步长 multiple 注记
    ax.annotate("fixed step may run\n0 / 1 / 2 ... times per frame",
                xy=(4.3, y_axis), xytext=(4.3, 2.35), fontsize=9.5, color=GREEN,
                ha="center",
                arrowprops=dict(arrowstyle="->", color=GREEN, lw=1.2))

    ax.set_xlim(-0.3, 14.0)
    ax.set_ylim(0.5, 7.6)
    ax.set_aspect("equal")
    clean_ax(ax)
    fig.savefig(os.path.join(OUT, "fig-p1_03_01-frame-timeline.png"))
    plt.close(fig)


# ============================================================
# fig-p1_03_02 : 子系统依赖顺序 (谁先谁后, 为什么)
# 输入 -> 逻辑/脚本 -> 动画 -> 物理 -> 音频 -> 渲染
# ============================================================
def fig_dependency_order():
    fig, ax = plt.subplots(figsize=(13.5, 5.6))

    stages = [
        ("1. INPUT",       "read keyboard / mouse /\ngamepad this frame",   LBLUE),
        ("2. GAME LOGIC",  "scripts / AI set\nvelocity & intent",           LAMBER),
        ("3. ANIMATION",   "advance clips, blend,\ncompute bone poses",     LGREEN),
        ("4. PHYSICS",     "integrate velocity,\nresolve collisions",       GREEN),
        ("5. AUDIO",       "trigger / position\nsound events",              LGREEN),
        ("6. RENDER",      "submit draw calls,\nGPU draws the frame",       PINK),
    ]
    n = len(stages)
    x0 = 0.3
    w = 2.05
    gap = 0.18
    y = 2.2
    h = 1.7
    centers = []
    for i, (title, sub, col) in enumerate(stages):
        x = x0 + i * (w + gap)
        box(ax, x, y, w, h, "", fc=col, lw=1.5)
        ax.text(x + w / 2, y + h - 0.38, title, ha="center", va="center",
                fontsize=11, weight="bold", color=INK)
        ax.text(x + w / 2, y + 0.55, sub, ha="center", va="center",
                fontsize=9.2, color=DGREY)
        centers.append((x, x + w))

    # 段间箭头
    for i in range(n - 1):
        a = (centers[i][1], y + h / 2)
        b = (centers[i + 1][0], y + h / 2)
        arrow(ax, a, b, color=INK, lw=1.8)

    # 上方原因注解 (为什么是这个顺序)
    notes = [
        ("logic reads\nfresh input", 0.5, BLUE),
        ("physics needs\nfinal velocity", 4.6, GREEN),
        ("render sees the\nsettled world", 10.6, RED),
    ]
    for txt, xb, col in notes:
        ax.annotate(txt, xy=(xb + 1.0, y + h), xytext=(xb + 1.0, y + h + 0.95),
                    fontsize=9.3, color=col, ha="center", weight="bold",
                    arrowprops=dict(arrowstyle="->", color=col, lw=1.1))

    ax.text(6.9, 0.9, "order is a DEPENDENCY chain: each stage consumes what the previous produced",
            ha="center", fontsize=11, color=INK, weight="bold")
    ax.text(6.9, 0.4, "(swap two => glitch: stale input, tunneling, drawing the old frame)",
            ha="center", fontsize=9.5, color=DGREY, style="italic")

    ax.set_xlim(0, 13.8)
    ax.set_ylim(0, 5.4)
    ax.set_aspect("equal")
    clean_ax(ax)
    fig.savefig(os.path.join(OUT, "fig-p1_03_02-dependency-order.png"))
    plt.close(fig)


# ============================================================
# fig-p1_03_03 : Bevy Main schedule 的子系统排布对照
# 把 Bevy 真实的 MainScheduleOrder 画出来, 标注各子系统落在哪个 set
# ============================================================
def fig_bevy_schedule():
    fig, ax = plt.subplots(figsize=(13.5, 7.6))

    # 顶部: app.update() 循环
    box(ax, 5.4, 9.6, 3.0, 0.7, "App::update()  (one frame)", fc=INK, tc="white",
        weight="bold")
    ax.annotate("", xy=(6.9, 9.6), xytext=(6.9, 9.15),
                arrowprops=dict(arrowstyle="-|>", color=INK, lw=1.6))

    # Main schedule 横条
    ax.text(0.2, 8.55, "Main schedule  (single-threaded executor, just dispatches sub-schedules)",
            fontsize=11.5, weight="bold", color=INK)

    sub = [
        ("First",            0.2, 1.5, SOFT,  "message flush"),
        ("PreUpdate",        1.8, 1.7, LBLUE, "INPUT\nraw OS events ->\nButtonInput<KeyCode>"),
        ("RunFixedMainLoop", 3.6, 2.0, LGREEN,"PHYSICS / AI /\ngame rules\n(fixed step x N)"),
        ("Update",           5.7, 1.8, LAMBER,"SCRIPTS / UI /\nAUDIO control\n(variable step)"),
        ("SpawnScene",       7.6, 1.4, SOFT,  "spawn scenes"),
        ("PostUpdate",       9.1, 1.8, LGREEN,"ANIMATION\ntransform\npropagation"),
        ("Last",            11.0, 1.3, SOFT,  "cleanup"),
    ]
    y = 6.7
    h = 1.5
    for name, x, w, col, note in sub:
        box(ax, x, y, w, h, "", fc=col, lw=1.3)
        ax.text(x + w / 2, y + h - 0.28, name, ha="center", va="center",
                fontsize=10.2, weight="bold", color=INK)
        ax.text(x + w / 2, y + 0.55, note, ha="center", va="center",
                fontsize=7.8, color=INK)
    # 顺序箭头
    for i in range(len(sub) - 1):
        x1 = sub[i][1] + sub[i][2]
        x2 = sub[i + 1][1]
        arrow(ax, (x1, y + h / 2), (x2, y + h / 2), color=DGREY, lw=1.2)

    # 下方: FixedMain 内部 (放大 RunFixedMainLoop)
    ax.text(0.2, 5.55, "FixedMain schedule  (inside RunFixedMainLoop, runs 0..N times per frame)",
            fontsize=11, weight="bold", color=GREEN)
    fixed = [
        ("FixedFirst",     0.2, 1.7, SOFT),
        ("FixedPreUpdate", 2.0, 1.9, SOFT),
        ("FixedUpdate",    4.0, 2.0, GREEN),
        ("FixedPostUpdate",6.1, 1.9, SOFT),
        ("FixedLast",      8.1, 1.5, SOFT),
    ]
    fy = 4.0
    fh = 1.0
    for name, x, w, col in fixed:
        box(ax, x, fy, w, fh, name, fc=col, fs=9.5)
    for i in range(len(fixed) - 1):
        x1 = fixed[i][1] + fixed[i][2]
        x2 = fixed[i + 1][1]
        arrow(ax, (x1, fy + fh / 2), (x2, fy + fh / 2), color=DGREY, lw=1.1)
    ax.annotate("physics + AI +\nnetworking live here",
                xy=(5.0, fy + fh), xytext=(5.0, fy + fh + 0.75),
                fontsize=9, color=GREEN, ha="center", weight="bold",
                arrowprops=dict(arrowstyle="->", color=GREEN, lw=1.1))

    # 最下: Render 是独立 SubApp (不在 Main)
    ax.text(0.2, 2.75, "RENDER = separate SubApp on its own OS thread (NOT in Main schedule)",
            fontsize=11.5, weight="bold", color=RED)
    box(ax, 0.4, 1.5, 3.3, 0.9, "Main thread\n(simulation)", fc=LBLUE, fs=10)
    box(ax, 9.0, 1.5, 3.6, 0.9, "Render thread\n(extract -> prepare -> queue)",
        fc=PINK, fs=10)
    # 双向 channel
    arrow(ax, (3.7, 1.95), (9.0, 1.95), color=AMBER, lw=1.8, rad=-0.18)
    arrow(ax, (9.0, 2.15), (3.7, 2.15), color=AMBER, lw=1.8, rad=-0.18)
    ax.text(6.35, 2.78, "bounded async channel", ha="center", fontsize=9,
            color=AMBER, weight="bold")
    ax.text(6.35, 1.15, "frame N render  ||  frame N+1 simulation  (pipelined)",
            ha="center", fontsize=9.2, color=RED, style="italic")

    ax.set_xlim(0, 13.5)
    ax.set_ylim(0.4, 10.6)
    ax.set_aspect("equal")
    clean_ax(ax)
    fig.savefig(os.path.join(OUT, "fig-p1_03_03-bevy-schedule.png"))
    plt.close(fig)


# ============================================================
# fig-p1_03_04 : 三类子系统驱动模式 (主路径 / 异步 / 事件驱动)
# ============================================================
def fig_three_patterns():
    fig, axes = plt.subplots(1, 3, figsize=(14.5, 5.2))

    titles = [
        ("A. ON THE MAIN PATH\n(render / physics / scripts)", BLUE),
        ("B. ASYNC BACKGROUND\n(assets)", GREEN),
        ("C. EVENT-DRIVEN\n(audio / network)", AMBER),
    ]
    for ax, (t, col) in zip(axes, titles):
        ax.set_title(t, fontsize=11.5, weight="bold", color=col)

    # ---- A: 每帧主路径 ----
    ax = axes[0]
    for i in range(5):
        y = 4.2 - i * 0.8
        ax.add_patch(Rectangle((0.6, y), 4.0, 0.55, facecolor=LBLUE,
                     edgecolor=INK, linewidth=1.1))
        ax.text(2.6, y + 0.27, f"frame {i}", ha="center", va="center",
                fontsize=9, color=INK)
        # 子系统在每个 frame 内
        ax.add_patch(Rectangle((1.6, y + 0.10), 0.6, 0.35, facecolor=RED))
        ax.add_patch(Rectangle((2.4, y + 0.10), 0.9, 0.35, facecolor=GREEN))
        ax.add_patch(Rectangle((3.5, y + 0.10), 0.7, 0.35, facecolor=AMBER))
        if i == 0:
            ax.text(1.9, y + 0.78, "phys", fontsize=7, color="white", ha="center")
            ax.text(2.85, y + 0.78, "scripts", fontsize=7, color="white", ha="center")
            ax.text(3.85, y + 0.78, "render", fontsize=7, color="white", ha="center")
    ax.text(2.6, 0.5, "runs EVERY frame\ninside the 16 ms budget",
            ha="center", fontsize=10, color=BLUE, weight="bold")
    ax.set_xlim(0, 5.2); ax.set_ylim(0, 5.0)
    ax.set_aspect("equal"); clean_ax(ax)

    # ---- B: 异步后台 ----
    ax = axes[1]
    # 主线程帧流(顶部)
    for i in range(5):
        x = 0.4 + i * 1.0
        ax.add_patch(Rectangle((x, 3.8), 0.8, 0.5, facecolor=LBLUE,
                     edgecolor=INK, linewidth=1.0))
    ax.text(2.9, 4.55, "main thread frames", ha="center", fontsize=9, color=INK)
    # 后台 I/O 线程
    ax.add_patch(FancyBboxPatch((0.4, 1.4), 4.6, 1.1,
                 boxstyle="round,pad=0.02,rounding_size=0.06",
                 facecolor=LGREEN, edgecolor=GREEN, linewidth=1.5))
    ax.text(2.7, 2.15, "I/O thread pool\nloads texture / mesh / audio",
            ha="center", va="center", fontsize=9.5, color=INK)
    # Handle 即时返回
    ax.annotate("AssetServer::load()\n=> returns Handle instantly",
                xy=(1.0, 3.8), xytext=(1.0, 3.05), fontsize=8.5, color=GREEN,
                ha="center", weight="bold",
                arrowprops=dict(arrowstyle="->", color=GREEN, lw=1.1))
    # 完成回调
    ax.annotate("loaded -> AssetEvent\n(posted into main schedule)",
                xy=(4.2, 2.5), xytext=(4.2, 3.2), fontsize=8.5, color=GREEN,
                ha="center", weight="bold",
                arrowprops=dict(arrowstyle="->", color=GREEN, lw=1.1))
    ax.text(2.7, 0.6, "NEVER blocks the frame;\nresult arrives in a later frame",
            ha="center", fontsize=10, color=GREEN, weight="bold")
    ax.set_xlim(0, 5.2); ax.set_ylim(0, 5.0)
    ax.set_aspect("equal"); clean_ax(ax)

    # ---- C: 事件驱动 ----
    ax = axes[2]
    # 事件队列
    ax.add_patch(FancyBboxPatch((0.4, 1.8), 4.6, 1.4,
                 boxstyle="round,pad=0.02,rounding_size=0.06",
                 facecolor=LAMBER, edgecolor=AMBER, linewidth=1.5))
    ax.text(2.7, 2.95, "event queue", ha="center", fontsize=9.5,
            color=INK, weight="bold")
    # 队列里几个事件
    for i, (t, c) in enumerate([("play SFX", RED), ("net pkt", BLUE), ("play SFX", RED)]):
        ax.add_patch(Rectangle((0.8 + i * 1.45, 2.05), 1.25, 0.55,
                     facecolor=c, edgecolor="white", linewidth=1.0))
        ax.text(0.8 + i * 1.45 + 0.62, 2.32, t, ha="center", va="center",
                fontsize=7.8, color="white")
    # 生产者(帧内触发)
    ax.text(2.7, 4.3, "scripts / physics\npush events during the frame",
            ha="center", fontsize=9, color=INK)
    arrow(ax, (2.7, 3.95), (2.7, 3.25), color=INK, lw=1.4)
    # 消费者(下一帧处理)
    ax.text(2.7, 1.0, "audio / net system\nDRAINS the queue later",
            ha="center", fontsize=9, color=AMBER, weight="bold")
    arrow(ax, (2.7, 1.75), (2.7, 1.35), color=AMBER, lw=1.4)
    ax.text(2.7, 0.4, "producers and consumers\nare DECOUPLED",
            ha="center", fontsize=9.5, color=INK, style="italic")
    ax.set_xlim(0, 5.2); ax.set_ylim(0, 5.0)
    ax.set_aspect("equal"); clean_ax(ax)

    fig.savefig(os.path.join(OUT, "fig-p1_03_04-three-patterns.png"))
    plt.close(fig)


if __name__ == "__main__":
    fig_frame_timeline()
    fig_dependency_order()
    fig_bevy_schedule()
    fig_three_patterns()
    print("done:", sorted(f for f in os.listdir(OUT) if f.startswith("fig-p1_03")))
