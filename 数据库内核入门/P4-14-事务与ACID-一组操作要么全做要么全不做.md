# 第 14 章 · 事务与 ACID:一组操作,要么全做要么全不做

> **前置**:你需要先读过[第 1 章《第一性原理》](P0-01-第一性原理-为什么需要数据库.md)——那里我们把 ACID 作为"用文件存数据的四个乱子"的解药介绍过(概念层面)。本章是 **P4 事务与并发控制的第一章**,我们从概念下沉到**机制**:事务在 PG 内部到底是个什么东西?它的一生怎么被管理?`COMMIT` / `ROLLBACK` 那一刻到底发生了什么,才敢向客户端做出 ACID 那四个承诺?

> **核心问题**:事务(transaction)在数据库内部究竟是怎么实现的?PG 用什么数据结构记住"现在在哪个事务里、处于什么阶段"?一句 `COMMIT`,内部走了多少步,才敢承诺"永久生效、掉电不丢"?一句 `ROLLBACK`,又凭什么能把改动全部抹掉?
>
> **读完本章你会明白**:PG 用**两层状态机**管理事务的一生;`COMMIT` 是一套严格的多阶段流程(触发器 → 写 commit record → 刷 WAL 落盘 → 释放锁);PG 的原子性靠**不记 undo 的 redo-only 设计**(这是它和 InnoDB 的根本差异);以及 ACID 四个字母各由什么机制兑现。

> **如果一读觉得太难**:先只记住三件事——① 事务在 PG 里是个状态机,`COMMIT`/`ROLLBACK` 触发状态流转;② PG 不记 undo,回滚靠"丢弃没刷盘的脏页 + 标记旧行无效";③ 持久性(D)全靠 `COMMIT` 时把 WAL 刷盘(fsync)。其余细节第二遍配合源码再抠。

---

## 一、先看清"事务"到底是什么

很多用过数据库的人,反而对"事务到底什么时候开始、什么时候结束"糊涂。先把这件基础事讲清。

### 显式事务:你自己写的 BEGIN...COMMIT

最直观的形态:

```sql
BEGIN;                                          -- 事务开始
UPDATE accounts SET balance = balance - 100 WHERE id = 1;
UPDATE accounts SET balance = balance + 100 WHERE id = 2;
COMMIT;                                         -- 事务结束(或 ROLLBACK)
```

`BEGIN` 到 `COMMIT`(或 `ROLLBACK`)之间,是一个**事务块(transaction block)**。这中间的所有 SQL,共享同一个"要么全做、要么全不做"的承诺。

### 隐式事务:你没写 BEGIN,事务也在

但很多人没意识到:**PostgreSQL 默认下,即使你不写 `BEGIN`,每一条单独的 SQL 也是一个事务**——只是个"只含一句话"的短事务,执行完自动提交(autocommit)。

```sql
DELETE FROM users WHERE id = 999;   -- 这本身就是一个完整事务:执行完立刻提交
```

这条 `DELETE` 没有外层 `BEGIN`,但它**仍然在一个事务里跑**——PG 在它开始时悄悄开了个事务,执行完悄悄提交了。这就是为什么单条 SQL 也有原子性(它要么完整生效、要么完整回滚,绝不会半残)。

> **一个反直觉的事实**:在 PG 里,"不在事务里"几乎是不存在的常态。绝大多数时候你的语句都在某个(哪怕只活一瞬的)事务里。区别只是:是**你自己用 `BEGIN` 圈出来的显式事务块**,还是 **PG 自动给你包的隐式单语句事务**。这两种形态,在状态机里是两个不同的状态(下面会看到 `TBLOCK_INPROGRESS` vs `TBLOCK_STARTED`)。

### 一个贯穿全章的例子:转账

后面我们反复用这个例子:把 100 块从账户 1 转到账户 2。它涉及两次 `UPDATE`,**必须作为一个事务**——否则就会出现第 1 章那个"扣了钱没到账"的半残灾难。我们来看 PG 内部是怎么把这两次 `UPDATE` 捆成一个不可分割的整体的。

---

## 二、为什么需要事务:把"半残状态"这个灾难讲透

第 1 章我们用"转账转一半掉电"引出了原子性。这里把它讲得更透:为什么"半残"是数据管理里**最不能容忍**的灾难。

### 半残状态的可怕之处

假设没有事务,两次 `UPDATE` 各自独立、各自可能失败:

> **不这样会怎样**:UPDATE 1(扣钱)成功,UPDATE 2(加钱)执行到一半机器崩了。结果是账户 1 少了 100,账户 2 没多——**100 块凭空蒸发**。更可怕的是:
> - 这个错误是**静默**的——没人会立刻发现,因为两个账户单独看都"正常";
> - 它是**不可逆**的——你不知道正确的状态应该是什么(是回退 UPDATE 1?可它已经生效了),只能靠人工对账,而人工对账在海量数据下根本不现实;
> - 它会**累积**——每发生一次,账面就差一点,时间一长,整个系统的数据都不可信了。

这种"系统处于一个谁都不想要的中间态,且无法自动恢复"的情况,就是事务要消灭的东西。事务的本质,是给一组操作加一个**边界**:在这个边界内,要么整体成功(对外可见),要么整体失败(像没发生过),**绝不把中间态泄漏出去**。

### 不止转账:半残无处不在

"多步操作必须原子"的场景到处都是:

- **电商下单**:扣库存 + 创建订单 + 扣余额 + 生成物流记录,这几步要么全成、要么全废,否则超卖或空订单;
- **银行结算**:批量转账、对账,每笔都涉及多个账户的联动修改;
- **复杂更新**:更新主表 + 同步更新多张关联表 + 维护汇总数据。

这些场景的共同点是:**业务上的"一个操作",在数据库层面是"多条 SQL",它们必须捆成一个事务。** 事务就是那根捆它们的绳子。

---

## 三、ACID:四个承诺,各有兑现机制

第 1 章我们把 ACID 作为四个乱子的解药介绍过(概念)。本章看完机制,可以精确地说**每个字母到底由 PG 内部的什么兑现**。这是理解后面所有章节的钥匙。

### A — 原子性:PG 靠"不记 undo"实现,这是它和 InnoDB 的根本差异

原子性问的是:事务中途失败(或主动 ROLLBACK),怎么把已经做了一半的改动**全部抹掉**?

主流数据库有两种思路:

**思路一:记 undo 日志(InnoDB 的做法)。** 改一页数据之前,先记下"这页改之前长什么样"(undo)。回滚时,按 undo 把页改回原样。代价:每次修改都要多写一份 undo,而且 undo 自己也得管(也得有日志、也得回收)。

**思路二:PG 的做法——redo-only,根本不记 undo。** PG 记的是"改之后长什么样"(redo,即 WAL),但不记"改之前长什么样"。那回滚靠什么?靠两个事实:

1. **未提交事务的脏页,PG 尽量不刷盘**。一个事务还没 `COMMIT`,它改过的页要么还在 Buffer Pool 内存里(abort 时直接丢弃即可),要么即使被刷盘了,页里那行也带着状态标记能被识别为"未提交 / 已回滚"(第 17 章 MVCC 详讲)。
2. **旧行还在**。PG 更新一行不是原地改,而是写一个新版本、旧行留着(这是 MVCC)。abort 一个事务,只要让它的"新版本"作废,旧版本天然还在,数据自然回到改之前的样子。

> **为什么 PG 敢不要 undo?** 因为它的 MVCC 设计让"旧数据"一直都在——回滚不需要"恢复",只需要"作废新版本"。这是 MVCC 带来的红利:它把原子性(回滚)和隔离性(多版本)用同一套机制解决了。代价是旧行不会立刻消失,需要 VACUUM 打扫(P6)。这套设计是 PG 区别于 InnoDB 最深刻的一点,也是第 17 章的核心。

### C — 一致性:约束 + A + I 的共同结果

一致性说的是:无论怎么操作,数据都必须满足**预先定义的规则**——主键唯一、外键存在、CHECK 成立、NOT NULL、UNIQUE……事务不能把数据改成一个违反规则的状态。

> **不这样会怎样**:如果允许事务把数据改成"主键重复""外键指向不存在的行""余额变成负数(违反 CHECK)",那数据的"含义"就崩了——主键不唯一就无法可靠定位,外键断裂就出现孤儿记录,业务规则被绕过。系统会陷入"数据都在,但谁都不敢信"的状态。

一致性是怎么兑现的?它是**三件事的共同结果**:

1. **数据库强制执行的约束**:你在建表时定义的主键、外键、CHECK 等,每次修改 PG 都会检查,违反就拒绝(事务 abort)。
2. **原子性(A)**:保证约束检查是"全有或全无"的——不会出现"检查了一半"。
3. **隔离性(I)**:保证并发事务不会互相破坏对方维护的不变量。

> **注意区分两种一致性**:数据库能强制的(主键 / 外键 / CHECK)叫**数据库一致性**;而像"转账前后总金额不变"这种**业务规则**,数据库并不知道,得靠应用层用事务(A + I)来保证。所以一致性有一部分是应用的责任,数据库提供的是"约束检查"和"A、I 这两块地基"。

### I — 隔离性:并发事务的协调,后三章的主角

隔离性说:多个事务并发执行时,每个都应该**感觉不到其他事务的存在**。这是 P4 最难、最精华的部分,本章只做铺垫。

先看"感觉不到别人"为什么难。两个事务并发时,如果不隔离,会出现这些**并发异常**(第 15 章详讲):

- **脏读**:事务 A 读到了事务 B **还没提交**的改动。B 一旦回滚,A 就读到了一个根本没存在过的数据。
- **不可重复读**:事务 A 两次读同一行,值不一样——因为 B 在中间改了并提交了。
- **幻读**:事务 A 两次按条件查,行数不一样——因为 B 在中间插入 / 删除了符合条件的行。
- **丢失更新**:前面讲过的,A、B 都基于"旧值"算"新值"后写回,后写的覆盖先写的。

完全消除这些异常(让并发事务的效果**完全等同于一个一个串行执行**,这叫**可串行化 / Serializable**)代价很大——要加很多锁、并发度大降。所以现实里数据库提供不同档位的**隔离级别**(Read Uncommitted / Read Committed / Repeatable Read / Serializable),让你在"隔离得多干净"和"并发得多高"之间权衡。

> **PG 的杀器是 MVCC**(多版本并发控制):它让**读不阻塞写、写不阻塞读**——读者看旧版本、写者写新版本,各取所需。这是 PG 实现隔离性的核心,也是为什么它敢"不记 undo"(见上面 A 那节,A 和 I 共用 MVCC 这一套)。第 15、16、17 章会把它讲透。本章你只需记住:**隔离性 = 并发事务不打架,兑现它的是锁 + MVCC。**

### D — 持久性:fsync,以及"存储设备会撒谎"

持久性说:`COMMIT` 一旦成功,改动就**永久生效**,掉电也不丢。

这个承诺的实现,核心是一句:**`COMMIT` 时,把这条事务的 WAL 日志 `fsync` 到磁盘**。只要日志落了盘,数据哪怕还在内存没落盘,掉电后也能靠重放日志恢复(第 18 章详讲)。

但这里有个容易被忽略、却极其关键的现实:**存储设备会撒谎。**

> **不这样会怎样**:你以为 `fsync` 返回了,数据就真的写到磁盘盘片了。可很多磁盘(尤其便宜的 SATA 盘、SSD)**自带一块缓存**(disk cache),`fsync` 可能只是把数据写到了这块缓存就返回"成功"。这时如果**断电**,缓存里的数据会丢——`fsync` 骗了你,数据其实没落盘。这就是为什么生产数据库要么用**电池备份的 RAID 卡**(BBU,缓存掉电不丢),要么**显式禁用磁盘缓存**。这是数据库管理员必须懂、却常被忽视的一层。

PG 还把这个权衡交给你:**同步提交**(默认,`COMMIT` 等 fsync 完成才返回,最安全、有延迟)vs **异步提交**(`synchronous_commit=off`,`COMMIT` 日志到内存就返回,快但提交后瞬间掉电可能丢)。第 18 章会展开。

### ACID 兑现机制总表

| 字母 | 兑现机制 | 关键代码 / 章节 |
|---|---|---|
| **A** 原子性 | redo-only(不记 undo)+ MVCC 旧行还在 + 不刷未提交脏页 | 本章 + 第 17 章 |
| **C** 一致性 | 约束检查(主键 / 外键 / CHECK)+ A + I | 本章 |
| **I** 隔离性 | 锁 + **MVCC** | 第 15、16、17 章 |
| **D** 持久性 | `COMMIT` 时 WAL **fsync 落盘** | 第 18 章 + 本章 |

---

## 四、事务的一生:两层状态机

现在进入机制的核心。PG 怎么记住一个事务"现在在哪个阶段"?答案是**两层状态机**。

### 为什么是两层

PG 给事务状态设了两个独立的枚举,各管一摊:

- **`TransState`(低层状态)**:事务**引擎**视角的状态——这个事务是空闲、刚开始、进行中、正在提交、正在回滚……它关心的是"事务引擎内部,这事务处于什么执行阶段"。
- **`TBlockState`(高层状态)**:**用户**视角的事务块状态——你在用 `BEGIN/COMMIT/ROLLBACK/SAVEPOINT` 这些 SQL 指挥它,它记录"用户视角下,这事务块走到哪一步了"。

> **为什么不一层就够?** 因为用户命令和引擎阶段不是一一对应的。比如用户发 `COMMIT`,在引擎里要先经过"pre-commit 触发器""写 commit record""刷 WAL"好几个子阶段,这些是引擎的事,用户不关心;而用户关心的"我现在在一个显式 BEGIN 里吗""上条语句出错了我得 ROLLBACK 吗"这些,用 `TBlockState` 记录更直接。两层分工,各管各的复杂度。

### 低层:`TransState`

> [src/backend/access/transam/xact.c:139-147](../postgresql-17.0/src/backend/access/transam/xact.c#L139-L147)

```c
typedef enum TransState
{
	TRANS_DEFAULT,				/* idle */
	TRANS_START,				/* transaction starting */
	TRANS_INPROGRESS,			/* inside a valid transaction */
	TRANS_COMMIT,				/* commit in progress */
	TRANS_ABORT,				/* abort in progress */
	TRANS_PREPARE,				/* prepare in progress */
} TransState;
```

六个状态,对应事务引擎的六个阶段:空闲 → 启动中 → 进行中 → 提交中 → 回滚中(还有个两阶段提交的 PREPARE,进阶话题)。事务的一生,就是在这几个状态间流转。

### 高层:`TBlockState`

> [src/backend/access/transam/xact.c:155-170](../postgresql-17.0/src/backend/access/transam/xact.c#L155-L170)

```c
typedef enum TBlockState
{
	/* not-in-transaction-block states */
	TBLOCK_DEFAULT,				/* idle */
	TBLOCK_STARTED,				/* running single-query transaction */

	/* transaction block states */
	TBLOCK_BEGIN,				/* starting transaction block */
	TBLOCK_INPROGRESS,			/* live transaction */
	TBLOCK_IMPLICIT_INPROGRESS, /* live transaction after implicit BEGIN */
	TBLOCK_PARALLEL_INPROGRESS, /* live transaction inside parallel worker */
	TBLOCK_END,					/* COMMIT received */
	TBLOCK_ABORT,				/* failed xact, awaiting ROLLBACK */
	TBLOCK_ABORT_END,			/* failed xact, ROLLBACK received */
	TBLOCK_ABORT_PENDING,		/* live xact, ROLLBACK received */
	TBLOCK_PREPARE,				/* live xact, PREPARE received */

	/* subtransaction states */
	TBLOCK_SUBBEGIN,			/* starting a subtransaction */
	TBLOCK_SUBINPROGRESS,		/* live subtransaction */
	TBLOCK_SUBRELEASE,			/* RELEASE received */
	TBLOCK_SUBCOMMIT,			/* COMMIT received while TBLOCK_SUBINPROGRESS */
	...
```

注意它的分组,泄露了设计:

- **`TBLOCK_STARTED`**:那条"隐式事务"——你没写 BEGIN,单条语句自动包的事务(对应第一节讲的隐式事务)。
- **`TBLOCK_BEGIN → TBLOCK_INPROGRESS → TBLOCK_END`**:显式事务块的正常一生(`BEGIN` → 执行 → 收到 `COMMIT`)。
- **`TBLOCK_ABORT / TBLOCK_ABORT_END / TBLOCK_ABORT_PENDING`**:出错分支——事务里某条 SQL 失败,进入 `TBLOCK_ABORT`(等待 `ROLLBACK`),或正常事务中收到 `ROLLBACK`(`TBLOCK_ABORT_PENDING`)。
- **`TBLOCK_SUB*`**:子事务 / savepoint 的状态(第六节讲)。

### 状态流转图

把主要流转画出来(隐式事务和出错分支):

```text
   ┌───────────────┐   语句到达(无BEGIN)    ┌───────────────┐
   │TBLOCK_DEFAULT  │ ───────────────────▶ │TBLOCK_STARTED  │ (隐式单语句事务)
   └───────┬───────┘                        └───────┬───────┘
           │ BEGIN                                  │ 语句执行完,自动提交
           ▼                                        │
   ┌───────────────┐                               │
   │ TBLOCK_BEGIN   │                               │
   └───────┬───────┘                               │
           ▼                                        │
   ┌─────────────────┐   某条SQL出错              │
   │TBLOCK_INPROGRESS │ ──────────▶ ┌────────────┐ │
   └────────┬────────┘              │ TBLOCK_ABORT│(等ROLLBACK)
            │ COMMIT                 └──────┬─────┘
            ▼                              │ ROLLBACK
   ┌───────────────┐                       ▼
   │  TBLOCK_END    │              ┌────────────────┐
   └───────┬───────┘              │TBLOCK_ABORT_END │
           ▼                       └───────┬────────┘
      (提交,回到 DEFAULT)                  ▼
                                    (回滚,回到 DEFAULT)
```

这张图背后,是 `CurrentTransactionState` 指针指向的当前状态块。每条 SQL 来了,PG 根据 `blockState` 决定怎么处理、处理完更新到下一个状态。

---

## 五、关键源码精读:从出生到提交

我们顺着源码,把事务的"出生 → 提交 → (回滚)"钉死。

### 1. 事务的出生:`StartTransaction`

每开始一个事务(无论隐式还是显式 BEGIN 之后),PG 调 `StartTransaction`。它的第一个动作是**状态校验 + 流转**:

> [src/backend/access/transam/xact.c:2028-2037](../postgresql-17.0/src/backend/access/transam/xact.c#L2028-L2037)

```c
	/* check the current transaction state */
	Assert(s->state == TRANS_DEFAULT);

	/*
	 * Set the current transaction state information appropriately during
	 * start processing. Note that once the transaction status is switched
	 * this process cannot fail until ...
	 */
	s->state = TRANS_START;
	s->fullTransactionId = InvalidFullTransactionId;	/* until assigned */
```

注意两点:

- **`Assert(s->state == TRANS_DEFAULT)`**:开始新事务前,旧事务必须已经彻底结束(回到 `TRANS_DEFAULT`)。这是状态机的护栏——不允许"事务里再开事务"(那是子事务,走另一套 `TBLOCK_SUB*` 状态,不是重新 StartTransaction)。
- **`s->state = TRANS_START`**:状态流转开始。注释点出一个重要约束:状态一旦切换,后续就不能随便失败——因为事务已经"开始了",中途失败需要走 abort 流程,不能简单返回。

### 2. 事务状态的载体:`TransactionStateData`

承载上面这些状态字段的,是 `TransactionStateData` 结构:

> [src/backend/access/transam/xact.c:191-216](../postgresql-17.0/src/backend/access/transam/xact.c#L191-L216)

```c
typedef struct TransactionStateData
{
	FullTransactionId fullTransactionId;	/* my FullTransactionId */
	...
	TransState	state;			/* low-level state */
	TBlockState blockState;		/* high-level state */
	int			nestingLevel;	/* transaction nesting depth */
	...
	struct TransactionStateData *parent;	/* back link to parent */
} TransactionStateData;
```

注意 `state`(低层)和 `blockState`(高层)两个字段并存——这正是第四节"两层状态机"在数据结构上的体现。而 `parent` 指针,把状态块串成栈(子事务用)。

`CurrentTransactionState` 永远指向栈顶当前事务:

```c
static TransactionState CurrentTransactionState = &TopTransactionStateData;
```

> ([src/backend/access/transam/xact.c:257](../postgresql-17.0/src/backend/access/transam/xact.c#L257))

### 3. 提交的入口:`CommitTransaction` 先做校验和触发器

用户发 `COMMIT`,PG 调 `CommitTransaction`。开头是状态校验:

> [src/backend/access/transam/xact.c:2178-2198](../postgresql-17.0/src/backend/access/transam/xact.c#L2178-L2198)

```c
static void
CommitTransaction(void)
{
	TransactionState s = CurrentTransactionState;
	...
	/*
	 * check the current transaction state
	 */
	if (s->state != TRANS_INPROGRESS)
		elog(WARNING, "CommitTransaction while in %s state",
			 TransStateAsString(s->state));
	Assert(s->parent == NULL);   // 只处理顶层事务
```

然后是 pre-commit 阶段(跑延迟触发器,直到没有新的要做):

```c
	for (;;)
	{
		AfterTriggerFireDeferred();   // 触发延迟触发器
		...
	}
```

> ([src/backend/access/transam/xact.c:2207-2212](../postgresql-17.0/src/backend/access/transam/xact.c#L2207-L2212))

为什么 pre-commit 要在"写日志 / 刷盘"之前?**因为触发器可能产生新的数据改动**(比如某个 `BEFORE COMMIT` 触发器又插了几行),这些改动**也必须属于这个事务、一起提交**。所以先把所有"用户定义的副作用"跑完,才能进入不可逆的提交阶段。

### 4. 提交的硬核:`RecordTransactionCommit` 写 commit record 并刷盘

pre-commit 干净后,提交进入**不可逆**阶段,核心是 `RecordTransactionCommit`。它先收集要写进 commit record 的各种信息:

> [src/backend/access/transam/xact.c:1304-1337](../postgresql-17.0/src/backend/access/transam/xact.c#L1304-L1337)

```c
RecordTransactionCommit(void)
{
	TransactionId xid = GetTopTransactionIdIfAny();
	bool		markXidCommitted = TransactionIdIsValid(xid);
	...
	/* Get data needed for commit record */
	nrels = smgrGetPendingDeletes(true, &rels);              // 这个事务删了哪些文件
	nchildren = xactGetCommittedChildren(&children);         // 有哪些子事务一起提交
	...
```

然后是整个提交里**最关键、最不可中断**的一段——进入临界区,写 commit record:

> [src/backend/access/transam/xact.c:1415-1427](../postgresql-17.0/src/backend/access/transam/xact.c#L1415-L1427)

```c
	START_CRIT_SECTION();
	MyProc->delayChkptFlags |= DELAY_CHKPT_START;

	/*
	 * Insert the commit XLOG record.
	 */
	XactLogCommitRecord(GetCurrentTransactionStopTimestamp(),
						nchildren, children, nrels, rels,
						...);
```

这几行信息量极大,逐个拆:

- **`START_CRIT_SECTION()`**:进入"临界区"。从这一刻起,代码**绝不允许出错**——出错就只能 PANIC 停机,不能优雅回滚。因为下面要做的事(写 commit record)一旦做了一半,事务状态就不可恢复了。
- **`MyProc->delayChkptFlags |= DELAY_CHKPT_START`**:**阻止 checkpoint 在此刻插队**。为什么?checkpoint 会推进"数据已落盘到哪个位置"。如果 checkpoint 插在"写了 commit record"和"数据页刷盘"之间,可能造成数据不一致。所以提交期间要摁住 checkpoint。这种"摁住"的细节,正是数据库正确性的微观体现。
- **`XactLogCommitRecord(...)`**:写出**commit record**——这条 WAL 记录,是**这个事务"已提交"的铁证**。它记下:这个事务的 XID、它有哪些子事务、它删了哪些文件、提交时间戳……**崩溃恢复时,数据库就是靠"有没有这条 commit record"来判断"这个事务到底提交了没有"**——有,就当作已提交(它的改动有效);没有,就当作没提交(回滚)。

写完 commit record 后(函数更靠后),就是**等 WAL fsync 落盘**——这一步完成,持久性(D)才算兑现,`COMMIT` 才能向客户端返回"成功"。

> **回头看整个 `COMMIT`**:校验状态 → pre-commit 触发器(可逆)→ 【临界区:写 commit record → 刷 WAL 落盘(不可逆)】→ 释放锁 → 清理资源 → 状态流转到 `TRANS_COMMIT`。其中**临界区那一段,是 ACID 里 A 和 D 真正落地的瞬间**。

### 5. 回滚:`AbortTransaction`

如果事务中途出错,或用户发 `ROLLBACK`,PG 调 `AbortTransaction`([src/backend/access/transam/xact.c:2749](../postgresql-17.0/src/backend/access/transam/xact.c#L2749))。

回滚比提交"简单"——因为 PG 不记 undo(见第三节 A 那节):

- **不需要把数据页改回去**。未提交事务的脏页,要么还在内存(直接丢弃)、要么带着"未提交"标记。
- **旧行天然还在**(MVCC),只要让这个事务产生的新版本作废即可。
- 主要工作是**清理**:释放这个事务加的锁、撤销它的资源占用、写一条 abort record(告诉崩溃恢复"这事务没提交")、把状态流转到 `TRANS_ABORT`。

> 这正是 redo-only 设计的红利:**回滚不需要"恢复数据",只需要"丢弃 + 标记 + 清理"**,比 InnoDB 那种按 undo 逐页恢复简单得多。代价是旧行堆积(要 VACUUM),这是 P6 的话题。

---

## 六、子事务与 savepoint:事务也能嵌套

最后讲一个进阶但重要的点:事务可以**嵌套**。

```sql
BEGIN;
  UPDATE accounts SET balance = balance - 100 WHERE id = 1;
  SAVEPOINT sp1;
  UPDATE accounts SET balance = balance + 100 WHERE id = 2;   -- 假设这句出错
  ROLLBACK TO sp1;    -- 只回滚到 savepoint,事务本身不结束
  -- 这里可以重试别的做法,然后 COMMIT
COMMIT;
```

`SAVEPOINT` 创建一个**子事务**。`ROLLBACK TO sp1` 只撤销 savepoint 之后的改动,**不结束整个事务**。这靠的就是 `TransactionStateData` 的 `parent` 指针组成的**栈**:

- 遇到 `SAVEPOINT`,压一个新的 `TransactionStateData` 进栈(`blockState` 进入 `TBLOCK_SUB*` 系列)。
- `ROLLBACK TO`,弹出栈顶到那个 savepoint,丢弃它的改动,事务继续。
- 外层 `COMMIT`,提交整个栈(所有未撤销的子事务一起提交)。

> 子事务让"复杂操作里某一步可以局部重试"成为可能,而不必整个大事务重来。代价是栈管理和子事务的可见性规则更复杂。这是 `TBlockState` 里那一堆 `TBLOCK_SUB*` 状态存在的理由。

---

## 章末小结

### 用一段话回顾本章

事务是数据库给你的一根"绳子",把一组 SQL 捆成一个**不可分割的整体**——要么全做、要么全不做,绝不泄漏半成品状态。它兑现 ACID 四个承诺:

- **A** 靠 PG 独特的 **redo-only 设计**(不记 undo,靠 MVCC 旧行还在 + 不刷未提交脏页,回滚 = 丢弃 + 标记 + 清理);
- **C** 靠约束检查 + A + I;
- **I** 靠锁 + MVCC(后三章);
- **D** 靠 `COMMIT` 时 WAL **fsync 落盘**(还要小心"存储设备撒谎")。

而这一切的运转,靠**两层状态机**(`TransState` 引擎层 + `TBlockState` 用户层)管理事务的一生。`COMMIT` 是一套严格流程:校验 → pre-commit 触发器 → 【临界区:写 commit record → 刷 WAL】→ 释放锁 → 清理。

### 回扣主线

本章正式进入"**不丢不乱**"那一侧。事务是 A、C、I 的载体;其中 D 的兑现(刷 WAL)是第 18 章的主角,A、I 的共用机制(MVCC)是第 17 章的主角。注意一个贯穿的洞察:**PG 的原子性和隔离性共用 MVCC 这一套**,这是它设计上最优雅、也最区别于其他数据库的地方。

### 想继续深入

- 事务状态全集:[src/backend/access/transam/xact.c](../postgresql-17.0/src/backend/access/transam/xact.c) 顶部的 `TransState` / `TBlockState` 枚举(L139、L155),看全部状态。
- 提交全流程:同文件的 `CommitTransaction`(L2178)→ `RecordTransactionCommit`(L1304)→ `XactLogCommitRecord`(L5752)。
- 回滚:同文件的 `AbortTransaction`(L2749)。
- 子事务 / savepoint:同文件的 `PushTransaction` / `RollbackToSavepoint`。

---

> 事务的状态机和提交流程清楚了,ACID 四个承诺的兑现机制也落地了。但 I(隔离性)那个字母,我们只是一笔带过——"并发事务不打架"到底怎么做到?那些脏读、幻读、丢失更新,具体怎么防?翻开 **第 15 章 · 隔离级别与并发异常**。
