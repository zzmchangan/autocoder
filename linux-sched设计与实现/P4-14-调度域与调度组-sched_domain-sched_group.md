# 第十四章 · 调度域与调度组:sched_domain / sched_group

> 篇:第 4 篇 · SMP 负载均衡:核间搬任务
> 主线呼应:第 9 章(P2-09)讲清了 PELT——每个调度实体维护一份衰减的 `load_avg`/`util_avg`,让内核知道"这个任务现在有多想吃 CPU"。但负载跟踪只是把账记好,真正的问题还在后面:几十个核上挂着成百上千个任务,一个核忙死、一个核闲死怎么办?要把任务从忙核搬到闲核。可是"搬"远不像听起来那么简单——同样是从核 A 搬到核 B,搬到一个共享 L1/L2 的超线程兄弟代价几乎为零,搬到一个跨 NUMA 节点的核代价可能是几十倍。如果负载均衡不认拓扑、对所有 CPU 一视同仁地均衡,那么搬任务省下来的 CPU 时间,还不够填它丢掉的 cache。本章要讲的,就是把硬件拓扑(SMT、MC、PKG、NUMA)抽象成一组**调度域(sched_domain)**,让负载均衡按"先近后远"的层次来做。这是 SMP 负载均衡的地基,下一章(`load_balance`)直接建立在这上面。

## 核心问题

**SMP 机器不是一张扁平的 CPU 列表——核与核之间有 cache 共享、有同核超线程、有跨 NUMA 节点。负载均衡要认这个拓扑:只把任务在"近"的核间搬,远的不轻易搬。内核怎么把硬件拓扑抽象成数据结构,让均衡算法一层一层地做?**

读完本章你会明白:

1. 调度域层次:`sched_domain` 按 SMT → MC(共享 LLC) → PKG(物理 CPU) → NUMA 自底向上分层,每层有自己的 `imbalance_pct`、`cache_nice_tries` 等参数。
2. 调度组环形链表:一个 `sched_domain` 挂一组 `sched_group`,每个组对应子域的一颗子树,均衡的"忙/闲"是以**组**为单位统计的。
3. `sched_domain_shared` 与 `sd_llc`:cache 共享线索,唤醒选核和均衡都会用到。
4. 为什么这样分层:把"全局一把尺子衡量均衡"换成"按拓扑距离逐层衡量",保护 cache 局部性,NUMA 跨节点迁移被严格限制。
5. 二分法归属:**机制**——它服务负载均衡这个机制层,把"均衡的尺度"从 CPU 个数变成拓扑层次。

> 逃生阀:本章数据结构嵌套较深(`sched_domain` 套 `sched_group` 套 `sched_group_capacity`),初读可以只记一句话——"每个 CPU 一条由低到高的 sched_domain 链,均衡自底向上逐层做,近域先做、远域后做"。细节是为下一章做铺垫。

---

## 14.1 一句话点破

> **调度域不是"任务在哪里跑"的结构,而是"负载均衡在哪里做"的结构:每个 CPU 一条自底向上的 sched_domain 链,告诉均衡算法——你先在本核 SMT 兄弟间均衡,再在同 LLC 的核间均衡,最后才跨物理 CPU/NUMA 节点;越往上层,允许的不平衡容忍度越高、迁移代价越大、均衡频率越低。**

这是结论,不是理由。本章倒过来拆:先看硬件拓扑为什么逼出"分层均衡"这个需求,再看 `sched_domain`/`sched_group` 长什么样,再看内核怎么构建这棵树,最后看几个关键技巧。

---

## 14.2 为什么不能"全局一把尺子"地均衡

朴素的负载均衡想象是这样的:系统有 N 个 CPU,每个 CPU 维护一个运行队列;每隔一段时间,找个最忙的队列,从它上面拉一两个任务到最闲的队列,使所有队列负载大致相等。这在所有 CPU 完全对称、共享同一块内存和 cache 的小机器上没问题。但 SMP 早就不"对称"了:

```
 一台典型双路服务器(简化):

    NUMA node 0                              NUMA node 1
    ┌──────────────────────────┐             ┌──────────────────────────┐
    │  物理CPU 0               │             │  物理CPU 1               │
    │  ┌─────────┬─────────┐   │   QPI       │  ┌─────────┬─────────┐   │
    │  │  core0  │  core1  │   │ ◄─────────► │  │  core2  │  core3  │   │
    │  │ HT0 HT1 │ HT0 HT1 │   │  跨节点     │  │ HT0 HT1 │ HT0 HT1 │   │
    │  └─────────┴─────────┘   │  延迟高     │  └─────────┴─────────┘   │
    │   ↑共享 L1↑   ↑共享 L1↑  │             │                          │
    │   ↑── 共享 LLC(L3) ──↑   │             │   ↑── 共享 LLC ──↑       │
    │   ↑──── 本地 DRAM ────↑   │             │   ↑──── 本地 DRAM ──↑    │
    └──────────────────────────┘             └──────────────────────────┘

  cache 距离(粗略):
    同一 HT 内核:       0(同核共享一切)
    同 core 的 HT 兄弟: 共享 L1/L2(很近)
    同物理CPU 不同 core:共享 LLC(较近)
    跨物理CPU:          不共享 cache(远)
    跨 NUMA 节点:       不共享 cache 且 DRAM 远(很远)
```

不同距离的"搬一次任务"代价差别巨大。一个任务如果跑了一会儿,它的工作集已经热在当前核的 L1/L2 和 LLC 上;把它搬到同 LLC 的另一个核,数据在 LLC 里还在,代价小;搬到不同 LLC 的核,L1/L2 全冷、LLC 也没了,要重新从 DRAM 拉一遍;搬到跨 NUMA 节点,不仅 cache 全冷,连 DRAM 访问都变慢(远程节点延迟是本地的 1.5~2 倍)。

> **不这样会怎样**:如果负载均衡对所有 CPU 一视同仁——只要看到"核 A 比核 B 忙",就从 A 拉任务到 B,不管 B 在哪里——那么在 NUMA 机器上,均衡器会频繁把任务跨节点搬来搬去。每次搬移,任务的 cache 全冷、内存访问变慢;省下来的那点 CPU 份额,还不够填 cache 重建的开销。**全局均衡"看起来公平",实际整体吞吐反而下降**。这就是为什么负载均衡必须认拓扑。

但"认拓扑"具体怎么落到代码?Linux 的答案是:**把硬件拓扑抽象成一棵自底向上的调度域树**,每个调度域代表"一组 cache 距离相近的 CPU",让均衡算法在树上从低到高逐层执行——先在最底层(SMT/同 LLC)均衡,够便宜、最该做;最上层(NUMA)均衡代价大,做得最谨慎、最不频繁。

---

## 14.3 `sched_domain` 长什么样

调度域的真正定义不在 `kernel/sched/`,而在 [`include/linux/sched/topology.h`](../linux/include/linux/sched/topology.h#L87-L161)([topology.h:87](../linux/include/linux/sched/topology.h#L87))。我们摘核心字段:

```c
/* include/linux/sched/topology.h:87 (简化,删掉 SCHEDSTATS 调试字段) */
struct sched_domain {
    /* 这些字段在构建拓扑时设置 */
    struct sched_domain __rcu *parent;   /* 上一层域,顶层必须为 NULL */
    struct sched_domain __rcu *child;    /* 下一层域,底层必须为 NULL */
    struct sched_group *groups;          /* 本域的调度组环形链表 */
    unsigned long min_interval;          /* 最小均衡间隔,单位 ms */
    unsigned long max_interval;          /* 最大均衡间隔,单位 ms */
    unsigned int busy_factor;            /* CPU 忙时把间隔乘这个倍数,降频均衡 */
    unsigned int imbalance_pct;          /* 不平衡容忍度,百分比 */
    unsigned int cache_nice_tries;       /* 留 cache 热任务几次再迁 */
    unsigned int imb_numa_nr;            /* NUMA 允许的少量不平衡任务数 */

    int nohz_idle;
    int flags;                           /* SD_* 标志位 */
    int level;                           /* 本域在第几层(0=SMT 底层) */

    /* 运行时字段 */
    unsigned long last_balance;          /* 上次均衡时刻(jiffies) */
    unsigned int balance_interval;       /* 当前均衡间隔,动态调整 */
    unsigned int nr_balance_failed;      /* 连续失败计数 */

    /* idle_balance() 代价统计 */
    u64 max_newidle_lb_cost;
    unsigned long last_decay_max_lb_cost;
    ...
    struct sched_domain_shared *shared;  /* 共享数据(见 14.5) */

    unsigned int span_weight;
    unsigned long span[];                /* 本域覆盖的 CPU 掩码(变长) */
};
```

几个字段值得专门看一眼,因为下一章 `load_balance` 会反复用它们:

- **`parent` / `child`**:`sched_domain` 不是孤岛,而是**每个 CPU 一条自底向上的链**。CPU 0 的链可能是 SMT→MC→PKG→NUMA,CPU 8(另一节点)的链也是 SMT→MC→PKG→NUMA,但两条链在 PKG 以下完全不同、到了 NUMA 层(如果系统是单 NUMA)才合流。`for_each_domain(cpu, sd)` 宏([sched.h:1834](../linux/kernel/sched/sched.h#L1834))就是遍历这条链:`for (sd = cpu_rq(cpu)->sd; sd; sd = sd->parent)`。
- **`groups`**:每个域挂一组 `sched_group`(见 14.4),代表"本层下面的子树"。
- **`imbalance_pct`**:**不平衡容忍度**。本域内最忙组与最闲组的负载差小于这个百分比就不均衡,典型的 SMT 域是 110、MC 域是 117、NUMA 域默认也是 117。这个数值直接控制下一章 `find_busiest_group` 的阈值——越高的值意味着越能容忍不均衡、均衡越懒,适合代价高的层级。
- **`balance_interval` / `min_interval` / `max_interval` / `busy_factor`**:本域**多久均衡一次**。`min_interval = sd_weight`(本域 CPU 数),`max_interval = 2 * sd_weight`([topology.c:1599](../linux/kernel/sched/topology.c#L1599)),`busy_factor = 16`。如果 CPU 忙,实际间隔会乘以 `busy_factor`,意思是"我已经很忙了,别老来打扰我做均衡";如果上次均衡失败(没找到可迁的任务),`balance_interval` 翻倍(见 P4-15 的 `load_balance` 末尾),慢慢退避,避免空转。
- **`flags`**:一堆 `SD_*` 标志,决定本域"该不该均衡、能不能跨 fork/exec 均衡、是否是 NUMA 域"等。最常用的几个在 [`include/linux/sched/sd_flags.h`](../linux/include/linux/sched/sd_flags.h#L51-L170)([sd_flags.h:110](../linux/include/linux/sched/sd_flags.h#L110)):`SD_SHARE_CPUCAPACITY`(SMT,共享算力)、`SD_SHARE_LLC`(共享 LLC,MC 域)、`SD_NUMA`(NUMA 域)、`SD_ASYM_PACKING`(异构算力,如 big.LITTLE)、`SD_PREFER_SIBLING`(提示上层"我们组愿意多吐任务出去")。
- **`cache_nice_tries`**:**给 cache 热的任务几次豁免**。在 `can_migrate_task` 里,如果一个任务刚跑过(`task_hot` 返回真),内核会看 `sd->cache_nice_tries`,数够几次才允许迁——典型的 MC 域是 1,NUMA 域是 2(见 14.3 末尾的 `sd_init`)。

> **钉死这件事**:`sched_domain` 把"负载均衡的尺子"参数化、层次化了。每个域有自己的容忍度、间隔、cache 豁免次数——这些数值在 `sd_init` 里按域类型不同地设置(见 14.6 的 `sd_init`),不是凭空来的。

---

## 14.4 `sched_group`:均衡的真正单位

光有 `sched_domain` 还不够——一个域可能覆盖很多 CPU(比如 NUMA 顶层覆盖整机的几十上百个 CPU),负载均衡要从里面挑"最忙的 CPU",总不能每次都线性扫描。Linux 又在每个域下面挂了一组 **`sched_group`**,把本域切成几个等价的子集,均衡先在组级别统计,再下沉到组内找最忙的 CPU。

`sched_group` 的定义在 [`kernel/sched/sched.h:1923`](../linux/kernel/sched/sched.h#L1923-L1941):

```c
/* kernel/sched/sched.h:1923 */
struct sched_group {
    struct sched_group *next;      /* 必须是循环链表 */
    atomic_t           ref;

    unsigned int       group_weight;  /* 组内 CPU 数 */
    unsigned int       cores;
    struct sched_group_capacity *sgc; /* 组的算力信息 */
    int                asym_prefer_cpu; /* ASYM_PACKING 组里最高优先级的 CPU */
    int                flags;

    unsigned long      cpumask[];     /* 本组覆盖的 CPU(变长) */
};
```

关键字段:

- **`next`**:`sched_group` 在一个域内是**循环链表**(`sd->groups` 指向其中一个,沿 `next` 转一圈回到自己)。这是为了均衡时方便遍历"本域所有组"。
- **`sgc`** (`sched_group_capacity`):组的**算力**字段,定义在 [sched.h:1904](../linux/kernel/sched/sched.h#L1904-L1921)。含 `capacity`(组的总算力,`SCHED_CAPACITY_SCALE=1024` 是单核满算力)、`min_capacity`/`max_capacity`(组内 CPU 算力范围,异构 CPU 用)、`next_update`、`imbalance`(标记本组是否因亲和约束卡住了不均衡)。`sgc` 跨域共享——所有指向同一组 CPU 的 `sched_group` 共用一个 `sgc`,节省内存又保证一致。
- **`cpumask`**:本组覆盖哪些 CPU。注意 `group_weight = hweight(cpumask)`。

那一个 `sched_group` 到底代表什么?topology.c 的注释([topology.c:1128-1197](../linux/kernel/sched/topology.c#L1128-L1197))讲得最清楚,这里翻译并图示:

> 一个 `sched_domain` "上上下下"地在拓扑层次间移动,而一个 `sched_group` "横向"地穿过这个层次,粒度是子域级别。**`sched_domain` 的第一个 `sched_group` 就是它的子域。**

举个具体例子(8 核 2 cache cluster,见 topology.c 注释):

```
 CPU   0   1   2   3   4   5   6   7
            (来自 topology.c:1156-1168)

 PKG  [================================]   域:span = {0..7}
       └ group {0..3} ──► group {4..7} ┘   组:两个 MC 子树

 MC   [            ] [            ]        域:span = {0..3}(对 CPU 0..3)
       └ sg{0,1} ─► sg{2,3} ─► sg{0,1}    组:每个组是 1 个核的 HT 兄弟

 SMT  [    ] [    ] [    ] [    ]          域:span = {0,1}(对 CPU 0 和 1)
       └ sg{0} ─► sg{1} ┘                  组:每个组是单个 CPU

 CPU 0 看到的 sched_domain 链(自底向上):
   SMT(level 0, span={0,1})  ──parent──►  MC(level 1, span={0..3})
        └─►child                              └─►child
                                                  ▼
       PKG(level 2, span={0..7}) ◄──parent──  (上面那层)
```

CPU 0 的 SMT 域 `groups` 指向一个循环链表 `{cpu0} → {cpu1} → {cpu0}`,每个 `sched_group` 是一个 CPU;它的 MC 域 `groups` 是 `{0,1} → {2,3} → {0,1}`,每个组是 2 个 HT(一个核);它的 PKG 域 `groups` 是 `{0..3} → {4..7} → {0..3}`,每个组是一个 LLC 子树。

> **为什么这样设计**:负载均衡在每一层都分两步——先找最忙的**组**,再在最忙的组里找最忙的**队列**。组的存在,让"找最忙"这件事在大机器上不用每次扫所有 CPU:NUMA 顶层只要扫几个组(组 = 物理CPU),找到最忙组,下沉到 MC 域,再下沉到 SMT,逐层缩小搜索范围。而且组级统计可以**直接复用 PELT 累加**:一个组的负载就是组内所有 CPU 上 `cfs_rq` 的 `load_avg` 之和,见下一章的 `update_sd_lb_stats`。

---

## 14.5 `sched_domain_shared` 与 `sd_llc`:cache 共享线索

除了 `sched_domain` 和 `sched_group`,还有一个对唤醒选核和均衡都很关键的小结构——`sched_domain_shared`,定义在 [topology.h:80-85](../linux/include/linux/sched/topology.h#L80-L85):

```c
/* include/linux/sched/topology.h:80 */
struct sched_domain_shared {
    atomic_t ref;
    atomic_t nr_busy_cpus;   /* 本 LLC 域里有多少 CPU 在忙 */
    int      has_idle_cores; /* 本 LLC 域里是否有空闲核 */
    int      nr_idle_scan;   /* 找空闲核时扫多少个 */
};
```

它挂在 `SD_SHARE_LLC` 域的 `sd->shared` 上(`sd_init` 里见 [topology.c:1674](../linux/kernel/sched/topology.c#L1674)),每个 CPU 一个 per-CPU 实例,但**指向同一组 LLC 的所有 CPU 共享同一个**——所以叫 `shared`。这是个轻量级跨 CPU 通讯渠道:CPU 忙起来就 `atomic_inc(&sd->shared->nr_busy_cpus)`,闲下去就 `atomic_dec`。

内核还有一组 per-CPU 全局变量缓存最关键的 LLC 域指针([topology.c:668-695](../linux/kernel/sched/topology.c#L668-L695)):

```c
/* topology.c:668 */
DEFINE_PER_CPU(struct sched_domain __rcu *, sd_llc);          /* 本 CPU 的 LLC 域 */
DEFINE_PER_CPU(int, sd_llc_size);                             /* LLC 域 CPU 数 */
DEFINE_PER_CPU(int, sd_llc_id);                               /* LLC 域 ID */
DEFINE_PER_CPU(struct sched_domain_shared __rcu *, sd_llc_shared); /* LLC 域的 shared */
DEFINE_PER_CPU(struct sched_domain __rcu *, sd_numa);         /* 本 CPU 的 NUMA 域 */
```

唤醒路径(`try_to_wake_up` → `select_task_rq` → `select_idle_sibling`)和 idle 均衡(下一章)都会直接读 `sd_llc`——"这个任务被唤醒时,优先放到和它上一个 CPU **同一个 LLC** 的空闲核上",因为那样它的 cache 还可能热着。这是为什么唤醒选核不读整条 `sched_domain` 链、而直接拿 LLC 域——快,而且命中率高。

> **钉死这件事**:`sched_domain_shared` 把"本 LLC 域里还有几个 CPU 闲着"这种高频查询的信息放到原子变量里,让任何 CPU 都能 O(1) 读。如果没有它,唤醒选核每次都要扫一遍本 LLC 域所有 CPU 的 `rq->nr_running`,在几十核的 LLC 域上是肉眼可见的开销。

---

## 14.6 拓扑是怎么构建出来的:`default_topology` 与 `build_sched_domains`

那这棵 `sched_domain` 树是怎么从硬件信息里长出来的?答案在 [`topology.c:1688`](../linux/kernel/sched/topology.c#L1688-L1702) 的 `default_topology`:

```c
/* topology.c:1688 (注释说 "bottom-up",从底到顶) */
static struct sched_domain_topology_level default_topology[] = {
#ifdef CONFIG_SCHED_SMT
    { cpu_smt_mask,       cpu_smt_flags,    SD_INIT_NAME(SMT) },
#endif
#ifdef CONFIG_SCHED_CLUSTER
    { cpu_clustergroup_mask, cpu_cluster_flags, SD_INIT_NAME(CLS) },
#endif
#ifdef CONFIG_SCHED_MC
    { cpu_coregroup_mask, cpu_core_flags,   SD_INIT_NAME(MC) },
    { cpu_cpu_mask,       SD_INIT_NAME(PKG) },
    { NULL, },
};
```

这是一张**拓扑层级表**:每一项是一个 `sched_domain_topology_level`([topology.h:195](../linux/include/linux/sched/topology.h#L195-L204)),包含一个 `mask` 函数指针(给定 CPU,返回本层域覆盖哪些 CPU)、一个 `sd_flags` 函数指针(本层域该带哪些 `SD_*` 标志)、一个名字。`SMT`/`CLS`/`MC`/`PKG`/`NUMA` 这些宏开关由硬件特性(有没有超线程、有没有 cluster、是不是 NUMA)在编译时决定。

那这些 `cpu_smt_mask`/`cpu_coregroup_mask` 这些函数怎么知道拓扑?它们来自**体系结构代码**,在 x86 上是 `arch/x86/kernel/smpboot.c` 根据 CPUID 探测出来的 `cpu_llc_shared_mask`、`cpu_core_map`、`cpu_sibling_map`(顶级互连拓扑表),通过 `include/linux/topology.h` 的 `topology_physical_package_id`/`topology_core_id`/`topology_thread_cpumask` 等抽象暴露给调度器。换句话说,**硬件拓扑由 arch 探测,通过 `topology.h` 抽象,被 `default_topology` 表组装成 sched_domain 树**。

树的构建主函数是 [`build_sched_domains`](../linux/kernel/sched/topology.c#L2383)([topology.c:2383](../linux/kernel/sched/topology.c#L2383)),启动时 `sched_init_smp`([core.c:9863](../linux/kernel/sched/core.c#L9863))会调它。核心逻辑分两步:

1. **遍历拓扑层级表**(自底向上),给每个 CPU 在每一层建一个 `sched_domain`(`sd_init` 初始化字段),用 `parent`/`child` 把同一 CPU 的各层串起来。`build_sched_domain` 在 [topology.c:2313](../linux/kernel/sched/topology.c#L2313-L2341) 把子域挂到父域上,且强制 `child->span ⊆ parent->span`——这是拓扑的数学不变量(子域必是父域子集),不满足会打 `pr_err("BUG: arch topology borken\n")`。
2. **建组**:`get_group`([topology.c:1199](../linux/kernel/sched/topology.c#L1199))用每个子域的第一个 CPU 作为"组的代表 CPU",把同一组 CPU 复用同一个 `sched_group`,然后把这些组串成循环链表挂到父域的 `groups` 上。

最关键的初始化函数是 [`sd_init`](../linux/kernel/sched/topology.c#L1574-L1683)([topology.c:1574](../linux/kernel/sched/topology.c#L1574)),它给不同层级的域打不同的"参数",这就是"分层均衡"落到代码的地方:

```c
/* topology.c:1574,简化 */
sd_init(struct sched_domain_topology_level *tl, ...)
{
    ...
    *sd = (struct sched_domain){
        .min_interval    = sd_weight,     /* 本域 CPU 数 */
        .max_interval    = 2 * sd_weight,
        .busy_factor     = 16,
        .imbalance_pct   = 117,           /* 默认 117% */
        .cache_nice_tries = 0,
        .flags           = 1*SD_BALANCE_NEWIDLE | 1*SD_BALANCE_EXEC
                         | 1*SD_BALANCE_FORK | 1*SD_WAKE_AFFINE
                         | 1*SD_PREFER_SIBLING | sd_flags,
        .balance_interval = sd_weight,
        ...
    };
    ...
    /* 按域类型差异化调整 */
    if (sd->flags & SD_SHARE_CPUCAPACITY) {
        sd->imbalance_pct = 110;          /* SMT:容忍度最低,该均衡就均衡 */

    } else if (sd->flags & SD_SHARE_LLC) {
        sd->imbalance_pct = 117;          /* MC(LLC):默认值 */
        sd->cache_nice_tries = 1;

#ifdef CONFIG_NUMA
    } else if (sd->flags & SD_NUMA) {
        sd->cache_nice_tries = 2;         /* NUMA:cache 热任务多豁免几次 */
        sd->flags &= ~SD_PREFER_SIBLING;  /* NUMA 不要"偏好吐任务" */
        sd->flags |= SD_SERIALIZE;        /* NUMA 域要全局串行化均衡 */
        ...
#endif
    } else {
        sd->cache_nice_tries = 1;
    }

    /* LLC 域:挂 sched_domain_shared */
    if (sd->flags & SD_SHARE_LLC) {
        sd->shared = *per_cpu_ptr(sdd->sds, sd_id);
        atomic_set(&sd->shared->nr_busy_cpus, sd_weight);
    }
    ...
}
```

读这张表你能看出"分层"的硬证据:

| 层级 | `imbalance_pct` | `cache_nice_tries` | 含义 |
|---|---|---|---|
| SMT(`SD_SHARE_CPUCAPACITY`) | 110 | 0 | 同核 HT 兄弟,迁过去几乎免费,容忍度最低,该均衡就均衡 |
| MC(`SD_SHARE_LLC`) | 117 | 1 | 共享 LLC,迁过去 L1/L2 冷但 LLC 还热,中等容忍 |
| NUMA(`SD_NUMA`) | 117 | 2 | 跨节点,代价最大,cache 热任务多豁免几次,且要全局串行 |

> **所以这样设计**:把"该多容忍、该多频繁均衡、cache 热任务该几次豁免"这些参数,按拓扑层级**差异化**地写到每个 `sched_domain`。下一章 `load_balance` 在每一层调一次,读这些参数决定阈值——**就自动做到了"近域勤均衡、远域懒均衡"**。如果只有一个全局参数表,你只能选一个值,SMT 该勤却变懒、NUMA 该懒却变勤,两败俱伤。

---

## 14.7 配图:调度域层次树与组链

把上面讲的拼成一张图,你应该看到这样的结构(双路 8 核服务器,简化为 4 核示意):

```
 双 NUMA 节点,每节点 2 核 4 线程(简化):

     NUMA 0 节点                               NUMA 1 节点
     ┌──────────────────────┐                  ┌──────────────────────┐
     │ CPU0 CPU1 CPU2 CPU3  │                  │ CPU4 CPU5 CPU6 CPU7  │
     │  ↓    ↓    ↓    ↓     │                  │  ↓    ↓    ↓    ↓     │
     │ SMT  SMT  SMT  SMT    │                  │ SMT  SMT  SMT  SMT    │
     │ {0,1}{2,3}            │                  │ {4,5}{6,7}            │
     │   ↓                    │                  │   ↓                    │
     │  MC(span={0..3})       │                  │  MC(span={4..7})       │
     │  groups:{0,1}->{2,3}   │                  │  groups:{4,5}->{6,7}   │
     └─────────┬──────────────┘                  └──────────────┬─────────┘
               │                                                 │
               └──────────────► NUMA(span={0..7}) ◄──────────────┘
                                 groups: {0..3} -> {4..7} -> {0..3}

 CPU 0 的 sched_domain 链(for_each_domain(cpu0, sd) 会遍历):

   sd0 = SMT (level 0, span={0,1}, groups:{0}->{1}->{0})
    └─parent─►
       sd1 = MC (level 1, span={0..3}, groups:{0,1}->{2,3}->{0,1})
        └─parent─►
           sd2 = NUMA (level 2, span={0..7}, groups:{0..3}->{4..7}->{0..3})
            └─parent─► NULL

 负载均衡(下一章 load_balance)从 sd0 开始,逐层向上做;
 每层的 imbalance_pct、cache_nice_tries 不同,自动实现"近域勤、远域懒"。
```

---

## 14.8 技巧精解:为什么 `sched_domain` 用 RCU + per-CPU

这一节挑两个最容易被忽略、但对正确性和性能都关键的设计,讲清"为什么 sound"。

### 技巧一:调度域全程在 RCU 保护下读

`for_each_domain` 宏([sched.h:1834](../linux/kernel/sched/sched.h#L1834-L1836))读 `sched_domain`:

```c
/* kernel/sched/sched.h:1834 */
#define for_each_domain(cpu, __sd) \
    for (__sd = rcu_dereference_check_sched_domain(cpu_rq(cpu)->sd); \
            __sd; __sd = __sd->parent)
```

注意那个 `rcu_dereference_check_sched_domain`——读 `cpu_rq(cpu)->sd` 必须在 RCU 读临界区里(`rcu_read_lock()` 持有)。负载均衡路径(`rebalance_domains`、`newidle_balance`)、唤醒选核(`select_task_rq`)开头都先 `rcu_read_lock()`,遍历域链。

> **不这样会怎样**:调度域不是启动后不变——CPU 热插拔、`cpuset` 改 cgroup 的 `cpuset.cpus`、NUMA balancing 触发的拓扑重组,都会调 `partition_sched_domains_locked`([topology.c:2684](../linux/kernel/sched/topology.c#L2684))重建调度域树。重建时,旧的 `sched_domain` 要释放。如果负载均衡正在遍历域链(`sd = sd->parent`),另一个 CPU 同时把旧 `sd` 释放了——你立刻拿到悬空指针,内核崩。

> **所以这样设计**:`sched_domain` 用 RCU——读端在 `rcu_read_lock()` 里可以无锁地拿指针遍历,写端(`build_sched_domains` / `destroy_sched_domains`([topology.c:653](../linux/kernel/sched/topology.c#L653)))释放旧域时走 `call_rcu`/`synchronize_rcu`,等所有读者退出临界区后才真正 `kfree`。这就是 [`sched_domain __rcu *parent`](../linux/include/linux/sched/topology.h#L89) 那个 `__rcu` 注解的含义——编译器/锁检查器强制走 RCU 路径。

> **朴素写法会撞什么墙**:如果给 `sched_domain` 加一把普通自旋锁,负载均衡每次遍历都要拿锁——可负载均衡在软中断(`SCHED_SOFTIRQ`)里跑,每秒可能上百次,锁竞争会随核数恶化。RCU 让**读端零开销**(就是几次普通内存读,只是加了 `smp_read_barrier_depends` 语义),重建时写端付出一点代价,在调度域这种"读极多、写极少"的场景上,RCU 是最优解。Linux 内核里 `task_struct`、`mm_struct`、`files_struct` 等大量"读多写少"结构都这么干。

### 技巧二:`sd_llc` per-CPU 缓存——把高频查询打平到 O(1)

唤醒选核(`select_idle_sibling`)和 idle 均衡会反复问同一个问题:**这个 CPU 的 LLC 域是哪个?里面有多少空闲核?** 如果每次都 `for_each_domain` 从底层往上扫到 `SD_SHARE_LLC`,在几十核的机器上每次唤醒都要扫几层,肉眼可见的慢。

Linux 的解法是 [`sd_llc` / `sd_llc_size` / `sd_llc_id` / `sd_llc_shared` 这组 per-CPU 变量](../linux/kernel/sched/topology.c#L668-L672)([topology.c:668](../linux/kernel/sched/topology.c#L668)),在 `sched_domain_debug`/`build_sched_domains` 时(`update_top_cache_domain` 函数里)预先算好缓存。唤醒选核直接 `this_cpu_ptr(sd_llc)`——单条 `mov` 指令拿到 LLC 域指针。

```c
/* 简化示意(实际在 update_top_cache_domain) */
for_each_online_cpu(cpu) {
    sd = lowest_flag_domain(cpu, SD_SHARE_LLC);
    rcu_assign_pointer(per_cpu(sd_llc, cpu), sd);
    per_cpu(sd_llc_size, cpu) = cpumask_weight(sched_domain_span(sd));
    per_cpu(sd_llc_id, cpu)   = cpumask_first(sched_domain_span(sd));
    rcu_assign_pointer(per_cpu(sd_llc_shared, cpu), sd->shared);
}
```

> **反面对比**:没有这组 per-CPU 缓存,唤醒路径每次都要 `for_each_domain` 找 LLC 域——在 32 核机器上,一个高并发服务器每秒数十万次唤醒,这个开销会显著影响 wakeup 延迟。**per-CPU 缓存把高频查询打平到 O(1)**,代价是构建拓扑时多算一遍,这个取舍在"读极多、写极少"的场景上稳赚。

---

## 章末小结

这一章我们立起了 SMP 负载均衡的**地基**:不是把所有 CPU 当一张扁平列表均衡,而是按硬件拓扑分层。回到二分法,本章服务的是**机制**面——它是负载均衡这个机制层的"地图",告诉下一章的 `load_balance`:"在每一层该怎么均衡、容忍度多少、cache 热任务豁免几次"。没有这张地图,负载均衡要么瞎搬(全局平铺)、要么算不清代价,都会把机器性能拖垮。

### 五个"为什么"清单

1. **为什么不能全局一把尺子均衡?** CPU 间有 cache 共享/NUMA 距离差异,任意迁任务会让 cache 全冷、省下的 CPU 时间填不平 cache 重建的开销。必须按拓扑分层均衡。
2. **`sched_domain` 和 `sched_group` 各代表什么?** `sched_domain` 是"一组 cache 距离相近的 CPU"——每个 CPU 自底向上有一条 SMT→MC→PKG→NUMA 的链;`sched_group` 是域内的均衡单位,代表子域的一颗子树,组级统计负载。
3. **`imbalance_pct` 和 `cache_nice_tries` 是干嘛的?** `imbalance_pct` 是容忍度(本域最忙组 vs 最闲组的负载比超过这个百分比才均衡),`cache_nice_tries` 是给 cache 热任务的豁免次数。两者按层级差异化:SMT 最勤、NUMA 最懒。
4. **`sd_llc`/`sched_domain_shared` 有什么用?** 把"本 CPU 的 LLC 域是哪个、还有几个空闲核"这种高频查询缓存成 per-CPU 变量,让唤醒选核和 idle 均衡 O(1) 拿到 LLC 域。`shared->nr_busy_cpus` 用原子变量跨 CPU 共享。
5. **拓扑树怎么构建?** `default_topology[]` 表 + arch 提供的 `cpu_smt_mask`/`cpu_coregroup_mask` 等函数,启动时 `sched_init_smp` → `build_sched_domains` 一层一层建。`sd_init` 给不同层域打不同参数,RCU 保护读端。

### 想继续深入往哪钻

- 想看拓扑树真实长什么样:`cat /proc/sched_debug`(开 `CONFIG_SCHED_DEBUG`),里面会打印每个 CPU 的 `sched_domain` 链,你能看到 level/name/span/groups。
- 想看每个 CPU 的 LLC 域:看 `/sys/devices/system/cpu/cpuN/cache/index*/`(LLC 是 `level: 3 shared_cpu_list`)。
- 源码阅读顺序:[`include/linux/sched/topology.h`](../linux/include/linux/sched/topology.h)(结构定义)→ [`kernel/sched/topology.c`](../linux/kernel/sched/topology.c) 的 `default_topology`(1688)、`sd_init`(1574)、`build_sched_domains`(2383)、`build_balance_mask`(920)、`update_group_capacity`→ [`kernel/sched/fair.c`](../linux/kernel/sched/fair.c) 的负载均衡(下一章)。
- 工具:`perf sched`、`trace-cmd record -e sched:sched_*`(看迁移事件)、`lscpu -e`(看 cache/NUMA 拓扑)。

### 引出下一章

调度域树是地图,真正干活的是负载均衡主循环。下一章 P4-15,我们钻进 [`kernel/sched/fair.c`](../linux/kernel/sched/fair.c) 的 `load_balance`([fair.c:11259](../linux/kernel/sched/fair.c#L11259))、`newidle_balance`([fair.c:12289](../linux/kernel/sched/fair.c#L12289))、`find_busiest_group`([fair.c:10837](../linux/kernel/sched/fair.c#L10837)),看负载均衡怎么在调度域树上一层一层地拉任务,以及它如何评估"迁这个任务到底值不值"。
