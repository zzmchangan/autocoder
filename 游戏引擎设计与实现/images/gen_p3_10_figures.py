"""
P3-10 主循环: fixed update vs render -- 配图脚本

四张图:
  fig-p3_10_01-accumulator-timeline.png   accumulator 时序: delta 累加, 每满 fixed_dt 跑物理, 渲染插值
  fig-p3_10_02-fixed-plus-interpolation.png  固定步长物理 + 插值渲染示意
  fig-p3_10_03-spiral-of-death.png        死亡螺旋: 物理耗时>fixed_dt 时 accumulator 爆炸 vs 钳位后稳定
  fig-p3_10_04-variable-vs-fixed-stability.png  可变步长数值发散 vs 固定步长稳定(数值轨迹)

风格: 纯英文标注, 固定配色, 去上右边框, bbox='tight'.
"""
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.patches import Rectangle, FancyArrow
import numpy as np

# ---------- 固定配色 + rcParams ----------
RED = "#dc2626"
GREEN = "#16a34a"
BLUE = "#2563eb"
AMBER = "#d97706"
GREY = "#6b7280"
LIGHT_BLUE = "#bfdbfe"
LIGHT_GREEN = "#bbf7d0"
LIGHT_AMBER = "#fde68a"
LIGHT_RED = "#fecaca"

plt.rcParams.update({
    "font.family": "DejaVu Sans",
    "font.size": 11,
    "axes.edgecolor": "#374151",
    "axes.linewidth": 1.0,
    "savefig.dpi": 150,
})


def strip_spines(ax):
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)


# =====================================================================
# 图 1: accumulator 时序
# 横轴=帧 (frame 0..6), 每帧来一个 delta (近似 16.7ms 但抖动),
# 累加进 accumulator, 每满 fixed_dt (设为 20ms 让单帧不够、双帧凑一次) 跑一次物理.
# 渲染时用 accumulator/fixed_dt 作插值系数 alpha.
# =====================================================================
def fig1_accumulator_timeline():
    fig, ax = plt.subplots(figsize=(11, 5.2))

    # fixed_dt 设为 20ms, 每帧 delta 约 12~13ms, 这样大约每两帧凑一次物理步.
    fixed_dt = 20.0
    deltas = [12.5, 13.0, 12.0, 13.5, 12.5, 12.5, 13.0]
    nframes = len(deltas)

    # 模拟 accumulator
    acc = 0.0
    # 我们画"每帧结束时的累计条" + "是否触发物理"
    frame_labels = []
    acc_after = []   # 每帧累加后、扣完物理步后的余数
    phys_steps = []  # 每帧触发的物理步数
    alpha = []       # 渲染插值系数

    for i, d in enumerate(deltas):
        acc += d
        steps = 0
        while acc >= fixed_dt:
            acc -= fixed_dt
            steps += 1
        acc_after.append(acc)
        phys_steps.append(steps)
        alpha.append(acc / fixed_dt)
        frame_labels.append(f"F{i}")

    x = np.arange(nframes)
    width = 0.62

    # 画每帧余数条 (accumulator 残留)
    bars = ax.bar(x, acc_after, width, color=LIGHT_BLUE,
                  edgecolor=BLUE, linewidth=1.2,
                  label="accumulator leftover (after fixed steps)")

    # 在条上标注余数 ms
    for xi, v in zip(x, acc_after):
        ax.text(xi, v + 0.4, f"{v:.1f}ms", ha="center", va="bottom",
                fontsize=9, color=BLUE)

    # fixed_dt 参考线
    ax.axhline(fixed_dt, color=RED, linestyle="--", linewidth=1.4,
               label=f"fixed_dt = {fixed_dt:.0f}ms (physics triggers when accumulator >= this)")

    # 标注每帧触发的物理步数
    for xi, steps, d in zip(x, phys_steps, deltas):
        if steps == 1:
            txt = "1 physics step"
            color = GREEN
        elif steps >= 2:
            txt = f"{steps} physics steps!"
            color = RED
        else:
            txt = "no physics step"
            color = GREY
        # 把标注放在条顶上方
        ax.text(xi, fixed_dt + 3, txt, ha="center", va="bottom",
                fontsize=9, color=color, fontweight="bold")
        # 在 x 轴下方标 delta
        ax.text(xi, -3.2, f"delta\n{d:.1f}ms", ha="center", va="top",
                fontsize=8, color=GREY)

    # 右侧第二个 y 轴标 alpha (渲染插值系数)
    ax2 = ax.twinx()
    ax2.plot(x, alpha, "o-", color=AMBER, linewidth=1.6, markersize=7,
             label="render alpha = accumulator / fixed_dt (interpolation)")
    ax2.set_ylim(-0.05, 1.05)
    ax2.set_ylabel("render interpolation alpha", color=AMBER)
    ax2.tick_params(axis="y", colors=AMBER)
    ax2.spines["top"].set_visible(False)
    # 把 right spine 留给 ax2, 但去掉它顶
    ax2.spines["right"].set_visible(True)
    ax2.spines["right"].set_color(AMBER)

    ax.set_xticks(x)
    ax.set_xticklabels(frame_labels)
    ax.set_xlabel("render frame")
    ax.set_ylabel("accumulator (ms)")
    ax.set_ylim(-6, fixed_dt + 22)
    ax.set_title("Accumulator timeline: variable delta in, fixed physics steps out",
                 fontsize=12, pad=12)

    # 合并图例
    h1, l1 = ax.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    ax.legend(h1 + h2, l1 + l2, loc="upper right", fontsize=8.5,
              framealpha=0.95)

    strip_spines(ax)
    fig.tight_layout()
    fig.savefig("fig-p3_10_01-accumulator-timeline.png", bbox_inches="tight")
    plt.close(fig)
    print("wrote fig-p3_10_01-accumulator-timeline.png")


# =====================================================================
# 图 2: 固定步长物理 + 插值渲染示意
# 画一个物体的真实物理位置 (在离散的物理 tick 上, 阶跃) 和
# 渲染插值位置 (在物理 tick 之间线性插值, 平滑).
# =====================================================================
def fig2_fixed_plus_interpolation():
    fig, ax = plt.subplots(figsize=(11, 5))

    # 物理在固定 tick 上推进, 位置 = 速度 * t (匀速), 但只在 tick 点存在.
    fixed_dt = 1.0  # 用无量纲时间
    phys_ticks = np.arange(0, 7)  # 6 个物理步
    phys_pos = 0.6 * phys_ticks * fixed_dt  # 匀速 0.6/step

    # 渲染帧在物理 tick 之间, 用 alpha 插值. 模拟渲染帧时间点.
    render_times = np.array([0.0, 0.4, 1.0, 1.7, 2.0, 2.5, 3.0, 3.3, 4.0, 4.8,
                             5.0, 5.6, 6.0])
    # 对每个渲染时刻, 找它落在哪两个物理 tick 之间, 算 alpha
    render_pos = []
    for t in render_times:
        lo = int(np.floor(t / fixed_dt))
        lo = min(lo, len(phys_ticks) - 1)
        hi = min(lo + 1, len(phys_ticks) - 1)
        a = (t - lo * fixed_dt) / fixed_dt
        p = phys_pos[lo] + a * (phys_pos[hi] - phys_pos[lo])
        render_pos.append(p)
    render_pos = np.array(render_pos)

    # 画物理 tick: 阶跃式
    ax.step(phys_ticks * fixed_dt, phys_pos, where="post",
            color=BLUE, linewidth=2.2, label="physics state (fixed tick, stepped)")
    ax.plot(phys_ticks * fixed_dt, phys_pos, "s", color=BLUE,
            markersize=11, zorder=5)

    # 画渲染插值: 平滑
    ax.plot(render_times, render_pos, "o-", color=RED, linewidth=1.8,
            markersize=7, label="rendered position (interpolated between physics ticks)",
            alpha=0.95)

    # 标注几个 tick 之间的插值
    for i in range(len(render_times)):
        t = render_times[i]
        if t % 1.0 != 0:  # 不是 tick 点
            lo = int(np.floor(t))
            a = t - lo
            ax.annotate(f"alpha={a:.1f}", xy=(t, render_pos[i]),
                        xytext=(t, render_pos[i] - 0.55),
                        fontsize=7.5, color=RED, ha="center",
                        arrowprops=dict(arrowstyle="-", color=RED, lw=0.6))

    # 标注物理 tick
    for tk, pv in zip(phys_ticks, phys_pos):
        ax.annotate(f"physics\n(t={tk})", xy=(tk, pv),
                    xytext=(tk, pv + 0.55),
                    fontsize=8, color=BLUE, ha="center",
                    arrowprops=dict(arrowstyle="-", color=BLUE, lw=0.6))

    ax.set_xlabel("time (in units of fixed_dt)")
    ax.set_ylabel("object position")
    ax.set_title("Fixed-step physics + interpolated rendering\n"
                 "(physics ticks at integers; render frames anywhere, blended by alpha)",
                 fontsize=12, pad=10)
    ax.set_xlim(-0.3, 6.3)
    ax.set_ylim(-0.8, max(phys_pos) + 1.4)
    ax.set_xticks(phys_ticks)
    ax.legend(loc="upper left", fontsize=9, framealpha=0.95)
    strip_spines(ax)
    fig.tight_layout()
    fig.savefig("fig-p3_10_02-fixed-plus-interpolation.png", bbox_inches="tight")
    plt.close(fig)
    print("wrote fig-p3_10_02-fixed-plus-interpolation.png")


# =====================================================================
# 图 3: spiral of death
# 左子图: 物理耗时 > fixed_dt -> accumulator 爆炸 (单调上升)
# 右子图: 钳位 max_delta 后 -> accumulator 被限制, 稳定 (但游戏慢动作)
# =====================================================================
def fig3_spiral_of_death():
    """
    更直观的演示 spiral of death:
      左图 (no clamp): 每物理步耗时 24ms, fixed_dt=16ms. 每帧 base 渲染 4ms.
        每帧 accumulator += frame_time; 跑物理直到 acc<fixed_dt (无上限).
        关键: 每跑一次物理, frame_time 增加 24ms, 又会被算进 acc,
        触发更多物理步 -> 单帧 frame_time 单调爆炸.
        我们记录每帧"测到的 frame_time"(= base + 24*steps) 和"跑的物理步数".
      右图 (clamped): 同样 24ms/物理步, 但 clamp max_delta 让 accumulator 每帧最多涨 32ms,
        最多跑 2 物理步. frame_time 被钳在 base+24*2=52ms, 游戏慢动作但稳定.
    """
    fig, (axL, axR) = plt.subplots(1, 2, figsize=(12, 4.8))

    fixed_dt = 16.0  # ms
    nframes = 10
    base_render = 4.0       # ms, 每帧渲染+input 等固定开销
    phys_step_cost = 24.0   # ms, 单次物理步耗时 ( > fixed_dt, 这就是 spiral 的根 )
    frames = np.arange(nframes)
    w = 0.4

    # --- 左: no clamp ---
    # 每帧 frame_time = base + phys_step_cost * 该帧跑的物理步数.
    # accumulator 每帧 += frame_time; while acc>=fixed_dt: 跑物理, acc-=fixed_dt, steps++.
    acc = 0.0
    ft_hist, steps_hist, acc_hist = [], [], []
    for i in range(nframes):
        # 这帧一开始 acc 是上一帧的余量, 先加一个最小 frame_time(base) 试探
        # 实际: frame_time 取决于这帧跑多少物理步, 但物理步数又取决于 acc. 互相依赖.
        # 用迭代: 假设这帧跑 k 步, k = (acc + base + 24*k) / fixed_dt 向下取整? 不对.
        # 正确建模: 主循环测到的 frame_time 是"上一帧实际花了多久"= 上一帧 base + 24*上一帧 steps.
        # 也就是: delta_time_this_frame = frame_time_last_frame.
        # 所以 acc += frame_time_last_frame.
        pass
    # 用"上一帧的 frame_time 作为这一帧的 delta"建模, 更符合真实 (delta = 上一帧实测耗时):
    acc = 0.0
    last_ft = base_render + phys_step_cost * 1  # 第一帧假设跑了 1 步
    for i in range(nframes):
        delta = last_ft                      # 主循环量到的 delta = 上一帧耗时
        acc += delta
        steps = 0
        while acc >= fixed_dt:               # 无钳位
            acc -= fixed_dt
            steps += 1
        this_ft = base_render + phys_step_cost * steps
        ft_hist.append(this_ft)
        steps_hist.append(steps)
        acc_hist.append(acc)
        last_ft = this_ft

    axL.bar(frames - w/2, acc_hist, w, color=LIGHT_RED, edgecolor=RED,
            label="accumulator leftover (ms)")
    axL.bar(frames + w/2, ft_hist, w, color=LIGHT_BLUE, edgecolor=BLUE,
            label="frame_time (base + 24ms per physics step)")
    axL.axhline(fixed_dt, color=GREY, linestyle="--", linewidth=1.2,
                label=f"fixed_dt={fixed_dt:.0f}ms")
    axL.set_xlabel("frame")
    axL.set_ylabel("time (ms)")
    axL.set_title("Spiral of death (no clamp): physics step 24ms > fixed_dt 16ms\n"
                  "each frame triggers more steps, frame_time snowballs",
                  fontsize=10, pad=8)
    axL.legend(fontsize=8, loc="upper left")
    strip_spines(axL)
    for xi, s, ft in zip(frames, steps_hist, ft_hist):
        axL.text(xi, ft + max(ft_hist) * 0.02, f"{s}", ha="center",
                 fontsize=8, color=RED, fontweight="bold")
    axL.text(0.02, 0.96, "(number above each bar = physics steps that frame)",
             transform=axL.transAxes, fontsize=7.5, color=GREY, va="top")

    # --- 右: clamped (max 2 physics steps/frame, 等价于钳 max_delta) ---
    max_steps = 2
    acc = 0.0
    last_ft = base_render + phys_step_cost * 1
    ft_hist2, steps_hist2, acc_hist2 = [], [], []
    for i in range(nframes):
        delta = last_ft
        acc += delta
        steps = 0
        while acc >= fixed_dt and steps < max_steps:   # 钳位
            acc -= fixed_dt
            steps += 1
        # 钳位后没跑完的 acc 留在 accumulator 里 (游戏会越来越落后, 但 frame_time 被钳住)
        # 为了让 acc 也稳定, 真实做法是再 clamp accumulator 本身 -- 这里我们展示 frame_time 被钳住,
        # acc 慢慢涨(慢动作), 但 frame_time 不再雪崩.
        this_ft = base_render + phys_step_cost * steps
        ft_hist2.append(this_ft)
        steps_hist2.append(steps)
        acc_hist2.append(acc)
        last_ft = this_ft

    axR.bar(frames - w/2, acc_hist2, w, color=LIGHT_GREEN, edgecolor=GREEN,
            label="accumulator leftover (ms)")
    axR.bar(frames + w/2, ft_hist2, w, color=LIGHT_AMBER, edgecolor=AMBER,
            label="frame_time (clamped: max 2 physics steps/frame)")
    axR.axhline(fixed_dt, color=GREY, linestyle="--", linewidth=1.2,
                label=f"fixed_dt={fixed_dt:.0f}ms")
    axR.set_xlabel("frame")
    axR.set_ylabel("time (ms)")
    axR.set_title("Clamped (max 2 physics steps/frame):\n"
                  "frame_time bounded, game runs slow but never freezes",
                  fontsize=10, pad=8)
    axR.legend(fontsize=8, loc="upper left")
    strip_spines(axR)
    for xi, s, ft in zip(frames, steps_hist2, ft_hist2):
        axR.text(xi, ft + max(ft_hist2) * 0.02, f"{s}", ha="center",
                 fontsize=8, color=GREEN, fontweight="bold")
    axR.text(0.02, 0.96, "(number above each bar = physics steps that frame)",
             transform=axR.transAxes, fontsize=7.5, color=GREY, va="top")

    # 共享 y 上限便于对比 (但右图 acc 会涨, 取两边最大)
    ymax = max(max(ft_hist), max(acc_hist), max(ft_hist2), max(acc_hist2)) * 1.25
    axL.set_ylim(0, ymax)
    axR.set_ylim(0, ymax)

    fig.tight_layout()
    fig.savefig("fig-p3_10_03-spiral-of-death.png", bbox_inches="tight")
    plt.close(fig)
    print("wrote fig-p3_10_03-spiral-of-death.png")


# =====================================================================
# 图 4: 可变步长数值发散 vs 固定步长稳定
# 模拟一个简单弹簧 (x'' = -omega^2 x), 显式欧拉积分.
# 左: 大且变化的 dt -> 能量增长, 振幅发散.
# 右: 固定 dt -> 振幅稳定.
# =====================================================================
def fig4_variable_vs_fixed_stability():
    fig, (axL, axR) = plt.subplots(1, 2, figsize=(12, 4.5))

    omega = 2.0
    T_total = 8.0

    # 左: 可变步长, dt 在 [0.05, 0.20] 之间随机抖动 (模拟卡顿帧)
    np.random.seed(7)
    t = 0.0
    x, v = 1.0, 0.0
    tv, xv = [0.0], [1.0]
    # 模拟"有时卡顿 dt 变大"
    while t < T_total:
        # 周期性来一个大 dt (模拟偶尔掉帧)
        dt = 0.05
        if int(t * 3) % 2 == 1:
            dt = 0.18  # 卡顿帧
        dt = min(dt, T_total - t)
        # 显式欧拉 (前向)
        x = x + v * dt
        v = v - omega * omega * x * dt
        t += dt
        tv.append(t)
        xv.append(x)
    axL.plot(tv, xv, "-", color=RED, linewidth=1.6)
    axL.plot(tv, xv, ".", color=RED, markersize=3)
    axL.axhline(0, color=GREY, linewidth=0.8)
    # 理论振幅包络 +-1
    tt = np.linspace(0, T_total, 400)
    axL.plot(tt, np.cos(omega * tt), "--", color=GREY, linewidth=1,
             label="true amplitude (±1)")
    axL.plot(tt, -np.cos(omega * tt), "--", color=GREY, linewidth=1)
    axL.set_xlabel("time (s)")
    axL.set_ylabel("position")
    axL.set_title("Variable dt + explicit Euler: energy leaks in on spikes,\n"
                  "amplitude grows (simulation diverges)",
                  fontsize=10.5, pad=8)
    axL.set_ylim(-2.2, 2.2)
    axL.legend(fontsize=8, loc="upper right")
    strip_spines(axL)

    # 右: 固定步长 dt=0.05, 同样显式欧拉
    dt = 0.05
    t = 0.0
    x, v = 1.0, 0.0
    tf, xf = [0.0], [1.0]
    while t < T_total:
        dt_use = min(dt, T_total - t)
        x = x + v * dt_use
        v = v - omega * omega * x * dt_use
        t += dt_use
        tf.append(t)
        xf.append(x)
    axR.plot(tf, xf, "-", color=GREEN, linewidth=1.6)
    axR.plot(tf, xf, ".", color=GREEN, markersize=3)
    axR.axhline(0, color=GREY, linewidth=0.8)
    tt = np.linspace(0, T_total, 400)
    # 固定 dt 下显式欧拉有轻微能量增长但稳定可预测(振幅轻微放大, 这里 dt=0.05 接近稳定)
    axR.plot(tt, np.cos(omega * tt), "--", color=GREY, linewidth=1,
             label="true amplitude (±1)")
    axR.plot(tt, -np.cos(omega * tt), "--", color=GREY, linewidth=1)
    axR.set_xlabel("time (s)")
    axR.set_ylabel("position")
    axR.set_title("Fixed dt + explicit Euler: every step identical,\n"
                  "behavior stable and reproducible",
                  fontsize=10.5, pad=8)
    axR.set_ylim(-2.2, 2.2)
    axR.legend(fontsize=8, loc="upper right")
    strip_spines(axR)

    fig.tight_layout()
    fig.savefig("fig-p3_10_04-variable-vs-fixed-stability.png", bbox_inches="tight")
    plt.close(fig)
    print("wrote fig-p3_10_04-variable-vs-fixed-stability.png")


if __name__ == "__main__":
    fig1_accumulator_timeline()
    fig2_fixed_plus_interpolation()
    fig3_spiral_of_death()
    fig4_variable_vs_fixed_stability()
    print("all figures written.")
