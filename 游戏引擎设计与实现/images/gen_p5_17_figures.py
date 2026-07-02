# -*- coding: utf-8 -*-
"""Generate PNG figures for chapter P5-17 (Multi-threaded job system).
English labels, Chinese text in the chapter body. Run: python gen_p5_17_figures.py

Figures:
  fig-p5_17_01-system-dag.png          One frame's job DAG: system nodes + dependency edges + parallel branches + sync point
  fig-p5_17_02-data-vs-task.png         Data parallelism (inside one System) vs Task parallelism (between Systems)
  fig-p5_17_03-gantt-scheduling.png     Multi-core scheduling Gantt: tasks wait on deps, run parallel when free, sync barrier
  fig-p5_17_04-bevy-entt-models.png     Two real models: Bevy executor (runtime conflict bits) vs EnTT organizer (static ro/rw graph, user-supplied pool)
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
    "savefig.pad_inches": 0.18,
})

OUT = os.path.dirname(os.path.abspath(__file__))

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
TEAL  = (0.13, 0.58, 0.60)


def clean_ax(ax):
    for s in ("top", "right", "bottom", "left"):
        ax.spines[s].set_visible(False)
    ax.set_xticks([]); ax.set_yticks([])


def node_box(ax, cx, cy, w, h, label, sub, color, txt_color="white", ec=INK, fs=10.5, sub_fs=8.2):
    ax.add_patch(FancyBboxPatch((cx - w / 2, cy - h / 2), w, h,
                                boxstyle="round,pad=0.02,rounding_size=0.10",
                                facecolor=color, edgecolor=ec, linewidth=1.2, alpha=0.96))
    ax.text(cx, cy + h * 0.18, label, fontsize=fs, ha="center", va="center",
            color=txt_color, weight="bold")
    if sub:
        ax.text(cx, cy - h * 0.24, sub, fontsize=sub_fs, ha="center", va="center",
                color=txt_color)


def dep_arrow(ax, a, b, color=DGREY, lw=1.6, rad=0.0, style="-|>"):
    ax.add_artist(FancyArrowPatch(a, b, arrowstyle=style, color=color, lw=lw,
                                  connectionstyle=f"arc3,rad={rad}", alpha=0.92))


# ============================================================
# Fig 1: One frame's job DAG
# ============================================================
def fig1_dag():
    fig, ax = plt.subplots(figsize=(14.2, 8.4))
    clean_ax(ax)
    ax.set_xlim(0, 30); ax.set_ylim(0, 11)
    ax.set_title("One frame as a job DAG:  nodes = Systems,  edges = 'must-finish-before' (data/sync deps)",
                 fontsize=13.5, weight="bold", color=INK, loc="left", pad=10)

    # Input
    node_box(ax, 3.0, 9.2, 3.0, 1.0, "Input", "read input devices", TEAL)

    # Tier A: independent leaf systems (no deps between them) -> can run parallel
    node_box(ax, 7.5, 9.2, 3.0, 1.0, "Animation", "write: AnimState", BLUE)
    node_box(ax, 7.5, 7.5, 3.0, 1.0, "AI", "write: Decision", BLUE)
    node_box(ax, 7.5, 5.8, 3.0, 1.0, "Particles", "write: ParticlePos", BLUE)
    node_box(ax, 7.5, 4.1, 3.0, 1.0, "Audio", "write: AudioSource", BLUE)

    # Movement: reads Velocity, writes Position (consumes AI decision indirectly)
    node_box(ax, 13.0, 8.3, 3.2, 1.1, "Movement", "read Vel  /  write Pos", GREEN)

    # Physics: writes Velocity, reads Pos (must run before Movement)
    node_box(ax, 13.0, 6.0, 3.2, 1.1, "Physics", "read Pos  /  write Vel", AMBER)

    # SYNC POINT: ApplyDeferred (commands from above: spawn/despawn/insert)
    node_box(ax, 18.5, 7.2, 3.6, 1.3, "ApplyDeferred", "SYNC POINT  flush Commands\nexclusive  &mut World",
             RED, fs=10.2, sub_fs=8.4)

    # Render prep + submit (depends on everything that touched Pos)
    node_box(ax, 24.5, 7.2, 3.4, 1.3, "Render submit", "read Pos/Mesh/Material\n-> draw calls", PURP, fs=10.2)

    # ---- edges (dependencies) ----
    dep_arrow(ax, (4.5, 9.2), (6.0, 9.2))   # input -> animation
    dep_arrow(ax, (4.5, 9.2), (6.0, 7.7), rad=-0.15)  # input -> AI
    dep_arrow(ax, (4.5, 9.2), (6.0, 6.0), rad=-0.25)  # input -> particles
    dep_arrow(ax, (4.5, 9.2), (6.0, 4.3), rad=-0.35)  # input -> audio

    # Animation -> Movement (AnimState may influence) ; AI -> Movement
    dep_arrow(ax, (9.0, 9.2), (11.4, 8.5), color=GREEN, rad=0.10)
    dep_arrow(ax, (9.0, 7.5), (11.4, 8.1), color=GREEN, rad=-0.05)

    # Physics depends on Movement? No - Physics reads Pos, writes Vel; Movement reads Vel writes Pos.
    # So Movement depends on Physics (Physics must finish first). Edge Physics -> Movement
    dep_arrow(ax, (14.6, 6.55), (14.6, 7.75), color=AMBER)
    ax.text(15.05, 7.15, "Physics writes Vel\nbefore Movement reads it",
            fontsize=8.4, color=AMBER, ha="left", va="center", weight="bold")

    # Movement/Audio/Particles -> ApplyDeferred (they used Commands)
    dep_arrow(ax, (14.6, 8.85), (16.8, 7.6), color=RED, rad=-0.10)
    dep_arrow(ax, (9.0, 5.8), (16.8, 7.0), color=RED, rad=-0.18)
    dep_arrow(ax, (9.0, 4.1), (16.8, 6.7), color=RED, rad=-0.25)

    # ApplyDeferred -> Render submit
    dep_arrow(ax, (20.3, 7.2), (22.8, 7.2), color=PURP, lw=2.0)

    # ---- highlight parallel branch (Tier A) ----
    # draw a translucent green band around the four independent leaf systems
    band = FancyBboxPatch((5.9, 3.4), 4.7, 6.7,
                          boxstyle="round,pad=0.05,rounding_size=0.15",
                          facecolor=LGREEN, edgecolor=GREEN, linewidth=1.5,
                          linestyle="--", alpha=0.35, zorder=0)
    ax.add_patch(band)
    ax.text(8.25, 2.7, "PARALLEL BRANCH:  4 systems touch DISJOINT components  ->  can run on 4 cores at once",
            fontsize=10.6, color=GREEN, weight="bold", ha="center", va="center",
            bbox=dict(boxstyle="round,pad=0.3", facecolor="white", edgecolor=GREEN))

    # ---- legend bottom ----
    ly = 1.3
    legend_items = [
        (BLUE, "independent system (read/write own components)"),
        (GREEN, "data dependency edge  (A writes what B reads)"),
        (RED, "sync point (ApplyDeferred: flush structural Commands)"),
        (PURP, "render submit (consumes final state)"),
    ]
    for i, (c, t) in enumerate(legend_items):
        ax.add_patch(Rectangle((0.6 + i * 7.3, ly), 0.55, 0.55, facecolor=c, edgecolor=INK))
        ax.text(1.35 + i * 7.3, ly + 0.28, t, fontsize=8.8, color=INK, va="center", ha="left")

    fig.savefig(os.path.join(OUT, "fig-p5_17_01-system-dag.png"))
    plt.close(fig)
    print("  saved fig-p5_17_01-system-dag.png")


# ============================================================
# Fig 2: Data parallelism vs Task parallelism
# ============================================================
def fig2_data_vs_task():
    fig, axes = plt.subplots(2, 1, figsize=(14.2, 9.4))

    # ---- top: data parallel (inside ONE system) ----
    ax = axes[0]
    clean_ax(ax)
    ax.set_xlim(0, 30); ax.set_ylim(0, 7)
    ax.set_title("Data parallelism  (INSIDE one System):  same operation on disjoint entity slices",
                 fontsize=13.0, weight="bold", color=INK, loc="left", pad=10)

    # one big "MovementSystem" panel
    ax.add_patch(FancyBboxPatch((1.0, 4.6), 28.0, 1.6,
                                boxstyle="round,pad=0.02,rounding_size=0.10",
                                facecolor=LGREEN, edgecolor=GREEN, linewidth=1.4))
    ax.text(1.5, 5.95, "MovementSystem:   for each entity:  pos += vel * dt",
            fontsize=11.5, color=INK, weight="bold", ha="left", va="center")

    # entity array, 16 cells, split into 4 chunks
    n = 16; cell_w = 1.5; x0 = 2.0; y0 = 4.75
    chunk_n = 4; per = n // chunk_n
    cols = [BLUE, AMBER, PURP, TEAL]
    for i in range(n):
        cx = x0 + i * cell_w
        ci = i // per
        ax.add_patch(Rectangle((cx, y0), cell_w - 0.08, 0.85, facecolor=cols[ci],
                               edgecolor=INK, linewidth=0.7))
        ax.text(cx + (cell_w - 0.08) / 2, y0 + 0.42, f"e{i}", ha="center", va="center",
                fontsize=7.6, color="white", weight="bold")

    # 4 cores row
    for k in range(chunk_n):
        cx_center = x0 + (k * per + per / 2) * cell_w
        ax.annotate("", xy=(cx_center, 3.6), xytext=(cx_center, y0 - 0.05),
                    arrowprops=dict(arrowstyle="-|>", color=cols[k], lw=1.6))
        ax.add_patch(FancyBboxPatch((cx_center - 1.7, 2.2), 3.4, 1.4,
                                    boxstyle="round,pad=0.02,rounding_size=0.10",
                                    facecolor=cols[k], edgecolor=INK, linewidth=1.1, alpha=0.96))
        ax.text(cx_center, 3.15, f"Core {k}", fontsize=10.5, ha="center", va="center",
                color="white", weight="bold")
        ax.text(cx_center, 2.55, f"pos += vel*dt\non e[{k*per}..{k*per+per-1}]",
                fontsize=8.4, ha="center", va="center", color="white")

    ax.text(15, 1.0,
            "Key:  ONE System,  SAME loop body,  DISJOINT entity slices.  No locks (slice i only writes its own rows).\n"
            "Bevy Query::par_iter does exactly this.  (Covered in P2-07.)",
            fontsize=10.2, color=INK, ha="center", va="center",
            bbox=dict(boxstyle="round,pad=0.4", facecolor=SOFT, edgecolor=DGREY))

    # ---- bottom: task parallel (between systems) ----
    ax = axes[1]
    clean_ax(ax)
    ax.set_xlim(0, 30); ax.set_ylim(0, 7)
    ax.set_title("Task parallelism  (BETWEEN Systems):  different Systems run at once if accesses are compatible",
                 fontsize=13.0, weight="bold", color=INK, loc="left", pad=10)

    # 4 different systems as separate panels, running concurrently
    panels = [
        (2.0, 3.6, "Animation", "write: AnimState", BLUE),
        (8.5, 3.6, "AI", "write: Decision", AMBER),
        (15.0, 3.6, "Particles", "write: ParticlePos", PURP),
        (21.5, 3.6, "Audio", "write: AudioSource", TEAL),
    ]
    for (px, py, name, acc, c) in panels:
        ax.add_patch(FancyBboxPatch((px, py), 5.5, 1.6,
                                    boxstyle="round,pad=0.02,rounding_size=0.10",
                                    facecolor=c, edgecolor=INK, linewidth=1.3, alpha=0.95))
        ax.text(px + 2.75, py + 1.1, name, fontsize=11.0, ha="center", va="center",
                color="white", weight="bold")
        ax.text(px + 2.75, py + 0.5, acc, fontsize=8.6, ha="center", va="center",
                color="white")

    # cores assignment - each system on a core
    for k, (px, py, name, acc, c) in enumerate(panels):
        cx_center = px + 2.75
        ax.annotate("", xy=(cx_center, 2.6), xytext=(cx_center, py),
                    arrowprops=dict(arrowstyle="-|>", color=c, lw=1.4))
        ax.add_patch(FancyBboxPatch((cx_center - 1.4, 1.4), 2.8, 1.1,
                                    boxstyle="round,pad=0.02,rounding_size=0.08",
                                    facecolor=GREY, edgecolor=INK, linewidth=1.0))
        ax.text(cx_center, 1.95, f"Core {k}", fontsize=9.8, ha="center", va="center",
                color=INK, weight="bold")

    ax.text(15, 0.5,
            "Key:  FOUR different Systems,  each writes a DIFFERENT component.\n"
            "No data overlap  ->  scheduler lets them run concurrently on 4 cores.  (This chapter P5-17.)",
            fontsize=10.2, color=INK, ha="center", va="center",
            bbox=dict(boxstyle="round,pad=0.4", facecolor=SOFT, edgecolor=DGREY))

    plt.subplots_adjust(hspace=0.55)
    fig.savefig(os.path.join(OUT, "fig-p5_17_02-data-vs-task.png"))
    plt.close(fig)
    print("  saved fig-p5_17_02-data-vs-task.png")


# ============================================================
# Fig 3: Multi-core scheduling Gantt (with deps + sync barrier)
# ============================================================
def fig3_gantt():
    fig, ax = plt.subplots(figsize=(14.4, 7.6))

    # cores
    cores = ["Core 0", "Core 1", "Core 2", "Core 3"]
    yticks = [3, 2, 1, 0]
    ax.set_ylim(-0.7, 4.0)
    ax.set_yticks(yticks)
    ax.set_yticklabels(cores, fontsize=11)

    # task specs: (label, start, duration, core_idx, color, h)
    # timeline 0..18 ; sync barrier at t=9
    tasks = [
        # phase 1: leaf systems, parallel, no deps
        ("Animation",  0.0, 2.6, 0, BLUE, 0.7),
        ("AI",         0.0, 3.2, 1, AMBER, 0.7),
        ("Particles",  0.0, 2.2, 2, PURP, 0.7),
        ("Audio",      0.0, 2.9, 3, TEAL, 0.7),
        # phase 2: Physics (depends on Pos? assume starts after Audio slot frees, deps on input)
        ("Physics",    3.2, 2.4, 3, AMBER, 0.7),
        ("Movement",   3.2, 2.2, 0, GREEN, 0.7),  # depends on Physics? -> actually wait
    ]
    # redo: make Movement wait for Physics (draw idle gap)
    tasks = [
        ("Animation",  0.0, 2.6, 0, BLUE, 0.7),
        ("AI",         0.0, 3.4, 1, AMBER, 0.7),
        ("Particles",  0.0, 2.2, 2, PURP, 0.7),
        ("Audio",      0.0, 2.9, 3, TEAL, 0.7),
        ("Physics",    2.9, 2.6, 3, AMBER, 0.7),     # after Audio on core 3
        ("Movement",   5.5, 2.0, 0, GREEN, 0.7),     # MUST wait for Physics (idle gap on core 0)
        # some fill-in
        ("Skinning",   2.6, 2.0, 0, BLUE, 0.7),      # independent, fills core 0 gap
    ]

    # draw tasks
    for (name, s, d, ci, c, h) in tasks:
        y = ci
        ax.add_patch(Rectangle((s, y - h / 2), d, h, facecolor=c, edgecolor=INK, linewidth=0.9, alpha=0.95))
        ax.text(s + d / 2, y, name, fontsize=9.0, ha="center", va="center",
                color="white", weight="bold")

    # idle gap shading on core 0 between Skinning(end=4.6) and Movement(start=5.5) due to dep
    ax.add_patch(Rectangle((4.6, 0 - 0.35), 0.9, 0.7, facecolor=LRED, edgecolor=RED,
                           linewidth=0.9, linestyle="--", alpha=0.6))
    ax.annotate("idle: waiting for Physics\nto finish writing Velocity",
                xy=(5.05, 0.0), xytext=(5.05, -0.55),
                fontsize=8.2, color=RED, ha="center", va="top", weight="bold",
                arrowprops=dict(arrowstyle="->", color=RED, lw=1.0))

    # SYNC POINT barrier at t=7.5
    barrier_t = 7.5
    ax.axvline(barrier_t, color=RED, lw=2.6, linestyle="--", alpha=0.85)
    ax.text(barrier_t, 3.7, "SYNC POINT\nApplyDeferred\n(exclusive &mut World)",
            fontsize=9.6, color=RED, weight="bold", ha="center", va="top")

    # phase 3: after sync, render prep + submit (serialize across cores but parallelize prep)
    post = [
        ("RenderExtract", 7.8, 1.6, 0, PURP, 0.7),
        ("RenderExtract", 7.8, 1.6, 1, PURP, 0.7),
        ("BuildBatch",    9.6, 1.4, 0, PURP, 0.7),
        ("BuildBatch",    9.6, 1.4, 1, PURP, 0.7),
        ("GPU submit",   11.2, 2.4, 0, PURP, 0.7),
    ]
    for (name, s, d, ci, c, h) in post:
        y = ci
        ax.add_patch(Rectangle((s, y - h / 2), d, h, facecolor=c, edgecolor=INK, linewidth=0.9, alpha=0.95))
        ax.text(s + d / 2, y, name, fontsize=8.6, ha="center", va="center",
                color="white", weight="bold")

    # cores 2,3 idle after their phase-1/2 work (no render work for them)
    for ci in (2, 3):
        ax.add_patch(Rectangle((7.8, ci - 0.35), 5.8, 0.7, facecolor=SOFT, edgecolor=DGREY,
                               linewidth=0.6, linestyle=":", alpha=0.6))
        ax.text(10.7, ci, "idle (no render work assigned)", fontsize=8.4, color=DGREY,
                ha="center", va="center", style="italic")

    # frame end marker
    ax.axvline(13.6, color=GREEN, lw=2.2, linestyle="-", alpha=0.8)
    ax.text(13.6, 3.7, "frame end\n(16ms budget)",
            fontsize=9.6, color=GREEN, weight="bold", ha="center", va="top")

    # dependency arrows (curved) - Physics -> Movement
    ax.add_artist(FancyArrowPatch((5.5, 3 + 0.35), (5.5, 0 + 0.35),
                                  arrowstyle="-|>", color=AMBER, lw=1.6,
                                  connectionstyle="arc3,rad=-0.5", alpha=0.9))
    ax.text(5.7, 1.8, "dep", fontsize=8.0, color=AMBER, weight="bold", rotation=90,
            ha="left", va="center")

    ax.set_xlim(-0.3, 14.4)
    ax.set_xlabel("time (ms, schematic)", fontsize=11.5)
    ax.set_title("Multi-core scheduling Gantt:  parallel when free,  wait on deps,  barrier at sync point",
                 fontsize=12.8, weight="bold", color=INK, loc="left", pad=22)
    ax.grid(True, axis="x", ls=":", alpha=0.4)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)

    fig.savefig(os.path.join(OUT, "fig-p5_17_03-gantt-scheduling.png"))
    plt.close(fig)
    print("  saved fig-p5_17_03-gantt-scheduling.png")


# ============================================================
# Fig 4: Bevy executor (runtime conflict bits) vs EnTT organizer (static ro/rw graph)
# ============================================================
def fig4_bevy_entt():
    fig, axes = plt.subplots(1, 2, figsize=(15.0, 8.2))

    # ---- left: Bevy MultiThreadedExecutor ----
    ax = axes[0]
    clean_ax(ax)
    ax.set_xlim(0, 16); ax.set_ylim(0, 11)
    ax.set_title("Bevy:  MultiThreadedExecutor  (runtime,  pre-computed conflict bitsets)",
                 fontsize=12.4, weight="bold", color=INK, loc="left", pad=10)

    # build phase: access matrix -> conflicting_systems bitset
    ax.text(0.4, 10.3, "BUILD (once):  for each system pair, check is_compatible(access)  ->  conflicting_systems[FixedBitSet]",
            fontsize=10.0, color=INK, ha="left", va="center", weight="bold")

    # 4 systems with a 4x4 conflict matrix
    sys_names = ["Anim", "AI", "Physics", "Movement"]
    # conflict matrix (1 = conflict, 0 = ok). symmetric.
    # Anim writes AnimState; AI writes Decision; Physics writes Vel reads Pos; Movement writes Pos reads Vel
    # Physics <-> Movement conflict (both touch Vel OR Pos mutably)
    M = np.array([
        [0, 0, 0, 0],
        [0, 0, 0, 0],
        [0, 0, 0, 1],
        [0, 0, 1, 0],
    ])
    # draw matrix
    mx0, my0 = 1.2, 6.4
    cell = 0.9
    for i in range(4):
        for j in range(4):
            v = M[i, j]
            face = RED if v == 1 else LGREEN
            ec = RED if v == 1 else GREEN
            ax.add_patch(Rectangle((mx0 + j * cell, my0 + (3 - i) * cell), cell - 0.06, cell - 0.06,
                                   facecolor=face, edgecolor=ec, linewidth=1.0))
            ax.text(mx0 + j * cell + (cell - 0.06) / 2, my0 + (3 - i) * cell + (cell - 0.06) / 2,
                    str(v), ha="center", va="center", fontsize=10, color=INK, weight="bold")
    # labels
    for j, n in enumerate(sys_names):
        ax.text(mx0 + j * cell + (cell - 0.06) / 2, my0 + 4 * cell + 0.1, n,
                fontsize=9.2, ha="center", va="bottom", color=INK, weight="bold")
    for i, n in enumerate(sys_names):
        ax.text(mx0 - 0.15, my0 + (3 - i) * cell + (cell - 0.06) / 2, n,
                fontsize=9.2, ha="right", va="center", color=INK, weight="bold")
    ax.text(mx0 + 2 * cell, my0 - 0.6, "conflict matrix  (1 = cannot co-run)",
            fontsize=9.6, color=INK, ha="center", va="center", style="italic")

    # RUNTIME: executor loop
    ax.text(0.4, 5.4, "RUNTIME (each tick):",
            fontsize=10.2, color=INK, ha="left", va="center", weight="bold")
    run_steps = [
        "1. ready_systems  =  those with num_dependencies_remaining == 0",
        "2. for each ready system:  can_run?  conflict_set  &  running  == empty",
        "3. spawn task on ComputeTaskPool (work-stealing)",
        "4. on finish: push to lock-free ConcurrentQueue, signal_dependents",
    ]
    for i, s in enumerate(run_steps):
        ax.text(0.6, 4.8 - i * 0.55, s, fontsize=9.4, color=INK, ha="left", va="center")

    # outcome
    ax.add_patch(FancyBboxPatch((0.6, 1.4), 14.8, 1.5,
                                boxstyle="round,pad=0.02,rounding_size=0.10",
                                facecolor=LBLUE, edgecolor=BLUE, linewidth=1.3))
    ax.text(8.0, 2.55, "Anim, AI, Physics  run concurrently  (disjoint writes)",
            fontsize=9.8, color=INK, ha="center", va="center", weight="bold")
    ax.text(8.0, 1.85, "Movement waits for Physics  (Vel conflict)  ->  signaled when Physics done",
            fontsize=9.8, color=INK, ha="center", va="center")

    ax.text(0.4, 0.5, "Executor RUNS tasks.  Conflict check is per-tick, reactive to completions.",
            fontsize=9.2, color=BLUE, ha="left", va="center", style="italic", weight="bold")

    # ---- right: EnTT organizer ----
    ax = axes[1]
    clean_ax(ax)
    ax.set_xlim(0, 16); ax.set_ylim(0, 11)
    ax.set_title("EnTT:  basic_organizer  (static graph,  ro/rw from C++ const-ness)",
                 fontsize=12.4, weight="bold", color=INK, loc="left", pad=10)

    ax.text(0.4, 10.3, "BUILD (once):  unpack_type deduces ro/rw per arg from const-ness",
            fontsize=10.0, color=INK, ha="left", va="center", weight="bold")

    # show a C++ signature -> ro/rw
    sig = "void movement(View<Pos, const Vel> v)"
    ax.text(0.5, 9.5, sig, fontsize=10.2, color=INK, ha="left", va="center",
            family="monospace",
            bbox=dict(boxstyle="round,pad=0.3", facecolor=SOFT, edgecolor=DGREY))
    ax.text(0.5, 8.7, "->  rw: {Pos}    ro: {Vel}    (const = read-only)",
            fontsize=9.6, color=PURP, ha="left", va="center", weight="bold")

    # flow builder produces adjacency
    ax.text(0.4, 7.9, "flow builder  ->  graph()  returns adjacency list  (vertex has in/out edges)",
            fontsize=10.0, color=INK, ha="left", va="center", weight="bold")

    # small DAG: 4 nodes, Physics->Movement edge
    nodes = [
        (3.0, 6.6, "Anim", BLUE),
        (7.0, 6.6, "AI", AMBER),
        (3.0, 5.0, "Physics", GREEN),
        (7.0, 5.0, "Movement", PURP),
    ]
    for (nx, ny, n, c) in nodes:
        ax.add_patch(FancyBboxPatch((nx - 1.1, ny - 0.4), 2.2, 0.8,
                                    boxstyle="round,pad=0.02,rounding_size=0.08",
                                    facecolor=c, edgecolor=INK, linewidth=1.1, alpha=0.95))
        ax.text(nx, ny, n, fontsize=9.6, ha="center", va="center", color="white", weight="bold")
    dep_arrow(ax, (3.0, 5.4), (7.0 - 1.1, 5.0), color=PURP, lw=1.8, rad=0.15)
    ax.text(5.0, 4.5, "Physics rw={Vel}  ->  Movement ro={Vel}  :  edge inserted",
            fontsize=8.6, color=PURP, ha="center", va="center", weight="bold")

    # the KEY point: organizer does NOT run
    ax.add_patch(FancyBboxPatch((0.5, 2.4), 15.0, 1.6,
                                boxstyle="round,pad=0.02,rounding_size=0.10",
                                facecolor=LAMBER, edgecolor=AMBER, linewidth=1.4))
    ax.text(8.0, 3.6, "organizer does NOT execute tasks.",
            fontsize=10.6, color=INK, ha="center", va="center", weight="bold")
    ax.text(8.0, 2.9, "It returns the graph; user feeds it to TBB / Taskflow / own thread pool.",
            fontsize=9.6, color=INK, ha="center", va="center")

    ax.text(0.4, 1.5, "Conflict analysis is STATIC (compile-time type deduction).  Execution is YOUR job.",
            fontsize=9.2, color=AMBER, ha="left", va="center", style="italic", weight="bold")
    ax.text(0.4, 0.7, "(Vertex also tracks sync_point if a System takes non-const Registry&.)",
            fontsize=8.6, color=DGREY, ha="left", va="center", style="italic")

    plt.subplots_adjust(wspace=0.18)
    fig.savefig(os.path.join(OUT, "fig-p5_17_04-bevy-entt-models.png"))
    plt.close(fig)
    print("  saved fig-p5_17_04-bevy-entt-models.png")


if __name__ == "__main__":
    print("Generating P5-17 figures ...")
    fig1_dag()
    fig2_data_vs_task()
    fig3_gantt()
    fig4_bevy_entt()
    print("Done.")
