import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch, Rectangle, Circle
from matplotlib.lines import Line2D

# Colors
C_SERVER = '#E8743B'   # orange
C_CLIENT = '#2F6DB5'   # blue
C_INPUT  = '#4C9A2A'   # green
C_RESULT = '#9C27B0'   # purple
C_BG     = '#F5F7FA'
C_EDGE   = '#33415C'

fig, ax = plt.subplots(figsize=(12, 7.6))
ax.set_xlim(0, 12)
ax.set_ylim(0, 7.6)
ax.axis('off')
ax.set_facecolor('white')

def box(x, y, w, h, color, text, fontsize=11, fc=None, ec=C_EDGE, lw=1.5, weight='normal'):
    p = FancyBboxPatch((x, y), w, h,
                       boxstyle="round,pad=0.04,rounding_size=0.12",
                       linewidth=lw, edgecolor=ec,
                       facecolor=(fc if fc else color), alpha=0.95)
    ax.add_patch(p)
    ax.text(x + w/2, y + h/2, text, ha='center', va='center',
            fontsize=fontsize, color='white', weight=weight, zorder=5)

def arrow(x1, y1, x2, y2, color=C_EDGE, style='-|>', lw=1.8, ls='-', text=None,
          tcolor=None, tbg=None, rad=0.0, tfontsize=9):
    a = FancyArrowPatch((x1, y1), (x2, y2),
                        arrowstyle=style, mutation_scale=16,
                        color=color, lw=lw, linestyle=ls,
                        connectionstyle=f"arc3,rad={rad}", zorder=3)
    ax.add_patch(a)
    if text:
        if tcolor is None: tcolor = color
        ax.text((x1+x2)/2, (y1+y2)/2, text,
                ha='center', va='center', fontsize=tfontsize, color=tcolor,
                weight='bold',
                bbox=dict(boxstyle='round,pad=0.25', fc=(tbg if tbg else 'white'),
                          ec=color, lw=1.0) if tbg else None)

# ---- Title ----
ax.text(6, 7.25, 'Lockstep Minimal Model: Server Forwards Inputs, Clients Compute Identical State',
        ha='center', va='center', fontsize=12.5, weight='bold', color=C_EDGE)

# ---- Server box (center top) ----
box(4.5, 5.3, 3.0, 1.0, C_SERVER,
    "SERVER (relay only)\nCollects inputs, broadcasts\n[ NEVER computes game state ]",
    fontsize=10, weight='bold')

# ---- Client A (left) ----
box(0.4, 3.1, 3.4, 1.4, C_CLIENT,
    "CLIENT A\n(Your phone)",
    fontsize=11, weight='bold')
# input box client A
box(0.4, 4.65, 3.4, 0.55, C_INPUT,
    "input: [A=idle,  B=right]", fontsize=9.5, fc=C_INPUT)
# result box client A
box(0.4, 1.3, 3.4, 1.5, C_RESULT,
    "Runs SAME logic locally\n-> A stays,  B moves +1\nRESULT = state_A",
    fontsize=9.5, fc=C_RESULT)

# ---- Client B (right) ----
box(8.2, 3.1, 3.4, 1.4, C_CLIENT,
    "CLIENT B\n(Opponent's phone)",
    fontsize=11, weight='bold')
box(8.2, 4.65, 3.4, 0.55, C_INPUT,
    "input: [A=idle,  B=right]", fontsize=9.5, fc=C_INPUT)
box(8.2, 1.3, 3.4, 1.5, C_RESULT,
    "Runs SAME logic locally\n-> A stays,  B moves +1\nRESULT = state_B",
    fontsize=9.5, fc=C_RESULT)

# ---- Arrows: clients send inputs to server ----
arrow(2.1, 4.65, 4.9, 5.95, color=C_INPUT, lw=2.0,
      text="(1) send input", tbg='white', tfontsize=9)
arrow(9.9, 4.65, 7.1, 5.95, color=C_INPUT, lw=2.0,
      text="(1) send input", tbg='white', tfontsize=9)

# ---- Arrows: server broadcasts ----
arrow(4.9, 5.55, 2.1, 4.65+0.55, color=C_SERVER, lw=2.0, rad=-0.18,
      text="(2) broadcast same frame", tbg='white', tfontsize=9)
arrow(7.1, 5.55, 9.9, 4.65+0.55, color=C_SERVER, lw=2.0, rad=-0.18,
      text="(2) broadcast same frame", tbg='white', tfontsize=9)

# ---- Arrow: client computes (down) ----
arrow(2.1, 3.1, 2.1, 2.8, color=C_RESULT, lw=2.0,
      text="(3) compute", tbg='white', tfontsize=9, rad=0.0)
arrow(9.9, 3.1, 9.9, 2.8, color=C_RESULT, lw=2.0,
      text="(3) compute", tbg='white', tfontsize=9, rad=0.0)

# ---- Equality between results ----
eq_y = 2.05
ax.text(6.0, eq_y, "=", ha='center', va='center', fontsize=34,
        weight='bold', color='#2E7D32')
ax.text(6.0, eq_y - 0.55, "state_A == state_B",
        ha='center', va='center', fontsize=11, weight='bold', color='#2E7D32',
        bbox=dict(boxstyle='round,pad=0.35', fc='#E8F5E9', ec='#2E7D32', lw=1.4))

# ---- Bottom contract banner ----
banner = FancyBboxPatch((0.4, 0.12), 11.2, 0.62,
                       boxstyle="round,pad=0.04,rounding_size=0.12",
                       linewidth=1.4, edgecolor=C_EDGE, facecolor=C_BG)
ax.add_patch(banner)
ax.text(6.0, 0.43,
        "Contract:  same initial state  +  same input sequence  +  deterministic logic  ==>  identical state",
        ha='center', va='center', fontsize=10.5, weight='bold', color=C_EDGE)

plt.tight_layout()
plt.savefig(r'c:/Users/86133/Desktop/深入浅出系列/帧同步设计与实现/images/fig-01-lockstep-minimal.png',
            dpi=150, bbox_inches='tight', facecolor='white')
print("saved fig-01")
