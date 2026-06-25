# 第 3 篇 · 第 11 章 · Apply 流水线:Raft 命令怎么落盘

> **核心问题**:前面三章把"数据存哪"(P3-09 RocksDB 三 CF)、"数据长什么样"(P3-10 MVCC 编码)都拆透了。可还有一个关键的环节没讲——**一条 Raft 命令,从被 Raft 算法判定 commit,到真正变成 RocksDB 里的写,中间到底发生了什么?** 这不是一句"apply 一下"能带过的。TiKV 的 Apply 是一个独立的 batch-system(ApplyFsm),和跑 Raft 的 PeerFsm 分开跑;它要把多条已 commit 的命令**攒成一批**一起写 RocksDB(提高吞吐);它要在 apply 完之后**回调通知客户端**;它还要处理 yield(一批太大要分多轮)、admin 命令(Split/ChangePeer/TransferLeader 的特殊 apply)、以及 apply 完成后把结果反馈给 PeerFsm。这一章把 ApplyFsm 的内部拆透——它是 P2-05 五步流水线的第五步(Apply)的落点。

> **读完本章你会明白**:
> 1. 为什么 Apply 要单独拆成一个 batch-system(ApplyFsm),不在 PeerFsm 里直接做——答案在"快慢分离",P1-04 已立骨架,本章拆到 ApplyFsm 内部。
> 2. **cmd batch** 是 Apply 性能的核心:ApplyFsm 把多条已 commit 的 Raft 命令攒进**同一个 WriteBatch**,一次性写 RocksDB,把"N 条命令 = N 次 RocksDB 写"变成"N 条命令 = 1 次 RocksDB 写(批量)"。攒批的触发条件是 `WriteBatch::should_write_to_engine`(默认 256 条)和 admin 命令的强制 flush。
> 3. Apply 的三阶段循环:`prepare_for`(准备 WriteBatch)→ `process_raft_cmd` 逐条 apply(往 WriteBatch 里塞 put_cf/delete_cf)→ `commit`/`flush`(WriteBatch 写 RocksDB + 回调 callback)。对应源码 `ApplyContext::{prepare_for, commit, write_to_db, flush}`。
> 4. apply 完成后怎么通知 PeerFsm(`ApplyRes` 经 Notifier)、怎么回调客户端 callback(`ApplyCallbackBatch`)——这条 callback 链路是 P2-05 那条"回信地址"的终点。
> 5. admin 命令(Split/CommitMerge/TransferLeader/CompactLog)的 apply 各有特殊处理——Split 改 Region 元数据、CompactLog 触发日志 GC、TransferLeader 在 apply 时推进状态。

> **如果一读觉得太难**:先只记住三件事——① Apply 在独立的 ApplyFsm 里跑(和 PeerFsm 分开),因为 Apply 慢(写 RocksDB),不能阻塞 Raft 心跳;② ApplyFsm 把多条已 commit 命令攒进一个 WriteBatch,一次性写 RocksDB(批量提高吞吐),攒够 256 条或遇到 admin 命令就 flush;③ apply 完后,ApplyFsm 通过 ApplyRes 通知 PeerFsm(更新 apply_index),通过 callback 通知客户端(写真的落盘了)。

---

## 〇、一句话点破

> **ApplyFsm 是个独立的 batch-system,它从 PeerFsm 收到"这批命令已 commit"的消息,把它们攒进一个 WriteBatch(放 put_cf/delete_cf 的写),攒够阈值(默认 256 条)或遇到 admin 命令就一次性写 RocksDB(write_to_db),写完通过 ApplyRes 通知 PeerFsm(更新 apply 进度)、通过 callback 通知客户端(写落盘了)。这个"攒批写 RocksDB"是 TiKV 写吞吐的关键——把 N 条命令的写开销摊薄成 1 次 RocksDB 写。**

这是结论,不是理由。本章倒过来拆:先讲 Apply 为什么要独立(P1-04 已立骨架,这里回到 ApplyFsm 内部);再拆 Apply 的三阶段循环(prepare_for → process_raft_cmd → commit/flush);然后拆 cmd batch 怎么攒批、什么时候 flush;接着看 apply 完怎么通知 PeerFsm 和客户端;最后看 admin 命令的特殊 apply。

承接 P2-05(五步流水线的第五步 Apply 落点)、P1-04(batch-system + FSM,ApplyFsm 在那里引入)、P3-09(RocksDB 三 CF,apply 写进这些 CF)、P3-10(MVCC 编码,apply 把命令转成这种编码的 key-value)。

---

## 一、Apply 为什么要独立:快慢分离

### 回顾 P1-04:PeerFsm 和 ApplyFsm 分两个 batch-system

P1-04 拆过,TiKV 跑**两个** batch-system:

- **Raft batch-system**:跑 `PeerFsm`(每个 Region 一个副本一个 PeerFsm),干 Raft 推进(propose、step、handle_raft_ready)——这些是**快操作**(微秒到毫秒级,纯内存 + 少量日志写)。
- **Apply batch-system**:跑 `ApplyFsm`(每个 Region 一个副本一个 ApplyFsm),干 Apply——把已 commit 的命令写进 RocksDB——这是**慢操作**(毫秒到几十毫秒,RocksDB 写 MemTable + 可能 flush + 可能触发 Compaction)。

为什么拆开?P1-04 给的论证:**慢 Apply 不能阻塞快 Raft**。如果 Apply 在 PeerFsm 里直接做,某个 Region 的 Apply 慢了(RocksDB 写卡顿),同线程其他几十个 Region 的 Raft 心跳都发不出去,leader 被以为挂了,触发误选举,集群抖动。拆开后,Raft 心跳在 Raft 线程跑(不被 Apply 阻塞),Apply 在 Apply 线程跑(慢但可批量)。

> **钉死这件事(承接 P1-04)**:ApplyFsm 和 PeerFsm 分两个 batch-system,本质是**快慢分离**——Raft 推进快且实时(心跳不能断),Apply 慢但可批量(写 RocksDB 可以攒一批一起做)。这种分离让两者各跑各的、互不阻塞。本章拆 ApplyFsm 的内部。

### ApplyFsm 和 PeerFsm 怎么通信:mailbox

P1-04 讲过,FSM 之间靠 mailbox 通信。ApplyFsm 和 PeerFsm 的通信路径:

- **PeerFsm → ApplyFsm**:PeerFsm 在 `handle_raft_ready_append` 里取到 ready 的 `committed_entries`(P2-05 第三节),把它们包装成 `Apply` 任务(带 entries、callback、commit_index),投递到对应 Region 的 ApplyFsm 信箱。
- **ApplyFsm → PeerFsm**:ApplyFsm apply 完一批后,构造 `ApplyRes`(带 applied_index、执行结果),通过 `Notifier` trait 发回 PeerFsm。

看 [`ApplyFsm` 的结构](../tikv/components/raftstore/src/store/fsm/apply.rs#L4048):

```rust
// components/raftstore/src/store/fsm/apply.rs#L4048-L4056(摘录)
pub struct ApplyFsm<EK>
where
    EK: KvEngine,
{
    delegate: ApplyDelegate<EK>,          // Apply 的状态:当前 region、apply 到哪个 index 了……
    receiver: Receiver<Box<Msg<EK>>>,     // 收 PeerFsm 发来的"这批命令 commit 了,请 apply"
    mailbox: Option<BasicMailbox<ApplyFsm<EK>>>,
}
```

和 PeerFsm 一样的结构——一个 `delegate`(真正的状态 ApplyDelegate)、一个 `receiver`(消息队列)、一个 `mailbox`(自己的句柄,可以给自己发消息)。这就是 actor 模型的标准形态。

---

## 二、Apply 的一轮:handle_normal 取消息、handle_apply 处理

Apply 线程(ApplyPoller)的主循环,和 Raft 线程一样,是 batch-system 的 `Poller::poll`(P1-04 第四节拆过)。核心是 `handle_normal`——一个 ApplyFsm 被取出来,处理一批消息。看 [ApplyPoller 的 handle_normal](../tikv/components/raftstore/src/store/fsm/apply.rs#L4785):

```rust
// components/raftstore/src/store/fsm/apply.rs#L4785-L4843(简化示意)
fn handle_normal(&mut self, normal: &mut impl DerefMut<Target = ApplyFsm<EK>>) -> HandleResult {
    let mut handle_result = HandleResult::KeepProcessing;
    normal.delegate.handle_start = Some(Instant::now_coarse());
    // ... 处理 yield_state(上一轮没干完的,先 resume)
    
    // —— 关键 1:从 receiver 批量取一批消息 ——
    while self.msg_buf.len() < self.messages_per_tick {
        match normal.receiver.try_recv() {
            Ok(msg) => self.msg_buf.push(msg),
            Err(TryRecvError::Empty) => {
                handle_result = HandleResult::stop_at(0, false);
                break;
            }
            Err(TryRecvError::Disconnected) => {
                normal.delegate.stopped = true;
                handle_result = HandleResult::stop_at(0, false);
                break;
            }
        }
    }

    // —— 关键 2:处理这批消息 ——
    normal.handle_tasks(&mut self.apply_ctx, &mut self.msg_buf);
    // ...
    handle_result
}
```

两步:**批量取一批消息**(最多 `messages_per_tick` 条,默认几十),然后 `handle_tasks` 处理这批。`handle_tasks` 内部对每条消息分发:

- `Msg::Apply(apply)` → 调 [`handle_apply`](../tikv/components/raftstore/src/store/fsm/apply.rs#L4100) 处理(这是本章主角);
- 其他消息(destroy、snapshot 等)各自处理。

### handle_apply:取出 entries,逐条 apply

看 [`handle_apply`](../tikv/components/raftstore/src/store/fsm/apply.rs#L4100) 的核心:

```rust
// components/raftstore/src/store/fsm/apply.rs#L4100-L4178(简化示意)
fn handle_apply(
    &mut self,
    apply_ctx: &mut ApplyContext<EK>,
    mut apply: Apply<Callback<EK::Snapshot>>,
) {
    // ... 前置检查(pending_remove、stopped、wait_data)

    let mut entries = Vec::new();
    // —— 关键 1:从 apply.entries 里取出已 commit 的 Raft 日志条目 ——
    for cached_entries in apply.entries {
        let (e, sz) = cached_entries.take_entries();
        if e.is_empty() {
            // entry cache 里没有,从 RaftEngine fetch
            self.delegate.raft_engine.fetch_entries_to(
                rid, start, end, None, &mut entries
            ).unwrap();
        } else if entries.is_empty() {
            entries = e;
        } else {
            entries.extend(e);
        }
    }

    // —— 关键 2:更新 commit_index/commit_term(防止 commit 回退)——
    self.delegate.apply_state.set_commit_index(cur_state.0);
    self.delegate.apply_state.set_commit_term(cur_state.1);

    // —— 关键 3:把 callback 挂到 pending_cmds(apply 完后回调)——
    self.append_proposal(apply.cbs.drain(..));

    // —— 关键 4:逐条 apply 这些 entries ——
    self.delegate.handle_raft_committed_entries(apply_ctx, entries.drain(..));
}
```

注意关键 2 那段有个 `panic!("commit state jump backward")`——Raft 的安全性保证 commit_index 单调递增,如果 apply 发现新的 commit_index 比旧的小,直接 panic(那是 Raft 正确性被破坏的 bug)。这是工程层面对 Raft 不变式的硬检查。

关键 4 是真正干活的地方——`handle_raft_committed_entries` 逐条处理 entries。

### handle_raft_committed_entries:逐条 entry 转 process_raft_cmd

看 [`handle_raft_committed_entries`](../tikv/components/raftstore/src/store/fsm/apply.rs#L1190):

```rust
// components/raftstore/src/store/fsm/apply.rs#L1190-L1230(简化示意)
fn handle_raft_committed_entries(
    &mut self,
    apply_ctx: &mut ApplyContext<EK>,
    mut committed_entries_drainer: Drain<'_, Entry>,
) {
    if committed_entries_drainer.len() == 0 {
        return;
    }
    apply_ctx.prepare_for(self);                    // ① 准备 WriteBatch(为这个 Region)
    apply_ctx.committed_count += committed_entries_drainer.len();
    let mut results = VecDeque::new();
    while let Some(entry) = committed_entries_drainer.next() {
        if self.pending_remove { break; }
        
        let expect_index = self.apply_state.get_applied_index() + 1;
        if expect_index != entry.get_index() {
            panic!("{} expect index {}, but got {}", ...);  // 连续性检查
        }

        // 按 entry 类型分发
        let res = match entry.get_entry_type() {
            EntryType::EntryNormal => self.handle_raft_entry_normal(apply_ctx, &entry),
            EntryType::EntryConfChange | EntryType::EntryConfChangeV2 => {
                self.handle_raft_entry_conf_change(apply_ctx, &entry)
            }
        };
        // ... 处理 res(Yield / WaitMergeSource 等)
    }
    self.finish_for(apply_ctx, results);            // ② 收尾(更新 apply_state、构造 ApplyRes)
}
```

三步:**prepare_for**(给这个 Region 准备一个 WriteBatch)、**逐条 apply**(每条 entry 调 `handle_raft_entry_normal` → `process_raft_cmd` → `apply_raft_cmd`,把命令的 Put/Delete 塞进 WriteBatch)、**finish_for**(收尾)。中间那个 `expect_index != entry.get_index()` 的 panic 是连续性检查——Raft 要求 apply 按 index 顺序,跳号就是 bug。

---

## 三、apply_raft_cmd:把命令的 Put/Delete 塞进 WriteBatch

### apply_raft_cmd:解码 + 分发

[`apply_raft_cmd`](../tikv/components/raftstore/src/store/fsm/apply.rs#L1514) 把一条 Raft 命令(RaftCmdRequest,protobuf 编码)解码,按内容分发:

- 普通写(Put/Delete)→ `exec_write_cmd` 塞进 WriteBatch;
- Get/Snap → 只读,apply 时直接读(快照读);
- Admin(Split/ChangePeer/...) → 各自的 `exec_split` / `exec_change_peer` / ...

### exec_write_cmd:真正写进 WriteBatch

看 [`exec_write_cmd`](../tikv/components/raftstore/src/store/fsm/apply.rs#L1936) 的核心:

```rust
// components/raftstore/src/store/fsm/apply.rs#L1928-L1955(摘录)
if !req.get_put().get_cf().is_empty() {
    let cf = req.get_put().get_cf();           // 命令指定了 CF(default/write/lock)
    if cf == CF_LOCK {
        self.metrics.lock_cf_written_bytes += key.len() as u64;
        self.metrics.lock_cf_written_bytes += value.len() as u64;
    }
    ctx.kv_wb.put_cf(cf, key, value).unwrap_or_else(|e| {
        panic!("{} failed to write ({}, {}) to cf {}: {:?}", ...);
    });
} else {
    ctx.kv_wb.put(key, value).unwrap_or_else(|e| { ... });   // 没指定 CF → default
}
```

**这就是 Apply 的物理落点**——`ctx.kv_wb.put_cf(cf, key, value)`,把一条 Put 命令变成 WriteBatch 里的一条 put_cf 操作。注意:

- **CF 是命令自带的**:`req.get_put().get_cf()` —— TiDB/scheduler 发命令时就指定了"这条 Put 写进哪个 CF"(prewrite 的 lock 写 lock CF、value 写 default CF;commit 的 Write 写 write CF,P4-13/14 详拆)。
- **key 已经是 MVCC 编码**:`key` 变量在这里已经是 `data_prefix + memcomparable(user_key) + desc(ts)` 的完整编码(P3-10 讲过),由上层(scheduler/txn 层)组装好。
- **没真正写 RocksDB**:`put_cf` 只是往 WriteBatch 里**追加**一条记录,WriteBatch 还在内存里。真正的 RocksDB 写要等 `commit`/`flush`(下一节)。

> **钉死这件事**:Apply 的核心动作就是 `ctx.kv_wb.put_cf(cf, key, value)`——把 Raft 命令里的 Put/Delete,按指定的 CF、用 MVCC 编码的 key,塞进 WriteBatch。这一步**不写 RocksDB**,只是攒。真正的写在 WriteBatch 提交时。

### Delete 也类似

delete_cf 在 [apply.rs#L1978](../tikv/components/raftstore/src/store/fsm/apply.rs#L1978),同样塞进 WriteBatch。DeleteRange 比较特殊(下面讲)。

---

## 四、cmd batch:攒批写 RocksDB 的核心

上一节看到 `put_cf` 只是往 WriteBatch 里塞,那 WriteBatch 什么时候真正写 RocksDB?这就是 **cmd batch** 的核心——攒够一定条件就 flush。

### WriteBatch:RocksDB 的批量写原语

WriteBatch 是 RocksDB 的标准能力(承接《LevelDB》)——一组 put/delete 操作打包成一个原子 batch,一次性写 RocksDB。它的好处:① **原子性**(一个 batch 要么全成功要么全失败);② **吞吐高**(一次 WAL 写 + 一次 MemTable 写,摊薄固定开销)。

ApplyFsm 复用 WriteBatch 做攒批。看 [`ApplyContext` 的 kv_wb 字段](../tikv/components/raftstore/src/store/fsm/apply.rs#L496):

```rust
// components/raftstore/src/store/fsm/apply.rs#L496(在 ApplyContext::new 里)
let kv_wb = engine.write_batch_with_cap(DEFAULT_APPLY_WB_SIZE);
```

`DEFAULT_APPLY_WB_SIZE = 4 * 1024`(4KB,初始容量,见 [apply.rs#L100](../tikv/components/raftstore/src/store/fsm/apply.rs#L100))。ApplyContext 持有一个 `kv_wb: EK::WriteBatch`,所有 Region 的 apply 都往这同一个 WriteBatch 里塞(注意:是 **Apply 线程级别共享**的,不是每个 Region 一个——这样能跨 Region 攒批,提高吞吐)。

### prepare_for / commit / finish_for:Region 级别的边界

虽然 WriteBatch 是线程级共享的,但 Apply 要**按 Region 区分边界**——因为每个 Region 的 apply 完成要单独通知 PeerFsm(更新各自的 apply_index)。看三个方法:

```rust
// components/raftstore/src/store/fsm/apply.rs#L547-L551
pub fn prepare_for(&mut self, delegate: &mut ApplyDelegate<EK>) {
    self.applied_batch.push_batch(&delegate.observe_info, delegate.region.get_id());
    self.kv_wb.prepare_for_region(&delegate.region);    // 标记 WriteBatch 里这个 Region 的起点
}

// components/raftstore/src/store/fsm/apply.rs#L558-L563
pub fn commit(&mut self, delegate: &mut ApplyDelegate<EK>) {
    if delegate.last_flush_applied_index < delegate.apply_state.get_applied_index() {
        delegate.maybe_write_apply_state(self);          // 把 apply_state 写进 WriteBatch
    }
    self.commit_opt(delegate, true);                     // 持久化(persistent=true)
}

// finish_for 在每个 Region 的 entries 处理完后调用,更新 apply_state
```

`prepare_for` 标记"这个 Region 的写要开始了",`commit` 把这个 Region 的 apply_state(记录 apply 到哪个 index 了)也写进 WriteBatch 并提交,`finish_for` 更新 Region 的 apply 进度。

### 攒批的触发条件:should_write_to_engine

关键问题:什么时候 flush?看 [apply.rs#L1332-L1348](../tikv/components/raftstore/src/store/fsm/apply.rs#L1332)(在 handle_raft_entry_normal 里,每处理完一条命令检查一次):

```rust
// components/raftstore/src/store/fsm/apply.rs#L1332-L1348(简化示意)
let has_unflushed_data =
    self.last_flush_applied_index != self.apply_state.get_applied_index();
if (has_unflushed_data
    && should_write_to_engine(!apply_ctx.kv_wb().is_empty(), &cmd)   // 命令类型要求 flush
    || apply_ctx.kv_wb().should_write_to_engine())                   // WriteBatch 攒够了
    && apply_ctx.host.pre_persist(&self.region, false, Some(&cmd))
{
    apply_ctx.commit(self);                                            // 提交 WriteBatch
    if self.metrics.written_bytes >= apply_ctx.yield_msg_size         // 写太多了
        || self.handle_start.elapsed() >= apply_ctx.yield_duration   // 干太久了
    {
        return ApplyResult::Yield;                                     // 让出 CPU,下轮继续
    }
}
```

两个 flush 触发条件(满足任一):

**条件一:命令类型要求 flush**。看 [`should_write_to_engine`](../tikv/components/raftstore/src/store/fsm/apply.rs#L880):

```rust
// components/raftstore/src/store/fsm/apply.rs#L880-L904(摘录)
fn should_write_to_engine(has_pending_writes: bool, cmd: &RaftCmdRequest) -> bool {
    if cmd.has_admin_request() {
        match cmd.get_admin_request().get_cmd_type() {
            AdminCmdType::ComputeHash |              // ComputeHash 要快照,先 flush
            AdminCmdType::CommitMerge |              // Merge 要最新 apply index,先 flush
            AdminCmdType::RollbackMerge => return true,
            _ => {}
        }
    }
    for req in cmd.get_requests() {
        if req.has_delete_range() { return true; }   // DeleteRange 要独占,先 flush
        if req.has_ingest_sst() && has_pending_writes { return true; }  // IngestSst 前先 flush
    }
    false
}
```

这些命令**必须先 flush 现有 WriteBatch** 才能执行——因为它们需要一个干净的快照(ComputeHash)、或最新的 apply index(CommitMerge)、或独占访问(DeleteRange/IngestSst)。

**条件二:WriteBatch 攒够了**。看 [`RocksWriteBatch::should_write_to_engine`](../tikv/components/engine_rocks/src/write_batch.rs#L149):

```rust
// components/engine_rocks/src/write_batch.rs#L149-L155
fn should_write_to_engine(&self) -> bool {
    if self.support_write_batch_vec {
        self.index >= WRITE_BATCH_MAX_BATCH_NUM       // 16(多 batch 写模式)
    } else {
        self.wbs[0].count() > RocksEngine::WRITE_BATCH_MAX_KEYS   // 256 条
    }
}
```

**默认 256 条 key 就 flush**(`WRITE_BATCH_MAX_KEYS = 256`,见 [write_batch.rs#L16](../tikv/components/engine_rocks/src/write_batch.rs#L16))。这是个甜点——太少(比如 16)每条命令都 flush,攒批失效;太多(比如 4096)WriteBatch 膨胀占内存、单次写延迟变长。256 是 RocksDB WriteBatch 的常见上限。

> **钉死这件事**:cmd batch 的攒批触发有两类——**WriteBatch 攒够 256 条**(默认,吞吐导向)或**遇到特殊命令(ComputeHash/CommitMerge/DeleteRange/IngestSst)**(正确性导向,这些命令需要干净状态)。这个"攒批 + 特殊命令强制 flush"的组合,是 Apply 在吞吐和正确性之间的工程权衡。

### write_to_db:真正写 RocksDB

触发 flush 后,调 `commit` → `commit_opt` → [`write_to_db`](../tikv/components/raftstore/src/store/fsm/apply.rs#L581):

```rust
// components/raftstore/src/store/fsm/apply.rs#L581-L655(简化示意)
pub fn write_to_db(&mut self) -> (bool, Option<SequenceNumber>) {
    let need_sync = self.sync_log_hint && !self.disable_wal;
    // —— 关键 1:先 ingest 待处理的 SST(如果有的话)——
    if !self.pending_ssts.is_empty() {
        self.importer.ingest(&self.pending_ssts, &self.engine).unwrap();
        self.pending_ssts = vec![];
    }
    // —— 关键 2:WriteBatch 非空,写 RocksDB ——
    if !self.kv_wb_mut().is_empty() {
        let mut write_opts = WriteOptions::new();
        write_opts.set_sync(need_sync);               // 是否 sync WAL(取决于 sync_log_hint)
        let seq = self.kv_wb_mut().write_opt(&write_opts).unwrap();   // 真正写!
        // —— 关键 3:写完后,根据 WriteBatch 大小决定复用还是重建 ——
        let data_size = self.kv_wb().data_size();
        if data_size > APPLY_WB_SHRINK_SIZE {         // 1MB,WriteBatch 太大了
            let kv_wb = self.engine.write_batch_with_cap(DEFAULT_APPLY_WB_SIZE);  // 重建一个
            self.kv_wb = self.host.on_create_apply_write_batch(kv_wb);
        } else {
            self.kv_wb_mut().clear();                 // 清空复用(省内存分配)
        }
    }
    // —— 关键 4:取出 callback,准备回调 ——
    let ApplyCallbackBatch { cmd_batch, cb_batch, .. } =
        mem::replace(&mut self.applied_batch, ApplyCallbackBatch::new());
    // ...
    (need_sync, seqno)
}
```

四个关键点:

1. **先 ingest SST**:如果有待处理的 SST 文件(Import 场景,sst_importer),先 ingest,保证顺序(ingest 的数据要先于 WriteBatch 的写)。
2. **`kv_wb.write_opt(&write_opts)`**:这一行是真正的 RocksDB 写!WriteBatch 里攒的所有 put_cf/delete_cf,一次性写进 RocksDB 的 MemTable + WAL。
3. **WriteBatch 复用 vs 重建**:写完后,如果 WriteBatch 膨胀到 >1MB(`APPLY_WB_SHRINK_SIZE = 1MB`,见 [apply.rs#L101](../tikv/components/raftstore/src/store/fsm/apply.rs#L101)),就重建一个小的(控制内存);否则 `clear()` 复用(省内存分配开销)。
4. **callback 取出准备回调**:写完后,把 `ApplyCallbackBatch` 里攒的 callback 取出来,准备回调客户端。

`need_sync`(是否 sync WAL)由 `sync_log_hint` 决定——这个 hint 在 [`should_sync_log`](../tikv/components/raftstore/src/store/fsm/apply.rs#L920) 里设置:admin 命令(除 CompactLog/ComputeHash/TransferLeader 外)和 IngestSst 要求 sync WAL(保证崩溃不丢)。普通 Put/Delete 默认不 sync(性能优先,靠 Raft 的多数派保证不丢)。

> **技巧(how)**:WriteBatch 写完后**根据大小决定复用还是重建**,是个精细的内存管理——复用省分配开销(常见情况),重建防止 WriteBatch 无限膨胀(异常情况)。这个 `APPLY_WB_SHRINK_SIZE = 1MB` 的阈值,是内存占用和分配开销之间的甜点。承接《LevelDB》——WriteBatch 的复用是 LSM 引擎的标准优化,这里 TiKV 在 Apply 层做了一层封装。

---

## 五、apply 完怎么通知:ApplyRes 和 callback

写完 RocksDB,ApplyFsm 要做两件通知:**通知 PeerFsm**(更新 apply 进度)+ **通知客户端**(写落盘了)。

### 通知 PeerFsm:ApplyRes 经 Notifier

看 [`flush`](../tikv/components/raftstore/src/store/fsm/apply.rs#L795) 方法(在每轮 Apply 结束时调):

```rust
// components/raftstore/src/store/fsm/apply.rs#L795-L830(简化示意)
pub fn flush(&mut self) -> bool {
    let t = match self.timer.take() { Some(t) => t, None => return false };
    let (is_synced, _) = self.write_to_db();         // 先把 WriteBatch 写 RocksDB

    // —— 关键:把 ApplyRes 发回给 PeerFsm ——
    if !self.apply_res.is_empty() {
        let apply_res = mem::take(&mut self.apply_res);
        self.notifier.notify(apply_res);             // Notifier 把 ApplyRes 投递给 PeerFsm
    }
    // ... metrics、slow_log
    is_synced
}
```

`ApplyRes` 里带什么?主要是:

- **region_id**:哪个 Region 的 apply 结果;
- **applied_index**:apply 到哪个 Raft log index 了(PeerFsm 据此更新自己的 apply 进度,知道"可以截断 index 之前的日志了");
- **exec_result**:执行结果(Split/ChangePeer 的结果,PeerFsm 要做后续处理);
- **bucket_meta**(如果有):Region bucket 的更新。

`Notifier` 是个 trait,实现者(RaftPoller 里)把 ApplyRes 包装成 `PeerMsg::ApplyRes` 投递到对应 Region 的 PeerFsm 信箱。PeerFsm 收到后更新自己的 `apply_state`,触发后续动作(比如 apply 到某个 index 了,可以给客户端回 callback、可以推进 Raft 的 commit_index)。

### 通知客户端:ApplyCallbackBatch

客户端的 callback 怎么挂上去的?回顾 P2-05:scheduler 发起写时,把 callback 挂在 RaftCmdRequest 上,经 `engine.async_write → router.send_command → PeerFsm propose`。Raft commit 后,这个 callback 随着 committed_entries 一起传给 ApplyFsm。ApplyFsm 在 [`append_proposal`](../tikv/components/raftstore/src/store/fsm/apply.rs#L4181) 里把 callback 挂到 `pending_cmds`:

```rust
// components/raftstore/src/store/fsm/apply.rs#L4191-L4204(摘录)
for p in props_drainer {
    let cmd = PendingCmd::new(p.index, p.term, p.cb);   // 把 callback 按 (index, term) 挂起来
    if p.is_conf_change {
        // ... conf change 特殊处理
    } else {
        self.delegate.pending_cmds.append_normal(cmd);
    }
}
```

apply 完一条命令后,在 [`process_raft_cmd`](../tikv/components/raftstore/src/store/fsm/apply.rs#L1458) 里 `find_pending(index, term)` 找到对应的 callback,塞进 `applied_batch`:

```rust
// components/raftstore/src/store/fsm/apply.rs#L1491-L1494(摘录)
let cmd_cb = self.find_pending(index, term, is_conf_change_cmd(&cmd.request));
apply_ctx.applied_batch.push(cmd_cb, cmd, &self.observe_info, self.region_id());
```

`write_to_db` 写完 RocksDB 后,把 `applied_batch` 里的 callback 全部触发——callback 里是 scheduler 当初挂的 `on_applied`闭包,它会通过 scheduler 把响应经 gRPC 回给 TiDB。

> **钉死这件事(承接 P2-05)**:callback 是一条贯穿五步流水线的"回信地址"——scheduler 挂上 → PeerFsm propose 携带 → committed_entries 传给 ApplyFsm → apply 完写完 RocksDB 后触发。这是异步系统的标准技巧:发起者不阻塞等结果,挂个回信地址,完成者按地址回信。**Apply 是这条 callback 链路的终点**——apply 完触发 callback,客户端才知道"写真的落盘了"。

---

## 六、admin 命令的特殊 apply

普通 Put/Delete 的 apply 很直接(塞 WriteBatch),但 admin 命令(Split/ChangePeer/TransferLeader/CompactLog/CommitMerge 等)的 apply 各有特殊处理:

| Admin 命令 | apply 时的特殊处理 | 源码 |
|-----------|---------------------|------|
| **Split / BatchSplit** | 切分 Region 元数据(边界、epoch version+新 Region 数)、创建新 Region 的本地数据 | `exec_batch_split` (P2-08 拆透) |
| **ChangePeer / ChangePeerV2** | 更新 Region 的副本配置(conf_ver+1)、通知 PD | `exec_change_peer` |
| **TransferLeader** | apply 时记录(实际切主在 Raft 层),apply 后 PeerFsm 推进 | `should_sync_log` 里 TransferLeader 不 sync |
| **CompactLog** | 触发 RaftEngine 的日志 GC(截断 index 之前的日志) | `exec_compact_log` |
| **ComputeHash / VerifyHash** | 强制 flush(要快照),算 Region 数据的 hash 做一致性检查 | `should_write_to_engine` 强制 flush |
| **CommitMerge / RollbackMerge** | 强制 flush(要最新 apply index),合并/回滚 Region | `should_write_to_engine` 强制 flush |

注意 [`should_sync_log`](../tikv/components/raftstore/src/store/fsm/apply.rs#L920) 里有个细节:CompactLog、ComputeHash、VerifyHash、TransferLeader **不要求 sync WAL**——因为 CompactLog 只是触发 GC(不影响数据)、ComputeHash/VerifyHash 是只读、TransferLeader 不改数据。其他 admin 命令(Split、ChangePeer、CommitMerge)**要求 sync WAL**——因为它们改的是元数据,崩溃丢了会导致 Region 状态不一致。

> **钉死这件事**:admin 命令的 apply 比普通写复杂得多——它们改 Region 元数据、触发日志 GC、要求特殊的一致性保证(sync WAL、强制 flush)。本章只给全貌,具体每种 admin 的细节在对应章节(Split 在 P2-08、ChangePeer 在 P5-18)。

---

## 七、yield:一批太大要分多轮

Apply 还有个重要机制——**yield(让出)**。看 [apply.rs#L1340-L1348](../tikv/components/raftstore/src/store/fsm/apply.rs#L1340):

```rust
apply_ctx.commit(self);
if self.metrics.written_bytes >= apply_ctx.yield_msg_size       // 写太多了(默认 ~32MB)
    || self.handle_start.elapsed() >= apply_ctx.yield_duration  // 干太久了(默认几十 ms)
{
    return ApplyResult::Yield;                                    // 让出 CPU
}
```

一个 Region 的 apply,如果连续写了太多字节(`yield_msg_size`,配置项 `apply_yield_write_size`)或耗时太长(`yield_duration`,配置项 `apply_yield_duration`),就主动 yield——让出 Apply 线程,让别的 Region 的 ApplyFsm 也能跑。

这个机制防止"一个大 Region(比如正在 bulk load)独占 Apply 线程,其他 Region 全饿死"。yield 后,这个 Region 的 ApplyFsm 会被放回全局队列,下一轮继续从中断处 apply。这就是 `handle_normal` 里那段 `yield_state` 处理的来源——下一轮进来时先 `resume_pending` 接着干。

> **钉死这件事(承接 P1-04)**:yield 是 batch-system 公平性的体现——一个 FSM 不能独占线程太久。P1-04 讲过 Raft 线程的"热点 FSM 半数 reschedule",Apply 的 yield 是同类机制——写太多/干太久就让出,让所有 Region 的 Apply 都能进展。没有 yield,一个大 Region 的 apply 能卡住整个 Apply 线程,其他 Region 的客户端 callback 全部超时。

---

## 八、技巧精解:两个最硬核的工程技巧

本章挑两个最值得拆透的技巧。

### 技巧一:cmd batch——跨 Region 共享 WriteBatch 攒批

Apply 性能的核心是 **cmd batch**:多条已 commit 的 Raft 命令,攒进**同一个 WriteBatch**,一次性写 RocksDB。但这里有个反直觉的细节——**WriteBatch 是 Apply 线程级别共享的,不是每个 Region 一个**。

看 `ApplyContext`(线程级)持有 `kv_wb`,而 `ApplyFsm`(Region 级)不持有。这意味着:一个 Apply 线程,一轮 poll 里会处理多个 Region 的 ApplyFsm,**这些 Region 的写都塞进同一个 WriteBatch**。最后一次性写 RocksDB。

**为什么不每个 Region 一个 WriteBatch?** 因为那样攒批效果差——一个 Region 一轮可能就几条命令,WriteBatch 攒不到 256 条就 flush,批量优势没了。跨 Region 共享 WriteBatch,能把多个 Region 的零散写攒成一个大 batch,RocksDB 写吞吐拉满。

**怎么区分 Region 边界?** `prepare_for` / `finish_for` 在 WriteBatch 里标记每个 Region 的起点和终点(用 `prepare_for_region` 在 WriteBatch 内部记录 region 边界)。写完后,`ApplyCallbackBatch` 按 Region 分桶,把 callback 和 ApplyRes 分别发回各自的 PeerFsm。

> **反面对比**:如果每个 Region 一个 WriteBatch,一个 Apply 线程一轮处理 10 个 Region,就是 10 次 RocksDB 写(每个 Region flush 一次)。跨 Region 共享,可能是 1-2 次 RocksDB 写(攒够 256 条才 flush)。对于小事务(每个 Region 几条命令),这个优化能把 RocksDB 写次数降一个数量级。这是 TiKV 写吞吐的关键技巧之一。

> **钉死这件事**:cmd batch 的精髓是**跨 Region 攒批**——一个 Apply 线程的所有 Region 的写,共享一个 WriteBatch,攒够阈值(256 条 / 特殊命令)才 flush。这把"N 条命令 = N 次 RocksDB 写"变成"N 条命令 = 1 次 RocksDB 写(可能跨 Region)",写吞吐提升一个数量级。

### 技巧二:WriteBatch 的复用 vs 重建

`write_to_db` 写完后,有个看似无关紧要但实则是精细内存管理的细节——根据 WriteBatch 的大小决定**复用还是重建**(见 [apply.rs#L633-L642](../tikv/components/raftstore/src/store/fsm/apply.rs#L633)):

```rust
let data_size = self.kv_wb().data_size();
if data_size > APPLY_WB_SHRINK_SIZE {                       // 1MB
    // WriteBatch 太大了,重建一个小的(控制内存)
    let kv_wb = self.engine.write_batch_with_cap(DEFAULT_APPLY_WB_SIZE);  // 4KB
    self.kv_wb = self.host.on_create_apply_write_batch(kv_wb);
} else {
    // 清空复用(省内存分配)
    self.kv_wb_mut().clear();
}
```

**为什么这么细?** WriteBatch 是个会增长的 buffer(put_cf 往里塞数据,它会扩容)。如果一直复用同一个 WriteBatch,它可能膨胀到几十 MB(攒了很多大 value),占内存。但如果每次都重建,内存分配/释放的开销又大。

TiKV 的策略:

- **常见情况(WriteBatch < 1MB)**:`clear()` 复用——清空数据但保留 buffer 容量,下次直接用,省分配开销;
- **异常情况(WriteBatch ≥ 1MB)**:重建一个 4KB 的小的——释放掉膨胀的 buffer,控制内存。

这个 1MB 阈值(`APPLY_WB_SHRINK_SIZE`)是个甜点——既不让 WriteBatch 无限膨胀,又避免频繁分配。这是大型系统里"buffer 复用"的经典技巧,对标《Tokio》里 task 的 buffer 池化、《LevelDB》里 WriteBatch 的复用。

> **钉死这件事**:WriteBatch 的"复用 vs 重建"看似微小,实则是个精细的内存/性能权衡。常复用省分配,异常重建控内存,1MB 是甜点。这种"在 hot path 上精细管理 buffer"是高吞吐系统的必备技巧——Apply 是 TiKV 最热的写路径之一,这里每一点优化都被放大百万倍。

---

## 九、架构演进:v1 单 Apply batch-system vs v2 多线程 Apply

本章讲的是经典 raftstore(v1)的 Apply:一个 store 一个 Apply batch-system(默认 2 个 Apply 线程),所有 Region 的 ApplyFsm 共享。这个模型的天花板和 Raft batch-system 一样——**单个 Region 的 Apply 无法利用多核**。一个大 Region(比如 256MB 热点)的 Apply,只能跑在 2 个 Apply 线程中的一个上。

raftstore-v2 打破了这点:Apply 也多线程化(更细粒度的并行,Region 分组到不同 Apply 线程)。配合 per-Region tablet(v2 每个 Region 一个 RocksDB),Apply 可以真正做到多 Region 并行写不同的 RocksDB 实例,没有 v1 的"共享 RocksDB 写锁"争抢。

但 v1 的 cmd batch 机制——跨 Region 攒批、WriteBatch 复用、yield 公平——是 v2 的基础。v2 在这套基础上加多线程,突破"单 Region Apply 无法多核"的天花板。本书主线讲 v1,因为它最能讲清 Apply 的机制;v2 是演进方向。

> **架构演进**:v1 → v2 在 Apply 层的演进,是从"一个 Apply batch-system"扩展成"多线程并行 Apply"。cmd batch、WriteBatch 复用、yield 这些机制都保留,只是并行度更高。理解 v1 的 Apply 是理解 v2 的前提。

---

## 十、章末小结

### 回扣主线

本章是 P2-05 五步流水线的**第五步(Apply)的落点**——ApplyFsm 把 Raft commit 的命令,按 MVCC 编码(P3-10)写进 RocksDB 的三个 CF(P3-09)。Apply 在独立的 batch-system 里跑(和 PeerFsm 分开,快慢分离),把多条命令攒进 WriteBatch(cmd batch,跨 Region 共享,默认 256 条 flush),一次性写 RocksDB,写完通过 ApplyRes 通知 PeerFsm、通过 callback 通知客户端。

回到二分法:**复制层 vs 事务层**。Apply 是**复制层的终点**——Raft 把命令 commit(Apply 的输入),Apply 把命令落盘(复制层的最后一步);但它写的 MVCC 数据(P3-10)是**事务层**的物理载体。所以 Apply 是复制层和事务层的**接地点**——事务层(scheduler)发起写,经复制层(Raft + Apply)落盘,Apply 完回调通知事务层(scheduler → 客户端)。这条链路是第 4 篇(Percolator 事务)的物理基础。

本章承上启下:**承上**——P1-04 立 batch-system 骨架、P2-05 立五步流水线、P3-09 立 RocksDB 三 CF、P3-10 立 MVCC 编码,本章把它们在 Apply 这一环串起来;**启下**——apply 落盘后,事务层怎么用这套落盘的数据做 prewrite/commit?第 4 篇(P4-12 事务模型全景)开始拆 Percolator 两阶段提交。

### 五个为什么

1. **为什么 Apply 要单独拆成 ApplyFsm,不在 PeerFsm 里做?**——Apply 是慢操作(写 RocksDB),如果和 Raft 推进(PeerFsm)混在一个线程,慢 Apply 会阻塞 Raft 心跳,触发误选举。拆开让快(Raft)慢(Apply)分离,各跑各的(承接 P1-04)。
2. **为什么 cmd batch 要跨 Region 共享 WriteBatch?**——一个 Region 一轮可能就几条命令,单独攒批效果差。跨 Region 共享能把多个 Region 的零散写攒成大 batch,把"N 条命令 = N 次 RocksDB 写"变成"N 条命令 = 1 次 RocksDB 写",写吞吐提升一个数量级。
3. **为什么 WriteBatch 攒够 256 条(`WRITE_BATCH_MAX_KEYS`)就 flush?**——太少(16)攒批失效,太多(4096)WriteBatch 膨胀占内存且单次写延迟变长。256 是 RocksDB WriteBatch 的甜点,平衡吞吐和延迟。
4. **为什么有些 admin 命令(ComputeHash/CommitMerge/DeleteRange)要强制 flush?**——它们需要干净的快照(ComputeHash)、或最新的 apply index(CommitMerge)、或独占访问(DeleteRange/IngestSst)。强制 flush 保证这些命令执行时 WriteBatch 里没有半截数据干扰。
5. **为什么 apply 完要 yield(让出)?**——防止一个大 Region(bulk load)独占 Apply 线程,其他 Region 饿死。写太多(`yield_msg_size`)或干太久(`yield_duration`)就让出,让所有 Region 的 Apply 都能进展。这是 batch-system 公平性的体现。

### 想继续深入往哪钻

- **batch-system 框架本身**(Poller::poll、handle_normal、FsmState 三态):读 P1-04,本章引用但未重复。
- **五步流水线的完整旅程**(Propose → Append → Replicate → Commit → Apply):读 P2-05,本章是第五步的展开。
- **RocksDB 的 WriteBatch 原语**(原子性、WAL、MemTable 写):读《LevelDB》对应章节(本章承接不重复)。
- **admin 命令的 apply 细节**:Split 读 P2-08、ChangePeer/调度读 P5-18。
- **apply 完后事务层怎么用数据**(prewrite/commit):读第 4 篇 P4-12~15。
- **关键源码文件**:
  - [`components/raftstore/src/store/fsm/apply.rs`](../tikv/components/raftstore/src/store/fsm/apply.rs)(本章主角:`ApplyFsm`、`ApplyContext`、`ApplyDelegate`、`handle_normal`、`handle_apply`、`handle_raft_committed_entries`、`process_raft_cmd`、`apply_raft_cmd`、`exec_write_cmd`、`should_write_to_engine`、`should_sync_log`、`prepare_for`、`commit`、`write_to_db`、`flush`、`append_proposal`)
  - [`components/engine_rocks/src/write_batch.rs`](../tikv/components/engine_rocks/src/write_batch.rs)(`RocksWriteBatch`、`should_write_to_engine`、`WRITE_BATCH_MAX_KEYS=256`、`WRITE_BATCH_MAX_BATCH_NUM=16`)
  - [`components/raftstore/src/store/fsm/store.rs`](../tikv/components/raftstore/src/store/fsm/store.rs)(`create_apply_batch_system`、`ApplyPoller`、`Builder`)
  - [`components/engine_traits/src/write_batch.rs`](../tikv/components/engine_traits/src/write_batch.rs)(`WriteBatch` trait、`should_write_to_engine`)

### 引出下一篇

讲完了第 3 篇(RocksDB + MVCC 编码 + Apply),我们已经把"Raft commit 的命令怎么落盘"的全链路拆透了——RocksDB 三 CF(P3-09)、MVCC key+ts 编码(P3-10)、ApplyFsm 批量 apply(P3-11)。接下来第 4 篇,我们从"复制层的终点(Apply)"走向"事务层"——Raft 只保证单 Region 一致,但一个事务可能改多个 Region(跨多个 Raft 组),怎么 ACID?答案:Percolator 两阶段提交。下一章 P4-12,我们拆事务模型全景——scheduler 怎么调度、latch 怎么做行锁、乐观 vs 悲观双引擎,这是 Percolator 在 TiKV 落地的工程框架。

> **下一章**:[P4-12 · 事务模型全景:scheduler + latch + 双引擎](P4-12-事务模型全景-scheduler-latch-双引擎.md)
