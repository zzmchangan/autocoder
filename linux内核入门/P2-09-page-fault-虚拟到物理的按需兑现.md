# 第 9 章 · page fault:虚拟到物理的"按需兑现"

> **核心问题**:第 8 章里,虚拟地址是进程手里一张张"空头支票"——它声称自己独占一片从 0 开始的连续地址空间,可这些地址背后大多**根本没配真实房间**。那么,这张空头支票**什么时候**变成真金白银的物理页?**又是怎么**变的?
>
> 答案就藏在两个字里:**page fault**。
>
> **读完本章你会明白**:
> - 为什么 **page fault 不是"错误",而是"兑现时机"**——这个视角一转,后面的一切都顺了;
> - 硬件抛出 page fault 后,内核是怎么**一层层分诊**的(从异常入口到最终兑现);
> - 缺页、写时复制(COW)、写只读页、非法访问这四类 fault 各自的命运,以及为什么 `fork()` 能做到几乎不复制内存;
> - 为什么你 `malloc` 了一大片内存,`RSS` 却纹丝不动,非得你真的去读写它才会涨。

> **如果一读觉得太绕**:先只记住三件事——① **page fault 是"兑现"不是"报错"**,触发它说明进程去敲了一扇"空头支票"的门;② 兑现的真正干活者是 [mm/memory.c](../linux-6.14/mm/memory.c) 里的 `handle_mm_fault`,它按 PTE 状态分流成"缺页/换入/写保护";③ **`fork()` 不复制内存,靠把父子页都标成只读、谁写就给谁单独复制一份**(COW)。其余分支细节第二遍配合源码再抠。

---

## 章首·先把"错误"这两个字从脑子里删掉

很多人的第一反应是:**"page fault?那是程序出 bug 了吧,段错误嘛。"**

这是最大的误解。名字里的 "fault" 是历史遗留的锅——它让人以为这是事故。真相恰恰相反:

> ⚠️ **page fault 是整个虚拟内存系统正常运转的"心跳",不是异常。** 没有它,前面第 8 章造出来的那些"幻觉"根本兑现不了。

回想第 8 章的结论:进程的虚拟地址空间是一张大网,但绝大多数网眼**此刻是空的**——`malloc` 没真正用过的内存、`mmap` 映射但没读过的文件、`fork()` 出来的子进程还没动过的页……这些地址在页表里要么**没有 PTE**,要么**PTE 指向一个并不在物理 RAM 里的页**(被 swap 出去了)。

**进程什么时候会发现"这门后头没房间"?** 只有它**真的去访问**那扇门的时候。访问的瞬间,CPU 拿着虚拟地址去查页表,发现"此路不通",于是**陷入内核**——这次陷入,就是 page fault。内核借这个机会,把房间临时配好,让进程重试那条访问指令。这次重试,成功了。

> **回扣比喻**(导言第七步):住户拿着空头支票去敲门,发现门后是空的 → 这"敲门"的动作触发了 page fault → 物业(内核)闻声赶来,临时搬一间真房间过来,在台账(页表)上把"这门牌 ↔ 这间房"记上 → 住户再敲一次,这次门开了。**兑现完成。**

所以这一章的主线只有一句话:**page fault = 兑现时刻**。带着这句话往下读。

---

## 一、触发:硬件先动的手

page fault 的"第一下",是**硬件(MMU)**干的,不是内核。搞清这一下发生了什么,后面内核的所有动作才有意义。

### 不这样会怎样:如果没有硬件这一下

设想 CPU 要执行一条 `mov`,读虚拟地址 `0x7fff1234`。它把这个地址交给 **MMU**(内存管理单元,CPU 里专门做地址翻译的硬件),MMU 去查页表。有两种情况会"查不到":

1. **该地址在页表里没有有效映射**(PTE 不存在,或 PTE 的 present 位为 0);
2. **有映射,但权限不对**(比如 PTE 标了只读,而这条指令是写)。

**不这样会怎样?** 如果硬件不声张、自己随便处理——比如"查不到就当读到一个 0",或者"权限不对也照样写"——那虚拟内存的所有设计瞬间崩塌:

- 隔离没了:进程能读写任意物理页;
- 保护没了:只读的代码段随便改;
- 按需兑现没了:空头支票永远没法兑现,`malloc` 的内存永远用不了。

所以硬件**必须**在"查不到/越权"的那一刻**停下来、报告给内核**。报告的方式就是**触发一个异常(中断)**,专业叫法是 **#PF(page fault 异常)**。CPU 把出事的虚拟地址记到一个特殊寄存器 **CR2** 里,并附上一段 **error code(错误码)** 说明"这次 fault 是为什么",然后跳到内核预先登记好的处理入口。

### 代码佐证:error code 说了什么

error code 虽然叫"错误码",其实它是在**描述这次兑现请求的性质**——是读还是写?是用户态还是内核态触发的?这几位 bit,决定了内核后续怎么分诊:

> [arch/x86/include/asm/trap_pf.h:21-25](../linux-6.14/arch/x86/include/asm/trap_pf.h#L21-L25)

```c
	X86_PF_PROT	=		BIT(0),   /* 0=缺页  1=权限冲突(页在但越权) */
	X86_PF_WRITE	=		BIT(1),   /* 1=这是一次写 */
	X86_PF_USER	=		BIT(2),   /* 1=来自用户态 */
	X86_PF_RSVD	=		BIT(3),   /* 保留位被置,页表坏了 */
	X86_PF_INSTR	=		BIT(4),   /* 1=取指令时触发的(缺指令页) */
```

读懂这几位,你就读懂了"硬件交给内核的'请求单'":

- **bit 0(`X86_PF_PROT`)**是关键分水岭。它为 **0**,说明这次 fault 是因为"**页压根没映射**"(典型的缺页,该兑现了);它为 **1**,说明"**页在,但权限不对**"(可能是写一个只读页——可能该 COW 了,也可能是真违法)。
- **bit 1(`X86_PF_WRITE`)**说明这次访问是**写**,这对 COW 判断至关重要——只有"写"一个共享只读页才需要复制。

> 这几位 error code 不是凭空设计的——它们精确对应了内核后面要分诊的几种情况。**硬件用 5 个 bit 就把"这次兑现请求的诉求"说清了**,内核照单分诊即可。

---

## 二、入口:从异常到 `do_user_addr_fault`

硬件把控制权交给内核后,谁来接?x86 上是一组宏定义的异常入口。我们顺着调用链走一遍,看内核怎么一步步缩小包围圈。

### 第一站:`exc_page_fault`(总入口)

> [arch/x86/mm/fault.c:1492-1542](../linux-6.14/arch/x86/mm/fault.c#L1492-L1542)

```c
DEFINE_IDTENTRY_RAW_ERRORCODE(exc_page_fault)
{
	irqentry_state_t state;
	unsigned long address;

	address = cpu_feature_enabled(X86_FEATURE_FRED) ? fred_event_data(regs) : read_cr2();
	...
	state = irqentry_enter(regs);
	instrumentation_begin();
	handle_page_fault(regs, error_code, address);
	instrumentation_end();
	irqentry_exit(regs, state);
}
```

这个入口干的都是"迎宾"的杂活:**从 CR2 读出出事的虚拟地址**(`read_cr2()`)、进入异常上下文(`irqentry_enter`)、完事再退出。真正分诊交给 `handle_page_fault`。

### 第二站:内核地址还是用户地址?

> [arch/x86/mm/fault.c:1468-1490](../linux-6.14/arch/x86/mm/fault.c#L1468-L1490)

```c
	/* Was the fault on kernel-controlled part of the address space? */
	if (unlikely(fault_in_kernel_space(address))) {
		do_kern_addr_fault(regs, error_code, address);
	} else {
		do_user_addr_fault(regs, error_code, address);
		...
	}
```

地址空间是分两半的:高半给内核,低半给用户。fault 发生在哪一半,处理逻辑天差地别。

**不这样会怎样?** 如果不分内核/用户两路:内核态的 fault(比如内核自己缺页、`copy_from_user` 碰到坏指针)和用户态的 fault(进程正常兑现)混在一起用同一套逻辑,会出现荒唐事——比如进程传了个非法指针给内核,内核访问时缺页,结果内核傻乎乎地给它"兑现"了一页,而不是发现"这是用户传进来的非法地址"。分两路,是为了让**内核态 fault 走"修整或 Oops"、用户态 fault 走"正常兑现"**。

本章聚焦用户侧,所以顺着 `do_user_addr_fault` 往下。

### 第三站:`do_user_addr_fault`——用户态兑现的总调度

这是这一路最关键的函数([arch/x86/mm/fault.c:1210-1452](../linux-6.14/arch/x86/mm/fault.c#L1210-L1452))。它做三件事,我们逐个看。

**(1)先排除"真违法"**——比如内核态代码试图执行用户内存(`X86_PF_INSTR` 而非用户态)、SMAP 违规、保留位坏页表……这些直接 `page_fault_oops`,不兑现。

**(2)把 error code 翻译成一组语义化标志**:

> [arch/x86/mm/fault.c:1292-1305](../linux-6.14/arch/x86/mm/fault.c#L1292-L1305)

```c
	if (error_code & X86_PF_SHSTK)
		flags |= FAULT_FLAG_WRITE;
	if (error_code & X86_PF_WRITE)
		flags |= FAULT_FLAG_WRITE;
	if (error_code & X86_PF_INSTR)
		flags |= FAULT_FLAG_INSTRUCTION;
	...
	if (user_mode(regs))
		flags |= FAULT_FLAG_USER;
```

注意这里把硬件的 `X86_PF_WRITE` bit 翻译成了更通用的 `FAULT_FLAG_WRITE`。**为什么多此一举?** 因为不同架构(ARM、RISC-V……)的 error code 长得不一样,但内核的兑现逻辑(`mm/` 那一层)是**架构无关**的。`do_user_addr_fault` 这一层是"架构翻译官",把 x86 的方言翻成 mm 层听得懂的通用标志。这样 [mm/memory.c](../linux-6.14/mm/memory.c) 的兑现代码就能跨架构复用。这是第 1 章"分层抽象、清晰接口解耦"的一个活样板。

**(3)找 VMA、查权限、调兑现**:

> [arch/x86/mm/fault.c:1359-1388](../linux-6.14/arch/x86/mm/fault.c#L1359-L1388)

```c
retry:
	vma = lock_mm_and_find_vma(mm, address, regs);
	if (unlikely(!vma)) {
		bad_area_nosemaphore(regs, error_code, address);   /* 没有 VMA → 非法地址 */
		return;
	}
	/* Ok, we have a good vm_area for this memory access, so we can handle it.. */
	if (unlikely(access_error(error_code, vma))) {
		bad_area_access_error(regs, error_code, address, mm, vma);  /* 有 VMA 但越权 */
		return;
	}
	...
	fault = handle_mm_fault(vma, address, flags, regs);   /* 真正的兑现 */
```

这三步是本章的脊梁:

1. **找 VMA**:这个出事的虚拟地址,有没有对应的 VMA?(VMA 是第 10 章的主角——进程虚拟空间的"圈地账本"。这里先记:VMA = "这段虚拟地址是合法圈出来的")。
2. **查权限**(`access_error`):就算有 VMA,也要看这次访问**是否越权**(比如对只读 VMA 做写)。
3. **兑现**(`handle_mm_fault`):前两关都过了,才是真正"配房间"的时刻。

**前两关任何一关失败,都不会兑现,而是发信号(通常是 SIGSEGV)杀进程。** 这正是第 1 章需求 B(保护)的落地——越界、越权当场拦下。

---

## 三、四类 fault 的命运:从 `handle_pte_fault` 看分诊

过了 `do_user_addr_fault` 的三关,真正"配房间"的活儿在 [mm/memory.c](../linux-6.14/mm/memory.c)。从 `handle_mm_fault` 一路深入,核心分诊点是 `handle_pte_fault`。这个函数,就是四类 fault 的"分诊台"。

先看 `handle_mm_fault` 怎么把球往下传:

> [mm/memory.c:6197-6200](../linux-6.14/mm/memory.c#L6197-L6200)

```c
	if (unlikely(is_vm_hugetlb_page(vma)))
		ret = hugetlb_fault(vma->vm_mm, vma, address, flags);
	else
		ret = __handle_mm_fault(vma, address, flags);
```

又是一刀:**大页走 `hugetlb_fault`(第 13 章),普通页走 `__handle_mm_fault`**。后者向下走到页表的最末级 PTE,然后交给 `handle_pte_fault`。分诊台登场:

> [mm/memory.c:5844-5907](../linux-6.14/mm/memory.c#L5844-L5907)(精简)

```c
static vm_fault_t handle_pte_fault(struct vm_fault *vmf)
{
	...
	if (!vmf->pte)
		return do_pte_missing(vmf);              /* ① PTE 根本没有 → 缺页 */

	if (!pte_present(vmf->orig_pte))
		return do_swap_page(vmf);                /* ② 页不在内存(被换出)→ 换入 */

	if (pte_protnone(vmf->orig_pte) && vma_is_accessible(vmf->vma))
		return do_numa_page(vmf);                /* NUMA 异地页 → 迁移就近 */

	...
	if (vmf->flags & (FAULT_FLAG_WRITE|FAULT_FLAG_UNSHARE)) {
		if (!pte_write(entry))
			return do_wp_page(vmf);              /* ③ 写一个只读页 → COW/写保护 */
		...
	}
	...
}
```

这一个函数,把目录里说的"四类 fault"清清楚楚摆开了。我们一类一类看,**每类都按"不这样会怎样 → 所以这样设计"**来讲。

---

## 四、第一类:缺页兑现——匿名页

### 场景

进程 `malloc` 了一片内存(或栈要长),`malloc` 只在账本上圈了地(建了 VMA),**没配任何 PTE**。进程第一次写这片地址,PTE 为空 → `do_pte_missing`:

> [mm/memory.c:4054-4060](../linux-6.14/mm/memory.c#L4054-L4060)

```c
static vm_fault_t do_pte_missing(struct vm_fault *vmf)
{
	if (vma_is_anonymous(vmf->vma))
		return do_anonymous_page(vmf);   /* 匿名内存 */
	else
		return do_fault(vmf);            /* 文件映射内存 */
}
```

又是一刀:**匿名(进程自己的堆/栈) vs 文件(映射自某个文件)**。两者兑现方式不同——匿名页凭空申请一页清零,文件页要从 page cache / 磁盘取(衔接第 11 章)。先看匿名。

### 不这样会怎样:读未初始化内存也得真申请一页?

进程常做一件事:**`malloc` 一大片,只读不写**(比如先 `calloc` 当只读查找表用,或者读一块还没写过、逻辑上全 0 的内存)。

**不这样会怎样?** 如果每次"读未初始化匿名页"都老老实实从伙伴系统申请一页物理内存、清零、映射——那进程申请多大就真占多大,**哪怕它只是读了一下从不写**。这些页内容全是 0,彼此完全一样,却各自独占一间 4KB 房间,纯属浪费。

### 所以这样设计:用一页"零页"应付所有只读访问

`do_anonymous_page` 看到这是一次**读** fault,干了一件聪明事——它不申请新页,而是把所有这种"全 0 的只读匿名页"统一映射到一页**共享的、预清零的特殊页**,叫 **zero page(零页)**:

> [mm/memory.c:4862-4883](../linux-6.14/mm/memory.c#L4862-L4883)

```c
	/* Use the zero-page for reads */
	if (!(vmf->flags & FAULT_FLAG_WRITE) &&
			!mm_forbids_zeropage(vma->vm_mm)) {
		entry = pte_mkspecial(pfn_pte(my_zero_pfn(vmf->address),
						vma->vm_page_prot));
		...
		goto setpte;
	}
```

`my_zero_pfn` 返回的就是那页**全系统共享的零页**的物理页帧号。成百上千个进程的"未初始化只读内存",此刻**全部指向同一页 4KB**——省下的房间极其可观。

> **这页是只读的**。一旦进程真去**写**它,会触发 bit 0=1 的权限 fault → 走到 `do_wp_page` → 这时才**真的**复制出一页给它单独用(就是下面第六类 COW 的逻辑)。**先欠着一张共享的零,要写时再兑现成独享的真页**——又一次"懒"哲学。

### 写匿名页:真兑现

如果是**写** fault(或者禁止用零页的场景),才走真正申请:

> [mm/memory.c:4886-4910](../linux-6.14/mm/memory.c#L4886-L4910)

```c
	/* Allocate our own private page. */
	ret = vmf_anon_prepare(vmf);
	if (ret)
		return ret;
	folio = alloc_anon_folio(vmf);          /* 从伙伴系统要一页(folio 是 6.x 的新叫法) */
	...
	__folio_mark_uptodate(folio);
	entry = mk_pte(&folio->page, vma->vm_page_prot);
	...
	if (vma->vm_flags & VM_WRITE)
		entry = pte_mkwrite(pte_mkdirty(entry), vma);  /* 标可写 + 脏 */
	...
	/* 最后:把这条 PTE 写进页表,兑现完成 */
```

这就是真正的"搬家具进房间":从伙伴系统(第 6 章)要一页、设置好 PTE(可写、脏)、写回页表。进程重试指令,这次成功。

> 一个**实战印证**:你 `malloc(1<<30)` 申请 1GB,马上 `cat /proc/<pid>/status | grep VmRSS`,会发现 RSS 几乎没涨——因为一张页都没兑现。你 `memset` 整片写一遍,RSS 才真的爬上去 1GB。**这就是"按需兑现"在你眼皮底下的表演。**

---

## 五、第二类:缺页兑现——文件页(衔接 page cache)

`do_pte_missing` 的另一支是文件映射(比如 `mmap` 了一个文件)。这时走 `do_fault`:

> [mm/memory.c:5542-5547](../linux-6.14/mm/memory.c#L5542-L5547)

```c
	} else if (!(vmf->flags & FAULT_FLAG_WRITE))
		ret = do_read_fault(vmf);            /* 只读:从 page cache / 磁盘读 */
	else if (!(vma->vm_flags & VM_SHARED))
		ret = do_cow_fault(vmf);             /* 私有写:读出来后标只读,待 COW */
	else
		ret = do_shared_fault(vmf);          /* 共享写:读出来 + 可写,脏页待写回 */
```

**不这样会怎样?** 如果文件映射缺页时直接申请空页——那进程读到的就是垃圾数据,根本不是文件内容。所以文件兑现**必须**先把文件对应的那一页数据**搬进内存**,这页数据就住在 **page cache**(第 11 章主角)里。

- **读 fault**(`do_read_fault`):先查 page cache 有没有这页——有就直接映射(所以"第二次读同一段文件特别快");没有就从磁盘读进 page cache,再映射。
- **私有写**(`do_cow_fault`):把页读进来,但**映射成只读**,等进程真写时再 COW 复制——和匿名页一个套路,保护原文件不被改。
- **共享写**(`do_shared_fault`):映射成可写,改动落在 page cache 里变成**脏页**,由内核择机写回磁盘(衔接第 11、12 章)。

这一节点到为止,细节留给第 11 章。你只要记住:**文件页的兑现,是把"磁盘那页"借调进 page cache 的那页房间**。

---

## 六、第三类:写时复制(COW)——为什么 `fork()` 不复制内存

这是 page fault 最妙的一类,也是 `fork()` 能又快又省的根基。

### 痛点:`fork()` 要复制整个地址空间?

`fork()` 创建子进程,子进程要得到父进程地址空间的**一份副本**。父进程可能占好几个 GB。

**不这样会怎样?** 如果 `fork()` 老老实实地把父进程每一页都**复制一份**给子进程:

- **慢**:几 GB 的内存复制,`fork()` 毫秒级变成秒级。一个 web 服务器每秒 fork 上百次,直接卡死。
- **浪费**:绝大多数情况下,`fork()` 之后立刻 `exec()` 换掉整个地址空间——刚复制的那几 GB 瞬间全部丢弃,**白复制**。就算不 `exec`,父子往往也只各改其中一小部分页,**大部分页双方永远都是只读共享就够**。

### 所以这样设计:先共享只读,谁写才复制(COW)

内核的解法叫 **copy-on-write(写时复制)**:

1. **`fork()` 时,不复制任何页**。父子进程的 PTE **指向同一批物理页**。
2. 但把这些页在父子**两边的页表里都标成"只读"**(哪怕它本来是可写的)。
3. 之后父子都只读这些页时,大家相安无事,共享同一份物理内存,**零复制**。
4. 一旦某一方**写**它,触发权限 fault → 内核这时才**单独复制一份**给写的那方,把新副本设回可写,原页仍是另一方的只读。

兑现发生在"写"的那一刻,而不是 `fork()` 那一刻——又是"懒"哲学。

### 代码佐证一:`fork()` 里把页标成只读

fork 复制页表时(`copy_page_range` 一路下到 `__copy_present_ptes`),关键就这几行:

> [mm/memory.c:947-957](../linux-6.14/mm/memory.c#L947-L957)

```c
	/* If it's a COW mapping, write protect it both processes. */
	if (is_cow_mapping(src_vma->vm_flags) && pte_write(pte)) {
		wrprotect_ptes(src_mm, addr, src_pte, nr);  /* 父进程这边也改只读 */
		pte = pte_wrprotect(pte);                    /* 给子进程的副本也只读 */
	}
```

注意 **`wrprotect_ptes` 把父进程自己的页也写保护了**——这一点常被忽略,却是 COW 的精髓:**父子双方此刻都不能写**。这样,无论谁先写,都会触发 fault,内核才有机会复制。**只有当两边都标只读,共享才安全。**

### 代码佐证二:写时触发的复制

那么"谁写谁触发"的兑现,从哪进?回到 `handle_pte_fault`,bit 0=1(权限冲突)+ 是写 + 页只读:

> [mm/memory.c:5902-5904](../linux-6.14/mm/memory.c#L5902-L5904)

```c
	if (vmf->flags & (FAULT_FLAG_WRITE|FAULT_FLAG_UNSHARE)) {
		if (!pte_write(entry))
			return do_wp_page(vmf);   /* 写一个无写权限的页 → 写保护处理 */
```

`do_wp_page` 是 COW 的核心决策点。它先判断:**能不能直接复用这页**(比如这页只有我一个进程在用,根本不用复制)?能就直接改成可写(`wp_page_reuse`),省一次复制:

> [mm/memory.c:3817-3827](../linux-6.14/mm/memory.c#L3817-L3827)

```c
	if (folio && folio_test_anon(folio) &&
	    (PageAnonExclusive(vmf->page) || wp_can_reuse_anon_folio(folio, vma))) {
		...
		wp_page_reuse(vmf, folio);   /* 能复用:直接改可写,不复制 */
		return 0;
	}
	/*
	 * Ok, we need to copy. Oh, well..
	 */
	if (folio)
		folio_get(folio);
	...
	return wp_page_copy(vmf);          /* 不能复用:复制一份 */
```

`wp_page_copy`([mm/memory.c:3425-3574](../linux-6.14/mm/memory.c#L3425-L3574))才是真正干复制活儿的:申请新页、把旧页内容拷过去、在新页的 PTE 上设可写。这一刻,COW 才真正"兑现"成了一份独立的副本。

> **一句总结**:`fork()` 的"复制整个地址空间"是个**谎言**——它只复制了页表(账本),没复制任何数据页;真正的数据复制,被**延迟到第一次写**才发生,而且**只复制被写的那一页**。这就是为什么 `fork()` 是 O(页表大小)而非 O(数据大小),为什么 web 服务器能疯狂 fork。

---

## 七、第四类:非法访问——没 VMA 或越权 → SIGSEGV

不是所有 fault 都该兑现。还有一类 fault,**内核不配房间,而是直接杀进程**。这就是"段错误(SIGSEGV)"的真相。

### 两种非法

回看 `do_user_addr_fault` 的两道关:

**关 1:没有对应的 VMA**:

> [arch/x86/mm/fault.c:1359-1388](../linux-6.14/arch/x86/mm/fault.c#L1359-L1388)(节选自 `do_user_addr_fault` 的 `retry:` 块)

```c
	vma = lock_mm_and_find_vma(mm, address, regs);
	if (unlikely(!vma)) {
		bad_area_nosemaphore(regs, error_code, address);   /* 这地址压根没被圈出来 */
		return;
	}
```

进程访问了一个**从未申请过**(没有 VMA)的地址——典型的空指针解引用(访问 `0x0` 附近)、越界访问。这是真违法。

**关 2:有 VMA,但访问越权**(`access_error`):

> [arch/x86/mm/fault.c:1082-1084](../linux-6.14/arch/x86/mm/fault.c#L1082-L1084)(节选)

```c
	if (!arch_vma_access_permitted(vma, (error_code & X86_PF_WRITE),
				       (error_code & X86_PF_INSTR), foreign))
		return 1;   /* 越权:比如对只读 VMA 写、对不可执行 VMA 取指 */
```

比如对一个**只读文件映射**做写、对一块**不可执行**的内存取指令(`NX` 位,防缓冲区溢出执行数据)。VMA 虽然圈了这块地,但权限不允许你这么访问。

### 不这样会怎样:兑现了非法访问,等于出卖保护

**不这样会怎样?** 如果对"没 VMA 的地址"也照样兑现配一页——那进程能访问任意虚拟地址,**隔离彻底失效**。如果对"越权访问"也放行——那进程能改只读的代码段、能执行本不该执行的数据,**保护彻底失效**。

所以这两类 fault,内核**绝不兑现**,而是走 `bad_area` / `bad_area_access_error` 一路发 **SIGSEGV** 信号给进程。默认处理下,进程被杀,屏幕上那句经典的 **"Segmentation fault"** 就此诞生。

> 这是第 1 章需求 A(隔离)+ 需求 B(保护)在 fault 层的最终落地:**page fault 既能"兑现"也能"拒付"——合法请求给房间,非法请求直接亮红牌**。

---

## 八、关键源码精读:两个核心函数

这一章源码多而散,我们精读两个最能体现设计的:`handle_pte_fault`(分诊台)和 `do_wp_page`(COW 决策)。把这两段读通,本章就通了。

### 精读一:`handle_pte_fault`——四类 fault 的分诊台

> [mm/memory.c:5887-5907](../linux-6.14/mm/memory.c#L5887-L5907)

```c
	if (!vmf->pte)
		return do_pte_missing(vmf);              /* ① 没映射 → 缺页(匿名/文件) */

	if (!pte_present(vmf->orig_pte))
		return do_swap_page(vmf);                /* ② 映射在但页不在 RAM → 换入 */

	if (pte_protnone(vmf->orig_pte) && vma_is_accessible(vmf->vma))
		return do_numa_page(vmf);                /* NUMA:页在远端节点 → 迁移就近 */

	...
	spin_lock(vmf->ptl);
	entry = vmf->orig_pte;
	if (unlikely(!pte_same(ptep_get(vmf->pte), entry))) {
		update_mmu_tlb(vmf->vma, vmf->address, vmf->pte);
		goto unlock;                              /* 拿锁后发现 PTE 变了 → 别人已处理,重来 */
	}
	if (vmf->flags & (FAULT_FLAG_WRITE|FAULT_FLAG_UNSHARE)) {
		if (!pte_write(entry))
			return do_wp_page(vmf);              /* ③ 写只读页 → COW */
		else if (likely(vmf->flags & FAULT_FLAG_WRITE))
			entry = pte_mkdirty(entry);          /* 写可写页 → 只是标脏 */
	}
	entry = pte_mkyoung(entry);                  /* 标"被访问过"(供 LRU 回收用) */
	...
```

逐段对应:

- **`!vmf->pte`(PTE 不存在)** → 这是真"缺页"。区别于"页存在但被换出"。走 `do_pte_missing`,再按匿名/文件分流(第四、五节)。
- **`!pte_present`(PTE 在,但 present 位为 0)** → PTE 这条台账存在,但它指向的物理页**此刻不在 RAM 里**(被 swap 到磁盘/地下室了,第 12 章)。走 `do_swap_page` 把它搬回来。
- **`pte_protnone` + NUMA** → 一种特殊"假保护"位,实际是 NUMA 用来骗进程触发 fault、好借机把页**迁移到更近的节点**(第 13 章)。这就是"protnone"名字的迷惑性——它看着像保护位,其实是 NUMA 钩子。
- **写 + 只读(`!pte_write`)** → 第三、六节讲的 COW / 写保护。注意这里 `pte_same` 那段:**拿到自旋锁后要复查 PTE 没被别人改过**,否则可能在另一个 CPU 正改页表时插进去——多核安全的细节,这里体现为"乐观检查 + 拿锁复查"。
- **`pte_mkyoung`** → 标记这页"刚被访问",这是给第 12 章回收用的年龄信息。**fault 处理顺手维护回收所需的元数据**——层与层之间的小耦合。

**一句话**:`handle_pte_fault` 用 PTE 的状态作为分诊依据,把"这次 fault 到底要干什么"干净地分到四条兑现/换入/迁移/复制支路。**它不关心硬件来自 x86 还是 ARM,只认页表状态——这就是抽象的边界。**

### 精读二:`do_wp_page`——COW 的"复制 or 复用"决策

> [mm/memory.c:3817-3839](../linux-6.14/mm/memory.c#L3817-L3839)

```c
	if (folio && folio_test_anon(folio) &&
	    (PageAnonExclusive(vmf->page) || wp_can_reuse_anon_folio(folio, vma))) {
		if (!PageAnonExclusive(vmf->page))
			SetPageAnonExclusive(vmf->page);
		...
		wp_page_reuse(vmf, folio);                /* 路径 A:这页只我一个独占 → 直接可写,不复制 */
		return 0;
	}
	/*
	 * Ok, we need to copy. Oh, well..
	 */
	if (folio)
		folio_get(folio);                         /* 给旧页加引用,防止复制期间被回收 */

	pte_unmap_unlock(vmf->pte, vmf->ptl);
	...
	return wp_page_copy(vmf);                     /* 路径 B:有别人共享 → 复制一份 */
```

为什么要有"路径 A(复用)"?这是 COW 的一个关键优化:

**不这样会怎样?** 假设父进程 `fork()` 出子进程后,子进程立刻 `exec()` 把自己的地址空间全换掉了——此时那批 COW 页**实际上只剩下父进程一个人在用**(子进程已经不指向它们了)。如果父进程这时写其中一页,还傻乎乎地复制一份,纯属浪费:这页根本没人共享。

所以内核给每个匿名页维护了一个 **`PageAnonExclusive`(匿名独占)**标记:如果这页"名义上可 COW、实际上只剩我一个所有者",那就**直接改成可写、跳过复制**(`wp_page_reuse`)。只有真有别人共享时,才走 `wp_page_copy` 老实复制。

> 这个优化体现了第 1 章"高效"需求:COW 的目的是省复制,**那么"连复制都该省"的情形就该省**。内核不会因为页"曾经被共享过"就永远多复制一次——它实时追踪这页到底还有没有别人。

---

## 九、章末小结

用大楼比喻把这一章收口。

这一章我们追着一张"空头支票",看它**如何、何时**变成真金白银的物理房间。一句话贯穿:**page fault 不是错误,是兑现时机。**

它的完整旅程是:

1. **硬件先动手**:进程访问虚拟地址,MMU 查页表发现"此路不通"或"越权",触发 `#PF`,把出事地址记进 CR2,把性质写进 error code;
2. **内核分诊**(`exc_page_fault` → `handle_page_fault` → `do_user_addr_fault`):先分内核/用户两路,再把 error code 翻译成通用标志,然后过"有没有 VMA"和"越不越权"两道关;
3. **真正兑现**(`handle_mm_fault` → `handle_pte_fault`):按 PTE 状态分到四条路——
   - **缺页**(没 PTE):匿名页申请/零页,文件页借调 page cache;
   - **换入**(页不在 RAM):从 swap 区搬回来;
   - **写保护**(写只读页):COW——能复用就改可写,有共享就复制一份;
   - **非法**(没 VMA 或越权):不兑现,发 SIGSEGV。

而这一切背后的灵魂,是贯穿全书的**"懒"哲学**:`malloc` 只开支票、`fork()` 只复制页表不复制数据、读未初始化内存先共享零页——**兑现永远被推迟到不得不做的最后一刻,而且只兑现被真正触及的那一页**。这就是为什么虚拟内存能以有限的物理 RAM,撑起远超其总量的"已分配"幻觉。

> **回扣全书主线**:这一章是**虚拟侧**的核心兑现环节。第 8 章造好了"独占、连续、从 0 开始"的幻觉(多级页表是它的台账),但台账里大多数条目是空的;**这一章负责把空的条目,在进程敲门的瞬间,临时填上真实房间**。没有它,第 8 章的幻觉永远兑现不了;有了它,幻觉才成为可用的现实。
>
> - **承前**:它直接消费第 8 章的页表(读写 PTE、判断 present/权限),也消费第 6 章的伙伴系统(`alloc_anon_folio` 兑现时要一页)和第 11 章的 page cache(文件页兑现);
> - **启后**:这一章反复提到的 **VMA**("找 VMA"那道关),正是第 10 章的主角——`malloc`/`mmap` 为什么不立即占内存、进程地址空间怎么记账,下一章揭开。

### 记住这三句话

1. **page fault 是"兑现时机",不是"错误"**——它是虚拟内存兑现空头支票的心跳。
2. **兑现永远按需、按页**:malloc 只圈地、fork 只复制页表、读未初始化先共享零页——推迟到不得不做的那一刻。
3. **fault 既能兑现也能拒付**:合法请求配房间,没 VMA / 越权的请求直接 SIGSEGV——这就是隔离与保护的落地。

### 想继续深入,该往哪儿钻

- 顺着调用链通读一遍:[arch/x86/mm/fault.c](../linux-6.14/arch/x86/mm/fault.c) 的 `do_user_addr_fault`([1210-1452](../linux-6.14/arch/x86/mm/fault.c#L1210-L1452)),看架构层怎么"翻译";再到 [mm/memory.c](../linux-6.14/mm/memory.c) 的 `handle_mm_fault`([6165-6230](../linux-6.14/mm/memory.c#L6165-L6230)) → `__handle_mm_fault`([5938-6032](../linux-6.14/mm/memory.c#L5938-L6032)) → `handle_pte_fault`([5844-5930](../linux-6.14/mm/memory.c#L5844-L5930)),看架构无关层怎么分诊。
- 想吃透 COW,重点读 `do_wp_page`([3749-3840](../linux-6.14/mm/memory.c#L3749-L3840))和 `wp_page_copy`([3425-3574](../linux-6.14/mm/memory.c#L3425-L3574)),对照 fork 那头的 `__copy_present_ptes`([947-957](../linux-6.14/mm/memory.c#L947-L957))看"标只读"和"复制"是如何呼应的。
- 实战观察:写个小程序 `malloc` 一大片后只读不写,用 `/proc/<pid>/smaps` 看 `Private_Clean`/`Private_Dirty`/`AnonHugePages` 变化;`strace` 配合 `perf stat -e page-faults` 看 fault 计数,亲手验证"按需兑现"。

> 下一章翻 **第 10 章 · VMA 与进程地址空间**——看看进程那张虚拟地址"账本"是怎么记账的,`malloc` 为什么不立即占内存,代码/堆/mmap/栈又是怎么在地址空间里各占其位的。
