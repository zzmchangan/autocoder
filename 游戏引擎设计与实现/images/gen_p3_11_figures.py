# -*- coding: utf-8 -*-
"""为《游戏引擎》P3-11《delta time 与帧率》生成一组 PNG 示意图。
图内英文标注, 正文中文解释。运行: python gen_p3_11_figures.py
配色与 rcParams 与 gen_p0/gen_p1_02 保持一致。
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
PURP  = (0.55, 0.30, 0.78)


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
# delta time 计算时序: 两/三帧时间戳之差
def fig_delta_time_timing():
    fig, ax = plt.subplots(figsize=(13, 5.4))

    # 时间轴
    y_axis = 1.0
    ax.annotate("", xy=(12.6, y_axis), xytext=(0.4, y_axis),
                arrowprops=dict(arrowstyle="->", color=INK, lw=1.6))
    ax.text(12.7, y_axis, "monotonic clock\n(Instant::now)",
            fontsize=10.5, color=INK, va="center")

    # 三帧的时间戳
    t_stamps = [(1.5, "t0", "frame N start"),
                (5.0, "t1", "frame N+1 start"),
                (9.2, "t2", "frame N+2 start")]
    for tx, lab, desc in t_stamps:
        ax.plot([tx, tx], [y_axis - 0.12, y_axis + 0.12], color=INK, lw=2.0)
        ax.text(tx, y_axis + 0.22, lab, ha="center", fontsize=12,
                weight="bold", color=INK, family="monospace")
        ax.text(tx, y_axis + 0.55, desc, ha="center", fontsize=10.5, color=DGREY)

    # dt0 = t1 - t0
    y_dt = -0.6
    ax.annotate("", xy=(5.0, y_dt), xytext=(1.5, y_dt),
                arrowprops=dict(arrowstyle="<->", color=BLUE, lw=2.2))
    ax.text(3.25, y_dt - 0.28, "delta_time = t1 - t0\n(measured at start of frame N+1)",
            ha="center", fontsize=11, color=BLUE, weight="bold")
    # 标 dt 数值
    ax.text(3.25, y_dt + 0.28, "16.7 ms", ha="center", fontsize=12,
            color=BLUE, weight="bold")

    # dt1 = t2 - t1  (这一帧变长了, 模拟卡顿)
    ax.annotate("", xy=(9.2, y_dt), xytext=(5.0, y_dt),
                arrowprops=dict(arrowstyle="<->", color=RED, lw=2.2))
    ax.text(7.1, y_dt - 0.28, "delta_time = t2 - t1\n(this frame took longer)",
            ha="center", fontsize=11, color=RED, weight="bold")
    ax.text(7.1, y_dt + 0.28, "22.1 ms", ha="center", fontsize=12,
            color=RED, weight="bold")

    # 帧的工作区间 (用色块表示 update+render)
    frame_segs = [
        (1.5, 3.5, "frame N\n(update+render)", BLUE),
        (5.0, 4.2, "frame N+1\n(update+render)", GREEN),
    ]
    fy = 2.2
    for fx, fw, flab, fc in frame_segs:
        ax.add_patch(Rectangle((fx, fy), fw, 0.7, facecolor=fc,
                               edgecolor="white", linewidth=1.5, alpha=0.75))
        ax.text(fx + fw / 2, fy + 0.35, flab, ha="center", va="center",
                fontsize=10.5, color="white", weight="bold")
    ax.text(0.4, fy + 0.35, "work:", fontsize=11, weight="bold", color=INK,
            ha="left", va="center")

    # 关键点: monotonic (never goes back), saturating
    ax.text(6.5, -1.55,
            "key points:  (1) monotonic clock -- never goes backwards  "
            "(2) saturating subtraction -- never negative",
            ha="center", fontsize=10.5, color=INK, style="italic")

    ax.set_xlim(0, 13.6)
    ax.set_ylim(-2.0, 3.2)
    ax.set_aspect("equal")
    clean_ax(ax)
    fig.savefig(os.path.join(OUT, "fig-p3_11_01-delta-time-timing.png"))
    plt.close(fig)


# ---------------------------------------------------------------- fig-02
# 帧率独立动画曲线: 数值模拟, 三种 FPS 下位移随时间
def fig_framerate_independent():
    fig, axes = plt.subplots(1, 2, figsize=(13.5, 5.6))

    # ---- 左: 错误做法 "每帧固定 5 像素", 不同 FPS 速度不同
    ax = axes[0]
    fps_list = [(60, GREEN), (30, RED), (144, BLUE)]
    t = np.linspace(0, 2.0, 400)
    # 每帧固定 5 像素 => 速度 = 5 * FPS px/s
    for fps, col in fps_list:
        speed = 5 * fps          # px/s
        pos = speed * t
        ax.plot(t, pos, color=col, lw=2.4,
                label=f"{fps} FPS  =>  {speed} px/s")
    ax.scatter([2.0], [5 * 144], color=BLUE, zorder=5, s=45)
    ax.scatter([2.0], [5 * 60], color=GREEN, zorder=5, s=45)
    ax.scatter([2.0], [5 * 30], color=RED, zorder=5, s=45)
    ax.annotate("1440 px", xy=(2.0, 720), xytext=(1.15, 760),
                fontsize=10, color=BLUE, weight="bold")
    ax.annotate("300 px", xy=(2.0, 300), xytext=(1.25, 330),
                fontsize=10, color=GREEN, weight="bold")
    ax.annotate("150 px", xy=(2.0, 150), xytext=(1.25, 130),
                fontsize=10, color=RED, weight="bold")
    ax.set_xlabel("time (seconds)", fontsize=12)
    ax.set_ylabel("position (pixels)", fontsize=12)
    ax.set_title("WRONG:  'move 5px per frame'\nspeed follows the framerate",
                 fontsize=12.5, weight="bold", color=RED)
    ax.set_xlim(0, 2.1); ax.set_ylim(0, 900)
    ax.grid(True, alpha=0.3)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.legend(loc="upper left", fontsize=10, frameon=False)

    # ---- 右: 正确做法 "speed_per_second * dt", 三条曲线重合
    ax = axes[1]
    target_speed = 300.0   # px/s 设计意图
    # 用离散步长模拟不同 FPS
    for fps, col in fps_list:
        dt = 1.0 / fps
        n = int(2.0 * fps)
        ts = np.array([i * dt for i in range(n + 1)])
        pos = np.array([target_speed * i * dt for i in range(n + 1)])
        ax.plot(ts, pos, color=col, lw=2.0, marker="o", ms=3.5,
                label=f"{fps} FPS  (dt={1000/fps:.1f}ms)")
    # 理想曲线
    ax.plot(t, target_speed * t, color=INK, lw=1.5, ls="--", alpha=0.5,
            label="ideal: 300 t")
    ax.scatter([2.0], [600], color=INK, zorder=6, s=70, marker="*")
    ax.annotate("all reach 600 px at t=2s\n=> speed decoupled from FPS",
                xy=(2.0, 600), xytext=(0.3, 740),
                fontsize=11, color=INK, weight="bold",
                arrowprops=dict(arrowstyle="->", color=INK, lw=1.2))
    ax.set_xlabel("time (seconds)", fontsize=12)
    ax.set_ylabel("position (pixels)", fontsize=12)
    ax.set_title("RIGHT:  'speed_per_second * dt'\nsame speed on any framerate",
                 fontsize=12.5, weight="bold", color=GREEN)
    ax.set_xlim(0, 2.1); ax.set_ylim(0, 900)
    ax.grid(True, alpha=0.3)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.legend(loc="upper left", fontsize=9.5, frameon=False)

    fig.suptitle("Framerate independence:  decouple game speed from FPS with delta time",
                 fontsize=13.5, weight="bold", color=INK, y=1.02)
    fig.savefig(os.path.join(OUT, "fig-p3_11_02-framerate-independence.png"))
    plt.close(fig)


# ---------------------------------------------------------------- fig-03
# delta 平滑 + 钳位对比: 数值模拟
def fig_smoothing_and_clamp():
    fig, axes = plt.subplots(2, 1, figsize=(13, 8.4), sharex=True)

    # 制造一段抖动的真实 delta: 基线 16.7ms, 偶尔尖刺 (调度抖动 + 一次大卡顿)
    rng = np.random.default_rng(42)
    n_frames = 90
    base = np.full(n_frames, 16.7)
    jitter = rng.normal(0, 1.6, n_frames)             # 调度抖动 ~1.6ms
    spikes = np.zeros(n_frames)
    spikes[[18, 33, 47, 61]] = [9.0, 12.0, 7.5, 14.0] # 偶尔小尖刺
    raw = base + jitter + spikes
    raw[75] = 210.0   # 一次大卡顿 (窗口切后台 ~210ms)

    frames = np.arange(n_frames)

    # ---- 上图: 平滑 (EMA)
    ax = axes[0]
    ax.plot(frames, raw, color=GREY, lw=1.6, alpha=0.8,
            label="raw delta (jittery)")
    # EMA: smoothed = a * raw + (1-a) * prev
    alpha = 0.25
    smoothed = np.zeros(n_frames)
    smoothed[0] = raw[0]
    for i in range(1, n_frames):
        smoothed[i] = alpha * raw[i] + (1 - alpha) * smoothed[i - 1]
    ax.plot(frames, smoothed, color=BLUE, lw=2.4,
            label=f"EMA smoothed  (alpha={alpha})")
    ax.axhline(16.7, color=GREEN, ls=":", lw=1.4, alpha=0.7)
    ax.text(1, 17.6, "target 16.7ms", color=GREEN, fontsize=10)
    # 标尖刺区
    ax.annotate("scheduling jitter\n(os scheduler / GC)",
                xy=(33, raw[33]), xytext=(40, 55),
                fontsize=10, color=RED, weight="bold",
                arrowprops=dict(arrowstyle="->", color=RED, lw=1.2))
    ax.set_ylabel("delta time (ms)", fontsize=12)
    ax.set_title("Smoothing:  raw jittery delta vs EMA-smoothed delta\n"
                 "(animation reads the smooth line, avoids micro-stutter)",
                 fontsize=12.5, weight="bold", color=INK)
    ax.set_xlim(0, n_frames - 1)
    ax.set_ylim(0, 80)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.grid(True, alpha=0.25)
    ax.legend(loc="upper left", fontsize=10.5, frameon=False)

    # ---- 下图: 钳位 (clamp) 防瞬移
    ax = axes[1]
    # 用 clamp 后的 delta 重画, 同时画 "不钳位" 时小球位移 vs 钳位后位移
    speed = 300.0  # px/s
    pos_unclamped = np.zeros(n_frames)
    pos_clamped = np.zeros(n_frames)
    dt_clamped = np.minimum(raw, 50.0)   # 钳到 50ms
    for i in range(n_frames):
        pos_unclamped[i] = (pos_unclamped[i - 1] + speed * raw[i] / 1000.0) if i > 0 else 0
        pos_clamped[i] = (pos_clamped[i - 1] + speed * dt_clamped[i] / 1000.0) if i > 0 else 0

    ax.plot(frames, pos_unclamped, color=RED, lw=2.4,
            label="position, raw dt  (no clamp)")
    ax.plot(frames, pos_clamped, color=GREEN, lw=2.4,
            label="position, dt clamped to 50ms")
    # 标卡顿帧的瞬移
    ax.annotate("frame 75 stalled ~210ms:\nno clamp => object teleports +63px\n"
                "clamp => moves at most 15px (no wall-clipping)",
                xy=(75, pos_unclamped[75]), xytext=(48, 75),
                fontsize=10.5, color=RED, weight="bold",
                arrowprops=dict(arrowstyle="->", color=RED, lw=1.3))
    ax.axvline(75, color=AMBER, ls="--", lw=1.4, alpha=0.7)
    ax.set_xlabel("frame number", fontsize=12)
    ax.set_ylabel("object position (px)", fontsize=12)
    ax.set_title("Clamping:  cap delta at a max (e.g. 50ms) so a stalled frame "
                 "cannot teleport the world",
                 fontsize=12.5, weight="bold", color=INK)
    ax.set_xlim(0, n_frames - 1)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.grid(True, alpha=0.25)
    ax.legend(loc="upper left", fontsize=10.5, frameon=False)

    fig.tight_layout()
    fig.savefig(os.path.join(OUT, "fig-p3_11_03-smoothing-and-clamp.png"))
    plt.close(fig)


# ---------------------------------------------------------------- fig-04
# 固定步长 vs delta time + 插值 alpha: 把 P3-10 和 P3-11 统一起来
def fig_fixed_plus_delta_unified():
    fig, ax = plt.subplots(figsize=(13.5, 5.6))

    # 时间轴 (世界时间)
    y_axis = 1.0
    ax.annotate("", xy=(12.8, y_axis), xytext=(0.3, y_axis),
                arrowprops=dict(arrowstyle="->", color=INK, lw=1.6))
    ax.text(12.9, y_axis, "world time",
            fontsize=11, color=INK, va="center")

    # 固定步长刻度 (每 1 个单位 = 一个 fixed step)
    step = 2.0
    fixed_marks = np.arange(1.0, 12.5, step)
    for fm in fixed_marks:
        ax.plot([fm, fm], [y_axis - 0.1, y_axis + 0.1], color=INK, lw=1.8)
    # 标 fixed steps
    for i, fm in enumerate(fixed_marks[:4]):
        ax.text(fm, y_axis + 0.25, f"physics step\n#{i}", ha="center",
                fontsize=9.5, color=DGREY)

    # 渲染帧 (不固定, 在两个 fixed step 之间, 用 alpha 插值)
    render_frames = [(2.4, "frame A\nalpha=0.2"),
                     (3.3, "frame B\nalpha=0.65"),
                     (5.7, "frame C\nalpha=0.35")]
    for rx, rlab in render_frames:
        # 找左右 fixed step
        left = max(m for m in fixed_marks if m <= rx)
        right = min(m for m in fixed_marks if m >= rx)
        alpha = (rx - left) / (right - left)
        # 画两条状态 (前/后 physics state)
        ax.plot([left, left], [2.6, 3.4], color=BLUE, lw=2.4)
        ax.plot([right, right], [2.6, 3.4], color=BLUE, lw=2.4)
        # 渲染帧位置
        ax.plot([rx, rx], [2.4, 3.6], color=GREEN, lw=2.8)
        ax.text(rx, 3.75, rlab, ha="center", fontsize=9.5,
                color=GREEN, weight="bold")
        # 插值连线
        ax.plot([left, right], [3.0, 3.0], color=GREY, lw=1.2, ls=":")
        ax.scatter([rx], [3.0], color=GREEN, s=55, zorder=5)
        ax.annotate(f"alpha = {alpha:.2f}\n= (rx - left)/step",
                    xy=(rx, 3.0), xytext=(rx, 4.25),
                    fontsize=8.8, color=GREEN, ha="center",
                    arrowprops=dict(arrowstyle="->", color=GREEN, lw=0.9))

    # 说明两个时钟
    box(ax, 0.4, -1.0, 5.6, 1.5,
        "Time<Fixed>  (physics clock)\n"
        "advances in fixed steps\n"
        "=> numerically stable, reproducible",
        fc=(0.86, 0.90, 1.0), ec=BLUE, fs=10.5, weight="bold", tc=BLUE)
    box(ax, 6.4, -1.0, 6.0, 1.5,
        "Time<Virtual>  (render/animation clock)\n"
        "advances by real delta_time\n"
        "=> smooth, framerate-independent",
        fc=(0.88, 0.95, 0.88), ec=GREEN, fs=10.5, weight="bold", tc=GREEN)

    # 总览箭头
    ax.text(6.5, 5.0,
            "Unify:  physics on fixed steps (stable)\n"
            "render interpolates between states with alpha = overstep_fraction",
            ha="center", fontsize=10.5, color=INK, weight="bold")

    ax.set_xlim(0, 13.6)
    ax.set_ylim(-1.4, 5.6)
    ax.set_aspect("equal")
    clean_ax(ax)
    fig.savefig(os.path.join(OUT, "fig-p3_11_04-fixed-plus-delta-unified.png"))
    plt.close(fig)


if __name__ == "__main__":
    fig_delta_time_timing()
    fig_framerate_independent()
    fig_smoothing_and_clamp()
    fig_fixed_plus_delta_unified()
    print("done:", sorted(f for f in os.listdir(OUT)
                          if f.startswith("fig-p3_11_") and f.endswith(".png")))
