# -*- coding: utf-8 -*-
"""Appendix B of the game-engine book: a minimal runnable ECS with EnTT.
Generates three PNGs (English labels, Chinese text explains them in the chapter).

Figures:
  fig-appB_01-aos-vs-entt-layout.png     Memory layout: AoS Ball[] vs EnTT three pools (SoA)
  fig-appB_02-bench-aos-vs-entt.png       Per-frame update time: naive AoS vs EnTT SoA, growing entity count
  fig-appB_03-view-filter.png             view<Position, Velocity> skips entities without both components

Run:  python gen_appendix_b_figures.py
"""
import os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, FancyBboxPatch, FancyArrowPatch
import numpy as np

plt.rcParams.update({
    "font.family": "DejaVu Sans",
    "font.size": 12.5,
    "axes.linewidth": 1.2,
    "figure.dpi": 150,
    "savefig.dpi": 150,
    "savefig.bbox": "tight",
    "savefig.facecolor": "white",
    "savefig.pad_inches": 0.18,
})

OUT = os.path.dirname(os.path.abspath(__file__))

# fixed palette shared across the series
RED   = (0.86, 0.18, 0.18)
GREEN = (0.20, 0.70, 0.32)
BLUE  = (0.20, 0.45, 0.88)
AMBER = (0.95, 0.70, 0.18)
GREY  = (0.78, 0.78, 0.80)
DGREY = (0.55, 0.55, 0.60)
INK   = (0.10, 0.10, 0.10)
SOFT  = (0.96, 0.96, 0.97)
LBLUE = (0.90, 0.94, 1.0)
LGREEN = (0.88, 0.94, 0.88)
LAMBER = (1.0, 0.94, 0.82)
LRED  = (1.0, 0.90, 0.90)
PURP  = (0.55, 0.36, 0.74)
LPURP = (0.94, 0.88, 0.98)


def clean_ax(ax):
    for s in ("top", "right", "bottom", "left"):
        ax.spines[s].set_visible(False)
    ax.set_xticks([]); ax.set_yticks([])


# ============================================================
# Fig 1: memory layout  AoS Ball[]  vs  EnTT three pools (SoA)
# ============================================================
def fig1_layout():
    fig, axes = plt.subplots(2, 1, figsize=(13.6, 8.2))

    # ---------- AoS Ball[] ----------
    ax = axes[0]
    clean_ax(ax)
    ax.set_xlim(0, 32); ax.set_ylim(0, 6.4)
    ax.set_title("Naive OOP:   std::vector<Ball>   (AoS, fields of one ball packed together)",
                 fontsize=13, weight="bold", color=INK, loc="left", pad=10)

    # fields: x y vx vy  r g b  rad   (Ball = 8 floats = 32B)
    field_colors = [BLUE, BLUE, GREEN, GREEN, AMBER, AMBER, AMBER, GREY]
    field_labels = ["x", "y", "vx", "vy", "r", "g", "b", "rad"]
    used = [True, True, True, True, False, False, False, False]

    cell_w = 1.0
    cell_h = 1.0
    n_obj = 4
    start_x = 0.5
    y0 = 3.0

    for obj in range(n_obj):
        ox = start_x + obj * (8 * cell_w + 0.5)
        ax.text(ox + 4 * cell_w, y0 + cell_h + 0.55, f"Ball[{obj}]",
                ha="center", va="center", fontsize=11.5, weight="bold", color=INK)
        for f in range(8):
            cx = ox + f * cell_w
            fc = field_colors[f]
            if used[f]:
                face = fc; edge = INK; lw = 1.3; alpha = 1.0
            else:
                face = fc; edge = DGREY; lw = 0.8; alpha = 0.55
            ax.add_patch(Rectangle((cx, y0), cell_w, cell_h, facecolor=face,
                                   edgecolor=edge, linewidth=lw, alpha=alpha))
            ax.text(cx + cell_w / 2, y0 + cell_h / 2, field_labels[f],
                    ha="center", va="center", fontsize=9.5, color="white", weight="bold")

    ax.text(start_x + 16, y0 - 0.7,
            "MovementSystem touches only x,y,vx,vy  ->  r,g,b,rad dragged into cache lines (wasted)",
            fontsize=10.5, color=RED, ha="center", va="center", weight="bold")
    ax.text(start_x, y0 - 1.5, "vptr (if virtual ~update): +8B per object, 3 extra misses per call",
            fontsize=9.5, color=DGREY, ha="left", va="center", style="italic")

    # ---------- EnTT three pools ----------
    ax = axes[1]
    clean_ax(ax)
    ax.set_xlim(0, 32); ax.set_ylim(0, 7.6)
    ax.set_title("EnTT:   one pool per component type   (Position[] / Velocity[] / Color[] are separate, contiguous)",
                 fontsize=13, weight="bold", color=INK, loc="left", pad=10)

    pools = [
        ("Position[]", BLUE,  ["x", "y"],       "used by MovementSystem", LGREEN),
        ("Velocity[]", GREEN, ["vx", "vy"],     "used by MovementSystem", LGREEN),
        ("Color[]",    AMBER, ["r", "g", "b"], "untouched by MovementSystem", LAMBER),
    ]

    n_elem = 8
    y_top = 6.6
    for pi, (name, col, sub, note, note_bg) in enumerate(pools):
        py = y_top - pi * 1.9
        ax.text(0.3, py + 0.5, name, fontsize=12, weight="bold", color=col, ha="left", va="center")
        ax.text(0.3, py - 0.05, "pool dense[]", fontsize=9, color=DGREY, ha="left", va="center", style="italic")
        # draw n_elem elements, each containing its sub-fields
        sub_n = len(sub)
        elem_w = sub_n * 0.55
        for i in range(n_elem):
            cx = 4.6 + i * (elem_w + 0.18)
            for si, sname in enumerate(sub):
                ax.add_patch(Rectangle((cx + si * 0.55, py), 0.52, 0.78,
                                       facecolor=col, edgecolor=INK, linewidth=0.9))
                ax.text(cx + si * 0.55 + 0.26, py + 0.39, sname,
                        fontsize=8, ha="center", va="center", color="white", weight="bold")
            ax.text(cx + elem_w / 2, py - 0.22, f"[{i}]", fontsize=7.5,
                    ha="center", va="center", color=DGREY)
        ax.text(3.4 + n_elem * (elem_w + 0.18) + 0.3, py + 0.39, note,
                fontsize=10, color=INK, ha="left", va="center",
                bbox=dict(boxstyle="round,pad=0.25", facecolor=note_bg, edgecolor=DGREY, lw=0.7))

    # cache line bracket over Position pool first 8 x's (and 8 y's): a 64B line holds 8 Position = 8*(x,y)
    cl_y = y_top + 0.95
    cl_x0 = 4.6
    cl_w = 8 * (2 * 0.55) + 7 * 0.18
    ax.add_patch(Rectangle((cl_x0, cl_y), cl_w, 0.16, facecolor=RED, edgecolor=RED))
    ax.text(cl_x0 + cl_w / 2, cl_y + 0.5,
            "64B cache line = 8 whole Positions   (100% useful to MovementSystem)",
            ha="center", va="center", fontsize=10.5, color=RED, weight="bold")

    ax.text(0.4, 0.5,
            "Each pool's dense[] is contiguous; view<Position, Velocity> only walks Position[] and Velocity[].",
            fontsize=10.5, color=INK, ha="left", va="center", style="italic")

    plt.subplots_adjust(hspace=0.55)
    fig.savefig(os.path.join(OUT, "fig-appB_01-aos-vs-entt-layout.png"))
    plt.close(fig)
    print("  saved fig-appB_01-aos-vs-entt-layout.png")


# ============================================================
# Fig 2: per-frame update time  AoS (vector<Ball>) vs EnTT SoA
# log-log, simulated but reflects the typical measured ratio (~2x-6x)
# ============================================================
def fig2_bench():
    fig, ax = plt.subplots(figsize=(12.6, 7.2))

    n = np.array([1000, 2000, 5000, 10000, 20000, 50000, 100000, 200000], dtype=float)

    rng = np.random.default_rng(42)

    # ns per element, reflecting cache behavior described in P2-06
    # EnTT SoA: tight loop on two contiguous pools, ~0.45 ns/elem (best case, prefetcher ahead)
    base_entt = 0.45
    # vector<Ball> AoS contiguous: ~1.05 ns/elem (8 floats/obj, but only 4 used -> ~50% useful bytes)
    base_aos = 1.05
    # scattered OOP (vector<Ball*> + virtual): heavy pointer chasing, super-linear after L2/L3 saturation
    obj_bytes = 32.0
    def scattered_time(nn):
        ws = nn * obj_bytes
        t = 2.4 + 0.0 * nn
        t = np.where(ws > 32e3,  t + 1.6, t)   # blew L1
        t = np.where(ws > 256e3, t + 3.2, t)   # blew L2
        t = np.where(ws > 8e6,   t + 9.0, t)   # blew L3
        return t

    t_entt = base_entt * n * (1.0 + 0.025 * rng.random(len(n)))
    t_aos  = base_aos  * n * (1.0 + 0.025 * rng.random(len(n)))
    t_scat = scattered_time(n) * n / 4.0 * (1.0 + 0.03 * rng.random(len(n)))

    ax.loglog(n, t_scat, "-o", color=RED,   lw=2.3, ms=7, label="std::vector<Ball*> + virtual  (pointer chasing)")
    ax.loglog(n, t_aos,  "-s", color=AMBER, lw=2.3, ms=7, label="std::vector<Ball>  (AoS, contiguous)")
    ax.loglog(n, t_entt, "-^", color=GREEN, lw=2.6, ms=8, label="EnTT  view<Position, Velocity>  (SoA pools)")

    ax.set_xlabel("number of entities", fontsize=12.5)
    ax.set_ylabel("per-frame update time  (relative, ns-equivalent, log)", fontsize=12)
    ax.set_title("Appendix B benchmark:  naive OOP vs EnTT ECS  (MovementSystem, log-log, simulated)",
                 fontsize=13, weight="bold", color=INK, loc="left", pad=12)
    ax.grid(True, which="both", ls=":", alpha=0.5)
    ax.legend(fontsize=11, loc="upper left", framealpha=0.96)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)

    # annotate the ratio at n=50000
    i50 = list(n).index(50000)
    ax.annotate(f"EnTT ~{t_aos[i50]/t_entt[i50]:.1f}x faster\nthan contiguous AoS",
                xy=(n[i50], t_entt[i50]), xytext=(2200, t_entt[i50] * 0.35),
                fontsize=11, color=GREEN, weight="bold",
                arrowprops=dict(arrowstyle="->", color=GREEN, lw=1.5))
    ax.annotate(f"pointer chasing blows up\nafter L2 (~256KB) and L3 (~8MB)",
                xy=(n[i50], t_scat[i50]), xytext=(1500, t_scat[i50] * 0.45),
                fontsize=10.5, color=RED, weight="bold",
                arrowprops=dict(arrowstyle="->", color=RED, lw=1.4))

    # frame budget line: 16ms = 16,000,000 ns; show how many entities fit
    # but our y-axis is per-element * n already in relative units, so just hint the budget qualitatively
    ax.text(n[-1] * 0.95, t_entt[-1] * 0.5,
            "smaller is better  ->  EnTT stays flat & low",
            fontsize=10.5, color=GREEN, ha="right", va="center", style="italic",
            bbox=dict(boxstyle="round,pad=0.3", facecolor=LGREEN, edgecolor=GREEN, lw=0.8))

    fig.savefig(os.path.join(OUT, "fig-appB_02-bench-aos-vs-entt.png"))
    plt.close(fig)
    print("  saved fig-appB_02-bench-aos-vs-entt.png")


# ============================================================
# Fig 3: view<Position, Velocity> filtering
# show 8 entities: some have Position+Velocity, some only Position, some +Health
# view walks only the ones with BOTH, skipping the rest at the pool level
# ============================================================
def fig3_view_filter():
    fig, ax = plt.subplots(figsize=(13.6, 7.6))
    clean_ax(ax)
    ax.set_xlim(0, 28); ax.set_ylim(0, 16)
    ax.set_title("view<Position, Velocity>  iterates ONLY entities that have BOTH components",
                 fontsize=13.5, weight="bold", color=INK, loc="left", pad=12)

    # entities along the top
    # tuple: (id, has_pos, has_vel, has_health)
    ents = [
        (0,  True,  True,  False),
        (1,  True,  False, False),
        (2,  True,  True,  True),
        (3,  False, True,  False),
        (4,  True,  True,  False),
        (5,  True,  False, True),
        (6,  True,  True,  True),
        (7,  False, False, True),
    ]

    # three pools, each only contains the entities that have that component
    # we draw the dense[] arrays for Position, Velocity, Health
    pools = [
        ("Position pool  dense[]", BLUE,  0, lambda e: e[1]),
        ("Velocity pool  dense[]", GREEN, 1, lambda e: e[2]),
        ("Health    pool  dense[]", RED,  2, lambda e: e[3]),
    ]

    y_top = 13.2
    for pi, (name, col, _, has) in enumerate(pools):
        py = y_top - pi * 3.6
        ax.text(0.3, py + 1.2, name, fontsize=12, weight="bold", color=col, ha="left", va="center")
        # which entities are in this pool?
        in_pool = [e for e in ents if has(e)]
        for i, e in enumerate(in_pool):
            cx = 3.6 + i * 2.55
            ax.add_patch(Rectangle((cx, py), 2.2, 0.95,
                                   facecolor=col, edgecolor=INK, linewidth=1.2))
            ax.text(cx + 1.1, py + 0.48, f"e{e[0]}", fontsize=10.5,
                    ha="center", va="center", color="white", weight="bold")
            ax.text(cx + 1.1, py - 0.25, f"dense[{i}]", fontsize=8,
                    ha="center", va="center", color=DGREY)
        # blank hint
        first_free_x = 3.6 + len(in_pool) * 2.55 + 0.4
        ax.text(first_free_x, py + 0.48,
                f"({len(ents) - len(in_pool)} entities lack this component, not stored here)",
                fontsize=9.5, color=DGREY, ha="left", va="center", style="italic")

    # bottom: view<Position, Velocity> = intersection of the two pools
    # which entities have BOTH?
    both = [e for e in ents if e[1] and e[2]]
    view_y = 1.4
    ax.text(0.3, view_y + 1.0, "view<Position, Velocity>  yields:",
            fontsize=12.5, weight="bold", color=PURP, ha="left", va="center")
    # highlight matched entities in purple boxes
    for i, e in enumerate(both):
        cx = 3.6 + i * 2.55
        ax.add_patch(FancyBboxPatch((cx, view_y), 2.2, 0.95,
                                    boxstyle="round,pad=0.02,rounding_size=0.08",
                                    facecolor=LPURP, edgecolor=PURP, linewidth=1.6))
        ax.text(cx + 1.1, view_y + 0.48, f"e{e[0]}",
                fontsize=11, ha="center", va="center", color=PURP, weight="bold")

    # excluded entities list (note which had Health)
    excluded = [e[0] for e in ents if not (e[1] and e[2])]
    ax.text(3.6 + len(both) * 2.55 + 0.4, view_y + 0.48,
            f"skipped:  {excluded}  (missing one of the two)",
            fontsize=10.5, color=RED, ha="left", va="center", weight="bold")

    # arrows: matched entities are the intersection of Position and Velocity pools
    # draw two faint connectors from Position pool e0/e2/e4/e6 -> view row
    matched_ids = [e[0] for e in both]
    # positions in Position pool
    pos_in_pool = [e[0] for e in ents if e[1]]
    vel_in_pool = [e[0] for e in ents if e[2]]
    for mid in matched_ids:
        # Position pool index
        pi = pos_in_pool.index(mid)
        # Velocity pool index
        vi = vel_in_pool.index(mid)
        # view index
        wi = matched_ids.index(mid)
        # source x: in Position pool row (py = y_top)
        sx_p = 3.6 + pi * 2.55 + 1.1
        sy_p = y_top + 0.0
        sx_v = 3.6 + vi * 2.55 + 1.1
        sy_v = y_top - 3.6 + 0.0
        tx = 3.6 + wi * 2.55 + 1.1
        ty = view_y + 0.95
        ax.add_artist(FancyArrowPatch((sx_p, sy_p), (tx, ty),
                                      arrowstyle="-|>", color=PURP, lw=0.9,
                                      connectionstyle="arc3,rad=-0.18", alpha=0.55))
        ax.add_artist(FancyArrowPatch((sx_v, sy_v), (tx, ty),
                                      arrowstyle="-|>", color=PURP, lw=0.9,
                                      connectionstyle="arc3,rad=-0.18", alpha=0.55))

    # health annotation: two of the matched entities (e2, e6) ALSO have Health
    ax.text(0.3, 0.3,
            "Note:  e2 and e6 also carry a Health component  ->  Archetype-style grouping would put them\n"
            "in a separate archetype from {Position, Velocity}-only entities (see P2-08 / P2-09).",
            fontsize=10, color=INK, ha="left", va="center", style="italic",
            bbox=dict(boxstyle="round,pad=0.35", facecolor=SOFT, edgecolor=DGREY, lw=0.8))

    fig.savefig(os.path.join(OUT, "fig-appB_03-view-filter.png"))
    plt.close(fig)
    print("  saved fig-appB_03-view-filter.png")


if __name__ == "__main__":
    print("Generating Appendix B figures ...")
    fig1_layout()
    fig2_bench()
    fig3_view_filter()
    print("Done.")
