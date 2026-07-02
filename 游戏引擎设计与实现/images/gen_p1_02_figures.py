# -*- coding: utf-8 -*-
"""为《游戏引擎》P1-02 生成一组 PNG 示意图。
图内英文标注, 正文中文解释。运行: python gen_p1_02_figures.py
配色与 rcParams 与 gen_p0_figures.py 保持一致。
"""
import os
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, FancyArrowPatch, FancyBboxPatch

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


def clean_ax(ax):
    for s in ("top", "right", "bottom", "left"):
        ax.spines[s].set_visible(False)
    ax.set_xticks([]); ax.set_yticks([])


def box(ax, x, y, w, h, text, fc=SOFT, ec=INK, fs=12, weight="normal", tc=INK):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.02,rounding_size=0.08",
                                facecolor=fc, edgecolor=ec, linewidth=1.4))
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center",
            fontsize=fs, weight=weight, color=tc)


def arrow(ax, p1, p2, color=INK, lw=1.6, style="-|>", rad=0.0):
    cs = f"arc3,rad={rad}" if rad else "arc3,rad=0"
    ax.add_artist(FancyArrowPatch(p1, p2, arrowstyle=style, color=color,
                                  lw=lw, connectionstyle=cs, mutation_scale=16))


# ---------------------------------------------------------------- fig-01
# 一帧 16ms 预算分配条形图 + CPU/GPU 并行示意
def fig_frame_budget():
    fig, ax = plt.subplots(figsize=(11.5, 5.6))

    # ---- 上半: 一帧 16ms 的 CPU 时间条 (按段堆叠)
    segs = [
        ("Input",       0.5, GREY),
        ("Update",      8.5, BLUE),    # 最重, 本书主战场
        ("Render(CPU)", 6.0, GREEN),   # 准备 draw call + 等 GPU
        ("Swap/VSync",  1.5, AMBER),   # 同步
    ]
    bar_y = 4.2
    bar_h = 0.9
    x = 0.6
    total = sum(v for _, v, _ in segs)
    for name, val, col in segs:
        w = val
        ax.add_patch(Rectangle((x, bar_y), w, bar_h, facecolor=col,
                               edgecolor="white", linewidth=1.5))
        # 段内标注
        ax.text(x + w / 2, bar_y + bar_h / 2, f"{name}\n{val}ms",
                ha="center", va="center", fontsize=10.5,
                color="white" if col != GREY else INK, weight="bold")
        x += w
    # 16ms 预算线
    ax.plot([0.6, 16.6], [bar_y - 0.25, bar_y - 0.25], color=INK, lw=1.4)
    ax.plot([0.6, 0.6], [bar_y - 0.35, bar_y - 0.15], color=INK, lw=1.4)
    ax.plot([16.6, 16.6], [bar_y - 0.35, bar_y - 0.15], color=INK, lw=1.4)
    ax.text(8.6, bar_y - 0.6, "16.67ms frame budget @ 60 FPS",
            ha="center", fontsize=12, weight="bold", color=INK)
    ax.text(0.6, bar_y + bar_h + 0.35,
            "CPU side: where the 16ms goes",
            fontsize=13, weight="bold", color=INK)

    # 标注 update 是主战场
    ax.annotate("Update is the biggest chunk\n=> this book's main battleground (ECS / data-oriented)",
                xy=(5.5, bar_y + bar_h), xytext=(8.5, 6.2),
                fontsize=10.5, color=RED,
                arrowprops=dict(arrowstyle="->", color=RED, lw=1.3))

    # ---- 下半: GPU 那边并行画
    gpu_y = 1.8
    gpu_h = 0.9
    ax.add_patch(Rectangle((1.1, gpu_y), 13.0, gpu_h, facecolor=GREEN,
                           edgecolor="white", linewidth=1.5, alpha=0.55))
    ax.text(1.1 + 6.5, gpu_y + gpu_h / 2,
            "GPU: rasterizing / shading this frame (runs in parallel with CPU)",
            ha="center", va="center", fontsize=10.5, color=INK, weight="bold")
    ax.text(0.6, gpu_y + gpu_h + 0.25, "GPU side:",
            fontsize=13, weight="bold", color=GREEN)

    # 并行说明: frame total = max(CPU, GPU)
    ax.text(8.6, 0.5,
            "frame total  =  max(CPU time, GPU time)   -- they run in parallel",
            ha="center", fontsize=11.5, color=INK, weight="bold")

    ax.set_xlim(0, 17.2)
    ax.set_ylim(0, 7.4)
    ax.set_aspect("equal")
    clean_ax(ax)
    fig.savefig(os.path.join(OUT, "fig-p1_02_02-frame-budget.png"))
    plt.close(fig)


# ---------------------------------------------------------------- fig-02
# 可变帧率陷阱: 同一份 "每帧移动 5 像素" 在 60FPS / 30FPS 下位移随时间
def fig_variable_framerate():
    fig, axes = plt.subplots(1, 2, figsize=(13, 5.4))

    # ---- 左: 位移随时间
    ax = axes[0]
    t = np.linspace(0, 2.0, 400)
    # 60 FPS: 每帧 1/60s 移动 5px => 300 px/s
    # 30 FPS: 每帧 1/30s 移动 5px => 150 px/s
    pos_60 = 300 * t
    pos_30 = 150 * t
    ax.plot(t, pos_60, color=GREEN, lw=2.6, label="60 FPS  (fast machine)")
    ax.plot(t, pos_30, color=RED,   lw=2.6, label="30 FPS  (slow machine)")
    # 标 2 秒处的位移差
    ax.scatter([2.0, 2.0], [600, 300], color=[GREEN, RED], zorder=5, s=55)
    ax.annotate("600 px after 2s", xy=(2.0, 600), xytext=(1.05, 615),
                fontsize=10.5, color=GREEN, weight="bold")
    ax.annotate("only 300 px after 2s", xy=(2.0, 300), xytext=(0.75, 340),
                fontsize=10.5, color=RED, weight="bold")
    # 双向箭头标差距
    ax.annotate("", xy=(1.92, 600), xytext=(1.92, 300),
                arrowprops=dict(arrowstyle="<->", color=AMBER, lw=2.0))
    ax.text(1.55, 450, "2x speed\n difference!", fontsize=11,
            color=AMBER, weight="bold", ha="center")

    ax.set_xlabel("time (seconds)", fontsize=12)
    ax.set_ylabel("ball position (pixels)", fontsize=12)
    ax.set_title("Same code: 'move 5px per frame'\n=> different speed on different machines",
                 fontsize=12.5, weight="bold", color=INK)
    ax.set_xlim(0, 2.1); ax.set_ylim(0, 680)
    ax.grid(True, alpha=0.3)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.legend(loc="upper left", fontsize=10.5, frameon=False)

    # ---- 右: 用 delta time 修复后, 两条曲线重合
    ax = axes[1]
    pos_fixed = 300 * t   # 不管帧率, 每秒都 300px
    ax.plot(t, pos_fixed, color=BLUE, lw=2.8, label="fixed: speed * dt")
    ax.plot(t, pos_60,    color=GREEN, lw=1.4, ls="--", alpha=0.6,
            label="60 FPS (overlaps)")
    ax.plot(t, pos_30,    color=RED,   lw=1.4, ls="--", alpha=0.6,
            label="30 FPS (overlaps)")
    ax.scatter([2.0], [600], color=BLUE, zorder=5, s=60)
    ax.annotate("both reach 600 px in 2s\n=> speed decoupled from framerate",
                xy=(2.0, 600), xytext=(0.35, 420),
                fontsize=11, color=BLUE, weight="bold",
                arrowprops=dict(arrowstyle="->", color=BLUE, lw=1.4))

    ax.set_xlabel("time (seconds)", fontsize=12)
    ax.set_ylabel("ball position (pixels)", fontsize=12)
    ax.set_title("Fixed: 'speed_per_second * dt'\n=> same speed on any machine",
                 fontsize=12.5, weight="bold", color=INK)
    ax.set_xlim(0, 2.1); ax.set_ylim(0, 680)
    ax.grid(True, alpha=0.3)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.legend(loc="upper left", fontsize=10.5, frameon=False)

    fig.suptitle("Variable-framerate trap  --  and how delta time fixes it",
                 fontsize=13.5, weight="bold", color=INK, y=1.02)
    fig.savefig(os.path.join(OUT, "fig-p1_02_03-variable-framerate.png"))
    plt.close(fig)


# ---------------------------------------------------------------- fig-03
# 三段式时序: 两帧的 input/update/render + swap (作为 mermaid 的补充可视化)
def fig_three_phase_timeline():
    fig, ax = plt.subplots(figsize=(12.5, 4.8))

    bar_h = 0.9
    y_frame1 = 3.2
    y_frame2 = 1.4
    # 第 N 帧三段
    segs1 = [("Input", 0.5, GREY), ("Update", 8.5, BLUE),
             ("Render", 6.0, GREEN), ("Swap", 1.5, AMBER)]
    segs2 = [("Input", 0.5, GREY), ("Update", 8.5, BLUE),
             ("Render", 6.0, GREEN), ("Swap", 1.5, AMBER)]

    def draw_segs(segs, y, label):
        x = 0.6
        for name, val, col in segs:
            ax.add_patch(Rectangle((x, y), val, bar_h, facecolor=col,
                                   edgecolor="white", linewidth=1.5))
            ax.text(x + val / 2, y + bar_h / 2, name, ha="center", va="center",
                    fontsize=10.5, color="white" if col != GREY else INK,
                    weight="bold")
            x += val
        ax.text(0.1, y + bar_h / 2, label, fontsize=11.5, weight="bold",
                color=INK, ha="right", va="center")
        return x  # 末端 x

    end1 = draw_segs(segs1, y_frame1, "Frame N")
    # 帧之间箭头
    arrow(ax, (end1 + 0.05, y_frame1 + bar_h / 2),
          (0.55, y_frame2 + bar_h / 2), color=INK, lw=1.6, rad=-0.18)
    end2 = draw_segs(segs2, y_frame2, "Frame N+1")

    # 顺序标注: 标在每个段的中点上方, 避免挤在一起
    seg_starts = []
    _x = 0.6
    for name, val, col in segs1:
        seg_starts.append((_x + val / 2, name, col))
        _x += val
    phase_labels = {"Input": ("1. Input", DGREY),
                    "Update": ("2. Update (heaviest)", BLUE),
                    "Render": ("3. Render", GREEN),
                    "Swap": ("swap", AMBER)}
    for cx, name, col in seg_starts:
        label, lc = phase_labels[name]
        ax.text(cx, y_frame1 + bar_h + 0.55, label,
                fontsize=11, color=lc, weight="bold", ha="center")

    # 16ms 预算标尺 (frame1 下方)
    ax.annotate("", xy=(0.6, y_frame1 - 0.25), xytext=(16.6, y_frame1 - 0.25),
                arrowprops=dict(arrowstyle="<->", color=INK, lw=1.3))
    ax.text(8.6, y_frame1 - 0.55, "16.67 ms  (must finish or frame drops)",
            ha="center", fontsize=11, color=INK, weight="bold")

    ax.set_xlim(-1.2, 18.0)
    ax.set_ylim(0.4, 5.0)
    ax.set_aspect("equal")
    clean_ax(ax)
    fig.savefig(os.path.join(OUT, "fig-p1_02_01-three-phase-timeline.png"))
    plt.close(fig)


# ---------------------------------------------------------------- fig-04
# 主循环三段式结构 (静态结构图, 配 mermaid 时序图互补)
def fig_main_loop_structure():
    fig, ax = plt.subplots(figsize=(12, 5.0))

    # while 框
    ax.add_patch(FancyBboxPatch((0.3, 0.5), 11.4, 4.0,
                                boxstyle="round,pad=0.05,rounding_size=0.15",
                                facecolor=(0.96, 0.97, 0.99),
                                edgecolor=INK, linewidth=1.8, linestyle="--"))
    ax.text(0.55, 4.15, "while (running)  {  ...  }",
            fontsize=13, weight="bold", color=INK, family="monospace")

    # 三段 box
    box(ax, 1.0, 2.2, 2.6, 1.3, "① Input\nread keys / pad\n(cheap)",
        fc=(0.92, 0.92, 0.94), ec=INK, fs=11.5, weight="bold")
    box(ax, 4.0, 2.2, 3.4, 1.3, "② Update\nmove / physics /\nAI / animation\n(HEAVIEST)",
        fc=(0.86, 0.92, 1.0), ec=BLUE, fs=11.5, weight="bold", tc=BLUE)
    box(ax, 7.8, 2.2, 2.6, 1.3, "③ Render\ndraw one frame\n(submit to GPU)",
        fc=(0.88, 0.95, 0.88), ec=GREEN, fs=11.5, weight="bold", tc=GREEN)

    # 顺序箭头
    arrow(ax, (3.6, 2.85), (4.0, 2.85), color=INK, lw=2.0)
    arrow(ax, (7.4, 2.85), (7.8, 2.85), color=INK, lw=2.0)
    # 回到开头 (下一帧) -- 用负曲率让弧线从三个 box 下方绕回去, 不穿过 Update box
    arrow(ax, (9.1, 2.2), (2.3, 2.2), color=AMBER, lw=1.8, rad=-0.35)
    ax.text(5.7, 0.95, "next frame  (swap buffers at the loop boundary)",
            ha="center", fontsize=11, color=AMBER, weight="bold")

    # 标注顺序不可换
    ax.annotate("order is a hard constraint:\nnewest input -> newest state -> newest picture",
                xy=(5.7, 2.2), xytext=(5.7, 0.7), fontsize=10.5, color=RED,
                ha="center", weight="bold",
                arrowprops=dict(arrowstyle="->", color=RED, lw=1.2))

    ax.set_xlim(0, 12)
    ax.set_ylim(0, 5.0)
    ax.set_aspect("equal")
    clean_ax(ax)
    fig.savefig(os.path.join(OUT, "fig-p1_02_04-main-loop-structure.png"))
    plt.close(fig)


if __name__ == "__main__":
    fig_three_phase_timeline()
    fig_frame_budget()
    fig_variable_framerate()
    fig_main_loop_structure()
    print("done:", sorted(f for f in os.listdir(OUT) if f.startswith("fig-p1_02_") and f.endswith(".png")))
