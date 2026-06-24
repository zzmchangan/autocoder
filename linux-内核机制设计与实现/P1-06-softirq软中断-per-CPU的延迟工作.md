# 第六章 · softirq 软中断:per-CPU 的延迟工作

> 篇:第 1 篇 · 中断与软中断(把控制权拉进内核)
> 主线呼应:上一章(P1-05)立起了"上半部快收快放、下半部延后接力"的切分——hardirq 里只做最紧急的事,剩下的活交给下半部。下半部有三种:softirq、tasklet(softirq 的薄包装)、workqueue。这一章钻进**第一种、也是最底层的下半部 softirq**:它不在任何进程上下文里跑、它和 hardirq 共用一条退出路径、它是内核里把"延迟工作"做得最轻最快的机制。读完本章你会看清 `raise_softirq` 那一行在网卡驱动里到底是什么意思、`__irq_exit_rcu` 退出时怎么顺手把 softirq 接着跑掉、`MAX_SOFTIRQ_RESTART` 这个数字 10 是为防什么灾难、还有那个每 CPU 一个的 32 位整数凭什么能零锁承载海量收包。
> 二分法归属:**进内核**(下半部)——softirq 服务的是"事件把控制权拉进内核"那一面,它只是把上半部来不及做的活,在 hardirq 退出后**就地**接着做完,整段都还在"内核处理事件"的旅程里。

## 核心问题

**hardirq 里来不及把活干完(比如收到了包但来不及推给协议栈),为什么不能另起一个内核线程来干,而非要发明一个 softirq?softirq 凭什么用每 CPU 一个 32 位整数就能记住"有哪些活要干",而不需要锁?为什么 hardirq 一退出,softirq 几乎是"无缝"地立刻就接着跑,中间几乎不经过调度?而那个 `ksoftirqd` 内核线程又是什么角色,什么时候才轮到它出手?**

读完本章你会明白:

1. softirq 的两段式生命周期:`raise_softirq` 只是"在本 CPU 的 pending 位图上置一位",真正执行发生在 hardirq 退出时的 `__irq_exit_rcu` → `invoke_softirq` → `handle_softirqs`,全程不走调度器。
2. **per-CPU 32 位 pending 位图 + `ffs` 找位**是 softirq 又快又无锁的根:每个 CPU 一个 `__softirq_pending` 整数,置位用原子或、查位用单条 `ffs` 指令,零锁竞争。
3. **`MAX_SOFTIRQ_RESTART`(10 次)/ `MAX_SOFTIRQ_TIME`(2ms)防饿死**:softirq 会在硬中断退出点"自激"(网络 RX 处理中又触发 TX,TX 又触发 RX),无限循环会让普通进程永远拿不到 CPU;限次 + 超限唤醒 `ksoftirqd` 把它降级成一个会被调度的普通线程。
4. `handle_softirqs` 是 6.9 的真正主体(`__do_softirq` 已瘦身成它的一行包装),里面那个"先清零 pending 再开中断"的顺序是 sound 的关键。
5. ★ 对照 Tokio:softirq 的"hardirq 收事件、softirq 接力处理"两段,和 Tokio 的"mio 只取 epoll 就绪、上层 task 接力"是同构的;softirq 的 per-CPU 无锁 pending,和 Tokio 的 per-worker 队列是同一思路。

> **逃生阀**:如果你已经知道 `__softirq_pending` 是个 per-CPU 位图、知道 `irq_exit` 会顺手跑 softirq,可以直接跳到 6.3 节(主体循环拆解)和 6.5 节(防饿死技巧精解),那是本章最硬的两段。但 6.2 节的"为什么要就地处理而非另起线程"的推导,即使你懂术语也建议读——它回答了一个很多人没想过的问题。

---

## 6.1 一句话点破

> **softirq 是中断的"续集":hardirq 退出那一刻,如果本 CPU 的 pending 位图上有位置了,内核就在中断退出点(几乎还没走出 hardirq 上下文)就地接着把它处理掉,不开新线程、不进调度器、不切栈。它存在的全部理由是——延迟工作要够快、够轻,但又不能让 hardirq 自己干完(那会卡死别的中断)。所以它选了一条最省的路:用每 CPU 一个 32 位整数记账,在 hardirq 退出点顺手跑掉。**

这是结论,不是理由。本章倒过来拆:先看为什么 hardirq 退出后还要"再处理一次"而不是把活留给某个线程,再看那个 32 位整数怎么承载记账、那个 10 次的循环上限防的是什么灾难,最后看清 `ksoftirqd` 这个兜底线程到底什么时候才出场。

---

## 6.2 为什么是"就地处理",而不是"另起线程"

先把问题摆正。上一章(P1-05)已经讲清:hardirq 里不能干重活——中断上下文不是进程、不能睡眠、长处理会让被屏蔽的其他中断饿死。所以网卡驱动上半部干完最紧急的事(把包从网卡 ring buffer 拷走、给硬件回个 ACK),就调用 [`raise_softirq(NET_RX_SOFTIRQ)`](../linux/kernel/softirq.c#L687) 标记"有一批网络收包的活待会儿处理",然后从中断返回。

那么问题来了:这批活,为什么不能**立刻丢给一个内核线程**去干(就像 workqueue 那样)?为什么非要发明一个软中断?

设想三种处理这批活的方式,逐一掂量:

**方案 A:全在 hardirq 里干完。** 网卡上半部收完包,直接在 hardirq 上下文里调协议栈把包分发出去。这最简单,但有两个致命问题:① hardirq 里通常会**屏蔽本 CPU 的其他中断**(至少同优先级及以下),协议栈处理可能要几百微秒,这期间别的中断全被卡住;② hardirq 不能睡眠,而协议栈某些路径(比如 socket buffer 分配失败要等内存)需要能阻塞。这条路堵死。

**方案 B:立刻唤醒一个专门的内核线程去干。** 把活挂到某个队列上,`wake_up_process()` 唤醒一个线程。这能解决"不能睡眠"的问题(线程有进程上下文),但代价是**一次调度**:内核要切到那个线程(可能等它被调度到、要切页表、要走完整的上下文切换),延迟从"立刻"变成"等下一次调度点"。对于一个网卡每秒收十万包的场景,每包都走一次调度切换,CPU 大半时间花在切换上。这条路太重。

**方案 C:在 hardirq 退出点"就地"接着干。** 这是 Linux 选的路。hardirq 干完最紧急的活,退出中断(`irq_exit`)那一刻,**内核顺手检查一下本 CPU 有没有 pending 的 softirq 位**,有就在原地(中断退出点)接着跑完它。这中间**不发生调度、不切线程、不切栈(在 x86 上 softirq 跑在独立的 irq 栈或当前任务栈上)**。所以延迟几乎为零——上半部刚放下,下半部紧接着就接力,对被打断的用户进程而言,它的时间片只是被"借走"了稍长一点的一整段(hardirq + softirq),而不是被切成两次。

> **不这样会怎样**:如果选方案 A(全在 hardirq 干),其他中断会被卡死、系统响应延迟暴涨;如果选方案 B(立刻起线程),每包一次调度切换,高 pps 网络场景下 CPU 全耗在切换上,而且调度延迟让收包的端到端延迟从微秒级跳到几十微秒。方案 C 是唯一兼顾"不卡中断"和"不切线程"的路。

那么,这个"就地处理"具体在哪个时机触发?答案在 [`__irq_exit_rcu`](../linux/kernel/softirq.c#L627):

```c
/* kernel/softirq.c,简化自 __irq_exit_rcu @ L627 */
static inline void __irq_exit_rcu(void)
{
    local_irq_disable();              /* 关中断,保证下面原子 */
    account_hardirq_exit(current);
    preempt_count_sub(HARDIRQ_OFFSET); /* 退出 hardirq 上下文:计数减回去 */
    if (!in_interrupt() && local_softirq_pending())
        invoke_softirq();             /* 关键:不在任何中断嵌套里 + 有 pending,就跑 softirq */
    tick_irq_exit();
}
```

注意两个条件:**`!in_interrupt()`** 和 **`local_softirq_pending()`**。第一个条件是最容易被忽略的精妙之处——只有当**退出这层 hardirq 后,不再处于任何中断嵌套里**(没有更高优先级的中断把我们打断、也没有 softirq 正在跑),才就地跑 softirq。如果还在嵌套里(比如 hardirq 里又嵌了一个 NMI、或者本来就在 softirq 里被硬中断打断),就不就地跑,把活留着,等最外层退出时再处理。这避免了 softirq 在奇怪的嵌套上下文里递归执行。

第二个条件 `local_softirq_pending()` 就是读那个 per-CPU 32 位整数,看有没有位置 1。

`invoke_softirq()` 在非 RT 内核里( [`softirq.c:419`](../linux/kernel/softirq.c#L419)):

```c
/* kernel/softirq.c @ L419,简化(非 CONFIG_PREEMPT_RT 版本) */
static inline void invoke_softirq(void)
{
    if (!force_irqthreads() || !__this_cpu_read(ksoftirqd)) {
        __do_softirq();        /* 就地跑 */
    } else {
        wakeup_softirqd();     /* 强制线程化时,唤醒 ksoftirqd */
    }
}
```

默认 `force_irqthreads()` 是 false(只有开 `threadirqs` 启动参数或 RT 内核才强制把中断线程化),所以常态下走 `__do_softirq()` 这条就地路径。而 [`__do_softirq`](../linux/kernel/softirq.c#L586) 在 6.9 已经瘦身为:

```c
/* kernel/softirq.c @ L586,完整(没简化) */
asmlinkage __visible void __softirq_entry __do_softirq(void)
{
    handle_softirqs(false);
}
```

**就是一行**——它把所有活都交给了 [`handle_softirqs(false)`](../linux/kernel/softirq.c#L511)。这是 6.9 的重要演进:老资料里讲的"`__do_softirq` 含主循环"早已过时,主体逻辑全部搬到了 `handle_softirqs`。

> **钉死这件事**:softirq 选择"在 hardirq 退出点就地处理",是为了**避免一次调度切换**——它把上半部和下半部拼成一段连续的内核执行,既不卡中断(下半部开中断跑)、又不切线程(没有调度开销)。`__irq_exit_rcu` 用 `!in_interrupt()` 这个条件保证只在最外层退出时跑一次,避免嵌套递归。这是 softirq 整个设计的出发点。

下面这一节的流程图把这条"就地处理"路径完整画出来。

```mermaid
flowchart TD
    HW["硬件中断到达<br/>CPU 被拉进内核"] --> ENTER["irq_enter_rcu<br/>preempt_count_add(HARDIRQ_OFFSET)"]
    ENTER --> HANDLER["hardirq handler<br/>驱动上半部:拷包/回 ACK"]
    HANDLER --> RAISE["raise_softirq(NET_RX_SOFTIRQ)<br/>or_softirq_pending(1&lt;&lt;NET_RX)"]
    RAISE --> EXIT["__irq_exit_rcu<br/>preempt_count_sub(HARDIRQ_OFFSET)"]
    EXIT --> CHECK{"!in_interrupt()<br/>&& local_softirq_pending()?"}
    CHECK -->|"否(还在嵌套里/无 pending)"--> RETURN["中断返回用户态/被中断的内核代码"]
    CHECK -->|"是"--> INVOKE["invoke_softirq<br/>__do_softirq()"]
    INVOKE --> DO["handle_softirqs(false)<br/>softirq_handle_begin<br/>循环 ffs(pending) 调 action"]
    DO --> MORE{"还有 pending<br/>且未超 10 次/2ms?"}
    MORE -->|"是"| DO
    MORE -->|"否,还有 pending"| WAKE["wakeup_softirqd<br/>交给内核线程"]
    MORE -->|"否,pending 清空"| RETURN
    classDef hw fill:#fee2e2,stroke:#dc2626
    classDef irq fill:#fef3c7,stroke:#d97706
    classDef soft fill:#dbeafe,stroke:#2563eb
    classDef wake fill:#dcfce7,stroke:#16a34a
    class HW hw
    class ENTER,HANDLER,RAISE,EXIT,CHECK,INVOKE irq
    class DO,MORE soft
    class WAKE wake
    class RETURN soft
```

这张图是本章的全景。下面三节我们把它拆开:6.3 拆 `handle_softirqs` 主体循环,6.4 拆 pending 位图怎么无锁记账,6.5 拆那个 10 次的循环上限。

---

## 6.3 主体循环:`handle_softirqs` 逐行拆解

`handle_softirqs` 是 softirq 的心脏,60 多行代码浓缩了 softirq 的全部精妙。我们一段段看(对应 [`softirq.c:511-584`](../linux/kernel/softirq.c#L511-L584))。

### 准备阶段:记账、进 softirq 上下文

```c
/* kernel/softirq.c @ L511,简化 */
static void handle_softirqs(bool ksirqd)
{
    unsigned long end = jiffies + MAX_SOFTIRQ_TIME;  /* 2ms 截止时间 */
    int max_restart = MAX_SOFTIRQ_RESTART;            /* 最多重启 10 次 */
    struct softirq_action *h;
    __u32 pending;
    int softirq_bit;

    /* 借用当前进程上下文跑,屏蔽 PF_MEMALLOC 防止 softirq 借机用紧急内存 */
    current->flags &= ~PF_MEMALLOC;

    pending = local_softirq_pending();   /* 读本 CPU 的位图快照 */

    softirq_handle_begin();              /* __local_bh_disable_ip(SOFTIRQ_OFFSET):
                                          * preempt_count += SOFTIRQ_OFFSET,标记"正在跑 softirq" */
    account_softirq_enter(current);
```

这里有几个要点:

1. **参数 `ksirqd`**:true 表示这次是从 `ksoftirqd` 内核线程调进来的,false 表示从 `__do_softirq`(hardirq 退出点)调进来。后面 `MAX_SOFTIRQ_RESTART` 那段对两者一视同仁,但 RCU 的 quiescent state 上报(`rcu_softirq_qs()`)只在 ksoftirqd 上下文做。
2. **`pending = local_softirq_pending()`** 读的是 per-CPU 位图的**快照**。注意:后面处理时用的是这个快照,而处理过程中新 `raise_softirq` 的位会落到一个**新位图**(因为下面会 `set_softirq_pending(0)` 先清零),所以不会丢、也不会和正在处理的混在一起。这是 sound 的关键之一,6.5 节展开。
3. **`softirq_handle_begin()`** 等价于 `__local_bh_disable_ip(SOFTIRQ_OFFSET)`,把 `preempt_count` 加一个 `SOFTIRQ_OFFSET`,标记"当前在 softirq 上下文"。这样 `in_softirq()` / `in_interrupt()` 都会返回真,任何 `might_sleep()` 检查都会报警——softirq 也不能睡眠(它和 hardirq 一样,借用的是被中断进程的上下文,但 `current` 不是"为它存在的可调度实体")。所以 softirq 是"下半部"但不是"能阻塞的下半部"——能阻塞的是 workqueue。

### 主循环:`ffs` 找第一个置位的位

```c
restart:
    /* Reset the pending bitmask before enabling irqs */
    set_softirq_pending(0);     /* 先清零本 CPU 的 pending 位图 */
    local_irq_enable();          /* 开中断!softirq 处理期间允许新的硬中断 */

    h = softirq_vec;

    while ((softirq_bit = ffs(pending))) {
        unsigned int vec_nr = (h + (softirq_bit - 1)) - softirq_vec;
        int prev_count = preempt_count();

        kstat_incr_softirqs_this_cpu(vec_nr);
        h += softirq_bit - 1;
        trace_softirq_entry(vec_nr);
        h->action(h);            /* 调对应 softirq 的处理函数 */
        trace_softirq_exit(vec_nr);

        if (unlikely(prev_count != preempt_count())) {
            pr_err("huh, entered softirq ... with preempt_count %08x, exited with %08x?\n", ...);
            preempt_count_set(prev_count);   /* 修复不平衡 */
        }
        h++;
        pending >>= softirq_bit;
    }
```

这段是全章最硬的代码,逐句拆:

**① `set_softirq_pending(0)` 先清零,再 `local_irq_enable()` 开中断。** 这两句的顺序是性命攸关的。设想反着来(先开中断、再清零):清零动作和"处理这批 pending"是分开的两步,中间如果来一个硬中断,硬中断里又 `raise_softirq`,那这个 raise 会落到**旧的位图上**(还没清零),而处理循环用的是之前读的快照 `pending`——结果这个新 raise 的位既不在快照里(所以本轮不处理)、又会被紧接着的 `set_softirq_pending(0)` 抹掉,**丢了**。Linux 的做法是**先清零**:清零后,处理期间任何新的 `raise_softirq` 都会落到一个干净的位图上,等本轮处理完再检查这个新位图,该跑的还会跑。这是"先记账、后处理"的标准无锁模式,和信号 pending 队列的处理思路一致。

**② `local_irq_enable()` 开中断。** softirq 在处理期间**允许新的硬中断抢占**。这和 hardirq 形成鲜明对比:hardirq 通常关中断跑(怕被自己嵌套),softirq 开中断跑(它没那么紧急,被打断也没事)。这就是为什么上半部要尽快退出、把活交给下半部——下半部能让中断重新响应。

**③ `while ((softirq_bit = ffs(pending)))`。** `ffs(find first set)` 是一条 CPU 指令(x86 上是 `bsf`),返回 `pending` 里第一个为 1 的位号(从 1 开始数,0 表示没有)。`pending` 是一个 32 位整数,每一位对应一种 softirq(见下表)。比如 `pending = 0b1000`(即 bit 3 = NET_RX_SOFTIRQ),`ffs` 返回 4(位号从 1 数),`softirq_bit - 1 = 3`,正好是 `NET_RX_SOFTIRQ` 的枚举值。然后 `h += softirq_bit - 1` 把指针跳到 `softirq_vec[3]`,调它的 `action`。处理完一个,`pending >>= softirq_bit` 把刚处理过的位(及更低位)移出去,继续找下一个。这是 **O(位数) 的扫描**,而位数最多 10,实际几乎都是 1~2 位。

**④ `h->action(h)`** 是真正干活的回调。`softirq_vec` 是全局数组,每一项是一个 [`struct softirq_action`](../linux/include/linux/interrupt.h#L591),里面只有一个 `action` 函数指针:

```c
/* include/linux/interrupt.h @ L591 */
struct softirq_action {
    void (*action)(struct softirq_action *);
};
```

谁在 `softirq_vec[nr].action` 里注册了自己的回调?用 [`open_softirq`](../linux/kernel/softirq.c#L703):

```c
/* kernel/softirq.c @ L703,完整 */
void open_softirq(int nr, void (*action)(struct softirq_action *))
{
    softirq_vec[nr].action = action;
}
```

内核启动时各子系统调 `open_softirq` 注册。比如 [`softirq_init`](../linux/kernel/softirq.c#L900) 注册 tasklet:

```c
/* kernel/softirq.c @ L911 */
open_softirq(TASKLET_SOFTIRQ, tasklet_action);
open_softirq(HI_SOFTIRQ, tasklet_hi_action);
```

定时器、网络、块 IO、RCU 各自注册。`softirq_vec` 是**全局唯一**的(所有 CPU 共用一份 action 表),因为同一种 softirq 的处理逻辑在哪都一样——差别在每个 CPU 的 pending 位图不同,各跑各的。这又是一个"全局静态策略 + per-CPU 动态数据"的分离,和上一本《调度器》的全局 `sched_class` + per-CPU `rq` 同构。

Linux 一共定义了 10 种 softirq(见 [`interrupt.h:551`](../linux/include/linux/interrupt.h#L551)):

| 枚举值 | 名字 | 谁注册的 action | 干什么 |
|--------|------|----------------|--------|
| 0 | `HI_SOFTIRQ` | `softirq_init` | 高优先级 tasklet(老 BH 时代遗物) |
| 1 | `TIMER_SOFTIRQ` | 定时器子系统 | 老低精度 timer wheel(已被 hrtimer 取代大半,但 timer 还在) |
| 2 | `NET_TX_SOFTIRQ` | 网络协议栈 | 发包的下半部 |
| 3 | `NET_RX_SOFTIRQ` | 网络协议栈 | **收包的下半部(NAPI)** |
| 4 | `BLOCK_SOFTIRQ` | 块 IO 子系统 | 块设备完成回调 |
| 5 | `IRQ_POLL_SOFTIRQ` | block polling | 块设备轮询优化 |
| 6 | `TASKLET_SOFTIRQ` | `softirq_init` | 普通 tasklet |
| 7 | `SCHED_SOFTIRQ` | 调度器 | 负载均衡、`try_to_wake_up` 的延迟部分 |
| 8 | `HRTIMER_SOFTIRQ` | hrtimer | hrtimer 的"软中断模式"(高精度模式直接在 hardirq 里跑) |
| 9 | `RCU_SOFTIRQ` | RCU | RCU 宽限期推进的回调 |

> 注释里( [`interrupt.h:545`](../linux/include/linux/interrupt.h#L545))内核专门留了一句"PLEASE, avoid to allocate new softirqs"——softirq 编号是稀缺资源,新加一种要改全局 enum,且会无差别地在每个 CPU 的位图里占一位。所以内核强烈建议:能用 tasklet(softirq 的包装)就用 tasklet,能用 workqueue 就用 workqueue,别轻易新加 softirq。

**⑤ `preempt_count` 不平衡检查。** `action` 调用前后 `preempt_count` 应当不变(softirq 的 action 不应该改 preempt_count)。如果变了,说明 action 里有 bug(比如拿了锁忘放、或者偷偷开了关抢占的 section),内核打印一条警告并把 count 修回去。这是软中断的"防御性编程"——因为 softirq 的 action 由各子系统提供,内核不信任它们。

### 收尾阶段:重检 pending,决定 restart 还是甩给 ksoftirqd

```c
    local_irq_disable();                /* 关中断,准备做决策 */

    pending = local_softirq_pending();  /* 再读一次:处理期间有没有新 raise 的? */
    if (pending) {
        if (time_before(jiffies, end) && !need_resched() &&
            --max_restart)
            goto restart;               /* 还有活、没超时、没人想抢占、次数没到:再来一轮 */

        wakeup_softirqd();              /* 否则:甩给 ksoftirqd 内核线程 */
    }

    account_softirq_exit(current);
    softirq_handle_end();               /* preempt_count -= SOFTIRQ_OFFSET,退出 softirq 上下文 */
}
```

处理完一轮(快照里所有位都跑了),内核**再读一次 pending 位图**。因为处理期间(开了中断),很可能又来了硬中断、又 raise 了新的 softirq——比如网络收包处理(`net_rx_action`)的过程中,协议栈可能要发包、又 `raise_softirq(NET_TX_SOFTIRQ)`。这个新 raise 的位会在这一刻被发现。

然后做三个检查:

1. **`time_before(jiffies, end)`**:从进入到现在没超过 2ms(`MAX_SOFTIRQ_TIME`)。
2. **`!need_resched()`**:没有更高优先级的任务在等调度(没有进程被饿得嗷嗷叫)。
3. **`--max_restart`**:重启次数还没到 10(`MAX_SOFTIRQ_RESTART`)。

三者都满足,`goto restart`——再清零、再开中断、再跑一轮。只要还有 pending 且条件满足,就一路 restart 下去。一旦任意一个不满足,**立刻 `wakeup_softirqd()` 把剩下的活甩给 `ksoftirqd` 内核线程**,自己退场——把 CPU 还给被中断的进程。

这"三个上限"是 softirq 不会把系统搞死的关键,6.5 节专门拆。

> **钉死这件事**:`handle_softirqs` 的核心是"先清零 pending 位图再开中断、用快照 pending 驱动本轮 ffs 循环、处理完重检新 pending 决定 restart 或甩锅"。三件事缺一不可:清零在开中断前(防丢),开中断(让硬中断能响应),restart 三上限(防饿死)。

---

## 6.4 per-CPU pending 位图:凭什么零锁

现在回到那个最基础的问题:softirq 怎么记账"有哪些活要干"?答案你已经看到了——一个 32 位整数。但这个整数到底存在哪、怎么操作,值得单独拆。

### 数据结构:per-CPU 的 `irq_stat`

Linux 给每个 CPU 准备了一个 [`irq_cpustat_t`](../linux/kernel/softirq.c#L56) 类型的 per-CPU 变量(叫 `irq_stat`),里面有一个字段 `__softirq_pending`,就是那个 32 位位图:

```c
/* kernel/softirq.c @ L56,per-CPU 定义 */
DEFINE_PER_CPU_ALIGNED(irq_cpustat_t, irq_stat);
```

(`irq_cpustat_t` 的完整定义在 `asm-generic/hardirq.h` 之类的体系结构相关头里,核心就是 `__softirq_pending` 这个字段。)

对这个字段的三种操作,封装成三个宏(见 [`interrupt.h:529`](../linux/include/linux/interrupt.h#L529)):

```c
/* include/linux/interrupt.h @ L526-L531 */
#define local_softirq_pending_ref irq_stat.__softirq_pending
#define local_softirq_pending()   (__this_cpu_read(local_softirq_pending_ref))
#define set_softirq_pending(x)    (__this_cpu_write(local_softirq_pending_ref, (x)))
#define or_softirq_pending(x)     (__this_cpu_or(local_softirq_pending_ref, (x)))
```

注意三个宏都用 `__this_cpu_*` 前缀(不是 `this_cpu_*`)——前者假定调用者已经关了抢占或在本 CPU 上,不做迁移保护,更快。

置位的入口是 [`__raise_softirq_irqoff`](../linux/kernel/softirq.c#L696),它就是一行 `or_softirq_pending(1UL << nr)`:

```c
/* kernel/softirq.c @ L696,完整 */
void __raise_softirq_irqoff(unsigned int nr)
{
    lockdep_assert_irqs_disabled();
    trace_softirq_raise(nr);
    or_softirq_pending(1UL << nr);
}
```

`1UL << nr` 把第 `nr` 位置 1,`or_softirq_pending` 把它"或"进本 CPU 的位图。这是**单条原子或指令**(x86 上是 `or` 内存操作),不需要任何锁——因为只有本 CPU 会改自己的位图。

### 为什么零锁成立:`raise` 总在本 CPU

这里有个容易被忽略的前提:**softirq 的 pending 位图是"谁 raise 谁的"**。网卡驱动在 CPU 3 上收到中断,在 CPU 3 的 hardirq 里 `raise_softirq(NET_RX_SOFTIRQ)`,置的是 **CPU 3 的位图**——于是 NET_RX_SOFTIRQ 会在 CPU 3 上跑。它**不会**跑去 CPU 5 上执行。

这意味着:`raise_softirq` 永远改的是**当前 CPU** 的位图,而 `handle_softirqs` 也只读/清**当前 CPU** 的位图。同一份位图只有本 CPU 上的代码会碰——hardirq 里的 raise、softirq 里的重检、可能还有本 CPU 上 ksoftirqd 线程的读取,但这些都串行在本 CPU 上发生(中间靠关中断互斥)。**没有跨 CPU 共享,所以不需要锁。**

> **不这样会怎样**:如果 pending 是一个**全局**位图(所有 CPU 共用一份),那么每次 `raise_softirq` 都要抢一把全局自旋锁,64 核机器上每秒十万次收包,光这把锁就够把 CPU 占满——这就是 softirq 要极力避免的。per-CPU 位图把"记账"这件事彻底分散,每个 CPU 自己管自己的账。这和上一本《调度器》的 per-CPU `rq`、第 8 本《内存分配器》的 per-cpu cache、上一本 mm 的 per-cpu pageset 是同一套思路:**凡是高频并发改的计数,首选 per-CPU 无锁化**。

### 两个变体:`raise_softirq` vs `raise_softirq_irqoff`

`raise_softirq` 有两个对外接口,差别在"是否已经关中断"([softirq.c:670](../linux/kernel/softirq.c#L670) 和 [softirq.c:687](../linux/kernel/softirq.c#L687)):

```c
/* 已经关中断的版本(hardirq 里调) */
inline void raise_softirq_irqoff(unsigned int nr)
{
    __raise_softirq_irqoff(nr);            /* 置位 */
    if (!in_interrupt() && should_wake_ksoftirqd())
        wakeup_softirqd();                  /* 不在中断里,直接唤醒 ksoftirqd 兜底 */
}

/* 没关中断的版本(进程上下文里调,如 __tasklet_schedule) */
void raise_softirq(unsigned int nr)
{
    unsigned long flags;
    local_irq_save(flags);          /* 自己关中断 */
    raise_softirq_irqoff(nr);
    local_irq_restore(flags);       /* 恢复 */
}
```

注意 `raise_softirq_irqoff` 里那个 `if (!in_interrupt())`:如果在**进程上下文**(不在任何中断里)调用 raise,它会**直接唤醒 ksoftirqd**,而不是等 `irq_exit` 那条路径——因为进程上下文里不会经过 `__irq_exit_rcu`,如果不主动唤醒,这个 softirq 可能要等到下一次硬中断才被处理。tasklet 的 `__tasklet_schedule` 就走这条路( [`softirq.c:731`](../linux/kernel/softirq.c#L731))。

这个细节也回答了一个常见疑问:"为什么我在进程上下文 `raise_softirq` 之后,softirq 没有立刻跑?"——因为它跑在 `ksoftirqd` 线程里,而 `ksoftirqd` 是普通 SCHED_NORMAL 线程,要等调度器选中它。在 hardirq 里 raise 的才会走"就地处理"的快路径。

> **钉死这件事**:softirq 的"零锁"建立在"per-CPU + 本 CPU 自己改自己"上。`__softirq_pending` 是每 CPU 一个的 32 位整数,raise 用 `or_softirq_pending`(单条原子或指令)、查位用 `ffs`(单条指令)、清零用 `set_softirq_pending(0)`(单条写)。三个操作都只碰本 CPU 数据,跨 CPU 不共享,所以无锁。这是 softirq 又快又 sound 的物理基础。

---

## 6.5 防饿死:`MAX_SOFTIRQ_RESTART` 和 `ksoftirqd`

到目前为止 softirq 看起来完美:无锁、就地、快。但有一个潜伏的灾难——**softirq 自激**。

### 灾难场景:softirq 自激

设想一台繁忙的网络服务器,网卡每秒收十几万包。每次收包中断:

1. hardirq 上半部:拷包、`raise_softirq(NET_RX_SOFTIRQ)`。
2. `irq_exit` → `handle_softirqs` → `net_rx_action`:把包推给协议栈。
3. 协议栈处理过程中,要回 ACK,于是发包,`raise_softirq(NET_TX_SOFTIRQ)`。
4. TX softirq 跑完,网卡发了出去,对方回包,又触发 RX 中断……
5. 而且 RX 处理本身,如果 socket buffer 不够,会触发内存分配,内存紧张又触发回收,回收路径可能又 raise 别的 softirq……

如果 `handle_softirqs` 不加上限、只要还有 pending 就一直 restart,会怎样?**它会无限循环下去**,因为这个场景下 pending 几乎永远有位。结果是:被中断的用户进程**永远拿不回 CPU**——softirq 一直占着 CPU,用户态被活活饿死。系统的 load average 看起来正常(因为 softirq 不算进程),但实际业务根本没推进。

这就是 softirq 的"自激"陷阱。Linux 用两个上限把它压住:

```c
/* kernel/softirq.c @ L475-L476 */
#define MAX_SOFTIRQ_TIME    msecs_to_jiffies(2)   /* 最多跑 2ms */
#define MAX_SOFTIRQ_RESTART 10                     /* 最多 restart 10 次 */
```

`handle_softirqs` 收尾的判断([softirq.c:572](../linux/kernel/softirq.c#L572)):

```c
if (pending) {
    if (time_before(jiffies, end) && !need_resched() &&
        --max_restart)
        goto restart;
    wakeup_softirqd();     /* 任一上限触发:甩给 ksoftirqd */
}
```

只要满足"还没到 2ms、没人想被调度、restart 次数 < 10"三个条件,就继续。否则——**哪怕还有 pending,也立刻退场,把剩下的活甩给 `ksoftirqd` 内核线程**。

### `ksoftirqd`:把 softirq 降级成一个会被调度的线程

`ksoftirqd` 是每 CPU 一个的内核线程,名字叫 `ksoftirqd/N`(N 是 CPU 号)。它是一个**普通的 SCHED_NORMAL 线程**,优先级默认Nice 0,和其他进程一样参与调度。它跑的还是 `handle_softirqs`,只是参数传 `true`:

```c
/* kernel/softirq.c @ L920 */
static void run_ksoftirqd(unsigned int cpu)
{
    ksoftirqd_run_begin();              /* local_irq_disable() */
    if (local_softirq_pending()) {
        handle_softirqs(true);          /* 注意传 true */
        ksoftirqd_run_end();            /* local_irq_enable() */
        cond_resched();                 /* 主动让出,允许调度 */
        return;
    }
    ksoftirqd_run_end();
}
```

这里有一个关键的差别:`ksoftirqd` 是**普通线程**,它会被调度器像对待任何进程一样调度——`need_resched()` 会让它让出 CPU、别的更高优先级的任务能抢占它、它的时间片用完了也得排队。所以把 softirq 甩给 `ksoftirqd`,本质是**把"无约束的自激循环"降级成"受调度器公平管辖的普通工作"**——softirq 该干的活还是干(`handle_softirqs` 主体一样),但不再能独占 CPU。

`ksoftirqd` 是怎么被唤醒的?两条路:

1. **就地处理超限时**:`handle_softirqs` 收尾 `wakeup_softirqd()`( [`softirq.c:75`](../linux/kernel/softirq.c#L75))调 `wake_up_process(ksoftirqd)`。
2. **进程上下文 raise 时**:`raise_softirq_irqoff` 里的 `if (!in_interrupt()) wakeup_softirqd()`。

`ksoftirqd` 的注册用 `smpboot_register_percpu_thread`( [`softirq.c:979`](../linux/kernel/softirq.c#L979)),每 CPU 一个,`thread_should_run` 检查 `local_softirq_pending()` 决定要不要跑。

### 这个设计的精妙:常态快路径 + 异常慢路径

把整个 softirq 系统看成两条路:

- **快路径(hardirq 退出就地处理)**:常态。绝大多数 softirq 在 `irq_exit` 那一刻就被处理掉,延迟最低、零调度开销。条件是不超 2ms、不超 10 次、没人抢占。
- **慢路径(ksoftirqd 线程)**:异常。当 softirq 负载过重(自激、洪水),快路径扛不住时,自动切到慢路径,把 softirq 降级成普通线程,让调度器来公平分配 CPU,保护用户进程不被饿死。

这两条路**共用同一个 `handle_softirqs`**,只差一个 `ksirqd` 参数(影响 RCU 上报时机)。设计极其对称:正常情况你享受快路径的好处,异常情况你被自动降级保平安。

> **不这样会怎样**:如果没有 `MAX_SOFTIRQ_RESTART`/`MAX_SOFTIRQ_TIME` 上限,softirq 自激会让用户进程永远拿不到 CPU,系统看上去 load 低实则业务停滞(所谓"softirq 风暴");如果没有 `ksoftirqd` 兜底,光有限次还不行——剩下的 pending 没人处理,数据包会堆积、socket buffer 会爆。两者配套,才既快又不失控。这是 Linux 工程美学的典范:**用同一个核心函数(`handle_softirqs`)+ 一组上限 + 一个降级线程,把"快"和"稳"都拿到手**。

---

## 6.6 技巧精解:先清零再开中断 + ffs 找位

这一节把 `handle_softirqs` 里两个最反直觉、最容易写错的技巧单独拆透。

### 技巧一:先 `set_softirq_pending(0)` 再 `local_irq_enable()`——为什么顺序不能反

回头再看那两行([softirq.c:536-538](../linux/kernel/softirq.c#L536-L538)):

```c
restart:
    /* Reset the pending bitmask before enabling irqs */
    set_softirq_pending(0);
    local_irq_enable();
```

注释就一句话:"在开中断前重置 pending 位图"。但这背后是一道并发正确性的难题。

**问题**:softirq 处理用的是之前读的快照 `pending`(在循环入口 `pending = local_softirq_pending()` 读的),但处理期间(`local_irq_enable()` 之后)随时可能来硬中断、又 `raise_softirq`。这个新 raise 的位会落到哪?如果不清零,它就和正在处理的旧位混在同一个位图里;如果清零时机不对,又会把它丢掉。

**反面对比 1(朴素错误:不清零)**:假设不调 `set_softirq_pending(0)`,处理期间新 raise 的位会"或"进当前位图。这看起来"没丢",但有问题——本轮 ffs 循环用的是快照 `pending`,新 raise 的位本轮根本不会被处理(快照已经定了)。它要等下一轮 restart 才被读到。但下一轮 restart 又是"重新读 `local_softirq_pending()`"——所以不清零似乎也能工作?问题在于:如果**不 restart**(到了上限、甩给 ksoftirqd),那么这一轮 ffs 用过的位还在位图里,ksoftirqd 接手时会**再处理一遍**已经处理过的位——重复处理。

**反面对比 2(朴素错误:先开中断后清零)**:

```c
/* 错误顺序(示意,非源码) */
local_irq_enable();           /* 先开中断 */
set_softirq_pending(0);       /* 再清零 */
```

这两行之间(开中断后、清零前)如果来一个硬中断,中断里 `raise_softirq` 把位置上了。紧接着 `set_softirq_pending(0)` 一抹,**这个 raise 丢了**。因为本轮 ffs 用的快照 `pending` 是开中断前读的(没包含这个新位),而位图又被清零了——既不在快照里、又不在位图里,彻底消失。

**正确做法(源码实际顺序)**:先清零、再开中断。清零时还关着中断(关中断是从 hardirq 上下文继承下来的、本 CPU 没人会抢),所以清零是原子的;清零后开中断,此后任何新的 raise 都会落到一个干净的位图上,等本轮 ffs 跑完、收尾 `pending = local_softirq_pending()` 再读一次,新位自然进入下一轮判断。**不丢、不重**。

> **为什么这套设计 sound**:它实质是一个"快照 + 双缓冲"模式——本轮处理用旧快照,新数据写进清空的位图(相当于第二缓冲),处理完两缓冲合并检查。这种"先记账到新位图、本轮处理旧快照、收尾合并"的思路,在内核很多地方都有(信号 pending 队列的 `collect_signal`、hrtimer 的红黑树摘节点再处理),都是为了避免"处理中和新增的并发冲突"。

### 技巧二:`ffs(pending)`——单条指令扫描位图

`while ((softirq_bit = ffs(pending)))` 这一行看起来平平无奇,但它是 softirq "O(1) 找待处理"的物理基础。

`ffs(find first set)` 在 x86 上编译成 `bsf`(bit scan forward)指令——**单条 CPU 指令**,在寄存器里扫描第一个为 1 的位,返回位号。对于一个 32 位整数,无论第几位是 1,`bsf` 都在常数周期内完成(实际是 1~3 个周期)。

朴素地写,找第一个置位的位会写成循环:

```c
/* 朴素写法(示意,非源码) */
for (int i = 0; i < NR_SOFTIRQS; i++) {
    if (pending & (1 << i)) {
        /* 处理第 i 位 */
        ...
    }
}
```

这要 10 次比较 + 10 次位测试,即便没有位置 1 也要扫完。`ffs` 直接跳到第一个置位的位,且 `pending >>= softirq_bit` 把刚处理的位及更低位整体移出去,下一次 `ffs` 直接找下一个——如果只有 2 个位置位,就只扫 2 次。

配合 per-CPU 位图(每个 CPU 自己的整数),`ffs` 操作的是寄存器里的本 CPU 数据,无 cache line 共享、无锁。**"per-CPU 数据 + 单条位扫描指令"的组合,让"找待处理事件"这件事变成几乎零开销**。这是 softirq 能扛住每秒十万次以上 raise + 处理的根。

> **钉死这件事**:`handle_softirqs` 的两个核心技巧——"先清零再开中断"(防丢防重的双缓冲)和"ffs 找位"(O(1) 扫描 per-CPU 位图)——合起来,让 softirq 在 zero-copy、zero-lock、zero-schedule 的前提下完成"延迟工作"。任何一处写成朴素的"链表 + 锁"或"循环扫位",softirq 的性能优势就会立刻蒸发。这也是为什么内核会把这 60 行代码保护得这么仔细——它太基础,任何一个并发 bug 都会让 softirq 在高负载下崩溃。

---

## 6.7 ★ 对照 Tokio:hardirq 收事件、softirq 接力

softirq 的"两段切分"和用户态运行时的事件模型天然同构。把内核和 Tokio 放一起看:

| 层 | 第一段(收事件,极简) | 第二段(处理事件,可放手做) | 待处理记账 |
|---|---|---|---|
| **内核 softirq** | hardirq 上半部:网卡拷包、`raise_softirq(NET_RX)` | softirq (`net_rx_action`):推包给协议栈 | per-CPU 32 位 `__softirq_pending` 位图,`ffs` 扫 |
| **Tokio** | `mio` 调 `epoll_wait` 只取就绪事件 | 上层 async task 接力处理 | per-worker task 队列,工作窃取 |
| **io_uring** | 内核写 SQE → CQE 完成环(无中断) | 用户态主动轮询 CQE | SPSC 环 + `smp_store_release`/`acquire` |

三组对照里,softirq 和 Tokio 最像:

- **两段切分同构**:内核 hardirq 像 `mio`——只做"事件到了"这一件最小的事(网卡拷包 / epoll 取 fd),不干重活;softirq 像 Tokio 的 async task——把事件真正处理掉(协议栈 / 业务逻辑)。
- **per-X 无锁记账同构**:softirq 用 per-CPU 位图(每 CPU 一个 32 位整数),Tokio 用 per-worker task 队列(每 worker 一份,工作窃取只在 steal 时碰别人的)。都是"把高频并发的记账分散到 per-unit 数据结构上,消灭锁竞争"。
- **降级机制同构**:softirq 自激时降级到 `ksoftirqd`(普通线程,受调度);Tokio 任务阻塞时会用 `spawn_blocking` 丢到独立的 blocking 线程池(避免阻塞 reactor)。两者都是"快路径扛不住时自动切到慢路径,慢路径受公平管辖"。

差别也在:**softirq 不能睡眠**(它仍在中断退出上下文),而 Tokio 的 async task 可以 `.await`(它有 future 状态机);要能睡眠的延迟工作,内核用 workqueue(下一章 P1-07),那才是真正对应 Tokio async task 的东西——worker 线程有进程上下文,能拿阻塞锁、能 `schedule()`。

记住这组对照,P1-07(workqueue)和第 5 篇收尾的总表会反复回扣。

---

## 章末小结

这一章我们把 softirq 的"就地处理、per-CPU 位图、防饿死三件套"完整拆了一遍。softirq 是**最底层的下半部**:它不在任何进程上下文里跑,它和 hardirq 共用同一条退出路径,它的全部存在感就是"在 hardirq 退出那一刻顺手把延迟工作干掉,既不卡中断又不切线程"。回到全书的二分法——**softirq 服务的是"进内核"那一面**:它仍是"事件把控制权拉进内核"旅程的一部分,只不过这段旅程从 hardirq 延伸到了中断退出点。

五个"为什么"清单:

1. **为什么 hardirq 退出后不另起线程处理 softirq?** 另起线程要走一次完整调度切换(切上下文、等调度点),高 pps 网络场景下 CPU 全耗在切换上,且延迟从微秒级跳到几十微秒;就地处理几乎零调度开销,把上半部和下半部拼成连续执行。
2. **softirq 凭什么用每 CPU 一个整数就能记账、不要锁?** pending 位图是 per-CPU 的,raise 永远改本 CPU 的位图,handle 也只读本 CPU 的位图——同一份位图只有本 CPU 的代码(关中断互斥)会碰,跨 CPU 不共享,所以无锁。
3. **`MAX_SOFTIRQ_RESTART`(10)和 `MAX_SOFTIRQ_TIME`(2ms)防的是什么?** 防 softirq 自激——网络 RX 触发 TX、TX 又触发 RX,pending 几乎永远有位,无限 restart 会让被中断的用户进程永远拿不回 CPU。限次 + 超限甩给 `ksoftirqd`,把无约束循环降级成受调度器管辖的普通线程。
4. **`ksoftirqd` 和就地处理的 softirq 用的是同一套逻辑吗?** 是,都跑 `handle_softirqs`,只差一个 `ksirqd` 参数(影响 RCU 上报)。差别在 `ksoftirqd` 是普通 SCHED_NORMAL 线程,会被调度、会被抢占、会 `cond_resched()`,所以它扛得住"过载"但延迟比就地处理高。
5. **为什么 `handle_softirqs` 要"先清零 pending 再开中断"?** 防丢防重。清零在开中断前(此时关中断、原子),清零后开中断,处理期间新 raise 的位落到干净位图上,本轮用旧快照处理、收尾重检新位图——双缓冲模式,既不丢新 raise 也不重复处理旧位。

### 想继续深入往哪钻

- **源码**:[`kernel/softirq.c`](../linux/kernel/softirq.c) 是本章全部事实的来源——`handle_softirqs`@L511(主体)、`__do_softirq`@L586(瘦身后的一行包装)、`__irq_exit_rcu`@L627(就地处理入口)、`invoke_softirq`@L419(非 RT)、`raise_softirq`@L687 / `raise_softirq_irqoff`@L670 / `__raise_softirq_irqoff`@L696、`open_softirq`@L703、`softirq_vec`@L60、`run_ksoftirqd`@L920、`softirq_init`@L900。位图宏在 [`include/linux/interrupt.h`](../linux/include/linux/interrupt.h#L523) 的 `local_softirq_pending` 一组(@L529-L531),softirq 枚举在 @L551-L565。`preempt_count` 的 bit 段定义在 [`include/linux/preempt.h`](../linux/include/linux/preempt.h#L38) 一组。
- **观测**:`cat /proc/softirqs` 看每 CPU 各 softirq 的累计计数(一眼看出哪个 CPU 在扛网络收包);`cat /proc/interrupts` 看硬中断分布;`perf stat -e 'softirq*'` 或 `perf report` 看 softirq 时间占比;`watch -n1 cat /proc/softirqs` 实时看 NET_RX 涨速判断 pps;`bpftrace` 用 `tracepoint:irq:softirq_entry` / `softirq_exit` 追踪每次 softirq 的入口出口和耗时;`ftrace` 的 `softirq` 事件组。
- **调参**:`threadirqs` 启动参数强制把所有中断(包括 softirq)线程化,用于 RT 调试——这时 `force_irqthreads()` 返回 true,`invoke_softirq` 走 `wakeup_softirqd` 而不是就地处理;`isolcpus` 把某些 CPU 隔离出来不跑 ksoftirqd,降低延迟噪声。
- **延伸**:想看 softirq 的真实用武之地,读网络子系统的 NAPI(`net_rx_action`)——它是 NET_RX_SOFTIRQ 的 action,实现了"中断 + 轮询"混合的收包模型;RCU 子系统的 `rcu_core` 是 RCU_SOFTIRQ 的 action,推进宽限期。这两个是 softirq 最重的用户。
- **下一章**:softirq 不能睡眠——但很多延迟工作需要调阻塞 API(磁盘 IO、`mutex`)。下一章 P1-07 讲 **workqueue**:它提供**有进程上下文**的延迟工作,worker 内核线程能 `schedule()`、能拿阻塞锁,是真正对应 Tokio async task 的内核机制。CMWQ(concurrency managed workqueue)的 worker 池管理是它的招牌技巧。

---

> 走到这里,你已经看清了内核"事件处理"在最底层的那一段:hardirq 收事件 → softirq 接力。下一段(workqueue)会把"接力"延伸到能睡眠的线程上下文,再下一段(系统调用 P2 篇)会切换视角,看用户态怎么主动合法地跨进这条边界。整条旅程,都是这四个机制在轮流出场。
