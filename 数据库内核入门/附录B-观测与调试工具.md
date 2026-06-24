# 附录 B · 观测与调试工具

> 正文二十多章,讲的都是"数据库**为什么**这么设计、源码**怎么**落地"。但你真要上手调一个慢查询、查一个死锁、确认一个"是不是该 VACUUM 了",光懂原理不够——你得**看得见**数据库内部正在发生什么。这个附录就给你配齐这套"眼睛"。
>
> 全书的主线是"让数据**快** vs 让数据**不丢不乱**"。本附录的八个工具,也按这条线分两组:
>
> - **看"快"那一侧**:执行计划对不对(EXPLAIN)、buffer 命中率(pg_buffercache)、索引有没有被用上(pg_stat_user_indexes)、慢查询到底慢在哪(auto_explain)。
> - **看"不丢不乱"那一侧**:谁在等谁(pg_stat_activity、pg_locks)、页和 tuple 字节级长啥样(pageinspect)、日志里到底记了什么(pg_waldump)、死元组攒了多少(pg_stat_user_tables)。
>
> 每个工具一节,讲三件事:**① 没有它会怎样(盲调的痛苦)② 怎么用(真实可运行的 SQL 或 shell)③ 它能看到本书讲的哪个机制**。

---

## 一、EXPLAIN / EXPLAIN ANALYZE / EXPLAIN (ANALYZE, BUFFERS)

### 1.1 不这样会怎样:盲调优化器

第 4 章讲过,优化器是基于代价(CBO)猜出来的,**猜得准不准全看统计信息新不新**。问题是:同一个查询慢了,你**只看 SQL 文本根本猜不出**它走了哪条路——是走了索引还是全表扫?优化器估的 1000 行,实际跑出来是 10 行还是 100 万行?是耗在扫描上,还是耗在排序上?

> **不这样会怎样**:你以为"`WHERE id = 42` 有索引,肯定快"。可你不知道优化器估错了基数、以为符合条件的行很少,结果走了索引扫描、然后**一行一行随机回表**一百万次,比直接全表扫还慢十倍。你反复改 SQL 写法、加 hint(其实 PG 没 hint),折腾半天,病根不在写法,在执行计划——而你看不见计划,就只能在黑屋里摸象。

EXPLAIN 就是那扇窗。

### 1.2 三档用法,信息量逐级递增

```sql
-- 档 1:只看估算计划,不真跑(不改数据、不锁表,任何环境都能跑)
EXPLAIN SELECT * FROM users WHERE id = 42;

-- 档 2:真跑一遍,拿到 actual rows / actual time
EXPLAIN ANALYZE SELECT * FROM users WHERE status = 'active';

-- 档 3:再加 BUFFERS,看读了多少个块、命中多少——这是性能命脉
EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM users WHERE status = 'active';
```

三档的区别是信息量,不是"高级与否":

| 档 | 跑不跑 | 看到什么 | 代价 |
|---|---|---|---|
| `EXPLAIN` | 不跑 | 优化器**估算**的 cost、rows、计划形状 | 零,任何库都能跑 |
| `EXPLAIN ANALYZE` | 真跑 | 加上 **actual** rows、actual time、循环次数 | 真执行(INSERT/UPDATE/DELETE 会真改数据,慎在生产跑) |
| `+ BUFFERS` | 真跑 | 再加每步读了多少块(hit/shared/dirtied/written) | 同上 |

### 1.3 看什么:回扣第 4、5 章

读输出的关键是**对比"估算"和"实际"**:

```
Index Scan using users_pkey on users  (cost=0.29..8.31 rows=1 width=68) (actual time=0.015..0.016 rows=1 loops=1)
  Index Cond: (id = 42)
  Buffers: shared hit=3
```

逐字段对回正文:

- **`Seq Scan` vs `Index Scan`**(算子名):回扣第 10 章——O(n) 全表扫 vs O(log n) 索引扫描。如果你看到本该走索引的查询出了 `Seq Scan`,要么没索引,要么优化器认为符合条件的行太多、走索引反而亏(回扣第 4 章"95% 行命中时规则会翻车"的例子)。
- **`cost=0.29..8.31`**(启动代价..总代价):回扣第 4 章代价模型——`seq_page_cost`、`random_page_cost`、`cpu_tuple_cost` 三个参数乘出来的无量纲数。第一个数是"吐出第一行要多久",对 `LIMIT` 很关键。
- **`rows=1`(估算) vs `actual ... rows=1`**:这两个数**差很多**,就是统计信息过期——第 4 章反复强调"优化器不是算真值,是在猜"。估算 10 行、实际 100 万行,基本就是 `ANALYZE` 没跑、统计信息陈旧,赶紧 `ANALYZE users;`。
- **`loops=1`**:这步算子被执行了几次。嵌套循环内层 `loops` 可能是几十万——`actual time` 是**每次**的,要乘以 loops 才是总耗时。
- **`Buffers: shared hit=3`**:回扣第 8 章 BufferPool——`hit` = 命中缓冲池没读磁盘,`read` = 没命中、真去读了磁盘。`hit` 越多越好;要是某步 `shared read=100000`,说明它在疯狂读盘,buffer pool 没帮上忙。

> **一句话**:EXPLAIN 让优化器的"猜测"变成可检验的命题;`ANALYZE` 让你看到"猜得准不准";`BUFFERS` 让你看到"快不快的物理真相"在缓冲池命中率里。三者合起来,就是诊断慢查询的标准三连。

---

## 二、pg_stat_activity:看现在谁连着、在跑什么、在等什么

### 2.1 不这样会怎样:数据库卡住了,你不知道是谁干的

生产库突然慢了。客户端报超时。你 SSH 上去看,数据库进程都在、CPU 也没满——可就是卡。这时候你必须立刻回答三个问题:**现在有多少连接?每个连接正在跑哪条 SQL?它们是在干活、还是在等?**

> **不这样会怎样**:你不知道是哪个连接把库拖慢的,只能 `kill` 进程瞎试,或者重启大法。更糟的是,一个长跑的 `SELECT count(*)` 把表锁住(第 16 章的 `AccessShareLock`),后面所有写都在排队,你却以为"是不是磁盘满了"——方向全错。

### 2.2 标准查询

```sql
-- 看所有非空闲连接在跑什么、等什么
SELECT pid, usename, application_name, state,
       wait_event_type, wait_event,
       now() - query_start AS query_duration,
       left(query, 80) AS query
FROM pg_stat_activity
WHERE state <> 'idle'
ORDER BY query_start;
```

关键列对回正文:

- **`state`**:`active`(真在跑 SQL)、`idle`(空闲)、`idle in transaction`(事务开着但没在跑——这是**长事务**,会阻塞 VACUUM,回扣第 21 章)、`idle in transaction (aborted)`。
- **`wait_event_type` + `wait_event`**:PG 把等待分了类——`Lock`(在等第 16 章的那种表锁/行锁)、`LWLock`(轻量锁,内部数据结构)、`IO`(在等磁盘,回扣第 8 章 buffer 没命中)、`Client`(在等客户端读结果)、`BufferPin`(等别人改完某个 buffer)。
  - 看到 `wait_event_type = 'Lock'`,基本就是并发冲突,跳到第三节 pg_locks。
  - 看到 `wait_event = 'DataFileRead'` 之类 IO 等待,说明 buffer pool 没罩住、在真读盘,回扣第 8 章。
- **`query_duration`**:长事务和长查询是两个最常见的"数据库变慢"元凶。

> **小白补一句**:`pg_stat_activity` 一行 = 一个 backend 进程 = 一个连接(回扣第 2 章"每连接一进程")。所以这里的行数,就是你当前的连接数;跟 `max_connections` 比一下,就知道有没有连接爆了。

### 2.3 回扣

第 2 章连接与会话(进程模型)、第 8 章 BufferPool(IO 等待)、第 16 章锁(Lock 等待)、第 21 章 VACUUM(`idle in transaction` 阻塞清理)。

---

## 三、pg_locks + pg_blocking_pids:谁阻塞了谁

### 3.1 不这样会怎样:死锁、卡住,却找不到元凶

第 16 章讲过,PG 的锁是分层的(表锁 8 种 + 行锁),而且"读不阻塞写、写不阻塞读"是 MVCC 的功劳——但**写和写之间还是会冲突**。一个事务 `UPDATE users SET ... WHERE id = 1` 拿了行锁没提交,另一个事务也想改同一行,就会**阻塞等待**。这时候现象是"某个连接卡住不动",但你看不见**它为什么卡、卡在谁手上**。

> **不这样会怎样**:线上一个 `UPDATE` 挂了 30 秒,你只能猜"是不是另一个事务没提交"。可到底哪个 PID 是凶手、它在跑什么 SQL、它拿了什么锁——没有 pg_locks 你一概不知,只能干瞪眼等用户反馈。

### 3.2 两步定位阻塞链

```sql
-- 步骤 1:被阻塞的会话 + 直接揪出阻塞它的 PID
SELECT
    blocked.pid     AS blocked_pid,
    blocked.query   AS blocked_query,
    blocking.pid    AS blocking_pid,
    blocking.query  AS blocking_query,
    blocking.state  AS blocking_state
FROM pg_stat_activity blocked
JOIN pg_stat_activity blocking
  ON blocking.pid = ANY (pg_blocking_pids(blocked.pid))
WHERE blocked.wait_event_type = 'Lock';
```

`pg_blocking_pids(pid)` 是个**系统函数**,返回"正在阻塞给定 PID 的那些 PID 数组"——它内部已经处理了"锁的等待队列"和"事务持有的行锁"(行锁不是记在 pg_locks 里、而是记在 tuple 的 `xmax` 上,这个函数帮你跨过去查了)。

```sql
-- 步骤 2:看凶手具体拿了什么锁
SELECT pid, locktype, relation::regclass, mode, granted
FROM pg_locks
WHERE pid = <blocking_pid>;   -- 把上一步查出的 blocking_pid 填进来
```

`pg_locks` 一行 = 一个进程持有的(或在等的)一把锁。看几个关键列:

- **`locktype`**:`relation`(表级)、`transactionid`(等某个事务结束,常对应行锁)、`tuple`(直接锁某行)、`virtualxid`、`object` 等。
- **`mode`**:`AccessShareLock`(最弱的表锁,SELECT 自动加)、`RowExclusiveLock`(INSERT/UPDATE/DELETE 加)、`AccessExclusiveLock`(最强的表锁,TRUNCATE/DROP/ALTER 加)——名字和冲突矩阵全在第 16 章。
- **`granted`**:`t` = 拿到了,`f` = 还在等。**凶手行都是 `granted=t`,受害者行是 `granted=f`**。

> **死锁检测回扣**:第 16 章讲过 PG 有个 `deadlock_timeout`(默认 1 秒)的后台检测器。两个事务互相等对方,PG 会主动 kill 一个、报 `ERROR: deadlock detected`。但如果只是**单向等待**(没到死锁),进程会一直挂着——这时 pg_locks + pg_blocking_pids 就是你唯一的诊断手段。

### 3.3 回扣

第 16 章锁(锁层级、冲突矩阵、死锁检测)、第 17 章 MVCC(为什么读不阻塞写,所以 `granted=f` 的行锁等待基本都来自"写写冲突")。

---

## 四、pageinspect:把页/tuple/索引项的字节扒开看

### 4.1 不这样会怎样:讲"页布局"全是纸上谈兵

第 6、7、11 章讲页内布局、tuple 编码、B 树节点结构,全是"源码里这么定义、所以它长这样"。但你想**亲眼验证**——这个表第 0 个页是不是真的 24 字节页头?某个 tuple 的 `t_xmin`/`t_xmax` 是不是真的对应了第 17 章讲的 MVCC 版本链?B 树叶子页里那些索引项,真的是按 key 排好序的吗?

> **不这样会怎样**:学完存储引擎篇,你对"页"的理解停留在源码注释和示意图。你没法回答"**我这张表的具体某一页,此刻到底装了什么**"——而这恰恰是排查"页损坏""索引膨胀""死元组没清"这类问题的唯一入口。

pageinspect 这个 contrib 扩展就是为这个而生:它让你**直接读出某个 8KB 原始页**,把它拆成页头、行指针、tuple 字段、索引项。

### 4.2 启用与基本用法

```sql
-- 一次性启用(需要超级用户或表 owner 权限)
CREATE EXTENSION pageinspect;

-- 看第 0 页的页头(回扣第 6 章:24 字节的 PageHeaderData)
SELECT * FROM page_header(get_raw_page('users', 0));
-- 返回:lsn, checksum, flags, lower, upper, special, pagesize, version, prune_xid

-- 看第 0 页里的所有 tuple(回扣第 7 章:ItemId + HeapTupleHeader)
SELECT lp, lp_off, lp_flags, lp_len,
       t_xmin, t_xmax, t_ctid
FROM heap_page_items(get_raw_page('users', 0));
```

逐列对回正文:

- **`lp`**(line pointer 序号)、**`lp_off`**(tuple 在页内的偏移)、**`lp_flags`**(回扣第 6 章:1=正常、2=HOT 重定向、3=死元组,这是第 21 章 VACUUM 的入口)、**`lp_len`**(tuple 字节数)。
- **`t_xmin`**(插入这个 tuple 的事务)、**`t_xmax`**(删除/更新它的事务)、**`t_ctid`**(回扣第 17 章 MVCC:更新后的新版本在哪——这是版本链的"下一跳"指针)。**MVCC 不是抽象概念,这里你能直接读到它的物理载体。**

```sql
-- 看一个 B 树索引的叶子页(回扣第 11 章:B 树节点内的项)
SELECT itemoffset, ctid, itemlen, left(data, 24) AS data
FROM bt_page_items('users_pkey', 1);
-- 返回每项:它在页内的偏移、指向的堆 tuple (页号,行号)、项长度、key 字节
```

`bt_page_items` 有两种调用方式:`bt_page_items('索引名', 块号)` 直接读;或 `bt_page_items(get_raw_page('索引名', 块号))` 先拿原始页再解析。前者更方便。

### 4.3 两个进阶用法

```sql
-- 校验某页的 checksum(回扣第 6 章:页带 checksum 防静默腐败)
-- page_checksum(页内容, 块号) 算出的值,应与页头里存的 checksum 一致
SELECT page_checksum(get_raw_page('users', 0), 0)
     = (page_header(get_raw_page('users', 0))).checksum AS ok;

-- 看 FSM 页里"哪些块还有多少空闲"(回扣第 9 章 FSM)
SELECT * FROM fsm_page_contents(get_raw_page('users', 'fsm', 0));
```

> **权限提示**:`get_raw_page` 取的是**此刻**的页内容,期间会持有对应表的 `AccessShareLock`(第 16 章)。对大表查具体某页不会扫全表,但建议避开高峰。

### 4.4 回扣

第 6 章页(页头、行指针、checksum)、第 7 章 tuple(xmin/xmax/ctid)、第 9 章 FSM/VM、第 11 章 B 树、第 17 章 MVCC(版本链)、第 21 章 VACUUM(lp_flags=3 的死元组)。

---

## 五、pg_waldump:把 WAL 段文件 dump 成人话

### 5.1 不这样会怎样:日志就是个黑盒二进制

第 18、19、20 章讲 WAL 是预写日志、`COMMIT` 在等日志落盘、崩溃后靠重放日志重建。可 WAL 段文件(`pg_wal/` 下一堆 16MB 文件)是**二进制的**,你 `cat` 出来全是乱码。你想验证"一条 INSERT 真的在 WAL 里产生了记录""checkpoint 记录长什么样""崩溃恢复要重放哪些段"——只能靠想象。

> **不这样会怎样**:出了"数据不一致""恢复失败"这类最吓人的问题,你面对的是一堆二进制 WAL 文件,完全不知道里面记了什么。生产出事的时候,你没有手段去核对"WAL 里这条 commit 记录的 LSN 是不是真比 checkpoint 的 redo 点晚"——而这正是判断"会不会丢数据"的关键事实。

`pg_waldump` 是 PG 自带的命令行工具,把 WAL 文件翻译成人能读的记录。

### 5.2 基本用法(shell 命令)

```bash
# 必须用安装了 PG 的那个系统用户跑(通常 postgres)
# 默认从最新 WAL 段开始,持续输出(类似 tail -f)
pg_waldump

# 指定从某个 WAL 文件开始 dump
pg_waldump /var/lib/postgresql/17/main/pg_wal/000000010000000000000001

# 只看某类资源管理器(rmgr)的记录——最常用的两个
pg_waldump --rmgr=Transaction   # 只看 commit/abort 记录
pg_waldump --rmgr=Storage       # 只看文件创建/扩展

# 指定 LSN 范围(回扣第 18 章:崩溃恢复从 checkpoint 的 redo 点重放到末尾)
pg_waldump --start=0/03000000 --end=0/04000000
```

### 5.3 输出长什么样,看什么

典型一行:

```
rmgr: Transaction len (rec/tot): 34/34, tx: 742, lsn: 0/030000A0, prev: 0/03000060, desc: COMMIT 2026-06-18 10:23:11.891 CST; inval msgs...
```

逐字段对回正文:

- **`rmgr`(resource manager,资源管理器)**:回扣第 18 章——不同对象(堆、B 树、事务、存储……)的 redo 各归各家,这里就是"这条记录归哪个 rmgr 管"。常见的:`Transaction`(commit/abort)、`Heap`(行增删改)、`Btree`(索引变更)、`Storage`(文件创建/截断)、`XLOG`(checkpoint 这种全局事件)。
- **`tx`**:这条记录属于哪个事务 ID(回扣第 14、17 章 XID)。
- **`lsn` / `prev`**:回扣第 18 章——LSN 是"这页改动是否已在 WAL 里"的唯一判据,`prev` 让 WAL 形成单链表,崩溃恢复靠它往前回溯。
- **`desc`**:这条记录干了什么。`COMMIT` 就是提交;`INSERT_LEAF` 是 B 树插了叶子项;`CHECKPOINT_ONLINE` 是第 19 章的检查点记录。

```bash
# 找最近的 checkpoint 记录(回扣第 19、20 章:崩溃恢复的起点)
pg_waldump | grep -i checkpoint
# 输出形如:
# rmgr: XLOG        len ... desc: CHECKPOINT_ONLINE redo 0/02FF0000; ...
# 这里的 redo 就是崩溃恢复要重放的起点 LSN
```

```bash
# 数一下某段时间内有多少次提交(回扣第 18 章 group commit:多次提交可能合并成一次 fsync)
pg_waldump --rmgr=Transaction | grep -c COMMIT
```

> **替代方案**:PG 15+ 还有 `pg_walinspect` 扩展,可以在 SQL 里查同样内容(`pg_walinspect.pg_wal_dump` 系列函数),不用退出 psql。生产里更安全(不用 OS 层面碰数据目录)。pg_waldump 适合离线分析、教学演示。

### 5.4 回扣

第 18 章 WAL(记录结构、LSN、rmgr、full page image)、第 19 章 Checkpoint(redo 点)、第 20 章崩溃恢复(从 redo 点重放)。

---

## 六、pg_buffercache:看缓冲池里缓存了哪些页

### 6.1 不这样会怎样:命中率是个黑数

第 8 章讲 BufferPool 是性能命脉,关键指标是"命中率"——读请求有多少命中了内存、没去碰磁盘。但平时你能看到的只是 `pg_stat_database` 里一个累计的 `blks_hit`/`blks_read` 比值,看不出**到底是哪些表的哪些页**在内存里、哪些热表反而没被缓存(被淘汰了)。

> **不这样会怎样**:某个核心表查询突然变慢,你怀疑"是不是它的页被挤出 buffer pool 了"。可你看不见缓冲池里此刻有什么,只能猜"可能是缓存不够、加大 `shared_buffers` 吧"——方向对不对全靠运气,而且加大 buffer pool 不一定有用(可能是别的大表把它挤了)。

`pg_buffercache` 让你直接查缓冲池的每一格。

### 6.2 用法

```sql
CREATE EXTENSION pg_buffercache;

-- 整体概览:缓冲池里按数据库/表分组,各占多少 buffer
SELECT c.relname,
       count(*) AS buffers,
       round(count(*) * 100.0 / sum(count(*)) OVER (), 2) AS pct
FROM pg_buffercache b
JOIN pg_class c ON c.relfilenode = b.relfilenode
WHERE b.relfilenode IS NOT NULL
GROUP BY c.relname
ORDER BY buffers DESC
LIMIT 10;
```

`pg_buffercache` 一行 = 缓冲池的一个槽位(一个 8KB 页)。关键列:`bufferid`、`relfilenode`(这页属于哪个表/索引)、`relblocknumber`(块号)、`isdirty`(是不是脏页——回扣第 8 章,脏页迟早要落盘)、`usagecount`(回扣第 8 章 Clock-Sweep:访问一次 +1,被淘汰时 −1,值越大越"热")。

```sql
-- 看有多少脏页(回扣第 8 章:脏页要靠 checkpointer/bgwriter 刷盘)
SELECT count(*) FILTER (WHERE isdirty) AS dirty,
       count(*)                        AS total
FROM pg_buffercache;
```

```sql
-- 看某个热表有多少页在缓冲池里、usagecount 分布
SELECT usagecount, count(*) AS buffers
FROM pg_buffercache
WHERE relfilenode = (SELECT relfilenode FROM pg_class WHERE relname = 'users')
GROUP BY usagecount ORDER BY usagecount;
```

### 6.3 回扣

第 8 章 BufferPool(8KB 槽位、Clock-Sweep 替换策略、脏页、usagecount)、第 6 章页(每格就是一个页)。

---

## 七、pg_stat_user_tables / pg_stat_user_indexes:统计累加器

### 7.1 不这样会怎样:不知道该 VACUUM 谁、哪些索引是废物

第 21 章讲 VACUUM 清理死元组,第 11、13 章讲索引。但日常运维的核心问题是:**哪张表死元组堆积了?哪些索引从来没被查询用过(纯浪费)?某张表的全表扫描跑了多少次?**

> **不这样会怎样**:你不知道死元组情况,只能"定时全库 VACUUM",既慢又可能锁着关键表;你不知道哪些索引没人用,索引越建越多,INSERT/UPDATE 越来越慢(每次写都要维护索引),却不知道砍哪个。PG 内置的累加统计视图,就是回答这些"运维决策"的事实依据。

### 7.2 pg_stat_user_tables:表的扫描与死元组统计

```sql
SELECT relname,
       n_live_tup, n_dead_tup,
       round(n_dead_tup * 100.0 / NULLIF(n_live_tup, 0), 1) AS dead_pct,
       last_vacuum, last_autovacuum,
       seq_scan, seq_tup_read,
       idx_scan
FROM pg_stat_user_tables
ORDER BY n_dead_tup DESC
LIMIT 10;
```

关键列对回正文:

- **`n_live_tup` / `n_dead_tup`**:活元组数 / 死元组数。回扣第 17、21 章——UPDATE/DELETE 不真删,只把旧版本标记成死元组(`xmax` 标记),要靠 VACUUM 回收。**`n_dead_tup` 远大于 `n_live_tup`、或 `dead_pct` 很高,就是该 VACUUM 了**——`autovacuum` 应该已经触发,没触发就是阈值(`autovacuum_vacuum_scale_factor`)没调好。
- **`last_vacuum` / `last_autovacuum`**:上次手动/自动 VACUUM 的时间,判断 autovacuum 是不是在干活。
- **`seq_scan` / `seq_tup_read`**:回扣第 10 章——这张表被全表扫了多少次、累计读了多少行。一个本该走索引的表 `seq_scan` 很大,说明查询在扫全表(回头看 EXPLAIN)。
- **`idx_scan`**:这张表上所有索引被用的总次数。`idx_scan = 0` 且表很大的,可能根本没用上索引。

### 7.3 pg_stat_user_indexes:单个索引的使用情况

```sql
SELECT schemaname, relname, indexrelname,
       idx_scan, idx_tup_read, idx_tup_fetch
FROM pg_stat_user_indexes
ORDER BY idx_scan ASC
LIMIT 20;
```

- **`idx_scan`**:这个索引被用来扫描的次数。**长期为 0 的索引是"废物索引"**——它不帮任何查询加速,却让每次 INSERT/UPDATE 都要维护它(回扣第 11 章:B 树插入要保平衡,有写代价)。候选删除对象。
- **`idx_tup_read`**:从索引读出多少项;**`idx_tup_fetch`**:回表取了多少行。回扣第 13 章——`idx_tup_fetch` 远小于 `idx_tup_read`,可能是"仅索引扫描"在生效(不用回表);反之大量回表,说明索引不够"覆盖"。

### 7.4 回扣

第 10/11/13 章索引(用不用、要不要)、第 17 章 MVCC(死元组的来源)、第 21 章 VACUUM(什么时候该清、autovacuum 在不在干活)。

---

## 八、auto_explain + 日志:自动捕捉慢查询的真实计划

### 8.1 不这样会怎样:问题复现不了,事后没证据

EXPLAIN ANALYZE 是诊断利器,但它有个硬伤:**你得手动跑**。生产里慢查询常常是偶发的——某个时刻数据分布变了、并发上来了、buffer pool 被冲了,某个平时快的 SQL 突然慢 10 倍,等你接到告警手动去 EXPLAIN,时过境迁,计划已经恢复正常,你抓不到现场。

> **不这样会怎样**:线上每天凌晨有个报表跑 40 秒,白天同一个查询只要 0.3 秒。你白天怎么 EXPLAIN 都看不出问题,因为复现不了凌晨的现场——统计信息是那个时刻的、buffer pool 是那个时刻的、并发是那个时刻的。没有 auto_explain,你永远不知道凌晨那条 SQL 当时到底走了什么计划。

auto_explain 让 PG **自动**把跑得慢的语句的真实执行计划(含 actual rows/time)写进服务器日志。

### 8.2 启用(两步)

```ini
# 步骤 1:postgresql.conf 里加载这个模块(它是 preload 库,必须重启生效)
shared_preload_libraries = 'auto_explain'
# 或者只想本会话生效、不想重启,可以会话里:LOAD 'auto_explain';
```

```sql
-- 步骤 2:设阈值和选项(可在 postgresql.conf 全局设,也可会话内 ALTER SYSTEM/SET)
-- 只记录跑超过 100ms 的语句的计划
SET auto_explain.log_min_duration = '100ms';
-- 真跑一遍拿 actual 数据(默认只记录估算计划)
SET auto_explain.log_analyze = on;
-- 顺便记录 buffer 命中(同 EXPLAIN BUFFERS)
SET auto_explain.log_buffers = on;
-- 用 JSON 格式存,便于后续工具解析
SET auto_explain.log_format = json;
-- 记录嵌套语句(函数里跑的 SQL)
SET auto_explain.log_nested_statements = on;
```

`log_min_duration = 0` 记录**所有**语句(慎用,日志会爆);设成一个合理阈值(如 `100ms`)只抓慢的,是生产推荐做法。

### 8.3 日志里能看到什么

跑完一条慢 SQL,服务器日志里会自动出现一段:

```
LOG:  duration: 1283.456 ms  plan:
        Query Text: SELECT * FROM orders WHERE user_id = 7724 AND created_at > '2026-01-01'
        Seq Scan on orders  (cost=0.00..45230.00 rows=120 width=142) (actual time=1.2..1280.3 rows=3 loops=1)
          Filter: ((user_id = 7724) AND (created_at > '2026-01-01'))
          Rows Removed by Filter: 4800000
          Buffers: shared hit=40 read=52030
```

和手动 EXPLAIN ANALYZE BUFFERS 的输出**一模一样**,只是它在真实业务流量下、真实的那个时刻自动生成的——`rows=3` 而 `Rows Removed by Filter: 4800000`,一目了然:**优化器以为只扫出 120 行,实际扫了 480 万行才筛出 3 行,典型的统计信息过期/缺索引**。这正是手动复现抓不到的现场。

### 8.4 回扣

第 4 章优化器(估算 vs 实际的偏差,正是慢查询的根因)、第 5 章执行器(actual time/loops 反映真实执行)、第 8 章 BufferPool(`shared read=52030` 说明疯狂读盘)、第 10 章(该走索引却走了 Seq Scan)。

---

## 收束:工具是主线的"探针"

把这八个工具和全书主线对一遍,你会发现它们不是孤立的"运维手册",而是**让你验证正文每一章机制的探针**:

- 想验证"优化器猜得准不准" → EXPLAIN + auto_explain(第 4 章)。
- 想验证"buffer pool 到底罩住了没有" → EXPLAIN BUFFERS + pg_buffercache(第 8 章)。
- 想验证"MVCC 的版本链是不是真的在 tuple 里" → pageinspect 的 `t_xmin`/`t_xmax`/`t_ctid`(第 7、17 章)。
- 想验证"COMMIT 真的在等 WAL 落盘、checkpoint 真的写了 redo 点" → pg_waldump(第 18-20 章)。
- 想验证"谁阻塞了谁、为什么读不阻塞写" → pg_stat_activity + pg_locks + pg_blocking_pids(第 2、16、17 章)。
- 想验证"死元组堆了多少、索引有没有被用" → pg_stat_user_tables/indexes(第 11、17、21 章)。

> **一句话**:原理告诉你"**应该**是这样",这些工具让你看见"**此刻是不是真的这样**"。会了原理不会工具,是纸上谈兵;会了工具不懂原理,是只见树木不见森林——两本书(正文 + 本附录)合起来,才是一个完整的内核视角。

---

## 涉及的系统视图与扩展清单

| 工具 | 类别 | 名称 |
|---|---|---|
| 执行计划 | SQL 命令 | `EXPLAIN` / `EXPLAIN ANALYZE` / `EXPLAIN (ANALYZE, BUFFERS)` |
| 会话监控 | 系统视图 | `pg_stat_activity` |
| 锁等待 | 系统视图 + 系统函数 | `pg_locks`、`pg_blocking_pids()` |
| 页/tuple 内部 | contrib 扩展 | `pageinspect`(`get_raw_page`、`heap_page_items`、`bt_page_items`、`page_header`、`page_checksum`、`fsm_page_contents`) |
| WAL 内容 | 命令行工具 + 扩展 | `pg_waldump`(自带)、`pg_walinspect`(PG 15+) |
| 缓冲池 | contrib 扩展 | `pg_buffercache` |
| 表/索引统计 | 系统视图 | `pg_stat_user_tables`、`pg_stat_user_indexes` |
| 慢查询自动记录 | preload 库 | `auto_explain`(`shared_preload_libraries`) |

> 全部为 PostgreSQL 自带的标准系统视图或官方 contrib 扩展,无需第三方依赖;除 `pageinspect`、`pg_buffercache`、`pg_walinspect`、`auto_explain` 需 `CREATE EXTENSION` 或 `shared_preload_libraries` 外,其余 `pg_stat_*` / `pg_locks` 开箱即用。
