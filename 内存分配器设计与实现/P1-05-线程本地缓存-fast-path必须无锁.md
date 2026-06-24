# 第五章 · 线程本地缓存:fast path 必须无锁

> 篇:P1 · 共通地基(三层快慢道与 size class)
> 主线呼应:上一章我们把自由链表的 `next` 指针藏进释放块,做出了 O(1) 的 push/pop。但那条 free list 若是**所有线程共享一把锁**,它在多核下立刻退化成"一秒百万次抢一把锁"的灾难。这一章要做的,就是把那条 free list **复制成"每线程一份"**,让 99% 的 `malloc`/`free` 在自己线程里伸手就拿、完全无锁。这一层就是三层快慢道最上面的"本地缓存"——它在二分法里是**局部这一面的本体**。讲完它,你才真正握住了 fast path。

## 核心问题

**一条自由链表,单线程下 push/pop 都是 O(1) 的纳秒级操作,为什么一旦多个线程共用它就要加锁?加了锁之后,"一秒百万次 malloc"的服务器会怎样?为了甩掉这把锁,分配器给每个线程塞一份私有的 free list(线程本地缓存 / thread cache),但这份私有缓存怎么"无中生有"、线程退出时又怎么"善后"?**

读完本章你会明白:

1. **fast path 为什么必须无锁**:一条共享 free list 在 N 核机器上会被 N 个线程抢同一把 `mutex`,锁争用把"纳秒级 pop"拖成"微秒级等锁"。
2. **TLS 怎么做到"每线程一份"**:线程本地存储(thread-local storage,TLS)让每个线程看到一个**独立的** `thread_local_data_` 变量,读写都不需要任何锁。
3. **四套怎么落地**:tcmalloc 的 `ThreadCache`(legacy)+ `CpuCache`(新版 per-CPU)、jemalloc 的 `tcache`(`cache_bin` 数组)、mimalloc 的 thread-local `_mi_heap_default`、ptmalloc 的 `tcache`(glibc 2.26 才补上)。
4. **cache 满了/空了怎么办**:free list 不是无限大的——空了要去中心链表批量补货(fill),满了要把多余的退回去(scavenge/flush)。这一章点到,下一章 P1-06 详讲。

> **如果一读觉得太难**:先只记住三件事——① fast path 的本质就是"每个线程私有的一份小货柜,谁也不抢";② 这份货柜靠 TLS(`thread_local` / `__thread`)做到每线程独立,关键是**懒创建**(第一次 `malloc` 时才建);③ 四套里只有 ptmalloc 是"后加的"tcache,这就是它慢的根之一。

---

## 5.1 一句话点破

> **fast path 必须无锁,因为一秒百万次的 `malloc` 经不起一把 `mutex` 的等待。线程本地缓存(thread cache)给每个线程私有的一份 free list:它在线程眼里就是一份"只属于我"的空闲块库存,读写都不跟任何人竞争——锁从根上消失。代价是"每线程囤一份货柜",内存占用比"全局共享一份"更高;代价的兜底,是空了/满了时去中心链表批量取还。**

这是结论,不是理由。本章倒过来拆:先看共享 free list 为什么会被锁拖垮,再讲 TLS 怎么把"一份"变成"每线程一份",再看四套的真实代码怎么落地,最后讲满/空的批量取还怎么接住"私有缓存只进不出"的代价。

---

## 5.2 不加锁会怎样:一秒百万次抢一把锁

上一章(P1-04)的自由链表,逻辑上是这样的——每个 size class 一条单链表,push/pop 都 O(1):

```
   释放块头几个字节被复用成 next 指针(上一章的伎俩):

   ┌──────────────┐      ┌──────────────┐      ┌──────────────┐
   │ obj A        │      │ obj B        │      │ obj C        │
   │ [next = &B]  │ ───▶ │ [next = &C]  │ ───▶ │ [next = NULL]│
   └──────────────┘      └──────────────┘      └──────────────┘
   head

   malloc → pop head(O(1));free → push 到 head(O(1))
```

单线程下,pop 是 `head = head->next; return old_head`,三条指令,纳秒级。问题来了——这是一条**全局共享**的链表。如果有两个线程同时 push 或 pop,会发生什么?

- 线程 1 正在执行 `head = head->next`,刚读到 `head->next = &B`,还没写回;
- 线程 2 同时 `pop`,把 `head` 改成了 `&C`;
- 线程 1 写回 `head = &B`——`&C` 被**丢掉**了(内存泄漏),更糟的是两个线程可能拿到同一个块(双重分配)。

这是教科书级的 data race。朴素的修法只有一个:**给这条链表加一把锁**。

```c
// 朴素方案:一条全局共享的 free list + 一把 mutex
pthread_mutex_lock(&freelist_lock);
void* p = freelist_head;
freelist_head = freelist_head->next;
pthread_mutex_unlock(&freelist_lock);
```

> **不这样会怎样**(加锁的反面):一次 `pthread_mutex_lock`/`unlock` 在**无争用**时大概 20~40 纳秒(快路径主要是原子 CAS);一旦发生争用,要进内核(futex),开销立刻飙到**微秒级**,还拖慢所有排队等锁的线程。一个繁忙服务有几十上百个线程,如果它们全在一把 `freelist_lock` 上排队——这就是经典的"malloc 锁瓶颈"。第 0 章讲 ptmalloc 在高并发下露怯,根子就在这里:它的 arena 锁虽然分摊到了多个 arena 上,但**线程数 ≫ arena 数**时,大量线程仍挤在少数几把锁上。

更阴险的是,共享 free list 还会让**缓存行在核间反复 invalidate**:线程 1 在 CPU 0 改 `head`,把 `head` 所在的缓存行打成 CPU 0 独占;线程 2 在 CPU 1 改 `head`,缓存行又被搬到 CPU 1。两个核反复抢这个缓存行(false sharing 的极端形态——连数据本身都是共享的),性能塌方。

**结论**:fast path 上的任何共享、任何锁,在百万次/秒的频率下都是灾难。出路只有一条——**让 fast path 上访问的那条 free list 根本不共享**。

---

## 5.3 解法:把 free list 装进"每线程一份"的 cache

既然共享是祸根,那就**给每个线程一份自己的 free list**。这一份里堆着这个线程最近 `free` 掉的块,`malloc` 时先从自己的那份拿。线程之间互不见对方的链表,自然没有竞争,自然不需要锁。

这是"线程本地缓存"(thread cache)的核心直觉,用一张 ASCII 图最清楚:

```
                       全局视角(一个进程,多个线程)

     线程 1 (CPU 0)            线程 2 (CPU 1)            线程 3 (CPU 2)
     ┌─────────────────┐       ┌─────────────────┐       ┌─────────────────┐
     │  ThreadCache    │       │  ThreadCache    │       │  ThreadCache    │
     │  ┌───┬───┬───┐  │       │  ┌───┬───┬───┐  │       │  ┌───┬───┬───┐  │
     │  │c0 │c1 │c2 │  │       │  │c0 │c1 │c2 │  │       │  │c0 │c1 │c2 │  │
     │  │fl │fl │fl │  │       │  │fl │fl │fl │  │       │  │fl │fl │fl │  │
     │  └───┴───┴───┘  │       │  └───┴───┴───┘  │       │  └───┴───┴───┘  │
     │ (size class 各  │       │ (size class 各  │       │ (size class 各  │
     │  一条 free list)│       │  一条 free list)│       │  一条 free list)│
     └────────┬────────┘       └────────┬────────┘       └────────┬────────┘
              │  miss/flush              │                          │
              ▼                          ▼                          ▼
            ┌──────────────────────────────────────────────────────────┐
            │   中心自由链表(central free list)/ transfer cache        │
            │   多线程共享,但只在 miss/flush 时访问,平摊锁开销         │
            └──────────────────────────────────────────────────────────┘
```

关键点:

- **每线程一份**:每个线程的 `ThreadCache` 里有 `kNumClasses` 条 free list(每个 size class 一条),它们只被**这个线程**访问。
- **fast path 无锁**:`malloc` 走自己的 cache 里 pop,`free` 走自己的 cache 里 push,全程不碰任何别的线程能看到的数据,因此不需要任何同步。
- **为什么 sound**:线程私有 = 没有并发访问 = 没有 data race = 不需要锁。这不是"用了什么巧妙的无锁算法",而是"从结构上消灭了共享"。

> **钉死这件事**:线程本地缓存让 fast path 无锁,不是靠原子操作的精妙,而是靠**结构上的隔离**——把共享数据拆成每线程一份,并发根本不存在了,锁也就无从谈起。这是分配器所有"无锁 fast path"的共同根源,不管它叫 tcache、ThreadCache 还是 thread-local heap,本质都是这一招。

### 工位手边零件盒(只点一次睛)

到这里,可以点一句全书那个比喻——**这就像每个工人手边都有自己的零件盒**(线程本地缓存),自己用、自己补货、自己退回,**不跟其他工人抢**。代价是每人占一个盒子(多份库存,内存占用高),换来的红利是伸手就拿、没有任何排队。这个"借了不急着还、每人囤一份"的取舍,就是本章所有技巧围绕的轴。

---

## 5.4 落地:TLS 怎么做出"每线程一份"

理论很美,工程问题是:**怎么让"同一个变量名"在每个线程里有不同的值?** 你写 `thread_local ThreadCache* tc`,在编译器和操作系统的配合下,每个线程访问 `tc` 时拿到的都是**这个线程自己的那一份**。这就是 TLS(thread-local storage,线程本地存储)。

TLS 的实现有几种,各有取舍——这是本章最值得拆的技巧,我们留到 5.6 节"技巧精解"单独讲清。这里先看四套分配器**各自怎么用 TLS 落地 thread cache**,以及它们在 fast path 入口处到底写了什么。

### tcmalloc:fast path 就是 `AllocateFast` / `ThreadCache::Allocate`

我们已在第 0 章扫过这段。tcmalloc 新版的小块 fast path 在 [tcmalloc.cc:1184-1205](../tcmalloc/tcmalloc/tcmalloc.cc#L1184-L1205),核心三行:

```cpp
// tcmalloc.cc:1198-1202 —— fast/慢道分流
if (ABSL_PREDICT_TRUE(weight == 0) ||
    (res = tc_globals.cpu_cache().AllocateFast(size_class)) == nullptr) {  // L1198 fast path
  if (UsePerCpuCache(tc_globals)) {                                        // L1199
    res = tc_globals.cpu_cache().AllocateSlow(size_class);                 // L1200 per-CPU(新版)
  } else {
    res = ThreadCache::GetCache()->Allocate(size_class);                   // L1202 legacy(每线程)
  }
```

**第 1198 行 `AllocateFast`** 是 fast path 的字面入口——它试图从**本 CPU** 的 slab 里无锁拿一块(新版默认走 per-CPU,细节第 12 章拆)。如果没开 per-CPU(或老版本),走第 1202 行的 **`ThreadCache::GetCache()->Allocate`**——这就是经典 per-thread 方案。`GetCache()` 拿到的是当前线程的 `ThreadCache`,定义在 [thread_cache.h:262-265](../tcmalloc/tcmalloc/thread_cache.h#L262-L265):

```cpp
inline ThreadCache* ThreadCache::GetCache() {
  ThreadCache* tc = GetCacheIfPresent();        // L263 —— 读 TLS 变量
  return (ABSL_PREDICT_TRUE(tc != nullptr)) ? tc : CreateCacheIfNecessary();  // L264 懒创建
}
inline ThreadCache* ThreadCache::GetCacheIfPresent() {
  return thread_local_data_;                    // thread_cache.h:259 —— 一个 thread_local 指针
}
```

`GetCacheIfPresent()` 一共一行,就返回一个 `thread_local` 指针 `thread_local_data_`。它定义在 [thread_cache.h:168-169](../tcmalloc/tcmalloc/thread_cache.h#L168-L169):

```cpp
ABSL_CONST_INIT static thread_local ThreadCache* thread_local_data_
    ABSL_ATTRIBUTE_INITIAL_EXEC;   // 强制用 initial-exec TLS 模型(更快)
```

**这一行就是 tcmalloc"每线程一份"的钥匙**:C++ `thread_local` 关键字让每个线程访问 `thread_local_data_` 时拿到的是自己的私有拷贝(对这个指针而言)。线程 A 的 `thread_local_data_` 指向线程 A 的 `ThreadCache`,线程 B 的指向线程 B 的,互不干扰。

拿到 cache 之后,真正的 pop 操作在 [thread_cache.h:225-237](../tcmalloc/tcmalloc/thread_cache.h#L225-L237):

```cpp
inline void* ThreadCache::Allocate(size_t size_class) {
  const size_t allocated_size = tc_globals.sizemap().class_to_size(size_class);
  FreeList* list = &list_[size_class];          // 这条 free list 只属于本线程
  void* ret;
  if (ABSL_PREDICT_TRUE(list->TryPop(&ret))) {  // 无锁 pop
    size_ -= allocated_size;
    return ret;
  }
  return FetchFromTransferCache(size_class, allocated_size);  // miss,去中心链表
}
```

注意 `list_[size_class]` ——这是**这个线程自己的**那条 free list(`list_` 是 `ThreadCache` 的成员,而每个线程有自己的 `ThreadCache`)。`TryPop` 是上一章讲过的无锁单线程 push/pop(因为只有本线程访问,连原子操作都不需要,普通读改写就 sound)。这就是 fast path 无锁的字面实现。

> **关键观察**:tcmalloc 的 `Allocate` 是**默认走无锁路径**(`TryPop`),只有 miss 才 `FetchFromTransferCache` 进 slow path。而且新版默认根本不走 `ThreadCache`,而是走 per-CPU 的 `CpuCache`(`AllocateFast`/`AllocateSlow`)——这是 tcmalloc 相对 jemalloc/ptmalloc 的代差,**第 12 章详拆**。本章的 per-thread `ThreadCache` 是它"legacy 但仍存在"的路径,用来对照讲清"线程本地缓存"这个共通概念。

### jemalloc:fast path 是 `imalloc_fastpath`

jemalloc 的 `je_malloc` 入口极薄,在 [jemalloc.c:805-810](../jemalloc/src/jemalloc.c#L805-L810):

```c
je_malloc(size_t size) {
    LOG("core.malloc.entry", "size: %zu", size);
    void *ret = imalloc_fastpath(size, &malloc_default);   // L808 fast path
    LOG("core.malloc.exit", "result: %p", ret);
    return ret;
}
```

真正的 fast path 在 [jemalloc_internal_inlines_c.h:286-368](../jemalloc/include/jemalloc/internal/jemalloc_internal_inlines_c.h#L286-L368),我们摘最关键的几行:

```c
JEMALLOC_ALWAYS_INLINE void *
imalloc_fastpath(size_t size, void *(fallback_alloc)(size_t)) {
    ...
    tsd_t *tsd = tsd_get(false);                       // L292 —— 读 TLS(拿 tsd)
    if (unlikely((size > SC_LOOKUP_MAXCLASS) || tsd == NULL)) {
        return fallback_alloc(size);                   // 太大或没初始化,走 slow path
    }
    ...
    tcache_t *tcache = tsd_tcachep_get(tsd);           // L342 —— 从 tsd 取 tcache
    cache_bin_t *bin = &tcache->bins[ind];             // L344 —— 对应 size class 的那个 bin
    ...
    ret = cache_bin_alloc_easy(bin, &tcache_success);  // L356 —— 无锁 pop
    if (tcache_success) { fastpath_success_finish(...); return ret; }
    ret = cache_bin_alloc(bin, &tcache_success);       // L361 —— 再试(允许触底)
    if (tcache_success) { fastpath_success_finish(...); return ret; }
    return fallback_alloc(size);                       // L367 —— bin 空,走 slow path
}
```

三个关键点:

1. **第 292 行 `tsd_get(false)`**:拿当前线程的 tsd(thread-specific data)。`tsd` 是 jemalloc 自己的一套 TLS 抽象(下面技巧精解详讲),它**不返回锁**,返回的是这个线程私有的 `tsd_t` 结构体指针。
2. **第 342 行 `tsd_tcachep_get(tsd)`**:从 tsd 里取出**这个线程的 tcache** 指针。`tcache_t` 就是 jemalloc 版的 "ThreadCache"。
3. **第 356 行 `cache_bin_alloc_easy(bin, ...)`**:从对应 size class 的 bin 里 pop 一块。这就是 fast path 的字面动作。

`cache_bin_alloc_easy` 实际调 [cache_bin_alloc_impl](../jemalloc/include/jemalloc/internal/cache_bin.h#L381-L425),核心是无锁的栈 pop:

```c
// cache_bin.h:381-407 (简化,保留结构)
JEMALLOC_ALWAYS_INLINE void *
cache_bin_alloc_impl(cache_bin_t *bin, bool *success, bool adjust_low_water) {
    void          *ret = *bin->stack_head;             // L395 —— 读栈顶的块
    cache_bin_sz_t low_bits = (cache_bin_sz_t)(uintptr_t)bin->stack_head;
    void         **new_head = bin->stack_head + 1;     // L397 —— 栈顶上移一个槽
    if (likely(low_bits != bin->low_bits_low_water)) { // L403 —— 还有库存
        bin->stack_head = new_head;                    // L404 —— 更新栈顶
        *success = true;
        return ret;                                    // 返回块
    }
    ...
}
```

`tcache->bins[ind]` 是一个**指针数组当作栈用**的 free list(把"释放块的地址"压进数组栈,而不是 tcmalloc 那种"把 next 藏进释放块")——这是 jemalloc 与 tcmalloc 一个有趣的差异(用数组栈 vs 内嵌链表),内存局部性各有取舍,第 4 章已讲过,这里不展开。要紧的是:**整个 pop 只动 `bin->stack_head`,而这个 bin 只属于本线程的 tcache,所以无锁**。

`cache_bin_s` 的布局值得画一下(它的精妙在于用低 16 位指针省 metadata,见 [cache_bin.h:74-86](../jemalloc/include/jemalloc/internal/cache_bin.h#L74-L86)):

```
   jemalloc 的 cache_bin:把一个栈数组当作 free list
   (low addr)                                                  (high addr)
   |------stashed------|------available------|------cached-----|
   ^                   ^                     ^                 ^
   low_bound(派生)    low_bits_full         stack_head         low_bits_empty

   stack_head: 一个完整 64 位指针(快路径要 deref,所以用全宽)
   low_bits_full / low_bits_empty / low_bits_low_water: 只存低 16 位
        (一个栈永远 < 2^16 字节,只做相等比较,省 metadata)
```

### mimalloc:default heap 就是 thread-local

mimalloc 的 fast path 最干净,[alloc.c:207-208](../mimalloc/src/alloc.c#L207-L208):

```c
mi_decl_restrict void* mi_malloc(size_t size) mi_attr_noexcept {
    return mi_heap_malloc(mi_prim_get_default_heap(), size);  // L207-208
}
```

**`mi_prim_get_default_heap()` 就是"拿当前线程的默认堆"**。它的"thread-local"在 [init.c:153](../mimalloc/src/init.c#L153):

```c
mi_decl_thread mi_heap_t* _mi_heap_default = (mi_heap_t*)&_mi_heap_empty;
```

`mi_decl_thread` 就是平台无关的 `__thread` / `thread_local` 宏。每个线程的 `_mi_heap_default` 是独立的指针,指向自己的 `mi_heap_t`。`mi_heap_t` 的结构在 [types.h:555-580](../mimalloc/include/mimalloc/types.h#L555-L580),关键字段是 `pages_free_direct[MI_PAGES_DIRECT]`(按 size 直接查到带空闲块的 page)和 `pages[MI_BIN_FULL + 1]`(按 size class 组织的 page 队列)——**mimalloc 的"thread cache"不是一个单独的小数据结构,而是整个 heap 本身就是线程私有的**,free list 直接挂在 page 上。这是 mimalloc 与 tcmalloc/jemalloc 在层次划分上一个明显的不同:tcmalloc/jemalloc 把"线程缓存"和"中心堆"分得很清楚(两套数据结构),mimalloc 把它们合并成"每线程一个 heap,heap 里挂着 pages"。

> **技巧点**:mimalloc 还为不同平台准备了好几种 `_mi_heap_default` 的实现方式(详见 5.6)。在能直接用 `__thread` initial-exec 的平台(大多数 Linux/Windows)就用最简单的那个;在 macOS(因为 dyld 在初始化前会调 malloc,普通 TLS 会递归)就**挖一个 OS 没用的 TLS slot**(`MI_TLS_SLOT`,见 [prim.h:374-385](../mimalloc/include/mimalloc/prim.h#L374-L385))。这种"为绕开递归调用而钻平台空子"的工程细节,是 mimalloc 干净设计背后的硬功夫。

### ptmalloc(baseline):tcache 是后加的

最后看 baseline。glibc ptmalloc 也有一个 per-thread cache,叫 `tcache`,结构是 `tcache_perthread_struct`(定义在 [malloc.c](https://github.com/glibc/glibc/blob/main/malloc/malloc.c)),每个线程通过 TLS 指针 `tcache` 持有它。逻辑和上面三家几乎一样——`malloc` 先查 tcache 的对应 bin,有就拿;`free` 先压回 tcache。**但关键是时点**:tcache 是 **glibc 2.26(2017 年)才加上的**。在那之前的十几年里,glibc 的 `malloc`/`free` 一上来就走 arena 的 bins,要抢 arena 锁。也就是说,整整一代 Linux 程序的 `malloc`,fast path 上**没有线程本地缓存这一层**。

这件事不只是"历史",它解释了为什么 tcmalloc/jemalloc 从一开始就把 thread cache 当架构核心:它们的作者见过"没有 tcache 的 ptmalloc 在多线程下被锁拖垮"的样子,知道这一层不是可选项。tcmalloc 的 `ThreadCache` 自 2005 年左右就有,jemalloc 的 `tcache` 同样是从 v1 开始的标配——**比 glibc 早了十几年**。这就是为什么 ptmalloc 在本书里是 baseline:它的 tcache 是"补课",而另外几家是"地基"。

> **不这样会怎样**(没有 tcache):每个 `malloc`/`free` 都要抢 arena 锁。线程数一多,锁争用爆炸。ptmalloc 的补救是"动态开多个 arena"(默认上限 `8 × ncpu`),把锁争用摊开——但线程数 ≫ arena 数时仍挤在少数锁上。tcmalloc/jemalloc/mimalloc 的解法是**让 fast path 根本没有共享数据**,锁从根上消失。这是两种思路的根本差异(分摊锁 vs 消灭锁),第 10 章会专门对照。

---

## 5.5 cache 满了/空了怎么办:fill 与 scavenge

到这里我们有个新问题:thread cache 是线程私有的,但它不是无限大的。会有两种情况:

- **cache 空了**(malloc 要的 size class 在自己的 free list 里没有):得去**中心自由链表**批量拿一串回来填上(下一章 P1-06 详讲)。
- **cache 满了**(free 压进的块太多,thread cache 占的内存超过预算):得把多余的**退回**中心自由链表。

这两件事是 thread cache 与中心层之间的"批量流转"。本章只点它们的存在,具体策略下一章拆。这里看一眼 tcmalloc 怎么把"满了就退"做成自动的——`Scavenge()`(清理),在 [thread_cache.cc:181-213](../tcmalloc/tcmalloc/thread_cache.cc#L181-L213):

```cpp
void ThreadCache::Scavenge() {
  // low-water mark 是这条 free list 自上次补货以来探到的最低水位。
  // 如果水位是 L,意味着即使把 free list 缩短 L 个块,也不会触发补货。
  // 我们按 L/2 退回,既释放内存又不至于马上又来补货。
  for (int size_class = 0; size_class < kNumClasses; size_class++) {
    FreeList* list = &list_[size_class];
    const int lowmark = list->lowwatermark();
    if (lowmark > 0) {
      const int drop = (lowmark > 1) ? lowmark / 2 : 1;
      ReleaseToTransferCache(list, size_class, drop);   // 批量退回
      ...
    }
    list->clear_lowwatermark();
  }
  IncreaseCacheLimit();
}
```

`Scavenge()` 是"thread cache 占用超过 `max_size_` 时"触发的,代码在 [thread_cache.cc:215-222](../tcmalloc/tcmalloc/thread_cache.cc#L215-L222) 的 `DeallocateSlow` 里(`size_ >= max_size_` 就调)。它的精妙之处在于用 **low-water mark**(低水位)决定退多少——只退"显然用不上"的那部分,避免"刚退回去又得补货"的抖动。这是 thread cache 设计的一个普适思路:**私有缓存要会自我节制,否则就退化成"借了不还"的内存黑洞**。

jemalloc 的对应机制是 `cache_bin` 的 low-water 触发 GC,以及 arena 层的 `tcache_event`(每次分配按概率触发一次 tcache 维护);mimalloc 把"满了"的 page 挂到 `pages_full` 队列、有后台 thread 做 purge。这些都是同一类问题的不同解,我们后面章节会回扣。

---

## 5.6 技巧精解:TLS 的几种实现,与"懒创建"

这一章最值得单独拆透的技巧,不是自由链表(上一章拆过),也不是 scavenge(下一章拆),而是**"每线程一份"到底是怎么做出来的**——也就是 TLS 的几种工程实现,以及与之配套的**懒创建**(lazy creation)。

### 为什么 TLS 这件事不简单

直觉上,"每线程一份"听起来很简单——C++ 有 `thread_local` 关键字、GCC 有 `__thread`,写上去不就行了?但分配器用 TLS 有几个**别的程序不会撞上的**特殊困难:

1. **fast path 上 TLS 读取必须是纳秒级**。一次 `malloc` 里要读 TLS,如果这一读就要一次函数调用(像 `pthread_getspecific`),fast path 就废了。
2. **分配器可能在程序启动最早期、甚至 dyld/loader 之前就被调用**。比如 loader 自己要 malloc,这时 TLS 还没初始化好,常规 TLS 会递归崩溃。
3. **线程退出时要清理 cache**。`thread_local` 变量在线程退出时会析构,但分配器需要的是"在线程退出时把 cache 退回中心堆"——析构钩子要可靠触发。

这三个困难,逼出了 TLS 的几种实现,以及一个共同的补丁:**双 TLS**(快变量 + destructor key)。

### 实现一:原生 TLS(`__thread` / `thread_local`)

最朴素、最快。GCC/Clang 的 `__thread` 或 C++11 的 `thread_local`,编译器和 OS 一起,把变量放在每线程独立的 TLS 块里,读取直接编译成一条 mov 指令(或 FS 段寄存器偏移)。

```c
// 最快:原生 TLS,读一次 = 一条 mov
static __thread ThreadCache* tls_cache;
// 或 C++:
static thread_local ThreadCache* tls_cache;
```

**优点**:快,读就是几纳秒。
**缺点**:**没有析构钩子**。线程退出时,`tls_cache` 这个指针变量消失了,但它指向的 `ThreadCache`(几十 KB 的内存)就泄漏了——没有任何机制能在线程退出时自动调 `Cleanup()`。这是原生 TLS 最大的硬伤。

### 实现二:pthreads key + destructor(有钩子,但慢)

POSIX 提供了 `pthread_key_create(&key, destructor)`,给每个 key 注册一个**线程退出时自动调用的析构函数**:

```c
pthread_key_create(&tcache_key, tcache_destructor);
// 线程内:
pthread_setspecific(tcache_key, my_tcache);    // 绑定
// 线程退出时,OS 自动调 tcache_destructor(my_tcache)
```

**优点**:有线程退出钩子,能清理。
**缺点**:**慢**。`pthread_getspecific` 不是一条 mov,要查一张 key 表,fast path 用不起。

### 实现三:双 TLS(快变量 + destructor key)—— 四套真正用的招

既然方案一快但没钩子,方案二有钩子但慢,那就**两个都用**:

- 平时读 TLS 用**原生 `__thread`**(快路径,一条 mov);
- 同时用 **`pthread_key_create`** 注册一个 destructor(只是为了在线程退出时拿到清理机会)。

这就是 tcmalloc、jemalloc、mimalloc **实际采用**的方案。看真实源码。

**tcmalloc** 的 `thread_local_data_` 定义和注释把这套思路讲得最清楚,[thread_cache.h:155-176](../tcmalloc/tcmalloc/thread_cache.h#L155-L176):

```cpp
// If TLS is available, we also store a copy of the per-thread object
// in a __thread variable since __thread variables are faster to read
// than pthread_getspecific().  We still need pthread_setspecific()
// because __thread variables provide no way to run cleanup code when
// a thread is destroyed.
//
// We also give a hint to the compiler to use the "initial exec" TLS
// model.  This is faster than the default TLS model, at the cost that
// you cannot dlopen this library.
ABSL_CONST_INIT static thread_local ThreadCache* thread_local_data_
    ABSL_ATTRIBUTE_INITIAL_EXEC;

// Thread-specific key.  Initialization here is somewhat tricky
// because some Linux startup code invokes malloc() before it
// is in a good enough state to handle pthread_keycreate().
// Therefore, we use TSD keys only after tsd_inited is set to true.
// Until then, we use a slow path to get the heap object.
static bool tsd_inited_;
```

四件事说在了这几行里:① 用 `thread_local`(实为 `__thread`)做快变量;② 用 `pthread_setspecific` 注册 destructor(注释里的 "cleanup code");③ **用 initial-exec TLS 模型**(`ABSL_ATTRIBUTE_INITIAL_EXEC`)进一步加速——这是 TLS 两种模型(local-dynamic / initial-exec)里**更快**的一种,代价是不能 `dlopen`,但对一个 malloc 替换库来说无所谓(分配器本来就该静态链接);④ **懒启用**——`pthread_key_create` 不能在程序最早期调(那时 malloc 可能被 loader 调,递归),所以有个 `tsd_inited_` 标志,在那之前走 slow path(扫一遍 `thread_heaps_` 链表找本线程的 cache)。

实际设置在 [thread_cache.cc:302-308](../tcmalloc/tcmalloc/thread_cache.cc#L302-L308):

```cpp
// We call pthread_setspecific() outside the lock because it may
// call malloc() recursively.  We check for the recursive call using
// the "in_setspecific_" flag so that we can avoid calling
// pthread_setspecific() if we are already inside pthread_setspecific().
if (!heap->in_setspecific_ && tsd_inited_) {
  heap->in_setspecific_ = true;
  thread_local_data_ = heap;                 // L305 —— 快路径变量
  PerCpuState::state().RegisterThreadCache(heap);
  heap->in_setspecific_ = false;
}
```

注意 `in_setspecific_` 标志——它在防 `pthread_setspecific` 自身的递归 malloc。这种"为递归调用留逃生门"的细节,是分配器代码里反复出现的味道(分配器要分配自己的元数据,容易自咬尾巴)。

**jemalloc** 用的是同一套思路,封装成自己的 `tsd_*` 抽象,代码在 [tsd_tls.h](../jemalloc/include/jemalloc/internal/tsd_tls.h):

```c
#define JEMALLOC_TSD_TYPE_ATTR(type) __thread type JEMALLOC_TLS_MODEL
extern JEMALLOC_TSD_TYPE_ATTR(tsd_t) tsd_tls;          // L12 —— 快变量
extern pthread_key_t tsd_tsd;                           // L13 —— destructor key

JEMALLOC_ALWAYS_INLINE bool
tsd_boot0(void) {
    if (pthread_key_create(&tsd_tsd, &tsd_cleanup) != 0) {  // L19 注册 destructor
        return true;
    }
    tsd_booted = true;
    return false;
}

JEMALLOC_ALWAYS_INLINE tsd_t *
tsd_get(bool init) {
    return &tsd_tls;                                     // L48-50 —— 快路径,一条 mov
}
```

`tsd_get` 直接返回 `&tsd_tls`(一个 `__thread` 变量的地址)——fast path 上零开销。`tsd_cleanup` 是 destructor,线程退出时被调,负责把 tcache 退回 arena。jemalloc 还有 `tsd_generic.h` / `tsd_win.h` 等不同后端(为不支持 `__thread` 的平台兜底),但思路一致:**快变量 + destructor key**。

**mimalloc** 更进一步,根据平台和"是否作为 malloc override"选不同方案,在 [prim.h:330-419](../mimalloc/include/mimalloc/prim.h#L330-L419) 里有完整的策略表:

| 平台/条件 | 方案 | 为什么 |
|-----------|------|--------|
| 大多数(Linux/Windows,非 override) | `__thread _mi_heap_default`(initial-exec) | 最快,一条 mov |
| macOS(override malloc) | 直接用 OS 未占用的 TLS slot(`MI_TLS_SLOT`) | dyld 在 TLS 初始化前就调 malloc,普通 TLS 会递归 |
| OpenBSD | 在 pthread 块里挖一个偏移(`MI_TLS_PTHREAD_SLOT_OFS`) | 同上,绕开递归 |
| Android | `pthread_key`(`MI_TLS_PTHREAD`) | 平台 TLS 不可靠 |

注释 [prim.h:330-346](../mimalloc/include/mimalloc/prim.h#L330-L346) 把根因说得很直白:在 macOS 上,**loader 本身会在模块初始化前调 malloc**,如果用普通 `__thread`,第一次访问会触发 TLS 初始化,而 TLS 初始化又会调 malloc——死循环。所以 mimalloc 在 macOS 上**偷一个 OS 已分配但没用的 TLS slot**,直接读写那个槽,绕开整个 TLS 子系统。这是"为了 fast path 无锁且不递归"钻到平台内部找空子的极致。

### 反面对比:用一把全局锁保护 cache 会怎样

现在回到本章开头那个朴素方案——既然 thread cache 只是"一份 free list",为什么不用一把全局锁保护它?把上一节的对比坐实:

| 方案 | fast path 上的开销 | 线程退出 | 评 |
|------|-------------------|----------|----|
| 全局锁 + 一份 cache | `pthread_mutex_lock`/`unlock`(无争用 ~30ns,争用 µs 级) | 简单(没 TLS) | 高并发下塌方 |
| 原生 TLS(每线程一份) | 一条 mov(~1ns) | **泄漏**(没钩子) | 快但漏 |
| pthread key(每线程一份) | `pthread_getspecific`(~10ns) | destructor 清理 | 慢但干净 |
| **双 TLS**(tcmalloc/jemalloc/mimalloc 实际用的) | 一条 mov(~1ns) | destructor 清理 | **快且干净** |

双 TLS 是这张表里**唯一两个目标都拿到**的方案。它付出的代价是代码复杂(两个变量、递归防护、initial-exec 模型、平台特例),但 fast path 上的红利——纳秒级、无锁、无争用——值回所有复杂度。

### 懒创建:第一次 malloc 时才建 cache

双 TLS 解决了"快"和"清理",但还有一个问题:**什么时候创建 `ThreadCache`?** 进程启动时不可能给每个将来会出生的线程都预创建一个 cache(线程数未知、且大部分线程可能根本不调 malloc)。答案是**懒创建**——`malloc` 第一次被这个线程调用时,才去 `CreateCacheIfNecessary()`。

tcmalloc 的 `GetCache()` 把这写得最直白,[thread_cache.h:262-265](../tcmalloc/tcmalloc/thread_cache.h#L262-L265):

```cpp
inline ThreadCache* ThreadCache::GetCache() {
  ThreadCache* tc = GetCacheIfPresent();        // 读 TLS
  return (ABSL_PREDICT_TRUE(tc != nullptr)) ? tc : CreateCacheIfNecessary();
}
```

99.99% 的情况下 `tc` 非 null(已经创建过),直接返回——一行 mov。只有线程**第一次** malloc 时,`tc` 是 null,走 `CreateCacheIfNecessary()`,这一次慢一点没关系(只发生一次)。jemalloc 的 `tsd_get` 在 `init=true` 时会触发 `tsd_data_init`(懒初始化 tsd 里的字段);mimalloc 在 `mi_thread_init()`([init.c:505](../mimalloc/src/init.c))里懒建 heap。

> **钉死这件事**:thread cache 是**懒创建**的——第一次 malloc 才建,这一次的开销被分摊到该线程的整个生命周期。"懒"是分配器反复出现的工程哲学:不在启动时做不必做的事,把代价推到真正需要的那一刻。

### 为什么这套整体 sound

最后,把"为什么 sound"讲清楚——这是本书每个技巧都要回答的:

- **fast path 无锁 sound**:因为 thread cache 是线程私有的,只有本线程的 `malloc`/`free` 会访问它,**没有并发访问**。没有并发 = 没有 data race = 不需要内存屏障或原子操作。普通读改写就足够。这比"用 CAS 做无锁队列"简单得多,也更可靠。
- **destructor 清理 sound**:`pthread_key_create` 注册的 destructor 由 pthreads 库保证在线程退出时被调用(只要线程是 `pthread_exit` 或正常返回退出的),它会把 cache 指针传给清理函数,清理函数把它退回中心堆,然后释放 cache 结构体本身。
- **懒创建 sound**:第一次 malloc 时 `tc` 是 null,触发 `CreateCacheIfNecessary`,创建后写回 TLS;之后所有访问都拿到非 null 的指针。这里**唯一的并发**是"同一个线程的 malloc 递归"(比如 `CreateCacheIfNecessary` 里又 malloc),靠 `in_setspecific_` 这类重入标志兜住(见 [thread_cache.cc:302](../tcmalloc/tcmalloc/thread_cache.cc#L302))。不同线程之间没有共享状态需要同步。

---

## 5.7 四套 thread cache 对照

把四套在一张表里收束(本章的高密度信息都压在这里):

| 维度 | tcmalloc(新版) | tcmalloc(legacy `ThreadCache`) | jemalloc | mimalloc | ptmalloc |
|------|----------------|------------------------------|----------|----------|----------|
| **缓存粒度** | per-CPU(`CpuCache`) | per-thread(`ThreadCache`) | per-thread(`tcache`) | per-thread(`mi_heap_t`) | per-thread(`tcache`) |
| **fast path 入口** | [`AllocateFast`](../tcmalloc/tcmalloc/tcmalloc.cc#L1198)([cpu_cache.h:336](../tcmalloc/tcmalloc/cpu_cache.h#L336)) | [`ThreadCache::Allocate`](../tcmalloc/tcmalloc/thread_cache.h#L225) | [`imalloc_fastpath`](../jemalloc/include/jemalloc/internal/jemalloc_internal_inlines_c.h#L286) | `mi_malloc` → [`mi_prim_get_default_heap`](../mimalloc/include/mimalloc/prim.h#L348) | `__libc_malloc` → tcache 查询 |
| **TLS 实现** | per-CPU slab(rseq,第 12 章) | `thread_local` + pthread key([thread_cache.h:168](../tcmalloc/tcmalloc/thread_cache.h#L168)) | `__thread tsd_tls` + pthread key([tsd_tls.h:12-19](../jemalloc/include/jemalloc/internal/tsd_tls.h#L12-L19)) | 平台自适应:`__thread` / TLS slot / pthread key([prim.h:330-419](../mimalloc/include/mimalloc/prim.h#L330-L419)) | TLS 指针(具体随 glibc 版本) |
| **free list 形态** | per-CPU slab(每核一数组) | 内嵌链表(`next` 藏进块) | 数组栈(`stack_head` 上移) | page 内 free list + heap 的 `pages_free_direct` | 单链表(`tcache_entry`) |
| **退出清理** | per-CPU 不需要(核常驻) | pthread destructor(`DestroyThreadCache`) | `tsd_cleanup`(pthread destructor) | `_mi_prim_thread_done_auto_done` | pthread destructor |
| **引入时点** | tcmalloc 新版(近年代差) | tcmalloc 早期(2005-) | jemalloc v1 起 | mimalloc v1 起 | **glibc 2.26(2017)才补** |

几个对照点尤其值得记:

- **粒度上只有 tcmalloc 走 per-CPU**,其余四套(包括 tcmalloc 的 legacy)都是 per-thread。这是 tcmalloc 新版的代差,第 12 章详拆为什么 per-CPU 比 per-thread 更好(线程数 ≫ 核数时,per-thread 浪费内存且迁移贵;per-CPU 天然按物理核摊开)。
- **free list 形态**:tcmalloc 用内嵌链表(把 `next` 藏进释放块,第 4 章讲过),jemalloc 用数组栈(把地址压进数组)。两种各有取舍:内嵌链表零 metadata 但要 deref 一次块,数组栈缓存更紧凑但每个槽要存全指针(或低 16 位)。
- **tcache 的引入时点**:ptmalloc 的 tcache 是 2017 年才补上的,比另外三家晚了十几年。这是它当 baseline 的核心原因。

---

## 章末小结

这一章把"线程本地缓存"立起来了,它是三层快慢道最上面的那一层、也是 fast path 的本体。核心收束成几条:

1. **fast path 必须无锁**,因为一秒百万次的 malloc 经不起一把 mutex 的等待(争用时 µs 级,无争用也要 ~30ns,还会 false sharing)。
2. **线程本地缓存靠结构隔离消灭共享**:给每个线程一份私有的 free list,fast path 上根本没有别的线程能访问的数据,锁从根上消失。
3. **TLS 是这层的关键技巧**,四套分配器都用"原生 `__thread`(快)+ pthread key destructor(清理)"的双 TLS 方案,既快又有钩子;mimalloc 甚至为 macOS 等平台钻进 OS 内部找 TLS slot。
4. **懒创建**让 cache 在第一次 malloc 时才建,启动零开销。
5. **cache 满/空要会自我节制**:满了 scavenge 退回中心、空了 fill 批量补货——这把"私有缓存只进不出"的代价兜住了(下一章详讲)。

回到二分法:这一章整章服务**局部这一面**(线程私有、无锁、快)。它和上一章(P1-04 自由链表,局部这一面的数据结构基础)合在一起,把 fast path 的全部机理讲透了。下一章我们要走到**衔接处**——当 fast path miss(cache 没有),要去中心自由链表批量取还,那是"局部"和"中心"之间的桥。

### 五个"为什么"清单

1. **为什么 fast path 必须无锁?** 一秒百万次 malloc 上任何锁(哪怕无争用 30ns 的 mutex)都会被锁争用拖到 µs 级,还会因共享缓存行触发核间 invalidate。线程私有让"共享"从结构上消失,锁自然不需要。
2. **为什么 thread cache 要"每线程一份"而不是全局一份?** 全局一份必然共享、必然要锁;每线程一份就线程私有、无并发,普通读改写就 sound。
3. **原生 TLS(`thread_local`)有什么不够?** 它快(一条 mov),但**没有线程退出钩子**——线程一退出,cache 指针析构了,但它指向的几十 KB 内存就泄漏了。所以需要补 pthread key destructor。
4. **为什么不只用 pthread key?** 它有 destructor(钩子够),但 `pthread_getspecific` 要查 key 表,fast path 用不起(几十纳秒 vs 原生 TLS 的 1 纳秒)。双 TLS 兼顾两者。
5. **tcmalloc 为什么从 per-thread 升级到 per-CPU?** 线程数 ≫ 核数时,per-thread 给每个线程一份 cache 既浪费内存又(线程迁移核时)cache 失效;per-CPU 按物理核摊开,缓存命中天然更好,且靠 rseq 在被抢占时安全回退。这是第 12 章的主题。

### 想继续深入往哪钻

- **源码**:本章所有引用都带行号,可直接点开看。重点三处——① tcmalloc 的 `GetCache()` / `Allocate`([thread_cache.h:225-265](../tcmalloc/tcmalloc/thread_cache.h#L225-L265));② jemalloc 的 `imalloc_fastpath`([jemalloc_internal_inlines_c.h:286-368](../jemalloc/include/jemalloc/internal/jemalloc_internal_inlines_c.h#L286-L368));③ mimalloc 的平台 TLS 策略表([prim.h:330-419](../mimalloc/include/mimalloc/prim.h#L330-L419))。
- **per-CPU 的代差**:本章只点了 tcmalloc 新版用 per-CPU 替代 per-thread。要理解"为什么 per-CPU 比 per-thread 好"和"rseq 怎么让 per-CPU 操作在被抢占时安全",直接跳到第 12 章。在那之前,第 10、11 章会先把"锁争用的三种解法"和"jemalloc 的多 arena 分流"铺好。
- **TLS 内部机制**:想彻底搞懂 TLS 的几种模型(local-dynamic / global-dynamic / initial-exec / local-exec)和它们在 ELF 里的实现,看 Ulrich Drepper 的经典文档 *ELF Handling For Thread-Local Storage*(mimalloc 注释里提到的 [tls.pdf](https://akkadia.org/drepper/tls.pdf))。
- **ptmalloc 的 tcache**:在 [malloc.c](https://github.com/glibc/glibc/blob/main/malloc/malloc.c) 里搜 `tcache_init` 和 `tcache_perthread_struct`,看 glibc 怎么补这一层(以及它为什么比 tcmalloc/jemalloc 简单——只有固定数量的 bin、没有 transfer cache)。

### 引出下一章

现在我们的 thread cache 能在 fast path 上无锁秒拿空闲块了。但 cache 不是无限大的——它空了怎么办?退到中心去补货,补多少、怎么补、能不能让别的线程刚释放的块直接流到我手里(不必退到页堆再切)?这就是下一章 P1-06《中心自由链表与 transfer cache》的主题,我们走到二分法的**衔接处**:局部和中心之间的批量流转。
