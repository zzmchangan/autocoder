# 附录 B · 源码阅读路线与实战

> 篇:附录
> 定位:这是一份**工具附录**。前面 21 章把四套分配器的"为什么"和"怎么实现"讲透了,这一篇只回答两个最实际的问题——**① 怎么真的去读这些源码,不至于在六千行的 `malloc.c` 里迷路;② 怎么把学到的东西用起来:换分配器、调参、抓内存问题。**
>
> 全文以清单、命令、表格为主,直球可操作。所有文件名、行号、参数名都对着源码核实过(tcmalloc@7723f74、jemalloc@9f37c70、mimalloc@fef6b0d;ptmalloc 用在线 glibc main 分支)。

---

## 第一部分 · 四套源码阅读地图

四套分配器代码量差异很大:`malloc.c` 一个文件约 6500 行,而 tcmalloc 整仓上千个文件。直接 `git clone` 下来打开根目录,新人大概率在第一周就放弃。下面给每套一套**推荐入口 + 顺藤摸瓜的阅读顺序 + 关键文件清单**,目标是用最短路径看到四套的核心机制。

通用的阅读心法:**永远从 `malloc`/`free` 的入口追下去,不要按目录顺序读**。分配器是高度流程化的代码,一次 `malloc` 的调用链就是它的骨架,顺着骨架走,每个文件什么时候被卷进来、为什么存在,一目了然。每追到一个新文件,先问一句"它在三层快慢道里属于哪一层(本地缓存 / 中心链表 / 页堆)",定位就清楚了。

### B.1 tcmalloc:从 fast path 追到 huge page filler

**源码位置**:`../tcmalloc/`(本地 `google/tcmalloc` @ `7723f74`)。这是 C++ 重写的新版,**不是** gperftools 那个老版本——两者 API、调参机制完全不同,读源码前先确认你 clone 的是 `google/tcmalloc`。

**推荐入口**:`tcmalloc/tcmalloc.cc`。全书第 1 章已经把 fast path 的入口拆过一次,这里再确认一遍调用链。

```cpp
// tcmalloc/tcmalloc.cc:1198 —— fast path 起点
res = tc_globals.cpu_cache().AllocateFast(size_class)
//         ↓ miss
// tcmalloc.cc:1200
res = tc_globals.cpu_cache().AllocateSlow(size_class)
//         ↓ 中心链表补货
```

**顺藤摸瓜的阅读顺序**(每一步都顺着上一步的 miss 路径往下追):

1. **`tcmalloc.cc`** —— 看 `AllocateFast`/`AllocateSlow`(行 1196~1205)、`do_malloc_pages`(行 624,大块旁路)。这是三层快慢道的总开关。
2. **`cpu_cache.cc` / `cpu_cache.h`** —— per-CPU 缓存的实现。重点看 `AllocateFast` 怎么用 rseq(Linux restartable sequences)做到 per-CPU 无锁、被抢占时安全回退。这是 tcmalloc 新版的灵魂(第 12 章专题)。
3. **`central_freelist.cc`** —— 中心自由链表,按 size class 组织,批量取还。配合 `transfer_cache.cc` 看线程间的批量流转。
4. **`page_allocator.cc` / `span.cc` / `pagemap.cc`** —— 页堆:span(连续 N 页)的切分合并、放射状 pagemap(页号 → span 的 O(1) 反查)。第 7~8 章专题。
5. **`huge_page_aware_allocator.cc` + `huge_page_filler.h` + `huge_page_subrelease.h`** —— HPAA,新一代治碎片的杀手锏(第 15 章专题)。`huge_page_filler.h` 是纯头文件,算法密度高,建议最后读。
6. **(支线)`sampler.cc` / `allocation_sampling.cc`** —— 几何采样,第 18 章;**`guarded_page_allocator.cc`** —— guarded page,第 19 章。

**关键文件清单**:

| 文件 | 作用 | 对应章节 |
|------|------|---------|
| `tcmalloc.cc` | malloc/free 入口,fast/slow 分流 | P0-01 |
| `cpu_cache.cc` / `.h` | per-CPU 缓存,rseq | P3-12 |
| `thread_cache.cc` | legacy per-thread 缓存(对照) | P1-05 |
| `central_freelist.cc` | 中心自由链表 | P1-06 |
| `transfer_cache.cc` | 线程间批量流转 | P1-06 |
| `page_allocator.cc` / `span.cc` | 页堆,span 切分合并 | P2-07 |
| `pagemap.cc` / `pagemap.h` | 放射状 pagemap(指针→span) | P2-08 |
| `huge_allocator.cc` / `huge_cache.cc` | 大块路径 | P2-09 |
| `huge_page_aware_allocator.cc` | HPAA 主体 | P4-15 |
| `huge_page_filler.h` | 大页碎片打包 | P4-15 |
| `guarded_page_allocator.cc` | guarded page 抓堆溢出 | P6-19 |
| `sampler.cc` | 几何采样 | P5-18 |
| `static_vars.cc` | 全局静态状态、自举 | P5-16 |

> 小贴士:低层原语(原子、rseq、缓存行对齐)在 `tcmalloc/tcmalloc/internal/` 下,比如 `internal/percpu.h`(rseq 支持)、`internal/cacheintrinsics.h`。遇到不懂的 `ABSL_` 宏或原子操作,先去 `internal/` 翻。

### B.2 jemalloc:从 imalloc_fastpath 追到 hpa

**源码位置**:`../jemalloc/`(本地 `jemalloc/jemalloc` @ `9f37c70`)。jemalloc 的代码组织比 tcmalloc 整齐:`src/` 放实现,`include/jemalloc/internal/` 放内联函数和数据结构头。

**推荐入口**:`src/jemalloc.c`。

```c
// src/jemalloc.c:805 —— je_malloc,薄得几乎一行
je_malloc(size_t size) {
    void *ret = imalloc_fastpath(size, &malloc_default);   // L808
    return ret;
}
```

`imalloc_fastpath` 命名直白——先走 fast path(线程 tcache),miss 了落到 `imalloc` 走 arena 的 bin。

**顺藤摸瓜的阅读顺序**:

1. **`src/jemalloc.c`** —— `je_malloc`(808)、`je_free`(1060)入口。看 fast path miss 后怎么落到 arena。
2. **`src/tcache.c` + `include/jemalloc/internal/cache_bin.h`** —— 线程缓存:`cache_bin` 是每个 size class 一条的无锁自由链表,`cache_bin_alloc`(tcache.c:607)就是 fast path 的 pop。
3. **`src/arena.c` + `src/bin.c` + `src/bin_info.c`** —— arena 和 bin:每个 arena 每个 size class 一个 bin、一把锁。`arenas_management.c` 管 arena 的创建和线程绑定。第 11 章专题。
4. **`src/extent.c` + `src/rtree.c` + `src/emap.c`** —— extent(连续页)和 radix tree(指针 → extent 的反查)。`extent_recycle`(extent.c:633)是页堆回收复用的核心。第 7~8 章专题。
5. **`src/hpa.c` + `src/hpa_central.c` + `src/hpdata.c` + `src/psset.c`** —— jemalloc 的 huge page allocator(对标 tcmalloc 的 HPAA)。第 15 章专题。
6. **(支线)`src/decay.c` + `src/background_thread.c`** —— 基于时间衰减的 purge(第 14 章);`src/prof.c` + `src/prof_data.c` —— 采样 profiling(第 18 章);`src/jemalloc_fork.c` —— fork 锁处理(第 17 章);`src/san.c` / `src/san_bump.c` —— sanitizer 集成(第 19 章)。

**关键文件清单**:

| 文件 | 作用 | 对应章节 |
|------|------|---------|
| `src/jemalloc.c` | malloc/free 入口 | P0-01 |
| `src/tcache.c` | 线程缓存 | P1-05 |
| `include/jemalloc/internal/cache_bin.h` | 无锁自由链表(内联 pop/push) | P1-04 |
| `src/arena.c` / `src/arenas_management.c` | arena 创建与线程绑定 | P3-11 |
| `src/bin.c` / `src/bin_info.c` | 每个 size class 一个 bin | P1-06 |
| `src/extent.c` | extent 切分合并、回收复用 | P2-07 |
| `src/rtree.c` + `include/jemalloc/internal/rtree.h` | radix tree(指针→extent) | P2-08 |
| `src/emap.c` | extent map 统一索引 | P2-08 |
| `src/large.c` | 大块路径 | P2-09 |
| `src/hpa.c` / `src/hpdata.c` / `src/psset.c` | huge page allocator | P4-15 |
| `src/decay.c` / `src/background_thread.c` | 时间衰减 purge | P4-14 |
| `src/prof.c` / `src/prof_data.c` | 采样 profiling | P5-18 |
| `src/jemalloc_fork.c` | fork 锁处理 | P5-17 |
| `src/conf.c` | MALLOC_CONF 解析(调参必读) | 本附录 B.6 |
| `src/tsd.c` | thread-specific data(懒创建) | P5-16 |
| `src/sc.c` / `src/sz.c` | size class 与 size 转换 | P1-02 |

> 小贴士:jemalloc 的内联函数大量在 `include/jemalloc/internal/` 头文件里(`cache_bin.h`、`rtree.h`、`tcache.h`、`tsd.h`),追调用时 `src/` 找不到就去 `include/` 翻。

### B.3 mimalloc:最干净的代码,适合入门

**源码位置**:`../mimalloc/`(本地 `microsoft/mimalloc` @ `fef6b0d`)。mimalloc 的代码是四套里**最干净、最易读**的——文件少、命名直观、注释充分,强烈建议**作为读分配器源码的第一站**,建立心智模型后再去啃 tcmalloc/jemalloc。

**推荐入口**:`src/alloc.c`。

```c
// src/alloc.c:203 —— mi_malloc 转给"默认堆"(当前线程的堆)
mi_heap_malloc(mi_heap_t* heap, size_t size) {
    return _mi_heap_malloc_zero(heap, size, false);          // L204
}
mi_malloc(size_t size) {
    return mi_heap_malloc(mi_prim_get_default_heap(), size); // L207-208
}
```

`mi_prim_get_default_heap()` 拿的是当前线程的 thread-local heap。mimalloc 的设计特别干净:**每个线程一个 heap,heap 挂着若干 segment,segment 里是按 page 组织的 free list**——本地缓存就是 heap 自己,没有单独的 tcache 概念。

**顺藤摸瓜的阅读顺序**:

1. **`src/alloc.c`** —— `mi_malloc`(207)、`mi_free` 入口。`alloc-aligned.c`、`alloc-posix.c` 是对齐分配和 POSIX 接口的变体。
2. **`src/heap.c`** —— `mi_heap_t` 结构、thread-local heap 的懒创建和清理。
3. **`src/segment.c` + `src/page.c`** —— segment(向 OS 要的一大块)和 page(segment 内的分配单元,每个 page 一个 size class 的 free list)。`_mi_segment_page_alloc`(segment.c:1662)是页堆补货入口。
4. **`src/arena.c` + `src/arena-abandon.c`** —— arena(更大的预留区,多个 segment 的容器)和 arena-abandon(整 arena 抛弃归还,第 14 章)。
5. **(支线)`src/random.c`** —— 布局随机化(第 19 章);`src/stats.c` —— 统计;`src/init.c` —— 自举;`src/options.c` —— 运行时选项解析。

**关键文件清单**:

| 文件 | 作用 | 对应章节 |
|------|------|---------|
| `src/alloc.c` | malloc/free 入口 | P0-01 |
| `src/heap.c` | thread-local heap | P1-05 |
| `src/segment.c` | segment(大块容器) | P2-07 |
| `src/page.c` / `src/page-queue.c` | page 内 free list | P1-04 |
| `src/segment-map.c` | 指针→page 反查 | P2-08 |
| `src/arena.c` / `src/arena-abandon.c` | arena 与 abandon | P4-14 |
| `src/init.c` | 自举初始化 | P5-16 |
| `src/options.c` | 运行时选项 | 本附录 B.7 |
| `src/random.c` | 随机化布局 | P6-19 |
| `src/stats.c` | 统计 | P5-18 |
| `include/mimalloc.h` | 公共 API(`mi_malloc` 声明在 110 行) | — |

> 小贴士:mimalloc 的 `include/mimalloc/` 下有完整的公开 API 头(`.h`),读源码前先扫一遍公共 API,对它的能力边界会清楚很多。

### B.4 ptmalloc:在线读一个 6500 行的巨石文件

**源码位置**:不本地 clone(整仓过大,且我们只看 `malloc/malloc.c`)。在线读:[glibc/malloc/malloc.c](https://github.com/glibc/glibc/blob/main/malloc/malloc.c)(主分支;也可用 [Elixir Bootlin 带行号跳转版](https://elixir.bootlin.com/glibc/latest/source/malloc/malloc.c))。它是 baseline,只作对照、不深挖。

**推荐入口**:`__libc_malloc`(对外入口)→ `_int_malloc`(真正的分配逻辑)→ `_int_free` → `consolidate`(合并)。

**顺藤摸瓜的阅读顺序**:

1. **`__libc_malloc`** —— 入口,先查 tcache,再进 `_int_malloc`。
2. **`_int_malloc`** —— 这是 ptmalloc 的心脏,按这个顺序看分支:**fastbin**(小块,LIFO 单链表)→ **smallbin**(中等,双向链表)→ **unsorted bin** 扫一遍 → **largebin**(大块,按大小分组)。每个 bin 是一段独立的代码,逐个看。
3. **`_int_free`** —— 释放:先塞 tcache/fastbin,否则进 unsorted bin。
4. **`consolidate`** —— 合并相邻空闲 chunk,治外部碎片。注意它只在特定时机触发(malloc 大块前、`malloc_trim` 时),这就是 ptmalloc 碎片压不住的根。
5. **`malloc_trim`** —— 主动归还内存给 OS(给 `mallopt`/`M_TRIM_THRESHOLD` 配套)。

**关键宏(读源码前先记住这几个数)**:

| 宏 | 值(64-bit glibc) | 含义 |
|----|-------------------|------|
| `NBINS` | 128 | smallbin + largebin 的桶数 |
| `MAX_FAST_SIZE` | 160 字节(80 `sizeof(size_t)`) | fastbin 的最大块大小 |
| `DEFAULT_MMAP_THRESHOLD` | 128 × 1024(128 KB) | 超过此大小的请求走 `mmap` |
| `DEFAULT_MMAP_MAX` | 65536 | mmap 区块数上限 |
| `DEFAULT_MTRIM_THRESHOLD` | 128 × 1024 | `malloc_trim` 的默认门槛 |

> 小贴士:`malloc.c` 文件巨大但结构清晰:顶部是宏和数据结构定义(arena、chunk、bins),中段是 `_int_malloc`/`_int_free`(主体),尾部是 `__libc_malloc`/`mallopt`/`malloc_trim`(对外接口)。建议用 Elixir Bootlin 的跳转功能按函数名导航,别从头线性读。

---

## 第二部分 · 实战

读懂源码是手段,用起来才是目的。这一部分给三组**真实可用的命令**:换分配器、调参、抓内存问题。所有命令在主流 Linux 发行版(Ubuntu/Debian/CentOS)上都能跑,所有参数名都对着源码或官方文档核实过。

### B.5 用 LD_PRELOAD 换分配器

这是感受分配器差异最直接的办法:**不重编译程序,在运行时把 `malloc`/`free` 换成另一套实现**。原理是 `LD_PRELOAD` 让你的共享库符号优先于 libc 被链接,jemalloc/tcmalloc/mimalloc 都提供了 `malloc`/`free`/`calloc`/`realloc` 的导出符号,一替换就成了。

#### 装分配器库

```bash
# Debian/Ubuntu
sudo apt install libjemalloc2       # jemalloc
sudo apt install libtcmalloc-minimal4  # tcmalloc(gperftools 版,注意见下)
# mimalloc 通常没有官方包,从源码装:
git clone https://github.com/microsoft/mimalloc && cd mimalloc
mkdir build && cd build && cmake .. && make -j && sudo make install
```

> 注意:发行版仓库里的 `libtcmalloc-minimal4` 通常是 **gperftools 版**(老版,用 `TCMALLOC_*` 环境变量),不是本书讲的 `google/tcmalloc` 新版(用 `MallocExtension` C++ API)。两者性能特性、调参方式不同。要试新版得从 `google/tcmalloc` 源码编译,它主要面向**静态链接 + C++ API**,LD_PRELOAD 场景下用发行版的 gperftools 版更现实。下面的 LD_PRELOAD 例子,`libtcmalloc.so` 指发行版的 gperftools 版;**调参一节(B.6)会分新版/老版分别讲**。

#### 换分配器跑程序

```bash
# 换成 jemalloc
LD_PRELOAD=/usr/lib/x86_64-linux-gnu/libjemalloc.so.2 ./your_app

# 换成 tcmalloc(gperftools 版)
LD_PRELOAD=/usr/lib/x86_64-linux-gnu/libtcmalloc_minimal.so.4 ./your_app

# 换成 mimalloc
LD_PRELOAD=/usr/local/lib/libmimalloc.so ./your_app
```

`.so` 的确切路径因发行版而异,先确认:

```bash
ldconfig -p | grep -E 'jemalloc|tcmalloc|mimalloc'
```

#### 验证替换成功

光跑起来不算数,得确认 `malloc` 真的被替换了。三种办法,任选其一:

```bash
# 方法 1:看进程映射了哪个 malloc 库
LD_PRELOAD=/usr/lib/x86_64-linux-gnu/libjemalloc.so.2 ./your_app &
PID=$!
grep -E 'jemalloc|tcmalloc|mimalloc|libc' /proc/$PID/maps
# 输出里看到 jemalloc 的 .so 被映射,且在你程序代码段之前,就对了

# 方法 2:看动态符号依赖
ldd ./your_app | grep -iE 'malloc'
# 如果 your_app 没直接链接 malloc 库,这里可能看不到;以 maps 为准

# 方法 3:jemalloc 专有——看版本字符串
MALLOC_CONF=stats_print:true ./your_app 2>&1 | head
# 若是 jemalloc,会打印一段 "___ Begin jemalloc statistics ___"
```

#### 对比三套的差异

写一个会反复 malloc/free 的小程序(比如循环分配几十万次不同大小的块),换三套各跑一遍,对比**RSS(常驻内存)**、**延迟(p99)**、**吞吐(ops/s)**:

```bash
# 看 RSS(单位 KB),采样峰值
/usr/bin/time -v ./your_app          # GNU time,会输出 Maximum resident set size

# 或运行中采样
while true; do cat /proc/$PID/status | grep VmRSS; sleep 1; done

# jemalloc 自带统计
MALLOC_CONF=stats_print:true LD_PRELOAD=...libjemalloc.so.2 ./your_app 2>&1 | tee jemalloc-stats.txt
```

典型观察:ptmalloc(baseline)在多线程高并发下延迟方差大(锁争用)、长期运行 RSS 不回落(碎片);换 jemalloc/tcmalloc 后吞吐提升、RSS 更稳。这就是第 1 章讲的"baseline 的两道墙"在现实里的样子。

### B.6 调参表

分配器都有一堆可调旋钮。下面给三套的**真实参数名、默认值、含义**。不确定的不要乱试,先理解每个参数动它换什么。

#### jemalloc:`MALLOC_CONF` 环境变量

jemalloc 用一个环境变量 `MALLOC_CONF` 传所有选项,逗号分隔。解析在 `src/conf.c`(行 474 起的 `CONF_HANDLE_*` 宏,本附录核实过真实存在)。

```bash
# 例:开 profiling + 设 arena 数 + 调 purge 节奏
export MALLOC_CONF="prof:true,prof_prefix:/tmp/jeprof,narenas:16,dirty_decay_ms:5000"
./your_app
```

| 参数 | 默认值 | 含义 | 调它换什么 |
|------|--------|------|-----------|
| `narenas` | `4 * ncpus`(`opt_narenas_ratio`) | arena 数量 | 线程数 ≫ 核数时,加 arena 减少锁争用;arena 越多每线程越可能独享 |
| `lg_tcache_max` | (派生自 size class) | tcache 最大对象的 log2 | 调大:tcache 覆盖更大对象,fast path 命中率升,但 tcache 占内存升 |
| `tcache_max` | (派生) | tcache 最大对象(字节,直接值) | 同上,直接给字节而非 log2 |
| `dirty_decay_ms` | 10000(10s) | dirty extent 的衰减时间常数 | 调小:更积极 purge 归还 OS,RSS 降但可能抖动;调大:RSS 高但延迟稳 |
| `muzzy_decay_ms` | 0 | muzzy(`MADV_FREE` 后)extent 的衰减常数 | 类似 dirty_decay_ms,控制 `MADV_FREE` 那一类的归还节奏 |
| `hpa` | false | 开 jemalloc 的 huge page allocator | 开启后用大页治碎片(对标 tcmalloc HPAA,第 15 章) |
| `prof` | false | 开 heap profiling | 抓内存问题必开(见 B.7) |
| `prof_prefix` | `jeprof.%d.%p.%p` | profile dump 文件前缀 | dump 文件存哪 |
| `lg_prof_sample` | 19(512 KB) | 采样间隔的 log2(每 2^N 字节采一次) | 调小:采样更密,精度高但开销大 |
| `lg_prof_interval` | -1(关) | 每分配 2^N 字节自动 dump 一次 | 周期性 dump,看内存增长轨迹 |
| `prof_leak` | false | 退出时 dump 未释放的(泄漏) | 抓泄漏 |
| `background_thread` | false | 后台线程做 purge/hugify | 开了 decay 才有意义的后台执行者 |
| `max_background_threads` | (派生) | 后台线程数上限 | 配合 background_thread |
| `stats_print` | false | 退出时打印完整统计 | 调试/对比用 |
| `abort` | false | 遇到配置错误时 abort | 防止配置写错静默生效 |

> 完整选项见 jemalloc 官方手册:[jemalloc(3)](https://jemalloc.net/jemalloc.3.html),以及源码 `src/conf.c` 行 556~1050 的 `CONF_HANDLE_*`。

#### tcmalloc:新老两套机制,**别搞混**

这是最容易踩的坑。tcmalloc 有两套差别很大的调参机制,取决于你用的是哪个版本:

**A. gperftools 版(发行版 `libtcmalloc.so`,LD_PRELOAD 场景)** —— 用 `TCMALLOC_*` 环境变量:

| 环境变量 | 默认值 | 含义 |
|---------|--------|------|
| `TCMALLOC_SAMPLE_PARAMETER` | 0(关) | 采样间隔(字节),用于 profiling,524288 约 512KB 采一次 |
| `TCMALLOC_MAX_TOTAL_THREAD_CACHE_BYTES` | 16 MB × ncpu | 所有线程缓存总和上限 |
| `TCMALLOC_CACHE_RELEASE_RATE` | 1.0 | 后台线程向 OS 归还内存的速率倍数 |

这些是老版环境变量,在新版 `google/tcmalloc` 里**已不再用**。如果你 LD_PRELOAD 的是发行版的 gperftools 版,这些才生效。

**B. `google/tcmalloc` 新版(本书主角,源码 `../tcmalloc/`)** —— 用 **C++ API** 和 **ABSL flag**,不用环境变量。这是新版的设计取向:静态链接、编译期/启动期配置,运行时通过 `MallocExtension` 控制。

主要 API(声明在 `tcmalloc/malloc_extension.h`,实现核实于 `malloc_extension.cc:563` 的 `GetNumericProperty`):

| API | 作用 |
|-----|------|
| `tcmalloc::MallocExtension::SetMaxPerCpuCacheSize(n)` | 设每个 CPU 缓存的上限(新版默认 per-CPU 模式) |
| `tcmalloc::MallocExtension::SetMaxTotalThreadCacheBytes(n)` | 设所有线程缓存总和上限(legacy per-thread 模式) |
| `tcmalloc::MallocExtension::ReleaseMemoryToSystem(n)` | 请求归还 n 字节给 OS |
| `tcmalloc::MallocExtension::ProcessBackgroundActions()` | 驱动后台任务(purge、hugify、缓存回收),需周期调用 |
| `tcmalloc::MallocExtension::ReleaseCpuMemory(cpu)` | 释放指定 CPU 上搁浅的缓存(线程已迁移走) |
| `GetNumericProperty("tcmalloc.xxx")` | 读取内部统计(如 `tcmalloc.pageheap_free_bytes`) |

ABSL flag(编译期/启动期 `--flag=value` 传):

| Flag | 含义 |
|------|------|
| `tcmalloc_max_per_cpu_cache_size` | 每 CPU 缓存上限 |
| `tcmalloc_sample_parameter` | 采样间隔(profiling) |

> 详见 [google/tcmalloc Tuning Guide](https://google.github.io/tcmalloc/tuning.html)(本附录核实过)。新版的核心建议:**逻辑页大小**(4/8/32/256 KiB,编译期选)、**per-CPU 缓存大小**(`SetMaxPerCpuCacheSize`)、**归还 OS 速率**(`ReleaseMemoryToSystem` + 后台 `ProcessBackgroundActions`)。三者都不是"越大越好",各有利弊,官方文档明确写了。

#### ptmalloc:`mallopt` 运行时调参

ptmalloc 通过 `mallopt(param, value)` 函数调参,参数用宏编号(核实于 [man mallopt(3)](https://man7.org/linux/man-pages/man3/mallopt.3.html) 和 glibc 源码):

```c
#include <malloc.h>
mallopt(M_TRIM_THRESHOLD, 64 * 1024);   // 调 trim 门槛
mallopt(M_MMAP_THRESHOLD, 256 * 1024);  // 调 mmap 门槛
mallopt(M_MXFAST, 0);                    // 关 fastbin(调试用)
```

| 参数宏 | 编号 | 默认值 | 含义 |
|--------|------|--------|------|
| `M_MXFAST` | 1 | 64(32-bit)/ 128(64-bit)字节 | fastbin 最大块大小;设 0 关 fastbin |
| `M_TRIM_THRESHOLD` | -1 | 128 KB | `malloc_trim` 归还门槛;-1 关 trimming |
| `M_TOP_PAD` | -3 | 128 KB | 扩展/收缩堆时的填充量 |
| `M_MMAP_THRESHOLD` | -2 | 128 KB | 超过此大小走 mmap;glibc 会**动态调整**这个值 |
| `M_MMAP_MAX` | -4 | 65536 | mmap 区块数上限;0 禁用 mmap |
| `M_CHECK_ACTION` | 3 | 3 | 堆损坏时的行为(abort / 打印 / 跳过) |

> 注意:`M_MMAP_THRESHOLD` 和 `M_TRIM_THRESHOLD` 在 glibc 里是**动态自适应**的——首次大块 mmap 后,阈值会被向上调整,避免反复 mmap/munmap。手动 `mallopt` 设的是初始值和上限。详见 man page。
>
> 主动归还内存:`malloc_trim(0)` 让 ptmalloc 把所有能还的空闲页还给 OS(配合 `M_TRIM_THRESHOLD`),常用于长跑服务定期调用降 RSS。

### B.7 抓内存问题

这是分配器实战的重头戏。三类常见问题:**泄漏**、**碎片/占用高**、**性能异常**,各有抓法。

#### 工具一:jemalloc heap profiling(`prof` + `jeprof`)

jemalloc 自带采样式 heap profiler,是抓内存问题最顺手的工具。流程(本附录核实过 [jemalloc(3)](https://jemalloc.net/jemalloc.3.html) 和多个实战案例):

```bash
# 1. 开 profiling 启动程序(dump 文件前缀 /tmp/jeprof)
MALLOC_CONF="prof:true,prof_prefix:/tmp/jeprof,lg_prof_sample:17" \
  LD_PRELOAD=/usr/lib/x86_64-linux-gnu/libjemalloc.so.2 ./your_app

# 2. 运行中触发 dump(向进程发信号,SIGUSR2 默认触发 dump)
kill -USR2 $PID
# 或周期性自动 dump:加 lg_prof_interval:N(每分配 2^N 字节 dump 一次)

# 3. 退出时 dump 泄漏(未释放的)
MALLOC_CONF="prof:true,prof_leak:true,prof_prefix:/tmp/jeprof,lg_prof_sample:17" \
  LD_PRELOAD=...libjemalloc.so.2 ./your_app

# 4. 用 jeprof 解析 dump(看调用栈 + 占用)
jeprof --text ./your_app /tmp/jeprof.*.heap          # 文本报告
jeprof --svg ./your_app /tmp/jeprof.*.heap > prof.svg  # 火焰图(SVG)
jeprof --web ./your_app /tmp/jeprof.*.heap            # 开浏览器看
```

`--text` 输出是按**占用字节**排序的调用栈列表,排前面的就是吃内存最多的分配路径。`--svg` 出火焰图,定位热点最直观。

> 关键参数:`lg_prof_sample`(默认 19 = 512KB,即每分配 512KB 采一次;抓精细问题调到 17 = 128KB);`prof_prefix`(dump 文件前缀,必设,否则不知道 dump 去哪了)。注意采样是**概率性**的,小对象可能采不到,大对象更容易被采——这正是第 18 章讲的几何采样的用意。

#### 工具二:tcmalloc 采样 profiling

tcmalloc 也支持采样 profiling,通过 `TCMALLOC_SAMPLE_PARAMETER`(gperftools 版)或 `tcmalloc_sample_parameter`(新版 flag)开,然后用 **pprof**(Google 的 profiling 工具)解析:

```bash
# gperftools 版
TCMALLOC_SAMPLE_PARAMETER=524288 LD_PRELOAD=...libtcmalloc.so.4 ./your_app
# 运行中用 pprof 抓(需要符号)
pprof --web ./your_app http://localhost:PORT/pprof/heap
# 或从 heap 文件解析
pprof --text ./your_app heap.prof
```

新版 `google/tcmalloc` 的 profiling 主要通过 C++ API(`MallocExtension`)或链接 Telemetry,LD_PRELOAD 场景用发行版的 gperftools 版更现实。

#### 工具三:看 RSS、对比分配器、抓碎片

不是所有问题都要上 profiler。一套排查清单:

**问题:RSS 居高不下,但程序逻辑上不该占这么多。**

```bash
# 1. 盯 RSS 变化曲线
while true; do awk '/VmRSS/{print $2}' /proc/$PID/status; sleep 5; done > rss.log

# 2. 看是不是分配器的锅——换 jemalloc/tcmalloc 重跑,对比 RSS 峰值
LD_PRELOAD=...libjemalloc.so.2 ./your_app    # 记录 RSS
LD_PRELOAD=...libtcmalloc.so.4 ./your_app    # 记录 RSS
./your_app                                    # baseline(ptmalloc)
# 如果换分配器 RSS 显著下降,基本就是 ptmalloc 碎片问题(第 13~14 章)

# 3. 强制归还内存看 RSS 能不能掉
# ptmalloc:程序里周期调 malloc_trim(0)
# jemalloc:调小 dirty_decay_ms / muzzy_decay_ms,开 background_thread
# tcmalloc:调 ReleaseMemoryToSystem + ProcessBackgroundActions

# 4. jemalloc 开 hpa 治碎片(第 15 章)
MALLOC_CONF="hpa:true,dirty_decay_ms:10000" LD_PRELOAD=...libjemalloc.so.2 ./your_app
```

**问题:怀疑内存泄漏(RSS 单调增长不回落)。**

```bash
# 1. jemalloc prof + prof_leak,退出时 dump 未释放的
MALLOC_CONF="prof:true,prof_leak:true,prof_prefix:/tmp/leak" \
  LD_PRELOAD=...libjemalloc.so.2 ./your_app
jeprof --text ./your_app /tmp/leak.*.heap   # 看退出时还活着的分配

# 2. 周期 dump,对比两个时间点的差量,看哪条调用栈在涨
kill -USR2 $PID; sleep 300; kill -USR2 $PID   # 两个 dump
jeprof --base=/tmp/jeprof.A.heap --text ./your_app /tmp/jeprof.B.heap
# --base 减去基线,只看增量——增量的就是泄漏点
```

**问题:性能异常(malloc 慢、延迟方差大)。**

```bash
# 1. jemalloc 看锁争用统计
MALLOC_CONFIG="stats_print:true" LD_PRELOAD=...libjemalloc.so.2 ./your_app 2>&1 | \
  grep -E 'mutex|arena'
# 看 arena/bin 的锁等待时间,争用高的就是瓶颈

# 2. tcmalloc 看 fast path 命中率
# (通过 GetNumericProperty 读 tcmalloc.cpu_cache_hits 等)

# 3. 换 per-CPU(tcmalloc 新版)看多核争用是否消失
```

#### 排查速查表

| 症状 | 第一步 | 第二步 |
|------|--------|--------|
| RSS 单调涨不回落 | jemalloc `prof:true,prof_leak:true` 退出 dump | jeprof 看存活分配调用栈 |
| RSS 稳但偏高 | 换 jemalloc/tcmalloc 对比峰值 | 调 dirty_decay_ms / 开 hpa / malloc_trim |
| malloc 延迟方差大 | jemalloc stats 看 mutex 争用 | 加 arena / 换 per-CPU / 查长锁路径 |
| 偶发 SIGSEGV | 怀疑堆溢出/UAF | tcmalloc guarded page / jemalloc san / AddressSanitizer |
| 启动慢、初始化报错 | 查 MALLOC_CONF 拼写 | `MALLOC_CONF="abort:true"` 让它对错误配置直接 abort |

---

## 收束

这份附录到此为止。它不教你分配器原理(那是前 21 章的事),只教你**怎么真的去碰那些代码、怎么真的让它们为你工作**。

回顾两块:

1. **读源码**:从 `malloc`/`free` 入口追下去(tcmalloc 的 `AllocateFast`/`AllocateSlow`、jemalloc 的 `imalloc_fastpath`、mimalloc 的 default heap、ptmalloc 的 `_int_malloc`),顺着 miss 路径一层层往下,每到一个文件问一句"它在三层快慢道的哪一层"。mimalloc 代码最干净,适合入门;tcmalloc 新版最复杂(尤其 rseq 和 HPAA);ptmalloc 单文件巨石,用 Elixir Bootlin 跳着读。
2. **用起来**:`LD_PRELOAD` 换分配器(验证看 `/proc/$PID/maps`);调参——jemalloc 用 `MALLOC_CONF`(选项在 `src/conf.c`),tcmalloc 新版用 `MallocExtension` C++ API(发行版 gperftools 版才用 `TCMALLOC_*` 环境变量,**别搞混**),ptmalloc 用 `mallopt`;抓内存问题——jemalloc `prof` + `jeprof` 是最顺手的 heap profiler,tcmalloc 用 pprof,碎片/泄漏/RSS 排查有现成清单。

把这份附录当前 21 章的"动手配套"。读懂原理之后,真的去 clone 一份 tcmalloc,打开 `tcmalloc.cc:1198`,顺着 `AllocateFast` 追一遍;真的写个小程序,`LD_PRELOAD` 换三套分配器,看 RSS 和延迟的差异。那一刻,前面 21 章讲的"为什么 malloc 还要自己写",会从纸面知识变成你的肌肉记忆。
