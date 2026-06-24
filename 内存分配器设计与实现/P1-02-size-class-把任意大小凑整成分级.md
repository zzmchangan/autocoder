# 第二章 · size class:把任意大小凑整成分级

> 篇:P1 共通地基——三层快慢道与 size class
> 主线呼应:上一章我们立起了三层快慢道("本地缓存秒拿 → 中心补货 → 页堆批发")和"局部缓存 vs 中心堆"的二分法。但真要走进"一次 `malloc` 的第一步",会撞上一个看似最朴素的岔路口:**用户给的是一个任意的 `size`(比如 17、99、233),而分配器内部不可能为每一个 size 都开一条库存**。这一章就是来拆这第一步——分配器怎么把"任意 size"算成一个"分级"(size class),以及这个"凑整"为什么反而是分配器**快起来**的前提,而不是浪费的代名词。

## 核心问题

**为什么分配器不按你申请的大小原样分配(每个 size 一条 free list),而是把所有大小凑整成几十个"分级"(size class),每个分级一条链?凑整会带来内部碎片,这笔账为什么划算?四套分配器分别怎么把 size 算成 class,而且算得几乎不花时间?**

读完本章你会明白:

1. **朴素方案为什么会爆**:每个 size 一条 free list,free list 数量爆炸、缓存结构爆炸、批量取还无从谈起——这是"按申请大小原样分配"的天花板。
2. **size class 的本质是一次"归类换 O(1)"**:把连续的大小轴归并成几十个离散的桶(few dozens),换来了"查表/位运算算 class"的 O(1)、换来了"每个 class 一条独立缓存"的可批量性。代价是**内部碎片**(要 17B 给 32B),这笔账在统计平均上是划算的。
3. **四套的真实分级表**:tcmalloc 默认 **46 个 base class**(`8/16/32/.../262144`)、jemalloc 默认 **几十到上百个 class**(小对象细、大对象按 2 倍步长)、mimalloc **73 个 bin**、ptmalloc 的 fastbin/smallbin/largebin 三段式——四套策略不同,都在"分级粒度 vs 内部碎片"之间权衡。
4. **size → class 这个映射是分配器最热的代码**,它必须**几条指令就算完**。tcmalloc 用一张"按 size 索引的扁平数组 + 两次位移"(`ClassIndexMaybe`)、jemalloc 用**纯位运算从 size 算 class index**(分组数学,根本不查表)、mimalloc 用"高 3 位 + 高位 bit"(`mi_bin`)、ptmalloc 用"三段式:小对象除法步长 + 大对象位运算分级"(`smallbin_index`/`largebin_index_64`)。

> **如果一读觉得太难**:先只记住三件事——① size class 就是"把任意大小凑整到几十个分级里",代价是内部碎片、收益是 O(1) 查 class + 可批量;② 每套分配器都有一张固定的 size 表,分配器一启动就算好、之后**绝不动态改**;③ "size → class"这条映射是整个分配器最热的指令路径之一,所以它必须用位运算或扁平数组,**不能是二分查找**。

---

## 2.1 一句话点破

> **`malloc(17)` 不会真的给你一块 17 字节的块,它给你的是一块属于某个 size class(比如 32 字节那一级)的块。size class 是分配器在"按申请大小原样分配(碎片和库结构爆炸)"与"统一一个大小(浪费太大)"之间,用几十个离散的分级找的折中。这个折中换来了三样东西:一条 O(1) 的 size→class 映射、每个 class 一条独立可批量的 free list、以及一个可以用位运算精算出来的紧凑编码。本章要拆的,就是这"几十个分级"是怎么定的、size 又是怎么几条指令算到 class 的。**

这是结论。本章倒过来拆:先看"每个 size 一条链"为什么会爆(2.2),再看"所有大小统一一个块"为什么不现实(2.3),然后看 size class 这个折中是怎么定的、内部碎片这笔账到底有多大(2.4),接着四套源码并排对照(2.5),最后进"技巧精解",把 size→class 这条最热的映射逐位拆透(2.6)。

---

## 2.2 朴素方案:每个 size 一条 free list,行不行?

顺着上一章的思路,我们已经知道:`malloc` 要"快",就得让绝大多数分配**在本地缓存里一击命中**。本地缓存按什么组织?最直觉的答案是:**按 size 组织**。`malloc(17)` 去本地缓存里找"17 字节那条链",`malloc(99)` 找"99 字节那条链",每条链都是同 size 的空闲块串成。

这个方案听上去天经地义——**申请多少、缓存多少,零内部碎片**。但它第一天就崩,原因有三:

**第一,free list 的数量会爆炸。** 一个真实程序里出现的 size 是高度发散的:`malloc(13)`、`malloc(17)`、`malloc(23)`、`malloc(89)`……可能上千种。如果每个 size 一条链,那本地缓存(每个线程/CPU 一份)就要存上千条链、每条链一个 head 指针、一个 length。一个有几百个线程的服务,光是这些 head 指针的元数据,就能吃掉几十 MB——而这些内存大部分链是空的(绝大多数 size 只被申请过一两次)。

**第二,"批量取还"无从谈起。** 本地缓存空了,得去中心链表批量补货;满了,得批量退回。如果按 size 粒度做,中心链表也得上千条,每条一把锁——锁争用爆炸。而且"批量"的语义也瓦解了:中心链表里"17 字节那条链"如果只有 3 个块,你怎么批量?补 3 个、用完再去,锁开销根本平摊不掉。

**第三,缓存亲和性瓦解。** 现代分配器一个核心优化是"对象在 cache line 里紧凑排列"。如果每个 size 一条链,块和块之间大小不一,放进 span/page 里会到处是空洞,缓存行利用率低。

> **不这样会怎样**:朴素"每个 size 一条链"看似零内部碎片,实则把成本全转嫁到了**元数据爆炸 + 锁爆炸 + 批量瓦解**上。这是分配器设计里典型的"省了一个维度的浪费,却在另一个维度上爆炸得更快"。

> **不这样会怎样**:朴素"每个 size 一条链"看似零内部碎片,实则把成本全转嫁到了**元数据爆炸 + 锁爆炸 + 批量瓦解**上——一个几百线程的服务,光是空链的 head 指针就能吃几十 MB,中心链表的锁争用又因为"每条链都稀疏"而平摊不掉。这是分配器设计里典型的"省了一个维度的浪费,却在另一个维度上爆炸得更快"。

所以分配器必须**归并**。但归并到什么粒度?

---

## 2.3 反向极端:所有大小统一一个块,行不行?

另一个极端是:**别管你申请多少,一律给你一个"够大"的块**(比如统一 256 字节)。`malloc(8)` 给 256,`malloc(200)` 也给 256。这样 free list 只有一条,组织极简,批量极顺。

这同样一问就破:**内部碎片爆炸**。程序里绝大部分 `malloc` 是小对象(`malloc(16)`、`malloc(24)` 是绝对主力),统一给 256 字节意味着**每次都浪费 90%+**。一个分配 1GB 对象的程序,真实占用会膨胀到 10GB——这种分配器没人用。

> **钉死这件事**:"每个 size 一条链"和"所有 size 一个块"是两个错误极端。前者元数据爆炸、后者内部碎片爆炸。**size class 就是这两者之间的折中**——把连续的 size 轴,归并成**几十个**离散的分级,每级一条链:既不让链爆炸(几十条链管理得过来、锁平摊得开),又把内部碎片压到可接受(每级只比用户要的大一点点)。

---

## 2.4 size class 这道折中题:几十个分级是怎么定的

现在我们看清了 size class 的本质:把 `[0, kMaxSize]` 这段连续的 size 轴,切成几十个**区间** `[prev+1, cur]`,每个区间对应一个 class(大小为 `cur`)。`malloc(n)` 找到 `n` 落在哪个区间,就给一个 `cur` 大小的块。

这里有两个关键决策:

**决策一:分多少个 class?** 分得越多,内部碎片越小(每个 class 只比申请大一点点),但 free list 越多、元数据越大、cache 命中率受影响。分得越少,内部碎片越大,但结构紧凑。四套分配器都在"几十个"这个量级上稳定下来——这是个经验得出的甜区(sweet spot)。

**决策二:这些 class 的具体大小怎么定?** 这是真正考功夫的地方。一个朴素直觉是"等距":每 16 字节一个 class。但这浪费——`malloc(8)` 给 16(浪费 50%)、`malloc(1024)` 给 1024(没浪费,但要开 64 条链才覆盖到 1024)。真实程序的 size 分布是**重尾的**:小对象极多(8、16、24、32、48、64),大对象也有但稀疏(几 KB、几十 KB)。所以分配器普遍用**变步长**:小对象步长细(每 8/16 字节一档,因为小对象多、碎片敏感),大对象步长粗(按 2 倍递增,因为大对象稀疏、对齐到 2 的幂也方便位运算)。

我们看 tcmalloc 默认配置(8KB 页,`PAGE_SHIFT == 13`)的真实 class 表的前几档,这是它最热的区间:

| class | size(字节) | 步长 | 说明 |
|------|------------|------|------|
| 1 | 8 | — | 最小对齐 |
| 2 | 16 | +8 | 小对象细步长 |
| 3 | 32 | +16 | |
| 4 | 64 | +32 | |
| 5 | 80 | +16 | 80、96、112——为了照顾常见的 64B+header 的对象 |
| 6 | 96 | +16 | |
| 7 | 112 | +16 | |
| 8 | 128 | +16 | |
| 9 | 160 | +32 | |
| 10 | 176 | +16 | |
| 11 | 208 | +32 | |
| 12 | 256 | +48 | |
| … | … | … | 步长逐渐变大 |
| 20 | 1024 | — | 小/大对象的分界(`kLargeSize`) |
| … | … | … | 1024 以上步长更粗 |
| 47 | 262144 | — | 最大 class(256KB),超过走页堆 |

这张表出自 [tcmalloc 的 size_classes.cc](../tcmalloc/tcmalloc/size_classes.cc#L57-L108)(默认 8KB 页配置,共 **47 个 class**,含 class 0 占位,实际可用 46 个),每一行的 `bytes`/`span_bytes`/`batch` 三个字段就是这张表。注意几个设计:

- **前 8 档(8~128)步长极细**(8 或 16 字节),因为这是程序里出现频率最高的区间,内部碎片必须压到最低。源码注释里 tcmalloc 给每档算了"动态开销"(`inc` 列,即比上一档的增量百分比),小对象这区基本控制在 **10%~25%**,这是 tcmalloc 调了很多年得出的甜区。
- **1024 是一个分水岭**(`kLargeSize`,见 [sizemap.h:58](../tcmalloc/tcmalloc/sizemap.h#L58))。1024 及以下,class 按 8 字节对齐索引(下一节技巧精解会拆);1024 以上,按 128 字节对齐索引。**同一个分界在 `ClassIndexMaybe` 里被字面写死**(`s <= kLargeSize` 用 `>>3`,`> kLargeSize` 用 `>>7`)。
- **大对象档(1024 以上)步长变粗**:1024→1152→1408→1792→2048→2688→3456……近似按 1.5~2 倍递增。因为大对象稀疏,凑整到 2 倍附近的内部碎片百分比反而下降(要 1.5KB 给 1.75KB,浪费 14%;要 1KB 给 1.28KB,浪费 22%)。

> **所以这样设计**:size class 不是随便分的几十档,而是**按程序 size 分布的经验密度**精调的——小对象密,档分细;大对象稀,档分粗;2 的幂附近多放档(对齐方便)。这张表是分配器"快又省"的地基,**一旦定下就编译期写死、运行时绝不改**(`static constexpr SizeClassInfo List[]`),因为改它会牵动整条缓存链。

### 内部碎片这笔账到底有多大

我们别让"内部碎片"停留在口号。算一下:**最坏情况下**,你要的 size 是某个 class 的"下界 +1",而 class 给你的是"上界",浪费率 = `(class_size - requested) / class_size`。比如 class=32,你要 17,浪费 = 15/32 ≈ **47%**。听起来吓人。

但**平均**情况下,假设 size 在每个区间内均匀分布,平均浪费率 ≈ `步长 / (2 × class_size)`。tcmalloc 的小对象档步长约是 class 的 10%~25%,平均浪费率约 **5%~12%**。大对象档步长更粗,但因为大对象本身稀疏、占总字节数小,综合下来 tcmalloc 的整体内部碎片率实测在 **10%~12%** 量级。这个数字分配器圈里有个公认目标:**<15%**——超过这个数,内存占用就不划算了。tcmalloc 这张表正是按"把碎片压在 12% 以内、同时 class 数不超过几十"优化出来的(源码注释的 `waste/fixed`、`waste/sampling`、`inc` 三列就是这套精算的产物)。

> **钉死这件事**:size class 不是"为了简单而容忍浪费",而是"**用一小撮可统计的内部碎片(10%~12%),换来 free list 数量可控(几十条)、批量取还可摊销锁、size→class 还能用位运算精算**"。这笔账在工程上极其划算——你损失 10% 的内存,换来了纳秒级的 fast path 和高并发下的低争用。

---

## 2.5 四套横评:size class 表长什么样

四套分配器的 size class 策略,恰好是"同一道题的四种解法"。我们把它们的**入口、表的组织、class 数量**并排:

| 维度 | tcmalloc | jemalloc | mimalloc | ptmalloc(baseline) |
|------|----------|----------|----------|----------|
| **表怎么生成** | 编译期 `static constexpr` 数组([size_classes.cc](../tcmalloc/tcmalloc/size_classes.cc)) | **运行时 `sc_boot` 计算**([sc.c:304](../jemalloc/src/sc.c#L304)),由几个常量推 | **运行时按位运算推导**(`mi_bin` 纯算法) | 编译期宏(`fastbin_index`/`smallbin_index`/`largebin_index_64`) |
| **小对象步长** | 8/16 字节 | tiny(2/4/8B)+ quantum(16B 起) | 8 字节(word)起步 | **固定 16 字节**(`SMALLBIN_WIDTH = MALLOC_ALIGNMENT`) |
| **大对象步长** | 按 2 倍近似递增 | 按 group(NGROUP=4)递增 | 12.5% 递增(`b<<2`) | 按 `>>6`/`>>9`/`>>12`/`>>15`/`>>18` 多段 |
| **class 数量** | 默认 **46 个 base class**(8KB 页配置) | 默认约 **几十~上百个**(可配置) | **73 个 bin**(`MI_BIN_HUGE`) | **128 个 bin**(NSMALLBINS=64 + 大 bin) |
| **小/大分界** | `kLargeSize = 1024` | `SC_SMALL_MAXCLASS`(页×group) | `MI_SMALL_SIZE_MAX = 1024` | `MIN_LARGE_SIZE = 1024`(64-bit) |
| **size→class 入口** | `SizeMap::GetSizeClass`([sizemap.h:210](../tcmalloc/tcmalloc/sizemap.h#L210)) | `sz_size2index`([sz.h:220](../jemalloc/include/jemalloc/internal/sz.h#L220)) | `mi_bin`([page-queue.c:60](../mimalloc/src/page-queue.c#L60)) | `smallbin_index`/`largebin_index_64`(在线 [malloc.c](https://github.com/glibc/glibc/blob/main/malloc/malloc.c)) |

下面把每一套拆几句。

### tcmalloc:编译期写死的精调表

tcmalloc 的做法最"老派但有效":**size class 表是编译期 `static constexpr` 写死的**。它有三个候选配置,运行时根据 `TCMALLOC_PAGE_SHIFT` 和实验开关选一张:

- **默认**([size_classes.cc](../tcmalloc/tcmalloc/size_classes.cc#L57)):8KB 页时 47 个 class(含 class 0),256KB 封顶。
- **pow2 实验**([experimental_pow2_size_class.cc](../tcmalloc/tcmalloc/experimental_pow2_size_class.cc#L57)):纯 2 的幂(8/16/32/64/128/256/.../262144),只有 **17 个 class**——极端少,用于研究"class 少 + pow2 对齐"对 cache/碎片的影响。内部碎片明显变大(要 33B 给 64B,浪费 48%),但 cache line 利用率拉满。
- **legacy**([legacy_size_classes.cc](../tcmalloc/tcmalloc/legacy_size_classes.cc#L57)):更细的小对象档(8/16/24/32/40/48/56/64……),class 数更多(~86),碎片更低但元数据更大。

这种"编好几张表、编译期/启动期挑一张"的设计,好处是**可以针对不同负载精调**(研究型分配器的姿态),坏处是改表要重编。`SizeMap::Init` 在启动时把选中的表灌进三张热数组:`class_to_size_`、`class_to_pages_`、`num_objects_to_move_`(见 [sizemap.h:158/155/131](../tcmalloc/tcmalloc/sizemap.h)),以及最重要的——**把 size→class 反向映射预填进一张按 size 索引的扁平数组 `class_array_`**(下一节拆)。

### jemalloc:运行时计算 + 查表/位运算双路

jemalloc 的风格不同:**class 表不是写死的常量数组,而是由 `sc_boot` 在启动时从几个核心常量算出来**([sc.c:304](../jemalloc/src/sc.c#L304),调 `sc_data_init`→`size_classes`)。这几个核心常量是:

- `SC_LG_TINY_MIN = 3`(最小对象 8 字节)
- `LG_QUANTUM = 4`(64-bit 上 quantum=16 字节,小对象按此对齐)
- `SC_LG_NGROUP = 2`(每个 group 4 个 class)
- `LG_PAGE`(页大小)
- `SC_LG_MAX_LOOKUP = 12`(查表上限 4096 字节)

`size_classes` 函数([sc.c:85](../jemalloc/src/sc.c#L85))用**双层循环**生成表:外层 `lg_base` 递增(2 倍步长),内层 `ndelta` 在每个 base 上加 `delta` 的整数倍。每个 class 存三个字段(`sc_t`,[sc.h:314](../jemalloc/include/jemalloc/internal/sc.h#L314)):`lg_base`(基础大小的 log2)、`lg_delta`(增量的 log2)、`ndelta`(增量的倍数)。**真实大小 = `(1<<lg_base) + (ndelta<<lg_delta)`**。这种编码很紧凑——一个 class 只占十几个字节,几百个 class 也只占几 KB。

然后 jemalloc 还会把这张表**反向预填成两张查找表**([sz.c:73/89](../jemalloc/src/sz.c#L73)):

- `sz_index2size_tab[SC_NSIZES]`——class index → size,直接数组下标。
- `sz_size2index_tab[]`——size → class index,按 `(size + 7) >> 3` 索引,**只覆盖到 `SC_LOOKUP_MAXCLASS = 4096`**(因为表不能太大)。

4096 字节以上的 size,**不查表,直接用位运算算**(`sz_size2index_compute_inline`)。这个位运算就是 jemalloc 最硬核的技巧,下一节详拆。

### mimalloc:73 个 bin,纯位运算推导

mimalloc 的 size class(它叫 **bin**)策略最"算法化":**73 个 bin,用 `mi_bin` 这个纯函数从 size 算出来,根本不存表**([page-queue.c:60](../mimalloc/src/page-queue.c#L60))。算法分三段:

1. **8 个 word 以内**(≤64 字节,64-bit):每个 word 一个 bin,精确无碎片(`bin 1~8`)。
2. **9 个 word 到 medium 上限**:`wsize--`,取最高位 `b = mi_clz` 反,**高 2 位**拼到 `(b<<2)`,这样每 4 倍区间里分 4 档,**最坏内部碎片 12.5%**。
3. 超过 medium:`MI_BIN_HUGE`(走大块路径)。

mimalloc 的设计哲学是"少表、多算"——`mi_bin` 是 `static inline`,在 fast path 上**几条指令就算完**,不依赖任何全局表。但**注意**:mimalloc 还有一层 `pages_free_direct[]` 数组([types.h:578](../mimalloc/include/mimalloc/types.h#L578)),这是**真正在 fast path 上用的查表**——按 wsize 直接索引到 page。我们下一节会把它和 `mi_bin` 的关系讲清。

### ptmalloc(baseline):三段式 bins,经典但粗

glibc 的 ptmalloc 没有 tcmalloc/jemalloc 那种"几十个 size class"的统一概念,它的等价物是**三段式 bins**(在线 [malloc.c](https://github.com/glibc/glibc/blob/main/malloc/malloc.c)):

- **fastbin**:`MAX_FAST_SIZE = 80 * SIZE_SZ / 4 = 160` 字节(64-bit),每 **16 字节**一档,共 **10 个** fastbin(`NFASTBINS`)。这是最热的路径,LIFO 单链表、无合并。
- **smallbin**:`NSMALLBINS = 64`,从 ~32 字节到 ~1024 字节,**固定 16 字节步长**(每档一个 bin,双向链表)。
- **largebin**:`NBINS = 128` 总共,largebin 占剩下的。大对象按**多段位运算**分级——`largebin_index_64(sz)` 宏([codebrowser/elixir](https://codebrowser.dev/glibc/glibc/malloc/malloc.c.html) 上可查)是一串嵌套三元 + 位移:`>>6` 算 48 以内、`>>9` 算到 ~25600、`>>12` 算到 ~40000、`>>15` 算到 ~128KB、`>>18` 算到更大。**这是 glibc 自己的 size class 位运算**(和 jemalloc 的分组数学异曲同工,但更手工、更不规整)。

ptmalloc 的 size class 有两个鲜明的"baseline 印记":

1. **fastbin 上限只有 160 字节**——也就是说,绝大多数中等对象(几百字节到 1KB)在 ptmalloc 里**根本享受不到 fastbin 的无锁待遇**,只能走 smallbin(带 arena 锁)。这正是 tcmalloc/jemalloc 用更细的 size class + 本地缓存补的窟窿。
2. **ptmalloc 没有独立的"本地缓存"概念**(tcache 是后加的,见上一章)——它的 bins **全是全局争用的**。所以 size class 在 ptmalloc 里只是"分桶",没有"换无锁"的红利。这是它在新一代分配器面前露怯的根本原因之一。

> **钉死这件事**:四套 size class 表,从粗到细排个序:**pow2-tcmalloc(17 个)< mimalloc(73 个)< tcmalloc-默认(46)/ jemalloc(几十~上百)< ptmalloc(128 个 bins)**。但"bins 多"不等于"先进"——ptmalloc 的 128 个 bin 是为了**补偿它没有本地缓存**(得多分桶才能降低单桶锁争用),而新一代分配器**用本地缓存替代了"分桶降争用"**,所以 class 数反而可以更少、更精。

---

## 2.6 技巧精解:size → class,这条最热的路径凭什么几条指令就算完

讲到这里,我们到了本章最值钱的地方。前面所有内容都是在回答"为什么要 size class"、"分多少个"——这些都是**设计动机(why)**。现在我们钻**实现技巧(how)**:`malloc(n)` 里,把 `n` 算成 size class index 这一步,是**整个分配器执行频率最高的指令序列之一**(每次 malloc 都要走一次,每秒百万次)。它**绝对不能慢**——不能是二分查找、不能是循环、最好是几条位运算指令。

四套给出了四种解法,精彩程度依次递进。我们逐个拆透。

### 解法一:tcmalloc 的"按 size 索引的扁平数组 + 两次位移"

tcmalloc 的核心思路最直白:**既然 size→class 这么热,那就预填一张表,运行时直接数组下标取**。但这里有个工程难点:你要的 size 范围是 `[0, 262144]`,如果每个字节一个表项,要 256K 项——太大。tcmalloc 的解法是**双段式索引**,把表压到 ~377 项。

核心是 `ClassIndexMaybe` 这个 inline 函数([sizemap.h:136-146](../tcmalloc/tcmalloc/sizemap.h#L136)):

```cpp
// sizemap.h:136 —— 把 size 折算成 class_array_ 的下标
ABSL_ATTRIBUTE_ALWAYS_INLINE static inline bool ClassIndexMaybe(size_t s,
                                                                size_t& idx) {
  if (ABSL_PREDICT_TRUE(s <= kLargeSize)) {     // s <= 1024,小对象
    idx = (s + 7) >> 3;                          // 按 8 字节对齐索引,0..128
    return true;
  } else if (s <= kMaxSize) {                    // 1024 < s <= 262144,大对象
    idx = ((s + 127) >> 7) + 120;                // 按 128 字节对齐索引,129..376
    return true;
  }
  return false;                                  // 超出最大 class,走页堆
}
```

这两行位移是 tcmalloc 整个 size→class 映射的**全部算术**。逐位拆:

- **小对象段**(`s <= 1024`):`idx = (s + 7) >> 3`。`(s+7)` 是向上取整到 8 的倍数(经典技巧,比 `(s + 7) / 8 * 8` 少一次乘除),`>> 3` 是除以 8。所以 size∈[0,1024] 被映射到 idx∈[0,128],共 129 项。**8 字节的粒度**正对应 tcmalloc 最小对齐 `kAlignment = 8`([common.h:212](../tcmalloc/tcmalloc/common.h#L212))。
- **大对象段**(`1024 < s <= 262144`):`idx = ((s + 127) >> 7) + 120`。`(s+127)>>7` 是向上取整到 128 的倍数,`+ 120` 是把大对象段接在小对象段后面(小对象段占了 0..128,但 1024 对应的 idx=128,所以大对象从 129 起,代码里写 `+ 120` 是因为 `(1025+127)>>7 = 9`,9+120=129,刚好接上)。**128 字节的粒度**对应大对象的粗对齐。整个大对象段是 idx∈[129,376],共 248 项。

合起来,`class_array_` 总共 `kClassArraySize = ((262144 + 127 + (120<<7)) >> 7) + 1 = 377` 项([sizemap.h:84](../tcmalloc/tcmalloc/sizemap.h#L84))。这张表在 `SizeMap::Init` 时被预填([sizemap.cc:247-259](../tcmalloc/tcmalloc/sizemap.cc#L247)):

```cpp
// sizemap.cc:247 —— 预填 class_array_:把每个 size 算出它属于哪个 class
int next_size = 0;
for (int c = 1; c < kNumClasses; c++) {
  const int max_size_in_class = class_to_size_[c];     // 这个 class 覆盖的最大 size
  for (int s = next_size; s <= max_size_in_class;
       s += static_cast<size_t>(kAlignment)) {          // 每 8 字节一格
    class_array_[ClassIndex(s)] = c;                    // 这一格指向 class c
  }
  next_size = max_size_in_class + static_cast<size_t>(kAlignment);
  ...
}
```

运行时,`GetSizeClass`([sizemap.h:210](../tcmalloc/tcmalloc/sizemap.h#L210))先调 `ClassIndexMaybe(size, idx)` 算出 `idx`(两次位移之一),再 `class_array_[idx]` 取 class。**整个映射 = 1 次比较 + 1 次位移 + 1 次数组取**,3~4 条指令搞定。

> **反面对比**:如果 tcmalloc 不用扁平数组、改用"在 46 个 class 里二分查找"会怎样?二分是 `O(log 46) ≈ 6` 次比较,每次比较还可能 cache miss(因为 class 表本身可能不在 L1)。在每秒百万次 malloc 的负载下,这一条路径就能多花几纳秒×百万 = 数秒的 CPU——**fast path 容不下任何分支预测失败的循环**。tcmalloc 用 377 字节的 `class_array_`(一个 cache line 都不到)换掉了二分,是典型的"用空间换 fast path 的常数"。

> **钉死这件事**:tcmalloc 的 size→class 是**"位运算算下标 + 扁平数组取值"**——下标用两次位移算(小对象 `>>3`、大对象 `>>7+120`),取值是一次 `class_array_[idx]`。这是"查表派"的极致。

### 解法二:jemalloc 的"纯位运算,大对象根本不查表"

jemalloc 走得更远:**小对象查表(预填),大对象直接位运算算,表都不存**。我们看大对象这段——`sz_size2index_compute_inline`([sz.h:165-198](../jemalloc/include/jemalloc/internal/sz.h#L165)),这是 jemalloc 最硬核的位运算:

```c
// sz.h:165 —— 纯位运算把 size 算成 class index(大对象路径)
JEMALLOC_ALWAYS_INLINE szind_t
sz_size2index_compute_inline(size_t size) {
  if (unlikely(size > SC_LARGE_MAXCLASS)) {
    return SC_NSIZES;                                  // 超大,非法
  }
  if (size == 0) return 0;
  // ... tiny 段省略 ...
  {
    szind_t x = lg_floor((size << 1) - 1);             // L181: size 的"向上取整 log2"
    szind_t shift = (x < SC_LG_NGROUP + LG_QUANTUM)
        ? 0 : x - (SC_LG_NGROUP + LG_QUANTUM);
    szind_t grp = shift << SC_LG_NGROUP;               // L185: group 基址

    szind_t lg_delta = (x < SC_LG_NGROUP + LG_QUANTUM + 1)
        ? LG_QUANTUM : x - SC_LG_NGROUP - 1;

    size_t delta_inverse_mask = ZU(-1) << lg_delta;    // L191: 掩码,清掉 delta 以下的位
    szind_t mod = ((((size - 1) & delta_inverse_mask) >> lg_delta))
        & ((ZU(1) << SC_LG_NGROUP) - 1);               // L192: 组内偏移

    szind_t index = SC_NTINY + grp + mod;              // L195: 最终 index
    return index;
  }
}
```

这十几行密集到要逐位拆。核心思想是 jemalloc 的 class 编码:**class index = `SC_NTINY + grp + mod`**,其中 `grp` 决定"在 2 的幂的哪一段",`mod` 决定"在这一段的哪一格"。`SC_LG_NGROUP = 2`,所以每段 4 格(`NGROUP=4`)。

- **`lg_floor((size << 1) - 1)`**(L181):这是 jemalloc 算 size 的"向上取整 log2"的技巧。`size << 1` 把 size 翻倍再 `-1`,然后用 `lg_floor`(最高位的位置)。效果等价于"找到覆盖 size 的最小 2 的幂区间"。
- **`grp = shift << 2`**(L185):`shift` 是"在 2 倍递增的哪一级",`<< 2` 是因为每级 4 个 class。所以 `grp` 直接给出 class index 的"段基址"。
- **`mod`**(L192):组内偏移。`delta_inverse_mask = -1 << lg_delta` 是个掩码,清掉 size 低于 `lg_delta` 的位;然后 `>> lg_delta` 把剩下的位移到低位,`& 0x3`(因为 `NGROUP=4`)取出组内 0~3 的偏移。

合起来,这十几行**没有任何查表、没有任何循环**,纯位运算(位移、与、或、`lg_floor` 是单条指令 `bsr`/`clz`)。在大对象路径上,jemalloc 比 tcmalloc 还省——**连 `class_array_` 都不用**,直接算。

但小对象 jemalloc 还是查表,因为**小对象的频率太高、size 太密集,纯位运算的常数比查表还大**。所以 `sz_size2index`([sz.h:220](../jemalloc/include/jemalloc/internal/sz.h#L220))是双路:

```c
// sz.h:220 —— 双路:size<=4096 查表,否则位运算
JEMALLOC_ALWAYS_INLINE szind_t sz_size2index(size_t size) {
  if (likely(size <= SC_LOOKUP_MAXCLASS)) {            // <= 4096,查表
    return sz_size2index_lookup(size);                 // sz.h:206
  }
  return sz_size2index_compute(size);                  // 否则位运算
}
```

查表那条 `sz_size2index_lookup_impl`([sz.h:206](../jemalloc/include/jemalloc/internal/sz.h#L206))也是位运算 + 数组:

```c
// sz.h:206 —— 小对象查表:按 (size+7)>>3 索引
return sz_size2index_tab[(size + (ZU(1) << SC_LG_TINY_MIN) - 1)
    >> SC_LG_TINY_MIN];
```

`(size + 7) >> 3` 和 tcmalloc 一模一样(向上取整到 8 字节)。`sz_size2index_tab` 这张表在 `sz_boot_size2index_tab` 时预填([sz.c:92](../jemalloc/src/sz.c#L92)),大小 `(4096 >> 3) + 1 = 513` 项,每项 1 字节(`uint8_t`,所以 jemalloc 限制 `SC_NBINS <= 256`)。**整张表 513 字节,8 个 cache line**,极紧凑。

> **反面对比**:如果 jemalloc 大对象也用扁平数组(像 tcmalloc 那样覆盖到 262144),表会是 `262144/128 + ...` 几千项,每项 1 字节也要几 KB,而且大部分大对象档稀疏、表项浪费。jemalloc 用**位运算把"大对象查表"彻底消灭**——这是"算法派"相对"查表派"的优势:当数据稀疏时,算比查更省内存。

> **钉死这件事**:jemalloc 的 size→class 是**"双路:小查表(8 字节粒度,513 项)、大位运算(分组数学)"**。它比 tcmalloc 多了一个"完全不查表"的大对象路径,代价是位运算更烧脑。`sz_size2index_usize_fastpath`([sz.h:300](../jemalloc/include/jemalloc/internal/sz.h#L300))还把"算 index + 算 size"两步合一,在 fast path 上**一次调用出两个结果**——这是 jemalloc 在 fast path 上压榨常数的典型。

### 解法三:mimalloc 的"高 3 位 + 最高位,73 个 bin 纯算法"

mimalloc 走到极端:**连小对象都不查 class 表,纯位运算算 bin**。`mi_bin`([page-queue.c:60](../mimalloc/src/page-queue.c#L60)):

```c
// page-queue.c:60 —— 纯位运算算 bin,无表
static inline size_t mi_bin(size_t size) {
  size_t wsize = _mi_wsize_from_size(size);           // 字节数 → word 数(向上取整)
  if mi_likely(wsize <= 8) {                          // ≤64 字节:每 word 一个 bin
    return (wsize == 0 ? 1 : wsize);                  // bin 1~8,精确
  }
  else if mi_unlikely(wsize > MI_MEDIUM_OBJ_WSIZE_MAX) {
    return MI_BIN_HUGE;                               // 超大,走 huge 路径
  }
  else {
    wsize--;                                          // L82
    const size_t b = (MI_SIZE_BITS - 1 - mi_clz(wsize));  // L84:最高位位置
    // 高 3 位决定 bin(~12.5% 最坏内部碎片)
    const size_t bin = ((b << 2) + ((wsize >> (b - 2)) & 0x03)) - 3;  // L88
    mi_assert_internal(bin > 0 && bin < MI_BIN_HUGE);
    return bin;
  }
}
```

最妙的是 L84/L88。`b = mi_clz` 反(最高位位置)确定 wsize 落在 `[2^b, 2^(b+1))` 哪个区间;然后 `(wsize >> (b-2)) & 0x3` 取出 wsize 的**紧接着最高位之下的 2 位**,把它和 `b<<2` 拼起来——这样每个 2 倍区间被切成 **4 个 bin**,最坏内部碎片 1/8 = **12.5%**(注释里 mimalloc 自己写明了这点)。`-3` 是个修正,因为前 8 个 word 已经精确分了 bin。

mimalloc 这套设计的精妙在于:**整张 size class 表是个数学函数,不占任何字节**。73 个 bin 全由 `mi_bin` 这个 ~10 行的函数定义。这和 tcmalloc"编一张 377 项表"、jemalloc"编一张 513 项表 + 算大对象"形成鲜明对比。

但**注意**:mimalloc 在真正的 fast path 上,**用的不是 `mi_bin`,而是 `pages_free_direct[]` 这张表**([types.h:578](../mimalloc/include/mimalloc/types.h#L578)):

```c
// internal.h:515 —— fast path 直接按 wsize 索引到 page
static inline mi_page_t* _mi_heap_get_free_small_page(mi_heap_t* heap, size_t size) {
  mi_assert_internal(size <= (MI_SMALL_SIZE_MAX + MI_PADDING_SIZE));
  const size_t idx = _mi_wsize_from_size(size);       // (size+7)>>3,和别家一样
  mi_assert_internal(idx < MI_PAGES_DIRECT);
  return heap->pages_free_direct[idx];                // 直接数组下标取 page
}
```

`MI_PAGES_DIRECT = MI_SMALL_WSIZE_MAX + 1 = 129` 项([types.h:551](../mimalloc/include/mimalloc/types.h#L551),`MI_SMALL_WSIZE_MAX=128`,即 1024 字节),每项一个 page 指针。`pages_free_direct[wsize]` 直接给出"这个 size 该去哪个 page"——**连 bin 这个中间步骤都跳过了**。`mi_bin` 反而是**统计/调试路径**用的(给 stats 分桶,见 [alloc.c:89](../mimalloc/src/alloc.c#L89))。

这张表在 page 第一次被填进队列时被反向刷(每个 page 覆盖的 size 区间,都指向这个 page,[page-queue.c:200-215](../mimalloc/src/page-queue.c#L200))。所以 mimalloc 实际是"**小对象查 `pages_free_direct`(直接到 page),大对象算 `mi_bin`**",和 jemalloc"小查表 / 大位运算"是同一个思路,只是 mimalloc 连小对象的"到 page"都一步到位。

> **反面对比**:如果 mimalloc 不维护 `pages_free_direct`,每次 malloc 都要先 `mi_bin` 算 bin、再从 heap 的 pages 队列里找——多一次间接寻址。用 129 个指针(1KB)的表换掉这次间接,**fast path 又省了几纳秒**。这是"查表派"对"算法派"的反向补刀:**算法省内存,查表省指令**。

### 解法四:ptmalloc 的"三段式手工位运算"

glibc 也有自己的位运算技巧,只是分散在三段里。最值得看的是大对象那段——`largebin_index_64` 宏(在线 [malloc.c](https://github.com/glibc/glibc/blob/main/malloc/malloc.c)):

```c
// glibc malloc.c —— largebin_index_64,大对象分级(简化示意,非源码原文)
#define largebin_index_64(sz)                                                \
  (((((unsigned long) (sz)) >>  6) <=  48) ?  48 + (((unsigned long) (sz)) >>  6) : \
   ((((unsigned long) (sz)) >>  9) <=  20) ?  91 + (((unsigned long) (sz)) >>  9) : \
   ((((unsigned long) (sz)) >> 12) <=  10) ? 110 + (((unsigned long) (sz)) >> 12) : \
   ((((unsigned long) (sz)) >> 15) <=   4) ? 119 + (((unsigned long) (sz)) >> 15) : \
   ((((unsigned long) (sz)) >> 18) <=   2) ? 124 + (((unsigned long) (sz)) >> 18) : \
                                             126)
```

这是一串嵌套三元 + 位移,把 `[1024, 几MB]` 的大对象**分了 5 段**(`>>6`/`>>9`/`>>12`/`>>15`/`>>18`),每段内步长不同。逻辑上和 jemalloc 的分组数学、mimalloc 的高 3 位**是同一类技巧**(都是"用位移近似对数分级"),只是 ptmalloc 是**手工硬编码的**——每一档的边界(`48`、`20`、`10`、`4`、`2`)都是手挑的魔数,改起来要重算所有偏移。这和 jemalloc"由几个常量推导"、mimalloc"纯函数"相比,**工程上更脆、更难调优**,但胜在"零运行时开销、纯宏展开"。

小对象 ptmalloc 用更简单的除法步长:`smallbin_index(sz) = (sz >> 4)`(按 16 字节一档,`SMALLBIN_WIDTH`),fastbin 用 `fastbin_index(sz) = (sz >> 4) - 2`。这两段没多少技巧,就是位移当除法用。

> **钉死这件事**:四套 size→class 的技巧谱系:**tcmalloc = 扁平数组查表**(最简单、占点内存)、**jemalloc = 小查表 + 大位运算**(双路、最精巧)、**mimalloc = 直接到 page 的查表 + 纯算法 bin**(两套并存、各司其职)、**ptmalloc = 三段手工位运算/位移**(最朴素、最脆)。它们解决的是同一个问题"把 size 几条指令映射到 class",但工程哲学从"查表派"到"算法派"铺开了一整条光谱。

---

## 2.7 一点反直觉:为什么表都是编译期/启动期写死,不能动态调

读完上面你可能会问:**既然 size class 是为了贴合程序的 size 分布,那为什么不统计程序运行时的 size 分布、动态调整这张表?** 比如"这个程序 80% 都在 malloc(48),那我把 48 也加进 class 表"。

这是个合理的直觉,但**四套分配器都拒绝了它**——size class 表一旦定下,**整个生命周期绝不改**。原因有三:

**第一,改表要重排整个缓存结构。** size class 不只是"一个数字",它牵着每个 class 的 free list、每个 class 的 span/page 配置、每个 class 的批量大小(`num_objects_to_move`)。你加一个 class,所有这些都要重排,**等于在线重构整个分配器**——这在 fast path 上根本做不到。

**第二,改表破坏 pagemap/rtree 的反查。** `free(p)` 时,分配器要从指针反查"p 属于哪个 class"(下一章 P2-08 拆)。这个反查依赖"page → class"的映射是**静态**的。如果 class 表动态变,反查结构也要动态更新,又是一笔无法摊销的开销。

**第三,程序的 size 分布其实没那么发散。** 大量 profiling 数据表明,真实程序的 size 高度集中在几十个值上(8、16、24、32、48、64、128、256……)。固定几十个 class 已经能覆盖 99% 的申请,**动态调表的边际收益极低**。

> **所以这样设计**:size class 表是**编译期/启动期一次性定死**,运行时只读。tcmalloc 用 `static constexpr`、jemalloc 用 `sc_boot` 一次性算、mimalloc 用纯函数(等于编译期)、ptmalloc 用宏。**这是分配器"用静态换 fast path"的又一个体现**——动态灵活是 slow path 的事,fast path 要的是"几条指令、绝不改"。

> **打个比方**(只在反直觉处点一下):size class 表像一份**预先印好的零件型号目录**——工厂开工前定好,生产线上工人一查目录就知道"这是几号零件"。如果目录在生产线上随时改,工人就要每次重新学,流水线就乱了。size 分布再怎么发散,**几十个固定型号**已经够用,不值得为了那 1% 的边缘 case 把整条流水线搞成动态的。

---

## 章末小结

这一章我们拆了"一次 `malloc` 的第一步":**把用户给的任意 size 算成一个 size class**。关键收获有四:

1. **size class 是"归类换 O(1)"**:把连续的 size 轴归并成几十个离散的桶,每个桶一条 free list。代价是**内部碎片(10%~12%)**,换来的是 size→class 的 O(1) 映射、每 class 独立的可批量缓存、以及紧凑的位运算编码。
2. **几十个 class 是经验甜区**:小对象步长细(8/16 字节,因为密集、碎片敏感),大对象步长粗(2 倍递增,因为稀疏)。tcmalloc 46 个、jemalloc 几十到上百、mimalloc 73 个、ptmalloc 128 个 bin——四套都在这个量级。
3. **size→class 是最热的路径,必须几条指令算完**:tcmalloc 用"位移算下标 + 扁平数组取值"、jemalloc 用"小查表 + 大位运算"、mimalloc 用"直接到 page 的查表 + 纯算法 bin"、ptmalloc 用"三段手工位移"——四套从"查表派"到"算法派"铺开了一整条光谱。
4. **表一旦定死,绝不动态改**:改表会牵动整条缓存链和 pagemap 反查,fast path 容不下。这是"静态换 fast path"的又一体现。

### 回扣主线:这一章服务哪一面

size class 是**衔接层**:它本身既服务"局部缓存"(每个 class 一条本地 free list,无锁 fast path 要按 class 查),也服务"中心堆"(中心链表按 class 组织、批量取还)。准确说,**它是"算出 class"这第一步——没有 size class,三层快慢道的"按 class 分流"就无从谈起**。下一章讲自由链表(P1-04)时,你会看到"每条 free list 就是一个 class 的库存";讲线程缓存(P1-05)时,你会看到"tcache 按 class 组织一堆 cache_bin";讲中心链表(P1-06)时,你会看到"central freelist 也是按 class 分的"。size class 是贯穿三层快慢道的**第一块地基**。

### 五个"为什么"清单

1. **为什么不按申请大小原样分配(每个 size 一条链)?** size 高度发散,会爆出 free list 数量爆炸(上千条)、元数据爆炸、锁爆炸、批量瓦解四连崩。"每个 size 一条链"把成本转嫁到了结构上,反而更糟。
2. **为什么不用一个统一大小的块?** 内部碎片爆炸——程序里小对象是绝对主力,统一给 256 字节浪费 90%+。没人这么做。
3. **size class 这笔账(10%~12% 内部碎片)为什么划算?** 它换来了 size→class 的 O(1)、每个 class 独立可批量的 free list、紧凑的位运算编码。损失 10% 内存,换来纳秒级 fast path 和高并发低争用——工程上极划算。
4. **size→class 凭什么几条指令算完?** 四套都拒绝二分/循环。tcmalloc 用 377 字节扁平数组(下标用 `>>3`/`>>7+120` 两次位移算)、jemalloc 大对象用纯位运算(`lg_floor` + 分组数学)、mimalloc 用 `pages_free_direct[]` 直接到 page(下标 `(size+7)>>3`)。**位运算或扁平数组,二选一,绝不用循环**。
5. **为什么 size class 表不能动态调?** 改表牵动整条缓存链(free list、span/page 配置、批量大小)和 pagemap 反查,fast path 容不下。且程序 size 分布其实集中在几十个值,动态调表边际收益极低。

### 想继续深入往哪钻

- **想看真实的 size class 表**:读 [tcmalloc size_classes.cc](../tcmalloc/tcmalloc/size_classes.cc#L57-L108)(默认 47 档)、[jemalloc sc.c 的 `size_classes` 函数](../jemalloc/src/sc.c#L85)(看表怎么由几个常量算出)、[mimalloc `mi_bin`](../mimalloc/src/page-queue.c#L60)(纯算法、无表)。
- **想自己观察 size 分布**:用 jemalloc 的 `prof` 或 tcmalloc 的采样(第 18 章 P5-18 会拆),打出你程序的真实 size 直方图,验证"集中在几十个值"。
- **想调 size class**:tcmalloc 有实验开关(`TCMALLOC_EXPERIMENTAL_POW2_SIZE_CLASS`,切到 17 档的 pow2 表),jemalloc 可通过 `MALLOC_CONF` 调一些边界(但 class 表整体是固定的)。日常不用调,知道能调即可。
- **想钻碎片数学**:size class 的"步长 vs 内部碎片"是个经典优化问题,tcmalloc 源码注释里的 `waste/fixed`、`waste/sampling`、`inc` 三列就是这套精算的产物,可以顺着读。
- ptmalloc 的经典 bin 组织,看在线 [malloc.c](https://github.com/glibc/glibc/blob/main/malloc/malloc.c) 的 `largebin_index_64` 宏和 `_int_malloc` 里 fastbin/smallbin/largebin 的分发。

### 引出下一章

size class 把"任意 size"算成了"几十个固定分级",每级一块固定大小的块。但这个块到底要多大才算"对齐得当"?为什么 tcmalloc 最小对齐是 8、jemalloc 的 quantum 是 16、所有分配器都执着于 2 的幂、还都把热点对象往缓存行(64 字节)上凑?**对齐不只是"硬件要求",它直接关系到 false sharing 和 cache 命中**。下一章,我们讲对齐与缓存行,正式把"size class 为什么这么对齐"讲透。
