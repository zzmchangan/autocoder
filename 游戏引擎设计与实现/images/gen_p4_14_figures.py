# -*- coding: utf-8 -*-
"""Generate PNG figures for P4-14 (Scripting System: Lua Hot Reload).

Three figures:
  fig-p4_14_01-lua-embed-arch.png   Engine embeds Lua VM: host <-> Lua VM <-> .lua
  fig-p4_14_02-binding-stack.png    C++ object pushed to Lua stack, GC/ownership boundary
  fig-p4_14_03-hotreload-flow.png   File mtime change -> reload chunk -> keep globals

Style follows the series conventions: English labels, fixed palette,
bbox='tight', top/right spines removed.
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

# Fixed palette (series-wide)
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
    ax.set_xticks([])
    ax.set_yticks([])


def box(ax, x, y, w, h, text, fc=SOFT, ec=INK, fs=12, weight="normal", tc=INK):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.02,rounding_size=0.06",
                                facecolor=fc, edgecolor=ec, linewidth=1.4))
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center",
            fontsize=fs, weight=weight, color=tc)


def arrow(ax, p1, p2, color=INK, lw=1.6, style="-|>", rad=0.0, ms=14, ls="-"):
    cs = f"arc3,rad={rad}" if rad else "arc3,rad=0"
    ax.add_artist(FancyArrowPatch(p1, p2, arrowstyle=style, color=color,
                                  lw=lw, connectionstyle=cs, mutation_scale=ms,
                                  linestyle=ls))


# ============================================================ fig 01
# Engine <-> Lua VM <-> .lua scripts architecture.
# Host C++ calls lua_pcall; scripts call bound C++ fns back. Same process.
def fig_lua_embed_arch():
    fig, ax = plt.subplots(figsize=(13.6, 8.6))
    clean_ax(ax)
    ax.set_xlim(0, 13.6)
    ax.set_ylim(0, 8.6)

    ax.text(6.8, 8.2, "Engine embeds Lua VM: same process, stack-based protocol",
            ha="center", fontsize=15, weight="bold", color=INK)
    ax.text(6.8, 7.75,
            "Host calls into Lua via lua_pcall; scripts call back via bound C++ functions",
            ha="center", fontsize=11.5, color=DGREY, style="italic")

    # ---- Left: Host engine (C++)
    host_layers = [
        ("C++ Systems (Movement, Combat, Render)", LGREEN, GREEN),
        ("ECS Registry (Entity / Component pools)", LBLUE, BLUE),
        ("Binding layer (sol2 / hand C API)", LAMBER, AMBER),
    ]
    y = 5.4
    for text, fc, ec in host_layers:
        box(ax, 0.5, y, 3.7, 1.15, text, fc=fc, ec=ec, fs=11.5, weight="bold")
        y -= 1.35
    ax.text(2.35, 6.95, "Host engine (C++)", ha="center", fontsize=13,
            weight="bold", color=INK)
    ax.add_patch(FancyBboxPatch((0.3, 1.3), 4.1, 5.95,
                                boxstyle="round,pad=0.05,rounding_size=0.1",
                                facecolor="none", edgecolor=GREEN,
                                linewidth=2.0, linestyle="--"))

    # ---- Right: Lua VM + scripts
    box(ax, 8.9, 4.5, 3.9, 1.6,
        "Lua VM (lua_State)\nstack-based C API\n(lapi.c, lstate.c)",
        fc=LPURP, ec=PURP, fs=11.5, weight="bold")
    box(ax, 8.9, 2.6, 3.9, 1.5,
        ".lua scripts\n(update.lua, skill.lua, AI.lua)",
        fc=LRED, ec=RED, fs=11.5, weight="bold")
    ax.text(10.85, 6.45, "Embedded runtime (in-process)", ha="center",
            fontsize=13, weight="bold", color=INK)
    ax.add_patch(FancyBboxPatch((8.7, 2.3), 4.3, 4.15,
                                boxstyle="round,pad=0.05,rounding_size=0.1",
                                facecolor="none", edgecolor=PURP,
                                linewidth=2.0, linestyle="--"))

    # ---- Between: the C API boundary
    ax.add_patch(Rectangle((5.0, 2.4), 3.4, 4.0, facecolor=SOFT,
                           edgecolor=GREY, linewidth=1.0, linestyle=":"))
    ax.text(6.7, 6.2, "C API boundary", ha="center", fontsize=11,
            color=DGREY, style="italic")
    # host -> vm (push args, pcall)
    arrow(ax, (4.4, 5.4), (8.85, 5.4), color=BLUE, lw=2.0)
    ax.text(6.6, 5.65, "lua_pcall  (push fn, push args, call)",
            ha="center", fontsize=10.5, color=BLUE, weight="bold")
    # vm -> host (bound fn called back)
    arrow(ax, (8.85, 4.6), (4.4, 4.6), color=AMBER, lw=2.0)
    ax.text(6.6, 4.32, "bound C++ function  (Entity.get_pos, Vec3.add ...)",
            ha="center", fontsize=10.5, color=AMBER, weight="bold")
    # result back
    arrow(ax, (4.4, 3.6), (8.85, 3.6), color=GREEN, lw=1.8, ls="--")
    ax.text(6.6, 3.82, "return value (popped from stack)",
            ha="center", fontsize=10.5, color=GREEN)

    # ---- Bottom legend: same process
    ax.add_patch(FancyBboxPatch((0.4, 0.35), 12.8, 1.15,
                                boxstyle="round,pad=0.03,rounding_size=0.08",
                                facecolor=SOFT, edgecolor=INK, linewidth=1.2))
    ax.text(0.7, 0.93, "Key point:", fontsize=12, weight="bold", color=INK)
    ax.text(0.7, 0.55,
            "Both live in ONE OS process. No IPC, no serialization. "
            "The binding layer translates C++ types <-> Lua values across a virtual stack.",
            fontsize=11, color=INK)

    plt.savefig(os.path.join(OUT, "fig-p4_14_01-lua-embed-arch.png"))
    plt.close(fig)


# ============================================================ fig 02
# C++ object pushed onto the Lua stack; the GC/ownership boundary.
# Shows: push C++ object -> full userdata wrapping a pointer -> metatable
# dispatches methods; Lua GC sweeps userdata but does NOT touch the C++ object.
def fig_binding_stack():
    fig, ax = plt.subplots(figsize=(13.6, 9.0))
    clean_ax(ax)
    ax.set_xlim(0, 13.6)
    ax.set_ylim(0, 9.0)

    ax.text(6.8, 8.65, "C++ <-> Lua binding: full userdata + metatable, two GCs",
            ha="center", fontsize=15, weight="bold", color=INK)
    ax.text(6.8, 8.22,
            "Lua GC reclaims the userdata wrapper; the C++ object lives in the ECS registry",
            ha="center", fontsize=11.5, color=DGREY, style="italic")

    # ---- Left: Lua side
    # stack
    ax.text(2.3, 7.65, "Lua virtual stack", ha="center", fontsize=12.5,
            weight="bold", color=PURP)
    ax.add_patch(Rectangle((0.6, 6.2), 3.4, 1.25, facecolor=LPURP,
                           edgecolor=PURP, linewidth=1.4))
    ax.text(2.3, 6.82, "Entity (full userdata)", ha="center", fontsize=10.5,
            weight="bold", color=INK)
    ax.text(2.3, 6.45, "= wrapper holding an Entity ID", ha="center",
            fontsize=9.5, color=DGREY, style="italic")
    for i in range(3):
        ax.add_patch(Rectangle((0.6, 5.5 - i * 0.55), 3.4, 0.5,
                               facecolor=SOFT, edgecolor=GREY, linewidth=1.0))
    ax.text(2.3, 5.75, "...", ha="center", fontsize=10, color=DGREY)
    ax.text(2.3, 5.2, "Velocity table", ha="center", fontsize=10, color=DGREY)
    ax.text(2.3, 4.65, "...", ha="center", fontsize=10, color=DGREY)
    ax.text(0.6, 4.25, "stack top ->", fontsize=9.5, color=DGREY,
            style="italic", ha="left")

    # metatable attached to userdata
    ax.add_patch(FancyBboxPatch((0.5, 2.7), 3.6, 1.25,
                                boxstyle="round,pad=0.02,rounding_size=0.06",
                                facecolor=LAMBER, edgecolor=AMBER, linewidth=1.4))
    ax.text(2.3, 3.55, "metatable: Entity", ha="center", fontsize=10.5,
            weight="bold", color=INK)
    ax.text(2.3, 3.15, "__index -> { get_pos, move, ... }", ha="center",
            fontsize=9.5, color=INK)
    arrow(ax, (2.3, 6.2), (2.3, 3.95), color=AMBER, lw=1.3, rad=-0.15)

    # Lua GC box
    ax.add_patch(FancyBboxPatch((0.5, 1.0), 3.6, 1.25,
                                boxstyle="round,pad=0.02,rounding_size=0.06",
                                facecolor=LRED, edgecolor=RED, linewidth=1.4))
    ax.text(2.3, 1.85, "Lua GC", ha="center", fontsize=10.5,
            weight="bold", color=INK)
    ax.text(2.3, 1.45, "sweeps the userdata wrapper ONLY", ha="center",
            fontsize=9.5, color=INK)

    # ---- Right: C++ side
    ax.text(10.6, 7.65, "C++ host (ECS registry)", ha="center", fontsize=12.5,
            weight="bold", color=GREEN)
    ax.add_patch(FancyBboxPatch((7.9, 6.2), 5.4, 1.3,
                                boxstyle="round,pad=0.02,rounding_size=0.06",
                                facecolor=LGREEN, edgecolor=GREEN, linewidth=1.4))
    ax.text(10.6, 7.05, "registry (owns all Component pools)", ha="center",
            fontsize=10.5, weight="bold", color=INK)
    ax.text(10.6, 6.55,
            "Position[]  Velocity[]  Health[]  ...  (SoA, see P2-06)",
            ha="center", fontsize=9.8, color=INK)
    # entity rows
    for i, (eid, note) in enumerate([(3, "Position{x,y}"), (7, "Position{x,y}"), (9, "Position{x,y}")]):
        ax.add_patch(FancyBboxPatch((8.0, 5.45 - i * 0.6), 5.2, 0.5,
                                    boxstyle="round,pad=0.01,rounding_size=0.04",
                                    facecolor=SOFT, edgecolor=GREEN, linewidth=1.0))
        ax.text(10.6, 5.7 - i * 0.6,
                f"Entity #{eid}  ->  {note}", ha="center", fontsize=9.5, color=INK)

    # ownership note
    ax.add_patch(FancyBboxPatch((7.9, 1.0), 5.4, 1.4,
                                boxstyle="round,pad=0.02,rounding_size=0.06",
                                facecolor=SOFT, edgecolor=GREEN, linewidth=1.4))
    ax.text(10.6, 2.05, "C++ owns the real data", ha="center", fontsize=10.5,
            weight="bold", color=INK)
    ax.text(10.6, 1.55,
            "Lua holds only an Entity ID handle. The C++ object is NOT freed\n"
            "when Lua GCs the userdata — its life is bound to the registry.",
            ha="center", fontsize=9.3, color=INK)

    # ---- Center: the binding arrow
    arrow(ax, (4.05, 6.82), (7.85, 6.85), color=INK, lw=1.8)
    ax.text(5.95, 7.15, "push as full userdata\n(stores Entity ID)",
            ha="center", fontsize=9.8, color=INK, weight="bold")
    arrow(ax, (7.85, 5.4), (4.05, 3.5), color=GREEN, lw=1.5, rad=0.25, ls="--")
    ax.text(5.6, 4.35, "method call resolves to\nget_pos(registry, id)",
            ha="center", fontsize=9.5, color=GREEN, weight="bold")

    # ---- Ownership boundary (vertical dashed)
    ax.plot([6.65, 6.65], [0.7, 7.7], color=RED, linewidth=1.6,
            linestyle="--", alpha=0.6)
    ax.text(6.65, 7.9, "GC boundary", ha="center", fontsize=10,
            color=RED, weight="bold")

    plt.savefig(os.path.join(OUT, "fig-p4_14_02-binding-stack.png"))
    plt.close(fig)


# ============================================================ fig 03
# Hot reload flow: mtime poll -> reload chunk -> keep _G globals
# but re-evaluate function definitions.
def fig_hotreload_flow():
    fig, ax = plt.subplots(figsize=(13.8, 8.8))
    clean_ax(ax)
    ax.set_xlim(0, 13.8)
    ax.set_ylim(0, 8.8)

    ax.text(6.9, 8.4, "Hot reload: detect change, re-run chunk, keep global state",
            ha="center", fontsize=15, weight="bold", color=INK)
    ax.text(6.9, 7.98,
            "package.loaded[mod] = nil forces require to re-run; tables you keep survive",
            ha="center", fontsize=11.5, color=DGREY, style="italic")

    # ---- Step 1: poll mtime
    box(ax, 0.4, 6.3, 3.0, 1.15,
        "1. Poll file mtime\neach frame (cheap)",
        fc=LBLUE, ec=BLUE, fs=10.8, weight="bold")

    # ---- Step 2: detect change
    box(ax, 4.0, 6.3, 3.0, 1.15,
        "2. mtime changed?\n-> reload that module",
        fc=LAMBER, ec=AMBER, fs=10.8, weight="bold")

    # ---- Step 3: clear package.loaded entry
    box(ax, 7.6, 6.3, 3.4, 1.15,
        "3. package.loaded[mod] = nil\n(bust the require cache)",
        fc=LPURP, ec=PURP, fs=10.5, weight="bold")

    # ---- Step 4: re-run chunk
    box(ax, 11.2, 6.3, 2.3, 1.15,
        "4. require(mod)\nre-runs chunk",
        fc=LRED, ec=RED, fs=10.5, weight="bold")

    # arrows top row
    arrow(ax, (3.4, 6.875), (4.0, 6.875), color=INK, lw=1.6)
    arrow(ax, (7.0, 6.875), (7.6, 6.875), color=INK, lw=1.6)
    arrow(ax, (11.0, 6.875), (11.2, 6.875), color=INK, lw=1.6)

    # ---- State preservation diagram (lower half)
    # global table _G (kept)
    ax.add_patch(FancyBboxPatch((0.5, 3.4), 5.8, 2.3,
                                boxstyle="round,pad=0.03,rounding_size=0.08",
                                facecolor=LGREEN, edgecolor=GREEN, linewidth=1.6))
    ax.text(3.4, 5.35, "_G (global table) — KEPT", ha="center",
            fontsize=11.5, weight="bold", color=INK)
    kept = ["player_hp = 87", "quest_log = {...}", "loot_table = {...}",
            "saved refs to long-lived C++ objects"]
    for i, t in enumerate(kept):
        ax.text(0.85, 4.95 - i * 0.4, "kept:  " + t, fontsize=10, color=INK,
                family="monospace")

    # module functions (re-evaluated)
    ax.add_patch(FancyBboxPatch((7.0, 3.4), 6.0, 2.3,
                                boxstyle="round,pad=0.03,rounding_size=0.08",
                                facecolor=LRED, edgecolor=RED, linewidth=1.6))
    ax.text(10.0, 5.35, "module chunk — RE-EVALUATED", ha="center",
            fontsize=11.5, weight="bold", color=INK)
    redef = ["function update_enemy(e, dt) ... end",
             "function cast_spell(caster, ...) ... end",
             "local helpers = { ... }   -- rebuilt",
             "old closures are garbage-collected"]
    for i, t in enumerate(redef):
        ax.text(7.35, 4.95 - i * 0.4, "new:   " + t, fontsize=10, color=INK,
                family="monospace")

    # arrow from step 4 down: re-run redefines funcs
    arrow(ax, (12.35, 6.3), (12.35, 5.7), color=RED, lw=1.5)
    # arrow from step 4 down to kept side
    arrow(ax, (11.5, 6.3), (4.0, 5.7), color=GREEN, lw=1.5, rad=-0.2)

    # ---- Bottom: the trap
    ax.add_patch(FancyBboxPatch((0.4, 0.4), 13.0, 1.7,
                                boxstyle="round,pad=0.03,rounding_size=0.08",
                                facecolor=SOFT, edgecolor=INK, linewidth=1.2))
    ax.text(0.75, 1.78, "Pitfall: stale closures", fontsize=12, weight="bold",
            color=RED)
    ax.text(0.75, 1.42,
            "A function captured an old version of a local. After reload, two copies coexist; "
            "callbacks may still call the OLD closure.",
            fontsize=10.5, color=INK)
    ax.text(0.75, 1.02,
            "Fix: route cross-module calls through _G or a single dispatcher table, "
            "so the indirection always picks up the latest definition.",
            fontsize=10.5, color=INK)
    ax.text(0.75, 0.62,
            "Also: never let Lua hold a raw C++ pointer across reloads — re-fetch "
            "the Entity handle through the binding each time.",
            fontsize=10.5, color=INK)

    plt.savefig(os.path.join(OUT, "fig-p4_14_03-hotreload-flow.png"))
    plt.close(fig)


if __name__ == "__main__":
    fig_lua_embed_arch()
    fig_binding_stack()
    fig_hotreload_flow()
    print("Generated 3 figures for P4-14.")
