# 第 12 章 · 索引家族:Hash / GiST / GIN / BRIN / SP-GiST

> **前置**:你需要先读过[第 11 章《B 树》](P3-11-索引家族-Hash-GiST-GIN-BRIN.md)——它讲了 PG 默认的 B 树:一棵平衡多路查找树,O(log n) 等值和范围通吃。但 B 树并不是万能:全文检索里 `to_tsvector` 拆出来的成千上万个词、地理空间里的"找离我最近的红点"、一张按时间顺序写入的几百 GB 日志表——这些场景硬上 B 树,要么建不出来,要么建出来比全表扫还慢。本章就来看 PG 为什么还要准备另外五种索引。

> **核心问题**:为什么 B 树之外,还要 Hash、GiST、GIN、BRIN、SP-GiST 这么多种?它们各自解决 B 树**解决不了**的哪类查询?
>
> 答案一句话:**没有一种数据结构能同时擅长所有查询形态。** 等值查询、范围查询、近邻查询(KNN)、全文检索、稀疏大表扫描——每一种查询背后对应一种最优的数据结构;把数据按错误的结构组织,再调参数也没用。PG 把这些结构做成一个个"索引访问方法(index access method)",让你**按查询形态选对工具**——这比调参数管用百倍。
>
> **读完本章你会明白**:每种索引为哪类查询而生、数据结构骨架长什么样、它付出了什么代价;为什么"用对了索引类型"是 PG 里性价比最高的一次优化;以及 PG 用一个统一的接口(`IndexAmRoutine`)+ 一张系统目录(`pg_am`)把这么多不同的数据结构**统管**起来的精妙设计——这套设计让你甚至能自己写一个全新的索引类型塞进去。

> **如果一读觉得太难**:先记三件事——① B 树擅长等值和范围,但对**多值列(数组/全文)和近邻搜索**束手无策。② 每种索引背后是一种数据结构:Hash(桶+哈希)、GiST(平衡树框架)、GIN(倒排:键→文档列表)、BRIN(块范围 min/max)、SP-GiST(空间分区)。③ 选索引看"数据分布 + 查询形态"——时间序列用 BRIN、全文/数组用 GIN、近邻/几何用 GiST 或 SP-GiST。其余细节第二遍配合源码再抠。

---

## 一、先看清"B 树到底不擅长什么"

第 11 章我们说,B 树是 PG 的默认索引,绝大多数场景它都是对的。但"绝大多数"之外,有几个地方 B 树**根本做不到**,或者**做了也是灾难**。这一节把这几个场景逐个摆出来——本章剩下五种索引,每一个都是为治其中一个而生。

### 场景一:纯等值查询,而且键的空间巨大

B 树查找是 O(log n) 的——这个复杂度对绝大多数场景都够好。但如果你有一张表,只做一种查询:`WHERE key = ?`(纯等值,从不范围),而且键的数量极大(比如几十亿个不同的 session token)。

> **不这样会怎样**:B 树为了支持范围查询,得在内部节点维护"有序"——每个键在树里有个精确的位置,左右子树各管一段区间。这套有序结构带来两个代价:一是查找要做多次比较(每层比一次,树高次),二是节点里存的是完整键值(键很长时,一个 8KB 页能塞的键就少,树就更高)。对**纯等值**查询来说,这套"有序"的开销是白花的——你根本不需要知道"42 的前一个是谁、后一个是谁",你只想知道"42 在不在"。

这就是 Hash 索引的地盘:**只要等值,不要有序**。

### 场景二:多值列——一行里有成百上千个"词"

B 树的索引项是"一行的某个列值 → 这一行"。但有些列**一个单元格里塞着一堆值**:全文搜索里的 `tsvector`(一段文本拆出的所有词)、数组列 `int[]`、JSONB 里的标签集合。

> **不这样会怎样(以全文搜索为例)**:假设你有一张文章表,想查"内容包含 'database' 的所有文章"。B 树怎么做?它只能给"整篇文章的 `tsvector`"建索引——但每篇文章的 `tsvector` 都不一样,你查 `'database'` 这个词,B 树的 `=` 在这儿完全不适用(没有两个 `tsvector` 相等),`LIKE '%database%'` 又用不上 B 树前缀(第 11 章说过,`LIKE 'x%'` 能用索引,`LIKE '%x%'` 不行)。结果就是:B 树对"这篇文档包含某个词吗"这种查询**毫无办法**,只能全表扫 + 逐篇解析。

这就是 GIN(倒排索引)的地盘:**把"哪个词出现在哪些文档里"这张映射表直接建出来**。

### 场景三:近邻搜索和几何查询——"找最近的""找相交的"

地理、图像、推荐系统里常见这类查询:"找离我(经纬度)最近的 10 家咖啡店""哪些多边形和这个区域相交""哪些图片的特征向量离这张图最近"。这些查询的核心是**一个"距离"或"相交"的运算符**,而不是"等于"或"大小"。

> **不这样会怎样**:B 树只能按**一维有序**的键来组织。二维坐标 `(x, y)` 没有天然的"大小序"——你硬要按 x 排,那"离我最近"这种同时看 x 和 y 距离的查询,B 树根本无从下手(按 x 近的不一定 y 也近)。把多维数据压成一维(比如按 x 排)会完全丢失空间邻近性。这类查询需要的是一种**能表达"区域包含/相交"**的数据结构。

这就是 GiST 和 SP-GiST 的地盘:**支持自定义运算符的平衡树 / 空间分区树**。

### 场景四:稀疏大表——海量、但物理上有序的时间序列

物联网、日志、监控这类场景:一张表几亿到几十亿行,按时间顺序追加写入。查询几乎都是"某段时间内的数据"——`WHERE ts BETWEEN '2026-06-01' AND '2026-06-02'`。

> **不这样会怎样**:给这种表建 B 树索引,索引本身就可能几十 GB——因为 B 树要为**每一行**存一个索引项(行指针 + 键值)。几十亿行的表,索引项几十亿条,占的空间和堆表本身一个量级。而且写入时每来一行都要更新 B 树,写放大严重。可仔细想想:因为数据是**按时间顺序写入**的,相邻的行在磁盘上**物理上也相邻**——同一段页里,时间戳的 min/max 范围很窄。我们其实根本不需要精确到每一行,只需要知道"这 128 页的一段里,时间戳在不在我要的范围"——如果整段都不在,这 128 页直接跳过;如果在,再进去细看。这种"粗粒度摘要"对这种**物理有序的大表**就够用了,而它的空间占用能比 B 树小几个数量级。

这就是 BRIN(块范围索引)的地盘:**不为每行建索引,只为每段页建一个 min/max 摘要**。

### 小结:五种索引,各管一种查询形态

把上面四个场景(等值被 Hash 拆出去、多值被 GIN 拆出去、近邻/几何被 GiST/SP-GiST 拆出去、稀疏大表被 BRIN 拆出去)汇成一张表:

| 索引类型 | 为哪类查询而生 | 数据结构骨架 | 典型运算符 | 主要代价 |
|---|---|---|---|---|
| **B 树** | 等值 + 范围(通用) | 平衡多路查找树 | `=` `<` `>` `BETWEEN` `LIKE 'x%'` | 通用但有上限(多值/近邻/稀疏大表不擅长) |
| **Hash** | 纯等值 | 哈希桶 + 溢出页 | `=`(仅此一种) | 只支持 `=`,不支持范围/排序;旧版本(10 前)有崩溃安全问题 |
| **GiST** | 范围 / 几何 / 近邻(KNN) | 平衡树框架(可自定义运算符) | `&&` `@>` `<->`(距离) `<%>` | 插入要算 penalty、可能不平衡;查找可能扫多个分支 |
| **GIN** | 全文 / 数组 / JSONB 多值列 | 倒排:键 → 文档列表(posting list/tree) | `@>` `?` `@@`(全文匹配) `@@@` | 插入慢(要维护 posting list)、建索引慢;查询飞快 |
| **BRIN** | 海量且物理有序的大表(时间序列) | 块范围 min/max 摘要 | `=` `<` `>` `BETWEEN`(借 B 树运算符) | 只对物理有序数据有效;无序数据几乎没用 |
| **SP-GiST** | 非平衡结构(Trie / R-tree / 四叉树) | 空间分区树(可自定义分区规则) | 同 GiST,适合前缀/空间 | 不平衡、复杂度高;适合特定结构(如 IP 路由 Trie) |

> 记住这张表,等于记住了本章的一半。下面五种,我们一个一个把"为什么"讲透——重点不是它们的 SQL 用法,而是**它们各自的数据结构为什么是为那种查询形态量身定做的**。

---

## 二、Hash 索引:只要等值,就别背"有序"的包袱

### 为哪类查询而生

Hash 索引只服务一种查询:**等值匹配**(`WHERE key = ?`)。它的核心思想极其朴素:把键扔进一个哈希函数,算出一个整数,这个整数决定它落到哪个"桶(bucket)"里。查找时同样算一次哈希,直接去那个桶里找——理想情况下**一次定位,O(1)**。

### 不这样会怎样:B 树的"有序"对纯等值是白花的

第 11 章讲过,B 树为了支持范围查询,内部节点必须维护键的有序关系。这套有序带来了:

- 查找要做 `O(log n)` 次比较(树高次,每次在节点内二分);
- 节点要存完整键(键长 → 一页能塞的键少 → 树更高 → 比较更多)。

如果你**只做等值、从不做范围**,这些代价全是浪费。Hash 索引干脆把"有序"扔掉:键经过哈希函数后**彻底打乱**,只为"快速定位到某个桶"服务。一个键在哪个桶,完全由它的哈希值决定,和它本身的大小、邻居无关。

> 源码里,Hash 索引把键的类型统一存成 `INT4OID`(32 位整数哈希值),正是这个"只存哈希、不存原值排序"思想的体现——见 [hashhandler 的 `amroutine->amkeytype = INT4OID`](../postgresql-17.0/src/backend/access/hash/hash.c#L82)。

### 数据结构骨架:桶 + 溢出页 + 位图页

Hash 索引在磁盘上是这样的:

```text
  元页(meta page)         ← 记录桶总数、split 点、空闲页位图
     │
     ▼
  桶 0     桶 1     桶 2  ... 桶 N      ← 每个"桶"的主页(bucket page)
   │        │        │         │
   ▼        ▼        ▼         ▼
  溢出页   (空)    溢出页    (主页满了就链溢出页)
```

- **元页(metapage)**:记录当前有多少个桶、下一次该分裂哪个桶、以及一张空闲页的位图。
- **桶页(bucket page)**:一个桶的"主"页,装着哈希值落在这个桶里的所有索引项(键哈希值 + 行指针 TID)。它就是上面 [hashhandler](../postgresql-17.0/src/backend/access/hash/hash.c#L57-L109) 挂的 `hashinsert`/`hashgettuple` 操作的对象。
- **溢出页(overflow page)**:当一个桶装满了(一页 8KB 塞不下),就在这个桶后面链一个溢出页,接着装。桶页和它的溢出页用 `hasho_nextblkno` 串成链表。

这套"桶 + 溢出链"在源码里体现在页的 opaque 数据(页尾特殊区)上:

> [src/include/access/hash.h:77-86](../postgresql-17.0/src/include/access/hash.h#L77-L86)

```c
typedef struct HashPageOpaqueData
{
	BlockNumber hasho_prevblkno;	/* see above */
	BlockNumber hasho_nextblkno;	/* see above */
	Bucket		hasho_bucket;	/* bucket number this pg belongs to */
	uint16		hasho_flag;		/* page type code + flag bits, see above */
	uint16		hasho_page_id;	/* for identification of hash indexes */
} HashPageOpaqueData;
```

`hasho_nextblkno` 就是溢出链的"下一个";`hasho_bucket` 标明这页属于哪个桶(桶页和它的溢出页都带同一个桶号);`hasho_flag` 用位区分这页是 `LH_BUCKET_PAGE`(桶主页)、`LH_OVERFLOW_PAGE`(溢出页)、`LH_BITMAP_PAGE`(位图页)还是 `LH_META_PAGE`(元页)——见 [src/include/access/hash.h:53-57](../postgresql-17.0/src/include/access/hash.h#L53-L57)。

### 一个键落在哪个桶:线性哈希的取模

把哈希值映射到桶号,用的是这套逻辑(线性哈希,linear hashing):

> [src/backend/access/hash/hashutil.c:124-135](../postgresql-17.0/src/backend/access/hash/hashutil.c#L124-L135)

```c
Bucket
_hash_hashkey2bucket(uint32 hashkey, uint32 maxbucket,
					 uint32 highmask, uint32 lowmask)
{
	Bucket		bucket;

	bucket = hashkey & highmask;
	if (bucket > maxbucket)
		bucket = bucket & lowmask;

	return bucket;
}
```

先拿哈希值和 `highmask`(一个形如 `0...011...1` 的掩码)做与运算,如果结果超过当前最大桶号,就再和一个更短的 `lowmask` 与一次,把它"折"回有效范围。这套 `highmask`/`lowmask` 的配合,是**线性哈希**的精髓:桶可以一个一个地分裂扩容,而不用一次性全表重哈希——分裂时只动一个桶,把它的内容按新掩码分到原桶和新桶。这样扩容代价被摊平了。

### Hash 的代价

1. **只支持 `=`**:能力位 `amcanorder = false`、`amcanorderbyop = false`([hash.c:64-65](../postgresql-17.0/src/backend/access/hash/hash.c#L64-L65))。范围查询、排序、`LIKE 'x%'` 全不行。
2. **不支持唯一约束**:`amcanunique = false`([hash.c:67](../postgresql-17.0/src/backend/access/hash/hash.c#L67))。
3. **不支持多列**:`amcanmulticol = false`([hash.c:68](../postgresql-17.0/src/backend/access/hash/hash.c#L68))——Hash 只能建在单列上。
4. **桶溢出时退化**:如果哈希函数分布不好(或者键大量重复),一个桶会链出长长的溢出页链,查找从 O(1) 退化成 O(链长)。

> **历史插曲**:PG 10 之前,Hash 索引被标为"不安全",因为崩溃后它的桶分裂状态没法用 WAL 正确恢复。PG 10 重写了 Hash 的 WAL 日志,它才变成一等公民。所以你可能在老资料里看到"别用 Hash 索引"——那已经是过时建议了。今天,如果你的查询**100% 是等值**、键很长(比如 256 字节的 token),Hash 在空间和速度上都能比 B 树略胜一筹。但因为 B 树的通用性和优化器的偏爱,实际项目里 Hash 用得很少——它更像是"为极致等值场景保留的专用工具"。

---

## 三、GiST:一棵可以"自定义运算符"的平衡树

### 为哪类查询而生

GiST(Generalized Search Tree,通用搜索树)是一个**框架**——它给你一棵平衡树的骨架(和 B 树一样,从根到叶,查找时一层层往下走),但**节点里存什么、怎么比较、怎么分裂**,全部由**用户提供的运算符类(opclass)**决定。这意味着:只要你给某种数据类型写好了"一致(consistent)""合并(union)""惩罚(penalty)""分裂(picksplit)"几个函数,GiST 就能替你建一棵能查它的索引。

PG 内置的 GiST opclass 覆盖:范围类型(`int4range` 等)、几何类型(point/box/polygon)、全文搜索的 `tsvector`(via `tsquery`)、以及近邻搜索(`<->` 距离运算符)。

### 不这样会怎样:多维/自定义运算符的查询,B 树无从下手

B 树的查找靠"键有序 + 二分"。但"一个盒子 A 是否和盒子 B 相交""点 P 离点 Q 多近"——这些运算符没有全序,没法二分。GiST 的解法是:**节点里不存单个键,而存一个"区域"(key union)——这个区域"包住"了它所有子树里的所有键。** 查找时,用你的运算符问这个区域:"你要找的东西可能在你这棵子树里吗?"(consistent 函数)——可能就往下走,不可能就剪枝。

这就是为什么 GiST 适合**范围/几何/近邻**:它的节点是"包围盒",天然能表达"包含/相交/邻近"。

### 数据结构骨架:平衡树,节点存"区域"

```text
              根: 区域 R(包住所有数据)
             /        \
       区域 R1         区域 R2
       /    \          /    \
     叶子    叶子      叶子    叶子
   (键→TID) (键→TID) (键→TID) (键→TID)
```

每个非叶节点存一个"区域"(对几何是 bounding box,对范围是区间并集)。查找 `WHERE col && box` 时,从根开始,问每个子节点的区域:"你和 box 相交吗?"(consistent),不相交的整棵子树剪掉。

### 四个用户可定义的关键函数

GiST 的"通用"体现在:它把数据结构的决策权下放给 opclass。opclass 要提供这几个核心支持函数(见 [src/include/access/gist.h:30-37](../postgresql-17.0/src/include/access/gist.h#L30-L37)):

```c
#define GIST_CONSISTENT_PROC			1
#define GIST_UNION_PROC					2
#define GIST_COMPRESS_PROC				3
#define GIST_PENALTY_PROC				5
#define GIST_PICKSPLIT_PROC				6
#define GIST_EQUAL_PROC					7
#define GIST_DISTANCE_PROC				8
```

- **`consistent`**:给定一个节点的区域和一个查询(比如"和 box 相交?"),返回 true/false(可能匹配就 true,用于决定要不要下探这棵子树)。这是**查找的剪枝核心**。
- **`union`**:把几个区域合并成一个更大的区域(父节点的区域就是这么算出来的,要"包住"所有子节点)。
- **`penalty`**:插入一个新键时,把它塞进哪个子节点代价最小?(区域要扩大多少?)——B 树没有这一步,因为 B 树的"塞哪"由键的大小唯一决定;GiST 的键没有全序,得**算**。
- **`picksplit`**:一个节点满了要分裂,怎么把它的内容分成两堆?(分成哪两堆,直接决定以后查找要剪多少枝。)
- **`distance`**:近邻搜索(KNN)用——给一个区域和一个查询点,返回"这棵子树里最近可能的距离下界",用于优先队列排序。

**插入路径的 penalty 选择**在源码里清晰可见——`gistchoose` 函数遍历当前页的所有子节点,对每个算 penalty,选最小的那个下探:

> [src/backend/access/gist/gistutil.c:372-469](../postgresql-17.0/src/backend/access/gist/gistutil.c#L372-L469)(节选核心循环)

```c
OffsetNumber
gistchoose(Relation r, Page p, IndexTuple it,	/* it has compressed entry */
		   GISTSTATE *giststate)
{
	...
	for (i = FirstOffsetNumber; i <= maxoff; i = OffsetNumberNext(i))
	{
		IndexTuple	itup = (IndexTuple) PageGetItem(p, PageGetItemId(p, i));
		...
		for (j = 0; j < IndexRelationGetNumberOfKeyAttributes(r); j++)
		{
			...
			/* Compute penalty for this column. */
			...
			usize = gistpenalty(giststate, j, &entry, IsNull,
								&identry[j], isnull[j]);
			...
		}
	}
}
```

而 `gistpenalty` 本身只是个壳,真正干活的是 opclass 注册的 `penaltyFn`:

> [src/backend/access/gist/gistutil.c:722-750](../postgresql-17.0/src/backend/access/gist/gistutil.c#L722-L750)

```c
float
gistpenalty(GISTSTATE *giststate, int attno,
			GISTENTRY *orig, bool isNullOrig,
			GISTENTRY *add, bool isNullAdd)
{
	float		penalty = 0.0;

	if (giststate->penaltyFn[attno].fn_strict == false ||
		(isNullOrig == false && isNullAdd == false))
	{
		FunctionCall3Coll(&giststate->penaltyFn[attno],
						  giststate->supportCollation[attno],
						  PointerGetDatum(orig),
						  PointerGetDatum(add),
						  PointerGetDatum(&penalty));
		...
	}
	...
	return penalty;
}
```

注意 `FunctionCall3Coll(&giststate->penaltyFn[attno], ...)`——它调用的就是 opclass 注册的那个 penalty 函数(对几何是"box 要扩大多少面积",对范围是"区间要延伸多少")。**这就是"通用"二字的代码体现:GiST 引擎不知道你在索引什么,它只管调用你提供的函数。**

### GiST 的代价

1. **插入要算 penalty**:每次插入都要在每一层算一遍"塞哪个子节点最划算",比 B 树的"二分定位"贵。
2. **可能不平衡**:GiST 保证平衡(所有叶子同深),但不保证"均匀"——某个区域可能塞得特别满,导致查找要扫很多分支。`picksplit` 写得好不好,直接决定性能。
3. **查找不是一次定位**:consistent 可能命中多个子树(区域有重叠时),都要下探。
4. **能力位**:`amcanorderbyop = true`([gist.c:67](../postgresql-17.0/src/backend/access/gist/gist.c#L67))——这正是 GiST 能做 KNN(按 `<->` 距离排序)的关键,而 B 树做不到。

---

## 四、GIN:倒排索引,一个键映射到很多行

### 为哪类查询而生

GIN(Generalized Inverted Index,通用倒排索引)专为**多值列**而生:全文搜索(`tsvector`)、数组(`int[]`、`text[]`)、JSONB。它的查询是"这一行里**包含**某个元素吗"——`@>`(包含)、`?`(存在键)、`@@`(全文匹配)。

### 不这样会怎样:B 树对"包含"查询彻底失效

回到本章开头的"场景二"。一篇文档的 `tsvector` 是 `{'database':1, 'index':2, 'postgres':3}` 这样的结构——一个单元格里塞着好多个词。你建 B 树,只能按"整个 tsvector"建——但你要查的是"包含 'database' 这个词的文档",这个谓词在 B 树的 `=`/`<` 体系里**无法表达**。

> **关键直觉**:搜索引擎(如 Lucene、Elasticsearch)的核心数据结构就是倒排索引(inverted index)。它解决的是同一个问题——给定一个词,快速找到所有包含它的文档。PG 的 GIN 就是把这套搬进了数据库,让你不用外挂搜索引擎,直接对 SQL 列建倒排索引。

### 数据结构骨架:键 → 文档列表(倒排)

倒排索引的核心是:**不是"文档 → 词",而是"词 → 文档列表"**。

```text
  GIN 索引
  ├── 键 'database'  →  [文档1, 文档3, 文档7, 文档42, ...]   (posting list/tree)
  ├── 键 'index'     →  [文档1, 文档2, 文档9, ...]
  ├── 键 'postgres'  →  [文档3, 文档7, ...]
  └── ...
```

左边是"键"(词、数组元素、JSONB 键),按 B 树组织(叫 **entry tree**,键有序,方便查找);右边是"包含这个键的所有行的 TID 列表",叫 **posting list**(行数少时,内联在 entry 里)或 **posting tree**(行数多时,拆成一棵独立的树)。

这套"键树 + 每个键挂一个 TID 集合"的结构,在源码里体现在 entry 树的叶子元组上:

> [src/include/access/ginblock.h:229-240](../postgresql-17.0/src/include/access/ginblock.h#L229-L240)

```c
#define GinGetNPosting(itup)	GinItemPointerGetOffsetNumber(&(itup)->t_tid)
#define GinSetNPosting(itup,n)	ItemPointerSetOffsetNumber(&(itup)->t_tid,n)
#define GIN_TREE_POSTING		((OffsetNumber)0xffff)
#define GinIsPostingTree(itup)	(GinGetNPosting(itup) == GIN_TREE_POSTING)
#define GinSetPostingTree(itup, blkno)	( GinSetNPosting((itup),GIN_TREE_POSTING), ItemPointerSetBlockNumber(&(itup)->t_tid, blkno) )
#define GinGetPostingTree(itup) GinItemPointerGetBlockNumber(&(itup)->t_tid)

#define GIN_ITUP_COMPRESSED		(1U << 31)
#define GinGetPostingOffset(itup)	(GinItemPointerGetBlockNumber(&(itup)->t_tid) & (~GIN_ITUP_COMPRESSED))
#define GinGetPosting(itup)			((Pointer) ((char*)(itup) + GinGetPostingOffset(itup)))
```

这几行宏藏着 GIN 的两个关键设计:

- **posting list 内联 vs posting tree**:一个 entry 的叶子元组,如果对应的 TID 不多,就**直接把 TID 列表压缩后内联在元组里**(`GinGetPosting` 取出内联的 posting list);如果 TID 多到一页塞不下,就标成 `GIN_TREE_POSTING`,把 `t_tid` 改成**指向一棵独立的 posting tree 的根**(`GinGetPostingTree`)。这个"小则内联、大则建树"的自动切换,是 GIN 兼顾空间和查询效率的精髓。
- **压缩**:posting list 是**压缩存储**的(varbyte 编码,TID 之间存差值)。因为一个热门词可能对应几百万个 TID,不压缩空间会很吓人。压缩结构是 `GinPostingList`:

> [src/include/access/ginblock.h:336-344](../postgresql-17.0/src/include/access/ginblock.h#L336-L344)

```c
typedef struct
{
	ItemPointerData first;		/* first item in this posting list (unpacked) */
	uint16		nbytes;			/* number of bytes that follow */
	unsigned char bytes[FLEXIBLE_ARRAY_MEMBER]; /* varbyte encoded items */
} GinPostingList;
```

`first` 存第一个 TID 的完整值,后面的 `bytes` 是后续 TID 的 varbyte 差值编码——相邻 TID 通常很接近(同一页里的行),差值很小,压缩率极高。

### 一个值怎么变成"键":extractValue

GIN 索引插入一行时,不是直接把行的值塞进去,而是先调用 opclass 的 `extractValue` 函数,把这个值**拆成一组键**。比如一篇文章的 `tsvector` 会被拆成它包含的所有词;一个数组 `[1,2,3]` 被拆成 `1`、`2`、`3` 三个键。然后每个键分别插进 entry 树,并把这一行的 TID 加到它们的 posting list 里。

这几个支持函数定义在 [src/include/access/gin.h:22-29](../postgresql-17.0/src/include/access/gin.h#L22-L29):

```c
#define GIN_COMPARE_PROC			   1
#define GIN_EXTRACTVALUE_PROC		   2
#define GIN_EXTRACTQUERY_PROC		   3
#define GIN_CONSISTENT_PROC			   4
#define GIN_COMPARE_PARTIAL_PROC	   5
#define GIN_TRICONSISTENT_PROC		   6
```

- **`extractValue`**:插入时,把一个单元格里塞的多个值拆成一组键(给索引用)。
- **`extractQuery`**:查询时,把用户的查询条件(比如一个 `tsquery` `'database & index'`)也拆成一组要查的键。
- **`consistent`**:给定一行匹配了查询里的哪些键,判断这行**到底满不满足**查询(比如 `&` 要求两个词都在)。

### GIN 的代价:插入慢,但查询飞快

1. **插入代价高**:插入一行多值列,要拆出 N 个键,每个键都要去 entry 树找/插,还要把 TID 加进对应的 posting list(可能触发 posting list 的重压缩或升级成 posting tree)。所以 GIN 的写入比 B 树贵得多——一行可能触发几十上百次索引更新。
2. **pending list**:为了缓解插入开销,GIN 有个"待处理列表(pending list)"——新插入的键先堆在这里(快),定期或查询前再批量合并进主索引(慢但摊平了)。`amusemaintenanceworkmem = true`([ginutil.c:58](../postgresql-17.0/src/backend/access/gin/ginutil.c#L58))正是为此——GIN 用 `maintenance_work_mem` 而不是 `work_mem` 来控制这个批量合并的内存。
3. **查询飞快**:查"包含 'database' 的文档",直接在 entry 树定位到 'database' 这个键,拿到它的整个 posting list——一次定位,拿到所有匹配行。多词查询(`&`)就是取几个 posting list 的交集。这就是为什么全文搜索用 GIN 能比全表扫快几个数量级。
4. **只支持 bitmap 扫描**:`amgettuple = NULL`([ginutil.c:79](../postgresql-17.0/src/backend/access/gin/ginutil.c#L79))——GIN 不支持普通的 IndexScan(逐行返回),只支持 BitmapScan(一次性收集所有 TID 再去堆里取)。因为一个查询可能命中海量 TID,逐行返回 + 回表的随机 I/O 会很糟,先在内存里攒成位图、再批量有序回表,效率高得多。

> 这正是 GIN 和 B 树最本质的差异:B 树是"一行一个索引项",GIN 是"一个键对应很多行"。这个"一对多"的倒排结构,让它在多值列查询上碾压 B 树,代价是写入时维护这个倒排映射的开销。

---

## 五、BRIN:块范围索引,为物理有序的大表而生

### 为哪类查询而生

BRIN(Block Range Index,块范围索引)专为一种特殊场景:**海量、且物理上按某个顺序有序的大表**——最典型的是时间序列(日志、监控、物联网),按时间追加写入,查询多是"某段时间"。

它的核心思想颠覆了"索引"的常识:**BRIN 根本不索引每一行,它索引的是"一段页的摘要"。**

### 不这样会怎样:给几十亿行的大表建 B 树,索引本身就成了灾难

一张 10 亿行的时间序列表,建 B 树索引,索引可能有几十 GB——因为 B 树要为**每一行**存一个 `(时间戳, TID)` 项。更糟的是,时间序列写入极频繁,每来一条都要更新 B 树,写放大严重。

> **关键洞察**:时间序列表是**按时间顺序追加写入**的,所以"相邻的行在磁盘上也相邻"——表的第 100~227 页(假设 128 页一段)里,时间戳几乎全落在某个很窄的区间(比如 `2026-06-15 10:00` 到 `2026-06-15 10:05`)。我们根本不需要知道每一行的精确时间戳,只需要知道**"第 100~227 页这段,时间戳的 min 是 10:00、max 是 10:05"**——如果查询要的是 `2026-06-16` 的数据,这段的 max(10:05) 远小于查询起点,整段 128 页**直接跳过**,一个字节都不用读。

这就是 BRIN 的精髓:**用"一段页的 min/max"这种极粗的摘要,换极小的空间占用。** 一个 10 亿行的表,BRIN 索引可能只有几 MB——比 B 树小几千倍。

### 数据结构骨架:块范围 + 摘要 + 反向映射

```text
  堆表(按块编号)
  块 0     块 1     ...  块 127  | 块 128  ...  块 255 | 块 256 ...
  └──────── 第 1 个范围 ────────┘ └──── 第 2 个范围 ───┘
       min=10:00  max=10:05            min=10:05 max=10:10
       hasnulls=false                  hasnulls=true
              │                                │
              ▼                                ▼
       BRIN 摘要元组(块 0-127)        BRIN 摘要元组(块 128-255)
              │                                │
              └──────── 反向映射(revmap) ──────┘
                 (块范围号 → 摘要元组位置)
```

- **块范围(block range)**:默认 128 个连续页(可配 `pages_per_range`)算一段。
- **摘要元组(summary tuple)**:每段一个,记录这段里每个被索引列的摘要。对最常用的 min/max opclass,摘要就是 `[min, max]`;还带两个 null 标志位(`bv_hasnulls`/`bv_allnulls`,见 [brin_tuple.h:29-38](../postgresql-17.0/src/include/access/brin_tuple.h#L29-L38))。
- **反向映射(revmap)**:一张"块范围号 → 摘要元组在哪页哪个偏移"的表,让给定一个堆块号能快速找到它对应范围的摘要。

摘要元组的磁盘结构极其紧凑:

> [src/include/access/brin_tuple.h:63-78](../postgresql-17.0/src/include/access/brin_tuple.h#L63-L78)

```c
typedef struct BrinTuple
{
	/* heap block number that the tuple is for */
	BlockNumber bt_blkno;

	/* ---------------
	 * bt_info is laid out in the following fashion:
	 *
	 * 7th (high) bit: has nulls
	 * 6th bit: is placeholder tuple
	 * 5th bit: range is empty
	 * 4-0 bit: offset of data
	 * ---------------
	 */
	uint8		bt_info;
} BrinTuple;
```

`bt_blkno` 是这个范围对应的起始堆块号;`bt_info` 用一个字节塞了三个标志位(has nulls / placeholder / empty range)和数据偏移。每个摘要元组可能就十几个字节——这是 BRIN 空间占用极小的根源。

### min/max opclass:摘要怎么更新

BRIN 最常用的 opclass 是 `minmax`(还有 `bloom`、`minmax_multi` 等)。minmax opclass 的 `oi_nstored = 2`——每个被索引列在摘要里存**两个值:min 和 max**:

> [src/backend/access/brin/brin_minmax.c:34-54](../postgresql-17.0/src/backend/access/brin/brin_minmax.c#L34-L54)(节选)

```c
Datum
brin_minmax_opcinfo(PG_FUNCTION_ARGS)
{
	Oid			typoid = PG_GETARG_OID(0);
	BrinOpcInfo *result;
	...
	result->oi_nstored = 2;
	result->oi_regular_nulls = true;
	...
	result->oi_typcache[0] = result->oi_typcache[1] =
		lookup_type_cache(typoid, 0);
	...
}
```

插入新行时,`brin_minmax_add_value` 把新值和当前范围的 min/max 比:比 min 小就更新 min,比 max 大就更新 max,否则摘要不变([brin_minmax.c:64-120](../postgresql-17.0/src/backend/access/brin/brin_minmax.c#L64-L120))。这意味着**大多数插入根本不写索引**——只要新值落在现有 min/max 范围内,摘要元组一行都不改。

### 查询时:扫描摘要,粗筛块范围

`bringetbitmap` 是 BRIN 唯一的扫描入口(`amgettuple = NULL`,只支持 bitmap 扫描,见 [brin.c:289-290](../postgresql-17.0/src/backend/access/brin/brin.c#L289-L290))。它的主循环就是**遍历所有块范围的摘要,用 consistent 函数问"这个范围可能包含匹配行吗"**:

> [src/backend/access/brin/brin.c:727-744](../postgresql-17.0/src/backend/access/brin/brin.c#L727-L744)(节选)

```c
	for (heapBlk = 0; heapBlk < nblocks; heapBlk += opaque->bo_pagesPerRange)
	{
		bool		addrange;
		...
		tup = brinGetTupleForHeapBlock(opaque->bo_rmAccess, heapBlk, &buf,
									   &off, &size, BUFFER_LOCK_SHARE);
		if (tup)
		{
			...
			btup = brin_copy_tuple(tup, size, btup, &btupsz);
			...
		}
		/*
		 * For page ranges with no indexed tuple, we must return the whole
		 * range; otherwise, compare it to the scan keys.
		 */
```

对每个范围,取出摘要元组,用查询键和它的 min/max 比:查询值在 `[min, max]` 外,整段跳过;在范围内,把这段所有页加进位图(后面再实际读这些页细筛)。

注意第 752-754 行的注释——**没有摘要的范围(placeholder 或空),BRIN 只能保守地把整段都返回**。这就是 BRIN 的"假阳性":它告诉你"这段可能匹配",但进去一读发现其实没有(因为 min/max 是粗粒度的,范围内不一定真有那个值)。所以 BRIN 之后还要回堆表细查——它是个**预过滤器**,不是精确定位器。

### BRIN 的代价和适用边界

1. **只对物理有序的数据有效**:这是 BRIN 的命门。如果数据是乱序的(比如随机写入、频繁更新),一段 128 页里 min/max 范围会极大(几乎覆盖全表),那 BRIN 的摘要等于"这段什么都有",查询时一段都剪不掉,索引白建。**BRIN 的效果和数据的物理有序程度成正比。**
2. **有假阳性,要回堆细筛**:BRIN 给的是"可能匹配的页段",不是精确行。它省的是"跳过大段不相关的页",代价是命中的页段还得进去读。
3. **不支持唯一、不支持精确单行定位**:`amcanunique = false`、`amgettuple = NULL`。
4. **`amsummarizing = true`**:这个能力位([brin.c:269](../postgresql-17.0/src/backend/access/brin/brin.c#L269))是 BRIN 独有的标记——它告诉系统"这个索引只在块粒度上存信息"。这也是为什么 BRIN 的插入可以很便宜(大多数时候只读不写摘要)。

> **什么时候选 BRIN**:表极大(几十 GB 以上)、按某列追加写入(时间戳最典型)、查询多是范围过滤——这三条同时满足,BRIN 是性价比之王(几 MB 索引换几个数量级的扫描剪枝)。如果数据无序或表小,别用 BRIN,用 B 树。

---

## 六、SP-GiST:空间分区,为非平衡结构而生

### 为哪类查询而生

SP-GiST(Space-Partitioned GiST,空间分区 GiST)和 GiST 是亲戚,但分区方式不同。GiST 是**平衡树**(像 B 树,所有叶子同深,节点分裂时数据重分布);SP-GiST 支持**非平衡、基于空间分区**的结构——典型如 Trie(字典树,前缀树)、四叉树/八叉树(空间四分)、R-tree 的某些变体。

PG 内置的 SP-GiST opclass 包括:文本前缀树(`text`、`inet` 的网络前缀路由)、几何点的四叉树(k-d 树近邻)。

### GiST 和 SP-GiST 的根本区别:怎么分裂

这是理解 SP-GiST 的关键。两者都是"通用框架树",但分裂策略不同:

- **GiST(数据驱动分裂)**:节点满了,调 `picksplit` 看"这些数据怎么分成两堆最合理",然后重分布。分裂后,父节点的"区域"要扩大(union)以包住新的两堆。树始终平衡。
- **SP-GiST(空间驱动分区)**:节点不是按"数据怎么分"分裂,而是按一个**分区规则(partition rule)**把空间切开。比如 Trie:按字符串的下一个字符分(26 个分支);四叉树:按象限分(4 个分支)。同一个内节点下的分支是**互不重叠的固定分区**,数据按规则落进对应的分区。这种树可以**很深且不平衡**(比如一个长前缀的数据会走很深),但每次下探都把搜索空间缩小一个确定的维度。

> **不这样会怎样**:某些数据天然就是"前缀/分区"结构——IP 路由表(`192.168.1.0/24`)、电话号码区号、字典里的单词。用 GiST(平衡树 + 区域包围盒)来索引它们,得反复算 penalty 和 union,既慢又不直观;用 SP-GiST 的 Trie,直接按字符一层层分,查找就是"顺着前缀走",极其自然。SP-GiST 给这类"天然分区"的数据一个匹配的骨架。

### 数据结构骨架:内节点存"前缀 + 节点标签"

SP-GiST 的内节点(internal tuple)由两部分组成:

- **前缀(prefix)**:可选。表示从根到这一层的"路径前缀"(比如 Trie 里到这一层已经匹配的字符串前缀)。
- **一组节点(node)**:每个节点有一个**标签(label)**,代表一个分区(比如下一个字符 'a'、'b'…)。数据按规则落进某个节点。

叶子节点存实际的键值 + TID。

这套结构体现在 SP-GiST 的核心支持函数上(见 [src/include/access/spgist.h:23-31](../postgresql-17.0/src/include/access/spgist.h#L23-L31)):

```c
#define SPGIST_CONFIG_PROC				1
#define SPGIST_CHOOSE_PROC				2
#define SPGIST_PICKSPLIT_PROC			3
#define SPGIST_INNER_CONSISTENT_PROC	4
#define SPGIST_LEAF_CONSISTENT_PROC		5
```

- **`config`**:告诉 SP-GiST 引擎"这个 opclass 的前缀类型、标签类型、叶子类型是什么"。见 [spgConfigOut](../postgresql-17.0/src/include/access/spgist.h#L41-L48):
  ```c
  typedef struct spgConfigOut
  {
  	Oid		prefixType;		/* Data type of inner-tuple prefixes */
  	Oid		labelType;		/* Data type of inner-tuple node labels */
  	Oid		leafType;		/* Data type of leaf-tuple values */
  	bool	canReturnData;	/* Opclass can reconstruct original data */
  	bool	longValuesOK;	/* Opclass can cope with values > 1 page */
  } spgConfigOut;
  ```
- **`choose`**:给定一个要插入的值和当前内节点(它的前缀 + 节点标签),决定"这个值应该走哪个节点",或者"需要给这个节点加个新分支",或者"这里要分裂"。见 [spgChooseOut 的三种结果](../postgresql-17.0/src/include/access/spgist.h#L67-L105):`spgMatchNode`(走已有节点)、`spgAddNode`(加新分支)、`spgSplitTuple`(改前缀再分)。
- **`picksplit`**:节点满了要分裂时,怎么切。
- **`inner_consistent`**:查找时,给定查询和一个内节点,哪些子节点可能匹配(剪枝)。
- **`leaf_consistent`**:到了叶子,精确判断这个叶子值满不满足查询。

### SP-GiST 的代价

1. **不平衡**:树可能很深(长前缀),查找深度不固定。但有 `longValuesOK` 机制处理超长值(超过一页的)。
2. **适用面窄**:只对"天然分区"的数据(前缀、象限)效果好;对一般数据,用 GiST 更合适。
3. **能力位**:`amcanorderbyop = true`([spgutils.c:52](../postgresql-17.0/src/backend/access/spgist/spgutils.c#L52))——和 GiST 一样,支持 KNN 近邻排序。`amcanmulticol = false`——只支持单列。

> **GiST 还是 SP-GiST?** 经验法则:如果你的数据有天然的"前缀/分区"结构(IP、字符串前缀、空间象限),用 SP-GiST;如果是"区域包含/相交"且没有固定分区(范围、bounding box),用 GiST。两者都支持自定义运算符和 KNN。

---

## 关键源码精读:`IndexAmRoutine`——五种索引的"能力清单"

讲了五种索引各自的数据结构,现在看它们在 PG 里是怎么被**统一管理**的。这是 PG 索引子系统最漂亮的设计之一:不管 B 树、Hash、GiST、GIN、BRIN、SP-GiST 内部多不同,在 PG 看来它们都是一个个"索引访问方法(access method)",每个实现同一个接口——`IndexAmRoutine`。

### 一个结构体,声明这个索引"能干什么、怎么干"

> [src/include/access/amapi.h:210-292](../postgresql-17.0/src/include/access/amapi.h#L210-L292)

```c
typedef struct IndexAmRoutine
{
	NodeTag		type;

	uint16		amstrategies;	/* 操作符策略数 */
	uint16		amsupport;		/* 支持函数数 */
	uint16		amoptsprocnum;
	bool		amcanorder;		/* 能按列值排序? */
	bool		amcanorderbyop; /* 能按运算符结果排序(KNN)? */
	bool		amcanbackward;	/* 能反向扫描? */
	bool		amcanunique;	/* 支持唯一约束? */
	bool		amcanmulticol;	/* 支持多列? */
	bool		amoptionalkey;	/* 首列无约束也行? */
	bool		amsearcharray;	/* 处理 ScalarArrayOp? */
	bool		amsearchnulls;	/* 处理 IS NULL? */
	bool		amstorage;
	bool		amclusterable;	/* 能 CLUSTER? */
	bool		ampredlocks;
	bool		amcanparallel;
	bool		amcanbuildparallel;
	bool		amcaninclude;	/* 支持 INCLUDE 列? */
	bool		amusemaintenanceworkmem;
	bool		amsummarizing;	/* 只在块粒度存信息?(BRIN) */
	uint8		amparallelvacuumoptions;
	Oid			amkeytype;		/* 存储的数据类型 */

	/* 接口函数:build/insert/scan/vacuum... */
	ambuild_function ambuild;
	ambuildempty_function ambuildempty;
	aminsert_function aminsert;
	...
	ambeginscan_function ambeginscan;
	amrescan_function amrescan;
	amgettuple_function amgettuple; /* 可为 NULL */
	amgetbitmap_function amgetbitmap;	/* 可为 NULL */
	...
} IndexAmRoutine;
```

这个结构体分两部分:

**第一部分:能力位(那一堆 `bool`)**。这是最关键的——它声明了"这个索引**能**做什么、**不能**做什么"。优化器在建索引、选执行计划时,就是看这些位决定"这个查询能不能用这个索引"。把本章五种索引的能力位摆一起对比(这才是"为什么不同索引能力不同"的答案):

| 能力位 | B 树 | Hash | GiST | GIN | BRIN | SP-GiST |
|---|---|---|---|---|---|---|
| `amcanorder`(按值排序) | true | **false** | false | false | false | false |
| `amcanorderbyop`(KNN) | false | false | **true** | false | false | **true** |
| `amcanunique`(唯一) | **true** | false | false | false | false | false |
| `amcanmulticol`(多列) | true | **false** | true | true | true | **false** |
| `amcaninclude`(INCLUDE 列) | true | false | true | false | false | true |
| `amsearchnulls`(IS NULL) | true | **false** | true | false | true | true |
| `amoptionalkey`(首列无约束) | false | false | true | true | true | true |
| `amsummarizing`(块粒度) | false | false | false | false | **true** | false |
| `amstrategies`(策略数) | 5 | 1 | 0 | 0 | 0 | 0 |
| `amgettuple`(逐行扫描) | 有 | 有 | 有 | **NULL** | **NULL** | 有 |

这张表信息量极大,几个亮点:

- **只有 B 树能唯一**(`amcanunique`)。所以主键、唯一约束只能用 B 树——这是为什么 B 树是默认。
- **KNN 近邻排序只有 GiST 和 SP-GiST**(`amcanorderbyop`)。所以"找最近的 N 个"只能用这两种。
- **Hash 和 SP-GiST 不支持多列**(`amcanmulticol = false`)。
- **GIN 和 BRIN 没有 `amgettuple`**——它们只支持 bitmap 扫描(一次拿一堆 TID),不支持逐行扫描。
- **BRIN 独占 `amsummarizing = true`**——全世界只有它在块粒度上摘要。
- **`amstrategies`** 的含义:B 树有 5 个策略(`<` `<=` `=` `>=` `>`),Hash 只有 1 个(`=`),GiST/GIN/BRIN/SP-GiST 是 0(它们的运算符由 opclass 动态定义,不固定)。**这一个数字,就道破了 B 树/Hash 是"固定语义"、GiST/GIN 等是"可扩展语义"的根本区别。**

**第二部分:接口函数(那一堆函数指针)**。`ambuild`(建索引)、`aminsert`(插一行)、`ambeginscan`/`amrescan`/`amgettuple`/`amgetbitmap`(扫描)、`ambulkdelete`/`amvacuumcleanup`(清理)。每个索引类型实现自己的一套,挂在这些指针上。PG 的上层代码(执行器、优化器)只认这个接口,不关心底下是 B 树还是 GIN——**这就是"统一接口、各自实现"的抽象**,和第 6 章讲的"一切皆页"是同一种设计哲学。

### 五个 handler:把能力清单交出去

每种索引有一个 handler 函数,它的工作就是**new 一个 `IndexAmRoutine`,填好自己的能力位和函数指针,返回给 PG**。本章的五个 handler,签名都一样:

- Hash:[`hashhandler`](../postgresql-17.0/src/backend/access/hash/hash.c#L57-L109)
- GiST:[`gisthandler`](../postgresql-17.0/src/backend/access/gist/gist.c#L59-L111)
- GIN:[`ginhandler`](../postgresql-17.0/src/backend/access/gin/ginutil.c#L37-L89)
- BRIN:[`brinhandler`](../postgresql-17.0/src/backend/access/brin/brin.c#L247-L299)
- SP-GiST:[`spghandler`](../postgresql-17.0/src/backend/access/spgist/spgutils.c#L44-L96)

以 GiST 为例,看它怎么填能力位——`amcanorderbyop = true`(能 KNN)、`amcanmulticol = true`、`amstorage = true`(叶子能存附加数据):

> [src/backend/access/gist/gist.c:59-84](../postgresql-17.0/src/backend/access/gist/gist.c#L59-L84)

```c
Datum
gisthandler(PG_FUNCTION_ARGS)
{
	IndexAmRoutine *amroutine = makeNode(IndexAmRoutine);

	amroutine->amstrategies = 0;
	amroutine->amsupport = GISTNProcs;
	...
	amroutine->amcanorder = false;
	amroutine->amcanorderbyop = true;
	amroutine->amcanbackward = false;
	amroutine->amcanunique = false;
	amroutine->amcanmulticol = true;
	amroutine->amoptionalkey = true;
	amroutine->amsearcharray = false;
	amroutine->amsearchnulls = true;
	amroutine->amstorage = true;
	amroutine->amclusterable = true;
	...
	amroutine->ambuild = gistbuild;
	amroutine->aminsert = gistinsert;
	...
	amroutine->amgettuple = gistgettuple;
	amroutine->amgetbitmap = gistgetbitmap;
	...

	PG_RETURN_POINTER(amroutine);
}
```

每个 handler 都是这么个套路。**PG 不需要 `switch-case` 去判断"这是 B 树还是 GIN",它只管拿着这个 `IndexAmRoutine` 指针,调上面的函数**——多态的 C 语言实现。

### 系统目录:pg_am / pg_opclass / pg_opfamily

这些 handler 在系统启动时被注册进 `pg_am` 这张系统目录表:

> [src/include/catalog/pg_am.h:29-41](../postgresql-17.0/src/include/catalog/pg_am.h#L29-L41)

```c
CATALOG(pg_am,2601,AccessMethodRelationId)
{
	Oid			oid;
	NameData	amname;			/* access method name */
	regproc		amhandler BKI_LOOKUP(pg_proc);	/* handler function */
	char		amtype;			/* see AMTYPE_xxx constants */
} FormData_pg_am;
```

每行是一个访问方法:`amname`(btree/hash/gist/gin/brin/spgist)、`amhandler`(对应 handler 函数的 OID)、`amtype`('i' = 索引,'t' = 表)。初始数据见 [pg_am.dat](../postgresql-17.0/src/include/catalog/pg_am.dat#L20-L35),正好六种(btree + 五种)。

但光有访问方法还不够。**"同一个数据类型,在不同索引里行为不同"**——这由**运算符类(operator class,opclass)**和**运算符族(operator family,opfamily)**决定。比如同样是 `int4`:

- 在 B 树里,opclass 定义了 `<` `<=` `=` `>=` `>` 五个策略运算符(全序比较);
- 在 Hash 里,opclass 只定义了 `=` 一个运算符(哈希相等);
- 在 BRIN 里,opclass(`int4_minmax_ops`)定义了怎么算 min/max、怎么做一致检查。

opclass 的目录定义见 [pg_opclass.h:49-76](../postgresql-17.0/src/include/catalog/pg_opclass.h#L49-L76),关键字段:`opcmethod`(这个 opclass 属于哪个访问方法)、`opcintype`(它索引的数据类型)、`opcfamily`(所属运算符族)。opfamily([pg_opfamily.h:29-44](../postgresql-17.0/src/include/catalog/pg_opfamily.h#L29-L44))则是把相关的 opclass 和运算符归成一组。

> **为什么这套设计重要**:因为它让 PG 的索引是**可扩展的**。你想给一种新数据类型(比如自己写的复数类型)建索引——不用改 B 树/GiST 的代码,只要写一个 opclass(定义它的比较/一致/距离函数),注册进 `pg_opclass`,就能用现成的 B 树或 GiST 引擎。这就是为什么 PG 能支持几十种数据类型 + 多种索引的组合,而内核代码不会爆炸——**数据结构和数据类型解耦**。

---

## 章末小结

### 用一句话回顾本章

B 树是通用索引,但**没有一种数据结构能擅长所有查询**。PG 把每种查询形态背后的最优数据结构做成一个独立的"索引访问方法":Hash(纯等值,桶+哈希)、GiST(自定义运算符的平衡树,范围/几何/近邻)、GIN(倒排,键→文档列表,全文/多值)、BRIN(块范围摘要,海量有序大表)、SP-GiST(空间分区,前缀/象限)。**选对了索引类型,比调任何参数都管用。**

### 五句话记住五种索引

1. **Hash**:只为 `=`,O(1) 定位,桶+溢出页;不支持范围/排序/唯一/多列。纯等值且键长时考虑。
2. **GiST**:平衡树框架,节点存"区域",靠 opclass 的 penalty/picksplit/consistent 驱动;适合范围/几何/KNN(`@>` `&&` `<->`)。
3. **GIN**:倒排索引,键树 + 每个键挂 posting list/tree;一个键映射到很多行;全文/数组/JSONB 的杀手锏;代价是插入慢(要维护倒排映射)。
4. **BRIN**:块范围 min/max 摘要,几 MB 索引管几十 GB 表;**只对物理有序的大表(时间序列)有效**,无序数据白建;是预过滤器,有假阳性。
5. **SP-GiST**:空间分区树,支持 Trie/四叉树等非平衡结构;适合前缀路由(IP、字符串)和空间分区;和 GiST 都支持 KNN。

### 回扣主线:快 vs 不丢不乱 + 三个本性

这一章,全在服务"让数据**快**"那一侧,对付的是第三个本性:**"用户说要什么,机器只会按位置读"** 这个错位。

- B 树解决的是"一维有序键的快速定位";但查询形态不止这一种——多值列、近邻、稀疏大表,各自需要不同的数据结构来弥合"要什么"和"怎么找"的鸿沟。
- **索引家族的存在,本质上是因为"查询形态的多样性"逼出了"数据结构的多样性"。** 一种查询形态 → 一种最优数据结构 → 一种索引类型。PG 把它们统一在 `IndexAmRoutine` 接口下,让你按需选用。
- 而每一种索引的**代价**(Hash 的功能受限、GiST 的插入开销、GIN 的写入放大、BRIN 的假阳性、SP-GiST 的不平衡),都是在用"某种牺牲"换"某种查询的极致快"——这是"快"这一侧永恒的权衡(和第 10 章讲"索引不是万能"一脉相承)。

### 想继续深入,该往哪钻

- **`IndexAmRoutine` 全貌**:[src/include/access/amapi.h](../postgresql-17.0/src/include/access/amapi.h) L210-292,每个能力位和函数指针都有注释。这是理解所有索引如何被统管的钥匙。
- **各 handler**:[hashhandler](../postgresql-17.0/src/backend/access/hash/hash.c)、[gisthandler](../postgresql-17.0/src/backend/access/gist/gist.c)、[ginhandler](../postgresql-17.0/src/backend/access/gin/ginutil.c)、[brinhandler](../postgresql-17.0/src/backend/access/brin/brin.c)、[spghandler](../postgresql-17.0/src/backend/access/spgist/spgutils.c)——对比它们的能力位,差异一目了然。
- **Hash 桶与线性哈希**:[src/include/access/hash.h](../postgresql-17.0/src/include/access/hash.h)(页类型、opaque)、[hashutil.c 的 `_hash_hashkey2bucket`](../postgresql-17.0/src/backend/access/hash/hashutil.c#L124-L135)(线性哈希取模)。
- **GIN 倒排结构**:[src/include/access/ginblock.h](../postgresql-17.0/src/include/access/ginblock.h)(entry 树叶子元组的内联/tree 切换、`GinPostingList` 压缩)、[gin.h 的支持函数号](../postgresql-17.0/src/include/access/gin.h#L22-L29)。
- **BRIN 摘要与 min/max**:[brin_tuple.h](../postgresql-17.0/src/include/access/brin_tuple.h)(`BrinTuple`/`BrinValues`)、[brin_minmax.c](../postgresql-17.0/src/backend/access/brin/brin_minmax.c)(min/max opclass 的 `oi_nstored=2` 和 add_value)、[bringetbitmap](../postgresql-17.0/src/backend/access/brin/brin.c#L556)(扫描主循环)。
- **GiST penalty/choose**:[gistutil.c 的 `gistchoose`/`gistpenalty`](../postgresql-17.0/src/backend/access/gist/gistutil.c#L372-L750)、[gist.h 支持函数号](../postgresql-17.0/src/include/access/gist.h#L30-L37)。
- **SP-GiST 分区与 choose**:[src/include/access/spgist.h](../postgresql-17.0/src/include/access/spgist.h)(`spgConfigOut`/`spgChooseOut` 三种结果)。
- **系统目录**:[pg_am.h](../postgresql-17.0/src/include/catalog/pg_am.h)、[pg_opclass.h](../postgresql-17.0/src/include/catalog/pg_opclass.h)、[pg_opfamily.h](../postgresql-17.0/src/include/catalog/pg_opfamily.h)——理解 opclass 如何让"同类型在不同索引里行为不同"。

---

> 五种索引各自的数据结构讲完了,但还有一个问题没回答:**索引找到了 TID,然后呢?** 不管是 B 树、Hash 还是 GIN,索引的叶子(除了少数情况)都只存着"这一行在堆表的哪个位置"——要拿到完整的行数据,还得拿着 TID 回堆表去取。这一步叫**回表**。回表是随机 I/O,贵;那能不能让索引直接把要查的列也存了,连回表都省了?这就是**覆盖索引**和**仅索引扫描(Index Only Scan)**。翻开 **第 13 章 · 索引与堆的配合:回表、覆盖索引、仅索引扫描**。
