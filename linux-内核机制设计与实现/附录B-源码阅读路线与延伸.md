# 附录 B · 源码阅读路线与延伸

> 篇:附录
> 主线呼应:正文二十一章把"中断、系统调用、时钟、信号"四个机制的**为什么 + 怎么做**讲透了,本附录不重复讲解,只给四份清单:① **源码阅读地图**——按本书篇章顺序,列每篇对应的核心 `.c`/`.h` + "先读哪个函数、再读哪个"的路线;② **观测手段**——`/proc/interrupts`、`/proc/softirqs`、`/proc/timer_list`、`/proc/<pid>/{status,timers}`、`perf`/`ftrace`/`strace`/`bpftrace`,每条命令给一句"看什么";③ **关键调参**——`CONFIG_HZ`、`isolcpus`、`nohz_full`、`irqaffinity`、RT preempt,各讲何时用、改了什么;④ **与其他 OS 对照**——BSD 的中断、Windows 的 IRQL、其他 OS 的系统调用/时钟/信号机制简比。
>
> 本附录是**参考性索引**,不是教程。读者读完正文想自己钻进源码、想观测内核运行、想调参、想横向对照时,把它当"下一步指南"。所有源码路径用 `ls`/Grep 核实过存在于本地 Linux 6.9 源码 `kernel/irq/`、`kernel/softirq.c`、`kernel/workqueue.c`、`kernel/entry/common.c`、`kernel/time/`、`kernel/signal.c`、`include/linux/`。

---

## B.1 源码阅读地图

按本书篇章顺序,列出每篇对应的核心源码文件 + 建议阅读顺序。表中行号均经 Grep 核实(以 Linux 6.9 为准);`arch/x86/` 未 sparse clone 的部分只给作用不给行号。

### B.1.1 第 1 篇 · 中断与软中断(把控制权拉进内核)

这是全书最大的一篇,源码主要在 [`kernel/irq/`](../linux/kernel/irq)、[`kernel/softirq.c`](../linux/kernel/softirq.c)、[`kernel/workqueue.c`](../linux/kernel/workqueue.c)、[`include/linux/interrupt.h`](../linux/include/linux/interrupt.h)、[`include/linux/irq.h`](../linux/include/linux/irq.h)。

| 章 | 主题 | 核心源码文件 | 阅读顺序提示 |
|----|------|------------|------------|
| P1-02 | 硬件中断与中断向量 | [`kernel/irq/handle.c`](../linux/kernel/irq/handle.c)(`set_handle_irq`)、`arch/x86/entry/entry_64.S` 体系结构入口(未 sparse clone,只描述作用) | 先理解 IDT 表 + CPU 自动保存现场(硬件做的工作,内核代码看不到);再看 `set_handle_irq` 怎么注册体系结构无关的入口 `handle_arch_irq` |
| P1-03 | IRQ domain 与 irq_chip | [`kernel/irq/irqdomain.c`](../linux/kernel/irq/irqdomain.c)、[`kernel/irq/chip.c`](../linux/kernel/irq/chip.c)、[`include/linux/irq.h`](../linux/include/linux/irq.h) | 先读 [`struct irq_data`](../linux/include/linux/irq.h#L179)(L179)、[`struct irq_chip`](../linux/include/linux/irq.h#L501)(L501)、[`struct irq_common_data`](../linux/include/linux/irq.h#L147)(L147),理解三层抽象;再读 `irq_domain_create`/`irq_domain_associate_many` 看 hwirq → Linux IRQ 映射 |
| P1-04 | 中断上下文 | [`kernel/softirq.c`](../linux/kernel/softirq.c)([`irq_enter_rcu`](../linux/kernel/softirq.c#L594) L594、[`__irq_exit_rcu`](../linux/kernel/softirq.c#L627) L627、[`irq_exit`](../linux/kernel/softirq.c#L659) L659)、[`include/linux/preempt.h`](../linux/include/linux/preempt.h) | 先读 preempt.h 的 bit 段布局(PREEMPT_MASK/SOFTIRQ_MASK/HARDIRQ_MASK/NMI_MASK,L27~L53),理解 `preempt_count` 嵌套计数;再看 `irq_enter_rcu`/`__irq_exit_rcu` 怎么 add/sub 各 OFFSET、`in_interrupt()` 怎么查位段(L141~L143) |
| P1-05 | 上半部与下半部 | [`include/linux/interrupt.h`](../linux/include/linux/interrupt.h)、[`kernel/irq/manage.c`](../linux/kernel/irq/manage.c)([`request_threaded_irq`](../linux/kernel/irq/manage.c#L2147) L2147) | 先读 `request_threaded_irq` 看怎么注册 hardirq handler + 可选 threaded irq;再读 interrupt.h 的 `IRQF_*` 标志和 `tasklet`/`workqueue` 声明,理解三种下半部切分 |
| P1-06 | softirq 软中断 | [`kernel/softirq.c`](../linux/kernel/softirq.c) | **核心是 `handle_softirqs`**:先读 [`softirq_vec[]`](../linux/kernel/softirq.c#L60)(L60,全局 action 表)→ [`open_softirq`](../linux/kernel/softirq.c#L703)(L703,注册)→ [`raise_softirq`](../linux/kernel/softirq.c#L687)/[`raise_softirq_irqoff`](../linux/kernel/softirq.c#L670)(标记 pending 位)→ [`handle_softirqs`](../linux/kernel/softirq.c#L511)(L511,6.9 主体逻辑在此:pending 位图 + `ffs` + `MAX_SOFTIRQ_RESTART`/`MAX_SOFTIRQ_TIME` 防饿死 + `wakeup_softirqd` 兜底)→ [`__do_softirq`](../linux/kernel/softirq.c#L586)(L586,只是一行 `handle_softirqs(false)` 包装)→ [`invoke_softirq`](../linux/kernel/softirq.c#L419)(L419,`__irq_exit_rcu` 里调用) |
| P1-07 | workqueue | [`kernel/workqueue.c`](../linux/kernel/workqueue.c) | 先读 [`__queue_work`](../linux/kernel/workqueue.c#L2313)(L2313,挑 pool 入队)→ [`process_one_work`](../linux/kernel/workqueue.c#L3166)(L3166,worker 执行单个 work)→ [`worker_thread`](../linux/kernel/workqueue.c#L3374)(L3374,worker 内核线程主循环);理解 CMWQ 的并发度管制(`manage_workers`/`create_worker`) |

**中断事件处理的主链**(顺着读一遍能串起上半部到下半部):

```
硬件中断 ──► arch entry (entry_64.S) ──► handle_arch_irq
        ──► irq_desc 的 high level handler (handle_edge_irq / handle_fasteoi_irq,见 kernel/irq/chip.c)
        ──► handle_irq_event ──► __handle_irq_event_percpu (kernel/irq/handle.c L139)
        ──► 各驱动注册的 handler (上半部,快收快放,raise_softirq)
        ──► irq_exit (kernel/softirq.c L659) ──► __irq_exit_rcu (L627) ──► invoke_softirq (L419)
        ──► __do_softirq (L586) ──► handle_softirqs (L511,处理 pending 位图)
        ──► softirq_vec[N].action (下半部,如 net_rx_action 推包给协议栈)
```

### B.1.2 第 2 篇 · 系统调用(用户态合法进内核)

源码主要在 [`kernel/entry/common.c`](../linux/kernel/entry/common.c)(通用入口框架)、[`kernel/sys.c`](../linux/kernel/sys.c)(系统调用实现集合)、`arch/x86/entry/`(`entry_64.S`/`syscall_64.c`,未 sparse clone)。

| 章 | 主题 | 核心源码文件 | 阅读顺序提示 |
|----|------|------------|------------|
| P2-08 | SYSCALL 指令与 sys_call_table | `arch/x86/entry/entry_64.S`(`SYSCALL` 入口、`do_syscall_64`,未 sparse clone,描述 MSR 直跳)、`arch/x86/entry/syscall_64.c`(`sys_call_table[]`,未 sparse clone)、[`kernel/entry/common.c`](../linux/kernel/entry/common.c) | 先理解 `SYSCALL` 指令 CPU 做的事(MSR `LSTAR` 直跳、不查 IDT、不压完整 trap frame);再读 [`syscall_enter_from_user_mode_prepare`](../linux/kernel/entry/common.c#L74)(L74,通用入口准备)和 [`syscall_exit_to_user_mode`](../linux/kernel/entry/common.c#L215)(L215,出口) |
| P2-09 | 参数传递与返回 | [`kernel/sys.c`](../linux/kernel/sys.c)(`SYSCALL_DEFINE*` 大量示例)、`arch/x86/lib/copy_user_*`(未 sparse clone)、`arch/x86/mm/extable.c`(fixup 表) | 先读 `kernel/sys.c` 里的 `SYSCALL_DEFINE3` 等宏展开(签名变成 `__x64_sys_xxx`),理解寄存器约定(rdi/rsi/rdx/r10/r8/r9);再看 `copy_from_user`/`copy_to_user` 的 fixup 机制(extable 把非法指针的 fault 路径回到 `-EFAULT`) |
| P2-10 | VDSO | `arch/x86/entry/vdso/`(未 sparse clone)、[`kernel/time/timekeeping.c`](../linux/kernel/time/timekeeping.c)、[`kernel/time/vsyscall.c`](../linux/kernel/time/vsyscall.c) | 先理解 VDSO 是内核映射的共享页 + 用户态函数;再看 timekeeping 怎么把墙上时间写到共享页、seqlock 怎么保证读到一致(奇数版本号=正在写) |
| P2-11 | seccomp 与 ftrace | [`kernel/seccomp.c`](../linux/kernel/seccomp.c)、[`kernel/trace/`](../linux/kernel/trace) | 先读 seccomp 怎么在系统调用入口前用 BPF 过滤;再看 ftrace 的 `trace_events`(`raw_syscalls:sys_enter`/`sys_exit`、`syscalls:sys_enter_*`)怎么抓系统调用 |

> **common.c 的关键脉络**:系统调用进出的全部"通用框架"都在 [`kernel/entry/common.c`](../linux/kernel/entry/common.c)。顺着读:入口 [`syscall_enter_from_user_mode_prepare`](../linux/kernel/entry/common.c#L74)(L74)→ 出口准备 [`syscall_exit_to_user_mode_prepare`](../linux/kernel/entry/common.c#L180)(L180)→ 出口循环 [`exit_to_user_mode_loop`](../linux/kernel/entry/common.c#L90)(L90,检查 `_TIF_SIGPENDING`/`_TIF_NOTIFY_RESUME`/`_TIF_NEED_RESCHED`)→ [`arch_do_signal_or_restart`](../linux/kernel/entry/common.c#L83)(L83,弱符号,arch 实现具体 signal 逻辑)→ [`syscall_exit_to_user_mode`](../linux/kernel/entry/common.c#L215)(L215,noinstr 最终出口)。中断进出也在这里:[`irqentry_enter_from_user_mode`](../linux/kernel/entry/common.c#L223)(L223)、[`irqentry_exit_to_user_mode`](../linux/kernel/entry/common.c#L228)(L228)、[`irqentry_enter`](../linux/kernel/entry/common.c#L236)(L236,内核态被打断时的 NMI/IRQ 嵌套处理)。

### B.1.3 第 3 篇 · 时钟与定时器(内核主动驱动的心跳)

源码主要在 [`kernel/time/`](../linux/kernel/time)。这一篇和调度器第 11 本 P1-04 的 hrtick 回扣——`hrtick` 是 hrtimer 的薄包装,实现 EEVDF 的精确抢占。

| 章 | 主题 | 核心源码文件 | 阅读顺序提示 |
|----|------|------------|------------|
| P3-12 | clocksource/clockevent | [`kernel/time/clocksource.c`](../linux/kernel/time/clocksource.c)、[`kernel/time/clockevents.c`](../linux/kernel/time/clockevents.c)、[`include/linux/clocksource.h`](../linux/include/linux/clocksource.h) | 先读 [`struct clocksource`](../linux/include/linux/clocksource.h#L96)(L96,只读纳秒源,带 `read()`/`mask`/`rating`/`watchdog)和 `struct clock_event_device`(可编程定时中断);再看 `clocksource_watchdog` 怎么多源互校、自动降级 |
| P3-13 | timekeeping | [`kernel/time/timekeeping.c`](../linux/kernel/time/timekeeping.c)、[`include/linux/timekeeper_internal.h`](../linux/include/linux/timekeeper_internal.h)、[`kernel/time/ntp.c`](../linux/kernel/time/ntp.c) | 先读 [`struct tk_read_base`](../linux/include/linux/timekeeper_internal.h#L34)(L34)、[`struct timekeeper`](../linux/include/linux/timekeeper_internal.h#L92)(L92,墙上时间 xtime/单调时间 monotonic/NTP 校正);再看 `ktime_get`/`ktime_get_real`/`ktime_get_ts` 怎么用 seqlock 无锁读;NTP 校正在 `ntp.c`(`adjtimex` 系统调用路径) |
| P3-14 | hrtimer ⚠️ 核心 | [`kernel/time/hrtimer.c`](../linux/kernel/time/hrtimer.c)、[`include/linux/hrtimer_types.h`](../linux/include/linux/hrtimer_types.h)、[`include/linux/hrtimer.h`](../linux/include/linux/hrtimer.h) | 先读 [`struct hrtimer`](../linux/include/linux/hrtimer_types.h#L39)(L39,内嵌 timerqueue_node 红黑树节点)和 `hrtimer_cpu_base`(per-CPU,4 个 clock_base:MONOTONIC/REAL/BOOT/TAI);再读 [`__hrtimer_next_event_base`](../linux/kernel/time/hrtimer.c#L505)(L505,挑最早到期)→ [`__hrtimer_run_queues`](../linux/kernel/time/hrtimer.c#L1724)(L1724,扫到期)→ [`hrtimer_interrupt`](../linux/kernel/time/hrtimer.c#L1788)(L1788,硬中断处理)→ [`hrtimer_run_queues`](../linux/kernel/time/hrtimer.c#L1901)(L1901,软中断模式);理解 softexpires 区间怎么合并唤醒 |
| P3-15 | tick 与 NOHZ | [`kernel/time/tick-sched.c`](../linux/kernel/time/tick-sched.c)、[`kernel/time/tick-common.c`](../linux/kernel/time/tick-common.c)、[`kernel/time/tick-oneshot.c`](../linux/kernel/time/tick-oneshot.c)、[`kernel/time/tick-broadcast.c`](../linux/kernel/time/tick-broadcast.c) | 先读 [`tick_sched_do_timer`](../linux/kernel/time/tick-sched.c#L206)(L206,周期 tick 做的事);再读 [`tick_nohz_idle_enter`](../linux/kernel/time/tick-sched.c#L1250)(L1250,进 idle 停 tick)→ [`tick_nohz_irq_exit`](../linux/kernel/time/tick-sched.c#L1287)(L1287,idle 中断退出处理)→ [`tick_nohz_restart_sched_tick`](../linux/kernel/time/tick-sched.c#L1086)(L1086,出 idle 重启 tick);理解 NOHZ 怎么"借下一个 hrtimer"当唤醒源 |
| P3-16 | POSIX timer/itimer | [`kernel/time/posix-timers.c`](../linux/kernel/time/posix-timers.c)、[`kernel/time/posix-cpu-timers.c`](../linux/kernel/time/posix-cpu-timers.c)、[`kernel/time/itimer.c`](../linux/kernel/time/itimer.c) | 先读 `itimer.c`(`setitimer` 映射成 hrtimer,薄包装);再读 `posix-timers.c`(`timer_create`/`timer_settime`);`posix-cpu-timers.c` 是进程 CPU 时间消耗的定时器(SIGALRM/SIGPROF 来源) |

> **时钟篇的主链**:硬件时钟周期触发 → `clock_event_device` 编程 → [`hrtimer_interrupt`](../linux/kernel/time/hrtimer.c#L1788) → [`__hrtimer_run_queues`](../linux/kernel/time/hrtimer.c#L1724) 扫红黑树到期 timer → 调各 timer 的回调(调度器 hrtick、用户 timer、tick_sched_timer)→ [`tick_sched_do_timer`](../linux/kernel/time/tick-sched.c#L206) 推进墙上时间 + 调度统计。NOHZ 让 idle CPU 停掉周期 tick、改由最近的 hrtimer 唤醒。

### B.1.4 第 4 篇 · 信号(内核向进程的异步通知)

源码主要在 [`kernel/signal.c`](../linux/kernel/signal.c)、[`include/linux/sched/signal.h`](../linux/include/linux/sched/signal.h)、`arch/x86/kernel/signal.c`(`__setup_rt_frame`,未 sparse clone)。

| 章 | 主题 | 核心源码文件 | 阅读顺序提示 |
|----|------|------------|------------|
| P4-17 | 信号投递 | [`kernel/signal.c`](../linux/kernel/signal.c)、[`include/linux/sched/signal.h`](../linux/include/linux/sched/signal.h) | 先读 [`struct sighand_struct`](../linux/include/linux/sched/signal.h#L21)(L21,handler 表)、[`struct signal_struct`](../linux/include/linux/sched/signal.h#L94)(L94,线程组共享)、`struct sigpending`/`struct sigqueue`(pending 队列);再读 [`do_send_sig_info`](../linux/kernel/signal.c#L1294)(L1294)→ [`complete_signal`](../linux/kernel/signal.c#L995)(L995,挑目标线程 + 挂 pending + 置 `_TIF_SIGPENDING`) |
| P4-18 | 处理入口 | [`kernel/entry/common.c`](../linux/kernel/entry/common.c)、[`kernel/signal.c`](../linux/kernel/signal.c) | 先读 [`exit_to_user_mode_loop`](../linux/kernel/entry/common.c#L90)(L90,检查 `_TIF_SIGPENDING`)→ [`arch_do_signal_or_restart`](../linux/kernel/entry/common.c#L83)(L83);再读 [`get_signal`](../linux/kernel/signal.c#L2675)(L2675,从 pending 取信号 + 判默认动作);理解为什么信号延迟到返回用户态才跑 handler |
| P4-19 | sigaction 与 rt_sigreturn | `arch/x86/kernel/signal.c`(`__setup_rt_frame`,未 sparse clone)、[`kernel/signal.c`](../linux/kernel/signal.c) | 先读 `sigaction` 系统调用(`do_sigaction`);再理解 `__setup_rt_frame` 在用户栈构建 `ucontext` + `siginfo` + 把返回地址改成 `rt_sigreturn` 系统调用;`SA_ONSTACK` + `sigaltstack` 让信号栈可自定义 |
| P4-20 | 信号与异常 | [`kernel/signal.c`](../linux/kernel/signal.c)([`force_sig_info_to_task`](../linux/kernel/signal.c#L1326) L1326)、`arch/x86/.../fault.c`(缺页、未 sparse clone)、`arch/x86/.../traps.c`(异常入口,未 sparse clone) | 先理解 CPU 异常(缺页/除零/非法指令)的同步入口;再看不可恢复的异常怎么统一走 `force_sig_info_to_task` 投信号(SIGSEGV/SIGFPE/SIGILL),和 `kill` 走的是同一条投递链 |

> **信号篇的主链**:`kill` 系统调用(或异常路径)→ [`do_send_sig_info`](../linux/kernel/signal.c#L1294) → [`complete_signal`](../linux/kernel/signal.c#L995)(挂 pending 队列 + 置 `_TIF_SIGPENDING`)→ 目标进程持续跑 → 返回用户态前 [`exit_to_user_mode_loop`](../linux/kernel/entry/common.c#L90) 检查 `_TIF_SIGPENDING` → [`get_signal`](../linux/kernel/signal.c#L2675) 取信号 → `__setup_rt_frame`(arch 代码)在用户栈构帧 → 跳到用户态 handler → handler 返回触发 `rt_sigreturn` 回内核恢复原现场。

### B.1.5 第 0 篇与第 5 篇(总览与收束)

- **P0-01**(第一性原理):全书的概念起点,无特定源码,但两个工程地基在 [`kernel/softirq.c`](../linux/kernel/softirq.c)(softirq per-CPU 位图、[`handle_softirqs`](../linux/kernel/softirq.c#L511))和 [`include/linux/preempt.h`](../linux/include/linux/preempt.h)(`preempt_count` 嵌套计数)。
- **P5-21**(哲学 + 对照总表):全书回顾,无新源码,对照 Tokio/Go/io_uring 见正文。

---

## B.2 观测手段

光读源码不知道内核实际在干什么,得观测。下面列核心观测工具 + 每条一句"看什么" + 对应章节。所有 `/proc` 文件、`perf`/`ftrace`/`strace`/`bpftrace` 探点都给出书内对应章节。

### B.2.1 /proc 类(读文件,最简单)

| 文件 | 看什么 | 对应章节 |
|------|--------|---------|
| `/proc/interrupts` | 每 CPU 每 IRQ 号的触发次数(行=IRQ,列=CPU)+ 中断名。直接看哪个设备的中断最频繁、IRQ 亲和在哪些核。源码 `show_interrupts` 在 [`kernel/irq/proc.c`](../linux/kernel/irq/proc.c#L460)(L460) | P1-02/P1-03 中断入口 |
| `/proc/softirqs` | 每 CPU 各类 softirq(HI/TIMER/NET_TX/NET_RX/BLOCK/...)的触发次数。看哪个 softirq 最忙(NET_RX 通常最忙)、分布是否均匀 | P1-06 softirq |
| `/proc/<pid>/status` | 进程级:重点看 `SigQ`(队列中信号数/上限)、`SigPnd`(本线程 pending 信号位图)、`ShdPnd`(线程组共享 pending 位图)、`SigBlk`(屏蔽)、`SigIgn`(忽略)、`SigCgt`(注册了 handler 的信号位图)。十六进制位图,bit N 对应信号 N+1 | P4-17 信号投递 |
| `/proc/<pid>/timers` | 进程的 POSIX 定时器列表(每个 timer 的 ID、信号、值、间隔)。看进程设了多少 `timer_create` 定时器 | P3-16 POSIX timer |
| `/proc/timer_list` | hrtimer/clocksource/clock_event 现状:每个 CPU 的 `hrtimer_cpu_base` 状态(`expires_next`/`hres_active`)、挂在前面的 timer、各 clocksource 的 rating/read 函数、tick device。**调试 hrtimer/NOHZ 的金标准**。源码 [`kernel/time/timer_list.c`](../linux/kernel/time/timer_list.c)(`print_cpu` L115、`print_tickdevice` L182) | P3-12/P3-14/P3-15 时钟 |
| `/proc/sys/kernel/hz` | 内核编译的 HZ 值(只读,由 `CONFIG_HZ` 决定)。注意这是编译期常量,不是运行时调节 | P3-15 tick 频率 |
| `/proc/sys/kernel/hostname` | 系统主机名。和本书主题关系不大,但常和 `hz` 一起被举例(都属 `/proc/sys/kernel/`) | — |
| `/proc/stat` | 系统级计数器:第一行 CPU 总时间(user/nice/system/idle/iowait/irq/softirq)、`intr`(总中断数)、`softirq`(总软中断数)、`ctxt`(上下文切换)。`irq`/`softirq` 时间是 P1-04 中断上下文开销的直接度量 | P1-04/P1-06 |
| `/sys/kernel/debug/tracing/` | ftrace 控制接口(见 B.2.3) | 全书 |

### B.2.2 perf(性能计数与采样)

| 命令 | 看什么 | 对应章节 |
|------|--------|---------|
| `perf stat -e irq:softirq-entry,irq:softirq-exit` | softirq 进入/退出次数(每次 softirq 处理的事件数 = entry/exit) | P1-06 |
| `perf stat -e 'irq:*'` | 所有 irq tracepoint 的计数 | P1-02/P1-06 |
| `perf stat -e context-switches,cpu-migrations` | 上下文切换、CPU 迁移次数 | P3-14(时钟驱动调度) |
| `perf stat -e syscalls:sys_enter_read,syscalls:sys_exit_read` | `read` 系统调用进入/退出次数,配合 `-e` 看耗时 | P2-08/P2-09 |
| `perf record -e 'irq:irq_handler_entry' -g` + `perf report` | 谁触发的中断最多、调用栈是什么 | P1-02 |
| `perf record -e 'raw_syscalls:sys_enter' -g` | 哪个进程在调哪些系统调用、调用频率 | P2-08 |
| `perf stat -e timer:hrtimer_expire_exit` | hrtimer 到期次数 | P3-14 |
| `perf top -e irq:softirq_entry` | 实时看 softirq 热点 | P1-06 |

### B.2.3 ftrace(tracepoint,系统调用/中断/信号)

ftrace 的 tracepoint 在 `/sys/kernel/debug/tracing/events/` 下。用 `trace-cmd`(封装)或直接写 `/sys/kernel/debug/tracing/`。

| 跟踪点(tracepoint) | 看什么 | 对应章节 |
|------|--------|---------|
| `raw_syscalls:sys_enter`/`sys_exit` | 所有系统调用进入/退出(参数 nr=系统调用号、args、ret 返回值) | P2-08/P2-11 |
| `syscalls:sys_enter_<name>`/`sys_exit_<name>` | 单个系统调用(如 `sys_enter_read`/`sys_enter_kill`/`sys_enter_sigaction`) | P2-08/P4-17 |
| `irq:irq_handler_entry`/`irq_handler_exit` | 每个 hardirq handler 进入/退出(参数 irq=IRQ 号、name=设备名、ret) | P1-02 |
| `irq:softirq_entry`/`softirq_exit`/`softirq_raise` | softirq 进入/退出/置位(参数 vec=softirq 类型编号) | P1-06 |
| `signal:signal_generate`/`signal_deliver` | 信号投递/送达(参数 sig=信号号、comm/pid=目标、result) | P4-17/P4-18 |
| `timer:hrtimer_cancel`/`hrtimer_expire_entry`/`hrtimer_expire_exit`/`hrtimer_start` | hrtimer 取消/到期/启动(参数 function=回调、now/expires) | P3-14 |
| `timer:timer_start`/`timer_expire_entry`(legacy timer wheel) | 低精度 timer(6.x 仍兼容存在,但 hrtimer 是主流) | P3-14 |
| `sched:sched_switch`/`sched_wakeup` | 调度切换/唤醒(看时钟 hrtick 怎么触发切换) | P3-14/P3-15 |
| `workqueue:workqueue_execute_start`/`workqueue_execute_end` | workqueue 执行单个 work(参数 function=回调) | P1-07 |

例子:抓一次网络 RX 的完整链路(网卡中断 → softirq → 协议栈):

```
trace-cmd record -e 'irq:irq_handler_entry' -e 'irq:irq_handler_exit' \
                 -e 'irq:softirq_entry' -e 'irq:softirq_exit' -e 'net:*'
trace-cmd report
```

抓所有 `kill`/信号投递:

```
trace-cmd record -e 'signal:*'
trace-cmd report
```

### B.2.4 strace(用户态系统调用追踪)

`strace` 在用户态拦截系统调用(基于 `ptrace`),是看"用户程序到底调了哪些系统调用、参数是什么、返回值多少"的最直接工具。

| 命令 | 看什么 | 对应章节 |
|------|--------|---------|
| `strace -c ./prog` | 统计程序运行期间各系统调用次数、总耗时、错误数 | P2-08 |
| `strace -e trace=read,write ./prog` | 只看 read/write 调用(参数 fd/buf/count、返回值) | P2-09 |
| `strace -e trace=signal ./prog` | 只看信号相关(kill/tgkill/sigaction/rt_sigreturn/...) | P4-17/P4-19 |
| `strace -T -ttt ./prog` | 每个系统调用带绝对时间戳 + 单次耗时 | P2-08 |
| `strace -f ./prog` | 跟踪 fork 出的子进程(信号常跨进程) | P4-17 |

注意:`strace` 会显著拖慢程序(每次系统调用两次陷入内核),只用于调试,别用于生产性能测量——生产用 B.2.5 的 `bpftrace` 或 B.2.2 的 `perf`。

### B.2.5 bpftrace(任意探点,低开销)

`bpftrace` 用 eBPF 在内核任意 `kprobe`/`tracepoint`/`uprobe` 上挂探针,开销比 strace 低得多,适合生产观测。

| 命令 | 看什么 | 对应章节 |
|------|--------|---------|
| `bpftrace -e 'tracepoint:irq:softirq_entry { @[vec] = count(); }'` | 各类 softirq 触发次数(vec=0 是 HI、1 TIMER、3 NET_RX...) | P1-06 |
| `bpftrace -e 'tracepoint:irq:irq_handler_entry { @[name] = count(); }'` | 各设备中断次数(网卡名如 eth0/virtio...) | P1-02 |
| `bpftrace -e 'kprobe:handle_softirqs { printf("softirq run on cpu=%d pending=%x\n", cpu, arg0); }'` | 每次 softirq 处理时的 pending 位图 | P1-06 |
| `bpftrace -e 'tracepoint:raw_syscalls:sys_enter { @[args->id] = count(); }'` | 各系统调用号触发次数 | P2-08 |
| `bpftrace -e 'tracepoint:signal:signal_generate { printf("sig=%d -> pid=%d\n", args->sig, args->pid); }'` | 信号投递事件 | P4-17 |
| `bpftrace -e 'kprobe:hrtimer_interrupt { printf("hrtimer irq on cpu=%d\n", cpu); }'` | 每次 hrtimer 硬中断 | P3-14 |
| `bpftrace -e 'kprobe:get_signal { printf("pid=%d checking signal\n", pid); }'` | 进程返回用户态检查信号 | P4-18 |
| `bpftrace -e 'kprobe:complete_signal { @[comm] = count(); }'` | 谁在投信号 | P4-17 |

> bpftrace 配合 `funcslower`(kernel 自带脚本)还能测任意函数的尾延迟。bpftrace 是观测本书四个机制**最灵活**的工具,但需要内核编译时开启 `CONFIG_BPF`、`CONFIG_KPROBES`、`CONFIG_BPF_EVENTS`(详见第 18 本 eBPF 书)。

---

## B.3 与其他 OS 对照

Linux 这四个机制不是孤岛,其他操作系统也解同样的问题——"事件跨越特权边界"。对照阅读能看清哪些是普适解、哪些是 Linux 的工程选择。

### B.3.1 中断:Linux 的 IRQ domain vs BSD 的 intr vs Windows 的 IRQL

| OS | 中断抽象 | 关键特点 |
|----|---------|---------|
| **Linux** | `irq_chip`(硬件操作)+ `irq_domain`(hwirq → Linux IRQ 映射)+ `irq_desc`(中断描述符) | 层级抽象支持 GIC/LAPIC/IOAPIC/MSI 各类控制器统一;上半部/下半部(softirq/workqueue)切分;per-CPU pending 位图无锁化 |
| **BSD(FreeBSD)** | `intr`(中断源)+ `ithread`(中断线程) | 较早就用**中断线程**(interrupt thread)处理中断——把中断处理放到可调度的内核线程里,天然可阻塞。Linux 的 `threaded_irq`(2009 年引入)思路类似,但默认仍用 hardirq + softirq 两段 |
| **Windows** | **IRQL**(Interrupt Request Level,中断请求级) | Windows 的核心设计:**用 IRQL 分层**。IRQL 是 CPU 当前的"中断优先级"(0=PASSIVE/APC、1=DISPATCH、2=设备中断、更高=时钟/Profiler/IPI)。提高 IRQL 等于屏蔽低于该级的中断,类似 Linux 的 `preempt_count` + `local_irq_disable`,但**按层级而非按计数**。Windows 不用 softirq,而是在 DISPATCH_LEVEL 调度 DPC(Deferred Procedure Call)做"下半部"——DPC 和 Linux softirq 同构 |

对照的洞察:**中断的"上半部快收快放、下半部延后接力"是普适思路**(Linux softirq、FreeBSD ithread、Windows DPC 都是它的变种);**中断上下文的"我现在能不能阻塞"判断**(Linux `preempt_count` 嵌套计数、Windows IRQL 层级)是各家共同的核心机制,只是实现选了不同形态。Linux 用计数(支持任意层嵌套),Windows 用层级(更直观但层级有限)。

### B.3.2 系统调用:SYSCALL vs BSD 的 syscall vs Windows 的系统调用

| OS | 系统调用入口 | 关键特点 |
|----|------------|---------|
| **Linux(x86_64)** | `SYSCALL` 指令(MSR 直跳,不查 IDT)+ `sys_call_table[]` 函数指针数组 | 6 寄存器传参(rdi/rsi/rdx/r10/r8/r9);VDSO 让 `gettimeofday` 不进内核 |
| **Linux(老 x86)** | `int 0x80` 软中断(查 IDT、压完整 trap frame) | 已废弃,比 `SYSCALL` 慢一个数量级 |
| **BSD** | `syscall` 指令(类似 `SYSCALL`)或 `int 0x80` | 类似的寄存器传参 + 系统调用号表;FreeBSD 有 `__syscall` stub |
| **Windows** | `syscall` 指令(x64)+ SSDT(System Service Descriptor Table) | 类似 `sys_call_table`,但 Windows 把系统调用分两组(Win32k/其他)用不同表;Win32 用户态有 ntdll.dll 做 stub |

各家系统调用入口机制**惊人同构**:都是"一条特权切换指令 + 一张函数指针表 + 寄存器传参"。差别在细节(Windows 的 SSDT 分组、Linux 的 VDSO 优化)。

### B.3.3 时钟:clocksource/clockevent vs 其他 OS 的时钟抽象

| OS | 时钟抽象 | 关键特点 |
|----|---------|---------|
| **Linux** | `struct clocksource`(只读纳秒源)+ `struct clock_event_device`(可编程定时中断) | 高精度 hrtimer 红黑树;NOHZ idle 停 tick 省电;timekeeping 用 seqlock 无锁读墙上时间 |
| **BSD** | `timecounter`(类似 clocksource)+ `eventtimer`(类似 clock_event_device) | FreeBSD 的抽象和 Linux 几乎一一对应(timecounter 提供 read 纳秒,eventtimer 提供可编程中断);FreeBSD 也支持无 tick(no-tick idle) |
| **Windows** | HAL(Hardware Abstraction Layer)时钟 + `KeQuerySystemTime` | Windows 用 HAL 抽象时钟硬件,`KeQueryPerformanceCounter` 提供高精度计时(类似 clocksource);`KdDpcTimer`/`KeSetTimer` 用 DPC 实现定时器(类似 hrtimer) |

时钟抽象的**普适解**就是把硬件分两类:**只读的高精度计数器**(墙上时间用)+ **可编程的定时中断源**(定时器/tick 用)。Linux 的 `clocksource`/`clock_event_device` 二分是这种思路的清晰实现,BSD 的 `timecounter`/`eventtimer` 是它的同构翻版。

### B.3.4 信号:Linux 信号 vs Windows 异常 vs 其他 OS

| OS | 异步通知机制 | 关键特点 |
|----|------------|---------|
| **Linux** | 信号(signal)+ 异常统一走 `force_sig` | 延迟到返回用户态才跑 handler;pending 队列 + 位图;`sigaction` + `rt_sigreturn` 栈帧劫持 |
| **Windows** | **没有信号**——用 Structured Exception Handling(SEH)处理异常 + APC(Asynchronous Procedure Call)做异步通知 | Windows 的 SEH 是同步的(异常发生时处理),APC 是异步的(类似信号延迟到特定点执行)。Windows 进程间通信用 IPC(消息、管道、事件对象)而非信号 |
| **BSD** | 信号(和 Linux 兼容,POSIX) | BSD 是 POSIX 信号规范的主要来源之一;Linux 的 `rt_sigqueueinfo` 实时信号排队机制和 BSD 一致 |

> 一个有趣的对照:**Linux/Unix 把"异步通知"和"异常处理"统一成信号**(段错误=SIGSEGV,用户态 handler 能截获),而 **Windows 把它们分开**(SEH 处理异常、APC 做异步通知)。Linux 的统一让用户态能用一套 handler 处理硬件错误和人为通知,代价是信号语义复杂(延迟投递、可重入性、信号栈);Windows 的分离语义清晰但接口分散。这是两种不同的工程哲学。

---

## B.4 关键调参

本节列四个子系统的核心调参,每条讲**何时用 + 改了什么**。所有参数名都用 Grep/源码核实过(`CONFIG_HZ`/`CONFIG_NO_HZ_FULL`/`isolcpus`/`irqaffinity` 等)。

### B.4.1 CONFIG_HZ(时钟频率)

- **作用**:内核编译期常量,决定周期性 tick 的频率(`HZ=100` = 每秒 100 次 tick、`HZ=250` = 250 次、`HZ=1000` = 1000 次)。在 [`kernel/Kconfig.hz`](../linux/kernel/Kconfig.hz)(未 sparse clone,内核源码 `kernel/Kconfig.hz`)中定义。
- **何时用**:
  - **桌面/交互**(`HZ=1000`):tick 更频繁,时间片更细,响应更快,但 idle 时空转更多(若无 NOHZ),耗电略高。
  - **服务器**(`HZ=250` 或 `HZ=100`):tick 开销低,CPU 留给业务更多,适合吞吐型负载。
  - **虚拟化/容器**:常用默认(`HZ=250`),配合 NOHZ 减少 guest 噪声。
- **改了什么**:影响调度器时间片粒度(P3-15 tick)、统计采样精度、`gettimeofday` 的更新频率(若不用 VDSO 高精度)。
- **注意**:`/proc/sys/kernel/hz` 是只读的,运行时不能改——这是编译期常量,要改得重编内核。

### B.4.2 NO_HZ_IDLE / NO_HZ_FULL(动态时钟)

- **作用**:见 [`kernel/time/Kconfig`](../linux/kernel/time/Kconfig)(`NO_HZ_IDLE` L113、`NO_HZ_FULL` L123)。`NO_HZ_IDLE` 让 idle CPU 停掉周期 tick(标准配置,默认开);`NO_HZ_FULL` 让跑单一 CPU 密集任务的 CPU 几乎完全无 tick(只在必要时)。
- **何时用**:
  - **`NO_HZ_IDLE`**(普通):几乎所有现代发行版默认开。idle CPU 真正睡眠、省电、减少虚拟化噪声。**无脑开**。
  - **`nohz_full=N-M`(内核参数)**:给 N~M 号 CPU 跑 CPU 密集实时任务(如低延迟交易、DPDK、单线程压满的数据库),几乎完全无 tick。需要 `CONFIG_NO_HZ_FULL` 编译开启 + 配 `rcu_nocbs=` 把 RCU offload 走。
- **改了什么**:tick 是调度器时间片的来源(P3-15),无 tick 的 CPU 上调度器无法靠 tick 主动抢占——所以 `nohz_full` 的 CPU 必须只跑少数(通常 1 个)任务,靠显式 `sched_yield`/IO 阻塞切换。
- **陷阱**:`nohz_full` 牺牲了调度器的细粒度抢占权,适合"我把这个核独占给一个任务"的场景,不适合普通多任务负载。

### B.4.3 isolcpus(隔离 CPU)

- **作用**:内核启动参数 `isolcpus=N-M`,把 N~M 号 CPU 从默认调度域里**踢出去**——普通进程不会被调度到这些核,只有显式 `sched_setaffinity` 才能绑上去。
- **何时用**:低延迟/实时场景,给关键任务独占的核,避免被其他进程干扰、减少 cache 污染。常和 `nohz_full`、`rcu_nocbs` 配合(`isolcpus=2-7 nohz_full=2-7 rcu_nocbs=2-7`)。
- **改了什么**:这些核不参与负载均衡、不接默认中断(除非显式配 `irqaffinity`)、不跑 RCU softirq(offload 给别的核)。
- **陷阱**:6.x 内核社区在弱化 `isolcpus`(更推荐 `cgroup cpuset` 做隔离),但 `isolcpus` 仍是简单可用的手段。

### B.4.4 irqaffinity / smp_affinity(中断亲和)

- **作用**:控制某个 IRQ 号的中断**只发给指定 CPU**。每个 IRQ 有 `/proc/irq/<irq>/smp_affinity`(位掩码)和 `/proc/irq/<irq>/smp_affinity_list`(CPU 列表)。
- **何时用**:
  - **网卡 RSS**(Receive Side Scaling):把网卡的多队列(Rx queue)中断绑定到不同 CPU,让收包并行(每个队列一个 IRQ,绑一个核)。
  - **隔离关键 CPU**:把所有硬件中断(网卡、磁盘、USB)赶到非关键核,让 `isolcpus`/`nohz_full` 的核不被中断打扰。
  - **NUMA 亲和**:把网卡中断绑到网卡所在 NUMA node 的核,减少跨 node 访问。
- **改了什么**:`echo <cpumask> > /proc/irq/<irq>/smp_affinity`。注意 IRQ 号从 `/proc/interrupts` 查,设备名在最后一列。
- **配合**:`irqbalance` 守护进程会自动调中断亲和,做精细控制时通常先 `systemctl stop irqbalance`。

### B.4.5 PREEMPT_RT(实时补丁)

- **作用**:`PREEMPT_RT`(实时 Linux)把内核变成**完全可抢占**——把大部分自旋锁换成可睡眠的 `rt_mutex`、把硬中断线程化(`threaded irqs`,所有 hardirq 跑在内核线程里)、把高延迟代码段拆开。2024 年起 `PREEMPT_RT` 已主线化(5.x 起逐步合入,6.x 完整)。
- **何时用**:硬实时场景(工业控制、机器人、音视频、低延迟交易),要求最坏情况延迟可预测(微秒级)。
- **改了什么**:
  - 编译时开 `CONFIG_PREEMPT_RT`(替代 `CONFIG_PREEMPT`/`CONFIG_PREEMPT_VOLUNTARY`)。
  - 自旋锁(`spinlock_t`)在 RT 下变成 `rt_mutex` 包装——可睡眠、可被优先级反转保护(PI)。
  - 硬中断默认线程化(`request_threaded_irq` 变默认),hardirq 只做最少的事(ack 中断控制器)、真正处理在 `irq/N-xxx` 内核线程里。
  - softirq 也部分线程化。
- **代价**:吞吐下降(锁开销变大、上下文切换增多),不适合吞吐优先的负载。
- **回扣本书**:RT 的"硬中断线程化"正是本书 P1-05 讲的"上半部/下半部切分"的极端版——把上半部也变成可调度的线程,牺牲一点延迟换取完全可抢占性。

### B.4.6 调参的一般原则

- **观测先于调参**:用 B.2 的 `/proc`/`perf`/`ftrace`/`bpftrace` 先看清楚瓶颈在哪(是中断太多?系统调用太慢?信号风暴?tick 噪声?),再决定调哪个参数。盲调大概率帮倒忙。
- **默认值是甜点**:`CONFIG_HZ=250`、`NO_HZ_IDLE=y`、不开 `isolcpus`/`nohz_full`、不开 RT——这些是经过大量负载验证的默认配置,99% 的场景不用改。
- **低延迟/实时才动**:只有当你确认问题是"中断/调度延迟抖动"(用 `cyclictest` 测延迟分布、`perf sched` 看调度延迟)时,才考虑 `isolcpus`+`nohz_full`+`irqaffinity` 三件套或 `PREEMPT_RT`。
- **改一个看一个**:每改一个参数,重新观测(`cyclictest`/`perf stat`/`/proc/interrupts`),确认真的有效果再继续。

---

## B.5 最后:读完本书之后

本书及附录到此结束。如果你读完了二十一章正文 + 附录 A/B,你现在应该能在脑子里放映出:

- 一次**网卡中断**怎么把 CPU 从用户进程拉进内核(P1-02)、上半部怎么快收快放(P1-05)、softirq 怎么在 IRQ 退出后接力把包推给协议栈(P1-06)、workqueue 怎么处理可睡眠的延迟工作(P1-07)。
- 一次 **`read()` 系统调用**怎么走过 `SYSCALL` 指令(P2-08)、`sys_call_table` 怎么分派(P2-08)、参数怎么跨边界(P2-09)、VDSO 怎么让读时间避免进内核(P2-10)、seccomp 怎么在入口前过滤(P2-11)。
- 一个 **hrtimer** 怎么在 per-CPU 红黑树上排队(P3-14)、`hrtimer_interrupt` 怎么扫到期调回调(P3-14)、NOHZ 怎么让 idle CPU 停 tick 又不丢事件(P3-15)、用户的 `setitimer` 怎么映射成 hrtimer(P3-16)。
- 一个 **`kill -9`** 怎么挂到目标进程的 pending 队列(P4-17)、为什么延迟到返回用户态才跑 handler(P4-18)、`rt_sigreturn` 怎么恢复原现场(P4-19)、CPU 异常怎么和信号统一(P4-20)。
- 这背后"延迟处理(softirq/信号)、per-CPU 无锁化(softirq pending/hrtimer cpu_base)、上下半部切分(中断/hrtimer 软中断模式)、seqlock 无锁读(timekeeper/VDSO)"四条哲学是怎么撑起整个内核事件驱动骨架的(P5-21)。

下一步:

1. **动手观测**:在 Linux 机器上跑 `cat /proc/interrupts`、`cat /proc/softirqs`、`cat /proc/timer_list`、`cat /proc/<pid>/status | grep Sig`,看内核事件处理的活的实况(B.2)。
2. **读源码**:按 B.1 的顺序,从 [`kernel/irq/handle.c`](../linux/kernel/irq/handle.c) 的 [`__handle_irq_event_percpu`](../linux/kernel/irq/handle.c#L139)(L139)开始,顺着中断主链读到 [`kernel/softirq.c`](../linux/kernel/softirq.c) 的 [`handle_softirqs`](../linux/kernel/softirq.c#L511)(L511),再读 [`kernel/time/hrtimer.c`](../linux/kernel/time/hrtimer.c) 的 [`hrtimer_interrupt`](../linux/kernel/time/hrtimer.c#L1788)(L1788)和 [`kernel/signal.c`](../linux/kernel/signal.c) 的 [`complete_signal`](../linux/kernel/signal.c#L995)(L995)、[`kernel/entry/common.c`](../linux/kernel/entry/common.c) 的 [`exit_to_user_mode_loop`](../linux/kernel/entry/common.c#L90)(L90)。
3. **跟社区**:订阅 LWN,看每年 LPC(Linux Plumbers Conference)的 sched/IRQ/timers/signal talks,跟进 NOHZ_FULL、PREEMPT_RT、hrtimer、io_uring(中断的"事件模型代际差"对照,见 P1-05/P5-21)的最新进展。
4. **读对照系**:本书正文每一篇的 ★ 对照栏(P1-05/P1-06/P3-14/P3-15/P4-17/P4-18)都点到 Tokio/Go/io_uring,如果想看清"内核事件模型 vs 用户态运行时"的完整全栈,配套读《深入浅出系列》的《Tokio》《Go runtime》《块设备与 IO》(io_uring)三本。

中断、系统调用、时钟、信号是 Linux 内核的**事件驱动骨架**——它们不直接产出业务结果,但没有它们,内核无法响应任何外部世界的变化。本书只是入门;但如果你把二十一章 + 附录 A/B 的地图刻进脑子里,再读任何内核事件相关的文章、源码、talk,你都能立刻定位"我在哪、这条事件流的上下游是谁"。这才是"看懂 Linux 内核机制"的真正起点。

**全书及附录完。**
