# 第 6 篇 · 第 21 章 · 悲观锁与 CDC

> **核心问题**:前面几章讲的事务都是**乐观事务**(Optimistic):TiDB 先假设不冲突,直接 prewrite,冲突了再重试。但很多 OLTP 场景(比如高并发的扣库存、抢券)冲突激烈,乐观事务的重试成本高得难以承受——一个事务 prewrite 几十个 key,冲突到最后一个才发现要回滚重来,前面的 Raft 复制全白做。TiKV 因此引入了**悲观事务**(Pessimistic):先加锁、加成功才往下走,把冲突在"加锁阶段"就拦住。而另一类需求是**变更捕获(CDC)**:业务希望 TiKV 的每一次数据变更,能实时流式地推给下游(数据仓库、搜索引擎、缓存),而不是靠定时批量同步。这两件事看起来无关,但底层都依赖 TiKV 的 MVCC 多版本和锁机制,所以放在一起讲。

> **读完本章你会明白**:
> 1. 为什么乐观事务在冲突激烈的场景下不行,悲观事务怎么把冲突检测提前到"加锁"而非"提交"——以及 `acquire_pessimistic_lock` 这个核心动作到底加了什么锁、`for_update_ts` 这个字段为什么是悲观事务正确性的命脉。
> 2. 加锁会阻塞,阻塞会等待,等待会死锁——TiKV 怎么用**集中式的 wait-for 图 + DFS 找环**检测死锁(`DetectTable`)。
> 3. **WaiterManager** 怎么管理成千上万个等待中的事务(锁队列、超时、唤醒),它和 raftstore 怎么解耦。
> 4. CDC 不是"另起一套写流水线",而是**挂在 raftstore 已有的 apply 流程上**——通过 observer 拿到每条已提交命令,转成变更事件推给下游。
> 5. CDC 怎么捕获"旧值"(old value)——MVCC 只存新版本,旧值要回头扫 write CF,这个"回头扫"的开销怎么用 OldValueCache 缓解。
> 6. CDC 和 GC 的微妙关系:CDC 要读旧版本,GC 要清旧版本——怎么保证 GC 不清掉 CDC 还要的版本。

> **如果一读觉得太难**:先只记住三件事——① 悲观事务的核心是 `acquire_pessimistic_lock`,它在 key 上加行锁,带 `for_update_ts` 保证读到最新已提交值(防丢失更新);② 死锁检测是集中式的,在 TiKV 上建一张 wait-for 图,DFS 找环;③ CDC 是个"旁路":挂在 apply 流程上拿变更事件,增量扫描 + 实时推下游,旧值靠扫 write CF + 缓存。

---

## 〇、一句话点破

> **悲观锁把冲突检测从"提交时"提前到"加锁时"——`acquire_pessimistic_lock` 在 key 上加行锁,用 `for_update_ts` 锚定"我读到的版本",防止别的并发事务在我之后偷偷改了又提交(丢失更新);CDC 则是个旁路订阅,挂在 raftstore 的 apply 流程上拿变更事件,增量扫描历史 + 实时推送增量,把 TiKV 变成一个流式数据源——两者都建立在 MVCC 多版本之上,只是用法相反:悲观锁用版本锁住"现在",CDC 用版本回放"过去"。**

这是结论,不是理由。本章倒过来拆:先讲乐观事务的痛点(为什么需要悲观锁),再讲悲观锁的核心动作和 `for_update_ts` 的命脉,再讲锁等待与死锁检测,接着讲 CDC 的数据流和 old value,最后讲 CDC 与 GC 的互动。

---

## 一、乐观事务为什么在冲突激烈时不行

### 回顾乐观事务:先做,提交时才发现冲突

前面第 4 篇(P4-12~14)讲的 Percolator 是**乐观事务**:TiDB 不预先加锁,直接发起 prewrite(给所有 key 写 lock + 写 data),只有 prewrite 时碰到 key 已经有锁(被别的事务占着),才认为冲突,要么等待要么重试。如果 prewrite 成功,再 commit。

这套机制在**冲突稀疏**的场景(比如大部分写入互不相关)很高效——不加锁省了开销,大部分事务一路 prewrite → commit 成功。但它在**冲突密集**的场景下灾难性:

> **不这样会怎样**:考虑一个高并发扣库存场景:100 个请求同时要扣同一件商品的库存。乐观事务下,它们几乎同时 prewrite 同一个 key——只有 1 个能成功,99 个碰到锁要重试。重试又是新一轮 prewrite,又是只有 1 个成功……最坏情况下,N 个并发请求要串行重试 N 轮,每轮都走完整的 Raft 复制(每个 prewrite 都是一次 Raft 提议 + 多数派确认)。这等于把并发退化成串行,且每次重试都是昂贵的分布式写。更糟的是,如果一个事务 prewrite 了 10 个 key,前 9 个都成功(都做了 Raft 复制),第 10 个冲突要整个回滚——前 9 个的复制开销全浪费。**冲突密集场景下,乐观事务的"乐观"变成了"盲目乐观"**。

### 所以这样设计:悲观事务,先加锁再做

悲观事务的思路反过来:**先确认能加锁,再加锁,锁住了才往下走**。具体流程:

1. TiDB 对每个要写的 key,先发一个 `AcquirePessimisticLock` 请求到 TiKV;
2. TiKV 在这个 key 上加一个**悲观锁**(写在 lock CF,和乐观事务的 prewrite 锁共用 lock CF,但类型不同);
3. 加锁成功,TiDB 才继续;加锁失败(key 已被占),TiDB **等待**或**回滚**;
4. 所有 key 都加锁成功后,事务才进入 prewrite/commit 阶段(此时锁已经在,prewrite 不会再冲突)。

```
   乐观事务(冲突密集):                  悲观事务(冲突密集):
   并发 prewrite 同一个 key               并发 acquire_pessimistic_lock 同一个 key
     ↓                                    ↓
   只有 1 个成功,99 个重试               只有 1 个拿到锁,99 个排队等待
     ↓                                    ↓
   重试又是一轮 Raft 复制                 锁释放后,下一个自动拿到(无需重试 Raft)
   开销:O(N²) 次 Raft                    开销:O(N) 次 Raft + O(N) 次锁等待
```

> **钉死这件事**:悲观事务的核心红利是**把冲突检测从"昂贵的 prewrite(Raft 复制)"提前到"廉价的加锁(本地 lock CF 写,虽然也要 Raft 但语义简单)"**。冲突在加锁阶段就被拦住,等待是"在原地等锁释放",而不是"prewrite 失败后整个事务重做"。冲突越激烈,悲观事务的相对优势越大。

但悲观事务不是免费的:它要维护锁等待队列、做死锁检测、处理锁超时。这些机制是本章的重点。

---

## 二、acquire_pessimistic_lock:核心动作与 for_update_ts

### 悲观锁长什么样

悲观锁也是写在 lock CF 里的(和乐观事务 prewrite 的锁共用一个 CF),但它的 `Lock` 结构里有个字段标明类型是悲观锁。来看 `acquire_pessimistic_lock` 的入口(`src/storage/txn/actions/acquire_pessimistic_lock.rs`,见 [acquire_pessimistic_lock](../tikv/src/storage/txn/actions/acquire_pessimistic_lock.rs#L44)):

```rust
// 简化示意(参数很多,只列关键的):
pub fn acquire_pessimistic_lock<S: Snapshot>(
    txn: &mut MvccTxn,
    reader: &mut SnapshotReader<S>,
    key: Key,
    primary: &[u8],                      // 事务的 primary key(和乐观事务一样)
    should_not_exist: bool,
    lock_ttl: u64,                       // 锁的 TTL(超时自动清,防挂死)
    mut for_update_ts: TimeStamp,        // ★ 悲观事务的命脉,下面专门讲
    need_value: bool,
    need_check_existence: bool,
    min_commit_ts: TimeStamp,
    need_old_value: bool,
    lock_only_if_exists: bool,
    allow_lock_with_conflict: bool,      // 8.x 新特性,下面讲
    is_shared_lock_req: bool,            // 共享锁(多个事务可同时读,排他写)
) -> MvccResult<(PessimisticLockKeyResult, OldValue)>
```

函数体干几件事:先 `load_lock` 看这个 key 上有没有现存的锁;如果有,按情况处理(独占锁冲突报 `KeyIsLocked`;共享锁可以叠加);如果没有,写一个新的悲观锁。最后返回加锁的结果(以及被加锁 key 的旧值,供 TiDB 判断)。

### for_update_ts:悲观事务正确性的命脉

这个字段是悲观事务最难理解、也最关键的设计。**`for_update_ts` 表示"这个悲观锁所基于的读快照时间戳"**。它的作用是防止**丢失更新**(lost update),一种乐观/悲观事务都可能踩的并发陷阱。

来看丢失更新的场景(没有 `for_update_ts` 会怎样):

```
   事务 A(读 x=10,要改成 x=11)            事务 B(读 x=10,要改成 x=12)
   1. 读 x,拿到 x=10(start_ts=t1)
                                           2. 读 x,拿到 x=10(start_ts=t1)
   3. acquire_pessimistic_lock(x)           (此时 A 的锁在)
      → 加锁成功(假设没有 for_update_ts 检查)
   4. (基于读到的 x=10,写 x=11)
   5. commit(x=11, commit_ts=t2)
                                           6. (A 已 commit,锁释放)
                                           7. acquire_pessimistic_lock(x)
                                              → 加锁成功
                                           8. (基于步骤2读到的旧 x=10,写 x=12)
                                              ⚠️ 但 x 已经被 A 改成 11 了!
                                              B 基于过期的 x=10 写 x=12,把 A 的更新丢了!
```

事务 B 在步骤 2 读到的是旧值 x=10,但它在加锁时如果不重新检查"我读到的值还是不是最新的",就会基于过期数据写,覆盖掉 A 的更新——这就是丢失更新。

`for_update_ts` 的作用就是堵这个陷阱。`acquire_pessimistic_lock` 里有这么一段([源码](../tikv/src/storage/txn/actions/acquire_pessimistic_lock.rs#L167-L181)):

```rust
// 简化示意:
// 读这个 key 最新的提交记录,看 commit_ts 是否 > for_update_ts
if commit_ts > for_update_ts {
    // 我加锁时基于的 for_update_ts 已经过期了——有比我更新的事务已提交!
    // 把 for_update_ts 推进到 commit_ts(更新自己看到的"最新版本")
    for_update_ts = commit_ts;
    // ...根据隔离级别,可能要让事务重读(返回 "需要重试")
}
```

意思是:加锁时,如果发现这个 key 已经有比自己 `for_update_ts` 更新的提交(自己之前读到的值过期了),就要处理(把 `for_update_ts` 推进,甚至让 TiDB 重试)。这样保证:**悲观锁加成功时,事务看到的版本是最新的,基于它写不会丢更新**。

> **钉死这件事**:`for_update_ts` 是悲观事务"读到最新"的锚。乐观事务的 prewrite 不需要这个(因为它本来就是"我要改这个值",冲突就直接失败);悲观事务要这个,因为它先加锁、之后才基于"读到的值"算新值——必须保证"读到的值"在加锁那一刻是最新的。这是悲观事务在 RC(Read Committed)隔离级别下保证不丢更新的核心机制。TiDB 的悲观事务默认是 RC,靠 `for_update_ts` 反复推进实现。

### 锁的 TTL 与 min_commit_ts

另外两个字段:

- **`lock_ttl`**:锁的超时时间。悲观锁可能持有较长时间(事务在 TiDB 端做计算),如果 TiDB 挂了,锁不能永久占着,所以有 TTL,超时自动被清(由 `CheckTxnStatus` 处理,承接 P4-15)。
- **`min_commit_ts`**:事务 commit 时的最小 commit_ts。这是个正确性约束:悲观事务的 commit_ts 必须 ≥ 所有 key 的 `for_update_ts` 的最大值(以及 `min_commit_ts`),保证 commit 顺序和加锁时看到的版本一致。

### 8.x 新特性:allow_lock_with_conflict

注意参数里有个 `allow_lock_with_conflict`(见 [源码](../tikv/src/storage/txn/actions/acquire_pessimistic_lock.rs#L388-L393))。这是 8.x 引入的优化:正常情况下,`acquire_pessimistic_lock` 遇到 key 已被别的事务锁住,会返回 `KeyIsLocked` 让 TiDB 等待。但开启 `allow_lock_with_conflict` 后,**可以直接抢锁**(把对方的锁顶掉,记录"冲突时的 commit_ts")——适用于"宁可让对方回滚,也要保证我能继续"的场景(比如带 `for update` 的强一致读)。这是悲观锁在冲突极端激烈时的一个激进选项。

---

## 三、锁等待与死锁检测

### 加锁失败怎么办:WaiterManager 管队列

悲观锁加失败(key 被占),事务不能直接报错回滚(那样太激进),而是**进入等待**——等持锁的事务释放后再唤醒。管理这个等待队列的,是 `WaiterManager`(`src/server/lock_manager/waiter_manager.rs`)。

每个等待中的事务,在 WaiterManager 里是一个 `Waiter`(见 [Waiter](../tikv/src/server/lock_manager/waiter_manager.rs#L195)),记录:等的是哪个 key、哪个锁、超时时间、唤醒回调。WaiterManager 的核心接口是 `wait_for`(见 [wait_for](../tikv/src/server/lock_manager/waiter_manager.rs#L431)):

```rust
// 简化示意:
pub fn wait_for(
    &self,
    token: LockWaitToken,             // 这个等待的唯一标识
    region_id: u64,
    region_epoch: RegionEpoch,
    term: u64,
    start_ts: TimeStamp,
    wait_info: KeyLockWaitInfo,        // 等哪个 key 哪个锁
    timeout: WaitTimeout,
    cancel_callback: CancellationCallback,
    diag_ctx: DiagnosticContext,
) {
    self.notify_scheduler(Task::WaitFor { ... });  // 扔给后台 scheduler 处理
}
```

注意它不是同步阻塞,而是把等待任务扔给一个后台 scheduler(基于 batch-system 类似的 actor 模型)。当持锁的事务 commit 或 rollback 释放锁时,会调 `remove_lock_wait` / 唤醒对应的 Waiter。这套机制让 TiKV 能同时管理成千上万个等待中的事务,而不占着调用线程。

### 死锁:等待图的环

等待会引出一个经典问题——**死锁**。事务 A 等 B 的锁,B 又等 A 的锁,两者永远等下去。看场景:

```
   事务 A: 持有 x 的锁,等 y 的锁
   事务 B: 持有 y 的锁,等 x 的锁
   → A 等 B,B 等 A,死锁!
```

死锁的特征是**等待关系形成一个环**。检测死锁,就是检测等待图(wait-for graph)里有没有环。

### DetectTable:集中式 wait-for 图 + DFS 找环

TiKV 的死锁检测是**集中式**的——整个 TiKV 集群选一个节点跑 `DetectTable`(`src/server/lock_manager/deadlock.rs`,见 [DetectTable](../tikv/src/server/lock_manager/deadlock.rs#L115)),所有 TiKV 把自己的"谁等谁"关系上报给它。DetectTable 维护一张 `wait_for_map: HashMap<txn_ts, HashMap<lock_ts, Locks>>`(等待图的邻接表表示),核心是 `detect` 方法([detect](../tikv/src/server/lock_manager/deadlock.rs#L150)):

```rust
// 简化示意:
pub fn detect(
    &mut self,
    txn_ts: TimeStamp,             // 发起检测的事务(我)
    lock_ts: TimeStamp,            // 我在等谁持的锁
    lock_hash: u64, lock_key: &[u8], resource_group_tag: &[u8],
) -> Option<(u64, Vec<WaitForEntry>)> {
    // 1. 先注册"我在等 lock_ts"这条边(如果还没注册)
    if self.register_if_existed(txn_ts, lock_ts, ...) {
        return None;   // 已注册过,不重复检测
    }
    // 2. 从 lock_ts 出发,DFS 找能不能回到 txn_ts(我)
    if let Some((deadlock_key_hash, wait_chain)) = self.do_detect(txn_ts, lock_ts) {
        // 找到环!死锁了
        return Some((deadlock_key_hash, wait_chain));
    }
    // 3. 没环,注册这条边(真的开始等)
    self.register(txn_ts, lock_ts, ...);
    None
}
```

`do_detect` 是 DFS([do_detect](../tikv/src/server/lock_manager/deadlock.rs#L179)):从 `lock_ts`(我等的那个事务)出发,沿着 wait-for 图往下走(它又在等谁?),如果路径绕回 `txn_ts`(我自己),就形成环——死锁。它还会顺便清理过期的边(`wait_for.retain(|_, locks| !locks.is_expired(now, ttl))`),避免过期等待污染图。

```
   wait-for 图(DetectTable 内部):
   txn_ts(A) ──等──▶ lock_ts(B)
                       │
                       └──等──▶ lock_ts(A) ← 绕回来了!环 = 死锁
```

一旦检测到死锁,DetectTable 返回一个 `wait_chain`(等待链,描述环上每个事务),通过 `WaiterManager::deadlock` 通知相关事务。TiKV 选一个事务(通常是环里"代价最小"的,或者就是发起检测的那个)让它**回滚**,打破环,其他事务继续。

> **不这样设计会怎样**:如果不做死锁检测,死锁的事务会一直等到 TTL 超时(可能几十秒甚至几分钟)才被清——业务感受到的是"卡住了"。集中式 DFS 检测能在死锁形成的瞬间发现它、打破它,把延迟从"几十秒"降到"毫秒级"。代价是有一个集中检测节点(它挂了要切换,但死锁检测不影响数据正确性,只影响响应延迟,所以可接受)。

> **钉死这件事**:TiKV 的死锁检测是经典的"wait-for 图 + 找环"算法,集中式实现。它不是 TiKV 的发明(数据库教科书的标准做法),但用 Rust 在分布式环境下做对有细节:边要带 TTL 自动清(避免过期等待污染图)、要能返回等待链(给业务诊断)、要支持并发上报(多 TiKV 同时 register)。这些是工程化的打磨,不是算法本身的难点。

---

## 四、CDC:把变更实时推给下游

### CDC 是什么:数据库的"流式订阅"

**CDC(Change Data Capture)** 是一种数据同步模式:把数据库的每一次变更(插入、更新、删除),作为事件实时推给下游消费者。典型场景:

- 数据仓库:把 TiDB 的业务数据实时同步到 OLAP 系统(ClickHouse、Snowflake)做分析;
- 搜索引擎:把商品表变更同步到 Elasticsearch 建索引;
- 缓存:把热点数据变更同步到 Redis;
- 微服务:某服务订阅另一服务的数据变更(事件驱动架构)。

朴素的同步是**定时批量**(每小时 dump 一次表),但有延迟、对源库压力大。CDC 是**实时流式**:TiKV 每提交一条数据,就推一个事件给下游,延迟可低到秒级甚至亚秒级。

```
   批量同步(老):                       CDC(实时):
   每小时 dump 一次                     TiKV 每次 commit → 立即推事件
   延迟:小时级                         延迟:秒级/亚秒级
   源库压力:大(全表扫)                源库压力:小(增量读)
```

### CDC 的数据流:挂在 apply 流程上的旁路

TiKV 的 CDC 不是另起一套写流水线,而是**旁路挂在 raftstore 已有的 apply 流程上**。回顾 P3-11:Raft commit 的命令,经 ApplyFsm apply 到 RocksDB。CDC 在这个流程里插了一个**observer**(`components/cdc/src/observer.rs`,见 [CdcObserver](../tikv/components/cdc/src/observer.rs#L26)),每当 apply 一条命令,observer 就把它捕获下来,转成一个 `CmdBatch`(一批已提交命令),发给 CDC 的 endpoint。

```
   Raft commit 的命令
        ↓
   ApplyFsm apply 到 RocksDB
        ↓ (apply 完,observer 旁路捕获)
   CdcObserver ──CmdBatch──▶ CDC Endpoint
                               ↓
                            Delegate(每个 Region 一个)
                               ↓ 转成 Event(变更事件)
                            下游消费者(TiCDC / Kafka / ...)
```

CDC Endpoint(`components/cdc/src/endpoint.rs`,见 [Endpoint](../tikv/components/cdc/src/endpoint.rs#L475))的 `on_multi_batch` 方法([on_multi_batch](../tikv/components/cdc/src/endpoint.rs#L1001))就是处理这批 CmdBatch 的入口——把它们按 Region 分发到对应的 `Delegate`,Delegate 转成 `Event` 推给下游。

### Delegate:每个 Region 一个,管下游订阅

每个 Region 在 CDC 里有一个 `Delegate`(`components/cdc/src/delegate.rs`,见 [Delegate](../tikv/components/cdc/src/delegate.rs))。它管:

- 这个 Region 有哪些下游订阅者(`Downstream`,每个订阅者是个 TiCDC 连接);
- 收到的 CmdBatch 怎么转成 Event 推给下游(通过 `sink`,见 [sink_event](../tikv/components/cdc/src/delegate.rs#L194));
- Region 的 resolved_ts(这个时间点之前的变更都已稳定,可以安全推给下游)。

订阅一个 Region 的数据流是两阶段的:

1. **增量扫描(Incremental Scan)**:订阅开始时,先扫这个 Region 在某个 start_ts 之后的**历史变更**(从 write CF 扫已提交的记录),把"积压的变更"先推给下游——这是 `Initializer` 干的(`components/cdc/src/initializer.rs`,见 [Initializer](../tikv/components/cdc/src/endpoint.rs#L952))。
2. **实时推送**:增量扫描追上当前后,切换到实时模式——observer 捕获的新 apply 事件,直接推给下游。

这个"先扫历史追平、再切实时"的模式,是所有 CDC/复制系统的标准做法(承接《etcd》那本讲的 watch 机制,原理类似)。

---

## 五、old value:CDC 最棘手的问题

### CDC 要的不只是新值,还有旧值

下游消费者常常不只想要"新值",还想要"旧值"(变更前的值)。比如:同步到数据仓库时,要知道一个 update 从什么值变成什么值;做审计日志时,要记录"原值 X 改成了 Y"。

但 TiKV 的 MVCC apply 流程,只直接给出**新值**(apply 一条 Put 命令就是写新值)。旧值在哪?在 write CF 里——上一个版本的 value。所以 CDC 要拿到 old value,得**回头扫 write CF**,找这个 key 在当前变更之前的最近一个版本。

这就是 `components/cdc/src/old_value.rs` 干的事(见 [OldValueCache](../tikv/components/cdc/src/old_value.rs#L50))。每次 apply 一条命令,CDC 要算 old value:

- 如果这条命令是 Put,old value 是这个 key 上一个版本的 value;
- 如果是 Delete,old value 也是上一个版本的 value(被删前的值);
- 算 old value 要扫 write CF,这是个 IO 开销。

### OldValueCache:缓解回头扫的开销

如果每条变更都回头扫 write CF 算 old value,CDC 的开销会很大(尤其在高写入场景)。所以 CDC 有个 `OldValueCache`(LRU 缓存):算过的 key 的 old value 缓存起来,下次同一个 key 再变更时,可能缓存里还有上次的 old value(就是这次的 old value 的来源),直接用。

```
   key X 第一次变更(put v=10):
     old_value = 扫 write CF(没有旧版本)→ None
     缓存: { X: (None, ...) }

   key X 第二次变更(put v=20):
     old_value = 上一版的 value = 10
     先查缓存:命中!X 的旧值是 None?不对——
     ⚠️ 实际逻辑更微妙:缓存的是"上次变更的 old value",这次要的是"上次的 new value"
```

实际逻辑比上面更精细(要区分 MutationType、要处理 cache miss),但思路是:**用 LRU 缓存减少回头扫 write CF 的次数**。这是 CDC 在高吞吐下能撑住的关键优化之一。

> **不这样写会怎样**:如果不缓存,每条变更都要扫 write CF 找 old value——在高写入 Region(每秒几千次更新),CDC 的 IO 开销会和前台事务读抢资源,拖慢业务。OldValueCache 把"重复 key 的 old value 查询"从 IO 降到内存命中,代价是缓存一致性要小心(Region split/merge 时要清缓存)。

---

## 六、CDC 与 GC 的微妙关系

### CDC 要旧版本,GC 要清旧版本——冲突?

讲到这里你应该发现一个张力:**CDC 要读旧版本(增量扫描历史、算 old value),而 GC(P6-20)要清旧版本**。如果 GC 把 CDC 还要的旧版本清掉了,CDC 就出错——增量扫描读不到完整历史,old value 算不出来。

所以 TiKV 必须保证:**GC 不清掉 CDC 还在用的旧版本**。机制是——CDC 向 PD 注册自己需要的最低 safe_point。

具体说,TiCDC(CDC 的协调层,在 TiDB 端)会向 PD 报告自己消费到的进度(已推给下游到哪个 ts 了)。PD 在算 GC safe_point 时,把 CDC 的进度作为下界之一——safe_point 不会超过 CDC 的进度,保证 CDC 还要读的版本不会被 GC 清掉(承接 P6-20 讲的 safe_point 计算考虑下游消费者)。

> **钉死这件事**:CDC 和 GC 是一对协作关系——CDC 报告进度,GC 尊重进度。这是分布式系统里"消费者保护数据不被回收"的通用模式(类似 Kafka 的 consumer offset 保护日志不被清理、类似《etcd》那本 watch + compaction 的关系)。如果 CDC 挂了或消费太慢,safe_point 会被卡住不前进,导致旧版本堆积——这是 CDC 故障的一个副作用(磁盘占用涨),运维要监控。

### resolved_ts:CDC 推进的安全边界

CDC 推给下游的事件,要保证"已提交且不会再变"。这靠 **resolved_ts**(`components/resolved_ts/`,承接 P5-17):每个 Region 算出一个时间戳,表示"此 ts 之前的变更都已在这个 Region 提交、且不会再被 rollback"。CDC 把 ≤ resolved_ts 的变更安全推给下游——它们是确定的。

resolved_ts 的计算要考虑锁:如果一个 Region 上有未提交的悲观锁(事务还在进行),resolved_ts 不能超过这个锁的 start_ts——因为锁对应的事务可能 commit(产生新变更)也可能 rollback(无变更),在它结束前不能确定。所以悲观锁会"卡住" resolved_ts 的推进,进而影响 CDC 的延迟。这是悲观事务和 CDC 的另一个隐秘联系。

---

## 七、技巧精解

本章挑两个最硬核的技巧单独拆透。

### 技巧一:for_update_ts 推进——悲观事务防丢失更新的命脉

前面提过 `for_update_ts`,这里单独拆透它的精妙。问题是:**悲观事务在 RC 隔离级别下,怎么保证"基于读到的值写,不会丢更新"?**

朴素方案是"加锁时重新读一次最新值"。但这有几个问题:① 重新读要扫 write CF,开销;② 读到的值要不要返回给 TiDB(让 TiDB 基于新值重算)?如果返回,语义复杂。

TiKV 的 `for_update_ts` 方案更优雅:它不真的"重读",而是**记录"我加锁时所基于的最新 commit_ts"**。逻辑是:

1. 加锁时,扫这个 key 的最新提交记录,拿到它的 commit_ts;
2. 如果 commit_ts > for_update_ts(我之前基于的版本过期了),**把 for_update_ts 推进到 commit_ts**;
3. 推进后,根据隔离级别决定:在 RC 下,可以让事务继续(它现在"基于"的版本是最新的);在需要严格一致的场景,返回"需要重试"让 TiDB 重读。

这个设计的精妙在于:**它不重读 value(省 IO),只比较 commit_ts(廉价)**。它利用了 MVCC 的版本号语义——commit_ts 单调递增,比较 commit_ts 就能判断"我的版本是不是最新的",不必读出 value 内容。

> **不这么写会怎样**:如果每次加锁都重读 value,TiDB 要基于新值重算事务逻辑(可能涉及复杂的 SQL),开销巨大且语义混乱。`for_update_ts` 只推进版本号、不重读内容,把"防丢失更新"的成本降到最低(一次 commit_ts 比较)。这是用"版本号单调性"代替"内容比较"的智慧——和 MVCC 的可见性判断(用 commit_ts 比较而非读 value)是同一种思路。

### 技巧二:wait-for 图的 DFS 找环——为什么集中式而非分布式

死锁检测有两种思路:**分布式**(每个节点各自检测局部环)和**集中式**(一个节点收集全局信息检测)。TiKV 选了集中式,这个选择值得拆。

朴素想法是分布式:每个 TiKV 自己检测自己节点上的等待关系。但这会漏掉**跨节点的环**——事务 A 在 TiKV-1 等 B 的锁,B 持的锁在 TiKV-1,但 B 又在 TiKV-2 等 A 的锁(A 持的锁在 TiKV-2)。这个环跨了两个 TiKV,任何单个 TiKV 都看不到完整的环。

集中式解决:所有 TiKV 把等待关系上报给一个 DetectTable,它有全局视图,能检测任何跨节点的环。代价是:① 集中节点的可用性(挂了要切换);② 上报的网络开销(但等待关系不频繁,开销小)。

```
   分布式(漏跨节点环):              集中式(TiKV 用的):
   TiKV-1: A 等 B                      所有 TiKV 上报 ──▶ DetectTable
   TiKV-2: B 等 A                        ↓
   每个节点只看到局部,看不到环         全局图:A→B→A,检测到环!
```

`do_detect` 的 DFS 实现也有个细节(见源码注释 [do_detect](../tikv/src/server/lock_manager/deadlock.rs#L178-L194)):它用一个 `pushed: HashMap<TimeStamp, TimeStamp>` 记录已访问节点的前驱,避免重复访问。注释提到"图是 DAG 不是树,一个节点可能有多个前驱,但只记一个就行"——因为只要找到一条回到 `txn_ts` 的路径就够了,不必找所有路径。这是 DFS 找环的标准剪枝。

> **不这么写会怎样**:分布式检测会漏跨节点死锁(漏报),导致死锁事务卡到 TTL 超时。集中式用一份全局图的代价(单点 + 上报开销),换"不漏报"。这是分布式系统"全局一致性 vs 分布式开销"的经典权衡——死锁检测选了集中式,因为它要求正确性(漏报=卡死),而开销可接受(等待关系稀疏)。

---

## 八、章末小结

### 回扣主线

本章讲悲观锁和 CDC,两者都服务二分法的**事务层**这一面,但角度不同:

- **悲观锁**是事务并发控制的另一套机制(和乐观 Percolator 并列)。它用 `acquire_pessimistic_lock` 在 key 上加行锁,用 `for_update_ts` 锚定读版本防丢失更新,用 WaiterManager 管等待队列,用 DetectTable 集中式检测死锁。它把冲突检测从"昂贵的 prewrite"提前到"廉价的加锁",适合冲突激烈的 OLTP。
- **CDC** 是事务层的"旁路订阅"。它挂在 raftstore 的 apply 流程上(复制层),捕获变更事件推给下游,用增量扫描 + 实时推送,用 OldValueCache 缓解 old value 回头扫开销,用 resolved_ts 保证推送安全,和 GC 协作保护旧版本。

两者都建立在 MVCC 多版本(P3-10)之上:悲观锁用版本锁住"现在"(防并发改),CDC 用版本回放"过去"(推历史变更)。它们是事务层在"Percolator 两阶段提交"之外的两个延伸——一个深化并发控制,一个扩展数据消费。

本章也是第 6 篇的收尾。第 6 篇讲了 Coprocessor(计算下推)、GC(版本回收)、悲观锁与 CDC(并发控制与变更捕获)——这些都是 TiKV 走向生产级的"配套件",让它在 OLTP/OLAP/数据同步等多种场景下都能用。下一章 P7-22,我们做全书收束,把"从 etcd 到 TiKV 的跃迁"做个总对照。

### 五个为什么

1. **为什么乐观事务在冲突密集时不行?**——prewrite 失败要整个重做(含 Raft 复制开销),N 个并发冲突要重试 O(N) 轮,退化成串行。悲观锁把冲突提前到加锁阶段,等待而非重试。
2. **为什么悲观锁要 `for_update_ts`?**——防丢失更新。加锁时如果发现 key 已有更新的提交(自己读到的过期了),推进 `for_update_ts` 锚定最新版本。用 commit_ts 比较代替重读 value,廉价且正确。
3. **为什么死锁检测是集中式?**——分布式会漏跨节点的环(漏报=卡死)。集中式 DetectTable 收集全局等待关系,DFS 找环,保证不漏。代价是单点(可切换)和上报开销(稀疏,可接受)。
4. **为什么 CDC 挂在 apply 流程而非另起流水线?**——apply 流程本就遍历每条提交命令(写 RocksDB),CDC 旁路捕获零额外遍历(和 P6-20 compaction filter 借车同理)。另起流水线要重复扫,浪费。
5. **为什么 CDC 和 GC 要协作?**——CDC 要读旧版本(增量扫描、old value),GC 要清旧版本。CDC 向 PD 报告进度,PD 把 CDC 进度作为 safe_point 下界,保证不清掉 CDC 还要的版本。这是"消费者保护数据不被回收"的通用模式。

### 想继续深入往哪钻

- **想看悲观锁实现**:读 `src/storage/txn/actions/acquire_pessimistic_lock.rs`(关注 `for_update_ts` 推进逻辑)。
- **想看锁等待与死锁**:读 `src/server/lock_manager/{waiter_manager,deadlock,client}.rs`。
- **想看 CDC 数据流**:读 `components/cdc/src/{endpoint,delegate,observer,initializer,old_value}.rs`。
- **想看 resolved_ts**:读 `components/resolved_ts/`(承接 P5-17 TSO)。
- **想看 TiCDC 协调层**:那是 TiDB 端的组件(`tikv/migration/cdc`),不在本书范围,但理解了 TiKV 这端的 endpoint/delegate,看 TiCDC 就顺了。

### 引出下一章

讲完悲观锁和 CDC,第 6 篇(生产特性)就完整了。从 P0-01 的"为什么需要 TiKV"出发,我们走过了 Region 切分、multi-raft、raftstore、RocksDB+MVCC、Percolator 事务、PD 协调、Coprocessor/GC/悲观锁/CDC——一条写请求从 TiDB 到 TiKV 落盘再回来的全过程,每一步的设计动机和实现技巧都拆过了。下一章 P7-22,我们做全书收束:把"从 etcd 的一个 Raft 组,到 TiKV 的百万个 Raft 组 + Percolator 跨组 ACID"这场跃迁,做一个总对照表,讲清得到了什么、付出了什么,并展望 raftstore-v2、resource_control 等演进方向。

> **下一章**:[P7-22 · 全书收束:从 etcd 到 TiKV 的跃迁](P7-22-全书收束-从etcd到TiKV的跃迁.md)
