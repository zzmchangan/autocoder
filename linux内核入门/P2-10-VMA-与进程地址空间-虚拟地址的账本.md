# 第 10 章 · VMA 与进程地址空间:虚拟地址的"账本"

> **核心问题**:进程的虚拟地址空间里,代码段、数据段、堆、mmap 映射、栈,各占其位,互不越界。内核用什么来**记账**?更反直觉的是——你调一次 `malloc`、`mmap`,为什么**物理内存一点都不涨**,直到你真正去读写它?
>
> **读完本章你会明白**:
> - VMA 到底是什么,为什么它是"**圈地**"而不是"**占地**"——这是理解"延迟兑现"那套哲学的关键结构;
> - 一个进程的虚拟地址空间是怎么布局的,ASLR 为什么存在、省的是哪笔账;
> - 为什么 6.14 的内核用 **maple tree**(而不是很多人书上写的红黑树)来管理 VMA,这次替换解决了什么痛点;
> - `do_mmap` / `vma_link` 这条"圈地流水线"在代码里长什么样。

> **如果一读觉得太难**:先只记住三件事——
> ① **VMA = 一行账本**,记录"某段虚拟地址 `[vm_start, vm_end)` 是什么性质、什么权限、背后是谁",但**绝不**分配物理页;
> ② `malloc`/`mmap` 只**新增一行账本**(圈地),真房间要等 **page fault**(第 9 章)才兑现;
> ③ 一个进程的所有 VMA,装在一棵 **maple tree**(`mm->mm_mt`)里,方便按地址快速查找"这段归谁管"。
> 其余的字段细节、ASLR 位数、maple tree 内部结构,第二遍配合源码再抠,不影响往下读第 11 章。

---

## 章首·这一章是"虚拟侧账本"的登场

回到全书那条二分法:**物理一侧管"切房间",虚拟一侧管"造幻觉"**。

第 8、9 章我们讲了虚拟内存的"幻觉"本身(多级页表 + page fault 兑现)。但你有没有想过一个更基础的问题:

> 进程的虚拟地址空间是**一整片**从 0 到天文数字的连续门牌号(比如 x86-64 是 128TB)。这片空间里,代码段在一段、数据段在一段、堆往上长、栈往下长、还有一堆 mmap 的共享库和文件映射……**内核怎么记住"哪段门牌号是干什么用的"?**

答案就是本章的主角:**VMA**(`struct vm_area_struct`)。它是虚拟那一侧的**账本**——每一行记录"一段连续虚拟地址是什么性质、什么权限、背后是匿名内存还是文件"。

> **回扣导言的比喻**:第 8 步我们说过,VMA 是住户的"预定单"——"我预定 100~200 号门牌归我用"。物业记下这张预定单(**建一个 VMA**),但**不搬家具**。等住户真去用了(page fault),物业才往里搬真房间。这一章讲的就是这本"预定单台账"是怎么记、怎么存、为什么这么设计。

本章牢牢站在**虚拟一侧**。它不碰任何物理页,只管"门牌号的圈占和登记"。

---

## 一、VMA 是"圈地",不是"占地":为什么 malloc 不立即占内存

### 提出问题

你写一行 `p = malloc(100 * 1024 * 1024);` 要 100MB。然后立刻去看 `/proc/meminfo`,你会惊讶地发现:**物理内存几乎一点没少**。明明申请成功了啊,内存去哪了?

### 不这样会怎样?

如果 `malloc` 一调用就**真的**给你 100MB 物理页,会怎样?

- **立刻挤爆内存**:100 个进程每个 malloc 100MB(但实际只用了其中 1MB),物理内存当场被这堆"申请了却没用"的页占满,真正干活的进程反而拿不到内存。
- **白干功**:分配、清零、建页表项,全是代价不小的活。可这 100MB 里 99MB 可能永远不被访问——为永远不用的东西预先付清,纯属浪费。
- **fork 变成噩梦**:`fork()` 要复制父进程的整个地址空间,如果每个虚拟页都早配好了真房间,fork 就得逐页复制,慢到没法用(第 9 章的 COW 也就没了意义)。

### 所以这样设计:把"圈地"和"占地"彻底分开

内核的解法是一刀两断:

- **圈地(建账本)**:`malloc` → `mmap`/`brk` → 内核在进程的地址空间里**圈出一段虚拟地址范围**,在账本上**新增一行 VMA**。仅此而已。不分配物理页,不建页表项,不清零。
- **占地(兑现)**:等进程真的去读写那段地址里的某个字节 → 触发 **page fault**(第 9 章)→ 内核这才从伙伴系统要一页真房间,在页表里把这个虚拟页 ↔ 物理页关联上。

> **这就是"延迟兑现"哲学落到 VMA 上的样子**:VMA 是"空头支票的存根",它证明"这段虚拟地址我预定了、合法、有这些权限",但**不保证**背后此刻有真房间。房间是按需、一页一页、被访问到才配的。

所以 `malloc` 不占内存,是因为 `malloc` 只动**账本(VMA)**,不动**实物(物理页)**。100MB 的 VMA 在账本上只占几十个字节(一个 `struct vm_area_struct`),物理页等真用到了再说。

> 顺便澄清一个常见误解:很多人以为"`malloc` 是用户态的事,内核不掺和"。其实 `malloc`(glibc)只是个用户态的缓存层;当它自己的缓存不够、要向内核要地时,最终还是要发 `mmap`/`brk` 系统调用——**那一刻才进内核、才建 VMA**。所以"建 VMA"和"配物理页"是两次截然不同的事件,中间可能隔很久。

---

## 二、账本的一行长什么样:`struct vm_area_struct`

### 提出问题

VMA 既然是账本,它到底记了哪些字段?为什么是这些?

### 所以这样设计:每一行 VMA 记录"一段连续同性质的虚拟地址"

VMA 的核心定义在 [include/linux/mm_types.h:681](../linux-6.14/include/linux/mm_types.h#L681-L787)。我们挑关键字段看:

```c
struct vm_area_struct {
	/* The first cache line has the info for VMA tree walking. */

	union {
		struct {
			/* VMA covers [vm_start; vm_end) addresses within mm */
			unsigned long vm_start;
			unsigned long vm_end;
		};
		...
	};

	struct mm_struct *vm_mm;
	pgprot_t vm_page_prot;          /* Access permissions of this VMA. */
	...
	union {
		const vm_flags_t vm_flags;
		...
	};
	...
	const struct vm_operations_struct *vm_ops;

	/* Information about our backing store: */
	unsigned long vm_pgoff;		/* Offset (within vm_file) in PAGE_SIZE units */
	struct file * vm_file;		/* File we map to (can be NULL). */
	...
};
```

逐个对应它的"职责":

- **`vm_start` / `vm_end`**:这段 VMA 覆盖的虚拟地址范围 `[vm_start, vm_end)`。这是账本行的"**门牌号区间**"。注意是半开区间,长度 `= vm_end - vm_start`。注释特意把它们放在**第一个 cache line**——因为查账(按地址找 VMA)太频繁,必须让关键字段贴着 CPU 缓存。
- **`vm_mm`**:这个 VMA 属于哪个进程的地址空间(指向 `struct mm_struct`,一个进程一个 mm)。账本总要知道自己挂在谁名下。
- **`vm_page_prot` / `vm_flags`**:**权限**。`vm_flags` 是一组位标志,比如 `VM_READ`/`VM_WRITE`/`VM_EXEC`(可读/可写/可执行)、`VM_MAY*`(允许改成什么)。这就是台账上的"**权限章**"——第 9 章 page fault 判"这次访问合不合法",依据就是这页所属 VMA 的这些标志。
- **`vm_ops`**:一组回调函数(`struct vm_operations_struct`)。当这段 VMA 发生 page fault、被 munmap、被 fork 复制时,**不同类型的 VMA 行为不同**——匿名页和文件页的处理截然不同。`vm_ops` 就是这种多态的入口。这是账本行的"**操作手册**"。
- **`vm_file` / `vm_pgoff`**:**后端是谁**。如果这段是文件映射(比如 mmap 了一个文件或 .so 共享库),`vm_file` 指向那个文件,`vm_pgoff` 记录"这段虚拟地址对应文件的哪个偏移"。如果是匿名映射(堆、栈、malloc 的纯内存),`vm_file` 为 NULL。这是账本行的"**来源说明**"。

> **一句话总结账本行**:一个 VMA 回答五个问题——**哪段门牌**(`vm_start/end`)、**谁的**(`vm_mm`)、**什么权限**(`vm_flags`)、**背后是谁**(`vm_file`/匿名)、**出事怎么处理**(`vm_ops`)。

### 为什么一个 VMA 只描述"一段连续同性质"的地址?

注意 VMA 的粒度:**它只描述一段连续、性质统一的地址**。如果同一段地址里前半是可读、后半是可写,那就是**两个** VMA。如果两段相邻、性质又相同,内核会尽量**合并**成一个 VMA(省一行账)。

> **为什么不细到"每一页一行"?** 算笔账:一个进程动辄几万个虚拟页(几百 MB 地址空间),如果每页一个 VMA,账本本身就要几百 MB——还没干活,账本先把内存吃光了。VMA 用"一段一段"的粒度,把几万页压成几十~几百行账,既够精细(同段内权限一致)又够省。

### 代码佐证:看一个真实进程的账本

想看你机器上某个进程的账本?`cat /proc/<pid>/maps`,**每一行就是一个 VMA**:

```
00400000-0040b000 r-xp 00000000 fd:00 ...  /usr/bin/ls     ← 代码段(可执行)
0040b000-0040c000 r--p 0000a000 fd:00 ...  /usr/bin/ls     ← 只读数据
0040c000-0040d000 rw-p 0000b000 fd:00 ...  /usr/bin/ls     ← 可写数据
0060c000-0060e000 rw-p 0002b000 ...                         ← bss(匿名)
7f1234560000-7f123471a000 r-xp ... /lib/libc.so            ← 共享库代码
...
7ffe12340000-7ffe12361000 rw-p ... [stack]                 ← 栈
```

每行的格式就是:**起始-结束  权限  偏移  设备:inode  路径**。这正是 `struct vm_area_struct` 那几个字段的可读化:`vm_start`-`vm_end`、`vm_flags`(`rwxsp`)、`vm_pgoff`、`vm_file`。**`/proc/pid/maps` 就是这本账本的人类视图。**

---

## 三、地址空间的布局,以及 ASLR 为什么存在

### 提出问题

进程的 VMA 们在虚拟地址空间里是怎么排布的?为什么代码段在低地址、栈在高地址、mmap 夹在中间?还有那个 ASLR——为什么要把每次启动的地址都打乱?

### 不这样会怎样?

如果每个进程的代码段都固定从 `0x400000` 开始、栈都固定在某个地址、共享库都固定加载在同一个位置,会怎样?

- **安全灾难**:攻击者写漏洞利用(exploit)时,最想要的就是"**地址是确定的**"。返回到库函数(ret2libc)、覆盖返回地址这些经典攻击,都依赖"我知道 `libc` 的 `system()` 在哪个固定地址"。地址一固定,攻击者可以照着写死偏移,一把通杀成千上万台机器。
- **历史包袱固化**:固定布局一旦定下,以后扩展地址空间(从 32 位到 64 位)会很别扭。

### 所以这样设计:固定分区 + ASLR 随机化

一个进程的虚拟地址空间,大体是**固定分区**叠加**随机扰动**:

```
低地址
┌───────────────────────┐ 0x0000000000000000
│  代码段 .text          │   ← 可执行文件载入,通常带 ASLR 偏移
│  只读数据 .rodata       │
│  可写数据 .data/.bss    │
├───────────────────────┤
│  堆(heap)  ↑ 向上长     │   ← brk, malloc 小块常落这
│         (空)           │
├───────────────────────┤  ← mmap 区域(文件映射、共享库、大块匿名)
│  mmap 映射  ↓ 向下长    │     libc.so, ld.so, mmap()…
│  (随机起点)             │
├───────────────────────┤
│  栈(stack) ↓ 向下长     │   ← 主线程栈,高地址附近
│  [stack]               │
└───────────────────────┘ 高地址(接近 TASK_SIZE,如 128TB)
```

**固定分区**保证了基本秩序:代码在低、栈在高、堆和 mmap 各有方向(堆往上长、mmap/栈往下长),中间留大片空白给 page fault 按需填充。

**ASLR(Address Space Layout Randomization)** 再在每个分区上**加一个随机偏移**:每次进程启动,代码段、堆、mmap 区域、栈的起始位置都**随机挪动**一段。攻击者就没法预知"关键函数的确切地址"——他要么得先泄露地址(难度大增),要么得盲打(成功率骤降)。

ASLR 的"随机量"是有限的、可配置的位数。看代码,以 x86 为例:

```c
static unsigned long arch_rnd(unsigned int rndbits)
{
	if (!(current->flags & PF_RANDOMIZE))
		return 0;
	return (get_random_long() & ((1UL << rndbits) - 1)) << PAGE_SHIFT;
}

unsigned long arch_mmap_rnd(void)
{
	return arch_rnd(mmap_is_ia32() ? mmap32_rnd_bits : mmap64_rnd_bits);
}
```

([arch/x86/mm/mmap.c:71](../linux-6.14/arch/x86/mm/mmap.c#L71-L80))。`mmap_rnd_bits` 就是"随机几位",64 位默认 28 位(可调)。`arch_rnd` 取这么多位随机数,再左移 `PAGE_SHIFT`(因为地址必须页对齐)。这个随机偏移就是 ASLR 的"扰动量"。

> **ASLR 省的是哪笔账?** 它不省内存、不省时间,**省的是"确定性"**——用一段随机地址,把"地址可预测"这个攻击者赖以生存的前提打破。这是**用随机性换安全性**的设计哲学。代价是:地址不连续了,调试时要看 `/proc/pid/maps` 才知道确切位置(这点小麻烦换来的是大安全)。

---

## 四、VMA 怎么存:为什么 6.14 用 maple tree,而不是红黑树

### 提出问题

一个进程的 VMA 通常几十到几百个(忙时上千个)。内核要把它们存起来,能支持"给我地址 X,它属于哪个 VMA"这种**按地址查找**(这是 page fault、缺页判断的第一步)。用什么数据结构?

> ⚠️ **一个要澄清的过时印象**:很多老资料(和一些书的目录)会说"VMA 用红黑树 + 链表组织"。这在 **Linux 6.1 之前**是对的。但**本仓库的 6.14 已经不是了**——它换成了 **maple tree**。下面我们就讲这个替换背后的"为什么",这恰恰是理解 VMA 组织的最佳切入点。

### 不这样会怎样?

旧方案是 **红黑树(`mm->mm_rb`)+ 有序双向链表(`mm->mmap`)**:

- 红黑树:按 `vm_start` 排序,支持 O(log N) 的按地址查找、插入、删除。
- 链表:O(N) 顺序遍历(比如 `/proc/pid/maps` 要按序打印所有 VMA)。

这套方案用了二十多年,但有几个**长期痛点**:

1. **要维护两套结构**:同样的 VMA,既要挂进红黑树、又要挂进链表。每次插入/删除/合并/分裂,两边都得同步改,代码复杂、bug 多。
2. **锁粒度粗**:查找 VMA 要拿 `mmap_lock`(读写锁)。page fault 又极其频繁,所有 CPU 的 page fault 都要读这把锁,竞争激烈,在大规模多核、大内存(几万个 VMA)的服务器上成了**性能瓶颈**。
3. **"找空洞"很贵**:ASLR 要给 mmap 找一个"够大的空闲地址洞",在红黑树+链表下,这是个 O(N) 的扫描——遍历相邻 VMA 看缝隙。VMA 越多越慢。
4. **预分配困难**:红黑树节点是内联在 VMA 里的,插入时如果要分配新节点,可能在中途(持锁状态下)才触发内存分配,引发递归锁的麻烦。

### 所以这样设计:换成 maple tree

6.1 起,VMA 改用 **maple tree**(`struct maple_tree mm_mt`,见 [include/linux/mm_types.h:822](../linux-6.14/include/linux/mm_types.h#L822-L822))。它是一种 **B-tree 的变体**(叫 RCU-safe 的 maple tree),一个节点能装多个条目(不是红黑树那种二叉)。这个替换怎么化解上面四个痛点?

- **一套结构搞定**:maple tree **既**支持 O(log N) 的按地址查找(对 page fault 友好),**又**天然有序、支持高效顺序遍历(对 `/proc/pid/maps` 友好)。不再需要"树 + 链表"两套。
- **RCU 友好、利于无锁读**:maple tree 从一开始就为 **RCU 读**设计,这为后来引入的 **per-VMA lock**(CONFIG_PER_VMA_LOCK,让 page fault 大多数情况下**只锁单个 VMA、不碰 mmap_lock**)铺平了路——这是 6.x 缓解 `mmap_lock` 瓶颈的关键一招。
- **找空洞变快**:maple tree 的节点里**记录了区间的空洞信息**,ASLR/mmap 找空闲洞可以高效进行,不再是 O(N) 全扫。
- **支持预分配**:maple tree 提供 `mas_preallocate` 这类接口,允许**先在外层分配好节点内存,再持锁插入**,避开了"持锁时才发现要分配内存"的递归锁陷阱。

> 看 VMA 结构体的注释就知道它已经为 maple tree 量身定制:开头那句 `/* The first cache line has the info for VMA tree walking. */`([mm_types.h:682](../linux-6.14/include/linux/mm_types.h#L681-L687)),就是把 `vm_start/vm_end` 这些"树遍历最常用"的字段**塞进第一个 cache line**——因为 maple tree 的遍历会频繁访问它们。VMA 结构体里也**已经没有** `vm_next`/`vm_prev`(旧链表的指针)了:链表彻底退役。
>
> 注意区分:VMA 里**确实还有**一个 `struct rb_node shared`([mm_types.h:742](../linux-6.14/include/linux/mm_types.h#L742-L745))——但那个**不是** per-mm 的 VMA 树,而是挂在**文件**的 `address_space->i_mmap` 上的**反向映射(interval tree)**,用途是"文件 → 哪些 VMA 映射了我",和"进程内 VMA 怎么组织"是两回事。别看到 `rb_node` 就以为 VMA 还用红黑树组织。

> **回扣"为什么优先"**:这次树→maple tree 的迁移,是全书"所有复杂设计都是被痛点逼出来的"的一个活样本。不是为了炫技,而是二十多年的红黑树+链表在**大内存多核时代**撑不住了——粗锁、双结构、找洞贵。maple tree 一次性回应了这几个痛点。当你以后看到内核里某个"突然换了数据结构"的改动,基本都能套这个思路去找它化解的痛点。

---

## 五、账本行数有上限:`map_count` 与 `sysctl_max_map_count`

### 提出问题

VMA 这么好用,进程能无限创建吗?

### 不这样会怎样?

如果允许进程无限创建 VMA(每次 mmap 一个字节就建一个),会被恶意程序滥用:瞬间创建几百万个 VMA,把 `mm_struct` 的账本撑爆、让 maple tree 操作变慢、拖垮整个系统——这是经典的**资源耗尽型攻击**(fork bomb 的近亲)。

### 所以这样设计:给 VMA 数量设上限

每个进程的 `mm_struct` 有个计数器 `map_count`([include/linux/mm_types.h:896](../linux-6.14/include/linux/mm_types.h#L896))记录"当前有多少个 VMA"。建新 VMA 时检查它:

```c
/* Too many mappings? */
if (mm->map_count > sysctl_max_map_count)
	return -ENOMEM;
```

([mm/mmap.c:380](../linux-6.14/mm/mmap.c#L380-L381))。`sysctl_max_map_count` 默认 65530(可调,`/proc/sys/vm/max_map_count`)。超过这个数,`mmap` 直接返回 `ENOMEM`。一个 VMA 一行账,账本行数封顶,就是给攻击者画的红线。

---

## 六、关键源码精读

### 6.1 `do_mmap()`:"圈地"的总入口

`mmap` 系统调用的核心是 [mm/mmap.c:337](../linux-6.14/mm/mmap.c#L337-L567) 的 `do_mmap()`。它干一大堆**检查和准备工作**,最后把"真正建 VMA"的活交给 `mmap_region()`。我们挑和"圈地哲学"最相关的几段看:

```c
unsigned long do_mmap(struct file *file, unsigned long addr,
		unsigned long len, unsigned int prot, unsigned int flags, ...)
{
	struct mm_struct *mm = current->mm;
	...
	if (!len)
		return -EINVAL;
	...
	/* Careful about overflows.. */
	len = PAGE_ALIGN(len);
	...
	/* Too many mappings? */
	if (mm->map_count > sysctl_max_map_count)
		return -ENOMEM;
	...
	/* Obtain the address to map to. we verify (or select) it and ensure
	 * that it represents a valid section of the address space.
	 */
	addr = __get_unmapped_area(file, addr, len, pgoff, flags, vm_flags);
	...
	addr = mmap_region(file, addr, len, vm_flags, pgoff, uf);
	...
	return addr;
}
```

逐段对应设计:

- **`PAGE_ALIGN(len)`**:把申请长度**向上对齐到页边界**。因为 VMA 和物理页都以页为最小单位,要 100 字节也会被圈成一整页(4KB)。这呼应了第 6 章"伙伴系统最小一页"的约束——虚拟侧的圈地,也以页为粒度。
- **`map_count > sysctl_max_map_count`**:上一节讲的红线,防资源耗尽。
- **`__get_unmapped_area(...)`**:**选一段还没被占的虚拟地址**。这一步是 ASLR 生效的地方——`get_unmapped_area` 内部会从 `mmap_base`(已经被 ASLR 随机化过)附近找空隙。**圈地选址,但不配房**。
- **`mmap_region(...)`**:真正分配、初始化、插入 VMA 的地方。

注意整个 `do_mmap` **从头到尾没有一处调用伙伴系统去分配物理页**。它只:对齐长度、查限额、选地址、建账本行。**这就是"圈地不占地"在代码里的铁证。**

### 6.2 `vma_link()`:把新行写进 maple tree

`mmap_region()` 最后会调 [mm/vma.c:1688](../linux-6.14/mm/vma.c#L1688-L1702) 的 `vma_link()`,把建好的 VMA 挂进进程的 maple tree:

```c
int vma_link(struct mm_struct *mm, struct vm_area_struct *vma)
{
	VMA_ITERATOR(vmi, mm, 0);

	vma_iter_config(&vmi, vma->vm_start, vma->vm_end);
	if (vma_iter_prealloc(&vmi, vma))
		return -ENOMEM;

	vma_start_write(vma);
	vma_iter_store(&vmi, vma);
	vma_link_file(vma);
	mm->map_count++;
	validate_mm(mm);
	return 0;
}
```

逐行读,正好是"写账本"的标准动作:

- **`VMA_ITERATOR(vmi, mm, 0)`**:初始化一个 maple tree 迭代器,指向本进程的 `mm_mt`([mm_types.h:1165](../linux-6.14/include/linux/mm_types.h#L1165) 的宏,展开就是初始化 `mas_init(&vmi->mas, &mm->mm_mt, addr)`)。这是访问 maple tree 的标准姿势。
- **`vma_iter_config(&vmi, vm_start, vm_end)`**:告诉迭代器"我要存的这个 VMA,覆盖 `[vm_start, vm_end)` 这段"。
- **`vma_iter_prealloc(&vmi, vma)`**:**预先分配好 maple tree 节点需要的内存**。这正是上一节讲的"先在外层分配,避免持锁时分配"的落实——`prealloc` 在这里把节点内存备好,后面真正插入时就不用再分配了。
- **`vma_iter_store(&vmi, vma)`**:**把 VMA 存进 maple tree**。这是"写账本"的核心一行。
- **`vma_link_file(vma)`**:如果这个 VMA 是文件映射,把它挂到文件的 `address_space->i_mmap`(反向映射树)上,这样文件知道"谁映射了我"。
- **`mm->map_count++`**:账本行数 +1。

读完这个函数,你就看清了"圈地"的全部代价:**分配一个 VMA 结构 + 几个 maple tree 节点 + 改个计数**。没有物理页,没有页表项。100MB 的 mmap,在 `vma_link` 里只付出几十字节的账本成本。

### 6.3 ASLR 的扰动从哪来:`arch_mmap_rnd()`

ASLR 的"随机偏移"在 [arch/x86/mm/mmap.c:77](../linux-6.14/arch/x86/mm/mmap.c#L77-L80):

```c
static unsigned long arch_rnd(unsigned int rndbits)
{
	if (!(current->flags & PF_RANDOMIZE))
		return 0;
	return (get_random_long() & ((1UL << rndbits) - 1)) << PAGE_SHIFT;
}

unsigned long arch_mmap_rnd(void)
{
	return arch_rnd(mmap_is_ia32() ? mmap32_rnd_bits : mmap64_rnd_bits);
}
```

读这段:

- **`PF_RANDOMIZE`**:进程是否开启了随机化。关闭它(比如 `setarch -R` 跑老程序),`arch_rnd` 直接返回 0——地址完全确定,方便调试也方便(历史上的)老攻击。
- **`get_random_long() & ((1UL << rndbits) - 1)`**:取 `rndbits` 位随机数。64 位默认 28 位,即 mmap 区域的起始地址在 2^28 × 4KB ≈ 1TB 范围里随机。
- **`<< PAGE_SHIFT`**:左移到页对齐——地址必须整页。
- 这个随机偏移最终喂给 `mmap_base()`([arch/x86/mm/mmap.c:82](../linux-6.14/arch/x86/mm/mmap.c#L82)),算出 mmap 区域的基准地址,后续 `__get_unmapped_area` 就从这里附近找空隙。**每次进程启动,`arch_mmap_rnd` 给的偏移都不同,所以代码段、库、mmap、栈的位置每次都变。**

---

## 七、章末小结

### 用大楼比喻回顾本章

回到那栋楼,这次我们站在**虚拟一侧**。

每个住户(进程)手里有本**预定单台账**(VMA 列表)。住户说"我要 100~200 号门牌归我用",物业(`mmap`)就在台账上**记一行**——这段门牌、什么权限、背后是空地(匿名)还是某个仓库(文件)。**记完就走,不搬家具**。

等住户真去敲某扇门了(page fault,第 9 章),物业才翻台账确认"这单合法吗",合法就临时配一间真房间。台账上 100MB 的预定,可能最后只兑现了其中几页真房间——这就是为什么 `malloc` 不占内存。

- **账本行**(`struct vm_area_struct`)记五件事:哪段门牌、谁的、什么权限、背后是谁、出事怎么办。
- **账本怎么存**:6.14 用一棵 **maple tree**(`mm->mm_mt`),换掉了用了二十多年的红黑树+链表,是为了在大内存多核时代解决粗锁、双结构、找洞贵的痛点。
- **账本怎么布局**:代码在低、栈在高、mmap 居中,再叠一层 **ASLR** 随机扰动,让攻击者摸不准关键地址。
- **账本有上限**:`map_count` 不许超过 `sysctl_max_map_count`(默认 65530),防资源耗尽。

整个过程,物业**一间真房间都没动过**——这一章纯在"造幻觉"那一侧,管的是门牌号的圈占和登记。

### 回扣全书主线

把本章放回二分法——**虚拟一侧管"造幻觉"**:

- 本章是虚拟侧的**记账层**。它和第 8 章(多级页表,翻译机制)、第 9 章(page fault,兑现机制)一起,构成虚拟那一侧的完整三角:
  > **VMA(圈地/预定) → 页表(翻译) → page fault(兑现)**
  - 你"申请"地址空间(VMA 圈地)→ 你"访问"时通过页表翻译 → 翻译发现还没兑现 → page fault 兑现真房间。
  - 这三步是**延迟兑现**哲学的三根支柱。理解了 VMA 只圈地不占地,你才真正懂"为什么 malloc 不立即占内存""为什么 fork 几乎不复制内存""为什么程序能比物理内存大得多"。
- 一条引线向**第 11 章**:本章讲 VMA 的 `vm_file` 字段——当它非 NULL(文件映射)时,这段虚拟地址背后是一个文件。`page fault` 兑现这种 VMA 时,不是从伙伴系统拿空页,而是去 **page cache** 里找(或从磁盘读进 page cache)。所以 VMA 的"后端"分两类:**匿名**(背后没文件,第 9 章的纯内存)和**文件**(背后是文件,第 11 章 page cache 的入口)。下一章就讲文件那一侧。
- 一条引线向**第 12 章**:VMA 的 `vm_flags` 里有 `VM_LOCKED`(mlock,锁定不准回收)这类标志,直接影响回收策略;匿名 VMA 的页回收时走 swap,文件 VMA 的页回收时直接丢(磁盘有原件)。账本上的"性质",决定了物理页不够时"牺牲谁"。

### 想继续深入,往哪钻

- **账本行**:[include/linux/mm_types.h:681](../linux-6.14/include/linux/mm_types.h#L681-L787) 的 `struct vm_area_struct`,和 `vm_flags` 的所有位标志(`grep "define VM_" include/linux/mm.h`)。
- **圈地流程**:[mm/mmap.c:337](../linux-6.14/mm/mmap.c#L337-L567) 的 `do_mmap` → `mmap_region` → [mm/vma.c:1688](../linux-6.14/mm/vma.c#L1688-L1702) 的 `vma_link`。顺着读一遍,你会看到"建账本"的完整链路。
- **maple tree**:这是 6.1 引入的独立子系统。从 [mm/maple_tree.c](../linux-6.14/mm/maple_tree.c) 和 [include/linux/maple_tree.h](../linux-6.14/include/linux/maple_tree.h) 入手,看一个 RCU-safe 的 B-tree 变体是怎么实现的——这是个可以单独成章的大主题。
- **ASLR**:[arch/x86/mm/mmap.c](../linux-6.14/arch/x86/mm/mmap.c) 的 `arch_mmap_rnd`/`mmap_base`,以及 mm/util.c 的 `randomize_page`/`arch_randomize_brk`(`/proc/sys/kernel/randomize_va_space` 控制 ASLR 开关:0 关、1 半开、2 全开)。
- **想自己观测**:
  - `cat /proc/<pid>/maps`——看任意进程的整本账本,每行一个 VMA。
  - `cat /proc/<pid>/smaps`——maps 的加强版,每个 VMA 还附带占用了多少物理页(Rss/Pss)、多少被 swap 等统计。
  - 写个小程序 `malloc(100MB)` 后不读写,对照 `maps`/`smaps` 看它的 VMA 建了、但物理页(Rss)几乎为 0——亲手验证"圈地不占地"。

---

> 下一章,我们看 VMA 的"后端"之一:当 `vm_file` 非空(文件映射)时,page fault 兑现的物理页不是空页,而是 **page cache** 里那份文件数据的内存面。这是 [第 11 章 · page cache:文件 IO 的"内存面"](P2-11-page-cache-文件-IO-的内存面.md)。
