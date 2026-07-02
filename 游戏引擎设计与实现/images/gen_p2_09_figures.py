# -*- coding: utf-8 -*-
"""为《游戏引擎》P2-09 (查询 Query: 快速找"有这些组件的实体") 生成一组 PNG 示意图。
图内英文标注, 正文中文解释。运行: python gen_p2_09_figures.py

Figures:
  fig-p2_09_01-bitset-match.png         Query 用组件位掩码匹配 archetype:
                                        每个 archetype 一个组件位掩码, Query 检查
                                        "Query 的 with 位是 archetype 位的子集"
  fig-p2_09_02-view-iterator.png        view/Query 只扫匹配的 archetype, 返回连续迭代器
                                        (matched_archetypes.ones() / leading storage)
  fig-p2_09_03-archetype-vs-sparse.png  EnTT 稀疏集合 view (取最小池驱动+逐实体 contains)
                                        vs Bevy archetype view (位集筛 archetype 再扫表)
  fig-p2_09_04-naive-vs-query.png       朴素遍历 vs Query 匹配: 耗时随实体数 (数值模拟)
"""
import os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, FancyArrowPatch, FancyBboxPatch
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


def bitcell(ax, x, y, val, w=0.62, h=0.62, on_col=GREEN, off_col=GREY,
            label=None, fs=11):
    col = on_col if val == 1 else off_col
    ax.add_patch(Rectangle((x, y), w, h, facecolor=col, edgecolor="white",
                           linewidth=1.6))
    ax.text(x + w / 2, y + h / 2, str(val), ha="center", va="center",
            fontsize=fs, color="white", weight="bold")
    if label is not None:
        ax.text(x + w / 2, y - 0.30, label, ha="center", va="top",
                fontsize=9.2, color=DGREY)


# ============================================================ fig-p2_09_01
# Bitmask / archetype matching. Each archetype has a component bitmask over the
# fixed ComponentId axis; a Query is a (with, without) mask pair; match iff
# (archetype_bits & with_mask) == with_mask  AND  (archetype_bits & without_mask) == 0.
def fig_bitset_match():
    fig, ax = plt.subplots(figsize=(13.6, 9.2))

    ax.text(6.8, 9.85, "Query matching: a component bitmask per archetype",
            ha="center", fontsize=15, weight="bold", color=INK)
    ax.text(6.8, 9.40,
            "Each ComponentId is one bit. An archetype matches a Query iff its "
            "bits are a superset of the Query's required bits.",
            ha="center", fontsize=11.5, color=DGREY, style="italic")

    # ---- Component axis header (5 components)
    comps = ["Position", "Velocity", "Health", "Color", "AI"]
    ax.text(0.45, 8.55, "ComponentId axis ->", fontsize=11.5, weight="bold",
            color=INK, ha="left")
    ox = 2.0
    cw = 0.62
    for i, c in enumerate(comps):
        ax.text(ox + i * (cw + 0.06) + cw / 2, 8.55, c, ha="center",
                va="center", fontsize=9.8, color=INK, weight="bold")

    # ---- Query definition box (Position AND Velocity, NOT AI)
    box(ax, 0.3, 7.35, 1.55, 0.95, "Query:\nwith {Pos, Vel}\nwithout {AI}",
        fc=LBLUE, ec=BLUE, fs=9.5, weight="bold")
    # Query with-mask
    ax.text(2.0, 7.95, "with-mask:", fontsize=9.5, weight="bold", color=BLUE,
            ha="left")
    q_with = [1, 1, 0, 0, 0]
    for i, b in enumerate(q_with):
        bitcell(ax, ox + i * (cw + 0.06), 7.40, b, on_col=BLUE, off_col=GREY)
    # Query without-mask
    ax.text(2.0, 7.05, "without-mask:", fontsize=9.5, weight="bold", color=RED,
            ha="left")
    q_without = [0, 0, 0, 0, 1]
    for i, b in enumerate(q_without):
        bitcell(ax, ox + i * (cw + 0.06), 6.50, b, on_col=RED, off_col=GREY)

    # ---- Four archetypes, each with a bitmask + a match verdict
    archetypes = [
        # (name, member hint, bits,                              verdict, color)
        ("Archetype A", "{Pos, Vel, Color}",    [1, 1, 0, 1, 0], True,  GREEN),
        ("Archetype B", "{Pos, Vel, Health}",   [1, 1, 1, 0, 0], True,  GREEN),
        ("Archetype C", "{Pos, Color}",         [1, 0, 0, 1, 0], False, RED),
        ("Archetype D", "{Pos, Vel, AI}",       [1, 1, 0, 0, 1], False, RED),
    ]
    y0 = 5.55
    row_h = 1.05
    for i, (name, hint, bits, ok, col) in enumerate(archetypes):
        ry = y0 - i * row_h
        # name + hint
        box(ax, 0.3, ry, 1.55, 0.75,
            f"{name}\n{hint}", fc=SOFT, ec=INK, fs=9.2, weight="bold")
        # bitmask cells
        for j, b in enumerate(bits):
            bitcell(ax, ox + j * (cw + 0.06), ry + 0.07, b,
                    on_col=AMBER if b else GREY)
        # verdict
        if ok:
            verdict_txt = "MATCH\n(Pos & Vel present, AI absent)"
            vc = LGREEN
        else:
            # explain why
            if name == "Archetype C":
                verdict_txt = "NO MATCH\n(Velocity bit missing)"
            else:
                verdict_txt = "NO MATCH\n(AI bit set -> excluded)"
            vc = LRED
        box(ax, 6.55, ry, 4.0, 0.75, verdict_txt, fc=vc, ec=col,
            fs=9.5, weight="bold")

    # ---- Bottom: the key arithmetic
    box(ax, 0.3, 0.55, 12.9, 0.95,
        "Match test  (cheap, O(components), independent of entity count):\n"
        "(archetype_bits & with_mask) == with_mask   AND   "
        "(archetype_bits & without_mask) == 0",
        fc=LPURP, ec=PURP, fs=10.8, weight="bold")

    ax.set_xlim(0, 13.6); ax.set_ylim(0.2, 10.2)
    ax.set_aspect("equal")
    clean_ax(ax)
    fig.savefig(os.path.join(OUT, "fig-p2_09_01-bitset-match.png"))
    plt.close(fig)


# ============================================================ fig-p2_09_02
# A view/Query only scans the matching archetypes/storages, returning a
# contiguous iterator over their densely-packed rows.
def fig_view_iterator():
    fig, ax = plt.subplots(figsize=(13.6, 8.4))

    ax.text(6.8, 8.95, "A view iterates only matched archetypes, densely",
            ha="center", fontsize=15, weight="bold", color=INK)
    ax.text(6.8, 8.50,
            "The engine precomputes matched_archetypes (a bitset); "
            "the view walks the set bits and streams each table's rows.",
            ha="center", fontsize=11.5, color=DGREY, style="italic")

    # ---- World: list of archetypes with their component sets & entity counts
    archs = [
        # (id, comps, n, matched)
        ("A", "Pos, Vel, Color",   120, True),
        ("B", "Pos, Vel, Health",  200, True),
        ("C", "Pos, Color",         60, False),
        ("D", "Pos, Vel, AI",       40, False),
    ]
    ax.text(0.3, 7.85, "World archetypes:", fontsize=12, weight="bold",
            color=INK)

    # matched_archetypes bitset row
    box(ax, 0.3, 6.95, 3.1, 0.75, "matched_archetypes\n(FixedBitSet)",
        fc=LPURP, ec=PURP, fs=10, weight="bold")
    bx0 = 3.6
    for i, (_, _, _, m) in enumerate(archs):
        col = GREEN if m else GREY
        ax.add_patch(Rectangle((bx0 + i * 0.78, 6.95), 0.7, 0.75,
                               facecolor=col, edgecolor="white", linewidth=1.8))
        ax.text(bx0 + i * 0.78 + 0.35, 6.95 + 0.375,
                "1" if m else "0", ha="center", va="center",
                fontsize=12, color="white", weight="bold")
        ax.text(bx0 + i * 0.78 + 0.35, 6.78, archs[i][0],
                ha="center", va="top", fontsize=9, color=DGREY)

    ax.text(bx0, 6.32, ".ones() yields only the set bits -> archetype A, B",
            fontsize=10, color=PURP, weight="bold", ha="left")

    # ---- Archetype tables (matched ones drawn as full contiguous blocks)
    ty0 = 5.4
    table_w = 2.7
    gap = 0.35
    x = 0.3
    matched_tables = []
    for i, (aid, comps, n, m) in enumerate(archs):
        ec = GREEN if m else GREY
        fc = LGREEN if m else SOFT
        # header
        box(ax, x, ty0, table_w, 0.7,
            f"Archetype {aid}\n{comps}", fc=fc, ec=ec, fs=9.5, weight="bold")
        # entity rows (draw a fixed small number of cells, then a count line)
        rows_to_draw = 4
        for r in range(rows_to_draw):
            ax.add_patch(Rectangle((x, ty0 - 0.45 - r * 0.4), table_w, 0.36,
                                   facecolor=BLUE if m else DGREY,
                                   alpha=0.55 if m else 0.25,
                                   edgecolor="white", linewidth=1.0))
            ax.text(x + table_w / 2, ty0 - 0.27 - r * 0.4,
                    f"entity + components",
                    ha="center", va="center", fontsize=7.8, color="white")
        foot_y = ty0 - 0.45 - rows_to_draw * 0.4 - 0.15
        if m:
            ax.text(x + table_w / 2, foot_y,
                    f"... ({n} rows densely packed)", ha="center",
                    fontsize=9, color=GREEN, weight="bold")
            matched_tables.append((x, table_w))
        else:
            ax.text(x + table_w / 2, foot_y,
                    f"({n} entities, SKIPPED)", ha="center",
                    fontsize=9, color=GREY, weight="bold")
            # hatch overlay to suggest "not iterated"
            ax.add_patch(Rectangle((x, ty0 - 0.5 - rows_to_draw * 0.4),
                                   table_w, 0.4 + rows_to_draw * 0.4,
                                   facecolor="none", edgecolor=GREY,
                                   linewidth=1.2, hatch="///", alpha=0.5))
        x += table_w + gap

    # ---- View iteration arrow streaming over the matched tables
    stream_y = ty0 - 0.45 - rows_to_draw * 0.4 - 1.05
    arrow(ax, (matched_tables[0][0] + table_w / 2, stream_y + 0.4),
          (matched_tables[1][0] + table_w / 2, stream_y + 0.4),
          color=GREEN, lw=2.4)
    ax.text((matched_tables[0][0] + matched_tables[1][0]) / 2 + table_w / 2,
            stream_y + 0.05,
            "for (pos, vel) in query.iter():  pos += vel",
            ha="center", fontsize=11, color=GREEN, weight="bold",
            family="monospace")

    # ---- Cost callouts
    box(ax, 0.3, 2.55, 4.2, 1.05,
        "Iterate ONLY matched tables:\nno per-entity has(Component) check,\n"
        "no branch on entity type",
        fc=LGREEN, ec=GREEN, fs=10, weight="bold")
    box(ax, 4.75, 2.55, 4.2, 1.05,
        "Within a table, rows are packed:\nlinear scan, prefetcher happy,\n"
        "SIMD-ready",
        fc=LBLUE, ec=BLUE, fs=10, weight="bold")
    box(ax, 9.2, 2.55, 4.1, 1.05,
        "Cost = O(matched entities),\nNOT O(total entities).\n"
        "Archetypes with no match are free.",
        fc=LAMBER, ec=AMBER, fs=10, weight="bold")

    box(ax, 0.3, 1.35, 13.0, 0.95,
        "Key insight: filtering is done ONCE per archetype (cheap), "
        "not once per entity. With K archetypes and N entities, the "
        "filtering cost is O(K), the iteration cost is O(matched entities).",
        fc=SOFT, ec=INK, fs=10.8, weight="bold")

    ax.set_xlim(0, 13.6); ax.set_ylim(0.6, 9.4)
    ax.set_aspect("equal")
    clean_ax(ax)
    fig.savefig(os.path.join(OUT, "fig-p2_09_02-view-iterator.png"))
    plt.close(fig)


# ============================================================ fig-p2_09_03
# Two storage strategies, two iteration shapes.
# Left:  EnTT sparse-set view -> pick the SMALLEST pool as the leader, walk it,
#         for each entity check `contains` on the other pools (multi-pass filter).
# Right: Bevy archetype view -> a bitset pre-filters which archetypes match;
#         iteration streams every row of every matched table (no per-entity check).
def fig_arch_vs_sparse():
    fig, axes = plt.subplots(1, 2, figsize=(13.8, 8.0))

    # ===================== LEFT: EnTT sparse-set view
    ax = axes[0]
    ax.text(3.4, 7.65, "EnTT: sparse-set view, smallest pool leads",
            ha="center", fontsize=13, weight="bold", color=BLUE)

    # two pools of different sizes (Velocity smaller)
    pools = [("Position[]", 6, BLUE), ("Velocity[]", 4, GREEN)]
    pool_oy = 5.2
    cell_w, cell_h = 0.95, 0.5
    pool_x = 0.7
    pool_ids = {0: [3, 7, 9, 12, 18, 25], 1: [7, 9, 18, 25]}
    drawn = {}
    for idx, (lab, n, col) in enumerate(pools):
        oy = pool_oy - idx * 1.15
        ax.text(pool_x, oy + cell_h / 2, lab, fontsize=10.5, weight="bold",
                ha="left", va="center", color=INK)
        ox = 2.2
        ids = pool_ids[idx]
        for k in range(n):
            ax.add_patch(Rectangle((ox + k * (cell_w + 0.1), oy), cell_w, cell_h,
                                   facecolor=col, alpha=0.32, edgecolor=col,
                                   linewidth=1.2))
            ax.text(ox + k * (cell_w + 0.1) + cell_w / 2, oy + cell_h / 2,
                    f"#{ids[k]}", ha="center", va="center",
                    fontsize=8.8, color=INK, weight="bold")
        drawn[idx] = (ox, oy)
        if idx == 1:
            # mark "smallest, leads iteration"
            ax.add_patch(Rectangle((ox - 0.08, oy - 0.08),
                                   n * (cell_w + 0.1) + 0.16, cell_h + 0.16,
                                   fill=False, edgecolor=AMBER, linewidth=2.0,
                                   linestyle="--"))
            ax.text(ox + n * (cell_w + 0.1) / 2, oy + cell_h + 0.22,
                    "smallest pool -> drives iteration",
                    ha="center", fontsize=9.5, color=AMBER, weight="bold")

    # iteration: walk Velocity (4), check contains in Position for each
    vel_ox, vel_oy = drawn[1]
    pos_ox, pos_oy = drawn[0]
    for k in range(4):
        # bracket under each velocity cell
        bx = vel_ox + k * (cell_w + 0.1)
        # a check mark if its id is in position (always true here)
        ax.text(bx + cell_w / 2, vel_oy - 0.28, "contains?\nyes",
                ha="center", va="top", fontsize=8.2, color=GREEN,
                weight="bold")
    arrow(ax, (vel_ox, vel_oy - 0.75),
          (vel_ox + 3 * (cell_w + 0.1) + cell_w, vel_oy - 0.75),
          color=BLUE, lw=1.8)
    ax.text(vel_ox + 1.5 * (cell_w + 0.1), vel_oy - 0.95,
            "walk 4 entities, each probed in the other pool",
            ha="center", fontsize=9.3, color=INK, weight="bold")

    box(ax, 0.4, 1.7, 7.6, 1.05,
        "Cost = O(smallest pool) probes;\neach probe is an O(1) sparse-set "
        "lookup.\nOnly entities in ALL get-pools survive.",
        fc=LBLUE, ec=BLUE, fs=10, weight="bold")
    box(ax, 0.4, 0.55, 7.6, 0.95,
        "Pro: pools are independent, adding/removing a component is cheap.\n"
        "Con: per-entity `contains` checks, harder to vectorize.",
        fc=SOFT, ec=INK, fs=10, weight="bold")

    ax.set_xlim(0, 8.4); ax.set_ylim(0.2, 8.0)
    ax.set_aspect("equal")
    clean_ax(ax)

    # ===================== RIGHT: Bevy archetype view
    ax = axes[1]
    ax.text(3.4, 7.65, "Bevy: archetype view, bitset-prefiltered",
            ha="center", fontsize=13, weight="bold", color=GREEN)

    # matched_archetypes bitset
    box(ax, 0.4, 6.55, 3.0, 0.75, "matched_archetypes",
        fc=LPURP, ec=PURP, fs=10, weight="bold")
    bx0 = 3.6
    bits = [1, 1, 0, 0, 1]
    for i, b in enumerate(bits):
        col = GREEN if b else GREY
        ax.add_patch(Rectangle((bx0 + i * 0.6, 6.55), 0.52, 0.75,
                               facecolor=col, edgecolor="white", linewidth=1.6))
        ax.text(bx0 + i * 0.6 + 0.26, 6.55 + 0.375,
                str(b), ha="center", va="center", fontsize=11,
                color="white", weight="bold")

    ax.text(bx0, 6.20, ".ones() -> archetypes 0,1,4 to scan",
            fontsize=9.8, color=PURP, weight="bold", ha="left")

    # three matched tables, stream all rows
    tables = [("T0", 5), ("T1", 5), ("T4", 4)]
    tx = 0.4
    tw = 2.5
    ty = 5.0
    cellh = 0.34
    for i, (lab, n) in enumerate(tables):
        ax.text(tx + tw / 2, ty + 0.55, lab, ha="center", fontsize=10.5,
                weight="bold", color=GREEN)
        for r in range(n):
            ax.add_patch(Rectangle((tx, ty - r * (cellh + 0.04)), tw, cellh,
                                   facecolor=GREEN, alpha=0.4,
                                   edgecolor="white", linewidth=1.0))
        # streaming arrow over the table
        arrow(ax, (tx, ty - n * (cellh + 0.04) - 0.15),
              (tx + tw, ty - n * (cellh + 0.04) - 0.15),
              color=BLUE, lw=1.6)
        tx += tw + 0.35

    ax.text(4.0, ty - 5 * (cellh + 0.04) - 0.55,
            "stream EVERY row of matched tables, no per-entity check",
            ha="center", fontsize=9.5, color=BLUE, weight="bold")

    box(ax, 0.4, 1.7, 7.7, 1.05,
        "Cost = O(matched entities) pure streaming;\nthe bitset has already "
        "filtered at the archetype grain.\nTrivially SIMD-able, multi-core ready.",
        fc=LGREEN, ec=GREEN, fs=10, weight="bold")
    box(ax, 0.4, 0.55, 7.7, 0.95,
        "Pro: tightest inner loop, best cache use.\n"
        "Con: moving an entity between archetypes costs a row move.",
        fc=SOFT, ec=INK, fs=10, weight="bold")

    ax.set_xlim(0, 8.4); ax.set_ylim(0.2, 8.0)
    ax.set_aspect("equal")
    clean_ax(ax)

    fig.tight_layout()
    fig.savefig(os.path.join(OUT, "fig-p2_09_03-archetype-vs-sparse.png"))
    plt.close(fig)


# ============================================================ fig-p2_09_04
# Numerical simulation: naive "for each entity, check has(Component)" vs
# archetype/Query matching, as entity count grows.
def fig_naive_vs_query():
    rng = np.random.default_rng(42)

    # entity counts from 1k to 200k
    Ns = np.linspace(1_000, 200_000, 24).astype(int)

    # Naive cost model: for each of N entities, check membership against a list
    # of required components. Membership check ~ a few ns per component; we model
    # it as proportional to N * (avg components per entity) plus pointer-chase
    # overhead (cache misses dominate at scale -> super-linear feel).
    # Calibrated so naive crosses the 16ms budget around ~30k entities.
    avg_comps_naive = 4.0            # look at ~4 component slots per entity
    ns_per_check_naive = 70.0        # ~70 ns per has(Component) probe (pointer chase, cache miss)
    # add a mild super-linear term for cache-miss amplification at large N
    naive_us = Ns * avg_comps_naive * ns_per_check_naive / 1000.0 \
        + (Ns / 1000.0) ** 1.15 * 35.0

    # Query cost model: O(K) to scan archetype bitsets (K small, ~ tens) + O(matched)
    # to iterate matched rows densely. matched ~ fraction of N.
    K_arch = 40                      # ~40 archetypes in the world
    matched_fraction = 0.35          # ~35% of entities match the query
    ns_per_arche_check = 1.0         # ~1 ns per bit test
    ns_per_dense_row = 9.0           # streaming a dense row is ~9 ns (cache friendly)
    arch_us = (K_arch * ns_per_arche_check) / 1000.0 \
        + Ns * matched_fraction * ns_per_dense_row / 1000.0

    # add tiny jitter for realism
    naive_us = naive_us * (1.0 + rng.normal(0, 0.02, size=Ns.shape))
    arch_us = arch_us * (1.0 + rng.normal(0, 0.02, size=Ns.shape))

    fig, ax = plt.subplots(figsize=(10.6, 6.6))
    ax.plot(Ns / 1000.0, naive_us / 1000.0, "-o", color=RED, lw=2.2, ms=6,
            label="Naive: for each entity, check has(Component)")
    ax.plot(Ns / 1000.0, arch_us / 1000.0, "-s", color=GREEN, lw=2.2, ms=6,
            label="Query: bitset-prefilter archetypes, then dense scan")

    # annotate the 16ms / 60FPS budget
    budget_ms = 16.0
    ax.axhline(budget_ms, color=AMBER, lw=1.8, linestyle="--")
    ax.text(2, budget_ms + 0.6,
            f"60 FPS frame budget = {budget_ms:.0f} ms",
            fontsize=11, color=AMBER, weight="bold")

    # find roughly where naive crosses the budget (interpolate)
    idx = int(np.argmax(naive_us / 1000.0 >= budget_ms))
    if 0 < idx < len(Ns):
        cross_n = Ns[idx] / 1000.0
        ax.axvline(cross_n, color=RED, lw=1.2, linestyle=":", alpha=0.7)
        ax.annotate(f"naive blows the budget\nat ~{cross_n:.0f}k entities",
                    xy=(cross_n, budget_ms),
                    xytext=(cross_n + 25, budget_ms * 0.45),
                    fontsize=10, color=RED, weight="bold",
                    arrowprops=dict(arrowstyle="->", color=RED, lw=1.4))

    ax.set_xlabel("number of entities (thousands)", fontsize=12.5, weight="bold")
    ax.set_ylabel("query / scan cost (ms)", fontsize=12.5, weight="bold")
    ax.set_title("Naive scan vs Query matching: cost vs entity count",
                 fontsize=14, weight="bold", pad=12)
    ax.grid(True, alpha=0.3, linestyle="--")
    ax.legend(loc="upper left", fontsize=11.5, framealpha=0.95)
    ax.set_xlim(0, Ns.max() / 1000.0 * 1.02)
    ax.set_ylim(0, max(naive_us.max() / 1000.0 * 1.05, budget_ms * 1.2))

    for s in ("top", "right"):
        ax.spines[s].set_visible(False)

    fig.tight_layout()
    fig.savefig(os.path.join(OUT, "fig-p2_09_04-naive-vs-query.png"))
    plt.close(fig)


if __name__ == "__main__":
    fig_bitset_match()
    fig_view_iterator()
    fig_arch_vs_sparse()
    fig_naive_vs_query()
    print("done:", sorted(f for f in os.listdir(OUT)
                          if f.startswith("fig-p2_09_") and f.endswith(".png")))
