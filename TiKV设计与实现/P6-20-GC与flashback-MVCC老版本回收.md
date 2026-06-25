# 第 6 篇 · 第 20 章 · GC 与 flashback:MVCC 老版本回收

> **核心问题**:MVCC 让 TiKV 能做快照读、能扛住高并发——代价是**每改一次 key,就在 write CF 里多留一条提交记录、在 default CF 里可能多留一份 value**。一个被频繁更新的热点 key,几天下来可能有几万个老版本堆在 RocksDB 里。这些老版本对当前事务没用了(读的时候只取最新或某个 start_ts 的版本),但它们占着磁盘、拖慢 scan(每次扫都要跳过一堆垃圾版本)、撑大 SST 文件。TiKV 必须定期把它们清掉,这件事叫 **GC(Garbage Collection)**。但 GC 又不能乱清——清错了会把还在被读的版本删掉、破坏快照隔离。本章拆 TiKV 怎么定一个"安全点"把它之前的版本安全清掉,以及新特性 flashback 怎么闪回到历史。

> **读完本章你会明白**:
> 1. MVCC 的老版本是怎么堆积的(为什么 GC 是必需品,不是优化项)——不清,磁盘会被垃圾版本撑爆、scan 会被慢死。
> 2. GC 的核心是 **safe point**:一个时间戳,表示"比它早的版本,没人会再读了,可以清"。这个 safe point 怎么算出来、由谁推进。
> 3. TiKV 有**两套** GC 执行路径:**active GC**(gc_worker 主动扫 key 删老版本)和 **compaction filter**(借用 RocksDB compaction 顺手删,9.x 的主线)——为什么后者更优。
> 4. 单 key 的 GC 是个精巧的**三态状态机**(Rewind → RemoveIdempotent → RemoveAll),它怎么保证"删老版本但不删最新提交"。
> 5. flashback 不是"备份恢复",而是**用 MVCC 的旧版本当新数据重新写一遍**——它复用 prewrite/commit 的机制,把数据"重放"到指定 version。

> **如果一读觉得太难**:先只记住三件事——① GC 清的是 MVCC 的老版本,清的边界叫 safe point(safe point 之前的版本可清);② TiKV 9.x 主用 compaction filter 借 RocksDB 压缩顺手删 GC(老资料讲的纯 active GC 已是辅助);③ flashback 本质是"把指定 version 的数据当新数据 prewrite+commit 一遍",不是备份回滚。

---

## 〇、一句话点破

> **GC 干的事是"清掉 safe point 之前、没人再读的 MVCC 老版本",而真正动刀子的不是单独的清理任务,是借用 RocksDB compaction 时经过的每个 key 顺手判断"这个老版本能删吗"——把 GC 这个大活摊薄到 compaction 的正常流程里;flashback 则是"用旧 version 当新数据走一遍 prewrite/commit",复用事务机制而不是另起一套回滚逻辑。**

这是结论,不是理由。本章倒过来拆:先讲 MVCC 老版本怎么堆积(GC 的动机),再讲 safe point 是什么、怎么算,再讲两套 GC 执行路径和单 key 的三态状态机,接着讲 compaction filter 的工程精妙,最后讲 flashback。

---

## 一、MVCC 的老版本是怎么堆积的

### 每次写都在留"考古层"

回顾 P3-10 讲的 MVCC 编码:TiKV 把一个 key 的多个版本,编码成 `key + commit_ts` 排在 RocksDB 里(同一个 key 的不同版本按 commit_ts 倒排——commit_ts 越大,编码后字典序越小,排在越前面)。具体落在三个 CF:

- **write CF**:提交记录。每次 commit 一个 key,就在 write CF 写一条 `(key_commit_ts → Write{type, start_ts, short_value})`。
- **default CF**:大 value。如果 value 比较长(超过 `SHORT_VALUE_MAX_LEN`,64 字节左右),value 不存在 write CF 里,而是写在 default CF,键是 `(key_start_ts → value)`,write CF 里的记录指向它。
- **lock CF**:未提交事务的锁(提交完就清掉,GC 一般不管 lock CF)。

```
   一个 key 被 5 次更新(commit_ts 分别是 t1<t2<t3<t4<t5),write CF 里:
   ┌──────────────────────────────────────────────────────┐
   │ key_t5 → Write{Put, start_ts=t5', ...}   ← 最新      │
   │ key_t4 → Write{Put, start_ts=t4', ...}               │
   │ key_t3 → Write{Put, start_ts=t3', ...}               │
   │ key_t2 → Write{Put, start_ts=t2', ...}               │
   │ key_t1 → Write{Put, start_ts=t1', ...}   ← 最老      │
   └──────────────────────────────────────────────────────┘
   default CF 里同样堆着 t1'~t5' 各自的 value(如果不短)
```

一个事务读这个 key 时(假设 start_ts = t3),扫 write CF 找到 ≤ t3 的最大 commit_ts 版本(t3 那条),拿到 value。**t1、t2 对这个事务是没用的垃圾——但它们还躺在 RocksDB 里占地方**。

### 不清会怎样:三重灾难

> **不这样会怎样**:
> 1. **磁盘爆炸**:一个热点账户每天被转几次账,一年就是上千个版本。一张几亿行的表,版本数轻易翻几十倍,磁盘占用无界增长。
> 2. **scan 被慢死**:一次 `SELECT * FROM t WHERE ...` 要扫 write CF,每个 key 都要跳过它的一堆老版本才能拿到当前版本。版本越多,scan 越慢——哪怕你只要最新值。
> 3. **SST 文件臃肿**:RocksDB 的 SST 文件塞满老版本,Bloom filter 命中率下降(查到的是老版本)、block cache 命中率下降(有效数据被稀释)、compaction 本身也变慢(要搬动的数据多了)。

所以 GC 不是"优化",是 MVCC 能持续运行的**必需品**。不清,系统会被自己的考古层压垮。

> **钉死这件事**:MVCC 用空间换隔离性(不阻塞读,但留多个版本)。GC 是这个交易的另一面——把"换出去的空间"定期收回来。理解 GC 的起点是:它清的是**已经没人会再读的旧版本**,而"没人会再读"的判定标准,就是 safe point。

---

## 二、safe point:GC 的安全边界

### 什么是 safe point

**safe point**(GC safe point)是一个时间戳(TimeStamp)。它的语义是:**所有 start_ts > safe_point 的事务,都不应该再读到 safe_point 之前的版本**——所以 safe_point 之前的旧版本,可以被 GC 清掉。

为什么这个语义成立?因为 MVCC 读的规则是"找 ≤ start_ts 的最大 commit_ts 版本"(P4-15 讲过)。如果一个事务的 start_ts 严格大于 safe_point,它读最新版本时,拿到的一定是 commit_ts 在 (safe_point, start_ts] 之间的某个版本——这个版本比 safe_point 新,**不会**是 safe_point 之前的旧版本。所以 safe_point 之前的旧版本,对任何活跃事务都没用,可以清。

```
   时间轴(commit_ts):
   ─────┬──────────┬──────────────────────┬──────────▶
      t_老       safe_point             现在

   safe_point 之前的版本 → 可被 GC 清掉(没有活跃事务会读它们)
   safe_point 之后的版本 → 必须保留(活跃事务可能要读)
```

> **钉死这件事**:safe_point 是 GC 正确性的根。GC 清掉的版本,必须满足"没有任何活跃事务会读它"。safe_point 通过"比所有活跃事务的 start_ts 都小"来保证这一点。所以 safe_point 怎么算,本质是"算出当前集群里,最老的还没结束的读事务的 start_ts 是多少"——比它再老一点的,就是 safe_point。

### safe_point 怎么算出来

safe_point 不是 TiKV 自己定的,是 **PD** 算出来告诉 TiKV 的。PD 综合几个来源取最小值:

1. **GC 的 TTL**:TiDB 周期性(默认每 10 分钟)发起一次 GC,记录一个 `gc_safe_point`。它告诉 TiKV"清到这个时间点"。
2. **TiDB CDC / BR 等下游消费者的最低需求**:CDC(变更捕获,P6-21 讲)需要持续读旧版本推给下游;BR(备份恢复)需要读一个一致快照。这些组件会向 PD 注册自己需要的最低 safe_point,PD 取所有注册值的下界,确保不破坏它们。
3. **resolved_ts**:`components/resolved_ts/` 算出的"这个时间点之前的数据都已稳定"的时间戳(承接 P5-17),也是 safe_point 的一个参考。

PD 把最终算出的 safe_point 通过心跳/请求告诉每个 TiKV。TiKV 的 `gc_worker` 有个 `GcSafePointProvider` 拿这个值(见 `gc_worker.rs` 里的 [get_safe_point](../tikv/src/server/gc_worker/gc_worker.rs#L80))。

> **不这样会怎样**:如果 TiKV 各自定 safe_point,会有两个灾难。一是**不一致**:不同 TiKV 对"最老的活跃事务"理解不同,可能清掉别人还要用的版本。二是**不知道全局视图**:一个 TiKV 不知道集群里有没有 CDC 任务、有没有 BR 备份在跑,擅自清会把它们破坏。所以 safe_point 必须由 PD 这个全局协调者统一算、统一下发。

### GC 不破坏快照隔离:为什么"清掉旧版本"是安全的

很多人会担心:GC 把旧版本删了,会不会破坏 MVCC 的快照隔离?答案是**不会,只要 safe_point 算对了**。逻辑链是:

1. MVCC 读:取 ≤ start_ts 的最大 commit_ts 版本;
2. 活跃事务的 start_ts 都 > safe_point;
3. 所以活跃事务拿到的版本,commit_ts 在 (safe_point, start_ts],**比 safe_point 新**;
4. GC 只清 commit_ts ≤ safe_point 的版本;
5. 所以 GC 清的版本,活跃事务根本不会读到——隔离性不破。

唯一要小心的是**最后一个 ≤ safe_point 的版本**:它是一个 key 在 safe_point 时刻的"当前值",虽然比 safe_point 老(或等于),但它是"safe_point 之前最后一个状态",删了的话,哪怕有事务 start_ts 略大于 safe_point,也找不到任何 ≤ start_ts 的版本了(误以为这个 key 不存在)。所以 GC 的规则是:**safe_point 之前的版本可以删,但每个 key 要保留一个"≤ safe_point 的最新版本"当锚点**——这个保留的版本,代表"这个 key 在 safe_point 时刻的状态"。这个细节在单 key GC 的状态机里体现(下一节)。

---

## 三、单 key 的 GC:三态状态机

### gc 函数的入口

GC 的最小工作单元是"对单个 key,清掉它 ≤ safe_point 的多余版本"。这个逻辑在 `src/storage/txn/actions/gc.rs` 里,入口是 `gc` 函数([gc](../tikv/src/storage/txn/actions/gc.rs#L13)):

```rust
// 简化示意:
pub fn gc<'a, S: Snapshot>(
    txn: &'a mut MvccTxn,
    reader: &'a mut MvccReader<S>,
    key: Key,
    safe_point: TimeStamp,
) -> MvccResult<GcInfo> {
    let gc = Gc::new(txn, reader, key);
    let info = gc.run(safe_point)?;   // 核心是 run
    info.report_metrics(STAT_TXN_KEYMODE);
    Ok(info)
}
```

它接收一个 `MvccTxn`(用来攒删除操作)、一个 `MvccReader`(用来扫这个 key 的所有版本)、要 GC 的 key、和 safe_point。核心是 `Gc::run`。

### run:三态状态机的拉动

`run` 的逻辑看起来不长,但它是个精巧的状态机([run](../tikv/src/storage/txn/actions/gc.rs#L67))。它从最新的版本开始(commit_ts 最大),倒着往前扫,根据当前状态决定每个版本是删还是留:

```rust
// 简化示意(基于源码结构):
fn run(mut self, safe_point: TimeStamp) -> MvccResult<GcInfo> {
    let mut state = State::Rewind(safe_point);
    while let Some((commit, write)) = self.next_write()? {        // 从最新往最老扫
        if self.txn.write_size >= MAX_TXN_WRITE_SIZE {            // 攒的删除太多,先返回(下次再来)
            return Ok(self.info);
        }
        state.step(&mut self, write, commit);                     // 状态机推进
    }
    if let State::RemoveAll(Some((commit, write))) = state {       // 收尾
        self.delete_write(write, commit);
    }
    self.info.is_completed = true;
    Ok(self.info)
}

enum State {
    Rewind(TimeStamp),                                  // 倒带:跳过 commit_ts > safe_point 的版本
    RemoveIdempotent,                                   // 删幂等记录(Rollback/Lock)
    RemoveAll(Option<(TimeStamp, Write)>),              // 全删(留一个最新 Put/Delete)
}
```

三个状态的含义:

1. **`Rewind(safe_point)`**:倒带。从最新版本开始,只要 `commit_ts > safe_point`,跳过不动(这些是活跃事务可能要读的)。一旦遇到第一个 `commit_ts ≤ safe_point` 的版本,切到下一个状态。
2. **`RemoveIdempotent`**:删幂等记录。这里遇到的版本都是 ≤ safe_point 的,可以清。但要看类型:
   - `Put`(写入):这是"一个有效的数据版本",**保留它**——切到 `RemoveAll`,从下一个更老的版本开始全删(因为这个 Put 是"safe_point 时刻的当前值",要留)。
   - `Delete`(删除):这也是"一个有效的状态",保留,切到 `RemoveAll(Some(...))`(记下这个 delete,如果后面没有别的就一起删,因为 delete 之前的版本都已被删过了,完全无用)。
   - `Rollback` / `Lock`:这些是未提交/已回滚的幂等记录,**直接删**(它们不代表有效数据状态)。
3. **`RemoveAll`**:全删。到了这个状态,说明已经保留了"最新的 ≤ safe_point 的有效版本",它之前的更老版本,**全部删除**——它们都是被覆盖的考古层,留着无用。

> **钉死这件事**:这个状态机的核心是"**保留一个最新的 ≤ safe_point 的有效版本(Put 或 Delete),其余全删**"。这保证了:① 活跃事务(start_ts > safe_point)读最新版本时,能拿到这个保留的版本;② 不会误删 key 在 safe_point 时刻的状态。Rollback/Lock 这种幂等记录则不保留——它们不代表数据状态,纯垃圾。

### 一个细节:write_size 限制

注意 `run` 里有这么一句:`if self.txn.write_size >= MAX_TXN_WRITE_SIZE { return Ok(self.info); }`。这是说,如果一个 key 的版本太多,删的删除操作攒到一定大小就先返回——GC 不是非要把一个 key 的所有老版本一次清完,可以分多次。这是为了控制单个 GC 事务的大小(避免一个 GC 操作写一个超大的 WriteBatch,阻塞 RocksDB)。`info.is_completed` 标记这次是否真的清完了(没清完下次接着清)。

---

## 四、两套 GC 执行路径:active GC vs compaction filter

单 key 的 `gc` 函数有了,但谁去调用它、对哪些 key 调?TiKV 有两套执行路径,理解它们的演进是本章的关键。

### 路径一:active GC(gc_worker 主动扫)

这是**老资料讲得最多**的路径,但 9.x 已不是主线。它的逻辑是:

1. **`gc_manager`** 周期性(由 PD 推进 safe_point 触发)启动一轮 GC,把集群的 key range 分成一批 Region;
2. 对每个 Region,`gc_worker` 主动扫这个 Region 的 write CF,**列出所有有版本的 key**;
3. 对每个 key,调用 `actions/gc.rs::gc` 清老版本;
4. 把删除操作攒成 WriteBatch,写回 RocksDB。

这条路径有几个问题:

- **要扫两遍**:先扫 write CF 列出所有 key,再对每个 key 扫它的版本。两遍扫描,IO 开销大。
- **写放大**:删老版本本身要写 RocksDB(delete 也是一种写),触发更多 compaction。
- **和 RocksDB compaction 割裂**:active GC 删的 key,要等 RocksDB 下次 compaction 才真正从 SST 里消失——GC 和 compaction 是两套独立流程,各自消耗资源。

### 路径二:compaction filter(主线,9.x 默认)

更聪明的做法是:**让 GC 借用 RocksDB compaction 的便车**。RocksDB 在 compaction 时,会把多个 SST 的数据合并、重写到新 SST——这个过程中**每个 key 都会被遍历一遍**。如果在这个遍历里顺手判断"这个 key 的这个版本是不是该 GC 掉",就不用再单独扫一遍了。

RocksDB 提供了一个叫 **CompactionFilter** 的钩子(承接《LevelDB》那本讲的 compaction,这里用的是 RocksDB 的扩展),允许用户在 compaction 时对每个 key-value 做自定义处理(保留/删除/改值)。TiKV 实现了 `WriteCompactionFilterFactory` 和 `WriteCompactionFilter`([Factory](../tikv/src/server/gc_worker/compaction_filter.rs#L205)),挂在 write CF 的 compaction 上。

核心入口是 `WriteCompactionFilterFactory::create_compaction_filter`,它在每次 compaction 开始时被调用([create_compaction_filter](../tikv/src/server/gc_worker/compaction_filter.rs#L210)):

```rust
// 简化示意:
fn create_compaction_filter(&self, context: &CompactionFilterContext) -> Option<(CString, Self::Filter)> {
    let gc_context = GC_CONTEXT.lock().unwrap();
    let safe_point = gc_context.safe_point.load(Ordering::Relaxed);
    if safe_point == 0 { return None; }                         // safe_point 没初始化,不 GC

    // 一些前置检查:DB 没卡住、特性开关开了、版本检查通过
    if db.as_ref().is_some_and(RocksEngine::is_stalled_or_stopped) { return None; }
    if !do_check_allowed(enable, skip_vcheck, &gc_context.feature_gate) { return None; }

    // 优化:用 TableProperties 判断这批 SST 是否值得 GC(老版本比例够高才做)
    if !check_need_gc(safe_point.into(), ratio_threshold, context) {
        return None;   // 老版本不多,跳过,省 CPU
    }

    let filter = WriteCompactionFilter::new(db, safe_point, context, ...);
    Some((CString::new("write_compaction_filter").unwrap(), filter))
}
```

`WriteCompactionFilter` 在 compaction 遍历每个 key 时被调用,它解码出这个 key 的 commit_ts 和 Write 类型,用和 `gc.rs` 类似的逻辑(只是更紧凑、流式地)判断这个版本删不删。删的版本不写进新 SST,自然就消失了。

```
   RocksDB compaction(正常流程,合并 SST):
   ┌──────────────────────────────────────────────────┐
   │  读老 SST 的每个 key                              │
   │     ↓                                             │
   │  调 WriteCompactionFilter(safe_point)             │
   │     ↓                                             │
   │  判断:这个版本 ≤ safe_point 且是多余的?          │
   │     是 → 不写进新 SST(GC 掉了)                  │
   │     否 → 写进新 SST(保留)                       │
   │     ↓                                             │
   │  生成新 SST                                      │
   └──────────────────────────────────────────────────┘
   GC 借了 compaction 的车,零额外扫描
```

> **钉死这件事**:compaction filter 是把 GC 和 RocksDB compaction **合二为一**。它的精妙在于:**compaction 本来就要遍历每个 key**(为了合并 SST),GC 也需要遍历每个 key(为了判断删不删)——两件事的遍历成本重合了,合并做就只付一次遍历的钱。而 active GC 是单独再扫一遍,和 compaction 各自付费。这是"借用现有流程顺手做事"的典范,和《LevelDB》里 compaction 顺手清 TTL 是同一种智慧(承接《LevelDB》那本讲的 compaction filter 机制)。

### check_need_gc:TableProperties 优化

注意 `create_compaction_filter` 里有个 `check_need_gc` 调用——它用 RocksDB 的 **TableProperties**(SST 文件的元数据)判断这批 SST 是否值得 GC。TableProperties 里记录了每个 SST 的 min_ts、max_ts、key 数量等。如果一批 SST 的 max_ts 都比 safe_point 新(没有老版本),或者老版本比例低于 `ratio_threshold`(默认 0.1,即 10%),就**跳过这次 GC**,省 CPU。

> **不这样会怎样**:如果每次 compaction 都无条件做 GC 过滤,那么一个几乎没有老版本的 SST(全是新数据),也要逐个 key 解码判断——白白浪费 CPU。TableProperties 让 TiKV 用"看一眼 SST 的统计元数据"的代价,跳过那些不值得 GC 的 compaction。这是用元数据换 CPU 的典型优化。

### 9.x 的主线:compaction runner

进一步,9.x 引入了 `compaction_runner`(见 [CompactionRunner](../tikv/src/server/gc_worker/compaction_runner.rs#L156))。它不是被动等 RocksDB 自然 compaction,而是**主动挑出老版本多的 Region,主动触发一次 compaction**(带上 compaction filter)。这样 TiKV 可以更主动地控制 GC 的节奏——不必等 RocksDB 自然 compaction(可能很久才触发一次),而是"我发现这个 Region 老版本多了,就主动让它 compact 一次,顺便清掉"。

这是 active GC 和 compaction filter 的融合:**保留 compaction filter 的执行机制(零额外遍历),但用 active 的方式主动触发**。两者优势合一。

---

## 五、承接《LevelDB》:compaction filter 的对照

这里必须做一次承接。CompactionFilter 是 RocksDB(《LevelDB》那本讲的工业级后代)的一个扩展点——它允许在 compaction 时对每个 key 做自定义处理。《LevelDB》那本讲过 LSM-tree 的 compaction 是怎么把多层 SST 合并的,这里 TiKV 在合并的过程中插了一个钩子。

对照:

| 维度 | 《LevelDB》讲的 compaction | TiKV 的 GC compaction filter |
|------|--------------------------|------------------------------|
| 触发 | LSM 各层大小超阈值 | 同(借用 RocksDB compaction)+ 9.x 主动触发 |
| 遍历 | 合并多个 SST,丢被覆盖的 key | 同,再加"丢 GC 掉的老版本" |
| 钩子 | LevelDB 原生无 | RocksDB 的 `CompactionFilter` trait |
| 判断 | key 是否被覆盖(看最新版本) | key 的 commit_ts ≤ safe_point 且多余 |

> **钉死这件事**:TiKV 的 GC 本质是"在 LSM compaction 的既有流程里,多加一条删除规则(老版本)"。理解 GC 的前提是理解 compaction——后者在《LevelDB》那本已拆透,本书不重复。这里只讲 TiKV 在钩子里**怎么判断该删哪个版本**(就是前面三态状态机的逻辑)。

---

## 六、flashback:闪回到某个 version

### flashback 不是"备份恢复"

讲完 GC,顺手讲一个相关的新特性——**flashback**(闪回)。它是 6.x 引入、7.x/8.x 完善的特性。很多人会把它和"备份恢复"混淆,但它们本质不同:

- **备份恢复(BR)**:从一个外部备份(存在 S3/磁盘)把数据拷回来,要读外部存储、要走网络、要长时间。
- **flashback**:把数据库(或某个库/表)**闪回到 TiKV 上还留着的某个历史 version**——只要这个 version 还没被 GC 清掉,就能闪回。**不需要外部备份,因为旧版本就在 TiKV 的 MVCC 里**。

> **钉死这件事**:flashback 能成立的前提是——**MVCC 的旧版本还在 write CF 里(没被 GC 清掉)**。所以 flashback 的目标 version,必须晚于当前的 safe_point(被 GC 清掉的版本闪回不了)。这也解释了为什么 GC 不能太激进——保留足够长的 MVCC 历史(通过调大 GC TTL),才能支持"误删数据后 N 小时内 flashback"的运维场景。

### flashback 怎么实现:复用 prewrite + commit

flashback 的实现思路很优雅:**它不"回滚"数据,而是"把旧 version 的值当新数据,走一遍正常的事务写流程"**。具体在 `src/storage/txn/actions/flashback_to_version.rs`(见 [flashback_to_version_write](../tikv/src/storage/txn/actions/flashback_to_version.rs#L124)):

1. **读阶段**(`flashback_to_version_read_write`):对每个要 flashback 的 key,读出它在目标 version(某个 commit_ts ≤ flashback_commit_ts)的值。
2. **写阶段**(`flashback_to_version_write`):把这个值,用 `flashback_start_ts` 当 start_ts、`flashback_commit_ts` 当 commit_ts,**走一遍 prewrite + commit**——在 write CF 写一条新的提交记录,在 default CF(如果需要)写 value。

```rust
// 简化示意(flashback_to_version_write 的核心):
pub fn flashback_to_version_write<...>(
    txn: &mut MvccTxn,
    reader: &mut SnapshotReader<...>,
    key: &Key,
    value: Option<Value>,                // 读到的目标 version 的值
    flashback_start_ts: TimeStamp,
    flashback_commit_ts: TimeStamp,
) -> Result<()> {
    match value {
        Some(value) => {
            // 把旧值当新值写:prewrite(写 default)+ write(写 write CF 的提交记录)
            txn.put_value(key.clone(), flashback_start_ts, value);
            let write = Write::new(WriteType::Put, flashback_start_ts, None);
            txn.put_write(key.clone(), flashback_commit_ts, write.as_ref().to_bytes());
        }
        None => {
            // 旧 version 是 Delete(或不存在),写一条 Delete
            let write = Write::new(WriteType::Delete, flashback_start_ts, None);
            txn.put_write(key.clone(), flashback_commit_ts, write.as_ref().to_bytes());
        }
    }
    Ok(())
}
```

注意这里有个优化(`flashback_to_version_read_write` 里的判定,见 [read_write](../tikv/src/storage/txn/actions/flashback_to_version.rs#L47-L57)):**如果一个 key 的最新 commit_ts 已经 ≤ flashback_version,或者已经等于 flashback_commit_ts(已经 flashback 过了),就跳过**——不必重复 flashback。这避免了"flashback 一个已经 flashback 过的 key"的重复劳动。

> **不这样设计会怎样**:如果 flashback 用"先删数据再恢复"的朴素思路,会有几个问题。一是**不原子**:删了一半挂了,数据就没了。二是**破坏 MVCC 历史**:删掉再写,中间的事务历史断了,后续读不到一致快照。复用 prewrite + commit,让 flashback 本身**也是一个正常事务**(有 start_ts/commit_ts,走 Raft 复制,落盘原子)——既原子,又不破坏 MVCC 链(它只是新增了一层"flashback 到的版本"在最新的 commit_ts 上)。这是用既有机制解决新问题的优雅范例。

### flashback 的两阶段语义

实际执行时,flashback 分两阶段(类似 Percolator):

1. **准备阶段**:`flashback_to_version_read_lock` 先检查这段 key range 里有没有残留的锁(未提交事务),有的话先 rollback 掉(`rollback_locks`,见 [rollback_locks](../tikv/src/storage/txn/actions/flashback_to_version.rs#L67))——保证 flashback 开始时这段数据是"干净"的。
2. **执行阶段**:`prewrite_flashback_key` + `flashback_to_version_write`,把每个 key 在目标 version 的值,用 flashback 事务的 ts 写回去。

为什么要先清锁?因为如果 flashback 期间有别的活跃事务在这段数据上加锁,会冲突。先 rollback 掉残留锁(把它们当失败事务清理),保证 flashback 事务能独占这段数据。这和 Percolator 的 prewrite 前检查锁是一脉相承的逻辑(承接 P4-13)。

---

## 七、技巧精解

本章挑两个最硬核的技巧单独拆透。

### 技巧一:compaction filter 借车——零额外遍历的 GC

这是本章的灵魂。前面讲过它的原理,这里单独拆"为什么这是最优解"。

对比三种 GC 执行方案:

1. **朴素方案 A(active GC,扫两遍)**:单独起一个任务,扫 write CF 列出所有 key,再对每个 key 扫版本删老版本。
   - 问题:扫两遍(列 key + 删版本)、写放大(删操作触发新 compaction)、和 RocksDB compaction 各干各的。
2. **朴素方案 B(后台常驻线程,持续清)**:起一个线程,持续扫 write CF 边扫边删。
   - 问题:持续占用 CPU/IO、和前台事务读抢资源、扫描无止境。
3. **TiKV 的方案(compaction filter)**:挂在 RocksDB compaction 上,compaction 本来就要遍历每个 key,顺手判断删不删。
   - 优势:**零额外遍历**(GC 的遍历成本被 compaction 吸收)、**写放大消失**(删除就是不写进新 SST,不再产生新的 delete 标记)、**和 compaction 节奏同步**(compaction 做完,老版本就真的消失了)。

> **不这么写会怎样**:用方案 A,GC 一个 Region 要扫两遍 write CF——如果这个 Region 有 256MB 数据,扫两遍就是 512MB 的 IO,且产生大量 delete 写入,触发更多 compaction,形成"GC 越多越累"的恶性循环。compaction filter 把 GC 的边际成本降到几乎为零(只在 compaction 时多算几条 commit_ts 比较),这是 TiKV 能扛住高写入负载同时持续 GC 的工程基础。这是"**把副作用挂在既有流程上**"的通用智慧——和《Linux 内核》里把统计挂在中断处理上、和《Tokio》里把超时检查挂在 IO 就绪上是同一种思路。

### 技巧二:三态状态机——为什么是 Rewind/RemoveIdempotent/RemoveAll

单 key GC 用三态状态机而不是简单的"删所有 ≤ safe_point 的版本",这个设计值得拆。看它怎么保证正确性:

朴素方案是"删所有 commit_ts ≤ safe_point 的版本"。但这会出错——如果删光了,一个 start_ts 略大于 safe_point 的事务读这个 key 时,找不到任何 ≤ start_ts 的版本,会误以为这个 key 不存在(而实际上它在 safe_point 时刻是有值的)。所以**必须保留一个最新的 ≤ safe_point 的有效版本**当锚点。

三态状态机精确地实现了这个语义:

- **Rewind**:跳过 commit_ts > safe_point 的(活跃事务可能要读,不能动);
- **RemoveIdempotent**:遇到第一个 ≤ safe_point 的版本,看类型——如果是 Put/Delete(有效状态),**保留它**当锚点,切到 RemoveAll;如果是 Rollback/Lock(垃圾),直接删,继续找;
- **RemoveAll**:锚点已留,它之前的所有版本全删(都是被覆盖的考古层)。

这样保证:每个 key 保留"最新的 ≤ safe_point 的 Put 或 Delete",其余老版本全清。既不破坏快照隔离(保留的锚点让 start_ts > safe_point 的事务能读到值),又最大化清理(其余老版本全删)。

> **不这么写会怎样**:如果朴素地"删所有 ≤ safe_point",会破坏 MVCC 读——一个 key 在 safe_point 时刻明明有值,删光后却读不到了。三态状态机用"先找锚点、留锚点、删其余"的顺序,把这个正确性陷阱堵死。`RemoveAll` 状态里的 `Option<(TimeStamp, Write)>` 参数还有个微妙用途:如果锚点是 Delete,且它前面没有别的有效版本,那么这个 Delete 也可以删(因为它代表"这个 key 已被删除",前面都是更古老的考古层,全删包括这个 Delete 都不影响——key 不存在这个事实由"找不到任何版本"自然表达)。这是对边界条件的极致打磨。

---

## 八、章末小结

### 回扣主线

本章讲 GC 和 flashback,服务二分法的**事务层**这一面。GC 是 MVCC 的配套机制——MVCC 用空间换隔离性,GC 把空间收回来;flashback 是 MVCC 的衍生红利——旧版本还在,就能闪回。两者都建立在"MVCC 的多版本编码"(P3-10)之上,而 GC 的执行又巧妙地借用了 RocksDB compaction(承接《LevelDB》),是事务层和复制层(单机引擎)协作的一个典范。

从全书的旅程看,GC 是写路径的"善后"——一条写请求经 prewrite→commit 落盘后,留下的版本最终要被 GC 清掉(直接清或借 compaction 清)。flashback 则是"反向"操作——不是清旧版本,而是用旧版本当新数据再写一遍。

### 五个为什么

1. **为什么 MVCC 必须有 GC?**——每次写都留版本,不清则磁盘爆炸、scan 慢死、SST 臃肿。GC 是 MVCC 持续运行的必需品。
2. **为什么 safe_point 是 GC 的边界?**——它表示"此前的版本没有活跃事务会读"。活跃事务 start_ts > safe_point,所以读到的版本 commit_ts 在 (safe_point, start_ts],比 safe_point 新,不会读到被 GC 的版本——隔离性不破。
3. **为什么用 compaction filter 而不是 active GC?**——compaction filter 借 RocksDB compaction 的遍历,零额外扫描、零写放大;active GC 要扫两遍、写放大、和 compaction 割裂。前者是 9.x 主线。
4. **为什么单 key GC 是三态状态机?**——必须保留"最新的 ≤ safe_point 的有效版本"当锚点,否则会误判 key 不存在。Rewind/RemoveIdempotent/RemoveAll 精确实现"留锚点、删其余"。
5. **为什么 flashback 复用 prewrite + commit?**——不"删数据再恢复"(不原子、破坏 MVCC),而是把旧 version 当新数据走正常事务流程,既原子又不破坏历史链。前提是目标 version 还没被 GC 清掉。

### 想继续深入往哪钻

- **想看单 key GC 的状态机**:读 `src/storage/txn/actions/gc.rs`(全文不到 150 行,精巧)。
- **想看 compaction filter 实现**:读 `src/server/gc_worker/compaction_filter.rs`(`WriteCompactionFilterFactory` + `WriteCompactionFilter`)。
- **想看 GC 的调度**:读 `src/server/gc_worker/{gc_manager,gc_worker,compaction_runner}.rs`。
- **想看 flashback 的两阶段**:读 `src/storage/txn/actions/flashback_to_version.rs`(read_lock / read_write / write / prewrite_flashback_key 四个函数)。
- **想看 RocksDB compaction 本体**:读《LevelDB》那本(compaction filter 是 RocksDB 对 LevelDB 的扩展)。

### 引出下一章

GC 和 flashback 讲完了。但事务层还有一块没讲——**悲观事务**和**变更捕获(CDC)**。悲观事务用行锁防止并发冲突,它的锁机制、死锁检测是 OLTP 场景的关键;CDC 则把 TiKV 的变更实时推给下游(给数据仓库、搜索引擎同步),它建立在 MVCC 多版本之上,和 GC 又有微妙的互动(CDC 要保护旧版本不被 GC 清掉)。下一章 P6-21,我们讲 **悲观锁与 CDC**——并收尾整个第 6 篇,引出第 7 篇的全书收束。

> **下一章**:[P6-21 · 悲观锁与 CDC](P6-21-悲观锁与CDC.md)
