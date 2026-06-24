# 第二章 · nsproxy:namespace 的总入口

> 篇:P1 namespace 视图隔离
> 主线呼应:上一章结尾我们立起"容器 = namespace 改视图 + cgroup 限资源"这条主线,并把 `task_struct->nsproxy` 这组指针点了出来。但留了一个核心问题没拆:fork 时内核到底怎么决定子进程是继承父亲的 nsproxy,还是造一组新的?一次 `clone(CLONE_NEWNS | CLONE_NEWPID | CLONE_NEWNET | ...)` 同时要六七种新 ns,内核怎么保证"要么全造出来、要么一个都不造"——绝不会留下一个"半新半旧"的进程?`setns` 进入别人容器的瞬间,进程的视图怎么安全切换,才不会在切到一半时被别的 CPU 看到?这一章钻进 `kernel/nsproxy.c`,把 namespace 的总入口彻底拆透。

## 核心问题

**`task_struct->nsproxy` 这一个指针凭什么能聚合 7 种命名空间?fork 时 `copy_namespaces` 怎么读 `CLONE_NEW*` 决定共享还是复制?`create_new_namespaces` 怎么做到"全成或全回滚",中间任何一步失败都不会留下半新半旧的进程?运行时切换视图的 `switch_task_namespaces` 为什么只锁 `task_lock` 而不用全局锁,这为什么 sound?**

读完本章你会明白:

1. `nsproxy` 是**一层聚合**:7 个 ns 指针收进一个结构体,`task_struct` 只持一个指针——这让"换视图"等于"换指针",是容器比 VM 轻一个数量级的根。
2. `CLONE_NEW*` 是**一组标志位**:fork 时用 `flags & (CLONE_NEWNS | CLONE_NEWUTS | ...)` 一次性表达"要哪些新 ns",一次 clone 就能切多种视图,而不是 7 次系统调用。
3. `create_new_namespaces` 用**构造失败回滚链**(goto out_xxx 反序 put)保证原子性:进程的 nsproxy 要么全换成新的,要么一个都不换,绝不会"新 mnt + 旧 pid"。
4. `switch_task_namespaces` 切视图只用 `task_lock`(任务自己的 `alloc_lock`),不是全局锁——视图切换的并发性靠"每个任务独立持自己的 nsproxy 指针"。
5. 各 ns 共享同一套 `copy_*_ns` 范式(检查标志位→共享则 get,要新则 clone)+ `struct ns_common` 多态表(`proc_ns_operations`),这是 namespace 子系统能像"插件"一样可扩展的骨架。

> **逃生阀**:本章代码会反复出现 `flags & CLONE_NEW*` 和 `goto out_xxx` 两条模式。抓住这两条就抓住了 80%:前者是"标志位驱动",后者是"原子回滚"。读不懂某段细节时,先问"这段是在判断要不要造新 ns,还是在回滚已造的"。

---

## 2.1 一句话点破

> **nsproxy 是 namespace 的总入口——7 个 ns 指针收进一个结构体,task_struct 只认这一个指针;fork 时一个 `CLONE_NEW*` 标志位掩码驱动 `create_new_namespaces` 把它们要么全造、要么一个不造,运行时 `switch_task_namespaces` 只换这一个指针就换了进程的整个视图。换指针而非换数据,是 namespace 这层全部的精妙。**

这是结论,不是理由。本章倒过来拆:先看为什么"7 个 ns 指针要聚合而不是散落在 task_struct 各处",再看 `copy_namespaces` 在 fork 路径里怎么读 `CLONE_NEW*`、`create_new_namespaces` 怎么用回滚链保证原子性,然后钻进 `switch_task_namespaces` 看运行时切视图为什么只用 `task_lock`,最后拆"7 种 ns 共用同一套 `copy_*_ns` 范式 + `proc_ns_operations` 多态"这张骨架怎么撑起后面 6 章。

---

## 2.2 为什么是 nsproxy 聚合:7 个指针收进一个结构体

Linux 有 7 种命名空间(mnt/pid/net/uts/ipc/user/cgroup,加上 5.6 引入的 time,严格说是 8 种,但 user ns 不在 nsproxy 里,后面讲),它们各自管一种视图:

| 命名空间 | 管什么视图 | 结构体 | 关键文件 |
|---------|-----------|--------|---------|
| mnt ns | 挂载树/根文件系统 | `struct mnt_namespace` | `fs/namespace.c` |
| pid ns | 进程号(PID 1) | `struct pid_namespace` | `kernel/pid_namespace.c` |
| net ns | 网卡/路由/iptables/socket | `struct net` | `net/core/net_namespace.c` |
| uts ns | hostname/domainname | `struct new_utsname` | `kernel/utsname.c` |
| ipc ns | SysV IPC/POSIX 消息队列 | `struct ipc_namespace` | `ipc/namespace.c` |
| cgroup ns | cgroup 路径视图 | `struct cgroup_namespace` | `kernel/cgroup/namespace.c` |
| time ns | 单调时钟偏移 | `struct time_namespace` | `kernel/time/namespace.c` |
| user ns | uid 映射(不在 nsproxy!) | `struct user_namespace` | `kernel/user_namespace.c` |

一个进程要"看起来独占整机",得同时切换其中多种视图——容器启动时通常要同时换 6~7 种。这 7 种指针放哪?朴素的写法是散落在 `task_struct` 各处:

```c
/* 朴素的、糟糕的写法(示意,非源码) */
struct task_struct {
    struct mnt_namespace  *mnt_ns;
    struct uts_namespace  *uts_ns;
    struct ipc_namespace  *ipc_ns;
    struct pid_namespace  *pid_ns;
    struct net            *net_ns;
    ...
};
```

> **不这样会怎样**:如果每种 ns 指针散落在 task_struct 各处,① 每次切换视图要改 task_struct 的 7 个字段,中间任何一刻被别的 CPU 读到,就是"半新半旧"——读到新 mnt + 旧 pid,根文件系统换了但进程表没换,行为不可预测;② fork 时无法用"一个标志位掩码"原子表达"要哪些新 ns",得逐个判断、逐个复制、逐个挂靠,7 段重复逻辑;③ 引用计数没法集中管理——共享 nsproxy 的多个任务得共享同一份引用计数,散落字段做不到。

Linux 的做法是**加一层聚合**:`task_struct` 只持一个 [`struct nsproxy *`](../linux/include/linux/sched.h#L1110) 指针(sched.h L1110),7 种 ns 指针全收进 [`struct nsproxy`](../linux/include/linux/nsproxy.h#L32-L42):

```c
/* include/linux/sched.h:1109-1110(简化) */
struct task_struct {
    /* Namespaces: */
    struct nsproxy *nsproxy;
    ...
};
```

```c
/* include/linux/nsproxy.h:32-42 */
struct nsproxy {
    refcount_t count;                          /* 共享此 nsproxy 的任务数 */
    struct uts_namespace     *uts_ns;          /* hostname 视图 */
    struct ipc_namespace     *ipc_ns;          /* SysV IPC 视图 */
    struct mnt_namespace     *mnt_ns;          /* 挂载视图(根文件系统) */
    struct pid_namespace     *pid_ns_for_children;  /* 子进程的 pid ns(自己用 active) */
    struct net               *net_ns;          /* 网络栈视图 */
    struct time_namespace    *time_ns;         /* 时间视图(本进程) */
    struct time_namespace    *time_ns_for_children; /* 子进程的时间 ns */
    struct cgroup_namespace  *cgroup_ns;       /* cgroup 路径视图 */
};
```

([nsproxy.h:32](../linux/include/linux/nsproxy.h#L32))

```
 task_struct(sched.h L1110):
 └─ nsproxy ──► struct nsproxy(nsproxy.h L32)
                ├─ count           (refcount_t,共享此 nsproxy 的任务数)
                ├─ uts_ns     ───► struct uts_namespace       (hostname)
                ├─ ipc_ns     ───► struct ipc_namespace       (SysV IPC)
                ├─ mnt_ns     ───► struct mnt_namespace       (挂载树)
                ├─ pid_ns_for_children ─► struct pid_namespace(子进程的 pid ns)
                ├─ net_ns     ───► struct net                 (网络栈)
                ├─ time_ns / time_ns_for_children ─► time ns
                └─ cgroup_ns ───► struct cgroup_namespace     (cgroup 路径)
```

注意 nsproxy.h 头部的注释——它把 nsproxy 的语义钉死了三件事:

```
* 'count' is the number of tasks holding a reference.
* The nsproxy is shared by tasks which share all namespaces.
* As soon as a single namespace is cloned or unshared, the nsproxy is copied.
```

([nsproxy.h:24-30](../linux/include/linux/nsproxy.h#L24-L30))

翻译过来:① `count` 是**任务数**(不是 nsproxy 数);② **共享全部 ns 的任务共享同一个 nsproxy**(通过 `get_nsproxy` 把 count 加 1);③ **任何一种 ns 要 clone/unshare,整个 nsproxy 都得复制一份**(COW 思想——只要有一个字段要变,就 copy 整个结构,把要变的那个换成新的,其余指向原来的 ns)。

这是 nsproxy 聚合的核心收益:

- **原子切换**:换视图 = 换一个 `task_struct->nsproxy` 指针,一行赋值(后面讲 `switch_task_namespaces` 时细看)。
- **引用计数集中**:`refcount_t count` 一个计数器管"多少任务共享这组 ns",`get_nsproxy`/`put_nsproxy` 一次 inc/dec 就完成全部 7 种 ns 的引用计数(因为每种 ns 自己也有 refcount,但 nsproxy 持它们的引用)。
- **COW 友好**:fork 时如果不要新 ns,子进程直接 `get_nsproxy(old_ns)` 共享父亲的 nsproxy,count 加 1——零拷贝;只要有一种要新 ns,才走 `create_new_namespaces` 复制。

> **钉死这件事**:nsproxy 是 namespace 这层的**唯一入口**。`task_struct` 不直接持有 7 种 ns,只持一个 `struct nsproxy *`。换视图 = 换这一个指针。共享全部 ns 的任务共享同一个 nsproxy(refcount 多个);任何一个 ns 要变,整个 nsproxy 复制一份。这种"用一层聚合换原子性 + 引用计数集中"的思路,和上一本《内存分配器》里 `css_set` 把 15 个 css 指针收进一个结构体是完全同构的设计。

### 一个反直觉:pid_ns_for_children 而非 pid_ns

nsproxy 里的字段名有个反直觉的细节:`pid_ns_for_children`——不是 `pid_ns`。nsproxy.h 的注释把原因钉死了:

```
* The pid namespace is an exception -- it's accessed using
* task_active_pid_ns.  The pid namespace here is the
* namespace that children will use.
```

([nsproxy.h:20-22](../linux/include/linux/nsproxy.h#L20-L22))

**一个进程自己的 pid ns(它被创建时所在的那个 ns)是它"身份"的一部分**,存在 `task->active_pid_ns`(由 `struct pid` 的 `numbers[]` 数组隐含,第 4 章详讲);而 nsproxy 里的 `pid_ns_for_children` 是**它以后 fork 出的子进程会进的 pid ns**。这两者通常相等(一个普通 fork 不会换 pid ns),但在 `CLONE_NEWPID` 之后会分叉:本进程仍活在原来的 pid ns 里(它的 PID 没变),但它再 fork 子进程,子进程就进新的 pid ns(成为容器里的 PID 1)。这就是为什么 `clone(CLONE_NEWPID)` 出来的子进程才是容器里的 PID 1,而不是调用 clone 的父亲——pid ns 的切换是"下一代生效"。

> **钉死这件事**:pid ns 是 7 种 ns 里最反直觉的一种,因为它"切换延迟一代"。其他 6 种 ns 切换立即生效(换了 mnt_ns 立刻看到新挂载树),唯独 pid ns 的切换只对子进程生效——这也是为什么容器里 PID 1 总是 runc fork 出来的那个子进程,而不是 runc 本身(第 4 章详讲 `pid->numbers[]` 多层 PID 怎么实现这个"延迟生效")。

---

## 2.3 fork 路径:copy_namespaces 读 CLONE_NEW* 决定共享还是复制

nsproxy 聚合好了,下一个问题是:**fork 时子进程的 nsproxy 怎么来?** 答案在 [`copy_namespaces`](../linux/kernel/nsproxy.c#L151-L188)([nsproxy.c:151](../linux/kernel/nsproxy.c#L151))——它在 `kernel/fork.c` 的 `copy_process` 里被调用:

```c
/* kernel/fork.c:2393(fork.c 里 copy_process 的一步) */
retval = copy_namespaces(clone_flags, p);
if (retval)
    goto bad_fork_cleanup_mm;
...
bad_fork_cleanup_namespaces:
    exit_task_namespaces(p);
```

([fork.c:2393](../linux/kernel/fork.c#L2393)、[fork.c:2643-2644](../linux/kernel/fork.c#L2643-L2644))

注意这个调用点位于 fork 的**资源复制阶段**——mm/fs/files/sighand 都在复制,namespaces 也是其中一项。失败就 `goto bad_fork_cleanup_namespaces` 走 `exit_task_namespaces`(就是 `switch_task_namespaces(p, NULL)`,把 nsproxy 置 NULL,等任务彻底释放)。这一对点构成了 fork 失败时的回滚闭环。

`copy_namespaces` 本体只有 30 多行,但把"共享 vs 复制"的判定逻辑讲得极其干净:

```c
/* kernel/nsproxy.c:151-188 */
int copy_namespaces(unsigned long flags, struct task_struct *tsk)
{
    struct nsproxy *old_ns = tsk->nsproxy;
    struct user_namespace *user_ns = task_cred_xxx(tsk, user_ns);
    struct nsproxy *new_ns;

    if (likely(!(flags & (CLONE_NEWNS | CLONE_NEWUTS | CLONE_NEWIPC |
                          CLONE_NEWPID | CLONE_NEWNET |
                          CLONE_NEWCGROUP | CLONE_NEWTIME)))) {
        /* 情形 A:不要任何新 ns */
        if ((flags & CLONE_VM) ||
            likely(old_ns->time_ns_for_children == old_ns->time_ns)) {
            get_nsproxy(old_ns);   /* 共享父亲的 nsproxy,count+1 */
            return 0;
        }
    } else if (!ns_capable(user_ns, CAP_SYS_ADMIN))
        return -EPERM;   /* 要新 ns 必须有 CAP_SYS_ADMIN */

    /* CLONE_NEWIPC 与 CLONE_SYSVSEM 互斥(语义冲突) */
    if ((flags & (CLONE_NEWIPC | CLONE_SYSVSEM)) ==
        (CLONE_NEWIPC | CLONE_SYSVSEM))
        return -EINVAL;

    /* 情形 B:有 CLONE_NEW*,走 create_new_namespaces */
    new_ns = create_new_namespaces(flags, tsk, user_ns, tsk->fs);
    if (IS_ERR(new_ns))
        return PTR_ERR(new_ns);

    /* time ns 特殊:fork 时把 time_ns_for_children 提升为 time_ns */
    if ((flags & CLONE_VM) == 0)
        timens_on_fork(new_ns, tsk);

    tsk->nsproxy = new_ns;   /* 一行赋值,原子挂上 */
    return 0;
}
```

([nsproxy.c:151-188](../linux/kernel/nsproxy.c#L151-L188))

这段代码可以拆成三个判定层:

**判定一(共享快路,L157-164)**:`if (likely(!(flags & (CLONE_NEWNS | ...))))`——这是**最常见**的情形(普通 fork,不要任何新 ns)。这里 `likely` 提示编译器:绝大多数 fork 都走这里。它做的事极简——`get_nsproxy(old_ns)` 把父亲的 nsproxy 引用计数加 1,子进程直接共享。**零拷贝、零分配**。这解释了一个朴素疑问:"为什么 Linux fork 这么快?"——因为绝大多数 fork 根本不碰 namespace,只是 refcount++。

里面还藏了一个小分支:`time_ns_for_children == old_ns->time_ns` 的判断。time ns 也有"代际延迟"特性(类似 pid ns),如果父亲设过 `time_ns_for_children` 但还没让子进程"提升",fork 时要单独处理。这个细节第 6 章(uts ns)/time ns 那章不展开,这里只要知道:**time ns 是少数 fork 时不走 create_new_namespaces 也要单独处理的特例**。

**判定二(权限检查,L165-166)**:如果要任何 `CLONE_NEW*`,必须 `ns_capable(user_ns, CAP_SYS_ADMIN)`——即在目标 user ns 里有 `CAP_SYS_ADMIN`。这是 namespace 创建的权限闸门。注意是 `ns_capable` 不是 `capable`——前者在指定 user ns 里检查(配合 user ns 让容器里 root 也能创 ns),后者在 init user ns 里检查(全机 root 才行)。第 8 章 user ns 详讲这个区别为什么是容器安全的基石。

**判定三(互斥约束,L175-177)**:`CLONE_NEWIPC | CLONE_SYSVSEM` 不能同时要。原因写在注释里——`CLONE_SYSVSEM` 表示"和父亲共享 SysV 信号量 undo list",而 `CLONE_NEWIPC` 表示"造一个新的 ipc ns",两者语义直接冲突(新 ipc ns 里那些信号量数组根本够不着父亲的 undo list)。这种"标志位之间的互斥约束"在 namespace 代码里很常见,读源码时遇到 `if ((flags & A) == (A|B)) return -EINVAL` 就要警觉——它在堵一个语义冲突。

> **不这样会怎样**:如果不用"标志位掩码一次性判定",而是每种 ns 单独写一段"要不要新 ns"的代码,会有 7 段几乎相同的逻辑——`if (flags & CLONE_NEWNS) { ... } if (flags & CLONE_NEWUTS) { ... } ...`。这不仅冗长,还破坏了"一次原子切换"的可能性(7 段独立代码之间没法共享回滚链)。Linux 用**一个掩码表达式** `flags & (CLONE_NEWNS | CLONE_NEWUTS | ...)` 一次性回答"要不要进 create_new_namespaces",把判定压缩成一行——这是标志位驱动的精髓:**用位运算把"多维度选择"压成一个数**。

### CLONE_NEW* 到底是哪 7 个位

这 7 个标志位定义在 [`include/uapi/linux/sched.h`](../linux/include/uapi/linux/sched.h#L20-L44):

```c
/* include/uapi/linux/sched.h:20-44 */
#define CLONE_NEWNS        0x00020000    /* New mount namespace group */
#define CLONE_NEWTIME      0x00000080    /* New time namespace */
#define CLONE_NEWCGROUP    0x02000000    /* New cgroup namespace */
#define CLONE_NEWUTS       0x04000000    /* New utsname namespace */
#define CLONE_NEWIPC       0x08000000    /* New ipc namespace */
#define CLONE_NEWUSER      0x10000000    /* New user namespace */
#define CLONE_NEWPID       0x20000000    /* New pid namespace */
#define CLONE_NEWNET       0x40000000    /* New network namespace */
```

([uapi/linux/sched.h:20-44](../linux/include/uapi/linux/sched.h#L20-L44))

注意 `CLONE_NEWUSER` 也在这个表里,但**它根本不在 nsproxy 里管理**——user ns 挂在 `task->cred->user_ns`(凭据里),不在 nsproxy 的 7 个指针里。为什么?

> **关键不对称**:user ns 管 uid 映射,而 uid 是**凭据(cred)的一部分**,不是"视图"——它是"我是谁",不是"我看到什么"。所以 user ns 走 cred 那条线(`task->cred->user_ns`),不走 nsproxy。这也解释了为什么 `CLONE_NEWUSER` 是唯一一个**不需要 `CAP_SYS_ADMIN`** 的 `CLONE_NEW*`(它有自己的创建权限模型,第 8 章详讲),以及为什么 unshare/setns 时 user ns 走 `commit_creds` 而不是 `switch_task_namespaces`。这是一个看似小、实则深的设计:**namespace 里 7 种的 6 种在 nsproxy,唯独 user ns 在 cred——因为它管的是"身份"不是"视图"**。

这 7 个位为什么是这些值?它们和 `CLONE_VM`/`CLONE_FILES`/`CLONE_SIGHAND` 等共享标志位共用一个 `flags` 参数(clone 的第一个参数)。低 8 位给传统共享(files/fs/sighand 等),`CLONE_NEWNS` 起开始给 namespace,基本是 0x02000000 起每两位翻一倍(0x02/0x04/0x08/0x10/0x20/0x40 百万位),正好排开。这种"一个 long 装下所有 clone 选项"的设计,是 clone 系统调用能"一次调用切多种视图"的根。

---

## 2.4 create_new_namespaces:全成或全回滚的构造艺术

走完判定二的 fork 进了 `create_new_namespaces`——这是 namespace 子系统里**最值得逐行读**的函数之一,因为它示范了内核经典的"构造失败回滚"模式。

```c
/* kernel/nsproxy.c:67-145 */
static struct nsproxy *create_new_namespaces(unsigned long flags,
    struct task_struct *tsk, struct user_namespace *user_ns,
    struct fs_struct *new_fs)
{
    struct nsproxy *new_nsp;
    int err;

    new_nsp = create_nsproxy();    /* 分配新 nsproxy,refcount 置 1 */
    if (!new_nsp)
        return ERR_PTR(-ENOMEM);

    new_nsp->mnt_ns = copy_mnt_ns(flags, tsk->nsproxy->mnt_ns, user_ns, new_fs);
    if (IS_ERR(new_nsp->mnt_ns)) {
        err = PTR_ERR(new_nsp->mnt_ns);
        goto out_ns;
    }

    new_nsp->uts_ns = copy_utsname(flags, user_ns, tsk->nsproxy->uts_ns);
    if (IS_ERR(new_nsp->uts_ns)) {
        err = PTR_ERR(new_nsp->uts_ns);
        goto out_uts;
    }

    new_nsp->ipc_ns = copy_ipcs(flags, user_ns, tsk->nsproxy->ipc_ns);
    if (IS_ERR(new_nsp->ipc_ns)) {
        err = PTR_ERR(new_nsp->ipc_ns);
        goto out_ipc;
    }

    new_nsp->pid_ns_for_children =
        copy_pid_ns(flags, user_ns, tsk->nsproxy->pid_ns_for_children);
    if (IS_ERR(new_nsp->pid_ns_for_children)) {
        err = PTR_ERR(new_nsp->pid_ns_for_children);
        goto out_pid;
    }

    new_nsp->cgroup_ns = copy_cgroup_ns(flags, user_ns,
                                        tsk->nsproxy->cgroup_ns);
    if (IS_ERR(new_nsp->cgroup_ns)) {
        err = PTR_ERR(new_nsp->cgroup_ns);
        goto out_cgroup;
    }

    new_nsp->net_ns = copy_net_ns(flags, user_ns, tsk->nsproxy->net_ns);
    if (IS_ERR(new_nsp->net_ns)) {
        err = PTR_ERR(new_nsp->net_ns);
        goto out_net;
    }

    new_nsp->time_ns_for_children = copy_time_ns(flags, user_ns,
                                    tsk->nsproxy->time_ns_for_children);
    if (IS_ERR(new_nsp->time_ns_for_children)) {
        err = PTR_ERR(new_nsp->time_ns_for_children);
        goto out_time;
    }
    new_nsp->time_ns = get_time_ns(tsk->nsproxy->time_ns);

    return new_nsp;

out_time:
    put_net(new_nsp->net_ns);
out_net:
    put_cgroup_ns(new_nsp->cgroup_ns);
out_cgroup:
    if (new_nsp->pid_ns_for_children)
        put_pid_ns(new_nsp->pid_ns_for_children);
out_pid:
    if (new_nsp->ipc_ns)
        put_ipc_ns(new_nsp->ipc_ns);
out_ipc:
    if (new_nsp->uts_ns)
        put_uts_ns(new_nsp->uts_ns);
out_uts:
    if (new_nsp->mnt_ns)
        put_mnt_ns(new_nsp->mnt_ns);
out_ns:
    kmem_cache_free(nsproxy_cachep, new_nsp);
    return ERR_PTR(err);
}
```

([nsproxy.c:67-145](../linux/kernel/nsproxy.c#L67-L145))

这段代码看起来长,其实是一个**极其规则的模板**,每一行都在重复同一个模式:**调 `copy_*_ns` → 失败 goto 对应标号 → 成功继续下一个**。注意三个细节:

**细节一:每个 `copy_*_ns` 都遵循同一套范式。** 以 `copy_utsname` 为例:

```c
/* kernel/utsname.c:89-104 */
struct uts_namespace *copy_utsname(unsigned long flags,
    struct user_namespace *user_ns, struct uts_namespace *old_ns)
{
    struct uts_namespace *new_ns;

    BUG_ON(!old_ns);
    get_uts_ns(old_ns);                /* 先把 old_ns 引用 +1 */

    if (!(flags & CLONE_NEWUTS))
        return old_ns;                 /* 不要新 ns:返回 old(已 +1) */

    new_ns = clone_uts_ns(user_ns, old_ns);  /* 要新 ns:复制一份 */
    put_uts_ns(old_ns);                /* old_ns 引用还回去 */
    return new_ns;
}
```

([utsname.c:89-104](../linux/kernel/utsname.c#L89-L104))

所有 7 种 ns 的 `copy_*_ns` 都是这套:**先 get old_ns → 检查标志位 → 不要新的就 return old(已 +1)→ 要新的就 clone 一份 + put old**。这种"先获取引用,再判断要不要新"的两步法,保证了无论是返回 old 还是返回 new,调用者拿到的都已经是一个"持了引用"的对象——不用再操心补加引用。这是 Linux 内核代码里反复出现的"引用计数契约":**函数返回一个带引用的对象,调用者负责 put**。

> **钉死这件事**:`copy_*_ns` 范式 = **检查标志位 → 共享(get,return old)或复制(clone,put old,return new)**。后面 6 章(mnt/pid/net/uts/ipc/cgroup)每一种 ns 的 copy 函数都是这个模板的特化,差异只在 `clone_*_ns` 内部怎么造新 ns(uts 是 kmem_cache_alloc + 复制字符串,net 是 setup_net 遍历 pernet_ops,mnt 是 copy_tree 整树复制)。

**细节二:回滚链是"反序 put"。** 看标号 `out_time → out_net → out_cgroup → out_pid → out_ipc → out_uts → out_ns`——这正是创建顺序(mnt → uts → ipc → pid → cgroup → net → time)的**反序**。如果 `copy_net_ns` 失败跳到 `out_net`,前面已经成功创建了 mnt/uts/ipc/pid/cgroup,回滚时必须按"后创建的先 put"——LIFO 顺序,和栈一样。这是"构造失败回滚"的标准模式:**分配顺序的镜像就是释放顺序**。

为什么必须是反序?因为这保证每个 ns 释放时,它依赖的其他 ns 还没释放(比如 net ns 可能引用了 user ns,得先释放 net 才能释放 user——虽然这里 user ns 不在 nsproxy,但依赖关系是普遍原则)。在 namespace 这种弱依赖场景里,反序 put 主要保证"释放语义清晰",但在更强依赖的子系统(如 cgroup controller 之间)反序是 soundness 的硬要求。

**细节三:`create_new_namespaces` 返回的 nsproxy 还没挂到 task 上。** 函数头部的注释明说:

```
* Return the newly created nsproxy.  Do not attach this to the task,
* leave it to the caller to do proper locking and attach it to task.
```

([nsproxy.c:62-66](../linux/kernel/nsproxy.c#L62-L66))

挂的动作在 `copy_namespaces` 的最后一句 `tsk->nsproxy = new_ns;`([nsproxy.c:186](../linux/kernel/nsproxy.c#L186))——一行指针赋值。这一行是 fork 路径里 namespace 切换的"原子点":在这之前,task 持的是 old nsproxy;在这一行之后,task 持的是 new nsproxy。没有任何中间状态。这就是为什么"换视图 = 换指针"是原子的——**指针赋值在所有架构上都是原子的**(对齐的指针读写不会撕裂)。

> **反面对比**:如果 create_new_namespaces 不用回滚链,而是"逐个改 task->nsproxy->mnt_ns、task->nsproxy->uts_ns、...",会撞上两个致命问题:① task 持的是父亲的 nsproxy(被多个任务共享),改它的字段会污染所有共享者——这不是"创建新 ns",这是"篡改父亲的视图";② 中间失败没法回滚——改了 mnt_ns 没改 uts_ns,task 的 nsproxy 就是"新 mnt + 旧 uts",这个 nsproxy 还被兄弟进程共享着,一堆进程视图错乱。所以 nsproxy 必须是**先造一个独立的新 nsproxy、填好全部字段、再一次性替换**——这个"构造 → 替换"的两阶段,是 nsproxy 原子切换的工程保证。

---

## 2.5 switch_task_namespaces:运行时切视图为什么只用 task_lock

fork 路径讲完了。但 namespace 还有另一条路径:**运行时切换**——`unshare`(从当前进程剥出新 ns)和 `setns`(加入已有 ns)。这两条路径最终都落到 [`switch_task_namespaces`](../linux/kernel/nsproxy.c#L239-L252):

```c
/* kernel/nsproxy.c:239-252 */
void switch_task_namespaces(struct task_struct *p, struct nsproxy *new)
{
    struct nsproxy *ns;

    might_sleep();

    task_lock(p);                    /* 只锁任务自己的 alloc_lock */
    ns = p->nsproxy;
    p->nsproxy = new;                /* 一行指针赋值 */
    task_unlock(p);

    if (ns)
        put_nsproxy(ns);             /* 释放旧 nsproxy(可能触发 free) */
}
```

([nsproxy.c:239-252](../linux/kernel/nsproxy.c#L239-L252))

这函数短得惊人——核心就 4 行:加锁、保存旧的、换上新的、解锁。`might_sleep()` 是为 `task_lock` 在 CONFIG_PREEMPT_RT 下可能睡眠的标注(普通内核里 task_lock 是 spinlock,不睡眠;RT 下变成可睡眠锁)。

这里最值得问的一个问题是:**为什么只锁 `task_lock`,不用全局 namespace 锁?** 毕竟 namespace 是全机共享的资源,看上去需要某种全局保护。

答案藏在 `task_lock` 的真实身份里——它是 `task_struct->alloc_lock` 这把 spinlock([sched.h:1141](../linux/include/linux/sched.h#L1141)):

```c
/* include/linux/sched.h:1141(简化) */
struct task_struct {
    ...
    spinlock_t alloc_lock;    /* 保护 nsproxy/fs/files 等字段的读写 */
    ...
};
```

这把锁**只保护这一个 task_struct 的若干字段**(nsproxy/fs 等用 alloc_lock 保护的字段,sched.h L1225/1261 注释明示),不是任何全局资源。换句话说,**每个任务持自己的 alloc_lock,各任务之间不竞争**。

这种"每个任务自己一把锁保护自己的 nsproxy 指针"的设计,sound 在三点:

**第一,nsproxy 是 per-task 的指针,不是全局表。** `task_struct->nsproxy` 这个字段属于这个 task,改它只需要保证"没有别的 CPU 在同时读它"。读它的是谁?主要是这个 task 自己(在系统调用里访问自己的 ns)和别的任务通过 `/proc/<pid>/ns/*` 观察时(走 task_lock 拍快照)。这两类访问都用 task_lock 序列化,**不存在跨任务并发改同一个 nsproxy 指针的场景**(每个任务只能改自己的 nsproxy 指针)。

**第二,nsproxy 本身用 refcount 保护,不需要锁。** 多个任务共享同一个 nsproxy(比如 fork 后子进程和父亲共享),靠 `refcount_t count` 管生命周期——`get_nsproxy` 原子加 1,`put_nsproxy` 减到 0 才 `free_nsproxy`。`switch_task_namespaces` 最后那句 `put_nsproxy(ns)` 释放旧 nsproxy 时,如果还有别人共享(count > 0),只是减 1,不释放;只有最后一个引用者 put 才真释放。**引用计数本身用原子操作,不需要锁**。

**第三,新 nsproxy 在挂上去之前,别的 CPU 看不到。** `create_new_namespaces` 返回的 new_nsp 是刚分配的,只有调用者持引用(没人共享它)。挂上去的瞬间是 `p->nsproxy = new;` 这一行指针赋值——**对齐的指针读写不撕裂**,所以读者要么看到旧的、要么看到新的,不会看到半个指针。task_lock 序列化了"读 nsproxy 指针 → 解引用 nsproxy 字段"这个序列(防止读到指针后、解引用前被换走),但**指针赋值本身已经是原子的**。

nsproxy.h L69-92 的注释把这三条访问规则钉死了:

```
* the namespaces access rules are:
*
*  1. only current task is allowed to change tsk->nsproxy pointer or
*     any pointer on the nsproxy itself.  Current must hold the task_lock
*     when changing tsk->nsproxy.
*
*  2. when accessing (i.e. reading) current task's namespaces - no
*     precautions should be taken - just dereference the pointers
*
*  3. the access to other task namespaces is performed like this
*     task_lock(task);
*     nsproxy = task->nsproxy;
*     if (nsproxy != NULL) {
*             / * work with the namespaces here * /
*     }
*     task_unlock(task);
```

([nsproxy.h:69-92](../linux/include/linux/nsproxy.h#L69-L92))

三条规则:

1. **改 nsproxy 指针**:只有 current 自己能改自己的 nsproxy,改时要持 task_lock。
2. **读自己的 nsproxy**:不用任何锁——直接解引用。因为没人能在你背后改你的 nsproxy(规则 1 说只有 current 能改自己的)。
3. **读别人的 nsproxy**:持对方的 task_lock 拍快照。

这三条规则合起来,把 nsproxy 的并发模型压到了**最低开销**:平时访问自己的 ns(99% 的场景)零开销;只有 setns/proc_ns 这种少数路径才要锁。这是 namespace 子系统能扛大规模容器并发(一个 K8s 节点几百个 pod,每个 pod 几十次 ns 切换)的根本。

> **反面对比**:如果用一把全局 `namespace_mutex` 保护所有 nsproxy 切换,① 任意两个并发 setns/unshare 会串行化(两个不同容器各自 setns,本来完全无冲突却要互相等);② 一个容器的 namespace 操作会阻塞另一个容器的 fork(因为 fork 也走 copy_namespaces)——这在云原生高密度场景下是性能灾难。Linux 选择"per-task alloc_lock + refcount"的组合,把切换并发性推到了极限:**两个 CPU 可以同时切换两个不同任务的 nsproxy,零竞争**。这是"用结构设计消灭锁"的典范,和第 13 本《同步原语》里 per-CPU 计数器、第 9 本《内存分配器》里 per-CPU cache 是同一思路。

### `exit_task_namespaces`:任务死亡时的最后一换

顺着 `switch_task_namespaces` 再看一个特殊情形——任务退出时:

```c
/* kernel/nsproxy.c:254-257 */
void exit_task_namespaces(struct task_struct *p)
{
    switch_task_namespaces(p, NULL);
}
```

([nsproxy.c:254-257](../linux/kernel/nsproxy.c#L254-L257))

就一行——`switch_task_namespaces(p, NULL)`,把 nsproxy 置 NULL。这看起来奇怪:为什么要置 NULL?直接让 task_struct 释放时不就自然释放了吗?

置 NULL 的目的是**和"读别人 nsproxy"的规则三配合**。规则三里 `if (nsproxy != NULL)` 的判断就是为这个场景设计的——nsproxy 已被 exit 置 NULL 的任务,处于"快死了"(do_exit 流程中)的状态,别的任务通过 `/proc/<pid>/ns/*` 看它时,task_lock 拿到的 nsproxy 是 NULL,直接返回 ESRCH(进程不存在)。这避免了"读到一个正在被释放的 nsproxy"的 use-after-free。注释 L88-90 明示:

```
*     } / *
*         * NULL task->nsproxy means that this task is
*         * almost dead (zombie)
*         * /
```

([nsproxy.h:88-90](../linux/include/linux/nsproxy.h#L88-L90))

而 `switch_task_namespaces(p, NULL)` 会把旧 nsproxy 的 refcount 减 1,如果这个 nsproxy 是最后一个引用者(count 减到 0),`put_nsproxy` 触发 `free_nsproxy`,把 7 种 ns 各 put 一遍:

```c
/* kernel/nsproxy.c:190-207 */
void free_nsproxy(struct nsproxy *ns)
{
    if (ns->mnt_ns)
        put_mnt_ns(ns->mnt_ns);
    if (ns->uts_ns)
        put_uts_ns(ns->uts_ns);
    if (ns->ipc_ns)
        put_ipc_ns(ns->ipc_ns);
    if (ns->pid_ns_for_children)
        put_pid_ns(ns->pid_ns_for_children);
    if (ns->time_ns)
        put_time_ns(ns->time_ns);
    if (ns->time_ns_for_children)
        put_time_ns(ns->time_ns_for_children);
    put_cgroup_ns(ns->cgroup_ns);
    put_net(ns->net_ns);
    kmem_cache_free(nsproxy_cachep, ns);
}
```

([nsproxy.c:190-207](../linux/kernel/nsproxy.c#L190-L207))

注意每个 ns 都有 `if (ns->xxx_ns)` 检查——因为这些字段可能为 NULL(比如 init_nsproxy 的 mnt_ns 就是 NULL,见下节)。这又是"构造时全成或全回滚"的反向版本——释放时每个字段独立判断,只要非 NULL 就 put。nsproxy 这层不持有任何"半构造"状态。

---

## 2.6 unshare:运行时给自己剥新 ns

第三条进入 namespace 的路径是 `unshare`——当前进程从自己的 nsproxy 里"剥"出一组新 ns。它从 [`SYSCALL_DEFINE1(unshare)`](../linux/kernel/fork.c#L3392) 入口,最终调 [`unshare_nsproxy_namespaces`](../linux/kernel/nsproxy.c#L213-L237):

```c
/* kernel/nsproxy.c:213-237 */
int unshare_nsproxy_namespaces(unsigned long unshare_flags,
    struct nsproxy **new_nsp, struct cred *new_cred, struct fs_struct *new_fs)
{
    struct user_namespace *user_ns;
    int err = 0;

    if (!(unshare_flags & (CLONE_NEWNS | CLONE_NEWUTS | CLONE_NEWIPC |
                           CLONE_NEWNET | CLONE_NEWPID | CLONE_NEWCGROUP |
                           CLONE_NEWTIME)))
        return 0;   /* 没要任何新 ns,直接返回 */

    user_ns = new_cred ? new_cred->user_ns : current_user_ns();
    if (!ns_capable(user_ns, CAP_SYS_ADMIN))
        return -EPERM;

    *new_nsp = create_new_namespaces(unshare_flags, current, user_ns,
                                     new_fs ? new_fs : current->fs);
    if (IS_ERR(*new_nsp)) {
        err = PTR_ERR(*new_nsp);
        goto out;
    }

out:
    return err;
}
```

([nsproxy.c:213-237](../linux/kernel/nsproxy.c#L213-L237))

和 `copy_namespaces` 的判定层几乎一模一样:① 检查有没有 `CLONE_NEW*`(没有就早退);② 检查 `CAP_SYS_ADMIN`;③ 调 `create_new_namespaces`。区别只有一个——它返回 new_nsp 给调用者,**不在函数里挂到 task 上**。挂的动作在 [`ksys_unshare`](../linux/kernel/fork.c#L3323) 的更上层(fork.c L3323 调用点之后,会在适当时机 `switch_task_namespaces(current, new_nsproxy)`)。

这种"构造和挂靠分离"的设计贯穿 namespace 子系统:**`create_new_namespaces` 只负责造,`switch_task_namespaces` 只负责挂**。两条职责分开,是因为:

- 造的过程可能睡眠(`copy_mnt_ns` 要分配内存、`copy_net_ns` 要遍历 pernet_ops 调各协议 init),不能在持锁状态下做。
- 挂的动作极快(一行指针赋值),可以在更窄的临界区里做。

构造阶段失败(比如 copy_net_ns 返回 ENOMEM),只是返回 ERR_PTR,没有副作用——因为没有挂到任何 task 上,新 nsproxy 自然没人引用,`put_nsproxy` 把它释放掉就完事。这种"两阶段"是构造失败 sound 的另一层保证:**失败时已经造出的新 ns 不会污染任何 task**。

> **钉死这件事**:namespace 三条进入路径(fork/unshare/setns)都汇聚到 `create_new_namespaces` + `switch_task_namespaces` 这对组合:**前者构造新 nsproxy(可能睡眠、可能失败、失败有回滚),后者挂到 task 上(一行指针赋值,持 task_lock)**。这种"构造和挂靠分离"是 namespace 子系统并发 sound 的工程骨架,也解释了为什么 `switch_task_namespaces` 那么短——它只做"最不可分割"的那一步(换指针),把所有可能失败的复杂逻辑都甩给了构造阶段。

---

## 2.7 ns_common + proc_ns_operations:7 种 ns 共用的多态骨架

本章最后一节,把 namespace 子系统的**架构骨架**点出来——它决定了为什么后面 6 章(mnt/pid/net/uts/ipc/cgroup)虽然各自实现差异巨大,却能共用同一套接口。

### ns_common:每个 ns 内嵌一个"多态锚"

每种 ns 的结构体(`struct mnt_namespace`/`struct uts_namespace`/...)里,都内嵌了一个 [`struct ns_common`](../linux/include/linux/ns_common.h#L9-L14):

```c
/* include/linux/ns_common.h:9-14 */
struct ns_common {
    struct dentry *stashed;
    const struct proc_ns_operations *ops;   /* 函数指针表! */
    unsigned int inum;                       /* 这个 ns 的唯一 inode 号 */
    refcount_t count;
};
```

([ns_common.h:9](../linux/include/linux/ns_common.h#L9))

每个 ns 通过 `container_of` 把自己从 `ns_common` 反查回来。以 uts ns 为例:

```c
/* kernel/utsname.c:114-117 */
static inline struct uts_namespace *to_uts_ns(struct ns_common *ns)
{
    return container_of(ns, struct uts_namespace, ns);
}
```

([utsname.c:114-117](../linux/kernel/utsname.c#L114-L117))

这种"每个具体结构体内嵌一个 common + ops 函数指针表"的模式,就是**面向对象的虚函数表在 C 里的写法**——和 cgroup 的 `struct cgroup_subsys`、文件的 `struct file_operations`、网络的 `struct pernet_operations`、调度的 `struct sched_class` 是完全同构的设计。`ns_common` 是 namespace 这层的多态锚。

### proc_ns_operations:7 种 ns 各填一张函数指针表

[`struct proc_ns_operations`](../linux/include/linux/proc_ns.h#L16-L25) 这张表长这样:

```c
/* include/linux/proc_ns.h:16-25 */
struct proc_ns_operations {
    const char *name;                                    /* "mnt"/"pid"/... */
    const char *real_ns_name;
    int type;                                            /* CLONE_NEWNS 等 */
    struct ns_common *(*get)(struct task_struct *task);  /* 取引用 */
    void (*put)(struct ns_common *ns);                   /* 放引用 */
    int (*install)(struct nsset *nsset, struct ns_common *ns);  /* setns 装入 */
    struct user_namespace *(*owner)(struct ns_common *ns); /* 谁拥有这个 ns */
    struct user_namespace *(*get_parent)(struct ns_common *ns); /* 父 ns */
} __randomize_layout;
```

([proc_ns.h:16-25](../linux/include/linux/proc_ns.h#L16-L25))

7 种 ns 各填一份,声明在 proc_ns.h L27-36:

```c
/* include/linux/proc_ns.h:27-36 */
extern const struct proc_ns_operations netns_operations;
extern const struct proc_ns_operations utsns_operations;
extern const struct proc_ns_operations ipcns_operations;
extern const struct proc_ns_operations pidns_operations;
extern const struct proc_ns_operations pidns_for_children_operations;
extern const struct proc_ns_operations userns_operations;
extern const struct proc_ns_operations mntns_operations;
extern const struct proc_ns_operations cgroupns_operations;
extern const struct proc_ns_operations timens_operations;
extern const struct proc_ns_operations timens_for_children_operations;
```

([proc_ns.h:27-36](../linux/include/linux/proc_ns.h#L27-L36))

以 uts ns 的 `get` 实现为例,看它怎么用 task_lock 拍快照(和 nsproxy.h 规则三一致):

```c
/* kernel/utsname.c:119-130(节选) */
static struct ns_common *utsns_get(struct task_struct *task)
{
    struct uts_namespace *ns = NULL;
    struct nsproxy *nsproxy;

    task_lock(task);                  /* 持对方的 task_lock */
    nsproxy = task->nsproxy;
    if (nsproxy) {
        ns = nsproxy->uts_ns;
        get_uts_ns(ns);               /* 拿到引用 */
    }
    task_unlock(task);

    return ns ? &ns->ns : NULL;       /* 返回 ns_common 锚 */
}
```

([utsname.c:119-130](../linux/kernel/utsname.c#L119-L130))

这就是规则三"读别人 nsproxy"的标准实现——持 task_lock、读 nsproxy 指针、判断非 NULL、get 引用、解锁、返回 `&ns->ns`(ns_common 锚)。**7 种 ns 的 get 实现几乎逐字相同**,只是字段不同(`nsproxy->uts_ns` vs `nsproxy->net_ns` vs ...)。

### setns 怎么用这张表:统一入口 + 多态分发

`proc_ns_operations` 的真正威力在 setns。看 `validate_ns`:

```c
/* kernel/nsproxy.c:363-366 */
static inline int validate_ns(struct nsset *nsset, struct ns_common *ns)
{
    return ns->ops->install(nsset, ns);   /* 多态调用! */
}
```

([nsproxy.c:363-366](../linux/kernel/nsproxy.c#L363-L366))

一行代码——`ns->ops->install(nsset, ns)`。这一行调的不是某个具体的 `mnt_ns_install` 或 `uts_ns_install`,而是**通过 ns_common 的 ops 指针动态分发**到对应 ns 的 install 实现。这就是多态:**核心路径不知道也不关心具体是哪种 ns,它只调 `ops->install`,具体行为由运行时 ops 指向哪张表决定**。

setns 的 [`SYSCALL_DEFINE2(setns, int, fd, int, flags)`](../linux/kernel/nsproxy.c#L546-L585)([nsproxy.c:546](../linux/kernel/nsproxy.c#L546))走的就是这套:① 从 fd 拿到 `struct ns_common`(通过 `get_proc_ns`,procfs 文件 inode 的 i_private 指向 ns_common);② 调 `validate_ns` 触发 `ops->install`;③ 全部成功后 `commit_nsset` 一次性挂上去(下一章 P3-16 详讲两阶段 commit)。

> **钉死这件事**:7 种 ns 共用同一套接口(get/put/install/owner/get_parent),靠 `struct ns_common` 内嵌 + `struct proc_ns_operations` 函数指针表实现多态。核心路径(copy_namespaces/create_new_namespaces/setns)不直接调某个具体 ns 的函数,而是通过 ops 分发。**新增一种 ns 不需要改核心,只需要实现一张 proc_ns_operations 表**——这是 namespace 子系统能从最初的 mnt ns(2002)一路扩展到 time ns(2020,5.6)而核心代码几乎不动的架构原因。这种"函数指针表 + 核心路径多态分发"是 Linux 内核代码复用的看家本领,和 cgroup 的 `cgroup_subsys`、调度的 `sched_class` 是同一套思路。

---

## 2.8 init_nsproxy:开机时整个世界只有一个 nsproxy

最后看一个看起来琐碎、实则点破 namespace 设计哲学的细节——开机时的 `init_nsproxy`:

```c
/* kernel/nsproxy.c:32-50 */
struct nsproxy init_nsproxy = {
    .count                  = REFCOUNT_INIT(1),
    .uts_ns                 = &init_uts_ns,
#if defined(CONFIG_POSIX_MQUEUE) || defined(CONFIG_SYSVIPC)
    .ipc_ns                 = &init_ipc_ns,
#endif
    .mnt_ns                 = NULL,                 /* 故意是 NULL! */
    .pid_ns_for_children    = &init_pid_ns,
#ifdef CONFIG_NET
    .net_ns                 = &init_net,
#endif
#ifdef CONFIG_CGROUPS
    .cgroup_ns              = &init_cgroup_ns,
#endif
#ifdef CONFIG_TIME_NS
    .time_ns                = &init_time_ns,
    .time_ns_for_children   = &init_time_ns,
#endif
};
```

([nsproxy.c:32-50](../linux/kernel/nsproxy.c#L32-L50))

注意 `.mnt_ns = NULL`——开机时 init 进程的 mnt_ns 居然是 NULL!这是 namespace 设计的一个反直觉细节:**init 进程在引导早期没有 mnt_namespace,直到 `rest_init` 阶段 `kernel_init` → `vfs_kern_mount` 挂上根文件系统后,才会通过显式赋值把 init 进程的 mnt_ns 设成 `&init_mnt_ns`**。在那之前,init 跑在"没有挂载视图"的状态下,所有路径操作走的是内核原始的 `init_task.fs`(一个 `struct fs_struct`,root/pwd 直接指向根 inode)。这是为什么 `free_nsproxy` 里每个字段都要 `if (ns->xxx_ns)` 检查——因为 init_nsproxy 这种特殊场景下字段可能为 NULL,后续 fork 出来的普通进程会继承这个 NULL,直到第一次真正操作 mount 时才被填上。

这个细节揭示了一件事:**namespace 子系统不是开机就完整的——它在引导过程中逐步成型**。mnt ns 是最后一个被填上的(init 之后),time ns 是更晚才稳定(到 5.6 才有)。这是 Linux 启动顺序的一部分,但读者读源码时要心里有数:**init_nsproxy 不是"7 种 ns 的标准模板",它是一个特殊构造体,字段可能为 NULL**。

---

## 技巧精解:nsproxy 聚合 + CLONE_NEW* 标志位驱动

本章技巧精解把两个最核心的工程设计单独拆透。它们看似简单,实则决定了 namespace 子系统的全部骨架。

### 技巧一:聚合换原子性 —— nsproxy 一层间接

7 种 ns 指针为什么不散落在 task_struct,而要聚合成一个 nsproxy?这个看似"加一层间接"(deref 两次:task->nsproxy->mnt_ns 而非 task->mnt_ns)的设计,换来的是三件朴素写法换不来的东西。

**换到一:原子切换。** 换视图 = `task->nsproxy = new;` 一行指针赋值。指针赋值在所有架构上原子(对齐读写不撕裂),所以视图切换天然原子——读者要么看到旧 nsproxy、要么看到新 nsproxy,绝不会看到"新 mnt + 旧 uts"这种半新半旧状态。

朴素写法(7 个字段散落)做不到这一点:改 7 个字段是 7 次独立赋值,中间任何一刻被别的 CPU 读到,就是混合状态。要避免,只能加全局锁——但全局锁就失去了"per-task 独立切换"的并发性。聚合一层,把 7 次赋值压缩成 1 次,自然原子,不用任何额外锁。

**换到二:引用计数集中。** 共享全部 ns 的多个任务(比如 fork 后不要 NEW 的子进程),只持一个 nsproxy 指针,共享一个 refcount。fork 走"共享快路"时只 `get_nsproxy(old_ns)` 一次 inc;exit 时 `put_nsproxy` 一次 dec。**7 种 ns 的引用计数开销被压成 1 次原子操作**。

朴素写法做不到:每个 ns 字段独立,得 inc/dec 7 次(fork 7 次原子操作,exit 7 次)。在高频 fork/exit 场景(如 shell 启动一堆子进程),这个差距会被放大。

**换到三:COW 友好。** fork 时不要任何 `CLONE_NEW*` 的情形(99% 的 fork),子进程直接共享父亲的 nsproxy,零拷贝;只要有一种 ns 要变,才 copy 整个 nsproxy。这是 nsproxy.h 注释里写的"As soon as a single namespace is cloned or unshared, the nsproxy is copied"——**COW 的本质就是"共享直到要变"**。

朴素写法没有这个共享层级——每个 task_struct 持自己的 7 个字段,fork 时每个字段都要判断"共享还是复制",逻辑复杂且容易错。

> **反面对比**:如果把 7 个 ns 指针散落在 task_struct,① 视图切换不再原子(7 次赋值,中间状态被读到);② 引用计数从 1 次变 7 次;③ 失去 COW 共享层级。聚合一层(多一次 deref),换来原子性 + 性能 + 简化——这是"用一层间接换正确性 + 性能"的典范。同构设计:`css_set` 把 15 个 css 指针收进一个结构体(第 9 章)、`files_struct` 把 fd 表收进一个结构体、`sighand_struct` 把信号 handler 表收进一个结构体——Linux 反复用这套"聚合一层换原子性"的思路管理 task_struct 上的多指针资源。

### 技巧二:标志位驱动换原子表达 —— CLONE_NEW* 一个掩码说要哪些新 ns

7 种 ns,容器启动时要同时切 6~7 种。怎么向内核表达"我要这些新 ns"?Linux 用一个 `unsigned long flags` 的 7 个位,一次性表达:

```c
/* 一次 clone 切 6 种视图(容器启动的典型调用) */
clone(CLONE_NEWNS | CLONE_NEWPID | CLONE_NEWNET |
      CLONE_NEWIPC | CLONE_NEWUTS | CLONE_NEWCGROUP, ...);
```

`copy_namespaces` 用一个 `flags & (CLONE_NEWNS | CLONE_NEWUTS | ...)` 掩码表达式,**一次性判定**"要不要进 create_new_namespaces"。这一行位运算同时表达了"要哪些"+"要不要"两层语义。

朴素写法会是 7 个独立系统调用(每种 ns 一个),撞上的问题我们在 P0-01 已经讲过——① 中间状态不一致(切到第 3 个时进程视图"半新半旧");② 失败回滚难(第 4 个失败,前 3 个怎么回滚)。标志位驱动 + create_new_namespaces 的回滚链,**把"切多种视图"压缩成一次原子操作**:

```
clone(CLONE_NEWNS | CLONE_NEWPID | ...) ──┐
                                          ▼
                            copy_namespaces(flags)
                                          │
                          flags & CLONE_NEW* ? ──── 否 ──► get_nsproxy(old) 共享
                                          │ 是
                                          ▼
                         create_new_namespaces(flags)
                          │  ┌──────────────┐
                          │  │ copy_mnt_ns   │ 失败 ─┐
                          │  │ copy_utsname  │ 失败 ─┤
                          │  │ copy_ipcs     │ 失败 ─┤ 反序 put
                          │  │ copy_pid_ns   │ 失败 ─┤ (goto out_xxx)
                          │  │ copy_cgroup_ns│ 失败 ─┤
                          │  │ copy_net_ns   │ 失败 ─┤
                          │  │ copy_time_ns  │ 失败 ─┘
                          │  └──────┬───────┘
                          │         │ 全成
                          │         ▼
                          │  返回 new_nsp
                          ▼
                tsk->nsproxy = new_ns;  ◄── 一行指针赋值,原子挂上
```

> **钉死这件事**:`CLONE_NEW*` 标志位 + `create_new_namespaces` 回滚链,是"一次系统调用切多种视图"的工程保证。它的精妙在**用位运算把多维选择压成一个数,用回滚链把多次构造压成一次原子操作**。这种"标志位驱动 + 全成或全回滚"的模式,在内核里反复出现:`open()` 的 O_RDONLY|O_WRONLY|O_CREAT、`mmap()` 的 PROT_READ|PROT_WRITE|MAP_SHARED、`socket()` 的 SOCK_STREAM|SOCK_NONBLOCK——它们共享同一种设计哲学:**用位掩码把多维度选项压成一次调用**。namespace 子系统把这个哲学用到了极致——一次 clone 切 7 种视图,全成或全回滚。

---

## 章末小结

这一章是第 1 篇的**地基**。我们没有钻进任何一种具体 ns 的视图隔离机制(mnt/pid/net/...),而是把所有 ns 共享的**总入口**讲透:`task_struct->nsproxy` 一个指针聚合 7 种 ns、`copy_namespaces` 在 fork 时读 `CLONE_NEW*` 决定共享还是复制、`create_new_namespaces` 用回滚链保证原子性、`switch_task_namespaces` 用 task_lock 切视图、`ns_common` + `proc_ns_operations` 多态表让 7 种 ns 共用同一套接口。

回扣全书二分法:这一章服务的显然是**视图隔离(namespace)**那一面——nsproxy 是 namespace 的总入口,聚合的是 7 种"视图指针"。但它本身不提供任何具体视图的隔离逻辑(那是后面 6 章的事),它只提供**聚合 + 切换 + 多态**的骨架。所以这一章在二分法里的位置是**视图(支撑)**——支撑后面所有 ns 章节的地基。

### 五个"为什么"清单

1. **为什么用 nsproxy 聚合 7 种 ns 指针,而不是散落在 task_struct?** 聚合一层换三件事:① 视图切换原子(一行指针赋值);② 引用计数集中(一次 inc/dec);③ COW 友好(共享全部 ns 的任务共享一个 nsproxy,要变才 copy)。

2. **为什么 fork 时 `copy_namespaces` 用 `CLONE_NEW*` 标志位掩码?** 用一个位掩码一次性表达"要哪些新 ns",把"切多种视图"压成一次判定、一次构造。朴素写法(每种 ns 单独系统调用)会留下"半新半旧"中间状态、失败回滚难。

3. **为什么 `create_new_namespaces` 用 goto out_xxx 反序 put?** 构造失败回滚链——分配顺序的镜像就是释放顺序。失败时把已造的 ns 按反序 put,保证进程永远不持有"半构造"的 nsproxy。这是内核"构造失败回滚"的标准模式。

4. **为什么 `switch_task_namespaces` 只用 task_lock,不用全局锁?** nsproxy 是 per-task 的指针(每个任务有自己的),不是全局表。改自己的 nsproxy 不需要全局保护——只需要 task_lock 序列化"读 nsproxy 指针 → 解引用"这个序列。新 nsproxy 在挂上前别的 CPU 看不到,挂上瞬间是一行原子指针赋值。这把切换并发性推到极限。

5. **为什么 7 种 ns 共用 `ns_common` + `proc_ns_operations`?** 多态——核心路径(copy_namespaces/setns)不直接调某个具体 ns 的函数,而是通过 ns_common 的 ops 指针动态分发。新增一种 ns 不改核心(只实现一张 proc_ns_operations 表),这是 namespace 子系统能从 2002 年的 mnt ns 一路扩展到 2020 年的 time ns 而核心几乎不动的架构原因。

### 想继续深入往哪钻

- 本章点到的 `CLONE_NEW*` 标志位定义见 [`uapi/linux/sched.h`](../linux/include/uapi/linux/sched.h#L20-L44)(L20-L44);`struct nsproxy` 见 [`include/linux/nsproxy.h`](../linux/include/linux/nsproxy.h#L32-L42)(L32-L42);`copy_namespaces`/`create_new_namespaces`/`switch_task_namespaces` 全在 [`kernel/nsproxy.c`](../linux/kernel/nsproxy.c)(L67/L151/L239)。
- 想看 7 种 ns 各自怎么实现 `copy_*_ns` 范式,uts ns 最简单([`kernel/utsname.c`](../linux/kernel/utsname.c) L89 的 copy_utsname),是入门的最好样本;net ns 最复杂([`net/core/net_namespace.c`](../linux/net/core/net_namespace.c) L479 的 copy_net_ns),涉及 pernet_ops 链表遍历。
- 想观测 namespace:`ls -l /proc/self/ns/` 看自己的 7 个 ns 链接(每个是个符号链接,指向 `nsfs` 文件,inode 号就是 `ns_common.inum`);`readlink /proc/self/ns/uts` 输出形如 `uts:[4026531838]`,方括号里就是 inum;两个进程的同一 ns 链接指向同一 inode 即表示共享该 ns。
- 想动手:用 `unshare -Urn` 给自己造一个新 user ns + net ns(不需要 root,因为 CLONE_NEWUSER 不需要 CAP_SYS_ADMIN),`ip link` 看到只有 lo;`nsenter -t <pid> -m` 进入别人的 mnt ns(需要权限)。

### 引出下一章

nsproxy 这层骨架立起来了,接下来就该钻进每一种具体的 ns,看它**怎么实现自己的视图隔离**。下一章我们挑最贴近"容器感觉"的一种——**mnt namespace**(挂载视图),讲容器凭什么看到自己的根文件系统。`copy_mnt_ns` 怎么用 `copy_tree` 整树复制挂载树、`pivot_root` 怎么换根、shared/slave/private 三种挂载传播类型怎么精细控制"容器挂个卷,宿主和别的容器看不看得见"——都在 `fs/namespace.c` 里,而且这里的代码量和精妙程度远超 nsproxy 这层。下一章 P1-03,进入 mnt namespace。
