# 第 3 篇 · 第 9 章 · RocksDB 引擎:LSM-tree + Column Family

> **核心问题**:Raft 把一条写命令 commit 了之后,它要落到哪里?答案大家都猜得到——单机存储引擎。可 TiKV 的单机引擎**不是**自己从零写的,它直接用 RocksDB。问题来了:同一个 RocksDB 实例,为什么要开**三个 Column Family**(default、write、lock)?为什么不把所有 key-value 平铺进一个 CF?更进一步,为什么 TiKV 要写一整套 `engine_traits` 抽象 trait,把 RocksDB 藏起来?还有 8.x 之后冒出来的 `in_memory_engine`(把热 Region 的数据塞进内存),它和 RocksDB 是什么关系、凭什么不破坏一致性?这一章就把这三件事——CF 三分工、engine_traits 抽象、in_memory_engine——拆透。

> **读完本章你会明白**:
> 1. 一个 RocksDB 实例开三个 CF(default 存 value、write 存提交记录、lock 存锁)的真正动机:把"访问模式不同"的数据物理分离,让 Compaction、Bloom、读放大各自调优;朴素地把它们塞进一个 CF 会撞什么墙。
> 2. `engine_traits` 不是"为了抽象而抽象"——它是 raftstore、storage/txn 这些上层模块**能同时挂在 RocksDB、RaftEngine、in_memory_engine、raftstore-v2 tablet 等多套后端之上**的根。`KvEngine`、`RaftEngine`、`RegionCacheEngine` 三个 trait 各管哪一摊。
> 3. `in_memory_engine`(RegionCacheMemoryEngine)为什么不是"另一套独立的 KV"而是 RocksDB 的一个**只读缓存层**,它怎么靠"sequence number 与磁盘 RocksDB 共享"做到不破坏一致性。
> 4. 为什么本章是"承接《LevelDB》"的——LSM-tree、MemTable、SST、Compaction、Bloom filter 这些**不重复讲**,本章只讲"TiKV 怎么在 RocksDB 之上组织 MVCC 数据"。

> **如果一读觉得太难**:先只记住三件事——① 一个 RocksDB 实例开三个 CF:default=value、write=提交记录、lock=未提交的锁,它们访问模式不同所以分开存;② TiKV 对所有存储后端都定义了 trait(`KvEngine`/`RaftEngine`/`RegionCacheEngine`),上层只对 trait 编程,所以 RocksDB、RaftEngine、in_memory_engine 都能接进来;③ in_memory_engine 是热数据的内存缓存,不是独立引擎,数据真身还在 RocksDB。

---

## 〇、一句话点破

> **TiKV 的单机引擎是 RocksDB(LevelDB 的工业级后代);同一个 RocksDB 实例开 default/write/lock 三个 CF,把访问模式不同的数据物理分离;再套一层 `engine_traits` trait,让上层(raftstore、storage/txn)对"KvEngine/RaftEngine/RegionCacheEngine"三个抽象编程,从而把 RocksDB、RaftEngine、in_memory_engine 等多套后端都接进来。**

这是结论,不是理由。本章倒过来拆:先讲为什么要把数据切成三个 CF(朴素地塞一个 CF 会撞什么墙);再讲 `engine_traits` 为什么要搞这么厚一层抽象(谁是 trait 的实现、谁是消费者);最后讲 in_memory_engine 这个"内存缓存层"为什么能不破坏一致性。

---

## 一、承接《LevelDB》:RocksDB 是什么,本章不讲什么

### 先把"地基"对齐:RocksDB = LevelDB 的工业级后代

RocksDB 是 Facebook(现 Meta)2012 年从 LevelDB fork 出来、面向**高吞吐、多核、大内存、SSD**重写的 LSM-tree 引擎。它在 LevelDB 的骨架上加了一堆工程优化:多线程 Compaction、Column Family、`WriteBatch`、可调的 memtable/table_factory/block cache、压缩选项、`IngestExternalFile`、`Checkpoint`、`DeleteRange` 等等。对 TiKV 来说,RocksDB 是个现成的、生产里被反复验证过的 LSM 引擎,TiKV 不需要自己重造轮子。

LSM-tree 怎么工作——写先进 MemTable(内存里的有序结构,跳表或跳表变体),MemTable 满了 flush 成 SST 文件(immutable,有序),SST 攒到一定条件触发 Compaction 把多层归并——这套**全部承接《LevelDB》那本**,本书不重复讲。SST 文件的 block-based table 格式、Bloom filter、size-tiered vs leveled compaction、写放大/读放大/空间放大的取舍、point lookup vs range scan 的优化,这些**都是《LevelDB》的活**。

> **钉死这件事**:本章只讲一件事——**TiKV 怎么"用"RocksDB 的 Column Family 能力,把 MVCC 多版本数据组织成三摊**。涉及 RocksDB 本体的机制(SST、Compaction、MemTable、Bloom),一律回《LevelDB》那本复习,本书不重复。

### 一个关键澄清:本章的"RocksDB"指的是哪个 RocksDB

读 TiKV 源码有个坑:整个进程里不止一个"RocksDB 实例"。经典 raftstore(v1)的模型是——**一个 store(TiKV 进程)一个 RocksDB 实例**(存所有 Region 的所有 KV 数据),加上一个**独立的 RaftEngine**(存 Raft 日志,P2-06 拆过)。本章讲的是前者——**那个存 KV 数据的 RocksDB 实例**。raftstore-v2 改成了 per-Region tablet(每个 Region 一个独立 RocksDB),P2-08 讲 snapshot 时点过这个差异,本章主要讲 v1 的"一个 RocksDB 多 CF"模型,因为它最能讲清"为什么三个 CF"。

---

## 二、三个 Column Family:为什么不全塞进一个 CF

RocksDB 的 **Column Family**(简称 CF,列族)是这样一种能力:同一个 RocksDB 实例里,把 key-value 按"逻辑分类"切成多摊,每摊是一个 CF;每个 CF **独立维护自己的 MemTable、SST、Compaction、Bloom、Block Cache**(甚至可以独立设置压缩算法、level 阈值);但所有 CF 共享同一个 WAL(预写日志,保证崩溃恢复)和同一套实例级的配置。可以把它想成"一个 RocksDB 实例里塞了 N 个小 LSM-tree,它们共享外壳但内部独立"。

### CF 在源码里是怎么定义的

TiKV 的 CF 名字是常量字符串,定义在 `engine_traits` 的 [`cf_defs.rs`](../tikv/components/engine_traits/src/cf_defs.rs):

```rust
// components/engine_traits/src/cf_defs.rs#L3-L12(摘录)
pub type CfName = &'static str;
pub const CF_DEFAULT: CfName = "default";
pub const CF_LOCK: CfName = "lock";
pub const CF_WRITE: CfName = "write";
pub const CF_RAFT: CfName = "raft";
// Cfs that should be very large generally.
pub const LARGE_CFS: &[CfName] = &[CF_DEFAULT, CF_LOCK, CF_WRITE];
pub const ALL_CFS: &[CfName] = &[CF_DEFAULT, CF_LOCK, CF_WRITE, CF_RAFT];
pub const DATA_CFS: &[CfName] = &[CF_DEFAULT, CF_LOCK, CF_WRITE];
```

注意四个 CF 名字:`default`、`lock`、`write` 是**数据 CF**(`DATA_CFS`),`raft` 是历史遗留——老版本 TiKV 把 Raft 日志存进 RocksDB 的 raft CF。**新版已经用独立的 RaftEngine 替代了它**(P2-06 拆透),所以本章只讲三个数据 CF。`CF_RAFT` 在 v1 里还在但实际由 RaftEngine 接管,可以理解成"名义上存在,实际不走"。

还有个细节 [`data_cf_offset`](../tikv/components/engine_traits/src/cf_defs.rs#L14):它把三个数据 CF 映射到 `0/1/2` 的下标(default=0, lock=1, write=2)。这个下标在后面的 `WriteBatch`、metrics、snapshot 里反复用到——比如一个 region 的 snapshot 要按 CF 分别 scan,这个 offset 就是用来按 CF 分桶统计的。

### 三个 CF 各存什么

三个数据 CF 的分工(P3-10 会拆到源码级,这里先给全貌):

```
   同一个 key 的多版本,在 RocksDB 里的物理布局(三 CF 分工)
   ┌──────────────────────────────────────────────────────────────────┐
   │  default CF   │  value 的真身(大 value 走这里)                  │
   │               │  key = data_prefix + user_key + start_ts(反转)   │
   │               │  value = 真实的 value bytes                       │
   ├──────────────────────────────────────────────────────────────────┤
   │  write CF     │  提交记录(每个版本的"出生证")                  │
   │               │  key = data_prefix + user_key + commit_ts(反转)  │
   │               │  value = Write{start_ts, write_type, short_value}│
   ├──────────────────────────────────────────────────────────────────┤
   │  lock CF      │  未提交事务的锁                                   │
   │               │  key = data_prefix + user_key                     │
   │               │  value = Lock{primary, ts, ttl, for_update_ts...}│
   └──────────────────────────────────────────────────────────────────┘
```

一句话概括(下一章详拆):

- **default CF**:存真正的 value。TiDB 一次 `INSERT` 的 value(比如一行数据编码后的 bytes)就写到这里,以 `user_key + start_ts` 为 key(P3-10 讲为什么这么编码)。
- **write CF**:存"提交记录"。一个 value 被 commit 了,就在 write CF 写一条记录,记录"这个 key 在 commit_ts 这个时刻,对应 start_ts 那个事务的 value"。**读操作先扫 write CF 找到可见版本,再去 default CF 取真身**。
- **lock CF**:存未提交事务的锁。一个事务 prewrite 阶段会在这里写锁,commit 时清掉。读遇到 lock 就知道"这个 key 有事务还没完"。

### 不这样会怎样:把三个 CF 合并成一个 CF

这是个很好的反面思考。假设把 default、write、lock 全塞进**一个 CF**(就像 LevelDB 原版只有 default CF),会撞几堵墙:

**墙一:Compaction 互相拖累。** default CF 的数据(真正的 value)通常**大**(一条记录可能几百字节到几 KB),write CF 的记录**小**(就是个提交元信息,几十字节),lock CF 的记录**极少**(只有未提交事务才有)。这三类数据的"体积"和"更新频率"差好几个数量级。塞一个 CF,Compaction 会把它们**一起归并**——每次 compact 都要重写一大堆无关的 value,写放大爆炸。分开成三个 CF 后,default CF 的 Compaction 只动 value,write CF 只动提交记录,lock CF 基本不 compact,各走各的节奏。

**墙二:读优化策略冲突。** 读操作最关心的是 write CF(扫 write 找可见版本),所以会在 write CF 上配 Bloom filter、配激进的 block cache、配更小的 target_file_base_size。而 default CF(取 value)走的是 point lookup(一次读一个 key),lock CF 走的是"有没有锁"的判断——这三者的 Bloom、cache、compaction 配置**完全不一样**。塞一个 CF,就只能用同一套配置,优化空间被锁死。

**墙三:lock CF 需要特殊对待。** lock CF 有个特殊需求——**清墓碑(tombstone)的时机不同**。锁被 commit 或 rollback 清掉后,应该尽快被 Compaction 物理删掉(否则 lock CF 会堆积无用的 tombstone)。而 default/write CF 的老版本要等 GC(超过 safe point 才能清,P6-20 讲)。分开 CF 让 lock CF 独立调优清墓碑策略。

> **所以这样设计**:三个 CF 的分工,本质是**按"访问模式"做物理分离**。default(大 value、读时按 key 取)、write(小记录、读时按扫)、lock(小记录、临时、频繁增删),这三者的 Compaction 节奏、Bloom 配置、cache 命中模式差到天上,强行塞一个 CF 就是把它们的优化空间互相锁死。CF 让它们各自跑在独立的小 LSM-tree 上,共享外壳(WAL、block cache 等共享)但内部独立演化。

> **钉死这件事**:default/write/lock 三 CF 不是"逻辑上的分类",是**物理上的三个 LSM-tree**。这意味着读 write 找到版本号后,要去 default 取真身时,是个**跨 CF 的点查**——P3-10 / P4-15 会看到这个跨 CF 跳转的代价(TiKV 用 short_value 优化把小 value 内联进 write,省掉这次跨 CF 跳转)。

### CF 在代码里怎么用:put_cf / get_cf

RocksEngine 对每个 CF 都提供独立的读写接口。看 [`engine.rs` 里的实现](../tikv/components/engine_rocks/src/engine.rs#L248):

```rust
// components/engine_rocks/src/engine.rs#L253-L256
impl SyncMutable for RocksEngine {
    fn put_cf(&self, cf: &str, key: &[u8], value: &[u8]) -> Result<()> {
        let handle = get_cf_handle(&self.db, cf)?;
        self.db.put_cf(handle, key, value).map_err(r2e)
    }
    // ...
}
```

注意 `put_cf(cf, key, value)` 的第一个参数是 CF 名字。TiKV 的所有 Apply 写入都是显式指定 CF 的——Apply 一条 Put 命令时,根据这是 value 还是 write 记录,选 `CF_DEFAULT` 或 `CF_WRITE` 写进去(P3-11 详拆 Apply 怎么把 Raft 命令转成 CF 写)。这就是 CF 在代码层面的暴露面——上层明确指定"写进哪个 CF",引擎只负责把这个写送进对应的 MemTable。

> **承接《LevelDB》**:RocksDB 的 CF 接口(`put_cf`、`get_cf`、`iterator_cf`、`delete_range_cf`、WriteBatch 的 `put_cf`)都是 LSM 引擎的标准能力。**这些接口怎么工作、底层 MemTable/SST 怎么响应**,承接《LevelDB》,本书只讲 TiKV 怎么用它们。

---

## 三、engine_traits:把 RocksDB 藏起来

如果三个 CF 是"TiKV 怎么用 RocksDB",那 `engine_traits` 就是"TiKV 怎么**不直接**用 RocksDB"。

### 不这样会怎样:上层直接用 RocksDB

朴素的写法是上层(`raftstore`、`storage/txn`)直接依赖 `rocksdb::DB`——所有读写直接调 `db.put_cf(...)`、`db.get_cf(...)`、`db.snapshot()`。简单直接。但这就把 TiKV 和 RocksDB **焊死**了:

- **换不了后端**:TiKV 实际上有**多个存储后端**——KV 数据用 RocksDB,Raft 日志用 RaftEngine(自己写的专用日志引擎,P2-06),热数据用 in_memory_engine(本章后面讲),测试用 mock 引擎。如果上层直接用 `rocksdb::DB`,这些后端就换不上去。
- **测试没法 mock**:存储层要写单元测试,得换成内存引擎。上层直接调 rocksdb,测试就得真开一个 RocksDB,又慢又重。
- **演进受阻**:TiKV 正在做的 raftstore-v2(多线程 raftstore)用的是 per-Region tablet(每个 Region 一个独立 RocksDB 实例)。上层直接调 `db` 的话,v2 的多 tablet 模型根本接不上。

### 所以这样设计:KvEngine / RaftEngine / RegionCacheEngine 三个 trait

TiKV 把所有存储后端的"接口"抽成 trait,放在 [`components/engine_traits`](../tikv/components/engine_traits/src/) 里。上层只对 trait 编程,具体用哪个后端,在启动时注入。**最核心的三个 trait**:

#### KvEngine:KV 数据引擎

[`KvEngine`](../tikv/components/engine_traits/src/engine.rs#L13) 是"存 KV 数据"的抽象:

```rust
// components/engine_traits/src/engine.rs#L13-L37(摘录)
pub trait KvEngine:
    Peekable            // 点查 get
    + SyncMutable       // 写 put/delete
    + Iterable          // 范围扫描 iterator
    + WriteBatchExt     // 批量写 WriteBatch
    + DbOptionsExt      // DB 级配置
    + CfNamesExt        // 列举 CF
    + CfOptionsExt      // CF 级配置
    + ImportExt         // SST ingest
    + SstExt            // SST 文件管理
    + CompactExt        // 主动 compact
    + RangePropertiesExt// 范围大小估算
    + MvccPropertiesExt // MVCC 统计
    + TtlPropertiesExt  // TTL 统计
    + TablePropertiesExt// 表属性
    + PerfContextExt    // 性能计数
    + MiscExt           // 杂项 flush/sync 等
    + Send + Sync + Clone + Debug + Unpin + Checkpointable + 'static
{
    /// A consistent read-only snapshot of the database
    type Snapshot: Snapshot;

    /// Create a snapshot
    fn snapshot(&self) -> Self::Snapshot;

    /// Syncs any writes to disk
    fn sync(&self) -> Result<()>;
    // ...
}
```

注意 `KvEngine` 是个**超 trait**(super-trait)的组合——它把 `Peekable`(点查)、`Iterable`(扫描)、`WriteBatchExt`(批量写)、`CfNamesExt`(CF 列举)等等十几个小 trait 拼起来,构成一个"能干所有 KV 事的"完整抽象。这种"小 trait 拼大 trait"的设计,让每个能力可以独立测试、独立换实现。

**实现者**:`RocksEngine`(主路径),以及 in_memory_engine 里的 skiplist 引擎等(测试)。`RocksEngine` 的结构([engine.rs#L147](../tikv/components/engine_rocks/src/engine.rs#L147)):

```rust
// components/engine_rocks/src/engine.rs#L147-L155
#[derive(Clone, Debug)]
pub struct RocksEngine {
    db: Arc<DB>,
    support_multi_batch_write: bool,
    #[cfg(feature = "trace-lifetime")]
    _id: trace::TabletTraceId,
    // Used to ensure mutual exclusity between compaction filter writes and the SST ingestion
    // operation.
    pub ingest_latch: Arc<RangeLatch>,
}
```

就是个 `Arc<DB>`( RocksDB 的句柄,clone 便宜)加几个辅助字段。`KvEngine for RocksEngine` 的实现就是把这些 trait 方法转发给底层 `db`(看 [engine.rs#L187-L207](../tikv/components/engine_rocks/src/engine.rs#L187))。

#### RaftEngine:Raft 日志引擎(承接 P2-06)

[`RaftEngine`](../tikv/components/engine_traits/src/raft_engine.rs#L84) 是"存 Raft 日志"的抽象:

```rust
// components/engine_traits/src/raft_engine.rs#L84-L92(摘录)
pub trait RaftEngine: RaftEngineReadOnly + PerfContextExt + Clone + Sync + Send + 'static {
    type LogBatch: RaftLogBatch;

    fn log_batch(&self, capacity: usize) -> Self::LogBatch;

    /// Synchronize the Raft engine.
    fn sync(&self) -> Result<()>;

    /// Consume the write batch by moving the content into the engine itself
    /// and return written bytes.
    fn consume(&self, batch: &mut Self::LogBatch, sync: bool) -> Result<usize>;
    // ...
}
```

**实现者**:`RaftLogEngine`(走 `components/raft_log_engine/`,P2-06 拆过)和老版本的 RocksDB(把 raft CF 当日志存,已弃用)。

> **关键洞察**:`RaftEngine` 和 `KvEngine` 是**两个独立的 trait**——这本身就是个设计决定。它意味着 TiKV 承认"KV 数据"和"Raft 日志"是**两类数据**,各有各的访问模式(KV 随机读写 + 范围扫,日志顺序追加 + 按 index 取),所以分两个引擎、两个 trait。这就是为什么 Raft 日志要单独存(P2-06 的招牌论证),根在这里。

#### RegionCacheEngine:内存缓存引擎

8.x 之后冒出来的 [`RegionCacheEngine`](../tikv/components/engine_traits/src/region_cache_engine.rs#L108):

```rust
// components/engine_traits/src/region_cache_engine.rs#L108-L135(摘录,注释保留)
/// RegionCacheEngine works as a region cache caching some regions (in Memory or
/// NVME for instance) to improve the read performance.
pub trait RegionCacheEngine:
    RegionCacheEngineExt + WriteBatchExt + Debug + Clone + Unpin + Send + Sync + 'static
{
    type Snapshot: Snapshot;

    // If None is returned, the RegionCacheEngine is currently not readable for this
    // region or read_ts.
    // Sequence number is shared between RegionCacheEngine and disk KvEnigne to
    // provide atomic write
    fn snapshot(
        &self,
        region: CacheRegion,
        read_ts: u64,
        seq_num: u64,
    ) -> result::Result<Self::Snapshot, FailedReason>;

    type DiskEngine: KvEngine;
    fn set_disk_engine(&mut self, disk_engine: Self::DiskEngine);
    // ...
}
```

**注意注释里这句"Sequence number is shared between RegionCacheEngine and disk KvEngine to provide atomic write"**——这是 in_memory_engine 不破坏一致性的关键,下节详拆。

### 谁消费这些 trait:上层只对 trait 编程

`KvEngine`、`RaftEngine` 的消费者主要是:

- **raftstore**:`Peer<EK, ER, T>` 的泛型参数 `EK: KvEngine, ER: RaftEngine`——raftstore 不关心 EK 是 RocksEngine 还是别的,只要有 `KvEngine` 就行。
- **storage/txn**:`scheduler` 用 `with_tls_engine(|e: &mut E| e.async_write(...))` 发起写(P2-05 拆过),`E` 是个泛型 `Engine`,可以是 RocksDB 也可以是别的。
- **Apply**(P3-11):`ApplyContext<EK: KvEngine>` 持有引擎引用,apply 时调 `kv_wb.put_cf`、`write_batch.write_opt`。

这种"上层泛型 + 下层多实现"的设计,让 TiKV 能在不改上层代码的前提下,换底层引擎。比如启动时:

- 走经典 raftstore:v1 → `EK = RocksEngine`,`ER = RaftLogEngine`;
- 走 raftstore-v2:每个 Region 一个 RocksDB tablet,`EK` 还是 RocksEngine 但路径不同;
- 开启 in_memory_engine:在 RocksEngine 之上叠一个 `RegionCacheMemoryEngine`,数据双写。

> **钉死这件事**:`engine_traits` 不是"为了抽象而抽象",是 TiKV 在演进过程中**被迫**搞出来的——RaftEngine 替代 RocksDB 存日志、in_memory_engine 做热数据缓存、raftstore-v2 换 tablet 模型,每一个演进都需要换底层引擎。如果上层直接用 rocksdb,这些演进都得改一大片代码。trait 这层间接,让 TiKV 可以"局部换骨架",这是大型系统演进的关键工程能力。

---

## 四、in_memory_engine:热数据的内存缓存层(8.x 新特性)

讲完 CF 和 engine_traits,我们看一个具体的"另一种引擎"——`in_memory_engine`(简称 IME),它是 8.x 引入的新特性,源码在 [`components/in_memory_engine/`](../tikv/components/in_memory_engine/src/)。

### 为什么要加 IME:RocksDB 的读放大天花板

RocksDB 是 LSM-tree,读操作的路径是:查 MemTable(内存)→ 查 Immutable MemTable(内存)→ 查 L0 SST(可能有多个文件,要全查,因为 L0 文件之间 key range 重叠)→ 查 L1-Lmax SST(Bloom filter 过滤 + 二分定位)。一个点查最坏情况要查十几个 SST 文件,每次都可能是一次随机 IO(SSD 上几十微秒)。

对大多数业务这没问题。但有一类场景——**热 Region**(比如 TiDB 里被疯狂访问的几行数据)——它的读 QPS 可能是平均的几十倍,每次都走 LSM 的多层查找,延迟抖动、CPU 占用都成痛点。

朴素解法是"把整个 RocksDB 都放进内存"——但 TiKV 一个 store 可能上百 GB,RocksDB 全内存不现实。于是 8.x 引入了**选择性内存缓存**:把**热的几个 Region** 的数据,在内存里**额外**缓存一份,读的时候先查内存,命中就返回(省掉 LSM 多层查找),没命中再回 RocksDB。

### IME 不是独立引擎,是 Region 级的缓存层

看 [`RegionCacheMemoryEngine` 的结构](../tikv/components/in_memory_engine/src/engine.rs#L338):

```rust
// components/in_memory_engine/src/engine.rs#L338-L350(摘录)
#[derive(Clone)]
pub struct RegionCacheMemoryEngine {
    bg_work_manager: Arc<BgWorkManager>,
    pub(crate) core: Arc<RegionCacheMemoryEngineCore>,
    pub(crate) rocks_engine: Option<RocksEngine>,   // 持有磁盘 RocksDB 引用
    memory_controller: Arc<MemoryController>,
    statistics: Arc<Statistics>,
    config: Arc<VersionTrack<InMemoryEngineConfig>>,
    pub(crate) lock_modification_bytes: Arc<AtomicU64>,
}
```

注意一个关键字段:`rocks_engine: Option<RocksEngine>`。这说明 **IME 持有磁盘 RocksDB 的引用**——它不是独立的存储,而是 RocksDB 的一个"前置缓存"。数据真身还是在 RocksDB,IME 只是把一部分热 Region 的数据**复制**到内存里(用 skiplist 存,见 [`SkiplistEngine`](../tikv/components/in_memory_engine/src/engine.rs#L120)),读时先查内存。

它实现的是 [`RegionCacheEngine` trait](../tikv/components/in_memory_engine/src/engine.rs#L505)(不是 KvEngine!),区别在于它的 snapshot 要带 region 和 read_ts:

```rust
// in_memory_engine/src/engine.rs#L505(签名示意)
impl RegionCacheEngine for RegionCacheMemoryEngine {
    fn snapshot(&self, region: CacheRegion, read_ts: u64, seq_num: u64)
        -> result::Result<Self::Snapshot, FailedReason> { ... }
}
```

`CacheRegion`(在 region_cache_engine.rs#L155)用 `(region_id, epoch_version, start, end)` 标识一段被缓存的 key range——IME 是按 **Region** 粒度缓存的,不是按 key。

### IME 怎么不破坏一致性:共享 sequence number

这是 IME 设计最微妙的地方。RocksDB 用 sequence number 给每次写打版本(MVCC 在 RocksDB 层的机制,每个 key 内部带 seq)。如果 IME 独立维护自己的 seq,那磁盘和内存两份就可能不一致——读内存拿到一个版本、读磁盘拿到另一个版本,数据分叉。

IME 的解法在 `RegionCacheEngine` trait 的注释里那句重话:**"Sequence number is shared between RegionCacheEngine and disk KvEngine to provide atomic write"**。意思是——IME 不自己产 sequence number,**它从磁盘 RocksDB 借**。每次 Apply 写时:

- 先往磁盘 RocksDB 写(走正常 Apply 路径,RocksDB 给这次写分配一个 seq);
- Apply 同步地把这次写**镜像**到 IME(如果这个 Region 被缓存了),用**同一个 seq**。

这样,IME 里的数据和 RocksDB 里的数据,在 sequence number 上**完全对齐**——同一时刻,内存里的某个 key@seqN 和磁盘里的 key@seqN 是同一份。读时拿 IME 的 snapshot(seq_num 参数)就是拿 RocksDB 那个时刻的一致性快照,不会分叉。

> **技巧(how)**:共享 sequence number 是 IME 设计的核心 trick。它让 IME **不需要自己保证一致性**——一致性来自 RocksDB 的 seq,IME 只是把"RocksDB 已经保证一致的某段数据"复制了一份到内存。读 IME = 读 RocksDB 在某 seq 的快照。这种"借用下层的版本号"的做法,是对标 LevelDB/RocksDB 里 snapshot 用 seq 实现的那套(承接《LevelDB》)。

### IME 的 evict 和 load

IME 的"缓存"是 Region 粒度的,所以需要决定**哪些 Region 进缓存、什么时候踢出去**。看 [`EvictReason` 枚举](../tikv/components/in_memory_engine/src/../engine_traits/src/region_cache_engine.rs#L91):

```rust
// engine_traits/src/region_cache_engine.rs#L91-L106
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum EvictReason {
    LoadFailed,
    LoadFailedWithoutStart,
    MemoryLimitReached,
    BecomeFollower,      // 不是 leader 了,缓存没意义
    AutoEvict,           // 内存不够,自动踢
    DeleteRange,
    PrepareMerge,
    Merge,
    Disabled,
    ApplySnapshot,       // 收到 snapshot,内存版本作废
    Flashback,
    Manual,
    DestroyPeer,         // Peer 销毁
    IngestSST,           // ingest SST 会绕过 IME,直接踢
}
```

这一串理由反映了 IME 的生命周期:一个 Region 变成 leader 且被识别为热 → load 进 IME;变成 follower / 内存压力 / Peer 销毁 / ingest SST → evict。`load_region` 和 `evict_region` 是 IME 的两个核心操作,由后台 `BgWorkManager` 异步做。

### 一个诚实的边界:IME 是可选的缓存,不是必需

IME 默认是**关闭**的(`enable = false`),只有显式配置开启才会用。它的目标是**特定场景**(热 Region + 大内存机器)的优化,不是普适的。如果不开 IME,TiKV 就用纯 RocksDB,一切照常。这个"可选性"也是 trait 抽象带来的——不开 IME,上层根本感知不到它的存在。

> **架构演进**:IME 是 8.x 引入的新特性,解决的是"RocksDB 读放大在热 Region 场景下的痛点"。它的设计哲学是**不重写引擎,而是在引擎之上加一层选择性缓存**——共享 sequence number 保证一致性,按 Region 粒度缓存保证可管理。这是 TiKV 在"不破坏现有架构"前提下做性能优化的典型路径。

---

## 五、技巧精解:三个最硬核的设计

本章挑三个最值得拆透的技巧。

### 技巧一:为什么是三个 CF 而不是 N 个

上面讲 default/write/lock 三 CF 分工,但有个更尖锐的问题——**为什么不干脆每个 CF 都对应一类数据(比如再加个 `index` CF、`schema` CF),反而就停在三个?**

这是个典型的"切粒度"权衡。CF 越多,物理分离越彻底,但代价是:

- **每个 CF 独立的 MemTable 占内存**:每个 CF 至少一个 MemTable(active)+ 一个 Immutable(mem),每 MB 数据一份固定开销;
- **跨 CF 读放大**:读一个 key 的完整 MVCC 信息要跨 default + write 两个 CF 跳(还有 lock),CF 越多跳得越散;
- **WAL 共享的代价**:所有 CF 共享一个 WAL,但 WAL 里要记录"这条写属于哪个 CF",CF 越多元数据开销越大;
- **Block cache 的争抢**:Block cache 默认是实例级共享(可以配 per-CF),CF 越多,热数据的 cache 互相争抢。

TiKV 选三个 CF 是个甜点——default/write/lock 的访问模式**真的差到必须分开**(读模式、写模式、清墓碑节奏都不同),但再细分(比如把 default 拆成 `default_small` 和 `default_large`)收益就很小(都是 value,访问模式一样)。

> **不这么设计会怎样**:CF 太少(1 个)→ 三类数据互相拖累,Compaction 爆炸、优化空间锁死;CF 太多(N 个)→ MemTable / Block cache 开销膨胀、跨 CF 跳转放大。三个 CF 是"访问模式差异显著"和"管理开销可控"的甜点。这个甜点和 Region 96MB(8.3+ 是 256MB)是同一类工程权衡——粒度的艺术。

### 技巧二:engine_traits 的"超 trait 拼装"

看 `KvEngine` 的定义([engine.rs#L13](../tikv/components/engine_traits/src/engine.rs#L13))——它是个**纯组合的超 trait**,自己只定义 `type Snapshot`、`snapshot()`、`sync()` 几个方法,其他十几个能力全靠 `Peekable + Iterable + WriteBatchExt + ...` 这种 trait 拼装。

```rust
pub trait KvEngine:
    Peekable + SyncMutable + Iterable + WriteBatchExt + DbOptionsExt
    + CfNamesExt + CfOptionsExt + ImportExt + SstExt + CompactExt
    + RangePropertiesExt + MvccPropertiesExt + TtlPropertiesExt
    + TablePropertiesExt + PerfContextExt + MiscExt
    + Send + Sync + Clone + Debug + Unpin + Checkpointable + 'static
{
    type Snapshot: Snapshot;
    fn snapshot(&self) -> Self::Snapshot;
    fn sync(&self) -> Result<()>;
    // ...
}
```

**为什么这么拼而不写一个大 trait?** 这是个 Rust 工程技巧。如果写一个大 trait,里面有 50 个方法,那么:

- **每个方法都得实现**:换引擎时,即使你只想用其中 5 个方法,也得把另外 45 个 stub 出来;
- **测试 mock 痛苦**:想 mock 一个只支持 `get` 的引擎,得把所有方法都实现;
- **能力无法独立复用**:`Iterable`(扫描)这个能力,谁需要谁 `trait Iterable`,不需要就不依赖,清晰。

拆成小 trait 后,`KvEngine` 的"实现者"只要把每个小 trait 各自实现(每个小 trait 就几个方法,各自独立测试),`KvEngine` 自动派生——这就是 trait 组合的力量。

**反面对比**:如果用继承(像 OOP 那样),`KvEngine extends Peekable extends Iterable`,在 Rust 里 trait 没法继承实现,只能继承接口,所以本质还是组合。TiKV 这种 `A: B + C + D` 的写法是 Rust 里"接口组合"的标准姿势,对标《Tokio》里 `AsyncRead + AsyncWrite + AsyncSeek` 那种超 trait 拼装。

> **钉死这件事**:`KvEngine` 不是"一个 trait",是"十几个能力 trait 的拼装"。这种设计让"换引擎"时可以**按能力迁移**——新引擎先实现 `Peekable`(能点查),再加 `Iterable`(能扫描),再加 `WriteBatchExt`(能批写),逐步成为完整的 `KvEngine`。这是 trait 组合在大型系统里的典型用法。

### 技巧三:共享 sequence number 让 IME 不破坏一致性

IME 设计最大的风险是**数据分叉**——内存里一份、磁盘里一份,怎么保证读到的不会是"内存比磁盘新/旧"的不一致状态?

朴素做法是:IME 自己维护一套版本号,每次 Apply 时给 IME 的写分配自己的 seq。但这立刻有问题——Apply 是个**批量原子操作**(P3-11 会拆,ApplyFsm 攒一批写一次性提交),磁盘 RocksDB 给这一批分配一个 seq,如果 IME 自己另分 seq,那"这一批里第几条"在两边就对不上。

IME 的解法是**不自己分 seq,从磁盘 RocksDB 借**。注释里那句 "Sequence number is shared between RegionCacheEngine and disk KvEngine to provide atomic write" 就是这个意思:

- Apply 写磁盘 RocksDB 时,RocksDB 分配 seq=N;
- Apply 同步把这个写镜像到 IME,**用同一个 seq=N**;
- 读 IME 的 snapshot(seq_num=M) = 读 RocksDB 的 snapshot(seq_num=M),两边数据对齐。

这要求 IME 写和磁盘写**原子**——要么都成功,要么都失败。怎么做到?Apply 的 WriteBatch 把磁盘写和 IME 写打包成同一个 batch,Apply 一次提交。如果磁盘写失败,IME 写也作废(回滚 WriteBatch 的 save point)。

> **不这么写会撞墙**:IME 自己分 seq,内存和磁盘可能差好几个版本(IME 写快了 seq 领先,RocksDB 慢了 seq 落后),读时一会从内存拿一会从磁盘拿,version 来回跳,数据可见性乱了。借 seq 让两边永远对齐——这是个看似简单实则精妙的设计,本质是把"一致性保证"外包给 RocksDB(它已经把 seq 这套做 sound 了),IME 只复制。

---

## 六、架构演进:v1 一个 RocksDB vs v2 per-Region tablet

本章主要讲 v1 的"一个 store 一个 RocksDB,开三个 CF"。v2(raftstore-v2)的演进在存储层有个重要区别:

- **v1**:一个 store 一个 RocksDB 实例,所有 Region 的数据**混在一起**(按 data_prefix + region 的 key range 区分)。三个 CF 是全局的。
- **v2**:每个 Region 一个独立的 RocksDB tablet(叫 tablet 是借用 RocksDB 的 ColumnFamily-as-tablet 概念),数据物理上**按 Region 分文件目录**。

为什么 v2 改用 per-Region tablet?有几个动机:

- **分裂/迁移更快**:Region 分裂时,v1 要 scan 出新 Region 的数据(慢,见 P2-08);v2 直接复制 tablet 目录(checkpoint,几秒完成);
- **Compaction 隔离**:v1 的 Compaction 跨 Region(一个大 SST 里可能有好几个 Region 的数据),互相影响;v2 每个 tablet 独立 compact,隔离干净;
- **Snapshot 更高效**:P2-08 讲过,v2 用 RocksDB checkpoint API 克隆整个 tablet,比 v1 的 scan SST 快几个数量级。

v2 的代价是——**tablet 数量爆炸**(几十万个 Region = 几十万个 RocksDB 实例),管理开销大。这是 TiKV 在"单 RocksDB 多 CF"和"per-Region tablet"之间的架构取舍——v1 简单但分裂迁移慢,v2 灵活但管理复杂。

> **架构演进**:v1 → v2 在存储层的演进,是从"一个 RocksDB 多 CF"换成"per-Region RocksDB tablet"。CF 这个能力在 v2 里仍然存在(每个 tablet 内部还是有三个 CF),但"全局 RocksDB"没了。本书主线讲 v1,因为它最能讲清"为什么三个 CF";v2 是演进方向,P2-08 / P3-11 会点它的差异。

---

## 七、章末小结

### 回扣主线

本章是**复制层**的最后一章前奏——它讲的是"Raft commit 的命令最终落盘到哪个引擎、这个引擎怎么组织数据"。RocksDB 的三 CF 分工,让 MVCC 的多版本数据(下一章 P3-10 详拆)能按访问模式物理分离;`engine_traits` 抽象,让 raftstore / storage / Apply 这些上层模块能挂在任何符合 trait 的后端之上(RocksDB / RaftEngine / IME / tablet);in_memory_engine 是 8.x 的演进,在 RocksDB 之上加一层热数据缓存。

回到二分法:**复制层(每个 Region 怎么不丢不乱)vs 事务层(跨 Region 怎么拼出 ACID)**。本章服务的偏**复制层**——RocksDB 是 Raft 命令落盘的物理载体,CF 是数据组织的物理布局。但下一章 P3-10 会看到,三 CF 里存的 MVCC 数据(default=value、write=提交记录、lock=锁)其实是为**事务层**服务的——MVCC 编码是事务层能 ACID 的物理基础。所以本章是复制层和事务层的**衔接点**:物理上属于复制层(数据存在 RocksDB 里),逻辑上服务事务层(MVCC 是事务的根基)。

### 五个为什么

1. **为什么 RocksDB 要开三个 CF(default/write/lock)?**——这三类数据的访问模式(value 大、write 小、lock 临时)差到必须物理分离,塞一个 CF 会让 Compaction、Bloom、清墓碑策略互相拖累。三 CF 让它们各自独立 LSM-tree,共享外壳但内部独立优化。
2. **为什么 TiKV 要搞 engine_traits 抽象?**——上层(raftstore、storage)需要能挂多个后端(RocksDB、RaftEngine、IME、tablet),直接依赖 rocksdb 会焊死。trait 让上层只对抽象编程,启动时注入具体实现,局部换骨架。
3. **为什么 `KvEngine` 和 `RaftEngine` 是两个独立 trait?**——KV 数据和 Raft 日志是两类数据(前者随机读写+范围扫,后者顺序追加+按 index 取),访问模式根本不同,分两个引擎、两个 trait。这是 Raft 日志要单独存(P2-06)的根。
4. **为什么 in_memory_engine 不自己分 sequence number?**——IME 和磁盘 RocksDB 共享 seq,保证同一时刻内存和磁盘的数据完全对齐(读 IME = 读 RocksDB 在某 seq 的快照)。如果 IME 自己分 seq,两边可能差好几个版本,数据可见性乱。借 seq 让一致性外包给 RocksDB。
5. **为什么 raftstore-v2 改用 per-Region tablet 而不是全局 RocksDB?**——分裂/迁移更快(checkpoint 克隆 vs scan SST)、Compaction 隔离干净、Snapshot 更高效。代价是 tablet 数量爆炸、管理复杂。v1 简单适合讲原理,v2 灵活是演进方向。

### 想继续深入往哪钻

- **RocksDB 的 CF 机制本体**(MemTable/SST/Compaction 怎么按 CF 工作):读《LevelDB》对应章节(本章承接不重复)。
- **`KvEngine` / `RaftEngine` / `RegionCacheEngine` trait 全貌**:读 [`components/engine_traits/src/`](../tikv/components/engine_traits/src/)(本章大量引用:`engine.rs`、`raft_engine.rs`、`region_cache_engine.rs`、`cf_defs.rs`)。
- **RocksEngine 的具体实现**:读 [`components/engine_rocks/src/engine.rs`](../tikv/components/engine_rocks/src/engine.rs)(`RocksEngine` 结构、`impl KvEngine`)、[`cf_names.rs`](../tikv/components/engine_rocks/src/cf_names.rs)、[`write_batch.rs`](../tikv/components/engine_rocks/src/write_batch.rs)(WriteBatch 的 CF 写)。
- **in_memory_engine 的完整实现**:读 [`components/in_memory_engine/src/engine.rs`](../tikv/components/in_memory_engine/src/engine.rs)(`RegionCacheMemoryEngine`)、`background.rs`(load/evict 后台)、`memory_controller.rs`(内存预算)、`keys.rs`(IME 的 key 编码,带 seq)。
- **三个 CF 各存什么的源码级细节**:读下一章 P3-10。
- **Apply 怎么把 Raft 命令转成 CF 写**:读 P3-11。

### 引出下一章

讲完了 RocksDB 引擎和它的三 CF 分工,自然要问——**三个 CF 里到底各存什么?key 怎么编码才能让"同一个 key 的多个版本"在 RocksDB 里有序排列?MVCC 的 default/write/lock 三 CF,具体存什么 bytes?** 下一章 P3-10,我们拆透 MVCC 的 key+ts 编码和三 CF 的物理内容,这是 TiKV 事务层的物理根基。

> **下一章**:[P3-10 · MVCC 编码:key + 时间戳](P3-10-MVCC编码-key加时间戳.md)
