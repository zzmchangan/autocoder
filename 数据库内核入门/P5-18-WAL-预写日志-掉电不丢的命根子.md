# 第 18 章 · WAL:预写日志,掉电不丢的命根子

> **前置**:你需要先读过[第 1 章《第一性原理》](P0-01-第一性原理-为什么需要数据库.md)(数据的"易失"本性)、[第 8 章《Buffer Pool》](P2-08-BufferPool-内存里的中转站.md)(脏页从哪里来——内存里的页改了之后,是"脏"的,迟早要落盘),以及 [第 14 章《事务与 ACID》](P4-14-事务与ACID-一组操作要么全做要么全不做.md)(那一章我们看到了 `RecordTransactionCommit` 写完 commit record 后调 `XLogFlush` 等落盘,这一步就是 `COMMIT` 在刷 WAL——可它到底在刷什么、为什么刷了这个就敢说"永久生效"?本章把这个问题彻底讲透)。

> **核心问题**:为什么"**先记日志,再改数据**"这个反直觉的顺序,能保证掉电也不丢?WAL 这套机制到底由哪些部件组成、各自防什么灾难?一条记录长什么样,凭什么它就能让数据库在崩溃后把改动"变回来"?

> **读完本章你会明白**:WAL 铁律(write-ahead)的真正落地(脏页刷盘前,它依赖的日志必须先落盘——靠 LSN 比较);一条 `XLogRecord` 怎么构造、写进 16MB 的段文件;**资源管理器(rmgr)** 怎么让不同对象的 redo 各归各家;**LSN** 怎么成为"这页的改动是否已在 WAL 里"的唯一判据;**full page image(FPI)** 怎么防磁盘的"部分写(torn page)";多 backend 怎么并发插日志;group commit 怎么把多次提交合并成一次 fsync;`COMMIT` 到底在等什么。

> **如果一读觉得太难**:先记三件事——① 先记日志再改数据,**只要日志落盘了,数据就不怕丢**(崩溃后靠重放日志补回);② WAL 切成 16MB 一段滚动管理;③ `COMMIT` 等的是"日志 fsync",这是持久性的唯一依据。

---

## 一、持久性的难题:内存易失,可你承诺了不掉

回到第 1 章立下的"易失"本性,以及第 8 章讲过的脏页:数据库改一页数据,改动**先发生在内存(Buffer Pool)里**,被改过的页叫**脏页(dirty page)**。内存一掉电就清空。可你已经向客户端承诺了"`COMMIT` 成功 = 永久生效,掉电不丢"(持久性 D,见第 14 章)。

这里有个根本矛盾:**数据实际被改的地方(内存)是易失的,但你承诺的是永久的**。怎么把这个矛盾解掉?数据库面前摆着几条路,我们先把它们都试一遍,看哪条走不通,就知道 WAL 为什么是唯一合理的解。

### 路线一:每次 COMMIT 都把改过的数据页立刻刷盘

> **不这样会怎样**:假设数据库老实地"每次 `COMMIT` 都把改过的数据页立刻刷到磁盘"。问题立刻冒出来——
>
> 1. **慢到没法用**:数据页是**随机写**(要改的页散落在文件各处,磁盘磁头要来回跑)。一个事务改 10 个页,就要 10 次随机磁盘写。随机写每次几毫秒,10 次 = 几十毫秒,`COMMIT` 慢到用户能感觉到卡顿。一个高并发 OLTP 库每秒几千上万次提交,这条路完全走不通。
> 2. **原子性难保**:为了原子性,你还得保证这 10 页"要么全刷成功、要么都不算",中途掉电还是可能半残——3 页刷出去了、7 页没刷。这时候数据库既不能当成"提交成功"(因为没刷全),也不能当成"没提交"(因为 3 页改动已经在磁盘上了),状态混乱。
> 3. **写放大**:每次提交都刷全量数据页,会让磁盘写量爆炸。

所以"每次提交都刷数据页"这条路,既慢又难保证原子。**这条路死。**

### 路线二:干脆不记日志,数据页脏着攒在内存,等 checkpoint 一起刷

> **不这样会怎样**:有人想偷懒——既然刷数据页慢,那就攒着,定期由 checkpoint 一起刷。可是,在 checkpoint 还没刷到那一刻,内存里的脏页还没落盘,如果这时**掉电**,这些脏页的改动全没了。更糟的是:你已经在内存里把改动告诉了用户("COMMIT 成功"),可掉电一来,这些"成功"的提交全部蒸发——**用户以为成功的转账,重启后全没了**。这就是直接违反持久性(D),是不可接受的灾难。

**这条路也死。**

### 路线三(正解):先把改动记进日志(WAL),日志落盘了,就敢承诺"永久生效"

WAL 给了一个聪明得多的解法——它**承认数据页可以脏着、可以慢慢刷,但加一道保险:在改数据页之前,先把这次改动记进一个日志文件,而且日志必须先落盘**。

为什么这条路能成?关键洞察是:

1. **日志是顺序写,快得飞起**。所有改动都往日志文件尾部追加,磁盘磁头不用来回跑,顺序写比随机写快几个数量级(下面第四节会看到日志还切成 16MB 段管理,进一步优化)。
2. **掉电后,靠重放(redo)日志就能把改动变回来**。哪怕数据页还没落盘,只要日志落了盘,重启后数据库重放一遍日志,把没刷盘的改动重新补到数据页上,数据就回来了。

> 这就把持久性解开了:`COMMIT` 不用等所有数据页刷盘,**只等这条事务的日志 fsync 到磁盘**。日志落盘后返回"成功",持久性兑现——掉电后 redo 会兜底。

这就是 WAL(Write-Ahead Logging)这个名字的含义:**Write-Ahead(预写)= 先写日志,后写数据**。

> [一个用完即弃的小比喻] 想象一个会记日记的会计:他每花一笔钱,都**先在账本上记一笔**(顺序写、快),然后再去改保险柜里的现金(随机拿、慢)。如果某天保险柜被偷了(掉电),他根本不慌——因为账本还在,**按账本重做一遍,现金就能复原**。WAL 就是数据库的那本"账本":数据页是那堆随时可能被偷的现金,日志是那本不怕偷的账本。这个比喻用一次就够,后面我们直接讲机制。

---

## 二、WAL 的核心铁律:先记日志,后改数据

WAL 的核心就一句话,PG 的源码注释把它写得明明白白:

> [src/backend/access/transam/xloginsert.c:471-474](../postgresql-17.0/src/backend/access/transam/xloginsert.c#L471-L474)

```c
 * This can be used as LSN for data pages affected by the logged action.
 * (LSN is the XLOG point up to which the XLOG must be flushed to disk
 * before the data page can be written out.  This implements the basic
 * WAL rule "write the log before the data".)
 */
XLogRecPtr
XLogInsert(RmgrId rmid, uint8 info)
```

**"write the log before the data"——先写日志,再写数据。** 这是 WAL 的铁律,也是它名字里 "Write-Ahead"(预写)的全部含义。

为什么这个顺序能保证持久性?回到第一节的三条洞察:

1. **日志是顺序追加**,快;
2. **`COMMIT` 只等日志 fsync**,不必等数据页;
3. **掉电后,重放日志补回数据**。

> 把第 14 章 `RecordTransactionCommit` 那段连起来:它写完 commit record(就是这条事务的 WAL 记录)后,调 `XLogFlush` 等日志落盘——**这一刻,持久性兑现**。`COMMIT` 返回成功后,数据页可能还脏在内存里没刷,但只要日志落盘了,数据库就敢承诺"永久生效"。

### 铁律的真正落地:靠 LSN 比较,保证"日志先于数据"

"先记日志后改数据"这句话,在代码里到底怎么强制?不能靠程序员自觉,得有机制。靠的是 **LSN(Log Sequence Number,日志序列号)** 和一个比较规则。

还记得第 6 章页头的 `pd_lsn` 吗?它在每个数据页的页头第一个字段:

> [src/include/storage/bufpage.h:155-162](../postgresql-17.0/src/include/storage/bufpage.h#L155-L162)

```c
typedef struct PageHeaderData
{
	/* XXX LSN is member of *any* block, not only page-organized ones */
	PageXLogRecPtr pd_lsn;		/* LSN: next byte after last byte of xlog
								 * record for last change to this page */
	uint16		pd_checksum;	/* checksum */
	uint16		pd_flags;		/* flag bits, see below */
	...
```

注释说得很直白:`pd_lsn` 是"这页最后一次改动对应的 WAL 记录的结尾位置"。而 `PageXLogRecPtr` 就是 `XLogRecPtr`:

> [src/include/access/xlogdefs.h:21](../postgresql-17.0/src/include/access/xlogdefs.h#L21)

```c
typedef uint64 XLogRecPtr;
```

`XLogRecPtr` 是一个 64 位整数,代表"日志流里的某个字节位置"。注释([xlogdefs.h:18-20](../postgresql-17.0/src/include/access/xlogdefs.h#L18-L20))特意说"These pointers are 64 bits wide, because we don't want them ever to overflow"——64 位保证数据库跑几百年都不会回绕。

**铁律的落地规则是**:

> **要把一个脏页刷到磁盘之前,必须先确保这个页的 `pd_lsn` 及其之前的 WAL 都已经落盘。**

换句话说:数据页的每个改动,都"挂着"一条对应的 WAL 记录(通过 `pd_lsn`);你想把这页落盘,得先把它依赖的那条日志落盘。否则掉电后,这页落盘了(可能是部分写的破损页,下面第五节讲)但对应的日志没有,redo 时就缺了这页的改动依据,数据就错了。

这个"日志先于数据"的强制,在刷脏页的代码里(`FlushBuffer`,见第 8 章)体现为:刷页前先 `XLogFlush(buffer->lsn)`,保证日志先落盘。这才是 write-ahead rule 在代码里的真身——**不是道德约束,而是一个 LSN 比较 + 一次 XLogFlush**。

> **回头看第一节的三条路**:WAL 选了第三条——承认数据页可以慢慢刷,但用"日志先落盘"这道保险兜底。`COMMIT` 只等日志 fsync,这是快和持久的完美平衡。代价是:数据库多了一套日志系统(本章全部内容都是这个代价),而且崩溃后要花时间重放(下一章 checkpoint 解决这个问题)。

---

## 三、一条 WAL 记录长什么样:`XLogRecord` 的解剖

铁律讲完了,我们下沉到具体的一条记录。WAL 里塞的是一条条记录(record),每条记录是"一个改动的说明书"。这条说明书长什么样?PG 用 `XLogRecord` 这个结构体描述:

> [src/include/access/xlogrecord.h:41-57](../postgresql-17.0/src/include/access/xlogrecord.h#L41-L57)

```c
typedef struct XLogRecord
{
	uint32		xl_tot_len;		/* total len of entire record */
	TransactionId xl_xid;		/* xact id */
	XLogRecPtr	xl_prev;		/* ptr to previous record in log */
	uint8		xl_info;		/* flag bits, see below */
	RmgrId		xl_rmid;		/* resource manager for this record */
	/* 2 bytes of padding here, initialize to zero */
	pg_crc32c	xl_crc;			/* CRC for this record */

	/* XLogRecordBlockHeaders and XLogRecordDataHeader follow, no padding */

} XLogRecord;
```

逐个字段拆:

- **`xl_tot_len`**:整条记录(包括头和后面的 payload)的总长度。redo 时靠它知道"下一条记录从哪开始"。
- **`xl_xid`**:产生这条记录的事务 ID。第 14 章讲过,崩溃恢复时数据库要靠"这个事务的 commit record 在不在"判断它提交没有,而 commit record 和数据改动 record 通过同一个 `xl_xid` 串起来。
- **`xl_prev`**:上一条记录的位置(LSN)。WAL 是链表,redo 时如果发现某条记录坏了(校验不过),可以靠 `xl_prev` 往回定位。
- **`xl_info`**:操作类型标志位(高 4 位给 rmgr 自由用,低 4 位是 XLog 系统的标志,见 [xlogrecord.h:62-63](../postgresql-17.0/src/include/access/xlogrecord.h#L62-L63))。同样是 `RM_HEAP_ID` 的记录,`xl_info` 区分它是 INSERT 还是 DELETE 还是 UPDATE。
- **`xl_rmid`**:**资源管理器 ID**——这条记录归谁管(redo 时由谁来重放)。这是关键字段,下一节专讲。
- **`xl_crc`**:整条记录的 CRC32C 校验。WAL 落盘后可能因为磁盘故障写坏,redo 时先校验 CRC,坏了就报错(防止用坏记录算出错误数据)。

记录头后面跟着的是 payload,分两类(注释里写了 "XLogRecordBlockHeaders and XLogRecordDataHeader follow"):

1. **`XLogRecordBlockHeader`**:描述"这条改动动了哪个块、动了它的哪部分"。看它的结构:

> [src/include/access/xlogrecord.h:103-114](../postgresql-17.0/src/include/access/xlogrecord.h#L103-L114)

```c
typedef struct XLogRecordBlockHeader
{
	uint8		id;				/* block reference ID */
	uint8		fork_flags;		/* fork within the relation, and flags */
	uint16		data_length;	/* number of payload bytes (not including page
								 * image) */

	/* If BKPBLOCK_HAS_IMAGE, an XLogRecordBlockImageHeader struct follows */
	/* If BKPBLOCK_SAME_REL is not set, a RelFileLocator follows */
	/* BlockNumber follows */
} XLogRecordBlockHeader;
```

一条记录可以引用多个块(`id` 是块引用号,0~32)。`fork_flags` 里塞着"哪个 fork(main/fsm/vm)、哪些标志位"。后面跟着的可能是"哪个关系文件 + 哪个块号"(如果和上一条引用的是同一个关系,用 `BKPBLOCK_SAME_REL` 省掉关系定位,见 [xlogrecord.h:201](../postgresql-17.0/src/include/access/xlogrecord.h#L201))。如果是 FPI(全页镜像),还会跟一个 `XLogRecordBlockImageHeader`(第五节详讲)。

2. **main data**:这条记录自带的 rmgr 专属数据(比如 heap insert 会塞"插入了哪些 tuple 的内容")。长短两种形式(`< 256` 用 short form)。

> 把整条记录读成一个故事:**"我(事务 xl_xid)对某个关系的某个块,做了 xl_info 这种操作,改动的细节如下(data),如果块要全页备份就附上 image,校验码 xl_crc 保证没写坏。"** redo 时,数据库读这条记录,按 `xl_rmid` 找到对应的资源管理器,把 data 和 block image 喂给它,它就能把改动重做一遍。

### 怎么构造一条记录:三阶段 API

知道了记录长什么样,再看怎么往里填内容。PG 提供三阶段 API(几乎所有写数据的代码路径都用这套):

```c
XLogBeginInsert();                                // ① 开始构造
XLogRegisterBuffer(...); / XLogRegisterData(...); // ② 登记改了哪些块/数据
XLogRecPtr lsn = XLogInsert(rmid, info);          // ③ 拼装写入,返回 LSN
```

第一步 `XLogBeginInsert` 打"开始构造"标记,带严格状态检查——不允许嵌套(一次只能构造一条记录):

> [src/backend/access/transam/xloginsert.c:148-163](../postgresql-17.0/src/backend/access/transam/xloginsert.c#L148-L163)

```c
void
XLogBeginInsert(void)
{
	Assert(max_registered_block_id == 0);
	Assert(mainrdata_last == (XLogRecData *) &mainrdata_head);
	Assert(mainrdata_len == 0);

	/* cross-check on whether we should be here or not */
	if (!XLogInsertAllowed())
		elog(ERROR, "cannot make new WAL entries during recovery");

	if (begininsert_called)
		elog(ERROR, "XLogBeginInsert was already called");

	begininsert_called = true;
}
```

注意 `if (!XLogInsertAllowed()) elog(ERROR, "cannot make new WAL entries during recovery")`——**崩溃恢复(redo)期间不允许产生新的 WAL**。这道理很简单:恢复时是在重放已有记录,不能再产生记录(否则循环)。这种"状态护栏"在数据库内核里随处可见。

第二步 `XLogRegisterBuffer` 登记"改了哪个 buffer",`XLogRegisterData` 登记"附带什么数据":

> [src/backend/access/transam/xloginsert.c:242-260](../postgresql-17.0/src/backend/access/transam/xloginsert.c#L242-L260)

```c
void
XLogRegisterBuffer(uint8 block_id, Buffer buffer, uint8 flags)
{
	registered_buffer *regbuf;

	/* NO_IMAGE doesn't make sense with FORCE_IMAGE */
	Assert(!((flags & REGBUF_FORCE_IMAGE) && (flags & (REGBUF_NO_IMAGE))));
	Assert(begininsert_called);

	/*
	 * Ordinarily, buffer should be exclusive-locked and marked dirty before
	 * we get here, otherwise we could end up violating one of the rules in
	 * access/transam/README.
	 ...
```

注释泄露了一条 WAL 规则:**buffer 在登记前必须先被排他锁住、并且已经标记为脏**。为什么?因为登记完之后,这条记录会带着"这个 buffer 的块号"落盘,redo 时会按这个块号去重放——如果登记时 buffer 还没脏(改动还没做),记录就名不副实了;如果没锁住,可能被别的事务并发改了又改,记录的语义就乱了。`flags` 里的 `REGBUF_FORCE_IMAGE`(强制全页镜像)和 `REGBUF_NO_IMAGE`(不要镜像)是给 FPI 用的开关,第五节会看到。

第三步 `XLogInsert` 真正拼装成 `XLogRecord` 写进 WAL 缓冲,返回 LSN(这条改动的"日志身份证",会写进数据页的 `pd_lsn`):

> [src/backend/access/transam/xloginsert.c:474-528](../postgresql-17.0/src/backend/access/transam/xloginsert.c#L474-L528)

```c
XLogRecPtr
XLogInsert(RmgrId rmid, uint8 info)
{
	XLogRecPtr	EndPos;
	...
	do
	{
		XLogRecPtr	RedoRecPtr;
		bool		doPageWrites;
		bool		topxid_included = false;
		XLogRecPtr	fpw_lsn;
		XLogRecData *rdt;
		int			num_fpi = 0;

		/*
		 * Get values needed to decide whether to do full-page writes. Since
		 * we don't yet have an insertion lock, these could change under us,
		 * but XLogInsertRecord will recheck them once it has a lock.
		 */
		GetFullPageWriteInfo(&RedoRecPtr, &doPageWrites);

		rdt = XLogRecordAssemble(rmid, info, RedoRecPtr, doPageWrites,
								 &fpw_lsn, &num_fpi, &topxid_included);

		EndPos = XLogInsertRecord(rdt, fpw_lsn, curinsert_flags, num_fpi,
								  topxid_included);
	} while (EndPos == InvalidXLogRecPtr);

	XLogResetInsertion();

	return EndPos;
}
```

`XLogInsert` 自己不直接拼装,它把活儿分给两个函数:**`XLogRecordAssemble`**(把登记的数据组装成完整的 `XLogRecord`,含决定要不要 FPI)和 **`XLogInsertRecord`**(把组装好的记录拷进 WAL 共享缓冲,占位置)。返回的 `EndPos` 就是这条记录的 LSN。

注意那个 `do { ... } while (EndPos == InvalidXLogRecPtr)` 循环——它处理一种竞态:在没拿到插入锁时读到的 `RedoRecPtr / doPageWrites`(决定 FPI)可能过期了,等拿到锁发现需要重做(可能要加 FPI),于是返回 `InvalidXLogRecPtr`,循环再来一次。这种"乐观读、拿到锁再复核"的模式在 WAL 路径里反复出现,是为了尽量缩短持锁时间。

---

## 四、资源管理器(rmgr):WAL 与崩溃恢复的接口

上一节看到每条记录都带一个 `xl_rmid`。这个字段是 **WAL 与崩溃恢复之间最重要的接口**,值得专讲。

WAL 是通用的日志机制——它能记录 heap(表)的插入、B 树的分裂、事务的提交、COMMIT TS 的更新……每一类对象的改动,redo 的方式都不一样(heap redo 要重建 tuple,B 树 redo 要调整索引页)。可 WAL 本身不关心"怎么 redo",它只负责"把记录存下来、按顺序读出来"。**"怎么 redo"这件事,委托给资源管理器(resource manager,简称 rmgr)**。

PG 把所有"会产生 WAL 记录的对象类型"都注册成一个 rmgr,每个 rmgr 在一张全局表 `RmgrTable` 里登记自己的处理函数:

> [src/include/access/xlog_internal.h:349-360](../postgresql-17.0/src/include/access/xlog_internal.h#L349-L360)

```c
typedef struct RmgrData
{
	const char *rm_name;
	void		(*rm_redo) (XLogReaderState *record);
	void		(*rm_desc) (StringInfo buf, XLogReaderState *record);
	const char *(*rm_identify) (uint8 info);
	void		(*rm_startup) (void);
	void		(*rm_cleanup) (void);
	void		(*rm_mask) (char *pagedata, BlockNumber blkno);
	void		(*rm_decode) (struct LogicalDecodingContext *ctx,
							  struct XLogRecordBuffer *buf);
} RmgrData;
```

读这个结构体,关键函数是 **`rm_redo`**——它就是"这条记录怎么重放"的实现。每个 rmgr 提供自己的 `rm_redo`:

- `RM_HEAP_ID`("Heap")的 `rm_redo` 是 `heap_redo`,负责重放 heap 表的 INSERT/DELETE/UPDATE;
- `RM_BTREE_ID`("Btree")的 `rm_redo` 是 `btree_redo`,负责重放 B 树的页面分裂;
- `RM_XACT_ID`("Transaction")的 `rm_redo` 是 `xact_redo`,负责重放 commit/abort record(标记事务的提交状态)。

完整的 rmgr 列表在 [src/include/access/rmgrlist.h](../postgresql-17.0/src/include/access/rmgrlist.h),每个条目是一行宏:

> [src/include/access/rmgrlist.h:31-43](../postgresql-17.0/src/include/access/rmgrlist.h#L31-L43)(节选)

```c
/* symbol name, textual name, redo, desc, identify, startup, cleanup, mask, decode */
PG_RMGR(RM_XLOG_ID, "XLOG", xlog_redo, xlog_desc, xlog_identify, NULL, NULL, NULL, xlog_decode)
PG_RMGR(RM_XACT_ID, "Transaction", xact_redo, xact_desc, xact_identify, NULL, NULL, NULL, xact_decode)
PG_RMGR(RM_SMGR_ID, "Storage", smgr_redo, smgr_desc, smgr_identify, NULL, NULL, NULL, NULL)
PG_RMGR(RM_CLOG_ID, "CLOG", clog_redo, clog_desc, clog_identify, NULL, NULL, NULL, NULL)
...
PG_RMGR(RM_HEAP_ID, "Heap", heap_redo, heap_desc, heap_identify, NULL, NULL, heap_mask, heap_decode)
PG_RMGR(RM_BTREE_ID, "Btree", btree_redo, btree_desc, btree_identify, btree_xlog_startup, btree_xlog_cleanup, btree_mask, NULL)
...
```

这张表通过宏展开,填进 `RmgrTable` 数组:

> [src/backend/access/transam/rmgr.c:47-51](../postgresql-17.0/src/backend/access/transam/rmgr.c#L47-L51)

```c
/* must be kept in sync with RmgrData definition in xlog_internal.h */
#define PG_RMGR(symname,name,redo,desc,identify,startup,cleanup,mask,decode) \
	{ name, redo, desc, identify, startup, cleanup, mask, decode },

RmgrData	RmgrTable[RM_MAX_ID + 1] = {
#include "access/rmgrlist.h"
};
```

崩溃恢复(redo)时,数据库从 WAL 读出一条记录,看它的 `xl_rmid`,然后调 `RmgrTable[rmid].rm_redo(record)`——**这就是"按 rmid 分发"**。每条记录归对应的 rmgr 重放,各管各的对象。这就是为什么 WAL 能通用地记录所有对象的改动:它不关心内容,只负责存和分发;真正的 redo 逻辑分散在各个 rmgr 里。

> **这是 WAL 与崩溃恢复的接口,要点出来**:本章只讲 WAL 怎么产生和存储记录;**"重放"的具体流程是第 20 章《崩溃恢复》的主角**。届时你会看到,崩溃恢复的主循环就是"读一条记录 → 按 `xl_rmid` 分发到 `rm_redo` → 这条记录的改动被重做"——如此循环,直到 WAL 末尾。`rm_redo` 函数指针就是连接这两章的桥。

顺带提一句:`RmgrData` 里除了 `rm_redo`,还有 `rm_desc`(把记录格式化成人能读的字符串,给 `pg_waldump` 工具用)、`rm_startup`/`rm_cleanup`(redo 开始/结束时的钩子,比如 B 树 redo 前要初始化一些状态)、`rm_mask`(掩盖掉页里"不参与校验"的字段,做一致性检查用)、`rm_decode`(逻辑解码用)。这张表是 PG 模块化的一个典范——加一类新对象,只要注册一个新 rmgr 即可。

---

## 五、LSN(日志序列号):"这页改动是否已在 WAL 里"的唯一判据

第二节我们用 `pd_lsn` 讲了 write-ahead rule 的落地,这里把 LSN 这个概念讲透,因为它在 WAL 体系里无处不在。

**LSN(Log Sequence Number,日志序列号)** 就是 `XLogRecPtr`——一个 64 位整数,代表"日志流里的某个字节位置"。它有两个用途:

### 用途一:WAL 记录的"身份证"

每条 WAL 记录插入时,会拿到一个唯一的 LSN(就是它在 WAL 流里的位置)。这个 LSN 是单调递增的——后插入的记录 LSN 一定更大。一条记录的 LSN 就是它的"地址",redo 时按 LSN 顺序读。

### 用途二:数据页的"进度戳"

数据页头的 `pd_lsn` 记录"这页最后一次改动对应的 WAL 记录的结尾 LSN"。这个字段在三个地方起作用,理解了这三处,LSN 的精髓就掌握了:

1. **write-ahead rule 的判据**(第二节讲过):要刷一个脏页,先 `XLogFlush(page->pd_lsn)`,保证这页的改动对应的日志已经落盘。
2. **FPI 触发的判据**(下一节详讲):`XLogRecordAssemble` 里判断要不要给某页加全页镜像,看的就是 `page_lsn <= RedoRecPtr`(这页自上次 checkpoint 后是否被改过)。
3. **redo 时的判据**(第 20 章详讲):崩溃恢复重放某条记录时,要先读它影响的页,比较 `page->pd_lsn` 和 `record->lsn`——如果 `page->pd_lsn >= record->lsn`,说明这页的改动已经在磁盘上了(比这条记录新),跳过这条 redo;否则才重放。这是 redo "不重复劳动"的关键。

> **一句话**:LSN 是 WAL 体系里的"时间线"。所有"哪个改动先、哪个改动后、哪个改动已经在盘上了"的问题,都靠 LSN 比较来回答。`pd_lsn` 是数据页挂在 WAL 时间线上的锚,redo、刷页、FPI 三件大事全靠它。

`RedoRecPtr` 是个相关的概念:它代表"当前 checkpoint 的 redo 起点"(下一章详讲)。每个 backend 在本地缓存一份 `RedoRecPtr`,判断 FPI 时用它。`GetRedoRecPtr` 从共享内存取最新值:

> [src/backend/access/transam/xlog.c:6409-6424](../postgresql-17.0/src/backend/access/transam/xlog.c#L6409-L6424)

```c
XLogRecPtr
GetRedoRecPtr(void)
{
	XLogRecPtr	ptr;

	/*
	 * The possibly not up-to-date copy in XlogCtl is enough. Even if we
	 * grabbed a WAL insertion lock to read the authoritative value in
	 * Insert->RedoRecPtr, someone might update it just after we've released
	 * the lock.
	 */
	SpinLockAcquire(&XLogCtl->info_lck);
	ptr = XLogCtl->RedoRecPtr;
	SpinLockRelease(&XLogCtl->info_lck);

	if (RedoRecPtr < ptr)
		RedoRecPtr = ptr;

	return RedoRecPtr;
}
```

注释点出一个微妙之处:取到的 `RedoRecPtr` 可能"刚拿到就过期了"——但这没关系,因为 FPI 的判断在真正拿锁插入时会复核一次(`XLogInsertRecord` 里)。这种"乐观读 + 持锁复核"是 WAL 路径的性能优化,避免每次判断 FPI 都去抢共享锁。

---

## 六、WAL 不是一个大文件,而是 16MB 一段的滚动日志

讲完记录的内部结构,看 WAL 在磁盘上的物理形态。WAL 日志在磁盘上不是无限长的一个文件,而是切成一段一段的**段文件(segment)**,每段默认 **16MB**:

> [src/include/pg_config_manual.h:20](../postgresql-17.0/src/include/pg_config_manual.h#L20)

```c
#define DEFAULT_XLOG_SEG_SIZE	(16*1024*1024)
```

文件名形如 `000000010000000000000001`、`000000010000000000000002`……(24 位十六进制,前 8 位是 timeline、中间 8 位是段高 32 位、后 8 位是段低 32 位)。每写满一段就滚动到下一段,所有写入都是顺序追加。

### 不这样会怎样:一个大文件的麻烦

> **不这样会怎样**:如果 WAL 是无限长的一个大文件,麻烦一堆——
>
> 1. **文件越来越大、管理困难**:单个文件越来越大,文件系统(尤其 inode、extent 表)吃不消,移动/备份/删除都难;
> 2. **归档和复制没法做**:你要把产生的日志传给备库(流复制)或归档到对象存储(`archive_command` 传到 S3),总不能传一个无限大的文件。切成固定大小的段,每写满一段就传一段,干净利落;
> 3. **回收困难**:旧日志要删,从一个无限长文件里"删除中间某段"几乎不可能,但删除一个完整的 16MB 段文件就是一个 `unlink`。

切成段后,每段写满就滚动,旧的、不再需要的段(它记录的改动,对应数据页都已落盘了)会被**回收复用**——这就是为什么 WAL 不会无限膨胀。回收的边界由 checkpoint 决定(下一章详讲:checkpoint 决定 redo 起点在哪,redo 起点之前的段就安全可删)。

### 16MB 的权衡

- **太大**:单段文件管理/传输笨重(传一个 1GB 的段,网络抖一下要重传整段);
- **太小**:段切换频繁(频繁 `open`/`close` 文件),且文件数太多,管理开销大;
- **16MB 是 PG 的默认折中**。可在 initdb 时用 `--wal-segsize` 调整(如 64MB、1GB)。对归档(传到 S3 等)和复制都很合适,小到能容忍单次失败重传,大到段切换开销可忽略。

> WAL 段的物理位置在数据目录的 `pg_wal/`(旧名 `pg_xlog/`)下。用 `pg_ls_waldir()` 这个 SQL 函数能看到当前所有段。

---

## 七、full page image:防"部分写"的关键设计

这是 WAL 里最容易被忽略、却极关键的一个设计,本章必须讲透。它解决的是磁盘的一个阴暗面:**部分写(partial write / torn page)**。

### 不这样会怎样:磁盘会"写一半"

> **不这样会怎样**:一个 8KB 的数据页,写到磁盘时**不是原子的**——磁盘可能在写了前 4KB 后断电,留下一个**前半新、后半旧**的破损页(torn page,撕裂页)。更糟糕的是,**数据库无法察觉这种破损**:页头的 checksum 能查出来,但在 checksum 出现之前(或关掉 checksum 的库),数据库会把这个破损页当成正常页用。
>
> 这种破损,普通的 redo 救不了。为什么?因为 redo 是"**在旧页基础上应用增量改动**"——它的前提是"旧页的内容是完整的、正确的"。可这个页已经破损了,旧页的内容根本不对,在错的基础上应用增量只会**错上加错**。
>
> 举个具体例子:checkpoint 之后,某页第一次被改(从版本 V0 改到 V1),WAL 记的是增量"WAL record R1:把字节 100~120 改成 X"。数据库刷盘时掉电,留下前 4KB 是 V1、后 4KB 是 V0 的破损页。重启 redo 时,数据库拿到这个破损页(以为是 V0),应用 R1 的增量——可这页既不是 V0 也不是 V1,应用 R1 后得到的是一个谁都不认识的"四不像"。数据彻底坏了。

PG 的解法是 **full page image(全页镜像,简称 FPI)**:

> **在一个 checkpoint 之后,第一次改动某个页时,把这一整页的完整内容记进 WAL**(而不只是记增量)。

这样,哪怕这页后来被部分写损坏,redo 时**先用 FPI 把整页恢复成 checkpoint 时的样子(V0),再应用之后的增量改动(R1 等),就能得到正确结果(V1)**。FPI 绕开了"旧页破损"这个前提——它直接提供了一份保证完整的 V0 副本。

### FPI 的触发:在 XLogRecordAssemble 里判断

什么时候加 FPI?在 `XLogRecordAssemble` 里,对每个登记的 buffer 判断:

> [src/backend/access/transam/xloginsert.c:604-623](../postgresql-17.0/src/backend/access/transam/xloginsert.c#L604-L623)

```c
		/* Determine if this block needs to be backed up */
		if (regbuf->flags & REGBUF_FORCE_IMAGE)
			needs_backup = true;
		else if (regbuf->flags & REGBUF_NO_IMAGE)
			needs_backup = false;
		else if (!doPageWrites)
			needs_backup = false;
		else
		{
			/*
			 * We assume page LSN is first data on *every* page that can be
			 * passed to XLogInsert, whether it has the standard page layout
			 * or not.
			 */
			XLogRecPtr	page_lsn = PageGetLSN(regbuf->page);

			needs_backup = (page_lsn <= RedoRecPtr);
			...
		}
```

读这段,触发 FPI 的条件就清楚了:

1. 调用者用 `REGBUF_FORCE_IMAGE` 显式要求(强制全页镜像,少数场景);
2. `doPageWrites` 开着(`fullPageWrites = on`,下面讲),**并且**这页的 `page_lsn <= RedoRecPtr`——也就是**这页自上次 checkpoint 以来第一次被改**(`RedoRecPtr` 是 redo 起点,这页的 `pd_lsn` 还没超过它,说明这页在 checkpoint 时就被改过、之后还没产生过新的非-FPI 记录)。

一旦满足 `needs_backup`,就在记录里附上整页镜像,并打上标志位 `BKPBLOCK_HAS_IMAGE`:

> [src/include/access/xlogrecord.h:196-201](../postgresql-17.0/src/include/access/xlogrecord.h#L196-L201)

```c
#define BKPBLOCK_FORK_MASK	0x0F
#define BKPBLOCK_FLAG_MASK	0xF0
#define BKPBLOCK_HAS_IMAGE	0x10	/* block data is an XLogRecordBlockImage */
#define BKPBLOCK_HAS_DATA	0x20
#define BKPBLOCK_WILL_INIT	0x40	/* redo will re-init the page */
#define BKPBLOCK_SAME_REL	0x80	/* RelFileLocator omitted, same as
									 * previous */
```

整页镜像的字节布局由 `XLogRecordBlockImageHeader` 描述:

> [src/include/access/xlogrecord.h:141-153](../postgresql-17.0/src/include/access/xlogrecord.h#L141-L153)

```c
typedef struct XLogRecordBlockImageHeader
{
	uint16		length;			/* number of page image bytes */
	uint16		hole_offset;	/* number of bytes before "hole" */
	uint8		bimg_info;		/* flag bits, see below */

	/*
	 * If BKPIMAGE_HAS_HOLE and BKPIMAGE_COMPRESSED(), an
	 * XLogRecordBlockCompressHeader struct follows.
	 */
} XLogRecordBlockImageHeader;
```

注意一个优化:PG 数据页中间通常有一段"空洞"(pd_lower 到 pd_upper 之间,全是零)。因为这段是零,WAL 不存它,只记 `hole_offset`(空洞开始位置)和(压缩时)`hole_length`(空洞长度)。这样 FPI 实际存的字节往往是 `BLCKSZ - hole_length`,比整页小。如果开了 `wal_compression`,还能进一步用 pglz/lz4/zstd 压缩 FPI(`BKPIMAGE_COMPRESS_*` 标志位)。

### FPI 的总开关:fullPageWrites

FPI 由 `fullPageWrites` 这个共享变量控制(默认 `on`):

> [src/backend/access/transam/xlog.c:419-429](../postgresql-17.0/src/backend/access/transam/xlog.c#L419-L429)

```c
	/*
	 * fullPageWrites is the authoritative value used by all backends to
	 * determine whether to write full-page image to WAL. This shared value,
	 * instead of the process-local fullPageWrites, is required because, when
	 * full_page_writes is changed by SIGHUP, we must WAL-log it before it
	 * actually affects WAL-logging by backends.  Checkpointer sets at startup
	 * or after SIGHUP.
	 *
	 * To read these fields, you must hold an insertion lock. To modify them,
	 * you must hold ALL the locks.
	 */
	XLogRecPtr	RedoRecPtr;		/* current redo point for insertions */
	bool		fullPageWrites;
```

注意这段设计的讲究:`fullPageWrites` 是**共享内存里**的值(不是每个 backend 自己的本地副本),原因是改它(GUC `full_page_writes` 通过 SIGHUP 改)**必须先 WAL-log 再生效**——否则某个 backend 还在用旧值、某个用新值,FPI 行为不一致会导致 redo 出错。`Checkpointer` 进程负责在启动或 SIGHUP 后更新这个共享值。

### FPI 的代价与权衡

> **代价**:FPI 让 WAL 变大。一个 checkpoint 后首次改某页,要记整页 8KB(扣掉空洞后往往还有几 KB),而正常的增量改动可能只有几十字节。所以**一个 checkpoint 周期内,每个页只在首次改时产生一个 FPI**;之后这页的 `pd_lsn` 推进了,再改就只记增量(`page_lsn > RedoRecPtr`,`needs_backup = false`)。
>
> 这就是**可靠性(能从 torn page 恢复)和 WAL 体积**之间的权衡。`fullPageWrites` 开关让你在"存储保证不会部分写"的场景关掉它省空间——比如使用带 BBU(电池备份)的 RAID 卡、或者 ZFS 这类做 CoW 的文件系统,它们能保证 8KB 写原子。但对普通磁盘/SSD,绝不能关。

> [和第 19 章 checkpoint 的呼应] FPI 在"checkpoint 后首次改某页"时触发,所以**checkpoint 越频繁,每个页"首次改"的机会越多,FPI 产生得越多,WAL 越膨胀**。这是 spread checkpoint(下一章)想拉长 checkpoint 间隔的动机之一——拉长间隔,FPI 更少,WAL 更紧凑。这个权衡在第 19 章会展开。

---

## 八、并发插入:多个 backend 怎么同时写日志

一台数据库同时有成百上千个连接,每个都在改数据、都要往 WAL 记日志。它们往同一个日志缓冲里追加,怎么不打架?

答案是 **WALInsertLock 数组**——不是一个全局锁,而是**一组锁**,每个 backend 哈希分散到其中一把上:

> [src/backend/access/transam/xlog.c:367-372](../postgresql-17.0/src/backend/access/transam/xlog.c#L367-L372)

```c
typedef struct
{
	LWLock		lock;
	pg_atomic_uint64 insertingAt;   /* 这个持有者已插入到哪个位置 */
	XLogRecPtr	lastImportantAt;
} WALInsertLock;
```

每把锁里除了 `lock`,还有 `insertingAt`(这个持有者已经插入到哪个位置)和 `lastImportantAt`(最近一条"重要"记录的位置,刷日志时用)。

> **不这样会怎样**:如果所有 backend 抢一把全局锁来插日志,那 WAL 插入就成了全库的瓶颈——成百上千个连接全在等这一把锁,串行化极差。用一组锁分散争用,多个 backend 能真正并行地往各自"预约"的日志位置插数据,只在最后汇总位置时短暂同步。

### 缓存行对齐:防 false sharing

WALInsertLock 数组特意**做了缓存行对齐**:

> [src/backend/access/transam/xlog.c:381-385](../postgresql-17.0/src/backend/access/transam/xlog.c#L381-L385)

```c
typedef union WALInsertLockPadded
{
	WALInsertLock l;
	char		pad[PG_CACHE_LINE_SIZE];
} WALInsertLockPadded;
```

把每个锁填充到 CPU 缓存行大小(`PG_CACHE_LINE_SIZE`,通常 64 字节)。为什么?**防止 false sharing**:多个 CPU 核各自频繁改自己的锁,若两把锁挤在同一缓存行,一个 CPU 改它的锁会让另一个 CPU 的缓存行失效,对方得重新从内存加载,白白变慢。注释([xlog.c:375-380](../postgresql-17.0/src/backend/access/transam/xlog.c#L375-L380))说得很直白:"ensures that individual slots don't cross cache line boundaries"。

> 这种"为了几个纳秒做缓存行对齐"的细节,正是数据库底层对性能锱铢必较的体现。第 8 章 `BufferDesc` 也做了同样的事——但凡会被多核并发频繁改的结构,基本都会对齐缓存行。

### 两步插入:先预约位置,再并行拷数据

拿到一把 `WALInsertLock` 后,插入记录是**两步**(`XLogInsertRecord` 里的注释讲得很清楚):

> [src/backend/access/transam/xlog.c:786-810](../postgresql-17.0/src/backend/access/transam/xlog.c#L786-L810)

```c
	/*----------
	 *
	 * We have now done all the preparatory work we can without holding a
	 * lock or modifying shared state. From here on, inserting the new WAL
	 * record to the shared WAL buffer cache is a two-step process:
	 *
	 * 1. Reserve the right amount of space from the WAL. The current head of
	 *	  reserved space is kept in Insert->CurrBytePos, and is protected by
	 *	  insertpos_lck.
	 *
	 * 2. Copy the record to the reserved WAL space. This involves finding the
	 *	  correct WAL buffer containing the reserved space, and copying the
	 *	  record in place. This can be done concurrently in multiple processes.
	 ...
```

- **第一步:预约位置**(`insertpos_lck` 短暂自旋锁保护)——快速原子地"占"住日志流里的一段空间,记下"我的记录从哪个 LSN 开始"。这步很快,因为只是个计数器加法。
- **第二步:拷贝数据**——把记录内容拷进预约的位置。这步**可以多进程并行**,因为每个 backend 都有自己的位置,互不重叠。

这种"先抢位置(短锁)再并行填数据(长操作但无锁)"的设计,把串行化的部分压到最小。`insertingAt` 字段就是为了让别的进程(比如 `XLogFlush`)知道"这个 inserter 已经填到哪了"——刷日志时要等所有 inserter 把对应位置填完才能刷,否则会刷到半成品。

---

## 九、日志的"插入/写/刷"三阶段,与 group commit

一条日志从产生到不怕掉电,要经过三道关。PG 用三个原子计数器跟踪进度:

> [src/backend/access/transam/xlog.c:470-473](../postgresql-17.0/src/backend/access/transam/xlog.c#L470-L473)

```c
	pg_atomic_uint64 logInsertResult;	/* last byte + 1 inserted to buffers */
	pg_atomic_uint64 logWriteResult;	/* last byte + 1 written out */
	pg_atomic_uint64 logFlushResult;	/* last byte + 1 flushed */
```

| 阶段 | 含义 | 怕不怕掉电 |
|---|---|---|
| **插入(insert)** | 写进 WAL 的**共享内存缓冲**(WAL buffers) | 怕(内存易失) |
| **写(write)** | `write()` 系统调用到 **OS 页缓存** | 还怕(OS 缓存也是易失的) |
| **刷(flush/fsync)** | `fsync()` **真正落盘** | **不怕**(磁盘是非易失的) |

> **这里有个新手最常踩的坑**:`write()` 返回了不等于数据落盘了!`write()` 只是把数据交给了操作系统的页缓存,掉电时 OS 缓存照样丢。**只有 `fsync()` 返回了,数据才算真正写到磁盘盘片上**。这是第 14 章 D 那节"存储设备会撒谎"的延续——`write` 会撒谎,`fsync` 才是承诺。WAL 的持久性,最终落在 `fsync` 这一步。

### group commit:把多次提交合并成一次 fsync

`fsync` 很贵——一次磁盘同步,几毫秒(机械盘甚至十几毫秒)。如果每个事务 `COMMIT` 都独立 fsync 一次,高并发下磁盘扛不住,延迟爆炸。

> **不这样会怎样**:1000 个事务同时提交,各自独立 fsync → 1000 次磁盘同步,每次几毫秒,光提交就要几秒,吞吐崩塌。这是同步提交方案的最大软肋。

PG 的优化是 **group commit(成组提交)**。`XLogFlush` 里有这样一段注释:

> [src/backend/access/transam/xlog.c:2820-2826](../postgresql-17.0/src/backend/access/transam/xlog.c#L2820-L2826)

```c
	/*
	 * Since fsync is usually a horribly expensive operation, we try to
	 * piggyback as much data as we can on each fsync: if we see any more data
	 * entered into the xlog buffer, we'll write and fsync that too, so that
	 * the final value of LogwrtResult.Flush is as large as possible. This
	 * gives us some chance of avoiding another fsync immediately after.
	 */
```

读这段:**fsync 太贵,所以一次 fsync 时尽量"搭车"多刷一些数据**。机制是这样的——多个并发到达的 `COMMIT`,会**共享同一次 fsync**:

1. 几个事务几乎同时到达 `COMMIT`,各自的 commit record 先后写进 WAL 缓冲;
2. 第一个去 fsync 的事务拿到 `WALWriteLock`,在 fsync 前它会"顺便看看"现在缓冲里有多少数据(`LogwrtRqst.Write`),**把这些数据一起 fsync 了**(注释里的 piggyback);
3. 在它 fsync 的这几毫秒里,又有别的事务把 commit record 写进了缓冲——这些也会被同一次 fsync 带走;
4. fsync 完成后,**这一批所有事务一起被唤醒、一起返回 `COMMIT` 成功**。

于是 N 个并发提交,常常只产生 1 次 fsync。**并发越高,group commit 省得越多**——这就是为什么数据库在并发上来后,单事务的提交延迟反而可能下降(共享了 fsync)。

`XLogFlush` 里拿到锁的方式也配合 group commit:

> [src/backend/access/transam/xlog.c:2852-2860](../postgresql-17.0/src/backend/access/transam/xlog.c#L2852-L2860)

```c
		/*
		 * Try to get the write lock. If we can't get it immediately, wait
		 * until it's released, and recheck if we still need to do the flush
		 * or if the backend that held the lock did it for us already. This
		 * helps to maintain a good rate of group committing when the system
		 * is bottlenecked by the speed of fsyncing.
		 */
		if (!LWLockAcquireOrWait(WALWriteLock, LW_EXCLUSIVE))
```

`LWLockAcquireOrWait` 的语义是"拿不到就等,等被唤醒后再看还要不要做"。如果别的事务在等的过程中已经把日志刷到我要的位置了,它就不用再刷——直接享受别人的劳动成果。这是 group commit 的精髓:**"等待"本身就是合并**。

---

## 十、`COMMIT` 到底在等什么:synchronous_commit 的权衡

回到第 14 章那句"`COMMIT` 要刷 WAL 落盘"。现在结合三阶段看,PG 通过 `synchronous_commit` 参数让你选 `COMMIT` 等到哪一道水位。先看代码里到底怎么决定的——在 `RecordTransactionCommit` 里:

> [src/backend/access/transam/xact.c:1476-1482](../postgresql-17.0/src/backend/access/transam/xact.c#L1476-L1482)

```c
	if ((wrote_xlog && markXidCommitted &&
		 synchronous_commit > SYNCHRONOUS_COMMIT_OFF) ||
		forceSyncCommit || nrels > 0)
	{
		XLogFlush(XactLastRecEnd);

		/*
		 * Now we may update the CLOG, if we wrote a COMMIT record above
		 */
		if (markXidCommitted)
			TransactionIdCommitTree(xid, nchildren, children);
	}
```

读这段:`synchronous_commit > SYNCHRONOUS_COMMIT_OFF` 时(即不是异步),才调 `XLogFlush` 同步等日志落盘;否则走 `else` 分支异步提交:

> [src/backend/access/transam/xact.c:1494-1511](../postgresql-17.0/src/backend/access/transam/xact.c#L1494-L1511)

```c
	else
	{
		/*
		 * Asynchronous commit case:
		 *
		 * This enables possible committed transaction loss in the case of a
		 * postmaster crash because WAL buffers are left unwritten. Ideally we
		 * could issue the WAL write without the fsync, but some
		 * wal_sync_methods do not allow separate write/fsync.
		 *
		 * Report the latest async commit LSN, so that the WAL writer knows to
		 * flush this commit.
		 */
		XLogSetAsyncXactLSN(XactLastRecEnd);

		/*
		 * We must not immediately update the CLOG, since we didn't flush the
		 * XLOG. Instead, we store the LSN up to which the XLOG must be
		 * flushed before the CLOG may be updated.
		 */
		if (markXidCommitted)
			TransactionIdAsyncCommitTree(xid, nchildren, children, XactLastRecEnd);
	}
```

异步提交的注释直白:"This enables possible committed transaction loss in the case of a postmaster crash"——异步提交**允许丢失已提交事务**(postmaster 崩溃时,WAL 缓冲没刷盘就丢了)。它做的事是 `XLogSetAsyncXactLSN`:**告诉 walwriter 进程"麻烦你抽空把日志刷到这个位置"**,然后立刻返回成功。CLOG(提交状态)的更新也延后——必须等 WAL 真正刷盘后才能更新,否则会出现"WAL 没刷但 CLOG 说已提交"的不一致。

### 五档水位的语义

PG 通过 `synchronous_commit` 这个 GUC 给你一整列档位(`SYNCHRONOUS_COMMIT_ON` 是默认值,见 [guc_tables.c:4908-4909](../postgresql-17.0/src/backend/utils/misc/guc_tables.c#L4908-L4909)):

| 设置 | `COMMIT` 等到 | 安全性 | 速度 |
|---|---|---|---|
| `off`(异步) | 日志进**内存缓冲**(logInsertResult),不直接等 fsync | 提交后瞬间掉电可能丢 | 最快 |
| `local` | 日志**本地** fsync(不关心备库) | 本地不丢,但备库可能丢 | 快 |
| `remote_write` | 本地 fsync + 备库**写到 OS**(没 fsync) | 备库掉电可能丢 | 中 |
| `remote_apply` | 本地 fsync + 备库**已回放** | 最强(备库一致) | 最慢 |
| `on`(默认) | 本地 fsync | 本地不丢 | 中 |

> 这是你手里的**持久性 vs 性能**开关。核心洞察:**持久性不是"有或无",而是一系列水位**——你愿意为"多扛一种掉电场景"付出多少延迟。fsync 才是跨越"易失"到"持久"的桥;`synchronous_commit` 决定你要不要过这座桥、过到哪一档。

注意 `synchronous_commit` 是**事务级**的参数——你可以给不同事务设不同档位:

```sql
-- 某个对丢失不敏感的统计任务,用异步换速度
SET LOCAL synchronous_commit = off;
INSERT INTO logs ... ;

-- 某个关键转账,强制同步
SET LOCAL synchronous_commit = on;
UPDATE accounts SET balance = ... ;
```

这种"按事务调档"的能力,让你在同一套数据库里,既能跑高性能的批量任务,又能保关键事务的持久性。

> 衔接 P4-14(持久性)和 P6-22(复制):本章讲的是**单机持久性**(本地 fsync);`remote_*` 几档是**主备一致性**的话题,会到 P6 第 22 章《复制与高可用》详讲——为什么主库要等备库、备库写到 OS 还是 fsync、备库回放完才算,这些档位对应不同的复制协议。本章你只需记住:**`synchronous_commit` 决定 `COMMIT` 等到 WAL 的哪一道水位,这是持久性的总开关**。

---

## 十一、walwriter 后台进程:异步提交的兜底人

上一节异步提交里提到"walwriter 进程会抽空刷 WAL"。这里把这个后台进程讲清——它不光服务异步提交,也是 WAL 整体落盘的重要角色。

**walwriter** 是 PG 启动时常驻的一个后台进程,主循环是 `WalWriterMain`:

> [src/backend/postmaster/walwriter.c:89-91](../postgresql-17.0/src/backend/postmaster/walwriter.c#L89-L91)

walwriter 的活儿就两件:**周期性地把 WAL 缓冲里的数据刷到磁盘**,以及**给异步提交兜底**。看它的主循环:

> [src/backend/postmaster/walwriter.c:223-268](../postgresql-17.0/src/backend/postmaster/walwriter.c#L223-L268)

```c
	for (;;)
	{
		long		cur_timeout;
		...
		/* Clear any already-pending wakeups */
		ResetLatch(MyLatch);

		/* Process any signals received recently */
		HandleMainLoopInterrupts();

		/*
		 * Do what we're here for; then, if XLogBackgroundFlush() found useful
		 * work to do, reset hibernation counter.
		 */
		if (XLogBackgroundFlush())
			left_till_hibernate = LOOPS_UNTIL_HIBERNATE;
		else if (left_till_hibernate > 0)
			left_till_hibernate--;

		/* report pending statistics to the cumulative stats system */
		pgstat_report_wal(false);

		/*
		 * Sleep until we are signaled or WalWriterDelay has elapsed.  If we
		 * haven't done anything useful for quite some time, lengthen the
		 * sleep time so as to reduce the server's idle power consumption.
		 */
		if (left_till_hibernate > 0)
			cur_timeout = WalWriterDelay;	/* in ms */
		else
			cur_timeout = WalWriterDelay * HIBERNATE_FACTOR;

		(void) WaitLatch(MyLatch,
						 WL_LATCH_SET | WL_TIMEOUT | WL_EXIT_ON_PM_DEATH,
						 cur_timeout,
						 WAIT_EVENT_WAL_WRITER_MAIN);
	}
```

读这段:

1. **`XLogBackgroundFlush()`** 是核心——它尝试把 WAL 缓冲里"已经写到、但还没落盘"的部分刷出去。这是个" opportunistic(机会主义)"的刷:有就刷,没有就算。
2. **`WalWriterDelay`** 控制休眠时长(默认 200ms,见 GUC)。空闲时按这个周期转一圈,把累积的 WAL 刷盘。
3. **`HIBERNATE_FACTOR`**:如果连着几圈都没活儿干(`left_till_hibernate` 归零),就进入"冬眠"模式,休眠时间拉长(省电)。一旦异步提交来信号唤醒(`SetWalWriterSleeping` / latch),立刻醒。

> **walwriter 解决两件事**:① 给异步提交兜底——异步提交不直接 fsync,但承诺"walwriter 会在 `WalWriterDelay` 内把这条记录刷盘",所以异步提交"可能丢"的窗口被压到这个量级(默认 200ms);② 平时持续把 WAL 缓冲往磁盘推,让缓冲不至于堆积太多,缓解 backend 同步提交时 fsync 的压力(group commit 之外的第二道缓解)。

> walwriter 和同步提交是**互补**的:同步提交自己 fsync(保证持久),walwriter 负责把"暂时没必要立刻刷但要尽快刷"的 WAL(主要是异步提交的)推出去。两者都靠 `XLogWrite`/`XLogFlush` 这套底层机制。

---

## 关键源码精读:`XLogRecordAssemble`——一条记录是怎么拼出来的

这一章最值得精读的是 `XLogRecordAssemble`([src/backend/access/transam/xloginsert.c:548](../postgresql-17.0/src/backend/access/transam/xloginsert.c#L548))——它把前面 `XLogRegisterBuffer/Data` 登记的内容,组装成一条完整的 `XLogRecord`。它把"一条记录长什么样、FPI 怎么决策、CRC 怎么算"全讲清了。

### 头部填充:记录头是最后填的

`XLogRecordAssemble` 先为每个登记的 block 写 `XLogRecordBlockHeader`(可能附 FPI 的 image header、可能省关系定位),再写 main data header,最后填 `XLogRecord` 头:

> [src/backend/access/transam/xloginsert.c:933-942](../postgresql-17.0/src/backend/access/transam/xloginsert.c#L933-L942)

```c
	/*
	 * Fill in the fields in the record header. Prev-link is filled in later,
	 * once we know where in the WAL the record will be inserted. The CRC does
	 * not include the record header yet.
	 */
	rechdr->xl_xid = GetCurrentTransactionIdIfAny();
	rechdr->xl_tot_len = (uint32) total_len;
	rechdr->xl_info = info;
	rechdr->xl_rmid = rmid;
	rechdr->xl_prev = InvalidXLogRecPtr;
	rechdr->xl_crc = rdata_crc;
```

注意注释:**`xl_prev` 现在填的是 `InvalidXLogRecPtr`,要等真正插入(知道这条记录在 WAL 里的位置)后才填上正确的值**。为什么?因为 `xl_prev` 是"上一条记录的位置",这个位置只有在记录被分配到 WAL 流里某个 LSN 时才能算出来——而分配位置是 `XLogInsertRecord` 的事,`Assemble` 这一步还不知道。这是个"两阶段填充"的设计:头部除了 `xl_prev` 都填好,CRC 先算 payload 的(不含头),等插入时再补 `xl_prev` 并把头加进 CRC。

`xl_xid` 是 `GetCurrentTransactionIdIfAny()`——如果当前有事务,填事务号;没有(比如某些系统操作)就是 `InvalidTransactionId`。

### CRC:先算 payload,头后加

CRC 的计算分两步,先算所有 payload(包括 block header、FPI、main data),不含 `XLogRecord` 头:

> [src/backend/access/transam/xloginsert.c:887-894](../postgresql-17.0/src/backend/access/transam/xloginsert.c#L887-L894)

```c
	/*
	 * Calculate CRC of the data
	 *
	 * Note that the record header isn't added into the CRC initially since we
	 * don't know the prev-link yet.  Thus, the CRC will represent the CRC of
	 * the whole record in the order: rdata, then backup blocks, then record
	 * header.
	 */
	INIT_CRC32C(rdata_crc);
	COMP_CRC32C(rdata_crc, hdr_scratch + SizeOfXLogRecord, hdr_rdt.len - SizeOfXLogRecord);
	for (rdt = hdr_rdt.next; rdt != NULL; rdt = rdt->next)
		COMP_CRC32C(rdata_crc, rdt->data, rdt->len);
```

注意 `hdr_scratch + SizeOfXLogRecord`——故意跳过记录头的 24 字节(`SizeOfXLogRecord`),只 CRC 后面的 block header 和 data。然后 `XLogInsertRecord` 在真正插入、知道 `xl_prev` 后,会把记录头加进 CRC(注释最后一行说"in the order: rdata, then backup blocks, then record header")。这种"分两段算 CRC"是为了让头部填充可以延后。

### FPI 决策:三档优先级

FPI 的决策(第七节讲过)在循环里对每个 block 执行,优先级是:

1. `REGBUF_FORCE_IMAGE` → 强制全页镜像(最高优先);
2. `REGBUF_NO_IMAGE` → 不要镜像;
3. `doPageWrites` 关 → 不要镜像;
4. 否则看 `page_lsn <= RedoRecPtr` → 是就加 FPI(这页自 checkpoint 后首次改)。

这个决策的精妙之处在于:**它把"什么时候该加 FPI"完全数据驱动**——只看这页的 `pd_lsn` 和 redo 起点的比较,不需要额外的记账。一个页在一个 checkpoint 周期里,只有"首次被改"那一刻 `pd_lsn` 才不超过 `RedoRecPtr`(因为改完 `pd_lsn` 就推进到新记录的 LSN 了,而 `RedoRecPtr` 在下个 checkpoint 前不变)。所以"首次改自动加 FPI"这个语义,被一个简单的 LSN 比较精确地实现。

### 总长校验:记录不能太大

组装完检查总长不超限:

> [src/backend/access/transam/xloginsert.c:896-908](../postgresql-17.0/src/backend/access/transam/xloginsert.c#L896-L908)

```c
	/*
	 * Ensure that the XLogRecord is not too large.
	 *
	 * XLogReader machinery is only able to handle records up to a certain
	 * size (ignoring machine resource limitations), so make sure that we will
	 * not emit records larger than the sizes advertised to be supported.
	 */
	if (total_len > XLogRecordMaxSize)
		ereport(ERROR,
				(errmsg_internal("oversized WAL record"),
				 errdetail_internal("WAL record would be %llu bytes (of maximum %u bytes); rmid %u flags %u.",
									(unsigned long long) total_len, XLogRecordMaxSize, rmid, info)));
```

`XLogRecordMaxSize` 是 1020MB(见 [xlogrecord.h:76](../postgresql-17.0/src/include/access/xlogrecord.h#L76)),因为 `XLogReader` 一次要把整条记录读进单个内存分配,太大的记录会撑爆。这其实是个"几乎不会触发"的护栏——正常记录最多几 KB(一个 FPI 加点 data),离 1GB 远得很。

> **把 `XLogRecordAssemble` 看成一个"按 block 逐个填 header + 决策 FPI + 算 CRC + 填记录头(除 xl_prev)"的流水线**,就能抓住一条 WAL 记录是怎么"从登记的数据变成落盘的字节流"的。它和 `XLogInsertRecord`(占位置 + 拷贝 + 补 xl_prev + 加头进 CRC)配合,完成了 WAL 记录从构造到就绪的全过程。

---

## 章末小结

- **WAL 铁律**:先记日志,后改数据。其落地靠 LSN——脏页刷盘前,它依赖的日志(`pd_lsn`)必须先落盘。这是持久性的根基,不是道德约束而是机制保证。
- **持久性靠日志不靠数据页**:`COMMIT` 只等日志 fsync,数据页可延后落盘;掉电后 redo 重放日志补回数据。这是 WAL 解决"易失 vs 持久"矛盾的核心——把"慢的随机数据页刷盘"换成"快的顺序日志追加"。
- **一条 `XLogRecord`**:头(xl_tot_len / xl_xid / xl_prev / xl_info / xl_rmid / xl_crc)+ 一组 BlockHeader(可能含 FPI)+ main data。它是一个"改动了哪个块的哪部分、改成什么样"的完整说明书。
- **资源管理器(rmgr)**:每类对象注册一个 rmgr,提供 `rm_redo`;redo 时按 `xl_rmid` 分发。这是 WAL 与崩溃恢复的接口(第 20 章详讲 redo)。
- **LSN**:64 位单调递增的日志位置,既是记录的身份证,又是数据页的进度戳(`pd_lsn`)。write-ahead rule、FPI 触发、redo 跳过判定,全靠 LSN 比较。
- **WAL 切成 16MB 段**滚动管理(默认 `DEFAULT_XLOG_SEG_SIZE`),便于归档、复制、回收;回收边界由 checkpoint 决定(下一章)。
- **full page image(FPI)**:防磁盘"部分写(torn page)"。checkpoint 后首次改某页时(`page_lsn <= RedoRecPtr`),记整页镜像(扣空洞、可压缩),代价是 WAL 变大。`fullPageWrites` 是总开关。
- **并发插入**:用一组 `WALInsertLock`(带缓存行对齐防 false sharing)分散争用;两步插入(短锁预约位置 + 无锁并行拷数据)。**group commit** 把多次提交合并成一次 fsync——`XLogFlush` 里 piggyback 尽量多刷数据,`LWLockAcquireOrWait` 让等待本身就是合并。
- **三道水位**:insert(进内存缓冲,怕掉电)→ write(到 OS 缓存,还怕)→ flush/fsync(真落盘,不怕)。`write` 不等于落盘,这是新手最常踩的坑。
- **持久性是一系列水位**(`synchronous_commit`):off/local/remote_write/remote_apply/on,你在安全和速度间权衡。**walwriter** 后台进程给异步提交兜底,周期性刷 WAL。
- **`COMMIT` 等什么**:等"这条事务的 WAL fsync 到磁盘"。`synchronous_commit > OFF` 时同步等(`XLogFlush`),否则异步(`XLogSetAsyncXactLSN` + walwriter 兜底)。

### 回扣主线

本章是"**不丢不乱**"那一侧的命脉——它直接兑现持久性(D),治"**易失**"本性。它和第 6 章的页(`pd_lsn`)、第 8 章的 Buffer Pool(脏页从哪来)、第 14 章的事务(`COMMIT` 刷 WAL、commit record、`RecordTransactionCommit`)首尾呼应,织成"数据怎么不丢"的完整链条。

至于数据的三个本性:本章主要服务"**易失**"(用日志的持久换内存的易失);同时 WAL 用顺序写、并发插入锁分散、group commit 这些设计,又一直在顾"**磁盘慢**"(把昂贵的随机刷盘换成便宜的顺序日志追加,把昂贵的 fsync 合并)——一只手治易失,另一只手顾磁盘慢,这正是 WAL 设计里最精巧的平衡。

### 想继续深入

- WAL 记录全家桶:[src/include/access/xlogrecord.h](../postgresql-17.0/src/include/access/xlogrecord.h)(尤其 full page image 那段注释,L113-194;各种 `BKPBLOCK_*` / `BKPIMAGE_*` 标志位)。
- 资源管理器:[src/include/access/rmgrlist.h](../postgresql-17.0/src/include/access/rmgrlist.h)(所有 rmgr 列表)、[src/include/access/xlog_internal.h:349-360](../postgresql-17.0/src/include/access/xlog_internal.h#L349-L360)(`RmgrData` 结构)。
- 构造与插入:[src/backend/access/transam/xloginsert.c](../postgresql-17.0/src/backend/access/transam/xloginsert.c) 的 `XLogBeginInsert`(L148)、`XLogRegisterBuffer`(L242)、`XLogRegisterData`(L364)、`XLogInsert`(L474)、`XLogRecordAssemble`(L548)。
- 共享状态与并发:[src/backend/access/transam/xlog.c](../postgresql-17.0/src/backend/access/transam/xlog.c) 的 `WALInsertLock`(L367)、`XLogCtlInsert`、`XLogInsertRecord`(L748)、`XLogWrite`(L2314)、`XLogFlush`(L2796)、`GetRedoRecPtr`(L6409)、`GetFullPageWriteInfo`(L6439)。
- `COMMIT` 与同步提交:[src/backend/access/transam/xact.c](../postgresql-17.0/src/backend/access/transam/xact.c) 的 `RecordTransactionCommit`(L1304),尤其同步/异步分支(L1476 / L1494)。
- walwriter:[src/backend/postmaster/walwriter.c](../postgresql-17.0/src/backend/postmaster/walwriter.c) 的 `WalWriterMain`(L89)。

---

> WAL 不停追加,段越滚越多。可很多旧段记录的改动,数据页其实已经刷盘了,这些旧段就"过期"了。**谁来划定"哪些 WAL 还得留、哪些可以回收"?崩溃后又要从哪开始重放日志?** 这些问题,WAL 自己回答不了——它需要一个"同步点"。翻开 **第 19 章 · Checkpoint:日志与数据的同步点**——它把脏页批量兑现落盘,在 WAL 里焊下一个 redo 起点,回答"WAL 能截到哪、恢复从哪重放"。
