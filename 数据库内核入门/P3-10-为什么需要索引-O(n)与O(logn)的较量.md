# 第 10 章 · 为什么需要索引:O(n) 全表扫描 vs O(log n) 索引扫描

> **前置**:前两篇讲了"一条 SQL 怎么被解析、优化、执行"(P1),以及"数据存在 8KB 的页里、Buffer Pool 做内存中转"(P2)。第 5 章执行器里,我们顺手看过两个扫描算子——`SeqScan`(从头扫到尾)和 `IndexScan`(走索引定位)。但那时只说了一句"索引详见 P3",就滑过去了。本章就把"索引"这个角色正式请上舞台:它到底为什么存在、把查找从多慢变多快、以及为什么它不是万能的。

> **核心问题**:没有索引会怎样(每次查找都要把整张表从头扫到尾,O(n),表越大越慢)?索引怎么把查找从 O(n) 降到 O(log n)?但索引不是万能——什么时候索引反而比全表扫描更慢(小表、低基数列、要回表取大量列),为什么?为什么索引本质是"用空间换时间 + 维护代价"?以及 PostgreSQL 的索引是**二级结构**、与堆表分离(叶子只存 TID 指回堆)——这意味着什么?

> **读完本章你会明白**:大数据量下 O(n) 为什么致命而 O(log n) 可控(用一张对照表钉死);索引扫描的"随机回表"代价从哪来、PG 怎么用 `spc_random_page_cost`(默认 4.0)vs `spc_seq_page_cost`(默认 1.0)量化它;优化器凭什么在"全表扫"和"索引扫"之间做选择;索引的两大代价(空间 + 写放大);以及 PG 索引"堆表之外、叶子存 TID"这个设计带来的连锁后果——它直接预告了下一章(B 树)和第 13 章(回表)。

> **进入 P3 索引篇**:从本章起连续四章(第 10~13 章),我们只讲一件事——怎么快速找到数据。它服务的是全书主线"让数据**快**"那一侧,直接对付第三个本性:"用户说**要什么**,机器只会**按位置读**"这个错位。

> **如果一读觉得太难**:先记三件事——① 没有索引,查找是 O(n):n 行就得碰 n 行;有了 B 树索引,查找是 O(log n):10 亿行也只要约 30 次比较。② 索引不是万能:小表、要返回大部分行的查询、低基数列,优化器反而会选全表扫——因为索引扫描多了"回表"这一步随机 I/O。③ 索引的代价是"占额外空间 + 每次写都要同步维护"(写放大)。其余细节第二遍配合源码再抠。

---

## 一、没有索引的世界:查找是 O(n)

第 1 章讲"用裸文件存数据的四个乱子"时,乱子三就是它:**找一条数据,要扫遍整个文件**。现在我们把这个痛点的"为什么致命"用复杂度语言讲透。

假设你有一张 `users` 表,1000 万行,存了若干页里。你执行:

```sql
SELECT * FROM users WHERE id = 42;
```

如果 `id` 列上**没有索引**,执行器唯一能做的就是**顺序扫描(SeqScan)**:从表的第一个页开始,一页一页读进来,把每一行都拿出来比对 `id = 42`?直到找到为止。

### 不这样会怎样:表越大,单次查找越慢,而且线性恶化

顺序扫描的代价是 **O(n)**——n 是表的总行数。要找的行可能在第 1 行,也可能在最后一行,平均要扫 n/2 行;最坏要扫满 n 行。关键在于:**这个代价和"你要几行"没关系,和"表多大"成正比。** 你只要 1 行,也得把整张表过一遍。

> **不这样会怎样**:表小的时候(几千行),O(n) 没什么感觉——扫几千行,毫秒级。但表一大起来,情况是**线性恶化**的:

| 表行数 n | 顺序扫描要碰的行(平均 n/2) | 直觉上的耗时 |
|---|---|---|
| 1 千 | 500 | 几乎瞬间 |
| 1 百万 | 50 万 | 几十毫秒 |
| 1 亿 | 5 千万 | 几秒 |
| 10 亿 | 5 亿 | 几十秒到分钟级 |

注意"线性"这个词的分量:**n 翻 1000 倍,耗时也翻约 1000 倍。** 表从 100 万涨到 10 亿(涨 1000 倍),一个点查询从几十毫秒变成几十秒。对一个 OLTP 系统(每秒几千次点查询),这是不可接受的——每查一次都要扫全表,数据库基本就废了。

> 这就是第 5 章执行器里 `SeqNext` 干的事:不断调 `table_scan_getnextslot`,把表一行行过一遍。它没有任何"跳过无关行"的能力——因为它不知道 `id = 42` 在哪,只能挨个看。

### 所以需要一种东西:能"按值跳到位置"

O(n) 的根源是:**机器只会"按位置读"(给我一个偏移量,我给你那块字节),但用户说的是"按值查"(id = 42 的那个)**。中间这道鸿沟,顺序扫描是用"蛮力遍历"填的——把所有位置都读一遍,逐个比对值。

要打破 O(n),就需要一个**额外的数据结构**,它能回答一个问题:"值等于 42 的行,在哪个位置?"——并且回答得**比遍历快得多**。这个结构,就是**索引(index)**。

> 用第 1 章的话说:索引填的,正是本性三("用户说要什么,机器只会按位置读")那道鸿沟。它把"按值查"翻译成"按位置读",而且翻译得快。

---

## 二、索引怎么把 O(n) 降到 O(log n)

### 索引长什么样:一棵平衡的多路查找树

最经典的索引是 **B 树(B-tree)**(PG 默认索引,下一章详讲它的页面结构)。这里先用它的**抽象性质**说清复杂度,细节留给第 11 章。

B 树是一种**平衡的多路查找树**:每个节点是一页(8KB),里面装着一组**排好序的索引项**和指向子节点的指针。查找时,从根节点开始,在节点内用二分或顺序比对定位"该往哪个子节点走",一路往下,直到叶子。叶子节点里存着真正的索引项——**键值 + 一个指向堆表行的 TID**(TID = 块号 + 行指针偏移,第 6、7 章讲过)。

关键性质:**B 树是平衡的**——从根到任何叶子的路径长度都一样,这个长度叫树高 h。

### 为什么 O(log n):每往下一层,搜索空间缩小一个倍数

在 B 树里,每访问一个节点(读一页),就能把搜索范围**缩小到一个子树**。一个节点能分出多少路?一页 8KB,一个索引项几十字节,一个节点能装上百个索引项,也就是上百路分叉(这个"分叉数"叫**扇出 fanout**,记作 F,典型值 100~200)。

所以每往下一层,剩下的候选行就**除以 F**。n 行的数据,树高 h 满足 F^h ≈ n,也就是 **h ≈ log_F(n)**。查找一棵 B 树,就是从根走到叶子,访问 h 个页——所以查找复杂度是 **O(log n)**(准确说是 O(log_F n),但常被简写成 O(log n))。

> **log 的魔力**:log 是增长极慢的函数。用 F=100 估一下树高:

| 表行数 n | B 树树高 h(访问的索引页数) | 顺序扫描要碰的页数(估算) |
|---|---|---|
| 1 千 | 1~2 | 几页 |
| 1 百万 | 3 | 几千页 |
| 1 亿 | 4~5 | 几十万页 |
| 10 亿 | 5 | 几百万页 |

**这张表是本章的核心。** 看右边两列的对比:

- 顺序扫描的页数**和 n 成正比**,线性增长。10 亿行要扫几百万页。
- B 树扫描的索引页数**是 log,几乎不增长**。10 亿行也只要读 5 个左右的索引页。

> **O(n) 和 O(log n) 的差距,在数据量大时是碾压性的。** 10 亿行的点查询:顺序扫描几百万页(几十秒);B 树索引扫描读 5 个索引页 + 1 次回表(微秒级)。这就是索引存在的全部理由——它把"找一行"这件事从"和表大小成正比"变成"和表大小的对数成正比",在数据量爆炸时依然可控。

### 一个常被忽略的点:索引扫描还要回表

注意上面那张表,B 树那列写的是"访问的**索引页**数"。但拿到 TID 之后,执行器通常还要**回到堆表**,把 TID 指向的那个页读进来,取出完整行——这一步叫**回表(heap fetch)**。

> 这一步是 O(1)(读一个堆页),但它是**随机 I/O**——TID 指向的堆页可能散落在表的任何位置,不像顺序扫描那样一页接一页地读。所以索引扫描的真实代价不是"几个索引页"那么简单,而是"几个索引页 + 一次随机堆页读取"。这个"随机"二字,是下一节"索引什么时候反而慢"的关键。

回表的细节(以及怎么干脆不回表)是第 13 章的主角。本章你只需记住:**B 树索引把"定位"从 O(n) 降到 O(log n),但定位完通常还要 O(1) 的随机回表才能拿到完整行。**

---

## 三、索引不是万能:三种"索引反而更慢"的场景

上面把索引夸得天花乱坠,现在泼三盆冷水。这是本章最反直觉、也最实用的一节:**索引用错了,比全表扫还慢。**

理解这三种场景的关键,是记住两个数字:**PG 默认认为顺序读一页代价是 1.0,随机读一页代价是 4.0**。这个 4:1 的比例,就来自两个 GUC:

> [src/backend/utils/misc/postgresql.conf.sample:419-L420](../postgresql-17.0/src/backend/utils/misc/postgresql.conf.sample#L419-L420)

```text
#seq_page_cost = 1.0			# measured on an arbitrary scale
#random_page_cost = 4.0			# same scale as above
```

`spc_seq_page_cost` 和 `spc_random_page_cost` 是优化器代价模型的核心参数(机械盘上随机读确实比顺序读慢几倍;SSD 上差距小些,所以 SSD 部署常把 `random_page_cost` 调到 1.1~2.0)。**顺序扫描是顺序读(每页 1.0),索引回表是随机读(每页 4.0)**——这就是权衡的根。

### 场景一:表很小——索引的固定开销还没省回来

> **不这样会怎样**:一张 100 行的小表,可能就两三个页。顺序扫描读 2~3 页(顺序,代价 ≈ 2~3)。走索引呢?先读根节点(1 页随机)、再读叶子(1 页随机)、再回表读堆页(1 页随机)——3 页随机 = 3 × 4.0 = 12,比顺序扫还贵!

所以优化器对小表**默认不走索引**。这里的直觉是:索引扫描有个"启动开销"(要从根走到叶子),表太小的时候,这个启动开销还没省回来,扫描就结束了。PG 的代价模型里有 `startup_cost` 和 `total_cost` 之分(见下文源码),小表上 SeqScan 的 `total_cost` 天然更低。

### 场景二:要返回大部分行——回表的随机 I/O 堆积

> **不这样会怎样**:`SELECT * FROM orders WHERE status = 'paid'`,而 90% 的订单都是 `paid`。走索引的话,先在索引里找出 90% 的 TID,然后**对每个 TID 回表取整行**。这些 TID 指向的堆页散落各处,绝大多数是随机读——几百万次随机回表,代价高得吓人。而顺序扫描呢?一页一页顺序读下来,顺带把 90% 的行筛出来,每页只花 1.0——反而快得多。

这个场景的教训是:**索引适合"返回少量行"的查询。** 返回的行占表的比例越高,回表的随机 I/O 堆积越严重,索引越不划算。PG 的优化器靠统计信息估算"这个条件会命中多少行"(选择性 selectivity),命中比例高就倾向全表扫。

> 这就是为什么第 5 章讲 SeqScan 时说:"查询要返回大部分行时,走索引反而要一次次回表,比直接扫全表慢。"

### 场景三:低基数列——索引几乎没有筛除能力

> **不这样会怎样**:在 `gender`(只有 'M'/'F' 两个值)上建索引。查 `WHERE gender = 'M'` 命中一半的行。索引虽然"找到了",但找到的是一半的 TID——等于退化成场景二(返回大量行,随机回表堆积)。更糟的是,这种列上建索引,索引本身也巨大(存了一半的 TID),维护代价全付出了,查询却用不上。

低基数列(low cardinality,取值种类很少)**不适合普通 B 树索引**。这种数据该用别的索引类型——比如 PG 的 BRIN 索引(第 12 章会讲,专门对付"取值和物理位置相关的大规模数据",如时间序列)。**索引选错类型,等于白建。**

### 三个场景的共同根:回表的随机 I/O

把三个场景叠在一起,你会发现它们**指向同一个根**:**索引扫描的代价大头不在"查索引"(那是 O(log n),很省),而在"回表"(那是随机 I/O,很贵)。** 当回表的次数多起来(表小但启动开销占比高、返回行多、低基数命中率高),随机 I/O 就压过了顺序扫描的顺序 I/O,索引就从"加速器"变成"拖油瓶"。

> 这正是 PG 代价模型要解决的问题:**它不盲目地"有索引就走索引",而是用统计信息估算两种路径各自的总代价,选省的那条。** 下面源码精读里,我们就看它怎么算这笔账。

---

## 四、索引的两大代价:空间 + 写放大

索引把查找从 O(n) 降到 O(log n),代价是什么?不是免费的——它用**两样东西**换时间。

### 代价一:空间——索引是一份额外的拷贝

索引是一个**独立的数据结构**,它把被索引列的值(加上 TID)**重新存了一份**。一张 100GB 的表,在 5 个列上各建一个索引,索引加起来可能又是几十甚至上百 GB。

> **不这样会怎样**:如果不占额外空间,那就得把索引"揉进"堆表里——但堆表是无序的(行按插入顺序塞进页),没法支持"按值快速定位"。索引必须是一个**按键排序的独立结构**,才能做二分/多路查找。排序的结构无法复用无序的堆表,只能另存一份。这是"用空间换时间"的经典权衡。

注意 PG 的索引叶子**默认只存(键值, TID),不存整行**(第 13 章会讲覆盖索引如何破例)。所以一份索引的大小,大致是 `行数 × (键长 + TID 的 6 字节)`。即便如此,多建几个索引,空间膨胀依然可观。

### 代价二:写放大——每次写都要同步维护索引

这是更隐蔽、也更痛的代价。**索引是堆表的"二级结构":堆表里每插、删、改一行,所有相关索引都要跟着动。**

> **不这样会怎样(不一致)**:如果你 `INSERT` 了一行到堆表,但忘了更新索引,那索引里就没有这行的 TID——之后的查询走索引就找不到它,数据"丢了"。所以索引必须和堆表**严格同步**:堆表改一行,每个索引都要相应地插入/删除一个索引项。

具体到 PG:

- **INSERT 一行**:堆表加一个 tuple;**每个索引都要插入一个新索引项**(B 树要在正确位置插入,可能触发页分裂,下一章讲)。
- **DELETE 一行**:堆表标记 `t_xmax`(第 1 章讲过,PG 的删是"打标记"不真删);**每个索引也要标记对应索引项为待清理**。
- **UPDATE 一行**:PG 是"写新版本、旧行留着"(MVCC,第 17 章详讲)。这意味着——**每个索引都要插入一个新 TID 指向新版本!** 所以在 PG 里,UPDATE 一个被多个索引覆盖的列,写放大尤其严重(这正是 HOT 优化要治的痛,第 21 章)。

> 所以一条朴素的工程经验:**索引不是越多越好。** 每多一个索引,写性能就多掉一截(尤其 UPDATE/INSERT 密集的表)。OLTP 系统建索引,要在"查询快"和"写得慢"之间仔细权衡——这正是本书主线"快"内部的又一个权衡。

### PG 索引是"二级结构":堆表之外,叶子存 TID

把上面两点合起来,PG 的索引设计有一个根本特征,值得单独点明(它贯穿整个 P3):

> **PostgreSQL 的索引,是堆表之外的独立结构。索引叶子节点里,存的是(键值, TID)——TID 指回堆表里的那一行。真正的完整数据,只在堆表里有一份。**

这个设计和某些引擎(如 MySQL InnoDB 的聚簇索引)不同——InnoDB 的主键索引叶子**直接存整行**,索引就是数据本身。PG 选择了"堆表独立、索引只存路标",换来的是:

- **一张表可以建任意多个索引**,而不冗余存整行(每个索引只多存键值 + TID)。
- **堆表无序、可变**(UPDATE 挪行、VACUUM 收缩)不影响索引的稳定性——索引只记 TID,行挪了更新 TID 即可。

但代价也明显:**几乎所有索引扫描都要回表取整行**(第 13 章的仅索引扫描是特例,要求很苛刻)。这条"回表"之路的代价,正是本节"写放大"和上一节"索引反而慢"的共同根源。

> 这个设计决策,直接预告了第 13 章(回表 / 仅索引扫描 / 覆盖索引)的全部内容。本章你只需记住:**PG 索引 = 堆表外的二级结构 + 叶子存 TID 指回堆。**

---

## 关键源码精读:优化器怎么算这笔账 —— `cost_seqscan` vs `cost_index`

前面讲了一堆"什么时候走索引、什么时候走全表扫"。但谁来**拍板**?是优化器的代价模型(P1 第 4 章讲过优化器的角色)。它对每种执行路径估算一个 `total_cost`,选最小的。这里我们看它怎么把"顺序扫描"和"索引扫描"的代价算出来——这是本章"O(n) vs O(log n)"和"随机 vs 顺序"在代码里的落点。

### 1. `cost_seqscan`:顺序扫描的代价 = 页数 × 顺序页代价 + 行数 × CPU 代价

> [src/backend/optimizer/path/costsize.c:284-L351](../postgresql-17.0/src/backend/optimizer/path/costsize.c#L284-L351)

```c
void
cost_seqscan(Path *path, PlannerInfo *root,
			 RelOptInfo *baserel, ParamPathInfo *param_info)
{
	Cost		startup_cost = 0;
	Cost		cpu_run_cost;
	Cost		disk_run_cost;
	double		spc_seq_page_cost;
	...
	/* fetch estimated page cost for tablespace containing table */
	get_tablespace_page_costs(baserel->reltablespace,
							  NULL,
							  &spc_seq_page_cost);

	/*
	 * disk costs
	 */
	disk_run_cost = spc_seq_page_cost * baserel->pages;

	/* CPU costs */
	get_restriction_qual_cost(root, baserel, param_info, &qpqual_cost);
	...
	cpu_per_tuple = cpu_tuple_cost + qpqual_cost.per_tuple;
	cpu_run_cost = cpu_per_tuple * baserel->tuples;
	...
	path->startup_cost = startup_cost;
	path->total_cost = startup_cost + cpu_run_cost + disk_run_cost;
}
```

读这段代码,顺序扫描的代价模型一目了然:

- **磁盘代价 `disk_run_cost = spc_seq_page_cost * baserel->pages`**:就是"表的页数 × 顺序读一页的代价"。`baserel->pages` 是统计信息给的表页数。注意用的是 `spc_seq_page_cost`(默认 1.0)——**顺序扫描的每一页都按"顺序读"计价**。这就是 SeqScan 在大表上"每页便宜"的来源。
- **CPU 代价 `cpu_run_cost = cpu_per_tuple * baserel->tuples`**:对每一行做过滤条件判断(`qpqual_cost`)的 CPU 开销。`baserel->tuples` 是表的总行数——**SeqScan 的 CPU 代价和表大小成正比,这正是 O(n) 的代码化身。**
- 两者相加 = `total_cost`。

> **一句话**:`cost_seqscan` 把 O(n) 写成了代码:磁盘代价随页数线性增长,CPU 代价随行数线性增长。表越大,这条路径越贵——和"返回多少行"无关。

### 2. `cost_index`:索引扫描的代价 = 查索引 + 随机回表

索引扫描复杂得多,因为它要算两段:查索引本身的代价 + 回表取堆行的代价。而且回表是**随机读**,要乘 `spc_random_page_cost`(默认 4.0)。先看它怎么把这两段拼起来。

> [src/backend/optimizer/path/costsize.c:549-L633](../postgresql-17.0/src/backend/optimizer/path/costsize.c#L549-L633)

```c
void
cost_index(IndexPath *path, PlannerInfo *root, double loop_count,
		   bool partial_path)
{
	IndexOptInfo *index = path->indexinfo;
	RelOptInfo *baserel = index->rel;
	bool		indexonly = (path->path.pathtype == T_IndexOnlyScan);
	amcostestimate_function amcostestimate;
	...
	/*
	 * Call index-access-method-specific code to estimate the processing cost
	 * for scanning the index, as well as the selectivity of the index (ie,
	 * the fraction of main-table tuples we will have to retrieve) and its
	 * correlation to the main-table tuple order.
	 */
	amcostestimate = (amcostestimate_function) index->amcostestimate;
	amcostestimate(root, path, loop_count,
				   &indexStartupCost, &indexTotalCost,
				   &indexSelectivity, &indexCorrelation,
				   &index_pages);

	...
	/* all costs for touching index itself included here */
	startup_cost += indexStartupCost;
	run_cost += indexTotalCost - indexStartupCost;

	/* estimate number of main-table tuples fetched */
	tuples_fetched = clamp_row_est(indexSelectivity * baserel->tuples);
	...
```

这里有三件事要看清:

- **查索引的代价交给 AM 自己算**:`amcostestimate = index->amcostestimate`——不同索引类型(B 树、Hash、GIN…)各有自己的代价估算函数,B 树的是 `btcostestimate`。这是"统一接口、各自实现":优化器不关心 B 树和 GIN 内部细节,只问 AM"扫这个索引要花多少、命中多少行"。`indexSelectivity` 就是"命中表行的比例",它直接决定回表多少次。
- **`tuples_fetched = indexSelectivity * baserel->tuples`**:这是关键一行——**索引扫描要回表多少次,等于"选择性 × 表行数"**。这就是本章反复说的"返回行越多,回表越多"。选择性高(点查询,selectivity ≈ 0),回表几次;选择性低(返回大部分行,selectivity ≈ 0.9),回表几百万次。

再看回表代价怎么算——这是"随机 vs 顺序"权衡的核心:

> [src/backend/optimizer/path/costsize.c:643-L744](../postgresql-17.0/src/backend/optimizer/path/costsize.c#L643-L744)

```c
	/*
	 * Estimate number of main-table pages fetched, and compute I/O cost.
	 *
	 * When the index ordering is uncorrelated with the table ordering,
	 * we use an approximation proposed by Mackert and Lohman ... to
	 * compute the number of pages fetched, and then charge
	 * spc_random_page_cost per page fetched.
	 *
	 * When the index ordering is exactly correlated with the table ordering
	 * (just after a CLUSTER, for example), the number of pages fetched should
	 * be exactly selectivity * table_size.  What's more, all but the first
	 * will be sequential fetches, not the random fetches that occur in the
	 * uncorrelated case.  So if the number of pages is more than 1, we
	 * ought to charge
	 *		spc_random_page_cost + (pages_fetched - 1) * spc_seq_page_cost
	 ...
	 */
	...
	else
	{
		/* Normal case: apply the Mackert and Lohman formula ... */
		pages_fetched = index_pages_fetched(tuples_fetched,
											baserel->pages,
											(double) index->pages,
											root);
		...
		rand_heap_pages = pages_fetched;

		/* max_IO_cost is for the perfectly uncorrelated case (csquared=0) */
		max_IO_cost = pages_fetched * spc_random_page_cost;

		/* min_IO_cost is for the perfectly correlated case (csquared=1) */
		pages_fetched = ceil(indexSelectivity * (double) baserel->pages);
		...
		if (pages_fetched > 0)
		{
			min_IO_cost = spc_random_page_cost;
			if (pages_fetched > 1)
				min_IO_cost += (pages_fetched - 1) * spc_seq_page_cost;
		}
```

这段注释和代码把本章的灵魂讲透了。注意它算了**两个极端**:

- **`max_IO_cost`(完全不相关)**:`pages_fetched * spc_random_page_cost`——假设回表的每一页都是**随机读**(4.0/页)。这是索引和堆表物理顺序完全错乱的情况(TID 散落各处)。`pages_fetched` 用 Mackert-Lohman 公式估算,它会考虑"多个 TID 落在同一页就只读一次"的缓存效应。
- **`min_IO_cost`(完全相关)**:`spc_random_page_cost + (pages_fetched - 1) * spc_seq_page_cost`——第一个堆页是随机读(4.0),但因为是完全相关(索引顺序 = 堆物理顺序),剩下的页是**顺序挨着的**,按顺序读(1.0/页)。这是 `CLUSTER` 过或按索引顺序插入的表的情况。

真实情况介于两者之间,代码用 `indexCorrelation`(索引顺序和堆物理顺序的相关性)在 max 和 min 之间**线性插值**。这就是为什么"数据是否按索引键有序地物理存储"会显著影响索引扫描性能——有序时回表近乎顺序读(便宜),无序时全是随机读(贵)。

> **回扣三个"索引反而慢"的场景**:看这段代码就全通了——
> - 小表:`pages_fetched` 小,但每页都是 4.0 的随机价,启动开销(`indexStartupCost`)还摆在那,总代价高过 SeqScan 的 1.0/页。
> - 返回大量行:`tuples_fetched` 大 → `pages_fetched` 大 → `max_IO_cost` 线性涨,全是随机价。
> - 低基数:`indexSelectivity` 接近 0.5 → 同上。
>
> 优化器就是靠这套代价模型,自动判断"这次该不该走索引"。它不是靠规则"有索引就走",而是**算账**:谁的 `total_cost` 小,走谁。

---

## 章末小结

### 本章讲了什么

- **没有索引,查找是 O(n)**:顺序扫描把整张表从头扫到尾,代价和表大小成正比。表越大越慢,线性恶化——10 亿行的点查询要扫几百万页。
- **索引把查找降到 O(log n)**:B 树每往下一层搜索空间缩小一个倍数(扇出 F),树高 h ≈ log_F(n)。10 亿行也只要读约 5 个索引页。对照表钉死了这个碾压性差距。
- **但索引扫描还要回表**:拿到 TID 后要随机读一个堆页取整行。这步是 O(1) 但是**随机 I/O**,是"索引有时反而慢"的根源。
- **三种索引反而慢的场景**:① 小表(启动开销没省回来);② 返回大部分行(随机回表堆积);③ 低基数列(索引几乎没筛除能力)。共同根都是"回表的随机 I/O 压过了顺序扫描的顺序 I/O"。
- **PG 用代价模型量化这个权衡**:`spc_seq_page_cost=1.0` vs `spc_random_page_cost=4.0`;`cost_seqscan` 算全表扫(每页 1.0,O(n)),`cost_index` 算索引扫(查索引 + 随机回表,用相关性在 max/min IO 代价间插值)。优化器选 `total_cost` 小的路径。
- **索引的两大代价**:空间(索引是键值+TID 的额外拷贝)+ 写放大(每次 INSERT/UPDATE/DELETE 都要同步维护所有索引)。
- **PG 索引是二级结构**:堆表之外独立存在,叶子存 TID 指回堆,完整数据只在堆里有一份。预告了第 13 章回表与仅索引扫描。

### 回扣主线:快 vs 不丢不乱 + 三个本性

本章整个服务"**快**"那一侧——索引的全部意义就是"别扫全表、别做无用功",把查找从线性变成对数。它直接对付的是数据的**第三个本性**:"用户说**要什么**(id=42),机器只会**按位置读**"这个错位。索引就是填这道鸿沟的那个"翻译官":把"按值查"翻译成"按位置读",而且翻译得快。

> 注意一个张力:索引在服务"快",但它本身**引入了写的代价**(写放大)。这是"快"这一侧内部的权衡——查询快了,写入慢了。数据库的设计处处是这样的取舍,没有免费的午餐。后续 P4 会看到,PG 的 MVCC 还会让这个写放大雪上加霜(UPDATE 写新版本,索引也要跟着插新 TID),第 21 章的 HOT 优化就是来治这个痛的。

### 想继续深入

- 代价模型全集:[src/backend/optimizer/path/costsize.c](../postgresql-17.0/src/backend/optimizer/path/costsize.c),重点看 `cost_seqscan`(L284)、`cost_index`(L549)、`cost_bitmap_heapscan`(位图扫描,介于两者之间,第 13 章会提)。
- 代价常量:`seq_page_cost` / `random_page_cost` / `cpu_tuple_cost` 的定义与可配置性,见 [src/backend/utils/misc/postgresql.conf.sample](../postgresql-17.0/src/backend/utils/misc/postgresql.conf.sample) 和 guc_tables.c。
- B 树 AM 怎么注册自己、怎么估算代价:[src/backend/access/nbtree/nbtree.c](../postgresql-17.0/src/backend/access/nbtree/nbtree.c) 的 `bthandler`(L101)和 `src/backend/access/nbtree/nbtcostestimate.c`。

---

> O(n) 和 O(log n) 的较量讲完了,索引的"为什么"立住了。但 B 树到底长什么样?为什么是"平衡的**多路**树",而不是二叉树、红黑树、哈希表?一个 B 树节点(一页)内部怎么摆、插入满了怎么分裂、删空了怎么合并?翻开 **第 11 章 · B 树:平衡的多路查找**——我们钻进 PG 默认索引的内部结构。
