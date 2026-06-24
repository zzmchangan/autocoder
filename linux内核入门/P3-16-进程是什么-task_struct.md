# 第 16 章 · 进程是什么:task_struct 到底装了什么

> **前置**:你需要先读过**内存篇**(尤其是第 8~10 章的虚拟内存、页表、VMA)。理由:进程的"家当"里最重要的一件就是地址空间(`mm_struct`),那是内存篇造的幻觉的"使用者"。本章会反复回扣那套概念。

> **核心问题**:内核眼里,"一个进程"到底是个什么东西?我们平时说"起了个进程",到底起来了什么?那一大坨 `task_struct` 里的几百个字段,为什么一个都不能少?
>
> **读完本章你会明白**:
> - 进程不是"程序",而是"程序 + 它此刻的全部状态"。这份状态凭什么不丢——全靠 `task_struct` 这份档案。
> - 为什么 Linux 内核里**进程和线程是同一种东西**(`task_struct`),区别只在"共享多少家当"。
> - `task_struct` 是怎么分块组织的:身份、资源、调度、硬件状态、亲属关系。
> - 为什么所有内核代码提到"当前进程"都直接用一个叫 `current` 的宏——它怎么做到 O(1) 拿到当前进程。

> **如果一读觉得字段太多太杂**:先只记住三件事——① 进程在内核里就是一份 `task_struct` 档案,进程 = 程序 + 这份档案里的状态;② **进程和线程都是 `task_struct`,区别只在共享 `mm`/`files` 这些资源与否**;③ `task_struct` 分五块:身份(pid/cred)、资源(mm/files/fs)、调度(se)、硬件(thread)、关系(parent/children/tasks)。其余字段第二遍配合源码再抠。

---

## 章首·进程不是程序,是"程序 + 状态"

先纠正一个根深蒂固的误解。很多人说"进程就是正在运行的程序"。这话听着对,但漏了最关键的一层:

> **程序是死的字节,躺在硬盘上。进程是活的,它的"活"体现在——它有"此刻的进度"。**

同一个 `ls` 程序,你同时跑 10 次,会有 10 个进程。它们跑的是**同一段代码**(硬盘上同一份字节),但每个都**算到了不同的地方**、各有各的变量值、各有各的栈、各有各的打开的文件。它们的区别,不在"代码",而在**"此刻的状态"**。

那么问题来了:**这份"此刻的状态",存在哪?**

进程随时会被调度器从 CPU 上"请下来"(`context switch`,第 19 章)。被请下来时,它算到一半的寄存器值、压到一半的栈、用到一半的内存映射……这些**必须有个地方存着**,否则下次再叫它上来,它就"失忆"了,不知道刚才算到哪。

这个"存状态的地方",就是 **`task_struct`**——内核给每个进程建的一份档案。

> **回扣比喻**(导言第一步):市民被叫号器"请离窗口"时,他手上算到一半的账、写到一半的信、当前翻到第几页,全得记进他的**户籍档案**。下次叫号器再叫他上来,档案一翻开,他就能**一丝不差地接着干**,就像从没离开过窗口。`task_struct` 就是这份档案。

这一章的主线就是:**拆开 `task_struct`,看一个进程凭什么"活着"。**

---

## 一、进程状态:`__state` 这一个字段,决定进程此刻能不能上窗口

打开 `task_struct` 的定义,排在 `thread_info` 之后、最先映入眼帘的几个字段之一,是状态:

> [include/linux/sched.h:791-813](../linux-6.14/include/linux/sched.h#L791-L813)

```c
struct task_struct {
#ifdef CONFIG_THREAD_INFO_IN_TASK
	/*
	 * For reasons of header soup (see current_thread_info()), this
	 * must be the first element of task_struct.
	 */
	struct thread_info		thread_info;
#endif
	unsigned int			__state;
	...
	unsigned int			flags;
```

这个 `__state`(历史上叫 `state`,新版改名了)是进程的**命根子**:它一个字段的值,就决定了进程此刻"能不能被调度器叫上窗口"。

### 几种核心状态

> [include/linux/sched.h:99-110](../linux-6.14/include/linux/sched.h#L99-L110)

```c
#define TASK_RUNNING			0x00000000
#define TASK_INTERRUPTIBLE		0x00000001
#define TASK_UNINTERRUPTIBLE		0x00000002
#define __TASK_STOPPED			0x00000004
...
#define EXIT_DEAD			0x00000010
#define EXIT_ZOMBIE			0x00000020
...
#define TASK_DEAD			0x00000080
```

注意一个**坑**:名字叫 `TASK_RUNNING`,不代表它"正在 CPU 上跑"。它只代表"**我想跑,请调度器随时叫我**"。具体分两种:

- **正在跑**:此刻真的在某个 CPU 上执行(调度器把它选出来了);
- **就绪**:已经准备好,在运行队列里排队等叫号。

这两种共用 `TASK_RUNNING` 这个值。内核靠"在不在运行队列里"(`on_rq`)来区分,而不是靠 `__state`。

其他几个状态,都是"**此刻不能跑**":

| 状态 | 含义 | 比喻 |
|------|------|------|
| `TASK_RUNNING` | 想跑(在跑 or 排队) | 市民在大厅等叫号 |
| `TASK_INTERRUPTIBLE` | 睡觉,等某件事(可被信号唤醒) | 市民回家睡觉,叫到号会醒 |
| `TASK_UNINTERRUPTIBLE` | 深睡,等硬件(信号也叫不醒) | 市民在办事途中,谁都不能打断 |
| `__TASK_STOPPED` | 被停(比如收到 SIGSTOP) | 市民被勒令停手 |
| `EXIT_ZOMBIE` | 已死,等父进程收尸 | 市民已断气,留张死亡证明 |
| `TASK_DEAD` / `EXIT_DEAD` | 彻底退场 | 市民入土,档案销毁 |

### 不这样会怎样:如果没有状态字段

设想 `task_struct` 没有 `__state`。那调度器怎么知道"这个进程现在能不能上窗口"?

- 它没法把"在等磁盘读完成"的进程和"万事俱备只差 CPU"的进程区分开——结果可能把一个正等硬盘的进程叫上窗口,它一执行发现"我要的数据还没回来",又得主动让出,**白占一次切换**;
- 它也没法跳过那些"在睡觉"的进程——调度器每次都要唤醒所有进程挨个问"你能跑吗?",CPU 全浪费在无效询问上。

**所以这样设计**:用一个字段把进程此刻的"可调度性"标清楚。调度器只看 `TASK_RUNNING` 的进程(`pick_next_task` 只在运行队列里挑),其余的连排队资格都没有。这一个字段,就把"几千个进程里此刻该考虑谁"这个集合,从全系统缩到了几百个。

> **一个实战印证**:在 shell 里跑 `ps aux`,你会看到 `STAT` 列有 `R/S/D/T/Z` 这些字母——它们正对应上面的状态(R=RUNNING,S=可中断睡眠,D=不可中断睡眠,T=停止,Z=僵尸)。你每天看到的 `ps`,就是在读每个进程的 `__state`。

---

## 二、身份:"我是谁"——pid、tgid 和 cred

状态之后,档案里最该记的,是"这个市民是谁"。

### pid 与 tgid:为什么有两个编号

> [include/linux/sched.h:1032-1033](../linux-6.14/include/linux/sched.h#L1032-L1033)

```c
	pid_t				pid;
	pid_t				tgid;
```

每个 `task_struct` 都有一个唯一的 **pid(process id)**。注意,**每个线程都有自己独立的 pid**——因为线程在内核里也是 `task_struct`,当然各有各的编号。

那 `tgid` 是什么?它是 **thread group id(线程组 id)**。同一个进程的所有线程,共享同一个 `tgid`,它等于这个进程的"主线程"的 pid。

> **为什么要有 tgid?** 因为对外界(用户、`ps`、`kill` 命令)来说,我们说的"进程"其实是"一组线程"。但内核内部管理的是一个个 `task_struct`。`tgid` 就是这两层视图的桥:内核按 `task_struct` 管,对外却要把"同一进程的所有线程"归成一个"进程"呈现。
>
> 具体来说:**`getpid()` 返回的是 tgid,不是 pid**。一个多线程程序里,所有线程调 `getpid()` 都拿到同一个值——它们的 tgid。但每个线程调 `gettid()` 拿到的是各自的 pid。这就是 pid/tgid 双编号的全部意义。

### 不这样会怎样:只有一个编号行不行?

如果只有 pid、没有 tgid:那一个 4 线程的程序,在 `ps` 里会显示成 4 个独立"进程",`kill` 一个也杀不掉整组——用户视角和内核视角对不齐,管理混乱。

如果只有 tgid(每个线程没独立 pid):那内核没法独立调度、独立管理每个线程——而内核恰恰需要对每个执行流单独调度。所以**内核需要 pid(管每个 task),用户需要 tgid(管每组的代表),两个都得有**。

### 凭证 cred:市民的身份证和工作证

光有编号还不够,市民办事还得亮**证件**——你是谁(uid)、你属于哪个组(gid)、你有哪些权限(capabilities)。这存在 `task_struct` 的 `cred` 字段里(本节不展开行号,它指向 `struct cred`)。

> **为什么证件是进程的属性,而不是程序属性?** 因为权限是"运行时"的事。同一个 `cat` 程序,root 跑和普通用户跑,能读的文件天差地别。所以证件跟着**进程**走,不跟程序走。这也是 `setuid` 程序(像 `passwd`)能临时提权的根基——`exec` 一个 setuid 程序时,内核会把进程的 cred 换成文件属主的。

---

## 三、家当:"我有哪些东西"——mm、files、fs

这一块是 `task_struct` 最"有分量"的部分,也是进程和线程区分的关键。

### mm:进程的房子

> [include/linux/sched.h:934-935](../linux-6.14/include/linux/sched.h#L934-L935)

```c
	struct mm_struct		*mm;
	struct mm_struct		*active_mm;
```

`mm` 指向进程的**地址空间**——内存篇第 8~10 章造的那个"独占整栋楼的幻觉"(页表、VMA、代码段/堆/栈……)。一个进程的"房子",就是这玩意儿。

> **回扣内存篇**:`mm_struct` 里面装着这个进程的页表根(pgd)、VMA 链表/红黑树、代码数据段的边界……内存篇讲的所有"虚拟内存"机制,都是挂在某个进程的 `mm` 上的。**进程是这些幻觉的"主人"。**

`active_mm` 是个"借用"字段,专门给内核线程用的——第 19 章讲 lazy TLB 时会详谈。这里先记住:**普通进程 `active_mm == mm`,内核线程 `mm == NULL`、靠 `active_mm` 借别人的**。

### files 和 fs:市民手上的政府服务凭证

市民除了房子,还持有两类"凭证":

- **`files_struct`**:我打开了哪些文件(文件描述符表)。你程序里的 fd 0/1/2(标准输入输出错误),就在这里面。
- **`fs_struct`**:我的"当前工作目录"在哪。

这两个看着不起眼,却是进程能和外部世界打交道(读文件、写屏幕、上网)的把手。第 17 章讲 `fork` 时会看到:**复制还是共享 `files`/`fs`/`mm`,正是进程与线程的分水岭。**

---

## 四、调度信息:"我归谁管、办了多久了"

进程要上窗口办事,就离不开调度。`task_struct` 里专门有一块记录调度相关信息,最核心的是**调度实体**:

> [include/linux/sched.h:844](../linux-6.14/include/linux/sched.h#L844)

```c
	struct sched_entity		se;
```

`struct sched_entity`(定义在 [include/linux/sched.h:547](../linux-6.14/include/linux/sched.h#L547))是调度器眼里的"这个进程"。它里面记着:

> [include/linux/sched.h:547-575](../linux-6.14/include/linux/sched.h#L547-L575)

```c
struct sched_entity {
	/* For load-balancing: */
	struct load_weight		load;
	struct rb_node			run_node;
	u64				deadline;
	u64				min_vruntime;
	u64				min_slice;
	...
	u64				exec_start;
	u64				sum_exec_runtime;
	u64				prev_sum_exec_runtime;
	u64				vruntime;
	s64				vlag;
	u64				slice;
	...
```

现在不用全懂,第 18 章会把每一个讲透。这里只要记住:**`vruntime`(虚拟运行时间)是 CFS 决定"该轮到谁"的依据;`load`(权重)决定这个进程"该占多少份额"。** 调度器不直接看 `task_struct`,它看的是 `sched_entity`——这是一个重要的解耦设计:**调度器管的是"可调度实体",这个实体既可以是进程,也可以是一组进程(组调度),接口统一。**

### 不这样会怎样:把调度信息直接散在 task_struct 里?

如果调度信息直接散落在 `task_struct` 各处,那"组调度"(把几个进程打包成一个整体参与调度)就没法实现——你得给"组"也建个假 task_struct。有了独立的 `sched_entity`,进程和"组"都是 sched_entity,调度器一视同仁。**这就是抽象的力量**(回扣内存篇第 1 章的"分层抽象")。

---

## 五、硬件状态:"我被请下窗口时,CPU 里那些值存哪"

这是 `task_struct` 最"硬核"的一块,也是第 19 章上下文切换的主角:

> [include/linux/sched.h:1629](../linux-6.14/include/linux/sched.h#L1629)

```c
	struct thread_struct		thread;
```

进程在 CPU 上跑时,有一堆**寄存器**记着它的中间状态:通用寄存器(RAX/RBX…)、指令指针(RIP,下一条要执行什么)、栈指针(RSP)、标志位、浮点/SSE 状态……进程被切下 CPU 时,这些值**必须存起来**,下次切回来再装回去。

`thread_struct` 就是存它们的地方——注意是**架构相关**的(x86 有 x86 的版本,RISC-V 有 RISC-V 的版本)。

> **一个关键细节**:寄存器这么多,要不要全存?**不。** 内核只存**"callee-saved"(被调用者保存)的寄存器**——也就是 rbp、rbx、r12~r15 这几个。为什么?因为别的寄存器,按函数调用约定,本来就是"调用者自己负责保存"的。进程切到内核态再切走时,那些 caller-saved 寄存器在内核态根本不会跨函数调用依赖。**只存该存的,这是优化的精髓**。第 19 章看 `__switch_to_asm` 时会眼见为实。

### thread_info:和硬件状态并排的"调度元数据"

档案的第一个字段是 `thread_info`:

> [include/linux/sched.h:797-798](../linux-6.14/include/linux/sched.h#L797-L798)

```c
	struct thread_info		thread_info;
```

它为什么必须是**第一个**字段?源码注释直接说了:"must be the first element"。原因和下面要讲的 `current` 宏有关。`thread_info` 存的是"当前进程"最常被访问的几个标志(是否在内核态执行、是否需要重新调度、是否被抢占……),放最前面是为了快速访问。

---

## 六、亲属关系:"我爹是谁、我有哪些孩子"

进程不是孤立存在的,它有家谱:

> [include/linux/sched.h:1046-1054](../linux-6.14/include/linux/sched.h#L1046-L1054)

```c
	struct task_struct __rcu	*real_parent;   /* 真正的父进程 */
	...
	struct task_struct		*parent;        /* 通常等于 real_parent,ptrace 时不同 */
	...
	struct list_head		children;       /* 我的孩子们 */
```

加上前面见过的 `tasks`:

> [include/linux/sched.h:928](../linux-6.14/include/linux/sched.h#L928)

```c
	struct list_head		tasks;          /* 全系统所有进程串成的链表 */
```

这三组关系,撑起了进程的"社会网络":

- **`tasks`**:全系统所有 `task_struct` 串成一条链表。`for_each_process` 宏就是沿着它遍历所有进程的——`ps` 命令、`/proc` 都是靠它。
- **`children`**:一个进程的所有子进程。
- **`parent`/`real_parent`**:父进程。绝大多数时候 `parent == real_parent`;被 `ptrace`(比如 gdb 调试)时,`parent` 会临时指向调试器。

### 不这样会怎样:不记父进程行不行?

记父进程,是为了**退场时找人收尸**(第 17 章详谈)。进程死了变僵尸,得有父进程来 `wait` 才能彻底清除。如果不知道父是谁:

- 僵尸没人收,系统里僵尸越积越多,占满 pid 空间;
- 退出码、资源用量统计没地方汇报。

父进程如果**先死**了呢?内核有个兜底:**孤儿进程被 `init`(1 号进程)收养**。`init` 会定期 `wait`,保证不会有僵尸永远挂着。这就是为什么 1 号进程必须有、且绝不能死——它是全城孤儿最终的监护人。

---

## 七、关键源码精读:`current` 宏——内核怎么 O(1) 拿到"当前进程"

这一章源码多是字段定义,没有单一"核心函数"。但有一个贯穿全书、每章都会用到的宏,值得专门讲透:**`current`**。

内核代码里随处可见 `current->mm`、`current->pid` 这样的写法——"当前正在 CPU 上跑的这个进程"。**内核是怎么做到不用任何查找、O(1) 拿到它的?**

这背后是一个精巧的硬件约定 + 数据结构布局技巧。

### 思路:栈指针 = 当前进程

x86-64 上,每个 CPU 有一个自己的**内核栈**。当前正在跑的进程,它的内核栈顶(也就是 `RSP` 寄存器的值),和"它是哪个进程"是**一一对应**的。

内核故意这样布局:**每个进程的内核栈,和它的 `thread_info`(进而 `task_struct`)是绑在一起的**。于是有了一条反向查找链:

> **从 CPU 当前的栈指针(RSP),能直接算出当前进程的 `task_struct`。**

这就是 `current_thread_info()` 的原理(早期实现):把 RSP 往下对齐到栈大小的边界(比如按 8KB/16KB 对齐),就落在 `thread_info` 上。

新版本(`CONFIG_THREAD_INFO_IN_TASK`)更直接:`thread_info` 被嵌进 `task_struct` 的第一个字段(就是本章前面看到的那行),所以"从栈找到 thread_info"等于"找到了 task_struct 本体"。

### 为什么 `thread_info` 必须是第一个字段

回到本章开头的注释——"this must be the first element of task_struct"。因为有了 `CONFIG_THREAD_INFO_IN_TASK` 后,`thread_info` 和 `task_struct` 共享同一起始地址。`current_thread_info()` 返回的指针,直接就是 `task_struct *`。把 `thread_info` 放第一个,这个强制转换就是零开销的。

> **回扣比喻**:这就像办事大厅的窗口上钉着"当前服务市民的档案夹"——工作人员(内核代码)一伸手就能拿到,不用去档案室翻。CPU 的栈指针,就是这个"窗口"的物理坐标。

### 这个设计为什么重要

**不这样会怎样?** 如果"当前进程"要靠一个全局变量或一张表去查:那每次中断、每次系统调用、每次调度——内核几十处地方都要查一次表,还要加锁(多核下表是共享的)。光"我是谁"这一个操作,开销就受不了。

靠"栈指针 ↔ task_struct"的硬件级绑定,内核拿 `current` 是**几条指令、零锁、O(1)**。这是内核能高速运转的底层支柱之一。

---

## 八、章末小结

用城市比喻把这一章收口。

这一章我们打开了一个市民的户籍档案(`task_struct`),看清了进程凭什么"活着":

1. **进程 = 程序 + 状态**,而"状态"全记在 `task_struct` 这份档案里;
2. 档案分五块:
   - **状态**(`__state`):决定此刻能不能上窗口;
   - **身份**(pid/tgid/cred):我是谁、证件是什么;
   - **家当**(mm/files/fs):我的房子、打开的文件、当前目录;
   - **调度**(se/vruntime):我归哪个调度类、办了多久;
   - **硬件状态**(thread)+ **关系**(parent/children/tasks):CPU 寄存器值、家谱;
3. **进程和线程在内核里是同一种东西**(都是 `task_struct`),区别只在第 17 章要讲的"共享多少家当"。

> **回扣全书主线**:这一章是**造幻觉的载体**。内存篇造的"独占内存"幻觉,需要一个"主人"——就是进程的 `mm`;后面第 18 章要造的"独占 CPU"幻觉,也需要一个主体——就是进程的 `sched_entity`。**`task_struct` 把这两个幻觉的承载者,统一在一个数据结构里。** 从这一章起,"进程"不再是一个抽象概念,而是一份你能 grep 到、能逐字段读的具体档案。
>
> - **承前**:它直接消费内存篇的 `mm_struct`(进程的"房子"就是内存篇造的幻觉本体);
> - **启后**:下一章(第 17 章)讲这份档案怎么"被造出来"(`fork`)、怎么"被换芯"(`exec`),而第 18、19 章讲它的 `se` 和 `thread` 怎么参与调度和切换。

### 记住这三句话

1. **进程在内核里就是一份 `task_struct`**——程序是死的字节,进程是程序加上这份档案里的全部状态。
2. **进程和线程是同一种东西**——都是 `task_struct`,区别只在共享 `mm`/`files`/`fs` 与否(pid 是每个线程独立,tgid 才是对外呈现的"进程号")。
3. **`current` 宏靠"栈指针 ↔ task_struct"的硬件绑定实现 O(1) 拿当前进程**——这是内核高速运转的底层支柱。

### 想继续深入,该往哪儿钻

- 通读 [include/linux/sched.h](../linux-6.14/include/linux/sched.h) 里 `struct task_struct` 的定义([791 行起](../linux-6.14/include/linux/sched.h#L791)),别怕字段多——按本章的"五块"分类去读,几百个字段就能归位。
- 配合 `crash` 工具或 `/proc/<pid>/status`、`/proc/<pid>/stat`,把字段和可观测数据对上:`State`、`Pid`、`Tgid`、`Uid`、`VmRSS`(对应 `mm`)、`voluntary_ctxt_switches`(对应切换次数)。
- 想理解 `current` 的具体实现,看 [arch/x86/include/asm/current.h](../linux-6.14/arch/x86/include/asm/current.h) 里 `current` 宏和 `this_cpu_read` 的展开(新版用 per-cpu 变量存当前进程指针,比老的"栈对齐"更直观)。

> 下一章翻 **第 17 章 · 市民的出生:`fork`/`exec`**——看看这份 `task_struct` 是怎么被"复制"出来的,以及为什么 `fork` 几乎不复制内存、`exec` 不创建新进程。
