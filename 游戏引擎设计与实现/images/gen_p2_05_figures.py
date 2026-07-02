# -*- coding: utf-8 -*-
"""为《游戏引擎》P2-05 (ECS 三件套: Entity / Component / System) 生成一组 PNG 示意图。
图内英文标注, 正文中文解释。运行: python gen_p2_05_figures.py

Figures:
  fig-p2_05_01-ecs-dataflow.png   System 读 Component -> 处理 -> 写 Component 的数据流
  fig-p2_05_02-entity-recycle.png Entity 生成/回收: ID 池 + version 机制 (slot 复用)
  fig-p2_05_03-oop-vs-system.png  OOP 方法绑对象 vs System 遍历组件 对比
"""
import os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, FancyArrowPatch, FancyBboxPatch, Polygon, Circle

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
PURP  = (0.55, 0.36, 0.74)
LBLUE = (0.92, 0.95, 1.0)
LGREEN = (0.90, 0.95, 0.90)
LAMBER = (1.0, 0.94, 0.84)
LRED  = (1.0, 0.92, 0.92)
LPURP = (0.95, 0.90, 0.99)


def clean_ax(ax):
    for s in ("top", "right", "bottom", "left"):
        ax.spines[s].set_visible(False)
    ax.set_xticks([]); ax.set_yticks([])


def box(ax, x, y, w, h, text, fc=SOFT, ec=INK, fs=12, weight="normal", tc=INK):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.02,rounding_size=0.06",
                                facecolor=fc, edgecolor=ec, linewidth=1.4))
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center",
            fontsize=fs, weight=weight, color=tc)


def arrow(ax, p1, p2, color=INK, lw=1.6, style="-|>", rad=0.0, ms=14):
    cs = f"arc3,rad={rad}" if rad else "arc3,rad=0"
    ax.add_artist(FancyArrowPatch(p1, p2, arrowstyle=style, color=color,
                                  lw=lw, connectionstyle=cs, mutation_scale=ms))


# ============================================================ fig-p2_05_01
# Dynamic ECS dataflow: System reads Component pools -> mutates -> writes back.
# Emphasizes that Entity is just an index/key, and System owns NO data.
def fig_ecs_dataflow():
    fig, ax = plt.subplots(figsize=(13.2, 8.2))

    # ---- Title / framing
    ax.text(6.6, 8.9, "ECS dataflow: a System reads pools, mutates, writes back",
            ha="center", fontsize=15, weight="bold", color=INK)
    ax.text(6.6, 8.45,
            "System owns NO data — it borrows the component pools via a Query/View, then returns",
            ha="center", fontsize=11.5, color=DGREY, style="italic")

    # ---- Three Entity IDs (top): just numbers, nothing else
    box(ax, 0.4, 6.7, 2.3, 0.85, "Entity #3", fc=LRED, ec=RED, weight="bold", fs=11.5)
    box(ax, 2.9, 6.7, 2.3, 0.85, "Entity #7", fc=LRED, ec=RED, weight="bold", fs=11.5)
    box(ax, 5.4, 6.7, 2.3, 0.85, "Entity #9", fc=LRED, ec=RED, weight="bold", fs=11.5)
    ax.text(4.05, 7.95, "Entity = a bare ID. No fields, no methods.",
            ha="center", fontsize=11.5, color=RED, weight="bold")

    # ---- Component pools (middle): contiguous columns per type
    # Position[] column
    pool_x = {"Position": 0.6, "Velocity": 3.4, "Color": 6.2, "Health": 9.0}
    pool_col = {"Position": BLUE, "Velocity": GREEN, "Color": GREY, "Health": AMBER}
    pool_used = {"Position": True, "Velocity": True, "Color": False, "Health": False}
    pool_top = 5.4
    cell_w, cell_h = 1.95, 0.55

    ids = [3, 7, 9]
    for name, ox in pool_x.items():
        col = pool_col[name]
        # header
        box(ax, ox, pool_top + 0.15, cell_w, 0.55, f"{name}[]",
            fc=col, ec=col, fs=11.5, weight="bold", tc="white")
        # cells (one per entity)
        for i, eid in enumerate(ids):
            yy = pool_top - 0.5 - i * (cell_h + 0.08)
            ax.add_patch(Rectangle((ox, yy), cell_w, cell_h, facecolor=col, alpha=0.32,
                                   edgecolor=col, linewidth=1.2))
            ax.text(ox + cell_w / 2, yy + cell_h / 2, f"#{eid}",
                    ha="center", va="center", fontsize=10.5, color=INK, weight="bold")
    ax.text(6.4, pool_top - 0.5 - 3 * (cell_h + 0.08) + 0.0,
            "Component pools: pure data, one column per type. Entity id indexes a row.",
            ha="center", fontsize=11, color=INK)

    # ---- MovementSystem box (left): declare what it needs
    box(ax, 0.4, 1.3, 4.6, 1.3,
        "MovementSystem\nquery<Position, Velocity>()\n(for each entity: pos += vel)",
        fc=LBLUE, ec=BLUE, weight="bold", fs=11.5)
    # RenderSystem box (right)
    box(ax, 5.4, 1.3, 4.6, 1.3,
        "RenderSystem\nquery<Position, Color>()\n(for each entity: draw)",
        fc=LGREEN, ec=GREEN, weight="bold", fs=11.5)

    # Health bar (untouched, to show other systems)
    box(ax, 10.3, 1.3, 2.6, 1.3, "CombatSystem\nquery<Health>()",
        fc=LAMBER, ec=AMBER, weight="bold", fs=10.5)

    # ---- Arrows: system READS the pools it queried (solid), and WRITES back (dashed)
    # Movement reads Position+Velocity
    arrow(ax, (1.8, 2.6), (1.55, 4.85), color=BLUE, lw=1.8, rad=-0.05)   # to Position
    arrow(ax, (3.0, 2.6), (4.35, 4.85), color=BLUE, lw=1.8, rad=0.05)    # to Velocity
    # write back arrow (dashed) from system back to Position
    arrow(ax, (1.5, 2.6), (0.95, 4.85), color=BLUE, lw=1.3, style="-|>", rad=0.18)
    ax.plot([], [], "--", color=BLUE, lw=1.3, label="read (solid) / write-back (dashed)")
    ax.text(0.55, 3.95, "read\n& write", fontsize=9.5, color=BLUE, weight="bold",
            ha="left")

    # Render reads Position + Color
    arrow(ax, (7.0, 2.6), (1.55, 4.85), color=GREEN, lw=1.5, rad=0.25)   # to Position (shared)
    arrow(ax, (8.0, 2.6), (7.15, 4.85), color=GREEN, lw=1.8, rad=-0.05)  # to Color
    ax.text(6.1, 3.5, "read only", fontsize=9.5, color=GREEN, weight="bold")

    # Combat reads Health
    arrow(ax, (11.4, 2.6), (9.95, 4.85), color=AMBER, lw=1.8, rad=0.0)

    # ---- Bottom: key invariants
    box(ax, 0.4, 0.05, 12.5, 0.95,
        "Invariants:  (1) System holds no data.   (2) Component holds no behavior.   "
        "(3) Entity holds nothing but an id.   (4) Adding a system never touches the data.",
        fc=SOFT, ec=INK, fs=10.8, weight="bold")

    ax.set_xlim(0, 13.2); ax.set_ylim(-0.2, 9.4)
    ax.set_aspect("equal")
    clean_ax(ax)
    fig.savefig(os.path.join(OUT, "fig-p2_05_01-ecs-dataflow.png"))
    plt.close(fig)


# ============================================================ fig-p2_05_02
# Entity generation & recycling: a fixed pool of slots indexed by entity id;
# each slot has a "version" counter that ticks up when the slot is freed, so
# stale handles to a recycled entity are detected.
def fig_entity_recycle():
    fig, ax = plt.subplots(figsize=(13.2, 7.4))

    ax.text(6.6, 7.7,
            "Entity lifecycle: a slot pool + a version per slot (stale-handle detection)",
            ha="center", fontsize=14.5, weight="bold", color=INK)

    # ---- Slot pool header
    ax.text(0.3, 6.55, "slot index (entity id part):", fontsize=11.5,
            weight="bold", color=INK)
    n_slots = 6
    slot_w = 1.85
    ox0 = 0.3
    oy_row = 5.7
    labels = ["0", "1", "2", "3", "4", "5"]
    for i, lab in enumerate(labels):
        x = ox0 + i * (slot_w + 0.15)
        ax.add_patch(Rectangle((x, oy_row), slot_w, 0.55,
                               facecolor=DGREY, edgecolor="white", linewidth=1.4))
        ax.text(x + slot_w / 2, oy_row + 0.275, lab, ha="center", va="center",
                fontsize=11.5, color="white", weight="bold")

    # ---- State A: initial. Slots 0,1,2 alive; 3,4,5 free.
    ax.text(0.3, 5.05, "state A: 3 entities alive (#0, #1, #2) — versions all 0",
            fontsize=11.5, color=GREEN, weight="bold")
    state_a = [("alive", "E#0\nv0", GREEN, LGREEN),
               ("alive", "E#1\nv0", GREEN, LGREEN),
               ("alive", "E#2\nv0", GREEN, LGREEN),
               ("free",  "free\nv0", GREY,  SOFT),
               ("free",  "free\nv0", GREY,  SOFT),
               ("free",  "free\nv0", GREY,  SOFT)]
    oy_a = 4.05
    for i, (st, txt, ec, fc) in enumerate(state_a):
        x = ox0 + i * (slot_w + 0.15)
        ax.add_patch(FancyBboxPatch((x, oy_a), slot_w, 0.85,
                                    boxstyle="round,pad=0.02,rounding_size=0.05",
                                    facecolor=fc, edgecolor=ec, linewidth=1.5))
        ax.text(x + slot_w / 2, oy_a + 0.425, txt, ha="center", va="center",
                fontsize=10.5, color=INK, weight="bold")

    # ---- State B: destroy #1. Slot 1 freed, version bumped 0->1.
    ax.text(0.3, 3.35, "state B: destroy entity #1  ->  slot 1 recycled, version bumped 0 -> 1",
            fontsize=11.5, color=RED, weight="bold")
    state_b = [("alive", "E#0\nv0", GREEN, LGREEN),
               ("free",  "free\nv1", AMBER, LAMBER),
               ("alive", "E#2\nv0", GREEN, LGREEN),
               ("free",  "free\nv0", GREY,  SOFT),
               ("free",  "free\nv0", GREY,  SOFT),
               ("free",  "free\nv0", GREY,  SOFT)]
    oy_b = 2.35
    for i, (st, txt, ec, fc) in enumerate(state_b):
        x = ox0 + i * (slot_w + 0.15)
        ax.add_patch(FancyBboxPatch((x, oy_b), slot_w, 0.85,
                                    boxstyle="round,pad=0.02,rounding_size=0.05",
                                    facecolor=fc, edgecolor=ec, linewidth=1.5))
        ax.text(x + slot_w / 2, oy_b + 0.425, txt, ha="center", va="center",
                fontsize=10.5, color=INK, weight="bold")
    # highlight the bump on slot 1
    arrow(ax, (ox0 + 1 * (slot_w + 0.15) + slot_w / 2, oy_a),
          (ox0 + 1 * (slot_w + 0.15) + slot_w / 2, oy_b + 0.85),
          color=RED, lw=1.6, rad=0.0)
    ax.text(ox0 + 1 * (slot_w + 0.15) - 0.15, 3.5, "v0 -> v1", fontsize=10,
            color=RED, weight="bold", ha="right")

    # ---- State C: create() again. Returns the FIRST free slot (1), id = 1 | (1 << 20)
    ax.text(0.3, 1.65,
            "state C: create() reuses slot 1  ->  new entity id encodes the new version (v1)",
            fontsize=11.5, color=BLUE, weight="bold")
    state_c = [("alive", "E#0\nv0", GREEN, LGREEN),
               ("alive", "E#1'\nv1", BLUE,  LBLUE),
               ("alive", "E#2\nv0", GREEN, LGREEN),
               ("free",  "free\nv0", GREY,  SOFT),
               ("free",  "free\nv0", GREY,  SOFT),
               ("free",  "free\nv0", GREY,  SOFT)]
    oy_c = 0.65
    for i, (st, txt, ec, fc) in enumerate(state_c):
        x = ox0 + i * (slot_w + 0.15)
        ax.add_patch(FancyBboxPatch((x, oy_c), slot_w, 0.85,
                                    boxstyle="round,pad=0.02,rounding_size=0.05",
                                    facecolor=fc, edgecolor=ec, linewidth=1.5))
        ax.text(x + slot_w / 2, oy_c + 0.425, txt, ha="center", va="center",
                fontsize=10.5, color=INK, weight="bold")
    # note: E#1 and E#1' have the SAME slot index but DIFFERENT version.
    # Anchor the callout text up high (between state B and state C rows), clear
    # of the state labels, and point down at the reused slot-1 box in state C.
    ax.annotate("same slot index 1, but version differs: v0 (old) vs v1 (new)",
                xy=(ox0 + 1 * (slot_w + 0.15) + slot_w / 2, oy_c + 0.85),
                xytext=(ox0 + 3.0 * (slot_w + 0.15), oy_b - 0.05),
                fontsize=10.5, color=BLUE, weight="bold",
                arrowprops=dict(arrowstyle="->", color=BLUE, lw=1.4,
                                connectionstyle="arc3,rad=0.0"))

    # ---- Bit layout note, placed top-right away from state rows to avoid clutter
    box(ax, 10.0, 6.5, 3.1, 1.2,
        "32-bit id layout:\n[ 20-bit slot | 12-bit version ]\n(EnTT default, id_type = uint32)",
        fc=LPURP, ec=PURP, fs=9.8, weight="bold")

    ax.set_xlim(0, 13.4); ax.set_ylim(-0.2, 8.3)
    ax.set_aspect("equal")
    clean_ax(ax)
    fig.savefig(os.path.join(OUT, "fig-p2_05_02-entity-recycle.png"))
    plt.close(fig)


# ============================================================ fig-p2_05_03
# OOP method-bound-to-object vs System iterating components.
# Left: each object carries its own update(); iteration = virtual dispatch per obj.
# Right: one System function streams over two contiguous pools.
def fig_oop_vs_system():
    fig, axes = plt.subplots(1, 2, figsize=(13.6, 7.4))

    # ===================== LEFT: OOP
    ax = axes[0]
    ax.text(3.3, 7.8, "OOP: behavior lives inside each object",
            ha="center", fontsize=13.5, weight="bold", color=RED)

    # vtable + data per object
    obj_w, obj_h = 2.7, 2.3
    objs = [(0.4, 4.4), (3.3, 4.4), (6.2, 4.4)]
    for i, (ox, oy) in enumerate(objs):
        # whole-object frame
        ax.add_patch(FancyBboxPatch((ox, oy), obj_w, obj_h,
                                    boxstyle="round,pad=0.02,rounding_size=0.06",
                                    facecolor=LRED, edgecolor=RED, linewidth=1.4))
        # vtable slot
        ax.add_patch(Rectangle((ox + 0.18, oy + obj_h - 0.7), obj_w - 0.36, 0.5,
                               facecolor=RED, alpha=0.55, edgecolor=RED))
        ax.text(ox + obj_w / 2, oy + obj_h - 0.45, "vtable -> update()",
                ha="center", va="center", fontsize=10, color="white", weight="bold")
        # data fields
        for j, (lab, col) in enumerate([("pos", BLUE), ("vel", GREEN),
                                        ("color", GREY), ("radius", DGREY)]):
            yy = oy + obj_h - 1.15 - j * 0.42
            ax.add_patch(Rectangle((ox + 0.3, yy), obj_w - 0.6, 0.34,
                                   facecolor=col, alpha=0.30, edgecolor=col))
            ax.text(ox + obj_w / 2, yy + 0.17, lab, ha="center", va="center",
                    fontsize=9.5, color=INK)
        ax.text(ox + obj_w / 2, oy - 0.25, f"Ball{i}", ha="center",
                fontsize=10.5, weight="bold", color=INK)

    # loop: for ball in balls: ball.update()  -> one virtual call per object
    ax.text(3.3, 3.55, "for ball in balls: ball.update()",
            ha="center", fontsize=11.5, color=INK, weight="bold",
            family="monospace")
    # arrows: loop hops object to object (pointer chase)
    for i in range(len(objs) - 1):
        a = (objs[i][0] + obj_w, objs[i][1] + obj_h / 2)
        b = (objs[i + 1][0], objs[i + 1][1] + obj_h / 2)
        arrow(ax, a, b, color=RED, lw=1.5, rad=-0.15)
    ax.text(3.3, 3.0,
            "one virtual dispatch per object,\nfields scattered, color/radius dragged along",
            ha="center", fontsize=10.5, color=RED, weight="bold")

    # cost badges
    box(ax, 0.4, 1.5, 2.9, 1.0,
        "indirect call\nper object\n(vtable miss)", fc=LAMBER, ec=AMBER, fs=10, weight="bold")
    box(ax, 3.55, 1.5, 2.9, 1.0,
        "data block per obj\ncolor/radius wasted\nin movement scan", fc=LAMBER, ec=AMBER,
        fs=10, weight="bold")
    box(ax, 6.7, 1.5, 0.0, 1.0, "", fc=SOFT, ec=SOFT, fs=10)  # spacer (no-op)

    box(ax, 0.4, 0.25, 8.5, 0.95,
        "OOP cost: N virtual calls + N scattered data accesses,\n"
        "behavior is duplicated inside every object",
        fc=LRED, ec=RED, fs=10.8, weight="bold")

    ax.set_xlim(0, 9.6); ax.set_ylim(0, 8.2)
    ax.set_aspect("equal")
    clean_ax(ax)

    # ===================== RIGHT: ECS System
    ax = axes[1]
    ax.text(3.3, 7.8, "ECS: one System streams over two pools",
            ha="center", fontsize=13.5, weight="bold", color=GREEN)

    # System function (one place the behavior lives) -- monospace via raw text
    ax.add_patch(FancyBboxPatch((0.5, 6.3), 8.3, 1.1,
                                boxstyle="round,pad=0.02,rounding_size=0.06",
                                facecolor=LGREEN, edgecolor=GREEN, linewidth=1.4))
    ax.text(0.5 + 8.3 / 2, 6.3 + 1.1 / 2,
            "MovementSystem::update(registry):\n"
            "  for (e, [pos, vel]) in view<Position, Velocity>.each():  pos += vel",
            ha="center", va="center", fontsize=10.8, weight="bold",
            color=INK, family="monospace")

    # two contiguous pools
    pool_labels = [("Position[]", BLUE), ("Velocity[]", GREEN)]
    pool_oy = [4.5, 3.0]
    cell_w, cell_h = 1.45, 0.55
    n = 5
    for (lab, col), oy in zip(pool_labels, pool_oy):
        ax.text(0.4, oy + cell_h / 2, lab, fontsize=10.5, weight="bold",
                color=INK, ha="left", va="center")
        ox = 2.0
        for k in range(n):
            ax.add_patch(Rectangle((ox + k * (cell_w + 0.1), oy), cell_w, cell_h,
                                   facecolor=col, alpha=0.32, edgecolor=col,
                                   linewidth=1.2))
            ax.text(ox + k * (cell_w + 0.1) + cell_w / 2, oy + cell_h / 2,
                    f"#{k}", ha="center", va="center", fontsize=9.5, color=INK)
    # cache line bracket over Position[]
    cl_x = 2.0
    ax.add_patch(Rectangle((cl_x - 0.05, pool_oy[0] - 0.05),
                           n * (cell_w + 0.1) + 0.1, cell_h + 0.1,
                           fill=False, edgecolor=AMBER, linewidth=1.8,
                           linestyle="--"))
    ax.text(cl_x + n * (cell_w + 0.1) / 2, pool_oy[0] + cell_h + 0.2,
            "one cache line covers several positions", ha="center",
            fontsize=10, color=AMBER, weight="bold")

    # streaming arrow across both pools
    arrow(ax, (2.0, 2.55), (2.0 + n * (cell_w + 0.1), 2.55),
          color=GREEN, lw=2.0)
    ax.text(2.0 + n * (cell_w + 0.1) / 2, 2.3,
            "sequential streaming: pos += vel, no per-object dispatch",
            ha="center", fontsize=10.5, color=GREEN, weight="bold")

    # cost badges
    box(ax, 0.4, 0.95, 4.0, 1.05,
        "ONE function\n(no virtual calls),\nbehavior lives in one place",
        fc=LBLUE, ec=BLUE, fs=10, weight="bold")
    box(ax, 4.6, 0.95, 4.2, 1.05,
        "data contiguous,\ncolor/radius untouched,\nSIMD-ready",
        fc=LGREEN, ec=GREEN, fs=10, weight="bold")

    box(ax, 0.4, 0.0, 8.4, 0.78,
        "ECS cost: tight loop over 2 arrays, behavior factored out of data",
        fc=LGREEN, ec=GREEN, fs=10.8, weight="bold")

    ax.set_xlim(0, 9.6); ax.set_ylim(-0.4, 8.2)
    ax.set_aspect("equal")
    clean_ax(ax)

    fig.tight_layout()
    fig.savefig(os.path.join(OUT, "fig-p2_05_03-oop-vs-system.png"))
    plt.close(fig)


if __name__ == "__main__":
    fig_ecs_dataflow()
    fig_entity_recycle()
    fig_oop_vs_system()
    print("done:", sorted(f for f in os.listdir(OUT)
                          if f.startswith("fig-p2_05_") and f.endswith(".png")))
