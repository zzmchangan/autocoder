# 第 12 章 · 回收、swap 与 OOM:内存不够时怎么办

> **核心问题**:物理 RAM 终究有限,分配压力大时,内核怎么决定"牺牲谁"?

> 读完本章你会明白:
> - 为什么回收要给住户**分三类**(干净文件页 / 脏页 / 匿名页),而不是一刀切地赶人——每类的"代价"天差地别。
> - 为什么回收用的不是教科书式的简单 LRU,而是"**活跃/非活跃两条链 + 第二机会**"的老化机制。
> - 三条**水位线**(min / low / high)如何分工,逼出"后台打扫 / 直接回收 / 杀进程"三级响应。
> - OOM killer 凭什么挑出"该杀的那一个"——它算的这笔账,和直觉其实不太一样。

> **如果一读觉得太难**:先只记住三件事——① 回收就是把"空房间"腾出来,代价从低到高是**干净文件页(直接丢)→ 脏页(写回磁盘再丢)→ 匿名页(swap 到地下室)**;② 回收不是简单 LRU,而是用"**活跃链 + 非活跃链 + 访问位第二机会**"把页面慢慢从活跃熬到非活跃、再熬出去,核心思想是"最近没用过的优先牺牲";③ 压力分三档——**低于 low**,kswapd 后台悄悄打扫;**逼近 min**,分配者自己上手"直接回收";**实在腾不出来**,OOM killer 杀进程。其余细节第二遍配合源码再抠。

---

## 一、回收的根本矛盾:空房间是有限资源

前 11 章我们一直在"分房间":伙伴系统发页、slab 切小对象、page cache 把文件搬进楼里住、VMA 圈虚拟地、page fault 兑现物理页。一路下来,大楼里**塞满了住户**——进程的堆栈、文件缓存、内核数据结构……满当当的。

但大楼(物理 RAM)是**有限**的。第 1 章讲过 RAM 的本性之一就是"有限"。当分配请求接踵而至、而空房间不够时,内核只有两条路:**要么把已经住下的某些住户请出去腾地方,要么拒绝这次分配**。拒绝分配对用户进程往往意味着 `malloc` 失败或被杀——这是最后手段。所以正常情况下,内核会**先想办法腾房间**,这就是**回收(reclaim)**。

> **不回收会怎样?** 假设内核只进不出:进程要多少给多少,从不请人离开。那么大楼很快就住满了,**任何新来的请求都直接失败**。可现实是,楼里大部分住户此刻是"占着房间不干活"的——比如某个文件刚读过一次、之后再也不碰,它占的那页缓存完全是浪费。把这些"冷"住户请走,房间立刻腾出来,系统就能继续运转。**回收的本质,就是把"此刻价值最低的房间"腾出来,给"此刻最需要的请求"。**

但马上就遇到一个要命的问题:**住户不是同质的。** 请走谁、怎么请、代价多大,差别巨大。这就引出本章的核心。

---

## 二、三类住户,三种待遇:为什么不能一刀切

大楼里的住户,按"赶走它们的代价"可以分成三类,内核必须区别对待。

### 第一类:干净的文件页(file-backed, clean)——直接赶走

page cache(第 11 章)里那些**读进来、没被改过**的文件页。它们的内容磁盘上有**原件**,内存里这份只是副本。

> 赶走它代价多大?**几乎为零**。直接把内存里的副本丢弃,房间清空。下次谁要用这页,从磁盘重新读一份就行——慢一点,但数据绝无丢失风险。

### 第二类:脏页(dirty)——先写回,再赶走

那些**被进程写过、但还没同步回磁盘**的文件页(或 tmpfs/shmem 页)。磁盘上的原件是**旧的**,内存里这份才是最新的真相。

> 不写回就赶走会怎样?**数据丢失**。进程以为"我已经写进文件了",可内存里的新内容被丢掉、磁盘上还是旧的——用户数据凭空消失。所以脏页**绝不能直接丢**,必须先把它写回磁盘(writeback),变成"干净页",再按第一类处理。这一步代价明显更高:要发起磁盘 I/O。

### 第三类:匿名页(anonymous)——搬到地下室(swap)

进程的堆、栈、匿名 mmap 分配出来的页。这些数据**磁盘上根本没有原件**——它们是进程凭空造出来的,只存在于内存里。

> 直接赶走会怎样?**无法挽回地丢失**。不像文件页那样磁盘上有备份,匿名页一旦丢掉,进程的数据就没了——堆里的变量、栈里的状态全没了,进程基本就废了。所以匿名页**不能丢**,只能**整体搬到地下室**:swap 区(磁盘上专门划出来的一块)。台账(页表)上记一笔"这门牌的东西现在在地下室第几格",等进程再访问这门牌时,触发 page fault,把内容从地下室搬回楼上。

看真实的分类依据——LRU 链表本身就按"匿名 / 文件"两条线分开组织,见 [include/linux/mmzone.h:279-286](../linux-6.14/include/linux/mmzone.h#L279-L286):

```c
/* include/linux/mmzone.h:279 —— 回收用的 LRU 链表,匿名/文件各分活跃/非活跃 */
enum lru_list {
    LRU_INACTIVE_ANON = LRU_BASE,
    LRU_ACTIVE_ANON = LRU_BASE + LRU_ACTIVE,
    LRU_INACTIVE_FILE = LRU_BASE + LRU_FILE,
    LRU_ACTIVE_FILE = LRU_BASE + LRU_FILE + LRU_ACTIVE,
    LRU_UNEVICTABLE,        // 锁定页(如 mlock),根本不许动
    NR_LRU_LISTS
};
```

> **为什么要分匿名/文件两条线?** 因为前述三类代价差异巨大。如果混在一起一条链,回收时可能随手就抓到一个匿名页,把它 swap 出去——代价高、还慢。把两类分开,内核就能根据当下压力**有选择地多扫文件页、少扫匿名页**(因为丢文件页便宜)。具体的扫多少由"swappiness"参数调节,第四节细讲。

### 实际驱逐的决策:干净 / 脏 / 匿名各走各的路

回收代码的核心驱逐函数 [`shrink_folio_list`](../linux-6.14/mm/vmscan.c#L1082-L1595) 对每个被抓出来的 folio(页)逐个判断"怎么处置"。脏页要走 writeback 这条路,入口是 [`pageout`](../linux-6.14/mm/vmscan.c#L638-L714):

```c
/* mm/vmscan.c:1393 —— 脏页:在允许写回的前提下,发起 pageout */
if (!sc->may_writepage)
    goto keep_locked;
/* ... */
switch (pageout(folio, mapping, &plug, folio_list)) {   // 写回磁盘
case PAGE_SUCCESS:
    /* 写回完成后,这页变干净了,可以继续走"移除映射"那一步 */
```

> 注意 `sc->may_writepage` 这个开关。它在 [`try_to_free_pages`](../linux-6.14/mm/vmscan.c#L6514-L6555) 里被初始化为 `!laptop_mode`(笔记本模式下默认禁止写回,避免频繁唤醒磁盘省电)。**这就是"代价分级"在代码里的直接体现:脏页能不能赶,本身就是一个可配置的策略,而不是无脑赶。**

---

## 三、决定"牺牲谁":为什么不是简单 LRU

知道要赶三类住户,但**每一类里到底先赶哪个**?最直觉的答案是 **LRU(Least Recently Used,最近最少使用)**:谁最久没人用,就赶谁。

> **不用 LRU 会怎样?** 用 FIFO(先进先出):谁住进来早谁先走。问题是,一个被频繁使用的热页面,仅仅因为它住得久,就被优先赶走——而一个住进来不久、却再也没人用的冷页面反而留着。这会让缓存命中率暴跌,系统反复"赶走热页 → 又得重新读回来",抖动到不可用。

但**朴素的 LRU 也有硬伤**:它要求每次访问都精确地把页面"移到链表头"。对一个 4KB 的页,光是更新链表指针就要拿锁、改内存——内存访问这么频繁,这个记账成本会让 CPU 拖垮。

Linux 的解法是经典的 **"第二机会(second chance)"** 思想,落地成**两条链 + 一个硬件访问位**:

1. **两条链**:每类(匿名/文件)各分**活跃(active)链**和**非活跃(inactive)链**。新页面先进非活跃链;被反复访问的,晋升到活跃链。回收时**只从非活跃链尾巴抓人**。
2. **硬件访问位(PTE 的 accessed 位)**:页表项里有个 accessed 位,MMU 每次翻译地址时**硬件自动置位**(几乎零开销,不需要软件动链表)。内核扫描时检查这位:置位了 = "最近被用过",给它**第二机会**——清掉这位,把它在链表里往后挪;没置位 = "很久没用了",牺牲它。

> **大楼比喻**:物业不会每次住户开门都去翻登记簿(那是简单 LRU 的代价)。他的做法是:每个房间门上贴个"今日有人出入"的电子标签,住户一开门标签自动亮(硬件置位,免费)。物业定期巡视(扫描):标签亮的——"还在用",清掉标签放它一马(第二机会);标签灭的——"好久没人了",请走。**用"硬件置位 + 软件批量扫描"代替"每次访问都动链表",把记账成本摊薄到几乎为零。**

看活跃链怎么把页面"熬"成非活跃——这正是 [`shrink_active_list`](../linux-6.14/mm/vmscan.c#L2074-L2167) 干的事:

```c
/* mm/vmscan.c:2125 —— 扫描活跃链,决定每页是"留活跃"还是"降级到非活跃" */
/* Referenced or rmap lock contention: rotate */
if (folio_referenced(folio, 0, sc->target_mem_cgroup, &vm_flags) != 0) {
    /* 最近被访问过 → 给第二机会,留在活跃链(甚至保护可执行代码) */
    if ((vm_flags & VM_EXEC) && folio_is_file_lru(folio)) {
        nr_rotated += folio_nr_pages(folio);
        list_add(&folio->lru, &l_active);   // 留在活跃链
        continue;
    }
}

folio_clear_active(folio);      /* we are de-activating */
folio_set_workingset(folio);
list_add(&folio->lru, &l_inactive);   // 降级到非活跃链,成为"候选牺牲品"
```

`folio_referenced` 检查的就是那个硬件 accessed 位。访问过的留一马(`VM_EXEC` 的可执行代码更是重点保护——它一旦被换出,程序运行就卡);没访问过的,清掉 active 标志,**降级到非活跃链**。非活跃链尾巴的页面,就是下一轮真正被牺牲的候选。

> **这就是"双指针/双链表"老化的实质**:页面不是直接被赶,而是先从活跃链"熬"到非活跃链,**在非活跃链上再熬一轮**(这一轮里若又被访问,还有机会升回活跃)。要经过"两道闸"才会被牺牲。这避免了 LRU 对偶发访问的过激反应——一次冷访问不会立刻把热页踢走。**用两条链的"缓冲带"吸收访问模式的抖动,代价是牺牲品可能稍微旧一点,但系统稳定得多。**

> 小提示:现代内核(5.x 之后)默认启用了 **MGLRU(Multi-Gen LRU)**,它把"两条链"细化成"多代"层级,老化更精细。你在 [`shrink_lruvec`](../linux-6.14/mm/vmscan.c#L5703-L5819) 开头会看到 `if (lru_gen_enabled())` 这个分支跳到 `lru_gen_shrink_lruvec`。**但底层思想没变**:还是"用代际/链表 + 硬件访问位,把最近用过的保护起来、把最久没用的牺牲掉"。理解了经典双链表模型,MGLRU 只是它的更精细版本。

---

## 四、匿名页 vs 文件页:扫多少,谁说了算(swappiness)

三类住户里,代价最低的是**干净文件页**(直接丢)。那是不是回收时**只扫文件页、永远不碰匿名页**就最优了?

> **只扫文件页会怎样?** 短期看是省事,但会埋雷。想象一个数据库服务器:它的工作集几乎全是匿名页(进程堆里缓存的数据),文件页很少。如果回收死活不碰匿名页,那点可怜的文件页很快被扫光、反复丢又反复读,系统抖动;而占大头的匿名页动都不动,内存压力根本缓解不了。**反之,如果无脑狂 swap 匿名页**,swap 区在磁盘上,搬进搬出极慢,系统会被 I/O 拖垮。

所以内核需要**在"丢文件页"和"swap 匿名页"之间按比例平衡**,且这个比例可调。调节旋钮就是 **swappiness**:

```c
/* mm/vmscan.c:203 —— 全局默认 60(范围 0~100) */
int vm_swappiness = 60;
```

`get_scan_count`([mm/vmscan.c:2410-2605](../linux-6.14/mm/vmscan.c#L2410-L2605))就是把这个旋钮翻译成"匿名链扫多少页、文件链扫多少页"的实际数字。它还综合考虑了**两类各自的回收历史**(最近从哪类回收得多,就稍微避一避,避免某类被扫秃)和**代价**(anon_cost / file_cost——哪类搬动更贵就少扫)。

> **直觉**:swappiness=0 表示"尽量只丢文件页,别 swap";swappiness=100 表示"匿名页和文件页一视同仁地扫"。默认 60 是个偏向文件页、但不拒绝 swap 的折中。**为什么不能 0 或 100 写死?** 因为不同负载差异巨大:数据库想要低 swappiness(保护它的匿名热数据),桌面/容器可能想要高一点(让匿名页也能被回收换出文件缓存)。**把这个权衡暴露成可调参数,是内核"不替你做死决定"的一贯风格。**

---

## 五、三条水位线:min / low / high——压力分级

回收不是"内存满了才一次性大扫除"——那样会让分配请求**阻塞在回收上**很久,体验极差。Linux 的做法是设**三条警戒水位**,让回收分**前中后三档**提前介入。

先看水位线本身,定义在 [include/linux/mmzone.h:671-676](../linux-6.14/include/linux/mmzone.h#L671-L676):

```c
/* include/linux/mmzone.h:671 —— 三条水位线(每个 zone 各自一组) */
enum zone_watermarks {
    WMARK_MIN,      // 红线:逼近它,情况已经很紧急
    WMARK_LOW,      // 黄线:低于它,kswapd 该起床干活了
    WMARK_HIGH,     // 绿线:回收目标,回收到这就可以歇了
    NR_WMARK
};
```

每个 zone 里存着这三个值([include/linux/mmzone.h:846](../linux-6.14/include/linux/mmzone.h#L846) 的 `_watermark[NR_WMARK]`),它们由 `min_free_kbytes` 等参数算出来。三条线如何驱动三档响应:

```
   空闲页
     ▲
     │  ─ ─ ─ ─  high  ─ ─ ─ ─   ← 回收到这就停(kswapd 收工)
     │           健康区
     │  ─ ─ ─ ─  low   ─ ─ ─ ─   ← 低于这,kswapd 被唤醒,后台悄悄扫
     │           压力区
     │  ─ ─ ─ ─  min   ─ ─ ─ ─   ← 逼近这,分配者自己上手"直接回收"
     │           危险区
     │            0
```

### 第一档:低于 low → kswapd 后台打扫

当某 zone 空闲页跌破 `low` 水位,内核唤醒一个**后台守护进程 kswapd**([`mm/vmscan.c:7201-7273`](../linux-6.14/mm/vmscan.c#L7201-L7273))。它在后台默默扫描 LRU、回收页面,**直到空闲页回到 `high` 水位**才重新去睡([`prepare_kswapd_sleep`](../linux-6.14/mm/vmscan.c#L6747-L6776) 检查 `high`):

> **为什么要后台打扫?** 因为回收(尤其脏页写回、swap)是**慢且可能阻塞**的。如果在分配的快路径里同步干这事,所有进程的分配都会被拖慢。让 kswapd 在后台异步地、提前地把房间打扫到 `high`,**分配的快路径就几乎总能立刻拿到空房间**。这是用"一个常驻后台进程"换"分配延迟可控"的经典设计。

### 第二档:逼近 min → 直接回收(direct reclaim)

如果分配速度太快,kswapd 打扫跟不上,空闲页逼近 `min` 水位,内核就不再等 kswapd 了——**正在分配内存的那个进程,被迫自己动手回收**,这叫**直接回收(direct reclaim)**,入口是 [`try_to_free_pages`](../linux-6.14/mm/vmscan.c#L6514-L6555):

```c
/* mm/vmscan.c:6514 —— 分配者自己上手回收(同步、阻塞) */
unsigned long try_to_free_pages(struct zonelist *zonelist, int order,
                                gfp_t gfp_mask, nodemask_t *nodemask)
{
    struct scan_control sc = {
        .nr_to_reclaim = SWAP_CLUSTER_MAX,   // 一次回收到 32 页就尝试返回
        .gfp_mask = current_gfp_context(gfp_mask),
        .priority = DEF_PRIORITY,            // 从最宽松的优先级开始扫
        .may_writepage = !laptop_mode,
        .may_unmap = 1,
        .may_swap = 1,
    };
    /* ... */
    nr_reclaimed = do_try_to_free_pages(zonelist, &sc);   // 同步回收
    return nr_reclaimed;
}
```

> **直接回收是"自保"**:分配者等不起 kswapd 了,只能边回收边分配。代价是这次分配会明显变慢(可能要等磁盘写回/swap 完成)。但这是为了**不让系统瞬间 OOM**。`DEF_PRIORITY=12`(见 [include/linux/mmzone.h:1230](../linux-6.14/include/linux/mmzone.h#L1230))意味着初始扫描量很克制,扫不出来就把 priority 加大(扫更多)再来一轮,直到回收到 `SWAP_CLUSTER_MAX`(32 页,[include/linux/swap.h:224](../linux-6.14/include/linux/swap.h#L224))为止。

### 第三档:连直接回收都失败 → OOM

如果直接回收也腾不出足够的房间(比如所有页都被 mlock 锁死、或 swap 区也满了),分配真的无法满足。这时**最后一道防线**启动:OOM killer。

> **三档响应的递进逻辑**:从"后台异步"(kswapd,对分配者几乎无感),到"分配者同步自救"(direct reclaim,慢但能扛),再到"壮士断腕"(OOM,杀进程)。每一档都比上一档代价更重,所以只有上一档扛不住才会升级。**这是内核"用分级延缓灾难"的标准手法。**

---

## 六、OOM killer:选谁杀,这笔账怎么算

到 OOM 这一步,系统已经危在旦夕。内核的选择是:**杀掉一个进程,释放它占的所有房间,把系统从崩溃边缘拉回来。** 但"杀谁"是个要命的决定——杀错了(比如杀了数据库主进程),比 OOM 本身还糟。

### 核心算法:oom_badness——谁的"占用分"最高,谁死

Linux 用一个叫 [`oom_badness`](../linux-6.14/mm/oom_kill.c#L202-L240) 的函数给每个候选进程打分,**分最高的那个被选中**。看它算什么:

```c
/* mm/oom_kill.c:202 —— 给进程算"该被牺牲的程度" */
long oom_badness(struct task_struct *p, unsigned long totalpages)
{
    long points;
    long adj;

    /* ... 不可杀的进程(内核线程、init 等)直接排除 ... */
    adj = (long)p->signal->oom_score_adj;
    if (adj == OOM_SCORE_ADJ_MIN || /* ... */) {
        task_unlock(p);
        return LONG_MIN;
    }

    /*
     * 基线分 = 该任务占的 RAM 比例:
     * RSS(常驻页) + swap 占用 + 页表大小
     */
    points = get_mm_rss(p->mm) + get_mm_counter(p->mm, MM_SWAPENTS) +
             mm_pgtables_bytes(p->mm) / PAGE_SIZE;
    task_unlock(p);

    /* 用 oom_score_adj 调整(管理员可手动微调) */
    adj *= totalpages / 1000;
    points += adj;

    return points;
}
```

> **oom_badness 的直觉**:**谁占的房间最多,就杀谁**——因为杀它**能一次性腾出最多的房间**,最可能解 OOM。这是一个"投入产出比"最大化的贪心策略:`get_mm_rss` 是进程真正占的物理页,`MM_SWAPENTS` 是它 swap 出去的量(这些 swap 份额也是它造成的内存压力),页表本身也占内存。三项加起来,就是"这个进程对内存压力的贡献"。

> **为什么不全算"占内存"还包括 swap 和页表?** 因为 OOM 的目的是**缓解总内存压力**,而进程造成的压力不只是它的 RSS——它换出去的匿名页占了 swap 槽位,它庞大的页表也吃内存。把这些一起算,才真正反映"杀掉它能释放多少压力"。

### 人工干预旋钮:oom_score_adj

注意那行 `points += adj`。`oom_score_adj`(范围 -1000~1000)是**给管理员的手动旋钮**:你可以把关键进程(如数据库)调成 -1000,让它**永不被 OOM 选中**;把无关紧要的进程调高,优先牺牲它。这是"内核给默认贪心策略留一个逃生口,让人类可以override"的设计——因为"谁重要"这种判断,算法做不了,只有部署者知道。

### 选择与执行

打分完成后,[`select_bad_process`](../linux-6.14/mm/oom_kill.c#L365-L380) 遍历所有进程,挑出 `oom_badness` 最高那个([`oom_evaluate_task`](../linux-6.14/mm/oom_kill.c#L309-L359) 里 `points < oc->chosen_points ? 略过 : 选中`),交给 [`oom_kill_process`](../linux-6.14/mm/oom_kill.c#L1017-L1063) 发 SIGKILL。整个 OOM 路径的总入口是 [`out_of_memory`](../linux-6.14/mm/oom_kill.c#L1112-L1181)。

> **杀进程不是直接 free 它的内存**。内核发 SIGKILL,进程被正常终止,**它的所有页(堆、栈、页表、匿名页)在退出时按正常路径释放**——这套释放路径本来就是现成的。OOM killer 只是"选一个牺牲品并触发它的死亡",善后用的是已有的机制。**这种"复用已有路径,不另起炉灶"的风格,内核里随处可见。**

---

## 七、关键源码精读:`shrink_lruvec`——回收的总调度

把前面几节串起来,精读回收的总调度函数 [`shrink_lruvec`](../linux-6.14/mm/vmscan.c#L5703-L5819)。它是一个 zone/node 上 LRU 回收的"总控",把"扫多少 / 扫哪条链 / 扫够了没"全部串起来:

```c
/* mm/vmscan.c:5703 —— 回收一个 lruvec(一个 node 的 LRU 集合) */
static void shrink_lruvec(struct lruvec *lruvec, struct scan_control *sc)
{
    unsigned long nr[NR_LRU_LISTS];   /* 4 条可回收链,各扫多少 */
    /* ... */

    /* MGLRU 分支:开了多代 LRU 就走更精细的路径 */
    if (lru_gen_enabled() && !root_reclaim(sc)) {
        lru_gen_shrink_lruvec(lruvec, sc);
        return;
    }

    get_scan_count(lruvec, sc, nr);   /* ① 按 swappiness 算出每条链的扫描量 */

    /* ... */
    while (nr[LRU_INACTIVE_ANON] || nr[LRU_ACTIVE_FILE] ||
           nr[LRU_INACTIVE_FILE]) {
        for_each_evictable_lru(lru) {              /* ② 逐条链处理 */
            if (nr[lru]) {
                nr_to_scan = min(nr[lru], SWAP_CLUSTER_MAX);  /* 一批 32 页 */
                nr[lru] -= nr_to_scan;

                nr_reclaimed += shrink_list(lru, nr_to_scan, lruvec, sc);
                /* shrink_list 内部:活跃链走 shrink_active_list(老化降级),
                   非活跃链走 shrink_inactive_list(真正驱逐) */
            }
        }

        cond_resched();

        if (nr_reclaimed < nr_to_reclaim || proportional_reclaim)
            continue;                  /* ③ 没回收到目标量,继续扫 */

        break;                         /* 够了,收工 */
    }
}
```

逐段对应前面讲的设计:

- **① `get_scan_count`**:第四节说的"按 swappiness + 各类回收历史 + 代价"算出**匿名链扫几页、文件链扫几页**。这是"三类住户区别对待"在代码里的总开关。
- **② `shrink_list` 分流**:扫到**活跃链**,调 [`shrink_active_list`](../linux-6.14/mm/vmscan.c#L2074-L2167)(第三节的老化——给第二机会、把没人用的降级到非活跃);扫到**非活跃链**,调 [`shrink_inactive_list`](../linux-6.14/mm/vmscan.c#L1954-L2055) → [`shrink_folio_list`](../linux-6.14/mm/vmscan.c#L1082-L1595)(第二节的真实驱逐——干净页丢、脏页 pageout、匿名页 swap)。**"两条链各司其职"的设计,在这一个分支点上一目了然。**
- **③ 边界条件**:`SWAP_CLUSTER_MAX`(32 页)是一批的量,避免一次扫太多拖太久;`nr_to_reclaim`(直接回收时是 32 页)是回收目标,够就停。`priority` 越高(扫描越激进),`nr` 越大。**这套"小批多次、够即止"的循环,是回收在"彻底打扫"和"尽快返回"之间的折中。**

整段函数没有一个写死的"赶谁"决策——它只负责**调度**:算扫描量、分流到两条链、计数回收进度。**真正的"牺牲谁"判断,被分散到了 `shrink_active_list`(老化)和 `shrink_folio_list`(驱逐)两个更专注的函数里。** 这种"总控只管调度,细节下放"的分层,正是 mm 子系统一贯的代码组织风格。

---

## 八、章末小结

### 用大楼比喻回顾

大楼(物理 RAM)满了,物业(内核)要腾房间。它的整套回收策略,可以浓缩成三句话:

- **住户分三类,代价分三档。** 干净文件页(磁盘有原件)→ 直接赶走,几乎零成本;脏页 → 先写回磁盘再赶,要花 I/O;匿名页(磁盘没原件)→ 搬到地下室 swap,最贵。**回收时偏向便宜的文件页,但用 swappiness 调节要不要碰匿名页。**
- **牺牲谁,靠"两条链 + 第二机会"。** 不用简单 LRU(每次访问动链表太贵),而是"活跃链 + 非活跃链 + 硬件访问位"。页面要从活跃"熬"到非活跃、再在非活跃熬一轮才会被牺牲,两道闸吸收访问抖动。
- **压力分三档,逐级升级。** 空闲跌破 `low` → kswapd 后台异步打扫(分配者无感);逼近 `min` → 分配者自己直接回收(同步变慢,但能扛);都失败 → OOM killer 杀"占房最多"的进程(`oom_badness` 算 RSS+swap+页表),用最小牺牲换最大释放。

### 回扣全书主线

这本书的二分法是"**物理一侧切房间,虚拟一侧造幻觉**"。回收**横跨两侧**:它回收的"房间"是物理页(物理侧),但这些页背后既有虚拟侧的产物(进程的匿名页、VMA 兑现出来的页),也有第 11 章的 page cache(文件 I/O 的内存面)。

回收是全书"**懒**"哲学的**反面兜底**:前面所有机制——VMA 只圈地、malloc 开空头支票、page fault 才兑现、page cache 尽量占满空闲内存——都在**尽量把 RAM 用满**。可物理 RAM 终究有限,当"懒"把楼塞满、新请求拿不到房间时,回收就是那个**把已兑现的房间再收回一部分**的安全阀。没有它,overcommit 和按需分页就敢无限承诺,系统迟早崩。

所以你在主线上的位置:

```
   物理侧:房间怎么切、怎么管          ← 第 1~7 章
   虚拟侧:幻觉怎么造、怎么兑现        ← 第 8~10 章
   文件的内存面:page cache           ← 第 11 章
   ────────────────────────────────
   内存不够时:腾房间 / 搬地下室 / 杀进程 ← 第 12 章(本章)
   ────────────────────────────────
   进阶:NUMA / 大页 / 容器隔离        ← 第 13 章
```

### 想继续深入,往这儿钻

- **回收的扫描控制**:[`struct scan_control`](../linux-6.14/mm/vmscan.c) 是贯穿整个回收流程的"任务单",所有 `may_writepage`/`may_swap`/`priority`/`nr_to_reclaim` 决策都在它上面。顺着 `try_to_free_pages → do_try_to_free_pages → shrink_node → shrink_lruvec` 这条调用链走一遍,回收的全貌就清晰了。
- **MGLRU(多代 LRU)**:`mm/vmscan.c` 里 `lru_gen_shrink_lruvec` 及 `mm/vmscan.c` 中 `lru_gen_*` 系列,是新时代的回收引擎,把"两条链"细化为"多代"。想理解为什么现代内核回收更平滑,钻这里。
- **kswapd 的睡眠/唤醒**:[`kswapd_try_to_sleep`](../linux-6.14/mm/vmscan.c) 和 [`balance_pgdat`](../linux-6.14/mm/vmscan.c#L6868-L7087),看 kswapd 怎么在 `low` 和 `high` 之间循环、怎么避免自己反而拖垮系统(`PF_MEMALLOC` 标志保护它不被自己回收)。
- **水位线的计算**:[`mm/page_alloc.c`](../linux-6.14/mm/page_alloc.c) 里 `setup_per_zone_wmarks` / `__zone_watermark_ok`([page_alloc.c:3125](../linux-6.14/mm/page_alloc.c#L3125)),看 `min_free_kbytes` 怎么变成三条线、`watermark_boost` 怎么在突发压力下临时抬高警戒线。
- **OOM 的全部细节**:[`mm/oom_kill.c`](../linux-6.14/mm/oom_kill.c) 整个文件不长,`oom_badness`/`select_bad_process`/`oom_kill_process`/`out_of_memory` 一条龙,加上 memcg 的局部 OOM(第 13 章会用到),是理解"内核杀进程决策"的最佳教材。
- **观测工具**:运行期看回收在干什么——`vmstat 1` 看 `si`/`so`(swap in/out)、`pgscan_kswapd`/`pgscan_direct`;`cat /proc/zoneinfo` 看每个 zone 的三条水位线;`cat /proc/<pid>/oom_score` 看某进程的 OOM 危险度。

> 大楼的回收系统至此讲完。空闲时尽量塞满(第 11 章 page cache),满了就分三档腾房间(本章),腾不动就杀进程。最后一章,我们看这套机制在**多核服务器、大内存数据库、容器**这三个现代场景下,被逼出了哪些进阶改造。
