# 第 6 篇 · 第 21 章 · 索引调优与 explain

> **核心问题**:你在 P1 篇已经知道了——InnoDB 的表就是一棵主键 B+树(聚簇索引)、二级索引是另一棵 B+树(叶子页存主键值)、按非主键查要回表、覆盖索引能免回表。可真到线上,你 `EXPLAIN SELECT ...`,面对 `type`、`key`、`rows`、`Extra` 这一堆字段,你能讲清"B+树到底怎么走的"吗?更扎心的是:**你明明在 `status` 上建了索引,优化器偏偏不走它,改走全表扫;你 `ANALYZE TABLE` 之后 `cardinality` 跳了一截;`rows` 估算有时差一个数量级**——为什么?这一章,把 explain 这一纸执行计划,从优化器怎么填它、到 InnoDB 怎么给优化器喂数据、再到怎么据此排查慢查询,沿着 P1 讲透的 B+树原理,从"观测和调优"的角度再过一遍。

> **读完本章你会明白**:
> 1. **explain 的每一列在源码里从哪来**:`type` 是 [`enum join_type`](../mysql-server/sql/sql_opt_exec_shared.h#L184)(`JT_SYSTEM`/`JT_CONST`/`JT_EQ_REF`/`JT_REF`/`JT_RANGE`/`JT_INDEX_SCAN`/`JT_ALL` 等),`Extra` 的每一句话是 [`traditional_extra_tags[]`](../mysql-server/sql/opt_explain_traditional.cc#L47) 里的一个枚举常量(`Using index`/`Using filesort`/`Using temporary`/`Using where`/`Using index condition`),`rows` 是优化器拿 InnoDB 给的统计 + [`records_in_range`](../mysql-server/storage/innobase/handler/ha_innodb.cc#L16936) 算出来的。
> 2. **InnoDB 怎么给优化器喂统计**:表级行数 `stat_n_rows`、每个索引的 `n_diff_key_vals[]`(cardinality)由 [`dict0stats.cc`](../mysql-server/storage/innobase/dict/dict0stats.cc) 在 `ANALYZE TABLE` 时**采样 N 个叶子页**估出来(默认 `srv_stats_persistent_sample_pages = 20`);范围行数由 [`btr_estimate_n_rows_in_range_low`](../mysql-server/storage/innobase/btr/btr0cur.cc#L5039) 用"两条 dive 路径分叉点处的页内记录数"外推。
> 3. **为什么有时不选你建的索引**:优化器按 cost 选,InnoDB 在 [`info_low_key`](../mysql-server/storage/innobase/handler/ha_innodb.cc#L17660) 里把 `rec_per_key` **故意除以 2**("MySQL 太偏袒全表扫,我们把索引选择性说成 2 倍好"),又在 [`scan_time`](../mysql-server/storage/innobase/handler/ha_innodb.cc#L17103) 里把全表扫成本**故意不除以 10**(物理上顺序读应该比随机读快约 10 倍)——这两道反向补偿,决定了"索引选不选"的边界。
> 4. **回表/覆盖/最左前缀在 explain 里长什么样**:`Extra: Using index` 就是覆盖索引(承接 P1-03 的 `need_to_access_clustered == false`),它由 server 层 [`covering_keys`](../mysql-server/sql/table.cc#L6094) 这个 bitmap 判定;`key_len` 能反推用了联合索引的前几列(最左前缀命中多少);`type` 从 `const` 到 `ALL` 是"B+树定位精度从一击命中到全扫"的连续光谱。
> 5. 一份**慢查询排查清单**:从 `EXPLAIN` → `EXPLAIN FORMAT=TREE` → `EXPLAIN ANALYZE`(9.x 真跑一遍给真实行数)→ `ANALYZE TABLE` → 看是不是统计过期 / 选错索引 / 回表过多 / filesort,逐层定位。

> **逃生阀**:如果一读觉得难,先记四件事——① explain 的 `rows` 是**估算**不是真值,根子在 InnoDB 采样统计;② `Extra: Using index` = 覆盖索引免回表,`Using filesort`/`Using temporary` = 要优化;③ 优化器按 cost 选索引,不是"有索引就一定走";④ `EXPLAIN ANALYZE`(8.0+)会真跑一遍、给真实 rows,排查慢查询比普通 `EXPLAIN` 香得多。

---

## 〇、一句话点破

> **explain 不是玄学,它是一张"优化器怎么决定走哪条 B+树路径"的体检报告——`type` 说定位精度、`key` 说走哪棵索引树、`rows` 说估算扫多少行、`Extra` 说有没有额外开销(回表/排序/临时表)。`rows` 之所以是估算,是因为 InnoDB 给优化器的统计是采样来的(默认就采 20 个叶子页);优化器之所以有时不选你建的索引,是因为它按 cost 选,而 cost 模型里 InnoDB 故意偏袒索引、server 又故意偏袒全表扫,两股劲儿在拔河。看懂这张报告,你就能讲清"B+树到底怎么走的、走得对不对"。**

这是结论,不是理由。本章倒过来拆:先把 explain 每一列从源码里抠出来,告诉你它在说什么;再讲 InnoDB 怎么给优化器喂统计(这是 `rows`/`cardinality` 的根,也是"估算不准"的根);然后讲优化器怎么按 cost 选索引、为什么 InnoDB 要在统计里"动手脚";接着把回表/覆盖/最左前缀在 explain 里的样子对应到 P1 的 B+树原理;最后给一份慢查询排查清单。

> **承接 P1**:本章不重讲聚簇索引、二级索引、回表、覆盖索引的"是什么"——那是 P1-02/03 的主角。本章只讲"**怎么观测、为什么有时观测结果和直觉不符、怎么据此调优**"。B+树怎么存、回表的源码路径(`row_search_mvcc` → `row_sel_get_clust_rec_for_mysql`),本章直接引用 P1-03 的结论,不重复。承接《PostgreSQL 数据库内核》那本讲的 SQL 优化器(`best_access_path`/cost model),本章只点到 InnoDB 给优化器的接口,不重讲优化器内部。

---

## 一、explain 的每一列,在源码里从哪来

很多人把 explain 当"查个 type 看看走没走索引"的速查表。这远远不够。explain 输出的每一列,背后都对应源码里一个具体的填充点。搞清楚"这一列是哪个函数、依据什么填的",你才能判断它的**可信度边界**——哪些列是事实、哪些列是估算、哪些列会撒谎。

### 一张表:explain 12 列对应的源码出处

标准 `EXPLAIN`(传统表格格式)输出这 12 列:

| 列 | 含义 | 源码出处(填这一列的地方) | 是事实还是估算 |
|----|------|--------------------------|----------------|
| `id` | 查询里第几个 SELECT(子查询/UNION 各有 id) | `Query_block::id` | 事实 |
| `select_type` | 这个 SELECT 的类型(SIMPLE/PRIMARY/SUBQUERY/DERIVED/UNION) | [`opt_explain.cc`](../mysql-server/sql/opt_explain.cc) 里 `select_type` 枚举 | 事实 |
| `table` | 表名(或别名 `<derived2>`) | `TABLE_LIST` | 事实 |
| `partitions` | 命中的分区 | 分区裁剪结果 | 事实 |
| **`type`** | **访问类型(定位精度)** | **[`enum join_type`](../mysql-server/sql/sql_opt_exec_shared.h#L184),由 `best_access_path` 决定** | **事实(决定走法)** |
| `possible_keys` | 优化器觉得"可能能用"的索引 | WHERE 里列 → 索引前缀匹配 | 事实 |
| **`key`** | **实际选用的索引** | **`best_access_path` 比完 cost 后胜出的那个** | **事实** |
| **`key_len`** | **选用索引用了多少字节(= 用了前几列)** | **按选中 key 的字段长度累加** | **事实(反推最左前缀命中几列)** |
| **`ref`** | **`type=ref/eq_ref` 时,和索引比较的来源(常量/某表的列/func)** | **`Key_use` 里的来源** | **事实** |
| **`rows`** | **估算"要扫多少行才能拿到结果"** | **优化器 = `records` × filter;`records` 来自 InnoDB [`info()`](../mysql-server/storage/innobase/handler/ha_innodb.cc#L17766) 或 [`records_in_range()`](../mysql-server/storage/innobase/handler/ha_innodb.cc#L16936)** | **估算!最易翻车的一列** |
| `filtered` | 经 WHERE 过滤后剩百分之几 | 条件过滤估算 `calculate_condition_filter` | 估算 |
| **`Extra`** | **额外信息(覆盖/排序/临时表/下推)** | **[`traditional_extra_tags[]`](../mysql-server/sql/opt_explain_traditional.cc#L47) 枚举数组** | **事实** |

读这张表,有三件事要先钉死:

1. **`type`、`key`、`key_len`、`ref`、`Extra` 是事实**——它们描述"优化器决定怎么走",是确定的。你看到 `type=ALL` 就是全表扫,没有歧义。
2. **`rows`、`filtered`、`possible_keys` 里的"可能用哪个"是估算**——尤其 `rows`,是优化器拿 InnoDB 给的统计 + 范围估算算出来的,**可能严重偏离真实**。本章第三节会拆为什么。
3. **`key_len` 是被严重低估的利器**:它能反推"联合索引命中了前几列"。`idx_a_b_c(a, b, c)`,如果 `key_len = a 的字节 + b 的字节`,说明 a、b 都用上了,c 没用上(可能因为 `WHERE a=? AND b>?`,c 在范围条件后用不上 B+树有序性)。这是排查"为什么联合索引只走了一半"的关键。

### `type` 列:从 const 到 ALL 的定位精度光谱

`type` 列是 explain 里信息量最大的一列。它来自 [`enum join_type`](../mysql-server/sql/sql_opt_exec_shared.h#L184),从"一击命中"到"全扫"是一个连续光谱:

```
   type 光谱(B+树定位精度从高到低):

   system > const > eq_ref > ref > fulltext > ref_or_null
       > index_merge > unique_subquery > index_subquery
       > range > index > ALL
     ┊         ┊         ┊        ┊         ┊       ┊       ┊
   单行     PK/唯一   唯一索引  非唯一索引   范围   扫索引树  全表扫
   常数化   等值命中   等值      等值        扫描   叶子页    (最慢)
```

每一档的含义、源码、典型场景,用一张表钉死:

| `type` | [`JT_*`](../mysql-server/sql/sql_opt_exec_shared.h#L184) | 含义 | B+树怎么走 | 典型 SQL |
|--------|----------|------|-----------|----------|
| `system` | `JT_SYSTEM` | 表只有一行(系统表) | 直接读 | `SELECT * FROM mysql.proxies_priv` |
| `const` | `JT_CONST` | 按**主键或唯一索引**等值查,最多一行;MySQL 把它当常数 | 聚簇/唯一 B+树一次定位 | `WHERE id=10`(id 是 PK) |
| `eq_ref` | `JT_EQ_REF` | 按**唯一索引**等值查,每次最多一行(用于 JOIN 的被驱动表) | 唯一 B+树一次定位 | `JOIN b ON a.id=b.id`(b.id 唯一) |
| `ref` | `JT_REF` | 按**非唯一索引**等值查,每次若干行 | 二级 B+树定位 → 可能回表 | `WHERE name='b'`(name 非唯一索引) |
| `range` | `JT_RANGE` | 索引范围扫(`>`, `<`, `BETWEEN`, `IN`) | 二级 B+树定位边界,沿叶子链表扫 | `WHERE id BETWEEN 1 AND 100` |
| `index` | `JT_INDEX_SCAN` | 扫**整棵索引树**的叶子页(不查内部节点) | 顺着叶子页链表从头扫到尾 | `COUNT(*)`(覆盖时) |
| `ALL` | `JT_ALL` | **全表扫**——扫聚簇索引所有叶子页 | 顺着聚簇索引叶子链表从头扫 | 没索引可用,或优化器算下来全表扫更便宜 |

> **钉死一件事**:`type` 的本质是"B+树定位精度"。`const`/`eq_ref` 是"在 B+树里走一次就能命中唯一行";`ref` 是"走一次 B+树命中若干行";`range` 是"走 B+树定位边界 + 顺叶子链表扫一段";`index` 是"不查内部节点,直接扫叶子链表全表";`ALL` 是"扫聚簇索引所有叶子页"。**从 `const` 到 `ALL`,精度下降、扫描行数上升、性能变差**。生产 OLTP,`type` 至少要到 `range` 以上(`ref`/`eq_ref`/`const` 最好);看到 `ALL` 就要警惕(除非表很小或确实要扫全表如报表)。

### 一个关键澄清:`ALL` 在 InnoDB 里,扫的其实是聚簇索引

有个老说法:"`type=ALL` 是扫表、`type=index` 是扫索引"。这话在 MyISAM/PG 那种堆表引擎里对(表和索引是分离的);**但在 InnoDB 里,表 = 聚簇索引 B+树,没有独立的"堆"**。所以 InnoDB 的 `ALL`,物理上就是**顺着聚簇索引的叶子页链表,从头扫到尾**——它扫的就是"那棵主键 B+树"。

证据在 [`best_access_path`](../mysql-server/sql/sql_planner.cc#L983) 的一段注释([sql_planner.cc:1046-1054](../mysql-server/sql/sql_planner.cc#L1046)):

```
    Don't do a table scan on InnoDB tables, if we can read the used
    parts of the row from any of the used index.
    This is because table scans uses index and we would not win
    anything by using a table scan.
```

注释直白:**InnoDB 的全表扫本来就用索引(聚簇索引),所以如果能用某个索引覆盖,就别全表扫**。这正是 InnoDB 索引组织表(P1-02)的必然推论——"扫表"和"扫聚簇索引"在 InnoDB 里是同一件事,优化器对它们的处理也因此和堆表引擎不同。

### `Extra` 列:每一句话都是一个枚举

`Extra` 列不像 `type` 是单值,它是个"附加说明"列表,每句话来自 [`traditional_extra_tags[]`](../mysql-server/sql/opt_explain_traditional.cc#L47) 里的一个枚举常量。最重要的几句:

| `Extra` 值 | 枚举 | 含义 | 该怎么办 |
|-----------|------|------|----------|
| `Using index` | `ET_USING_INDEX` | **覆盖索引,免回表**(P1-03 讲的 `need_to_access_clustered==false`) | 很好,保持 |
| `Using where` | `ET_USING_WHERE` | server 层要再用 WHERE 过滤一遍(引擎返回的行不全符合) | 一般,看是不是该建索引 |
| `Using index condition` | `ET_USING_INDEX_CONDITION` | **ICP(Index Condition Pushdown)**,把 WHERE 的一部分下推到引擎层在索引上先过滤 | 好,减少回表 |
| `Using filesort` | `ET_USING_FILESORT` | ORDER BY 没法用索引的有序性,**得额外排序**(可能落临时文件) | **警告,要优化**(给排序列建索引) |
| `Using temporary` | `ET_USING_TEMPORARY` | 用了临时表(GROUP BY / DISTINCT / 某些 JOIN) | **警告,看能不能消除** |
| `Using join buffer` | `ET_USING_JOIN_BUFFER` | 用了 BNL(Block Nested Loop)join buffer | 一般,看 join 顺序 |
| `Range checked for each record` | `ET_RANGE_CHECKED_FOR_EACH_RECORD` | JOIN 时被驱动表没好索引,每行重新选 | **要建索引** |
| `Using MRR` | `ET_USING_MRR` | 多范围读(顺序化随机 IO) | 好 |
| `Backward index scan` | `ET_BACKWARD_SCAN` | 反向扫索引(ORDER BY ... DESC 且索引能用) | 好 |

> **钉死一件事**:`Extra` 里最该盯的两句是 **`Using filesort` 和 `Using temporary`**——它们意味着"除了 B+树扫描,还要额外干一大票活"。`Using filesort` 是 ORDER BY 没命中索引、得在内存/磁盘排序(`filesort.cc`,可能用 [`estimate_rows_upper_bound`](../mysql-server/storage/innobase/handler/ha_innodb.cc#L17056) 估上界);`Using temporary` 是 GROUP BY/DISTINCT 要落临时表。看到这两句,基本就是要优化的信号。反过来,**`Using index`(覆盖索引)是最想看到的**——它直接对应 P1-03 讲的"免回表"。

### `key_len`:反推联合索引用了几列

`key_len` 是个被忽视的列。它告诉你"选中的索引用了多少字节"。对联合索引 `idx_a_b_c(a INT, b INT, c INT)`,如果 `key_len = 4`,说明只用上了 a(INT 4 字节);`key_len = 8` 用上了 a+b;`key_len = 12` 三列全用上。

这能排查一类高频问题:**联合索引只命中了一半**。典型场景:

```sql
-- idx_a_b_c(a, b, c)
SELECT * FROM t WHERE a = 1 AND c = 3;        -- key_len=4!  b 没给值,c 用不上 B+树序
SELECT * FROM t WHERE a = 1 AND b = 2 AND c = 3; -- key_len=12,三列全用
SELECT * FROM t WHERE a = 1 AND b > 2;        -- key_len=8,  a 等值+b 范围,c 用不上
```

第一条 `WHERE a=1 AND c=3`——B+树按 `(a,b,c)` 排,知道 a=1 后,在 a=1 段里 b 是无序的(因为没给 b),c 自然也用不上。所以 `key_len=4`,只用上 a。这是 P1-03 讲的**最左前缀原则**在 explain 里的直接观测——`key_len` 就是"最左前缀命中到第几列"的字节化表达。

> **钉死一件事**:`key_len` = 选中索引的"已用前缀"字节长度。联合索引 `idx_a_b_c`,`key_len` 能反推用了前几列。用它排查"联合索引只走一半"的问题——尤其是"中间列没给值"(`a=? AND c=?`)和"等值后接范围"(`a=? AND b>?`,范围列之后用不上)。

---

## 二、`rows` 是怎么算出来的:InnoDB 给优化器喂的统计

`type`/`key`/`key_len`/`Extra` 都是事实,唯独 `rows` 是估算,而且经常不准。这是 explain 最容易让人困惑的地方——明明表里只有 100 行匹配,`rows` 显示 10000;或者反过来。要搞懂为什么,得钻进"InnoDB 给优化器喂什么数据"。

### 优化器需要两类数字

优化器要算 cost、决定走哪个索引,需要两类输入:

1. **表里大概有多少行**(`stat_n_rows`)——决定"全表扫要扫多少"。
2. **每个索引的每个前缀,有多少不同的值**(`n_diff_key_vals[]`,即 cardinality)——决定"按这个索引等值查,平均命中多少行"(`rec_per_key = n_rows / n_diff`)。
3. **范围查询时,这个范围里大概有多少行**——由 `records_in_range` 实时估。

前两类是"静态统计",在 `ANALYZE TABLE` 或后台线程算好、存起来;第三类是"动态估算",查询时实时算。

### 静态统计:采样 N 个叶子页,估 cardinality

InnoDB 怎么算 `n_diff_key_vals[]`?**不是扫全表**(那太慢),而是**采样 N 个叶子页**(默认 N=20)。入口在 [`dict0stats.cc`](../mysql-server/storage/innobase/dict/dict0stats.cc),核心是 [`dict_stats_analyze_index`](../mysql-server/storage/innobase/dict/dict0stats.cc#L2234)。采多少页由这个宏决定([dict0stats.cc:138-142](../mysql-server/storage/innobase/dict/dict0stats.cc#L138)):

```cpp
/* Gets the number of leaf pages to sample in persistent stats estimation */
#define N_SAMPLE_PAGES(index)                                    \
  static_cast<uint64_t>((index)->table->stats_sample_pages != 0  \
                            ? (index)->table->stats_sample_pages \
                            : srv_stats_persistent_sample_pages)
```

逻辑很直白:**如果表自己设了 `STATS_SAMPLE_PAGES`(建表时 `STATS_SAMPLE_PAGES=N`),用它;否则用全局 `srv_stats_persistent_sample_pages`(默认 20)**。也就是说,默认情况下,**InnoDB 只随机抽 20 个叶子页来估整个索引的 cardinality**。

> **不这样会怎样**:如果为了精确而扫全表所有叶子页,`ANALYZE TABLE` 在几十亿行的大表上要跑几分钟、阻塞业务。采样 20 页是性能和精度的折中——**对数据分布均匀的列,20 页足够准;对分布严重倾斜的列,20 页可能差出一个数量级**。这就是 explain `rows` 不准的头号根因。

`n_diff_key_vals[]` 算出来后,`rec_per_key`(每个键值平均多少行)在 [`innodb_rec_per_key`](../mysql-server/storage/innobase/handler/ha_innodb.cc#L17268) 里反推:

```cpp
// handler/ha_innodb.cc,第 17268-17283 行(简化示意)
rec_per_key_t innodb_rec_per_key(const dict_index_t *index, ulint i,
                                 ha_rows records) {
  ...
  if (records == 0) return (1.0);                 // 空表,返回 1 最方便优化器
  n_diff = index->stats.n_diff_key_vals[i];       // 第 i 个前缀的不同值数
  if (n_diff == 0) {
    rec_per_key = static_cast<rec_per_key_t>(records);  // 没统计,悲观假设全是同一个值
  } else if (...SRV_STATS_NULLS_IGNORED...) {
    ...                                            // NULL 处理分支
  } else {
    rec_per_key = records / n_diff;                // 核心公式:行数 / 不同值数
  }
  ...
}
```

核心就一行:`rec_per_key = records / n_diff`。一个索引前缀的不同值越多(`n_diff` 大),每个值平均命中的行就越少(`rec_per_key` 小),选择性越好、优化器越爱用。

### 动态估算:范围查询时,两条 dive 路径外推

`rec_per_key` 只对等值查询(`=`)有用。范围查询(`BETWEEN`、`>`、`IN`)呢?优化器调用 handler 接口 [`records_in_range`](../mysql-server/storage/innobase/handler/ha_innodb.cc#L16936),InnoDB 实时估算范围里有多少行。算法精妙且容易讲不清,但它是 explain `rows` 在范围查询下的直接来源,值得拆透。

InnoDB 的实现是 [`btr_estimate_n_rows_in_range_low`](../mysql-server/storage/innobase/btr/btr0cur.cc#L5039)。思路:**从 B+树根分别 dive 到范围的左右边界,记下两条路径;两条路径在某一层"分叉"——分叉点之前,两条路径走同一批页;分叉点之后,各走各的。在分叉那一层的页里,用"页内记录数"做比例外推**。

```
   范围 id BETWEEN 100 AND 500 的行数估算(B+树 dive 路径):

   根节点          [10, 50, 100, 500, 1000]           ← 两条路径都从根走
                       │            │
                     id=100       id=500
                       │            │
   第 1 层       ┌─────┴─────┐  ┌────┴─────┐
                 [页A: 80-200]  [页B: 400-600]      ← 在这层分叉!路径分别去 A 和 B
                       │            │
   叶子层         页 A: 100-200    页 B: 400-500
                 (n_recs=120)    (n_recs=80)

   估算逻辑:
   - 分叉点(第 1 层)的父页说:"我下面有 N 个叶子页,共 M 行"
   - 左边界在页 A 第 k1 条,右边界在页 B 第 k2 条
   - 比例外推:范围行数 ≈ (页 A 从 k1 到尾 + 中间整页 + 页 B 从头到 k2)
```

真实代码([btr0cur.cc:5039-5357](../mysql-server/storage/innobase/btr/btr0cur.cc#L5039))就是这个逻辑:维护两条 `btr_path_t` 数组(`path1`/`path2`),记录每一层走到哪个页的第几条记录(`nth_rec`)、那一层那个页有多少记录(`n_recs`)。两条路径从根同步往下走,一旦走到不同的页(`diverged = true`),就从分叉点开始用 `n_recs` 做外推。

这个算法快(只 dive 两条路径,不真扫范围),但**精度依赖 B+树页内记录数分布均匀**。如果范围恰好覆盖一个数据密集区,估算偏低;覆盖稀疏区,偏高。

### 一个会被坑的细节:0 行会被强制改成 1

[`records_in_range`](../mysql-server/storage/innobase/handler/ha_innodb.cc#L16936) 返回前,有一段极其重要的注释和代码([ha_innodb.cc:17039-17047](../mysql-server/storage/innobase/handler/ha_innodb.cc#L17039)):

```cpp
  /* The MySQL optimizer seems to believe an estimate of 0 rows is
  always accurate and may return the result 'Empty set' based on that.
  The accuracy is not guaranteed, and even if it were, for a locking
  read we should anyway perform the search to set the next-key lock.
  Add 1 to the value to make sure MySQL does not make the assumption! */

  if (n_rows == 0) {
    n_rows = 1;
  }
```

这段话翻译过来:**"MySQL 优化器相信 0 行的估算是绝对准确的,可能直接返回 Empty set。但我们的估算不保证准确,而且即使是锁定读,也得真扫一遍去加 next-key 锁。所以把 0 强制改成 1,别让 MySQL 犯懒。"**

这是个典型的"引擎层校正优化器过度信任"的细节。优化器看到 `rows=0` 会走捷径(短路返回空结果),InnoDB 怕它走错了(因为估算是猜的),故意给个 1。这种"引擎对优化器的防御性补偿",在 InnoDB 源码里不止这一处——下一节的"除以 2"和"不除以 10",是更大规模的同类补偿。

> **钉死一件事**:`rows` 估算的根在两个地方——① 静态统计 `n_diff_key_vals[]`(采样 20 页估,`ANALYZE TABLE` 刷新);② 动态 `records_in_range`(两条 dive 路径外推)。两者都是估算、都可能不准。**`rows` 不是真值,是优化器的体检报告里的一项"估计值"**——尤其在大表 + 数据倾斜时,可能差一个数量级。排查时要用 `EXPLAIN ANALYZE`(第四节)看真实行数对照。

### `ANALYZE TABLE` 刷新的就是这两个数

`ANALYZE TABLE t` 干的事,就是重新跑一遍 [`dict_stats_update_persistent`](../mysql-server/storage/innobase/dict/dict0stats.cc#L2288),重新采样 N 个叶子页,刷新 `stat_n_rows` 和 `n_diff_key_vals[]`,存进 `mysql.innodb_table_stats` / `mysql.innodb_index_stats` 两张系统表(8.0 持久化统计)。

一个常见现象:**大表删了一大半数据后,`EXPLAIN` 的 `rows` 还是老值**——因为统计没刷新,`stat_n_rows` 还是删之前的数。这时候 `ANALYZE TABLE` 一下,`rows` 就准了。这也是为什么 DBA 在"执行计划突然变差"时,第一反应往往是 `ANALYZE TABLE`。

> 一个版本细节:8.0 起 InnoDB 统计**默认持久化**(`innodb_stats_persistent=ON`),存在 `mysql` 库的两张表里;5.6 之前是内存的、重启就丢。你 `SHOW INDEX FROM t` 看到的 `Cardinality` 列,就来自 `n_diff_key_vals[0]`。如果它显示 `NULL` 或明显不对,八成是统计过期或采样不足(可以 `ANALYZE TABLE t STATS_SAMPLE_PAGES=200` 加大采样)。

---

## 三、为什么有时不选你建的索引:cost 模型 + InnoDB 的两道反向补偿

这是本章最扎心、也最常被误解的问题。你在 `status` 上建了索引,`EXPLAIN` 显示 `type=ALL`(全表扫)——优化器为什么不用?

### 优化器按 cost 选,不是"有索引就一定走"

朴素印象是"有索引就走索引"。这是错的。**优化器按 cost(代价)选,谁 cost 低走谁**。全表扫的 cost 和走索引的 cost,各自算出来,比大小。这个比较在 [`best_access_path`](../mysql-server/sql/sql_planner.cc#L983) 里完成。

cost 怎么算?简化版公式:

```
   cost ≈ (IO 成本) + (CPU 评估成本)

   全表扫 cost  ≈  scan_time()         + row_evaluate_cost(rows_after_filter)
                  ↑ InnoDB 给的           ↑ server 算的(行数 × 单行评估常数)

   走索引 cost  ≈  read_cost(每行 B+树查找) × prefix_rowcount  +  回表 cost(若不覆盖)
```

关键在于:**走索引的 cost 里,如果"要回表",每命中一行就得加一次"回表 B+树查找"的成本**。如果一个非唯一索引的选择性差(`rec_per_key` 大,比如 `status` 只有 0/1/2 三个值,每个值平均命中 1/3 表),走它要命中海量行、海量回表——cost 可能比直接全表扫还高。于是优化器选全表扫。

看一个具体例子:

```sql
-- orders 表 1 亿行,status 只有 0/1/2/3 四个值
-- 有索引 idx_status(status)
SELECT * FROM orders WHERE status = 1;  -- 命中约 2500 万行
```

走 `idx_status`:在二级索引 B+树定位 `status=1`,拿到 2500 万个主键值,**每个都要回表一次聚簇索引**——2500 万次 B+树查找。全表扫:顺着聚簇索引叶子链表扫 1 亿行,顺序读,**一次 B+树遍历**。优化器一比:走索引的 2500 万次随机回表 ≫ 全表扫的 1 亿次顺序读——**选全表扫**。`EXPLAIN` 显示 `type=ALL`。

这不是优化器笨,是它算得对——**这种低选择性列上建索引,本就没用**。索引的价值在"高选择性 + 少量命中",`status` 这种四值列,索引帮不了 OLTP 点查。

### InnoDB 的两道反向补偿:`/2` 和"不除 10"

但事情还有更微妙的一面。看 [`info_low_key`](../mysql-server/storage/innobase/handler/ha_innodb.cc#L17660) 里这段被注释称为"legacy"但实际仍在生效的代码([ha_innodb.cc:17729-17743](../mysql-server/storage/innobase/handler/ha_innodb.cc#L17729)):

```cpp
        /* Since MySQL seems to favor table scans too much over index searches,
        we pretend index selectivity is 2 times better than our estimate: */
        rec_per_key_int = rec_per_key_int / 2;

        if (rec_per_key_int == 0) {
          rec_per_key_int = 1;
        }

        key->rec_per_key[j] = rec_per_key_int;
```

注释和代码合起来翻译:**"MySQL(优化器)太偏袒全表扫了,所以我们假装索引选择性比我们估的好 2 倍——把 `rec_per_key` 除以 2。"** 也就是说,InnoDB 在给优化器喂"每个键值平均多少行"时,**故意把它砍半**,让索引看起来更值(命中行数更少、回表更少),从而诱导优化器更倾向走索引。

还有另一处,在 [`scan_time`](../mysql-server/storage/innobase/handler/ha_innodb.cc#L17103)(全表扫成本估算)里([ha_innodb.cc:17103-17107](../mysql-server/storage/innobase/handler/ha_innodb.cc#L17103)):

```cpp
double ha_innobase::scan_time() {
  /* Since MySQL seems to favor table scans too much over index
  searches, we pretend that a sequential read takes the same time
  as a random disk read, that is, we do not divide the following
  by 10, which would be physically realistic. */
  ...
  return ((double)stat_clustered_index_size);   // 直接返回聚簇索引叶子页数,不除以 10
}
```

翻译:**"MySQL 太偏袒全表扫,所以我们假装顺序读和随机读一样快——本该把全表扫成本除以 10(物理上顺序读快约 10 倍),我们就是不除,让全表扫看起来更贵。"** 这一手让全表扫的 cost 在优化器眼里被放大 10 倍,进一步抑制全表扫、诱导走索引。

> **钉死这件事**(本章技巧精解的核心):InnoDB 在它和 server 优化器的接口上,埋了**两道反向补偿**——① 把索引的 `rec_per_key` **除以 2**(假装索引选择性 2 倍好);② 把全表扫成本 **不除以 10**(假装顺序读和随机读一样慢)。两道补偿都指向同一个方向:**抑制 server 优化器对全表扫的天然偏袒、诱导它走索引**。这是"InnoDB 引擎层对 server 优化器行为的校正",源码注释写得直白("MySQL seems to favor table scans too much")。**理解了这两道补偿,你才能解释"为什么 InnoDB 有时走索引、有时不走"——那是 server 的偏袒和 InnoDB 的反偏袒拔河的结果**。

### 反过来:为什么有时 InnoDB 想走索引,但 server 还是选全表扫

有了这两道补偿,你以为 InnoDB 已经把天平拨向索引了。那为什么还会出现"明明有索引却走全表扫"?几个常见原因:

1. **统计严重过期**:`stat_n_rows`/`n_diff_key_vals[]` 是上次 `ANALYZE` 的值,表已经变了 10 倍。优化器拿过期的 `rec_per_key` 算 cost,自然算错。这是**最常见**的原因——`ANALYZE TABLE` 一下就好。
2. **数据分布倾斜 + 采样 20 页不够**:列大部分值高选择性,但有几个热点值占大头。采样 20 页正好采到热点区,`n_diff` 估低、`rec_per_key` 估高,优化器以为走索引命中太多行、不如全表扫。加大 `STATS_SAMPLE_PAGES` 或用直方图(8.0+)。
3. **范围太广**:`WHERE id BETWEEN 1 AND 90000000`(表 1 亿行),走索引要 9000 万次回表,全表扫更便宜——优化器对。这不是 bug。
4. **`LIKE '%xxx'`(前导通配)**:B+树没法用(前缀未知),只能全表扫或全索引扫。这是 B+树的物理限制,不是优化器问题。
5. **隐式类型转换**:`WHERE phone = 13800000000`(phone 是 varchar,传了个 int),MySQL 做类型转换,索引失效。**线上高频坑**。

> **排查口诀**:"索引没用上"时,按这个顺序查——① 统计过期?`ANALYZE TABLE`;② 选错索引?`ANALYZE TABLE` + 看 cardinality;③ 范围太广?正常;④ 前导通配?改 SQL;⑤ 隐式转换?对齐类型。第五节有完整清单。

---

## 四、承接 P1:回表/覆盖/最左前缀,在 explain 里长什么样

P1-02/03 讲了聚簇索引、二级索引、回表、覆盖索引、最左前缀的"是什么"和源码路径。本章从"怎么观测"的角度,把它们落到 explain 的输出上。

### 覆盖索引 = `Extra: Using index`

P1-03 讲过,InnoDB 内部用 `need_to_access_clustered` 这个布尔位判定要不要回表。那 server 层怎么把它显示成 explain 的 `Using index`?靠 server 层维护的一个 bitmap——[`TABLE::covering_keys`](../mysql-server/sql/table.cc#L6094)。

初始化时,如果一个索引的全部 key_part 覆盖了表的全部字段,标为 covering([table.cc:6093-6094](../mysql-server/sql/table.cc#L6093)):

```cpp
// sql/table.cc,第 6093-6094 行
if (!invisible) {
  if (field_count == key_part_count) covering_keys.set_bit(keyno);  // 索引含全表字段 → covering
  ...
}
```

然后每次查询时,根据"实际要读哪些列"(read_set)逐步收窄 `covering_keys`(`covering_keys.intersect(...)`,见 [item_func.cc:7975](../mysql-server/sql/item_func.cc#L7975))。最终,`covering_keys` 里还亮着的那些索引,就是"这次查询能覆盖的索引"。优化器/explain 用它判定:

```cpp
// sql/join_optimizer/explain_access_path.cc,第 149-151 行
static bool IsCoveringIndexScan(const KEY &key, const TABLE &table) {
  return !table.no_keyread && table.covering_keys.is_set(&key - table.key_info);
}
```

`IsCoveringIndexScan` 返回 true → explain 输出 `Extra: Using index`。

> **承接 P1-03**:P1-03 讲的是 InnoDB 引擎内部的 `need_to_access_clustered`(逐列检查 `rec_field_no`)。本章讲的是 server 层的 `covering_keys` bitmap。**两者是同一件事在两层的表现**——server 层用 `covering_keys` 决定"这个索引能不能 covering",InnoDB 层用 `need_to_access_clustered` 决定"这个查询实际要不要回表"。两层配合:server 选索引时优先 covering 的,InnoDB 执行时按 `need_to_access_clustered` 跳过回表。`Using index` 就是这两层配合的观测点。

### 回表 = `Extra` 里**没有** `Using index`(且 `type` 不是 `const/eq_ref`)

回表没有专门的 `Extra` 标记,但可以用排除法:如果走的是二级索引(`type=ref/range/index` 且 `key` 不是主键),且 `Extra` 里**没有** `Using index`,那基本就是要回表。对比:

```sql
-- idx_name(name) on users(id PK, name, age, email)
EXPLAIN SELECT name, id   FROM users WHERE name='b';  -- Extra: Using index  → 覆盖,不回表
EXPLAIN SELECT *          FROM users WHERE name='b';  -- Extra: NULL          → 回表
EXPLAIN SELECT name, age  FROM users WHERE name='b';  -- Extra: NULL          → 回表(age 不在 idx_name)
```

第二条 `SELECT *` 要 age/email,不在 `idx_name` 里 → 回表。第三条要 age,也不在 → 回表。只有第一条(只要 name + 主键 id)才是覆盖——P1-03 讲的"主键隐含在所有二级索引里"的红利。

> **钉死一件事**:`Extra: Using index` = 覆盖索引免回表(承接 P1-03)。排查慢查询时,如果一个走二级索引的查询慢,第一步就看 `Extra` 有没有 `Using index`——没有的话,八成是回表太多。这时候要么改 SQL 只取索引列,要么建个更大的联合索引把常用列都覆盖进去。

### 最左前缀 = `key_len` 反推用了几列

第二节讲过 `key_len` 反推。这里用 P1-03 的最左前缀原理再点一遍:

```sql
-- idx_user_status(user_id, status) on orders
EXPLAIN SELECT * FROM orders WHERE user_id = 100;            -- key_len=8 (BIGINT), 用了 user_id
EXPLAIN SELECT * FROM orders WHERE user_id = 100 AND status=1; -- key_len=9 (8+1 TINYINT), 两列都用
EXPLAIN SELECT * FROM orders WHERE status = 1;                -- type=ALL! 最左前缀不命中
```

第三条 `WHERE status=1`——`idx_user_status` 按 `(user_id, status)` 排,status 在全局无序(只在每个 user_id 小段内有序),B+树用不上。`key_len=0`(没用索引),`type=ALL`。这就是 P1-03 最左前缀的 explain 体现。

### 一个综合例子:四条 SQL,四种走法

把 P1 的 B+树原理和本章的 explain 读完,用一个例子把四条 SQL 的 explain 都过一遍。表:

```sql
CREATE TABLE orders (
    id        BIGINT PRIMARY KEY,            -- 聚簇索引
    order_no  VARCHAR(32),
    user_id   BIGINT,
    amount    DECIMAL(10,2),
    status    TINYINT,
    INDEX idx_order_no (order_no),           -- 二级索引:叶子页 (order_no, id)
    INDEX idx_user_status (user_id, status)  -- 联合二级索引:叶子页 (user_id, status, id)
);
```

**查询 A:主键点查(聚簇索引一跳)**

```sql
EXPLAIN SELECT * FROM orders WHERE id = 10086;
-- +----+--------+--------+------+---------+------+-------+-------+
-- | id | select | table  | type | key     | key_ | rows  | Extra |
-- |    | _type  |        |      |         | len  |       |       |
-- +----+--------+--------+------+---------+------+-------+-------+
-- |  1 | SIMPLE | orders | const| PRIMARY | 8    | 1     | NULL  |
-- +----+--------+--------+------+---------+------+-------+-------+
```

`type=const`(PK 等值,一行)、`key=PRIMARY`、`rows=1`、`key_len=8`(BIGINT)、`Extra=NULL`(聚簇索引,本来就在里面,无所谓回表)。**B+树一次定位到叶子页,数据就在那**——P1-02 讲的"主键一跳"。

**查询 B:二级索引 + 回表**

```sql
EXPLAIN SELECT * FROM orders WHERE order_no = 'ORD001';
-- type=ref, key=idx_order_no, rows≈1, Extra: NULL
```

`type=ref`(非唯一索引等值)、`key=idx_order_no`、`Extra` 没 `Using index`(`SELECT *` 要 amount/status,不在 idx_order_no 里)→ **回表**。两步:idx_order_no B+树定位拿 id → 聚簇索引 B+树用 id 取整行(P1-03 的回表路径)。

**查询 C:二级索引 + 覆盖**

```sql
EXPLAIN SELECT order_no, id FROM orders WHERE order_no = 'ORD001';
-- type=ref, key=idx_order_no, rows≈1, Extra: Using index
```

只要 order_no + id,都在 idx_order_no 叶子页里 → `Using index`,**免回表**。一次 B+树查找。

**查询 D:联合索引最左前缀 + 范围**

```sql
EXPLAIN SELECT * FROM orders WHERE user_id = 100 AND status > 1;
-- type=ref, key=idx_user_status, key_len=9, rows≈50, Extra: Using where
```

`key_len=9`(user_id 8 + status 1)→ 两列都用上(等值 user_id + 范围 status,range scan)。`Extra: Using where` 说明 server 还要再过滤一遍(status>1 在引擎层已部分处理,但 server 再确认)。**B+树定位到 user_id=100 段,在段内 range 扫 status>1**。

> **钉死一件事**:把这四条 SQL 的 explain 和 P1 的 B+树原理一一对应——`const`(聚簇一跳)、`ref`+回表(二级定位 + 回聚簇)、`ref`+`Using index`(二级定位、免回表)、`ref`+`key_len` 反推最左前缀。**explain 的每一列,都是 P1 的 B+树在物理上怎么走的观测投影**。读不懂 explain,本质是没把 B+树原理和这些字段对应起来。

---

## 五、慢查询排查清单:从 explain 到真因

讲了这么多原理,落到实战。线上一个慢查询,怎么排查?下面是一份从 explain 出发的清单,按"由表及里、由估算到事实"的顺序。

### 第 0 步:确认它真的慢、且是查询本身慢

- 看慢查询日志(`slow_query_log`),确认 `Query_time` 和 `Rows_examined`。`Rows_examined` 远大于返回行数 → 扫了太多没用的行(典型:没索引或选错索引)。
- 排除锁等待:`SHOW PROCESSLIST` 看是不是 `Sending data` 之外的状态(如 `Waiting for table metadata lock`、`updating`)——锁等待会让一个本该快的查询变慢,根因不在索引。

### 第 1 步:`EXPLAIN` 看走法

```sql
EXPLAIN SELECT ...;
```

盯这四列:

| 看哪 | 什么样要警觉 | 可能原因 |
|------|-------------|----------|
| `type` | `ALL`(全表扫) | 没可用索引 / 统计过期 / 范围太广 / 隐式转换 |
| `key` | `NULL`(没走索引) | 同上 |
| `rows` | 远大于返回行数 | 选错索引 / 范围太广 / 过滤条件没下推 |
| `Extra` | `Using filesort` / `Using temporary` | ORDER BY/GROUP BY 没命中索引 |

### 第 2 步:`EXPLAIN FORMAT=TREE`(8.0+)看执行计划树

```sql
EXPLAIN FORMAT=TREE SELECT ...;
```

传统 explain 是表格,看不出"谁驱动谁、谁嵌套谁"。TREE 格式给的是带缩进的执行计划树,能看清 JOIN 的驱动/被驱动关系、各节点的 cost。比如:

```
-> Filter: (orders.status > 1)  (cost=12.3 rows=50)
    -> Index lookup on orders using idx_user_status (user_id=100)  (cost=... rows=...)
```

能看出"先 index lookup,再 filter",以及每一步的估算 cost 和 rows。

### 第 3 步:`EXPLAIN ANALYZE`(8.0+)看真实行数——**最有用的一步**

```sql
EXPLAIN ANALYZE SELECT ...;
```

`EXPLAIN ANALYZE` **真的跑一遍查询**,记录每个迭代器的**真实** loop 次数、真实 rows、真实耗时。这是排查慢查询最有力的工具——它消除了"估算不准"的迷雾。对比:

```
-> Index lookup on orders using idx_user_status (user_id=100)
    (actual time=0.1..5.2 rows=1234 loops=1)
```

`rows=1234` 是真值(不是估算的 50)。如果估算 50、实际 1234,说明统计严重过期或数据倾斜——`ANALYZE TABLE` + 加大 `STATS_SAMPLE_PAGES`。

> **钉死一件事**:`EXPLAIN ANALYZE` 是 8.0+ 排查慢查询的杀手锏。普通 `EXPLAIN` 给估算,`EXPLAIN ANALYZE` 给事实。**估算和事实对不上,就是问题所在**。这是本章最实用的一条——线上慢查询,先 `EXPLAIN ANALYZE`。

### 第 4 步:统计过期?`ANALYZE TABLE`

```sql
ANALYZE TABLE orders;
-- 或加大采样
ANALYZE TABLE orders UPDATE STATS_SAMPLE_PAGES=200;
```

如果第 3 步发现估算和真实差很多,先 `ANALYZE TABLE`。大表加大 `STATS_SAMPLE_PAGES`(默认 20 太少)。`ANALYZE` 后再 `EXPLAIN`,看 `rows` 是否变准、`type`/`key` 是否变好。

### 第 5 步:看是不是回表过多

走二级索引、`Extra` 没 `Using index`、`rows` 又大 → 回表海量。两种解法:

1. **改 SQL 只取索引列**(覆盖索引):`SELECT *` → `SELECT user_id, status`(只要 idx_user_status 里的列)。
2. **建更大的联合索引覆盖常用列**:`idx_user_status` → `idx_user_status_amount(user_id, status, amount)`,把 amount 也覆盖进去,`SELECT user_id, status, amount` 就免回表。

### 第 6 步:看是不是 ORDER BY 没命中索引(`Using filesort`)

`Extra: Using filesort` 意味着额外排序,大结果集时可能落临时文件、巨慢。解法:给 ORDER BY 的列建索引(或联合索引,让排序列在最后,利用 B+树叶子链表有序)。比如 `ORDER BY create_time DESC` → 给 `create_time` 建索引,explain 的 `Using filesort` 会消失。

### 第 7 步:看是不是 GROUP BY 要临时表(`Using temporary`)

`Extra: Using temporary` 意味着落临时表。某些 GROUP BY 可以靠索引的有序性避免临时表(GROUP BY 列建索引、且顺序对)。但这不是总能消除——看具体场景。

### 第 8 步:看是不是 JOIN 被驱动表没索引(`Range checked for each record`)

`Extra: Range checked for each record (index map: 0x...)` 是个大坑——JOIN 时被驱动表没合适索引,优化器每行重新选索引,极慢。解法:给 JOIN 条件的列建索引。

### 第 9 步:实在不行,`FORCE INDEX` 临时止血

```sql
SELECT * FROM orders FORCE INDEX (idx_user_status) WHERE ...;
```

如果确认优化器选错了(走全表扫、但其实 idx_user_status 更快),可以 `FORCE INDEX` 强制。但这是**止血不是根治**——根因往往是统计过期或 cost 模型偏差,`FORCE INDEX` 绕过了优化器,后续数据变了可能又不对。优先用前面的 `ANALYZE TABLE` / 直方图 / `optimizer_hints`。

### 一份速查清单

| 症状(explain) | 八成原因 | 第一步动作 |
|---------------|----------|-----------|
| `type=ALL` 且有大表 | 没索引 / 统计过期 / 范围太广 | `ANALYZE TABLE`;看 WHERE 列有没有索引 |
| `rows` 远大于真实返回 | 估算不准 / 选错索引 | `EXPLAIN ANALYZE` 对照真实 |
| `Extra: Using filesort` | ORDER BY 没命中索引 | 给排序列建索引 |
| `Extra: Using temporary` | GROUP BY/DISTINCT 要临时表 | GROUP BY 列建索引(看场景) |
| `key` 是二级索引但没 `Using index` | 回表 | 改 SQL 覆盖 / 建联合索引 |
| `key_len` 没用满联合索引 | 最左前缀没命中全 | 检查 WHERE 顺序/中间列缺值 |
| `Range checked for each record` | JOIN 被驱动表没索引 | 给 JOIN 列建索引 |

---

## 六、技巧精解:InnoDB 在引擎-优化器接口上的两道反向补偿

本章正文讲完了 explain 的读法和索引选择的原理。最后,有一个最硬核的洞察值得单独拆透——**InnoDB 在它和 server 优化器的统计接口上,埋了两道反向补偿,刻意扭曲喂给优化器的数字,以校正优化器对全表扫的天然偏袒**。这个洞察,解释了"为什么有时走索引、有时不走"的深层原因,是本章的灵魂。

### 技巧:两道反向补偿——`/2` 和"不除 10"

我们已经见过这两段代码,现在从"为什么 sound(为什么这么干合理)"的角度钉死。

**补偿一:把索引的 `rec_per_key` 除以 2**([ha_innodb.cc:17735-17737](../mysql-server/storage/innobase/handler/ha_innodb.cc#L17735))

```cpp
/* Since MySQL seems to favor table scans too much over index searches,
we pretend index selectivity is 2 times better than our estimate: */
rec_per_key_int = rec_per_key_int / 2;
```

**补偿二:把全表扫成本不除以 10**([ha_innodb.cc:17103-17107](../mysql-server/storage/innobase/handler/ha_innodb.cc#L17103))

```cpp
double ha_innobase::scan_time() {
  /* Since MySQL seems to favor table scans too much over index
  searches, we pretend that a sequential read takes the same time
  as a random disk read, that is, we do not divide the following
  by 10, which would be physically realistic. */
  ...
  return ((double)stat_clustered_index_size);
}
```

### 为什么这两道补偿 sound(合理)

**第一层:物理事实是什么?**

- **顺序读比随机读快**:HDD 上,顺序读约 100MB/s,随机寻道后单页读约 100 次/s——差约 100 倍;SSM 上差距小些(顺序 ~2GB/s,随机 ~100K IOPS × 16KB ≈ 1.6GB/s),但仍约 1-10 倍。所以"全表扫(顺序)成本 ÷ 10"是物理上合理的。
- **索引等值查的选择性**:就是 `rec_per_key`(每键值平均多少行)。这是 InnoDB 采样的客观估算。

**第二层:server 优化器的偏袒在哪?**

server 优化器在算"全表扫 vs 走索引"的 cost 时,有一个系统性的偏差——**它倾向于低估全表扫的代价、高估走索引的代价**。原因复杂(涉及 cost model 常数、JOIN 的 fanout 计算),但效果是:**在 InnoDB 这种"全表扫其实也是走聚簇索引 B+树"的引擎上,server 的偏袒尤其不合理**——InnoDB 的全表扫和走二级索引,物理上都是 B+树遍历,差距没那么大;但 server 的 cost model 把它们算得很悬殊,导致过度选全表扫。

**第三层:InnoDB 怎么校正?**

InnoDB 没法改 server 的 cost model(那是 server 层的事),只能在它给 server 的统计接口上"动手脚":

- **`/2`**:把 `rec_per_key` 砍半 → server 眼里"走索引命中行数少了一半"→ 走索引 cost 低 → 更倾向走索引。
- **"不除 10"**:全表扫成本不按物理顺序读优势打折 → server 眼里"全表扫没那么便宜"→ 更倾向走索引。

两道补偿都指向同一方向:**抑制全表扫、诱导走索引**。这是 InnoDB 在引擎-优化器接口上,对 server 行为的"反向校正"。

### 反面对比:如果 InnoDB 不补偿会怎样?

假设 InnoDB 老老实实给真实 `rec_per_key`、`scan_time` 真的除以 10:

- server 的天然偏袒 + 真实数字 → **大量本该走索引的查询走了全表扫**。
- InnoDB 的二级索引建了等于没建(优化器不肯用)。
- 用户体感:"InnoDB 索引没用,查得慢"——其实是优化器选错了。

这正是 InnoDB 早期(5.x 之前)被吐槽"索引选不准"的背景之一。`/2` 和"不除 10"是逐渐加进去的校正(代码注释的"legacy"和"Since MySQL seems to favor..."就是这段历史)。

> **钉死这件事**:InnoDB 的两道反向补偿,是**引擎层对优化器层行为的校正**——一个引擎没法改 server 的 cost model,但能扭曲自己喂给 server 的统计,间接影响 server 的决策。这种"在接口边界上做反向补偿"的工程手法,在系统设计里很常见(类似 Linux 内核对硬件特性的 quirks)。**它也提醒我们:explain 的 `rows` 是 cost model + 扭曲统计的综合产物,不是客观物理量**——这也是为什么 `EXPLAIN ANALYZE` 给的真实 rows 才是事实。

### 一个推论:cost model 在演进,补偿可能过时

值得注意:MySQL 8.0 引入了新的 cost model(`opt_costconstants`/`opt_costmodel`,可配 `cost_model_server` 表),server 对全表扫的偏袒在减弱。这意味着 InnoDB 的"除以 2"和"不除 10"**可能在新 cost model 下过度补偿了**——某些场景反而导致 InnoDB 过度选索引。这也是为什么 8.0+ 偶尔出现"以前走全表扫挺快、升级后改走索引反而慢"的现象。这种引擎层固定补偿 vs 优化器层演进 cost model 的张力,是 InnoDB 这套老架构的固有债务。未来的方向是把更多 cost 决策下放进引擎(让引擎自己估,而不是 server 算 + 引擎扭曲),但这需要大改 handler 接口。

---

## 七、章末小结

### 回扣主线

本章服务二分法的**"存储与索引"**这一面,而且是这一面的**实践收尾**。主线"一条写,InnoDB 用 B+树聚簇索引找到位置、redo 保 crash 不丢、undo/MVCC 保并发读、锁保隔离"——本章讲的是"B+树找到位置"的**观测和调优**:P1 篇讲了 B+树怎么存、二级索引怎么回表,本章讲怎么用 explain 观测"B+树实际怎么走的、走得对不对",以及怎么据此调优索引、排查慢查询。

第 6 篇三章是全书从"原理"到"实践"的过渡:P6-20 讲一条 SQL 怎么从 server 层走到 InnoDB(衔接),**本章 P6-21 讲怎么观测和调优这条 SQL 走得对不对(存储与索引/实践)**,P6-22 讲怎么在线改表结构(存储与索引)。本章是 P1 篇(聚簇索引/二级索引/页)的"调优实践篇"——P1 讲"是什么、为什么这么设计",本章讲"怎么观测、怎么调"。

### 五个为什么清单

1. **为什么 explain 的 `rows` 是估算、经常不准?** —— `rows` 来自两类数字:① `stat_n_rows`/`n_diff_key_vals[]`(静态统计,`ANALYZE TABLE` 时**采样 N 个叶子页**估,默认 N=20,见 [`N_SAMPLE_PAGES`](../mysql-server/storage/innobase/dict/dict0stats.cc#L138));② `records_in_range`(动态范围估算,两条 dive 路径外推,见 [`btr_estimate_n_rows_in_range_low`](../mysql-server/storage/innobase/btr/btr0cur.cc#L5039))。两者都是估算、都依赖数据分布均匀。大表 + 倾斜 → 可能差一个数量级。`EXPLAIN ANALYZE` 给真实值。

2. **为什么有时不选你建的索引?** —— 优化器按 cost 选,不是"有索引就走"。低选择性列(如 `status` 四值)走索引要海量回表,cost 比全表扫高,优化器选全表扫——它算得对。常见原因:统计过期(`ANALYZE TABLE`)、范围太广、`LIKE '%xx'` 前导通配、隐式类型转换。

3. **为什么 InnoDB 要在统计接口上"动手脚"(`rec_per_key/2`、`scan_time` 不除 10)?** —— server 优化器天然偏袒全表扫(在 InnoDB 这种"全表扫也走聚簇 B+树"的引擎上不合理)。InnoDB 没法改 server cost model,只能在引擎-优化器接口上反向补偿:把索引选择性说成 2 倍好、把全表扫成本说成 10 倍贵,诱导 server 走索引。见 [`info_low_key`](../mysql-server/storage/innobase/handler/ha_innodb.cc#L17735) 和 [`scan_time`](../mysql-server/storage/innobase/handler/ha_innodb.cc#L17103)。

4. **为什么 `Extra: Using index` 是好信号?** —— 它 = 覆盖索引免回表(P1-03 的 `need_to_access_clustered==false`)。判定在 server 层 [`covering_keys`](../mysql-server/sql/table.cc#L6094) bitmap + [`IsCoveringIndexScan`](../mysql-server/sql/join_optimizer/explain_access_path.cc#L149)。覆盖索引省的不只是回表那次 B+树查找,还有聚簇索引页的锁竞争和 buffer pool 压力。

5. **为什么 `EXPLAIN ANALYZE` 比普通 `EXPLAIN` 更适合排查??** —— 普通 `EXPLAIN` 只给优化器的估算(`rows` 是估的,可能严重偏离)。`EXPLAIN ANALYZE`(8.0+)**真跑一遍**,给每个迭代器的真实 loop/rows/耗时。估算和真实对不上的地方,就是问题所在(统计过期、数据倾斜、选错索引)。

### 想继续深入往哪钻

- **源码**:
  - explain 字段填充:[`sql/opt_explain.cc`](../mysql-server/sql/opt_explain.cc)、[`sql/opt_explain_traditional.cc`](../mysql-server/sql/opt_explain_traditional.cc)(`traditional_extra_tags[]`)。
  - join_type 枚举(type 各级别):[`sql/sql_opt_exec_shared.h:184`](../mysql-server/sql/sql_opt_exec_shared.h#L184)。
  - 索引选择核心:[`sql/sql_planner.cc:983`](../mysql-server/sql/sql_planner.cc#L983) `best_access_path`(承接《PG》优化器章节)。
  - InnoDB 给优化器喂统计:[`storage/innobase/handler/ha_innodb.cc`](../mysql-server/storage/innobase/handler/ha_innodb.cc) 的 `records_in_range`(L16936)、`scan_time`(L17103)、`innodb_rec_per_key`(L17268)、`info_low_key`(L17660)。
  - 范围行数估算:[`storage/innobase/btr/btr0cur.cc:5039`](../mysql-server/storage/innobase/btr/btr0cur.cc#L5039) `btr_estimate_n_rows_in_range_low`。
  - 统计采样(ANALYZE):[`storage/innobase/dict/dict0stats.cc`](../mysql-server/storage/innobase/dict/dict0stats.cc) `dict_stats_analyze_index`(L2234)、`N_SAMPLE_PAGES`(L138)。
  - covering_keys 判定:[`sql/table.cc:6094`](../mysql-server/sql/table.cc#L6094)、[`sql/join_optimizer/explain_access_path.cc:149`](../mysql-server/sql/join_optimizer/explain_access_path.cc#L149)。
- **MySQL 宏方文档**:"EXPLAIN Output Format"、"EXPLAIN ANALYZE"、"Optimizer Cost Model"、"InnoDB Persistent Statistics"。
- **动手感受**:
  - 建一张大表(或用 `sys` 库的 `sysbench` 生成),试 `EXPLAIN` vs `EXPLAIN ANALYZE` 的 rows 差异;
  - `ANALYZE TABLE t STATS_SAMPLE_PAGES=200`,看 cardinality 变化;
  - 在低选择性列建索引,观察优化器选全表扫;
  - `EXPLAIN FORMAT=TREE` 看 JOIN 的驱动关系;
  - `SHOW INDEX FROM t` 看 Cardinality;`optimizer_trace`(`SET optimizer_trace=1`)看 cost model 决策细节。

### 引出下一章

本章讲了怎么用 explain 观测和调优已有索引。但生产环境还有一类高频痛点——**加字段、加索引这种 DDL,怎么不锁表、不停业务?** 老版本(5.6 之前)加个字段要重建整张表、锁几小时,业务全瘫。MySQL 5.6 加了 online DDL(in-place)、8.0 加了 instant DDL(秒级、只改数据字典)。下一章 P6-22,讲 **在线 DDL:加字段/索引怎么不锁表**——从 in-place alter 到 online rebuild 到 instant DDL,拆 InnoDB 怎么在改表结构的同时不阻塞业务读写。

> **下一章**:[P6-22 · 在线 DDL:加字段/索引怎么不锁表](P6-22-在线DDL-加字段索引怎么不锁表.md)
