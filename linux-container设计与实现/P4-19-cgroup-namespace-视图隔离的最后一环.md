# 第十九 章 · cgroup namespace:视图隔离的最后一环

> 篇:P4 cgroup 进阶
> 主线呼应:前 18 章我们拆完了 namespace 的 7 种视图(mnt/pid/net/uts/ipc/user/cgroup 中的前 6 种)和 cgroup 的资源控制(cpu/memory/io/pids/freezer/cpuset),也讲完了 cgroup v2 的统一层级。但你可能一直没注意一个怪现象:一个容器里 `cat /proc/self/cgroup` 看到的路径,有时是 `/`,有时是 `/kubepods/pod123/container456`——同一个进程,为什么在容器里看到的 cgroup 路径和宿主机上看到的不一样?更怪的是:容器里的进程 `ls /sys/fs/cgroup/`,看到的子目录结构也"刚好"只到它自己那一级,宿主机顶层的 `kubepods`、`system.slice` 这些目录,容器里根本看不见。这不是 cgroup v2 的挂载隔离(mnt ns 早做了一半),也不是资源限额(cgroup 该限的还在限),而是第 7 种、也是最晚被合入(2016,4.6)的一种 namespace 做的事:**cgroup namespace**,它专门裁剪"进程看到的 cgroup 路径"这层视图。这一章把这条"视图隔离的最后一环"彻底拆透。

## 核心问题

**容器里 `cat /proc/self/cgroup` 为什么看到的是 `/` 而不是宿主机上的绝对路径?cgroup namespace 凭什么能让一个进程"看不见"自己 cgroup 路径以上的祖先目录,但 cgroup 资源限额照常生效?它为什么是 7 种 namespace 里最晚被合入(4.6,2016)的?这个晚到的 namespace,代码量小到几乎只有一个文件,却解决了什么样的信息泄漏问题?**

读完本章你会明白:

1. **cgroup ns 不改资源、只改路径展示**:同一个进程的同一个 `css_set` 完全不动,资源限额(cpu.max/memory.max)原封不动地生效,变的只是 `cat /proc/self/cgroup`、`/proc/<pid>/cgroup`、`cpuset.cpus` 等接口里路径字符串的展示。
2. **cgroup ns 的全部秘密是 `root_cset` 一个字段**:每个 cgroup ns 钉住创建它那一刻、调用者所处的一个 `css_set`,把这个 cset 当成"ns 眼里的根 cset";路径生成时,以这个 cset 对应的 cgroup 为"新根",算相对路径。
3. **视图裁剪靠 `cgroup_path_ns_locked` → `cset_cgroup_from_root` → `kernfs_path_from_node` 三步走**:先从 ns 的 `root_cset` 反查"这个 ns 眼里 cgroup 层级的根节点",再让 kernfs 算"目标 cgroup 相对这个根的相对路径"。
4. **它为何最晚**:cgroup ns 依赖 cgroup v2 的"统一层级"先稳定下来(v2 在 4.x 仍在演进),且只为解决"容器里 `/proc/self/cgroup` 路径泄漏宿主顶层结构"这一个具体问题——前面 6 种 ns 都是基础视图(mnt/pid/net/uts/ipc/user 早早就有),cgroup ns 是"补刀"。
5. **它的安全语义**:`CGRP_ROOT_NS_DELEGATE` 标志位下,cgroup ns 是资源委派边界——容器内进程不能把别人迁出这个 ns 的视野之外,cgroup ns 把"视图边界"和"权限边界"对齐。

> **逃生阀**:如果本章代码读得绕,记住三件事就够了:① **cgroup ns 不动 `css_set`,只改路径字符串**;② **一个 cgroup ns = 一个 `root_cset` 指针**,创建 ns 时把"当前调用者的 cset"钉住当根;③ **所有路径展示都走 `cgroup_path_ns(... , current->nsproxy->cgroup_ns)`**,这个函数算"相对 ns 根的相对路径"。后面任何一段代码看不懂,回到这三句问"它在算根、还是在算相对路径、还是在用 ns 做权限边界"。

---

## 19.1 一句话点破

> **cgroup namespace 是 namespace 家族里最小的一个——它不改任何资源、不复制任何 cgroup 子树、只做一件事:把"创建 ns 那一刻调用者所处的那个 cset"记下来当"ns 眼里的根",之后这个 ns 内的进程看到的所有 cgroup 路径,都相对这个根来算。容器里 `/proc/self/cgroup` 显示 `/` 而不是宿主的绝对路径,根因就是这一行字段 `root_cset`。**

这是结论,不是理由。本章倒过来拆:先看为什么"不裁剪 cgroup 路径"会泄漏信息、为什么朴素地"挂载隔离"做不到这件事;再看 `struct cgroup_namespace` 长什么样(整个结构体只有 4 个字段)、`copy_cgroup_ns` 创建时怎么钉住 `root_cset`;然后钻进路径生成的三步走(`cgroup_path_ns` → `cset_cgroup_from_root` → `kernfs_path_from_node`),看"相对根算路径"这件事怎么做到又快又 sound;最后讲它为什么 2016 年才进内核,以及它和 `CGRP_ROOT_NS_DELEGATE` 配合时,如何把"视图边界"和"权限边界"对齐。

---

## 19.2 不裁剪会怎样:容器里 `/proc/self/cgroup` 暴露宿主结构

先把这个 namespace 要解决的问题钉死。一个 K8s 节点上,容器进程真实的 cgroup 路径可能是这样的(在宿主机上 `cat /proc/<容器 PID>/cgroup`):

```
0::/kubepods/pod-7c8f/burstable/pod-7c8f-..../container-456
```

这一行暴露了什么?① 顶层 `kubepods`(说明这台机器在跑 K8s);② `burstable`(这个 pod 的 QoS 等级——给运维看的,租户不该知道);③ pod 的完整 UUID;④ 容器在宿主 cgroup 层级里的精确位置。这些信息对租户来说**完全不该可见**——一个隔离得干净的容器,进程应该觉得"我自己就是根",`cat /proc/self/cgroup` 应该显示 `0::/`,而不是把宿主的 cgroup 顶层组织结构(kubepods/burstable/...)暴露给容器内的人。

> **不这样会怎样**:如果不在路径展示层裁剪,① 容器里 `cat /proc/self/cgroup` 会把宿主 cgroup 顶层组织结构泄漏出去,攻击者据此推测宿主编排系统(K8s/Docker)、QoS 分级、租户分布;② 容器里读 `cpuset.cpus`、`memory.current` 这些文件时,某些工具(如 `systemd-cgls`、`cadvisor` 内嵌的路径解析)会因为路径前缀不是 `/` 而误判;③ 容器内进程被迁到宿主上别的 cgroup 时,会观察到自己的 cgroup 路径"超出"了自己 ns 视野——这种"视图越界"破坏了容器"独占整机"的幻觉。

那为什么朴素地用 mnt ns 隔离 `/sys/fs/cgroup/` 不够?因为 `/proc/<pid>/cgroup` 不是个普通文件——它由内核的 `proc_cgroup_show` 动态生成,直接打印 cgroup 在层级里的绝对路径,和你挂载了什么文件系统无关。即使你 mnt ns 里只挂了 `/sys/fs/cgroup` 的一个子目录,`/proc/self/cgroup` 照样打印 `/kubepods/...`——这是"内核直接吐出来的字符串",不是文件系统里的路径名。所以要解决这层泄漏,必须有一个**专门裁剪这个字符串生成过程**的 namespace,这就是 cgroup ns 存在的根本理由。

> **钉死这件事**:cgroup ns 解决的是"`/proc/<pid>/cgroup` 和 `/sys/fs/cgroup/` 里**路径字符串**的展示"这一个问题——不是隔离资源(资源照常被限额),不是挂载隔离(mnt ns 管文件系统,cgroup ns 管字符串生成),只是让"内核吐路径"这个动作,以"ns 内的根 cset"为起点算相对路径。

---

## 19.3 一个 cgroup ns 长什么样:`root_cset` 是全部秘密

cgroup namespace 的数据结构小到惊人,整个 [`struct cgroup_namespace`](../linux/include/linux/cgroup.h#L769-L774) 只有 4 个字段:

```c
/* include/linux/cgroup.h:769-774 */
struct cgroup_namespace {
    struct ns_common      ns;         /* 通用 ns 头(inum/ops/count) */
    struct user_namespace *user_ns;   /* 这个 cgroup ns 的 owner user ns */
    struct ucounts        *ucounts;   /* 每 user ns 的 ns 计数(限制嵌套) */
    struct css_set        *root_cset; /* ★ 整个 ns 的全部秘密 */
};
```

([cgroup.h:769](../linux/include/linux/cgroup.h#L769))

前三个字段(`ns`/`user_ns`/`ucounts`)是所有 namespace 都有的通用件:`ns` 是 [`struct ns_common`](../linux/include/linux/ns_common.h)(inum 唯一编号、ops 指向 `cgroupns_operations`、count 引用计数);`user_ns` 记录"这个 cgroup ns 属于哪个 user ns";`ucounts` 是限制(每个 user ns 里 cgroup ns 数量有上限,防止 fork 炸弹)。这三件是"namespace 通用基础设施",和 uts ns、ipc ns、pid ns 共用同一套机制(回扣 P1-02 讲过的 `proc_ns_operations` 多态)。

**真正让 cgroup ns 成立的,是第 4 个字段 `root_cset`。**

```
 宿主上 cgroup 层级(简化,只画 v2 unified):

    /                                ← cgrp_dfl_root.cgrp(根 cgroup)
    ├── system.slice
    │   ├── sshd.service
    │   └── kubelet.service
    └── kubepods
        └── burstable
            └── pod-7c8f
                └── container-456    ← 容器进程的真实 cgroup

  容器内进程(假设它的 cgroup_ns 是在被放进 container-456 之后创建的):

    cgroup_ns->root_cset  ─────────►  css_set {
                                         subsys[cpu]     = container-456 的 cpu css
                                         subsys[memory]  = container-456 的 memcg css
                                         subsys[pids]    = container-456 的 pids css
                                         subsys[io]      = container-456 的 io css
                                         ...
                                       }

  ns 内进程 cat /proc/self/cgroup:
    0::/          ← 路径以 root_cset 对应的 cgroup(container-456)为根算,
                     container-456 在 ns 眼里就是 "/",自己就是根
```

这个图是理解整个 cgroup ns 的钥匙。`root_cset` 不是什么"虚拟的根 css_set",它就是**创建 cgroup ns 那一刻,调用者进程所处的那个真实的 `css_set`**——把那一刻调用者的 cset 引用一个,记下来,作为这个 ns 的"根 cset"。从此 ns 内所有路径展示,都以"这个 cset 所对应的那个 cgroup"为根算相对路径。

> **所以这样设计**:为什么是钉一个 `css_set *` 而不是钉一个 `cgroup *`?因为一个进程同时归属"一组" cgroup(每个 controller 一个,见 P2-09 的 `subsys[]` 数组)。钉一个 cset 等于一次钉住"所有 controller 上这个 ns 的根",路径生成时再用 `cset_cgroup_from_root(root_cset, 目标 root)` 反查"在指定 cgroup_root(某棵 cgroup 树)上,这个 cset 对应哪个 cgroup"。这是用 cset(已有的去重表单元)做根,不引入新的"虚拟 cgroup"概念——简洁。

宿主的 init cgroup ns 是 [`init_cgroup_ns`](../linux/kernel/cgroup/cgroup.c#L213-L219),它的 `root_cset` 就是 [`init_css_set`](../linux/kernel/cgroup/cgroup.c#L218)(cgroup.h 里的静态全局):

```c
/* kernel/cgroup/cgroup.c:213-219 */
/* cgroup namespace for init task */
struct cgroup_namespace init_cgroup_ns = {
    .ns.count   = REFCOUNT_INIT(2),
    .user_ns    = &init_user_ns,
    .ns.ops     = &cgroupns_operations,
    .ns.inum    = PROC_CGROUP_INIT_INO,
    .root_cset  = &init_css_set,     /* init 进程的 cset,在 cgrp_dfl_root 根上 */
};
```

([cgroup.c:213](../linux/kernel/cgroup/cgroup.c#L213))

`init_css_set` 关联到 `cgrp_dfl_root.cgrp`(unified 树的根),所以宿主上 init cgroup ns 的"根"就是 `/`——这和"宿主上 `cat /proc/self/cgroup` 显示绝对路径"完全一致,因为绝对路径的根就是它。**宿主的 init cgroup ns 就是"没裁剪"的那一种**——它的 `root_cset` 指向根 cgroup,所以"相对根算"的相对路径就等于绝对路径。这给了我们一个统一的视角:**所有 cgroup ns,宿主的、容器的,都用同一套"相对 ns 根算"的路径生成逻辑;宿主 ns 的根恰好在 `/`,所以看起来是绝对路径**。

---

## 19.4 怎么创建一个 cgroup ns:钉住当前 cset

创建一个 cgroup ns 的入口是 [`copy_cgroup_ns`](../linux/kernel/cgroup/namespace.c#L50-L91),它在 `copy_namespaces`→`create_new_namespaces` 链里被调用(回扣 P1-02 的总入口),也会被 `unshare(CLONE_NEWCGROUP)` 触发。整个函数只有 40 行,核心逻辑只有两句:

```c
/* kernel/cgroup/namespace.c:50-91(简化,完整见源文件) */
struct cgroup_namespace *copy_cgroup_ns(unsigned long flags,
                    struct user_namespace *user_ns,
                    struct cgroup_namespace *old_ns)
{
    struct cgroup_namespace *new_ns;
    struct ucounts *ucounts;
    struct css_set *cset;

    BUG_ON(!old_ns);

    /* ① 不要求新 cgroup ns,共享老的:引用计数 +1 即可 */
    if (!(flags & CLONE_NEWCGROUP)) {
        get_cgroup_ns(old_ns);
        return old_ns;
    }

    /* ② 权限检查:必须有 CAP_SYS_ADMIN */
    if (!ns_capable(user_ns, CAP_SYS_ADMIN))
        return ERR_PTR(-EPERM);

    ucounts = inc_cgroup_namespaces(user_ns);
    if (!ucounts)
        return ERR_PTR(-ENOSPC);   /* 每个 user ns 里 cgroup ns 数有上限 */

    /* ★③ 钉住当前进程的 css_set,这是整个 ns 的核心动作 */
    spin_lock_irq(&css_set_lock);
    cset = task_css_set(current);
    get_css_set(cset);              /* 引用计数 +1,防止这个 cset 被回收 */
    spin_unlock_irq(&css_set_lock);

    new_ns = alloc_cgroup_ns();
    if (IS_ERR(new_ns)) {
        put_css_set(cset);
        dec_cgroup_namespaces(ucounts);
        return new_ns;
    }

    new_ns->user_ns  = get_user_ns(user_ns);
    new_ns->ucounts  = ucounts;
    new_ns->root_cset = cset;       /* ★ 记下来:这个 ns 的根就是当前 cset */

    return new_ns;
}
```

([namespace.c:50](../linux/kernel/cgroup/namespace.c#L50))

这段代码里有三个细节决定它为什么 sound。

**第一个细节:注释里那行 `/* It is not safe to take cgroup_mutex here */`(namespace.c L73)。**为什么 `copy_cgroup_ns` 拿 `css_set_lock`(spinlock,namespace.c L74)而不是 `cgroup_mutex`?因为 `copy_cgroup_ns` 在 `create_new_namespaces` 链里被调用,而 `create_new_namespaces` 是在 fork/unshare 路径上,这时持有 `task_lock`、`cgroup_threadgroup_rwsem` 等锁;`cgroup_mutex` 是 cgroup 子系统的"大锁",持有它再调 fork 路径会撞上 lockdep 的反转规则(在 cgroup_attach_task 路径里,cgroup_mutex 在 threadgroup_rwsem 之内获取)。所以这里只用细粒度的 `css_set_lock` 保护"读 cset 指针 + 引用计数 +1"这一小段,不拿 `cgroup_mutex`。这是一个典型的"锁粒度选择"技巧——拿够用的最小锁,不拿会被反转的粗锁。

> **不这样会怎样**:如果这里拿 `cgroup_mutex`,fork/unshare 创建 cgroup ns 这条路径会和 `cgroup_attach_task`(也持 cgroup_mutex)、`cgroup_init`(早期持 cgroup_mutex)、`cgroup_destroy_root`(销毁根时持 cgroup_mutex)抢同一把大锁,fork 频繁时锁竞争严重;更糟的是,某些路径下会形成 `cgroup_mutex → task_lock → cgroup_mutex` 的环,触发 lockdep 警告。用细粒度 `css_set_lock` 只保护"读 cset + inc refcount"这两步,既 sound 又不撞反转。

**第二个细节:`get_css_set(cset)` 把当前 cset 的引用计数 +1。** 这是为什么?因为新建的 cgroup ns 把这个 cset 当根,要保证"只要这个 ns 还活着,这个 cset 就不能被释放"。cset 的生命周期靠引用计数管理(P2-09 讲过),进程 fork/exit 时 inc/dec,如果某个 cset 上所有进程都退出了,它就会被 `put_css_set` 回收。如果 cgroup ns 不持引用,这个 ns 还活着,但 root_cset 指向的 cset 已经被释放——悬垂指针。`get_css_set` 把引用钉住,`free_cgroup_ns` 里 [`put_css_set(ns->root_cset)`](../linux/kernel/cgroup/namespace.c#L42)(namespace.c L42)对称释放。

**第三个细节:`BUG_ON(!old_ns)` —— 创建 cgroup ns 时,"老的 ns"必须存在。** 这看起来是废话,但它钉死了一件事:任何进程,即使是 init 的子进程,也一定有一个 cgroup ns(init cgroup ns);你 `clone(CLONE_NEWCGROUP)` 时,父进程一定已经挂在某个 cgroup ns 里,所以"共享老的 ns"(`get_cgroup_ns(old_ns)`)这条路径永远有意义。这是 namespace 子系统"init ns 永远存在"的不变量。

> **钉死这件事**:创建一个 cgroup ns 的核心动作就两句——**读 `current->cgroups`(当前进程的 cset),`get_css_set` 引用 +1,塞进 `new_ns->root_cset`**。整个 ns 的全部秘密就是这个被钉住的 cset。后面所有路径展示,都以它对应的 cgroup 为根算相对路径。

---

## 19.5 视图裁剪三步走:`cgroup_path_ns` 怎么算相对路径

现在看 cgroup ns 最核心的技巧——**给定一个 cgroup,在某个 ns 眼里它该显示成什么路径**。这个动作在内核里走三步:

```
用户态 cat /proc/self/cgroup
        │
        ▼
proc_cgroup_show()  @cgroup.c:6246
  │  遍历每个 cgroup_root,对每个 root 拿到 task 在该 root 上的 cgroup
  └─► cgroup_path_ns_locked(cgrp, buf, PATH_MAX, current->nsproxy->cgroup_ns)
                │  @cgroup.c:2363
                ▼
          ① cset_cgroup_from_root(ns->root_cset, cgrp->root)
                │  算出"在这个 cgroup_root 上,ns 的根 cset 对应哪个 cgroup"
                │  这个 cgroup 就是"ns 眼里的根"
                ▼
          ② kernfs_path_from_node(cgrp->kn, root->kn, buf, buflen)
                │  让 kernfs 算"目标 cgroup 的 kernfs node,
                │   相对 ns 根 cgroup 的 kernfs node 的相对路径"
                ▼
          返回 "/相对/路径"(目标在 ns 根之外)或 "/"(目标就是 ns 根)
```

**第一步:`proc_cgroup_show` 的入口。** 这是 `/proc/<pid>/cgroup` 文件的实现,核心循环对每个 cgroup_root(每棵 cgroup 树)算一行,关键两行是:

```c
/* kernel/cgroup/cgroup.c:6294-6295 */
retval = cgroup_path_ns_locked(cgrp, buf, PATH_MAX,
                current->nsproxy->cgroup_ns);
```

注意它传的 ns 参数是 **`current->nsproxy->cgroup_ns`**——也就是"读这个文件的人(当前进程)的 cgroup ns",而不是"被读的那个 task 的 cgroup ns"。这是一个微妙的设计选择:你在容器 A 里 `cat /proc/<容器 B 的 PID>/cgroup`,看到的是"从容器 A 的 cgroup ns 视角看过去"的路径,不是从容器 B 的视角。这保证了"读者只看得见自己 ns 视野里的路径结构",不会因为读了别人进程的 `/proc` 文件就越过自己的 ns 边界。

> **钉死这件事**:路径展示用的是**读者的 cgroup ns**(`current->nsproxy->cgroup_ns`),不是被读进程的。这意味着:你在宿主上 `cat /proc/<容器PID>/cgroup`,看到的是绝对路径(因为你的 cgroup ns 是 init,根是 `/`);你在容器内 `cat /proc/self/cgroup`,看到的是裁剪后的相对路径(因为你的 cgroup ns 的根是 container-456)。同一个进程,不同读者看到的路径不同——这是 cgroup ns 的"读者视角"语义。

**第二步:`cgroup_path_ns_locked` → `cset_cgroup_from_root`。** 这是真正的"算 ns 根"动作:

```c
/* kernel/cgroup/cgroup.c:2363-2369 */
int cgroup_path_ns_locked(struct cgroup *cgrp, char *buf, size_t buflen,
              struct cgroup_namespace *ns)
{
    struct cgroup *root = cset_cgroup_from_root(ns->root_cset, cgrp->root);

    return kernfs_path_from_node(cgrp->kn, root->kn, buf, buflen);
}
```

([cgroup.c:2363](../linux/kernel/cgroup/cgroup.c#L2363))

第一行 `cset_cgroup_from_root(ns->root_cset, cgrp->root)`——给定"ns 钉住的那个 cset"和"目标 cgroup 所在的 cgroup_root(那棵树)",算出"在这个 root 上,这个 cset 对应哪个 cgroup"。这个 cgroup 就是"ns 眼里的根"。注意 `cgrp->root` 是"目标 cgroup 所属的 cgroup_root"——v2 上几乎全是 `cgrp_dfl_root`,但 v1 上可能有多个 root(每挂载一棵 v1 树一个 root),这个函数为每棵树算各自的 ns 根。

`cset_cgroup_from_root` 内部走 [`__cset_cgroup_from_root`](../linux/kernel/cgroup/cgroup.c#L1369-L1402)(cgroup.c L1369):

```c
/* kernel/cgroup/cgroup.c:1369-1402(简化) */
static inline struct cgroup *__cset_cgroup_from_root(struct css_set *cset,
                              struct cgroup_root *root)
{
    struct cgroup *res_cgroup = NULL;

    if (cset == &init_css_set) {
        res_cgroup = &root->cgrp;             /* init cset 在每棵树上都是根 */
    } else if (root == &cgrp_dfl_root) {
        res_cgroup = cset->dfl_cgrp;          /* v2 树直接走 dfl_cgrp 字段 */
    } else {
        struct cgrp_cset_link *link;
        /* v1 多树:遍历 cset 上挂的 cgrp_links,找属于该 root 的那个 */
        list_for_each_entry(link, &cset->cgrp_links, cgrp_link) {
            struct cgroup *c = link->cgrp;
            if (c->root == root) {
                res_cgroup = c;
                break;
            }
        }
    }
    return res_cgroup;
}
```

([cgroup.c:1369](../linux/kernel/cgroup/cgroup.c#L1369))

三个分支,每条都通向同一个目标:"给定 cset + root,返回这个 cset 在该 root 上对应的那个 cgroup":

- **init cset**:它是初始进程的 cset,在每棵树上都对应那棵树的根 cgroup(`root->cgrp`),所以直接返回 `&root->cgrp`——这就是为什么 init cgroup ns 看到的路径是绝对路径(它的根是真正的树根)。
- **dfl_root(v2)**:`css_set` 上专门有个字段 [`dfl_cgrp`](../linux/include/linux/cgroup-defs.h#L217)(cgroup-defs.h L226 附近,见 P2-09),记录"这个 cset 在 v2 默认层级上对应的 cgroup",直接读字段,不走遍历——这是热路径优化,v2 几乎所有路径都走这条。
- **v1 多树**:遍历 `cset->cgrp_links`(一个链表,记录"这个 cset 在哪些 cgroup 上有 css"),找到属于目标 root 的那个 cgroup。这是 v1 兼容路径,慢一点但只在 v1 时走。

**第三步:`kernfs_path_from_node` 算相对路径。** 这一步不归 cgroup 子系统管,而是 kernfs(虚拟文件系统底层)提供的通用能力——"给定两个 kernfs_node,算从一个到另一个的相对路径"。cgroup 的每个节点(`struct cgroup`)里挂着一个 kernfs_node(`cgrp->kn`),cgroup 层级在 kernfs 里是一棵树;`kernfs_path_from_node(cgrp->kn, root->kn)` 就是"从 root 的 kn 到 cgrp 的 kn,路径是什么"。如果 `cgrp == root`,返回 `/`;如果 `cgrp` 是 root 的孩子,返回 `/child`;以此类推。

> **所以这样设计**:为什么 cgroup 子系统不自己写"算相对路径",而是借用 kernfs 的能力?因为 kernfs 本来就维护着 cgroup 层级的树结构——每个 cgroup 的 `kn` 就是这棵树上的一个节点,kernfs 已经实现了"树上两节点间算路径"的高效算法。cgroup 只需要告诉 kernfs "起点是 ns 根的 kn,终点是目标 cgrp 的 kn",剩下的交给 kernfs。这是"职责分离":cgroup 子系统负责"哪个 cset 对应哪个 cgroup"(根选择),kernfs 负责"树上两节点间的字符串路径"(路径生成)。**不重复造轮子**。

一个完整例子:宿主上容器进程在 `/kubepods/burstable/pod-7c8f/container-456`,这个进程被 `unshare(CLONE_NEWCGROUP)` 后,新建的 cgroup ns 的 `root_cset` 钉的就是"container-456 这个 cgroup 对应的 cset"。之后这个 ns 内进程 `cat /proc/self/cgroup`:

1. `proc_cgroup_show` 拿到 task 在 dfl_root 上的 cgroup:container-456。
2. `cgroup_path_ns_locked(cgrp=container-456, ns=这个 ns)`。
3. `cset_cgroup_from_root(ns->root_cset, dfl_root)` 返回 container-456(因为 root_cset 钉的就是它)。
4. `kernfs_path_from_node(container-456->kn, container-456->kn)` 返回 `/`。
5. `/proc/self/cgroup` 显示 `0::/`。

容器内进程看到自己"在根上",完全符合"我独占整机"的幻觉。但宿主机上 `cat /proc/<这个 PID>/cgroup`,因为读者的 cgroup ns 是 init(根在 `/`),`cset_cgroup_from_root` 返回 `cgrp_dfl_root.cgrp`,算出来的是绝对路径 `/kubepods/.../container-456`——视角不同,路径不同。

---

## 19.6 它和 mnt ns 的关系:两件事,各管一摊

讲到这里你可能会问:**既然 mnt namespace 已经能让容器里 `/sys/fs/cgroup/` 只显示一个子树(把整个 cgroupfs 重新挂载到一个裁剪过的根),那 cgroup ns 是不是重复造轮子?**

不是。这两件事管的是**不同的展示路径**:

| 展示场景 | 谁管 | 机制 |
|---------|------|------|
| `ls /sys/fs/cgroup/` 看到的目录结构 | mnt ns | bind mount / pivot_root,只挂裁剪过的子树 |
| `cat /proc/self/cgroup` 的路径字符串 | **cgroup ns** | `proc_cgroup_show` 走 `cgroup_path_ns_locked` |
| `cat /sys/fs/cgroup/cpu.max`(读文件)的路径 | mnt ns + 文件系统 | kernfs 文件操作 |
| `cat /proc/<pid>/cgroup` 路径字符串 | **cgroup ns** | 同 `/proc/self/cgroup`,走读者 ns |
| 内核日志 `pr_cont_cgroup_path` 打印的路径 | 无 ns(都是绝对) | [`pr_cont_cgroup_path`](../linux/include/linux/cgroup.h#L607)(cgroup.h L607)走 init ns |
| `docker logs` / `kubectl describe` 看到的路径 | 宿主视角 | init ns,绝对路径 |

这两层配合的典型用法:runc 在容器里先 `mount -t cgroup2 none /sys/fs/cgroup`(在容器的 mnt ns 里,挂 cgroupfs),再 `unshare(CLONE_NEWCGROUP)` 创建 cgroup ns——前者让 `/sys/fs/cgroup/` 这个目录在容器里可见且可写(不然容器连读自己 cgroup 配额的接口都没有),后者让 `/proc/self/cgroup` 显示 `/` 而不是绝对路径。**两层一起,才让容器"从文件系统到路径字符串"都看起来独占**。

> **钉死这件事**:cgroup ns 不取代 mnt ns,它补的是 mnt ns 管不到的那一层——**内核动态生成的路径字符串**(`/proc/<pid>/cgroup`、`cpuset.cpus` 等文件里出现的路径)。mnt ns 管文件系统挂载,cgroup ns 管"内核吐路径时以什么为根算"。两者职责正交,容器要"看起来独占"必须两层都做。

一个边角:有些读者会注意到 `pr_cont_cgroup_path`(内核日志里打印 cgroup 路径,如 memcg OOM 时)走的是 init cgroup ns 的视角(绝对路径),为什么?因为内核日志是给宿主运维看的,要能定位到真实 cgroup;容器内进程看不到这些内核日志(除非宿主特意转发),所以这里用绝对路径才合理。这是"视角选择"的另一种情况——**不是所有路径都要按 ns 裁剪,只有给容器进程自己看的接口才裁剪**。

---

## 19.7 进入别人的 cgroup ns:`cgroupns_install` 与 `setns`

和所有 namespace 一样,cgroup ns 也支持 `setns(2)` 进入别人的 ns。实现是 [`cgroupns_install`](../linux/kernel/cgroup/namespace.c#L98-L116):

```c
/* kernel/cgroup/namespace.c:98-116 */
static int cgroupns_install(struct nsset *nsset, struct ns_common *ns)
{
    struct nsproxy *nsproxy = nsset->nsproxy;
    struct cgroup_namespace *cgroup_ns = to_cg_ns(ns);

    if (!ns_capable(nsset->cred->user_ns, CAP_SYS_ADMIN) ||
        !ns_capable(cgroup_ns->user_ns, CAP_SYS_ADMIN))
        return -EPERM;

    /* Don't need to do anything if we are attaching to our own cgroupns. */
    if (cgroup_ns == nsproxy->cgroup_ns)
        return 0;

    get_cgroup_ns(cgroup_ns);
    put_cgroup_ns(nsproxy->cgroup_ns);
    nsproxy->cgroup_ns = cgroup_ns;

    return 0;
}
```

([namespace.c:98](../linux/kernel/cgroup/namespace.c#L98))

这个函数展示了 namespace 切换的"两阶段 + 引用计数"范式(回扣 P3-16 的 setns 总框架):① 权限检查(必须对源 user ns 和目标 cgroup ns 的 owner user ns 都有 `CAP_SYS_ADMIN`);② 如果目标就是当前 ns,直接返回(幂等);③ `get` 新 ns、`put` 旧 ns、换指针——这是 atomic 的指针交换,被 `commit_nsset` 在持锁状态下统一调用,所以不会"切一半"。

> **注意**:这里只换了 `nsproxy->cgroup_ns` 这一个指针,**没有动 `task_struct->cgroups`**——也就是说,进程的 `css_set` 没变,它的 cpu.max/memory.max/pids.max 这些限额**完全照旧**。`setns` 进一个新 cgroup ns 之后,进程的资源归属没变,变的只是它之后 `cat /proc/self/cgroup` 看到的路径字符串。**这是 cgroup ns 和 cgroup 资源控制完全解耦的铁证**——同一件事的"视图面"和"资源面"由两套独立机制管。

还要注意 `cgroupns_install` 的权限检查是**双向的**:`CAP_SYS_ADMIN` 既要对源 user ns 有,也要对目标 cgroup ns 的 owner user ns 有。这是为了防止"从高权限 user ns 溜进低权限 user ns 的 cgroup ns 后反向逃逸"——只有"对两边都持 CAP_SYS_ADMIN"的进程才能 setns,典型场景是 runc 在容器启动时帮容器进程装 ns(此时 runc 在宿主 user ns,有充分权限)。

---

## 19.8 一个关键边界:`CGRP_ROOT_NS_DELEGATE` 把视图边界对齐权限边界

cgroup ns 不止是个"展示裁剪"工具,在某个标志位下它还扮演"权限边界"。这就是 [`CGRP_ROOT_NS_DELEGATE`](../linux/include/linux/cgroup-defs.h#L79)(cgroup-defs.h L79 附近的 `cgroup_root->flags` 位)——当 v2 root 挂载时带 `nsdelegate` 选项(`/sys/fs/cgroup` 挂载时 `mount -t cgroup2 none /sys/fs/cgroup -o nsdelegate`),这个 root 上创建的 cgroup ns,其 `root_cset` 对应的 cgroup 子树就成了"委派边界"。

具体看 [`cgroup_procs_write_permission`](../linux/kernel/cgroup/cgroup.c#L5086-L5115)(写 `cgroup.procs` 时的权限检查):

```c
/* kernel/cgroup/cgroup.c:5105-5112(简化) */
/*
 * If namespaces are delegation boundaries, %current must be able
 * to see both source and destination cgroups from its namespace.
 */
if ((cgrp_dfl_root.flags & CGRP_ROOT_NS_DELEGATE) &&
    (!cgroup_is_descendant(src_cgrp, ns->root_cset->dfl_cgrp) ||
     !cgroup_is_descendant(dst_cgrp, ns->root_cset->dfl_cgrp)))
    return -ENOENT;
```

([cgroup.c:5105](../linux/kernel/cgroup/cgroup.c#L5105))

这段是说:如果 `nsdelegate` 启用,那么一个进程尝试迁移别的进程(写 `cgroup.procs`)时,源 cgroup 和目标 cgroup **都必须是当前进程 cgroup ns 视野里的后代**——即都必须是 `ns->root_cset->dfl_cgrp`(ns 在 v2 树上的根)的子节点。否则返回 `-ENOENT`(连"不存在"都不告诉它,避免信息泄漏)。

> **不这样会怎样**:如果 cgroup ns 只裁剪视图、不对齐权限边界,会出现这样的攻击:容器内的进程 A,虽然自己只能看见 ns 视野里的 cgroup,但它只要知道宿主上某个 PID(比如通过侧信道),就能 `echo <宿主PID> > /sys/fs/cgroup/some/dest` 把宿主的别的进程迁到一个恶意 cgroup 里(比如一个 `cpu.max=100%` 的"抢占 cgroup"),拖垮别人。`nsdelegate` 把"视图边界"和"权限边界"对齐——你能操作的 cgroup 必须在你 ns 视野之内,跨边界操作直接被拒。

这是 cgroup ns 在"视图裁剪"之外的另一面:**它不仅让进程"看不见"ns 之外的 cgroup,还让进程"动不了"ns 之外的迁移**。在 systemd、K8s 这些以 cgroup ns 为委派边界的运行时里,这是容器隔离的关键一环——容器内的进程,只能在它 ns 视野里的那棵子树上操作 cgroup,宿主的 cgroup 层级组织结构(kubepods/burstable/...)对它不仅"不可见",还"不可达"。

---

## 19.9 技巧精解:`root_cset` —— 用已有数据结构做"ns 根"的极简设计

这一章我们挑一个技巧拆透——**cgroup ns 为什么用 `css_set *` 当根,而不是 `cgroup *`,更不是"新造一个虚拟 cgroup"**。这是整个 cgroup ns 设计中最精妙的一笔,它用最小的数据结构代价,撑起了"每棵 cgroup 树上算 ns 根"这件事。

### 朴素方案一:钉一个 `cgroup *`

```c
/* 朴素的、糟糕的写法(示意,非源码) */
struct cgroup_namespace {
    struct ns_common ns;
    struct cgroup *root_cgrp;   /* 钉一个 cgroup 当根 */
    ...
};
```

这看起来更直接——"ns 的根就是一个 cgroup 嘛"。但它撞墙了:Linux 上一个进程**同时归属多个 cgroup**(v1 时代每个 controller 一棵树,一个进程在 cpu树上一个 cgroup、memory 树上另一个、io 树上又一个,见 P4-18 的 v1 hybrid)。钉一个 `cgroup *` 只能表示一棵树上的根,要表示"每棵树上各自的 ns 根"就得改成数组:

```c
/* 钉一棵树还好,v1 多树就崩了(示意,非源码) */
struct cgroup_namespace {
    struct cgroup *root_cgrps[NR_CGROUP_ROOTS];  /* 每棵树一个 */
    ...
};
```

但这又撞墙:① cgroup_root 数量动态变化(用户可以挂/卸 v1 树),数组长度无法静态确定;② 维护"每棵树一个 cgroup 指针"的代码繁琐(挂新树时要给所有现存 ns 补一个根);③ 完全没用到 cgroup 子系统已有的数据结构。

### 朴素方案二:新造一个"虚拟根 css_set"

```c
/* 想得太多了的写法(示意,非源码) */
struct cgroup_namespace {
    struct css_set *virtual_root_cset;  /* 一个不挂任何真实进程的"虚拟 cset" */
    ...
};
```

这没必要——`css_set` 本来就是"一组 cgroup 指针"的去重表单元(P2-09),它的存在意义是"被一组任务共享"。造一个不被任何任务共享的"虚拟 cset",徒增一个数据对象,而且每次"算 ns 根"时还要特殊处理"这个 cset 不是真实任务的"。

### 实际方案:钉一个已有的、真实的 `css_set *`

Linux 实际做的就是 [`ns->root_cset = 当前进程的 cset`](../linux/kernel/cgroup/namespace.c#L88)(namespace.c L88)。这一个指针同时解决了三件事:

1. **多树根一次表达**:`css_set` 内部有 `subsys[CGROUP_SUBSYS_COUNT]` 数组,记录"在每个 controller 上归属哪个 css";同时通过 `cgrp_links` 链表记录"在每棵 cgroup_root 上对应哪个 cgroup"。钉一个 cset,等于一次钉住"这个进程在所有 cgroup 树上各自的根 cgroup",无论 v2 单一树还是 v1 多树,都自动覆盖。
2. **复用已有的反查函数**:[`cset_cgroup_from_root`](../linux/kernel/cgroup/cgroup.c#L1461)(cgroup.c L1461)早就为 cgroup 子系统内部使用而存在(`cgroup_attach_task` 等路径都要用),cgroup ns 直接借用,零新增逻辑。
3. **引用计数天然 sound**:`css_set` 有成熟的引用计数机制(P2-09 讲过 `get_css_set`/`put_css_set`),cgroup ns 只要在创建时 `get`,销毁时 `put`,生命周期就自动正确,不需要新设计。

```c
/* 实际方案(kernel/cgroup/namespace.c:79-88,简化) */
new_ns = alloc_cgroup_ns();
...
new_ns->root_cset = cset;   /* ★ 一个指针,涵盖所有树 */
```

> **反面对比**:如果钉 `cgroup *`,v1 多树场景下要维护"每棵树一个根"的数组,代码复杂、维护成本高、还容易漏掉新挂载的树;如果造"虚拟 cset",引入一个不被任何任务共享的 cset,打破了"cset 是任务去重表单元"的设计契约。实际方案用一个 `css_set *`,**借已有的、为别的目的存在的基础设施(cset + cset_cgroup_from_root + 引用计数)**,完成新功能。这是"用结构设计消灭问题"的典范——和第 1 章 P0-01 讲的 `css_set` 去重表、第 9 章 P2-09 讲的多对多账本,是同一套工程美学:**优先复用,不要新造**。

### 并发安全:为什么钉 cset 这件事不会 race

再深一层:钉 `root_cset` 这件事,创建时的并发安全怎么保证?

`copy_cgroup_ns` 在拿 `css_set_lock`(spinlock,namespace.c L74)的保护下读 `current->cgroups` 并 `get_css_set`。这两步必须原子:如果中间被别的 CPU 把当前进程迁到别的 cgroup(比如 `cgroup_attach_task` 并发执行),`current->cgroups` 指针可能正好被换成新 cset,我们读到的 cset 可能已经是"即将被释放"的老 cset,`get_css_set` 拿不到引用就悬垂。

`css_set_lock` 这把 spinlock 正是为保护"`current->cgroups` 读取 + 引用计数 inc"这一对操作而存在——cgroup 子系统里**所有读 cset 指针并 inc refcount 的路径**都持这把锁(`css_task_iter`、`task_cgroup_from_root`、`cgroup_attach_task` 等等)。`copy_cgroup_ns` 借同一把锁,和这些路径天然互斥,不会 race。

> **钉死这件事**:cgroup ns 的并发安全不是新设计的——它**复用 cgroup 子系统为 `css_set` 生命周期已经建立的并发机制**(`css_set_lock` + refcount)。这是"用已有锁保护新增字段"的典型,新增字段(`root_cset`)的生命周期管理完全 piggyback 在 cgroup 子系统已 sound 的基础设施上,不需要新锁、不需要新协议。

---

## 19.10 它为何最晚:依赖 v2 统一层级先稳定

最后一个问题:cgroup ns 为什么 2016 年(4.6)才进内核,是 7 种 namespace 里最晚的?

回顾一下 namespace 家族的时间线(简化):

| namespace | 引入版本 | 年份 | 解决什么 |
|-----------|---------|------|---------|
| mnt ns | 2.4.19 | 2002 | 挂载视图隔离 |
| uts ns | 2.6.19 | 2006 | hostname 隔离 |
| ipc ns | 2.6.19 | 2006 | SysV IPC 隔离 |
| pid ns | 2.6.24 | 2008 | 进程号隔离 |
| net ns | 2.6.29 | 2009 | 网络栈隔离 |
| user ns | 3.8 | 2013 | uid 映射(容器安全基石) |
| cgroup ns | **4.6** | **2016** | cgroup 路径视图 |

前 6 种 ns 都是基础视图——mount/uts/ipc/pid/net 这些"一个进程看到什么"的隔离,逻辑相对独立,早就有了。user ns 稍晚(3.8),因为它引入了 uid 映射这个全新的安全模型,需要先理清 capability 在 user ns 内的语义、kuid_t 的内核全局性、各种 syscall 的权限检查改造。这些都到位后,容器才有了"安全的非特权 root"。

cgroup ns 之所以最晚,有三个原因:

**① 依赖 cgroup v2 先稳定。** cgroup v2 在 4.x 经历了多次大改(单一层级约束、`no internal process`、`cgroup.subtree_control` 的语义、threaded cgroup、`CGRP_ROOT_NS_DELEGATE` 等)。cgroup ns 的"视图裁剪"语义只有在 v2 的"单一根、清晰层级"成立后才有意义——v1 多树时代,一个进程在多棵树上各有归属,"裁剪路径"这件事几乎无从谈起(裁哪棵树?按哪个 controller 裁?)。v2 统一层级稳定后,cgroup ns 才有了清晰的裁剪对象。

**② 需求出现得晚。** 前 6 种 ns 解决的是"容器能不能跑起来"的问题(没有 mnt ns 容器没有自己的根文件系统、没有 pid ns 容器没有 PID 1、没有 net ns 容器没有独立网络);cgroup ns 解决的是"容器跑起来后,`/proc/self/cgroup` 暴露宿主结构"这个更细的问题——这是个"可用性 + 安全加固"问题,不是"能不能跑"的问题。前 6 种到位后,Docker/K8s 大规模铺开,才有人注意到"容器里 `cat /proc/self/cgroup` 看到宿主路径结构"这个具体痛点。

**③ 设计权衡耗时。** "裁剪 cgroup 路径"这件事,有过几个候选方案:要不要在每个 cgroup 上加个"是否可见"标志位?要不要用 mnt ns 隔离 `/sys/fs/cgroup/`?最终选择"用一个独立的 cgroup ns + 一个 `root_cset` 字段",是因为这套方案最轻(新增字段少、复用已有基础设施)、最 sound(借用 css_set_lock + refcount)、和 namespace 家族最一致(走 `proc_ns_operations` 多态)。这个权衡过程也花时间。

> **钉死这件事**:cgroup ns 不是"被发明的",是"被需要的"——前 6 种 ns 让容器能跑,cgroup v2 让资源控制干净,这两件齐了之后,容器里路径泄漏这个细节问题才浮现出来,才催生了 cgroup ns。它是 namespace 家族里最"补刀"性质的一个,小而精,用一个 `root_cset` 字段就解决了问题,完全符合 Linux 内核"不为小问题引入大机制"的工程哲学。

---

## 章末小结

这一章是第 4 篇(P4 cgroup 进阶)的最后一章,也是 namespace 家族的最后一环。我们没讲什么大机制——cgroup ns 全部代码就一个 `namespace.c`(152 行),一个 4 字段的结构体——但它把"视图隔离"这件事收了个干净利落的尾。立清楚四件事:

1. **cgroup ns 只管路径展示,不管资源**:进程的 `css_set` 不动,cgroup 资源限额(cpu.max/memory.max/pids.max)原封不动地生效,变的只是 `/proc/<pid>/cgroup`、`cpuset.cpus` 等接口里路径字符串的展示——以"ns 的根 cset"为起点算相对路径。
2. **一个 `root_cset` 字段撑起整个 ns**:创建 cgroup ns 时,把当前进程的 `css_set` 钉一个引用当根,之后所有路径生成都以这个 cset 对应的 cgroup 为起点算。
3. **视图裁剪三步走**:`cgroup_path_ns` → `cset_cgroup_from_root`(算 ns 根)→ `kernfs_path_from_node`(算相对路径);cgroup 子系统负责"哪个 cset 对应哪个 cgroup",kernfs 负责"树上两节点间的路径",职责分离。
4. **它是 namespace 家族里最晚(4.6,2016)也是最小的**:依赖 cgroup v2 先稳定,解决"容器里 `/proc/self/cgroup` 暴露宿主结构"这个具体痛点;配合 `CGRP_ROOT_NS_DELEGATE`,还能把"视图边界"对齐"权限边界",让 cgroup ns 成为资源委派边界。

### 五个"为什么"清单

1. **为什么需要 cgroup ns?不能只用 mnt ns 隔离 `/sys/fs/cgroup/` 吗?**
   mnt ns 管文件系统挂载,但 `/proc/<pid>/cgroup` 是内核动态生成的字符串,不归文件系统管——mnt ns 隔离不到这层。cgroup ns 专门裁剪这个字符串的生成过程,让内核吐路径时以 ns 根为起点算相对路径。
2. **cgroup ns 为什么不改资源限额?**
   资源限额由 `task_struct->cgroups`(css_set)决定,cgroup ns 完全不动这个指针——它只改 `task_struct->nsproxy->cgroup_ns`,换的是 nsproxy 里的一个指针。视图(路径展示)和资源(配额记账)是两层正交的事,由两套独立机制管。
3. **`root_cset` 为什么是 `css_set *` 而不是 `cgroup *`?**
   一个进程同时归属多个 cgroup(v2 单一树上一个、v1 多树时每棵一个),钉一个 cset 等于一次钉住"在所有树上的根",复用 cgroup 子系统已有的 `cset_cgroup_from_root` 反查函数;钉 `cgroup *` 要维护数组、要为每棵树单独算,繁琐且容易漏。
4. **`/proc/<pid>/cgroup` 里看到的路径用谁的 cgroup ns 算?**
   用**读者**(当前进程)的 cgroup ns 算(`current->nsproxy->cgroup_ns`),不是被读进程的。宿主上读容器进程的 `/proc/<pid>/cgroup` 看到绝对路径(宿主 ns 的根是 `/`),容器内读自己的看到 `/`(容器 ns 的根是它自己)。
5. **cgroup ns 为什么 2016 年(4.6)才进内核?**
   ① 依赖 cgroup v2 统一层级先稳定(v1 多树时代路径裁剪语义混乱);② 前 6 种 ns 解决"容器能不能跑",cgroup ns 解决"跑起来后路径泄漏"这个更细的需求,需求出现得晚;③ 设计方案(用 `root_cset` + 复用 css_set_lock)是权衡过的最小方案,不是一上来就这么设计的。

### 想继续深入往哪钻

- **源码**:[`kernel/cgroup/namespace.c`](../linux/kernel/cgroup/namespace.c) 全文 152 行,15 分钟可以读完,是 namespace 家族里最小的实现文件。搭配 [`struct cgroup_namespace`](../linux/include/linux/cgroup.h#L769-L774)、[`init_cgroup_ns`](../linux/kernel/cgroup/cgroup.c#L213-L219) 一起看。
- **路径裁剪核心**:[`cgroup_path_ns_locked`](../linux/kernel/cgroup/cgroup.c#L2363-L2369) 只有 4 行,但它是整个视图裁剪的钥匙;顺着 `cset_cgroup_from_root` → [`__cset_cgroup_from_root`](../linux/kernel/cgroup/cgroup.c#L1369-L1402) 钻下去,能看到"init cset 走根"、"v2 走 dfl_cgrp 字段"、"v1 走链表"三条分支。
- **`/proc/<pid>/cgroup` 的展示**:[`proc_cgroup_show`](../linux/kernel/cgroup/cgroup.c#L6246-L6319),重点看 L6294 那行 `cgroup_path_ns_locked(..., current->nsproxy->cgroup_ns)`——读者视角的语义就体现在这里。
- **观测**:容器里 `cat /proc/self/cgroup`、宿主上 `cat /proc/<容器PID>/cgroup`,对比两份输出;`ls -l /proc/self/ns/cgroup` 看 cgroup ns 的符号链接;`unshare -C sleep 100 && cat /proc/$$/status | grep Cgroup` 亲手创建一个 cgroup ns 看效果。
- **延伸**:看 [`cgroup_procs_write_permission`](../linux/kernel/cgroup/cgroup.c#L5086-L5115) 里的 `CGRP_ROOT_NS_DELEGATE` 分支,理解"视图边界对齐权限边界";systemd 和 runc 的 `nsdelegate` 挂载选项是怎么用的(附录 B 给运行时对照表)。
- **历史**:commit `a9af3cd`(2016,4.6)是 cgroup ns 合入的主线 commit;`Documentation/admin-guide/cgroup-v2.rst` 的 "Cgroup Namespaces" 一节有官方语义说明。

### 引出下一章

cgroup ns 是 namespace 家族和 cgroup 家族的最后一章细节。到这里,我们已经把"视图隔离(namespace 7 种)"和"资源控制(cgroup 6 个 controller + v2 统一层级 + cgroup ns)"全部拆透——容器这个盒子的两面墙都立清楚了。下一章(第 20 章,P5-20),我们站在全书的高度收束:梳理 Linux 容器的哲学(namespace 切视图不切物理、css_set 多对多去重、CLONE_NEW* 一次创多 ns、cgroup v2 单一树、user ns kuid_t 解耦),并给一张**内核能力 → 运行时接口 → 容器效果**的对照总表,把本书(内核积木)和 runc/Docker/K8s(用户态组装)钉成"容器全栈"。
