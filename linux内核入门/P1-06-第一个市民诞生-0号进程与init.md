# 第 6 章 · 第一个市民诞生:0 号进程与 init

> **前置**:你需要先读过上一章 [start_kernel:初始化大游行](P1-05-start_kernel-初始化大游行.md)——它讲清了"市政府怎么把所有部门挂上线"。本章紧接它的最后一行:`start_kernel` 的结尾调用了 `rest_init`,大游行走完,城市"活了"。可这一章开头有个问题:**城里一个市民都还没有**。市民(进程)不能凭空冒出来,那**第一个市民是谁生的、怎么生的**?这就是本章要讲的开天辟地的一刻。

> **核心问题**:启动走到这里,各部门到岗、城市通电,可城里**一个市民都还没有**。市民不能凭空冒出来——那**第一个市民是谁生的、怎么生的**?为什么必须有"0 号""1 号""2 号"这三尊祖宗,它们各自的命运是什么?

> **读完本章你会明白**:`ps` 里 PID 1 的 `init`、PID 2 的 `kthreadd`、以及那堆 `kworker`/`ksoftirqd` 内核线程,是怎么从"一个编译期写死的 0 号进程"一步步繁衍出来的;为什么 0 号进程**永远不死**、为什么**杀掉 init 等于杀掉整座城市**、以及"市民的出生"这件事在内核里到底是怎么落地的。

> **如果一读觉得太难**:先只记住三件事——① **0 号进程是编译期就钉死在内核镜像里的**,它没有父亲,是全城祖先;② `rest_init` 亲手生出 **1 号 `init`(所有用户程序的祖先)** 和 **2 号 `kthreadd`(所有内核线程的祖先)**,且 init 必须先出生以抢 PID 1;③ 0 号进程最后的归宿是 `do_idle`——**永远不死,没活干时睡觉候着**。其余细节第二遍配合源码再抠。

---

## 章首·一句话点破

上一章结尾,`start_kernel` 的最后一行调用了 `rest_init`。注释说 `we're now alive`——城市活了。可"活了"只是说**部门到岗、机器能跑**了,并不等于**有市民**。一个没有市民的城市,再通电也是座空城。

那市民从哪来?这本该是个"鸡生蛋"的死结:

> 市民(进程)是由 `fork`/`kernel_thread` 生出来的;可 `fork` 自己也是个进程在执行——得先有个进程,才能生别的进程。第一个进程是谁生的?

内核的答案极其干脆:**第一个进程不是"生"出来的,是"刻"出来的**——它在编译期就被**静态写死在内核镜像里**,作为一切的祖先。然后由这个祖先,亲手生下后面所有市民。

这一章就讲这三尊祖宗:**0 号(idle)、1 号(init)、2 号(kthreadd)**。

---

## 一、0 号进程:刻在奠基石里的祖先

我们先从最特殊的一个说起:**0 号进程**,也叫 **idle**(在 `comm` 字段里显示为 `swapper`)。它是全城所有进程的终极祖先,也是内核里**唯一一个不是被"生"出来的进程**。

### 不这样会怎样:第一个进程没法被"生"

为什么 0 号进程不能像别的进程一样被 `fork` 生出来?因为 `fork` 本身就是**一个进程在执行**——执行 `fork` 的那个进程,得先存在。如果世界上一个进程都没有,谁来执行第一次 `fork`?这就是死结。

所以内核的做法是:**跳过"生",直接"刻"**。在编译期,就把 0 号进程的完整档案——一个写满默认值的 `struct task_struct`——静态地放进内核镜像。内核一开机,这个档案就**已经在那儿了**,不需要谁去创建它。

### 它在哪:`init_task`

这个静态的 0 号进程,定义在 [init/init_task.c](../linux-6.14/init/init_task.c):

```c
/*
 * Set up the first task table, touch at your own risk!. Base=0,
 * limit=0x1fffff (=2MB)
 */
struct task_struct init_task __aligned(L1_CACHE_BYTES) = {
	...
	.__state	= 0,
	.stack		= init_stack,
	.usage		= REFCOUNT_INIT(2),
	.flags		= PF_KTHREAD,
	.prio		= MAX_PRIO - 20,
	...
	.mm		= NULL,
	.active_mm	= &init_mm,
	...
	.real_parent	= &init_task,
	.parent		= &init_task,
	.children	= LIST_HEAD_INIT(init_task.children),
	.sibling	= LIST_HEAD_INIT(init_task.sibling),
	.group_leader	= &init_task,
	...
	.comm		= INIT_TASK_COMM,
	...
};
EXPORT_SYMBOL(init_task);
```

([init/init_task.c:62-223](../linux-6.14/init/init_task.c#L62-L223),摘录关键字段)

逐字读这段静态初始化,你会发现 0 号进程的几个**身份烙印**:

1. **`.flags = PF_KTHREAD`**:它是个**内核线程**(KTHREAD),不是用户态进程。它永远活在内核态(ring 0),没有用户态地址空间。
2. **`.mm = NULL`**:它**没有自己的用户态地址空间**(内存篇讲过,`mm` 指向进程的页表/地址空间)。0 号进程不需要,因为它只跑内核代码、用内核地址空间。
3. **`.active_mm = &init_mm`**:虽然它没有自己的 `mm`,但调度它时需要一个"临时借用"的地址空间(内核的 `init_mm`)。这是调度器的一个细节,记一笔。
4. **`.real_parent = &init_task` / `.parent = &init_task`**:**它的父亲是它自己**!这印证了"它没有父亲"——没人能生它,所以它的 parent 字段指向自己。这是个很有意思的"自指"。
5. **`.comm = INIT_TASK_COMM`**:`INIT_TASK_COMM` 在 [include/linux/init_task.h:38](../linux-6.14/include/linux/init_task.h#L38) 定义为 `"swapper"`。所以你 `ps` 里看到的 0 号进程名字是 `swapper`(或 idle)。
6. **`.prio = MAX_PRIO - 20`**:**它的优先级是最低档的**。这是它命运的关键——稍后会看到,0 号进程最终的归宿是"没活干时永远睡觉",所以它必须是最低优先级:任何其他进程只要想跑,都能立刻把它从 CPU 上挤下去。

> **比喻**:0 号进程就是新城市的**奠基石**——在一切动工之前就已经埋好了,上面刻着"本城祖先"。它没有父母,没有生辰,从城市存在的那一刻它就在。所有后来的市民,血脉都能追溯到这块奠基石。

### 它何时"动起来"

注意——0 号进程这个 `init_task` 结构体在编译期就存在了,但**编译期它还不"跑"**。它真正"开始执行"的时刻,是在 `start_kernel` 的第一行:

```c
	set_task_stack_end_magic(&init_task);
```

([init/main.c:901](../linux-6.14/init/main.c#L901))

这一行给 0 号进程的栈底打个"魔法标记"(用来检测栈溢出)。**从这一刻起,当前 CPU 正在执行的这个"上下文",就是 0 号进程**——也就是说,`start_kernel` 这个函数,其实**就是 0 号进程在跑**(此时还是它唯一的进程,自己跑自己)。

所以回顾上一章:那条两百多行的大游行,**执行者就是 0 号进程**。它一边挂部门、一边等会儿(在 `rest_init` 里)生下自己的孩子。这是理解启动的一个关键视角:**0 号进程不是被生的,它是"原住民",内核一开机它就在跑 `start_kernel`**。

---

## 二、`rest_init`:亲手生下 1 号和 2 号

大游行走完,0 号进程在 `start_kernel` 的最后一行进入 `rest_init`。这是它**最开天辟地的动作**:亲手生下两个孩子。看真实代码:

```c
static noinline void __ref __noreturn rest_init(void)
{
	struct task_struct *tsk;
	int pid;

	rcu_scheduler_starting();
	/*
	 * We need to spawn init first so that it obtains pid 1, however
	 * the init task will end up wanting to create kthreads, which, if
	 * we schedule it before we create kthreadd, will OOPS.
	 */
	pid = user_mode_thread(kernel_init, NULL, CLONE_FS);
	/*
	 * Pin init on the boot CPU. Task migration is not properly working
	 * until sched_init_smp() has been run.
	 */
	rcu_read_lock();
	tsk = find_task_by_pid_ns(pid, &init_pid_ns);
	tsk->flags |= PF_NO_SETAFFINITY;
	set_cpus_allowed_ptr(tsk, cpumask_of(smp_processor_id()));
	rcu_read_unlock();

	numa_default_policy();
	pid = kernel_thread(kthreadd, NULL, NULL, CLONE_FS | CLONE_FILES);
	rcu_read_lock();
	kthreadd_task = find_task_by_pid_ns(pid, &init_pid_ns);
	rcu_read_unlock();
	...
	system_state = SYSTEM_SCHEDULING;

	complete(&kthreadd_done);

	/*
	 * The boot idle thread must execute schedule()
	 * at least once to get things moving:
	 */
	schedule_preempt_disabled();
	/* Call into cpu_idle with preempt disabled */
	cpu_startup_entry(CPUHP_ONLINE);
}
```

([init/main.c:697-744](../linux-6.14/init/main.c#L697-L744))

这段代码信息量极大,我们拆成三步看。

### 第一步:生 1 号 `init`

```c
	pid = user_mode_thread(kernel_init, NULL, CLONE_FS);
```

([init/main.c:708](../linux-6.14/init/main.c#L708))

[`user_mode_thread`](../linux-6.14/kernel/fork.c#L2883) 是创建一个** destined for user mode** 的线程——它创建出一个新进程,这个进程一开始在内核态跑函数 `kernel_init`,但它的"命运"是之后 `exec` 一个用户程序、变成堂堂正正的用户态进程。这就是 **1 号进程 `init`**。

注意紧接着的几行:

```c
	rcu_read_lock();
	tsk = find_task_by_pid_ns(pid, &init_pid_ns);
	tsk->flags |= PF_NO_SETAFFINITY;
	set_cpus_allowed_ptr(tsk, cpumask_of(smp_processor_id()));
	rcu_read_unlock();
```

([init/main.c:714-718](../linux-6.14/init/main.c#L714-L718))

它把刚出生的 init **钉在启动 CPU(boot CPU)上**(`PF_NO_SETAFFINITY` = 禁止改亲和性,`set_cpus_allowed_ptr` 限定只能在当前 CPU)。为什么?因为此刻**多核调度还没完全就位**(`sched_init_smp` 要等会儿才跑),让 init 乱跑多核会出问题。等会儿 SMP 起来了,这个限制会再放开。注释把原因写得很清楚。

### 第二步:生 2 号 `kthreadd`

```c
	numa_default_policy();
	pid = kernel_thread(kthreadd, NULL, NULL, CLONE_FS | CLONE_FILES);
	rcu_read_lock();
	kthreadd_task = find_task_by_pid_ns(pid, &init_pid_ns);
	rcu_read_unlock();
```

([init/main.c:720-724](../linux-6.14/init/main.c#L720-L724))

这步用 [`kernel_thread`](../linux-6.14/kernel/fork.c)(标准的"创建纯内核线程"接口)生下 **2 号进程 `kthreadd`**——它的函数是 `kthreadd`,任务是把 `kthreadd_task` 这个全局指针记下来。`kthreadd` 是**所有内核线程的祖先**:后面任何子系统想建内核线程(`kthread_create`),都排队找它代生。稍后专门讲。

### 关键追问:为什么 init 必须先于 kthreadd 出生?

仔细看上面——**先生 init(1 号),再生 kthreadd(2 号)**。这个顺序不是随意的。代码上面那段注释说得很直白:

> "We need to spawn init first so that it obtains pid 1, however the init task will end up wanting to create kthreads, which, if we schedule it before we create kthreadd, will OOPS."
>
> (我们需要先生 init,这样它才能拿到 PID 1;但 init 之后会想创建内核线程,如果我们在创建 kthreadd 之前就调度它跑,就会 OOPS。)

这段注释藏了两层意思:

1. **为什么 init 要先**:因为**PID 是按出生顺序分配的**。先出生的拿 PID 1。Unix 世界有个铁律:**PID 1 必须是 init**(它是所有用户进程的祖先、是 systemd/sysvinit 这些"1 号"的宿主)。如果 kthreadd 先生,它就占了 PID 1,那 init 只能拿 PID 2——规矩就破了。所以**为了让 init 抢到 PID 1,必须先生它**。
2. **但又不能让 init 真的先跑**:init 后来会想建内核线程(比如做点驱动探测),可建内核线程要靠 kthreadd。如果 init 在 kthreadd 出生前就被调度跑了,它去找 kthreadd 发现还没有,直接 OOPS。

**解决方案**正是你看到的:**先"生"init(登记 PID 1,但不让它跑)、再生 kthreadd(登记 PID 2)、最后才通过 `complete(&kthreadd_done)` 通知"kthreadd 好了",并 `schedule()` 让调度器开始分发**。这样 init 拿到了 PID 1,但又保证了它真正跑起来时 kthreadd 已经就位。**一个顺序问题,用"登记顺序"和"唤醒时机"两个手段分别解决**——这是非常典型的内核级"时序解耦"。

### 第三步:0 号进程退居幕后

```c
	complete(&kthreadd_done);

	schedule_preempt_disabled();
	cpu_startup_entry(CPUHP_ONLINE);
```

([init/main.c:735-743](../linux-6.14/init/main.c#L735-L743))

- `complete(&kthreadd_done)`:发个信号,告诉正在 `wait_for_completion` 的 1 号 init"kthreadd 好了,你可以继续了"(下面讲 init 时会看到它在等这个)。
- `schedule_preempt_disabled()`:**主动让出 CPU**。从这一刻起,调度器开始工作——init 和 kthreadd 终于可以被调度上 CPU 跑了。注释说"boot idle thread must execute schedule() at least once to get things moving"(启动的 idle 线程至少要执行一次 `schedule` 才能让事情转起来)。
- `cpu_startup_entry(CPUHP_ONLINE)`:0 号进程**正式进入 idle 循环**——从此它就是那个"没活干时永远睡觉"的最低优先级市民。

> 注意 `rest_init` 函数声明里的 `__noreturn`——它**永不返回**。因为 0 号进程一旦走进 `cpu_startup_entry` 的 idle 循环,就再也不会回到 `rest_init` 的调用者了。它的下半生都在 `do_idle` 里。

---

## 三、1 号进程 `init`:从内核线程到用户进程

接下来看 **1 号进程**。它的函数是 `kernel_init`:

```c
static int __ref kernel_init(void *unused)
{
	int ret;

	/*
	 * Wait until kthreadd is all set-up.
	 */
	wait_for_completion(&kthreadd_done);

	kernel_init_freeable();
	/* need to finish all async __init code before freeing the memory */
	async_synchronize_full();

	system_state = SYSTEM_FREEING_INITMEM;
	kprobe_free_init_mem();
	ftrace_free_init_mem();
	kgdb_free_init_mem();
	exit_boot_config();
	free_initmem();
	mark_readonly();
	...
	system_state = SYSTEM_RUNNING;
	numa_default_policy();

	rcu_end_inkernel_boot();

	do_sysctl_args();
	...
```

([init/main.c:1448-1480](../linux-6.14/init/main.c#L1448-L1480))

1 号进程一上来就 `wait_for_completion(&kthreadd_done)`——**等 0 号进程发出"kthreadd 好了"的信号**(就是上面 `rest_init` 里的 `complete(&kthreadd_done)`)。这印证了刚才说的时序解耦:init 一出生就被调度了,但它第一件事就是**原地等**,直到 kthreadd 就位才继续。这样既抢到了 PID 1,又不会在 kthreadd 没好时乱跑。

### `kernel_init_freeable`:真正干活的下半场

接着它调 `kernel_init_freeable`——这才是 init **真正干活**的地方(在 [init/main.c:1538](../linux-6.14/init/main.c#L1538))。它干了几件大事:

```c
static noinline void __init kernel_init_freeable(void)
{
	...
	smp_prepare_cpus(setup_max_cpus);
	workqueue_init();
	...
	do_pre_smp_initcalls();
	...
	smp_init();           /* ← 把其余 CPU 都拉起来 */
	sched_init_smp();     /* ← 多核调度就位 */

	workqueue_init_topology();
	async_init();
	...
	page_alloc_init_late();

	do_basic_setup();     /* ← 上一章讲的:driver_init + do_initcalls,几千个驱动在这起来 */

	...
	wait_for_initramfs();
	console_on_rootfs();

	/*
	 * check if there is an early userspace init.  If yes, let it do all
	 * the work
	 */
	if (init_eaccess(ramdisk_execute_command) != 0) {
		ramdisk_execute_command = NULL;
		prepare_namespace();   /* ← 挂载根文件系统 */
	}
	...
}
```

([init/main.c:1538-1593](../linux-6.14/init/main.c#L1538-L1593),摘录)

注意这里发生了**视角的奇妙的转换**:上一章我们说"`start_kernel` 是 0 号进程在跑",而这里 **`do_basic_setup`(里面跑了几千个 initcall)是 1 号进程 init 在跑**!也就是说,**大游行的尾巴(驱动批量初始化)其实是 init 这个 1 号进程在执行的**,而不是 0 号。0 号在大游行的主线(`start_kernel` 主体)跑完后,就把活儿交给了 init,自己去 idle 了。

这一段还做了**两个启动里程碑**:

- `smp_init()`:**把其余 CPU 全部拉起来**。启动一直只在 boot CPU(0 号核)上跑,到这里才唤醒其他核,多核正式就位。
- `prepare_namespace()` / `wait_for_initramfs()`:**挂载根文件系统**。没有根文件系统,后面 `exec("/sbin/init")` 就没地方找这个文件。

### 脱胎换骨:`exec` 一个用户程序

`kernel_init_freeable` 跑完,回到 `kernel_init`,init 做完最后的清理(释放启动期专用的 `__init` 内存、把内核代码段标记为只读),然后来到**它一生中最关键的一步**——从一个内核线程,**脱胎成用户进程**:

```c
	if (ramdisk_execute_command) {
		ret = run_init_process(ramdisk_execute_command);
		if (!ret)
			return 0;
		pr_err("Failed to execute %s (error %d)\n",
		       ramdisk_execute_command, ret);
	}

	...
	if (execute_command) {
		ret = run_init_process(execute_command);
		if (!ret)
			return 0;
		panic("Requested init %s failed (error %d).",
		      execute_command, ret);
	}

	if (CONFIG_DEFAULT_INIT[0] != '\0') {
		ret = run_init_process(CONFIG_DEFAULT_INIT);
		...
	}

	if (!try_to_run_init_process("/sbin/init") ||
	    !try_to_run_init_process("/etc/init") ||
	    !try_to_run_init_process("/bin/init") ||
	    !try_to_run_init_process("/bin/sh"))
		return 0;

	panic("No working init found.  Try passing init= option to kernel. "
	      "See Linux Documentation/admin-guide/init.rst for guidance.");
}
```

([init/main.c:1482-1521](../linux-6.14/init/main.c#L1482-L1521))

这是一条**逐级降级的查找链**——内核想尽一切办法找到一个能跑的 init 程序:

1. 先试 `ramdisk_execute_command`——它默认是 `/init`([init/main.c:162](../linux-6.14/init/main.c#L162)),也就是 **initramfs(内存根文件系统)里的 init**。现代发行版通常用这个:内核先跑 initramfs 里的 `/init`,它负责加载真正的根文件系统驱动,然后再切换到真正的 init。
2. 再试 `execute_command`——这是你在内核命令行里用 `init=xxx` 指定的程序。如果指定了却跑不起来,直接 `panic`(因为你明确要求了,失败就是失败)。
3. 再试 `CONFIG_DEFAULT_INIT`——编译时配置的默认 init。
4. 最后**挨个试** `/sbin/init`、`/etc/init`、`/bin/init`、`/bin/sh`。前面任意一个跑成功就返回。
5. **一个都找不到** → `panic("No working init found...")`。这就是你偶尔会在没配好根文件系统时看到的、著名的"No working init found"崩溃。

### `run_init_process`:那一声 `execve`

真正"脱胎"的动作在 `run_init_process`:

```c
static int run_init_process(const char *init_filename)
{
	const char *const *p;

	argv_init[0] = init_filename;
	pr_info("Run %s as init process\n", init_filename);
	...
	return kernel_execve(init_filename, argv_init, envp_init);
}
```

([init/main.c:1366-1379](../linux-6.14/init/main.c#L1366-L1379))

[`kernel_execve`](../linux-6.14) 是系统调用 `execve` 的内核内部版本。**这一行是 init 一生的分水岭**:在它之前,init 是个内核线程(跑在 ring 0、没有用户地址空间、`comm` 是 `kernel_init` 的宿主);**`kernel_execve` 之后,它把一个用户态程序(比如 `/sbin/init` 或 systemd)加载进自己的地址空间,从此它变成了一个堂堂正正的用户态进程**(ring 3、有自己的 `mm`、有自己的代码段数据段)。

> **回扣内存篇**:`kernel_execve` 内部会做一系列内存管理的事——建新的地址空间、把可执行文件的代码段/数据段映射进虚拟内存(只建 VMA、不立即分配物理页,内存篇 ch10/11)、设置入口指令地址。**这里就是"造幻觉"开始交付的时刻**:init 从此活在自己独占的虚拟地址空间幻觉里(幻觉二),而它要列文件、要打印,还得靠系统调用穿越 ring 0/ring 3 那道线(P0-01 讲过的)。

> **不这样会怎样**:如果 init 不 `exec` 成用户程序,那它就永远是个内核线程,系统里**永远不会出现第一个用户态进程**——所有用户程序(你的 shell、浏览器)都没法存在,因为它们的祖先是用户态的 init。**`kernel_execve` 这一脚,是把"内核世界"和"用户世界"正式打通的第一脚。**

从 `kernel_execve` 成功返回的那一刻起,init 不再回来——它已经在用户态跑 `/sbin/init` 了。从此,**用户空间的启动交给 init**:它读自己的配置(systemd 的 unit、sysvinit 的脚本),拉起登录服务、网络、cron……给你一个登录界面。**内核的启动使命,到这里彻底完成。**

---

## 四、2 号进程 `kthreadd`:内核线程的祖宗

1 号讲完,回头看 2 号 `kthreadd`。它的函数在 [kernel/kthread.c:818](../linux-6.14/kernel/kthread.c#L818):

```c
int kthreadd(void *unused)
{
	static const char comm[TASK_COMM_LEN] = "kthreadd";
	struct task_struct *tsk = current;

	/* Setup a clean context for our children to inherit. */
	set_task_comm(tsk, comm);
	ignore_signals(tsk);
	set_cpus_allowed_ptr(tsk, housekeeping_cpumask(HK_TYPE_KTHREAD));
	set_mems_allowed(node_states[N_MEMORY]);

	current->flags |= PF_NOFREEZE;
	cgroup_init_kthreadd();

	for (;;) {
		set_current_state(TASK_INTERRUPTIBLE);
		if (list_empty(&kthread_create_list))
			schedule();
		__set_current_state(TASK_RUNNING);

		spin_lock(&kthread_create_lock);
		while (!list_empty(&kthread_create_list)) {
			struct kthread_create_info *create;
			...
```

([kernel/kthread.c:818-840](../linux-6.14/kernel/kthread.c#L818-L840))

`kthreadd` 的结构是**一个永恒的循环 + 一个等待队列**:

- 它大部分时间在 `TASK_INTERRUPTIBLE`(可中断睡眠)状态睡觉,挂在 `kthread_create_list` 这个队列上。
- 任何子系统想建一个内核线程,就调用 `kthread_create` / `kthread_run`,这会把一个"创建请求"塞进 `kthread_create_list`,然后唤醒 kthreadd。
- kthreadd 醒来后,遍历队列,**替每个请求生出一个内核线程**(`kernel_thread`)。

> **为什么内核线程要由 kthreadd "代生",而不是调用者自己 `kernel_thread`?** 历史和工程原因都有:统一由 kthreadd 代生,能保证所有内核线程有**一致的、干净的环境**(继承自 kthreadd 的干净上下文:`ignore_signals` 屏蔽信号、`PF_NOFREEZE` 不被冻结、合适的 CPU 亲和性)。这也是为什么你在 `ps` 里看到的所有 `kworker`、`ksoftirqd`、`migration`、`rcu_*` 这些内核线程,**父进程都是 `[kthreadd]`**——它们都是 kthreadd 代生的孩子。

> **比喻**:kthreadd 就是市政府的**"临时工招聘处"**。各科室需要临时工(内核线程干后台活:回写脏页、处理软中断、做 RCU 回收……),不自己招,统一填张单子交到招聘处。招聘处(kthreadd)永远有人值班,有单子就招人、没单子就打盹。你 `ps` 看到的所有 `[kworker/0:1]` 之类,都是这个招聘处招来的临时工。

---

## 五、0 号进程的归宿:`do_idle`,永远候着

最后回到 0 号进程。它生完 init 和 kthreadd、做完 `schedule()`,就走进了 `cpu_startup_entry`,**再也不回来**。它的下半生,全在 [kernel/sched/idle.c](../linux-6.14/kernel/sched/idle.c) 的 `do_idle` 里:

```c
void cpu_startup_entry(enum cpuhp_state state)
{
	...
	while (1)
		do_idle();
}
```

([kernel/sched/idle.c:417-423](../linux-6.14/kernel/sched/idle.c#L417-L423))

`cpu_startup_entry` 就是一个死循环不停调 `do_idle`。而 `do_idle` 的核心是:

```c
static void do_idle(void)
{
	int cpu = smp_processor_id();
	...
	__current_set_polling();
	tick_nohz_idle_enter();

	while (!need_resched()) {
		...
		local_irq_disable();
		...
		if (cpu_idle_force_poll || tick_check_broadcast_expired()) {
			tick_nohz_idle_restart_tick();
			cpu_idle_poll();
		} else {
			cpuidle_idle_call();    /* ← 真正让 CPU 进入低功耗的指令(mwait/hlt) */
		}
		...
	}
	...
}
```

([kernel/sched/idle.c:252-326](../linux-6.14/kernel/sched/idle.c#L252-L326),摘录核心)

这段代码刻画了 0 号进程的**永恒命运**:

- 它在一个 `while (!need_resched())` 循环里**反复检查"有没有人需要 CPU"**。
- 没人需要(`!need_resched()` 为真)时,它调 `cpuidle_idle_call`,**让 CPU 执行一条"睡觉"指令**(x86 上是 `mwait` 或 `hlt`)——CPU 进入低功耗状态,直到下一个中断把它唤醒。
- 一旦有人需要 CPU(`need_resched()` 变真,通常是被时钟中断或唤醒某个进程设上的),它跳出循环,调度器把别的进程换上 CPU。

> **这就是 0 号进程的"工作":没有工作**。它是全城那个**永远候着的最低优先级市民**——别的市民都没活干时,CPU 不能闲着空转浪费电,于是 0 号进程顶上去,让 CPU 睡觉省电;一旦有活来了(中断、唤醒),它立刻让位。**它既是祖先,又是永远的最后备胎。**

> **为什么 0 号进程不能死?** 因为每个 CPU **在任何时刻都必须有一个进程可调度**——CPU 不能"没进程跑"。当所有真正的活儿都干完、所有进程都在睡觉时,这个 CPU 必须有个"占位进程"顶上去,那就是 0 号进程(准确说是每核一个 idle 任务,boot CPU 上是原始的 0 号 `init_task`,其他核上是各自的 idle 线程)。所以 0 号进程**永生**——它死了,CPU 就没进程可跑了,直接崩。

---

## 关键源码精读:三尊祖宗的"出生证明"

我们把本章最关键的几处,从真实源码里钉一遍。

### 证明一:0 号进程是静态写死的

```c
struct task_struct init_task __aligned(L1_CACHE_BYTES) = {
	...
	.flags		= PF_KTHREAD,
	...
	.real_parent	= &init_task,
	.parent		= &init_task,
	...
	.comm		= INIT_TASK_COMM,   /* = "swapper" */
	...
};
EXPORT_SYMBOL(init_task);
```

([init/init_task.c:66-124](../linux-6.14/init/init_task.c#L66-L124))

- `PF_KTHREAD`:它是内核线程。
- `real_parent`/`parent` 都指向 `&init_task`(自己):**它没有父亲,自指**。
- `comm = "swapper"`:0 号进程的名字。

这段静态初始化就是 0 号进程的"出生证明"——**它不是被生的,是编译期刻在内核镜像里的**。

### 证明二:`rest_init` 的三步出生

```c
	pid = user_mode_thread(kernel_init, NULL, CLONE_FS);   /* ① 生 init(PID 1) */
	...
	pid = kernel_thread(kthreadd, NULL, NULL, CLONE_FS | CLONE_FILES);  /* ② 生 kthreadd(PID 2) */
	...
	complete(&kthreadd_done);      /* ③ 通知 init:kthreadd 好了 */
	schedule_preempt_disabled();   /*    开调度器,让 init/kthreadd 跑起来 */
	cpu_startup_entry(CPUHP_ONLINE);  /* 0 号自己进 idle 循环 */
```

([init/main.c:708-743](../linux-6.14/init/main.c#L708-L743))

三步的顺序锁死了:必须**先生 init(抢 PID 1)、再生 kthreadd、最后开调度**。这个顺序是 PID 分配规则和依赖关系共同逼出来的(见前文"关键追问")。

### 证明三:init 的脱胎换骨

```c
	if (!try_to_run_init_process("/sbin/init") ||
	    !try_to_run_init_process("/etc/init") ||
	    !try_to_run_init_process("/bin/init") ||
	    !try_to_run_init_process("/bin/sh"))
		return 0;
	panic("No working init found. ...");
```

([init/main.c:1513-1520](../linux-6.14/init/main.c#L1513-L1520))

挨个试、都不行就 panic。`run_init_process` 内部的 `kernel_execve` 是那一脚"从内核线程变成用户进程"。**找不到 init,机器直接起不来**——这就是为什么"根文件系统坏了 / init 程序坏了"会得到一个 kernel panic。

### 证明四:0 号的永恒归宿

```c
void cpu_startup_entry(enum cpuhp_state state)
{
	...
	while (1)
		do_idle();
}
```

([kernel/sched/idle.c:417-423](../linux-6.14/kernel/sched/idle.c#L417-L423))

`while (1) do_idle()`——**0 号进程永生,且永远在 idle**。这个死循环,就是 0 号进程的下半生。

---

## 章末小结

### 用城市比喻回顾本章

回到那座开张的城市。这一章我们见证了**城市开张那天最神圣的一幕:第一批市民诞生**。

1. **0 号进程(idle / swapper)**:它不是出生的,是**刻在奠基石里的祖先**——编译期就写死在 `init_task` 里,没有父亲(parent 自指)、是内核线程、优先级最低。它执行了 `start_kernel` 大游行,然后生下两个孩子的 `rest_init`。生完孩子,它**退居幕后**:`cpu_startup_entry` → `while (1) do_idle()`——**永远不死,没活干时让 CPU 睡觉省电,有活被唤醒让位**。它是祖先,也是永恒的最后备胎。

2. **1 号进程(init)**:0 号用 `user_mode_thread(kernel_init)` 生下,**抢到了 PID 1**。它先原地等 kthreadd 就位,然后干完启动的下半场(拉起多核 `smp_init`、跑 `do_basic_setup` 让几千个驱动起来、挂根文件系统),最后 **`kernel_execve("/sbin/init")` 一脚脱胎成用户进程**——从此它是所有用户程序的祖先。**它找不到能跑的 init 程序,就 panic;它死了,全城陪葬**(因为所有用户进程的祖先没了)。

3. **2 号进程(kthreadd)**:0 号用 `kernel_thread(kthreadd)` 生下,**PID 2**。它**永远候着**,谁想建内核线程都填单子找它代生。`ps` 里所有 `[kworker]`、`[ksoftirqd]`、`[migration]`、`[rcu_*]` 的父进程都是它——它是所有内核线程的祖宗、政府的临时工招聘处。

### 本章在全书主线中的位置

记住全书导言的二分法:**内核一半在"管共享资源",一半在"造独占幻觉"**。这一章是这条主线上的一个**关键转折点**:

- **0 号进程 + `rest_init`**,是"管资源"侧的能力**开始运转**的时刻——调度器第一次真正派活(`schedule()`),CPU 第一次真正有进程在跑。
- **1 号 init 的 `kernel_execve`**,是"造幻觉"侧**正式交付**的时刻——从此进程活在自己独占的虚拟地址空间幻觉里(幻觉二),并通过系统调用穿越 ring 0/ring 3 边界(回扣 P0-01 的特权线)。

所以第 1 篇(Booting)到这里**完整收束**了:从一片漆黑 → 内核自举 → memblock 临时记账 → `start_kernel` 大游行挂部门 → `rest_init` 生三尊祖宗 → init 脱胎跑用户空间。**城市,灯火通明。** 后面所有的篇章,都是在这座已经亮灯的城市里,深入讲某个部门。

### 五个"为什么"清单

如果你只能记五件事,记这五件:

1. **0 号进程为什么不是被生的**:第一个进程没法被 `fork`(没有进程能执行第一次 fork),所以它**编译期静态写死**在 `init_task` 里,执行 `start_kernel` 的就是它。
2. **为什么 init 先于 kthreadd 出生**:PID 按出生顺序分配,**先出生拿 PID 1**;Unix 铁律 PID 1 必须是 init。但又不能让 init 真先跑(它要靠 kthreadd 建内核线程),所以"先登记 init、再生 kthreadd、最后才开调度"——用时序解耦解决。
3. **init 怎么变成用户进程**:`kernel_init` 跑完启动下半场后,`kernel_execve("/sbin/init")` 把一个用户程序加载进自己的地址空间——**这一脚打通了内核世界和用户世界**。找不到 init 就 panic。
4. **kthreadd 是干嘛的**:所有内核线程的祖宗。它在永恒循环里睡觉,谁 `kthread_create` 它就代生一个。`ps` 里所有内核线程的父进程都是它。
5. **0 号进程为什么永生**:每个 CPU 任何时刻都必须有进程可调度;别的进程都睡时,要有 idle 顶上去让 CPU 睡觉省电、不空转。0 号就是那个永恒备胎,死了 CPU 就没进程跑。

### 想继续深入,该往哪钻

- **读 0 号进程的完整档案**:[init/init_task.c:66-223](../linux-6.14/init/init_task.c#L66-L223)。每个字段的默认值,都是"全城祖先"该有的样子。
- **读 `rest_init` 全文**:[init/main.c:697-744](../linux-6.14/init/main.c#L697-L744)。短短四十多行,把三尊祖宗的诞生讲完,注释也极有信息量。
- **读 init 的脱胎过程**:[init/main.c:1448-1521](../linux-6.14/init/main.c#L1448-L1521) 的 `kernel_init`,特别是末尾那条降级查找链和 `run_init_process` → `kernel_execve`。
- **读 kthreadd**:[kernel/kthread.c:818](../linux-6.14/kernel/kthread.c#L818)。它的永恒循环和 `kthread_create_list`,是"内核线程招聘处"的全貌。
- **读 idle 循环**:[kernel/sched/idle.c:252](../linux-6.14/kernel/sched/idle.c#L252) 的 `do_idle` 和 [kernel/sched/idle.c:417](../linux-6.14/kernel/sched/idle.c#L417) 的 `cpu_startup_entry`。看 0 号进程怎么在 `while (!need_resched())` 里反复睡觉、让位。
- **想亲眼看看三尊祖宗**:在你的 Linux 机器上跑 `ps -ef | head`,PID 1 是你的 init(systemd/sysvinit),PID 2 是 kthreadd;`ps -ef | grep kthreadd` 能看到所有内核线程的 PPID 都是 2。把这一章的源码和这条命令对照着看,会有"原来如此"的快感。

---

> 城市灯火通明,三尊祖宗就位,init 已经在用户空间跑起来。**第 1 篇·启动 到此结束。** 旅程的下一站,是去认识"市民"本身——进程是什么、`task_struct` 里装了什么、市民怎么生老病死。翻开 **第 3 篇 · 进程**。
