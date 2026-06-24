# 第九章 · 大块分配:huge/large 的单独路径

> 篇:P2 页堆:批量向系统要内存
> 主线呼应:前两章我们立起了页堆的两根支柱——**“连续页 + 长度”的两数建模**(P2-07 span/extent/segment)和**指针 → 元数据的 O(1) 反查**(P2-08 pagemap/rtree/掩码/header)。但这两章隐含一个前提:每次 `malloc` 要的大小都**落在 size class 范围内**,都能在页堆里切一个 span/extent 装下。可如果用户一开口就要 **4MB、64MB、甚至 1GB** 呢?这条 size class 链就撞墙了——最大 size class 通常只有几十 KB,center freelist 根本不会有这么大的块;就算硬凑,要把页堆里几百个空闲 span 拼起来,内部碎片会爆炸。所以四套分配器**都给大块开了一条旁路**:绕过 size class 链,直接 `mmap` 一整块(或独占一个 segment),单独管理,释放时直接 `munmap`(或归还 OS)。这一章就拆这条旁路。它**仍是中心堆那一面**——大块分配是 slow path 的极端形态,它的 KPI 不是“快”(大块本身就稀少),而是“省”:不浪费、不囤积、用完就还。本章最值钱的技巧是 ptmalloc 的 **mmap threshold 自适应**——它会观察释放的大块,动态把阈值往上抬,但封顶在 32MB 防碎片。

## 核心问题

**当用户 `malloc(4 * 1024 * 1024)` 申请 4MB 内存时,分配器为什么不走和 `malloc(48)` 一样的 size class 链?它怎么知道这是“大块”、该走哪条旁路?四套各自的“大块单独路径”在判定阈值、内存来源(mmap 直采 / segment 独占 / extent 走 arena)、释放策略上有什么差别?而 ptmalloc 那个会“自我学习”的 mmap threshold,凭什么能让频繁分配/释放大块的场景不反复抖动?**

读完本章你会明白:

1. **大块为什么必须走旁路**:走 size class 链要切几百个 span/extent,内部碎片爆炸;center freelist 根本没有这么大的块;就算有,把 4MB 塞进“每个 size class 一条链”的体系,元数据和管理开销都不可接受。**旁路的核心是“一整块、一次 mmap、单独记账、释放一次 munmap”**。
2. **四套大块路径的差异**:tcmalloc 新版用 `slow_alloc_large → do_malloc_pages → page_allocator.NewAligned`,**没有独立的 huge allocator**(HPAA 时代已废弃),大块和页堆同走 page_allocator,只是 size class 为 0;jemalloc 用 `large_malloc → arena_extent_alloc_large → pa_alloc`,走 arena 的 extent 路径,每个大块是一个独立 extent;mimalloc 有**三级阈值**(small ≤ `MI_SMALL_SIZE_MAX` ≤ large ≤ `MI_LARGE_OBJ_SIZE_MAX=16MiB` ≤ huge),huge 块独占一个 32MiB 对齐的 segment;ptmalloc 用 `DEFAULT_MMAP_THRESHOLD`(默认 128KB),超过就直接 `mmap`,释放直接 `munmap`。
3. **mmap threshold 的自适应**:ptmalloc 释放一个 mmap 大块时,如果它比当前 threshold 大且不超过封顶(64 位 32MB),就把 threshold **抬到这个块大小**,trim_threshold 翻倍——这样下次分配相近大小的大块,会走 brk 扩张而非 mmap,**避免反复 mmap/munmap 的开销**;封顶则是防碎片:不能让 threshold 无限涨到把整个堆都变成 mmap。
4. **大块释放的简单性**:小块释放要回 tcache、回 center freelist、可能合并;**大块释放就是一次 munmap**(或一次独占 segment 的回收),没有 size class、没有 free list、没有合并——它用“独占 + 直接还 OS”换掉了所有元数据开销。这也是大块路径的简洁之美。

> **如果一读觉得太难**:先只记住三件事——① 大块(远超最大 size class)绕过 size class 链,直接 mmap 一整块、单独管理、释放直接 munmap;② 四套判定阈值和路径不同(tcmalloc size class==0 走 page_allocator、jemalloc 走 arena extent、mimalloc 三级 small/large/huge、ptmalloc 默认 128KB mmap threshold);③ ptmalloc 的 mmap threshold 会自适应(释放大块时抬阈值),但有 32MB 封顶。抓住这三点,本章就通了。

---

## 9.1 一句话点破

> **大块和块根本是两种货色。块(size class 范围内)是“标准件”,分配器囤一批在缓存里秒拿秒还;大块是“非标准件”,囤不起也用不着囤——每次直接问 OS 整块要(`mmap`),用完整块还(`munmap`),不进 size class 体系、不进 center freelist、不做合并。这条旁路的设计哲学是“简单换效率”:大块本身就稀少(一个程序 99% 的 `malloc` 都是小块),为它搞复杂的分级/缓存/合并是浪费;直接 mmap/munmap 的 syscall 开销,在大块场景下被“无需管理”的简洁性摊薄了。四套分配器在这条旁路上的差异,主要在“阈值划在哪”和“大块用什么数据结构承载”——但**绕过 size class、直接对接 OS** 这条主线,四套完全一致。**

这是结论,不是理由。本章倒过来拆:先看“为什么大块不能走 size class 链”(朴素方案为什么死),再依次拆 tcmalloc / jemalloc / mimalloc / ptmalloc 的大块路径,然后聚焦 ptmalloc 那个会自适应的 mmap threshold,最后四套对照收束第 2 篇。

---

## 9.2 朴素方案为什么死:把大块塞进 size class 链

在拆四套真实路径之前,先回答最根本的问题:**大块为什么不能和小块走同一条路?** 这个“为什么”是理解一切大块设计的前提。

### 反例 A:把 4MB 当成“超大 size class”切 span

最朴素的念头:既然小块按 size class 分级,那我在 size class 表里加几个“超大 class”——比如 1MB、2MB、4MB 各一个 class,大块也走 center freelist 不就行了?

听起来统一,一算就崩:

- **内部碎片爆炸**:size class 是“凑整”的,4MB 这个 class 实际给的可能是 4MB 或更大。但真正的问题不在凑整,而在**center freelist 要攒“备货”**——如果某个超大 class 的链表里囤了一个 4MB 备货(为了下次秒拿),这 4MB 就**一直占着 RSS**,哪怕程序再也不 malloc 4MB。对小块,囤几十个 48 字节无所谓;对大块,囤一个 4MB 就是 4MB 的浪费。
- **center freelist 锁放大**:大块走 center freelist,意味着每次分配大块都要抢 center freelist 的锁。大块本身稀少,但抢锁的开销和大块大小无关——一次 4MB 的分配,锁开销可能比 mmap syscall 还高。
- **页堆管理爆炸**:page heap 切一个 4MB 的 span,要在 pagemap/rtree 里登记 1024 个页(8KB 页)。如果大块频繁分配释放,pagemap 的 Ensure/更新开销线性放大。

> **不这样会怎样**:把大块塞进 size class 链,等于让一个稀少事件(大块分配)背上和小块一样的“分级 + 缓存 + 合并”全套管理开销,**收益几乎为零**(大块又不会高频命中缓存),代价却实打实(囤货浪费、锁放大、元数据爆炸)。这条路在第一天就被毙了。

### 反例 B:所有分配都直接 mmap(包括小块)

反过来,既然大块要直接 mmap,那干脆**所有分配都直接 mmap**?第一章已经拆过这条路(syscall 灾难 + 按页浪费),这里只重申:**小块直接 mmap 是灾难,因为它和高频撞上了**。大块直接 mmap 是合理的,因为它稀少。**差别全在“频率”**——大块的低频,让 mmap 的 syscall 开销可以被接受;小块的高频,让任何 syscall 都不可接受。

这两条反例合起来,点出了大块路径的设计起点:

> **钉死这件事**:大块和块必须分流。块走 size class 链(快、有缓存、有合并);大块走 mmap 旁路(简单、无缓存、无合并)。分流的关键是**一个阈值**——低于阈值是块,高于阈值是大块。四套分配器的差异,主要就在这个阈值划在哪、以及大块用什么数据结构承载。

```mermaid
flowchart TD
    M[“malloc(n)”] --> SC{“算 size class<br/>GetSizeClass(n)”}
    SC -->|“is_small=true<br/>(n ≤ 最大 size class)”| SMALL[“块路径<br/>local cache → central → page heap 切 span”]
    SC -->|“is_small=false<br/>(n > 最大 size class)”| LARGE[“大块路径<br/>绕过 size class 链”]
    LARGE --> Q1{“用什么数据结构<br/>承载这块?”}
    Q1 -->|“tcmalloc”| TC[“Span(size class=0)<br/>page_allocator.NewAligned”]
    Q1 -->|“jemalloc”| JE[“独立 extent<br/>arena_extent_alloc_large → pa_alloc”]
    Q1 -->|“mimalloc”| MI{“n > 16MiB?”}
    MI -->|“否”| MIL[“large: NORMAL segment 内<br/>多 slice 组合的 page”]
    MI -->|“是”| MIH[“huge: 独占 MI_SEGMENT_HUGE<br/>(32MiB 对齐 segment)”]
    Q1 -->|“ptmalloc”| PT[“mmap chunk(IS_MMAPPED)<br/>sysmalloc → mmap”]
    TC --> OST[“向 OS mmap 整块”]
    JE --> OST
    MIL --> OSM[“segment 来自 arena / mmap”]
    MIH --> OST
    PT --> OST
    SMALL --> OSS[“page heap → 必要时 mmap/brk”]
    classDef small fill:#dbeafe,stroke:#2563eb
    classDef large fill:#fef3c7,stroke:#d97706
    classDef os fill:#fee2e2,stroke:#dc2626
    class SMALL small
    class LARGE,TC,JE,MI,MIL,MIH,PT,Q1 large
    class OST,OSM,OSS os
```

---

## 9.3 tcmalloc 新版:大块走 page_allocator,没有独立 huge path

先看双主角之一 tcmalloc。这里有个**必须澄清的关键事实**:很多老资料(和本书总纲的早期描述)会讲 tcmalloc 有独立的 `huge_allocator.cc`/`huge_cache.cc`/`huge_address_map.cc` 大块路径。**但在 google/tcmalloc 新版(commit `7723f74`),这套独立的 huge 机制已经被废弃**——`huge_allocator.cc` 等文件虽然还在仓库里(主要用于 HPAA 的内部测试),但**生产路径的 `tcmalloc.cc` 里零引用**。新版大块统一走 `page_allocator`,HPAA(`huge_page_aware_allocator.cc`)是 page_allocator 的内部组件,不再是“大块单独路径”。

> **为什么不沿用独立 huge allocator**:旧版的 huge allocator 把“超大块”当成完全独立的一类(有自己的地址映射 `huge_address_map`、自己的缓存 `huge_cache`)。但 HPAA 时代,tcmalloc 把内存管理统一到“页 + 大页”的粒度——大块就是一个“超长 span”(size class=0),它和小块 span 走同一套 page_allocator/pagemap 机制,只是 size class 字段为 0。**统一减少了代码路径,也让大块能享受 HPAA 的大页优化**(2MB huge page 打包)。这是新版相对旧版的简化。

### 判定:fast_alloc 里 `!is_small`

大块判定的入口在 [tcmalloc.cc:1266-1278](../tcmalloc/tcmalloc/tcmalloc.cc#L1266-L1278) 的 `fast_alloc`:

```cpp
// tcmalloc.cc:1266 —— fast_alloc,大块判定的入口
template <typename Policy, typename Pointer = typename Policy::pointer_type>
static inline Pointer ABSL_ATTRIBUTE_ALWAYS_INLINE fast_alloc(size_t size,
                                                              Policy policy) {
  const auto [is_small, size_class] =
      tc_globals.sizemap().GetSizeClass(policy, size);              // :1273-1274
  if (ABSL_PREDICT_FALSE(!is_small)) {                              // :1275
    SLOW_PATH_BARRIER();
    TCMALLOC_MUSTTAIL return slow_alloc_large(size, policy);         // :1277
  }
  // ... 小块 fast path ...
}
```

关键就是 [tcmalloc.cc:1273-1274](../tcmalloc/tcmalloc/tcmalloc.cc#L1273-L1274) 的 `GetSizeClass(policy, size)`——它返回一个 `(is_small, size_class)` 对。`is_small` 是个 bool,表示这个 size 能不能塞进 size class 表。**`!is_small` 就是大块**。tcmalloc 的 size class 表最大覆盖到约 256KB(具体看 `size_classes.cc`),超过就是大块。

判定为大块后,**`SLOW_PATH_BARRIER()` 强制从 fast path 退出**(它是个编译器屏障,阻止编译器把后续代码内联进 fast path,保持 fast path 精简),然后 `MUSTTAIL` 尾调用 `slow_alloc_large`。

### slow_alloc_large → do_malloc_pages → page_allocator

大块的真正实现在 [tcmalloc.cc:1249-1263](../tcmalloc/tcmalloc/tcmalloc.cc#L1249-L1263):

```cpp
// tcmalloc.cc:1249 —— slow_alloc_large
template <typename Policy>
ABSL_ATTRIBUTE_NOINLINE static typename Policy::pointer_type slow_alloc_large(
    size_t size, Policy policy) {
  size_t weight = GetThreadSampler().RecordAllocation(size);          // :1251  大块必定采样
  __sized_ptr_t res = do_malloc_pages(size, weight, policy);          // :1252
  if (ABSL_PREDICT_FALSE(res.p == nullptr)) return policy.handle_oom(size);
  // ... 调 new hook ...
  return Policy::as_pointer(res.p, res.n);
}
```

注意 [tcmalloc.cc:1251](../tcmalloc/tcmalloc/tcmalloc.cc#L1251) 的 `RecordAllocation`(不是小块的 `RecordedAllocationFast`)——**大块每次都走完整采样**(不像小块有 fast path 采样)。这是因为大块稀少,采样开销可忽略,且大块对内存占用贡献大,值得精确统计。

核心在 [tcmalloc.cc:1252](../tcmalloc/tcmalloc/tcmalloc.cc#L1252) 的 `do_malloc_pages`,它的实现就在 [tcmalloc.cc:624-653](../tcmalloc/tcmalloc/tcmalloc.cc#L624-L653):

```cpp
// tcmalloc.cc:624 —— do_malloc_pages(简化,保留关键行)
template <typename Policy>
inline sized_ptr_t do_malloc_pages(size_t size, size_t weight, Policy policy) {
  Length num_pages = std::max<Length>(BytesToLengthCeil(size), Length(1));  // :626  字节→页数(向上取整)

  MemoryTag tag = MemoryTag::kNormal;                                 // :628
  // ... 根据 policy 算 tag(cold/multi-normal)...

  Span* span = tc_globals.page_allocator().NewAligned(                // :636  ← 核心:页堆切一个 span
      num_pages, BytesToLengthCeil(policy.align()),
      {1, AccessDensityPrediction::kSparse}, tag);
  if (span == nullptr) return {nullptr, 0};

  sized_ptr_t res{span->start_address(), num_pages.in_bytes()};       // :644  返回起点 + 字节数
  // ... 采样 ...
  return res;
}
```

整个大块分配就这几步:

1. **[tcmalloc.cc:626](../tcmalloc/tcmalloc/tcmalloc.cc#L626)** `BytesToLengthCeil(size)`:把字节转成页数,向上取整。`Length` 是页数类型(P2-07 拆过)。比如 4MB / 8KB = 512 页。
2. **[tcmalloc.cc:636](../tcmalloc/tcmalloc/tcmalloc.cc#L636)** `page_allocator().NewAligned(...)`:**这是大块和小块 span 共用的入口**。page_allocator 内部(经过 HPAA)决定是从现有空闲 span 切,还是向 OS mmap 新页。`AccessDensityPrediction::kSparse` 是个提示——告诉 page_allocator “这块只装一个大对象,密度稀疏”,HPAA 会据此把它放到合适的 huge page(第 15 章拆)。
3. **[tcmalloc.cc:644](../tcmalloc/tcmalloc/tcmalloc.cc#L644)** 返回 `{span->start_address(), num_pages.in_bytes()}`——一个 `(指针, 实际字节)` 对。注意实际字节是 `num_pages.in_bytes()`,可能比用户要的 `size` 大(因为按页向上取整),这就是大块的“凑整到页”。

**关键洞察**:tcmalloc 新版的大块**就是一个 size class=0 的 span**。它和“装 100 个 48 字节小对象的 span”用同一个 `Span` 数据结构,走同一套 page_allocator,只是 pagemap 里它的 size class 字段是 0。**统一带来好处**:free 时不需要特殊路径(都查 pagemap)、能享受 HPAA 大页优化、代码路径少。代价是大块和小块在 page_allocator 内部抢资源——但 HPAA 的 filler 机制(P4-15)专门处理这个问题。

### 大块 free:InvokeHooksAndFreePages

大块释放走 [tcmalloc.cc:660-680](../tcmalloc/tcmalloc/tcmalloc.cc#L660-L680) 的 `InvokeHooksAndFreePages`:

```cpp
// tcmalloc.cc:660 —— 大块 free(简化)
template <typename Policy>
ABSL_ATTRIBUTE_NOINLINE static void InvokeHooksAndFreePages(
    void* ptr, std::optional<size_t> size, Policy policy) {
  const PageId p = PageIdContaining(ptr);                             // :662  指针→页号

  auto [span, size_class] = tc_globals.pagemap().GetDescriptorAndSizeClass(p);  // :668  ← P2-08 的反查
  // ... 双 free / corrupt 检测(span==nullptr 或 invalid_span)...
  // ... GWP-ASan / hook ...
  // 最终:把 span 还给 page_allocator(可能合并、可能 madvise 归还)
}
```

P2-08 讲过的反查在这里登场:`GetDescriptorAndSizeClass(p)` 拿到 `Span*`。注意**大块的 free 反查用的是 `GetDescriptorAndSizeClass`(一次拿 span + size class),不是小块的 `sizeclass()`(只拿 size class)**——因为大块 free 需要 span 指针才能归还给 page_allocator。拿到 span 后,归还逻辑(合并、`madvise`、必要时 munmap)在 page_allocator 内部,第 13~14 章拆。

> **钉死这件事**:tcmalloc 新版没有独立的 huge path,大块就是 size class=0 的 span,和小块 span 共用 page_allocator/pagemap。判定靠 `GetSizeClass` 返回的 `is_small`;分配走 `slow_alloc_large → do_malloc_pages → page_allocator.NewAligned`;释放走 `InvokeHooksAndFreePages`,靠 pagemap 反查到 span。这是 HPAA 时代的简化——旧版独立的 huge_allocator/huge_cache 已废弃。

---

## 9.4 jemalloc:大块走 arena extent,每个大块一个独立 extent

jemalloc 的大块路径和 tcmalloc 表面相似(都“绕过 size class,走页堆切大单位”),但**承载大块的数据结构不同**:tcmalloc 用 size class=0 的 Span,jemalloc 用**独立的 extent**(P2-07 拆过 extent = 连续页 + 长度 + 状态)。每个大块对应一个 extent,挂在该 arena 的 large list 上。

### 入口:large_malloc / large_palloc

jemalloc 大块分配的入口在 [large.c:17-22](../jemalloc/src/large.c#L17-L22):

```c
// large.c:17 —— large_malloc
void *
large_malloc(tsdn_t *tsdn, arena_t *arena, size_t usize, bool zero) {
	assert(usize == sz_s2u(usize));                                  // :19  usize 必须已是 size class 凑整后的
	return large_palloc(tsdn, arena, usize, CACHELINE, zero);         // :21  转给 large_palloc(默认 cacheline 对齐)
}
```

`large_malloc` 是个薄包装,核心在 [large.c:25-58](../jemalloc/src/large.c#L25-L58) 的 `large_palloc`:

```c
// large.c:25 —— large_palloc(简化,保留关键行)
void *
large_palloc(
    tsdn_t *tsdn, arena_t *arena, size_t usize, size_t alignment, bool zero) {
	size_t            ausize;
	edata_t          *edata;

	ausize = sz_sa2u(usize, alignment);                               // :33  按对齐凑整(可能比 usize 大)
	if (unlikely(ausize == 0 || ausize > SC_LARGE_MAXCLASS)) {        // :34  超最大 large class → 失败
		return NULL;
	}

	if (likely(!tsdn_null(tsdn))) {
		arena = arena_choose_maybe_huge(tsdn_tsd(tsdn), arena, usize);  // :39  ← 选 arena(可能用 huge arena)
	}
	if (unlikely(arena == NULL)
	    || (edata = arena_extent_alloc_large(                          // :42-44  ← 核心:切 extent
	            tsdn, arena, ausize, alignment, zero))
	        == NULL) {
		return NULL;
	}

	if (!arena_is_auto(arena)) {                                       // :49
		malloc_mutex_lock(tsdn, &arena->large_mtx);                    // :51  large list 一把锁
		edata_list_active_append(&arena->large, edata);                // :52  挂到 arena 的 large list
		malloc_mutex_unlock(tsdn, &arena->large_mtx);                  // :53
	}

	arena_decay_tick(tsdn, arena);                                     // :56  触发 decay tick(归还 OS 的节奏,P4-14)
	return edata_addr_get(edata);                                      // :57  返回 extent 的数据起点
}
```

几个关键点:

1. **[large.c:34](../jemalloc/src/large.c#L34) `SC_LARGE_MAXCLASS`**:jemalloc 的 large 有个上限(典型 4MB×sizeof(long),和 `lg_chunk` 相关)。**超过这个上限的“超大块”走另一条路**——`arena_choose_maybe_huge`(large.c:39)可能会把它路由到一个专门的 “huge arena”(独立 mmap,不进任何 arena 的 extent 体系)。这是 jemalloc 内部的二级分流(large vs huge),类似 mimalloc 的三级。本章先聚焦 large 路径。
2. **[large.c:42-44](../jemalloc/src/large.c#L42-L44) `arena_extent_alloc_large`**:这是真正切 extent 的入口,在 [arena.c:405-448](../jemalloc/src/arena.c#L405-L448):

```c
// arena.c:405 —— arena_extent_alloc_large(简化)
arena_extent_alloc_large(
    tsdn_t *tsdn, arena_t *arena, size_t usize, size_t alignment, bool zero) {
	bool    deferred_work_generated = false;
	szind_t szind = sz_size2index(usize);                              // :408  usize → size class index
	size_t  esize = usize + sz_large_pad;                              // :409  加 large pad(防 guard 越界)

	bool guarded = san_large_extent_decide_guard(                      // :411  sanitizer 决定要不要 guard
	    tsdn, arena_get_ehooks(arena), esize, alignment);

	bool zero_override = zero && (usize >= opt_calloc_madvise_threshold);  // :421
	edata_t *edata = pa_alloc(tsdn, &arena->pa_shard, esize, alignment,  // :422  ← 核心:pa (page allocator) 切 extent
	    /* slab */ false, szind, zero_override, guarded,
	    &deferred_work_generated);

	if (edata == NULL) {
		return NULL;                                                   // :426-427
	}
	// ... 统计、cache-oblivious 随机化、zeroing ...
	return edata;                                                      // :447
}
```

注意 [arena.c:422](../jemalloc/src/arena.c#L422) 的 `pa_alloc(..., /* slab */ false, ...)`——**`slab=false` 是大块的关键标志**。jemalloc 的 extent 有两种用法:① `slab=true`:切成等大小块的小对象页(走 bin/cache_bin 体系);② `slab=false`:整块作为一个大对象(走 large 路径)。`slab` 标志存在 rtree 叶子的位里(P2-08 拆过),free 时据此分流。

3. **[large.c:51-53](../jemalloc/src/large.c#L51-L53) `arena->large` list**:每个大块 extent 挂在 arena 的 `large` 链表上,受 `large_mtx` 保护。这个链表用于**遍历该 arena 的所有大块**(比如 decay purge、profiling)。**大块不走 bin**(bin 是给小对象 size class 用的),它们直接挂在 arena 层面。

### 释放:large_dalloc

jemalloc 大块释放走 [large.c:264-270](../jemalloc/src/large.c#L264-L270):

```c
// large.c:264 —— large_dalloc
void
large_dalloc(tsdn_t *tsdn, edata_t *edata) {
	arena_t *arena = arena_get_from_edata(edata);                      // :266  从 extent 反查 arena
	large_dalloc_prep_impl(tsdn, arena, edata, false);                 // :267  准备:从 large list 摘除、统计
	large_dalloc_finish_impl(tsdn, arena, edata);                      // :268  真正归还:pa_dalloc
	arena_decay_tick(tsdn, arena);                                     // :269  触发 decay tick
}
```

`large_dalloc_prep_impl`([large.c:226-243](../jemalloc/src/large.c#L226-L243))把 extent 从 `arena->large` 链表摘除,并调 `arena_extent_dalloc_large_prep` 更新统计。`large_dalloc_finish_impl`([large.c:246-252](../jemalloc/src/large.c#L246-L252))调 `pa_dalloc` 把 extent 真正还给 page allocator(可能进 extent 的 dirty/muzzy/retained 队列,可能合并,可能 `madvise` 归还 OS——P4-13/14 拆)。

> **关键差异(对照 tcmalloc)**:tcmalloc 大块 = size class=0 的 Span(和小块 span 同构);jemalloc 大块 = 独立 extent(`slab=false`)挂 arena large list(和小对象 slab extent 用 `slab` 位区分)。两者都“绕过 size class 链、走页堆切大单位”,但承载结构和挂载位置不同。**jemalloc 的 arena 化设计让大块也归属某个 arena**(大块锁是 arena 的 `large_mtx`,不是全局),这是它多 arena 分流(第 11 章)的延伸——大块分配也摊到多 arena 上,减少锁争用。

---

## 9.5 mimalloc:三级阈值,大块走 NORMAL segment,超大走独占 segment

mimalloc 作为新秀,大块路径最“分层清晰”。它有**三级阈值**:

- **small**:`size <= MI_SMALL_SIZE_MAX`(走 small page,fast path)
- **large**:`MI_SMALL_SIZE_MAX < size <= MI_LARGE_OBJ_SIZE_MAX`,在 NORMAL segment 内由多个 slice 组合成一个大 page
- **huge**:`size > MI_LARGE_OBJ_SIZE_MAX`(= `MI_SEGMENT_SIZE/2` = 64 位下 **16MiB**),独占一个 `MI_SEGMENT_HUGE` segment

这些常量在 [types.h:176-204](../mimalloc/include/mimalloc/types.h#L176-L204):

```c
// types.h:176 —— segment 大小(64 位 32MiB)
#ifndef MI_SEGMENT_SHIFT
#if MI_INTPTR_SIZE > 4
#define MI_SEGMENT_SHIFT                  ( 9 + MI_SEGMENT_SLICE_SHIFT)  // 32MiB  :178
#else
#define MI_SEGMENT_SHIFT                  ( 7 + MI_SEGMENT_SLICE_SHIFT)  // 4MiB on 32-bit  :180
#endif
#endif
#define MI_SEGMENT_SIZE                   (MI_ZU(1)<<MI_SEGMENT_SHIFT)   // :192
// ...
#define MI_LARGE_OBJ_SIZE_MAX             (MI_SEGMENT_SIZE/2)      // 16 MiB on 64-bit  :204
```

`MI_SEGMENT_SLICE_SHIFT=16`(slice=64KB,P2-08 拆过),64 位下 `MI_SEGMENT_SHIFT = 9 + 16 = 25`,即 segment = 32MiB。`MI_LARGE_OBJ_SIZE_MAX = 16MiB` = 半个 segment。

### 入口:_mi_malloc_generic → mi_find_page → mi_large_huge_page_alloc

mimalloc 的 `mi_malloc` 在 [alloc.c:207-209](../mimalloc/src/alloc.c#L207-L209) 转 `mi_heap_malloc`,再到 [alloc.c:172-197](../mimalloc/src/alloc.c#L172-L197) 的 `_mi_heap_malloc_zero_ex`:

```c
// alloc.c:172 —— _mi_heap_malloc_zero_ex(简化)
extern inline void* _mi_heap_malloc_zero_ex(mi_heap_t* heap, size_t size, bool zero,
                                            size_t huge_alignment, size_t* usable) mi_attr_noexcept {
  if mi_likely(size <= MI_SMALL_SIZE_MAX) {                            // :174  small 快路径
    return mi_heap_malloc_small_zero(heap, size, zero, usable);        // :176
  }
  // ... guarded ...
  else {
    void* const p = _mi_malloc_generic(heap, size + MI_PADDING_SIZE,   // :187  ← 大块走 generic
                                       zero, huge_alignment, usable);
    return p;
  }
}
```

**[alloc.c:174](../mimalloc/src/alloc.c#L174) `size <= MI_SMALL_SIZE_MAX` 是 small/large+huge 的分流点**。超过就进 `_mi_malloc_generic`([page.c:1021](../mimalloc/src/page.c#L1021)),它内部调 `mi_find_page`([page.c:996-1015](../mimalloc/src/page.c#L996-L1015)):

```c
// page.c:996 —— mi_find_page(简化)
static mi_page_t* mi_find_page(mi_heap_t* heap, size_t size, size_t huge_alignment) mi_attr_noexcept {
  const size_t req_size = size - MI_PADDING_SIZE;                      // :998
  if mi_unlikely(req_size > (MI_MEDIUM_OBJ_SIZE_MAX - MI_PADDING_SIZE)  // :999  超中等?
                 || huge_alignment > 0) {
    if mi_unlikely(req_size > MI_MAX_ALLOC_SIZE) {                     // :1000  溢出检查
      _mi_error_message(EOVERFLOW, “allocation request is too large (%zu bytes)\n”, req_size);
      return NULL;
    }
    else {
      return mi_large_huge_page_alloc(heap, size, huge_alignment);     // :1005  ← large/huge 走这里
    }
  }
  else {
    return mi_find_free_page(heap, size);                              // :1013  中等块:找空闲 page
  }
}
```

超 `MI_MEDIUM_OBJ_SIZE_MAX`(64 位 64KB)的都进 `mi_large_huge_page_alloc`([page.c:952-991](../mimalloc/src/page.c#L952-L991)):

```c
// page.c:952 —— mi_large_huge_page_alloc(简化)
static mi_page_t* mi_large_huge_page_alloc(mi_heap_t* heap, size_t size, size_t page_alignment) {
  size_t block_size = _mi_os_good_alloc_size(size);                    // :953  按页/对齐凑整
  bool is_huge = (block_size > MI_LARGE_OBJ_SIZE_MAX || page_alignment > 0);  // :955  ← large/huge 分流
  // ...
  mi_page_t* page = mi_page_fresh_alloc(heap, pq, block_size, page_alignment);  // :962
  if (page != NULL) {
    if (is_huge) {                                                     // :966
      mi_assert_internal(mi_page_is_huge(page));                       // :967
      mi_assert_internal(_mi_page_segment(page)->kind == MI_SEGMENT_HUGE);  // :968
      // ...
    }
    // ...
  }
  return page;
}
```

**[page.c:955](../mimalloc/src/page.c#L955) `block_size > MI_LARGE_OBJ_SIZE_MAX` 是 large/huge 的分流点**——超过 16MiB 就是 huge。huge 块走 `mi_segment_huge_page_alloc`([segment.c:1581](../mimalloc/src/segment.c#L1581)):

```c
// segment.c:1581 —— mi_segment_huge_page_alloc(简化)
static mi_page_t* mi_segment_huge_page_alloc(size_t size, size_t page_alignment,
                                             mi_arena_id_t req_arena_id, mi_segments_tld_t* tld) {
  mi_page_t* page = NULL;
  mi_segment_t* segment = mi_segment_alloc(size, page_alignment,       // :1584  ← 分配独占 segment
                                           req_arena_id, tld, &page);
  if (segment == NULL || page==NULL) return NULL;
  mi_assert_internal(segment->used==1);                                // :1586  整个 segment 只用一次
  // ...
  #if MI_HUGE_PAGE_ABANDON
  segment->thread_id = 0; // huge segments are immediately abandoned   // :1589  huge segment 立即“抛弃”
  #endif
  // ...
}
```

注意 [segment.c:1589](../mimalloc/src/segment.c#L1589) 的 **“huge segments are immediately abandoned”**——这是 mimalloc 的独特设计:huge segment 分配后**立即从当前线程的 segment 队列移除**(thread_id 置 0),不参与正常的 segment 复用。这样大块 segment 不会占着线程本地 segment 队列的位置,释放时直接 munmap(或归还 arena)。这是 mimalloc “arena-abandon” 哲学(P4-14)在大块上的体现。

### large 块:NORMAL segment 内多 slice 组合

注意 large(非 huge)块**不独占 segment**——它在 NORMAL segment(32MiB)内,由多个连续 slice(64KB)组合成一个“大 page”。一个 NORMAL segment 可以装多个 large page(只要每个 ≤ 16MiB)。`mi_segments_page_alloc`([segment.c:1549-1573](../mimalloc/src/segment.c#L1549-L1573))负责在 segment 里找连续 slice:

```c
// segment.c:1549 —— mi_segments_page_alloc(简化)
static mi_page_t* mi_segments_page_alloc(mi_heap_t* heap, mi_page_kind_t page_kind,
                                         size_t required, size_t block_size, mi_segments_tld_t* tld) {
  mi_assert_internal(required <= MI_LARGE_OBJ_SIZE_MAX && page_kind <= MI_PAGE_LARGE);  // :1551  large 上限
  size_t page_size = _mi_align_up(required, (required > MI_MEDIUM_PAGE_SIZE
                             ? MI_MEDIUM_PAGE_SIZE : MI_SEGMENT_SLICE_SIZE));  // :1554  按中等页/slice 凑整
  size_t slices_needed = page_size / MI_SEGMENT_SLICE_SIZE;            // :1555  需要几个 slice
  mi_page_t* page = mi_segments_page_find_and_allocate(slices_needed, heap->arena_id, tld);  // :1557  找连续 slice
  // ...
  return page;
}
```

> **钉死这件事**:mimalloc 的三级阈值最清晰——small(走 small page fast path)/ large(在 NORMAL segment 内多 slice 组合,上限 16MiB)/ huge(超 16MiB,独占 MI_SEGMENT_HUGE segment,立即 abandon)。判定靠 `MI_SMALL_SIZE_MAX`(small/large 分流)和 `MI_LARGE_OBJ_SIZE_MAX`(large/huge 分流)。huge segment 独占 + abandon,释放直接回收,这是 mimalloc “用独占换简洁”的哲学(和它用对齐掩码换零元数据反查是同源气质)。

---

## 9.6 ptmalloc:DEFAULT_MMAP_THRESHOLD 与 mmap/munmap 直来直往

最后看 baseline ptmalloc。它的大块路径最“朴素”,也最直白——**大块直接 mmap,释放直接 munmap,中间不进 arena**。但 ptmalloc 有个别家没有的杀手锏:**mmap threshold 自适应**(下一节技巧精解的主角)。

### 阈值:DEFAULT_MMAP_THRESHOLD

ptmalloc 的 mmap 阈值定义在 [malloc.c:945-1057](https://github.com/glibc/glibc/blob/main/malloc/malloc.c#L945):

```c
// malloc.c:945-957(在线源码)
#ifndef DEFAULT_MMAP_THRESHOLD_MIN
#define DEFAULT_MMAP_THRESHOLD_MIN (128 * 1024)        // :946  默认下限 128KB
#endif

#ifndef DEFAULT_MMAP_THRESHOLD_MAX
/* For 32-bit platforms we cannot increase the maximum too much. ... */
# if __WORDSIZE == 32
#  define DEFAULT_MMAP_THRESHOLD_MAX (512 * 1024)      // :955  32 位封顶 512KB
# else
#  define DEFAULT_MMAP_THRESHOLD_MAX (4 * 1024 * 1024 * sizeof(long))  // :957  64 位封顶 32MB
# endif
#endif

// malloc.c:1055-1057
#ifndef DEFAULT_MMAP_THRESHOLD
#define DEFAULT_MMAP_THRESHOLD DEFAULT_MMAP_THRESHOLD_MIN  // :1056  默认 = 下限 = 128KB
#endif
```

**默认 mmap threshold 是 128KB**。意思是:用户 `malloc(n)`,如果 `n`(加上 chunk header 凑整后)≥ 128KB,就走 mmap 分支,而不是 arena 的 brk/top chunk。

### sysmalloc 的 mmap 分支

大块分配的实际逻辑在 `sysmalloc`([malloc.c:2564-2575](https://github.com/glibc/glibc/blob/main/malloc/malloc.c#L2564) 是 mmap 分支):

```c
// malloc.c:2564(在线源码,简化)
if ((unsigned long)(nb) >= (unsigned long)(mp_.mmap_threshold)        // :2564  ← 超阈值?
    && (mp_.n_mmaps < mp_.n_mmaps_max))                              // 且 mmap 数没超上限
{
    mm = sysmalloc_mmap(nb, ...);                                      // mmap 一整块
    if (mm != MAP_FAILED) return mm;                                   // 成功就返回
}
// 否则走 brk/top chunk 扩张(小块路径)
```

`nb` 是用户请求 size 加上 chunk header 凑整后的字节数。`mp_.mmap_threshold` 就是当前生效的阈值(初始 128KB,会被自适应调整)。

mmap 出来的 chunk,其 `mchunk_size` 字段的 `IS_MMAPPED` 位(P2-08 拆过,[malloc.c:1035](https://github.com/glibc/glibc/blob/main/malloc/malloc.c#L1035))被置 1。**这个标志位是大块释放分流的关键**——free 时 `mem2chunk(p)` 回退到 header,读 `mchunk_size`,看到 `IS_MMAPPED`,就知道这是 mmap 块,直接 `munmap_chunk`(释放时 munmap,不进 bins、不合并)。

### 释放:munmap_chunk

大块 free 走的是 `munmap_chunk`(在 `_int_free` 里,`chunk_is_mmapped(p)` 为真时),它做两件事:① 算出 mmap 时的真实大小(从 chunk header);② 调 `munmap` 把整块还给 OS。**不进 bins、不做 consolidate、不进 arena**——大块释放比小块释放简单得多。

这就是 ptmalloc 大块路径的全貌:**简单、直接、没有缓存**。它的精妙之处不在路径本身,而在那个会自适应的 mmap threshold——下一节拆。

---

## 9.7 四套大块路径对照

把四套放一张表全局对照:

| 维度 | tcmalloc(新版) | jemalloc | mimalloc | ptmalloc(baseline) |
|------|-----------------|----------|----------|----------|
| **大块判定阈值** | size 超最大 size class(`GetSizeClass` 返回 `!is_small`) | size 超最大 small class(进 `large_malloc`);超 `SC_LARGE_MAXCLASS` 走 huge arena | 三级:`MI_SMALL_SIZE_MAX`(small/large)、`MI_LARGE_OBJ_SIZE_MAX=16MiB`(large/huge) | `mp_.mmap_threshold`(默认 128KB,自适应) |
| **大块承载结构** | Span(size class=0),和小块 span 同构 | 独立 extent(`slab=false`),挂 arena `large` list | large:NORMAL segment 内多 slice 组合的 page;huge:独占 `MI_SEGMENT_HUGE` segment | mmap chunk(`IS_MMAPPED` 位置 1),独立于 arena |
| **内存来源** | page_allocator(内部 mmap + HPAA 大页) | `pa_alloc`(extent 走 arena 页分配器) | large:segment 内 slice;huge:独占 segment(来自 arena 或 mmap) | 直接 `mmap`(匿名映射) |
| **释放路径** | `InvokeHooksAndFreePages` → pagemap 反查 span → 归还 page_allocator(可能合并、madvise) | `large_dalloc` → 从 large list 摘除 → `pa_dalloc`(可能合并、decay purge) | large:归还 page free list;huge:abandon segment(直接 munmap 或归还 arena) | `munmap_chunk` → 直接 `munmap`(无合并、无 bins) |
| **元数据开销** | Span 结构 + pagemap 登记(按页) | edata_t 结构 + rtree 登记(按 extent) | large:slice 数组;huge:segment header | 每 chunk 16 字节 header(`IS_MMAPPED` 标志) |
| **自适应阈值?** | 否(size class 表固定) | 否(`SC_LARGE_MAXCLASS` 固定,`lg_chunk` 可配) | 否(三级阈值固定) | **是**(`mp_.mmap_threshold` 动态调整,封顶 32MB) |
| **关键源码** | [tcmalloc.cc:1266](../tcmalloc/tcmalloc/tcmalloc.cc#L1266) `fast_alloc`;[tcmalloc.cc:1249](../tcmalloc/tcmalloc/tcmalloc.cc#L1249) `slow_alloc_large`;[tcmalloc.cc:624](../tcmalloc/tcmalloc/tcmalloc.cc#L624) `do_malloc_pages` | [large.c:18](../jemalloc/src/large.c#L18) `large_malloc`;[large.c:25](../jemalloc/src/large.c#L25) `large_palloc`;[arena.c:405](../jemalloc/src/arena.c#L405) `arena_extent_alloc_large`;[large.c:265](../jemalloc/src/large.c#L265) `large_dalloc` | [alloc.c:172](../mimalloc/src/alloc.c#L172) `_mi_heap_malloc_zero_ex`;[page.c:996](../mimalloc/src/page.c#L996) `mi_find_page`;[page.c:952](../mimalloc/src/page.c#L952) `mi_large_huge_page_alloc`;[segment.c:1581](../mimalloc/src/segment.c#L1581) `mi_segment_huge_page_alloc` | [malloc.c:946](https://github.com/glibc/glibc/blob/main/malloc/malloc.c#L946) `DEFAULT_MMAP_THRESHOLD_MIN`;[malloc.c:2564](https://github.com/glibc/glibc/blob/main/malloc/malloc.c#L2564) `sysmalloc` mmap 分支;[malloc.c:3379](https://github.com/glibc/glibc/blob/main/malloc/malloc.c#L3379) 自适应阈值 |

> **四套横评小结**:
> - **tcmalloc**:大块 = size class=0 的 Span,**和页堆统一**(HPAA 时代废弃了独立 huge path)。判定靠 `GetSizeClass` 的 `is_small`;释放靠 pagemap 反查。
> - **jemalloc**:大块 = 独立 extent(`slab=false`)挂 arena large list,**arena 化**(大块也归某 arena,锁是 arena 的 `large_mtx`)。超大块走 huge arena。
> - **mimalloc**:**三级阈值最清晰**,large 在 NORMAL segment 内多 slice 组合,huge 独占 segment 且立即 abandon。体现“用独占换简洁”。
> - **ptmalloc**:大块直接 mmap、释放直接 munmap,**朴素但有效**;独有 **mmap threshold 自适应**(下节拆),是四套里唯一会“学习”分配模式的。

---

## 9.8 技巧精解:ptmalloc 的 mmap threshold 自适应——会“学习”的阈值

这一章最硬的技巧,是 ptmalloc 那个会自我调整的 mmap threshold。它看似简单(几行代码),背后却是一个**精巧的工程权衡**:在大块分配的两种策略(mmap vs brk 扩张)之间,根据程序的实际行为动态选择,既避免反复 mmap/munmap 的开销,又防止碎片无限累积。这是 ptmalloc 在“朴素”中藏着的智慧。

### 动机:为什么不能定死一个 threshold

先想清楚:如果 mmap threshold 定死在 128KB(初始值),会怎样?

考虑一个真实场景:一个程序先 `malloc(200KB)`,然后 `free` 它,然后又 `malloc(200KB)`,又 `free`……循环一万次。

- **threshold 定死 128KB**:每次 200KB > 128KB,都走 mmap 分支。每次 `malloc` = 一次 mmap,每次 `free` = 一次 munmap。**一万次循环 = 两万次 syscall**(一万 mmap + 一万 munmap)。每次 mmap/munmap 是几十微秒,总共几百毫秒纯 syscall 开销。
- **threshold 抬到 256KB**:200KB < 256KB,走 brk 扩张(小块路径)。第一次 `malloc(200KB)` 把 top chunk 扩张到 200KB+;`free` 后这 200KB 回到 top chunk(不还给 OS,因为没到 trim_threshold);第二次 `malloc(200KB)` 直接从 top chunk 切,**无 syscall**。一万次循环 = 一次 brk 扩张(第一次)。开销几乎为零。

**差别巨大**。问题在于:**程序到底会反复分配多大?** 这个问题事先不知道,只能靠运行时观察。ptmalloc 的解法是:**让 threshold 自己往上长**——观察释放的大块,如果它比当前 threshold 大,就把 threshold 抬到它的大小。这样,反复分配的同一档大块,第二次起就会走 brk(因为 threshold 已经抬上去了),不再 mmap。

### 实现:释放大块时抬阈值

关键代码在 `_int_free` 里,释放 mmap chunk 时([malloc.c:3375-3385](https://github.com/glibc/glibc/blob/main/malloc/malloc.c#L3375)):

```c
// malloc.c:3375(在线源码,简化)
if (chunk_is_mmapped (p)) {                  /* release mmapped memory. */   // :3375
    /* See if the dynamic brk/mmap threshold needs adjusting.
       Dumped fake mmapped chunks do not affect the threshold. */            // :3377-3378
    if (!mp_.no_dyn_threshold                                              // // :3379  没被 mallopt 关闭
        && chunksize_nomask (p) > mp_.mmap_threshold                       // // :3380  本块比当前 threshold 大
        && chunksize_nomask (p) <= DEFAULT_MMAP_THRESHOLD_MAX)             // // :3381  且没超封顶(64 位 32MB)
    {
        mp_.mmap_threshold = chunksize (p);                                // :3383  ← 把 threshold 抬到本块大小
        mp_.trim_threshold = 2 * mp_.mmap_threshold;                       // :3384  trim_threshold 同步翻倍
        /* ... 同步调整 top_pad 等 ... */
    }
    // ... munmap_chunk(p) ...
}
```

读这段代码,三个条件 + 两个动作:

**三个条件(全满足才抬)**:

1. **[malloc.c:3379](https://github.com/glibc/glibc/blob/main/malloc/malloc.c#L3379) `!mp_.no_dyn_threshold`**:用户没用 `mallopt(M_MMAP_THRESHOLD, ...)` 手动设定。`do_set_mmap_threshold`([malloc.c:5445-5452](https://github.com/glibc/glibc/blob/main/malloc/malloc.c#L5445))里会设 `mp_.no_dyn_threshold = 1`——一旦用户手动配过,自适应就关闭(尊重用户意图)。
2. **[malloc.c:3380](https://github.com/glibc/glibc/blob/main/malloc/malloc.c#L3380) `chunksize_nomask(p) > mp_.mmap_threshold`**:本块大小严格大于当前 threshold。这保证 threshold 单调递增(只升不降),避免抖动。
3. **[malloc.c:3381](https://github.com/glibc/glibc/blob/main/malloc/malloc.c#L3381) `chunksize_nomask(p) <= DEFAULT_MMAP_THRESHOLD_MAX`**:本块大小不超过封顶(64 位 32MB)。**这是防碎片的关键**——见下文。

**两个动作**:

1. **[malloc.c:3383](https://github.com/glibc/glibc/blob/main/malloc/malloc.c#L3383) `mp_.mmap_threshold = chunksize(p)`**:把 threshold 抬到本块大小。下次分配 ≤ 这个大小的块,都走 brk 而非 mmap。
2. **[malloc.c:3384](https://github.com/glibc/glibc/blob/main/malloc/malloc.c#L3384) `mp_.trim_threshold = 2 * mp_.mmap_threshold`**:`trim_threshold` 是“brk 堆顶高于这个水位就 trim 还给 OS”的阈值。它和 mmap_threshold 联动——threshold 抬了,trim 也抬,保证 brk 堆能攒住这次的大块(不会被立刻 trim 还 OS)。

### 封顶的意义:防碎片

为什么要有 `DEFAULT_MMAP_THRESHOLD_MAX`(64 位 32MB)这个封顶?

假设没有封顶:程序 `malloc(500MB)` 一次然后 `free`,threshold 就被抬到 500MB。从此所有 ≤ 500MB 的分配都走 brk 扩张,brk 堆顶被顶到 500MB 以上——**这 500MB 哪怕程序再也不用,也难以归还 OS**(trim_threshold = 1GB,要攒到 1GB 才 trim)。这是**碎片灾难**:RSS 居高不下,且大量虚拟地址被 brk 占住。

封顶把 threshold 限制在 32MB(64 位),意思是:**大于 32MB 的块,永远走 mmap**(不管 threshold 怎么自适应)。这样,超大块(几百 MB)不会污染 brk 堆——它们 mmap 进来、用完 munmap 走,不占 brk 水位。只有“中等大块”(128KB ~ 32MB)会触发 threshold 自适应,享受 brk 复用的好处。这是一个精巧的分级:**小大块自适应(享受 brk 复用),超大块永远 mmap(防碎片)**。

```
  mmap threshold 的自适应(64 位):
  
  初始:mp_.mmap_threshold = 128KB (DEFAULT_MMAP_THRESHOLD_MIN)
        mp_.trim_threshold = 128KB (DEFAULT_TRIM_THRESHOLD)
        │
        │  程序 malloc(200KB) → 200KB > 128KB → mmap 一块
        │  程序 free(200KB)   → 释放时检查:
        │     200KB > 128KB(threshold)?  是
        │     200KB ≤ 32MB(封顶)?       是
        │     → mp_.mmap_threshold = 200KB
        │     → mp_.trim_threshold  = 400KB
        ▼
  阈值抬升后:mp_.mmap_threshold = 200KB
        │
        │  程序再 malloc(200KB) → 200KB ≤ 200KB → 走 brk 扩张(无 mmap!)
        │  程序 free(200KB)    → 回 top chunk,不 munmap
        │  下次再 malloc(200KB) → 从 top chunk 秒切(无 syscall)
        │  ... 循环 N 次,只 1 次 brk ...
        ▼
  遇到超大块:程序 malloc(500MB)
        │  500MB > 200KB(threshold) → 走 mmap
        │  程序 free(500MB)   → 释放时检查:
        │     500MB > 200KB(threshold)? 是
        │     500MB ≤ 32MB(封顶)?      否!  ← 封顶拦住
        │     → threshold 不动(保持 200KB)
        │     → munmap 释放 500MB(还给 OS,不污染 brk)
        ▼
  封顶保护:threshold 永远 ≤ 32MB,brk 堆不会被超大块顶飞
```

### 反面对比:朴素定死 vs 自适应 vs 无限自适应

把三种策略摊开对比,自适应的妙处就显出来了:

| 策略 | 反复 malloc(200KB)/free 一万次 | 一次性 malloc(500MB)/free | 评价 |
|------|--------------------------------|---------------------------|------|
| **threshold 定死 128KB** | 2 万次 mmap/munmap syscall(几百 ms 纯开销) | 1 次 mmap + 1 次 munmap(合理) | 大块循环场景灾难 |
| **自适应 + 无封顶** | 第一次后 threshold=200KB,后续无 syscall(快) | threshold 抬到 500MB,brk 堆顶飞到 500MB+,RSS 不降(碎片灾难) | 超大块污染 brk |
| **自适应 + 32MB 封顶**(ptmalloc 实际) | 第一次后 threshold=200KB,后续无 syscall(快) | 500MB > 32MB 封顶,threshold 不动,500MB 走 mmap/munmap(还给 OS) | **两者兼得** |

> **不这样会怎样(反面对比一)**:假设 ptmalloc 不做自适应,threshold 定死 128KB。一个图像处理程序反复分配/释放 1MB 的缓冲区(典型场景),每次都走 mmap/munmap。在 60fps 的渲染循环里,每帧两次 syscall × 60 = 120 次 mmap/munmap 每秒,每次几十微秒,**光 syscall 就吃掉几毫秒 CPU**,渲染卡顿。自适应把 threshold 抬到 1MB 后,后续走 brk 复用,syscall 归零——这就是自适应的现实价值。
>
> **不这样会怎样(反面对比二)**:假设 ptmalloc 自适应但无封顶。一个数据库程序偶尔 `malloc(2GB)` 做大查询缓冲,然后 `free`。threshold 被抬到 2GB,从此 brk 堆顶永久卡在 2GB+,RSS 不降。更糟的是,后续所有 ≤ 2GB 的分配都挤进 brk 堆(而不是各自 mmap),brk 堆碎片化严重。封顶在 32MB,把这个风险挡住——超大块强制 mmap,不污染 brk。
>
> **关键洞察**:ptmalloc 的 mmap threshold 自适应,本质是**让分配器“学习”程序的分配模式**——观察释放的大块,推断“这个程序会反复分配多大”,据此调整 mmap/brk 的分界线。但学习必须有边界(32MB 封顶),否则会被一次性超大块带偏。这是“自适应 + 封顶”的工程哲学,也是 ptmalloc 在朴素中藏着的智慧。

> **钉死这个技巧**:ptmalloc 的 mmap threshold 自适应 = 释放 mmap 块时,若它比当前 threshold 大且 ≤ 32MB 封顶,就把 threshold 抬到它的大小(单调递增,trim 联动翻倍)。动机是避免反复 mmap/munmap 同一档大块(让它们走 brk 复用);封顶是防超大块污染 brk 堆(碎片)。代码在 [malloc.c:3379-3384](https://github.com/glibc/glibc/blob/main/malloc/malloc.c#L3379),核心是三个条件 + 两个动作。这是四套里唯一会“学习”的大块路径——代价是几行代码,收益是真实场景下数量级的 syscall 减少。

---

## 9.9 技巧精解补论:大块释放的“独占 + 直接还”哲学

除了 mmap threshold 自适应,本章还有一个贯穿四套的技巧值得点透:**大块释放为什么这么简单?** 四套的大块释放(tcmalloc `InvokeHooksAndFreePages`、jemalloc `large_dalloc`、mimalloc huge abandon、ptmalloc `munmap_chunk`)都**没有小块释放那套复杂的回 tcache / 回 center / 合并 / 桶归位逻辑**。这是为什么?

答案在“独占”二字。**大块在它的生命周期里,独占一块连续内存**(一个 span / 一个 extent / 一个 segment / 一个 mmap chunk),这块内存不和别人共享。所以释放时:

- **不需要回 free list**:小块释放要塞回 tcache/center 的 free list(供下次复用);大块独占,没有“下次复用同一个大块”的场景(每个大块大小可能不同,凑巧一样的概率低),所以**直接还给 OS 或 page allocator,不缓存**。
- **不需要做 size class 桶归位**:小块释放要按 size class 归位到正确的 bin/tcache slot;大块没有 size class(它是旁路),自然不用归位。
- **合并只在 page allocator 层面**:tcmalloc/jemalloc 的大块释放,合并(和物理相邻的空闲 span/extent 合并)发生在 page allocator 内部,不是释放接口的职责。ptmalloc 的 mmap 块根本不合并(直接 munmap)。mimalloc 的 huge segment 直接 abandon/munmap。

> **关键洞察**:大块释放的简单性,源于“独占换简单”。小块为了快,必须复用(回 free list),这带来复杂的缓存/归位/合并逻辑;大块为了省,不复用(直接还 OS),换来极简的释放路径。**这是“快”和“省”在大块路径上的不同表现**——小块的“快”靠缓存复用,大块的“省”靠独占直还。四套都不约而同地选择了“大块不缓存”,因为这个选择在大块场景下是 Pareto 最优(既不浪费内存囤大块,又不增加管理开销)。
>
> **反面对比**:假设给大块也加缓存(像 tcache 那样囤几个大块备货)会怎样?① 内存浪费:tcache 囤一个 16 字节块无所谓,囤一个 4MB 块就是 4MB 的 RSS;② 命中率低:大块大小各异,囤的备货大概率不匹配下次请求;③ 锁开销:大块缓存要加锁,大块分配本就稀少,加锁收益为零。所以四套都明白:**大块不值得缓存,直接还最省**。

---

## 章末小结

这一章把第 2 篇(页堆)的最后一个边角拆透了:**大块分配的单独路径**。当用户 `malloc` 一块远超最大 size class 的内存,四套分配器都不约而同地绕开 size class 链,走 mmap 旁路——一整块、一次 mmap(或独占 segment)、单独记账、释放一次 munmap(或归还 page allocator)。它们的差异在判定阈值和承载结构:tcmalloc 用 size class=0 的 Span(和页堆统一,HPAA 时代废弃独立 huge path);jemalloc 用独立 extent 挂 arena large list(arena 化);mimalloc 三级阈值最清晰(small/large/huge,huge 独占 segment 且 abandon);ptmalloc 直接 mmap/munmap,但独有会自适应的 mmap threshold(封顶 32MB)。

本章服务二分法的**中心堆**那一面。大块分配是 slow path 的极端形态——它连 size class 链都不走,直接对接 OS。它的 KPI 不是“快”(大块稀少,延迟不敏感),而是“省”:不囤积、不浪费、用完就还。ptmalloc 的 mmap threshold 自适应,正是“省”的极致——通过学习程序行为,在大块循环场景下把 syscall 砍到接近零。大块释放的“独占 + 直接还”哲学,也是“省”的体现——不缓存换简单,不囤积换低占用。

> **点睛比喻**(总纲体系的“超大件整车送”,本章点一次,后面不再用):第 1 章我们点过“超大件直接让供应商整车送”——这就是大块 mmap 旁路。工人(线程)要一个 4MB 的大件,不从小货柜(tcache)拿、不从中转货架(center freelist)凑、也不让总仓库(page heap)切一堆小箱子拼——而是直接打电话给供应商(OS),整车(mmap)送来一个 4MB 的整件,用完整车退(munmap)。供应商不囤这种大件(大块不缓存),因为它太大太特殊,囤着占地方。ptmalloc 的 mmap threshold 自适应,像是**总仓库管理员观察了一阵子,发现工人老要同一规格的大件,就悄悄把“整车送”的门槛抬高一点**——让这种规格的大件改走“从中转仓库整箱出”(brk 复用),省得反复麻烦供应商。但管理员给自己定了个上限(32MB):超过这个规格的大件,无论怎样都得整车送,免得中转仓库被巨型件塞爆。这个心智,就是大块路径的角色。下一章起回到直球。

### 五个“为什么”清单

1. **为什么大块不能走 size class 链?** size class 链是为“高频小块”设计的:有缓存、有合并、按 size class 归位。大块走这条路,① 内部碎片爆炸(超大 class 囤备货浪费 MB 级内存);② center freelist 锁放大(大块分配抢锁,收益为零);③ 页堆管理爆炸(4MB 要登记 1024 个页)。大块稀少,为它搞复杂管理是浪费。

2. **tcmalloc 新版的大块为什么没有独立 huge path?** HPAA 时代,大块统一为“size class=0 的 Span”,和小块 span 共用 page_allocator/pagemap。旧版的 `huge_allocator`/`huge_cache`/`huge_address_map` 已废弃(`tcmalloc.cc` 零引用)。统一减少代码路径,让大块享受 HPAA 大页优化。判定靠 `GetSizeClass` 的 `is_small`,分配走 `slow_alloc_large → do_malloc_pages → page_allocator.NewAligned`([tcmalloc.cc:1249](../tcmalloc/tcmalloc/tcmalloc.cc#L1249)、[tcmalloc.cc:624](../tcmalloc/tcmalloc/tcmalloc.cc#L624))。

3. **mimalloc 的三级阈值(small/large/huge)凭什么划在 `MI_SMALL_SIZE_MAX` 和 16MiB?** `MI_SMALL_SIZE_MAX` 是 fast path 能直接服务的上限(走 small page);`MI_LARGE_OBJ_SIZE_MAX = MI_SEGMENT_SIZE/2 = 16MiB` 是 large/huge 分界——large 块在 NORMAL segment(32MiB)内由多 slice 组合(一个 segment 装多个 large page),huge 块超 16MiB 必须独占一个 segment(`MI_SEGMENT_HUGE`),因为一个 segment 装不下两个这么大块。huge segment 立即 abandon([segment.c:1589](../mimalloc/src/segment.c#L1589)),不占线程队列位置。

4. **ptmalloc 的 mmap threshold 为什么默认 128KB?** 这是经验值:小于 128KB 的分配占绝大多数(典型程序的 malloc size 分布是长尾,小块多),走 brk 复用收益高;大于 128KB 的走 mmap,避免单个大块污染 brk 堆。128KB 也和典型 page / huge page 大小相协调。定义在 [malloc.c:946](https://github.com/glibc/glibc/blob/main/malloc/malloc.c#L946) `DEFAULT_MMAP_THRESHOLD_MIN`。

5. **mmap threshold 自适应为什么有 32MB 封顶?** 封顶防碎片:若不封顶,一次性 `malloc(500MB)` + `free` 会把 threshold 抬到 500MB,此后所有 ≤ 500MB 的分配都挤进 brk 堆,RSS 不降、brk 碎片化。封顶在 32MB(`DEFAULT_MMAP_THRESHOLD_MAX`,64 位,[malloc.c:957](https://github.com/glibc/glibc/blob/main/malloc/malloc.c#L957)),意思是大于 32MB 的块永远 mmap(还给 OS),不污染 brk。自适应逻辑在 [malloc.c:3379-3384](https://github.com/glibc/glibc/blob/main/malloc/malloc.c#L3379):三个条件(未手动关、本块>threshold、本块≤封顶)+ 两个动作(threshold 抬到本块大小、trim 翻倍)。

### 想继续深入往哪钻

- **tcmalloc 大块**:读 [tcmalloc.cc:1266-1304](../tcmalloc/tcmalloc/tcmalloc.cc#L1266-L1304) 的 `fast_alloc` 看 `is_small` 分流;[tcmalloc.cc:624-653](../tcmalloc/tcmalloc/tcmalloc.cc#L624-L653) 的 `do_malloc_pages`;[tcmalloc.cc:660-680](../tcmalloc/tcmalloc/tcmalloc.cc#L660-L680) 的大块 free + 双 free 检测。想看 HPAA 怎么把大块塞进大页,读 `huge_page_aware_allocator.cc`(第 15 章细拆)。旧版的 `huge_allocator.cc`/`huge_cache.cc` 可作历史对照(已不在生产路径)。

- **jemalloc large**:读 [large.c:17-58](../jemalloc/src/large.c#L17-L58) 的 `large_malloc`/`large_palloc`;[arena.c:405-448](../jemalloc/src/arena.c#L405-L448) 的 `arena_extent_alloc_large`(注意 `slab=false`);[large.c:264-270](../jemalloc/src/large.c#L264-L270) 的 `large_dalloc`。超大块(超 `SC_LARGE_MAXCLASS`)走 huge arena,搜 `arena_choose_maybe_huge`。

- **mimalloc 三级**:读 [types.h:176-220](../mimalloc/include/mimalloc/types.h#L176-L220) 的 `MI_SEGMENT_SHIFT`/`MI_LARGE_OBJ_SIZE_MAX`/`MI_MEDIUM_OBJ_SIZE_MAX`;[page.c:996-1015](../mimalloc/src/page.c#L996-L1015) 的 `mi_find_page`(三级分流);[page.c:952-991](../mimalloc/src/page.c#L952-L991) 的 `mi_large_huge_page_alloc`;[segment.c:1581-1620](../mimalloc/src/segment.c#L1581-L1620) 的 `mi_segment_huge_page_alloc`(huge 独占 + abandon)。

- **ptmalloc mmap threshold**:读在线 [malloc.c:945-1057](https://github.com/glibc/glibc/blob/main/malloc/malloc.c#L945) 的阈值宏定义;[malloc.c:2564](https://github.com/glibc/glibc/blob/main/malloc/malloc.c#L2564) 的 `sysmalloc` mmap 分支;[malloc.c:3375-3385](https://github.com/glibc/glibc/blob/main/malloc/malloc.c#L3375) 的自适应阈值(本章主角);[malloc.c:5445-5452](https://github.com/glibc/glibc/blob/main/malloc/malloc.c#L5445) 的 `do_set_mmap_threshold`(mallopt 关闭自适应)。可用 `mallopt(M_MMAP_THRESHOLD, value)` 手动设阈值,或 `M_MMAP_MAX` 限 mmap 数量。

- **调参感受**:ptmalloc 的 `M_MMAP_THRESHOLD`/`M_TRIM_THRESHOLD` 可调,实测反复分配大块场景下,自适应 vs 手动定死的 syscall 次数差异(`strace -e mmap,munmap,brk` 计数)。mimalloc 的 `MIMALLOC_SEGMENT_SIZE`(改 segment 大小,会影响 large/huge 分界)。jemalloc 的 `MALLOC_CONF=lg_chunk:...`(改大块上限)。

### 引出下一篇

这一章我们拆透了大块分配的旁路,第 2 篇(页堆)到此收束。回顾这三章:P2-07 立“连续页 + 长度”的 span/extent/segment 建模,P2-08 立 pagemap/rtree/掩码/header 的指针反查,P2-09 立大块的 mmap 旁路与 ptmalloc 的 threshold 自适应。**页堆这一层,把“向 OS 批发”和“向用户零售”彻底解耦了**——绝大多数 `malloc`/`free` 在 size class 链(第 1 篇)+ 页堆(第 2 篇)里就能闭环,极少碰 OS。但这里有个一直被我们绕开的问题:**当几十个线程同时 `malloc`/`free`,它们怎么不互相打架?** 中心自由链表有锁、页堆有锁、arena 有锁——这些锁在高并发下会不会成瓶颈?ptmalloc 的 arena 锁为什么被嫌?jemalloc 的多 arena、tcmalloc 的 per-CPU cache 又是怎么把锁争用摊开的?第 3 篇 **多核并发:不让锁成瓶颈**(P3-10~12),我们正式进入全书性能核心——前三章立的“局部缓存 vs 中心堆”二分法,在这里迎来最激烈的战场。下一章 P3-10 先立总纲:争用从哪来、三种解法是什么。
