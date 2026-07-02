import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch, Rectangle

C_VER   = '#E8743B'   # orange - version header
C_FRAME = '#2F6DB5'   # blue   - frame
C_RAND  = '#9C27B0'   # purple - random state
C_HASH  = '#C62828'   # red    - incremental hash
C_ENT   = '#4C9A2A'   # green  - entity management
C_POOL  = '#00897B'   # teal   - component pools
C_BG    = '#F5F7FA'
C_EDGE  = '#33415C'

fig, ax = plt.subplots(figsize=(13.5, 8.6))
ax.set_xlim(0, 13.5)
ax.set_ylim(0, 8.6)
ax.axis('off')
ax.set_facecolor('white')

def hseg(x, w, color, label_top, label_mid, sub=None, h=0.9, y=4.2):
    """a horizontal byte-stream segment."""
    r = Rectangle((x, y), w, h, facecolor=color, edgecolor=C_EDGE, linewidth=1.6, alpha=0.92)
    ax.add_patch(r)
    ax.text(x + w/2, y + h + 0.14, label_top, ha='center', va='bottom',
            fontsize=9.3, weight='bold', color=color)
    ax.text(x + w/2, y + h/2, label_mid, ha='center', va='center',
            fontsize=9, color='white', weight='bold')
    if sub:
        ax.text(x + w/2, y - 0.14, sub, ha='center', va='top',
                fontsize=8, color=C_EDGE, style='italic')

# ---- Title ----
ax.text(6.75, 8.25,
        'World.SaveState byte layout: three layers of ordering keep both peers bit-exact',
        ha='center', va='center', fontsize=12.5, weight='bold', color=C_EDGE)

# ---- the byte stream (single row of segments) ----
y_bar = 4.2
h_bar = 0.9
x = 0.4
# segment widths (relative, not to scale)
w_ver   = 1.7
w_ver2  = 1.5
w_frame = 1.2
w_rand  = 2.0
w_hash  = 1.5
w_ent   = 2.6
w_pool  = 2.6

xs = x
hseg(xs, w_ver, C_VER, "VersionMagic", '"LSEP"', sub="0x4C534550 (u32)"); xs += w_ver + 0.06
hseg(xs, w_ver2, C_VER, "SerialVersion", "= 2", sub="(i32)"); xs += w_ver2 + 0.06
hseg(xs, w_frame, C_FRAME, "CurrentFrame", "i32", sub="frame no."); xs += w_frame + 0.06
hseg(xs, w_rand, C_RAND, "Random.State", "s0 (u64)", sub="+ s1 (u64)"); xs += w_rand + 0.06
hseg(xs, w_hash, C_HASH, "IncrementalHash", "u32", sub="dual-track"); xs += w_hash + 0.06
hseg(xs, w_ent, C_ENT, "Entity mgmt", "nextId / gens[]", sub="active / recycled"); xs += w_ent + 0.06
hseg(xs, w_pool, C_POOL, "Component pools", "by type FullName", sub="Ordinal sort"); xs += w_pool

# arrow under bar showing direction
ax.annotate("", xy=(0.4, y_bar - 0.45), xytext=(xs - 0.2, y_bar - 0.45),
            arrowprops=dict(arrowstyle='-|>', color=C_EDGE, lw=1.4))
ax.text(xs/2, y_bar - 0.7, "byte stream written in this order   (LoadState reads it back exactly)",
        ha='center', va='center', fontsize=9, color=C_EDGE, style='italic')

# ---- bottom: the three ordering layers ----
y0 = 2.2
ax.text(6.75, 2.95,
        "Three ordering layers guarantee peer-to-peer byte identity:",
        ha='center', va='center', fontsize=10.5, weight='bold', color=C_EDGE)

def layer_card(x, y, w, color, num, title, body):
    r = FancyBboxPatch((x, y), w, 1.55,
                       boxstyle="round,pad=0.04,rounding_size=0.10",
                       linewidth=1.6, edgecolor=color, facecolor=C_BG)
    ax.add_patch(r)
    ax.text(x + 0.18, y + 1.32, num, fontsize=11, weight='bold', color=color)
    ax.text(x + w/2 + 0.2, y + 1.32, title, fontsize=10, weight='bold', color=color, ha='center')
    ax.text(x + w/2, y + 0.62, body, fontsize=8.7, color=C_EDGE, ha='center', va='center')

cardw = 4.0
gap = 0.3
layer_card(0.4, y0, cardw, C_POOL, "Layer 1",
           "Between pools",
           "component pools sorted by type\nFullName  +  StringComparison.Ordinal\n(avoids same-Name clash + culture skew)")
layer_card(0.4 + cardw + gap, y0, cardw, C_ENT, "Layer 2",
           "Entities within a pool",
           "each pool's entity list is a\nList<int> kept ascending by BinarySearch\n(removal = mark + defer, NOT swap-pop)")
layer_card(0.4 + 2*(cardw + gap), y0, cardw, C_HASH, "Layer 3",
           "Fields within a component",
           "[AutoSerialize] Source Generator\nwrites fields in declaration order\n(DEBUG round-trip verifies bit-exact)")

# ---- top: dual-version note ----
note = FancyBboxPatch((0.4, 7.0), 12.7, 0.0, linewidth=0)
ax.text(6.75, 7.45,
        "Note: SerializationVersion (=2, snapshot layout) is INDEPENDENT of ProtocolVersion (=1.1, wire/handshake).",
        ha='center', va='center', fontsize=9.2, color=C_VER, style='italic')

# ---- bottom-most banner ----
banner = FancyBboxPatch((0.4, 0.15), 12.7, 0.7,
                       boxstyle="round,pad=0.04,rounding_size=0.12",
                       linewidth=1.4, edgecolor=C_EDGE, facecolor=C_BG)
ax.add_patch(banner)
ax.text(6.75, 0.5,
        "Any layer using the wrong comparer (e.g. CurrentCulture instead of Ordinal) splits the two byte streams  ->  desync.",
        ha='center', va='center', fontsize=9.5, weight='bold', color=C_EDGE)

plt.tight_layout()
out = r'c:/Users/86133/Desktop/深入浅出系列/帧同步设计与实现/images/fig-05-savestate-byte-format.png'
plt.savefig(out, dpi=150, bbox_inches='tight', facecolor='white')
print("saved", out)
