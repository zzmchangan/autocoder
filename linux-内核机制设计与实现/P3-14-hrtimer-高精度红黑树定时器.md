# 第十四 章 · hrtimer:高精度红黑树定时器

> 篇:P3 时钟与定时器(内核主动驱动的心跳)
> 主线呼应:前两章我们看清了时钟硬件的两副面孔——`clocksource` 是只读的"现在几点"(墙上时间靠它),`clock_event_device` 是可编程的"到点叫我"(定时中断靠它)。但"到点叫我"只回答了**谁**来叫。真正回答"叫醒之后内核要干什么"的,是本章的主角 **hrtimer**(high-resolution timer,高精度定时器)。一个进程 `nanosleep(0.5ms)`、一条 TCP 重传定时器、调度器 EEVDF 的精确时间片到期、NOHZ idle 的"睡到下一个事件"——全挂在一棵叫 **timerqueue** 的红黑树上。每个 CPU 一棵森林(其实是 8 棵,4 种时钟 × 硬/软两路),按到期时间排序,到期时从最左节点一路摘下来跑回调。读完本章你会理解,为什么内核从老的低精度 timer wheel 换成了 hrtimer 红黑树,为什么 `sleep(1ms)` 真能在 1ms 醒而不是等到下个 jiffy,以及为什么内核不傻乎乎给每个 timer 都编一个精确中断(那会变成中断风暴)。

## 核心问题

**`clock_event_device` 只能给 CPU 编"下一次中断在什么绝对时刻"——一次一个。但内核里同时可能有成千上万个定时器(`nanosleep`、TCP、调度 hrtick、看门狗……),怎么用一个时钟硬件满足所有这些"在某个时刻叫我"的请求?每个 CPU 怎么独立管理自己的定时器又不互相打架?凭什么能做到纳秒级精度又不在定时器多时被中断风暴淹死?**

读完本章你会明白:

1. hrtimer 的数据结构:每个 CPU 一个 `hrtimer_cpu_base`,内含 8 个 `hrtimer_clock_base`(MONOTONIC/REALTIME/BOOTTIME/TAI 四种时钟,每种再分硬中断 / 软中断两路),每个 base 一棵 timerqueue 红黑树按到期时间排序。
2. 入队 / 出队 / 找最早到期都是 O(log n) / O(1):红黑树保证最左节点就是最早到期者,`timerqueue_getnext` 一条 `rb_first_cached` 取出来。
3. **softexpires 区间(slack)** 是消中断风暴的关键:timer 给一个 `[soft, hard]` 区间,只要不晚于 hard 就行,允许把多个 timer 的"最早可唤醒点"对齐成同一次中断——换"宁可早一点点集体叫醒"换"绝不每个 timer 一个中断"。
4. 三条执行路径:`hrtimer_interrupt`(高精度模式,clock_event_device 到点中断触发)、`hrtimer_run_queues`(低精度回退,每个 jiffy 轮询)、`hrtimer_run_softirq`(软中断回调,HRTIMER_SOFTIRQ 里跑 `is_soft` 的 timer)。
5. ★ 对照 Tokio 的层级时间轮(`tokio::time::wheel`):内核要**纳秒级精确**,选红黑树(精确、O(log n));Tokio 要**百万级 timer 的高吞吐**,选层级时间轮(批量、O(1))——两种数据结构,两种取舍。

> **逃生阀**:如果你只关心"hrtimer 大概是怎么组织的",看 14.2 的 ASCII 框图和 14.3 的入队 / 到期流程就够了。14.4(softexpires)和 14.5(三条执行路径)是内核工程精妙处,14.6 的软 / 硬两路设计是 RT 内核(实时内核)的产物。但本章是时钟篇的核心,建议通读。

---

## 14.1 一句话点破

> **hrtimer 把"我要在某个时刻被叫醒"这件事,变成往一棵按到期时间排序的红黑树上插一个节点——一个 CPU 8 棵树,每棵树最左节点就是该树最早到期者,8 棵树取个 min 就是这个 CPU 的"下一个该被叫醒的时刻",把这个时刻编进 clock_event_device 即可。一个时钟硬件喂饱成千上万个定时器。**

这是结论,不是理由。本章倒过来拆:先看老 timer wheel 为什么被淘汰(为什么必须换数据结构),再看 hrtimer 的三层嵌套结构(per-CPU cpu_base → 8 个 clock_base → 8 棵红黑树),然后看入队 / 到期 / 找最早到期的源码,接着拆最关键的 softexpires 区间(凭什么不发生中断风暴),最后看清三条执行路径与软 / 硬两路设计,并回扣调度器的 hrtick。

---

## 14.2 为什么是红黑树:从老 timer wheel 说起

### 老 timer wheel 的问题:精度不够,结构也不对

Linux 早期(2.6 之前到 4.x 一段很长的时间)用的定时器叫 **timer wheel**(低精度定时器,`kernel/time/timer.c` 里现在还保留,但已经只是 hrtimer 的低分辨率模式的一种回退)。它的思路是**按 jiffy 分桶**:把到期时间对 HZ 取模,塞进一个长度为几万的哈希桶数组,每个 tick(每 1/HZ 秒,典型 1ms 或 4ms)扫一桶。

这套设计的根本问题有两个:

- **精度被 HZ 钉死**:`sleep(1ms)` 在 `CONFIG_HZ=250` 的机器上,实际可能要等到 4ms 才醒——因为定时器只能按 jiffy 粒度排。对网络协议(TCP 重传 RTO 要 sub-millisecond)、对调度器(EEVDF 要精确时间片)这是不可接受的。
- **数据结构对"成千上万个 timer"不友好**:桶数有限,大量 timer 撞同一个桶时要遍历桶内链表,找最早到期是 O(n)。而且桶的分布依赖"到期时间被 HZ 取模",在不同时间尺度上行为不一致。

> **不这样会怎样**:如果内核一直停留在 timer wheel,`nanosleep` 永远做不到纳秒精度,实时 / 多媒体应用、金融交易系统的微秒级延迟全部泡汤;TCP 的 RTO 估算不准,网络吞吐受损;调度器想做精确时间片(EEVDF / deadline 调度)根本无从谈起。所以必须有一套**和 HZ 解耦、基于纳秒级绝对时间、数据结构对大量 timer 高效**的新机制。

### hrtimer 的选择:红黑树 + 绝对纳秒时间

hrtimer(2005 年 Thomas Gleixner / Ingo Molnar 加入)彻底换了思路:

1. **时间用绝对纳秒(`ktime_t`,本质是 `s64`)**:不再依赖 jiffy,和 HZ 解耦。一个 timer 说"我要在 `t = 12345678900` 纳秒被叫醒"就是绝对时间。
2. **每个 timer 是红黑树上的一个节点**:节点按到期时间排序,最左节点永远是"最早到期的那个"。找最早到期是 O(1)(`rb_first_cached`,红黑树缓存了最左节点指针);插入 / 删除是 O(log n)。
3. **数据结构用 timerqueue**(见 [`include/linux/timerqueue.h`](../linux/include/linux/timerqueue.h)),它是对带"最左节点缓存"的红黑树(`rb_root_cached`)的薄包装,专门为定时器场景设计。

```c
/* include/linux/timerqueue.h,timerqueue_getnext 取最早到期者 */
static inline
struct timerqueue_node *timerqueue_getnext(struct timerqueue_head *head)
{
    struct rb_node *leftmost = rb_first_cached(&head->rb_root);
    return rb_entry_safe(leftmost, struct timerqueue_node, node);
}
```

> 见 [timerqueue.h:22-28](../linux/include/linux/timerqueue.h#L22-L28)。`rb_root_cached` 比普通红黑树多一个 `rb_leftmost` 指针,插入 / 删除时维护它,所以"取最早到期"是常数时间——这是 hrtimer 能高效的核心。

> **反面对比**:如果 timer queue 用普通链表(老 BSD 部分实现就这么做),插入时要遍历链表找位置 O(n),timer 多了性能崩;如果用普通红黑树(不缓存最左),"找下一个到期"每次 O(log n),而 clock_event_device 编程要频繁问"下一个是谁",累积起来很贵。**timerqueue = 红黑树 + 最左缓存**,把"按到期排序插入 O(log n)"和"取最早 O(1)"这两个最频繁的操作都做到了最优。

> **钉死这件事**:hrtimer 的精度本质来自三件事——① 时间用纳秒绝对值,不依赖 HZ;② clock_event_device 硬件能被编到任意纳秒时刻(第 12 章);③ 数据结构是红黑树,任何时刻能 O(1) 拿到"下一个该叫醒的 timer"。前两章已经讲完前两件,本章就是把第三件讲透。

---

## 14.3 数据结构:三层嵌套(per-CPU → 8 个 base → 红黑树)

一个 `hrtimer` 怎么找到自己的家?它经过三层指针:

```
 hrtimer_cpu_base(每个 CPU 一个,this_cpu_ptr(&hrtimer_bases)):
 ┌────────────────────────────────────────────────────────────────────┐
 │ raw_spinlock_t lock          (保护整个 cpu_base 及其所有 clock_base)│
 │ unsigned int active_bases    (位图:bit N 置 1 表示第 N 个 base 非空)│
 │ unsigned int hres_active:1   (高精度模式是否开启)                    │
 │ unsigned int in_hrtirq:1     (正在执行 hrtimer_interrupt)            │
 │ ktime_t expires_next         (下一个硬中断到期时刻,编进硬件)        │
 │ struct hrtimer *next_timer   (下一个到期的硬 timer,优化用)          │
 │ ktime_t softirq_expires_next (下一个软 timer 到期时刻)               │
 │ struct hrtimer *softirq_next_timer                                   │
 │                                                                    │
 │ struct hrtimer_clock_base clock_base[8]:  (HRTIMER_MAX_CLOCK_BASES) │
 │   ┌─[0] MONOTONIC      ──┐ ┌─[4] MONOTONIC_SOFT   ──┐              │
 │   │  running / active   │ │  running / active       │              │
 │   │  (timerqueue_head)  │ │  (timerqueue_head)      │              │
 │   │   rb_root_cached →  │ │   rb_root_cached →      │              │
 │   │     红★            │ │     红★                 │              │
 │   │   (按 expires 排序) │ │                         │              │
 │   └─────────────────────┘ └─────────────────────────┘              │
 │   ┌─[1] REALTIME ───────┐ ┌─[5] REALTIME_SOFT  ─────┐              │
 │   │  ...                │ │  ...                    │              │
 │   └─────────────────────┘ └─────────────────────────┘              │
 │   ┌─[2] BOOTTIME ───────┐ ┌─[6] BOOTTIME_SOFT  ─────┐              │
 │   └─────────────────────┘ └─────────────────────────┘              │
 │   ┌─[3] TAI ────────────┐ ┌─[7] TAI_SOFT       ─────┐              │
 │   └─────────────────────┘ └─────────────────────────┘              │
 └────────────────────────────────────────────────────────────────────┘
       硬中断 4 路(active_bases 低 4 位) │ 软中断 4 位(active_bases 高 4 位)
       HRTIMER_ACTIVE_HARD = 0x0F        │ HRTIMER_ACTIVE_SOFT = 0xF0
```

### 为什么每个 CPU 一份(per-CPU)

```c
/* kernel/time/hrtimer.c,简化 */
DEFINE_PER_CPU(struct hrtimer_cpu_base, hrtimer_bases) = { ... };
```

每个 CPU 一个 `hrtimer_cpu_base`,意味着**每个 CPU 各自管自己的 timer**,不需要跨 CPU 抢同一把锁。和 softirq 的 per-CPU pending 位图(第 6 章)、调度器的 per-CPU `rq`(第 11 本)是同一套思路:**凡是高频并发改的数据结构,首选 per-CPU 无锁化**。一个 CPU 给自己的 timer 入队 / 删除,只动自己的 `cpu_base`,别的 CPU 完全不受影响。

### 为什么 8 个 clock_base(4 种时钟 × 硬 / 软两路)

```c
/* include/linux/hrtimer_defs.h */
enum hrtimer_base_type {
    HRTIMER_BASE_MONOTONIC,        /* CLOCK_MONOTONIC,不受 settimeofday 影响 */
    HRTIMER_BASE_REALTIME,         /* CLOCK_REALTIME,墙上时间,可被 NTP / settimeofday 调 */
    HRTIMER_BASE_BOOTTIME,         /* CLOCK_BOOTTIME,含挂起期间 */
    HRTIMER_BASE_TAI,              /* International Atomic Time,无闰秒 */
    HRTIMER_BASE_MONOTONIC_SOFT,   /* 下面四个是上面的"软"版本 */
    HRTIMER_BASE_REALTIME_SOFT,
    HRTIMER_BASE_BOOTTIME_SOFT,
    HRTIMER_BASE_TAI_SOFT,
};
```

> 见 [hrtimer_defs.h:58-68](../linux/include/linux/hrtimer_defs.h#L58-L68)。

四种时钟分开,是因为**它们的"时间参照系"不同**:MONOTONIC 单调递增(适合测间隔)、REALTIME 可被改(settimeofday 会跳)、BOOTTIME 含挂起、TAI 不受闰秒干扰(适合金融 / 高精度)。一个 timer 一旦选了某种时钟,它的到期时间就是那个时钟坐标系下的绝对值,挂在对应的 base 上。

硬 / 软两路(`*_SOFT`)是 4.x 后加的,为 PREEMPT_RT(实时内核)服务:有些 timer 的回调里会做"不能在硬中断上下文做"的事(拿自旋锁太久、要稍重逻辑),就把它们标成 `is_soft`,到期时不直接在 hardirq 里跑,而是 raise 一个 `HRTIMER_SOFTIRQ`,在 softirq 上下文(仍然不是进程上下文,但中断已经打开)里跑。这个区分在 14.5 节展开。

### 每个 clock_base 一棵 timerqueue 红黑树

```c
/* include/linux/hrtimer_defs.h */
struct hrtimer_clock_base {
    struct hrtimer_cpu_base *cpu_base;
    unsigned int             index;     /* 在 clock_base[] 里的下标 */
    clockid_t                clockid;
    seqcount_raw_spinlock_t  seq;        /* 保护 running 指针的 seqcount */
    struct hrtimer          *running;    /* 当前正在跑回调的 timer(同一 base 同时只有一个) */
    struct timerqueue_head   active;     /* ← 就是这棵红黑树(timerqueue 包装) */
    ktime_t                (*get_time)(void);
    ktime_t                 offset;      /* 该时钟与 MONOTONIC 的偏移 */
} __hrtimer_clock_base_align;
```

> 见 [hrtimer_defs.h:47-56](../linux/include/linux/hrtimer_defs.h#L47-L56)。

`active` 字段就是 timerqueue 红黑树的根(`timerqueue_head` 内含 `rb_root_cached`)。所有挂在这个 base 上的 timer,都按 `node.expires`(到期时间)排在这棵树上。`running` 字段记录"当前这个 base 上正在执行回调的 timer 是谁"——它保证同一个 base 上的回调串行执行(下一个 timer 想跑得等当前这个跑完),这是并发正确性的关键(14.6 节展开)。

### hrtimer 自己长什么样

```c
/* include/linux/hrtimer_types.h */
struct hrtimer {
    struct timerqueue_node   node;         /* 红黑树节点,内含 expires(硬到期) */
    ktime_t                  _softexpires; /* 软到期(最早可被唤醒的时刻) */
    enum hrtimer_restart   (*function)(struct hrtimer *);  /* 到期回调 */
    struct hrtimer_clock_base *base;
    u8                       state;        /* INACTIVE / ENQUEUED */
    u8                       is_rel;       /* 相对时间模式 */
    u8                       is_soft;      /* 软中断回调 */
    u8                       is_hard;      /* 硬中断回调(RT 下强制硬) */
};

enum hrtimer_restart {
    HRTIMER_NORESTART,   /* 回调跑完不再重启 */
    HRTIMER_RESTART,     /* 回调要求把自己再插回去(周期性 timer) */
};
```

> 见 [hrtimer_types.h:39-48](../linux/include/linux/hrtimer_types.h#L39-L48) 与 [hrtimer_types.h:13-16](../linux/include/linux/hrtimer_types.h#L13-L16)。

**两个到期时间并存是理解 hrtimer 的关键**:

- `node.expires` 是**硬到期**(hard expiry)——绝不能晚于它。
- `_softexpires` 是**软到期**(soft expiry)——最早可以被叫醒的时刻。

正常 timer(`hrtimer_start` 不给 slack)两者相等,精确到期。给了 slack(`hrtimer_start_range_ns`),`_softexpires` 是用户给的原始时间,`node.expires` 是 `_softexpires + slack`。于是 timer 有一个 `[_softexpires, node.expires]` 的"可被唤醒区间",在这个区间里任何时刻被叫醒都算合规。这套"软区间"是消中断风暴的核心,14.4 节单独拆。

> **钉死这件事**:hrtimer 的三层嵌套——**per-CPU `hrtimer_cpu_base`(每核一个,无锁)→ 8 个 `hrtimer_clock_base`(4 时钟 × 硬/软两路,各管一种时间参照系 + 一种回调上下文)→ 每棵 timerqueue 红黑树(按到期排序,最左就是最早)**——这套结构同时回答了"怎么并发(每核一份)"、"怎么支持多种时钟(8 个 base)"、"怎么快速找最早到期(红黑树 + 最左缓存)"三个问题。

---

## 14.4 入队、找最早到期、到期处理:三个核心操作

### 入队:hrtimer_start_range_ns → enqueue_hrtimer

一个 timer 被启动,典型路径是 [`hrtimer_start_range_ns`](../linux/kernel/time/hrtimer.c#L1287)(`hrtimer_start` 是它的 `delta=0` 薄包装,见 [hrtimer.h:272-276](../linux/include/linux/hrtimer.h#L272-L276))。核心步骤:

```c
/* kernel/time/hrtimer.c,简化自 __hrtimer_start_range_ns @ L1219 */
static int __hrtimer_start_range_ns(struct hrtimer *timer, ktime_t tim,
                                    u64 delta_ns, const enum hrtimer_mode mode,
                                    struct hrtimer_clock_base *base)
{
    ...
    remove_hrtimer(timer, base, true, force_local);          /* 先把自己从老位置摘掉(若已入队) */
    if (mode & HRTIMER_MODE_REL)
        tim = ktime_add_safe(tim, base->get_time());         /* 相对时间 → 绝对时间 */
    hrtimer_set_expires_range_ns(timer, tim, delta_ns);      /* 设置 _softexpires=tim, node.expires=tim+delta_ns */
    ...
    new_base = switch_hrtimer_base(timer, base, ...);        /* 必要时迁移到别的 CPU */
    first = enqueue_hrtimer(timer, new_base, mode);          /* ← 插入红黑树 */
    ...
}
```

`enqueue_hrtimer` 是真正干插入的([hrtimer.c:1086-1099](../linux/kernel/time/hrtimer.c#L1086-L1099)):

```c
/* kernel/time/hrtimer.c,enqueue_hrtimer */
static int enqueue_hrtimer(struct hrtimer *timer, struct hrtimer_clock_base *base,
                           enum hrtimer_mode mode)
{
    debug_activate(timer, mode);
    WARN_ON_ONCE(!base->cpu_base->online);

    base->cpu_base->active_bases |= 1 << base->index;        /* 置 active_bases 位图 */
    WRITE_ONCE(timer->state, HRTIMER_STATE_ENQUEUED);         /* 标记已入队 */
    return timerqueue_add(&base->active, &timer->node);       /* 插入红黑树,返回是否新的最左 */
}
```

两个细节值得注意:

- **`active_bases` 位图**:8 个 base 哪个非空,对应 bit 置 1。这样扫"所有 base 找最早到期"时不用扫 8 棵空树,只扫有位的——这和 softirq 的 per-CPU pending 位图(第 6 章)是同一套路:位图消灭"扫空"。
- **`timerqueue_add` 返回是否新的最左**:如果新 timer 比原来的最左还早(成了新的最早到期者),返回 true,调用方据此决定要不要重编硬件(`hrtimer_reprogram`)。

### 找最早到期:__hrtimer_next_event_base

一个 CPU 上挂着的 timer 散在 8 棵树上,要回答"下一个该被叫醒的是谁",得对 8 棵树各取最左再求 min。这是 [`__hrtimer_next_event_base`](../linux/kernel/time/hrtimer.c#L505):

```c
/* kernel/time/hrtimer.c,__hrtimer_next_event_base @ L505,简化 */
static ktime_t __hrtimer_next_event_base(struct hrtimer_cpu_base *cpu_base,
                                         const struct hrtimer *exclude,
                                         unsigned int active,
                                         ktime_t expires_next)
{
    struct hrtimer_clock_base *base;

    for_each_active_base(base, cpu_base, active) {           /* 只遍历 active 位图置位的 base */
        struct timerqueue_node *next;
        struct hrtimer *timer;

        next = timerqueue_getnext(&base->active);            /* O(1) 取这棵树的最左 */
        timer = container_of(next, struct hrtimer, node);
        ...
        expires = ktime_sub(hrtimer_get_expires(timer), base->offset);  /* 减偏移,统一到 MONOTONIC 坐标系 */
        if (expires < expires_next) {
            expires_next = expires;                          /* 更新最早 */
            if (timer->is_soft)
                cpu_base->softirq_next_timer = timer;
            else
                cpu_base->next_timer = timer;
        }
    }
    if (expires_next < 0)                                    /* clock_was_set 可能让 offset 变化,夹到 0 */
        expires_next = 0;
    return expires_next;
}
```

> 见 [hrtimer.c:505-549](../linux/kernel/time/hrtimer.c#L505-L549)。`for_each_active_base` 是个宏([hrtimer.c:502-503](../linux/kernel/time/hrtimer.c#L502-L503)),用 `__ffs` 找 `active_bases` 第一个置位的 bit,只遍历非空 base——这是 `active_bases` 位图的好处。

注意**减 offset 这步**:不同 base 用不同时钟坐标系(REALTIME 和 MONOTONIC 差一个墙上时间偏移),要比较必须统一到同一坐标系(MONOTONIC)。`base->offset` 就是"这个时钟相对于 MONOTONIC 偏多少"。比较完得到 `expires_next` 这个绝对时刻,再编进 clock_event_device。

### 到期处理:__hrtimer_run_queues → __run_hrtimer

硬件中断(或低精度回退 / 软中断)进来后,要"把所有已到期的 timer 摘下来跑回调"。这是 [`__hrtimer_run_queues`](../linux/kernel/time/hrtimer.c#L1724):

```c
/* kernel/time/hrtimer.c,__hrtimer_run_queues @ L1724,简化 */
static void __hrtimer_run_queues(struct hrtimer_cpu_base *cpu_base, ktime_t now,
                                 unsigned long flags, unsigned int active_mask)
{
    struct hrtimer_clock_base *base;
    unsigned int active = cpu_base->active_bases & active_mask;   /* 只看本次要扫的(硬或软) */

    for_each_active_base(base, cpu_base, active) {
        struct timerqueue_node *node;
        ktime_t basenow = ktime_add(now, base->offset);           /* 当前时刻在该时钟坐标系下的值 */

        while ((node = timerqueue_getnext(&base->active))) {      /* 一直取最左 */
            struct hrtimer *timer = container_of(node, struct hrtimer, node);

            if (basenow < hrtimer_get_softexpires_tv64(timer))   /* ← 关键!判软到期,不是硬到期 */
                break;                                            /* 这个 timer 还没软到期,后面更晚,break */

            __run_hrtimer(cpu_base, base, timer, &basenow, flags); /* 摘下来跑回调 */
            ...
        }
    }
}
```

> 见 [hrtimer.c:1724-1761](../linux/kernel/time/hrtimer.c#L1724-L1761)。

**这里有个反直觉的细节**:判"该不该摘下来跑",用的是 `_softexpires`(软到期),不是 `node.expires`(硬到期)!为什么?

因为只要当前时刻 `now >= _softexpires`,就说明这个 timer 进入了它的"可被唤醒区间",现在叫醒它是合规的(不早于 soft)。一旦叫醒,这次中断顺便把这棵树上其他进入区间的 timer 一起摘——**一次中断处理一批**,而不是每个 timer 一个精确中断。这就是 14.5 节要讲的 softexpires 的真意。源码注释([hrtimer.c:1741-1752](../linux/kernel/time/hrtimer.c#L1741-L1752))说得清楚:用 softexpires 是为了"minimizing wakeups",而不是"as early as possible"。

摘下来后,调 [`__run_hrtimer`](../linux/kernel/time/hrtimer.c#L1649) 真正跑回调:

```c
/* kernel/time/hrtimer.c,__run_hrtimer @ L1649,简化 */
static void __run_hrtimer(struct hrtimer_cpu_base *cpu_base,
                          struct hrtimer_clock_base *base,
                          struct hrtimer *timer, ktime_t *now, unsigned long flags)
{
    ...
    base->running = timer;                                 /* 标记"这个 base 上我正在跑这个 timer" */
    raw_write_seqcount_barrier(&base->seq);                /* 内存序屏障,配合 hrtimer_active 的无锁读 */
    __remove_hrtimer(timer, base, HRTIMER_STATE_INACTIVE, 0);  /* 先从红黑树摘掉 */
    fn = timer->function;
    raw_spin_unlock_irqrestore(&cpu_base->lock, flags);    /* ← 释放锁再跑回调! */
    ...
    restart = fn(timer);                                   /* 跑用户回调(可能很慢,所以先放锁) */
    raw_spin_lock_irq(&cpu_base->lock);
    if (restart != HRTIMER_NORESTART &&
        !(timer->state & HRTIMER_STATE_ENQUEUED))
        enqueue_hrtimer(timer, base, HRTIMER_MODE_ABS);    /* 回调要求 RESTART,再插回去 */
    raw_write_seqcount_barrier(&base->seq);
    base->running = NULL;
}
```

> 见 [hrtimer.c:1649-1722](../linux/kernel/time/hrtimer.c#L1649-L1722)。

两个 sound 的点:

- **跑回调前先从树摘掉 + 释放锁**:回调可能很慢(TCP 重传要发包、调度 hrtick 要重排队列),如果一直持着 `cpu_base->lock`,其他 CPU 想给这个 cpu_base 加 timer 会被堵死。所以先摘掉(避免被重复摘)、放锁、再跑回调,回调里甚至可以 `hrtimer_forward` 给自己重新定时(此时会再次入队)。
- **`base->running` + seqcount_barrier 保护跨 CPU 的"它还在跑吗"判断**:回调跑着的时候锁是放的,别的 CPU 可能想 `hrtimer_cancel` 它——怎么知道它还在跑?看 `base->running == timer`(配合 seqcount 无锁读,见 `hrtimer_active`)。这个并发正确性细节在技巧精解里展开。

---

## 14.5 技巧精解之一:softexpires 区间——消中断风暴的根本

这是 hrtimer 最反直觉、也最精妙的设计。我们单独拆透。

### 问题:朴素精确到期会怎样

假设你朴素地写:每个 timer 都给一个精确到期时间,硬件就编到这个精确时刻,到点叫醒它。考虑这种场景:

- 1000 个网络连接,每个都有一个 TCP 重传定时器,到期时间散布在接下来 10ms 内,但很多相距只有几微秒。
- 调度器 100 个任务的 hrtick,每个的时间片 deadline 散布在接下来 1ms 内,密集到纳秒级。

朴素精确到期会怎样?

- **中断风暴(thundering herd)**:每个 timer 触发一次硬件中断,1000 个 timer 在 10ms 内能打出 1000 次中断,CPU 几乎全在处理中断打断,正经业务跑不动。
- **重编程开销爆炸**:每加一个更早到期的 timer,都要重编硬件(`tick_program_event`),这是写硬件寄存器的慢操作,频繁重编也贵。

### 解法:给每个 timer 一个 slack 区间

hrtimer 的招:**让 timer 接受一个"可被提前唤醒的区间"**。timer 实际挂在树上的关键字是 `node.expires`(硬到期,最晚),但它还有一个 `_softexpires`(软到期,最早)。`[_softexpires, node.expires]` 是这个 timer 的合法唤醒窗口——在这个窗口里任何时刻被叫醒都算它合规。

```c
/* hrtimer_set_expires_range_ns,@ hrtimer.h L109 */
static inline void hrtimer_set_expires_range_ns(struct hrtimer *timer,
                                                ktime_t time, u64 delta)
{
    timer->_softexpires = time;                          /* 用户给的时刻就是软到期 */
    timer->node.expires = ktime_add_safe(time, ns_to_ktime(delta));  /* 硬到期 = 软 + slack */
}
```

> 见 [hrtimer.h:109-113](../linux/include/linux/hrtimer.h#L109-L113)。

现在看 14.4 节那个 `__hrtimer_run_queues` 里**判软到期**的判断(`basenow < _softexpires` 才 break):一旦硬件中断在某个时刻 `now` 触发(这个 `now` 是由树上**最早硬到期**的那个 timer 触发的),内核不只摘最早那一个,而是**把所有 `_softexpires <= now` 的 timer 一起摘下来**——它们都进入了合法窗口,顺手叫醒。

**效果**:多个 timer 的合法窗口重叠时,它们被同一次中断集体叫醒。比如 1000 个 TCP timer 的窗口都覆盖 `[t, t+50us]`,只要有一个 timer 在 `t` 触发了中断,这 1000 个全被这一波摘完——**1 次中断替代 1000 次**。

### 谁来给 slack

slack 不是用户每次都显式给。几条路径:

- **`hrtimer_start(timer, tim, mode)`**:这是最常用的,`delta=0`,即 `soft == hard`,精确到期——大多数 timer 不在乎那点抖动。
- **`hrtimer_start_range_ns(timer, tim, delta_ns, mode)`**:显式给 slack 区间。
- **POSIX timer(`timer_settime`)/ `nanosleep`**:内核根据 timer 的精度需求自动算 slack(比如 `CLOCK_MONOTONIC_COARSE` 给大 slack,普通 timer 给小 slack,见 `task_struct->timer_slack_ns`)。

slack 的核心洞察来自源码注释([hrtimer.c:1741-1752](../linux/kernel/time/hrtimer.c#L1741-L1752)):"The immediate goal for using the softexpires is minimizing wakeups, not running timers at the earliest interrupt after their soft expiration."——**目标是减少唤醒次数,不是尽量早跑**。代价是 timer 可能被提前到区间左端就跑(对大多数应用无所谓),换回来的是中断数大幅下降。

### 反面对比

> **不这样会怎样**:如果 timer 没有区间、只能精确到期,你会撞上三堵墙——① **中断风暴**:大量 timer 到期时间相近,每个一个中断,CPU 被中断打断淹没;② **重编程爆炸**:每加一个稍早到期的 timer 就重编硬件;③ **thundering herd**:多个等待同一时刻的进程被逐个叫醒,cache 颠簸。**softexpires 用"接受提前"换"批量唤醒"**,是 hrtimer 工程美学的核心。这正是为什么 `__hrtimer_run_queues` 判的是软到期——把"现在能合规叫醒的"全部摘掉。

---

## 14.6 技巧精解之二:并发 sound——`base->running` + seqcount,为什么 `hrtimer_cancel` 不会丢

hrtimer 的回调是在**释放 `cpu_base->lock`** 后跑的(见 14.4 节 `__run_hrtimer`),这意味着回调执行期间,别的 CPU 可能并发地想取消这个 timer。这怎么不出错?

### 问题:回调跑着,别的 CPU 想 cancel

考虑:CPU 0 正在跑 timer A 的回调(已放锁),CPU 1 上某个线程调 `hrtimer_cancel(A)`。CPU 1 拿锁、查树,发现 A 不在树上(已经被摘了),它怎么知道 A 的回调还在不在跑?如果回调还在跑,CPU 1 不能直接返回(调用方期望"cancel 返回时回调肯定不会再跑"),得**等回调跑完**。

### 解法:base->running 指针 + seqcount 无锁读

`hrtimer_clock_base` 有个 `running` 字段([hrtimer_defs.h:52](../linux/include/linux/hrtimer_defs.h#L52)),记录"这个 base 当前正在跑哪个 timer 的回调"。`__run_hrtimer` 跑回调前置上、跑完清掉。配合 `seq` 这个 seqcount,跨 CPU 读"它还在跑吗"是无锁的。

关键在 `__run_hrtimer` 里这两条**内存序屏障**(见 [hrtimer.c:1664-1670](../linux/kernel/time/hrtimer.c#L1664-L1670) 和 [hrtimer.c:1712-1718](../linux/kernel/time/hrtimer.c#L1712-L1718)):

```c
/* kernel/time/hrtimer.c,__run_hrtimer 里两道屏障(简化) */
    base->running = timer;
    raw_write_seqcount_barrier(&base->seq);    /* ← 屏障 1:running 写完后,再清 state */
    __remove_hrtimer(timer, base, HRTIMER_STATE_INACTIVE, 0);
    ...
    /* 回调跑完 */
    raw_write_seqcount_barrier(&base->seq);    /* ← 屏障 2:清 running 之前,state 已稳定 */
    base->running = NULL;
```

源码注释([hrtimer.c:1664-1672](../linux/kernel/time/hrtimer.c#L1664-L1672)、[hrtimer.c:1712-1718](../linux/kernel/time/hrtimer.c#L1712-L1718))直接说了目的:**防止读侧(`hrtimer_active`)看到 `running == NULL && state == INACTIVE` 这个组合**。如果没这个屏障,在弱内存序的 CPU(如 ARM)上,读者可能先看到 `running = NULL` 再看到 `state = INACTIVE`(顺序被打乱),误以为 timer 完全不活跃,从而 `hrtimer_cancel` 直接返回——但它下一行回调可能正在跑!屏障强制了顺序:跑回调期间 `running` 一定非 NULL,读者看到 `running != NULL` 就知道要等。

### 这套设计 sound 的三件事

- **回调不会丢**:`hrtimer_cancel` 看到回调在跑会等(`hrtimer_cancel_wait_running`),不会出现"cancel 返回了回调还在跑"。
- **不会死锁**:等待是用 `cpu_relax()` 自旋(等回调跑完会清 `running`),回调跑着的时候锁是放的,不会被自己堵死。
- **无锁读高性能**:判断"还在跑吗"用 seqcount,大多数情况(回调没在跑)是无锁快速路径,只有真撞上才等。

> **反面对比**:如果用一个简单的布尔 `bool callback_running` 配一把自旋锁来保护,读者每次都要拿锁,在弱内存序机器上还可能因为编译器 / CPU 重排看到错误的组合。**`running` 指针 + seqcount 屏障**把"回调是否在跑"做成了无锁快速路径 + 严格内存序,是内核里 seqcount 模式的典型应用(和 timekeeping 的 seqlock、VDSO 的 seqlock 同构,见第 10、13 章)。

---

## 14.7 三条执行路径:谁会调 __hrtimer_run_queues

`__hrtimer_run_queues` 是"扫到期的树、跑回调"的核心,但它有三个调用方,对应三条执行路径:

| 路径 | 入口 | 何时触发 | 扫哪路 | 适用场景 |
|------|------|---------|--------|---------|
| **高精度模式** | [`hrtimer_interrupt`](../linux/kernel/time/hrtimer.c#L1788) @ L1788 | clock_event_device 到点硬件中断 | `HRTIMER_ACTIVE_HARD`(硬) | 大多数现代 x86 / ARM(`CONFIG_HIGH_RES_TIMERS=y`) |
| **低精度回退** | [`hrtimer_run_queues`](../linux/kernel/time/hrtimer.c#L1901) @ L1901 | 每 jiffy 的 `run_local_timers`(老模式) | `HRTIMER_ACTIVE_HARD` | 没高精度硬件 / 高精度未启用 |
| **软中断回调** | [`hrtimer_run_softirq`](../linux/kernel/time/hrtimer.c#L1763) @ L1763 | `HRTIMER_SOFTIRQ` 被 raise | `HRTIMER_ACTIVE_SOFT`(软) | timer 被标 `is_soft`,回调想在 softirq 跑 |

### 高精度路径:hrtimer_interrupt

```c
/* kernel/time/hrtimer.c,hrtimer_interrupt @ L1788,简化 */
void hrtimer_interrupt(struct clock_event_device *dev)
{
    struct hrtimer_cpu_base *cpu_base = this_cpu_ptr(&hrtimer_bases);
    ...
    raw_spin_lock_irqsave(&cpu_base->lock, flags);
    entry_time = now = hrtimer_update_base(cpu_base);     /* 读当前时刻 */
retry:
    cpu_base->in_hrtirq = 1;
    cpu_base->expires_next = KTIME_MAX;                   /* 设成 MAX,迁移代码据此判断 */

    if (!ktime_before(now, cpu_base->softirq_expires_next)) {
        cpu_base->softirq_expires_next = KTIME_MAX;
        cpu_base->softirq_activated = 1;
        raise_softirq_irqoff(HRTIMER_SOFTIRQ);            /* 软 timer 到点了,raise 让 softirq 跑 */
    }

    __hrtimer_run_queues(cpu_base, now, flags, HRTIMER_ACTIVE_HARD);  /* ← 跑硬 timer */

    expires_next = hrtimer_update_next_event(cpu_base);   /* 算下一个最早到期 */
    cpu_base->expires_next = expires_next;
    cpu_base->in_hrtirq = 0;
    raw_spin_unlock_irqrestore(&cpu_base->lock, flags);

    if (!tick_program_event(expires_next, 0)) {           /* 编硬件:下一次在这个时刻叫我 */
        cpu_base->hang_detected = 0;
        return;
    }
    /* 编程失败(到点时刻已经过去,中断跑太久)→ retry 几次,再不行算 hang */
    ...
}
```

> 见 [hrtimer.c:1788-1877](../linux/kernel/time/hrtimer.c#L1788-L1877)。

几个 sound 点:

- **`in_hrtirq = 1` 标记**:这告诉"正在 hrtimer 中断里",别的路径(`hrtimer_reprogram`)看到这个位就不重编硬件了——知道中断处理完会重新算 `expires_next` 并编。避免重复编程。
- **hang 检测**:如果 `tick_program_event` 失败(说明算出 `expires_next` 已经是过去时刻,意味着回调跑太久、CPU 被调度走、tracing 拖慢),重试 3 次;3 次还不行就算"hang",记 `nr_hangs`,下次给一个至少 100ms 的延迟(`pr_warn_once` 报警)。这是防止 hrtimer 中断死循环的最后保险。
- **软 timer 走 raise softirq,不直接在硬中断里跑**:`__hrtimer_run_queues` 这次只传 `HRTIMER_ACTIVE_HARD`,软 timer 由 raise 出来的 `HRTIMER_SOFTIRQ` 在中断退出后(softirq 上下文,中断已开)跑。

### 软中断路径:hrtimer_run_softirq

```c
/* kernel/time/hrtimer.c,hrtimer_run_softirq @ L1763,简化 */
static __latent_entropy void hrtimer_run_softirq(struct softirq_action *h)
{
    struct hrtimer_cpu_base *cpu_base = this_cpu_ptr(&hrtimer_bases);
    ...
    raw_spin_lock_irqsave(&cpu_base->lock, flags);
    now = hrtimer_update_base(cpu_base);
    __hrtimer_run_queues(cpu_base, now, flags, HRTIMER_ACTIVE_SOFT);  /* ← 跑软 timer */
    cpu_base->softirq_activated = 0;
    hrtimer_update_softirq_timer(cpu_base, true);         /* 更新下一个软 timer 到期 */
    raw_spin_unlock_irqrestore(&cpu_base->lock, flags);
    ...
}
```

> 见 [hrtimer.c:1763-1779](../linux/kernel/time/hrtimer.c#L1763-L1779)。

这就是第 6 章 softirq 体系的一个用户:`HRTIMER_SOFTIRQ` 是注册在 `softirq_vec[]` 里的一种 softirq,`open_softirq(HRTIMER_SOFTIRQ, hrtimer_run_softirq)` 注册(见 softirq.c)。hardirq 里 raise,IRQ 退出时 `handle_softirqs` 接力跑——这正是"中断的续集"在 hrtimer 上的体现。**软 timer 的回调在 softirq 上下文跑,可以拿自旋锁(中断已开),但仍不能睡眠**(softirq 不是进程上下文)。

### 低精度回退:hrtimer_run_queues

```c
/* kernel/time/hrtimer.c,hrtimer_run_queues @ L1901,简化 */
void hrtimer_run_queues(void)
{
    struct hrtimer_cpu_base *cpu_base = this_cpu_ptr(&hrtimer_bases);
    ...
    if (__hrtimer_hres_active(cpu_base))
        return;                                           /* 高精度模式下这条路不跑 */

    if (tick_check_oneshot_change(!hrtimer_is_hres_enabled())) {
        hrtimer_switch_to_hres();                         /* 试着切到高精度 */
        return;
    }

    raw_spin_lock_irqsave(&cpu_base->lock, flags);
    now = hrtimer_update_base(cpu_base);
    ...
    __hrtimer_run_queues(cpu_base, now, flags, HRTIMER_ACTIVE_HARD);  /* 每 jiffy 扫一次 */
    ...
}
```

> 见 [hrtimer.c:1901-1931](../linux/kernel/time/hrtimer.c#L1901-L1931)。

这条路在低精度模式下(没有高精度 clock_event_device,或被 boot 参数 `highres=0` 关掉)走:每个 jiffy(`run_local_timers` 调它),扫一次硬 timer 树。注释 [hrtimer.c:1910-1916](../linux/kernel/time/hrtimer.c#L1910-L1916) 自嘲 "This _is_ ugly":它还得每 jiffy 检查"能不能切到高精度",因为时钟源切换发生在 `xtime_lock` 下,这里不能直接等通知,只能轮询。这是 hrtimer 兼容老硬件的妥协路径。

> **钉死这件事**:三条路径共享 `__hrtimer_run_queues` 这一个核心——区别只在**谁触发**(硬件中断 / jiffy / softirq)、**扫哪路**(HARD / SOFT)。这种"核心逻辑统一、触发路径多样"的设计,让 hrtimer 同时服务高精度和低精度硬件、同时服务硬中断和软中断回调,而代码不分裂。

---

## 14.8 回扣调度器:hrtick 就是 hrtimer 的薄包装

本书和上一本《Linux 调度器》紧密相关。调度器的 EEVDF 算法要"精确时间片到期就抢占",靠的就是 hrtimer。每个 CPU 的 `struct rq` 里挂着一个 `hrtick_timer`,它就是个普通的 `struct hrtimer`:

```c
/* kernel/sched/core.c,hrtick_rq_init @ L868,简化 */
static void hrtick_rq_init(struct rq *rq)
{
    ...
    hrtimer_init(&rq->hrtick_timer, CLOCK_MONOTONIC, HRTIMER_MODE_REL_HARD);
    rq->hrtick_timer.function = hrtick;       /* ← 回调就是 hrtick */
}
```

> 见 [core.c:868-874](../linux/kernel/sched/core.c#L868-L874)。

它的回调 [`hrtick`](../linux/kernel/sched/core.c#L788) 干的事很直接:

```c
/* kernel/sched/core.c,hrtick @ L788,简化 */
static enum hrtimer_restart hrtick(struct hrtimer *timer)
{
    struct rq *rq = container_of(timer, struct rq, hrtick_timer);
    ...
    rq_lock(rq, &rf);
    update_rq_clock(rq);
    rq->curr->sched_class->task_tick(rq, rq->curr, 1);   /* 触发调度类的 task_tick,通常置 need_resched */
    rq_unlock(rq, &rf);
    return HRTIMER_NORESTART;
}
```

> 见 [core.c:788-801](../linux/kernel/sched/core.c#L788-L801)。

启动它(`hrtick_start`)就是一次普通的 `hrtimer_start`:

```c
/* kernel/sched/core.c,__hrtick_restart @ L805,简化 */
hrtimer_start(timer, time, HRTIMER_MODE_ABS_PINNED_HARD);   /* PINNED:钉在本 CPU;HARD:硬中断跑 */
```

> 见 [core.c:805-811](../linux/kernel/sched/core.c#L805-L811)。

读这几行你就明白了:**调度器的 hrtick 没有任何特殊魔法,它就是本章讲的 hrtimer 的一个普通用户**。`HRTIMER_MODE_ABS_PINNED_HARD` 表示"绝对时间、钉在本 CPU 不迁移、强制硬中断跑"——因为 hrtick 必须在精确时刻抢占当前任务,不能被延迟到 softirq。hrtick timer 到期 → `hrtimer_interrupt` 摘它跑回调 → `hrtick` 置 `need_resched` → 中断返回用户态前 `__schedule` 切走当前任务。

这就是为什么上一本《调度器》讲 EEVDF 精确时间片时,反复依赖"纳秒级定时器"——那个定时器就是本章的 hrtimer。读完本章,你应该能完整讲清"调度器说'给这个任务 2ms 时间片'是怎么落地的":调度器调 `hrtimer_start(hrtick_timer, now+2ms, ...)`,把 timer 挂进本 CPU 的 MONOTONIC 硬 base 红黑树,`hrtimer_reprogram` 把这个时刻编进 clock_event_device,2ms 后硬件中断 → `hrtimer_interrupt` → `__run_hrtimer` → `hrtick` → 抢占。

---

## 14.9 ★ 对照:红黑树 vs Tokio 时间轮——精确与批量的取舍

本书特色是内核机制与用户态运行时对读。hrtimer 是最适合对照的一章。

| 维度 | Linux hrtimer | Tokio 时间轮(`tokio::time::wheel`) |
|------|---------------|--------------------------------------|
| 数据结构 | timerqueue(红黑树 + 最左缓存) | 层级时间轮(6 层,每层 64 槽) |
| 插入 | O(log n) | O(1)(按到期时间算到哪一层哪个槽) |
| 找最早到期 | O(1)(最左缓存) | O(1)(最近非空槽) |
| 精度 | 纳秒级精确(softexpires 区间内) | 毫秒级(最细一档,批量) |
| timer 规模 | 内核典型每核几百到几千 | 用户态运行时可达百万级 |
| 到期处理 | 一次中断扫一批(softexpires 对齐窗口) | 一次 tick 扫一槽,批量叫醒 |
| 减中断机制 | softexpires 区间 | 时间轮自然批量(同槽一起) |

两者解决的是同一个问题——"用一次时钟触发喂饱大量 timer"。但取舍不同:

- **内核要精确**:TCP RTO 要 sub-millisecond,调度 hrtick 要纳秒级 deadline。所以选红黑树(任何时刻能精确定位最早到期),配 softexpires 在不影响精度的前提下减中断。
- **Tokio 要吞吐**:一个高并发服务可能挂百万个 `tokio::time::sleep`,精度到毫秒就够。所以选层级时间轮(O(1) 插入、天然批量),牺牲一点精度换海量 timer 的高吞吐。

> **钉死这件事**:数据结构是工程取舍的镜子。**红黑树 = 精确 + 中等规模**,**层级时间轮 = 批量 + 海量规模**。内核 hrtimer 选前者,因为它的负载是"定时器不算特别多,但精度要求高";Tokio 选后者,因为它的负载是"定时器可能上百万,精度到毫秒就行"。没有银弹,只有取舍——这是本书反复出现的主题(softirq per-CPU 位图、preempt_count 嵌套计数,都是"用数据结构匹配问题形状"的范例)。第 21 章对照总表会汇总。

---

## 章末小结

这一章是时钟篇的核心重头戏。我们把 hrtimer 从数据结构到执行路径完整拆了一遍,核心是五样东西:

1. **三层嵌套结构**:per-CPU `hrtimer_cpu_base`(无锁)→ 8 个 `hrtimer_clock_base`(4 时钟 × 硬/软两路)→ 每棵 timerqueue 红黑树(按到期排序)。
2. **timerqueue = 红黑树 + 最左缓存**:O(log n) 插入、O(1) 取最早,替代老 timer wheel 的低精度 + 链表。
3. **softexpires 区间**:`[_softexpires, node.expires]` 给 timer 一个可被提前唤醒的窗口,让多个 timer 的窗口对齐成一次中断,消中断风暴。
4. **三条执行路径**:高精度(`hrtimer_interrupt`,硬件中断)、低精度回退(`hrtimer_run_queues`,每 jiffy 扫)、软中断回调(`hrtimer_run_softirq`,HRTIMER_SOFTIRQ 接力)。
5. **并发 sound**:`base->running` + seqcount 屏障让 `hrtimer_cancel` 跨 CPU 不会丢回调、不会死锁。

二分法上,这一章服务**内核主动**那一面——时钟硬件到点把控制权拉进内核(`hrtimer_interrupt` 是"进内核"那一面的硬件中断入口),但 hrtimer 整体是**内核主动用这个中断驱动调度与定时**(驱动调度器 hrtick、驱动 `nanosleep` 唤醒、驱动 TCP 重传),是内核"主动向外"的心跳。

### 五个"为什么"清单

1. **为什么 hrtimer 用红黑树而不是哈希桶(timer wheel)?** timer wheel 按 jiffy 分桶,精度被 HZ 钉死、桶内链表找最早 O(n);红黑树按绝对纳秒时间排序,精度和 HZ 解耦、最左缓存 O(1) 取最早。
2. **为什么每个 CPU 一份 `hrtimer_cpu_base`?** 高频并发改的数据结构首选 per-CPU 无锁化——每核只动自己的 cpu_base,不抢锁。和 softirq per-CPU 位图、调度器 per-CPU `rq` 同思路。
3. **为什么要有 softexpires 区间?** 朴素精确到期会让大量 timer 各打一个中断,形成中断风暴;softexpires 给每个 timer 一个合法唤醒窗口,让窗口重叠的 timer 被同一次中断集体叫醒——一次中断替代 N 次。
4. **为什么 `__run_hrtimer` 跑回调前要释放 `cpu_base->lock`?** 回调可能很慢(发网络包、重排调度队列),持锁会让别的 CPU 入队被堵;放锁跑回调,用 `base->running` + seqcount 屏障保证跨 CPU 的 cancel 不丢、不死锁。
5. **hrtick 和 hrtimer 什么关系?** hrtick 就是 hrtimer 的一个普通用户:`rq->hrtick_timer` 是个 `struct hrtimer`,回调是 `hrtick`,到期触发 `task_tick` 置 `need_resched` 实现 EEVDF 精确时间片抢占。没有特殊魔法。

### 想继续深入往哪钻

- **源码**:[`kernel/time/hrtimer.c`](../linux/kernel/time/hrtimer.c) 是主体,先读 `__hrtimer_run_queues`@L1724、`hrtimer_interrupt`@L1788、`__hrtimer_next_event_base`@L505、`enqueue_hrtimer`@L1086、`__run_hrtimer`@L1649;头文件 [`include/linux/hrtimer_types.h`](../linux/include/linux/hrtimer_types.h)(`struct hrtimer`)、[`include/linux/hrtimer_defs.h`](../linux/include/linux/hrtimer_defs.h)(`struct hrtimer_cpu_base`/`hrtimer_clock_base`)、[`include/linux/timerqueue.h`](../linux/include/linux/timerqueue.h)(红黑树包装)。
- **回扣调度器**:上一本《Linux 调度器》P1-04 的 hrtick,`kernel/sched/core.c` 的 `hrtick`@L788 / `hrtick_start`@L831 / `hrtick_rq_init`@L868,对照本章理解"调度器精确时间片就是 hrtimer 的薄包装"。
- **观测**:`/proc/timer_list` 能看到每个 CPU 的 `hrtimer_cpu_base` 状态(`hres_active`、`expires_next`、`nr_events`、`nr_hangs`)和挂在各 base 上的所有 hrtimer;`perf stat -e timer:hrtimer_expire_entry,timer:hrtimer_start` 看 timer 触发频率;`bpftrace` 可以 trace `hrtimer_start` / `__hrtimer_run_queues` 看是谁在频繁加 timer。
- **延伸**:`Documentation/timers/hpet.txt`、`Documentation/timers/highres.txt`(高精度原理)、Thomas Gleixner 原 hrtimer 论文 / LCA 演讲;对比 BSD `callout`(也是红黑树变种)、Go runtime 的 timer(四叉堆,另一种"堆 vs 树"取舍)。

### 引出下一章

我们讲清了 hrtimer 怎么在红黑树上挑最早到期、怎么用 softexpires 消中断风暴。但还有个绕不开的问题:周期性 tick(每 HZ 一次的中断,驱动统计和调度)在 CPU 进 idle 时还在空跑,白白烧电——一个 idle 的服务器每秒被打断 100/250/1000 次,功耗和虚拟化噪声都受不了。下一章我们讲 **tick 与 NOHZ**:进 idle 时怎么把周期 tick 停掉、让最近的 hrtimer 充当唤醒源、CPU 真正睡下去又不丢任何该到期的定时器(NOHZ idle);以及极端场景下让一个 CPU 跑单一 CPU 密集任务时几乎完全无 tick(NOHZ full)。NOHZ 的"停 tick"机制正是建立在本章的 hrtimer 之上——它把"下一个该叫醒的时刻"从固定的周期 tick 换成了 hrtimer 算出的 `expires_next`。
