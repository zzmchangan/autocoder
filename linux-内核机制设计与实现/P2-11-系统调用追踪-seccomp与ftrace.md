# 第十一章 · 系统调用追踪:seccomp 与 ftrace

> 篇:P2 系统调用
> 主线呼应:第 8 章讲了 `SYSCALL` 指令怎么把 CPU 切进 ring 0、跳进 `do_syscall_64`,第 9 章讲了参数怎么从用户态寄存器安全地搬进内核,第 10 章讲了 VDSO 怎么让"读时间"这种高频路径**根本不进内核**。但系统调用入口这条路上还能挂别的东西:**入口处**插一道安全闸门(seccomp,在系统调用真正执行**之前**决定要不要让它跑)、**入口和出口处**各挂一个观测探头(ftrace 的 `trace_sys_enter`/`trace_sys_exit`)。这一章把"进内核"这一面的两个横切能力讲透——安全和观测都不是系统调用的本职,但它们都长在系统调用入口这条路上,因为这里是"用户态意图进入内核"的唯一咽喉,想拦截或观测,只能在咽喉处下手。

## 核心问题

**系统调用是用户态合法进内核的唯一入口,怎么在这条入口上"插钩子"?安全(seccomp)怎么在系统调用真正执行**之前**就用 BPF 程序决定放行还是拒绝?观测(ftrace/trace_events)怎么把每一次系统调用的入口、参数、返回值、延迟记录下来?这两件事为什么都非得长在入口路径上,而不能在系统调用执行之后才做?**

读完本章你会明白:

1. **seccomp 是 BPF 的第一个大规模内核用户**:在系统调用入口、真正执行之前,跑一段用户配置的 BPF 程序,拿到系统调用号和六个参数,返回 ALLOW/KILL/TRAP/ERRNO/TRACE/USER_NOTIF 等动作——沙箱、容器安全的核心机制。
2. **入口前过滤的代价远小于"执行后检查"**:seccomp 拦截发生在系统调用**还没碰内核资源**之前,而"执行后再检查"既白交了一次系统调用开销,还可能已经产生副作用(已经写盘、已经发包)。
3. **ftrace 的系统调用埋点是两个 tracepoint**(`trace_sys_enter`/`trace_sys_exit`),由通用入口框架 `kernel/entry/common.c` 在 `syscall_trace_enter`/`syscall_exit_work` 里调用;默认关闭,开启才有开销,这正是"观测不影响未被观测者"的设计。
4. **`/proc/<pid>/syscall` 看到的"当前系统调用号 + 六个参数"**,是内核在入口处把它写进 `task_struct` 留给观测的,配合"进程此刻阻塞在哪个系统调用"能定位生产环境的卡顿。

> **逃生阀**:如果你已经知道 seccomp 是 BPF 过滤、ftrace 是 tracepoint,可以直接跳到 11.4(入口前过滤为什么 sound)和 11.5(技巧精解:`__seccomp_filter` 的动作分派)。但 11.2~11.3 的"钩子长在哪、为什么长在这"推导,即使你懂机制也建议读——它解释了为什么所有"想拦截/观测系统调用"的工具(seccomp、ftrace、ptrace、audit、strace)都挤在同一条入口路径上。

---

## 11.1 一句话点破

> **seccomp 和 ftrace 都是长在系统调用入口这条路上的钩子:seccomp 是一道"闸门",在系统调用真正执行之前用 BPF 程序决定放不放行;ftrace 是两个"探头",在入口和出口各记一笔。它们之所以都长在入口路径,是因为系统调用入口是用户态意图进入内核的唯一咽喉——想拦截、想观测,只能在咽喉处下手,事后补救既慢又不安全。**

这是结论,不是理由。本章倒过来拆:先看系统调用入口这条路上有哪些"挂载点",再看 seccomp 怎么在入口前用 BPF 当闸门、为什么闸门必须在前面,然后看 ftrace 的两个埋点怎么工作、为什么默认关闭,最后讲 `/proc/<pid>/syscall` 这个观测窗口。第 2 篇到此收尾。

---

## 11.2 入口路径上的挂载点:所有钩子都在咽喉处

回忆第 8 章:用户态执行 `SYSCALL` 指令,CPU 切到 ring 0、跳进 `do_syscall_64`,这条路径上有一段通用框架。这段框架不在 `arch/x86/entry/`(那部分 sparse clone 没拉),而在 [kernel/entry/common.c](../linux/kernel/entry/common.c) 里,所有架构共用。

关键的入口分发函数是 `syscall_enter_from_user_mode_work`([entry-common.h:163](../linux/include/linux/entry-common.h#L163-L171)),它读 `current_thread_info()->syscall_work` 这个标志位字段,只要里面有任何"进内核要做的横切工作"位被置上,就调 `syscall_trace_enter` 把这些工作一并跑掉:

```c
/* include/linux/entry-common.h,简化自 syscall_enter_from_user_mode_work */
static __always_inline long syscall_enter_from_user_mode_work(struct pt_regs *regs, long syscall)
{
    unsigned long work = READ_ONCE(current_thread_info()->syscall_work);

    if (work & SYSCALL_WORK_ENTER)
        syscall = syscall_trace_enter(regs, syscall, work);  /* 跑横切工作 */

    return syscall;
}
```

`SYSCALL_WORK_ENTER` 这组标志位([entry-common.h:44](../linux/include/linux/entry-common.h#L44-L47))包括 `SYSCALL_WORK_SECCOMP`(要跑 seccomp)、`SYSCALL_WORK_SYSCALL_TRACEPOINT`(要打 ftrace 埋点)、`SYSCALL_WORK_SYSCALL_TRACE`(被 ptrace 追踪)。**注意这三个标志位的设计**:它们平时是清零的,只有进程主动 `prctl(PR_SET_SECCOMP, ...)`、`echo > /sys/kernel/debug/tracing/events/syscalls/enable`、被 `strace`/`gdb` attach 时才会被置上——这意味着**绝大多数进程跑系统调用时根本不进这条慢路径**,这就是"观测不影响未被观测者"。

进了 `syscall_trace_enter` 之后,真正的"横切工作大集合"就在 [common.c:28](../linux/kernel/entry/common.c#L28-L72)。这一段是全章的罗塞塔石碑,我们贴出来逐行看:

```c
/* kernel/entry/common.c,简化自 syscall_trace_enter */
long syscall_trace_enter(struct pt_regs *regs, long syscall, unsigned long work)
{
    long ret = 0;

    /* 1. Syscall User Dispatch(用户态自己接管系统调用分发,用于 Wine 仿真) */
    if (work & SYSCALL_WORK_SYSCALL_USER_DISPATCH) {
        if (syscall_user_dispatch(regs))
            return -1L;
    }

    /* 2. ptrace(strace/gdb 走这里) */
    if (work & (SYSCALL_WORK_SYSCALL_TRACE | SYSCALL_WORK_SYSCALL_EMU)) {
        ret = ptrace_report_syscall_entry(regs);
        if (ret || (work & SYSCALL_WORK_SYSCALL_EMU))
            return -1L;
    }

    /* 3. seccomp —— 在 ptrace 之后,为了抓 tracer 改的系统调用号 */
    if (work & SYSCALL_WORK_SECCOMP) {
        ret = __secure_computing(NULL);
        if (ret == -1L)
            return ret;
    }

    /* tracer/seccomp 可能改了系统调用号,重新读 */
    syscall = syscall_get_nr(current, regs);

    /* 4. ftrace 的 trace_sys_enter 埋点 */
    if (unlikely(work & SYSCALL_WORK_SYSCALL_TRACEPOINT)) {
        trace_sys_enter(regs, syscall);
        syscall = syscall_get_nr(current, regs);  /* probe 可能又改了 */
    }

    syscall_enter_audit(regs, syscall);  /* 5. audit */
    return ret ? : syscall;
}
```

五个挂载点一字排开,顺序经过精心安排。读这段代码,你能看清"系统调用入口这条路上的所有钩子"长什么样:

```text
 系统调用入口路径上的五个挂载点(kernel/entry/common.c: syscall_trace_enter):

 用户态 SYSCALL 指令
        │ (CPU 切 ring 0,跳 do_syscall_64)
        ▼
 arch 入口 → syscall_enter_from_user_mode()  ← 建立上下文、开中断
        │
        ▼
 syscall_enter_from_user_mode_work()
        │  读 syscall_work,有横切工作?
        │
   ┌────┴────────────────────────────────────────────┐
   │ work 非零才进 syscall_trace_enter():            │
   │                                                   │
   │  ① Syscall User Dispatch(Wine 仿真)            │
   │  ② ptrace(strace/gdb)                            │
   │  ③ seccomp(本章主角之一:入口前 BPF 过滤)      │
   │  ④ trace_sys_enter(本章主角之二:ftrace 埋点)   │
   │  ⑤ audit                                          │
   └────┬─────────────────────────────────────────────┘
        │
        ▼
 根据(可能被改过的)系统调用号 → sys_call_table[nr] 真正执行
        │
        ▼ (系统调用执行完)
 syscall_exit_work() → trace_sys_exit()(出口埋点)
```

> **钉死这件事**:seccomp 在 ③、ftrace 的入口埋点在 ④——**seccomp 在 ftrace 之前**。这个顺序不是随便排的:seccomp 是安全闸门(可能直接拒绝系统调用),ftrace 只是观测。如果一个系统调用要被 seccomp 杀掉,根本不该轮到 ftrace 去记录它"进入了"——所以闸门在前、探头在后。同时 seccomp 在 ptrace(②)之后,这样 tracer 改的系统调用号会被 seccomp 再过滤一遍(注释 `Do seccomp after ptrace, to catch any tracer changes`)。

这一节立住了"入口路径"这个概念。下面看 seccomp 这道闸门到底怎么工作。

---

## 11.3 seccomp:入口前的 BPF 闸门

### 11.3.1 seccomp 是什么:把"哪些系统调用允许"编成一段 BPF 程序

seccomp(secure computing mode)的初心极简:让一个进程**只能调 `read`/`write`/`exit`/`sigreturn` 这几个系统调用**,其它一律杀掉。这是最早的 STRICT 模式(`SECCOMP_MODE_STRICT`),给那种"只算数、不碰系统"的计算密集 helper 进程用的。

但 STRICT 太粗,真实场景要的是**细粒度按系统调用号 + 参数过滤**:允许 `read(fd, ...)` 但只允许 fd=3、允许 `openat` 但只允许只读、拒绝所有 `ptrace`/`mount`/`reboot`……这种过滤规则本质上是一堆 `if (syscall == X && args == Y) return DENY; else return ALLOW;` 的判断——而这正是 BPF(Berkeley Packet Filter)这门小语言最擅长表达的。于是从 2012 年起,seccomp 有了 FILTER 模式(`SECCOMP_MODE_FILTER`):用户配一段 BPF 程序,内核在系统调用入口前跑它,根据返回值决定动作。

> **不这样会怎样**:如果没有 seccomp,想"限制一个进程能调哪些系统调用"只能靠两种土办法:① 用 `LD_PRELOAD` 在用户态 libc 上挂钩子——但这拦不住静态链接程序,也拦不住程序直接发 `syscall` 指令绕过 libc,纯属自欺欺人;② 在内核里给每个系统调用硬编码权限检查——这要改内核源码,且策略写死,无法运行时配置。seccomp 的妙处是**把策略(哪些允许)和机制(在哪拦截、怎么执行判断)分开**:机制固定在入口前跑一段 BPF,策略由用户态 BPF 程序任意写。

seccomp 因此成了 **BPF 在内核里的第一个大规模用户**(早于 eBPF 火起来好几年)。今天我们讲 eBPF,常把它和"网络/XDP/追踪"挂钩,但 seccomp 才是 BPF 进内核的先锋:它证明了一套"用户写小程序、内核安全执行"的模型是可行的,后面的 eBPF 扩展(verifier/JIT/map)都站在这套模型上。seccomp 用的 BPF 程序是经典的 cBPF(`struct sock_filter`),由 `seccomp_check_filter`([seccomp.c:280](../linux/kernel/seccomp.c#L280))校验后转成 eBPF 内部表示,经 JIT 编译执行——这层细节我们点到为止,eBPF 全貌在第 18 本《eBPF 与可观测》详讲。

### 11.3.2 BPF 程序拿到什么:`struct seccomp_data`

BPF 程序跑的时候,输入是一份 `struct seccomp_data`(定义在 `include/uapi/linux/seccomp.h`,未 sparse clone,这里据内核用法描述):

| 字段 | 类型 | 含义 |
|------|------|------|
| `nr` | `int` | 系统调用号(如 x86_64 上 `read`=0、`write`=1) |
| `arch` | `__u32` | 指令集标识(`AUDIT_ARCH_X86_64` 等,防 32/64 位混淆攻击) |
| `instruction_pointer` | `__u64` | 用户态触发系统调用那条指令的地址 |
| `args[6]` | `__u64[6]` | 六个系统调用参数(rdi/rsi/rdx/r10/r8/r9 的值,不解引用) |

这份 `seccomp_data` 由 `populate_seccomp_data`([seccomp.c:246](../linux/kernel/seccomp.c#L246-L266))从 `pt_regs` 里填出来:

```c
/* kernel/seccomp.c,简化自 populate_seccomp_data */
struct task_struct *task = current;
struct pt_regs *regs = task_pt_regs(task);
unsigned long args[6];

sd->nr = syscall_get_nr(task, regs);          /* 系统调用号 */
sd->arch = syscall_get_arch(task);            /* 防跨架构绕过 */
syscall_get_arguments(task, regs, args);      /* 六个参数 */
sd->args[0] = args[0]; /* ... */ sd->args[5] = args[5];
sd->instruction_pointer = KSTK_EIP(task);     /* 用户态 PC */
```

> **钉死这件事**:`args[6]` 是参数**值**,不是参数指向的内容。也就是说 seccomp BPF 能看到 `openat(AT_FDCWD, "/etc/passwd", O_RDONLY)` 里的第二个参数(那个字符串的指针值),但**看不到字符串内容**。BPF 程序不能解引用用户态指针(那是 `copy_from_user` 的活,且 BPF verifier 不允许任意内存访问)。所以 seccomp 能按"系统调用号 + 参数数值"过滤,但不能按"参数指向的字符串内容"过滤——这是 seccomp 的能力边界,也是它的安全边界(BPF 程序不会因为碰坏用户态指针而 fault)。

### 11.3.3 返回值即动作:ALLOW/KILL/TRAP/ERRNO/TRACE/USER_NOTIF

BPF 程序返回一个 32 位整数,高 16 位是**动作**(action),低 16 位是**数据**(data),由宏 `SECCOMP_RET_ACTION_FULL` 和 `SECCOMP_RET_DATA` 分开(见 [seccomp.c:1223-1224](../linux/kernel/seccomp.c#L1223-L1224) 的 `data = filter_ret & SECCOMP_RET_DATA; action = filter_ret & SECCOMP_RET_ACTION_FULL;`)。动作有这些(值见 `include/uapi/linux/seccomp.h`):

| 动作 | 含义 | 数据字段用途 |
|------|------|-------------|
| `SECCOMP_RET_ALLOW` | 放行 | — |
| `SECCOMP_RET_KILL_THREAD` | 杀当前线程(SIGSYS) | — |
| `SECCOMP_RET_KILL_PROCESS` | 杀整个进程 | — |
| `SECCOMP_RET_TRAP` | 给进程发 SIGSYS(可被 `sigaction` 接) | 16 位用户数据,塞进 `siginfo` |
| `SECCOMP_RET_ERRNO` | 系统调用直接返回 `-errno` | errno 值(≤ MAX_ERRNO) |
| `SECCOMP_RET_TRACE` | 通知 ptrace tracer 决定 | 传给 tracer 的消息 |
| `SECCOMP_RET_USER_NOTIF` | 把决定权交给用户态监督进程(`/proc/<pid>/seccomp` 监听者) | — |
| `SECCOMP_RET_LOG` | 记日志后放行(用于灰度测试策略) | — |

这八种动作覆盖了从"放行"到"杀进程"的整个谱系,是 seccomp 表达力的核心。具体怎么分派,看 `__seccomp_filter` 的 switch(下一节技巧精解详拆)。这里先理解一件事:**返回 `SECCOMP_RET_ERRNO` 时,系统调用根本没执行**,内核直接把 `-errno` 塞进返回值寄存器,然后跳过系统调用体——这正是"入口前过滤"的本质。

### 11.3.4 过滤器是栈:多个 BPF 程序串联,最严的赢

一个进程可以装多个 seccomp 过滤器(`seccomp_set_mode_filter` 可重复调),它们组织成一条链(`current->seccomp.filter` 指向最新的,各 filter 用 `->prev` 串成栈)。`seccomp_run_filters`([seccomp.c:406](../linux/kernel/seccomp.c#L406-L434))遍历整条链,**所有过滤器都要跑,动作值最小的(最严的)赢**:

```c
/* kernel/seccomp.c,简化自 seccomp_run_filters */
u32 ret = SECCOMP_RET_ALLOW;  /* 起始假设放行 */
struct seccomp_filter *f = READ_ONCE(current->seccomp.filter);

if (seccomp_cache_check_allow(f, sd))   /* 命中 allow 缓存,直接放行 */
    return SECCOMP_RET_ALLOW;

/* 所有过滤器都跑一遍,动作值最小的(最严的)赢 */
for (; f; f = f->prev) {
    u32 cur_ret = bpf_prog_run_pin_on_cpu(f->prog, sd);
    if (ACTION_ONLY(cur_ret) < ACTION_ONLY(ret)) {
        ret = cur_ret;
        *match = f;
    }
}
return ret;
```

> **所以这样设计**:`SECCOMP_RET_ALLOW`(0x7fff0000)值最大,`SECCOMP_RET_KILL_PROCESS`(0x80000000)值最小。多个过滤器串联时,只要有一个说"杀",最终就是杀——这是安全策略的"最严优先"语义,确保子过滤器(比如容器运行时加的)无法放宽父过滤器(比如 seccomp profile 加的)的约束。容器场景里 Docker 先装一层、Pod 再装一层、应用再装一层,层层收紧、谁都不能放宽,这正是链式"最小值赢"保证的。

> **钉死这件事**:seccomp 过滤器一旦装上,**只能加严、不能放松**,而且子进程 `fork`/`exec` 会继承整条链。这是 seccomp 作为沙箱基座的根本保证——你给一个 helper 进程装了 seccomp,它再 `execve` 出来的任何程序都跑不出这道闸门(除非程序本身有 `CAP_SYS_ADMIN` 且没设 `no_new_privs`,见 [seccomp.c:670](../linux/kernel/seccomp.c#L670-L672) 的安装前置检查)。

---

## 11.4 入口前过滤为什么 sound:事后检查又慢又危险

讲到这里,seccomp 的核心问题可以正面回答了:**为什么非得在系统调用执行之前过滤?执行之后检查不行吗?**

设想一种"事后检查"的替代设计:系统调用照常执行,执行完后内核再看 BPF 程序说"这个不该允许",然后回滚。这听起来也能实现安全,实际上有两个致命问题。

**第一个问题:白交了一次系统调用开销,且可能已经产生副作用。** 拿 `write(fd, buf, n)` 举例:如果策略是"禁止写 fd=5",事后检查意味着这次 `write` 已经把数据真写到 fd=5 指向的文件/管道/套接字里了——网络包已经发出去、文件已经落盘、管道对端已经收到数据。这时再"回滚"要么不可能(包已经上网)、要么极贵(要记 undo log)。而入口前过滤(`__secure_computing` 返回 `-1`,系统调用体根本不执行)是**零副作用**的:系统调用号刚进内核、参数还在寄存器里、内核什么都没动,直接返回 `-EPERM`,干净利落。

> **反面对比**:朴素地写"事后回滚"会撞上**副作用不可逆**这堵墙。一次 `reboot` 系统调用如果事后才发现该禁,机器已经重启了;一次 `mount` 事后才发现该禁,mount 已经生效。入口前过滤把"检查"放在"执行"之前,根本不给副作用发生的机会——这是安全机制最该长的位置。

**第二个问题:性能。** 事后检查意味着每次系统调用都要先完整执行、再跑 BPF、再(可能)回滚——开销是"系统调用执行 + BPF + 回滚"三份。入口前过滤只有"BPF"一份,而且 BPF 程序通常只有几条指令(典型的 seccomp profile 也就几十条 BPF),开销远小于一次真正的系统调用。对高频系统调用(网络服务器每秒百万次 `epoll_wait`/`recvmsg`),这个差距会被放大成可观的 CPU 占用。

> **钉死这件事**:seccomp 的"入口前过滤"不是一种实现选择,是**安全语义的要求**——只有在副作用产生之前拦截,拦截才有意义。"事后检查 + 回滚"在大多数系统调用上根本做不到(副作用不可逆)。这就是为什么 seccomp 必须长在系统调用入口这条路上、而且在系统调用体执行之前(`syscall_trace_enter` 在 `sys_call_table[nr]()` 之前调用,见 [entry/common.c:28-72](../linux/kernel/entry/common.c#L28-L72) 整个函数返回后才轮到真正的系统调用)。

### 11.4.1 容器为什么离不开 seccomp

把这层"入口前闸门"放到容器场景,你就理解它为什么是容器安全的基石。一个容器里跑着用户不可信代码,Docker 默认给它装一层 seccomp profile,**默认黑名单禁掉几十个危险系统调用**:`keyctl`(内核密钥环)、`mount`/`umount`(挂载)、`reboot`(重启)、`pivot_root`/`chroot`(改根,绕过命名空间)、`kexec_load`(换内核)、`bpf`(直接加载 eBPF,绕过 seccomp 自己)、`perf_event_open`(性能计数器,可侧信道)、`ptrace`(进程注入)……这些系统调用每一个都能成为容器逃逸或提权的跳板。

```text
 容器里一次被 seccomp 拦截的系统调用(简化时序):

  容器进程:  syscall(__NR_mount, ...)
      │
      │ (SYSCALL 指令,CPU 切 ring 0)
      ▼
  内核入口 → syscall_enter_from_user_mode_work()
      │   syscall_work 有 SECCOMP 位(容器启动时 docker 装的)
      ▼
  syscall_trace_enter() → __secure_computing()
      │
      ▼
  __seccomp_filter() → seccomp_run_filters()
      │   跑 BPF:nr == __NR_mount → 返回 SECCOMP_RET_ERRNO(EPERM)
      │
      ▼
  action = SECCOMP_RET_ERRNO → syscall_set_return_value(-EPERM)
      │   return -1  (系统调用体 mount() 根本没执行!)
      ▼
  容器进程:  mount() 返回 -1, errno = EPERM
             (内核里什么都没动,零副作用)
```

如果没有 seccomp,这些系统调用的拦截就只能靠"每个系统调用体内自己检查权限",既分散又容易漏——历史上有不少容器逃逸 CVE 就是某个系统调用忘了检查 namespace/capability。seccomp 在入口处一刀切,把整类系统调用挡在内核之外,是纵深防御的关键一层。

---

## 11.5 技巧精解:seccomp 的入口前过滤与 allow 缓存

这一节挑两个最硬核的技巧拆透。

### 技巧一:`__seccomp_filter` 的动作分派 —— 一个 switch 决定系统调用的命运

seccomp 的全部动作语义,集中在 [seccomp.c:1203](../linux/kernel/seccomp.c#L1203-L1326) 的 `__seccomp_filter`。这个函数是 `__secure_computing`([seccomp.c:1337](../linux/kernel/seccomp.c#L1337-L1363))在 FILTER 模式下调用的核心,我们贴关键路径:

```c
/* kernel/seccomp.c,简化自 __seccomp_filter(去掉 USER_NOTIF/LOG 分支) */
static int __seccomp_filter(int this_syscall, const struct seccomp_data *sd,
                            const bool recheck_after_trace)
{
    u32 filter_ret, action;
    struct seccomp_filter *match = NULL;
    int data;
    struct seccomp_data sd_local;

    smp_rmb();   /* 配合 seccomp_assign_mode 的 smp_mb__before_atomic */

    if (!sd) {
        populate_seccomp_data(&sd_local);   /* 入口路径传 NULL,这里填 */
        sd = &sd_local;
    }

    filter_ret = seccomp_run_filters(sd, &match);
    data   = filter_ret & SECCOMP_RET_DATA;
    action = filter_ret & SECCOMP_RET_ACTION_FULL;

    switch (action) {
    case SECCOMP_RET_ERRNO:
        if (data > MAX_ERRNO) data = MAX_ERRNO;     /* errno 上限保护 */
        syscall_set_return_value(current, current_pt_regs(), -data, 0);
        goto skip;                                  /* 直接伪造返回值,跳过系统调用 */

    case SECCOMP_RET_TRAP:
        syscall_rollback(current, current_pt_regs());
        force_sig_seccomp(this_syscall, data, false);  /* 发 SIGSYS */
        goto skip;

    case SECCOMP_RET_KILL_THREAD:
    case SECCOMP_RET_KILL_PROCESS:
    default:
        current->seccomp.mode = SECCOMP_MODE_DEAD;   /* 进 DEAD 态,再进系统调用直接杀 */
        seccomp_log(this_syscall, SIGSYS, action, true);
        syscall_rollback(current, current_pt_regs());
        force_sig_seccomp(this_syscall, data, true); /* coredump + SIGSYS */
        return -1;                                   /* skip syscall,直接去信号处理 */

    case SECCOMP_RET_ALLOW:
        return 0;   /* 唯一让系统调用正常执行的出口 */
    }
skip:
    seccomp_log(this_syscall, 0, action, match ? match->log : false);
    return -1;       /* -1 = 跳过系统调用体 */
}
```

逐行看这里的精妙:

**① `goto skip` 是"跳过系统调用体"的统一出口。** 凡是不放行的动作(ERRNO/TRAP/KILL),`__seccomp_filter` 都返回 `-1`。回到 [entry/common.c:51](../linux/kernel/entry/common.c#L51-L55),`syscall_trace_enter` 看到 `__secure_computing` 返回 `-1L` 就直接 `return ret`,不再往下走;再往上 `syscall_enter_from_user_mode_work` 把 `-1` 传回 arch 入口,arch 入口看到系统调用号是 `-1` 就**不调** `sys_call_table[nr]()`,直接走退出路径。**系统调用体根本没执行**——这就是"入口前过滤"的代码体现。

**② `SECCOMP_RET_ERRNO` 直接伪造返回值。** 注意 `syscall_set_return_value(current, current_pt_regs(), -data, 0)`——它把 `-errno` 直接写进 `pt_regs` 的返回值寄存器(rax),等返回用户态时用户进程看到的就像是 `mount()` 正常执行完返回了 `-EPERM`。用户进程完全不知道自己被 seccomp 拦了(除非它查 `prctl(PR_GET_SECCOMP)`),这种透明性让 seccomp 可以无侵入地加在任意进程上。

**③ `SECCOMP_MODE_DEAD` 是一道死锁门。** KILL 动作把 mode 改成 `SECCOMP_MODE_DEAD`,之后这个进程**任何**系统调用都会在 `__secure_computing` 里走 `case SECCOMP_MODE_DEAD: do_exit(SIGKILL)`([seccomp.c:1356](../linux/kernel/seccomp.c#L1356-L1359))。这保证一个被 seccomp 判死刑的进程连"挣扎着调个 `write` 留遗言"都做不到,防止它在被信号杀死前再触发别的系统调用。

> **反面对比**:如果 KILL 动作不设 DEAD 态、只是发个 SIGSYS 就返回,被杀进程在信号真正投递前(信号延迟到返回用户态才处理,见第 18 章)还能再发几个系统调用——可能正好是那些被禁的危险系统调用。DEAD 态把"判死刑"和"立即不可再进任何系统调用"绑成原子,关死了这个时间窗。这是 seccomp 和信号延迟投递语义配合的精妙之处。

**④ `smp_rmb()` 配合 `seccomp_assign_mode` 的 `smp_mb__before_atomic`。** `seccomp_assign_mode`([seccomp.c:449](../linux/kernel/seccomp.c#L449-L465))在装过滤器时先设 `task->seccomp.mode`,再 `smp_mb__before_atomic()` 内存屏障,最后 `set_task_syscall_work(task, SECCOMP)` 置上 `SYSCALL_WORK_SECCOMP` 位。`__seccomp_filter` 进来第一件事是 `smp_rmb()`:保证读到的 `mode` 一定在 `SYSCALL_WORK_SECCOMP` 位被置之前就生效。这是无锁安装过滤器的关键——另一个线程装 seccomp 时,本线程要么完全看不到(不跑 seccomp),要么看到完整的 mode 和 filter,不会看到"位已置但 mode 没设"的中间态。**这是 seccomp 为什么 sound 的并发保证。**

### 技巧二:`seccomp_run_filters` 之前的 allow 缓存 —— 让高频允许的系统调用几乎零开销

seccomp 有个性能隐患:BPF 程序再短,也是几十条指令,一个网络服务器每秒调百万次 `recvmsg`,如果每次都跑一遍 BPF,开销可观。内核的优化是 **allow 缓存**(action cache,见 [seccomp.c:351-389](../linux/kernel/seccomp.c#L351-L389)):装过滤器时,**预先算一遍哪些系统调用号会返回 ALLOW**,把这些号记进一个 bitmap;运行时 `seccomp_run_filters` 一上来先查 bitmap:

```c
/* kernel/seccomp.c,简化自 seccomp_run_filters 开头 */
if (seccomp_cache_check_allow(f, sd))
    return SECCOMP_RET_ALLOW;   /* 命中缓存,跳过整条 BPF 链 */
```

`seccomp_cache_check_allow` 的核心是一次 `test_bit`([seccomp.c:359](../linux/kernel/seccomp.c#L359)):

```c
/* kernel/seccomp.c,简化自 seccomp_cache_check_allow_bitmap */
static inline bool seccomp_cache_check_allow_bitmap(const void *bitmap,
                                                    size_t bitmap_size,
                                                    int syscall_nr)
{
    if (unlikely(syscall_nr < 0 || syscall_nr >= bitmap_size))
        return false;
    syscall_nr = array_index_nospec(syscall_nr, bitmap_size);  /* 防 Spectre */
    return test_bit(syscall_nr, bitmap);
}
```

注意两个细节:**(a) 只缓存 ALLOW 动作**——因为只有 ALLOW 是"不需要做任何事"的,可以安全跳过 BPF;其它动作(ERRNO/TRAP/KILL)可能带 data 字段、可能有副作用,不能缓存。**(b) `array_index_nospec` 防 Spectre 侧信道**——系统调用号来自用户态,用越界值去索引 bitmap 会被 Spectre 类攻击利用来泄漏内核内存,`array_index_nospec` 在推测执行路径上把索引夹在合法范围内,堵住这个侧信道。

> **反面对比**:如果不做 allow 缓存,朴素地每次系统调用都跑一遍 BPF,对高频系统调用(网络、futex、时钟)开销会被放大百万倍。allow 缓存把"绝大多数系统调用都允许"这种常见场景优化成**一次 `test_bit`(单条 BT 指令)**,让 seccomp 在生产环境几乎零开销。这正是 seccomp 能默认开启在 Docker/Flatpak/Chrome 渲染器里的前提——没有这个优化,容器/沙箱的 CPU 开销会让人望而却步。`array_index_nospec` 那一行则是内核应对 CPU 推测执行漏洞的标配,在第 13 本《同步原语》讲内存序时会再遇到。

---

## 11.6 ftrace 与 trace_events:入口和出口的观测探头

seccomp 是"闸门",ftrace 则是"探头"。Linux 有两套观测系统调用的机制,都和入口路径相关,但层次不同。

### 11.6.1 两个 tracepoint:`trace_sys_enter` 和 `trace_sys_exit`

第一套是 **trace_events 的 `syscalls` 子系统**,在 [kernel/trace/trace_syscalls.c](../linux/kernel/trace/trace_syscalls.c) 里实现。它给系统调用入口和出口各定义一个 tracepoint(定义在 `include/trace/events/syscalls.h`,sparse clone 未拉,据内核用法描述),分别是:

- `trace_sys_enter(regs, syscall)`:系统调用**进入**时触发,记录系统调用号、参数。
- `trace_sys_exit(regs, ret)`:系统调用**退出**时触发,记录返回值。

这两个 tracepoint 的调用点就在通用入口框架里。入口埋点在 [entry/common.c:60](../linux/kernel/entry/common.c#L60-L67)(`syscall_trace_enter` 里),出口埋点在 [entry/common.c:168](../linux/kernel/entry/common.c#L168-L169)(`syscall_exit_work` 里):

```c
/* kernel/entry/common.c,入口埋点(syscall_trace_enter 内) */
if (unlikely(work & SYSCALL_WORK_SYSCALL_TRACEPOINT)) {
    trace_sys_enter(regs, syscall);
    syscall = syscall_get_nr(current, regs);  /* probe 可能改了系统调用号 */
}

/* kernel/entry/common.c,出口埋点(syscall_exit_work 内) */
if (work & SYSCALL_WORK_SYSCALL_TRACEPOINT)
    trace_sys_exit(regs, syscall_get_return_value(current, regs));
```

注意两件事:**(1)** 两个埋点都受 `SYSCALL_WORK_SYSCALL_TRACEPOINT` 位控制,而这个位**默认是关的**——只有当有人去 `echo 1 > /sys/kernel/debug/tracing/events/syscalls/sys_enter_*/enable` 或者 perf/bpftrace 挂了 tracepoint probe 时,内核才会把这个位置上([trace_syscalls.c:368](../linux/kernel/trace/trace_syscalls.c#L368-L404) 的 `reg_event_syscall_enter` 调 `register_trace_sys_enter`)。**(2)** `trace_sys_enter`/`trace_sys_exit` 是 tracepoint 宏展开后的函数,内部有个 static key(jump label),**没人挂 probe 时它编译成一条 `jmp` 直接跳过,近乎零开销**——这是 ftrace tracepoint 设计的核心,让"埋点常驻但默认免费"成为可能。

> **不这样会怎样**:如果入口/出口埋点是硬编码的函数调用(没有 static key),那即使没人观测,每一次系统调用都要付一次函数调用 + 记录的开销,64 核机器每秒上亿次系统调用,这个开销会吃掉几个百分点的 CPU。static key(jump label)把"未启用"的路径优化成一条 NOP/`jmp`,让埋点在没有消费者时几乎不存在。这套机制和第 18 本《eBPF》讲的 tracepoint hook、第 13 本《同步原语》讲的 `static_branch` 是同一套基础设施。

埋点常驻但默认免费的代价是:启用 `events/syscalls` 会同时打开入口和出口埋点,这时每一次系统调用都要往 ring buffer 写一条记录,开销陡升(几微秒/次)。所以**生产环境长开 `syscalls` tracepoint 是不合适的**,只适合短时诊断。这正是观测工具的设计权衡:"平时免费,用时付钱"。

### 11.6.2 strace 用的是 ptrace,不是 ftrace

很多人以为 `strace` 用的是 ftrace,其实不是。`strace` 底层走 **ptrace**(系统调用 `ptrace(PTRACE_SYSCALL,...)`),对应 `SYSCALL_WORK_SYSCALL_TRACE` 位,在 `syscall_trace_enter` 里走的是第 ② 个挂载点([entry/common.c:44](../linux/kernel/entry/common.c#L44-L48))——`ptrace_report_syscall_entry`。ptrace 比 tracepoint 重得多:每次系统调用都要让被追踪进程停下来、发 SIGCHLD 通知 tracer、tracer `wait` 后读写被追踪进程的寄存器、再让它继续——**两次上下文切换 + 两次信号投递**,所以 `strace` 下程序会慢 10~100 倍。

| 工具 | 底层机制 | `SYSCALL_WORK` 位 | 开销 | 用途 |
|------|---------|-------------------|------|------|
| `strace`/`gdb` | ptrace | `SYSCALL_TRACE` | 每次 syscalls 两次上下文切换,慢 10~100x | 调试、改寄存器 |
| ftrace `events/syscalls` | tracepoint | `SYSCALL_TRACEPOINT` | 启用时每系统调用几 us,未启用近零 | 短时观测、统计 |
| perf/bpftrace `tracepoint:syscalls:` | tracepoint(perf 接口) | `SYSCALL_TRACEPOINT` | 同上,可采样 | 性能剖析 |
| `/proc/<pid>/syscall` | 读 `task_struct`(只在进程阻塞时) | 不置位 | 零 | 看当前卡在哪个系统调用 |

这张表把"观测系统调用"的工具谱系理清了:ptrace 最强(能改寄存器)但最慢,tracepoint 中等(只读但快),`/proc/<pid>/syscall` 最轻(零开销但只能看当前一个)。

### 11.6.3 `/proc/<pid>/syscall`:看进程此刻卡在哪个系统调用

最后这个观测窗口值得单独说。`cat /proc/<pid>/syscall` 会输出一行:

```
7 0x3 0x7ffd12345678 0x100 0x0 0x0 0x0 0x0 0x7ffd12340000
```

第一个数字是**系统调用号**(7 是 x86_64 的 `poll`),后面六个是参数,最后一个是用户态栈指针。如果进程没在系统调用里(在用户态跑、或者刚被调度出去但没阻塞),输出 `running`。

这个文件由 `fs/proc/base.c` 的 `proc_pid_syscall` 生成(该文件 sparse clone 未拉,据内核用法描述)。它不靠 tracepoint、不靠 ptrace,**直接读目标进程 `task_struct` 里的 `pt_regs`**——所以零开销,但**只能在进程阻塞在系统调用里时读**(否则读到的寄存器可能是上次系统调用的残留)。它的典型用法是生产环境定位"某个进程卡住了":连续 `cat /proc/<pid>/syscall` 几次,如果一直是同一个系统调用号 + 同一个参数,就知道它卡在哪个系统调用的哪个参数上(比如卡在 `futex` 等锁、卡在 `recvmsg` 等网络、卡在 `read` 等磁盘)。

> **钉死这件事**:seccomp 和 ftrace/ptrace 都长在 `syscall_trace_enter` 这条路上,但**默认都不启用**(`SYSCALL_WORK` 相关位默认清零)。这是 Linux 入口框架的核心设计:**观测和安全是可选的横切能力,绝大多数进程跑系统调用时走的是直通快路径,不进 `syscall_trace_enter`**。只有主动装 seccomp、主动开 ftrace、主动 attach ptrace,这些钩子才生效。这就是为什么"系统能装上各种观测/安全工具但平时不拖慢业务"。

---

## 章末小结

这一章把"进内核"这一面的两个横切能力讲完了。系统调用入口这条路上,除了第 8、9 章讲的正常入口流程,还长着两类钩子:

1. **seccomp(闸门)**:在系统调用真正执行**之前**,跑一段用户配置的 BPF 程序,按系统调用号 + 参数返回 ALLOW/KILL/TRAP/ERRNO/TRACE/USER_NOTIF。它是 BPF 在内核的第一个大规模用户,是容器/沙箱安全的基石。
2. **ftrace(探头)**:在入口和出口各埋一个 tracepoint(`trace_sys_enter`/`trace_sys_exit`),由 static key 控制,默认近零开销,启用后记录每一次系统调用。`/proc/<pid>/syscall` 则是更轻的观测窗口,零开销但只能看当前阻塞的系统调用。

它们都长在 [kernel/entry/common.c](../linux/kernel/entry/common.c) 的 `syscall_trace_enter` 里,顺序是 Syscall User Dispatch → ptrace → **seccomp** → **ftrace 入口埋点** → audit——闸门在前、探头在后,且全部默认关闭(`SYSCALL_WORK` 相关位清零),只有主动启用才进慢路径。

**回扣二分法**:seccomp 和 ftrace 都服务"进内核"这一面——它们都长在系统调用入口路径上,因为入口是用户态意图进入内核的唯一咽喉。seccomp 在"进内核"这个动作完成之前(系统调用体执行之前)就决定要不要让这次进入生效,这是它能做到零副作用拦截的根本原因。ftrace 则记录"进/出内核"这个边界事件本身。两者都不改变"进内核"的本质,只是给这条边界加上安全和观测的横切能力。

至此第 2 篇(系统调用)收尾。从第 8 章 `SYSCALL` 指令怎么切 ring 0,到第 9 章参数怎么安全搬运,到第 10 章 VDSO 怎么避免进内核,再到本章入口上能挂的 seccomp/ftrace 钩子——读者该能在脑子里放映出"一次系统调用从用户态 SYSCALL 到内核返回"的完整画面,以及这条路上每一个驿站。

### 五个"为什么"清单

1. **为什么 seccomp 必须在系统调用执行之前过滤,而不是事后回滚?** 事后回滚面对的是已经产生的副作用(网络包已发、文件已写、`reboot` 已执行),大多数系统调用的副作用不可逆;而且事后检查要付"系统调用执行 + BPF + 回滚"三份开销。入口前过滤零副作用、只有 BPF 一份开销,是安全语义和性能的双重要求。
2. **为什么 seccomp 的多个过滤器是"最严优先"而不是"最后一个赢"?** 因为安全策略要求子过滤器不能放宽父过滤器的约束——容器场景里 Docker/Pod/应用层层收紧,任何一层说"杀"就必须杀,这样沙箱才不会被内层逃逸。`seccomp_run_filters` 取动作值最小(最严)的那个返回。
3. **seccomp 和 eBPF 什么关系?** seccomp 是 BPF 在内核的第一个大规模用户(2012 年),它证明了"用户写小程序、内核安全执行"的模型可行,后来的 eBPF(verifier/JIT/map)站在这套模型上。seccomp 用的是经典 cBPF(`struct sock_filter`),经 `seccomp_check_filter` 校验后转 eBPF 内部表示执行。
4. **为什么 ftrace 的系统调用埋点默认近零开销?** 因为 tracepoint 用 static key(jump label),没人挂 probe 时编译成一条 `jmp` 直接跳过;只有 `echo 1 > events/syscalls/enable` 或 perf/bpftrace 挂 probe 时才把位打开、真正记录。这让"埋点常驻但默认免费"成为可能。
5. **`/proc/<pid>/syscall` 为什么零开销,而 strace 慢 10~100 倍?** `/proc/<pid>/syscall` 直接读目标进程 `task_struct` 的 `pt_regs`,不挂任何钩子,所以零开销,但只能在进程阻塞时读。strace 走 ptrace,每次系统调用要两次上下文切换 + 两次信号投递,代价巨大。

### 想继续深入往哪钻

- **源码**:[kernel/seccomp.c](../linux/kernel/seccomp.c) 的 `__seccomp_filter`(L1203)、`seccomp_run_filters`(L406)、`populate_seccomp_data`(L246)、`seccomp_cache_check_allow`(L369)、`__secure_computing`(L1337)、`seccomp_assign_mode`(L449)、`SYSCALL_DEFINE3(seccomp`(L2071);[kernel/entry/common.c](../linux/kernel/entry/common.c) 的 `syscall_trace_enter`(L28)、`syscall_exit_work`(L149);[kernel/trace/trace_syscalls.c](../linux/kernel/trace/trace_syscalls.c) 的 `reg_event_syscall_enter`(L368)、`event_class_syscall_enter`(L484);[include/linux/entry-common.h](../linux/include/linux/entry-common.h) 的 `syscall_enter_from_user_mode_work`(L163)、`SYSCALL_WORK_ENTER`(L44);[include/linux/thread_info.h](../linux/include/linux/thread_info.h) 的 `SYSCALL_WORK_SECCOMP`/`SYSCALL_WORK_SYSCALL_TRACEPOINT`(L51-L53)。
- **观测**:开 `events/syscalls` 看 `trace_pipe`(`echo 1 > /sys/kernel/debug/tracing/events/syscalls/enable; cat trace_pipe`);`cat /proc/<pid>/syscall` 看当前阻塞系统调用;`perf trace` perf 接口的系统调用追踪;`bpftrace -e 'tracepoint:syscalls:sys_enter_openat { @[comm] = count(); }'` 统计谁在开文件;`strace -c -p <pid>` 用 ptrace 汇总(慢);libseccomp 的 `seccomp-tools dump <pid>` 反汇编装上的 seccomp 过滤器。
- **延伸**:第 18 本《eBPF 与可观测》详讲 BPF verifier/JIT/seccomp 和 eBPF 的关系;`Documentation/userspace-api/seccomp_filter.rst` 是 seccomp 用户态 API 权威文档;`samples/seccomp/` 有 BPF 过滤器示例;Chrome/Flatpak/Docker 的 seccomp profile 是生产级参考;`prctl(PR_SET_NO_NEW_PRIVS)` 是装 seccomp 的前置条件(防提权绕过)。

### 引出下一篇

第 2 篇(系统调用)到本章结束。我们讲清了"用户态合法进内核"这条入口的方方面面:`SYSCALL` 指令、参数传递、VDSO 避免进内核、入口上的 seccomp/ftrace 钩子。但内核除了"被用户拉进来",还会**主动**产生事件——第 3 篇讲时钟:时钟硬件周期触发,内核借它驱动调度器时间片、驱动 hrtimer 红黑树、维护墙上时间、让 idle CPU 省电(NOHZ)。时钟是"内核主动向外"这一面的第一个典型。下一章我们从 clocksource/clockevent 这两套硬件抽象讲起,正式进入第 3 篇:时钟与定时器。
