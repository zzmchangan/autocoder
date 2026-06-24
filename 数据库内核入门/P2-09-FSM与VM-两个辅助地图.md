# 第 9 章 · FSM 与 VM:空闲空间映射与可见性映射

> **前置**:你需要先读过[第 6 章《页面 Page》](P2-06-页面Page-磁盘IO的最小单位.md)(8KB 页的布局、行指针、`pd_lower`/`pd_upper` 圈出的空闲空间)和[第 8 章《Buffer Pool》](P2-08-BufferPool-内存里的中转站.md)(数据页在内存里流转)。本章看两张和堆表形影不离的**辅助地图**:它们各自常驻在一个独立的 relation fork(分支文件)里,目的只有一个——**让插入和清理这两种操作,不必逐页扫整张表**。FSM 记录"哪个堆页还塞得下",VM 记录"哪个堆页的行对谁都可见"。

> **核心问题**:你执行 `INSERT` 一行,数据库怎么**快速**知道几百万个堆页里哪一个还塞得下这一行,而不必逐页翻找?这就是 **FSM(Free Space Map,空闲空间映射)**——它怎么用一棵**树**把"每页剩余空间"压缩成可以 O(log n) 查询的结构?而 **VM(Visibility Map,可见性映射)** 又是什么——它为每个堆页只存一个 bit,标记"这一页里所有 tuple 对当前及之后所有事务都可见"。这个看似简单的位图,为什么能让 **VACUUM 跳过整页扫描**,又为什么能让"**仅索引扫描(Index-Only Scan)**"成为可能?
>
> **读完本章你会明白**:FSM 为什么用**树**而非数组;每个堆页的空闲空间怎么被压缩成**一个字节**的对数刻度;页内树和跨页树是怎么拼成一张可寻址 2³² 个堆页的地图;VM 的两个位(`ALL_VISIBLE`/`ALL_FROZEN`)各自防什么;为什么一次 `UPDATE` 会把页的 VM 位清掉(为 MVCC 埋下伏笔);以及"辅助结构掉电坏了也没事"——为什么它们大多只当 hint 用。

> **如果一读觉得太难**:先记三件事——① FSM 把每个堆页的空闲空间压成一个字节(0~255 的"类别"),再组织成一棵"父节点=子树最大值"的树,找空页从根往下走 O(log n);② VM 给每个堆页两个 bit,一个说"全可见"(让 VACUUM 和仅索引扫描跳过这页),一个说"全冻结"(让防回滚 VACUUM 也跳过这页);③ 任何 INSERT/UPDATE/DELETE 一旦改了页里 tuple 的可见性,就会把该页的 VM 位清掉。树的内部细节第二遍配合源码抠,不影响理解后续章节。

---

## 一、INSERT 的痛点:塞哪一页,总不能挨个翻

回到第 7 章讲过的:一张表在磁盘上是一堆 8KB 页,每页能塞的行数有限。你执行 `INSERT INTO users VALUES (...)`,执行器要把这一行 tuple 写进某个页。问题是——**写进哪个页**?

### 不这样会怎样:挨页翻找,INSERT 退化成 O(n) 全表扫描

> **不这样会怎样**:如果数据库没有任何关于"哪个页还有空位"的记忆,那它唯一能做的就是**从第 0 页开始,逐页读进来,看 `pd_upper - pd_lower` 够不够放下这一行**。一张几千万行、几百万页的表,每插入一行就要读若干页来找一个有空位的——插入比查询还慢,而且把一堆本不需要的页也搅进 Buffer Pool,把热数据挤出去。更要命的是,这种"找空位"每次都从同一头开始,会导致前面的页被反复塞满、后面的页无人问津,负载极度不均。

所以,堆表需要一个**旁路的索引结构**,只回答一个问题:**"哪个堆页还有 ≥ X 字节的空闲空间?"** 答得越快越好。这就是 FSM。

注意,FSM 是"堆表"专属的辅助结构——索引(B 树等)自己有判断"往哪插"的机制(沿着树一路往下找叶子位置),不需要 FSM;只有堆表这种"无序、往哪儿塞都行"的存储,才需要一张地图来记录空位。

> 源码里,FSM 存在一个**独立的 relation fork**(分支文件)里,叫 `FSM_FORKNUM`。堆表的主数据在 main fork(`<oid>`),FSM 在 `<oid>_fsm`,VM 在 `<oid>_vm`。一个表 = 多个文件,各司其职。FSM 的 README 开宗明义:

> [src/backend/storage/freespace/README:1-5](../postgresql-17.0/src/backend/storage/freespace/README#L1-L5)

```
Free Space Map
--------------

The purpose of the free space map is to quickly locate a page with enough
free space to hold a tuple to be stored; or to determine that no such page
exists and the relation must be extended by one page.
```

一句话:**快速定位一个够大的页;找不到就让表伸长一页**。下面就看它怎么"快"。

---

## 二、压缩:为什么一个堆页的空闲空间只占一个字节

要给几百万个堆页各存一个"空位"信息,先得把"空位"本身压到极小。FSM 的设计是:**每个堆页只占一个字节(0~255)**。

### 不这样会怎样:精确记录空间,地图比表还大

> **不这样会怎样**:一个 8KB 页的空闲空间,精确值在 0~8192 字节之间,需要 13 位才能精确表示。如果再想记"哪一段连续、哪一段碎片化",信息更多。几百万页乘下来,FSM 自己就成了一张大表,查它比查堆表还累——这违背了"辅助地图必须小、必须快"的初衷。

FSM 的选择是**牺牲精度换体积**:每个堆页用一个字节(8 位,0~255 共 256 档)表示空闲空间,但注意——它不是简单的"字节数 / 32"那种线性等分,而是刻意把**最高一档 255 设成特殊含义**。先看刻度定义:

> [src/backend/storage/freespace/freespace.c:36-66](../postgresql-17.0/src/backend/storage/freespace/freespace.c#L36-L66)

```c
/*
 * We use just one byte to store the amount of free space on a page, so we
 * divide the amount of free space a page can have into 256 different
 * categories. The highest category, 255, represents a page with at least
 * MaxFSMRequestSize bytes of free space, and the second highest category
 * represents the range from 254 * FSM_CAT_STEP, inclusive, to
 * MaxFSMRequestSize, exclusive.
 ...
 * 0	- 31   0
 * 32	- 63   1
 * ...    ...  ...
 * 8096 - 8127 253
 * 8128 - 8163 254
 * 8164 - 8192 255
 */
#define FSM_CATEGORIES	256
#define FSM_CAT_STEP	(BLCKSZ / FSM_CATEGORIES)
#define MaxFSMRequestSize	MaxHeapTupleSize
```

这段注释把刻度说得很清楚。以默认 8KB 页(`BLCKSZ=8192`)为例,`FSM_CAT_STEP = 8192/256 = 32`,于是类别 0~254 大致是**每 32 字节一档**:

| 实际空闲(字节) | 类别(cat) |
|---|---|
| 0 ~ 31 | 0 |
| 32 ~ 63 | 1 |
| … | … |
| 8096 ~ 8127 | 253 |
| 8128 ~ 8163 | 254 |
| **8164 ~ 8192** | **255(特殊)** |

为什么 255 特殊?注释解释了一个微妙问题:一页的最大可塞 tuple 大小是 `MaxHeapTupleSize`(`8192 - 对齐后的页头 - 一个 ItemId`,见 [htup_details.h:563](../postgresql-17.0/src/include/access/htup_details.h#L563))。如果某个请求恰好要 `MaxHeapTupleSize` 字节,而某一页的空闲空间也恰好等于 `MaxHeapTupleSize`,在一个"下取整"的线性刻度里,这页可能被归到 254 档(下界 8128),那它"看起来"就不够 MaxHeapTupleSize(8164)。于是 FSM 把 **255 档单列出来,语义是"≥ MaxHeapTupleSize"**,确保"一个完全空的页永远能满足最大请求"。

把空间压成 0~255 的类别后,两个换算函数负责来回转换:

> [src/backend/storage/freespace/freespace.c:391-411](../postgresql-17.0/src/backend/storage/freespace/freespace.c#L391-L411)

```c
static uint8
fsm_space_avail_to_cat(Size avail)
{
	int			cat;

	Assert(avail < BLCKSZ);

	if (avail >= MaxFSMRequestSize)
		return 255;

	cat = avail / FSM_CAT_STEP;

	/*
	 * The highest category, 255, is reserved for MaxFSMRequestSize bytes or
	 * more.
	 */
	if (cat > 254)
		cat = 254;

	return (uint8) cat;
}
```

把"实际空闲字节数"折算成类别时**向下取整**(`avail / FSM_CAT_STEP`),并保留 255 给"满到顶"的页。反过来,查询时要把"我需要 N 字节"折算成"我需要至少几档",那就**向上取整**,以免低估需求:

> [src/backend/storage/freespace/freespace.c:431-449](../postgresql-17.0/src/backend/storage/freespace/freespace.c#L431-L449)

```c
static uint8
fsm_space_needed_to_cat(Size needed)
{
	int			cat;
	...
	if (needed == 0)
		return 1;

	cat = (needed + FSM_CAT_STEP - 1) / FSM_CAT_STEP;
	...
}
```

`(needed + step - 1) / step` 就是向上取整的标准写法。注意 `needed == 0` 时返回 1 而非 0——**0 档表示"这页没有可用空间"**,所以哪怕要 0 字节,也要求至少 1 档,避开"空页"语义。

> **小结这一节**:FSM 用**一页一字节、256 档、32 字节精度**的刻度,把"空闲空间"这个连续量压成离散的小整数。精度损失换来的是地图极小——一页能记录几千个堆页的空位。这就是后面"树形查询能放进一页"的前提。

---

## 三、为什么要用"树"而不是"数组":把"找第一个够大的页"从 O(n) 降到 O(log n)

把每页压成一个字节后,最朴素的存法是一个**数组**:第 i 个字节 = 第 i 个堆页的空闲类别。查询"哪个页 ≥ X 档",就线性扫一遍数组。

### 不这样会怎样:数组查询是 O(n),大表扛不住

> **不这样会怎样**:假设一张表有 400 万页,FSM 是个 400 万字节的数组。每次 INSERT 找空位,最坏要扫完整个数组才确定"没有够大的页、得扩展表"。400 万次比较,每次插入都这么干,完全无法接受。更糟的是,这个数组本身也放不进一页(一页才 8KB ≈ 8000 字节),它自己又得分页存,查询时要反复 I/O,雪上加霜。

FSM 的解法是经典数据结构思路:**把数组组织成一棵二叉树**,让"找第一个 ≥ X 的页"变成**自顶向下的 O(log n)**。原理一句话——**每个非叶子节点存它两个子节点中的较大值**(即"我这棵子树里最大的空闲类别")。这样:

- **要判断"整张表有没有页 ≥ X",只看根节点**。根 ≥ X,就有;根 < X,整棵树都没有,直接扩展表。O(1)。
- **要找到一个具体的页,从根往下走**:每到一个节点,挑一个值 ≥ X 的子节点走下去,走到叶子就是目标页。O(log n)。

页内的树长这样(README 给的例子):

```
       4           <- 根:整棵树的最大值是 4
    4     2        <- 非叶子:左右子树各自的最大值
   3 4   0 2       <- 叶子:对应 4 个堆页的实际空闲类别
```

(数字是"类别",值越大空位越多。)要找"≥ 3 档"的页:根是 4 ≥ 3,进左子;左子 4 ≥ 3,进左叶;叶 3 ≥ 3,命中——第 0 页。要找"≥ 5 档":根 4 < 5,**整棵树都没有**,立刻知道要扩展表,根本不用往下走。

> 这个结构最妙的地方在于:**"判断有没有"和"找到具体哪个"的成本天差地别**。前者 O(1)(看根),后者 O(log n)。而 INSERT 的常态是"大多时候有空位",所以判断这步几乎总是"根 ≥ X,有的",然后才花 log n 去定位。

这棵树的节点用一个数组按下标存(堆式存储:节点 i 的左孩子是 `2i+1`,右孩子 `2i+2`,父亲是 `(i-1)/2`),不是用指针:

> [src/backend/storage/freespace/fsmpage.c:28-31](../postgresql-17.0/src/backend/storage/freespace/fsmpage.c#L28-L31)

```c
/* Macros to navigate the tree within a page. Root has index zero. */
#define leftchild(x)	(2 * (x) + 1)
#define rightchild(x)	(2 * (x) + 2)
#define parentof(x)		(((x) - 1) / 2)
```

这是教科书式的**二叉堆数组布局**:不存指针,靠下标算术导航,省空间、缓存友好。FSM 页结构体本身就一个 `fp_next_slot` 提示加上一个 `fp_nodes[]` 字节数组:

> [src/include/storage/fsm_internals.h:24-43](../postgresql-17.0/src/include/storage/fsm_internals.h#L24-L43)

```c
typedef struct
{
	/*
	 * fsm_search_avail() tries to spread the load of multiple backends by
	 * returning different pages to different backends in a round-robin
	 * fashion. fp_next_slot points to the next slot to be returned (assuming
	 * there's enough space on it for the request). ...
	 */
	int			fp_next_slot;

	/*
	 * fp_nodes contains the binary tree, stored in array. The first
	 * NonLeafNodesPerPage elements are upper nodes, and the following
	 * LeafNodesPerPage elements are leaf nodes. Unused nodes are zero.
	 */
	uint8		fp_nodes[FLEXIBLE_ARRAY_MEMBER];
} FSMPageData;
```

一棵 8KB 页里能塞下的树有多大?叶子和非叶子的数量:

> [src/include/storage/fsm_internals.h:51-61](../postgresql-17.0/src/include/storage/fsm_internals.h#L51-L61)

```c
#define NodesPerPage (BLCKSZ - MAXALIGN(SizeOfPageHeaderData) - \
					  offsetof(FSMPageData, fp_nodes))

#define NonLeafNodesPerPage (BLCKSZ / 2 - 1)
#define LeafNodesPerPage (NodesPerPage - NonLeafNodesPerPage)

/*
 * Number of FSM "slots" on a FSM page. This is what should be used
 * outside fsmpage.c.
 */
#define SlotsPerFSMPage LeafNodesPerPage
```

一棵近似满二叉树,叶子比非叶子多一个。8KB 页去掉页头后,大约能存 **4000 多个叶子节点**(README 里说约 4000),也就是**一个 FSM 页能记录约 4000 个堆页的空位**。

> **顺带说 `fp_next_slot`**:这个字段是个"下次从哪开始找"的提示。多个 backend 同时插同一张表时,如果都从根的左子树开始找,会挤同一个堆页,造成锁竞争。`fp_next_slot` 让它们**轮转着从不同位置开始**,把插入分散到不同页,避免热点。同时它又只是个 hint,坏了无所谓(下面会讲为什么)。

---

## 四、跨页的树:三层结构覆盖 2³² 个堆页

一页只能记 4000 个堆页。一张表可能有几十亿页(2³²-1 是 PG 单表上限)。怎么办?**把树再往上层叠**——FSM 不止是"页内一棵树",而是"**页之间也是一棵树**"。

README 画得清楚(假设每页只记 4 个堆页,实际约 4000):

```
 0     <-- 第 2 层(根页,始终在物理 block 0)
  0     <-- 第 1 层
   0     <-- 第 0 层(叶子层,每个叶子对应若干堆页)
   1
   2
   3
  1
   4 5 6 7
  2
   8 9 10 11
  3
   12 13 14 15
```

意思是:**第 0 层(叶子层)的每个 FSM 页,记一批堆页的空位;第 1 层的每个 FSM 页,记一批第 0 层页的"最大值";第 2 层(根)记一批第 1 层页的最大值。** 上层节点的值 = 对应下层页的根值,层层传递。

层数是固定的:

> [src/backend/storage/freespace/freespace.c:68-78](../postgresql-17.0/src/backend/storage/freespace/freespace.c#L68-L78)

```c
/*
 * Depth of the on-disk tree. We need to be able to address 2^32-1 blocks,
 * and 1626 is the smallest number that satisfies X^3 >= 2^32-1. Likewise,
 * 256 is the smallest number that satisfies X^4 >= 2^32-1. In practice,
 * this means that 4096 bytes is the smallest BLCKSZ that we can get away
 * with a 3-level tree, and 512 is the smallest we support.
 */
#define FSM_TREE_DEPTH	((SlotsPerFSMPage >= 1626) ? 3 : 4)
```

默认 8KB 页,`SlotsPerFSMPage ≈ 4000 > 1626`,所以是 **3 层**。算一下:4000³ ≈ 640 亿 > 2³² ≈ 43 亿,绰绰有余。也就是说:**默认配置下,一棵 3 层 FSM 树就能覆盖 PG 单表允许的最大页数**,树高是常数,查询成本恒定。

逻辑地址到物理 block 的换算由 `fsm_logical_to_physical` 完成([freespace.c:454-485](../postgresql-17.0/src/backend/storage/freespace/freespace.c#L454-L485));根页永远在物理 block 0(`FSM_ROOT_ADDRESS`)。查询时从根页开始,在页内树里找到一个槽,根据槽号定位到下一层的某个子页,再在那个页里找……直到第 0 层,槽号就直接对应堆页号。这就是 `fsm_search` 的主循环([freespace.c:677-795](../postgresql-17.0/src/backend/storage/freespace/freespace.c#L677-L795))——稍后在"关键源码精读"里细看。

---

## 五、更新空位:值变了怎么"冒泡"

INSERT 塞了一行,目标页的空闲空间变小了,FSM 里对应那个字节得更新。更新的难点是:**改了叶子,所有祖先的"最大值"可能都要跟着变**。

### 不这样会怎样:只改叶子,树就坏了

> **不这样会怎样**:假设叶子页 A 从类别 5 降到 2(塞了一行进去),而它的兄弟叶子 B 还是 4。如果只改 A 不动父节点,父节点还写着 5(原来的 max),查询时以为这棵子树还有 5 档空位,顺着走下来扑个空——树的内容和实际不一致,查询出错。

所以更新要走"**冒泡(bubble up)**":改完叶子,逐层往上,把每个父节点重算成"两个孩子的 max",直到某层父节点的值没变(说明再往上也不会变),提前终止。代码在 `fsm_set_avail`:

> [src/backend/storage/freespace/fsmpage.c:62-113](../postgresql-17.0/src/backend/storage/freespace/fsmpage.c#L62-L113)

```c
bool
fsm_set_avail(Page page, int slot, uint8 value)
{
	int			nodeno = NonLeafNodesPerPage + slot;
	FSMPage		fsmpage = (FSMPage) PageGetContents(page);
	uint8		oldvalue;
	...
	fsmpage->fp_nodes[nodeno] = value;

	/*
	 * Propagate up, until we hit the root or a node that doesn't need to be
	 * updated.
	 */
	do
	{
		uint8		newvalue = 0;
		int			lchild;
		int			rchild;

		nodeno = parentof(nodeno);
		lchild = leftchild(nodeno);
		rchild = lchild + 1;

		newvalue = fsmpage->fp_nodes[lchild];
		if (rchild < NodesPerPage)
			newvalue = Max(newvalue,
						   fsmpage->fp_nodes[rchild]);

		oldvalue = fsmpage->fp_nodes[nodeno];
		if (oldvalue == newvalue)
			break;

		fsmpage->fp_nodes[nodeno] = newvalue;
	} while (nodeno > 0);
	...
}
```

注意那个 `if (oldvalue == newvalue) break;`——冒泡的**提前终止**:一旦某层父节点算出来的新值和旧值一样,说明这次改动没有把"子树最大值"拉低(可能这页本来就不是最大的那个孩子),再往上都不会变了,直接停。这让"小幅改动"的开销很小,大多数更新只冒泡一两层就停了。

> 还有个安全网:函数末尾检查 `if (value > fsmpage->fp_nodes[0]) fsm_rebuild_page(page);`——如果新值居然比根还大,说明这棵树某处已经损坏(某个父节点写小了),直接整页重建。这是 FSM 容错的一部分(下面"为什么坏了也没事"会展开)。

跨页的冒泡由 `fsm_set_and_search` 间接完成([freespace.c:645-672](../postgresql-17.0/src/backend/storage/freespace/freespace.c#L645-L672));而把底层的变动传播到上层 FSM 页,是 **VACUUM 的活**——`FreeSpaceMapVacuum` / `FreeSpaceMapVacuumRange` 从根递归扫描,把上层节点的值刷成底层页的根值([freespace.c:357-384](../postgresql-17.0/src/backend/storage/freespace/freespace.c#L357-L384))。这就是为什么 README 说:一次普通的 INSERT 更新只让 FSM 的空位信息"在底层页内可见",要让它对**整棵树的所有查询者**都可见,得等下次 VACUUM 把上层页刷一遍。

---

## 六、VM:可见性映射——一个 bit 换一次跳页

讲完 FSM(服务"快":快速找空页),来看本章另一个主角 **VM(Visibility Map,可见性映射)**。它服务"快"的另一面,还顺手承接了 MVCC 的清理负担。

### VM 是什么:每个堆页两个 bit

VM 是一个位图,为**每个堆页存 2 个 bit**:

> [src/include/access/visibilitymapdefs.h:16-23](../postgresql-17.0/src/include/access/visibilitymapdefs.h#L16-L23)

```c
/* Number of bits for one heap page */
#define BITS_PER_HEAPBLOCK 2

/* Flags for bit map */
#define VISIBILITYMAP_ALL_VISIBLE	0x01
#define VISIBILITYMAP_ALL_FROZEN	0x02
#define VISIBILITYMAP_VALID_BITS	0x03	/* OR of all valid visibilitymap
											 * flags bits */
```

- **`ALL_VISIBLE`(低位,0x01)**:这一页里**所有 tuple 对当前及之后所有事务都可见**。换句话说,这页里没有"死元组"、没有"正在进行中的删除/更新残留"。
- **`ALL_FROZEN`(高位,0x02)**:这一页里所有 tuple 都已被**冻结(frozen)**——它们的 `xmin` 被替换成一个永远"已提交"的特殊值,即便将来发生事务 ID 回卷(xid wraparound),这些行也不会突然"看不见"。

一个堆页占 2 bit,意味着**一个 8KB 的 VM 页能表示约 32768 个堆页**(`MAPSIZE × 4`,见 [visibilitymap.c:108-114](../postgresql-17.0/src/backend/access/heap/visibilitymap.c#L108-L114))。比 FSM 还省——FSM 一页一字节,VM 一页只占 1/4 字节。

`ALL_FROZEN` 必须在 `ALL_VISIBLE` 之后才能设:一个页连"全可见"都不是(还有死元组),谈不上"全冻结"。

> [src/backend/access/heap/visibilitymap.c:24-32](../postgresql-17.0/src/backend/access/heap/visibilitymap.c#L24-L32)

```c
/*
 * The visibility map is a bitmap with two bits (all-visible and all-frozen)
 * per heap page. A set all-visible bit means that all tuples on the page are
 * known visible to all transactions, and therefore the page doesn't need to
 * be vacuumed. A set all-frozen bit means that all tuples on the page are
 * completely frozen, and therefore the page doesn't need to be vacuumed even
 * if whole table scanning vacuum is required (e.g. anti-wraparound vacuum).
 * The all-frozen bit must be set only when the page is already all-visible.
 */
```

VM 也是保守的:**位被设置时,条件一定为真;位没设置时,条件可能为真也可能不为真**。也就是说,VM 是个"乐观备忘录",漏记了没关系(顶多多扫一页),但绝不能错记(否则会跳过本该处理的页,导致数据错乱)。

---

## 七、VM 为什么能让 VACUUM 和仅索引扫描跳页

VM 的两个位,各自省下了一大笔功夫。

### `ALL_VISIBLE`:让 VACUUM 跳过整页

第 6 章讲过 `LP_DEAD`(死元组):MVCC 下,DELETE 或 UPDATE 会留下"对谁都不可见的旧版本",占着页里的空间,等 VACUUM 清理。VACUUM 默认要扫整张表来找这些死元组。

> **不这样会怎样**:如果 VACUUM 每次都老老实实从头扫到尾,一张几千万行的表,每次 VACUUM 都全表扫描一遍,即使绝大多数页根本没有死元组(都是活跃数据),也得逐页读、逐 tuple 检查——I/O 和 CPU 开销巨大,而且会和正常查询抢 Buffer Pool。

VM 的 `ALL_VISIBLE` 位解决了这个:**一个页如果 `ALL_VISIBLE` 位是 1,说明这页里没有死元组(全都可见),VACUUM 直接跳过,根本不用读这个页**。于是 VACUUM 只需要处理那些"有过 DELETE/UPDATE 痕迹"的页,工作量从"全表"缩小到"真正有垃圾的页"。注释说得很直白:

> [src/backend/access/heap/visibilitymap.c:53-55](../postgresql-17.0/src/backend/access/heap/visibilitymap.c#L53-L55)

```c
/*
 * VACUUM will normally skip pages for which the visibility map bit is set;
 * such pages can't contain any dead tuples and therefore don't need vacuuming.
 */
```

### `ALL_VISIBLE`:让"仅索引扫描"成为可能

这是 VM 另一个高频且关键的用处。索引里存的不只是键值,通常还存了指向堆 tuple 的 TID(行指针)。一次走索引的查询,典型的两步是:**① 在索引里找到匹配的 TID;② 拿 TID 回堆表取完整行(回表)**。

但有时候你只要索引里就有全部需要的列(比如 `SELECT id FROM users WHERE id < 100`,而 `id` 在索引里)——理论上不必回表。**可为什么不能直接信索引、跳过回表?** 因为 MVCC:索引里存的是 TID,但那个 TID 指向的堆 tuple,对**当前这个事务**到底可见不可见,索引自己不知道(可见性信息只在堆 tuple 的 `xmin`/`xmax` 里)。要确认可见性,就得回堆表看。

VM 的 `ALL_VISIBLE` 位提供了一条捷径:**如果这个堆页 `ALL_VISIBLE`,那么这页里所有 tuple 对所有事务都可见**——既然对谁都可见,那对当前事务肯定可见,**不必回堆表确认,直接用索引里的数据就行**。这就是**仅索引扫描(Index-Only Scan)**能成立的条件:

> 一个堆页被标记 `ALL_VISIBLE` ⟹ 索引扫描可以跳过对这个页的回表。

所以 VM 不是"可有可无的优化",而是"仅索引扫描"这个执行计划的**前提条件**——没有 VM,PG 就不敢跳过回表,因为无法廉价地确认可见性。

### `ALL_FROZEN`:让"防回滚 VACUUM"也跳页

`ALL_VISIBLE` 让**普通 VACUUM**(清死元组)跳页。但还有一种特殊的 VACUUM 叫 **anti-wraparound VACUUM(防回滚 VACUUM)**——它的任务不是清垃圾,而是**冻结**:把 tuple 的 `xmin` 换成特殊值,防止事务 ID 用完一圈后回卷导致数据"突然不可见"(这是 PG 的 xid wraparound 问题,P6 会详讲)。

如果只有 `ALL_VISIBLE`,那防回滚 VACUUM 还是得全表扫一遍去找"还没冻结的 tuple"。`ALL_FROZEN` 位就是为它准备的:**`ALL_FROZEN` 位为 1 的页,所有 tuple 已冻结,防回滚 VACUUM 也能跳过**。于是两种 VACUUM 各有各的跳页依据,互不干扰。

---

## 八、一次 UPDATE 为什么会把 VM 位清掉:MVCC 的伏笔

这是本章最值得想通的一点,也是 VM 和 MVCC 的交接处。

回到 MVCC 的核心:PG 的 `UPDATE` 不是"原地改",而是**写一个新版本的 tuple,把旧版本标记为"已被某事务删除"(`xmax` 设置),旧版本先留在页里**(详见第 17 章)。也就是说,**一次 UPDATE 会让一个原本"所有 tuple 都对所有人可见"的页,突然冒出一个对某些事务不可见的新版本**(新版本还没提交,或者刚提交但有些旧事务的快照看不到它)。

> **不这样会怎样**:假设 UPDATE 写了新版本,但 VM 位还亮着 `ALL_VISIBLE`。这时另一个仅索引扫描路过,看到 `ALL_VISIBLE`,信以为真,**跳过回表,直接用索引里的旧数据**——可实际上这页已经有了新版本,索引(还指向旧 TID)拿到的可能是过时数据。这就是数据不一致,比慢还可怕。

所以铁律是:**只要一个堆页里有 tuple 的可见性发生变化(INSERT 进了新行、UPDATE 写了新版本、DELETE 标记了旧行),这个页就不再是"对所有事务都可见"了,必须立刻清掉它的 `ALL_VISIBLE` 位**。

这条铁律在代码里处处可见。看 `heap_insert`(插入新行):

> [src/backend/access/heap/heapam.c:2047-2054](../postgresql-17.0/src/backend/access/heap/heapam.c#L2047-L2054)

```c
	if (PageIsAllVisible(BufferGetPage(buffer)))
	{
		all_visible_cleared = true;
		PageClearAllVisible(BufferGetPage(buffer));
		visibilitymap_clear(relation,
							ItemPointerGetBlockNumber(&(heaptup->t_self)),
							vmbuffer, VISIBILITYMAP_VALID_BITS);
	}
```

逻辑很直白:如果这页原来是 `ALL_VISIBLE`,现在要塞进一个新 tuple(这个新 tuple 在它的事务提交前对别人不可见),那这页立刻就不是"全可见"了——**同时清掉页头的 `PD_ALL_VISIBLE` 标志位和 VM 里对应的 bit**,两处一起清。注意它清的是 `VISIBILITYMAP_VALID_BITS`(两个位都清)——因为新插入一个未提交的 tuple,这页也谈不上"全冻结"了。

`heap_update` 也是同样的处理,而且因为 UPDATE 可能涉及两个页(旧版本所在的页 + 新版本所在的页,如果新版本塞不下原页),两个页的 VM 位都要清:

> [src/backend/access/heap/heapam.c:3956-3970](../postgresql-17.0/src/backend/access/heap/heapam.c#L3956-L3970)

```c
	/* clear PD_ALL_VISIBLE flags, reset all visibilitymap bits */
	if (PageIsAllVisible(BufferGetPage(buffer)))
	{
		all_visible_cleared = true;
		PageClearAllVisible(BufferGetPage(buffer));
		visibilitymap_clear(relation, BufferGetBlockNumber(buffer),
							vmbuffer, VISIBILITYMAP_VALID_BITS);
	}
	if (newbuf != buffer && PageIsAllVisible(BufferGetPage(newbuf)))
	{
		all_visible_cleared_new = true;
		PageClearAllVisible(BufferGetPage(newbuf));
		visibilitymap_clear(relation, BufferGetBlockNumber(newbuf),
							vmbuffer_new, VISIBILITYMAP_VALID_BITS);
	}
```

这段把"改了页 → 清 VM 位"这条 MVCC 不变量钉死了。`PD_ALL_VISIBLE` 是页头里的一个标志位(`pd_flags` 的 0x0004 位,见 [bufpage.h:184-187](../postgresql-17.0/src/include/storage/bufpage.h#L184-L187)),它和 VM 里的 `ALL_VISIBLE` bit 是**同一个信息的两份副本**:页头那份供"已经持有这页锁的代码"快速看,VM 那份供"不想读堆页、只看位图"的 VACUUM 和仅索引扫描看。两份必须同步——这给"清位"操作带来了一个微妙的崩溃安全问题,下一节讲。

> 这里埋下第 17 章 MVCC 的伏笔:**VM 的存在,本质上是为了高效地回答"这页有没有不可见的 tuple"这个问题,而"不可见的 tuple"正是 MVCC 多版本机制的产物。** 没有 MVCC(像 MyISAM 那种原地覆盖写),就不需要 VM。PG 因为坚持"读不阻塞写",才背负了维护 VM 的代价——但这笔代价比全表扫描便宜得多。

---

## 九、关键源码精读:清位与设位的崩溃安全

VM 的两个位虽然只是 hint(漏设不影响正确性,顶多多扫一页),但**"设位"这件事却必须写 WAL 日志**,这点很反直觉。看 `visibilitymap_set`:

> [src/backend/access/heap/visibilitymap.c:243-314](../postgresql-17.0/src/backend/access/heap/visibilitymap.c#L243-L314)

```c
void
visibilitymap_set(Relation rel, BlockNumber heapBlk, Buffer heapBuf,
				  XLogRecPtr recptr, Buffer vmBuf, TransactionId cutoff_xid,
				  uint8 flags)
{
	...
	if (flags != (map[mapByte] >> mapOffset & VISIBILITYMAP_VALID_BITS))
	{
		START_CRIT_SECTION();

		map[mapByte] |= (flags << mapOffset);
		MarkBufferDirty(vmBuf);

		if (RelationNeedsWAL(rel))
		{
			if (XLogRecPtrIsInvalid(recptr))
			{
				...
				recptr = log_heap_visible(rel, heapBuf, vmBuf, cutoff_xid, flags);
				...
			}
			PageSetLSN(page, recptr);
		}

		END_CRIT_SECTION();
	}

	LockBuffer(vmBuf, BUFFER_LOCK_UNLOCK);
}
```

为什么设个 hint 位都要 `log_heap_visible` 写 WAL?文件开头的注释解释了这层微妙([visibilitymap.c:41-52](../postgresql-17.0/src/backend/access/heap/visibilitymap.c#L41-L52)):

> **`ALL_VISIBLE` 位和页头的 `PD_ALL_VISIBLE` 位是成对设置的**,而问题出在"成对"上。设想:VACUUM 把一页判为全可见,设置 VM 的 `ALL_VISIBLE` 位,同时也要设这页的 `PD_ALL_VISIBLE`。如果**VM 页先落盘、堆页还没落盘**,这时崩溃——重启后,VM 位在,堆页的 `PD_ALL_VISIBLE` 没了 redo 回来。接下来某个 INSERT 改这个堆页,它看 `PD_ALL_VISIBLE` 没设,就不会去清 VM 位——**于是 VM 位的 `ALL_VISIBLE` 还亮着,但堆页里其实已经有未提交的新 tuple 了**,仅索引扫描会拿到错数据。

所以**设位必须写 WAL**,redo 时会重新设 VM 位 + 堆页的 `PD_ALL_VISIBLE` 位,保证两者一致。代价是多写一条 WAL,换来的是"VM 位绝不会错误地保持 set 状态"。

而**清位(`visibilitymap_clear`)则不单独写 WAL**——清位总是和堆页的修改(INSERT/UPDATE/DELETE)在同一个临界区里、同一条 WAL 记录里完成,redo 那条堆记录时会顺带把 VM 位清掉。清位永远安全(少设一个 `ALL_VISIBLE` 位,顶多多扫一页,不会错)。

> [src/backend/access/heap/visibilitymap.c:137-172](../postgresql-17.0/src/backend/access/heap/visibilitymap.c#L137-L172)

```c
bool
visibilitymap_clear(Relation rel, BlockNumber heapBlk, Buffer vmbuf, uint8 flags)
{
	...
	/* Must never clear all_visible bit while leaving all_frozen bit set */
	Assert(flags & VISIBILITYMAP_VALID_BITS);
	Assert(flags != VISIBILITYMAP_ALL_VISIBLE);
	...
	if (map[mapByte] & mask)
	{
		map[mapByte] &= ~mask;

		MarkBufferDirty(vmbuf);
		cleared = true;
	}
	...
}
```

注意那个断言 `Assert(flags != VISIBILITYMAP_ALL_VISIBLE)`——**绝不允许只清 `ALL_VISIBLE` 而留下 `ALL_FROZEN`**。因为 `ALL_FROZEN` 蕴含 `ALL_VISIBLE`(冻结的必然全可见),一旦出现"非全可见却全冻结"这种矛盾状态,逻辑就乱了。所以要么两个都不动,要么至少连 `ALL_VISIBLE` 一起清。`heap_insert`/`heap_update` 里传的都是 `VISIBILITYMAP_VALID_BITS`(两位都清),符合这条约束。

---

## 十、为什么 FSM 和 VM"坏了也没事":hint 与自愈

最后讲一个贯穿 FSM 和 VM 的设计哲学:**它们都是 hint(提示),不是权威数据**。

### 不这样会怎样:辅助结构必须和主数据绝对一致,否则……

> **不这样会怎样**:FSM 和 VM 存在独立 fork 里,它们的内容是对堆页状态的"旁路记录"。如果要求它们和堆页**绝对一致**(任何时刻 FSM 的每个字节都精确等于对应堆页的空闲空间、VM 的每个 bit 都精确反映可见性),那每次堆页一改,就要同步改 FSM/VM,而且为了崩溃安全还得给这些同步操作都写 WAL——FSM/VM 自己又变成了一套需要强一致性的系统,复杂度和开销爆炸。

PG 的选择是**放松一致性**:

- **FSM 不写 WAL**。它的更新只用 `MarkBufferDirtyHint`(脏页提示,不是真正的脏页标记),崩溃后可能丢失、可能和实际不符。但这没关系——FSM 错了只会导致"找到一个其实不够大的页"(INSERT 时会检测到,重找)或"漏掉一个其实够大的页"(多扩展一页,浪费一点空间)。**FSM 错了不会导致数据错乱,只会导致效率略降**。而且 FSM 有一套**自愈机制**:搜索时发现"父节点承诺有空间、孩子却没有",会重建那页的树([fsmpage.c:262-289](../postgresql-17.0/src/backend/storage/freespace/fsmpage.c#L262-L289));VACUUM 还会定期把底层页的真实空位刷进 FSM([freespace.c:183-187](../postgresql-17.0/src/backend/storage/freespace/README#L183-L187))。
- **VM 大部分时候也是 hint**。除了"设位"因为和 `PD_ALL_VISIBLE` 配对必须写 WAL(上节讲的),其它场合都容忍不一致。VM 读时用 `RBM_ZERO_ON_ERROR`(读到坏页直接当全零处理,见 [visibilitymap.c:562-580](../postgresql-17.0/src/backend/access/heap/visibilitymap.c#L562-L580))——VM 全零意味着"所有位都没设",这只会让 VACUUM 多扫些页、让仅索引扫描回表,不会出错。

> **一句话**:堆表(main fork)是**权威数据**,必须精确、必须 WAL 保护;FSM 和 VM 是**派生索引**,可以错、可以丢、可以重建,只要保证"错了也最多损失效率、绝不损失正确性"。这是数据库里"权威数据 vs 派生缓存"的经典二分——FSM/VM 本质上是从堆表派生出来的缓存,所以享有缓存的自由(可重建、可不一致),也承担缓存的义务(必须能自愈)。

---

## 关键源码精读:`fsm_search` 的自顶向下与失败重试

把前面几节串起来,看一次完整的"找空页"是怎么走的。入口是 `GetPageWithFreeSpace`,它把"需要 N 字节"换成"需要至少几档",再调 `fsm_search`:

> [src/backend/storage/freespace/freespace.c:136-142](../postgresql-17.0/src/backend/storage/freespace/freespace.c#L136-L142)

```c
BlockNumber
GetPageWithFreeSpace(Relation rel, Size spaceNeeded)
{
	uint8		min_cat = fsm_space_needed_to_cat(spaceNeeded);

	return fsm_search(rel, min_cat);
}
```

`fsm_search` 是核心,从根页开始往下找,中途可能失败重试:

> [src/backend/storage/freespace/freespace.c:677-760](../postgresql-17.0/src/backend/storage/freespace/freespace.c#L677-L760)

```c
static BlockNumber
fsm_search(Relation rel, uint8 min_cat)
{
	int			restarts = 0;
	FSMAddress	addr = FSM_ROOT_ADDRESS;

	for (;;)
	{
		int			slot;
		Buffer		buf;
		uint8		max_avail = 0;

		/* Read the FSM page. */
		buf = fsm_readbuf(rel, addr, false);

		/* Search within the page */
		if (BufferIsValid(buf))
		{
			LockBuffer(buf, BUFFER_LOCK_SHARE);
			slot = fsm_search_avail(buf, min_cat,
									(addr.level == FSM_BOTTOM_LEVEL),
									false);
			if (slot == -1)
			{
				max_avail = fsm_get_max_avail(BufferGetPage(buf));
				UnlockReleaseBuffer(buf);
			}
			else
			{
				/* Keep the pin for possible update below */
				LockBuffer(buf, BUFFER_LOCK_UNLOCK);
			}
		}
		else
			slot = -1;

		if (slot != -1)
		{
			/*
			 * Descend the tree, or return the found block if we're at the
			 * bottom.
			 */
			if (addr.level == FSM_BOTTOM_LEVEL)
			{
				BlockNumber blkno = fsm_get_heap_blk(addr, slot);
				...
				if (fsm_does_block_exist(rel, blkno))
				{
					ReleaseBuffer(buf);
					return blkno;
				}
				...
			}
			else
			{
				ReleaseBuffer(buf);
			}
			addr = fsm_get_child(addr, slot);
		}
		else if (addr.level == FSM_ROOT_LEVEL)
		{
			/*
			 * At the root, failure means there's no page with enough free
			 * space in the FSM. Give up.
			 */
			return InvalidBlockNumber;
		}
		else
		{
			...
		}
	}
}
```

逐段对应前面讲的设计:

1. **从根开始**:`addr = FSM_ROOT_ADDRESS`(第 2 层,物理 block 0)。读这一页,在页内树里找"≥ min_cat"的槽(`fsm_search_avail`)。
2. **找到槽,还没到底**(`slot != -1` 且 `addr.level > 0`):这个槽对应下一层的某个子页,`addr = fsm_get_child(addr, slot)`,循环再走一遍——下一层页内再找。
3. **找到槽,到底了**(`addr.level == FSM_BOTTOM_LEVEL`):槽号直接对应堆页号,`fsm_get_heap_blk` 算出来,返回。调用方(`RelationGetBufferForTuple`)拿到这个堆页号去真正读页、塞 tuple。
4. **根页就找不到**(`slot == -1` 且 `addr.level == FSM_ROOT_LEVEL`):**整棵树都没有够大的页**,返回 `InvalidBlockNumber`。调用方据此去**扩展表**(在表尾新加一页)。这正是 README 第一句"determine that no such page exists and the relation must be extended by one page"的落点。

注意那个 `restarts` 计数器和失败分支:`fsm_search` 只在**持有某一层页的 share lock 时**读到它的内容,释放锁之后、去读孩子页之前,这个孩子页可能被别的 backend 改了(空位被别人抢走)。于是走到孩子发现"父节点承诺有、实际没有",就要**回头修正父节点的值,然后从根重试**。这就是前面"FSM 允许不一致"的具体表现——一致性靠"发现不对就修、然后重试"来保证,而不是靠全程持锁。`restarts > 10000` 的紧急阀是为了防止极端情况下死循环。

而真正"往堆里塞一行"的入口 `RelationGetBufferForTuple`(在 [hio.c:502](../postgresql-17.0/src/backend/access/heap/hio.c#L502))就是 FSM 的主顾:它先问 FSM 拿一个候选页,读进来上排他锁,检查真的够不够大;不够就 `RecordAndGetPageWithFreeSpace`(把这次实测的空位回写 FSM,顺便再要一个),循环到找到或决定扩展:

> [src/backend/access/heap/hio.c:578-585](../postgresql-17.0/src/backend/access/heap/hio.c#L578-L585)

```c
	if (targetBlock == InvalidBlockNumber && use_fsm)
	{
		/*
		 * We have no cached target page, so ask the FSM for an initial
		 * target.
		 */
		targetBlock = GetPageWithFreeSpace(relation, targetFreeSpace);
	}
```

这条链路把"INSERT 找页"的代价压到了 O(log n)(FSM 树深)加几次锁重试——比起逐页扫全表,是天壤之别。

---

## 章末小结

- **FSM(Free Space Map)**:每个堆页的空闲空间压成**一个字节**(0~255 的"类别",默认 32 字节一档,255 特殊表示"≥ MaxHeapTupleSize");这些字节组织成**页内二叉树**(父=子之 max)+ **跨页三层树**,默认 3 层覆盖 2³² 个堆页。
- **为什么用树不用数组**:数组找"≥ X 的页"是 O(n);树让"判断有没有"变成 O(1)(看根),"找具体哪个"变成 O(log n)(自顶向下)。
- **更新走冒泡**:改叶子后逐层重算父节点的 max,值没变就提前停;底层变动传到上层靠 VACUUM 的 `FreeSpaceMapVacuum`。
- **VM(Visibility Map)**:每个堆页 2 个 bit——`ALL_VISIBLE`(全可见,让 VACUUM 和仅索引扫描跳页)、`ALL_FROZEN`(全冻结,让防回滚 VACUUM 也跳页)。一页 1/4 字节,比 FSM 更省。
- **一次 INSERT/UPDATE/DELETE 必清 VM 位**:页里一出现对某些事务不可见的 tuple(新插入未提交、新版本、被删旧版本),这页就不再"全可见",`PD_ALL_VISIBLE` 和 VM 的 bit 成对清掉。这是 MVCC 不变量的直接体现。
- **设位写 WAL、清位不单独写**:VM 位虽是 hint,但"设 `ALL_VISIBLE`"因和 `PD_ALL_VISIBLE` 配对、涉及崩溃后一致性,必须 WAL;清位永远安全,可省。
- **FSM 和 VM 都是 hint**:坏了一致性也只损失效率、不损失正确性;FSM 靠搜索时自愈 + VACUUM 刷新,VM 靠 `RBM_ZERO_ON_ERROR` 把坏页当全零。权威数据只在堆表 main fork。

### 回扣主线

本章两个结构,正好横跨"**快**"和"**不丢不乱**"两侧:

- **FSM 服务"快"**:让 INSERT 不必逐页扫表找空位,把"找空页"从 O(n) 降到 O(log n)。它纯粹是对付"磁盘慢、表大"这个本性的效率工具,和正确性无关(错了顶多慢点)。
- **VM 同时服务"快"和"不丢不乱"**:一方面它让 VACUUM 和仅索引扫描跳页(快);另一方面它**承接 MVCC**——VM 的存在前提就是"一个页里可能有对某些事务不可见的 tuple",而这恰恰是 MVCC 多版本机制的产物。没有 MVCC,就不需要 VM;有了 MVCC,VM 是让它高效运转的关键。一次 UPDATE 清掉 VM 位这一动作,把存储层(P2)和事务层(P4)牢牢扣在一起。

至于三个本性:FSM/VM 主要对付"**磁盘慢、表大**"(让扫表变成查地图),VM 的 `ALL_FROZEN` 还牵出"**事务 ID 有限**"这个隐性的第四约束(防 xid wraparound,P6 详讲)。

### 想继续深入

- FSM 全家桶:[src/backend/storage/freespace/README](../postgresql-17.0/src/backend/storage/freespace/README)(树形结构讲得最清楚)、[freespace.c](../postgresql-17.0/src/backend/storage/freespace/freespace.c)(跨页寻址、`fsm_search`)、[fsmpage.c](../postgresql-17.0/src/backend/storage/freespace/fsmpage.c)(页内树、`fsm_search_avail` 的"搜索三角"算法注释值得读)、[fsm_internals.h](../postgresql-17.0/src/include/storage/fsm_internals.h)。
- VM 全家桶:[src/backend/access/heap/visibilitymap.c](../postgresql-17.0/src/backend/access/heap/visibilitymap.c)(开头的 NOTES 注释把 hint 与崩溃安全讲透了)、[visibilitymapdefs.h](../postgresql-17.0/src/include/access/visibilitymapdefs.h)、[visibilitymap.h](../postgresql-17.0/src/include/access/visibilitymap.h)。
- INSERT/UPDATE 怎么调 FSM、怎么清 VM:[src/backend/access/heap/hio.c](../postgresql-17.0/src/backend/access/heap/hio.c) 的 `RelationGetBufferForTuple`、[heapam.c](../postgresql-17.0/src/backend/access/heap/heapam.c) 的 `heap_insert`/`heap_update`。

---

> 存储引擎这一篇到这就齐了:页(Page)、行(Tuple)、内存中转(Buffer Pool)、还有本章的两张辅助地图(FSM/VM)。数据"住哪、怎么在内存里流转、怎么快速被找到空位、怎么被标记可见性"都讲完了。但"快速找到**空位**"只是写入的一面;读取时,`WHERE id = 42` 要**快速找到某一行的位置**,靠的是另一个完全不同的结构——**索引**。翻开 **P3 第 10 章 · 为什么需要索引:顺序扫描 vs 索引扫描**,进入索引篇。那里你会看到:VM 的 `ALL_VISIBLE` 位,正是让"仅索引扫描"省掉回表的关键——本章和索引篇,在 VM 这个 bit 上汇合。
