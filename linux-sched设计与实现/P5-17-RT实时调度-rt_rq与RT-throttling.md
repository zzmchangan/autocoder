# 第十七(五)章 · RT 实时调度:rt_rq 与 RT throttling

> 篇:第 5 篇 · 实时与特殊调度类
> 主线呼应:前面四篇讲的 EEVDF、抢占、负载均衡,都默认任务是 `SCHED_NORMAL`——也就是普通进程。但内核里还有一类**绝不能和普通任务"公平"地分 CPU 的任务**:机械臂控制、音频采样、股票撮合、内核自身的 `migration/n`(迁移线程)。它们的"延迟"不是性能问题,而是**正确性问题**——晚 10 毫秒,机械臂可能撞墙,音频卡顿,撮合丢单。Linux 给这类任务专门准备了一个调度类 `rt_sched_class` 和两条策略 `SCHED_FIFO`/`SCHED_RR`,**按优先级绝对抢占**普通任务。但"绝对抢占"是个危险的特权——一个 `while(1);` 的实时死循环能挂死整机。于是内核又加了一道**保险丝:RT throttling**,默认把实时任务限在 95% CPU,留 5% 给系统自救。这一章讲清 `rt_rq` 的"位图+队列"如何 O(1) 选最高优先级,以及 RT throttling 如何在"宁可丢实时保障,也要保住系统可用"间权衡。

## 核心问题

**实时任务凭什么"绝对抢占"普通任务?`rt_rq` 怎么在 O(1) 时间内选最高优先级?RT throttling 这道保险丝为什么是 95% 而不是 100%?它怎么在带宽到期、补充、再次到期之间循环?**

读完本章你会明白:

1. `SCHED_FIFO`/`SCHED_RR` 两条策略的差别(一个没时间片、一个轮转),以及它们为什么"按 prio 绝对抢占"。
2. `rt_rq` 的数据结构:**位图 + 100 条链表**,让选最高优先级变成一次位扫描——这就是 Linux 早期 O(1) 调度器(2.6 起)留下的招牌技巧。
3. RT throttling 三件套:`rt_bandwidth`(period/runtime 配额)+ `rt_rq->rt_time`(累计)+ 周期补充定时器;默认 1 秒周期里允许跑 0.95 秒,超了就 throttle 整个 `rt_rq`。
4. SMP 上实时任务的"反向均衡":不是把任务从忙核拉到闲核,而是把**高优先级任务从忙核推到闲核**——靠 `cpupri` 全局位图找最低优先级的 CPU。
5. 为什么 RT throttling 是"95% 而不是 100%":留出 5% 的 CPU 给系统管理(卸载 throttle、跑迁移线程、回写日志),否则一个失控 RT 任务能让整机永久锁死。

> 逃生阀:如果你只想要"RT 任务怎么调度"的直觉,记住两句:**① 优先级数字越小越重要(0 最猛、99 最弱),绝对抢占普通任务;② 任何 RT 任务的 CPU 占比默认被钉死在 95%,超出整个 rt_rq 就被冻结,等下一个周期补充。** 后面细节都是这两句的展开。

---

## 17.1 一句话点破

> **`SCHED_FIFO`/`SCHED_RR` 任务按 prio(0-99)绝对抢占普通任务;`rt_rq` 用一个 100 位位图 + 100 条链表实现"选最高 prio"的 O(1);RT throttling 在 1 秒周期里把实时任务的总 CPU 占比钉在 95%,超了就 throttle 整个 rt_rq,留 5% 给系统自救——这是"宁可让实时保障失效,也不让整机挂死"的最后一道防线。**

这是结论,不是理由。本章倒过来拆:先看 RT 任务的"绝对抢占"是怎么回事,再看 `rt_rq` 怎么用位图 O(1) 选下一个,然后看 SMP 上 RT 的"推"模型,最后看 RT throttling 这道保险丝。

---

## 17.2 RT 任务:凭什么绝对抢占普通任务

普通任务用 EEVDF,所有任务在一个 `cfs_rq` 里"公平"地按权重分 CPU。但有些任务**不能这样分**。考虑:

- 一个机械臂控制任务,要求每 5 毫秒做一次伺服运算,晚了机械臂就抖动。
- 一个音频采集任务,要求每 10 毫秒读一次声卡缓冲,晚了就丢样本。
- 内核的 `migration/n` 线程,接到 `stop_one_cpu` 后必须**立刻**切走目标 CPU 上的任务,否则迁移指令就要等一个调度延迟。

这些任务的需求是**确定性(determinism)**:必须在固定时间内拿到 CPU,不能被任何"公平"理由挤掉。如果它们和编译任务一起进 `cfs_rq`,编译任务可以靠权重把它挤到下个时间片——延迟毫无保障。

> **不这样会怎样**:如果把控制任务当普通任务,它的"最坏响应时间"等于"系统所有任务的最大调度延迟",这个值在繁忙机器上能到几十毫秒甚至上百毫秒。机械臂控制用不了,音频无法播放,实时交易系统不可用。

Linux 的做法:**给这类任务单独的调度类**——[`rt_sched_class`](../linux/kernel/sched/rt.c#L2648)([rt.c:2648](../linux/kernel/sched/rt.c#L2648)),两条策略:

- **`SCHED_FIFO`**:先进先出,任务一旦被选中就**一直跑**,直到它自己阻塞(`sleep`/`wait`)、被更高优先级的 RT 任务抢占、或者主动 `sched_yield`。**没有时间片**。
- **`SCHED_RR`**:轮转(round-robin),任务按 prio 排队,每个任务分一个时间片(默认 100 毫秒,见 [`RR_TIMESLICE`](../linux/include/linux/sched/rt.h#L61) = `100 * HZ / 1000`),时间片耗尽就排到同优先级队列末尾,让同优先级的兄弟轮。**只有同优先级才轮,不同优先级依然是高优先级绝对压制**。

两条策略的"优先级"是用户给的 `sched_priority`(0-99,数字越小越重要,通过 `chrt -f 80 ./task` 设 FIFO 优先级 80)。内核把这个用户优先级换算成 `p->prio`(也 0-99),`prio` 越小越优先。普通任务的 `prio` 在 100-139(`MAX_RT_PRIO`=100,见 [`include/linux/sched/prio.h`](../linux/include/linux/sched/prio.h#L16)),**永远比任何 RT 任务大**——所以 RT 任务一上 rq,普通任务立刻被挤走。

```
 内核 prio 体系(简化):

   prio 范围        类型                含义
   ──────────────────────────────────────────────────────────
   0  ──── 99       SCHED_FIFO/RR       实时,数字越小越优先(0 最猛)
   100             SCHED_NORMAL        nice=−20(最高的普通任务)
   120             SCHED_NORMAL        nice=0(默认)
   139             SCHED_NORMAL        nice=+19(最低的普通任务)

  关键:任何 RT 任务(≤99)绝对优先于任何普通任务(≥100)
```

[`__pick_next_task`](../linux/kernel/sched/core.c#L6016)([core.c:6016](../linux/kernel/sched/core.c#L6016)) 里 `for_each_class(class)` 按 stop > dl > rt > fair > idle 的顺序遍历,只要 `rt_sched_class->pick_next_task` 返回非 NULL,`fair_sched_class` 根本不会被问到。这就是"绝对抢占"在调度类链上的落实——**RT 在 fair 之前**。

> **钉死这件事**:RT 的"绝对抢占"不是"打断别人插队"(那叫 preempt),而是"挑选时根本不看你普通任务"——调度类链按优先级遍历,RT 有可运行任务时,fair 类的 `pick_next_task_fair` 永远轮不到调用。这是 [第 2 章](P1-02-task_struct与sched_entity-任务怎么表示.md)讲的 `sched_class` 多态的真实用武之地:不同调度策略"选下一个"的算法不同,RT 选最高 prio,EEVDF 选最早 deadline,各走各的,共用同一套遍历框架。

---

## 17.3 rt_rq:位图 + 100 条链表,O(1) 选最高优先级

RT 任务"按 prio 选"听起来简单——找 prio 最小的那个任务。但朴素地写,会是什么样?

```c
/* 朴素的、糟糕的写法(示意,非源码) */
struct task_struct *pick_next_rt(struct rt_rq *rt_rq) {
    struct task_struct *best = NULL;
    int best_prio = MAX_PRIO;
    /* 遍历所有 RT 任务,找 prio 最小的 */
    list_for_each_entry(p, &rt_rq->tasks, rt_tasks) {
        if (p->prio < best_prio) {
            best_prio = p->prio;
            best = p;
        }
    }
    return best;
}
```

> **不这样会怎样**:这是 O(N) 的——`rt_rq` 上挂几千个 RT 任务(实时音视频系统、电信网关常见),每次 `pick_next` 都要扫一遍,频繁抢占下这个开销吃 CPU 吃到惊人。而且 `rt_rq` 还要做"同优先级 FIFO 入队、RR 出队"——朴素链表需要每次都维护"按 prio 分组的子队列"。

Linux 用的是 2002 年 Ingo Molnar 的 O(1) 调度器留下的招牌数据结构:**位图 + prio 数组**。看 [`struct rt_prio_array`](../linux/kernel/sched/sched.h#L264)([sched.h:264](../linux/kernel/sched/sched.h#L264)):

```c
struct rt_prio_array {
    DECLARE_BITMAP(bitmap, MAX_RT_PRIO+1); /* include 1 bit for delimiter */
    struct list_head queue[MAX_RT_PRIO];
};
```

两个数组,一个 100 位的位图 + 一个 100 条 `list_head` 的数组。意义是:

- 位图第 `i` 位为 1,表示 prio `i` 的队列**非空**(有任务)。
- `queue[i]` 是 prio `i` 的所有 RT 任务的双向链表(同优先级 FIFO 排队)。

`rt_rq` 里嵌的就是这个:

```c
struct rt_rq {                                   // sched.h:691
    struct rt_prio_array active;                 // 位图 + 100 队列
    unsigned int      rt_nr_running;             // 本队列任务数
    unsigned int      rr_nr_running;             // 其中 RR 任务数
    struct {
        int curr; /* highest queued rt task prio */
        int next; /* next highest */             // SMP 用
    } highest_prio;
    int      overloaded;                         // SMP push 用
    struct plist_head pushable_tasks;            // SMP push 候选
    int      rt_queued;                          // 是否在 rq 顶层
    int      rt_throttled;                       // 是否被 throttle
    u64      rt_time;                            // 累计运行时间(throttle 用)
    u64      rt_runtime;                         // 本周期配额
    raw_spinlock_t rt_runtime_lock;
    ...
};
```

入队时,看 [`__enqueue_rt_entity`](../linux/kernel/sched/rt.c#L1375)([rt.c:1375](../linux/kernel/sched/rt.c#L1375)):

```c
static void __enqueue_rt_entity(struct sched_rt_entity *rt_se, unsigned int flags)
{
    struct rt_rq *rt_rq = rt_rq_of_se(rt_se);
    struct rt_prio_array *array = &rt_rq->active;
    ...
    struct list_head *queue = array->queue + rt_se_prio(rt_se);  // 按 prio 选链表

    if (group_rq && (rt_rq_throttled(group_rq) || !group_rq->rt_nr_running)) {
        ...                                           // 组被 throttle 时不入队
        return;
    }

    if (move_entity(flags)) {
        ...
        if (flags & ENQUEUE_HEAD)
            list_add(&rt_se->run_list, queue);       // FIFO 唤醒插头
        else
            list_add_tail(&rt_se->run_list, queue);  // 否则插尾

        __set_bit(rt_se_prio(rt_se), array->bitmap); // ★ 位图标 1
        rt_se->on_list = 1;
    }
    rt_se->on_rq = 1;
    inc_rt_tasks(rt_se, rt_rq);                      // 维护 nr_running、highest_prio
}
```

关键就一行 `__set_bit(rt_se_prio(rt_se), array->bitmap)`——**位图的第 prio 位被点亮**。

出队时,在 [`__delist_rt_entity`](../linux/kernel/sched/rt.c#L1263)([rt.c:1263](../linux/kernel/sched/rt.c#L1263)) 看到:同 prio 链表空了,`__clear_bit(rt_se_prio(rt_se), array->bitmap)` 清掉对应位。

选下一个时,看 [`pick_next_rt_entity`](../linux/kernel/sched/rt.c#L1714)([rt.c:1714](../linux/kernel/sched/rt.c#L1714)):

```c
static struct sched_rt_entity *pick_next_rt_entity(struct rt_rq *rt_rq)
{
    struct rt_prio_array *array = &rt_rq->active;
    struct sched_rt_entity *next = NULL;
    struct list_head *queue;
    int idx;

    idx = sched_find_first_bit(array->bitmap);    // ★ 位扫描,找第一个置 1 的位
    BUG_ON(idx >= MAX_RT_PRIO);

    queue = array->queue + idx;                    // 取该 prio 的链表
    ...
    next = list_entry(queue->next, struct sched_rt_entity, run_list);
    return next;
}
```

整个"选最高优先级"的操作 = **一次位图扫描(`sched_find_first_bit`)+ 一次链表头取下一个**。位扫描在 x86 上是单条 `bsf` 指令(bit scan forward,见 `arch/x86/include/asm/bitops.h`,未 sparse clone),100 位的位图最多扫两个 64 位字,**常数时间**。

```
 rt_prio_array:位图 + 100 条链表(简化)

   bitmap[0..99]:     0 1 0 0 0 0 0 0 ... 0 0   ← 只有 prio 1 有任务
                      ↑
                      sched_find_first_bit 扫到第 1 位
                      → idx = 1
   queue[0]:          (空)
   queue[1]:          task_A ↔ task_B ↔ task_C  (同 prio FIFO 排队)
   queue[2..99]:      (空)

  pick_next:位扫描 → queue[1] → 取链头 task_A
  入队 prio=1 的 task_D:__set_bit(1) + list_add_tail → 接到 task_C 后
  出队 task_A:链表只剩 B,C → 还非空,位图不动
  出队 task_C:链表空了 → __clear_bit(1)
```

> **反面对比**:朴素链表选最高 prio 是 O(N),几千个 RT 任务时每次 `pick_next` 都要扫全部。位图+链表把它降到 O(1)(单条 CPU 指令的位扫描 + O(1) 链表操作)。这就是为什么 Linux 2002 年的 O(1) 调度器能告别 O(N) 时代——这个数据结构沿用至今(EEVDF 时代仍是 RT 的招牌)。位图扫描的另一妙处:**"找第一个 1"在硬件上原生支持**,x86 的 `bsf`、ARM 的 `clz`,任何架构的 `find_first_bit` 都是常数时间,不需要任何循环展开。

---

## 17.4 SCHED_RR 的时间片:每 tick 减一,归零轮转

`SCHED_FIFO` 没时间片,一直跑直到自愿让出或被更高 prio 抢占。但同 prio 的几个 `SCHED_RR` 任务要"轮"——靠时间片。看 [`task_tick_rt`](../linux/kernel/sched/rt.c#L2588)([rt.c:2588](../linux/kernel/sched/rt.c#L2588)):

```c
static void task_tick_rt(struct rq *rq, struct task_struct *p, int queued)
{
    struct sched_rt_entity *rt_se = &p->rt;

    update_curr_rt(rq);                            // 记账 + 检查 throttle
    update_rt_rq_load_avg(rq_clock_pelt(rq), rq, 1);
    watchdog(rq, p);                               // 防 RT 死锁的看门狗

    /* RR tasks need a special form of timeslice management.
     * FIFO tasks have no timeslices. */
    if (p->policy != SCHED_RR)
        return;                                    // FIFO 不动时间片

    if (--p->rt.time_slice)                        // ★ 时间片 -1,没归零就继续
        return;

    p->rt.time_slice = sched_rr_timeslice;         // 归零,重置

    /* Requeue to the end of queue if we (and all of our ancestors) are not
     * the only element on the queue */
    for_each_sched_rt_entity(rt_se) {
        if (rt_se->run_list.prev != rt_se->run_list.next) {  // 队列不止我一个
            requeue_task_rt(rq, p, 0);             // 排到队尾
            resched_curr(rq);                      // 标记需要重调度
            return;
        }
    }
}
```

逻辑简单到漂亮:

1. 每个 tick(`scheduler_tick` 调过来)给当前 RT 任务 `--p->rt.time_slice`。
2. 没归零就 `return`,继续跑。
3. 归零了,把 `time_slice` 重置成 [`sched_rr_timeslice`](../linux/kernel/sched/rt.c#L7) (= [`RR_TIMESLICE`](../linux/include/linux/sched/rt.h#L61) = `100 * HZ / 1000`,即 100 毫秒)。
4. 如果**同 prio 队列还有其他任务**(链表 prev ≠ next,说明不是只有自己),`requeue_task_rt` 把自己挪到队尾,`resched_curr` 触发重调度——下一个 tick 会从队头选另一个同 prio 任务。
5. 如果同 prio 只有自己,什么也不做——继续跑完下一个时间片。

> **钉死这件事**:`SCHED_RR` 的时间片轮转**只在同 prio 内发生**,高 prio 不会因为"轮到我了"就让位——这正是 prio 的绝对优先含义。`SCHED_FIFO` 直接不进入这个分支(line 2601 `return`),根本没时间片概念。两者的差别只在"同优先级怎么办":FIFO 是"谁先进谁一直跑直到自愿让",RR 是"同优先级轮着跑"。

100 毫秒的默认值是 `RR_TIMESLICE`,可通过 `/proc/sys/kernel/sched_rr_timeslice_ms` 调(单位毫秒,见 rt.c 的 sysctl 表 [`sysctl_sched_rr_timeslice`](../linux/kernel/sched/rt.c#L28))。注意它和 EEVDF 的动态时间片(几毫秒,见 [第 10 章](P2-10-EEVDF的pick_next_fair与时间片.md))完全不同——RT 任务追求确定性,固定值反而好预测。

---

## 17.5 SMP 上 RT 的反向均衡:push 而非 pull

[第 4 篇](P4-15-load_balance-周期均衡与idle均衡.md)讲普通任务的 `load_balance` 是**pull 模型**——闲核/周期定时器主动从忙核拉任务。RT 任务不一样,**不是均衡负载,是均衡优先级**:目标是"高优先级的 RT 任务别堆在一个核上挤掉别人,而其他核闲着"。

具体两件事:

- **push**(推送):一个核上刚唤醒或刚入队了一个高 prio RT 任务,但当前核在跑更高 prio 的任务——这个新任务应该被**推到别的核**上去立刻跑,而不是排队等。这是 [`push_rt_task`](../linux/kernel/sched/rt.c)(在 [`balance_rt`](../linux/kernel/sched/rt.c#L1642) 和 `task_woken_rt` 触发)做的事。
- **pull**(拉取):一个核将要 idle,可以主动从别的核拉 RT 任务过来。这是 [`pull_rt_task`](../linux/kernel/sched/rt.c)(在 `balance_rt` 里调,见 rt.c:1652)做的事。

push 是主要的(几乎每次入队都考虑),pull 是辅助的(只在 `balance_rt` 时机)。两者都要回答一个核心问题:**"哪个 CPU 上当前跑的 RT 优先级最低(或 idle),适合接收我的高 prio 任务?"**——答案就在一个全局数据结构 [`cpupri`](../linux/kernel/sched/cpupri.c)。

### cpupri:全局 CPU 优先级位图

[`struct cpupri`](../linux/kernel/sched/cpupri.h#L15):

```c
#define CPUPRI_NR_PRIORITIES  (MAX_RT_PRIO+1)   // 101
#define CPUPRI_INVALID   -1
#define CPUPRI_NORMAL     0
/* values 1-99 are for RT1-RT99 priorities */
#define CPUPRI_HIGHER   100

struct cpupri_vec {
    atomic_t       count;     // 该优先级有几个 CPU
    cpumask_var_t  mask;      // 哪些 CPU 在这个优先级
};

struct cpupri {
    struct cpupri_vec  pri_to_cpu[CPUPRI_NR_PRIORITIES];  // prio → CPU 集合(位图)
    int               *cpu_to_pri;                        // CPU → 当前 prio
};
```

每个 CPU 在某一刻"当前跑的 RT 任务优先级"被记到 `cpu_to_pri[cpu]` 和对应 prio 的 `pri_to_cpu[pri].mask`。看 [`cpupri_find_fitness`](../linux/kernel/sched/cpupri.c#L144)([cpupri.c:144](../linux/kernel/sched/cpupri.c#L144)):

```c
int cpupri_find_fitness(struct cpupri *cp, struct task_struct *p,
        struct cpumask *lowest_mask, bool (*fitness_fn)(...))
{
    int task_pri = convert_prio(p->prio);     // 我要找比我低优先级的 CPU
    int idx, cpu;

    for (idx = 0; idx < task_pri; idx++) {    // ★ 从最低 prio 扫到我自己的 prio
        if (!__cpupri_find(cp, p, lowest_mask, idx))
            continue;
        ...
        return 1;                              // 找到候选 CPU 集合
    }
    ...
}
```

这又是一个"位图扫描"——但**这次是扫 CPU 而不是扫任务**:从最低 prio 的 CPU 集合扫起,找到第一个 prio 比我的任务低的 CPU 集合,任务就推过去。

```
 cpupri:全局 CPU 优先级索引(简化)

   CPU 0  跑 prio=10  → cpu_to_pri[0]=10
   CPU 1  跑 prio=50  → cpu_to_pri[1]=50
   CPU 2  跑 prio=80  → cpu_to_pri[2]=80
   CPU 3  idle        → cpu_to_pri[3]=NORMAL(0)

   pri_to_cpu[prio].mask:
     prio 0(NORMAL/idle): {CPU3}              ← 最闲
     prio 10:             {CPU0}
     prio 50:             {CPU1}
     prio 80:             {CPU2}

   唤醒 prio=20 的 RT 任务在 CPU 2(它自己 prio 80 比 20 高,新任务挤不掉):
     cpupri_find_fitness 从 idx=0 扫到 idx=19,先扫到 prio=0 的 {CPU3}
     → 选 CPU3(最闲),把 prio=20 的任务推过去
```

> **反面对比**:如果不用 `cpupri`,而是每次 push 都遍历所有 CPU 的 `rq->rt.highest_prio.curr`,64 核机器上每次 push 都要扫 64 次。`cpupri` 用 prio → CPU 集合的位图,把"找最低 prio 的 CPU"也变成位扫描——和 `rt_prio_array` 一脉相承,**两层数据结构都用位图加速选核**。注意 `cpupri_vec.count` 是 `atomic_t`,`__cpupri_find` 是无锁读的——允许读到稍微过期的数据(注释 cpupri.c:138 明说:"By the time the call returns, the CPUs may have in fact changed priorities any number of times. While not ideal, it is not an issue of correctness since the normal rebalancer logic will correct any discrepancies")——这是**乐观读**的典型用法,准确性换性能,正确性靠后续均衡兜底。

---

## 17.6 RT throttling:把实时任务钉在 95% CPU

到这里 RT 任务看起来无懈可击——绝对抢占普通任务,O(1) 选下一个,SMP 上还能精准 push。但有个**致命陷阱**:

> **不这样会怎样**:如果你写了一个 RT 程序(优先级 99),里面是个 `while(1);`——它会**永久占据 CPU**。普通任务永远轮不到,系统无法响应 SSH,无法运行 `kill`,无法回写日志,甚至无法触发 RT throttling 本身的检查(因为检查代码也是普通任务)。**整机永久锁死**,只能硬重启。

这不是危言耸听,是 Linux 历史(以及所有 RTOS)的真实问题。所以内核加了一道保险丝:**RT throttling**——把所有 RT 任务的 CPU 占比**钉死在一个上限**(默认 95%),超出就把整个 `rt_rq` 冻结,留 5% 给系统自救。

### 三件套:period / runtime / rt_time

看 [`sysctl_sched_rt_period`](../linux/kernel/sched/rt.c#L19) 和 [`sysctl_sched_rt_runtime`](../linux/kernel/sched/rt.c#L25):

```c
/* period over which we measure -rt task CPU usage in us.
 * default: 1s */
int sysctl_sched_rt_period = 1000000;          // 1 秒(单位 μs)

/* part of the period that we allow rt tasks to run in us.
 * default: 0.95s */
int sysctl_sched_rt_runtime = 950000;          // 0.95 秒(单位 μs)
```

意思:每 1 秒(period)里,所有 RT 任务**最多跑 0.95 秒**(runtime)。可通过 `/proc/sys/kernel/sched_rt_period_us` 和 `/proc/sys/kernel/sched_rt_runtime_us` 调;设 runtime=-1 表示禁用 throttle(危险,自担风险)。

每个 `rt_rq` 记账靠 [`rt_rq->rt_time`](../linux/kernel/sched/sched.h#L711)(累计运行时间)和 [`rt_rq->rt_runtime`](../linux/kernel/sched/sched.h#L712)(本 rq 的配额,从全局 `sysctl_sched_rt_runtime` 分来)。看 [`update_curr_rt`](../linux/kernel/sched/rt.c#L1001)([rt.c:1001](../linux/kernel/sched/rt.c#L1001)):

```c
static void update_curr_rt(struct rq *rq)
{
    struct task_struct *curr = rq->curr;
    struct sched_rt_entity *rt_se = &curr->rt;
    s64 delta_exec;

    if (curr->sched_class != &rt_sched_class)
        return;

    delta_exec = update_curr_common(rq);         // 算本次跑了多久
    if (unlikely(delta_exec <= 0))
        return;

    if (!rt_bandwidth_enabled())
        return;

    for_each_sched_rt_entity(rt_se) {
        struct rt_rq *rt_rq = rt_rq_of_se(rt_se);
        int exceeded;

        if (sched_rt_runtime(rt_rq) != RUNTIME_INF) {
            raw_spin_lock(&rt_rq->rt_runtime_lock);
            rt_rq->rt_time += delta_exec;                // ★ 累计本周期跑了多久
            exceeded = sched_rt_runtime_exceeded(rt_rq); // ★ 检查超没超
            if (exceeded)
                resched_curr(rq);                        // 超了,标记重调度
            raw_spin_unlock(&rt_rq->rt_runtime_lock);
            if (exceeded)
                do_start_rt_bandwidth(sched_rt_bandwidth(rt_rq)); // 启动补充定时器
        }
    }
}
```

每个 tick(以及每次入队/出队/切换)都会调 `update_curr_rt`,把当前 RT 任务跑的时间累计到 `rt_rq->rt_time`,然后检查超没超。

### throttle 的判定

看 [`sched_rt_runtime_exceeded`](../linux/kernel/sched/rt.c#L954)([rt.c:954](../linux/kernel/sched/rt.c#L954)):

```c
static int sched_rt_runtime_exceeded(struct rt_rq *rt_rq)
{
    u64 runtime = sched_rt_runtime(rt_rq);

    if (rt_rq->rt_throttled)
        return rt_rq_throttled(rt_rq);            // 已 throttle,继续 throttle

    if (runtime >= sched_rt_period(rt_rq))
        return 0;                                  // runtime≥period,等于不限制

    balance_runtime(rt_rq);                        // ★ 尝试从别的 rq 借配额
    runtime = sched_rt_runtime(rt_rq);
    if (runtime == RUNTIME_INF)
        return 0;

    if (rt_rq->rt_time > runtime) {                // ★ 累计时间超了配额
        struct rt_bandwidth *rt_b = sched_rt_bandwidth(rt_rq);
        if (likely(rt_b->rt_runtime)) {
            rt_rq->rt_throttled = 1;               // ★ 标记 throttle
            printk_deferred_once("sched: RT throttling activated\n");
        }
        ...
        if (rt_rq_throttled(rt_rq)) {
            sched_rt_rq_dequeue(rt_rq);            // ★ 整个 rt_rq 出队!
            return 1;
        }
    }
    return 0;
}
```

关键三步:

1. `rt_time += delta_exec`(累计);
2. `rt_time > runtime`(超了 950ms)?标 `rt_throttled=1`、`sched_rt_rq_dequeue`——**整个 `rt_rq` 从顶层 rq 出队**;
3. 一旦 throttle,后续 `pick_next_task_rt` 在 [`sched_rt_runnable`](../linux/kernel/sched/sched.h) 返回 false(因为 `rt_rq` 没挂上去),RT 调度类就选不到任务,fair 类接管——这 5% CPU 就是这么腾出来的。

> **钉死这件事**:RT throttling 的 throttle 粒度是**整个 `rt_rq`**,不是单个任务。一旦某个核的 `rt_rq` 累计 RT 时间超过 950ms,**该核上所有 RT 任务一起被冻结**——不管具体是谁占的时间。这看起来"连坐",其实是合理的:`rt_rq` 是这个核所有 RT 任务的容器,throttle 它等于"这个核接下来 50ms 不接 RT 活"。`balance_runtime` 还会尝试从同 root domain 的别的 rq 借没花完的 runtime,实现全局配额在核间再分配——但这只在 SMP 上做,单核就直接 throttle。

### 补充:period 定时器到点发还

throttle 后谁来解冻?靠 `rt_bandwidth` 里的 [`rt_period_timer`](../linux/kernel/sched/sched.h#L274)([`struct rt_bandwidth`](../linux/kernel/sched/sched.h#L269))。period(1 秒)到了,定时器 [`sched_rt_period_timer`](../linux/kernel/sched/rt.c#L70) 触发,调 [`do_sched_rt_period_timer`](../linux/kernel/sched/rt.c#L857)([rt.c:857](../linux/kernel/sched/rt.c#L857)):

```c
static int do_sched_rt_period_timer(struct rt_bandwidth *rt_b, int overrun)
{
    ...
    for_each_cpu(i, span) {                       // 遍历所有相关 CPU
        struct rt_rq *rt_rq = sched_rt_period_rt_rq(rt_b, i);
        ...
        if (rt_rq->rt_time) {
            u64 runtime;
            raw_spin_lock(&rt_rq->rt_runtime_lock);
            if (rt_rq->rt_throttled)
                balance_runtime(rt_rq);
            runtime = rt_rq->rt_runtime;
            rt_rq->rt_time -= min(rt_rq->rt_time, overrun*runtime);  // ★ 扣已用时间
            if (rt_rq->rt_throttled && rt_rq->rt_time < runtime) {
                rt_rq->rt_throttled = 0;          // ★ 解冻
                enqueue = 1;
                ...
            }
            ...
        }
        ...
        if (enqueue)
            sched_rt_rq_enqueue(rt_rq);           // ★ 重新挂回顶层 rq
        ...
    }
    ...
}
```

`rt_rq->rt_time -= min(rt_rq->rt_time, overrun*runtime)`——按周期数扣已用时间。如果扣完后 `rt_time < runtime`(还有剩余配额),`rt_throttled=0`,重新 `sched_rt_rq_enqueue`——下个调度时机 RT 任务又能跑了。

```
 RT throttling 一个周期(1s)的时间线(简化):

   0ms            950ms  950ms             1000ms
   │   RT 任务跑   │  throttle │  fair 跑  │  period 到,解冻
   │◄────────────►│◄─────────►│◄────────►│
   │  rt_time+=   │  rt_rq    │  留 5%   │  do_sched_rt_period_timer
   │  每次 update │  出 rq    │  给系统   │  rt_time -= ,rt_throttled=0
   │              │           │  自救    │  rt_rq 重新入队
```

> **钉死这件事**:RT throttling 是个**周期性循环**:累计 → 超额 → throttle → 5% fair 跑 → period 到 → 扣时间 → 解冻 → 再累计。1 秒周期、950ms 配额的默认值是工程取舍:周期太短(如 100ms),throttle/解冻太频繁,调度抖动大;周期太长(如 10s),一次 throttle 就冻 500ms,实时保障变成"实时失去保障"。1 秒和 95% 是经验值,大部分发行版默认就这个,只有真正的硬实时系统才会调(或干脆禁用 throttle 走 RT-Linux/`PREEMPT_RT` 路线)。

---

## 17.7 技巧精解:位图 O(1) 选 prio + cpupri 乐观读

这一章最硬核的两个技巧:**位图选 prio**(单核)、**cpupri 选 CPU**(跨核)。它们是同一种思路(位图索引)在两个层次的应用,值得单独拆透。

### 技巧一:位图 + prio 链表 = O(1) 选最高优先级

回头看 [`__enqueue_rt_entity`](../linux/kernel/sched/rt.c#L1401)([rt.c:1401](../linux/kernel/sched/rt.c#L1401)) 的核心:

```c
__set_bit(rt_se_prio(rt_se), array->bitmap);   // 入队:位标 1
list_add_tail(&rt_se->run_list, queue);          // 同 prio 接到链尾
```

和 [`pick_next_rt_entity`](../linux/kernel/sched/rt.c#L1721)([rt.c:1721](../linux/kernel/sched/rt.c#L1721)) 的核心:

```c
idx = sched_find_first_bit(array->bitmap);      // 选:位扫描
queue = array->queue + idx;
next = list_entry(queue->next, ...);
```

**位图承担"有没有 prio=i 的任务"的查询,链表承担"具体是哪个任务"的取出**。两者分工,选最高 prio 永远是常数时间。

> **朴素写法的墙**:朴素链表选最高 prio 是 O(N)——`list_for_each_entry` 扫一遍几千个任务。位图+链表是 O(1)(位扫描单条指令 + O(1) 链表)。**两个数量级**的差距,在几千个 RT 任务的电信网关上,每秒成千上万次 `pick_next` 累计起来是决定性的。
>
> 还有个**正确性陷阱**:朴素链表要按 prio 分组排序,否则找不到"最高 prio"。但 RT 任务**频繁入队/出队**(中断唤醒、信号、阻塞),每次维护有序链表是 O(N) 插入。位图+链表让"分组"变成 100 条独立链表(每条不要求内部排序,FIFO/RR 自己处理),插入是 O(1) `list_add_tail`——**把"有序"这个负担从链表转移到位图**,而位图天生就是常数时间查询。

**为什么 sound(为什么不会出错)**:位图的 `__set_bit` / `__clear_bit` 是原子位操作(在 x86 上是 `lock or` / `lock btr` 单条带 lock 前缀的指令),即使在 SMP 上也不需要额外锁——而 `rt_rq->active` 的访问都在 `rq->lock` 保护下,所以位图操作天然安全。链表的 `list_add_tail` / `list_del` 也在同一个 `rq->lock` 下,不会撕裂。

### 技巧二:cpupri 的乐观读——准确性换性能

回头看 [`__cpupri_find`](../linux/kernel/sched/cpupri.c#L67)([cpupri.c:67](../linux/kernel/sched/cpupri.c#L67)) 的注释:

```c
static inline int __cpupri_find(struct cpupri *cp, struct task_struct *p,
        struct cpumask *lowest_mask, int idx)
{
    struct cpupri_vec *vec = &cp->pri_to_cpu[idx];
    int skip = 0;

    if (!atomic_read(&(vec)->count))                // ★ 无锁读 count
        skip = 1;
    /*
     * When looking at the vector, we need to read the counter,
     * do a memory barrier, then read the mask.
     * ...
     * Note: This is still all racy, but we can deal with it.
     */
    ...
}
```

注释明说:"This is still all racy, but we can deal with it." cpupri 的读端**不加锁**,直接读 `count`(atomic)和 `mask`(cpumask),允许读到过期数据。

> **朴素写法的墙**:cpupri 的更新(`cpupri_set`,每次 CPU 上 RT prio 变化都调)非常频繁——任何 RT 任务入队/出队都会改。朴素写法是每次读都拿 `cp->lock`,但 SMP 上 push 操作每秒可能几千次,锁竞争激烈。乐观读不拿锁,读到的"CPU X prio 比我低"可能已经过期(CPU X 刚被一个更高 prio 的 RT 任务占了)——但这不要紧,push 失败后下一次 `pick_next` 还会重新决策,而且拉/推操作会自然纠正错误(注释 cpupri.c:138:"the normal rebalancer logic will correct any discrepancies")。
>
> 这是个典型的**乐观并发**模式:读多写少、对短暂不一致容忍、用后续机制兜底。同 mm 系列的 per-cpu pageset 乐观读、内存分配器的 per-CPU cache 一脉相承——**Linux 内核大量用"读端无锁、写端原子、错误容忍"换性能**。

**为什么 sound**:cpupri 的数据是**辅助决策**而非权威——选错 CPU 不会破坏正确性,只会让任务多走一步(被 push 到的 CPU 发现自己其实更忙,任务再被 push 一次或拉回)。RT 任务最终能不能跑,权威决策还是 `rq->lock` 保护下的 `pick_next_task_rt`——乐观读只影响"放哪个核",不影响"能不能跑"。

---

## 17.8 RT 的"组调度":RT throttling 与 cgroup

顺带一提,RT 也支持组调度([`CONFIG_RT_GROUP_SCHED`](https://elixir.bootlin.com/linux/v6.9/source/Documentation/admin-guide/cgroup-v1/cpu_rt.txt)),每个 cgroup 有独立的 `rt_rq` 和 `rt_bandwidth`。这时 throttling 是**层级化**的:

- 父 cgroup 的 `rt_rq` 被 throttle,子 cgroup 的 `rt_rq` 也不能跑(就算自己配额没满)。
- 子 cgroup 的 `rt_rq` 被 throttle,父 cgroup 的 `rt_rq` 仍可跑别的子组任务。

这让系统管理员能"给某个 cgroup 分配 30% 的 RT 配额"——`cpu.rt_runtime_us` cgroup 文件控制。具体的 cgroup 机制我们留到 [第 19 章](P6-19-cgroup-cpu-组调度与bandwidth限额.md)讲 fair cgroup 时统一讲,这里只需记住:**RT throttling 的"period/runtime 配额"模型同样适用于 cgroup**,层级化 throttle 让多租户环境(一个机器上跑多个客户的 RT 服务)能隔离。

---

## 章末小结

这一章服务二分法的**策略层**——`rt_sched_class` 回答"给定一个 rt_rq,下一个跑谁"。它的答案简单粗暴:**prio 最小的那个**,靠位图+100 条链表实现 O(1)。但"绝对抢占"是个危险特权,所以又有 RT throttling 这道保险丝:把所有 RT 任务钉在 95% CPU,留 5% 给系统自救。

我们学到的几个关键点:

1. RT 任务的"绝对优先"靠**调度类链顺序**——RT 在 fair 之前,有可运行任务时 fair 类根本不被问到。
2. `rt_rq` 的位图+链表让"选最高 prio"变成单条位扫描指令——这是 Linux O(1) 调度器留下的招牌技巧。
3. `SCHED_FIFO` 没时间片(一直跑)、`SCHED_RR` 在同 prio 内轮转(100ms 时间片),两者都按 prio 绝对抢占普通任务。
4. SMP 上 RT 用 **push 模型**(`cpupri` 全局位图找最低 prio 的 CPU),和普通任务的 pull `load_balance` 镜像。
5. **RT throttling**(period=1s、runtime=0.95s)是保险丝:整个 rt_rq 累计超额就被冻结,period 到了扣时间、解冻,留 5% 给系统自救。

### 五个"为什么"清单

1. **为什么 RT 任务能"绝对抢占"普通任务?** 调度类链按 stop > dl > **rt** > fair > idle 顺序遍历,RT 有可运行任务时 fair 类的 `pick_next_task_fair` 根本不会被调到——这是"绝对"的工程落实。
2. **`rt_rq` 怎么 O(1) 选最高优先级?** 位图(100 位)+ 100 条链表。入队 `__set_bit(prio, bitmap)`,选下一个 `sched_find_first_bit(bitmap)`——位扫描是单条 CPU 指令(`bsf`)。
3. **`SCHED_FIFO` 和 `SCHED_RR` 差在哪?** FIFO 没时间片,一直跑到自愿让出或被更高 prio 抢占;RR 在同 prio 内 100ms 时间片轮转(`task_tick_rt` 里 `--time_slice` 归零时 `requeue_task_rt`)。两者都按 prio 绝对优先。
4. **RT throttling 为什么是 95% 而不是 100%?** 一个 `while(1);` 的失控 RT 进程能永久锁死 CPU。留 5% 给系统(fair 类跑管理指令、迁移线程、回写日志)自救——"宁可丢实时保障,也不让整机挂死"。
5. **SMP 上 RT 任务怎么均衡?** push 模型:一个核 RT 入队但当前核更忙,用 `cpupri`(prio → CPU 集合的位图)找最低 prio 的 CPU 把任务推过去。和普通任务的 pull `load_balance` 镜像——一个推一个拉。

### 想继续深入往哪钻

- 本章点到的 `rt_rq` 字段(`rt_throttled`/`rt_time`/`rt_runtime`)详见 [`kernel/sched/sched.h:691-722`](../linux/kernel/sched/sched.h#L691-L722)。
- 位图+链表的 O(1) 选 prio 看 [`pick_next_rt_entity@rt.c:1714`](../linux/kernel/sched/rt.c#L1714)、[`__enqueue_rt_entity@rt.c:1375`](../linux/kernel/sched/rt.c#L1375)。
- RT throttling 的判定看 [`sched_rt_runtime_exceeded@rt.c:954`](../linux/kernel/sched/rt.c#L954),补充看 [`do_sched_rt_period_timer@rt.c:857`](../linux/kernel/sched/rt.c#L857)。
- `cpupri` 全局 CPU 优先级索引看 [`kernel/sched/cpupri.c`](../linux/kernel/sched/cpupri.c)、[`cpupri.h`](../linux/kernel/sched/cpupri.h)。
- 想观测 RT 调度:`chrt -p <pid>` 看策略和优先级,`/proc/<pid>/sched` 看 policy,`/proc/sys/kernel/sched_rt_runtime_us` 看 throttle 配额。RT throttle 触发时 dmesg 会出现 `"sched: RT throttling activated"`(printk_deferred_once)。
- 想调 RT 时间片:`/proc/sys/kernel/sched_rr_timeslice_ms`(单位 ms,0 表示用默认 100ms)。

### 引出下一章

RT 用 prio 排队,简单粗暴但缺乏**理论保障**——它不能保证一个 RT 任务"在它的截止时间前完成",只保证"比低 prio 优先"。如果有个任务有**严格的截止时间**(如"必须在 5ms 内完成,否则控制失败"),prio 救不了你。下一章讲 Linux 的最高优先级调度类 `dl_sched_class`(`SCHED_DEADLINE`):**EDF 最早截止期 + CBS 带宽预留**,数学上能证明可调度性。还顺便讲两个"特殊"调度类:**`idle_sched_class`**(没事干的兜底)、**`stop_sched_class`**(特权,迁移/CPU 热插拔用,能抢占一切)。
