# 附录 B · 源码阅读路线与延伸

> 本附录是全书的**参考性清单**。主体二十章把每个机制的"为什么 + 怎么做"讲透了,但读者读完想自己钻进 mm/ 源码、想观测 mm 在运行、想调参、想和别的系统对照时,需要一份地图。本附录给四份清单:① mm/ 阅读地图(按本书篇章顺序,列每篇对应核心 .c/.h + 建议阅读顺序);② 观测 mm 运行的工具(`/proc`/`ftrace`/`perf`/`crash`/`bpftrace`,每个一两句看什么);③ 关键调参(`vm.*` sysctl + THP + memcg + oom_score_adj);④ 延伸阅读(BSD/Mach VM 对照、Linux mm 演进、推荐书、LPC/LSF/MM talks)。
>
> 本附录是参考性材料,不重复二十章的讲解,只给清单 + 简短说明。读者可以把它当"读完本书后的下一步指南"。

## B.1 mm/ 阅读地图

按本书篇章顺序,列出每篇对应的核心源码文件 + 建议阅读顺序。所有文件名都用 `ls`/`Glob` 核实过存在于本地 Linux 6.9 源码 `mm/` 和 `include/linux/` 下。

| 篇 | 章节 | 核心源码文件 | 阅读顺序提示 |
|----|------|------------|------------|
| **P0 开篇** | P0-01 | [`include/linux/mm_types.h`](../linux/include/linux/mm_types.h)(`struct page`/`struct folio`/`struct mm_struct`) | 先读 `struct page`(P0-01 引用的 L74),理解账本;再读 `struct mm_struct`(进程地址空间的根) |
| **P1 buddy** | P1-02 物理内存模型 | [`include/linux/mmzone.h`](../linux/include/linux/mmzone.h)(`struct zone`/`struct pglist_data`/`enum zone_type`/`struct per_cpu_pages`)、[`mm/page_alloc.c`](../linux/mm/page_alloc.c) | 先读 `enum zone_type`(L727)、`struct zone`(L822,`free_area[]`/`_watermark[]`/`per_cpu_pageset`),`struct per_cpu_pages`(L687);`struct pglist_data` NUMA 拓扑 |
| | P1-03 buddy 算法 | [`mm/page_alloc.c`](../linux/mm/page_alloc.c) | 核心函数:`__free_one_page`(L765,合并伙伴)、`__rmqueue`(L2087,分配摘页)、`free_area[]` 数据结构 |
| | P1-04 快慢路径 | [`mm/page_alloc.c`](../linux/mm/page_alloc.c)、[`include/linux/gfp.h`](../linux/include/linux/gfp.h) | `get_page_from_free_area`(L709,快路径)、`__alloc_pages_slowpath`(L4046,慢路径)、GFP 标志(`__GFP_*`) |
| | P1-05 释放 + pageset | [`mm/page_alloc.c`](../linux/mm/page_alloc.c) | `free_unref_page`(L2479,进 pcp)、`pcp_batch_high_lock`、`pcpu_task_pin`(L117,关抢占) |
| | P1-06 migrate/CMA | [`mm/page_alloc.c`](../linux/mm/page_alloc.c)、[`mm/page_isolation.c`](../linux/mm/page_isolation.c)、[`mm/cma.c`](../linux/mm/cma.c)、[`mm/cma.h`](../linux/mm/cma.h) | migrate types 枚举(`MIGRATE_MOVABLE` 等)、`set_pageblock_migratetype`、CMA 预留和分配 |
| **P2 slab** | P2-07 kmem_cache | [`mm/slub.c`](../linux/mm/slub.c)、[`mm/slab.h`](../linux/mm/slab.h)、[`mm/slab_common.c`](../linux/mm/slab_common.c) | 先读 [`struct kmem_cache`](../linux/mm/slub.c)、`struct slab`(slab 是 folio 的视图)、对象布局;再读 [`allocate_slab`](../linux/mm/slub.c#L2322) 摆对象 |
| | P2-08 per-cpu partial | [`mm/slub.c`](../linux/mm/slub.c) | `struct kmem_cache_cpu`(L384,freelist/tid/slab/partial)、`struct kmem_cache_node`(L425,per-node partial)、`___slab_alloc`(L3376,慢路径)、`slab_alloc_node`(L3826,快路径入口) |
| | P2-09 kmalloc | [`mm/slab_common.c`](../linux/mm/slab_common.c) | `kmalloc_info[]`(L777,size class 档位表)、`kmalloc_caches[]`(L657,二维数组)、`create_kmalloc_caches`、`__kmalloc`(L3983) |
| | P2-10 对照 | (P2-10 是对照章,无新源码) | 复读 slub.c + 参考 tcmalloc/jemalloc 文档 |
| **P3 vmalloc/percpu** | P3-11 | [`mm/vmalloc.c`](../linux/mm/vmalloc.c)、[`mm/percpu.c`](../linux/mm/percpu.c)、[`mm/percpu-internal.h`](../linux/mm/percpu-internal.h)、[`mm/percpu-vm.c`](../linux/mm/percpu-vm.c)、[`include/linux/percpu.h`](../linux/include/linux/percpu.h)、[`include/linux/percpu-defs.h`](../linux/include/linux/percpu-defs.h) | vmalloc:`__vmalloc_node_range`、`alloc_vmap_area`、`vmap_pte_range`(L93);percpu:`pcpu_alloc`、`this_cpu_ptr` 宏展开 |
| **P4 用户地址空间** | P4-12 VMA/mmap | [`mm/mmap.c`](../linux/mm/mmap.c)、[`mm/vma.c`](../linux/mm/vma.c)(6.x 拆出)、[`mm/mmap_lock.c`](../linux/mm/mmap_lock.c)、[`include/linux/mm.h`](../linux/include/linux/mm.h)(`struct vm_area_struct`) | `SYSCALL_DEFINE1(brk)`(L178)、`do_mmap`(L1214)、`mmap_region`(L2715)、`SYSCALL_DEFINE6(mmap_pgoff)`(L1438);VMA 用 maple tree |
| | P4-13 页表/mmu_gather | [`mm/memory.c`](../linux/mm/memory.c)、[`mm/pgtable-generic.c`](../linux/mm/pgtable-generic.c)、[`mm/mmu_gather.c`](../linux/mm/mmu_gather.c)、`arch/x86/include/asm/pgtable.h`(架构相关) | 多级页表 walk:`pgd_offset`→`p4d_offset`→`pud_offset`→`pmd_offset`→`pte_offset`;`mmu_gather` 批量收集 TLB 刷新 |
| | P4-14 缺页 | [`mm/memory.c`](../linux/mm/memory.c) | `handle_mm_fault`、`do_anonymous_page`(L108 声明)、`do_wp_page`(COW)、`do_read_fault`/`do_fault`(文件页)、`alloc_anon_folio` |
| | P4-15 rmap/gup | [`mm/rmap.c`](../linux/mm/rmap.c)、[`mm/gup.c`](../linux/mm/gup.c)、[`mm/page_vma_mapped.c`](../linux/mm/page_vma_mapped.c) | rmap:`try_to_unmap_one`(L1616)、`page_add_new_anon_rmap`、`rmap_walk_anon`(L2569);gup:`get_user_pages`、`faultin_page`(L919)、`check_vma_flags`(L1032) |
| **P5 回收** | P5-16 watermark/kswapd | [`mm/vmscan.c`](../linux/mm/vmscan.c)、[`include/linux/mmzone.h`](../linux/include/linux/mmzone.h) | `struct zone._watermark[]`、WMARK_HIGH/LOW/MIN、`kswapd`(L7097)、`balance_pgdat`(L6764) |
| | P5-17 LRU/workingset/vmscan | [`mm/vmscan.c`](../linux/mm/vmscan.c)、[`mm/workingset.c`](../linux/mm/workingset.c)、[`mm/swap.c`](../linux/mm/swap.c)、[`include/linux/mm_inline.h`](../linux/include/linux/mm_inline.h) | `shrink_folio_list`(L1011,回收单页)、`shrink_lruvec`/`shrink_node`、LRU active/inactive、workingset refault |
| | P5-18 compaction | [`mm/compaction.c`](../linux/mm/compaction.c) | `compact_zone`(L2525)、迁移扫描器(migrate scanner)+ 空闲扫描器(free scanner)、`migrate_pages` |
| | P5-19 swap/OOM | [`mm/swapfile.c`](../linux/mm/swapfile.c)、[`mm/swap_state.c`](../linux/mm/swap_state)、[`mm/page_io.c`](../linux/mm/page_io.c)、[`mm/zswap.c`](../linux/mm/zswap.c)、[`mm/oom_kill.c`](../linux/mm/oom_kill.c) | swap:`get_swap_page`、`swap_writepage`、`swap_cache`;zswap 压缩;OOM:`oom_badness`(L202)、`select_bad_process`(L365)、`__oom_kill_process`(L917)、`oom_kill_process`(L1013) |
| **P6 进阶** | P6-20 大页/KSM/memcg/NUMA | [`mm/hugetlb.c`](../linux/mm/hugetlb.c)、[`mm/huge_memory.c`](../linux/mm/huge_memory.c)、[`mm/khugepaged.c`](../linux/mm/khugepaged.c)、[`mm/ksm.c`](../linux/mm/ksm.c)、[`mm/memcontrol.c`](../linux/mm/memcontrol.c)、[`mm/mempolicy.c`](../linux/mm/mempolicy.c) | THP:`transparent_hugepage_flags`(huge_memory.c)、`khugepaged` 守护进程;KSM:`ksm_scan`、`cmp_and_merge_page`;memcg:`mem_cgroup`、`try_charge`、`memory.current`/`memory.max`;NUMA:`mpol_new`、`do_mbind`、`vma_migratable` |

### 阅读顺序建议

如果你刚读完本书,想顺 mm/ 源码,推荐这条顺序(对应二十章主线):

1. **账本地基**:`include/linux/mm_types.h`(`struct page`/`struct folio`/`struct mm_struct`)→ `include/linux/mmzone.h`(`struct zone`/`struct pglist_data`)。
2. **buddy 分配**:`mm/page_alloc.c`(`__rmqueue` → `get_page_from_free_area` → `__alloc_pages_slowpath` → `free_unref_page` → `__free_one_page`)。
3. **slab 切对象**:`mm/slub.c`(`kmem_cache_create` → `slab_alloc_node` → `___slab_alloc` → `allocate_slab` → `alloc_slab_page` → 回到 buddy)。
4. **用户地址空间**:`mm/mmap.c`(`brk`/`mmap_pgoff` → `do_mmap` → `mmap_region` 建 VMA)→ `mm/memory.c`(`handle_mm_fault` → `do_anonymous_page`/`do_wp_page` → `alloc_pages` → 回到 buddy)。
5. **回收**:`mm/vmscan.c`(`kswapd` → `balance_pgdat` → `shrink_node` → `shrink_lruvec` → `shrink_folio_list`)→ `mm/compaction.c`(`compact_zone`)→ `mm/oom_kill.c`(`oom_badness` → `select_bad_process` → `__oom_kill_process`)。

这条顺序正好对应"分配出去又收回来"的主线——你能看到两次"回到 buddy"(slab 找 buddy 要页、缺页找 buddy 要页),和一次"从 buddy 收回"(vmscan 把页归还 buddy 或换 swap)。

---

## B.2 观测 mm 运行

观测是理解 mm 的关键——光读代码不知道实际行为,看 `/proc`、抓 ftrace、用 crash 分析 dump,mm 才是活的。下表列核心工具 + 看什么 + 对应章节。

### /proc 类(读文件,最简单)

| 工具 / 文件 | 看什么 | 对应章节 |
|------------|--------|---------|
| `/proc/meminfo` | 系统级内存总览:`MemTotal`/`MemFree`/`MemAvailable`/`Cached`/`Buffers`/`SwapTotal`/`SwapFree`/`Slab`/`SReclaimable`/`SUnreclaim`/`AnonPages`/`Mapped`/`Shmem`/`KReclaimable`/`HugePages_*` | P0-01 全貌,P2 slab 看 `Slab`,P5 回收看 `MemAvailable`/`SwapFree` |
| `/proc/buddyinfo` | 各 node/zone 各 order 的空闲页数(行=zone,列=order 0~10)。直接看 buddy 的 `free_area[]` 实况——碎片化时高 order 列全是 0 | P1-03 buddy,P1-06 碎片化诊断 |
| `/proc/slabinfo` | 所有 slab cache 列表:active/total objects、对象大小、每 slab 对象数、order、slabs/sizes。配合 `slabtop` 排序看哪些 cache 最吃内存 | P2-07/P2-09 slab |
| `/proc/<pid>/maps` | 一个进程的虚拟地址空间布局:每段 VMA 的起止地址、权限、映射对象(`[heap]`/`[stack]`/文件名)。用户态分配器的批发结果在这里可见 | P4-12 VMA/mmap |
| `/proc/<pid>/smaps` | maps 的详细版:每段 VMA 的 `Rss`/`Pss`/`Shared_Clean`/`Private_Dirty`/`Anonymous`/`Swap`/`KernelPageSize`/`THPeligible`。PSS 是诊断进程内存占用的金标准 | P4-12/P4-14,THP 看 `THPeligible`(P6-20) |
| `/proc/<pid>/smaps_rollup` | smaps 的聚合版(整进程一行总览),给进程级内存账单 | P4-12 |
| `/proc/<pid>/oom_score` | 进程的 OOM 分数(0~1000),分数高的优先被 OOM 杀。配合 `/proc/<pid>/oom_score_adj` 调整 | P5-19 OOM |
| `/proc/vmstat` | mm 全局计数器:`pgalloc`/`pgfree`/`pgmajfault`/`pgsteal`/`pgscan_kswapd`/`pgscan_direct`/`compact_stall`/`compact_success`/`oom_kill`/`thp_fault_alloc`/`thp_collapse_alloc`/`swpin`/`swpout` 等。看回收/规整/大页活动量 | P5 全部,P6-20 THP(看 `thp_*`) |
| `/proc/zoneinfo` | 各 zone 详细:每 zone 的 `free`/`min`/`low`/`high`(watermark)、`spanned`/`present`/`managed`、按 order 的空闲页数(类似 buddyinfo 但更全) | P1-02 zone,P5-16 watermark |
| `/proc/<pid>/status` | 进程级:`VmPeak`/`VmSize`/`VmRSS`/`VmData`/`VmStk`/`VmExe`/`VmLib`/`VmPTE`/`VmSwap`/`Threads` | P4-12 |
| `/proc/<pid>/numa_maps` | 进程的 NUMA 内存分布:每段 VMA 在哪个 node、多少页 | P6-20 NUMA |
| `/sys/kernel/mm/transparent_hugepage/enabled` | THP 当前模式:`always`/`madvise`/`never` | P6-20 THP |
| `/sys/kernel/mm/transparent_hugepage/defrag` | THP 缺页时是否触发 compaction:`always`/`defer`/`defer+madvise`/`madvise`/`never` | P5-18/P6-20 |
| `/sys/devices/system/node/node*/meminfo` | NUMA 每 node 内存总览 | P6-20 NUMA |

### ftrace / perf / bpftrace(动态观测)

| 工具 | 看什么 | 对应章节 |
|------|--------|---------|
| `ftrace`(tracepoint) | mm 关键 tracepoint:`vmscan:mm_vmscan_*`(回收决策)、`compaction:mm_compaction_*`(规整)、`kmem:mm_page_alloc`/`mm_page_free`(buddy 分配释放)、`kmem:mm_page_alloc_extfrag`(碎片跨类型分配)、`exceptions:page_fault_user/kernel`(缺页)、`oom:oom_*`(OOM)。`trace-cmd record -e 'vmscan:*' -e 'compaction:*'` 抓回收链路 | P1/P5 全部 |
| `perf stat -e page-faults,major-faults,minor-faults` | 进程的缺页率(尤其 major-faults = 走磁盘的,minor = 纯内存建映射的) | P4-14 |
| `perf stat -e 'kmem:mm_page_alloc','kmem:mm_page_free'` | 进程触发的 buddy 分配/释放频率 | P1 |
| `perf record -e 'vmscan:*'` + `perf report` | 回收热点函数 profiling | P5 |
| `perf mem record/report` | 内存访问 profiling,看 cache miss / TLB miss | P4-13/P6-20 |
| `bpftrace` | 自定义探针。例:`bpftrace -e 'tracepoint:vmscan:mm_vmscan_lru_shrink_inactive { @[comm] = count(); }'` 看谁触发回收。`bpftrace -e 'kprobe:__alloc_pages_slowpath { printf("slow path pid=%d\n", pid); }'` 抓慢路径 | 全书 |
| `crash`(dump 分析) | 分析内核 crash dump:看 panic 时的内存状态、buddy、slab、页表、所有进程的 VMA。配合 `kdump` 抓 panic 现场。`crash> kmem -p`(buddy)、`crash> kmem -s`(slab)、`crash> vm <pid>`(进程 VMA)、`crash> pt <addr>`(页表 walk) | 全书(故障诊断) |

---

## B.3 关键调参

下表列 mm 的核心 sysctl 和参数。所有参数名都用 `grep` 在源码里核实过(`sysctl_overcommit_memory`/`min_free_kbytes`/`watermark_scale_factor`/`vm_swappiness`/`vm_dirty_ratio` 等)。

| 参数 | 作用 | 对应章节 |
|------|------|---------|
| `vm.swappiness`(默认 60,0~200) | anon 页换 swap 的倾向(0 = 尽量不换 anon 优先回收 file,100 = anon/file 同等,>100 倾向 anon)。源码 `int vm_swappiness = 60`(vmscan.c:194) | P5-17/P5-19 |
| `vm.watermark_scale_factor`(默认 10,0~10000) | watermark 之间的距离比例(控制 kswapd 何时唤醒、唤醒多激进)。值大 = 更早更激进回收。源码 `static int watermark_scale_factor = 10`(page_alloc.c:292) | P5-16 |
| `vm.min_free_kbytes` | 每个 zone 的 `WMARK_MIN`(最低水位),也是 direct reclaim 触发点。设大给回收留更多余地,但浪费内存。源码 `int min_free_kbytes = 1024`(page_alloc.c:289) | P5-16 |
| `vm.dirty_ratio`(默认 20)/ `vm.dirty_background_ratio`(默认 10) | 脏页占比阈值。超过 `dirty_background_ratio` 后台回写开始,超过 `dirty_ratio` 写进程阻塞同步回写。源码 `vm_dirty_ratio = 20`(page-writeback.c:91)、`dirty_background_ratio = 10`(page-writeback.c:74) | P5-19 文件页回写 |
| `/sys/kernel/mm/transparent_hugepage/enabled`(`[always]`/`madvise`/`never`) | THP 是否启用。`madvise` = 只对显式 `madvise(MADV_HUGEPAGE)` 的区域启用 | P6-20 |
| `/sys/kernel/mm/transparent_hugepage/defrag` | THP 缺页时是否触发 compaction 规整:`always`/`defer`/`defer+madvise`/`madvise`/`never` | P5-18/P6-20 |
| `vm.overcommit_memory`(默认 0=HEURISTIC,1=ALWAYS,2=NEVER) | 是否允许 overcommit(分配超过物理+swap 的虚拟内存)。源码 `sysctl_overcommit_memory = OVERCOMMIT_GUESS`(util.c:831) | P4-12 |
| `vm.overcommit_ratio`(默认 50) | 配合 `overcommit_memory=2`,允许分配 = (swap + 物理内存 × ratio%)。源码 `sysctl_overcommit_ratio = 50`(util.c:832) | P4-12 |
| `vm.max_map_count`(默认 65536) | 一个进程最多多少 VMA(mmap 区间)。源码 `sysctl_max_map_count = DEFAULT_MAX_MAP_COUNT`(util.c:834) | P4-12 |
| `vm.zone_reclaim_mode`(默认 0=关) | NUMA zone 内存紧张时是否回收本 node 而非跨 node 分配。源码在 `kernel/sysctl.c` 或 `mm/vmscan.c` | P6-20 NUMA |
| `vm.percpu_pagelist_high_frac`(默认 0=用默认) | per-cpu pageset 缓存上限比例。设大 = 更少 zone 锁竞争,但缓存占用更多 | P1-05 |
| `vm.stat_interval`(默认 1 秒) | `vm_stat` 从 per-cpu flush 到全局的间隔 | P1-02 |
| `/proc/<pid>/oom_score_adj`(-1000~1000) | 手动调进程的 OOM 分数(-1000 = 永不被杀,1000 = 最优先杀)。源码 `p->signal->oom_score_adj`(oom_kill.c:219) | P5-19 OOM |
| `/proc/<pid>/oom_adj`(-17~15,已废弃) | `oom_score_adj` 的老接口(向后兼容),`-17` = 不杀 | P5-19 |
| `memory.max`(cgroup v2) | memcg 限额(字节)。进程组超过限额触发 memcg OOM(只杀组内进程)。源码 `mem_cgroup` | P6-20 memcg |
| `memory.high`(cgroup v2) | memcg 软限,超过后该组进程分配被减速(但不立刻杀) | P6-20 memcg |

### 调参的一般原则

- **生产环境**一般不动 `vm.swappiness`/`watermark_scale_factor`/`min_free_kbytes`——默认值是经过大量负载调过的甜点。
- **延迟敏感型**(数据库、消息队列):考虑 `vm.swappiness=10`(尽量不换出 anon,避免 swap 抖动)、`vm.watermark_scale_factor=100`(更早回收,direct reclaim 少)、`transparent_hugepage/enabled=madvise`(THP 只给显式请求的程序,避免 khugepaged 抢 CPU)。
- **内存富余、要大页**:`transparent_hugepage/enabled=always` + `defrag=madvise`(只对 madvise 区域规整,避免全局 compaction 卡顿)。
- **OOM 保护关键进程**:`echo -1000 > /proc/<critical_pid>/oom_score_adj`,让 OOM 永远不杀它。
- **调参之前先观测**(B.2 的工具):不观测就调参是盲调,大概率帮倒忙。

---

## B.4 延伸阅读

### B.4.1 与 BSD/Mach VM 的对照

Linux mm 不是孤岛,其他 UNIX 系系统也解同样的问题。对照阅读能看清"哪些是普适解、哪些是 Linux 选择":

- **BSD(FreeBSD)VM**:Unified Buffer Cache(UBC)把文件缓存和空闲内存统一——不像 Linux 把 page cache 和 buddy 的空闲区分开,而是动态伸缩(file cache 用不完的页自动给进程)。VFS 层和 VM 层耦合更紧。VM 抽象用 `vmspace` + `vm_map` + `vm_object`(类似 Linux 的 `mm_struct` + VMA + page cache 的 folio),`vm_map_entry` 对应 VMA,`vm_object` 对应 page cache / swap 对象。缺页用 `vm_fault`。看《The Design and Implementation of the FreeBSD Operating Service》(McKusick et al.)第 5 章 VM。
- **Mach VM**:CMU Mach 的 VM 是现代 UNIX VM 的鼻祖(Linux、BSD 都受影响)。核心抽象 `vm_map`(进程地址空间)+ `vm_object`(内存对象,带 shadow chain 做写时复制)+ `pmap`(物理页表抽象,MD 机器相关)。Mach 的 `memory_object` 接口把 swap/file 都当"后备存储"统一处理——Linux 的 swap cache/file cache 概念也类似。看 CMU Mach 3.0 的 paper 和《A Comparison of OS/2, Mach, and Linux Memory Management》类文章。
- **Windows NT/2000 VM**:`_VAD`(Virtual Address Descriptor)对应 VMA,`PFN_database` 对应 `struct page`,工作集(working set)对应 active LRU。看《Windows Internals》(Russinovich et al.)Part 2 的 Memory Management 章。

对照的洞察:**各家 VM 的核心抽象惊人地同构**——进程地址空间(区段表)、物理页描述符、缺页处理、page cache。差别在细节(Linux 的 buddy + slab 组合,FreeBSD 的 UBC,Mach 的 memory_object 抽象),本质都是"分配 + 映射 + 回收"三件事的不同切法。学透 Linux mm 后看 BSD/Mach,你会发现它们在解同一个问题,只是术语和工程取舍不同。

### B.4.2 Linux mm 的演进(近年的大变化)

Linux mm 在 5.x~6.x 有几波大改动,本书基于 6.9,以下是近年值得关注的演进:

- **folio**(5.8 起,Matthew Wilcox):把"head page 概念扶正"。之前 `struct page` 把一个复合页(compage / THP / hugetlb)的元数据散在 head 和 tail,用 union 挪用字段导致 tail page 字段含义模糊,bug 频发。folio 是"一个内存分配的单位"(可以是单页,也可以是 2MB 大页),元数据集中在 head。本书 P1-02/P1-05 已用 folio。仍在推进中——`page_alloc.c` 里很多 API 同步有 `_folio_` 版本(`__folio_alloc`/`alloc_anon_folio`)。
- **maple tree**(5.16 起,Liam Howlett):替代 VMA 的老红黑树 + `rb_augmented`(augmented rbtree 维护区间)。maple tree 是 RCU-safe 的 B-tree 变种,搜索/插入/区间查询都更快,且支持无锁读(per-VMA lock 依赖它)。本书 P4-12 用 maple tree。看 LPC 2021 Liam 的 talk "Maple Tree, the VMA replacement for the red-black tree"。
- **MGLRU(Multi-Gen LRU)**(6.1 起,Yu Zhao):重写 LRU/aging 机制。传统 active/inactive 双链对冷热判断粗糙,MGLRU 用多代(generation)细分,对 SSD/内存富余负载回收更准。看 `/sys/kernel/mm/lru_gen/enabled`,本书 P5-17 提到。看 LPC 2022 Yu Zhao 的 talk。
- **per-VMA lock**(6.3 起,Suren Baghdasaryan):页错误处理时,不拿 `mmap_lock`(读锁,进程级),而是拿 per-VMA 的 `vma_lock`(细粒度)。依赖 maple tree 的 RCU-safe。大幅降低多线程页错误的锁竞争。本书 P4-12 提到。
- **mTHP(multi-size THP)**(6.8 起):THP 不再只有 2MB 一档,支持中间尺寸(如 64KB、128KB、512KB),在 TLB 收益和规整成本之间取平衡。`/sys/kernel/mm/transparent_hugepage/hugepage-*/enabled`。
- **DAMON**(5.15 起,数据访问 monitor):基于采样的精确访问模式监控,给 proactive reclaim / KSM 用。`/sys/kernel/mm/damon/`。
- **large folio for file**(6.x 近期):文件映射也支持大 folio,不只匿名页。

跟进这些演进的最好途径是看每届 **LPC(Linux Plumbers Conference)** 和 **LSF/MM(Linux Storage Filesystem and Memory Management Summit)** 的 talk,以及 [LWN.net](https://lwn.net) 的 mm 系列报道。

### B.4.3 推荐书

- **MEL Gorman《Understanding the Linux Virtual Memory Manager》**(2004,基于 2.6.6):虽然老(二十年前),但**概念框架最清晰**——buddy、slab、页表、vmscan 的设计动机讲得最透。是 mm 的"老三样"之首。免费 PDF 在 lineo 或作者主页。读时要脑内"翻译"成新版本(2.6 → 6.9,page → folio,rb-tree → maple tree)。
- **ULK《Understanding the Linux Kernel》**(Bovet & Cesati,第 3 版,2005,基于 2.6):mm 章(第 8 章内存管理、第 9 章进程地址空间、第 16 章swap、第 17 章回收)是经典。同样老,但概念讲解无出其右。
- **Professional Linux Kernel Architecture**(Wolfgang Mauerer,2008,基于 2.6.24):比 ULK 稍新一点,mm 部分更侧重工程实现。
- **Linux Kernel Development**(Robert Love,第 3 版,2010,基于 2.6.34):mm 章偏入门,适合先读这本再读 ULK/MEL Gorman。
- **Linux 内核源码自带的文档**:`Documentation/mm/`(在内核源码树里)是当前最权威的 mm 文档——overcommit-accounting.rst、transhuge.rst、ksm.rst、numa_memory_policy.rst、slb.rst 等。配合源码读,权威性最高。
- **LWN.net 的 mm 文章**:Jonathan Corbet / Mel Gorman 等写的 mm 系列,覆盖每届新特性(folio/maple tree/MGLRU/per-VMA lock 的引入都先在 LWN 讲)。订阅 LWN 值得。
- **学术论文**:Christoph Lameter 的 SLUB paper(2007);Sanjay Ghemawat 的 tcmalloc paper(2005);Jason Evans 的 jemalloc paper(2011);Peter Denning 的虚拟内存经典论文(1970,working set 理论)。

### B.4.4 LPC / LSF/MM talks

每届 LPC 和 LSF/MM 都有 mm 的核心 talk,是跟进最新进展的最佳途径。值得找录像/ppt 的:

- **Liam Howlett**(Oracle):maple tree 系列(2021~2024 LPC),per-VMA lock(2023 LPC)。
- **Matthew Wilcox**(Oracle):folio 系列(2020~2024),large folio,page cache 重构。
- **David Hildenbrand**(Red Hat):folio/mapcount,hugetlb vmemmap 优化,KSM,memfd。
- **Yu Zhao**(Google):MGLRU(2021~2023),多代 LRU。
- **Johannes Weiner**(Facebook/Meta):workingset,psi(pressure stall information),cgroup v2 memory。
- **Michal Hocko / Roman Gushchin**(SUSE/Meta):memcg,cgroup v2 memory。
- **Minchan Kim / Suren Baghdasaryan**:compaction,per-VMA lock,reclaim 改进。
- **Mel Gorman**(SUSE):compaction,NUMA balancing,page migration,长期 mm 核心贡献者。

**LSF/MM 的议题**每年在 [lwn.net](https://lwn.net) 有详细报道,是理解 mm 社区在讨论什么、往哪走的最佳窗口。

---

## B.5 最后:读完本书之后

本书到此结束。如果你读完了二十章 + 附录 A/B,你现在应该能在脑子里放映出:

- 一次 `kmalloc(128)` 怎么从 slab 的 per-cpu freelist 摘对象(P2-08),没命中怎么回 partial、回 buddy(P1-04)。
- 一次用户 `malloc(1GB)` 怎么先建 VMA(P4-12),访问触发缺页时怎么建页表 + 分配物理页(P4-14),实际只兑现被访问的部分(惰性)。
- 内存紧张时 watermark 怎么预判、kswapd 怎么后台回收(P5-16)、LRU/workingset 怎么选冷页(P5-17)、compaction 怎么规整(P5-18)、swap 怎么换出(P5-19)、最后 OOM 怎么挑进程杀(P5-19)。
- 这背后 per-cpu 无锁、order 合并、惰性兑现、抗碎片、分级回收、省元数据这六条哲学是怎么撑起整个 mm 的(P7-21)。

下一步:

1. **动手观测**:在 Linux 机器上跑 `cat /proc/buddyinfo`、`cat /proc/slabinfo | head -30`、`cat /proc/vmstat | grep -E 'pgscan|compact|thp|oom'`,看 mm 活的实况(B.2)。
2. **读源码**:按 B.1 的顺序,从 `mm_types.h` 开始,顺着主线读 `page_alloc.c` → `slub.c` → `mmap.c` → `memory.c` → `vmscan.c`。
3. **跟社区**:订阅 LWN,看每年 LPC/LSF/MM 的 mm talks,跟进 folio/maple tree/MGLRU/per-VMA lock 的最新进展(B.4)。
4. **读经典书**:MEL Gorman《Understanding the Linux Virtual Memory Manager》是 mm 的圣经,配合本书的动机+技巧双线读,效果最好。

mm 是 Linux 内核里最大、最复杂的子系统之一。本书只是个入门——但如果你把二十章 + 附录 A/B 的地图刻进脑子里,再读任何 mm 文章、源码、talk,你都能定位"我在哪、这条流的上下游是谁"。这才是"看懂 Linux mm"的真正起点。

**全书及附录完。**
