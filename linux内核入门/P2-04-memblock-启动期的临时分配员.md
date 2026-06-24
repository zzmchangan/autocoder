# 第 4 章 · memblock:启动期的临时分配员

> **核心问题**:正式的伙伴系统还没建好,但内核启动过程中就要分配内存(建页表、铺 per-cpu 数据、解析设备树……),怎么办?

> 读完本章你会明白:
> - 为什么 memblock 不像伙伴系统那样"管每一页",而只记"哪些楼层空着、哪些被划走了"。
> - 为什么"临时、丑、能凑合"恰恰是这个阶段最正确的工程取舍。
> - memblock 如何在伙伴系统就位后,把手里剩下的空房间**整批移交**过去,然后功成身退。

> **如果一读觉得太难**:先只记住三件事——① memblock 只管"区间"不管"页",靠两本粗账(可用 `memory` / 已划走 `reserved`)相减算出空闲;② 分配永远是"先**找**地址、再**划**进账"两步走;③ 它的宿命是把空闲内存整批移交给伙伴系统后**消失**。其余细节第二遍配合源码再抠,不影响往下读。

---

## 一、先回到那个"鸡生蛋"的死结

上一章(第 3 章)结束时,内核刚把自己从"笨模式(实模式)"拎到了"聪明模式(长模式)",搭好了一张够自己跑起来的临时页表。可紧接着它就撞上一堵墙:

> **正式的内存分配器(伙伴系统)还没建好,但内核在启动过程中就要分配内存。**

要给后续的页表腾地方、要给每个 CPU 铺一份 per-cpu 数据、要把设备树/ACPI 表读进来建结构、要给各种 `__init` 数据准备空间……这一桩桩一件件,每一件都在喊"给我一块内存"。可此刻整栋大楼里,能分配内存的出口**一个都还没开张**。

这就是经典的**鸡生蛋问题**:

```
  伙伴系统初始化  ──需要──▶  内存
       ▲                        │
       └─────────分配──────────┘
                 (出口还没开)
```

> **不解决会怎样?** 内核根本启动不下去。伙伴系统初始化自己需要内存,而唯一能给出内存的伙伴系统还没起来——死锁。你需要一个**不依赖任何复杂基础设施、自己就能跑**的最简分配器,先把启动这一段熬过去。

答案是先上**临时工**:memblock([mm/memblock.c](../linux-6.14/mm/memblock.c))。

> **大楼比喻**:正式物业(伙伴系统)还没入驻,但施工队马上要在楼里干活,得先有个**临时工棚管理员**。他不懂什么精装修、不懂什么按楼层分组,只会一招——"这层空着,你用这段;那段划走了,我记一笔"。丑、糙,但**今天就能开张**。等正式物业接管,工棚拆掉,材料全搬进大楼。

---

## 二、设计哲学:只记"区间",不记"单间"

理解 memblock,最关键的一步是想明白:**它和伙伴系统管的东西粒度完全不同。**

伙伴系统管的是"**每一页(4KB)**"的归属——哪页空着、哪页被谁占了、哪几页能合并成大块。这是"精装修式"的细账。

memblock 不管这个。它只记两件最粗的事:

1. **memory**:这栋楼里**哪些楼层是可用的 RAM**(从 BIOS 的 e820 图搬过来的)。
2. **reserved**:这些可用楼层里,**哪些区间已经被划走了**(给了内核镜像、给了页表、给了某次启动期分配)。

至于"被划走的区间里头具体每一页怎么用",它**根本不关心**。一段 `[base, size)` 记一条,完事。

> **为什么不记单页?** 三个理由,每一条都是被启动期的现实逼出来的:
>
> 1. **需求都是"一大段连续",不是"一页一页的"。** 启动期要分配的东西——一张页表目录、一块 per-cpu 区、一份设备树副本——动辄几页、几十页连续内存。一颗颗页去记账,粒度太细,纯属浪费。
> 2. **没有数据结构可以用。** 这会儿连 slab 都没起来,你想给每页挂个 `struct page`、挂个链表?没那个条件。最朴素的做法,就是**两个数组**,每条记录 `{base, size, flags}`,数组本身静态分配好,开机即用。
> 3. **记账本身也得尽量便宜。** 启动期分配次数其实不多(几百次量级),用最简单的"区间数组 + 插入合并",几百行代码搞定。伙伴系统那种 O(1) 的精巧结构,在这里**没有用武之地,反而有 bug 风险**。

所以 memblock 的全部数据结构,就是两个"区间数组"。看真实定义:

```c
/* include/linux/memblock.h:62 —— 一条区间记录 */
struct memblock_region {
    phys_addr_t base;
    phys_addr_t size;
    enum memblock_flags flags;
#ifdef CONFIG_NUMA
    int nid;
#endif
};

/* include/linux/memblock.h:79 —— 一类区间(就是"一个数组") */
struct memblock_type {
    unsigned long cnt;          // 当前有几条记录
    unsigned long max;          // 数组容量
    phys_addr_t total_size;     // 这一类区间加起来多大
    struct memblock_region *regions;  // 指向区间数组
    char *name;
};

/* include/linux/memblock.h:94 —— memblock 的全部家当 */
struct memblock {
    bool bottom_up;
    phys_addr_t current_limit;  // 分配不许越过这条物理地址线
    struct memblock_type memory;   // 第一本账:可用 RAM
    struct memblock_type reserved; // 第二本账:已划走
};
```

完整定义见 [struct memblock_region](../linux-6.14/include/linux/memblock.h#L62-L69)、[struct memblock_type](../linux-6.14/include/linux/memblock.h#L79-L85)、[struct memblock](../linux-6.14/include/linux/memblock.h#L94-L99)。

一句话:**memblock 没有"页"这个概念,它只有"区间"。**

那"空闲内存"怎么算?**不在 reserved 里的 memory,就是空闲。** 这是 memblock 全部逻辑的基石:

```
   可用楼层(memory):   ████████████████████████████
   已划走(reserved):        ████      ██        ███
   空闲 = memory 减去 reserved:
                        ██      ████        ██
```

> **这个"减法"视角很重要**,后面你会看到,移交伙伴系统时,内核遍历的正是"memory 里有、reserved 里没有"的那些缝隙——即 [mm/memblock.c](../linux-6.14/mm/memblock.c#L2232) 里 `for_each_free_mem_range` 那一句。

---

## 三、开机即用:数组是静态分配好的

另一个"为什么"藏在数据结构怎么来的问题上。

> **不这样会怎样?** 假设 memblock 的区间数组也要"现分配",那就又回到了鸡生蛋——分配数组本身就需要一个分配器。死循环。

所以 memblock 的两个数组是**编译期就钉死的静态数组**,开机那一刻就在内存里摆好了,谁都不用分配:

```c
/* mm/memblock.c:109 */
static struct memblock_region memblock_memory_init_regions[INIT_MEMBLOCK_MEMORY_REGIONS] __initdata_memblock;
static struct memblock_region memblock_reserved_init_regions[INIT_MEMBLOCK_RESERVED_REGIONS] __initdata_memblock;
```

容量是 128 条,即 [mm/memblock.c:25](../linux-6.14/mm/memblock.c#L25) 的 `INIT_MEMBLOCK_REGIONS`。全局唯一的 `memblock` 实例也直接静态初始化好,把两个数组挂上去(完整初始化见 [mm/memblock.c 的 memblock 定义](../linux-6.14/mm/memblock.c#L115-L126)):

```c
/* mm/memblock.c:115 */
struct memblock memblock __initdata_memblock = {
    .memory.regions  = memblock_memory_init_regions,
    .memory.max      = INIT_MEMBLOCK_MEMORY_REGIONS,
    .memory.name     = "memory",
    .reserved.regions = memblock_reserved_init_regions,
    .reserved.max     = INIT_MEMBLOCK_RESERVED_REGIONS,
    .reserved.name    = "reserved",
    .bottom_up        = false,
    .current_limit    = MEMBLOCK_ALLOC_ANYWHERE,
};
```

注意那两个 `__initdata_memblock` 标记——它告诉链接器:"这些变量只在启动期用,启动完就可以回收掉它占的那点内存。" 这是临时工棚的宿命:**用完即拆**。

> **128 条够吗?** 一般够。但万一启动期 reserve 的区间特别多(某些机器 BIOS 报的内存图碎得厉害),数组会不够——这时 memblock 有个精妙的"自救"机制,我们在第六节讲。

---

## 四、分配的本质:先"找",再"划"

现在看 memblock 最核心的动作:**分配内存**。

伙伴系统分配是"从自由链表上摘一个块"。memblock 完全不同——它**分两步走**:

1. **找(find)**:在"memory 里有、reserved 里没有"的空闲缝隙里,找一个够大、对齐满足要求的物理地址。
2. **划(reserve)**:把这个地址区间加进 `reserved` 数组,从此它就是"已划走"。

> **为什么不一步到位,非要拆成"找"和"划"两步?**
>
> 拆开的好处是**职责单一、可组合**。"找"只负责定位,"划"只负责记账。这样:
> - 我可以"找"到一个地址但**先不划**(比如想拿来干别的,或想确认够不够再决定);
> - 我也可以"划"一个**早就知道地址**的区间(比如内核镜像本来就被 BIOS 装在固定位置,内核只需把那段 `[kernel_start, kernel_size)` reserve 掉,根本不需要"找");
> - 各种约束(DMA 限制、NUMA 节点、镜像内存)只作用在"找"这一步,"划"永远是同一套逻辑。

看真实的分配入口 [memblock_alloc_range_nid](../linux-6.14/mm/memblock.c#L1434-L1506),正好是这两步:

```c
/* mm/memblock.c:1459 */
again:
    found = memblock_find_in_range_node(size, align, start, end, nid, flags);
    if (found && !memblock_reserve(found, size))
        goto done;
    /* ... 找不到就回退到别的节点 / 降级镜像要求,再 goto again ... */
```

`find` 拿到地址,`reserve` 把它钉死。两步之间夹着判断:`found` 必须非零(找到了)、且 `memblock_reserve` 成功(没和别人撞车、数组没满),才算分配到手。这就是 memblock 分配的全部精髓。

### "找"的方向:为什么默认从高地址往下找

`memblock_find_in_range_node` 内部会根据 `bottom_up` 标志选择搜索方向(见 [mm/memblock.c:304](../linux-6.14/mm/memblock.c#L304))。默认 `bottom_up == false`,也就是**从高地址往低地址找(top-down)**,核心循环在 [`__memblock_find_range_top_down`](../linux-6.14/mm/memblock.c#L252-L273):

```c
/* mm/memblock.c:252 —— 从高到低,在每个空闲缝隙里挑地址 */
for_each_free_mem_range_reverse(i, nid, flags, &this_start, &this_end, NULL) {
    this_start = clamp(this_start, start, end);
    this_end   = clamp(this_end,   start, end);
    if (this_end < size)
        continue;
    cand = round_down(this_end - size, align);  // 贴着缝隙的上沿对齐
    if (cand >= this_start)
        return cand;
}
```

> **为什么默认 top-down?** 因为**低地址是"地皮紧张的好地段"**。还记得第 5 章要讲的 ZONE_DMA 吗?老的 ISA 设备做 DMA,**物理上只能寻址到 16MB 以下**;很多 32 位外设只能到 4GB 以下。这些低地址区间是稀缺资源,启动期如果随便往低端塞东西,等会儿设备要 DMA 时就抓瞎了。所以 memblock 默认从高端往下吃,**把低端的珍贵地段留给真正需要它的设备**。这是个很小但很对的默认值选择。

---

## 五、记账的灵魂:插入并合并相邻区间

memblock 把一段区间加进 `reserved`(或 `memory`)数组时,做的不只是"在数组末尾追加一条"。它做了一件更重要的事:**把新区间和已有区间缝合并合并**。

> **不合并会怎样?** 假设你 reserve 了 `[0, 0x1000)`,又 reserve 了相邻的 `[0x1000, 0x2000)`。如果不合并,数组里就有两条挨在一起的记录。再来一条、再来一条……很快 128 条就撑爆了,而且记录间到处是"伪边界",既难看又难查。更糟的是,"空闲 = memory − reserved" 的减法会变得更繁琐。

所以 [`memblock_add_range`](../linux-6.14/mm/memblock.c#L584-L688) 的核心职责是:**插入新区间后,保证整个数组是"最小"的——所有相邻且属性相同的区间都合并成一条**。

它用一个很聪明的"两遍式"算法:第一遍只数数(算出最坏情况要插几条),第二遍才真插,插完立刻调 [`memblock_merge_regions`](../linux-6.14/mm/memblock.c#L509-L536) 把相邻同质的区间缝起来:

```c
/* mm/memblock.c:509 —— 把相邻、同节点、同 flags 的区间合并 */
while (i < end_rgn) {
    struct memblock_region *this = &type->regions[i];
    struct memblock_region *next = &type->regions[i + 1];

    if (this->base + this->size != next->base ||      // ① 必须首尾相接
        memblock_get_region_node(this) !=
        memblock_get_region_node(next) ||              // ② 必须同 NUMA 节点
        this->flags != next->flags) {                  // ③ 必须同属性
        i++;
        continue;
    }
    this->size += next->size;                          // 合体:尺寸相加
    memmove(next, next + 1, ...);                      // 后面的记录整体前移
    type->cnt--;                                       // 总条数减一
    end_rgn--;
}
```

三个条件(首尾相接、同节点、同 flags)全满足,才把两条并成一条。这一手让区间数组在频繁增删下始终保持紧凑——**这就是 memblock 用区间数组而不是页表、不是位图、不是链表,却不会爆炸的根本原因**。

释放也走同一条路。[`memblock_free`](../linux-6.14/mm/memblock.c#L879-L883) 其实就是从 `reserved` 数组里**移除**一段区间(调 `memblock_remove_range`),并不归还给任何"自由链表"——因为 memblock 根本没有自由链表,空闲是算出来的,不是存出来的:

```c
/* mm/memblock.c:893 */
int __init_memblock memblock_phys_free(phys_addr_t base, phys_addr_t size)
{
    /* ... */
    return memblock_remove_range(&memblock.reserved, base, size);
}
```

> **注意一个反直觉点**:在 memblock 阶段"释放"内存,**只是把它从 reserved 账上划掉**,让它在"减法"里重新算作空闲。它并不会、也没法立刻回到伙伴系统——因为伙伴系统还没就位。真正把空闲内存交给伙伴系统,是后面"移交"那一步的事。

---

## 六、自救:数组不够用时,memblock 给自己分配更大的数组

启动期一般 reserve 不到 128 条。但万一真不够了呢?这里藏着 memblock 最巧妙的一段——[`memblock_double_array`](../linux-6.14/mm/memblock.c#L410-L500)。

它要做的事听起来像悖论:**"用 memblock 自己,给 memblock 自己分配一个更大的区间数组。"** 这不又是鸡生蛋吗?

关键在于:此刻 memblock 的"找 + 划"已经能用了。所以做法是——

1. 用 `memblock_find_in_range` 在空闲缝隙里找一块够放双倍数组大小的连续内存(**只找,先不划**)。
2. 找到后,把旧数组内容拷过去。
3. 再把这块新数组占的区间正式 `memblock_reserve` 进 reserved 账(见 [mm/memblock.c:494](../linux-6.14/mm/memblock.c#L494) 的 `BUG_ON(memblock_reserve(addr, new_alloc_size))`)。

之所以"先找后划",正是前面第四节强调的两步分离带来的好处:**找的时候还没占用,所以查找过程不会和自己冲突**。这是个利用自身机制解决自身扩容问题的优雅例子——临时工虽然糙,但够聪明到能给自己换个大点的工棚。

> 小细节:如果此时 slab 已经能用,它会直接用 `kfree`/`kmalloc` 来管理这块新数组(标志位 `memblock_memory_in_slab` / `memblock_reserved_in_slab` 记录这一点,见 [mm/memblock.c:158](../linux-6.14/mm/memblock.c#L158));否则就用 memblock 自己的老办法。这套兼容设计让 memblock 在"slab 前"和"slab 后"两个阶段都能正常扩容。

---

## 七、一道防火墙:memblock 和 slab 互斥

启动是分阶段的。一开始只有 memblock;后来 slab 起来了,就该走 slab 分配。**这两套绝不能同时用**,否则同一块内存可能被两边重复分配——典型 use-after-free。

memblock 在自己的分配入口埋了一道显式的防火墙(见 [mm/memblock.c:1447](../linux-6.14/mm/memblock.c#L1447)):

```c
/* mm/memblock.c:1434 memblock_alloc_range_nid 开头 */
if (WARN_ON_ONCE(slab_is_available())) {
    void *vaddr = kzalloc_node(size, GFP_NOWAIT, nid);
    return vaddr ? virt_to_phys(vaddr) : 0;
}
```

> **为什么是 `WARN_ON_ONCE` 而不是直接 panic?** 因为有些驱动/架构在 slab 起来之后,历史代码里还残留着 `memblock_alloc` 的调用。直接崩机太狠,所以内核选择:**打印一次警告,然后偷偷转交给 slab 去分配**。既保护了正确性(不会用到已销毁的 memblock 数据),又给了一线兼容。这是"防御性编程 + 实用主义"的典型一笔。

---

## 八、高潮:把空房间整批移交给伙伴系统

前面所有铺垫,都服务于这一刻:**memblock 把手里剩下的空闲内存,整批交给伙伴系统,然后退场。**

这件事发生在 [`memblock_free_all`](../linux-6.14/mm/memblock.c#L2265-L2274):

```c
/* mm/memblock.c:2265 */
void __init memblock_free_all(void)
{
    unsigned long pages;

    free_unused_memmap();
    reset_all_zones_managed_pages();

    pages = free_low_memory_core_early();
    totalram_pages_add(pages);
}
```

核心是 [`free_low_memory_core_early`](../linux-6.14/mm/memblock.c#L2217-L2237),它干的就是前面说的那个"减法"——遍历 **memory 里有、reserved 里没有**的所有缝隙:

```c
/* mm/memblock.c:2232 */
for_each_free_mem_range(i, NUMA_NO_NODE, MEMBLOCK_NONE, &start, &end, NULL)
    count += __free_memory_core(start, end);
```

`for_each_free_mem_range` 这个宏(见 [include/linux/memblock.h:333](../linux-6.14/include/linux/memblock.h#L333-L348))内部就是拿 `memory` 当 A、`reserved` 当 B,做一次 A − B 的区间运算,吐出一段段真正的空闲区间。每段空闲区间交给 [`__free_memory_core`](../linux-6.14/mm/memblock.c#L2163-L2176),换算成页帧号后,进入 [`__free_pages_memory`](../linux-6.14/mm/memblock.c#L2137-L2161):

```c
/* mm/memblock.c:2137 —— 把一串连续页帧按"尽量大的块"喂给伙伴系统 */
static void __init __free_pages_memory(unsigned long start, unsigned long end)
{
    int order;

    while (start < end) {
        if (start)
            order = min_t(int, MAX_PAGE_ORDER, __ffs(start));
        else
            order = MAX_PAGE_ORDER;

        while (start + (1UL << order) > end)
            order--;

        memblock_free_pages(pfn_to_page(start), start, order);  // 喂给伙伴系统
        start += (1UL << order);
    }
}
```

这里有个**双向依赖的小浪漫**:移交时调的 `memblock_free_pages` 最终会走到伙伴系统的 [`__free_pages_core`](../linux-6.14/mm/page_alloc.c#L1275-L1321)——也就是说,**伙伴系统初始化所需的那批"种子页",正是 memblock 一手喂给它的**。鸡生蛋的死结,就在这一刻解开了:memblock 用最简陋的工具,把启动期撑过去,亲手把接力棒递给了正式管家。

> 而那些**被 reserve 的区间**(内核镜像、页表、per-cpu 数据……)自然不会出现在 `for_each_free_mem_range` 里——它们被永久占用,伙伴系统永远拿不到,也永远不会重复分配。这就是 reserve 账的最终意义:**标记"启动期就占死、永不出让"的那部分内存**。

---

## 九、功成身退:工棚拆除

移交完毕,memblock 的使命就结束了。它占的那些静态数组、扩出来的大数组,也该还回去。

[`memblock_discard`](../linux-6.14/mm/memblock.c#L367-L392) 就是干这个的——把当初 `double_array` 扩出来的那块数组内存(此时已经在 slab 或 reserved 里)释放掉:

```c
/* mm/memblock.c:367 */
if (memblock.reserved.regions != memblock_reserved_init_regions) {
    /* ... 把扩容用的数组内存归还 ... */
}
if (memblock.memory.regions != memblock_memory_init_regions) {
    /* ... 同上 ... */
}
memblock_memory = NULL;   // 置空指针,此后再误用就会立刻 NULL deref 暴露
```

而那两个 `__initdata_memblock` 标记的原始静态数组,会被链接器在启动末尾连同所有 `__init` 段一起整体回收(那段内存也变成空闲,归伙伴系统)。

至此,临时工棚彻底拆除,材料搬进了大楼。memblock 从开机到退场,自始至终没有越界——它从不试图成为一个通用分配器,**它只解决"启动这一段"这一个问题,解决完就消失**。

---

## 十、关键源码精读:`memblock_alloc_range_nid`

把前面几节串起来,精读一个最能代表 memblock 全貌的函数——分配主路径 [`memblock_alloc_range_nid`](../linux-6.14/mm/memblock.c#L1434-L1506):

```c
phys_addr_t __init memblock_alloc_range_nid(phys_addr_t size,
                    phys_addr_t align, phys_addr_t start,
                    phys_addr_t end, int nid, bool exact_nid)
{
    enum memblock_flags flags = choose_memblock_flags();   // ① 镜像优先?
    phys_addr_t found;

    /* ② 防火墙:slab 起来了就别用我 */
    if (WARN_ON_ONCE(slab_is_available())) {
        void *vaddr = kzalloc_node(size, GFP_NOWAIT, nid);
        return vaddr ? virt_to_phys(vaddr) : 0;
    }

    if (!align) {                                           // ③ 对齐兜底
        dump_stack();
        align = SMP_CACHE_BYTES;
    }

again:
    /* ④ 找 + 划:memblock 的两步走 */
    found = memblock_find_in_range_node(size, align, start, end, nid, flags);
    if (found && !memblock_reserve(found, size))
        goto done;

    /* ⑤ 指定节点没货?放宽到任意节点 */
    if (numa_valid_node(nid) && !exact_nid) {
        found = memblock_find_in_range_node(size, align, start,
                            end, NUMA_NO_NODE, flags);
        if (found && !memblock_reserve(found, size))
            goto done;
    }

    /* ⑥ 镜像内存不够?降级到普通内存再试 */
    if (flags & MEMBLOCK_MIRROR) {
        flags &= ~MEMBLOCK_MIRROR;
        pr_warn_ratelimited("Could not allocate %pap bytes of mirrored memory\n", &size);
        goto again;
    }

    return 0;                                               // ⑦ 真没了

done:
    /* ... kmemleak 记账 ... */
    return found;
}
```

逐段对应前面的设计:

- **②** 是第七节的防火墙——memblock 和 slab 互斥,防止双分配。
- **④** 是第四节的灵魂——`find` 定位,`reserve` 钉死,两步分离。
- **⑤⑥** 是"降级回退"哲学的体现:启动期**宁可放宽约束也要分配成功**(换节点、去掉镜像要求),因为此刻分配失败 = 启动失败,没有比这更糟的了。这跟运行期伙伴系统"分配失败就等回收/唤醒 kswapd"的从容完全是两套态度——**态度差异的根源,是两者面对的压力不同**。
- **⑦** 真的失败就返回 0。调用方(通常是 `panic` 友好的启动代码)自己决定怎么死。

整段函数没有一个链表节点、没有一把锁、没有一页 `struct page`——全是 plain 的整数运算和数组操作。**这正是 memblock"极简凑合"哲学的代码化身。**

---

## 十一、章末小结

### 用大楼比喻回顾

机器刚通电,物业(伙伴系统)还没入驻,但施工队要干活。于是请了个**临时工棚管理员 memblock**:

- 他不懂精装修,只会**记两本粗账**:一本"哪些楼层是可用的"(memory),一本"哪些区间划走了"(reserved)。空闲 = 可用 − 划走。
- 要分配?**两步走**:先在空闲缝隙里**找**个地址,再把这段**划**进账本。默认从高楼层往下吃,把低端好地段留给将来要搬货的设备。
- 记账时**随时把相邻同质的区间合并**,保证账本永远是紧凑的最小集;万一账本写满了,他能**用自己给自己换个大账本**。
- 一旦正式物业(slab / 伙伴系统)就位,他**立刻让位**:slab 起来后谁再喊他,他打你一个 `WARN` 然后偷偷转交给 slab;伙伴系统就位时,他把手里**所有没划走的空房间整批移交**,亲手喂出伙伴系统的第一批种子页,然后**拆掉工棚、连同自己的账本一起消失**。

### 回扣全书主线

这本书的二分法是"**物理一侧切房间,虚拟一侧造幻觉**"。memblock 服务的是**物理一侧**——它干的是最底层的"切房间"的前哨工作:在还没有正式分配器的时候,先把物理 RAM 里哪些能用、哪些占了这件事**记清楚、用起来**。

它在全书时间线上是个**过渡角色**:上承第 2、3 章(e820 图 → 内核自举搭临时页表),下接第 5、6 章(ZONE 分区 → 伙伴系统正式接管)。没有 memblock 这块"过渡跳板",内核根本熬不到伙伴系统建立的那一刻。memblock 把那个"鸡生蛋"的死结,用一个"丑但够用"的临时分配员解开了——**这是 Linux 内存管理"分层抽象、各管一段"哲学的第一次精彩亮相**。

### 想继续深入,往这儿钻

- **memblock 的对外 API 全家桶**:[include/linux/memblock.h](../linux-6.14/include/linux/memblock.h)。`memblock_alloc` / `memblock_alloc_low` / `memblock_alloc_node` 等都是 `memblock_alloc_internal`(见 [mm/memblock.c:1571](../linux-6.14/mm/memblock.c#L1571))的薄包装,差别只在 `min_addr`/`max_addr`/`nid` 的默认值——读完本章,这几个 API 你应该一眼就能看穿。
- **区间运算的引擎**:`for_each_free_mem_range` 背后的 `__next_mem_range`([mm/memblock.c](../linux-6.14/mm/memblock.c))是 A − B 区间运算的核心,值得单独抠一遍。
- **谁在启动期调用 memblock**:在内核里 `grep -r "memblock_alloc" init/ arch/x86/`,你会看到页表、per-cpu、ACPI、设备树解析……全是它的客户。这是理解"启动期到底分配了些啥"的最快路径。
- **移交的对面**:本章讲的是 memblock → 伙伴系统的方向。下一步请翻**第 5 章 · ZONE**,看伙伴系统接管这些页之后,第一件事为什么是"给房间分区"。

> 临时工棚已拆,材料就位。物业办公室正式挂牌——下一章,我们看它怎么给这栋楼的房间**分区**。
