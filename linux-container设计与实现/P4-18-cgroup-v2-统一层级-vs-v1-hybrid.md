# 第十八章 · cgroup v2 统一层级 vs v1 hybrid

> 篇:P4 cgroup 进阶
> 主线呼应:前 6 章(第 2 篇 P2-09~14)我们把 cgroup 的内部骨架(`css_set` 去重表、`cgroup_attach_task` 四步迁移、cpu/memcg/io/pids/freezer/cpuset 各 controller)拆了一遍。但你回头敲 `ls /sys/fs/cgroup/`,会看到两套并存的目录:`/sys/fs/cgroup/`(v2 默认层级)+ `/sys/fs/cgroup/memory`、`/sys/fs/cgroup/cpu,cpuacct`、`/sys/fs/cgroup/blkio`(v1 各 controller 各挂一棵)。这背后是 cgroup 历史上最痛苦的一次架构转向——从 v1 的"每 controller 一棵树"(hybrid,任务归属可以任意分裂)到 v2 的"所有 controller 共用一棵树"(unified,归属唯一)。本章讲清这次转向**解决了什么硬骨头、又用什么新约束(`no internal process`)堵住 v2 单一树带来的新问题**。读完你才能解释一件怪事:为什么 v2 里一个**有进程在里面跑的** cgroup,**禁止**用 `cgroup.subtree_control` 启用任何 controller。

---

## 核心问题

**为什么 cgroup v2 强行规定"所有 controller 共用同一棵树"?v1 那种"cpu 一个树、memory 另一个树、一个进程在两个树里各属一个 cgroup"的灵活做法,撞上了什么不可调和的矛盾?v2 用 `no internal process` 这个看似古怪的约束,又是在堵什么漏洞?**

读完本章你会明白:

1. v1 hybrid 的根本病根:**一个进程在多个 controller 树里归属不同 cgroup,语义矛盾、状态撕裂**——`cpu cgroup A` 说它是 A 组,`memory cgroup B` 说它是 B 组,记账到底归谁、迁移怎么迁、控制策略谁优先,全是悬案。
2. v2 unified 的解药:**所有 controller 共用同一棵 cgroup 树,一个进程在树上有唯一归属点**——归属一致,矛盾消失。
3. `no internal process` 约束的必要性:**单一树带来新问题**(中间 cgroup 既有进程又启用 controller,进程和子 cgroup 在同一层抢资源、记账含糊),v2 用"启用 controller 的 cgroup 不许住进程"堵住。
4. `cgroup.type`(domain/threaded)是 `no internal process` 的逃生口:某些场景(进程的多个线程要分开限 CPU)必须让进程住进启用 controller 的层,v2 提供 threaded 子树绕开约束。

> **逃生阀**:如果本章术语太密,先记住三句话——① v1 多树,归属可分裂,有矛盾;② v2 一棵树,归属唯一,矛盾消失,但带来"中间层有进程就不能启用 controller"的新约束;③ threaded 模式是这条约束的逃生口,让多线程进程也能按线程分限额。

---

## 18.1 一句话点破

> **cgroup v2 的核心架构转向,是把 v1"每个 controller 一棵独立树"改成"所有 controller 共用同一棵树"——这不是审美洁癖,而是 v1 让一个进程在 cpu 树属 A、memory 树属 B 的"分裂归属"产生了不可调和的记账/迁移/策略矛盾;但单一树带来一个 v1 没有的新问题——中间 cgroup 既要住进程又要启 controller 时父子争抢同一份资源,v2 用 `no internal process` 约束(启用 controller 的层不许住进程)一刀切开,再开 `threaded` 这个口子给多线程场景逃生。**

这是结论,不是理由。本章倒过来拆:先看 v1 hybrid 到底把人逼到什么墙角,再看 v2 怎么用单一树 + `no internal process` 一举解决,然后钻进 `struct cgroup_root`、`cgroup_apply_control`、`cgroup_migrate_vet_dst` 这三处源码看清 enforcement,最后讲 threaded 这条逃生口。

---

## 18.2 v1 hybrid 到底哪里错:一个进程,N 个归属

要理解 v2 为什么要统一树,先看 v1 的 mount 模型。

### v1 的多树挂载

cgroup v1 的挂载接口允许每个 controller **单独挂一棵树**。你完全可以这么挂:

```bash
# v1 的典型挂载(每个 controller 一个 superblock)
mount -t cgroup -o cpu,cpuacct none /sys/fs/cgroup/cpu,cpuacct
mount -t cgroup -o memory      none /sys/fs/cgroup/memory
mount -t cgroup -o blkio       none /sys/fs/cgroup/blkio
mount -t cgroup -o devices     none /sys/fs/cgroup/devices
mount -t cgroup -o freezer     none /sys/fs/cgroup/freezer
```

每条 `mount -t cgroup -o <controller>` 调用到内核 [`cgroup1_get_tree`](../linux/kernel/cgroup/cgroup-v1.c#L1233)([cgroup-v1.c:1233](../linux/kernel/cgroup/cgroup-v1.c#L1233)),创建一个独立的 [`struct cgroup_root`](../linux/include/linux/cgroup-defs.h#L556)([cgroup-defs.h:556](../linux/include/linux/cgroup-defs.h#L556))——这个 root 结构里 `subsys_mask`@560 是"挂在这棵树上的 controller 位图"、`hierarchy_id`@563 是这棵树的唯一 ID。一棵 v1 树只挂一组 controller,所以叫**hybrid**(混合,各 controller 各一棵,可以任意组合)。

然后你可以在**不同**的树上,把同一个进程归到**不同**的子目录:

```bash
mkdir /sys/fs/cgroup/cpu,cpuacct/containerA
mkdir /sys/fs/cgroup/cpu,cpuacct/containerB
mkdir /sys/fs/cgroup/memory/containerC

# 同一个进程 PID 1234
echo 1234 > /sys/fs/cgroup/cpu,cpuacct/containerA/cgroup.procs   # cpu 上属 A
echo 1234 > /sys/fs/cgroup/memory/containerC/cgroup.procs        # memory 上属 C
```

这下 PID 1234 的归属变成了:**cpu 维度属 A,memory 维度属 C**。这个进程的 `task_struct->cgroups` 指针指向的 `css_set` 里,`subsys[cpu]` 指向 A 的 cpu css,`subsys[memory]` 指向 C 的 memcg css——[`css_set.subsys[]`](../linux/include/linux/cgroup-defs.h#L223)([cgroup-defs.h:223](../linux/include/linux/cgroup-defs.h#L223))是个 15 槽数组,每槽独立。这正是第 9 章(P2-09)讲 `css_set` 时我们留的一个伏笔——v1 的 `css_set` 是真正"每个 controller 各自一组",而 v2 大部分时候所有 css 都在同一棵树的同一个节点上。

### 病根一:语义矛盾——这个进程"到底是谁的"

这个分裂归属立即产生一堆讲不清的问题:

| 问题 | 具体矛盾 |
|------|----------|
| **资源记账归谁?** | PID 1234 用了 1GB 内存,是算到 A(它 cpu 在这)还是 C(它 memory 在这)?"容器 A 用了多少内存"这种 K8s 最关心的统计,在 v1 hybrid 下根本问不清。 |
| **迁移怎么迁?** | 你想把 PID 1234 这个"容器"整体迁到新节点,但它在 cpu 树属 A、memory 树属 C,得分别写两个 cgroup.procs,两次迁移之间存在窗口期——进程"半迁"状态。 |
| **限额策略冲突?** | A 设了 `cpu.cfs_quota=50%`,C 设了 `memory.limit=512M`,这个进程的"限额"是 50% CPU + 512M 内存,但任何一个 controller 的 cgroup 看到的都只是它自己那一面——运维要拼出完整画像得跨多个目录。 |
| **OOM 杀谁?** | C 触发 OOM,内核按 memcg 树找进程来杀,但和它 cpu 归属无关,可能杀掉"按 cpu 视角看不该死的进程"。 |

这种"一个进程有 N 个身份"是 v1 hybrid 的根本病根。生产环境里 K8s 想给每个 pod 一个清晰的"它在哪、用了多少、超了怎么办"的画像,在 v1 hybrid 下根本做不到。

### 病根二:层级结构不一致——资源拓扑语义撕裂

更糟的是,每棵树的**目录结构**可以各不相同。你可以这么玩:

```
cpu 树:    /A_subsonic/B_web/C_db
memory 树: /X_big/Y_small
```

同一个进程在 cpu 树的深度是 3(A_subsonic/B_web/C_db),在 memory 树的深度是 2(X_big/Y_small)。"父子继承"的语义(子 cgroup 默认继承父 cgroup 的限额)在不同 controller 上的**拓扑都不一样**——`cpu.cfs_quota` 在 cpu 树里按 A→B→C 累加,`memory.limit` 在 memory 树里按 X→Y 累加,两个累加路径毫无关系。

这导致一个直观的"容器 = 一个目录"的诉求完全落空:你不能指着任何一个目录说"这是容器 A 的 cgroup"——容器 A 的 cpu 在一棵树上、memory 在另一棵树上,根本不是同一个目录。

> **不这样会怎样**:如果你硬要在 v1 hybrid 上跑 K8s,会出现这种荒唐事——某个 pod 的 CPU 用量来自 cpu 树的 `/pod-xyz` 目录,内存用量来自 memory 树的 `/burstable/pod-xyz` 目录,两个路径都是 pod-xyz 但**完全独立**,改一个不影响另一个。更可怕的是 freezer(冻结整组)这种需要"原子地把这个进程组所有 controller 状态一起冻结"的操作,在 v1 下根本做不到——freezer 自己一棵树,冻的是"按 freezer 树看的组",和 cpu/memory 树的组没对齐关系,freeze 期间进程的 cpu/memory 状态可能在另一棵树上继续变化。**v1 hybrid 的根本病灶,是"进程的归属分裂在 N 棵树上,而没有任何机制强制它们一致"。**

---

## 18.3 v2 的解药:一棵统一的树

cgroup v2(2016 年 4.5 起稳定,4.x~5.x 持续演进,本书以 6.9 为准)就是来根治这个病的。它的根本改动是:

> **所有 controller 共用同一棵 cgroup 树,叫 default hierarchy(unified hierarchy)。一个进程在这棵树上**有唯一归属点**,它在 cpu/memory/io/pids 各 controller 上的 css,都来自这个唯一归属点(或其祖先)。**

源码层面,这棵统一树的根是 [`cgrp_dfl_root`](../linux/kernel/cgroup/cgroup.c#L167)([cgroup.c:167](../linux/kernel/cgroup/cgroup.c#L167)):

```c
/* kernel/cgroup/cgroup.c:167(简化) */
struct cgroup_root cgrp_dfl_root = { .cgrp.rstat_cpu = &cgrp_dfl_root_rstat_cpu };
```

这是一个全局变量(不是 mount 出来的),在内核 boot 时由 `cgroup_init` 初始化,挂在 `/sys/fs/cgroup/`(注意没有 controller 后缀,就是统一根)。`/sys/fs/cgroup/` 下面的子目录(`/burstable/pod-xyz` 这种),才叫 cgroup,所有 controller 共用。

判断一个 cgroup 是不是在统一树上,用 [`cgroup_on_dfl`](../linux/kernel/cgroup/cgroup.c#L321)([cgroup.c:321](../linux/kernel/cgroup/cgroup.c#L321)):`return cgrp->root == &cgrp_dfl_root;`——一行比较 `cgroup->root` 指针是不是 `&cgrp_dfl_root`。v1 mount 出来的 cgroup,`cgrp->root` 指向另一个 `cgroup_root`,`cgroup_on_dfl` 返回 false。

### 关键约束:controller 不是默认全启用,要按子树启用

v2 的另一条核心规则——一棵树共用,但**不是每个 cgroup 都自动启用所有 controller**。每个 cgroup 通过 `cgroup.subtree_control` 这个**接口文件**显式声明:"我这个 cgroup 的子树里要启用哪些 controller"。

```bash
# 在根上启用 cpu 和 memory(对根的子树生效)
echo "+cpu +memory" > /sys/fs/cgroup/cgroup.subtree_control
# 现在所有直接子 cgroup 自动获得 cpu/memory 的 css
mkdir /sys/fs/cgroup/containerA
ls /sys/fs/cgroup/containerA/   # 里面会出现 cpu.max、memory.max 等接口
```

这是 v2 和 v1 的一个本质区别:v1 是 mount 时决定哪些 controller 挂这棵树(`-o cpu,cpuacct`),v2 是 mount 后**动态**地按子树启用(`echo +cpu > subtree_control`)。源码上,这个动作走进 [`cgroup_subtree_control_write`](../linux/kernel/cgroup/cgroup.c#L3369)([cgroup.c:3369](../linux/kernel/cgroup/cgroup.c#L3369))——它解析 `+cpu -memory` 这种 token,然后调 [`cgroup_apply_control`](../linux/kernel/cgroup/cgroup.c#L3293)([cgroup.c:3293](../linux/kernel/cgroup/cgroup.c#L3293))应用变更。

`cgroup.subtree_control` 的位图记录在 [`cgroup->subtree_control`](../linux/include/linux/cgroup-defs.h#L460)([cgroup-defs.h:460](../linux/include/linux/cgroup-defs.h#L460)),这是个 `u16`(15 个 controller 用 16 位足够)。`+cpu` 就是把 `subtree_control` 里 cpu 那一位置 1,`-memory` 就是清掉 memory 那一位。`cgroup_apply_control` 拿到新位图后,会**遍历这棵子树**,为每个后代 cgroup 创建/隐藏对应 controller 的 css(后面 18.5 详讲)。

> **钉死这件事**:v2 统一树不是"所有 cgroup 都启用所有 controller",而是"**所有 controller 共用同一棵树,但每个 cgroup 自己用 `cgroup.subtree_control` 决定它的子树启用哪些 controller**"。前者(共用一棵树)解决"分裂归属"病根;后者(按子树启用)是 v2 为了让 controller 启用可以**动态、按需、分层**配置而引入的灵活性。两者一起,才让 v2 既能保证"一个进程唯一归属",又能让运维按需把不同子树配置成不同 controller 组合(如某些子树只关心 cpu,另一些只关心 io)。

### v1 兼容:hybrid 模式

注意:Linux 内核**同时**支持 v1 和 v2,可以并存(叫 hybrid 模式,不是 v1 那个 hybrid)。boot 参数 `cgroup_no_v1=memory`、`systemd.unified_cgroup_hierarchy=1` 控制用哪套。systemd 默认 v2(2019 年起),Docker/K8s 也都已默认 v2,但老系统仍是 v1。内核里的 `cgrp_dfl_root`(v2)永远在,v1 mount 创建的额外 `cgroup_root` 也都在——它们并存,但一个 cgroup 要么在 dfl(`cgroup_on_dfl` 为真),要么在某个 v1 root 上,不会跨界。

这也是为什么 [`struct cgroup_root`](../linux/include/linux/cgroup-defs.h#L556) 里有 `subsys_mask`@560 字段——v1 root 上记录"这棵树挂了哪些 controller",v2 root(dfl)上 `subsys_mask` 一直是全 1(因为所有 controller 都"潜在可用",具体哪些启用由 `subtree_control` 控制)。

---

## 18.4 `no internal process`:单一树带来的新约束

单一树解决了"分裂归属",但也带来了一个 v1 没有的新问题。这节是本章最关键、也是最容易让初学者困惑的地方——**v2 为什么禁止"一个 cgroup 既有进程在里面跑,又用 `cgroup.subtree_control` 启用了 controller"?**

### 新问题:进程和子 cgroup 在同一层抢资源

考虑这个场景:你想这么建 cgroup 树

```
/sys/fs/cgroup/
├── containerA/                # 想让进程 PID 1000 住这里
│   ├── cgroup.procs           # ← 写 1000
│   └── cgroup.subtree_control # ← 想写 "+memory" 给子树启用 memcg
└── containerA/worker/         # 子 cgroup,也住进程
```

如果允许这么做,会发生什么?

**进程 1000 直接住 `containerA`,而 `containerA` 又启用了 memory controller 给子树**。这意味着 `containerA` 本身既是一个"**进程组**"(直接住着进程 1000),又是一个"**资源域**"(它的 `subtree_control` 启用了 memory,`containerA/worker` 是它的资源子域)。**这两种身份在资源记账上是矛盾的**:

- 进程 1000 每分配一个 page,memcg 把它记到 `containerA` 的 memory 账上(因为进程住 `containerA`)。
- `containerA/worker` 里的进程分配的 page,memcg 也记到 `containerA` 的账上(因为它是 worker 的父域,层级累加)。
- 现在 `containerA` 的 `memory.current` 是"**进程 1000 + worker 子树所有进程**"的混合量。

这带来三个无解的问题:

| 问题 | 为什么讲不清 |
|------|-------------|
| **限额该卡谁?** | 如果设 `containerA/memory.max=1G`,超了是杀进程 1000 还是杀 worker 里的进程?两者都"贡献"了用量,但 OOM 选择逻辑无法区分。 |
| **资源争抢语义模糊** | 进程 1000 和 worker 子树"在同一个资源域里竞争",但没有任何机制保证公平——进程 1000 可能瞬间吃满,worker 子树被饿死,或反之。 |
| **迁移和记账错位** | 你想把 worker 子树迁到别处,但 worker 的 memory 用量有一部分"记在 `containerA` 身上"(层级累加的语义),迁走后这部分账怎么清? |

> **不这样会怎样**:如果你**不**加这个约束,允许"启用 controller 的 cgroup 也住进程",那么 v2 单一树的清晰性会被彻底摧毁——一个 cgroup 既是"我自己的限额域"又是"我子树的累加域",双重身份互相干扰,记账永远是混合量,限额策略无法讲清"卡谁、杀谁、迁谁"。这正是 v1 hybrid"一个目录含义不清"的另一种复活方式。v2 设计者(Tejun Heo,2014~2016 主导)下定决心:**这个口子不能开**。

### 解决方案:`no internal process` 约束

v2 用一条看似古怪、实则精准的规则堵住它:

> **一个 cgroup 要么是"域 cgroup"(domain,它的 `subtree_control` 启用了 controller,**只能住子 cgroup,不许直接住进程**),要么是"叶子 cgroup"(没启用任何 controller,**可以住进程**)。中间状态不允许。**

这条规则叫 **`no internal process` 约束**(no internal process constraint),内核源码里多处出现这个词组(见 `cgroup_is_mixable` 的注释 [cgroup.c:367-373](../linux/kernel/cgroup/cgroup.c#L367-L373)、`cgroup_migrate_vet_dst` 的注释 [cgroup.c:2641-2645](../linux/kernel/cgroup/cgroup.c#L2641-L2645))。

它有两处 enforcement 点,分别在**启用 controller** 和**迁移进程**两个方向:

**点一:启用 controller 时拒绝**——往 `cgroup.subtree_control` 写 `+memory` 时,如果这个 cgroup 自己有进程住,直接拒绝。源码在 [`cgroup_vet_subtree_control_enable`](../linux/kernel/cgroup/cgroup.c#L3328)([cgroup.c:3328](../linux/kernel/cgroup/cgroup.c#L3328)):

```c
/* kernel/cgroup/cgroup.c:3328-3366(简化,去掉 threaded 分支后聚焦核心) */
static int cgroup_vet_subtree_control_enable(struct cgroup *cgrp, u16 enable)
{
    u16 domain_enable = enable & ~cgrp_dfl_threaded_ss_mask;

    if (!enable)
        return 0;

    /* can @cgrp host any resources? */
    if (!cgroup_is_valid_domain(cgrp->dom_cgrp))
        return -EOPNOTSUPP;

    /* mixables(root) don't care */
    if (cgroup_is_mixable(cgrp))
        return 0;

    if (domain_enable) {
        /* can't enable domain controllers inside a thread subtree */
        if (cgroup_is_thread_root(cgrp) || cgroup_is_threaded(cgrp))
            return -EOPNOTSUPP;
    }
    ...
    /*
     * Controllers can't be enabled for a cgroup with tasks to avoid
     * child cgroups competing against tasks.
     */
    if (cgroup_has_tasks(cgrp))          /* L3362 */
        return -EBUSY;                   /* ← 这就是 no internal process 的拦截 */

    return 0;
}
```

([cgroup.c:3328-3366](../linux/kernel/cgroup/cgroup.c#L3328-L3366))

注意 L3358-3363 那段注释 "Controllers can't be enabled for a cgroup with tasks to avoid child cgroups competing against tasks" —— 这正是"进程和子 cgroup 在同一层抢资源"的源码级表达。 [`cgroup_has_tasks`](../linux/kernel/cgroup/cgroup.c#L355)([cgroup.c:355](../linux/kernel/cgroup/cgroup.c#L355))的实现极简,一行:`return cgrp->nr_populated_csets;`(查 [`cgroup->nr_populated_csets`](../linux/include/linux/cgroup-defs.h#L440),[cgroup-defs.h:440](../linux/include/linux/cgroup-defs.h#L440))——只要这个 cgroup 关联的 css_set 里有任意进程,这个计数器就 >0。

**点二:迁移进程时拒绝**——往一个 cgroup 的 `cgroup.procs` 写 PID 时,如果这个目标 cgroup 的 `subtree_control` 启用了任何 controller,拒绝。源码在 [`cgroup_migrate_vet_dst`](../linux/kernel/cgroup/cgroup.c#L2646)([cgroup.c:2646](../linux/kernel/cgroup/cgroup.c#L2646)):

```c
/* kernel/cgroup/cgroup.c:2646-2668(简化,去掉 threaded 分支后聚焦核心) */
int cgroup_migrate_vet_dst(struct cgroup *dst_cgrp)
{
    /* v1 doesn't have any restriction */
    if (!cgroup_on_dfl(dst_cgrp))
        return 0;

    /* verify @dst_cgrp can host resources */
    if (!cgroup_is_valid_domain(dst_cgrp->dom_cgrp))
        return -EOPNOTSUPP;

    /*
     * If @dst_cgrp is already or can become a thread root or is
     * threaded, it doesn't matter.
     */
    if (cgroup_can_be_thread_root(dst_cgrp) || cgroup_is_threaded(dst_cgrp))
        return 0;

    /* apply no-internal-process constraint */
    if (dst_cgrp->subtree_control)       /* L2664 */
        return -EBUSY;                   /* ← 这就是迁移方向的拦截 */

    return 0;
}
```

([cgroup.c:2646-2668](../linux/kernel/cgroup/cgroup.c#L2646-L2668))

L2663 那行注释 "apply no-internal-process constraint" 是内核源码里**直接出现这个术语**的地方(罕见,内核源码很少直接写设计术语名)。L2664 的检查 `if (dst_cgrp->subtree_control)` 一目了然:目标 cgroup 启用了任何 controller,迁移就被拒。

### 双向拦截:为什么必须两边都查

你可能会问:既然启用 controller 时已经拒绝了"有进程的 cgroup",为什么还要在迁移进程时再查一次"目标 cgroup 启用了 controller"?——**因为这两件事是独立的写操作**,只查一边会留 race:

- 如果只在"启用 controller"时查:运维先 `echo PID > cgroup.procs`(此时 cgroup 没启用 controller,允许),再 `echo +memory > cgroup.subtree_control`(此时 cgroup 有进程,应该拒)——这一步**会被 L3362 拦住**,没问题。
- 如果只在"迁移进程"时查:运维先 `echo +memory > cgroup.subtree_control`(此时没进程,允许),再 `echo PID > cgroup.procs`(此时 cgroup 启用了 controller,应该拒)——这一步**会被 L2664 拦住**,没问题。
- 两边都查:无论运维按什么顺序操作,都能堵住。**双向拦截,是防止"先 A 后 B"和"先 B 后 A"两种顺序都留下漏洞的标准工程做法**。

> **钉死这件事**:`no internal process` 约束不是"建议"或"最佳实践",它是 v2 的硬性约束,内核在 `cgroup_vet_subtree_control_enable`(启用 controller 方向,L3362)和 `cgroup_migrate_vet_dst`(迁移进程方向,L2664)两个 enforcement 点**强制拒绝**(`-EBUSY`)。这两处双向拦截,确保无论运维操作顺序如何,都不会出现"一个 cgroup 既住进程又启用 controller"的非法状态。代价是:在 v2 下,你**不能**像 v1 那样随便"建个目录、往里塞进程、再启用个 controller"——你必须**先规划好层级**(哪些层是资源域、哪些层是叶子进程组),再按这个规划建树。

### Root cgroup 豁免:`cgroup_is_mixable`

有一个例外:**根 cgroup**(`/sys/fs/cgroup/` 自己)不受 `no internal process` 约束。源码在 [`cgroup_is_mixable`](../linux/kernel/cgroup/cgroup.c#L366)([cgroup.c:366](../linux/kernel/cgroup/cgroup.c#L366)):

```c
/* kernel/cgroup/cgroup.c:366-374 */
static bool cgroup_is_mixable(struct cgroup *cgrp)
{
    /*
     * Root isn't under domain level resource control exempting it from
     * the no-internal-process constraint, so it can serve as a thread
     * root and a parent of resource domains at the same time.
     */
    return !cgroup_parent(cgrp);
}
```

([cgroup.c:366-374](../linux/kernel/cgroup/cgroup.c#L366-L374))

L370-372 的注释直接讲清原因:"Root isn't under domain level resource control exempting it from the no-internal-process constraint" —— 根 cgroup **不在"域级资源控制"下**(它没有"父域"来和它的子域竞争),所以它既是"宿主系统所有进程的默认家"(boot 后所有进程都住根 cgroup),又是"启用 controller 给整个子树"的根。这个豁免是必须的——否则系统启动时所有进程都在根 cgroup,而根 cgroup 又必须启用 controller 给子树用,矛盾无解。

所以 `cgroup_vet_subtree_control_enable` L3341 那行 `if (cgroup_is_mixable(cgrp)) return 0;` 就是给根开的后门。普通 cgroup(有 parent)走后面的检查,根直接放行。

---

## 18.5 `cgroup_apply_control`:启用/禁用 controller 的引擎

讲清了"为什么要约束",我们再看 v2 的"启用 controller"这条路径完整怎么走——这是 v2 独有、v1 没有的机制(v1 是 mount 时一次定死),也是 v2 动态配置的核心。

### 四步式:save → modify → apply → finalize

`cgroup_subtree_control_write`(L3369)拿到用户态的 `+cpu -memory` 字符串后,走一个经典的"**保存旧状态 → 修改 → 应用 → 失败则回滚**"四步式:

```c
/* kernel/cgroup/cgroup.c:3443-3461(简化) */
ret = cgroup_vet_subtree_control_enable(cgrp, enable);   /* ① 先查 no internal process */
if (ret)
    goto out_unlock;

/* save and update control masks and prepare csses */
cgroup_save_control(cgrp);                                /* ② 保存旧 subtree_control 到 old_subtree_control */

cgrp->subtree_control |= enable;                          /* ③ 修改 */
cgrp->subtree_control &= ~disable;

ret = cgroup_apply_control(cgrp);                         /* ④ 应用 */
cgroup_finalize_control(cgrp, ret);                       /* ⑤ finalize:成功则 disable 多余 css,失败则回滚 */
if (ret)
    goto out_unlock;

kernfs_activate(cgrp->kn);
```

([cgroup.c:3443-3461](../linux/kernel/cgroup/cgroup.c#L3443-L3461))

这五步的精妙在于 [`cgroup_finalize_control`](../linux/kernel/cgroup/cgroup.c#L3318)([cgroup.c:3318](../linux/kernel/cgroup/cgroup.c#L3318))的"**双向收尾**":

- **ret == 0(成功)**:调 `cgroup_apply_control_disable`(L3325)清掉那些应该消失但还没消失的 css(如 `-memory` 后,memory 的 css 要 kill 掉)。
- **ret != 0(失败)**:先 `cgroup_restore_control`(L3321)把 `subtree_control` 恢复到 `old_subtree_control`,再 `cgroup_propagate_control`(L3322)重新传播,最后 `cgroup_apply_control_disable`(L3325)清掉刚才误创建的 css。

**全成或全回滚**,这和我们第 1 章(P0-01)讲 `create_new_namespaces` 的 `goto out_xxx` 回滚链、第 10 章(P2-10)讲 `cgroup_attach_task` 四步迁移是同一套内核工程哲学——**对涉及多对象、有副作用的操作,先把旧状态存起来,改一半,全成就提交、失败就回滚**。

### `cgroup_apply_control_enable`:遍历子树为每个节点创建 css

真正干活的是 [`cgroup_apply_control_enable`](../linux/kernel/cgroup/cgroup.c#L3202)([cgroup.c:3202](../linux/kernel/cgroup/cgroup.c#L3202))。启用一个 controller 不是只改根 cgroup 的位图——它要**遍历整棵子树**,为每个后代 cgroup 创建/激活对应 controller 的 css:

```c
/* kernel/cgroup/cgroup.c:3202-3233(简化) */
static int cgroup_apply_control_enable(struct cgroup *cgrp)
{
    struct cgroup *dsct;
    struct cgroup_subsys_state *d_css;
    struct cgroup_subsys *ss;
    int ssid, ret;

    cgroup_for_each_live_descendant_pre(dsct, d_css, cgrp) {   /* 前序遍历子树 */
        for_each_subsys(ss, ssid) {
            struct cgroup_subsys_state *css = cgroup_css(dsct, ss);

            if (!(cgroup_ss_mask(dsct) & (1 << ss->id)))       /* 这个 cgroup 该不该启用这个 ss? */
                continue;

            if (!css) {                                         /* 还没有 css,创建一个 */
                css = css_create(dsct, ss);                     /* 调 ss->css_alloc() 多态 */
                if (IS_ERR(css))
                    return PTR_ERR(css);
            }
            ...
            if (css_visible(css)) {                             /* 让对应接口文件出现 */
                ret = css_populate_dir(css);
                if (ret)
                    return ret;
            }
        }
    }
    return 0;
}
```

([cgroup.c:3202-3233](../linux/kernel/cgroup/cgroup.c#L3202-L3233))

注意 `css_create` 会调 `ss->css_alloc()` —— 这就是第 9 章(P2-09)讲过的**函数指针多态**:`struct cgroup_subsys` 是一张函数指针表,每个 controller(memcg 的、cpu 的、io 的)各填一份自己的 `css_alloc` 实现。`css_create` 不写死"创建一个 memcg",而是 `ss->css_alloc(ss, cgrp)` 让 controller 自己决定创建什么。这是 cgroup v2 架构可插拔的核心——新增一个 controller,**不用改 `cgroup_apply_control_enable` 这个核心循环一行**,只要新 controller 实现自己的 `cgroup_subsys` 表项。

`css_populate_dir`(L3225)会让对应 controller 的接口文件(如 `memory.max`、`cpu.weight`)在这个 cgroup 目录里出现/消失——这就是为什么你 `echo +memory > subtree_control` 后,子目录里突然多出 `memory.max` 这些文件。

### 禁用方向:`cgroup_apply_control_disable`

反向操作在 [`cgroup_apply_control_disable`](../linux/kernel/cgroup/cgroup.c#L3248)([cgroup.c:3248](../linux/kernel/cgroup/cgroup.c#L3248))。注意它有两种处理:

```c
/* kernel/cgroup/cgroup.c:3255-3273(简化) */
cgroup_for_each_live_descendant_post(dsct, d_css, cgrp) {   /* 后序遍历 */
    for_each_subsys(ss, ssid) {
        struct cgroup_subsys_state *css = cgroup_css(dsct, ss);
        if (!css)
            continue;
        if (css->parent &&
            !(cgroup_ss_mask(dsct) & (1 << ss->id))) {
            kill_css(css);              /* 情况 A:彻底不需要,杀掉 */
        } else if (!css_visible(css)) {
            css_clear_dir(css);         /* 情况 B:有依赖,隐藏接口但保留 css */
            if (ss->css_reset)
                ss->css_reset(css);     /* 调 controller 的 reset 多态回调 */
        }
    }
}
```

([cgroup.c:3255-3273](../linux/kernel/cgroup/cgroup.c#L3255-L3273))

**情况 A(kill_css)**:这个 cgroup 不再需要这个 controller 的 css,彻底 kill(引用计数降到 0 后释放)。
**情况 B(css_clear_dir + css_reset)**:这个 controller 被别的 controller 依赖(比如某些 controller 隐式依赖另一个,通过 `cgrp_dfl_inhibit_ss_mask`@177 这种机制管理),不能彻底杀,只能**隐藏**它的接口文件(`css_clear_dir`)并把它**重置到初始状态**(`ss->css_reset()` 多态回调,让 controller 自己决定怎么 reset)。

注意**前序 vs 后序**:enable 是前序(先父后子,父先有 css 才能让子有),disable 是后序(先子后父,先杀子的再杀父的)。这是树形数据结构操作的常识——构造自顶向下,析构自底向上。

> **钉死这件事**:`cgroup_apply_control` 的"四步式 + 双向 finalize"是 cgroup v2 动态配置的核心机制。它和第 10 章 `cgroup_attach_task` 的四步迁移、第 1 章 `create_new_namespaces` 的 `goto out_xxx` 回滚链是同一套工程模式:**保存旧状态 → 修改 → 应用 → 失败则回滚**,确保 controller 启用/禁用这个有副作用的操作**全成或全回滚**。配合函数指针多态(`ss->css_alloc`/`ss->css_reset`),让 controller 可插拔,新增 controller 不动核心循环——这是 cgroup v2 比 v1 在架构上更干净的关键之一。

---

## 18.6 `cgroup.type`:threaded 逃生口

`no internal process` 约束解决了"分裂归属"病根,但有时它**挡了正经路**。

### 真实痛点:多线程进程要按线程分限额

考虑这种场景:你有一个**多线程进程**(比如一个 JVM,里面几百个线程),你想:

- 这个进程的**资源域**是一个 cgroup(限额 CPU、内存给整个 JVM)。
- 但这个 JVM 内部的某些线程(比如 GC 线程、业务线程)想**分别限 CPU**——GC 线程最多吃 10% CPU,业务线程可以吃到 90%。

在 v2 `no internal process` 下,这做不到——因为这个 JVM 的进程住进 cgroup A 后,A 就不能启用 controller 给子树,意味着 A 下面没法再开 `A/gc/`、`A/business/` 子 cgroup 来分线程。

v1 hybrid 下能做(每个线程在不同 controller 树上分开),但代价是回到 v1 的分裂归属病根。

### v2 的解药:threaded 模式

v2 给这条约束开了**一个**逃生口:**threaded cgroup**(线程化 cgroup)。它允许一个 cgroup 的**子树**专门用于"按线程分",而**资源域**由这个子树的**根**(thread root)统一承担。

具体地:你建 `/sys/fs/cgroup/jvm/`(进程住这里),它启用 `+cpu`;再建 `/sys/fs/cgroup/jvm/gc/`、`/sys/fs/cgroup/jvm/business/`,把这两个子 cgroup **标记为 threaded**(`echo threaded > cgroup.type`)。此后:

- `jvm/gc/` 和 `jvm/business/` 是 threaded cgroup,它们的**进程**(JVM 的 GC 线程、业务线程)按线程级别归属,可以分别设 `cpu.max`。
- 但它们的**资源域**统一指向 `jvm/`(thread root)——所有 cpu 用量累加到 `jvm`,由 `jvm` 的 `cpu.max` 统一限额。
- `jvm/` 自己**不**受 `no internal process` 约束(它是 thread root,可以同时住进程和给子树启用 controller)。

源码上,把一个 cgroup 切到 threaded 模式走 [`cgroup_type_write`](../linux/kernel/cgroup/cgroup.c#L3536)([cgroup.c:3536](../linux/kernel/cgroup/cgroup.c#L3536)),它只能 `echo threaded`(单向切换,不能从 threaded 切回 domain):

```c
/* kernel/cgroup/cgroup.c:3536-3556(简化) */
static ssize_t cgroup_type_write(struct kernfs_open_file *of, char *buf,
                                 size_t nbytes, loff_t off)
{
    struct cgroup *cgrp;
    int ret;

    /* only switching to threaded mode is supported */
    if (strcmp(strstrip(buf), "threaded"))           /* L3543 */
        return -EINVAL;

    cgrp = cgroup_kn_lock_live(of->kn, true);
    if (!cgrp)
        return -ENOENT;

    /* threaded can only be enabled */
    ret = cgroup_enable_threaded(cgrp);              /* L3552 */

    cgroup_kn_unlock(of->kn);
    return ret ?: nbytes;
}
```

([cgroup.c:3536-3556](../linux/kernel/cgroup/cgroup.c#L3536-L3556))

真正干活的是 [`cgroup_enable_threaded`](../linux/kernel/cgroup/cgroup.c#L3473)([cgroup.c:3473](../linux/kernel/cgroup/cgroup.c#L3473)),核心是把当前 cgroup 和它所有 threaded 后代的 `dom_cgrp` 指针指向父 cgroup 的 `dom_cgrp`:

```c
/* kernel/cgroup/cgroup.c:3473-3518(简化) */
static int cgroup_enable_threaded(struct cgroup *cgrp)
{
    struct cgroup *parent = cgroup_parent(cgrp);
    struct cgroup *dom_cgrp = parent->dom_cgrp;      /* 父的 dom_cgrp 就是我们的资源域 */
    ...
    /* noop if already threaded */
    if (cgroup_is_threaded(cgrp))
        return 0;

    /* 不能在已 populated 或已启用 domain controller 的 cgroup 上切 */
    if (cgroup_is_populated(cgrp) ||
        cgrp->subtree_control & ~cgrp_dfl_threaded_ss_mask)
        return -EOPNOTSUPP;

    /* 父必须是 valid domain 且能当 thread root */
    if (!cgroup_is_valid_domain(dom_cgrp) ||
        !cgroup_can_be_thread_root(dom_cgrp))
        return -EOPNOTSUPP;
    ...
    cgroup_save_control(cgrp);

    /* 把自己和所有 threaded 后代的 dom_cgrp 都改到父的 dom_cgrp */
    cgroup_for_each_live_descendant_pre(dsct, d_css, cgrp)
        if (dsct == cgrp || cgroup_is_threaded(dsct))
            dsct->dom_cgrp = dom_cgrp;               /* ← 关键:dom_cgrp 指向 thread root */

    ret = cgroup_apply_control(cgrp);
    if (!ret)
        parent->nr_threaded_children++;              /* 父记录"我有 threaded 子树了" */
    ...
}
```

([cgroup.c:3473-3518](../linux/kernel/cgroup/cgroup.c#L3473-L3518))

注意 [`cgroup->dom_cgrp`](../linux/include/linux/cgroup-defs.h#L492)([cgroup-defs.h:492](../linux/include/linux/cgroup-defs.h#L492))这个字段——它的注释(L485-491)讲透了:"If !threaded, self. If threaded, it points to the nearest domain ancestor"——非 threaded 的 cgroup,`dom_cgrp` 指向自己;threaded 的,指向最近的 domain 祖先。这是 threaded 模式的核心数据结构:**threaded cgroup 的资源域不在于自己,而在于它的 thread root**。

### `cgroup.type` 的四种取值

[`cgroup_type_show`](../linux/kernel/cgroup/cgroup.c#L3520)([cgroup.c:3520](../linux/kernel/cgroup/cgroup.c#L3520))展示 `cgroup.type` 文件的四种取值:

```c
/* kernel/cgroup/cgroup.c:3520-3534 */
static int cgroup_type_show(struct seq_file *seq, void *v)
{
    struct cgroup *cgrp = seq_css(seq)->cgroup;

    if (cgroup_is_threaded(cgrp))
        seq_puts(seq, "threaded\n");                 /* ① 自己是 threaded cgroup */
    else if (!cgroup_is_valid_domain(cgrp))
        seq_puts(seq, "domain invalid\n");            /* ② 不合法的 domain(祖先链断了) */
    else if (cgroup_is_thread_root(cgrp))
        seq_puts(seq, "domain threaded\n");           /* ③ 是 thread root(住进程 + threaded 子树) */
    else
        seq_puts(seq, "domain\n");                    /* ④ 普通 domain cgroup */
    return 0;
}
```

([cgroup.c:3520-3534](../linux/kernel/cgroup/cgroup.c#L3520-L3534))

四种取值的关系:

| `cgroup.type` 取值 | 含义 | `no internal process` 约束 |
|---|---|---|
| `domain` | 普通 domain cgroup,可启用 controller 给子树,但**不许**住进程 | 受约束 |
| `domain threaded` | thread root,可启用 threaded controller,也可住进程 | **豁免**(它是 threaded 子树的资源域) |
| `domain invalid` | 不合法的 domain(祖先链上有 threaded,自己却被当 domain 用) | 受约束(且不能用) |
| `threaded` | threaded cgroup,资源域指向 thread root,可住进程 | **豁免**(进程的资源记账走到 dom_cgrp) |

判断这四种状态的辅助函数,前面 18.4 节已经列过:`cgroup_is_threaded`(L360,查 `dom_cgrp != self`)、`cgroup_is_thread_root`(L399,查有 threaded 子树或有进程且启用了 threaded controller)、`cgroup_is_valid_domain`(L421,查祖先链是否断)、`cgroup_can_be_thread_root`(L377,查能不能当 thread root)。

### css_set 的对应物:`dom_cset`

threaded 模式下,任务归属的账本也要适配——`css_set` 有个对应字段 [`dom_cset`](../linux/include/linux/cgroup-defs.h#L234)([cgroup-defs.h:234](../linux/include/linux/cgroup-defs.h#L234)),它的注释(L228-233)讲透:"For a domain cgroup, the following points to self. If threaded, to the matching cset of the nearest domain ancestor. The dom_cset provides access to the domain cgroup and its csses to which domain level resource consumptions should be charged."

意思是:一个 threaded cgroup 里的任务的 `css_set`,它的 `dom_cset` 指向 thread root 的 `css_set`。**所有"域级资源"(不归属某个具体任务、而是整个域共享的资源)都记到 `dom_cset` 上**——这就是 threaded 模式下"资源域统一由 thread root 承担"的账本机制。

> **钉死这件事**:`threaded` 模式是 `no internal process` 约束的**唯一**逃生口,专为"多线程进程要按线程分限额"这种场景设计。它通过引入"thread root + threaded 子树"两级结构,让任务的归属可以按线程分裂(在 threaded cgroup 间分布),而资源域统一(都指向 thread root 的 `dom_cgrp`/`dom_cset`)。代价是:threaded 子树**只能用 threaded 标记的 controller**(`cgrp_dfl_threaded_ss_mask`,L183,只有 cpu、pids 等少数几个,memcg 不在其中——因为内存是进程级而非线程级的),且**单向**(一旦切到 threaded 不能切回 domain)。这种"开一个受控的逃生口,而不是放开整条约束"的设计,是 cgroup v2 在保持单一树清晰性的同时,给真实多线程场景留出空间的精妙之处。

---

## 18.7 技巧精解:单一层级 + `no internal process` —— v2 的两道闸

本章最硬核的两个设计技巧,我们已经零散讲过,这里单独拆透。

### 技巧一:单一层级(unified)—— 用结构消灭"分裂归属"

**问题**:v1 hybrid 让一个进程在 N 棵树上各有归属,导致记账/迁移/策略全部撕裂(见 18.2)。

**朴素做法**:加一堆"协调机制"——每次迁移时同步多棵树、记账时跨树聚合、限额策略跨树规划。这条路在工程上是地狱,因为你要在 N 个独立的 superblock 之间维护一致性,任何 race 都会让分裂状态再次出现。

**Linux 的做法**:**用结构消灭问题**。不搞协调,直接规定**所有 controller 共用同一棵树**,一个进程在树上有唯一归属点。源码上:

- 全局变量 [`cgrp_dfl_root`](../linux/kernel/cgroup/cgroup.c#L167)([cgroup.c:167](../linux/kernel/cgroup/cgroup.c#L167))是这棵树的根。
- 一个 cgroup 要么在 dfl(`cgroup_on_dfl` 为真),要么在某个 v1 root 上,不会跨界([cgroup.c:321](../linux/kernel/cgroup/cgroup.c#L321))。
- 进程的 `css_set->subsys[]` 数组虽然仍是 15 槽,但 v2 下大部分槽指向**同一个 cgroup**(或其祖先)的 css——归属天然一致,不需要协调。

**为什么 sound**(为什么这么设计不会丢东西):
- 单一树并不意味着"每个 cgroup 都启用所有 controller"——`subtree_control` 按需启用,让运维可以分层配置不同子树关心不同 controller。这是 v2 兼顾"归属一致"和"按需启用"的精妙。
- 一个进程在某个 cgroup,而那个 cgroup 没启用 memcg——那进程的内存记账走哪?走**最近的启用了 memcg 的祖先** cgroup 的 css。这就是 [`css_set->subsys[]`](../linux/include/linux/cgroup-defs.h#L223) 的注释(L257-263)讲的:"On the default hierarchy, ->subsys[ssid] may point to a css attached to an ancestor instead of the cgroup this css_set is associated with"——`subsys[]` 的指针可以指向祖先的 css,这是 v2 单一树但 controller 按需启用的底层机制。

> **反面对比**:如果不用单一树,继续用 v1 hybrid,那么"一个进程在 cpu 树属 A、memory 树属 B"的分裂状态永远存在,任何协调机制都只是补丁——race 期间进程仍可能半迁、记账仍可能错位。v2 的设计者(Tejun Heo)在 2014 年 LCA 演讲里明确说:"v1 hybrid 是个错误,我们要从架构上纠正它"——**纠正的方式不是给错误打补丁,而是消灭产生错误的土壤**(N 棵树 → 一棵树)。这是"用结构设计消灭问题"的典范,和我们第 9 章讲的 `css_set` 去重表(用一层间接消灭内存爆炸)、第 10 章讲的 `cgroup_threadgroup_rwsem`(用一把读写锁消灭 fork/迁移 race)是同一种工程美学。

### 技巧二:`no internal process` —— 用单向约束堵住单一树的新漏洞

**问题**:单一树解决了分裂归属,但带来新问题——"一个 cgroup 既住进程又启用 controller"导致进程和子 cgroup 在同一层抢资源、记账混合、限额策略讲不清(见 18.4)。

**朴素做法**:在 controller 的 charge 路径里加"区分这个 page 是 cgroup 自己的进程产生的还是子 cgroup 的进程产生的"的逻辑。这是地狱——每次 memcg charge 都要做这种区分,性能开销巨大;而且仍然讲不清"限额该卡谁"。

**Linux 的做法**:**用单向约束堵在源头**。不让"既住进程又启用 controller"这种状态出现——`cgroup_vet_subtree_control_enable`(L3328)和 `cgroup_migrate_vet_dst`(L2646)两个 enforcement 点,任何方向尝试制造这种状态都返回 `-EBUSY`。

**为什么 sound**(为什么这条约束是必要的、不冗余):
- 它把"一个 cgroup 的身份"二值化:**要么是资源域(住子 cgroup,启用 controller),要么是进程组(住进程,不启用 controller)**。两者互斥,记账不再混合。
- 它是**约束(不允许某种状态)**而非**协调(允许但事后处理)**,所以没有运行时开销——只在用户态写接口文件时检查一次,正常路径零成本。
- 它配合 `subsys[]` 可指向祖先 css 的机制,让"进程住叶子 cgroup,而它的资源域在祖先"自然成立——叶子 cgroup 的进程的 memcg charge,自动走到祖先那个启用了 memcg 的 cgroup 的 css 上(通过 `css_set->subsys[memory]` 指向祖先的 memcg css)。

> **反面对比**:如果不用这条约束,继续允许"既住进程又启用 controller",那么 v1 hybrid 的"一个目录含义不清"问题会在 v2 里复活——一个 cgroup 既是"我自己的限额域"又是"我子树的累加域",双重身份互相干扰,记账永远是混合量。**单一树解决了"进程在 N 棵树上分裂",`no internal process` 解决了"controller 在一棵树上重叠"——前者消灭"横向分裂",后者消灭"纵向重叠",两道闸一起,让 v2 的归属语义彻底干净**。这是"用约束换清晰"的典范,和操作系统里其他"用约束换 sound"的设计(如 `immutable inode`、`O_APPEND` 原子写)是同一种哲学:与其允许复杂状态再费力处理,不如一开始就不让复杂状态出现。

### 这两个技巧的合谋

把这两个技巧合起来看,v2 的设计哲学浮出水面:**用结构(单一树)+ 约束(no internal process)的合谋,把"进程↔资源"的归属关系变成一个清晰的二维表**——每个进程在唯一一棵树上有唯一归属点,每个 cgroup 要么是资源域要么是进程组。这张表上不存在"分裂归属"或"重叠归属"的状态,记账、迁移、限额、OOM 全部可以在这张表上讲清。

代价是:**运维必须按"资源域/进程组"的二分法预先规划 cgroup 树**(v1 那种随意建目录、随便塞进程、随时启用 controller的自由没了)。但这个代价是值得的——它换来的是 K8s 等编排系统能稳定运行的清晰语义。systemd、Docker、K8s 都已默认 v2,正是因为 v2 的这种"用约束换清晰"的设计,让上层编排不再需要和"分裂归属"这种内核层的混乱搏斗。

---

## 章末小结

这一章我们站在 cgroup v2 的架构转向点,看清楚了 v1 → v2 这次痛苦转向的本质。我们没有钻进任何具体 controller,但立起了 v2 区别于 v1 的两条核心规则:

1. **单一层级(unified)**:所有 controller 共用同一棵树,一个进程在树上有唯一归属点——消灭 v1 hybrid 的"分裂归属"病根。
2. **`no internal process` 约束**:启用 controller 的 cgroup 不许住进程(进程住叶子 cgroup,资源域在祖先)——堵住单一树带来的"进程和子 cgroup 同层抢资源"新漏洞。
3. **`cgroup.type`(domain/threaded)**:threaded 模式是 `no internal process` 的唯一逃生口,让多线程进程能按线程分限额,代价是只能用 threaded controller 且单向切换。
4. **`cgroup_apply_control` 四步式**:启用/禁用 controller 是 v2 独有的动态配置机制,走"save → modify → apply → finalize"全成或全回滚。

回到二分法:本章服务**资源(演进)**面。前 6 章(第 2 篇 P2-09~14)讲的是 cgroup **怎么记账**(`css_set`/`cgroup_attach_task`/各 controller 的 charge 路径),本章讲的是这些记账机制**挂在什么结构上**——从 v1 的多树 hybrid 演进到 v2 的单一树 + `no internal process`。前者是"资源怎么算",后者是"资源算到谁的头上"——后者是更深层的架构问题。

### 五个"为什么"清单

1. **为什么 v1 hybrid 有"分裂归属"病根?** v1 每棵树挂一组 controller,同一个进程在 cpu 树属 A、memory 树属 B,A 和 B 是两个独立 superblock 上的不同 cgroup,内核没有任何机制强制它们一致。导致记账归谁、迁移怎么迁、限额策略谁优先全是悬案。
2. **为什么 v2 用单一树能解决?** 所有 controller 共用同一棵树,一个进程在树上有唯一归属点,它的 `css_set->subsys[]` 各槽都指向同一个 cgroup(或其祖先)的 css,归属天然一致,不需要跨树协调。
3. **为什么单一树需要 `no internal process` 约束?** 单一树带来新问题:一个 cgroup 既有进程又启用 controller 时,进程和子 cgroup 在同一层抢资源、记账混合、限额策略讲不清。约束把 cgroup 身份二值化(要么资源域要么进程组),堵住这种非法状态。
4. **为什么 `no internal process` 要双向 enforcement?** 启用 controller(L3362)和迁移进程(L2664)两个方向都查,防止运维操作顺序(先 A 后 B vs 先 B 后 A)留下 race 漏洞。这是"双向拦截"的标准工程做法。
5. **为什么需要 threaded 模式?** 多线程进程要按线程分限额(JVM 的 GC 线程 vs 业务线程),但 `no internal process` 禁止"进程住的 cgroup 启用 controller"。threaded 模式开一个受控逃生口:threaded 子树按线程归属,资源域统一指向 thread root。代价是只能用 threaded controller(cpu/pids 等,不含 memcg),且单向切换。

### 想继续深入往哪钻

- 本章点到的 `cgrp_dfl_root` 详见 [`kernel/cgroup/cgroup.c:167`](../linux/kernel/cgroup/cgroup.c#L167);`cgroup_apply_control` 四步式见 [L3293](../linux/kernel/cgroup/cgroup.c#L3293)、`cgroup_vet_subtree_control_enable` 见 [L3328](../linux/kernel/cgroup/cgroup.c#L3328)、`cgroup_migrate_vet_dst` 见 [L2646](../linux/kernel/cgroup/cgroup.c#L2646)、`cgroup_enable_threaded` 见 [L3473](../linux/kernel/cgroup/cgroup.c#L3473)。
- `struct cgroup` 的 `subtree_control`@460、`dom_cgrp`@492、`nr_populated_csets`@440 见 [`include/linux/cgroup-defs.h:397`](../linux/include/linux/cgroup-defs.h#L397);`struct cgroup_root` 见 [L556](../linux/include/linux/cgroup-defs.h#L556)。
- v1 mount 路径 `cgroup1_get_tree` 见 [`kernel/cgroup/cgroup-v1.c:1233`](../linux/kernel/cgroup/cgroup-v1.c#L1233);v1 hybrid 的多 superblock 模型从这里追起。
- 想观测,自己造一个最小例子:`mount | grep cgroup` 看是 v1 还是 v2(v2 是 `/sys/fs/cgroup/`,v1 是 `/sys/fs/cgroup/<controller>`);`cat /sys/fs/cgroup/cgroup.controllers` 看可用 controller;`echo +cpu > /sys/fs/cgroup/cgroup.subtree_control` 试启用,然后试着在有进程的 cgroup 上启用 controller,看 `-EBUSY`;`echo threaded > /sys/fs/cgroup/x/cgroup.type` 试切 threaded 模式。

### 引出下一章

我们讲完了 v2 的统一树模型和 `no internal process` 约束,但还有最后一个 namespace 没讲——**cgroup namespace**(cgroup ns)。它和本章关系密切:cgroup ns 让一个进程**只看见**自己 cgroup 路径以下的子树(视图裁剪),配合 v2 的统一树,让容器里 `/proc/self/cgroup` 显示的是容器内的相对路径而非宿主的绝对路径。下一章(P4-19),我们讲 cgroup namespace——视图隔离的最后一环,也是 7 种 namespace 里出现得最晚的(2016,4.6),它为什么这么晚才出现,以及它如何让容器的 cgroup 视图干净起来。
