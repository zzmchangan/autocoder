# 第 5 篇 · 第 19 章 · 隔离级别:RR/RC/Read Uncommitted/Serializable

> **核心问题**:你背过 SQL 标准的四个隔离级别——`READ UNCOMMITTED`、`READ COMMITTED`、`REPEATABLE READ`、`SERIALIZABLE`,背过"RR 解决幻读""Serializable 最严格"。可这三件事你能讲清吗——**为什么这四个级别在 InnoDB 里,不是四套完全不同的代码,而是"同一套 MVCC + 锁的框架,只调三个旋钮"?这三个旋钮到底在源码哪一行?InnoDB 默认是 RR,凭什么默认它而不是 RC——而那么多大厂(阿里、Google、Facebook)又凭什么在内部把默认改成 RC?**这一章把四个隔离级别从动机到源码一次性收口。它是第 5 篇(锁)的收尾,也是整个"事务与并发"二分法的总收口——MVCC(P4)+ 锁(P5-16/17/18)讲了那么多零件,本章把它们组合成四个用户能选的级别。

> **读完本章你会明白**:
> 1. 四个隔离级别在 InnoDB 里**不是四套机制**,而是"read view 时机 × 间隙锁开关 × 是否把快照读转加锁读"这三个旋钮的组合——三个旋钮各取什么值,就长出什么级别。这一张总表是本章的灵魂。
> 2. **RU(读未提交)**为什么源码里 `skip_gap_locks()` 把它和 RC 一视同仁(都返回 `true`)、为什么它"可能读到未提交版本"——它的 read view 行为和 RC 几乎一样,只是 MySQL 官方文档把它标成"非一致读"。
> 3. **RC(读已提交)**每条语句新建 read view、不加间隙锁、不匹配的行扫完就放锁——三个动作在源码里各是哪一行,以及它和 RR 的"事务级 read view + 间隙锁"在并发异常(脏读/不可重复读/幻读)上的完整对照。
> 4. **RR(可重复读)**为什么是 InnoDB 默认、**Serializable(串行化)**为什么把"普通 SELECT 也转成 `LOCK IN SHARE MODE`"——后者的实现是 `ha_innodb.cc` 里一个 `if (isolation_level == SERIALIZABLE && select_lock_type == LOCK_NONE)` 的判断。
> 5. 为什么"InnoDB 默认 RR"和"大厂切 RC"都有道理——这是 **statement-based binlog 复制** vs **row-based binlog + 高写并发**两种工程约束下的取舍,也是"工程没有银弹"的范本。

> **如果一读觉得太难**:先记住三件事——① 四个级别 = 三个旋钮的组合,不是四套代码;② 三个旋钮是:**read view 何时建**(RU/RC 每条语句一个、RR/Serializable 一个事务一个)、**间隙锁开关**(RR/Serializable 开、RC/RU 关)、**普通 SELECT 是否转加锁读**(只有 Serializable 转);③ **InnoDB 默认 RR**,因为它历史地搭配 statement-based binlog 复制更安全,而大厂切 RC 是因为 row-based binlog 让 RC 也能正确复制、且 RC 不加间隙锁、写并发高。本章把前几章的零件(MVCC + 锁 + 间隙锁)拼成一张完整的隔离级别总表,是"事务与并发"这一面的收口。

---

## 〇、一句话点破

> **四个隔离级别,不是四套机制,而是同一套 MVCC + 锁的框架,只调三个旋钮:① read view 何时建;② 间隙锁开不开;③ 普通 SELECT 转不转加锁读。RR 是 InnoDB 默认——因为它事务级 read view + 开间隙锁,既"可重复读"又"防幻读",且历史地搭配 statement-based binlog 复制最安全。RC 关间隙锁 + 每条语句新 read view,在 row-based binlog 时代是更"高并发"的选择,所以大厂切它。Serializable 干脆把普通 SELECT 也变成加 S 锁的当前读,把所有并发都串行化掉。**

这是结论,不是理由。本章倒过来拆:先把四个级别用一张总表摊开,看清三个旋钮各取什么值;再逐个级别拆源码(read view 时机、间隙锁开关、是否加锁读),并配各级别的并发异常(脏读/不可重复读/幻读);然后把"InnoDB 默认 RR"这个看起来理所当然的决策,拆回到历史和工程约束里;最后两个技巧精解——一个是"三个旋钮的组合矩阵",一个是"RC 的逐语句放锁优化"。

---

## 一、先把三个旋钮认清:read view 时机 × 间隙锁 × 加锁读

(承接 P4 MVCC + P5-16/17 锁)

P4 篇讲了 MVCC,你知道"快照读看 read view 对应的版本链版本,不加锁";P5-16 讲了行锁 S/X 与两阶段锁;P5-17 讲了间隙锁/临键锁/插入意向锁;P5-18 讲了死锁。**这些零件单独都讲透了,可用户面对的不是一个"MVCC 开关"或一个"间隙锁开关",而是 `SET TRANSACTION ISOLATION LEVEL ...` 这一个 SQL 选四个级别之一**。InnoDB 怎么把"四个级别"落地的?

答案出乎意料地简洁:**它不是四套机制,而是同一套框架 + 三个旋钮**。

### 旋钮一:read view 何时建

第一个旋钮管"快照读(`SELECT`)看哪个版本"。回顾 P4-13/14:read view 是事务的"快照",记录"开快照这一刻哪些事务还在跑"。两次快照读用同一个 read view,看到的就是同一个版本("可重复读");用不同的 read view,看到的就是"各自开快照那一刻"的最新已提交("读已提交")。这个旋钮有两个取值:

- **事务级 read view**:整个事务复用一个 read view(第一次快照读时建,之后不换)。RR、Serializable 取这个值。
- **每条语句一个 read view**:事务里每条快照读语句,各自新建一个 read view。RC、RU 取这个值。

这一个旋钮,就决定了"可重复读"还是"读已提交"。P4-13 已经把它的源码拆透了(`trx_assign_read_view` 复用 + RC 在每条语句末尾 `view_close`),本章后面会回到这两处代码。

### 旋钮二:间隙锁开不开

第二个旋钮管"当前读/写(`SELECT ... FOR UPDATE`、`UPDATE`、`DELETE` 等)防不防别人往范围内插入新行"。回顾 P5-17:RR 下默认用临键锁(next-key lock = 记录锁 + 前面的间隙),把"记录"和"记录前面的空"一起锁,防止别的事务在范围内插入造成幻读。这个旋钮也有两个取值:

- **开间隙锁**:locking read/write 用 `LOCK_ORDINARY`(next-key lock,记录锁 + 间隙)。RR、Serializable 取这个值。
- **关间隙锁**:locking read/write 用 `LOCK_REC_NOT_GAP`(只锁记录本身,不锁前面的间隙)。RC、RU 取这个值。

源码里这个旋钮就是一个函数 `trx->skip_gap_locks()`,对 RC 和 RU 返回 `true`,对 RR 和 Serializable 返回 `false`——P5-17 已拆,本章再回到它。

### 旋钮三:普通 SELECT 转不转加锁读

第三个旋钮管"普通快照读 `SELECT` 还走不走 MVCC"。前两个旋钮上,RR 和 Serializable 取值一样(事务级 read view、开间隙锁),那 Serializable 凭什么"更严格"?就严格在这第三个旋钮上:

- **普通 SELECT 走 MVCC(不加锁)**:RU、RC、RR 都取这个值。
- **普通 SELECT 转成 `LOCK IN SHARE MODE`(加 S 锁的当前读)**:只有 Serializable 取这个值。

这个旋钮的源码,是 `ha_innodb.cc` 里 `if (trx->isolation_level == TRX_ISO_SERIALIZABLE && select_lock_type == LOCK_NONE ...)` 一处判断——把"本该走 MVCC 的普通 SELECT"强行改成"加 S 锁的当前读",于是所有读都串行化。本章后半段拆透它。

> **钉死这件事**:**四个隔离级别 = 三个旋钮的组合**。这不是事后归纳的口诀,而是 InnoDB 源码的真实结构——`isolation_level` 这个枚举字段,在源码里就只被这三个旋钮(以及一些边缘优化)消费。理解了这三个旋钮,你就理解了"InnoDB 的四个隔离级别不是四套代码,而是一套代码三个开关"。这是本章最重要的洞察。

---

## 二、四级别总表:把三个旋钮摊成一张表

把三个旋钮各取什么值、对应的并发异常防住哪些,摊成一张总表——这张表是理解四个隔离级别的"中央索引":

### 四隔离级别在 InnoDB 里的三旋钮配置

| 级别 | **read view 时机** | **间隙锁** | **普通 SELECT** | 防脏读 | 防不可重复读 | 防幻读 |
|------|-------------------|-----------|----------------|--------|-------------|--------|
| **READ UNCOMMITTED (RU)** | 每条语句(类似 RC) | 关(`skip_gap_locks=true`) | 走 MVCC,但"非一致" | ❌(可能脏读) | ❌ | ❌ |
| **READ COMMITTED (RC)** | 每条语句新建 | 关(`skip_gap_locks=true`) | 走 MVCC | ✅ | ❌ | ❌ |
| **REPEATABLE READ (RR,默认)** | 事务级(整事务复用一个) | 开(next-key lock) | 走 MVCC | ✅ | ✅ | ✅(InnoDB 特有,SQL 标准不保证) |
| **SERIALIZABLE** | 事务级 | 开 | **转加锁读**(S 锁当前读) | ✅ | ✅ | ✅ |

(对照 SQL 标准:SQL 标准里 RR **不保证**防幻读,但 InnoDB 的 RR **额外用间隙锁防住**了——这是 InnoDB 比标准更"严格"的地方,也是它"敢默认 RR"的底气之一。)

### 三个旋钮怎么读这张表

横向看一个级别:把它的三旋钮配置串起来,就得到它的全部行为。例如 RR = "事务级 read view" + "开间隙锁" + "普通 SELECT 走 MVCC",于是它:① 事务级 read view 让两次读同一行看同一版本(防不可重复读);② 开间隙锁让范围查询两次之间别人插不进来(防幻读);③ 普通 SELECT 走 MVCC 不加锁,所以读写还是不互斥。

纵向看一个旋钮:三旋钮的取值在不同级别间是怎么变的。比如 read view 时机这一列,RU/RC 是"每条语句",RR/Serializable 是"事务级"——这就是"RC vs RR 的全部差别"在哪一行;间隙锁这一列,RU/RC 是"关",RR/Serializable 是"开"——这就是"为什么 RC 写并发高、RR 防幻读";加锁读这一列,只有 Serializable 是"转",其余都"不转"——这就是"Serializable 凭什么最严格"。

### 并发异常:脏读、不可重复读、幻读

表的右半边是每个级别**防住**的并发异常。这三个异常的精确定义(承 P4-13):

- **脏读(dirty read)**:事务 A 读到了事务 B **还没提交**的修改;B 万一回滚,A 读到的是"从没存在过"的数据。最严重。
- **不可重复读(non-repeatable read)**:事务 A 里两次读**同一行**,值不一样(中间 B 改了并提交)。
- **幻读(phantom read)**:事务 A 里两次**同样的范围查询**,结果集行数变了(中间 B 在范围内插了新行)。

```
   隔离级别严格性:从左到右越来越强(防住的异常越来越多)
   ────────────────────────────────────────────────────────────
   RU ─── RC ─── RR ─── Serializable
   ↑      ↑      ↑           ↑
   脏读   防脏读  防不可重复读  全防(还把 SELECT 串行化)
   不可重复读  幻读   幻读(InnoDB 特有防)
   幻读
```

> **不这样会怎样**:如果不要这套"按级别分档",要么所有事务都按 Serializable 跑(并发废掉——每个 SELECT 都加 S 锁,读写全互斥),要么所有事务都按 RU 跑(读到脏数据,正确性没保障)。"分档"的意义就是:**让用户在"严格"和"并发"之间,按业务场景自己挑**。InnoDB 把默认放在 RR(比 SQL 标准的 RR 更强、防幻读),把"切到 RC"留给高并发场景,把 Serializable 留给"必须严格串行"的极少数场景。

---

## 三、RU(读未提交):几乎不用的"危险级别"

从最宽松的 RU 开始倒着讲,后面逐级加严。RU 是 SQL 标准里最不严格的级别——允许脏读。

### RU 的源码事实:它在三旋钮上的取值

很多人以为 RU "完全不加锁、读最新页"——其实 InnoDB 的 RU **没有这么朴素**。看三旋钮:

- **read view 时机**:源码里 RU 在多数路径上**和 RC 一样走 MVCC**(每条快照读仍然 `trx_assign_read_view`)。但有个关键例外:**采样读**(`TABLESAMPLE`)路径下,源码显式跳过 read view 分配:
  ```c
  /* handler/ha_innodb.cc:10968-10970(源码原文) */
  if (trx->isolation_level > TRX_ISO_READ_UNCOMMITTED) {
    trx_assign_read_view(trx);
  }
  ```
  —— 见 [ha_innodb.cc 采样读跳过 view](../mysql-server/storage/innobase/handler/ha_innodb.cc#L10968-L10970)。这里 `TRX_ISO_READ_UNCOMMITTED` 是枚举最小值(0),RU 时 `isolation_level > TRX_ISO_READ_UNCOMMITTED` 为假,**直接不分配 read view**,于是这条采样路径读到的就是当前页里的最新字节(可能含未提交的修改)。MySQL 官方文档对 RU 的描述("SELECT statements are performed in a nonlocking fashion, but a possible earlier version of a row might be used... Otherwise, this isolation level works like READ COMMITTED")说的就是这个意思——它在多数路径上像 RC,但在某些边缘路径上"非一致"。

- **间隙锁**:`skip_gap_locks()` 对 RU 返回 `true`(和 RC 一样):
  ```c
  /* include/trx0trx.h:1113-1124(源码原文,trx_t::skip_gap_locks) */
  bool skip_gap_locks() const {
    switch (isolation_level) {
      case READ_UNCOMMITTED:
      case READ_COMMITTED:
        return (true);
      case REPEATABLE_READ:
      case SERIALIZABLE:
        return (false);
    }
    ut_d(ut_error);
    ut_o(return (false));
  }
  ```
  —— 见 [trx_t::skip_gap_locks](../mysql-server/storage/innobase/include/trx0trx.h#L1113-L1124)。所以 RU 下,locking read/write 一律只用 `LOCK_REC_NOT_GAP`,不加间隙锁。

- **普通 SELECT 转加锁读**:不转(RU 走 MVCC,像 RC)。

> **钉死这件事**:InnoDB 的 RU **不是"完全不隔离"**,它在源码里被当成"和 RC 共用一套代码,只是允许更宽松的读"。`skip_gap_locks()` 把 RU 和 RC 一并处理;`isolation_level > TRX_ISO_READ_UNCOMMITTED` 这种判断只在采样读等边缘路径出现。**生产里几乎没人用 RU**(读到脏数据的正确性风险太大),它在 InnoDB 里更多是个"为了凑齐 SQL 标准四级"的合规级别。

### 朴素陷阱:"RU = 不加锁读最新"是简化说法

很多博客说"RU 就是直接读页里的最新字节,不加锁不看版本",这是过度简化。准确的源码事实是:① 普通 `SELECT` 在主流路径上仍然 `trx_assign_read_view`(和 RC 一样走 MVCC 快照);② 只有少数路径(采样读、个别内部读取)显式跳过 read view;③ locking read/write 不加间隙锁(和 RC 一样)。所以"RU 比 RC 更宽松"在 InnoDB 里主要体现在那几个显式跳过 read view 的边缘路径,主流读路径上两者几乎没差别。这也是为什么 MySQL 官方文档对 RU 的描述里有一句"Otherwise, this isolation level works like READ COMMITTED"——它在大多数行为上**就是 RC**。

---

## 四、RC(读已提交):大厂切它,因为它高并发

RC 是"读已提交":每条语句看到当下最新已提交数据,但不防"不可重复读"和"幻读"。SQL 标准里这是第二级,很多现代数据库(PostgreSQL、Oracle)默认它。InnoDB 默认 RR 而非 RC 是个历史选择(下一节讲),但在 row-based binlog 时代,**RC 是高并发场景下更优的选择**——阿里、Google、Facebook 等大厂都内部切到 RC。

### RC 在源码里的三个动作

RC 在源码里有三个标志性动作,把它们串起来就是 RC 的全部实现:

**动作一:每条语句新建 read view(关掉上一条的)**

回顾 P4-13 的核心发现——`trx_assign_read_view` 本身不分级别(没有活动 view 才新建),RC 的"每条语句一个 view"是靠**在每条语句结束时把当前 view 关掉**实现的:

```c
/* handler/ha_innodb.cc:19749-19759(源码原文,store_lock 里) */
if (trx->isolation_level <= TRX_ISO_READ_COMMITTED &&
    MVCC::is_view_active(trx->read_view)) {
  /* At low transaction isolation levels we let
  each consistent read set its own snapshot */      // ← 注释明说:RC 让每条快照读自己开快照

  mutex_enter(&trx_sys->mutex);

  trx_sys->mvcc->view_close(trx->read_view, true);

  mutex_exit(&trx_sys->mutex);
}
```

—— 见 [ha_innodb.cc RC 关 view](../mysql-server/storage/innobase/handler/ha_innodb.cc#L19749-L19759)。注意判断条件 `isolation_level <= TRX_ISO_READ_COMMITTED`——这里 `<=` 是因为 RC 是枚举第二个(0=RU、1=RC、2=RR、3=Serializable),所以 `<=` 覆盖了 RU 和 RC。这一处判断在 `store_lock`(`ha_innodb.cc:19749`)和 `external_lock`(`ha_innodb.cc:19156`)两处几乎一模一样地出现(本章技巧精解二会单独拆它的妙处)。**RC 的下一条快照读调 `trx_assign_read_view`,发现 view 被关了,于是新建——这就实现了"每条语句一个新快照"。**

**动作二:不加间隙锁(`skip_gap_locks` 返回 true)**

前面引过 `trx_t::skip_gap_locks()`,RC 返回 `true`。这个返回值在 `row0sel.cc` 里被反复用来决定"locking read 加什么锁":

```c
/* row/row0sel.cc:864(源码原文,row_sel_get_clust_rec_for_mysql) */
lock_type = trx->skip_gap_locks() ? LOCK_REC_NOT_GAP : LOCK_ORDINARY;
```

—— 见 [row0sel.cc 864 锁类型二选一](../mysql-server/storage/innobase/row/row0sel.cc#L864)。`LOCK_ORDINARY` 就是 next-key lock(记录锁 + 间隙),`LOCK_REC_NOT_GAP` 是"只锁记录,不锁前面的间隙"。RC/RU 走后者,RR/Serializable 走前者。

还有更直接的——`row_search_for_mysql`(每条 SELECT/UPDATE/DELETE 的入口)在判断"要不要对当前记录加间隙锁"时,开头就有一段:

```c
/* row/row0sel.cc:4785-4795(源码原文,row_search_for_mysql) */
if (prebuilt->table->skip_gap_locks() ||
    (trx->skip_gap_locks() && prebuilt->select_lock_type != LOCK_NONE &&
     trx->mysql_thd != nullptr && thd_is_query_block(trx->mysql_thd))) {
  /* It is a plain locking SELECT and the isolation
  level is low: do not lock gaps */

  /* Reads on DD tables dont require gap-locks as serializability
  between different DDL statements is achieved using
  metadata locks */
  set_also_gap_locks = false;
}
```

—— 见 [row0sel.cc 4785 关间隙锁](../mysql-server/storage/innobase/row/row0sel.cc#L4785-L4795)。RC 下 `trx->skip_gap_locks()` 为 `true`,于是 `set_also_gap_locks` 被置为 `false`——整个查询里所有"要不要加间隙锁"的判断点(`row0sel.cc:4320/4893/4917/5033/5156/5190` 等十几处)看到这个标志为假就**全部跳过**。一个旋钮,十几处分支一起变。

**动作三:不匹配的行扫完就放锁(`releases_non_matching_rows`)**

这是 RC 一个常被忽略、但对死锁率影响巨大的细节。RR 下,一次 `UPDATE t SET ... WHERE b=2` 在没有索引时,会扫表给每一行都加 X 锁(并加 next-key 锁),哪怕这一行根本不匹配 `WHERE`——锁全保留到 commit。RC 下,**扫到不匹配的行,WHERE 评估完就立刻放掉那行的锁**:

```c
/* include/trx0trx.h:1127-1131(源码原文) */
/** Checks if this transaction releases locks on non matching records due to
low isolation level.
@return true iff in this transaction's isolation level locks on records which
             do not match the WHERE clause are released */
bool releases_non_matching_rows() const { return skip_gap_locks(); }
```

—— 见 [trx_t::releases_non_matching_rows](../mysql-server/storage/innobase/include/trx0trx.h#L1127-L1131)。注释明说:RC/RU 下,WHERE 不匹配的行的锁会被释放。这个行为在 `row0sel.cc:5259-5263` 的代码里就能看到踪迹:

```c
/* row/row0sel.cc:5259-5264(源码原文,RC 下标记"放锁") */
case DB_SUCCESS_LOCKED_REC:
  if (trx->releases_non_matching_rows()) {
    /* Note that a record of
    prebuilt->index was locked. */
    ut_ad(!prebuilt->new_rec_lock[row_prebuilt_t::LOCK_PCUR]);
    prebuilt->new_rec_lock[row_prebuilt_t::LOCK_PCUR] = true;
  }
  err = DB_SUCCESS;
```

—— 见 [row0sel.cc 5259](../mysql-server/storage/innobase/row/row0sel.cc#L5259-L5264)。这里记录"这条记录虽然锁上了,但因为不匹配,稍后要放掉"——后续扫描下一条时会处理这个标记。MySQL 官方文档对 RC 的描述里有一段经典例子:无索引表 `UPDATE t SET b=5 WHERE b=3`,RC 下扫到 `b=2` 的行 X 锁、评估不匹配、立刻放掉(`x-lock(1,2); unlock(1,2)`),只有真正要改的行才保留锁到 commit;RR 下扫到的每一行都保留锁到 commit。

**动作四(补):semi-consistent read——RC 独有的"先试读再决定锁"**

RC 还有一个连很多老 DBA 都没听过的优化——**semi-consistent read(半一致读)**。它解决一个具体痛点:`UPDATE ... WHERE`(无可用索引)扫到一行,这行已经被别的事务 X 锁了——RR 下只能干等,RC 下可以先"读一个已提交的旧版本"判断要不要改,如果根本不匹配 `WHERE` 就直接跳过,**根本不去等那把 X 锁**。

```c
/* row/row0sel.cc:5248-5250(源码原文) */
/* in case of semi-consistent read, we use SELECT_SKIP_LOCKED, so we don't
waste time on creating a WAITING lock, as we won't wait on it anyway */
const bool use_semi_consistent =
    prebuilt->row_read_type == ROW_READ_TRY_SEMI_CONSISTENT &&
    !unique_search && index == clust_index && !trx_is_high_priority(trx);
```

—— 见 [row0sel.cc semi-consistent](../mysql-server/storage/innobase/row/row0sel.cc#L5246-L5254)。后面 `case DB_SKIP_LOCKED:` 分支(`row0sel.cc:5272-5289`)做的事就是:遇到锁住的行,调 `row_sel_build_committed_vers_for_mysql` 读出它**最新已提交版本**,给 MySQL server 层评估 `WHERE`——如果 WHERE 不匹配,直接 `goto next_rec` 跳过(没等锁);如果匹配,MySQL 会重新读这行并真正加锁(这时才等)。

这个优化只在 RC 下启用(`allow_semi_consistent()` 直接返回 `skip_gap_locks()`,见 [`trx0trx.h:1126`](../mysql-server/storage/innobase/include/trx0trx.h#L1126))。RR 下不能启用——因为"读已提交版本再决定锁"违反"可重复读"语义(RR 必须用事务级快照,不能临时读已提交)。MySQL 官方文档对 RC 的描述里那段"InnoDB performs a 'semi-consistent' read, returning the latest committed version to MySQL so that MySQL can determine whether the row matches the WHERE condition"说的就是这个。

> **钉死这件事**:RC 的"高并发"不是一句"不加间隙锁"能概括的,它是**三个配套优化合起来**:① 不加间隙锁;② 不匹配行扫完放锁;③ semi-consistent read(锁住的行先读已提交版评估 WHERE,不匹配就跳过不等锁)。三个优化在源码里**都由 `skip_gap_locks()` 这一个开关控制**——因为它们本质都是"放松到读已提交"这一个权衡在不同场景下的表现。这种"一个开关,多个配套优化"的设计,是 InnoDB 工程优雅的体现。

### RC 的并发异常:不防"不可重复读"和"幻读"

RC 这三个旋钮(外加三个配套优化)的取值,自然带来两个不防的异常:

- **不防不可重复读**:每条语句一个新 read view,事务里两次读同一行,中间别人改了并提交,第二次读就看到新值——"不可重复读"。这是"读已提交"的字面代价。
- **不防幻读**:不加间隙锁,范围查询两次之间别人可以往范围内插新行,第二次查多出来——"幻读"。

```
   RC 下的不可重复读(同一行两次读,值变了):
   时刻   事务 A (RC)                       事务 B
   t1    BEGIN
   t2    SELECT bal FROM t WHERE id=10      → 100
          (建 read view RV1,只认已提交)
   t3                                      BEGIN
   t4                                      UPDATE t SET bal=200 WHERE id=10
   t5                                      COMMIT
   t6    SELECT bal FROM t WHERE id=10      → 200
          (语句结束关掉 RV1,新建 RV2,
           RV2 认得 B(trx=200)已提交 → 看 v2)
          ↑ 同事务两次读同一行,值不同 = 不可重复读
```

> **不这样会怎样**:如果 RC 也开间隙锁(像 RR),那它的写并发优势就没了——间隙锁是 RR 写吞吐的主要拖累。RC 故意"用放松一致性换并发":放松到"读已提交"(还防脏读),但不强求"可重复读"和"防幻读"。这个放松在 row-based binlog 时代是安全的(复制不再依赖"事务内一致性快照"),所以大厂敢切。在 statement-based binlog 时代,这个放松**会破坏复制正确性**——这就是下一节"为什么默认 RR"的根。

---

## 五、RR(可重复读):InnoDB 默认,凭什么

RR 是 InnoDB 的默认。它的三旋钮配置——事务级 read view + 开间隙锁 + 普通 SELECT 走 MVCC——让它既"可重复读"又"防幻读"(后者是 InnoDB 比标准更严格的地方)。

### RR 在源码里:三个旋钮都取"严格"那一档

**read view 时机**:RR 不在 `store_lock`/`external_lock` 里关 view(那个判断是 `<= TRX_ISO_READ_COMMITTED`,RR 不满足),于是 `trx_assign_read_view` 第一次建的 view 整事务复用:

```c
/* trx/trx0trx.cc:2291-2305(源码原文,trx_assign_read_view) */
/** Assigns a read view for a consistent read query. All the consistent reads
 within the same transaction will get the same read view, which is created
 when this function is first called for a new started transaction.
 @return consistent read view */
ReadView *trx_assign_read_view(trx_t *trx) /*!< in/out: active transaction */
{
  ut_ad(trx_can_be_handled_by_current_thread_or_is_hp_victim(trx));
  ut_ad(trx->state.load(std::memory_order_relaxed) == TRX_STATE_ACTIVE);

  if (srv_read_only_mode) {
    ut_ad(trx->read_view == nullptr);
    return (nullptr);

  } else if (!MVCC::is_view_active(trx->read_view)) {   // 只有"当前没有活动的 view"时
    trx_sys->mvcc->view_open(trx->read_view, trx);      // 才新建一个
  }

  return (trx->read_view);                               // 否则复用已有的
}
```

—— 见 [trx_assign_read_view](../mysql-server/storage/innobase/trx/trx0trx.cc#L2291-L2305)。注意函数头注释那句:**"All the consistent reads within the same transaction will get the same read view, which is created when this function is first called for a new started transaction"**——这就是 RR 的行为,事务里第一次调用建,之后复用到事务结束。RC 因为每条语句结束都 `view_close`,所以每次调 `trx_assign_read_view` 都"没活动 view",于是每次都新建——同一个函数,两种级别,差别只在"调用前 view 有没有被关掉"。

**间隙锁**:`skip_gap_locks()` 对 RR 返回 `false`,所以 RR 下 locking read/write 默认用 `LOCK_ORDINARY`(next-key lock = 记录锁 + 前面的间隙)。这一套机制 P5-17 已拆透。

**普通 SELECT 转加锁读**:RR 不转,普通 SELECT 走 MVCC。

### RR 防住的并发异常:全防(包括幻读)

RR 的三旋钮配置,让它把 SQL 标准里 RR **不要求**防的幻读也防住了:

- **防不可重复读**:事务级 read view,两次读同一行用同一个快照,看到同一版本。
- **防幻读**:开间隙锁,范围查询时把"记录 + 前面的间隙"都锁了,别的事务在范围内插入会被间隙锁挡住(插入意向锁冲突)。
- **防脏读**:read view 天然跳过未提交事务的版本(P4-13 讲过)。

```
   RR 下的可重复读 + 防幻读:
   时刻   事务 T (RR)                              事务 U
   t1    BEGIN
   t2    SELECT * FROM t WHERE bal > 100
          → 3 行(id=5,8,12)
          (建 read view RV_T;locking read 给 bal>100 范围
           加 next-key 锁,锁住 id=5 前、5-8 间、8-12 间、12 后的间隙)
   t3                                            BEGIN
   t4                                            INSERT INTO t VALUES(20, 500);
                                                  → 被 12 后的间隙锁挡住,等待!
   t5    SELECT * FROM t WHERE bal > 100
          → 仍 3 行(复用 RV_T,U 还没提交,看不到)
   t6    COMMIT(放掉 next-key 锁)
                                                   ↑ U 被唤醒,插入成功
```

> **钉死这件事**:RR 在 InnoDB 里**比 SQL 标准更严格**(标准里 RR 不保证防幻读,InnoDB 靠间隙锁额外防住了)。这是 InnoDB 的"招牌卖点"——它让默认级别下,事务里的读是一致的、范围查询不会被"幽灵行"干扰。这个卖点是有代价的(间隙锁拖累写并发),所以不是所有人都需要它(大厂切 RC 就是为甩掉这个代价)。

### 为什么 InnoDB 默认 RR,而不是 RC

这是本章最常被问的问题。InnoDB 选 RR 作默认,有三个层面的原因:

**原因一(历史的、也是决定性的):statement-based binlog 复制**

MySQL 早期的 binlog 格式是 **statement-based**(记 SQL 语句本身,从库重放 SQL)。这要求"主库执行一条 SQL 看到的数据状态,和从库重放这条 SQL 看到的状态**完全一致**"。RC 下事务里多条语句的可见性会变(每条 SELECT 看不同快照),可能导致"主库这条 UPDATE 命中了 3 行,从库重放时却命中了 4 行"(因为主从各自在 RC 下看到的快照不同)。RR 下事务用一个 read view,整个事务里语句的可见性稳定,statement-based 复制更安全。**MySQL 早期把默认设 RR,根本原因就是为 statement-based replication 的正确性兜底。**

**原因二:OLTP 业务直觉**

业务开发常把"一个事务"想成一个原子的操作单元——"我这次事务里看到的数据应该是稳定的"。RR 的"整事务一个快照"更符合这个直觉,RC 的"每条语句看当下"反而容易让人困惑(为什么同事务里两次 SELECT 结果不一样?)。RR 更"不让人意外"。

**原因三:InnoDB 的 RR 比标准强**

InnoDB 的 RR 额外防了幻读(间隙锁),严格说比 SQL 标准的 RR 更强、更接近 Serializable(但没 Serializable 那么重的锁开销)。所以"默认 RR"在很多业务场景下既安全又不过分损失并发——是个稳妥的中间档。

> **不这样会怎样**:如果默认 RC,在 statement-based binlog 时代,主从复制会有正确性风险——主库命中 3 行、从库命中 4 行,数据慢慢就漂了。这是 MySQL 早期不可接受的。所以默认 RR 是个"宁可稳一点,也别复制出错"的工程稳妥选择。

### 但是——大厂切 RC 的道理

历史归历史,现代生产环境的现实是:**很多大型互联网公司在内部 MySQL 把默认改成 RC**。理由是:

- **row-based binlog 已经成主流**(MySQL 8.0 起默认 `binlog_format=ROW`)。row-based 记的是"行的变更"(前后镜像),从库重放时**不依赖主库的可见性快照**——RC 下复制也正确。statement-based 时代"必须 RR 才能正确复制"的约束没了。
- **RC 不加间隙锁,写并发高**:间隙锁是 RR 写吞吐的主要拖累——一次范围 UPDATE 可能锁住大段间隙,挡住所有往那段插入的事务。RC 关掉间隙锁,锁的范围小、释放早(不匹配的行扫完就放),写密集场景吞吐显著提升。
- **RC 下死锁更少**:不加间隙锁,锁的范围小,两个事务互相等对方锁的概率(死锁率)下降。死锁要回滚一个事务,是隐形成本。

所以"RR 还是 RC 更好"**没有绝对答案**——它取决于业务的读写比例、并发模式、binlog 格式、对一致性的容忍度。InnoDB 默认 RR 是"历史 + 稳妥"的折中,大厂切 RC 是"row-based binlog + 高写并发"的现代选择。这个选择背后,就是本章讲的"三旋钮"在 read view 时机和间隙锁两处的差异。

### 一个具体场景:RR vs RC 的锁面差多少

讲这么多理论,不如看一个具体的数字对比。假设一张 100 万行的订单表 `orders`(无索引列 `status`),两个事务并发执行 `UPDATE orders SET flag=1 WHERE status='PENDING'`(假设 `status='PENDING'` 的行有 1000 行散布在全表):

```
   RR 下(默认):
   ──────────────────────────────────────────────────────────
   事务 A 扫表,给扫到的每一行(100 万行)都加 X 锁 + next-key 锁
   即便 999,000 行根本 status≠'PENDING',锁也全保留到 commit
   → 持锁面 ≈ 100 万行 + 它们之间所有间隙(几乎锁全表)
   
   事务 B 此时也想跑同样的 UPDATE:
   → 第一步就要给 orders 第 1 行加 X 锁,被 A 挡住,直接等
   → A 跑完(假设 30 秒)+ commit,B 才能开始
   → 串行化,两个事务总耗时 ≈ 60 秒

   RC 下:
   ──────────────────────────────────────────────────────────
   事务 A 扫表,遇到 status='PENDING' 的 1000 行 → 加 X 锁,保留到 commit
   遇到 status≠'PENDING' 的 999,000 行 → 加 X 锁、评估 WHERE、立刻放掉
   → 持锁面 ≈ 1000 行(只有真正要改的)
   
   事务 B 同时跑:
   → 扫到的行,大部分是 A 已经放掉锁的不匹配行,B 直接锁、评估、放掉
   → 只有 A 正在改的那 1000 行,B 会撞上(但散布全表,撞概率低)
   → 两个事务大部分时间并行,总耗时 ≈ 35 秒(接近一个事务的时间)
```

—— 这就是大厂切 RC 的真实收益:**写密集 + 范围更新场景,RC 的吞吐能比 RR 高近一倍**。代价是:RC 下事务 B 看到的可能是"A 改到一半的中间态"(因为 A 还没 commit,B 的每条语句看到的是当下已提交),业务必须能容忍这个。对很多互联网业务(展示类、统计类)来说,这个容忍是值得的;对金融核心(账户余额)来说,绝不能容忍,得用 RR 甚至更高。

### 一段历史:为什么 MySQL 5.0 前甚至不支持 RC 复制

补一段历史帮助理解"InnoDB 默认 RR"的根。MySQL 5.0 之前,**binlog 默认且唯一格式是 statement-based**,而且**RC 隔离级别 + statement-based binlog 的组合会导致复制损坏**——主库在 RC 下,事务里多条语句看到不同快照,某条 `UPDATE ... WHERE` 在主库命中 N 行,从库重放时由于可见性不同可能命中 M 行,主从数据漂移。当时 MySQL 甚至强制**"如果用 statement-based binlog,会话隔离级别不能是 RC"**(会被静默提到 RR)。

MySQL 5.7.7 起 `binlog_format` 默认改成 `ROW`,这个限制才解除——row-based 记的是"行的前后镜像",从库直接套用,不依赖主库可见性快照,RC 终于可以安全复制。MySQL 8.0 起默认 `binlog_format=ROW`,RC 在生产里彻底"解禁"。**这就是"为什么 InnoDB 默认 RR 在历史上有道理、但在 2020 年代后切 RC 也越来越有道理"的技术背景**。今天你新建一个 MySQL 8.x 实例,默认 `transaction_isolation=REPEATABLE_READ` + `binlog_format=ROW`——这个组合下,你其实可以安全地把隔离级别降到 RC 拿写并发,Oracle 只是没有把默认改过来(为了向后兼容)。

> **钉死这件事**:"InnoDB 默认 RR"和"大厂切 RC"都**有道理**,只是不同的工程约束下的取舍。这不是数据库设计的"bug",而是"没有银弹"的真实写照——任何默认值都要在"严格"和"并发"之间挑一个点,InnoDB 挑了 RR(更稳),大厂按需挑了 RC(更快)。能讲清这个取舍,你才算真正理解了 InnoDB 的隔离级别设计。

---

## 六、Serializable(串行化):把普通 SELECT 也串行化

Serializable 是 SQL 标准里最严格的级别——"所有事务像串行执行一样,完全没有并发干扰"。在 InnoDB 里,它的实现出人意料地轻量:**它不是一套全新的串行化机制,而是"RR 的三旋钮配置 + 把普通 SELECT 也转成加 S 锁的当前读"**。

### Serializable 的三旋钮配置

- **read view 时机**:事务级(和 RR 一样)。但这个旋钮在 Serializable 下其实"用不上"——因为普通 SELECT 不走 MVCC 了(转加锁读),根本不需要 read view。
- **间隙锁**:开(`skip_gap_locks` 返回 `false`,和 RR 一样)。
- **普通 SELECT 转加锁读**:**这是 Serializable 唯一区别于 RR 的地方**。

### 源码:把普通 SELECT 强行加 S 锁

Serializable 把普通 `SELECT`(本该走 MVCC,`select_lock_type = LOCK_NONE`)改成加 S 锁的当前读。源码在 `ha_innodb.cc` 的 `external_lock` 里:

```c
/* handler/ha_innodb.cc:19022-19083(源码原文节选,external_lock 里的大表) */
/*
For reads we will use LOCK_NONE, LOCK_S or LOCK_X according to this chart:
                     +-----------------------------------------+
                     | is_dd_table or skip_locking             |
                     +----------------------------------+------+
                     | false                            | true |
                     +----------------------------------|      |
                     | TRANSACTION ISOLATION LEVEL      |      |
                     +----------------+-----------------+      |
                     | < SERIALIZABLE | = SERIALIZABLE  |      |
+--------------------+----------------+-----------------+------+
| non-locking SELECT | NONE [1]       | S [3]           | NONE |
| SELECT FOR SHARE   | S [2]          | S               | NONE |
| SELECT FOR UPDATE  | X              | X               | X    |
+--------------------+----------------+-----------------+------+
...
[3] An exception is consistent reads in the AUTOCOMMIT=1 mode:
    we know that they are read-only transactions, and they can be serialized
    also if performed as consistent reads. Thus we use LOCK_NONE for them.
*/
if (lock_type == F_RDLCK) {
  ...
  if (m_prebuilt->table->is_dd_table || m_prebuilt->no_read_locking) {
    m_prebuilt->select_lock_type = LOCK_NONE;
    m_stored_select_lock_type = LOCK_NONE;
  } else if (trx->isolation_level == TRX_ISO_SERIALIZABLE &&
             m_prebuilt->select_lock_type == LOCK_NONE &&
             thd_test_options(thd, OPTION_NOT_AUTOCOMMIT | OPTION_BEGIN)) {
    m_prebuilt->select_lock_type = LOCK_S;          // ← 这里!把 LOCK_NONE 改成 LOCK_S
    m_stored_select_lock_type = LOCK_S;
  } else {
    // Retain value set earlier for example via store_lock()
    ut_ad(m_prebuilt->select_lock_type == LOCK_S ||
          m_prebuilt->select_lock_type == LOCK_NONE);
  }
}
```

—— 见 [ha_innodb.cc external_lock 的 Serializable 转 S 锁](../mysql-server/storage/innobase/handler/ha_innodb.cc#L19022-L19083)。**关键就这一行**:`if (trx->isolation_level == TRX_ISO_SERIALIZABLE && m_prebuilt->select_lock_type == LOCK_NONE && 非autocommit)` → `select_lock_type = LOCK_S`。注释表里也写得清清楚楚:non-locking SELECT 在 `= SERIALIZABLE` 列里是 **S**。

注意这个 `if` 的**第三个条件**:`thd_test_options(thd, OPTION_NOT_AUTOCOMMIT | OPTION_BEGIN)`——意思是"当前在显式事务里(不是 autocommit 单条语句)"。注释 [3] 解释了:**autocommit=1 下的单条 SELECT,Serializable 也用 LOCK_NONE**(走 MVCC)。为什么?因为单条 autocommit SELECT 是只读事务,"它自己就是个原子点,天然可串行化",没必要加 S 锁拖累并发。所以"Serializable 把 SELECT 转 S 锁"只在**显式事务里**生效。

### 一处类似的:FLUSH TABLES WITH READ LOCK

`ha_innodb.cc` 里还有一处几乎一样的判断:

```c
/* handler/ha_innodb.cc:19788-19794(源码原文,store_lock 里 FLUSH 分支) */
if (trx->isolation_level == TRX_ISO_SERIALIZABLE) {
  m_prebuilt->select_lock_type = LOCK_S;
  m_stored_select_lock_type = LOCK_S;
} else {
  m_prebuilt->select_lock_type = LOCK_NONE;
  m_stored_select_lock_type = LOCK_NONE;
}
```

—— 见 [ha_innodb.cc 19788 FLUSH 分支](../mysql-server/storage/innobase/handler/ha_innodb.cc#L19788-L19794)。同样是"Serializable 把 LOCK_NONE 改成 LOCK_S",逻辑和 `external_lock` 那处一致。

### Serializable 的代价:并发废掉

把普通 SELECT 也变成加 S 锁的当前读,后果是显式的:

- **读读共享(S+S 兼容),读写互斥(S 和 X 冲突)**——一个 SELECT 持着 S 锁,任何 UPDATE/DELETE 都得等。这破坏了 MVCC"读不阻塞写"的红利。
- **所有读变成两阶段锁持有到 commit**——S 锁也要 commit 才放,持有时间长。

```
   Serializable 下的读写互斥:
   时刻   事务 T (Serializable)               事务 U
   t1    BEGIN
   t2    SELECT * FROM t WHERE id=10
          → 普通SELECT被改成 SELECT ... LOCK IN SHARE MODE
            给 id=10 加 S 锁,持到 commit
   t3                                        BEGIN
   t4                                        UPDATE t SET bal=bal-100 WHERE id=10
                                              → 想加 X 锁,被 T 的 S 锁挡住,等待!
   t5    ... T 还没 commit,U 一直等 ...
   t6    COMMIT(放 S 锁)
                                                 ↑ U 被唤醒,加 X 锁,改
```

> **不这样会怎样**:Serializable 的"严格"是用真金白银的并发换来的——每个 SELECT 都加 S 锁,读写全互斥,OLTP 高并发读写场景吞吐直接塌。所以**生产里极少用 Serializable**,它只在"数据绝对不能有一丝并发异常"(比如某些金融核心对账)的极少数场景下才用。InnoDB 提供 Serializable 更多是"为 SQL 标准凑齐四级",不是鼓励日常用。

---

## 七、四级别的并发异常对照:一张时序图

把四个级别对三个并发异常的防住与否,用四个事务时序串起来看(同一时间轴,不同级别看到不同结果):

```mermaid
sequenceDiagram
    autonumber
    participant A as 事务 A
    participant B as 事务 B(写)
    participant VL as 版本链(id=10) / 范围索引(bal>100)

    Note over A,B: ─── 场景一:脏读(B 改了但没提交,A 能不能读到? ) ───
    Note over A: A 在 RU / 其他级别
    A->>A: BEGIN
    A->>VL: SELECT bal FROM t WHERE id=10 (RU 下采样等边缘路径无 read view)
    Note over B: BEGIN
    B->>VL: UPDATE t SET bal=200 WHERE id=10 (X 锁,未提交)
    A->>VL: SELECT bal FROM t WHERE id=10
    Note over A: RU → 可能读到 200(脏读);RC/RR/Serializable → 走 MVCC,看不到未提交

    Note over A,B: ─── 场景二:不可重复读(B 改了并提交了,A 两次读值变不变? ───
    Note over A: A 在 RR / RC
    A->>A: BEGIN
    A->>VL: SELECT bal WHERE id=10 → 100 (RR 建 RV_A;RC 建 RV_A1)
    B->>VL: UPDATE SET bal=200 WHERE id=10; COMMIT
    A->>VL: SELECT bal WHERE id=10
    Note over A: RR 复用 RV_A → 100(可重复读);RC 新建 RV_A2 → 200(不可重复读)

    Note over A,B: ─── 场景三:幻读(B 插了新行,A 范围查行数变不变? ───
    Note over A: A 在 RR / RC
    A->>A: BEGIN
    A->>VL: SELECT * WHERE bal>100 → 3 行(RR 给范围加 next-key 锁;RC 只记录锁)
    B->>VL: INSERT VALUES(20,500)
    Note over B: RR → 被间隙锁挡,等待;RC → 顺利插入并 commit
    A->>VL: SELECT * WHERE bal>100
    Note over A: RR → 仍 3 行(防幻读);RC → 4 行(幻读)
```

### 四级别异常防住对照(承接 P4-13 的三种异常)

| 异常 \ 级别 | RU | RC | RR | Serializable |
|-------------|----|----|----|--------------|
| **脏读**(读未提交) | ❌可能发生 | ✅防 | ✅防 | ✅防 |
| **不可重复读**(同行两次值变) | ❌ | ❌发生 | ✅防 | ✅防 |
| **幻读**(范围两次行数变) | ❌ | ❌发生 | ✅防(InnoDB 特有) | ✅防 |
| **加锁读代价** | 无间隙锁 | 无间隙锁、不匹配行扫完放锁 | 间隙锁、持有到 commit | 普通 SELECT 也加 S 锁持到 commit |

读这张表的关键:**从左到右,防住的异常越来越多,锁的代价也越来越大**。这就是隔离级别"严格 vs 并发"的权衡——你想要更严的一致性,就得付更多的锁开销。

---

## 八、技巧精解一:三旋钮组合矩阵——四个级别凭什么不是四套代码

(正文后、小结前的固定位置)

本章最硬核的洞察,是"四个隔离级别 = 三个旋钮的组合"。这一节把这个洞察单独钉死,并用源码证明它不是事后归纳,而是 InnoDB 真实结构。

### 朴素认知的墙:"四个级别 = 四套机制"

很多刚接触数据库的人,下意识以为四个隔离级别是"四套完全不同的并发控制机制"——RU 一套、RC 一套、RR 一套、Serializable 又一套。这种认知会导致两个问题:① 觉得 InnoDB 源码里"一定有个巨大的 switch(isolation_level)",但实际上没有;② 觉得"切级别"是个大动作,实际上是改几个开关。

### InnoDB 的解法:三个正交旋钮,组合出四级

InnoDB 的真实做法是:**把"并发控制的几个独立维度"拆成正交的旋钮,每个旋钮独立开关,组合得到不同级别**。这三个旋钮是:

1. **read view 时机**:每条语句 vs 事务级——管"快照读看哪个版本"(防不防不可重复读)。
2. **间隙锁开关**:开 vs 关——管"locking read/write 防不防别人插入"(防不防幻读)。
3. **普通 SELECT 转加锁读**:不转 vs 转——管"读要不要也串行化"。

每个旋钮**独立**——你可以任意组合,但 SQL 标准只定义了四种有意义的组合(RU/RC/RR/Serializable),InnoDB 就支持这四种。源码里:

- 旋钮一由 `trx_assign_read_view` + `isolation_level <= READ_COMMITTED` 时 `view_close` 这套机制控制;
- 旋钮二由 `trx_t::skip_gap_locks()` 这一个函数控制(被 `row0sel.cc` 十几处消费);
- 旋钮三由 `external_lock` 里 `if (isolation_level == SERIALIZABLE && select_lock_type == LOCK_NONE && 非autocommit)` 这一处判断控制。

把这三个旋钮组合成矩阵,就是:

```
   三旋钮组合矩阵(每个组合对应一个级别):
   ┌───────────────────────────────────────────────────────────────┐
   │ read view 时机 │ 间隙锁 │ SELECT 转加锁读 │ = 哪个级别          │
   ├───────────────────────────────────────────────────────────────┤
   │ 每条语句(边缘)│ 关      │ 不转             │ READ UNCOMMITTED    │
   │ 每条语句       │ 关      │ 不转             │ READ COMMITTED      │
   │ 事务级         │ 开      │ 不转             │ REPEATABLE READ     │
   │ 事务级         │ 开      │ 转(→S锁当前读) │ SERIALIZABLE        │
   └───────────────────────────────────────────────────────────────┘

   读法:
   - RC → RR:只动"read view 时机"(每条语句 → 事务级)和"间隙锁"(关 → 开)两个旋钮
   - RR → Serializable:只动"SELECT 转加锁读"一个旋钮(不转 → 转)
   - RC → RU:几乎不动(RU 在主流路径上就是 RC,只是边缘路径更宽松)
```

这个矩阵的精妙之处:**它不是"四套机制选一套",而是"几个正交旋钮各取值"**。这意味着:① 代码复用度极高(同样的 `trx_assign_read_view` 服务所有级别,差别只在调用前后有没有 `view_close`);② 切级别开销极小(就是改几个判断的走向,没有"重新初始化一套并发控制");③ 演进灵活(将来要加新级别,比如"snapshot isolation",只要组合现有旋钮的新取值,不用大改)。

### 反面对比:如果是"四套独立机制"

> **不这样会怎样**:如果 InnoDB 真的实现"四套并发控制机制"(RU 一套、RC 一套、RR 一套、Serializable 一套),会有三个灾难:① 代码量翻四倍,每套都要自己处理 read view、锁、版本链——维护成本爆炸;② 切级别要"切换整套机制",运行时开销大、容易出 bug;③ SQL 标准将来加新级别(比如 SQL:1999 之后的 SI、RC2),要重写一套。"正交旋钮组合"是数据库领域并发控制设计的成熟范式——PostgreSQL、Oracle、SQL Server 的隔离级别实现也都是类似思路(虽然具体旋钮不同)。这是 InnoDB 工程功底的体现:**用最少的机制,覆盖最多的语义**。

> **钉死这件事**:**四个隔离级别不是四套代码,而是三个旋钮的组合**。这是本章的灵魂洞察,也是"InnoDB 工程优雅"的范本。理解了它,你看 InnoDB 源码不会再找"那个 switch(isolation_level)",而是知道三个旋钮各自在哪:`skip_gap_locks()`(旋钮二,最显眼)、`store_lock`/`external_lock` 里的 `view_close`(旋钮一)、`external_lock` 里 SERIALIZABLE 转 S 锁(旋钮三)。

---

## 九、技巧精解二:RC 的"逐语句放锁"——为什么 RC 死锁少

(第二个技巧,讲 RC 那三个动作里最容易被忽略、但对生产影响最大的"不匹配行扫完就放锁")

**这个技巧解决什么问题**:RC 在生产里有个明显现象——**死锁率比 RR 低很多**,锁等待也少。很多人以为这是因为"RC 不加间隙锁",这只对了一半。RC 还有一个同等重要的优化:**扫描时遇到 WHERE 不匹配的行,加的记录锁扫完就立刻放掉**,而不是持有到 commit。这个动作让 RC 的"持锁面"比 RR 小一个数量级。

**朴素方案(RR 的做法)为什么不灵**:RR 下,一次 `UPDATE t SET ... WHERE b=2`(假设 `b` 无索引)会全表扫,给扫到的**每一行**都加 X 锁 + next-key 锁,哪怕这行根本 `b≠2`——所有这些锁都持有到 commit。一张 100 万行的表,一次只改 10 行的 UPDATE,RR 下可能锁住几千个记录 + 间隙(因为扫到的不匹配行也得锁,防"幻读")。两个这样的 UPDATE 并发,互相等对方的锁——死锁概率高。

**RC 的巧妙手段**:`releases_non_matching_rows()` 对 RC/RU 返回 `true`(`include/trx0trx.h:1131`),扫描时遇到不匹配的行,加 X 锁后立刻标记"待放",下一轮循环处理掉:

```c
/* row/row0sel.cc:5259-5265(源码原文) */
case DB_SUCCESS_LOCKED_REC:
  if (trx->releases_non_matching_rows()) {
    /* Note that a record of
    prebuilt->index was locked. */
    ut_ad(!prebuilt->new_rec_lock[row_prebuilt_t::LOCK_PCUR]);
    prebuilt->new_rec_lock[row_prebuilt_t::LOCK_PCUR] = true;  // 标记"这条要放"
  }
  err = DB_SUCCESS;
```

—— 见 [row0sel.cc 5259](../mysql-server/storage/innobase/row/row0sel.cc#L5259-L5265)。MySQL 官方文档用一段经典例子讲清这个差异(无索引表 `UPDATE t SET b=5 WHERE b=3`):

```
   RR 下扫到的每一行都保留 X 锁到 commit:
   x-lock(1,2); retain x-lock          ← b=2 不匹配,但锁保留(防幻读)
   x-lock(2,3); update(2,3) to (2,5); retain x-lock  ← b=3 匹配,改
   x-lock(3,2); retain x-lock          ← b=2 不匹配,但锁保留
   x-lock(4,3); update(4,3) to (4,5); retain x-lock  ← b=3 匹配,改
   x-lock(5,2); retain x-lock          ← b=2 不匹配,但锁保留

   RC 下扫到的不匹配行扫完就放:
   x-lock(1,2); unlock(1,2)            ← b=2 不匹配,立刻放!
   x-lock(2,3); update(2,3) to (2,5); retain x-lock  ← b=3 匹配,改,保留
   x-lock(3,2); unlock(3,2)            ← b=2 不匹配,立刻放!
   x-lock(4,3); update(4,3) to (4,5); retain x-lock  ← b=3 匹配,改,保留
   x-lock(5,2); unlock(5,2)            ← b=2 不匹配,立刻放!
```

—— RR 锁了 5 行(全保留),RC 只锁了 2 行(只改的保留)。**持锁面差 2.5 倍**,这就是 RC 死锁少、写并发高的根之一。

**为什么 RR 必须保留不匹配行的锁**:RR 要防幻读。如果 RR 下"扫到的不匹配行也放锁",那放掉的间隙就会被别的事务插进来——幻读就来了。所以 RR 是"宁可多锁,也要防幻读",RC 是"反正不防幻读,能放就放"——两个级别各自的语义,直接决定了这个优化的开关。这是"旋钮二(间隙锁)"和"releases_non_matching_rows"在源码里**用同一个函数 `skip_gap_locks()` 控制**的原因(见 `trx_t::releases_non_matching_rows` 就是 `return skip_gap_locks();`)——它们本质是同一个权衡的两面。

**反面对比**:

> **不这样会怎样**:如果 RC 也保留不匹配行的锁到 commit(像 RR),那 RC 的写并发优势就只剩"不加间隙锁"这一半,死锁率还是不会显著下降。RC 这个"逐语句放锁"优化,和"不加间隙锁"是**配套**的——两个一起,才让 RC 在写密集场景下吞吐显著高于 RR。这也是大厂切 RC 的真实收益构成:"不加间隙锁"省了一半锁,"逐语句放锁"又省了另一半。两个优化叠加,RC 的锁面比 RR 小近一个数量级。

> **钉死这件事**:RC 的"高并发"不是凭空来的,它由两个配套优化支撑:① 不加间隙锁(`skip_gap_locks=true`,旋钮二);② 不匹配行扫完放锁(`releases_non_matching_rows=true`)。两个优化在源码里**用同一个函数 `skip_gap_locks()` 控制**——因为它们本质是"放松到读已提交"这一个权衡的两面。这个设计的经济性(一个函数控制两个相关优化)是 InnoDB 工程优雅的体现。

---

## 十、章末小结

### 回扣主线

本章是第 5 篇(锁)的收尾,也是整个"事务与并发"二分法那面的总收口。它把前面讲的所有零件——P4 篇的 MVCC(read view、版本链、可见性)、P5-16 的行锁(记录锁 S/X、两阶段锁、意向锁)、P5-17 的间隙锁(gap/next-key/insert intention)、P5-18 的死锁检测——组合成用户能选的**四个隔离级别**。

主线回扣:**一条写,InnoDB 用四个保证让它"既不丢又高并发"——B+树聚簇索引找到位置、redo(WAL)保 crash 不丢、undo(MVCC)保并发读、锁保隔离。** 本章讲的是第四个保证(锁保隔离)和第三个保证(MVCC 保并发读)合起来,怎么用"三个旋钮"组合出 SQL 标准的四个隔离级别。读到这里,"事务与并发"这一面已经收口:WAL/redo(P3-08~12)+ MVCC(P4-13~15)+ 锁(P5-16~19)四件套,就是 InnoDB 让"写不丢、并发不乱"的全部家底。

### 四级别总表(回顾)

| 级别 | **read view 时机** | **间隙锁** | **普通 SELECT** | 防脏读 | 防不可重复读 | 防幻读 |
|------|-------------------|-----------|----------------|--------|-------------|--------|
| RU | 每条语句(边缘宽松) | 关 | 走 MVCC | ❌ | ❌ | ❌ |
| RC | 每条语句新建 | 关 | 走 MVCC | ✅ | ❌ | ❌ |
| **RR(默认)** | 事务级 | 开(next-key) | 走 MVCC | ✅ | ✅ | ✅ |
| Serializable | 事务级 | 开 | **转 S 锁当前读** | ✅ | ✅ | ✅ |

一句话:从 RU 到 Serializable,防住的异常越来越多,锁的代价也越来越大。InnoDB 默认 RR 是"稳",大厂切 RC 是"快",Serializable 极少用。

### 五个为什么

1. **为什么四个隔离级别不是四套代码?**——它们是"read view 时机 × 间隙锁 × 普通 SELECT 转加锁读"三个正交旋钮的组合。InnoDB 把并发控制拆成正交旋钮,组合得到四级——代码复用度高、切级别开销小、演进灵活。
2. **为什么 InnoDB 默认 RR 而不是 RC?**——历史地,statement-based binlog 复制要求"主从执行同样 SQL 得同样结果",RR 的事务级 read view 让复制更安全;InnoDB 的 RR 还额外防幻读(比标准强),是个稳妥的中间档。
3. **为什么大厂切 RC?**——row-based binlog 时代,RC 也能正确复制(不再依赖事务内一致性快照);RC 不加间隙锁、不匹配行扫完就放锁,写并发高、死锁少。是"row-based binlog + 高写并发"的现代选择。
4. **为什么 Serializable 把普通 SELECT 也转成加锁读?**——为了"所有读都串行化"。源码就是 `external_lock` 里一处 `if (isolation_level == SERIALIZABLE && select_lock_type == LOCK_NONE && 非autocommit) → select_lock_type = LOCK_S`,把本该走 MVCC 的普通 SELECT 强行改成加 S 锁的当前读。代价是读写互斥,所以生产极少用。
5. **为什么 RC 死锁比 RR 少?**——两个配套优化:① 不加间隙锁(锁的范围小);② 不匹配行扫完就放锁(`releases_non_matching_rows`,持锁面小)。两个优化在源码里用同一个 `skip_gap_locks()` 控制,本质是"放松到读已提交"这一个权衡的两面。

### 想继续深入往哪钻

- **看各级别运行时行为**:`SET TRANSACTION ISOLATION LEVEL ...` 后,用 `SHOW ENGINE INNODB STATUS` 看持锁/等锁差异;`performance_schema.data_locks`、`data_lock_waits` 能看到具体的锁对象(尤其 RC vs RR 下,范围 UPDATE 持锁面差多少)。
- **看源码**:三个旋钮的核心点:
  - 旋钮一(read view 时机):[`trx_assign_read_view`](../mysql-server/storage/innobase/trx/trx0trx.cc#L2291-L2305)(不分级别,复用 view) + [`ha_innodb.cc:19749`](../mysql-server/storage/innobase/handler/ha_innodb.cc#L19749-L19759)(RC 在 store_lock 里 view_close) + [`ha_innodb.cc:19156`](../mysql-server/storage/innobase/handler/ha_innodb.cc)(RC 在 external_lock 里 view_close)。
  - 旋钮二(间隙锁):[`trx_t::skip_gap_locks`](../mysql-server/storage/innobase/include/trx0trx.h#L1113-L1124)(RU/RC 返回 true,RR/Serializable 返回 false) + [`row0sel.cc:864`](../mysql-server/storage/innobase/row/row0sel.cc#L864)(锁类型二选一) + [`row0sel.cc:4785`](../mysql-server/storage/innobase/row/row0sel.cc#L4785-L4795)(关 `set_also_gap_locks`)。
  - 旋钮三(SELECT 转加锁读):[`ha_innodb.cc:19022-19083`](../mysql-server/storage/innobase/handler/ha_innodb.cc#L19022-L19083)(external_lock 里的大表,Serializable 把 LOCK_NONE 改 LOCK_S) + [`ha_innodb.cc:19788`](../mysql-server/storage/innobase/handler/ha_innodb.cc#L19788-L19794)(store_lock 里 FLUSH 分支)。
  - RC 逐语句放锁:[`trx_t::releases_non_matching_rows`](../mysql-server/storage/innobase/include/trx0trx.h#L1127-L1131) + [`row0sel.cc:5259`](../mysql-server/storage/innobase/row/row0sel.cc#L5259-L5265)。
  - 采样读 RU 跳 view:[`ha_innodb.cc:10968`](../mysql-server/storage/innobase/handler/ha_innodb.cc#L10968-L10970)。
- **看官方文档**:MySQL 9.x 官方手册 "InnoDB Transaction Isolation Levels"([dev.mysql.com](https://dev.mysql.com/doc/refman/en/innodb-transaction-isolation-levels.html))讲清了四级别的官方定义和 RC 的逐语句放锁例子;"Consistent Nonlocking Reads"、"Phantom Rows"、"Next-Key Locks" 三节是 MVCC + 间隙锁的官方表述。
- **看经典论文**:隔离级别的奠基是 Berenson 等人 1995 的 "A Critique of ANSI SQL Isolation Levels"(揭示了 SQL 标准隔离级别定义的歧义,提出了更精确的"异常"分类);MySQL 官方文档里 RR 防幻读的说法,实际上比 SQL 标准更强——这篇 critique 解释了为什么。
- **承接《TiKV》**:TiKV 默认是 SI(Snapshot Isolation,靠 TSO + Percolator),它不等价于 InnoDB 的任何一级——SI 介于 RC 和 Serializable 之间(防不可重复读和幻读,但允许 write skew)。把 InnoDB 的 RR 和 TiKV 的 SI 对照,你会发现"可重复读"在不同数据库里含义不同——这正是 Berenson 论文指出的 SQL 标准歧义。

### 引出下一篇

第 5 篇(锁)到这里收尾。从 P5-16(行锁全貌)到 P5-17(间隙锁/临键锁)、P5-18(死锁检测)、再到本章 P5-19(隔离级别),InnoDB 的"并发控制"全套已经讲透:MVCC(P4)管读-写并发、锁(P5)管写-写并发和隔离级别。这两套合起来,就是 InnoDB 在"高并发"这一仗上的全部家底。

但到此为止,我们讲的都还是 InnoDB 内部的事。还有最后一程没走:**一条 SQL 从客户端发出来,server 层怎么解析优化、怎么通过 handler 接口调进 InnoDB、InnoDB 怎么把它翻译成 buffer pool 找页 + 加锁 + 改数据 + 写 redo 的具体操作**。这是"一条写的旅程"的完整闭环,也是承接《PG》那本 SQL 旅程的地方。下一章 P6-20,我们把这条旅程从 server 层走到 InnoDB,串起全书前 19 章所有的零件。

> **下一章**:[P6-20 · 一条 SQL 的完整旅程](P6-20-一条SQL的完整旅程.md)
