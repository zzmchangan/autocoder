# 第 8 章 · current-thread vs multi-thread:两种运行时模式的取舍

> **核心问题**:tokio 给你两种运行时模式——`new_multi_thread()`(默认,多 worker 线程 + work-stealing)和 `new_current_thread()`(单线程,没 work-stealing)。第 7 章我们把 multi-thread 的调度器内部拆透了——双 head 无锁队列、偷一半、injector 兜底,花活儿满满。可既然 multi-thread 这么强,**为什么还要保留 current-thread 这个"看起来更弱"的模式**?它省了什么、丢了什么?两种模式在 LIFO slot、driver 持有方式、blocking pool 配置上到底差在哪?
>
> 这一章是第 2 篇的对照章——把第 6、7 章建立的 multi-thread 心智,跟 current-thread 并排放,看清**两套实现是同一个骨架的不同裁剪**。同时本章技巧精解的主角是 multi-thread 独有的 **LIFO slot**(刚被唤醒的任务优先跑,提高缓存命中、降低延迟),它是一个看似简单、实则精妙的"用排队顺序换性能 + 用次数上限防饿死"的设计。
>
> **读完本章你会明白**:
> - 为什么有 current-thread 模式——它**省掉了 work-stealing 的所有同步开销**(无锁队列、steal CAS、idle 状态原子),单线程下 task 调度是纯内存操作(`VecDeque::push_back` / `pop_front`),零原子、零跨核。对于"单核嵌入式 / 单线程小程序 / 测试"场景,这是巨大的简洁性红利。
> - current-thread 的调度器内部长什么样:**没有本地队列、没有 lifo_slot、没有 work-stealing**,只有一个 `VecDeque<Notified>` 作为 pending 队列,task 进队出队就是普通的 `push_back` / `pop_front`(FIFO)。
> - 两模式在 **driver 持有**上的根本差别:multi-thread 是 `TryLock<Driver>`(共享、可竞争,park 时抢锁);current-thread 是 `Option<Driver>`(独占,park 时 `take` 出来用完放回,纯粹的所有权转移,无并发)。这直接决定了"谁能睡 epoll"。
> - **LIFO slot** 是 multi-thread 独有的优化:刚被唤醒的 task(通常 cache 还热)塞进单槽,**下次第一个被 poll**,而不是排到队尾。这提升了消息传递(message passing)模式的局部性、降低延迟。但它有个命门——**ping-pong 饿死**(A 唤醒 B、B 唤醒 A,无限循环挤掉别的 task),tokio 用"连续 LIFO poll ≤ 3 次"的上限防饿死。
> - 为什么 LIFO slot 在 current-thread 模式下不存在(单线程没有"另一个 worker 偷走"的风险,park 期间也不会有新 task 进 LIFO,意义不大),以及它和 budget(第 9 章)的关系:budget 耗尽时 LIFO 任务**降级**到队尾,但 **LIFO slot 本身保持启用**;真正触发 LIFO 关闭的是 ping-pong 次数上限,不是 budget。
>
> **如果一读觉得太难**:先只记住三件事——① current-thread 是 multi-thread 的"简化版",省掉了 work-stealing 和 LIFO slot,task 队列是个普通 `VecDeque`,FIFO,零原子;② multi-thread 独有 **LIFO slot**——刚唤醒的 task 优先跑(缓存热、延迟低),但有"连续 3 次"上限防 ping-pong 饿死;③ 两模式最大实现差别是 **driver 怎么持有**:multi-thread 共享 + TryLock(抢锁睡 epoll),current-thread 独占 + Option(take 出来用),这决定了"多 worker 抢一个 epoll fd" vs "单线程独占 epoll fd"。

---

## 章首·一句话点破

> **两种模式是同一套骨架的两份裁剪:current-thread 是"光秃秃的骨架"——一个 `VecDeque` 队列、一个独占的 driver、FIFO 调度,零原子零跨核,简单到极致;multi-thread 是"骨架上挂满装备"——加了无锁本地队列、work-stealing、LIFO slot、共享 driver 加 TryLock。多挂的每一件装备都是"为多核并发付的代价换来的收益":本地队列让 worker 自给自足、work-stealing 让失衡自愈、LIFO slot 让刚唤醒的 task 趁热打铁、TryLock 让多 worker 共享一个 epoll fd 而不互相阻塞。current-thread 省掉这一切,换来的是单线程下的极致简洁和零同步开销——这就是它存在的全部理由。**

这是**结论**。这一章倒过来拆:先把 current-thread 的调度器内部掰开,看清它"简到什么程度"(就一个 `VecDeque`);再把两模式并排对比,逐项看清差异(队列结构、driver 持有、blocking pool、LIFO slot);最后落到 LIFO slot 这个 multi-thread 独有的优化上,拆透它"为什么用、怎么防饿死"。

第 7 章结尾留了钩子:"tokio 还有个 current-thread 模式,它没 work-stealing、没本地队列,为什么还要存在?" 这一章回答。

---

## 一、current-thread:简到极致的调度器

先看 current-thread 调度器的内部。读完你会发现,**它比 multi-thread 简单一个数量级**。

### 1.1 一个 `VecDeque` 就是全部

current-thread 的 `Core` 结构体:

```rust
// tokio/src/runtime/scheduler/current_thread/mod.rs(摘录)
struct Core {
    /// Scheduler run queue
    tasks: VecDeque<Notified>,

    /// Current tick
    tick: u32,

    /// Runtime driver
    ///
    /// The driver is removed before starting to park the thread
    driver: Option<Driver>,
    // ... 其他字段省略(metrics、unhandled_panic 等)
}
```

([tokio/src/runtime/scheduler/current_thread/mod.rs:61-82](../tokio/tokio/src/runtime/scheduler/current_thread/mod.rs#L61-L82))

**对比 multi-thread 的 `Core`**(第 7 章读过)——multi-thread 的 Core 有 `run_queue: queue::Local<Arc<Handle>>`(那个 256 容量的无锁环形数组)、`lifo_slot: Option<Notified>`、`is_searching: bool`、`rand`(偷 victim 用)、`park: Option<Parker>` 等一堆字段。current-thread 的 Core **只有一个 `tasks: VecDeque<Notified>`**——一个标准库的双端队列,装 task。

这就是"简到极致"。没有无锁队列、没有 LIFO slot、没有 work-stealing 相关的任何字段。task 的进队出队就是:

```rust
// tokio/src/runtime/scheduler/current_thread/mod.rs(摘录)
fn push_task(&mut self, handle: &Handle, task: Notified) {
    self.tasks.push_back(task);
    // ... metrics 更新
}

fn next_local_task(&mut self, handle: &Handle) -> Option<Notified> {
    let ret = self.tasks.pop_front();
    handle.shared.worker_metrics.set_queue_depth(self.tasks.len());
    ret
}
```

([tokio/src/runtime/scheduler/current_thread/mod.rs:324-351](../tokio/tokio/src/runtime/scheduler/current_thread/mod.rs#L324-L351))

`push_back`(塞队尾)+ `pop_front`(取队首)= **标准 FIFO**。零原子操作、零 CAS、零跨核同步。这两个方法连 `unsafe` 都没有——就是普通的 `VecDeque` 调用,跟你在应用代码里写的 `vec.push_back()` 没区别。

> **钉死这件事**:current-thread 的 task 队列是个 `VecDeque`,**普通的标准库数据结构,无锁、无原子、无 unsafe**。这是它"简到极致"的物理体现——multi-thread 那套 600 行的 `queue.rs`(双 head 打包、steal_into2 三段式 CAS、push_overflow 半数搬迁)在 current-thread 里**一行都没有**。原因很简单:**只有一个线程**,根本不需要无锁——所有操作天然串行,加锁/原子都是徒劳的开销。

### 1.2 schedule 路径:本线程直推,外部线程进 injector

看 current-thread 的 `schedule`(task 被唤醒后塞回队列的入口):

```rust
// tokio/src/runtime/scheduler/current_thread/mod.rs(摘录)
fn schedule(&self, task: task::Notified<Self>) {
    use scheduler::Context::CurrentThread;

    context::with_scheduler(|maybe_cx| match maybe_cx {
        Some(CurrentThread(cx)) if Arc::ptr_eq(self, &cx.handle) => {
            let mut core = cx.core.borrow_mut();
            if let Some(core) = core.as_mut() {
                core.push_task(self, task);   // 本线程:直接 push 到 pending 队列
            }
        }
        _ => {
            // Track that a task was scheduled from **outside** of the runtime.
            self.shared.scheduler_metrics.inc_remote_schedule_count();
            self.shared.inject.push(task);    // 跨线程:进 injector
            self.driver.unpark();
        }
    });
}
```

([tokio/src/runtime/scheduler/current_thread/mod.rs:666-693](../tokio/tokio/src/runtime/scheduler/current_thread/mod.rs#L666-L693))

对比 multi-thread 的 `schedule_task`(第 7 章读过)——multi-thread 是"worker 塞 local、外部塞 injector"。current-thread 是**同一个套路**:本线程(就是 runtime 跑的那个线程)直接 `push_task`(进 `VecDeque`);外部线程(主线程、blocking pool 线程)走 `self.shared.inject.push`(进 injector)+ `driver.unpark()`(叫醒 runtime 线程)。

注意:**current-thread 也有 injector**(在 `Shared.inject`,见 [mod.rs:87](../tokio/tokio/src/runtime/scheduler/current_thread/mod.rs#L87))。为什么单线程也要 injector?——因为**外部线程也可能 spawn task**(比如 blocking pool 里的同步任务完成后唤醒 task)。这些 task 必须先放 injector,runtime 线程下次循环时从 injector 取。injector 在这里的作用和 multi-thread 一样——**跨线程投递的中转站**。

### 1.3 主循环:同一个骨架,简化版

current-thread 的主循环(`CoreGuard::block_on`,第 6 章已读过)和 multi-thread 的 `Context::run` **骨架完全一致**:`'outer: loop` → poll block_on future → 循环 poll `event_interval` 个 task → 没活 park。差别只在:

- current-thread 没有 `steal_work`(没的偷,只有自己一个"worker")。
- current-thread 的 `next_task` 取 task 直接从 `VecDeque` `pop_front`,没有 LIFO slot 优先。
- current-thread 的 driver 是 `Option<Driver>` 独占,park 时 `take` 出来用完放回(下面详拆)。

### 1.4 为什么保留 current-thread:零同步开销的纯粹

到这里你可能问:既然 multi-thread 这么强,**`new_multi_thread().worker_threads(1)` 不就模拟了单线程吗?为什么还要单独的 current-thread 模式?**

> **不这样会怎样(如果用 multi-thread + 1 worker 模拟单线程)**:即便只有 1 个 worker,multi-thread 的代码路径**仍然**走完整的那一套——
> - 每次 push/pop 走 `queue.rs` 的双 head 原子字操作(`AtomicU64::compare_exchange_weak`),哪怕根本没有别的线程竞争,原子指令的开销(几纳秒 + cache line 同步)照样付。
> - 每次 park 走 `Parker` 的 `TryLock<Driver>` 抢锁逻辑(虽然只有自己一个,但 `try_lock` 的代码路径要跑)。
> - `Idle` 状态原子(`num_searching` / `num_unparked` 打包的 `AtomicUsize`)照样更新,哪怕 num_workers=1 时这些状态毫无意义。
> - LIFO slot 的逻辑(`lifo_enabled`、`MAX_LIFO_POLLS_PER_TICK`)照样跑,带来微小的分支开销。
>
> 这些开销在多核高并发下微不足道(相比偷工作的收益),但在**单线程**场景下就是纯浪费——你为"永远用不到的并发"付了全套代价。
>
> current-thread 的存在,就是给"我知道我是单线程、不需要这些"的场景一个**零浪费**的选择:`VecDeque` 的 `push_back`/`pop_front` 是普通内存操作,比 `AtomicU64::CAS` 快一个数量级;`Option<Driver>` 的 `take`/放回是所有权转移,比 `TryLock` 简单;没有 `Idle`、没有 LIFO、没有 work-stealing。**纯粹、零开销**。

> **钉死这件事**:current-thread 存在的全部理由是**"零同步开销的纯粹"**。它在源码层的体现,就是把 multi-thread 那套并发装备全部拿掉,只留骨架。这种"为不同场景做不同简化"的设计,体现了 Rust 生态"零成本抽象"的精神——**你不用的特性,不该让你付代价**。tokio 没有"一刀切用一个泛型 runtime 适配所有场景",而是分两套实现,让用户按场景选。这也是为什么 Rust 把 async 做成语言特性、runtime 留给库(第 0 章讲过)——库可以针对场景做极致定制。

#### current-thread 的典型适用场景

源码里没有"罗列适用场景"的 prose 注释,但从它的设计能反推:

1. **嵌入式 / no_std**(embassy 等运行时受 tokio 启发):单核 MCU,没有"多 worker"的概念,current-thread 是天然选择。
2. **单线程小程序**:命令行工具、简单的异步脚本,不需要多核并行,单线程够用且最简。
3. **测试**:单元测试里跑 async 代码,单线程确定性高(没 work-stealing 的随机性),bug 易复现。
4. **库的内部 runtime**:某些库(如 `tokio-util`)在内部起一个单线程 runtime 跑后台任务,不抢用户的主 runtime 资源。
5. **配合 `LocalSet` 跑非 `Send` 的 task**:`LocalSet` 必须搭 current-thread runtime(因为非 `Send` task 不能跨线程,multi-thread 的 work-stealing 会破坏这个保证)。

---

## 二、两模式逐项对比

把两模式并排放,逐项看清差异。这是本章的对照表。

### 2.1 worker 数量与队列结构

| 维度 | current-thread | multi-thread |
|------|----------------|--------------|
| worker 线程数 | **固定 1**(就是调 `block_on` 的那个线程) | **默认 = CPU 核数**(`worker_threads.unwrap_or_else(num_cpus)`,builder.rs L1859) |
| task 队列 | 单个 `VecDeque<Notified>`(FIFO) | 每个 worker 一个 `queue::Local`(256 容量无锁环形数组)+ 全局 `Inject` |
| LIFO slot | **无** | **有**(`Core.lifo_slot: Option<Notified>`) |
| work-stealing | **无**(没的偷) | **有**(随机 victim + 偷一半) |
| 排队顺序 | 纯 FIFO(`pop_front`) | LIFO slot 优先,然后本地队列 FIFO,然后偷/injector |

> **钉死这件事**:队列结构的差异是两模式最直观的差别。current-thread 一个 `VecDeque` 解决一切;multi-thread 是"本地队列 + LIFO slot + injector"三级结构。后者复杂,但换来了多核下的负载均衡和缓存友好。前者简单,但只能用满一个核。

### 2.2 driver 持有:TryLock vs Option

这是两模式**最深的实现差别**,也是第 6 章技巧精解里"multi-thread Parker 双睡眠方式"的根源。

**multi-thread**:`TryLock<Driver>`(共享、可竞争)

```rust
// tokio/src/runtime/scheduler/multi_thread/park.rs(摘录)
struct Shared {
    /// Shared driver. Only one thread at a time can use this
    driver: TryLock<Driver>,
}
```

([tokio/src/runtime/scheduler/multi_thread/park.rs:51-53](../tokio/tokio/src/runtime/scheduler/multi_thread/park.rs#L51-L53))

N 个 worker 共享同一个 driver(一个 epoll fd)。worker park 时 `try_lock` driver——抢到了睡 epoll(顺便推进 I/O),没抢到睡 condvar。这是第 6 章技巧精解拆过的"双睡眠方式"。

**current-thread**:`Option<Driver>`(独占,所有权转移)

```rust
// tokio/src/runtime/scheduler/current_thread/mod.rs(摘录)
struct Core {
    // ...
    /// Runtime driver
    ///
    /// The driver is removed before starting to park the thread
    driver: Option<Driver>,
    // ...
}
```

([tokio/src/runtime/scheduler/current_thread/mod.rs:68-71](../tokio/tokio/src/runtime/scheduler/current_thread/mod.rs#L68-L71))

注释明说:"The driver is removed before starting to park the thread"。看 park 的实现:

```rust
// tokio/src/runtime/scheduler/current_thread/mod.rs(摘录)
fn park(&self, mut core: Box<Core>, handle: &Handle) -> Box<Core> {
    let mut driver = core.driver.take().expect("driver missing");
    // ... 用 driver park(睡 epoll)
    if !self.has_pending_work(&core) {
        core = self.park_internal(core, handle, &mut driver, None);
        // ...
    }
    core.driver = Some(driver);   // 用完放回
    core
}
```

([tokio/src/runtime/scheduler/current_thread/mod.rs:380-394](../tokio/tokio/src/runtime/scheduler/current_thread/mod.rs#L380-L394))

**`core.driver.take()` 把 driver 从 Option 里拿出来**(变成 `Driver`,`Option` 变 `None`),用完 `core.driver = Some(driver)` 放回。这是**纯所有权转移**,不是并发控制——单线程,根本没人和它抢。

> **为什么是 Option 而非 TryLock?**——单线程运行时,driver 天然独占,**无需任何同步**。用 `Option` + `take()`/放回,是为了在 park 期间把 driver "借出"给 park 逻辑(它需要 `&mut Driver`),park 完再放回。这是**所有权转移**(Rust 的 `take` 把值移出),编译期保证"同一时刻只有一个地方持有 driver",不需要运行时锁。
>
> 对比 multi-thread 的 `TryLock<Driver>`:多 worker 共享,**运行时**靠 `try_lock` 决定"现在谁用 driver"。这是并发控制的本质——多线程下所有权转移给不了"互斥访问",只能靠锁(或原子)。
>
> **钉死这件事**:`Option<Driver>` vs `TryLock<Driver>` 是 Rust "用类型表达并发模型"的典范。**单线程 → 用所有权(Option + take)表达独占,编译期保证;多线程 → 用运行时同步(TryLock)表达互斥,运行时保证**。同一份"持有 driver"的语义,在两种并发模型下用截然不同的语言机制实现,且都是各自场景下最优的(单线程不付锁开销,多线程不丢共享性)。这是 Rust 类型系统在系统级编程里的威力。

### 2.3 blocking pool 配置

`spawn_blocking` 的去处在两模式下也不同:

```rust
// tokio/src/runtime/builder.rs(摘录)
// current-thread:
let blocking_pool = blocking::create_blocking_pool(self, self.max_blocking_threads);
//                                                         ^^^^^^^^^^^^^^^^^^^^^^^^

// multi-thread:
let worker_threads = self.worker_threads.unwrap_or_else(num_cpus);
let blocking_pool = blocking::create_blocking_pool(self, self.max_blocking_threads + worker_threads);
//                                                         ^^^^^^^^^^^^^^^^^^^^^^^^^ ^^^^^^^^^^^^^^
```

([tokio/src/runtime/builder.rs:1676, 1859-1865](../tokio/tokio/src/runtime/builder.rs#L1676)(current), ([L1859, L1864-1865](../tokio/tokio/src/runtime/builder.rs#L1859-L1865))(multi))

差别:**multi-thread 的 blocking pool 容量 = `max_blocking_threads + worker_threads`**,current-thread 是 `max_blocking_threads`(没有 worker_threads 加项)。

为什么?——multi-thread 模式下,**worker 线程本身在 `spawn_blocking` 满载时会被"借去"当 blocking 线程**(worker.rs 里 `spawn_blocking` 满了,worker 会把自己的 core 让出,转去做 blocking 任务,见 [worker.rs:451-457](../tokio/tokio/src/runtime/scheduler/multi_thread/worker.rs#L451-L457))。所以 blocking pool 的总容量要把 worker 数算进去。current-thread 没有"worker 转 blocking"的机制(单线程不能转,转了 runtime 就停了),所以 blocking pool 就是纯粹的 `max_blocking_threads`。

> **钉死这件事**:这个细节体现了一个深层差异——**multi-thread 的 worker 是"可转换的"**(worker 干活干到一半,可以被借去当 blocking 线程,只要把 core 让给别的 worker 偷走);current-thread 的 runtime 线程是"不可转换的"(它就是 runtime,不能去干 blocking,否则 runtime 停摆)。这种"可转换性"是 multi-thread 灵活性的来源,也是它复杂的来源(要处理 core 让出、被偷、LIFO slot 转移等)。

### 2.4 一张总图:两模式的部件对照

```
   ┌──────────────────────────────┬──────────────────────────────┐
   │       current-thread         │         multi-thread         │
   ├──────────────────────────────┼──────────────────────────────┤
   │ worker 数:固定 1              │ worker 数:默认 = CPU 核数     │
   ├──────────────────────────────┼──────────────────────────────┤
   │ task 队列:                    │ task 队列(三级):             │
   │   VecDeque<Notified>(FIFO)   │   ① lifo_slot(单槽,LIFO)    │
   │                              │   ② 本地队列(256 环形,无锁) │
   │                              │   ③ injector(全局,Mutex链表)│
   ├──────────────────────────────┼──────────────────────────────┤
   │ work-stealing:无              │ work-stealing:随机victim+偷半│
   ├──────────────────────────────┼──────────────────────────────┤
   │ driver:Option<Driver>(独占)  │ driver:TryLock<Driver>(共享) │
   │   park:take→用→放回           │   park:try_lock→抢到睡epoll │
   │                              │         抢不到睡condvar       │
   ├──────────────────────────────┼──────────────────────────────┤
   │ LIFO slot:无                  │ LIFO slot:有(连续3次上限)   │
   ├──────────────────────────────┼──────────────────────────────┤
   │ blocking pool:max_blocking   │ blocking pool:               │
   │                              │   max_blocking + worker_threads│
   ├──────────────────────────────┼──────────────────────────────┤
   │ Idle 状态原子:无              │ Idle:num_searching/unparked │
   │                              │   打包 AtomicUsize(半数门槛) │
   ├──────────────────────────────┼──────────────────────────────┤
   │ 同步开销:零原子、零CAS        │ 同步开销:原子+CAS+TryLock    │
   └──────────────────────────────┴──────────────────────────────┘
```

这张表是本章的核心。**左列每一项的"无/零",都是右列对应项"有/非零"的简化**。current-thread 不是"功能阉割版的 multi-thread",而是"明知单线程不需要这些,刻意省掉的纯粹实现"。

---

## 三、LIFO slot:刚唤醒的 task 优先跑

现在钻进本章技巧精解的主角——LIFO slot。它是 multi-thread 独有的优化,current-thread 没有。

### 3.1 LIFO slot 是什么:一个单槽,装"刚唤醒的 task"

回到第 7 章读过的 `schedule_local`,这里只盯 LIFO 分支:

```rust
// tokio/src/runtime/scheduler/multi_thread/worker.rs(摘录)
fn schedule_local(&self, core: &mut Core, task: Notified, is_yield: bool) {
    // ...
    let should_notify = if is_yield || !core.lifo_enabled {
        core.run_queue.push_back_or_overflow(task, self, &mut core.stats);  // 走队尾
        true
    } else {
        // Push to the LIFO slot
        let prev = core.lifo_slot.take();
        let ret = prev.is_some();
        if let Some(prev) = prev {
            core.run_queue.push_back_or_overflow(prev, self, &mut core.stats);  // 原来的挤到队尾
        }
        core.lifo_slot = Some(task);   // 新 task 进 LIFO slot
        ret
    };
    // ...
}
```

([tokio/src/runtime/scheduler/multi_thread/worker.rs:1353-1385](../tokio/tokio/src/runtime/scheduler/multi_thread/worker.rs#L1353-L1385))

逻辑:**worker 自己唤醒的 task(非 yield),如果 LIFO 启用,塞进 `lifo_slot` 单槽;如果 slot 里原来有 task,把它挤到本地队列队尾**。

配合 `next_local_task`:

```rust
// tokio/src/runtime/scheduler/multi_thread/worker.rs(摘录)
fn next_local_task(&mut self) -> Option<Notified> {
    self.lifo_slot.take().or_else(|| self.run_queue.pop())
}
```

([tokio/src/runtime/scheduler/multi_thread/worker.rs:1132-1134](../tokio/tokio/src/runtime/scheduler/multi_thread/worker.rs#L1132-L1134))

**先取 lifo_slot,再 pop 本地队列**。所以 LIFO slot 里的 task,**下次第一个被 poll**。

`Core` 结构体里的字段定义和设计注释:

```rust
// tokio/src/runtime/scheduler/multi_thread/worker.rs(摘录)
/// When a task is scheduled from a worker, it is stored in this slot. The
/// worker will check this slot for a task **before** checking the run
/// queue. This effectively results in the **last** scheduled task to be run
/// next (LIFO). This is an optimization for improving locality which
/// benefits message passing patterns and helps to reduce latency.
lifo_slot: Option<Notified>,

/// When `true`, locally scheduled tasks go to the LIFO slot. When `false`,
/// they go to the back of the `run_queue`.
lifo_enabled: bool,
```

([tokio/src/runtime/scheduler/multi_thread/worker.rs:115-124](../tokio/tokio/src/runtime/scheduler/multi_thread/worker.rs#L115-L124))

注释把设计动机说清楚了:**"improving locality which benefits message passing patterns and helps to reduce latency"**——提升局部性,有利于消息传递模式,降低延迟。

### 3.2 为什么 LIFO:消息传递模式的局部性

这里要讲清"为什么刚唤醒的 task 优先跑能提升局部性"。最典型的场景是**消息传递(message passing)**:

> **场景**:task A 通过 channel 给 task B 发消息,这会唤醒 B(B 之前在等 channel)。tokio 的 channel(第 5 篇详拆)的 `send` 内部调 `waker.wake()`,经 `schedule_task` 把 B 塞进当前 worker 的 LIFO slot。worker poll 完 A(A 发完消息返回 Pending 或 Ready),下一个 poll 的就是 B(从 LIFO slot 取)。
>
> **为什么这样快**:
> - **cache 热**:A 刚 poll 完,它读写过的数据(channel 的内部 buffer、共享状态)还在 worker 的 L1/L2 cache 里。立刻 poll B,B 要读的正是这些数据(它要从 channel 拿消息)——**cache 直接命中**,不用从内存重新加载。
> - **消息在寄存器/缓存里流转**:A 发的消息可能还在寄存器里(没被刷到内存),B 立刻读,省一次内存往返。
> - **延迟低**:B 不用排队等(如果排到本地队列队尾,前面可能还有几百个 task,延迟几毫秒到几十毫秒)。立刻 poll,B 的响应时间最短。

> **不这样会怎样(如果用 FIFO,新 task 排队尾)**:A 唤醒 B,B 排到本地队列队尾。worker 接着 poll 队首的其他 task(假设有 100 个),poll 这 100 个 task 的过程中,**A 写过的 channel 数据逐渐被挤出 cache**(被其他 task 的数据覆盖)。等轮到 B poll 时,B 要读的 channel 数据已经不在 cache,得从内存加载——一次 cache miss 几十纳秒,如果 B 读很多数据,累加成微秒。而且 B 的响应延迟 = 100 个 task 的 poll 时间,可能毫秒级。
>
> 消息传递模式(channel、Notify、oneshot)在异步代码里**极其常见**——actor 模型、生产者-消费者、请求-响应,全是。LIFO slot 让这类模式的延迟和缓存友好度大幅提升。这是 tokio 多线程性能的关键优化之一。

> **钉死这件事**:LIFO slot 的本质是**"用排队顺序换缓存命中"**。它打破 FIFO 的"公平"(后唤醒的先跑),换来的是消息传递模式下"发送方和接收方共享 cache"的局部性红利。这是一个**反直觉但高性能**的设计——直觉上"先到先服务"才公平,但异步运行时追求的不是公平,是吞吐和延迟。LIFO slot 是 tokio "为常见模式做特殊优化"的范例。

### 3.3 LIFO 的命门:ping-pong 饿死

可 LIFO 有个**致命的命门**——**ping-pong 饿死**。源码注释直接讲了这个场景:

```rust
// tokio/src/runtime/scheduler/multi_thread/worker.rs(摘录)
// Disable the LIFO slot if we reach our limit
//
// In ping-ping style workloads where task A notifies task B,
// which notifies task A again, continuously prioritizing the
// LIFO slot can cause starvation as these two tasks will
// repeatedly schedule the other. To mitigate this, we limit the
// number of times the LIFO slot is prioritized.
if lifo_polls >= MAX_LIFO_POLLS_PER_TICK {
    core.lifo_enabled = false;
    super::counters::inc_lifo_capped();
}
```

([tokio/src/runtime/scheduler/multi_thread/worker.rs:736-745](../tokio/tokio/src/runtime/scheduler/multi_thread/worker.rs#L736-L745))

**ping-pong 场景**:task A 唤醒 B(B 进 LIFO slot),worker poll 完 A 接着 poll B;B 又唤醒 A(A 进 LIFO slot,把 B 挤到队尾——不,B 已经 poll 完了,B 又唤醒 A);worker poll 完 B 接着 poll A;A 又唤醒 B……**A 和 B 互相唤醒,无限循环,本地队列里其他几百个 task 永远轮不到**。这就是"ping-pong 饿死"。

tokio 的对策:**限制 LIFO slot 连续 poll 的次数**,超过上限就关闭 LIFO(本次 tick 内)。看那个上限常量:

```rust
// tokio/src/runtime/scheduler/multi_thread/worker.rs(摘录)
/// Value picked out of thin-air. Running the LIFO slot a handful of times
/// seems sufficient to benefit from locality. More than 3 times probably
/// is over-weighting. The value can be tuned in the future with data that shows
/// improvements.
const MAX_LIFO_POLLS_PER_TICK: usize = 3;
```

([tokio/src/runtime/scheduler/multi_thread/worker.rs:260-264](../tokio/tokio/src/runtime/scheduler/multi_thread/worker.rs#L260-L264))

**`MAX_LIFO_POLLS_PER_TICK = 3`**。注释自嘲"picked out of thin-air"(凭感觉选的),但解释了理由:**"Running the LIFO slot a handful of times seems sufficient to benefit from locality"**——LIFO poll 几次就够吃到局部性红利了,超过 3 次"probably is over-weighting"(可能过度倾斜)。这个数是经验值,可调。

完整的 LIFO poll 循环在 `run_task` 里(关键部分):

```rust
// tokio/src/runtime/scheduler/multi_thread/worker.rs(摘录,关键结构)
// run_task 内部的 LIFO 循环
let mut lifo_polls = 0;
loop {
    // As long as there is budget remaining and a task exists in the
    // `lifo_slot`, then keep running.
    // ... 检查 budget(第 9 章)...

    let task = match core.lifo_slot.take() {
        Some(task) => task,
        None => {
            self.reset_lifo_enabled(&mut core);   // LIFO slot 空了,重新启用
            return core;
        }
    };

    // ... budget 不够时:把 task 降级到队尾,LIFO 保持 enabled ...
    // if not enough budget: push task to run_queue, return (debug_assert lifo_enabled)

    lifo_polls += 1;
    // ... poll task ...

    // Disable the LIFO slot if we reach our limit
    if lifo_polls >= MAX_LIFO_POLLS_PER_TICK {
        core.lifo_enabled = false;
        super::counters::inc_lifo_capped();
    }
}
```

([tokio/src/runtime/scheduler/multi_thread/worker.rs:689-765](../tokio/tokio/src/runtime/scheduler/multi_thread/worker.rs#L689-L765),结构摘录)

读这段,抓三个状态切换:

- **LIFO slot 空了** → `reset_lifo_enabled`(重新启用,下次 tick 又能用 LIFO)。
- **budget 不够**(第 9 章)→ 当前 LIFO task **降级**到队尾,LIFO slot **保持 enabled**(下次 tick 还能用)。注意这里**不**关闭 LIFO——budget 是另一回事。
- **LIFO poll 满 3 次** → `lifo_enabled = false`(本次 tick 关闭 LIFO,后续 schedule_local 走队尾)。下次 tick 顶部 `Context::run` 会 `reset_lifo_enabled` 重新启用。

### 3.4 LIFO slot 和 budget 的关系:降级,不关闭

这里有个**容易混淆的点**,值得专门讲清(因为第 9 章要讲 budget,本章要衔接好):

**budget 耗尽时,LIFO task 降级到队尾,但 LIFO slot 保持启用**。源码注释明说:

```rust
// Not enough budget left to run the LIFO task, push it to the back of the
// queue and return.
// ... push task to run_queue ...
// (注意:没有 lifo_enabled = false)
debug_assert!(core.lifo_enabled);
```

([tokio/src/runtime/scheduler/multi_thread/worker.rs:716-728](../tokio/tokio/src/runtime/scheduler/multi_thread/worker.rs#L716-L728),`debug_assert` 在 L728)

**为什么 budget 不关 LIFO?**——因为 budget 耗尽是"当前这个 task 该让出了"(防它霸占线程,第 9 章主题),不是"LIFO 策略本身有问题"。下次 tick 重新开始,LIFO 照样启用。budget 是**任务粒度**的让出,LIFO 是**调度策略**——两者正交。

**真正触发 LIFO 关闭的是 ping-pong 次数上限**(3 次),不是 budget。这两个机制各自独立:budget 管"单个 task 别霸占线程",LIFO 上限管"A/B 互唤醒别饿死别人"。

> **钉死这件事**:LIFO slot 有**两个独立的"防滥用"机制**:① **budget**(第 9 章)——单个 task poll 太多次就强制让出,LIFO task 也一样(降级到队尾);② **MAX_LIFO_POLLS_PER_TICK=3**——LIFO 连续 poll 超 3 次就关闭 LIFO(防 ping-pong)。两者正交:budget 耗尽不关 LIFO,LIFO 关闭也不消耗 budget。理解这个正交性,你就理解了 tokio 防饿死的多层防御——每一层管一种滥用,不互相干扰。

### 3.5 为什么 current-thread 没有 LIFO slot

最后一个问题:current-thread 为什么不搞 LIFO slot?

> **不这样会怎样(如果 current-thread 也加 LIFO slot)**:加 LIFO slot 要付出代价——`Core` 多一个字段、`schedule` 多一个分支、`next_task` 多一次 `take`、要处理 ping-pong 上限、要和 budget 协调。current-thread 的核心卖点是**简洁**,加 LIFO slot 就破坏了这个卖点。
>
> 更重要的是,**current-thread 场景下 LIFO 的收益有限**:
> - current-thread 常用于**嵌入式 / 测试 / 单线程小程序**,这些场景消息传递模式不那么密集,LIFO 的局部性红利不明显。
> - current-thread 没有 work-stealing,**task 不会从别的 worker 偷来**,所以"刚唤醒的 task cache 热"这个前提没那么强(worker 自己唤醒的 task,cache 当然热,但这和 multi-thread 一样;差别在于 multi-thread 还要应对"偷来的 task"的缓存问题,LIFO 能缓解)。
> - current-thread 的 `VecDeque` 操作已经足够快(`push_back`/`pop_front` 是 O(1) 且无原子),FIFO 的"延迟略高"在单线程下不是瓶颈。
>
> 收益有限 + 破坏简洁,tokio 选择**不给 current-thread 加 LIFO slot**。这是"按场景做裁剪"的体现——每个特性都问"在这个场景下收益是否大于代价",而不是"一刀切给所有模式都加上"。

---

## 技巧精解:LIFO slot——用排队顺序换缓存命中 + 用次数上限防饿死

这一节把 LIFO slot 拆透,它是本章最硬核的技巧。

### 这套设计在解决什么

multi-thread 调度器面临一个两难:

- **FIFO**(先到先服务)公平,但对消息传递模式不友好——刚唤醒的 task 排队尾,等几百个 task poll 完才轮到,延迟高 + cache 冷了。
- **纯 LIFO**(后到先服务)对消息传递友好,但会饿死老 task——新 task 不断插队,队尾的 task 永远轮不到。

LIFO slot 是两者的折中:**单个槽的 LIFO**(只让"最近一个被唤醒的 task"插队),**配上次数上限**(连续 LIFO poll ≤ 3 次就退回 FIFO)。这样既吃到消息传递的局部性红利,又限制了饿死风险。

### 反面对比 A:纯 FIFO

```rust
// 简化示意,非源码原文:纯 FIFO
fn schedule_local(&self, core: &mut Core, task: Notified) {
    core.run_queue.push_back_or_overflow(task, ...);  // 永远排队尾
}
fn next_local_task(&mut self) -> Option<Notified> {
    self.run_queue.pop()   // 永远从队首
}
```

> **不这样会怎样**:task A 唤醒 B(B 排队尾),worker 接着 poll 队首的 task 1、task 2、...、task 100。这 100 个 task 的 poll 期间,A 写的 channel 数据被挤出 cache。轮到 B 时,cache 全冷,B 要从内存重新加载——慢。而且 B 的延迟 = 100 个 task 的 poll 时间,可能毫秒级。消息传递模式(在异步代码里极常见)的延迟和吞吐都被 FIFO 拖累。

### 反面对比 B:纯 LIFO(所有 task 都插队)

```rust
// 简化示意,非源码原文:纯 LIFO
fn schedule_local(&self, core: &mut Core, task: Notified) {
    core.run_queue.push_front(task);  // 永远插队首
}
```

> **不这样会怎样**:新唤醒的 task 永远插队首。如果有个 task 持续唤醒新 task(比如一个 dispatcher task 不断 spawn worker task),**所有新 task 都插队,老 task 永远在队尾轮不到**——饿死。这在异步服务器里很常见(一个 acceptor task 不断为每个连接 spawn handler task)。

### 正解:单槽 LIFO + 次数上限

tokio 的做法:

```rust
// 简化示意,基于源码逻辑
struct Core {
    lifo_slot: Option<Notified>,    // 单槽,只装"最近一个"
    run_queue: Local,               // 主队列,FIFO
    lifo_enabled: bool,
}

fn schedule_local(&self, core: &mut Core, task: Notified, is_yield: bool) {
    if is_yield || !core.lifo_enabled {
        core.run_queue.push_back(task);    // yield 或 LIFO 关 → 走队尾
    } else {
        let prev = core.lifo_slot.take();
        if let Some(prev) = prev {
            core.run_queue.push_back(prev);  // 原来的挤到队尾
        }
        core.lifo_slot = Some(task);          // 新 task 进 LIFO slot
    }
}

fn next_local_task(&mut self) -> Option<Notified> {
    self.lifo_slot.take().or_else(|| self.run_queue.pop())  // LIFO slot 优先
}
```

**两个关键约束**:

1. **单槽**:LIFO slot 只装**一个** task。新 task 来了,把原来的挤到队尾。这样"插队"只发生在"最近一个 task"上,不会无限插队(老 task 至少在主队列里按 FIFO 推进)。
2. **次数上限**(`MAX_LIFO_POLLS_PER_TICK=3`):连续 LIFO poll ≤ 3 次,超过就 `lifo_enabled = false`,本次 tick 内所有新 task 都走队尾。下次 tick 顶部重新启用。

**为什么不直接限制"单槽"就够,还要加次数上限?**——单槽只解决"插队数量"问题,但 ping-pong 场景下,A 和 B 互相唤醒,**单槽永远在 A/B 之间切换**(A 唤醒 B,B 进槽;poll B,B 唤醒 A,A 进槽,B 被挤到队尾但又被 poll 了所以没事;poll A,A 唤醒 B,B 进槽...)。单槽不解决 ping-pong——A/B 一直霸占 LIFO slot,主队列里的 task 还是轮不到。次数上限就是治这个:**3 次后强制关 LIFO,让主队列里的 task 有机会跑**。

### 反面对比 C:用时间片(像 OS 抢占式调度)

有人可能想:为什么不学 OS 的"时间片",给每个 task 固定时长,超了就让出?

> **不这样会怎样**:OS 时间片靠**时钟中断**强制抢占,但 tokio 是**协作式调度**(第 0、9 章),没有时钟中断——task 不主动让出,谁也抢不走它。给 LIFO slot 配"时间片",要么用真实计时器(开销大、且协作式下计时器响了也没用,task 不停还是不停),要么用 poll 次数(这就是 budget,第 9 章)。budget 是**每个 task**的让出机制,不是**LIFO 策略**的让出机制——两者正交。LIFO 需要的是"策略级的让出"(关 LIFO 让 FIFO 接管),budget 是"任务级的让出"(单个 task 让出线程)。所以 LIFO 用次数上限,不用 budget。

### sound 性与正确性

LIFO slot 不涉及 `unsafe`(它是个普通的 `Option<Notified>`,Rust 编译器保证借用正确)。它的"正确性"主要是**调度正确性**——保证不饿死、不丢 task。这套保证靠:

1. **单槽 + 主队列** 的两级结构:task 不会丢(LIFO slot 满了就挤到主队列)。
2. **lifo_enabled 的状态机**:连续 3 次关闭、tick 顶部重置,保证 LIFO 不会永久开启。
3. **`reset_lifo_enabled` 在 `Context::run` 顶部调用**([worker.rs:564](../tokio/tokio/src/runtime/scheduler/multi_thread/worker.rs#L564)):保证 core 被偷后(可能被前一个 owner 关了 LIFO),新 owner 重新启用。注释明说:"Reset `lifo_enabled` here in case the core was previously stolen from a task that had the LIFO slot disabled."

> **钉死这件事**:LIFO slot 是个**纯 Rust safe 代码**的优化(无 unsafe),它的精妙在**调度策略的设计**,不在底层技巧。这跟第 5 章(状态位打包)、第 7 章(双 head 无锁队列)那种"底层技巧"不同——LIFO slot 是"算法层"的巧思。tokio 的技巧密度不仅在底层原子操作,也在调度策略层。这种"策略层优化"在工程上往往收益巨大(消息传递模式延迟降低几倍到几十倍),且实现简单(就一个 `Option` + 一个 bool + 一个常量),性价比极高。

---

## 章末小结

### 用"餐厅服务员"比喻回顾本章

1. **current-thread 是"只有一个服务员的餐厅"** —— 一个服务员(线程),手边一本订单本(`VecDeque`),订单来了 `push_back`、处理时 `pop_front`,FIFO,简单到极致。没有同事可偷工作、没有 LIFO 插队、没有 driver 抢锁——**所有 multi-thread 的复杂装备都没有,因为单线程用不上**。
2. **multi-thread 是"多个服务员的餐厅"** —— 每个服务员手边一摞自己的订单(本地队列)+ 一个"刚接的单"特权位(LIFO slot)+ 前台总订单本(injector)。忙不过来的服务员把订单转给闲的同事(work-stealing);刚接的单优先处理(LIFO slot,因为客户还在吧台、cache 热);所有服务员共享一个厨房叫号系统(driver,靠 TryLock 抢着用)。
3. **LIFO slot 是"刚接的单优先"** —— 服务员刚从客户手里接过一张单,先处理它(客户还站着、信息还在嘴边);如果这当口又来一张新单,原来的那张排到自己那摞的队尾。但**连续这么干 3 次就停**(防 A/B 互相塞单把别的订单饿死),改回 FIFO。
4. **driver 的持有:独占 vs 共享** —— current-thread 的服务员**独占**厨房叫号系统(`Option<Driver>`,take 出来用,没人和他抢);multi-thread 的服务员们**共享**叫号系统(`TryLock<Driver>`,谁 park 谁抢,抢到睡 epoll,没抢到睡 condvar)。
5. **blocking pool 的差别** —— multi-thread 忙不过来时,服务员能"转岗"当传菜的(`spawn_blocking` 满载时 worker 转 blocking);current-thread 的服务员不能转(转了餐厅停摆),所以 blocking pool 就是独立的传菜队。

### 本章在全书主线中的位置

记住全书的二分法:**调度执行(让就绪的任务跑) vs 事件唤醒(让等待的任务不空耗、就绪了再叫)**。

本章服务的是**调度执行**那一面——更具体说,是**"调度器在不同并发模型下的不同实现"**这一面。第 6 章立了 worker 主循环的骨架,第 7 章拆了 multi-thread 的内部,本章把两模式并排对比,看清"同一个骨架,两种裁剪"。

本章把第 2 篇的"调度执行"面收束了一大半,但还差最后一块:**协作式让出**。前面所有章节,task 都假设"在 await 点自觉让出"。可如果一个 task **不自觉**(死循环、超长 CPU 计算、忘了 await),会怎样?LIFO slot 的次数上限只防 ping-pong,不防"单个 task 霸占线程"。这是 budget 机制的戏——第 9 章拆。

### 五个"为什么"清单

1. **为什么有 current-thread 模式?**:它**省掉了 multi-thread 的所有同步开销**(无锁队列、steal CAS、idle 原子、TryLock、LIFO slot),task 队列是个普通 `VecDeque`,push/pop 零原子零跨核。对于单核嵌入式 / 单线程小程序 / 测试 / 配合 LocalSet 跑非 Send task 的场景,这是极致简洁。用 multi-thread + 1 worker 模拟单线程,会为"永远用不到的并发"付全套代价。
2. **current-thread 和 multi-thread 在 task 队列上差在哪?**:current-thread 就一个 `VecDeque<Notified>`(FIFO),无锁无原子;multi-thread 是"lifo_slot(单槽 LIFO)+ 本地队列(256 无锁环形)+ injector(全局 Mutex 链表)"三级结构。前者简,后者强(多核负载均衡 + 缓存友好)。
3. **driver 持有的差别为什么重要?**:current-thread 是 `Option<Driver>`(独占,take/放回的所有权转移,无并发);multi-thread 是 `TryLock<Driver>`(共享,park 时 try_lock,抢到睡 epoll 没抢到睡 condvar)。这决定了"谁能睡 epoll"——单线程独占,多线程靠抢锁轮流。这是 Rust "用类型表达并发模型"的典范:单线程用所有权(编译期),多线程用运行时同步。
4. **LIFO slot 是什么?为什么用?**:multi-thread 独有的单槽优化——刚被唤醒的 task(通常 cache 热)塞进 `lifo_slot`,下次第一个被 poll(优先于本地队列)。提升消息传递模式(channel、Notify)的局部性、降低延迟。本质是"用排队顺序换缓存命中"——打破 FIFO 公平,换吞吐和延迟。
5. **LIFO slot 怎么防饿死?**:**连续 LIFO poll ≤ 3 次**(`MAX_LIFO_POLLS_PER_TICK=3`),超过就 `lifo_enabled = false`(本次 tick 关 LIFO,新 task 走队尾),下次 tick 顶部 `reset_lifo_enabled` 重新启用。这专门防 **ping-pong 饿死**(A 唤醒 B、B 唤醒 A,无限循环挤掉别的 task)。注意:budget 耗尽(第 9 章)只让当前 LIFO task 降级到队尾,**不关 LIFO**——budget 是任务级让出,LIFO 上限是策略级让出,两者正交。

### 想继续深入,该往哪钻

- **本章引用的核心源码(按重要度排)**:
  - [`tokio/src/runtime/scheduler/multi_thread/worker.rs`](../tokio/tokio/src/runtime/scheduler/multi_thread/worker.rs) —— **LIFO slot 主战场**。`Core.lifo_slot`/`lifo_enabled` 字段 + 设计注释(L115-127)、`MAX_LIFO_POLLS_PER_TICK=3`(L260-264)、`run_task` 的 LIFO 循环(L630-766,关键:降级 L716-728、关闭 L736-745)、`reset_lifo_enabled`/`assert_lifo_enabled_is_correct`(L769-778)、`schedule_local`(L1353-1385,LIFO 分支)、`next_local_task`(L1132-1134,LIFO 优先)、core 被让出时 LIFO 转移(L451-457)。
  - [`tokio/src/runtime/scheduler/current_thread/mod.rs`](../tokio/tokio/src/runtime/scheduler/current_thread/mod.rs) —— current-thread 对照版。`Core.tasks: VecDeque`(L61-82)、`next_task`/`next_local_task`/`push_task`(L324-351,FIFO)、`schedule`(L666-693,本线程直推/外部 injector)、`park` + `Option<Driver>` take/放回(L380-394)。
  - [`tokio/src/runtime/scheduler/multi_thread/park.rs`](../tokio/tokio/src/runtime/scheduler/multi_thread/park.rs) —— `TryLock<Driver>`(L51-53,共享 driver)。
  - [`tokio/src/runtime/builder.rs`](../tokio/tokio/src/runtime/builder.rs) —— `Kind` enum(L237-242)、`new_current_thread`/`new_multi_thread`(L255-273,默认 event_interval=61 注释 "copied from golang")、`build` 分发(L1062-1068)、blocking pool 容量差异(L1676 current vs L1859/L1864-1865 multi)、`enable_eager_driver_handoff` current 强制 false(L1706-1709)。
- **亲手对比两模式**:用 builder 分别建 current-thread 和 multi-thread(1 worker)runtime,跑同一个"消息传递密集"的 workload(比如 oneshot channel 串联 1 万个 task),用 `criterion` 基准测试对比延迟。你会发现 multi-thread(哪怕 1 worker)因为有 LIFO slot,消息传递延迟显著低于 current-thread。这直观验证了 LIFO slot 的价值。
- **用 `tokio-console` 观察 LIFO 行为**:`tokio-console` 能看到 task 的调度顺序。跑一个 ping-pong workload(A/B 互唤醒),观察 LIFO slot 怎么在 A/B 之间切换、3 次后怎么关 LIFO、别的 task 怎么终于轮到。配 `counters::inc_lifo_capped()` 的 metrics,能看到 LIFO 被关的次数。
- **读 `loom` 测试**:tokio 对 LIFO slot 有专门的 loom 测试(在 worker.rs 的 `#[cfg(test)]` 模块和 tests/loom_*.rs),穷举 LIFO + budget + work-stealing 各种交错。想真正理解"LIFO 不饿死"的保证,看这些测试。
- **下一站**:LIFO slot 讲清了"刚唤醒的 task 优先跑 + 防互唤醒饿死"。可还有一类饿死没治——**单个 task 霸占线程**(死循环、超长 CPU 计算、忘了 await)。LIFO 次数上限只管"策略级"饿死,不管"任务级"霸占。这是 **budget** 机制的戏——每个 task 默认跑约 128 次 poll 就强制让出,防止它把同线程其他 task 饿死。翻开 **第 9 章 · 协作式让出与 budget:为什么不能让一个 task 一直占着线程**——我们拆透 budget,兑现第 0 章"协作式调度"的兜底伏笔,收束第 2 篇。

---

> 两模式讲清了:current-thread 简到极致(一个 VecDeque、独占 driver、无 LIFO),multi-thread 装备齐全(无锁队列、work-stealing、LIFO slot、共享 driver)。LIFO slot 用"刚唤醒优先 + 3 次上限"吃到消息传递的缓存红利又不饿死别人。可这些机制都假设 task 自觉让出——要是有个 task **不自觉**(死循环、忘了 await),怎么办?翻开 **第 9 章 · 协作式让出与 budget**——兑现第 0 章"协作式调度"的兜底伏笔。
