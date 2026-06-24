# 第十一章 · jemalloc 的 arena 与 bin

> 篇:P3 · 多核并发(不让锁成瓶颈)
> 主线呼应:P3-10 把"锁争用"立成了第 3 篇的总命题,并给出三种解法的总览:多 arena、per-CPU、无锁 fast path。这一章是"解法①"的工程化深拆——专拆 jemalloc 的多 arena 方案:它怎么决定开几个 arena、线程怎么被绑到一个 arena、一个 arena 内部的锁又是怎么按 size class 再细分成一把把 `bin` 锁、extent 操作怎么另立门户。读完这一章,你会理解为什么 jemalloc 在"几千线程 + 高频小对象"的服务里,中心层的锁争用能被压到几乎不疼——以及它和 ptmalloc 同样是"多 arena",差距到底从哪几个工程细节里拉开。下一章(P3-12)再走"解法②":tcmalloc 的 per-CPU,你会看到 jemalloc 这条路的尽头在哪、tcmalloc 为什么另起一条。

## 核心问题

**P3-10 告诉我们"多 arena"是治锁争用的第一招,ptmalloc 和 jemalloc 都用。但同样是多 arena,为什么 ptmalloc 在几千线程的服务里中心锁还是被抢爆、而 jemalloc 能稳住?差距不在"arena 数量"(两者数量级都是 `~8×ncpu`),而在四个工程细节:① jemalloc 默认开 `4×ncpu` 个 arena 且一次性建好元数据,ptmalloc 是 `8×ncpu` 上限、按需懒创建;② jemalloc 给新线程绑 arena 时用 least-loaded(挑线程数最少的),ptmalloc 用 `reused_arena` 遍历找"能抢到锁的";③ jemalloc 在单个 arena 内,每个 size class 一把 `bin` 锁,甚至一个 size class 还能再分 shard——而 ptmalloc 是"一个 arena 一把 `mutex`",粒度粗一个数量级;④ jemalloc 把 extent(页)操作、large 分配、stats 各立独立锁,witness 在 debug build 里钉死锁序防死锁。这四条叠加,才是 jemalloc "多 arena 也不疼"的真相。**

读完本章你会明白:

1. **arena 数量怎么定**:jemalloc 默认 `4×ncpu`,源码里是 `ncpus × opt_narenas_ratio`(ratio 默认 4)算出来的。为什么是 4 而不是 1 或 ncpus,是"线程数 ≫ 核数时的容纳力"与"每 arena 元数据占用 + 缓存局部性"的权衡。
2. **线程怎么绑 arena**:第一次分配时走 `arena_choose` → miss 才走 `arena_choose_hard`,后者优先用没初始化的 arena(`first_null`)、其次选 `nthreads` 最少的——比 ptmalloc 的 `reused_arena`(纯遍历 trylock)更均衡。绑定后写进 tsd,之后只读。
3. **arena 内部的锁不是一把**:每个 size class 一个 `bin`、一个 `bin->lock`(bin.h:25),`bin` 还能按 `bin_shards` 再拆(默认 1 个 shard,可配多);large 走 `large_mtx`,extent 走 `ecache->mtx`,stats 走 `cache_bin_array_descriptor_ql_mtx`——四类锁互不干扰,witness 用 rank 钉死获取顺序。
4. **为什么 ptmalloc 的多 arena 仍不够**:它的 arena 是"一个 arena 一把 `mutex`"(malloc_state.mutex),分配 32 字节和分配 64 字节的线程抢同一把锁;`reused_arena` 用 `trylock` 遍历,本身就可能在 `list_lock` 上排队;且 tcache 弱、miss 率高,中心锁被打得频。
5. **这一章服务二分法的哪一面**:全部是"局部缓存"这一面的**防御纵深**——多 arena、bin 锁细分、least-loaded 绑定,都是为了让 fast path(tcache)miss 后的中心层(bin / extent)不被锁拖死。中心堆的"省"(合并、归还、大页)留给第 4 篇。

> **如果一读觉得太难**:先只记住四件事——① jemalloc 默认 `4×ncpu` 个 arena,线程绑定时挑最闲的;② 一个 arena 内**每个 size class 一把锁**(bin 锁),不是一把大锁;③ extent、large、stats 各有独立锁,witness 在 debug 防死锁;④ ptmalloc 的 arena 是"一个 arena 一把 mutex",粒度粗,这是它高并发下被 jemalloc 拉开差距的根。抓住这四点,本章就通了。

---

## 11.1 一句话点破

> **jemalloc 的多 arena,本质是"把锁按两个维度摊开":横向按 arena 摊(线程分流到不同 arena,arena 之间无共享锁),纵向按 size class 摊(单个 arena 内,每个 size class 一个 bin、一把锁,分配 32B 和 64B 的线程抢不同的锁)。这两个维度相乘,锁的总数 = arena 数 × size class 数(再 × shard 数),争用密度随之近似平方级下降。再配上 least-loaded 的线程绑定、extent/large/stats 的锁分离、witness 的锁序检查,中心层在高并发下几乎不疼。ptmalloc 同样是多 arena,但它只做了"横向摊",纵向是一把 arena mutex——这就是差距的根。**

这是结论,不是理由。本章倒过来拆:先看 arena 数量怎么定(`4×ncpu` 的来由),再看线程怎么被绑到 arena(least-loaded),再看 arena 内部的锁怎么按 size class 拆(bin + shard),最后看 extent/large/stats 的锁分离和 witness 怎么防死锁。每一步都配真实源码,并和 ptmalloc 对照。

---

## 11.2 arena 数量:`4 × ncpus` 怎么算出来的

P3-10 已经点过:jemalloc 默认开 `4 × ncpus` 个 arena。现在拆开看这个数字是怎么算的、为什么是 4。

### 源码:两个文件拼出 `4 × ncpus`

arena 数量的默认值,由两个东西决定:一是"每 CPU 配几个 arena"的**比例**,二是 CPU 核数。比例的默认值在 [jemalloc.c:187-188](../jemalloc/src/jemalloc.c#L187-L188):

```c
// jemalloc.c:187-188 —— arena 数量的两个可调参数
unsigned opt_narenas = 0;                       // 0 表示"用默认算法算"
fxp_t    opt_narenas_ratio = FXP_INIT_INT(4);   // 默认 ratio = 4(每 CPU 配 4 个 arena)
```

`opt_narenas` 是用户可以用 `MALLOC_CONF=narenas:N` 直接指定的 arena 数;`opt_narenas_ratio` 是"如果没指定 narenas,就按 `ncpus × ratio` 算"的比例。两个都是全局变量,`opt_narenas` 默认 0(表示"待算"),`opt_narenas_ratio` 默认 4。

真正算默认 arena 数的函数在 [jemalloc_init.c:445-463](../jemalloc/src/jemalloc_init.c#L445-L463):

```c
// jemalloc_init.c:445 —— 默认 arena 数量计算
static unsigned
malloc_narenas_default(void) {
    assert(ncpus > 0);
    /*
     * For SMP systems, create more than one arena per CPU by default.
     */
    if (ncpus > 1) {
        fxp_t    fxp_ncpus = FXP_INIT_INT(ncpus);
        fxp_t    goal = fxp_mul(fxp_ncpus, opt_narenas_ratio);   // L454 —— ncpus × ratio
        uint32_t int_goal = fxp_round_nearest(goal);
        if (int_goal == 0) {
            return 1;
        }
        return int_goal;
    } else {
        return 1;                                               // 单核机器,只开 1 个
    }
}
```

**第 454 行 `fxp_mul(fxp_ncpus, opt_narenas_ratio)`** 就是核心——`ncpus × opt_narenas_ratio`,默认就是 `ncpus × 4`。`fxp_t` 是 jemalloc 自己的定点小数类型(fixed-point),允许 `opt_narenas_ratio` 配成 2.5 这种非整数(用 `MALLOC_CONF=narenas_ratio:2.5`)。最终结果四舍五入取整。

这个默认值在 [jemalloc_init.c:537-539](../jemalloc/src/jemalloc_init.c#L537-L539) 被采用——只有当用户没显式配 `opt_narenas` 时(即为 0),才用 `malloc_narenas_default()`:

```c
// jemalloc_init.c:537 —— 用户没配才用默认
if (opt_narenas == 0) {
    opt_narenas = malloc_narenas_default();
}
```

> **钉死这件事**:jemalloc 默认 arena 数 = `ncpus × 4`(单核退化为 1)。源码上是 `fxp_mul(ncpus, opt_narenas_ratio)`,ratio 默认 4。用户可以用 `MALLOC_CONF=narenas:N` 直接指定,或 `narenas_ratio:X` 改比例。

### 为什么是 4,而不是 1 或 ncpus

这是本章技巧精解的主菜之一,这里先立直觉,细节留到 11.6。`4×ncpu` 这个数字,是三个力拉扯出来的:

- **太少(1× 或更少)→ 锁仍争用**。arena 数 ≤ 核数时,线程数一旦超过核数(典型服务:几百上千线程 vs 几十核),必然有多个线程挤在同一个 arena,锁密度回升。1×ncpu 在"线程数 = 2× 核数"时,平均每个 arena 2 个线程,已经不轻松。
- **太多(每线程一个,或 ncpus×的高倍数)→ 内存占用爆炸 + 缓存局部性差**。每个 arena 都要一份元数据:`arena_t` 结构本身 + 每个 size class 一个 `bin_t`(每个 bin 含一把 `malloc_mutex_t`、slab 堆、统计)+ pa_shard(decay 状态、extent 缓存)。一个 36 size class 的 arena,光 bin 数组就几十 KB;开 1000 个 arena(每线程一个),元数据直接几十 MB 起步,且每个 arena 各囤一份 cache,总占用飙升。更糟的是缓存局部性:线程 A 在 arena 5 分配的块、线程 B 在 arena 7 释放,跨 arena 流转比跨 tcache 还重。
- **4× 是经验上的甜点**。它让 arena 数 ≈ 4 × 核数,在"线程数 ≪ 4× 核数"时,每个 arena 平均 ≤ 1 个线程(几乎不争用);在"线程数 ≫ 核数"(典型服务)时,arena 数仍有限(32 核 → 128 个 arena),元数据可控。这个 4 不是数学最优,是 Facebook/FreeBSD 在 jemalloc 长期实践里拍板的经验值——它假设"线程数通常显著大于核数,但不会到几千上万"。

> **反面对比**(朴素方案 A:1×ncpu):假设一个 32 核机器,开 32 个 arena,跑 500 个线程。平均每个 arena 15~16 个线程,bin 锁被 15 个线程抢,争用密度高。开成 4×ncpu = 128 个,平均每个 arena 4 个线程,争用密度直接降到约 1/16(锁被抢的概率随线程数平方下降)。再开成 8×ncpu = 256 个(ptmalloc 的默认上限),平均每个 arena 2 个线程,争用更轻,但元数据翻倍——这就是 jemalloc 选 4 而不是 8 的理由:4× 在"争用"和"占用"之间更均衡。

> **反面对比**(朴素方案 B:每线程一个 arena):假设 1000 个线程,开 1000 个 arena。争用确实为零(每 arena 1 个线程),但每个 arena 囤一份 cache_bin(每个 size class 几个到几十个块)、一份 pa_shard 的 extent 缓存,总占用轻松上百 MB;且线程退出后,它独占的 arena 的 cache 里那些块没法被别的线程立刻复用(要等 decay purge),内存碎片化。这就是 jemalloc 不走"每线程一个 arena"的理由:arena 是按核数规模建的共享资源,不是每线程一份的私有物(私有物是 tcache)。

### 和 ptmalloc 对照:8×ncpu 上限 vs 4×ncpu 默认

ptmalloc 的 arena 上限是 `NARENAS × ncpu`,其中 `NARENAS` 是常量 8。但这不是"默认就开 8×ncpu 个",而是"上限是 8×ncpu,实际是按需懒创建"。看在线 [arena.c](https://github.com/glibc/glibc/blob/main/malloc/arena.c) 的 `arena_get2`([arena.c:817-865](https://codebrowser.dev/glibc/glibc/malloc/arena.c.html)):

```c
// arena.c:817 —— ptmalloc 的 arena 获取(简化示意,非源码原文)
static mstate arena_get2(size_t size, mstate avoid_arena) {
    mstate a = get_free_list();              // 先从 free_list 找被释放的 arena
    if (a == NULL) {
        /* 没有可复用的,看是否还能新建 */
        if (narenas_limit == 0) {
            int n = get_nprocs();
            narenas_limit = NARENAS_FROM_NCORES(n);   // = 8 * n(在 malloc.c 里定义)
        }
        if (narenas <= narenas_limit - 1) {
            /* 还没到上限,新建一个 */
            a = _int_new_arena(size);
        } else {
            /* 到上限了,复用一个 */
            a = reused_arena(avoid_arena);
        }
    }
    return a;
}
```

ptmalloc 的策略和 jemalloc 有两点关键差异:**第一,它是懒创建**——arena 不是启动时建好,而是新线程第一次 `malloc` 抢不到现有 arena 时才新建([_int_new_arena](https://codebrowser.dev/glibc/glibc/malloc/arena.c.html),arena.c:630-697)。这意味着进程启动初期,所有线程都挤在 `main_arena` 上,直到争用把 arena 数推上去。**第二,它有 free_list 复用机制**——线程退出时(`__malloc_arena_thread_freeres`,arena.c:891-915),如果它是 arena 的最后一个 attached 线程,arena 被挂回 `free_list` 等下一个线程复用,而不是销毁。这避免了"线程频繁创建销毁时 arena 反复新建"。

| 维度 | jemalloc | ptmalloc |
|------|----------|----------|
| **arena 数默认/上限** | 默认 `4×ncpu`(一次性算好) | 上限 `8×ncpu`,实际按需懒创建 |
| **创建时机** | 启动时算好 `narenas_auto`,但每个 arena 也是**懒初始化**(`arena_choose_hard` 里 `first_null` 时才 `arena_init_locked`) | 完全懒创建(`_int_new_arena` 在 arena_get2 里按需调) |
| **线程绑定策略** | least-loaded(`nthreads` 最少的 arena 优先) + first_null(没初始化的优先) | `get_free_list`(先找 free_list)→ `_int_new_arena`(没满就建)→ `reused_arena`(满了遍历 trylock) |
| **线程退出** | `arena_unbind` 减 `nthreads`,arena 不销毁(留作后续线程用) | `attached_threads--`,归零则挂 `free_list` |
| **可调** | `MALLOC_CONF=narenas:N` 或 `narenas_ratio:X` | `MALLOC_ARENA_MAX` / `glibc.malloc.arena_max` tunable |

> **钉死这件事**:ptmalloc 的 `8×ncpu` 是**上限**(且按需懒创建),jemalloc 的 `4×ncpu` 是**目标值**(算好后按需初始化)。两者数量级相当,但 jemalloc 用 least-loaded 绑定 + 元数据预算更精细,ptmalloc 用 free_list 复用更省元数据。真正的差距不在 arena 数,而在 arena 内部的锁粒度——下一节。

---

## 11.3 线程怎么绑到 arena:arena_choose 与 least-loaded

arena 数量定了,下一个问题是:**一个新线程来了,怎么决定它用哪个 arena?** 这个决定只做一次——线程第一次分配时绑定,之后写进 tsd(thread-specific data),后续分配直接读 tsd,不再重选。

### 两级路径:arena_choose(快)→ arena_choose_hard(慢)

线程拿 arena 的入口是 [arena_inlines.h:90](../jemalloc/include/jemalloc/internal/arena_inlines.h#L90) 的 `arena_choose_impl`(对外封装成 `arena_choose`,arena_inlines.h:144):

```c
// arena_inlines.h:90 —— arena_choose 的内联实现(简化示意,非源码原文)
static inline arena_t *
arena_choose_impl(tsd_t *tsd, arena_t *arena, bool internal) {
    if (arena != NULL) {
        return arena;                              // 调用方显式指定了 arena,直接用
    }
    if (unlikely(tsd_reentrancy_level_get(tsd) > 0)) {
        return arena_get(tsd_tsdn(tsd), 0, true);  // 重入(分配器内部递归分配),用 arena 0 最安全
    }
    ret = internal ? tsd_iarena_get(tsd) : tsd_arena_get(tsd);   // L102 —— 读 tsd 里的绑定
    if (unlikely(ret == NULL)) {
        ret = arena_choose_hard(tsd, internal);    // L104 —— 没绑过,走慢路径选一个
        ...
    }
    ...   // percpu_arena 模式下的动态调整(见 11.5)
    return ret;
}
```

**第 102 行 `tsd_arena_get(tsd)` 是 fast path**——绝大多数情况下,线程早就绑好了 arena,这里只是一次 tsd 读取(无锁,thread-local)。只有第一次分配(`ret == NULL`)才走 **第 104 行 `arena_choose_hard`** 这个慢路径。慢路径只跑一次,之后这个线程的 arena 绑定就固化在 tsd 里了。

### arena_choose_hard:least-loaded + first_null

`arena_choose_hard` 是真正的"选 arena"逻辑,在 [arenas_management.c:226-340](../jemalloc/src/arenas_management.c#L226-L340)。它做两件事:给"应用分配"和"内部元数据分配"各选一个 arena(`choose[0]` 和 `choose[1]`,通常选同一个)。核心选择逻辑:

```c
// arenas_management.c:239-322 —— 选 arena 的核心(简化示意,非源码原文)
if (narenas_auto > 1) {
    unsigned choose[2], first_null;
    first_null = narenas_auto;                          // L256 —— 标记"还没找到未初始化的 arena"
    malloc_mutex_lock(tsd_tsdn(tsd), &arenas_lock);     // L257 —— 全局 arenas 列表锁
    for (i = 1; i < narenas_auto; i++) {
        if (arena_get(tsd_tsdn(tsd), i, false) != NULL) {
            /* 这个 arena 已初始化,比较它的线程数 */
            for (j = 0; j < 2; j++) {
                if (arena_nthreads_get(..., i, ...)       // L266 —— 取 arena i 的 nthreads
                    < arena_nthreads_get(..., choose[j], ...)) {
                    choose[j] = i;                        // L274 —— 线程数更少,选它
                }
            }
        } else if (first_null == narenas_auto) {
            first_null = i;                               // L287 —— 记下第一个未初始化的 arena
        }
    }
    for (j = 0; j < 2; j++) {
        if (arena_nthreads_get(..., choose[j], ...) == 0
            || first_null == narenas_auto) {
            /* choose[j] 是空载的,或所有 arena 都已初始化 → 用 choose[j](最少线程的) */
            ret = arena_get(..., choose[j], false);
        } else {
            /* 还有未初始化的 arena → 初始化 first_null 这个新的 */
            choose[j] = first_null;                       // L308
            arena = arena_init_locked(..., choose[j], &arena_config_default);
            is_new_arena[j] = true;
            ret = arena;
        }
        arena_bind(tsd, choose[j], !!j);                  // L321 —— 绑定到 tsd,nthreads++
    }
    malloc_mutex_unlock(tsd_tsdn(tsd), &arenas_lock);
}
```

这段比 ptmalloc 的 `reused_arena` 聪明在两个地方:

**第一,优先用没初始化的 arena(`first_null`)。** jemalloc 的 arena 元数据是预算好的(`arena_new` 时分配 `sizeof(arena_t) + sizeof(bin_t) * nbins_total`,见 arena.c:1701-1703),但每个 arena 的**实际初始化**(建 background thread、初始化 pa_shard 等)是懒的——只有第一个绑到它的线程触发 `arena_init_locked`。所以"还有未初始化的 arena"意味着"还有完全空载、零争用的 arena",优先用它,而不是去挤已初始化但线程多的。

**第二,所有 arena 都已初始化时,选 `nthreads` 最少的(least-loaded)。** 第 266-274 行的循环遍历所有 arena,记录每个 `nthreads` 最小的下标。这样新线程总是去最闲的 arena,避免"一个 arena 挤爆、另一个空着"的不均衡。

> **不这样会怎样**(朴素方案:round-robin):如果按 `thread_id % narenas` 简单轮询绑 arena,线程退出/新建的节奏不均,容易出现某个 arena 上挤了一堆长寿命线程、另一个 arena 上是几个短寿命线程的失衡。least-loaded 让负载自然均衡,代价是每次新线程绑定时要遍历一次 arena 列表(但这是每线程一次,O(narenas),且在 `arenas_lock` 下,narenas 才几十上百,可以接受)。

### 和 ptmalloc 的 reused_arena 对照

ptmalloc 的线程绑 arena 走的是 [arena.c](https://codebrowser.dev/glibc/glibc/malloc/arena.c.html) 的 `get_free_list`(arena.c:700-731)→ `_int_new_arena`(arena.c:630-697)→ `reused_arena`(arena.c:756-815)三级。前两级和 jemalloc 类似(先找空闲的,没有就新建)。差别在第三级 `reused_arena`——当 arena 数到上限、free_list 也空时:

```c
// arena.c:756 —— ptmalloc 的 reused_arena(简化示意,非源码原文)
static mstate reused_arena(mstate avoid_arena) {
    static mstate next_to_use;
    if (next_to_use == NULL) next_to_use = &main_arena;
    result = next_to_use;
    do {
        if (!__libc_lock_trylock(result->mutex)) {   // L770 —— trylock,抢到就用
            goto out;
        }
        result = result->next;                        // 抢不到,试下一个
    } while (result != next_to_use);
    /* 全都抢不到,死等 next_to_use 这一个 */
    __libc_lock_lock(result->mutex);                  // L785 —— 阻塞等
out:
    ++result->attached_threads;
    thread_arena = result;
    next_to_use = result->next;
    return result;
}
```

ptmalloc 的 `reused_arena` 是**纯 trylock 遍历**——从 `next_to_use` 开始,逐个 arena 尝试抢锁,抢到哪个用哪个;全抢不到就死等 `next_to_use`。它**不考虑 arena 上有几个线程**,只看"锁能不能立刻拿到"。这有两个问题:**一是负载不均**——某个 arena 上的线程正好都在做别的(没抢锁),它的 mutex 就能 trylock 成功,新线程就堆过去,而不管它已经有几百个线程;**二是 trylock 遍历本身**要访问每个 arena 的 mutex 变量(虽然不持锁,但缓存行要读到本核),arena 多时也有开销。

> **钉死这件事**:jemalloc 的 `arena_choose_hard` 用 least-loaded + first_null,选的是"线程数最少的"或"完全空载的";ptmalloc 的 `reused_arena` 用 trylock 遍历,选的是"锁能立刻拿到的"。前者优化负载均衡,后者优化锁即时可用性。在"线程数 ≫ arena 数"的高并发服务里,前者更稳(避免少数 arena 被打爆)。

### percpu_arena 模式:动态按核重绑

上面讲的是 jemalloc 默认模式(percpu_arena_disabled)。jemalloc 还有个实验性的 `opt_percpu_arena` 模式(默认关闭,`PERCPU_ARENA_DEFAULT = percpu_arena_disabled`,见 arena.h:57),开了之后,arena 数 = ncpus(每核一个),线程会按"当前在哪个核"动态重绑 arena。看 [arena_inlines.h:128-138](../jemalloc/include/jemalloc/internal/arena_inlines.h#L128-L138):

```c
// arena_inlines.h:128 —— percpu_arena 模式下的动态重绑
if (have_percpu_arena && PERCPU_ARENA_ENABLED(opt_percpu_arena)
    && !internal
    && (arena_ind_get(ret) < percpu_arena_ind_limit(opt_percpu_arena))
    && (ret->last_thd != tsd_tsdn(tsd))) {           // L131 —— 上次访问的线程不是本线程
    unsigned ind = percpu_arena_choose();              // 读当前 CPU 核号
    if (arena_ind_get(ret) != ind) {
        percpu_arena_update(tsd, ind);                 // 重绑到当前核对应的 arena
        ret = tsd_arena_get(tsd);
    }
    ret->last_thd = tsd_tsdn(tsd);
}
```

**第 131 行 `ret->last_thd != tsd_tsdn(tsd)`** 是个优化——只有"上一个用这个 arena 的线程不是本线程"时,才读 CPU 核号(读核号是个相对贵的操作,要尽量少做)。这是 jemalloc 对 per-CPU 思路的实验性尝试,但默认不开——因为它依赖 `getcpu` 且每次跨线程访问都要重绑,工程上不如 tcmalloc 的 rseq per-CPU 干净。这个模式留作了解,主线还是默认的"按线程绑 arena"。P3-12 会讲 tcmalloc 怎么把 per-CPU 做成默认且无锁。

---

## 11.4 arena 内部的锁:每个 size class 一个 bin

讲完了"线程怎么绑到 arena",现在进入本章最值钱的部分:**一个 arena 内部,锁是怎么组织的?** 这才是 jemalloc 和 ptmalloc 拉开差距的核心。

### arena 不是一把锁,是三类锁

先看 arena 结构里有哪些锁。[arena.h:83-174](../jemalloc/include/jemalloc/internal/arena.h#L83-L174) 的 `struct arena_s`,挑出所有锁字段:

```c
// arena.h:83 —— arena_s 结构(简化,只列锁与 bin 相关字段)
struct arena_s {
    atomic_u_t nthreads[2];                          // L97 —— 绑定线程数(原子,无锁)
    atomic_u_t binshard_next;                        // L100 —— bin shard 轮询计数器(原子)
    ...
    ql_head(cache_bin_array_descriptor_t) cache_bin_array_descriptor_ql;
    malloc_mutex_t cache_bin_array_descriptor_ql_mtx; // L120 —— 统计链表锁(独立)
    ...
    edata_list_active_t large;
    malloc_mutex_t large_mtx;                         // L136 —— 大块分配锁(独立)
    pa_shard_t pa_shard;                             // L139 —— 页堆分片(内含 extent 的 ecache mtx)
    ...
    JEMALLOC_ALIGNED(CACHELINE)
    bin_t all_bins[];                                 // L168-170 —— 每个 size class 的 bin 数组(柔性数组)
};
```

注意三件事:

1. **`nthreads`、`binshard_next` 是 `atomic_u_t`(原子),不需要锁**——线程绑定计数是高频读写,用原子变量而非 mutex。
2. **arena 有三个独立的 `malloc_mutex_t`**:`cache_bin_array_descriptor_ql_mtx`(统计)、`large_mtx`(大块)、以及藏在 `pa_shard` 里的 extent 锁(`ecache->mtx`)。这三把锁互不干扰。
3. **真正的小对象分配锁,在 `all_bins[]` 里**——这是一个柔性数组,每个 size class 对应一个或多个 `bin_t`,每个 `bin_t` 自己一把锁。

### bin_t:每个 size class 一把锁

`bin_t` 的定义在 [bin.h:22-50](../jemalloc/include/jemalloc/internal/bin.h#L22-L50):

```c
// bin.h:22 —— bin_t:一个 size class 的 slab 管理 + 一把锁
typedef struct bin_s bin_t;
struct bin_s {
    /* All operations on bin_t fields require lock ownership. */
    malloc_mutex_t lock;              // L25 —— 这个 bin 的专属锁

    bin_stats_t stats;                // 统计(锁内访问,放 lock 旁边利于缓存)

    edata_t *slabcur;                 // 当前正在切的 slab
    edata_heap_t slabs_nonfull;       // 非满 slab 堆(按地址排序,优先用低地址)
    edata_list_active_t slabs_full;   // 满 slab 链表
};
```

**第 25 行 `malloc_mutex_t lock` 是关键**——每个 `bin_t` 自带一把锁。一个 arena 内,有 `SC_NBINS` 个 size class(典型配置 36 个小对象 size class),就有 36 个 `bin_t`,36 把锁。分配 32 字节的线程抢的是 bin[binind_32B]->lock,分配 64 字节的线程抢的是 bin[binind_64B]->lock,**它们抢的是不同的锁**。

这就是 jemalloc 相对 ptmalloc 的核心代差所在。ptmalloc 的 `malloc_state`(arena)是**一个 arena 一把 `mutex`**——同一个 arena 上,无论你分配多大的对象,抢的都是 `ar_ptr->mutex` 这一把。看 ptmalloc 的 `arena_get` 宏(arena.c:129-139):

```c
// arena.c:129 —— ptmalloc 的 arena_get 宏(简化示意)
#define arena_get(ptr, size) do { \
    ptr = thread_arena;           \  // 读 TLS 绑定
    arena_lock(ptr, size);        \  // 锁 arena 的 mutex
} while (0)

#define arena_lock(ptr, size) do {     \
    if (ptr)                           \
        __libc_lock_lock(ptr->mutex);  \  // 一个 arena 一把 mutex
    else                               \
        ptr = arena_get2((size), NULL);\
} while (0)
```

**`__libc_lock_lock(ptr->mutex)`**——ptmalloc 的 arena 锁粒度是"arena 级",一个 arena 内所有 size class 的分配/释放,都抢这一把 `mutex`。同一个 arena 上,32B 和 64B 的分配互相阻塞。

用一张 ASCII 图把 jemalloc 和 ptmalloc 的锁结构对比清楚:

```
ptmalloc 的 arena(一把大锁,所有 size class 共享):
┌─────────────────────────────────────────────────────┐
│  arena (malloc_state)                               │
│  ┌───────────┐                                      │
│  │ mutex     │ ◀── 所有 size class 的 malloc/free   │
│  └───────────┘     都抢这一把                        │
│  fastbin[0..9]  smallbin[...]  largebin[...]        │
└─────────────────────────────────────────────────────┘
  → 同 arena 上,分配 32B 和 64B 的线程互相阻塞

jemalloc 的 arena(每 size class 一把锁):
┌─────────────────────────────────────────────────────────────────┐
│  arena_s                                                        │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐       ┌──────────┐    │
│  │bin[0]    │  │bin[1]    │  │bin[2]    │  ...  │bin[35]   │    │
│  │ lock     │  │ lock     │  │ lock     │       │ lock     │    │
│  │ slabcur  │  │ slabcur  │  │ slabcur  │       │ slabcur  │    │
│  │ slabs_   │  │ slabs_   │  │ slabs_   │       │ slabs_   │    │
│  │ nonfull  │  │ nonfull  │  │ nonfull  │       │ nonfull  │    │
│  └──────────┘  └──────────┘  └──────────┘       └──────────┘    │
│   ↑ 8B class    ↑ 16B class  ↑ 32B class         ↑ 大 class      │
│                                                                 │
│  ┌──────────┐  ┌──────────────────┐  ┌────────────────────┐     │
│  │large_mtx │  │cache_bin_array_  │  │pa_shard            │     │
│  │          │  │descriptor_ql_mtx │  │ └ ecache->mtx      │     │
│  └──────────┘  └──────────────────┘  └────────────────────┘     │
└─────────────────────────────────────────────────────────────────┘
  → 分配 32B 和 64B 的线程抢不同的锁,互不阻塞
```

> **所以这样设计**:jemalloc 的锁是"按 size class 细分"——一个 arena 上绑了 N 个线程,只要它们分配的 size class 分布开(典型程序:小对象集中在十几个 class),实际同时抢同一把 bin 锁的线程数远小于 N。锁的粒度越细,争用密度越低。这是 P3-10 讲的"锁的粒度"维度在 jemalloc 里的具体落地。

### bin 怎么被选中:bin_choose 与 bin shard

线程绑了 arena 之后,分配某个 size class 时,怎么找到对应的 bin?入口是 [bin.c:319-333](../jemalloc/src/bin.c#L319-L333) 的 `bin_choose`:

```c
// bin.c:319 —— 选 bin(可能跨 shard)
bin_t *
bin_choose(tsdn_t *tsdn, arena_t *arena, szind_t binind,
    unsigned *binshard_p) {
    unsigned binshard;
    if (tsdn_null(tsdn) || tsd_arena_get(tsdn_tsd(tsdn)) == NULL) {
        binshard = 0;                                      // 重入/未初始化,用 shard 0
    } else {
        binshard = tsd_binshardsp_get(tsdn_tsd(tsdn))->binshard[binind];  // L326 —— 读 tsd 里的 shard 绑定
    }
    assert(binshard < bin_infos[binind].n_shards);
    if (binshard_p != NULL) {
        *binshard_p = binshard;
    }
    return arena_get_bin(arena, binind, binshard);        // L332 —— 取 arena->all_bins[offset + binshard]
}
```

**第 326 行 `tsd_binshardsp_get(...)->binshard[binind]`**——每个线程的 tsd 里,存着一个 `binshard[SC_NBINS]` 数组,记录"本线程在每个 size class 上用哪个 shard"。默认这个数组全 0(每个 size class 只有 1 个 shard,见 bin.c:35-40 的 `bin_shard_sizes_boot`,默认 `N_BIN_SHARDS_DEFAULT = 1`),所以默认情况下 `bin_choose` 就是 `arena_get_bin(arena, binind, 0)`,即 `arena->all_bins[binind]`。

`bin_t` 和 `bins_t`(注意单复数)的关系在 [bin.h:52-57](../jemalloc/include/jemalloc/internal/bin.h#L52-L57):

```c
// bin.h:52 —— bins_t:一个 size class 的所有 shard 容器
typedef struct bins_s bins_t;
struct bins_s {
    /* Sharded bins.  Dynamically sized. */
    bin_t *bin_shards;       // L56 —— 指向这个 size class 的所有 bin_t(shard 数可配)
};
```

也就是说,**一个 size class 默认 1 个 shard(1 把锁),但可以配成多个**——比如 `MALLOC_CONF=bin_shards:"32:4,64:4"` 表示 32B 和 64B 这两个 size class 各开 4 个 shard(4 把锁)。这是 jemalloc 给"热点 size class"额外分流锁争用的手段。`all_bins[]` 这个柔性数组的总长度,就是所有 size class 的 shard 数之和(`nbins_total`,见 arena.c:1701-1702 的 `sizeof(bin_t) * nbins_total`)。

> **钉死这件事**:jemalloc 的 bin 锁是**两级细分**:第一级是 size class(每个 class 一个 bin,默认一把锁),第二级是 shard(热点 class 可配多个 shard,多把锁)。默认配置下,一个 arena = 36 把 bin 锁 + 1 把 large_mtx + 1 把 stats 锁 + pa_shard 里的 extent 锁。ptmalloc 是一个 arena 一把 mutex——粗了两个数量级。

---

## 11.5 extent、large、stats 的锁分离

讲完了 bin 锁(小对象路径),还有三类操作的锁,它们和 bin 锁完全独立。jemalloc 把它们分开,是为了"不同性质的操作不互相阻塞"。

### large 路径:large_mtx

大块分配(超过最大 small size class,直接走 extent 而非 slab)用 arena 的 `large_mtx`(arena.h:136)。看 [arena.c:1731-1735](../jemalloc/src/arena.c#L1731-L1735) 在 `arena_new` 里初始化它:

```c
// arena.c:1731 —— 初始化 large_mtx
edata_list_active_init(&arena->large);
if (malloc_mutex_init(&arena->large_mtx, "arena_large",
        WITNESS_RANK_ARENA_LARGE, malloc_mutex_rank_exclusive)) {
    goto label_error;
}
```

`large_mtx` 保护的是 `arena->large` 这个"大块分配链表"——所有通过 extent 直接分配的大块,都挂在这个链表上,增删要抢 `large_mtx`。它和 bin 锁独立:一个线程在分配 1KB 小对象(抢 bin 锁),另一个线程在分配 2MB 大块(抢 large_mtx),互不阻塞。

### extent 路径:ecache->mtx

页堆的 extent 管理(分配/回收/合并连续页)用的是 pa_shard 里每个 ecache 自己的锁。看 [extent.c:170-238](../jemalloc/src/extent.c#L170-L238) 的 `ecache_evict`(典型 extent 操作):

```c
// extent.c:170 —— ecache_evict(回收 extent 时的典型锁模式,简化示意)
static void
ecache_evict(tsdn_t *tsdn, pac_t *pac, ehooks_t *ehooks, ecache_t *ecache, ...) {
    malloc_mutex_lock(tsdn, &ecache->mtx);              // L172 —— 锁这个 ecache
    ...
    eset_t *eset = &ecache->eset;
    ...
    /* 合并、状态转换等,都在 ecache->mtx 持有期间做 */
    malloc_mutex_unlock(tsdn, &ecache->mtx);            // L238
}
```

**第 172 行 `malloc_mutex_lock(tsdn, &ecache->mtx)`**——extent 操作锁的是 `ecache->mtx`,不是 arena 的某把锁。jemalloc 的 pa_shard 里,有多个 ecache(分别管理 dirty/muzzy/retained 等不同状态的 extent),每个 ecache 一把锁。这意味着:**小对象分配(bin 锁)、大块分配(large_mtx)、页回收与合并(ecache->mtx)三类操作,可以真正并行**——它们抢的是不同的锁。

这和 ptmalloc 形成鲜明对比。ptmalloc 的 `consolidate`(合并相邻空闲 chunk)、`_int_free` 把块挂回 bins、`systrim` 归还页给 OS——这些操作都在 arena 的 `mutex` 持有期间做(因为整个 `_int_malloc`/`_int_free` 都持有 arena mutex)。也就是说,**ptmalloc 里,合并和分配是串行的,共享一把锁**。这是 ptmalloc 在高并发 + 频繁大块分配/释放场景下中心锁疼的另一个根。

### stats 路径:cache_bin_array_descriptor_ql_mtx

统计信息的合并(把各 tcache 的 cache_bin 统计汇总到 arena)用 arena 的 `cache_bin_array_descriptor_ql_mtx`(arena.h:120)。这是个低频操作(主要在 stats dump 和线程退出时),单独一把锁避免它干扰热路径。

### 四类锁的小结

| 锁 | 保护对象 | 谁会抢 | 频率 |
|----|---------|--------|------|
| `bin->lock`(每 size class 一把,可 shard) | slab 链表、slabcur | tcache miss 后分配小对象的线程 | 中(miss 时) |
| `large_mtx`(每 arena 一把) | large 链表 | 分配/释放大块的线程 | 低 |
| `ecache->mtx`(每 ecache 一把) | extent 集合、合并 | 页堆切分/回收/合并的线程 | 低(slow path) |
| `cache_bin_array_descriptor_ql_mtx`(每 arena 一把) | tcache 统计链表 | 线程绑定/退出、stats dump | 极低 |

> **钉死这件事**:jemalloc 的锁是"按职责分":小对象(bin 锁)、大块(large_mtx)、页(ecache->mtx)、统计(stats 锁)各有独立的锁,互不阻塞。这是"治争用"的另一个维度——不只把锁摊开(多 arena、多 bin),还把**不同性质的操作**用不同的锁隔开,避免"慢操作(合并)阻塞快操作(分配)"。ptmalloc 是反面:所有这些操作共享一把 arena mutex。

---

## 11.6 锁的正确性:witness 钉死锁序防死锁

讲了一堆"锁怎么摊开、怎么分离"(性能),不能忘了锁的另一半——**正确性**。jemalloc 内部有几十把锁(bin × narenas、large_mtx × narenas、ecache->mtx × necaches、rtree、base、prof...),它们之间存在"必须按某顺序加锁"的约束,否则就死锁。

### 死锁的经典配方

死锁的经典配方是"锁顺序不一致":线程 A 先锁 X 再锁 Y,线程 B 先锁 Y 再锁 X,两个同时跑到中间就死锁。分配器里这种情况很多——比如"先锁 arena 的 bin,再锁 pa_shard 的 ecache 做 slab 补货";另一个路径可能"先锁 ecache,再锁 bin 做统计合并"。如果两个路径的锁序不一致,并发下就偶发死锁。

### witness:rank-based 锁序检查

jemalloc 用一个叫 **witness** 的机制,在 debug build 里检查锁序。每把锁在创建时带一个 `rank`(等级,是个枚举值),加锁时 witness 检查"当前线程已持有的锁里,有没有 rank 比这把锁更高的"——有就 assert 失败(因为意味着锁序反了)。

rank 的定义在 [witness.h:12-81](../jemalloc/include/jemalloc/internal/witness.h#L12-L81) 的 `enum witness_rank_e`。这个枚举是**按获取顺序排的**(注释明确写:"higher valued locks can only be acquired after lower-valued ones"):

```c
// witness.h:12 —— 锁的 rank(获取顺序,值越大越后加)
enum witness_rank_e {
    WITNESS_RANK_OMIT,                          // 不参与检查
    WITNESS_RANK_MIN,
    WITNESS_RANK_INIT = WITNESS_RANK_MIN,       // 初始化锁(最早)
    WITNESS_RANK_CTL,
    WITNESS_RANK_TCACHES,
    WITNESS_RANK_ARENAS,                        // 全局 arena 列表锁(arenas_lock)
    ...
    WITNESS_RANK_CORE,
    WITNESS_RANK_DECAY = WITNESS_RANK_CORE,     // decay(purge 调度)
    WITNESS_RANK_CACHE_BIN_ARRAY_DESCRIPTOR_QL, // arena stats 锁
    WITNESS_RANK_SEC_BIN,                       // SEC(小对象中心缓存)的 bin 锁
    WITNESS_RANK_EXTENT_GROW,                   // extent 增长锁
    WITNESS_RANK_HPA_SHARD_GROW = WITNESS_RANK_EXTENT_GROW,
    WITNESS_RANK_EXTENTS,                       // ecache->mtx 在这里
    WITNESS_RANK_HPA_SHARD = WITNESS_RANK_EXTENTS,
    WITNESS_RANK_EDATA_CACHE,
    WITNESS_RANK_RTREE,                         // radix tree 锁
    WITNESS_RANK_BASE,                          // base 元数据锁
    WITNESS_RANK_ARENA_LARGE,                   // large_mtx 在这里
    WITNESS_RANK_HOOK,
    WITNESS_RANK_LEAF = 0x1000,                 // 叶子锁(最后加)
    WITNESS_RANK_BIN = WITNESS_RANK_LEAF,       // bin->lock 是叶子
    WITNESS_RANK_ARENA_STATS = WITNESS_RANK_LEAF,
    ...
};
```

这个枚举**字面就是 jemalloc 锁的获取顺序规范**:全局列表锁(`ARENAS`)→ decay → extent grow/extends → rtree → base → large → 最后才是叶子锁(bin、stats)。任何 jemalloc 代码加锁,只要按这个 rank 从小到大,就不会死锁。

注意几个关键点:

- **`WITNESS_RANK_BIN = WITNESS_RANK_LEAF`**——bin 锁是叶子,最后加。这意味着 jemalloc 的惯例是"先拿 arena 级或全局级的锁,最后才拿 bin 锁"。比如 `arena_choose_hard` 就是先拿 `arenas_lock`(rank ARENAS),再在它保护下选 arena、初始化 arena(初始化时不拿 bin 锁)。
- **`WITNESS_RANK_EXTENTS`(ecache->mtx)在中间**——extent 锁可以在 bin 锁之前拿(补货 slab 时先锁 ecache 切页,再锁 bin 挂上),但反过来不行。
- **同 rank 的锁(如多个 bin 锁,都是 LEAF)不能同时持有**——这是为什么 jemalloc 一次分配只锁一个 bin(不会同时锁两个 bin)。

> **不这样会怎样**(没有 witness):一个有几十把锁的分配器,任何一处锁顺序写反,就是**偶发死锁**——生产环境上极难复现、极难调试。witness 把"锁顺序"这个隐式约束**显式化**:每把锁带 rank,加锁时 witness 遍历当前线程已持锁,发现 rank 更高的立刻 assert。这是 debug build 的"防呆",也是 jemalloc 这种锁密集型代码能保持正确性的工程基建。release build 里 witness 检查被编译掉(零开销),但开发/测试时跑 debug build 能抓出绝大多数锁序 bug。

> **钉死这件事**:jemalloc 用 witness(rank-based 锁顺序检查)在 debug build 防死锁。每把锁带一个 rank,加锁时检查"已持锁里有没有 rank 更高的",有就 assert。这个枚举就是 jemalloc 锁获取顺序的规范文档。tcmalloc 用 absl 的 `SpinLock` 配合 RAII holder(`SpinLockHolder`)防遗忘 unlock,但不做锁序检查(它的锁更少、层次更浅)。P5-17 fork 章会详讲 fork 时怎么在 prefork 抢所有锁(也是靠 witness 的全 rank 遍历)。

---

## 11.7 技巧精解:arena 数量与 bin shard 的权衡

本章有两个最硬核的工程权衡,单独拆透:**为什么是 `4×ncpu` 个 arena**(而不是 1×、ncpus× 或每线程一个)、以及 **bin shard 什么时候值得开**。

### 权衡一:arena 数量的三难

"开几个 arena"是个三难问题,三个力在拉扯:

1. **争用密度**(要 arena 多):锁被抢的概率随"抢同一把锁的线程数"近似平方下降。arena 越多,每个 arena 的线程越少,bin 锁争用越轻。
2. **元数据占用**(要 arena 少):每个 arena 一份元数据——`arena_t` 结构 + `all_bins[nbins_total]`(每个 bin 含 `malloc_mutex_t` ~40 字节、slab 堆、stats)+ pa_shard(ecache 数组、decay 状态)。一个 36-bin 的 arena,光 bin 数组就 `36 × sizeof(bin_t)` ≈ 几 KB;pa_shard 的 extent 缓存还要更多。开 N 个 arena,这些元数据 ×N。
3. **缓存局部性**(要 arena 少且稳定):线程 A 在 arena 5 分配的块,如果被线程 B(绑 arena 7)释放,跨 arena 的 free 要走更重的路径(不能直接挂回 arena 5 的 bin,要经 pa_shard 中转)。arena 越多,跨 arena 流转越频繁,缓存局部性越差。另外,每个 arena 各自囤 cache_bin,总占用 = narenas × 每 arena 缓存量,线性增长。

把三个力画成一张图(横轴 arena 数,纵轴代价):

```
代价
  ↑
  │  元数据占用 ───────────────  (线性增长,每 arena 一份元数据)
  │
  │                    缓存局部性损失 ───────────  (随 arena 数缓增,跨 arena 流转变多)
  │
  │──────────────  锁争用密度  (随 arena 数平方下降,但很快趋平)
  │
  └─────────────────────────────────────→ arena 数
     1×ncpu    4×ncpu    8×ncpu    每线程一个
                   ▲
              甜点(经验值)
```

`4×ncpu` 是 jemalloc 在 Facebook/FreeBSD 长期实践里选的甜点。它的隐含假设是:**典型服务的线程数显著大于核数(几百到几千 vs 几十核),但不会到几万**。在这个假设下:

- arena 数 = 4×ncpu,在 32 核机器上是 128 个。500 个线程 → 平均每 arena 4 个线程,bin 锁争用已经很低。
- 元数据:128 个 arena × (几 KB bin + pa_shard),总计几十 KB 到几百 KB,可接受。
- 缓存局部性:128 个 arena 不算多,跨 arena 流转可控。

**为什么不是 1×ncpu?** 在"线程数 = 2× 核数"时(64 核机器跑 128 线程),1×ncpu = 64 个 arena,平均每 arena 2 个线程——还行。但典型服务线程数远超 2× 核数(几百上千),1×ncpu 就会让每 arena 挤十几个线程,bin 锁争用回升。4× 留了余量。

**为什么不是 8×ncpu(ptmalloc 的上限)?** ptmalloc 选 8×ncpu 是因为它**没有 bin 锁细分**(一个 arena 一把 mutex),只能靠更多 arena 来摊争用。jemalloc 有 bin 锁细分(36 把 bin 锁/arena),不需要那么多 arena 就能把争用压下去,所以选了更省的 4×。换句话说:**jemalloc 的"bin 锁细分"替"arena 数量"分担了降压任务,所以 arena 可以少开**。这是两个机制的正交配合。

**为什么不每线程一个 arena?** 1000 个线程开 1000 个 arena:元数据爆炸(几 MB 到几十 MB)、每个 arena 囤一份 cache 总占用飙升、线程退出后独占 arena 的 cache 无法立刻被复用。arena 是"按核数规模建的共享资源",不是每线程一份的私有物——私有的那一层是 tcache(P1-05),不是 arena。

> **反面对比**(单 arena 一把锁,ptmalloc 早期/线程数≫arena 时):32 核 1000 线程,ptmalloc arena 上限 256 个(8×ncpu),平均每 arena 4 个线程,但**每 arena 一把 mutex**——这 4 个线程无论分配什么 size class 都抢同一把锁。jemalloc 同样 4 个线程/arena,但抢的是 36 把 bin 锁里的一把(如果 size class 分布开,实际同时抢同一把 bin 锁的往往只有 1 个线程)。这就是"arena 数相当,但 jemalloc 争用低一个数量级"的根。

### 权衡二:bin shard 什么时候开

默认每个 size class 1 个 shard(1 把 bin 锁)。什么时候值得开多个 shard?答案是:**某个 size class 成了热点**——即大量线程集中在同一个 size class 上分配,导致这一把 bin 锁被打爆。

典型场景:一个 RPC 服务器,所有请求都分配 64 字节的请求对象(协议头)。几千个线程都集中在 64B 这个 size class 上,即使分散在多个 arena,每个 arena 的 64B bin 锁仍可能被该 arena 上所有线程抢(因为它们都分 64B)。这时给 64B 配 4 个 shard,等于把这一把锁摊成 4 把,争用降 16 倍。

jemalloc 允许用 `MALLOC_CONF=bin_shards:"64:4"` 这样配。源码上,`bin_update_shard_size`([bin.c:10-32](../jemalloc/src/bin.c#L10-L32))处理这个配置:

```c
// bin.c:10 —— 配置某 size class 的 shard 数
bool
bin_update_shard_size(unsigned bin_shard_sizes[SC_NBINS], size_t start_size,
    size_t end_size, size_t nshards) {
    if (nshards > BIN_SHARDS_MAX || nshards == 0) {
        return true;                                     // shard 数超限或为 0,拒绝
    }
    ...
    szind_t ind1 = sz_size2index_compute(start_size);
    szind_t ind2 = sz_size2index_compute(end_size);
    for (unsigned i = ind1; i <= ind2; i++) {
        bin_shard_sizes[i] = (unsigned)nshards;          // 给这个区间的 size class 都设 nshards 个 shard
    }
    return false;
}
```

线程绑 shard 是通过 tsd 里的 `binshard[binind]` 数组(bin.c:326 读它)。绑定逻辑在 tsd 初始化时,用 arena 的 `binshard_next`(arena.h:100,原子计数器)round-robin 分配——每个新线程在每个 size class 上,拿到 `binshard_next++ % n_shards` 这个 shard。这样同一 size class 的多个线程,被均匀摊到该 class 的多个 shard 上。

> **反面对比**(朴素方案:不分 shard,只用 bin 锁细分到 size class):如果 64B 这个热点 size class 只有 1 个 shard,几千个线程集中分 64B,即使分散到 128 个 arena,每个 arena 的 64B bin 锁仍被该 arena 上集中分 64B 的线程抢。开 4 个 shard,每把锁的争用降到 1/16。代价是元数据(每 shard 一个 bin_t,几十字节)+ slab 在 shard 间分散(每 shard 各自维护 slabcur 和 slabs_nonfull,slab 利用率略降)。这是个"用元数据换争用"的精细权衡,只在确有热点 size class 时才开(默认不开)。

### 两个权衡的共性:用元数据/局部性换争用

`4×ncpu` 和 bin shard,本质都是同一个权衡的不同粒度:**用元数据占用和缓存局部性,换锁争用的下降**。

- `4×ncpu` 是"arena 级"的摊——多开 arena,每 arena 元数据一份,换 bin 锁争用整体下降。
- bin shard 是"size class 级"的摊——热点 class 多开 shard,每 shard 元数据一份,换这一把锁的争用下降。

两者叠加,jemalloc 的锁总数 = `narenas × Σ(n_shards_per_class)`,在默认配置下是 `4×ncpu × 36`,在 32 核机器上是 4608 把 bin 锁。ptmalloc 是 `≤ 8×ncpu × 1`,最多 256 把 mutex。锁多了近 20 倍,且每把锁的争用密度低得多——这就是 jemalloc 中心层在高并发下几乎不疼的算术根。

> **钉死这件事**:jemalloc 的并发设计是"两级摊 + 一级隔离":第一级 arena 摊(横向,4×ncpu),第二级 bin shard 摊(纵向,热点 class 多 shard),第三级 extent/large/stats 隔离(不同性质操作不同锁)。三级叠加,把锁争用压到极低,代价是元数据和缓存局部性。ptmalloc 只做了第一级的粗糙版(8×ncpu 上限 + 一把 arena mutex),没做第二级,第三级也没有——这就是代差。

---

## 11.8 四套横评:arena/bin 并发模型对照

把四套分配器的并发模型放一起对照,看清各自的取舍。

| 维度 | tcmalloc | jemalloc | mimalloc | ptmalloc |
|------|----------|----------|----------|----------|
| **arena 机制** | 无 arena(中心层按 size class 分锁) | **多 arena**,默认 `4×ncpu`,least-loaded 绑定 | 无 arena(thread-local heap) | **多 arena**,上限 `8×ncpu`,按需懒创建 |
| **小对象中心锁粒度** | 每 size class 一个 `CentralFreeList` + `SpinLock`(central_freelist.h:271) | 每 arena × 每 size class 一个 `bin->lock`(bin.h:25),可 shard | 无中心锁(heap 私有,跨线程走 delayed free) | **每 arena 一把 `mutex`**(arena.c:136),粒度最粗 |
| **线程绑定对象** | per-CPU cache(随调度变,P3-12) | arena(绑死在 tsd,first_null + least-loaded) | thread-local heap(每线程一个) | arena(TLS `thread_arena`,arena.c:88) |
| **大块/extent 锁** | page_allocator 内部锁 | `large_mtx`(arena.h:136)+ `ecache->mtx`(extent.c:172)分离 | segment/arena 锁 | arena mutex(和分配共享) |
| **锁序防护** | `SpinLockHolder` RAII(防遗忘 unlock) | **witness**(rank-based 锁序检查,debug build) | 无显式锁序机制 | 无显式锁序机制 |
| **热点 size class 额外分流** | 中心层天然按 size class 分(无热点放大) | **bin shard**(`MALLOC_CONF=bin_shards`) | 无(每线程私有,无热点) | 无(一把 mutex,热点更疼) |
| **per-CPU 能力** | **是**(rseq,P3-12 主菜) | 实验性(`opt_percpu_arena`,默认关) | 否 | 否 |

几个要点:

- **tcmalloc 不走 arena 路线**。它的中心层是"每 size class 一个 `CentralFreeList`,每个一把 `absl::SpinLock`"(见 [central_freelist.h:271](../tcmalloc/tcmalloc/central_freelist.h#L271) 的 `SpinLock lock_;`)。它不靠 arena 分流线程,而是靠 per-CPU cache(P3-12)在 fast path 治跨核争用,中心锁用批量取还摊薄。所以 tcmalloc 没有"arena 选择""线程绑定 arena"这一整套机制——它的并发主轴是 per-CPU,不是 arena。
- **mimalloc 走的是 thread-local heap**(每线程一个 `mi_heap_t`,见 [alloc.c:208](../mimalloc/src/alloc.c#L208) 的 `mi_prim_get_default_heap()`)。本线程的 malloc/free 操作这个 heap 的 page free list,**完全无锁**;跨线程 free 用 delayed free(一次 CAS 入延迟列表,P3-10 讲过)。它既没有 arena 也没有 per-CPU,但靠"heap 私有 + 延迟"把跨线程争用压到一次 CAS。
- **ptmalloc 是 jemalloc 的"粗糙版前身"**。同样是多 arena,但 ptmalloc 的 arena 是"一把 mutex 管所有 size class",且 reused_arena 用 trylock 遍历(不考虑负载)。它的 fast path(tcache)又是 glibc 2.26 才后加的、容量小,miss 率高,中心锁被打得频。三条叠加,ptmalloc 在高并发服务里中心锁最疼。
- **jemalloc 是"多 arena + bin 锁细分 + witness"的集大成者**。它把 P3-10 讲的"解法①(多 arena)"做到了工程极致:arena 数精心选(4×ncpu)、线程绑定精心选(least-loaded)、arena 内锁按 size class 细分(bin)、热点 class 还能再 shard、不同性质操作锁分离(extent/large/stats)、witness 防死锁。

> **钉死这件事**:四套分配器的并发模型是四条不同的路。jemalloc = 多 arena + bin 锁细分;ptmalloc = 多 arena + 粗 mutex(baseline);tcmalloc = per-CPU + 中心 SpinLock(无 arena);mimalloc = thread-local heap + delayed free(无 arena 无 per-CPU)。jemalloc 和 ptmalloc 同属"解法①",但 jemalloc 在每个工程细节上都更精细——这就是同样多 arena、性能却差一个数量级的根。

---

## 章末小结

这一章是"解法①(多 arena)"的工程化深拆,我们没有钻进 rseq 或 per-CPU(那些留给 P3-12),但把 jemalloc 的多 arena 方案拆到了源码:arena 数怎么定(`4×ncpu`)、线程怎么绑(least-loaded + first_null)、arena 内部锁怎么按 size class 细分(bin + shard)、extent/large/stats 怎么分离、witness 怎么防死锁。

1. **arena 数量 = `ncpus × 4`**:源码是 `fxp_mul(ncpus, opt_narenas_ratio)`(jemalloc_init.c:454),ratio 默认 4。这是"争用密度、元数据占用、缓存局部性"三难权衡的甜点,假设线程数显著大于核数但不到几万。ptmalloc 是 `8×ncpu` 上限、按需懒创建——数量级相当,但 jemalloc 配 bin 锁细分,不需要那么多 arena。
2. **线程绑 arena = least-loaded + first_null**:`arena_choose` 读 tsd(fast path),miss 才 `arena_choose_hard`(每线程一次),后者优先用未初始化的 arena、其次选 `nthreads` 最少的(arenas_management.c:266-274)。比 ptmalloc 的 `reused_arena`(trylock 遍历,不考虑负载)更均衡。
3. **arena 内部锁按 size class 细分**:每个 size class 一个 `bin_t`、一把 `malloc_mutex_t`(bin.h:25),热点 class 还能配多个 shard(bin.h:52-57)。一个 arena = 36 把 bin 锁 + large_mtx + extent(ecache->mtx)+ stats 锁,互不干扰。ptmalloc 是一把 arena mutex——粗两个数量级。
4. **锁序用 witness 防死锁**:每把锁带 rank(witness.h:12-81),debug build 加锁时检查"已持锁里有没有 rank 更高的"。这个枚举就是 jemalloc 锁获取顺序的规范。
5. **两个核心权衡**:arena 数量(4×ncpu)和 bin shard,本质都是"用元数据/局部性换争用"——多开 arena/多开 shard,每份元数据一份,换锁争用平方级下降。

> **打个比方**(只在反直觉处点一下):多 arena 像"把一个大收银台拆成 N 个窗口"——排队的人分流了;bin 锁细分像"每个窗口里再按商品型号分通道"——买 32 号零件和买 64 号零件的人不互相挡;bin shard 像"某个热门型号的通道再开几条"——挤在同一个型号的人进一步分流;extent/large/stats 锁分离像"退货、开发票、咨询各开独立窗口"——不同业务不互相阻塞。四层叠起来,几千人同时进出,每个窗口前几乎没人排队。ptmalloc 只有第一层(还是粗糙的),所以它的收银台前总是排长队。

回扣全书的二分法:这一章讲的所有机制——多 arena、bin 锁、bin shard、least-loaded 绑定、锁分离——**全部服务于"局部缓存"这一面(fast path 的"防御纵深")**。它们的职责是:当 fast path(tcache)miss 后,中心层(bin / extent)不能被锁拖死。jemalloc 用"多 arena × 多 bin × 多 shard × 锁分离"把中心层的锁争用压到极低,让 miss 后的降级不至于卡住。中心堆的"省"(合并、归还 OS、大页)是第 4 篇的主题,那里的锁(ecache->mtx、decay 锁)本章只点到,留给 P4-13~15 详讲。

### 五个"为什么"清单

1. **为什么 jemalloc 默认开 `4×ncpu` 个 arena?** `fxp_mul(ncpus, opt_narenas_ratio)`,ratio 默认 4(jemalloc_init.c:454)。4× 是"争用密度(要 arena 多)、元数据占用(要 arena 少)、缓存局部性(要 arena 少且稳定)"三难权衡的甜点,假设线程数显著大于核数但不到几万。1×ncpu 在高并发下争用回升,8×ncpu(ptmalloc 上限)元数据翻倍且 jemalloc 有 bin 锁细分不需要那么多,每线程一个则元数据爆炸。
2. **为什么 jemalloc 的 bin 锁比 ptmalloc 的 arena mutex 细?** jemalloc 每个 size class 一个 `bin->lock`(bin.h:25),分配 32B 和 64B 抢不同锁;ptmalloc 一个 arena 一把 `mutex`(arena.c:136),所有 size class 共享。锁粒度差两个数量级,争用密度随之差近两个数量级——这是同样多 arena、jemalloc 性能更好的根。
3. **为什么线程绑 arena 用 least-loaded 而不是 round-robin?** round-robin(`thread_id % narenas`)在线程退出/新建节奏不均时容易失衡(某 arena 挤爆、另一个空着)。least-loaded(`arena_choose_hard` 选 `nthreads` 最少的,arenas_management.c:266)让负载自然均衡。代价是每线程一次 O(narenas) 遍历,但 narenas 才几十上百,可接受。
4. **为什么 jemalloc 要把 extent/large/stats 的锁和 bin 锁分开?** 不同性质操作的延迟不同:extent 合并是慢操作(扫邻居、改 rtree),bin 分配是快操作(pop slab)。共享一把锁会让慢操作阻塞快操作。分开后,小对象分配(bin 锁)、大块分配(large_mtx)、页回收(ecache->mtx)可真正并行。ptmalloc 反面:这些操作共享 arena mutex,合并阻塞分配。
5. **为什么需要 witness?** jemalloc 内部几十把锁,存在"必须按某顺序加"的约束(否则死锁)。witness 给每把锁一个 rank(witness.h:12-81),debug build 加锁时检查"已持锁里有没有 rank 更高的",有就 assert。把隐式的锁序约束显式化,debug 时抓锁序 bug。release build 编译掉,零开销。

### 想继续深入往哪钻

- **arena 数量与线程绑定**:[jemalloc_init.c:445-463](../jemalloc/src/jemalloc_init.c#L445-L463) 的 `malloc_narenas_default`(4×ncpu 怎么算)、[arenas_management.c:226-340](../jemalloc/src/arenas_management.c#L226-L340) 的 `arena_choose_hard`(least-loaded + first_null)。想自己调,试 `MALLOC_CONF=narenas:8` 或 `narenas_ratio:2`,跑 benchmark 看争用变化。
- **bin 与 bin shard**:[bin.h:22-57](../jemalloc/include/jemalloc/internal/bin.h#L22-L57) 的 `bin_t`/`bins_t` 结构、[bin.c:10-40](../jemalloc/src/bin.c#L10-L40) 的 `bin_update_shard_size`/`bin_shard_sizes_boot`、[bin.c:319-333](../jemalloc/src/bin.c#L319-L333) 的 `bin_choose`。想给热点 size class 加 shard,试 `MALLOC_CONF=bin_shards:"64:4"`。
- **锁结构**:[arena.h:83-174](../jemalloc/include/jemalloc/internal/arena.h#L83-L174) 的 `arena_s`(三类锁)、[bin.h:25](../jemalloc/include/jemalloc/internal/bin.h#L25) 的 `bin->lock`、[extent.c:170-238](../jemalloc/src/extent.c#L170-L238) 的 `ecache->mtx`、[mutex.h:264-268](../jemalloc/include/jemalloc/internal/mutex.h#L264-L268) 的 `malloc_mutex_lock`(先 trylock 再 slow path)。
- **witness 锁序**:[witness.h:12-81](../jemalloc/include/jemalloc/internal/witness.h#L12-L81) 的 `witness_rank_e` 枚举(锁获取顺序规范)。debug build(`--enable-debug`)编译 jemalloc,故意写个锁序错误的路径,witness 会立刻 assert。
- **ptmalloc 对照**:[arena.c](https://codebrowser.dev/glibc/glibc/malloc/arena.c.html) 的 `arena_get2`(817-865)、`reused_arena`(756-815)、`_int_new_arena`(630-697)、`get_free_list`(700-731)。对比 jemalloc 的 `arena_choose_hard`,看同样是"选 arena",两者的策略差异。
- **mimalloc 对照**:[alloc.c:203-209](../mimalloc/src/alloc.c#L203-L209) 的 `mi_malloc` → `mi_prim_get_default_heap`(thread-local heap,无 arena)、[heap.c:200-213](../mimalloc/src/heap.c#L200-L213) 的 `mi_heap_get_default`/`mi_heap_get_backing`。对比 jemalloc 的多 arena,理解"为什么 mimalloc 不需要 arena"。

### 引出下一章

我们拆透了 jemalloc 的多 arena 方案——它是"解法①"的工程极致。但 P3-10 已经点过,多 arena 有个天花板:**它治不了"跨核争用"本身**。一个 arena 上绑的线程,可能被调度在不同的核上,它们抢同一把 bin 锁时,锁的缓存行仍会在核间弹来弹去。要治这个,得用"解法②"——让锁只在核内存在。下一章,P3-12,走进 tcmalloc 的 per-CPU cache:它怎么用 Linux rseq(restartable sequences)做到"被抢占时安全回退"、为什么 per-CPU 天然按物理核摊开避免跨核争用、以及为什么这是 tcmalloc 新版相对 jemalloc 的代差。两条路在 P3 这一篇分叉,你会在第 12 章末尾看到它们各自的代价。
