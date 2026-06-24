# 第三章 · rq、cfs_rq、rt_rq:运行队列

> 篇:第 1 篇 · 任务与运行队列:调度的账本(本章是本篇第 2 章)
> 主线呼应:上一章我们把"任务怎么表示"钉死了——一个任务有 `policy`/`sched_class`/`se`/`rt`/`dl`。但这些任务得有个**地方排队**等调度器来挑。这一章讲的就是这个地方:**运行队列(runqueue)**。Linux 给每个 CPU 配一个 [`struct rq`](../linux/kernel/sched/sched.h#L985)([sched.h:985](../linux/kernel/sched/sched.h#L985)),里面嵌着三个子队列——`cfs_rq`(公平,挂 EEVDF 实体)、`rt_rq`(实时,按 prio 位图+链表)、`dl_rq`(deadline,按截止期红黑树)——外加一把 `rq->lock`、一个 `rq->curr`(当前在跑)、一个 `rq->idle`(没事干时的兜底)。每个核只动自己的 `rq`,只锁自己的 `rq->lock`——这是 Linux 调度器**多核可扩展**的地基。读完本章,你就能回答:为什么是每核一个队列而不是全机一把?三种子队列各自怎么组织?`rq->lock` 这把锁到底锁住了什么?

## 核心问题

**多个任务在一个 CPU 上排队等跑,内核怎么组织这个队列?为什么每个 CPU 一个独立 `rq`、每队列一把独立 `rq->lock`?公平/实时/deadline 三种策略为什么各有各的子队列、且数据结构完全不同(红黑树 vs 位图+链表 vs 截止期红黑树)?**

读完本章你会明白:

1. `rq` 是什么:每个 CPU 一个,内含 `cfs`/`rt`/`dl` 三个子队列、`curr`/`idle`/`stop` 三个任务指针、一把 `__lock`、一组时钟字段。
2. 为什么是 per-CPU:把锁竞争消灭在数据结构里——一个核上的调度决策只动本核 `rq`,只锁本核 `rq->lock`,不同核互不干扰。
3. `cfs_rq`(公平队列):挂 `sched_entity` 的红黑树(6.9 按 `deadline` 排)+ 一堆 PELT/组调度字段。
4. `rt_rq`(实时队列):位图(`DECLARE_BITMAP(bitmap, MAX_RT_PRIO+1)`)+ 每 prio 一个链表头(`queue[MAX_RT_PRIO]`),选最高 prio 是 O(1)。
5. `dl_rq`(deadline 队列):按 `deadline` 排的红黑树 + 缓存的 `earliest_dl.curr/next`。
6. `rq->lock` 的语义:它锁住的是**本核运行队列的一致性**——入队、出队、选下一个、改 `curr`,都要持锁。

> 逃生阀:本章不展开 RT/deadline 的策略细节(第 17、18 章),只讲它们的**队列结构**;也不讲负载均衡怎么跨核拉任务(第 15 章),只讲单核视角的 `rq`。

---

## 3.1 一句话点破

> **运行队列是调度器的"候车厅"——每个 CPU 一个独立 `rq`,里面分三个隔间(cfs_rq/rt_rq/dl_rq),分别装公平、实时、deadline 三类候车人;每个 `rq` 自己一把锁,本核调度只动本核队列,不同核互不锁竞争。这是 Linux 调度器在 64 核、128 核机器上仍能线性扩展的根基——它把"锁瓶颈"消灭在了数据结构设计里。**

这是结论,不是理由。本章倒过来拆:先讲为什么是 per-CPU 队列(而不是全机一把)、再分别看 `rq`/`cfs_rq`/`rt_rq`/`dl_rq` 的结构,然后钻 `rq->lock` 的语义。

---

## 3.2 为什么是 per-CPU 运行队列

### 不这样会怎样

设想一个朴素设计:**全机一个全局 `rq`**,所有任务挤一个队列,一把全局自旋锁。

```
 朴素设计(糟糕):全机一个全局 rq

   CPU 0 ──┐
   CPU 1 ──┼──► [ 全局 rq (一个队列,一把锁) ]
   CPU 2 ──┤
   ...     │
   CPU N ──┘
```

调度器每个时钟 tick、每次唤醒、每次阻塞都要锁这个队列。一台 64 核机器,每核每秒上千次调度路径,**64 个核全在抢同一把锁**——锁竞争随核数线性恶化。这就是 2000 年代初 Linux 2.4 调度器(O(1) 之前)和大内核锁(BKL)时代的真实痛:16 核以上调度器自己就成了瓶颈,核越多反而越慢。

### 所以这样设计:每 CPU 一个独立 rq

Linux 给每个 CPU 配一个独立的 [`struct rq`](../linux/kernel/sched/sched.h#L985),声明在 [`core.c:119`](../linux/kernel/sched/core.c#L119):

```c
/* kernel/sched/core.c */
DEFINE_PER_CPU_SHARED_ALIGNED(struct rq, runqueues);   // 每 CPU 一个 rq,缓存行对齐
```

对应的访问宏在 [`sched.h:1222-1226`](../linux/kernel/sched/sched.h#L1222-L1226):

```c
/* kernel/sched/sched.h */
DECLARE_PER_CPU_SHARED_ALIGNED(struct rq, runqueues);
#define cpu_rq(cpu)   (&per_cpu(runqueues, cpu))   /* 按核号取 */
#define this_rq()     this_cpu_ptr(&runqueues)      /* 当前核的 rq(快,无锁取) */
#define task_rq(p)    cpu_rq(task_cpu(p))            /* 任务所在核的 rq */
```

`this_rq()` 用 `this_cpu_ptr`,直接读本核的 per-CPU 变量,**无锁、极快**(就是一次 per-CPU 段偏移加法)。本核调度路径绝大多数操作都用 `this_rq()`,根本不碰别的核。

```
 per-CPU rq(正确设计):每核独立队列、独立锁

   CPU 0 的 rq                  CPU 1 的 rq                  CPU 2 的 rq
   ┌──────────────────┐         ┌──────────────────┐         ┌──────────────────┐
   │ cfs (公平队列)   │         │ cfs (公平队列)   │         │ cfs (公平队列)   │
   │ rt  (实时队列)   │         │ rt  (实时队列)   │         │ rt  (实时队列)   │
   │ dl  (deadline)   │         │ dl  (deadline)   │         │ dl  (deadline)   │
   │ curr = 任务A     │         │ curr = 任务X     │         │ curr = 任务Q     │
   │ idle = idle线程  │         │ idle = idle线程  │         │ idle = idle线程  │
   │ __lock ─────┐    │         │ __lock ─────┐    │         │ __lock ─────┐    │
   └────────────│────┘         └────────────│────┘         └────────────│────┘
                │                            │                            │
        本核锁,不冲突 ──── 三把锁互不干扰 ─── ──── 只有 load_balance 偶尔跨核
```

> **不这样会怎样**:如果全机一把锁,64 核抢一把锁,锁缓存行在核间反复 invalidate(见第 11 本《同步原语》的缓存行抖动),调度吞吐随核数线性下降。per-CPU rq + 独立锁,让**正常调度路径几乎零锁竞争**——只有第 15 章的 `load_balance`(偶尔跨核拉任务)才会碰别的核的 `rq->lock`。这和第 8 本《内存分配器》的 per-CPU cache、上一本 mm 的 per-CPU pageset 是同一套思路:**把并发瓶颈消灭在数据结构设计里**。

### 但跨核怎么办:load_balance 偶尔借锁

当然,任务要在核间迁移(负载均衡、亲和性改变、CPU 热插拔),这时确实要碰别的核的 `rq`。Linux 的做法是**短时**借目标核的 `rq->lock`:

- 迁移一个任务:`set_task_cpu` + 在源/目标两个 `rq` 上各做一次 `dequeue`/`enqueue`,期间持两把 `rq->lock`(用 `double_rq_lock` 按固定顺序加锁防死锁,见 [`sched.h:2838`](../linux/kernel/sched/sched.h#L2838))。
- 这种跨核操作**不频繁**(load_balance 默认毫秒级触发一次),且持锁时间极短(就是几次指针操作),不会成为瓶颈。

跨核协调的细节留到第 15 章讲,本章只钉死一件事:**正常情况下,每核调度只动本核 `rq`、只锁本核 `rq->lock`**。这是 per-CPU 设计能 work 的前提。

---

## 3.3 struct rq:一个 CPU 运行队列的全貌

来看 `rq` 的真身([sched.h:L985-1184](../linux/kernel/sched/sched.h#L985-L1184),只摘调度器关心的核心字段):

```c
/* kernel/sched/sched.h(简化展示) */
struct rq {
    raw_spinlock_t      __lock;        // 987: 本 rq 的自旋锁(锁整个 rq 一致性)

    unsigned int        nr_running;    // 989: 本队列上所有可运行任务数(cfs+rt+dl 之和)

    struct cfs_rq       cfs;           // 1017: 公平子队列
    struct rt_rq        rt;            // 1018: 实时子队列
    struct dl_rq        dl;            // 1019: deadline 子队列

    unsigned int        nr_uninterruptible;  // 1033: 本核上 D 状态(不可中断睡眠)任务数

    struct task_struct __rcu *curr;    // 1035: 当前在跑的任务(__schedule 切的就是它)
    struct task_struct      *idle;     // 1036: 本核的 idle 线程(没事干时跑它)
    struct task_struct      *stop;     // 1037: 本核的 migration/stop 线程(特权迁移用)
    unsigned long            next_balance;  // 1038: 下次 load_balance 的时刻
    struct mm_struct        *prev_mm;       // 1039: 切换前任务的 mm(kernel thread 切回时用)

    unsigned int        clock_update_flags; // 1041
    u64                 clock;               // 1042: 本 rq 的时钟(纳秒,见下章)
    u64                 clock_task ____cacheline_aligned; // 1044: 扣除 IRQ 时间的"任务时钟"
    u64                 clock_pelt;          // 1045: PELT 时钟(第 9 章)

    atomic_t            nr_iowait;    // 1054: 本核上等 IO 的任务数(算 load 用)

#ifdef CONFIG_SMP
    struct root_domain      *rd;      // 1066: 根域(第 15 章,跨核共享的忙/闲位图等)
    struct sched_domain __rcu *sd;     // 1067: 调度域层次树根(第 14 章)
    unsigned long        cpu_capacity; // 1069: 本核算力(频率/异构 CPU)
    int                  cpu;          // 1084: 本 rq 的核号
    /* ...load_balance、active_balance、nohz 相关字段 */
#endif

#ifdef CONFIG_SCHED_HRTICK
    struct hrtimer       hrtick_timer; // 1126: hrtick 高精度定时器(下章详讲)
    ktime_t              hrtick_time;  // 1127
#endif

    /* ...统计、core scheduling、cfsb bandwidth 等 */
};
```

把字段分组理解:

| 组 | 字段 | 作用 |
|----|------|------|
| **锁** | `__lock` | 保护整个 rq 一致性 |
| **三个子队列** | `cfs`、`rt`、`dl` | 分别装公平/实时/deadline 任务(下三节细讲) |
| **当前/兜底任务** | `curr`、`idle`、`stop` | curr 在跑;idle 是没事干时的兜底;stop 是特权迁移线程 |
| **计数** | `nr_running`、`nr_uninterruptible`、`nr_iowait` | 多少在跑/在睡(D)/在等 IO |
| **时钟** | `clock`、`clock_task`、`clock_pelt` | 三套时钟(下章详讲为什么有这么多) |
| **SMP** | `rd`、`sd`、`cpu_capacity`、`cpu` | 跨核协调(第 14-16 章) |
| **hrtick** | `hrtick_timer` | 高精度单次定时器(下章) |

注意 `curr` 的类型是 `struct task_struct __rcu *`——`__rcu` 注解表示这个指针**可能被 RCU 读端并发访问**(比如 `/proc/sched_debug` 读 `rq->curr` 不持锁,靠 RCU 保护)。这是内核用 Sparse 工具做 RCU 注解检查的惯例。

> **钉死这件事**:`rq` 是一个 CPU 上调度状态的**集中账本**。本核调度路径(`__schedule`/`scheduler_tick`/`try_to_wake_up`)的第一件事几乎都是 `rq = this_rq()` 或 `rq = cpu_rq(cpu)`,然后 `rq_lock(rq, &rf)`。一切决策都围绕这个 `rq` 展开。读 `rq` 字段就像读这台 CPU 的"调度仪表盘"。

### curr、idle、stop 三个任务指针

这三个指针容易混,拎出来讲:

- **`rq->curr`**:本核**当前正在跑**的任务。`__schedule` 切换的就是它——把 `curr` 换成 `pick_next_task` 选出的 `next`。它几乎总是非 `NULL`(就算是 idle,也是个 idle 任务)。
- **`rq->idle`**:本核的 **idle 线程**。当 `cfs_rq`/`rt_rq`/`dl_rq` 都空了,`pick_next_task` 一路 fail 到 `idle_sched_class`,返回 `rq->idle`。idle 线程是个特殊的 per-CPU 内核线程(`swapper/N`),它的工作就是 `cpuidle` 进深睡、等下一个中断唤醒。
- **`rq->stop`**:本核的 **migration/stop 线程**(per-CPU,`migration/N`),归 `stop_sched_class`(最高优先级)。它是个特权线程,只在需要"不可打断"地做迁移/热插拔/CPU 亲和性强制迁移时被唤醒——因为它优先级最高,一旦唤醒立刻抢占任何任务(包括 RT),保证迁移操作迅速完成。

> **不这样会怎样**:如果 idle 不是个真正的 `task_struct` 而是个特殊值,`__schedule` 的 `pick_next_task` 就要写 `if (都没有) return IDLE_SPECIAL`,主路径分支变多。把 idle 做成一个真正的任务(虽然它的 `sched_class` 是 `idle_sched_class`),让 `pick_next_task` 统一返回 `task_struct *`,主路径无特例。这是"用一致性消灭特例"的工程美学。

---

## 3.4 cfs_rq:公平子队列

公平调度类(`fair_sched_class`)管理 `SCHED_NORMAL`/`SCHED_BATCH`/`SCHED_IDLE` 三种 policy 的任务,它们都挂在 `rq->cfs` 上。[`struct cfs_rq`](../linux/kernel/sched/sched.h#L573)([L573-678](../linux/kernel/sched/sched.h#L573-L678))的核心字段:

```c
/* kernel/sched/sched.h(简化展示) */
struct cfs_rq {
    struct load_weight   load;            // 574: 本队列总权重(所有 se 权重之和,决定时间片分配)
    unsigned int         nr_running;      // 575: 本队列上(顶层)可运行 se 数
    unsigned int         h_nr_running;    // 576: 含子组的总任务数(组调度用,"hierarchical")

    s64                  avg_vruntime;    // 580: EEVDF 的平均 vruntime(第 7 章核心)
    u64                  avg_load;        // 581

    u64                  exec_clock;      // 583
    u64                  min_vruntime;    // 584: 本队列最小的 vruntime(单调递增,新任务/迁移对齐基准)

    struct rb_root_cached tasks_timeline; // 594: 红黑树根(挂 sched_entity,按 EEVDF deadline 排)

    struct sched_entity *curr;            // 600: 本队列当前在跑的 se
    struct sched_entity *next;            // 601

    /* ... PELT 负载统计(L607-621)、组调度字段(L623-677)、CFS bandwidth throttle */
};
```

最关键的是 [`tasks_timeline`](../linux/kernel/sched/sched.h#L594):这是一棵**红黑树**,所有就绪的 `sched_entity` 都挂在上面。注意 6.9 是 **EEVDF**——红黑树按 `se->deadline`(virtual deadline)排,**不是**老 CFS 那样按 `vruntime` 排。挑下一个时,从树里挑"最早 deadline 且 eligible"的那个(第 7 章详讲 `pick_eevdf`)。

```
 cfs_rq 内部(简化):

    cfs_rq
    ├─ load (总权重)
    ├─ nr_running / h_nr_running
    ├─ min_vruntime (单调递增的基准 vruntime)
    ├─ avg_vruntime (EEVDF 用)
    └─ tasks_timeline (rb_root_cached)
            │
            ▼
        红黑树(按 se->deadline 排序)
                ╭───╮
            ╭───┤D3 │
            │   ╰───╯
        ╭───╴       ╰───╮
       ╭╴                ╰╮
     ╭─┴─╮              ╭─┴─╮
     │D1 │              │D5 │   ← D1=最早 deadline,eligible 时第一个被 pick
     ╰───╯              ╰───╯
       每个 se 都带 vlag、deadline、slice、vruntime(第 7 章)
```

`rb_root_cached` 比 `rb_root` 多了一个 `rb_leftmost` 缓存——O(1) 取最左节点(老 CFS 时是最小 vruntime,EEVDF 时是最小 deadline,但 EEVDF 还要过 eligible 检查,所以不完全是 leftmost)。这是红黑树在内核里常见的"加个缓存优化最热路径"做法。

> **CFS 残留 vs EEVDF**:6.9 里 `cfs_rq` 仍叫 cfs_rq、仍用红黑树、`vruntime`/`min_vruntime` 字段还在——这是 CFS 时代的遗产,EEVDF 没全删。但**挑选算法**已经是 EEVDF(看 eligible + deadline,不是单纯最小 vruntime)。这是为什么很多老资料讲 CFS 红黑树按 vruntime 排会让人困惑——名字没变,行为变了。第 6、7 章会专门拆这个演进。

### h_nr_running 和组调度

注意 `cfs_rq` 有两个计数:`nr_running`(本队列上**直接**挂的 se 数)和 `h_nr_running`(**层级化**(hierarchical)总数,含子组里的任务)。开启组调度后,一个组的 `se` 挂在父 `cfs_rq` 上,组里的任务挂在组自己的 `cfs_rq` 上——父 `cfs_rq` 的 `nr_running` 只数到那个组 se(算 1),但 `h_nr_running` 会下钻把组里所有任务都数上。`h_nr_running` 主要用于"判断这个 cfs_rq 还有没有可运行任务"——只要 `h_nr_running > 0`,哪怕组内还有任务,这个 cfs_rq 就不算空。

---

## 3.5 rt_rq:实时子队列

实时调度类(`rt_sched_class`)管理 `SCHED_FIFO`/`SCHED_RR`,它们挂在 `rq->rt` 上。实时任务的核心特征是**按 prio 绝对抢占**:prio 数值越小越重要,0-99 共 100 个优先级。挑选下一个时,**总是选 prio 最小的**。

为了 O(1) 选最高 prio,`rt_rq` 用的是**位图 + 链表数组**的结构([`struct rt_rq`](../linux/kernel/sched/sched.h#L691),[L691-722](../linux/kernel/sched/sched.h#L691-L722);底层 [`struct rt_prio_array`](../linux/kernel/sched/sched.h#L264),[L264-267](../linux/kernel/sched/sched.h#L264-L267)):

```c
/* kernel/sched/sched.h */
struct rt_prio_array {
    DECLARE_BITMAP(bitmap, MAX_RT_PRIO+1);     /* 100+1 位的位图,第 i 位=1 表示有 prio=i 的任务 */
    struct list_head queue[MAX_RT_PRIO];        /* 100 个链表头,queue[i] 是 prio=i 的任务链表 */
};

struct rt_rq {
    struct rt_prio_array active;                 /* 692: 位图+链表数组 */
    unsigned int         rt_nr_running;          /* 693: 本队列实时任务总数 */
    unsigned int         rr_nr_running;          /* 694: 其中 SCHED_RR 的(轮转的) */
    struct {
        int curr; /* highest queued rt task prio */   /* 697: 本队列当前最高 prio(缓存) */
#ifdef CONFIG_SMP
        int next; /* next highest */                  /* 699: 次高(迁移决策用) */
#endif
    } highest_prio;
    int                  rt_queued;              /* 708: rt_rq 是否已挂到 rq 上 */
    int                  rt_throttled;           /* 710: 是否被 throttle(第 17 章 RT throttling) */
    u64                  rt_time;                /* 711: 本周期已用 RT 时间 */
    u64                  rt_runtime;             /* 712: 本周期配额 */
    raw_spinlock_t       rt_runtime_lock;        /* 714: throttle 配额锁(嵌在 rq->lock 内) */
    /* ... */
};
```

挑选最高 prio 的实时任务,逻辑是:

1. 在 `bitmap` 里用 `find_first_bit` 找第一个置位的位——那就是最高 prio(数字最小)。`find_first_bit` 在 x86 上有 `bsf` 指令,**单周期**搞定。
2. 拿 `queue[prio]` 链表的第一个任务。

```
 rt_rq 的位图+链表(简化):

   bitmap[100+1 位]:位 i = 1 表示有 prio=i 的任务在队列
   bit:  0 1 2 ... 15 16 ... 50 ... 99 |
         0 0 0    1  0     1      0     |  ← 有 prio=15 和 prio=50 的任务

   find_first_bit → 15

   queue[15]: taskB → taskC → NULL    ← prio=15 的任务链表(FIFO 顺序)
   queue[50]: taskD → NULL            ← prio=50 的任务链表

   pick_next_task_rt:取 queue[15] 的第一个 taskB
   O(1) 选出最高 prio(位图 bsf 单指令)
```

> **为什么 RT 用位图+链表,不用红黑树?** 因为 RT 调度的核心是"绝对按 prio 抢占",prio 取值范围固定且小(0-99,100 个)。位图把这 100 个桶扁平摊开,`find_first_bit` 一条指令定位最高 prio 桶,比红黑树 O(log n) 还快。这是 2002 年 Ingo Molnar 的 O(1) 调度器遗产,Linux 至今沿用。红黑树适合"按可比较的连续值排序、范围查询"的场景(公平调度的 vruntime/deadline),不适合"100 个固定桶"的 RT。

### RT throttling 的配额锁

注意 `rt_rq` 里 `rt_runtime_lock` 这个**嵌套锁**——它单独保护 `rt_time`/`rt_runtime`(RT throttling 的配额)。注释明说 `/* Nests inside the rq lock: */`:意思是持 `rq->lock` 时可以再拿 `rt_runtime_lock`,顺序固定(不能反过来)。为什么要单独一把?因为 RT throttling 的配额检查可能跨核(一个核 RT 超额时,会从别的核"借"配额),这个跨核操作不想每次都锁整个 `rq`(太重),所以另开一把细粒度锁只保护配额字段。第 17 章会详讲。

---

## 3.6 dl_rq:deadline 子队列

deadline 调度类(`dl_sched_class`)管理 `SCHED_DEADLINE`——这是 Linux 里优先级最高的"真正的"调度类(stop 是特例不算),采用 **EDF(Earliest Deadline First)+ CBS(Constant Bandwidth Server)** 算法。挂在 `rq->dl` 上。[`struct dl_rq`](../linux/kernel/sched/sched.h#L730)([L730-755](../linux/kernel/sched/sched.h#L730-L755)):

```c
/* kernel/sched/sched.h(简化展示) */
struct dl_rq {
    struct rb_root_cached root;            // 732: 红黑树,按 deadline 排(EDF)
    unsigned int          dl_nr_running;   // 734
#ifdef CONFIG_SMP
    struct {
        u64 curr;    /* 744: 本队列最早 deadline(当前在跑的) */
        u64 next;    /* 745: 次早 deadline(用于迁移决策) */
    } earliest_dl;
    int overloaded;                        // 748: 是否过载(有任务可被推走)
    /* ... */
#endif
};
```

和 cfs_rq 一样是红黑树,但**排序键不同**:`dl_rq` 按**绝对 deadline**(任务的 `dl_se->deadline`)排,挑最左(最早截止)。EDF 的直觉:**谁最该交差了谁先跑**。注意 cfs_rq 也按 deadline 排,但那是 EEVDF 的 **virtual** deadline(虚拟的、按权重算的);dl_rq 的 deadline 是用户用 `sched_setscheduler(SCHED_DEADLINE, {.sched_deadline=..., sched_runtime=...})` 显式给的**真实**截止期。

`earliest_dl.curr/next` 是缓存:存本队列最早和次早 deadline。`curr` 用于"判断本核是否在跑最紧的 deadline 任务",`next` 用于负载均衡时"判断是否有过载任务可推走"。这两个缓存避免每次负载均衡都遍历红黑树。

> **deadline 调度为什么优先级最高?** 因为它**带硬性保证**:`SCHED_DEADLINE` 的任务用 CBS 算法预留了带宽(`runtime`/`period`),内核在 `sched_setscheduler` 时会做**可调度性测试**(admission control)——保证所有 deadline 任务的利用率之和不超过 1(或某个上限),否则拒绝创建。这样通过测试的 deadline 任务,内核能保证它们的 deadline 不会被错过(EDF 在单核下最优)。所以它该比 RT(没有 admission control,可能超载)还高。第 18 章详讲。

---

## 3.7 rq->lock:这把锁到底锁住了什么

现在把三个子队列合起来看。`rq->lock`(字段名是 `__lock`,`rq_lock` 是包装)是本核运行队列的**总一致性锁**。它锁住的,是**这个 CPU 上所有跟运行队列有关的状态变化**:

| 操作 | 持锁 |
|------|------|
| 入队(`enqueue_task`)、出队(`dequeue_task`) | 持 `rq->lock` |
| 选下一个(`pick_next_task`)、设 `curr` | 持 `rq->lock` |
| 改 `nr_running`、PELT 更新 | 持 `rq->lock` |
| tick 里 `task_tick`、`update_curr` | 持 `rq->lock` |
| 唤醒路径最终入队(`ttwu_queue`) | 持目标 `rq->lock` |
| 负载均衡跨核拉任务 | 持**两把** `rq->lock`(源+目标,`double_rq_lock`) |

访问 `rq` 的助手函数(都在 [`sched.h:L1381-1750`](../linux/kernel/sched/sched.h#L1381-L1750)):

```c
/* kernel/sched/sched.h(摘) */
void raw_spin_rq_lock_nested(struct rq *rq, int subclass);
static inline void raw_spin_rq_lock(struct rq *rq) { raw_spin_rq_lock_nested(rq, 0); }
static inline void raw_spin_rq_lock_irq(struct rq *rq) { local_irq_disable(); raw_spin_rq_lock(rq); }

/* rq_lock 还要存一下 rq_flags(记中断状态、时钟更新标志等) */
static inline void rq_lock(struct rq *rq, struct rq_flags *rf) {
    raw_spin_rq_lock(rq);
    /* ... 记 rf->flags 等 */
}
```

注意一个细节:`rq->lock` 在不同上下文用不同的"包装":

- `raw_spin_rq_lock`:只拿锁,不关中断(调用者已知中断关闭)。
- `raw_spin_rq_lock_irq`:拿锁 + 关中断(最常见,tick/wakeup 路径)。
- `raw_spin_rq_lock_irqsave`:拿锁 + 关中断 + 保存中断状态(嵌套调用时用)。
- `rq_lock(rq, &rf)`:拿锁 + 填充 `rq_flags`(调度器内部用,会记下时钟更新标志等)。

为什么这么多种?因为 `rq->lock` 是个 raw spinlock(不允许睡眠、不允许嵌套自旋锁 deadlock),且调度路径可能在**中断上下文**(timer 中断里调 `scheduler_tick`)或**进程上下文**(`schedule()`/`try_to_wake_up`)被调用,中断状态不同,拿锁方式必须匹配——这是内核 spinlock 用的基本功(见第 11 本《同步原语》的 spinlock 章节)。

### 双 rq 锁:跨核迁移时的死锁预防

跨核迁移要同时持两把 `rq->lock`(源和目标),见 [`double_rq_lock`](../linux/kernel/sched/sched.h#L2838)([L2838-2850](../linux/kernel/sched/sched.h#L2838-L2850))。为了防死锁(两个核同时 `double_rq_lock(0, 1)` 和 `double_rq_lock(1, 0)`),内核按**固定顺序**加锁——按 `rq` 的某种全序(地址序或核号序)。`__rq_lockp` 取锁指针、`rq_order_less` 比较顺序,见 [`sched.h:2647-2770`](../linux/kernel/sched/sched.h#L2647-L2770) 一带的注释和实现。这是多核加锁的常识,本书第 15 章负载均衡时会再见到它。

### lock 和 RCU 的分工

注意 `rq->curr` 是 `__rcu` 注解——它**可以**被 RCU 读端无锁访问(比如 `/proc/sched_debug` 显示当前任务)。但**改** `rq->curr`(在 `context_switch` 里)必须持 `rq->lock`。这是内核典型的"读走 RCU、写持锁"模式——RCU 让观测路径(慢、不频繁)不必抢锁,而热路径(写)有锁保护一致性。第 11 本《同步原语》RCU 章节详讲这个模式。

---

## 3.8 技巧精解

本章最硬核的工程技巧有两条:per-CPU `rq` + 独立锁的可扩展性设计,和 RT 队列的**位图+链表** O(1) 选 prio。

### 技巧一:per-CPU rq + 独立 rq->lock——可扩展性的地基

#### 朴素地写会撞什么墙

前面 3.2 节说过:全机一个全局 `rq` + 一把全局锁,64 核线性恶化。这不仅是"理论上的"问题——是 Linux 2.4 时代真实发生过、被 Ingo Molnar 的 O(1) 调度器(2.6 起)解决掉的痛点。O(1) 调度器的两个核心创新就是:**per-CPU runqueue** 和 **O(1) 的优先级位图**(RT 队列用的就是这套)。

#### 妙在哪:把锁竞争消灭在结构里

per-CPU `rq` 不是"加一层锁的优化",是**重新设计数据结构**让锁竞争消失。核心洞察是:**调度决策有天然的本核局部性**——一个核上选下一个、tick、唤醒,绝大多数只动本核队列。把队列 per-CPU 化,本核操作只锁本核锁,**默认零跨核协调**。

锁的粒度也要细:

- `rq->lock`:保护整个 `rq` 一致性(本核调度路径用)。
- `rt_rq->rt_runtime_lock`:嵌套锁,只保护 RT throttle 配额(跨核借配额用,不想锁整个 rq)。
- `cfs_rq->removed.lock`(在 [`sched.h:L615-621`](../linux/kernel/sched/sched.h#L615-L621)):PELT 的 removed 队列锁,跨核移除负载用。

这种"分层次细粒度锁"是内核多核代码的通用套路——把高频本核操作用大锁保护(但因为 per-CPU 化、实际不竞争),把低频跨核操作用更细的小锁保护(避免不必要地牵动大锁)。

#### 反面对比

如果只优化锁本身(比如用更快的 qspinlock,见第 11 本)而不 per-CPU 化,64 核抢一把锁,缓存行在核间反复 bounce,即使锁本身再快,总吞吐仍随核数下降。**结构设计 > 锁优化**:per-CPU 化是治本,锁优化是治标。Linux 调度器从 O(1) 起就钉死了 per-CPU rq,沿用至今(EEVDF 改的是挑选算法,不是队列结构)。

### 技巧二:RT 队列的位图+链表——O(1) 选最高 prio

#### 朴素地写会撞什么墙

100 个 RT 优先级,选最高的那个。朴素做法:

- 用一个链表挂所有 RT 任务,挑的时候遍历找最小 prio:**O(n)**,任务多了慢。
- 用红黑树按 prio 排:**O(log n)**,但 RT prio 只有 100 个固定桶,红黑树杀鸡用牛刀,且每次入队出队要旋转树。
- 用堆:**O(log n)**,同上,overkill。

#### 所以 RT 用位图+链表

见 3.5 节。妙在:

1. **位图把 100 个桶扁平摊开**:每个桶对应一个 prio,位 i = 1 表示有 prio=i 的任务。
2. **`find_first_bit` 单指令定位**:x86 的 `bsf`、ARM 的 `clz` 都能在**一个时钟周期**内找到第一个置位的位。
3. **链表数组处理同 prio 内的 FIFO**:`queue[prio]` 是个链表,同 prio 任务按 FIFO 顺序(`SCHED_FIFO`)或轮转(`SCHED_RR`,加个时间片)。

整体入队、出队、挑下一个都是 **O(1)**(准确说挑下一个是 O(位图字数/字长),100 位 = 2 个 64 位字,常数级)。这就是 Ingo Molnar 的 O(1) 调度器的"O(1)"出处——不管队列里有多少任务,选下一个是常数时间。

> **反面对比**:如果用红黑树,100 万个 RT 任务(理论上)选下一个要 O(log 1e6) ≈ 20 次比较 + 树旋转。位图+链表只要 1 次 `find_first_bit`。更重要的是,**红黑树的旋转会改节点指针**,在多核并发(虽然 RT 队列有 rq->lock 保护)下 cache 抖动大;位图操作是原子的、定点的,cache 友好。

> **钉死这件事**:RT 的位图+链表是 Linux 调度器的招牌结构之一,展示了"**根据数据的真实分布选数据结构**":RT prio 是 100 个固定桶,就用扁平位图,而不是无脑用红黑树。这是内核工程师的工程审美——**最合适的数据结构胜过最通用的数据结构**。cfs_rq 用红黑树(连续可比较值),rt_rq 用位图(固定桶),dl_rq 用红黑树(连续 deadline)——各有各的理由,不是"统一用一种树"。

---

## 章末小结

这一章把"运行队列怎么组织"钉死了。要点:

1. **per-CPU `rq`**:每核一个独立 `rq`,内含 `cfs`/`rt`/`dl` 三个子队列、`curr`/`idle`/`stop` 三个任务指针、一把 `__lock`。本核调度只动本核 `rq`、只锁本核 `rq->lock`——把锁竞争消灭在结构里。
2. **三种子队列各有数据结构**:`cfs_rq` 用红黑树(EEVDF 按 virtual deadline 排)、`rt_rq` 用位图+链表(O(1) 选最高 prio)、`dl_rq` 用红黑树(按绝对 deadline 排,EDF)。
3. **`rq->lock` 锁住本核一致性**:入队、出队、选下一个、改 curr、tick,都要持锁。跨核迁移用 `double_rq_lock` 按固定顺序加两把锁。
4. **RCU 分工**:`rq->curr` 等可被 RCU 读端无锁访问(观测路径),写持锁。

本章服务二分法的**支撑**面——它是调度器的"候车厅"基础设施。下一章讲调度器的心跳:`sched_clock`、`scheduler_tick`、`hrtick`——没有时钟,调度器不知道时间片该不该到点、不知道现在该不该均衡。

### 五个"为什么"清单

1. **为什么每 CPU 一个 `rq`,不是全机一个?** 全机一把锁,64 核线性恶化(2.4 时代的痛)。per-CPU rq 让本核调度只动本核锁,默认零跨核竞争。只在 load_balance 偶尔跨核借锁。
2. **`rq->curr`、`rq->idle`、`rq->stop` 三者什么关系?** `curr` 是当前在跑的(几乎总非 NULL);`idle` 是没事干时的兜底(per-CPU swapper/N 线程);`stop` 是特权迁移线程(per-CPU migration/N),优先级最高,只在迁移/热插拔时被唤醒。
3. **`cfs_rq`/`rt_rq`/`dl_rq` 为什么数据结构不一样?** 因为它们的挑选规则不同:公平按 virtual deadline(EEVDF,连续值 → 红黑树);RT 按 prio(100 个固定桶 → 位图+链表 O(1));deadline 按绝对截止期(连续 → 红黑树 EDF)。根据数据真实分布选结构,不无脑统一。
4. **`rq->lock` 到底锁什么?** 锁整个本核运行队列的一致性:入队/出队/选下一个/改 curr/tick 都要持锁。跨核迁移时持两把(`double_rq_lock` 按固定顺序防死锁)。
5. **RT 的位图+链表为什么是 O(1)?** 100 个 prio 桶扁平摊开成位图,`find_first_bit` 在 x86 上是 `bsf` 单指令;同 prio 任务挂链表 FIFO。不管队列多少任务,选下一个是常数时间。这是 Ingo Molnar O(1) 调度器的招牌。

### 想继续深入往哪钻

- 本章讲的结构,详见 [`kernel/sched/sched.h`](../linux/kernel/sched/sched.h#L985):`struct rq`(L985-1184)、`struct cfs_rq`(L573-678)、`struct rt_rq`(L691-722)、`struct dl_rq`(L730-755)、`struct rt_prio_array`(L264-267)。
- per-CPU `rq` 的定义在 [`kernel/sched/core.c:119`](../linux/kernel/sched/core.c#L119);访问宏在 [`sched.h:1222-1226`](../linux/kernel/sched/sched.h#L1222-L1226)。
- 想观测实际队列:`cat /proc/sched_debug` 会列出每个 CPU 的 `cfs_rq`/`rt_rq`/`dl_rq` 状态、`nr_running`、各 `sched_entity` 的 vruntime/deadline/lag 等(附录 B 详讲)。
- 锁的助手函数在 [`sched.h:1381-1750`](../linux/kernel/sched/sched.h#L1381-L1750);`double_rq_lock` 在 [`sched.h:2838`](../linux/kernel/sched/sched.h#L2838)。
- 跨核协调的 `root_domain`/`sched_domain` 字段留在第 14-16 章展开。

### 引出下一章

队列搭好了,但调度器要靠**时钟**才能知道"时间片到没到"、"该不该负载均衡"、"现在该不该抢"。下一章讲调度器的心跳:纳秒级的 `sched_clock`、定期的 `scheduler_tick`、高精度的 `hrtick`——为什么有这么多时钟、它们各自管什么。
