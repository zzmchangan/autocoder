# 附录 B · InnoDB 工具链与实践:怎么看见、怎么备份、怎么排查

> **定位**:前面 23 章把 InnoDB 的四个灵魂(B+树聚簇索引、WAL/redo/undo/2PC、MVCC、锁)从设计动机到实现技巧拆了一遍,你脑子里应该已经能"放映"出一条 `UPDATE` 在 InnoDB 里的全过程。可一个真实的生产 InnoDB 是个**活着的、有状态的黑盒**——它在内存里维护着几百万个缓存页、几十万把锁、几千条事务,在磁盘上摊着几百个表空间、几条 redo 文件、undo 段;一旦线上出了"慢查询""死锁""锁等待""undo 堆积""crash 起不来",你光懂原理不够,**还得有一套能动手观测、能定位到具体页/具体事务/具体锁的工具**。本附录就是这套工具箱:把你读完全书掌握的"原理",接到真实线上能用的"命令"上。

> **读完本附录你会明白**:
> 1. **怎么从外部看见 InnoDB 内部**——`SHOW ENGINE INNODB STATUS`、`information_schema.INNODB_*` 一族、`performance_schema` 的锁等待/IO/语句观测、`explain` 看执行计划,各自看什么、看哪个表对应哪一章的原理。
> 2. **怎么直接看磁盘上的页和数据字典**——`ibd2sdi` 导出 SDI、`innochecksum` 校验页、`innodb_page_size` 看 B+树页布局,把 P1-04 的页结构落到真实 `.ibd` 文件上。
> 3. **怎么备份与恢复**——`xtrabackup`(物理备份,承 P3-08 的 redo 一致性)、`clone` 插件(8.0+ 原生克隆,架构演进)、`mysqldump`(逻辑备份)、它们和 P3-12 crash recovery 的关系。
> 4. **怎么把 InnoDB 的概念映射到《PG》《LevelDB》《TiKV》**——读过前作的读者,一张对照表就能快速打通。
> 5. **线上问题排查清单(实用重头)**——死锁、慢查询、锁等待、buffer pool 命中率、undo/purge 滞后、crash recovery 失败,每条给**命令 + 怎么读输出 + 对应正文哪一章原理**。

> **一个提醒**:本附录是"工具书 + 排查手册",**不重复讲机制**(机制在正文 23 章里),每条命令都回扣正文原理、指路哪一章。读的时候,可以把它当字典查——遇到哪类问题,直接翻到对应小节。

---

## 〇、一句话点破

> **观测 InnoDB 的工具分三圈:最内圈是 `SHOW ENGINE INNODB STATUS`(一把抓全貌)、中间一圈是 `information_schema.INNODB_*` 和 `performance_schema` 的细粒度观测表(按需深挖某一块)、最外圈是磁盘工具(`ibd2sdi`/`innochecksum`,直接看 `.ibd` 文件)。备份分三类:物理备份(xtrabackup/clone,快、承 redo 一致性)、逻辑备份(mysqldump,慢但跨版本)、binlog(增量 + point-in-time 恢复)。排查线上问题,就是"先用 SHOW STATUS 抓异常指标 → 再用 i_s/p_s 锁定具体事务/锁/SQL → 最后用 explain 定位 SQL 本身的问题"。**

这是结论,不是理由。本附录倒过来:先讲三圈观测工具各自看什么,再讲怎么看磁盘页,然后讲备份恢复三套,接着给一张与前作的对照表,最后把线上最常见的六类问题的排查步骤摊成清单。

---

## 一、观测 InnoDB 内部:三圈工具

### (一)最内圈:`SHOW ENGINE INNODB STATUS`——一把抓全貌

这是 InnoDB 自带的、最古老也最常用的"体检报告"。一条命令:

```sql
SHOW ENGINE INNODB STATUS\G
```

(用 `\G` 让输出竖排,因为这段输出极长,横排会折断。)

它的输出是一段叫 **InnoDB Monitor** 的文本,分成好几节,每一节对应 InnoDB 内部的一个子系统。**读懂这段输出的关键是:知道每一节对应正文的哪一章。**

| 输出小节 | 长什么样 | 看什么 | 对应正文 |
|---------|---------|--------|---------|
| `LATEST DETECTED DEADLOCK` | 死锁时打印两个事务互相等的锁链 | 死锁发生在哪两个事务、哪两个索引、哪两行 | P5-18 |
| `LATEST FOREIGN KEY ERROR` | 外键冲突 | 哪个外键约束被违反 | (外键略) |
| `TRANSACTIONS` | `Trx id counter`、`Trx active approx`、活跃事务列表、`History list length` | 当前有多少活跃事务、**undo 版本链堆积多长(purge 滞后)** | P3-10 / P4-15 |
| `FILE I/O` | `Pending reads/writes`、I/O 线程状态、`reads/s, writes/s, fsyncs/s` | IO 压力、是否 IO bound | P2-05 |
| `INSERT BUFFER AND ADAPTIVE HASH INDEX` | `Ibuf: size, free list len, merges`、`AHI: size, hash searches/s` | change buffer 和 AHI 的命中、是否在起作用 | P2-07 |
| `LOG` | `Log sequence number`(LSN)、`Log flushed up to`、`Pages flushed up to`、`Last checkpoint at` | **redo 写到哪、刷到哪、checkpoint 到哪——三者的差就是崩溃恢复要重做的量** | P3-08 / P3-12 |
| `BUFFER POOL AND MEMORY` | `Total memory allocated`、`Dictionary memory allocated`、`Buffer pool size`、`Free buffers`、`Database pages`、`Modified db pages`、`Buffer pool hit rate N / 1000` | buffer pool 容量、脏页数、**命中率(看是否 IO bound)** | P2-05 / P2-06 |
| `ROW OPERATIONS` | `0 queries inside InnoDB`、`0 read views open`、`Number of rows inserted/updated/deleted/read` | 当前在 InnoDB 内的查询数、打开的 read view 数、行级吞吐 | P4-13 |

**怎么读它,有几个最常见的看点:**

1. **`Buffer pool hit rate 1000 / 1000`**:命中满分,说明绝大多数读都在内存,不碰磁盘;如果掉到 `990 / 1000` 甚至 `900 / 1000`,说明每 100~1000 次读就有一次真打磁盘——这是 buffer pool 太小的信号(承 P2-05)。这个字符串就是源码 `buf0buf.cc` 里直接 `fprintf` 出来的(`storage/innobase/buf/buf0buf.cc` 的 `Buffer pool hit rate %lu / 1000`),所以无论哪个版本都长这样。
2. **`Log sequence number` vs `Last checkpoint at`**:这两个 LSN 的差,就是"redo 已经写了但 checkpoint 还没推进"的量,也就是**崩溃恢复时要重放的 redo 量**。差值越大,恢复越慢——如果你看到这个差值在涨(checkpoint 推不动),往往是刷脏页跟不上(承 P3-08 的 checkpoint、P3-12 的恢复)。
3. **`History list length`(在 `TRANSACTIONS` 节)**:这是 undo 版本链的长度,也就是 purge 还没清理掉的旧版本数量。**这个数字稳定在低位(几百几千)是健康的;如果它持续涨(几十万、上百万),说明 purge 跟不上**(长事务持着 read view 不放,旧版本不能回收)——这是 MVCC 滞后的最直接信号(承 P4-15)。
4. **`LATEST DETECTED DEADLOCK` 节**:只要死锁发生过,这里就存着**最近一次**死锁的详情(两个事务、加锁的索引、等待的锁类型)。这是排查死锁的第一现场(本附录第五节详讲)。

> **钉死这件事**:`SHOW ENGINE INNODB STATUS` 是 InnoDB 的"全科体检报告",它的每一节都直接对应正文某一章的子系统。**读完正文再看这段输出,你会发现每一段不再是天书**——你知道 `History list length` 是 P4-15 的 purge 滞后、`Modified db pages` 是 P2-05 的 flush list、`Log sequence number - Last checkpoint at` 是 P3-12 要重放的 redo 量。

**一个注意**:`SHOW ENGINE INNODB STATUS` 的输出有大小限制(默认 1MB 左右,`innodb_status_output` 还能控制是否定期写错误日志)。活跃事务特别多时,事务列表会被截断——这时要用下面的细粒度观测表。

### (二)中间圈:`information_schema.INNODB_*`——按需深挖某一块

`SHOW ENGINE INNODB STATUS` 是一段固定文本,不好按条件查。InnoDB 把内部状态搬进了一族 `information_schema` 的表(源码在 `storage/innobase/handler/i_s.cc`),你可以像查普通表一样 `SELECT ... WHERE ...`,精确地看某一块。**这是日常排查的主力。**

最常用的几张:

| 表名 | 看什么 | 对应正文 | 典型查询 |
|------|--------|---------|---------|
| `INNODB_TRX` | 当前所有活跃事务(trx_id、状态、开始时间、锁数、行修改数、执行的 SQL) | P3-10 / P5-16 | `SELECT trx_id, trx_state, trx_started, trx_weight, trx_query FROM information_schema.INNODB_TRX ORDER BY trx_started;` |
| `INNODB_LOCKS`(8.0 起迁到 performance_schema.data_locks) | 当前持有/等待的锁(8.0 之前用) | P5-16 / P5-17 | (8.0+ 见下) |
| `INNODB_LOCK_WAITS`(8.0 起迁到 performance_schema.data_lock_waits) | 锁等待关系(谁等谁) | P5-18 | (8.0+ 见下) |
| `INNODB_BUFFER_PAGE` | buffer pool 里**每个缓存页**的详情(表空间、页号、页类型、是否脏、是否 young) | P2-05 / P2-06 | `SELECT PAGE_TYPE, COUNT(*) FROM information_schema.INNODB_BUFFER_PAGE GROUP BY PAGE_TYPE;` |
| `INNODB_BUFFER_PAGE_LRU` | 同上,但按 LRU 顺序(看 young/old 分布) | P2-06 | 看 midpoint insertion 的效果 |
| `INNODB_BUFFER_POOL_STATS` | buffer pool 整体统计(命中、free、脏页) | P2-05 | 比读文本更结构化 |
| `INNODB_METRICS` | 几百个内部计数器(buffer/lock/log/os 等) | 全书 | `SELECT name, count FROM information_schema.INNODB_METRICS WHERE name LIKE 'buffer_pool%';` |
| `INNODB_CMP` / `INNODB_CMP_RESET` | 压缩页的压缩/解压次数、耗时(按页大小) | P1-04(压缩表) | 调压缩表时看 |
| `INNODB_CMPMEM` / `INNODB_CMPMEM_RESET` | buffer pool 里压缩页的分配/换出 | P1-04 / P2-05 | 同上 |
| `INNODB_CMP_PER_INDEX` / `..._RESET` | 按索引粒度的压缩统计 | P1-04 | 同上 |
| `INNODB_TEMP_TABLE_INFO` | 当前 InnoDB 临时表 | — | 8.0 临时表在独立表空间 |
| `INNODB_FT_*`(`DEFAULT_STOPWORD`/`DELETED`/`BEING_DELETED`/`INDEX_CACHE`/`INDEX_TABLE`/`CONFIG`) | 全文索引内部状态 | — | 用 FT 时看 |
| `INNODB_INDEXES` / `INNODB_TABLES` / `INNODB_COLUMNS` / `INNODB_FIELDS` / `INNODB_TABLESPACES` | 8.0 新数据字典(DD)在 InnoDB 里的元数据视图 | P1-02 / P6-22 | 看 instant DDL 改了哪些列 |
| `INNODB_TABLESPACES_SCRUBBING` | 表空间"擦除"进度(安全删除) | — | 安全合规场景 |

**最常用的三张是 `INNODB_TRX`、`INNODB_BUFFER_PAGE`、`INNODB_METRICS`。** 比如:

- **找长时间运行的事务**(往往是死锁/锁等待/purge 滞后的元凶):

```sql
SELECT trx_id, trx_state, trx_started,
       TIMESTAMPDIFF(SECOND, trx_started, NOW()) AS run_seconds,
       trx_rows_modified, trx_query
FROM information_schema.INNODB_TRX
ORDER BY trx_started
LIMIT 10;
```

`run_seconds` 大的事务,就是"持锁太久 / read view 挡着 purge"的嫌疑犯(承 P5-16 两阶段锁协议、P4-15 purge 与 read view)。

- **看 buffer pool 都缓存了哪些类型的页**(索引页/数据页/undo 页/系统页/adaptive hash):

```sql
SELECT PAGE_TYPE, COUNT(*) AS cnt
FROM information_schema.INNODB_BUFFER_PAGE
GROUP BY PAGE_TYPE
ORDER BY cnt DESC;
```

如果 `INDEX` 类型的页占绝大多数,说明缓存的就是业务 B+树页,正常;如果 `UNDO_LOG` 或 `SYSTEM` 占比异常高,要警惕(承 P2-05、P3-10)。

> **新旧差异提醒**:8.0 之前,看锁用 `information_schema.INNODB_LOCKS` / `INNODB_LOCK_WAITS`;**8.0 起,锁信息迁到了 `performance_schema.data_locks` / `data_lock_waits`**(老的 `INNODB_LOCKS` 还在但被废弃、9.x 可能移除)。这是排查死锁/锁等待时最容易踩的版本坑——本附录第五节详讲新表怎么查。

### (三)最内圈的补充:`performance_schema`——锁等待/IO/语句级观测

`performance_schema`(p_s)是 MySQL 的运行时观测框架,和 `information_schema.INNODB_*` 互补:前者更偏"跨存储引擎的、语句级/等待事件级"的观测,后者是 InnoDB 内部的细粒度快照。**对 InnoDB 排查,最有用的是 p_s 的锁等待和 IO 两块。**

| p_s 表 | 看什么 | 对应正文 |
|--------|--------|---------|
| `data_locks` | **当前所有 InnoDB 锁**(8.0+ 取代 INNODB_LOCKS)——锁在哪个库/表/索引、锁类型(RECORD/GAP/NEXT-KEY)、模式(S/X/IS/IX) | P5-16 / P5-17 |
| `data_lock_waits` | **锁等待关系**(哪个事务等哪个锁、等了多久) | P5-18 |
| `events_waits_current` / `_history` / `_history_long` | 当前/历史等待事件(synch mutex/rwlock、IO) | 同步原语、P2-05 |
| `events_statements_summary_by_digest` | 按SQL指纹聚合的执行统计(执行次数、平均耗时、扫描行数、临时表) | P6-21 |
| `file_summary_by_instance` | 按文件聚合的 IO 读写量(哪个表空间 IO 最重) | P2-05 |
| `mutex_instances` / `rwlock_instances` | InnoDB 内部互斥量/读写锁的实例(诊断内部锁竞争) | `sync/` |

**死锁排查的两张表 `data_locks` + `data_lock_waits`**,本附录第五节有完整例程。这里先看一个"锁等待实时抓"的典型:

```sql
-- 谁在等谁的锁
SELECT
    r.trx_id AS waiting_trx_id,
    r.trx_mysql_thread_id AS waiting_thread,
    r.trx_query AS waiting_query,
    b.trx_id AS blocking_trx_id,
    b.trx_mysql_thread_id AS blocking_thread,
    b.trx_query AS blocking_query,
    TIMEDIFF(NOW(), r.trx_wait_started) AS waited
FROM performance_schema.data_lock_waits w
JOIN information_schema.INNODB_TRX b ON b.trx_id = w.blocking_engine_transaction_id
JOIN information_schema.INNODB_TRX r ON r.trx_id = w.requesting_engine_transaction_id;
```

这一条,把"谁被卡住、卡它的是谁、卡了多久、各自在跑什么 SQL"一次抓全(承 P5-18 死锁检测的 wait-for graph,这里相当于手动版)。

> **启用提醒**:p_s 默认开启,但某些 instrument(尤其 `wait/synch/mutex/innodb/%`、`wait/lock/table/sql/handler`)可能没默认 enable。诊断锁/IO 前,先 `UPDATE performance_schema.setup_instruments SET ENABLED='YES', TIMED='YES' WHERE NAME LIKE '%lock%';` 把相关 instrument 打开。

### (四)最外圈:看执行计划——`explain`(承 P6-21)

`explain` 是看一条 SQL **怎么走 B+树、走了哪个索引、扫了多少行、有没有回表** 的工具,正文 P6-21 单独一章讲透。这里只给排查时最常用的判读速查:

```sql
EXPLAIN SELECT * FROM orders WHERE user_id = 12345 AND create_time > '2026-01-01';
```

| `explain` 列 | 看什么 | 出问题的信号 | 对应正文 |
|-------------|--------|-------------|---------|
| `type` | 访问类型 | `ALL`(全表扫)、`index`(全索引扫)通常是坏的;`const`/`eq_ref`/`ref`/`range` 好 | P1-02 / P1-03 / P6-21 |
| `key` | 实际用的索引 | `NULL`(没用索引)或用了非预期的索引 | P6-21 |
| `key_len` | 索引里用了多少字节 | 判断"最左前缀"用到哪一列 | P1-03 |
| `rows` | 估算扫描行数 | 数量级远大于结果集 → 索引没选好 | P6-21 |
| `Extra` | 额外信息 | `Using filesort`(额外排序)、`Using temporary`(临时表)、`Using join buffer` 通常要优化;`Using index` 是覆盖索引(好) | P1-03(覆盖索引)/ P6-21 |

**8.0+ 的 `EXPLAIN ANALYZE`**(实际跑一遍、给出每一步真实耗时和行数),对复杂 JOIN 定位瓶颈更准:

```sql
EXPLAIN ANALYZE SELECT ... ;
```

排查慢查询的完整流程见本附录第五节"慢查询"清单。

---

## 二、看磁盘上的页:`ibd2sdi` / `innochecksum` / 页布局

P1-04 讲了 B+树页的 16KB 内部长什么样(File Header + Page Header + 记录 + Page Directory + File Trailer)。但那是"纸上"的页。**想看一个真实 `.ibd` 文件里的页**,得用磁盘工具。

### (一)`ibd2sdi`:导出表空间的序列化数据字典(SDI)

8.0 数据字典重构(替掉老 `.frm` 文件)之后,**每个 `.ibd` 表空间文件里,都内嵌了一份它自己的元数据**,叫 **SDI(Serialized Dictionary Information)**。`ibd2sdi` 就是把这份元数据导出来的工具(源码在 `utilities/ibd2sdi.cc`):

```bash
ibd2sdi /var/lib/mysql/mydb/orders.ibd
```

输出是 JSON,包含这张表的列定义、索引定义、字符集等——**相当于把一张表的"身份证"从 `.ibd` 里抠出来**,不用连数据库就能看。

```json
[
  [
    {
      "type": 1,
      "id": 1234,
      "object": {
        "dd_version": 90700,
        "name": "orders",
        "columns": [
          {"name": "id", "type": 3, "is_nullable": false, ...},
          {"name": "user_id", "type": 3, ...},
          ...
        ],
        "indexes": [
          {"name": "PRIMARY", "type": 1, "elements": [...]},
          {"name": "idx_user", "type": 3, ...}
        ]
      }
    }
  ]
]
```

**什么场景用它**:
- **不用起库、想确认某张表的列/索引定义**(比如一个 `.ibd` 文件是别人拷来的、或数据库起不来);
- **验证 instant DDL 改了什么**(承 P6-22:instant DDL 只改 SDI、不动数据页,`ibd2sdi` 能看出列数变了);
- **排查数据字典和 `.ibd` 不一致**(极端情况下的元数据损坏)。

> **注意**:SDI 只在"file-per-table"的 `.ibd` 里有;系统表空间、临时表空间、undo 表空间里 SDI 的内容不同。另外 SDI 存的是 DD(新数据字典)格式,版本间字段可能变。

### (一+)`innochecksum`:校验页完整性、看页类型分布

`innochecksum` 原本是校验 `.ibd`/`.ibdata` 文件页 checksum 的工具,但加了几个有用的开关,变成了"看页布局"的瑞士军刀:

```bash
# 看 .ibd 文件里各类型页的分布(数据页/undo/insert buffer/...):
innochecksum --page-type-summary /var/lib/mysql/mydb/orders.ibd

# 看某个具体页号的内容:
innochecksum --page=x /var/lib/mysql/mydb/orders.ibd

# 只校验 checksum(损坏排查,见第五节 crash recovery):
innochecksum /var/lib/mysql/mydb/orders.ibd
```

`--page-type-summary` 的输出会告诉你这张表占了多少页、其中多少是叶子数据页、多少是内部节点、多少是 blob/overflow 页(承 P1-04 行溢出 lob)。**排查"为什么这张表这么大""大字段占了多少页"时很有用。**

### (二)直接看页的二进制(进阶,可选)

更硬核的玩法:把页 dump 出来看二进制。MySQL 没有官方"页 dump"工具(社区有 `innodb_ruby` 等第三方工具),但 `innochecksum -p N`(导出第 N 页)能拿到一个页的原始字节,自己对着 P1-04 的页结构解析。这是深入理解页结构最直接的方式——但日常排查用不到,属于"想玩透"的读者的事。

### (三)几个常查的页大小/布局参数

```sql
SHOW VARIABLES LIKE 'innodb_page_size';       -- 一般是 16384(16KB),可设 4K/8K/16K/32K/64K
SHOW VARIABLES LIKE 'innodb_data_file_path';  -- 系统表空间
SHOW VARIABLES LIKE 'innodb_file_per_table';  -- 是否 file-per-table
SHOW VARIABLES LIKE 'innodb_undo_tablespaces';-- undo 表空间(承 P3-10)
```

`innodb_page_size` 一旦建库就定死(后续不能改),因为它决定了 B+树页的布局、行格式、`innodb_log_file_size` 的下限关系(承 P1-04)。

---

## 三、备份与恢复:物理 / 逻辑 / binlog 三套

数据丢了是灾难,所以**备份是 InnoDB 实践里和"调优"并列的头等大事**。InnoDB 的备份分三类,各有适用场景,理解它们要用到正文 P3-08(WAL)、P3-12(crash recovery)的原理。

### (一)物理备份:复制页 + 配 redo——快、承 WAL 一致性

物理备份就是**直接拷 InnoDB 的数据文件(页)**,不经过 SQL 层。好处是快(拷文件比一条条 SELECT 快几个数量级),坏处是**必须解决"拷的时候页还在被改"的一致性问题**——这正是 WAL/redo 的用武之地。

**① Percona XtraBackup**(社区最主流的物理备份工具):

它的工作原理就是 P3-08/P3-12 WAL 思想的直接应用:
1. **拷 `.ibd`/`.ibdata` 数据文件**(这期间数据库还在写,拷到的页可能"半新半旧");
2. **同时持续追 redo log**(把备份期间产生的 redo 也存下来);
3. **恢复时,先恢复拷到的页,再重放这段 redo**(就像一次迷你 crash recovery)——把"半新半旧"的页补成一致状态。

```bash
# 全量备份
xtrabackup --backup --target-dir=/backup/full --user=... --password=...

# prepare(应用 redo,让备份达到一致点,这一步就是模拟 crash recovery 的 redo 重放)
xtrabackup --prepare --target-dir=/backup/full

# 恢复(拷回数据目录)
xtrabackup --copy-back --target-dir=/backup/full
```

`--prepare` 这一步,本质就是 P3-12 的 `log0recv` 重放在备份场景下复用——redo 重放把页里"redo 已写、脏页未刷"的修改补齐。**所以理解了 P3-08/12,你就理解了 xtrabackup 为什么 sound。**

**② MySQL 原生 clone 插件(8.0+,架构演进)**:

8.0 起,Oracle 把"克隆"做成了内置插件(源码在 `plugin/clone/`,核心 `clone_plugin.cc`),`CLONE_PLUGIN_VERSION = 0x0100`。它和 xtrabackup 思路类似(物理拷页 + redo 一致),但**原生集成进 server**——可以直接用 SQL 命令克隆,不用外部工具:

```sql
INSTALL PLUGIN clone SONAME 'mysql_clone.so';

-- 本地克隆到目录
CLONE LOCAL DATA DIRECTORY = '/backup/clone_dir/';

-- 远程克隆(从 donor 实例克隆到本地)
CLONE INSTANCE FROM 'user'@'donor-host':3306 IDENTIFIED BY 'pwd';
```

clone 插件的优势:① 原生,不用装第三方;② 远程克隆可以直接拿来**搭从库、扩容、升级**(承 redo log 一致性);③ 和 InnoDB 的页/redo/字典深度集成。劣势:只克隆 InnoDB(其他引擎要自己处理)、版本兼容性要求严(一般 donor 和 recipient 大版本一致)。

> **新旧差异**:5.7 时代物理备份只能靠 xtrabackup;8.0 起有了原生 clone。两者原理都是"物理拷页 + redo 一致",只是 clone 是 Oracle 官方、集成更深。生产里两者都用,看团队习惯。

### (二)逻辑备份:`mysqldump`——SQL 重放,慢但跨版本

逻辑备份就是把数据**导成 SQL 语句**(一堆 `CREATE TABLE` + `INSERT`),恢复时重新执行一遍。工具是 `mysqldump`(源码在 `client/mysqldump.cc`):

```bash
# 单库逻辑备份(--single-transaction 保证拿到一致快照,承 P4-13 read view)
mysqldump --single-transaction --routines --triggers --events \
          --databases mydb > mydb.sql

# 全实例
mysqldump --single-transaction --all-databases > all.sql
```

**关键参数 `--single-transaction`**:它在一个事务里(`START TRANSACTION WITH CONSISTENT SNAPSHOT`)dump 所有表,本质就是开一个 RR 的 read view(P4-13/14),dump 全程看的是这个一致快照、不被别的事务干扰。**这就是 MVCC 在备份场景的直接应用**——不用锁表、不阻塞业务,就能拿到一致快照。不用这个参数,要么锁表(阻塞写)、要么 dump 出来的数据跨事务不一致。

`mysqldump` 的特点:
- **慢**:走 SQL 层,一条条 `SELECT` + `INSERT`,几百 GB 要几小时;
- **跨版本、跨引擎**:导出是纯 SQL,恢复时不挑版本(8.0 dump 出来的能导进 8.4);
- **小数据量/迁移/升级**首选,大数据量物理备份更合适。

**另一个逻辑备份工具:`mysqlpump`(8.0+,并行版 mysqldump)** 和 **`SELECT ... INTO OUTFILE`**(导 CSV)。还有第三方的 `mydumper`/`myloader`(并行逻辑备份,生产常用)。

### (三)binlog:增量备份 + point-in-time 恢复

物理/逻辑备份都是"某个时间点的全量快照"。要恢复到"昨天的某个时刻",得靠 **binlog**(MySQL server 层的归档日志,记录所有改库操作)。binlog 是**逻辑日志**(记的是"执行了什么 SQL"或"行怎么变的",statement/row/mixed 三种格式),和 InnoDB 的 redo(物理日志,记"页怎么变")互补。

```bash
# 看 binlog 列表
mysqlbinlog --list-binlogs

# 把某个 binlog 转成可读 SQL
mysqlbinlog --start-datetime="2026-06-24 10:00:00" \
            --stop-datetime="2026-06-24 14:00:00" \
            mysql-bin.000123 > recover.sql

# 恢复:先恢复全量备份,再重放 binlog 到目标时刻
mysql < full_backup.sql
mysql < recover.sql
```

**binlog 和 P3-11 两阶段提交的关联**:redo(InnoDB)和 binlog(server 层)必须一致,靠 2PC 保证。备份恢复时,全量物理备份(经 redo 重放一致)+ binlog 重放 = 完整恢复。binlog 还兼从库复制的来源(主从同步)。

### (四)三套备份的对照与选型

| 维度 | 物理备份(xtrabackup/clone) | 逻辑备份(mysqldump) | binlog |
|------|---------------------------|---------------------|--------|
| 备份对象 | 数据文件(页) | SQL 语句 | 改库事件 |
| 速度 | 快(拷文件) | 慢(走 SQL) | 持续记录 |
| 体积 | 大(原页) | 中(文本) | 中(看格式) |
| 一致性机制 | redo 重放(承 P3-12) | MVCC read view(承 P4-13) | 2PC(承 P3-11) |
| 跨版本 | 差(同版本) | 好 | 好 |
| 主要用途 | 全量备份/快速恢复 | 小库/迁移/升级 | 增量/point-in-time/复制 |

**生产标准组合**:**每日物理全备(xtrabackup/clone)+ binlog 持续归档 + 每周/月逻辑备(留一份跨版本兜底)**。恢复时物理全备打底 + binlog 重放到故障前一刻。

> **和 P3-12 的关系**:物理备份的"prepare"阶段,就是 P3-12 crash recovery 的子集——重放 redo 让页一致。clone 插件更是直接复用 InnoDB 的页拷贝和 redo。**懂了 P3-08/12,就懂了物理备份凭什么 sound**。

---

## 四、与前作的承接:一张对照表打通

读过《PostgreSQL 数据库内核》《LevelDB》《Linux内存管理》《TiKV》的读者,InnoDB 的很多概念你早见过"同源不同形"的版本。**下表帮你把 InnoDB 的工具/概念,快速映射到前作**——同一个思想,在不同系统里换了身衣服。

| InnoDB 概念/工具 | 承接前作 | 同源思想的差异点 |
|----------------|---------|----------------|表就是索引(P1-02) | 表和索引分离(PG heap + 二级索引指向行) | 两种"表怎么存"的根本范式;主键查一跳 vs 两跳 |
| `explain` 看执行计划 | `EXPLAIN`(PG) | 两者语法/输出很像(PG 的 `EXPLAIN ANALYZE` 和 MySQL 8.0+ 的同名);MySQL 多 `key_len`/`Extra` 列 |
| redo log(WAL) | WAL(PG)、WAL/Manifest(LevelDB)、Raft log(TiKV) | 都是"先记日志再动手";InnoDB redo 是**物理**(页偏移),PG/TiKV 是**逻辑**(行变更),LevelDB WAL 是 raw record |
| undo + MVCC 版本链 | MVCC(PG xmin/xmax)、MVCC(TiKV key+TS) | InnoDB 用 undo 串版本链;PG 用 xmin 在行头标记;TiKV 在 key 上加多版本写 RocksDB |
| purge 清理旧版本 | VACUUM(PG)、GC(TiKV) | 都清"没人再需要的旧版本";InnoDB purge 顺 undo 链、PG autovacuum 扫表、TiKV GC 扫写 CF |
| buffer pool | page cache(《Linux mm》) | InnoDB 自管 buffer pool(改进 LRU midpoint),Linux page cache 内核管;InnoDB 一般 `O_DIRECT` 绕过 page cache |
| 两阶段提交(2PC) | 2PC(TiKV Percolator PD)、两阶段(分布式系统) | InnoDB 2PC 是 redo+binlog 单机内;TiKV Percolator 是跨 Region 分布式 |
| 行锁/间隙锁 | 行锁(PG)、分布式锁(TiKV) | InnoDB 间隙锁锁"不存在的间隙"(P5-17),PG 无间隙锁(用 Serializable SSI)、TiKV 用单 key 锁 + Percolator primary |
| `SHOW ENGINE INNODB STATUS` | `pg_stat_activity`/`pg_locks`(PG) | InnoDB 一把抓文本 vs PG 一张张系统视图 |
| 物理备份(xtrabackup/clone) | `pg_basebackup`(PG) | 两者都是物理拷 + 一致性靠 WAL;clone 是 Oracle 原生 |

**一句话**:InnoDB 的"WAL/redo"思想,在 PG/TiKV/LevelDB 里各有一个同源的兄弟;InnoDB 的"MVCC undo 链"思想,在 PG 的 xmin 和 TiKV 的多版本里各有一版;InnoDB 的"buffer pool",就是 Linux page cache 的 InnoDB 自管版。**读过前作再看 InnoDB 的工具,会发现底层思想是相通的,只是观测命令和实现细节不同。**

---

## 五、★线上问题排查清单(实用重头)

这一节是本附录的核心:**把线上最常见的六类问题,各给一套"症状 → 命令 → 怎么读输出 → 对应哪章原理"的排查清单。** 遇到问题直接翻这里。

### (一)死锁怎么排查

**症状**:应用报错 `ERROR 1213 (40001): Deadlock found when trying to get lock; try restarting transaction`,某条事务被 InnoDB 主动回滚。

**原理**(P5-18):两阶段锁协议下,两个事务互相等对方的锁,形成环。InnoDB 的 wait-for graph 检测到环,挑 undo 量小的事务回滚(代价最小)。

**排查步骤**:

1. **立刻抓死锁现场**。InnoDB 把最近一次死锁详情存在 `SHOW ENGINE INNODB STATUS` 里,但它**只保留最近一次**,且重启就丢。线上要打开"每次死锁都记日志":

```sql
SHOW VARIABLES LIKE 'innodb_print_all_deadlocks';  -- 看是否开启
SET GLOBAL innodb_print_all_deadlocks = ON;         -- 开启:每次死锁都写错误日志
```

开之后,每次死锁都会写进 MySQL error log,不会丢。

2. **读死锁日志**:

```sql
SHOW ENGINE INNODB STATUS\G
```

找 `LATEST DETECTED DEADLOCK` 节,里面长这样(简化示意):

```
*** (1) TRANSACTION:
TRANSACTION 12345, ACTIVE 2 sec starting index read
mysql tables in use 1, locked 1
LOCK WAIT 3 lock struct(s), heap size 1136, 2 row lock(s)
MySQL thread id 8, OS thread handle ..., query id 100 localhost root updating
UPDATE account SET bal=bal-100 WHERE id=10

*** (1) WAITING FOR THIS LOCK TO BE GRANTED:
RECORD LOCKS space id 50 page no 3 n bits 72 index PRIMARY of table `mydb`.`account`
trx id 12345 lock_mode X locks rec but not gap waiting

*** (2) TRANSACTION:
TRANSACTION 12346, ACTIVE 1 sec starting index read
...
UPDATE account SET bal=bal+50 WHERE id=20
*** (2) HOLDS THE LOCK(S):
RECORD LOCKS ... index PRIMARY ... trx id 12346 lock_mode X locks rec but not gap
*** (2) WAITING FOR THIS LOCK TO BE GRANTED:
RECORD LOCKS ... index PRIMARY ... trx id 12345 ... waiting
*** WE ROLL BACK TRANSACTION (2)
```

**怎么读**:
- `*** (1) TRANSACTION` 和 `*** (2) TRANSACTION`:死锁的两个事务,各自跑什么 SQL(`UPDATE ... WHERE id=10` / `WHERE id=20`);
- `WAITING FOR THIS LOCK`:各自等什么锁——注意 `lock_mode X locks rec but not gap`(记录锁,承 P5-16)、`X locks gap before rec`(间隙锁,承 P5-17)、`X`(next-key 锁);
- `index PRIMARY of table`:锁在哪个表哪个索引;
- `*** WE ROLL BACK TRANSACTION (2)`:InnoDB 选了哪个事务回滚(挑 undo 量小的,承 P5-18)。

3. **结合 `data_locks` 看当前还残留的锁**(死锁已被解开,但想确认当时的锁形态,可复现):

```sql
SELECT ENGINE_LOCK_ID, ENGINE_TRANSACTION_ID, LOCK_MODE, LOCK_TYPE,
       INDEX_NAME, OBJECT_SCHEMA, OBJECT_NAME
FROM performance_schema.data_locks
WHERE OBJECT_NAME = 'account';
```

4. **定位根因**:死锁日志会告诉你两个事务"在哪个索引上加锁的顺序冲突了"。常见根因:① **两个事务以不同顺序更新同一批行**(调整成统一顺序即可解);② **间隙锁在 RR 下互相阻塞**(P5-17,改成 RC 或缩小事务);③ **唯一性检查/外键的隐式锁**。

> **对应正文**:P5-16(行锁/两阶段锁)、P5-17(间隙锁/临键锁)、P5-18(wait-for graph 检测、回滚 undo 小的)。

### (二)慢查询怎么定位

**症状**:某条 SQL 慢、CPU/IO 飙高、应用超时。

**原理**(P6-21):慢查询根因无非三类——① **没走索引**(全表扫);② **走了索引但回表多**(没覆盖);③ **索引选错**(优化器估算偏差)。

**排查步骤**:

1. **开慢查询日志,抓慢 SQL**:

```sql
SHOW VARIABLES LIKE 'slow_query_log%';          -- 是否开、文件在哪
SET GLOBAL slow_query_log = ON;
SET GLOBAL long_query_time = 1;                  -- 超过 1 秒记
SET GLOBAL log_queries_not_using_indexes = ON;   -- 没用索引的也记
```

慢日志里会记每条慢 SQL、耗时、扫描行数、是否用索引。

2. **拿到慢 SQL,跑 `explain`**(承 P6-21):

```sql
EXPLAIN SELECT ... ;
-- 8.0+ 跑实际执行:
EXPLAIN ANALYZE SELECT ... ;
```

看 `type`(全表扫 `ALL`?)、`key`(用了哪个索引?)、`rows`(估算扫多少行?)、`Extra`(`Using filesort`/`Using temporary`?)。

3. **按 SQL 指纹聚合,找最该优化的**(p_s):

```sql
SELECT DIGEST_TEXT,
       COUNT_STAR AS exec_count,
       ROUND(AVG_TIMER_WAIT/1000000000, 2) AS avg_ms,
       SUM_ROWS_EXAMINED,
       SUM_ROWS_SENT,
       ROUND(SUM_ROWS_EXAMINED/NULLIF(SUM_ROWS_SENT,0), 1) AS rows_examined_per_sent
FROM performance_schema.events_statements_summary_by_digest
ORDER BY SUM_TIMER_WAIT DESC
LIMIT 10;
```

`rows_examined_per_sent` 大(扫了几万行只返回几行)= 索引没选好;`avg_ms` 大 = 单次慢。

4. **常见解法**:① 加合适索引(`WHERE`/`JOIN`/`ORDER BY` 的列);② 用覆盖索引免回表(P1-03);③ 强制/提示索引(`FORCE INDEX`,承 P6-21 索引选择);④ 改 SQL(避免 `SELECT *`、避免前置函数包列、避免隐式类型转换)。

> **对应正文**:P1-03(回表/覆盖索引)、P6-21(explain/索引选择/慢查询)。

### (三)锁等待怎么查

**症状**:某条 SQL 一直卡着不动(超过 `innodb_lock_wait_timeout` 默认 50 秒后被 InnoDB 中断,报 `Lock wait timeout exceeded`)。

**原理**(P5-16/18):事务 A 持着某行的 X 锁不释放(两阶段锁,要等 commit),事务 B 要改同一行,就在 `data_lock_waits` 里等。

**排查步骤**:

1. **实时抓"谁等谁"**(本附录第一节给过):

```sql
SELECT r.trx_id AS waiting_trx, r.trx_query AS waiting_sql,
       b.trx_id AS blocking_trx, b.trx_query AS blocking_sql,
       r.trx_started AS wait_since
FROM performance_schema.data_lock_waits w
JOIN information_schema.INNODB_TRX b ON b.trx_id = w.blocking_engine_transaction_id
JOIN information_schema.INNODB_TRX r ON r.trx_id = w.requesting_engine_transaction_id;
```

`blocking_sql` 就是"卡别人"的那条事务的当前 SQL——它往往是元凶(改了一行没 commit,锁住了一片)。

2. **看 blocking 事务的完整状态**(它可能跑了很久、持锁一堆):

```sql
SELECT trx_id, trx_state, trx_started,
       TIMESTAMPDIFF(SECOND, trx_started, NOW()) AS run_sec,
       trx_rows_locked, trx_rows_modified, trx_query
FROM information_schema.INNODB_TRX
ORDER BY trx_started;
```

`run_sec` 大 + `trx_rows_locked` 大 = 长事务持大量锁,典型元凶。

3. **决定怎么办**:① 让业务方 commit/rollback blocking 事务;② 实在不行 `KILL <blocking_thread>`(释放它的所有锁);③ 根因往往是"事务太长/没及时 commit"(承 P5-16 两阶段锁,commit 才放锁)。

4. **调超时**(治标):

```sql
SHOW VARIABLES LIKE 'innodb_lock_wait_timeout';  -- 默认 50 秒
SET innodb_lock_wait_timeout = 30;               -- 单位秒
```

> **对应正文**:P5-16(行锁、两阶段锁协议)、P5-18(锁等待队列、innodb_lock_wait_timeout)。

### (四)buffer pool 命中率 / IO bound 判断

**症状**:数据库响应变慢、IO 利用率高、QPS 上不去。

**原理**(P2-05/06):buffer pool 命中率低 = 频繁打磁盘 = 内存不够缓存热页。

**排查步骤**:

1. **看命中率**:

```sql
SHOW ENGINE INNODB STATUS\G
-- 找 "Buffer pool hit rate N / 1000"
```

`1000 / 1000` 满分;`< 995 / 1000`(即 < 99.5%)就要警惕;< 990 / 1000 通常意味着 IO bound。

或结构化查:

```sql
SELECT name, count
FROM information_schema.INNODB_METRICS
WHERE name IN ('buffer_pool_size', 'buffer_pool_pages_total', 'buffer_pool_pages_free',
               'buffer_pool_read_requests', 'buffer_pool_reads');
-- 命中率 = 1 - buffer_pool_reads / buffer_pool_read_requests
```

2. **看大小够不够**:

```sql
SHOW VARIABLES LIKE 'innodb_buffer_pool_size';
-- 经验:专用服务器给到物理内存的 50~75%
```

3. **看 buffer pool 都缓存了什么**(承 P2-05,本附录第一节):

```sql
SELECT PAGE_TYPE, COUNT(*) FROM information_schema.INNODB_BUFFER_PAGE GROUP BY PAGE_TYPE;
```

4. **常见解法**:① 加大 `innodb_buffer_pool_size`;② 看 buffer pool 实例数(`innodb_buffer_pool_instances`,承 P2-05 多实例拆锁);③ 看是不是有全表扫在冲刷热页(承 P2-06 midpoint insertion 防冲刷)。

> **对应正文**:P2-05(buffer pool/free/LRU/flush)、P2-06(midpoint insertion、young/old)。

### (五)undo / purge 滞后

**症状**:`History list length` 持续涨、undo 表空间越来越大、空间不回收、某张表的旧版本堆积。

**原理**(P4-15):MVCC 的 undo 版本链,要等"没有事务还需要旧版本"才能被 purge 清理。如果有长事务持着 read view 不放,所有比它新的旧版本都不能 purge。

**排查步骤**:

1. **看 `History list length`**:

```sql
SHOW ENGINE INNODB STATUS\G
-- TRANSACTIONS 节:"History list length N"
```

健康值看业务(几百~几千正常);持续涨到几十万上百万 = purge 跟不上。

2. **找长事务**(它们持着 read view 挡 purge):

```sql
SELECT trx_id, trx_state, trx_started,
       TIMESTAMPDIFF(SECOND, trx_started, NOW()) AS run_sec,
       trx_query
FROM information_schema.INNODB_TRX
WHERE TIMESTAMPDIFF(SECOND, trx_started, NOW()) > 60
ORDER BY trx_started;
```

`run_sec` 大的就是元凶——它的 read view 把 purge 卡住了。

3. **看 undo 表空间**:

```sql
SHOW VARIABLES LIKE 'innodb_undo_tablespaces';
SHOW VARIABLES LIKE 'innodb_undo_log_truncate%';  -- 8.0 undo 自动 truncate
SELECT * FROM information_schema.INNODB_TABLESPACES
WHERE SPACE_TYPE = 'Undo';  -- (字段名以实际为准)
```

4. **常见解法**:① 杀长事务(最直接);② 业务侧控制事务长度(承 P5-16,事务越短锁越少、挡 purge 越短);③ 8.0 开 `innodb_undo_log_truncate=ON` 让 undo 段自动回收。

> **对应正文**:P3-10(undo log)、P4-13/14(read view)、P4-15(purge 与 read view)。**承接《TiKV》GC**:同源思想——都是清"没人再需要的旧版本",InnoDB purge 顺 undo 链、TiKV GC 扫写 CF。

### (六)crash recovery 失败:起不来库怎么办

**症状**:机器断电/进程被 kill 后,MySQL 重启卡在"InnoDB recovery",或者直接起不来、报页损坏。

**原理**(P3-12):正常启动会跑 `doublewrite 修复 → redo 重放 → 2PC 裁决 → undo 回滚`。如果 redo 或数据页严重损坏(磁盘坏块、文件被截断、内核 panic 时文件系统损坏),正常 recovery 过不去。

**排查步骤**:

1. **看 error log**,定位卡在哪一步。常见错误:页 checksum 错、redo 文件读不了、undo 段损坏。

2. **用 `innodb_force_recovery` 强制启动**(最后的手段)。这是 6 级开关,**级越高越激进、牺牲越多一致性**,源码定义在 `storage/innobase/include/srv0srv.h`(枚举 `srv_force_recovery`):

| 级别 | 常量名(`srv0srv.h`) | 含义 | 牺牲 | 能做什么 |
|------|---------------------|------|------|---------|
| 0(默认) | — | 正常 recovery,全部做 | 不牺牲 | 正常情况 |
| 1 | `SRV_FORCE_IGNORE_CORRUPT` | 检测到坏页也继续跑 | 跳过坏页(可能丢数据) | 能 `SELECT` 出大部分表 |
| 2 | `SRV_FORCE_NO_BACKGROUND` | 不跑主线程(防 purge 崩溃) | 后台任务停 | 进一步提高存活 |
| 3 | `SRV_FORCE_NO_TRX_UNDO` | recovery 后不回滚事务 | 未提交事务不回滚(违反 A) | 起得来 |
| 4 | `SRV_FORCE_NO_IBUF_MERGE` | 不做 change buffer 合并 | 二级索引可能暂时不一致 | 更激进 |
| 5 | `SRV_FORCE_NO_UNDO_LOG_SCAN` | 不扫 undo(把未提交当已提交) | 严重不一致 | 几乎只为能起 |
| 6 | `SRV_FORCE_NO_LOG_REDO` | 不做 redo roll-forward | 不补已提交的修改(违反 D) | 最后手段,只为把数据 dump 出来 |

**操作流程**:

```ini
# my.cnf 里加(从最低级试,不行再加一级)
[mysqld]
innodb_force_recovery = 1
```

重启,起得来就:

```bash
# 立刻逻辑导出所有能读的表(mysqldump),然后重建库
mysqldump --single-transaction --all-databases > rescue.sql
```

**起不来就 `innodb_force_recovery = 2` → 3 → ... 逐级加,直到起来。** 一旦起来,**只做 dump,不做任何写操作**(高级别下数据库不一致,写会加剧损坏)。

3. **dump 完后**:去掉 `innodb_force_recovery`,删掉损坏的数据目录,用最近的备份恢复 + binlog 重放,再把 rescue.sql 里的数据补回去。

> **对应正文**:P3-12(doublewrite 修复、redo 重放、2PC 裁决、undo 回滚)。`innodb_force_recovery` 本质是"逐级跳过这四步里的某一步,只为能起来 dump 数据"。

> **钉死这件事**:`innodb_force_recovery > 0` 是**灾难恢复**,不是日常运维。一旦用它,数据库就不保证 ACID 了——目标是"把数据救出来",救出来后必须重建库。**平时靠备份+binlog,不要指望 force recovery。**

### (七)排查速查总表

把六类问题的"第一步命令"汇总,贴墙上:

| 症状 | 第一步命令 | 对应正文 |
|------|-----------|---------|
| 死锁(1213) | `SHOW ENGINE INNODB STATUS\G` 找 `LATEST DETECTED DEADLOCK` | P5-18 |
| 慢查询 | `EXPLAIN <sql>` + 慢日志 | P6-21 |
| 锁等待(1205) | 查 `data_lock_waits` + `INNODB_TRX` | P5-16/18 |
| 命中率低/IO 高 | `SHOW ENGINE INNODB STATUS` 看 `hit rate` | P2-05/06 |
| undo/purge 涨 | 看 `History list length` + 找长事务 | P4-15 |
| crash 起不来 | error log → `innodb_force_recovery` 逐级 | P3-12 |

---

## 六、附:常用观测命令速查卡

最后给一张"日常巡检"命令卡,把最常用的几条汇总(每条都回扣正文):

```sql
-- 0. 全貌(体检报告)
SHOW ENGINE INNODB STATUS\G

-- 1. 活跃事务(找长事务)
SELECT trx_id, trx_state, trx_started,
       TIMESTAMPDIFF(SECOND, trx_started, NOW()) AS sec,
       trx_rows_locked, trx_query
FROM information_schema.INNODB_TRX
ORDER BY trx_started;

-- 2. 当前锁与等待
SELECT * FROM performance_schema.data_locks;
SELECT * FROM performance_schema.data_lock_waits;

-- 3. buffer pool 命中与脏页
SELECT name, count FROM information_schema.INNODB_METRICS
WHERE name LIKE 'buffer_pool%';

-- 4. redo 进度(LSN 差 = 恢复要重做的量)
SHOW ENGINE INNODB STATUS\G   -- 找 LOG 节

-- 5. undo/purge 滞后
SHOW ENGINE INNODB STATUS\G   -- 找 History list length

-- 6. 执行计划
EXPLAIN SELECT ...;
EXPLAIN ANALYZE SELECT ...;   -- 8.0+

-- 7. 慢查询聚合(p_s)
SELECT DIGEST_TEXT, COUNT_STAR, ROUND(AVG_TIMER_WAIT/1e9,2) AS avg_ms
FROM performance_schema.events_statements_summary_by_digest
ORDER BY SUM_TIMER_WAIT DESC LIMIT 10;

-- 8. 关键变量
SHOW VARIABLES LIKE 'innodb_buffer_pool_size';
SHOW VARIABLES LIKE 'innodb_lock_wait_timeout';
SHOW VARIABLES LIKE 'innodb_force_recovery';
SHOW VARIABLES LIKE 'innodb_undo_log_truncate';
```

---

## 小结:工具是原理的延伸

本附录把 InnoDB 的"实践面"摊开了:三圈观测工具(`SHOW STATUS` / `i_s+p_s` / 磁盘工具)、三套备份(物理 / 逻辑 / binlog)、六类线上问题排查清单。**但全篇从头到尾都在做一件事:把正文 23 章的原理,接到真实能跑的命令上。**

- 你读懂了 P2-05 的 buffer pool,`Buffer pool hit rate 990 / 1000` 才不是无意义数字;
- 你读懂了 P3-08/12 的 WAL,xtrabackup 的 `--prepare` 才不是黑魔法;
- 你读懂了 P5-16/17 的锁,死锁日志里的 `lock_mode X locks rec but not gap` 才能一眼看穿;
- 你读懂了 P4-15 的 purge,`History list length` 涨才不会让你慌而不知道找长事务。

> **一句话**:InnoDB 的工具不是独立的一套"运维技能",而是**四个灵魂(B+树/WAL/MVCC/锁)在观测层面的投影**。读完正文懂原理、读完本附录会用工具,你就真正把 InnoDB 从黑盒变成了白盒——能在脑子里放映一条 UPDATE 的全过程,也能在线上用几条命令定位到是哪一环卡住了。

> **想继续深入往哪钻**:
> - 官方文档:"InnoDB INFORMATION Schema Tables"、"Performance Schema"、"The InnoDB Storage Engine / Monitoring";
> - 工具源码:`utilities/ibd2sdi.cc`、`storage/innobase/handler/i_s.cc`、`handler/p_s.cc`、`plugin/clone/src/clone_plugin.cc`、`client/mysqldump.cc`(都在本书源码树 `mysql-server/` 里);
> - 备份工具:Percona XtraBackup 文档、MySQL clone 插件文档;
> - 第三方:`innochecksum` 详解、社区 `innodb_ruby`/`innodb-diagnostic` 工具(看页二进制)。
