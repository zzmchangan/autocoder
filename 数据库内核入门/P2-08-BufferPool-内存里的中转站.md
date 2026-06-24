# 第 8 章 · Buffer Pool:内存里的中转站

> **前置**:你需要先读过[第 7 章《堆表与元组》](P2-07-堆表与元组-一行数据怎么落盘.md)——那一章把"一行数据怎么塞进 8KB 页"讲完了。但第 6、7 章里我们一直默认"读写一页就是读写磁盘",这话其实只对了一半:**真要每次都直接打磁盘,数据库会慢到没法用**。本章就把这层窗户纸捅破——数据页在内存里有一座"中转站",绝大多数读写其实在内存里就完成了。它就是 **Buffer Pool(共享缓冲池)**。

> **核心问题**:为什么不能每次读写都直接打磁盘?Buffer Pool 怎么用一块固定大小的共享内存缓存磁盘页?当缓冲池满了,该淘汰谁——**Clock-Sweep(时钟扫描)** 替换算法怎么工作,为什么不用 LRU?一个页被读到内存要经过几道映射?**脏页(dirty page)** 为什么既是性能命脉又是风险点,它什么时候被写回、由谁写?

> **读完本章你会明白**:读一个页的完整路径(查 hash 表 → 命中直接用 / 未命中:挑 victim → 若脏先刷 WAL 再写盘 → 从磁盘读入 → 登记 hash 表);**pin(refcount)** 和 **lock** 的区别,为什么访问页必须先 pin;Clock-Sweep 的 usage count 怎么做到"近似 LRU 但更廉价";以及 `shared_buffers` 为什么不是越大越好。

> **如果一读觉得太难**:先记三件事——① Buffer Pool 是内存里缓存磁盘页的固定数组,读写都先在内存里搞;② 满了用 Clock-Sweep 挑一个 victim 淘汰,脏的 victim 先把日志刷盘才能换走;③ 访问页要先 pin(防被淘汰),改页要加 lock(防互相踩)。其余细节第二遍配合源码再抠。

---

## 一、为什么不能每次读写都直接打磁盘

第 6 章说过:磁盘 I/O 的最小单位是 8KB 的页,一次寻道几毫秒。现在把这个数字和内存对比一下——内存的随机访问大约 **几十纳秒**,磁盘寻道大约 **几毫秒**,两者差了大约 **十万倍**。

一条 `SELECT * FROM users` 要扫几万页,如果每页都老老实实去读磁盘,就是几万次寻道,几万次"几毫秒"——一条查询要跑几十秒。这还没算写入:每次 `UPDATE` 都直接改磁盘上的页,不仅慢,还要面对随机写(第 18 章会看到,随机改数据页是数据库最想避免的事)。

> **不这样会怎样**:让每一次页面读写都穿透到磁盘。结果就是:磁盘成了全库的独木桥,成百上千个连接全在排队等寻道;一条查询里反复访问同一个热点页(比如索引的根节点),每次都要重新从磁盘搬一遍,内存里刚刚读过的数据被白白浪费;并发一上来,系统直接卡死。这种数据库没法用。

所以数据库在进程的地址空间里,**开辟一块固定大小的共享内存**,把磁盘页的副本缓存起来。这块内存就叫 **Buffer Pool(缓冲池)**。规则很简单:

- 要读写一个页,**先看它在不在 Buffer Pool 里**。在——直接用内存里的副本(这叫**命中 hit**);不在——从磁盘搬一页进来(**未命中 miss**)。
- **所有改动先发生在内存里的页上**,不立刻写回磁盘(写回的事交给后台进程和 checkpoint,第 19 章)。

只要热点数据大部分能命中,数据库的读写速度就从"磁盘级"飙升到"内存级"。这块共享内存的大小由参数 `shared_buffers` 控制(PG 默认 128MB,生产环境通常调到物理内存的 1/4 左右)。

> 这是"**磁盘慢**"这个本性逼出的第一道防线:**用内存换速度**。Buffer Pool 是性能的命脉,这一章整章都在讲它怎么把这道防线守好。它直接服务全书主线里"让数据**快**"那一侧。

---

## 二、Buffer Pool 长什么样:一个固定数组 + 一张 hash 表

理解 Buffer Pool,先看清它在内存里的物理形态。它其实就两样东西。

### 1. 缓冲区数组:NBuffers 个槽位

Buffer Pool 在共享内存里是一块连续的区域,**按页切成固定数量的小格子**。格子的总数叫 **NBuffers**(等于 `shared_buffers / 8KB`)。每个格子能装一个 8KB 页。PG 用一个编号(`buf_id`,从 0 开始)来索引这些格子。

光有"格子"还不够——你得知道"第 3 号格子现在缓存的是哪个磁盘页"。这个映射关系,存在每个格子对应的**描述符(BufferDesc)** 里。描述符和缓冲区分开存放(分两个数组),但一一对应:

> [src/include/storage/buf_internals.h:245-256](../postgresql-17.0/src/include/storage/buf_internals.h#L245-L256)

```c
typedef struct BufferDesc
{
	BufferTag	tag;			/* ID of page contained in buffer */
	int			buf_id;			/* buffer's index number (from 0) */

	/* state of the tag, containing flags, refcount and usagecount */
	pg_atomic_uint32 state;

	int			wait_backend_pgprocno;	/* backend of pin-count waiter */
	int			freeNext;		/* link in freelist chain */
	LWLock		content_lock;	/* to lock access to buffer contents */
} BufferDesc;
```

- **`tag`**:这个格子当前缓存的是**哪个磁盘页**(下面详讲)。
- **`buf_id`**:格子的编号。
- **`state`**:一个 32 位整数,**把标志位、refcount、usage count 打包塞在一起**(下面专讲)。
- **`content_lock`**:访问页**内容**时要加的锁(读写页里数据时用)。

> **注意一个性能细节**:描述符数组被刻意**对齐到 64 字节(一个 CPU 缓存行)并填充到缓存行大小**。注释直白地说:"Keeping it below 64 bytes is fairly important for performance"。因为成百上千个连接会并发地读写各自的描述符,如果两个描述符挤在同一缓存行,一个 CPU 改它的描述符会顺带把邻居的缓存行冲掉(false sharing),白白变慢。这种"为了几个纳秒做缓存行对齐"的抠门,正是数据库底层对性能锱铢必较的体现。

### 2. BufferTag:一个磁盘页的"身份证"

一个磁盘页在全世界怎么被唯一地指认?靠 `BufferTag`——它就是"哪个表、哪个 fork、第几页"的元组:

> [src/include/storage/buf_internals.h:93-100](../postgresql-17.0/src/include/storage/buf_internals.h#L93-L100)

```c
typedef struct buftag
{
	Oid			spcOid;			/* tablespace oid */
	Oid			dbOid;			/* database oid */
	RelFileNumber relNumber;	/* relation file number */
	ForkNumber	forkNum;		/* fork number */
	BlockNumber blockNum;		/* blknum relative to begin of reln */
} BufferTag;
```

它精确定位"表空间 X、数据库 Y、关系文件 Z、fork F、第 N 页"。注意这里**不存表名也不存 OID 的逻辑名**,而是直接用存储层的物理标识——这样即使正在刷盘的 backend 还"看不见"某个表(它的事务开始得比建表事务早),也能凭物理标识找到页并写出去。注释里专门强调了这一点。

### 3. state:标志位 + refcount + usage count 三合一

`state` 是这一章最关键的字段。PG 把三样东西压进一个 32 位整数(注释见 [src/include/storage/buf_internals.h:31-48](../postgresql-17.0/src/include/storage/buf_internals.h#L31-L48)):**18 位 refcount、4 位 usage count、10 位标志位**。为什么要打包?因为这样可以用一次原子操作(CAS)同时改它们,**不必加锁**——这是高并发下省锁的关键技巧。

标志位([src/include/storage/buf_internals.h:60-70](../postgresql-17.0/src/include/storage/buf_internals.h#L60-L70))里,本章要反复打交道的有:

```c
#define BM_LOCKED				(1U << 22)	/* buffer header is locked */
#define BM_DIRTY				(1U << 23)	/* data needs writing */
#define BM_VALID				(1U << 24)	/* data is valid */
#define BM_TAG_VALID			(1U << 25)	/* tag is assigned */
#define BM_IO_IN_PROGRESS		(1U << 26)	/* read or write in progress */
...
#define BM_PERMANENT			(1U << 31)	/* permanent buffer */
```

- **`BM_DIRTY`**:这页被改过、和磁盘上的版本不一样了——**脏页**。它是性能命脉(改动攒在内存不用立刻写盘),也是风险点(掉电就丢,得靠 WAL 兜底,第 18 章)。
- **`BM_VALID`**:这页的数据已经从磁盘读进来了,可以安全使用。
- **`BM_IO_IN_PROGRESS`**:这页正在被读入或写出,**别人别插手**。

> **为什么用 `Buffer` 而不是指针来引用一个缓冲区?** PG 的惯例是用一个 `int` 编号([src/include/storage/buf.h:23-25](../postgresql-17.0/src/include/storage/buf.h#L23-L25)):正数 = 共享缓冲池第 N 个(1-based),负数 = 本地缓冲池(临时表),`0 = InvalidBuffer` 表示"没有"。用编号而不用指针,是为了在多进程间稳定地引用同一个槽位,而且省一个解引用。

---

## 三、读一个页的完整路径:命中、淘汰、读入

这是本章的主干。从"我要读 users 表第 42 页"到"拿到内存里的页",中间到底发生了什么?核心函数是 [ReadBufferExtended](../postgresql-17.0/src/backend/storage/buffer/bufmgr.c#L791-L815),它转给内部的 `ReadBuffer_common`,再落到 `PinBufferForBlock`,真正干活的叫 **BufferAlloc**:

> [src/backend/storage/buffer/bufmgr.c:1594-1598](../postgresql-17.0/src/backend/storage/buffer/bufmgr.c#L1594-L1598)

```c
BufferAlloc(SMgrRelation smgr, char relpersistence, ForkNumber forkNum,
			BlockNumber blockNum,
			BufferAccessStrategy strategy,
			bool *foundPtr, IOContext io_context)
```

它分两大步:**先查 hash 表看在不在;不在就挑个 victim 槽位腾出来、读入**。

### 第一步:查 Buffer Mapping hash 表

Buffer Pool 本身是个数组,但"某个 tag 在不在池里"不能靠扫数组(O(N) 太慢)。所以 PG 又**额外维护了一张共享 hash 表**,把 `BufferTag → buf_id` 的映射存起来。这张表叫 **Buffer Mapping Table**(实现在 [src/backend/storage/buffer/buf_table.c](../postgresql-17.0/src/backend/storage/buffer/buf_table.c)),它的每个条目是 `{BufferTag key; int id}`([buf_table.c:27-31](../postgresql-17.0/src/backend/storage/buffer/buf_table.c#L27-L31))。

读一个页的第一动作,就是拿 tag 去 hash 表里查(`BufTableLookup`):

> [src/backend/storage/buffer/bufmgr.c:1611-1621](../postgresql-17.0/src/backend/storage/buffer/bufmgr.c#L1611-L1621)

```c
	/* create a tag so we can lookup the buffer */
	InitBufferTag(&newTag, &smgr->smgr_rlocator.locator, forkNum, blockNum);

	/* determine its hash code and partition lock ID */
	newHash = BufTableHashCode(&newTag);
	newPartitionLock = BufMappingPartitionLock(newHash);

	/* see if the block is in the buffer pool already */
	LWLockAcquire(newPartitionLock, LW_SHARED);
	existing_buf_id = BufTableLookup(&newTag, newHash);
```

这里有个关键设计:**hash 表被分成 128 个分区**(`NUM_BUFFER_PARTITIONS = 128`,[src/include/storage/lwlock.h:93](../postgresql-17.0/src/include/storage/lwlock.h#L93))。每个分区一把锁(`BufMappingLock`)。查表时只锁**自己 tag 落在的那个分区**,不影响别的分区。

> **不这样会怎样**:如果只有一把全局锁保护整张 hash 表,那成百上千个并发查询,每次查表都要抢这一把锁——hash 查询本来是飞快的,结果全卡在锁上。分成 128 个分区,把争用摊薄了 128 倍,绝大多数情况下不同连接查的 tag 落在不同分区,互不干扰。这是"把全局锁拆成一组分片锁"的经典并发优化(第 18 章讲 WAL 插入的 `WALInsertLock` 数组也是同一招)。

### 第二步之一:命中——直接 pin 了用

如果 `BufTableLookup` 返回了一个有效的 `buf_id`,说明这个页**已经在池里**了(命中)。这时只要做一件关键的事:**pin 住它**(`PinBuffer`),然后就能用了:

> [src/backend/storage/buffer/bufmgr.c:1631-1636](../postgresql-17.0/src/backend/storage/buffer/bufmgr.c#L1631-L1636)

```c
		buf = GetBufferDescriptor(existing_buf_id);

		valid = PinBuffer(buf, strategy);

		/* Can release the mapping lock as soon as we've pinned it */
		LWLockRelease(newPartitionLock);
```

注意注释那句"**一 pin 住就立刻释放 mapping lock**"——这正是 pin 的核心作用,下一节详讲。命中路径到这就结束了:**没碰磁盘,纯内存操作**。这就是 Buffer Pool 的价值所在。

### 第二步之二:未命中——挑 victim、可能要刷脏、读入

如果 hash 表里没查到(未命中),这个页得从磁盘读。可池子满了怎么办?得**腾一个槽位**出来给新页——这个被腾出来的槽叫 **victim(牺牲品)**。挑 victim 的活,在 `BufferAlloc` 里委托给 `GetVictimBuffer`:

> [src/backend/storage/buffer/bufmgr.c:1664-1665](../postgresql-17.0/src/backend/storage/buffer/bufmgr.c#L1664-L1665)

```c
	victim_buffer = GetVictimBuffer(strategy, io_context);
	victim_buf_hdr = GetBufferDescriptor(victim_buffer - 1);
```

`GetVictimBuffer` 内部干三件事(下一节详讲 Clock-Sweep 怎么挑):① 调 `StrategyGetBuffer` 挑一个 refcount=0 的槽;② **如果这个 victim 是脏页,得先把它写回磁盘**(否则数据就丢了!);③ 把 victim 的 tag 改成新 tag,登记进 hash 表。

改 tag 的代码很能说明问题:

> [src/backend/storage/buffer/bufmgr.c:1721-1742](../postgresql-17.0/src/backend/storage/buffer/bufmgr.c#L1721-L1742)

```c
	/* Need to lock the buffer header too in order to change its tag. */
	victim_buf_state = LockBufHdr(victim_buf_hdr);
	...
	victim_buf_hdr->tag = newTag;
	...
	victim_buf_state |= BM_TAG_VALID | BUF_USAGECOUNT_ONE;
	if (relpersistence == RELPERSISTENCE_PERMANENT || forkNum == INIT_FORKNUM)
		victim_buf_state |= BM_PERMANENT;

	UnlockBufHdr(victim_buf_hdr, victim_buf_state);
```

把旧 tag 换成新 tag——这个槽位从此"换了身份",缓存的是新页了。`BM_TAG_VALID` 表示 tag 已分配,`BM_PERMANENT` 标记它是不是要在每次 checkpoint 时必须写出。到这,槽位准备好了,真正的磁盘 I/O(读入)随后发生。

> **整条读路径串起来**:`ReadBufferExtended → ReadBuffer_common → PinBufferForBlock → BufferAlloc`。命中:查 hash 表 → pin → 完事。未命中:查 hash 表 miss → 挑 victim → 若脏先刷 → 改 tag → 登记新 hash 项 → 从磁盘读入 → 置 `BM_VALID`。**hash 表是入口,Clock-Sweep 决定腾谁,WAL 保证脏 victim 不丢。**

---

## 四、pin(refcount)和 lock:为什么访问页必须先 pin

上一节反复出现"pin"。这是 Buffer Pool 里最容易和"锁"混淆的概念,必须讲清。

### pin = "这页有人在用,别淘汰它"

**pin(钉住)** 的本质是把缓冲区的 **refcount(引用计数)** 加 1。`PinBuffer` 干的就是这件事——而且它**不是加锁**,而是用 CAS 原子地把 refcount +1,顺便把 usage count 也调一下(命中热门页会涨 usage count,下面 Clock-Sweep 那节讲):

> [src/backend/storage/buffer/bufmgr.c:2659-2688](../postgresql-17.0/src/backend/storage/buffer/bufmgr.c#L2659-L2688)

```c
		old_buf_state = pg_atomic_read_u32(&buf->state);
		for (;;)
		{
			if (old_buf_state & BM_LOCKED)
				old_buf_state = WaitBufHdrUnlocked(buf);

			buf_state = old_buf_state;

			/* increase refcount */
			buf_state += BUF_REFCOUNT_ONE;

			if (strategy == NULL)
			{
				/* Default case: increase usagecount unless already max. */
				if (BUF_STATE_GET_USAGECOUNT(buf_state) < BM_MAX_USAGE_COUNT)
					buf_state += BUF_USAGECOUNT_ONE;
			}
			...
			if (pg_atomic_compare_exchange_u32(&buf->state, &old_buf_state,
											   buf_state))
			{
				...
				break;
			}
```

为什么访问一个页前**必须先 pin**?看 Clock-Sweep 挑 victim 的条件就明白了——它**只挑 refcount=0 的槽**淘汰。只要你 pin 了(refcount≥1),这页就不会被淘汰、不会被换出去。这保证你正在读/写的页,底下不会被别人悄悄搬走。

用完要 `ReleaseBuffer`(对应 `UnpinBuffer`),把 refcount 减回去([bufmgr.c:4896-4905](../postgresql-17.0/src/backend/storage/buffer/bufmgr.c#L4896-L4905))。refcount 归零的页,才重新成为淘汰候选。

### lock = "这页的内容我正在改,别人别同时改"

**lock**(`content_lock`)保护的是页**内容**的一致性。pin 只防"被淘汰",**不防别人同时改同一页**。你要改一页,得先 pin(防淘汰),再加 `content_lock` 的**排他锁**(`LW_EXCLUSIVE`),改完再放锁、unpin。读一页则加**共享锁**。

> **不这样会怎样**:假设只 pin 不加 lock。两个事务同时改同一个页,你挪了页头指针、他压缩了 tuple 存储,两边写互相覆盖,页内容就烂了。lock 保证"同一时刻只有一个写者",而多个读者可以共享(读不互斥)。

> **pin 和 lock 的关键区别**:pin 是**长期持有**的(只要你还想用这页,就一直 pin 着,哪怕你只是 hold 着没在操作);lock 是**短期持有**的(只在真正读/写页内容的那一瞬间)。而且有个**锁序铁律:先 pin,后 lock;先 unlock,后 unpin**。绝不能反过来——否则你 lock 着一个没 pin 的页,它可能被别人淘汰换走,你 lock 的就是个已经被覆盖的幽灵页。

pin 还有个隐含价值:它让 hash 表的分区锁可以**早早释放**。前面"一 pin 住就释放 mapping lock"就是这个道理——只要 pin 着,这个 tag 对应的槽就不会变(淘汰要求 refcount=0),分区锁自然不用一直占着。

---

## 五、满了淘汰谁:Clock-Sweep 替换算法

Buffer Pool 是固定大小,热点数据又远多于槽位,**满了必须淘汰**。淘汰策略决定了"哪些页该留、哪些页该走"。这是 Buffer Pool 性能的核心。

### 为什么不用 LRU

最直觉的策略是 **LRU(最近最少使用)**:每次访问都把这个页挪到链表头,淘汰时拿链表尾。LRU 效果好,但有两个硬伤:

> **不这样会怎样(用 LRU 的麻烦)**:① 每次访问都要**移动链表节点**,这要改链表指针、加锁保护链表——在成百上千并发下,这个链表成了全局热点锁,抵消了 LRU 的精度优势;② LRU 怕**全表扫描**。一个查询扫几百万页,这些页被依次访问、依次进 LRU 链表头,把原本的热点页全挤到尾部淘汰了。扫完之后这些扫描页大部分再也用不上,可它们却霸占了缓冲池——这叫"扫描冲刷(scan resistance 失败)"。PG 的解法是 **Clock-Sweep(时钟扫描)**,也叫"二次机会算法"。它**近似 LRU,但廉价得多,还能抵抗扫描冲刷**。

### Clock-Sweep:一根转动的时钟指针

把所有缓冲区槽位想象成一个**环**,有一根**时钟指针(`nextVictimBuffer`)** 在环上转([src/backend/storage/buffer/freelist.c:36-40](../postgresql-17.0/src/backend/storage/buffer/freelist.c#L36-L40)):

```c
	/*
	 * Clock sweep hand: index of next buffer to consider grabbing. Note that
	 * this isn't a concrete buffer - we only ever increase the value. So, to
	 * get an actual buffer, it needs to be used modulo NBuffers.
	 */
	pg_atomic_uint32 nextVictimBuffer;
```

每个槽位有一个 **usage count**(就是 `state` 里那 4 位,范围 0~5)。指针转一圈,对经过的每个槽:

- 如果 **refcount=0 且 usage count=0** → 这就是 victim,用它。
- 如果 **refcount=0 但 usage count>0** → **把它减 1**(给一次"二次机会"),指针继续往前转。
- 如果 **refcount>0**(有人在用) → 跳过,指针继续转。

核心循环在 `StrategyGetBuffer` 里:

> [src/backend/storage/buffer/freelist.c:314-356](../postgresql-17.0/src/backend/storage/buffer/freelist.c#L314-L356)

```c
	/* Nothing on the freelist, so run the "clock sweep" algorithm */
	trycounter = NBuffers;
	for (;;)
	{
		buf = GetBufferDescriptor(ClockSweepTick());

		/*
		 * If the buffer is pinned or has a nonzero usage_count, we cannot use
		 * it; decrement the usage_count (unless pinned) and keep scanning.
		 */
		local_buf_state = LockBufHdr(buf);

		if (BUF_STATE_GET_REFCOUNT(local_buf_state) == 0)
		{
			if (BUF_STATE_GET_USAGECOUNT(local_buf_state) != 0)
			{
				local_buf_state -= BUF_USAGECOUNT_ONE;

				trycounter = NBuffers;
			}
			else
			{
				/* Found a usable buffer */
				...
				*buf_state = local_buf_state;
				return buf;
			}
		}
		else if (--trycounter == 0)
		{
			...
			elog(ERROR, "no unpinned buffers available");
		}
		UnlockBufHdr(buf, local_buf_state);
	}
```

读这段代码,几个关键点:

1. **指针只前进不后退**:`ClockSweepTick` 用原子加把指针加 1([freelist.c:117-118](../postgresql-17.0/src/backend/storage/buffer/freelist.c#L117-L118)),并发安全,不需要全局锁来挪指针。
2. **usage count 给热点页"续命"**:一个页每被命中一次,`PinBuffer` 会把它的 usage count 往上加(最高 5)。所以热点页的 usage count 高,指针转一圈(只减 1)杀不掉它,要转好几圈才轮到它被淘汰——这就是"近似 LRU":访问越频繁,活得越久。
3. **扫描页不会冲刷热点**:全表扫描时,新读进来的页 usage count 初始只有 1(甚至用 ring 策略时更克制,见下),指针转一圈就把它淘汰了;而老热点页 usage count 是 5,要转 5 圈才动它们。扫描页根本抢不过热点——这就是 Clock-Sweep 的**抗扫描**能力,LRU 做不到。

> **为什么 usage count 上限是 5?** 注释说得很直白([src/include/storage/buf_internals.h:71-79](../postgresql-17.0/src/include/storage/buf_internals.h#L71-L79)):上限越大,越接近 LRU 的精度,但找 victim 时可能要转 **上限+1 圈** 才能淘汰一个页,搜索成本变高。5 是"精度"和"淘汰速度"的权衡——既区分得出热点冷点,又不至于转太多圈。

### freelist:还有"从未用过"的槽

系统刚启动时,池里全是空槽,根本不用 Clock-Sweep。PG 维护一个 **freelist(空闲链表)**,空槽挂在上面。`StrategyGetBuffer` 会先从 freelist 拿([freelist.c:268-312](../postgresql-17.0/src/backend/storage/buffer/freelist.c#L268-L312));freelist 空了才转 Clock-Sweep。VACUUM 等操作把不再用的槽还回来时(`StrategyFreeBuffer`),也挂回 freelist。

### BufferAccessStrategy:扫描/批量任务的"小环"

全表扫描、`COPY`、VACUUM 这种一次读海量页的任务,如果让它们的页进主池污染热点,还是有风险。PG 给这类任务一个**私有的小环策略(BufferAccessStrategy)**——它在自己复用一小圈缓冲区(比如 32 个),用完一个换一个,**不污染主池**。`StrategyGetBuffer` 一开始就检查有没有策略环([freelist.c:209-217](../postgresql-17.0/src/backend/storage/buffer/freelist.c#L209-L217))。这是 Clock-Sweep 之外又一道抗扫描保险。

---

## 六、脏页:性能命脉,也是风险点

到目前为止我们都在讲"读"。现在讲"写"——以及它埋下的雷。

### 改一页:先 pin+lock,改完 MarkBufferDirty

一个 `UPDATE` 改某个页,流程是:pin 这个页 → 加排他 `content_lock` → 在内存里改页内容 → **调 `MarkBufferDirty` 把 `BM_DIRTY` 置位** → 放锁 → unpin。`MarkBufferDirty` 的核心就是把脏标志打上:

> [src/backend/storage/buffer/bufmgr.c:2541-2555](../postgresql-17.0/src/backend/storage/buffer/bufmgr.c#L2541-L2555)

```c
	old_buf_state = pg_atomic_read_u32(&bufHdr->state);
	for (;;)
	{
		if (old_buf_state & BM_LOCKED)
			old_buf_state = WaitBufHdrUnlocked(bufHdr);

		buf_state = old_buf_state;

		Assert(BUF_STATE_GET_REFCOUNT(buf_state) > 0);
		buf_state |= BM_DIRTY | BM_JUST_DIRTIED;

		if (pg_atomic_compare_exchange_u32(&bufHdr->state, &old_buf_state,
										   buf_state))
			break;
	}
```

注意 `BM_JUST_DIRTIED`——它标记"在本次写回期间又被改脏了",防止写回线程刚写完、你又改了一笔却没被记录(写回相关的细节下面讲)。**这一步只改了内存,没碰磁盘。** 改动停留在内存里,这就是"脏"的含义:内存里的版本比磁盘上的新。

> **为什么脏页是性能命脉**:如果每次改动都立刻写回磁盘,那就是每次 `UPDATE` 都做一次随机磁盘写——慢到没法用(第 18 章算过这笔账)。把改动攒在内存的脏页里,**延后、批量地写回**,是数据库把"随机写"摊平成"少量顺序写"的关键。没有脏页这个机制,数据库的性能会塌掉一半。

### 为什么脏页是风险点:掉电就丢

脏页的改动只在内存。内存一掉电(或进程崩),所有脏页的改动**全没了**。磁盘上还是旧版本。这直接违背持久性——你已经告诉客户端"更新成功了",结果掉电后数据没了。

> 这正是第 18 章 WAL 存在的理由:**改数据之前,先把改动记进 WAL 日志**。日志落盘了,脏页哪怕还没写回,掉电后也能靠重放日志补回来。所以脏页能"放心地脏着",前提是它的 WAL 已经先落盘了。这一章和第 18 章在这里咬合:**Buffer Pool 主服务"快",但脏页的"不丢"必须靠 WAL 兜底**。

### 脏页什么时候、由谁写回

脏页不会一直脏着。两个角色负责把它们写回磁盘:

1. **后台写进程(bgwriter)**:平时在后台转,趁系统不忙,**提前**把脏页刷出去,给将来腾出干净的 victim(免得淘汰时才现刷、拖慢查询)。入口是 `BgBufferSync`([src/backend/storage/buffer/bufmgr.c:3177](../postgresql-17.0/src/backend/storage/buffer/bufmgr.c#L3177)),由 [BackgroundWriterMain](../postgresql-17.0/src/backend/postmaster/bgwriter.c#L87) 周期性调用。
2. **检查点进程(checkpointer)**:定期强制把**所有**脏页刷盘,为的是划定"日志可以回收到哪"(下一章详讲)。
3. **后端进程自己**:淘汰一个脏 victim 时,如果它还脏着,**当前进程得自己先把它刷干净**才能换走。这就是 `GetVictimBuffer` 里的脏处理。

第三种最关键,因为它直接卡在查询路径上。看 `GetVictimBuffer` 怎么处理脏 victim:

> [src/backend/storage/buffer/bufmgr.c:1979-2042](../postgresql-17.0/src/backend/storage/buffer/bufmgr.c#L1979-L2042)

```c
	if (buf_state & BM_DIRTY)
	{
		LWLock	   *content_lock;
		...
		/*
		 * We need a share-lock on the buffer contents to write it out ...
		 * We must use a conditional lock acquisition here to avoid deadlock.
		 */
		content_lock = BufferDescriptorGetContentLock(buf_hdr);
		if (!LWLockConditionalAcquire(content_lock, LW_SHARED))
		{
			/* Someone else has locked the buffer, so give it up and loop back. */
			UnpinBuffer(buf_hdr);
			goto again;
		}
		...
		/* OK, do the I/O */
		FlushBuffer(buf_hdr, NULL, IOOBJECT_RELATION, io_context);
		LWLockRelease(content_lock);
```

读这段有几个要点:

- **脏 victim 必须先刷再换**:不刷就把别人的改动覆盖丢了。
- **用条件锁防死锁**:写回只需共享锁(写的时候别人也能读 hint bit),但要**条件获取**——拿不到就放弃这个 victim 换下一个。注释专门讲了:如果不加条件、硬等这把锁,两个 backend 可能互相等对方的页,死锁。
- **真正干写盘的是 `FlushBuffer`**。

### FlushBuffer:WAL 铁律的真身

`FlushBuffer` 是把一个脏页写回磁盘的函数。它最关键的一步,是把第 18 章那句"**日志必须先于数据落盘**"在代码里强制执行:

> [src/backend/storage/buffer/bufmgr.c:3819-3837](../postgresql-17.0/src/backend/storage/buffer/bufmgr.c#L3819-L3837)

```c
	/*
	 * Force XLOG flush up to buffer's LSN.  This implements the basic WAL
	 * rule that log updates must hit disk before any of the data-file changes
	 * they describe do.
	 */
	if (buf_state & BM_PERMANENT)
		XLogFlush(recptr);
```

注释一字一句:"log updates must hit disk before any of the data-file changes they describe do"。在写数据页之前,先 `XLogFlush(recptr)`——把这个页的 LSN(`pd_lsn`,第 6 章页头那个字段)对应的 WAL 强制刷盘。如果 WAL 还没落盘就先写了数据页,掉电后数据页是新版本但日志是旧的,redo 就缺依据,数据就错了。**这一行 `XLogFlush`,就是 WAL write-ahead rule 在 Buffer Pool 里的强制点。**

> **把"快 vs 不丢"在这里看清楚**:脏页让写操作"快"(攒内存、延后写盘),`FlushBuffer` 里的 `XLogFlush` 让脏页"不丢"(刷数据前先保日志)。Buffer Pool 这一站,一只手服务"快",另一只手和第 18 章的 WAL 紧紧握着,保证"不丢"。这正是全书主线在这一个机制上的具象化。

### I/O 串行化:BM_IO_IN_PROGRESS

一个页同一时刻只能有一个 I/O 在做。`FlushBuffer` 开头调 `StartBufferIO`:

> [src/backend/storage/buffer/bufmgr.c:5541-5559](../postgresql-17.0/src/backend/storage/buffer/bufmgr.c#L5541-L5559)

```c
		if (!(buf_state & BM_IO_IN_PROGRESS))
			break;
		...
	/* Once we get here, there is definitely no I/O active on this buffer */

	if (forInput ? (buf_state & BM_VALID) : !(buf_state & BM_DIRTY))
	{
		/* someone else already did the I/O */
		UnlockBufHdr(buf, buf_state);
		return false;
	}

	buf_state |= BM_IO_IN_PROGRESS;
```

置上 `BM_IO_IN_PROGRESS`,别人再想对这页做 I/O 就得 `WaitIO` 等着。这避免了两个进程同时读同一个页进来的浪费,也避免写回撞车。

---

## 七、双缓冲:shared_buffers 和 OS 文件缓存的配合

有个绕不开的问题:操作系统自己也有文件系统缓存(page cache)。PG 从磁盘读一页,`read()` 其实常常是从 OS 缓存里拿的,不一定真打磁盘。这就形成了**双缓冲(double buffering)**——一份数据,OS 缓存里一份,PG 的 `shared_buffers` 里一份。

> **不这样会怎样(为什么还留着 OS 缓存)**:有人会问,既然 PG 自己有缓冲池,为什么不让 PG 直接读写裸设备、绕过 OS 缓存省一份内存?PG 选择的是**用普通文件 I/O**(不默认用 `O_DIRECT`)。好处是开发简单、跨平台、能借 OS 的预读和写合并优化;代价是同一份页可能占两份内存。这在 `shared_buffers` 不大时还能接受,但当 `shared_buffers` 很大、OS 缓存也很大时,确实有内存浪费。这是 PG 的一个历史权衡(一些数据库如 Oracle/InnoDB 提供直接 I/O 选项)。PG 社区长期的经验法则是:`shared_buffers` 设到物理内存的 **1/4** 左右,剩下的留给 OS 缓存——让两层缓存各司其职,而不是把全部内存都塞给 `shared_buffers`。

---

## 八、shared_buffers 为什么不是越大越好

新手常以为"`shared_buffers` 越大,命中率越高,性能越好"。**这是错的。** 调大有几个明显的副作用:

> **不这样会怎样(无脑调大的代价)**:
> 1. **脏页刷盘压力变大**:池子越大,攒的脏页越多。checkpoint 到点要刷的脏页就越多,一旦来不及,checkpoint 会"拖尾",下一个 checkpoint 还没开始就被迫延长——引发**检查点抖动**(写盘突刺,I/O 被打满,查询卡顿)。第 19 章会详讲这个。
> 2. **和 OS 缓存争内存**:上面说过,把内存全给 `shared_buffers`,OS 文件缓存就被挤掉。PG 读一个不在池里的页,本来能从 OS 缓存秒拿,现在得真打磁盘——反而变慢。
> 3. **启动/崩溃恢复变慢**:池子大,初始化和管理开销略增;更重要的是 checkpoint 范围大,崩溃后重放的脏页多。
> 4. ** diminishing returns**:命中率到了 99% 以后,再加内存只换来 0.1% 的提升,投入产出比极差。

所以生产环境的经验值是物理内存的 **25% 左右**,而不是越大越好。看命中率该看 `pg_stat_database` 的 `blks_hit / (blks_hit + blks_read)`,而不是盲目加内存。

---

## 关键源码精读:BufferAlloc——读路径的总指挥

把本章最核心的函数 `BufferAlloc` 完整梳理一遍。它把"查 hash 表 → 命中 / 未命中 → 挑 victim → 登记"串成一条线,是理解 Buffer Pool 的钥匙。源码在 [src/backend/storage/buffer/bufmgr.c:1594-1752](../postgresql-17.0/src/backend/storage/buffer/bufmgr.c#L1594-L1752),前面已分段贴过,这里理清它的三条分支:

**分支一——命中(tag 已在 hash 表)**:第 [1619-1651](../postgresql-17.0/src/backend/storage/buffer/bufmgr.c#L1619-L1651) 行。拿到 shared partition lock → `BufTableLookup` 命中 → `PinBuffer` 钉住 → 立刻放锁 → `*foundPtr = true` 返回。**全程无磁盘 I/O,无淘汰。** 这是热点路径,设计目标就是让它尽量短、尽量少持锁。

**分支二——未命中,挑 victim 装新页**:第 [1657-1752](../postgresql-17.0/src/backend/storage/buffer/bufmgr.c#L1657-L1752) 行。先放掉 shared lock(挑 victim 耗时,不能一直占着)→ `GetVictimBuffer` 挑一个干净的(脏的在它内部已经刷过了)→ **重新拿 exclusive partition lock** → `BufTableInsert` 登记新 tag → 改 victim 的 tag、置 `BM_TAG_VALID` → 放锁 → `*foundPtr = false` 返回(调用方随后负责把页从磁盘读入)。

**分支三——竞态:你挑 victim 的工夫,别人也插了这个 tag**:第 [1672-1719](../postgresql-17.0/src/backend/storage/buffer/bufmgr.c#L1672-L1719) 行。`BufTableInsert` 发现这个 tag 已经被别人插进去了(返回别人的 buf_id)。这时:放弃自己的 victim(`UnpinBuffer` + `StrategyFreeBuffer` 还回 freelist),改用别人插好的那个。这处理了一个常见的并发竞态——两个 backend 同时 miss 同一页,谁也别重复读磁盘。

> **锁的升级是关键设计**:查的时候用**共享锁**(LW_SHARED,多个读者可并发);只有要插入新条目时才升级为**排他锁**(LW_EXCLUSIVE)。而且——**一旦 pin 住就立刻放锁**。这两条合起来,把锁的持有时间和范围压到最小。在成百上千并发的数据库里,锁的粒度就是性能的命脉,这里每一行都在为"少持锁、持小锁"服务。

这段代码还体现了 PG 处理并发的哲学:**乐观重试**。它不去预先协调"谁负责读这页",而是让大家各自往前冲,撞上了(victim 被别人占了、tag 被别人插了)就回退重来。这种"先干,撞了再修"的风格,在低冲突场景下比"先锁住再干"快得多。

---

## 章末小结

- **Buffer Pool 是内存里缓存磁盘页的固定数组**(NBuffers 个槽 + 一张 hash 表),读写都先在内存里搞——这是对付"磁盘慢"的第一道防线,服务全书主线里"让数据**快**"那一侧。
- **每个槽有个 BufferDesc**:`tag`(缓存的是哪个磁盘页)、`state`(标志位 + refcount + usage count 三合一打包进 32 位整数,便于无锁 CAS)、`content_lock`(保护页内容)。
- **读一个页的路径**:查 Buffer Mapping hash 表 → 命中:pin 了就用 / 未命中:挑 victim(脏的先 `FlushBuffer` 刷盘,里面 `XLogFlush` 强制日志先落盘)→ 改 tag → 登记新 hash 项 → 从磁盘读入。
- **pin(refcount)防淘汰,lock 防内容冲突**:访问页必须先 pin(refcount+1,让 Clock-Sweep 跳过它);改页才加 content_lock。锁序:先 pin 后 lock,先 unlock 后 unpin。
- **Clock-Sweep 近似 LRU 但更廉价**:指针转圈,usage count>0 的页减 1 给二次机会、usage count=0 且 refcount=0 的页被淘汰。usage count 上限 5,权衡精度与淘汰速度。**抗扫描**:扫描页 usage count 低,转一圈就被淘汰,抢不过热点。
- **脏页是性能命脉也是风险点**:改动攒在内存(快),但掉电就丢——靠 `FlushBuffer` 里"刷数据前先 `XLogFlush` 刷日志"保证不丢(和第 18 章 WAL 咬合)。
- **bgwriter/checkpointer/后端自己**三个角色都能写回脏页;淘汰脏 victim 时由当前后端现刷。
- **shared_buffers 不是越大越好**:脏页刷盘压力、checkpoint 抖动、和 OS 缓存争内存,经验值是物理内存的 1/4。

### 回扣主线

本章是对"**磁盘慢、内存快但易失**"这一本性里"**慢**"那一半的正面回应——**用内存换速度**。Buffer Pool 让绝大多数读写落在内存,把"磁盘十万倍慢"的鸿沟填平。但内存易失,脏页的改动掉电就没——所以这一章的另一只手,死死扣住第 18 章的 WAL:`FlushBuffer` 里那句 `XLogFlush(recptr)`,就是"快"和"不丢"两条主线在一个函数里交汇的瞬间。Buffer Pool 自己只保证"快",持久性永远是 WAL 给的。

至于数据的**三本性**:本章主要服务"磁盘慢"——缓存换速度;同时脏页/淘汰又和"易失"(要刷盘、要 WAL)纠缠;而 pin/lock 这套机制,则是"共享会冲突"在页粒度的初现(P4 锁那一章会把并发冲突讲到底)。

### 想继续深入

- 缓冲区管理全集:[src/backend/storage/buffer/bufmgr.c](../postgresql-17.0/src/backend/storage/buffer/bufmgr.c)(`ReadBufferExtended`、`BufferAlloc`、`GetVictimBuffer`、`FlushBuffer`、`PinBuffer`、`UnpinBuffer`)。
- Clock-Sweep 与策略:[src/backend/storage/buffer/freelist.c](../postgresql-17.0/src/backend/storage/buffer/freelist.c)(`StrategyGetBuffer`、`ClockSweepTick`、`BufferAccessStrategy` 的 ring)。
- hash 表映射:[src/backend/storage/buffer/buf_table.c](../postgresql-17.0/src/backend/storage/buffer/buf_table.c)(`BufTableLookup`/`Insert`/`Delete`)。
- 描述符与标志位:[src/include/storage/buf_internals.h](../postgresql-17.0/src/include/storage/buf_internals.h)(`BufferDesc`、`BufferTag`、`BM_*` 标志、`BM_MAX_USAGE_COUNT`)。
- 后台写:[src/backend/postmaster/bgwriter.c](../postgresql-17.0/src/backend/postmaster/bgwriter.c) 的 `BackgroundWriterMain` 和 `BgBufferSync`。

---

> Buffer Pool 让热点页留在内存,可往一张表里塞新行时,数据库怎么知道"哪个页还有空位能塞"?要是每张表都维护一张"哪页空着"的小地图,塞行就不用挨个页去试。翻开 **第 9 章 · FSM 与 VM:空闲空间映射与可见性映射**——两个不起眼却必不可少的辅助结构。
