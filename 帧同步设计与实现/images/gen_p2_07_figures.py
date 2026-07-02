"""
P2-07 figures: Byte-level serialization (BitWriter / sorted dict / FNV-1a / overflow).

Outputs (dpi 150, Agg backend, English labels):
  fig-07-01-dict-iteration-divergence.png  -- same Dictionary, two runtimes,
                                              different traversal order -> different
                                              byte stream -> false desync.
                                              Right column: WriteDictionarySorted fixes it.
  fig-07-02-write-dictionary-sorted.png    -- flow: rent ArrayPool -> copy keys ->
                                              Array.Sort (valid range only) ->
                                              write [count][k,v ...] -> return pool (zero GC).
  fig-07-03-integer-overflow-oom.png       -- attacker count = int.MaxValue,
                                              count*4 wraps to -4, boundary check
                                              bypassed, new T[count] -> OOM.
                                              Right: long pre-check blocks it.
  fig-07-04-fnv1a-hash.png                 -- FNV-1a: hash = offset_basis,
                                              per byte (XOR then *prime),
                                              avalanche demo.
"""
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, Rectangle, FancyArrowPatch

# ---- Series-consistent palette ----
C_BLUE   = '#2F6DB5'
C_ORANGE = '#E8743B'
C_GREEN  = '#2E7D32'
C_RED    = '#C62828'
C_BG     = '#F5F7FA'
C_EDGE   = '#33415C'
C_DIM    = '#6B7280'
C_PURPLE = '#7D3C98'
C_TEAL   = '#117A65'

OUT_DIR = r'c:/Users/86133/Desktop/深入浅出系列/帧同步设计与实现/images/'


def box(ax, x, y, w, h, text, fc='white', ec=C_EDGE, lw=1.4, fs=9.5,
        weight='normal', color=C_EDGE, rounded=0.06, family=None):
    p = FancyBboxPatch((x, y), w, h,
                       boxstyle=f"round,pad=0.02,rounding_size={rounded}",
                       linewidth=lw, edgecolor=ec, facecolor=fc, alpha=0.97, zorder=3)
    ax.add_patch(p)
    ax.text(x + w/2, y + h/2, text, ha='center', va='center',
            fontsize=fs, weight=weight, color=color, family=family, zorder=4)
    return p


def arrow(ax, p1, p2, color=C_EDGE, lw=1.8, ls='-', rad=0.0, mut=16):
    a = FancyArrowPatch(p1, p2, arrowstyle='-|>', mutation_scale=mut,
                        color=color, lw=lw, linestyle=ls, zorder=2,
                        connectionstyle=f"arc3,rad={rad}")
    ax.add_patch(a)
    return a


# ============================================================
# Figure 07-01: Dictionary iteration order -> false desync
# ============================================================
def fig_07_01():
    fig, ax = plt.subplots(figsize=(14.0, 8.6))
    ax.set_xlim(0, 14.0)
    ax.set_ylim(0, 8.6)
    ax.axis('off')
    ax.set_facecolor('white')

    # ---- Title ----
    ax.text(7.0, 8.30,
            "Dictionary Traversal Order: Why Naive foreach Causes False Desync",
            ha='center', va='center', fontsize=13.5, weight='bold', color=C_EDGE)
    ax.text(7.0, 7.92,
            "Same in-memory Dictionary on two runtimes -> different bucket layout -> "
            "different byte stream -> hash mismatch (memory is identical).",
            ha='center', va='center', fontsize=10.3, color=C_DIM, style='italic')

    # ---- LEFT: problem (naive foreach) ----
    ax.text(3.6, 7.42, "WITHOUT SORTING  (foreach over dict.Keys)",
            ha='center', va='center', fontsize=11, weight='bold', color=C_RED)

    # Client A bucket strip (bucket size 7)
    ax.text(0.55, 6.78, "Client A\n(runtime X,\nbuckets = 7)",
            ha='left', va='center', fontsize=9.2, weight='bold', color=C_EDGE)
    buckets_a = ['4', '1', '2', '3', '_', '_', '_']
    bx = 2.0
    for i, b in enumerate(buckets_a):
        fc = C_BLUE if b != '_' else '#E5E8EE'
        tc = 'white' if b != '_' else C_DIM
        box(ax, bx + i*0.62, 6.55, 0.56, 0.46, b, fc=fc, ec=C_EDGE, lw=1.0,
            fs=9.5, weight='bold', color=tc, rounded=0.04)
    ax.text(bx + 7*0.62 + 0.05, 6.78,
            "  traversal:  4 -> 1 -> 2 -> 3",
            ha='left', va='center', fontsize=9.0, color=C_EDGE, family='monospace')

    # Client B bucket strip (bucket size 5)
    ax.text(0.55, 5.85, "Client B\n(runtime Y,\nbuckets = 5)",
            ha='left', va='center', fontsize=9.2, weight='bold', color=C_EDGE)
    buckets_b = ['1', '4', '2', '3', '_']
    for i, b in enumerate(buckets_b):
        fc = C_ORANGE if b != '_' else '#E5E8EE'
        tc = 'white' if b != '_' else C_DIM
        box(ax, bx + i*0.62, 5.62, 0.56, 0.46, b, fc=fc, ec=C_EDGE, lw=1.0,
            fs=9.5, weight='bold', color=tc, rounded=0.04)
    ax.text(bx + 5*0.62 + 0.05, 5.85,
            "  traversal:  1 -> 4 -> 2 -> 3",
            ha='left', va='center', fontsize=9.0, color=C_EDGE, family='monospace')

    # byte streams
    ax.text(0.55, 4.95, "byte stream A:", ha='left', va='center',
            fontsize=9.3, weight='bold', color=C_BLUE, family='monospace')
    box(ax, 2.0, 4.72, 4.6, 0.42, "[4,A][1,B][2,C][3,D]",
        fc='#E8F0FB', ec=C_BLUE, fs=9.5, family='monospace')
    ax.text(0.55, 4.32, "byte stream B:", ha='left', va='center',
            fontsize=9.3, weight='bold', color=C_ORANGE, family='monospace')
    box(ax, 2.0, 4.09, 4.6, 0.42, "[1,B][4,A][2,C][3,D]",
        fc='#FDEEE6', ec=C_ORANGE, fs=9.5, family='monospace')

    # hash mismatch
    box(ax, 1.3, 3.05, 5.2, 0.78,
        "FNV-1a(A) != FNV-1a(B)\n-> SERVER SHOUTS 'DESYNC!'\n(memory is actually identical)",
        fc='#FDECEC', ec=C_RED, fs=9.8, weight='bold', color=C_RED)

    # memory-equal note
    box(ax, 0.55, 1.85, 6.7, 0.85,
        "In memory BOTH clients hold exactly\n  { 1:A, 2:B, 3:C, 4:D }\nThe bug is purely in serialization order.",
        fc='#FFFBE6', ec=C_EDGE, fs=9.2, color=C_EDGE)

    # ---- divider ----
    ax.plot([7.35, 7.35], [1.4, 7.7], color=C_EDGE, lw=0.8, ls=':', alpha=0.55)

    # ---- RIGHT: fix (WriteDictionarySorted) ----
    ax.text(10.75, 7.42, "WITH WriteDictionarySorted  (sort keys first)",
            ha='center', va='center', fontsize=11, weight='bold', color=C_GREEN)

    ax.text(8.05, 6.78, "Client A\n(foreach -> [3,1,4,2])",
            ha='left', va='center', fontsize=9.0, weight='bold', color=C_BLUE)
    ax.text(8.05, 6.30, "Client B\n(foreach -> [4,1,2,3])",
            ha='left', va='center', fontsize=9.0, weight='bold', color=C_ORANGE)

    # both converge into Array.Sort
    arrow(ax, (9.55, 6.78), (10.25, 6.55), color=C_BLUE, lw=1.5, rad=-0.18)
    arrow(ax, (9.55, 6.30), (10.25, 6.55), color=C_ORANGE, lw=1.5, rad=0.18)
    box(ax, 10.25, 6.32, 2.6, 0.55, "Array.Sort(keys)",
        fc=C_GREEN, ec=C_GREEN, fs=10, weight='bold', color='white')

    # sorted output
    box(ax, 9.85, 5.45, 3.4, 0.55, "canonical order: [1,2,3,4]",
        fc='#EAF6EE', ec=C_GREEN, fs=9.5, family='monospace', weight='bold',
        color=C_GREEN)
    arrow(ax, (11.55, 6.32), (11.55, 6.02), color=C_GREEN, lw=1.8)

    # identical byte streams
    ax.text(8.05, 4.78, "byte stream A:", ha='left', va='center',
            fontsize=9.3, weight='bold', color=C_BLUE, family='monospace')
    box(ax, 9.5, 4.56, 4.0, 0.42, "[1,B][2,C][3,D][4,A]",
        fc='#E8F0FB', ec=C_BLUE, fs=9.3, family='monospace')
    ax.text(8.05, 4.15, "byte stream B:", ha='left', va='center',
            fontsize=9.3, weight='bold', color=C_ORANGE, family='monospace')
    box(ax, 9.5, 3.93, 4.0, 0.42, "[1,B][2,C][3,D][4,A]",
        fc='#FDEEE6', ec=C_ORANGE, fs=9.3, family='monospace')

    # hash match
    box(ax, 9.6, 3.05, 3.8, 0.78,
        "FNV-1a(A) == FNV-1a(B)\n-> IN SYNC (correct)\nbyte-stream equal even when\nbucket layout differs",
        fc='#EAF6EE', ec=C_GREEN, fs=9.5, weight='bold', color=C_GREEN)

    # takeaway banner
    box(ax, 7.7, 1.85, 5.95, 0.85,
        "RULE: any Dictionary to be serialized MUST sort keys first.\n"
        "Traversal order depends on bucket layout, insert order, runtime version --\n"
        "never rely on it.",
        fc='#FFFBE6', ec=C_EDGE, fs=9.2, color=C_EDGE)

    plt.tight_layout()
    out = OUT_DIR + 'fig-07-01-dict-iteration-divergence.png'
    plt.savefig(out, dpi=150, bbox_inches='tight', facecolor='white')
    plt.close(fig)
    print("saved", out)


# ============================================================
# Figure 07-02: WriteDictionarySorted flow
# ============================================================
def fig_07_02():
    fig, ax = plt.subplots(figsize=(13.5, 7.8))
    ax.set_xlim(0, 13.5)
    ax.set_ylim(0, 7.8)
    ax.axis('off')
    ax.set_facecolor('white')

    ax.text(6.75, 7.50,
            "WriteDictionarySorted: Force Canonical Order with Zero GC",
            ha='center', va='center', fontsize=13.5, weight='bold', color=C_EDGE)
    ax.text(6.75, 7.13,
            "Rent a temp array from ArrayPool, sort the valid range only, write [count][k,v]*, "
            "return the array. No `new`, no GC pressure.",
            ha='center', va='center', fontsize=10.2, color=C_DIM, style='italic')

    # central vertical flow
    cx = 6.75
    steps = [
        # (y, title, detail, color, mono)
        (6.25, "1. Entry: WriteDictionarySorted(dict)",
         "if (dict == null) { WriteInt32(-1); return; }", C_BLUE, True),
        (5.30, "2. WriteInt32(dict.Count)",
         "length prefix written first (will not be overwritten)", C_BLUE, True),
        (4.35, "3. TKey[] keys = ArrayPool<TKey>.Shared.Rent(dict.Count)",
         "BORROW a temp array -- no `new T[]`, no GC. Pool may give an array\n"
         "LARGER than Count (power-of-2 bucketing); tail holds garbage.", C_ORANGE, False),
        (3.40, "4. foreach (key in dict.Keys) keys[count++] = key;",
         "copy keys in whatever order the runtime gives -- order does NOT matter,\n"
         "we sort next. This is the whole point.", C_ORANGE, False),
        (2.45, "5. Array.Sort(keys, 0, dict.Count)",
         "sort ONLY the valid range [0 .. dict.Count). Garbage tail left untouched.\n"
         "Result is canonical, independent of bucket layout.", C_GREEN, False),
        (1.50, "6. for i in [0, Count): write key, then dict[key].Serialize(this)",
         "key: int -> WriteInt32, string -> WriteString, else -> NotSupportedException\n"
         "(only int/string keys built in)", C_GREEN, False),
        (0.55, "7. finally { ArrayPool<TKey>.Shared.Return(keys); }",
         "RETURN the temp array. Zero allocation on the hot path.", C_PURPLE, True),
    ]

    for y, title, detail, color, mono in steps:
        box(ax, 1.2, y - 0.30, 11.1, 0.62, "",
            fc='white', ec=color, lw=1.6, rounded=0.06)
        # step title (left, bold colored)
        ax.text(1.45, y + 0.12, title, ha='left', va='center',
                fontsize=9.8, weight='bold', color=color,
                family='monospace' if mono else None)
        # detail (below title)
        ax.text(1.45, y - 0.13, detail, ha='left', va='center',
                fontsize=8.9, color=C_EDGE,
                family='monospace' if mono else None)

    # arrows between steps
    for i in range(len(steps) - 1):
        y_from = steps[i][0] - 0.30
        y_to   = steps[i+1][0] + 0.32
        arrow(ax, (cx, y_from), (cx, y_to), color=C_DIM, lw=1.4, mut=12)

    # side annotations
    # zero-GC badge on step 3
    box(ax, 0.20, 4.20, 0.95, 0.62, "ZERO\nGC", fc=C_GREEN, ec=C_GREEN,
        fs=8.8, weight='bold', color='white', rounded=0.10)
    # canonical badge on step 5
    box(ax, 0.20, 2.30, 0.95, 0.62, "CANON-\nICAL", fc=C_GREEN, ec=C_GREEN,
        fs=8.6, weight='bold', color='white', rounded=0.10)
    # P1-SEC-style note on step 7
    box(ax, 12.35, 0.40, 0.95, 0.62, "hot\npath", fc=C_PURPLE, ec=C_PURPLE,
        fs=8.8, weight='bold', color='white', rounded=0.10)

    plt.tight_layout()
    out = OUT_DIR + 'fig-07-02-write-dictionary-sorted.png'
    plt.savefig(out, dpi=150, bbox_inches='tight', facecolor='white')
    plt.close(fig)
    print("saved", out)


# ============================================================
# Figure 07-03: integer overflow wrap-around -> OOM DoS
# ============================================================
def fig_07_03():
    fig, ax = plt.subplots(figsize=(14.0, 8.8))
    ax.set_xlim(0, 14.0)
    ax.set_ylim(0, 8.8)
    ax.axis('off')
    ax.set_facecolor('white')

    ax.text(7.0, 8.50,
            "Integer Overflow Wrap-Around: A 4-byte Packet That Crashes the Process",
            ha='center', va='center', fontsize=13.5, weight='bold', color=C_EDGE)
    ax.text(7.0, 8.13,
            "Naive: count*4 wraps, boundary check bypassed, new T[count] -> OOM.   "
            "Fix: long pre-check before any allocation.",
            ha='center', va='center', fontsize=10.2, color=C_DIM, style='italic')

    # =================== LEFT: naive (vulnerable) ===================
    ax.text(3.6, 7.55, "NAIVE  (vulnerable to OOM DoS)",
            ha='center', va='center', fontsize=11.5, weight='bold', color=C_RED)

    # attacker packet
    box(ax, 0.55, 6.70, 6.1, 0.65,
        "attacker sends a 4-byte packet:   count = int.MaxValue = 2147483647",
        fc='#FDECEC', ec=C_RED, fs=9.4, color=C_RED, family='monospace')

    # step 1: read count
    box(ax, 0.55, 5.78, 6.1, 0.62,
        "int count = ReadInt32();        // count = 2147483647",
        fc='white', ec=C_EDGE, fs=9.3, family='monospace')

    # step 2: negative check passes
    box(ax, 0.55, 4.92, 6.1, 0.62,
        "if (count < 0) throw;          // 2147483647 > 0  ->  check passes",
        fc='white', ec=C_EDGE, fs=9.3, family='monospace')

    # step 3: WRAP (the killer) -- arithmetic breakdown
    box(ax, 0.55, 3.55, 6.1, 1.18,
        "int totalBytes = count * 4;    //  <-- WRAP-AROUND\n"
        "  0x7FFFFFFF * 4 = 0x1FFFFFFFC\n"
        "  int keeps low 32 bits  ->  0xFFFFFFFC = -4",
        fc='#FDECEC', ec=C_RED, fs=9.2, color=C_RED, family='monospace', weight='bold')

    # step 4: boundary check bypassed
    box(ax, 0.55, 2.55, 6.1, 0.78,
        "if (_position + totalBytes > Length) throw;\n"
        "  // _position + (-4)  <  Length   ->  BYPASSED",
        fc='#FDECEC', ec=C_RED, fs=9.2, color=C_RED, family='monospace', weight='bold')

    # step 5: OOM
    box(ax, 0.55, 1.30, 6.1, 1.02,
        "new int[count];   //  new int[2147483647]\n"
        "  -> needs 8 GB contiguous  ->  OOM, process dies",
        fc=C_RED, ec=C_RED, fs=10.0, color='white', family='monospace', weight='bold')

    box(ax, 0.55, 0.40, 6.1, 0.65,
        "ATTACK COST: 4 bytes    DAMAGE: process crash (DoS)\n"
        "no login, no privilege required -- remotely triggerable",
        fc='#FFFBE6', ec=C_EDGE, fs=8.8, color=C_EDGE)

    # =================== divider ===================
    ax.plot([7.05, 7.05], [0.30, 7.85], color=C_EDGE, lw=0.8, ls=':', alpha=0.55)

    # =================== RIGHT: BitReader fix ===================
    ax.text(10.55, 7.55, "BitReader FIX  (long pre-check)",
            ha='center', va='center', fontsize=11.5, weight='bold', color=C_GREEN)

    box(ax, 7.45, 6.70, 6.05, 0.65,
        "same attacker packet:   count = 2147483647",
        fc='#EAF6EE', ec=C_GREEN, fs=9.4, color=C_GREEN, family='monospace')

    box(ax, 7.45, 5.78, 6.05, 0.62,
        "int count = ReadInt32();",
        fc='white', ec=C_EDGE, fs=9.3, family='monospace')

    box(ax, 7.45, 4.92, 6.05, 0.62,
        "ValidateCollectionCount(count, ...);   // -1=null, neg=fail-fast",
        fc='white', ec=C_EDGE, fs=9.3, family='monospace')

    # the long pre-check (key box)
    box(ax, 7.45, 3.55, 6.05, 1.18,
        "int remaining = _buffer.Length - _position;\n"
        "if ( (long)count > (long)remaining / 4 )     //  <-- long, no wrap\n"
        "        ThrowBufferOverflow(...);            //  cast avoids count*4",
        fc='#EAF6EE', ec=C_GREEN, fs=9.2, color=C_GREEN, family='monospace', weight='bold')

    # arithmetic showing why it works
    box(ax, 7.45, 2.55, 6.05, 0.78,
        "e.g. remaining = 1024  ->  remaining/4 = 256\n"
        "  (long)2147483647 > 256   ->  TRUE  ->  REJECTED before any alloc",
        fc='white', ec=C_GREEN, fs=9.0, color=C_EDGE, family='monospace')

    # safe
    box(ax, 7.45, 1.30, 6.05, 1.02,
        "new int[count] never reached for malicious count\n"
        "  ->  no allocation  ->  no OOM",
        fc=C_GREEN, ec=C_GREEN, fs=10.0, color='white', family='monospace', weight='bold')

    box(ax, 7.45, 0.40, 6.05, 0.65,
        "Pattern repeats across ALL read-array methods:\n"
        "ReadIntArray(/4) ReadLongArray(/8) ReadLFloatArray(/8) "
        "ReadLVector2Array(/16) ReadList(/1)",
        fc='#FFFBE6', ec=C_EDGE, fs=8.5, color=C_EDGE)

    # big arrow between panels labelled "fix"
    arrow(ax, (6.95, 4.14), (7.55, 4.14), color=C_PURPLE, lw=2.2, mut=20)
    ax.text(7.25, 4.55, "fix", ha='center', va='center',
            fontsize=10, weight='bold', color=C_PURPLE)

    plt.tight_layout()
    out = OUT_DIR + 'fig-07-03-integer-overflow-oom.png'
    plt.savefig(out, dpi=150, bbox_inches='tight', facecolor='white')
    plt.close(fig)
    print("saved", out)


# ============================================================
# Figure 07-04: FNV-1a hash
# ============================================================
def fig_07_04():
    fig, ax = plt.subplots(figsize=(13.8, 7.6))
    ax.set_xlim(0, 13.8)
    ax.set_ylim(0, 7.6)
    ax.axis('off')
    ax.set_facecolor('white')

    ax.text(6.9, 7.30,
            "FNV-1a: Per-byte XOR-then-Multiply State Hash",
            ha='center', va='center', fontsize=13.5, weight='bold', color=C_EDGE)
    ax.text(6.9, 6.93,
            "Deterministic, allocation-free, fast, good avalanche. Two magic constants "
            "are fixed by the FNV spec, so every platform agrees.",
            ha='center', va='center', fontsize=10.2, color=C_DIM, style='italic')

    # ---- LEFT: the algorithm pipeline ----
    ax.text(3.5, 6.40, "Algorithm  (BitWriter.ComputeHash)",
            ha='center', va='center', fontsize=11.5, weight='bold', color=C_EDGE)

    # init box
    box(ax, 1.0, 5.55, 5.0, 0.62,
        "ulong hash = 14695981039346656037;   // offset basis  0xcbf29ce484222325",
        fc=C_BLUE, ec=C_BLUE, fs=9.0, color='white', family='monospace', weight='bold')

    # loop label
    box(ax, 1.0, 4.70, 5.0, 0.45, "for each byte b in span:",
        fc=C_BG, ec=C_EDGE, fs=9.5, family='monospace', weight='bold', color=C_EDGE)

    # XOR step
    box(ax, 1.0, 3.85, 5.0, 0.62,
        "hash ^= b;        //  XOR the byte into the low 8 bits",
        fc='#E8F0FB', ec=C_BLUE, fs=9.3, family='monospace')
    # multiply step
    box(ax, 1.0, 3.00, 5.0, 0.62,
        "hash *= 1099511628211;   //  multiply by FNV prime  0x100000001b3",
        fc='#E8F0FB', ec=C_BLUE, fs=9.3, family='monospace')

    # return box
    box(ax, 1.0, 2.10, 5.0, 0.62,
        "return hash;      //  64-bit fingerprint",
        fc=C_GREEN, ec=C_GREEN, fs=9.5, color='white', family='monospace', weight='bold')

    # arrows down the pipeline
    for y1, y2 in [(5.55, 5.15), (4.70, 4.47), (3.85, 3.62), (3.00, 2.72)]:
        arrow(ax, (3.5, y1), (3.5, y2), color=C_DIM, lw=1.5, mut=12)

    # why-FNV box
    box(ax, 1.0, 0.95, 5.0, 0.95,
        "WHY FNV-1a (not MD5/SHA):\n"
        "  - 2 ops/byte, no tables, no rounds  ->  fast\n"
        "  - XOR & int mul are cross-platform exact  ->  deterministic\n"
        "  - iterates Span in place  ->  zero allocation",
        fc='#FFFBE6', ec=C_EDGE, fs=9.0, color=C_EDGE)

    # ---- divider ----
    ax.plot([6.65, 6.65], [0.7, 6.7], color=C_EDGE, lw=0.8, ls=':', alpha=0.55)

    # ---- RIGHT: avalanche demo (one byte change -> wildly different hash) ----
    ax.text(10.2, 6.40, "Avalanche: One Byte Change, Hash Completely Different",
            ha='center', va='center', fontsize=11.0, weight='bold', color=C_EDGE)

    # stream A
    box(ax, 7.5, 5.62, 5.4, 0.50, "stream A:   ... 0x42 0x07 0x1F ...",
        fc='#E8F0FB', ec=C_BLUE, fs=9.5, family='monospace', color=C_BLUE)
    # stream B (one byte differs)
    box(ax, 7.5, 4.95, 5.4, 0.50, "stream B:   ... 0x42 0x08 0x1F ...   (1 byte changed)",
        fc='#FDEEE6', ec=C_ORANGE, fs=9.5, family='monospace', color=C_ORANGE)

    # arrows into the hash pipe
    arrow(ax, (10.2, 5.62), (10.2, 4.55), color=C_BLUE, lw=1.5)
    arrow(ax, (10.2, 4.95), (10.2, 4.55), color=C_ORANGE, lw=1.5)

    # FNV-1a pipe
    box(ax, 7.5, 4.05, 5.4, 0.50, "FNV-1a",
        fc=C_PURPLE, ec=C_PURPLE, fs=10.5, weight='bold', color='white')

    # two diverging hash outputs
    # fake-but-illustrative 64-bit hex values
    box(ax, 7.2, 3.05, 2.85, 0.62,
        "hash A:\n0x8A3F...C1D2",
        fc='#E8F0FB', ec=C_BLUE, fs=9.2, family='monospace', weight='bold', color=C_BLUE)
    box(ax, 10.35, 3.05, 2.85, 0.62,
        "hash B:\n0x5E71...04B9",
        fc='#FDEEE6', ec=C_ORANGE, fs=9.2, family='monospace', weight='bold', color=C_ORANGE)

    arrow(ax, (9.0, 4.05), (8.6, 3.67), color=C_BLUE, lw=1.5, rad=0.15)
    arrow(ax, (11.4, 4.05), (11.8, 3.67), color=C_ORANGE, lw=1.5, rad=-0.15)

    # mismatch banner
    box(ax, 7.5, 2.10, 5.4, 0.78,
        "hash A != hash B   ->  desync detected at the exact divergence frame",
        fc='#FDECEC', ec=C_RED, fs=9.8, weight='bold', color=C_RED)

    # connect to ch.23
    box(ax, 7.5, 0.95, 5.4, 0.95,
        "This is the BASELINE rail of the dual-hash scheme (Ch. 23):\n"
        "  - full recompute hash  (this chapter, FNV-1a over the whole byte stream)\n"
        "  - incremental hash     (per-change XOR, O(1)) -- validated by this rail",
        fc='#FFFBE6', ec=C_EDGE, fs=9.0, color=C_EDGE)

    plt.tight_layout()
    out = OUT_DIR + 'fig-07-04-fnv1a-hash.png'
    plt.savefig(out, dpi=150, bbox_inches='tight', facecolor='white')
    plt.close(fig)
    print("saved", out)


if __name__ == '__main__':
    fig_07_01()
    fig_07_02()
    fig_07_03()
    fig_07_04()
    print("all P2-07 figures done")
