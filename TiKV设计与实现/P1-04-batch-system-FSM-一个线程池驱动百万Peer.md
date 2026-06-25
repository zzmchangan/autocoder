# 第 1 篇 · 第 4 章 · batch-system + FSM:一个线程池驱动百万 Peer

> **核心问题**:上一章拆清了朴素 multi-Raft 为什么爆炸,以及解法的雏形——"少量线程 + 批量轮询 + 事件驱动 + FSM 状态隔离"。雏形好说,具体怎么落地?一个 TiKV 进程里几十万个 Peer(每个 Region 一个副本就是一个 Peer),怎么用**几个** OS 线程把它们全驱动起来,还能保证:① 空闲 Peer 不占 CPU、② 热点 Peer 不饿死别的 Peer、③ 线程之间无锁、④ 状态不被并发踩坏?这就是本章——**batch-system + FSM**,TiKV 最硬核的工程技巧之一,也是"百万个 Raft 怎么共存"的最终答案。

> **读完本章你会明白**:
> 1. 为什么把 Peer 抽象成 **FSM(有限状态机)**——让它的私有状态只能被"持有它的那个线程"碰,从根上消灭数据竞争,而不是靠加锁。
> 2. **`FsmState` 的三态(IDLE / NOTIFIED / DROP)+ 一个 CAS**——怎么用一个原子变量实现"FSM 的所有权在线程间安全流转",且保证**同一个 FSM 同一时刻只被一个线程处理**。这是无锁 actor 模型的精妙之处。
> 3. **mailbox + Router** 怎么把"给某个 Peer 发消息"变成 O(1) 路由——任何线程都能给任何 Peer 投递消息,但只有"轮询到它的那个 Raft 线程"才真正处理。
> 4. **`Poller::poll` 的批量循环**——一个线程怎么一次取出一批 FSM、批量 `handle_normal`、再用"hot_fsm_count % 2"这种**反直觉但精妙**的策略防止热点 FSM 独占线程。
> 5. 三种 FSM(`PeerFsm` / `StoreFsm` / `ApplyFsm`)怎么分工协作——Raft 的"推进"和"落盘"被拆进两个 batch-system,为什么这么拆。

> **如果一读觉得太难**:先只记住三件事——① 每个 Peer 是一个 FSM,有自己的私有状态和邮箱(mailbox);② 几个 Raft 线程从全局队列里批量取出"有消息的 FSM"来处理,处理完放回,这就是 actor 模型;③ FSM 的所有权靠一个三态原子变量流转(IDLE→NOTIFIED→IDLE),保证一个 FSM 同时只被一个线程碰,所以无锁。具体代码细节先不啃,记住这三件事,后续章节引用 batch-system 时你能对上号就行。

---

## 〇、一句话点破

> **batch-system 把每个 Peer 抽象成一个 FSM(有自己的私有状态和邮箱),让几个 Raft 线程批量地从全局队列里取出"有消息待处理"的 FSM 来驱动——FSM 的所有权靠一个三态原子变量在线程间无锁流转,保证同一 FSM 同一时刻只被一个线程碰。这是 actor 模型在 Rust 里的一次工业级落地,也是"百万 Peer、几个线程"之所以能成立的全部秘密。**

这是结论。本章倒过来拆:先讲为什么 FSM 抽象能消灭锁;再讲 mailbox 和 Router 怎么让"给 Peer 发消息"变得廉价;接着拆 `FsmState` 三态流转这个无锁所有权的核心技巧;然后钻进 `Poller::poll` 的批量循环,看清一个线程一轮到底干了什么;最后把三种 FSM 的分工和 raftstore-v2 的演进讲清。

---

## 一、为什么是 FSM:把状态藏起来,就不需要锁了

几十万个 Peer,每个都有自己的状态:`RawNode`(Raft 状态机)、当前 term、已 propose 但未提交的命令队列、hibernate 状态、mailbox……这些状态**会同时被多个地方访问**:Raft 线程在 `tick()` 它,网络线程在给它投递 Raft 消息,客户端在给它发写请求。多个线程碰同一份状态,怎么不出错?

朴素的答案是**加锁**:每个 Peer 一把 `Mutex`,谁要碰谁锁住。但这在几十万规模下有三个硬伤:

1. **锁本身有开销**:加锁/解锁、cache line 反弹。几十万个锁的元数据也是内存。
2. **粒度难调**:粗了(一把大锁护一个 Peer 的所有状态)把本可并行的操作串成串行;细了(每个字段一把锁)代码爆炸、容易死锁。
3. **热 Peer 的锁竞争**:热点 Peer 被频繁访问,锁成了瓶颈——所有访问者都在等这把锁。

TiKV 的答案更狠:**根本不让多个线程同时碰同一个 Peer 的状态**。怎么做到?把每个 Peer 包成一个 **FSM**,加上一条铁律——

> **一个 FSM 的内部状态,只有"当前持有它的那个线程"能读写;别的线程想影响它,只能通过给它发消息(mailbox),不能直接碰它的字段。**

这就是 **actor 模型**:每个 Peer 是一个 actor,有自己的私有状态,actor 之间靠消息通信。状态被"藏"在 FSM 里,外面根本看不到、碰不到——既然碰不到,就不需要锁保护。看 batch-system 里 `Fsm` trait 的定义(`components/batch-system/src/fsm.rs`):

```rust
/// A `Fsm` is a finite state machine. It should be able to be notified for
/// updating internal state according to incoming messages.
pub trait Fsm: Send + 'static {                              // batch-system/src/fsm.rs#L40
    type Message: Send + ResourceMetered;

    const FSM_TYPE: FsmType;

    fn is_stopped(&self) -> bool;

    /// Set a mailbox to FSM, which should be used to send message to itself.
    fn set_mailbox(&mut self, _mailbox: Cow<'_, BasicMailbox<Self>>)
    where
        Self: Sized,
    {
    }

    /// Take the mailbox from FSM.
    fn take_mailbox(&mut self) -> Option<BasicMailbox<Self>>
    where
        Self: Sized,
    {
        None
    }

    fn get_priority(&self) -> Priority {
        Priority::Normal
    }
}
```

注意这个 trait **没有任何"获取内部状态"的方法**——它只暴露 `is_stopped`(自己问自己)和 `set_mailbox` / `take_mailbox`(管理自己的邮箱)。FSM 的内部状态(`PeerFsm.peer`、`ApplyFsm.delegate` 等)是这个 trait 的实现细节,**外部完全访问不到**。外部唯一能影响 FSM 的方式,是往它的 mailbox 里塞消息(`BasicMailbox::try_send`)。

```rust
pub struct PeerFsm<EK, ER>                                   // raftstore/src/store/fsm/peer.rs#L145
where
    EK: KvEngine,
    ER: RaftEngine,
{
    pub peer: Peer<EK, ER>,            // 私有状态:Raft 状态机、term、命令队列……全在这
    tick_registry: [bool; PeerTick::VARIANT_COUNT],
    missing_ticks: usize,
    hibernate_state: HibernateState,
    stopped: bool,
    has_ready: bool,
    mailbox: Option<BasicMailbox<PeerFsm<EK, ER>>>,
    pub receiver: Receiver<PeerMsg<EK>>,
    // ...
    batch_req_builder: BatchRaftCmdRequestBuilder<EK>,
}
```

`PeerFsm` 把 `Peer`(真正的状态)和它的消息 receiver 都包在一起。外面的线程只能拿到 `BasicMailbox<PeerFsm>`(一个克隆便宜的句柄),往里 `try_send` 消息;**消息进了 receiver,等某个 Raft 线程把这个 FSM 取出来,才会被处理**。从始至终,`Peer` 的字段只有一个线程在碰——那个"持有 FSM"的线程。

> **不这样会怎样**:如果不用 FSM 抽象、直接让多个线程共享 `&mut Peer`,你得给 Peer 的每个字段加锁,而且锁的粒度极难调(粗了串行、细了死锁)。几十万个 Peer × 多个字段 × 多线程访问,锁的数量和竞争会失控。FSM 把状态私有化、用消息通信,**把"并发安全"问题转化成了"消息投递"问题**——后者靠 mailbox + 单线程持有,天然安全。

---

## 二、mailbox 和 Router:给任意 Peer 发消息,O(1) 找到它

actor 模型的第一要素是"消息通信"。TiKV 用两个结构实现:**`BasicMailbox`(每个 FSM 一个,持有消息队列 + FSM 状态)**,和 **`Router`(全局路由表,addr → mailbox)**。

### BasicMailbox:一个 FSM 的"信箱 + 状态持有者"

看 `components/batch-system/src/mailbox.rs`:

```rust
pub struct BasicMailbox<Owner: Fsm> {                        // mailbox.rs#L31
    sender: mpsc::LooseBoundedSender<Owner::Message>,       // 消息队列的发送端
    state: Arc<FsmState<Owner>>,                            // FSM 的"状态持有者"(关键!)
}
```

一个 `BasicMailbox` 持有两样东西:

1. **消息队列的发送端**(`sender`):别人往这个邮箱里 `try_send(msg)`,msg 就进了队列。队列的另一端(receiver)在 FSM 自己手里(如 `PeerFsm.receiver`)。
2. **`FsmState` 的 Arc 引用**(`state`):这是个原子状态机,记录这个 FSM 现在是"空闲(IDLE)"、"被线程拿走了(NOTIFIED)"、还是"已销毁(DROP)"。下一节详拆。

`BasicMailbox` 是 `Clone` 的(`mailbox.rs#L107`),克隆只是复制两个 Arc 引用,很便宜。所以一个 FSM 可以有多个 mailbox 句柄分发到各处——网络线程拿一个、Raft 线程拿一个、scheduler 拿一个——大家都往同一个队列里塞消息。

发消息的逻辑(`mailbox.rs#L88`):

```rust
pub fn try_send<S: FsmScheduler<Fsm = Owner>>(               // mailbox.rs#L88
    &self,
    msg: Owner::Message,
    scheduler: &S,
) -> Result<(), TrySendError<Owner::Message>> {
    scheduler.consume_msg_resource(&msg);
    self.sender.try_send(msg)?;          // ① 消息进队列
    self.state.notify(scheduler, Cow::Borrowed(self));  // ② 如果 FSM 空闲,唤醒它(投递到全局队列)
    Ok(())
}
```

两步:① 把消息塞进队列;② `notify`——如果这个 FSM 当前是空闲的(没被任何线程持有),就把它**投递到全局 fsm_receiver 队列**,等某个 Raft 线程取走。`notify` 是 actor 模型的"唤醒"动作,它的实现靠 `FsmState`,下一节拆。

### Router:全局路由表

光有 mailbox 不够——几十万个 mailbox,发消息的人怎么知道某个 Peer 的 mailbox 在哪?靠 `Router`(`components/batch-system/src/router.rs`):

```rust
pub struct Router<N: Fsm, C: Fsm, Ns, Cs> {                  // router.rs#L48
    normals: Arc<DashMap<u64, BasicMailbox<N>>>,             // 全局 addr → mailbox 表(普通 FSM)
    pub(super) control_box: BasicMailbox<C>,                 // 唯一的控制 FSM mailbox
    pub(crate) normal_scheduler: Ns,
    pub(crate) control_scheduler: Cs,
    state_cnt: Arc<AtomicUsize>,
    shutdown: Arc<AtomicBool>,
}
```

`normals: DashMap<u64, BasicMailbox<N>>` 是核心——一个并发安全的 hashmap,key 是 `u64`(Peer 的地址,通常是 region_id 或 peer_id),value 是这个 FSM 的 mailbox。`DashMap` 是 Rust 生态里分片加锁的并发 map,读多写少场景下性能很好。

`register(addr, mailbox)`(`router.rs#L118`)把一个新创建的 FSM 注册进路由表:

```rust
pub fn register(&self, addr: u64, mailbox: BasicMailbox<N>) {
    if let Some(mailbox) = self.normals.insert(addr, mailbox) {
        mailbox.close();        // 如果这个 addr 之前有旧 mailbox(比如 Peer 被销毁重建),关掉旧的
    }
}
```

发消息给某个 Peer 时,先 `normals.get(&addr)` 拿到它的 mailbox,再 `mailbox.try_send(msg, scheduler)`——O(1) 的路由,没有遍历、没有广播。这就是为什么"给任意 Peer 发消息"能很廉价:就是一次 hashmap 查找 + 一次 channel 发送。

> **钉死这件事**:mailbox + Router 合起来,实现了"任意线程 → 任意 Peer"的廉价消息投递。Router 是全局地址簿(DashMap),mailbox 是每个 FSM 的信箱 + 状态。这套设计让"几十万个 FSM 互相通信"变成了"hashmap 查找 + channel 发送",完全没有传统 RPC 那种连接/序列化开销——因为所有 FSM 都在同一个进程里,消息就是普通的 Rust 结构体在 channel 里流转。

---

## 三、技巧精解一:FsmState 三态——一个 CAS 实现无锁所有权流转

这是整个 batch-system 最精妙的地方。几十万个 FSM,几个线程,怎么保证**同一个 FSM 同一时刻只被一个线程持有并处理**?用锁?太贵。TiKV 用了一个**三态原子变量 + 一个 CAS**,优雅地解决了这个问题。

### 三态的定义

看 `components/batch-system/src/fsm.rs`:

```rust
/// A holder of FSM.
///
/// There are three possible states:
///
/// 1. NOTIFYSTATE_NOTIFIED: The FSM is taken by an external executor. `data`
///    holds a null pointer.
/// 2. NOTIFYSTATE_IDLE: No actor is using the FSM. `data` owns the FSM.
/// 3. NOTIFYSTATE_DROP: The FSM is dropped. `data` holds a null pointer.
pub struct FsmState<N> {                                     // fsm.rs#L76
    status: AtomicUsize,
    data: AtomicPtr<N>,
    state_cnt: Arc<AtomicUsize>,
}

impl<N: Fsm> FsmState<N> {
    const NOTIFYSTATE_NOTIFIED: usize = 0;                   // fsm.rs#L84
    const NOTIFYSTATE_IDLE: usize = 1;                       // fsm.rs#L85
    const NOTIFYSTATE_DROP: usize = 2;                       // fsm.rs#L86
    // ...
}
```

三个状态:

| 状态 | 含义 | `data` 指针 |
|------|------|------------|
| `NOTIFIED`(0) | FSM 正被某个 Poller 线程持有处理中 | null(指针被线程拿走了) |
| `IDLE`(1) | FSM 空闲,没人处理它 | 指向 FSM 的 `Box`(它"拥有"自己) |
| `DROP`(2) | FSM 已销毁 | null |

注意一个反直觉的设计:`FsmState` 不是 FSM 本身,而是**FSM 的"所有权 holder"**。FSM 真正的 `Box<N>` 指针存在 `data: AtomicPtr<N>` 里。当 FSM 空闲(IDLE)时,`data` 指向它(它"自持");当某个线程要处理它时,通过 CAS 把状态从 IDLE 翻成 NOTIFIED,**同时把 `data` 的指针 swap 成 null 取走**——FSM 的所有权就从"自持"转移到了"线程持有"。线程处理完,再把 FSM 的 Box 放回 `data`,状态翻回 IDLE。

### 取走 FSM:`take_fsm`

```rust
pub fn take_fsm(&self) -> Option<Box<N>> {                   // fsm.rs#L98
    let res = self.status.compare_exchange(
        Self::NOTIFYSTATE_IDLE,
        Self::NOTIFYSTATE_NOTIFIED,
        Ordering::AcqRel,
        Ordering::Acquire,
    );
    if res.is_err() {
        return None;                  // CAS 失败:别人已经拿走了(或已 DROP)
    }

    let p = self.data.swap(ptr::null_mut(), Ordering::AcqRel);
    if !p.is_null() {
        Some(unsafe { Box::from_raw(p) })   // 拿到 FSM 的所有权
    } else {
        panic!("inconsistent status and data, something should be wrong.");
    }
}
```

核心是一个 `compare_exchange(IDLE → NOTIFIED)`。**只有当前状态是 IDLE 时才能成功**——这保证了"同一时刻只有一个线程能把 FSM 从 IDLE 翻成 NOTIFIED"。CAS 成功后,`data.swap(null)` 取走指针,FSM 的所有权转移到线程。CAS 失败(别人先成功了)直接返回 None。

> **关键洞察**:这个 CAS 是**无锁所有权流转**的全部。它不需要任何 Mutex,因为"同一 FSM 只被一个线程持有"这个不变式,是由"IDLE→NOTIFIED 的 CAS 只能成功一次"这个原子语义保证的。这是无锁编程的经典套路——**用原子变量的状态转换,代替互斥锁的临界区**。

### 唤醒 FSM:`notify`

`mailbox.try_send` 里调用的 `notify`,做的事情是:如果 FSM 当前 IDLE,就把它投递到全局 fsm_receiver 队列(让某个 Raft 线程取走):

```rust
pub fn notify<S: FsmScheduler<Fsm = N>>(                     // fsm.rs#L119
    &self,
    scheduler: &S,
    mailbox: Cow<'_, BasicMailbox<N>>,
) {
    match self.take_fsm() {
        None => {}                            // FSM 已被线程持有(NOTIFIED)或已 DROP,不用再投递
        Some(mut n) => {
            n.set_mailbox(mailbox);           // 给 FSM 装上自己的 mailbox(它能给自己发消息)
            scheduler.schedule(n);            // 投递到全局队列
        }
    }
}
```

精髓在 `match self.take_fsm()`:`take_fsm` 内部的 CAS 保证,**即使多个线程同时给同一个 FSM 发消息、同时调 `notify`,也只有一个能成功 take_fsm**(把 FSM 从 IDLE 翻成 NOTIFIED)。成功的那个把 FSM 投递到全局队列;失败的 None 直接返回——因为 FSM 已经在(或马上在)某个线程手里了,新消息进了它的队列,等那个线程处理完一轮会看到。

这就是"消息去重唤醒":**给同一个 FSM 发 100 条消息,它只会被投递到全局队列一次**(第一次 notify 成功),不会被 100 次投递导致 100 次调度。剩下 99 条消息安静地躺在队列里,等那个唯一的持有者线程把它取出来一并处理。

### 归还 FSM:`release`

线程处理完一批消息,要把 FSM 还回去:

```rust
pub fn release(&self, fsm: Box<N>) {                        // fsm.rs#L139
    let previous = self.data.swap(Box::into_raw(fsm), Ordering::AcqRel);  // 把 Box 指针放回 data
    let mut previous_status = Self::NOTIFYSTATE_NOTIFIED;
    if previous.is_null() {
        // data 之前是 null(正常,因为线程持有期间 data 是 null)
        let res = self.status.compare_exchange(
            Self::NOTIFYSTATE_NOTIFIED,
            Self::NOTIFYSTATE_IDLE,                         // 状态翻回 IDLE
            Ordering::AcqRel,
            Ordering::Acquire,
        );
        match res {
            Ok(_) => return,                                // 正常归还:IDLE 了,等下次 notify
            Err(Self::NOTIFYSTATE_DROP) => {                // 期间 FSM 被要求销毁
                let ptr = self.data.swap(ptr::null_mut(), Ordering::AcqRel);
                unsafe { let _ = Box::from_raw(ptr); }      // 销毁
                return;
            }
            Err(s) => s,
        }
    }
    panic!("invalid release state: ...");
}
```

`release` 是 `take_fsm` 的逆操作:把 Box 指针塞回 `data`,状态从 NOTIFIED 翻回 IDLE。这里有个细节——`release` 之后会**重新检查 FSM 的队列**(在 `Batch::release` 里),如果队列里又攒了新消息(在它被处理期间有新消息进来),会**立刻再 take_fsm 一次**,继续处理。这就是 actor 模型的"持续驱动":只要邮箱里有消息,FSM 就不会被闲置。

> **不这么写会怎样**:如果用 `Mutex<Option<Box<N>>>` 来保护 FSM 的所有权,每次 take/release 都要加锁/解锁,几十万个 FSM × 高频投递,锁竞争会成为瓶颈。而 `AtomicUsize + AtomicPtr + CAS` 是无锁的,在无竞争时(常见,因为同一 FSM 通常只被一个线程活跃处理)CAS 一次就过,开销极低。**这是无锁数据结构在"高频小对象所有权流转"场景下的典型胜利**——Rust 的 `unsafe`(Box::from_raw / into_raw)在这里被谨慎地用来手动管理所有权,是 unsafe 用得其所的范例(对标《Tokio》里 Pin/UnsafeCell 的拆法)。

---

## 四、Poller::poll:一个线程的一轮,到底干了什么

有了 FSM、mailbox、Router、FsmState,现在看一个 Raft 线程(Poller)的循环。这是 batch-system 的"主循环",定义在 `components/batch-system/src/batch.rs`:

```rust
pub fn poll(&mut self) {                                     // batch.rs#L382
    let mut batch = Batch::with_capacity(self.max_batch_size);
    let mut reschedule_fsms = Vec::with_capacity(self.max_batch_size);
    let mut to_skip_end = Vec::with_capacity(self.max_batch_size);

    let mut run = true;
    while run && self.fetch_fsm(&mut batch) {                // ① 取一批 FSM
        let mut max_batch_size = std::cmp::max(self.max_batch_size, batch.normals.len());
        self.handler.begin(max_batch_size, |cfg| {           // ② 每轮开始
            self.max_batch_size = cfg.max_batch_size();
        });

        if batch.control.is_some() {                         // ③ 处理控制 FSM(StoreFsm)
            let len = self.handler.handle_control(batch.control.as_mut().unwrap());
            // ... 控制 FSM 的归还/移除逻辑
        }

        for (i, p) in batch.normals.iter_mut().enumerate() {  // ④ 批量处理普通 FSM(PeerFsm)
            let p = p.as_mut().unwrap();
            let res = self.handler.handle_normal(p);          //    真正的 Raft 推进在这
            // ... 根据 res 和优先级、热度,决定是 Release / Remove / Schedule
        }

        // ⑤ 还能再塞更多 FSM 的话,继续 try_recv 并处理(填满 max_batch_size)
        while batch.normals.len() < max_batch_size {
            if let Ok(fsm) = self.fsm_receiver.try_recv() { run = batch.push(fsm); }
            // ... handle_normal 新进来的
        }

        self.handler.light_end(&mut batch.normals);          // ⑥ 轻量收尾(批量发消息等)
        for index in &to_skip_end { batch.schedule(&self.router, *index); }
        self.handler.end(&mut batch.normals);                // ⑦ 每轮结束(批量 flush)

        batch.tick_round();                                  // ⑧ 更新指标
        for index in reschedule_fsms.iter().rev() {          // ⑨ 重新调度(Release/Remove/Schedule)
            batch.schedule(&self.router, *index);
            batch.swap_reclaim(*index);
        }
    }
}
```

一轮 `poll` 做了 9 件事,核心是 ①④⑦。拆开看:

### ① fetch_fsm:取一批,空了就睡

```rust
fn fetch_fsm(&mut self, batch: &mut Batch<N, C>) -> bool {   // batch.rs#L362
    if batch.control.is_some() {
        return true;
    }
    if let Ok(fsm) = self.fsm_receiver.try_recv() {          // 先非阻塞取
        return batch.push(fsm);
    }
    if batch.is_empty() {
        self.handler.pause();                                // 没活儿了,准备睡
        if let Ok(fsm) = self.fsm_receiver.recv() {          // 阻塞等一个
            return batch.push(fsm);
        }
    }
    !batch.is_empty()
}
```

`fetch_fsm` 先 `try_recv`(非阻塞)攒一批;队列空了就 `pause`(handler 可以做点收尾,比如 flush 缓冲),然后 `recv`(阻塞)等下一个 FSM 来。这就是"事件驱动"——**没有 FSM 待处理时,线程真的会阻塞睡眠,不空转 CPU**。这呼应了上一章讲的"事件驱动"雏形。

### ④ handle_normal:批量处理,这是 Raft 推进的真正发生地

`PollHandler` trait(`batch.rs#L298`)定义了"怎么处理一批 FSM"的接口:

```rust
pub trait PollHandler<N, C>: Send + 'static {                // batch.rs#L298
    /// 每轮开始
    fn begin<F>(&mut self, _batch_size: usize, update_cfg: F)
    where for<'a> F: FnOnce(&'a Config);

    /// 控制 FSM 就绪时
    fn handle_control(&mut self, control: &mut C) -> Option<usize>;

    /// 普通 FSM 就绪时——Raft 推进的核心
    fn handle_normal(&mut self, normal: &mut impl DerefMut<Target = N>) -> HandleResult;

    /// handle_normal 全部调完之后、end 之前,做轻量工作(比如批量发攒着的消息)
    fn light_end(&mut self, _batch: &mut [Option<impl DerefMut<Target = N>>]) {}

    /// 每轮结束
    fn end(&mut self, batch: &mut [Option<impl DerefMut<Target = N>>]);

    /// 准备睡觉时
    fn pause(&mut self) {}
}
```

`PollHandler` 是个 trait——**batch-system 库本身不知道怎么处理 PeerFsm**(它不知道 Raft 是什么),它只定义"begin/handle_normal/end"这套钩子,具体的 Raft 处理逻辑由 raftstore 实现(`RaftPollerBuilder` / `RaftPoller` 在 `store/fsm/store.rs`)。这就是 batch-system 作为**通用 actor 框架**的设计——它和具体的业务(Peer/Apply)解耦。

`handle_normal` 的具体实现(在 raftstore 的 `RaftPoller`),大致做这几件事(详见 P2-05):① 把 PeerFsm mailbox 里的消息(写请求、Raft 消息、tick 信号)全取出来;② 喂给 `raft_group.step()` 或触发 `raft_group.tick()`;③ 检查 `raft_group.has_ready()`,如果有 ready(要持久化的日志、要发的消息、要 apply 的已提交命令),交给后续 worker;④ 返回 `HandleResult`,告诉 batch-system 是继续处理(KeepProcessing)还是暂停(StopAt)。

### ⑨ reschedule:热点 FSM 不能独占线程

这里有个**最反直觉但最精妙**的设计。看 poll 循环里这段(`batch.rs#L423`):

```rust
if p.metrics.timer.saturating_elapsed() >= self.reschedule_duration {
    hot_fsm_count += 1;
    // We should only reschedule a half of the hot regions, otherwise,
    // it's possible all the hot regions are fetched in a batch the
    // next time.
    if hot_fsm_count % 2 == 0 {                              // batch.rs#L429
        p.policy = Some(ReschedulePolicy::Schedule);
        reschedule_fsms.push(i);
        continue;
    }
}
```

注释说得很明白:一个 FSM 如果连续被处理超过 `reschedule_duration`(默认 5 秒,`config.rs#L31`),它就是"热"的。热 FSM 会被重新调度(`ReschedulePolicy::Schedule`)——也就是从当前线程放回全局队列,让别的线程取走。

**但只重新调度一半**(`hot_fsm_count % 2 == 0`)。为什么?注释给出了答案:"otherwise, it's possible all the hot regions are fetched in a batch the next time." 如果你把所有热 FSM 都 reschedule,它们会一起回到全局队列;下一轮某个线程 fetch_fsm 时又会把它们全取回来——又成了"一个线程独占所有热点"。**只 reschedule 一半,让热点散布到多个线程,避免"热点扎堆"**。

> **钉死这件事**:这是 batch-system 公平性的核心。朴素 actor 模型有个通病——一个慢/热的 actor 会独占它所在的线程,别的 actor 饿死。TiKV 用"超时 + 半数 reschedule"破解:热 FSM 超时就被放回全局队列,而且只放一半,让热点散布开。**这个 `% 2` 看着土,却是生产里调出来的防饿死关键**——没有它,一个热点 Region 能把一个 Raft 线程卡死,同线程的其他几万个 Region 全部阻塞。

---

## 五、三种 FSM:PeerFsm / StoreFsm / ApplyFsm 的分工

batch-system 是个**双 FSM 模型**:每个 batch-system 有一个"控制 FSM"(Control,全局唯一)和多个"普通 FSM"(Normal,每个 Peer 一个)。但 TiKV 实际上跑了**两个 batch-system**,所以一共有三种 FSM。看 `components/raftstore/src/store/fsm/store.rs`:

```rust
pub fn create_raft_batch_system<EK: KvEngine, ER: RaftEngine>(     // store.rs#L2093
    cfg: &Config,
    resource_manager: &Option<Arc<ResourceGroupManager>>,
) -> (RaftRouter<EK, ER>, RaftBatchSystem<EK, ER>) {
    let (store_tx, store_fsm) = StoreFsm::new(cfg);                 // 唯一的控制 FSM(StoreFsm)
    let (apply_router, apply_system) = create_apply_batch_system(   // 另一个 batch-system(Apply)
        cfg,
        ...
    );
    let (router, system) = batch_system::create_system(             // Raft batch-system
        &cfg.store_batch_system,
        store_tx,
        store_fsm,
        None,
    );
    // ...
}
```

三种 FSM 分工:

| FSM | 所在 batch-system | 角色 | 数量 | Message 类型 |
|-----|------------------|------|------|-------------|
| `StoreFsm` | Raft 系统(Control) | 全局控制:创建/销毁 Peer、Store 级消息 | **1 个** | `StoreMsg`(`store.rs#L794`) |
| `PeerFsm` | Raft 系统(Normal) | 每个 Region 的一个副本:跑 Raft | **几十万个** | `PeerMsg`(`peer.rs#L635`) |
| `ApplyFsm` | Apply 系统(Normal) | 把 Raft 已提交的命令 apply 到 RocksDB | **几十万个** | `Box<Msg>`(`apply.rs#L4632`) |

为什么把 Raft 推进和 Apply 拆成两个 batch-system?这是个关键的架构决定:

### 拆分的原因:Raft 推进快,Apply 慢,不能互相阻塞

`PeerFsm` 干的事(`tick`、`step`、`propose`、处理 ready)大多是**纯内存操作 + 少量日志写**,很快(微秒到毫秒级)。而 `ApplyFsm` 干的事是把命令写进 RocksDB——**RocksDB 写涉及 MemTable、WAL、可能触发 Compaction,是慢操作**(毫秒到几十毫秒)。如果把两者放一个 batch-system,一个慢 Apply 会卡住同线程的 Raft 推进——某个 Region 的 Apply 慢了,同线程其他 Region 的心跳都发不出去,leader 被以为挂了,触发重新选举。

拆成两个 batch-system 后:Raft 线程专注推进 Raft(快),Apply 线程专注写 RocksDB(慢)。Raft 线程把已 commit 的命令通过 mailbox 发给对应的 ApplyFsm(`ApplyFsm` 和 `PeerFsm` 一一对应,都用 region_id 寻址),Apply 线程异步消费。Apply 完成后,ApplyFsm 再把结果通过 mailbox 发回给 PeerFsm(更新 apply index 等)。**Raft 推进不被 Apply 阻塞,Apply 也不被 Raft 的实时性要求裹挟**。

`ApplyFsm` 的结构(`apply.rs#L4048`):

```rust
pub struct ApplyFsm<EK>                                       // apply.rs#L4048
where
    EK: KvEngine,
{
    delegate: ApplyDelegate<EK>,        // Apply 的状态:当前 region、apply 到哪个 index 了……
    receiver: Receiver<Box<Msg<EK>>>,   // 收 PeerFsm 发来的"这批命令 commit 了,请 apply"
    mailbox: Option<BasicMailbox<ApplyFsm<EK>>>,
}
```

Apply 系统的 batch 还有个额外优化——**cmd batch**:多个已 commit 的命令攒一批一起 apply,提高 RocksDB 写吞吐(P3-11 详拆)。

> **钉死这件事**:三个 FSM 的分工,本质是"快慢分离"。Raft 推进(PeerFsm)要快且实时(心跳不能断),Apply(ApplyFsm)慢但可批量。把它们拆进两个 batch-system,用 mailbox 通信,让两者各跑各的、互不阻塞。这是 TiKV 流水线(P2-05 五步流水线)能并行的根基——**没有这个分离,一个慢 Apply 就能让几十万个 Region 的 Raft 心跳停摆**。

### 控制 FSM(StoreFsm)是干嘛的

`StoreFsm` 全局唯一,处理"store 级"的事件:创建新 Peer(收到新 Region 的消息时)、销毁 Peer、store 级配置变更、一致性检查。它是 batch-system 框架要求的"Control FSM"角色的具体实现——batch-system 规定每个系统有一个 Control FSM 来做"需要全局视角"的事。这种"Normal/Control 双 FSM"的设计在 `batch.rs` 开头的注释里讲得很清楚:

> Generally there will be two different kind of FSMs in TiKV's FSM system. One is normal FSM, which usually represents a peer, the other is control FSM, which usually represents something that controls how the former is created or metrics are collected.

---

## 六、几个线程,驱动几十万个 Peer

讲完机制,回到一个具体问题:**batch-system 到底开几个线程?** 看 `BatchSystem::spawn`(`batch.rs#L595`):

```rust
pub fn spawn<B>(&mut self, name_prefix: String, mut builder: B)  // batch.rs#L595
where
    B: HandlerBuilder<N, C>,
    B::Handler: Send + 'static,
{
    for i in 0..self.pool_size {                              // 开 pool_size 个 Normal 线程
        self.start_poller(thd_name!(format!("{}-{}", name_prefix, i)), Priority::Normal, &mut builder);
    }
    for i in 0..self.low_priority_pool_size {                 // 加几个 Low 优先级线程
        self.start_poller(thd_name!(format!("{}-low-{}", name_prefix, i)), Priority::Low, &mut builder);
    }
    self.name_prefix = Some(name_prefix);
}
```

`pool_size` 来自配置。`batch-system/src/config.rs` 的默认值:

```rust
impl Default for Config {                                     // batch-system/src/config.rs#L26
    fn default() -> Config {
        Config {
            max_batch_size: None,        // unwrap_or(256)
            pool_size: 2,                // 默认 2 个线程!
            reschedule_duration: ReadableDuration::secs(5),
            low_priority_pool_size: 1,
        }
    }
}
```

**默认 pool_size = 2**。注意这是 batch-system 库的默认;raftstore 在自己的 config 里可以覆盖(`store_batch_system.pool_size`,默认也是 2,生产环境按 CPU 核数调大,上限是 `cpu_cores * 10`,`config.rs#L960`)。也就是说,**默认情况下,2 个 Raft 线程驱动几十万个 PeerFsm,2 个(可配)Apply 线程驱动几十万个 ApplyFsm**——加上几个其他 worker(网络、scheduler),整个 TiKV 进程也就几十个线程,管着百万级 FSM。

这就是"百万 Peer、几个线程"的字面真相。它之所以可能,是因为前面那套 FSM + mailbox + 无锁所有权流转 + 批量 poll 的设计——**把"并发驱动百万个状态机"这个问题,降成了"几个线程批量消费一个全局队列"**。

> **钉死这件事**:batch-system 不是"开很多线程硬扛",而是"用很少的线程 + 巧妙的状态管理,把百万 FSM 调度得井井有条"。默认 2 个 Raft 线程能扛住几十万 Peer,靠的是:① 空闲 Peer 不占线程(事件驱动,mailbox 有消息才唤醒);② 一个线程一次处理一批 FSM(批量);③ 热点 FSM 被半数 reschedule 散布开(公平)。**这不是"堆线程",是"省线程"——用算法换资源**。

---

## 七、架构演进:raftstore-v2 打破了什么

本书以经典 raftstore(v1)为主线。v1 的核心约束是:**每个 store 的所有 PeerFsm,共享一个 batch-system**(默认 2 个 Raft 线程)。这意味着——一个 store 上的几十万个 Peer 的 Raft 推进,**串行在 2 个线程里跑**。虽然批量 + 事件驱动让它总体很快,但有个天花板:**单个 Region 的 Raft 推进,无法利用多核**。一个热点 Region 的 Raft 操作(比如大批量 propose),只能跑在 2 个线程中的一个上,其余 CPU 核心干着急。

raftstore-v2(`components/raftstore-v2/`)打破了这点——它把 Raft 推进**多线程化**了:不再是"一个 store 一个 batch-system",而是更细粒度的并行(多个 Raft 线程池、Region 分组到不同线程)。配合 region bucket(Region 内部再切分,P1-02 提过)和更大的默认 Region(10GB),v2 能把单个大 Region 的负载摊到多个 CPU 核上。

但 v2 的复杂度也高得多——多线程 Raft 引入了新的同步问题(比如多线程 apply 的顺序保证)。所以本书的策略是:**v1 讲清 multi-Raft 的原理(它更简单、更易理解),v2 作为演进方向对照**。v1 的 batch-system 是 v2 的基础——v2 仍然是"FSM + mailbox + Poller",只是把"一个 batch-system"扩展成了"多个并行 batch-system"。

> **钉死这件事**:v1(经典 raftstore)用"一个 store 一个 batch-system(2 线程)"的简单模型,讲清了 multi-Raft 的全部精髓——FSM、mailbox、无锁所有权、批量 poll、热点 reschedule。v2 在这套基础上加多线程,突破"单 Region 无法用多核"的天花板。**理解 v1 是理解 v2 的前提**;v2 的多线程不是推翻 v1,而是把 v1 的"一个 Poller"扩展成"多个 Poller 协作"。

---

## 八、章末小结

### 回扣主线

本章是全书**复制层招牌章**,它把"百万个 Raft 组怎么共存"这个核心问题,从 P1-03 的"雏形"落到了真实代码。batch-system + FSM 是 TiKV 区别于 etcd 的最硬核工程技巧——etcd 一个 Raft 组,根本不需要这套东西;TiKV 几十万个 Raft 组,必须靠它。后续所有复制层章节(P2-05 raftstore 全貌、P2-06 RaftEngine、P3-11 Apply)都建立在 batch-system 之上——PeerFsm 发起 Raft 提议、RaftEngine 存日志、ApplyFsm 把命令落盘,全是在这套 actor 框架里跑的。

一句话总结:每个 Peer 是一个 FSM(状态私有,消灭锁);mailbox + Router 让任意线程给任意 Peer 投递消息(O(1) 路由);FsmState 三态 + 一个 CAS 实现无锁所有权流转(同一 FSM 同时只被一个线程碰);Poller::poll 批量取出一批 FSM 处理(事件驱动 + 批量 + 热点半数 reschedule);三种 FSM(PeerFsm/StoreFsm/ApplyFsm)分工,Raft 推进和 Apply 慢操作分离;默认 2 个线程驱动几十万 Peer,靠算法换资源。

### 五个为什么

1. **为什么把 Peer 抽象成 FSM?**——让每个 Peer 的私有状态只能被"持有它的线程"碰,从根上消灭数据竞争,而不是靠加锁(几十万个锁会失控)。
2. **为什么 `FsmState` 用三态 + CAS 而不是 Mutex?**——CAS 是无锁的,无竞争时(常见)一次过,开销极低;Mutex 在高频小对象所有权流转场景下锁竞争成瓶颈。这是无锁编程的经典胜利。
3. **为什么给同一个 FSM 发 100 条消息只投递一次?**——`notify` 里的 `take_fsm` CAS 只能成功一次(IDLE→NOTIFIED),后续 notify 都返回 None;消息安静躺在队列里,等唯一的持有者线程一并处理。这是"消息去重唤醒",避免重复调度。
4. **为什么 Raft 和 Apply 拆成两个 batch-system?**——Raft 推进快且实时(心跳不能断),Apply 慢但可批量(写 RocksDB);拆开让两者各跑各的,避免慢 Apply 卡死 Raft 心跳触发误选举。
5. **为什么热点 FSM 只 reschedule 一半(`% 2`)?**——全 reschedule 会让所有热点一起回到全局队列、下轮又被一个线程全取走,热点又扎堆;只 reschedule 一半让热点散布到多线程,防饿死。这个看着土的 `% 2` 是生产调出来的防饿死关键。

### 想继续深入往哪钻

- **batch-system 框架源码**:`components/batch-system/src/`(本章大量引用)——`fsm.rs`(FsmState 三态)、`mailbox.rs`(BasicMailbox)、`router.rs`(Router DashMap)、`batch.rs`(Poller::poll、PollHandler trait)、`scheduler.rs`(NormalScheduler/ControlScheduler)。
- **PeerFsm 的具体实现**:`components/raftstore/src/store/fsm/peer.rs`(`PeerFsm` 结构 `peer.rs#L145`、`type Message = PeerMsg` `peer.rs#L635`、`PeerFsmDelegate` 处理消息 `peer.rs#L664`)。
- **StoreFsm 和创建**:`components/raftstore/src/store/fsm/store.rs`(`StoreFsm` `store.rs#L762`、`create_raft_batch_system` `store.rs#L2093`、`RaftBatchSystem` `store.rs#L1664`)。
- **ApplyFsm**:`components/raftstore/src/store/fsm/apply.rs`(`ApplyFsm` `apply.rs#L4048`、cmd batch 逻辑,P3-11 详拆)。
- **RaftPoller / RaftPollerBuilder**(PollHandler 的具体实现,真正的 Raft 推进逻辑):`components/raftstore/src/store/fsm/store.rs` 里搜 `impl PollHandler`,P2-05 详拆。
- **raftstore-v2 演进**:`components/raftstore-v2/`,看它怎么把"一个 batch-system"扩展成多线程;配合 P1-02 讲的 10GB Region + region bucket。
- **承接前作**:本章的 actor 模型 + 无锁所有权流转,是对标《Tokio》里 task 调度 + 无锁队列的同类技巧(Rust 系统级编程);`unsafe`(Box::from_raw/into_raw)的谨慎使用,对标《Tokio》Pin/UnsafeCell 的拆法。本书在涉及处会继续拆 Rust 技巧。

### 引出下一章

batch-system + FSM 立起来了——百万 Peer 能在一个进程里跑。但这只是"框架",一个 PeerFsm 收到一条写请求后,到底怎么发起 Raft 提议、日志怎么存、怎么复制、怎么 apply?这就是第 2 篇开头 P2-05——**raftstore 全貌:一条写请求的旅程**,把 Propose → Append → Replicate → Commit → Apply 五步流水线串起来。

> **下一章**:[P2-05 · raftstore 全貌:一条写请求的旅程](P2-05-raftstore全貌-一条写请求的旅程.md)
