# 第 5 章 · ZONE:把物理内存"分区"的理由

> **核心问题**:物理内存明明是一根从 0 排到底的连续地址线,为什么内核非要把它切成 DMA / DMA32 / NORMAL 几个区?直接用一个池子统一管不行吗?
>
> 这一章我们正式进入**物理侧的主力**。但你会发现,这一章真正在讲的不是"怎么分配",而是"**分配之前为什么要先分桶**"。ZONE 这层抽象,是在回答一个纯硬件的问题——**有些住户的"手够不到高层的房间"**。
>
> **读完本章你会明白**:
> - 为什么分区不是软件工程师的洁癖,而是被一类硬件约束(DMA 寻址能力)逼出来的;
> - 为什么区与区之间是"**有方向的降级回退**",而不是平等的"谁有空用谁";
> - 为什么除了按地址分区,还要在同一区内按"**能不能搬走**"再分一次类(migrate type)——这一刀,是为后面大页和回收铺路的伏笔。

> **如果一读觉得太杂**:先只记住三件事——① **ZONE 是给"手短的设备"留够得着的低层房间**(DMA/DMA32/NORMAL);② **分配时从高到低调级回退**(NORMAL→DMA),怕挤光低区再加 `lowmem_reserve` 储备闸;③ **每个区内部还按"能不能搬走"再分 migrate type**。这三件是后面伙伴系统、大页、回收的公共地基,其余字段细节第二遍配合 `/proc/zoneinfo` 再抠,不影响往下读。

---

## 章首·一句话点破

> **ZONE 的本质,是给"手短的设备"留出它们够得着的低层房间。**

第 1 章我们说过,物理 RAM 的第 4 个本性是"有物理拓扑"——它不是一块均匀的膏药。那一章点了一句:内核要"按物理位置把内存分区、分节点",NUMA 是分节点,而 **ZONE 就是分区**。这一章我们就把"分区"这件事彻底讲透。

先说一个反直觉的结论,把它刻在脑子里,这一章才读得顺:

> ⚠️ **ZONE 不是按"谁用好"分的,而是按"谁能用"分的。** DMA、DMA32 这些区的存在,不是因为那里的房间"更好",而是因为有些设备**只能**用那里的房间。这是一条被硬件焊死的约束,软件绕不过去。

为什么绕不过去?我们从根上的痛点讲起。

---

## 一、痛点:设备的"搬货之手"够不到高层

### 不这样会怎样:手短设备够不到的房间

回到大楼比喻。前面我们一直把分配内存说成"住户住进房间"。但有一类特殊的住户——**硬件设备**(网卡、磁盘控制器、老的声卡……)——它们要的不是"住",而是"**搬货**":把数据从自己的寄存器搬进 RAM,或从 RAM 搬出去。这个搬运动作,叫 **DMA(Direct Memory Access,直接内存访问)**。

问题来了:**设备搬货用的不是 CPU 的"手",而是自己的"手"——而设备的"手"往往比 CPU 短得多。**

- CPU 是 64 位的,它能给整个 64 位地址空间的任意一个物理地址发读写,够得着大楼里**任何一间房**。
- 但一个老式 **ISA 设备**,它的 DMA 引擎可能只有 **24 位**寻址能力——它只能发出 `0x00000000` 到 `0x00FFFFFF` 的地址,也就是**只能搬货到最低的 16MB**。再高的楼层,它的手根本伸不上去。
- 一个 32 位的 **PCI 设备**,只能寻址到 **4GB 以下**。

这就是设备的 **DMA mask(直接内存访问掩码)**——一个硬件属性,标明"这个设备最多能发出多少位的物理地址"。每个设备在驱动初始化时,会通过 `dma_set_mask()` 告诉内核自己手有多长。

> **比喻**:设备搬货像送货。CPU 是住顶楼还能跑遍全楼的精壮小伙;老 ISA 设备是只能爬到一楼的送货大爷;32 位设备是能爬到三楼的青壮年。**你给送货大爷指一间 8 楼的房间,他真的搬不上去**——不是不愿意,是腿脚够不着。

**不这样会怎样?** 如果内核只有一个统一的内存池,不管谁来要都随便挑一页发:

1. 某次分配,内核恰好把一页**高地址**的房间(比如 8GB 那一带)发给了一个老 ISA 设备做 DMA 缓冲。
2. 设备高高兴兴地开始往那块"内存"搬数据——可它的 DMA 引擎只能发 24 位地址,**它发出去的高位地址被硬件直接截断或忽略**,数据搬到了一个**完全错误的地方**(可能是别的设备寄存器、可能是 BIOS 区),也可能直接搬丢。
3. 结果:**数据损坏、设备行为异常、甚至系统崩溃**。而且这种 bug 极难排查——CPU 看内存一切正常,只有设备视角是错乱的。

所以问题很清楚:**不是所有房间都能给所有设备用。手短的设备,必须保证拿到的是它们够得着的房间。**

### 所以这样设计:按"够不够得着"把房间分区

既然约束是"低层房间是稀缺资源(手短设备只能用这些)",那最直接的办法就是:**把内存按物理地址高低切成几段,每段贴上标签,分配时按需取用。** 这就是 ZONE。

> **回扣第 1 章的本性 4**:RAM 有物理拓扑,不能假装均匀。ZONE 就是这条本性在"分配"层面的兑现——它**主动承认了"不同地址段的房间,可用性不同"**,而不是装作一视同仁。

那么切成几段、每段是什么?这不是拍脑袋,是跟着**历史上几代设备的 DMA 能力**来的。我们直接看代码怎么说。

---

## 二、切成几个区:`enum zone_type`

所有区的种类,定义在内核里一个枚举上:

> [include/linux/mmzone.h:747-836](../linux-6.14/include/linux/mmzone.h#L747-L836)

```c
enum zone_type {
#ifdef CONFIG_ZONE_DMA
	ZONE_DMA,
#endif
#ifdef CONFIG_ZONE_DMA32
	ZONE_DMA32,
#endif
	/*
	 * Normal addressable memory is in ZONE_NORMAL. DMA operations can be
	 * performed on pages in ZONE_NORMAL if the DMA devices support
	 * transfers to all of addressable memory.
	 */
	ZONE_NORMAL,
#ifdef CONFIG_HIGHMEM
	ZONE_HIGHMEM,
#endif
	ZONE_MOVABLE,
#ifdef CONFIG_ZONE_DEVICE
	ZONE_DEVICE,
#endif
	__MAX_NR_ZONES
};
```

注意三件事,它们直接对应设计意图:

1. **每个区都被 `#ifdef` 包着**。这不是偷懒,是刻意的:**不是所有架构都需要所有区**。一个 64 位、没有老破设备的现代 x86 机器,可能根本没有 `ZONE_DMA`(用 `CONFIG_ZONE_DMA=n` 编译掉)。区是**按需存在**的——没有"手短设备"就不必留低层区。这正是第 4 章那种"用临时的、够用的方案换取可靠/精简"的工程取舍在数据结构上的延续。
2. **注释里写得分明**:`ZONE_NORMAL` 的注释直说"DMA operations can be performed on pages in ZONE_NORMAL **if the DMA devices support transfers to all of addressable memory**"。翻译过来就是:**NORMAL 区的房间,只有"手够长"的设备能用**。一句话点破了分区的本质动机。
3. **枚举顺序 = 地址从低到高**。`ZONE_DMA` 在最前(最低地址),`ZONE_NORMAL` 在后。这个顺序后面会反复用到——它决定了降级回退的方向。

下面逐个看这些区是怎么来的。

### ZONE_DMA:一楼靠货梯的房间(≤ 16MB)

> [include/linux/mmzone.h:748-757](../linux-6.14/include/linux/mmzone.h#L748-L757) 注释:"`ZONE_DMA` ... is left for the ones with smaller DMA addressing constraints."

这是给**最手短**的设备留的——历史上 ISA 设备只能 DMA 到物理 **16MB 以下**。所以内核在最低的 16MB 里专门划一个区,凡是声明"我只能寻址到 16MB"的设备,DMA 缓冲区一律从这里拿。

> 这就是导言表格里"一楼靠货梯的房间"。它是最稀缺的:16MB 在动辄几十 GB 的机器上,只是九牛一毛。可它一旦被普通分配占光,所有依赖它的老设备就全瘫了。这就引出了后面"降级回退"和"保护"的必要性。

### ZONE_DMA32:1~3 楼(≤ 4GB)

> 同上注释:"`ZONE_DMA32` ... is used [when] this area covers the whole 32 bit address space."

给 **32 位 PCI 设备**留的——它们能 DMA 到 **4GB 以下**。x86-64 上,这个区通常覆盖物理地址 `0` ~ `4GB`。

为什么 DMA 和 DMA32 要**分开**?注释里说得很清楚:"Some 64-bit platforms may need **both** zones as they support peripherals with **different** DMA addressing limitations." 同一台机器上可能**同时**有只能到 16MB 的老设备和只能到 4GB 的次新设备,它们的能力不同,所以区也得拆开。**一个区对应一档 DMA 能力,一一映射,不混。**

### ZONE_NORMAL:4 楼以上(普通内存)

这是绝大多数内存待的地方,也是绝大多数分配走的地方。CPU 内核自己、普通进程的物理页,基本都从这儿来。注释里那句"DMA 能否在这里做,取决于设备手够不够长"已经说尽。

### ZONE_HIGHMEM:32 位内核的"够不到的高区"(现已少见)

> [include/linux/mmzone.h:770-780](../linux-6.14/include/linux/mmzone.h#L770-L780) 注释:"A memory area that is only addressable by the kernel through mapping portions into its own address space."

这一段很有意思,它体现的是**另一种**"够不着"——不是设备够不着,而是**内核自己够不着**。在 32 位系统(i386)上,内核虚拟地址空间只有 1GB 左右(用户占 3GB),而物理内存可能有好几 GB。内核**没法把所有物理页都同时映射进自己那 1GB 的窗口**,所以"高出 1GB 那部分物理内存"就得特殊对待:用的时候临时映射进来,用完拆掉。这部分就是 `ZONE_HIGHMEM`。

> 这一条提醒我们一个常被忽略的事实:**"够不着"是双向的**——不仅设备可能够不到 RAM,在某些架构上内核自己都可能够不到全部 RAM。ZONE 把这两种"够不着"都纳入了管理。64 位系统内核地址空间大得很,HIGHMEM 基本退出舞台(所以它也被 `#ifdef CONFIG_HIGHMEM` 关着)。

### ZONE_MOVABLE / ZONE_DEVICE:为"非分配"目的存在的区

这两个区要特别说明,因为它们**不是为了 DMA**:

- **ZONE_MOVABLE**:它和 NORMAL 一样是普通可用内存,但被**约定只放"可搬走"的页**。它存在的目的是为了**热插拔内存、offlining、以及给大页腾地**。简单说:预留一批"住户随时能搬走"的房间,将来要腾整层时方便(详见本章后半 migrate type 和第 13 章大页)。
- **ZONE_DEVICE**:给设备特有的内存(比如显卡的专用显存、持久内存 PMEM)用,由设备驱动管理,**不参与正常分配**。

这两个区告诉我们一个更宽的视角:**ZONE 已经从最初的"为 DMA 分区",演化成了"为一切需要按类隔离的内存需求分区"**。每一类特殊需求,内核都倾向于给它一个专属的区——这就是第 1 章讲的"用分类对抗约束"这一设计哲学的活样板。

---

## 三、为什么是"降级回退",而不是"谁有空用谁"

区划分好了,下一个问题来了:**一次分配,该去哪个区找?** 这里有个关键设计,也是初学者最容易想错的地方。

### 不这样会怎样:平等的池子会"反噬"稀缺区

设想一个**天真**的方案:所有区平等,分配时"哪个区有空就从哪个区拿"。

听起来公平高效,可它有个致命后果。看这个场景:

1. 系统启动,`ZONE_DMA`(低 16MB)和 `ZONE_NORMAL`(4GB 以上)都有大量空闲。
2. 内核做大量**普通分配**(建页表、分配 `task_struct` 之类,这些根本不需要在低地址)。在天真方案下,分配器可能随手就从 `ZONE_DMA` 拿了一页——因为那里正好有空。
3. 久而久之,`ZONE_DMA` 被这些"根本不需要低地址的普通分配"占满了。
4. 这时候,一个老 ISA 设备要做 DMA,它**只能**用 `ZONE_DMA`——结果发现全被占光了。设备要么死等、要么直接失败。

> **比喻**:一楼靠货梯的房间本该留给送货大爷。可物业图省事,"哪层有空就用哪层",把一堆不急着用电梯的普通住户塞进了一楼。等送货大爷真来了,发现一楼满员,他爬不上楼,货送不进去——而 4 楼明明空着一大片,他要是能爬早就自己去了。

**病根**:稀缺资源(`ZONE_DMA`)被不稀缺的普通需求**挤占**了。平等的池子,保护不了稀缺。

### 所以这样设计:有方向的"降级回退"

内核的真正方案是给区之间定**优先级和方向**:

> **核心规则:一次分配,先试"最理想"(最高地址、最不稀缺)的区;只有当理想区没货时,才"降级"借较低地址的区。而且这条降级是单向的。**

具体说:

- 一个**普通分配**(标记为 `GFP_KERNEL`,绝大多数内核分配都是它),理想区是 `ZONE_NORMAL`。它**先试 NORMAL**。
- 如果 NORMAL 没货,**允许**降级去 `ZONE_DMA32`、再不行去 `ZONE_DMA`——毕竟低区的房间当普通房间用,功能上没问题。
- 但一个**需要 DMA 的分配**(比如标记了 `GFP_DMA`),它的理想区就是 `ZONE_DMA`,它**只会**从 DMA 区拿,**绝不会**升到 NORMAL 去——因为拿了它也用不了,设备够不着。

这就是导言里那句"**降级回退,而非随便挑**"的精确含义。方向是**从高到低**(从 NORMAL 往 DMA 借),**反过来不行**。这样,稀缺的低区平时尽量不动,只在真需要它的高优先级场景下才被消耗。

### 代码佐证:`gfp_zone()` 把意图翻成区号

分配请求是用一组标志位 `gfp_t`(Get Free Pages flags)表达的。比如 `GFP_KERNEL` 是普通内核分配,`GFP_DMA` 是要给老设备 DMA 用的。内核拿到这串标志,第一步就是**翻译出"最高允许到哪个区"**:

> [include/linux/gfp.h:132-141](../linux-6.14/include/linux/gfp.h#L132-L141)

```c
static inline enum zone_type gfp_zone(gfp_t flags)
{
	enum zone_type z;
	int bit = (__force int) (flags & GFP_ZONEMASK);

	z = (GFP_ZONE_TABLE >> (bit * GFP_ZONES_SHIFT)) &
					 ((1 << GFP_ZONES_SHIFT) - 1);
	VM_BUG_ON((GFP_ZONE_BAD >> bit) & 1);
	return z;
}
```

这里不必纠结位运算细节(那是为了把"标志位 → 区号"压成一次查表,纯属性能优化)。抓住两点:

- 它的输出是一个 `enum zone_type`,即**"允许分配的最高区"**(叫 `highest_zoneidx`)。`GFP_DMA` → 输出 `ZONE_DMA`;`GFP_KERNEL` → 输出 `ZONE_NORMAL`。
- 这个 `highest_zoneidx` 之后会被用来**裁剪候选区列表**:分配器只会去**这个区及比它更低的区**里找,绝不向上越界。

### 代码佐证:zonelist 是一张"排好序的降级清单"

那么"先试 NORMAL、不行再降级"这个顺序,在哪里维护?答案是 **zonelist(区列表)**——一张**启动时就排好序**的候选区清单。

分配时,核心函数 `get_page_from_freelist()` 会用一个宏遍历这张清单:

> [mm/page_alloc.c:4365-4366](../linux-6.14/mm/page_alloc.c#L4365-L4366)(`__alloc_pages_slowpath` 重置候选起点)

```c
ac->preferred_zoneref = first_zones_zonelist(ac->zonelist,
				ac->highest_zoneidx, ac->nodemask);
```

`first_zones_zonelist` / `next_zones_zonelist` 这一组函数(内联版本在 [include/linux/mmzone.h:1684-1690](../linux-6.14/include/linux/mmzone.h#L1684-L1690),底层实现在同文件 1665 行起的 `__next_zones_zonelist`),做的事情就是:**从 zonelist 里,取出 `highest_zoneidx` 及以下的区,按预先排好的顺序一个个试**。

> zonelist 的排序逻辑在 [mm/page_alloc.c:5163-5195](../linux-6.14/mm/page_alloc.c#L5163-L5195) 的 `build_zonelists()` 里。它在启动时为每个 NUMA 节点建一张清单:**本节点的区排在最前(就近),然后才是其他节点**;同一节点内,**NORMAL 在前、DMA32、DMA 在后**(降级)。这张清单一旦建好,分配时就照着走,既尊重了"降级回退",也顺带实现了 NUMA 的"就近优先"(第 13 章展开)。

一句话:**zonelist 把"降级方向"和"就近优先"这两条规则,固化成了一张静态排好序的候选名单。分配时不用每次重新算,顺着名单走即可——又快又对。**

---

## 四、保护稀缺区:lowmem_reserve(低端区预留)

降级回退解决了"普通分配尽量不碰低区",但还不够彻底。还有一种情况:**NORMAL 全空了**,普通分配降级到 DMA 区借——这本身是允许的。但要是无节制地借,依然会把 DMA 区借光,留给真需要它的设备没货。

所以内核还要再加一道闸:**给低端区留一笔"应急储备"**,普通分配降级过来时,不准动用这笔储备;只有真正需要该区的请求(以及系统告急时)才能用。

这道闸就是 `lowmem_reserve`(低端区预留),它存在每个 `zone` 里:

> [include/linux/mmzone.h:852-861](../linux-6.14/include/linux/mmzone.h#L852-L861)(`struct zone` 内,字段上方就是内核白话解释)

```c
	/*
	 * We don't know if the memory that we're going to allocate will be
	 * freeable or/and it will be released eventually, so to avoid totally
	 * wasting several GB of ram we must reserve some of the lower zone
	 * memory (otherwise we risk to run OOM on the lower zones despite
	 * there being tons of freeable ram on the higher zones). ...
	 */
	long lowmem_reserve[MAX_NR_ZONES];
```

这段注释本身就是"为什么不这样会出事"的最佳注脚:**高层区明明还有大把可回收的内存,却可能因为低层区被借光而 OOM**——所以必须给低层区留底。

它的"比例"由一张全局表定:

> [mm/page_alloc.c:228-240](../linux-6.14/mm/page_alloc.c#L228-L240)

```c
static int sysctl_lowmem_reserve_ratio[MAX_NR_ZONES] = {
#ifdef CONFIG_ZONE_DMA
	[ZONE_DMA] = 256,
#endif
#ifdef CONFIG_ZONE_DMA32
	[ZONE_DMA32] = 256,
#endif
	[ZONE_NORMAL] = 32,
#ifdef CONFIG_HIGHMEM
	[ZONE_HIGHMEM] = 0,
#endif
	[ZONE_MOVABLE] = 0,
};
```

这个比例怎么读?它表示:**"针对某高端区的降级分配,低端区必须保留 `free / ratio` 这么多页不被碰"**。`ZONE_DMA` 比例是 256,意思是 DMA 区的空闲页里,每 256 页中要留出 1 页作为储备(对从 NORMAL 降级下来的请求不可见)。

**不这样会怎样?** 没有这道储备闸,普通分配一旦降级就会像决堤一样把 DMA 区吃干,真要 DMA 的设备瞬间断粮。有了它,即便 NORMAL 全空、降级在发生,DMA 区也始终留着一层"保命钱"。

> 这道闸和降级回退是**一对**:降级回退是"尽量别用稀缺区"(软的,靠优先级),lowmem_reserve 是"就算用了也得给真主留底"(硬的,靠预留量)。两者合起来,才算把"稀缺区被挤占"这个痛点堵实了。

---

## 五、再切一刀:migrate type(在同一区内按"能不能搬走"分组)

到这里,ZONE 的主线(DMA 寻址 → 分区 → 降级 → 保护)已经完整。但 ZONE 的故事还没完——内核在**每个区内部**,又切了第二刀。这一刀和 DMA 无关,是为了**对抗碎片**和**服务大页**。

### 痛点:物理碎片——能用的房间够,但凑不出一整片连续的

回顾第 1 章:伙伴系统管的是"几页连续的物理内存"。它最小发一页(4KB),最大发一大块。可问题是,**长期频繁地分配/释放**之后,物理内存会变成这样:

```
空闲页散布在:  _空_占_空_占_占_空_占_空_占_空_占_占_空_
                  ↑这些"空"加起来够,但彼此不挨着
```

明明空闲页总数够,可它们**被占用的页隔开,凑不出一段连续的大块**。这叫**外部碎片(external fragmentation)**——空闲总量够,但形不成连续。

**不这样会怎样?** 伙伴系统要分配一大块连续页(比如给一个大页,或一段 DMA 连续缓冲),却发现虽然总空闲够,但没有一处是连着的——分配失败。这对需要连续物理内存的场景(大页、设备 DMA 缓冲、`alloc_contig_range`)是致命的。

> **比喻**:大楼要腾出一整层给一位大客户(大页)。可这一层里东一间西一间都住了人,虽然全楼空房总数绰绰有余,但这层就是凑不齐一整片。想把零散住户搬去和别处合并?——多数住户好说话(他们的数据能迁),少数钉子户搬不动(比如内核里某些必须固定物理位置的页)。

### 所以这样设计:把"能搬的"和"不能搬的"分桶

既然碎片难处理在"有些页能搬、有些不能搬",那最朴素的办法就是:**从一开始就把它们分开存放,别让钉子户混进大片可搬区**。

这就是 **migrate type(迁移类型)**:

> [include/linux/mmzone.h:48-71](../linux-6.14/include/linux/mmzone.h#L48-L71)

```c
enum migratetype {
	MIGRATE_UNMOVABLE,    /* 不能搬:内核页表、per-cpu 数据等固定页 */
	MIGRATE_MOVABLE,      /* 能搬:进程的匿名页、page cache 等 */
	MIGRATE_RECLAIMABLE,  /* 可回收:脏页缓存等,需要时能丢/写回 */
	MIGRATE_PCPTYPES,     /* per-cpu 列表用到的类型数边界 */
	MIGRATE_HIGHATOMIC = MIGRATE_PCPTYPES,
#ifdef CONFIG_CMA
	MIGRATE_CMA,          /* 给 CMA(连续内存分配)预留 */
#endif
#ifdef CONFIG_MEMORY_ISOLATION
	MIGRATE_ISOLATE,      /* 正在被隔离迁移,不参与分配 */
#endif
	MIGRATE_TYPES
};
```

三类是主力,搞清这三个就够:

- **MIGRATE_UNMOVABLE(不可移动)**:这些页在物理上**必须固定**,搬不得。比如内核自己的页表、`kmalloc` 出来的内核数据结构——它们的物理地址被各种指针记着,搬了就乱套。
- **MIGRATE_MOVABLE(可移动)**:这些页**内容可以搬到别处再更新映射**。进程的匿名页、page cache 大多属于这类——它们通过页表访问,搬个家只要改页表条目就行。
- **MIGRATE_RECLAIMABLE(可回收)**:不一定要搬,可以直接**丢掉**(回收),比如干净的文件缓存页。

伙伴系统在**每个区(zone)的每一档(order)上,都为每种 migrate type 维护一条独立的空闲链表**。分配时,先按请求的 migrate type 找对应的链表。

> **为什么这么分?** 关键在**隔离**:**不可移动的页(钉子户)被集中放在它们自己的区域,绝不会混进大片可移动区**。这样,当将来要腾出一整片连续大块(给大页、给 CMA、给热插拔下线)时,要搬的只是"可移动"那批——而它们天生好搬。**把难题(搬不动)和好题(搬得动)从一开始就分到不同考场,这是用"分类"对抗碎片的典型手法。**

这就是第 1 章"用分类对抗约束"那条哲学的第二次亮相(第一次是 ZONE 本身按 DMA 能力分类)。**ZONE 是按"谁能用"分,migrate type 是按"能不能搬"分,两刀正交,组合出一张二维的分类网。**

### 这一刀的回扣:为后面铺路

migrate type 现在看可能像"多此一举",但它是后面几章的地基:

- **第 6 章(伙伴系统)**:分配/释放时怎么在 migrate type 之间回退(`MIGRATE_MOVABLE` 区没了,能不能临时去 `UNMOVABLE` 区拿?有讲究的"偷页块"逻辑),就在这层之上。
- **第 13 章(大页)**:透明大页要 2MB 连续,只有在 `MOVABLE` 占主导的区域才容易凑齐——正是 migrate type 隔离的功劳。
- **内存回收/迁移**(第 12 章及 CMA):把 `MOVABLE` 页搬走腾出连续块,前提就是它们被分了类、好搬。

**伏笔先埋这儿。你只要记住:ZONE 是物理侧的"第一刀分类"(按 DMA),migrate type 是"第二刀分类"(按可移动性)。**

---

## 六、关键源码精读:`struct zone` 长什么样

讲了这么多设计,我们落到代码上,看一个 `zone` 到底装了哪些东西。你不必现在全懂,但要能在脑子里建立"这个结构体 = 这一区房间的管理台账"的直觉。

> [include/linux/mmzone.h:842-1028](../linux-6.14/include/linux/mmzone.h#L842-L1028)

> 下面这个代码块是**简化示意**(为讲解而**挑选并重排**了字段,非源码原顺序;字段名与类型逐字取自真实结构体)。真实结构体字段更多、带大量 `#ifdef`,想看原貌请点上面链接。

```c
struct zone {
	/* zone watermarks, access with *_wmark_pages(zone) macros */
	unsigned long _watermark[NR_WMARK];          /* ① 水位线:min / low / high */

	struct free_area	free_area[NR_PAGE_ORDERS]; /* ② 伙伴系统核心:空闲链表
	                                                  每档 order × 每种 migrate type 一条 */

	struct pglist_data	*zone_pgdat;              /* ③ 反向指针:我属于哪个节点 */
	struct per_cpu_pages	__percpu *per_cpu_pageset; /* ④ per-cpu 快通道 */

#ifndef CONFIG_SPARSEMEM
	unsigned long		*pageblock_flags;         /* ⑤ 每个 pageblock 的 migrate type 记在这 */
#endif

	/* —— 这一区的"三种页数" —— */
	unsigned long		zone_start_pfn;           /* 本区起始页帧号 */
	unsigned long		spanned_pages;           /* 跨度:含空洞的总地址范围 */
	unsigned long		present_pages;           /* 实存:去掉空洞后真实存在的 */
	atomic_long_t		managed_pages;           /* 受管:实际交给伙伴系统的 */

	long lowmem_reserve[MAX_NR_ZONES];           /* ⑥ 低端区储备(本章第四节) */
	/* ... */
};
```

逐个对应我们讲过的设计:

- **② `free_area[NR_PAGE_ORDERS]`** —— 这是伙伴系统的**主战场**。数组大小 `NR_PAGE_ORDERS = MAX_PAGE_ORDER + 1 = 11`(对应 order 0~10,即 4KB ~ 4MB)。每档下又按 migrate type 分链表。第 6 章整章就在讲这里怎么动。
- **① `_watermark[NR_WMRK]`** —— **水位线**(min/low/high)。它决定"这个区还算不算健康",驱动 kswapd 回收。这是第 12 章的核心,这里只先认个脸。
- **④ `per_cpu_pageset`** —— **per-cpu 快通道**。绝大多数分配/释放在这完成,几乎无锁。第 6 章会展开"为什么 per-cpu 能这么快"。
- **⑤ `pageblock_flags`** —— **migrate type 的存放处**。物理内存被切成一个个 pageblock(一大块 pageblock 默认是 `2^(pageblock_order)` 页,`pageblock_order` 通常取 `MAX_PAGE_ORDER - 1`,即一大页 2MB 的大小),每个 pageblock 的 migrate type 记在这片位数组里。这是本章第五节"第二刀分类"的物理载体。
- **⑥ `lowmem_reserve`** —— 本章第四节的"应急储备闸"。
- **三种页数** `spanned_pages` / `present_pages` / `managed_pages` —— 这三者的区分本身就是第 1 章本性 4(内存有洞、有拓扑)的代码化身:
  - `spanned_pages`:本区地址跨度包含的总页数(含空洞);
  - `present_pages`:扣掉物理空洞后,真实存在的;
  - `managed_pages`:再扣掉内核预留(reserved)后,**真正交给伙伴系统管**的。
  
  > 为什么三个都要?因为**一个区的地址范围内,可能既有物理空洞(被 MMIO 占了),又有内核预留(给内核代码、crash dump 等)**。分配器能动的只是 `managed_pages`,记账要用它;但报告"这区多大"要用 `spanned_pages`。三者的差,正是物理 RAM "不连续、不干净"这件事的账面体现。

**一句话**:`struct zone` 是"**这一片房间**"的完整管理台账:空闲怎么排(②)、健康与否(①)、快通道(④)、能搬与否(⑤)、给稀缺区留底(⑥)、有多大(三种页数)。后面第 6、12 章的几乎所有动作,都是在某个 `zone` 的这些字段上动。

> 想验证你机器上的 zone 实况?`cat /proc/zoneinfo`,每个区一段,水位线、各档空闲、lowmem_reserve 全在里面。配合本章看,会发现那些数字全是这里讲的结构体字段。

---

## 七、章末小结

用大楼比喻把这一章收口。

我们这一章回答的核心问题是:**物理内存平铺连续,为什么非要分区?** 答案不在软件,而在硬件——**有一类住户(设备)手短,搬货只够得着低层房间**。这逼出了"按地址高低切区"这第一刀:

- **ZONE_DMA / DMA32 / NORMAL**:一一对应几代设备的 DMA 寻址能力(16MB / 4GB / 全程)。这是给"手短设备"留的够得着的房间。
- **HIGHMEM**:另一种"够不着"——32 位内核自己够不到的高区(64 位已基本不用)。
- **MOVABLE / DEVICE**:ZONE 演化出的新身份,为热插拔/大页/设备专用内存服务——证明 ZONE 已从"为 DMA 分区"长成了"为一切需要分类的内存需求分区"的通用框架。

而区与区**不是平等竞争**,而是**有方向的降级回退**:普通分配从 NORMAL 出发,不够了才往下借(DMA32 → DMA),且绝不反向。怕稀缺的低区被普通分配挤光,又加了 **lowmem_reserve** 这道储备闸。两道关一起,把"稀缺资源被滥用"这个痛点堵死了。

接着,内核在每个区**内部**又切了第二刀——**migrate type**(不可移动/可移动/可回收),把"钉子户"和"好说话的住户"从一开始就分桶存放,为将来腾连续大块(大页、CMA、回收迁移)铺路。

> **回扣全书主线**:这一章 100% 是**物理侧**的事。ZONE 既不造幻觉、也不兑现支票,它干的是最实在的活儿——**承认物理房间"并不都一样好使",并按硬件约束把它们分桶管理**。它是第 1 章本性 4(RAM 有拓扑)在分配层的直接兑现。
>
> - **承前**:它接住第 4 章——memblock 把剩余内存移交时,内核正是按 ZONE 把它们归类挂进各自的 `free_area`(具体移交在第 6 章开头讲)。
> - **启后**:它给第 6 章(伙伴系统)搭好了舞台——伙伴系统的所有空闲链表、水位线、per-cpu 快通道,**全都挂在 `struct zone` 上**;也给第 13 章大页/CMA、第 12 章回收埋下了 migrate type 和水位线的伏笔。

### 记住这三句话

1. **ZONE 是按"谁能用"分,migrate type 是按"能不能搬"分——两刀正交的分类网。**
2. **分配是"降级回退"(NORMAL→DMA),不是"谁有空用谁"——为了护住稀缺的低区。**
3. **稀缺区还得加 `lowmem_reserve` 储备闸——降级是软保护,储备是硬保护。**

### 想继续深入,该往哪儿钻

- 直接读 `struct zone` 每个字段的注释([include/linux/mmzone.h:842-1028](../linux-6.14/include/linux/mmzone.h#L842-L1028)),内核注释写得相当白话,能补全我们略过的细节;
- 看 `enum zone_type` 的注释([include/linux/mmzone.h:747-836](../linux-6.14/include/linux/mmzone.h#L747-L836))和 `enum migratetype`([include/linux/mmzone.h:48-71](../linux-6.14/include/linux/mmzone.h#L48-L71)),把这两套分类的边界摸清;
- 想看"降级清单"怎么生成的,跳到 [mm/page_alloc.c:5163-5195](../linux-6.14/mm/page_alloc.c#L5163-L5195) 的 `build_zonelists()`;
- 实战观察:`cat /proc/zoneinfo`,对照本章结构体字段看真实数字。

> 下一章我们正式登上 ZONE 搭好的舞台——**第 6 章 · 伙伴系统:页级分配的核心算法**,看那个挂在 `struct zone` 上的 `free_area` 到底怎么靠"对半劈 + 对偶合并"做到 O(1) 分配、自动抗碎片。
