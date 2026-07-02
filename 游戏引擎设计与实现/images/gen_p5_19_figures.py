# -*- coding: utf-8 -*-
"""为《游戏引擎》P5-19 (输入与事件系统) 生成一组 PNG 示意图。
图内英文标注, 正文中文解释。运行: python gen_p5_19_figures.py
配色沿用全书固定: RED/GREEN/BLUE/AMBER/GREY。
"""
import os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, FancyArrowPatch, FancyBboxPatch, Circle, Polygon
import numpy as np

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
LRED  = (0.99, 0.92, 0.92)


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
# fig-p5_19_01 : 输入流水线
# OS 硬件事件 -> wint 后端 -> Bevy message (KeyboardInput 等)
# -> PreUpdate 的 keyboard_input_system / gamepad_event_processing_system
# -> ButtonInput<KeyCode> / Gamepad 资源(状态) -> Update 段 System 查询
# ============================================================
def fig_input_pipeline():
    fig, ax = plt.subplots(figsize=(14.0, 7.6))
    clean_ax(ax)
    ax.set_xlim(0, 14)
    ax.set_ylim(0, 8.6)

    # 标题
    ax.text(7.0, 8.25, "Input Pipeline:  from hardware event to gameplay system",
            ha="center", va="center", fontsize=14.5, weight="bold", color=INK)

    # 第一层: OS / 硬件 (左)
    box(ax, 0.2, 5.5, 2.6, 1.5,
        "OS / Hardware\n(keyboard / mouse\n/ gamepad / touch)", fc=LAMBER, fs=11.5)
    # 第二层: winit 后端
    box(ax, 3.2, 5.5, 2.6, 1.5,
        "winit window backend\n(platform event loop\non main thread)", fc=SOFT, fs=11.5)
    # 第三层: Bevy message (事件队列)
    box(ax, 6.2, 5.5, 3.0, 1.5,
        "Bevy Message queue\n(KeyboardInput /\nMouseButtonInput /\nRawGamepadEvent)",
        fc=LBLUE, fs=11)

    # 第四层: PreUpdate 里的 input system (折叠 message -> 状态)
    box(ax, 9.6, 5.5, 4.2, 1.5,
        "PreUpdate: InputSystems\nkeyboard_input_system()\ngamepad_event_processing_system()\n(fold message stream -> state)",
        fc=LGREEN, fs=10.5)

    # 第五层: 状态资源
    box(ax, 3.6, 2.7, 3.4, 1.4,
        "State Resources\nButtonInput<KeyCode>\nButtonInput<MouseButton>\nGamepad (analog/digital)",
        fc=PINK, fs=10.5, weight="bold")
    # 第六层: Update 段 gameplay system
    box(ax, 8.2, 2.7, 4.4, 1.4,
        "Update: gameplay systems\nRes<ButtonInput<KeyCode>>\n.pressed() / .just_pressed()",
        fc=SOFT, fs=10.5)

    # 最底: 主循环 input 段示意
    box(ax, 0.6, 0.5, 12.8, 1.3,
        "Main loop input phase (one place, one snapshot per frame):\n"
        "collect  ->  fold into state  ->  gameplay reads ONE consistent snapshot",
        fc=GREY, fs=11, tc=INK)

    # 箭头链
    arrow(ax, (2.8, 6.25), (3.2, 6.25))
    arrow(ax, (5.8, 6.25), (6.2, 6.25))
    arrow(ax, (9.2, 6.25), (9.6, 6.25))
    # input system 写状态资源
    arrow(ax, (11.7, 5.5), (6.5, 4.1), color=GREEN, lw=1.8)
    ax.text(9.4, 4.95, "press() / release()\nwrites state", ha="center", va="center",
            fontsize=10, color=GREEN, style="italic")
    # 状态资源 -> gameplay
    arrow(ax, (7.0, 3.4), (8.2, 3.4), color=BLUE, lw=1.8)
    ax.text(7.6, 3.75, "read", ha="center", va="center", fontsize=10, color=BLUE,
            style="italic")
    # gameplay -> 主循环
    arrow(ax, (10.4, 2.7), (10.4, 1.8), color=INK)

    # 时段标注 (上方)
    ax.text(1.5, 7.35, "outside engine", ha="center", fontsize=10.5, color=DGREY, style="italic")
    ax.text(7.7, 7.35, "PreUpdate  (frame head)", ha="center", fontsize=10.5, color=GREEN,
            weight="bold")
    ax.text(10.4, 2.35, "Update  (frame body)", ha="center", fontsize=10.5, color=BLUE,
            weight="bold")

    # 右上角注解: message vs state
    ax.text(13.6, 6.25, "transient\n(consumed)",
            ha="right", va="center", fontsize=9.5, color=DGREY, style="italic")
    ax.text(5.3, 2.45, "lasting (per-frame snapshot)",
            ha="center", fontsize=9.5, color=DGREY, style="italic")

    plt.savefig(os.path.join(OUT, "fig-p5_19_01-input-pipeline.png"))
    plt.close(fig)


# ============================================================
# fig-p5_19_02 : 轮询 vs 事件总线 (含短按漏检场景)
# 上半: 轮询 pressed() 适合持续状态 (按住移动)
# 下半: 事件总线 + just_pressed 适合瞬时动作 (跳跃)
# 中间: 短按漏检 -- 两帧之间按下又抬起, 轮询可能错过,
#       但 message 队列里按下+抬起两条都在, 不丢。
# ============================================================
def fig_poll_vs_event():
    fig, ax = plt.subplots(figsize=(14.2, 9.2))
    clean_ax(ax)
    ax.set_xlim(0, 14.2)
    ax.set_ylim(0, 9.6)

    ax.text(7.1, 9.25, "Polling (state) vs Event bus (message)  -  and the missed-press trap",
            ha="center", fontsize=14.5, weight="bold", color=INK)

    # ============ 上半: 轮询 / 持续状态 ============
    ax.text(0.3, 8.3, "A. Polling  ButtonInput::pressed()  -  for HELD state (move)",
            fontsize=12.5, weight="bold", color=GREEN)

    # 时间轴 frame N-1, N, N+1, N+2
    fy = 6.6
    frames = [("frame N-1", 1.0), ("frame N", 4.0), ("frame N+1", 7.0), ("frame N+2", 10.0)]
    for name, x in frames:
        ax.add_patch(Rectangle((x, fy), 2.6, 0.9, facecolor=LGREEN, edgecolor=GREEN, lw=1.4))
        ax.text(x + 1.3, fy + 0.45, name, ha="center", va="center", fontsize=11, weight="bold")

    # "按住" 横条 跨越所有帧
    ax.add_patch(Rectangle((1.0, fy + 1.05), 11.6, 0.45, facecolor=GREEN, edgecolor=GREEN, alpha=0.55))
    ax.text(6.8, fy + 1.27, "player HOLDS 'Move Right'  (pressed == true every frame)",
            ha="center", va="center", fontsize=10.5, color=INK, weight="bold")

    # 每帧 pressed? -> yes
    for name, x in frames:
        arrow(ax, (x + 1.3, fy + 1.55), (x + 1.3, fy + 0.92), color=GREEN, lw=1.3)
        ax.text(x + 1.3, fy + 1.85, "pressed? YES", ha="center", fontsize=10, color=GREEN, weight="bold")
    ax.text(6.8, fy - 0.25, "->  character moves right every frame.  Polling is perfect for HELD input.",
            ha="center", fontsize=10.5, color=INK, style="italic")

    # ============ 下半: 事件总线 / 瞬时动作 ============
    ax.text(0.3, 5.4, "B. Event bus  just_pressed()  -  for ONE-SHOT action (jump)",
            fontsize=12.5, weight="bold", color=BLUE)

    fy2 = 3.5
    for name, x in frames:
        ax.add_patch(Rectangle((x, fy2), 2.6, 0.9, facecolor=LBLUE, edgecolor=BLUE, lw=1.4))
        ax.text(x + 1.3, fy2 + 0.45, name, ha="center", va="center", fontsize=11, weight="bold")

    # 按 jump 只在 frame N 发生 (一个瞬时事件)
    arrow(ax, (5.3, fy2 + 1.7), (5.3, fy2 + 0.92), color=BLUE, lw=1.6)
    ax.text(5.3, fy2 + 1.95, "press 'Jump'", ha="center", fontsize=10.5, color=BLUE, weight="bold")
    ax.text(5.3, fy2 + 1.35, "KeyboardInput{Pressed}", ha="center", fontsize=9, color=BLUE, style="italic")

    ax.text(1.3 + 1.3, fy2 - 0.25, "just_pressed?\nNO", ha="center", fontsize=9.5, color=DGREY)
    ax.text(4.0 + 1.3, fy2 - 0.25, "just_pressed?\nYES -> jump!", ha="center", fontsize=9.5,
            color=BLUE, weight="bold")
    ax.text(7.0 + 1.3, fy2 - 0.25, "just_pressed?\nNO (consumed)", ha="center", fontsize=9.5, color=DGREY)
    ax.text(10.0 + 1.3, fy2 - 0.25, "just_pressed?\nNO", ha="center", fontsize=9.5, color=DGREY)

    ax.text(6.8, fy2 - 1.0, "->  jump fires exactly once.  Event bus is perfect for ONE-SHOT input.",
            ha="center", fontsize=10.5, color=INK, style="italic")

    # ============ 底部: 短按漏检陷阱 ============
    ax.text(0.3, 1.95, "C. The missed-press trap  (why we need BOTH)",
            fontsize=12.5, weight="bold", color=RED)

    # 两个帧之间, 玩家按下又抬起 (比一帧还短的点击)
    ax.add_patch(Rectangle((4.0, 1.25), 2.6, 0.55, facecolor=AMBER, edgecolor=AMBER, alpha=0.5))
    ax.add_patch(Rectangle((7.0, 1.25), 2.6, 0.55, facecolor=AMBER, edgecolor=AMBER, alpha=0.5))
    ax.text(6.85, 1.52, "between frames", ha="center", va="center", fontsize=9.5, color=INK, style="italic")

    # 一个非常短的脉冲, 落在 frame N 和 frame N+1 之间
    ax.add_patch(Rectangle((5.7, 0.55), 1.7, 0.45, facecolor=RED, edgecolor=RED, alpha=0.75))
    ax.text(6.55, 0.78, "press+release\n(< 16ms)", ha="center", va="center", fontsize=9, color="white", weight="bold")

    ax.text(4.0 + 1.3, 1.15, "frame N\npolled:\nreleased", ha="center", fontsize=9, color=DGREY)
    ax.text(7.0 + 1.3, 1.15, "frame N+1\npolled:\nreleased", ha="center", fontsize=9, color=DGREY)

    ax.text(6.8, 0.18,
            "Polling pressed() sees 'released' on BOTH frames  ->  press LOST.\n"
            "Event queue still holds both Pressed + Released messages  ->  not lost.",
            ha="center", fontsize=10, color=RED, style="italic")

    plt.savefig(os.path.join(OUT, "fig-p5_19_02-poll-vs-event.png"))
    plt.close(fig)


# ============================================================
# fig-p5_19_03 : 设备抽象 -- 键鼠/手柄/触屏 -> 统一 Action 映射
# 左: 三种物理设备各自的事件/状态
# 中: 输入子系统归一化 + (可选) Action 映射层
# 右: 游戏逻辑只认 Action (Jump/Move/Attack), 不关心设备
# 配死区/平滑说明
# ============================================================
def fig_device_abstraction():
    fig, ax = plt.subplots(figsize=(14.2, 8.4))
    clean_ax(ax)
    ax.set_xlim(0, 14.2)
    ax.set_ylim(0, 8.6)

    ax.text(7.1, 8.25, "Device Abstraction:  gameplay code binds to ACTIONS, not hardware",
            ha="center", fontsize=14.5, weight="bold", color=INK)

    # 左: 三种设备
    box(ax, 0.3, 6.1, 3.0, 1.3, "Keyboard + Mouse\n(KeyboardInput /\nMouseButtonInput)",
        fc=LAMBER, fs=10.5)
    box(ax, 0.3, 4.4, 3.0, 1.3, "Gamepad\n(RawGamepadEvent:\naxes + buttons)",
        fc=LAMBER, fs=10.5)
    box(ax, 0.3, 2.7, 3.0, 1.3, "Touchscreen\n(TouchInput: \npress / drag / pinch)",
        fc=LAMBER, fs=10.5)

    # 中: 输入子系统归一化
    box(ax, 4.0, 4.0, 3.6, 4.4,
        "Input subsystem\n(normalize)\n\n"
        "- raw event -> state\n"
        "- deadzone +/- 0.05\n"
        "- threshold 0.01\n"
        "- press 0.75 / release 0.65\n"
        "  (hysteresis, no jitter)\n"
        "- smoothing / accel",
        fc=LGREEN, fs=10.5)

    # 中右: Action 映射层 (虚线表示可选/游戏自定义)
    box(ax, 8.2, 4.0, 3.4, 4.4,
        "Action map  (optional,\ngame-defined layer)\n\n"
        "Move  <-  WASD / Left stick / drag\n"
        "Jump  <-  Space / A button / 2-finger\n"
        "Attack<-  LMB / RT / tap\n"
        "Pause <-  Esc / Start / swipe-down",
        fc=LBLUE, fs=10.5, ec=BLUE)

    # 右: 游戏逻辑
    box(ax, 12.1, 4.6, 1.9, 3.2,
        "Gameplay\nlogic\n\n"
        "only sees\n"
        "ACTIONS\n"
        "(device-\nagnostic)",
        fc=PINK, fs=11, weight="bold")

    # 箭头
    for y in (6.75, 5.05, 3.35):
        arrow(ax, (3.3, y), (4.0, 5.8 + (y - 5.0) * 0.25), color=INK, lw=1.3)
    arrow(ax, (7.6, 6.2), (8.2, 6.2), color=GREEN, lw=1.7)
    ax.text(7.9, 6.5, "state", ha="center", fontsize=9.5, color=GREEN, style="italic")
    arrow(ax, (11.6, 6.2), (12.1, 6.2), color=BLUE, lw=1.7)
    ax.text(11.85, 6.5, "action", ha="center", fontsize=9.5, color=BLUE, style="italic")

    # 底部: 死区/平滑示意小图 (左侧位置)
    # 画一个摇杆原始值 -> 死区 -> 归一化的曲线
    inset = fig.add_axes([0.04, 0.04, 0.20, 0.20])
    x = np.linspace(-1, 1, 400)
    raw = 0.6 * np.sin(3 * x) * np.exp(-x**2) + 0.05 * np.sign(np.sin(15*x))  # 含噪声
    dead = np.where(np.abs(x) < 0.05, 0.0, x)
    inset.plot(x, x, color=DGREY, lw=1.0, linestyle="--", label="ideal")
    inset.plot(x, dead, color=RED, lw=2.0, label="after deadzone")
    inset.axvspan(-0.05, 0.05, color=AMBER, alpha=0.4)
    inset.set_title("deadzone clips center noise", fontsize=9)
    inset.tick_params(labelsize=7)
    inset.legend(fontsize=7, loc="upper left")
    inset.set_xlabel("raw axis", fontsize=8)
    inset.set_ylabel("filtered", fontsize=8)

    # 右下文字结论
    ax.text(7.1, 1.5,
            "Why abstract?\n"
            "1. gameplay code never branches on 'is it keyboard or gamepad?'\n"
            "2. rebinding is a data change, not a code change\n"
            "3. accessibility: one action -> many devices (auto fallback)",
            ha="center", va="center", fontsize=10.5, color=INK,
            bbox=dict(boxstyle="round,pad=0.4", facecolor=SOFT, edgecolor=DGREY))

    plt.savefig(os.path.join(OUT, "fig-p5_19_03-device-abstraction.png"))
    plt.close(fig)


# ============================================================
# fig-p5_19_04 : ButtonInput 三态状态机 (pressed / just_pressed / just_released)
# 一帧内时间线: clear -> press events -> state frozen for this frame
# ============================================================
def fig_button_state():
    fig, ax = plt.subplots(figsize=(13.5, 6.6))
    clean_ax(ax)
    ax.set_xlim(0, 13.5)
    ax.set_ylim(0, 6.8)

    ax.text(6.75, 6.4, "ButtonInput:  three HashSets fold the event stream into a per-frame snapshot",
            ha="center", fontsize=14, weight="bold", color=INK)

    # 三个 HashSet 框
    box(ax, 0.8, 3.2, 3.6, 2.2,
        "pressed: HashSet<T>\n\n"
        "every key held down\nRIGHT NOW\n\n"
        "pressed(K) -> O(1)",
        fc=PINK, fs=11)
    box(ax, 4.95, 3.2, 3.6, 2.2,
        "just_pressed: HashSet<T>\n\n"
        "keys whose press\narrived THIS frame\n\n"
        "cleared at frame head",
        fc=LGREEN, fs=11)
    box(ax, 9.1, 3.2, 3.6, 2.2,
        "just_released: HashSet<T>\n\n"
        "keys whose release\narrived THIS frame\n\n"
        "cleared at frame head",
        fc=LBLUE, fs=11)

    # 上方: 事件流
    ax.text(6.75, 5.75, "event stream:  Press(A)  Press(B)  Release(A)  Press(C)  ...",
            ha="center", fontsize=11, color=DGREY, style="italic")

    # 折叠箭头
    arrow(ax, (6.75, 5.55), (2.6, 5.45), color=GREEN)
    arrow(ax, (6.75, 5.55), (6.75, 5.45), color=GREEN)
    arrow(ax, (6.75, 5.55), (10.9, 5.45), color=BLUE)

    # 下方: 帧边界 clear
    box(ax, 3.5, 1.0, 6.5, 1.4,
        "frame head:  clear() wipes just_pressed & just_released\n"
        "(pressed survives - it is the lasting state)\n"
        "then keyboard_input_system replays this frame's messages",
        fc=SOFT, fs=10.5)
    arrow(ax, (6.75, 2.95), (6.75, 2.45), color=INK)

    ax.text(6.75, 0.45,
            "Result:  gameplay systems in Update read ONE consistent per-frame snapshot,\n"
            "no matter how many messages arrived or in what order.",
            ha="center", fontsize=10.5, color=INK, style="italic")

    plt.savefig(os.path.join(OUT, "fig-p5_19_04-button-state.png"))
    plt.close(fig)


if __name__ == "__main__":
    fig_input_pipeline()
    fig_poll_vs_event()
    fig_device_abstraction()
    fig_button_state()
    print("done: 4 figures written to", OUT)
