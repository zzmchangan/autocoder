# 第 5 篇 · 第 17 章 · TSO:全局单调递增的时间戳(全书招牌章)

> **核心问题**:前一章我们讲了 PD 管 TSO,但只停在"客户端怎么取"。这一章正面拆这个全书最硬核的问题:**PD 怎么凭一个单点(leader),给整个集群的事务发出"全局唯一、全局单调递增"的时间戳?** 以及 TiKV 侧的镜像问题——**`resolved_ts` 怎么算出"这个时间点之前的数据都稳定了"的安全点**(给 GC 和事务读用)。这两件事,一个是"发时间戳",一个是"用时间戳定安全点",它们一起构成了 TiKV 事务层的"时间轴"。

> **读完本章你会明白**:
> 1. TSO 为什么是"物理时钟(毫秒)左移 18 位 + 逻辑计数器"的混编——物理时钟保证大致跟墙钟走、逻辑计数器保证同一毫秒内能发很多个、18 位是物理/逻辑的精确切分(`TSO_PHYSICAL_SHIFT_BITS = 18`,源码事实)。
> 2. PD leader 切换时,凭什么新 leader 发的时间戳不会比老 leader 小——这是 TSO 正确性的命脉(否则全局单调性就破了),靠的是"leader 切换 = 物理时钟跳进 + 计数器重置 + 一段保护期"。
> 3. TiKV 侧的 `resolved_ts`(`components/resolved_ts/`):它不是 PD 发的,而是 **TiKV 自己算**的——"我这个 Region 上所有未提交事务的最小 `start_ts`,再加上 PD 给的 `min_ts`,取两者最小"。读到 `resolved_ts` 之前的版本,绝不会撞上未提交事务。
> 4. `resolved_ts` 推进时为什么要 `check_leader`(向其他 TiKV 发 `CheckLeaderRequest` 确认 leader 身份)——这是为了防止"我已经不是 leader 了但还在发 resolved_ts",导致读到 stale 数据。

> **如果一读觉得太难**:先只记住三件事——① TSO = 物理时钟(毫秒)<< 18 + 逻辑计数器,PD leader 集中发;② leader 切换时,新 leader 会把物理时钟往后跳一段再开始发,保证不倒退;③ `resolved_ts` 是 TiKV 侧算的安全点 = min(本 Region 最小未提交 `start_ts`, PD 给的 `min_ts`),它告诉读"这个点之前都安全"。

---

## 〇、一句话点破

> **TSO 把"全局单调递增"这个看似需要全局锁的难题,拆成了"物理时钟负责粗粒度推进、逻辑计数器负责细粒度并发、PD leader 单点负责全局唯一"三层——每一层都简单,三层叠起来就能在单点上扛住百万 QPS 还不丢单调性。而 `resolved_ts` 是 TiKV 侧的"逆向工程":它从 PD 拿一个 `min_ts`,再扫自己 Region 上的锁,两者取最小,算出"这个点之前的数据肯定都稳定了"。**

这是结论,不是理由。本章倒过来拆:先讲为什么"全局单调递增"在分布式系统里是个真问题(比想象中难),再拆 TSO 的三层混编(物理时钟 + 逻辑计数器 + 单 leader),接着拆 leader 切换的正确性(最容易翻车的地方),然后落到本地源码——`pd_client/tso.rs` 怎么取、`resolved_ts/` 怎么算,最后是 GC/CDC/stale read 怎么用 `resolved_ts`。

---

## 一、为什么"全局单调递增的时间戳"是个真问题

读到这一篇,你已经知道 MVCC 靠时间戳定版本序(一个 key 的多个版本按 `commit_ts` 排,读时看 `commit_ts ≤ start_ts` 的最大版本),Percolator 靠时间戳判定事务先后(`commit_ts` 大的晚于 `commit_ts` 小的)。这套机制有个隐含前提:**时间戳必须全局单调递增**。否则:

- **两个事务拿到一样的时间戳**:A 事务 `start_ts = 100`,B 事务也 `start_ts = 100`(并发拿到),A 提交了 `commit_ts = 101`,B 用 `start_ts = 100` 读——按 MVCC 规则,B 应该看不到 A(commit_ts 101 > start_ts 100),但**如果 B 和 A 的 start_ts 撞了,版本序就乱了**,隔离性可能破。
- **后拿的事务拿到更小的时间戳**:A 先拿了 `ts = 100`,B 后拿了 `ts = 99`(因为某处倒退了)。B 用 `ts = 99` 去读,反而看到了 A 之前的状态——**"后开始的事务看到更早的状态"**,这违反了线性一致性(linealizability)。

所以 TSO 的承诺非常硬:**全局唯一、全局单调递增、不倒退**。这个承诺,在分布式系统里实现起来比想象中难得多。

### 朴素做法会撞什么墙

**朴素做法一:每台机器用本地墙钟(`System.currentTimeMillis()`)当时间戳。** 立刻坏掉——机器之间的墙钟不可能完全同步(哪怕 NTP,也有几十毫秒误差)。机器 A 的墙钟比机器 B 快 50ms,那 A 发的时间戳就比 B 大,但 A 可能物理上更早——**单调性和真实时间脱钩**。更糟的是,墙钟可能**回跳**(NTP 校时、闰秒),一发时间戳倒退,MVCC 直接乱套。

**朴素做法二:每台机器自己维护一个计数器,定期和别的机器同步。** 也坏——同步有延迟,同步期间两台机器各自推计数器,可能产生重叠的时间戳。要让两台机器的计数器"全局同步",本质上需要一个全局共识——**而这正是 PD 要解决的**。

> **不这样会怎样**:如果不强制"全局单点发时间戳",无论怎么设计(本地墙钟、本地计数器 + 同步、逻辑时钟 Lamport),都无法同时保证"全局唯一 + 全局单调 + 不倒退"。Lamport 时钟保证偏序但不保证和物理时间相关(对人工排查极不友好);向量时钟太重;TrueTime(Google Spanner 用的,靠原子钟 + GPS)需要专用硬件。TiKV 选了一个朴素但可靠的答案——**一个中心点(PD leader)集中发,物理时钟 + 逻辑计数器混编**。

### 为什么不用 Google Spanner 的 TrueTime

值得专门讲一句,因为很多人会问。Google Spanner 用 TrueTime——每个数据中心装原子钟和 GPS 接收器,API 返回 `[earliest, latest]` 一个区间,保证真实时间落在这个区间内。Spanner 据此做"等待真实时间确定过去"(commit wait),实现外部一致性。这看起来比 TSO 高级——**去中心化**(没有单点 PD),**真正的物理时间**(误差有界)。

但 TrueTime 有个硬要求:**专用硬件(原子钟/GPS)**。这对你能跑在普通云服务器、自建机房的 TiKV 来说,门槛太高。TiKV 选了一个朴素的替代:**牺牲一点精度(单点 PD 是瓶颈),换"能在普通硬件上跑"**。这个取舍的核心是——**大多数业务场景下,PD 单点 + 批量预分配已经够用**(后面拆),TrueTime 的硬件门槛不划算。

> **钉死这件事**:TSO 的设计是个**工程取舍**——它没有 TrueTime 的硬件精度,也没有 Lamport 的去中心化,但它**简单、可靠、能在普通硬件上跑**。代价是 PD leader 是单点(用 Raft 高可用缓解)、所有事务都要过 PD(用客户端批量缓解)。这是 TiKV 在"理论优雅"和"工程现实"之间选了后者。

---

## 二、TSO 的三层混编:物理时钟 + 逻辑计数器 + 单 leader

TSO 的核心机制,可以拆成三层。三层都很简单,但叠在一起恰好满足"全局唯一 + 全局单调 + 不倒退 + 高并发"。

### 第一层:PD leader 单点——保证全局唯一

这是最底层、最朴素的一层:**任意时刻只有一个 PD leader 在发时间戳**。客户端(TiKV)所有 `get_tso` 请求都发给 leader(前一章讲过,`pd_client` 初始化时会找到 leader 地址)。因为只有一个点在发,**全局唯一性天然保证**——不存在两个 PD 节点同时发时间戳撞车的问题。

> **PD 服务端逻辑在 pd 仓**:`tikv/pd` 仓的 `server/tso/` 实现了 leader 选举(用 etcd 的 Raft 库)、时间戳分配算法。**不在本地 clone 的 tikv 仓**。本章讲清原理(可对照 pd 官方源码),重点拆本地能看到的 tikv 侧协作(`pd_client/tso.rs` 取、`resolved_ts/` 用)。

### 第二层:物理时钟 + 逻辑计数器——保证单调和不倒退

单 leader 解决了"唯一",但没解决"单调递增"。朴素地在 leader 上用一个全局自增计数器也行(每个请求 +1),但有两个问题:① leader 重启,计数器归零,时间戳倒退(灾难);② 时间戳和物理时间无关,人工排查(看一个时间戳猜这是几点几分发的)极不友好。

TSO 的答案是**物理时钟 + 逻辑计数器混编**:

- **物理部分(physical)**:PD leader 的本地墙钟,毫秒精度。每次发时间戳,先看当前墙钟。
- **逻辑部分(logical)**:同一毫秒内,每发一个时间戳,逻辑计数器 +1。下一毫秒,逻辑计数器清零,物理部分更新。

时间戳的编码是 `physical << 18 + logical`(本地 `txn_types/src/timestamp.rs` 的 `compose`):

```rust
// components/txn_types/src/timestamp.rs#L14-L25
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq, Ord, PartialOrd, Hash)]
#[repr(transparent)]
pub struct TimeStamp(u64);

pub const TSO_PHYSICAL_SHIFT_BITS: u64 = 18;

impl TimeStamp {
    /// Create a time stamp from physical and logical components.
    pub fn compose(physical: u64, logical: u64) -> TimeStamp {
        TimeStamp((physical << TSO_PHYSICAL_SHIFT_BITS) + logical)
    }

    /// Extracts physical part of a timestamp, in milliseconds.
    pub fn physical(self) -> u64 {
        self.0 >> TSO_PHYSICAL_SHIFT_BITS
    }

    /// Extracts logical part of a timestamp.
    pub fn logical(self) -> u64 {
        self.0 & ((1 << TSO_PHYSICAL_SHIFT_BITS) - 1)
    }
    ...
}
```

注意几个关键细节:

1. **`TimeStamp` 是个 `u64` 的透明包装**(`#[repr(transparent)]`),也就是个 64 位整数。整个 TiKV 的时间戳就是一个 64 位数。
2. **左移 18 位**:物理部分(毫秒)占高 46 位,逻辑部分占低 18 位。`2^18 = 262144`,也就是**同一毫秒内最多能发 262144 个时间戳**——这对单 PD leader 来说绰绰有余(单 leader 的网络/CPU 处理能力远到不了这个上限)。
3. **物理部分 46 位**:`2^46` 毫秒 ≈ 35 年,够 TiKV 用几十年。

```
   一个 TSO 时间戳(64 位 u64)的内部结构
   ┌────────────────────────────┬──────────────────────────┐
   │  物理部分(高 46 位,毫秒)  │  逻辑部分(低 18 位,计数器) │
   │  ≈ 35 年                   │  ≈ 26 万/毫秒             │
   └────────────────────────────┴──────────────────────────┘
   compose: ts = (physical << 18) + logical
   比较:    两个 ts 直接按 u64 比较(先比 physical,再比 logical)
```

> **钉死这件事**:这个 18 位的切分是个精心调过的甜点——逻辑部分够大(每毫秒 26 万,扛住突发并发),物理部分够长(35 年,够用)。整个 TiKV 用一个 64 位数同时编码"时间"和"序",**直接按 u64 比较就是时间序**(因为 physical 在高位)。这个编码的精妙在于:**它把"全局单调递增"这件事,变成了 u64 数字的递增**——简单到不可能错。

### 为什么是"物理 + 逻辑"而不是单纯计数器

你可能会问:既然 PD 是单 leader,为什么不用一个单纯的全局自增计数器(每发一个 +1)?那样不也是全局单调递增吗?

技术上可以,但有两个实际问题:

1. **持久化和恢复**:leader 重启或切换,计数器必须从持久化的地方恢复。如果用单纯计数器,每次发时间戳都要持久化(否则崩溃后倒退),性能扛不住;不每次持久化,崩溃后会倒退(灾难)。**物理时钟 + 逻辑计数器**巧妙地用"墙钟天然不倒退"解决这个——leader 重启后,重新读墙钟,物理部分自然前进(因为时间过去了),逻辑部分清零,新发的时间戳必然比崩溃前大(只要崩溃期间墙钟走了哪怕 1ms)。
2. **可观测性**:单纯计数器的时间戳是一串没意义的数字(比如 `12345678`),人工排查时无法判断这是几点发的。**物理时钟的时间戳能直接还原成墙钟时间**(`ts.physical()` 得到毫秒时间戳,可格式化成日期),这对调试和监控极有价值。TiKV 的很多日志、metric 都用 `physical` 还原成可读时间。

> **不这么设计会怎样**:用单纯计数器,leader 重启时要么每次发都 fsync 持久化(性能崩),要么冒着倒退的风险(正确性破)。物理时钟 + 逻辑计数器,把"不倒退"的保证从"靠持久化"变成了"靠时间本身的单向性",这是个聪明的卸载——**把正确性的一部分责任,从存储层转移到了物理定律**(时间不会倒流)。

### 第三层:批量预分配——扛住高并发

单 leader + 物理/逻辑混编,正确性够了,但性能不够——每秒 10 万事务,每个都过 PD leader 一次,leader 处理不过来。这就是前一章讲的**客户端批量**(`pd_client/tso.rs` 的 `TimestampOracle`):客户端攒一批(最多 64 个)再发一次 RPC。这里再补充一个**服务端侧的批量**:

PD leader 收到一个 `TsoRequest(count=N)`,不是逐个分配 N 个时间戳,而是**一次性把逻辑计数器推进 N**:当前 logical 是 L,推到 L+N,返回 `(physical, L+N, count=N)`。客户端拿到后自己分:[L+1, L+N]。这意味着 **PD 处理一个批量请求的开销,和单个请求几乎一样**——所以批量越大,PD 单位时间能服务的总事务数越多。

```rust
// components/pd_client/src/tso.rs#L207-L237(简化)
fn allocate_timestamps(
    resp: &TsoResponse,
    pending_requests: &mut VecDeque<RequestGroup>,
) -> Result<()> {
    // PD 返回最大的 logical 值(tail_ts.logical)
    let tail_ts = resp.timestamp.as_ref().ok_or_else(...)?;
    let mut offset = resp.count;   // 这一批总共 N 个
    if let Some(RequestGroup { tso_request, requests }) = pending_requests.pop_front() {
        // 把 [logical - offset + 1, logical] 范围内的时间戳分发给这一组的请求
        for request in requests {
            offset -= request.count;
            let physical = tail_ts.physical as u64;
            let logical = tail_ts.logical as u64 - offset as u64;
            let ts = TimeStamp::compose(physical, logical);
            let _ = request.sender.send(ts);
        }
    }
    Ok(())
}
```

注意分发逻辑——PD 返回的是**这一批里最大的 logical**(tail),客户端按请求顺序**从最大值倒着减**来分配。这保证了:**同一批里的多个请求,每个拿到的都是不重叠的、连续递增的时间戳**。

> **钉死这件事**:TSO 的三层混编,**每一层都解决一个具体问题**:单 leader 解决唯一性、物理+逻辑解决不倒退、批量解决性能。这三层叠起来,就是 TiKV 在普通硬件上实现"全局单调递增时间戳"的全部秘诀。没有 TrueTime 的原子钟,没有 Paxos 的复杂,就是**朴素的三层叠加**——但每一层都做对了,合起来就是正确的。

---

## 三、最易翻车的地方:PD leader 切换时凭什么不倒退

这一节是 TSO 正确性的命脉,也是面试最常考的"刁钻问题":**PD leader 切换(老 leader 挂了,新 leader 上任)时,新 leader 发的时间戳,凭什么不会比老 leader 小?**

### 问题为什么棘手

考虑这个场景:

1. 老 leader 在物理时间 T=1000ms,逻辑计数器 L=5000,刚发了一批时间戳(最大到 `compose(1000, 5000)`)。
2. 老 leader 挂了(网络分区或宕机)。
3. 新 leader 上任(剩 2 个 PD 节点选出新 leader)。新 leader 读自己的墙钟,T=1005ms(时间过去了 5ms)。
4. **问题**:新 leader 怎么知道老 leader 已经发到了 `compose(1000, 5000)`?如果新 leader 从自己的墙钟 T=1005ms、逻辑计数器 L=0 开始发,那它发的第一个时间戳是 `compose(1005, 0)`——**这比老 leader 最后发的 `compose(1000, 5000)` 大吗?**

`compose(1005, 0) = 1005 << 18 = 1005 * 262144 = 263554560`
`compose(1000, 5000) = 1000 * 262144 + 5000 = 262144000 + 5000 = 262149000`

263554560 > 262149000,所以**只要新 leader 的墙钟比老 leader 的墙钟晚 ≥ 1ms,新 leader 从 L=0 开始发,时间戳就一定比老 leader 大**。

但这有个隐患——**如果老 leader 的墙钟比真实时间快(比如快了 50ms),新 leader 的墙钟比真实时间准**。老 leader 在 T=1050ms(墙钟快了)发了时间戳,新 leader 上任时墙钟 T=1005ms(真实时间),那新 leader 的 T=1005 < 老 leader 的 T=1050,**时间戳就倒退了**。这就是 TSO 正确性最容易翻车的地方。

### PD 的答案:leader 切换时强制"跳进"一段保护期

PD 服务端的解法(原理,具体代码在 pd 仓):

1. **新 leader 上任后,不立刻发时间戳**。先等一段时间(默认约 3 秒,可配置),让"老 leader 可能发过但还没送达"的时间戳请求都过期——这叫 **Graceful leader 选举保护期**。
2. **新 leader 重新校准物理时钟**:读自己的墙钟,但**不直接用**。它会把这个墙钟值,和老 leader 上次持久化的"最后发的 physical"做对比,**取较大值**。如果老 leader 持久化的值更大(说明老 leader 墙钟快),新 leader 用老的值 + 1ms 当起点,保证新发的时间戳物理部分必然大于老 leader。
3. **逻辑计数器清零**:物理部分更新后,逻辑计数器从 0 开始重新累加。

这套机制保证了:**无论老 leader 的墙钟多偏、网络分区多久,新 leader 发的第一个时间戳,都严格大于老 leader 发的最后一个**。代价是 leader 切换有 3 秒左右的"不可用窗口"(这期间 `get_tso` 会失败或超时)——这是 TSO 单 leader 模型不可避免的代价。

> **PD 服务端的 leader 切换代码在 pd 仓**:`server/tso/` 的 `timestamporacle.go` 实现了这套"校准 + 保护期"逻辑。**不在本地 clone 的 tikv 仓**。这里讲的是原理,你能从 PD 官方文档和源码对照。
>
> **tikv 侧的表现**:leader 切换时,tikv 的 `pd_client` 会遇到 `get_tso` 超时(前一章讲过,`REQUEST_TIMEOUT = 2` 秒)或错误,然后重试(`LEADER_CHANGE_RETRY`)。对上层事务来说,就是"这次拿时间戳慢了一点"(几秒),不影响正确性。

### leader 切换期间事务会怎样

值得讲一下用户视角的影响。leader 切换的 3 秒里,新事务的 `get_tso` 会卡住或失败。事务要么重试(乐观事务 prewrite 失败重试)、要么等待(悲观事务 acquire lock 时拿不到 start_ts)。**已经在执行中的事务不受影响**(它们已经拿到了 start_ts,后续的 commit_ts 也可以用 start_ts 推算,不强依赖实时 TSO)。

所以 PD leader 切换的体感是:**集群有 3 秒左右,新事务的延迟抖动一下,然后恢复**。这就是 TSO 单点模式的"故障窗口"——用 Raft 高可用缓解(切换自动化),但切换期间的延迟抖动不可避免。这是 TiKV 用 TSO 而非 TrueTime 的代价之一。

> **钉死这件事**:PD leader 切换的正确性,是 TSO 最易翻车也最被精心设计的地方。核心机制是"切换时校准物理时钟(取老 leader 持久化值和本地墙钟的较大者)+ 保护期(等过期请求落地)+ 逻辑计数器清零"。这套保证了"新 leader 的时间戳严格大于老 leader",代价是 3 秒切换窗口。**没有这套保护,leader 切换时时间戳倒退,MVCC 立刻乱套**——这就是为什么 TSO 的正确性论证这么讲究。

---

## 四、tikv 侧怎么取 TSO:`pd_client/tso.rs` 全流程

前面三章讲的是 PD 服务端的原理(在 pd 仓)。这一节落到本地源码,拆 tikv 侧怎么取 TSO。前一章已经讲了 `TimestampOracle` 的批量机制(技巧精解),这里补全细节。

### `TimestampOracle` 的双 future 架构

```rust
// components/pd_client/src/tso.rs#L55-L95(简化)
pub struct TimestampOracle {
    request_tx: mpsc::Sender<TimestampRequest>,   // 本地 channel
    close_rx: watch::Receiver<()>,
}

struct TimestampRequest {
    sender: oneshot::Sender<TimeStamp>,   // 拿到时间戳后通过这个回送
    count: u32,                           // 这个请求要几个时间戳
}
```

`TimestampOracle::new` 起一个后台线程,跑两个并发的 future(前章拆过):

- **`send_requests`**:从 `request_rx` 攒一批(最多 `MAX_BATCH_SIZE = 64`),合成一个 `TsoRequest` 发给 PD。
- **`receive_and_handle_responses`**:收 `TsoResponse`,调用 `allocate_timestamps` 分发。

两个常量值得记:

```rust
// components/pd_client/src/tso.rs#L39-L41
const MAX_BATCH_SIZE: usize = 64;        // 一次最多攒 64 个请求
const MAX_PENDING_COUNT: usize = 1 << 16; // 最多 65536 个请求在飞(等响应)
```

`MAX_PENDING_COUNT` 是个**背压机制**——如果 PD 太慢,飞行中的请求堆积到 65536,`TsoRequestStream` 就不再从 channel 捞新请求,直到有响应回来腾出位置(通过 `sending_future_waker` 唤醒)。这防止了 PD 卡顿时 tikv 侧内存爆炸。

### `get_timestamp` 怎么用

```rust
// components/pd_client/src/tso.rs#L98-L113(简化)
pub(crate) fn get_timestamp(
    &self, count: u32,
) -> impl Future<Output = Result<TimeStamp>> + 'static {
    let (request, response) = oneshot::channel();   // 建一个 oneshot channel
    let request_tx = self.request_tx.clone();
    async move {
        // 把 TimestampRequest(含 sender)塞进 channel,后台线程会攒批
        request_tx.send(TimestampRequest { sender: request, count }).await
            .map_err(|_| -> Error { box_err!("TimestampRequest channel is closed") })?;
        // 等后台线程把时间戳送回来
        response.await.map_err(|_| box_err!("Timestamp channel is dropped"))
    }
}
```

整个流程,从事务视角看:

1. 事务调用 `pd_client.get_tso()`(本质 `batch_get_tso(1)`)。
2. `batch_get_tso` 把请求塞进 `TimestampOracle` 的 channel(带 count=1)。
3. 后台 TSO worker 线程攒批,攒够 64 个(或超时)发一个 `TsoRequest(count=64)` 给 PD。
4. PD 返回 `TsoResponse { physical, logical, count=64 }`。
5. worker 把这 64 个时间戳按顺序分给 64 个等待的请求(通过 oneshot sender)。
6. 事务的 `get_tso` future 就绪,拿到时间戳。

> **钉死这件事**:tikv 侧取 TSO 的核心是**"事务不直接调 PD,而是塞 channel 等回送"**。这个解耦让 tikv 能在客户端做批量(把 64 个独立请求合并成 1 次 RPC),是 TSO 高并发的关键。整个机制没有任何 PD 服务端代码(那在 pd 仓),全是 tikv 侧的客户端优化。

---

## 五、`resolved_ts`:tikv 侧的安全点算法

讲完"怎么取 TSO",来到本章第二大主题——**`resolved_ts`**。它是 TiKV 侧(不是 PD)算出来的"安全点",对 GC、CDC、stale read 都关键。

### 什么是 `resolved_ts`,为什么需要它

先定义清楚:**`resolved_ts` 是一个时间戳,表示"这个 Region 上,所有 `commit_ts ≤ resolved_ts` 的事务,都肯定已经提交或回滚了"**。换句话说,`resolved_ts` 之前的版本,都是稳定的、不会被未提交事务修改的。

为什么需要这个?三个场景:

1. **GC(垃圾回收)**:MVCC 会堆积海量老版本,GC 要清掉。但 GC 不能清"还在进行中的事务可能用到的版本"——GC safe point 必须保证"这个点之前的事务都结束了"。`resolved_ts` 就是这个保证的依据。
2. **CDC(Change Data Capture)**:CDC 要把 TiKV 的变更实时推给下游。它需要知道"我已经把 `ts ≤ X` 的所有变更都推完了",才能告诉下游"你看到的数据是一致到 X 这个时间点的"。`resolved_ts` 就是这个 X。
3. **Stale Read(过期读)**:某些场景允许读"稍微旧一点"的数据(比如读 1 秒前的,换取跨 Region 不阻塞)。stale read 需要保证"读 `ts ≤ X` 的版本,不会撞上未提交事务"。`resolved_ts` 提供这个保证。

注意 `resolved_ts` 是**per-Region** 的——每个 Region 有自己的 `resolved_ts`,因为每个 Region 上的事务进度不同。一个 TiKV 上有几十万个 Region,就有几十万个 `resolved_ts`。

### `resolved_ts` 怎么算:`Resolver`

`resolved_ts` 的计算核心是 `components/resolved_ts/src/resolver.rs` 的 `Resolver` 结构(每个 Region 一个):

```rust
// components/resolved_ts/src/resolver.rs#L85-L115(简化,只留关键字段)
pub struct Resolver {
    region_id: u64,
    // 这个 Region 上当前未提交事务的锁,key → start_ts
    locks_by_key: HashMap<Arc<[u8]>, TimeStamp>,
    // 按 start_ts 排序的锁堆(BTreeMap,方便找最小 start_ts)
    lock_ts_heap: BTreeMap<TimeStamp, TxnLocks>,
    large_txns: HashMap<TimeStamp, TxnLocks>,
    // The timestamps that guarantees no more commit will happen before.
    resolved_ts: TimeStamp,          // 当前的 resolved_ts
    // The highest index `Resolver` had been tracked
    tracked_index: u64,
    min_ts: TimeStamp,
    stopped: bool,
    ...
}
```

关键算法在 `resolve` 方法:

```rust
// components/resolved_ts/src/resolver.rs#L415-L485(简化,保留核心逻辑)
pub fn resolve(
    &mut self,
    min_ts: TimeStamp,         // PD 给的 min_ts(全局推进)
    now: Option<Instant>,
    source: TsSource,
) -> TimeStamp {
    if self.stopped {
        return self.resolved_ts;
    }

    // 找这个 Region 上所有未提交事务里,最小的 start_ts
    let min_lock = self.oldest_transaction();
    let has_lock = min_lock.is_some();
    let min_txn_ts = min_lock.as_ref().map(|(ts, _)| *ts).unwrap_or(min_ts);

    // resolved_ts = min(最小未提交 start_ts, PD 给的 min_ts)
    // 含义:这个值之前,肯定没有未提交事务了
    let new_resolved_ts = cmp::min(min_txn_ts, min_ts);

    // Resolved ts never decrease. (只增不减)
    self.resolved_ts = cmp::max(self.resolved_ts, new_resolved_ts);

    // 发布到 RegionReadProgress,供 stale read 用
    if let Some(rrp) = &self.read_progress {
        rrp.update_safe_ts_with_time(self.tracked_index, self.resolved_ts.into_inner(), now);
    }
    ...
    self.resolved_ts
}
```

这段代码是 `resolved_ts` 算法的精华。逐步拆:

1. **`min_ts`(PD 给的)**:这是 `advance_ts_for_regions` 里从 PD 取的 `get_tso()`(见 `advance.rs`)。它代表"当前全局最新时间"——PD 保证之后发的时间戳都 > 这个值。
2. **`oldest_transaction()`**:扫 `lock_ts_heap`(按 start_ts 排序的锁堆),找**这个 Region 上当前未提交事务里最小的 start_ts**。如果有这么个事务(它的 start_ts 是 S),那么 `resolved_ts` 不能超过 S——因为这个事务随时可能 commit(commit_ts > S 但 ≤ ... ),S 之后的版本都不"稳定"。
3. **`new_resolved_ts = min(min_txn_ts, min_ts)`**:取两者最小。这个公式是精髓——
   - 如果 Region 上没锁(`has_lock = false`),`min_txn_ts = min_ts`,`new_resolved_ts = min_ts`(跟着 PD 推进)。
   - 如果 Region 上有锁(`has_lock = true`,最小锁 start_ts = S),`new_resolved_ts = min(S, min_ts) = S`(被未提交事务卡住,resolved_ts 停在 S)。
4. **`resolved_ts = max(resolved_ts, new_resolved_ts)`**:只增不减。这是个**单调性保证**——`resolved_ts` 一旦推进,就不会倒退(哪怕后来算出的 new_resolved_ts 更小,也取较大的旧值)。

> **钉死这件事**:`resolved_ts = min(本 Region 最小未提交 start_ts, PD 给的 min_ts)`,且只增不减。这个公式把"这个 Region 上哪些数据稳定了"这个问题,归约成了"找最小未提交 start_ts"——只要扫一遍锁堆就能算出。锁清掉了(事务 commit/cleanup),最小 start_ts 推进,`resolved_ts` 就能往前走。

### 锁怎么 track:从 raftstore 的 apply 流监听

`Resolver` 怎么知道 Region 上有哪些锁?它不是主动扫 RocksDB,而是**订阅 raftstore 的 apply 流**——每当 apply 一条 Raft 命令(可能是 prewrite 写的 lock、commit 清的 lock),resolved_ts 的 observer 就把这条 change log 推给 `Resolver`:

```rust
// components/resolved_ts/src/resolver.rs#L309-L342(简化,track_normal_lock)
pub fn track_lock(
    &mut self,
    start_ts: TimeStamp,
    key: Vec<u8>,
    index: Option<u64>,
    generation: u64,
) -> Result<(), MemoryQuotaExceeded> {
    if let Some(index) = index {
        self.update_tracked_index(index);
    }
    if generation == 0 {
        self.track_normal_lock(start_ts, key)?;
    } else {
        self.track_large_txn_lock(start_ts, key)?;
    }
    Ok(())
}

fn track_normal_lock(
    &mut self, start_ts: TimeStamp, key: Vec<u8>,
) -> Result<(), MemoryQuotaExceeded> {
    let bytes = self.lock_heap_size(&key);
    self.memory_quota.alloc(bytes)?;        // 内存配额控制
    let key: Arc<[u8]> = key.into_boxed_slice().into();
    match self.locks_by_key.entry(key) {
        HashMapEntry::Occupied(_) => { self.memory_quota.free(bytes); }
        HashMapEntry::Vacant(entry) => {
            // 同时维护 locks_by_key(key→ts)和 lock_ts_heap(ts→TxnLocks)
            let txn_locks = self.lock_ts_heap.entry(start_ts).or_insert_with(|| {
                let mut txn_locks = TxnLocks::default();
                txn_locks.sample_lock = Some(entry.key().clone());
                txn_locks
            });
            txn_locks.lock_count += 1;
            entry.insert(start_ts);
        }
    }
    Ok(())
}
```

注意两个数据结构的双索引:

- **`locks_by_key: HashMap<key, start_ts>`**:O(1) 查"这个 key 上的锁是哪个事务的",用于 untrack(锁清掉时快速删除)。
- **`lock_ts_heap: BTreeMap<start_ts, TxnLocks>`**:按 start_ts 排序,O(1) 取最小 start_ts(就是 `lock_ts_heap.first_key()`),用于算 `resolved_ts`。

这两个结构一起维护,track 一个锁两个都加,untrack 一个锁两个都删。这是典型的"用空间换时间"——多花一倍内存,换 O(1) 查找 + O(1) 取最小。

> **钉死这件事**:`Resolver` 不扫 RocksDB,它订阅 raftstore 的 apply 流,实时 track/untrack 锁。两个数据结构(HashMap by key + BTreeMap by ts)双索引,分别服务"按 key 删锁"和"取最小 start_ts"。这个设计让 `resolved_ts` 的推进开销和锁数量成正比,而不是和 Region 数据量成正比——对大 Region 也高效。

---

## 六、`resolved_ts` 怎么推进:check_leader 的关键作用

`Resolver` 算出了 `resolved_ts`,但还有个隐患——**这个 Region 的 leader 身份还成立吗**?如果这个 TiKV 节点已经不是这个 Region 的 leader 了(刚发生 transfer leader),它还在发 `resolved_ts`,读客户端基于这个 `resolved_ts` 做 stale read,可能读到**旧 leader 上的过时数据**——这正是 stale read 正确性的大坑。

### 问题:旧 leader 的 resolved_ts 不可信

考虑这个场景:

1. Region X 的 leader 在 TiKV A,它算出 `resolved_ts = 100`,告诉客户端"你可以读 ts ≤ 100 的版本"。
2. 就在这一刻,Region X 发生 transfer leader,leader 换到 TiKV B。但 TiKV A 还不知道(网络延迟),它继续算 `resolved_ts = 101`,继续告诉客户端。
3. 客户端基于 TiKV A 的 `resolved_ts = 101` 做 stale read,但**TiKV A 已经不是 leader 了**,它上面的数据可能比 TiKV B(新 leader)旧——读到 stale 数据。

这就是为什么 `resolved_ts` 推进时,必须**确认自己还是 leader**。

### 答案:`LeadershipResolver` + `CheckLeaderRequest`

`components/resolved_ts/src/advance.rs` 的 `LeadershipResolver` 解决这个问题。它的核心方法 `resolve`(注意名字和 `Resolver::resolve` 撞了,但这是另一个)会**向 Region 的所有副本所在的 TiKV 发 `CheckLeaderRequest`,确认 quorum 还承认自己是 leader**:

```rust
// components/resolved_ts/src/advance.rs#L167-L185(简化)
pub struct LeadershipResolver {
    tikv_clients: Mutex<HashMap<u64, TikvClient>>,   // 到各 TiKV 的 gRPC 客户端
    pd_client: Arc<dyn PdClient>,
    region_read_progress: RegionReadProgressRegistry,
    store_id: u64,
    // store_id -> CheckLeaderRequest,记录要发往各 store 的请求
    store_req_map: HashMap<u64, CheckLeaderRequest>,
    progresses: HashMap<u64, RegionProgress>,
    checking_regions: HashSet<u64>,
    valid_regions: HashSet<u64>,
    ...
}
```

它的 `resolve` 方法逻辑(简化):

1. 遍历所有要推进 `resolved_ts` 的 Region,从 `region_read_progress` 拿每个 Region 的 leader 信息。
2. **如果 leader 不在本 store,跳过**(不是这个 TiKV 的事)。
3. **构造 `CheckLeaderRequest`**:包含 leader 信息(leader_id、region_epoch、peers),还有 `ts = min_ts`(当前要推进的时间戳)。
4. **并发发往所有 peer 所在的 store**(除了自己):"你们还认这个 leader 吗?认的话,你们 apply 到了哪个 index?"
5. **等 quorum 响应**:如果多数 peer 响应"还认这个 leader",这个 Region 算"valid",可以推进 `resolved_ts`。否则(没有 quorum),这个 Region 暂时不推进。

```rust
// components/resolved_ts/src/advance.rs#L362-L410(简化)
for (store_id, req) in store_req_map {
    if req.regions.is_empty() { continue; }
    let rpc = async move {
        let client = get_tikv_client(to_store, pd_client, ...).await?;
        req.set_ts(min_ts.into_inner());    // 设置要推进的 min_ts
        let rpc = client.check_leader_async(req)?;
        let resp = tokio::time::timeout(timeout, rpc).await??;
        Ok((to_store, resp))
    }.boxed();
    check_leader_rpcs.push(rpc);
}

// 用 select_all 避免某个 TiKV 挂了阻塞整个推进
for _ in 0..rpc_count {
    let (res, _, remains) = select_all(check_leader_rpcs).await;
    check_leader_rpcs = remains;
    // 处理响应,检查 quorum
}
```

注意 `select_all` 的用法——**不等所有 RPC 都回来,而是边收边判断 quorum**。如果某个 TiKV 挂了(响应慢),`select_all` 不阻塞,其他响应够了 quorum 就继续。这是个延迟优化——避免单个慢节点拖累整个 `resolved_ts` 推进。

> **钉死这件事**:`resolved_ts` 推进时的 `check_leader`,本质是**"旧 Raft quorum 确认"**——它借用 Raft 的多数派机制,确认"我发这个 resolved_ts 时,quorum 还认我是 leader"。这保证了 resolved_ts 只在"确认的 leader 任期内"推进,杜绝了旧 leader 误发 resolved_ts 导致 stale read。这个机制和 Raft 的 lease/read_index 是同源思想(承接《etcd》那本的 Raft 读)。

### `check_leader` 的双重作用:顺便推进 min_ts

`CheckLeaderRequest` 里带的 `ts = min_ts` 还有个妙用——**它顺便把 `min_ts` 同步到了 quorum 的 TiKV**。每个收到 `CheckLeaderRequest` 的 TiKV,会把自己的 `max_ts`(它见过的最大时间戳)更新到 ≥ min_ts。这保证了:**quorum 上的 TiKV 都知道"现在全局时间已经到了 min_ts"**,之后它们不会发 `start_ts < min_ts` 的新事务(否则就和已推进的 resolved_ts 撞了)。这是个**双向同步**——leader 推进 resolved_ts,顺便把 min_ts 广播给 quorum。

---

## 七、`resolved_ts` 的全流程:`advance_ts_for_regions`

把前面讲的拼起来,看 `resolved_ts` 推进的完整循环(在 `advance.rs` 的 `advance_ts_for_regions`):

```rust
// components/resolved_ts/src/advance.rs#L92-L150(简化)
pub fn advance_ts_for_regions(
    &self,
    regions: Vec<u64>,
    mut leader_resolver: LeadershipResolver,
    advance_ts_interval: Duration,
    advance_notify: Arc<Notify>,
) {
    let cm = self.concurrency_manager.clone();
    let pd_client = self.pd_client.clone();
    let timeout = self.timer.delay(advance_ts_interval);

    let fut = async move {
        // 1. 从 PD 取一个 min_ts(全局最新时间)
        let mut min_ts = pd_client.get_tso().await.unwrap_or_default();

        // 2. 更新 concurrency_manager 的 max_ts(给内存锁用)
        if let Err(e) = cm.update_max_ts(min_ts, "resolved-ts") {
            error!("failed to update max_ts in concurrency manager"; "err" => ?e);
            return;
        }
        // 3. 检查内存锁(concurrency_manager 里的),如果有锁 start_ts < min_ts,用锁的 ts
        if let Some((min_mem_lock_ts, lock)) = cm.global_min_lock() {
            if min_mem_lock_ts < min_ts {
                min_ts = min_mem_lock_ts;
                ts_source = TsSource::MemoryLock(lock);
            }
        }

        // 4. check_leader:向 quorum 确认每个 Region 的 leader 身份
        let regions = leader_resolver.resolve(regions, min_ts, Some(advance_ts_interval)).await;

        // 5. 给 valid 的 Region 调 Resolver::resolve,推进 resolved_ts
        if !regions.is_empty() {
            scheduler.schedule(Task::ResolvedTsAdvanced {
                regions, ts: min_ts, ts_source,
            })?;
        }

        // 6. 等下一个 interval(或被 notify 唤醒),循环
        futures::select! {
            _ = timeout.compat().fuse() => (),
            _ = advance_notify.notified().fuse() => (),
        };
        // 7. 再次调度自己(自循环)
        scheduler.schedule(Task::AdvanceResolvedTs { leader_resolver })?;
    };
    self.worker.spawn(fut);
}
```

这个循环每 `advance_ts_interval`(默认 1 秒)跑一次,每次:

1. 从 PD 拿最新 `min_ts`。
2. 算上内存锁的约束(ConcurrencyManager 里的悲观锁)。
3. `check_leader` 确认 leader 身份。
4. 给 valid Region 调 `Resolver::resolve` 推进。
5. 等下一个 interval。

注意 `TsSource` 枚举(在 `resolver.rs`),它标记 `resolved_ts` 推进的"原因"——是被 lock 卡住(`Lock`)、被内存锁卡住(`MemoryLock`)、还是跟着 PD 推进(`PdTso`)。这个 tag 主要用于 metric 和诊断,帮你判断"为什么 resolved_ts 推不动"。

```rust
// components/resolved_ts/src/resolver.rs#L19-L38
pub enum TsSource {
    Lock(TxnLocks),         // lock CF 里的锁
    MemoryLock(Key),        // concurrency_manager 里的内存锁(悲观锁)
    PdTso,                  // 跟着 PD 的 min_ts 推进
    BackupStream,           // 给 backup stream 用
    Cdc,                    // 给 CDC 用
}
```

> **钉死这件事**:`resolved_ts` 的推进是个**周期性(默认 1 秒)的循环**——拿 TSO、查内存锁、check_leader、推进。这个 1 秒的间隔决定了 `resolved_ts` 的"新鲜度"——太短(check_leader RPC 太频繁浪费资源)、太长(CDC/stale read 的延迟变大)。1 秒是个权衡甜点。

---

## 八、技巧精解:resolved_ts 的"只增不减"单调性

本章挑一个最值得单独拆透的技巧:**`resolved_ts` 的"只增不减"单调性,凭什么保证**。这个看似简单的性质(`resolved_ts = max(resolved_ts, new_resolved_ts)`),背后藏着 distributed system 里一个深刻的陷阱。

### 朴素做法会撞什么墙

假设朴素地写:`resolved_ts = new_resolved_ts`(不取 max)。看起来没问题——每次重新算,用新值覆盖。但实际上会出大问题:

考虑这个场景:

1. t=1: Region X 上无锁,`min_ts = 100`,算出 `resolved_ts = 100`。
2. t=2: 一个事务在 Region X 上 prewrite,加了 start_ts = 50 的锁(这是个老事务,50 < 100)。注意这个锁的 `start_ts = 50` 比 `resolved_ts = 100` 小——这是因为这个事务可能在别的地方卡了很久,现在才来 prewrite。
3. t=3: 重新算 `resolved_ts`,这次 `min_lock = (50, ...)`,`new_resolved_ts = min(50, min_ts=101) = 50`。朴素地写,`resolved_ts = 50`——**倒退了!**
4. t=4: GC 看到 `resolved_ts = 50`,只清 `ts < 50` 的版本。但 t=1 时它以为能清到 100,可能已经清了一些 50~100 之间的版本。**那些被清的版本,现在事务 50 可能还需要——数据丢了**。

这就是为什么 `resolved_ts` 必须**只增不减**——一旦它推进到某个值,任何依赖它的组件(GC、CDC、stale read)都可能已经基于这个值做了决策(比如 GC 已经清了版本)。如果 `resolved_ts` 倒退,这些决策就错了。

### TiKV 的答案:`cmp::max(resolved_ts, new_resolved_ts)`

```rust
// components/resolved_ts/src/resolver.rs#L449-L467(关键几行)
if self.resolved_ts >= new_resolved_ts {
    // 推进失败,记录原因(用于诊断 metric)
    RTS_RESOLVED_FAIL_ADVANCE_VEC
        .with_label_values(&[reason.label()])
        .inc();
    self.last_attempt = Some(LastAttempt {
        success: false, ts: new_resolved_ts, reason,
    });
} else {
    self.last_attempt = Some(LastAttempt {
        success: true, ts: new_resolved_ts, reason,
    })
}

// Resolved ts never decrease.
self.resolved_ts = cmp::max(self.resolved_ts, new_resolved_ts);
```

这个 `cmp::max` 是个一行的代码,但它保证了 **`resolved_ts` 永不倒退**。即使 `new_resolved_ts` 比当前值小(被某个老事务的锁卡住),也保持当前值不变。代价是:`resolved_ts` 可能"卡在"一个值(被老锁卡住),直到那个老事务 commit/cleanup 释放锁。

> **不这么写会怎样**:不取 max,`resolved_ts` 会倒退,GC 会基于错误的 safe point 清版本,导致数据丢失。这个 bug 一旦发生,是**静默的数据损坏**——事务读到的数据不一致,而且无法回滚。`cmp::max` 这一行的简洁,掩盖了它背后的深刻性——**在分布式系统里,"单调性"不是自然属性,是必须显式保证的不变量**(invariant)。

### 单调性 + track_lock 的配合

但只取 max 还不够,还有个隐藏陷阱——**锁的 track 必须在 resolved_ts 推进之前**。考虑:

1. t=1: 算 `resolved_ts = 100`(此时无锁)。
2. t=1.5: 一个 prewrite 来了,加 start_ts = 80 的锁。但这个锁的 change log 还没送到 `Resolver`(网络/队列延迟)。
3. t=2: 重新算 `resolved_ts`,此时 `Resolver` 还不知道锁 80,`min_lock = None`,`new_resolved_ts = min_ts = 101`,`resolved_ts = max(100, 101) = 101`。
4. t=2.5: 锁 80 的 change log 才送到 `Resolver`。但 `resolved_ts` 已经是 101 了——**锁 80 的 start_ts < resolved_ts,但 resolved_ts 已经越过了它**。

这有问题吗?其实没有——因为 `track_lock` 里有个 `tracked_index` 机制。锁的 change log 带了它对应的 Raft apply index。`Resolver` 只在 `tracked_index >= 锁的 index` 时才认为这个锁"已确认"。而 `resolved_ts` 的推进,是基于 `Resolver` 当前已知的所有锁——任何 apply index 之后的锁,都不会被漏掉(因为 change log 是按 apply 顺序送的)。这个 **`tracked_index` 单调递增 + change log 按 apply 顺序** 保证了:**`resolved_ts` 推进时,绝不会漏掉任何已 apply 的锁**。

> **钉死这件事**:`resolved_ts` 的正确性,是**"只增不减的单调性(`cmp::max`)+ tracked_index 的 apply 顺序保证 + check_leader 的 leader 身份确认"** 三件套合起来的。任何一个环节漏了,都可能读到未提交事务或 stale 数据。这是 TiKV 事务层正确性最精细的地方之一,和 Percolator 的 Primary 锚点(P4-13/14)是同一级别的精细度。

---

## 九、用 `resolved_ts` 的三个场景(承接前作)

`resolved_ts` 算出来了,谁用它?三个主要消费者,每个都是后面某章的主题:

1. **GC(P6-20)**:GC worker 从 PD 拿 `safe_point`(所有 TiKV 上报自己的 `min_resolved_ts` 给 PD,PD 算全局最小当 safe point)。GC 只清 `commit_ts < safe_point` 的老版本。这保证了 GC 不会清掉任何还在用的事务需要的版本。
2. **CDC(P6-21)**:CDC 的 `Endpoint` 订阅 `resolved_ts`,每次推进就把"这个 ts 之前的所有变更"刷给下游。下游据此保证"看到的数据一致到这个 ts"。
3. **Stale Read**:允许客户端读"稍微旧一点"的数据(换取不阻塞)。客户端指定一个 `read_ts`,TiKV 检查这个 Region 的 `resolved_ts >= read_ts`(通过 `RegionReadProgress`),是的话就读 `ts ≤ read_ts` 的版本,保证不会撞上未提交。

这三个场景的共同点:**它们都需要一个"这个点之前肯定稳定了"的安全点,而 `resolved_ts` 正是 tikv 侧算出的、per-Region 的、保证单调的安全点**。

> **钉死这件事**:`resolved_ts` 是 TiKV 事务层"时间轴"的终点——TSO 发时间戳(起点),`resolved_ts` 算安全点(终点)。中间是事务用时间戳做 MVCC。GC/CDC/stale read 都消费这个安全点。理解了 TSO + resolved_ts,你就理解了 TiKV 怎么"驯服时间"这个分布式系统里最棘手的资源。

---

## 十、架构演进:9.x 的相关变化

最后交代几个 8.x/9.x 和 TSO/resolved_ts 相关的演进:

1. **`causal_ts`(`components/causal_ts/`)**:前章提过,这里补充。它是个"因果序"时间戳,主要给 CDC 用。传统 TSO 是全局线性序(每次都过 PD),causal_ts 是**批量预取 TSO + 本地推算**:PD 一次给一批 ts,本地用一个计数器在批之间推算,不必每个事务都过 PD。这降低了 PD 的 TSO 压力,代价是因果序比线性序弱(但对 CDC 够了)。它有个 `async_flush` 方法,在 leader 切换等关键事件时,把缓存刷掉,保证因果性不破。

    ```rust
    // components/causal_ts/src/lib.rs(简化)
    #[async_trait]
    pub trait CausalTsProvider: Send + Sync {
        async fn async_get_ts(&self) -> Result<TimeStamp>;
        /// Flush (cached) timestamps and return first timestamp to keep causality
        /// on some events, such as "leader transfer".
        async fn async_flush(&self) -> Result<TimeStamp>;
    }
    ```

2. **`min_resolved_ts` 上报 PD**:8.x 引入。每个 TiKV 把自己所有 Region 的最小 `resolved_ts` 上报给 PD(`report_min_resolved_ts`),PD 据此算全局 GC safe point。这让 GC 的 safe point 是**基于真实 TiKV 状态**而非 PD 的猜测,更准。

3. **`follower_resolved_ts`(9.x 增强)**:以前只有 leader 能算 `resolved_ts`(因为只有 leader 能 check_leader quorum)。9.x 让 follower 也能维护一个 `safe_ts`(只读 leader 推进的 resolved_ts,不主动算),用于 follower read。`ResolverStatus` 和 `min_follower_resolved_ts` 字段(endpoint.rs 里的 metric)是这部分演进的痕迹。

4. **`async_commit`(8.x)**:一个事务优化。prewrite 时直接把 PD 给的 `min_ts` 当作初步 commit_ts,不等第二次 TSO。这降低了一阶段延迟,但要求 `resolved_ts` 的 check_leader 更严格(因为现在 commit_ts 可能跨多个 Region 同时定)。这超出本章范围,知道它存在即可。

> **演进趋势**:TSO 本体没大改(物理 + 逻辑 + 单 leader),但围绕它的"用时间戳"侧在演进——causal_ts 给 CDC 开快通道、follower_resolved_ts 支持 follower read、min_resolved_ts 让 GC 更准。这些演进说明:**TSO 是稳定的基石,而 `resolved_ts` 是不断创新的应用层**。

---

## 十一、章末小结

### 回扣主线

本章是第 5 篇的核心章(招牌)。回到二分法:**TSO 是事务层的命脉**——没有全局单调递增的时间戳,MVCC 的版本序、Percolator 的 commit_ts 判定全都不成立。`resolved_ts` 是事务层的"安全点"——它把"哪些数据稳定了"这个问题,变成了一个时间戳,供 GC/CDC/stale read 消费。两者一起,构成了 TiKV 事务层的"时间轴"。

本章的设计可以浓缩成两句话:**TSO 把"全局单调递增"拆成了物理时钟 + 逻辑计数器 + 单 leader 三层,朴素但可靠;`resolved_ts` 把"数据稳定点"归约成了"找最小未提交 start_ts",配 check_leader 保证 leader 身份**。

### 五个为什么

1. **为什么 TSO 用"物理时钟 + 逻辑计数器"混编,而不是单纯计数器?**——物理时钟天然不倒退(墙钟单向),解决 leader 重启不倒退的问题;逻辑计数器解决同毫秒高并发;混编还让时间戳可读(能还原成日期),对调试友好。
2. **为什么物理部分左移 18 位?**——18 位 = 26 万/毫秒的逻辑容量,够单 PD leader 扛突发;高 46 位 = 35 年的物理容量,够 TiKV 用几十年。这个切分是性能和容量的甜点(源码 `TSO_PHYSICAL_SHIFT_BITS = 18`)。
3. **为什么 PD leader 切换有 3 秒不可用?**——切换时新 leader 要等保护期(让老 leader 的过期请求落地)+ 校准物理时钟(取老 leader 持久化值和本地的较大者),这期间不发时间戳。这是 TSO 单 leader 模式不可避免的代价。
4. **为什么 `resolved_ts` 必须"只增不减"(`cmp::max`)?**——GC/CDC/stale read 都基于 `resolved_ts` 做过决策(比如 GC 已经清版本),倒退会导致这些决策错(数据丢失)。单调性是分布式系统里必须显式保证的不变量。
5. **为什么 `resolved_ts` 推进要 check_leader?**——防止旧 leader(TiKV 已经不是这个 Region 的 leader 了)误发 resolved_ts,导致 stale read 读到过时数据。check_leader 用 Raft quorum 确认 leader 身份还成立。

### 想继续深入往哪钻

- **PD 服务端的 TSO 算法**:在 `tikv/pd` 仓的 `server/tso/`,`timestamporacle.go` 实现了物理+逻辑混编和 leader 切换保护期。**不在本地 clone 的 tikv 仓**,可对照官方源码。
- **TSO 的批量机制(客户端侧)**:`components/pd_client/src/tso.rs`,前一章的技巧精解已拆透。
- **`resolved_ts` 的实现**:`components/resolved_ts/src/{endpoint,resolver,advance,scanner}.rs`。本章拆了 resolver 和 advance 的核心,scanner(扫 Region 上的锁初始化)可继续看。
- **GC 怎么用 `resolved_ts`**:P6-20 拆 GC safe point 和 compaction filter。
- **CDC 怎么用 `resolved_ts`**:P6-21 拆 CDC 的变更推送。
- **承接《etcd》**:check_leader 的 quorum 确认,和 Raft 的 read index/lease 是同源思想——保证"读的时候 leader 身份还成立",见《etcd》那本的 Raft 读。

### 引出下一章

我们搞清了 TSO 怎么发、`resolved_ts` 怎么算。但 PD 还有第三件职责没拆透——**调度(balance/hot region)**。数据不均了、某几个 Region 是热点(被疯狂读写),PD 怎么识别、怎么决定搬哪、TiKV 侧怎么执行?还有,tikv 侧的 `split_controller` 怎么在本地识别热点 Region 并主动分裂?下一章 P5-18,我们拆 Region 调度。

> **下一章**:[P5-18 · Region 调度:balance 与热点](P5-18-Region调度-balance与热点.md)
