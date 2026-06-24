# 附录 B · 源码阅读路线与调试工具

> 前面 13 章是"讲道理",这篇是"动手"。
> 它回答两个问题:**源码该按什么顺序读**,以及**怎么用工具把抽象的设计变成眼前可量化的数字**。
> 内存管理最大的学习障碍是"看不见"。读完这篇,你会有一套**仪表盘**,让每一章讲的机制都能在真实机器上观测到。

---

## 一、推荐的源码精读顺序

直接从 [mm/page_alloc.c](../linux-6.14/mm/page_alloc.c) 开啃,十有八九会迷路——它两千多行,牵扯的东西太多。**源码要顺着数据流和控制流读,而不是顺着文件读。** 下面这条路线,是按"先有地基、再起柱子"的依赖顺序排的,每一步都建立在前一步之上。

### 第一站:物理地基(先搞清楚"手里有什么")

| 顺序 | 文件 | 读什么 | 对应章 |
|------|------|--------|--------|
| 1 | [arch/x86/kernel/e820.c](../linux-6.14/arch/x86/kernel/e820.c) | `e820__memory_setup` → `e820__update_table`(清洗) → `e820__memblock_setup`(移交) | 第 2 章 |
| 2 | [mm/memblock.c](../linux-6.14/mm/memblock.c) + [include/linux/memblock.h](../linux-6.14/include/linux/memblock.h) | `struct memblock`(L94)、`memblock_add`/`memblock_alloc`/`memblock_free`——体会"只记区间"的极简 | 第 4 章 |

> 这一段是"启动期",代码量小、逻辑直白,**适合作为读源码的入门**。读完后你应该能回答:"内核醒来时,怎么从一份满是洞的 e820,走到一份干净的可用内存清单。"

### 第二站:分配核心(整本书的心脏)

| 顺序 | 文件 | 读什么 | 对应章 |
|------|------|--------|--------|
| 3 | [include/linux/mmzone.h](../linux-6.14/include/linux/mmzone.h) | `struct zone`、`struct free_area`、`enum zone_type`、`enum migratetype`、`enum zone_watermarks`——**先读结构定义,后读算法** | 第 5 章 |
| 4 | [mm/page_alloc.c](../linux-6.14/mm/page_alloc.c) | **三件套**:`__free_one_page`(合并循环)→ `__rmqueue`/`rmqueue`(分配快慢分流)→ `__alloc_frozen_pages_noprof`(对外入口,快慢路径分界) | 第 6 章 |
| 5 | [mm/slub.c](../linux-6.14/mm/slub.c) | `kmem_cache_alloc` → `___slab_alloc`(per-cpu 快通道)。对比伙伴系统,看"页→小对象"怎么切 | 第 7 章 |

> 这一段是物理侧的核心,也是源码量最大的部分。**建议每个函数配 `/proc/buddyinfo`、`/proc/slabinfo` 一起看**(见第二节),边读边在你机器上验证。

### 第三站:虚拟幻象(从"切房间"过到"造幻觉")

| 顺序 | 文件 | 读什么 | 对应章 |
|------|------|--------|--------|
| 6 | [mm/mmap.c](../linux-6.14/mm/mmap.c) + [mm/vma.c](../linux-6.14/mm/vma.c) | `do_mmap`(圈地总入口)→ `vma_link`(写进 maple tree)。配 [include/linux/mm_types.h](../linux-6.14/include/linux/mm_types.h) 的 `struct vm_area_struct`(L681)、`struct mm_struct` | 第 10 章 |
| 7 | [arch/x86/mm/fault.c](../linux-6.14/arch/x86/mm/fault.c) + [mm/memory.c](../linux-6.14/mm/memory.c) | `do_user_addr_fault`(找 VMA、查权限)→ `handle_mm_fault`(兑现:匿名页 / COW / 文件页)。**这是全书最精彩的一段控制流** | 第 9 章 |
| 8 | [mm/memory.c](../linux-6.14/mm/memory.c) 的页表遍历 | `pgd/pud/pmd/pte` 四级页表操作,看 48 位地址怎么一步步翻译 | 第 8 章 |

> 读 `handle_mm_fault` 时,建议配一个会触发 page fault 的小程序(见第二节),用 `perf` 或 `ftrace` 看它真的走过这些函数。

### 第四站:文件与回收(把"实物"和"不够时"补全)

| 顺序 | 文件 | 读什么 | 对应章 |
|------|------|--------|--------|
| 9 | [mm/filemap.c](../linux-6.14/mm/filemap.c) | `address_space`(在 [include/linux/fs.h:502](../linux-6.14/include/linux/fs.h#L502))、`filemap_fault`(文件 page fault 怎么从 page cache 取页)。配 [include/linux/mm_types.h](../linux-6.14/include/linux/mm_types.h) 的 `struct folio`(L324) | 第 11 章 |
| 10 | [mm/vmscan.c](../linux-6.14/mm/vmscan.c) | `shrink_lruvec`(LRU 老化)→ `try_to_free_pages`(直接回收)→ `out_of_memory`(OOM 选谁杀)。配水位线 min/low/high 看它什么时候启动 | 第 12 章 |
| 11 | [lib/maple_tree.c](../linux-6.14/lib/maple_tree.c) + [include/linux/maple_tree.h](../linux-6.14/include/linux/maple_tree.h) | (可选进阶)VMA 的底层数据结构,RCU-safe 的 B-tree 变体 | 第 10 章延伸 |

### 第五站:现代场景(按需选读)

| 顺序 | 文件 | 读什么 | 对应章 |
|------|------|--------|--------|
| 12 | [mm/hugetlb.c](../linux-6.14/mm/hugetlb.c) / [mm/huge_memory.c](../linux-6.14/mm/huge_memory.c) | 大页:预留式(hugetlb)vs 透明(THP) | 第 13 章 |
| 13 | [mm/memcontrol.c](../linux-6.14/mm/memcontrol.c) | memcg:容器内存隔离与局部 OOM | 第 13 章 |

> **一句话原则:先结构,后算法;先快路径,后慢路径;先物理侧,后虚拟侧。** 任何函数读不下去,先去读它操作的那个 `struct`,字段注释往往直接讲清了设计意图。

---

## 二、观测工具速查:把抽象变成可见的数字

内存管理的术语再多,不如一个 `/proc/meminfo` 直观。下面按"对应哪一章"组织,每个工具配一句"它告诉你什么"和"什么时候看它"。

### 2.1 进程级地址空间(第 8、10 章)

| 命令 | 看什么 | 什么时候看 |
|------|--------|-----------|
| `cat /proc/<pid>/maps` | 一个进程的**整本 VMA 账本**,每行一个 VMA(起止/权限/偏移/文件) | 验证"malloc 只建 VMA 不配页";看代码段/堆/栈/库各自在哪;理解 ASLR |
| `cat /proc/<pid>/smaps` | maps 的加强版:每个 VMA 附带**实际占了多少物理页**(Rss/Pss)、多少 swap | 对照 maps,看"圈地(VMA)vs 占地(Rss)"的差距——这是延迟兑现的铁证 |
| `cat /proc/<pid>/smaps_rollup` | 整个进程的内存汇总(总 RSS、swap、匿名页……) | 快速看一个进程"吃了多少真内存" |
| `pmap -x <pid>` | smaps 的友好排版版(可读性更好) | 日常排查进程内存占用 |

> **动手实验**:写个程序 `p = malloc(100*1024*1024)` 然后 `sleep`。先 `cat /proc/$PID/maps` 看到那 100MB 的 VMA 已经建好,再 `cat /proc/$PID/smaps \| grep Rss` 发现 Rss 几乎为 0。**亲眼看见"圈地不占地"。**

### 2.2 物理页分配(第 5、6 章)

| 命令 | 看什么 | 什么时候看 |
|------|--------|-----------|
| `cat /proc/buddyinfo` | 每个 zone 的 **11 档空闲块数量**(伙伴系统的 free_area 直接外露) | 看内存"碎成什么样";高阶档(右边的列)为 0 说明大块连续内存没了 |
| `cat /proc/pagetypeinfo` | buddyinfo 的细分版:按 **migrate type** 分开看各档 | 看抗碎片情况;排查"为什么申请不到大页" |
| `cat /proc/zoneinfo` | 每个 zone 的三条**水位线**(min/low/high)、`pages free`、`nr_free_highatomic`、managed_pages | 理解第 6 章水位线;判断 kswapd 会不会被唤醒 |

> `buddyinfo` 输出示例:`Node 0, Zone Normal 123 45 12 8 3 1 0 ...` ——从左到右是 order 0~10 的空闲块个数。**左边多是单页碎片,右边多是连续大块。** 长期运行的机器,右边几列往往是 0,这就是外部碎片。

### 2.3 小对象池(第 7 章)

| 命令 | 看什么 | 什么时候看 |
|------|--------|-----------|
| `cat /proc/slabinfo` | 所有 `kmem_cache` 的统计:活跃对象数、每个对象大小、slab 页数 | 看哪种内核对象最吃内存(inode/dentry/task_struct…);(需 root) |
| `slabtop -o` 或 `slabtop` | slabinfo 的实时排序版,按占用排序 | 排查"内核内存(xen/文件系统缓存)涨了" |

### 2.4 全局内存 + 回收(第 11、12 章)

| 命令 | 看什么 | 什么时候看 |
|------|--------|-----------|
| `cat /proc/meminfo` | 全局仪表盘:**MemFree / MemAvailable / Cached / Buffers / SwapTotal / Dirty / Writeback / AnonPages / Slab** | 最常看的一个。"free 很少"不代表不健康(Cached 可让出) |
| `cat /proc/swaps` | 启用的 swap 区及用量 | 看有没有真的在用 swap |
| `vmstat 1` | 实时:`si`/`so`(swap in/out)、`bi`/`bo`(块 IO)、`free`、`wa` | 监控回收/swap 压力;`si/so` 持续 > 0 说明内存吃紧 |
| `cat /proc/sys/vm/overcommit_memory` | overcommit 策略(0 启发式 / 1 总是允许 / 2 严格) | 理解第 12 章"为什么能超量分配" |
| `cat /proc/sys/vm/swappiness` | 匿名页 vs 文件页回收的倾向(0~200,默认 60) | 调回收偏好 |
| `cat /proc/sys/vm/watermark_scale_factor` | 水位线 min/low 之间的间距 | 调 kswapd 启动积极度 |
| `cat /proc/sys/vm/max_map_count` | 单进程 VMA 数上限(默认 65530) | 第 10 章那条资源红线 |

> **关键认知**:`MemFree` 低 ≠ 不健康。Linux 故意把空闲内存当 page cache 用(第 11 章)。**看 `MemAvailable` 而不是 `MemFree`**——前者把"可让出的缓存"算回了"可用"。

### 2.5 内存压力与 OOM(第 12 章)

| 命令 / 文件 | 看什么 |
|------------|--------|
| `dmesg \| grep -i "oom\|killed"` | OOM killer 何时启动、杀了谁、为什么(它打印被杀进程的 rss/分数) |
| `journalctl -k \| grep oom` | 同上,从 journal 取 |
| `cat /proc/<pid>/oom_score` | 一个进程的 OOM 分数(越高越先被杀) |
| `cat /proc/<pid>/oom_score_adj` | 手动调 OOM 倾向(-1000 永不被杀) |
| `ps aux --sort=-rss \| head` | 按物理内存占用排序找"大户" |

### 2.6 大页与容器(第 13 章)

| 命令 | 看什么 |
|------|--------|
| `cat /proc/meminfo \| grep -i huge` | Hugetlb 大页的总量/空闲/预留 |
| `cat /sys/kernel/mm/transparent_hugepage/enabled` | 透明大页 THP 是否开启(always/madvise/never) |
| `cat /sys/fs/cgroup/memory.<x>/...`(cgroup v2: `/sys/fs/cgroup/.../memory.max` 等) | memcg 的内存上限与用量;容器隔离的仪表盘 |

### 2.7 深度调试(出事时钻进去)

| 工具 | 干什么 | 难度 |
|------|--------|------|
| **`crash`** | 配合 vmlinux 和内核转储(dump),**事后**查看任意内核数据结构:`struct zone`、page 数组、VMA 树……排查"为什么机器挂了" | 高,但威力最大 |
| **`ftrace`** / `trace-cmd` | 追踪特定函数(如 `__alloc_pages_noprof`、`handle_mm_fault`)何时被谁调用,看真实控制流 | 中 |
| **`perf`** | 采样热点、统计 page fault、统计 slab 分配;`perf stat -e page-faults,faults` | 中 |
| **`/sys/kernel/debug/`** | 内核调试入口(`CONFIG_DEBUG_INFO`、kpagecount/kpageflags 等),需 root + debugfs 挂载 | 中高 |

> **`ftrace` 快速上手**(验证第 9 章 page fault 路径):
> ```bash
> echo 1 > /sys/kernel/debug/tracing/events/exceptions/page_fault_user/enable
> cat /sys/kernel/debug/tracing/trace_pipe    # 跑个程序,看 page fault 事件流
> ```
> 把"书上画的调用链"变成"屏幕上跳动的真实事件",这一刻抽象就落地了。

---

## 三、一个推荐的"读书 + 实验"循环

光读不练,记忆留不过一周。建议每读完一章,做这个三步循环:

1. **读源码**:照第一节那张表,读对应文件的关键函数,记下"它在干什么、解决什么痛点"。
2. **找仪表盘**:照第二节,找到能观测这一层的 `/proc` 文件或工具。
3. **制造现象**:写个小程序或跑个命令,让那一层的机制在你的机器上"动起来",用仪表盘看它的数字变化。

> 举一个完整例子(对应第 9、10 章):
> - 读 `handle_mm_fault`,理解"匿名页 fault 怎么从伙伴系统要一页"。
> - 找仪表盘:`/proc/<pid>/smaps` 的 `Rss` 字段、`/proc/buddyinfo` 的 order-0 列。
> - 制造现象:程序 `malloc` 一块内存→记下 smaps 的 Rss(几乎 0)和 buddyinfo→**touch** 那块内存的每一页→再看 smaps(Rss 涨了)和 buddyinfo(order-0 少了)。
> - 你会亲眼看到:**一次访问 = 一次 page fault = 从伙伴系统拿走一页 = VMA 从空头支票变成真房间。** 第 9、10 章的核心,就在这个对照里活了过来。

---

## 四、收尾

到这里,你有了三样东西:

- **13 章 + 附录 A**:一套从第一性原理出发的、讲清"为什么"的内存管理心智模型;
- **本附录第一节**:一条顺着数据流/控制流、不会迷路的源码阅读路线;
- **本附录第二、三节**:一套把抽象变成数字的仪表盘和实验方法。

接下来就靠你自己跑了。内存管理这门课,**读十遍不如亲手观测一遍**。祝你把那栋满是洞的大楼,看明白、调得动。

---

> 返回 **[附录 A · 全景脉络与设计哲学总结](P2-附录A-全景脉络与设计哲学总结.md)**,或回到 **[目录](P2-00-目录.md)**。
