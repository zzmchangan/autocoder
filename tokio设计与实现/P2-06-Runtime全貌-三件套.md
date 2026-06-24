# 第 6 章 · Runtime 全貌:scheduler + reactor + timer 三件套

> **核心问题**:第 1 篇攒下的 task,在源码里就是一个堆上的 `Cell`(第 5 章):顶部状态字、中间 Future、底部 JoinHandle 回执。可这个 task **自己不会跑**——它需要有人把它塞进队列、有 worker 线程从队列里把它取出来 `poll`、有谁在它挂起时盯住"它在等的那个 I/O / 时间"并在就绪时把它叫醒、worker 没活时还得能睡下去省 CPU。这套"把 task 真正驱动起来"的机器,长什么样?
>
> 这一章我们不钻进任何单一部件的内部(reactor 留给第 3 篇、调度器队列留给第 7 章、timer 留给第 4 篇),只盯一件事:**一个 `Runtime` 是由哪些部件拼成的、这些部件在一个 worker 线程的主循环里怎么交替工作**——怎么做到"调度 task / poll reactor / 推进 timer / 没活了 park 睡眠"四件事在**同一个循环**里轮转,而且谁也不阻塞谁。这是第 2 篇(运行时心脏)的总览章,把后面三章要分别钻深的对象先摆上桌。
>
> **读完本章你会明白**:
> - 一个 `Runtime` 在源码里是哪三个字段(`scheduler` / `handle` / `blocking_pool`)、它们各自兜什么脏活;为什么"三件套 = scheduler + reactor + timer"这个说法对应到源码,要拆出**第四件 blocking_pool**(同步阻塞任务的隔离区)。
> - 为什么 reactor 和 timer 在源码里**不分家**——它们被层层包成一个叫 `Driver` 的洋葱结构(`Driver` → `TimeDriver` → `IoStack` → `IoDriver`),一次 `driver.park()` 调用就能把"I/O 事件 + 时间到期"两件事**一起推进**,而不用 worker 分别去 poll。
> - 一个 worker 线程的主循环(无论是 multi-thread 还是 current-thread)长什么样:**一个 `while` 循环,轮里依次做四件事**——`tick` 一下 → 维护(每 61 次 poll 一次 driver,不睡)→ 取 task poll → 没活就 park。这四件事的交替,就是"调度与唤醒衔接"的物理基础。
> - **park / unpark** 这对机制(服务员"靠着吧台打盹 / 被叫一声就醒")在 tokio 源码里**不是** `std::thread::park`,而是 `Mutex` + `Condvar` + 一个三态 `AtomicUsize`(EMPTY/PARKED/NOTIFIED)手写的;而且 multi-thread worker 有**两种睡法**——睡在 condvar 上,或睡在 driver turn 上(epoll_wait),取决于有没有抢到 driver 锁。这是本章技巧精解的主角。
>
> **如果一读觉得太难**:先只记住三件事——① `Runtime` = `scheduler`(调度 task)+ `handle`(里面装着 reactor/timer 句柄)+ `blocking_pool`(隔离同步阻塞任务);② worker 在一个循环里反复做四件事:**poll 几十个 task → 没活就调 `driver.park` 睡下去 → reactor/timer 把就绪的 task 叫醒塞回队列 → worker 被叫醒接着 poll**;③ worker 没活时不是"忙等"也不是"占着 CPU",而是真的 `condvar.wait` 睡下去,谁要派单就 `condvar.notify_one` 把它叫醒。三种睡法、四种状态的细节看不懂可以先跳过,但要把"调度和唤醒在一个循环里交替"这个直觉带走。

---

## 章首·一句话点破

> **运行时就是一家餐厅:scheduler 是经理(决定哪个 worker 接哪单)、reactor 是厨房叫号系统(菜好了喊服务员)、timer 是催单闹钟(到点了响);worker 线程是服务员,他在一个循环里穿梭——接单 poll → 没单了就靠着吧台打盹(park)→ 厨房或闹钟喊他(unpark)→ 醒来接着接单。经理、叫号系统、闹钟不是三个独立的人,而是同一个店里的三套系统,服务员的每一次打盹和醒来,都同时被这三套系统服务。**

这是**结论**。这一章倒过来拆:先把"一个 Runtime 在源码里长什么样"掰开,看清三件套(scheduler / reactor / timer)+ blocking_pool 是怎么被打包进一个 `Runtime` 结构、reactor 和 timer 又怎么被层层包成一个 `Driver`;再钻进 worker 线程的主循环,看清"poll task / 推进 driver / park 睡眠"这三件事是怎么在同一个 `while` 循环里交替的;最后落到 park/unpark 这对机制上,看清它为什么不用 `std::thread::park`,而是手写了一套 condvar + 三态原子。

第 5 章结尾留了钩子:"task 自己不会跑,它需要运行时把它塞进队列、有 worker 来 poll、有 reactor 在它挂起时叫醒它。" 这一章回答"这套机器长什么样、怎么转起来"。

---

## 一、一个 `Runtime` 在源码里长什么样

先不碰 worker 循环,只盯**最外层**:`tokio::runtime::Runtime` 这个结构体,到底装了什么。

### 1.1 `Runtime` 的三个字段

直接看定义:

```rust
// tokio/src/runtime/runtime.rs(摘录)
#[derive(Debug)]
pub struct Runtime {
    /// Task scheduler
    scheduler: Scheduler,

    /// Handle to runtime, also contains driver handles
    handle: Handle,

    /// Blocking pool handle, used to signal shutdown
    blocking_pool: BlockingPool,
}
```

([tokio/src/runtime/runtime.rs:97-106](../tokio/tokio/src/runtime/runtime.rs#L97-L106))

三个字段,正好对应三件事:

- **`scheduler: Scheduler`** —— 调度器。`Scheduler` 是个枚举,两种变体:

  ```rust
  // tokio/src/runtime/runtime.rs(摘录)
  pub(super) enum Scheduler {
      CurrentThread(CurrentThread),
      #[cfg(feature = "rt-multi-thread")]
      MultiThread(MultiThread),
  }
  ```

  ([tokio/src/runtime/runtime.rs:122-129](../tokio/tokio/src/runtime/runtime.rs#L122-L129))

  这就是第 8 章要对比的两种模式:`CurrentThread`(单线程,所有 task 跑在调 `block_on` 的那个线程)和 `MultiThread`(多 worker 线程 + work-stealing)。第 7 章拆的 work-stealing,只在 `MultiThread` 这一支里。

- **`handle: Handle`** —— **句柄**。这个字段是本章的关键。它的注释只有一句 "Handle to runtime, also contains driver handles"——意思是:**它不光是"调度器的句柄",里面还塞着 driver 的句柄**。换句话说,reactor 和 timer 的句柄,是从这个 `handle` 字段里掏出来的。下一节详拆。

- **`blocking_pool: BlockingPool`** —— 阻塞线程池。这是**第 0 章那句伏笔的落地**:"如果你要跑同步阻塞代码(`std::thread::sleep`、同步文件 I/O、CPU 密集计算),用 `spawn_blocking`"。`spawn_blocking` 把这种任务**扔进 blocking_pool**——一组专门的 OS 线程,跟 worker 线程**隔离**。为什么隔离?因为同步阻塞任务会**占死**线程,worker 线程绝不能被它占住(否则这一核上的所有 task 都饿死)。blocking_pool 是"为同步任务准备的隔离病房",本章末尾会简略带过,不展开。

> **钉死这件事**:一个 `Runtime` 在源码里是**三个字段**:`scheduler`(调度器,两种模式)+ `handle`(调度器句柄 + driver 句柄,reactor/timer 从这掏)+ `blocking_pool`(同步阻塞任务的隔离区)。第 0 章讲的"三件套(scheduler + reactor + timer)",对应到源码,**scheduler 是独立字段,reactor/timer 不是独立字段而是藏在 `handle` 里**(因为它们是"句柄"——driver 本体在 worker 那边,这里只放一份能在任意线程调用的指针)。

### 1.2 为什么 reactor/timer 不和 scheduler 平级?

这里有个**初读源码必踩的坑**:你可能以为 `Runtime` 应该长这样——

```rust
// 简化示意,非源码原文:想象的"平级三件套"结构(错误!)
struct Runtime {
    scheduler: Scheduler,
    io_driver: IoDriver,
    time_driver: TimeDriver,
    blocking_pool: BlockingPool,
}
```

可源码没有。reactor/timer **没有**作为顶级字段出现。它们去哪了?——**钻进 worker 线程里了**。

> **不这样会怎样(反面)**:如果把 `IoDriver`(reactor)和 `TimeDriver` 作为 `Runtime` 的顶级字段,那它们就是**共享对象**,任意 worker 想用都得先拿锁。可 reactor 的核心是 `epoll_wait`(Linux)——这是一个**会阻塞**的系统调用,**只有调它的那个线程才会真的睡在 epoll 上**。如果 driver 是共享的,那只有一个 worker 能睡在 epoll 上,其他 worker 想推进 I/O 就得等它让出 driver——这就退化成了"所有 worker 抢一个全局 driver 锁",并发度直接砍掉。
>
> tokio 的做法:**driver 本体(driver 进程的 epoll fd、时间轮、signal 处理)是共享的**(放在 `Arc<Shared>` 里),但**谁去 `epoll_wait` / `park` 它,要靠抢锁**——`Shared` 里有个 `driver: TryLock<Driver>`(下一章详拆),worker park 时 `try_lock`,抢到了就用 driver turn 睡(顺便推进 I/O),抢不到就退回 condvar 睡。这样**任意 worker 都可能睡在 epoll 上,但同一时刻只有一个真的在调 epoll_wait**,既共享了 driver 状态,又避开了"多线程同时调 epoll_wait"的混乱。

所以 reactor/timer **本体**藏在 worker 共享的 `Parker` 里(multi-thread)或 worker 自己的 `Core` 里(current-thread),而 `Runtime.handle` 只是**句柄**——一份能在任意线程用的指针,让 task 在被 `spawn` 后,即便不在 worker 线程上,也能拿到 driver 注册自己。这就是为什么 `handle` 字段注释写 "also contains driver handles"——它装的是 **handles(句柄)**,不是 **driver(本体)**。

### 1.3 driver 句柄在 `handle` 里到底长什么样

往 `handle` 里钻一层。`Handle`(公开的)只是个壳:

```rust
// tokio/src/runtime/handle.rs(摘录)
pub struct Handle {
    pub(crate) inner: scheduler::Handle,
}
```

([tokio/src/runtime/handle.rs:13-15](../tokio/tokio/src/runtime/handle.rs#L13-L15))

`inner` 是 `scheduler::Handle`(也是枚举,CurrentThread / MultiThread 两变体)。真正装 driver 句柄的,是**内层的 driver 句柄结构**:

```rust
// tokio/src/runtime/driver.rs(摘录)
#[derive(Debug)]
pub(crate) struct Handle {
    /// IO driver handle
    pub(crate) io: IoHandle,

    /// Signal driver handle
    #[cfg_attr(any(not(unix), loom), allow(dead_code))]
    pub(crate) signal: SignalHandle,

    /// Time driver handle
    pub(crate) time: TimeHandle,

    /// Source of `Instant::now()`
    #[cfg_attr(not(all(feature = "time", feature = "test-util")), allow(dead_code))]
    pub(crate) clock: Clock,
}
```

([tokio/src/runtime/driver.rs:20-35](../tokio/tokio/src/runtime/driver.rs#L20-L35))

四个字段:`io`(reactor 句柄)、`signal`(信号句柄)、`time`(timer 句柄)、`clock`(时间源)。前三个对应第 0 章讲的三件套里的"事件唤醒"那一面(I/O 事件、信号事件、时间事件),`clock` 是个配套的"现在几点"提供者。

> **钉死一件事**:你写 `tokio::net::TcpStream` 或 `tokio::time::sleep(...)`,它们内部要"把自己注册到 reactor/timer",靠的就是**从当前线程的 thread-local 里取出这个 `driver::Handle`,然后用它的 `io` / `time` 字段注册**。这就是为什么"必须在 runtime context 里才能用 `tokio::net`"——不在 context 里,thread-local 拿不到 `driver::Handle`,注册无从谈起。`#[tokio::main]` 展开成 `block_on`,进入 runtime context,这一切才可用。

### 1.4 三件套组装的全景图

把上面拼起来,一个 `Runtime` 的部件关系长这样:

```
   Runtime
   ┌──────────────────────────────────────────────────────────────────┐
   │                                                                  │
   │  scheduler: Scheduler                                            │
   │  ├─ CurrentThread(CurrentThread)   ← 单线程模式(第 8 章)         │
   │  └─ MultiThread(MultiThread)       ← 多线程模式(第 7 章)         │
   │      │                                                           │
   │      └─ 内部持有:N 个 Worker 线程 + 共享的 Arc<Shared>             │
   │           Shared 里装:                                          │
   │             ├─ injector 全局队列(第 7 章)                         │
   │             ├─ idle 计数器(多少 worker 在睡)                    │
   │             └─ driver: TryLock<Driver>  ← reactor/timer 本体在这! │
   │                                                                  │
   │  handle: Handle                                                  │
   │  └─ inner: scheduler::Handle                                    │
   │       └─ 从中可取出 driver::Handle(io/signal/time/clock 句柄)    │
   │            ↑ task 注册 I/O/时间用                                │
   │                                                                  │
   │  blocking_pool: BlockingPool                                     │
   │  └─ spawn_blocking 任务的去处(隔离的同步线程池)                  │
   │                                                                  │
   └──────────────────────────────────────────────────────────────────┘

   Driver 的洋葱结构(reactor + timer 不分家,层层包):
   Driver ─┬─ TimeDriver ─┬─ IoStack ─┬─ ProcessDriver ─┬─ SignalDriver ─┬─ IoDriver
           │              │           │                 │                │
           │              │           │                 │                └─ mio/epoll 真正在这
           │              │           │                 └─ Unix 信号
           │              │           └─ 进程相关
           │              └─ 时间轮(第 4 篇详拆)
           └─ 最外层 Driver(给 worker 用的统一接口)
```

这张图最重要的两个直觉:**① reactor 和 timer 不是独立模块,被层层包成一个 `Driver`**,worker 只调一次 `driver.park()` 就把"时间到期 + I/O 就绪"一起推进;**② driver 本体在共享的 `Arc<Shared>` 里**(靠 `TryLock` 保护),`Runtime.handle` 里只是句柄。这两件事,下一节细拆。

---

## 二、reactor 和 timer 为什么不分家:`Driver` 洋葱

第 0 章把 reactor 和 timer 列为"三件套"里两个并列的部件。但读源码你会发现——**它们根本不是并列的,而是层层嵌套的**。这一节拆这个"洋葱"。

### 2.1 `Driver` 只是一个壳,真正干活的是 `TimeDriver`

看最外层的 `Driver` 结构:

```rust
// tokio/src/runtime/driver.rs(摘录)
#[derive(Debug)]
pub(crate) struct Driver {
    inner: TimeDriver,
}

impl Driver {
    pub(crate) fn new(cfg: Cfg) -> io::Result<(Self, Handle)> {
        let (io_stack, io_handle, signal_handle) = create_io_stack(cfg.enable_io, cfg.nevents)?;
        let clock = create_clock(cfg.enable_pause_time, cfg.start_paused);
        let (time_driver, time_handle) =
            create_time_driver(cfg.enable_time, cfg.timer_flavor, io_stack, &clock);
        Ok((
            Self { inner: time_driver },
            Handle { io: io_handle, signal: signal_handle, time: time_handle, clock },
        ))
    }
    // ...
}
```

([tokio/src/runtime/driver.rs:15-77](../tokio/tokio/src/runtime/driver.rs#L15-L77))

读这段,**抓一个关键句**:`Driver` 内部只包了一个 `TimeDriver`!它**没有**单独的 `IoDriver` 字段。那 reactor(I/O driver)去哪了?——**被塞进 `TimeDriver` 里了**(看 `create_time_driver` 的第三个参数 `io_stack`)。

也就是说,真正的关系是:

```
   Driver
     └─ inner: TimeDriver
           └─ (内部)io: IoStack
                  └─ (内部)ProcessDriver / SignalDriver / IoDriver
```

time driver 不是"和 io driver 并列",而是**把 io driver 包在里面**。这就是"洋葱"。

### 2.2 一次 `Driver::park`,层层推进

为什么要这样包?看 `Driver::park` 的实现就懂了:

```rust
// tokio/src/runtime/driver.rs(摘录)
impl Driver {
    pub(crate) fn park(&mut self, handle: &Handle) {
        self.inner.park(handle);
    }
    pub(crate) fn park_timeout(&mut self, handle: &Handle, duration: Duration) {
        self.inner.park_timeout(handle, duration);
    }
    pub(crate) fn shutdown(&mut self, handle: &Handle) {
        self.inner.shutdown(handle);
    }
}
```

([tokio/src/runtime/driver.rs:66-76](../tokio/tokio/src/runtime/driver.rs#L66-L76))

`Driver::park` 一行:`self.inner.park(handle)`——委托给 `TimeDriver::park`。再看 `TimeDriver::park`:

```rust
// tokio/src/runtime/driver.rs(摘录)
impl TimeDriver {
    pub(crate) fn park(&mut self, handle: &Handle) {
        match self {
            TimeDriver::Enabled { driver, .. } => driver.park(handle),
            TimeDriver::EnabledAlt(v) => v.park(handle),
            TimeDriver::Disabled(v) => v.park(),
        }
    }
    // park_timeout / shutdown 同构(省略)
}
```

([tokio/src/runtime/driver.rs:327-334](../tokio/tokio/src/runtime/driver.rs#L327-L334))

`TimeDriver::park` 又委托给 `crate::runtime::time::Driver::park`(那个 `driver` 字段)。**time driver 的 `park` 内部,会先算"下一个最近要到期的定时器还有多久",把这个时长当作 epoll 的超时,然后调内层 `IoStack::park`**:

```rust
// tokio/src/runtime/driver.rs(摘录)
impl IoStack {
    pub(crate) fn park(&mut self, handle: &Handle) {
        match self {
            IoStack::Enabled(v) => v.park(handle),   // → ProcessDriver → SignalDriver → IoDriver
            IoStack::Disabled(v) => v.park(),        // 退化为纯线程 park(没有 I/O)
        }
    }
    // ...
}
```

([tokio/src/runtime/driver.rs:169-175](../tokio/tokio/src/runtime/driver.rs#L169-L175))

`IoStack::park` 一路委托下去,最终落到 `IoDriver::park`——这里才真正调 `epoll_wait`(在 Linux,通过 mio crate,第 3 篇详拆)。

**这一层层委托,形成一条单链**:

```
   worker 调 driver.park()
        ↓
   Driver::park       (最外层壳)
        ↓
   TimeDriver::park   (算"下一个定时器多久到期" → 当作 epoll 超时)
        ↓
   IoStack::park      (process / signal)
        ↓
   IoDriver::park     (调 epoll_wait,真正睡在 epoll 上)
        ↓
   epoll_wait 返回(I/O 事件来了,或定时器超时了)
        ↓
   回到 TimeDriver:推进时间轮,把到期的定时器叫醒
        ↓
   回到 worker:队列里多了被唤醒的 task
```

> **钉死这件事**:reactor(I/O)和 timer **不分家**,是因为它们要在**同一次线程睡眠**里一起推进——timer 算出"下一个定时器还有 N 毫秒",把它当作 `epoll_wait` 的超时;线程睡在 epoll 上,要么 I/O 事件来了被唤醒,要么 N 毫秒超时被唤醒(定时器到点)。**一次 park,两件事一起办**。如果它们分家,worker 就得"先 park 一下 reactor,再单独推进 timer"——两次系统调用、两次睡眠,而且容易把定时器搞不准(epoll_wait 醒来的时机和定时器无关)。洋葱结构是"用一个超时把两个事件源对齐"的精妙设计。

### 2.3 反面对比:如果 reactor 和 timer 分家

> **不这样会怎样(反面)**:假设 reactor 和 timer 是两个独立的、各自有 `park` 方法的对象:

> ```rust
> // 简化示意,非源码原文:反面,reactor 和 timer 分家
> struct BadRuntime {
>     io: IoDriver,
>     timer: TimerDriver,
> }
> impl BadRuntime {
>     fn park(&mut self) {
>         // 错误的做法:各自 park
>         self.io.park(Some(Duration::MAX));   // 没超时,睡死
>         self.timer.advance();                 // 永远轮不到!
>     }
> }
> ```
>
> 这样写,worker 一旦 `io.park` 就睡死了——除非有 I/O 事件,否则 timer 永远推进不了。即使把顺序反过来(先 timer 再 io),也救不了:**只要其中一个调系统调用阻塞了线程,另一个就没机会跑**。唯一的出路是**把它们绑在同一个系统调用上**——让 timer 的"下次到期时间"成为 io 的"epoll 超时",一次 `epoll_wait` 同时等两类事件。这就是洋葱结构的全部理由。
>
> 这也是为什么 tokio 不允许"只开 reactor 不开 timer"或反之——它们在源码里根本**没法独立 park**,必须拼成一个洋葱。`enable_io` / `enable_time` 这些 builder 选项控制的是"哪一层是 Enabled / Disabled",但洋葱的外形不变(`Disabled` 变体内部退化成纯线程 park,不走 epoll,但 `park` 方法签名还在)。

---

## 三、worker 线程的主循环:四件事在一个 `while` 里交替

reactor/timer 拼好的洋葱(`Driver`)就绪了,接下来看 worker 线程怎么用它在主循环里跑。这是本章的核心。

### 3.1 multi-thread worker 的主循环

直接看 `Context::run`——这是 multi-thread worker 的主循环,**第 2 篇全书最关键的一个函数**:

```rust
// tokio/src/runtime/scheduler/multi_thread/worker.rs(摘录)
impl Context {
    fn run(&self, mut core: Box<Core>) -> RunResult {
        self.reset_lifo_enabled(&mut core);
        core.stats.start_processing_scheduled_tasks();

        while !core.is_shutdown {
            self.assert_lifo_enabled_is_correct(&core);
            if core.is_traced { core = self.worker.handle.trace_core(core); }

            // Increment the tick
            core.tick();

            // Run maintenance, if needed
            core = self.maintenance(core);

            // First, check work available to the current worker.
            if let Some(task) = core.next_task(&self.worker) {
                core = self.run_task(task, core)?;
                continue;
            }

            // We consumed all work in the queues and will start searching for work.
            core.stats.end_processing_scheduled_tasks();

            // There is no more **local** work to process, try to steal work
            // from other workers.
            if let Some(task) = core.steal_work(&self.worker) {
                core.stats.start_processing_scheduled_tasks();
                core = self.run_task(task, core)?;
            } else {
                // Wait for work
                core = if !self.defer.is_empty() {
                    self.park_yield(core)
                } else {
                    self.park(core)
                };
                core.stats.start_processing_scheduled_tasks();
            }
        }
        // ... shutdown 处理
        self.worker.handle.shutdown_core(core);
        Err(())
    }
}
```

([tokio/src/runtime/scheduler/multi_thread/worker.rs:560-628](../tokio/tokio/src/runtime/scheduler/multi_thread/worker.rs#L560-L628))

这个 `while !core.is_shutdown` 循环,每一轮做四件事,我把它提炼成一个"循环里的四拍":

```
   ┌─────────────────────────────────────────────────────────────┐
   │ worker 主循环的一拍(每一轮 while):                            │
   │                                                             │
   │  ① tick()                ← 拍一下节拍器(递增 tick 计数)        │
   │  ② maintenance(core)     ← 每 61 拍调一次,内部 park_yield     │
   │                            (零超时推进 driver:reactor + timer)  │
   │  ③ 取 task → run_task    ← next_task / steal_work → poll       │
   │  ④ 没活就 park(core)     ← condvar.wait 睡下去,或 epoll_wait 睡 │
   │                                                             │
   │  被唤醒后,回到 ①                                            │
   └─────────────────────────────────────────────────────────────┘
```

逐拍拆:

**① `core.tick()`** —— 拍一下节拍器。`tick` 是个递增的计数,用来决定"什么时候该推进 driver"(下一拍会用)。

**② `self.maintenance(core)`** —— **关键一拍**。看它的实现:

```rust
// tokio/src/runtime/scheduler/multi_thread/worker.rs(摘录)
fn maintenance(&self, mut core: Box<Core>) -> Box<Core> {
    if core.tick % self.worker.handle.shared.config.event_interval == 0 {
        super::counters::inc_num_maintenance();
        core.stats.end_processing_scheduled_tasks();
        // Call `park` with a 0 timeout. This enables the I/O driver, timer, ...
        // to run without actually putting the thread to sleep.
        core = self.park_yield(core);
        // Run regularly scheduled maintenance
        core.maintenance(&self.worker);
        core.stats.start_processing_scheduled_tasks();
    }
    core
}
```

([tokio/src/runtime/scheduler/multi_thread/worker.rs:780-797](../tokio/tokio/src/runtime/scheduler/multi_thread/worker.rs#L780-L797))

注释把话说得明明白白:**"Call `park` with a 0 timeout. This enables the I/O driver, timer, ... to run without actually putting the thread to sleep."** —— 每隔 `event_interval`(默认 **61**)拍,worker 就 `park_yield` 一次,而 `park_yield` 是"零超时的 park"——**不睡,只调一次 `driver.park_timeout(Duration::ZERO)`,把 reactor 和 timer 推进一步**。

这就是"调度 task / 推进 reactor / 推进 timer"在同一个循环里交替的**核心机制**:worker 不用专门停下来等 I/O,而是**每 poll 61 个 task,顺手推进一次 driver**。61 这个数字来自 `event_interval` 配置(可在 builder 里改),源码注释解释过这个取值的权衡(下一节展开)。

**③ `core.next_task(...) → self.run_task(...)`** —— 取一个 task 来 poll。`next_task` 内部按"本地队列 / 全局队列"的策略挑(第 7 章详拆),`run_task` 内部包了 `coop::budget`(第 9 章详拆)调 `task.run()`(真正 poll Future)。

**④ 没活就 `self.park(core)`** —— 本地队列空了、也偷不到别人的活了,worker 就 park 睡下去。这次是**真正睡**(不是零超时),线程要么睡在 condvar 上,要么睡在 epoll_wait 上(取决于抢没抢到 driver 锁,技巧精解详拆)。

> **比喻回到餐厅(循环的四拍)**:服务员(worker)在一个循环里穿梭——
> - **① 拍一下计时器**(tick):"我服务了多久了"
> - **② 维护(每服务 61 单一次)**:扭头看一眼厨房叫号屏和催单闹钟(maintenance → park_yield)——"有没有菜好了 / 有没有催单到点的?",有就把对应订单重新排进自己的派单队列
> - **③ 取一张订单去服务**(next_task → poll):把订单做到下一个 await 点
> - **④ 没订单了,靠着吧台打盹**(park):厨房或闹钟会喊他
> - 醒来接着 ①
>
> 这个循环的关键,是**② 和 ④ 都会推进 driver,但方式不同**:② 是"不睡、零超时、轮询式推进"(每 61 单一次),④ 是"真睡、靠 epoll_wait/condvar 阻塞式等待事件"(只有没活时)。两者搭配,让 worker 既有活干时不耽误 I/O(②定期推进),又没活干时不浪费 CPU(④真睡)。

### 3.2 current-thread worker 的主循环:同一个骨架,简化版

current-thread 模式没有 work-stealing、没有多 worker,但**主循环的骨架一模一样**。看 `CoreGuard::block_on`:

```rust
// tokio/src/runtime/scheduler/current_thread/mod.rs(摘录)
impl CoreGuard<'_> {
    #[track_caller]
    fn block_on<F: Future>(self, future: F) -> F::Output {
        let ret = self.enter(|mut core, context| {
            let waker = Handle::waker_ref(&context.handle);
            let mut cx = std::task::Context::from_waker(&waker);
            pin!(future);
            core.metrics.start_processing_scheduled_tasks();

            'outer: loop {
                let handle = &context.handle;

                // 1) 先 poll 传进来的 block_on future
                if handle.reset_woken() {
                    let (c, res) = context.enter(core, || {
                        crate::task::coop::budget(|| future.as_mut().poll(&mut cx))
                    });
                    core = c;
                    if let Ready(v) = res { return (core, Some(v)); }
                }

                // 2) 循环 poll event_interval 个调度出来的 task
                for _ in 0..handle.shared.config.event_interval {
                    if core.unhandled_panic { return (core, None); }
                    core.tick();
                    let entry = core.next_task(handle);
                    let task = match entry {
                        Some(entry) => entry,
                        None => {
                            core.metrics.end_processing_scheduled_tasks();
                            core = if context.has_pending_work(&core) {
                                context.park_yield(core, handle)    // 零超时推进 driver
                            } else {
                                context.park(core, handle)          // 真正 park 睡
                            };
                            core.metrics.start_processing_scheduled_tasks();
                            continue 'outer;
                        }
                    };
                    let task = context.handle.shared.owned.assert_owner(task);
                    let (c, ()) = context.run_task(core, || { task.run(); /* 省略 hooks */ });
                    core = c;
                }

                // 3) 跑满 event_interval 后,yield 给 driver 推进 timer / I/O
                core.metrics.end_processing_scheduled_tasks();
                // Yield to the driver, this drives the timer and pulls any
                // pending I/O events.
                core = context.park_yield(core, handle);
                core.metrics.start_processing_scheduled_tasks();
            }
        });
        // ...
    }
}
```

([tokio/src/runtime/scheduler/current_thread/mod.rs:767-856](../tokio/tokio/src/runtime/scheduler/current_thread/mod.rs#L767-L856))

读这段,**抓和 multi-thread 的同与异**:

- **同**:**同一个 `event_interval = 61` 的节奏**。current-thread 也是"poll 一批 task → park_yield 推进 driver → 接着 poll"。注释 `// Yield to the driver, this drives the timer and pulls any pending I/O events.`(L842)一字不差地说出和 multi-thread 同样的逻辑。
- **异一**:current-thread **没有 work-stealing**(只有自己一个 worker,没的偷)。`steal_work` 那一拍直接没有,没活就 park。
- **异二**:current-thread 把 **block_on 的 future 直接放在主循环里 poll**(L783 `future.as_mut().poll`),而 multi-thread 的 `block_on` future 走另一条路(`CachedParkThread`,见下节),worker 循环只 poll 被 spawn 的 task。
- **异三**:current-thread 的 `Driver` **直接存在 `Core` 里**(独占,不用抢锁,因为只有一个 worker);multi-thread 的 `Driver` 在共享的 `Arc<Shared>` 里(要 `TryLock`)。这是单线程/多线程的根本差别,第 8 章详拆。

> **钉死这件事**:两种模式的 worker 主循环**骨架完全一致**:tick → 推进 driver(定期 / 没活时)→ poll task → 没活 park。差别只在"有没有 work-stealing""driver 是独占还是共享""block_on future 放哪"。这个骨架,就是 tokio 运行时的"心跳"。后面第 7、8、9 章,都是在这个骨架的某一拍上做文章:第 7 章拆 ③ 取 task 的策略(work-stealing)、第 8 章对比两模式在 ③④ 的差异(LIFO slot / 独占 driver)、第 9 章拆 `run_task` 里的 budget。

### 3.3 `block_on` 在 multi-thread 上的另一条路:`CachedParkThread`

到这里你可能有个疑问:multi-thread 模式下,`Runtime::block_on(some_future)` 里的 `some_future`,跑在哪?**它不在 worker 主循环里**(worker 循环只 poll 被 spawn 的 task)。

答案:**它跑在调 `block_on` 的那个线程(通常是主线程)上,用一套独立的、简化版的循环**。看 `MultiThread::block_on`:

```rust
// tokio/src/runtime/scheduler/multi_thread/mod.rs(摘录)
pub(crate) fn block_on<F>(&self, handle: &scheduler::Handle, future: F) -> F::Output
where F: Future,
{
    crate::runtime::context::enter_runtime(handle, true, |blocking| {
        blocking.block_on(future).expect("failed to park thread")
    })
}
```

([tokio/src/runtime/scheduler/multi_thread/mod.rs:83-94](../tokio/tokio/src/runtime/scheduler/multi_thread/mod.rs#L83-L94))

`blocking.block_on(future)` 走到 `CachedParkThread::block_on`:

```rust
// tokio/src/runtime/park.rs(摘录)
pub(crate) fn block_on<F: Future>(&mut self, f: F) -> Result<F::Output, AccessError> {
    use std::task::Context;
    use std::task::Poll::Ready;
    let waker = self.waker()?;
    let mut cx = Context::from_waker(&waker);
    pin!(f);
    loop {
        if let Ready(v) = crate::task::coop::budget(|| f.as_mut().poll(&mut cx)) {
            return Ok(v);
        }
        self.park();                 // poll 完就 park(交由 condvar 睡)
    }
}
```

([tokio/src/runtime/park.rs:274-290](../tokio/tokio/src/runtime/park.rs#L274-L290))

这是**最简的 poll 循环**:poll 一下 future,没好就 park 睡,被叫醒再 poll。它没有"取队列里的 task"那一拍——因为它就 poll **那一个** future。它的 park 用的是 `ParkThread`(下一节详拆,纯 condvar,不带 driver)。

为什么用这么简化的循环?因为 `block_on` 这个 future 通常就是用户主函数里那段 `async { ... }`,里面会 `tokio::spawn` 别的 task——**那些 task 才进 worker 循环**,而 `block_on` 自己的 future 只在主线程被反复 poll,等它 Ready 就退出。它**不参与 worker 之间的工作分配**,所以不需要完整的 worker 循环。

> **钉死这件事**:multi-thread runtime 里其实有**两类循环**——
> - **worker 线程的循环**(`Context::run`):N 个 worker 线程在跑,主循环 poll 被 spawn 的 task,完整版(含 work-stealing、driver 推进)。
> - **主线程(block_on 调用者)的循环**(`CachedParkThread::block_on`):简化版,只反复 poll 那一个 future + park,不碰 worker 队列。
>
> 这种"主线程跑 block_on future + N 个 worker 跑 spawn 出来的 task"的分工,是 multi-thread runtime 的标准用法。current-thread runtime 则没有这种分工——它就一个线程,block_on future 和 spawn 的 task 都在同一个主循环里。

### 3.4 `event_interval = 61` 这个数字怎么来的

回到主循环的 ② 拍:为什么是 61?

`event_interval` 是 worker **连续 poll 多少个 task 才推进一次 driver** 的阈值,默认 61,可在 builder 里改。源码里 `runtime/mod.rs` 的文档直接讲了这个数:

```rust
//! The runtime will check for new IO or timer events whenever there are no
//! tasks ready to be scheduled, or when it has scheduled 61 tasks in a row. The
//! number 61 may be changed using the [`event_interval`] setting.
```

([tokio/src/runtime/mod.rs:335-337](../tokio/tokio/src/runtime/mod.rs#L335-L337))(current-thread 版本同句在 [L369-371](../tokio/tokio/src/runtime/mod.rs#L369-L371))

**61 这个数字是个权衡**:

- **太小**(比如 1):worker 每 poll 一个 task 就推进一次 driver,driver 推进太频繁——`epoll_wait`(或 `kevent`、`io_uring_enter`)是系统调用,每次几十到几百纳秒,频繁调直接吃吞吐。
- **太大**(比如 10000):worker 长时间不推进 driver,I/O 事件和定时器的**延迟**变大——一个 socket 数据到了,worker 要等几千次 poll 后才轮到推进 driver,响应延迟飙升。
- **61 是个折衷**:既不至于太频繁调系统调用,又让 I/O / timer 的延迟保持在"几十微秒到一两毫秒"的可接受范围。这是个**经验值**,不是理论上最优——`loom` 测试和实际 workload 调出来的。

> **钉死这件事**:61 不是魔法数字,是"driver 推进频率 vs 系统调用开销"的权衡。这个数暴露了 tokio 一个根本张力——**worker 既要拼命 poll task(吞吐),又要定期抬眼看 I/O / timer(延迟)**。两种需求抢同一个 worker 线程的时间,event_interval 就是它们的分界线。如果你的 workload 是"超大吞吐、能容忍几百毫秒 I/O 延迟",可以把 event_interval 调大(比如 1000);如果是"低延迟响应",调小(比如 10)。默认 61 是"大多数场景都不太差"的中间值。

---

## 四、park / unpark:worker 怎么睡、怎么醒

主循环的 ④ 拍——`self.park(core)`——是 worker "没活时怎么省 CPU" 的核心。这一节拆它的实现,也是本章技巧精解的主角。

### 4.1 不是 `std::thread::park`,是 `Condvar` + 三态原子

第一个意外:tokio 的 park **不是** Rust 标准库的 `std::thread::park` / `Thread::unpark`,而是**自己手写**了一套,基于 `Mutex<()>` + `Condvar` + 一个三态 `AtomicUsize`。看最简的实现 `ParkThread`(用于 block_on 主线程):

```rust
// tokio/src/runtime/park.rs(摘录)
#[derive(Debug)]
pub(crate) struct ParkThread {
    inner: Arc<Inner>,
}

#[derive(Debug)]
struct Inner {
    state: AtomicUsize,
    mutex: Mutex<()>,
    condvar: Condvar,
}

const EMPTY: usize = 0;     // 没人 park,也没人 notify
const PARKED: usize = 1;    // 有人 park 睡着了
const NOTIFIED: usize = 2;  // 有人 notify 过(还没被消费)
```

([tokio/src/runtime/park.rs:9-29](../tokio/tokio/src/runtime/park.rs#L9-L29))

三个常量:`EMPTY`(初始)、`PARKED`(睡着)、`NOTIFIED`(被叫过但还没睡的人消费)。这三态原子是**避免丢唤醒**的关键——下面详拆。

`Inner::park`(真正睡下去的地方):

```rust
// tokio/src/runtime/park.rs(摘录)
fn park(&self) {
    // fast path: 已被 notified 则直接消费通知返回(不睡)
    if self
        .state
        .compare_exchange(NOTIFIED, EMPTY, SeqCst, SeqCst)
        .is_ok()
    {
        return;
    }
    let mut m = self.mutex.lock();
    match self.state.compare_exchange(EMPTY, PARKED, SeqCst, SeqCst) {
        Ok(_) => {}
        Err(NOTIFIED) => {
            let old = self.state.swap(EMPTY, SeqCst);
            assert_eq!(old, NOTIFIED);
            return;
        }
        Err(actual) => panic!("state was not empty: {}", actual),
    }
    loop {
        m = self.condvar.wait(m).unwrap();          // ← 线程在此真正睡眠
        if self
            .state
            .compare_exchange(NOTIFIED, EMPTY, SeqCst, SeqCst)
            .is_ok()
        {
            return;
        }
        // spurious wakeup(虚假唤醒),回去接着睡
    }
}
```

([tokio/src/runtime/park.rs:79-124](../tokio/tokio/src/runtime/park.rs#L79-L124))

`Inner::unpark`(叫醒的地方):

```rust
// tokio/src/runtime/park.rs(摘录)
fn unpark(&self) {
    match self.state.swap(NOTIFIED, SeqCst) {
        EMPTY => return,    // 没人在等,只标记 notified,下次 park 直接返回
        NOTIFIED => return, // 已经 notify 过了,不重复
        PARKED => {}        // 有人睡着,要去叫醒
        _ => panic!("state inconsistent"),
    }
    drop(self.mutex.lock());        // 等被 park 方进 condvar.wait(内存屏障)
    self.condvar.notify_one();      // ← 唤醒
}
```

([tokio/src/runtime/park.rs:177-204](../tokio/tokio/src/runtime/park.rs#L177-L204))

### 4.2 为什么不用 `std::thread::park`

你可能会问:`std::thread::park` / `Thread::unpark` 不就是干这个的吗?为什么 tokio 要自己手写?

> **不这样会怎样(反面)**:用 `std::thread::park`,撞两个坑——
>
> **坑一:`std::thread::park` 的 Token 只有一个**。Rust 标准库的 `thread::park` 用"每个线程一个 token"的模型:`unpark` 会把 token 设为"已通知",`park` 消费它。**一个线程只有一个 token**——多次 `unpark` 在 `park` 之前,只算一次通知(合并)。这在"一个 worker 只有一个唤醒源"时没问题,但 tokio 的 worker **同时被多个源唤醒**:reactor、timer、别的 worker(派单)、injector 队列。如果用单 token `thread::park`,这些唤醒会**合并丢失**——比如 reactor 和 timer 同时叫,worker 只看见一次,另一个事件得等下一次 park 才能处理,延迟翻倍。
>
> **坑二:`std::thread::park` 不能"在 epoll_wait 里睡"**。worker 真正想睡的时候,往往不是单纯睡 condvar,而是**睡在 epoll_wait 上**(顺便等 I/O 事件)。`thread::park` 只能调 OS 的 `futex`/`WaitForSingleObject`,睡在内核的一个地址上,跟 epoll fd 是两个东西。tokio 需要"要么睡 epoll、要么睡 condvar"的灵活性,标准库给不了。
>
> 自己手写,就能把"状态机(三态原子)"、"睡眠方式(condvar 或 epoll)"解耦——这正是下一节 multi-thread `Parker` 的精妙处。

### 4.3 multi-thread 的 `Parker`:两种睡法,靠抢 driver 锁切换

multi-thread worker 用的不是 `ParkThread`,是更复杂的 `Parker`:

```rust
// tokio/src/runtime/scheduler/multi_thread/park.rs(摘录)
pub(crate) struct Parker {
    inner: Arc<Inner>,
}
struct Inner {
    state: AtomicUsize,
    mutex: Mutex<()>,
    condvar: Condvar,
    /// Resource (I/O, time, ...) driver
    shared: Arc<Shared>,
}
/// Shared across multiple Parker handles
struct Shared {
    /// Shared driver. Only one thread at a time can use this
    driver: TryLock<Driver>,
}
```

([tokio/src/runtime/scheduler/multi_thread/park.rs:16-54](../tokio/tokio/src/runtime/scheduler/multi_thread/park.rs#L16-L54))

两个关键差别:

- **多了一个 `shared: Arc<Shared>`,里面是 `driver: TryLock<Driver>`**——driver 本体在这!所有 worker 共享这一份 driver,靠 `TryLock` 决定谁去 park 它。
- **状态原子多了一态**:不再是 EMPTY/PARKED/NOTIFIED 三态,而是 **EMPTY / PARKED_CONDVAR / PARKED_DRIVER / NOTIFIED 四态**(见 [L45-48](../tokio/tokio/src/runtime/scheduler/multi_thread/park.rs#L45-L48))。多出来的两态区分"睡在 condvar 上"还是"睡在 driver turn 上"。

看 `Inner::park`(关键:抢 driver 锁):

```rust
// tokio/src/runtime/scheduler/multi_thread/park.rs(摘录)
fn park(&self, handle: &driver::Handle) -> HadDriver {
    // fast path: 已 notified 则消费
    if self
        .state
        .compare_exchange(NOTIFIED, EMPTY, SeqCst, SeqCst)
        .is_ok()
    {
        return HadDriver::No;
    }
    if let Some(mut driver) = self.shared.driver.try_lock() {
        self.park_driver(&mut driver, handle, None)    // 抢到 driver:用 driver turn 睡
    } else {
        self.park_condvar(None);                        // 没抢到:condvar 睡
        HadDriver::No
    }
}
```

([tokio/src/runtime/scheduler/multi_thread/park.rs:132-149](../tokio/tokio/src/runtime/scheduler/multi_thread/park.rs#L132-L149))

读这段,**抓关键决策**:`try_lock` driver——

- **抢到了**(`Some(driver)`):worker 调 `park_driver`——它会**真去 `epoll_wait`**(经 driver 洋葱),睡在 epoll 上,顺便推进 reactor/timer。
- **没抢到**:worker 调 `park_condvar`——退回纯 condvar 睡,不碰 driver(因为别的 worker 正在用)。

为什么这么设计?因为 driver 本体是**共享的**(一个 epoll fd),**同一时刻只能有一个 worker 调 epoll_wait**(否则两个线程同时往同一个 epoll fd 提交 / 等待,事件归属混乱)。所以用 `TryLock`——**worker 想 park 时尝试抢 driver,抢到了就由它负责"睡在 epoll 上推进 I/O",抢不到的就纯睡 condvar 等被叫**。

`park_driver` 的核心(driver turn 睡):

```rust
// tokio/src/runtime/scheduler/multi_thread/park.rs(摘录)
fn park_driver(
    &self,
    driver: &mut Driver,
    handle: &driver::Handle,
    duration: Option<Duration>,
) -> HadDriver {
    if duration.as_ref().is_some_and(Duration::is_zero) {
        // zero duration doesn't actually park the thread, it just
        // polls the I/O events, timers, etc.
        driver.park_timeout(handle, Duration::ZERO);    // ← 零超时 = park_yield!
        return HadDriver::Yes;
    }
    // ... 设置 PARKED_DRIVER 状态
    if let Some(duration) = duration {
        driver.park_timeout(handle, duration);
    } else {
        driver.park(handle);                            // ← 真正 driver turn 睡(epoll_wait)
    }
    // ... 清状态
    HadDriver::Yes
}
```

([tokio/src/runtime/scheduler/multi_thread/park.rs:228-275](../tokio/tokio/src/runtime/scheduler/multi_thread/park.rs#L228-L275))

注意那个零超时分支——**`duration.is_zero` 时,不睡,只调一次 `driver.park_timeout(Duration::ZERO)` 推进 I/O**。这正是上一节讲的 `park_yield`!worker 主循环 ② 拍里的 `park_yield`,底层就是 `Parker::park_timeout(Duration::ZERO)`,它走的就是这条 `park_driver` 的零超时分支。

> **钉死这件事**:multi-thread 的 worker 有**两种睡法**:
> - **抢到 driver 锁**:睡在 `epoll_wait` 里(`PARKED_DRIVER` 状态),顺便推进 reactor/timer。这是"高效睡眠"——一次睡眠既等 I/O 事件又省 CPU。
> - **没抢到 driver 锁**:睡在 `condvar.wait` 里(`PARKED_CONDVAR` 状态),纯等被叫。这是"低效但必要"——总得有人睡 epoll,但其他 worker 也不能傻等。
>
> 加上 `park_yield`(零超时,不睡只推进),worker 的 park 有**三种行为**:零超时推进(不睡)、driver turn 睡(睡 epoll)、condvar 睡(睡 condvar)。这三种行为由"要不要睡 + 抢没抢到 driver 锁"组合决定。这套灵活性是 tokio 多线程运行时能做到"既共享 driver 又不抢锁僵死"的关键。

### 4.4 unpark:叫醒睡在不同地方的 worker

睡法有两种,叫醒也得对路。看 `Inner::unpark`:

```rust
// tokio/src/runtime/scheduler/multi_thread/park.rs(摘录)
fn unpark(&self, driver: &driver::Handle) {
    match self.state.swap(NOTIFIED, SeqCst) {
        EMPTY => {}
        NOTIFIED => {}
        PARKED_CONDVAR => self.unpark_condvar(),   // condvar 睡的:notify_one
        PARKED_DRIVER => driver.unpark(),           // driver 睡的:driver.unpark(叫醒 epoll)
        actual => panic!("inconsistent park state: {}", actual),
    }
}
```

([tokio/src/runtime/scheduler/multi_thread/park.rs:277-290](../tokio/tokio/src/runtime/scheduler/multi_thread/park.rs#L277-L290))

**根据 worker 当前的睡法,选择对应的叫醒方式**——`PARKED_CONDVAR` 就 `condvar.notify_one`,`PARKED_DRIVER` 就 `driver.unpark`(它会通过 eventfd / pipe 给 epoll 发个事件,让 epoll_wait 立刻返回)。这套"看状态选叫醒方式"的逻辑,是 multi-thread worker 能被任意源(reactor / 别的 worker / injector)叫醒的基础。

---

## 技巧精解:三态原子 + Condvar 防丢唤醒 + driver 锁切换睡眠

这一节把本章最硬核的两个技巧拆透:**① 三态原子状态机怎么防止丢唤醒**(ParkThread 那套);**② multi-thread `Parker` 怎么用"抢 driver 锁"在两种睡眠方式间切换**。

### 技巧一:三态原子(EMPTY/PARKED/NOTIFIED)防丢唤醒

#### 这套设计在解决什么问题

park/unpark 最怕的就是**丢唤醒**:

- 线程 A 准备 park(检查条件 → 没事 → 准备睡)。
- **就在 A 检查完、还没真睡的瞬间**,线程 B 调 unpark(以为 A 已经睡了)。
- A 真睡下去,B 的 unpark 没人接收——A 永远睡死。

经典的解决方法是"先标记、再检查",但朴素地用两个变量(`bool should_park` + `bool notified`)会撞**撕裂中间态**——A 读到 `should_park=true` 时,`notified` 还没被 B 写,A 错过。

#### 反面对比:朴素的两变量

```rust
// 简化示意,非源码原文:反面,两个独立变量
struct BadPark {
    should_park: AtomicBool,    // A 想睡
    notified: AtomicBool,       // B 叫过
}
impl BadPark {
    fn park(&self) {
        self.should_park.store(true, SeqCst);
        // ← 此时 B 调 unpark,notified = true,但 A 还没进 wait
        if !self.notified.load(SeqCst) {
            self.condvar.wait(...);   // ← 丢唤醒!B 的 notified 没人消费,A 永睡
        }
    }
    fn unpark(&self) {
        self.notified.store(true, SeqCst);
        self.condvar.notify_one();
    }
}
```

> **不这样会怎样**:A 设 `should_park=true` 后、检查 `notified` 前,B 把 `notified` 设 true 并 `notify_one`——可 condvar 上还没人 wait,这次 notify 直接丢了(Condvar 的 notify 不计数)。A 随后检查 `notified`,看见 true,跳过 wait——**看似躲过了**。可如果 B 的 `notified.store` 发生在 A 的 `should_park.store` 和 `notified.load` **之间**的某个交错下,A 可能在 `notified=false` 时进 wait,然后 B 已经 notify 过了——A 永睡。两个独立原子变量给不了"检查 + 决定睡"的原子性。

#### 正解:三态原子 + CAS

tokio 把"该不该睡、有没有人叫过、有没有人正睡着"三件事,塞进**一个** `AtomicUsize` 的三个值:

```
   state: AtomicUsize  (三态)
   ┌──────────────────────────────────────────────────────────┐
   │ EMPTY(0)    : 初始 / 已被消费                            │
   │ PARKED(1)   : 有人正睡在 condvar 上                     │
   │ NOTIFIED(2) : 有人叫过(还没被睡的人消费)                 │
   └──────────────────────────────────────────────────────────┘
```

park 流程用 CAS 推进状态:

```
   park():
     ① CAS(NOTIFIED → EMPTY): 成功 → 直接返回(消费掉之前的通知,不睡)
                              失败 → 进 ②
     ② lock mutex, CAS(EMPTY → PARKED): 成功 → 进 ③ condvar.wait
                                        失败 →
                                          若旧值是 NOTIFIED,swap 清空返回
                                          (B 在 ①②之间叫过,赶上了)
                                          其他 → panic
     ③ condvar.wait(循环,处理 spurious wakeup)
        醒来后 CAS(NOTIFIED → EMPTY): 成功 → 返回
                                      失败 → 虚假唤醒,接着睡

   unpark():
     ① swap(NOTIFIED): 旧值 EMPTY → 直接返回(没人在等,通知被存下来,下次 park 直接返回)
                       旧值 NOTIFIED → 直接返回(已通知过,不重复)
                       旧值 PARKED → 进 ②
     ② lock mutex(内存屏障,确保 park 方已进 condvar.wait)
        condvar.notify_one()
```

**关键在于 `NOTIFIED` 这个状态**——它代表"通知已被发出但还未被消费"。这个状态让 unpark 在"没人在等"时**不丢通知**:unpark 把状态从 EMPTY 改 NOTIFIED,即便没人 park,下次有人 park 时第一步 CAS(NOTIFIED → EMPTY)就成功,直接返回不睡——**通知被保留下来,等下一个 park 消费**。

> **钉死这件事**:三态原子的精髓是**把"通知"做成一个持久状态(NOTIFIED),而不是一次性事件(notify_one)**。这样 unpark 永远不丢——不管接收方此刻在不在 wait,通知都会留到下一次 park 被消费。这是 park/unpark 防丢唤醒的标准范式,Rust 标准库的 `std::thread::park`、Java 的 `LockSupport.park`、Go 的 `gopark` 都是同构设计——一个状态字 + 三态 + CAS。tokio 手写一份,是为了下面要讲的"多睡眠方式"灵活性。

#### 为什么所有原子操作都用 `SeqCst`

注意上面所有 CAS / swap 都用 `SeqCst`(顺序一致)内存序,而不是更轻的 `Acquire/Release`。这是 park/unpark 的特殊要求:

> **不这样会怎样(用 Acq/Rel)**:park/unpark 涉及"状态字 + condvar + 业务数据"三处的同步。比如 worker park 时,它之前的"本地队列已空"判断,必须严格在 `state → PARKED` 之前对其他线程可见;unpark 后,worker 醒来重新读"本地队列",必须严格在 `state → EMPTY` 之后。`SeqCst` 提供"全局总序"——所有线程看到的操作顺序一致,这种"状态字 + 旁路业务数据"的强协调才不会乱。Acq/Rel 在这种"多变量协调"场景下不够强,可能出现"A 看见 state=NOTIFIED 但看不见业务数据更新"的撕裂。park 这种"睡了能不能保证醒来状态对"的关键路径,tokio 一律上 `SeqCst`,牺牲一点性能换绝对正确。

### 技巧二:multi-thread `Parker` 的"抢 driver 锁 + 双睡眠方式"

#### 这套设计在解决什么

multi-thread 有 N 个 worker 共享**一个** driver(一个 epoll fd)。worker 没 task 时想 park,面临的根本矛盾是:

- **想睡在 epoll 上**(高效:一次睡既省 CPU 又顺便等 I/O)。
- **但 epoll fd 同一时刻只能一个线程 `epoll_wait`**(否则事件归属混乱)。
- **N 个 worker 里,只有一个能睡 epoll,其他 N-1 个怎么办?**

#### 反面对比 A:共享 driver + 全局锁

```rust
// 简化示意,非源码原文:反面,共享 driver + Mutex
struct BadShared {
    driver: Mutex<Driver>,    // 强锁
}
impl BadShared {
    fn park(&self) {
        let mut driver = self.driver.lock().unwrap();   // ← N 个 worker 抢这一把锁
        driver.park();                                   // 睡 epoll
    }
}
```

> **不这样会怎样**:`Mutex` 是**阻塞锁**——worker park 时如果抢不到 driver 锁(因为别的 worker 正睡在 epoll 上),它会**自己阻塞在 `Mutex::lock` 上**,占着 OS 线程干等。这违反 async 运行时"绝不让 worker 线程阻塞"的铁律——而且睡在 epoll 上的 worker 可能睡很久(等 I/O 事件),其他 worker 全堵在锁上,整个运行时停摆。**N 核机器,N-1 个核废了**。

#### 反面对比 B:每个 worker 独占一个 driver

> **不这样会怎样**:给每个 worker 配一个独立的 driver(独立 epoll fd)。看似解了锁,撞新墙——**每个 driver 都得注册所有 I/O 资源**,可 task spawn 时不知道自己会被哪个 worker poll,资源注册到哪个 driver?而且 I/O 事件就绪后,要"叫醒等待它的 task",这个 task 可能正被另一个 worker 持有——driver 之间还得通信。**N 个 driver = N 倍注册开销 + N 倍跨 driver 唤醒复杂度**。tokio 早期版本试过这种,后来合并成单 driver。

#### 正解:`TryLock<Driver>` + 两种睡眠

tokio 的做法:driver 本体**唯一**且**共享**,但**谁去 park 它,用 `TryLock`(非阻塞锁)抢**:

- worker park 时 `try_lock` driver:
  - **抢到了** → 调 `driver.park()` 睡 epoll(`PARKED_DRIVER` 状态)。
  - **没抢到**(`TryLock` 立刻返回失败,不阻塞)→ 调 `condvar.wait` 纯睡(`PARKED_CONDVAR` 状态)。

关键差别:`TryLock` **不会阻塞抢锁的线程**——抢不到立刻返回失败,worker 转而走 condvar 路径,**绝不占着 OS 线程等锁**。这样:

- **同一时刻只有一个 worker 睡在 epoll 上**(driver 锁保证),事件归属清晰。
- **其他 worker 走 condvar 睡**,纯等被叫,不碰 driver。
- **抢到 driver 的 worker 被 I/O 事件 / 定时器叫醒后**,释放 driver 锁,其他 worker 下次 park 才有机会抢。

状态机里多出来的 `PARKED_DRIVER` 状态,就是为了让 unpark 知道"该 worker 睡在 driver 上,得用 `driver.unpark()`(给 epoll 发 eventfd)叫醒它",而不是 `condvar.notify_one()`。

> **钉死这件事**:`TryLock` 是这套设计的灵魂——它把"共享 driver"和"不阻塞 worker"两个看似矛盾的需求解开:**TryLock 抢不到不阻塞,而是退到 condvar**。这是无锁/非阻塞设计的经典套路:**把"抢不到就等"换成"抢不到就走 plan B"**。tokio 的 work-stealing 队列(第 7 章)、task 状态机(第 5 章)都是同一个思路——CAS 失败不阻塞、而是重试或换路。`loom` 测试对这套多 worker 抢 driver 的交错做了穷举验证。

#### sound 性小结

这套 Parker 的 unsafe 都封在 `Condvar::wait` / `notify_one`(标准库内部)和 driver 的 `epoll_ctl`(mio 内部),Parker 自身的代码全是 safe Rust。状态机的正确性靠"一个原子字 + CAS 自旋"保证,不需要额外 unsafe。这是 tokio "把 unsafe 关进底层 crate(mio/std),上层全 safe"风格的体现。

---

## 章末小结

### 用"餐厅服务员"比喻回顾本章

1. **`Runtime` 就是一家餐厅** —— 三个字段:`scheduler`(经理,调度 task)、`handle`(里面装着 reactor / timer 的句柄,以及 driver 本体的访问权)、`blocking_pool`(同步阻塞任务的隔离病房)。**reactor 和 timer 不是独立模块,而是层层包成一个 `Driver` 洋葱**,因为它们要在同一次线程睡眠里一起推进——timer 算"下一个定时器还有多久",当作 epoll 的超时。
2. **worker 线程是服务员,在一个循环里穿梭** —— 主循环四拍:**tick → maintenance(每 61 单推进一次 driver)→ poll task → 没活就 park**。这个循环就是餐厅每天的开门营业:服务员不断接单、偶尔看一眼厨房叫号屏、没单了靠着吧台打盹。
3. **park / unpark 是"打盹 / 被叫醒"** —— 不是 `std::thread::park`,而是自己手写的 `Condvar` + 三态原子(EMPTY/PARKED/NOTIFIED),精髓是把"通知"做成持久状态(NOTIFIED),不丢不重。
4. **multi-thread worker 有两种睡法** —— 抢到 driver 锁就睡 epoll(`PARKED_DRIVER`,高效:一次睡既等 I/O 又省 CPU);没抢到就睡 condvar(`PARKED_CONDVAR`,纯等)。`TryLock` 是灵魂——抢不到不阻塞,而是退回 condvar。
5. **`block_on` 在 multi-thread 上走另一条路** —— 主线程用 `CachedParkThread` 反复 poll 那一个 future + park,不碰 worker 队列;N 个 worker 线程跑完整主循环,只 poll 被 spawn 的 task。current-thread 模式则两者合一。

### 本章在全书主线中的位置

记住全书的二分法:**调度执行(让就绪的任务跑) vs 事件唤醒(让等待的任务不空耗、就绪了再叫)**。

本章是**衔接**那一面——更具体说,是**把调度执行和事件唤醒缝在一个循环里**。前 5 章我们立起了"被调度的对象(task)"和"唤醒的载体(Waker)",但它们一直是**两个分离的概念**:

- task 在调度器队列里被排队、被 poll(调度执行);
- Waker 在事件源(reactor / timer)那边被登记、被按一下(事件唤醒)。

**这一章把它们缝起来了**:worker 主循环的 ② 拍(推进 driver)和 ④ 拍(park)里,reactor 和 timer 把就绪的 task 通过 Waker 塞回调度队列;③ 拍 poll task 时,task 在 await 点挂起、把 Waker 留给事件源。**调度和唤醒,在同一个 `while` 循环里来回穿梭**,这就是运行时心脏跳动的样子。

本章把后面三章要钻深的对象摆上了桌:

- 第 7 章钻进主循环的 ③ 拍——`next_task` / `steal_work`,拆 work-stealing 调度器和那个无锁环形队列;
- 第 8 章对比 ③ ④ 两拍在两模式下的差异——LIFO slot、injector 队列、独占 vs 共享 driver;
- 第 9 章钻进 `run_task` 里——budget 怎么逼 task 让出;
- 第 3 篇钻进 driver 洋葱的最里层——`IoDriver` 的 mio/epoll、注册表 slab + token 映射;
- 第 4 篇钻进 `TimeDriver`——层级时间轮。

### 五个"为什么"清单

1. **一个 `Runtime` 在源码里长什么样?**:三个字段——`scheduler`(调度器,CurrentThread/MultiThread 两变体)、`handle`(调度器句柄 + driver 句柄,reactor/timer 句柄从这掏)、`blocking_pool`(同步阻塞任务的隔离区)。reactor/timer 本体不在 `Runtime` 里,而在 worker 共享的 `Arc<Shared>` 里(multi-thread)或 worker 自己的 `Core` 里(current-thread)。
2. **为什么 reactor 和 timer 不分家?**:它们要在**同一次线程睡眠**里一起推进——timer 算"下一个定时器还有 N 毫秒",把它当作 `epoll_wait` 的超时;一次 `driver.park()` 同时等 I/O 事件和定时器超时。源码里它们层层包成一个洋葱(`Driver` → `TimeDriver` → `IoStack` → `IoDriver`),`Driver::park` 一行委托,层层往下调到 `epoll_wait`。
3. **worker 主循环长什么样?**:一个 `while !shutdown` 循环,每轮四拍:① `tick` 计数;② `maintenance`(每 `event_interval=61` 拍调 `park_yield` 零超时推进 driver);③ `next_task → run_task`(poll task);④ 没活 `park`(真睡)。current-thread 和 multi-thread 骨架一致,差别在有没有 work-stealing、driver 是独占还是共享。
4. **为什么 worker 没活要 park,不能忙等?**:忙等 = 占着 CPU 空转,违背 async 运行时"等待不占线程"的初衷。park 让 worker 真睡下去(`condvar.wait` 或 `epoll_wait`),省 CPU 给别的 worker / 别的进程;被叫醒(unpark)立刻接着干活。
5. **park / unpark 为什么不用 `std::thread::park`?**:两个坑——① `std::thread::park` 的 token 每线程只有一个,多唤醒源会合并丢失;② 它只能睡内核 futex,不能"睡在 epoll_wait 里"。tokio 自己手写 `Condvar` + 三态原子(NOTIFIED 持久状态防丢唤醒),multi-thread 再加 `TryLock<Driver>` 实现"抢到 driver 睡 epoll、抢不到睡 condvar"的双睡眠方式。

### 想继续深入,该往哪钻

- **本章引用的核心源码(按重要度排)**:
  - [`tokio/src/runtime/scheduler/multi_thread/worker.rs`](../tokio/tokio/src/runtime/scheduler/multi_thread/worker.rs) —— **本章灵魂**。`Context::run` 主循环(L560-628)、`maintenance`(L780-797)、`park`/`park_yield`/`park_internal`(L799-905,driver turn 在 L873)、`run_task`(L630-704,`task.run()` 在 L684)。这一份文件,把 worker 主循环写到了极致。
  - [`tokio/src/runtime/scheduler/multi_thread/park.rs`](../tokio/tokio/src/runtime/scheduler/multi_thread/park.rs) —— **技巧精解主角**。`Parker`/`Inner`/`Shared`(L16-54)、四态常量(L45-48)、`Inner::park` 抢 driver 锁(L132-149)、`park_driver`(L228-275)、`unpark`(L277-290)。
  - [`tokio/src/runtime/park.rs`](../tokio/tokio/src/runtime/park.rs) —— `ParkThread`/`Inner` 三态原子(L9-29)、`Inner::park`(L79-124,`condvar.wait` 在 L111)、`unpark`(L177-204)、`CachedParkThread::block_on`(L274-290,multi-thread block_on 实际走的路径)。
  - [`tokio/src/runtime/driver.rs`](../tokio/tokio/src/runtime/driver.rs) —— `Driver` 洋葱(L15-77,组装)、`Handle`(L20-35,driver 句柄)、`Driver::park` 委托链(L66-76 → L169-175 → L327-334)。
  - [`tokio/src/runtime/runtime.rs`](../tokio/tokio/src/runtime/runtime.rs) —— `Runtime` 三字段(L97-106)、`Scheduler` enum(L120-129)、`block_on` 分发(L340-379)。
  - [`tokio/src/runtime/scheduler/multi_thread/mod.rs`](../tokio/tokio/src/runtime/scheduler/multi_thread/mod.rs) —— `MultiThread::block_on`(L83-94,委托 `CachedParkThread`)。
  - [`tokio/src/runtime/scheduler/current_thread/mod.rs`](../tokio/tokio/src/runtime/scheduler/current_thread/mod.rs) —— `CoreGuard::block_on` 主循环(L767-856)、park/park_yield/park_internal(L380-439,driver turn 在 L436)。current-thread 的对照版本。
  - [`tokio/src/runtime/mod.rs`](../tokio/tokio/src/runtime/mod.rs) —— module 文档(L1-14 三件套、L335-337 / L369-371 `event_interval = 61` 的解释)。
- **用 `tokio-console` 观察 worker 状态**:`tokio-console` 能实时看到每个 worker 的状态(执行中 / 空闲 / park)、task 数量、steal 次数。装 `console-subscriber`,运行时连 `tokio-console`,看着 worker 在"poll / park"之间切换,对本章理解极有帮助。
- **亲手感受 `event_interval` 的影响**:用 builder 把 `event_interval` 调成 1 和 100000,跑一个"I/O 密集 + CPU 密集混合"的 workload,对比延迟和吞吐。你会直观看到"小 event_interval = 低 I/O 延迟但低吞吐;大 event_interval = 高吞吐但高 I/O 延迟"。
- **下一站**:运行时的骨架立起来了——worker 在一个循环里 poll task、推进 driver、park。可 worker **怎么决定下一个 poll 哪个 task**?本地队列、全局队列(injector)、偷工作(work-stealing)是怎么回事?那个无锁环形队列为什么不是教科书 Chase-Lev?翻开 **第 7 章 · work-stealing 调度器:偷工作的艺术**——我们钻进主循环的 ③ 拍,拆调度器内部。

---

> 三件套摆上了桌:reactor 和 timer 包成一个洋葱,worker 在一个循环里交替 poll task / 推进 driver / park 睡眠,park 用三态原子防丢唤醒,multi-thread 用 TryLock 在两种睡眠间切换。可这个循环的 ③ 拍——"取一个 task 来 poll"——里的"取"大有文章:本地队列怎么排、全局队列干嘛用、忙的 worker 怎么把活偷给闲的?翻开 **第 7 章 · work-stealing 调度器:偷工作的艺术**。
