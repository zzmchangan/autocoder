"""fig-dir-02-paths.png
Decision tree: five reader profiles -> five reading paths through the book.
Root = reader profile; first split = "your lockstep experience?";
five leaves = path cards (skip chapters / focus chapters / goal).
"""
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch

C_ROOT   = '#33415C'
C_Q      = '#475569'
C_PATH   = '#D97706'   # amber - matches mermaid path classDef
C_KERNEL = '#1F6FB2'
C_BRIDGE = '#2E8B57'
C_SYNC   = '#C0563E'
C_CROSS  = '#7C3AED'
C_TXT    = '#1B2733'
C_PAPER  = '#F7F9FC'

fig, ax = plt.subplots(figsize=(14.5, 8.6))
ax.set_xlim(0, 14.5)
ax.set_ylim(0, 8.6)
ax.axis('off')
fig.patch.set_facecolor('white')

# title
ax.text(7.25, 8.28,
        'Reading Paths for Different Readers',
        ha='center', va='center', fontsize=13.5, weight='bold', color=C_ROOT)
ax.text(7.25, 7.98,
        'root = reader profile;  first split = your lockstep experience;  five leaves = what to skip / focus on',
        ha='center', va='center', fontsize=9.6, style='italic', color='#5A6B7B')

# ---- root node (top-left) ----
def box(cx, cy, w, h, text, fc, ec, fs=9.5, weight='bold', tc='white', round=0.12):
    ax.add_patch(FancyBboxPatch((cx - w / 2, cy - h / 2), w, h,
                 boxstyle=f"round,pad=0.02,rounding_size={round}",
                 linewidth=1.4, edgecolor=ec, facecolor=fc, alpha=1.0))
    ax.text(cx, cy, text, ha='center', va='center',
            fontsize=fs, weight=weight, color=tc)

root_cx, root_cy = 1.55, 6.7
box(root_cx, root_cy, 2.5, 0.95, "READER\nPROFILE",
    fc=C_ROOT, ec=C_ROOT, fs=11)

# decision diamond (question)
q_cx, q_cy = 4.5, 6.7
ax.add_patch(plt.Polygon([(q_cx - 1.15, q_cy), (q_cx, q_cy + 0.55),
                          (q_cx + 1.15, q_cy), (q_cx, q_cy - 0.55)],
          closed=True, facecolor='#EEF2F7', edgecolor=C_Q, linewidth=1.5))
ax.text(q_cx, q_cy, "lockstep\nexperience?", ha='center', va='center',
        fontsize=9.2, weight='bold', color=C_Q)

# root -> question
ax.add_patch(FancyArrowPatch((root_cx + 1.25, root_cy), (q_cx - 1.15, q_cy),
             arrowstyle='-|>', mutation_scale=12, linewidth=1.4, color=C_Q))

# ---- five path leaves ----
# (label, branch_label, accent, skip_text, focus_text, goal_text)
paths = [
    ("PATH 1",
     "zero experience",
     C_KERNEL,
     "skip nothing - read all 27 in order",
     "P1-02/03 fixed-point\nP3-09 rollback (essence)",
     "full mastery"),
    ("PATH 2",
     "network\nbackground",
     C_SYNC,
     "P4-17 transport (TCP/UDP/WS basics)\nP4-16 handshake version only",
     "P1-03 cross-TFM desync\nP3-08..11 predict/rollback\nP4-13 clock Jacobson+bounds\nP6-23/24/25 desync hunting",
     "lockstep-specific\nmechanisms"),
    ("PATH 3",
     "game\ndeveloper",
     C_BRIDGE,
     "P0-01 (you know it)\nP7-26 collisions (if read)",
     "P1-02 why no float\nP2-05 ordered ECS\nP2-06 no swap-pop delete\nP3-09 7 rollback disciplines\nP3-11 visual offset\nP4-14 relay vs authoritative",
     "disciplines lockstep\nforces on game code"),
    ("PATH 4",
     "desync to\nhunt now",
     C_CROSS,
     "everything not below",
     "P6-23 hash dual-track\nP6-24 red-line checklist\nP6-25 11 bug cases\nAppendix B toolchain",
     "locate the desync\nfastest"),
    ("PATH 5",
     "evaluating /\nchoosing",
     '#0E7490',
     "deep implementation\ndetails",
     "P0-01 state-sync comparison\nP5-18 SDK positioning\nP5-22 perf ceiling\nP4-14 server mode choice",
     "decide: lockstep?\nwhich plan?"),
]

leaf_top = 6.7
leaf_y_positions = [6.6, 5.25, 3.90, 2.55, 1.20]  # vertical spread for 5 leaves on right
# distribute the 5 leaves stacked vertically on the right side
lx = 10.6
card_w = 6.4
card_h = 1.20

# We'll stack them vertically; pull lines from the decision diamond.
leaf_cys = [6.55, 5.30, 4.05, 2.80, 1.55]

for (name, branch, accent, skip_t, focus_t, goal_t), lcy in zip(paths, leaf_cys):
    # branch label chip just right of the diamond path start
    # card
    ax.add_patch(FancyBboxPatch((lx - card_w / 2, lcy - card_h / 2), card_w, card_h,
                 boxstyle="round,pad=0.02,rounding_size=0.10",
                 linewidth=1.5, edgecolor=accent, facecolor='#FFFFFF', alpha=1.0))
    # header strip
    hh = 0.34
    ax.add_patch(FancyBboxPatch((lx - card_w / 2, lcy + card_h / 2 - hh), card_w, hh,
                 boxstyle="round,pad=0.02,rounding_size=0.10",
                 linewidth=0, facecolor=accent))
    ax.text(lx - card_w / 2 + 0.18, lcy + card_h / 2 - hh / 2, name,
            ha='left', va='center', fontsize=9.6, weight='bold', color='white')
    ax.text(lx + card_w / 2 - 0.18, lcy + card_h / 2 - hh / 2, branch,
            ha='right', va='center', fontsize=8.2, style='italic', color='white')

    # three columns: skip / focus / goal
    col_w = (card_w - 0.5) / 3
    base_y = lcy - 0.05
    cols = [("SKIP", skip_t, '#6B7280'),
            ("FOCUS", focus_t, accent),
            ("GOAL", goal_t, '#1B2733')]
    for j, (head, body, col) in enumerate(cols):
        cx = lx - card_w / 2 + 0.25 + col_w * (j + 0.5)
        ax.text(cx, base_y + 0.20, head, ha='center', va='center',
                fontsize=7.6, weight='bold', color=col)
        ax.text(cx, base_y - 0.16, body, ha='center', va='center',
                fontsize=7.0, color=C_TXT, linespacing=1.25)

    # connector from diamond to card
    src = (q_cx + 1.0, q_cy - 0.1) if lcy > q_cy else (q_cx + 1.0, q_cy + 0.1)
    # route: out the right of diamond, then horizontal to card left edge
    midx = (q_cx + 1.0 + (lx - card_w / 2)) / 2
    arr = FancyArrowPatch((q_cx + 1.0, q_cy),
                          (lx - card_w / 2, lcy),
                          arrowstyle='-|>', mutation_scale=11,
                          linewidth=1.3, color=accent,
                          connectionstyle="arc3,rad=0.0",
                          shrinkA=2, shrinkB=2)
    ax.add_patch(arr)

# ---- footer reminder ----
ax.text(7.25, 0.45,
        'Reminder on every path:  P1 fixed-point (Ch.2-3)  and  P3-09 rollback  are the keys to the whole book.',
        ha='center', va='center', fontsize=9.0, style='italic', color='#B91C1C')

plt.tight_layout()
plt.savefig('fig-dir-02-paths.png', dpi=150, bbox_inches='tight',
            facecolor='white')
print("saved fig-dir-02-paths.png")
