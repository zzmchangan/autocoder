import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch, Rectangle

# ---- palette (match series style) ----
C_LOGIC   = '#2F6DB5'   # blue   - System (logic)
C_DATA    = '#4C9A2A'   # green  - Component (data, serializable)
C_HANDLE  = '#E8743B'   # orange - Entity (handle)
C_REPLAY  = '#9C27B0'   # purple - presentation gated
C_BG      = '#F5F7FA'
C_EDGE    = '#33415C'
C_DANGER  = '#C62828'

fig, ax = plt.subplots(figsize=(13, 8.2))
ax.set_xlim(0, 13)
ax.set_ylim(0, 8.2)
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
        ax.text((x1+x2)/2, (y1+y2)/2 + (0.18 if rad else 0), text,
                ha='center', va='center', fontsize=tfontsize, color=tcolor,
                weight='bold',
                bbox=dict(boxstyle='round,pad=0.22', fc=(tbg if tbg else 'white'),
                          ec=color, lw=1.0))

# ---- Title ----
ax.text(6.5, 7.85,
        'Lockstep ECS: Logic / Data Separation (rollback needs the whole world to be byte-serializable)',
        ha='center', va='center', fontsize=12.5, weight='bold', color=C_EDGE)

# ============ LEFT COLUMN: System ============
box(0.4, 4.5, 3.6, 1.0, C_LOGIC,
    "SYSTEM  (logic)\nstateless pure function", fontsize=10.5, weight='bold')
box(0.4, 5.62, 3.6, 1.05, C_BG,
    "void Update(World w, int frame)\n  - reads component data\n  - writes component data\n  - NO hidden state, NO refs",
    fontsize=9, fc=C_BG, ec=C_LOGIC, tcolor=C_EDGE)

# ============ CENTER COLUMN: Component pools (data) ============
box(4.7, 4.5, 3.6, 1.0, C_DATA,
    "COMPONENTS  (data)\nfully serializable", fontsize=10.5, weight='bold')
box(4.7, 5.62, 3.6, 1.05, C_BG,
    "PositionComponent[]\nVelocityComponent[]\nHealthComponent[]\n  -> SaveState / LoadState bytes",
    fontsize=9, fc=C_BG, ec=C_DATA, tcolor=C_EDGE)

# ============ RIGHT COLUMN: Entity ============
box(9.0, 4.5, 3.6, 1.0, C_HANDLE,
    "ENTITY  (handle)\nreadonly struct", fontsize=10.5, weight='bold')
box(9.0, 5.62, 3.6, 1.05, C_BG,
    "int Id\nint Generation\n  (8 bytes, value type)\n  holds NO component ref",
    fontsize=9, fc=C_BG, ec=C_HANDLE, tcolor=C_EDGE)

# ---- relations between top three ----
arrow(4.0, 5.0, 4.7, 5.0, color=C_EDGE, lw=2.0,
      text="read/write by Id", tbg='white', tfontsize=8.5)
arrow(8.3, 5.0, 9.0, 5.0, color=C_EDGE, lw=2.0,
      text="indexes pools", tbg='white', tfontsize=8.5)

# ============ MIDDLE: one tick of the loop ============
box(4.2, 3.0, 4.6, 0.95, C_BG,
    "Each tick:  System.Update(world, frame)\n  stateless  +  same world  +  same input  ->  same result",
    fontsize=9.5, fc=C_BG, ec=C_EDGE, weight='bold', tcolor=C_EDGE)
arrow(2.2, 4.5, 5.0, 3.95, color=C_LOGIC, lw=1.8, rad=0.1)
arrow(6.5, 4.5, 6.5, 3.95, color=C_DATA, lw=1.8)
arrow(10.8, 4.5, 8.0, 3.95, color=C_HANDLE, lw=1.8, rad=-0.1)

# ============ BOTTOM: rollback lane ============
# snapshot bar
box(0.4, 1.5, 12.2, 1.05, C_BG,
    "ROLLBACK  (frame N was wrong -> rewind world to a snapshot, replay with correct input)",
    fontsize=10.5, fc=C_BG, ec=C_DANGER, weight='bold', tcolor=C_DANGER)

# save -> bytes
box(0.6, 0.15, 3.6, 1.0, C_DATA,
    "1) SaveState()\nworld -> byte stream\n(snapshot, byte-exact\n on both peers)",
    fontsize=9, fc=C_DATA, weight='bold')
# restore
box(4.7, 0.15, 3.6, 1.0, C_DATA,
    "2) LoadState(bytes)\nbyte stream -> world\n(components fully\n restored)",
    fontsize=9, fc=C_DATA, weight='bold')
# replay
box(8.8, 0.15, 3.6, 1.0, C_LOGIC,
    "3) replay frames\nSystem.Update runs again\non restored data",
    fontsize=9, fc=C_LOGIC, weight='bold')

arrow(4.2, 0.65, 4.7, 0.65, color=C_DANGER, lw=2.0)
arrow(8.3, 0.65, 8.8, 0.65, color=C_DANGER, lw=2.0)

# ============ GATE: IsReplaying isolates presentation ============
# gate badge to the right of replay
box(0.4, 2.75, 3.4, 0.0, C_REPLAY, "", fc='white')  # spacer noop (removed below)
# presentation layer box top-right, gated
box(9.0, 2.75, 3.6, 1.0, C_REPLAY,
    "PRESENTATION (non-serializable)\nanimation / particles / sfx",
    fontsize=9.5, fc=C_REPLAY, weight='bold')
box(9.0, 1.5, 3.6, 1.0, C_BG,
    "GATED by IsReplaying == true\n  -> side effects SKIPPED\n  (no replay double-fx)",
    fontsize=9, fc=C_BG, ec=C_REPLAY, tcolor=C_EDGE)
arrow(10.8, 2.75, 10.8, 2.5, color=C_REPLAY, lw=2.0,
      text="gate", tbg='white', tfontsize=8.5)

# contract banner
banner = FancyBboxPatch((0.4, 7.02), 12.2, 0.0, linewidth=0)  # noop
ax.text(6.5, 7.02,
        "Discipline: all state that affects the outcome lives in serializable components; "
        "logic is a pure function; presentation is gated during replay.",
        ha='center', va='center', fontsize=9.5, color=C_EDGE, style='italic')

plt.tight_layout()
out = r'c:/Users/86133/Desktop/深入浅出系列/帧同步设计与实现/images/fig-05-ecs-logic-data-separation.png'
plt.savefig(out, dpi=150, bbox_inches='tight', facecolor='white')
print("saved", out)
