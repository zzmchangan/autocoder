# 第 5 章 · start_kernel:初始化大游行

> **前置**:你需要先读过[全书导言](全书导言-一篇看懂内核全貌.md)和[第 0 篇第 1 章](P0-01-第一性原理-为什么需要内核.md)——它们讲清了"内核是一个垄断特权的市政府、卖三个幻觉"。本章承接那个结论:市政府既然非存在不可,那它**开张那天**是怎么把一个个部门挂上线的?另外,本章紧接内存篇的 ch3/ch4([head_64.S 自举](P2-03-内核自举-head_64.S-与第一张页表.md)、[memblock](P2-04-memblock-启动期的临时分配员.md))——那两章讲的是"内核怎么进城、怎么搭工棚",本章讲的是"工棚搭好后,市政府怎么挂牌营业"。

> **核心问题**:`head_64.S` 把内核带进长模式、跳到 C 代码后,内核是怎么把"内存、调度、中断、时钟、控制台、VFS……"这么多部门**按什么顺序**一个个挂上线的?为什么必须是这个顺序、不能乱来?
>
> **读完本章你会明白**:启动不是魔法,而是一条**被依赖关系严格约束的、线性的、可读的调用链**;每个部门的"到岗"时刻都被它依赖的部门决定;以及成百上千个驱动是怎么靠 `initcall` 等级机制,在这条链的末尾被**批量、有序**地叫起来的。

> **如果一读觉得太难**:先只记住三件事——① `start_kernel` 是一个**线性的、两三百行的 C 函数**,一行挂一个部门;② 它**一开始就关中断**,因为中断处理依赖的东西还没建好;③ 队伍末尾的 `initcall` 机制把所有驱动的初始化分成 8 个等级(`pure→core→…→late`)逐级调用。其余细节第二遍配合源码再抠,不影响理解下一章。

---

## 章首·一句话点破

前两章(内存篇 ch3/ch4)我们看着内核完成了两件苦活:

- 在 `head_64.S` 里**搭临时页表、切长模式**,让自己能正常跑 C 代码;
- 靠 `memblock` 这个临时工**熬过启动期**,在伙伴系统就位前也能分配内存。

可走到这一步,这座城市依然**空空荡荡**:没有进程(连 0 号都还没"动起来")、没有调度器、中断是关着的、控制台还没通(连一行字都打印不出来)、VFS 连个影子都没有……市政府挂了牌,可**所有职能部门都还没到岗**。

那这些部门是**谁、按什么顺序**挂上线的?答案就藏在一个函数里:

> [`start_kernel()`](../linux-6.14/init/main.c#L896) —— 启动的**初始化大游行**。

它是整个内核里**最重要的函数之一**,也是最好读的之一:因为它几乎就是个**线性的调用清单**,一行挂一个部门。这一章我们就顺着这条线走一遍,搞清楚每个部门**为什么在这个时刻到岗、为什么不能更早或更晚**。

---

## 一、大游行的入口:谁第一个跳进了 `start_kernel`

先定位坐标。`start_kernel` 不是凭空被调用的,它有清晰的"上游"。

x86 上,`head_64.S` 完成自举(切长模式、跳到 C)后,控制权交到 [arch/x86/kernel/head64.c](../linux-6.14/arch/x86/kernel/head64.c) 里的 `x86_64_start_kernel`。它做一点 x86 专属的早期设置(清理 bss、加载早期 GDT/IDT、解析 boot_params……),然后一路调到 `x86_64_start_reservations`,在那里**正式发起大游行**:

```c
void __init __noreturn x86_64_start_reservations(char *real_mode_data)
{
	/* version is always not zero if it is copied */
	if (!boot_params.hdr.version)
		copy_bootdata(__va(real_mode_data));

	x86_early_init_platform_quirks();

	switch (boot_params.hdr.hardware_subarch) {
	case X86_SUBARCH_INTEL_MID:
		x86_intel_mid_early_setup();
		break;
	default:
		break;
	}

	start_kernel();
}
```

([arch/x86/kernel/head64.c:499-516](../linux-6.14/arch/x86/kernel/head64.c#L499-L516))

注意最后一行:`start_kernel();`。从这一行开始,代码就**离开了 x86 专属世界、进入所有架构共享的通用启动流程**。这也是为什么 `start_kernel` 写在 [init/main.c](../linux-6.14/init/main.c) 而不是 `arch/` 下——它是**架构无关**的。不同 CPU 架构(x86、ARM、RISC-V……)的 `head_*.S` 各不相同,但它们最后都殊途同归,跳进同一个 `start_kernel`。

> **不这样会怎样**:如果启动逻辑分散在各个架构里、没有统一的 `start_kernel`,那每加一个新部门(比如 cgroup),就得在 x86/ARM/RISC-V 三套启动代码里各改一遍——重复、易错、不可维护。**把"挂部门"的逻辑提到架构无关层**,是内核"共用最大化、特化最小化"的一贯设计。

---

## 二、大游行的第一件事:把门锁上(关中断)

`start_kernel` 一进来,前几行就很关键:

```c
asmlinkage __visible __init __no_sanitize_address __noreturn __no_stack_protector
void start_kernel(void)
{
	char *command_line;
	char *after_dashes;

	set_task_stack_end_magic(&init_task);
	smp_setup_processor_id();
	debug_objects_early_init();
	init_vmlinux_build_id();

	cgroup_init_early();

	local_irq_disable();
	early_boot_irqs_disabled = true;
```

([init/main.c:895-909](../linux-6.14/init/main.c#L895-L909))

注意 [`local_irq_disable();`](../linux-6.14/init/main.c#L908) 这一行——**大游行一开始,先把中断关掉**,并设了个标志 `early_boot_irqs_disabled = true` 告诉全世界"现在中断是关着的,别乱来"。

### 不这样会怎样:这时候来一个中断,全完蛋

为什么开局就关中断?因为中断处理依赖一大堆**还没建好**的数据结构:

- 中断要找**中断处理函数**,可中断描述符表(IRQ descriptor)还没初始化;
- 中断处理可能要**分配内存**,可伙伴系统还没就位;
- 中断处理可能要**唤醒进程**,可调度器还没建好;
- 时钟中断要更新**时间**,可时钟子系统还没初始化。

这时候要是开着一个硬件中断(比如网卡突然来了个包、时钟走了一格),CPU 会去查中断处理函数——查到的要么是空的、要么是还没准备好的,直接**panic**。

> **比喻**:新城市开张,政府的**门铃系统**还没装好(接线员没到位、登记簿没印好)。这时候如果门铃响了(来个访客/报警),没人接、接了也没法处理,场面直接失控。所以开张仪式开始前,先把门铃**断电**,等接待部门全套就位了再通电。

### 所以这样设计:关中断 → 挂依赖 → 再开中断

`start_kernel` 的策略很清晰:**先关中断,把所有中断处理依赖的东西(陷阱表、内存、调度、RCU、IRQ 子系统、时钟)一个个挂好,然后才在中途打开中断**。我们待会儿会在源码里看到那个"开闸"的时刻([`local_irq_enable()`](../linux-6.14/init/main.c#L1025))。

---

## 三、大游行的主线:一行挂一个部门

现在我们顺着这条线往下走。`start_kernel` 的中间是一长串"挂部门"的调用,我按**功能分组**列出来(不逐行讲,只讲每一组**为什么在这个位置**):

### 第 1 组:先把"我是谁"搞清楚(架构 + 命令行)

```c
	pr_notice("%s", linux_banner);        /* 打印 "Linux version 6.14 ..." 那行横幅 */
	setup_arch(&command_line);            /* 架构专属初始化:读 e820、探测 CPU、early memblock 等 */
	...
	setup_per_cpu_areas();                /* 给每个 CPU 建一份 per-cpu 数据区 */
	smp_prepare_boot_cpu();               /* 启动 CPU 相关的钩子 */
```

([init/main.c:917-927](../linux-6.14/init/main.c#L917-L927))

[`pr_notice("%s", linux_banner)`](../linux-6.14/init/main.c#L917) 就是开机时你在屏幕/串口看到的**第一行**——`Linux version 6.14.0 ...`。这是城市开张的"鸣炮宣告"。

[`setup_arch(&command_line)`](../linux-6.14/init/main.c#L918) 是**架构专属的初始化大块**(x86 下在 [arch/x86/kernel/setup.c:732](../linux-6.14/arch/x86/kernel/setup.c#L732))。它干的事相当多:把 BIOS 传来的 e820 地图正式吸收、探测 CPU、做早期的 memblock 分配、解析内核命令行……注意——**内存篇讲的 e820/memblock,真正被"正式吸收"的动作就发生在这个 `setup_arch` 里**,所以它们天然排在 `start_kernel` 的最前面。

### 第 2 组:陷阱 + 内存 + 调度(三大地基)

```c
	trap_init();        /* 建异常/中断入口表(idt):除零、缺页、int 指令往哪跳 */
	mm_core_init();     /* ← 伙伴系统!正式分配器接管,memblock 退休 */
	...
	sched_init();       /* ← 调度器就位 */
```

([init/main.c:956-969](../linux-6.14/init/main.c#L956-L969))

这三行是整个大游行**最关键的三块地基**:

- **`trap_init()`**:建好"出事了往哪跳"的表。没有它,后面的缺页、系统调用、除零都没法处理。
- **`mm_core_init()`**:这就是**伙伴系统接管**的时刻(6.14 里这个函数名取代了老的 `mm_init()`)。从这一行起,memblock 退场、`alloc_pages`/`free_pages`/`kmalloc` 这些正式分配接口全部可用。**整个内核的内存分配,从这一行开始才"正经"起来**。
- **`sched_init()`**:调度器就位。从这一行起,内核才**有能力管理"谁此刻占用 CPU"**——这是后面创建任何进程的前提。

> **不这样会怎样**:这三块必须**先于**任何"会分配内存 / 会创建进程 / 会触发异常"的动作。如果调度器还没建好就去创建进程,新进程无处安放(没有运行队列);如果伙伴系统还没就位就去 `kmalloc`,直接崩。所以这三行**必须排在很前面**,而且顺序基本是固定的(陷阱 → 内存 → 调度)。

### 第 3 组:RCU + 中断 + 时钟(并发与时间)

```c
	rcu_init();         /* RCU(读多写少的无锁同步原语)就位 */
	...
	early_irq_init();   /* 先建一部分 IRQ 描述符 */
	init_IRQ();         /* ← 正式中断子系统就位 */
	tick_init();
	init_timers();
	hrtimers_init();
	softirq_init();     /* 软中断就位 */
	timekeeping_init();
	time_init();        /* 真正把硬件时钟挂上来 */
```

([init/main.c:990-1010](../linux-6.14/init/main.c#L990-L1010))

这一组建的是**并发和时间**的根基:RCU(后面同步篇主角)、中断子系统(边界篇主角)、定时器、软中断、时钟。注意它们都排在**调度器和内存之后**——因为这些子系统要分配内存、要注册回调,得等前面就位。

### 第 4 组:开闸!——中断终于可以开了

走完上面三组,中断处理依赖的东西**全部就位**了(陷阱表、内存、调度、IRQ 子系统、时钟都有了)。于是:

```c
	early_boot_irqs_disabled = false;
	local_irq_enable();     /* ← 大游行到这里,终于打开中断! */
```

([init/main.c:1024-1025](../linux-6.14/init/main.c#L1024-L1025))

`local_irq_enable()` 这一行,是启动过程中一个**心理上的里程碑**:从这一刻起,硬件可以打断 CPU 了,中断处理函数能正常工作。前面关着中断憋的一大段路,到这里"开闸放水"。

### 第 5 组:控制台 —— 终于能打印了

```c
	/*
	 * HACK ALERT! This is early. We're enabling the console before
	 * we've done PCI setups etc, and console_init() must be aware of
	 * this. But we do want output early, in case something goes wrong.
	 */
	console_init();
```

([init/main.c:1029-1034](../linux-6.14/init/main.c#L1029-L1034))

注意这段注释——作者自己都标了 `HACK ALERT!`。控制台本该等所有硬件都探测完再开,但**等不及**:万一后面哪步 panic 了,没有控制台你就连死因都看不到。所以这里**故意提前**开控制台,代价是 `console_init` 必须容忍"很多东西还没就绪"。

> **工程味道**:这段注释是内核里少见的"坦白型"注释——它不粉饰,直接告诉你"这是个 hack,但我们权衡过了,值得"。启动代码里这种"宁可丑也要早"的取舍随处可见,和内存篇 memblock 的"极简凑合"是同一个精神。

### 第 6 组:文件、凭证、命名空间等用户态地基

```c
	fork_init();          /* 给进程创建子系统初始化(最大进程数等) */
	...
	vfs_caches_init();    /* VFS 的 slab cache 建好(inode/dentry/file 的样板间) */
	pagecache_init();
	signals_init();
	...
	proc_root_init();     /* /proc 挂起来 */
	cgroup_init();        /* cgroup(容器配额的根基)就位 */
```

([init/main.c:1075-1090](../linux-6.14/init/main.c#L1075-L1090))

这一组建的是**面向用户态的地基**:`fork_init`(进程怎么生)、`vfs_caches_init`(文件系统的样板间,回扣内存篇 ch7 的 slab)、`proc_root_init`(`/proc` 这个"伪文件系统")、`cgroup_init`(容器篇主角)。

> 注意 `vfs_caches_init`——它内部会调用 slab 的接口,为 `inode`、`dentry`、`file` 这些高频对象**建专属的 `kmem_cache`**(内存篇 ch7 讲过的"样板间工厂")。这一步把"内存管理的预制件"和"文件系统的对象"接上了——**跨子系统的衔接点,往往就藏在这些 `*_init` 的调用里**。

### 大游行的终点:调用 `rest_init`

```c
	/* Do the rest non-__init'ed, we're now alive */
	rest_init();
```

([init/main.c:1098-1099](../linux-6.14/init/main.c#L1098-L1099))

注释里那句 `we're now alive`("我们现在活了")点睛——走到这里,所有部门到岗,城市**正式活了**。`rest_init` 干的是**最后一件、也是最开天辟地的一件**事:**亲手生出第一个市民**(0/1/2 号进程)。那是**下一章**的主角。

---

## 四、`start_kernel` 为什么是"线性"的

到这里你可能有个疑问:这条大游行**为什么这么线性**?一行挂一个部门,像列清单一样。现代计算机不是讲究并发吗,这些部门能不能**并行挂载**、加快启动?

**不能。** 原因就一个字:**依赖**。

### 不这样会怎样:并行挂部门 = 抢跑 = panic

设想一下,如果把"建内存"和"建调度器"丢给两个 CPU 并行做。会出现什么?

- 建调度器的人要 `kmalloc` 一块运行队列内存——可伙伴系统还没就位,崩。
- 建内存的人要注册一个回调——可能触发调度,可调度器还没就位,崩。

每个部门都**假设它依赖的部门已完成**。这种依赖是**单向、刚性、全连**的:几乎所有部门都依赖"内存 + 调度 + 中断"这三块地基,而这三块地基之间又互相依赖。一旦并行,竞态无处不在,而且**启动期是最没能力处理竞态的时候**(锁、RCU 都还在建)。

> **比喻**:新城市开张,通水、通电、通气这三件事不能并行——因为通气的工人要用电焊机(得先通电)、通水的工人要测试管道(得先通气烧锅炉)。每一步都踩着前一步的肩膀。强行并行,就是工人互相抢工具、抢场地,现场大乱。

### 所以这样设计:宁可慢、宁可丑,也要可预测

`start_kernel` 选择了**最朴素、最可读**的方案:一条线、顺序调用、两三百行摊开。这样做的回报是巨大的:

- **可读**:任何人顺着读一遍,就知道部门挂载的全貌;
- **可预测**:任何一步用到的东西,都保证在更上面已经初始化过——不会有时序相关的玄学 bug;
- **可调试**:启动卡住或 panic 了,看最后打印的日志就知道卡在哪一步。

这就是内核启动代码的**最高准则:可靠优先于性能、可读优先于优雅**。启动只发生一次(机器开机),慢一点、丑一点没关系;但**绝不能错**——错了机器就开不了机,什么性能都无从谈起。

> **回扣主线**:这跟内存篇 memblock 的"极简凑合"、伙伴系统的"O(1) 换复杂度"是一脉相承的工程哲学——**在约束最紧、容错最低的地方,选择最朴素、最不容易出错的方案,把复杂性和性能优化留给约束宽松的运行期**。

---

## 五、群众智慧:initcall —— 让驱动自己登记

大游行的主线讲完了,但还有**一个巨大的尾巴**没讲:成百上千个**驱动**的初始化,谁调用的?

你想想,内核里光驱动就有几千个(网卡、显卡、USB、文件系统……)。每个都要在启动时初始化自己。如果让 `start_kernel` 一个个手写调用:

```c
// 假想的、绝不存在的写法
e1000_init();
ext4_init();
usb_init();
nvme_init();
... (几千行)
```

这会带来灾难:

- **不可扩展**:你每加一个驱动,就得改 `start_kernel`;
- **不可配置**:你编译时没选某个驱动,这条调用还在,链接报错;
- **顺序难管**:哪个驱动先初始化?驱动之间也有依赖(比如 SCSI 驱动要等 SCSI 总线驱动)。

内核的解法极其优雅,叫 **initcall 机制**。

### 让每个驱动"自我登记"

每个驱动作者在自己的驱动文件末尾写一个宏,比如:

```c
module_init(e1000_init);     /* 模块/驱动初始化函数登记 */
```

或者更细粒度地指定等级:

```c
pure_initcall(fn)     /* 等级 0:最早的,几乎不依赖任何东西 */
core_initcall(fn)     /* 等级 1:核心子系统级 */
postcore_initcall(fn) /* 等级 2 */
arch_initcall(fn)     /* 等级 3:架构相关 */
subsys_initcall(fn)   /* 等级 4:子系统级 */
fs_initcall(fn)       /* 等级 5:文件系统 */
device_initcall(fn)   /* 等级 6:设备/驱动(绝大多数驱动在这) */
late_initcall(fn)     /* 等级 7:最晚的,前面都跑完了才轮到它 */
```

([include/linux/init.h:298-313](../linux-6.14/include/linux/init.h#L298-L313))

这些宏在编译期做了一件事:把你的初始化函数**塞进内核镜像里一个特定的段**(`.initcallN_init` 段,N 就是等级)。**同一个等级的函数,链接器按链接顺序排进同一个段**。

> **不这样会怎样**:如果不用这种"段链接 + 运行时扫描"的机制,内核就只能在源码里维护一张巨大的初始化函数指针表,每加/删/配置一个驱动都得手动改这张表,而且无法处理"这个驱动没编译进去"的情况。initcall 把"登记"这件事**下沉到链接器**——驱动写不写、编不编进去,完全由配置和链接决定,核心启动代码一行都不用改。

### 运行时:大游行末尾,按等级批量调用

到了大游行的尾巴(在 `rest_init` 里、由 1 号进程 `kernel_init` 执行的 `do_basic_setup` 中),内核扫这些段,**按等级从低到高逐级调用**:

```c
static initcall_entry_t *initcall_levels[] __initdata = {
	__initcall0_start,
	__initcall1_start,
	__initcall2_start,
	__initcall3_start,
	__initcall4_start,
	__initcall5_start,
	__initcall6_start,
	__initcall7_start,
	__initcall_end,
};

/* Keep these in sync with initcalls in include/linux/init.h */
static const char *initcall_level_names[] __initdata = {
	"pure",
	"core",
	"postcore",
	"arch",
	"subsys",
	"fs",
	"device",
	"late",
};
```

([init/main.c:1283-1296](../linux-6.14/init/main.c#L1283-L1296))

这两个数组就是等级表:`initcall_levels[]` 是每个等级段的**起始地址**(由链接器脚本定义的符号),`initcall_level_names[]` 是每个等级的人话名字(给日志用)。

真正干活的循环在这里:

```c
static void __init do_initcalls(void)
{
	int level;
	size_t len = saved_command_line_len + 1;
	char *command_line;

	command_line = kzalloc(len, GFP_KERNEL);
	...
	for (level = 0; level < ARRAY_SIZE(initcall_levels) - 1; level++) {
		/* Parser modifies command_line, restore it each time */
		strcpy(command_line, saved_command_line);
		do_initcall_level(level, command_line);
	}
	...
}
```

([init/main.c:1322-1339](../linux-6.14/init/main.c#L1322-L1339))

最外层这个 `for (level = 0; ...; level++)` 就是核心:**从等级 0(pure)一路扫到等级 7(late),每一级把该级的所有 initcall 函数都跑一遍**。`do_initcall_level` 内部再遍历该等级段里的每一个函数指针,逐个调用:

```c
static void __init do_initcall_level(int level, char *command_line)
{
	initcall_entry_t *fn;
	...
	for (fn = initcall_levels[level]; fn < initcall_levels[level+1]; fn++)
		do_one_initcall(initcall_from_entry(fn));
}
```

([init/main.c:1307-1320](../linux-6.14/init/main.c#L1307-L1320))

而 `do_initcalls` 又是被 `do_basic_setup` 调的:

```c
static void __init do_basic_setup(void)
{
	cpuset_init_smp();
	driver_init();      /* 设备模型/sysfs 的地基 */
	init_irq_proc();
	do_ctors();
	do_initcalls();     /* ← 成百上千个驱动在这里被批量叫起 */
}
```

([init/main.c:1348-1355](../linux-6.14/init/main.c#L1348-L1355))

### 为什么是 8 个等级:用等级表达依赖

initcall 的等级**就是依赖关系的编码**:

- **pure(0)**:几乎不依赖任何东西,最早跑;
- **core/postcore(1-2)**:核心子系统,要早;
- **arch(3)**:架构相关初始化;
- **subsys(4)**:各种子系统(总线等);
- **fs(5)**:文件系统;
- **device(6)**:**绝大多数驱动在这**——它们要等总线、文件系统都就位了才能探测设备;
- **late(7)**:前面全跑完了才轮到它(比如一些需要"全局就绪"才能做的事)。

驱动作者根据自己的驱动**依赖什么**,选合适的等级。一个网卡驱动通常用 `module_init`(默认等同 `device_initcall`),因为它要等 PCI/USB 总线先就位;一个核心调度策略可能用 `core_initcall`,因为它得早。

> **比喻**:新城市开张,政府不可能一个一个手写"先叫水厂、再叫电厂、再叫煤气厂……"。它的办法是:每个单位在**报到表**上填自己的"依赖等级"(我需要水、需要电、还是什么都不需要),政府**按等级从低到高分批叫号**——什么都不依赖的先报到,依赖多的后报到。等级,就是依赖的化身。

> **一个边界细节**:initcall 在**等级内**的顺序,是由**链接顺序**决定的(即 `Makefile` 里 obj 列出的先后),不是严格定义的。所以**等级内的顺序不能被依赖**——驱动如果依赖"同等级的另一个驱动先跑",那就是 bug,必须靠别的机制(如 deferred probe,延迟探测)解决。这是 initcall 机制的一个边界,记一笔即可。

---

## 关键源码精读:`start_kernel` 的"开闸时刻"

我们把本章最核心的几行,从真实源码里摘出来逐行对一遍,看"依赖 → 顺序"是怎么落到代码上的。

### 片段一:开局关中断(锁门)

```c
asmlinkage __visible __init __no_sanitize_address __noreturn __no_stack_protector
void start_kernel(void)
{
	...
	local_irq_disable();
	early_boot_irqs_disabled = true;
```

([init/main.c:895-909](../linux-6.14/init/main.c#L895-L909))

- 函数那一长串修饰符里,`__no_stack_protector`(关栈保护)和 `__no_sanitize_address`(关 KASAN)很有意思——**启动这么早,连栈保护 canary、地址消毒器都还没就位**,所以这个函数必须显式关掉它们。这是"启动期什么都还没就位"的又一个证据。
- `local_irq_disable()` 就是关中断的硬件指令(x86 上是 `cli`)。从这一行起,本 CPU 不响应硬件中断,直到第 1025 行才开。

### 片段二:三大地基(陷阱 → 内存 → 调度)

```c
	trap_init();
	mm_core_init();
	...
	sched_init();
```

([init/main.c:956-969](../linux-6.14/init/main.c#L956-L969))

这三行的顺序就是**依赖顺序**:`trap_init` 建异常入口(后面任何缺页/异常都要它);`mm_core_init` 让伙伴系统接管(后面任何 `kmalloc` 都要它);`sched_init` 建调度器(后面创建任何进程都要它)。三者互为基础、顺序基本不可换。

### 片段三:开闸放水

```c
	early_boot_irqs_disabled = false;
	local_irq_enable();
```

([init/main.c:1024-1025](../linux-6.14/init/main.c#L1024-L1025))

注意它出现在 `init_IRQ()`/`init_timers()`/`time_init()` 这一组建中断+时钟的代码**之后**——因为这些就是"中断处理依赖的最后几块"。它们一就位,中断就可以安全地打开了。`local_irq_enable()` 是 x86 的 `sti` 指令。**从这一行起,硬件可以打断 CPU 了。**

### 片段四:终点

```c
	/* Do the rest non-__init'ed, we're now alive */
	rest_init();
```

([init/main.c:1098-1099](../linux-6.14/init/main.c#L1098-L1099))

`rest_init` 是 `start_kernel` 的最后一行。注释里 `__init` 是个关键字——它告诉编译器"这个函数只在启动期用,启动完可以把它的代码回收掉"。`rest_init` 特意**不带 `__init`**,因为它会生出新进程、自己变成永久的 idle 循环,不能被回收。下一章细讲。

---

## 章末小结

### 用城市比喻回顾本章

回到那座开张的城市。这一章我们跟着 **`start_kernel`** 这条**初始化大游行**走了一遍,看市政府怎么把一个个部门挂上线:

1. **锁门**(`local_irq_disable`):开张仪式开始前,先把门铃断电——接待部门还没就位,这时候来客会失控。
2. **三大地基**(`trap_init` / `mm_core_init` / `sched_init`):先建好"出事往哪跳""内存怎么分""谁用 CPU"这三块,后面所有部门都踩着它们。
3. **开闸**(`local_irq_enable`):中断依赖的东西全就位了,门铃通电,硬件可以打断 CPU 了。
4. **控制台**(`console_init`):哪怕是个 HACK,也要早点让屏幕能打印——万一后面崩了,至少能看到死因。
5. **群众智慧**(`initcall`):成百上千个驱动不靠手写调用,而是**自我登记 + 按等级批量叫起**,等级就是依赖的编码。
6. **终点**(`rest_init`):所有部门到岗,"we're now alive",接下来亲手生第一个市民。

### 本章在全书主线中的位置

记住全书导言的二分法——**内核一半在"管共享资源",一半在"造独占幻觉"**。启动期是这两半**从无到有**建立起来的过程:

- 大游行的前半段(陷阱、内存、调度、中断、时钟),是把"**管资源**"的能力一块块搭起来;
- 大游行的后半段(VFS、cgroup、proc),开始为"**造幻觉**"铺地基(文件幻觉、容器隔离的雏形);
- 而 `rest_init` 生出的 1 号进程 `init`,最终会把这些幻觉**交付给用户态**——那是下一章。

所以 `start_kernel` 不是一个孤立的"初始化函数",它是**全书所有子系统到岗的剪彩仪式**:你在后面每一篇(进程、调度、中断、文件……)学到的东西,**第一次"通电"的时刻,几乎都在这条大游行里**。记住这一点,后面读到任何子系统的初始化,你都能在脑子里定位它"在大游行的哪一段"。

### 五个"为什么"清单

如果你只能记五件事,记这五件:

1. **`start_kernel` 是谁叫的**:`head_64.S` 自举后 → `x86_64_start_kernel` → `x86_64_start_reservations` → `start_kernel`([head64.c:514](../linux-6.14/arch/x86/kernel/head64.c#L514))。它架构无关,各架构殊途同归。
2. **为什么开局关中断**:中断处理依赖的东西(陷阱表、内存、调度、IRQ 子系统、时钟)都还没建,这时来中断会 panic。关中断 → 挂依赖 → 第 1025 行才开。
3. **三大地基的顺序**:`trap_init` → `mm_core_init`(伙伴系统接管,memblock 退休) → `sched_init`。这个顺序是依赖逼出来的,基本不可换。
4. **为什么是线性不是并行**:部门间依赖刚性且全连,并行 = 抢跑 = panic。启动只发生一次,**可靠优先于性能**。
5. **initcall 是什么**:驱动用 `core_initcall`/`module_init`/`late_initcall` 等**自我登记**到对应等级段,内核在大游行末尾按 `pure→core→…→late` 八级**批量调用**。等级 = 依赖的编码,链接器负责排队,核心启动代码一行都不用为驱动改。

### 想继续深入,该往哪钻

- **读完整的 `start_kernel`**:[init/main.c:896-1108](../linux-6.14/init/main.c#L896-L1108)。它不长,顺着读一遍,你会对"部门挂载顺序"有全貌。每行的函数名,基本就是后面某一篇的主角。
- **读 `setup_arch`**:[arch/x86/kernel/setup.c:732](../linux-6.14/arch/x86/kernel/setup.c#L732)。内存篇的 e820/memblock 真正被"正式吸收"就在这里。
- **读 initcall 的等级定义**:[include/linux/init.h:282-313](../linux-6.14/include/linux/init.h#L282-L313),看 `__define_initcall` 怎么把函数塞进段。再配合 [init/main.c:1283-1355](../linux-6.14/init/main.c#L1283-L1355) 看运行时怎么扫段。
- **想看启动时每个 initcall 的执行情况**:启动参数加 `initcall_debug`,内核会在每个 initcall 前后打印一行,你可以亲眼看到几千个驱动是怎么按等级被叫起来的——这是理解本章最直观的办法。

---

> 大游行走完,所有部门到岗,城市"活了"。可城里**一个市民都还没有**。市政府怎么亲手生下第一个市民?为什么必须有 0/1/2 三尊祖宗?翻开 **第 6 章 · 第一个市民诞生:0 号进程与 init**。
