# 第 1 篇 · 第 3 章 · Raft 库回顾与 multi-Raft 的挑战

> **核心问题**:上一章立起了 Region——一段 key range 一个独立 Raft 组。可一个 TiKV 进程里堆着几十万个 Region,也就是几十万个独立的 Raft 组。Raft 算法本身你在《etcd》那本已经啃透了(选主、日志复制、提交、安全性);本章不重复讲。本章只问一件事:**一个 Raft 库实例,怎么扩展成几十万个,还能在一个进程里跑得动?** 朴素地"每个 Region 一个 OS 线程定时推进 Raft",听上去最直接,可为什么这条路走不通?走不通之后,TiKV 又该往哪个方向想?

> **读完本章你会明白**:
> 1. TiKV 用的 `raft` crate(`rust-lang/raft-rs`,etcd Raft 的 Rust 移植)是个**无运行时、纯逻辑**的库——它不自己跑线程、不自己管网络,只暴露 `tick()` / `step()` / `propose()` 这种同步函数让你驱动。这个设计,是 multi-Raft 能成立的前提。
> 2. 朴素做法("每 Region 一个线程,每 100ms tick 一次")为什么爆炸——栈内存、上下文切换、cache 抖动,三道墙;几十万线程不是"慢",是"操作系统直接拒绝"。
> 3. 既然不能"一个 Region 一个线程",那剩下两条路:**时间复用**(少量线程批量轮询多个 Region)和 **事件驱动**(Region 有事才处理,没事不空转)。本章把这些思路的雏形立起来,具体落地留给下一章 P1-04 的 batch-system。
> 4. 为什么"批量"是 multi-Raft 的命根子——不只是批量 tick,还有批量 propose、批量 append、批量 send,这些"batch"贯穿后续整个 raftstore。

> **如果一读觉得太难**:先只记住三件事——① Raft 库是纯逻辑库,不自带线程,要你自己驱动;② 几十万线程撑爆操作系统,所以不能"一 Region 一线程";③ 解法的方向是"少量线程 + 批量轮询",具体是 P1-04 的 batch-system + FSM。

---

## 〇、一句话点破

> **Raft 算法本身(《etcd》拆透的选主/日志/提交)不是瓶颈;瓶颈在于"怎么把几十万个 Raft 实例塞进一个进程里跑"——而这恰恰是 etcd 从来没遇到过的问题,因为 etcd 只有一个 Raft 组。**

这是结论。本章倒过来拆:先回顾 TiKV 用的那个 Raft 库是个什么形态(为什么它"恰好"适合 multi-Raft);再算一笔账,证明"每 Region 一个线程"的朴素做法在几十万规模下会以三种方式爆掉;最后把"少量线程批量轮询"这条路的雏形画出来,把具体实现留给 P1-04。

---

## 一、承接《etcd》:Raft 算法本体,本章不重复

本书的"三重承接"里,第一条就是 **Raft 承接《etcd》**。所以在动笔前先把边界划清楚:

| 主题 | 在《etcd》拆过 | 本书(本章及后续)拆 |
|------|---------------|---------------------|
| Leader 选举(split vote、pre-vote、election timeout) | ✅ | 不重复 |
| 日志复制(append、replicate、commit 规则) | ✅ | 不重复 |
| 安全性(term、commit、log matching) | ✅ | 不重复 |
| 成员变更(joint consensus) | ✅ | 不重复 |
| Snapshot 与日志截断 | ✅ | 不重复 |
| 一个 Raft 库实例的接口形态 | ✅(etcd-raft Go) | 对照 raft-rs(Rust),简提 |
| **几十万个 Raft 实例怎么共存** | ❌(etcd 只有一个组) | **✅ 本章 + P1-04 招牌** |

一句话:**Raft 算法的"正确性"已经由《etcd》保证了;本书只问"工业上怎么把一个 Raft 放大成百万个"**。这正是书名"为什么能把一个 Raft 放大成百万个"的字面含义——放大的是**数量**,不是单个 Raft 的复杂度。

---

## 二、TiKV 用的 Raft 库:raft-rs,一个"纯逻辑"库

TiKV 用的 Raft 算法实现,是 `raft` crate(俗称 raft-rs),在 Cargo.toml 里钉得很明确:

```toml
# Cargo.toml#L207(workspace 依赖,指向 tikv/raft-rs master)
raft = { git = "https://github.com/tikv/raft-rs", branch = "master" }
# Cargo.toml#L368(具体版本)
raft = { version = "0.7.0", default-features = false, features = ["protobuf-codec"] }
```

它的逻辑和 etcd 的 Go 版 raft 库(`etcd-io/etcd/raft`)一脉相承——事实上 raft-rs 就是 etcd-raft 的 Rust 移植。它对外暴露的核心抽象是 **`RawNode`**——一个"光秃秃的"Raft 状态机。看 TiKV 怎么用它,在 `components/raftstore/src/store/peer.rs`:

```rust
pub struct Peer<EK, ER> {                            // peer.rs#L728 附近
    // ...
    pub raft_group: RawNode<PeerStorage<EK, ER>>,    // peer.rs#L728
    // ...
}
```

每个 Peer(一个 Region 的一个副本)里,都有一个 `raft_group: RawNode<...>` 字段。**这就是"一个 Region 一个 Raft 实例"的字面体现**:几十万个 Peer = 几十万个 `RawNode` 实例,活在同一个进程里。

`RawNode` 提供的是几个**同步**函数(简化示意,具体签名以 raft-rs 源码为准):

```rust
// (简化示意,非源码原文)
impl<T: Storage> RawNode<T> {
    pub fn new(cfg: &Config, storage: T, logger: &Logger) -> Result<RawNode<T>>;
    pub fn tick(&mut self);                              // 推进一个逻辑时钟(election/heartbeat 计数)
    pub fn step(&mut self, m: Message) -> Result<()>;    // 喂一条收到的 Raft 消息进来
    pub fn propose(&mut self, context: Vec<u8>, data: Vec<u8>) -> Result<u64>;  // 提议一条写
    pub fn has_ready(&self) -> bool;                     // 有没有待处理的输出(要发消息/要存日志/...)
    pub fn ready(&self) -> Ready;                        // 取出待处理输出
    pub fn advance(&mut self, rd: &Ready);               // 告诉它"上次的 ready 我处理完了"
}
```

注意这里**没有**的东西:

- **没有 `run()` / `start()` / 后台线程**。`RawNode` 自己不跑任何线程,它就躺在那里,等你调它的函数。
- **没有网络**。`RawNode` 不知道网络是什么,它只产出"要发给谁的消息"(`Ready::messages`),由你负责发出去。
- **没有磁盘**。它只产出"要持久化的日志"(`Ready::entries`),由你负责写盘。
- **没有定时器**。它不知道真实时间,所谓"选举超时""心跳间隔",是靠你**周期性地调 `tick()`** 来推进的——`tick()` 一次 = 一个逻辑 tick,逻辑 tick 攒够了就是 election timeout。

这是个非常关键的形态:**Raft 库是纯逻辑,所有 I/O(线程、网络、磁盘、时钟)都外置给使用者**。这种设计在 etcd 那本里你应该见过(etcd-raft 也是这个风格)——但它在 TiKV 这里产生了一个 etcd 没用到的红利,下面讲。

### 为什么"纯逻辑"是 multi-Raft 的前提

设想一下,如果 Raft 库是个"自带线程、自带网络、自带定时器"的"完整服务"(像某些 Raft 实现那样),你要起几十万个它——每个都自己开线程、自己连网络、自己设定时器。那几十万个线程、几十万条连接、几十万个定时器,直接把进程的内核资源耗光。

而"纯逻辑库"的形态意味着:**线程、网络、磁盘、时钟这些东西,全在你(TiKV)手里**。你可以**让一个线程驱动多个 `RawNode`**(一个线程轮询一批 Peer 的 `tick()`),可以让**一个网络连接复用传多个 Region 的消息**,可以让**一次磁盘写批量 append 多个 Region 的日志**。**I/O 资源是被你聚合的,而不是被 Raft 库瓜分的**。这是 multi-Raft 能成立的第一个前提。

> **钉死这件事**:raft-rs(以及 etcd-raft)这种"无运行时、纯逻辑、I/O 全外置"的库设计,本来是为了"使用者灵活集成",但在 TiKV 这里恰好成了救命稻草——它意味着几十万个 Raft 实例**可以共享同一批线程/网络/磁盘/时钟**,而不是每个实例独占一份。**没有这个前提,后面所有的 batch-system 优化都无从谈起**。

---

## 三、朴素做法:每 Region 一个线程,然后爆炸

理解了 Raft 库的形态,现在来看最朴素的 multi-Raft 实现思路。一个 Region 需要 Raft 持续推进(election timeout 要倒数、心跳要发),而 `RawNode::tick()` 是个普通函数,得有人周期性地调它。最直白的实现:

> 给每个 Region 开一个 OS 线程,这个线程的循环就是:睡 100ms → 调 `raft_group.tick()` → 检查 `has_ready()` → 处理 ready(发消息、写日志)。每个线程管自己那个 Region,互不干扰。

听起来清爽,代码也简单。但它会在几十万规模下以三种方式爆掉。

### 爆炸一:栈内存——线程是最贵的"对象"

Linux 上一个线程默认分配 **8MB 栈地址空间**(虽然实际驻留只有几十 KB,但地址空间要预留)。几十万个线程 = 几百万 GB 的虚拟地址空间预留,虽然 64 位地址空间够大,但内核要为每个线程维护 `task_struct`、内核栈(通常 8-16KB)、TLS、调度队列条目。**几十万线程的内核元数据,光是创建就慢得离谱,而且每个线程的内核栈+用户栈是实打实占内存的**。

粗算:30 万线程 × (16KB 内核栈 + 64KB 用户栈实际驻留) ≈ **24GB**。光是为"每个 Region 一个线程"这个设计,就要烧 24GB 内存——而且这些线程 99.9% 的时间在睡觉(`tick` 间隔是百毫秒级,真正干活的时间微乎其微)。**用最贵的对象(线程)去做最闲的事(周期性 tick),这是设计的反模式**。

### 爆炸二:上下文切换——CPU 全在切换,没在干活

几十万线程在睡觉,OS 调度器还是要轮询它们。每个线程到点被唤醒(tick 时刻到了),被调度上 CPU 跑几十微秒(`tick()` 很快),再睡。这意味着**每秒几十万次上下文切换**——每个切换要保存/恢复寄存器、切换 TLB、刷 cache 预热。

更糟的是 cache:**几十万个线程的栈和工作集,远超 CPU cache 容量**(L2 几百 KB、L3 几十 MB)。线程 A 上 CPU,把它的栈和工作集加载进 cache;切换到线程 B,B 的工作集把 A 的挤出 cache;再切回 A,A 的数据早被挤出去了——**cache 命中率雪崩,每个线程都在"冷 cache"上跑**。结果是:CPU 看着 100% 忙碌,大部分时间在切换和 cache miss 上,真正驱动 Raft 的有效计算少得可怜。

### 爆炸三:惊群与同步开销

Raft 心跳间隔通常是 100ms 量级。如果你让每个 Region 的 tick 时刻各自独立,几十万个线程会在每个 100ms 周期里**随机散布**地醒来——听上去均匀,实际上 OS 调度器的唤醒队列会有抖动尖峰。更要命的是,多个 Region 的 ready 处理往往要访问**共享资源**(同一个 RocksDB 实例、同一个网络连接),几十万线程抢同一把锁,锁竞争会让吞吐塌方。

> **不这样会怎样**:朴素地"每 Region 一个线程",在几十万规模下,不是"慢一点",而是"操作系统直接拒绝/进程起不来/起来了也跑不动"。这是个**不可行**的方案,不是"可行但次优"。TiKV 必须换思路。

### 反思:线程不该绑在 Region 上,该绑在"驱动"上

朴素做法的根本错误,是把"一个 Region 需要被周期性推进"误读成"一个 Region 需要一个专属线程"。其实 Region 需要的不是线程,**是"被周期性地调一下 `tick()` 和处理 ready"**。这两件事,完全可以**用一个线程批量做多个 Region**:

- 一个线程,持有 10 万个 Peer 的列表;
- 每 100ms,这个线程遍历它的列表,给每个 Peer 调一次 `tick()`;
- 顺便检查每个 Peer 有没有 ready,有就处理。

这就把"几十万线程"压缩成了"几个线程"。但这又引出新问题:**如果一个 Region 特别忙(比如热点),它会不会独占这个线程,饿死同线程的其他 Region?** 这就是 P1-04 batch-system 要解决的核心问题。

> **钉死这件事**:朴素 multi-Raft 不可行的根因,不是 Raft 算法慢,而是**"线程"这个对象太贵,不该一对一绑到 Region 上**。解法的方向只有两个词:**时间复用**(少量线程轮询多个 Region)+ **事件驱动**(Region 有事才处理,没事不空转)。下一节的几个具体思路,都是这两个词的变体。

---

## 四、解法雏形:从"一 Region 一线程"到"少量线程批量轮询"

朴素做法破产之后,TiKV(以及所有 multi-Raft 实现)都往同一个方向走。本章只把雏形立起来,具体落地是 P1-04 的招牌内容。

### 雏形一:时间片轮转——一个线程,一批 Region

最直接的改进:不要 1:1,改成 1:N。一个 Raft 线程持有一批 Peer(比如 1 万个),周期性地把它们都 `tick()` 一遍。Raft 的 tick 不需要精确的实时性——election timeout 是 10 个 tick(`raftstore` 默认 `raft_election_timeout_ticks = 10`),早晚几十毫秒无所谓,只要"平均频率"对就行。这正好契合"批量轮询"——一次循环里处理一大批,tick 之间有抖动但平均频率达标。

```
  一个 Raft 线程(管理 1 万个 Peer)
  循环:
   ┌─────────────────────────────────────────────┐
   │ sleep 一小段                                 │
   │ for peer in my_peers:                        │
   │     peer.raft_group.tick()                   │  ← 一次 tick 一万个
   │     if peer.raft_group.has_ready():          │
   │         handle_ready(peer)                   │  ← 顺手处理 ready
   └─────────────────────────────────────────────┘
```

把几十万 Peer 分给几十个线程,每个线程管 1 万个——几十个线程,完全可控。这是从"百万线程"到"几十线程"的第一次跃迁。

### 雏形二:不要"空转"——Region 没事就别理它

但时间片轮转有个浪费:大多数 Region 大多数时间是空闲的(没有写请求、没有消息)。让一个线程周期性地把 1 万个 Peer 都 tick 一遍,其中 9999 个的 tick 啥也没干(election timeout 没到、没消息)。这些空 tick 虽然单次很快,但乘以几十万规模,累积起来仍是可观的 CPU 浪费。

改进:**只推进有事的 Region**。怎么知道哪个 Region 有事?靠**消息驱动**——某个 Region 收到了一条 Raft 消息(比如 follower 收到 leader 的 append),就把它标记为"有事",放进一个待处理队列;Raft 线程从队列里批量取出"有事的 Region"来处理,空闲的 Region 不进队列、不被理。

这就是 TiKV **hibernate**(P2-07 招牌)的雏形:**空闲 Region 的 leader 进入休眠,不发心跳、不被 tick,省 CPU;有事(收到消息/写请求)才被唤醒**。在 hibernate 开启时(`config.rs#L617` `hibernate_regions: true` 默认开),几十万 Region 里只有少数活跃的被反复推进,绝大多数在睡觉——CPU 占用从"满载"降到"几乎为零"。

### 雏形三:不光 tick 批量,所有 I/O 都批量

`tick()` 的批量只是开始。Raft 的每个动作,都可以批量:

- **批量 propose**:一个 Raft 线程一轮里收到 100 条写请求,不要 propose 一条就 append 一条日志,而是**攒一批一起 propose**(对应 raftstore 的 `BatchRaftCmdRequestBuilder`,P2-05 拆)。
- **批量 append**:多个 Region 的 Raft 日志,一次 RocksDB 写 / 一次 RaftEngine flush 一起落盘(对应 P2-06 RaftEngine 的批量写)。
- **批量 send**:多个 Region 要发给同一个对端 store 的 Raft 消息,合并成一个网络包发(对应 raftstore 的 `transport`,P2-05)。

"批量"是 multi-Raft 性能的命根子——单个 Raft 组的开销(一次 propose、一次 append、一次 send)是固定的,但**把它们攒成一批,均摊到每条的开销就降一个数量级**。几十万个 Region 如果每个都独立 I/O,磁盘和网络会被随机小 IO 淹没;批量之后,I/O 模式变成"少量大块",吞吐高一个数量级。

> **钉死这件事**:multi-Raft 的工程核心,是把"几十万个独立小单位"的所有操作,从"逐个串行"改成"批量聚合"。tick 批量、propose 批量、append 批量、send 批量——**没有这些 batch,百万个 Raft 组即使能跑(用少量线程驱动),也会被随机小 IO 拖死**。后续每一章(P2-05 的 propose、P2-06 的 RaftEngine、P3-11 的 Apply)都会看到某种形式的 batch。

---

## 五、还剩一个硬骨头:状态怎么管

把"少量线程批量轮询"立起来之后,还剩一个不那么显眼但同样关键的问题:**每个 Peer(每个 Region 的副本)自己的状态,怎么管?**

一个 Peer 的状态包括:它的 `RawNode`(Raft 状态机)、它的 `PeerStorage`(日志/快照存储)、它当前是不是 leader、它有哪些待处理的写请求、它的 hibernate 状态、它的 mailbox(收消息用)……这些状态分散在各处、彼此关联,而且**只有当前正在处理它的那个线程能碰**(避免数据竞争)。

朴素做法是用一把大锁保护一个 Peer 的所有状态。但大锁有两个问题:① 粒度太粗,一个 Peer 的不同操作(比如收消息 + tick)本来可以并行,被锁串成串行;② 锁竞争——多个线程都要碰同一个热点 Peer 时,锁成了瓶颈。

TiKV 选了一个更优雅的抽象:**把每个 Peer 看成一个有限状态机(FSM),它的状态只能被"给它发消息"改变,而消息通过一个 mailbox 投递**。这是 actor 模型的思路——每个 Peer 是一个 actor,有自己的私有状态,线程只是"驱动 actor 处理它邮箱里的消息"。这套抽象叫 **batch-system**,是下一章 P1-04 的全部内容,也是本书招牌之一。本章只点破它的雏形:

```
  Peer(FSM)         mailbox           Raft 线程
   ┌────┐          ┌──────┐          ┌──────────┐
   │状态 │ ◀────── │消息队列│ ◀─────── │批量取出   │
   │    │          │      │          │有消息的FSM│
   │    │ ──────▶ │      │          │,驱动处理 │
   └────┘ 释放     └──────┘          └──────────┘
   (一个线程一次处理一批 FSM,而非一个 FSM 一个线程)
```

这个雏形解决了三件事:① 状态私有化(只有持有 FSM 的线程能改它,无锁);② 消息驱动(没事的 FSM 不占线程);③ 批量(一个线程一次处理一批 FSM 的消息)。具体的 `Fsm` trait、`FsmState` 三态、`Poller::poll` 循环、`Router` 路由表,全在 P1-04 拆透。

> **钉死这件事**:multi-Raft 的工程挑战,可以被拆成两个正交的问题——**① 怎么用少量线程驱动几十万个 Raft 实例(本章);② 怎么管理这几十万个实例的私有状态、让线程安全地处理它们(P1-04)**。前者的答案是"批量轮询 + 事件驱动",后者的答案是"FSM + mailbox + batch-system"。这两个答案合起来,就是"百万个 Raft 怎么共存"的完整解。

---

## 六、技巧精解:为什么是 raft-rs 而不是别的 Raft 实现

本章没有大段源码可拆(算法本体在《etcd》,batch-system 在 P1-04),但有一个值得钉死的"选型技巧":**为什么 TiKV 用 raft-rs,而不是自己从零写 Raft,或用别的现成实现?**

### 候选一:自己从零写 Raft

好处是可以完全贴合 TiKV 的需求(比如原生支持 multi-Raft)。坏处是 Raft 算法的正确性极难保证——election、log matching、safety 这些规则环环相扣,一个边界条件写错就是数据不一致。TiKV 团队评估后认为**自研 Raft 的正确性风险 > 收益**,所以选了"复用成熟实现 + 自己做 multi-Raft 编排"。

### 候选二:etcd-raft(Go)

算法最成熟、最被验证。但它是 Go 写的,没法在 Rust 项目里直接用。于是有了 raft-rs——**etcd-raft 的 Rust 移植,逻辑一一对应,可以和 etcd-raft 对照着读**。这是个聪明的选择:① 复用 etcd-raft 经过生产验证的算法逻辑;② Rust 重写拿到内存安全和性能;③ 两边逻辑同构,bug 可以交叉比对。

### 候选三:其他 Rust Raft 实现(如 openraft)

openraft 等更新、更"现代"的 Rust Raft 库存在,但它们大多**自带运行时**(自己管线程、定时器),这就回到了本章第二节那个问题——"自带运行时"的库不适合 multi-Raft,因为它会为每个实例分配线程/定时器资源,几十万个实例就爆了。raft-rs 的"纯逻辑、无运行时"形态,恰恰是 multi-Raft 需要的。

> **钉死这件事**:TiKV 选 raft-rs,本质是选了一个**"算法正确性已被 etcd 验证"+"无运行时可被自由编排"+"Rust 内存安全"** 三者兼得的库。这个选型决定了后续整个 batch-system 的设计——因为 Raft 库不自带运行时,所以 TiKV 可以用自己的 FSM + actor 模型来编排几十万个实例。**选型即架构**,raft-rs 的"纯逻辑"形态,是 multi-Raft 这座大厦的地基。

---

## 七、章末小结

### 回扣主线

本章服务**复制层**:它把"一个 Raft 库实例扩展成几十万个"这个 etcd 从没遇到过的工程问题,清晰地摆了出来。Region(上一章)是"被复制的单位",而本章告诉你"这些单位该怎么共存"——答案的雏形是"少量线程 + 批量轮询 + 事件驱动 + FSM 状态隔离",具体落地在下一章。**这一章是从 P1-02 的"静态单位"过渡到 P1-04 的"动态编排"的桥梁**。

一句话总结:Raft 算法本体承接《etcd》不重复;raft-rs 是个"纯逻辑、无运行时"的库,几十万个 `RawNode` 实例可以共享同一批线程/网络/磁盘;朴素地"每 Region 一个线程"会以栈内存、上下文切换、cache 抖动三种方式爆掉;解法方向是"少量线程批量轮询 + 事件驱动 + FSM",具体是 P1-04 的 batch-system。

### 五个为什么

1. **为什么本章不重新讲 Raft 选主/日志?**——那是《etcd》那本的活,本书三重承接第一条就是 Raft 承接《etcd》;本书只讲"百万个 Raft 怎么共存",这是 etcd 没遇到的问题。
2. **为什么 raft-rs 的"纯逻辑、无运行时"形态对 multi-Raft 至关重要?**——它让几十万个 Raft 实例可以共享同一批线程/网络/磁盘/时钟,而不是每个实例独占一份资源;自带运行时的 Raft 库做不到这点。
3. **为什么"每 Region 一个线程"不可行?**——栈内存(几十万 × 8MB 地址空间 + 实际驻留)、上下文切换(每秒几十万次)、cache 抖动(工作集远超 cache)三重爆炸;这不是"慢"是"起不来"。
4. **为什么 multi-Raft 必须批量?**——单个 Raft 组的 propose/append/send 开销固定,几十万个独立小 IO 会淹没磁盘和网络;批量(tick batch、propose batch、append batch、send batch)把均摊开销降一个数量级。
5. **为什么状态管理也要换思路?**——几十万个 Peer 的私有状态,用大锁保护会粒度太粗+锁竞争;用 FSM + mailbox(actor 模型)让状态私有化、消息驱动、无锁,这是 P1-04 batch-system 的核心。

### 想继续深入往哪钻

- **Raft 算法本体**(选主/日志/提交/安全/成员变更):读《etcd》那本,本书不重复。
- **raft-rs 的 `RawNode` 接口**:读 TiKV 的使用点 `components/raftstore/src/store/peer.rs`(`peer.rs#L728` 的 `raft_group: RawNode<PeerStorage<EK, ER>>` 字段,`peer.rs#L1011` 的 `RawNode::new` 调用);raft-rs 源码在 GitHub `tikv/raft-rs`(本地未 vendored,逻辑同 etcd-raft)。
- **multi-Raft 的工程综述**:读 TiDB 官方博客 "TiKV 的多 Raft 实现"(注意老博客讲单线程 raftstore,本书以经典 raftstore 为主线、v2 对照)。
- **承接关系**:本章是 P1-02(Region)到 P1-04(batch-system)的桥梁;Raft 算法本体承接《etcd》,单机存储(Raft 日志怎么存)承接《LevelDB》(具体在 P2-06 RaftEngine 拆,会讲为什么不用 RocksDB 存日志了)。

### 引出下一章

雏形立起来了:少量线程批量轮询、事件驱动、FSM 状态隔离。但这套雏形怎么变成真实代码?一个线程怎么批量取出"有消息的 FSM"、怎么驱动它们处理消息、怎么在热点 FSM 和空闲 FSM 之间公平调度?这就是下一章 P1-04——**batch-system + FSM:一个线程池驱动百万 Peer**,本书招牌之一,深度会吃到源码级。

> **下一章**:[P1-04 · batch-system + FSM:一个线程池驱动百万 Peer](P1-04-batch-system-FSM-一个线程池驱动百万Peer.md)
