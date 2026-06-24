# 第 17 章 · Notify 与 Semaphore

> **核心问题**:第 15 章的 Mutex、第 16 章的 channel 都是"等**别人**干完事"——等锁、等消息。可任务之间还有两类更轻的协作:一类是**纯粹的"通知"**——A 不发数据给 B,只是"拍一下"B 说"该你了",这种通知怎么做到**"先 notify 后 wait 也不丢"**(通知了 B 还没来等,这笔账怎么留住)?另一类是**"限流"**——同时只允许 N 个 task 进临界区(或调外部 API),这 N 个"许可牌"怎么发、怎么收?
>
> 这是**第 5 篇·并发原语:sync 模块**的收束章。`Notify` 解决"通知不丢",`Semaphore` 解决"许可限流"。两者都是前面章节的延伸:`Semaphore` 的底层就是第 15 章讲透的 `batch_semaphore`(本章只补它的公共 API 和限流用法);`Notify` 则是个全新的、精巧的"无锁计数 + 状态字"设计。读完这一章,第 5 篇三种 sync 协作(互斥 / 传消息 / 通知)就齐全了。
>
> **读完本章你会明白**:
> - `Notify` 怎么做到"先 notify 后 wait 也不丢"——核心是个**状态字 + 无锁快路径**:notify 时如果没人等,把 state 置成 `NOTIFIED`(把这笔通知**记下来**);wait 的人来了一看 `NOTIFIED`,直接消费它、立刻通过,**根本不挂起**。这就是经典的"通知不丢"无锁设计。
> - 为什么 `Notify` 内部还要一个**计数器**(记录 `notify_waiters` 被调了多少次),这个计数器解决了什么微妙的竞态("广播期间新加入的等待者怎么处理")。
> - `Semaphore` 作为**限流器**怎么用(许可数 = N,每个 task 进临界区 acquire 1、出来 release 1),以及它的 `acquire().await` 内部和第 15 章 Mutex 的 `lock().await` 是同一套机制(许可 = 1 的信号量 = Mutex)。
> - 第 5 篇三种 sync 原语(Mutex / channel / Notify)在"事件唤醒"这条主线下是怎么统一的:它们都是**事件源 + Waker 等待者队列**。
> - 一个工程上的关键区别:**`Notify` vs 一个容量 0 的 channel / oneshot**——什么场景用哪个。
>
> **如果一读觉得太难**:先只记住三件事——① **Notify 的核心是"通知不丢"**:notify 时如果没人等,把通知记进状态字(`NOTIFIED`);wait 的人来了看见 `NOTIFIED` 直接消费,不挂起;② **Semaphore 是限流器**:许可数 = N,N 个 task 能同时拿、第 N+1 个挂起,典型用法是"限制对外部 API 的并发请求数";③ **Semaphore 的底层是第 15 章的 `batch_semaphore`**,本章只补它的公共 API 和限流场景。状态字位段 / 计数器的精妙细节看不懂可以先跳,抓住"通知不丢"这一个心智模型。

---

## 章首·一句话点破

> **`Notify` 是个"通知不丢"的事件源**:你 `notify_one()`,它把通知"记一笔"(状态字置 `NOTIFIED`,或 wake 队首 Waker);你来 `notified().await`,它先看状态字有没有"未消费的通知"——有就吃掉、立刻通过,没有就留 Waker 挂起。**先后顺序无所谓,通知永远不丢**。`Semaphore` 是个"许可牌箱":许可数 = N,N 个 task 同时能拿,第 N+1 个挂着等归还。它的底层就是第 15 章那把信号量——Mutex 是它的"许可数 = 1 特例",本章看它的限流用法。两者都落在"事件唤醒"那条主线上:Notify 是"纯事件",Semaphore 是"带配额的事件"。

这是**结论**。本章倒过来拆:先从"朴素通知会丢"的反面讲起,看清为什么需要一个状态字;再把 `Notify` 的状态字(EMPTY/WAITING/NOTIFIED 三态 + `notify_waiters` 计数器)逐位拆透,看清"不丢"的物理基础;然后看 `Semaphore` 的限流用法和它跟 Mutex 的关系;最后技巧精解把"无锁计数不丢事件"这个总纲钦定的主角技巧拆透,配反面对比。第 5 篇在这里收束。

第 16 章结尾留了钩子:"任务之间最轻量的协作是纯粹的'通知'——不传数据,只拍一下肩膀"。这一章回答。

---

## 一、Notify:为什么"朴素通知"会丢

要理解 `Notify` 的设计,得先看清"不这么干会怎样"。这一节是本章的地基——`Notify` 的整个状态字设计,就是**为了躲开下面两条朴素方案的坑**。

### 反面一:用一个 bool flag 当通知

最朴素的"通知":搞个 `AtomicBool`,notify 把它置 true,wait 的人看到 true 就过、然后把它清回 false。

```rust
// 简化示意,非源码原文:朴素 bool flag
struct NaiveNotify {
    flag: AtomicBool,
}

impl NaiveNotify {
    fn notify_one(&self) {
        self.flag.store(true, Release);
    }

    async fn wait(&self) {
        // 自旋等 flag 变 true
        while !self.flag.swap(false, AcqRel) {
            // 怎么挂起???
            std::thread::yield_now();   // 阻塞 / 自旋,糟
        }
    }
}
```

> **不这样会怎样**:三个死穴——
>
> **死穴一:多次 notify 会合并成一次**。`notify_one()` 调了 3 次,flag 都只是 true,第 4 次调用的人以为"通知了 4 次",可实际只有一个 true 等着。3 次通知丢了。如果你想"通知 3 次,叫醒 3 个等待者",这套机制做不到。
>
> **死穴二:wait 还没注册就 notify,丢了**。设想线程 A 准备 wait(还没自旋到 swap 那一步),线程 B `notify_one()`(flag = true),线程 A 此时第一次 swap——拿到 true,过。看似正常,但反过来:A 还没开始 wait,B 已经 notify_one 了(flag = true),过了一会儿 A 才进 wait,swap 拿到 true——这一笔也过。**看似都对,可问题是**:如果 B 在 A 进 wait 之前,做了 `notify_one` 然后 `flag = false`(被别人消费了),A 进 wait 时 swap 拿到 false,**A 永远自旋等不到**——B 那次 notify 完全丢给 A 了。
>
> **死穴三:wait 怎么挂起**?bool flag 没法挂起——它没有 Waker。要么自旋(CPU 烧光),要么阻塞线程(回到 thread-per-connection)。**它根本接不进 async**。

### 反面二:用一个容量 0 的 channel

有人想:那我用 channel 啊,`tokio::sync::mpsc::channel(0)` 不就行?容量 0,send 必须等 recv——这就是个"通知"。

```rust
// 简化示意,非源码原文:用 channel 当 Notify
let (tx, mut rx) = tokio::sync::mpsc::channel::<()>(0);

// notify
tx.send(()).await;   // 等 receiver 来收

// wait
rx.recv().await;
```

> **不这样会怎样**:容量 0 的 channel 在 tokio 里其实**会 panic**(`mpsc::channel(buffer)` 要求 `buffer > 0`,见 [bounded.rs:160](../tokio/tokio/src/sync/mpsc/bounded.rs#L160))。即使退一步用 `channel(1)` 模拟,也有问题——**它把"通知"和"传值"绑死了**:每次 notify 都得 send 一个值(哪怕是 `()`),capacity 满了 notify 还会挂起(背压)。而真正的"通知"语义应该是:**notify 不该被 receiver 拖慢——notify 是单方面的"拍肩膀",拍完就走,不管你来不来等**。
>
> 更要命的是,channel 的"通知"是**传递式**的——一条消息对应一次唤醒。而 Notify 的语义是**事件式**的——状态机维护"有没有未被消费的通知",**多次 notify 可以累积(或不累积,取决于策略),先后顺序无所谓**。这两种语义根本不同。

### 反面三:Notify 的真问题——"已到的事件不丢"

把两个反面放一起,夹出 `Notify` 的核心需求:

> **我们需要一种"通知不丢"的机制——notify 可以发生在 wait 之前、之后、之中任意时刻,事件都不能丢。具体说:notify 时如果没人等,这笔通知要"记下来";wait 的人来了一看有"记下来的通知",直接消费、立刻通过,不挂起。**

这就是 `Notify` 的设计目标。它的核心技巧——**无锁计数(或状态位)记录"未消费的通知"**——是这类"事件计数"无锁设计的范式,后面会拆透。

> **比喻回到餐厅**:Notify 是餐厅的**呼叫铃**——吧台有个呼叫铃,服务员按一下(notify_one),厨房那边灯亮一下;再按一下,灯还是亮(不累积多次"按"),但**灯亮这个状态被记下了**。另一个服务员(`wait` 的人)走到吧台,看见灯亮——他知道"有人按过铃",把灯按下(消费通知),进去干活,**不用等下一次按**。如果灯本来是灭的,他就在便签上留个名字(Waker)挂着,等下次有人按铃叫他。**关键不丢**:哪怕按铃和走到吧台的顺序乱七八糟,只要"按过"这件事被记下,走过来的人一定能看见。

---

## 二、Notify 的状态字:EMPTY / WAITING / NOTIFIED 三态

现在看 `Notify` 的真身。它的全部"通知"语义,落在一个 `AtomicUsize` 状态字上:

```rust
// tokio/src/sync/notify.rs(摘录)
pub struct Notify {
    // `state` uses 2 bits to store one of `EMPTY`, `WAITING` or `NOTIFIED`.
    // The rest of the bits are used to store the number of times
    // `notify_waiters` was called.
    //
    // Throughout the code there are two assumptions:
    // - state can be transitioned *from* `WAITING` only if `waiters` lock is held
    // - number of times `notify_waiters` was called can be modified only if
    //   `waiters` lock is held
    state: AtomicUsize,
    waiters: Mutex<WaitList>,
}
```

([tokio/src/sync/notify.rs:202-215](../tokio/tokio/src/sync/notify.rs#L202-L215))

两个字段:**state(状态字)+ waiters(等待者链表)**。状态字塞了两件事:

```rust
// tokio/src/sync/notify.rs(摘录)
const NOTIFY_WAITERS_SHIFT: usize = 2;
const STATE_MASK: usize = (1 << NOTIFY_WAITERS_SHIFT) - 1;          // bit 0~1:state
const NOTIFY_WAITERS_CALLS_MASK: usize = !STATE_MASK;               // bit 2+:计数

const EMPTY: usize = 0;       // 初始空闲态
const WAITING: usize = 1;     // 有 task 在等(且没有未消费的通知)
const NOTIFIED: usize = 2;    // 有未消费的通知(且此刻没人在等)
```

([tokio/src/sync/notify.rs:440-451](../tokio/tokio/src/sync/notify.rs#L440-L451))

**低位 2 bit 是三态状态机**(EMPTY/WAITING/NOTIFIED),**高位是 `notify_waiters` 被调用的次数计数器**。我们一步步拆。

### 三态状态机的语义

- **`EMPTY`(0)** —— 空闲:没人在等,也没有未消费的通知。初始状态。
- **`WAITING`(1)** —— 有 task 在等:**有人调了 `notified().await`、把 Waker 留在 waiters 链表里挂起了**,此刻没有未消费的通知(否则它就消费了直接走人)。
- **`NOTIFIED`(2)** —— 有未消费的通知:**有人调了 `notify_one()`,但当时没人在等**,这笔通知"记下来"——置成 NOTIFIED。后来有人 `notified().await` 进来,看见 NOTIFIED 直接消费、过。

这套三态的核心,在 `NOTIFIED` 这个态——**它就是"通知记下来"的化身**。notify 时如果没人等(WAITING 没置),就置 NOTIFIED;wait 时如果看见 NOTIFIED,直接消费。

### notify_one 的快路径:无锁,只 CAS 状态字

看 `notify_one` 的实现(简化展示):

```rust
// tokio/src/sync/notify.rs(摘录)
fn notify_with_strategy(&self, strategy: NotifyOneStrategy) {
    let mut curr = self.state.load(SeqCst);

    // If the state is `EMPTY`, transition to `NOTIFIED` and return.
    while let EMPTY | NOTIFIED = get_state(curr) {
        // The compare-exchange from `NOTIFIED` -> `NOTIFIED` is intended. A
        // happens-before synchronization must happen between this atomic
        // operation and a task calling `notified().await`.
        let new = set_state(curr, NOTIFIED);
        let res = self.state.compare_exchange(curr, new, SeqCst, SeqCst);

        match res {
            Ok(_) => return,                  // ← 快路径:无锁完成!
            Err(actual) => curr = actual,
        }
    }

    // 走到这里说明 state 是 WAITING(有人等),才进锁唤醒
    let mut waiters = self.waiters.lock();
    // ... 唤醒队首 Waker
}
```

([tokio/src/sync/notify.rs:677-709](../tokio/tokio/src/sync/notify.rs#L677-L709))

`notify_one` 的**快路径完全是原子操作**——**只 CAS 状态字**,**根本不拿 `waiters` 锁**!具体逻辑:

- 当前是 `EMPTY` → CAS 置成 `NOTIFIED`(把这笔通知记下来),返回。
- 当前已经是 `NOTIFIED` → CAS 改成 `NOTIFIED`(其实没变,但 SeqCst 的 CAS 建立了 happens-before),返回。**这就是"多次 notify 不累积,但不丢"的体现**:多次 notify 在没人等的情况下,只留一笔 `NOTIFIED`,但绝不丢。
- 当前是 `WAITING`(有人等)→ **CAS 失败**(while 循环退出),进慢路径,拿 `waiters` 锁,唤醒队首 Waker。

**注意那个 "NOTIFIED → NOTIFIED 的 CAS 也是有意的"**——注释明说它建立 happens-before。意思是:即使状态没变,CAS 这个原子操作本身保证了"notify 之前的写"对"wait 之后读到这个状态的线程"可见。这是 Notify 无锁设计的内存模型根基。

### notified 的快路径:看见 NOTIFIED 直接消费

`notified()` 返回一个 `Notified` Future,它的 poll:

```rust
// tokio/src/sync/notify.rs(摘录,简化)
fn poll_notified(self, waker: Option<&Waker>) -> Poll<()> {
    'outer_loop: loop {
        match *state {
            State::Init => {
                let curr = notify.state.load(SeqCst);

                // Optimistically try acquiring a pending notification
                let res = notify.state.compare_exchange(
                    set_state(curr, NOTIFIED),
                    set_state(curr, EMPTY),         // ← CAS NOTIFIED → EMPTY:消费!
                    SeqCst,
                    SeqCst,
                );

                if res.is_ok() {
                    // 拿到通知了
                    *state = State::Done;
                    continue;
                }

                // CAS 失败(不是 NOTIFIED),进锁注册 Waker、挂起
                let mut waiters = notify.waiters.lock();
                // ... 把 Waker 入队,state 置 WAITING,Pending
            }
            // ...
        }
    }
}
```

([tokio/src/sync/notify.rs:1108-1195](../tokio/tokio/src/sync/notify.rs#L1108-L1195),简化展示)

`poll_notified` 的快路径同样是**无锁 CAS**:乐观地尝试 `CAS NOTIFIED → EMPTY`(消费那笔通知)。成功 → 立刻 `Ready`,**根本不挂起**。失败(不是 NOTIFIED,即没人 notify 过)→ 才进锁、注册 Waker、置 WAITING、返回 Pending。

**这就是"先 notify 后 wait 也不丢"的物理实现**:

- notify 在前、wait 在后:notify 把 state 置成 NOTIFIED;wait 来了 CAS 消费、立刻通过。
- wait 在前、notify 在后:wait 进来 state 是 EMPTY,CAS 失败,进锁、置 WAITING、留 Waker、挂起;notify 来了看见 WAITING,进锁、wake 队首 Waker;wait 被 wake,重新 poll,这次……(注意此时 state 可能已被 notify 改变,需要小心,见下文)。

> **钉死这件事(Notify 的核心机制)**:**notify 和 notified 各有一个"无锁快路径"——notify 在 EMPTY/NOTIFIED 时只 CAS 置位、不进锁;notified 在 NOTIFIED 时只 CAS 消费、不进锁**。两边的快路径都靠"状态字的 `NOTIFIED` 态表达未消费的通知"。**先后顺序无所谓:notify 早了记下 NOTIFIED,wait 来了消费;wait 早了留 Waker 挂起,notify 来了 wake**。这就是经典的"通知不丢"无锁设计。

### 高位计数器:解决"广播期间的微妙竞态"

状态字的高位还藏了个计数器——`notify_waiters` 被调用的次数。这个计数器解决一个非常微妙的竞态。看 `notify_waiters`(广播,叫醒所有等待者):

```rust
// tokio/src/sync/notify.rs(摘录)
fn inner_notify_waiters<'a>(
    &'a self,
    curr: usize,
    mut waiters: crate::loom::sync::MutexGuard<'a, LinkedList<Waiter, Waiter>>,
) {
    if matches!(get_state(curr), EMPTY | NOTIFIED) {
        // 没人等,只增加计数器
        atomic_inc_num_notify_waiters_calls(&self.state);
        return;
    }

    // Increment the number of times this method was called
    // and transition to empty.
    let new_state = set_state(inc_num_notify_waiters_calls(curr), EMPTY);
    self.state.store(new_state, SeqCst);

    // ... 唤醒所有 waiters
}
```

([tokio/src/sync/notify.rs:747-762](../tokio/tokio/src/sync/notify.rs#L747-L762))

**注意:`notify_waiters` 不会置 NOTIFIED**。这是它和 `notify_one` 的关键区别——`notify_one` 留一笔"未消费通知"给后来者;`notify_waiters` **只唤醒当前已经在等的 task,不留通知给未来的人**。文档明说:

> Unlike with `notify_one()`, no permit is stored to be used by the next call to `notified().await`. The purpose of this method is to notify all already registered waiters.
> ([notify.rs:712-717](../tokio/tokio/src/sync/notify.rs#L712-L717))

可这就引出一个问题:**如果一个 task 正在 `notified().await` 的状态机里(已经构造了 Future,但还没 poll 到挂起),这时 `notify_waiters` 来了——这笔广播它能不能收到**?如果不收,它会一直挂起,等下一次 notify——可它"差点就等到了",这太冤。

**这就是计数器的用处**。每个 `Notified` Future 构造时,会**记下当前的 `notify_waiters_calls` 值**(`notify_waiters_calls` 字段,见 `poll_notified` 开头那个判断)。然后 poll 时,先比较"现在的计数器"和"我构造时的计数器":

```rust
// Check if `notify_waiters` was called before attempting to acquire
// the `NOTIFIED` state. If a broadcast occurred, we will be woken by it,
// leaving the `notify_one` permit for other waiters.
if get_num_notify_waiters_calls(curr) != *notify_waiters_calls {
    *state = State::Done;
    continue 'outer_loop;
}
```

([tokio/src/sync/notify.rs:1121-1127](../tokio/tokio/src/sync/notify.rs#L1121-L1127))

**计数器变了 → 期间发生过 `notify_waiters` → 这笔广播属于我,我直接通过**。这个细节极其微妙,但保证了"广播和 wait 注册并发时,wait 的人不丢广播"。

> **钉死这件事(计数器的作用)**:**`notify_waiters_calls` 计数器解决"广播期间新加入 / 状态过渡中的 wait 不丢广播"这个微妙竞态**。它不是"通知数",它是"广播次数"——每次 `notify_waiters` 加一,`Notified` Future 构造时记下当前值,poll 时比较,变了就说明"我错过了的广播发生过",直接通过。**这是 Notify 设计里最容易被忽视、却最关键的细节之一**——没有它,广播会在某些竞态下被悄悄丢失。

---

## 三、Semaphore:许可牌箱,限流器

讲完 Notify,看 Semaphore。**第 15 章已经把底层 `batch_semaphore` 讲透了**——本章只补它的公共 API 和作为"限流器"的用法。

### 公共 Semaphore 是 `batch_semaphore` 的薄包装

```rust
// tokio/src/sync/semaphore.rs(摘录)
pub struct Semaphore {
    ll_sem: ll::Semaphore,         // ← batch_semaphore::Semaphore
}
```

([tokio/src/sync/semaphore.rs:398-410](../tokio/tokio/src/sync/semaphore.rs#L398-L410))

公共 `Semaphore` 就是个壳——它包了一个 `batch_semaphore::Semaphore`(底层实现),对外提供 safe API。所有"许可"语义(抢许可、挂起、唤醒、归还)都是第 15 章那套,这里不重复,只看几个关键 API:

```rust
// tokio/src/sync/semaphore.rs(摘录)
pub const MAX_PERMITS: usize = super::batch_semaphore::Semaphore::MAX_PERMITS;

pub fn new(permits: usize) -> Self {
    // ...
    let ll_sem = ll::Semaphore::new(permits);
    Self { ll_sem, /* ... */ }
}

pub fn available_permits(&self) -> usize {
    self.ll_sem.available_permits()
}

pub fn add_permits(&self, n: usize) {
    self.ll_sem.release(n);
}

pub async fn acquire(&self) -> Result<SemaphorePermit<'_>, AcquireError> {
    // ...
    let inner = self.ll_sem.acquire(1);
    // ...
    inner.await?;
    Ok(SemaphorePermit { sem: self, permits: 1 })
}

pub async fn acquire_many(&self, n: u32) -> Result<SemaphorePermit<'_>, AcquireError> {
    // ...
    self.ll_sem.acquire(n as usize).await?;
    Ok(SemaphorePermit { sem: self, permits: n })
}
```

([tokio/src/sync/semaphore.rs:450-644](../tokio/tokio/src/sync/semaphore.rs#L450-L644),简化展示)

`acquire` 抢 1 个许可,`acquire_many(n)` 抢 n 个许可,都返回 `SemaphorePermit`(RAII guard)。permit 的 Drop 自动归还:

```rust
// tokio/src/sync/semaphore.rs(摘录)
impl Drop for SemaphorePermit<'_> {
    fn drop(&mut self) {
        self.sem.add_permits(self.permits as usize);    // ← 归还
    }
}
```

([tokio/src/sync/semaphore.rs:1196-1200](../tokio/tokio/src/sync/semaphore.rs#L1196-L1200))

`add_permits` → `ll_sem.release(n)` → 第 15 章那套 `add_permits_locked`(投递许可给队首 + 唤醒 Waker)。**和 Mutex 的 `MutexGuard::drop` 完全同构**:RAII 还许可,等的人被叫醒。

### Semaphore 当限流器:典型用法

Semaphore 最常见的用途是**限流**——限制同时能跑多少个 task。一个经典场景:**限制对外部 API 的并发请求数**(避免打爆对方)。

```rust
// 简化示意,非源码原文:用 Semaphore 限流
use tokio::sync::Semaphore;
use std::sync::Arc;

let sem = Arc::new(Semaphore::new(10));   // 最多 10 个并发

let mut handles = Vec::new();
for req in requests {
    let permit = sem.clone().acquire_owned().await.unwrap();
    handles.push(tokio::spawn(async move {
        let _permit = permit;             // 持有 permit,函数结束自动 drop 归还
        call_external_api(req).await
    }));
}

for h in handles {
    let _ = h.await;
}
```

这套机制干的事:**最多 10 个 task 同时调 API,第 11 个 `acquire_owned().await` 挂起等归还**。某个 task 调完 API、函数返回、`_permit` drop → 还 1 个许可 → 唤醒一个挂起的 task → 它继续。**完全自动的限流**,不需要任何额外协调代码。

> **比喻回到餐厅**:经理手上**一摞"接单牌"**(许可),只有 10 个。服务员要接单,先来拿一块牌;拿到的服务员去服务桌子,**干完归还牌子**;牌子发完了(10 个都在用),下一个服务员在吧台排队等(`acquire().await` 挂起),有人还牌子才能拿。**这就保证了"同时在接单的服务员不超过 10 个"**——限流。这就是 Semaphore 的全部语义。

### Semaphore vs Mutex:许可数的差别

回顾第 15 章:**Mutex = Semaphore(1)**。这一章的 `Semaphore` 是 Mutex 的泛化:

| | Mutex | Semaphore |
|---|---|---|
| 许可数 | 永远 1 | 任意 N |
| 抢的方式 | `lock().await` | `acquire().await`(抢 1) / `acquire_many(n).await`(抢 n) |
| 用途 | 互斥(独占访问数据) | 限流(限制并发数)、资源池 |
| guard | `MutexGuard`(独占数据) | `SemaphorePermit`(就是个凭证,不持有数据) |

Mutex 多了一层"guard 持有 `&mut T`"(互斥访问数据);Semaphore 的 permit 只是个"凭证",不持有任何数据——它的全部意义就是"我有这个凭证,所以我有权进临界区(或调 API、或用资源)"。

> **钉死这件事**:**Mutex 是 Semaphore(1) + 数据访问语义**;**Semaphore 是 Mutex 的"许可数 N、不绑数据"泛化**。两者底层是同一套 `batch_semaphore`。理解第 15 章,这一章的 Semaphore 几乎是免费的——它就是"许可数 > 1 的 Mutex,但不绑数据"。

---

## 四、Notify vs channel:什么场景用什么

讲完 Notify,一个常见困惑:**有了 channel,为什么还要 Notify**?什么场景用 Notify,什么场景用 channel?这是个工程实践问题,值得专门讲清。

### Notify 是"无值、单方面、可累积(或不累积)的事件"

| | Notify | oneshot | mpsc |
|---|---|---|---|
| 传值? | **不传值,纯通知** | 传一个值 | 传任意多值 |
| 通知方向 | 单方面(notify 不等 wait) | 发送方要等接收方收到 | 发送方挂起等接收方收(背压) |
| 多次通知 | `notify_one` 留 1 笔 / `notify_waiters` 广播所有人 | 只能一次 | 一一对应 |
| 典型场景 | 唤醒 / 通知事件、关闭信号、条件满足 | 一次性结果回传 | 流水线、消息队列 |

**关键区别**:

- **Notify 的 notify 不等**——你 `notify_one()` 立刻返回,不管有没有人在 wait、不管那人是谁。channel 的 send 是"等"——容量满了 sender 挂起。
- **Notify 不传值**——它就是个信号。channel 必须传值(哪怕 `()`)。
- **Notify 的多次语义灵活**——`notify_one` 留 1 笔(多余的丢,但不会"少");`notify_waiters` 广播给当前所有人(不留未来的)。channel 是严格的"一一对应"。

### 什么时候用 Notify

- **任务通知 / 唤醒**:一个 task 在 `Notify` 上等"该干活了",另一个 task 干完前置活 `notify_one()`。**不传值,只喊一声**。
- **关闭信号 / 取消信号**:`tokio_util::sync::CancellationToken` 底层就是 Notify(带 broadcast 的版本)。一组 task 在 `notified().await` 等关闭信号,管理者 `notify_waiters()` 一次全唤醒。
- **条件满足的通知**:某个条件(数据准备好、状态变更)成立,notify 一下;等待方被叫醒重新检查。
- **去抖动 / 单触发事件**:不管谁、不管多少次 `notify_one`,等待方只关心"有没有未消费的通知"——这种"事件式"语义,channel 不擅长。

### 什么时候用 channel

- **要传值**:那就别用 Notify,用 channel。
- **要背压**:有界 mpsc,慢的接收方拖慢发送方。
- **要严格一一对应**:每条消息必须被收到,用 mpsc。

> **钉死这件事**:**Notify 是"事件式"(状态机:有没有未消费的通知),channel 是"传递式"(一条消息对应一次唤醒)**。需要传值 / 背压 / 一一对应 → channel;只需要"喊一声" / 单方面通知 / 累积或不累积的事件 → Notify。这两套不是替代关系,是**互补**——tokio 同时提供,因为这两种语义在并发编程里都极其常见,各有最佳实现。

---

## 技巧精解:无锁计数,已到的事件不丢

这一节把本章最硬核的技巧拆透,配反面对比——这是总纲钦定的主角技巧。

### 技巧一:状态字 `NOTIFIED` 态——"通知记下来"

#### 解决的问题

notify 可能发生在 wait 之前——这时还没人在等,通知该不该"留"给后来的人?**该留**(否则就丢了)。怎么留?

#### 反面对比 A:不留(纯事件,wake-only)

```rust
// 简化示意,非源码原文:反面,纯 wake
fn notify_one(&self) {
    let waiters = self.waiters.lock();
    if let Some(waker) = waiters.queue.pop() {
        waker.wake();           // 只 wake 当前在等的人
    }
    // 没人在等?通知直接丢!
}
```

> **不这样会怎样**:**通知早于 wait,直接丢**。这是经典的"notify 信号丢失"bug——很多朴素的 Condvar 实现就有这个问题:`notify` 时没人 `wait`,信号消失;后来的人 `wait` 永远等不到,除非再来一次 notify。Java 的 `Object.notifyAll` 在某些场景也是这个德行。tokio 的 Notify 必须避免这个坑。

#### 反面对比 B:用计数器记录所有通知

```rust
// 简化示意,非源码原文:反面,记录所有通知
struct NaiveCounterNotify {
    count: AtomicUsize,    // 每次 notify +1,每次 wait -1
}

fn notify_one(&self) {
    self.count.fetch_add(1, Release);
}

async fn wait(&self) {
    while self.count.fetch_sub(1, AcqRel) == 0 {
        // 还没通知,挂起
        // ... 但 +1 / -1 交错很容易乱
    }
}
```

> **不这样会怎样**:计数器路径有几个坑——**① fetch_sub 减成负数怎么办**?处理起来复杂;**② 多个等待者并发 wait,谁该拿到 count**?要协调;**③ `notify_one` 的语义是"留一笔给一个等待者",不是"留 N 笔给 N 个等待者"**——计数器把语义搞错了(它会累积所有 notify,但 `notify_one` 设计上只留一笔)。

#### 正解:`NOTIFIED` 态——一笔就够

tokio 的做法:**用一个状态位 `NOTIFIED`,表达"有一笔未消费的通知"**。多次 `notify_one` 在没人等时,只置位一次(不累积),但不丢(永远有一笔留给后来者)。

这套设计的精妙:**它把"未消费的通知数"压缩成"有没有未消费的通知"——1 bit 信息**。对于 `notify_one` 的语义(单次通知、单次消费),1 bit 足够——通知要么有、要么没有,不需要数。这是个**信息论级别的极简**:用最少的位,表达所需的全部语义。

**什么时候需要真正的计数**?如果你要"notify 5 次,叫醒 5 个等待者",Notify 不是为这个设计的——这时你应该用 Semaphore(初始许可数 0,notify = add_permits(1),wait = acquire().await)。**这正是 tokio 的设计哲学:不同语义用不同原语,Notify 做"事件式",Semaphore 做"配额式"**。

> **钉死这件事(`NOTIFIED` 的极简)**:**Notify 用 1 bit 的 `NOTIFIED` 表达"有一笔未消费的通知",而不是用计数器。这是为"事件式通知"语义量身定制的极简设计**——`notify_one` 的语义就是"留一笔",1 bit 够了;真要累积计数,用 Semaphore。这种"用最少的位,精确匹配语义"的设计品味,贯穿 tokio 整个 sync 模块。

### 技巧二:notify 和 wait 双侧的"乐观 CAS"快路径

#### 解决的问题

`notify_one` 和 `notified().poll` 都是热路径——高频调用下,每次都进 `waiters.lock()` 不可接受。

#### tokio 的做法:双侧快路径

- **`notify_one` 快路径**:state 是 EMPTY/NOTIFIED 时,**只 CAS 置位、不进锁**。只有 state 是 WAITING(有人等)才进锁唤醒。
- **`poll_notified` 快路径**:state 是 NOTIFIED 时,**只 CAS 消费、不进锁**。只有 state 不是 NOTIFIED 才进锁注册 Waker、挂起。

两边快路径都是**无锁原子操作**——十几纳秒,纯 CAS。锁只在"确实有人等"或"确实要挂起"时才碰。

#### 反面对比:每次都进锁

```rust
// 简化示意,非源码原文:反面,每次进锁
fn notify_one(&self) {
    let waiters = self.waiters.lock();
    if waiters.has_waiters() {
        waiters.queue.pop().wake();
    } else {
        waiters.pending_notification = true;    // 在锁里改状态
    }
}
```

> **不这样会怎样**:百万次 notify,每次进 `waiters.lock()`——这把 std Mutex 在高频下成为瓶颈。tokio 的双快路径设计,让**无人在等时的 notify 完全无锁**(快路径 CAS 走完),性能大幅提升。

#### 一个 sound 性细节:为什么 SeqCst

注意 Notify 的 CAS 全用 `SeqCst`(顺序一致性),而前面 mutex、oneshot 多用 AcqRel。为什么?

> **SeqCst 是 Notify 的"保险丝"**。Notify 的状态字语义复杂——三态 + 计数器,且 notify 和 wait 双侧都做"乐观 CAS"快路径。在这种"双侧乐观 CAS + 多种状态"的复杂场景下,SeqCst 提供最强的保证:**所有线程看到的状态字修改是全序的**。相对 AcqRel,SeqCst 在某些架构上贵一点点(主要是多一个 memory barrier),但 Notify 是 sync 原语里的"末端"——它的调用频率比 Mutex 低(没人会在死循环里 `notify_one`),用 SeqCst 换正确性保险,值。
>
> 注释里那个 "NOTIFIED → NOTIFIED 的 CAS 也是有意的,要建立 happens-before"([notify.rs:682-685](../tokio/tokio/src/sync/notify.rs#L682-L685)),就是在解释 SeqCst 的 happens-before 保证。这是个细节,但体现了 tokio 团队对内存模型的严谨。

> **钉死这件事(双快路径)**:**Notify 在 notify 和 wait 双侧都设计了"乐观 CAS"快路径,无人在等时 / 无未消费通知时,完全无锁**。这是第 15、16 章"双路径"模式在 Notify 上的延续。**所有 tokio sync 原语都用同一套设计哲学**:热路径无锁 CAS,争用 / 慢路径才进锁。这套模式理解了,看任何 sync 原语的源码都不慌。

---

## 第 5 篇收束:sync 原语统一在"事件唤醒"下

讲完 Notify 和 Semaphore,第 5 篇(并发原语)的三种协作就齐全了。这一节把整篇收束。

### 三种 sync 协作,统一在"事件唤醒"下

回看全书的二分法:**调度执行(让就绪的任务跑) vs 事件唤醒(让等待的任务不空耗、就绪了再叫)**。第 5 篇全部服务**事件唤醒**那一面——更具体说,**任务之间的事件源**:

| 章 | 原语 | 等待的事件 | 等的人留什么 Waker | 事件来了谁 wake |
|---|---|---|---|---|
| 第 15 章 | Mutex / RwLock | 锁被释放(许可被还) | `Waiter`(在 `batch_semaphore` 的 waiters 链表) | 释放锁的人(`release` → 弹队首 wake) |
| 第 16 章 | mpsc | 新消息到来 / channel 关 | `rx_waker` | sender `push` 时 |
| 第 16 章 | mpsc | buffer 有空(许可被还) | `Waiter`(在信号量 waiters 链表) | receiver `recv` 后 `add_permit` |
| 第 16 章 | oneshot | 值发送 / 关闭 | `rx_task`(在 state 字里) | sender `send`(`set_complete`) |
| 第 16 章 | broadcast | 新广播 / 满了被覆盖 | `Waiter`(在 tail.waiters 链表) | sender `send` 后 `notify_rx` |
| 第 17 章 | Notify | 收到通知 | `Waiter`(在 waiters 链表) | notify 的人 |
| 第 17 章 | Semaphore | 许可被还 | `Waiter`(在 `batch_semaphore` 链表) | 还许可的人(`add_permits` → 弹队首 wake) |

**它们全部**用同一套机制:**等的人从 `cx.waker()` clone 出 Waker、塞进等待者链表(或状态字)、返回 Pending;事件来了,事件源把 Waker 弹出、调 `wake`**。**这就是第 4 章 Waker 在 sync 原语上的全面落地**。

### 三种原语共享的底层组件

更有意思的是,这七种原语**底层只用了两三个共享组件**:

- **`batch_semaphore::Semaphore`** —— 是 Mutex、RwLock、Semaphore、有界 mpsc 的共同底座(许可机制)。
- **侵入式 `LinkedList<Waiter>`** —— 是 batch_semaphore、Notify、broadcast 的共同等待者队列实现。
- **`AtomicWaker`** —— 是 mpsc 的 receiver waker 实现(单消费者场景的优化)。

**这种"几个底层组件,组合出多种原语"的设计,是 tokio sync 模块的精髓**。理解了第 15-17 章这三章,你看任何 tokio sync 原语,都能在脑子里把它拆成"哪个底层组件 + 哪种事件源 + 哪种等待者队列"。

### 三个统一的设计模式

第 5 篇三章反复出现三个设计模式:

1. **双路径(快路径 + 慢路径)**:热路径无锁 CAS,争用时进锁入队。所有原语都用(batch_semaphore 的 `poll_acquire`、Notify 的 `notify_one` / `poll_notified`、oneshot 的状态字 CAS)。
2. **状态字位段打包**:一个 `AtomicUsize` 装下"状态 + 标志 + 计数器",位运算 + CAS 修改。所有原语都用(batch_semaphore 的 CLOSED 位、oneshot 的 4 bit、Notify 的三态 + 计数器、mpsc Block 的 32 bit ready 位图 + TX_CLOSED)。
3. **事件源 + Waker 等待者队列**:等的人留 Waker 挂起,事件来了显式 wake。所有原语都用。

> **钉死这件事(第 5 篇的统一性)**:**第 5 章讲的 task 状态字打包、第 4 章讲的 Waker,在第 5 篇里全面落地**。tokio sync 模块的所有原语,本质都是"状态字位段 + Waker 等待者队列"的不同组合。理解了这三个模式,你就掌握了 tokio 整个 sync 模块的设计 DNA。后面看到任何新的原语(`Barrier`、`watch`、`OnceCell`),都能迅速看懂——它们都是这几个模式的变奏。

---

## 章末小结

### 用"餐厅服务员"比喻回顾本章

1. **Notify 是吧台的呼叫铃**——按一下(`notify_one`),灯亮("有未消费的通知",`NOTIFIED` 态);服务员走过来看见灯亮,把灯按下去(消费通知)、进吧台干活,**不用等下一次按**。如果灯本来是灭的,他留个名字(Waker)挂着,等下次有人按铃叫他。**先后顺序无所谓,通知永远不丢**。
2. **多次按铃只留一笔**——`notify_one` 的语义是"留一笔",所以连按 5 次,灯也只亮一次(不累积)。**真要累积(按 5 次叫 5 个人),用 Semaphore 当呼叫铃**(初始 0 个许可,按 = add_permits(1),等 = acquire())。**事件式 vs 配额式,两种语义两种原语**。
3. **`notify_waiters` 是"全场广播"**——一次叫醒当前在等的所有服务员。但它**不留一笔给后来的人**(和 `notify_one` 不同),所以广播期间差点错过的人靠"广播次数计数器"补回来——计数器变了,说明"我赶上的广播发生过",直接通过。
4. **Semaphore 是经理手里的"接单牌"**——一摞 N 块牌,服务员拿一块才能接单,干完归还;牌发完了下一个服务员排队等。**N=1 时它就是 Mutex**(只一块牌),N>1 时它是限流器(限制并发数)。
5. **Notify vs channel**——Notify 是"喊一声"(不传值、不等收),channel 是"传消息"(传值、要收)。**需要传值 / 背压 → channel;只需要喊一声 / 通知事件 → Notify**。两者互补,不替代。

### 本章在全书主线中的位置(第 5 篇收束)

记住全书的二分法:**调度执行(让就绪的任务跑) vs 事件唤醒(让等待的任务不空耗、就绪了再叫)**。

本章和整个第 5 篇,服务**事件唤醒**那一面——更具体说,**任务之间的事件源**。第 3 篇的 reactor 是"I/O 事件源"(数据来了),第 4 篇的 timer 是"时间事件源"(到点了),**第 5 篇的 sync 原语是"任务之间的事件源"**——锁释放了、消息来了、通知到了、许可被还了。**三类事件源,同一套机制**:等的人留 Waker、挂起、不占线程;事件来了 wake。

第 4 章讲 Waker 时,我们说"Waker 是连接挂起和被叫醒的桥"。第 5 篇三章,把这座桥落到了七种具体的 sync 原语上——你看到了 Waker 怎么被塞进 `batch_semaphore` 的 Waiter、塞进 mpsc 的 rx_waker、塞进 Notify 的 waiters 链表。**第 4 章的抽象,在第 5 篇里变成了具体的、可触摸的代码**。

第 5 篇收束,你手里有了完整的 sync 协作工具箱:**互斥(Mutex / RwLock)、传消息(mpsc / oneshot / broadcast)、通知(Notify)、限流(Semaphore)**。这套工具,加上前面几篇的 task / runtime / reactor / timer,足以写出任意复杂的 async 协作逻辑。

### 五个"为什么"清单

1. **Notify 怎么做到"先 notify 后 wait 也不丢"?**:notify 时如果没人等(state 是 EMPTY/NOTIFIED),把 state 置成 `NOTIFIED`——**这笔通知"记下来"了**。wait 的人来了 `poll_notified`,乐观地 CAS `NOTIFIED → EMPTY` 消费它,立刻 Ready,**根本不挂起**。先后顺序无所谓,通知永远不丢——`NOTIFIED` 态就是"未消费通知"的化身。
2. **为什么 Notify 用 1 bit 的 `NOTIFIED` 而不是计数器?**:`notify_one` 的语义是"留一笔给一个等待者",**1 bit 足够**(有/无)。计数器会把语义搞错(累积所有 notify,但 `notify_one` 只想留一笔)。**真要计数累积,用 Semaphore(许可数 0 + add_permits)**——这是"事件式"(Notify)vs"配额式"(Semaphore)的分工。
3. **`notify_waiters_calls` 计数器解决什么?**:解决"广播期间新加入的 / 状态过渡中的 wait 不丢广播"这个微妙竞态。每个 `Notified` Future 构造时记下当前计数器值,poll 时比较——变了说明"期间发生过 `notify_waiters`",直接通过。**没有它,广播会在某些并发交错下被悄悄丢失**。
4. **Semaphore 和 Mutex 是什么关系?**:**Mutex = Semaphore(1) + 数据访问语义**。Semaphore 是"许可数 N、不绑数据"的 Mutex 泛化。N=1 互斥、N>1 限流。底层都是 `batch_semaphore::Semaphore`(第 15 章讲透的那套)。`SemaphorePermit::drop` 自动 `add_permits` 归还,触发 `release` 唤醒队首等待者——和 `MutexGuard::drop` 完全同构。
5. **Notify 和 channel 怎么选?**:**Notify 是"事件式"(无值、单方面、不累积 or 广播),channel 是"传递式"(有值、一一对应、可背压)**。需要传值 / 背压 / 一一对应 → channel(mpsc / oneshot / broadcast);只需要"喊一声" / 单方面通知 / 事件累积(或不累积) → Notify。两套互补,各有最佳实现场景。

### 想继续深入,该往哪钻

- **本章核心源码**:
  - [`tokio/src/sync/notify.rs`](../tokio/tokio/src/sync/notify.rs) —— **本章灵魂**。`Notify` 结构(L202-215)、状态字常量(L440-471,EMPTY/WAITING/NOTIFIED + 计数器)、`notify_with_strategy`(L677-709,快路径 CAS + 慢路径 wake)、`poll_notified`(L1108-1195,快路径消费 + 慢路径挂起)、`inner_notify_waiters`(L747-818,广播 + 计数器递增)。
  - [`tokio/src/sync/semaphore.rs`](../tokio/tokio/src/sync/semaphore.rs) —— 公共 `Semaphore`(L398-410,是 `batch_semaphore` 的薄包装)、`new`(L456)、`acquire` / `acquire_many`(L585-644)、`SemaphorePermit::drop`(L1196-1200,自动还许可)。
  - [`tokio/src/sync/batch_semaphore.rs`](../tokio/tokio/src/sync/batch_semaphore.rs) —— **底层实现,第 15 章已详拆**。本章回扣:`Semaphore`(L35)、`poll_acquire`(L397)、`release` / `add_permits_locked`(L231)、`close`(L242)。
- **`loom` 验证**:Notify 的状态字三态 + 计数器在并发下的正确性,有专门的 loom 测试穷举各种 notify / wait / notify_waiters 交错。想深入"Notify 为什么在各种竞态下都不丢",看 loom 测试用例(附录 B)。**特别注意"广播期间加入 wait"的测试用例**,它能让你直观看见计数器的作用。
- **`tokio-util` 的 `CancellationToken`**:[tokio-util crate](https://docs.rs/tokio-util) 里的 `CancellationToken` 是 Notify 的"带子树传播"增强版——cancel 一个父 token,所有子 token 都被 cancel。它的底层就是 Notify(`notify_waiters` 一次唤醒所有等的人)。读完本章,去看 CancellationToken 的源码——你会发现它就是 Notify 的应用。
- **亲手实验"通知不丢"**:写个程序——先 `notify.notify_one()`,再 `notify.notified().await`。你会发现它**立刻返回**(没有挂起)。再反过来——先 `notified().await`(挂起),再在另一个 task `notify_one()`,等待方被叫醒。**两种顺序都不丢**。这一下就理解 Notify 的核心机制。
- **下一站**:第 5 篇在这里收束——你手里有了完整的 sync 协作工具箱。可前面几篇我们一直说"reactor 盯着 socket",却没真正讲过**"socket 怎么变成 async 的"**——`AsyncRead` / `AsyncWrite` 到底是什么,`TcpListener::accept().await` 内部怎么挂起、怎么被 reactor 叫醒?翻开 **第 6 篇 · 网络 I/O 的第 18 章 · AsyncRead / AsyncWrite**——第 5 篇攒下的"事件唤醒"心智模型,在第 6 篇里终于落到了最常见的事件源——网络字节流。

---

> 第 5 篇收束:sync 原语把"任务之间的事件"做透了——锁、消息、通知、许可,本质都是"等的人留 Waker 挂起、事件来了 wake"。可前面几篇一直说"reactor 盯着 socket"——socket 怎么变成 async 的?`read().await` 内部怎么挂起、数据来了怎么被叫醒?翻开 **第 6 篇 · 网络 I/O 的第 18 章 · AsyncRead / AsyncWrite**——把"事件唤醒"落到最常见的事件源:网络字节流。
