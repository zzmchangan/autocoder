# 第 19 章 · Checkpoint:日志与数据的同步点

> **前置**:你需要先读过 [第 18 章《WAL》](P5-18-WAL-预写日志-掉电不丢的命根子.md)——那一章立下铁律"先记日志后改数据",并讲清了 `COMMIT` 只等日志 fsync、脏页可以安心留在内存里脏着。可问题立刻来了:**脏页能脏到什么时候?WAL 段文件能一直滚下去吗?** 这一章回答这个追问——什么时候、由谁把脏页安全地落盘,并把"落盘到哪一刻"在 WAL 里打一个锚点,这个锚点就是 **checkpoint(检查点)**。

> **核心问题**:为什么数据库必须周期性地做 checkpoint?它到底干了哪几件事、为什么干这些事就足以让"崩溃后恢复"既快又对?checkpoint 本身是个把所有脏页刷盘的"重活",怎么避免它一启动就把磁盘 I/O 打满、把查询拖卡?checkpoint 的节奏(多久一次、刷多快)怎么调,调偏了各有什么代价?
>
> **读完本章你会明白**:① checkpoint 为何是"WAL 与数据的同步锚点"——它决定了崩溃后要从 WAL 的哪个位置开始重放(redo 起点为什么必须存进 checkpoint 记录);② 一次 checkpoint 在源码里的完整生命周期(定 redo 点 → 刷所有脏页 → fsync 数据文件 → 写一条 checkpoint WAL 记录并 fsync → 更新 ControlFile → 截断/回收旧 WAL 段);③ spread checkpoint(配合 `checkpoint_completion_target` 默认 0.9)怎么把刷脏页摊到整个时间窗里、避免 I/O 尖峰;④ bgwriter 和 checkpointer 怎么分工把脏页平滑落盘;⑤ 调 `checkpoint_timeout` / `max_wal_size` 的代价权衡。

> **如果一读觉得太难**:先记三件事——① checkpoint 的核心动作是"把所有脏页刷盘 + fsync 数据文件 + 写一条 checkpoint WAL 记录";② 它在 WAL 里标了一个 redo 起点,崩溃后只需重放这个起点之后的 WAL;③ checkpoint 是重活,所以要在 `checkpoint_timeout` 这段时间内匀速做(spread checkpoint),别一次性打满磁盘。其余细节第二遍配合源码再抠。

---

## 一、为什么必须有 checkpoint:两个非做不可的理由

第 18 章讲完 WAL,你可能会想:既然"先记日志后改数据"已经保证掉电不丢,那脏页就一直攒在内存里、WAL 就一直往后写不就行了?——不行。有两个硬约束逼着数据库必须周期性地做 checkpoint。

### 理由一:WAL 不能无限增长,得有个"安全截断点"

WAL 是滚动追加的日志,每个改动都往里塞一条记录。第 18 章说过它切成 16MB 一段管理。问题来了:**这些段文件能无限堆下去吗?**

> **不这样会怎样(WAL 不截断)**:假设 WAL 永远只追加、不回收。后果——① 磁盘很快被 `pg_wal/` 目录撑爆,数据库直接停摆;② 复制(流复制、归档)要追赶的日志越来越多,新搭的备库可能要传几百 GB 才能追上;③ 备份体积爆炸。所以 WAL 必须能被回收或删除——**但只有当某段 WAL 记录的所有改动,对应的数据页都已经落盘了,这段 WAL 才真正"过期"、可以删**。否则删早了,掉电后这些改动的依据就没了,redo 时缺数据。

谁来划定"哪些 WAL 已经安全可删"?就是 checkpoint。一次 checkpoint 完成,意味着"此刻之前产生的所有脏页都已经刷盘"。那么,从这次 checkpoint 的 redo 起点之前的 WAL,理论上就不再需要用于崩溃恢复了。这就是 checkpoint 的第一个使命:**给 WAL 一个安全截断边界**。

### 理由二:崩溃恢复要重放的 WAL 不能太多,得有个"重放起点"

第 18 章埋了一个钩子:"掉电后重放日志就能补回"。但**重放多少**?如果从头重放数据库诞生以来的全部 WAL,那是几个 TB、几天的活——数据库崩溃重启一次要好几天才能开门营业,这显然不可接受。

> **不这样会怎样(没有重放起点)**:假如数据库每次崩溃后都要从最早的 WAL 重放,那数据库运行越久,崩溃恢复越慢——上线一年的库,重启可能要几天。这是不可接受的。正确做法是:周期性地把内存里的脏页**批量刷盘**,刷完之后在 WAL 里记一条"**到这里为止,数据页都已经落盘了**"。下次崩溃重启,只要重放这条记录之后的 WAL 就够了——之前的脏页已经在磁盘上是新版本,不用再 redo。

这条"到这里为止数据已落盘"的记录,就是 **checkpoint WAL 记录**;它指向的位置叫 **redo 起点(redo point)**。这就是 checkpoint 的第二个使命:**缩短崩溃恢复要重放的 WAL 量**。

> 两个理由合起来看,checkpoint 的本质就清楚了:**它是一个把"内存里的脏(易失数据)"批量兑现成"磁盘上的持久数据"的动作,完成后在 WAL 里打一个锚点,既界定了"WAL 可截断到哪",又界定了"崩溃恢复从哪重放"。** 没有 checkpoint,数据库的"易失"本性就治不干净——日志会爆炸、恢复会无限久。

---

## 二、checkpoint 到底干了哪几件事:把锚点打牢

现在讲机制。一次 online checkpoint(非 shutdown)在源码里是 `CreateCheckPoint` 干的([src/backend/access/transam/xlog.c:6856](../postgresql-17.0/src/backend/access/transam/xlog.c#L6856))。它大体按下面这个顺序做六件事,每一件都对应"为什么要做 checkpoint"里的某个理由。

### 第一件:定 redo 起点,并存进 checkpoint 记录

这是 checkpoint 最关键的语义动作。redo 起点决定了"崩溃后从哪重放"。在线(非 shutdown)checkpoint 里,PG 用一条特殊的 WAL 记录 `XLOG_CHECKPOINT_REDO` 来标记 redo 起点:

> [src/backend/access/transam/xlog.c:7023-7037](../postgresql-17.0/src/backend/access/transam/xlog.c#L7023-L7037)

```c
	if (!shutdown)
	{
		/* Include WAL level in record for WAL summarizer's benefit. */
		XLogBeginInsert();
		XLogRegisterData((char *) &wal_level, sizeof(wal_level));
		(void) XLogInsert(RM_XLOG_ID, XLOG_CHECKPOINT_REDO);

		/*
		 * XLogInsertRecord will have updated XLogCtl->Insert.RedoRecPtr in
		 * shared memory and RedoRecPtr in backend-local memory, but we need
		 * to copy that into the record that will be inserted when the
		 * checkpoint is complete.
		 */
		checkPoint.redo = RedoRecPtr;
	}
```

`checkPoint.redo` 就是 redo 起点的 LSN。它会被存进最终的 checkpoint 记录里(见 `CheckPoint` 结构体的第一个字段):

> [src/include/catalog/pg_control.h:35-43](../postgresql-17.0/src/include/catalog/pg_control.h#L35-L43)

```c
typedef struct CheckPoint
{
	XLogRecPtr	redo;			/* next RecPtr available when we began to
								 * create CheckPoint (i.e. REDO start point) */
	TimeLineID	ThisTimeLineID; /* current TLI */
	TimeLineID	PrevTimeLineID; /* previous TLI, if this record begins a new
								 * timeline (equals ThisTimeLineID otherwise) */
	bool		fullPageWrites; /* current full_page_writes */
	int			wal_level;		/* current wal_level */
	FullTransactionId nextXid;	/* next free transaction ID */
	Oid			nextOid;		/* next free OID */
	...
```

注意结构体里还存了 `nextXid`(下一个事务号)、`nextOid`、`oldestXid` 等一堆"事务系统当前状态"。为什么?因为崩溃恢复不只是重放数据,还要把这些全局计数器恢复到一致状态——重启后从这里继续发号,不会重号。

> **为什么 redo 起点必须存进 checkpoint 记录本身?** 因为崩溃恢复启动时,数据库要找"最近一次成功的 checkpoint 记录",从它读出 `redo` 字段,才知道从哪个 LSN 开始重放。这个 redo 指针就是 checkpoint 记录的"身份证信息"。下一章(崩溃恢复)会看到,ControlFile 里记着最近 checkpoint 记录的位置,启动时定位到它、读出 redo,就开始重放。

### 第二件:等"关键事务"出危险区,然后刷所有脏页

redo 点定好之后,接下来要把内存里所有脏页都刷到磁盘。但刷之前有个细节:必须先等某些正在进行的事务"脱离危险区"。看源码:

> [src/backend/access/transam/xlog.c:7097-7145](../postgresql-17.0/src/backend/access/transam/xlog.c#L7097-L7145)(节选)

```c
	/*
	 * One example is end of transaction, so we must wait for any transactions
	 * that are currently in commit critical sections.  If an xact inserted
	 * its commit record into XLOG just before the REDO point, then a crash
	 * restart from the REDO point would not replay that record, which means
	 * that our flushing had better include the xact's update of pg_xact.  So
	 * we wait till he's out of his commit critical section before proceeding.
	 */
	vxids = GetVirtualXIDsDelayingChkpt(&nvxids, DELAY_CHKPT_START);
	if (nvxids > 0)
	{
		do
		{
			/*
			 * Keep absorbing fsync requests while we wait. There could even
			 * be a deadlock if we don't, if the process that prevents the
			 * checkpoint is trying to add a request to the queue.
			 */
			AbsorbSyncRequests();
			...
		} while (HaveVirtualXIDsDelayingChkpt(vxids, nvxids,
											  DELAY_CHKPT_START));
	}
```

这段注释把"为什么要等"讲得非常清楚:一个事务可能把它的 commit record 插进 WAL、恰好就在 redo 点之前——这种事务,崩溃重放时不会重放它的 commit record,所以它的"提交生效"必须靠 checkpoint 把它对 `pg_xact`(提交状态位图)的改动一起刷盘来保证。如果 checkpoint 抢先刷了脏页、却漏掉了它正在写的关键页,就会破坏一致性。所以 checkpoint 要等这些事务走出它们的"提交临界区"。

> 顺带注意注释里的另一句:"Keep absorbing fsync requests while we wait"——等待期间还要不停地吸收 backend 转发来的 fsync 请求,否则可能死锁(那个阻塞 checkpoint 的事务正想往 fsync 队列里加请求,而队列被 checkpoint 占着)。这是数据库内核里典型的"等的时候别闲着、顺手把活儿接了,避免死锁"的处理。

等完之后,真正刷脏页的入口是 `CheckPointGuts`,它会刷 SLRU(pg_xact、pg_multixact 等小表)、再刷主 buffer pool:

> [src/backend/access/transam/xlog.c:7483-7497](../postgresql-17.0/src/backend/access/transam/xlog.c#L7483-L7497)

```c
	/* Write out all dirty data in SLRUs and the main buffer pool */
	TRACE_POSTGRESQL_BUFFER_CHECKPOINT_START(flags);
	CheckpointStats.ckpt_write_t = GetCurrentTimestamp();
	CheckPointCLOG();
	CheckPointCommitTs();
	CheckPointSUBTRANS();
	CheckPointMultiXact();
	CheckPointPredicate();
	CheckPointBuffers(flags);

	/* Perform all queued up fsyncs */
	TRACE_POSTGRESQL_BUFFER_CHECKPOINT_SYNC_START();
	CheckpointStats.ckpt_sync_t = GetCurrentTimestamp();
	ProcessSyncRequests();
	CheckpointStats.ckpt_sync_end_t = GetCurrentTimestamp();
```

注意这里把 checkpoint 明确分成了**两个阶段**:`write` 阶段(`CheckPointBuffers` 把脏页 `write()` 出去)和 `sync` 阶段(`ProcessSyncRequests` 做 `fsync()`)。`LogCheckpointEnd` 打的日志里也是分开统计 `write` 和 `sync` 耗时的([xlog.c:6664-6668](../postgresql-17.0/src/backend/access/transam/xlog.c#L6664-L6668))。这个两段式划分非常重要,下面第五节会讲为什么。

### 第三件:fsync 所有数据文件(光 write 不够)

这是新手最容易忽略、却致命的一步。`CheckPointBuffers` 里 `BufferSync` 把脏页 `write()` 出去了,但这只是把数据交给了**操作系统的页缓存(OS page cache)**,**还没有真正落盘**。掉电的话 OS 缓存里的数据照样丢。

> **不这样会怎样(只 write 不 fsync)**:假设 checkpoint 只 `write` 脏页、不做 `fsync`。表面上看脏页都"写出去"了,可它们其实还在 OS 的内存里。这一刻掉电,OS 缓存清空,这些脏页的改动**全没了**——而 checkpoint 记录却已经写进 WAL 并落盘了,redo 起点已经推进。结果:崩溃恢复从新的 redo 点重放,可 redo 点之前本该"已经落盘"的脏页其实根本没落盘,数据就错了。checkpoint 的承诺("redo 点之前的数据都已在磁盘上")被打破,一致性崩溃。

所以 checkpoint 必须在 write 之后,对**所有这次写出的数据文件**做 `fsync`,把 OS 缓存里的数据真正冲到磁盘。这就是上面 `ProcessSyncRequests` 干的事([src/backend/storage/sync/sync.c:286](../postgresql-17.0/src/backend/storage/sync/sync.c#L286))。它把这次 checkpoint 期间累积的 fsync 请求(哪个文件要 fsync)队列遍历一遍,逐个 `fsync`:

> [src/backend/storage/sync/sync.c:384-397](../postgresql-17.0/src/backend/storage/sync/sync.c#L384-L397)

```c
		if (enableFsync)
		{
			/*
			 * If in checkpointer, we want to absorb pending requests every so
			 * often to prevent overflow of the fsync request queue.
			 */
			if (--absorb_counter <= 0)
			{
				AbsorbSyncRequests();
				absorb_counter = FSYNCS_PER_ABSORB;
			}
```

注意 `enableFsync` 这个判断——如果用户故意把 `fsync = off`(只为追求极致性能、接受掉电丢数据,比如某些批量导入场景),这里就跳过 fsync。这是 PG 给的"自找麻烦"开关,正常生产环境绝对不能关。

> **"先 write 再 fsync"这个两段式有什么好处?** write 是把数据交给 OS,OS 会用它的 I/O 调度器把大量零散的写**合并、排序**后批量下发——比每个文件写完立刻 fsync(强制每次都等磁盘真写完)高效得多。攒一大批 write 出去,最后统一 fsync,是把"随机写"摊平、利用 OS 调度能力的常见手法。代价是 fsync 阶段会有一段集中的"等磁盘"时间。

### 第四件:写一条 checkpoint WAL 记录,并 fsync 它

脏页都 fsync 完了,数据真的在磁盘上了。这时才能写"checkpoint 完成"这条 WAL 记录:

> [src/backend/access/transam/xlog.c:7176-7185](../postgresql-17.0/src/backend/access/transam/xlog.c#L7176-L7185)

```c
	/*
	 * Now insert the checkpoint record into XLOG.
	 */
	XLogBeginInsert();
	XLogRegisterData((char *) (&checkPoint), sizeof(checkPoint));
	recptr = XLogInsert(RM_XLOG_ID,
						shutdown ? XLOG_CHECKPOINT_SHUTDOWN :
						XLOG_CHECKPOINT_ONLINE);

	XLogFlush(recptr);
```

这条记录里塞的是前面填好的 `checkPoint` 结构体(含 redo 起点等)。注意紧接着的 `XLogFlush(recptr)`——**checkpoint 记录本身也必须立刻 fsync 落盘**,不能只留在 WAL 缓冲里。为什么?因为这条记录就是"崩溃恢复的入口":重启时第一个要找的就是它。如果它没落盘,checkpoint 等于白做了。

### 第五件:更新 ControlFile,记下"最近 checkpoint 在哪"

checkpoint 记录写完并落盘后,要把"这条 checkpoint 记录的位置"记进 `pg_control` 文件(ControlFile)。这样下次启动才能找到它:

> [src/backend/access/transam/xlog.c:7219-7236](../postgresql-17.0/src/backend/access/transam/xlog.c#L7219-L7236)

```c
	LWLockAcquire(ControlFileLock, LW_EXCLUSIVE);
	if (shutdown)
		ControlFile->state = DB_SHUTDOWNED;
	ControlFile->checkPoint = ProcLastRecPtr;
	ControlFile->checkPointCopy = checkPoint;
	/* crash recovery should always recover to the end of WAL */
	ControlFile->minRecoveryPoint = InvalidXLogRecPtr;
	ControlFile->minRecoveryPointTLI = 0;
	...
	UpdateControlFile();
	LWLockRelease(ControlFileLock);
```

`ControlFile->checkPoint` 存的是"最近一次 checkpoint 记录的 LSN",`checkPointCopy` 存的是那条记录的完整内容(含 redo 起点)。这两个字段定义在:

> [src/include/catalog/pg_control.h:133-135](../postgresql-17.0/src/include/catalog/pg_control.h#L133-L135)

```c
	XLogRecPtr	checkPoint;		/* last check point record ptr */
	XLogRecPtr	prevCheckPoint; /* previous check point record ptr */
	CheckPoint	checkPointCopy; /* copy of last check point record */
```

> 这里有个值得注意的细节:`UpdateControlFile()` 写 `pg_control` 文件时,PG 用的是"写临时文件 + rename 原子替换"的方式,保证 `pg_control` 文件**永远不会写一半损坏**——因为崩溃恢复启动时第一件事就是读它,它坏了数据库就起不来了。这种"关键元数据用原子替换写"的手法,在数据库内核里反复出现。

### 第六件:截断/回收旧 WAL 段

到这一步,checkpoint 在语义上已经完成了("redo 点之前的数据已落盘 + 锚点已记录")。最后是清扫工作:把不再需要的旧 WAL 段删掉或回收复用。

> [src/backend/access/transam/xlog.c:7284-7299](../postgresql-17.0/src/backend/access/transam/xlog.c#L7284-L7299)

```c
	/*
	 * Delete old log files, those no longer needed for last checkpoint to
	 * prevent the disk holding the xlog from growing full.
	 */
	XLByteToSeg(RedoRecPtr, _logSegNo, wal_segment_size);
	KeepLogSeg(recptr, &_logSegNo);
	if (InvalidateObsoleteReplicationSlots(RS_INVAL_WAL_REMOVED,
										   _logSegNo, InvalidOid,
										   InvalidTransactionId))
	{
		/*
		 * Some slots have been invalidated; recalculate the old-segment
		 * horizon, starting again from RedoRecPtr.
		 */
		XLByteToSeg(RedoRecPtr, _logSegNo, wal_segment_size);
		KeepLogSeg(recptr, &_logSegNo);
	}
	_logSegNo--;
	RemoveOldXlogFiles(_logSegNo, RedoRecPtr, recptr,
					   checkPoint.ThisTimeLineID);
```

`KeepLogSeg` 算出"最早还要保留到哪个段号"——它要同时考虑 redo 起点、复制槽位置(`wal_keep_size`)、WAL 摘要进度等,因为只要还有任何消费者(崩溃恢复、备库、归档)可能用到某段 WAL,这段就不能删。`KeepLogSeg` 的逻辑([xlog.c:7918-7982](../postgresql-17.0/src/backend/access/transam/xlog.c#L7918-L7982))就是把这几个"保留边界"取最小值(取最保守的)。算完之后,`RemoveOldXlogFiles` 把比这个段号更老的段处理掉。

> **不能回收 checkpoint 之后还可能用到的 WAL 段**——这是铁律。`KeepLogSeg` 的存在就是为了守住这条底线:它从"当前 WAL 写到哪(recptr)"倒推,考虑复制槽和 `wal_keep_size` 要留多少冗余,**绝不让删除边界越过 redo 起点**。一旦越过,崩溃恢复就找不到需要的 WAL 了。

被删的旧段不一定真删,常常是**回收复用**(`wal_recycle` 打开时):把旧段改名成未来要用的新段号,省掉重新分配文件的开销。看 `RemoveXlogFile`:

> [src/backend/access/transam/xlog.c:4020-4033](../postgresql-17.0/src/backend/access/transam/xlog.c#L4020-L4033)

```c
	if (wal_recycle &&
		*endlogSegNo <= recycleSegNo &&
		XLogCtl->InstallXLogFileSegmentActive &&	/* callee rechecks this */
		get_dirent_type(path, segment_de, false, DEBUG2) == PGFILETYPE_REG &&
		InstallXLogFileSegment(endlogSegNo, path,
							   true, recycleSegNo, insertTLI))
	{
		ereport(DEBUG2,
				(errmsg_internal("recycled write-ahead log file \"%s\"",
								 segname)));
		CheckpointStats.ckpt_segs_recycled++;
```

`recycleSegNo` 由 `XLOGfileslop` 算出([xlog.c:2240-2278](../postgresql-17.0/src/backend/access/transam/xlog.c#L2240-L2278)),它会预估"下个 checkpoint 大概还需要多少段",提前回收这么多段备用,既不让 `pg_wal/` 涨爆、也不至于每次都要临时现建文件。这个预估里有 `min_wal_size` / `max_wal_size` 作为上下限兜底。

> **小结这六件事**:定 redo 点 → 等危险事务 → 刷脏页(write)→ fsync 数据文件 → 写并 fsync checkpoint WAL 记录 → 更新 ControlFile → 截断/回收旧 WAL。每一步都有它对应的"不这样会怎样"。整个链条严丝合缝,核心目的就一个:**让"redo 点之前的数据确定在磁盘上"这句话成立,并在 WAL 里把这个锚点焊死。**

---

## 三、checkpoint 是个重活:怎么平滑它(spread checkpoint)

上面六件事里,最耗时的是"刷所有脏页 + fsync"。如果老老实实"一个 checkpoint 启动,就立刻把所有脏页一口气刷完",那每次 checkpoint 都是一次 **I/O 风暴**——磁盘被打满、所有查询卡顿,用户体验直接崩。这种"硬刷"叫 **tight checkpoint**。

> **不这样会怎样(tight checkpoint 的灾难)**:假设数据库设了 `checkpoint_timeout = 5min`,而且每次 checkpoint 都 tight 模式——一到点,瞬间把几万个脏页全 write + fsync。结果:① 这几秒到几分钟里,磁盘 I/O 被独占,正常查询的读写全排队等,响应时间飙升;② checkpoint 完成后,磁盘又回到空闲,形成"一阵尖峰一阵空闲"的锯齿形 I/O 模式,对存储硬件也不友好。生产环境里这种"周期性卡顿"是致命的。

PG 的解法叫 **spread checkpoint(铺开的检查点)**:不要一口气刷完,而是把刷脏页的活儿**摊到整个 checkpoint 时间窗里匀速做**。这是靠 `checkpoint_completion_target` 这个参数控制的。

### checkpoint_completion_target:把刷盘摊到时间窗的 90%

`checkpoint_completion_target` 默认 **0.9**,含义是:**checkpoint 应该在两次 checkpoint 之间这段时间的 90% 内完成刷盘**。比如 `checkpoint_timeout = 5min`,那 spread checkpoint 的目标是:用 5min × 0.9 = 4.5min 的时间匀速把脏页刷完,留 0.5min 余量。

这个"匀速"是怎么实现的?在 `BufferSync`(刷脏页的主循环)里,每刷完一批页,就调一次 `CheckpointWriteDelay`,它判断"现在刷的进度,是不是比时间进度快了":

> [src/backend/postmaster/checkpointer.c:711-754](../postgresql-17.0/src/backend/postmaster/checkpointer.c#L711-L754)(节选)

```c
void
CheckpointWriteDelay(int flags, double progress)
{
	...
	/*
	 * Perform the usual duties and take a nap, unless we're behind schedule,
	 * in which case we just try to catch up as quickly as possible.
	 */
	if (!(flags & CHECKPOINT_IMMEDIATE) &&
		!ShutdownRequestPending &&
		!ImmediateCheckpointRequested() &&
		IsCheckpointOnSchedule(progress))
	{
		...
		AbsorbSyncRequests();
		...
		WaitLatch(MyLatch, WL_LATCH_SET | WL_EXIT_ON_PM_DEATH | WL_TIMEOUT,
				  100,
				  WAIT_EVENT_CHECKPOINT_WRITE_DELAY);
		ResetLatch(MyLatch);
	}
```

逻辑是:如果当前刷盘进度(`progress`)已经领先于"按时间匀速该到的进度",就**睡 100ms 歇一歇**,让磁盘歇口气;如果落后了,就一秒不睡、拼命刷。`IsCheckpointOnSchedule` 里把进度乘上 `checkpoint_completion_target` 来比较:

> [src/backend/postmaster/checkpointer.c:789-790](../postgresql-17.0/src/backend/postmaster/checkpointer.c#L789-L790)

```c
	/* Scale progress according to checkpoint_completion_target. */
	progress *= CheckPointCompletionTarget;
```

这样,脏页的刷盘就被匀到了 `checkpoint_timeout × completion_target` 这段时间里,I/O 从"尖峰"变成了"平台"。

> **和 full_page_writes 的配合**:spread checkpoint 把刷盘摊长了,这有一个连带好处——它让 `checkpoint_timeout` 之间的间隔变长(因为刷得慢、不那么频繁触发),从而让第 18 章讲的 **full page image(FPI)**触发得更少。回忆第 18 章:FPI 是"checkpoint 之后第一次改某页时,记整页镜像防 torn page"。checkpoint 越稀疏,FPI 触发越少,WAL 体积越小。所以 spread checkpoint 不光平 I/O,还顺带省 WAL。这两个机制是配套设计的。

> `checkpoint_completion_target` 可以设到 1.0,意思是"刷到下一次 checkpoint 该启动的那一刻才完成"——理论上最平滑,但没有任何余量,一旦刷得稍慢就会"checkpoint 拖尾"(下一次 checkpoint 该启动了,上一次还没刷完,被迫延长)。所以默认 0.9 留 10% 余量,是个安全的折中。

---

## 四、谁在刷脏页:bgwriter 和 checkpointer 的分工

刷脏页这件事,PG 有两个常驻后台进程在做,分工不同。这一节把它们和 checkpoint 的关系理清。

### bgwriter:平时持续刷,给 checkpoint 铺路

**bgwriter(后台写进程)** 是一个一直在转的进程,它的活儿是:**趁系统不忙,提前把脏页刷出去**,让 buffer pool 里始终保持一定数量的干净页。为什么需要它?

> **不这样会怎样(没有 bgwriter)**:buffer pool 满了要淘汰一个脏 victim 时(第 8 章讲过),当前查询的 backend 得**自己**先把那个脏页刷干净才能换走——这就把"刷盘"这个慢操作塞进了查询的关键路径里,查询被无故拖慢。如果有个后台进程提前把脏页刷了,victim 大概率是干净的,backend 直接换走,查询不受影响。

bgwriter 的主循环很简单:周期性调 `BgBufferSync` 刷一批脏页,刷完看情况休眠:

> [src/backend/postmaster/bgwriter.c:221-238](../postgresql-17.0/src/backend/postmaster/bgwriter.c#L221-L238)

```c
	for (;;)
	{
		bool		can_hibernate;
		int			rc;

		/* Clear any already-pending wakeups */
		ResetLatch(MyLatch);

		HandleMainLoopInterrupts();

		/*
		 * Do one cycle of dirty-buffer writing.
		 */
		can_hibernate = BgBufferSync(&wb_context);

		/* Report pending statistics to the cumulative stats system */
		pgstat_report_bgwriter();
		pgstat_report_wal(true);
```

`BgBufferSync`([bufmgr.c:3176](../postgresql-17.0/src/backend/storage/buffer/bufmgr.c#L3176))的策略是:跟踪 Clock-Sweep 指针的推进速度(代表 buffer 分配的速率),据此估计"接下来大概要淘汰多少页",提前把它们刷干净。它刷的是**未来的 victim**——和 checkpoint "刷当前所有脏页"的目标不同,bgwriter 是"持续小批量地清场,让淘汰不卡"。

### checkpointer:到点了,强制刷全部

**checkpointer(检查点进程)** 平时也在转,但它干的是"等 checkpoint 触发、然后强制刷所有脏页"。它和 bgwriter 的关键区别是:**checkpointer 刷的是"那一刻存在的所有脏页"(为了 checkpoint 语义),bgwriter 刷的是"挑一部分脏页"(为了腾干净 victim)**。

checkpointer 的主循环在 `CheckpointerMain`([checkpointer.c:173](../postgresql-17.0/src/backend/postmaster/checkpointer.c#L173))。每个循环它做三件事:① 检查有没有 checkpoint 请求或超时到了;② 到了就调 `CreateCheckPoint`;③ 没到就睡到下一次该检查。看超时检查:

> [src/backend/postmaster/checkpointer.c:371-379](../postgresql-17.0/src/backend/postmaster/checkpointer.c#L371-L379)

```c
		now = (pg_time_t) time(NULL);
		elapsed_secs = now - last_checkpoint_time;
		if (elapsed_secs >= CheckPointTimeout)
		{
			if (!do_checkpoint)
				chkpt_or_rstpt_timed = true;
			do_checkpoint = true;
			flags |= CHECKPOINT_CAUSE_TIME;
		}
```

`CheckPointTimeout` 就是 `checkpoint_timeout` 这个 GUC 的值(默认 300 秒 = 5 分钟,见 [guc_tables.c:2850-2851](../postgresql-17.0/src/backend/utils/misc/guc_tables.c#L2850-L2851))。超时就置 `CHECKPOINT_CAUSE_TIME` 标记,表示"这次 checkpoint 是被时间触发的"。

触发后调 `CreateCheckPoint`:

> [src/backend/postmaster/checkpointer.c:462-466](../postgresql-17.0/src/backend/postmaster/checkpointer.c#L462-L466)

```c
			if (!do_restartpoint)
			{
				CreateCheckPoint(flags);
				ckpt_performed = true;
			}
```

> **三者的协作**:bgwriter 平时小批量刷脏页(服务"快",不让淘汰卡查询);checkpointer 到点强制刷全部脏页(服务"不丢",划定 redo 锚点);后端进程自己也会在淘汰脏 victim 时现刷(第 8 章)。这三者刷的都是同一批 buffer,靠 `BM_CHECKPOINT_NEEDED`、`BM_DIRTY` 这些标志位协调,不会重复刷同一个页。checkpoint 时 `BufferSync` 会先给所有该刷的脏页打上 `BM_CHECKPOINT_NEEDED` 标记(下面源码精读会看到),别的进程刷掉一个就清一个标记,checkpoint 主循环只刷还带着标记的——这样分工不撞车。

---

## 五、checkpoint 的节奏怎么调:两个旋钮的代价权衡

现在讲 DBA 最关心的:**checkpoint 多久一次、刷多快,怎么调才对**。这涉及两个核心 GUC:`checkpoint_timeout` 和 `max_wal_size`,以及已经讲过的 `checkpoint_completion_target`。

### 两个触发条件:时间到了,或 WAL 涨太多了

checkpoint 有两种触发方式,谁先到就谁触发:

1. **时间触发**:`checkpoint_timeout`(默认 5min)到了 → `CHECKPOINT_CAUSE_TIME`。就是上面 `CheckpointerMain` 里那段超时检查。
2. **WAL 量触发**:自上次 checkpoint 以来,新增的 WAL 超过了 `max_wal_size` 算出的阈值 → `CHECKPOINT_CAUSE_XLOG`。

WAL 量触发的逻辑在哪?在 WAL 写入路径里。每写满一个 WAL 段,`XLogWrite` 会检查"距离上次 checkpoint 的 redo 点,已经跨了多少段",超过阈值就请求一次 checkpoint:

> [src/backend/access/transam/xlog.c:2515-2526](../postgresql-17.0/src/backend/access/transam/xlog.c#L2515-L2526)

```c
				/*
				 * Request a checkpoint if we've consumed too much xlog since
				 * the last one.  For speed, we first check using the local
				 * copy of RedoRecPtr, which might be out of date; if it looks
				 * like a checkpoint is needed, forcibly update RedoRecPtr and
				 * recheck.
				 */
				if (IsUnderPostmaster && XLogCheckpointNeeded(openLogSegNo))
				{
					(void) GetRedoRecPtr();
					if (XLogCheckpointNeeded(openLogSegNo))
						RequestCheckpoint(CHECKPOINT_CAUSE_XLOG);
				}
```

`XLogCheckpointNeeded` 比较的是"当前段号"和"redo 点所在段号 + CheckPointSegments - 1":

> [src/backend/access/transam/xlog.c:2290-2299](../postgresql-17.0/src/backend/access/transam/xlog.c#L2290-L2299)

```c
bool
XLogCheckpointNeeded(XLogSegNo new_segno)
{
	XLogSegNo	old_segno;

	XLByteToSeg(RedoRecPtr, old_segno, wal_segment_size);

	if (new_segno >= old_segno + (uint64) (CheckPointSegments - 1))
		return true;
	return false;
}
```

`CheckPointSegments` 这个阈值由 `max_wal_size` 和 `checkpoint_completion_target` 算出:

> [src/backend/access/transam/xlog.c:2161-2188](../postgresql-17.0/src/backend/access/transam/xlog.c#L2161-L2188)(节选)

```c
	/*
	 * Calculate the distance at which to trigger a checkpoint, to avoid
	 * exceeding max_wal_size_mb. This is based on two assumptions:
	 *
	 * a) we keep WAL for only one checkpoint cycle ...
	 * b) during checkpoint, we consume checkpoint_completion_target *
	 *	  number of segments consumed between checkpoints.
	 */
	target = (double) ConvertToXSegs(max_wal_size_mb, wal_segment_size) /
		(1.0 + CheckPointCompletionTarget);

	/* round down */
	CheckPointSegments = (int) target;
```

注释讲得很明白:之所以除以 `(1 + target)`,是因为 checkpoint 期间(spread 时长 = target × 间隔)还会继续产生新 WAL,这部分要预留空间,否则就会超过 `max_wal_size`。所以"触发 checkpoint 的 WAL 距离"要比 `max_wal_size` 小一点,留出 spread 期间还会涨的部分。

> 默认值:`max_wal_size = 1024MB`(64 段 × 16MB,见 [guc_tables.c:2839](../postgresql-17.0/src/backend/utils/misc/guc_tables.c#L2839) 和 `DEFAULT_MAX_WAL_SEGS = 64` [xlog_internal.h:92](../postgresql-17.0/src/include/access/xlog_internal.h#L92)),`min_wal_size = 80MB`(5 段,`DEFAULT_MIN_WAL_SEGS = 5`)。

### 调偏了各有什么代价

这是个典型的权衡场景,没有绝对的最优值,看你怕什么:

**短间隔(`checkpoint_timeout` 小 / `max_wal_size` 小)**:

- 好处:崩溃恢复快——redo 点离现在近,要重放的 WAL 少。
- 坏处:① checkpoint 太频繁,I/O 抖动多(即使 spread,刷盘的总量没少,频繁刷 = 频繁占 I/O);② FPI 触发多(checkpoint 间隔短 → 每页"checkpoint 后首次改"的机会多 → 整页镜像记得多 → WAL 膨胀);③ WAL 段频繁 recycle。

如果 checkpoint 被 WAL 量频繁触发(而不是时间触发),PG 还会**警告**你:

> [src/backend/postmaster/checkpointer.c:437-445](../postgresql-17.0/src/backend/postmaster/checkpointer.c#L437-L445)

```c
		if (!do_restartpoint &&
			(flags & CHECKPOINT_CAUSE_XLOG) &&
			elapsed_secs < CheckPointWarning)
			ereport(LOG,
					(errmsg_plural("checkpoints are occurring too frequently (%d second apart)",
								   "checkpoints are occurring too frequently (%d seconds apart)",
								   elapsed_secs,
								   elapsed_secs),
					 errhint("Consider increasing the configuration parameter \"%s\".", "max_wal_size")));
```

`checkpoint_warning` 默认 30 秒([guc_tables.c:2865-2866](../postgresql-17.0/src/backend/utils/misc/guc_tables.c#L2865-L2866))——如果两次 checkpoint 间隔不到 30 秒,就提示"考虑调大 `max_wal_size`"。这条日志是调优的重要信号。

**长间隔(`checkpoint_timeout` 大 / `max_wal_size` 大)**:

- 好处:I/O 平稳,checkpoint 不频繁;FPI 少,WAL 紧凑。
- 坏处:① 崩溃恢复久——redo 点远,要重放的 WAL 多,重启开门慢;② WAL 堆积多(`pg_wal/` 占用大);③ 一次 checkpoint 要刷的脏页多,即使 spread 也可能压满 I/O。

> **一个典型的调优场景**:OLTP 高写入库,默认 `checkpoint_timeout=5min / max_wal_size=1GB`。观察到日志里频繁出现"checkpoints are occurring too frequently"——说明是 WAL 量在触发,不是时间。这时应该调大 `max_wal_size`(比如到 4GB、8GB),让时间触发成为主导(checkpoint 每 5min 一次,节奏稳定),而不是被写入量牵着鼻子走。同时把 `checkpoint_completion_target` 保持 0.9,确保刷盘平稳。这样代价是崩溃恢复要重放最多 5min × (1+0.9) 的 WAL,但换来了平稳的在线 I/O——对大多数生产库,这个权衡是值的。

> 反过来,如果你特别在意"崩溃恢复快"(比如对停机时间极敏感的库),可以把 `checkpoint_timeout` 调小到 1-2min。代价是更频繁的刷盘和更多 FPI。这是"恢复速度 vs 在线性能"的权衡,看你业务更怕哪个。

---

## 六、崩溃恢复要重放多少:从 redo 起点开始

把前面几节串起来,回答本章开头那个核心问题:"崩溃后要重放多少 WAL?"

答案是:**从最近一次成功 checkpoint 记录里的 `redo` 字段开始,重放到 WAL 末尾**。

为什么是这个范围?因为 checkpoint 完成时,redo 点之前的所有脏页都已经 write + fsync 落盘了(本章第二节的六件事保证了这点)。所以 redo 点之前的 WAL,即使不重放,磁盘上的数据也是新的、一致的。redo 点**之后**产生的脏页改动,才可能还没落盘(掉电时还脏在内存里),需要靠重放 redo 补回来。

这就是为什么 checkpoint 记录里**必须存 redo 起点的 LSN**([pg_control.h:37](../postgresql-17.0/src/include/catalog/pg_control.h#L37))——它是崩溃恢复的出发点,没有它,数据库不知道从哪开始重放。下一章(崩溃恢复)会详细讲启动时怎么定位这条 checkpoint 记录、怎么从 redo 点开始 replay。

> **checkpoint 越近,崩溃恢复越快**——这就是上一节"短间隔好处:恢复快"的根因。redo 点离崩溃时刻越近,要重放的 WAL 越少,重启开门越快。但这要用更频繁的 checkpoint(I/O 抖动)来换。整个 checkpoint 机制的设计,本质上就是在"崩溃恢复的速度"和"在线运行的平稳"之间找平衡点,而 `checkpoint_timeout` / `max_wal_size` / `checkpoint_completion_target` 这三个旋钮,就是给你调这个平衡的。

---

## 关键源码精读:BufferSync——checkpoint 怎么批量刷脏页

这一章最值得精读的是 `BufferSync`([src/backend/storage/buffer/bufmgr.c:2901-3163](../postgresql-17.0/src/backend/storage/buffer/bufmgr.c#L2901-L3163))——它把"checkpoint 要刷哪些脏页、按什么顺序刷、怎么和 spread checkpoint 配合"讲得清清楚楚。它分三大步。

**第一步:扫描全部 buffer,给该刷的脏页打标记**。

> [src/backend/storage/buffer/bufmgr.c:2941-2971](../postgresql-17.0/src/backend/storage/buffer/bufmgr.c#L2941-L2971)

```c
	num_to_scan = 0;
	for (buf_id = 0; buf_id < NBuffers; buf_id++)
	{
		BufferDesc *bufHdr = GetBufferDescriptor(buf_id);

		/*
		 * Header spinlock is enough to examine BM_DIRTY, see comment in
		 * SyncOneBuffer.
		 */
		buf_state = LockBufHdr(bufHdr);

		if ((buf_state & mask) == mask)
		{
			CkptSortItem *item;

			buf_state |= BM_CHECKPOINT_NEEDED;

			item = &CkptBufferIds[num_to_scan++];
			item->buf_id = buf_id;
			item->tsId = bufHdr->tag.spcOid;
			item->relNumber = BufTagGetRelNumber(&bufHdr->tag);
			item->forkNum = BufTagGetForkNum(&bufHdr->tag);
			item->blockNum = bufHdr->tag.blockNum;
		}

		UnlockBufHdr(bufHdr, buf_state);
```

读这段有几个关键点:

1. **`mask` 决定刷哪些**:默认 `mask = BM_DIRTY | BM_PERMANENT`(只刷永久表的脏页);shutdown 或 `CHECKPOINT_FLUSH_ALL` 时才刷所有脏页(包括临时表)。这是 `mask` 在函数开头设的([bufmgr.c:2913-2923](../postgresql-17.0/src/backend/storage/buffer/bufmgr.c#L2913-L2923))。
2. **打 `BM_CHECKPOINT_NEEDED` 标记**:这一步是"快照"——只刷此刻脏的页。注释([bufmgr.c:2930-2935](../postgresql-17.0/src/backend/storage/buffer/bufmgr.c#L2930-L2935))说得很清楚:"This allows us to write only those pages that were dirty when the checkpoint began, and not those that get dirtied while it proceeds."。checkpoint 进行中新脏的页,归下一次 checkpoint 管。
3. **存进 `CkptBufferIds` 数组**:把要刷的页按 `buf_id` 收集起来,后面要排序。注意存了 `tsId`(表空间)、`relNumber`、`forkNum`、`blockNum`——这是为了排序时按"哪个表空间、哪个文件、哪个块"排,把相邻的块排在一起刷,**减少随机 I/O**。

**第二步:排序,把相邻的脏页聚到一起刷**。

> [src/backend/storage/buffer/bufmgr.c:2987](../postgresql-17.0/src/backend/storage/buffer/bufmgr.c#L2987)

```c
	sort_checkpoint_bufferids(CkptBufferIds, num_to_scan);
```

按 `(tsId, relNumber, forkNum, blockNum)` 排序。刷的时候从同一个文件的连续块开始刷,磁盘磁头不用来回跑——这是把"随机写"转成"近似顺序写"的优化。同时这一步还为"跨表空间均衡写"做了准备(用最小堆在多个表空间间轮流刷,避免一个表空间的磁盘被写爆)。

**第三步:边刷边 throttle(配合 spread checkpoint)**。

> [src/backend/storage/buffer/bufmgr.c:3084-3144](../postgresql-17.0/src/backend/storage/buffer/bufmgr.c#L3084-L3144)(节选)

```c
	while (!binaryheap_empty(ts_heap))
	{
		BufferDesc *bufHdr = NULL;
		CkptTsStatus *ts_stat = (CkptTsStatus *)
			DatumGetPointer(binaryheap_first(ts_heap));

		buf_id = CkptBufferIds[ts_stat->index].buf_id;
		...
		if (pg_atomic_read_u32(&bufHdr->state) & BM_CHECKPOINT_NEEDED)
		{
			if (SyncOneBuffer(buf_id, false, &wb_context) & BUF_WRITTEN)
			{
				...
				PendingCheckpointerStats.buffers_written++;
				num_written++;
			}
		}
		...
		/*
		 * Sleep to throttle our I/O rate.
		 */
		CheckpointWriteDelay(flags, (double) num_processed / num_to_scan);
	}
```

读这段:

1. **用最小堆在表空间间均衡刷**:`binaryheap` 按各表空间的"已刷进度"排,每次从刷得最少的表空间取一个页刷——保证 I/O 负载在多个磁盘(表空间)间均匀分布,而不是把一个表空间写爆再写下一个。注释([bufmgr.c:3076-3081](../postgresql-17.0/src/backend/storage/buffer/bufmgr.c#L3076-L3081))专门讲了这点。
2. **检查 `BM_CHECKPOINT_NEEDED` 再刷**:虽然第一步打了标记,但刷之前还要再查一次——因为在这期间,bgwriter 或某个 backend 可能已经把这个页刷掉了(它们刷完会清这个标记)。这样不重复劳动。
3. **每刷一个调 `CheckpointWriteDelay`**:这就是 spread checkpoint 的落点。`progress = num_processed / num_to_scan` 是当前刷盘进度,`CheckpointWriteDelay` 拿它和"按时间匀速该到的进度"比,超前了就睡 100ms。**整段循环 + CheckpointWriteDelay,就是把 spread checkpoint 的节流逻辑焊死在刷脏页的主循环里**。

真正干刷盘的是 `SyncOneBuffer`([bufmgr.c:3475-3538](../postgresql-17.0/src/backend/storage/buffer/bufmgr.c#L3475-L3538)):pin 住页 → 加共享 content_lock → 调 `FlushBuffer`(就是第 8 章那个,里面会先 `XLogFlush` 保证日志先落盘)→ 放锁 unpin。注意它只 `write`,**不 fsync**——fsync 是后面 `ProcessSyncRequests` 统一做的(本章第二节第三件讲过)。

> **把 BufferSync 看成一个"收集 → 排序 → 均衡 + 节流刷"的流水线**,就能抓住 checkpoint 刷脏页的全貌:它不是无脑遍历 buffer 硬刷,而是精心安排了"刷哪些、按什么顺序、刷多快",每一步都在为"I/O 平稳"和"不漏刷"服务。

---

## 章末小结

- **checkpoint 是 WAL 与数据的同步锚点**:它把内存里的脏页批量兑现成磁盘上的持久数据,完成后在 WAL 里记一个 redo 起点。这个锚点同时界定两件事——WAL 可以安全截断到哪、崩溃恢复要从哪重放。
- **一次 checkpoint 干六件事**:定 redo 点 → 等危险事务出临界区 → 刷所有脏页(write)→ fsync 数据文件(否则 OS 缓存没真落盘)→ 写并 fsync 一条 checkpoint WAL 记录 → 更新 ControlFile → 截断/回收旧 WAL 段。每一步都对应一个"不这样会怎样"。
- **崩溃恢复从最近 checkpoint 的 redo 起点重放**——所以 checkpoint 记录里必须存 redo 指针。checkpoint 越近,恢复越快,但 I/O 越频繁。
- **spread checkpoint 把刷盘摊匀**:`checkpoint_completion_target`(默认 0.9)让刷盘在两次 checkpoint 间隔的 90% 时间内匀速进行,靠 `CheckpointWriteDelay` 每刷一批就判断进度、超前就睡,避免 I/O 尖峰。它还顺带让 checkpoint 间隔变长、FPI 触发更少。
- **bgwriter 和 checkpointer 分工**:bgwriter 平时小批量刷脏页(服务"快",让淘汰不卡查询);checkpointer 到点强制刷全部(服务"不丢",划定锚点);两者靠 `BM_CHECKPOINT_NEEDED` 等标志协调不撞车。
- **两个触发条件**:`checkpoint_timeout`(默认 5min,时间触发)或 `max_wal_size`(默认 1GB,WAL 量触发),谁先到谁触发。短间隔→恢复快但 I/O 抖、FPI 多;长间隔→I/O 平但恢复久、WAL 堆积。日志里"checkpoints are occurring too frequently"是调大 `max_wal_size` 的信号。

### 回扣主线

这一章是"**不丢不乱**"那一侧的关键一环,治的是"**易失**"这个本性里"脏页不能永远脏在内存"的那一半。

WAL(第 18 章)解决了"改动的依据不丢"(先记日志),但它留下一个尾巴:脏页总得落盘,不然 WAL 会无限涨、崩溃恢复会无限久。**checkpoint 就是收这个尾巴的动作**——它把 WAL 兜的"易失"风险,周期性地转化成磁盘上的确定性。它是 WAL 与数据落盘这两个动作的**同步点**:WAL 决定"先记什么",checkpoint 决定"记到哪可以安全地让数据追上来"。

至于数据的三个本性:本章主要服务"**易失**"(把内存脏页兑现成磁盘持久);同时 checkpoint 的 I/O 平滑(bgwriter/spread)又在服务"**磁盘慢**"(不能让刷盘打满 I/O 拖垮查询)——一只手治易失,另一只手顾磁盘慢,这正是 checkpoint 设计里最精巧的平衡。

### 想继续深入

- checkpoint 主流程:[src/backend/access/transam/xlog.c](../postgresql-17.0/src/backend/access/transam/xlog.c) 的 `CreateCheckPoint`([6856](../postgresql-17.0/src/backend/access/transam/xlog.c#L6856))、`CheckPointGuts`([7475](../postgresql-17.0/src/backend/access/transam/xlog.c#L7475))、`LogCheckpointStart/End`([6621/6653](../postgresql-17.0/src/backend/access/transam/xlog.c#L6621))、`KeepLogSeg`([7918](../postgresql-17.0/src/backend/access/transam/xlog.c#L7918))、`XLOGfileslop`([2240](../postgresql-17.0/src/backend/access/transam/xlog.c#L2240))、`XLogCheckpointNeeded`([2290](../postgresql-17.0/src/backend/access/transam/xlog.c#L2290))。
- checkpointer 进程:[src/backend/postmaster/checkpointer.c](../postgresql-17.0/src/backend/postmaster/checkpointer.c) 的 `CheckpointerMain`([173](../postgresql-17.0/src/backend/postmaster/checkpointer.c#L173))、`CheckpointWriteDelay`([711](../postgresql-17.0/src/backend/postmaster/checkpointer.c#L711))、`IsCheckpointOnSchedule`([779](../postgresql-17.0/src/backend/postmaster/checkpointer.c#L779))、`ForwardSyncRequest`([1093](../postgresql-17.0/src/backend/postmaster/checkpointer.c#L1093))、`AbsorbSyncRequests`([1264](../postgresql-17.0/src/backend/postmaster/checkpointer.c#L1264))。
- 刷脏页:[src/backend/storage/buffer/bufmgr.c](../postgresql-17.0/src/backend/storage/buffer/bufmgr.c) 的 `BufferSync`([2901](../postgresql-17.0/src/backend/storage/buffer/bufmgr.c#L2901))、`SyncOneBuffer`([3475](../postgresql-17.0/src/backend/storage/buffer/bufmgr.c#L3475))、`BgBufferSync`([3176](../postgresql-17.0/src/backend/storage/buffer/bufmgr.c#L3176))。
- fsync 队列:[src/backend/storage/sync/sync.c](../postgresql-17.0/src/backend/storage/sync/sync.c) 的 `SyncPreCheckpoint`([177](../postgresql-17.0/src/backend/storage/sync/sync.c#L177))、`SyncPostCheckpoint`([202](../postgresql-17.0/src/backend/storage/sync/sync.c#L202))、`ProcessSyncRequests`([286](../postgresql-17.0/src/backend/storage/sync/sync.c#L286))。
- GUC 与结构体:[guc_tables.c:2821-2879](../postgresql-17.0/src/backend/utils/misc/guc_tables.c#L2821-L2879)(checkpoint 族参数)、[pg_control.h:35-65](../postgresql-17.0/src/include/catalog/pg_control.h#L35-L65)(`CheckPoint`)、[pg_control.h:104-135](../postgresql-17.0/src/include/catalog/pg_control.h#L104-L135)(`ControlFileData`)。

---

> checkpoint 在 WAL 里焊下了一个 redo 起点,说"从这里开始重放就行"。可数据库真的崩溃了,重启时它是怎么找到这个起点、怎么挨条重放 WAL、怎么判断"重放到哪算完"的?翻开 **第 20 章 · 崩溃恢复:从日志重建一致性**——从 checkpoint 的 redo 起点出发,走一遍 PG 的 redo-only 恢复流程。
