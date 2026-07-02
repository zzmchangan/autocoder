import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch, Rectangle, Circle

C_TANK   = '#E8743B'   # orange - tank A (gen 0)
C_BULLET = '#2F6DB5'   # blue   - bullet B (gen 1)
C_SLOT   = '#4C9A2A'   # green  - slot / generations array
C_OLD    = '#C62828'   # red    - stale handle
C_BG     = '#F5F7FA'
C_EDGE   = '#33415C'

fig, ax = plt.subplots(figsize=(13, 8.0))
ax.set_xlim(0, 13)
ax.set_ylim(0, 8.0)
ax.axis('off')
ax.set_facecolor('white')

def box(x, y, w, h, color, text, fontsize=11, fc=None, ec=C_EDGE, lw=1.5, weight='normal', tcolor=None):
    p = FancyBboxPatch((x, y), w, h,
                       boxstyle="round,pad=0.04,rounding_size=0.12",
                       linewidth=lw, edgecolor=ec,
                       facecolor=(fc if fc else color), alpha=0.95)
    ax.add_patch(p)
    if tcolor is None: tcolor = 'white'
    ax.text(x + w/2, y + h/2, text, ha='center', va='center',
            fontsize=fontsize, color=tcolor, weight=weight, zorder=5)

def arrow(x1, y1, x2, y2, color=C_EDGE, style='-|>', lw=1.8, ls='-', text=None,
          tcolor=None, tbg=None, rad=0.0, tfontsize=9):
    a = FancyArrowPatch((x1, y1), (x2, y2),
                        arrowstyle=style, mutation_scale=16,
                        color=color, lw=lw, linestyle=ls,
                        connectionstyle=f"arc3,rad={rad}", zorder=3)
    ax.add_patch(a)
    if text:
        if tcolor is None: tcolor = color
        ax.text((x1+x2)/2, (y1+y2)/2 + (0.2 if rad else 0), text,
                ha='center', va='center', fontsize=tfontsize, color=tcolor,
                weight='bold',
                bbox=dict(boxstyle='round,pad=0.22', fc=(tbg if tbg else 'white'),
                          ec=color, lw=1.0))

# ---- Title ----
ax.text(6.5, 7.7,
        'Entity Generation: defeating Id-reuse mismatch when recycled Ids change owner',
        ha='center', va='center', fontsize=12.5, weight='bold', color=C_EDGE)

# ============ TOP: the _generations slots row ============
ax.text(0.6, 6.95, '_generations[]  (one generation counter per Id slot)',
        ha='left', va='center', fontsize=10, weight='bold', color=C_SLOT)

slot_y = 6.05
slot_w = 1.55
slot_h = 0.7
n_slots = 6
x0 = 0.6
for i in range(n_slots):
    x = x0 + i * (slot_w + 0.18)
    is_target = (i == 2)  # slot id=2 highlighted (we'll narrate id=2)
    fc = '#FFF3E0' if is_target else C_BG
    ec = C_TANK if is_target else C_EDGE
    p = FancyBboxPatch((x, slot_y), slot_w, slot_h,
                       boxstyle="round,pad=0.03,rounding_size=0.08",
                       linewidth=1.8, edgecolor=ec, facecolor=fc)
    ax.add_patch(p)
    ax.text(x + slot_w/2, slot_y + slot_h*0.7, f"slot {i}", ha='center', va='center',
            fontsize=9, color=C_EDGE, weight='bold')
    gen_val = 1 if is_target else 0
    ax.text(x + slot_w/2, slot_y + slot_h*0.32, f"gen = {gen_val}",
            ha='center', va='center', fontsize=10.5,
            color=(C_TANK if is_target else C_SLOT), weight='bold')

# recycled queue label
ax.text(x0 + n_slots*(slot_w+0.18) + 0.1, slot_y + slot_h/2,
        "<-- _recycledIds queue\n     (id=2 recycled)",
        ha='left', va='center', fontsize=8.5, color=C_BULLET, style='italic')

# ============ TIMELINE: 4 steps ============
steps = [
    ("Frame 100", "Tank A spawns",        "Entity(id=2, gen=0)",   C_TANK,
     "CreateEntity() -> id=2 from fresh counter, gen=0"),
    ("Frame 150", "Tank A destroyed",     "gens[2]++ -> 1", C_TANK,
     "DestroyEntity: remove components, push id=2 to _recycledIds, gen++"),
    ("Frame 151", "Bullet B spawns",      "Entity(id=2, gen=1)",   C_BULLET,
     "CreateEntity() -> id=2 dequeued from _recycledIds, NEW gen=1"),
    ("Frame 152", "Stale handle arrives", "Entity(id=2, gen=0)",   C_OLD,
     "delayed callback still holds Tank A's handle -> IsValid == FALSE -> op rejected"),
]

ystep = 1.18
ytop = 5.55
for i, (frame, action, handle, color, detail) in enumerate(steps):
    y = ytop - i * ystep
    # frame badge
    box(0.6, y - 0.42, 1.7, 0.84, C_BG, frame, fontsize=10, fc=C_BG, ec=color,
        weight='bold', tcolor=color)
    # action
    box(2.5, y - 0.42, 3.2, 0.84, color, action, fontsize=10, weight='bold')
    # handle / state
    box(5.9, y - 0.42, 3.5, 0.84, color, handle, fontsize=10.5, weight='bold')
    # detail
    box(9.6, y - 0.42, 3.0, 0.84, C_BG, detail, fontsize=8.3, fc=C_BG, ec=C_EDGE, tcolor=C_EDGE)

# column headers
yh = ytop + 0.62
ax.text(0.6 + 1.7/2, yh, "time", ha='center', va='center', fontsize=9.5, weight='bold', color=C_EDGE)
ax.text(2.5 + 3.2/2, yh, "event", ha='center', va='center', fontsize=9.5, weight='bold', color=C_EDGE)
ax.text(5.9 + 3.5/2, yh, "entity handle", ha='center', va='center', fontsize=9.5, weight='bold', color=C_EDGE)
ax.text(9.6 + 3.0/2, yh, "what happens", ha='center', va='center', fontsize=9.5, weight='bold', color=C_EDGE)

# ---- highlight arrow: stale handle gen=0 vs slot gen=1 mismatch ----
# draw a red bracket from the last step's handle up to the slot gen=1
ax.annotate("", xy=(x0 + 2*(slot_w+0.18) + slot_w/2, slot_y),
            xytext=(7.6, ytop - 3*ystep + 0.42),
            arrowprops=dict(arrowstyle='-|>', color=C_OLD, lw=2.0,
                            connectionstyle="arc3,rad=-0.25"))
ax.text(7.0, 0.95,
        "gen 0  !=  slot gen 1   ->   IsValid() returns false   ->   operation rejected",
        ha='center', va='center', fontsize=10, weight='bold', color=C_OLD,
        bbox=dict(boxstyle='round,pad=0.3', fc='#FFEBEE', ec=C_OLD, lw=1.4))

plt.tight_layout()
out = r'c:/Users/86133/Desktop/深入浅出系列/帧同步设计与实现/images/fig-05-entity-generation.png'
plt.savefig(out, dpi=150, bbox_inches='tight', facecolor='white')
print("saved", out)
