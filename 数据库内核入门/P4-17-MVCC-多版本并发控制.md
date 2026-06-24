# 第 17 章 · MVCC:多版本并发控制

> **前置**:你需要先读过[第 16 章《锁》](P4-16-锁-表锁行锁与死锁.md)。锁能防住"写写冲突"(两个事务抢着改同一行,排队),但用锁去做"读写隔离"——读者为了不让别人改,加共享锁;写者为了不让别人读,加排他锁——这条路在数据库里走不通:它会退化成"读挡写、写挡读",并发度塌方。PG 实现高并发的真正杀器不是锁,而是 **MVCC(Multi-Version Concurrency Control,多版本并发控制)**——让读永远不阻塞写、写永远不阻塞读。本章是 P4 的压轴,也是全书最难、最精华的一章。

> **核心问题**:PG 凭什么能做到"读不阻塞写、写不阻塞读"?答案是 **MVCC**:UPDATE/DELETE 不"就地改"数据,而是**产生一个新版本、把旧版本标记成"已被某个事务改过"**;每个事务用自己的"快照(snapshot)"只看到"它开始那一刻已经提交的版本"。具体——
>
> - tuple 头上的 `t_xmin`(插入它的事务号)/ `t_xmax`(删除/更新它的事务号)怎么标记一个版本的"生"与"灭"?
> - 快照里记录什么(`xmin` 下界、`xmax` 上界、`xip[]` 正在跑的事务列表),才能回答"这行对我可见吗"?
> - 可见性判定函数 `HeapTupleSatisfiesMVCC` 怎么综合 tuple 的 `xmin`/`xmax` + 事务状态(clog:已提交/已回滚)+ 快照,三段式判定"这行我到底看不看得见"?
> - hint bit(提示位)为什么是 MVCC 性能的隐形加速器(避免每次可见性判定都去磁盘查 clog)?
> - 旧版本(死元组 dead tuple)谁来清(埋 P6 VACUUM 的伏笔)?
> - 事务号是 32 位、会用完,为什么要"冻结(freeze)"老版本(把 `t_xmin` 改成 `FrozenTransactionId`)防止事务号回绕(wraparound)?

> **读完本章你会明白**:一次 UPDATE 实际做了什么(旧行打 `xmax`、新行打 `xmin`,两个物理版本并存——这就是读写互不阻塞的根源);同一逻辑行的多个物理版本怎么用 `t_ctid` 串成一条链;可见性判定的核心三问(① `xmin` 对应事务提交了吗?② 它是不是在我快照之前提交?③ `xmax` 是不是已提交且对我可见?);hint bit 如何把"查 clog 才知道的事务状态"缓存进 tuple 头;以及 MVCC 的两笔长期账——死元组堆积(要 VACUUM)和事务号回绕(要冻结)。

> **如果一读觉得太难**:先只记三件事——
> ① **版本 = (xmin, xmax) 一对事务号**:`xmin` 插入它、`xmax` 删/改它。`xmin` 已提交且早于我的快照、`xmax` 未提交或晚于我的快照 → 这行我**看得见**;否则看不见。
> ② **hint bit 是缓存**:tuple 头里有几个 bit,记着"`xmin` 这事务到底提交没",下次扫到就不用去 clog 磁盘查了。
> ③ **MVCC 不是免费的**:旧行不会立刻消失(死元组,要 VACUUM);事务号会用完(要冻结)。这两笔账是 P6 的主线。
> 其余细节(状态机的每个分支、冻结的具体规则)第二遍配合源码再抠,不影响往下读 P5。

---

## 一、先看清 MVCC 在解决什么:读写隔离不能靠锁

第 16 章我们讲锁管"写写冲突"——两个事务抢着改同一行,锁让它们排队。但数据库里更常见的并发模式不是"两个人抢着写",而是 **"一个人在写,另一个人在读"**:

```text
事务 A (长查询,做报表)              事务 B (写)
BEGIN;                               BEGIN;
SELECT count(*), sum(balance)
        FROM accounts WHERE ...;
        -- A 正在大范围扫描账户表
                                     UPDATE accounts SET balance = balance + 100
                                             WHERE id = 1;
                                     COMMIT;   -- B 改完提交了
        -- A 还没扫完...
COMMIT;
```

A 是个长事务,正在扫整张账户表;B 这时改了其中一行(`id=1`)并提交。问题来了:**A 该不该看到 B 的改动?A 该不该被 B 阻塞?**

### 不这样会怎样:用锁做读写隔离,并发度塌方

> **不这样会怎样**:如果靠锁来隔离读写,A 扫表时给扫过的每一行加**共享锁(S 锁)**,"我要读这行,别人不许改";B 想 `UPDATE id=1`,要先拿那行的**排他锁(X 锁)**——可那行正被 A 用 S 锁占着,B 被挡住,**只能等 A 整个扫完**。反过来也一样:B 改完一行还没提交,A 想读就被挡住,等 B 提交或回滚。

这种"读挡写、写挡读"在数据库里是灾难性的:

- **报表/分析场景**:一个统计全表的长查询可能跑几分钟到几小时,这期间**整张表的所有写全被堵死**——线上业务直接卡死。
- **热点行**:一行被频繁写(计数器、库存),任何读它的人都被写者挡住,反之亦然。
- **锁膨胀**:读要给扫过的每一行加锁,几亿行的表加几亿把锁,内存爆。

也就是说,**锁这套机制对"写写"很合适(必须有人排队),对"读写"太重了**——读写本不该互相挡路:读者只是看一眼数据,它不改变数据;写者改的是新值,只要读者看的是"改之前的某个一致版本",两者完全可以并行。

### 所以这样设计:不就地改,产生新版本,各看各的版本

MVCC 的核心思想,一句话:

> **更新一行,不是把旧值覆盖成新值,而是写一个全新的物理版本,把旧版本留着;删除一行,不是抹掉,而是给旧版本打个"已被删"的标记。每个事务根据自己的快照,只看到它该看到的版本。**

回到上面那个例子,A、B 并发时,底层物理上发生了什么:

```text
账户 id=1, 旧版本(余额=100)            新版本(余额=200)
┌─────────────────────────────┐       ┌─────────────────────────────┐
│ xmin = X (某事务,很久前已提交)│  ───►  │ xmin = B  (这次 UPDATE 的事务)│
│ xmax = B  (被事务 B 改了)     │  t_ctid │ xmax = 0  (还活着,没被谁改)   │
│ 数据: balance = 100         │  链     │ 数据: balance = 200         │
└─────────────────────────────┘       └─────────────────────────────┘
```

- 旧版本没被删,**还在页里**,只是 `t_xmax` 被打上了 B 的事务号(意思是"我已被事务 B 改/删了")。
- 新版本**单独存在**别处,`t_xmin` 是 B 的事务号(意思是"我是事务 B 插入的")。
- 旧版本的 `t_ctid` 指向新版本(下面讲版本链)。

现在,A、B 各看哪个版本,**全凭各自的快照**:

- **事务 A**(在 B 改之前就开了事务,快照拍得早):它的快照"不知道 B 存在"(B 是 A 之后才提交的)。所以 A 扫到旧版本——它的 `t_xmax=B`,而 B 在 A 的快照里"还没提交"——A 判定"这行没被删,我看见",读到的还是 **余额=100**。
- **事务 C**(在 B 提交之后才开事务,快照拍得晚):它的快照"知道 B 已经提交"。C 扫到旧版本——`t_xmax=B` 已提交——判定"这行已被删,跳过";顺着 `t_ctid` 找到新版本——`t_xmin=B` 已提交且早于 C 的快照——判定"这行是活的,我看见",读到 **余额=200**。

> **关键洞察:读者和写者根本没有共享同一份数据。** 写者写新版本(在页里另起一摊),读者读旧版本(还在原位)。两人操作的物理内存/磁盘位置不同,自然不互相阻塞——**这就是"读不阻塞写、写不阻塞读"的根源**。一个 `SELECT` 拿到快照后,扫表只读不改,根本不需要加任何会挡住写者的锁(它只拿最弱的 `AccessShareLock`,那只是为了防止别人 `DROP TABLE`)。

> 用个**一次性小比喻**:MVCC 就像事务**进门那一刻拍下的一张照片**——照片里定格了"此刻哪些事务已提交"。一个事务拿着这张照片去翻数据:凡是在照片拍摄时已提交的改动它看得见,在拍摄时还在跑或还没开始的事务的改动它看不见。照片一旦拍下就不再变,后面新提交的事务对它等于不存在。**这就是第 15 章讲的"快照(snapshot)",本章我们把这张照片和 tuple 头里的 `xmin`/`xmax` 怎么咬合,彻底讲透。**

---

## 二、版本的生灭:`t_xmin` / `t_xmax` / `t_ctid`

第 7 章我们埋过伏笔:每行 tuple 头里都有 `t_xmin`/`t_xmax`/`t_cid`/`t_ctid` 这四个"看不见的列",是为 MVCC 准备的。现在把它们彻底拆开——这是 MVCC 全部机制的物理基础。

先回顾结构(P2 第 7 章已贴,这里只重申关键字段):

> [src/include/access/htup_details.h:122-132](../postgresql-17.0/src/include/access/htup_details.h#L122-L132)

```c
typedef struct HeapTupleFields
{
	TransactionId t_xmin;		/* inserting xact ID */
	TransactionId t_xmax;		/* deleting or locking xact ID */

	union
	{
		CommandId	t_cid;		/* inserting or deleting command ID, or both */
		TransactionId t_xvac;	/* old-style VACUUM FULL xact ID */
	}			t_field3;
} HeapTupleFields;
```

四个逻辑概念(源码注释把五个塞进三个物理字段):

- **`t_xmin`(inserting xact ID)**——**插入这行的事务号**。每行都知道自己是被哪个事务"生"出来的。这是版本的"出生证"。
- **`t_xmax`(deleting or locking xact ID)**——**删除或锁定这行的事务号**。一行被删/被 UPDATE,不是抹掉,而是给 `t_xmax` 打上"已被某某事务改过"。这是版本的"死亡证"。
- **`t_cid`(CommandId)**——事务内第几条命令。第 7 章讲过,用来区分"自己事务里前面的命令改的、还是后面的命令改的",决定"一个事务能不能看见自己前面命令的改动"。
- **`t_xvac`**——和老式 VACUUM FULL 配合,和 `t_cid` 共用一个 union(两者不会同时需要)。

版本的一生,就是 `t_xmin`/`t_xmax` 这两个字段的演化:

```text
       出生(INSERT/UPDATE 产生)           灭亡(DELETE/UPDATE 覆盖)
              │                                      │
              ▼                                      ▼
        t_xmin = 事务X (生它的人)           t_xmax = 事务Y (改/删它的人)
        t_xmax = 0 (无效,还没被改)         (版本自此"死亡",等人回收)
```

### 一个版本,什么时候算"活着"、什么时候算"死了"?

判定一个 tuple 版本"是否对这个事务可见",本质上就是回答两个问题(这就是后面 `HeapTupleSatisfiesMVCC` 的骨架):

1. **"它出生了吗?"**——看 `t_xmin`:插入它的事务(`t_xmin`),有没有提交?是不是在我快照之前提交?**是 → 这行诞生于我能看到的世界;否 → 这行我根本看不到**(可能是别的事务还没提交就插的,我不能看脏数据)。
2. **"它死了吗?"**——看 `t_xmax`:删除/更新它的事务(`t_xmax`),有没有提交?是不是在我快照之前提交?**是 → 这行已经死了,我跳过;否 → 它还活着,我看得见**。

把这两问合起来,一个 tuple 对某事务的可见性,就是四个状态的组合:

| `t_xmin` 对我 | `t_xmax` 对我 | 结论 |
|---|---|---|
| 已提交 & 早于我快照 | 未提交 / 晚于我快照 / 无效 | **可见**(出生了,还没死) |
| 已提交 & 早于我快照 | 已提交 & 早于我快照 | **不可见**(出生过,但已死) |
| 未提交 / 晚于我快照 | (任何) | **不可见**(我看不到别人没提交的插入) |

> 注意第一行和第三行的不对称:出生要"已提交且早于我",死亡也要"已提交且早于我"。两者都满足,版本才算"活在我能看见的区间里"。这就是 MVCC 可见性的全部内核——剩下的复杂度,全在"怎么判断'已提交且早于我快照'"这个细节上(下一节讲)。

### `t_ctid`:把多个物理版本串成一条链

一个逻辑行(比如 `id=1` 的账户)被 UPDATE 多次后,物理上会有**多个版本并存**:V0(最老)→ V1 → V2 → V3(最新)。它们怎么组织?靠 `t_ctid`:

> [src/include/access/htup_details.h:161-162](../postgresql-17.0/src/include/access/htup_details.h#L161-L162)

```c
	ItemPointerData t_ctid;		/* current TID of this or newer tuple (or a
								 * speculative insertion token) */
```

`t_ctid`(current TID)是个 6 字节的行指针(块号 + 行号)。规则:

- **新插入的 tuple**:`t_ctid` 指向自己(表示"我就是最新版本")。
- **被 UPDATE 后**:旧版本的 `t_ctid` 改成指向新版本的物理位置,新版本的 `t_ctid` 指向自己。

这样,旧版本 → 新版本串成一条链。源码注释把判定的捷径说得很清楚:

> [src/include/access/htup_details.h:86-94](../postgresql-17.0/src/include/access/htup_details.h#L86-L94)
> "a tuple is the latest version of its row iff XMAX is invalid or t_ctid points to itself"

也就是:**判断一行是不是"最新版本",看 `t_xmax` 无效 或 `t_ctid` 指向自己**。

画一张完整的"版本链"示意图(这是理解 MVCC 物理形态最该记住的一张图):

```text
逻辑行:账户 id=1(被 UPDATE 3 次,从余额 100 → 150 → 200 → 300)

页P,槽1                          页Q,槽5                      页R,槽3
┌────────────────────────┐       ┌────────────────────────┐    ┌────────────────────────┐
│ 版本 V0                │       │ 版本 V1                │    │ 版本 V2 (最新,V3 还没写) │
│ xmin = A (已提交,很久) │       │ xmin = B (已提交)      │    │ xmin = D (已提交)       │
│ xmax = B (被 B 改了)    │ ─t_ctid─►│ xmax = C (被 C 改了) │ ─t_ctid─►│ xmax = 0 (活着)         │
│ t_ctid → 页Q槽5         │       │ t_ctid → 页R槽3        │    │ t_ctid → 自己(页R槽3)    │
│ data: balance=100      │       │ data: balance=150      │    │ data: balance=300       │
└────────────────────────┘       └────────────────────────┘    └────────────────────────┘
```

- 每个版本都知道自己的"生父"(`xmin`)和"凶手"(`xmax`)。
- 顺着 `t_ctid`,可以从最老的版本一路追到最新版本。
- **不同事务,顺着这条链各看各的版本**:一个老事务可能停在 V0(它快照里 B 还没提交,V0 的 `xmax=B` 对它"未生效",V0 还活着,读到 100);一个新事务一路追到 V3(读到 300)。**同一逻辑行,不同事务读到不同物理版本——这就是"多版本"。**

> **为什么旧版本不立刻删?** 因为可能有"老事务"还需要读它(它们的事务开始得早,快照里这个旧版本还活着)。一删,正在读它的事务就读到错乱。所以旧版本必须留着,**等所有可能读它的事务都结束后**,才能回收(这就是 VACUUM,P6)。

---

## 三、快照:决定"我能看到哪些版本"的那张清单

`xmin`/`xmax` 是 tuple 的"身份证",但光有身份证还不够——一个事务扫到这行,还得知道"这行身份证上的事务号(X),跟我是什么关系"。这个"关系",由事务自己的**快照(snapshot)**回答。第 15 章我们已经从隔离级别角度讲过快照("进门时拍的照片"),这里把它的字段和"怎么和 tuple 的 xmin/xmax 比对"彻底讲透。

### 快照里装了什么:`SnapshotData` 的三个核心字段

> [src/include/utils/snapshot.h:142-217](../postgresql-17.0/src/include/utils/snapshot.h#L142-L217)

```c
typedef struct SnapshotData
{
	SnapshotType snapshot_type; /* type of snapshot */

	/*
	 * The remaining fields are used only for MVCC snapshots, and are normally
	 * just zeroes in special snapshots.  (But xmin and xmax are used
	 * specially by HeapTupleSatisfiesDirty, and xmin is used specially by
	 * HeapTupleSatisfiesNonVacuumable.)
	 *
	 * An MVCC snapshot can never see the effects of XIDs >= xmax. It can see
	 * the effects of all older XIDs except those listed in the snapshot. xmin
	 * is stored as an optimization to avoid needing to search the XID arrays
	 * for most tuples.
	 */
	TransactionId xmin;			/* all XID < xmin are visible to me */
	TransactionId xmax;			/* all XID >= xmax are invisible to me */

	/*
	 * For normal MVCC snapshot this contains the all xact IDs that are in
	 * progress, unless the snapshot was taken during recovery in which case
	 * it's empty. ...
	 *
	 * note: all ids in xip[] satisfy xmin <= xip[i] < xmax
	 */
	TransactionId *xip;
	uint32		xcnt;			/* # of xact ids in xip[] */
	...
	CommandId	curcid;			/* in my xact, CID < curcid are visible */
	...
} SnapshotData;
```

抓住三组字段就够:

**第一组:可见性的核心——`xmin` / `xmax` / `xip[]`**。这三个字段,共同定义了"这张快照能看见哪些事务的改动"。注释把规则说得很清楚:

- **`xmin`(下界)**——"所有事务号 `< xmin` 的,**都已提交,我看得见**"。这是个**优化**:大多数事务号都比 `xmin` 小(早就提交了),不必逐个查 `xip[]`,直接判 `< xmin` 就可见。
- **`xmax`(上界)**——"所有事务号 `≥ xmax` 的,**在我拍快照之后才开始,我看不见**"。
- **`xip[]`(in-progress 列表)**——"在 `xmin` 和 `xmax` 之间的,要查这个数组:在列表里的(还在跑、没提交),**我看不见**;不在列表里的(已提交),**我看得见**"。

`xmin` 是"所有还在跑的事务里最小的事务号"——比它小的事务都不在跑(已提交完),所以直接判可见。`xmax` 是"最近已完成事务号 + 1",也就是"下一个即将开始的事务号"——比它大的事务,拍快照时还没开始。

> 注意这里的**命名陷阱**:tuple 头里的 `t_xmin`/`t_xmax`(指插入/删除这行的事务号)和**快照**里的 `xmin`/`xmax`(指可见性的下界/上界)**是完全不同的东西**,只是名字撞了。读源码时要分清"这是 tuple 的字段,还是 snapshot 的字段"。判断可见性时,正是要把 tuple 的 `t_xmin`/`t_xmax` 拿来和 snapshot 的 `xmin`/`xmax`/`xip[]` 比较。

**第二组:命令级可见性——`curcid`**。`CommandId` 是事务内每条 SQL 的编号。同一个事务里,自己刚 `INSERT` 的行,下一条 `SELECT` 应该能看到——这个"自己的改动对自己可见"靠的就是 `curcid`:命令号(CID)比当前命令号小的(这条命令之前产生的改动),对自己可见。

**第三组:子事务——`subxip[]`**。子事务(savepoint 产生的,第 14 章讲过)的在跑列表,逻辑类似 `xip[]` 但针对子事务。

### 快照是怎么拍出来的:`GetSnapshotData`

光有结构不够,得看它怎么被填出来。`GetSnapshotData()`(注意:实现在 `procarray.c`,第 15 章贴过)做的事是——**锁住进程数组,扫一遍所有正在运行的后端进程,收集它们的事务号,据此算出 `xmin`、`xmax`、`xip[]`**。

它最核心的两步(第 15 章已贴,这里只重申结论):

> [src/backend/storage/ipc/procarray.c:2249-2260](../postgresql-17.0/src/backend/storage/ipc/procarray.c#L2249-L2260)

```c
	/* xmax is always latestCompletedXid + 1 */
	xmax = XidFromFullTransactionId(latest_completed);
	TransactionIdAdvance(xmax);
	Assert(TransactionIdIsNormal(xmax));

	/* initialize xmin calculation with xmax */
	xmin = xmax;
```

- **`xmax` = 最近已完成事务号 + 1**。为什么?"最近已完成的事务 +1"就是"下一个即将开始的事务号"——比这大的事务,在我拍快照时还没开始,它们的改动我当然看不见。所以 `xmax` 卡在"现在"这个时间点上。
- **`xmin` 初始化为 `xmax`**,然后往下扫,凡是在跑的事务号比当前 `xmin` 小的,就把它降下来。循环走完,`xmin` 就是"所有在跑事务里最小的事务号"——比它还小的事务都已提交完(没在跑列表里),所以 `< xmin` 直接判可见。

> **快门按下的瞬间**:`GetSnapshotData` 拿 `ProcArrayLock`(进程数组锁),扫一遍所有后端进程,把"现在谁在跑"这张照片定格。照片拍完,以后无论外面世界怎么变(新事务开始、老事务提交),你拿的始终是这张照片——这就是 MVCC 让"读不阻塞写"的根基。

### 快照什么时候拍、用多久:RC vs RR

第 15 章讲过,RC(读已提交)和 RR(可重复读)的差别,不是机制不同,而是**快照拍几张、用多久**:

| | 读已提交 RC | 可重复读 RR |
|---|---|---|
| 快照何时拍 | **每条 SQL 语句**开始时各拍一张 | 事务**第一条语句**开始时拍一张,贯穿整事务 |
| 看到别人的新提交吗 | 看到 | 看不到 |
| 代价 | 每条语句重新扫"谁在跑" | 拍一次,后续免单 |

判定的开关是宏 `IsolationUsesXactSnapshot()`(隔离级别 ≥ RR 时为真),见 [src/backend/utils/time/snapmgr.c:215-283](../postgresql-17.0/src/backend/utils/time/snapmgr.c#L215-L283) 的 `GetTransactionSnapshot()`(第 15 章详讲)。本章不重复,只需记住:**快照是可见性判定的输入,RC/RR 只是输入的更新频率不同**。

---

## 四、clog:事务到底提交了没有,查这本"账本"

可见性判定还有一个绕不开的依赖:**"某个事务号(X)到底提交了没有?"** tuple 的 `t_xmin`/`t_xmax` 只记着事务号,但事务号本身不会告诉你它提交没——这信息存在另一个地方,叫 **clog(commit log,提交日志)**。

### clog 是什么:每个事务 2 个 bit 的状态位图

clog 是一个**紧凑的位图**,记录每个事务的提交状态。每个事务占 **2 个 bit**(所以有 4 种状态):

> [src/include/access/clog.h:25-30](../postgresql-17.0/src/include/access/clog.h#L25-L30)

```c
typedef int XidStatus;

#define TRANSACTION_STATUS_IN_PROGRESS		0x00
#define TRANSACTION_STATUS_COMMITTED		0x01
#define TRANSACTION_STATUS_ABORTED			0x02
#define TRANSACTION_STATUS_SUB_COMMITTED	0x03
```

四个状态:进行中(`0x00`,初始值,因为页是全零初始化的)、已提交(`0x01`)、已回滚(`0x02`)、子事务已提交但父还没定(`0x03`,过渡态)。

因为每事务 2 bit,一个 8KB 页能塞下 `8192 × 8 / 2 = 32768` 个事务的状态——非常紧凑。这套页存在磁盘上的 `pg_xact/` 目录下(由 SLRU 管理,和 `pg_xlog`/WAL 是两回事,别混):

> [src/backend/access/transam/clog.c:811-812](../postgresql-17.0/src/backend/access/transam/clog.c#L811-L812)

```c
	SimpleLruInit(XactCtl, "transaction", CLOGShmemBuffers(), CLOG_LSNS_PER_PAGE,
				  "pg_xact", LWTRANCHE_XACT_BUFFER,
```

`"pg_xact"` 就是磁盘目录名。`SimpleLruInit` 把它包成一个 SLRU(Simple Least Recently Used)结构——内存里留若干页做缓存,冷页落盘到 `pg_xact/`。

### 不这样会怎样:把提交状态塞进 tuple 头?

> **不这样会怎样**:假设把"这事务提交没"直接塞进每个 tuple 头。问题是——一个事务可能改了成千上万行,它最终是提交还是回滚,是**一次性决定**的(COMMIT/ROLLBACK 那一刻)。如果状态分散在它改过的每个 tuple 头里,COMMIT 时就得去**逐个翻遍这些 tuple** 把状态改过来,这是 O(改的行数) 的写,还要给每个 tuple 加锁(它们可能正被别的事务读),并发灾难。更糟的是事务状态是全局的(一个事务号只有一个状态),塞进每个 tuple 是冗余存储。

**所以这样设计**:事务状态**集中存在 clog** 这一本"账本"里。一个事务号 → 一个 2-bit 状态,全局唯一。COMMIT 时,只要在 clog 里把这一个事务的状态位从 `0x00`(进行中)改成 `0x01`(已提交)——**一次写,就告诉全世界"这个事务提交了"**。所有引用这个事务号的 tuple,下次被扫到时,去 clog 查一下就知道它提交没。这是"集中存状态、分散引用"的经典设计。

查 clog 的入口是 `TransactionIdDidCommit`(以"已提交"为例):

> [src/backend/access/transam/transam.c:126-136](../postgresql-17.0/src/backend/access/transam/transam.c#L126-L136)

```c
TransactionIdDidCommit(TransactionId transactionId)
{
	XidStatus	xidstatus;

	xidstatus = TransactionLogFetch(transactionId);

	/*
	 * If it's marked committed, it's committed.
	 */
	if (xidstatus == TRANSACTION_STATUS_COMMITTED)
		return true;
	...
```

`TransactionLogFetch` 是真正的查表函数,它还会先查一个**单条目缓存**(`cachedFetchXid`):

> [src/backend/access/transam/transam.c:51-62](../postgresql-17.0/src/backend/access/transam/transam.c#L51-L62)

```c
TransactionLogFetch(TransactionId transactionId)
{
	XidStatus	xidstatus;
	XLogRecPtr	xidlsn;

	/*
	 * Before going to the commit log manager, check our single item cache to
	 * see if we didn't just check the transaction status a moment ago.
	 */
	if (TransactionIdEquals(transactionId, cachedFetchXid))
		return cachedFetchXidStatus;
	...
```

这个单条目缓存是个关键优化——连续扫到的 tuple 往往属于同一两个事务(比如一个长 UPDATE 改了一大片,它们的 `xmin`/`xmax` 都指向同一个事务号),缓存命中就省一次 SLRU 查询。

### 查 clog 太贵:hint bit(提示位)登场

但即便有单条目缓存,**每扫一行都去查 clog 还是太贵**。clog 是共享内存结构,查它要走 SLRU、可能加 LWLock 争用。一张几亿行的表,扫一遍就是几亿次 clog 查询。怎么办?

**把"查 clog 才知道的事务状态",缓存进 tuple 头自己**——这就是 **hint bit(提示位)**。tuple 头的 `t_infomask` 里有这么几个 bit:

> [src/include/access/htup_details.h:204-210](../postgresql-17.0/src/include/access/htup_details.h#L204-L210)

```c
#define HEAP_XMIN_COMMITTED		0x0100	/* t_xmin committed */
#define HEAP_XMIN_INVALID		0x0200	/* t_xmin invalid/aborted */
#define HEAP_XMIN_FROZEN		(HEAP_XMIN_COMMITTED|HEAP_XMIN_INVALID)
#define HEAP_XMAX_COMMITTED		0x0400	/* t_xmax committed */
#define HEAP_XMAX_INVALID		0x0800	/* t_xmax invalid/aborted */
#define HEAP_XMAX_IS_MULTI		0x1000	/* t_xmax is a MultiXactId */
#define HEAP_UPDATED			0x2000	/* this is UPDATEd version of row */
```

关键四个 hint bit:

- **`HEAP_XMIN_COMMITTED`(0x0100)**——"`t_xmin` 那个事务,**已提交**"。下次扫到这行,直接看位就知道,不用查 clog。
- **`HEAP_XMIN_INVALID`(0x0200)**——"`t_xmin` 那个事务,**已回滚/无效**"。这行是别人没提交就插的,直接判不可见。
- **`HEAP_XMAX_COMMITTED`(0x0400)**——"`t_xmax` 那个事务,**已提交**"。这行已被提交的事务删/改了。
- **`HEAP_XMAX_INVALID`(0x0800)**——"`t_xmax` 无效(没被谁改/改它的事务回滚了)"。

> **hint bit 的工作方式是"惰性设置"**:
> - 一个 tuple 刚 INSERT 时,`HEAP_XMIN_COMMITTED` 这些位**是空的**(没人知道这个事务最终会不会提交)。
> - 第一次有事务扫到这行,做可见性判定,发现 `t_xmin` 对应事务**已经提交**了——它就**顺手把这个 hint bit 点上**(`SetHintBits`),写回 tuple(标记 buffer 为脏 hint)。
> - 以后所有事务再扫到这行,看 hint bit 就行,**永远不用再查 clog**。
>
> 这是用"一次写"(设置 hint bit)换"无数次读"(后续免查 clog)。在海量扫描场景下,这是 MVCC 性能的隐形加速器——第 7 章(P2)我们就提过"提示位是 MVCC 性能的隐形加速器",现在讲清它的来龙去脉。

设置 hint bit 的函数 `SetHintBits`(可见性判定函数内部调用):

> [src/backend/access/heap/heapam_visibility.c:113-132](../postgresql-17.0/src/backend/access/heap/heapam_visibility.c#L113-L132)

```c
static inline void
SetHintBits(HeapTupleHeader tuple, Buffer buffer,
			uint16 infomask, TransactionId xid)
{
	if (TransactionIdIsValid(xid))
	{
		/* NB: xid must be known committed here! */
		XLogRecPtr	commitLSN = TransactionIdGetCommitLSN(xid);

		if (BufferIsPermanent(buffer) && XLogNeedsFlush(commitLSN) &&
			BufferGetLSNAtomic(buffer) < commitLSN)
		{
			/* not flushed and no LSN interlock, so don't set hint */
			return;
		}
	}

	tuple->t_infomask |= infomask;
	MarkBufferDirtyHint(buffer, true);
}
```

注意中间那段 LSN 检查(`BufferGetLSNAtomic(buffer) < commitLSN`):**设置 hint bit 有个微妙的正确性约束**。hint bit 一旦写下去,就等于"这行的事务状态永久确定了"(因为 hint bit 不会回头去和 clog 校验)。但 WAL(预写日志)和 clog 的落盘是异步的——如果 commitLSN(这个事务提交记录的 WAL 位置)还没刷盘,而 buffer 的 LSN 又比它老,这时设置 hint bit 就有风险:万一崩溃,clog 可能还没落盘,但 hint bit 已经被当成"已提交"写进了数据页,崩溃恢复时会出现"页上写着已提交、但 clog 里没有记录"的不一致。所以这段 LSN 检查保证:**只有当提交记录已经落盘到不早于 buffer 的位置,才允许写 hint bit**。这是个极精细的细节,体现了 MVCC、WAL、崩溃恢复三者如何咬合(第 18-20 章会展开 WAL)。

> `MarkBufferDirtyHint` 是"标记 buffer 为脏 hint"——它**不强制刷盘**,只是告诉系统"这个 buffer 有 hint 改动,后续 checkpoint 时顺手刷下去就行"。hint bit 的丢失不致命(大不了下次再查 clog 重设),所以可以这么轻量。

---

## 五、可见性判定的核心:`HeapTupleSatisfiesMVCC` 三段式

理论齐了——tuple 有 `xmin`/`xmax`,快照有 `xmin`/`xmax`/`xip[]`,事务状态查 clog(或看 hint bit)。现在看它们怎么在代码里咬合成"这行对我可见吗"的判定。这是全书最该精读的函数之一。

先讲清判定的**逻辑骨架**(把代码的分支结构提炼成三段):

### 第一段:`t_xmin` 决定"这行出生了吗、属于我的世界吗"

```
这行插入我能不能看见?看 t_xmin 对应的事务(记为 X):
├─ X 是我自己?(TransactionIdIsCurrentTransactionId)
│   └─ 是 → 看插入命令号 t_cid:如果 >= 我的 curcid(我之后才插的)→ 不可见;否则 → 出生了,继续看 xmax
├─ X 在我的快照"正在跑"列表里?(XidInMVCCSnapshot)
│   └─ 是 → 不可见(别人还没提交就插的,我看不到脏数据)
├─ X 已提交?(TransactionIdDidCommit / 看 hint bit HEAP_XMIN_COMMITTED)
│   └─ 是 → 出生了,设 hint bit,继续看 xmax
└─ X 没提交(已回滚)→ 不可见,设 hint bit HEAP_XMIN_INVALID
```

### 第二段:`t_xmax` 决定"这行死了吗、属于我的世界吗"

如果"出生了"(第一段通过),再看 `t_xmax`(记为 Y,改/删这行的事务):

```
这行被删/改我能不能看见?看 t_xmax:
├─ t_xmax 无效(HEAP_XMAX_INVALID)/ 只是锁(HEAP_XMAX_IS_LOCKED_ONLY)
│   └─ 没被真正删 → 可见
├─ Y 是我自己?
│   └─ 看删除命令号 t_cid:>= curcid → 我之后才删的,可见;< curcid → 已删,不可见
├─ Y 在我的快照"正在跑"列表里?(XidInMVCCSnapshot)
│   └─ 是 → 改它的人还没提交 → 这行还活着 → 可见
├─ Y 已提交且早于我的快照?
│   └─ 是 → 这行真的死了 → 不可见
└─ Y 没提交 → 这行还活着 → 可见,设 hint bit HEAP_XMAX_INVALID
```

### 第三段:综合

两段都判完:`t_xmin` 通过(出生在我世界)且 `t_xmax` 未通过(没死在我世界)→ **可见**;否则 **不可见**。

> **核心三问(再强调一遍,这是 MVCC 可见性的灵魂)**:
> ① `xmin` 对应事务**提交了吗**?(查 clog 或看 hint bit)
> ② 它是不是**在我快照之前提交**(事务号 `< snapshot.xmin`,或在 `[xmin,xmax)` 区间且不在 `xip[]` 里)?
> ③ `xmax` 是不是**已提交且在我快照之前**?是 → 这行已死,我看不到;否 → 我看得到。
>
> 这三问回答清楚,任何 tuple 对任何事务的可见性都定了。

### `XidInMVCCSnapshot`:快照比对的核心

第二问"是不是在我快照之前提交"的具体实现,是 `XidInMVCCSnapshot`——它判断"这个事务号,在我的快照里算不算'还在跑'":

> [src/backend/utils/time/snapmgr.c:1856-1872](../postgresql-17.0/src/backend/utils/time/snapmgr.c#L1856-L1872)

```c
XidInMVCCSnapshot(TransactionId xid, Snapshot snapshot)
{
	/*
	 * Make a quick range check to eliminate most XIDs without looking at the
	 * xip arrays.  Note that this is OK even if we convert a subxact XID to
	 * its parent below, because a subxact with XID < xmin has surely also got
	 * a parent with XID < xmin, while one with XID >= xmax must belong to a
	 * parent that was not yet committed at the time of this snapshot.
	 */

	/* Any xid < xmin is not in-progress */
	if (TransactionIdPrecedes(xid, snapshot->xmin))
		return false;
	/* Any xid >= xmax is in-progress */
	if (TransactionIdFollowsOrEquals(xid, snapshot->xmax))
		return true;
	...
```

逻辑分三步(注意函数返回 `true` 表示"还在跑、对我不可见"):

1. **`xid < snapshot->xmin`** → 返回 `false`(不在跑,早就提交了,我看得见)。这是快路径——绝大多数老事务都走这条,根本不碰 `xip[]`。
2. **`xid >= snapshot->xmax`** → 返回 `true`(在拍快照之后才开始,我看不见)。
3. **介于 `xmin` 和 `xmax` 之间** → 查 `xip[]`(在跑列表):在列表里 → `true`(还在跑);不在 → `false`(已提交)。

把这个函数和 `HeapTupleSatisfiesMVCC` 串起来:`xmin`/`xmax` 比对快照的下界/上界,是用 `XidInMVCCSnapshot` 完成的;比对 clog 状态,是用 `TransactionIdDidCommit` 完成的;hint bit 是这两个的缓存。

---

## 六、关键源码精读:`HeapTupleSatisfiesMVCC` 全分支逐段拆

现在把 `HeapTupleSatisfiesMVCC` 整个函数从头到尾贴出来逐段讲(这是本章的源码收束)。先看函数签名和它开头那段**极重要的注释**:

> [src/backend/access/heap/heapam_visibility.c:937-971](../postgresql-17.0/src/backend/access/heap/heapam_visibility.c#L937-L971)

```c
/*
 * HeapTupleSatisfiesMVCC
 *		True iff heap tuple is valid for the given MVCC snapshot.
 *
 * See SNAPSHOT_MVCC's definition for the intended behaviour.
 *
 * Notice that here, we will not update the tuple status hint bits if the
 * inserting/deleting transaction is still running according to our snapshot,
 * even if in reality it's committed or aborted by now.  This is intentional.
 * Checking the true transaction state would require access to high-traffic
 * shared data structures, creating contention we'd rather do without, and it
 * would not change the result of our visibility check anyway.  The hint bits
 * will be updated by the first visitor that has a snapshot new enough to see
 * the inserting/deleting transaction as done.  In the meantime, the cost of
 * leaving the hint bits unset is basically that each HeapTupleSatisfiesMVCC
 * call will need to run TransactionIdIsCurrentTransactionId in addition to
 * XidInMVCCSnapshot (but it would have to do the latter anyway). ...
 */
static bool
HeapTupleSatisfiesMVCC(HeapTuple htup, Snapshot snapshot,
					   Buffer buffer)
{
	HeapTupleHeader tuple = htup->t_data;

	Assert(ItemPointerIsValid(&htup->t_self));
	Assert(htup->t_tableOid != InvalidOid);
```

这段注释点出一个**有意的设计**:如果一个事务"在我的快照里还在跑",即使它实际上已经提交了(现实世界变了,但我的快照是老照片),**我也不会去设 hint bit**。为什么?因为去查"它真实状态"要碰高频共享结构(ProcArray),制造争用;而且反正结果不影响我的判定(我的快照说它还在跑,它就当我看不见)。hint bit 会由"快照够新、看得见这事务已结束"的后来者来设。这种"宁可多跑两次轻量检查,也不去碰热点共享结构"的取舍,是 MVCC 性能设计的一个缩影。

### 分支①:`t_xmin` 已提交(hint bit 已设)

> [src/backend/access/heap/heapam_visibility.c:968-971](../postgresql-17.0/src/backend/access/heap/heapam_visibility.c#L968-L971)

```c
	if (!HeapTupleHeaderXminCommitted(tuple))
	{
		if (HeapTupleHeaderXminInvalid(tuple))
			return false;
```

最外层先看 `HEAP_XMIN_COMMITTED` 这个 hint bit:

- **已设(`HeapTupleHeaderXminCommitted` 为真)** → 跳到后面"已提交但要看快照"那段(下面分支④)。
- **没设,但 `HEAP_XMIN_INVALID` 已设** → 直接返回 `false`(`t_xmin` 那事务回滚了,这行是脏插入,谁也看不见)。
- **两个 hint bit 都没设** → 进入下面的"未知状态"判定流程(查快照 + 查 clog)。

### 分支②:我自己插入的这行(`t_xmin` 是当前事务)

> [src/backend/access/heap/heapam_visibility.c:1012-1053](../postgresql-17.0/src/backend/access/heap/heapam_visibility.c#L1012-L1053)

```c
	else if (TransactionIdIsCurrentTransactionId(HeapTupleHeaderGetRawXmin(tuple)))
	{
		if (HeapTupleHeaderGetCmin(tuple) >= snapshot->curcid)
			return false;	/* inserted after scan started */

		if (tuple->t_infomask & HEAP_XMAX_INVALID)	/* xid invalid */
			return true;

		if (HEAP_XMAX_IS_LOCKED_ONLY(tuple->t_infomask))	/* not deleter */
			return true;
		...
		if (!TransactionIdIsCurrentTransactionId(HeapTupleHeaderGetRawXmax(tuple)))
		{
			/* deleting subtransaction must have aborted */
			SetHintBits(tuple, buffer, HEAP_XMAX_INVALID,
						InvalidTransactionId);
			return true;
		}

		if (HeapTupleHeaderGetCmax(tuple) >= snapshot->curcid)
			return true;	/* deleted after scan started */
		else
			return false;	/* deleted before scan started */
	}
```

这是"自己改的自己看"的分支,用 `t_cid`/`curcid` 做命令级可见性:

- **插入命令号 `t_cid >= snapshot->curcid`** → 返回 `false`(这行是我"当前命令之后"才插的,我还看不到自己未来的改动)。
- **`HEAP_XMAX_INVALID`**(没被谁改)→ 返回 `true`(我自己插的、还没被改,当然可见)。
- **`HEAP_XMAX_IS_LOCKED_ONLY`**(只被锁没被删)→ 返回 `true`。
- 如果删除它的事务(`t_xmax`)**不是我当前事务** → 那它一定是我的某个子事务回滚了 → 设 `HEAP_XMAX_INVALID` hint,返回 `true`。
- 如果删除它的就是我自己 → 看删除命令号 `t_cmax`:`>= curcid` → 返回 `true`(我之后才删的,还可见);`< curcid` → 返回 `false`(我之前就删了,不可见)。

### 分支③:`t_xmin` 在我快照"正在跑"列表里 → 不可见

> [src/backend/access/heap/heapam_visibility.c:1054-1055](../postgresql-17.0/src/backend/access/heap/heapam_visibility.c#L1054-L1055)

```c
	else if (XidInMVCCSnapshot(HeapTupleHeaderGetRawXmin(tuple), snapshot))
		return false;
```

插入这行的事务,在我的快照里"还在跑"——返回 `false`(我看不到别人没提交的改动)。注意这里**不设 hint bit**(对应开头那段注释的取舍)。

### 分支④:`t_xmin` 不在快照的"正在跑"里,查 clog 看提交没

> [src/backend/access/heap/heapam_visibility.c:1056-1066](../postgresql-17.0/src/backend/access/heap/heapam_visibility.c#L1056-L1066)

```c
	else if (TransactionIdDidCommit(HeapTupleHeaderGetRawXmin(tuple)))
		SetHintBits(tuple, buffer, HEAP_XMIN_COMMITTED,
					HeapTupleHeaderGetRawXmin(tuple));
	else
	{
		/* it must have aborted or crashed */
		SetHintBits(tuple, buffer, HEAP_XMIN_INVALID,
					InvalidTransactionId);
		return false;
	}
```

- **查 clog,`t_xmin` 已提交** → 设 `HEAP_XMIN_COMMITTED` hint bit(下次免查),继续往下看 `t_xmax`。
- **`t_xmin` 没提交(回滚/崩溃)** → 设 `HEAP_XMIN_INVALID` hint bit,返回 `false`。

到这一步,"这行出生在我能看见的世界里"已经确认。下面看 `t_xmax`。

### 分支⑤:`t_xmax` 已提交(hint bit 已设)——但要再看快照

> [src/backend/access/heap/heapam_visibility.c:1067-1073](../postgresql-17.0/src/backend/access/heap/heapam_visibility.c#L1067-L1073)

```c
	else
	{
		/* xmin is committed, but maybe not according to our snapshot */
		if (!HeapTupleHeaderXminFrozen(tuple) &&
			XidInMVCCSnapshot(HeapTupleHeaderGetRawXmin(tuple), snapshot))
			return false;		/* treat as still in progress */
	}
```

这是分支①走下来的延续——`t_xmin` hint bit 已设(标记已提交),但**仍要再和我的快照比一次**。为什么?因为 hint bit 是"全局状态"(这事务客观上已提交),但我的快照是"老照片"——它可能拍得早,那时这事务还在跑。所以即使 hint bit 说"已提交",如果 `XidInMVCCSnapshot` 返回 `true`(我的老照片里它还在跑),仍判 `false`。**hint bit 加速了"查 clog",但不能绕过"比对快照"**——快照是每个事务私有的,没法缓存。

注意 `!HeapTupleHeaderXminFrozen(tuple)` 这个条件——**冻结(freeze)过的 tuple,跳过快照比对**(下面第七节详讲:冻结就是"这行对所有快照都可见,别再比了")。

### 分支⑥:看 `t_xmax`——这行被删了吗

> [src/backend/access/heap/heapam_visibility.c:1075-1081](../postgresql-17.0/src/backend/access/heap/heapam_visibility.c#L1075-L1081)

```c
	/* by here, the inserting transaction has committed */

	if (tuple->t_infomask & HEAP_XMAX_INVALID)	/* xid invalid or aborted */
		return true;

	if (HEAP_XMAX_IS_LOCKED_ONLY(tuple->t_infomask))
		return true;
```

- **`HEAP_XMAX_INVALID`**(没人改/改的人回滚了)→ 返回 `true`(这行还活着,可见)。
- **`HEAP_XMAX_IS_LOCKED_ONLY`**(只是被锁,如 `SELECT FOR SHARE`)→ 返回 `true`(锁不改变版本存亡)。

### 分支⑦:`t_xmax` 是 MultiXact(多个事务同时锁/改这行)

> [src/backend/access/heap/heapam_visibility.c:1083-1108](../postgresql-17.0/src/backend/access/heap/heapam_visibility.c#L1083-L1108)

```c
	if (tuple->t_infomask & HEAP_XMAX_IS_MULTI)
	{
		TransactionId xmax;

		/* already checked above */
		Assert(!HEAP_XMAX_IS_LOCKED_ONLY(tuple->t_infomask));

		xmax = HeapTupleGetUpdateXid(tuple);

		/* not LOCKED_ONLY, so it has to have an xmax */
		Assert(TransactionIdIsValid(xmax));

		if (TransactionIdIsCurrentTransactionId(xmax))
		{
			if (HeapTupleHeaderGetCmax(tuple) >= snapshot->curcid)
				return true;	/* deleted after scan started */
			else
				return false;	/* deleted before scan started */
		}
		if (XidInMVCCSnapshot(xmax, snapshot))
			return true;
		if (TransactionIdDidCommit(xmax))
			return false;		/* updating transaction committed */
		/* it must have aborted or crashed */
		return true;
	}
```

`HEAP_XMAX_IS_MULTI` 表示 `t_xmax` 是个 MultiXactId(第 16 章提过:多个事务同时锁同一行时,PG 把它们打包成一个 MultiXact)。这段把 MultiXact 解开,拿到真正"更新"这行的事务号(`HeapTupleGetUpdateXid`),再做和普通 `xmax` 一样的判定。

### 分支⑧:`t_xmax` hint bit 没设——查 clog + 比快照

> [src/backend/access/heap/heapam_visibility.c:1110-1140](../postgresql-17.0/src/backend/access/heap/heapam_visibility.c#L1110-L1140)

```c
	if (!(tuple->t_infomask & HEAP_XMAX_COMMITTED))
	{
		if (TransactionIdIsCurrentTransactionId(HeapTupleHeaderGetRawXmax(tuple)))
		{
			if (HeapTupleHeaderGetCmax(tuple) >= snapshot->curcid)
				return true;	/* deleted after scan started */
			else
				return false;	/* deleted before scan started */
		}

		if (XidInMVCCSnapshot(HeapTupleHeaderGetRawXmax(tuple), snapshot))
			return true;

		if (!TransactionIdDidCommit(HeapTupleHeaderGetRawXmax(tuple)))
		{
			/* it must have aborted or crashed */
			SetHintBits(tuple, buffer, HEAP_XMAX_INVALID,
						InvalidTransactionId);
			return true;
		}

		/* xmax transaction committed */
		SetHintBits(tuple, buffer, HEAP_XMAX_COMMITTED,
					HeapTupleHeaderGetRawXmax(tuple));
	}
	else
	{
		/* xmax is committed, but maybe not according to my snapshot */
		if (XidInMVCCSnapshot(HeapTupleHeaderGetRawXmax(tuple), snapshot))
			return true;		/* treat as still in progress */
	}

	/* xmax transaction committed */

	return false;
}
```

这是 `t_xmax` 的"完整判定",和 `t_xmin` 完全对称:

- **我自己删的** → 看删除命令号 `t_cmax` 决定可见与否。
- **在我快照"正在跑"里** → 返回 `true`(改它的人还没提交,这行还活着)。
- **查 clog 没提交** → 设 `HEAP_XMAX_INVALID` hint,返回 `true`(改它的人回滚了,这行还活着)。
- **查 clog 已提交** → 设 `HEAP_XMAX_COMMITTED` hint。
- 最后,即使 hint bit 说"已提交",仍要比快照:`XidInMVCCSnapshot` 为真 → 返回 `true`(我的老照片里它还在跑,这行对我还活着)。

函数最后:**`return false`**——走到这里意味着"`t_xmax` 已提交且在我快照之前",这行确实死了,不可见。

### 把整张状态机画出来

```text
                    ┌──────────────────────────────────────────────┐
                    │         HeapTupleSatisfiesMVCC               │
                    └──────────────────────────────────────────────┘
                                     │
                   看 t_xmin (插入这行的事务 X)
                                     │
        ┌────────────────────────────┼────────────────────────────┐
        ▼                            ▼                            ▼
  X 是我自己?                  X 在快照                  查 clog:X 提交了吗?
  (IsCurrentTransactionId)      "正在跑"列表里?
                                (XidInMVCCSnapshot)
        │                            │                            │
   看 t_cid 命令号                  返回 false                是→设 COMMITTED    否→设 INVALID,返回 false
   (自己改的自己看)               (看不见别人                继续看 xmax         (脏插入,谁也看不见)
                                  没提交的插入)
                                     │
                            ━━━━━━━━ 通过(t_xmin 出生在我的世界)━━━━━━━━
                                     │
                   看 t_xmax (删/改这行的事务 Y)
                                     │
        ┌────────────────────────────┼────────────────────────────┐
        ▼                            ▼                            ▼
  XMAX_INVALID /                Y 在快照                  查 clog:Y 提交了吗?
  LOCKED_ONLY                    "正在跑"列表里?
  (没被真正删)                                          (或看 XMAX_COMMITTED hint)
        │                            │                            │
     返回 true                    返回 true                是→再看快照:        否→设 INVALID,返回 true
     (这行活着,可见)            (改的人还没提交,        XidInMVCCSnapshot?    (改的人回滚,这行还活着)
                                  这行还活着,可见)        是→返回 true
                                                          否→返回 false
                                                                    │
                                                              (这行已死,不可见)
```

这张图就是 `HeapTupleSatisfiesMVCC` 的全部。读源码时,把每个分支对应到这张图的某个箭头,就不会迷路。核心永远是那三问:`xmin` 提交了且在我世界?`xmax` 提交了且在我世界?——前者决定"生",后者决定"死",两者都满足才"活着、可见"。

---

## 七、一次 UPDATE 实际做了什么:写不阻塞读的根源

讲了这么多机制,回到一个具体问题:**一次 `UPDATE` 在 MVCC 下到底改了什么?** 把它和"原地改"对比,你就彻底明白"写不阻塞读"是怎么落地的。

### 传统做法(就地改 / in-place update)

很多系统(包括 MySQL InnoDB 的部分场景、很多 NoSQL)是**就地改**:UPDATE 直接把旧值覆盖成新值。

> **不这样会怎样(就地改的麻烦)**:如果 A 正在读 `id=1`(读到旧值 100 的中间几个字节),B 这时 `UPDATE` 就地把它改成 200——A 可能读到"1?0"这种半新半旧的撕裂值。要避免,A 读时必须加共享锁挡住 B(读挡写),或 B 写时加排他锁挡住 A(写挡读)。这就是"锁做读写隔离"的死结。

### PG 的做法:写新版本,留旧版本

PG 的 UPDATE 是 **"标记旧版本 + 写新版本"** 两步。看 `heap_update` 的关键代码:

**第一步:给旧版本打 `t_xmax`,记下"它被这次 UPDATE 改了"。**

> [src/backend/access/heap/heapam.c:3945-3954](../postgresql-17.0/src/backend/access/heap/heapam.c#L3945-L3954)

```c
	oldtup.t_data->t_infomask2 &= ~HEAP_KEYS_UPDATED;
	/* ... and store info about transaction updating this tuple */
	Assert(TransactionIdIsValid(xmax_old_tuple));
	HeapTupleHeaderSetXmax(oldtup.t_data, xmax_old_tuple);
	oldtup.t_data->t_infomask |= infomask_old_tuple;
	oldtup.t_data->t_infomask2 |= infomask2_old_tuple;
	HeapTupleHeaderSetCmax(oldtup.t_data, cid, iscombo);

	/* record address of new tuple in t_ctid of old one */
	oldtup.t_data->t_ctid = heaptup->t_self;
```

- `HeapTupleHeaderSetXmax(oldtup.t_data, xmax_old_tuple)`——把旧版本的 `t_xmax` 设成这次 UPDATE 的事务号。
- `oldtup.t_data->t_ctid = heaptup->t_self`——把旧版本的 `t_ctid` 改成指向新版本的物理位置,串上版本链。

注意:**旧版本的数据一个字节都没动**。它还好好地躺在原位,只是头上多了个"已被 X 改过"的标记和一条"我的继任者在哪儿"的指针。

**第二步:写新版本,打 `t_xmin`。**

> [src/backend/access/heap/heapam.c:3632-3638](../postgresql-17.0/src/backend/access/heap/heapam.c#L3632-L3638)

```c
	/*
	 * Prepare the new tuple with the appropriate initial values of Xmin and
	 * Xmax, as well as initial infomask bits as computed above.
	 */
	newtup->t_data->t_infomask &= ~(HEAP_XACT_MASK);
	newtup->t_data->t_infomask2 &= ~(HEAP2_XACT_MASK);
	HeapTupleHeaderSetXmin(newtup->t_data, xid);
	HeapTupleHeaderSetCmin(newtup->t_data, cid);
	newtup->t_data->t_infomask |= HEAP_UPDATED | infomask_new_tuple;
	newtup->t_data->t_infomask2 |= infomask2_new_tuple;
	HeapTupleHeaderSetXmax(newtup->t_data, xmax_new_tuple);
```

新版本是**全新的物理 tuple**(可能在另一个页、另一个槽位):

- `HeapTupleHeaderSetXmin(newtup->t_data, xid)`——`t_xmin` 是这次 UPDATE 的事务号(我是被 X 插入的)。
- `t_infomask |= HEAP_UPDATED`——打个标记"我是 UPDATE 产生的新版本"(不是直接 INSERT 的)。
- `HeapTupleHeaderSetXmax(newtup->t_data, xmax_new_tuple)`——新版本的 `t_xmax` 初始为无效(还没被谁改)。

> 对比 `heap_insert`(直接插入)和 `heap_delete`:`heap_insert` 设 `t_xmin`、把 `t_xmax` 留无效([src/backend/access/heap/heapam.c:2199-2205](../postgresql-17.0/src/backend/access/heap/heapam.c#L2199-L2205));`heap_delete` 只设旧版本的 `t_xmax`([src/backend/access/heap/heapam.c:2972](../postgresql-17.0/src/backend/access/heap/heapam.c#L2972))、不产生新版本;`heap_update` 则是两者都做(旧行设 xmax + 新行设 xmin)。**UPDATE = DELETE 旧版本 + INSERT 新版本**,这在 MVCC 里不是比喻,是字面意义。

### 为什么这样写就不阻塞读?

把上面两步和"读者"对照:

```text
UPDATE 执行时(事务 B 还没提交):

  旧版本(原位,数据没动)            新版本(别处新写的)
  ┌──────────────────────┐         ┌──────────────────────┐
  │ xmin = A (老,已提交) │         │ xmin = B (B 还没提交) │
  │ xmax = B (被 B 改)    │  t_ctid │ xmax = 0             │
  │ data = 100           │ ──────► │ data = 200           │
  └──────────────────────┘         └──────────────────────┘
            ▲
            │ 事务 A(快照早于 B)扫到这:
            │   xmin=A 已提交 & 早于我快照 → 出生了
            │   xmax=B,B 在我快照"正在跑"里 → 没死
            │   → 可见,读到 100 ✓
            │
            │ 事务 C(快照晚于 B 提交)扫到这:
            │   xmax=B,B 已提交 & 早于我快照 → 死了
            │   → 不可见,顺 t_ctid 找新版本
            │   └─► 新版本 xmin=B 已提交 & 早于我 → 可见,读到 200 ✓
```

- **事务 A(老快照)**:扫到旧版本,它的 `t_xmax=B`,而 B 在 A 的快照里"还在跑"——A 判定"这行没被删",**读到 100**。它**根本不碰新版本**(新版本的 `xmin=B` 还没提交,A 看不见)。
- **事务 C(新快照)**:扫到旧版本,`t_xmax=B` 已提交且早于 C——C 判定"这行死了",跳过,顺着 `t_ctid` 找到新版本,新版本 `xmin=B` 已提交且早于 C——C **读到 200**。

> **关键:A 和 C 读的是不同的物理位置,UPDATE 写的也是另一个物理位置。三者操作的内存/磁盘区域完全不重叠**——这就是为什么 UPDATE 不阻塞 SELECT、SELECT 不阻塞 UPDATE。读者拿快照各看各的版本,写者只往新地方写新版本。**没有共享数据,就没有冲突。** 这是 MVCC 区别于"锁做读写隔离"的根本所在。

### 写不阻塞读的代价:死元组

但这个设计有代价——**旧版本不会立刻消失**:

- A 可能还拿着老快照,需要读旧版本(读到 100)。如果 B 一提交就把旧版本抹掉,A 就读到错乱。
- 所以旧版本必须**留着**,直到"所有可能读它的事务都结束"。
- 这些"已经死了、但还不能删"的旧版本,叫 **死元组(dead tuple)**。它们**白占空间**——页被塞满、表膨胀(bloat)、扫描变慢。

这就是 MVCC 的第一笔长期账:**读写分离的代价是死元组堆积**。谁来还这笔账?**VACUUM**(P6 第 21 章)。VACUUM 扫表,找出"对所有活事务都不可见"的死元组,回收它们的空间(把行指针标成 `LP_DEAD`/`LP_UNUSED`,或整页压缩)。这是 MVCC 设计内建的"打扫机制",第 7 章(P2)我们埋过伏笔(`LP_DEAD` 状态)。

> **死元组为什么是 MVCC 的内在代价**:任何"读写不阻塞"的方案都得让旧版本留一段时间(供老读者读)。PG 选择了"留到 VACUUM 来清",这是空间换并发度——它让 PG 的高并发成为可能,代价是必须定期 VACUUM。如果一个高写入表从不 VACUUM,死元组会无限堆积,最终表膨胀到几倍、几十倍,I/O 性能崩塌。这是 PG 运维最常见的坑之一。

---

## 八、事务号回绕与冻结:MVCC 的第二笔长期账

MVCC 还有一笔更隐蔽、但更致命的长期账:**事务号(xid)会用完**。

### 事务号是 32 位、有限的

PG 的事务号 `TransactionId` 是个 **32 位无符号整数**:

> [src/include/access/transam.h:31-35](../postgresql-17.0/src/include/access/transam.h#L31-L35)

```c
#define InvalidTransactionId		((TransactionId) 0)
#define BootstrapTransactionId		((TransactionId) 1)
#define FrozenTransactionId			((TransactionId) 2)
#define FirstNormalTransactionId	((TransactionId) 3)
#define MaxTransactionId			((TransactionId) 0xFFFFFFFF)
```

`MaxTransactionId = 0xFFFFFFFF`,约 **42 亿**。看起来很多,但一个繁忙的数据库,每秒可能几百几千个事务,几天到几个月就会逼近上限。那到了 42 亿怎么办?

### 不这样会怎样:事务号回绕(wraparound)会毁掉数据

> **不这样会怎样**:事务号是循环的——到了 42 亿,下一个事务号会**回绕(wraparound)到很小的值**(比如 3)。这会引发灾难:假设事务号回绕了,现在有个新事务号 X=5(回绕后的),而表里有行的 `t_xmin=4000000000`(回绕前很老的值)。做可见性判定时,"X=5 比 `t_xmin=4000000000` 小"会被理解成"X 是更老的事务"——但物理上 X 是回绕后的新事务!可见性判定全部错乱,数据库会**读到混乱的数据**,甚至崩溃。这就是 **XID wraparound(事务号回绕)**——PG 里最可怕的灾难之一,一旦发生,可能造成不可逆的数据损失。

### 解法:冻结(freeze)老版本,让它们不再占用事务号区间

PG 的解法是 **"冻结"(freeze)**:既然事务号会回绕,那就**把"很老的、肯定对所有事务都可见的" tuple 的 `t_xmin`,改成一个特殊值 `FrozenTransactionId`**(=2)。这个特殊事务号对**所有快照都可见**(不需要再比对快照,也不需要查 clog——`TransactionLogFetch` 里直接返回 `TRANSACTION_STATUS_COMMITTED`)。

冻结后,这行的 `t_xmin` 不再是那个"很老的、占用事务号区间的"事务号,而是 `FrozenTransactionId`。原来那个老事务号就可以被"遗忘"——它的状态在 clog 里也可以被清理。这样,**事务号的可用区间就被不断"腾出来"**,避免回绕。

冻结的实现:`t_infomask` 里有专门的 hint bit 组合 `HEAP_XMIN_FROZEN`:

> [src/include/access/htup_details.h:206](../postgresql-17.0/src/include/access/htup_details.h#L206)

```c
#define HEAP_XMIN_FROZEN		(HEAP_XMIN_COMMITTED|HEAP_XMIN_INVALID)
```

注意这个定义的巧妙——`HEAP_XMIN_FROZEN = HEAP_XMIN_COMMITTED | HEAP_XMIN_INVALID`,即**两个 bit 同时置位**。这是个"特殊编码":正常情况下 COMMITTED 和 INVALID 互斥(一个事务不可能既提交又回滚),所以"两个都置位"就成了一个**未使用的编码**,拿来表示"冻结"这种特殊状态,不浪费新 bit。判定宏:

> [src/include/access/htup_details.h:336-339](../postgresql-17.0/src/include/access/htup_details.h#L336-L339)

```c
#define HeapTupleHeaderXminFrozen(tup) \
( \
	((tup)->t_infomask & (HEAP_XMIN_FROZEN)) == HEAP_XMIN_FROZEN \
)
```

设置冻结的宏:

> [src/include/access/htup_details.h:353-357](../postgresql-17.0/src/include/access/htup_details.h#L353-L357)

```c
#define HeapTupleHeaderSetXminFrozen(tup) \
( \
	AssertMacro(!HeapTupleHeaderXminInvalid(tup)), \
	((tup)->t_infomask |= HEAP_XMIN_FROZEN) \
)
```

冻结的核心逻辑在 `heap_prepare_freeze_tuple`(VACUUM 调用)。当某个 tuple 的 `t_xmin` 对应事务"老于冻结阈值(`FreezeLimit`)"时,就把它标成 frozen:

> [src/backend/access/heap/heapam.c:6747-6752](../postgresql-17.0/src/backend/access/heap/heapam.c#L6747-L6752)

```c
	if (freeze_xmin)
	{
		Assert(!xmin_already_frozen);

		frz->t_infomask |= HEAP_XMIN_FROZEN;
	}
```

> **冻结的语义**:`HEAP_XMIN_FROZEN` 一旦设上,这行的 `t_xmin` 就"对所有事务都可见"——在 `HeapTupleSatisfiesMVCC` 的分支⑤里,你会看到 `if (!HeapTupleHeaderXminFrozen(tuple) && XidInMVCCSnapshot(...)) return false;`——**冻结的 tuple 跳过快照比对**,直接当作"已提交、对所有快照可见"。这就是"冻结 = 这行永生,谁都能看见"。

### 回绕的防线:三层阈值

事务号分配在 `GetNewTransactionId`(第 15 章贴过入口)。它在分配新事务号前,会检查一系列"回绕防线":

> [src/backend/access/transam/varsup.c:123-167](../postgresql-17.0/src/backend/access/transam/varsup.c#L123-L167)

```c
	if (TransactionIdFollowsOrEquals(xid, TransamVariables->xidVacLimit))
	{
		/*
		 * For safety's sake, we release XidGenLock while sending signals,
		 * warnings, etc. ...
		 */
		TransactionId xidWarnLimit = TransamVariables->xidWarnLimit;
		TransactionId xidStopLimit = TransamVariables->xidStopLimit;
		TransactionId xidWrapLimit = TransamVariables->xidWrapLimit;
		Oid			oldest_datoid = TransamVariables->oldestXidDB;

		LWLockRelease(XidGenLock);

		/*
		 * To avoid swamping the postmaster with signals, we issue the autovac
		 * request only once per 64K transaction starts. ...
		 */
		if (IsUnderPostmaster && (xid % 65536) == 0)
			SendPostmasterSignal(PMSIGNAL_START_AUTOVAC_LAUNCHER);

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

这是一套**三道防线**的设计(阈值在 `SetTransactionIdLimit` 里算):

> [src/backend/access/transam/varsup.c:389-406](../postgresql-17.0/src/backend/access/transam/varsup.c#L389-L406)

```c
	xidWrapLimit = oldest_datfrozenxid + (MaxTransactionId >> 1);
	if (xidWrapLimit < FirstNormalTransactionId)
		xidWrapLimit += FirstNormalTransactionId;
	...
	xidStopLimit = xidWrapLimit - 3000000;
	...
	xidWarnLimit = xidWrapLimit - 40000000;
	...
	xidVacLimit = oldest_datfrozenxid + autovacuum_freeze_max_age;
```

提炼成四道闸(从松到紧):

| 阈值 | 含义 | 触发动作 |
|---|---|---|
| `xidVacLimit` | 该考虑冻结了(`oldest_datfrozenxid + autovacuum_freeze_max_age`,默认 2 亿) | 触发 autovacuum 的 anti-wraparound(防回绕)扫描 |
| `xidWarnLimit` | 离危险很近了(`xidWrapLimit - 4000 万`) | 日志里 WARNING 告警 |
| `xidStopLimit` | 拒绝再发事务号(`xidWrapLimit - 300 万`) | 普通模式直接 ERROR,拒绝写(只读还能用) |
| `xidWrapLimit` | 真正回绕点(`oldest_datfrozenxid + 21 亿`,半个事务号空间) | **绝对不能越过**——越过就回绕、数据毁 |

核心数学约束(注释说得很清楚):`xidWrapLimit = oldest_datfrozenxid + (MaxTransactionId >> 1)`,即**回绕点 = 最老的未冻结事务号 + 半个事务号空间(21 亿)**。为什么是"半个"?因为事务号比较是**模 2^32 的环形**——只要保证"最老的未冻结事务号"和"最新的已分配事务号"之间的距离不超过半个环(21 亿),比较的大小关系就不会因回绕而反转。**冻结的作用,就是把 `oldest_datfrozenxid` 不断往前推**,让这个"半环安全距离"始终成立。

> **autovacuum 的 anti-wraparound 模式**:当事务号逼近 `xidVacLimit`,autovacuum 会**强制启动**(即使你关了普通的 autovacuum),专门去扫老表、冻结老 tuple、推进 `datfrozenxid`。这是 PG 防回绕的"自动救命机制"——它和清理死元组是两回事(虽然都由 VACUUM 做):清理死元组是"省空间",冻结是"防数据毁"。一个数据库哪怕写入很少,只要事务在跑(消耗事务号),也必须定期冻结。

### 冻结是个 WAL 记录的操作

冻结不只是改个 bit——它要**写 WAL**(因为崩溃恢复时必须能重放这个改动,否则重启后又看到未冻结的老 `t_xmin`,可能回绕)。`heap_freeze_tuple` 是给 CLUSTER 这种自己管 WAL 的操作用的:

> [src/backend/access/heap/heapam.c:6921-6946](../postgresql-17.0/src/backend/access/heap/heapam.c#L6921-L6946)

```c
bool
heap_freeze_tuple(HeapTupleHeader tuple,
				  TransactionId relfrozenxid, TransactionId relminmxid,
				  TransactionId FreezeLimit, TransactionId MultiXactCutoff)
{
	HeapTupleFreeze frz;
	bool		do_freeze;
	bool		totally_frozen;
	struct VacuumCutoffs cutoffs;
	HeapPageFreeze pagefrz;
	...
	do_freeze = heap_prepare_freeze_tuple(tuple, &cutoffs,
										  &pagefrz, &frz, &totally_frozen);
```

普通的 VACUUM 走的是 `heap_prepare_freeze_tuple` + 批量写 WAL freeze record 的路径(更高效)。这是个细节,本章点到——重点是"冻结不是免费的,它要扫全表、改 tuple、写 WAL",这也是为什么 PG 不会无脑冻结所有 tuple,而是只冻"老于阈值"的。

---

## 九、MVCC 全景:把所有零件装回一辆车

讲完所有零件,把它们装回一辆完整的车,看 MVCC 怎么协同工作。一次完整的"并发 UPDATE + SELECT"流程:

```text
时刻    事务 A (RR,长查询)                事务 B (写)              事务 C (RC,晚来)
T0      BEGIN; (拍快照 SA:                 
          xip=[B], xmin=100, xmax=300)
T1                                         BEGIN; (领 xid=200)
T2                                         UPDATE accounts SET balance=200 WHERE id=1;
                                             ↳ heap_update:
                                               - 旧版本: xmax=200, t_ctid→新版本
                                               - 新版本: xmin=200, xmax=0 (别处新写)
                                             (还没 COMMIT)
T3      SELECT balance FROM accounts WHERE id=1;
          ↳ 扫到旧版本:
            - HeapTupleSatisfiesMVCC(tuple, SA)
            - xmin=老事务, COMMITTED hint 已设, 早于 SA → 出生
            - xmax=200, XidInMVCCSnapshot(200, SA)=true (B 在 SA 的 xip 里)
            → 判定:没死 → 可见,读 balance=100  ✓ (不阻塞 B!)
T4                                         COMMIT;
          ↳ clog: 事务 200 → COMMITTED
          ↳ 旧版本: 第一次有人扫到,设 HEAP_XMAX_COMMITTED hint
T5                                                                  BEGIN; (拍快照 SC:
                                                                      xip=[], xmin=250, xmax=300)
T6                                                                  SELECT balance WHERE id=1;
                                                                      ↳ 扫到旧版本:
                                                                        - xmax=200, COMMITTED, 早于 SC → 死了
                                                                        → 顺 t_ctid 找新版本
                                                                        - 新版本: xmin=200, COMMITTED, 早于 SC → 出生
                                                                        - xmax=0 → 没死
                                                                        → 可见,读 balance=200  ✓
T7      COMMIT; (A 结束,旧版本现在没人需要了 → 成死元组,等 VACUUM)
```

整个流程里,**A、B、C 没有任何一刻互相阻塞**:

- A 读旧版本(B 写新版本到别处,A 根本不碰新版本)。
- B 写新版本(A 读旧版本在原位,B 不碰旧版本的数据字节,只在它头上设 `xmax`)。
- C 读新版本(顺 `t_ctid` 链找到)。
- 三者靠 `xmin`/`xmax`/快照/clog/hint bit 协同,完全不需要锁来隔离读写。

这就是 MVCC 的全貌。所有的复杂度,都是为了"让读和写在物理上分开,各看各的版本"。

---

## 章末小结

### 用一段话回顾本章

MVCC(多版本并发控制)是 PG 高并发的根基。它的核心思想是 **"更新不就地改、删除不抹掉,而是产生新版本、标记旧版本;每个事务用快照只看到它开始时已提交的版本"**。物理上,每个 tuple 头带 `t_xmin`(插入它的事务号)/`t_xmax`(删/改它的事务号)/`t_ctid`(指向更新后的新版本),这三个字段标记一个版本的"生、灭、继任";多个物理版本用 `t_ctid` 串成链,构成一个逻辑行的多版本历史。可见性判定的核心是 `HeapTupleSatisfiesMVCC`——它综合 tuple 的 `xmin`/`xmax` + 事务状态(clog:已提交/已回滚)+ 快照(`xmin` 下界 / `xmax` 上界 / `xip[]` 正在跑列表),回答三问:① `xmin` 提交了吗?② 它在我快照之前提交吗?③ `xmax` 提交了且在我快照之前吗?——前两问决定"生",第三问决定"灭"。hint bit(`HEAP_XMIN_COMMITTED` 等)把"查 clog 才知道的事务状态"缓存进 tuple 头,避免每次可见性判定都查共享的 clog,是 MVCC 性能的隐形加速器。MVCC 让读写不阻塞,但有两笔长期账:**死元组堆积**(要 VACUUM 回收,这是 MVCC 读写分离的代价)和**事务号回绕**(32 位事务号会用完,要冻结老版本——把 `t_xmin` 改成 `FrozenTransactionId`,让老事务号可被遗忘,腾出事务号空间)。

### 三件最该记住的事

1. **版本 = (xmin, xmax)**:`xmin` 是它的出生证、`xmax` 是它的死亡证。一个 tuple 对某事务可见,当且仅当"它出生在该事务的世界里(`xmin` 已提交且早于快照)、且还没死在该事务的世界里(`xmax` 未提交或晚于快照)"。
2. **hint bit 是缓存**:tuple 头有几个 bit,缓存了"`xmin`/`xmax` 对应事务的提交状态",下次扫到就不用查 clog。这是惰性设置的——第一次扫到的人查 clog 并设上,以后所有人受益。
3. **MVCC 不是免费的**:读写不阻塞的代价是死元组堆积(要 VACUUM)和事务号回绕(要冻结)。这两笔账是 P6 VACUUM 的主线,也是 PG 运维最常见的坑。

### 回扣主线:为什么 PG 的高并发扎根于此

本章服务的是全书主线里"**让数据不丢不乱**"那一侧的"**不乱**",而且是最精髓的那种"不乱"——**让并发的事务各自看到一个一致的、稳定的数据视图,而不需要互相阻塞**。

- 回扣**三个本性**:MVCC 治的是第二条——**"数据要共享,但共享会冲突"**。共享就是并发访问,冲突就是"有人写、有人读"时的可见性混乱。MVCC 用"多版本 + 快照"把这种冲突化解:写者写新版本、读者读旧版本,物理上分开,逻辑上各得其所。
- 回扣**"快 vs 不丢不乱"二分法**:MVCC 同时服务两侧——它让并发度飞起(读不挡写、写不挡读,这是"快"),又保证每个事务看到的数据是一致的(这是"不乱")。但它的"快"是有代价的(死元组、冻结),这些代价反过来又需要别的机制(VACUUM)来偿还。这正是数据库设计的辩证法:**没有一个机制是免费的,每一个"快"都对应某个角落的"账"**。
- 回扣 **ACID**:第 14 章我们说 PG 的 **A(原子性)和 I(隔离性)共用 MVCC 这一套**——回滚不需要 undo,因为旧行还在(MVCC),只要让新版本作废;隔离不需要"读加共享锁",因为读者看旧版本(MVCC)。本章把这层"共用"彻底落地:同一个多版本机制,既实现了原子性(回滚=作废新版本),又实现了隔离性(各看各的快照)。这是 PG 区别于 InnoDB 最深刻的设计。

### 锁 + MVCC:完整的 I

至此,P4 四章把"并发控制"讲完了,合起来才是完整的 **I(隔离性)**:

| 机制 | 管什么 | 代价 |
|---|---|---|
| **MVCC(本章)** | 读写并发(读不阻塞写、写不阻塞读) | 死元组堆积、事务号回绕 |
| **锁(第 16 章)** | 写写冲突、结构变更冲突 | 持有期间挡住别人 |
| **快照 + 隔离级别(第 15 章)** | 决定 MVCC "看多严" | 越严快照持有越久,越拖累系统 |

三者协同:MVCC 让读写互不阻塞,锁让写写排队,隔离级别决定快照的更新频率(从而决定看到别人改动的程度)。一个 `SELECT` 拿快照扫表(MVCC,不挡写);一个 `UPDATE` 加行锁改数据(锁,挡住并发改同一行的事务);隔离级别调高,快照锁死,看不到中途新提交的改动。这就是 PG 并发控制的全景。

### 想继续深入

- **可见性判定全集**:[src/backend/access/heap/heapam_visibility.c](../postgresql-17.0/src/backend/access/heap/heapam_visibility.c)。本章精读的 `HeapTupleSatisfiesMVCC`(L960)是核心;同文件还有 `HeapTupleSatisfiesSelf`(L169,自己看自己)、`HeapTupleSatisfiesVacuum`(L1161,VACUUM 用,判断"这行对所有人是不是都不可见")、`HeapTupleSatisfiesDirty`(脏读级别用)。顶部 L40-100 的注释把" xmin/xmax + clog + hint bit 怎么协作"讲得极清楚,是 MVCC 的总纲。
- **hint bit 设置**:`SetHintBits`(L114)、`HeapTupleSetHintBits`(L141);`MarkBufferDirtyHint`(在 `bufmgr.c`)是"脏 hint"的落盘机制。
- **tuple 头标志位全集**:[src/include/access/htup_details.h](../postgresql-17.0/src/include/access/htup_details.h) L188-285,所有 `HEAP_XMIN_*`/`HEAP_XMAX_*`/`HEAP_*` 宏;访问宏 L300-430(`HeapTupleHeaderGetXmin` 等)。注意 `HEAP_XMIN_FROZEN`(L206)是"两个互斥 bit 同时置位"的特殊编码。
- **快照结构**:[src/include/utils/snapshot.h](../postgresql-17.0/src/include/utils/snapshot.h) 的 `SnapshotData`(L142);快照何时拍 [src/backend/utils/time/snapmgr.c](../postgresql-17.0/src/backend/utils/time/snapmgr.c) 的 `GetTransactionSnapshot`(L216);快照内容怎么算 [src/backend/storage/ipc/procarray.c](../postgresql-17.0/src/backend/storage/ipc/procarray.c) 的 `GetSnapshotData`;快照比对 `XidInMVCCSnapshot`([src/backend/utils/time/snapmgr.c:1856](../postgresql-17.0/src/backend/utils/time/snapmgr.c#L1856))。
- **clog(提交日志)**:[src/backend/access/transam/clog.c](../postgresql-17.0/src/backend/access/transam/clog.c)(SLRU 实现,L811 `SimpleLruInit` 用 `"pg_xact"`);状态定义 [src/include/access/clog.h](../postgresql-17.0/src/include/access/clog.h)(L25-30,4 种状态);查询入口 `TransactionIdDidCommit`([src/backend/access/transam/transam.c:126](../postgresql-17.0/src/backend/access/transam/transam.c#L126))、`TransactionLogFetch`(L51,带单条目缓存)。
- **UPDATE/DELETE/INSERT 怎么改版本**:[src/backend/access/heap/heapam.c](../postgresql-17.0/src/backend/access/heap/heapam.c) 的 `heap_insert`(L1994,设 xmin)、`heap_delete`(L2683,设旧版本 xmax)、`heap_update`(L3150,两步:旧版本设 xmax + t_ctid 在 L3948,新版本设 xmin 在 L3634)。
- **事务号分配与回绕**:[src/backend/access/transam/varsup.c](../postgresql-17.0/src/backend/access/transam/varsup.c) 的 `GetNewTransactionId`(L77,含三道防线 L123-L167)、`SetTransactionIdLimit`(L367,算各阈值);特殊事务号 [src/include/access/transam.h](../postgresql-17.0/src/include/access/transam.h)(L31-35,`FrozenTransactionId=2` 等)。
- **冻结**:[src/backend/access/heap/heapam.c](../postgresql-17.0/src/backend/access/heap/heapam.c) 的 `heap_prepare_freeze_tuple`(L6550,冻结判定与计划)、`heap_freeze_tuple`(L6922);VACUUM 怎么批量冻结见 P6 第 21 章。
- **整章的总纲注释**:`heapam_visibility.c` 顶部 L40-L100 的那段大注释,是 PG 内核对"MVCC 怎么用 xmin/xmax/clog/hint bit 协同"的官方说明,强烈建议配合本章一起读。

---

> MVCC 讲完了——读写不阻塞的根源是"多版本 + 快照",死元组是它的代价。但死元组不会自己消失,事务号也不会自己腾出来——这两笔账,都得靠 **VACUUM** 来还。VACUUM 扫表、回收死元组空间、冻结老 tuple、推进 `datfrozenxid`,是 MVCC 能长期运转的"清道夫"。翻开 **P6 第 21 章 · VACUUM:MVCC 的代价**,看 PG 怎么打扫这张多版本桌子,以及 autovacuum 怎么自动守护数据库不被死元组和回绕淹没。
>
> 不过在去 P6 之前,我们还有 P5 要走——**持久性与崩溃恢复**。MVCC 让并发不乱,但内存里的所有这些改动(xmin/xmax 的设置、新版本的写入),掉电就没了。PG 怎么敢说"COMMIT 成功就永久生效"?答案在 **WAL(预写日志)**——改数据之前先把改动记进日志,日志先落盘。P5 三章(WAL / Checkpoint / 崩溃恢复)讲的就是这条"不丢"的命脉。MVCC 解决"不乱",WAL 解决"不丢",两者合起来才是完整的"让数据不丢不乱"。
