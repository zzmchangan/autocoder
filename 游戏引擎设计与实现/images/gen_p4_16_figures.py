# -*- coding: utf-8 -*-
"""Generate PNG figures for chapter P4-16 (Serialization & Scene Persistence).
All labels in English; main text explains in Chinese.
Run:  python gen_p4_16_figures.py

Figures:
  fig-p4_16_01-ecs-vs-oop-serialize.png   ECS serialization (dump contiguous component
                                          arrays) vs OOP object-graph serialization
                                          (pointer chasing, cycles, type tags).
  fig-p4_16_02-entity-id-remap.png        Entity id remapping: serialize id=5 -> allocate
                                          fresh id on load -> rewrite entity references
                                          stored inside components.
  fig-p4_16_03-scene-file-structure.png   Scene file structure: a list of entities, each
                                          carrying typed components; asset Handles are
                                          stored as asset paths, not pointers.
"""
import os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, FancyArrowPatch, FancyBboxPatch, Circle
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

# Palette consistent with the rest of the book
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
LTEAL = (0.86, 0.95, 0.95)


def clean_ax(ax):
    for s in ("top", "right", "bottom", "left"):
        ax.spines[s].set_visible(False)
    ax.set_xticks([]); ax.set_yticks([])


def rpatch(ax, x, y, w, h, fc, ec=INK, lw=1.2, alpha=1.0, rad=0.02):
    p = FancyBboxPatch((x, y), w, h,
                       boxstyle=f"round,pad=0.0,rounding_size={rad}",
                       fc=fc, ec=ec, lw=lw, alpha=alpha)
    ax.add_patch(p)
    return p


def text(ax, x, y, s, **kw):
    kw.setdefault("ha", "center"); kw.setdefault("va", "center")
    kw.setdefault("color", INK)
    ax.text(x, y, s, **kw)


# ============================================================
# Fig 1: ECS serialization vs OOP object-graph serialization
# ============================================================
def fig1_ecs_vs_oop():
    fig, ax = plt.subplots(figsize=(15.5, 9.6))
    clean_ax(ax)
    ax.set_xlim(0, 31); ax.set_ylim(0, 19)
    ax.set_title("Serialization:  ECS dump of contiguous arrays  vs  OOP object-graph walk",
                 fontsize=14, weight="bold", color=INK, loc="left", pad=12)

    # ---- Left half: ECS ----
    LX = 0.3
    text(ax, LX + 7.0, 18.0, "ECS  (data-oriented: serialize each component column)",
         fontsize=13.5, weight="bold", color=BLUE)

    # Two archetype tables, drawn as columns of arrays.
    tables = [
        ("Archetype {Pos, Vel}",  ["Pos", "Vel"],        4, LX + 0.2, 8.5),
        ("Archetype {Pos, Col}",  ["Pos", "Col"],        3, LX + 0.2, 1.0),
    ]
    col_w = 1.7; row_h = 0.85; head_h = 1.0
    comp_fc = {"Pos": LBLUE, "Vel": LGREEN, "Col": LAMBER, "HP": LRED}
    comp_ec = {"Pos": BLUE,  "Vel": GREEN,  "Col": AMBER,  "HP": RED}

    for name, comps, nrows, tx, ty in tables:
        rpatch(ax, tx - 0.15, ty - 0.4, col_w * len(comps) + 0.5 + 2.6, head_h + nrows * row_h + 0.5,
               fc=SOFT, ec=DGREY, lw=1.0, rad=0.04)
        text(ax, tx + (col_w * len(comps) + 2.6) / 2, ty + head_h + nrows * row_h + 0.15,
             name, fontsize=11.5, weight="bold", color=INK)
        # entity id label column
        text(ax, tx + 1.1, ty + head_h + nrows * row_h - 0.35, "entity", fontsize=9.5, color=DGREY)
        for i in range(nrows):
            yy = ty + head_h + (nrows - 1 - i) * row_h
            rpatch(ax, tx + 0.1, yy + 0.05, 2.0, row_h - 0.12, fc="white", ec=DGREY, lw=0.8, rad=0.02)
            text(ax, tx + 1.1, yy + row_h / 2, f"e{i}", fontsize=9.5, color=DGREY)
        for ci, c in enumerate(comps):
            cx = tx + 2.4 + ci * col_w
            text(ax, cx + col_w / 2, ty + head_h + nrows * row_h - 0.35, c, fontsize=10.5,
                 weight="bold", color=comp_ec[c])
            for i in range(nrows):
                yy = ty + head_h + (nrows - 1 - i) * row_h
                rpatch(ax, cx, yy + 0.05, col_w - 0.1, row_h - 0.12, fc=comp_fc[c], ec=comp_ec[c], lw=0.9, rad=0.02)

    # Arrow down: "serialize = dump each column"
    ax.add_patch(FancyArrowPatch((LX + 7.0, 7.9), (LX + 7.0, 6.6),
                 arrowstyle="-|>", mutation_scale=22, lw=2.0, color=GREEN))
    text(ax, LX + 7.0, 7.25, "serialize:  for each column,  memcpy the contiguous array",
         fontsize=11.5, color=GREEN, weight="bold")

    # Byte stream (flat, typed)
    rpatch(ax, LX + 0.2, 3.0, 13.8, 3.3, fc=LTEAL, ec=TEAL, lw=1.3, rad=0.04)
    text(ax, LX + 7.1, 5.85, "byte stream  (type-path -> values,  no pointers,  no cycles)",
         fontsize=11.5, weight="bold", color=TEAL)
    rows = [
        '"Pos":  [(0,0)(1,2)(3,1)(2,5)(7,7)(9,0)(4,3)]   <- 7 floats x 2, one memcpy',
        '"Vel":  [(1,0)(0,1)(2,2)(3,0)]                  <- only 4 entities have Vel',
        '"Col":  [(#f00)(#0f0)(#00f)]                    <- 3 entities',
    ]
    for i, r in enumerate(rows):
        text(ax, LX + 0.5, 5.15 - i * 0.7, r, fontsize=10.0, ha="left", family="monospace", color=INK)

    # ---- Right half: OOP ----
    RX = 16.0
    text(ax, RX + 7.3, 18.0, "OOP  (object graph: walk pointers, mind cycles)",
         fontsize=13.5, weight="bold", color=RED)

    # Object nodes scattered, connected by pointers (incl. a cycle).
    nodes = {
        "Hero":   (RX + 2.5, 15.0),
        "Sword":  (RX + 7.5, 16.0),
        "Enemy":  (RX + 10.5, 13.0),
        "Quest":  (RX + 5.0, 11.5),
        "Inv":    (RX + 1.5, 12.0),
    }
    fc_node = LRED
    # edges (pointer refs). Hero->Sword, Hero->Inv, Inv->Hero (cycle!), Hero->Quest,
    # Enemy->Hero, Quest->Enemy (cycle!)
    edges = [("Hero", "Sword"), ("Hero", "Inv"), ("Inv", "Hero"),
             ("Hero", "Quest"), ("Enemy", "Hero"), ("Quest", "Enemy")]
    for a, b in edges:
        (xa, ya) = nodes[a]; (xb, yb) = nodes[b]
        col = PURP if {a, b} == {"Hero", "Inv"} or {a, b} == {"Quest", "Enemy"} else DGREY
        lw = 2.0 if col == PURP else 1.2
        rad = 0.35 if a == "Inv" and b == "Hero" else (0.25 if a == "Quest" and b == "Enemy" else 0.0)
        ax.add_patch(FancyArrowPatch((xa, ya), (xb, yb), arrowstyle="-|>",
                     mutation_scale=16, lw=lw, color=col,
                     connectionstyle=f"arc3,rad={rad}", alpha=0.85))
    for nm, (x, y) in nodes.items():
        c = Circle((x, y), 0.85, fc=fc_node, ec=RED, lw=1.4)
        ax.add_patch(c)
        text(ax, x, y, nm, fontsize=10.5, weight="bold", color=INK)

    # legend for cycle
    ax.add_patch(FancyArrowPatch((RX + 12.6, 17.0), (RX + 13.4, 17.0),
                 arrowstyle="-|>", mutation_scale=14, lw=2.0, color=PURP))
    text(ax, RX + 13.0, 17.45, "cycle", fontsize=9.5, color=PURP, weight="bold")

    # Arrow down
    ax.add_patch(FancyArrowPatch((RX + 6.0, 10.4), (RX + 6.0, 9.0),
                 arrowstyle="-|>", mutation_scale=22, lw=2.0, color=RED))
    text(ax, RX + 6.0, 9.7, "serialize:  walk graph,  assign each object an id,\nresolve pointers to ids,  detect cycles",
         fontsize=11.0, color=RED, weight="bold")

    # Byte stream (with id table + forward refs)
    rpatch(ax, RX + 0.2, 3.0, 14.6, 5.4, fc=LRED, ec=RED, lw=1.3, rad=0.04)
    text(ax, RX + 7.5, 7.95, "byte stream  (object id table + per-field ref ids)",
         fontsize=11.5, weight="bold", color=RED)
    orows = [
        'id=0  Hero   { hp:80, weapon:->1, inv:->2, quest:->3 }',
        'id=1  Sword  { dmg:12 }',
        'id=2  Inv    { owner:->0, items:[...] }          <- back-ref (cycle)',
        'id=3  Quest  { target:->4 }',
        'id=4  Enemy  { target:->0 }                      <- back-ref (cycle)',
        '+ need: type tags,  cycle-breaking (seen-set),  id relocation on load',
    ]
    for i, r in enumerate(orows):
        text(ax, RX + 0.5, 7.2 - i * 0.66, r, fontsize=9.8, ha="left", family="monospace", color=INK)

    # bottom contrast labels
    text(ax, LX + 7.1, 1.6, "flat,  typed,  cache-friendly\nno pointers to fix up",
         fontsize=10.5, color=TEAL, weight="bold")
    text(ax, RX + 7.5, 1.6, "graph,  needs id remap,\ncycle handling,  pointer fixup",
         fontsize=10.5, color=RED, weight="bold")

    fig.savefig(os.path.join(OUT, "fig-p4_16_01-ecs-vs-oop-serialize.png"))
    plt.close(fig)


# ============================================================
# Fig 2: Entity id remapping
# ============================================================
def fig2_entity_id_remap():
    fig, ax = plt.subplots(figsize=(15.5, 9.2))
    clean_ax(ax)
    ax.set_xlim(0, 31); ax.set_ylim(0, 18)
    ax.set_title("Entity id remapping:  serialized ids are LOCAL;  on load they are reallocated",
                 fontsize=14, weight="bold", color=INK, loc="left", pad=12)

    # Three columns: save / file / load
    colx = [1.0, 11.5, 22.0]
    titles = [
        "1. SAVE  (world -> file)",
        "2. FILE  (local ids)",
        "3. LOAD  (file -> fresh world)",
    ]
    tcols = [BLUE, AMBER, GREEN]
    for x, t, c in zip(colx, titles, tcols):
        text(ax, x + 4.0, 17.0, t, fontsize=13, weight="bold", color=c)

    # ---- SAVE side: a tiny world ----
    sx = colx[0]
    rpatch(ax, sx, 9.0, 8.5, 6.8, fc=LBLUE, ec=BLUE, lw=1.3, rad=0.04)
    text(ax, sx + 4.25, 15.2, "running world", fontsize=11.5, weight="bold", color=BLUE)
    world_rows = [
        ("e3", "Pos(2,5)",  "Vel(1,0)"),
        ("e7", "Pos(9,1)",  "Weapon(target: e12)"),
        ("e12", "Pos(4,4)", "Health(80)"),
    ]
    for i, (eid, c1, c2) in enumerate(world_rows):
        yy = 14.0 - i * 1.4
        rpatch(ax, sx + 0.4, yy - 0.45, 7.7, 1.1, fc="white", ec=DGREY, lw=0.9, rad=0.03)
        text(ax, sx + 1.1, yy, eid, fontsize=10.5, weight="bold", color=BLUE)
        text(ax, sx + 3.3, yy, c1, fontsize=9.5, family="monospace", color=INK)
        text(ax, sx + 6.3, yy, c2, fontsize=9.0, family="monospace", color=PURP)

    # arrow SAVE -> FILE
    ax.add_patch(FancyArrowPatch((sx + 8.7, 12.4), (colx[1] - 0.2, 12.4),
                 arrowstyle="-|>", mutation_scale=22, lw=2.0, color=AMBER))
    text(ax, (sx + 8.7 + colx[1]) / 2, 13.0, "serialize\n(reflect + serde)",
         fontsize=10, color=AMBER, weight="bold")

    # ---- FILE column: text scene ----
    fx = colx[1]
    rpatch(ax, fx, 3.2, 9.5, 12.6, fc=LAMBER, ec=AMBER, lw=1.3, rad=0.04)
    text(ax, fx + 4.75, 15.2, "scene file  (.scn / .ron)", fontsize=11.5, weight="bold", color=AMBER)
    file_lines = [
        'entities: [',
        '  { id: 0, Pos(2,5), Vel(1,0) },',
        '  { id: 1, Pos(9,1),',
        '      Weapon(target: 2) },     <- local id',
        '  { id: 2, Pos(4,4), Health(80) },',
        ']',
        '',
        '(ids are LOCAL to this file,',
        ' reassigned 0..N on save)',
    ]
    for i, ln in enumerate(file_lines):
        col = PURP if "local id" in ln else INK
        text(ax, fx + 0.4, 14.4 - i * 0.66, ln, fontsize=9.8, ha="left",
             family="monospace", color=col)

    # arrow FILE -> LOAD
    ax.add_patch(FancyArrowPatch((fx + 9.7, 12.4), (colx[2] - 0.2, 12.4),
                 arrowstyle="-|>", mutation_scale=22, lw=2.0, color=GREEN))
    text(ax, (fx + 9.7 + colx[2]) / 2, 13.0, "deserialize\n+ remap",
         fontsize=10, color=GREEN, weight="bold")

    # ---- LOAD side: a NEW world with fresh ids ----
    lx = colx[2]
    rpatch(ax, lx, 9.0, 8.5, 6.8, fc=LGREEN, ec=GREEN, lw=1.3, rad=0.04)
    text(ax, lx + 4.25, 15.2, "fresh world (ids reallocated)", fontsize=11.5, weight="bold", color=GREEN)
    load_rows = [
        ("e21", "Pos(2,5)",  "Vel(1,0)"),
        ("e22", "Pos(9,1)",  "Weapon(target: e23)  <- rewritten"),
        ("e23", "Pos(4,4)", "Health(80)"),
    ]
    for i, (eid, c1, c2) in enumerate(load_rows):
        yy = 14.0 - i * 1.4
        rpatch(ax, lx + 0.4, yy - 0.45, 7.7, 1.1, fc="white", ec=DGREY, lw=0.9, rad=0.03)
        text(ax, lx + 1.1, yy, eid, fontsize=10.5, weight="bold", color=GREEN)
        text(ax, lx + 3.3, yy, c1, fontsize=9.5, family="monospace", color=INK)
        text(ax, lx + 6.3, yy, c2, fontsize=8.6, family="monospace", color=PURP)

    # remap table at the bottom
    rpatch(ax, lx - 0.5, 3.6, 9.5, 4.4, fc=LPURP, ec=PURP, lw=1.2, rad=0.04)
    text(ax, lx + 4.25, 7.55, "id remap table  (built during load)", fontsize=10.8, weight="bold", color=PURP)
    remap = [
        "file id 0  ->  world e21",
        "file id 1  ->  world e22",
        "file id 2  ->  world e23",
    ]
    for i, r in enumerate(remap):
        text(ax, lx + 0.0, 6.8 - i * 0.7, r, fontsize=10.0, ha="left", family="monospace", color=INK)
    text(ax, lx + 4.25, 4.2,
         "Weapon stored target=2  ->  rewritten to e23\n(component implements ReflectMapEntities hook)",
         fontsize=9.2, color=PURP, style="italic")

    # step labels under each column
    text(ax, sx + 4.25, 8.4, "entity ids come from the\nlive Entities allocator",
         fontsize=9.5, color=BLUE)
    text(ax, fx + 4.75, 2.6, "ids are reassigned 0..N\nso the file is self-contained",
         fontsize=9.5, color=AMBER)
    text(ax, lx + 4.25, 2.9, "ids may differ from save time;\nrefs inside components MUST be rewritten",
         fontsize=9.5, color=GREEN)

    fig.savefig(os.path.join(OUT, "fig-p4_16_02-entity-id-remap.png"))
    plt.close(fig)


# ============================================================
# Fig 3: Scene file structure
# ============================================================
def fig3_scene_file_structure():
    fig, ax = plt.subplots(figsize=(15.0, 9.6))
    clean_ax(ax)
    ax.set_xlim(0, 30); ax.set_ylim(0, 19)
    ax.set_title("Scene file structure:  entities x typed components,  handles stored as asset paths",
                 fontsize=14, weight="bold", color=INK, loc="left", pad=12)

    # Left: a small in-world scene (3 entities) to be saved
    LX = 0.3
    text(ax, LX + 6.5, 18.0, "in-world:  3 entities with mixed components",
         fontsize=12.5, weight="bold", color=BLUE)
    rpatch(ax, LX, 9.0, 13.0, 8.2, fc=SOFT, ec=DGREY, lw=1.1, rad=0.04)

    ents = [
        ("e0  Hero",   [("Transform", LBLUE, BLUE), ("Health", LRED, RED), ("Sprite", LAMBER, AMBER)]),
        ("e1  Sword",  [("Transform", LBLUE, BLUE), ("Damage", LGREEN, GREEN)]),
        ("e2  Camera", [("Transform", LBLUE, BLUE), ("Camera", LPURP, PURP)]),
    ]
    for i, (name, comps) in enumerate(ents):
        yy = 15.5 - i * 2.2
        text(ax, LX + 1.6, yy + 0.7, name, fontsize=11, weight="bold", color=INK, ha="left")
        for j, (cn, fc, ec) in enumerate(comps):
            cx = LX + 0.4 + j * 4.0
            rpatch(ax, cx, yy - 0.5, 3.6, 1.1, fc=fc, ec=ec, lw=1.1, rad=0.04)
            text(ax, cx + 1.8, yy + 0.05, cn, fontsize=10, weight="bold", color=ec)
    # note: Sprite's texture handle
    text(ax, LX + 6.5, 9.6,
         "Sprite.texture = Handle<Image>   (a light id,  NOT the 30MB pixel data)",
         fontsize=9.5, color=AMBER, style="italic")

    # arrow -> file
    ax.add_patch(FancyArrowPatch((LX + 13.3, 13.0), (16.0, 13.0),
                 arrowstyle="-|>", mutation_scale=24, lw=2.2, color=AMBER))
    text(ax, 14.65, 13.7, "serialize", fontsize=11, color=AMBER, weight="bold")

    # Right: the resulting scene file (text form)
    FX = 16.0
    rpatch(ax, FX, 1.0, 13.7, 16.6, fc=LTEAL, ec=TEAL, lw=1.3, rad=0.04)
    text(ax, FX + 6.85, 16.9, "scene file  (text form,  e.g. RON)",
         fontsize=12.5, weight="bold", color=TEAL)

    lines = [
        '(',
        '    resources: {},',
        '    entities: {',
        '        0: (                       // Hero',
        '            Transform(x:0, y:0, rot:0),',
        '            Health(max:100, now:80),',
        '            Sprite(texture: "sprites/hero.png"),  <- asset PATH',
        '        ),',
        '        1: (                       // Sword',
        '            Transform(x:1, y:0, rot:0),',
        '            Damage(12),',
        '        ),',
        '        2: (                       // Camera',
        '            Transform(x:0, y:0, rot:0),',
        '            Camera(near:0.1, far:1000),',
        '        ),',
        '    },',
        ')',
    ]
    for i, ln in enumerate(lines):
        col = AMBER if "asset PATH" in ln else (PURP if "//" in ln else INK)
        text(ax, FX + 0.5, 16.0 - i * 0.72, ln, fontsize=10.0, ha="left",
             family="monospace", color=col)

    # callouts on the right margin
    text(ax, FX + 13.4, 12.6, "<- entity key\n   (local id,",
         fontsize=8.8, color=PURP, ha="left")
    text(ax, FX + 13.4, 11.9, "   remapped on load)",
         fontsize=8.8, color=PURP, ha="left")
    text(ax, FX + 13.4, 9.4, "<- component is\n   fully self-contained\n   data; no pointers",
         fontsize=8.8, color=BLUE, ha="left")
    text(ax, FX + 13.4, 6.7, "<- Handle stored as\n   asset PATH, not id;\n   AssetServer reloads\n   the 30MB on demand",
         fontsize=8.8, color=AMBER, ha="left")

    fig.savefig(os.path.join(OUT, "fig-p4_16_03-scene-file-structure.png"))
    plt.close(fig)


if __name__ == "__main__":
    fig1_ecs_vs_oop()
    fig2_entity_id_remap()
    fig3_scene_file_structure()
    print("Generated 3 figures for P4-16.")
