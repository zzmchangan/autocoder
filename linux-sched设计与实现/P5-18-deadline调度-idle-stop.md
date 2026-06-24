# 第十八(五)章 · deadline 调度 + idle + stop ★

> 篇:第 5 篇 · 实时与特殊调度类
> 主线呼应:[上一章](P5-17-RT实时调度-rt_rq与RT-throttling.md)讲 RT 用 prio 排队——简单粗暴,但**没有理论保障**:它不能保证一个 RT 任务"在它的截止时间前完成",只保证"比低 prio 先跑"。如果你的任务是机械臂的"每 5ms 必须算完一次伺服控制,晚一毫秒就撞墙",prio 救不了你,你需要的是**数学上能证明可调度性**的算法。Linux 给这种"硬实时"任务准备了 [`dl_sched_class`](../linux/kernel/sched/deadline.c#L2815)(`SCHED_DEADLINE`),用 **EDF(最早截止期)+ CBS(常数带宽服务)** 两件套,是目前内核里**优先级最高的真实调度类**。但调度类链上还有两个"看起来奇怪"的特殊类:[`idle_sched_class`](../linux/kernel/sched/idle.c#L528)(没事干的兜底)和 [`stop_sched_class`](../linux/kernel/sched/stop_task.c#L106)(特权,迁移/CPU 热插拔用,能抢一切)。这一章把这三个调度类一次讲清,凑齐调度类链上最后几块拼图。

## 核心问题

**`SCHED_DEADLINE` 凭什么"理论上保证可调度"?EDF(选最早截止)和 CBS(带宽预留)各起什么作用?为什么 dl 类的 `dl_rq` 用红黑树而不是位图?idle 和 stop 这两个"特殊"调度类为什么必须存在——没有它们系统哪里就崩了?**

读完本章你会明白:

1. `SCHED_DEADLINE` 的三参数模型(`runtime`/`deadline`/`period`):一个周期里允许跑多久、相对截止期多久、周期多长。
2. **EDF**(Earliest Deadline First):`dl_rq` 用红黑树按绝对截止期排序,选最左节点——截止期最近的任务先跑。
3. **CBS**(Constant Bandwidth Server):每个任务带"预算"(`runtime`),耗尽就 throttle 到下一个周期补满——这是 EDF 的可调度性证明成立的关键(否则一个超估 runtime 的任务能拖死整个系统)。
4. **准入控制**(admission control):新加 dl 任务前检查 `total_bw + new_bw ≤ 100%`,从源头拒绝过载——RT 类没这层保护。
5. `idle_sched_class`(每核一个 idle 线程,跑 cpuidle 省电)和 `stop_sched_class`(每核一个 stop/migration 线程,特权级最高,执行 `migration_cpu_stop` 等"必须立刻执行且不可被打断"的工作)为什么是调度类链的两端。

> 逃生阀:如果只想要直觉,记住三句:**① `SCHED_DEADLINE` = "在 deadline 前的 budget(runtime)跑完一个周期的事,CBS 防止超预算";② EDF 用红黑树选最早 deadline(注意 RT 用位图选最低 prio,dl 用红黑树选最早 deadline——结构反映算法);③ idle 是兜底(没活干时跑省电循环),stop 是特权(迁移/热插拔用,能抢一切,自己不被抢)。**

---

## 18.1 一句话点破

> **`SCHED_DEADLINE` 用 EDF 选最早截止期的任务、用 CBS 给每个任务发"预算"防止超支、用准入控制从源头拒绝过载——三者合起来才有数学上的可调度性保障;`idle_sched_class` 是"没活干时跑省电循环"的兜底;`stop_sched_class` 是"特权级最高、能抢一切、自己不被抢"的迁移/热插拔专用类,只在内核需要"绝对原子地做一件事"时短暂出现。三者凑齐调度类链两端。**

这是结论,不是理由。本章倒过来拆:先讲 EDF+CBS 为什么能"保证"可调度(这是 dl 类的灵魂),再看 `dl_rq` 用红黑树怎么实现 EDF,然后看准入控制,最后讲 idle 和 stop 这两个"调度类链的两端"为什么是系统运转不可缺的角色。

---

## 18.2 SCHED_DEADLINE:为什么要数学保障,RT 不够吗

[上一章](P5-17-RT实时调度-rt_rq与RT-throttling.md)的 RT 任务(`SCHED_FIFO`/`SCHED_RR`)按 prio 抢占。但 prio 是个**定性**指标——你说"我的任务 prio=99 最高",内核就让你先跑,但内核**不知道你具体要多久跑完**。这导致两个问题:

- **优先级反转无法避免**:prio=99 的任务 A 跑了一段时间被 prio=99 的任务 B 抢(同 prio FIFO 不会自动让),B 又被 prio=99 的 C 抢……A 的"什么时候跑完"完全不可预测。
- **过载时集体饿死**:10 个 prio=99 的任务,每个声明"我要 100ms 跑完"——加起来 1 秒,而 CPU 一秒只能干 1 秒的事。如果它们周期都是 1s,理论上刚好够,但 prio 调度**不知道这件事**,可能让其中几个饿死。

> **不这样会怎样**:工业控制、机器人、音视频采集这类场景需要的是**确定性**:我能告诉你"这个任务每 10ms 触发一次,每次需要 2ms CPU 算完,10ms 内必须出结果"——内核应该能**事先告诉你**"能不能容纳这个任务,以及容纳了之后不会违约"。prio 调度做不到,因为它没有"需要多少 CPU"的信息。

Linux 3.14 引入 `SCHED_DEADLINE`(由 Dario Faggioli、Juri Lelli 等实现),它的思路是:**让用户在 `sched_setattr()` 时明确告诉内核三件事**:

- **`sched_runtime`**:这个任务每个周期需要的最坏 CPU 时间(预算)。
- **`sched_deadline`**:相对截止期(从周期开始算多少时间内必须完成)。
- **`sched_period`**:周期(每过多久触发一次)。

内核拿到这三参数,做**两件事**:

1. **准入控制**(admission):算 `utilization = runtime / period`(密度 density 是 `runtime / deadline`),检查加入后总利用率 ≤ 100%(per root domain)。**超了直接拒绝**(`sched_setattr` 返回 -EINVAL)。这就是 dl 类的"事前承诺"——你不会在运行时才知道系统过载。
2. **运行时调度**:用 EDF(选最早截止)挑下一个跑,用 CBS(预算耗尽就 throttle)防止任何任务超支——两者合起来,**数学上可证明**:只要所有任务都满足 `Σ runtime_i / period_i ≤ 1`(隐式 deadline 模型),EDF 就能保证没有任何任务错过 deadline。

> **钉死这件事**:RT 任务靠 prio,内核不知道你要多少 CPU;dl 任务靠**三参数**,内核事前算账。RT 是"先到先得"(prio 高的占着),dl 是"事先预算"(过载就拒绝设置)。这是两种**完全不同**的实时模型——RT 是"尽力而为 + 优先级",dl 是"硬保证 + 利用率约束"。如果你的任务真的不能错过 deadline,用 `SCHED_DEADLINE`;如果只是想"比普通任务优先",用 `SCHED_FIFO`/`SCHED_RR` 就够。

---

## 18.3 EDF:用红黑树选最早截止期的任务

EDF(Earliest Deadline First)是实时调度理论里的经典算法——**每次选截止期(absolute deadline)最早的那个任务跑**。理论已证明:对于隐式 deadline(deadline = period)的周期任务,EDF 是**最优**的单机调度算法——只要任何算法能调度的任务集,EDF 也能。

实现上,看 [`dl_rq`](../linux/kernel/sched/sched.h#L730)([sched.h:730](../linux/kernel/sched/sched.h#L730)):

```c
struct dl_rq {
    /* runqueue is an rbtree, ordered by deadline */
    struct rb_root_cached  root;                  // ★ 红黑树,按绝对 deadline 排序

    unsigned int           dl_nr_running;

#ifdef CONFIG_SMP
    struct {
        u64 curr;   /* 本核正在跑的 dl 任务的 deadline */
        u64 next;   /* 本核就绪队列里最早 ready 的 deadline */
    } earliest_dl;
    int            overloaded;
    struct rb_root_cached pushable_dl_tasks_root; // SMP push 用
#else
    struct dl_bw   dl_bw;
#endif
    u64 running_bw;   /* "active utilization" */
    u64 this_bw;
    u64 extra_bw;
    u64 max_bw;
    u64 bw_ratio;
};
```

注释明说:"runqueue is an rbtree, ordered by deadline"——dl_rq 是**红黑树**,按 `dl_se->deadline`(绝对截止期)排序。这和 RT 的位图+链表(O(1) 选 prio)、EEVDF 的红黑树(按 virtual deadline 排序,见 [第 7 章](P2-07-EEVDF算法-lag-eligibility-virtual-deadline.md))都不一样——**dl 用红黑树是因为 deadline 是连续的纳秒值,位图没法索引**。

红黑树的比较函数 [`__dl_less`](../linux/kernel/sched/deadline.c#L1598)([deadline.c:1598](../linux/kernel/sched/deadline.c#L1598)) 简单到只有一行:

```c
static inline bool __dl_less(struct rb_node *a, const struct rb_node *b)
{
    return dl_time_before(__node_2_dle(a)->deadline, __node_2_dle(b)->deadline);
}
```

入队 [`__enqueue_dl_entity`](../linux/kernel/sched/deadline.c#L1679)([deadline.c:1679](../linux/kernel/sched/deadline.c#L1679)):

```c
static void __enqueue_dl_entity(struct sched_dl_entity *dl_se)
{
    struct dl_rq *dl_rq = dl_rq_of_se(dl_se);

    WARN_ON_ONCE(!RB_EMPTY_NODE(&dl_se->rb_node));

    rb_add_cached(&dl_se->rb_node, &dl_rq->root, __dl_less);  // ★ 按 deadline 插入红黑树

    inc_dl_tasks(dl_se, dl_rq);                                // 维护 earliest_dl.curr/next, push 用
}
```

选下一个 [`pick_next_dl_entity`](../linux/kernel/sched/deadline.c#L2075)([deadline.c:2075](../linux/kernel/sched/deadline.c#L2075)):

```c
static struct sched_dl_entity *pick_next_dl_entity(struct dl_rq *dl_rq)
{
    struct rb_node *left = rb_first_cached(&dl_rq->root);   // ★ 取最左 = 最早 deadline

    if (!left)
        return NULL;

    return __node_2_dle(left);
}
```

红黑树的缓存(`rb_root_cached`)维护了最左节点指针——`rb_first_cached` 是 O(1)。所以"选最早 deadline"=取红黑树最左,常数时间。

```
 dl_rq 的红黑树(按绝对 deadline 排序,简化)

   绝对 deadline:    10ms     25ms     50ms     80ms
                    task_A   task_B   task_C   task_D
                     ↑
                     rb_first_cached 指向最左(最早 deadline)

   pick_next_task_dl → 取最左 → task_A
   入队 deadline=30ms 的 task_E:rb_add_cached → 插到 task_B 和 task_C 之间
   task_A 跑完出队:rb_erase_cached → 最左变成 task_B,缓存更新
```

> **钉死这件事**:dl 用红黑树、RT 用位图、EEVDF 也用红黑树——**数据结构反映算法本质**。RT 的 prio 是 0-99 的离散值,位图索引完美匹配;dl 的 deadline 是连续的纳秒值,位图无法索引(数值范围太大),红黑树是自然选择。EEVDF 的 virtual deadline 同样连续,也用红黑树。**调度器的数据结构不是随便选的,是算法本质决定的**——这是看内核源码能学到的"数据结构→算法"对应关系的活教材。

---

## 18.4 CBS:给每个任务发预算,防止超支

光有 EDF 不够。EDF 的可调度性证明有个**前提**:任务的实际 CPU 需求**不超过声明的 runtime**。如果某个任务声明 "10ms 周期里要 2ms",但实际跑了 5ms——它就把别的任务的预算挤掉了,EDF 的保证破产。

> **不这样会怎样**:如果允许任务超支,一个错估 runtime 的任务(或恶意任务)能拖死整个 dl 系统——和 RT 没有 throttle 时的"while(1) 锁死整机"是同一种病。Linux 必须给 dl 任务也加一道"预算约束",防止任何单一任务超支破坏全局可调度性。

这就是 **CBS(Constant Bandwidth Server)**——每个 dl 任务带一个"预算"(=`runtime`),耗尽就 throttle,等下一个周期(`period`)补充。看 [`update_curr_dl_se`](../linux/kernel/sched/deadline.c#L1326)([deadline.c:1326](../linux/kernel/sched/deadline.c#L1326)) 的核心:

```c
static void update_curr_dl_se(struct rq *rq, struct sched_dl_entity *dl_se, s64 delta_exec)
{
    s64 scaled_delta_exec;

    if (unlikely(delta_exec <= 0)) {
        if (unlikely(dl_se->dl_yielded))
            goto throttle;
        return;
    }
    ...
    /* 频率/容量归一化(在异构 CPU 上让 runtime 公平) */
    scaled_delta_exec = cap_scale(delta_exec, scale_freq);
    scaled_delta_exec = cap_scale(scaled_delta_exec, scale_cpu);

    dl_se->runtime -= scaled_delta_exec;             // ★ 扣预算

throttle:
    if (dl_runtime_exceeded(dl_se) || dl_se->dl_yielded) {   // ★ 预算耗尽(<=0)
        dl_se->dl_throttled = 1;                     // ★ 标记 throttle

        if (dl_runtime_exceeded(dl_se) &&
            (dl_se->flags & SCHED_FLAG_DL_OVERRUN))
            dl_se->dl_overrun = 1;                   // 通知用户态"你超预算了"

        dequeue_dl_entity(dl_se, 0);                 // ★ 出队
        ...
        if (unlikely(is_dl_boosted(dl_se) || !start_dl_timer(dl_se))) {
            /* boost 或定时器启动失败,立即重新入队(罕见) */
            enqueue_dl_entity(dl_se, ENQUEUE_REPLENISH);
        }

        if (!is_leftmost(dl_se, &rq->dl))
            resched_curr(rq);                        // 触发重调度
    }
    ...
}
```

判定预算是否耗尽,看 [`dl_runtime_exceeded`](../linux/kernel/sched/deadline.c#L1281)([deadline.c:1281](../linux/kernel/sched/deadline.c#L1281))——简单到只有一行:

```c
int dl_runtime_exceeded(struct sched_dl_entity *dl_se)
{
    return (dl_se->runtime <= 0);                    // ★ 预算耗尽
}
```

throttle 后,任务的 `dl_timer`(per-task 的 hrtimer,见 [`sched_dl_entity.dl_timer`](../linux/include/linux/sched.h#L651))被启动,定到下一个周期开始时触发(看 [`start_dl_timer`](../linux/kernel/sched/deadline.c#L1045),[deadline.c:1045](../linux/kernel/sched/deadline.c#L1045))。定时器到点,补充预算 + 推后 deadline,看 [`replenish_dl_entity`](../linux/kernel/sched/deadline.c#L831)([deadline.c:831](../linux/kernel/sched/deadline.c#L831)) 的核心:

```c
static void replenish_dl_entity(struct sched_dl_entity *dl_se)
{
    ...
    /* 预算严重超支时(deadline 已过),可能要补好几个周期才把 runtime 补正 */
    while (dl_se->runtime <= 0) {
        dl_se->deadline += pi_of(dl_se)->dl_period;   // ★ 推后一个 period
        dl_se->runtime += pi_of(dl_se)->dl_runtime;   // ★ 补一个 runtime
    }
    ...
    if (dl_se->dl_throttled)
        dl_se->dl_throttled = 0;                      // 解冻
}
```

这就是 CBS 的全部:**预算耗尽 → throttle → 等下一个 period → 补预算 + 推 deadline → 解冻**。一个 `while` 循环处理"预算超支多个 period"的极端情况(任务声明 2ms 实际跑了 10ms,要补好几个周期)。

```
 CBS 的运行时(简化):

   period=10ms, runtime=2ms, deadline=10ms(隐式)的任务

   时间轴(每格 1ms):
   0  1  2  3  4  5  6  7  8  9  10 11 ...
   │██│  throttle     │  等下一个 period │██│
   │  │               │                  │  │
   │  runtime=2ms     │  dl_timer 在      │  period 到了:
   │  跑完 → runtime=0│  10ms 处触发      │  replenish:
   │  dl_throttled=1  │                  │  runtime=2ms
   │  出队            │                  │  deadline 推到 20ms
   │                  │                  │  重新入队,选最早 dl → 又轮到我
```

> **钉死这件事**:CBS 把"超支任务"关进它自己的带宽里——你声明 2ms 就只给 2ms,多了不让跑,等下个周期。这保证了**任何单一 dl 任务都不会破坏全局的可调度性**。这是 EDF 的可调度性证明在工程上能成立的**关键**——纯 EDF 假设任务不超支,CBS 把这个假设变成强制约束。EDF + CBS 才是完整的 `SCHED_DEADLINE` 算法,缺一不可。

---

## 18.5 CBS 唤醒规则:要不要给唤醒任务补预算

dl 任务经常周期性地唤醒(sleep → wakeup → 跑 → sleep)。问题是:任务中途唤醒,**剩余预算够不够跑到 deadline**?如果不够,要不要立刻补?

这就是 CBS 的"唤醒规则",看 [`update_dl_entity`](../linux/kernel/sched/deadline.c#L1012)([deadline.c:1012](../linux/kernel/sched/deadline.c#L1012)):

```c
static void update_dl_entity(struct sched_dl_entity *dl_se)
{
    struct rq *rq = rq_of_dl_se(dl_se);

    if (dl_time_before(dl_se->deadline, rq_clock(rq)) ||
        dl_entity_overflow(dl_se, rq_clock(rq))) {

        if (unlikely(!dl_is_implicit(dl_se) &&
                     !dl_time_before(dl_se->deadline, rq_clock(rq)) &&
                     !is_dl_boosted(dl_se))) {
            update_dl_revised_wakeup(dl_se, rq);   // constrained deadline 用 Revised CBS
            return;
        }

        replenish_dl_new_period(dl_se, rq);        // 隐式 deadline 用 Original CBS
    }
}
```

判定要不要补预算的 [`dl_entity_overflow`](../linux/kernel/sched/deadline.c#L903)([deadline.c:903](../linux/kernel/sched/deadline.c#L903)) 用一个等式(注释 deadline.c:893-897):

```
  runtime / (deadline - t) > dl_runtime / dl_deadline  → 溢出,需要补
```

`runtime` 是剩余预算,`deadline - t` 是距截止期剩余时间,`dl_runtime/dl_deadline` 是声明的密度。**剩余密度大于声明密度 → 任务来不及跑完 → 必须补预算 + 推 deadline**。

```
 CBS 唤醒规则的两种情况(隐式 deadline):

   情况 A:任务在 deadline 还远时唤醒,剩余预算够
     runtime=1ms, deadline-t=5ms → 密度 1/5=0.2 < 声明 2/10=0.2(相等)
     → 不补,继续用剩余预算和原 deadline(节省 deadline 推移)

   情况 B:任务在 deadline 即将到来时唤醒,剩余预算显然不够
     runtime=1ms, deadline-t=1ms → 密度 1/1=1.0 > 声明 0.2
     → 溢出!replenish_dl_new_period:
       runtime = dl_runtime(2ms)
       deadline += dl_period(推到下个周期)
```

> **钉死这件事**:这个唤醒规则是 CBS 论文(Abeni & Lipari)的精髓——**只在必要时补预算**,能复用就复用。这避免了"任务每次唤醒都把 deadline 推一个周期"的过度宽松,让 dl 任务的 deadline 尽可能贴近真实需求,提高调度密度。注释 deadline.c:893-897 明说:"we can keep the current (absolute) deadline and residual budget without disrupting the schedulability of the system"——这是 CBS 数学上的**关键洞察**:只要剩余密度 ≤ 声明密度,就还能按原 deadline 跑完,不会破坏全局可调度性。

---

## 18.6 准入控制:从源头拒绝过载

dl 类的另一道保障是**准入控制**(admission control)——**设置 dl 策略时就检查总利用率,过载直接拒绝**。看 [`sched_dl_overflow`](../linux/kernel/sched/deadline.c#L2945)([deadline.c:2945](../linux/kernel/sched/deadline.c#L2945)):

```c
int sched_dl_overflow(struct task_struct *p, int policy, const struct sched_attr *attr)
{
    u64 period = attr->sched_period ?: attr->sched_deadline;
    u64 runtime = attr->sched_runtime;
    u64 new_bw = dl_policy(policy) ? to_ratio(period, runtime) : 0;  // ★ 算带宽比例
    ...
    struct dl_bw *dl_b = dl_bw_of(cpu);                              // root domain 级带宽容器

    raw_spin_lock(&dl_b->lock);
    cpus = dl_bw_cpus(cpu);
    cap = dl_bw_capacity(cpu);

    if (dl_policy(policy) && !task_has_dl_policy(p) &&
        !__dl_overflow(dl_b, cap, 0, new_bw)) {                      // ★ 新任务:加进去不溢出?
        ...
        __dl_add(dl_b, new_bw, cpus);
        err = 0;                                                     // 接受
    } else if (dl_policy(policy) && task_has_dl_policy(p) &&
               !__dl_overflow(dl_b, cap, p->dl.dl_bw, new_bw)) {     // ★ 已是 dl,改参数后不溢出?
        ...
        err = 0;                                                     // 接受
    } ...
    raw_spin_unlock(&dl_b->lock);
    return err;                                                      // -1 = 拒绝(返回 -EINVAL 给用户)
}
```

判定 [`__dl_overflow`](../linux/kernel/sched/deadline.c#L240)([deadline.c:240](../linux/kernel/sched/deadline.c#L240)) 也是一行:

```c
static inline bool
__dl_overflow(struct dl_bw *dl_b, unsigned long cap, u64 old_bw, u64 new_bw)
{
    return dl_b->bw != -1 &&
           cap_scale(dl_b->bw, cap) < dl_b->total_bw - old_bw + new_bw;
}
```

`total_bw` 是这个 root domain(逻辑 CPU 集群)已分配的 dl 带宽总和,`bw` 是上限(每 CPU 100% 即 `1 << BW_SHIFT`)。**新任务的 `new_bw` 加进去,超过 root domain 的总容量 → 拒绝**。

> **反面对比**:RT 类没有准入控制——你可以无限地 `chrt -f 99 ./task`,内核照单全收,直到它们互相抢 CPU 才发现问题。dl 类**事前算账**,从源头杜绝过载。这是两种实时模型的根本差异:RT 是"先到先得,过载大家一起饿死",dl 是"事先预算,过载拒绝加入"。如果你的任务真的不能错过 deadline,dl 的准入控制是你**写程序时就知道系统能不能容纳**的关键。

注意 `dl_bw` 是 **per root domain** 容器([`struct dl_bw`](../linux/kernel/sched/sched.h#L301)),不是 per-CPU。一个 root domain(通常 = 整机或 cpuset 划分的子集)共享一份带宽池,任务在这个池内任意迁移都不破坏准入约束。这和 RT throttling 的 per-rt_rq 配额模型不同——RT 是"每个核自己算",dl 是"整个 root domain 共享一份账"。

---

## 18.7 dl_server:把 fair/rt 套进 dl 服务器(6.x 重要演进)

讲完 dl 的核心,提一个 6.x(尤其 6.10 起)的重要演进:**dl_server**(deadline server)。它的想法是——**把 fair 和 rt 任务"包"进 dl 服务器里跑**,让普通任务也能享受到 dl 的"截止期保障"。

具体看 [`sched_dl_entity.dl_server`](../linux/include/linux/sched.h#L645) 标志位和 [`pick_task_dl`](../linux/kernel/sched/deadline.c#L2085)([deadline.c:2085](../linux/kernel/sched/deadline.c#L2085)) 的特殊路径:

```c
static struct task_struct *pick_task_dl(struct rq *rq)
{
    struct sched_dl_entity *dl_se;
    struct dl_rq *dl_rq = &rq->dl;
    struct task_struct *p;

again:
    if (!sched_dl_runnable(rq))
        return NULL;

    dl_se = pick_next_dl_entity(dl_rq);              // 取最早 deadline 的 dl_se
    WARN_ON_ONCE(!dl_se);

    if (dl_server(dl_se)) {                          // ★ 这是个 dl_server(不是真 dl 任务)
        p = dl_se->server_pick(dl_se);               // ★ 让 server 自己挑一个 fair/rt 任务
        if (!p) {
            ...
            goto again;
        }
        p->dl_server = dl_se;                        // 标记这个任务被某 dl_server 包着
    } else {
        p = dl_task_of(dl_se);                       // 普通 dl 任务
    }

    return p;
}
```

`dl_server` 是 6.x 引入的(参见 `kernel/sched/deadline.c` 顶部的 dl_server 接口注释 [deadline.c:318-343](../linux/kernel/sched/deadline.c#L318-L343)):每个 dl_server 自己是个 dl 调度实体(有 runtime/deadline/period),但它**不代表具体任务**,而是"代表一组 fair 或 rt 任务"——`server_pick` 让 server 自己用 fair/rt 算法挑一个被它管的任务来跑。这让内核能给"普通任务"也提供延迟保障(比如"EDI 公平调度里,某个 cgroup 的延迟不超过 X")。

> **钉死这件事**:dl_server 是个高级特性,本章只点到——它把 dl 的"截止期保障"扩展到普通任务上。源码细节(怎么用 `dl_server_init`/`dl_server_start`/`dl_server_stop` 管理)超出本章范围,但你需要知道:**dl 不仅能调度真正的 `SCHED_DEADLINE` 任务,还能"包"住 fair/rt 任务给它们提供延迟保障**。这是 6.x 内核调度器最重要的演进之一,RT-Linux(`PREEMPT_RT`)合入主线后,dl_server 是连接"通用调度"和"硬实时"的桥梁。

---

## 18.8 idle_sched_class:没事干时的兜底

dl/rt/fair 调度类都返回 NULL 时(没有可运行任务),系统怎么办?——总不能让 CPU 干停在 `__schedule` 里。这就是 [`idle_sched_class`](../linux/kernel/sched/idle.c#L528)([idle.c:528](../linux/kernel/sched/idle.c#L528)) 的职责——**永远返回一个可运行任务**,保证 `pick_next_task` 永不返回 NULL。

看 [`pick_next_task_idle`](../linux/kernel/sched/idle.c#L476)([idle.c:476](../linux/kernel/sched/idle.c#L476)):

```c
struct task_struct *pick_next_task_idle(struct rq *rq)
{
    struct task_struct *next = rq->idle;            // ★ 每 CPU 一个 idle 线程

    set_next_task_idle(rq, next, true);
    return next;
}
```

简单到极致:返回 `rq->idle`——每 CPU 一个 idle 线程(在 [`idle_thread_get`](https://elixir.bootlin.com/linux/v6.9/source/kernel/sched/core.c) 创建)。这个线程跑什么?看 [`do_idle`](https://elixir.bootlin.com/linux/v6.9/source/kernel/sched/idle.c)([`cpu_idle_poll`](../linux/kernel/sched/idle.c#L52) 或更深度的 cpuidle 框架):

```c
static noinline int __cpuidle cpu_idle_poll(void)   // idle.c:52
{
    ...
    raw_local_irq_enable();
    while (!tif_need_resched() &&
           (cpu_idle_force_poll || tick_check_broadcast_expired()))
        cpu_relax();                                // ★ 空转等 TIF_NEED_RESCHED
    raw_local_irq_disable();
    ...
    return 1;
}
```

idle 线程就是 `while (!tif_need_resched()) cpu_relax();`——开中断、空转、等任何中断(时钟、网卡、键盘中断)把某个任务唤醒并 `set_tsk_need_resched`,然后退出循环,回到 `__schedule` 选下一个真任务。在没有任务的间隙,空闲 CPU 进 cpuidle 省电框架,选一个低功耗状态(`mwait`/`hlt`)睡到中断来。

> **不这样会怎样**:如果没有 idle 调度类,`pick_next_task` 在没有可运行任务时会返回 NULL,`__schedule` 会 `BUG()`(看 [`__pick_next_task@core.c:6067`](../linux/kernel/sched/core.c#L6067) 的 `BUG(); /* The idle class should always have a runnable task. */`)。**调度器永远需要一个能跑的东西**——idle 调度类保证这一点。它不只是"省电",它是**调度器逻辑闭环的兜底**。

idle 调度类有几个特殊点:

1. **没有入队/出队**——`enqueue_task_idle`/`dequeue_task_idle` 实际上是 `BUG()`([`dequeue_task_idle@idle.c:490`](../linux/kernel/sched/idle.c#L490) 明说 "scheduling from the idle thread" 是 bad)。idle 线程永远"在 rq 上",不能被移除。
2. **不能迁移**——[`select_task_rq_idle@idle.c:439`](../linux/kernel/sched/idle.c#L439) 直接 `return task_cpu(p)`,idle 线程永远绑在它那个核。
3. **任何任务唤醒都立刻抢占它**——[`wakeup_preempt_idle@idle.c:454`](../linux/kernel/sched/idle.c#L454) 无条件 `resched_curr`,因为 idle 优先级最低。

`rq->idle` 这个 task_struct 是个特殊的 idle 线程(每核一个,pid 0 的 per-CPU 实例,也叫 "swapper"),不是用户进程,只在没有真任务时短暂出现。

---

## 18.9 stop_sched_class:特权级最高,能抢一切

讲完最低优先级的 idle,反过来讲**最高优先级**的 [`stop_sched_class`](../linux/kernel/sched/stop_task.c#L106)([stop_task.c:106](../linux/kernel/sched/stop_task.c#L106))。stop 调度类排在调度类链**最前**,在 dl 之前——它能抢占一切,自己不被任何东西抢。

stop_task.c 顶部注释开宗明义:

```
/*
 * stop-task scheduling class.
 *
 * The stop task is the highest priority task in the system, it preempts
 * everything and will be preempted by nothing.
 *
 * See kernel/stop_machine.c
 */
```

每核有一个 stop 任务(`rq->stop`),代表 "migration/n" 线程(内核线程,可见于 `ps -ef | grep migration`)。看 [`pick_task_stop`](../linux/kernel/sched/stop_task.c#L36):

```c
static struct task_struct *pick_task_stop(struct rq *rq)
{
    if (!sched_stop_runnable(rq))
        return NULL;

    return rq->stop;
}
```

`sched_stop_runnable` 检查 `rq->stop` 是否在 rq 上排队——只有内核主动 "调度一个 stop 工作"时才会让 stop 任务 runnable,平时它不参与调度。

什么工作用 stop 任务?都是"**必须立刻执行且执行期间不可被打断**"的:

- **任务迁移**:[`migration_cpu_stop@core.c:2583`](../linux/kernel/sched/core.c#L2583)——把一个任务从 CPU A 迁到 CPU B。`set_cpus_allowed_ptr`(改亲和掩码)、`migrate_swap`(对调两个任务)、`active_balance`(主动负载均衡)都靠它。
- **CPU 热插拔**:CPU offline 时,把上面的任务都迁走。
- **cgroup cpuset 迁移**:cpuset 改了,把任务从一组 CPU 迁到另一组。

为什么这些工作要用 stop 任务而不是普通内核线程?因为它们**要求执行期间 rq 状态稳定**——比如迁移任务时要保证"任务此刻就在这个 rq 上,不会被别的调度路径移走"。stop 任务一旦被 pick,会一直跑到它自己结束(不能被任何东西抢),这给了它一个**绝对原子的执行窗口**。

看 stop 调度类的几个"特殊"实现:

```c
static void wakeup_preempt_stop(struct rq *rq, struct task_struct *p, int flags)
{
    /* we're never preempted */                       // ★ 永远不让位
}

static void yield_task_stop(struct rq *rq)
{
    BUG(); /* the stop task should never yield, its pointless. */
}

static void switched_to_stop(struct rq *rq, struct task_struct *p)
{
    BUG(); /* its impossible to change to this class */   // ★ 不能切换到 stop 类
}
```

注释和 BUG() 明说:**stop 任务不让位、不让出、不能被"切换进来"**。它只在内核需要时被"激活",激活后跑完就回到 idle 等下一次。

> **钉死这件事**:stop 任务是个"特权急救车"——平时不出来,出来就独占到任务完成。`migration_cpu_stop` 把"迁移任务"这种需要**原子窗口**的操作变成可能:stop 任务在跑的时候,这个 rq 不会调度别的东西,迁移代码可以放心地 `__migrate_task(rq, ..., p, dest_cpu)`,不用怕目标任务中途变化。这是 [第 16 章](P4-16-任务迁移与CPU亲和.md)讲的"任务迁移"在调度器层面的实现保障——没有 stop 任务,迁移这种"跨核数据一致性"操作根本没法安全做。

调度类链的完整顺序,看 [`DEFINE_SCHED_CLASS` 宏](../linux/kernel/sched/sched.h#L2347) 和注释([sched.h:2337-2350](../linux/kernel/sched/sched.h#L2337-L2350)):

```c
/*
 * Helper to define a sched_class instance; each one is placed in a separate
 * section which is ordered by the linker script:
 *
 *   include/asm-generic/vmlinux.lds.h
 *
 * *CAREFUL* they are laid out in *REVERSE* order!!!
 */
#define DEFINE_SCHED_CLASS(name) \
const struct sched_class name##_sched_class \
    __aligned(__alignof__(struct sched_class)) \
    __section("__" #name "_sched_class")
```

每个调度类被链接器放进独立的 section,链接脚本(`include/asm-generic/vmlinux.lds.h`,未 sparse clone)按 **stop > dl > rt > fair > idle** 的顺序排列。`for_each_class(class)` 从 stop 开始往后扫,第一个返回非 NULL 的就是赢家。所以**优先级 = 链接脚本里 section 的顺序**——这是个编译期决定的、不可改的优先级。

```
 调度类链(由链接脚本 section 顺序决定):

   __stop_sched_class  → __dl_sched_class  → __rt_sched_class  → __fair_sched_class  → __idle_sched_class
   ─────────────         ─────────────       ─────────────       ──────────────       ──────────────
   stop_sched_class      dl_sched_class      rt_sched_class      fair_sched_class     idle_sched_class
   最高(特权)          最高真实(dl)        高(rt)           中(fair/EEVDF)       最低(兜底)
   migration/n           SCHED_DEADLINE      SCHED_FIFO/RR       SCHED_NORMAL         swapper(idle)
   rq->stop              rq->dl              rq->rt              rq->cfs              rq->idle

   for_each_class 从左到右扫,第一个返回非 NULL 的就是赢家
   ★ stop 永远在最前(特权),idle 永远在最后(兜底,总返回非 NULL)
```

---

## 18.10 技巧精解:红黑树按 deadline 排序 + per-task hrtimer 补预算

这一章最硬核的两个技巧:**红黑树按 deadline 排序选最早**(选下一个)、**per-task hrtimer 精确补充预算**(throttle 后的恢复)。它们一起支撑了 dl 类的"可调度性保障"。

### 技巧一:红黑树 `rb_root_cached` —— 缓存最左节点,选最早 deadline 是 O(1)

回头看 [`__enqueue_dl_entity`](../linux/kernel/sched/deadline.c#L1685) 和 [`pick_next_dl_entity`](../linux/kernel/sched/deadline.c#L2077):

```c
rb_add_cached(&dl_se->rb_node, &dl_rq->root, __dl_less);   // 入队:按 deadline 插入,缓存最左

struct rb_node *left = rb_first_cached(&dl_rq->root);      // 选下一个:取缓存的左节点
```

`rb_root_cached` 是 Linux 红黑树的扩展(见 [`include/linux/rbtree_augmented.h`](https://elixir.bootlin.com/linux/v6.9/source/include/linux/rbtree_augmented.h),未 sparse clone),比标准 `rb_root` 多一个 `rb_leftmost` 指针,每次 `rb_add_cached` 时维护——**插入 O(log N),取最左 O(1)**。

> **朴素写法的墙**:朴素红黑树取最左是 O(log N)(从根往左走到底)。调度器每次 `pick_next` 都要取最左,O(log N) 在数千个 dl 任务时累计开销不小。`rb_root_cached` 把"取最左"降成 O(1)——**插入时维护一下最左指针,换来选下一个的常数时间**。这是个典型的"用插入的额外开销换查询的性能"权衡——调度器选下一个远比插入频繁(每秒可能上万次 pick,几百次 enqueue),这个权衡划算。
>
> 同样 EEVDF(第 7 章)、CFS 的虚拟运行时间排序、内存管理(第 9 本)的 VMA 排序、ext4 的 extent 树,都用 `rb_root_cached`——**这是 Linux 内核里"有序集合 + 频繁取极值"场景的招牌数据结构**。学 dl 调度器顺便记住这个模式,看 mm、fs 源码会一眼就懂。

**为什么 sound**(为什么不出错):红黑树的插入/删除都在 `rq->lock` 保护下(`__enqueue_dl_entity` 调用者都持有 rq 自旋锁),所以并发安全。`rb_leftmost` 的维护是 `rb_add_cached`/`rb_erase_cached` 内部完成的(它们知道什么时候最左变了),调用者不用关心——这是个干净的封装。

### 技巧二:per-task hrtimer —— 每个 dl 任务自带定时器,精确补预算

dl 任务 throttle 后,**谁来精确地"在下一个 period 开始时"补预算**?答案:每个 dl 任务**自带一个 hrtimer**(高精度定时器)。看 [`struct sched_dl_entity.dl_timer`](../linux/include/linux/sched.h#L651):

```c
struct sched_dl_entity {
    struct rb_node rb_node;
    u64 dl_runtime;       /* 每周期预算 */
    u64 dl_deadline;      /* 相对截止期 */
    u64 dl_period;        /* 周期 */
    ...
    s64 runtime;          /* 当前剩余预算(可为负,超支) */
    u64 deadline;         /* 当前周期的绝对截止期 */
    ...
    unsigned int dl_throttled : 1;
    ...
    struct hrtimer dl_timer;          /* ★ per-task 带宽补充定时器 */
    struct hrtimer inactive_timer;    /* GRUB 的 0-lag 时间定时器 */
    ...
};
```

throttle 时 [`start_dl_timer`](../linux/kernel/sched/deadline.c#L1045) 把 `dl_timer` 定到 `dl_next_period`(下一个周期开始 = `deadline - dl_deadline + dl_period`)。到点了,hrtimer 回调 [`dl_task_timer`](../linux/kernel/sched/deadline.c)(deadline.c 中部)被触发,调 `replenish_dl_entity` 补预算、解冻、把任务重新塞回 rq。

> **朴素写法的墙**:朴素做法是用一个全局定时器扫所有 throttle 的 dl 任务——但 dl 任务可能很多(几百个),全局定时器要么用一个长链表(扫描 O(N)),要么用一个时间轮(精度受限)。**per-task hrtimer 让每个任务自己负责自己的补充**,hrtimer 框架内部用红黑树组织所有定时器,插入/触发都是 O(log N),精度是纳秒级(hrtimer 基于 `CLOCK_MONOTONIC`)。
>
> 这是个"**把状态推进封装进数据结构**"的典范——每个 dl 任务自带"它自己的恢复机制",不需要外部扫描。和 EEVDF 的 hrtick(每个 cfs_rq 一个,精确抢占)、RT throttling 的 per-rt_bandwidth rt_period_timer 一样,**Linux 调度器大量用 per-entity/per-rq 的 hrtimer 做"到点推进状态"**——这是内核里"用定时器表达周期性约束"的标准模式。

**为什么 sound**(为什么 throttle 不会丢):`dl_timer` 是 hrtimer,即使 CPU 进了深度睡眠(cpuidle),hrtimer 也能通过 `tick_nohz` 框架的广播机制被唤醒(看 [`tick_check_broadcast_expired`](../linux/kernel/sched/idle.c#L61) 在 idle 循环里检查)。所以一个 throttle 的 dl 任务**绝不会因为 CPU 睡过头而错过补充时机**——它的 dl_timer 一定会按时触发。这保证了 dl 任务的"周期性"在 idle CPU 上也成立——是个 subtle 但关键的 sound 性质。

---

## 18.11 ★ 对照第 7 本:内核实时调度 vs Go runtime

这一章标 ★,简短对照 dl/idle/stop 和 Go runtime GMP:

- **dl/rt/fair/idle/stop 五个调度类** vs **Go 只有一种调度策略**(goroutine 无优先级、无 deadline):Linux 内核能区分"硬实时"(dl)、"软实时"(rt)、"普通"(fair)三种延迟需求,Go 的 goroutine 默认全公平、无优先级(只能用 channel + 优先级队列在用户态模拟)。**要硬实时保障的 Go 程序,最终还是得落在内核的 dl 任务上**(用 `runtime.LockOSThread` + cgroup + `chrt -d`)。
- **stop_sched_class(特权,迁移用)** vs **Go 的 `sysmon` 线程**:Linux 用 stop 任务(每核一个,能抢一切)做任务迁移这种"原子窗口"操作;Go 用一个独立的 `sysmon` goroutine(其实跑在独立的 M 上,不绑 P)做"长时间运行的 G 异步抢占"和"网络 poller 检查"。两者都是"超脱常规调度的特权执行体",但 stop 是**内核级、能停一切**,sysmon 是**用户态、靠发信号异步抢占**——粒度不同。
- **idle_sched_class(cpuidle 省电)** vs **Go 的 `runtime.GOMAXPROCS` 空闲 P**:Linux CPU 没事干时进 cpuidle 省电框架(`mwait`/`hlt`);Go 的 P 没事干时(本地队列为空)先尝试 work-stealing 偷别的 P 的 goroutine,实在偷不到就进 `schedtrace` 的 idle 状态。Linux 的 idle 是**物理省电**,Go 的 idle P 是**逻辑挂起**(还会被 `sysmon` 检查),粒度不同。
- **EDF+CBS 数学保障** vs **Go 没有延迟保障**:dl 类的 EDF+CBS 能数学证明"任务不违约",这是内核级硬实时;Go 的 GMP 是"尽力而为"调度,延迟取决于 G 数量和 P 数量,没有理论保障。如果 Go 程序需要硬实时,**必须把那段代码放在内核 dl 任务里跑**(或用 RT-Go 这类补丁)。

---

## 章末小结

这一章服务二分法的**策略层**——`dl_sched_class` 回答"给定一个 dl_rq,下一个跑谁"。它的答案是 **EDF 选最早截止期**(红黑树最左),配套 **CBS 防超支**(预算耗尽 throttle,per-task hrtimer 补预算)和**准入控制**(过载拒绝)。三者合起来才有 dl 类的"可调度性保障"。本章还讲了调度类链两端:`idle_sched_class`(没活干时跑省电循环,总返回非 NULL 兜底)和 `stop_sched_class`(特权级最高,迁移/CPU 热插拔用,能抢一切不被抢)。

凑齐调度类链:**stop > dl > rt > fair > idle**。stop 和 idle 是两个"特殊"类(特权急救车 + 兜底),dl/rt/fair 是三个"真实"策略类(截止期 > 优先级 > 公平)。整个第 5 篇到此结束——你已经看清内核调度器的全部"选下一个跑谁"的策略。

### 五个"为什么"清单

1. **`SCHED_DEADLINE` 比 RT 强在哪?** RT 用 prio,内核不知道你要多少 CPU,过载大家一起饿死;dl 用三参数(`runtime`/`deadline`/`period`),**事前准入控制**(总利用率超 100% 拒绝)+ **运行时 CBS**(预算耗尽 throttle),数学上能证明可调度性。
2. **dl_rq 为什么用红黑树而不是位图?** dl 的 deadline 是连续纳秒值,位图没法索引;红黑树按 deadline 排序,`rb_root_cached` 维护最左指针让"选最早"=O(1)。**数据结构反映算法本质**——RT 的离散 prio 用位图,dl 的连续 deadline 用红黑树。
3. **EDF 和 CBS 各起什么作用?** EDF(选最早 deadline)是调度算法,保证截止期最早的任务先跑;CBS(常数带宽服务)是预算约束,防止任何任务超支破坏全局可调度性。**EDF + CBS 才是完整的 dl 算法**——EDF 假设任务不超支,CBS 把这个假设变成强制约束。
4. **idle 和 stop 调度类为什么必须存在?** idle 类保证 `pick_next_task` 永不返回 NULL(没真任务时跑 cpuidle 省电循环),没有它 `__schedule` 会 BUG();stop 类提供"绝对原子的执行窗口"(迁移/CPU 热插拔用),没有它跨核数据一致性操作没法安全做。两者是调度器逻辑闭环的两端。
5. **调度类链的顺序怎么定的?** 链接脚本(`include/asm-generic/vmlinux.lds.h`)按 **stop > dl > rt > fair > idle** 的顺序排列各调度类 section,`for_each_class` 从 stop 扫到 idle,第一个返回非 NULL 的赢。**优先级 = 编译期 section 顺序**,不可改。

### 想继续深入往哪钻

- 本章点到的 `dl_rq`/`sched_dl_entity` 字段详见 [`kernel/sched/sched.h:730-789`](../linux/kernel/sched/sched.h#L730-L789) 和 [`include/linux/sched.h:598-700`](../linux/include/linux/sched.h#L598-L700)。
- EDF 选最早看 [`__enqueue_dl_entity@deadline.c:1679`](../linux/kernel/sched/deadline.c#L1679)、[`pick_next_dl_entity@deadline.c:2075`](../linux/kernel/sched/deadline.c#L2075)、[`__dl_less@deadline.c:1598`](../linux/kernel/sched/deadline.c#L1598)。
- CBS 扣预算 + throttle 看 [`update_curr_dl_se@deadline.c:1326`](../linux/kernel/sched/deadline.c#L1326)、[`dl_runtime_exceeded@deadline.c:1281`](../linux/kernel/sched/deadline.c#L1281);补充看 [`replenish_dl_entity@deadline.c:831`](../linux/kernel/sched/deadline.c#L831);唤醒规则看 [`dl_entity_overflow@deadline.c:903`](../linux/kernel/sched/deadline.c#L903)、[`update_dl_entity@deadline.c:1012`](../linux/kernel/sched/deadline.c#L1012)。
- 准入控制看 [`sched_dl_overflow@deadline.c:2945`](../linux/kernel/sched/deadline.c#L2945)、[`__dl_overflow@deadline.c:240`](../linux/kernel/sched/deadline.c#L240)。
- idle 看 [`pick_next_task_idle@idle.c:476`](../linux/kernel/sched/idle.c#L476)、[`cpu_idle_poll@idle.c:52`](../linux/kernel/sched/idle.c#L52);stop 看 [`pick_task_stop@stop_task.c:36`](../linux/kernel/sched/stop_task.c#L36)、[`migration_cpu_stop@core.c:2583`](../linux/kernel/sched/core.c#L2583)。
- 想观测 dl 调度:`chrt -d --runtime 2000000 --deadline 5000000 --period 10000000 ./task`(设 deadline:runtime=2ms, deadline=5ms, period=10ms);`/proc/<pid>/sched` 看 `policy`;`/proc/sched_debug` 看 dl_rq 状态。dl_server 和 RT-Linux 进阶可读 `Documentation/scheduler/sched-deadline.rst`。

### 引出下一章

第 5 篇到此结束,你已经看清五个调度类。但有个问题没回答:**为什么一个进程开几千个线程,它独占整机 CPU 是合理的?**——因为内核默认按任务公平,一个进程多开线程就多分 CPU。如果一台机器跑多个客户的服务,这么干就崩了。第 6 篇开篇——[第 19 章](P6-19-cgroup-cpu-组调度与bandwidth限额.md)讲 **cgroup cpu 子系统**:把任务分组,每组复用 `sched_entity` 当"组实体"参与公平,加 bandwidth throttle 限额(`cpu.max`),让一个 cgroup 最多用 N% CPU。这是调度器从"任务级公平"扩展到"组级公平"的关键一步。
