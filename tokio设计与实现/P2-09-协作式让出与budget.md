# 第 9 章 · 协作式让出与 budget:为什么不能让一个 task 一直占着线程

> **核心问题**:第 0 章我们立了一个全书性的伏笔——**tokio 是协作式调度,不是 OS 那种抢占式调度**。这意味着:**一个 task 不在 await 点主动让出,谁也抢不走它**。前 8 章所有的调度器机制(本地队列、work-stealing、LIFO slot、park/unpark)都建立在一个假设上——"task 会自觉在 await 点让出"。可如果有个 task **不自觉**呢?比如一个 `while true { /* 不 await 的 CPU 计算 */ }`,或者一个 `while let Some(_) = stream.next().await {}` 恰好遇到"stream 永远就绪"的 workload——**它会独占整个 worker 线程,把同线程上所有其他 task 饿死**。这种"协作式调度的命门",tokio 怎么治?
>
> 答案是本章的主角:**coop budget(协作式预算)**。每个 task 默认被给一份 **128** 次的"poll 预算",poll 一次(确切说是每消费一次 tokio 内部资源)扣一点,扣光就被强制让出。这是"协作式调度里塞一点软抢占"——task 还是自己让出(没改协作式的根本),但 tokio 用 budget 逼它"别太贪"。
>
> **读完本章你会明白**:
> - **协作式调度的命门在哪**:一个 task 不自觉让出会饿死同线程所有 task + 让 reactor/timer 推进不了(I/O 事件和定时器都卡住)。这不是"性能差",是"运行时停摆"。
> - **budget 怎么实现**:一个 `thread_local` 的 `Cell<Budget>`,budget 是个 `Option<u8>`(`Some(128)` 或 `None` 表示 unconstrained)。worker 每次 `run_task` 前 `coop::budget(|| task.run())` 把 budget 设成 128,task 在内部资源(channel recv、io read、sleep、copy 等)的 poll 里调 `poll_proceed` 扣 1,扣到 0 返回 `Pending`(让出)。
> - **为什么是 128**:这个数是"高到能摊销调度开销、低到不至于饿死别人太久"的折衷。它不是魔法数字,是经验调出来的——太高单 task 霸占太久,太低每 task 没做几件事就让出,调度开销(进队、出队、poll)摊不下来。
> - **`poll_proceed` + `RestoreOnPending` 的精妙**:`poll_proceed` 扣 1 并返回一个 `RestoreOnPending` guard,如果 task **没进展**(内层 poll 返回 Pending)就要把扣掉的预算**还回去**(drop guard 时恢复),只有"真做了事"才确认消费(`made_progress`)。这避免了"白扣预算"——一个反复 poll 一个没就绪的 fd 的 task,不会因为"poll 了 128 次"就被冤枉地让出。
> - **budget 是"任务级让出",和 LIFO slot 的"策略级让出"正交**:第 8 章讲的 LIFO slot 次数上限(防 ping-pong)和 budget(防单 task 霸占)是两套独立机制,budget 耗尽时 LIFO task 降级到队尾但 **LIFO slot 保持启用**。两者各管一种滥用,不互相干扰。
> - **opt-out 的 `unconstrained`**:如果你真的有一个"必须跑完不能让出"的 CPU 密集 task,可以用 `task::unconstrained(fut)` 把它包起来,关掉 budget。但要承担"饿死别人"的风险——tokio 把选择权留给你,但警告你。
>
> **如果一读觉得太难**:先只记住三件事——① 协作式调度的命门是"task 不让出就饿死别人",budget 是 tokio 的兜底;② 每个 task 默认 **128 次** poll 预算,扣光就强制让出,这个数在 `Budget::initial()`,注释解释了"为什么是 128";③ budget 靠 `thread_local` + `poll_proceed` API 实现,tokio 内部的 channel/io/time/copy 都在 poll 里调 `poll_proceed` 扣预算,所以"用 tokio 资源的 task 自动协作"。

---

## 章首·一句话点破

> **budget 是协作式调度的"软抢断":tokio 不能像 OS 那样用时钟中断硬抢一个跑飞的 task(那是抢占式),但它能给每个 task 发一张 128 次的"poll 配额卡",task 每用一次 tokio 资源(channel、io、sleep)就刷一次卡,刷光就请让出——下次再调度。task 还是"自己让出"(协作式的根本没变),但 tokio 用 budget 逼它"别太贪"。这是协作式调度能扛海量并发的兜底机制:没有它,一个 `while true {}` 就能让整个运行时停摆。**

这是**结论**。这一章倒过来拆:先看"协作式调度的命门"——一个不让出的 task 会造成什么灾难(饿死别人 + reactor 停摆);再拆 budget 的实现——`thread_local` 的 `Cell<Budget>` + `poll_proceed` API + `RestoreOnPending` guard;然后回答"为什么是 128"和"budget 怎么和 LIFO slot 正交";最后落到 opt-out 的 `unconstrained`,看清 tokio 把选择权留给用户的工程哲学。

第 0 章末尾立了伏笔:"协作式调度意味着任务必须自觉让出,否则 budget 机制兜底。" 这一章兑现。

---

## 一、协作式调度的命门:不让出 = 灾难

要理解 budget 为什么必须存在,得先看清"协作式调度 + 不自觉的 task"会造成什么灾难。

### 1.1 一个不让出的 task 会发生什么

考虑一个看似无害的 async 函数(来自 tokio `coop` 模块文档的真实例子):

```rust
// 来自 tokio coop 模块文档(摘录)
async fn drop_all<I: Stream + Unpin>(mut input: I) {
    while let Some(_) = input.next().await {}
}
```

([tokio/src/task/coop/mod.rs:20-22](../tokio/tokio/src/task/coop/mod.rs#L20-L22))

它从一个 stream 里读出所有元素然后丢弃。看似无害——它**有 await**,看起来"会让出"。可如果这个 stream **永远就绪**(比如一个 unbounded channel 一直在被疯狂 send),会发生什么?

> **不这样会怎样(没有 budget)**:`input.next().await` 每次都立即 `Ready`——await 点根本没真正让出(task 没真挂起,只是被立即重新调度)。于是 `while let Some(_) = ...` 变成**永不停止的紧密循环**,task 一次又一次被 poll,每次都立即返回 Ready 又被重新入队。这个 task **独占了整个 worker 线程**——worker 主循环的 ③ 拍 `run_task` poll 它,它返回 Ready(但 while 没完),又被立刻重排进队列(LIFO slot 或本地队列),worker 下次又 poll 它……**循环往复,worker 永远在 poll 这一个 task**。
>
> 灾难性后果:
> 1. **同线程其他 task 全饿死**。worker 线程上还有几百个其他 task,它们永远轮不到被 poll——因为这个贪婪 task 一直占着。
> 2. **reactor 推进不了**。worker 主循环的 ② 拍 `maintenance`(每 61 次 poll 推进一次 driver)——**61 次 poll 都花在这个贪婪 task 上了**,driver 一直没机会推进。结果:I/O 事件来了没人取(别的 task 等的 socket 数据到了,reactor 不知道,因为 driver 没轮询 epoll),定时器到点了没人叫(同样,timer 没推进)。
> 3. **整个 worker 停摆**。这个 worker 上所有"等待 I/O/时间"的 task 永远不会被唤醒——因为 reactor 没推进,它们等的事件没被检测到。
>
> 这不是"性能差",是"**运行时停摆**"。一个不自觉的 task,能让整个 worker 线程上的所有并发都死掉。这就是协作式调度的命门:**协作式的"轻"(用极少线程驱动海量任务)是建立在"任务自觉让出"的假设上的;一旦假设被打破,系统崩溃**。

### 1.2 为什么不能用抢占式(时钟中断)

你可能问:既然不让出这么致命,为什么 tokio 不学 OS,用时钟中断硬抢?

> **不这样会怎样(如果用抢占式)**:抢占式调度要求"内核能在任意指令处打断线程"——这靠**硬件时钟中断 + 内核态陷入**。tokio 跑在用户态,task 就是个普通的函数调用栈,用户态代码**没有时钟中断机制**。要做到"硬抢",只有两条路:
>
> 1. **给每个 task 配一个真正的 OS 线程**(才能被 OS 时钟中断抢)。但这就回到了 thread-per-connection(第 0 章讲的崩点)——async "用极少线程扛海量并发"的全部意义没了。10 万 task = 10 万线程 = 栈撑爆。
> 2. **用信号(SIGALRM)打断**。这条路在用户态理论上可行,但**极度危险**——信号打断的可能是 task 中间的任意指令(包括持有锁的中间态),强行切走会破坏数据结构。而且信号处理本身是异步的,跟 async 的状态机模型不兼容。
>
> 两条路都不可行。所以 async 运行时**只能协作式**——task 必须自己让出。tokio 的贡献,不是"改成抢占式",而是**"在协作式框架里,设计一个让 task 几乎无法逃避的'软抢断'机制"**——这就是 budget。

> **钉死这件事**:budget 是"协作式 + 软抢断"的妥协产物。tokio 不能硬抢(没有时钟中断),只能让 task 自己让出(协作式的根本);但 tokio 可以**逼** task 让出——通过在它用的所有资源(channel、io、time)里埋"扣预算"的钩子,预算扣光就强制让出。task 想逃避 budget,要么不用任何 tokio 资源(那它的 I/O 和同步就全废了),要么显式 opt-out(`unconstrained`,自己承担风险)。**绝大多数真实的 task 都用 tokio 资源,所以 budget 几乎无法逃避**——这就是"软抢断"的威力。

---

## 二、budget 的实现:thread_local + poll_proceed

现在拆 budget 的实现。它在 `tokio/src/task/coop/mod.rs`,核心数据结构和 API 很紧凑。

### 2.1 Budget 数据结构:Option<u8>

```rust
// tokio/src/task/coop/mod.rs(摘录)
/// Opaque type tracking the amount of "work" a task may still do before
/// yielding back to the scheduler.
#[derive(Debug, Copy, Clone)]
pub(crate) struct Budget(Option<u8>);

pub(crate) struct BudgetDecrement {
    success: bool,
    hit_zero: bool,
}

impl Budget {
    /// Budget assigned to a task on each poll.
    ///
    /// The value itself is chosen somewhat arbitrarily. It needs to be high
    /// enough to amortize wakeup and scheduling costs, but low enough that we
    /// do not starve other tasks for too long. The value also needs to be high
    /// enough that particularly deep tasks are able to do at least some useful
    /// work at all.
    ///
    /// Note that as more yield points are added in the ecosystem, this value
    /// will probably also have to be raised.
    const fn initial() -> Budget {
        Budget(Some(128))
    }

    /// Returns an unconstrained budget. Operations will not be limited.
    pub(crate) const fn unconstrained() -> Budget {
        Budget(None)
    }

    fn has_remaining(self) -> bool {
        self.0.map_or(true, |budget| budget > 0)
    }
}
```

([tokio/src/task/coop/mod.rs:94-127](../tokio/tokio/src/task/coop/mod.rs#L94-L127))

逐句拆:

**① `Budget(Option<u8>)`** —— 一个 `u8`(0~255)包在 `Option` 里。`Some(n)` 表示"还剩 n 次预算",`None` 表示"无限制"(unconstrained)。

**② `Budget::initial() = Budget(Some(128))`** —— **128 这个数**。注释把理由说得明明白白:
- "high enough to amortize wakeup and scheduling costs" —— 要够高,让 task 做够多事,摊销掉"被唤醒 + 重新调度"的开销(进队、出队、park/unpark 这些都有成本)。
- "low enough that we do not starve other tasks for too long" —— 要够低,不让别的 task 等太久。
- "high enough that particularly deep tasks are able to do at least some useful work" —— 要够高,让"调用栈很深的 task"(nested await 多层)能至少做点有用的事,而不是刚进去就让出。
- "as more yield points are added in the ecosystem, this value will probably also have to be raised" —— 随着生态里加更多 yield 点,这个值可能要往上提(因为每个 yield 点都扣预算,点越多单 task 实际做的事越少)。

**③ `Budget::unconstrained() = Budget(None)`** —— 无预算。`has_remaining` 对 `None` 返回 `true`(永远有剩余),对 `Some(0)` 返回 `false`。

> **为什么是 128?**——这是个**折衷值**,不是理论最优。128 次"消费"意味着一个 task 在被强制让出前,能调用 128 次 tokio 资源(128 次 channel recv,或 128 次 io read,或它们的组合)。在典型的 I/O 密集 workload 下,128 次消费大约对应"几毫秒到几十毫秒的 CPU 时间"——够 task 做有用的事,又不至于让别的 task 等太久。这个数和 worker 主循环的 `event_interval=61`(第 6 章)是同一量级的考量——都在"吞吐 vs 延迟"之间找平衡。

### 2.2 budget 存哪:thread_local

budget 不是存在 task 上,而是存在**线程的 thread-local** 里。看 `with_budget`:

```rust
// tokio/src/task/coop/mod.rs(摘录)
#[inline(always)]
fn with_budget<R>(budget: Budget, f: impl FnOnce() -> R) -> R {
    struct ResetGuard {
        prev: Budget,
    }

    impl Drop for ResetGuard {
        fn drop(&mut self) {
            let _ = context::budget(|cell| {
                cell.set(self.prev);
            });
        }
    }

    #[allow(unused_variables)]
    let maybe_guard = context::budget(|cell| {
        let prev = cell.get();
        cell.set(budget);

        ResetGuard { prev }
    });

    // The function is called regardless even if the budget is not successfully
    // set due to the thread-local being destroyed.
    f()
}
```

([tokio/src/task/coop/mod.rs:143-168](../tokio/tokio/src/task/coop/mod.rs#L143-L168))

关键:**`context::budget(|cell| ...)`** 访问的是当前线程的 thread-local budget cell(`Cell<Budget>`)。`with_budget` 干三件事:

1. 读出当前 budget(`prev`),保存。
2. 把 budget 设成传入值(比如 `Budget::initial()` = 128)。
3. 返回一个 `ResetGuard`,它 drop 时把 budget **恢复成 prev**。

这就是"**临时设置 budget + 自动恢复**"——budget 只在 `f()` 执行期间是新值,函数返回(guard drop)后恢复原值。

公开的 `budget` 函数包了一层:

```rust
// tokio/src/task/coop/mod.rs(摘录)
/// Runs the given closure with a cooperative task budget. When the function
/// returns, the budget is reset to the value prior to calling the function.
#[inline(always)]
pub(crate) fn budget<R>(f: impl FnOnce() -> R) -> R {
    with_budget(Budget::initial(), f)
}
```

([tokio/src/task/coop/mod.rs:131-134](../tokio/tokio/src/task/coop/mod.rs#L131-L134))

`coop::budget(f)` = `with_budget(Budget::initial() = 128, f)`——把 budget 设成 128,跑 `f`,完事恢复。

### 2.3 worker 怎么调用 budget

看 worker 主循环里 `run_task` 怎么用 budget(multi-thread):

```rust
// tokio/src/runtime/scheduler/multi_thread/worker.rs(摘录)
fn run_task(&self, task: Notified, mut core: Box<Core>) -> RunResult {
    // ... 准备工作省略(assert_owner、transition_from_searching 等)...

    // Make the core available to the runtime context
    *self.core.borrow_mut() = Some(core);

    // Run the task
    coop::budget(|| {
        // ... task hooks ...
        task.run();             // ← 真正 poll task 的地方
        // ... LIFO 循环(第 8 章)...
    });
    // ...
}
```

([tokio/src/runtime/scheduler/multi_thread/worker.rs:630-688](../tokio/tokio/src/runtime/scheduler/multi_thread/worker.rs#L630-L688),关键部分摘录)

**`coop::budget(|| { task.run(); ... })`** —— task 的整个 poll(包括 LIFO 循环里 poll 的子 task)**都被包在 `coop::budget` 里**。这就是"每个 task 被 poll 时,拿到一份 128 的预算"。current-thread 主循环同理(L795 `crate::task::coop::budget(|| future.as_mut().poll(&mut cx))` 和 L825 类似)。

> **钉死这件事**:budget 是**线程级**(thread_local)的,但**逻辑上是 task 级**的——因为 worker 每次 poll 一个 task 前,都用 `coop::budget` 把线程的 budget 重置成 128,这个 128 只服务于当前 task 的 poll。task 切换时(poll 完 A,去 poll B),budget 被重新设成 128(给 B 一份新的)。所以每个 task 都"独立享受 128 的预算",互不影响。这是 budget 设计的精髓——**用 thread_local 的存储 + 每次 poll 前重置,模拟出"每 task 一份预算"的语义,而不用在 task 结构体里加字段**。

#### 为什么用 thread_local 而不是存 task 上

> **不这样会怎样(如果存 task 上)**:要在 task 的 `Header` 或 `Core` 里加一个 `budget: u8` 字段(第 5 章讲的 task 内存布局)。这有几个问题:① **内存开销**——百万 task 每个 +1 字节,百万字节,虽然不大但能省则省;② **访问开销**——poll 时要从 task header 读 budget、写回,要定位 task 的内存位置(虽然 Header 在偏移 0,但仍是一次访存);③ **逻辑复杂**——budget 的"扣减"发生在 task 内部资源的 poll 里(channel、io 等),这些资源不知道"自己属于哪个 task",要拿到 budget 得从某处取。
>
> thread_local 解决这一切:**budget 存线程上,task 内部资源通过 `context::budget` 直接访问当前线程的 budget cell**——零额外字段、O(1) 访问(thread_local 在 x86 上用 `FS`/`GS` 段寄存器,几纳秒)、资源不需要知道 task 是谁。worker 在 poll 前"设"budget,资源 poll 时"扣"budget,完美解耦。这是 Rust 系统**编程"用 thread_local 做隐式上下文传递"的典范**(本书第 5 章讲 task 的 schedule handle 也是类似思路——task 自带 schedule,但 budget 用 thread_local 更轻)。

### 2.4 poll_proceed:扣预算的 API

task 内部资源(channel、io、time)怎么扣预算?靠 `poll_proceed`:

```rust
// tokio/src/task/coop/mod.rs(摘录)
#[inline]
pub fn poll_proceed(cx: &mut Context<'_>) -> Poll<RestoreOnPending> {
    context::budget(|cell| {
        let mut budget = cell.get();

        let decrement = budget.decrement();

        if decrement.success {
            let restore = RestoreOnPending::new(cell.get());
            cell.set(budget);

            // avoid double counting
            if decrement.hit_zero {
                inc_budget_forced_yield_count();
            }

            Poll::Ready(restore)
        } else {
            register_waker(cx);
            Poll::Pending
        }
    }).unwrap_or(Poll::Ready(RestoreOnPending::new(Budget::unconstrained())))
}
```

([tokio/src/task/coop/mod.rs:343-364](../tokio/tokio/src/task/coop/mod.rs#L343-L364))

读这段,**抓两个分支**:

- **扣成功**(`decrement.success`):budget > 0,扣 1。返回 `Poll::Ready(RestoreOnPending)`——告诉调用者"你有钱,继续干",并给一个 guard(下面详拆)。如果扣到 0(`hit_zero`),记一次 forced yield 的 metric。
- **扣失败**(budget = 0):**返回 `Poll::Pending` + 注册 waker**。这就是"强制让出"——调用它的资源(比如 channel recv)看到 Pending,自己也返回 Pending,task 整体返回 Pending,**让出线程**。

`decrement` 的实现:

```rust
// tokio/src/task/coop/mod.rs(摘录)
impl Budget {
    /// Decrements the budget. Returns `true` if successful. Decrementing fails
    /// when there is not enough remaining budget.
    fn decrement(&mut self) -> BudgetDecrement {
        if let Some(num) = &mut self.0 {
            if *num > 0 {
                *num -= 1;
                let hit_zero = *num == 0;
                BudgetDecrement { success: true, hit_zero }
            } else {
                BudgetDecrement { success: false, hit_zero: false }
            }
        } else {
            BudgetDecrement { success: true, hit_zero: false }   // None = unconstrained, 永远成功
        }
    }
    // ...
}
```

([tokio/src/task/coop/mod.rs:410-427](../tokio/tokio/src/task/coop/mod.rs#L410-L427))

`Some(num)`:num > 0 则扣 1,num == 0 则失败。`None`(unconstrained):永远成功,不扣。注意 unconstrained 模式下 `poll_proceed` 永远返回 Ready——**这就是 `unconstrained` 关闭 budget 的原理**。

### 2.5 tokio 内部资源怎么用 poll_proceed

来看 tokio 自己的资源怎么调 `poll_proceed`。`Sleep` 的 poll:

```rust
// tokio/src/time/sleep.rs(摘录)
// (来自 grep 结果,L398/L402 附近)
let coop = ready!(crate::task::coop::poll_proceed(cx));
// ... 用 coop ...
```

([tokio/src/time/sleep.rs:398-402](../tokio/tokio/src/time/sleep.rs#L398-L402))

`io::copy`、`io::util::mem`、`process` 等也都类似:

```rust
// tokio/src/io/util/copy.rs:97(摘录)
let coop = ready!(crate::task::coop::poll_proceed(cx));
// ...

// tokio/src/process/mod.rs:1140(摘录)
let coop = ready!(crate::task::coop::poll_proceed(cx));
// ...
```

([tokio/src/io/util/copy.rs:97](../tokio/tokio/src/io/util/copy.rs#L97), [tokio/src/process/mod.rs:1140](../tokio/tokio/src/process/mod.rs#L1140))

模式都一样:**资源的 poll 开头先 `ready!(poll_proceed(cx))`**——这是"先扣 1 预算,没钱就让出"的惯用法。`ready!` 宏展开是 `match poll_proceed(cx) { Ready(v) => v, Pending => return Pending }`——所以"没钱"时直接 `return Pending`,task 让出。

> **钉死这件事**:tokio 的"协作性"是**通过在所有内部资源的 poll 里埋 `poll_proceed` 钩子实现的**。你用 `tokio::sync::mpsc::Receiver::recv`、`tokio::io::AsyncReadExt::read`、`tokio::time::sleep`,这些 future 的 poll 开头都先扣预算。所以**用 tokio 资源的 task,自动协作**——它没法"不扣预算地狂跑",因为每做一次 I/O 或同步操作都要扣。这是 budget 能"几乎无法逃避"的根源:tokio 把扣预算埋在了所有"会做工作的地方"。
>
> 反过来说,**纯 CPU 计算(不碰 tokio 资源)的 task,budget 管不到**——比如一个 `async { let mut s = 0; for i in 0..1_000_000 { s += i } s }`,它的 poll 里没调任何 `poll_proceed`,budget 一直 128 没扣,它会一口气跑完这 100 万次循环。这就是为什么 tokio 还提供 `task::consume_budget().await`(下面讲)和 `task::unconstrained`(opt-out)——前者让你手动埋扣预算点,后者让你显式关掉 budget。

### 2.6 RestoreOnPending:没进展就还回去

`poll_proceed` 返回的 `RestoreOnPending` 是个**精妙的 guard**,值得专门拆。看它的定义:

```rust
// tokio/src/task/coop/mod.rs(摘录)
/// Value returned by the [`poll_proceed`] method.
#[derive(Debug)]
#[must_use]
pub struct RestoreOnPending(Cell<Budget>, PhantomData<*mut ()>);

impl RestoreOnPending {
    fn new(budget: Budget) -> Self {
        RestoreOnPending(
            Cell::new(budget),
            PhantomData,
        )
    }

    /// Signals that the task that obtained this `RestoreOnPending` was able to make
    /// progress. This prevents the task budget from being restored to the value
    /// it had prior to obtaining this instance when it is dropped.
    pub fn made_progress(&self) {
        self.0.set(Budget::unconstrained());
    }
}

impl Drop for RestoreOnPending {
    fn drop(&mut self) {
        // Don't reset if budget was unconstrained or if we made progress.
        // They are both represented as the remembered budget being unconstrained.
        let budget = self.0.get();
        if !budget.is_unconstrained() {
            let _ = context::budget(|cell| {
                cell.set(budget);
            });
        }
    }
}
```

([tokio/src/task/coop/mod.rs:257-289](../tokio/tokio/src/task/coop/mod.rs#L257-L289))

读这段,**抓两个动作**:

**① `poll_proceed` 成功时,创建 `RestoreOnPending::new(cell.get())`**——把**扣之前的 budget 快照**存进 guard 的 `Cell<Budget>`。注意是 `Cell`(内部可变性),因为 `made_progress` 要改它。

**② guard drop 时,看 `made_progress` 是否被调过**:
- 如果**没**调 `made_progress`(说明 task 没进展):guard 里的 budget 是"扣之前的快照",drop 时把它**恢复**到 thread_local(`cell.set(budget)`)——**把扣掉的预算还回去**。
- 如果**调过** `made_progress`:`made_progress` 把 guard 里的 budget 设成 `unconstrained`,`is_unconstrained()` 返回 true,drop 时**跳过恢复**——**预算消费被确认**。

用一段真实代码看这个模式(tokio 文档里的例子,讲怎么把 futures 的 channel 包成协作的):

```rust
// tokio/src/task/coop/mod.rs(摘录,文档示例)
impl<T> Stream for CoopUnboundedReceiver<T> {
    type Item = T;
    fn poll_next(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>
    ) -> Poll<Option<T>> {
        let coop = ready!(coop::poll_proceed(cx));    // ① 扣 1 预算,拿到 guard
        match self.receiver.poll_next_unpin(cx) {
            Poll::Ready(v) => {
                // We received a value, so consume budget.
                coop.made_progress();                  // ② 有进展!确认消费
                Poll::Ready(v)
            }
            Poll::Pending => Poll::Pending,            // ③ 没进展,coop drop 时还预算
       }
    }
}
```

([tokio/src/task/coop/mod.rs:324-340](../tokio/tokio/src/task/coop/mod.rs#L324-L340),文档示例)

逻辑:
- ① `ready!(poll_proceed(cx))` 扣 1 预算,拿到 `coop` guard。
- ② 如果 `receiver.poll_next_unpin` 返回 `Ready`(真收到一个值),调 `coop.made_progress()`——告诉 guard"我做了事,预算消费有效"。coop drop 时不再恢复。
- ③ 如果返回 `Pending`(没收到值,channel 空),**不调** `made_progress`——coop drop 时把扣掉的 1 预算**还回去**。

> **为什么不这么设计就有问题**——设想"扣了就扣了,不管有没有进展":
>
> 一个 task 在 `poll` 一个还没就绪的 fd(比如 socket 没数据)。它调 `poll_proceed` 扣 1,然后 poll 内层 io,内层返回 Pending(没数据),task 整体返回 Pending 让出。下次被唤醒,再调 `poll_proceed` 又扣 1,再 poll io,又 Pending……**反复唤醒反复扣,128 次预算被"白扣"——task 实际什么都没做(没读到一个字节),却被冤枉地"用光预算让出"**。这会让"等一个慢 I/O 的 task"被频繁无意义地让出,性能差。
>
> `RestoreOnPending` 解决这个:**没进展就还回去**。task 反复 poll 一个没就绪的 fd,每次 `poll_proceed` 扣 1,每次内层 Pending,每次 coop drop 还回 1——**净扣 0**。只有真读到数据(made_progress)才确认消费。这样 budget 只计"真做了事"的次数,不计"白 poll"的次数。这是 budget 公平性的关键——它惩罚"贪婪地做很多事"的 task,不惩罚"老实等一个慢 I/O"的 task。
>
> **钉死这件事**:`RestoreOnPending` 是 budget 设计的精髓——**"扣预算"和"确认消费"分离**。扣是悲观的(poll_proceed 立即扣,防止超支),确认是乐观的(只有 made_progress 才真消费,没进展就回滚)。这套"乐观扣减 + 回滚"模式,在数据库(事务)、并发控制(STM)里都见过,这里用在 task 调度上——同一个思想,不同的场景。

---

## 三、为什么是 128,以及 budget 和 LIFO 的正交

### 3.1 128 这个数的权衡

回到 `Budget::initial()` 的注释,把"为什么 128"再深挖一层:

```
   budget = 128 的三个约束:

   ┌─────────────────────────────────────────────────────────────┐
   │ 约束一:摊销调度开销(下限)                                  │
   │   task 每次被调度,有固定开销:进队 + 出队 + park/unpark      │
   │   + coop::budget 设置/恢复 + ... ≈ 几百纳秒到几微秒           │
   │   如果 budget 太小(比如 4),task 做几件事就让出,             │
   │   调度开销占比过高 → 吞吐崩。                                │
   ├─────────────────────────────────────────────────────────────┤
   │ 约束二:不饿死别人太久(上限)                                │
   │   budget 决定单 task 最长霸占时间 ≈ budget × 单次操作耗时      │
   │   太大(比如 65536),单 task 霸占几百毫秒,                  │
   │   别的 task + reactor 等太久 → 延迟崩。                      │
   ├─────────────────────────────────────────────────────────────┤
   │ 约束三:深 task 至少做点事(下限)                            │
   │   一个 nested await 多层的 task,每层都扣预算,               │
   │   budget 太小 → task 还没走到自己真正干活的代码就让出。        │
   ├─────────────────────────────────────────────────────────────┤
   │ 128 是三者交集里的一个值。                                   │
   │ 注释原话:"chosen somewhat arbitrarily" +                    │
   │          "as more yield points are added, this will          │
   │           probably also have to be raised"                   │
   └─────────────────────────────────────────────────────────────┘
```

注释坦白说"chosen somewhat arbitrarily"(某种程度上是任意选的)——这不是数学最优,是经验值。但"任意"不等于"乱选",它落在三个约束的交集里:

- **下限**(摊销 + 深 task):经验上,几十以下就太小(task 做不了几件事)。
- **上限**(不饿死太久):经验上,几千以上就太大(单 task 霸占过百毫秒)。
- **128 在中间偏小**——偏向"宁可让出勤一点,也别饿死别人"。这符合 tokio "低延迟优先"的定位。

注释最后那句"as more yield points are added in the ecosystem, this value will probably also have to be raised"——这是个**前瞻性警告**:tokio 生态每加一个 `poll_proceed` 调用点(比如新加一个 io 工具),task 每次 poll 实际做的事就少一件(因为多扣了一次预算),所以 budget 要随之提高,否则 task 太容易被冤枉让出。这是个**动态平衡**——budget 不是写死的真理,是跟随生态演进的参数。

> **钉死这件事**:128 是个**经验折衷值**,不是理论最优。它体现了 tokio "低延迟优先 + 摊销调度开销"的工程取向。这个值在 `Budget::initial()` 是个 `const fn`,理论上可以改(但 tokio 没暴露成配置项,因为改它要很懂这套权衡)。理解"为什么 128"比记住"是 128"重要——理解了,你才知道什么时候该用 `consume_budget` 手动加扣点,什么时候该用 `unconstrained` opt-out。

### 3.2 budget 和 LIFO slot 的正交(衔接第 8 章)

第 8 章讲过 LIFO slot 有"连续 3 次"上限防 ping-pong,这里要明确 budget 和它的关系——**两者正交,各管一种滥用**。

回顾第 8 章的源码(`run_task` 的 LIFO 循环):

```rust
// tokio/src/runtime/scheduler/multi_thread/worker.rs(摘录)
coop::budget(|| {
    task.run();                  // 主 task poll(在 budget 内)
    let mut lifo_polls = 0;
    loop {
        // ... budget 不够时:LIFO task 降级到队尾,LIFO 保持 enabled ...
        // if not enough budget: push to run_queue, return (debug_assert lifo_enabled)
        // ... poll LIFO task ...
        lifo_polls += 1;
        if lifo_polls >= MAX_LIFO_POLLS_PER_TICK {   // = 3
            core.lifo_enabled = false;                // 关 LIFO(策略级)
        }
    }
});
```

([tokio/src/runtime/scheduler/multi_thread/worker.rs:675-765](../tokio/tokio/src/runtime/scheduler/multi_thread/worker.rs#L675-L765),结构摘录)

**关键观察:整个 LIFO 循环都在 `coop::budget` 包裹内**——也就是说,**LIFO poll 出来的子 task,和主 task 共享同一份 128 预算**。这就是第 8 章那句注释的物理体现:"the tasks came from the LIFO slot and are considered part of the current task for scheduling purposes. These tasks inherent the 'parent''s limits."(LIFO slot 来的 task 被视为当前 task 的一部分,继承 parent 的限制)。

两个机制的分工:

| 机制 | 管什么 | 触发条件 | 触发后 |
|------|--------|----------|--------|
| **budget**(任务级) | 单个 task 霸占线程太久 | poll_proceed 扣到 0 | 当前 task 让出(返回 Pending);LIFO task 降级队尾 |
| **LIFO 上限**(策略级) | A/B 互唤醒挤掉别的 task | LIFO 连续 poll ≥ 3 次 | `lifo_enabled = false`,后续 schedule 走 FIFO |

**正交性**:

- budget 耗尽**不关 LIFO**(`debug_assert!(core.lifo_enabled)` 在降级分支里)——budget 是"这个 task 该让出了",不是"LIFO 策略有问题"。
- LIFO 上限触发**不消耗 budget**——关 LIFO 是策略调整,不影响当前 budget 计数。

两层防御各管一种"贪婪":

- budget 管"**纵向贪婪**"——一个 task 沿着调用栈往下,做了太多事(128 次 channel recv、128 次 io read)。
- LIFO 上限管"**横向贪婪**"——A 和 B 互相唤醒,横向霸占 LIFO slot,挤掉队列里别的 task。

一个纵向,一个横向,互不重叠。tokio 用两层独立机制,把协作式调度的命门(各种"贪婪")都堵上。

> **钉死这件事**:budget 和 LIFO 上限是**正交的两层防饿死机制**。理解这个正交性,你就理解了 tokio 防御性设计的思路——**每种滥用配一种机制,机制之间不互相干扰**。这跟第 5 章 task 状态字(一个 AtomicUsize 装 6 个状态位,每个位管一件事)、第 7 章 queue(双 head 各管一摊)是同一个哲学:**关注点分离,每个机制职责单一**。

---

## 四、opt-out:unconstrained 和 consume_budget

最后看 tokio 怎么把"是否协作"的选择权留给用户。

### 4.1 unconstrained:彻底关掉 budget

如果你有一个**必须一口气跑完**的 CPU 密集 task(比如一个大计算、一个加密解密),用 `task::unconstrained`:

```rust
// tokio/src/task/coop/unconstrained.rs(摘录)
pin_project! {
    /// Future for the [`unconstrained`](unconstrained) method.
    #[cfg_attr(docsrs, doc(cfg(feature = "rt")))]
    #[must_use = "Unconstrained does nothing unless polled"]
    pub struct Unconstrained<F> {
        #[pin]
        inner: F,
    }
}

impl<F: Future> Future for Unconstrained<F>
where
    F: Future,
{
    type Output = <F as Future>::Output;

    cfg_coop! {
        fn poll(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Self::Output> {
            let inner = self.project().inner;
            crate::task::coop::with_unconstrained(|| inner.poll(cx))
        }
    }
    // ...
}

/// Turn off cooperative scheduling for a future. The future will never be forced to yield by
/// Tokio. Using this exposes your service to starvation if the unconstrained future never yields
/// otherwise.
pub fn unconstrained<F>(inner: F) -> Unconstrained<F> {
    Unconstrained { inner }
}
```

([tokio/src/task/coop/unconstrained.rs:6-45](../tokio/tokio/src/task/coop/unconstrained.rs#L6-L45))

机制:`Unconstrained<F>` 的 poll 用 `with_unconstrained` 包裹内层 poll。`with_unconstrained` 把 budget 设成 `Budget::unconstrained()`(=`Budget(None)`),内层 poll 期间 budget 是 None,`poll_proceed` 永远返回 Ready(因为 None 永远 success)。**所以 unconstrained 里的 task 永远不会被 budget 强制让出**。

注释警告明明白白:"Using this exposes your service to starvation if the unconstrained future never yields otherwise."——用它,你自己承担饿死别人的风险。

> **钉死这件事**:`unconstrained` 是 tokio 的**逃生舱**。它承认"budget 不是万能的——有些 task 真的需要一口气跑完"。但它把选择权和风险一起交给用户:**你 opt-out,你就得保证这个 task 自己会结束(或者至少不饿死关键 task)**。这是 Rust "把权力和责任一起交给程序员"哲学的体现——不绑架你,但警告你。工程上,`unconstrained` 适合:① 已知会快速结束的 CPU 密集片段(比如一个 ~1ms 的计算);② 嵌套在已经有 await 点的 task 里的子计算(外层 await 已经保证了协作性,内层可以放开)。**不适合**:长跑的循环、可能无限循环的逻辑。

### 4.2 consume_budget:手动埋扣点

如果你有个 CPU 密集循环**但又想保持协作**(不想 unconstrained),可以用 `task::consume_budget().await` 手动埋扣点:

```rust
// tokio/src/task/coop/consume_budget.rs(摘录)
/// Consumes a unit of budget and returns the execution back to the Tokio
/// runtime *if* the task's coop budget was exhausted.
pub async fn consume_budget() {
    let mut status = std::task::Poll::Pending;

    std::future::poll_fn(move |cx| {
        std::task::ready!(crate::trace::trace_leaf());
        if status.is_ready() {
            return status;
        }
        status = crate::task::coop::poll_proceed(cx).map(|restore| {
            restore.made_progress();    // 注意:consume_budget 默认算"做了事"
        });
        status
    })
    .await
}
```

([tokio/src/task/coop/consume_budget.rs:24-39](../tokio/tokio/src/task/coop/consume_budget.rs#L24-L39))

文档示例:

```rust
// tokio/src/task/coop/consume_budget.rs(摘录,文档示例)
async fn sum_iterator(input: &mut impl std::iter::Iterator<Item=i64>) -> i64 {
    let mut sum: i64 = 0;
    while let Some(i) = input.next() {
        sum += i;
        tokio::task::consume_budget().await   // 每次循环扣 1,budget 光就让出
    }
    sum
}
```

([tokio/src/task/coop/consume_budget.rs:15-23](../tokio/tokio/src/task/coop/consume_budget.rs#L15-L23))

`consume_budget` 内部就是个 `poll_fn`,调 `poll_proceed` 扣 1,没钱就 Pending(让出)。注意它**直接 `restore.made_progress()`**——consume_budget 本身就是"我承认我在做事"(迭代下一项就是事),所以默认算进展。

这是"手动协作"的入口——纯 CPU 循环(没碰 tokio 资源,budget 管不到)可以手动埋 `consume_budget().await`,把自己纳入 budget 机制。

> **钉死这件事**:`unconstrained`(全关)和 `consume_budget`(手动埋点)是 budget 机制的两个"用户侧出口"。前者适合"我知道这段不该被打断",后者适合"我知道这段是 CPU 密集但我想保持协作"。tokio 的设计哲学是:**默认协作(budget=128 自动应用),需要时 opt-out(unconstrained)或手动调(consume_budget)**。默认安全,逃生舱可选——这是好的默认值设计。

---

## 技巧精解:thread_local 隐式上下文 + 乐观扣减回滚

这一节把本章两个最硬核的技巧拆透。

### 技巧一:thread_local 做"task 级"语义的隐式上下文

#### 这套设计在解决什么

budget 逻辑上是 **task 级**的(每个 task 一份 128 预算),但物理上存 **thread 级**(thread_local)。task 内部的资源(channel、io)要扣 budget,但它们**不知道自己属于哪个 task**(一个 channel 可能被多个 task poll)。怎么让"task 内部的资源访问到当前 task 的 budget"?

#### 反面对比 A:存 task 上,资源拿 task 引用

```rust
// 简化示意,非源码原文:反面,budget 存 task,资源拿 task 引用
struct Task {
    budget: u8,
    // ...
}
impl Channel {
    fn poll_recv(&self, cx: &mut Context, task: &Task) -> Poll<T> {
        if task.budget == 0 { return Pending; }
        task.budget -= 1;        // ← 要可变访问 task
        // ...
    }
}
```

> **不这样会怎样**:① `poll` 的签名是 `poll(self: Pin<&mut Self>, cx: &mut Context)`——**没有 task 参数**。要传 task,得改 poll 契约(破坏整个 Future 生态);② 退一步,从 Context 里取 task 句柄——但 Context 只装 Waker,要扩 Context 装 budget,又是改标准库;③ 即便能传,budget 要可变访问(`&mut Task`),可 task 此刻正被 worker `&mut` poll 着,借用冲突。
>
> 这条路死在"Future poll 契约不允许额外参数 + 借用冲突"。

#### 反面对比 B:全局注册表(task_id → budget)

> **不这样会怎样**:搞个 `HashMap<TaskId, Budget>`,资源 poll 时查当前 task 的 budget。问题:① 当前 task 是谁?要从某处取 task_id(又回到 thread_local 或 context);② HashMap 查询开销(哈希 + 查表,几十纳秒),每次 poll_proceed 都查,百万 task 累加可见;③ 并发同步——多个 worker 访问 HashMap 要锁(虽然每个 worker 只动自己的 task,但 HashMap 共享)。
>
> 这条路开销大、复杂度高。

#### 正解:thread_local Cell

tokio 的做法:

```rust
// 简化示意,基于源码逻辑
thread_local! {
    static BUDGET: Cell<Budget> = Cell::new(Budget::unstrained());  // 默认 None
}

fn budget<R>(f: impl FnOnce() -> R) -> R {
    let prev = BUDGET.get();
    BUDGET.set(Budget::initial());   // 设成 128
    let r = f();
    BUDGET.set(prev);                 // 恢复
    r
}

fn poll_proceed(cx: &mut Context) -> Poll<...> {
    let mut b = BUDGET.get();
    if b.decrement() {
        BUDGET.set(b);
        Ready(...)
    } else {
        Pending
    }
}
```

**thread_local + Cell 的优势**:

- **零参数传递**:资源 poll 时直接 `BUDGET.get()`,不用知道 task 是谁。task 内部的资源天然访问"当前线程的 budget",而当前线程此刻在 poll 这个 task,所以就是"这个 task 的 budget"。
- **O(1) 访问**:thread_local 在 x86 上用 `FS`/`GS` 段寄存器寻址,`Cell::get/set` 是普通内存读写(几纳秒),无锁、无哈希。
- **借用无冲突**:`Cell` 是内部可变性,`get/set` 只要 `&self`,不撞 worker 对 task 的 `&mut`。
- **自动 task 隔离**:worker poll A 前 `budget(|| ...)` 设 128,poll B 前**再次** `budget(|| ...)` 设 128——每次重置,task 间天然隔离。不需要"记住每个 task 的 budget",因为 budget 不持久化在 task 上,而是"每次 poll 时重新发一份"。

> **钉死这件事**:thread_local + Cell 是 Rust 系统**编程"用隐式上下文换 API 简洁"的典范**。它让 budget 这个"逻辑上属于 task"的状态,物理上存在线程上,通过"每次 poll 前重置"模拟出"每 task 一份"。这种"用 thread_local 做隐式传递"在 Rust 生态里到处都是——`tokio::runtime::Handle` 的 current handle、`tracing` 的 span context、标准库的 `task::Context`——本质都是同一招:**避免污染函数签名,用 thread_local 做上下文**。本书第 5 章讲 task 的 schedule handle(task 自带 schedule 字段)是另一种思路——直接存 task 上。budget 选 thread_local 因为它更轻(不用每个 task +1 字段)且访问更快。

### 技巧二:乐观扣减 + RestoreOnPending 回滚

#### 这套设计在解决什么

`poll_proceed` 扣 1 预算,但 task 可能**没进展**(内层 poll 返回 Pending)。怎么避免"白扣预算"——一个反复 poll 慢 I/O 的 task 被冤枉地用光预算?

#### 反面对比:扣了就扣了

```rust
// 简化示意,非源码原文:反面,扣了就扣了
fn poll_proceed(cx: &mut Context) -> Poll<()> {
    let mut b = BUDGET.get();
    if b == 0 { return Pending; }
    b -= 1;                          // 扣了就扣了
    BUDGET.set(b);
    Ready(())
}
// 调用方
let _ = ready!(poll_proceed(cx));    // 扣 1
match inner.poll(cx) {
    Ready(v) => Ready(v),
    Pending => Pending,              // 没进展,但预算已经扣了!
}
```

> **不这样会怎样**:task poll 一个慢 socket(数据还没来)。每次被唤醒(比如被定时器叫醒):`poll_proceed` 扣 1 → poll socket → Pending → 让出。下次被唤醒又扣 1……**128 次唤醒后,budget 光了,task 被强制让出,可它实际一个字节都没读到**。这种 task 会被频繁冤枉地让出,延迟漂移、吞吐下降。问题根源:**扣预算时不知道这次 poll 会不会有进展**(悲观扣减)。

#### 正解:乐观扣减 + guard 回滚

tokio 的做法:

```rust
// 简化示意,基于源码逻辑
fn poll_proceed(cx: &mut Context) -> Poll<RestoreOnPending> {
    let mut b = BUDGET.get();
    if !b.decrement() { return Pending; }       // 没钱 → Pending
    let prev = b + 1;                            // 扣前的快照(注意:这里 prev 是 decrement 后的 b 加回 1)
    BUDGET.set(b);                               // 悲观扣减
    Ready(RestoreOnPending::new(prev_snapshot))  // 返回 guard,里面存"扣前快照"
}

// 调用方
let coop = ready!(poll_proceed(cx));             // 扣 1,拿 guard
match inner.poll(cx) {
    Ready(v) => {
        coop.made_progress();                    // 有进展!guard 标记"不回滚"
        Ready(v)
    }
    Pending => {
        // 不调 made_progress
        Pending
        // coop 在这里 drop → 把预算恢复成"扣前快照"(还回去)
    }
}
```

**关键**:`RestoreOnPending` 是个 RAII guard,drop 时**根据"是否 made_progress"决定恢复与否**。

- `made_progress` 调过:guard 内部 budget 被设成 unconstrained,drop 时 `is_unconstrained() == true`,**跳过恢复**——预算消费确认。
- `made_progress` 没调:guard 内部 budget 是"扣前快照",drop 时把它写回 thread_local——**预算还回去**。

这就是"乐观扣减 + 按需回滚":扣的时候悲观(立即扣,防超支),但如果事后发现"白扣了"(没进展),drop 时回滚。**净效果:只有真做了事的 poll 才消耗预算,白 poll 不消耗**。

> **钉死这件事**:`RestoreOnPending` 是"**用 RAII guard 做事务回滚**"在调度里的应用。这套模式在数据库(事务 rollback)、并发控制(STM、lock-free 的 hazard pointer)里都见过——**悲观获取 + 乐观确认 + 失败回滚**。tokio 把它用在了"task 预算"上:悲观扣减(防超支)、乐观确认(只有真进展才消费)、失败回滚(没进展就还回去)。这让 budget 既严格(真做了 128 件事就让出)又公平(白 poll 不算)。这是 Rust 系统级代码"用类型系统(RAII guard + Drop trait)表达事务语义"的范例——**Drop 不只是释放资源,还能做"补偿动作"**。

#### sound 性小结

budget 机制的代码**几乎全是 safe Rust**(除了 `Cell` 的内部可变性,但那是标准库的 safe API)。没有 `unsafe`,因为:

- thread_local 访问由标准库保证线程隔离。
- `Cell<Budget>` 是 `Copy` 类型(Budget 是 `Copy`),`get/set` 无并发问题(单线程)。
- `RestoreOnPending` 的 `PhantomData<*mut ()>` 是为了**禁止 Send/Sync**(guard 不该跨线程,因为它管的是当前线程的 budget)——这是个**用 PhantomData 标记拒绝 Send 的技巧**,防止用户错误地把 guard 送到别的线程。

这是 tokio "把 unsafe 关进底层 crate"风格的延续——budget 这种上层调度逻辑,全是 safe Rust,正确性靠类型系统保证。

---

## 章末小结

### 用"餐厅服务员"比喻回顾本章

1. **协作式调度的命门:服务员不放手就饿死全店** —— tokio 是协作式,服务员(worker)处理一张订单(task)时,**他不主动放手(让出),谁也抢不走他**。一个服务员卡在某张"无尽订单"上(比如一个 `while true` 不 await 的 task),这家店所有别的订单都停摆——没人接、厨房叫号屏没人看、催单闹钟没人理。这是协作式的代价。
2. **budget 是"服务员手里的工单配额卡"** —— 经理(scheduler)给每个服务员发一张**128 次**的配额卡,服务员每从厨房取一次菜(每用一次 tokio 资源)刷一次卡,**刷光就请把当前订单放回去(让出),去服务下一张**。这是"软抢断"——经理不能硬抢服务员手里的订单(协作式),但能用配额卡逼他"别太贪"。
3. **配额卡存在服务员身上(thread_local),不存订单上** —— 每次服务员接新订单,经理**重置**配额卡(发新的 128 次),所以"每张订单独立享受 128 次"。订单内部(订单调用的子资源,channel、io)**直接刷服务员身上的卡**,不用知道"我属于哪张订单"——因为此刻服务员就在处理这张订单。
4. **没取到菜不算刷卡(`RestoreOnPending`)** —— 服务员刷卡去厨房取菜,结果菜还没好(内层 Pending),**这次刷卡不算**——把次数还回卡里。只有真取到菜(made_progress)才确认消费。这避免"服务员反复去厨房取没好的菜,被冤枉刷光配额"。
5. **budget(任务级)和 LIFO 上限(策略级)正交** —— budget 管"纵向贪婪"(一个 task 沿调用栈做太多事),LIFO 上限管"横向贪婪"(A/B 互唤醒挤掉别人)。两层独立防御,各管一种滥用。budget 耗尽不关 LIFO,LIFO 关闭不消耗 budget。
6. **逃生舱 `unconstrained`** —— 如果有个订单必须一口气做完(比如一个加密计算),服务员可以**不刷卡**(`task::unconstrained`)——但风险自负,可能饿死别的订单。tokio 把选择权留给用户:默认协作,需要时 opt-out。

### 本章在全书主线中的位置 + 第 2 篇收束

记住全书的二分法:**调度执行(让就绪的任务跑) vs 事件唤醒(让等待的任务不空耗、就绪了再叫)**。

本章服务的是**调度执行**那一面——更具体说,是**"调度执行的兜底"**这一面。前 8 章的调度器机制都假设"task 自觉让出",本章补上了"task 不自觉怎么办"的答案。**没有 budget,协作式调度的命门就裸露着——一个不自觉的 task 能让整个运行时停摆。budget 是协作式能扛海量并发的最后一块拼图。**

**第 2 篇(Runtime 心脏:调度器)到此收束。** 把第 2 篇四章串起来:

| 章 | 立起的东西 | 一句话 |
|----|----------|------|
| 第 6 章 | Runtime 三件套全貌 | scheduler + reactor + timer 拼在一个 `Runtime` 里,worker 在一个循环里交替 poll task / 推进 driver / park,park 用三态原子防丢唤醒 |
| 第 7 章 | work-stealing 调度器内部 | 本地队列(双 head 无锁环形)+ injector(全局兜底)+ 偷一半;不是 Chase-Lev,是定制的"双 head 串行化 steal" |
| 第 8 章 | 两模式对比 + LIFO slot | current-thread 简到极致(VecDeque + 独占 driver),multi-thread 装备齐全(无锁队列 + 共享 driver + LIFO slot);LIFO slot 用"刚唤醒优先 + 3 次上限"吃到缓存红利 |
| **第 9 章(本章)** | **协作式让出与 budget** | **协作式命门 = task 不让出就停摆;budget 用 thread_local + poll_proceed + RestoreOnPending 实现"128 次软抢断";与 LIFO 正交;unconstrained 是逃生舱** |

四章合起来,你完成了从"task 是什么"(第 1 篇)到"task 怎么被运行时驱动起来、怎么被调度、怎么被防止霸占"的完整认知链。第 2 篇结束,你脑子里应该能放映出 tokio 多线程运行时的完整运转:

> 一个 task 被 `spawn`,进 injector(如果是外部线程)或本地队列(如果是 worker 自己);worker 主循环 tick → 每 61 次推进 driver(reactor + timer)→ `next_task` 取一个 task(先看 LIFO slot,再看本地队列,再偷别人,再看 injector)→ `coop::budget` 包着 `task.run()` poll 它,poll 期间 task 用 tokio 资源时扣 budget,扣光就让出;task 在 await 点挂起,把 Waker 留给 reactor/timer;worker 继续取下一个 task;reactor/timer 事件就绪时,经 Waker 把 task 重新塞回队列(LIFO slot 或本地或 injector);worker 没 task 了就 park(condvar 或 epoll);被叫醒接着干。

这是运行时心脏的完整跳动。**第 3 篇开始,我们从"调度执行"面转向"事件唤醒"面**——reactor 怎么盯住海量 socket、I/O 事件怎么把等待的 task 叫醒。budget 兑现了协作式的兜底,接下来该看"唤醒"那一面的兜底了。

### 五个"为什么"清单

1. **协作式调度的命门是什么?**:task 不在 await 点主动让出,谁也抢不走它(用户态没时钟中断)。一个不让出的 task(死循环、永远就绪的 stream)会独占整个 worker 线程,饿死同线程所有 task + 让 reactor/timer 推进不了(因为 maintenance 每 61 次 poll 才推进 driver,全被贪婪 task 占了)。这是"运行时停摆",不是"性能差"。
2. **budget 怎么实现?**:thread_local 的 `Cell<Budget>`,Budget 是 `Option<u8>`。worker 每次 `run_task` 前 `coop::budget(|| task.run())` 设成 128;task 内部资源(channel、io、time、copy)的 poll 开头调 `poll_proceed` 扣 1,扣到 0 返回 Pending(让出)。budget 存线程上不存 task 上,靠"每次 poll 前重置"模拟"每 task 一份"。
3. **为什么是 128?**:经验折衷值(注释原话 "chosen somewhat arbitrarily")。三个约束的交集:① 够高摊销调度开销(下限);② 够低不饿死别人太久(上限);③ 够高让深 task 至少做点事(下限)。128 偏向"低延迟优先"(宁可让出勤一点)。注释警告:随着生态加更多 yield 点,这个值可能要提。
4. **`RestoreOnPending` 干什么?为什么需要它?**:`poll_proceed` 扣 1 后返回的 RAII guard。如果 task **没进展**(内层 poll Pending),guard drop 时把预算**还回去**(回滚);如果调了 `made_progress`(真做了事),guard drop 时**跳过恢复**(确认消费)。这套"乐观扣减 + 按需回滚"避免"白扣预算"——一个反复 poll 慢 I/O 的 task 不会被冤枉刷光预算。本质是 RAII guard 做事务回滚,跟数据库的 rollback 同源。
5. **budget 和 LIFO slot 上限什么关系?**:**正交**。budget 是**任务级**让出(单 task 霸占线程太久,扣到 0 让出),LIFO 上限是**策略级**让出(A/B 互唤醒挤掉别人,连续 3 次关 LIFO)。budget 耗尽时 LIFO task 降级到队尾**但 LIFO 保持启用**(`debug_assert!(lifo_enabled)`);LIFO 关闭不消耗 budget。两者各管一种贪婪(纵向 vs 横向),不互相干扰。

### 想继续深入,该往哪钻

- **本章引用的核心源码(按重要度排)**:
  - [`tokio/src/task/coop/mod.rs`](../tokio/tokio/src/task/coop/mod.rs) —— **本章灵魂**。module 文档(L1-59,讲协作式动机 + `drop_all` 反例 + unconstrained)、`Budget(Option<u8>)`(L97)、`Budget::initial()=128` + 注释(L104-117)、`with_budget`/`ResetGuard`(L143-168)、`has_budget_remaining`(L223-227)、`poll_proceed`(L343-364)、`RestoreOnPending` + `made_progress` + Drop 回滚(L257-289)、`Budget::decrement`(L410-427)、`Coop`/`cooperative`(L434-492)、test 用例(L506-572,直观展示 budget 计数)。
  - [`tokio/src/task/coop/unconstrained.rs`](../tokio/tokio/src/task/coop/unconstrained.rs) —— `Unconstrained<F>` + `with_unconstrained`(L6-45,opt-out 实现)。
  - [`tokio/src/task/coop/consume_budget.rs`](../tokio/tokio/src/task/coop/consume_budget.rs) —— `consume_budget` 手动埋点(L24-39,文档示例 sum_iterator)。
  - [`tokio/src/runtime/scheduler/multi_thread/worker.rs`](../tokio/tokio/src/runtime/scheduler/multi_thread/worker.rs) —— `run_task` 的 `coop::budget` 包裹(L675)+ LIFO 循环(L689-765,继承 budget + budget 耗尽降级 L716-728)。
  - **tokio 内部资源的 poll_proceed 调用点**(印证"用 tokio 资源就自动协作"):[`time/sleep.rs:398`](../tokio/tokio/src/time/sleep.rs#L398)、[`process/mod.rs:1140`](../tokio/tokio/src/process/mod.rs#L1140)、[`io/util/copy.rs:97`](../tokio/tokio/src/io/util/copy.rs#L97)、[`io/util/mem.rs:335`](../tokio/tokio/src/io/util/mem.rs#L335)。
- **亲手感受 budget**:
  - 写一个 `async { let mut s = 0; for i in 0..1_000_000_000 { s += i; } s }`(纯 CPU,不碰 tokio 资源),spawn 进 multi-thread runtime。**你会发现它不被 budget 限制**(一口气跑完,期间别的 task 饿死)——证明 budget 只埋在 tokio 资源里。
  - 把它改成 `async { let mut s = 0; for i in 0..1_000_000_000 { s += i; tokio::task::consume_budget().await; } s }`,**别的 task 就能跑了**——证明手动埋点有效。
  - 写一个疯狂 recv 一个 unbounded channel 的 task,观察它**被自动 budget 限制**(每 128 次让出)——证明 tokio 资源自动协作。
- **用 `tokio-console` 观察 budget 行为**:`tokio-console` 能看到 task 的 poll 次数、是否被 forced yield。跑一个贪婪 task,看它怎么被 budget 强制让出(budget_forced_yield_count 这个 metric 会涨)。
- **对比其他运行时的防霸占机制**:Go 的 goroutine 有**抢占式调度**(从 1.14 起基于信号,真硬抢)、async-std 没有 budget(容易饿死)、Java 的 Project Loom 用 JVM 内部抢占。tokio 的 budget 是"协作式 + 软抢断"的折衷——比纯协作强(有兜底),比真抢占弱(纯 CPU 仍能逃逸)。读这些对比,能理解 budget 在设计空间里的位置。
- **下一站**:**第 2 篇(调度执行)收束,进入第 3 篇(事件唤醒)**。前 9 章我们讲透了"task 怎么被调度、被 poll、被防止霸占"。但 task 在 await 点挂起后,**谁负责在它等的 I/O 就绪时把它叫醒?** 这是 reactor 的戏——盯住海量 socket,数据来了精确唤醒。翻开 **第 10 章 · mio 与 epoll:事件驱动的底座**——我们从"调度"面转向"唤醒"面,看 reactor 怎么用 epoll 盯住几万个 socket,事件来了怎么找到对应的 task。

---

> budget 兑现了协作式调度的兜底伏笔——task 不会因为"贪婪"而停摆运行时。第 2 篇(调度执行)收束。可 task 在 await 点挂起后,等的是 I/O 事件或时间事件——**谁来盯着这些事件、就绪了怎么叫醒 task?** 这是"事件唤醒"面的戏。翻开 **第 10 章 · mio 与 epoll:事件驱动的底座**——第 3 篇开始,我们从调度转向唤醒。
