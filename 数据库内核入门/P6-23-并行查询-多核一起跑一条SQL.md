# 第 23 章 · 并行查询:多核一起跑一条 SQL

> **前置**:你需要先读过 [第 5 章《执行器:把计划跑出来》](P1-05-执行器-把计划跑出来.md)——那里的火山模型(`ExecProcNode`、上层拉下层吐)是本章的全部地基;并行查询不是另起炉灶,而是把火山模型**切成两半**:下面交给多个进程分头跑、上面由一个节点收拢。再读 [第 4 章《查询优化器》](P1-04-查询优化器-怎么决定走哪条路最省.md)——并行要不要开、开几个 worker,是优化器在 costsize.c 里**比代价**比出来的,正是那章代价模型的一个应用。

> **核心问题**:一条 SQL,凭什么能用上多个 CPU 核一起跑?为什么 PostgreSQL **默认就不开并行**——同样的查询,小表上并行反而比串行还慢?优化器怎么判断"这次并行划算不划算",又是怎么把一个单进程的计划树改造成"几个进程各跑一份 + 一个节点合并"的形态的?哪些东西天生不能并行(写操作、临时表、用了某些函数的查询),为什么?

> **读完本章你会明白**:① 并行查询的全貌——一个 leader 进程 + N 个 worker 进程,worker 各跑一份 **partial plan(部分计划)**,通过**共享内存队列(shm_mq)**把 tuple 一个个传回,leader 用 **Gather / GatherMerge** 节点把它们拼成最终结果;② 为什么 PG 默认不并行,以及"并行有固定 overhead(启动进程、建共享内存、协调)、对 OLTP 小查询是纯亏"这一关键事实;③ 优化器怎么在 costsize.c 里给并行计划加 `parallel_setup_cost` 和 `parallel_tuple_cost`,以及为什么并行计划**必须比串行更省才会被选**;④ 什么是"并行安全(parallel safe)",为什么有些函数只能 leader 跑、有些碰了就不能并行;⑤ 并行的硬限制:worker 数全局有上限、不是所有算子都支持、写操作(CRUD)不并行。

> **如果一读觉得太难**:先记三件事——① 并行 = 把计划树下段切成几份,leader 起 N 个 worker 各跑一份,worker 通过共享内存把结果送回,leader 用 Gather 节点合并;② 并行不是免费午餐,有"启动 worker + 建共享内存 + tuple 跨进程传递"的固定开销,**查询太小则并行纯亏**,所以 PG 默认只在查询代价足够大时才开;③ 用了"并行不安全"的函数(碰了共享状态/序列/不可重入的)的查询,优化器会直接放弃并行。其余细节配合源码第二遍再抠。

---

## 一、为什么需要并行:单核到顶了,但核越来越多

第 5 章结束时,执行器是这样的形态:一个 backend 进程,顺着 Plan 树自顶向下"拉",每个算子一行一行吐出结果。一棵树从头到尾,**只有一个进程在跑**。这种单进程执行对绝大多数 OLTP(事务型)查询完全够用——一条 `SELECT * FROM users WHERE id = 42` 碰几十行就完事,毫秒级返回。

但 OLAP(分析型)是另一个世界:

```sql
-- 典型 OLAP:一张几亿行的大表,做聚合
SELECT region, sum(amount)
FROM orders
GROUP BY region;
```

这种查询要**把整张几亿行的表过一遍**。回到第 4 章的代价模型:SeqScan 的 CPU 代价是 `cpu_tuple_cost × tuples`,一亿行就是一亿次行处理,哪怕每次只要几个纳秒,单核也得跑好几秒甚至更久。可问题在于——

> **背景现实**:2005 年前后,单核 CPU 主频基本到顶了(功耗墙、散热墙)。从那以后,CPU 厂商给的不再是"更快的核",而是"更多的核"。今天一台普通服务器动辄几十个核。**单进程执行把 32 核机器上的 31 个核晾在一边**,只让一个核埋头苦干——这是巨大的浪费。

所以并行查询的根本动机很直白:**大查询里,如果能让多个核同时干活,把"扫表 + 算聚合"的活儿拆成几份并行跑,就能把几秒的查询压到几百毫秒**。这是 OLAP 场景下"让数据**快**"那一侧(回扣全书二分法)的一个直接杠杆。

但请注意这里的前提:**大查询**。一条只碰几十行的小查询,根本不值得并行——下面整个第二节就在讲为什么。

---

## 二、为什么不默认并行:overhead 是真的,小查询并行纯亏

这一节是本章最关键的一个"为什么",也是 PG 与一些"默认并行"数据库的最大差别。

### 并行有固定的、不可省的 overhead

把"一个进程跑计划"变成"多个进程一起跑",要付出三类**固定开销(overhead)**,无论查询多大都得付:

1. **启动 worker 进程**。PG 的并行 worker 是真实的操作系统进程(回扣第 2 章:PG 是"每连接一进程"的进程模型,worker 也一样,不是线程)。每开一个 worker,要 `fork`(实际走 `RegisterDynamicBackgroundWorker`)+ 初始化一个完整的 backend:连共享内存、装快照、设信号、复制 GUC 设置……这套动作的代价是**毫秒级**的固定开销。

2. **建共享内存**。worker 和 leader 不能靠函数返回值传数据(它们是不同进程,内存不互通)。PG 的办法是在**动态共享内存(DSM,Dynamic Shared Memory)**里开一片区域,worker 把算出来的 tuple 写进去,leader 从里面读。这片共享内存要预先规划大小、要建一个**目录(toc)**记录各种结构在里面的偏移——这也是固定开销。

3. **tuple 跨进程传递**。worker 每算出一行,要走**共享内存队列(shm_mq)**序列化、写队列、通知 leader、leader 反序列化读出来。这一来一回比"同进程里一次函数调用返回一个 tuple"贵得多。

### 不这样会怎样:对 OLTP 小查询,并行是纯亏

> **不这样会怎样**:假设 PG 像"默认并行"的数据库那样,对**任何**查询都试图并行。考虑一条 OLTP 点查 `SELECT name FROM users WHERE id = 42`——串行执行的话,优化器选个索引扫描,1 毫秒返回。可如果硬上并行:启动 worker(几毫秒)+ 建共享内存(几毫秒)+ 索引扫描就一行结果还要跨进程传一次(微秒级,但有调度开销)——**总开销十几毫秒,比串行慢一个数量级**。更要命的是,OLTP 系统里这种小查询**每秒几万次**,每次都付一遍并行 overhead,数据库会被自己拖垮。

而且 worker 不是免费的——它们占 `max_worker_processes` 的名额,占内存。一台机器的核是有限的,如果一个小查询就抢 4 个 worker,真正需要并行的大查询反而抢不到。

### 所以这样设计:PG 默认不并行,只在"值得"时才开

这就是 PG 的设计哲学:**并行是一个可选项,不是一个默认项**。是否并行,**由优化器在每条查询上单独决策**——它会估算"并行后的代价"和"串行的代价",**只有并行更省才选并行**。代价估算里,前面那三类 overhead 都被显式地建模成两个 GUC:

| 常数 | 默认值 | 含义 |
|------|--------|------|
| `parallel_setup_cost` | 1000.0 | 一次并行的固定启动代价(开 worker、建共享内存等) |
| `parallel_tuple_cost` | 0.1 | 每个 tuple 跨进程传递的额外代价 |

这两个值定义在 `cost.h`,和第 4 章那五个代价常数放一起:

> [src/include/optimizer/cost.h:24-30](../postgresql-17.0/src/include/optimizer/cost.h#L24-L30)

```c
#define DEFAULT_SEQ_PAGE_COST  1.0
#define DEFAULT_RANDOM_PAGE_COST  4.0
#define DEFAULT_CPU_TUPLE_COST	0.01
#define DEFAULT_CPU_INDEX_TUPLE_COST 0.005
#define DEFAULT_CPU_OPERATOR_COST  0.0025
#define DEFAULT_PARALLEL_TUPLE_COST 0.1
#define DEFAULT_PARALLEL_SETUP_COST  1000.0
```

注意 `parallel_setup_cost = 1000.0`——对照第 4 章 `seq_page_cost = 1.0`(一次顺序读页),1000 这个数字意味着:**优化器认为,开一次并行的固定开销,约等于顺序读 1000 个页(8MB)**。所以一张表如果只有几十页(几百 KB),哪怕全部分给 worker 扫,扫表省下的代价远远填不平 1000 的启动代价——优化器算出来并行总代价比串行高,**就不会选并行**。只有当表大到扫一遍的代价能盖过 1000,并行才进入候选。

> **一句话**:PG 不是"能并行就并行",而是"划算才并行"。这个"划算"是用代价模型量化的,而代价模型里显式地为并行的 overhead 留了位置。这种"保守"恰恰是 PG 在 OLTP/OLAP 混合负载下稳健的原因——小查询走单进程毫秒返回,大查询自动并行加速,各得其所。

---

## 三、怎么并行:leader + worker + 共享内存队列 + Gather

现在看并行查询的**具体形态**。一旦优化器决定并行,它会把原计划树改造成下面这种结构(简化示意):

```text
                   Gather  (或 GatherMerge)
                  /        \
            ┌────┘          (leader 也可能参与,见下)
            │
        partial plan   ← 这一段会被 worker 各自复制一份跑
       (并行 SeqScan
        / 并行 Hash 等)

数据流:
  worker 1 ─┐
  worker 2 ─┼──shm_mq──▶ Gather ──▶ 上层(leader 跑)
  worker N ─┘             合并
```

核心角色有三个:

1. **leader 进程**:就是发起这条 SQL 的那个 backend。它负责启动 worker、收集 worker 的结果、跑 Gather 节点之上的部分(以及可能自己也算一份 partial plan,后面讲)。
2. **worker 进程**:被 leader 动态启动的并行工作进程(`parallel worker`),每个 worker 跑一份**完全相同**的 partial plan——但通过"每个 worker 只扫表的特定块"等机制,让它们各干各的活儿,不重复劳动。
3. **共享内存队列 shm_mq**:worker 和 leader 之间的数据通道,本质是一片环形缓冲区(ring buffer)放在 DSM 里,带同步原语。

下面拆开看。

### worker 怎么避免"扫同一块":parallel-aware 算子

如果每个 worker 都把表从头扫一遍,那并行毫无意义——N 个 worker 还是 O(N×n) 的总工作量,只是把内存压力放大 N 倍。所以**并行扫描必须是"parallel-aware"(并行感知)的**:每个 worker 知道"我该扫哪些块,别人扫哪些块"。

以并行顺序扫描(parallel SeqScan)为例,worker 们共享一个**块号游标**放在共享内存里,每个 worker 要取下一块时,原子地把游标加一——拿到的块号就是它独占负责的那一块。这样 N 个 worker 就把表的块**几乎均匀地瓜分**了,不会有重叠。第 5 章讲 SeqScan 时贴过的 `SeqNext` 里就有这么一句:

> [src/backend/executor/nodeSeqscan.c:L49-L83](../postgresql-17.0/src/backend/executor/nodeSeqscan.c#L49-L83)

```c
static TupleTableSlot *
SeqNext(SeqScanState *node)
{
	...
	scandesc = node->ss.ss_currentScanDesc;
	...
	if (scandesc == NULL)
	{
		/*
		 * We reach here if the scan is not parallel, or if we're serially
		 * executing a scan that was planned to be parallel.
		 */
		scandesc = table_beginscan(node->ss.ss_currentRelation,
								   estate->es_snapshot,
								   0, NULL);
		node->ss.ss_currentScanDesc = scandesc;
	}
	...
}
```

注释那句 "if the scan is not parallel, or if we're serially executing a scan that was planned to be parallel" 透露了一个关键设计:**同一份 SeqScan 代码,既能在串行里跑,也能在并行 worker 里跑**——区别在于 `table_beginscan` 拿到的扫描描述符不同(并行时是个能协调块分配的版本)。这就是火山模型的威力:并行没有另起一套执行框架,而是**复用**了第 5 章的全部算子,只是给"并行"的算子多挂了一份共享状态。

类似地,并行 Hash 连接是多个 worker **共建一张**哈希表(每个 worker 把自己扫到的内表行插进去,然后又一起探测),这也是 parallel-aware 的体现。能这么干,是因为这些算子的逻辑天然是"对每一行独立处理"——把行分给不同进程不会出错。

### 数据怎么回来:共享内存队列 shm_mq

worker 算出的 tuple,要传回 leader。PG 用的是一个叫 **shm_mq(shared message queue)**的机制:一片放在 DSM 里的环形缓冲区,worker 往里写、leader 从里读,带进程闩(latch)做唤醒。

写入端 `shm_mq_send`:

> [src/backend/storage/ipc/shm_mq.c:L329-L338](../postgresql-17.0/src/backend/storage/ipc/shm_mq.c#L329-L338)

```c
shm_mq_result
shm_mq_send(shm_mq_handle *mqh, Size nbytes, const void *data, bool nowait,
			bool force_flush)
{
	shm_mq_iovec iov;

	iov.data = data;
	iov.len = nbytes;

	return shm_mq_sendv(mqh, &iov, 1, nowait, force_flush);
}
```

读取端 `shm_mq_receive`:

> [src/backend/storage/ipc/shm_mq.c:L571-L573](../postgresql-17.0/src/backend/storage/ipc/shm_mq.c#L571-L573)

```c
shm_mq_result
shm_mq_receive(shm_mq_handle *mqh, Size *nbytesp, void **datap, bool nowait)
{
```

这是个**阻塞式消息队列**:`nowait=false` 时,缓冲区满了 send 会去睡(等 leader 把数据读走再唤醒),缓冲区空了 receive 会去睡(等 worker 写了再唤醒)。leader 这边用 `TupleQueueReader` 包装一层,把"从某个 worker 的队列里读出的字节流"还原成 tuple。

> **不这样会怎样**:如果不走共享内存,而是让 worker 把结果写临时文件再让 leader 读——那引入磁盘 I/O,并行省的 CPU 全被 I/O 吃掉,白搭。如果让 worker 通过网络 socket 把数据发回——同样有内核态切换开销,且 PG 没有"内部 socket"这套基础设施。**共享内存是同机多进程之间最快的数据通道(零拷贝、纯内存)**,所以这是 PG 的不二之选。代价是它只能用于"同一台机器"——跨节点的分布式并行(MPP)是另一回事,PG 的并行查询严格是**单机多核**并行。

### leader 怎么收拢:Gather 与 GatherMerge

leader 那一侧,负责"把 N 个 worker 的输出合并成一个流"的算子叫 **Gather**(无序合并)或 **GatherMerge**(保序合并)。它是并行计划和上层串行计划之间的**分界线**:它的子树是并行的(worker 各跑一份),它本身及以上是 leader 单进程跑的。

Gather 节点的执行函数 `ExecGather`,核心逻辑是**第一次执行时启动 worker,之后反复从各 worker 的队列里轮询取 tuple**:

> [src/backend/executor/nodeGather.c:L137-L209](../postgresql-17.0/src/backend/executor/nodeGather.c#L137-L209)

```c
static TupleTableSlot *
ExecGather(PlanState *pstate)
{
	GatherState *node = castNode(GatherState, pstate);
	TupleTableSlot *slot;
	ExprContext *econtext;

	CHECK_FOR_INTERRUPTS();

	/*
	 * Initialize the parallel context and workers on first execution. We do
	 * this on first execution rather than during node initialization, as it
	 * needs to allocate a large dynamic segment, so it is better to do it
	 * only if it is really needed.
	 */
	if (!node->initialized)
	{
		EState	   *estate = node->ps.state;
		Gather	   *gather = (Gather *) node->ps.plan;

		/*
		 * Sometimes we might have to run without parallelism; but if parallel
		 * mode is active then we can try to fire up some workers.
		 */
		if (gather->num_workers > 0 && estate->es_use_parallel_mode)
		{
			ParallelContext *pcxt;

			/* Initialize, or re-initialize, shared state needed by workers. */
			if (!node->pei)
				node->pei = ExecInitParallelPlan(outerPlanState(node),
												 estate,
												 gather->initParam,
												 gather->num_workers,
												 node->tuples_needed);
			else
				ExecParallelReinitialize(outerPlanState(node),
										 node->pei,
										 gather->initParam);

			/*
			 * Register backend workers. We might not get as many as we
			 * requested, or indeed any at all.
			 */
			pcxt = node->pei->pcxt;
			LaunchParallelWorkers(pcxt);
			/* We save # workers launched for the benefit of EXPLAIN */
			node->nworkers_launched = pcxt->nworkers_launched;

			/* Set up tuple queue readers to read the results. */
			if (pcxt->nworkers_launched > 0)
			{
				ExecParallelCreateReaders(node->pei);
				...
				node->nreaders = pcxt->nworkers_launched;
				...
			}
			else
			{
				/* No workers?	Then never mind. */
				node->nreaders = 0;
				node->reader = NULL;
			}
			node->nextreader = 0;
		}

		/* Run plan locally if no workers or enabled and not single-copy. */
		node->need_to_scan_locally = (node->nreaders == 0)
			|| (!gather->single_copy && parallel_leader_participation);
		node->initialized = true;
	}
	...
	slot = gather_getnext(node);
	...
}
```

读这段要抓住四个点:

- **延迟启动(lazy init)**。注意 `if (!node->initialized)`——worker **不在 Plan 树建好时就启动,而是在 Gather 节点第一次被 `ExecProcNode` 拉到时才启动**。注释点明原因:建 DSM 是大开销,得确保"真的要并行"才做。这是 PG 把 overhead 控制到极致的一个细节——如果上层有 `LIMIT` 之类短路,Gather 可能根本不被调用,worker 就一个都不起。
- **`ExecInitParallelPlan`** 准备共享内存:它把 partial plan 序列化进 DSM、规划好 worker 要用的所有共享状态。下一节会展开。
- **`LaunchParallelWorkers(pcxt)`** 真正去注册后台 worker 进程。注意紧接着的注释 "We might not get as many as we requested, or indeed any at all"——**worker 启动可能失败**(达到 `max_worker_processes` 上限),Gather 必须能容忍"实际拿到的 worker 比 num_workers 少甚至为零"。
- **leader 也参与(fallback)**。看末尾 `need_to_scan_locally`:如果压根没启动到 worker,Gather 会让 leader 自己跑一份 partial plan——优雅降级,查询照常完成,只是不并行。这个由 GUC `parallel_leader_participation`(默认 on)控制,默认情况下即使有 worker,leader 也帮忙跑一份,把第 0 号 worker 的活儿省下来。

实际取 tuple 的循环在 `gather_getnext`:

> [src/backend/executor/nodeGather.c:L256-L298](../postgresql-17.0/src/backend/executor/nodeGather.c#L256-L298)

```c
gather_getnext(GatherState *gatherstate)
{
	PlanState  *outerPlan = outerPlanState(gatherstate);
	TupleTableSlot *outerTupleSlot;
	TupleTableSlot *fslot = gatherstate->funnel_slot;
	MinimalTuple tup;

	while (gatherstate->nreaders > 0 || gatherstate->need_to_scan_locally)
	{
		CHECK_FOR_INTERRUPTS();

		if (gatherstate->nreaders > 0)
		{
			tup = gather_readnext(gatherstate);

			if (HeapTupleIsValid(tup))
			{
				ExecStoreMinimalTuple(tup,	/* tuple to store */
									  fslot,	/* slot to store the tuple */
									  false);	/* don't pfree tuple  */
				return fslot;
			}
		}

		if (gatherstate->need_to_scan_locally)
		{
			EState	   *estate = gatherstate->ps.state;

			/* Install our DSA area while executing the plan. */
			estate->es_query_dsa =
				gatherstate->pei ? gatherstate->pei->area : NULL;
			outerTupleSlot = ExecProcNode(outerPlan);
			estate->es_query_dsa = NULL;

			if (!TupIsNull(outerTupleSlot))
				return outerTupleSlot;

			gatherstate->need_to_scan_locally = false;
		}
	}

	return ExecClearTuple(fslot);
}
```

`gather_readnext` 轮询各个 reader(每个 reader 绑定一个 worker 的 shm_mq),非阻塞地试读一个 tuple;读到了就返回,读不到(队列暂时空)就跳到下一个 reader 试。这是**轮询(round-robin)**式合并:**worker 谁先算出来谁的结果就先被吐出去**,tuple 在 Gather 输出里**没有特定顺序**。

如果查询需要保序(比如 `ORDER BY` 还在上层,Gather 下面的并行 SeqScan 是按主键有序的),就要换 **GatherMerge**——它用一个小顶堆(堆顶是当前 N 个 worker 输出里最小的那个),保证合并出来的流是全局有序的。`ExecGatherMerge` 的结构和 ExecGather 几乎一样(启动 worker、建 reader),区别只在取 tuple 的合并方式:

> [src/backend/executor/nodeGatherMerge.c:L183-L249](../postgresql-17.0/src/backend/executor/nodeGatherMerge.c#L183-L249)

```c
static TupleTableSlot *
ExecGatherMerge(PlanState *pstate)
{
	GatherMergeState *node = castNode(GatherMergeState, pstate);
	...
	if (!node->initialized)
	{
		...
		if (gm->num_workers > 0 && estate->es_use_parallel_mode)
		{
			...
			node->pei = ExecInitParallelPlan(outerPlanState(node), ...);
			pcxt = node->pei->pcxt;
			LaunchParallelWorkers(pcxt);
			...
		}

		/* allow leader to participate if enabled or no choice */
		if (parallel_leader_participation || node->nreaders == 0)
			node->need_to_scan_locally = true;
		node->initialized = true;
	}
	...
	slot = gather_merge_getnext(node);
	...
}
```

实际合并用堆的代价,在优化器 `cost_gather_merge` 里有建模:

> [src/backend/optimizer/path/costsize.c:L463-L528](../postgresql-17.0/src/backend/optimizer/path/costsize.c#L463-L528)

```c
/*
 * cost_gather_merge
 *	  Determines and returns the cost of gather merge path.
 *
 * GatherMerge merges several pre-sorted input streams, using a heap that at
 * any given instant holds the next tuple from each stream. If there are N
 * streams, we need about N*log2(N) tuple comparisons to construct the heap at
 * startup, and then for each output tuple, about log2(N) comparisons to
 * replace the top heap entry with the next tuple from the same stream.
 */
void
cost_gather_merge(...)
{
	...
	if (!enable_gathermerge)
		startup_cost += disable_cost;

	/*
	 * Add one to the number of workers to account for the leader.  This might
	 * be overgenerous since the leader will do less work than other workers
	 * in typical cases, but we'll go with it for now.
	 */
	Assert(path->num_workers > 0);
	N = (double) path->num_workers + 1;
	logN = LOG2(N);
	...

	/* Assumed cost per tuple comparison */
	comparison_cost = 2.0 * cpu_operator_cost;

	/* Heap creation cost */
	startup_cost += comparison_cost * N * logN;

	/* Per-tuple heap maintenance cost */
	run_cost += path->path.rows * comparison_cost * logN;

	/* small cost for heap management, like cost_merge_append */
	run_cost += cpu_operator_cost * path->path.rows;

	/*
	 * Parallel setup and communication cost.  Since Gather Merge, unlike
	 * Gather, requires us to block until a tuple is available from every
	 * worker, we bump the IPC cost up a little bit as compared with Gather.
	 * For lack of a better idea, charge an extra 5%.
	 */
	startup_cost += parallel_setup_cost;
	run_cost += parallel_tuple_cost * path->path.rows * 1.05;

	path->path.startup_cost = startup_cost + input_startup_cost;
	path->path.total_cost = (startup_cost + run_cost + input_total_cost);
}
```

注意末尾那段注释:**GatherMerge 比 Gather 贵一点点(多算 5%)**,因为它要"等所有 worker 都至少吐出一条才能建堆",启动更阻塞。所以如果上层不要求保序,优化器会用 Gather 而非 GatherMerge。

> **回扣第 5 章火山模型**:Gather 节点本身是个**完全符合火山协议**的算子——它向父节点暴露 `ExecProcNode` 接口,父节点"拉"它就返回一行。它内部的复杂性(管 worker、读 shm_mq)被封装在节点实现里,对外完全透明。这正是第 5 章讲的"火山模型接口统一、组合自由"在并行场景的兑现:**并行不是一个全新的执行框架,而是火山模型里多了一个叫 Gather 的算子**。你把第 5 章那棵 Plan 树里的某个节点换成 Gather,它就自动获得并行能力——这是 PG 并行设计最优雅的地方。

---

## 四、优化器怎么决定:并行划算不划算,开几个 worker

第二节讲了"PG 默认不并行,只在划算时才开"。这一节看"划算"具体怎么算。这其实是第 4 章代价模型的一个直接应用——所有逻辑都在 `costsize.c` 和 `allpaths.c` 里。

### 给并行的代价加两笔:cost_gather 的全貌

Gather 节点自己的代价函数 `cost_gather`,把第二节那两个并行常数(`parallel_setup_cost`、`parallel_tuple_cost`)显式加到代价里:

> [src/backend/optimizer/path/costsize.c:L435-L461](../postgresql-17.0/src/backend/optimizer/path/costsize.c#L435-L461)

```c
void
cost_gather(GatherPath *path, PlannerInfo *root,
			RelOptInfo *rel, ParamPathInfo *param_info,
			double *rows)
{
	Cost		startup_cost = 0;
	Cost		run_cost = 0;

	/* Mark the path with the correct row estimate */
	if (rows)
		path->path.rows = *rows;
	else if (param_info)
		path->path.rows = param_info->ppi_rows;
	else
		path->path.rows = rel->rows;

	startup_cost = path->subpath->startup_cost;

	run_cost = path->subpath->total_cost - path->subpath->startup_cost;

	/* Parallel setup and communication cost. */
	startup_cost += parallel_setup_cost;
	run_cost += parallel_tuple_cost * path->path.rows;

	path->path.startup_cost = startup_cost;
	path->path.total_cost = (startup_cost + run_cost);
}
```

读这段代码时注意三点:

- `startup_cost = path->subpath->startup_cost` + `parallel_setup_cost`——**并行的启动代价 = partial plan 自己的启动代价 + 1000**(那个固定 overhead)。这就是第二节的"小查询并行纯亏"在代码里的具象:任何并行计划的 startup 都比串行多 1000。
- `run_cost = (subpath total - subpath startup) + parallel_tuple_cost × rows`——并行计划的运行代价,等于 partial plan 自己的运行代价(注意!**这里没除以 worker 数**,因为 partial plan 的代价是"单个 worker 跑一份"的代价,而真正的好处来自下层算子的代价已经被 `compute_parallel_worker` 算成"分摊后"的了),再加上每个 tuple 跨进程的传递开销。
- 真正的并行收益体现在 **partial plan 子树的代价已经被并行化降低**了——比如 cost_seqscan 里就有这么一段(第 4 章贴过,这里再聚焦看):

> [src/backend/optimizer/path/costsize.c:L327-L347](../postgresql-17.0/src/backend/optimizer/path/costsize.c#L327-L347)

```c
	/* Adjust costing for parallelism, if used. */
	if (path->parallel_workers > 0)
	{
		double		parallel_divisor = get_parallel_divisor(path);

		/* The CPU cost is divided among all workers. */
		cpu_run_cost /= parallel_divisor;

		/*
		 * It may be possible to amortize some of the I/O cost, but probably
		 * not very much, because most operating systems already do aggressive
		 * prefetching.  For now, we assume that the disk run cost can't be
		 * amortized at all.
		 */

		/*
		 * In the case of a parallel plan, the row count needs to represent
		 * the number of tuples processed per worker.
		 */
		path->rows = clamp_row_est(path->rows / parallel_divisor);
	}
```

读注释能品出务实的工程判断:**CPU 代价除以 worker 数(活儿分摊了),但磁盘代价不除**——理由是"操作系统本来就在做激进预读,多个 worker 也榨不出更多顺序 I/O 的便宜"。这是个清醒的认知:并行省的是 CPU,不是磁盘 I/O(对顺序扫描而言)。所以一张大表的并行 SeqScan:CPU 代价砍掉一大块(÷ worker 数),磁盘代价不变——总代价比串行低,但低不到 N 倍。

### 决定 worker 数:compute_parallel_worker

worker 数不是越多越好。核有限、`max_worker_processes` 有限、worker 之间协调也有开销。PG 用一个启发式函数 `compute_parallel_worker` 决定一张表并行扫描时该用几个 worker:

> [src/backend/optimizer/path/allpaths.c:L4203-L4279](../postgresql-17.0/src/backend/optimizer/path/allpaths.c#L4203-L4279)

```c
compute_parallel_worker(RelOptInfo *rel, double heap_pages, double index_pages,
						int max_workers)
{
	int			parallel_workers = 0;

	/*
	 * If the user has set the parallel_workers reloption, use that; otherwise
	 * select a default number of workers.
	 */
	if (rel->rel_parallel_workers != -1)
		parallel_workers = rel->rel_parallel_workers;
	else
	{
		/*
		 * If the number of pages being scanned is insufficient to justify a
		 * parallel scan, just return zero ... unless it's an inheritance
		 * child. In that case, we want to generate a parallel path here
		 * anyway.  It might not be worthwhile just for this relation, but
		 * when combined with all of its inheritance siblings it may well pay
		 * off.
		 */
		if (rel->reloptkind == RELOPT_BASEREL &&
			((heap_pages >= 0 && heap_pages < min_parallel_table_scan_size) ||
			 (index_pages >= 0 && index_pages < min_parallel_index_scan_size)))
			return 0;

		if (heap_pages >= 0)
		{
			int			heap_parallel_threshold;
			int			heap_parallel_workers = 1;

			/*
			 * Select the number of workers based on the log of the size of
			 * the relation.  This probably needs to be a good deal more
			 * sophisticated, but we need something here for now.  Note that
			 * the upper limit of the min_parallel_table_scan_size GUC is
			 * chosen to prevent overflow here.
			 */
			heap_parallel_threshold = Max(min_parallel_table_scan_size, 1);
			while (heap_pages >= (BlockNumber) (heap_parallel_threshold * 3))
			{
				heap_parallel_workers++;
				heap_parallel_threshold *= 3;
				if (heap_parallel_threshold > INT_MAX / 3)
					break;		/* avoid overflow */
			}

			parallel_workers = heap_parallel_workers;
		}
		...
	}

	/* In no case use more than caller supplied maximum number of workers */
	parallel_workers = Min(parallel_workers, max_workers);

	return parallel_workers;
}
```

逻辑很直白:

- 用户在表上设了 `ALTER TABLE ... SET (parallel_workers = N)` 就用 N(用户主导)。
- 否则按**表大小对数增长**:表越大,worker 越多,但增长是 log 级的(`while (heap_pages >= heap_parallel_threshold * 3)`)。一个直观例子(默认 `min_parallel_table_scan_size = 8MB = 1000 页`):1000 页以下 → 0 个 worker(并行不划算);3000 页 → 1 个;9000 页 → 2 个;27000 页 → 3 个……表每变 3 倍大,worker +1。这个对数策略是工程上的折中——**worker 越多协调开销越大,所以不能线性增长**。
- 第一道关 `if (... heap_pages < min_parallel_table_scan_size) return 0`——**表太小,直接否决并行**。这就是 `min_parallel_table_scan_size`(默认 8MB)的作用:小于这个尺寸的表,扫一遍的代价填不平 `parallel_setup_cost`,优化器压根不生成并行路径。
- 最后 `Min(parallel_workers, max_workers)`——不能超过上层给的上限。这个上限就是另一个 GUC **`max_parallel_workers_per_gather`**(默认 2)。

### 三道闸门:三个 GUC 共同限制并行

PG 设了三个 GUC 把并行的"开闸"控制得很保守。它们各管一道关:

| GUC | 默认值 | 含义 |
|-----|--------|------|
| `max_worker_processes` | 8 | 整个 PG 实例**最多**能同时存在的后台 worker 总数(并行 worker、逻辑复制 worker、autovacuum worker 都从这里扣)。改它要重启。 |
| `max_parallel_workers` | 8 | 上面这个池子里,**专门留给并行查询**的名额。 |
| `max_parallel_workers_per_gather` | 2 | **单个 Gather 节点**最多能要几个 worker。这是"单查询并行度"的直接控制。 |

> [src/backend/utils/misc/guc_tables.c:L3409-L3429](../postgresql-17.0/src/backend/utils/misc/guc_tables.c#L3409-L3429)

```c
	{
		{"max_parallel_workers_per_gather", PGC_USERSET, RESOURCES_ASYNCHRONOUS,
			gettext_noop("Sets the maximum number of parallel processes per executor node."),
			NULL,
			GUC_EXPLAIN
		},
		&max_parallel_workers_per_gather,
		2, 0, MAX_PARALLEL_WORKER_LIMIT,
		NULL, NULL, NULL
	},

	{
		{"max_parallel_workers", PGC_USERSET, RESOURCES_ASYNCHRONOUS,
			gettext_noop("Sets the maximum number of parallel workers that can be active at one time."),
			NULL,
			GUC_EXPLAIN
		},
		&max_parallel_workers,
		8, 0, MAX_PARALLEL_WORKER_LIMIT,
		NULL, NULL, NULL
	},
```

`min_parallel_table_scan_size` 默认是 8MB,定义在同文件:

> [src/backend/utils/misc/guc_tables.c:L3510-L3519](../postgresql-17.0/src/backend/utils/misc/guc_tables.c#L3510-L3519)

```c
	{
		{"min_parallel_table_scan_size", PGC_USERSET, QUERY_TUNING_COST,
			gettext_noop("Sets the minimum amount of table data for a parallel scan."),
			gettext_noop("If the planner estimates that it will read a number of table pages too small to reach this limit, a parallel scan will not be considered."),
			GUC_UNIT_BLOCKS | GUC_EXPLAIN,
		},
		&min_parallel_table_scan_size,
		(8 * 1024 * 1024) / BLCKSZ, 0, INT_MAX / 3,
		NULL, NULL, NULL
	},
```

`(8 * 1024 * 1024) / BLCKSZ` = 8MB / 8KB(默认页大小)= 1024 页。三层 GUC 的意义在于:**即使一张表很大、优化器想开 10 个 worker,系统也只允许它开 2 个**(默认 `max_parallel_workers_per_gather = 2`)。这就把单查询的并行度死死限住,避免一条大查询把整个机器的核全占光、其它查询都饿死。**保守**是 PG 并行设计的总基调。

> **不这样会怎样**:如果没有任何闸门,一条大查询就能把 32 个核全占满——OLTP 流量进来全部卡死、其它分析查询也排不上队。把并行度限到默认 2,是"对一台机器上同时跑多个查询"这个共享场景的必要妥协。需要更高并行度的用户,可以调高 `max_parallel_workers_per_gather`(比如分析专用服务器调到 8 或 16),代价是单查询把核占满。

---

## 五、worker 怎么被启动、怎么找到自己的活:ParallelContext 全流程

第三节贴了 `ExecGather` 里的 `ExecInitParallelPlan` 和 `LaunchParallelWorkers` 调用,但跳过了它们背后那套机制。这一节我们把它拆开——这是把"一份 partial plan 在 N 个进程里复制起来跑"的核心。

### ExecInitParallelPlan:把计划搬进共享内存

`ExecInitParallelPlan` 是并行执行的准备工作:它要把 partial plan 序列化、把 worker 需要的所有状态(GUC、快照、范围表、缓冲使用统计……)打包进 DSM 的一段连续内存,并用一个**目录(toc,table of contents)**记录各部分的偏移:

> [src/backend/executor/execParallel.c:L586-L629](../postgresql-17.0/src/backend/executor/execParallel.c#L586-L629)

```c
/*
 * Sets up the required infrastructure for backend workers to perform
 * execution and return results to the main backend.
 */
ParallelExecutorInfo *
ExecInitParallelPlan(PlanState *planstate, EState *estate,
					 Bitmapset *sendParams, int nworkers,
					 int64 tuples_needed)
{
	ParallelExecutorInfo *pei;
	ParallelContext *pcxt;
	ExecParallelEstimateContext e;
	ExecParallelInitializeDSMContext d;
	FixedParallelExecutorState *fpes;
	char	   *pstmt_data;
	char	   *pstmt_space;
	char	   *paramlistinfo_space;
	BufferUsage *bufusage_space;
	WalUsage   *walusage_space;
	SharedExecutorInstrumentation *instrumentation = NULL;
	SharedJitInstrumentation *jit_instrumentation = NULL;
	int			pstmt_len;
	int		paramlistinfo_len;
	int			instrumentation_len = 0;
	int			jit_instrumentation_len = 0;
	int			instrument_offset = 0;
	Size		dsa_minsize = dsa_minimum_size();
	char	   *query_string;
	int			query_len;

	/*
	 * Force any initplan outputs that we're going to pass to workers to be
	 * evaluated, if they weren't already.
	 ...
	 */
	ExecSetParamPlanMulti(sendParams, GetPerTupleExprContext(estate));

	/* Allocate object for return value. */
	pei = palloc0(sizeof(ParallelExecutorInfo));
	pei->finished = false;
	pei->planstate = planstate;
	...
```

注意它要为 `BufferUsage`、`WalUsage`、`SharedExecutorInstrumentation` 这些都预留空间——因为 worker 跑完后要把"我读了多少 buffer、写了多少 WAL、各算子耗时多少"汇报回 leader,供 `EXPLAIN ANALYZE` 汇总。也就是说,**DSM 里不只是放计划,还放了一整套协调 worker 和汇总统计的基础设施**。

`ExecInitParallelPlan` 干完后返回一个 `ParallelExecutorInfo`(pei),里面带一个 `ParallelContext`(`pcxt`)——后者是更底层的、跨模块通用的并行上下文,定义在 `parallel.c`。pei 是执行器层对 pcxt 的封装。

### CreateParallelContext + LaunchParallelWorkers:真的去 fork worker

底层 `ParallelContext` 的创建是 `CreateParallelContext`:

> [src/backend/access/transam/parallel.c:L166-L168](../postgresql-17.0/src/backend/access/transam/parallel.c#L166-L168)

```c
ParallelContext *
CreateParallelContext(const char *library_name, const char *function_name,
```

它接受一个 `library_name`/`function_name`——也就是 worker 启动后该调用的入口函数(对并行查询来说,就是 `ParallelWorkerMain`)。这种"通过名字注册入口"的设计,让并行框架不只服务查询执行,逻辑复制、并行工具都能用同一套基础设施。

真正去 fork 进程的是 `LaunchParallelWorkers`:

> [src/backend/access/transam/parallel.c:L552-L606](../postgresql-17.0/src/backend/access/transam/parallel.c#L552-L606)

```c
LaunchParallelWorkers(ParallelContext *pcxt)
{
	MemoryContext oldcontext;
	BackgroundWorker worker;
	int			i;
	bool		any_registrations_failed = false;

	/* Skip this if we have no workers. */
	if (pcxt->nworkers == 0 || pcxt->nworkers_to_launch == 0)
		return;

	/* We need to be a lock group leader. */
	BecomeLockGroupLeader();

	/* If we do have workers, we'd better have a DSM segment. */
	Assert(pcxt->seg != NULL);

	/* We might be running in a short-lived memory context. */
	oldcontext = MemoryContextSwitchTo(TopTransactionContext);

	/* Configure a worker. */
	memset(&worker, 0, sizeof(worker));
	snprintf(worker.bgw_name, BGW_MAXLEN, "parallel worker for PID %d",
			 MyProcPid);
	snprintf(worker.bgw_type, BGW_MAXLEN, "parallel worker");
	worker.bgw_flags =
		BGWORKER_SHMEM_ACCESS | BGWORKER_BACKEND_DATABASE_CONNECTION
		| BGWORKER_CLASS_PARALLEL;
	worker.bgw_start_time = BgWorkerStart_ConsistentState;
	worker.bgw_restart_time = BGW_NEVER_RESTART;
	sprintf(worker.bgw_library_name, "postgres");
	sprintf(worker.bgw_function_name, "ParallelWorkerMain");
	worker.bgw_main_arg = UInt32GetDatum(dsm_segment_handle(pcxt->seg));
	worker.bgw_notify_pid = MyProcPid;

	/*
	 * Start workers.
	 *
	 * The caller must be able to tolerate ending up with fewer workers than
	 * expected, so there is no need to throw an error here if registration
	 * fails.  It wouldn't help much anyway, because registering the worker in
	 * no way guarantees that it will start up and initialize successfully.
	 */
	for (i = 0; i < pcxt->nworkers_to_launch; ++i)
	{
		memcpy(worker.bgw_extra, &i, sizeof(int));
		if (!any_registrations_failed &&
			RegisterDynamicBackgroundWorker(&worker,
											&pcxt->worker[i].bgwhandle))
		{
			shm_mq_set_handle(pcxt->worker[i].error_mqh,
							  pcxt->worker[i].bgwhandle);
			pcxt->nworkers_launched++;
		}
		else
		{
			/*
			 * If we weren't able to register the worker, then we've bumped up
			 * against the max_worker_processes limit, and future
			 * registrations will probably fail too, so arrange to skip them.
			 ...
```

读这段要注意几个工程细节:

- worker 是通过 **`RegisterDynamicBackgroundWorker`** 注册的——这是 PG 的"动态后台工作进程"框架(`BackgroundWorker` 是注册时填的"工人简历")。注册成功不等于启动成功,启动成功不等于初始化成功——所以注释说 "no way guarantees that it will start up and initialize successfully"。
- **worker 编号 `i` 通过 `worker.bgw_extra` 传给 worker 进程**——这样每个 worker 启动后知道自己"是第几号",从而能算出"我该读哪个 shm_mq、该扫哪些块"。
- **`bgw_function_name = "ParallelWorkerMain"`** 写死了——所有并行查询 worker 都从同一个入口进来。
- **遇到 `max_worker_processes` 上限就放弃**(`any_registrations_failed`)——一旦一次注册失败,后续都跳过(因为大概率也会失败)。最终 `pcxt->nworkers_launched` 可能小于 `nworkers_to_launch`,执行器要按实际启动的 worker 数干活(回扣第三节 Gather 里 `need_to_scan_locally` 的优雅降级)。

### ParallelWorkerMain:worker 进程的入口

worker 进程启动后,它的 main 函数就是 `ParallelWorkerMain`:

> [src/backend/access/transam/parallel.c:L1271-L1299](../postgresql-17.0/src/backend/access/transam/parallel.c#L1271-L1299)

```c
ParallelWorkerMain(Datum main_arg)
{
	dsm_segment *seg;
	shm_toc    *toc;
	FixedParallelState *fps;
	char	   *error_queue_space;
	shm_mq	   *mq;
	shm_mq_handle *mqh;
	char	   *libraryspace;
	char	   *entrypointstate;
	char	   *library_name;
	char	   *function_name;
	parallel_worker_main_type entrypt;
	char	   *gucspace;
	char	   *combocidspace;
	char	   *tsnapspace;
	char	   *asnapspace;
	char	   *tstatespace;
	char	   *pendingsyncsspace;
	char	   *reindexspace;
	char	   *relmapperspace;
	char	   *uncommittedenumsspace;
	char	   *clientconninfospace;
	char	   *session_dsm_handle_space;
	Snapshot	tsnapshot;
	Snapshot	asnapshot;

	/* Set flag to indicate that we're initializing a parallel worker. */
	InitializingParallelWorker = true;

	/* Establish signal handlers. */
	pqsignal(SIGTERM, die);
	BackgroundWorkerUnblockSignals();
	...
```

看那一长串局部变量就能感受到 worker 要恢复多少状态:`gucspace`(GUC 设置)、`tsnapspace`/`asnapspace`(事务快照和活跃快照——回扣 P4 MVCC,worker 必须和 leader 看到同一份数据视图)、`combocidspace`(组合 cid,用于 MVCC)、`relmapperspace`(relmapper 映射)、`tstatespace`(事务状态)……**worker 不是一张白纸,它要把自己"还原"成 leader 进程在执行那一刻的样子**,才能保证它跑出来的 partial plan 结果和 leader 自己跑的一致。这些状态的迁移就是 `ExecInitParallelPlan` 那一侧打包、`ParallelWorkerMain` 这一侧解包的对应过程。

恢复完状态后,worker 调用 `entrypt`(也就是执行器注册的并行执行入口),开始按 partial plan 跑——和 leader 跑一份串行计划一样,只不过它把自己的结果通过 shm_mq 送回去,而不是送给上层算子。

> 这一段把"一条并行计划怎么从 leader 启动到 worker 各自干活再到 Gather 收拢"的完整链条钉死了。回扣第二节:**这套机制的固定开销(建 DSM、序列化计划、启动进程、恢复状态)就是 `parallel_setup_cost` 在建模的东西**,小查询付不起,所以默认不开。

---

## 六、并行安全:为什么有些查询不能并行

到这里你可能会问:既然并行这么好,为什么不所有查询都并行?除了第二节的"开销"理由之外,还有一个**正确性**理由:有些查询**根本不能**并行——强行并行会出错。这就是"并行安全(parallel safety)"问题。

### 三级标签:SAFE / RESTRICTED / UNSAFE

PG 给每个函数标了一个并行级别,记录在 `pg_proc.proparallel` 字段:

> [src/include/catalog/pg_proc.h:L77](../postgresql-17.0/src/include/catalog/pg_proc.h#L77)

```c
	char		proparallel BKI_DEFAULT(s);
```

三个取值(注释在同文件):

> [src/include/catalog/pg_proc.h:L169-L175](../postgresql-17.0/src/include/catalog/pg_proc.h#L169-L175)

```c
/*
 * Symbolic values for proparallel column: these indicate whether a function
 ...
 */
#define PROPARALLEL_SAFE		's' /* can run in worker or leader */
#define PROPARALLEL_RESTRICTED	'r' /* can run in parallel leader only */
#define PROPARALLEL_UNSAFE		'u' /* banned while in parallel mode */
```

逐个解释:

- **`PROPARALLEL_SAFE`('s')**:这个函数可以在 worker 里跑,也可以在 leader 里跑。绝大多数纯计算函数(`+`、`substr`、`lower`……)都是 SAFE——它们没有副作用、不依赖进程私有状态,N 个进程同时跑结果一样。
- **`PROPARALLEL_RESTRICTED`('r')**:只能在 **leader** 里跑,不能下推到 worker。典型例子:读了临时表的函数(临时表是 backend 私有的,worker 看不到)、用了游标的函数。这类函数只要放在 Gather **之上**(leader 跑的部分)就没问题;放在 Gather 之下(worker 要跑的部分)就不行。
- **`PROPARALLEL_UNSAFE`('u')**:**只要查询里有这个函数,整个查询都不能并行**——不管放在哪一层。为什么这么狠?因为这类函数碰了全局共享状态或不可重入资源:序列(`nextval`)、写操作、注册自定义 FDW、调用 `dblink` 跨库连接……多个 worker 同时跑会互相打架或破坏状态。

### max_parallel_hazard:扫一遍查询,找出最危险的那个

优化器怎么知道一条查询整体是否可并行?它要遍历整个查询树,找出其中**最不安全**的那个函数,以此决定整条查询的并行级别。这个函数叫 `max_parallel_hazard`:

> [src/backend/optimizer/util/clauses.c:L722-L743](../postgresql-17.0/src/backend/optimizer/util/clauses.c#L722-L743)

```c
/*
 * max_parallel_hazard
 *		Find the worst parallel-hazard level in the given query
 *
 * Returns the worst function hazard property (the earliest in this list:
 * PROPARALLEL_UNSAFE, PROPARALLEL_RESTRICTED, PROPARALLEL_SAFE) that can
 * be found in the given parsetree.  We use this to find out whether the query
 * can be parallelized at all.  The caller will also save the result in
 * PlannerGlobal so as to short-circuit checks of portions of the querytree
 * later, in the common case where everything is SAFE.
 */
char
max_parallel_hazard(Query *parse)
{
	max_parallel_hazard_context context;

	context.max_hazard = PROPARALLEL_SAFE;
	context.max_interesting = PROPARALLEL_UNSAFE;
	context.safe_param_ids = NIL;
	(void) max_parallel_hazard_walker((Node *) parse, &context);
	return context.max_hazard;
}
```

注释点出关键设计:**取"最坏"的级别作为整条查询的级别**(UNSAFE > RESTRICTED > SAFE),而且结果缓存在 `PlannerGlobal` 里——因为绝大多数查询整条都是 SAFE,扫一遍后,后续优化时碰到具体表达式就不必再扫(短路)。

核心比较逻辑在 `max_parallel_hazard_test`:

> [src/backend/optimizer/util/clauses.c:L792-L818](../postgresql-17.0/src/backend/optimizer/util/clauses.c#L792-L818)

```c
/* core logic for all parallel-hazard checks */
static bool
max_parallel_hazard_test(char proparallel, max_parallel_hazard_context *context)
{
	switch (proparallel)
	{
		case PROPARALLEL_SAFE:
			/* nothing to see here, move along */
			break;
		case PROPARALLEL_RESTRICTED:
			/* increase max_hazard to RESTRICTED */
			Assert(context->max_hazard != PROPARALLEL_UNSAFE);
			context->max_hazard = proparallel;
			/* done if we are not expecting any unsafe functions */
			if (context->max_interesting == proparallel)
				return true;
			break;
		case PROPARALLEL_UNSAFE:
			context->max_hazard = proparallel;
			/* we're always done at the first unsafe construct */
			return true;
		default:
			elog(ERROR, "unrecognized proparallel value \"%c\"", proparallel);
			break;
	}
	return false;
}
```

注意 UNSAFE 那一行:**一旦碰到一个 unsafe 函数,立刻返回 true 停止扫描**——因为不可能有更坏的情况了,不必再扫。这是个早退优化(early return)。结果:如果整条查询里出现任何一个 `nextval`、任何写操作、任何 dblink,`max_parallel_hazard` 返回 UNSAFE,优化器直接放弃并行。

### 不这样会怎样:碰了共享状态的函数并行会出错

> **不这样会怎样**:考虑 `SELECT nextval('my_seq'), count(*) FROM huge_table GROUP BY ...`。如果硬并行,4 个 worker 各自跑一份 partial plan、各自调 `nextval`——序列是个全局共享对象,4 个进程同时 `nextval`,序列号会被乱跳、可能产生重复或竞争(序列本身有锁不会算错,但语义上"`nextval` 调用次数 = 输出行数"会被破坏,且序列号消耗速度变成 4 倍)。再考虑一个调用 `dblink` 连远程库的函数——worker 各自连一遍,远程库看到的连接数莫名翻 4 倍,且这些连接没有协调,行为不可预测。**碰了进程外共享状态的函数,并行下行为不可预测,所以一刀切禁掉**。

这就解释了第六节标题里"写操作(CRUD)不并行"的根:`INSERT/UPDATE/DELETE` 本质上就是"修改共享状态"(改 heap 页、改索引、产生 WAL),它们碰的是整个数据库的全局状态——所以 PG 干脆规定:**数据修改语句(CRUD)不并行**。并行只服务 `SELECT`。

`set_rel_consider_parallel` 里就能看到一连串"否决并行"的检查,以临时表为例:

> [src/backend/optimizer/path/allpaths.c:L607-L651](../postgresql-17.0/src/backend/optimizer/path/allpaths.c#L607-L651)

```c
	/* Assorted checks based on rtekind. */
	switch (rte->rtekind)
	{
		case RTE_RELATION:

			/*
			 * Currently, parallel workers can't access the leader's temporary
			 * tables.  We could possibly relax this if we wrote all of its
			 * local buffers at the start of the query and made no changes
			 * thereafter (maybe we could allow hint bit changes), and if we
			 * taught the workers to read them.  Writing a large number of
			 * temporary buffers could be expensive, though, and we don't have
			 * the rest of the necessary infrastructure right now anyway.  So
			 * for now, bail out if we see a temporary table.
			 */
			if (get_rel_persistence(rte->relid) == RELPERSISTENCE_TEMP)
				return;
			...
			/*
			 * Ask FDWs whether they can support performing a ForeignScan
			 * within a worker.  Most often, the answer will be no.  For
			 * example, if the nature of the FDW is such that it opens a TCP
			 * connection with a remote server, each parallel worker would end
			 * up with a separate connection, and these connections might not
			 * be appropriately coordinated between workers and the leader.
			 */
			if (rte->relkind == RELKIND_FOREIGN_TABLE)
			{
				Assert(rel->fdwroutine);
				if (!rel->fdwroutine->IsForeignScanParallelSafe)
					return;
				if (!rel->fdwroutine->IsForeignScanParallelSafe(root, rel, rte))
					return;
			}
```

读注释能感受到 PG 工程师的谨慎:临时表为什么不能并行?"worker 看不到 leader 的临时缓冲区,要让它能看到得把 leader 的全部 local buffer 写出来,代价大,而且基础设施没有,所以现在就不让"。FDW(外部表)同理:"每个 worker 各开一个 TCP 连接,这些连接没法协调,所以默认不行"——除非 FDW 自己实现 `IsForeignScanParallelSafe` 说可以。

**这些"现在不让"的注释,都是"不这样会怎样"在源码里的活化石**——每一个并行限制背后,都有一个被预见到的失败模式。

---

## 七、并行的局限:不是所有计划都能并行

最后把并行的几个硬限制列清楚——它们是从前面几节自然推出来的,知道边界才不会用错。

### 1. worker 数有全局上限

第三节讲过三道闸门:`max_worker_processes`(实例总 worker 数,默认 8)、`max_parallel_workers`(并行专用,默认 8)、`max_parallel_workers_per_gather`(单 Gather,默认 2)。**一条查询实际拿到的 worker 数,可能远少于优化器想要的**——这个差距在 `EXPLAIN` 里能看到(实际 worker 数 vs 计划 worker 数)。

### 2. 不是所有算子都支持并行

只有**parallel-aware**(知道自己在并行,主动瓜分工作)或 **parallel-safe**(并行下不会出错,但各 worker 各自全跑一份)的算子才能进 Gather 下面。已支持的:并行 SeqScan、并行 IndexScan、并行 Bitmap Heap Scan、并行 Hash Join(共建哈希表)、并行 Append(分区表)。**不支持**的典型:CTE 扫描(WorkTable 扫描有递归依赖)、一些自定义算子。

### 3. 并行 Nested Loop 通常没用

回扣第 5 章:NestedLoop 的代价是 O(外 × 内),靠的是"内层有索引、用外层参数化重扫"才划算。但并行下,内层索引扫描在 worker 里重扫,既难协调(每个 worker 各自的索引游标)、又往往没有 parallel-aware 的实现。所以**并行 NestedLoop 收益通常很小**,优化器很少选。

### 4. 写操作(CRUD)不并行

第六节解释过:`INSERT/UPDATE/DELETE` 修改共享状态,worker 同时改 heap/index/WAL 会乱套。所以 `INSERT ... SELECT` 这种"看起来能并行"的语句,**`SELECT` 部分可能并行,但最终的 `INSERT` 一定串行**(在 leader 里执行)。

### 5. 游标、可滚动结果集限制

如果查询以游标(`DECLARE CURSOR`)方式执行,要支持"前后翻"和"暂停/恢复",并行会出问题(worker 不能跨 fetch 调用保持状态)。所以游标查询默认不并行。

### 6. 串行执行下的"逃生"

万一并行计划出问题(比如某个 worker 崩了、或者环境不允许并行),Gather 会**优雅降级**——回扣第三节的 `need_to_scan_locally`:没启动到 worker,leader 自己把 partial plan 跑一份。查询结果不会错,只是慢(和不并行一样)。这种"能并行就并行,不能就退回串行"的设计,保证了并行的引入不会破坏正确性。

---

## 关键源码精读:ExecGather 的拉取循环

前面散落着贴了不少片段,这一节我们把并行执行的心脏——`ExecGather` 拉取 tuple 的主循环——完整走一遍。这是把"leader + worker + 共享队列"这套机制串起来的关键代码。

### 启动 worker 与取 tuple 的完整骨架

前面贴过 `ExecGather` 的开头(L137-L209),这里聚焦它**调用 `gather_getnext` 之后**的逻辑。前面已贴过 `gather_getnext`:

> [src/backend/executor/nodeGather.c:L256-L298](../postgresql-17.0/src/backend/executor/nodeGather.c#L256-L298)

这个函数是个 `while` 大循环,循环条件是 `nreaders > 0 || need_to_scan_locally`——意思是"还有 worker 没结束,或者 leader 自己也得扫一遍"。每轮做两件事:

1. **试着从某个 worker 读一个 tuple**(`gather_readnext`)。`gather_readnext` 内部轮询所有 reader,每个 reader 是一个 worker 的 shm_mq 包装。读到了就装进 `fslot`(漏斗槽,funnel slot,意思是"多个 worker 的流在这里汇成一个")返回。
2. **如果上面没读到,且需要本地扫**(`need_to_scan_locally`),就 `ExecProcNode(outerPlan)`——leader 自己跑一份 partial plan 取一行。

注意第 2 步里两行 `estate->es_query_dsa = ...`——leader 跑 partial plan 前,把"查询用的 DSA 区域"指针装进 EState;跑完再清掉。这是因为 partial plan 里可能有需要 DSA 的算子(比如并行哈希表),它要从 EState 找到那片共享内存。

### gather_readnext:轮询多个 worker 队列

真正轮询的逻辑在 `gather_readnext`:

> [src/backend/executor/nodeGather.c:L303-L345](../postgresql-17.0/src/backend/executor/nodeGather.c#L303-L345)

```c
static MinimalTuple
gather_readnext(GatherState *gatherstate)
{
	int			nvisited = 0;

	for (;;)
	{
		TupleQueueReader *reader;
		MinimalTuple tup;
		bool		readerdone;

		/* Check for async events, particularly messages from workers. */
		CHECK_FOR_INTERRUPTS();

		/*
		 * Attempt to read a tuple, but don't block if none is available.
		 *
		 * Note that TupleQueueReaderNext will just return NULL for a worker
		 * which fails to initialize.  We'll treat that worker as having
		 * produced no tuples; WaitForParallelWorkersToFinish will error out
		 * when we get there.
		 */
		Assert(gatherstate->nextreader < gatherstate->nreaders);
		reader = gatherstate->reader[gatherstate->nextreader];
		tup = TupleQueueReaderNext(reader, true, &readerdone);

		/*
		 * If this reader is done, remove it from our working array of active
		 * readers.  If all readers are done, we're outta here.
		 */
		if (readerdone)
		{
			Assert(!tup);
			--gatherstate->nreaders;
			if (gatherstate->nreaders == 0)
			{
				ExecShutdownGatherWorkers(gatherstate);
				return NULL;
			}
			memmove(&gatherstate->reader[gatherstate->nextreader],
					&gatherstate->reader[gatherstate->nextreader + 1],
					sizeof(TupleQueueReader *)
					* (gatherstate->nreaders - gatherstate->nextreader));
			...
```

读这段的核心是抓住一个字眼:`TupleQueueReaderNext(reader, true, &readerdone)`——第二个参数 `true` 表示 **nonblock(非阻塞)**。也就是说,**Gather 不会卡在某个 worker 上等它出数据**,而是"问一下,有就拿走,没有就换下一个问"。这就是为什么 Gather 输出是"哪个 worker 先算完就先吐"——天然负载均衡,快的 worker 多出力,慢的不拖累快的。

如果某个 worker 结束了(`readerdone`),把它从活跃 reader 数组里移除(`memmove`);所有 reader 都结束,`nreaders == 0`,关闭 worker(`ExecShutdownGatherWorkers`)返回 NULL,`gather_getnext` 的循环退出。

### 优雅降级与 leader 参与

回到 `ExecGather` 开头那行设置:

```c
/* Run plan locally if no workers or enabled and not single-copy. */
node->need_to_scan_locally = (node->nreaders == 0)
	|| (!gather->single_copy && parallel_leader_participation);
```

`need_to_scan_locally` 在两种情况下为真:

1. **没启动到任何 worker**(`nreaders == 0`)——优雅降级,leader 自己跑。
2. **不是 single-copy 模式,且 `parallel_leader_participation` 打开**——正常情况下,即使有 worker,leader 也参与跑一份,凑成 N+1 路并行。

`single_copy` 是个特殊场景:某些计划(比如 Gather 下面是 `Partial Aggregate` + 上层 Final Aggregate)只允许一份 partial plan 的输出,leader 不能重复跑——这时关掉 leader 参与。这种边角情况体现了并行计划设计的复杂性:**不是所有 partial plan 都能多份复制跑而不重复产出**。

> 把 `ExecGather` 和 `gather_readnext` 串起来,你就看清了并行执行的完整心跳:**启动时 fork worker + 建 reader;每个 ExecProcNode 触发一轮轮询,非阻塞地从最快出活的 worker 拿一个 tuple;某个 worker 完了就踢出数组;全完了关 worker**。这套机制和第 5 章的火山模型无缝衔接——Gather 节点对父节点就是一个普通的 `ExecProcNode` 来源,父节点完全不知道底下是 N 个进程在并行干活。

---

## 章末小结

### 一句话回顾本章

并行查询的本质,是把一棵 Plan 树的下段切成几份,让 leader 进程启动 N 个 worker 进程**各跑一份 partial plan**,worker 通过**共享内存队列 shm_mq** 把 tuple 送回,leader 用 **Gather / GatherMerge** 节点把它们合并成最终结果。它**没有另起一套执行框架**,而是火山模型里多了一个 Gather 算子——这是 PG 并行设计最优雅的地方。

本章可以拆成三个层次:

1. **要不要并行,优化器比代价决定**。代价模型给并行加了两个常数(`parallel_setup_cost = 1000`、`parallel_tuple_cost = 0.1`),所以并行计划一定有 1000 的固定启动开销。**对 OLTP 小查询,这笔开销大于并行省下的时间,纯亏**——所以 PG 默认不并行,只在查询代价足够大时才开。三道 GUC(`max_worker_processes`、`max_parallel_workers`、`max_parallel_workers_per_gather`)层层限制并行度,把"单查询占满机器"的风险挡住。
2. **怎么并行,leader + worker + shm_mq + Gather**。`ExecInitParallelPlan` 把 partial plan 和状态打包进 DSM;`LaunchParallelWorkers` 通过 `RegisterDynamicBackgroundWorker` 真正 fork worker;worker 在 `ParallelWorkerMain` 里恢复状态后跑计划,把结果通过 `shm_mq_send` 送回;`ExecGather` 轮询各 worker 的队列,非阻塞地取最快的 tuple,合并输出。worker 数由 `compute_parallel_worker` 按表大小对数增长决定,小表直接否决。
3. **什么不能并行,并行安全分级**。每个函数标 `PROPARALLEL_SAFE/RESTRICTED/UNSAFE`,优化器用 `max_parallel_hazard` 取整条查询里"最坏"的级别决定是否并行。UNSAFE(碰了序列、写操作、dblink 这类共享状态)→ 整条查询禁并行。这解释了为什么**写操作(CRUD)不并行**:修改共享状态在多 worker 下会乱套。

### 回扣主线:并行服务"快",但被"不乱"约束

回到全书的二分法:**数据库一半在让数据快,一半在让数据不丢不乱**。并行查询毫无疑问站在"**快**"这一侧——它直接把单核到顶的 CPU 瓶颈,变成多核并行加速,是 OLAP 大查询"让数据快"的关键杠杆。

但并行查询的设计处处体现"**不乱**"对它的约束:

- 第二节讲的"默认不并行、只在划算时才开",是为了**不让并行的 overhead 把小查询拖慢**——这是对"性能不乱"的维护。
- 第六节讲的并行安全分级,是为了**不让多 worker 同时碰共享状态而出错**——这是对"数据不乱"的维护。
- 第三节讲的 partial plan 在 worker 里各跑一份、worker 数受三道 GUC 限制、Gather 优雅降级——都是为了让并行**不破坏正确性、不拖累别的查询**。

所以并行查询这一章,是全书主线"快 vs 不丢不乱"二分法最生动的合流:**它在追求快的同时,被不乱牢牢约束着**。每一处保守的设计(默认不开、worker 限数、写不并行),都是"不乱"在牵制"快"。

对应数据的三个本性,并行查询主要对付的是**"用户说要什么、机器只会按位置读"**这一本性在 OLAP 场景下的放大——大表扫描和聚合的 CPU 开销,只有靠多核并行才能压住。而它对"易失"(worker 是进程可能崩)和"共享冲突"(多 worker 协调)这两条本性的处理,体现在 worker 的优雅降级和并行安全检查里。

### 想深入往哪钻

- **并行执行器**:[src/backend/executor/nodeGather.c](../postgresql-17.0/src/backend/executor/nodeGather.c)(Gather)、[src/backend/executor/nodeGatherMerge.c](../postgresql-17.0/src/backend/executor/nodeGatherMerge.c)(GatherMerge,带堆合并)。重点读 `gather_getnext` 和 `gather_readnext` 的轮询逻辑。
- **并行基础设施**:[src/backend/executor/execParallel.c](../postgresql-17.0/src/backend/executor/execParallel.c)(`ExecInitParallelPlan`、`ExecParallelCreateReaders`)、[src/backend/access/transam/parallel.c](../postgresql-17.0/src/backend/access/transam/parallel.c)(`CreateParallelContext`、`LaunchParallelWorkers`、`ParallelWorkerMain`——这是并行框架的核心,不只服务查询)。
- **共享内存队列**:[src/backend/storage/ipc/shm_mq.c](../postgresql-17.0/src/backend/storage/ipc/shm_mq.c)。`shm_mq_sendv`/`shm_mq_receive` 的阻塞与唤醒机制值得细读,它用进程闩(latch)做同步,是 PG 进程间通信的基础组件。
- **优化器并行代价**:[src/backend/optimizer/path/costsize.c](../postgresql-17.0/src/backend/optimizer/path/costsize.c) 的 `cost_gather`(L436)、`cost_gather_merge`(L474);以及 [src/backend/optimizer/path/allpaths.c](../postgresql-17.0/src/backend/optimizer/path/allpaths.c) 的 `set_rel_consider_parallel`(L589,一表一表地判并行)、`compute_parallel_worker`(L4203)。
- **并行安全**:[src/backend/optimizer/util/clauses.c](../postgresql-17.0/src/backend/optimizer/util/clauses.c) 的 `max_parallel_hazard`(L734)和 `max_parallel_hazard_test`(L794)。再追 `func_parallel`(查单个函数的 proparallel 标签)。
- **并行 Hash**:[src/backend/executor/nodeHash.c](../postgresql-17.0/src/backend/executor/nodeHash.c) 的 `ExecChooseHashTableSize` 支持 `parallel_workers` 参数,这是多个 worker 共建哈希表的基础——比本章讲的并行 SeqScan 更复杂,值得专门研究。
- **观测**:`EXPLAIN (ANALYZE, VERBOSE)` 输出里会显示每个 Gather 节点的"Workers Planned"和"Workers Launched"——前者是优化器想要的、后者是实际启动到的。这俩数不一致,通常意味着 `max_worker_processes` 不够,是诊断并行问题的第一手数据。

---

> **全书正文到这里收尾。**
>
> 让我们回过头,把这条**一条 SQL 的一生**从头走一遍:
>
> - **P1 查询引擎**:你敲下的 SQL,从连接进门(第 2 章),被解析器拆成树(第 3 章),优化器决定走哪条路最省(第 4 章),执行器按火山模型把计划跑出来(第 5 章)。
> - **P2 存储引擎**:执行器要取的数据,住在 8KB 的页面里(第 6 章),按堆表和元组摆好(第 7 章),通过 Buffer Pool 在内存中转(第 8 章),靠 FSM/VM 找空位和可见性(第 9 章)。
> - **P3 索引**:为了不每次扫全表,B 树(第 11 章)和各种索引家族(第 12 章)给你 O(log n) 的查找(第 10 章为什么需要、第 13 章怎么和堆表配合)。
> - **P4 事务与并发**:多人同时改数据时,事务 ACID(第 14 章)、隔离级别(第 15 章)、锁(第 16 章)、MVCC(第 17 章)保证数据不乱。
> - **P5 持久性与恢复**:内存里的改动靠 WAL 先记日志(第 18 章),靠 checkpoint 划定同步点(第 19 章),崩溃后从 redo 重放(第 20 章)——掉电不丢。
> - **P6 进阶**:MVCC 的代价由 VACUUM 收拾(第 21 章),单机的可靠由复制扩展到机房(第 22 章),单核的极限由本章的并行查询打破(第 23 章)。
>
> 一条 SQL 进门、被理解、被规划、被并行执行;它的数据被高效地存在内存和磁盘、被索引加速找到、在并发中保持一致、在掉电和崩溃中幸存、在单机损毁时被复制备份、在大查询时被多核并行加速——**这条旅程的每一站,都对应着数据库内核的一个子系统,每一个子系统都是被数据的三个本性(易失、共享冲突、声明式查询)逼出来的**。
>
> 现在你已经走完了一条 SQL 的完整一生,看清了数据库这台机器是怎么运转的。**附录**里会教你两件事:怎么自己开始读 PG 源码(目录结构、阅读路线、cscope/ctags 的用法),以及怎么用 `EXPLAIN`、`pg_stat_*`、`pageinspect`、`pg_waldump` 这些工具去**观测**一个活着的数据库——把这本书里讲的每一条原理,在你自己的机器上亲眼看见。翻开 **附录 A · 怎么读 PG 源码**。
