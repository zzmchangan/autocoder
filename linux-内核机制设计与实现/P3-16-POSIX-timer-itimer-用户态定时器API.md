# 第十六章 · POSIX timer/itimer:用户态定时器 API

> 篇:P3 时钟与定时器
> 主线呼应:前两章(P3-14 hrtimer、P3-15 tick 与 NOHZ)讲的全是**内核自己用**的时钟——调度器借 hrtick 抢占、tick 推进统计、NOHZ 让 idle CPU 睡。可用户态也有一堆定时器需求:`sleep(3)`、`setitimer` 定时给 `SIGALRM`、`gprof` 性能采样靠 `setitimer(ITIMER_PROF)`、`timer_create` 设一个能在多线程间挑目标的精确纳秒定时器、`prlimit` 的 `RLIMIT_CPU` "CPU 跑超就杀进程"。这些**用户态 API** 都靠不到一个真的"系统调用直接计时"——而是**内核把用户态的定时器请求,映射成已经在跑的 hrtimer 或进程 CPU 时间定时器**。读完本章你会发现:第 14 章那套 hrtimer 红黑树地基,不是只给内核自己用,**它就是所有用户态定时器的最终归宿**;而 CPU 定时器(CPU 时间到了才发信号)则是时钟篇与第 4 篇信号的**接合口**——定时器到期靠信号告诉进程,本章正是时钟篇通往信号篇的桥。

## 核心问题

**用户态有 `setitimer`(BSD 时代的老接口)、`timer_create`/`timer_settime`(POSIX.1b 实时扩展)、CPU 定时器(`ITIMER_VIRTUAL`/`ITIMER_PROF`/`CLOCK_PROCESS_CPUTIME_ID`)这一大堆 API,内核是不是为每种各写一套定时器机制?当然不是——内核用一道 `k_clock` 抽象把它们统一,**墙上时间类定时器(REAL/MONOTONIC/BOOTTIME/TAI)全部内嵌一个 hrtimer**,**CPU 时间类定时器(VIRTUAL/PROF/PROCESS_CPUTIME/THREAD_CPUTIME)走另一条 `posix-cpu-timers.c` 路径**,老的 `setitimer` 只是 hrtimer 或 CPU 定时器的一层薄包装。本章讲清这套"复用而非碎片化"的设计,以及用户态定时器到期凭什么能"发出一个带数据的信号"——这正好把时钟篇接到第 4 篇信号。

读完本章你会明白:

1. **三类用户定时器 API 的内核落地**:`setitimer(ITIMER_REAL)` → 一个挂在 `signal_struct` 里的 hrtimer;`setitimer(ITIMER_VIRTUAL/PROF)` → CPU 定时器;`timer_create` → `k_itimer` 内嵌一个 hrtimer(REAL 类)或一个 `cpu_timer` 节点(CPU 类)。
2. **`k_clock` 抽象**:一个操作函数指针表,让所有时钟(REAL/MONOTONIC/TAI/BOOTTIME/PROCESS_CPUTIME/THREAD_CPUTIME/动态时钟)共用同一套 `timer_create`/`timer_settime`/`timer_del` 系统调用入口,**新加一种时钟只要填一张表**。
3. **CPU 定时器的特殊性**:它不在"墙上时间到期",而在"进程消耗的 CPU 时间到期"——所以不能挂 hrtimer 红黑树,而是挂在 `task_struct`/`signal_struct` 的 CPU 定时器链上,在每次 tick 的 `run_posix_cpu_timers` 里被检查。
4. **定时器到期与信号的接合**:POSIX timer 到期不在内核里跑用户回调(那是中断上下文,跑不了用户代码),而是**发一个携带 `siginfo`(含 `si_value` 自定义数据、`si_overrun` 溢出计数)的实时信号**(第 4 篇详讲)——定时器到期 = 内核主动发信号。

> **逃生阀**:如果你已经知道 `timer_create` 和 `setitimer` 的区别,可以直接跳到 16.4(POSIX timer 复用 hrtimer 的源码)和 16.5(CPU 定时器,本章最反直觉的一块)。16.2 的 `k_clock` 抽象建议读,它解释了"为什么加一种时钟这么便宜"。

---

## 16.1 一句话点破

> **用户态的所有定时器 API(`setitimer`/`timer_create`/CPU 定时器)都不是从零实现的——墙上时间类的全部内嵌一个 hrtimer(老的 itimer 只是 hrtimer 的一层包装),CPU 时间类的走另一条 `posix-cpu-timers.c` 路径,通过一道 `k_clock` 函数指针表让多种时钟共用一套系统调用入口。定时器到期不在内核里跑用户代码,而是发一个带 `siginfo` 的实时信号,把"时钟篇"接到"信号篇"。**

这是结论,不是理由。本章倒过来拆:先看用户态到底有哪几种定时器 API、它们各自要什么(墙上时间 vs CPU 时间 vs 单次 vs 周期),再看内核为什么不为每种各搞一套、而是统一到 hrtimer/CPU timer + `k_clock`,然后看 POSIX timer 怎么把 hrtimer 嵌进去、CPU 定时器为什么走另一条路,最后看定时器到期怎么变成一个信号。

---

## 16.2 用户态定时器 API:到底有几种,各自要什么

Linux 用户态进程能调的定时器 API 主要有三组,它们的**需求差异**决定了内核必须分两条路实现。

| API 组 | 代表函数 | 时钟基准 | 触发条件 | 到期发什么信号 |
|---|---|---|---|---|
| **BSD itimer(老接口)** | `setitimer(which, new, old)` | `ITIMER_REAL`:墙上时间(`CLOCK_REALTIME` 系);`ITIMER_VIRTUAL`:用户态 CPU 时间;`ITIMER_PROF`:用户 + 内核 CPU 时间 | 墙上时间 or CPU 时间到 | `SIGALRM`/`SIGVTALRM`/`SIGPROF`,**无附加数据** |
| **POSIX.1b interval timer** | `timer_create(clockid, evp, &id)` + `timer_settime(id, ...)` | `CLOCK_REALTIME`/`CLOCK_MONOTONIC`/`CLOCK_BOOTTIME`/`CLOCK_TAI`/`CLOCK_PROCESS_CPUTIME_ID`/`CLOCK_THREAD_CPUTIME_ID` | 同上,但支持**精确纳秒**、**绝对时间**(`TIMER_ABSTIME`)、**指定信号**和**附加数据**(`sigev_value`) | **任意实时信号**(`SIGRTMIN`~`SIGRTMAX`),携带 `siginfo`(含 `si_value`/`si_overrun`/`si_timerid`) |
| **alarm(古老)** | `alarm(seconds)` | 墙上时间,秒精度 | 秒到 | `SIGALRM` |

几条关键差异:

1. **墙上时间 vs CPU 时间**:`ITIMER_REAL`/`CLOCK_REALTIME`/`CLOCK_MONOTONIC` 这类**墙上时间定时器**,在"现实过了多久"到期,无论进程在跑还是在睡都计时;`ITIMER_VIRTUAL`/`ITIMER_PROF`/`CLOCK_PROCESS_CPUTIME_ID` 这类 **CPU 时间定时器**,在"进程**消耗的 CPU 时间**到指定值"才到期——一个进程 `sleep(10)`,它的 CPU 定时器一滴不涨,但墙上定时器正常走。**这两种时间维度根本不一样**,所以内核的实现路径必然分叉。
2. **信号能不能带数据**:`setitimer` 只能发 `SIGALRM`(普通信号,无 `siginfo` 内容);`timer_create` 发的实时信号携带 `si_value`(用户 `timer_create` 时传的 `sigevent.sigev_value`,可放一个指针或 int)、`si_overrun`(定时器溢出计数,后面讲)、`si_timerid`(哪个 timer)。这就是为什么 profiler、实时框架几乎都用 `timer_create` 而不是 `setitimer`。
3. **能不能多个共存**:一个进程的 `ITIMER_REAL` 全进程只能有一个(共用一个 `signal_struct->real_timer`);`timer_create` 可以创建任意多个(每个一个 `k_itimer`,在 `signal_struct->posix_timers` 链表上)。

> **钉死这件事**:用户态这些 API 看起来琳琅满目,但**内核只有两种底层机制**:墙上时间类(复用 hrtimer)和 CPU 时间类(走 `posix-cpu-timers.c`)。`setitimer(ITIMER_REAL)` 和 `timer_create(CLOCK_REALTIME,...)` 在内核里**殊途同归——都最终驱动一个 hrtimer**;区别只在 hrtimer 挂在 `signal_struct` 里(进程级、一个)还是一个 `k_itimer` 里(可多个、带 siginfo)。

---

## 16.3 `k_clock` 抽象:为什么不为每种时钟各搞一套

面对这么多时钟(`CLOCK_REALTIME`/`CLOCK_MONOTONIC`/`CLOCK_TAI`/`CLOCK_BOOTTIME`/`CLOCK_PROCESS_CPUTIME_ID`/`CLOCK_THREAD_CPUTIME_ID`/`CLOCK_MONOTONIC_RAW`/`CLOCK_REALTIME_COARSE`/`CLOCK_BOOTTIME_ALARM`/动态时钟...),朴素地写,每个时钟各写一份 `timer_create`/`timer_settime`/`timer_del` 实现:

```c
/* 朴素的、糟糕的写法(示意,非源码) */
int timer_create_realtime(...)  { /* 一份 create */ }
int timer_create_monotonic(...) { /* 又一份 create,大部分代码重复 */ }
int timer_create_tai(...)       { /* 再一份,还是大部分重复 */ }
int timer_create_cputime(...)   { /* CPU 的,差别大,确实要单独写 */ }
/* ...每个时钟 × 每个 API = N×M 个函数 */
```

这样写**代码重复爆炸**:墙上时间类时钟(REAL/MONOTONIC/TAI/BOOTTIME)的 timer 行为几乎一样(都是挂 hrtimer、到期发信号),只是**读哪个时钟基准**不同。N 个时钟 × M 个操作 = N×M 个函数,维护噩梦。

Linux 的做法是定义一个**操作函数指针表** `struct k_clock`([kernel/time/posix-timers.h:4](../linux/kernel/time/posix-timers.h#L4-L31)):

```c
struct k_clock {
    int   (*clock_getres)(const clockid_t which_clock, struct timespec64 *tp);
    int   (*clock_get_timespec)(const clockid_t which_clock, struct timespec64 *tp);
    ktime_t (*clock_get_ktime)(const clockid_t which_clock);
    int   (*timer_create)(struct k_itimer *timer);
    int   (*timer_set)(struct k_itimer *timr, int flags, ...);
    int   (*timer_del)(struct k_itimer *timr);
    void  (*timer_get)(struct k_itimer *timr, ...);
    void  (*timer_rearm)(struct k_itimer *timr);      /* 周期 timer 重新上膛 */
    s64   (*timer_forward)(struct k_itimer *timr, ktime_t now);
    ktime_t (*timer_remaining)(struct k_itimer *timr, ktime_t now);
    int   (*timer_try_to_cancel)(struct k_itimer *timr);
    void  (*timer_arm)(struct k_itimer *timr, ktime_t expires, bool absolute, bool sigev_none);
    void  (*timer_wait_running)(struct k_itimer *timr);
    /* ... */
};
```

然后为每一类时钟填一张表,函数指针指向该类时钟的实现。墙上时间类全部共用同一套 `common_*` 函数(都内嵌 hrtimer),CPU 类填 `clock_posix_cpu` 那张表(走 `posix-cpu-timers.c`)。系统调用入口只需根据 `clockid` 查到对应的 `k_clock`,再调它指针指的函数——**调用方根本不关心这个时钟背后是 hrtimer 还是 CPU timer**:

```c
/* posix-timers.c:1514, posix_clocks[] 表 */
static const struct k_clock * const posix_clocks[] = {
    [CLOCK_REALTIME]            = &clock_realtime,
    [CLOCK_MONOTONIC]           = &clock_monotonic,
    [CLOCK_PROCESS_CPUTIME_ID]  = &clock_process,
    [CLOCK_THREAD_CPUTIME_ID]   = &clock_thread,
    [CLOCK_MONOTONIC_RAW]       = &clock_monotonic_raw,
    [CLOCK_REALTIME_COARSE]     = &clock_realtime_coarse,
    [CLOCK_MONOTONIC_COARSE]    = &clock_monotonic_coarse,
    [CLOCK_BOOTTIME]            = &clock_boottime,
    [CLOCK_REALTIME_ALARM]      = &alarm_clock,
    [CLOCK_BOOTTIME_ALARM]      = &alarm_clock,
    [CLOCK_TAI]                 = &clock_tai,
};
```

墙上时间类的四张表(`clock_realtime`/`clock_monotonic`/`clock_boottime`/`clock_tai`)几乎一模一样,只是 `clock_get_*` 不同,它们共享 `common_timer_create`/`common_timer_set`/`common_timer_del`/`common_hrtimer_arm`/`common_hrtimer_rearm` 这一整套——因为这些都是"挂 hrtimer"的活,和具体哪种时钟基准无关([posix-timers.c:1429-1512](../linux/kernel/time/posix-timers.c#L1429-L1512))。见 [clock_realtime 定义](../linux/kernel/time/posix-timers.c#L1429-L1446)、[clock_monotonic 定义](../linux/kernel/time/posix-timers.c#L1448-L1463)。

`clockid_to_kclock`([posix-timers.c:1528](../linux/kernel/time/posix-timers.c#L1528-L1541))负责查表:正数 clockid(标准时钟)直接索引 `posix_clocks[]`;负数 clockid(用户用 `clockid` 编码了某个特定 PID/TID 的 CPU 时钟,或动态注册的字符设备时钟,见 `clockid_t` 的位编码)走 `clock_posix_cpu` 或 `clock_posix_dynamic`。

> **不这样会怎样**:如果每种时钟各写一份 `timer_create`/`timer_settime`,N×M 个函数,且大部分重复代码(尤其 REAL/MONOTONIC/TAI/BOOTTIME 这四个墙上时间类的 timer 行为完全相同)。改一处 bug 要改 N 份。`k_clock` 抽象把这个矩阵折叠成**"每类时钟填一张表 + 一套通用入口"**——加一种时钟(比如将来再加 `CLOCK_XXX`)只要填一张 `k_clock` 表加进 `posix_clocks[]`,系统调用入口一个字都不用改。这是 Linux 内核"用函数指针表折叠矩阵"的典范,和第 1 篇的 `irq_chip`/`irq_domain`(P1-03)折叠五花八门的中断控制器是同一套思路。

> **所以这样设计**:用户看到的"多种定时器 API"在内核里被压成"两套底层机制(hrtimer / CPU timer)+ 一张 `k_clock` 表"。系统调用 `timer_create`/`timer_settime` 是**通用入口**,根据 clockid 选一张表,然后调表里对应的函数——这是面向对象里的"多态"在 C 里的标准实现(操作表 + 实例指针,和 VFS 的 `inode_operations`、socket 层的 `proto`、cgroup 的 `cgroup_subsys` 一脉相承)。

---

## 16.4 POSIX timer 复用 hrtimer:struct k_itimer 内嵌一个 hrtimer

现在看本章最核心的一个技巧:**POSIX timer 怎么把一个 hrtimer"嵌"进自己的结构里**,从而让 `timer_create(CLOCK_REALTIME/MONOTONIC/...)` 不用新建一套定时器机制,直接借第 14 章那套 hrtimer 红黑树跑。

### 16.4.1 k_itimer 结构:union 内嵌 hrtimer

POSIX timer 在内核里用一个 [`struct k_itimer`](../linux/include/linux/posix-timers.h#L160-L189) 表示。关键看它的 `it` 联合:

```
 struct k_itimer(简化,见 include/linux/posix-timers.h:160):

 ┌──────────────────────────────────────────────────────────┐
 │ list          : 挂到 signal_struct->posix_timers 的链表  │
 │ t_hash        : 在全局 posix_timers_hashtable 里的节点   │
 │ it_lock       : 自旋锁(保护本 timer 的字段)            │
 │ kclock        : 指向 const struct k_clock(操作表)       │
 │ it_clock/it_id: clockid 和返回给用户的 timer_t           │
 │ it_active     : 是否激活                                 │
 │ it_overrun    : 信号还没被取走时又到期的次数(溢出计数) │
 │ it_requeue_pending : "重新上膛"代际号(技巧精解里讲)    │
 │ it_sigev_notify : SIGEV_SIGNAL/SIGEV_THREAD_ID/SIGEV_NONE│
 │ it_interval   : 周期(0 = 单次)                         │
 │ it_signal     : 指向创建者的 signal_struct              │
 │ it_pid        : 信号发往哪个 PID                         │
 │ sigq          : 预分配的 sigqueue(发信号用,见 16.6)   │
 ├──────────────────────────────────────────────────────────┤
 │ union it {                                               │
 │   struct { struct hrtimer timer; } real;   ← REAL 类用   │
 │   struct cpu_timer cpu;                    ← CPU 类用    │
 │   struct { struct alarm alarmtimer; } alarm; ← ALARM 用  │
 │ };                                                       │
 ├──────────────────────────────────────────────────────────┤
 │ rcu           : RCU 释放头                               │
 └──────────────────────────────────────────────────────────┘
```

关键在 `union it`:**REAL 类/MONOTONIC/TAI/BOOTTIME 的 POSIX timer 把一个 `struct hrtimer` 嵌在 `it.real.timer` 里**——这就是"复用 hrtimer"的核心。POSIX timer 不自己实现定时器队列,而是把自己当成一个 hrtimer 的"外壳"。

> **钉死这件事**:`k_itimer` 的 `it.real.timer` 是一个**内嵌的、完整的 `struct hrtimer`**(不是指针),它会作为节点被挂到 `hrtimer_clock_base` 的红黑树上(见 [hrtimer_types.h:39](../linux/include/linux/hrtimer_types.h#L39) 的 `struct hrtimer` 定义)。这意味着第 14 章讲的那套(`hrtimer_start` 入队、`__hrtimer_run_queues` 扫到期、`hrtimer_interrupt` 处理、softexpires 区间、NOHZ 借 hrtimer 唤醒 idle CPU)对 POSIX timer **一字不改地全适用**。POSIX timer 没有任何特殊的定时器代码,它只是给 hrtimer 套了一层"带 siginfo 投递、能周期、能被 `timer_create` 标识"的用户态外壳。

### 16.4.2 timer_create:创建一个 k_itimer,初始化内嵌 hrtimer

`timer_create` 系统调用入口 [`SYSCALL_DEFINE3(timer_create,...)`](../linux/kernel/time/posix-timers.c#L530) 把用户传的 `sigevent` 拷进内核,然后调 [`do_timer_create`](../linux/kernel/time/posix-timers.c#L444-L528)。后者做四件事:

1. **查时钟操作表**:`clockid_to_kclock(which_clock)` 找到对应的 `k_clock`(REAL 类→`clock_realtime`,CPU 类→`clock_posix_cpu`)。
2. **分配一个 `k_itimer`**:[`alloc_posix_timer`](../linux/kernel/time/posix-timers.c#L401-L413) 从 `posix_timers_cache` slab 分配,并预分配一个 `sigqueue`(`sigqueue_alloc`)挂到 `sigq` 字段。**这个预分配的 sigqueue 是关键**:定时器到期要发信号,而信号投递需要 `sigqueue` 对象——提前分配好,到期时直接用,不会因为内存不足而"定时器到了却发不出信号"。
3. **填 siginfo**:`sigq->info.si_signo` = 用户指定的信号(默认 `SIGALRM`)、`si_value` = 用户传的自定义数据、`si_code = SI_TIMER`(表示"来自 POSIX 定时器")、`si_tid = timer_id`。
4. **调时钟操作表的 `timer_create`**:对墙上时间类时钟,这就是 [`common_timer_create`](../linux/kernel/time/posix-timers.c#L437-L441),一行:

```c
static int common_timer_create(struct k_itimer *new_timer)
{
    hrtimer_init(&new_timer->it.real.timer, new_timer->it_clock, 0);
    return 0;
}
```

`hrtimer_init` 把内嵌的 `it.real.timer` 初始化成一个 hrtimer,关联到 `it_clock` 对应的时钟基准(REAL/MONOTONIC/...)。此时 hrtimer 还没启动,只是"造好了子弹"。

最后 `WRITE_ONCE(new_timer->it_signal, current->signal)` + `list_add(...posix_timers)` 把它挂到当前进程的 `signal_struct->posix_timers` 链表上——这一刻起,这个 timer 对用户态"可见"了。

### 16.4.3 timer_settime:给内嵌 hrtimer 上膛

`timer_settime` 系统调用走 [`do_timer_settime`](../linux/kernel/time/posix-timers.c#L900-L938) → `kc->timer_set`(对 REAL 类是 [`common_timer_set`](../linux/kernel/time/posix-timers.c#L860-L898))。关键路径:

```c
int common_timer_set(struct k_itimer *timr, int flags,
                     struct itimerspec64 *new_setting, ...)
{
    /* 1. 先取消现有 timer(若在跑) */
    if (kc->timer_try_to_cancel(timr) < 0)
        return TIMER_RETRY;          /* 回调正在跑,告诉上层重试 */
    timr->it_active = 0;
    timr->it_requeue_pending = (timr->it_requeue_pending + 2) & ~REQUEUE_PENDING;

    /* 2. it_value 为 0 表示停 timer */
    if (!new_setting->it_value.tv_sec && !new_setting->it_value.tv_nsec)
        return 0;

    /* 3. 周期(interval)+ 到期时间(value) */
    timr->it_interval = timespec64_to_ktime(new_setting->it_interval);
    expires = timespec64_to_ktime(new_setting->it_value);

    /* 4. 调 hrtimer_arm 把内嵌的 hrtimer 挂上红黑树 */
    kc->timer_arm(timr, expires, flags & TIMER_ABSTIME, sigev_none);
    timr->it_active = !sigev_none;
    return 0;
}
```

`timer_arm` 对 REAL 类是 [`common_hrtimer_arm`](../linux/kernel/time/posix-timers.c#L783-L811):

```c
static void common_hrtimer_arm(struct k_itimer *timr, ktime_t expires,
                               bool absolute, bool sigev_none)
{
    struct hrtimer *timer = &timr->it.real.timer;
    enum hrtimer_mode mode = absolute ? HRTIMER_MODE_ABS : HRTIMER_MODE_REL;

    hrtimer_init(&timr->it.real.timer, timr->it_clock, mode);
    timr->it.real.timer.function = posix_timer_fn;     /* ← 回调指过来 */

    if (!absolute)
        expires = ktime_add_safe(expires, timer->base->get_time());
    hrtimer_set_expires(timer, expires);

    if (!sigev_none)
        hrtimer_start_expires(timer, HRTIMER_MODE_ABS);
}
```

两件事钉死:

1. **hrtimer 的回调函数设成 `posix_timer_fn`**——定时器到期时,hrtimer 子系统(`__hrtimer_run_queues`)会调这个回调,由它来发信号(16.6 节详讲)。hrtimer 本身不知道也不关心"这是个 POSIX timer",它只管"到点了调你注册的函数"。
2. **`hrtimer_start_expires` 把这个内嵌 hrtimer 挂到本 CPU 的 `hrtimer_clock_base` 红黑树上**(第 14 章详讲过)。从这一刻起,这个 POSIX timer 就和内核自己的 hrtimer(hrtick、tick、网络超时...)**混在同一棵红黑树上**,由同一套 `hrtimer_interrupt` 处理。

> **反面对比**:如果 POSIX timer 不复用 hrtimer、而是自己实现一套定时器队列,内核里就会有"内核 timer 队列"和"POSIX timer 队列"两套并行的红黑树 + 两套到期处理 + 两套 NOHZ 协调——重复实现、维护成本翻倍、bug 也翻倍。复用 hrtimer 让 POSIX timer **白捡**了 hrtimer 的纳秒精度、softexpires 优化、NOHZ 兼容、per-CPU 无锁化(第 14 章那些技巧)。"在内核里,一个机制吃干榨净再开下一个"——这是 Linux 一贯的工程哲学。

### 16.4.4 setitimer:只是 signal_struct 里一个 hrtimer 的薄包装

理解了 `timer_create` 复用 hrtimer,`setitimer` 就更简单了——它**根本不创建 `k_itimer`**。BSD 老接口的 `ITIMER_REAL` 直接复用进程 `signal_struct` 里的一个 hrtimer 字段:

```c
/* include/linux/sched/signal.h:136-159(简化) */
struct signal_struct {
    ...
    /* POSIX.1b Interval Timers */
    unsigned int    next_posix_timer_id;
    struct list_head posix_timers;       /* timer_create 创建的挂这 */

    /* ITIMER_REAL timer for the process */
    struct hrtimer  real_timer;          /* ← setitimer(ITIMER_REAL) 用的 */
    ktime_t         it_real_incr;        /* 周期 */

    /* ITIMER_PROF and ITIMER_VIRTUAL timers */
    struct cpu_itimer it[2];             /* VIRT/PROF 用,走 CPU timer 路径 */
    ...
};
```

`setitimer` 系统调用([`SYSCALL_DEFINE3(setitimer,...)`](../linux/kernel/time/itimer.c#L332))→ [`do_setitimer`](../linux/kernel/time/itimer.c#L206-L250) 根据 `which` 分三种:

- **`ITIMER_REAL`**:直接 [`hrtimer_start(&tsk->signal->real_timer, expires, HRTIMER_MODE_REL)`](../linux/kernel/time/itimer.c#L233),到期回调 [`it_real_fn`](../linux/kernel/time/itimer.c#L156-L166),它一行 `kill_pid_info(SIGALRM, SEND_SIG_PRIV, leader_pid)` 发 `SIGALRM` 给进程组长,**不携带任何自定义数据**(BSD 老接口的局限)。
- **`ITIMER_VIRTUAL`/`ITIMER_PROF`**:[`set_cpu_itimer`](../linux/kernel/time/itimer.c#L168-L198) → `set_process_cpu_timer`,走 CPU 定时器路径(16.5 节)。

注意 `it_real_fn` 的结尾 `return HRTIMER_NORESTART`——它**不像周期 hrtimer 那样自己用 `HRTIMER_RESTART` 续命**,而是"到期一次就停"。周期性靠什么?靠信号**出队路径**接管:`get_signal`([signal.c:655-664](../linux/kernel/signal.c#L655-L664))在取走 `SIGALRM` 时,若发现 `it_real_incr != 0` 且 hrtimer 已不在队列里(`!hrtimer_is_queued`),就 `hrtimer_forward` 推一个周期 + `hrtimer_restart` 重新上膛。源码注释明说这是"老式自重启 itimer"的行为,特意挪到了信号出队阶段以减少高负载非高精度系统的 timer 噪声。**这正是 POSIX timer 的 `posixtimer_rearm`(技巧精解详讲)在 itimer 上的对应物**——两者思路完全一致:不在 hrtimer 回调里自己重启,而是把"重上膛"推迟到信号被用户取走那一刻,既避免信号合并导致的周期丢失,也让出队那一刻顺路处理周期推进。

> **钉死这件事**:`setitimer(ITIMER_REAL)` 和 `timer_create(CLOCK_REALTIME, NULL, &id)` 在**底层是完全一样的东西——都驱动一个 hrtimer**。差别只在外壳:`setitimer` 复用进程级的单个 hrtimer(`signal_struct->real_timer`,全进程一个,只能发 `SIGALRM`),`timer_create` 为每个 timer 分配一个 `k_itimer`(可多个、带 siginfo、可指定信号)。**老的 BSD itimer 就是 hrtimer 的一层薄包装**——这是本章标题里"复用 hrtimer"的另一面。

---

## 16.5 CPU 定时器:CPU 时间维度,为什么走另一条路

现在看本章**最反直觉**的一块:CPU 定时器(`ITIMER_VIRTUAL`/`ITIMER_PROF`/`CLOCK_PROCESS_CPUTIME_ID`/`CLOCK_THREAD_CPUTIME_ID`)为什么不能也挂 hrtimer 红黑树?

### 16.5.1 问题:CPU 时间不是墙上时间

墙上时间(`CLOCK_REALTIME`/`MONOTONIC`)由 `clocksource` 硬件每纳秒前进,挂 hrtimer 红黑树按墙上时间排序,到期靠硬件中断触发——这套第 14 章讲过。

**CPU 时间是另一个维度**:`ITIMER_VIRTUAL` 计的是"进程在**用户态**消耗的 CPU 时间",`ITIMER_PROF` 计的是"用户态 + 内核态 CPU 时间",`CLOCK_PROCESS_CPUTIME_ID` 计的是"整个线程组消耗的 CPU 时间"。一个进程 `sleep(10)`,它的 CPU 时间**一滴不涨**(CPU 拿去跑别的进程了),所以一个 5 秒的 `ITIMER_VIRTUAL` 在一个大部分时间在 sleep 的进程上,可能好几分钟才到期。

这带来两个根本不同:

1. **没有硬件中断在 CPU 时间到指定值时触发你**:硬件时钟中断触发的是墙上时间事件(每 HZ 一次 tick),它不知道某个进程的 CPU 时间累积到哪了。所以**CPU 定时器到期检测,只能挂在"墙上 tick"上**——每次 tick,顺便检查一下当前进程的 CPU 时间有没有越过它设的定时器。
2. **CPU 时间分线程组级和线程级**:一个多线程进程,线程组 CPU 定时器到期要对整个组发信号;线程级 CPU 定时器到期只对该线程发信号。挂红黑树的 hrtimer 没有"哪个进程/线程"的概念(它是 CPU 全局的),不适合。

> **不这样会怎样**:如果把 CPU 定时器也挂 hrtimer 红黑树,那 hrtimer 的"到期时间"是墙上时间戳,根本无法表达"等这个进程跑够 5 秒 CPU 时间"——进程可能 sleep、可能被别的核的进程抢 CPU,它的 CPU 时间增长速率完全不固定。墙上时间红黑树无法承载这种"按另一个时钟维度到期"的语义。所以**CPU 定时器必须有自己的存储和检查机制**,不能复用 hrtimer。

### 16.5.2 实现:挂在 task/signal 上,tick 时检查

CPU 定时器的实现在 [kernel/time/posix-cpu-timers.c](../linux/kernel/time/posix-cpu-timers.c)。核心数据结构是 `struct posix_cputimers`(嵌在 `task_struct` 和 `signal_struct` 里),里面有按 CPUCLOCK_PROF/VIRT/SCHED 三维的 `posix_cputimer_base`,每个 base 一个最早到期时间缓存 `nextevt` 和一个 timerqueue 链表。

POSIX CPU timer(`timer_create(CLOCK_PROCESS_CPUTIME_ID,...)` 或 `CLOCK_THREAD_CPUTIME_ID`)在 [`posix_cpu_timer_create`](../linux/kernel/time/posix-cpu-timers.c#L386-L414) 里初始化一个 `timerqueue_node`(挂在 `it.cpu.node`,**不是 hrtimer**)和目标 PID:

```c
static int posix_cpu_timer_create(struct k_itimer *new_timer)
{
    ...
    new_timer->kclock = &clock_posix_cpu;
    timerqueue_init(&new_timer->it.cpu.node);   /* ← timerqueue 节点,非 hrtimer */
    new_timer->it.cpu.pid = get_pid(pid);
    ...
}
```

注意 `k_itimer.it` union 的另一支 `struct cpu_timer cpu`——CPU 类用这一支,而不是 `real.timer` hrtimer 那一支。这一支挂的是 timerqueue 节点(同样基于红黑树,但**和 hrtimer 的红黑树是分开的**),挂在目标 task/signal 的 CPU timer 队列上。

**到期检查在每次 tick 发生**。第 15 章讲过周期 tick 走 `update_process_times`([timer.c:2479](../linux/kernel/time/timer.c#L2479-L2494)):

```c
void update_process_times(int user_tick)
{
    account_process_tick(p, user_tick);   /* 累计本进程 CPU 时间 */
    run_local_timers();
    ...
    scheduler_tick();
    if (IS_ENABLED(CONFIG_POSIX_TIMERS))
        run_posix_cpu_timers();           /* ← 在这里检查 CPU 定时器 */
}
```

`run_posix_cpu_timers`([posix-cpu-timers.c:1434](../linux/kernel/time/posix-cpu-timers.c#L1434-L1455))先走 `fastpath_timer_check`(看缓存的 `nextevt` 有没有被当前 CPU 时间样本超过,没超就直接返回——绝大多数 tick 走这条 fast path),超了才进 `__run_posix_cpu_timers` 详查,调 [`check_process_timers`](../linux/kernel/time/posix-cpu-timers.c#L974-L1035)/[`check_thread_timers`](../linux/kernel/time/posix-cpu-timers.c#L897) 检查每个定时器是否到期,到期就发信号。

[`check_cpu_itimer`](../linux/kernel/time/posix-cpu-timers.c#L947-L967)(检查 itimer 的 CPU 类)最直白:

```c
static void check_cpu_itimer(struct task_struct *tsk, struct cpu_itimer *it,
                             u64 *expires, u64 cur_time, int signo)
{
    if (!it->expires)
        return;

    if (cur_time >= it->expires) {            /* 当前 CPU 时间 ≥ 设的到期值 */
        if (it->incr)
            it->expires += it->incr;          /* 周期 timer:挪一个周期 */
        else
            it->expires = 0;                  /* 单次:清零 */

        send_signal_locked(signo, SEND_SIG_PRIV, tsk, PIDTYPE_TGID);  /* 发信号 */
    }

    if (it->expires && it->expires < *expires)
        *expires = it->expires;               /* 更新最早到期缓存 */
}
```

CPU 时间样本怎么来?`check_process_timers` 调 [`proc_sample_cputime_atomic`](../linux/kernel/time/posix-cpu-timers.c#L229-L237) 从 `signal_struct->cputimer.cputime_atomic` 读——这个 atomic 计数是**每次 tick 的 `account_process_tick` 累积起来的**(utime/stime/sum_exec_runtime),正是和调度器的 PELT 同源的 cputime 统计。这就是本章开头说的"与 PELT 的关系":**CPU 定时器复用的就是调度器已经在维护的进程 CPU 时间统计**(`task->se.sum_exec_runtime`、`signal->cputimer.cputime_atomic`),它不自己另算一份。`store_samples`([posix-cpu-timers.c:214-218](../linux/kernel/time/posix-cpu-timers.c#L214-L218))把这三个数按 PROF/VIRT/SCHED 三维分拆:

```c
static inline void store_samples(u64 *samples, u64 stime, u64 utime, u64 rtime)
{
    samples[CPUCLOCK_PROF] = stime + utime;   /* PROF = 用户 + 内核 */
    samples[CPUCLOCK_VIRT] = utime;           /* VIRT = 仅用户 */
    samples[CPUCLOCK_SCHED] = rtime;          /* SCHED = sum_exec_runtime */
}
```

`it[CPUCLOCK_PROF]` 对应 `ITIMER_PROF`(到期发 `SIGPROF`)、`it[CPUCLOCK_VIRT]` 对应 `ITIMER_VIRTUAL`(到期发 `SIGVTALRM`)。`RLIMIT_CPU` 软限制超了发 `SIGXCPU`、硬限制超了发 `SIGKILL`,也在这条路径里([check_process_timers:1022-1030](../linux/kernel/time/posix-cpu-timers.c#L1022-L1030))。

> **所以这样设计**:CPU 定时器走 `posix-cpu-timers.c` 而非 hrtimer,是因为它的时间维度(CPU 时间)与墙上时间正交,挂墙上 hrtimer 红黑树语义不通;它**寄生在 tick** 上,每次 tick 顺路检查——这正是"复用已有的统计(cputime)+ 复用已有的触发(tick)"的组合拳,不引入新硬件中断源、不另算 CPU 时间。这也是为什么 `RLIMIT_CPU`(CPU 跑超就杀进程)在内核里几乎免费——它和 `ITIMER_PROF` 共用一套 CPU 定时器基础设施。

---

## 16.6 定时器到期凭什么变成一个信号

到这里有个关键问题:hrtimer 到期回调 `posix_timer_fn` 是在**硬中断上下文**(或 hrtimer softirq,取决于配置)里跑的,它**不能直接调用户的 handler**(用户代码跑在 ring 3、handler 要用户栈、用户上下文,内核态根本没法跳过去——第 4 篇详讲)。所以定时器到期**只能发一个信号**,让信号机制在进程返回用户态时把 handler 跑起来(第 18 章详讲)。

[`posix_timer_fn`](../linux/kernel/time/posix-timers.c#L310-L375)(hrtimer 到期回调)的核心:

```c
static enum hrtimer_restart posix_timer_fn(struct hrtimer *timer)
{
    struct k_itimer *timr = container_of(timer, struct k_itimer, it.real.timer);
    unsigned long flags;
    int si_private = 0;

    spin_lock_irqsave(&timr->it_lock, flags);
    timr->it_active = 0;
    if (timr->it_interval != 0)
        si_private = ++timr->it_requeue_pending;   /* 代际号,技巧精解详讲 */

    posix_timer_event(timr, si_private);            /* 发信号 */
    ...
    unlock_timer(timr, flags);
    return HRTIMER_NORESTART;                       /* 自己不重启,交给信号路径 */
}
```

`posix_timer_event`([posix-timers.c:280](../linux/kernel/time/posix-timers.c#L280-L301))做两件事:

1. 把 `si_sys_private`(就是刚才的 `si_private` 代际号)写进预分配的 sigqueue 的 info;
2. 调 `send_sigqueue(timr->sigq, timr->it_pid, type)` 把这个 sigqueue 投递到目标进程的 pending 队列。

`send_sigqueue` 是信号投递的"带数据版本"(普通 `kill` 不带数据,实时信号 `sigqueue` 带数据)——它把 `sigqueue` 挂到目标进程的 pending 队列,置 `_TIF_SIGPENDING`,**等目标进程返回用户态时**才真正跑 handler(第 4 篇详讲)。

这里有个**关键的精度问题**:定时器到期 → 信号投递 → 进程下次返回用户态才跑 handler。如果进程在跑 CPU 密集型代码,它**不会主动进内核**,信号就只能干等——但 tick 在!`scheduler_tick` 会周期性让进程进出内核,返回用户态前检查 `_TIF_SIGPENDING`,所以信号最迟一个 tick 周期(1/HZ 秒,典型 1ms 或 4ms)内被处理。NOHZ_FULL 的孤立 CPU 上进程可能不收 tick,这时靠 `task_work` 或显式 `TIF_NOTIFY_RESUME` 触发——这是 NOHZ_FULL 与 POSIX timer 的一个微妙交互(超出本章范围,第 15 章 NOHZ 提过)。

> **钉死这件事**:定时器到期 = 内核主动发信号。这正是本章作为**时钟篇→信号篇的桥**:时钟这一面(hrtimer/CPU timer 到期)产生的事件,通过信号投递传递给用户态 handler。第 4 篇(P4-17 信号投递、P4-18 信号处理入口、P4-19 sigframe)就是这条桥的另一头——本章发出的 `sigqueue` 怎么被 `complete_signal` 挂上 pending、怎么在 `exit_to_user_mode_loop` 被 `get_signal` 取出、怎么在 `__setup_rt_frame` 里把 handler 跑起来。读完第 4 篇你会看到:**一次 `timer_create` + `timer_settime` 到 handler 跑起来,横跨了时钟篇(hrtimer 到期)和信号篇(sigqueue→pending→handler)整个链路**。

---

## 16.7 技巧精解:周期 timer 的"代际号"防丢重上膛 + 预分配 sigqueue

POSIX timer 的两段最精妙代码,都和"信号投递是异步的、可能延迟"这件事有关。

### 技巧一:`it_requeue_pending` 代际号——防止信号还没被取走又重上膛,丢更新

考虑一个**周期性** POSIX timer(`it_interval > 0`),比如 `timer_settime` 设了 100ms 周期。它的行为应该是:每 100ms 到期一次、发一个信号、再等 100ms。朴素地写,hrtimer 回调到期就发信号 + `hrtimer_forward` + `HRTIMER_RESTART` 自己重启:

```c
/* 朴素的、糟糕的写法(示意,非源码) */
static enum hrtimer_restart posix_timer_fn_naive(struct hrtimer *timer)
{
    send_sigqueue(...);
    hrtimer_forward(timer, now, interval);   /* 推一个周期 */
    return HRTIMER_RESTART;                  /* 自己重启 */
}
```

这会撞墙:如果信号发出去后**用户还没取**(比如用户线程在跑别的、还没 `sigaction` handler 触发),下一个 100ms 又到期了——又发一个信号。但普通信号(包括 `SIGALRM` 这种)是**合并的**(第 17 章详讲),同编号未取走的信号只保留一份,第二个被丢。于是用户感知到"只到了一次",定时器显得不准。

更要命的是,`hrtimer_forward` 会把到期时间推一个周期,但**用户实际只看到一次信号**——overrun(错过次数)信息丢了。POSIX 标准要求 timer 提供 `timer_getoverrun()` 让用户查"我错过了几次",这个信息必须留。

Linux 的做法极其巧妙:**hrtimer 回调不自己重启**(`return HRTIMER_NORESTART`),而是把"重上膛"的活**推迟到信号被用户取走时**。具体:

1. 回调 `posix_timer_fn` 到期,发信号前 `++timr->it_requeue_pending` 生成一个新的**代际号** `si_private`,把这个代际号塞进 `sigqueue->info.si_sys_private`,然后发信号、`return HRTIMER_NORESTART`(不自己重启)。
2. 用户进程返回用户态前取信号(第 18 章 `get_signal`),取走这个 sigqueue 时,信号投递路径发现 `info.si_sys_private != 0`,调 [`posixtimer_rearm`](../linux/kernel/time/posix-timers.c#L257-L278):
   ```c
   void posixtimer_rearm(struct kernel_siginfo *info)
   {
       timr = lock_timer(info->si_tid, &flags);
       if (timr->it_interval && timr->it_requeue_pending == info->si_sys_private) {
           timr->kclock->timer_rearm(timr);    /* ← 重新上膛 hrtimer */
           timr->it_active = 1;
           timr->it_overrun_last = timr->it_overrun;
           timr->it_overrun = -1LL;
           ++timr->it_requeue_pending;
           info->si_overrun = timer_overrun_to_int(timr, info->si_overrun);
       }
       unlock_timer(timr, flags);
   }
   ```
   `timer_rearm` 对 REAL 类是 [`common_hrtimer_rearm`](../linux/kernel/time/posix-timers.c#L243-L250):
   ```c
   static void common_hrtimer_rearm(struct k_itimer *timr)
   {
       struct hrtimer *timer = &timr->it.real.timer;
       timr->it_overrun += hrtimer_forward(timer, timer->base->get_time(),
                                           timr->it_interval); /* 推所有错过的周期,数出 overrun */
       hrtimer_restart(timer);                              /* 真正重启 */
   }
   ```
   `hrtimer_forward` 返回值是"推了几个周期"——如果信号在被取走前又过了 3 个周期,`hrtimer_forward` 返回 3,累加到 `it_overrun`。用户 `timer_getoverrun()` 读的就是它。

**代际号 `it_requeue_pending` 的妙处**:它防止"取走信号后重上膛时,定时器已经被另一条路径(比如 `timer_settime` 取消重设)动过"这种竞态。`posixtimer_rearm` 在重上膛前比较 `timr->it_requeue_pending == info->si_sys_private`——如果不等,说明这个信号是"旧的"、定时器已经被重新设过,**不能再按旧周期重上膛**,直接放弃。`common_timer_set` 里那句神秘的 `timr->it_requeue_pending = (timr->it_requeue_pending + 2) & ~REQUEUE_PENDING`([posix-timers.c:881](../linux/kernel/time/posix-timers.c#L881-L882))就是取消时让代际号奇偶翻转、清掉低位 REQUEUE_PENDING 标志,使任何在飞的旧信号都失效。

> **反面对比**:如果 hrtimer 回调朴素地 `HRTIMER_RESTART` 自己续命,周期 timer 在用户来不及取信号时会持续触发但信号全被合并丢掉,`timer_getoverrun` 也无从统计——定时器既不准、又丢信息。把重上膛推迟到"信号被取走"这个同步点,既保证每次信号都被用户感知、又精确统计了 overrun。`it_requeue_pending` 代际号则是一道额外的竞态闸,防止"重上膛时定时器状态已变"。这是"把同步点选对地方"的典范——内核很多精妙设计都在做这件事(`futex` 的用户态 fast path、`wait_event` 的唤醒序、第 17 章 `complete_signal` 的 pending 合并)。

### 技巧二:预分配 sigqueue——保证"定时器到了就一定发得出信号"

看 [`alloc_posix_timer`](../linux/kernel/time/posix-timers.c#L401-L413):

```c
static struct k_itimer *alloc_posix_timer(void)
{
    struct k_itimer *tmr = kmem_cache_zalloc(posix_timers_cache, GFP_KERNEL);
    if (!tmr)
        return tmr;
    if (unlikely(!(tmr->sigq = sigqueue_alloc()))) {   /* ← 创建时就分配 */
        kmem_cache_free(posix_timers_cache, tmr);
        return NULL;
    }
    clear_siginfo(&tmr->sigq->info);
    return tmr;
}
```

`timer_create` 时就 `sigqueue_alloc` 一个 sigqueue 挂在 `k_itimer.sigq` 上,**和 timer 生命周期绑定**。到期发信号时 `send_sigqueue(timr->sigq, ...)` 直接复用它——**绝不会因为内存不足在到期那一刻分配失败**。

> **反面对比**:如果到期时才 `sigqueue_alloc` 分配,定时器在中断上下文里跑(不能睡眠、不能 GFP_KERNEL 分配),要么分配必失败(GFP_ATOMIC 在内存紧张时也失败),要么默默丢信号。更糟的是,"定时器到了却发不出信号"会让用户进程永远错过这个时间点——这是定时器语义不能接受的正确性缺陷。预分配把"可能失败的分配"挪到 `timer_create` 这个用户态能感知错误并处理的时机(`timer_create` 返回 `EAGAIN`),让到期那一刻**零分配、零失败**。这是内核"把可失败操作前移到能失败的地方"的通用技巧(LevelDB 的 `Write` 预分配 WAL、`futex` 的 `futex_wait_setup` 把可失败检查放 `futex_wake` 之外,都是同思路)。

---

## 章末小结

这一章我们把时钟篇收口到用户态:第 12 章(clocksource/clockevent 硬件抽象)、13 章(timekeeping 墙上时间)、14 章(hrtimer 红黑树)、15 章(tick 与 NOHZ)讲的都是**内核自己用**的时钟;本章讲**用户态看到的定时器 API 怎么落地**。答案是:**几乎全白嫖 hrtimer**。

1. **三类用户定时器 API**:BSD `setitimer`(老,无 siginfo)、POSIX `timer_create`(可多个、带 siginfo、精确纳秒、可指定信号)、CPU 定时器(`ITIMER_VIRTUAL`/`PROF`/`CLOCK_PROCESS_CPUTIME_ID`,按 CPU 时间而非墙上时间到期)。
2. **`k_clock` 抽象**:一张操作函数指针表折叠了"N 种时钟 × M 种操作"的矩阵,新加一种时钟只填一张表——多态在 C 里的标准实现。
3. **POSIX timer 复用 hrtimer**:`k_itimer.it.real.timer` 内嵌一个 `struct hrtimer`,挂到 `hrtimer_clock_base` 红黑树上,和内核自己的 hrtimer 混跑;`setitimer(ITIMER_REAL)` 更简,直接用 `signal_struct->real_timer` 那个 hrtimer。
4. **CPU 定时器走另一条路**:CPU 时间维度与墙上时间正交,挂不进 hrtimer 红黑树;它寄生在 tick 上,每次 tick 的 `run_posix_cpu_timers` 检查;复用调度器已在维护的 cputime 统计(与 PELT 同源)。
5. **定时器到期 = 内核主动发信号**:hrtimer 回调 `posix_timer_fn` → `send_sigqueue` 把预分配的 sigqueue 投递到目标进程 pending——本章发出的 sigqueue,就是第 4 篇要拆的信号投递链的起点。**本章是时钟篇到信号篇的桥**。

本章服务全书二分法的"**内核主动**"那一面:定时器到期由内核主动产生(hrtimer 中断或 tick 检查触发)、内核主动投递信号(把 sigqueue 挂到目标进程 pending)——用户进程完全是被动接收方。它把"时钟驱动"(内核主动驱动调度与定时)和"信号通知"(内核向进程异步通知)这两条内核主动线**接合**起来。

### 五个"为什么"清单

1. **为什么 `setitimer` 和 `timer_create` 在底层是一回事?** 两者最终都驱动一个 hrtimer:`setitimer(ITIMER_REAL)` 复用 `signal_struct->real_timer`(全进程一个),`timer_create` 给每个 timer 分配 `k_itimer`(内嵌 hrtimer)。复用而非各搞一套,内核只维护一份 hrtimer 红黑树。
2. **为什么要有 `k_clock` 抽象?** 时钟种类多(REAL/MONOTONIC/TAI/BOOTTIME/CPU/...)、操作多(create/set/get/del/rearm),朴素实现是 N×M 矩阵爆炸。`k_clock` 用函数指针表把矩阵折叠成"每类时钟一张表 + 一套通用入口",加时钟只填表。
3. **为什么 CPU 定时器不挂 hrtimer?** CPU 时间和墙上时间是正交维度,hrtimer 红黑树按墙上时间戳排序无法表达"等进程跑够 5 秒 CPU 时间";CPU 定时器寄生在 tick 上、每次 tick 检查,复用调度器已在维护的 cputime。
4. **为什么 POSIX timer 到期不直接跑用户 handler,而是发信号?** hrtimer 回调在中断上下文跑,中断上下文不是进程、不能跳到 ring 3 跑用户代码、不能访问用户栈(第 4 章 P1-04);只能发信号,让信号机制在进程返回用户态时跑 handler(第 18 章)。
5. **为什么 `timer_create` 时要预分配 sigqueue、周期 timer 重上膛要推迟到信号被取走?** 预分配保证到期那一刻零分配零失败(中断上下文分配必失败);推迟重上膛到"信号被取走"这个同步点,既让用户感知每一次到期、又能用 `hrtimer_forward` 精确统计 overrun(错过的周期数),`it_requeue_pending` 代际号防止重上膛时定时器状态已变的竞态。

### 想继续深入往哪钻

- 本章核心源码:[`kernel/time/posix-timers.c`](../linux/kernel/time/posix-timers.c)(`k_clock` 表 L1429+、`do_timer_create` L444、`common_timer_set` L860、`posix_timer_fn` L310、`posixtimer_rearm` L257、`common_hrtimer_arm` L783);[`kernel/time/posix-timers.h`](../linux/kernel/time/posix-timers.h)(`struct k_clock` L4、`struct k_itimer` 定义在 include/linux/posix-timers.h L160);[`kernel/time/itimer.c`](../linux/kernel/time/itimer.c)(`do_setitimer` L206、`it_real_fn` L156);[`kernel/time/posix-cpu-timers.c`](../linux/kernel/time/posix-cpu-timers.c)(`run_posix_cpu_timers` L1434、`check_cpu_itimer` L947、`check_process_timers` L974、`posix_cpu_timer_create` L386);[`include/linux/sched/signal.h`](../linux/include/linux/sched/signal.h)(`signal_struct->real_timer` L143、`posix_timers` L140、`it[2]` L151)。
- 想理解 hrtimer 子系统(POSIX timer 的地基),读第 14 章 + [`kernel/time/hrtimer.c`](../linux/kernel/time/hrtimer.c) 的 `__hrtimer_run_queues`(L1724)、`hrtimer_interrupt`(L1788)。
- 想理解信号投递(POSIX timer 到期的下一站),读第 17 章 + [`kernel/signal.c`](../linux/kernel/signal.c) 的 `complete_signal`(L995)、`do_send_sig_info`(L1294)、`send_sigqueue`。
- 想理解 CPU 定时器与调度统计的关系,读 [`kernel/sched/core.c`](../linux/kernel/sched/core.c) 的 `update_curr`(累积 `sum_exec_runtime`,与 PELT 同源);上一本《Linux 调度器》P1-04 hrtick 详讲了 hrtimer 怎么驱动精确抢占。
- 观测:`/proc/<pid>/timers`(进程的 POSIX 定时器列表)、`/proc/<pid>/status` 的 `CapEff`/`SigPnd`/`ShdPnd`(信号 pending)、`/proc/timer_list`(hrtimer/clockevents 全景);`perf record -e signal:signal_generate`、`bpftrace -e 'tracepoint:timer:timer_expire ...'`、`strace -e timer_create,setitimer,clock_nanosleep ./yourprog`。

### 引出下一篇

时钟篇到这里收束。我们看清了:硬件时钟(clocksource/clockevent)→ 墙上时间(timekeeping)→ 高精度 hrtimer 红黑树 → tick 与 NOHZ 省电 → 用户态定时器 API(本章,复用 hrtimer)。但本章埋了一个钩子:**定时器到期靠发信号通知用户进程**——hrtimer 回调 `posix_timer_fn` 调 `send_sigqueue` 把一个携带 siginfo 的实时信号挂到目标进程 pending。这个 `send_sigqueue` 到底做了什么?信号挂在 pending 上之后,进程凭什么能感知到?handler 在哪里跑?这套机制正是第 4 篇——**信号:内核向进程的异步通知**——的核心。第 17 章我们就从 `kill`/`tgkill`/`rt_sigqueueinfo` 的入口,拆开 `do_send_sig_info` → `complete_signal`,看一个信号怎么挂上 pending 队列、为什么普通信号会合并而实时信号不会、信号 pending 队列的数据结构长什么样。
