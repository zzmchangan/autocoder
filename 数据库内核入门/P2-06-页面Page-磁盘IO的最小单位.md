# 第 6 章 · 页面 Page:磁盘 I/O 的最小单位

> **前置**:你需要先读过[第 1 章《第一性原理》](P0-01-第一性原理-为什么需要数据库.md)——它讲了数据三个本性,其中"**磁盘慢、内存易失**"是本章的直接动因;第 1 章末尾还点过 `PageHeaderData` 的 `pd_lsn`。第 5 章是 P1 查询引擎的收尾(执行器把计划跑完,得到一批行);**从本章起,我们进入 P2 存储引擎篇**——视角从"SQL 怎么被理解、被执行",转到"这些数据到底以什么形态躺在磁盘上、又怎么搬进内存"。本章是 P2 的第一章,要先把地基钉死:**数据在磁盘上的最小单位是 8KB 的页,页内字节怎么布局**。后续第 7 章(tuple 怎么落盘)、第 8 章(BufferPool 怎么缓存页)都建立在这套布局之上。

> **核心问题**:数据库存几千万行数据,为什么不是"一行一行"存,而是切成一个个 8KB 的**页(page)**?这 8KB 在磁盘上到底长什么样——开头那 24 字节记了什么、行指针怎么编码、空闲空间在哪、校验和怎么保住"不乱"?
>
> **读完本章你会明白**:
> ① 为什么 I/O 的粒度必须是"页"而不是"行"或"字节",以及为什么偏偏是 8KB(不是 1KB,也不是 64KB);
> ② 页内字节级布局——页头 24 字节、行指针数组从前往后长、tuple 从后往前长、中间是空闲空间,以及用 `pd_lower`/`pd_upper`/`pd_special` 三个偏移精确算出空闲空间;
> ③ **行指针 `ItemId` 的 4 字节位域编码和四种状态**(这预告了 P6 的 HOT 和 VACUUM);页为什么带 checksum(防静默腐败)和版本号;为什么索引页需要一块"特殊区"。

> **如果一读觉得太难**:先记三件事——① 数据按 8KB 页存,顺应磁盘块粒度;② 页两头往中间长,行指针在头部、行数据在尾部,中间是空闲空间;③ 行指针 `ItemId` 只有 4 字节,有四种状态(正常/死/重定向/未用),是后面 VACUUM/HOT 的基础。

---

## 一、为什么 I/O 按"页",不按"行"、更不按"字节"

要理解"页",先回到磁盘和 CPU 的物理本性。

### 1.1 三层硬件都按"块"工作,没有谁按"字节"随机读写

数据最终落在三层硬件上,而**这三层硬件的最小读写单位,没有一层是字节**:

| 层级 | 最小读写单位 | 为什么不能更细 |
|---|---|---|
| SSD / 机械盘 | 扇区(sector,512B 或 4KB)/ NAND 页(常 4KB) | 硬件一次擦写/读取的最小粒度,改 1 字节也得读出-改-写回整块 |
| 操作系统 | 内存页(几乎所有平台都是 4KB) | 虚拟内存、页表、page cache 都按 4KB 管理内存 |
| CPU | cache line(通常 64B),且要按地址对齐访问 | CPU 不会跨 cache line 拆开读一个未对齐的字 |

这些粒度是**物理决定的**,数据库改不了。磁盘说"我一次至少给你一个扇区",OS 说"我按 4KB 页缓存文件",CPU 说"你要的数据最好对齐"——数据库只能**顺应**它们,把自己存数据的单位也对齐到这个粒度上。

> **小白补一句**:"块"和"页"在这里基本是同一个意思——一块连续的字节。硬件层叫"扇区/块",OS 层叫"页",数据库层也叫"页"。下面我们统一叫"页"。

### 1.2 不这样会怎样:按字节/按行裸存,三个灾难

假设数据库真的"一行一行"或"一个字节一个字节"地组织磁盘数据(像你写文本文件那样,想插就插、想删就删),会发生什么?

> **灾难一:读一行也要搬一整块,寻道成本摊不开。**
> 你要读 id=42 的那一行(假设几十字节)。磁盘的物理本性决定了:**它根本无法只给你这几十字节**——它至少得读出一个扇区 / 一个 NAND 页(几 KB)。更糟的是,读之前还要先**寻道**(机械盘把磁头移到对应磁道,要几毫秒)、等盘片旋转到对应扇区。这点字节本身传输几乎不花时间,**时间全耗在"找到那个位置"上**。如果 100 行分散在不同位置,就是 100 次寻道,慢到无法忍受。数据库必须**一次 I/O 读一大块、把好多行塞进去**,才能摊薄寻道成本。

> **灾难二:改一个字节,也是 read-modify-write 整块。**
> 你只想改某行里的一个字段(几个字节)。但磁盘最小写入单位是块,所以硬件实际上得**把整块读出来 → 在内存里改那几个字节 → 把整块写回去**。这叫 read-modify-write。如果数据库按字节裸存,每次小改都要读写一整块,而且还要自己维护"我要的字节在哪个块的哪个偏移"——这套管理逻辑又复杂又慢。直接把"块"当一等公民来管理,反而简单高效。

> **灾难三:无法高效寻址、并发难、缓存也难。**
> 如果数据是平铺的字节流,你想找第 N 行,就得从头扫(或维护一张"第 N 行在第 M 字节"的索引,但这张索引自己又会变)。多个事务同时改不同行时,如果它们恰好挤在同一个块里,就要靠块级锁协调——按字节存连"块"这个概念都没有,并发控制无从下手。BufferPool(第 8 章)要缓存数据,也得有个固定大小的"碗"来装,字节流没法直接缓存。

所以数据库**必须**顺应硬件的块粒度:一次 I/O 读一整块,把好多行塞进一个块里,这个块在 PostgreSQL 里就叫 **页(page)**,默认 **8KB**。

> 这是"磁盘慢"这个本性逼出的第一个设计:**I/O 的粒度,必须匹配硬件的块粒度,而不是逻辑上的行粒度。** 一次读一页,页里装很多行。这也是为什么本章标题强调"磁盘 I/O 的最小单位"——页就是这把最小的尺子。

---

## 二、为什么是 8KB,不是 1KB 或 64KB?

既然要按页,那页多大合适?这是个典型的**权衡(trade-off)**,没有完美答案。PG 选 8KB,是在几对矛盾里找的中间点。

### 2.1 页太小(比如 1KB)的坏处

- **页头占比高**:每页都要有一个页头(PageHeader,固定 24 字节)+ 若干行指针(每个 4 字节)。这是"固定开销"。页越小,固定开销占的比例越大,有效载荷越少。一页 1KB(1024 字节),光是 24 字节页头就吃掉 2.3%;而 8KB 页,页头只占 0.3%。
- **一页能装的行太少**:一行 200 字节,1KB 页去掉头部和指针只能塞几行;查询时要多读很多页才能凑够数据,I/O 次数增加。
- **缓冲池管理结构膨胀**:Buffer Pool 按页管理(每个页对应一个 buffer descriptor)。页越小,同样大的表对应的页数越多、buffer descriptor 数组越大,管理开销上升。

### 2.2 页太大(比如 64KB)的坏处

- **缓冲池粒度太粗**:Buffer Pool 以页为单位缓存、按页淘汰。页太大,一次换入换出搬的数据多,可能搬进一堆现在用不上的行(浪费内存和 I/O 带宽);淘汰时也按整页走,可能把有用的行也一起淘汰了。
- **一次 I/O 搬太多**:你只要页里一行,却要把整个大页读进来,放大了无用 I/O(尤其随机读场景)。
- **锁竞争更激烈**:很多操作是页级粒度的,页越大,并发事务撞在同一页的概率越高,锁等待变多。
- **行指针位域的限制**:`lp_off`/`lp_len` 只有 15 位(下面第四节会讲),所以页大小最大只能 `2^15 = 32768` 字节 = 32KB。**64KB 页在 PG 里根本表示不了**——这是硬上限,不是偏好问题。

> [src/include/pg_config.h.in:24-31](../postgresql-17.0/src/include/pg_config.h.in#L24-L31) 的注释明确写了:`BLCKSZ must be a power of 2. The maximum possible value of BLCKSZ is currently 2^15 (32768). This is determined by the 15-bit widths of the lp_off and lp_len fields in ItemIdData.`——页大小上限是被行指针的位宽卡死的。

### 2.3 8KB 是 PG 的权衡结果

PostgreSQL 选 **8KB**(可配置,但需要重新编译 + initdb,且几乎所有部署都用默认值)。这是一个"不大不小"的中间值:足够装下几十到上百行典型记录,摊薄了页头开销和寻道成本;又不至于大到让缓冲池粒度过粗、也不触及 32KB 的位域上限。这个数字是几十年实战沉淀下来的——MySQL InnoDB 默认 16KB,SQLite 默认 4KB,都是各自场景下的权衡。

> 源码里页大小由 `BLCKSZ` 宏定义(默认 8192),由 `./configure` 在编译期生成、写进 `pg_config.h`。它散布在整个存储子系统里——下面你会看到 `PageAddItemExtended` 校验指针时直接拿它和 `pd_special` 比(`bufpage.c:214` 的 `phdr->pd_special > BLCKSZ`)。

---

## 三、页里长什么样:两头往中间长,中间是空闲空间

8KB 的页,内部不是一坨平铺的字节,而是有明确布局的。关键设计只有一句话:**页是从两头往中间长的。**

### 3.1 页布局字节级详解(先看全景图)

```text
 偏移 0                                                  偏移 8192
   ↓                                                        ↓
  ┌─────────────────────────────────────────────────────────┐
  │  PageHeaderData (固定 24 字节)                            │  ← 页头,SizeOfPageHeaderData
  │  ┌────────────┬───────────┬──────────┬──────────┬─────┐ │
  │  │ pd_lsn (8B)│pd_checksum│ pd_flags │pd_lower  │ ... │ │     pd_lsn: 这页最后一次改动对应的 WAL 位置
  │  │            │  (2B)     │  (2B)    │ pd_upper │     │ │     pd_checksum: 校验和
  │  │            │           │          │pd_special│     │ │     pd_lower/pd_upper/pd_special: 三个偏移
  │  │            │           │          │pd_pagesize_version│ │     pd_pagesize_version: 页大小+版本号打包
  │  │            │           │          │pd_prune_xid│    │ │     pd_prune_xid: 最早可清理的事务号
  │  └────────────┴───────────┴──────────┴──────────┴─────┘ │
  ├─────────────────────────────────────────────────────────┤  ← pd_lower(空闲空间起始)
  │  ItemId[0] │ ItemId[1] │ ItemId[2] │ ...   (每个 4 字节)  │  ← 行指针数组,从前往后长
  │  (lp_off/   (lp_off/                                   │     每项指向一个 tuple 的偏移+长度
  │   lp_flags/  lp_flags/                                 │
  │   lp_len)    lp_len)                                   │
  ├─────────────────────────────────────────────────────────┤
  │                                                         │
  │                    空  闲  空  间                        │  ← 大小 = pd_upper - pd_lower
  │             (新 tuple 从这里塞,tuple 从尾部往前长)         │
  │                                                         │
  ├─────────────────────────────────────────────────────────┤  ← pd_upper(空闲空间结束 = 第一个 tuple 的起点)
  │  Tuple[n] │ ... │ Tuple[2] │ Tuple[1] │ Tuple[0]        │  ← 行数据,从后往前长
  │                                                         │     每行一个 HeapTupleHeader + 字段数据
  ├─────────────────────────────────────────────────────────┤  ← pd_special(特殊区起点)
  │  Special Region(可选,索引页才有;堆页这里为空)            │     pd_special 到页尾,给索引用
  └─────────────────────────────────────────────────────────┘  ← 页末(偏移 8192)
```

四段从前往后依次是:

1. **页头 `PageHeaderData`**:固定 24 字节(由 `SizeOfPageHeaderData` 给出,是 `offsetof(PageHeaderData, pd_linp)`,因为 `pd_linp` 是柔性数组不算进去),钉在页的最前面,记录这页的元信息。
2. **行指针 `ItemId` 数组(即 `pd_linp`)**:从页头**向后**长。每塞进一行,这里就加一个 4 字节的条目,记录"第 N 行的数据在页里的偏移和长度"——它就是页内行的**索引**。
3. **空闲空间**:夹在行指针区和行数据区**中间**,由 `pd_lower`(空闲空间起始)和 `pd_upper`(空闲空间结束)圈出来。新行就从尾部往这块空间里塞。
4. **行数据 `Tuple`**:从页尾**向前**长。新行从 `pd_upper` 往前塞。

(如果这页是索引页,在 tuple 区之后、页尾之前,还可能有一块 **Special Region**,下面第六节讲。)

### 3.2 三个偏移的精确语义:怎么算空闲空间

页头里有三个关键偏移(类型 `LocationIndex`,本质是 `uint15`/`uint16` 级别的偏移量),它们圈定了页内各区的边界:

- **`pd_lower`**:**空闲空间的起始偏移**(= 页头 + 行指针数组的末尾)。它同时也告诉你"这页目前有多少个行指针"——因为行指针数组从页头之后开始、每个 4 字节,所以行指针个数 = `(pd_lower - SizeOfPageHeaderData) / 4`。这一点 `PageGetMaxOffsetNumber` 直接用上了。

> [src/include/storage/bufpage.h:369-378](../postgresql-17.0/src/include/storage/bufpage.h#L369-L378)

```c
static inline OffsetNumber
PageGetMaxOffsetNumber(Page page)
{
	PageHeader	pageheader = (PageHeader) page;

	if (pageheader->pd_lower <= SizeOfPageHeaderData)
		return 0;
	else
		return (pageheader->pd_lower - SizeOfPageHeaderData) / sizeof(ItemIdData);
}
```

- **`pd_upper`**:**空闲空间的结束偏移**(= 第一个 tuple 的起始位置)。tuple 从 `pd_upper` 开始往前排。
- **`pd_special`**:**特殊区的起始偏移**。堆页里它等于页大小(没有特殊区);索引页里它小于页大小,`pd_special` 到页尾这块就是给索引自己用的特殊区。

**空闲空间的大小,就是一个减法:`pd_upper - pd_lower`。** 要塞一行新数据,PG 先算这行对齐后的长度 `alignedSize`,然后检查 `pd_upper - pd_lower >= alignedSize + 4`(多出来的 4 字节是给新行指针准备的)。够就塞,不够就说页满了。下面第五节看 `PageAddItemExtended` 源码时你会看到这个检查的原型。

### 3.3 为什么两头往中间长

> **不这样会怎样**:如果行指针和行数据都从一头开始、顺序紧挨着排,那么加新行时就陷入两难——要么把新行指针挤进行指针区(要搬动后面所有行数据,把整个 tuple 区整体后移),要么把新行数据挤进行数据区(要搬动后面所有行指针)。无论哪种,加一行都要搬动一大片数据,还可能触发 WAL 日志放大。两头往中间长,加行指针只动头部(`pd_lower` 往后挪 4 字节)、加行数据只动尾部(`pd_upper` 往前挪一个 tuple 的长度),**两边互不干扰**,中间的空闲空间被两头蚕食,直到 `pd_lower` 追上 `pd_upper`(页满)。

这个"两段相向生长"的设计,是页布局的核心:**加新行是 O(1) 的——只在头部加个指针、在尾部加个 tuple,不搬动任何已有数据。** 删行也不必搬——只要把对应行指针标记成 `LP_DEAD`/`LP_UNUSED` 留着,等 VACUUM 来收拾(P6 第 21 章)。

---

## 四、行指针 ItemId:页内行的"索引"

上一节说页里有"行指针(ItemId)数组"。现在把它讲透——它是页内每一行的索引,设计极其紧凑,还藏着全书后面两个机制的伏笔。

### 4.1 ItemIdData:4 字节,塞进三个字段

> [src/include/storage/itemid.h:25-30](../postgresql-17.0/src/include/storage/itemid.h#L25-L30)

```c
typedef struct ItemIdData
{
	unsigned	lp_off:15,		/* offset to tuple (from start of page) */
				lp_flags:2,		/* state of line pointer, see below */
				lp_len:15;		/* byte length of tuple */
} ItemIdData;
```

整个行指针只有 **4 字节**(32 位),用**位域(bitfield)**塞进三个字段:

- **`lp_off`(15 位)**:这行数据在页里的**偏移**(从页起始算的字节位置)。
- **`lp_len`(15 位)**:这行数据的**长度**(字节数)。
- **`lp_flags`(2 位)**:这个行指针的**状态**(下面详讲)。

> **为什么用位域压成 4 字节?** 因为一页里可能有上百个行指针(一页塞多少行,就有多少个 ItemId)。如果每个行指针用普通结构体(偏移 + 长度 + 状态各占一个 int),就要 12-16 字节,一页的行指针数组就占掉一大块,挤占了存真实数据的空间。压成 4 字节,空间省了 3-4 倍。15 位偏移 / 长度,最大表示 `2^15 = 32768`,足以覆盖最大 32KB 的页(这也是上一节说的"页大小上限 32KB"的由来)。

> **小白补一句**:什么是位域?C 语言允许在一个 `unsigned` 里按"位"切分字段。这里把一个 32 位的 `unsigned` 切成 `15 + 2 + 15` 三段,`lp_off` 用高 15 位、`lp_flags` 用中间 2 位、`lp_len` 用低 15 位。访问时编译器帮你做移位,你就像用普通字段一样写 `itemid->lp_off`,但实际只占 4 字节。

还有一个约定(注释明说):**`lp_len == 0` 表示这个行指针没有对应的存储**(行数据已被删除,只剩个空指针)。

### 4.2 行指针的四种状态:预告 HOT 和 VACUUM

`lp_flags` 那 2 位,编码出**四种状态**:

> [src/include/storage/itemid.h:38-41](../postgresql-17.0/src/include/storage/itemid.h#L38-L41)

```c
#define LP_UNUSED		0		/* unused (should always have lp_len=0) */
#define LP_NORMAL		1		/* used (should always have lp_len>0) */
#define LP_REDIRECT		2		/* HOT redirect (should have lp_len=0) */
#define LP_DEAD			3		/* dead, may or may not have storage */
```

这四种状态不只是"占位",它们预告了全书后面两个重要机制:

- **`LP_NORMAL`(1)**:正常,指针指向一行的真实数据(有 `lp_off`/`lp_len`)。绝大多数行指针是这个状态。
- **`LP_UNUSED`(0)**:这个槽位空着,可被新行复用。
- **`LP_DEAD`(3)**:**死元组**——这行数据已经没人能看见了(删除事务已提交,或被新版本取代),但还占着页里的空间。它等 **VACUUM** 来清理(P6 第 21 章)。**MVCC 的代价就是产生死元组,而行指针的 `LP_DEAD` 状态正是它的直接体现。**
- **`LP_REDIRECT`(2)**:**HOT 重定向**——这行被更新了,新版本存在页里另一处,旧指针不删,而是"重定向"指向新版本。这样索引还指向旧指针位置,顺着 redirect 就能找到新行,**不必为每次更新都去更新索引**(这是 HOT 优化,P6)。

> **为什么把"状态"和"位置"塞在行指针里?** 因为页里的行会经历"正常 → 死 → 清理"的生命周期,状态机直接编码在行指针里,扫描页时一眼就知道哪些行是活的、哪些是死的该清理。这是 VACUUM 和 HOT 能高效工作的基础。

### 4.3 怎么用行指针定位一行

行指针让"定位一行"变成一个简单的两步查表:

> [src/include/storage/bufpage.h:240-244](../postgresql-17.0/src/include/storage/bufpage.h#L240-L244)

```c
static inline ItemId
PageGetItemId(Page page, OffsetNumber offsetNumber)
{
	return &((PageHeader) page)->pd_linp[offsetNumber - 1];
}
```

注意 `offsetNumber - 1`:对外的行号是 **1-based**(从 1 开始,SQL 用户的世界里没有第 0 行),但数组下标是 0-based。索引(B 树等)里存的就是 `(block号, offset号)`,定位时先用 block 号找到页,再用 offset 号从 `pd_linp` 里取出 `ItemId`,最后用 `lp_off`/`lp_len` 取出真实 tuple:

> [src/include/storage/bufpage.h:351-358](../postgresql-17.0/src/include/storage/bufpage.h#L351-L358)

```c
static inline Item
PageGetItem(Page page, ItemId itemId)
{
	Assert(page);
	Assert(ItemIdHasStorage(itemId));

	return (Item) (((char *) page) + ItemIdGetOffset(itemId));
}
```

`PageGetItem` 就是"页起始地址 + `lp_off` 偏移 = tuple 地址"。**整页的数据寻址,不靠扫,靠两次数组下标 + 一次加法**——O(1)。这就是为什么索引只要存一个 `(block, offset)` 就够了。

> **为什么 tuple 可以在页里移动、但索引不用改?** 因为索引不直接指向 tuple 的字节位置,而是指向行指针(offset 号)。如果 tuple 因为页内整理(compact)挪了位置,只要更新那个 offset 号对应的 `lp_off`,索引存着的 `(block, offset)` 一个字都不用改。这层**间接寻址**是 PG 的关键设计——它让页内布局可以自由调整,而索引保持稳定。

---

## 五、关键源码精读:页头、初始化、加一行

### 5.1 页头 `PageHeaderData`

> [src/include/storage/bufpage.h:155-168](../postgresql-17.0/src/include/storage/bufpage.h#L155-L168)

```c
typedef struct PageHeaderData
{
	/* XXX LSN is member of *any* block, not only page-organized ones */
	PageXLogRecPtr pd_lsn;		/* LSN: next byte after last byte of xlog
								 * record for last change to this page */
	uint16		pd_checksum;	/* checksum */
	uint16		pd_flags;		/* flag bits, see below */
	LocationIndex pd_lower;		/* offset to start of free space */
	LocationIndex pd_upper;		/* offset to end of free space */
	LocationIndex pd_special;	/* offset to start of special space */
	uint16		pd_pagesize_version;
	TransactionId pd_prune_xid; /* oldest prunable XID, or zero if none */
	ItemIdData	pd_linp[FLEXIBLE_ARRAY_MEMBER]; /* line pointer array */
} PageHeaderData;
```

逐字段过一遍(每个字段都不是随便放的):

- **`pd_lsn`(8 字节)**:这页**最后一次改动**对应的 WAL 日志位置(Log Sequence Number)。它是 P5(持久恢复)的命根子:BufferPool 在把脏页刷盘前,必须先保证 WAL 已经 flush 到这页的 `pd_lsn`,否则掉电就丢。第 1 章末尾我们点过这个字段,这里看到它的真身了。
- **`pd_checksum`(2 字节)**:整页的**校验和**,防静默腐败(下面第七节详讲)。
- **`pd_flags`(2 字节)**:状态位,目前用了 3 个 bit(`PD_HAS_FREE_LINES`/`PD_PAGE_FULL`/`PD_ALL_VISIBLE`):

> [src/include/storage/bufpage.h:184-189](../postgresql-17.0/src/include/storage/bufpage.h#L184-L189)

```c
#define PD_HAS_FREE_LINES	0x0001	/* are there any unused line pointers? */
#define PD_PAGE_FULL		0x0002	/* not enough free space for new tuple? */
#define PD_ALL_VISIBLE		0x0004	/* all tuples on page are visible to
									 * everyone */
```

其中 `PD_ALL_VISIBLE`(这页所有行对所有人都可见)是第 9 章可见性映射(VM)和第 13 章仅索引扫描的关键;`PD_HAS_FREE_LINES`(页里有可复用的空行指针槽位)是 VACUUM 复用空间的提示。

- **`pd_lower`/`pd_upper`/`pd_special`**(各 4 字节,`LocationIndex`):第三节讲过的三个偏移,圈出空闲空间和特殊区。
- **`pd_pagesize_version`(2 字节)**:这个字段**同时编码了两件事——页大小(高位)和布局版本号(低位)**。PG 把页大小约束成 256 的倍数(注释原话:`We constrain page sizes to be multiples of 256, leaving the low eight bits available for a version number`),所以低 8 位腾出来存版本号,高位存页大小。为什么打包?因为不同页大小和不同版本的页布局可能并存,读一个页时,这一个字段同时告诉你"这页多大""用哪个版本的布局来解析"。

> [src/include/storage/bufpage.h:203](../postgresql-17.0/src/include/storage/bufpage.h#L203)

```c
#define PG_PAGE_LAYOUT_VERSION		4
```

当前布局版本是 4(注释里能看到版本演进史:0 是 pre-7.3,1 是 7.3/7.4,2 是 8.0,3 是 8.1,4 从 8.3 沿用至今)。

- **`pd_prune_xid`(4 字节,`TransactionId`)**:最早可清理(prune)的事务 ID。给页内清理用——告诉 PG"这页里有些死元组,事务号到了这个值之后就可以清理了"。和 `LP_DEAD` 配合,是 VACUUM 的触发线索。
- **`pd_linp`(柔性数组)**:行指针数组起点。`FLEXIBLE_ARRAY_MEMBER` 是 C99 柔性数组成员,意味着这个数组"长度不定",实际长度由 `pd_lower` 决定。所以 `SizeOfPageHeaderData = offsetof(PageHeaderData, pd_linp)`(到 `pd_linp` 之前的字节数,即 24),不含数组本身。

### 5.2 页初始化:`PageInit`

新建一个空页时(`PageInit`),PG 把页头填好,关键是把 `pd_lower`/`pd_upper`/`pd_special` 摆到正确位置:

> [src/backend/storage/page/bufpage.c:42-60](../postgresql-17.0/src/backend/storage/page/bufpage.c#L42-L60)

```c
PageInit(Page page, Size pageSize, Size specialSize)
{
	PageHeader	p = (PageHeader) page;

	specialSize = MAXALIGN(specialSize);

	Assert(pageSize == BLCKSZ);
	Assert(pageSize > specialSize + SizeOfPageHeaderData);

	/* Make sure all fields of page are zero, as well as unused space */
	MemSet(p, 0, pageSize);

	p->pd_flags = 0;
	p->pd_lower = SizeOfPageHeaderData;
	p->pd_upper = pageSize - specialSize;
	p->pd_special = pageSize - specialSize;
	PageSetPageSizeAndVersion(page, pageSize, PG_PAGE_LAYOUT_VERSION);
	/* p->pd_prune_xid = InvalidTransactionId;		done by above MemSet */
}
```

注意初始化的几个细节:

- **`pd_lower = SizeOfPageHeaderData`(=24)**:刚初始化时,行指针数组是空的,所以空闲空间从页头之后(偏移 24)就开始。
- **`pd_upper = pd_special = pageSize - specialSize`**:空页没有任何 tuple,所以"第一个 tuple 的位置"(`pd_upper`)和"特殊区起点"(`pd_special`)重合——都等于"页大小减去特殊区大小"。堆页 `specialSize=0`,所以 `pd_upper = pd_special = 8192`(整个尾部都是空闲空间);索引页 `specialSize>0`,`pd_special` 在页尾留出那块特殊区,`pd_upper` 紧贴特殊区起点。
- **空闲空间 = `pd_upper - pd_lower`**:空堆页就是 `8192 - 24 = 8168` 字节全可用来塞 tuple。
- `MemSet(p, 0, pageSize)`:整页先清零,这是"页内不能有未初始化字节"的约定(`PageAddItemExtended` 后面会校验)。

### 5.3 加一行:`PageAddItemExtended` 的校验与空间计算

往页里加一行,核心函数是 `PageAddItem`(它转调 `PageAddItemExtended`)。先看它的开头——一道**布局不变量校验**:

> [src/backend/storage/page/bufpage.c:194-218](../postgresql-17.0/src/backend/storage/page/bufpage.c#L194-L218)

```c
PageAddItemExtended(Page page,
					Item item,
					Size size,
					OffsetNumber offsetNumber,
					int flags)
{
	PageHeader	phdr = (PageHeader) page;
	Size		alignedSize;
	int			lower;
	int			upper;
	ItemId		itemId;
	OffsetNumber limit;
	bool		needshuffle = false;

	/*
	 * Be wary about corrupted page pointers
	 */
	if (phdr->pd_lower < SizeOfPageHeaderData ||
		phdr->pd_lower > phdr->pd_upper ||
		phdr->pd_upper > phdr->pd_special ||
		phdr->pd_special > BLCKSZ)
		ereport(PANIC,
				(errcode(ERRCODE_DATA_CORRUPTED),
				 errmsg("corrupted page pointers: lower = %u, upper = %u, special = %u",
						phdr->pd_lower, phdr->pd_upper, phdr->pd_special)));
```

这几行把页的布局**不变量(invariant)**写死了:`pd_lower >= 页头大小`、`pd_lower <= pd_upper`、`pd_upper <= pd_special`、`pd_special <= BLCKSZ`。这四条正是第三节那张布局图里的几何关系。一旦违反,说明这页**被写坏了**(可能磁盘腐败、可能内存踩了),数据库直接 `PANIC` 停机重启——**宁可停,也不能带着坏数据继续跑**。这道校验,是"数据不丢不乱"在页这个粒度的守门员。

接着是空间计算的关键几行(在确认 offset 号合法之后):

> [src/backend/storage/page/bufpage.c:304-320](../postgresql-17.0/src/backend/storage/page/bufpage.c#L304-L320)

```c
	/*
	 * Compute new lower and upper pointers for page, see if it'll fit.
	 *
	 * Note: do arithmetic as signed ints, to avoid mistakes if, say,
	 * alignedSize > pd_upper.
	 */
	if (offsetNumber == limit || needshuffle)
		lower = phdr->pd_lower + sizeof(ItemIdData);
	else
		lower = phdr->pd_lower;

	alignedSize = MAXALIGN(size);

	upper = (int) phdr->pd_upper - (int) alignedSize;

	if (lower > upper)
		return InvalidOffsetNumber;
```

这三步就是"两头往中间长"的代码化身:

1. **`lower = pd_lower + 4`**:加一个新行指针,`pd_lower` 往后挪 4 字节(一个 `ItemIdData` 的大小)。如果是复用已有空槽位(`offsetNumber < limit` 且不需 shuffle),`lower` 不变。
2. **`upper = pd_upper - alignedSize`**:新 tuple 从 `pd_upper` 往前挪一个对齐后的大小。`MAXALIGN(size)` 把 tuple 大小向上对齐到 8 字节(CPU 对齐要求)。
3. **`if (lower > upper) return InvalidOffsetNumber`**:**核心空间检查**。如果新行指针的尾部(`lower`)超过了新 tuple 的头部(`upper`),说明这页塞不下了,直接返回无效 offset(上层会去找新页)。这就是第三节说的"`pd_upper - pd_lower` 算空闲空间"在代码里的落实——只不过这里是"塞进去之后还合不合法",和空闲空间检查等价。

通过检查后,函数才真正写入行指针(`ItemIdSetNormal(itemId, upper, size)`)和 tuple 数据,并更新页头的 `pd_lower`/`pd_upper`。**整个加行过程,不搬动任何已有 tuple、不搬动任何已有行指针**——这是"两头往中间长"设计带来的 O(1) 收益。

---

## 六、页的变体:special 区与不同页类型

页不只一种。PostgreSQL 里,**堆表、各种索引、FSM、VM,都用"页"这个基本容器**,但页内的内容结构各不相同。其中 `pd_special` 字段就是为这种差异准备的。

- **堆页(heap page)**:存表的行数据。页头之后是行指针 + 行 tuple。`pd_special` 通常等于页大小(没有特殊区)。
- **索引页(index page)**:存索引项(B 树节点等)。索引往往需要在页尾留一块**特殊区(special area)** 存自己的元数据(比如 B 树页要存"左右兄弟页的指针"、页内的高键 high key)。`pd_special` 就标出这块特殊区的起点——`pd_special` 到页尾是给索引用的,而 `pd_upper` 到 `pd_special` 之间是普通 tuple 区。访问特殊区有专门的宏:

> [src/include/storage/bufpage.h:336-340](../postgresql-17.0/src/include/storage/bufpage.h#L336-L340)

```c
static inline char *
PageGetSpecialPointer(Page page)
{
	PageValidateSpecialPointer(page);
	return (char *) page + ((PageHeader) page)->pd_special;
}
```

- **FSM 页 / VM 页**:空闲空间映射(Free Space Map)、可见性映射(Visibility Map,第 9 章),是结构完全不同的辅助页(FSM 页是一棵树形位图,VM 页是位图)。

> **为什么索引页需要额外空间(回扣 P3 B 树)?** B 树的每个节点都是一个页。B 树节点之间要互相链接(左兄弟、右兄弟),还要存"这页里所有 key 的上界"(high key,用于判断该不该往兄弟节点走)。这些信息不属于"索引项",塞不进普通的行指针+tuple 结构,所以索引页专门留一块 special 区放它们。这就是为什么 special region 主要服务于索引。第 11 章 B 树会详讲。

> **为什么用"页"这个统一容器,而内容各异?** 因为 I/O、Buffer Pool、WAL 这些基础设施都是按"页"工作的(一次 I/O 一页、缓冲池按页缓存、WAL 按页记日志)。只要堆表、索引、FSM 都封装成"页",就能**复用同一套 I/O 和缓冲机制**。至于页内装什么,由各子系统自己解释(靠 `pd_special` 等字段区分)。这是"统一接口、各自实现"的抽象——和操作系统的"一切皆文件"异曲同工。

---

## 七、页校验和:防住"静默腐败"

### 7.1 什么是静默腐败,为什么比崩溃还可怕

磁盘、SSD、SATA 线缆、RAID 卡、内存——任何一环都可能**悄悄把数据写坏**(位翻转、坏道、传输错误、宇宙射线击中内存芯片)。可怕之处在于:**数据库和上层应用完全察觉不到**。一次 SELECT 读出来一个错的字段,但你以为它是对的,继续基于它做计算、写别的表——错误就这么悄无声息地扩散开。

> **不这样会怎样**:没有校验和,数据库读到坏数据时,可能表现为:
> - 行指针 `lp_off` 指向了一个错的位置,扫描时读出一堆乱码甚至越界 → 崩溃;
> - 某个字段的值被翻转了一个 bit(比如存款从 1000 变成 1004),没人发现 → **数据错了,但系统以为是对的**,继续跑;
> - 索引项指向了错误的堆页 → 查询返回错行。
>
> 崩溃你至少知道(进程挂了、有报错),重启后用 WAL 恢复;**腐败是悄无声息地污染,等你发现时可能已经错了一大堆,且无法定位是哪一刻开始的**。所以静默腐败比崩溃可怕得多。

### 7.2 校验和怎么工作

页头里的 `pd_checksum`(2 字节)就是为此而生。每次把一页写盘前,PG 对**整个页的内容**算一个校验和,存进 `pd_checksum`;每次从磁盘读一页进来,重新算一遍校验和,和存着的值比,对不上就报 checksum failure。

算法是基于 **FNV-1a 哈希**改的(PG 没直接用 FNV-1a,因为高位混合不好,做了改良):

> [src/include/storage/checksum_impl.h:25-40](../postgresql-17.0/src/include/storage/checksum_impl.h#L25-L40)(节选注释)

```text
 * The checksum algorithm itself is based on the FNV-1a hash ...
 * The primitive of a plain FNV-1a hash folds in data 1 byte at a time
 * according to the formula:
 *	   hash = (hash ^ value) * FNV_PRIME
 ...
 * PostgreSQL doesn't use FNV-1a hash directly because it has bad mixing of
 * high bits ... To resolve this we xor in the value prior to multiplication
 * shifted right by 17 bits. ...
 * For performance reasons we choose to combine 4 bytes at a time.
```

注释第一段就点出选型理由:**算法必须算得非常快**——因为热路径上工作集在 OS file cache 里但不在 shared buffers 时,读页速度极快,校验和本身可能成为最大瓶颈。所以选了 FNV-1a 这种极轻量哈希,还一次处理 4 字节加速。

还有一个精巧设计:**校验和里混入了页号(block number)**。

> [src/include/storage/checksum_impl.h:187-207](../postgresql-17.0/src/include/storage/checksum_impl.h#L187-L207)

```c
pg_checksum_page(char *page, BlockNumber blkno)
{
	PGChecksummablePage *cpage = (PGChecksummablePage *) page;
	uint16		save_checksum;
	...
	checksum = pg_checksum_block(cpage);
	cpage->phdr.pd_checksum = save_checksum;

	/* Mix in the block number to detect transposed pages */
	...
```

为什么混入页号?为了检测**整页错位**——如果文件系统或存储把第 5 页的内容写到了第 8 页的位置,光对内容算校验和是发现不了的(内容本身没错)。混入页号后,内容对得上但页号对不上,校验和就对不上,这种"页被搬错地方"的腐败也能抓出来。

### 7.3 默认开还是关

PG 的页校验和由 `data_checksums` 这个 GUC(配置参数)控制,**在 `initdb` 时决定**:`initdb` 默认开启校验和。一旦建库时定了,后面要改只能用 `pg_checksums` 工具 offline 切换(全表重算一遍)。

> **为什么是 initdb 时定、而不是运行时随便开关?** 因为校验和写在每页的 `pd_checksum` 字段里,是**每页都有的元数据**。关掉校验和的库,页里的 `pd_checksum` 可能是 0 也可能是历史遗留值(PG 9.3 之前这个字段存的是 timelineid);开启时必须把所有页的校验和重新算一遍并落盘——这是个全库扫描操作,不能在线随便做。

读页时校验的逻辑在 `PageIsVerifiedExtended`:

> [src/backend/storage/page/bufpage.c:103-122](../postgresql-17.0/src/backend/storage/page/bufpage.c#L103-L122)

```c
		if (DataChecksumsEnabled())
		{
			checksum = pg_checksum_page((char *) page, blkno);

			if (checksum != p->pd_checksum)
				checksum_failure = true;
		}

		/*
		 * The following checks don't prove the header is correct, only that
		 * it looks sane enough to allow into the buffer pool. Later usage of
		 * the block can still reveal problems, which is why we offer the
		 * checksum option.
		 */
		if ((p->pd_flags & ~PD_VALID_FLAG_BITS) == 0 &&
			p->pd_lower <= p->pd_upper &&
			p->pd_upper <= p->pd_special &&
			p->pd_special <= BLCKSZ &&
			p->pd_special == MAXALIGN(p->pd_special))
			header_sane = true;
```

注意这里有两层检查:**校验和**(抓内容损坏)和**页头结构合理性**(`header_sane`,抓指针越界)。校验和是"强"检查(但算起来贵),页头合理性是"弱"检查(几乎免费,先用它过滤一遍)。

> 一个有意思的设计取舍:`pd_checksum` 字段本身**不参与校验和计算**(注释里写明 `there is no indication on a page as to whether the checksum is valid or not, a deliberate design choice`)——因为校验和不能把自己也校验了。算之前先把 `pd_checksum` 临时存起来(`save_checksum`)、清零、算完、再恢复。这个细节体现了 PG 内核对"自指"问题的谨慎。

---

## 八、章末小结

- I/O 按"页"不按"行"也不按"字节",是**磁盘/OS/CPU 三层都按块工作**这个物理本性逼出来的(顺应硬件粒度,摊薄寻道,避免 read-modify-write,让并发和缓存有抓手)。
- 8KB 是页头开销、行容量、缓冲池粒度、以及 `lp_off`/`lp_len` 15 位位域上限(32KB)之间的**权衡**结果。
- 页**两头往中间长**:页头(24 字节)+ 头部行指针数组、尾部 tuple、中间空闲空间,由 `pd_lower`/`pd_upper`/`pd_special` 三个偏移精确划定;**空闲空间 = `pd_upper - pd_lower`**;加新行是 O(1)(头部加指针、尾部加 tuple,不搬动已有数据)。
- **行指针 `ItemId` 只有 4 字节**(位域:`lp_off:15`/`lp_flags:2`/`lp_len:15`),四种状态(`NORMAL`/`UNUSED`/`DEAD`/`REDIRECT`)——其中 `LP_DEAD` 和 `LP_REDIRECT` 分别预告了 P6 的 VACUUM 和 HOT。索引通过 `(block, offset)` 经行指针间接定位 tuple,tuple 页内移动不影响索引。
- 页带 `pd_checksum`(基于 FNV-1a 改良、混入页号、防静默腐败、initdb 默认开)和 `pd_pagesize_version`(页大小 + 版本打包,当前版本 4)。
- 页是统一容器(堆页 / 索引页 / FSM 页 / VM 页),靠 `pd_special` 区分:索引页在页尾留 special 区放 B 树兄弟指针/高键等,堆页没有 special 区。复用同一套 I/O / 缓冲 / WAL。

### 回扣主线

本章对付的是"**磁盘慢**"这个本性——按页 I/O,正是为了让慢吞吞的磁盘少寻道、多办事;页布局的两头生长设计,又是为了在页内操作时尽量少搬数据、少写日志。而 `pd_checksum` 和那道 `PANIC` 校验,守的是"**不丢不乱**"——在页这个粒度,不让坏数据混进去。`pd_lsn` 字段为 P5(持久恢复)埋了伏笔(它锁住"WAL 必须先 flush 到这页 LSN 才能刷脏页"这条铁律);`LP_DEAD` 又悄悄为"MVCC 的代价(VACUUM)"埋下了伏笔(第 14、21 章)。

### 想继续深入

- 页结构与操作全集:[src/include/storage/bufpage.h](../postgresql-17.0/src/include/storage/bufpage.h)(结构定义、所有 `PageGet*` 内联函数、顶部 109-153 行有官方 ASCII 布局注释)、[src/backend/storage/page/bufpage.c](../postgresql-17.0/src/backend/storage/page/bufpage.c)(`PageInit`/`PageAddItem`/`PageRepairFragmentation`/`PageIsVerifiedExtended`)。
- 行指针的细节:[src/include/storage/itemid.h](../postgresql-17.0/src/include/storage/itemid.h)(四种状态的注释讲得清楚,还有 `ItemIdIsUsed`/`ItemIdIsNormal`/`ItemIdIsDead` 等判定宏)。
- `BLCKSZ` 的定义与约束:[src/include/pg_config.h.in:24-31](../postgresql-17.0/src/include/pg_config.h.in#L24-L31)(注释说清了为什么最大 32KB),实际值在编译期由 `./configure --with-blocksize` 写进 `pg_config.h`。
- 校验和算法:[src/include/storage/checksum_impl.h](../postgresql-17.0/src/include/storage/checksum_impl.h)(FNV-1a 改良,带详细注释),GUC 控制:[`data_checksums`](../postgresql-17.0/src/backend/utils/misc/guc.c) 与 `pg_checksums` 工具。
- 动手观测:装 `pageinspect` 扩展,用 `get_raw_page('表名', 页号)` + `heap_page_items()` 能肉眼看到一页里的页头和每个行指针的真实字节——强烈建议跑一次,把抽象的布局图变成眼见为实的十六进制。

---

> 页这个容器有了,行指针也懂了。但页里塞的"行(tuple)"自己长什么样?一行有定长字段、有变长字符串、有 NULL、还有第 1 章见过的 `t_xmin`/`t_xmax`——它们在页的空闲空间里怎么排?为什么 tuple 还要再套一个 `HeapTupleHeader`?翻开 **第 7 章 · 堆表与元组:一行数据怎么落盘**。
