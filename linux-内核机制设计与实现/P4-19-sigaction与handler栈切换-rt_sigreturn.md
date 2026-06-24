# 第十九章 · sigaction 与 handler 栈切换:rt_sigreturn

> 篇:P4 信号
> 主线呼应:上一章(P4-18)讲清楚了——内核只把信号挂进 `pending` 队列(`complete_signal`),真正的 handler 要等到进程**返回用户态前一刻**,在 [`exit_to_user_mode_loop`](../linux/kernel/entry/common.c#L90) 检查 `_TIF_SIGPENDING` 时才被启动。那一章留下了一个大坑:**handler 是用户态的函数,但它要跑在被信号打断的"半截现场"上——进程的寄存器已经压在内核栈里、用户栈还停在被打断的位置、返回地址指向被打断的下一条指令。内核怎么让 handler 跑起来,跑完之后又能一字不差地恢复"被中断的那一瞬间"?** 这就是本章的主线:`sigaction` 注册 → 内核在用户栈上搭一个 `sigframe` 帧 → 改返回地址让 handler "看起来像普通函数被调用" → handler 跑完"返回"时被劫持到 `rt_sigreturn` 系统调用 → 内核从用户栈把保存的上下文读回、回到被打断的现场。这一套"在用户栈上搭戏台、劫持返回地址"的戏法,是 Linux 信号子系统最精巧的一笔,也是读者最容易"看了源码没看懂"的地方。

## 核心问题

**用户进程调了 `sigaction(2)`,把 SIGINT 的 handler 改成自己的函数。当 SIGINT 真的到达,内核已经把信号挂到了进程的 `pending` 队列、在返回用户态前决定要"跑这个 handler"。可是 handler 是一段用户态代码,它需要一个干净的函数调用环境(参数、返回地址、可用的栈空间),而此刻进程正卡在"被中断的某个瞬间",所有寄存器都压在内核栈里。内核怎么让 handler 跑起来,跑完之后还能精确恢复被打断的寄存器和指令位置?如果主栈已经溢出了(handler 没地方放栈),又该怎么办?**

读完本章你会明白:

1. `sigaction` 注册了什么:它把"信号 → handler"映射写进 `sighand->action[]` 数组,handler 函数指针、屏蔽字、标志(`SA_RESTART`/`SA_ONSTACK`/`SA_NODEFER`/`SA_SIGINFO`/`SA_RESTORER` 等)各落到哪。
2. 内核在**用户栈上**构建 `rt_sigframe` 帧(包含 `ucontext` + `siginfo` + 一段 `rt_sigreturn` trampoline),把 handler 的"返回地址"改成 `rt_sigreturn` 的入口——让 handler 像普通函数被调用,却能在"返回"时自动回到内核恢复现场。
3. `SA_ONSTACK` + `sigaltstack` 为什么必要:主栈溢出触发 SIGSEGV,handler 还要有地方可跑——Linux 给每个线程预留一块备用信号栈。
4. `rt_sigreturn` 凭什么"回来":它不是普通系统调用,它复用了 C 函数调用返回的机制,把"恢复上下文"伪装成"函数返回"。

> **逃生阀**:如果你已经懂 `sigaction(2)` 怎么调、知道 `siginfo_t`/`ucontext_t` 这两个结构体长什么样,可以直接跳到 19.3 节(sigframe 在用户栈的布局)和 19.5 节(技巧精解:rt_sigreturn 的返回地址劫持)。但 19.2 的"为什么非要在用户栈搭戏台"建议读,因为这一节回答了整个机制最反直觉的那一问。

---

## 19.1 一句话点破

> **Linux 信号 handler 的"调用"和"返回",是一次精心设计的伪装。内核不发明任何新的调用机制——它把 handler 当成一个普通的 C 函数,在用户栈上搭好"调用一个函数"需要的全部东西(参数、被屏蔽的寄存器现场、返回地址),然后把"返回地址"偷偷换成 `rt_sigreturn` 系统调用的入口。handler 跑完执行 `ret` 指令"返回"时,实际跳进的是 `rt_sigreturn`,内核从用户栈读回保存的上下文,一字不差地恢复被打断的现场。**

这是结论,不是理由。本章倒过来拆:先看 `sigaction` 注册了什么(19.2),再看内核到底在用户栈上搭了什么"戏台"(19.3),然后看 `SA_ONSTACK`/`sigaltstack` 这个备用栈为什么必要(19.4),最后在技巧精解(19.5)里把"`rt_sigreturn` 劫持返回地址"这一招彻底拆透。

---

## 19.2 `sigaction` 注册了什么:把 handler 写进 `action[]`

在讲"信号到达时怎么跑 handler"之前,先把"用户态怎么告诉内核我要换 handler"这一步讲清楚——这一步决定了后面所有戏台的形状。

### 用户态的 `sigaction` 系统调用

用户进程要换某个信号的 handler,调 [`sigaction(2)`](https://man7.org/linux/man-pages/man2/sigaction.2.html) 系统调用,传三个参数:信号号 `sig`、新的 `struct sigaction` 指针 `act`、旧的 `struct sigaction` 指针 `oact`。内核入口是 [`SYSCALL_DEFINE4(rt_sigaction, ...)`](../linux/kernel/signal.c#L4486)(注意名字带 `rt_`,这是 POSIX 实时信号统一入口):

```c
/* kernel/signal.c,简化自 SYSCALL_DEFINE4(rt_sigaction)@L4486 */
SYSCALL_DEFINE4(rt_sigaction, int, sig,
                const struct sigaction __user *, act,
                struct sigaction __user *, oact,
                size_t, sigsetsize)
{
    struct k_sigaction new_sa, old_sa;
    int ret;

    if (sigsetsize != sizeof(sigset_t))      /* 检查 sigset 大小 */
        return -EINVAL;

    if (act && copy_from_user(&new_sa.sa, act, sizeof(new_sa.sa)))
        return -EFAULT;                      /* 用户指针非法,fixup 回 -EFAULT(见 P2-09) */

    ret = do_sigaction(sig, act ? &new_sa : NULL, oact ? &old_sa : NULL);
    if (ret)
        return ret;

    if (oact && copy_to_user(oact, &old_sa.sa, sizeof(old_sa.sa)))
        return -EFAULT;
    return 0;
}
```

注意第一行的 `copy_from_user`/`copy_to_user`——用户态的 `act`/`oact` 指针是 ring 3 的内存,内核必须用 `copy_from_user` 把它安全地拷进内核的 `new_sa`(这个机制见第 9 章 `copy_from_user` 的 page fault fixup)。拷进来之后,真正干活的函数是 [`do_sigaction`](../linux/kernel/signal.c#L4163)。

### `do_sigaction`:把 handler 写进 `sighand->action[sig-1]`

[`do_sigaction`](../linux/kernel/signal.c#L4163) 是核心——它把用户态传来的 `k_sigaction` 写进当前进程的 `sighand->action[sig-1]`:

```c
/* kernel/signal.c,简化自 do_sigaction@L4163 */
int do_sigaction(int sig, struct k_sigaction *act, struct k_sigaction *oact)
{
    struct task_struct *p = current, *t;
    struct k_sigaction *k;
    sigset_t mask;

    if (!valid_signal(sig) || sig < 1 || (act && sig_kernel_only(sig)))
        return -EINVAL;                      /* SIGKILL/SIGSTOP 不能改 handler */

    k = &p->sighand->action[sig-1];          /* 1) 取出当前 handler 槽 */

    spin_lock_irq(&p->sighand->siglock);     /* 2) 抢 sighand->siglock */
    if (k->sa.sa_flags & SA_IMMUTABLE) {     /*    内核钉死的 handler,不许动 */
        spin_unlock_irq(&p->sighand->siglock);
        return -EINVAL;
    }
    if (oact)
        *oact = *k;                          /* 3) 备份旧 handler 给 oact */

    if (act) {
        sigdelsetmask(&act->sa.sa_mask,
                      sigmask(SIGKILL) | sigmask(SIGSTOP)); /* KILL/STOP 永远不被屏蔽 */
        *k = *act;                           /* 4) 把新 handler 写进 action[sig-1] */

        /* POSIX 3.3.1.3: 把 handler 设成 SIG_IGN 的信号,pending 里的要全部清掉 */
        if (sig_handler_ignored(sig_handler(p, sig), sig)) {
            sigemptyset(&mask);
            sigaddset(&mask, sig);
            flush_sigqueue_mask(&mask, &p->signal->shared_pending);
            for_each_thread(p, t)
                flush_sigqueue_mask(&mask, &t->pending);
        }
    }

    spin_unlock_irq(&p->sighand->siglock);
    return 0;
}
```

四步:取槽 → 上锁 → 备份旧的 → 写新的。这里有几个关键的"为什么 sound"点,后面 19.5 会回扣:

**第一,`action[]` 是 per-`sighand_struct` 的数组,不是 per-task。** 见 [`struct sighand_struct`](../linux/include/linux/sched/signal.h#L21-L26):

```c
/* include/linux/sched/signal.h */
struct sighand_struct {
    spinlock_t      siglock;
    refcount_t      count;
    wait_queue_head_t   signalfd_wqh;
    struct k_sigaction  action[_NSIG];   /* _NSIG = 65,索引 0..63 */
};
```

同一个线程组的所有线程共享同一个 `sighand_struct`(通过 `CLONE_SIGHAND`)——所以一个线程 `sigaction` 改了 handler,整组都看得到。`action[sig-1]` 索引到信号 sig 的那个槽,槽里存的是 `struct k_sigaction`(内核态的 `sigaction`,多了 `sa_restorer` 字段)。

**第二,`do_sigaction` 上 `sighand->siglock` 自旋锁**(`spin_lock_irq`)——为什么上锁?因为信号投递路径(`complete_signal` → `dequeue_signal` → `get_signal`)读 `action[sig-1]` 取 handler,而 `do_sigaction` 写它,两边可能在不同 CPU 并发。**`spin_lock_irq` 还顺带关了本核中断**——因为 `do_sigaction` 可能在中断里被 softirq 触发的信号路径打扰,关中断避免本核被打断。这是"读写同一个数组槽"的标准自旋锁保护,和中断上下文不能睡眠的约束一致(第 4 章)。

**第三,SIGKILL 和 SIGSTOP 永远不能被改 handler、不能被屏蔽**——`sig_kernel_only(sig)` 检查 + `sigdelsetmask` 把它们从 `sa_mask` 里抠掉。为什么?因为这两个信号是系统管理员的"最后底牌"(`kill -9`),如果允许进程改 handler 把 SIGKILL 设成"忽略",一个失控进程就杀不死了。这是 POSIX 强制约束,内核在 `do_sigaction` 入口直接 `-EINVAL` 拒绝。

> **不这样会怎样**:如果 `do_sigaction` 不上 `siglock`,两个 CPU 同时跑——一个 `sigaction` 改 SIGINT 的 handler,另一个线程被 SIGINT 触发 `get_signal` 读 handler——读到的可能是半新半旧的 `k_sigaction`(函数指针是新的、`sa_mask` 是旧的),handler 跑起来行为未定义。`siglock` 把"读 handler"和"写 handler"串行化,保证槽的读写是原子的。

> **钉死这件事**:`sigaction(2)` 的全部作用就是:把"信号 sig → handler 函数 + 屏蔽字 + 标志位"这一个 `struct k_sigaction` 写进 `current->sighand->action[sig-1]`。后面所有"信号到达时怎么跑 handler"的戏法,都基于这一步留下的 `action[]` 表。注册完成后,handler 只是"静静地躺在数组里等被读",真正"调起来"发生在下一节。

---

## 19.3 信号到达时:内核在用户栈上搭"sigframe"戏台

上一章 P4-18 讲过,信号真正"跑 handler"发生在 [`exit_to_user_mode_loop`](../linux/kernel/entry/common.c#L90) 检查 `_TIF_SIGPENDING` 时:

```c
/* kernel/entry/common.c,简化自 exit_to_user_mode_loop@L90 */
while (ti_work & EXIT_TO_USER_MODE_WORK) {
    ...
    if (ti_work & (_TIF_SIGPENDING | _TIF_NOTIFY_SIGNAL))
        arch_do_signal_or_restart(regs);     /* <- 这里进信号处理 */
    ...
}
```

`arch_do_signal_or_restart` 是个弱符号([entry/common.c:83](../linux/kernel/entry/common.c#L83)),由架构代码(如 `arch/x86/kernel/signal.c`)实现。它内部走三步:① [`get_signal`](../linux/kernel/signal.c#L2675) 从 `pending` 队列摘一个信号、决定动作;② 如果动作是"跑自定义 handler",就调架构相关的 `handle_signal`;③ `handle_signal` 调 `setup_rt_frame`(或 32 位 ABI 的 `setup_frame`)在用户栈上搭戏台。本章就钻第二步和第三步——`get_signal` 怎么决定跑 handler,以及 `setup_rt_frame` 在用户栈上搭了什么。

### 19.3.1 `get_signal`:决定"跑这个 handler"

[`get_signal`](../linux/kernel/signal.c#L2675) 是个长函数,但对我们这一章来说核心就一段——它从 `pending` 摘出信号后,判断要不要跑自定义 handler:

```c
/* kernel/signal.c,简化自 get_signal@L2675 */
bool get_signal(struct ksignal *ksig)
{
    ...
    for (;;) {
        struct k_sigaction *ka;
        ...
        signr = dequeue_signal(current, &current->blocked, &ksig->info, &type);
        if (!signr)
            break;                           /* 没信号了,返回 0 */

        ...
        ka = &sighand->action[signr-1];      /* 取出这个信号的 handler 槽 */

        if (ka->sa.sa_handler == SIG_IGN)    /* 设成"忽略",扔掉 */
            continue;
        if (ka->sa.sa_handler != SIG_DFL) {  /* 有自定义 handler! */
            /* Run the handler. */
            ksig->ka = *ka;                  /* 把整个 k_sigaction 复制给调用者 */

            if (ka->sa.sa_flags & SA_ONESHOT)/* 老语义 SA_RESETHAND:跑一次后恢复默认 */
                ka->sa.sa_handler = SIG_DFL;

            break;                           /* 跳出循环,返回非零 signr */
        }

        /* 走默认动作(终止/停止/忽略) */
        ...
    }
    ...
    ksig->sig = signr;
    ...
    return signr > 0;                        /* 返回 true 表示"有信号要跑 handler" */
}
```

关键三步:① `dequeue_signal` 从 `pending` 摘一个信号;② 查 `action[signr-1]` 拿 handler;③ 如果 `sa_handler != SIG_DFL`(即用户设了自定义 handler),就把整个 `k_sigaction` 通过 `ksig->ka = *ka` 复制到调用者的 `ksig` 结构里,`break` 出循环返回 `true`。调用者(架构的 `handle_signal`)拿到这个 `ksig`,就知道:要跑 `ksig->ka.sa.sa_handler` 这个函数,信号屏蔽要按 `ksig->ka.sa.sa_mask` 设,标志位是 `ksig->ka.sa.sa_flags`。

> **不这样会怎样**:为什么不让 `get_signal` 直接调 handler?因为 handler 是**用户态代码**,而 `get_signal` 在**内核态**跑——内核态不能直接跳进用户态代码执行(特权级、地址空间、栈都不同)。`get_signal` 只能"准备好调用所需的所有信息",把控制权交回给架构相关的 `handle_signal`/`setup_rt_frame`——后者负责在用户栈上搭好戏台、改返回地址,然后让进程**真的返回用户态**时 CPU 自动跳到 handler。

### 19.3.2 `setup_rt_frame`:在用户栈上搭戏台(架构相关)

`get_signal` 返回后,架构代码调 `handle_signal`(在 `arch/x86/kernel/signal.c`,未 sparse clone,这里描述作用 + 引通用框架),`handle_signal` 再调 `setup_rt_frame`——**这是本章的核心戏法所在**。

`setup_rt_frame` 做的事,可以一句话概括:**它在用户栈上"伪造"一次函数调用,让 handler 看起来像被普通 C 调用一样启动,但"返回地址"被劫持成 `rt_sigreturn` 的入口**。

具体来说,`setup_rt_frame` 在用户栈上压一个叫 `rt_sigframe` 的结构(`rt_` 表示 real-time,POSIX.1b 实时信号统一用这套,老的非实时版本 `sigframe` 已基本不用)。这个结构(简化,非源码原文,因为 arch/x86 未 sparse clone)大致长这样:

```
 用户栈(从高地址往低地址生长),rt_sigframe 布局(简化,arch/x86):

   ↑ 高地址(旧栈顶,被打断时的 sp)
   ┌─────────────────────────────────────────┐
   │ pretcode(返回地址)                      │  ← handler "返回"时跳到这里
   │                                         │     = &__restore_rt 或 &rt_sigreturn
   ├─────────────────────────────────────────┤
   │ struct ucontext uc {                    │  ← 保存被打断的现场
   │   uc_flags                             │
   │   uc_link = NULL                        │
   │   uc_stack (ss_sp/ss_flags/ss_size)    │  ← sigaltstack 信息
   │   uc_mcontext {                         │
   │     r8..r15, rdi, rsi, rbp, rbx,        │  ← 全部通用寄存器(被打断的值)
   │     rdx, rax, rcx, rsp, rip, rflags     │
   │     cs, ss;                             │
   │     fpstate (FPU/XMM 状态,如有)         │
   │   }                                     │
   │   uc_sigmask (handler 期间的屏蔽字)     │
   │ }                                       │
   ├─────────────────────────────────────────┤
   │ struct siginfo info {                   │  ← 信号信息(为什么发的)
   │   si_signo, si_errno, si_code,          │
   │   si_addr / si_pid / si_uid / ...       │
   │ }                                       │
   ├─────────────────────────────────────────┤
   ↓ 低地址(新栈顶,handler 的 sp)
```

三个关键部分:

1. **`siginfo`(信号信息)**:handler 如果是三参数版(用 `SA_SIGINFO` 注册,签名 `void handler(int, siginfo_t *, void *)`),内核把信号编号、来源 pid、触发地址等打包成 `siginfo_t` 写到这里。
2. **`ucontext`(被打断的现场)**:这是戏台的核心。内核把**所有被打断的寄存器**(通用寄存器、指令指针 `rip`、栈指针 `rsp`、标志寄存器 `rflags`、段寄存器 `cs`/`ss`,甚至 FPU/XMM 状态)原封不动写进 `uc_mcontext`。这份"现场快照"是 `rt_sigreturn` 后来恢复一切的依据。
3. **`pretcode`(返回地址)**:handler 是个普通 C 函数,它的开头会建立栈帧、结尾会执行 `ret` 指令"返回"。`ret` 从栈顶弹一个地址跳过去——这个地址就是 `pretcode`。**内核在这里放的不是"调用 handler 的下一条指令",而是 `rt_sigreturn` 系统调用的入口**(具体是一个叫 `__restore_rt` 的小 trampoline,它执行 `mov $__NR_rt_sigreturn, %eax; syscall`)。

搭完戏台,`setup_rt_frame` 还要做三件"寄存器准备"的事:

- **`regs->sp` 改成 sigframe 的新栈顶**(让 handler 用这个栈)。
- **`regs->ip` 改成 handler 函数入口地址**(`ksig->ka.sa.sa_handler`)。
- **设置传参寄存器**(`rdi` = signo,`rsi` = `&siginfo`,`rdx` = `&ucontext`,对应三参数 handler 的 ABI)。

做完这一切,`handle_signal` 返回,`arch_do_signal_or_restart` 返回,`exit_to_user_mode_loop` 退出循环,通用入口框架执行最后那条 `iret`/`sysret`——CPU 切回 ring 3、加载 `regs` 里的 `ip`/`sp`……而此刻 `regs->ip` 已经是 handler 入口!于是进程"返回用户态"的一瞬间,CPU 跳进的就是 handler,**进程本身完全不知道自己被打断过,它只觉得"自己突然在跑 handler"**。

```
 handler 看到的"世界"(被 setup_rt_frame 伪造的):

   ┌─ handler 入口(regs->ip 已改)
   │    push %rbp; mov %rsp,%rbp; ...    ← 像 normal 函数一样建栈帧
   │    [读 rdi=signo, rsi=siginfo*, rdx=ucontext*]
   │    [执行用户代码]
   │    ...
   │    leave; ret                        ← "返回"
   └────────────────────┬────────────────┘
                        │ ret 弹栈顶地址
                        ▼
                 pretcode = &__restore_rt
                        │
                        ▼
              mov $__NR_rt_sigreturn,%eax
              syscall                    ← 触发 rt_sigreturn 系统调用!
                        │
                        ▼
              内核读 sigframe 里的 ucontext
              把所有寄存器恢复成被打断时的值
                        │
                        ▼
              iret/sysret 回用户态 → rip = 被打断的下一条指令
```

这就是整个机制的全貌:**handler 像被"骗"着跑了一趟,跑完"返回"实际是跳进了 `rt_sigreturn`,内核从用户栈把现场读回来,让进程像什么都没发生过一样继续。**

> **不这样会怎样**:这是本章最关键的反面对比。如果内核**不在用户栈上搭这个 sigframe**,而是"自己保存完整用户上下文、维护一个状态机来恢复",会怎样?

- **方案 B**:内核维护一个 per-task 的"被信号打断的现场"队列,handler 跑完时内核从队列里弹出最早的现场、`iret` 回去。
- **撞墙 1(嵌套信号)**:handler 自己也可能被信号打断(信号嵌套),内核要维护一个 LIFO 的现场栈——多一层管理、多一层锁、多一层边界条件。
- **撞墙 2(handler 是用户代码)**:handler 在用户态跑,它可能调 `longjmp` 跳走、可能 `mmap` 改地址空间、可能 `siglongjmp` 跨函数返回——内核的"现场队列"和用户态的实际行为会脱节,内核要不断地"猜"用户态在干嘛。
- **撞墙 3(可移植性)**:不同架构的寄存器集、调用约定、栈布局不同,内核要在每个架构各写一套"现场保存/恢复"代码,复杂度爆炸。

Linux 选的方案(sigframe 放用户栈 + 劫持返回地址)把这一切都消解了:**handler 的"调用"和"返回"完全复用 C 函数调用机制,内核只在两端做最少的事(搭戏台 + 拆戏台),中间 handler 怎么折腾都是用户态的事,内核不掺和**。`longjmp` 跳过 `rt_sigreturn`?那就跳过了,sigframe 留在栈上(或被覆盖),用户自己负责。这是"把状态放在用户能看到的地方、让用户自己管"的设计哲学——和"内存分配器把元数据放进对象 header"(第 8 本)、"调度器把调度信息放进 `task_struct`"(第 11 本)是同一思路。

> **钉死这件事**:`setup_rt_frame` 在用户栈上搭一个 `siginfo` + `ucontext` + `pretcode` 的帧,把 `regs->ip` 改成 handler 入口、`regs->sp` 改成 sigframe 顶部、`pretcode` 改成 `rt_sigreturn` 入口。进程"返回用户态"瞬间跳进 handler,handler 跑完"返回"瞬间跳进 `rt_sigreturn`——两端对称,中间完全复用 C 调用机制。这是 Linux 信号子系统最精巧的设计。

### 19.3.3 `signal_delivered`:跑完戏台后的扫尾

`handle_signal` 调完 `setup_rt_frame` 后,会调 [`signal_setup_done`](../linux/kernel/signal.c#L2954)→ [`signal_delivered`](../linux/kernel/signal.c#L2934) 做扫尾:

```c
/* kernel/signal.c,简化自 signal_delivered@L2934 */
static void signal_delivered(struct ksignal *ksig, int stepping)
{
    sigset_t blocked;

    /* A signal was successfully delivered, and the saved sigmask was stored
       on the signal frame, and will be restored by sigreturn.
       So we can simply clear the restore sigmask flag. */
    clear_restore_sigmask();

    sigorsets(&blocked, &current->blocked, &ksig->ka.sa.sa_mask);  /* 屏蔽 sa_mask */
    if (!(ksig->ka.sa.sa_flags & SA_NODEFER))
        sigaddset(&blocked, ksig->sig);                            /* 默认还屏蔽自己 */
    set_current_blocked(&blocked);                                 /* 更新 current->blocked */
    if (current->sas_ss_flags & SS_AUTODISARM)
        sas_ss_reset(current);                                     /* SS_AUTODISARM:用完复位备用栈 */
    if (stepping)
        ptrace_notify(SIGTRAP, 0);
}
```

两件事:① 把 `ksig->ka.sa.sa_mask`(handler 期间要屏蔽的信号集)合并进 `current->blocked`,默认还把当前信号自己也加进去(`SA_NODEFER` 可关掉这个默认行为,但通常不推荐——会让自己递归触发);② 如果备用栈设了 `SS_AUTODISARM` 标志,handler 跑起来时把备用栈标记清掉(避免 handler 里再被信号时重复用同一块备用栈)。这两件事保证 handler 跑的时候不会被自己关心的信号再打断。

---

## 19.4 `SA_ONSTACK` 与 `sigaltstack`:主栈溢出时,handler 还要有地方跑

到这里戏台机制已经清楚了,但有一个反直觉的边界情况:**如果信号就是因为栈溢出触发的(handler 还能跑在哪?)**。考虑这段用户代码:

```c
/* 故意递归爆栈(简化示意,非源码) */
void boom(void) {
    char buf[4096];
    boom();   /* 无限递归 */
}

void segv_handler(int sig, siginfo_t *si, void *uc) {
    /* 栈已经溢出,这里还能跑吗?sp 还指向爆掉的栈区! */
    write(2, "stack overflow\n", 15);
    _exit(1);
}

int main(void) {
    struct sigaction sa = { .sa_sigaction = segv_handler, .sa_flags = SA_SIGINFO };
    sigaction(SIGSEGV, &sa, NULL);
    boom();
}
```

`boom()` 递归到栈底,内核下一次访问栈页触发缺页,发现栈不能扩展了(`MAP_GROWSDOWN` 边界检查失败),投 SIGSEGV。这时进程的 `sp` 已经在爆掉的栈区,**如果 `setup_rt_frame` 还在主栈上搭 sigframe,sigframe 会写到无效内存、再触发 SIGSEGV,陷入死循环**。

这就是 `SA_ONSTACK` + `sigaltstack` 的存在理由。

### `sigaltstack`:预留一块备用信号栈

用户进程可以调 [`sigaltstack(2)`](https://man7.org/linux/man-pages/man2/sigaltstack.2.html) 系统统调用,预先分配一块内存当"备用信号栈",注册时给 handler 加 `SA_ONSTACK` 标志。内核入口是 [`SYSCALL_DEFINE2(sigaltstack, ...)`](../linux/kernel/signal.c#L4303),真正干活的是 [`do_sigaltstack`](../linux/kernel/signal.c#L4246):

```c
/* kernel/signal.c,简化自 do_sigaltstack@L4246 */
do_sigaltstack(const stack_t *ss, stack_t *oss, unsigned long sp, size_t min_ss_size)
{
    struct task_struct *t = current;
    int ret = 0;

    if (oss) {                                      /* 输出旧的备用栈信息 */
        oss->ss_sp    = (void __user *) t->sas_ss_sp;
        oss->ss_size  = t->sas_ss_size;
        oss->ss_flags = sas_ss_flags(sp) | (current->sas_ss_flags & SS_FLAG_BITS);
    }

    if (ss) {
        void __user *ss_sp = ss->ss_sp;
        size_t ss_size = ss->ss_size;
        unsigned ss_flags = ss->ss_flags;

        if (unlikely(on_sig_stack(sp)))             /* 已经在备用栈上,不许再设 */
            return -EPERM;

        ...
        sigaltstack_lock();
        if (ss_mode == SS_DISABLE) {                /* 禁用备用栈 */
            ss_size = 0;
            ss_sp = NULL;
        } else {
            if (unlikely(ss_size < min_ss_size))    /* 太小,MINSIGSTKSZ */
                ret = -ENOMEM;
            ...
        }
        if (!ret) {
            t->sas_ss_sp    = (unsigned long) ss_sp;   /* 写进 task_struct */
            t->sas_ss_size  = ss_size;
            t->sas_ss_flags = ss_flags;
        }
        sigaltstack_unlock();
    }
    return ret;
}
```

`sigaltstack` 把备用栈的地址(`sas_ss_sp`)、大小(`sas_ss_size`)、标志(`sas_ss_flags`)记到当前线程的 `task_struct` 里(这三个字段在 [`struct task_struct`](../linux/include/linux/sched.h#L1120-L1122),**per-task 而非 per-sighand**——每个线程可以有自己的备用栈):

```c
/* include/linux/sched.h */
struct task_struct {
    ...
    unsigned long           sas_ss_sp;      /* 备用信号栈地址 */
    size_t                  sas_ss_size;    /* 大小 */
    unsigned int            sas_ss_flags;   /* SS_ONSTACK/SS_DISABLE/SS_AUTODISARM */
    ...
};
```

注意 [`on_sig_stack(sp)`](../linux/include/linux/sched/signal.h#L585) 的检查——**如果当前 `sp` 已经在备用栈上(说明 handler 已经在备用栈跑、又被信号打断),就不许再设备用栈**(`-EPERM`)。为什么?因为备用栈正在用,改它会把正在跑的 handler 的栈搞乱。这是"备用栈同一时刻只能被一层 handler 用"的约束。

### `sigsp`:信号到达时,要不要切到备用栈?

信号到达、`setup_rt_frame` 要搭戏台时,**先决定用主栈还是备用栈**——这个决定由 [`sigsp`](../linux/include/linux/sched/signal.h#L617-L626) 做:

```c
/* include/linux/sched/signal.h */
static inline unsigned long sigsp(unsigned long sp, struct ksignal *ksig)
{
    if (unlikely((ksig->ka.sa.sa_flags & SA_ONSTACK)) && !sas_ss_flags(sp))
#ifdef CONFIG_STACK_GROWSUP
        return current->sas_ss_sp;
#else
        return current->sas_ss_sp + current->sas_ss_size;   /* 备用栈栈顶 */
#endif
    return sp;                                               /* 用主栈当前 sp */
}
```

逻辑就一句:**handler 注册时带了 `SA_ONSTACK`,并且当前 `sp` 不在备用栈上(`sas_ss_flags(sp)` 返回 0 而非 `SS_ONSTACK`),就把 sigframe 搭到备用栈顶**。`sas_ss_flags(sp)` 内部用 [`on_sig_stack`](../linux/include/linux/sched/signal.h#L571) 判断 `sp` 是不是落在 `[sas_ss_sp, sas_ss_sp + sas_ss_size)` 区间:

```c
/* include/linux/sched/signal.h,简化自 __on_sig_stack@L571 */
static inline int __on_sig_stack(unsigned long sp)
{
#ifdef CONFIG_STACK_GROWSUP
    return sp >= current->sas_ss_sp &&
           sp - current->sas_ss_sp < current->sas_ss_size;
#else
    return sp > current->sas_ss_sp &&
           sp - current->sas_ss_sp <= current->sas_ss_size;
#endif
}
```

这样就实现了"主栈溢出时,handler 自动切到备用栈"——主栈的 `sp` 远离备用栈区间,`sas_ss_flags(sp)` 返回 0,`sigsp` 返回备用栈顶,`setup_rt_frame` 在备用栈上搭 sigframe,handler 就在备用栈上跑。**这就是为什么前面的 `boom()` 例子,只要 `sigaction` 加了 `SA_ONSTACK` + 先调过 `sigaltstack`,`segv_handler` 就有干净的栈可用**。

> **不这样会怎样**:如果没有 `sigaltstack` 机制,栈溢出触发的 SIGSEGV handler 必须在已经爆掉的栈上跑——`setup_rt_frame` 写 sigframe 会再触发缺页、再投 SIGSEGV,handler 永远跑不起来,进程直接被默认动作(`do_group_exit`)杀掉。**`SA_ONSTACK` 是"用户态从栈溢出里恢复"的唯一救命稻草**,这是为什么 glibc 的 `signal(7)` 文档强调"处理栈溢出的 handler 必须用 `SA_ONSTACK`"。

> **钉死这件事**:`sigaltstack` 在 `task_struct` 里记三个字段(`sas_ss_sp/size/flags`),`SA_ONSTACK` 是 `sigaction` 的标志位。信号到达时 `sigsp` 判断:`SA_ONSTACK` 且当前不在备用栈上 → 切到备用栈。这是 per-task 的备用栈,每个线程一份。

---

## 19.5 技巧精解:`rt_sigreturn` 的返回地址劫持

这一章最值得拆透的就是"劫持返回地址"这一招。它看起来朴素,却解决了三个本来要写一大堆代码的问题:**① 怎么让 handler 跑起来;② 怎么让 handler 跑完自动回内核;③ 怎么恢复被打断的现场**。Linux 用一个"伪装的 C 函数调用"把三件事一次性解决,这就是它妙的地方。

### 技巧一:把"恢复现场"伪装成"函数返回"

先看"为什么不用别的方案"。一种朴素设计是:**给信号 handler 发明一种新的"返回机制"**——比如用户态 handler 跑完显式调一个 `signal_done()` 库函数,`signal_done()` 内部走系统调用回内核。听起来直观,撞三堵墙:

- **墙 1(用户必须配合)**:用户写的 handler 必须记得调 `signal_done()`,忘了调就回不去(进程"卡"在 handler 里)。可 handler 是用户代码,内核没法强制它调。
- **墙 2(ABI 污染)**:这要求信号 handler 的调用约定和普通函数不同(普通函数 `ret` 返回,信号 handler 必须调 `signal_done`),破坏 C ABI 一致性。
- **墙 3(`longjmp` 等控制流)**:如果 handler 用 `siglongjmp` 跳出(跨函数返回),`signal_done` 永远不会被调,内核的现场栈永远不清理。

Linux 的方案彻底绕开这三堵墙:**不发明新机制,复用 C 函数调用返回**。handler 就是个普通 C 函数,开头 `push %rbp; mov %rsp,%rbp`、结尾 `leave; ret`——`ret` 从栈顶弹一个 8 字节地址跳过去。`setup_rt_frame` 唯一的"手脚"是把栈顶那个返回地址换成 `__restore_rt`:

```
 handler 的栈帧(运行时,简化):

   ↑
   │ ... handler 用的局部变量 ...           │
   ├──────────────────────────────────────┤
   │ 保存的 rbp(handler 开头 push 的)     │
   ├──────────────────────────────────────┤  ← handler 的 sp 指这里附近
   │ pretcode = &__restore_rt             │  ← setup_rt_frame 写的"返回地址"
   ├──────────────────────────────────────┤
   │ ucontext(被打断的现场)              │
   │ siginfo                              │
   ↓
```

handler 结尾 `leave; ret`:① `leave` 等价于 `mov %rbp,%rsp; pop %rbp`(恢复调用者的栈帧寄存器);② `ret` 弹栈顶 8 字节(`__restore_rt`)进 `rip`。CPU 跳到 `__restore_rt`——这是一段内核预先放在用户态可执行映像里的 trampoline(vsyscall page 或 vdso 里),汇编就两条指令:

```asm
__restore_rt:                    /* arch/x86/ 下,未 sparse clone,示意 */
    movq $__NR_rt_sigreturn, %rax    /* 系统调用号 15 进 rax */
    syscall                          /* 触发 rt_sigreturn 系统调用 */
```

`syscall` 把控制权交回内核,内核的 `rt_sigreturn` 处理函数(架构相关,在 `arch/x86/kernel/signal.c`)从用户栈读回 `ucontext`,把里面的寄存器值灌进内核的 `pt_regs`,然后返回——这次通用入口框架的 `iret`/`sysret` 加载的就是**被打断时的寄存器值**,CPU 回到被打断的那条指令,进程像什么都没发生过。

这一招妙在:**整个"恢复现场"的过程,从用户态看就是个普通的 `ret`,内核不需要任何"用户态配合"的协议**——用户忘了写 handler 的返回语句?C 编译器自动生成 `ret`;handler `longjmp` 跳走?那就跳走,sigframe 留在用户栈上用户自己管(POSIX 提供 `sigsetjmp`/`siglongjmp` 配套地处理屏蔽字恢复);handler 改了 `ucontext`?那 `rt_sigreturn` 就用改过的值恢复(这是调试器/`setcontext` 利用的合法能力)。**内核把现场的所有权完全交给用户态,自己只负责"按用户栈上写的值恢复"**,这是一种极简但极鲁棒的设计。

> **反面对比**:如果用"内核维护现场队列"方案(19.3.2 的方案 B),嵌套信号、`longjmp`、handler 改地址空间,每一种边界情况都要内核各写一段处理代码——内核和用户态会反复"猜对方在干嘛"。`rt_sigreturn` + 用户栈 sigframe 把这一切消解:**现场在用户栈上,谁改的谁负责,内核只是个忠实的"按值恢复器"**。

### 技巧二:`SA_RESTORER` —— 让 libc 而不是内核提供 trampoline

`__restore_rt` 这段两条指令的 trampoline 放哪?有两个选择:① 内核在用户态映像里固定一块地址(vsyscall page,老机制);② 用户态的 libc 自己提供(`SA_RESTORER` 标志)。现代 glibc 默认走第二条——`sigaction` 库函数在填 `struct sigaction` 时,会自动把 `sa_flags |= SA_RESTORER`、`sa_restorer = __restore_rt`(libc 内部的一段汇编)。`do_sigaction` 把这个 `sa_restorer` 指针也存进 `action[]`,`setup_rt_frame` 搭戏台时 `pretcode` 用 `ksig->ka.sa.sa_restorer` 而不是内核提供的固定地址。

为什么这么设计?**避免 vsyscall page 这个固定地址被攻击者利用**(返回导向编程 ROP 攻击常用固定地址的 gadget)。让 libc 自己选 trampoline 地址(ASLR 后地址随机),攻击者没法预测 `__restore_rt` 在哪,ROP 链接不上。这是一个安全增强的小细节,但体现 Linux 在"返回地址"这种关键控制流上的谨慎。

> **为什么这套设计 sound**:`rt_sigreturn` 的全部魔法就是"栈顶返回地址被换"。它 sound 在:① **复用 C ABI**,不污染调用约定,任何 C 编译器生成的 handler 都自动兼容;② **现场在用户栈**,内核无状态,嵌套/`longjmp`/handler 改现场都不破坏内核一致性;③ **`SA_RESTORER` 让 trampoline 地址可由 libc 随机化**,堵 ROP 攻击面;④ **`rt_sigreturn` 是个 syscall**,内核从用户栈读 `ucontext` 时走标准 `copy_from_user` 路径,用户伪造的 `ucontext` 指针非法会 fixup 回 `-EFAULT` 而不是 panic。这四点合起来,让一个看似"hack"的机制既灵活又安全。

---

## 19.6 一次完整旅程:从 SIGINT 到 handler 跑完回现场

把前面所有片段串起来,看一次 SIGINT 从"内核决定跑 handler"到"进程回到被打断现场"的完整时序:

```mermaid
sequenceDiagram
    participant U as 用户进程<br/>(跑在某条指令上)
    participant K as 内核<br/>(exit_to_user_mode_loop)
    participant S as setup_rt_frame<br/>(arch/x86)
    participant H as 用户 handler

    Note over U: 进程此刻 sp=主栈某处,<br/>rip=被打断的指令
    U->>K: 信号已 pending(别的进程 kill -INT)
    Note over K: exit_to_user_mode_loop 检查<br/>_TIF_SIGPENDING=1
    K->>K: arch_do_signal_or_restart(regs)
    K->>K: get_signal(ksig)<br/>dequeue SIGINT<br/>读 action[SIGINT-1]<br/>有自定义 handler<br/>ksig->ka=*ka, return true
    K->>S: handle_signal(ksig, regs)
    S->>S: sigsp(sp, ksig)<br/>SA_ONSTACK? 切备用栈 : 用主栈
    S->>S: 在用户栈压 sigframe:<br/>siginfo + ucontext(全部寄存器)<br/>+ pretcode=&__restore_rt
    S->>S: 改 regs:<br/>ip=handler 入口<br/>sp=sigframe 栈顶<br/>rdi=signo,rsi=&siginfo,rdx=&ucontext
    S-->>K: 返回(戏台搭好)
    K->>K: signal_delivered<br/>合并 sa_mask 进 current->blocked
    K-->>U: iret/sysret 切回 ring 3<br/>加载 regs(ip=handler)
    Note over U: CPU 跳进 handler,<br/>进程"以为"自己在跑 handler
    U->>H: handler 执行用户代码<br/>(读 rdi/rsi/rdx 拿参数)
    H->>H: ...处理...
    H->>U: leave; ret
    Note over U: ret 弹栈顶 = __restore_rt
    U->>U: 跳到 __restore_rt<br/>mov $__NR_rt_sigreturn,%rax<br/>syscall
    U->>K: rt_sigreturn 系统调用
    K->>K: 从用户栈读 ucontext<br/>灌进 pt_regs(全部寄存器)
    K-->>U: iret/sysret<br/>加载被打断的 rip/sp/...
    Note over U: 进程回到被打断的现场<br/>像什么都没发生过
```

整条链路两次"跨越边界":① `iret/sysret` 切回用户态瞬间跳进 handler(进程被动进 handler);② handler 的 `ret` 被劫持成 `syscall` 主动回内核(进程主动进内核读现场)。**两次跨越都复用了既有的机制——第一次复用通用入口的"返回用户态",第二次复用 `syscall` 系统调用**。Linux 没有为信号 handler 发明任何新的 CPU 控制流机制,这是这套设计最经济的体现。

---

## 章末小结

这一章把信号 handler 的"调用"和"返回"彻底拆透了。核心就一句:**Linux 不发明新机制,它把 handler 伪装成普通 C 函数,在用户栈上搭 sigframe、把返回地址劫持成 `rt_sigreturn`,让 C 函数调用返回的 `ret` 指令自动触发"回内核恢复现场"**。

回到全书二分法:**这一章服务"内核主动驱动/通知"那一面**——信号是内核(或别的进程)主动发给目标进程的通知,handler 是这次通知的"收件人"。但有趣的是,handler 的执行不在内核(它在用户态跑),内核只负责"搭好戏台让用户态跑起来"和"等用户态演完回来拆戏台"——这是一种"内核主导设计、用户态主导执行"的边界跨越。和上一章 P4-18 的"信号延迟到返回用户态才处理"是一脉相承的:内核始终只在"返回用户态那一刻"和用户态交接信号这件事,中间过程完全交给用户态自己。

本章的五样东西:

1. **`do_sigaction` 把 handler 写进 `sighand->action[sig-1]`**,带 `siglock` 保护、`SIGKILL`/`SIGSTOP` 不可改。
2. **`get_signal` 决定跑 handler**:`dequeue_signal` 摘信号 → 查 `action[]` → `ksig->ka = *ka` 传给架构层。
3. **`setup_rt_frame` 在用户栈搭 sigframe**:`siginfo` + `ucontext`(全部寄存器)+ `pretcode`(`__restore_rt`),改 `regs->ip/sp/参数寄存器`。
4. **`SA_ONSTACK` + `sigaltstack`**:`sigsp` 判断切备用栈,救栈溢出场景的 handler。
5. **`rt_sigreturn` 劫持返回地址**:handler 的 `ret` 跳进 `__restore_rt`→`syscall`,内核从用户栈读 `ucontext` 恢复现场。

### 五个"为什么"清单

1. **为什么 `sigaction` 注册 handler 不是写进 `task_struct`,而是写进 `sighand_struct`?** 因为同一个线程组的线程共享 `sighand`(`CLONE_SIGHAND`),一个线程改 handler 整组都生效——信号是 per-process 语义(进程组级 handler),不是 per-thread。`action[_NSIG]` 数组用信号号索引,槽里存 `k_sigaction`。
2. **为什么 handler 跑在用户栈,不是内核栈?** 因为 handler 是用户代码(用户写的函数),它要访问用户数据、用户栈;内核栈在 ring 0,用户代码不能直接跑。更重要的是——handler 跑在用户栈上,它崩了不会污染内核栈,内核的稳定性不受用户 handler 影响。
3. **为什么 `rt_sigreturn` 不直接是个普通系统调用入口,而要用"劫持返回地址"的 trampoline?** 因为这样 handler 完全复用 C 函数调用约定,不需要用户记得调"特殊的返回函数",也不污染 ABI。`longjmp`、handler 改地址空间等边界情况,因为"现场在用户栈、内核无状态",都不会破坏内核一致性。
4. **`SA_ONSTACK` 为什么必要?** 栈溢出触发 SIGSEGV 时,主栈已经爆了——`setup_rt_frame` 在主栈搭 sigframe 会再触发 SIGSEGV,handler 永远跑不起来。`SA_ONSTACK` + `sigaltstack` 预留一块备用栈,`sigsp` 判断切到备用栈,handler 才有干净栈可用。
5. **`sas_ss_sp/size/flags` 为什么是 per-task 而不是 per-sighand?** 因为每个线程有自己的栈,备用栈必须跟着线程走(per-task),不能线程组共享一块(多线程同时被信号时会撞栈)。`siglock` 保护的是 `action[]`(per-sighand),备用栈字段在 `task_struct`(per-task),两者粒度不同,各管各的。

### 想继续深入往哪钻

- **源码**:[`kernel/signal.c`](../linux/kernel/signal.c) 的 [`do_sigaction`](../linux/kernel/signal.c#L4163)(注册 handler)、[`get_signal`](../linux/kernel/signal.c#L2675)(决定跑 handler)、[`signal_delivered`](../linux/kernel/signal.c#L2934)(扫尾屏蔽字)、[`do_sigaltstack`](../linux/kernel/signal.c#L4246)(设备用栈);[`kernel/entry/common.c`](../linux/kernel/entry/common.c) 的 [`exit_to_user_mode_loop`](../linux/kernel/entry/common.c#L90)(检查 `_TIF_SIGPENDING`);[`include/linux/sched/signal.h`](../linux/include/linux/sched/signal.h) 的 [`struct sighand_struct`](../linux/include/linux/sched/signal.h#L21-L26);[`include/linux/sched/signal.h`](../linux/include/linux/sched/signal.h#L571-L626) 的 `__on_sig_stack`/`on_sig_stack`/`sas_ss_flags`/`sigsp`(备用栈判定);[`include/linux/sched.h`](../linux/include/linux/sched.h#L1120-L1122) 的 `sas_ss_sp/size/flags` 字段。
- **arch/x86(未 sparse clone,只描述作用)**:`arch/x86/kernel/signal.c` 的 `handle_signal`/`setup_rt_frame`/`__restore_rt`/`rt_sigreturn` 处理函数,这是真正"搭戏台/拆戏台"的代码;`arch/x86/entry/vdso/vdso-note.S` 里的 `__kernel_rt_sigreturn` trampoline(现代 vdso 提供的 `rt_sigreturn` 入口,替代老的 vsyscall page)。
- **观测**:用 `strace -e rt_sigaction,rt_sigreturn,sigaltstack ./your_prog` 看你的程序注册了哪些 handler、handler 跑完时有没有 `rt_sigreturn` 系统调用(每个 handler 跑完应该看到一次);用 `cat /proc/<pid>/status | grep -E 'Sig(Cgt|Ign|Pnd|Shd)'` 看进程注册/屏蔽的信号位图;用 `gdb` 在 handler 入口下断点,`info registers` 看 `rdi/rsi/rdx`(signo/siginfo*/ucontext*)、`x/8gx $rsp` 看用户栈上的 sigframe。
- **延伸**:读 glibc 的 `sysdeps/unix/sysv/linux/x86_64/libc_sigaction.c`(看 glibc 怎么自动塞 `SA_RESTORER` + `sa_restorer = __restore_rt`);读 POSIX 的 `sigaction(2)`/`sigaltstack(2)`/`sigsetjmp(3)` manpage,理解 `siglongjmp` 跨 handler 返回时怎么和 `rt_sigreturn` 配合(它跳过 `rt_sigreturn`、直接用 `sigsetjmp` 保存的 `mcontext` 恢复,屏蔽字单独走 `sigprocmask`)。

### 引出下一章

到这里,信号的"投递→处理→handler 调用→返回恢复"四步已经全部讲完(P4-17 投递、P4-18 处理入口、P4-19 handler 调用与返回)。但还有一个我们一直绕开的问题没回答:**信号和 CPU 异常是什么关系?** 缺页、除零、非法指令这些 CPU 同步事件,最后很多都变成了 SIGSEGV/SIGFPE/SIGILL 投给进程——它们走的也是 `complete_signal` → `get_signal` → `setup_rt_frame` 这条路吗?异常和外部中断又有什么本质区别(异常同步、中断异步)?下一章(P4-20)把这些串起来:CPU 异常是不可恢复时统一走 `force_sig` 投递信号,让用户态用同一套信号 handler 处理硬件错误——这是信号机制"兼任"异常通知通道的设计,也是"进内核"(异常)和"内核主动"(信号)二分法的桥梁。
