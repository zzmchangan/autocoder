# 第十七章 · LRU + workingset + vmscan

> 篇:第 5 篇 · 回收与规整:紧张时收回来
> 主线呼应:上一章(P5-16)立起了回收链的第一环——**何时回收**(watermark 触发,kswapd 在 low~high 工作区里干活)。本章是第二环——**回收谁**。内存里塞着成百万上千万个页,回收时不能"随机抽一页扔出去":扔了热页马上又缺页(thrashing),扔了没回写的脏页丢数据。内核需要一套**页的"价值"判断**:哪些页值得留在内存、哪些可以扔。本章拆的就是这套判断——LRU 双链表近似"最近最少使用"、workingset 用 refault 估算真热页、vmscan 的扫描决策循环。你会看到,这是 mm 里**启发式(heuristic)设计**最密集的地方:每个判断都不完美,但组合起来在绝大多数负载下都"差不多对"。

## 核心问题

**kswapd(或 direct reclaim)决定要回收了,但"回收哪些页"?内核怎么用有限的元数据近似估算一个页的"活跃度/价值"?vmscan 的核心回收循环 `shrink_lruvec` 怎么选页、怎么决定扫 anon 还是 file、扫多少?被踢出去的页怎么用 workingset 的 shadow entry 记"价值指纹",refault 时估算它是不是真在 working set 里?**

读完本章你会明白:

1. **LRU 双链表** active/inactive × anon/file 的四(加 unevictable 五)条链结构,为什么这么分。
2. **第二机会算法**(`PG_active`/`PG_referenced` + PTE accessed 位)如何用 1~2 位状态近似 LRU,避免时间戳开销。
3. **workingset 用 shadow entry** 记 refault 距离,估算页是否在 working set 内;为什么纯 LRU 会被 streaming IO 冲刷,workingset 怎么补救。
4. **`shrink_lruvec`/`get_scan_count`** 的扫描决策:swappiness 怎么平衡 anon/file 扫描比例,priority 怎么控制扫描量。
5. **6.x 默认的 MGLRU**(多代 LRU)——把 active/inactive 扩成多代,更精确识别冷热。

> **逃生阀**:本章默认你读过上一章(P5-16,知道 kswapd 何时唤醒、balance_pgdat 调 shrink_node)和第 15 章(P4-15 rmap,知道回收要解除 PTE 映射、为什么 rmap 是回收的地基)。如果你对"页有匿名(anon)和文件(file)之分"还模糊,回第 14 章 14.5/14.6 看一眼 `do_anonymous_page`/`do_fault` 的分流——本章的 anon/file LRU 分流就是从这里来的。

---

## 17.1 一句话点破

> **回收不是随机扔,而是按"价值"扔——价值低的先扔。Linux 用 LRU 双链表(active/inactive)近似"最近最少使用",用 1~2 位状态(PG_active/PG_referenced)+ PTE accessed 位实现"第二机会",避免精确时间戳的开销;用 workingset 的 shadow entry 在被踢出的页位置记一个"内存压力指纹",refault 时算 refault 距离,识别出"反复访问的真热页"vs"一次性流过的冷页"。vmscan 的 `shrink_lruvec` 按这套价值判断扫描、回收。6.x 默认的 MGLRU 把双链扩成多代,让冷热识别更准。**

这是结论,不是理由。本章倒过来拆:先看朴素 LRU 为什么不行,引出第二机会算法;再看双链 + 四链结构;然后拆 vmscan 的扫描决策;接着 workingset 的价值估算;最后 MGLRU 这个 6.x 的新主力。

---

## 17.2 撞墙:朴素 LRU 的两道墙

### 朴素 LRU 会怎样

"回收哪些页"的最朴素答案:**最近最少使用的(Least Recently Used)**。直觉上,最久没被访问的页,接下来被访问的概率也最低,扔它最划算。这就是经典的 **LRU 算法**。

朴素 LRU 的实现:**给每个页记一个"最后访问时间戳",每次访问就更新;回收时挑时间戳最旧(最久没访问)的扔**。听起来对,但撞上两道墙:

**墙一:元数据与更新开销爆炸**。一台 16GB 机器有 4 百万页。给每页记一个 8 字节时间戳 = 32MB 元数据(还好);但**每次访问都要更新时间戳**——用户态读写、缺页、内核访问……每秒几十亿次访问,每次都要"改时间戳"。这要么走 PTE accessed 位(每次访问硬件自动置位,但只在页表项这一层)、要么走软件更新(太慢)。而且"挑时间戳最旧的扔"要排序或堆,O(log n) 每页——海量页 × 频繁操作 = 元数据和 CPU 开销爆炸。

**墙二:streaming IO 冲刷**。考虑这个场景:数据库在跑(workset 几 GB 热页),同时有个 `dd if=/dev/sda of=/dev/null bs=1M`(或 `grep -r` 一个大目录)在顺序扫一遍磁盘。`grep` 读进的文件页会塞满 page cache——如果按朴素 LRU,这些"刚读进来"的页时间戳最新,**反而把数据库的热页挤到最旧、被回收**。结果 `grep` 跑完,数据库再访问自己的热页,全部缺页——这就是**一次 streaming IO 把 working set 冲刷掉的灾难**。朴素 LRU 把"新"等同于"热",但 streaming 的页是"新但只读一次",根本不热。

> **不这样会怎样**:朴素 LRU 在 streaming 负载下会出现"**LRU 颠簸**"——大文件读一遍,真热页全被换出,之后访问全缺页。数据库、文件服务器、备份系统都深受其害。这就是为什么 Linux mm 不能直接用朴素 LRU,而要用"近似 + 第二机会 + workingset 价值估算"这套组合拳。

### Linux 的解法:三招组合

Linux mm 的回收价值判断由三招组合:

1. **第二机会(active/inactive 双链 + PG_active/PG_referenced 两位)**:用"两条链 + 两个状态位"近似 LRU,不记时间戳。代价是精度差(只有"活跃/不活跃"两档),但开销 O(1)(链表摘挂 + 位操作)。
2. **workingset refault 距离**:被踢出的页在原位置留一个 shadow entry(记"内存压力指纹"),refault 时算"refault 距离"——距离小说明"刚踢出去就又访问了"(真热页),立刻激活;距离大说明"踢出去很久才访问"(冷页或 streaming),保持冷。**这一招专门补救 streaming IO 冲刷**。
3. **anon/file 分链**:匿名页(anon)换出要写 swap(慢 IO),文件页(file)干净可以直接丢(快)。把它们分到不同 LRU,用 swappiness 平衡扫描比例——避免在 swap 紧张时还大量扫 anon、或反之。

后面四节拆这三招。

---

## 17.3 LRU 双链:active/inactive × anon/file 的四链结构

### 为什么是"两条链"而不是"时间戳排序"

第一招:**用两条链近似 LRU**。每个 lruvec(LRU vector,每个 zone+memcg 一个)维护多条 LRU 链表。最基本的两条是 **active**(活跃,推测为热)和 **inactive**(不活跃,推测为冷)。回收时**只从 inactive 扫**——inactive 链尾的页是"最久没被翻到 active 的",最该被回收。

[`struct lruvec`](../linux/include/linux/mmzone.h#L606)([mmzone.h:606](../linux/include/linux/mmzone.h#L606))里:

```c
// include/linux/mmzone.h#L606-L635 (简化)
struct lruvec {
    struct list_head    lists[NR_LRU_LISTS];   /* 多条 LRU 链 */
    spinlock_t          lru_lock;              /* 保护本 lruvec 的 LRU 链 */
    unsigned long       anon_cost;             /* anon 回收代价(用于扫描平衡) */
    unsigned long       file_cost;             /* file 回收代价 */
    atomic_long_t       nonresident_age;       /* workingset 用的非驻留页年龄 */
    unsigned long       refaults[ANON_AND_FILE];
    ...
};
```

[`enum lru_list`](../linux/include/linux/mmzone.h#L273)([mmzone.h:273](../linux/mmzone.h#L273))定义了所有 LRU 链:

```c
// include/linux/mmzone.h#L269-L280
#define LRU_BASE 0
#define LRU_ACTIVE 1   /* active 位 */
#define LRU_FILE 2     /* file 位 */

enum lru_list {
    LRU_INACTIVE_ANON = LRU_BASE,                       /* 0:anon 不活跃 */
    LRU_ACTIVE_ANON   = LRU_BASE + LRU_ACTIVE,          /* 1:anon 活跃 */
    LRU_INACTIVE_FILE = LRU_BASE + LRU_FILE,            /* 2:file 不活跃 */
    LRU_ACTIVE_FILE   = LRU_BASE + LRU_FILE + LRU_ACTIVE, /* 3:file 活跃 */
    LRU_UNEVICTABLE,                                    /* 4:不可回收(mlock 等)*/
    NR_LRU_LISTS
};
```

注意这个枚举的位编码很巧妙:**用 `LRU_ACTIVE`(位 0)和 `LRU_FILE`(位 1)两个 bit 编码四条链**。所以 `is_file_lru(lru) = (lru & LRU_FILE)`、`is_active_lru(lru) = (lru & LRU_ACTIVE)`——一次位与就能判断,免去查表。这是内核"用位编码多维分类"的典型技巧(第 1 章 struct page 的位段也是同源)。

四条可回收链画出来:

```
              anon(匿名页)             file(文件页)
           ┌──────────────────┐    ┌──────────────────┐
  active   │ LRU_ACTIVE_ANON  │    │ LRU_ACTIVE_FILE  │  ← 推测为"热"
           │ 推测热的匿名页     │    │ 推测热的文件页    │
           └──────────────────┘    └──────────────────┘
                    ▲                         ▲
       第二机会     │ 升 active               │  (访问并被识别为热)
       (referenced  │                         │
        两次)       │                         │
                    ▼                         ▼
           ┌──────────────────┐    ┌──────────────────┐
 inactive │ LRU_INACTIVE_ANON│    │ LRU_INACTIVE_FILE│  ← 推测为"冷",回收扫这里
           │ 推测冷的匿名页     │    │ 推测冷的文件页    │
           └──────────────────┘    └──────────────────┘
                    │                         │
                    ▼                         ▼
              vmscan 从这里扫         vmscan 从这里扫
              (shrink_inactive_list)  扫链尾→踢出去/换 swap/丢弃
```

加上 `LRU_UNEVICTABLE`(给 `mlock` 钉死、Shared 内存等不可回收页),一共五条链(`NR_LRU_LISTS = 5`)。`for_each_evictable_lru` 宏遍历前四条(可回收的),`LRU_UNEVICTABLE` 不参与扫描。

> **不这样会怎样**:如果只用一条 LRU 链(所有页混在一起按时间近似排序),那 anon(换出贵)和 file(丢弃便宜)混排,回收时分不清——可能为了省内存扫到一堆 anon,结果 swap IO 巨大;也可能扫到一堆 file 但都是脏页,得 writeback。分链让回收按"类别"分别扫描,各扫各的,平衡 IO 成本。**anon/file 分链**是 mm 里"按代价分类决策"的典型。

### 锁:`lruvec->lru_lock`

所有 LRU 链的修改(摘挂、移动)都由 [`lruvec->lru_lock`](../linux/include/linux/mmzone.h#L609) 这一把自旋锁保护。这是个 per-lruvec(per-zone-per-memcg)细粒度锁——不是一把全局锁锁所有 LRU,而是每个 lruvec 一把锁。这让多 memcg、多 zone 的系统能并行回收(各锁各的)。

锁的粒度演变值得注意:早期内核(2.x)是 zone 级别的 `zone->lru_lock`,后来引入 memcg 后改成 `lruvec->lru_lock`(per memcg+zone)。这是 mm 里"锁细化以提并行度"的演进——和第 4 章的 per-cpu pageset、第 8 章的 per-cpu partial slab 是同一种思路:热路径无锁、慢路径细锁。

### 加入 LRU:`folio_add_lru`

一个新页(比如缺页分配的匿名页、文件读入的 page cache)怎么进入 LRU?通过 [`folio_add_lru`](../linux/mm/swap.c#L516)([swap.c:516](../linux/mm/swap.c#L516)):

```c
// mm/swap.c#L516-L535 (简化)
void folio_add_lru(struct folio *folio)
{
    struct folio_batch *fbatch;

    /* 见 lru_gen_add_folio() 的注释:MGLRU + 在缺页上下文 + 不是 PF_MEMALLOC → 默认 active */
    if (lru_gen_enabled() && !folio_test_unevictable(folio) &&
        lru_gen_in_fault() && !(current->flags & PF_MEMALLOC))
        folio_set_active(folio);

    folio_get(folio);
    local_lock(&cpu_fbatches.lock);
    fbatch = this_cpu_ptr(&cpu_fbatches.lru_add);
    folio_batch_add_and_move(fbatch, folio, lru_add_fn);
    local_unlock(&cpu_fbatches.lock);
}
```

注意两件事:

1. **per-cpu 批量缓存**:`folio_add_lru` 不是直接挂到 LRU 链(那要拿 lru_lock),而是先塞进 per-cpu 的 `folio_batch`(类似 buddy 的 per-cpu pageset)。批量满了或定时 drain,一次性把整批挂到 LRU——拿一次锁处理多个页,**减少锁竞争**。这是 mm 里 per-cpu 缓存的又一次应用。
2. **缺页路径默认 active**(MGLRU):新页如果在缺页上下文分配(`lru_gen_in_fault`),默认设 `PG_active`——直觉是"刚被访问的页是热的"。老内核(非 MGLRU)默认进 inactive,要第二次访问才升 active。

### 第二机会:`folio_mark_accessed`

页被访问时(用户态读写触发 PTE accessed 位、或内核显式标记),通过 [`folio_mark_accessed`](../linux/mm/swap.c#L473)([swap.c:473](../linux/mm/swap.c#L473))反馈给 LRU:

```c
// mm/swap.c#L473-L505 (简化)
void folio_mark_accessed(struct folio *folio)
{
    if (lru_gen_enabled()) {
        folio_inc_refs(folio);          /* MGLRU:增加 refs 计数 */
        return;
    }

    if (!folio_test_referenced(folio)) {
        /* 第一次访问:置 PG_referenced,留在 inactive */
        folio_set_referenced(folio);
    } else if (!folio_test_active(folio)) {
        /* 第二次访问(已 referenced):升 active! */
        if (folio_test_lru(folio))
            folio_activate(folio);                    /* 从 inactive 搬到 active */
        else
            __lru_cache_activate_folio(folio);
        folio_clear_referenced(folio);                /* 清 referenced,下次重新计数 */
        workingset_activation(folio);
    }
    ...
}
```

这就是**第二机会算法(second chance)**的精髓,用 `PG_active`/`PG_referenced` 两位编码"几阶段活跃度":

| PG_active | PG_referenced | 含义 | 下次访问会怎样 |
|-----------|---------------|------|----------------|
| 0(inactive)| 0 | 全新页,从没标记过 | 置 PG_referenced,留 inactive |
| 0(inactive)| 1 | 被访问过一次 | 第二次访问 → 升 active(第二机会)|
| 1(active)  | 0 | 已升 active,稳定活跃 | 维持 active |
| 1(active)  | 1 | (短暂中间态)| 处理 active 链老化 |

核心规则:**第一次访问置 referenced,第二次访问才升 active**——这就是"第二机会"。一个页要被访问**两次**(在不同周期)才被认定为热,避免了一次性 streaming(只读一次)就把页升 active。

> **钉死这件事**:第二机会算法用 1~2 位状态 + 偶尔搬运(摘挂链表),O(1) 地近似 LRU。代价是精度差(只有 active/inactive 两档,无法区分 active 链内的"哪个更热"),但开销极低。这是 mm 里典型的"用少量状态近似复杂判断"——和 buddy 的 order 编码、struct page 的位段是同一种工程哲学。**朴素 LRU 太精确太贵,second chance 够用且便宜**。

### 老化:active → inactive 的搬运

active 链不能无限膨胀(全成 active 就没意义了)。`shrink_active_list`([vmscan.c:1998](../linux/mm/vmscan.c#L1998))在回收压力大时,把 active 链尾的页搬到 inactive(叫"老化",aging)。搬运规则:**active 链尾的页如果 PG_referenced=0,搬回 inactive 并清 PG_active**(给"曾经热但最近没访问"的页降级)。这让 active 链保持"近期真的活跃"的子集。

---

## 17.4 vmscan 的回收主循环:`shrink_lruvec`

立完 LRU 结构,看 vmscan 怎么扫描它。kswapd(或 direct reclaim)最终调到 [`shrink_lruvec`](../linux/mm/vmscan.c#L5641)([vmscan.c:5641](../linux/mm/vmscan.c#L5641))——这是回收的真正核心。

### 主循环结构

```c
// mm/vmscan.c#L5641-L5700 (大幅简化)
static void shrink_lruvec(struct lruvec *lruvec, struct scan_control *sc)
{
    unsigned long nr[NR_LRU_LISTS];
    unsigned long targets[NR_LRU_LISTS];
    unsigned long nr_to_scan;
    enum lru_list lru;
    unsigned long nr_reclaimed = 0;
    ...

    /* MGLRU 走另一条路径 */
    if (lru_gen_enabled() && !root_reclaim(sc)) {
        lru_gen_shrink_lruvec(lruvec, sc);
        return;
    }

    /* ① 算出每条 LRU 该扫多少页(核心决策在 get_scan_count)*/
    get_scan_count(lruvec, sc, nr);
    memcpy(targets, nr, sizeof(nr));

    blk_start_plug(&plug);
    /* ② 循环扫描,直到所有 LRU 都扫完目标量 */
    while (nr[LRU_INACTIVE_ANON] || nr[LRU_ACTIVE_FILE] || nr[LRU_INACTIVE_FILE]) {

        for_each_evictable_lru(lru) {
            if (nr[lru]) {
                nr_to_scan = min(nr[lru], SWAP_CLUSTER_MAX);  /* 一批最多 32 页 */
                nr[lru] -= nr_to_scan;

                nr_reclaimed += shrink_list(lru, nr_to_scan, lruvec, sc);
            }
        }

        cond_resched();   /* 让出 CPU,避免长期持有 lru_lock */

        if (nr_reclaimed < nr_to_reclaim || proportional_reclaim)
            continue;
        break;
    }
    ...
}
```

主干两步:**① `get_scan_count` 算每条 LRU 扫多少;② 循环调 `shrink_list` 扫**。每批最多 `SWAP_CLUSTER_MAX = 32` 页(一个 swap cluster 大小,和 swap IO 对齐)。`shrink_list` 按 LRU 类型分流([vmscan.c:2152](../linux/mm/vmscan.c#L2152)):

```c
// mm/vmscan.c#L2152-L2164 (简化)
static unsigned long shrink_list(enum lru_list lru, unsigned long nr_to_scan,
                                 struct lruvec *lruvec, struct scan_control *sc)
{
    if (is_active_lru(lru)) {
        /* active 链:不直接回收,做"老化"——把 active 搬到 inactive */
        if (sc->may_deactivate & (1 << is_file_lru(lru)))
            shrink_active_list(nr_to_scan, lruvec, sc, lru);
        else
            sc->skipped_deactivate = 1;
        return 0;   /* active 扫描本身不回收页 */
    }

    /* inactive 链:真正回收 */
    return shrink_inactive_list(nr_to_scan, lruvec, sc, lru);
}
```

关键分工:
- **active 链扫描** = 老化(把曾经热、最近没访问的页搬到 inactive 给回收机会),本身不直接回收。
- **inactive 链扫描** = 真正的回收(`shrink_inactive_list` → `shrink_folio_list`,逐页判断能不能扔)。

> **钉死这件事**:vmscan 从不直接回收 active 链上的页——总是先"老化"到 inactive,再从 inactive 回收。这给了每个页"**第二机会**":曾经热的页降级到 inactive 后,如果在 inactive 期间又被访问,会重新升 active,逃过本次回收。这个"先降级、观察、再决定扔不扔"的两阶段,就是 LRU 近似的精髓——避免一次扫描就把热页误杀。

### `get_scan_count`:扫描平衡的核心决策

每条 LRU 扫多少,由 [`get_scan_count`](../linux/mm/vmscan.c#L2334)([vmscan.c:2334](../linux/mm/vmscan.c#L2334))决定。这是 vmscan 最重要的决策函数,涉及多个分支:

```c
// mm/vmscan.c#L2334-L2422 (大幅简化,保留主干)
static void get_scan_count(struct lruvec *lruvec, struct scan_control *sc,
                           unsigned long *nr)
{
    int swappiness = mem_cgroup_swappiness(memcg);
    enum scan_balance scan_balance;

    /* ① 没 swap 空间或 anon 不可换出:只扫 file */
    if (!sc->may_swap || !can_reclaim_anon_pages(memcg, pgdat->node_id, sc)) {
        scan_balance = SCAN_FILE;
        goto out;
    }

    /* ② memcg 且 swappiness=0:只扫 file */
    if (cgroup_reclaim(sc) && !swappiness) {
        scan_balance = SCAN_FILE;
        goto out;
    }

    /* ③ 接近 OOM(priority=0):anon/file 均等扫 */
    if (!sc->priority && swappiness) {
        scan_balance = SCAN_EQUAL;
        goto out;
    }

    /* ④ file 页极少:强制扫 anon */
    if (sc->file_is_tiny) {
        scan_balance = SCAN_ANON;
        goto out;
    }

    /* ⑤ cache_trim 模式:有足够 inactive file,只扫 file */
    if (sc->cache_trim_mode) {
        scan_balance = SCAN_FILE;
        goto out;
    }

    /* ⑥ 一般情况:按 swappiness + refault 代价比例分摊 */
    scan_balance = SCAN_FRACT;
    total_cost = sc->anon_cost + sc->file_cost;
    anon_cost = total_cost + sc->anon_cost;     /* anon 的总代价(含 refault)*/
    file_cost = total_cost + sc->file_cost;
    total_cost = anon_cost + file_cost;

    ap = swappiness * (total_cost + 1);
    ap /= anon_cost + 1;                          /* anon 扫描比例 */

    fp = (200 - swappiness) * (total_cost + 1);
    fp /= file_cost + 1;                          /* file 扫描比例 */

    fraction[0] = ap;                              /* anon 份额 */
    fraction[1] = fp;                              /* file 份额 */
    ...
}
```

这个函数本质上是**多档决策树**,优先级从上到下:

1. **完全不可换 anon**(没 swap):只扫 file。
2. **memcg swappiness=0**:用户明确不想 swap,只扫 file。
3. **接近 OOM**(priority=0):anon/file 均等扫,抢回任何能抢的页。
4. **file 极少**:anon 多但 file 少,扫 anon。
5. **cache_trim 模式**:file cache 太大,优先 trim file。
6. **一般情况**:按 **swappiness + refault 代价**比例分摊。

第 ⑥ 档是稳态行为,最关键。公式拆开看:

- `ap = swappiness * (total_cost+1) / (anon_cost+1)`:anon 的扫描份额 ∝ swappiness × 总代价 / anon 代价。**anon 回收代价高(refault 多)→ 扫少一点**;**swappiness 大 → 扫 anon 多**。
- `fp = (200-swappiness) * (total_cost+1) / (file_cost+1)`:file 份额对称。注意是 `200 - swappiness`(swappiness 范围 0~200,6.x 改了),所以 swappiness 大 → file 份额小。

`anon_cost`/`file_cost` 由 [`lruvec->anon_cost`/`file_cost`](../linux/include/linux/mmzone.h#L615) 维护,它们累计"最近回收 anon/file 时的 refault 代价"——哪个 LRU 最近回收后 refault 多(refault 意味着回收错了,页其实还热),就把它的 cost 提高,下次少扫它。**这是个反馈环:回收错了 → 提高 cost → 少扫**。

> **钉死这件事**:`get_scan_count` 不是"扫满所有 LRU",而是"在 anon/file 之间、热/冷之间权衡扫描量"。核心变量是 **swappiness**(用户旋钮,0~200,默认 60,大→更愿意扫 anon 换 swap)和 **refault cost**(自适应反馈,回收错了就少扫)。这两个旋钮让 vmscan 在不同负载、不同 swap 配置下都能找到合理的扫描平衡——这是 mm 启发式的精华。

### swappiness:用户旋钮

[`vm_swappiness`](../linux/mm/vmscan.c#L194)([vmscan.c:194](../linux/mm/vmscan.c#L194))是个全局变量,默认 60:

```c
// mm/vmscan.c#L194
int vm_swappiness = 60;
```

它通过 `/proc/sys/vm/swappiness` 可调(0~200,6.x 之前是 0~100)。语义:
- **0**:尽量不换出 anon(只回收 file)。
- **60**(默认):倾向 file,anon 偶尔换。
- **100**:anon/file 等价。
- **200**:更激进换 anon(6.x 新增,主要是 demotion/cgroup 场景)。

注意 swappiness **不是"是否 swap"的开关,而是倾向**——即使 swappiness=0,在 file 极少时(`file_is_tiny`)也会扫 anon,只是不情愿。直觉是:**anon 换出要写 swap(慢 IO),file 干净页可以直接丢(快)**——所以默认倾向 file。但 file 也可能全是热页(cache trim 不动),这时还是要扫 anon。

> **不这样会怎样**:如果没有 swappiness 这个旋钮,系统在 swap 紧张时(比如数据库不愿被 swap)、或在 swap 充足时(比如桌面多任务)用同一套扫描策略,要么"swap 用得太狠数据库卡死"、要么"swap 用不上,内存闲置"。swappiness 给管理员一个旋钮,按负载特性调整——数据库服务器常设 10(尽量不 swap),桌面设 60,某些 HPC 设 100(愿 swap 换吞吐)。

### `shrink_folio_list`:逐页判断回收

`shrink_inactive_list` 把 inactive 链尾的一批页 isolate 出来(从 LRU 摘下,放到一个临时链表),然后调 [`shrink_folio_list`](../linux/mm/vmscan.c#L1011)([vmscan.c:1011](../linux/mm/vmscan.c#L1011))**逐页判断**能不能回收:

```
对 isolate 出的每个 folio:
  ① 还有人映射吗?(rmap 反查,第 15 章)
       - 没人映射 → 可以回收(干净直接丢,脏的回写)
       - 有人映射 → 尝试 unmap(try_to_unmap,第 15 章)
  ② 是脏页吗?
       - 干净 → 直接回收
       - 脏 → 加入 writeback 队列,等 IO 完成再回收
  ③ 是 anon 吗?
       - anon → 写入 swap(swap out)
       - file → 从 page cache 删除(干净)或回写后删除
  ④ 正在 writeback 吗?
       - 是 → 跳过(等 IO 完成)
  ⑤ 最近被访问过吗?(查 PTE accessed 位)
       - 是 → 升 active,跳过回收(第二机会)
```

每一步都可能"跳过这个页"(留在内存)或"决定回收它"。被回收的页:anon 写 swap、file 丢弃或回写后丢弃。**关键:被回收的 file 页如果之前被认为是 workingset(热),会在原 page cache 位置留一个 shadow entry**(下一节)。

---

## 17.5 workingset:用 refault 距离识别真热页

第二机会解决了"streaming 只读一次不升 active",但还不够——**一个页被踢到 inactive、被回收后,如果马上又被访问(refault),怎么知道它是"真热被误杀"还是"streaming 第二轮"?** 这就是 workingset 要解决的。

### 影子条目(shadow entry):被踢出页的"指纹"

当一个 file 页(或 swap cache 的 anon 页)被回收时,内核**在原 page cache 的位置(xarray 里)留一个 shadow entry**——不存数据,只存一个"内存压力指纹"。看 [`workingset_eviction`](../linux/mm/workingset.c#L382)([workingset.c:382](../linux/mm/workingset.c#L382)):

```c
// mm/workingset.c#L382-L404 (简化)
void *workingset_eviction(struct folio *folio, struct mem_cgroup *target_memcg)
{
    struct pglist_data *pgdat = folio_pgdat(folio);

    ...

    return pack_shadow(memcgid, pgdat, eviction,
                       folio_test_workingset(folio));
}
```

shadow entry 的内容,由 [`pack_shadow`](../linux/mm/workingset.c#L199)([workingset.c:199](../linux/mm/workingset.c#L199))打包:

```c
// mm/workingset.c#L199-L208 (简化)
static void *pack_shadow(int memcgid, pg_data_t *pgdat, unsigned long eviction,
                         bool workingset)
{
    eviction &= EVICTION_MASK;
    eviction = (eviction << MEM_CGROUP_ID_SHIFT) | memcgid;
    eviction = (eviction << NODES_SHIFT) | pgdat->node_id;
    eviction = (eviction << WORKINGSET_SHIFT) | workingset;

    return xa_mk_value(eviction);
}
```

把"memcg id + node id + eviction 时间戳 + 是否曾 active"压缩进一个 `void *`(放回 xarray)。这里的核心是 **`eviction`**——它是页被踢出时 `lruvec->nonresident_age` 的快照,一个单调增长的计数器(每次有页变成非驻留就 +1)。`eviction` 相当于"页被踢出时的内存压力时戳"。

> **钉死这件事**:shadow entry 不是数据,是**指纹**——记录"这页是在什么内存压力下被踢出去的"。它放在原 page cache 位置(占用一个 xarray slot,但不占实际内存页),开销极小(一个指针)。这是 mm 里"用极小元数据换回收精度"的典范——和 struct page 的紧凑布局、PTE 的 accessed 位是同一种思路。

### refault 距离:真热的判断

被回收的页,过段时间又被访问(`read` 文件、`do_fault` 文件缺页),会通过 page cache 查到这个 shadow entry。这时调 [`workingset_refault`](../linux/mm/workingset.c#L530)([workingset.c:530](../linux/mm/workingset.c#L530)),算**refault 距离**:

```c
// mm/workingset.c#L418-L518 (简化)
bool workingset_test_recent(void *shadow, bool file, bool *workingset)
{
    ...
    /* 解包 shadow:得到踢出时的 eviction 时戳 */
    unpack_shadow(shadow, &memcgid, &pgdat, &eviction, workingset);

    /* 读当前 nonresident_age(现在的内存压力时戳)*/
    refault = atomic_long_read(&eviction_lruvec->nonresident_age);

    /* refault 距离 = 现在 - 踢出时(用掩码处理回绕)*/
    refault_distance = (refault - eviction) & EVICTION_MASK;

    /* 算 workingset size(当前的活跃页数)*/
    workingset_size = lruvec_page_state(eviction_lruvec, NR_ACTIVE_FILE);
    if (!file)
        workingset_size += lruvec_page_state(eviction_lruvec, NR_INACTIVE_FILE);
    if (mem_cgroup_get_nr_swap_pages(eviction_memcg) > 0) {
        workingset_size += lruvec_page_state(eviction_lruvec, NR_ACTIVE_ANON);
        ...
    }

    /* ★ 关键判断:refault 距离 <= workingset 大小 → 真热! */
    return refault_distance <= workingset_size;
}
```

**核心公式:`refault_distance <= workingset_size` → 真热**。

直觉解释:refault 距离 = "从踢出到 refault 之间,有多少其他页也变成非驻留"——可以理解为"该页在内存外待的时间长度"(以非驻留页数为单位)。workingset_size = 当前内存能容纳的热页总量。如果 refault 距离 ≤ workingset size,说明**这页被踢出去之后,还没等到一整个 working set 大小的页都被换过一遍,它就被重新访问了**——这就是"working set 里的真热页,被误杀了"。立刻激活它([workingset_refault L564](../linux/mm/workingset.c#L564):`folio_set_active(folio)`)。

反之,如果 refault 距离 > workingset size,说明"踢出去之后,内存换过了一整轮,它才被访问"——这是**冷页或 streaming 第二轮**(streaming 数据可能轮一遍要很久,期间内存里所有页都被换过),不激活,留在 inactive 让它下次还被回收。

> **钉死这件事**:workingset refault 距离是 mm 里最 elegant 的启发式之一。它用**一个时戳 + 一个计数器**,在页 refault 时**反推**"这页在踢出期间经历了多少内存压力",从而判断它是不是真热。这套机制不需要记录每个页的访问历史(那样元数据爆炸),只需要在踢出和 refault 各取一次 `nonresident_age`——O(1) 的判断,精度足够好。

### 反面对比:纯 LRU 在 streaming 下的惨剧

回看本章开头说的"streaming IO 冲刷热页"。考虑这个时序:

```
T0: 数据库 working set = 10GB 热页(全在 active)
T1: dd 开始读 100GB 文件(streaming)
T2: dd 读入的 file 页塞满 inactive,被回收(它们时间戳新,但只读一次)
T3: 但 dd 读得太快,inactive 链周转不开,active 链也开始被"老化"搬到 inactive
T4: 数据库热页被搬到 inactive,被回收(被 dd 的 streaming 冲刷)
T5: dd 完成。数据库再访问自己的热页 → 全部缺页!working set 被冲毁了。
```

**纯 LRU 在这里完败**:dd 的页"新但冷",数据库页"旧但热"——LRU 只看新旧,把热的当冷扔了。

workingset 的补救:

```
T4: 数据库热页被回收时,在 page cache 位置留 shadow entry(记 eviction 时戳)
T5: 数据库访问热页 → refault,查到 shadow
T6: workingset_refault 算:refault 距离 小(dd 期间虽然换了很多页,但 refault 很快)
        → 判定"真热,被误杀"→ folio_set_active → 立刻升 active
T7: 数据库热页虽然被回收了,但 refault 时被立刻激活,后续访问就在 active 了
```

workingset 让"被 streaming 冲刷的真热页"在 refault 时**立刻恢复活性**,而不是从 inactive 冷启动(慢慢升 active)。这极大缓解了 streaming 冲刷——你能在 `/proc/vmstat` 看到 `workingset_refault`、`workingset_activate`、`workingset_restore` 这些计数。

> **不这样会怎样**:没有 workingset 的话,数据库备份、大文件 grep、日志扫描这类 streaming 负载会反复冲刷真热页,系统陷入"换出→缺页→换入→再换出"的 thrashing。workingset 用极小的 shadow entry 开销,把 streaming 的危害降到"被误杀的页 refault 时立即恢复"——这是 mm 在"启发式不完美"上的关键补救。

### `nonresident_age`:全节点共享的"虚拟时钟"

`lruvec->nonresident_age`([mmzone.h:618](../linux/include/linux/mmzone.h#L618))是个 atomic_long,每次有页变非驻留(被踢出)+1。它是个**全 lruvec 共享的虚拟时钟**——不反映真实时间,反映"内存压力累积"。所有 eviction/refault 都读写它,所以 atomic。

为什么用"非驻留页数"而不是真实时间(wall clock)?因为回收的价值判断和**时间无关,和压力有关**。一个页 1 秒没访问,如果系统空闲(nonresident_age 没怎么涨),它可能还是热的;如果系统内存压力大(nonresident_age 涨了很多),它就是冷的。**用 nonresident_age 当时钟,自动适应了系统负载**——忙的系统里"短时间就判冷",闲的系统里"长时间还判热"。

---

## 17.6 技巧精解:LRU 近似 + workingset 价值估算

本章两个最硬核的设计,单独拆透。

### 技巧一:第二机会算法——O(1) 近似 LRU

#### 它解决什么问题

需要"按最近最少使用回收",但精确 LRU 要给每页记时间戳、每次访问更新——海量页 × 频繁更新 = 元数据与 CPU 开销爆炸。

#### 反面对比:精确 LRU 会怎样

> **反面对比**:给每个页一个 8 字节时间戳,每次访问都更新。一台 16GB 机器有 4 百万页,时间戳本身 32MB(尚可),但**每次访问都要改时间戳**——每秒几十亿次访问,要么走软件路径(慢到无法接受)、要么走硬件(PTE accessed 位只能告诉你"被访问过",不能告诉你"什么时候")。而且挑最旧的页要排序,每次回收 O(n log n)。这些开销在 mm 的关键路径上完全无法承受。

#### 实现的精妙:两位状态 + 双链搬运

Linux 的解法是**用两条链 + 两个 bit 近似 LRU**:

1. **两条链**:active(推测热)、inactive(推测冷)。回收只扫 inactive。
2. **两个 bit**:`PG_active`(在 active 链吗)、`PG_referenced`(被访问过吗)。
3. **状态转换**:
   - 新页进 inactive,`PG_active=0, PG_referenced=0`。
   - 第一次访问 → `PG_referenced=1`(第二机会的"第一机会")。
   - 第二次访问(已 referenced)→ 升 active,`PG_active=1, PG_referenced=0`(第二机会用完,升级)。
   - 老化时 active 链尾的页 → 搬回 inactive,`PG_active=0`。

每次操作:**O(1) 的位测试 + 链表摘挂**。代价是**精度差**——active 链内的页无法区分"哪个更热"(都是 active,没有顺序信息),但这个精度损失在大多数负载下可接受,因为:
- active 链的页反正不直接回收(要先老化到 inactive),误判无害。
- 真要细分,有 workingset refault 距离做更精确的判断(下个技巧)。

> **钉死这件事**:第二机会是"**用最小状态近似 LRU**"的典范。1 个 bit(PG_referenced)+ 1 个 bit(PG_active),编码出 4 个状态,配合两条链的搬运,实现了"够用的 LRU 近似"。这是 mm 里反复出现的工程哲学:**朴素方案太精确太贵,用少量状态近似就够了**——后续 workingset 在判断失误时补救。两招组合,精度和开销两全。

### 技巧二:workingset refault 距离——反推页的"价值"

#### 它解决什么问题

LRU 近似会误杀真热页(尤其 streaming 冲刷)。被回收的页,refault 时怎么判断"是误杀"还是"本来就该冷"?

#### 反面对比:没有 workingset 会怎样

> **反面对比**:没有 shadow entry,被回收的页 refault 时无法知道"之前是热是冷",只能进 inactive 重新走第二机会。streaming 冲刷的真热页,refault 后要等"两次访问"才升 active——这期间可能又被回收(还在 inactive)。系统陷入 thrashing:streaming 一次次冲刷,真热页一次次被回收、refault、又被回收。

#### 实现的精妙:shadow entry + nonresident_age 反推

workingset 的精妙在于**用踢出时的指纹 + refault 时的现状,反推"踢出期间的压力"**:

1. **踢出时**:存 `eviction = nonresident_age`(当前压力时戳)+ `workingset`(是否曾 active)。
2. **refault 时**:读 `refault = nonresident_age`(当前压力时戳),算 `refault_distance = refault - eviction`。
3. **判断**:`refault_distance <= workingset_size` → 真热,激活;否则冷,留 inactive。

精妙之处:
- **不需要记每页访问历史**。只在踢出和 refault 两个时刻各读一次全局 `nonresident_age`,O(1)。
- **nonresident_age 是"压力时钟"而非真实时钟**,自动适应系统负载(忙时短间隔就判冷,闲时长间隔还判热)。
- **shadow entry 开销极小**:一个指针大小的 xarray slot,不占实际内存页。被 inode 回收时一起清掉。

这个机制让 mm 在 LRU 误判时有**自我修正**能力——误杀的热页 refault 时立刻恢复,把 streaming 的危害降到最低。

> **钉死这件事**:workingset refault 是 mm 里**反馈机制**的典范。LRU 是前馈(按状态判断),workingset 是反馈(按结果修正)。两者组合,让回收的"价值判断"在 LRU 近似的基础上有了**自我纠错**能力。这是 mm 启发式设计的高光——不追求一次到位的完美算法,而是用"近似 + 反馈修正"在绝大多数负载下都"差不多对"。

---

## 17.7 MGLRU:6.x 默认的多代 LRU

前面讲的都是"传统 LRU"(active/inactive 双链),但 Linux 6.x 默认启用了一套新机制——**MGLRU(Multi-Gen LRU)**。它把双链扩成**多代(generation)**,更精确识别冷热。本节拆它。

### MGLRU 的动机:双链的精度瓶颈

传统双链的问题:**只有两档(active/inactive)**,链内无顺序。这导致:
- active 链里"刚升上来"和"已经 active 很久"的页无法区分,老化时一律按 FIFO 搬到 inactive。
- inactive 链里"刚降级"和"早该回收"的页也无法区分。
- 在大内存机器上,LRU 链极长(几百万页),双链的粒度太粗——很多"中间温度"的页被错判。

MGLRU 的解法:**把 active/inactive 扩成多个"代(generation)"**,每一代代表一个"访问时间窗"。页被访问 → 移到最新代;回收 → 从最旧代扫。代数越多,冷热分辨率越高。

### MGLRU 的数据结构

[`struct lru_gen_folio`](../linux/include/linux/mmzone.h)(在 `lruvec->lrugen` 里)维护多代 LRU。每代有一个序号(`min_seq` ~ `max_seq`),每代下分 anon/file 两个类型、每类型多个 tier(tier 表示"被访问次数",更细的冷热):

```
MGLRU 结构(简化,概念示意):

lruvec->lrugen:
  min_seq[ANON], min_seq[FILE]   ← 最旧代序号(回收从这里扫)
  max_seq                        ← 最新代序号(新访问的页进这)

  generations:  Gen 0(最旧,最冷)  Gen 1   Gen 2   ...   Gen N(最新,最热)
                ┌─────────────┐ ┌──────┐ ┌──────┐     ┌──────────────┐
   anon tier 0  │ 即将回收     │ │      │ │      │     │ 新页进这       │
   anon tier 1  │              │ │      │ │      │     │              │
   file tier 0  │              │ │      │ │      │     │              │
   file tier 1  │              │ │      │ │      │     │              │
                └─────────────┘ └──────┘ └──────┘     └──────────────┘
                ▲                                              ▲
                │                                              │
            回收扫这                                         新访问进这
        (evict_folios)                                   (folio_update_gen)
```

每代下按 anon/file × tier 组织。tier 来自"被访问的 refs 数"(`lru_tier_from_refs`),访问次数多的页在更高 tier(更热)。`get_tier_idx`([vmscan.c](../linux/mm/vmscan.c))按"refault 概率"选要扫的 tier——哪个 tier 的页"再访问概率低"就先扫它。

### MGLRU 的回收决策:`evict_folios` / `get_type_to_scan`

MGLRU 的回收入口是 [`lru_gen_shrink_lruvec`](../linux/mm/vmscan.c#L4863)([vmscan.c:4863](../linux/mm/vmscan.c#L4863)),它循环调 [`evict_folios`](../linux/mm/vmscan.c#L4507)([vmscan.c:4507](../linux/mm/vmscan.c#L4507))。`evict_folios` 调 [`isolate_folios`](../linux/mm/vmscan.c#L4460)([vmscan.c:4460](../linux/mm/vmscan.c#L4460))选页——选哪个 type(anon/file)、哪个 tier,由 [`get_type_to_scan`](../linux/mm/vmscan.c#L4432)([vmscan.c:4432](../linux/mm/vmscan.c#L4432))决定:

```c
// mm/vmscan.c#L4432-L4458 (简化)
static int get_type_to_scan(struct lruvec *lruvec, int swappiness, int *tier_idx)
{
    int type, tier;
    struct ctrl_pos sp, pv;
    int gain[ANON_AND_FILE] = { swappiness, 200 - swappiness };

    /* 比较 anon vs file 的第一 tier,选收益大的 */
    read_ctrl_pos(lruvec, LRU_GEN_ANON, 0, gain[LRU_GEN_ANON], &sp);
    read_ctrl_pos(lruvec, LRU_GEN_FILE, 0, gain[LRU_GEN_FILE], &pv);
    type = positive_ctrl_err(&sp, &pv);

    /* 在选定 type 里,选要扫到哪个 tier */
    read_ctrl_pos(lruvec, !type, 0, gain[!type], &sp);
    for (tier = 1; tier < MAX_NR_TIERS; tier++) {
        read_ctrl_pos(lruvec, type, tier, gain[type], &pv);
        if (!positive_ctrl_err(&sp, &pv))
            break;
    }
    *tier_idx = tier - 1;
    return type;
}
```

`positive_ctrl_err` 比较"继续扫这个 tier 的收益"(基于 refault 概率模型)。MGLRU 用一个**控制论模型**(`ctrl_pos`,控制位置)估算"扫这一 tier 的预期收益",选收益最高的 type+tier 扫——这比传统 LRU 的 SCAN_FRACT 更精确。

### MGLRU 的访问追踪:`lru_gen_look_around`

传统 LRU 靠 PTE accessed 位 + `folio_mark_accessed`,但 PTE accessed 位是**惰性**的(硬件置位,软件要查才清)。MGLRU 引入 [`lru_gen_look_around`](../linux/mm/vmscan.c#L3988)([vmscan.c:3988](../linux/mm/vmscan.c#L3988)):在回收扫描某个 PTE 时,**顺便扫描同一 PMD(2MB 区域)内的所有 PTE**,把被访问的页一次性升级到最新代。这利用了**空间局部性**——访问某页时,邻近页很可能也被访问(典型如顺序读、循环遍历数组)。

```c
// mm/vmscan.c#L3988-L4032 (简化)
void lru_gen_look_around(struct page_vma_mapped_walk *pvmw)
{
    ...
    /* 扫描当前 PMD 范围内的所有 PTE */
    start = max(addr & PMD_MASK, vma->vm_start);
    end = min(addr | ~PMD_MASK, vma->vm_end - 1) + 1;

    ...
    for (i = 0, addr = start; addr != end; i++, addr += PAGE_SIZE) {
        pte_t ptent = ptep_get(pte + i);
        pfn = get_pte_pfn(ptent, vma, addr);
        ...
        /* 如果这页被访问了(accessed 位),升级到最新代 */
        if (ptep_test_and_clear_young(vma, addr, pte + i))
            folio_update_gen(folio, new_gen);
    }
    ...
}
```

`look_around` 是 MGLRU 相比传统 LRU 的一个**关键性能优化**——传统 LRU 要逐页 `folio_mark_accessed`(每次锁),MGLRU 在回收路径上"顺便"批量扫描 PMD,一次处理多个页,大幅减少 lru_lock 次数。

### MGLRU vs 传统 LRU:对照

| 维度 | 传统 LRU | MGLRU |
|------|---------|-------|
| 链数 | 4(active/inactive × anon/file)+ unevictable | 多代 × anon/file × tier,结构更细 |
| 冷热粒度 | 2 档(active/inactive) | N 代 + tier,粒度细 |
| 访问追踪 | PTE accessed → folio_mark_accessed | PTE accessed + look_around 批量扫描 |
| 决策模型 | swappiness + refault cost 比例 | 控制论模型(ctrl_pos),更精确 |
| 默认启用 | 5.x 及之前 | **6.x 默认**(CONFIG_LRU_GEN) |
| 适用场景 | 一般负载 | 大内存、复杂负载(更精确,开销可控) |

MGLRU 在 6.x 默认启用,但传统 LRU 路径仍在(`lru_gen_enabled()` 为 false 时走老路)。可以 `/sys/kernel/mm/lru_gen/enabled` 查看/控制。

> **钉死这件事**:MGLRU 是 mm 在 6.x 的重大升级,用"多代 + tier + 控制论决策 + look_around 批量扫描"替代传统双链。它在 SSD、大内存、容器等现代负载下表现明显更好(社区 benchmark 显示 thrashing 减少、cache 命中率提升)。但设计哲学一脉相承——**用 O(1) 状态近似 LRU,用 workingset refault 修正误判**,只是 MGLRU 把"近似"做得更细。传统路径仍保留,作为兜底。

---

## 17.8 把本章放进全局:回收链的第二环

回收链至今两环:

| 章 | 环 | 回答的问题 | 关键机制 |
|----|----|---------|---------|
| P5-16 | ① 何时收 | free 多少时启动回收 | watermark(low/high/min)+ kswapd 滞回 |
| **P5-17** | **② 收谁** | **回收哪些页(价值判断)** | **LRU 双链 + 第二机会 + workingset refault + swappiness + MGLRU** |
| P5-18 | ③ 碎片化 | 高阶分配拿不到连续页怎么办 | compaction 规整 |
| P5-19 | ④ 兜底 | 实在收不动了 | swap 换出 anon + OOM 杀进程 |

本章服务**回收路径**,而且是**价值判断**这一环——决定哪些页值得留、哪些可以扔。这是 mm 启发式设计的精华:每一步都不完美(LRU 近似有误判、workingset 也只是估算),但组合起来在绝大多数负载下"差不多对",且开销可控。在 mm 圈里,这套机制被称为 "**The workingset protection**"——保护真热页不被误杀,是 Linux 长期稳定运行的核心保障之一。

---

## 章末小结

这一章是回收链的第二环——**回收谁**。我们没有讲新的"何时收"(上一章 watermark + kswapd)、也没讲"碎片化怎么办"(下一章 compaction)、更没讲"swap 怎么换出"(第 19 章),只讲了一件事:**给定要回收,怎么选页**。

钉死四件事:

1. **LRU 双链 + 第二机会**:active/inactive × anon/file 四链 + `PG_active`/`PG_referenced` 两位 + PTE accessed 位,O(1) 近似 LRU。第二次访问才升 active,避免 streaming。
2. **workingset refault 距离**:被回收的页在原位置留 shadow entry(记 `nonresident_age` 时戳),refault 时算 `refault_distance <= workingset_size` 判断真热,立刻激活——补救 LRU 误杀。
3. **`shrink_lruvec` + `get_scan_count`**:按 swappiness + refault cost 平衡 anon/file 扫描比例,priority 控制扫描量,`SWAP_CLUSTER_MAX` 批处理。
4. **6.x 默认 MGLRU**:多代 + tier + 控制论决策 + look_around 批量扫描,比双链更精确,SSD/大内存负载下表现更好。

四个关键技巧:

- **第二机会算法**:两位状态 + 双链搬运,O(1) 近似 LRU(反面对比"精确 LRU 时间戳开销爆炸")。
- **anon/file 分链**:按回收代价(换 swap vs 丢弃)分类扫描,swappiness 平衡。
- **shadow entry + nonresident_age**:用极小元数据(一个指针)+ 单调计数器反推页的"压力期"。
- **MGLRU look_around**:批量扫描 PMD 范围内 PTE,利用空间局部性减少 lru_lock 次数。

### 五个"为什么"清单

1. **为什么用 active/inactive 双链而不是精确时间戳 LRU?** 精确 LRU 要给每页记时间戳、每次访问更新——海量页 × 频繁更新 = 元数据与 CPU 开销爆炸。双链 + 两位状态用 O(1) 摘挂近似 LRU,精度虽差但够用,workingset 在误判时补救。这是 mm "用少量状态近似复杂判断"的典型。
2. **为什么 anon 和 file 分链?** anon 换出要写 swap(慢 IO),file 干净页可以直接丢(快)。分链让 vmscan 按"代价类别"分别扫描,用 swappiness 平衡——避免在 swap 紧张时还大量扫 anon,或反之。
3. **workingset 怎么补救 streaming IO 冲刷?** 被回收的页在原 page cache 位置留 shadow entry(记 `nonresident_age` 时戳)。refault 时算 `refault_distance = 现在 - 踢出时`,距离 ≤ workingset size 说明"踢出去不久就又访问了"(真热),立刻激活。这让 streaming 冲刷的真热页 refault 时立即恢复活性。
4. **swappiness=60 这个默认值是什么意思?** swappiness 是 0~200 的旋钮,控制 vmscan 扫 anon vs file 的倾向。60 表示"中等偏向 file"(默认倾向回收 file,因为 file 干净页丢弃便宜,anon 换出要写 swap 慢)。数据库常调到 10(尽量不 swap),某些 HPC 调到 100+。它不是"是否 swap 的开关",是倾向。
5. **MGLRU 比传统 LRU 好在哪?** ① 多代 + tier 让冷热粒度更细(active/inactive 只两档,N 代能区分更多"中间温度");② 控制论决策模型(`ctrl_pos`)比传统 swappiness 比例更精确;③ `look_around` 批量扫描 PMD 利用空间局部性,减少锁次数。在 SSD、大内存、容器负载下 thrashing 更少、cache 命中率更高。6.x 默认启用,传统路径仍保留兜底。

### 想继续深入往哪钻

- **源码**:
  - [`mm/vmscan.c`](../linux/mm/vmscan.c):本章主角。重点读 `shrink_lruvec`(L5641)、`shrink_node`(L5887)、`shrink_list`(L2152)、`shrink_active_list`(L1998)、`shrink_inactive_list`(L1880)、`shrink_folio_list`(L1011)、`get_scan_count`(L2334)、`isolate_lru_folios`(L1612)、MGLRU 路径 `lru_gen_shrink_lruvec`(L4863)/`evict_folios`(L4507)/`isolate_folios`(L4460)/`get_type_to_scan`(L4432)/`lru_gen_look_around`(L3988)。
  - [`mm/workingset.c`](../linux/mm/workingset.c):`workingset_refault`(L530)、`workingset_test_recent`(L418)、`workingset_eviction`(L382)、`workingset_activation`(L584)、`pack_shadow`(L199)/`unpack_shadow`(L210)、MGLRU 路径 `lru_gen_eviction`(L232)/`lru_gen_refault`(L280)/`lru_gen_test_recent`(L263)。
  - [`mm/swap.c`](../linux/mm/swap.c):`folio_add_lru`(L516)、`folio_mark_accessed`(L473)、`folio_batch_add_and_move`(L243)。
  - [`include/linux/mmzone.h`](../linux/include/linux/mmzone.h):`enum lru_list`(L273)、`struct lruvec`(L606)、`struct lru_gen_folio`、`nonresident_age`。
  - [`include/linux/page-flags.h`](../linux/include/linux/page-flags.h):`PG_active`(L109)、`PG_referenced`(L103)、`PG_workingset`(L110)、`PG_reclaim`(L119)。
- **观测**:
  - `cat /proc/vmstat | grep -E 'pgsteal|pgscan|workingset'`:看 `pgsteal_kswapd`/`pgsteal_direct`/`pgscan_kswapd`/`pgscan_direct`(回收/扫描量)、`workingset_refault`/`workingset_activate`/`workingset_restore`(workingset 命中率)。
  - `cat /sys/devices/system/node/node*/vmstat`:每 NUMA node 的回收统计。
  - `cat /proc/sys/vm/swappiness`:看 swappiness。
  - `cat /sys/kernel/mm/lru_gen/enabled`:看 MGLRU 是否启用(6.x 默认 `0x7` 即启用)。
  - `echo 1 > /sys/kernel/mm/lru_gen/enabled && echo 1000 > /sys/kernel/mm/lru_gen/min_ttl_ms`:可以打开 MGLRU 的 debug(显示代信息)。
  - `perf stat -e vmscan:* <cmd>`:精确统计 vmscan 各事件。
  - 压测对比:用 `cachetop` 或 `vmstat 1` 看不同 swappiness、不同负载下 cache 命中率变化。
- **延伸**:
  - **MGLRU 论文/文档**:Yu Zhao 的 MGLRU patch set,以及 Google/Ant Group 在生产环境的实测报告(SSD/Android/容器场景显著改善)。`Documentation/admin-guide/mm/multigen_lru.rst`。
  - **refault distance 的理论**:Johannes Weiner 的 original patch commit message 有完整推导(`mm: workingset: per-refault latency check`),核心思想来自 SPDY/cache 替换理论。
  - **swap 与 workingset 互动**:被 swap out 的 anon 页,swap cache 也留 shadow entry;refault 时(do_swap_page)同样算 refault 距离。第 19 章(swap)会接上。
  - **memcg 的 swappiness**:每个 cgroup 可以有自己的 swappiness(`memory.swappiness`),让不同 cgroup 用不同 swap 策略——容器场景常用。
  - **memory.reclaim**(cgroup v2):用户主动触发回收的接口,可以测试 vmscan 行为。

### 引出下一章

本章立起了"**收谁**"——LRU 价值判断 + workingset 修正 + vmscan 扫描循环。但回收只解决了"页不够"的问题,**没解决"连续页不够"的问题**。

考虑这个场景:free 页总数够(比如 100MB 空闲),但要分配一个 order-3(8 页连续)的大页(THP、HugeTLB、网络大 buffer)——如果这 100MB 全是碎片化的单页(每两页之间夹着不可移动的页),order-3 分配会失败。**回收再多也没用,因为问题是"不连续"而不是"不够"**。

下一章,第 18 章,我们拆 **compaction**(内存规整)——把可移动的页搬走,凑出连续大块。它是回收之后的"**最后一公里**":回收腾出空间,compaction 把空间规整成连续。两者协作,才能满足高阶分配。compaction 也是 kswapd 的搭档(kswapd 回收完会唤醒 kcompactd 规整,见上一章 16.4)。

> 迷路时回到二分法:本章服务**回收路径**——价值判断这一环。第 16 章(何时)、第 17 章(收谁)、第 18 章(连续化)、第 19 章(兜底),四层兜底构成回收链。每层都不是完美解,但组合起来让 Linux mm 在物理内存有限的前提下,撑起海量的虚拟空间和并发负载——这就是"启发式 + 多层兜底"的工程哲学。
