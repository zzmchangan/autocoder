# 第 6 篇 · 第 19 章 · Coprocessor:把计算下推

> **核心问题**:一条 `SELECT sum(price) FROM orders WHERE city = 'SH'` 如果老老实实地执行,要把 `orders` 表里成千万行的 `price` 全部从 TiKV 拉到 TiDB,再在 TiDB 端做过滤和求和。可真正回给用户的,只是 `sum` 算出来的一个数字。这中间几千万行的网络传输,99% 都是浪费。TiKV 的 Coprocessor 干的事,就是**把过滤、聚合这些计算"搬到数据所在的 TiKV 上执行**,只把最终结果传回 TiDB**——省网络是它的第一性动机,但远不止于此。

> **读完本章你会明白**:
> 1. 为什么"计算下推"不是个性能优化的小技巧,而是分布式数据库**必须做**的事——不下推,网络会成为不可逾越的瓶颈。
> 2. TiKV 的 Coprocessor 不是"在 TiKV 里跑 SQL",而是跑一个 **DAG 执行计划**(TiDB 把 SQL 编译成一棵算子树,序列化成 protobuf 发给 TiKV),TiKV 拿到的是一棵"先 TableScan → 再 Selection(过滤)→ 再 Aggregation(聚合)"的树,自己用 volcano-like 模型驱动它。
> 3. tipb 是 TiDB 和 TiKV 之间约定的 protobuf 协议(算子、表达式、类型的 schema),它是 Coprocessor 的"语言"——TiKV 不解析 SQL,只解码 tipb。
> 4. 一条 Coprocessor 请求怎么从 gRPC `coprocessor` RPC 进来,经 `endpoint.rs` 派发成 DAG/Analyze/Checksum 三类,再被 `BatchExecutorsRunner` 拉动执行——以及它为什么跑在 read pool 上、为什么和事务读共用一套 MVCC 快照。
> 5. 这套机制和《gRPC》那本讲的 HTTP/2 流、和《LevelDB》讲的 RocksDB 之间的承接关系。

> **如果一读觉得太难**:先只记住三件事——① Coprocessor 的本质是"把计算搬到数据旁边跑,省网络";② TiDB 不发 SQL 给 TiKV,发的是一棵用 protobuf 序列化的 DAG 算子树(tipb);③ TiKV 用 volcano 模型一棵一棵拉动这棵树,底层扫数据用的是和事务读同一套 MVCC 快照。

---

## 〇、一句话点破

> **Coprocessor 把 TiDB 编译好的"算子树"序列化成 protobuf 下推到 TiKV,TiKV 不解析 SQL、只解码这棵树,用 volcano 模型在数据旁边拉动它,把"扫千万行 + 过滤 + 聚合"压缩成"回一个结果"——这是分布式数据库把网络这个瓶颈关进笼子的核心招式。**

这是结论,不是理由。本章倒过来拆:先讲不下推会怎样(为什么必须下推),再讲下推要解决的两个子问题(TiKV 凭什么能"跑 SQL"——其实是跑 DAG;两边怎么约定算子的"语言"),再讲 DAG 执行模型和源码落点,最后讲 read pool / 缓存 / 批处理这些工程化技巧。

---

## 一、不下推会怎样:网络是不可逾越的瓶颈

### 一个反例:朴素执行

假设没有 Coprocessor,一条 `SELECT count(*), sum(price) FROM orders WHERE city = 'SH'` 要怎么跑?最朴素的方案是:

1. TiDB 知道 `orders` 表的数据按 Region 分布在哪些 TiKV 上;
2. TiDB 对每个相关 Region 发一个"把这一段 key range 的数据全给我"的请求(本质是个 range scan);
3. TiKV 老老实实把这一段几百万行的 `city`、`price` 列,编码成 KV,通过 gRPC 流式传回 TiDB;
4. TiDB 在自己内存里做过滤(`city = 'SH'`)、做聚合(`count`、`sum`)。

这能跑通,但在分布式场景下是个灾难。灾难的根源不是 CPU、不是磁盘,是**网络**。

> **不这样会怎样**:一个 Region 现在默认 256MB(8.3.0 之前是 96MB)。哪怕只扫 10 个 Region,就是 GB 级的数据要从 TiKV 涌向 TiDB。千兆网卡理论 125MB/s,这一个查询就要传好几秒;万兆网卡也就是 1.25GB/s,同时跑几十个这样的查询就把带宽吃满。**而且这些数据里 99% 是要被过滤掉、或者要被聚合压缩成一个数字的**——传过去只是为了在 TiDB 端算完扔掉。这是纯粹的浪费。更阴险的是,网络传输还会反噬 TiDB 的 CPU(解 protobuf、解码 KV)、内存(攒着几百万行做聚合),让协调节点比存储节点还累——**职责完全倒挂**。

### 所以这样设计:把计算搬到数据旁边

分布式数据库的核心直觉是:**数据是大的、计算结果是小的,应该让计算去找数据,而不是让数据来找计算**。这就是"计算下推"(push down)。具体到 TiKV:

- TiDB 不要原始数据,而是把"我要干什么"(过滤条件 `city = 'SH'`、聚合函数 `count/sum`)告诉 TiKV;
- TiKV 在自己的进程里,对这一段 Region 的数据,边扫边过滤、边聚合;
- 最后只把聚合结果(几个数字)传回 TiDB。

```
   朴素做法(不下推):                  下推做法(Coprocessor):
   TiKV ──几百万行原始数据──▶ TiDB      TiKV ──┐
   (TiDB 端过滤+聚合)                   │ 在 TiKV 内:扫→过滤→聚合
                                       └──只有结果(sum=...)──▶ TiDB
   网络传 GB 级                          网络传 KB 级
```

> **钉死这件事**:计算下推的第一性动机是**省网络**——把"大 → 小"的变换(过滤丢掉大部分行、聚合把多行压成一行)放在数据旁边完成,只传变换后的小结果。这个动机解释了 Coprocessor 几乎所有设计:为什么它接收的是"算子树"而不是 SQL、为什么它要支持 streaming 模式、为什么结果要带 chunk 编码。每一个都是"省网络"这条主线的展开。

但"把计算搬到 TiKV"立刻引出两个难题,这两个难题就是本章后面要拆的:

1. **TiKV 凭什么能"跑 SQL"?** 它是个 KV 存储,没有 SQL 解析器、没有优化器。答案是:它不跑 SQL,它跑一棵 **DAG 算子树**,而且这棵树是 TiDB 编译好、序列化成 protobuf 发过来的——TiKV 只负责"解码 + 执行"。
2. **TiDB 和 TiKV 怎么约定"算子"长什么样?** 两边是不同的进程、不同的代码库(TiDB 是 Go,TiKV 是 Rust)。答案是 **tipb**——一份共享的 protobuf schema,定义了所有算子、表达式、数据类型的二进制格式。

下面逐个拆。

---

## 二、TiKV 不跑 SQL,跑一棵 tipb 描述的 DAG 算子树

### 为什么不在 TiKV 里跑 SQL

一个朴素的想法是:TiDB 把 `SELECT sum(price) FROM orders WHERE city='SH'` 这条 SQL 原文发给 TiKV,TiKV 自己解析、优化、执行。这条路立刻坏掉几件事:

1. **重复劳动**:TiDB 已经有完整的 SQL 解析器、优化器(代价很大的活),让每个 TiKV 也来一遍,既浪费又是维护噩梦(两边优化器逻辑要对齐)。
2. **优化需要全局视图**:查询优化是全局决策(选哪个索引、表怎么 join),只有协调者 TiDB 看得到全局,TiKV 看不到。让 TiKV 自己优化,它会做出次优计划。
3. **职责分离**:TiDB 的定位就是"接收 SQL、编译成分布式执行计划、协调执行";TiKV 的定位是"存数据、执行具体的算子"。各司其职才清爽。

> **所以这样设计**:TiDB 把 SQL 编译成一棵**算子树**(TableScan → Selection → Aggregation),这棵树描述了"对这段数据按什么顺序执行什么操作"。然后 TiDB 把这棵树**序列化成 protobuf**,连同要扫的 key ranges 一起,通过 gRPC 发给 TiKV。TiKV 收到的是结构化的执行计划,不是 SQL 文本——它只负责"解码这棵树 + 拉动执行"。

这就是为什么 TiKV 的 Coprocessor 模块里**没有 SQL parser、没有优化器**,只有 executor(算子的具体实现)。

### tipb:两边共享的"算子语言"

TiDB(Go)和 TiKV(Rust)要交换"一棵算子树",必须有共同的语言。这份语言就是 **tipb**(TiDB Protobuf),它定义在单独的仓库 `pingcap/tipb` 里,被 TiDB 和 TiKV 同时依赖。tipb 里定义了:

- **`Executor`**:所有算子的 schema。一个 `Executor` 有个 `tp`(类型:TableScan / IndexScan / Selection / Aggregation / Limit / TopN / Projection / IndexLookUp ...),和一个对应类型的详细字段(比如 Selection 里装着它的过滤表达式)。
- **`Expr`**:表达式(列引用、常量、比较、算术、函数调用)。`city = 'SH'` 就是一个 `Expr`(`tp = ScalarFunc`,函数名是比较,左右孩子是列引用和常量)。
- **`FieldType`**:列的类型信息(INT、VARCHAR、FLOAT、DECIMAL ...)。
- **`DagRequest`**:一次 Coprocessor DAG 请求的顶层消息,里面装着 `executors`(一串 `Executor`)、`collect_token`、时间戳等。

```
   TiDB 端                            TiKV 端
   ┌────────────────────┐             ┌──────────────────────┐
   │ SQL 文本            │             │  收到 DagRequest      │
   │  ↓ parse           │             │  ↓ protobuf 解码       │
   │ AST                 │             │  Vec<tipb::Executor>  │
   │  ↓ optimize        │   gRPC      │  ↓ build_executors    │
   │ 物理算子树          │ ─────────▶ │  Box<dyn BatchExecutor>│
   │  ↓ 序列化           │ DagRequest │  ↓ volcano 拉动        │
   │ tipb::DagRequest   │  (protobuf)│  执行 → 结果 chunk     │
   └────────────────────┘             └──────────────────────┘
```

> **钉死这件事**:tipb 是 Coprocessor 的契约。它把"算子树"固化成二进制格式,让两个不同语言、不同代码库的进程能精确地交换执行计划。**TiKV 永远不解析 SQL,只解码 tipb**——这是职责分离的硬边界。这也是为什么 Coprocessor 的所有改动(加新算子、新函数)都要先改 tipb 仓库、再两边同步升级版本。

### 三类请求:DAG、Analyze、Checksum

进 Coprocessor 的请求不止"查数据"。`endpoint.rs` 在解析请求时,根据请求头的 `tp` 字段把它分成三类(`parse_request_and_check_memory_locks_impl`,见 [parse_request_and_check_memory_locks_impl](../tikv/src/coprocessor/endpoint.rs#L188)):

- **`REQ_TYPE_DAG`**:DAG 请求,最常见。TiDB 发一棵 `DagRequest`(算子树),TiKV 执行后返回 `SelectResponse`(结果行,用 chunk 编码)。
- **`REQ_TYPE_ANALYZE`**:统计信息收集。TiDB 要为优化器收集表的直方图、Count-Min Sketch 等统计信息,把这些计算的算子下推到 TiKV。
- **`REQ_TYPE_CHECKSUM`**:算表数据的校验和(用于校验 backup、数据迁移是否一致)。

这三类共享同一套 Coprocessor 基础设施(gRPC 入口、read pool 调度、MVCC 快照),只是 `RequestHandler` 不同。下面重点讲 DAG,它是主线。

---

## 三、DAG 执行模型:volcano 一样的拉动式执行

### 一棵算子树怎么变成结果

TiKV 收到 `DagRequest` 后,要执行里面的算子树。这棵树长这样(以 `SELECT sum(price) FROM orders WHERE city='SH'` 为例):

```
   Aggregation(sum(price))           ← 根:聚合
        ▲ next_batch()
        │
   Selection(city = 'SH')            ← 中间:过滤
        ▲ next_batch()
        │
   TableScan(orders, [col=price, city])  ← 叶:扫表
        ▲
     RocksDB(MVCC 三 CF)
```

执行这棵树的经典模型叫 **volcano / iterator model**(火山模型):每个算子实现一个 `next_batch()` 接口,根算子调用孩子的 `next_batch()` 拿一批数据、处理完往上递。TiKV 用的就是这个模型的批量变体——`BatchExecutor` trait,每次 `next_batch()` 吐一批行(不是一行),摊薄虚函数调用开销。

来 `runner.rs` 看真身。`BatchExecutorsRunner` 是 DAG 请求的执行器,它的 `handle_request` 就是驱动整棵算子树跑完、收集结果的地方([BatchExecutorsRunner::handle_request](../tikv/components/tidb_query_executors/src/runner.rs#L840)):

```rust
// 简化示意,非源码原文:BatchExecutorsRunner::handle_request 的骨架
pub async fn handle_request(&mut self) -> Result<(SelectResponse, Option<IntervalRange>)> {
    let mut chunks = Vec::new();
    // 反复调用根算子的 next_batch,直到它说"没数据了"(logical_rows == 0 表示扫完)
    loop {
        let (drained, scan_details) = self.inner_batch.next_batch(BATCH_MAX_SIZE).await?;
        if scan_details.logical_rows == 0 && drained {
            break;
        }
        // 把这一批结果编码成 chunk,加进要返回的列表
        ...
    }
    Ok((SelectResponse { chunks, ... }, ...))
}
```

### build_executors:把 protobuf 的算子序列,串成 rust 的算子树

`handle_request` 拉动的是根算子。那"根算子"是从哪来的?答案是 `build_executors`,它把 `DagRequest` 里的一串 `tipb::Executor` 描述符,逐个翻译成 Rust 的 `Box<dyn BatchExecutor>`,并用 `.collect_summary()` 串起来([build_executors](../tikv/components/tidb_query_executors/src/runner.rs#L307)):

```rust
// 简化示意(基于源码结构,非逐字):
pub fn build_executors<...>(executor_descriptors: Vec<tipb::Executor>, ...) -> Result<Box<dyn BatchExecutor<...>>> {
    let mut it = executor_descriptors.into_iter();
    let first = it.next().ok_or_else(|| other_err!("No executors"))?;
    // 第一个算子必须是 TableScan 或 IndexScan(叶子,负责扫数据)
    let mut executor: Box<dyn BatchExecutor<...>> = match first.get_tp() {
        ExecType::TypeTableScan => BatchTableScanExecutor::new(...)?.collect_summary(0),
        ExecType::TypeIndexScan => BatchIndexScanExecutor::new(...)?.collect_summary(0),
        _ => return Err(other_err!("Unexpected first executor")),
    };
    // 后续算子按 parent_idx 串到前一个上面:Selection / Aggregation / Limit / TopN ...
    for ed in it {
        executor = match ed.get_tp() {
            ExecType::TypeSelection => BatchSelectionExecutor::new(executor, ...)?.collect_summary(...),
            ExecType::TypeAggregation => BatchSimpleAggrExecutor::new(executor, ...)?.collect_summary(...),
            ExecType::TypeLimit => BatchLimitExecutor::new(executor, ...)?.collect_summary(...),
            ExecType::TypeTopN => BatchTopNExecutor::new(executor, ...)?.collect_summary(...),
            ExecType::TypeProjection => BatchProjectionExecutor::new(executor, ...)?.collect_summary(...),
            ...
        };
    }
    Ok(executor)
}
```

注意这里有个 TiKV 自己的小改进:protobuf 里的 `executors` 是个**数组**,每个元素带个 `parent_idx` 指明它的父节点是数组的第几个——这样一棵树(可能有多个中间算子)可以扁平地存在数组里。`build_executors` 按顺序消费这个数组,用 `parent_idx` 决定新算子挂在谁后面。这比递归地序列化一棵树更紧凑(protobuf 数组比嵌套 message 更省空间),也是省网络的一个细节。

### 一棵算子树能做的事:为什么 TableScan 一定是叶子

注意 `build_executors` 里有一个硬约束:**第一个算子(叶子)必须是 TableScan 或 IndexScan**,否则报错。为什么?

因为 Coprocessor 是"对一段 Region 的数据执行计算"——数据的来源只有两种:扫表(TableScan,扫 default/write CF 拿数据)、扫索引(IndexScan,扫索引 CF)。没有别的数据源(不会有 join 的另一边——join 是 TiDB 端做的全局协调,不下推到单个 Region)。所以叶子一定是 scan,上面才是 Selection/Aggregation/Limit 这些变换。

> **钉死这件事**:DAG 模型 = 叶子 scan + 一串变换算子,用 volcano 拉动式执行。这个结构决定了 Coprocessor 能下推什么、不能下推什么:**凡是能用"扫一段数据 + 过滤 + 聚合 + 排序 + limit"表达的查询,都能下推**;凡是需要跨 Region 协调的(join、跨 Region 的分布式聚合),TiDB 自己在结果层面做——每个 Region 的 Coprocessor 算出局部聚合,TiDB 把局部聚合再汇总成全局聚合。

这就是为什么 TiDB 端做 join 时,经常看到 `HashJoin` 配合两侧各一个 Coprocessor 请求——每个 Region 跑一棵 DAG 拿局部结果,TiDB 端做 join。

---

## 四、入口与调度:从 gRPC 到 read pool

### gRPC 入口:coprocessor RPC

一条 Coprocessor 请求,从 TiDB 发出来后,先到 TiKV 的 gRPC service。gRPC 那一层(HTTP/2、HPACK、流)**承接《gRPC》那本**,本书只看 TiKV 在 service 层怎么接:

在 `src/server/service/kv.rs` 里,TiKV 给 `tikvpb::Tikv` trait 实现了 `coprocessor` 方法,这就是 gRPC 入口([coprocessor](../tikv/src/server/service/kv.rs#L590)):

```rust
// 简化示意:
fn coprocessor(&mut self, ctx: RpcContext<'_>, req: Request, sink: UnarySink<Response>) {
    forward_unary!(self.proxy, coprocessor, ctx, req, sink);  // 必要时转发给别的 TiKV
    let begin_instant = Instant::now();
    let future = future_copr(&self.copr, Some(ctx.peer()), req);  // 交给 coprocessor::Endpoint
    let task = async move {
        let resp = future.await?.consume();
        sink.success(resp).await?;
        // 记录耗时指标 ...
    };
    ctx.spawn(task);
}
```

`future_copr` 把请求转给 `self.copr`(一个 `coprocessor::Endpoint`)。注意这里还有个 `coprocessor_stream` RPC([coprocessor_stream](../tikv/src/server/service/kv.rs#L731)),用于 streaming 模式——结果不一次性返回,而是边算边通过 gRPC server stream 吐给 TiDB。这在 OLAP 大查询(返回几百万行)时特别重要,避免一次性把结果全攒在 TiKV 内存里。

### endpoint.rs:解析、调度、执行

`coprocessor::Endpoint` 是 Coprocessor 的总调度(见 [Endpoint](../tikv/src/coprocessor/endpoint.rs#L71))。它有几个关键字段:

- `read_pool: ReadPoolHandle` —— Coprocessor 请求最终跑在这个线程池上(不是 gRPC 线程,因为 Coprocessor 是 CPU/IO 重活,不能阻塞 gRPC 线程);
- `semaphore: Option<Arc<Semaphore>>` —— 并发限制器,防止 Coprocessor 把 read pool 占满影响事务读;
- `memory_quota: Arc<MemoryQuota>` —— 内存配额,防止单个大查询 OOM。

请求处理的核心路径是 `parse_request_and_check_memory_locks` + `handle_unary_request`,在 `endpoint.rs` 里串起来(见 [parse + handle](../tikv/src/coprocessor/endpoint.rs#L676-L678)):

```rust
// 简化示意:
let result_of_future = self
    .parse_request_and_check_memory_locks(req, peer, false)   // 解析 tipb,生成 RequestHandler builder
    .map(|r| self.handle_unary_request(r));                    // 把 builder 扔进 read pool 执行
```

`parse_request_and_check_memory_locks_impl` 干三件事([源码](../tikv/src/coprocessor/endpoint.rs#L188)):

1. **解码 tipb**:根据 `tp` 是 DAG/Analyze/Checksum,从 `data` 里反序列化出对应的请求结构(`DagRequest`、`AnalyzeReq`、`ChecksumRequest`)。
2. **构造 `ReqContext`**:把 start_ts、ranges、隔离级别、deadline 等打包。
3. **`check_memory_locks`**:这是个关键的正确性检查——在下推执行前,先查一下这段 key range 里有没有内存中的悲观锁(在 `in_memory_engine` 场景下尤其重要)。如果有锁会和这次读冲突,提前返回锁信息让 TiDB 重试或等待,避免执行半天才发现冲突。

> **不这样会怎样**:如果不做 `check_memory_locks`,一个 Coprocessor 请求可能扫了几百万行、聚合到一半,才碰到一个悲观锁,然后要么返回错误(TiDB 重试,前面的活全白干)、要么读到不一致的数据。提前查锁,把"必然要失败"的请求在执行前就挡掉,是性能和正确性的双重保险。

### read pool:和事务读共用一套 MVCC 快照

注意一个关键点:**Coprocessor 不是另起一套读路径,它用的是和普通事务读(`get`/`batch_get`/`scan`)同一套 MVCC 快照机制**。在 `parse_request_and_check_memory_locks_impl` 的 DAG 分支里(见 [DAG 分支](../tikv/src/coprocessor/endpoint.rs#L260-L273)):

```rust
handler_builder = Box::new(move |snap, req_ctx| {
    let store = SnapshotStore::new(
        snap,                                     // 一个 RocksDB 快照
        start_ts.into(),                          // 读这个 start_ts 之前的版本
        req_ctx.context.get_isolation_level(),
        !req_ctx.context.get_not_fill_cache(),
        req_ctx.bypass_locks.clone(),             // 遇到这些锁可以绕过(比如已提交的 Primary 的锁)
        req_ctx.access_locks.clone(),
        req.get_is_cache_enabled(),
    );
    ...
});
```

`SnapshotStore` 就是事务读用的那个 MVCC store(在 `src/storage` 里)。它拿一个 RocksDB 快照(保证读期间数据不变),按 `start_ts` 找可见版本(扫 write CF 找 ≤ start_ts 的最大 commit_ts 对应的版本,详见 P3-10 MVCC 编码和 P4-15 MVCC 读)。**Coprocessor 的 TableScan/IndexScan,本质上就是带 start_ts 的 range scan**——它和 `Storage::scan` 走的是同一套 MVCC 读逻辑,只是结果交给算子树继续处理,而不是直接返回给客户端。

> **钉死这件事**:Coprocessor 读 = MVCC range scan + 算子树处理。它复用事务读的快照、锁检查、可见性判断,所以**读到的数据是事务一致的**(在 RC/RR 隔离级别下,读到的是 start_ts 时刻的快照)。这是为什么 Coprocessor 能用于事务里的查询(不只是 OLAP)——它本来就是事务读的一种,只是顺带做了计算。

---

## 五、表达式与聚合:tidb_query_* 组件矩阵

Coprocessor 的执行能力,分摊在 `components/tidb_query_*` 一组组件里。每个组件管一类能力:

| 组件 | 管什么 | 典型文件 |
|------|--------|----------|
| `tidb_query_common` | 公共基础设施:trait(`BatchExecutor`)、`EvalConfig`、统计、错误 | `components/tidb_query_common/src/` |
| `tidb_query_datatype` | tipb 的 `FieldType` 对应的 Rust 类型系统(MySQL 兼容的类型:Int、Decimal、Duration、Json ...) | `components/tidb_query_datatype/src/` |
| `tidb_query_expr` | 表达式求值:列引用、常量、比较、算术、各种内置函数(`-upper`、`date_format` ...) | `components/tidb_query_expr/src/` |
| `tidb_query_aggr` | 聚合函数实现:`sum`、`count`、`avg`、`max`、`min`、`first`、bit 算、方差 | `components/tidb_query_aggr/src/impl_*.rs` |
| `tidb_query_executors` | 算子实现:TableScan / IndexScan / Selection / Limit / TopN / 各种 Aggr | `components/tidb_query_executors/src/*.rs` |
| `tidb_query_codegen` | 过程宏:用宏批量生成表达式/聚合函数的分发代码(避免手写海量 match) | `components/tidb_query_codegen/` |

注意 `tidb_query_aggr` 里聚合是按函数拆文件的([`impl_sum.rs`](../tikv/components/tidb_query_aggr/src/impl_sum.rs)、[`impl_avg.rs`](../tikv/components/tidb_query_aggr/src/impl_avg.rs)、[`impl_count.rs`](../tikv/components/tidb_query_aggr/src/impl_count.rs)、[`impl_max_min.rs`](../tikv/components/tidb_query_aggr/src/impl_max_min.rs)、[`impl_variance.rs`](../tikv/components/tidb_query_aggr/src/impl_variance.rs) ...)——每个聚合函数的状态机(update、merge、output)各管各的,清晰隔离。

为什么聚合要分这么多实现类?看一眼 `tidb_query_executors/src/` 里的 executor 列表就懂了:

- `simple_aggr_executor.rs`:简单聚合(无 group by,比如 `count(*)`、`sum(price)`);
- `fast_hash_aggr_executor.rs`:快速哈希聚合(group by 列是整数,用数组下标做哈希);
- `slow_hash_aggr_executor.rs`:慢速哈希聚合(group by 列是字符串/复杂类型,用真哈希表);
- `stream_aggr_executor.rs`:流式聚合(group by 列有序,边扫边合并相邻的相同组,不开哈希表);
- `partition_top_n_executor.rs`:分区 TopN(topN 配合 partition by)。

> **不这样会怎样**:聚合的实现高度依赖数据特征。如果只有一个通用聚合实现(比如一律用慢速哈希表),那么 `count(*)`(无 group by)和 `group by int_col`(整数分组)这种本可以极快的场景,会被拖到和 `group by varchar` 一样的速度。TiKV 把这些实现拆开,让 TiDB 优化器根据查询特征选最优的那一个下推——**这是"算子树"模型的另一个红利:算子是可插拔的,加新实现不影响别的**。

---

## 六、工程化技巧:批处理、缓存、配额

Coprocessor 在生产里还要解决几个工程问题,这些是它"能用"的关键。

### 批处理:一个请求扫多个 Region

一个大查询往往要扫几十上百个 Region。如果每个 Region 一个 gRPC 请求,TiDB 要管理海量连接、TiKV 要处理海量小请求(每个都有 gRPC 解码、调度开销)。所以 tipb 请求里支持 `tasks` 字段——一个请求可以装多个"子任务",每个子任务扫一段 range、落在某个 Region。`endpoint.rs` 的 `process_batch_tasks` 就是处理这种批量请求的([process_batch_tasks](../tikv/src/coprocessor/endpoint.rs#L713)):

```rust
// 简化示意:
pub fn process_batch_tasks(&self, req: &mut coppb::Request, peer: &Option<String>)
    -> impl Future<Output = Vec<coppb::StoreBatchTaskResponse>>
{
    let batch_reqs: Vec<(coppb::Request, u64)> = req.take_tasks().iter_mut().map(|task| {
        // 每个 task 拆成一个独立的子请求,带上自己的 region_id/epoch/peer
        ...
    }).collect();
    // 每个子请求独立调度到 read pool,结果用 FuturesOrdered 串起来返回
    stream::FuturesOrdered::from_iter(batch_futs).collect()
}
```

这是"把多个小请求打包成一个大请求"的经典优化——摊薄 gRPC 往返和调度开销。

### 结果缓存:Coprocessor cache

如果同一个查询(同样的 SQL、同样的 start_ts、同样的 ranges)被重复执行,每次都重新扫数据太浪费。tipb 请求里有 `is_cache_enabled` / `cache_if_match_version` 字段,支持**结果缓存**:TiKV 算出结果后,连带数据版本号缓存;下次同样请求来,如果底层数据版本没变(用 Region 的 `version`/`conf_ver` 判断),直接返回缓存结果。这是读多写少场景(比如报表查询)的利器。

### 内存与并发配额

Coprocessor 是潜在的"内存大户"——一个大聚合可能在内存里攒几百万行的中间状态。所以 `Endpoint` 有 `memory_quota`(单请求内存上限)和 `semaphore`(并发上限)双重保护:

- 超过内存配额,请求被 reject(返回 `MemoryQuotaExceeded` 错误),防止单查询 OOM 拖垮整个 TiKV;
- 并发超过 semaphore 上限,请求排队(防止 Coprocessor 把 read pool 占满,影响延迟敏感的事务读)。

还有一个 `LIGHT_TASK_THRESHOLD`(5ms,见 [LIGHT_TASK_THRESHOLD](../tikv/src/coprocessor/endpoint.rs#L67))——预估执行时间小于这个阈值的"轻任务",可以不申请 semaphore 直接跑(避免短任务也走排队开销)。

> **钉死这件事**:Coprocessor 是 OLAP 风格的负载(大扫描、重计算),而 TiKV 主要服务 OLTP(小查询、低延迟)。让 OLAP 和 OLTP 共存,靠的是**隔离**:read pool 分池(可以配置 coprocessor 专用线程数)、semaphore 限并发、memory_quota 限内存。没有这套隔离,一个慢分析查询能把整个 TiKV 的事务读拖卡——这是共享存储的经典难题。

---

## 七、Coprocessor V2:演进方向的对照

讲到这里必须交代一个架构演进:TiKV 其实有**两套** Coprocessor:

1. **经典 Coprocessor**(`src/coprocessor/`):就是本章讲的主力。它面向 TiDB,跑 tipb 描述的 DAG,有完整的 SQL 算子(Scan/Selection/Aggregation/...)支持。**这是生产里的主线,本章以它为准**。
2. **Coprocessor V2**(`src/coprocessor_v2/`):一个新的**插件化**框架。它不跑 tipb DAG,而是允许用户**加载自定义的 WASM/native 插件**,对 RawKV 数据执行任意计算。入口是 `RawCoprocessorRequest`(走 `raw_coprocessor` RPC,见 [raw_coprocessor](../tikv/src/server/service/kv.rs#L633)),由 `coprocessor_v2::Endpoint::handle_request` 派发给对应的插件([handle_request](../tikv/src/coprocessor_v2/endpoint.rs#L56))。

两者的区别:

| 维度 | 经典 Coprocessor (v1) | Coprocessor V2 |
|------|----------------------|----------------|
| 面向 | TiDB(SQL 下推) | 自定义插件(任意计算) |
| 协议 | tipb(DAG/Analyze/Checksum) | `RawCoprocessorRequest`(自定义 data) |
| 算子 | 内置 TableScan/Selection/Aggr ... | 插件自己实现(`on_raw_coprocessor_request`) |
| 数据 | TiDB 的 MVCC 表(RocksDB 三 CF) | RawKV(原始 KV) |
| 加载方式 | 编译进 TiKV | 动态加载(`coprocessor_plugin_directory`,支持热重载) |
| 隔离 | 进程内 | 进程内(未来可能 WASM 沙箱) |

> **为什么会有 V2**:经典 Coprocessor 的算子是 TiDB 强约定的——加个新算子要改 tipb、改 TiDB、改 TiKV,三边同步发版,迭代慢。而有些场景(比如在 TiKV 上跑自定义的图计算、机器学习推理)根本不在 TiDB 的 SQL 范畴里。V2 给这些场景开了一扇门:**用户写个插件(动态库),TiKV 加载它,对 RawKV 数据执行任意逻辑**。这是把"计算下推"从一个"SQL 算子下推"泛化成"任意计算下推"。目前 V2 还在演进中,生产里经典 Coprocessor 仍是主线。

本章主要拆经典 Coprocessor(它是理解"计算下推"原理的核心),V2 作为演进方向知道它存在、和经典版的区别即可。

---

## 八、技巧精解

本章挑两个最硬核的技巧单独拆透。

### 技巧一:volcano 模型的批量变体——为什么 next_batch 而不是 next

经典 volcano 模型里,每个算子有个 `next()` 方法,返回**一行**。这种"一行一行拉"的模型优雅、易实现,但在高性能执行引擎里有个致命问题:**虚函数调用太多**。一个 `Selection.next()` 调 `TableScan.next()`,每行都是一次动态分发(Rust 里 `Box<dyn Trait>` 的方法调用)。扫一百万行就是一百万次虚函数调用,光分支预测失败的开销就不得了。

TiKV 的 `BatchExecutor` trait 用的是**批量变体**:每次 `next_batch()` 吐**一批**行(默认 BATCH_MAX_SIZE,通常几十到几百行),用 chunk 的列式格式存放。这样:

- 虚函数调用从"每行一次"摊薄到"每批一次",开销降两个数量级;
- 列式格式(chunk)对 CPU cache 友好(连续内存)、便于 SIMD(同一列同类型,可向量化);
- 下层的 scan 一次性从 RocksDB 拿一批 KV,而不是一行一行 Get,IO 效率高。

```rust
// BatchExecutor trait 的核心(简化示意):
pub trait BatchExecutor {
    async fn next_batch(&mut self, scan_rows_limit: usize) -> Result<BatchExecuteResult>;
    // ...
}

struct BatchExecuteResult {
    physical_columns: LazyBatchColumnVec,  // 列式数据
    logical_rows: usize,                    // 这批里有效的行数(可能 < 列容量,因为过滤掉了)
    is_drained: { ... },                    // 是否扫完了
    warnings: ...,
}
```

注意一个微妙的设计:`logical_rows` 可能小于列的物理容量。为什么?因为 **Selection 算子是"原地标记"而不是"拷贝"**。TableScan 吐出 100 行,Selection 过滤后只剩 30 行——它不是把 30 行拷到新数组(那有内存分配开销),而是在原数组上标记"这 30 个下标是有效的",通过 `logical_rows` 和一个行索引数组告诉上层"只看这几行"。这是向量化执行引擎的招牌优化:**零拷贝过滤**。

> **不这么写会怎样**:如果用经典一行一拉 volcano,扫一亿行的聚合查询,光是虚函数调用开销就足以让它慢 10 倍以上。批量变体 + 列式 chunk + 原地过滤,是 TiKV Coprocessor 能撑住 OLAP 级别扫描的工程基础。这也是为什么现代分析型数据库(ClickHouse、Doris、DuckDB)全都用向量化执行——TiKV 虽是 TP 存储,但 Coprocessor 借鉴了这一套。

### 技巧二:parent_idx 扁平数组——protobuf 里怎么存一棵树

前面提到 `DagRequest.executors` 是个数组,每个 `Executor` 带 `parent_idx`。这个设计值得单独拆,因为它折射了"序列化树结构"的工程权衡。

朴素地序列化一棵树,会用 protobuf 的嵌套 message:

```protobuf
// 朴素方案(嵌套,没采用):
message Executor {
    oneof exec {
        TableScan tbl_scan = 1;
        Selection selection = 2;  // Selection 里嵌 child: Executor
        ...
    }
}
message Selection {
    Executor child = 1;   // 嵌套!树深 = protobuf 嵌套深度
    repeated Expr conditions = 2;
}
```

这个方案的问题:**树有多深,protobuf 就嵌多深**。而 protobuf 解码嵌套深的 message 时,递归调用栈深(前面 `endpoint.rs` 里那个 `recursion_limit` 就是防这个的);而且 DAG 的算子通常不止一个孩子(虽然 TiKV 下推的多是链式,但 TiDB 端的算子树是真正的 DAG,有共享子节点),嵌套结构表达不了 DAG。

tipb 的实际方案是**扁平数组 + parent_idx**:

```protobuf
// tipb 实际方案(扁平):
message DagRequest {
    repeated Executor executors = 1;   // 一个数组
    ...
}
message Executor {
    ExecType tp = 1;
    int64 parent_idx = 2;   // 指向 executors 数组里的父节点下标
    oneof exec { TableScan tbl_scan = 3; Selection sel = 4; ... }
}
```

每个 `Executor` 用 `parent_idx` 指明它的父节点是数组的第几个。这有几个好处:

1. **扁平,无递归**:protobuf 解码不需要深递归,栈安全;
2. **能表达 DAG**:多个算子可以共享同一个 `parent_idx`(同一个孩子),这是嵌套结构做不到的;
3. **省空间**:数组比嵌套 message 更紧凑(没有重复的字段标签、长度前缀)。

`build_executors` 就是按数组顺序消费、用 `parent_idx` 决定挂载点的(前面贴过它的骨架)。

> **不这么写会怎样**:用嵌套结构,一棵深算子树(比如 7 层:scan→sel→proj→aggr→sort→limit→proj)protobuf 解码就要递归 7 层,而且表达不了 DAG 共享子节点。扁平数组 + parent_idx 是用"一次线性扫描 + 下标查找"换掉递归,兼顾了序列化效率、栈安全和表达力——这是 protobuf 设计树/DAG 结构的通用技巧。

---

## 九、章末小结

### 回扣主线

本章讲的是 Coprocessor,它服务二分法的哪一面?**主要是事务层的"读"这一侧,也是可观测/计算下推的入口**。它把 TiDB 的计算搬到 TiKV,本质是"用 TiKV 的 CPU 换网络带宽"——让"扫千万行 + 过滤 + 聚合"在网络上传的只是结果。它复用事务读的 MVCC 快照,所以读到的数据是事务一致的;它跑在 read pool 上,和事务读共享调度基础设施。

从全书的旅程看,Coprocessor 是"读路径"的一个特殊变体——普通读(`get`/`scan`)是把数据原样返回,Coprocessor 读是把数据"加工后再返回"。它站在事务层和复制层的交界:用事务层的 MVCC 快照,在复制层(RocksDB)的数据上执行计算。

### 五个为什么

1. **为什么要把计算下推?**——不下推,几千万行的原始数据要涌向 TiDB,网络是不可逾越的瓶颈,且 99% 传过去的数据会被丢弃。下推把"大→小"的变换放在数据旁边,只传小结果。
2. **为什么 TiKV 不跑 SQL 而跑 DAG?**——SQL 解析优化是全局活,该 TiDB 干;TiKV 没有优化器、不该重复劳动。TiDB 把 SQL 编译成算子树序列化成 tipb 发给 TiKV,TiKV 只解码执行,职责分离。
3. **为什么用 volcano 批量模型?**——一行一拉的虚函数调用开销在扫上亿行时是灾难。批量变体把调用摊薄到每批一次,列式 chunk 对 cache 友好,原地过滤零拷贝。
4. **为什么 Coprocessor 复用事务读的 MVCC 快照?**——它要在事务里执行查询,必须读到一致快照。复用 `SnapshotStore` 保证隔离级别(RC/RR),不必另造一套读路径。
5. **为什么有 read pool 隔离、semaphore、memory_quota?**——Coprocessor 是 OLAP 负载(大扫描),TiKV 主要服务 OLTP(低延迟)。没有隔离,一个慢分析查询会拖卡事务读。配额是 OLAP/OLTP 共存的护栏。

### 想继续深入往哪钻

- **想看 gRPC 层怎么定义 `coprocessor` RPC**:读 kvproto(protobuf 定义),承接《gRPC》那本讲的 service/stream 实现。
- **想看算子实现细节**:读 `components/tidb_query_executors/src/{table_scan,selection,fast_hash_aggr,limit,top_n}_executor.rs`。
- **想看聚合函数实现**:读 `components/tidb_query_aggr/src/impl_*.rs`(sum/count/avg/max_min/variance 各一个文件)。
- **想看表达式求值**:读 `components/tidb_query_expr/src/`(列引用、常量、比较、内置函数的分发)。
- **想看 Coprocessor V2 插件**:读 `src/coprocessor_v2/{endpoint,plugin_registry,raw_storage_impl}.rs`,理解演进方向。

### 引出下一章

Coprocessor 解决的是"读路径的计算下推"。但 MVCC 持续堆积老版本(P3-10 讲过,每次写都新增一个版本,只在 write CF 留提交记录),这些老版本越攒越多,不清掉会撑爆磁盘、拖慢 scan。下一章 P6-20,我们讲 **GC 与 flashback:MVCC 老版本回收**——TiKV 怎么定一个 safe point 把它之前的版本清掉,清的活儿怎么巧妙地交给 RocksDB 的 compaction filter 顺手做,以及新特性 flashback 怎么闪回到某个历史版本。

> **下一章**:[P6-20 · GC 与 flashback:MVCC 老版本回收](P6-20-GC与flashback-MVCC老版本回收.md)
