import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch, Rectangle

C_FLOW  = '#2F6DB5'   # blue   - flow boxes
C_DEC   = '#E8743B'   # orange - decision
C_BAD   = '#C62828'   # red    - rejected / throw
C_OK    = '#4C9A2A'   # green  - pass
C_PASS  = '#00897B'   # teal   - AllowUnsafeField bypass
C_BG    = '#F5F7FA'
C_EDGE  = '#33415C'

fig, ax = plt.subplots(figsize=(13.5, 8.8))
ax.set_xlim(0, 13.5)
ax.set_ylim(0, 8.8)
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

def diamond(x, y, w, h, color, text, fontsize=9.5):
    from matplotlib.patches import Polygon
    pts = [(x + w/2, y + h), (x + w, y + h/2), (x + w/2, y), (x, y + h/2)]
    poly = Polygon(pts, closed=True, facecolor=color, edgecolor=C_EDGE, linewidth=1.6, alpha=0.92)
    ax.add_patch(poly)
    ax.text(x + w/2, y + h/2, text, ha='center', va='center',
            fontsize=fontsize, color='white', weight='bold', zorder=5)

def arrow(x1, y1, x2, y2, color=C_EDGE, style='-|>', lw=1.8, ls='-', text=None,
          tcolor=None, tbg=None, rad=0.0, tfontsize=9):
    a = FancyArrowPatch((x1, y1), (x2, y2),
                        arrowstyle=style, mutation_scale=16,
                        color=color, lw=lw, linestyle=ls,
                        connectionstyle=f"arc3,rad={rad}", zorder=3)
    ax.add_patch(a)
    if text:
        if tcolor is None: tcolor = color
        ax.text((x1+x2)/2 + (0.25 if rad else 0), (y1+y2)/2, text,
                ha='center', va='center', fontsize=tfontsize, color=tcolor,
                weight='bold',
                bbox=dict(boxstyle='round,pad=0.18', fc=(tbg if tbg else 'white'),
                          ec=color, lw=1.0))

# ---- Title ----
ax.text(6.75, 8.5,
        'SystemStateValidator: reflection scan at System.Initialize (DEBUG only)',
        ha='center', va='center', fontsize=12.5, weight='bold', color=C_EDGE)

# ============ LEFT: flow ============
# 1. Initialize
box(0.5, 7.0, 3.4, 0.9, C_FLOW, "SystemBase.Initialize(world)", fontsize=10.5, weight='bold')
# 2. ValidateSystemState (Conditional DEBUG)
box(0.5, 5.85, 3.4, 0.9, C_FLOW, "ValidateSystemState()\n[Conditional(\"DEBUG\")]", fontsize=10, fc=C_FLOW)
# 3. reflect
box(0.5, 4.7, 3.4, 0.9, C_FLOW, "reflect all instance\n+ static fields", fontsize=10, fc=C_FLOW)

# 4. decision: marked AllowUnsafeField?
diamond(0.7, 3.3, 3.0, 1.15, C_DEC,
        "field has\n[AllowUnsafeField]?")

# 5. scan against dangerous lists
diamond(0.7, 1.7, 3.0, 1.15, C_DEC,
        "type / name\nmatches a\nviolation list?")

# arrows down the left chain
arrow(2.2, 7.0, 2.2, 6.75, color=C_FLOW, lw=2.0)
arrow(2.2, 5.85, 2.2, 5.6, color=C_FLOW, lw=2.0)
arrow(2.2, 4.7, 2.2, 4.45, color=C_FLOW, lw=2.0)
arrow(2.2, 3.3, 2.2, 2.85, color=C_FLOW, lw=2.0, text="NO", tbg='white', tfontsize=9)

# AllowUnsafe YES -> skip right
arrow(3.7, 3.875, 5.6, 3.875, color=C_PASS, lw=2.0, text="YES  -> skip", tbg='white', tfontsize=9)
box(5.6, 3.45, 3.0, 0.85, C_PASS,
    "SKIP field\n(reason logged)", fontsize=9.5, fc=C_PASS)

# ============ decision NO path enters scan; if matches -> throw ============
arrow(3.7, 2.275, 5.6, 2.275, color=C_BAD, lw=2.0, text="YES -> violation", tbg='white', tfontsize=9)
box(5.6, 1.85, 3.0, 0.85, C_BAD,
    "Log.Error + THROW\nInvalidOperationException", fontsize=9, fc=C_BAD, weight='bold')

# scan NO -> all clear
arrow(2.2, 1.7, 2.2, 1.2, color=C_OK, lw=2.0, text="NO", tbg='white', tfontsize=9, rad=0.0)
box(0.5, 0.25, 3.4, 0.9, C_OK, "all fields clear\nSystem registers OK", fontsize=10, fc=C_OK, weight='bold')

# ============ RIGHT: violation checklist ============
ax.text(9.05, 7.55, "Violation checklist (DEBUG scan)",
        ha='center', va='center', fontsize=11, weight='bold', color=C_EDGE)

checks = [
    (C_BAD, "World / Entity cached refs",     "stale after rollback"),
    (C_BAD, "Dictionary<,> / HashSet<> / Hashtable", "iteration order undefined"),
    (C_BAD, "System.Random / Guid / Stopwatch",      "machine-dependent"),
    (C_BAD, "Task / ValueTask / Thread / Timer",     "must be single-thread sync"),
    (C_BAD, "static fields (non-readonly, non-Delegate)", "global, not snapshot-restored"),
    (C_BAD, "names: lastframe / cachedEntity ...",   "smells like cached frame/entity"),
]
yc = 6.9
ch = 0.62
for color, head, sub in checks:
    r = Rectangle((9.05, yc - ch), 4.2, ch, facecolor=C_BG, edgecolor=color, linewidth=1.4)
    ax.add_patch(r)
    ax.text(9.18, yc - ch/2 + 0.08, "X", fontsize=10, weight='bold', color=color, va='center')
    ax.text(9.5, yc - ch/2 + 0.12, head, fontsize=9.2, weight='bold', color=C_EDGE, va='center')
    ax.text(9.5, yc - ch/2 - 0.13, sub, fontsize=8, color=C_EDGE, va='center', style='italic')
    yc -= ch + 0.08

# honest note: float NOT caught
r = Rectangle((9.05, yc - ch), 4.2, ch, facecolor='#FFF8E1', edgecolor='#F9A825', linewidth=1.6)
ax.add_patch(r)
ax.text(9.18, yc - ch/2 + 0.08, "!", fontsize=11, weight='bold', color='#F9A825', va='center')
ax.text(9.5, yc - ch/2 + 0.12, "float NOT in the reject list",
        fontsize=9.2, weight='bold', color='#F9A825', va='center')
ax.text(9.5, yc - ch/2 - 0.13, "use LFloat (fixed-point) by discipline",
        fontsize=8, color=C_EDGE, va='center', style='italic')

# ============ bottom banner ============
banner = FancyBboxPatch((0.5, 0.0), 0.0, 0.0, linewidth=0)
ax.text(4.4, 0.55,
        "Release: [Conditional(\"DEBUG\")] strips ValidateSystemState() from IL  ->  zero runtime cost",
        ha='left', va='center', fontsize=9.3, color=C_EDGE, style='italic')

plt.tight_layout()
out = r'c:/Users/86133/Desktop/深入浅出系列/帧同步设计与实现/images/fig-05-system-state-validator.png'
plt.savefig(out, dpi=150, bbox_inches='tight', facecolor='white')
print("saved", out)
