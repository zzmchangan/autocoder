# 第十章 · VDSO:不进内核的"系统调用"

> 篇:P2 系统调用
> 主线呼应:上一章我们讲完了一次系统调用怎么"走 `SYSCALL` 指令进内核、走 `SYSRET` 指令回用户态",以及参数指针怎么靠 `copy_from_user` 的 page fault fixup 安全地穿过用户/内核边界。但你一定隐隐觉得不对劲——`gettimeofday`、`clock_gettime` 这种"读个时间"的小事,几乎每个稍大点的程序每秒都在做,有些热点路径一秒调用上百万次。如果每一次都得真的进一次内核(`SYSCALL` → 保存现场 → 进 ring 0 → 查 `sys_call_table` → 执行 → 恢复现场 → `SYSRET`),那光是"读时间"这一项就能把 CPU 烧掉一大块。内核给了另一条路:**VDSO(Virtual Dynamic Shared Object)**——把"时间"这种只读、又超高频率的信息,直接放在一块**内核维护、用户态映射**的共享页里,用户态函数根本不进内核,就像读自己进程的内存一样把时间读出来。这一章讲的不是"又一种系统调用",而是"**怎么让一次系统调用压根不发生**"——它是第 2 篇这场"合法进内核"之旅的反面高潮:进内核是有代价的,所以能不进就别进。

## 核心问题

**`gettimeofday`/`clock_gettime` 这类读时间的系统调用,为什么要特别对待、让它干脆不进内核?内核和用户态是怎么做到"同一份数据、一个写一个读"还不出错的?用户态读时间时,内核正在写共享页,这中间的并发怎么解决?VDSO 凭什么既快又对?**

读完本章你会明白:

1. VDSO 是什么:**一个内核映射到每个进程地址空间的共享页 + 一段在用户态执行的函数**,它让读时间完全不进内核。
2. 为什么 `gettimeofday` 这种调用值得特殊对待:它**只读**、**超高频率**、**结果由内核独占维护**,这三条决定了它有"避免进内核"的优化空间。
3. 内核侧 `update_vsyscall` 怎么把墙上时间写进共享页(每个 tick、每次 `settimeofday` 都写)。
4. **seqlock 在用户态的化身**:内核写时把版本号从偶数变奇数、写完再变偶数,用户态读到奇数就重试、读到偶数才用——无锁(用户态拿不到内核锁)、又一致(永远读不到写一半的中间状态)。

> **逃生阀**:如果你已经知道"VDSO 就是个被映射进进程的 .so,`gettimeofday` 不进内核"这个结论,可以直接跳到 10.4(seqlock 配合)和 10.5(技巧精解)。但 10.2(为什么是时间、为什么是只读)和 10.3(内核写共享页的路径)是理解 seqlock 为什么必要的地基,建议读。

---

## 10.1 一句话点破

> **VDSO 把"读时间"变成一次普通的用户态内存读:内核把墙上时间维护在共享页里,用户态函数直接读这个页就算时间;读的时候靠 seqlock 的奇偶版本号避开"内核正在写"的中间态,既不要系统调用、又不会读到写一半的数据。**

这是结论,不是理由。本章倒过来拆:先看为什么偏偏是"读时间"值得这么做,再看共享页里放了什么、内核怎么写它,然后钻进最硬核的 seqlock 读写配合,最后做反面对比——朴素地每次进内核会怎样。

---

## 10.2 为什么是"读时间"值得不进内核

系统调用成百上千个,`read`、`write`、`open`、`socket`、`fork`、`mmap`……为什么唯独 `gettimeofday`、`clock_gettime`、`clock_getres`、`time` 这一小撮被拎出来塞进 VDSO?要回答这个问题,先看一次普通系统调用的代价有多大。

### 一次系统调用的代价

第 8 章讲过 `SYSCALL` 指令比老的 `int 0x80` 快一个数量级,因为它不查 IDT、不压完整 trap frame,CPU 用 MSR 直跳内核入口。但即使是最快的 `SYSCALL`,一次调用也不是免费的:

```
 一次 SYSCALL + SYSRET 的固定开销(简化,数字量级):

  用户态: SYSCALL 指令          ── CPU 切到 ring 0
  内核入口 do_syscall_64:
    保存用户态寄存器(压栈)        ── 几十条指令
    syscall_enter_from_user_mode_prepare ── 第 8 章讲过的入口框架
    查 sys_call_table[nr]            ── 找到 sys_gettimeofday
    执行 sys_gettimeofday            ── 真正干活
    copy_to_user 把结果拷回用户态    ── 页边界检查 + 实际拷贝
    syscall_exit_to_user_mode        ── 退出口还要检查信号/pending
    恢复用户态寄存器
  SYSRET 指令                       ── CPU 切回 ring 3
                                      ↓
  整条路径:几百到上千条指令,涉及两次特权级切换、TLB/缓存扰动
```

在现在的 x86 上,一次"空"系统调用(round trip)大约在 **100~300 纳秒**这个量级,具体数字随 CPU 代际和内核版本浮动。这对一年调几次的系统调用(`mount`、`reboot`)完全可以忽略。但对 `gettimeofday`,问题性质完全变了。

### `gettimeofday` 的特殊性:只读、超高频、内核独占维护

`gettimeofday`/`clock_gettime` 有三条别的系统调用很难同时满足的特性:

1. **它只读**。`read(fd, buf, n)` 要改文件偏移、可能要等数据、可能要触发磁盘 IO——它会**改变内核状态**。`gettimeofday` 不改任何东西,它只是"问一下现在几点",纯读。
2. **它超高频率**。几乎所有正经程序都频繁读时间:打日志带时间戳、限流(令牌桶看现在到没到点)、统计耗时(前后各读一次)、随机数播种、缓存 TTL 判断、分布式系统打版本号(逻辑时钟)……一个跑得欢的 Java/Go 服务,一秒读时间上万次是常态;某些热点路径(比如 Redis 单线程事件循环每处理一个命令都更新 LRU 时间)一秒读**上百万次**。
3. **结果由内核独占维护**。"现在几点"这件事,只有内核知道:墙上时间靠时钟硬件 + NTP 校正维护在 `struct timekeeper` 里,用户态没有别的合法途径自己算出来(直接读 TSC 不行,得知道基点和换算系数)。

把这三条放一起,就看出 `gettimeofday` 和 `read` 的本质区别:

| 系统调用 | 读写 | 频率 | 结果来源 |
|---|---|---|---|
| `read`/`write`/`open` | 改内核状态 | 低~中 | 内核现场算 |
| `fork`/`mmap` | 大改内核状态 | 极低 | 内核现场算 |
| **`gettimeofday`/`clock_gettime`** | **只读** | **极高** | **内核维护的墙上时间** |

后三者都"现场算、要改状态",没法避免进内核;而 `gettimeofday` 的结果内核**早就维护好了、放在固定位置、谁读都是同一份**,这给"避免进内核"留下了空间。

> **不这样会怎样**:如果 `gettimeofday` 每次都真的进内核,一个每秒调一百万次读时间的热点程序(这并不夸张,Redis、数据库、网络框架都这样),光读时间就要消耗掉**一个 CPU 核的全部算力**(100ns × 1M = 100ms,占一核 10%;如果系统调用更慢或调用更密,轻松吃满)。更糟的是多核场景:墙上时间是全局唯一的,内核要用一把 `timekeeper_lock` 保护,64 核同时读时间会在锁上排队,锁竞争本身又放大开销。**百万级 QPS 的读时间,直接拖垮 CPU**。

那怎么办?既然结果只读、内核已经维护好、放在固定位置——**把它放到用户态也能看见的地方,让用户态自己读**。这就是 VDSO。

> **钉死这件事**:VDSO 不是"又实现一遍 `gettimeofday`",它是"把内核维护的墙上时间数据,搬到一块用户态映射的共享页里,让用户态函数直接读"。优化的是**数据的位置**,而不是**算法的快慢**。这条思路(把高频只读数据搬到读方就近)是内核性能优化的万能套路之一,和 per-CPU 数据无锁化是同源的——都是"用数据布局消灭锁竞争"。

---

## 10.3 共享页里有什么:内核怎么写,用户态怎么读

VDSO 全名是 **Virtual Dynamic Shared Object**(虚拟动态共享对象)。说它"虚拟",是因为它**长得像一个普通 `.so`**(有 ELF 头、有符号 `__vdso_gettimeofday`、能被动态链接器解析),但它**不是磁盘上的文件**——内核在进程创建时,由 `setup_additional_pages` 这类 arch 钩子把一页(或几页)内核准备的代码 + 数据映射进进程地址空间(通常在栈附近的高地址区,`vdso` 是 `cat /proc/<pid>/maps` 里能看到的一段)。它有两部分:

1. **代码部分**:`gettimeofday`/`clock_gettime`/`clock_getres`/`time` 的纯用户态实现。这些函数的机器码由内核提供,但执行时跑在 ring 3(用户态),就像进程自己写的代码一样。
2. **数据部分**:一块叫 `struct vdso_data` 的结构,里面是内核维护的"时间基准"(墙上时间的秒/纳秒、时钟换算系数、墙上时间到单调时间的偏移、时区信息……)。**代码部分读数据部分算时间,全程不进内核**。

> arch 细节:`arch/x86/entry/vdso/` 下有 `vdso.lds`(链接脚本)、`vclock_gettime.c`(那些用户态函数的 C 源码)、`vma.c`(映射逻辑)。本书 sparse 树未包含 `arch/x86/`,这里只描述作用——你只要知道"内核在 `execve` 时把这块页映射进每个新进程"即可,具体映射代码不影响理解原理。

### 共享页的布局:`struct vdso_data`

`struct vdso_data` 的定义在通用头 `include/vdso/datapage.h`(跨 arch 共享,本地 sparse 树未拉)。它本质上是这样一个布局(简化示意,字段名对齐源码):

```
 struct vdso_data(每块,简化,非源码原文):

 ┌─────────────────────────────────────────────────────────┐
 │ u32 seq                ← seqlock 版本号(关键!)         │
 │ s32 clock_mode         ← 时钟源类型(VDSO 能用 TSC 才行)│
 │ u64 cycle_last         ← 上次更新的硬件 cycle 计数       │
 │ u64 mask               ← 时钟计数器位宽掩码              │
 │ u32 mult               ← cycle → 纳秒的换算乘子          │
 │ u32 shift              ← 配套的位移(用移位代替除法)     │
 │ struct vdso_timestamp basetime[N]  ← 各时钟的基准时间    │
 │   ├─ [CLOCK_REALTIME]      .sec / .nsec                  │
 │   ├─ [CLOCK_MONOTONIC]     .sec / .nsec                  │
 │   ├─ [CLOCK_BOOTTIME]      .sec / .nsec                  │
 │   ├─ [CLOCK_TAI]           .sec / .nsec                  │
 │   ├─ [CLOCK_REALTIME_COARSE]  ...                        │
 │   ├─ [CLOCK_MONOTONIC_COARSE] ...                        │
 │   └─ [CLOCK_MONOTONIC_RAW]   ...                         │
 │ s32 tz_minuteswest     ← 时区(给 gettimeofday 的 tz)    │
 │ s32 tz_dsttime                                             │
 │ u32 hrtimer_res        ← 给 clock_getres 用              │
 └─────────────────────────────────────────────────────────┘
   ↑ 实际上是数组 vdata[CS_HRES_COARSE] 和 vdata[CS_RAW] 两块
     (高精度/粗精度 + raw 时钟各自一份)
```

`clock_mode` 是这套机制的命门:它告诉用户态函数"当前的时钟硬件能不能直接在用户态读"。x86 上,如果 `clock_mode == VDSO_CLOCKMODE_TSC`(或更精确的 `VDSO_CLOCKMODE_PVCLOCK`/`VDSO_CLOCKMODE_HVCLOCK`,虚拟化场景),用户态就能直接用 `rdtsc` 读 TSC、拿 `mult`/`shift` 把 cycle 换算成纳秒、加上 `basetime[CLOCK_REALTIME]` 的基准,算出当前时间;如果 `clock_mode == VDSO_CLOCKMODE_NONE`(比如时钟源被切成了 HPET,VDSO 读不了),那 VDSO 函数会老老实实 fallback 到真系统调用。这就是为什么切换时钟源(`sysfs` 里改 `current_clocksource`)有时会让读时间变慢——VDSO 不灵了。

### 用户态读时间的算法

用户态 VDSO 函数(以 `clock_gettime(CLOCK_REALTIME, &ts)` 为例)做的事情,展开是这样(简化,非源码原文):

```c
/* 用户态 VDSO 函数 do_clock_gettime,简化示意 */
do {
    seq = vdso_read_begin(vdata);          /* 读版本号,带 smp_rmb */
    if (vdata->clock_mode == VDSO_CLOCKMODE_NONE)
        return fallback_syscall();          /* 时钟源不支持,进内核 */
    cycles = rdtsc() & vdata->mask;         /* 直接读 TSC */
    delta = (cycles - vdata->cycle_last) & vdata->mask;
    ns = (delta * vdata->mult) >> vdata->shift;  /* cycle→ns */
    ts->sec  = vdata->basetime[CLOCK_REALTIME].sec;
    ts->nsec = vdata->basetime[CLOCK_REALTIME].nsec + ns;
    /* nsec 进位到 sec 的细节略 */
} while (vdso_read_retry(vdata, seq));      /* 版本号变了?重来 */
return 0;
```

注意三个关键点:① **整个函数没有一条系统调用指令**(除非 fallback);② 它要读 `basetime`、`mult`、`shift`、`cycle_last` **好几个字段**,如果这些字段读到的是"内核写了一半"的中间状态(比如 `sec` 已经更新但 `nsec` 还没),算出来的时间就错了;③ `do { ... } while (vdso_read_retry)` 这个循环,就是 seqlock 的读者侧——**读到不一致就重来**。这引出本章最硬核的技巧。

---

## 10.4 seqlock 在用户态的化身:无锁又一致

这是 VDSO 整套设计里最精妙的一环。问题先摆清楚:

- **共享页是"一个写者(内核)、多个读者(各进程的各线程)"**。写者是内核(每个 tick 更新一次,大约每 1ms 一次,加上 `settimeofday`/`adjtime` 偶发更新),读者是所有进程所有线程(可能上千个线程同时读)。
- 读者**拿不到任何锁**:用户态既不能拿内核的自旋锁(那是 ring 0 的数据结构),也不能拿用户态的 pthread 锁(那是进程内的,跨进程共享页没法用)。读者必须**完全不阻塞、不等待**。
- 但读者**必须读到一致的数据**:`sec`、`nsec`、`mult`、`shift`、`cycle_last` 这些字段是一个整体,读到写一半的中间态会算出错误的时间(比如时间突然倒退、跳变)。

这是一对尖锐的矛盾:**无锁** vs **一致**。普通的互斥锁能保证一致但会阻塞读者;原子变量能无锁但只适合单个字段,扛不住"多字段原子读"。seqlock(seqlock_t / seqcount_t,Linux 内核的经典原语,见 [`include/linux/seqlock.h`](../linux/include/linux/seqlock.h))就是为这种"写少读多、读者绝不阻塞"的场景设计的,而 VDSO 把它的精髓搬到了用户态/内核跨边界场景。

### seqlock 的基本思想:版本号奇偶

seqlock 用一个递增的版本号(`seq`)协调读写:

```
 seqlock 读写时序(简化):

  写者(内核,持有 timekeeper_lock):
    seq: 0(偶数,稳定状态)
    ── 准备写 ──
    seq = 1 (奇数!表示"正在写,读者别用")   ← smp_wmb
    [ 改 basetime.sec / nsec / mult / ... ]
    seq = 2 (偶数!表示"写完了")              ← smp_wmb
    ── 写完 ──

  读者(用户态,不拿锁):
    seq1 = read(seq)              ← smp_rmb
    [ 读 basetime.sec / nsec / mult / ... ]
    smp_rmb
    seq2 = read(seq)
    if (seq1 != seq2 || seq1 是奇数):   ← 写者正在写或刚写完
        重来(回到 seq1 = read(seq))
    else:
        用读到的数据
```

奇偶的语义非常精确:

- **偶数** = 共享页处于**稳定状态**,此刻没有写者,或上一次写已经完整结束。读者读到偶数版本号、读完数据后版本号没变,就保证读到的字段是同一个"快照"。
- **奇数** = 共享页**正在被写**,字段可能处于中间状态。读者一旦发现版本号是奇数(或读到一半版本号变了),就丢弃这次结果、重来。

这套机制为什么 sound,后面技巧精解里拆。先看内核侧怎么实现写者。

### 内核侧写者:`update_vsyscall`

每个 tick(或 `settimeofday`、`adjtimex`)更新墙上时间时,内核会顺带把共享页刷新一遍。完整的调用链是这样的:

```
 tick / settimeofday / adjtimex
   │
   ▼
 update_wall_time()                      [timekeeping.c:2230]
   └─ timekeeping_advance(TK_ADV_TICK)   [timekeeping.c:2151]
        └─ timekeeping_update(tk, ...)   [timekeeping.c:757]
             ├─ update_vsyscall(tk)      [vsyscall.c:72]   ← 写共享页
             └─ ...其他更新
```

最关键的就是 [`update_vsyscall`](../linux/kernel/time/vsyscall.c#L72-L121),它干三件事:**开头 `vdso_write_begin` 把 seq 置奇,中间把 timekeeper 里的字段拷进 `struct vdso_data`,结尾 `vdso_write_end` 把 seq 置偶**。下面贴真实源码(节选自 [vsyscall.c:72-121](../linux/kernel/time/vsyscall.c#L72-L121)):

```c
void update_vsyscall(struct timekeeper *tk)
{
	struct vdso_data *vdata = __arch_get_k_vdso_data();   // 拿到共享页指针
	struct vdso_timestamp *vdso_ts;
	s32 clock_mode;
	u64 nsec;

	/* copy vsyscall data */
	vdso_write_begin(vdata);                              // ① seq 置奇(+smp_wmb)

	clock_mode = tk->tkr_mono.clock->vdso_clock_mode;
	vdata[CS_HRES_COARSE].clock_mode	= clock_mode;
	vdata[CS_RAW].clock_mode		= clock_mode;

	/* CLOCK_REALTIME also required for time() */
	vdso_ts		= &vdata[CS_HRES_COARSE].basetime[CLOCK_REALTIME];
	vdso_ts->sec	= tk->xtime_sec;                       // ② 写各个基准时间字段
	vdso_ts->nsec	= tk->tkr_mono.xtime_nsec;

	/* CLOCK_REALTIME_COARSE */
	vdso_ts		= &vdata[CS_HRES_COARSE].basetime[CLOCK_REALTIME_COARSE];
	vdso_ts->sec	= tk->xtime_sec;
	vdso_ts->nsec	= tk->tkr_mono.xtime_nsec >> tk->tkr_mono.shift;

	/* ... CLOCK_MONOTONIC_COARSE / hrtimer_res / update_vdso_data 等略 ... */

	if (clock_mode != VDSO_CLOCKMODE_NONE)
		update_vdso_data(vdata, tk);                  // 高精度字段(CLOCK_MONOTONIC 等)

	__arch_update_vsyscall(vdata, tk);

	vdso_write_end(vdata);                               // ③ seq 置偶(+smp_wmb)

	__arch_sync_vdso_data(vdata);
}
```

三个动作一字排开:`vdso_write_begin` → 写一堆字段 → `vdso_write_end`。`vdso_write_begin`/`vdso_write_end` 是 VDSO 通用辅助函数(定义在内核源码 `include/vdso/helpers.h`,跨 arch 共享,本地 sparse 树未拉),它们的语义就是 seqcount 的 `raw_write_seqcount_begin`/`end`:

- `vdso_write_begin(vdata)`:先 `smp_wmb()`(保证之前的读者看到本 CPU 上更早的写都已可见),然后把 `vdata->seq` 加 1(变奇数),再 `smp_wmb()`(保证读者先看到 seq 变奇、再看到随后的字段改)。
- `vdso_write_end(vdata)`:先 `smp_wmb()`(保证字段改都可见),然后把 `vdata->seq` 加 1(变偶数)。

注意:**写者自己是有锁的**——`update_vsyscall` 的调用者 `timekeeping_update` 持有 `timekeeper_lock`([timekeeping.c:2160](../linux/kernel/time/timekeeping.c#L2160) `raw_spin_lock_irqsave(&timekeeper_lock, flags)`),而且写者只有一个(就是更新墙上时间的那个 tick 上下文)。seqlock 的前提就是**写者互斥**(写者之间不能并发),它解决的是"写者 vs 读者"的并发,不是"写者 vs 写者"。这正好符合 VDSO 的场景:写者(内核 timekeeper)独占,读者(所有用户线程)海量。

### 用户侧读者:循环直到版本号稳定

用户态函数的读者循环前面贴过了,这里拆它的正确性。读者做的事情:

1. `seq1 = vdata->seq`,跟一个 `smp_rmb`(读内存屏障)。
2. 读 `basetime`/`mult`/`shift`/`cycle_last` 等所有需要的字段。
3. `smp_rmb`。
4. `seq2 = vdata->seq`。
5. 如果 `seq1` 是奇数,或 `seq1 != seq2`,说明读的过程中写者动过(或刚开始读时写者就在写),**丢弃结果、回到第 1 步重来**;否则用读到的字段。

> **钉死这件事**:读者侧**没有任何阻塞、没有任何系统调用**。它只是一个 `do-while` 循环,最坏情况(恰好每次都撞上写者)重试几次,但写者每秒才出现上千次(每 tick 一次),读者撞上的概率极低,绝大多数情况下一次读完版本号没变、直接用。这正是 seqlock 的精髓——**用"偶尔重试"换"永远不阻塞"**,在读多写少(读写比 1000:1 甚至更高)的场景下,重试的期望次数趋近于 1。

读者侧的源码在 VDSO 通用代码里(`lib/vdso/` 下的 `gettimeofday.c`,本地 sparse 树未拉,但逻辑就是上面这段),核心是一个宏 `__iter_div_u64_rem` 和读者循环。arch 侧的 `__arch_get_k_vdso_data()` 负责把"当前进程的共享页地址"给出来——这个地址在 `execve` 时就映射好了,是个固定偏移,取地址本身就是一两条指令。

---

## 10.5 ★对照:VDSO ↔ Tokio `Instant::now` / Go `time.Now`

VDSO 不是 Linux 独有的思路,用户态运行时也在做"把高频只读数据搬到读方就近"的事:

- **Tokio 的 `Instant::now()`**:底层走标准库的 `std::time::Instant`,在 Linux 上**直接调 VDSO**(`clock_gettime(CLOCK_MONOTONIC)`),所以 Tokio 里 `Instant::now()` 是纳秒级、几乎零开销的——它压根没进内核。Tokio 的时间轮(回扣第 14 章)在判断 timer 是否到期、计算下一个最近 timer 时,频繁调 `Instant::now()`,正是依赖 VDSO 把这次调用变成了一次内存读。
- **Go runtime 的 `time.Now()`**:Go 在 Linux 上也走 VDSO,但 Go runtime 还做了另一层——它在自己调度器里缓存了"当前 P 的时钟视图",某些路径连 VDSO 都省了。思路同源:**高频只读数据,搬到离 CPU 最近的地方**。
- **对照要点**:VDSO 是**内核把数据搬到用户进程**;Tokio/Go 是**运行时把数据搬到 worker 线程的本地缓存**。两者都是"用数据布局消灭调用开销",区别只在于边界——VDSO 跨用户/内核边界,Tokio/Go 跨运行时/应用边界。本书后面第 21 章会给一张总表,把这层"数据就近"的共性钉死。

---

## 10.6 技巧精解:seqlock 奇偶版本号,为什么 sound

这一节拆本章最硬核的技巧:**为什么 seqlock 的奇偶版本号机制能保证"读者无锁、又读到一致"**。这是 VDSO 整套设计成立的关键,也是 seqlock 这个原语在内核里(timekeeping、dpath、`ktimet`、vfs 路径查找)反复出现的根因。

### 朴素写法的两个陷阱

先看朴素写法会撞什么墙。如果不用 seqlock,有两条朴素的路线:

**朴素路线一:读者拿锁**。给共享页配一把用户态/内核共享的自旋锁(比如基于共享内存的 futex)。问题:① 用户态实现跨进程自旋锁极难(需要原子操作 + 共享内存 + 内存序全对);② 读者要自旋等写者,而写者每 tick 来一次、持有锁期间要做不少字段拷贝,海量读者自旋会把 CPU 烧光;③ 锁本身是共享变量,多核同时争抢引发缓存行乒乓,反而把读时间的性能打到比进内核还差。

**朴素路线二:双缓冲(double buffering)**。准备两份 `vdso_data`,写者写"后台那份",写完原子地切个指针,读者永远读"前台那份"。问题:① 切指针的瞬间,正在读"旧前台"的读者读到的是上一拍的数据——对读时间来说误差一拍(最多一个 tick,1ms)勉强能接受,但**写者切指针和读者读指针之间仍需内存序保证**,否则读者可能看到指针已切但数据没刷过去的中间态;② 双缓冲要 2 倍内存(对 vdso_data 这种小结构无所谓,但思路不通用);③ 仍然没解决"读者怎么知道读的是哪一拍、是否该重读"的问题。

seqlock 比这两条都妙的地方在于:**它用版本号让读者自己判断"读到的这一份是不是完整的",不需要锁、不需要双缓冲、不需要切指针**。

### 奇偶机制的正确性,逐条拆

把读写时序展开,逐条验证 seqlock 为什么 sound。设版本号初始为 0(偶数):

```
 时间轴    写者(持 timekeeper_lock)        读者 R(不持锁)
 ───────  ──────────────────────────       ─────────────────────
  t0       seq=0(偶,稳定)
                                              读 seq1=0
                                              读字段(读到 v0)
                                              读 seq2=0
                                              seq1==seq2 且偶 → 用 v0 ✓

  t1       vdso_write_begin:
            smp_wmb
            seq=1(奇,开始写)
            smp_wmb

  t2                                          读 seq1=1(奇!)
                                              → 立即知道"正在写",不读字段,重试

  t3       写 basetime.sec = new_sec
  t4       写 basetime.nsec = new_nsec
  t5       ...

  t6                                          读 seq1=2(读到的瞬间 t6 在 t7 之后)
                                              等等——t6 时刻 seq 还是 1,见下文分析

  t7       vdso_write_end:
            smp_wmb
            seq=2(偶,写完)
            smp_wmb

  t8                                          读 seq1=2(偶)
                                             读字段(读到 v_new)
                                             读 seq2=2
                                             用 v_new ✓
```

关键的"夹缝"情况:**读者恰好在写者写的过程中开始读**。假设读者在 t3 和 t4 之间开始(t2.5):

- 读 `seq1` → 此时是 1(奇),读者**立刻知道正在写**,跳过字段读取,直接重试。这是 seqlock 的第一道防线:**奇数版本号 = 写者持有,读者立刻退避**。
- 但读者怎么保证读到 `seq1` 之前,之前那次"seq 还是偶数"的判断不误导它?这就靠 `vdso_write_begin` 里的 **`smp_wmb`(写内存屏障)**:写者先把 seq 置奇、再改字段;读者先读 seq、再读字段。在 x86 这样强序的架构上内存序天然满足,但 seqlock 是跨架构的通用原语,必须有屏障兜底。

另一类夹缝:**读者在写者写完之前已经读完字段,但读到的是写了一半的**。假设读者在 t0.5 开始(写者还没来):

- 读 `seq1` = 0(偶),读字段(读到 v0),读 `seq2`——如果在这中间写者把 seq 改成 1 又改回 2(写了完整一拍),读者读到的 `seq2` = 2,`seq1 == seq2` 都是偶,但**字段是 v0 和 v_new 混着的!**

这是个真问题。seqlock 靠什么堵住它?**靠"读者在读完字段后必须重新读 seq,且 seq 必须和开头一致"**。如果写者在读者读字段的过程中写过(哪怕写完了),seq 会从 0 → 1 → 2,**变化了**,读者读到的 `seq2 = 2 ≠ seq1 = 0`,**重试**。这就是 seqlock 的第二道防线:**版本号变化 = 中途被写过,读者丢弃结果**。

那会不会有一种情况:写者把 seq 从 0 改成 1 又改回 0(版本号回绕,碰巧回到原值),骗过读者?这靠**版本号只增不减、且只在写者持有锁时才变**:写者进临界区必加 1(变奇),出临界区必再加 1(变偶),一次完整写让 seq 增加 2,绝不会"改回原值"。版本号回绕只在 `2^32` 次写之后(每 tick 一次大约 50 天),且即使回绕,因为写者持锁、读者重试,也不会有读者恰好踩在"回绕瞬间且 seq1==seq2"的窗口里——这个窗口在数学上存在但工程上可以忽略,内核代码注释里也认可这点。

### 三道屏障为什么必须

把这套机制的内存序拆开,有**三对屏障配对**:

1. **写者 `vdso_write_begin` 内的 `smp_wmb`(seq 置奇之前)**:保证"之前那次写的字段全可见"在"seq 变奇"之前。作用:防止读者看到 seq 已经变奇、但前一次写的字段还没刷出去。
2. **写者 `vdso_write_end` 内的 `smp_wmb`(seq 置偶之前)**:保证"本次写的字段全可见"在"seq 变偶"之前。这是最关键的一道——它保证读者一旦看到偶数 seq,本次的字段改动一定已经可见。
3. **读者侧的两次 `smp_rmb`**(读 seq 之后、读字段之前;读字段之后、读 seq 之前):和写者的 `smp_wmb` 配对,保证读者读字段的操作"夹在"两次读 seq 之间,不会被 CPU 或编译器重排到 seq 读取之外。

在 x86(强序模型,TSO)上,这些屏障大部分是空操作(x86 天然不允许"写-写"和"读-读"重排,`smp_wmb`/`smp_rmb` 都编译成空指令),但 seqlock 是**跨架构原语**,在 ARM/PowerPC/RISC-V 这样弱序的架构上,这些屏障是实打实的 `dmb ish`/`sync` 指令,缺一个就会读到撕裂的字段。

> **反面对比**:如果**省掉 `vdso_write_end` 里那道 `smp_wmb`**(假设写者写完字段直接 `seq++`),在弱序架构上,读者可能先看到 seq 变偶、再看到字段更新,于是"读到偶数 seq、但字段还是旧的",算出的时间就错了。这就是为什么 seqlock 的实现里写者**必须**在改完字段、加 seq 之间插一道写屏障——它不是性能开销(强序架构上是空操作),而是**正确性的支柱**。

### 把 seqlock 的精髓压成一句

> seqlock 用一个只增的版本号,让读者靠"读到奇数/读到不一致就重试"**自己判一致性**,从而**不需要任何阻塞或锁**。它的代价是读者偶尔重试(读多写少时趋近于 0 次重试),收益是读者路径上零锁开销、零系统调用。这是"用重试换无锁"的典范,和乐观锁(CAS)同源,但比 CAS 更适合"多字段原子读"——CAS 只能保护单个字,seqlock 靠版本号保护一整片字段。

---

## 10.7 反面对比:朴素地每次进内核会怎样

把 VDSO 的价值量化一下,反面对比才显形。假设一个 Web 服务,8 核机器,每核每秒处理 1 万个请求,每个请求平均读时间 10 次(打日志、限流、缓存 TTL、耗时统计),也就是每核每秒 10 万次 `clock_gettime`,全机每秒 80 万次。

- **走真系统调用**(假设每次 150ns):80 万 × 150ns = **120ms/秒 的 CPU 时间**耗在读时间上,约占一个核 12%,或全机 1.5%。看起来不算夸张?但这只是"纯读时间"的开销,还没算锁竞争。
- **锁竞争放大**:墙上时间是全局唯一的,内核侧 `gettimeofday` 的实现要走 `tk->seq`(timekeeper 自己也用 seqlock),多核同时进内核读时间会在 timekeeper 的 seqlock 上产生缓存行乒乓,真实开销远超单次 150ns 的线性叠加。64 核机器上,这个放大能把"读时间"的总开销推到**全机 5%~10% 的 CPU**。
- **VDSO 路径**:每次读时间退化成"读 seq → 读几个字段 → 算一下 → 读 seq → 比较"的大约 20~30 条用户态指令,大约 **20~40ns**,无锁、无系统调用、无缓存行跨核(共享页是只读映射,读者只读不改,缓存行稳定停在每个核的 L1)。80 万 × 30ns = 24ms/秒,**分散在各核上各占 0.3%**,可忽略。

差距大约 **5~10 倍**,而且核数越多差距越大(VDSO 路径随核数线性扩展,真系统调用因锁竞争而亚线性)。这就是为什么 VDSO 不是"锦上添花",而是"高并发服务的必需品"——没有它,64 核机器上光读时间就能吃掉一个核。

> **钉死这件事**:VDSO 的收益不是"省了一次函数调用",而是"省了一次特权级切换 + 一把跨核锁"。它把"读时间"从**内核全局共享数据上的同步操作**,变成**用户进程私有地址空间里的纯只读操作**。这正是 per-CPU 无锁化思路的另一种体现——把高频只读数据,搬到读方的私有视图里。

---

## 10.8 VDSO 的边界:什么时候它不灵

VDSO 不是万能的,它有几条边界,知道这些边界才算真懂:

1. **时钟源不支持时 fallback**。如果当前 `clocksource` 不是 TSC/PVCLOCK/HVCLOCK(比如被切成了 HPET,某些老平台或虚拟化场景),`clock_mode == VDSO_CLOCKMODE_NONE`,VDSO 函数会老实进内核走真系统调用。这就是为什么"切时钟源"有时会让性能掉一截。
2. **粗精度时钟(CLOCK_REALTIME_COARSE)不需要读 TSC**,直接读 `basetime[CLOCK_REALTIME_COARSE]` 就行,但它只精确到 tick(1ms 量级),更快但更糙。需要纳秒精度的必须读 TSC。
3. **不是所有时钟都能 VDSO**。`CLOCK_PROCESS_CPUTIME_ID`/`CLOCK_THREAD_CPUTIME_ID`(CPU 时间消耗)没法 VDSO——它依赖调度器统计,得进内核。VDSO 只覆盖了"墙上时间/单调时间"这一类。
4. **数据更新有延迟**。共享页是每个 tick 更新一次,NOHZ idle 状态下 tick 停了,共享页就不再刷新——但这没关系,因为 idle CPU 上的进程也都在睡眠,没人读时间;等 CPU 被唤醒(下一个 hrtimer),tick 恢复,共享页也恢复刷新。这呼应第 15 章 NOHZ。

---

## 章末小结

这一章是第 2 篇"系统调用"的反面高潮:前两章讲"怎么合法进内核",这一章讲"**怎么让一次系统调用压根不发生**"。VDSO 的本质,是把"读时间"这种**只读、高频、内核独占维护**的数据,搬到一块**内核维护、用户态映射**的共享页里,让用户态函数直接读、不算系统调用。读者侧靠 seqlock 的奇偶版本号避开"内核正在写"的中间态,无锁又一致。

回到全书二分法:VDSO 服务的是"**进内核**"这一面,但它的角色是**减少进内核的次数**——它不是"又一种进内核的方式",而是"避免进内核"的优化。它和第 8 章的 `SYSCALL` 快路径、第 9 章的 `copy_from_user` fixup 一起,构成"用户态合法进内核"这条线上的性能闭环:`SYSCALL` 让"不得不进的"尽量快,`copy_from_user` 让"进了之后的"尽量安全,VDSO 让"能不进的"干脆别进。

### 五个"为什么"清单

1. **为什么偏偏是 `gettimeofday`/`clock_gettime` 被 VDSO 化,而不是 `read`/`write`?** 因为它**只读**(不改内核状态)、**超高频**(热点路径每秒百万次)、**结果由内核独占维护**(放在固定位置谁读都一样)。这三条同时满足,才有"搬到用户态就近读"的空间。`read`/`write` 要改状态、要现场算,搬不动。
2. **用户态读共享页时,内核正在写,这中间的并发怎么解决?** seqlock 的奇偶版本号:内核写时 seq 先变奇(正在写)、写完变偶(稳定);用户态读到奇数或读到一半版本号变了就重试,读完整一致才用。无锁(用户态拿不到内核锁)、又一致(永远不读中间态)。
3. **为什么读者不需要任何锁?** 因为 seqlock 用"重试"换"无锁"——读者只读版本号和字段,绝不阻塞;写者每 tick 才来一次,读者撞上的概率极低,期望重试次数趋近于 1。这适合"读多写少"的场景。
4. **为什么 seqlock 要那么严格的内存序屏障?** 因为它是跨架构原语。在弱序架构(ARM/PowerPC)上,如果省掉 `vdso_write_end` 里"字段写完→seq 置偶"之间的写屏障,读者可能看到 seq 变偶但字段还是旧的,读出错的时间。三对屏障是正确性支柱。
5. **VDSO 和 Tokio/Go 的 `Instant::now`/`time.Now` 什么关系?** 它们在 Linux 上**直接走 VDSO**(Tokio 经标准库、Go 经 runtime),所以都是"几乎零开销的内存读"。VDSO 是内核把数据搬到用户进程,Tokio/Go 是运行时把数据搬到 worker 本地——同一种"数据就近"思路在不同边界上的体现。

### 想继续深入往哪钻

- **源码**:本章核心是 [`kernel/time/vsyscall.c`](../linux/kernel/time/vsyscall.c) 的 `update_vsyscall`(L72)、`update_vdso_data`(L18)、`update_vsyscall_tz`(L123);写者路径上游是 [`kernel/time/timekeeping.c`](../linux/kernel/time/timekeeping.c) 的 `update_wall_time`(L2230)→ `timekeeping_advance`(L2151)→ `timekeeping_update`(L757)→ 在 L767 调 `update_vsyscall(tk)`。`struct timekeeper` 见 [`include/linux/timekeeper_internal.h`](../linux/include/linux/timekeeper_internal.h#L92)。`vdso_write_begin`/`vdso_read_begin` 等 seqlock 辅助函数在 `include/vdso/helpers.h`(跨 arch,本地 sparse 树未拉,可看在线 [elixir.bootlin.com](https://elixir.bootlin.com/linux/v6.9/source/include/vdso/helpers.h));用户态 VDSO 函数(`__vdso_clock_gettime` 等)在 `lib/vdso/gettimeofday.c`(跨 arch 共享)和 `arch/x86/entry/vdso/vclock_gettime.c`(arch 特定)。
- **观测**:`cat /proc/<pid>/maps | grep vdso` 看每个进程的 VDSO 映射段;`ldd` 一个动态链接程序有时也会打印 `linux-vdso.so.1`;`strace -e clock_gettime ./your_program` —— 如果 VDSO 生效,**你看不到任何 `clock_gettime` 系统调用**(它压根没进内核),这是验证 VDSO 工作的最直接方法;`perf stat -e raw_syscalls:sys_enter_clock_gettime` 同理,计数应该是 0(除非 fallback)。`sysctl` / `/sys/devices/system/clocksource/` 下能看当前 `clocksource`,切到非 TSC 的源会让 VDSO 失效、`strace` 里开始出现 `clock_gettime`。
- **延伸**:seqlock 在内核里到处都是——timekeeper 自己用它保护 `struct timekeeper` 的读、`dpath` 用它做路径查找、`ktimer` 用它。读 [`include/linux/seqlock.h`](../linux/include/linux/seqlock.h) 的注释(L630 起的 latch seqcount 那段尤其精彩),能把"奇偶版本号 + 双缓冲"的变体看透。再深一层,NOHZ idle 状态下 VDSO 共享页不更新(因为 tick 停了),这是第 15 章要讲的"停 tick 又不丢时间"的伏笔。

### 引出下一章

VDSO 让"读时间"这种最高频的系统调用避免进内核,这是第 2 篇"系统调用"的性能闭环。但还有一类需求:不是"让系统调用不发生",而是"**监控系统调用本身**"——安全沙箱要拦截危险系统调用、性能分析要观测每次系统调用的延迟和参数。这就是 seccomp(BPF 在系统调用入口前过滤)和 ftrace/trace_events(观测系统调用旅程)。下一章我们讲第 2 篇的收尾:**系统调用追踪**——看 seccomp 怎么在系统调用真正执行**之前**就把它毙掉,ftrace 怎么把一次系统调用的全程摊开给你看。
