# 第十四 章 · pids/freezer/cpuset:进程数、冻结、绑核

> 篇:第 2 篇 · cgroup 资源控制
> 主线呼应:前四章(P2-11~13)讲的 `cpu.max`/`memory.max`/`io.max` 限的都是**流量**(每秒多少、每字节多少),那是连续记账、连续 throttle。这一章换三件不太一样的东西:`pids.max` 限的是**个数**(整数计数),`cgroup.freeze` 要的是**静止**(整组任务一瞬间全停下来),`cpuset.cpus`/`cpuset.mems` 限的是**位置**(这个容器只能跑在哪些 CPU、只能从哪些内存节点分配)。三件东西表面无关,但都落在 cgroup "资源"这一面,且各有各的内核级难点:进程数要嵌进 fork 路径整树累加,冻结要解决 SIGSTOP 到不了子进程且 race 期新 fork 漏冻的问题,绑核要把 cpumask 一路下发到 `task_struct->cpus_ptr` 并重建调度域。读完本章,第 2 篇就完整了——cpu/memory/io/pids/freezer/cpuset,容器资源控制的全部六个 controller。

## 核心问题

**进程数、冻结、绑核这三件"非流量"的资源控制,内核各用什么手段实现?为什么 SIGSTOP 没法替代 cgroup freezer?为什么绑核不能简单地改一个 `task_struct` 的 cpumask 就完事?**

读完本章你会明白:

1. `pids.max` 限进程数的本质:在 fork 路径(`cgroup_can_fork`→`pids_can_fork`→`pids_try_charge`)里塞一层**层级原子计数**,超了就返回 `-EAGAIN`,fork 直接失败。
2. `cgroup.freeze` 冻结整组的本质:给每个任务设 `JOBCTL_TRAP_FREEZE` 位,让它借信号路径"自己睡着",用 `task->frozen` + `nr_frozen_tasks` 状态机判定整组是否冻透——比 SIGSTOP 安全(race 期新 fork 也被冻、持有锁的任务会先释放再睡)。
3. `cpuset.cpus`/`cpuset.mems` 绑核/绑内存节点的本质:维护"用户配的 + 实际生效的"两套掩码,层级取交集,**一次写要同步下发到组内每个任务的 `cpus_ptr`、可能重建调度域**。
4. freezer 不是独立 controller(`struct cgroup_freezer_state` 直接挂在 `struct cgroup` 上)、不再用 `TIF_FROZEN`(6.9 改为 `task->frozen:1` + `TASK_FREEZABLE`)——这是讲老资料最容易翻车的两个点,本章以 6.9 源码为准。

> **逃生阀**:如果只想记住三句话——`pids.max` 卡 fork、`cgroup.freeze` 让整组自己睡、`cpuset.cpus/mems` 改掩码并下发。三个 controller 各自的"层级"和"状态机"细节是给想读源码的人准备的。

---

## 14.1 一句话点破

> **`pids` 在 fork 上插一杆秤、`freezer` 在信号上挖一道陷阱、`cpuset` 在掩码上画一个圈——三个 controller 各卡一个 fork/signal/scheduling 的关键路径,但都用 cgroup 的层级模型把"一个目录的限制"变成"全子树都受限"。**

这是结论,不是理由。本章倒过来拆:先看进程数怎么在 fork 路径上被层层拦截,再看冻结为什么非得绕开 SIGSTOP 走信号陷阱,最后看绑核的 cpumask 怎么从 cgroup 文件一路下发到 `task_struct->cpus_ptr` 和调度域。

---

## 14.2 pids:在 fork 路径上挂一层层级计数器

### 14.2.1 提出问题:为什么进程数是资源

一个容器里,进程数本身就是一种**资源**。它不像 CPU 时间可以"少用一点",也不像内存可以"换出一点"——一个 `while(1) fork()` 的失控进程,几分钟内能 fork 出几万个子进程,把整机 PID 表(`pid_max` 默认 32768、最大 4194304)吃光,宿主上**任何别的服务都无法再 fork**(sshd 连不上、cron 起不来)。这叫 **fork bomb**,是云原生环境最现实的 DoS 之一。

`pids` controller 就是给这种攻击兜底的:你给一个容器写 `pids.max = 500`,它最多只能有 500 个进程/线程,第 501 个 fork 直接返回 `-EAGAIN`(`fork()` 的标准"暂时不可用"返回码),容器内程序看得到清晰的失败而不是把整机拖垮。

> **不这样会怎样**:没有 `pids` 限制,一个失控容器能把整机 PID 表耗尽,所有别的租户的进程都起不来。这是真正的**跨租户** DoS——和 CPU/内存一样,必须按 cgroup 圈起来。

### 14.2.2 设计:嵌进 fork 路径的 can_fork 回调

cgroup v2 的 controller 都实现成一张函数指针表 [`struct cgroup_subsys`](../linux/include/linux/cgroup-defs.h#L688-L711)([cgroup-defs.h:688](../linux/include/linux/cgroup-defs.h#L688))。其中有一对回调专门给 fork 用:

```c
/* include/linux/cgroup-defs.h:705-708(简化) */
struct cgroup_subsys {
    int  (*can_fork)(struct task_struct *task, struct css_set *cset);
    void (*cancel_fork)(struct task_struct *task, struct css_set *cset);
    void (*fork)(struct task_struct *task);
    ...
};
```

fork 路径在 [`kernel/fork.c`](../linux/kernel/fork.c) 的 `copy_process` 里调 [`cgroup_can_fork`](../linux/kernel/cgroup/cgroup.c#L6525)([cgroup.c:6525](../linux/kernel/cgroup/cgroup.c#L6525)),后者遍历所有"声明了 `can_fork` 回调"的 controller,挨个调 `ss->can_fork(child, kargs->cset)`:

```c
/* kernel/cgroup/cgroup.c:6525-6553(简化) */
int cgroup_can_fork(struct task_struct *child, struct kernel_clone_args *kargs)
{
    struct cgroup_subsys *ss;
    int i, j, ret;

    ret = cgroup_css_set_fork(kargs);          /* 先把目标 css_set 准备好 */
    if (ret)
        return ret;

    do_each_subsys_mask(ss, i, have_canfork_callback) {
        ret = ss->can_fork(child, kargs->cset); /* <- 调每个 controller 的 can_fork */
        if (ret)
            goto out_revert;
    } while_each_subsys_mask();
    return 0;

out_revert:
    for_each_subsys(ss, j) {
        if (j >= i)
            break;
        if (ss->cancel_fork)
            ss->cancel_fork(child, kargs->cset); /* 失败时反序 cancel */
    }
    cgroup_css_set_put_fork(kargs);
    return ret;
}
```

([cgroup.c:6525-6553](../linux/kernel/cgroup/cgroup.c#L6525-L6553))

注意这个回滚链——和 `create_new_namespaces`(P1-02 讲过的)一个模式:**全成或全回滚**。如果第 i 个 controller 的 `can_fork` 失败,前面 i-1 个已记账的 controller 都要 `cancel_fork` 反账。这是 cgroup 把"多 controller 并行记账"做成原子事务的关键。

`pids` 的 `can_fork` 实现就是 [`pids_can_fork`](../linux/kernel/cgroup/pids.c#L238)([pids.c:238](../linux/kernel/cgroup/pids.c#L238)):

```c
/* kernel/cgroup/pids.c:238-260(简化) */
static int pids_can_fork(struct task_struct *task, struct css_set *cset)
{
    struct cgroup_subsys_state *css;
    struct pids_cgroup *pids;
    int err;

    if (cset)
        css = cset->subsys[pids_cgrp_id];
    else
        css = task_css_check(current, pids_cgrp_id, true);
    pids = css_pids(css);

    err = pids_try_charge(pids, 1);          /* <- 实际的记账 */
    if (err) {
        /* 第一次超限时打个 info 日志,方便排查 */
        if (atomic64_inc_return(&pids->events_limit) == 1) {
            pr_info("cgroup: fork rejected by pids controller in ");
            pr_cont_cgroup_path(css->cgroup);
            pr_cont("\n");
        }
        cgroup_file_notify(&pids->events_file);  /* 通知 pids.events 读者 */
    }
    return err;
}
```

([pids.c:238-260](../linux/kernel/cgroup/pids.c#L238-L260))

超了返回 `-EAGAIN`(下一节看 `pids_try_charge` 怎么算),这个 `-EAGAIN` 一路冒到用户态的 `fork()`/`clone()` 返回值。用户程序看到 `errno == EAGAIN`,就知道是 cgroup 卡的。

### 14.2.3 技巧:`pids_try_charge` 的"先加后查,失败回滚"层级原子计数

这是 pids 最有意思的地方。一个任务属于一棵 cgroup 子树(比如 `/sys/fs/cgroup/a/b/c`),它 fork 时**不是只算 c 这一层,而是从 c 一路算到根**——因为父层的 `pids.max` 同样要满足。看 [`pids_try_charge`](../linux/kernel/cgroup/pids.c#L158)([pids.c:158](../linux/kernel/cgroup/pids.c#L158)):

```c
/* kernel/cgroup/pids.c:158-189(简化) */
static int pids_try_charge(struct pids_cgroup *pids, int num)
{
    struct pids_cgroup *p, *q;

    for (p = pids; parent_pids(p); p = parent_pids(p)) {
        int64_t new = atomic64_add_return(num, &p->counter);   /* 先把这一层 +num */
        int64_t limit = atomic64_read(&p->limit);

        if (new > limit)                                        /* 超了 */
            goto revert;

        pids_update_watermark(p, new);                          /* 更新峰值 */
    }
    return 0;

revert:
    for (q = pids; q != p; q = parent_pids(q))                  /* 回滚已加的祖先 */
        pids_cancel(q, num);
    pids_cancel(p, num);                                        /* 回滚刚超限的这一层 */
    return -EAGAIN;
}
```

([pids.c:158-189](../linux/kernel/cgroup/pids.c#L158-L189))

这里藏着一个**非平凡的正确性问题**:为什么是"先 `atomic64_add_return` 加,再查超限,失败再回滚",而不是"先查会不会超,不超才加"?

答案是为并发正确性。设想两个任务同时 fork(`A` 在 cgroup `c`,`B` 在 cgroup `b/c`,共享祖先 `b`),`b` 的 `limit=10`,当前 `b->counter=9`(还差 1):

- 如果"先查后加":A 查 `9 < 10` 通过,B 查 `9 < 10` 也通过,两人都加,`b->counter` 变 11,**超了**——漏判。
- 如果"先加后查":A `add_return` 得到 10,`10<=10` 通过;B `add_return` 得到 11,`11>10` 失败,回滚。**永远不漏判**。

`atomic64_add_return` 是原子读改写,保证两个并发 charge 看到的 `new` 是严格递增的两个值,所以**只要某个祖先会让总计数超过 limit,必然有且仅有一个并发者拿到那个"刚好超限"的返回值并失败**。这是无锁层级计数的标准模式——和《内存分配器》里 per-CPU 计数器回中心链表 flush、`css_set` 引用计数的 `atomic_inc_return` 一个套路。

> **反面对比**:如果用"先查后加",得在查和加之间加锁,而且锁住的是整棵 cgroup 树(因为要锁所有祖先),fork 路径每秒可能几万次,锁竞争会把整机 fork 性能拖垮。"先加后查,失败回滚"用原子操作换掉了锁,是 cgroup 计数器的性能命脉。注意代价是回滚成本(失败时已加的祖先要 `cancel`),但 fork 失败是罕见路径,这个代价可接受。

### 14.2.4 cancel_fork 与 release:fork 失败和进程退出时反账

`pids_try_charge` 在 `can_fork` 阶段就记了账。但 fork 是分阶段的:`can_fork` 之后还有大量初始化(分配 PID、拷页表、装 mm/fs/files...),任何一步失败,整个 fork 失败,这时 `cgroup_can_fork` 的回滚链会调 [`pids_cancel_fork`](../linux/kernel/cgroup/pids.c#L262)([pids.c:262](../linux/kernel/cgroup/pids.c#L262))把刚记的 1 个 pid 反账:

```c
/* kernel/cgroup/pids.c:262-273(简化) */
static void pids_cancel_fork(struct task_struct *task, struct css_set *cset)
{
    struct pids_cgroup *pids = css_pids(cset->subsys[pids_cgrp_id]);
    pids_uncharge(pids, 1);   /* 层级 uncharge:从 pids 一路 cancel 到根 */
}
```

进程正常退出时(走 `do_exit`→`cgroup_release`),调 [`pids_release`](../linux/kernel/cgroup/pids.c#L275)([pids.c:275](../linux/kernel/cgroup/pids.c#L275))同样 `pids_uncharge(pids, 1)`。一进一出,账平。

进程**迁移**到另一个 cgroup 时(写 `cgroup.procs`),也得反账源、记账目标。pids 实现了 `can_attach`/`cancel_attach`([pids.c:191-232](../linux/kernel/cgroup/pids.c#L191-L232)):

```c
/* kernel/cgroup/pids.c:191-214(简化) */
static int pids_can_attach(struct cgroup_taskset *tset)
{
    cgroup_taskset_for_each(task, dst_css, tset) {
        struct pids_cgroup *pids = css_pids(dst_css);
        struct pids_cgroup *old_pids = css_pids(task_css(task, pids_cgrp_id));

        pids_charge(pids, 1);            /* 先给目标 +1(注意:charge 不查限) */
        pids_uncharge(old_pids, 1);      /* 再给源 -1 */
    }
    return 0;
}
```

([pids.c:191-214](../linux/kernel/cgroup/pids.c#L191-L214))

注意 `pids_can_attach` 用的是 `pids_charge`(不是 `pids_try_charge`):[pids.c:138](../linux/kernel/cgroup/pids.c#L138) 的 `pids_charge` 注释说得很清楚——"This function does *not* follow the pid limit set. It cannot fail and the new pid count may exceed the limit. This is only used for reverting failed attaches, where there is no other way out than violating the limit."

> **钉死这件事**:迁移用 `charge`(不限)而不是 `try_charge`,是因为迁移不能失败——一个任务必须属于某个 cgroup,不能"迁移到一半卡住"。源已经 -1 了,目标必须 +1 哪怕超限;超了也只是临时态,接下来要么任务被迁回(`cancel_attach` 反账)、要么管理员调高 limit。**这是"账必须平"高于"限必须守"的精心取舍**。

### 14.2.5 `pids.max`/`pids.current`/`pids.peak`/`pids.events`

pids 暴露给用户态的文件很简单,都在 [`pids_files`](../linux/kernel/cgroup/pids.c#L350-L374)([pids.c:350](../linux/kernel/cgroup/pids.c#L350)):

| 文件 | 含义 | 实现 |
|------|------|------|
| `pids.max` | 进程数上限,写 "max" 或数字 | `pids_max_write`@L282 / `pids_max_show`@L312 |
| `pids.current` | 当前进程数(含子树) | `pids_current_read`@L326,读 `atomic64_t counter` |
| `pids.peak` | 历史峰值 | `pids_peak_read`@L334,读 `watermark` |
| `pids.events` | 超限次数 | `pids_events_show`@L342,读 `events_limit` |

`pids.current` 是**层级累加**的:子 cgroup 的进程也算进父的 counter(因为 `pids_try_charge` 从叶子一路加到根)。所以 `parent/pids.current >= parent/child/pids.current` 永远成立——这和 cgroup v2 的"单一层级"模型(P4-18 详讲)天然契合。

`pids.max` 写入用一个 `atomic64_set` 就改了 `limit`,注释明确说"Limit updates don't need to be mutex'd, since it isn't critical that any racing fork()s follow the new limit"([pids.c:304-307](../linux/kernel/cgroup/pids.c#L304-L307))——limit 是个宽松的门槛,竞态期间一两个 fork 多通过/少通过都无所谓,不需要锁。

pids 的 [`cgroup_subsys`](../linux/kernel/cgroup/pids.c#L376-L387) 注册表里只填了 `css_alloc`/`css_free`/`can_attach`/`cancel_attach`/`can_fork`/`cancel_fork`/`release` 这几个回调——其他都是默认行为。这是 cgroup v2 controller 的典型形态:**核心代码完全不知道 pids 的存在,只通过函数指针表 `struct cgroup_subsys` 调用;pids 反过来也不知道 cpu/memcg 的存在,各管各的**。这是"函数指针多态"在内核里的标准用法,新增一个 controller 不需要改 fork/exit/attach 的核心代码。

---

## 14.3 freezer:让整组任务"自己睡着"

### 14.3.1 提出问题:为什么 SIGSTOP 不够

容器场景里,经常需要"瞬间冻住一个 cgroup 的所有任务":K8s 的 pod freeze/restore、checkpoint/restore(CRIU)、调试时暂停容器、避免迁移过程中任务乱跑。最朴素的办法是 `kill -STOP <pid>`(发 SIGSTOP),但 SIGSTOP 有几个**致命**问题:

1. **到不了 cgroup 的所有子进程**:SIGSTOP 是按 PID 发的,你得 `ps` 列出容器里所有 PID 一个个发。但容器里的 PID 在宿主上不是连续可见的( pid namespace 隔离,P1-04 讲过),你得在宿主视角枚举容器所有 task——这本身需要遍历 cgroup 任务列表。
2. **race 期间新 fork 的任务漏冻**:你发完 STOP,容器内某个任务正好在 `fork()`,新子进程在 STOP 发送窗口内创建,它没收到信号,**照样跑**。freeze 的语义被破坏。
3. **持有锁的任务被强制停下会死锁**:SIGSTOP 不给任务"先释放锁再睡"的机会,如果任务正持有某个全局锁(比如 `mmap_lock`),冻住后别的等锁任务全卡死。
4. **SIGSTOP 无法被子进程覆盖**:父进程对 SIGSTOP 不能 catch、不能 mask,但**自定义的子进程可以通过 ptrace 或其他机制绕开**——你要冻的是整组,不能有漏网之鱼。

> **不这样会怎样**:用 SIGSTOP 冻容器,新 fork 漏冻、持锁任务死锁、pid ns 隔离枚举困难——四个坑任何一个都让"冻结整组"不可靠。cgroup 必须提供一个**整组、原子、给任务释放锁机会**的冻结机制。

### 14.3.2 设计:在 cgroup 上挂"想要冻结"的标记,让任务自己睡着

cgroup freezer 的核心思路不是"内核主动把任务停下",而是**两步走**:

1. 在 cgroup 上设一个"应该被冻结"的标记(`CGRP_FREEZE` flag),所有后代 cgroup 都继承。
2. 给这个 cgroup 里的每个任务设一个 `JOBCTL_TRAP_FREEZE` 位,这个位让任务**在下次进入信号处理路径时**(从系统调用返回、从中断返回、从 `schedule()` 醒来),自己调 `do_freezer_trap` 把状态切到 `TASK_FREEZABLE` 然后 `schedule()` 出去睡。

**任务是"自己睡着"的,不是被外部强制停的**。这是关键差别:任务在 `schedule()` 之前,有机会释放它持有的任何锁(内核栈上的 mutex、spinlock 在 syscall 路径里都会在合适的点释放)。睡着的任务状态是 `TASK_INTERRUPTIBLE|TASK_FREEZABLE`,可以被信号唤醒——解冻就是清掉 `JOBCTL_TRAP_FREEZE` 位并 `wake_up_process`。

冻结的入口是写 `cgroup.freeze` 文件(在 cgroup v2 的 base files 表 [cgroup.c:5259-5264](../linux/kernel/cgroup/cgroup.c#L5259-L5264)):

```c
/* kernel/cgroup/cgroup.c:5259-5264 */
{
    .name = "cgroup.freeze",
    .flags = CFTYPE_NOT_ON_ROOT,
    .seq_show = cgroup_freeze_show,
    .write = cgroup_freeze_write,
},
```

写入 1 触发 [`cgroup_freeze_write`](../linux/kernel/cgroup/cgroup.c#L3933)→[`cgroup_freeze`](../linux/kernel/cgroup/freezer.c#L260)([freezer.c:260](../linux/kernel/cgroup/freezer.c#L260)):

```c
/* kernel/cgroup/freezer.c:260-323(简化) */
void cgroup_freeze(struct cgroup *cgrp, bool freeze)
{
    struct cgroup_subsys_state *css;
    struct cgroup *dsct;
    bool applied = false;

    lockdep_assert_held(&cgroup_mutex);

    if (cgrp->freezer.freeze == freeze)   /* 没变化,直接退出 */
        return;
    cgrp->freezer.freeze = freeze;

    /* 沿子树向下传播 */
    css_for_each_descendant_pre(css, &cgrp->self) {
        dsct = css->cgroup;
        if (cgroup_is_dead(dsct))
            continue;

        if (freeze) {
            dsct->freezer.e_freeze++;
            if (dsct->freezer.e_freeze > 1)   /* 已经被某个祖先冻着,跳过 */
                continue;
        } else {
            dsct->freezer.e_freeze--;
            if (dsct->freezer.e_freeze > 0)   /* 还有别的祖先冻着,跳过 */
                continue;
        }

        cgroup_do_freeze(dsct, freeze);       /* 实际去冻/解冻这个 cgroup */
        applied = true;
    }
    ...
}
```

([freezer.c:260-323](../linux/kernel/cgroup/freezer.c#L260-L323))

这里有个**精妙的设计**:`e_freeze` 是个**整数计数器**,不是 bool。为什么?因为 cgroup 是树,可能有多个祖先同时要求冻结:祖先 A 冻结时,后代 C 的 `e_freeze=1`;然后另一个祖先 B(在 A 和 C 之间)也冻结,C 的 `e_freeze=2`。这时 B 解冻,只是把 C 的 `e_freeze` 减到 1,**C 仍然冻着**(因为 A 还在冻)——直到 A 也解冻,C 才真正解冻。这避免了"中间祖先解冻导致叶子意外解冻"的错误。`e_freeze` 是"effective freeze"的引用计数。

实际的冻/解冻动作在 [`cgroup_do_freeze`](../linux/kernel/cgroup/freezer.c#L177)([freezer.c:177](../linux/kernel/cgroup/freezer.c#L177)):

```c
/* kernel/cgroup/freezer.c:177-216(简化) */
static void cgroup_do_freeze(struct cgroup *cgrp, bool freeze)
{
    struct css_task_iter it;
    struct task_struct *task;

    spin_lock_irq(&css_set_lock);
    if (freeze)
        set_bit(CGRP_FREEZE, &cgrp->flags);      /* 在 cgroup 上打标记 */
    else
        clear_bit(CGRP_FREEZE, &cgrp->flags);
    spin_unlock_irq(&css_set_lock);

    css_task_iter_start(&cgrp->self, 0, &it);     /* 遍历这个 cgroup 的所有任务 */
    while ((task = css_task_iter_next(&it))) {
        if (task->flags & PF_KTHREAD)
            continue;                              /* 内核线程不冻 */
        cgroup_freeze_task(task, freeze);
    }
    css_task_iter_end(&it);
    ...
}
```

([freezer.c:177-216](../linux/kernel/cgroup/freezer.c#L177-L216))

注意两个点:① `css_task_iter` 用 RCU 安全地遍历 cgroup 任务列表(回扣 P2-09 的 css_set 设计),迭代和迁移不互锁;② 内核线程(`PF_KTHREAD`)不冻——内核线程可能正在做关键 IO(比如刷盘),冻住会死锁。

`cgroup_freeze_task` 才是真正给单个任务"埋陷阱"的地方([freezer.c:155](../linux/kernel/cgroup/freezer.c#L155)):

```c
/* kernel/cgroup/freezer.c:155-172(简化) */
static void cgroup_freeze_task(struct task_struct *task, bool freeze)
{
    unsigned long flags;

    if (!lock_task_sighand(task, &flags))   /* 拿 task 的 sighand 锁 */
        return;

    if (freeze) {
        task->jobctl |= JOBCTL_TRAP_FREEZE;    /* 设陷阱位 */
        signal_wake_up(task, false);            /* 唤醒任务,让它进信号路径 */
    } else {
        task->jobctl &= ~JOBCTL_TRAP_FREEZE;   /* 清陷阱位 */
        wake_up_process(task);                  /* 直接唤醒 */
    }

    unlock_task_sighand(task, &flags);
}
```

([freezer.c:155-172](../linux/kernel/cgroup/freezer.c#L155-L172))

`JOBCTL_TRAP_FREEZE` 是个 jobctl 位(定义在 `include/linux/sched/jobctl.h`),它本身不直接停任务,而是把任务"骗"进信号处理路径。任务下次从内核返回用户态(或被 `signal_wake_up` 唤醒)时,会检查 `TIF_SIGPENDING`(`signal_wake_up` 顺便设的),进 `do_signal`→`get_signal`→看到 `JOBCTL_TRAP_FREEZE`,调 [`do_freezer_trap`](../linux/kernel/signal.c#L2580)([signal.c:2580](../linux/kernel/signal.c#L2580)):

```c
/* kernel/signal.c:2580-2605(简化) */
static void do_freezer_trap(void)
    __releases(&current->sighand->siglock)
{
    /* 如果还有别的待处理信号/trap,先处理那个,下一轮再来 */
    if ((current->jobctl & (JOBCTL_PENDING_MASK | JOBCTL_TRAP_FREEZE)) !=
         JOBCTL_TRAP_FREEZE) {
        spin_unlock_irq(&current->sighand->siglock);
        return;
    }

    __set_current_state(TASK_INTERRUPTIBLE|TASK_FREEZABLE);  /* 睡,且可被 freezer 跳过 */
    clear_thread_flag(TIF_SIGPENDING);
    spin_unlock_irq(&current->sighand->siglock);
    cgroup_enter_frozen();    /* 把自己标成 frozen,更新 cgroup 计数 */
    schedule();               /* 让出 CPU,真正睡下去 */
}
```

([signal.c:2580-2605](../linux/kernel/signal.c#L2580-L2605))

`cgroup_enter_frozen`([freezer.c:107](../linux/kernel/cgroup/freezer.c#L107))把这个任务标成 frozen、bump 所在 cgroup 的 `nr_frozen_tasks`、调 `cgroup_update_frozen` 判定整组是否冻透:

```c
/* kernel/cgroup/freezer.c:52-84(简化) */
void cgroup_update_frozen(struct cgroup *cgrp)
{
    bool frozen;

    lockdep_assert_held(&css_set_lock);

    /* 冻透的条件:CGRP_FREEZE 标记在,且 nr_frozen_tasks == 任务总数 */
    frozen = test_bit(CGRP_FREEZE, &cgrp->flags) &&
        cgrp->freezer.nr_frozen_tasks == __cgroup_task_count(cgrp);

    if (frozen) {
        if (test_bit(CGRP_FROZEN, &cgrp->flags))
            return;                       /* 已经冻透了 */
        set_bit(CGRP_FROZEN, &cgrp->flags);
    } else {
        if (!test_bit(CGRP_FROZEN, &cgrp->flags))
            return;
        clear_bit(CGRP_FROZEN, &cgrp->flags);
    }
    cgroup_file_notify(&cgrp->events_file);   /* 通知 cgroup.events 读者 */
    cgroup_propagate_frozen(cgrp, frozen);    /* 把状态往上传播 */
}
```

([freezer.c:52-84](../linux/kernel/cgroup/freezer.c#L52-L84))

`cgroup.events` 文件里有个 `frozen` 字段(读 `CGRP_FROZEN` flag),用户态可以 poll 它来等"整组冻透"——CRIU 等工具就这么等。

> **钉死这件事**:freezer 不是"内核主动停任务",而是**给每个任务埋一个 `JOBCTL_TRAP_FREEZE` 陷阱位,让任务下次进信号路径时自己 `schedule()` 出去睡**。任务睡之前可以释放任何持有的锁,睡的状态是 `TASK_FREEZABLE`,可以被 freezer 子系统识别和跳过。这是"协作式冻结",比 SIGSTOP 的"强制式停止"安全得多。

### 14.3.3 race 期新 fork 怎么办:fork 路径里也埋一份

回忆 14.2 讲的 `cgroup_can_fork`。fork 路径里除了 pids 的 `can_fork`,还有 `cgroup_post_fork`([cgroup.c:6585](../linux/kernel/cgroup/cgroup.c#L6585))在子进程挂到 css_set 之后,检查它所在的 cgroup 是否在冻:

```c
/* kernel/cgroup/cgroup.c:6615-6633(简化) */
if (!(child->flags & PF_KTHREAD)) {
    if (unlikely(test_bit(CGRP_FREEZE, &cgrp_flags))) {
        /*
         * If the cgroup has to be frozen, the new task has too.
         * Let's set the JOBCTL_TRAP_FREEZE jobctl bit to get the
         * task into the frozen state.
         */
        spin_lock(&child->sighand->siglock);
        WARN_ON_ONCE(child->frozen);
        child->jobctl |= JOBCTL_TRAP_FREEZE;
        spin_unlock(&child->sighand->siglock);
    }
    ...
}
```

([cgroup.c:6615-6633](../linux/kernel/cgroup/cgroup.c#L6615-L6633))

这就是 14.3.1 列的第二个坑的解药:**即使任务在 freezer 遍历之后才 fork 出来,子进程也会在 `cgroup_post_fork` 里被设上 `JOBCTL_TRAP_FREEZE` 位**,它一调度就睡着。这就是为什么 freezer 比 SIGSTOP 安全——新 fork 不会漏。

任务退出时也有对称处理:`cgroup_exit`([cgroup.c:6677](../linux/kernel/cgroup/cgroup.c#L6677))会把任务从 css_set 摘掉,如果这个任务之前是 frozen 的,`nr_frozen_tasks` 会随之减1,可能让整组从"冻透"变成"没冻透"——这是 `cgroup_update_frozen` 在每次 inc/dec 后被调用的原因。

### 14.3.4 6.9 源码修正:不再有 `TIF_FROZEN`

讲 cgroup freezer 的老资料(包括 5.x 之前的内核)常说"任务冻住靠 `TIF_FROZEN` 这个 thread_info 标志位"。**这在 6.9 已经过时了**。

6.9 里冻结的状态记录在 [`task_struct->frozen`](../linux/include/linux/sched.h#L945)([sched.h:945](../linux/include/linux/sched.h#L945),一个 `unsigned frozen:1` 位字段),任务进入睡眠用的是 `TASK_FREEZABLE` 这个**任务状态位**(配合 `TASK_INTERRUPTIBLE`,见 signal.c:2600 的 `__set_current_state(TASK_INTERRUPTIBLE|TASK_FREEZABLE)`)。`TASK_FREEZABLE` 是 5.x 引入的,它告诉调度器"我睡着了,但如果是 freezer 相关的唤醒可以跳过我"——这是为 v2/可中断的 froze 设计的,避免任务被无关信号叫醒后又被 freezer 当成没冻。

老的 `TIF_FROZEN`(thread_info 里的 flag)在 v2 中已经不用了,6.9 源码里 Grep 不到任何 `TIF_FROZEN` 的引用。这个改动是为了让 freezer 能和 `TASK_FREEZABLE` 的"可中断睡眠但 freezer 跳过"语义对齐,统一在任务状态而非 thread flag 上表达。

> **钉死这件事(给读源码的人)**:6.9 的 cgroup freezer 用 `task->frozen:1`(`sched.h:945`)+ `TASK_FREEZABLE`(`signal.c:2600`)+ `JOBCTL_TRAP_FREEZE`(`jobctl`,设在 `task_struct->jobctl` `sched.h:892`)三件套。`TIF_FROZEN` 是老资料的说法,在 v2/6.9 已废弃。本书所有源码引用以此为准。

### 14.3.5 freezer 不是独立 controller

另一个老资料常翻车的点:freezer 在 cgroup v1 里曾经是个独立 controller(`freezer_cgrp_subsys`),有自己的 css。但在 cgroup v2,**freezer 不再是 controller**,它的状态直接挂在 [`struct cgroup`](../linux/include/linux/cgroup-defs.h#L397) 的一个子结构 [`cgroup_freezer_state`](../linux/include/linux/cgroup-defs.h#L378-L395)([cgroup-defs.h:378](../linux/include/linux/cgroup-defs.h#L378))上:

```c
/* include/linux/cgroup-defs.h:378-395 */
struct cgroup_freezer_state {
    /* Should the cgroup and its descendants be frozen. */
    bool freeze;                  /* 用户态配的(写 cgroup.freeze) */

    /* Should the cgroup actually be frozen? */
    int e_freeze;                 /* effective,引用计数,祖先叠加 */

    /* Fields below are protected by css_set_lock */

    /* Number of frozen descendant cgroups */
    int nr_frozen_descendants;

    /*
     * Number of tasks, which are counted as frozen:
     * frozen, SIGSTOPped, and PTRACEd.
     */
    int nr_frozen_tasks;
};
```

`struct cgroup` 里直接内嵌这个子结构([cgroup-defs.h:541](../linux/include/linux/cgroup-defs.h#L541),`struct cgroup_freezer_state freezer;`)。所以 `cgroup.freeze` 这个文件不是某个 controller 的 cftype,而是 cgroup 核心的 base files(在 `cgroup.c` 的 `cgroup_base_files` 表里,和 `cgroup.procs`、`cgroup.kill`、`cgroup.events` 一组)。

> **钉死这件事**:cgroup v2 的 freezer 是"核心功能",不是"可加载 controller"。这意味着所有 cgroup(无论 enable 了什么 controller)都能被冻结。这也解释了为什么 `cgroup.freeze` 写入走的是 `cgroup.c` 的 `cgroup_freeze_write`(L3933)而不是某个 controller 的 `.write` 回调。

---

## 14.4 cpuset:把任务钉在指定的 CPU 和内存节点上

### 14.4.1 提出问题:绑核到底绑什么

很多场景要"把这个容器钉在 CPU 2、3、4 上,只让它从 NUMA 节点 0 分配内存":

- **NUMA 系统**:跨节点访问内存慢,数据库类容器要绑在离它的网卡近的节点。
- **隔离干扰**:实时任务绑在专用核心,避免被批处理任务拖累(配合 isolcpus、`cpus.partition`)。
- **资源切片**:K8s 的 `resources.limits.cpu: 2` 翻译成 cpuset,让 pod 只能在 2 个核上跑(对比 `cpu.max` 是按时间片限额,实际能跑在所有核上,只是总时间被限)。

cpuset 解决这个问题:每个 cgroup 配一组 `cpuset.cpus`(允许的 CPU 列表)和 `cpuset.mems`(允许的内存节点列表),组里的任务只能在对应的 CPU/节点上跑。

注意 cpuset 和 `cpu.max` 的区别:**`cpu.max` 限的是时间(每秒多少 ns),cpuset 限的是空间(哪些 CPU/节点)**。一个限"能跑多久",一个限"能在哪跑"——前者是 throttle(P2-11 讲过),后者是 affinity。

> **不这样会怎样**:不用 cpuset,容器可能在所有 CPU 上乱跑,NUMA 系统跨节点访问内存延迟翻倍;实时任务和批处理任务抢同一核心,尾延迟失控;K8s 的 `guaranteed` QoS 类无法严格兑现("独占 CPU"成空话)。

### 14.4.2 设计:用户配的 + 实际生效的,两套掩码

cpuset 最容易让人迷糊的点是 [`struct cpuset`](../linux/kernel/cgroup/cpuset.c#L94-L202)([cpuset.c:94](../linux/kernel/cgroup/cpuset.c#L94))里**每类资源都有两套掩码**:

```c
/* kernel/cgroup/cpuset.c:94-202(简化) */
struct cpuset {
    struct cgroup_subsys_state css;

    unsigned long flags;   /* CS_CPU_EXCLUSIVE / CS_MEM_EXCLUSIVE / CS_SCHED_LOAD_BALANCE / ... */

    /* 用户配的 configured mask(写 cpuset.cpus/cpuset.mems 改这两个) */
    cpumask_var_t cpus_allowed;
    nodemask_t mems_allowed;

    /* 实际生效的 effective mask(任务真正用的) */
    cpumask_var_t effective_cpus;
    nodemask_t effective_mems;

    /* 6.x 引入:专属于本 cgroup 的"独占 CPU",给 partition root 用 */
    cpumask_var_t effective_xcpus;
    cpumask_var_t exclusive_cpus;

    nodemask_t old_mems_allowed;    /* 迁移内存时用 */
    struct fmeter fmeter;            /* memory_pressure 滤波器 */

    int attach_in_progress;          /* can_attach 到 attach 之间防 zero 掩码 */
    int partition_root_state;        /* PRS_MEMBER/PRS_ROOT/PRS_ISOLATED */
    ...
};
```

([cpuset.c:94-202](../linux/kernel/cgroup/cpuset.c#L94-L202))

为什么要两套?因为 **configured 可能和祖先冲突,实际能用的(effect)是取祖先 effective 的交集**。cgroup 注释(cpuset.c:97-117)说得明白:

> effective_mask == configured_mask & parent's effective_mask,
> and if it ends up empty, it will inherit the parent's mask.

举例:父 cgroup 配 `cpuset.cpus=0-7`,子配 `cpuset.cpus=2-10`(用户乱写),子的 `effective_cpus = 2-7`(取交集,把不合法的 8-10 砍掉)。如果 CPU 热插拔拔了 CPU 3,父的 `effective_cpus` 变 `0-2,4-7`,子的 `effective_cpus` 自动跟着变 `2,4-7`。**configured 是用户写的"愿望",effective 是系统算的"现实"**——这样设计让 hotplug 和层级约束自动收敛,不需要用户每次手动改。

每次写 `cpuset.cpus`,核心流程在 [`cpuset_write_resmask`](../linux/kernel/cgroup/cpuset.c#L3582)→[`update_cpumask`](../linux/kernel/cgroup/cpuset.c#L2422):

```c
/* kernel/cgroup/cpuset.c:3582-3648(简化) */
static ssize_t cpuset_write_resmask(struct kernfs_open_file *of,
                                    char *buf, size_t nbytes, loff_t off)
{
    struct cpuset *cs = css_cs(of_css(of));
    struct cpuset *trialcs;

    /* 等待之前的 hotplug 工作完成,避免和迁移任务竞争 */
    css_get(&cs->css);
    kernfs_break_active_protection(of->kn);
    flush_work(&cpuset_hotplug_work);

    cpus_read_lock();                  /* 全局 CPU 读锁(percpu_rw_semaphore) */
    mutex_lock(&cpuset_mutex);         /* cpuset 主锁 */
    if (!is_cpuset_online(cs))
        goto out_unlock;

    trialcs = alloc_trial_cpuset(cs);  /* 在副本上试验,不改原 */

    switch (of_cft(of)->private) {
    case FILE_CPULIST:
        retval = update_cpumask(cs, trialcs, buf);
        break;
    case FILE_MEMLIST:
        retval = update_nodemask(cs, trialcs, buf);
        break;
    ...
    }
    ...
}
```

([cpuset.c:3582-3648](../linux/kernel/cgroup/cpuset.c#L3582-L3648))

`trialcs` 是个**副本**——cpuset 的标准模式是"在副本上做所有验证和计算,验证通过再用 `callback_lock` 保护地拷回原"。`update_cpumask`([cpuset.c:2422](../linux/kernel/cgroup/cpuset.c#L2422))做四件事:① 解析用户写的 cpu 列表;② `validate_change` 检查(不能把兄弟 cgroup 的独占 CPU 抢了、不能让父变成空等);③ 在 `callback_lock` 下拷 configured;④ 调 `update_cpumasks_hier` 重新计算整棵子树的 effective,下发到任务。

### 14.4.3 技巧:下发到任务的 `cpus_ptr`

光改 cgroup 的掩码没用,真正让任务跑在指定 CPU 上的是 `task_struct->cpus_ptr`(任务可调度的 CPU 掩码,调度器选 CPU 时查它)。所以 cpuset 改完掩码后,要**遍历组内所有任务,调 `set_cpus_allowed_ptr` 改它们的 cpus_ptr**:

```c
/* kernel/cgroup/cpuset.c:1283-1306(简化) */
static void update_tasks_cpumask(struct cpuset *cs, struct cpumask *new_cpus)
{
    struct css_task_iter it;
    struct task_struct *task;
    bool top_cs = cs == &top_cpuset;

    css_task_iter_start(&cs->css, 0, &it);
    while ((task = css_task_iter_next(&it))) {
        const struct cpumask *possible_mask = task_cpu_possible_mask(task);

        if (top_cs) {
            if (kthread_is_per_cpu(task))
                continue;                    /* per-CPU kthread 不动 */
            cpumask_andnot(new_cpus, possible_mask, subpartitions_cpus);
        } else {
            cpumask_and(new_cpus, possible_mask, cs->effective_cpus);
        }
        set_cpus_allowed_ptr(task, new_cpus);  /* <- 真正下发 */
    }
    css_task_iter_end(&it);
}
```

([cpuset.c:1283-1306](../linux/kernel/cgroup/cpuset.c#L1283-L1306))

`set_cpus_allowed_ptr` 是调度器接口,它会:① 改 `task_struct->cpus_ptr`;② 如果任务正在跑且当前 CPU 不在新掩码里,通过 IPI 或 `migrate_task` 把它迁到允许的 CPU;③ 处理 SCHED_DEADLINE 带宽重算(`dl_bw`)。这一套细节超出了 cpuset 本身,关键是要理解:**cpuset 写文件最终落到每个任务的 `cpus_ptr`,调度器才认**。

`update_tasks_cpumask` 由 `update_cpumasks_hier`([cpuset.c:2181](../linux/kernel/cgroup/cpuset.c#L2181))沿子树调用,自上而下重算每个后代的 effective_cpus 并下发到该后代的所有任务。

### 14.4.4 attach:任务迁进来时也要下发

把一个进程写进 cpuset(写 `cgroup.procs`),如果这个 cpuset 的掩码和源不一样,要把这个任务的 `cpus_ptr` 和 `mems_allowed` 改到目标 cpuset 的 effective 上。这走 [`cpuset_can_attach`](../linux/kernel/cgroup/cpuset.c#L3271)→[`cpuset_attach`](../linux/kernel/cgroup/cpuset.c#L3397):

```c
/* kernel/cgroup/cpuset.c:3271-3345(简化) */
static int cpuset_can_attach(struct cgroup_taskset *tset)
{
    ...
    ret = cpuset_can_attach_check(cs);   /* 目标 cpuset 不能是空掩码 */
    if (ret)
        goto out_unlock;
    ...
    cgroup_taskset_for_each(task, css, tset) {
        ret = task_can_attach(task);     /* 检查任务能否迁(SCHED_DEADLINE 带宽够不够) */
        if (ret)
            goto out_unlock;
        ...
        if (dl_task(task)) {
            cs->nr_migrate_dl_tasks++;
            cs->sum_migrate_dl_bw += task->dl.dl_bw;
        }
    }

    /* SCHED_DEADLINE 任务跨 cpuset 迁移要重算带宽,够才让迁 */
    if (!cpumask_intersects(oldcs->effective_cpus, cs->effective_cpus)) {
        ...
        ret = dl_bw_alloc(cpu, cs->sum_migrate_dl_bw);   /* -EBUSY 就拒绝 */
        ...
    }

    cs->attach_in_progress++;    /* 关键:标记"正在 attach" */
    ...
}
```

([cpuset.c:3271-3345](../linux/kernel/cgroup/cpuset.c#L3271-L3345))

注意 `cs->attach_in_progress++`:这是为了在 `can_attach` 到 `attach` 之间,**阻止别人把这个 cpuset 的 cpus/mems 清零**(`validate_change` 会查 `attach_in_progress` 拒绝)。否则任务刚通过 can_attach 检查,目标 cpuset 掩码就被清空了,attach 时任务无处可去——经典 TOCTOU。

实际的 attach 在 [`cpuset_attach_task`](../linux/kernel/cgroup/cpuset.c#L3378)([cpuset.c:3378](../linux/kernel/cgroup/cpuset.c#L3378)):

```c
/* kernel/cgroup/cpuset.c:3378-3395(简化) */
static void cpuset_attach_task(struct cpuset *cs, struct task_struct *task)
{
    if (cs != &top_cpuset)
        guarantee_online_cpus(task, cpus_attach);
    else
        cpumask_andnot(cpus_attach, task_cpu_possible_mask(task),
                       subpartitions_cpus);

    WARN_ON_ONCE(set_cpus_allowed_ptr(task, cpus_attach));  /* 改 cpus_ptr */

    cpuset_change_task_nodemask(task, &cpuset_attach_nodemask_to);  /* 改 mems_allowed */
    cpuset_update_task_spread_flags(cs, task);  /* 同步 spread_page/spread_slab */
}
```

([cpuset.c:3378-3395](../linux/kernel/cgroup/cpuset.c#L3378-L3395))

attach 还会处理内存迁移:如果 `CS_MEMORY_MIGRATE` 标志在,任务从老 cpuset 迁到新 cpuset 时,它的 mm 里在老 `mems_allowed` 上的页会被 migrate 到新 `mems_allowed`(走 `cpuset_migrate_mm`,见 [cpuset.c:3457](../linux/kernel/cgroup/cpuset.c#L3457) 的调用)。这是 cpuset 独有的重活——其他 controller attach 时只改计数,cpuset 可能要动页。

### 14.4.5 fork 路径:cpuset 的 can_fork 何时触发

注意 [`cpuset_cgrp_subsys`](../linux/kernel/cgroup/cpuset.c#L4279-L4296) 注册了 `can_fork`/`cancel_fork`/`fork`([cpuset.c:4279](../linux/kernel/cgroup/cpuset.c#L4279)):

```c
/* kernel/cgroup/cpuset.c:4279-4296 */
struct cgroup_subsys cpuset_cgrp_subsys = {
    .css_alloc     = cpuset_css_alloc,
    .css_online    = cpuset_css_online,
    .css_offline   = cpuset_css_offline,
    .css_free      = cpuset_css_free,
    .can_attach    = cpuset_can_attach,
    .cancel_attach = cpuset_cancel_attach,
    .attach        = cpuset_attach,
    .post_attach   = cpuset_post_attach,
    .bind          = cpuset_bind,
    .can_fork      = cpuset_can_fork,
    .cancel_fork   = cpuset_cancel_fork,
    .fork          = cpuset_fork,
    .legacy_cftypes = legacy_files,
    .dfl_cftypes   = dfl_files,
    .early_init    = true,        /* boot 时就给 init 任务挂上 */
    .threaded      = true,        /* 支持 threaded cgroup */
};
```

([cpuset.c:4279-4296](../linux/kernel/cgroup/cpuset.c#L4279-L4296))

但 cpuset 的 `can_fork`([cpuset.c:4185](../linux/kernel/cgroup/cpuset.c#L4185))有个快速路径:**如果父子在同一 cpuset(`same_cs`),直接返回 0,不做检查**。绝大多数 fork 是父子同 cpuset,这条快速路径让 cpuset 几乎对 fork 零开销。只有 fork 时显式指定 `cgroup`(`CLONE_INTO_CGROUP`,把子进程直接 fork 到别的 cgroup)才走完整 can_attach 检查路径。

子进程的 cpumask 是在 `cpuset_fork`([cpuset.c:4248](../linux/kernel/cgroup/cpuset.c#L4248))里设的:`set_cpus_allowed_ptr(task, current->cpus_ptr)`——简单继承父的 cpus_ptr。

`early_init: true` 这个字段也很关键:它让 cpuset 在 boot 早期(其他子系统还没起来)就给 init 任务挂上 css,这样内核启动过程中所有 fork 出来的内核线程都自动归到 `top_cpuset`,不会"无主"。

### 14.4.6 partition root:cgroup v2 的"硬隔离"

cgroup v2 的 cpuset 有个 v1 没有的高级特性叫 **partition root**。普通的 cpuset 改 `cpuset.cpus` 只是改"任务能在哪跑",但 CPU 仍然参与全局负载均衡——别的 cgroup 的任务也可能被调度到这些 CPU 上(只是本 cgroup 的任务不能跑出去)。partition root 提供更强的隔离:

```c
/* kernel/cgroup/cpuset.c:226-230 */
#define PRS_MEMBER          0    /* 普通成员 */
#define PRS_ROOT            1    /* partition root,独占这些 CPU + 重建调度域 */
#define PRS_ISOLATED        2    /* partition root 但不参与负载均衡 */
#define PRS_INVALID_ROOT   -1    /* 配置冲突,变成无效态 */
#define PRS_INVALID_ISOLATED -2
```

([cpuset.c:226-230](../linux/kernel/cgroup/cpuset.c#L226-L230))

把一个 cgroup 设成 partition root(写 `cpus.partition = root`),它会:

1. 把配的 `cpuset.cpus` 从父的 `effective_cpus` 里**减掉**(这些 CPU 不再参与父的负载均衡)。
2. 用这些 CPU **重建一个独立的调度域**(`rebuild_sched_domains_locked`),只在这个 partition 内部做负载均衡。
3. 别的 cgroup 的任务**无法调度到这些 CPU**。

这是真正的"硬隔离 CPU"——数据库容器、实时容器想要独占几个核,就靠这个。`PRS_ISOLATED` 更狠:连内部负载均衡都不要,任务只在被显式唤醒时才跑(适合绑核的实时任务、轮询型 worker)。这部分细节很深(PRS 状态机、exclusive_cpus、remote partition),本书不展开,知道 partition root 是 cpuset 在 v2 的"硬隔离 CPU"能力即可。

---

## 14.5 技巧精解:freezer 的"陷阱位 + 自睡 + 状态机"

这一章的硬核技巧不在 pids(它就是个层级计数器,套路和 memcg 几乎一样),而在 **freezer 的 `JOBCTL_TRAP_FREEZE` + `task->frozen` + `nr_frozen_tasks`/`nr_frozen_descendants` 三件套**。这套设计解决了"如何安全、整组、抗 fork race 地冻结任务"这个本质难题。

### 反面对比一:朴素方案 = 逐个 SIGSTOP

如果朴素地写,要冻一个 cgroup:

```c
/* 朴素的、糟糕的写法(示意,非源码) */
css_task_iter_start(&cgrp->self, 0, &it);
while ((task = css_task_iter_next(&it))) {
    send_sig(SIGSTOP, task, 1);    /* 给每个任务发 SIGSTOP */
}
css_task_iter_end(&it);
```

这撞上 14.3.1 列的全部坑:① race 期间新 fork 不在迭代列表里,漏冻;② SIGSTOP 强制停,持有锁的任务死锁;③ 解冻时逐个 SIGCONT,顺序错的话可能再次死锁;④ 无法判定"整组何时冻透"——只能 `waitpid` 每个任务,但任务在 pid ns 里不可见。

### 反面对比二:朴素方案 = 内核主动改任务状态

进阶一点:不靠信号,内核直接 `set_current_state(TASK_STOPPED)` 把每个任务停了。这避免了 SIGSTOP 的信号路径问题,但还是撞墙:① 改任务状态要拿任务锁,大量任务时锁竞争剧烈;② 任务可能在内核态任意位置(持锁中),强行改状态会破坏内核不变量;③ 无法让任务"先释放锁再睡"——它已经在某个持锁的内核路径里卡住。

### 6.9 的正确解法:协作式自睡

freezer 的真实做法是**把"决定睡"的权力交还给任务自己**:

1. **埋陷阱**:freezer 在每个任务的 `task->jobctl` 上设 `JOBCTL_TRAP_FREEZE` 位(`cgroup_freeze_task`@freezer.c:155),并 `signal_wake_up` 把它叫醒(让它尽快进信号路径)。
2. **任务自己睡**:任务下次从内核返回用户态(或被叫醒后 schedule),在 `do_signal`→`get_signal` 里看到陷阱位,调 `do_freezer_trap`(signal.c:2580)——`__set_current_state(TASK_INTERRUPTIBLE|TASK_FREEZABLE)` + `cgroup_enter_frozen()` + `schedule()`。**任务在 `schedule()` 之前,已经走完了它当时所在的内核路径,释放了所有持有的锁**。
3. **状态机记账**:`cgroup_enter_frozen` 把 `task->frozen` 设 1,bump 所在 cgroup 的 `nr_frozen_tasks`,调 `cgroup_update_frozen` 判定整组是否冻透(`nr_frozen_tasks == __cgroup_task_count`)。
4. **抗 fork race**:cgroup 的 `CGRP_FREEZE` flag 是组级属性,新 fork 的子进程在 `cgroup_post_fork` 里(cgroup.c:6616)被同样设上 `JOBCTL_TRAP_FREEZE`,一调度就睡——**漏不掉**。
5. **解冻**:写 `cgroup.freeze=0` 走同一套传播链(`e_freeze--`),`cgroup_freeze_task(task, false)` 清陷阱位 + `wake_up_process`,任务从 `schedule()` 醒来,`cgroup_leave_frozen` 把 `task->frozen` 清 0、dec `nr_frozen_tasks`。

这个设计的精妙之处在于**"决定冻结"和"执行冻结"分离**:cgroup 子系统只负责"标记任务应该被冻",任务自己负责"在合适的时机真的冻"。cgroup 不需要知道任务当前在内核哪里、持有什么锁;任务也不需要知道是哪个 cgroup 在冻它——它只看自己的 `JOBCTL_TRAP_FREEZE` 位。

### 状态机的两个层级

freezer 维护两个计数器,分别对应"组级冻透"和"子树级冻透":

```
 cgroup_freezer_state(简化):

 ┌────────────────────────────────────────────────────┐
 │ freeze           (bool)  用户配的:这个组该不该冻    │
 │ e_freeze         (int)   引用计数:有几个祖先在冻我  │
 │ nr_frozen_descendants (int) 子树里有多少个冻透的 cgroup │
 │ nr_frozen_tasks       (int) 这个组里有多少个冻/STOP/ptrace 的任务│
 └────────────────────────────────────────────────────┘
```

- **`nr_frozen_tasks`** 判定**本 cgroup 是否冻透**:`cgroup_update_frozen` 算 `frozen = test_bit(CGRP_FREEZE) && nr_frozen_tasks == __cgroup_task_count`。注意"任务"包括 frozen、SIGSTOPped、被 ptrace 的(都算"不会跑了")。
- **`nr_frozen_descendants`** 判定**祖先 cgroup 是否间接冻透**:一个 cgroup 冻透,`cgroup_propagate_frozen`(freezer.c:14) 把它的所有祖先的 `nr_frozen_descendants++`,当某祖先的 `nr_frozen_descendants == nr_descendants` 时,这个祖先也算冻透(它的整个子树都冻了)——设 `CGRP_FROZEN` 位,`cgroup.events` 的 `frozen` 字段变 1。

这套双层计数器让用户态可以精确知道:**"我冻结 /a 后,/a 的整个子树都冻透了吗?"**——poll `/a/cgroup.events` 等 `frozen 1` 即可。CRIU、K8s 的 pod freeze 都靠这个。

> **钉死这件事**:cgroup freezer 的"协作式自睡"是 cgroup v2 设计美学的典范——**不强制改任务状态,只在任务的 jobctl 上埋一个陷阱位,让任务自己进信号路径把自己 schedule 出去**。这同时解决了"持锁任务不死锁""抗 fork race""整组原子""可观测"四个难题。对比之下,SIGSTOP 是"从外部把任务按停",粗糙且危险。

---

## 章末小结

这一章把第 2 篇剩下的三个 controller 讲完了,第 2 篇就此完整:`cpu.max`(P2-11)、`memory.max`(P2-12)、`io.max`(P2-13)、`pids.max`、`cgroup.freeze`、`cpuset.cpus/mems`——容器资源控制的全部六类原语。

回到二分法:**这三个 controller 全部服务"资源"这一面**。但它们限的"资源"形态各异:

- **pids 限的是个数**(整数计数器):嵌进 fork 路径的 `can_fork`,层级原子计数,超了 fork 返回 `-EAGAIN`。
- **freezer 限的是静止**(状态机):在 jobctl 上埋陷阱位,让任务自己睡着,抗 fork race。
- **cpuset 限的是位置**(掩码):configured/effective 两套掩码,改完下发到每个任务的 `cpus_ptr`,partition root 还能重建调度域。

三个 controller 都遵守 cgroup v2 的层级模型(子树累加/祖先约束),都通过 `struct cgroup_subsys` 的函数指针表接入核心(can_fork/can_attach/attach/css_alloc)。pids 和 cpuset 是独立 controller,freezer 在 v2 已经"晋升"为 cgroup 核心功能(直接挂在 `struct cgroup` 上,不走 controller 注册)。

### 五个"为什么"清单

1. **为什么 `pids_try_charge` 是"先加后查"而不是"先查后加"?** 为了并发正确性。两个并发 fork 同时 charge 同一个祖先,只有"先 `atomic64_add_return` 加,再查超限,失败回滚"能保证不漏判。先查后加会在查和加之间有窗口,两个并发者都通过但加完已超限。

2. **为什么 freezer 不直接发 SIGSTOP?** 四个原因:SIGSTOP 按进程发、到不了整组;race 期新 fork 漏冻;强制停任务会死锁(持锁任务没法释放);解冻顺序难保证。freezer 用 `JOBCTL_TRAP_FREEZE` 让任务**自己**进信号路径 schedule 出去,协作式而非强制式。

3. **为什么 cpuset 有 configured 和 effective 两套掩码?** 因为用户配的可能和祖先冲突或受 hotplug 影响。effective = configured & parent's effective,自动收敛。这样 hotplug(拔 CPU)和层级约束(父限了 0-7,子配 2-10 自动变 2-7)都不需要用户手动调整。

4. **为什么 6.9 不再用 `TIF_FROZEN`?** 5.x 重构后,冻结状态记录在 `task->frozen:1`(sched.h:945)位字段 + `TASK_FREEZABLE` 任务状态(signal.c:2600),不再用 thread_info 的 `TIF_FROZEN` flag。这是为了和"可中断睡眠但 freezer 跳过"的语义对齐。老资料讲的 `TIF_FROZEN` 在 v2/6.9 已废弃。

5. **为什么 cpuset 在 v2 还是个独立 controller,而 freezer 不是?** cpuset 有自己的 css(每 cgroup 一个 `struct cpuset`,维护掩码、partition_root_state、fmeter 等),状态重、回调多(can_fork/attach/css_alloc/bind),必须是独立 controller。freezer 状态轻(就 4 个字段),所有 cgroup 都用得上,放 `struct cgroup` 里更直接,不必走 controller 注册——v2 把"通用功能核心化、专用功能 controller 化"的设计哲学体现得淋漓尽致。

### 想继续深入往哪钻

- **pids**:`kernel/cgroup/pids.c` 全文就 388 行,建议通读。重点看 [`pids_try_charge`](../linux/kernel/cgroup/pids.c#L158)(层级计数核心)、[`pids_can_fork`](../linux/kernel/cgroup/pids.c#L238)(fork 钩子)、[`pids_can_attach`](../linux/kernel/cgroup/pids.c#L191)(迁移钩子,注意用 charge 不用 try_charge 的取舍)。
- **freezer**:`kernel/cgroup/freezer.c` 全文 323 行,通读。重点看 [`cgroup_freeze`](../linux/kernel/cgroup/freezer.c#L260)(e_freeze 引用计数传播)、[`cgroup_do_freeze`](../linux/kernel/cgroup/freezer.c#L177)(任务级冻结)、[`cgroup_update_frozen`](../linux/kernel/cgroup/freezer.c#L52)(冻透判定)。配合 `kernel/signal.c` 的 [`do_freezer_trap`](../linux/kernel/signal.c#L2580)(任务自睡)和 `kernel/cgroup/cgroup.c` 的 [`cgroup_post_fork`](../linux/kernel/cgroup/cgroup.c#L6585)(fork race 补救)。
- **cpuset**:`kernel/cgroup/cpuset.c` 有 5117 行,是 cgroup v2 最复杂的 controller。建议先看 [`struct cpuset`](../linux/kernel/cgroup/cpuset.c#L94)(两套掩码)、[`cpuset_write_resmask`](../linux/kernel/cgroup/cpuset.c#L3582)(写入入口)、[`update_cpumask`](../linux/kernel/cgroup/cpuset.c#L2422)、[`update_tasks_cpumask`](../linux/kernel/cgroup/cpuset.c#L1283)(下发到任务)、[`cpuset_attach`](../linux/kernel/cgroup/cpuset.c#L3397)(迁移)。partition root 部分看 [`update_prstate`](../linux/kernel/cgroup/cpuset.c#L3042) 和 [`generate_sched_domains`](../linux/kernel/cgroup/cpuset.c#L956)。
- **观测**:`cat /sys/fs/cgroup/<path>/pids.current`、`cat .../cgroup.events`(看 `frozen 1`)、`cat .../cpuset.cpus` 和 `cpuset.cpus.effective`(对比 configured vs effective)。手动实验:建一个 cgroup,设 `pids.max=10`,写个 `bash -c 'while true; do sleep 1000 & done'` 进去看 fork 几个就 EAGAIN;`echo 1 > cgroup.freeze` 冻结后 `ps` 看任务状态变成 D/T。
- **延伸**:CRIU(checkpoint/restore)用 freezer 整组冻住再 dump;K8s 的 CPU manager `static` 策略用 cpuset 给 Guaranteed pod 独占 CPU;systemd 的 `CPUAffinity=` 底层也是 cpuset。

### 引出下一章

第 2 篇(cgroup 资源控制)到此结束。回头看,我们讲了 `css_set`(P2-09)怎么把任务和 cgroup 多对多挂起来、`cgroup_attach_task`(P2-10)怎么四步迁移、cpu/memory/io/pids/freezer/cpuset(P2-11~14)各自限什么资源。但这些都是"在已经造好的 cgroup 树上"做事——**容器是怎么"造"出来的?一次 `clone(CLONE_NEWNS | CLONE_NEWPID | CLONE_NEWNET | ...)` 内核到底做了什么、`unshare` 和 `setns` 怎么改视图、写 `cgroup.procs` 怎么把进程关进限额盒子、`pivot_root` 怎么换根?** 这些问题的答案在第 3 篇:容器组装。下一章 P3-15,我们从 [`kernel/fork.c`](../linux/kernel/fork.c) 的 `copy_namespaces`([L2393](../linux/kernel/fork.c#L2393))和 [`kernel/nsproxy.c`](../linux/kernel/nsproxy.c) 的 `create_new_namespaces`([L67](../linux/kernel/nsproxy.c#L67))讲起,正式进入"组装篇"——内核积木怎么拼成容器。
