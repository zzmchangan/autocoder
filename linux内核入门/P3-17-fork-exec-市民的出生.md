# 第 17 章 · 市民的出生:`fork` / `exec`

> **前置**:你需要先读过**第 16 章**(进程是什么、`task_struct`),以及**内存篇第 9 章**(page fault 与 COW)。理由:这一章的 `fork` 在"复制 `task_struct`",而它"不复制内存"的根基就是内存篇讲的写时复制。两边拼起来才完整。

> **核心问题**:`fork()` 凭什么能做到"给子进程一份和父进程一样的地址空间",却几乎不复制任何内存?`exec()` 又是怎么把一个全新的程序"塞进"同一个进程壳里的?进程和线程,到底是在哪一步分道扬镳的?
>
> **读完本章你会明白**:
> - `fork` 的精髓:**复制的是档案和页表(账本),不是数据**。一个进程"出生"的真实代价是 O(页表大小),不是 O(数据大小)。
> - `copy_process` 怎么靠 `CLONE_VM`/`CLONE_FILES` 这些标志,决定每一项资源"共享"还是"复制"——**这就是进程与线程的分水岭**。
> - `exec` 的视角转换:**它不创建新进程,而是把现有进程"洗脑"**——换掉地址空间和代码,但 pid 不变、父进程还认得它。
> - 进程退场的两步走:`exit` 留下"僵尸"等收尸,`wait` 才真正入土——为什么必须这么绕。

> **如果一读觉得流程绕**:先只记住三件事——① **`fork` = 复制 `task_struct` + 复制页表(COW 不复制数据)**;② **进程 vs 线程 = `copy_process` 里各项资源"复制"还是"共享",由 `CLONE_*` 标志决定**;③ **`exec` 不建新进程,只把现有进程的 `mm` 整个换掉(洗脑)**。退场的僵尸细节第二遍再抠。

---

## 章首·一个程序怎么"活"起来:`fork` + `exec`

第 16 章我们看清了"进程 = 一份 `task_struct` 档案"。现在的问题是:**这份档案是怎么被造出来的?**

答案藏在每个程序员都见过的那个套路里。你在 shell 里敲 `ls`,shell 不是直接"运行 ls",而是做两步:

```c
pid = fork();        // ① 分身出一个新市民(壳)
if (pid == 0) {
    execve("/bin/ls", argv, envp);  // ② 这新市民换上 ls 的衣服(芯)
}
// 父进程(shell)继续,等子进程结束
```

这两步,各自干一件截然不同的事:

- **`fork`**:shell 把自己**复制**一份,得到一个全新的 `task_struct`(亲生骨肉)。这一刻,子进程和父进程**长得一模一样**——同样的代码、同样的变量、同样的打开文件,连"下一条要执行哪行"都一样。
- **`exec`**:子进程把自己的"芯"换掉——**地址空间整个扔掉**,换成 `ls` 的代码和数据。从此它不再是 shell,而是 `ls`。但**壳没换**:pid 还是那个 pid,父进程还是 shell。

> **回扣比喻**(导言第 0、4 步):`fork` 造的是市民的"肉身壳子",`exec` 给这壳子换上"新灵魂"。**两步合起来,才叫"启动一个新程序"**。绝大多数程序的诞生,都是这个套路——后面你会看到,这背后藏着内核两个最巧妙的设计:COW 和"洗脑式 exec"。

---

## 一、`fork` 的入口:从系统调用到 `kernel_clone`

用户态调 `fork()`,陷进内核(系统调用怎么陷的,第 22 章详谈),最终落到:

> [kernel/fork.c:2897](../linux-6.14/kernel/fork.c#L2897)

```c
SYSCALL_DEFINE0(fork)
{
	struct kernel_clone_args args = {
		.exit_signal = SIGCHLD,
	};
	return kernel_clone(&args);
}
```

`fork` 的系统调用实现薄得可怜——它只是把参数打包进一个 `kernel_clone_args`,真正的活儿全在 `kernel_clone`:

> [kernel/fork.c:2774-2820](../linux-6.14/kernel/fork.c#L2774-L2820)

```c
pid_t kernel_clone(struct kernel_clone_args *args)
{
	u64 clone_flags = args->flags;
	struct completion vfork;
	struct pid *pid;
	struct task_struct *p;
	int trace = 0;
	pid_t nr;
	...
	p = copy_process(NULL, trace, NUMA_NO_NODE, args);   /* ★ 核心:造新进程 */
	add_latent_entropy();

	if (IS_ERR(p))
		return PTR_ERR(p);
	...
```

`kernel_clone` 自己不造进程,它把核心的"造人"活儿**委托**给 `copy_process`。拿到造好的 `task_struct`(`p`)之后,再做一些"出生登记":分配 pid、把新进程挂进全系统链表、唤醒它(让它去排队等窗口)。

> **为什么不把"造人"和"出生登记"放一个函数?** 因为 `copy_process` 要干的活儿又多又杂(复制几百个字段、复制各项资源、还要保证"造到一半失败能干净回滚"),单独抽出来才能写得清爽;`kernel_clone` 只负责"出生后的社会登记"。**职责分离,回扣内存篇第 1 章的"清晰接口解耦"。**

真正的重头戏是 `copy_process`。它就是"市民出生"的全部秘密。

---

## 二、`copy_process`:复制一份档案

`copy_process`([kernel/fork.c:2147](../linux-6.14/kernel/fork.c#L2147))是个巨长的函数,但骨架清晰。它做四件事:

1. **分配一个新的 `task_struct`**(`dup_task_struct`),把父进程的档案字段**逐字节复制**过来;
2. **逐项决定每件家当"复制"还是"共享"**(`copy_mm`/`copy_files`/`copy_fs`/`copy_signal`/`copy_sighand`/`copy_creds`…);
3. **重新设置那些"不能照抄"的字段**:新的 pid、新的亲属指针、清零统计计数、新的调度实体初值;
4. **返回造好的 `task_struct`**(给 `kernel_clone` 去登记)。

第 1 步"逐字节复制"是起点——子进程一开始就是个"父进程的克隆体"。**精髓全在第 2 步:每一项资源,复制还是共享?** 这正是进程与线程分道扬镳的地方。

---

## 三、进程 vs 线程:`CLONE_*` 标志决定一切

这是本章最关键的一节。我们把第 16 章讲的"家当"——`mm`、`files`、`fs`、`signal`、`sighand`——逐个放到 `copy_process` 的放大镜下,看它们是"复制"还是"共享"。

### 看代码:`copy_mm` 的分岔

最具代表性的是 `copy_mm`:

> [kernel/fork.c:1725-1759](../linux-6.14/kernel/fork.c#L1725-L1759)

```c
static int copy_mm(unsigned long clone_flags, struct task_struct *tsk)
{
	struct mm_struct *mm, *oldmm;
	...
	tsk->mm = NULL;
	tsk->active_mm = NULL;
	...
	oldmm = current->mm;
	if (!oldmm)
		return 0;                       /* 内核线程没有 mm */

	if (clone_flags & CLONE_VM) {
		mmget(oldmm);
		mm = oldmm;                 /* ★ 共享:直接指向父进程的 mm */
		goto set_mm;
	}

	mm = dup_mm(tsk, current->mm);       /* ★ 复制:深拷贝一份新 mm */
	if (!mm)
		return -ENOMEM;
set_mm:
	tsk->mm = mm;
	tsk->active_mm = mm;
	...
	return 0;
}
```

看那个 `if (clone_flags & CLONE_VM)`——这就是分水岭:

- **带 `CLONE_VM` 标志** → 子进程的 `mm` **直接指向父进程的 `mm`**。父子的"房子"是**同一栋**(`mmget` 只是给引用计数 +1)。这是**线程**。
- **不带 `CLONE_VM`** → 调 `dup_mm` 给子进程**深拷贝一份全新的 `mm`**(新的页表、新的 VMA 树)。这是**进程**。

### 不这样会怎样:把"线程"做成另一种数据结构?

设想如果线程是一种独立于进程的数据结构(`struct thread`),那内核里就要有两套管理代码:进程调度器、线程调度器;进程切换、线程切换;进程链表、线程链表……重复、易错、难维护。

Linux 的设计哲学是:**统一**。线程和进程都是 `task_struct`,进同一个调度器、走同一套切换代码。**唯一的区别,就是 `copy_process` 时各项资源共享与否**——而这是由调用者传的 `CLONE_*` 标志决定的。

一张表说清进程和线程在 `fork` 时的差别:

| 资源 | 普通 `fork`(进程) | `pthread_create`(线程) | 对应标志 |
|------|---------------------|--------------------------|----------|
| 地址空间 `mm` | **深拷贝** | **共享** | `CLONE_VM` |
| 打开的文件 `files` | 复制 | 通常**共享** | `CLONE_FILES` |
| 文件系统信息 `fs` | 复制 | 通常**共享** | `CLONE_FS` |
| 信号处理 `sighand` | 复制 | **共享** | `CLONE_SIGHAND` |
| 信号结构 `signal` | 复制 | **共享** | `CLONE_THREAD` |
| pid | 新的 | 新的 pid,**共享 tgid** | `CLONE_THREAD` |

> **一句话**:**"进程"是"`fork` 时不带任何 `CLONE_*`"的特例,"线程"是"带了一堆 `CLONE_*` 共享"的特例。它们在内核里没有本质区别,只是资源共享程度不同。** `pthread_create` 库函数,本质就是调 `clone(2)` 系统调用并带上 `CLONE_VM|CLONE_FILES|CLONE_FS|CLONE_SIGHAND|CLONE_THREAD` 这一串标志。**Linux 的"线程"= 共享资源的"进程"。**

### 进程组的统一:tgid 再回顾

第 16 章留了个扣:`tgid` 是"线程组 id"。现在能讲透了:`copy_process` 在 `CLONE_THREAD` 时,会**让新 task 的 tgid 等于父 task 的 tgid**,而不是给它新户号。于是同一进程的所有线程共享一个 tgid,对外(对 `getpid()`、对 `ps`)呈现为一个"进程"。**这套 pid/tgid + CLONE_THREAD 的组合,就是 Linux 把"线程"塞进"进程"框架的全部机制。**

---

## 四、`dup_mm`:复制房子,但靠 COW 不复制家具

回到不带 `CLONE_VM` 的普通 `fork`。它调 `dup_mm` 给子进程"复制一份房子":

> [kernel/fork.c:1684-1710](../linux-6.14/kernel/fork.c#L1684-L1710)

```c
static struct mm_struct *dup_mm(struct task_struct *tsk,
				struct mm_struct *oldmm)
{
	struct mm_struct *mm;
	int err;

	mm = allocate_mm();                 /* 申请新的 mm_struct 壳子 */
	if (!mm)
		goto fail_nomem;

	memcpy(mm, oldmm, sizeof(*mm));     /* 把父进程 mm 的字段抄过来 */

	if (!mm_init(mm, tsk, mm->user_ns))
		goto fail_nomem;
	...
	err = dup_mmap(mm, oldmm);          /* ★ 核心:复制页表(VMA + PTE) */
	if (err)
		goto free_pt;
	...
	return mm;
	...
}
```

注意它干了什么、**没干什么**:

- ✅ 申请新的 `mm_struct` 壳子,把父进程的字段(各段边界、标志)抄过来;
- ✅ 调 `dup_mmap` 复制 **VMA 树和页表(PTE)**——这是"账本";
- ❌ **没有复制任何一个数据页**。

这就是内存篇第 9 章讲透的 **COW(写时复制)** 在 `fork` 这头的体现。`dup_mmap` 复制页表时,对每个有数据的页做一件事(内存篇第 9 章见过):

> [mm/memory.c:947-957](../linux-6.14/mm/memory.c#L947-L957)

```c
	/* If it's a COW mapping, write protect it both processes. */
	if (is_cow_mapping(src_vma->vm_flags) && pte_write(pte)) {
		wrprotect_ptes(src_mm, addr, src_pte, nr);  /* 父这边也改只读 */
		pte = pte_wrprotect(pte);                    /* 子那边也只读 */
	}
```

父子两边的 PTE **都指向同一批物理页**,且**都标成只读**。之后谁都不写 → 共享同一份内存,零复制;谁先写 → 触发 page fault → 内核**当场只复制被写的那一页**(`do_wp_page`,见内存篇第 9 章)。

### 不这样会怎样:老实复制每一页

**不这样会怎样?** 如果 `dup_mm` 老老实实把父进程每个数据页都 `memcpy` 一份:

- **慢**:父进程占 2GB,`fork` 就得拷 2GB,从微秒级变成百毫秒甚至秒级。web 服务器每秒 fork 上百次处理连接,直接卡死。
- **浪费**:绝大多数 `fork` 之后立刻 `exec`(像 shell 跑 `ls` 那样)——刚花大力气复制的 2GB 瞬间全部丢弃,**白复制**。就算不 `exec`,父子通常也只各改其中一小部分页。

**所以这样设计**:`fork` 只复制"账本"(页表),不复制"实物"(数据页);真正的数据复制,推迟到**第一次写**、且**只复制被写的那一页**。这是贯穿全书的"懒"哲学,在进程篇最响亮的一次回响。

> **一句总结**:`fork()` 声称"复制整个地址空间",这是个**善意的谎言**——它只复制了页表。这就是为什么 `fork` 又快又省,也是为什么 web 服务器、数据库能用 `fork` 玩出各种花样(比如 Redis 的 `fork`+COW 做持久化快照)。

---

## 五、`exec`:不创建新进程,只"洗脑"

`fork` 出来的子进程,此刻是父进程的克隆体(shell)。要变成 `ls`,得 `exec`。

### 反直觉:exec 不建新进程

很多人以为"exec 启动了一个新进程"。错。

> **`exec` 不创建任何新的 `task_struct`。它操作的是"当前进程自己"——把当前的 `mm`(地址空间)整个扔掉,换成新程序的。**

`exec` 之后:

- **pid 不变**——还是原来那个壳;
- **父进程还是认得它**——shell 还能用原来的 pid `wait` 它、收它的退出码;
- **变的只有"芯"**:代码段、数据段、堆、栈,全是新程序的。

**不这样会怎样?** 如果 `exec` 是"杀掉旧进程、创建新进程":那 pid 会变,父进程(shell)就跟丢了这个孩子——它原本 `fork` 出来是为了等它结束、看它退出码的,现在 pid 变了,这层父子关系断了。`exec` 选择"洗脑而非重建",正是为了**保住这层父子关系**,让 `shell` 这类父进程能可靠地管理子进程的生命周期。

### exec 的入口:`do_execveat_common`

用户态调 `execve`,陷进内核最终到:

> [fs/exec.c:1890-1930](../linux-6.14/fs/exec.c#L1890-L1930)

```c
static int do_execveat_common(int fd, struct filename *filename,
			      struct user_arg_ptr argv,
			      struct user_arg_ptr envp,
			      int flags)
{
	struct linux_binprm *bprm;
	int retval;
	...
	bprm = alloc_bprm(fd, filename, flags);     /* 准备一个"执行上下文" bprm */
	...
	retval = count(argv, MAX_ARG_STRINGS);       /* 数参数个数 */
	...
	bprm->argc = retval;
	...
	retval = prepare_binprm(bprm);               /* 把可执行文件头读进来 */
	...
	retval = exec_binprm(bprm);                  /* ★ 真正的"洗脑"在这 */
	...
}
```

这里的 `bprm`(`linux_binprm`)是 `exec` 过程的"工作台",临时用来装"要执行什么文件、参数是什么、目标格式是什么"。`exec_binprm` 会调 `search_binary_handler`,在一个**格式处理程序链表**里找谁能认得这个文件(ELF?脚本?`#!` 开头的解释器?)。找到了就调它的 handler——比如 ELF 的 `load_elf_binary`,它负责把程序的各个段加载进新地址空间。

> **为什么要一个"格式处理程序链表"?** 因为 Linux 能执行的不仅仅是 ELF 可执行文件,还有 `#!` 脚本(靠 `binfmt_script`)、甚至 Java 字节码(靠 `binfmt_misc`)。每种格式一个 handler,挂链表上,`exec` 时挨个问"你认得这个文件吗?"。**谁认得谁处理**——又一个清晰的接口解耦(回扣内存篇第 1 章)。

### 洗脑的关键一步:换 `mm`

`exec` 最核心的动作,是把进程的 `mm` 换掉。这在 `exec_mmap`([fs/exec.c:955](../linux-6.14/fs/exec.c#L955))里完成:

> [fs/exec.c:957-1000](../linux-6.14/fs/exec.c#L957-L1000)(精简)

```c
static int exec_mmap(struct mm_struct *mm)
{
	struct task_struct *t;
	struct mm_struct *old_mm, *active_mm;
	...
	t = current;
	...
	old_mm = current->mm;
	...
	tsk->mm = mm;                  /* ★ 换上新的地址空间(新程序的) */
	active_mm = tsk->active_mm;
	tsk->active_mm = mm;
	...
	activate_mm(active_mm, mm);    /* 让硬件加载新页表 */
	...
}
```

`exec` 之前,`bprm_mm_init`([fs/exec.c:375](../linux-6.14/fs/exec.c#L375))已经给 `exec` 过程准备了一个**临时的新 `mm`**(`bprm->mm = mm_alloc()`),ELF handler 把新程序加载进这个临时 mm。`exec_mmap` 在这里把进程的 `mm` **从旧的换成这个新的**,旧 `mm` 释放掉。**洗脑完成**——同一个 `task_struct`,换了完全不同的地址空间。

> **一个细节**:在 `begin_new_exec`([fs/exec.c:1209](../linux-6.14/fs/exec.c#L1209))里,内核还会重置一批"该恢复出厂设置"的状态——信号处理函数清成默认、关闭设置了 `O_CLOEXEC` 的文件、清零各种统计。**`exec` 把进程"洗"成一个干净的新程序运行环境,但壳(pid、父进程关系、默认未关的文件)保留。**

### `fork` + `exec` 为什么是两步

把前面合起来:`fork` 给"分离的执行环境"(新 pid、能独立 wait 的壳),`exec` 给"新的程序内容"。两步分离的好处是**灵活性**:

- 只 `fork` 不 `exec`:父子跑**同一段代码**(比如 web 服务器 fork 出 worker,大家都跑服务器代码);
- `fork` 后立即 `exec`:换一个全新程序(比如 shell 跑 `ls`);
- `fork` 后做点别的事再 `exec`:比如重定向完文件描述符再 exec(这就是 shell 管道 `|` 的实现)。

**两步分离 = 可组合**。这是 Unix 设计哲学的经典体现,而内核用 COW + 洗脑式 exec,让这套机制又快又省。

---

## 六、退场:`exit` 与僵尸

有生就有死。进程跑完(或被杀),调 `exit` 退场。但退场不是一刀两断,而是**两步走**。

### 第一步:`do_exit`——释放资源,变僵尸

> [kernel/exit.c:876-900](../linux-6.14/kernel/exit.c#L876-L900)

```c
void __noreturn do_exit(long code)
{
	struct task_struct *tsk = current;
	...
	exit_signals(tsk);  /* sets PF_EXITING */
	...
}
```

`do_exit` 释放进程占用的几乎所有资源:退房子(`exit_mm`,把 `mm` 还给房屋管理局)、关文件、注销定时器、记账……最后,把它改成僵尸状态:

> [kernel/exit.c:742](../linux-6.14/kernel/exit.c#L742)

```c
	tsk->exit_state = EXIT_ZOMBIE;
```

**僵尸状态意味着什么?** 这个进程:

- ❌ 不再占 CPU、不再排队等窗口(从调度器眼里消失了);
- ❌ 不再占内存(房子退了);
- ✅ **但 `task_struct` 档案还在**——里面记着它的退出码、用了多少 CPU/内存的统计。

### 不这样会怎样:当场拆干净

**不这样会怎样?** 如果 `do_exit` 把 `task_struct` 也当场删了:

- 父进程(比如 shell)调 `wait()` 想收尸时,**找不到这个孩子**——它的退出码丢失,shell 不知道 `ls` 是成功(0)还是失败(非 0);
- 资源用量统计(给 `getrusage`、`wait4` 用)也没了。

**所以这样设计**:进程死了,先变"僵尸",**档案留一段时间**等父进程来取。父进程 `wait` 时,内核从僵尸档案里读出退出码、统计信息交给父进程,**这时才把 `task_struct` 彻底删除**:

> [kernel/exit.c:1168](../linux-6.14/kernel/exit.c#L1168)

```c
	if (cmpxchg(&p->exit_state, EXIT_ZOMBIE, state) != EXIT_ZOMBIE)
```

(`wait` 路径里把 `EXIT_ZOMBIE` 推进到 `EXIT_DEAD`,随后释放。)

### 僵尸的危害与孤儿

僵尸不占 CPU、不占内存,但**占一个 pid 和一份 `task_struct`**。如果父进程写崩了、不调 `wait`,子进程的僵尸就永远挂着。僵尸堆多了,系统 pid 耗尽,建不出新进程——这就是"僵尸进程(zombie)"为什么是个经典运维问题。

兜底机制:**如果父进程先死,孤儿进程被 `init`(1 号)收养**。`init` 会定期 `wait` 所有收养来的孩子,保证僵尸不会无限堆积。这就是为什么 1 号进程绝不能死——它是全城孤儿的最终监护人(第 16 章讲亲属关系时埋的扣,这里收了)。

---

## 七、关键源码精读:`copy_process` 的骨架

这一章源码分散在 `fork.c`/`exec.c`/`exit.c`。我们精读最能体现"进程如何出生"的 `copy_process` 骨架——把它读通,进程 vs 线程、fork 的代价就全通了。

`copy_process` 极长(几百行),但按"四件事"读就清爽。挑关键节点:

**第 1 步:克隆壳子**

```c
p = dup_task_struct(current, node);   /* 逐字节复制父进程的 task_struct */
```

这一步之后,`p` 是父进程的一个完整克隆体——所有字段都一样。接下来要做的,就是**把那些"该改的"改掉、该复制/共享的家当分别处理**。

**第 2 步:逐项处理家当(进程/线程分水岭)**

```c
	retval = copy_creds(p, clone_flags);   /* 凭证 */
	...
	retval = copy_files(clone_flags, p);   /* 打开的文件 */
	...
	retval = copy_fs(clone_flags, p);      /* 文件系统信息(当前目录等) */
	...
	retval = copy_signal(clone_flags, p);  /* 信号结构 */
	retval = copy_sighand(clone_flags, p); /* 信号处理函数表 */
	retval = copy_mm(clone_flags, p);      /* ★ 地址空间 —— 本章第三节详讲 */
	...
	retval = copy_thread(p);               /* 硬件状态/寄存器(thread_struct) */
```

每一项 `copy_*` 都长一个样:**看 `clone_flags` 里有没有对应的 `CLONE_*`,有就共享、没就复制**(像 `copy_mm` 那样)。这一组函数,就是"进程和线程是同一种东西"的全部机制。

> 特别注意 **`copy_thread`**——它复制硬件状态(`thread_struct`),但有个关键细节:它**故意把子进程的返回值寄存器设成 0**。还记得 `fork()` 在用户态"返回两次"吗?父进程拿到子进程 pid,子进程拿到 0。这个魔法的实现,就在 `copy_thread` 里:**子进程的寄存器初值被设成"仿佛刚从系统调用返回、返回值是 0"**。等子进程第一次被调度上 CPU,它一"返回",就拿到 0,于是走进 `if (pid == 0)` 那个分支。

**第 3 步:改掉"不能照抄"的字段**

```c
	p->pid = ...;            /* 新的 pid(不是照抄父进程的) */
	p->real_parent = current;/* 父进程指针指向调用者 */
	...                       /* 统计计数清零、调度实体初始化等 */
```

**第 4 步:返回**

> [kernel/fork.c:2638](../linux-6.14/kernel/fork.c#L2638)

```c
	return p;   /* 把造好的 task_struct 交给 kernel_clone 去出生登记 */
```

**一句话**:`copy_process` = "克隆壳子 + 按标志共享/复制家当 + 改身份字段 + 返回"。**进程和线程的差别,仅仅在中间那组 `copy_*` 调用时传了哪些 `CLONE_*` 标志。** 这就是 Linux 进程模型的全部优雅之处。

---

## 八、章末小结

用城市比喻把这一章收口。

这一章我们跟着一个市民从出生到死亡:

1. **`fork` 复制 `task_struct`(壳),靠 COW 不复制数据(芯)**——出生的代价是 O(页表大小),不是 O(数据大小)。这就是 web 服务器、数据库敢疯狂 `fork` 的底气。
2. **进程 vs 线程 = `copy_process` 里各项家当"复制"还是"共享"**,由 `CLONE_*` 标志决定。**Linux 的"线程"就是"共享资源的进程",内核里没有第二种数据结构。**
3. **`exec` 不建新进程,只把现有进程的 `mm` 整个换掉(洗脑)**——pid 不变、父子关系不变,变的只有代码和数据。
4. **退场两步走**:`exit` 释放资源、变僵尸(留档案等收尸);`wait` 取退出码、彻底删除档案。僵尸不占 CPU/内存,只占 pid——父进程不收尸就会堆积。

> **回扣全书主线**:这一章是**身份与资源**那一侧的完整故事。第 16 章看清了"进程是一份档案",这一章看清了"这份档案怎么被造出来、怎么被换芯、怎么被销毁"。**`fork` 是内存篇 COW 的最大消费者**——内存篇讲"写时复制"是为了"省内存",这一章告诉你"谁在用它、为什么必须用它"。两章合起来,COW 的全貌才完整。
>
> - **承前**:它直接依赖第 16 章的 `task_struct`(复制的对象)和内存篇第 9 章的 COW(复制的手段);
> - **启后**:这一章造出了进程,但"几千个进程谁先用 CPU"还没解决——这正是第 18 章调度器和第 19 章上下文切换的主角。

### 记住这三句话

1. **`fork` 复制档案和页表,不复制数据(COW)**——进程出生的真实代价是页表大小。
2. **进程和线程都是 `task_struct`,区别只在 `CLONE_*` 标志决定的"共享还是复制"**——`mm`/`files`/`fs`/`signal` 共享就是线程。
3. **`exec` 不建新进程,只洗脑(换 `mm`)**——保住父子关系;退场分 `exit`(变僵尸)和 `wait`(收尸)两步。

### 想继续深入,该往哪儿钻

- 通读 [kernel/fork.c](../linux-6.14/kernel/fork.c) 的 `copy_process`([2147 起](../linux-6.14/kernel/fork.c#L2147)),按"克隆壳子 → 处理家当 → 改身份 → 返回"四段读,重点对照每个 `copy_*` 的 `CLONE_*` 判断。
- 配合 `strace -f` 跑一个会 `fork`+`exec` 的命令(比如 `strace -f ls`),亲眼看 `clone`/`execve`/`wait4` 的调用序列,把代码和可观测对上。
- 想吃透 COW,回内存篇第 9 章([P2-09](P2-09-page-fault-虚拟到物理的按需兑现.md))的 `__copy_present_ptes` ↔ `do_wp_page` 那一对——fork 这头"标只读",page fault 那头"复制",两边呼应才是完整闭环。
- 想理解僵尸,写个小程序:父进程 `fork` 后**故意不 `wait`** 也不退出,用 `ps` 看子进程变 `[ls] <defunct>`(Z 状态);再把父进程 kill,观察孤儿被 `init` 收养、僵尸消失。

> 下一章翻 **第 18 章 · 调度器:谁此刻用窗口**——这一章造出了这么多进程,接下来看调度器怎么决定"此刻叫谁上窗口"。
