# 第二十章 · Linux 容器的哲学 + ★对照 runc/Docker/K8s 总表

> 篇:P5 收尾 · 哲学与对照总表
> 主线呼应:19 章走下来,我们从"为什么需要容器"出发,拆了 7 种 namespace 各自怎么切视图、6 个 cgroup controller 各自怎么限资源、组装篇怎么用 `clone(CLONE_NEW*)`+`unshare`/`setns`+`pivot_root`+写 `cgroup.procs` 把它们拼成盒子,以及 cgroup v2 的统一层级为什么取代 v1。这一章不写新源码,只做一件事——**把 19 章的能力收束成一张哲学 + 一张总表**:哲学部分讲清 Linux 容器七条贯穿性的设计取向(为什么"换指针不换数据"、为什么"中间加一层 css_set"、为什么"函数指针表驱动可插拔"、为什么"层级即语义"、为什么"kuid_t 类型解耦"等);总表部分给一张"**内核能力 → 运行时接口 → 容器效果**"的三列对照,把全书各章和 runc/Docker/K8s 钉成"容器全栈"——内核积木(namespace + cgroup)与用户态组装(runc 按 OCI 规范)一对照,你就看清了内核给了什么、运行时怎么把它拼成容器、Docker/K8s 又在上面加了什么。读完这一章,全书闭合:一个 `docker run nginx`,你能从内核的 `copy_namespaces` 一直讲到 K8s 的 Pod 调度,中间每一环都钉得死死的。

## 核心问题

**走完 19 章,容器这件事能提炼成哪几条"哲学"?每个组件在工程上做了什么取舍?给一张表把"内核积木 → 用户态组装 → 容器效果"对齐,内核给了什么、runc 怎么用、Docker/K8s 在上层加了什么?为什么"内核只给原语,组装交给用户态"这个分工是对的,而不是让内核直接提供 `create_container` 系统调用?**

读完本章你会明白:

1. **七条容器设计哲学**:视图即指针、资源即记账、聚合换原子性、去重换 scale、函数指针换可插拔、层级换语义、类型换安全。这七条贯穿全书,是 Linux 内核工程美学的核心。
2. **一张总表把全书钉死**:内核能力(`copy_namespaces`/`cgroup_attach_task`/`pivot_root`/`css_set`/`make_kuid`/...)、运行时接口(runc 的 `unix.Clone`/`unix.Setns`/`unix.PivotRoot`/写 `cgroup.procs`/写 `uid_map`)、容器效果(容器看不见宿主进程、CPU/内存被限、根换成镜像、容器 root 不是宿主 root)三列对齐。
3. **"内核积木 vs 用户态组装"这个分工为什么 sound**:内核只提供最小化的、可组合的、不带策略的原语(namespace + cgroup),把"造容器"的策略交给用户态——这让内核不被"容器"这种特定上层概念绑架,也让用户态可以演化出 Docker/K8s/Kata/gVisor/firecracker 等多种运行时,共享同一套内核底座。
4. **容器与 VM 的根本差异在哪一行代码**:容器跑在宿主内核上(没有客户机内核),VM 跑在 hypervisor 上(有客户机内核)。这一行差异决定了容器的全部优势和全部软肋。
5. ★ 对照 runc 的真实代码点:runc 在哪几行 Go 代码里调了 `clone(CLONE_NEW*)`、`setns`、`unix.PivotRoot`、写 `cgroup.procs`、写 `uid_map`——它们对应内核的哪些函数,以及 Docker/K8s 在 runc 之上加了什么。

> **逃生阀**:如果只想带一样东西走,带那张"**内核能力 → 运行时接口 → 容器效果**"对照总表(本章 20.4 节)。它把全书 19 章压缩成一页,丢了哪章回去查就行。哲学七条是为了让你"知其所以然",总表是为了让你"查得到、对得上"。

---

## 20.1 一句话点破

> **容器不是一个东西,是一套组装关系——内核提供七种 namespace 切视图(换指针不换数据)、六类 cgroup 限资源(记账到 cgroup 账上)、user ns 用 kuid_t 把身份解耦,runc 按 OCI 规范用 `clone`/`unshare`/`setns`/写 `cgroup.procs`/`pivot_root` 把它们拼成盒子,Docker 加镜像管理、K8s 加编排——内核只给原语、把策略留给用户态,这个分工让同一套内核底座能演化出从 Docker 到 gVisor 到 firecracker 的整个云原生生态。**

这是结论,不是理由。本章倒过来拆:先把 19 章的设计提炼成七条哲学(20.2),再把"内核只给原语、用户态组装"这个分工讲清(20.3),然后给出对照总表(20.4),最后用 runc 真实代码印证(★ 20.5),并和 VM/jail/Zones 等其他隔离方案做对照(20.6),指出本书没讲透的边界和延伸(20.7)。

---

## 20.2 七条哲学:容器是怎么被设计出来的

19 章读下来,你可能被 `nsproxy`/`css_set`/`cgroup_attach_task`/`uid_map`/`pernet_ops`/`pivot_root` 这一堆名词淹没。但它们不是散点,而是**同一种工程美学**的七种体现。把它们提炼成七条,你就抓住了 Linux 容器的设计哲学。

### 哲学一:视图即指针,不复制数据(namespace 的根)

第 1 章(P0-01)和第 2 章(P1-02)讲透的这条,是容器**最反直觉也最根本**的设计:`namespace` 不是"复制一份宿主的进程表/网络栈/挂载树给容器",而是给容器进程的 `task_struct->nsproxy` 换一组指针,让它指向另一组(可能新建的)ns 对象。物理上,宿主内核里只有**一张**进程表、**一套**网络栈、**一棵**挂载树;每个进程根据自己的 `nsproxy` 看到不同的"投影"。

```c
/* include/linux/sched.h:1110 */
struct task_struct {
    struct nsproxy *nsproxy;   /* 一组 7 个 ns 指针,换指针 = 换视图 */
    ...
};
```

([`include/linux/sched.h`](../linux/include/linux/sched.h#L1110)、[`include/linux/nsproxy.h`](../linux/include/linux/nsproxy.h#L32-L42))

> **不这样会怎样**:如果 namespace 是"复制一份数据",容器要拷一份进程表(几百 MB)、一份网络栈(协议栈各层数据结构)、一份挂载树(整棵 mount 树)。这会让容器启动慢成 VM 的级别、内存开销巨大,云原生"秒级拉起几百个微服务"根本不可能。换指针不换数据,是容器比 VM 轻一个数量级的根本——它把"隔离"从"物理隔离"降级成"视图过滤",用"看不到别人"换"启动快、密度高"。

这条哲学贯穿全书:第 3 章 mnt namespace 的 `copy_tree` 整树复制([`fs/namespace.c:1969`](../linux/fs/namespace.c#L1969))复制的是**挂载点对象**,不是文件数据(文件 inode 还是同一份);第 4 章 pid namespace 的 `pid->numbers[]`([`include/linux/pid_namespace.h`](../linux/include/linux/pid_namespace.h))是"一个进程在不同层 ns 里各有一个 pid 号",不是"多个进程";第 5 章 net namespace 的 `struct net` 是另一组**网络数据结构指针**(路由表、iptables 规则),不是另一份网卡硬件。**所有 ns 都在做同一件事:换指针,不换数据。**

### 哲学二:资源即记账,不换执行环境(cgroup 的根)

namespace 只解决"看到什么",没解决"能用多少"。cgroup(control group)解决后者,它的设计哲学同样反直觉:**不是"把进程关进一个独立的执行环境",而是"把进程的每次资源消耗(一个 tick、一个 page、一次 IO)都记到所属 cgroup 的账上,超了就拦"**。

```c
/* include/linux/sched.h:1233-1235 */
struct task_struct {
    struct css_set __rcu *cgroups;   /* 一组 css 指针,记账的入口 */
    struct list_head cg_list;
    ...
};
```

([`include/linux/sched.h`](../linux/include/linux/sched.h#L1233-L1235))

`task_struct->cgroups` 指向一个 [`struct css_set`](../linux/include/linux/cgroup-defs.h#L217-L305)(cgroup-defs.h:217),`css_set` 内部是一组 `subsys[CGROUP_SUBSYS_COUNT]` 指针,每个指针指向一个 [`cgroup_subsys_state`](../linux/include/linux/cgroup-defs.h#L160)(css,即"某 controller 在某 cgroup 里的实例")。任务消耗资源时:

- 第 11 章(P2-11)CPU:调度器按 task 的 `cpu` css 算它的时间片配额,超 `cpu.max` 就 [`throttle_cfs_rq`](../linux/kernel/sched/fair.c);
- 第 12 章(P2-12)内存:每次 page 分配走 [`mem_cgroup_try_charge`](../linux/mm/memcontrol.c)(mm/memcontrol.c),超 `memory.max` 触发 OOM;
- 第 13 章(P2-13)IO:每个 bio 走 blk-iocost/blk-iolatency 的令牌桶,超 `io.max` throttle;
- 第 14 章(P2-14)进程数/冻结/绑核:`pids` 的计数器、`freezer` 的 TIF_FROZEN 状态、`cpuset` 的 `cpus_allowed`。

这些 controller **没有**把进程关进"独立的 CPU/内存/IO 硬件",而是给宿主的每次资源消耗**记一笔账**,记到任务的 css 上,超了就拦。这是 cgroup 和 VM hypervisor(把 CPU/内存物理切给客户机)的本质差别:**VM 是物理切分,cgroup 是逻辑记账**。

> **不这样会怎样**:如果 cgroup 是"物理切分"(像 hypervisor 那样把 CPU 核/内存页静态分给容器),容器要么承担切换开销(切 CPU 上下文),要么无法利用空闲资源(分给 A 的 CPU A 不用,B 也用不了)。逻辑记账让"超卖"(overcommit)成为可能——一台 64 核机器可以给 100 个容器各发 1 核的配额,只要它们不同时满载。这是云原生密度的根。

### 哲学三:聚合换原子性(`nsproxy` 与 `css_set` 的共同骨架)

第 1 章(P0-01)和第 2 章(P1-02)反复强调的这条,是 Linux 管理 `task_struct` 上多指针资源的看家本领:**把多个相关指针收进一个中间结构,换指针时一次赋值原子完成**。

- **`nsproxy` 聚合 7 种 ns 指针**:`task_struct->nsproxy` 是一个指针,指向 [`struct nsproxy`](../linux/include/linux/nsproxy.h#L32-L42)(7 个 ns 指针聚在里面)。切视图 = `task->nsproxy = new_nsp;` 一行赋值([`nsproxy.c:186`](../linux/kernel/nsproxy.c#L186))。
- **`css_set` 聚合 15 个 css 指针**:`task_struct->cgroups` 是一个指针,指向 [`struct css_set`](../linux/include/linux/cgroup-defs.h#L217)(15 个 css 指针聚在里面)。换归属 = 改一个 `cgroups` 指针([`css_set_move_task`](../linux/kernel/cgroup/cgroup.c#L870))。

这两套设计是**完全同构**的:

```
 聚合换原子性(两套同构):

 task_struct                          task_struct
 ├─ nsproxy ──► struct nsproxy        ├─ cgroups ──► struct css_set
 │             ├─ uts_ns              │             ├─ subsys[cpuset]
 │             ├─ ipc_ns              │             ├─ subsys[cpu]
 │             ├─ mnt_ns              │             ├─ subsys[memory]
 │             ├─ pid_ns_for_children │             ├─ subsys[io]
 │             ├─ net_ns              │             ├─ subsys[pids]
 │             ├─ time_ns             │             ├─ subsys[freezer]
 │             └─ cgroup_ns           │             └─ subsys[...](共 15)
 │                                   │
 └─ 换视图 = 改 1 个 nsproxy 指针     └─ 换归属 = 改 1 个 cgroups 指针
    一行赋值,原子                       一次 list_move + 改指针,原子
```

> **不这样会怎样**:如果 7 个 ns 指针散落在 `task_struct` 各处,切视图要改 7 个字段,中间任何一刻被别的 CPU 读到就是"半新半旧"(新 mnt + 旧 pid,根文件系统换了进程表没换,行为不可预测)。聚合一次,指针赋值在所有架构上都原子(对齐读写不撕裂),**视图切换天然原子,不用任何额外锁**。15 个 css 同理——聚合让"换归属"和"换视图"一样,是一行指针赋值的事。

这条哲学不只用在 nsproxy 和 css_set,还出现在 `files_struct`(fd 表)、`sighand_struct`(信号 handler 表)、`fs_struct`(根/pwd)、`cred`(凭据)——`task_struct` 上所有"一组相关资源"都被聚合成一个中间结构,管理它们 = 管理一个指针。这是 Linux 内核代码复用的深层统一性。

### 哲学四:去重换 scale(`css_set` 哈希表的命脉)

第 9 章(P2-09)用整章讲的这条,是 cgroup 能扛大规模容器场景的命脉:**同归属的任务共享一个 `css_set`,通过一张全局哈希表去重**。

一个 K8s 节点跑 300 个 pod,每个 pod 平均 30 个进程,9000 个 task_struct。同一 pod 内的 30 个进程,它们的 15 个 css 归属**完全相同**(都在同一个 cgroup 节点、同一组启用的 controller 上)。如果每个任务独立存 15 个指针:

- 9000 × 15 = 135000 次指针存储(99% 冗余);
- 每次 fork/exit 要 inc/dec 15 次 atomic(跨 CPU cache line);
- 迁移整 pod 要改 30 × 15 = 450 个字段。

Linux 的解法是在 task 和 cgroup 之间加一层 `css_set`,**归属完全相同的任务共享一个 cset**。新建/迁移时,先在 [`css_set_table`](../linux/kernel/cgroup/cgroup.c#L910) 哈希表里查有没有现成的([`find_existing_css_set`](../linux/kernel/cgroup/cgroup.c#L1051),cgroup.c:1051)——有就复用(`refcount++`),没有才 [`find_css_set`](../linux/kernel/cgroup/cgroup.c#L1170)(cgroup.c:1170)`kzalloc` 一个新的。结果:

- 9000 个任务压缩到 300 个 cset(每个 pod 一个),省 30 倍内存;
- fork/exit 只 inc/dec 一次 cset 的 `refcount`(单 cache line);
- 迁移整 pod 的 30 个任务 = 把它们的 `task->cgroups` 指针都换成另一个 cset,目标 cset 还可能复用现成的(零分配)。

> **不这样会怎样**:如果不去重,几千核大机上的 cgroup hot path(fork/exit/迁移)会被**内存冗余 + 原子操作 cache line 反弹 + 迁移开销**三重压垮。云原生密度根本顶不住。

这条哲学和第 8 本《内存分配器》的 per-CPU cache、《mm》的 per-cpu pageset、第 11 本《调度器》的 per-CPU rq 是**同一脉的内核工程美学**——**用一层间接 + 一张去重表,把多对多关系压缩成少数等价类**,消灭 hot path 上的并发瓶颈。它是"用结构设计消灭问题"的典范。

### 哲学五:函数指针表换可插拔(`cgroup_subsys` / `proc_ns_operations` / `pernet_ops` 的共同骨架)

第 2 章(P1-02)末尾的 `ns_common` + `proc_ns_operations`、第 9 章(P2-09)的 `cgroup_subsys`、第 5 章(P1-05)的 `pernet_ops`——这三个看起来无关的设计,其实是**同一种"用函数指针表实现多态"的 C 语言手法**的三次应用:

| 子系统 | 共通接口表 | 各实现填一份 | 核心路径分发方式 |
|--------|-----------|-------------|----------------|
| namespace | [`struct proc_ns_operations`](../linux/include/linux/proc_ns.h#L16-L25)(get/put/install/owner) | 7 种 ns 各填一份(`mntns_operations`/`netns_operations`/...) | `ns->ops->install(nsset, ns)` 多态调用([`validate_ns`](../linux/kernel/nsproxy.c#L363)) |
| cgroup | [`struct cgroup_subsys`](../linux/include/linux/cgroup-defs.h#L688)(css_alloc/online/attach/fork/exit) | 15 个 controller 各填一份(`memory_cgrp_subsys`/`cpu_cgrp_subsys`/...) | `ss->css_alloc()` / `ss->fork(task)` 多态调用([`cgroup_init_subsys`](../linux/kernel/cgroup/cgroup.c#L5978)) |
| net ns | [`struct pernet_operations`](../linux/include/linux/net_namespace.h)(init/exit) | 各协议模块各填一份(`loopback_net_ops`/`inet_net_init`/...) | `for_each_pernet_operators(op) op->init(net)` 遍历([`setup_net`](../linux/net/core/net_namespace.c#L320)) |

```c
/* 这三处代码长得几乎一样(以 cgroup_subsys 为例) */
struct cgroup_subsys {
    struct cgroup_subsys_state *(*css_alloc)(struct cgroup_subsys_state *parent_css);
    int   (*css_online)(struct cgroup_subsys_state *css);
    void  (*attach)(struct cgroup_taskset *tset);
    void  (*fork)(struct task_struct *task);
    void  (*exit)(struct task_struct *task);
    ...
};

/* 核心通过函数指针调:*/
css = ss->css_alloc(parent_css);   /* 让 controller 自己造 css */
ss->fork(task);                    /* 让 controller 自己处理 fork */
```

> **不这样会怎样**:如果核心代码硬编码调每个 controller(`if (ssid == memory_cgrp_id) mem_cgroup_css_alloc(...); else if (ssid == cpu_cgrp_id) ...`),每加一个 controller 就要改核心的几十处 `if-else`,核心代码和 controller 耦合死。函数指针表把"契约"和"实现"解耦:**核心只认函数指针表的签名,controller 各填一份**;新增 controller 不改核心一行代码。

这条哲学让 namespace 子系统能从 2002 年的 mnt ns 一路扩展到 2020 年的 time ns,核心几乎不动;让 cgroup 能从最初的 4 个 controller 长到 15 个;让 net ns 能在创建时遍历每个协议模块的 init 回调。**它是 Linux 内核可扩展性的根基**,和 VFS 的 `file_operations`、调度器的 `sched_class`、中断的 `irq_chip` 是同一套思路。

### 哲学六:层级即语义(pid ns / user ns / cgroup 树的统一模式)

第 4 章(P1-04)的 pid ns、第 8 章(P1-08)的 user ns、第 9 章(P2-09)的 cgroup 树——这三者看起来无关,但都遵循同一种"层级即语义"的设计:**每个对象有一个 parent 指针,层级关系决定了视图、权限、限额的传播规则**。

- **pid ns 层级**(`struct pid_namespace.parent`):进程在每一层 pid ns 里各有一个 pid 号(`pid->numbers[]`),高层(宿主)能看到所有低层(容器)的进程,低层看不到高层的。**层级决定可见性**。
- **user ns 层级**(`struct user_namespace.parent`,`level`):`uid_map` 把本 ns 的 uid 翻译成**父 ns** 的 uid(不是直接全局 kuid),递归到底层才是全局。capability 通过 `ns_capable(ns, cap)` 沿祖先链检查。**层级决定权限边界**。
- **cgroup 树**(`struct cgroup.parent`,`level`):子 cgroup 的资源用量自动累加到所有祖先(第 12 章 memcg 的层级 charge);限额在父 cgroup 设置,子 cgroup 继承(`subtree_ss_mask`)。**层级决定资源边界**。

```c
/* 三个 parent 指针,同一种设计:*/
struct pid_namespace  { struct pid_namespace  *parent; ... };   /* kernel/pid_namespace.c */
struct user_namespace { struct user_namespace *parent; int level; ... };   /* include/linux/user_namespace.h:72 */
struct cgroup         { struct cgroup         *parent; int level; ... };   /* include/linux/cgroup-defs.h:397 */
```

层级带来的核心收益是**自动传播**——给父节点设一个限额/权限,所有后代自动继承;子节点的用量自动汇总到父节点。这让"给一个部门/租户/容器设一组限额,它下面的所有子单位自动遵守"成立。第 18 章(P4-18)的 cgroup v2 "no internal process"约束就是为了让这种传播干净——避免"父 cgroup 一边直接住进程、一边给子 cgroup 提供 controller"破坏统计链。

> **不这样会怎样**:如果没有层级(扁平结构),你要给 100 个容器各设一组限额,得手动对齐 100 次配置;父限额变了,100 个子配置要逐个改。层级让"组织结构 = 配置结构",管理成本随树深度对数增长,而不是随节点数线性增长。这是 cgroup v2 取代 v1 的核心理由——v1 多树结构破坏了这种层级语义(第 18 章)。

### 哲学七:类型换安全(kuid_t 与 cred 的编译期防线)

第 8 章(P1-08)讲透的这条,是容器安全的基石,也是一种**用类型系统强制区分易混淆概念**的工程手法:**内核里的全局 uid 不再是 `uid_t`,而是新类型 `kuid_t`,两者不能直接赋值**——编译器帮你抓 bug。

```c
/* include/linux/uidgid.h */
typedef struct { uid_t val; } kuid_t;   /* 全局 uid,内核记账用 */
/* uid_t 仍是 unsigned int,用户态/ns 局部视图 */

/* 两者转换必须显式走映射表 */
kuid_t make_kuid(struct user_namespace *ns, uid_t uid);   /* ns 局部 → 全局 */
uid_t  from_kuid(struct user_namespace *ns, kuid_t kuid); /* 全局 → ns 局部 */
```

([`make_kuid`](../linux/kernel/user_namespace.c#L411)、[`from_kuid`](../linux/kernel/user_namespace.c#L430))

所有"在内核里唯一标识一个用户"的地方(inode owner、task 的 real/effective uid、capability 检查目标)都用 `kuid_t`;从用户态进来的、可能是 ns 局部的 uid 用 `uid_t`。两者要互换,必须传 `user_namespace` 走 `uid_map` 映射表。这让"容器里 uid 0"在内核里被翻译成"宿主 uid 100000"(`kuid_t`),它拥有的 `CAP_SYS_ADMIN` 只在这个 user ns 里有效(`ns_capable` 检查祖先链),碰不到宿主的 `init_user_ns`。

> **不这样会怎样**:如果内核继续用 `uid_t` 一个类型贯穿全栈,工程师写代码时根本分不清"这个 uid 是来自用户态输入(ns 局部)还是内核记账(全局)"。一个不经意的 `inode->i_uid = stat.uid`(把 ns 局部 uid 当成 inode owner)就成了安全漏洞——容器里 uid 0 把自己的文件改成 uid 0,宿主上看起来就是 root 拥有。`kuid_t` 的类型系统让这种错误在**编译期**就被抓到。

这条哲学和第 13 本《同步原语》把 `atomic_t` 单列一个类型(防止"误把普通 int 当原子变量")、第 8 本《内存分配器》用 tagged pointer 区分元数据/指针是同一思路——**用类型设计换正确性,让编译器替你抓最容易犯的那类错**。

---

## 20.3 内核只给原语,组装交给用户态:这个分工为什么 sound

讲完七条哲学,接下来一个根本问题:**为什么内核不直接提供一个 `create_container` 系统调用,把 namespace + cgroup + rootfs 全包了?** 这看起来"更方便",但 Linux 偏偏选择了**只给最小化的、可组合的、不带策略的原语**,把"造容器"的策略留给用户态。这个分工为什么 sound?

### 三个理由:内核稳定、用户态演化、安全审计

**理由一:内核不想被"容器"这个特定上层概念绑架。** 容器是 2013 年 Docker 火起来之后才成为主流概念的,而 namespace 2002 年就进了内核(mnt ns)、cgroup 2007 年进的。如果当年内核提供一个 `create_container` 把"容器"语义固化下来,后来出现的新玩法(rootless 容器、Kata/gVisor 沙箱、firecracker microVM、Wasm 运行时)都得改内核。**只给原语、不组合**的策略,让内核底座十年不变,上层却演化出整个云原生生态。

**理由二:用户态演化速度快过内核。** 内核一个版本两年,加一个系统调用要走邮件列表、几轮 review、上游合并、然后等发行版 adopt,周期 3~5 年。用户态运行时(runc/crun/youki/runsc)几周发一个版本,可以快速试新(比如 rootless 容器的 user ns 映射策略、Kata 的 hypervisor 集成)。把"造容器"的策略放用户态,迭代速度快一个数量级。

**理由三:安全审计的边界更清晰。** 内核只暴露**最小特权原语**(`clone`/`unshare`/`setns`/写 `cgroup.procs`/写 `uid_map`),每个原语自带一套权限检查(`CAP_SYS_ADMIN`/`ns_capable`/打开时凭证)。用户态运行时把这些原语按特定顺序组合(runc 的三件套),组合的"正确性"(顺序有没有错、权限有没有漏)可以由用户态代码审计 + 内核原语权限检查**两道防线**保证。如果内核包办,组合逻辑藏在内核里,出了逃逸 CVE 很难定位是哪一步的漏洞。

### 这个分工在源码里的样子:用户态调 5 个 syscall,内核各管各的

把这个分工摊开看:

```
 用户态(runc,Docker 的运行时):
 ┌──────────────────────────────────────────────────────────┐
 │ 1. clone(CLONE_NEWNS|CLONE_NEWPID|CLONE_NEWNET|... )     │  造命名空间
 │ 2. unshare(...) / setns(fd, ...)                         │  微调视图
 │ 3. pivot_root(new_root, put_old)                         │  换根
 │ 4. echo $PID > /sys/fs/cgroup/X/cgroup.procs             │  关进限额
 │ 5. echo "0 100000 65536" > /proc/$PID/uid_map            │  设 uid 映射
 │ ────────────────────────────────────────────────────     │
 │   按 OCI 规范组合成"容器"                                  │
 └──────────────────────────────────────────────────────────┘
              │
              │ 系统调用边界(每个 syscall 自带权限检查)
              ▼
 内核(只给原语,各管各的,不知道"容器"为何物):
 ┌──────────────────────────────────────────────────────────┐
 │ kernel/fork.c      : clone → copy_process → copy_namespaces
 │ kernel/nsproxy.c   : copy_namespaces / switch_task_namespaces / setns
 │ fs/namespace.c     : pivot_root / copy_mnt_ns
 │ kernel/cgroup/     : cgroup.procs 写 → cgroup_attach_task
 │ kernel/user_namespace.c : uid_map 写 → map_write → make_kuid/from_kuid
 │ ────────────────────────────────────────────────────     │
 │   各原子能力,没有"container"这个概念                      │
 └──────────────────────────────────────────────────────────┘
```

注意**内核里没有任何一个函数叫 `create_container`**。`copy_namespaces` 不知道自己在造容器,它只管"按 `CLONE_NEW*` 标志位决定共享还是复制 nsproxy";`cgroup_attach_task` 不知道自己在造容器,它只管"把任务的 `css_set` 换一个";`pivot_root` 不知道自己在造容器,它只管"交换两棵 mount 子树"。**容器是用户态把它们按特定顺序组装出来的"涌现现象"——内核各管各的,组合策略在用户态**。

> **钉死这件事**:Linux 容器的分工是**内核只给最小化的、可组合的、不带策略的原语**(`clone`/`unshare`/`setns`/`pivot_root`/写 `cgroup.procs`/写 `uid_map`),**用户态运行时按策略组装**(runc 按 OCI 规范)。内核因此能保持底座稳定十年以上,用户态因此能演化出 Docker/K8s/Kata/gVisor/firecracker 整个生态。这是"机制与策略分离"(mechanism vs policy)的经典应用——**机制放内核,策略放用户态**。

---

## 20.4 对照总表:内核能力 → 运行时接口 → 容器效果

这是本章的核心交付物——**一张表把全书 19 章 + runc + Docker/K8s 钉成"容器全栈"**。三列对齐:**内核能力**(本书各章讲的机制)、**运行时接口**(runc 按 OCI 规范调的 syscall/文件)、**容器效果**(用户/Docker/K8s 看到的现象)。

### 20.4.1 namespace 视图隔离部分

| 内核能力 | 运行时接口(runc) | 容器效果 | 本书章节 |
|---------|------------------|---------|---------|
| [`copy_namespaces`](../linux/kernel/nsproxy.c#L151) 读 `CLONE_NEW*` 决定共享/复制;[`create_new_namespaces`](../linux/kernel/nsproxy.c#L67) 一次性造 7 种 ns,全成或全回滚 | `clone(CLONE_NEWNS \| CLONE_NEWPID \| CLONE_NEWNET \| CLONE_NEWIPC \| CLONE_NEWUTS \| CLONE_NEWUSER \| CLONE_NEWCGROUP, ...)` | 一次系统调用造出全套独立视图;容器启动没有"半新半旧"窗口 | P1-02 |
| [`copy_mnt_ns`](../linux/fs/namespace.c#L3760) + [`copy_tree`](../linux/fs/namespace.c#L1969) 整树复制挂载点;`pivot_root` 重新组织挂载拓扑 | `unix.PivotRoot(".", ".")`([runc rootfs_linux.go:1164](../runc/libcontainer/rootfs_linux.go));或 `mount+chroot` 兜底 | 容器看到自己的根文件系统(镜像);`ls /` 是镜像内容,不是宿主 | P1-03 / P3-17 |
| [`copy_pid_ns`](../linux/kernel/pid_namespace.c) + `pid->numbers[]` 多层 pid | 同上 clone 的 `CLONE_NEWPID` | 容器里 PID 1 是容器 init;`ps` 只看到容器内进程 | P1-04 |
| [`copy_net_ns`](../linux/net/core/net_namespace.c#L479) + [`setup_net`](../linux/net/core/net_namespace.c#L320) 遍历 `pernet_ops` 初始化各协议栈 | 同上 clone 的 `CLONE_NEWNET`;veth pair 跨 ns 连通 | 容器有独立 eth0/lo/路由表/iptables;网络和宿主隔离 | P1-05 |
| [`copy_utsname`](../linux/kernel/utsname.c#L89) 复制 `new_utsname` | 同上 clone 的 `CLONE_NEWUTS` | 容器有独立 hostname | P1-06 |
| [`copy_ipcs`](../linux/ipc/namespace.c) 复制 ipc `ids` 表 | 同上 clone 的 `CLONE_NEWIPC` | 容器有独立 SysV IPC/POSIX 消息队列 | P1-07 |
| [`create_user_ns`](../linux/kernel/user_namespace.c#L82) + [`map_write`](../linux/kernel/user_namespace.c#L923) 写 `uid_map`;`kuid_t` + [`make_kuid`](../linux/kernel/user_namespace.c#L411)/[`from_kuid`](../linux/kernel/user_namespace.c#L430) 双向映射 | `clone(CLONE_NEWUSER)` + 写 `/proc/$PID/uid_map`("0 100000 65536") | 容器里 uid 0 = 宿主 uid 100000;容器 root 不是宿主 root(rootless 容器基石) | P1-08 |
| [`copy_cgroup_ns`](../linux/kernel/cgroup/namespace.c) 裁剪 cgroup 路径视图 | 同上 clone 的 `CLONE_NEWCGROUP` | 容器里 `/proc/self/cgroup` 只看到自己路径 | P4-19 |
| [`switch_task_namespaces`](../linux/kernel/nsproxy.c#L239) 用 `task_lock` 原子切视图;[`prepare_nsset`](../linux/kernel/nsproxy.c#L331)/[`commit_nsset`](../linux/kernel/nsproxy.c#L512) 两阶段 setns | `unix.Setns(fd, CLONE_NEWNS\|CLONE_NEWPID\|...)`([runc process_linux.go:716](../runc/libcontainer/process_linux.go));fd = `open("/proc/$PID/ns/mnt")` | `docker exec` / `kubectl exec` 进入已有容器;`nsenter` 工具 | P3-16 |

### 20.4.2 cgroup 资源控制部分

| 内核能力 | 运行时接口(runc) | 容器效果 | 本书章节 |
|---------|------------------|---------|---------|
| [`cgroup_init_early`](../linux/kernel/cgroup/cgroup.c#L6037) → [`cgroup_init_subsys`](../linux/kernel/cgroup/cgroup.c#L5978) → [`cgroup_init`](../linux/kernel/cgroup/cgroup.c#L6074) boot 时建起 `cgrp_dfl_root` 单一树 | 无(内核 boot 自建,`/sys/fs/cgroup/` 挂载点) | 系统启动后有一棵统一 cgroup 树,Docker/K8s 在其下建子目录 | P2-09 |
| [`css_set`](../linux/include/linux/cgroup-defs.h#L217) 去重表 + [`find_existing_css_set`](../linux/kernel/cgroup/cgroup.c#L1051)/[`find_css_set`](../linux/kernel/cgroup/cgroup.c#L1170) 哈希查找 | 无(内核自动去重,用户态无感) | 同归属进程共享一个 cset;cgroup 能 scale 到几万进程 | P2-09 |
| [`__cgroup_procs_write`](../linux/kernel/cgroup/cgroup.c#L5138) → [`cgroup_attach_task`](../linux/kernel/cgroup/cgroup.c#L2866) → [`cgroup_migrate`](../linux/kernel/cgroup/cgroup.c#L2836) 四步迁移 | `echo $PID > /sys/fs/cgroup/X/cgroup.procs` | 把进程关进限额 cgroup;CPU/内存/IO/PID 同时套上 | P2-10 / P3-17 |
| `cpu.max` → `cfs_bandwidth` → [`throttle_cfs_rq`](../linux/kernel/sched/fair.c);`cpu.weight` → 调度权重;组调度复用 `sched_entity` | `echo "100000 200000" > cpu.max`(50% CPU);`echo 100 > cpu.weight` | 容器 CPU 被限;超配额 throttle(回扣《调度器》P6-19) | P2-11 |
| `memory.max` → [`mem_cgroup_try_charge`](../linux/mm/memcontrol.c) 每 page charge;超 → OOM kill;`memory.swap.max`;层级累加 | `echo 536870912 > memory.max`(512MB);`echo 0 > memory.swap.max` | 容器内存被限;超 OOM kill 容器内进程(回扣《mm》) | P2-12 |
| `io.max` → blk-iocost/blk-iolatency 令牌桶;per-device 限额 | `echo "8:16 rbps=10485760 wbps=10485760" > io.max` | 容器 IO 被限;超 throttle(回扣《块设备》) | P2-13 |
| `pids.max` → css 计数器;`freezer` → TIF_FROZEN 状态机;`cpuset.cpus`/`cpuset.mems` 绑核/内存节点 | `echo 100 > pids.max`;`echo "+frozen" > cgroup.freeze`;`echo "0-3" > cpuset.cpus` | 容器进程数上限;整组冻结(比 SIGSTOP 安全);绑核 | P2-14 |
| 单一树 + `cgroup.subtree_control` + `no internal process` 约束;`cgroup.type`(domain/threaded) | `echo "+memory +cpu" > cgroup.subtree_control` | 所有 controller 共用一棵树;v1 的归属矛盾消失 | P4-18 |

### 20.4.3 组装部分

| 内核能力 | 运行时接口(runc 全流程) | 容器效果 | 本书章节 |
|---------|----------------------|---------|---------|
| [`copy_namespaces`](../linux/kernel/nsproxy.c#L151)(在 [`copy_process`](../linux/kernel/fork.c#L2393)) + [`create_new_namespaces`](../linux/kernel/nsproxy.c#L67) | `clone(CLONE_NEWNS\|CLONE_NEWPID\|... , ...)`([runc libcontainer](../runc/libcontainer)) | 一次系统调用造全套命名空间 | P3-15 |
| [`unshare_nsproxy_namespaces`](../linux/kernel/nsproxy.c#L213) 从当前进程剥新 ns;[`SYSCALL_DEFINE1(unshare)`](../linux/kernel/fork.c#L3392) | `unshare(CLONE_NEWNS\|...)` | 从当前进程剥出新视图(runc 初始化用) | P3-16 |
| [`SYSCALL_DEFINE2(setns)`](../linux/kernel/nsproxy.c#L546) + [`prepare_nsset`](../linux/kernel/nsproxy.c#L331)/[`commit_nsset`](../linux/kernel/nsproxy.c#L512) 两阶段 | `unix.Setns(fd, CLONE_NEWNS)`([runc process_linux.go:716](../runc/libcontainer/process_linux.go)) | `docker exec`/`kubectl exec` 进入已有容器 | P3-16 |
| [`SYSCALL_DEFINE2(pivot_root)`](../linux/fs/namespace.c#L4179) 重新组织挂载树拓扑 | `unix.PivotRoot(".", ".")`([runc rootfs_linux.go:1164](../runc/libcontainer/rootfs_linux.go)) | 根文件系统换成镜像 | P3-17 |
| [`__cgroup_procs_write`](../linux/kernel/cgroup/cgroup.c#L5138) → [`cgroup_attach_task`](../linux/kernel/cgroup/cgroup.c#L2866) 四步迁移 | `echo $PID > /sys/fs/cgroup/X/cgroup.procs` | 进程被关进资源笼子(三件套最后一步) | P3-17 |
| **三件套顺序依赖**:必须先 ns 再 pivot 再 cgroup.procs | runc 按 OCI 规范严格按顺序调(父子进程 sync pipe 同步) | 顺序错→容器逃逸或卡死 | P3-17 |

### 20.4.4 内核不做的:Docker/K8s 在 runc 之上加的

内核和 runc 都不做的,是 Docker/K8s 这层的事:

| 上层能力 | 谁做 | 不在本书范围(本书讲内核 + runc 接口) |
|---------|------|--------------------------------|
| 镜像管理(pull/build/层叠) | Docker/containerd | 镜像是 tar 包,内核只看 rootfs |
| 卷管理(volume plugin) | Docker/K8s CSI | 卷是挂载点,内核只管 mount |
| 网络插件(CNI:calico/flannel/...) | K8s CNI | 网络插件配 veth/bridge/iptables,内核只提供 net ns 和 netfilter |
| 编排(调度到哪个节点、副本数、滚动升级) | K8s scheduler/controller | K8s 调度是用户态策略 |
| 服务发现/负载均衡 | K8s kube-proxy/Service | 用 netfilter/iptables,内核只提供包过滤 |
| 镜像签名/安全扫描 | Docker/cosign/... | 用户态供应链安全 |

> **钉死这件事**:这张总表是本书的"地图"。任何时候你忘了某个能力在内核哪一层、runc 怎么用、用户看到什么,回来查这张表。三列对齐:**内核给机制,runc 组装,Docker/K8s 在上做策略**。这是"容器全栈"的完整骨架。

---

## 20.5 ★ 对照 runc:真实 Go 代码里的容器组装

总表说了"runc 调哪些 syscall",这一节用 runc 的**真实 Go 代码**佐证——它们就在 [`runc/libcontainer/`](../runc/libcontainer) 里。

### 20.5.1 clone 标志位:runc 怎么挑 ns

runc 的 ns 配置在 [`configs/namespaces_syscall.go`](../runc/libcontainer/configs/namespaces_syscall.go),它把每种种 ns 映射到 `CLONE_NEW*`:

```go
/* runc/libcontainer/configs/namespaces_syscall.go(简化) */
var namespaceInfo = map[NamespaceType]int{
    NEWNET:    unix.CLONE_NEWNET,
    NEWNS:     unix.CLONE_NEWNS,
    NEWUSER:   unix.CLONE_NEWUSER,
    NEWIPC:    unix.CLONE_NEWIPC,
    NEWUTS:    unix.CLONE_NEWUTS,
    NEWPID:    unix.CLONE_NEWPID,
    NEWCGROUP: unix.CLONE_NEWCGROUP,
}
```

([runc/libcontainer/configs/namespaces_syscall.go:12-18](../runc/libcontainer/configs/namespaces_syscall.go))

OCI spec 里声明的 ns 列表 → 这些 `CLONE_NEW*` 位 → 传给 `clone()` 系统调用 → 内核 [`copy_namespaces`](../linux/kernel/nsproxy.c#L151) 读这些位、走 [`create_new_namespaces`](../linux/kernel/nsproxy.c#L67)。**用户态的 ns 列表(OCI)→ `CLONE_NEW*` 位掩码 → 内核的 `copy_*_ns` 范式**,这条链路完整闭合。

### 20.5.2 setns:`docker exec` 进入容器

runc 的 `exec` 路径用 `unix.Setns` 进入已有容器的 ns,在 [`libcontainer/process_linux.go:716`](../runc/libcontainer/process_linux.go):

```go
/* runc/libcontainer/process_linux.go(简化) */
if err := unix.Setns(int(nsFd.Fd()), unix.CLONE_NEWNS); err != nil {
    return err
}
```

`nsFd` 来自 `open("/proc/<容器PID>/ns/mnt")`(或 mnt/pid/net/... 各 ns)。这个 fd 在内核里对应一个 `nsfs` inode,inode 的 `i_private` 指向 `struct ns_common`;`setns(fd, flags)` 进内核后用 [`prepare_nsset`](../linux/kernel/nsproxy.c#L331) → `validate_ns` → `ops->install`([`nsproxy.c:363`](../linux/kernel/nsproxy.c#L363)),最后 [`commit_nsset`](../linux/kernel/nsproxy.c#L512) 一次性 `switch_task_namespaces`([`nsproxy.c:542`](../linux/kernel/nsproxy.c#L542)) 挂上。**两阶段 commit**,prepare 全成才 commit,不会半新半旧。

### 20.5.3 pivot_root:换根

runc 的 [`rootfs_linux.go:1164`](../runc/libcontainer/rootfs_linux.go) 调 `unix.PivotRoot`:

```go
/* runc/libcontainer/rootfs_linux.go(简化,L1144-L1170) */
// pivotRoot will call pivot_root such that rootfs becomes the new root
if err := unix.PivotRoot(".", "."); err != nil {
    return &os.PathError{Op: "pivot_root", Path: ".", Err: err}
}
```

注意 `pivot_root(".", ".")` 这个特殊用法——runc 注释([L1147](../runc/libcontainer/rootfs_linux.go))解释:`pivot_root(".", ".")` 是合法的(虽然 manpage 说要不同路径),它让新根和 put_old 都是当前目录,省去创建临时目录。这进内核 [`SYSCALL_DEFINE2(pivot_root)`](../linux/fs/namespace.c#L4179) → `do_pivot_root` 重新组织挂载拓扑。如果 rootfs 是 ramfs(不支持 pivot_root),runc 走 [`chroot`](../runc/libcontainer/rootfs_linux.go) 兜底([`configs/config.go:97-99`](../runc/libcontainer/configs/config.go) 的 `NoPivotRoot` 字段)。

### 20.5.4 cgroup.procs:关进资源笼子

runc 在 [`libcontainer/cgroups`](../runc/libcontainer/cgroups) 子目录里管理 cgroup。对 cgroup v2,它把容器 PID 写进 `cgroup.procs`(Go 的 `os.WriteFile`),触发内核 [`__cgroup_procs_write`](../linux/kernel/cgroup/cgroup.c#L5138) → [`cgroup_attach_task`](../linux/kernel/cgroup/cgroup.c#L2866) 四步迁移。同时 runc 还写 `cpu.max`/`memory.max`/`pids.max` 等 controller 文件(每个文件在内核里是一个 `cftype` 的 `.write` 回调,见 [`cgroup_base_files`](../linux/kernel/cgroup/cgroup.c#L5210) 附近的注册)。

### 20.5.5 uid_map:rootless 容器的身份映射

runc 的 rootless 模式([`libcontainer/internal/userns/`](../runc/libcontainer/internal/userns))用 `clone(CLONE_NEWUSER)` 创 user ns,然后通过 [`userns_maps_linux.c`](../runc/libcontainer/internal/userns/userns_maps_linux.c) 的 C helper 写 `uid_map`/`gid_map`:

```c
/* runc/libcontainer/internal/userns/userns_maps_linux.c(简化,L46) */
int err = setns(nsfd, CLONE_NEWUSER);   /* 先 setns 进新 user ns */
/* 然后写 /proc/self/uid_map */
```

这进内核 [`proc_uid_map_write`](../linux/kernel/user_namespace.c#L1111) → [`map_write`](../linux/kernel/user_namespace.c#L923) 把"容器 uid `[0, 65536)` ↔ 父 ns uid `[100000, 165536)`"写进 [`uid_gid_map`](../linux/include/linux/user_namespace.h#L17) 的 extent 数组。从此容器里 uid 0 经 [`make_kuid`](../linux/kernel/user_namespace.c#L411) 翻译成宿主 uid 100000。

### 20.5.6 Docker 和 K8s 在 runc 之上加了什么

runc 是 OCI 运行时(低级),Docker 和 K8s 不直接调 runc,而是通过 **containerd**(Docker 拆出来的容器运行时守护进程)或 **CRI-O**(K8s 的轻量运行时)间接调:

```
 用户 ──► docker/kubectl ──► containerd/CRI-O ──► runc(low-level runtime)
                                │
                                ├─ 镜像管理(pull/build,层叠 overlayfs)
                                ├─ 卷管理(volume plugin,CSI)
                                ├─ 网络插件(CNI: calico/flannel/...)
                                └─ 日志/监控
                                              │
                                              ▼
                                           runc(create/start/exec)
                                              │
                                              ▼
                                      Linux kernel(namespace + cgroup)
```

K8s 在这之上还加了 **scheduler**(把 Pod 调度到哪个节点)、**controller manager**(副本数/滚动升级)、**kube-proxy**(Service 负载均衡,用 iptables/IPVS)、**etcd**(集群状态)。但所有这些,最终都落到 runc 这套动作上,runc 又都落到内核的 namespace + cgroup 上——**"容器全栈"从顶到底贯通**。

---

## 20.6 容器 vs VM vs jail vs Zones:为什么是 Linux 容器赢了

本书通篇讲 Linux 容器,但容器这个概念不是 Linux 独有。横向对照一下,能看清 Linux 容器的设计取舍:

| 隔离方案 | 隔离机制 | 隔离强度 | 启动速度 | 密度 | 生态 |
|---------|---------|---------|---------|------|------|
| **全虚拟化 VM**(KVM/Xen/VMware) | hypervisor 仿真硬件 + 客户机内核 | 最强(连内核隔离) | 慢(几十秒) | 低(每 VM 一个客户机内核) | 强(成熟) |
| **半虚拟化 VM**(Xen PV) | hypervisor + 改过的客户机内核 | 强 | 中 | 中 | 中 |
| **Linux 容器** | namespace + cgroup(共享宿主内核) | 中(共享内核是软肋) | 快(秒级) | 高(几百/节点) | 极强(Docker/K8s) |
| **FreeBSD jail**(1999) | `chroot` + `jail()` syscall | 中 | 快 | 高 | 弱(生态小) |
| **Solaris Zones**(2004) | zone + 资源池 | 中-强(更细的内核隔离) | 快 | 高 | 弱(OpenSolaris 没落) |
| **Wasm 运行时**(2020s) | Wasm 字节码沙箱 | 中(语言级隔离) | 极快(毫秒) | 极高 | 成长中 |
| **gVisor**(Google) | 用户态内核(拦截 syscall) | 强(内核接口被过滤) | 中 | 中 | 小众 |
| **Kata Containers** | 轻量 VM + 容器接口 | 强(VM 级隔离 + 容器接口) | 中 | 中 | 成长中 |

为什么 Linux 容器在云原生赢了?**不是因为它隔离最强(VM 更强),而是因为它在"隔离/启动速度/密度/生态"四个维度上平衡最好**——共享宿主内核换来了秒级启动和几百/节点的高密度,namespace+cgroup 提供了"够用"的隔离,再加上 Docker/K8s 在用户态做出了生态规模。这是一个**工程上的胜利**,不是技术上最优的胜利——它接受了"共享内核是安全软肋"的代价(一堆 CVE:runc 逃逸 CVE-2019-5736、CVE-2019-14271、dirty COW、.overlayfs 等),换来了部署密度。

**Linux 容器的安全软肋和缓解**:

- **软肋**:容器和宿主共享内核。任何内核漏洞(尤其是 syscall 路径、namespace 创建、cgroup 写文件路径的 race)都可能被容器内进程利用逃逸到宿主。
- **缓解 1**:user ns(P1-08)把容器 root 关进独立 uid 空间,即使逃逸也只是宿主 nobody。
- **缓解 2**:seccomp 过滤 syscall(默认 Docker 只开 ~50 个白名单 syscall)。
- **缓解 3**:AppArmor/SELinux 强制访问控制。
- **缓解 4**:对隔离要求高的场景用 Kata(gVisor/Kata Containers)——它们在 runc 接口之下加一层轻量 VM,每个容器/pod 一个客户机内核,代价是启动慢、密度低。

> **钉死这件事**:Linux 容器不是技术上最优的隔离方案,但它是**工程上最平衡的方案**——共享内核换密度,user ns/seccomp/AppArmor 补安全,加上 Docker/K8s 的生态规模,它在云原生赢了。理解这个权衡,才能理解为什么有 Kata/gVisor/firecracker 这些"更强隔离但更重"的方案存在——它们是为安全要求高的场景准备的,不是取代 Linux 容器,而是补充。

---

## 20.7 边界与延伸:本书没讲透的

收尾章要诚实——本书有几条线没讲透,这里点出来,作为延伸的起点。

### 20.7.1 设备 cgroup / BPF / seccomp:容器安全的其他支柱

本书讲了 user ns 这个"容器安全的基石",但容器安全是一个**纵深防御**体系,还有几个支柱:

- **devices controller**(cgroup v1 的 `devices` 子系统,cgroup v2 用 BPF 替代):限制容器能访问哪些设备节点(`/dev/sda` 等)。本书 P2-14 没展开。
- **seccomp-BPF**:过滤容器能调哪些 syscall。默认 Docker 的 seccomp profile 只放行 ~50 个 syscall,堵掉 `keyctl`/`kexec_load`/`unshare`(部分)等危险 syscall。
- **AppArmor/SELinux**:强制访问控制(MAC),给容器进程套一层"能访问哪些路径"的策略。
- **LSM(Linux Security Module)**:包括 BPF LSM,可以在 hook 点(如 `bprm_check_security`)做自定义策略。

这几个支柱**和 namespace/cgroup 是互补的**——namespace/cgroup 是"隔离 + 限额",seccomp/AppArmor/SELinux 是"过滤 + 强制访问"。容器安全是这五层叠加的结果。

### 20.7.2 runc 的 sync pipe / init 协议:父子进程的握手

P3-17 讲了三件套的顺序依赖,但没钻 runc 是**怎么在父子进程间同步**的——runc 用一根 `sync pipe`(Unix socketpair)在父进程(创 ns、写 cgroup)和子进程(被关进盒子后 exec 业务)之间传状态码。这套握手协议是 runc 工程的核心,OCI runtime-spec 定义了它的语义。延伸阅读 runc 源码 [`libcontainer/container_linux.go`](../runc/libcontainer/container_linux.go) 的 `Start`/`Exec` 路径,以及 [`nsenter`](../runc/libcontainer/nsenter)(C 语言,在 setns 之前 early init)。

### 20.7.3 cgroup v1 的遗留代码

本书以 cgroup v2 为主,v1 只在 P4-18 作对照。但生产环境仍有 v1(尤其 systemd 的 hybrid 模式),源码在 [`kernel/cgroup/cgroup-v1.c`](../linux/kernel/cgroup/cgroup-v1.c)。v1 的多树结构、`release_agent`、`tasks` 文件等本书没展开。

### 20.7.4 容器逃逸 CVE 的具体路径

附录 B 会讲几个经典逃逸 CVE:

- **CVE-2019-5736**:runc 本身被容器内进程覆盖(容器内 `/proc/self/exe` 指向宿主 runc 二进制,容器内替换它,下次 runc exec 时执行恶意代码)。缓解:runc 后续版本用 `memfd` 加载自己。
- **CVE-2019-14271**:dockerd 的 `docker cp` 加载了 nsswitch 模块,容器内可以放恶意 `.so` 提权。
- **dirty COW**(CVE-2016-5195):内核 race,可写只读映射,绕过文件权限。
- **overlayfs CVE**(CVE-2020-14386 等):overlayfs 的权限检查 race,容器内可写宿主文件。

这些 CVE 的共同点是——**它们都利用了"容器和宿主共享内核"这个根本前提**。某个内核子系统(syscall/文件系统/cgroup)出了 race,容器内进程能触发它,逃逸到宿主。这是 Linux 容器(共享内核)的固有风险,缓解只能靠 user ns + seccomp + 及时打补丁,或干脆换 Kata/gVisor。

---

## 20.8 技巧精解:聚合换原子性 —— 容器两层骨架的共同美学

本章是收束章,技巧精解挑全书**最贯穿**的一个技巧单独拆透——**聚合换原子性**。它不是某个具体函数的技巧,而是 `nsproxy`(P1-02)和 `css_set`(P2-09)共同的骨架设计,也是 Linux 管理 `task_struct` 上多指针资源的看家本领。

### 朴素方案为什么会撞墙

`task_struct` 上有大量"一组相关资源"需要管理:7 种 ns 指针(namespace)、15 个 css(cgroup)、fd 表(files)、信号 handler 表(sighand)、根/pwd(fs)、凭据(cred)。朴素地写,会把它们散落在 task_struct 各处:

```c
/* 朴素的、糟糕的写法(示意,非源码) */
struct task_struct {
    /* namespace:7 个指针散落 */
    struct mnt_namespace  *mnt_ns;
    struct uts_namespace  *uts_ns;
    struct ipc_namespace  *ipc_ns;
    struct pid_namespace  *pid_ns;
    struct net            *net_ns;
    /* cgroup:15 个指针散落 */
    struct cgroup_subsys_state *cpuset_css;
    struct cgroup_subsys_state *cpu_css;
    struct cgroup_subsys_state *memory_css;
    /* files:fd 表散落 */
    struct file *fd0, *fd1, *fd2, ...;
    /* ... 几十个字段,改起来要逐个原子操作 */
};
```

这撞上三面墙:

1. **改一组 = 改多个字段 = 不原子**。切视图要改 7 个字段,中间被别的 CPU 读到就是"半新半旧"。要避免只能加全局锁——但全局锁就失去了 per-task 独立切换的并发性。
2. **引用计数无法集中**。共享全部 ns 的多个任务(比如 fork 后不要 NEW 的子进程)本来该共享同一份引用计数,散落字段做不到——每个字段独立 inc/dec,fork/exit 几十次原子操作。
3. **没有 COW 共享层级**。每个 task_struct 持自己的几十个字段,fork 时每个字段都要判断"共享还是复制",逻辑复杂。

### Linux 的解法:聚合一个中间结构,换指针 = 换整组

Linux 反复用**同一种解法**:把"一组相关资源"收进一个中间结构,task_struct 只持一个指针,换指针 = 换整组。

| 资源组 | 聚合结构 | task_struct 里的指针 | 换指针的位置 |
|-------|---------|---------------------|-------------|
| 7 种 namespace | `struct nsproxy` | `task->nsproxy` | [`switch_task_namespaces`](../linux/kernel/nsproxy.c#L239)(nsproxy.c:239) |
| 15 个 css | `struct css_set` | `task->cgroups` | [`css_set_move_task`](../linux/kernel/cgroup/cgroup.c#L870)(cgroup.c:870) |
| fd 表 | `struct files_struct` | `task->files` | `task_lock` 下赋值 |
| 信号 handler 表 | `struct sighand_struct` | `task->sighand` | `task_lock` 下赋值 |
| 根/pwd | `struct fs_struct` | `task->fs` | `task_lock` 下赋值 |
| 凭据 | `struct cred` | `task->real_cred`/`cred` | `commit_creds`/`override_creds`(RCU) |

每一处都是同一套模式:**聚合 → 一个指针 → 指针赋值原子换整组**。

```c
/* nsproxy 切视图: */
task_lock(p);
ns = p->nsproxy;
p->nsproxy = new;           /* 一行赋值 */
task_unlock(p);
put_nsproxy(ns);

/* css_set 换归属(在 css_set_lock 下):*/
list_move(&task->cg_list, &to_cset->tasks);
rcu_assign_pointer(task->cgroups, to_cset);   /* 一行 RCU 赋值 */
```

([`switch_task_namespaces`](../linux/kernel/nsproxy.c#L239)、[`css_set_move_task`](../linux/kernel/cgroup/cgroup.c#L870))

### 这套设计 sound 在三点

**第一,指针赋值原子**。对齐的指针读写不撕裂(所有架构都保证),读者要么看到旧的、要么看到新的,不会看到半个指针。聚合让"换整组"等价于"换一个指针",这个原子性是**免费的**(架构保证的,不需要锁)。

**第二,引用计数集中**。聚合结构持一个 `refcount_t`,共享这组资源的任务共享这个计数。fork 不要新视图时 `get_nsproxy(old)` 一次 inc、exit `put_nsproxy` 一次 dec——**几十个字段的引用计数被压成一次原子操作**。

**第三,COW 友好**。共享全部资源的任务(99% 的 fork)直接共享聚合结构,零拷贝;只要有一个字段要变,才 copy 整个聚合结构,把要变的那个换成新的。nsproxy.h 的注释("As soon as a single namespace is cloned or unshared, the nsproxy is copied")、css_set 的"subsys 数组创建后不变"约束,都是 COW 的体现。

### 反面对比:为什么不用全局锁

如果朴素写法要避免"半新半旧",只能加一把全局 `task_resource_mutex`,所有 task_struct 的所有资源切换都串行化:

```c
/* 朴素的、糟糕的写法(示意) */
mutex_lock(&task_resource_mutex);
task->mnt_ns = new_mnt_ns;      /* 改一个字段 */
task->uts_ns = new_uts_ns;      /* 改另一个 */
/* ... 中间任何一刻被读者看到,是混合状态 */
task->net_ns = new_net_ns;
mutex_unlock(&task_resource_mutex);
```

这会让:① 任意两个 task 的资源切换串行化(两个不同容器各自 setns,本来完全无冲突却要互相等);② 一个容器的 ns 操作阻塞另一个容器的 fork(fork 也走 `copy_namespaces`)——在云原生高密度场景下是性能灾难;③ 读者也要拿锁(`task_resource_mutex`),拖慢所有读路径。

Linux 选择聚合 + per-task `alloc_lock`(`task_lock`)的方案,把切换并发性推到极限:**两个 CPU 可以同时切换两个不同 task 的 nsproxy,零竞争**。这是"用结构设计消灭锁"的典范。

> **钉死这件事**:**聚合换原子性**是 Linux 管理 task_struct 多指针资源的看家本领。`nsproxy`(namespace)、`css_set`(cgroup)、`files_struct`、`sighand_struct`、`fs_struct`、`cred`——六处都用同一套设计:**一个聚合结构 + 一个 task_struct 指针 + 指针赋值换整组 + refcount 集中管理 + COW 共享**。这不是巧合,是 Linux 内核代码复用的深层统一性。容器是建立在这个统一性之上的——`nsproxy` 切视图、`css_set` 切归属,两个聚合结构撑起了容器的全部骨架。理解这个设计模式,你就能在内核里读懂任何"一组资源"是怎么管理的。

---

## 章末小结

这是全书的**收束章**。我们没有写新源码,而是把 19 章的能力收束成**七条哲学 + 一张总表**:

1. **视图即指针**(namespace):换指针不换数据,容器比 VM 轻一个数量级。
2. **资源即记账**(cgroup):每次资源消耗记到 css 账上,超了就拦,不是物理切分。
3. **聚合换原子性**(`nsproxy` + `css_set`):一组资源收进一个中间结构,换指针 = 换整组,天然原子。
4. **去重换 scale**(`css_set` 哈希表):同归属任务共享一个 cset,把多对多压缩成等价类,消灭 hot path 瓶颈。
5. **函数指针表换可插拔**(`cgroup_subsys`/`proc_ns_operations`/`pernet_ops`):核心认契约,controller/ns/协议各填一份,新增不改核心。
6. **层级即语义**(pid ns / user ns / cgroup 树):parent 指针 + 层级传播,让"组织结构 = 配置结构"。
7. **类型换安全**(`kuid_t`):用类型系统把"全局 uid"和"ns 局部 uid"钉死,编译期防 bug。

这七条哲学 + 三列对照总表(内核能力 → 运行时接口 → 容器效果),把本书和 runc/Docker/K8s 钉成"容器全栈":**内核给机制,用户态组装,Docker/K8s 在上做策略**。这个分工让内核底座十年不变,用户态演化出整个云原生生态。

### 五个"为什么"清单

1. **为什么 Linux 容器不像 VM 那样物理隔离,而是"换指针 + 记账"?** 物理隔离要起客户机内核,启动慢、密度低;换指针 + 记账跑宿主内核,秒级启动、密度高——代价是隔离弱(共享内核),需要 namespace + cgroup + user ns + seccomp 多层补。
2. **为什么内核只给原语,不直接提供 `create_container` 系统调用?** 机制与策略分离。内核给最小化、可组合、不带策略的原语,用户态按策略组装——这让内核底座稳定十年,用户态演化出 Docker/K8s/Kata/gVisor 整个生态。
3. **为什么 `nsproxy` 和 `css_set` 是同构设计?** 它们都是"聚合换原子性"的应用——一组相关资源收进一个中间结构,task_struct 持一个指针,换指针 = 换整组。这套模式在内核里重复 6 次(nsproxy/css_set/files_struct/sighand_struct/fs_struct/cred)。
4. **为什么 cgroup 用函数指针表(`cgroup_subsys`),不硬编码调每个 controller?** 可插拔。核心只认函数指针表签名,controller 各填一份。新增 controller 不改核心一行代码——这是 cgroup 能从 4 个 controller 长到 15 个的架构原因。
5. **Linux 容器 vs VM vs jail vs Zones,为什么容器赢了?** 不是技术最优(VM 隔离更强),是工程最平衡:隔离够用 + 启动快 + 密度高 + 生态强。这是一个工程权衡的胜利,接受"共享内核是安全软肋"换密度。

### 想继续深入往哪钻

- **横向对比其他隔离方案**:Kata Containers(轻量 VM + 容器接口)、gVisor(用户态内核)、firecracker(microVM)、Wasm 运行时(WasmEdge/Wasmtime)——它们的隔离边界在哪,为什么更重。
- **容器安全纵深**:seccomp-BPF(过滤 syscall)、AppArmor/SELinux(MAC)、BPF LSM、devices controller(设备访问控制)。它们和 namespace/cgroup 互补,构成多层防御。
- **容器逃逸 CVE 案例**:CVE-2019-5736(runc 二进制覆盖)、CVE-2019-14271(docker cp nsswitch)、dirty COW、overlayfs CVE——它们都利用"共享内核"前提,理解它们能加固对容器安全边界的认识。
- **OCI 规范**:image-spec(镜像格式)、runtime-spec(运行时行为,包括 state transitions、namespaces、cgroup 路径、capabilities)。runc 是 runtime-spec 的参考实现。
- **K8s 调度与编排**:kubelet(containerd/CRI-O 调用 runc)、scheduler(节点选择)、kube-proxy(Service iptables/IPVS)、controller manager(副本/滚动升级)。它们在容器全栈的顶层。
- **源码再深入**:[`kernel/cgroup/cgroup.c`](../linux/kernel/cgroup/cgroup.c) 的 [`cgroup_init`](../linux/kernel/cgroup/cgroup.c#L6074)(L6074,boot 初始化)、[`cgroup_attach_task`](../linux/kernel/cgroup/cgroup.c#L2866)(L2866,迁移主体);[`kernel/nsproxy.c`](../linux/kernel/nsproxy.c) 的 [`copy_namespaces`](../linux/kernel/nsproxy.c#L151)(L151,fork)、[`switch_task_namespaces`](../linux/kernel/nsproxy.c#L239)(L239,运行时切);[`kernel/user_namespace.c`](../linux/kernel/user_namespace.c) 的 [`map_write`](../linux/kernel/user_namespace.c#L923)(L923,uid_map 写)、[`make_kuid`](../linux/kernel/user_namespace.c#L411)/[`from_kuid`](../linux/kernel/user_namespace.c#L430)(双向映射)。

### 收束

容器不是一个东西,是一套组装关系。内核提供七种 namespace 切视图、六类 cgroup 限资源、user ns 用 kuid_t 把身份解耦,runc 按 OCI 规范用 `clone`/`unshare`/`setns`/`pivot_root`/写 `cgroup.procs`/写 `uid_map` 把它们拼成盒子,Docker 加镜像管理、K8s 加编排。内核只给原语、把策略留给用户态,这个分工让同一套内核底座能演化出从 Docker 到 gVisor 到 firecracker 的整个云原生生态。

七条哲学、一张总表,把全书的 19 章和 runc/Docker/K8s 钉成"容器全栈"。一个 `docker run nginx`,你现在能从内核的 `copy_namespaces` 一直讲到 K8s 的 Pod 调度,中间每一环都钉得死死的。这是本书的终点——也是你深入容器生态任何分支的起点。

**全书完。**
