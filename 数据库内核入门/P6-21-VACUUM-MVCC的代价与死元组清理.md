# 第 21 章 · VACUUM:MVCC 的代价与死元组清理

> **前置**:你需要先读过[第 17 章《MVCC:多版本并发控制》](P4-17-MVCC-多版本并发控制.md)(本章的母题——死元组、冻结、事务号回绕,全是从第 17 章直接接过来的)和[第 9 章《FSM 与 VM:两个辅助地图》](P2-09-FSM与VM-两个辅助地图.md)(VM 的 `ALL_VISIBLE`/`ALL_FROZEN` 两位是 VACUUM 跳页的依据),以及[第 7 章《堆表与元组》](P2-07-堆表与元组-一行数据怎么落盘.md)(tuple 结构、行指针状态 `LP_NORMAL`/`LP_DEAD`/`LP_UNUSED`)。本章是 P6 的第一篇,它专门回答 P4 第 17 章结尾埋下的那个钩子:**MVCC 留下来的旧版本(dead tuple)和会回卷的事务号,谁来收拾?**

> **核心问题**:第 17 章讲清了 MVCC 为什么"读不阻塞写"——靠的是"UPDATE/DELETE 不就地改、而是产生新版本、把旧版本留着"。但这个设计有个绕不开的代价:**旧版本(dead tuple)会无限堆积**,而且 **PG 的事务号是 32 位、用完会回绕(wraparound)**。这两件事数据库自己不会自愈:旧版本不会自动消失(它们白占空间,会让表膨胀、查询越来越慢),老事务号也不会自动"过期"(不处理就会在回绕后让数据"突然看不见",这是 PG 最怕的灾难)。**谁负责打扫?答案是 VACUUM。** 本章讲清——
>
> - 为什么 VACUUM 是 MVCC 的内置收尾,而不是可有可无的运维操作?(不 VACUUM 会怎样?从"表膨胀变慢"到"事务号回卷致数据库强制停机")
> - 一次 lazy VACUUM 实际做了哪几件事?(识别死元组 → 回收空间给新行用 → 清索引里指向死元组的项 → 更新 VM/FSM → 冻结老元组 → 可能截断 clog)
> - lazy VACUUM 和 VACUUM FULL 各自的代价?(一个不锁表、空间不还 OS;一个全程锁表、空间还 OS)
> - autovacuum 怎么自动触发?为什么它防回绕的那一档"不认任何手动配置、必须跑"?
> - 单页内的即时清理(page prune)和 HOT 更新怎么和 VACUUM 分工?
> - VM 的两个位怎么让 VACUUM"跳过整页",把工作量从"全表"缩到"真正有垃圾的页"?

> **读完本章你会明白**:为什么 PG 比 MySQL InnoDB 更需要一套显式的清理机制(InnoDB 靠 undo log 在事务结束后即时清理,PG 把清理推迟到 VACUUM);表膨胀(bloat)是怎么一步步把一个高写入表拖垮的;XID wraparound 这场"PG 最怕的灾难"的完整因果链(为什么 32 位、为什么回绕会让数据"消失"、为什么 PG 会"自己把自己关进只读"救命);autovacuum 的阈值公式(`threshold + scale_factor * reltuples`)怎么把"该不该扫这张表"量化;以及为什么"装了 PG 就别关 autovacuum"是 PG 运维的第一条铁律。

> **如果一读觉得太难**:先记四件事——① **MVCC 的代价是死元组堆积 + 事务号回绕**,两件事都得 VACUUM 收尾;② **VACUUM 不删数据、不还空间给 OS**,它只是把死元组占的行指针标记成 `LP_UNUSED`,让新插入能复用这块空间(表大小基本不变,但不再膨胀);③ **XID wraparound 是 PG 最怕的灾难**——32 位事务号会用完,不冻结老元组的话回绕后数据会"看不见",PG 会强制自己进只读模式自保,这就是为什么 anti-wraparound VACUUM 不认任何手动配置;④ **autovacuum 别关**,关了必遭 wraparound。lazy VACUUM 的多趟扫描、freeze 的细节代码第二遍配合源码抠,不影响理解主线。

---

## 一、VACUUM 是 MVCC 的内置收尾:不是可选运维,是机制的一半

第 17 章结尾我们留下一个钩子:MVCC 让读写不阻塞,代价是**旧版本必须留着**(供持有老快照的事务读),这些旧版本叫**死元组(dead tuple)**——它们"对当前和之后所有事务都不可见",但**物理上还占着页里的空间**。问题是:**谁来把它们清掉?**

> **一句话先立住**:在 PG 里,**清死元组不是某条 UPDATE/DELETE 完了就顺手做的事,而是推迟到一个独立的、叫 VACUUM 的操作里统一做**。这是 PG 的设计选择,不是 PG 的缺陷——但它意味着,如果一个 PG 数据库从不 VACUUM,死元组就会无限堆积。这一节先讲清"为什么必须有个 VACUUM",再讲"不 VACUUM 会怎样"。

### 死元组从哪来:复习第 17 章的伏笔

回顾一下死元组的来源(第 17 章详讲过):

- **DELETE**:给旧行的 `t_xmax` 打上"被某事务删了",旧行物理上**不动**,等所有人不再需要它后才能回收。
- **UPDATE**:本质是"DELETE 旧版本 + INSERT 新版本",旧版本 `t_xmax` 被设、新版本另起一处写,**旧版本占的空间同样留着**。
- **回滚的事务插的行**:`t_xmin` 那个事务 abort 了,这行谁也看不见,也是死元组。

这些"已经死了、但物理上还占着位"的旧版本,统称死元组。第 9 章我们提过页里有行指针状态 `LP_DEAD`(表示"这行的 tuple 已死、但行指针还留着,等 VACUUM 来清"),就是为它们准备的。

### 不这样会怎样:为什么不能像 InnoDB 那样"事务一完就清"

> **不这样会怎样**:对比一下 MySQL InnoDB。InnoDB 也是 MVCC,但它清死元组的策略是 **"在事务结束、或回表读到死元组时,顺手清理"**——它有一套 undo log(回滚日志),每条记录上挂着"指向旧版本的指针",当系统判断"没有事务还需要这个旧版本"时,后台的 purge 线程就把旧版本从页里抹掉。这条路的代价是:**每条记录都要带 undo 指针、每次操作都要维护 undo 链**,但好处是清理"跟着事务走",通常不需要全表扫描。

PG 走的是**另一条路**:tuple 头上的 `t_xmin`/`t_xmax` 只记录"是谁生了我、是谁灭了我",**不挂任何 undo 指针**。这样设计的好处是 tuple 结构简单、写路径快(改一行不用维护额外的 undo 链);代价是**系统没法"顺藤摸瓜"找到散落在各个页里的死元组**——它们和活元组长得一模一样,只是 `t_xmax` 已经提交。要找它们,只能**扫表**。这个"扫表找死元组并回收"的操作,就是 VACUUM。

> 这是两条工程路线的取舍:**InnoDB 用更复杂的写路径(undo 链)换"即时清理、不需扫表";PG 用更简单的写路径换"延迟清理、需要扫表"**。没有谁绝对更好——PG 的选择让它的 tuple 更紧凑、写路径更轻,但把这个"打扫"的成本单独拎出来,叫 VACUUM。理解了这一层,你就明白为什么 VACUUM 在 PG 里是"机制的一半"而不是"运维的可选项":**PG 把 MVCC 的清理代价,显式地、推迟地、单独地还了**。

---

## 二、不 VACUUM 会怎样:两笔账,一笔慢、一笔致命

这一节是本章最有冲击力的一段——把"从不 VACUUM"的后果写具体。它有两笔账,**第一笔让数据库越来越慢,第二笔让数据库直接停机**。

### 第一笔账:表膨胀(bloat),查询越来越慢

死元组不回收,直接后果是**表膨胀(bloat)**:一个表逻辑上只有 100 万行,但因为历史上有过大量 UPDATE/DELETE,物理页里塞满了"已死但没清"的旧版本,实际占用可能膨胀到几倍、几十倍。

```text
一张高频更新的账户表,逻辑上 1000 万行,从不 VACUUM:

理想情况(没有死元组):           真实情况(死元组堆积成山):
┌──────────────────┐             ┌──────────────────┐
│ 活元组 活元组 活元组 │             │ 活 死 死 活 死 死 活 │   ← 死元组占了大半空间
│ 活元组 活元组 活元组 │             │ 死 活 死 死 死 活 死 │
│ ...              │             │ 死 死 死 活 死 死 死 │
└──────────────────┘             │ ...              │   ← 物理页数是逻辑的 N 倍
  物理页数 = 1000 万行 / 每页      └──────────────────┘
                                  物理页数 = N × (理想页数)
```

膨胀的连锁后果:

- **全表扫描变慢**:`SELECT count(*)`、报表查询、autovacuum 自己扫表,都得把这些"死页"也读一遍——逻辑上 1000 万行的工作量,变成物理上几千万行的 I/O。
- **Buffer Pool 被垃圾挤爆**:热数据(活元组)被冷冰冰的死元组挤出缓存,缓存命中率掉。
- **索引膨胀**:索引里也指向了一堆死元组的 TID,索引变大、深度变深,索引扫描变慢(下面讲清为什么 VACUUM 还要清索引)。
- **磁盘爆满**:表和索引文件越涨越大,最终撑爆磁盘。

**这一笔账是"慢和浪费",但不致命**——数据库还能跑,只是越来越慢。运维上常见症状:一张以前秒回的表,某天突然要几秒;`pg_stat_user_tables` 里 `n_dead_tup` 列蹭蹭涨。

### 第二笔账:事务号回绕(XID wraparound)——PG 最怕的灾难

这一笔才是**致命的**。它来自第 17 章第八节埋的另一条伏笔:**PG 的事务号是 32 位,约 42 亿,会用完、会回绕**。这里把因果链彻底讲透,因为它是 PG 运维里最容易被忽略、一旦发生又最难挽回的灾难。

#### 事务号为什么是 32 位、为什么会回绕

PG 的 `TransactionId` 是 **32 位无符号整数**(第 17 章贴过):

> [src/include/access/transam.h:31-35](../postgresql-17.0/src/include/access/transam.h#L31-L35)

```c
#define InvalidTransactionId		((TransactionId) 0)
#define BootstrapTransactionId		((TransactionId) 1)
#define FrozenTransactionId			((TransactionId) 2)
#define FirstNormalTransactionId	((TransactionId) 3)
#define MaxTransactionId			((TransactionId) 0xFFFFFFFF)
```

`MaxTransactionId = 0xFFFFFFFF`,约 **42 亿**。事务号从 3 开始发(`FirstNormalTransactionId`),每开一个写事务就 +1,涨到 42 亿就到顶。到顶怎么办?**回绕(wraparound)——下一个事务号从 3 重新开始**。

这是一个**环形空间**:42 亿个事务号围成一圈,事务号的大小比较是**模 2³² 的**(第 17 章讲过)。只要保证"系统里最老的未冻结事务号"和"最新已分配事务号"之间的距离不超过半圈(21 亿),大小关系就不会因回绕而反转;一旦超过半圈,比较就乱了。

#### 不冻结会怎样:回绕后老元组"消失"

> **不这样会怎样**:假设我们从不冻结老元组。事务号涨到 42 亿,回绕,新事务号又从 3 开始发。这时表里有一行,它的 `t_xmin = 4 000 000 000`(很久以前某事务插的,早就提交了)。现在一个新事务(事务号 X=10,回绕后的)来做可见性判定。源码里比 `t_xmin` 和快照用的是 `TransactionIdPrecedes`(事务号 A < B 吗),它按"模 2³² 的环形"比较——**X=10 会被判定成"比 4 000 000 000 小",也就是"X 是更老的事务"**。可物理上 X=10 是回绕后的新事务,这行(`t_xmin=40 亿`)其实是个老事务插的、早就该对 X 可见——结果可见性判定全错乱:有些本该可见的行"看不见"(数据像凭空消失),有些本该死的行"活着"。

这就是 **XID wraparound(事务号回绕)** 灾难。一旦真的发生回绕导致可见性错乱,数据库会读到**自相矛盾的数据**,这是不可逆的损坏——因为 PG 没法知道哪些行"原本该可见、现在被错判了"。

#### 解法:冻结(freeze),把老元组的 xmin 换成 FrozenXID

PG 的解法在第 17 章第八节讲过原理,这里从 VACUUM 的视角补一句关键的话:**冻结这件事,是 VACUUM 干的**。

冻结的核心:`t_infomask` 上设 `HEAP_XMIN_FROZEN` hint bit(等于 `HEAP_XMIN_COMMITTED | HEAP_XMIN_INVALID` 两个互斥 bit 同时置位,第 17 章讲过这个编码),把元组的 `t_xmin` 语义上替换成 `FrozenTransactionId`(=2)——这个特殊事务号"对所有快照都可见、不需要再查 clog、不需要再比快照"(第 17 章 `HeapTupleSatisfiesMVCC` 的分支⑤里 `if (!HeapTupleHeaderXminFrozen(tuple) && XidInMVCCSnapshot(...))` 就是为了跳过冻结元组)。冻结后,**这个元组原本占用的那个老事务号就不再有意义了**——它的事务状态在 clog 里可以被截断、被遗忘,事务号的"可用区间"就被腾出来。

> **冻结的数学效果**:`xidWrapLimit = oldest_datfrozenxid + (MaxTransactionId >> 1)`(第 17 章贴过 `SetTransactionIdLimit`)。**回绕安全距离 = 全库最老的未冻结事务号 + 半圈(21 亿)**。冻结的作用,就是把"最老未冻结事务号"(`oldest_datfrozenxid`)**不断往前推**,让这个半圈安全距离始终成立。**谁负责推?VACUUM——每张表 VACUUM 完,会更新该表的 `relfrozenxid`;全库所有表里最老的那个 `relfrozenxid`,就是全库的 `oldest_datfrozenxid`**(存在 `pg_database.datfrozenxid`)。所以"定期 VACUUM 冻结老表"和"防回绕"是同一件事:VACUUM 推进冻结水位线,水位线推得动,半圈安全距离就在,就不会回绕。

#### PG 的救命机制:四道闸 + 强制只读

为了不让回绕真的发生,PG 在发事务号的地方(`GetNewTransactionId`)设了**四道闸**(第 17 章贴过 [varsup.c:123-167](../postgresql-17.0/src/backend/access/transam/varsup.c#L123-L167) 和阈值公式 [varsup.c:389-406](../postgresql-17.0/src/backend/access/transam/varsup.c#L389-L406)):

| 阈值 | 含义(距回绕点的距离) | 触发动作 |
|---|---|---|
| `xidVacLimit` | 离回绕还很远,但该冻结了(`oldest_datfrozenxid + autovacuum_freeze_max_age`,默认 2 亿) | 触发 autovacuum 的 **anti-wraparound** 扫描 |
| `xidWarnLimit` | 离回绕 4000 万 | 日志 WARNING |
| `xidStopLimit` | 离回绕 300 万 | **拒绝再发事务号**,普通写直接 ERROR(只读还能用) |
| `xidWrapLimit` | 回绕点本身 | **绝对不可越过** |

**最关键的是 `xidStopLimit` 这道闸**:当事务号逼近回绕点(只剩 300 万的余量)时,PG 会**拒绝任何需要新事务号的写操作**,数据库进入**只读模式**(self-administered emergency、自救):

> [src/backend/access/transam/varsup.c:1009-1021](../postgresql-17.0/src/backend/access/transam/varsup.c#L1009-L1021)

```c
			if (IsUnderPostmaster &&
				TransactionIdFollowsOrEquals(xid, xidStopLimit))
			{
				char	   *oldest_datname = get_database_name(oldest_datoid);

				/* complain even if that DB has disappeared */
				if (oldest_datname)
					ereport(ERROR,
							(errcode(ERRCODE_PROGRAM_LIMIT_EXCEEDED),
							 errmsg("database is not accepting commands that assign new transaction IDs to avoid wraparound data loss in database \"%s\"",
									oldest_datname),
							 errhint("Execute a database-wide VACUUM in that database.\n"
									 "You might also need to commit or roll back old prepared transactions, or drop stale replication slots.")));
```

注意这条报错的措辞——"**database is not accepting commands ... to avoid wraparound data loss**"。**这是 PG 故意把自己关进只读,以避免数据真的损坏**。报错的 hint 直接告诉你怎么救:**在这个数据库里跑一个全库 VACUUM**(`Execute a database-wide VACUUM`)。这是 PG 设计里最戏剧性的一幕:宁可让数据库停摆,也不让它读到错乱的数据。

> **为什么 anti-wraparound VACUUM 不认任何手动配置**:这正是上面 `relation_needs_vacanalyze` 里 `force_vacuum` 那一支的用意。当一张表的 `relfrozenxid` 老到逼近 `xidForceLimit`(`recentXid - autovacuum_freeze_max_age`),`force_vacuum` 会被置为 `true`,autovacuum 会**无视该表 `autovacuum_enabled = false` 的 reloption**,强制对它跑一次以冻结为主的 VACUUM:

> [src/backend/postmaster/autovacuum.c:2989-3014](../postgresql-17.0/src/backend/postmaster/autovacuum.c#L2989-L3014)

```c
	/* Force vacuum if table is at risk of wraparound */
	xidForceLimit = recentXid - freeze_max_age;
	if (xidForceLimit < FirstNormalTransactionId)
		xidForceLimit -= FirstNormalTransactionId;
	relfrozenxid = classForm->relfrozenxid;
	force_vacuum = (TransactionIdIsNormal(relfrozenxid) &&
					TransactionIdPrecedes(relfrozenxid, xidForceLimit));
	...
	*wraparound = force_vacuum;

	/* User disabled it in pg_class.reloptions?  (But ignore if at risk) */
	if (!av_enabled && !force_vacuum)
	{
		*doanalyze = false;
		*dovacuum = false;
		return;
	}
```

**关键就在 `if (!av_enabled && !force_vacuum)`**——只有当 `force_vacuum` 为假时,用户的 `autovacuum_enabled = false` 才会让这张表跳过 autovacuum。一旦 `force_vacuum` 为真(表有回绕风险),用户关 autovacuum 的设置**被直接无视**。这是 PG 写进内核的"防自杀条款":你可以嫌 autovacuum 慢、关掉普通清理,但你**关不掉防数据损坏的那一档**。

> **一句话总结这一段**:wraparound 是 PG 最致命的灾难——它让数据"看不见",且一旦发生难以挽回。PG 用"四道闸 + 强制只读 + anti-wraparound VACUUM 不认配置"三层防线把它挡住,而这三层防线的核心动作都是**冻结老元组、推进 `datfrozenxid`**——这件事只有 VACUUM 能做。这就是为什么"VACUUM 不是可选运维"——它关乎数据存亡。

---

## 三、VACUUM 的命令入口:从 SQL 到 `heap_vacuum_rel`

讲清了"为什么必须 VACUUM",现在看 VACUUM 在代码里怎么入口、怎么走。

你敲一句 `VACUUM users;`,经过解析后(解析器把 `VACUUM` 当成工具命令,不是普通 DML),进入命令处理:

> [src/backend/commands/vacuum.c:147](../postgresql-17.0/src/backend/commands/vacuum.c#L147)

```c
ExecVacuum(ParseState *pstate, VacuumStmt *vacstmt, bool isTopLevel)
```

`ExecVacuum` 把 SQL 里的选项(`FULL`、`FREEZE`、`VERBOSE`、`ANALYZE` 等)解析成 `VacuumParams`,然后调 `vacuum()`:

> [src/backend/commands/vacuum.c:478-479](../postgresql-17.0/src/backend/commands/vacuum.c#L478-L479)

```c
vacuum(List *relations, VacuumParams *params, BufferAccessStrategy bstrategy,
	   MemoryContext vac_context, bool isTopLevel)
```

`vacuum()` 有一个**关键约束**(注释写得很直白):

> [src/backend/commands/vacuum.c:491-498](../postgresql-17.0/src/backend/commands/vacuum.c#L491-L498)

```c
	/*
	 * We cannot run VACUUM inside a user transaction block; if we were inside
	 * a transaction, then our commit- and start-transaction-command calls
	 * would not have the intended effect!	There are numerous other subtle
	 * dependencies on this, too.
	 *
	 * ANALYZE (without VACUUM) can run either way.
	 */
	if (params->options & VACOPT_VACUUM)
	{
		PreventInTransactionBlock(isTopLevel, stmttype);
```

**VACUUM 不能在一个用户事务块里跑**(`VACUUM` 不能跟在 `BEGIN ... COMMIT` 中间)。原因下面讲——VACUUM 内部要自己管事务的起止(每处理一些表就 commit 一次,释放锁、推进 `datfrozenxid`),如果套在外层事务里,这些内部 commit 会被外层事务吞掉,锁和事务号都得不到释放。所以你 `VACUUM` 时如果手贱写了 `BEGIN; VACUUM; COMMIT;`,PG 会报错"VACUUM cannot run inside a transaction block"。

`vacuum()` 对每张要清理的表,最终调到 heap AM 的入口 `heap_vacuum_rel`:

> [src/backend/access/heap/vacuumlazy.c:284-296](../postgresql-17.0/src/backend/access/heap/vacuumlazy.c#L284-L296)

```c
/*
 *	heap_vacuum_rel() -- perform VACUUM for one heap relation
 *
 *		This routine sets things up for and then calls lazy_scan_heap, where
 *		almost all work actually takes place.  Finalizes everything after call
 *		returns by managing relation truncation and updating rel's pg_class
 *		entry. (Also updates pg_class entries for any indexes that need it.)
 *
 *		At entry, we have already established a transaction and opened
 *		and locked the relation.
 */
void
heap_vacuum_rel(Relation rel, VacuumParams *params,
				BufferAccessStrategy bstrategy)
```

`heap_vacuum_rel` 是**单表的入口**——它做准备工作(算冻结阈值、分配统计内存),然后调 workhorse `lazy_scan_heap` 干活,收尾时处理可能的表尾部 truncate、更新 `pg_class.relfrozenxid`。

> **autovacuum 的入口和手动 VACUUM 不同,但汇流到同一个函数**:autovacuum 的 worker 进程(`AutoVacWorkerMain`)接到 launcher 派的任务后,也走 `autovacuum_do_vac_analyze` → `vacuum()` → `heap_vacuum_rel`。也就是说,**手动 `VACUUM` 和 autovacuum 在底层是同一条路**,区别只在"谁触发、用什么参数"。

---

## 四、lazy VACUUM 干什么:workhorse `lazy_scan_heap` 的六件事

`heap_vacuum_rel` 把活儿几乎全交给 `lazy_scan_heap`(叫 lazy 是因为它的设计哲学:**尽可能别挡住正常读写**)。看 workhorse 的注释:

> [src/backend/access/heap/vacuumlazy.c:780-815](../postgresql-17.0/src/backend/access/heap/vacuumlazy.c#L780-L815)

```c
/*
 *	lazy_scan_heap() -- workhorse function for VACUUM
 *
 *		This routine prunes each page in the heap, and considers the need to
 *		freeze remaining tuples with storage (not including pages that can be
 *		skipped using the visibility map).  Also performs related maintenance
 *		of the FSM and visibility map.  These steps all take place during an
 *		initial pass over the target heap relation.
 *
 *		Also invokes lazy_vacuum_all_indexes to vacuum indexes, which largely
 *		consists of deleting index tuples that point to LP_DEAD items left in
 *		heap pages following pruning.  Earlier initial pass over the heap will
 *		have collected the TIDs whose index tuples need to be removed.
 *
 *		Finally, invokes lazy_vacuum_heap_rel to vacuum heap pages, which
 *		largely consists of marking LP_DEAD items (from vacrel->dead_items)
 *		as LP_UNUSED.  This has to happen in a second, final pass over the
 *		heap, to preserve a basic invariant that all index AMs rely on: no
 *		extant index tuple can ever be allowed to contain a TID that points to
 *		an LP_UNUSED line pointer in the heap. ...
 */
static void
lazy_scan_heap(LVRelState *vacrel)
```

这段注释把 lazy VACUUM 的全部工作说清楚了。提炼成**六件事**(顺序大致是它们在代码里发生的顺序):

### 第 1 件:扫描堆表,识别死元组(prune + 收集 LP_DEAD)

`lazy_scan_heap` 从第 0 页扫到最后一页。对每个页,先尝试拿到 **cleanup lock**(一种特殊的 buffer 锁,见下),然后调 `heap_page_prune_and_freeze` 做"页内清理"(prune):

> [src/backend/access/heap/vacuumlazy.c:1431-1439](../postgresql-17.0/src/backend/access/heap/vacuumlazy.c#L1431-L1439)

```c
	/*
	 * We will update the VM after collecting LP_DEAD items and freezing
	 * ...
	 */
	heap_page_prune_and_freeze(rel, buf, vacrel->vistest, prune_options,
```

prune 做什么?把页里那些"对当前所有事务都不可见"的死元组,其行指针标记成 `LP_DEAD`(意思是"这槽位的 tuple 已死、可回收")。这部分留到第六节和 HOT 一起讲——这里先记住:**prune 把"逻辑上的死元组"变成"页里物理上标了 LP_DEAD 的行指针"**,并把这些 LP_DEAD 对应的 TID 收集到一个叫 `dead_items` 的结构里:

> [src/backend/access/heap/vacuumlazy.c:187-188](../postgresql-17.0/src/backend/access/heap/vacuumlazy.c#L187-L188)

```c
	TidStore   *dead_items;		/* TIDs whose index tuples we'll delete */
	VacDeadItemsInfo *dead_items_info;
```

`dead_items` 是个 `TidStore`(一种压缩的 TID 集合,默认最多用 `maintenance_work_mem` 或 `autovacuum_work_mem` 内存)。`dead_items` 里存的就是"这趟 VACUUM 要清理的所有死元组的 TID"——它是后面两件事(清索引、清堆)的依据。

### 第 2 件:冻结老元组(freeze)

prune 的同时,`heap_page_prune_and_freeze` 会**判断每个活元组要不要冻结**——如果它的 `t_xmin` 对应事务老于本次 VACUUM 的 `FreezeLimit`(冻结水位线,由调用方根据 `vacuum_freeze_min_age` 等参数算出来),就给它设上 `HEAP_XMIN_FROZEN`。冻结的具体逻辑在 `heap_prepare_freeze_tuple`:

> [src/backend/access/heap/heapam.c:6550](../postgresql-17.0/src/backend/access/heap/heapam.c#L6550)

```c
heap_prepare_freeze_tuple(HeapTupleHeader tuple, ...)
```

它逐字段判断"这行能不能冻、要不要冻",决定后,`heap_page_prune_and_freeze` 把 `HEAP_XMIN_FROZEN` 写到 tuple 的 `t_infomask` 上(第 17 章贴过 `frz->t_infomask |= HEAP_XMIN_FROZEN` 在 [heapam.c:6747-6752](../postgresql-17.0/src/backend/access/heap/heapam.c#L6747-L6752))。

> **冻结要写 WAL**:像 hint bit 一样改 `t_infomask`,但冻结的改动必须写一条专门的 WAL freeze record——因为崩溃恢复时必须能重放"这行被冻了",否则重启后又看到未冻结的老 `t_xmin`,可能在下次回绕时出问题。这是 freeze 比"普通 hint bit"更重的地方。

### 第 3 件:更新 VM(标记全可见/全冻结页)

扫完一页,如果这一页**所有活元组都对所有人可见、且都已冻结**,VACUUM 会把这页在 VM 里的 `ALL_VISIBLE`(和可能 `ALL_FROZEN`)位设上(第 9 章详讲过 VM 的两位)。设上之后,**下次 VACUUM 和仅索引扫描都能跳过这页**。这是 lazy VACUUM 让后续工作越变越轻的关键。

判断"这页是不是全可见"的核心函数是 `heap_page_is_all_visible`:

> [src/backend/access/heap/vacuumlazy.c:2952](../postgresql-17.0/src/backend/access/heap/vacuumlazy.c#L2952)

```c
heap_page_is_all_visible(LVRelState *vacrel, Buffer buf, ...)
```

它扫一遍这页所有 tuple,只要有一个不是"对所有人可见",就返回否。注意第 9 章讲过:**VM 设位必须写 WAL**(因为 `ALL_VISIBLE` 和页头的 `PD_ALL_VISIBLE` 成对、涉及崩溃一致性),所以这一步不是免费的。

### 第 4 件:清索引——删掉指向死元组的索引项

这是 VACUUM 最容易被忽略但很重要的一步。索引(比如 B 树)里每个项都存着 `(key, TID)`,TID 指向堆里的某个 tuple。一个死元组被清掉后,**它在索引里的对应项就成了"野指针"**——指向一个不复存在的堆位置。如果不删,索引会越来越臃肿(索引膨胀),而且可能让索引扫描跑到错的地方。

清索引的动作由 `lazy_vacuum` 触发:

> [src/backend/access/heap/vacuumlazy.c:1859-1871](../postgresql-17.0/src/backend/access/heap/vacuumlazy.c#L1859-L1871)

```c
lazy_vacuum(LVRelState *vacrel)
{
	bool		bypass;

	/* Should not end up here with no indexes */
	Assert(vacrel->nindexes > 0);
	Assert(vacrel->lpdead_item_pages > 0);

	if (!vacrel->do_index_vacuuming)
	{
		Assert(!vacrel->do_index_cleanup);
		dead_items_reset(vacrel);
		return;
	}
```

它把 `dead_items` 里收集到的 TID,**交给每个索引的 AM**(B 树的 `ambulkdelete` 等)去删掉指向这些 TID 的索引项。注释把这个动作说成"deleting index tuples that point to LP_DEAD items left in heap pages"。

> **为什么必须"先清索引、再清堆",顺序不能反**:这是 lazy VACUUM 注释里反复强调的一条**铁律**——"no extant index tuple can ever be allowed to contain a TID that points to an LP_UNUSED line pointer in the heap"。意思是:**只要索引里还有一个项指向某个 TID,堆里那个 TID 对应的行指针就绝不能被标成 `LP_UNUSED`(完全回收)**。为什么?因为如果堆里的行指针是 `LP_UNUSED`,PG 可能会把这个槽位**复用**——塞进一个全新的 tuple。这时索引里那个旧项(还指向这个 TID)就会**误指到新 tuple 上**,索引扫描会读到错误数据。所以正确的顺序是:**先把索引里所有指向死元组的项删干净,确保没有任何索引项再指向这些 TID,然后才敢把堆里的行指针标成 `LP_UNUSED`**。这就是为什么 lazy VACUUM 要走**两趟**(initial pass 清索引 + final pass 清堆)的原因。

### 第 5 件:清堆——把 LP_DEAD 行指针标成 LP_UNUSED

清完索引后,`lazy_vacuum_heap_rel`(第二趟扫堆)把 `dead_items` 里那些 TID 对应的行指针**从 `LP_DEAD` 标成 `LP_UNUSED`**:

> [src/backend/access/heap/vacuumlazy.c:2084-2101](../postgresql-17.0/src/backend/access/heap/vacuumlazy.c#L2084-L2101)

```c
/*
 * This routine marks LP_DEAD items in vacrel->dead_items as LP_UNUSED. ...
 */
static void
lazy_vacuum_heap_rel(LVRelState *vacrel)
{
	BlockNumber vacuumed_pages = 0;
	Buffer		vmbuffer = InvalidBuffer;
	LVSavedErrInfo saved_err_info;
	TidStoreIter *iter;
	TidStoreIterResult *iter_result;
```

它遍历 `dead_items`,对每个 TID,读进对应的堆页,把行指针改成 `LP_UNUSED`,顺手把页里的空闲空间腾出来(可能压缩页内 tuple 位置)。标成 `LP_UNUSED` 后,这个槽位就可以被**新插入复用**——这就是"VACUUM 回收死元组空间"的物理含义:**它不删除页、不缩小表文件,只是把死元组占的槽位重新挂回"可分配"的池子,下次 INSERT 会优先用这些槽**。

> **关键认知:lazy VACUUM 不还空间给 OS**。一个表 VACUUM 完,操作系统层面看 `ls -l`,表文件大小**几乎不变**(可能尾部少量页被 truncate 掉)。回收的空间是"表内部可复用的空槽",留给将来的 INSERT 用。这是 lazy VACUUM 和 VACUUM FULL 最大的区别(下一节讲)。如果你 VACUUM 完发现表还是很大,那不是 VACUUM 没起作用,而是 lazy VACUUM 的设计就是这样——它优先"不锁表、不重排",代价是空间留在表内。

### 第 6 件:推进 relfrozenxid,可能截断 clog

六件事都做完,`heap_vacuum_rel` 收尾时,会**更新这张表在 `pg_class` 里的 `relfrozenxid`**——把它推进到"本次 VACUUM 冻结的最老事务号"。这是防回绕的关键:每张表的 `relfrozenxid` 推进一步,全库的 `oldest_datfrozenxid` 就能往前走一步。

`relfrozenxid` 的更新和 clog 截断是连在一起的。当一次 VACUUM 把全库最老的 `datfrozenxid` 往前推进了,系统可以**截断 clog**(commit log)里那些"对应事务号已经全部冻结、不再需要查"的旧页——这样 `pg_xact/` 目录不会无限增长。clog 截断由 `vac_truncate_clog` 完成:

> [src/backend/commands/vacuum.c:1766-1786](../postgresql-17.0/src/backend/commands/vacuum.c#L1766-L1786)

```c
/*
 *	vac_truncate_clog() -- attempt to truncate the commit log
 *
 *		Scan pg_database to determine the system-wide oldest datfrozenxid,
 *		and use it to truncate the transaction commit log (pg_xact).
 *		Also update the XID wrap limit info maintained by varsup.c.
 *		Likewise for datminmxid.
 *	...
 */
static void
vac_truncate_clog(TransactionId frozenXID,
				  MultiXactId minMulti,
				  TransactionId lastSaneFrozenXid,
				  MultiXactId lastSaneMinMulti)
{
	TransactionId nextXID = ReadNextTransactionId();
	...
```

它扫一遍 `pg_database` 找最老的 `datfrozenxid`,据此调 `TruncateCLOG`(`src/backend/access/transam/clog.c:1000`)把 `pg_xact/` 里不再需要的旧页删掉,同时更新 `varsup.c` 维护的回绕阈值信息(`SetTransactionIdLimit`)。**clog 截断是 VACUUM 的"长期副作用"——它让 `pg_xact/` 不无限膨胀,也让事务号空间的"已用部分"可以被遗忘**。

> **把六件事串起来**:lazy VACUUM 是一趟(实际是两趟)扫表,做六件事:① prune 找死元组、收集 LP_DEAD;② 冻结老元组(防回绕);③ 更新 VM(让下次更轻);④ 清索引(删指向死元组的项);⑤ 清堆(LP_DEAD → LP_UNUSED,空间可复用);⑥ 推进 `relfrozenxid`、可能截断 clog。前五件服务"清死元组"(防膨胀),第二、六件服务"防回绕"。**两笔账,一个 VACUUM 一起还**。

---

## 五、lazy VACUUM vs VACUUM FULL:代价的取舍

`VACUUM` 命令有个孪生兄弟 `VACUUM FULL`,两者干的事完全不同,代价也完全不同。这是 PG 运维最常被搞混的一对。

| | **lazy VACUUM**(就是普通的 `VACUUM`) | **VACUUM FULL** |
|---|---|---|
| 怎么做 | 扫表、标 LP_DEAD → LP_UNUSED,空间留在表内 | **整表重写**:建一张新表,把活元组拷过去,丢掉所有死元组 |
| 空间还给 OS 吗 | **不还**,留在表内供新 INSERT 复用 | **还**,新表紧凑,旧表文件被替换 |
| 锁表吗 | **不锁**(拿的是 cleanup lock,和正常读写可并发) | **全程 `AccessExclusiveLock`**,整表完全不能读写 |
| 重排 tuple 吗 | 不重排,行号(TID)基本不变 | **重排**,活元组被物理紧凑排列,TID 全变 |
| 什么时候用 | 日常清理,autovacuum 跑的就是这个 | 表严重膨胀、lazy VACUUM 救不回来时,偶尔手动跑一次 |
| 代价 | 轻但不还空间 | 重(锁表 + 全表拷贝),但能彻底回收空间 |

**关键差异**:lazy VACUUM 是**在线**操作(不挡正常读写),代价是**不还空间给 OS**——它只是让死元组占的槽位可被复用;VACUUM FULL 是**离线**操作(全程锁表),好处是**彻底重排、还空间给 OS**,代价是期间表完全不可用。

> **不这样会怎样(各自为什么这么设计)**:
> - **lazy VACUUM 为什么不重排不还空间**:因为重排要移动 tuple 的物理位置,这会改变它们的 TID——而索引、其他事务持有的快照,都依赖 TID 不变。要安全地重排,就得锁住整表(没人能插/查/改),这就退化成 VACUUM FULL。lazy VACUUM 优先"不挡读写",所以选择"标 LP_DEAD/LP_UNUSED、不移动 tuple"。代价是空间不还 OS,但换来了在线、低锁。
> - **VACUUM FULL 为什么锁表**:它要**物理重写整张表**(实际上走的是 `CLUSTER` 的代码路径,建新表、拷数据、改文件名),TID 全变,索引全部重建。这种规模的改动必须独占整表,否则并发会错乱。所以 VACUUM FULL 是"偶尔救命"的操作,不是日常清理。

> **运维直觉**:日常交给 autovacuum(跑的是 lazy VACUUM);只有当表膨胀到 lazy VACUUM 也救不回来(死元组太多、空间无法被新 INSERT 复用,比如某表只 DELETE 不 INSERT),才**偶尔、人工、在维护窗口**跑一次 `VACUUM FULL`。两者不是替代关系,是分工。

---

## 六、autovacuum:让 VACUUM 自己跑起来

讲清了 VACUUM 干什么,下一个问题:**谁来跑 VACUUM?** 如果全靠人,数据库迟早要出事(谁会记得天天 VACUUM?)。PG 的答案是 **autovacuum**——一个常驻的后台服务,自动决定"什么时候、对哪张表、跑一次 VACUUM"。

### autovacuum 的进程模型:launcher + worker

autovacuum 是 postmaster 启动时拉起的两个常驻进程:

- **AutoVacuumLauncher**([src/backend/postmaster/autovacuum.c:361](../postgresql-17.0/src/backend/postmaster/autovacuum.c#L361) `AutoVacuumLauncherMain`):**调度员**。它周期性(`autovacuum_naptime`,默认 1 分钟)醒来,扫一遍系统表找出"需要 VACUUM/ANALYZE 的表",把它们排进队列,然后 fork 一个个 worker 去处理。
- **AutoVacuumWorker**([src/backend/postmaster/autovacuum.c:1359](../postgresql-17.0/src/backend/postmaster/autovacuum.c#L1359) `AutoVacWorkerMain`):**执行者**。每个 worker 一次处理一张表,跑完向 launcher 汇报、领下一张,或退出。worker 数量有上限(`autovacuum_max_workers`,默认 3)。

worker 拿到任务后,调 `do_autovacuum`([autovacuum.c:1873](../postgresql-17.0/src/backend/postmaster/autovacuum.c#L1873))真正干活,它最终也走到 `heap_vacuum_rel`(和手动 VACUUM 同一条路)。

### autovacuum 怎么判断"这张表该不该 VACUUM":阈值公式

这是 autovacuum 最核心的逻辑,在 `relation_needs_vacanalyze`([autovacuum.c:2905](../postgresql-17.0/src/backend/postmaster/autovacuum.c#L2905))。它对每张表算两个阈值,和该表当前的"死元组数 / 新插入数"比较:

> [src/backend/postmaster/autovacuum.c:3034-3055](../postgresql-17.0/src/backend/postmaster/autovacuum.c#L3034-L3055)

```c
		vacthresh = (float4) vac_base_thresh + vac_scale_factor * reltuples;
		vacinsthresh = (float4) vac_ins_base_thresh + vac_ins_scale_factor * reltuples;
		anlthresh = (float4) anl_base_thresh + anl_scale_factor * reltuples;
		...
		/* Determine if this table needs vacuum or analyze. */
		*dovacuum = force_vacuum || (vactuples > vacthresh) ||
			(vac_ins_base_thresh >= 0 && instuples > vacinsthresh);
		*doanalyze = (anltuples > anlthresh);
```

公式翻译成人话:

```
vacthresh  = autovacuum_vacuum_threshold       + autovacuum_vacuum_scale_factor  × reltuples(表的估算行数)
vacinsthresh = autovacuum_vacuum_insert_threshold + autovacuum_vacuum_insert_scale_factor × reltuples
```

- `vactuples` = 该表自上次 VACUUM 以来**累积的死元组数**(DELETE/UPDATE 产生的)。
- `instuples` = 自上次 VACUUM 以来**新插入的行数**。
- 当 `vactuples > vacthresh`(死元组够多)**或** `instuples > vacinsthresh`(新插入够多)→ 对这张表跑 VACUUM。

**为什么是"基数 + 比例"的混合公式**:这是为了让大表和小表都用同一套参数自适应。默认值:

- `autovacuum_vacuum_threshold = 50`(基数:至少 50 个死元组才考虑)
- `autovacuum_vacuum_scale_factor = 0.2`(比例:表的 20%)

意思是:**一张表的死元组数超过 `50 + 20% × 行数` 时,autovacuum 就会启动**。对一张 1000 万行的大表,阈值是 `50 + 200 万 ≈ 200 万`死元组;对一张 100 行的小表,阈值就是 `50 + 20 = 70`。小表早点扫(防止小表死元组比例过高),大表晚点扫(避免频繁扫大表)。这个"基数 + 比例"的设计,让 DBA 用一组全局参数就能覆盖各种大小的表。

### 为什么不能手动关掉 autovacuum

答案在第二节已经讲过——**anti-wraparound 那一档不认配置**。看 `relation_needs_vacanalyze` 里这段:

> [src/backend/postmaster/autovacuum.c:3008-3014](../postgresql-17.0/src/backend/postmaster/autovacuum.c#L3008-L3014)

```c
	/* User disabled it in pg_class.reloptions?  (But ignore if at risk) */
	if (!av_enabled && !force_vacuum)
	{
		*doanalyze = false;
		*dovacuum = false;
		return;
	}
```

**只有在 `!force_vacuum`(表没有回绕风险)时,用户的 `autovacuum_enabled = false` 才生效**。一旦 `force_vacuum = true`(该表 `relfrozenxid` 老到逼近 `xidForceLimit`),autovacuum 会**无视任何"我关了 autovacuum"的设置,强制对它跑一次以冻结为主的 VACUUM**。

> 这就是为什么"装了 PG 就别关 autovacuum"是铁律——你可以嫌它慢、嫌它打扰业务,**关掉普通清理**,但你**关不掉防回绕清理**。而且关掉普通清理的后果(死元组堆积)最终会让回绕风险来得更快,逼出更多的 anti-wraparound VACUUM(那种是全表扫、更重)。所以**正确做法是调参数(降 scale_factor、加 worker、给 autovacuum 单独的 `autovacuum_work_mem`),而不是关它**。

### autovacuum 的代价与调优直觉

autovacuum 不是免费的,它扫表会占 I/O 和 CPU,会和高峰期业务抢资源。常见调优点:

- `autovacuum_vacuum_cost_limit` / `autovacuum_vacuum_cost_delay`:**限速**。autovacuum 默认是"低优先级后台任务",会主动 sleep 来让出资源(基于 cost-based vacuum delay)。但这也会导致 autovacuum 跑得太慢、跟不上死元组产生速度——这是大表最常见的运维痛点。
- `autovacuum_max_workers`:并发 worker 数(默认 3)。调大能同时扫更多表,但每个 worker 还是限速的,所以单纯加 worker 不一定有用。
- `autovacuum_naptime`:launcher 醒来周期(默认 1 分钟)。表越多,这个值要越小,才能让每张表都被及时检查。
- 对单张特别热的表,可以在 reloption 里覆盖全局参数:`ALTER TABLE hot_table SET (autovacuum_vacuum_scale_factor = 0.05);`——让这张表更早触发 VACUUM。

> **autovacuum 跟不上的典型症状**:监控里 `pg_stat_user_tables.n_dead_tup` 持续涨、表膨胀率(pgstattuple 看死元组比例)越来越高、日志里频繁出现 "autovacuum was interrupted" 或 "skipped vacuum due to lock"。这些都是 autovacuum 跑得不够快的信号,需要调参数或补手动 VACUUM。

---

## 七、单页即时清理与 HOT:VACUUM 的"前哨"

到这里你可能会问:每次清死元组都得等 VACUUM(或 autovacuum)扫整张表?那一张高频更新的表,死元组岂不是会先堆积、再被周期性清掉,**中间总有垃圾滞留**?PG 有两套"前哨"机制,在 VACUUM 之前就**即时、局部**地清一部分死元组。

### page-level prune:读写时顺手清一页

PG 在正常的 `SELECT` / `UPDATE` / `DELETE` 访问一个堆页时,如果"启发式地判断这页该清理了",会**顺手做一次页内清理(prune)**。入口是 `heap_page_prune_opt`:

> [src/backend/access/heap/pruneheap.c:180-246](../postgresql-17.0/src/backend/access/heap/pruneheap.c#L180-L246)

```c
/*
 * Optionally prune and repair fragmentation in the specified page.
 *
 * This is an opportunistic function.  It will perform housekeeping
 * only if the page heuristically looks like a candidate for pruning and we
 * can acquire buffer cleanup lock without blocking.
 * ...
 */
void
heap_page_prune_opt(Relation relation, Buffer buffer)
{
	Page		page = BufferGetPage(buffer);
	TransactionId prune_xid;
	GlobalVisState *vistest;
	Size		minfree;

	/*
	 * First check whether there's any chance there's something to prune,
	 * determining the appropriate horizon is a waste if there's no prune_xid
	 * (i.e. no updates/deletes left potentially dead tuples around).
	 */
	prune_xid = ((PageHeader) page)->pd_prune_xid;
	if (!TransactionIdIsValid(prune_xid))
		return;

	/*
	 * Check whether prune_xid indicates that there may be dead rows that can
	 * be cleaned up.
	 */
	vistest = GlobalVisTestFor(relation);

	if (!GlobalVisTestIsRemovableXid(vistest, prune_xid))
		return;
	...
	if (PageIsFull(page) || PageGetHeapFreeSpace(page) < minfree)
	{
		/* OK, try to get exclusive buffer lock */
		if (!ConditionalLockBufferForCleanup(buffer))
			return;
```

注意几个细节,它们体现了 prune 的设计哲学:

1. **`pd_prune_xid`**:页头有个字段,记着"这页上一次产生死元组时的事务号"。如果它无效(这页从没产生过死元组),立刻 return——**绝大多数页这一步就退出了**,prune 的开销极低。
2. **`GlobalVisTestIsRemovableXid`**:判断"这页上 `prune_xid` 对应的死元组,现在是不是对所有活事务都可删了"。如果还有老事务可能需要读它们,return,不动。
3. **`ConditionalLockBufferForCleanup`**:用**非阻塞**的方式拿 cleanup lock——拿不到就立刻 return,**绝不阻塞正常业务**。prune 是"机会主义"的,只在"刚好能干、又不打扰别人"时才干。
4. **触发条件**:`PageIsFull` 或空闲空间低于 fillfactor——也就是这页**快满了、需要腾地方**时才 prune(典型场景:一次 UPDATE 想在这页写新版本,但页满了,先 prune 腾地方)。

prune 干的活,最终调到 `heap_page_prune_and_freeze`([pruneheap.c:350](../postgresql-17.0/src/backend/access/heap/pruneheap.c#L350))——**和 VACUUM 调的是同一个函数**。也就是说,**page prune 和 VACUUM 在底层是同一套清理逻辑**,区别只在"谁触发、什么时候触发、清多少"。prune 是"读/写路过时顺手清一页",VACUUM 是"系统性地扫整张表"。

> prune 清完一页的死元组后,会把它们的行指针**标成 `LP_DEAD` 或 `LP_REDIRECT`**(指向链上的下一个版本,保留索引能找到的逻辑链),甚至直接 `LP_UNUSED`——但**直接 LP_UNUSED 只在没有索引、或这页的所有索引项已被前面清过时才安全**(回看第四节的铁律)。这就是为什么 prune 后页里可能留下 `LP_DEAD`(等 VACUUM 来配合清索引后再变 `LP_UNUSED`)。

### HOT update:UPDATE 不用更新索引的秘密

讲 prune 不能不讲 **HOT(Heap-Only Tuple)** 更新——它是 PG 对高频 UPDATE 的最大优化,和 prune 是一对搭档。

**问题**:一次普通的 `UPDATE`,MVCC 下要写新版本 + 给索引加新项(B 树里插一条 `(key, 新TID)`)。如果这张表有 5 个索引,一次 UPDATE 就要更新 5 个索引——索引更新是 UPDATE 的大头开销,而且索引项的 INSERT 又会留下死索引项(等 VACUUM 清)。

**HOT 的条件**:如果一个 UPDATE 满足两个条件——① **没有改任何索引列**(更新的列都不在索引里);② 新版本能塞进**旧版本所在的同一个页**(或相邻页)——PG 就走 HOT 路径:**只更新堆,不动任何索引**。

```text
普通 UPDATE(非 HOT):                   HOT UPDATE:
                                        (条件:没改索引列 + 新版本在同一页)
索引 (key, TID_new) ← 要插            索引(不动)              (key, TID_old) 保留
       │                                  │
       ▼                                  ▼
  堆页: 旧版本(TID_old, xmax=X)         堆页: 旧版本(TID_old, xmax=X)
         新版本(TID_new, xmin=X)               └─LP_REDIRECT─► 新版本(TID_old_slot?不,新槽, xmin=X)
                                              (旧槽 redirect 到新槽,索引还指旧槽就能顺藤摸到新版本)
```

HOT 的妙处:

- **索引不动**:索引还指向旧 TID。旧版本的行指针被标成 `LP_REDIRECT`(或旧版本本身保留),指向同一页里的新版本。索引扫描顺着旧 TID → redirect → 找到新版本,**逻辑上完全等价,但省了所有索引更新**。
- **死元组可以被 prune 立即清**:因为 HOT 更新的死元组**没有任何索引指向它**(索引指向的是旧 TID,旧 TID 是 redirect 不是 tuple 本身),prune 清掉这种死元组时**不需要先清索引**——它可以直接把 redirect 链压缩、把死元组的槽位回收。这就是 `heap_page_prune_opt` 能"即时清理"的关键场景:**HOT 死元组不依赖 VACUUM 清索引,prune 一趟搞定**。

> **HOT 和 prune 的协同**:HOT 让 UPDATE 不产生索引死项、死元组局限在页内;prune 让这些 HOT 死元组在页级别被即时清理。两者合起来,让一张"高频 UPDATE 且没改索引列"的表,**死元组基本不堆积,autovacuum 压力大减**。这是 PG 高写入场景调优的核心:尽量让 UPDATE 走 HOT(别改索引列、把 `fillfactor` 调低一点给新版本留页内空间)。

---

## 八、关键源码精读:`heap_vacuum_rel` 的入口与收尾

把前面几节串起来,精读 `heap_vacuum_rel` 的入口(看它怎么准备)和收尾(看它怎么推进 `relfrozenxid`),这是单表 VACUUM 的总骨架。

入口 `heap_vacuum_rel` 准备阶段做的事(节选):

> [src/backend/access/heap/vacuumlazy.c:294-313](../postgresql-17.0/src/backend/access/heap/vacuumlazy.c#L294-L313)

```c
void
heap_vacuum_rel(Relation rel, VacuumParams *params,
				BufferAccessStrategy bstrategy)
{
	LVRelState *vacrel;
	bool		verbose,
				instrument,
				skipwithvm,
				frozenxid_updated,
				minmulti_updated;
	BlockNumber orig_rel_pages,
				new_rel_pages,
				new_rel_allvisible;
	PGRUsage	ru0;
	TimestampTz starttime = 0;
	PgStat_Counter startreadtime = 0,
				startwritetime = 0;
	WalUsage	startwalusage = pgWalUsage;
	BufferUsage startbufferusage = pgBufferUsage;
	ErrorContextCallback errcallback;
	char	  **indnames = NULL;

	verbose = (params->options & VACOPT_VERBOSE) != 0;
```

注意几个字段:

- **`skipwithvm`**:这个布尔变量名就揭示了 VACUUM 的核心优化——**"能不能靠 VM 跳过整页"**(`withvm` = with visibility map)。后面 `lazy_scan_heap` 里每扫到一页,先查 VM 的 `ALL_VISIBLE` 位,如果是 1、且不是 anti-wraparound 扫描,直接跳过这页(死元组肯定没有、不需要冻——因为 `ALL_FROZEN` 也蕴含了)。
- **`frozenxid_updated` / `minmulti_updated`**:VACUUM 收尾时要判断"这次有没有成功推进 `relfrozenxid`"——只有冻了新的事务,这两个布尔才会置真,后面据此决定要不要触发 clog 截断。

`heap_vacuum_rel` 把活儿交给 `lazy_scan_heap`,自己收尾时处理两件大事:**表尾部 truncate**(如果末尾有一串全空页,可以把它们从文件里砍掉,这部分空间是会还 OS 的——lazy VACUUM 唯一会还空间的地方)和**更新 `pg_class.relfrozenxid`**(推进冻结水位线)。

> **为什么要更新 `relfrozenxid`**:这是 VACUUM 防回绕的"成果交付"。第二节讲过,系统靠"全库最老的 `relfrozenxid`"(也就是 `oldest_datfrozenxid`)和 `recentXid` 的距离来判断回绕风险。**每张表 VACUUM 一次,如果冻了新事务,就把自己的 `relfrozenxid` 往前推**;全库所有表里最老的那个,就是 `datfrozenxid`。`vac_update_datfrozenxid`([vacuum.c:1565](../postgresql-17.0/src/backend/commands/vacuum.c#L1565))负责扫一遍 `pg_database` 重算 `datfrozenxid`,并决定要不要调 `vac_truncate_clog` 截断 clog。**这条链:`heap_vacuum_rel` 推 `relfrozenxid` → `vac_update_datfrozenxid` 推 `datfrozenxid` → `vac_truncate_clog` 截 clog + 更新回绕阈值**,就是 VACUUM 防回绕的完整因果链。

最后,把 `lazy_scan_heap` 的两趟结构画成一张图,这是理解 lazy VACUUM 全流程的钥匙:

```text
              lazy_scan_heap (扫整张表,一趟)
                       │
   ┌───────────────────┼────────────────────┐
   ▼                   ▼                    ▼
 每页:                 每页:                 每页:
 ① heap_page_prune_   ② 若 dead_items 满    (VM 是 ALL_VISIBLE?
    and_freeze          (超 maintenance_       是 → 整页跳过,不读堆)
   - prune 死元组       work_mem),中途          (ALL_FROZEN 也跳过
   - 标 LP_DEAD           调 lazy_vacuum:       anti-wraparound)
   - freeze 老元组       ④ 清索引(删指向      这是 VACUUM 越跑越轻
   - 收集 dead_items      LP_DEAD 的索引项)     的关键
                          ⑤ 清堆(LP_DEAD→
   └────────────┬─────     LP_UNUSED),但只清
                │           本趟已扫的页
                │
   ┌────────────┘ (一趟扫完后,如果 dead_items 非空)
   ▼
 ④ lazy_vacuum_all_indexes  ← 删所有 dead_items 对应的索引项
   (一表多索引,可并行;parallel vacuum)
                       │
                       ▼
 ⑤ lazy_vacuum_heap_rel (第二趟,只扫有过 LP_DEAD 的页)
   把 dead_items 里的 TID 对应行指针 → LP_UNUSED
   (顺序铁律:先 ④ 清索引,再 ⑤ 清堆,
    保证没有索引项指向 LP_UNUSED 槽位)
                       │
                       ▼
 heap_vacuum_rel 收尾:
   - 表尾 truncate(空页砍掉,这部分空间还 OS)
   - 更新 pg_class.relfrozenxid / relminmxid
   - (跨表) vac_update_datfrozenxid → 可能 vac_truncate_clog
```

这张图把第四节的六件事、第五节 lazy vs FULL 的"不还空间"细节(只有尾部空页还 OS)、第六节 VM 跳页、第七节 prune/HOT 都串到了一起。读 `vacuumlazy.c` 时,把每个函数对应到图里的某个框,就不会迷路。

---

## 章末小结

### 用一段话回顾本章

VACUUM 是 MVCC 的内置收尾,它解决第 17 章留下的两笔长期账。**第一笔——死元组堆积**:MVCC 让 UPDATE/DELETE 不就地改、保留旧版本,这些旧版本(死元组)白占空间,导致表膨胀、查询变慢、磁盘爆满。**第二笔——事务号回绕**:PG 的事务号是 32 位(约 42 亿),会用完、会回绕,如果不把老元组的 `t_xmin` 冻结成 `FrozenTransactionId`、推进 `relfrozenxid`,回绕后可见性判定全错乱、数据"凭空消失"——这是 PG 最怕的灾难。两笔账都由 VACUUM 还:一次 lazy VACUUM 扫表,做六件事——prune 识别死元组、冻结老元组、更新 VM、清索引(删指向死元组的项)、清堆(LP_DEAD→LP_UNUSED,空间留表内供复用)、推进 `relfrozenxid`(可能截断 clog)。lazy VACUUM 不锁表、不还空间给 OS(只有尾部空页还),优先在线;VACUUM FULL 则全程锁表、整表重写、彻底还空间,是偶尔救命的离线操作。autovacuum(launcher + worker)按"基数+比例"阈值公式(`threshold + scale_factor × reltuples`)自动决定该不该扫某表;它的 anti-wraparound 模式**不认任何手动关闭配置**——表有回绕风险时强制冻结扫描,这是 PG 写进内核的"防自杀条款"。最后,page-level prune 和 HOT update 是 VACUUM 的前哨:prune 在正常读写路过时机会主义地清单页死元组,HOT 让没改索引列的 UPDATE 不产生索引死项、死元组局限页内能被 prune 即时清——两者让高频 UPDATE 表的死元组基本不堆积。

### 四件最该记住的事

1. **VACUUM 是 MVCC 的代价,不是可选运维**:PG 选择"简单写路径 + 延迟清理"(对比 InnoDB 的 undo 链即时清理),代价是必须定期 VACUUM。从不 VACUUM 的两笔账:慢(膨胀)和停(wraparound)。
2. **XID wraparound 是 PG 最怕的灾难**:32 位事务号会回绕,不冻结老元组会让数据"消失"。PG 用"四道闸(xidVacLimit/Warn/Stop/WrapLimit)+ 强制只读 + anti-wraparound VACUUM 不认配置"三层防线挡住它。**装了 PG 就别关 autovacuum**。
3. **lazy VACUUM 不删数据、不还空间**:它把死元组的行指针标成 `LP_UNUSED` 让新 INSERT 复用,表文件大小基本不变;唯一还 OS 的是表尾的空页 truncate。想彻底回收空间,得 VACUUM FULL(锁表、重写)。
4. **VACUUM 必须先清索引再清堆**:因为"没有任何索引项可以指向 LP_UNUSED 行指针",否则索引会误指到复用后的新 tuple。这条铁律让 lazy VACUUM 走两趟(initial + final)。

### 回扣主线:VACUUM 还的是 MVCC 欠的账

本章服务的是全书主线里"**让数据不丢不乱**"那一侧,而且专还 P4 第 17 章欠下的债:

- **回扣三个本性**:VACUUM 治的是第二条"**数据要共享,但共享会冲突**"的副作用。MVCC 用多版本化解"读写冲突",代价是旧版本(死元组)堆积——这是"共享"逼出的复杂性在存储层的延续。VACUUM 是 MVCC 这套设计的**闭环**:没有 VACUUM,MVCC 用不了几次就会把数据库拖垮;有了 VACUUM,MVCC 才能长期、可持续地运转。
- **回扣"快 vs 不丢不乱"二分法**:VACUUM 同时服务两侧——它回收死元组(让查询不读垃圾、让 INSERT 有空间可用,这是"快");它冻结老元组、推进 `relfrozenxid`(防数据因回绕而损坏,这是"不丢不乱")。**MVCC 用"留旧版本"换来了"读写不阻塞"的快,这个快是有代价的——代价就是死元组和回绕这两笔账,VACUUM 来还**。这正是数据库设计的辩证法:每个"快"都对应某个角落的"账",MVCC 的账记在死元组里,VACUUM 是还账的会计。
- **回扣存储与事务的咬合**:第 9 章(VM)、第 7 章(tuple/行指针状态)、第 17 章(xmin/xmax/冻结)在本章全部汇合——VM 的两位让 VACUUM 跳页、`LP_DEAD`/`LP_UNUSED` 是 VACUUM 操作的物理对象、冻结操作的是 tuple 的 `t_infomask`、防回绕推进的是 `relfrozenxid`。VACUUM 是把存储层(P2)和事务层(P4)的所有零件拧到一起的"运维枢纽",理解它就理解了 PG 内核几个子系统怎么协同。

### 想继续深入

- **lazy VACUUM 全家桶**:[src/backend/access/heap/vacuumlazy.c](../postgresql-17.0/src/backend/access/heap/vacuumlazy.c)。入口 `heap_vacuum_rel`(L294),workhorse `lazy_scan_heap`(L815,顶部注释 L780-L814 是 lazy VACUUM 全流程的总纲,强烈建议精读),清索引 `lazy_vacuum`(L1858,含"bypass 优化"——死元组极少时跳过索引清理),清堆 `lazy_vacuum_heap_rel`(L2100),VM 跳页判断 `heap_page_is_all_visible`(L2952),死元组收集 `dead_items_add`(L2882)。
- **命令入口与跨表收尾**:[src/backend/commands/vacuum.c](../postgresql-17.0/src/backend/commands/vacuum.c)。`ExecVacuum`(L147)、`vacuum`(L478,看它为什么不能在事务块里跑 L491-L498)、`vac_update_datfrozenxid`(L1565,推进 `datfrozenxid`)、`vac_truncate_clog`(L1782,截断 clog + 更新回绕阈值)。
- **autovacuum 全家桶**:[src/backend/postmaster/autovacuum.c](../postgresql-17.0/src/backend/postmaster/autovacuum.c)。launcher `AutoVacLauncherMain`(L361)、worker `AutoVacWorkerMain`(L1359)、`do_autovacuum`(L1873)、判定函数 `relation_needs_vacanalyze`(L2905,阈值公式 L3034-L3055、anti-wraparound 的 `force_vacuum` 在 L2989-L3014)。文件开头有大量注释解释进程模型和锁。
- **page prune 与 HOT**:[src/backend/access/heap/pruneheap.c](../postgresql-17.0/src/backend/access/heap/pruneheap.c)。机会主义清理入口 `heap_page_prune_opt`(L193,看它怎么"快速退出"和"非阻塞拿 cleanup lock")、真正的清理 `heap_page_prune_and_freeze`(L350,VACUUM 和 prune 共用)。HOT 的判定和写入在 [src/backend/access/heap/heapam.c](../postgresql-17.0/src/backend/access/heap/heapam.c) 的 `heap_update` 里(搜 `HEAP_HOT_UPDATED`、`HeapTupleSetHot`)。
- **冻结机制**:[src/backend/access/heap/heapam.c](../postgresql-17.0/src/backend/access/heap/heapam.c) 的 `heap_prepare_freeze_tuple`(L6550,逐字段判定"要不要冻、能不能冻")、`heap_freeze_tuple`(L6922)。冻结的 hint bit `HEAP_XMIN_FROZEN` 见 [src/include/access/htup_details.h:206](../postgresql-17.0/src/include/access/htup_details.h#L206)(第 17 章讲过它的"两 bit 同置"编码)。
- **事务号回绕的四道闸**:[src/backend/access/transam/varsup.c](../postgresql-17.0/src/backend/access/transam/varsup.c) 的 `GetNewTransactionId`(L77,闸门 L123-L167)、`SetTransactionIdLimit`(L372,算 `xidVacLimit`/`xidStopLimit`/`xidWrapLimit` L389-L406);clog 截断 [src/backend/access/transam/clog.c:1000](../postgresql-17.0/src/backend/access/transam/clog.c#L1000) `TruncateCLOG`。
- **VM 与跳页**:回看[第 9 章](P2-09-FSM与VM-两个辅助地图.md)。VM 设位 `visibilitymap_set`([visibilitymap.c:244](../postgresql-17.0/src/backend/access/heap/visibilitymap.c#L244),第 9 章讲过为什么设位要写 WAL)。

---

> 本章把 MVCC 的两笔账(死元组 + 回绕)和 VACUUM 怎么还账讲完了——PG 单机内部的数据,现在能做到"自洽、不膨胀、不因回绕而损坏"。但**单机本身是单点**:这台机器的磁盘一旦损坏、这台机器一旦整体下线,上面所有数据(无论 VACUUM 维护得多干净)都跟着没了。VACUUM 解决的是"单机数据的长期健康",解决不了"单机本身的不可靠"。要让数据在**整机故障**下也不丢,PG 的答案是**复制(replication)**——把数据实时抄一份(或多份)到别的机器上。翻开 **P6 第 22 章 · 复制与高可用**,看 PG 怎么用流复制把 WAL(第 18 章)源源不断地送到备库,实现主从一致和故障切换;以及为什么"复制 + VACUUM + WAL"三者合起来,才是"让数据不丢"的完整答案。
