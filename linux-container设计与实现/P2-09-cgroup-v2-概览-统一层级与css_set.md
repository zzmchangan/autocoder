# 第九章 · cgroup v2 概览:统一层级与 css_set

> 篇:P2 cgroup 资源控制
> 主线呼应:第 1 篇讲完了"进程看到什么"(namespace 改视图)。但光改视图还不够——一个被关进 pid ns + net ns 的进程,照样可以 `while(1)` 占满整机 CPU、`malloc` 到 OOM、`fork` 出几万个进程拖垮宿主。要让这个盒子真能"多租",还差一刀:**限它的资源**。这就是 cgroup(control group)要做的。本章是第 2 篇的**地基**:不钻任何具体 controller(cpu/memory/io/pids),先把 cgroup v2 的**骨架**立清楚——为什么 v2 要一棵"统一的层级树",为什么每个任务要挂一个叫 `css_set` 的"指针去重表",为什么每个 controller 在每个 cgroup 里都有一个 `css`,以及那张把所有 controller 串起来的**函数指针表** `struct cgroup_subsys`。看懂这一章,后面 5 章(P2-10 attach_task,P2-11~14 各 controller)才有落脚点。

## 核心问题

**cgroup v2 到底是怎么组织的?为什么是"一棵所有 controller 共用的层级树",而不是 v1 的"每个 controller 一棵树"?一个进程为什么不是直接挂在某个 cgroup 上,而是挂在一个叫 `css_set` 的中间结构上——这个中间结构为什么要做"去重"?每个 controller(cpuset/cpu/memory/...)在 cgroup v2 里到底是什么——为什么用一张函数指针表(`cgroup_subsys`)把 15 个 controller 串起来,新增一个 controller 不用改核心代码?**

读完本章你会明白:

1. cgroup v2 的"单一根"(unified hierarchy):所有 controller 共用同一棵 cgroup 树,`cgrp_dfl_root` 是这棵树的根,`struct cgroup` 是树上的节点,`struct cgroup_root` 是一棵层级(目前 99% 场景只有一棵)。
2. 一个进程通过 `task_struct->cgroups` 指向一个 [`struct css_set`](../linux/include/linux/cgroup-defs.h#L217) —— 这是一组 css 指针(每个 controller 一个),不是直接指向 cgroup。**归属相同的任务共享同一个 `css_set`**(哈希去重),这是 cgroup 性能的关键。
3. 每个 cgroup×controller 有一个 [`struct cgroup_subsys_state`](../linux/include/linux/cgroup-defs.h#L160)(css)——它是 controller 的"实例对象",内嵌在每个 controller 自己更大的结构里(memcg 的 `struct mem_cgroup`、cpu 的 `struct task_group`),通过 `container_of` 反查。
4. `struct cgroup_subsys` 是一张**函数指针表**(css_alloc/css_online/attach/can_fork/fork/exit/...),每个 controller 填一份——cgroup 核心通过 `ss->css_alloc(...)` 这种间接调用驱动 controller,新增 controller 不改核心。
5. boot 时 `cgroup_init_early` → `cgroup_init_subsys` → `cgroup_init` 三步把 15 个 controller 注册到默认层级,造出 root css、把 `init_css_set` 填满、挂进 `css_set_table` 哈希表——从此 `init_task.cgroups` 指向 `init_css_set`,所有后续 fork 出的进程**默认继承这个 css_set**。

> **逃生阀**:如果你被 4 个名字相近的结构(`cgroup`/`css_set`/`cgroup_subsys_state`/`cgroup_subsys`)绕晕,先记住四句话就够了——**`cgroup` 是树的节点(目录),`css_set` 是进程挂的那组指针(去重表),`css` 是"某个 controller 在某个 cgroup 里的实例",`cgroup_subsys` 是 controller 的"类"(函数指针表)**。本章就是把这四个东西讲清。

---

## 9.1 一句话点破

> **cgroup v2 的全部秘密是"一棵树 + 一张去重表 + 一张函数指针表":所有 controller 共用一棵 cgroup 树(统一层级),进程通过 `css_set` 间接挂到树上(去重省内存),每个 controller 在每个 cgroup 里有一个 css 实例,核心代码用函数指针多态调它们——核心不动、controller 可插拔。**

这是结论,不是理由。本章倒过来拆:先看 v1 的多棵树为什么不行(逼出"统一层级"),再看为什么进程不直接挂 cgroup 而要套一层 `css_set`(去重表),然后钻进 css 的"实例对象"设计和 `cgroup_subsys` 函数指针多态,最后看 boot 时这套结构怎么从无到有地建起来(`cgroup_init_early`/`cgroup_init_subsys`/`cgroup_init`)。

---

## 9.2 从 v1 的"多棵树"到 v2 的"一棵树":统一层级的由来

### 9.2.1 v1 的病:每个 controller 一棵树,任务归属矛盾

cgroup 一开始(v1,2.6.24)的设计是:**每个 controller 自己一棵树**。你想限 CPU,就 `mount -t cgroup -o cpu cpu /sys/fs/cgroup/cpu`;想限内存,再 `mount -t cgroup -o memory memory /sys/fs/cgroup/memory`。两棵树**互相独立**,各自有各自的层级。

这听起来挺自然——各管各的嘛。但它撞上了一个致命问题:**同一个任务可以同时出现在两棵树的多个不同 cgroup 里,归属矛盾**。比如:

```
  CPU 树:           Memory 树:
  /                 /
  ├── A            ├── X
  │   └── (进程 P)  │   └── (进程 P)
  └── B            └── Y
                      └── (进程 P)
```

进程 P 在 CPU 树里属于 A,在 Memory 树里**同时**属于 X 和 Y。这听起来似乎可以,但当你想问"P 到底在哪个 cgroup?"——答不上来。一对多。这直接破坏了容器的"一个进程属于一个盒子"的语义:容器里那个进程,它到底是 A 这个盒子还是 X 这个盒子的?CPU 限了 A 的额,Memory 限了 X 的额,**这两个限额没法形成一个"统一的盒子"**。

更要命的是管理上的爆炸:N 个 controller 可以组合出 2^N 种挂载方案,运维要为每个 controller 单独维护一棵树;而**很多 controller 实际上想共享同一组进程**(限 CPU 的同时往往也想限 Memory),v1 强迫你手动对齐两棵树的目录结构。

> **不这样会怎样**:v1 多树结构下,一个进程的归属"分崩离析"——它散落在 N 棵树的若干 cgroup 里,没有一个统一的"盒子"概念。容器化场景(Docker/K8s 想给每个容器发一组限额)根本对不齐:`docker run --cpus=2 --memory=512m` 想要的是"一个盒子同时限 CPU 和 Memory",v1 得在两棵树上分别建同名目录、分别把进程迁进去,稍微不一致盒子就漏了。这套结构没法承载大规模容器编排。

### 9.2.2 v2 的药:所有 controller 共用一棵树(unified hierarchy)

cgroup v2(4.5 起,正式可用;到 6.9 已是默认且唯一推荐方案)的解决方案干脆利落:**所有 controller 共用同一棵 cgroup 树**,叫"默认层级"(default hierarchy,代码里叫 `cgrp_dfl_root`)。一个进程,在这棵树上**只属于一个 cgroup**(准确说是只属于一个 `css_set`,见 9.3),它的 CPU/Memory/IO 限额全部从这同一棵树的同一个节点套下去。

```
  v2 的统一层级(cgrp_dfl_root,唯一一棵):

  /sys/fs/cgroup/         ← 根 cgroup(cgrp_dfl_root.cgrp,L167)
  ├── mycontainer/        ← 一个容器
  │   ├── cpu.max         (cpu controller 文件)
  │   ├── memory.max      (memory controller 文件)
  │   ├── io.max          (io controller 文件)
  │   ├── cgroup.procs     (谁在这个 cgroup)
  │   └── cgroup.subtree_control  (子树启用哪些 controller)
  └── another/
```

关键设计:**一个 cgroup 节点**可以同时承载多个 controller 的配置文件(`cpu.max`/`memory.max`/`io.max`/`pids.max`/...)。一个进程迁进 `mycontainer`,它的 CPU/Memory/IO/PID 限额**全部**来自这一个 cgroup——这就形成了"一个盒子一套限额"的干净语义。

代码里这棵树的根叫 [`cgrp_dfl_root`](../linux/kernel/cgroup/cgroup.c#L167)([cgroup.c:167](../linux/kernel/cgroup/cgroup.c#L167)),它是一个 `struct cgroup_root`:

```c
/* kernel/cgroup/cgroup.c:167(简化) */
struct cgroup_root cgrp_dfl_root = { .cgrp.rstat_cpu = &cgrp_dfl_root_rstat_cpu };
```

([cgroup.c:167](../linux/kernel/cgroup/cgroup.c#L167))

它内嵌了一个根 cgroup(`cgrp_dfl_root.cgrp`),所有其他 cgroup 都是它的后代。一个 cgroup 是否在 v2 默认层级,用 [`cgroup_on_dfl`](../linux/kernel/cgroup/cgroup.c#L319)([cgroup.c:319](../linux/kernel/cgroup/cgroup.c#L319))判断——就一行 `return cgrp->root == &cgrp_dfl_root;`。本书讲 cgroup v2,99% 情况下 `cgroup_on_dfl(cgrp)` 都为 true。

> **钉死这件事**:cgroup v2 的核心架构选择是**统一层级**(unified hierarchy):所有 controller 共用一棵 cgroup 树,根是 `cgrp_dfl_root.cgrp`。一个进程在这棵树上只属于一个 cgroup,它所有的资源限额都从这个节点的 controller 文件读出。这消灭了 v1 的归属矛盾,让"一个容器 = 一组进程 + 一个 cgroup 节点 + 一套限额文件"成立。

### 9.2.3 单一树的代价:no internal process 约束(预告)

统一层级不是没代价。v2 强加了一条 v1 没有的约束:**一个 cgroup 要么有子进程,要么往下启用了 controller,不能两者兼具**(no internal process constraint)。换句话说,`mycontainer/` 如果在 `cgroup.subtree_control` 里启用了 `+cpu`(给它的子 cgroup 提供 cpu controller),那么 `mycontainer/` 自己**不能直接住进程**——进程只能住在它的**子** cgroup 里。

这条约束的动机我们留到 P4-18 详讲(它关系到 controller 的层级统计能不能干净地做)。这里先记一句:**它不是 cgroup v2 的设计缺陷,而是统一层级的必然代价**——既然所有 controller 共用一棵树,就必须避免"父 cgroup 一边直接住进程、一边给子 cgroup 提供 controller"造成的统计混淆。本书后续章节都以 v2 默认层级为准,v1 只在 P4-18 作对照出现。

---

## 9.3 为什么不直接挂 cgroup?中间加一层 `css_set`

理解了"一棵树",下一个问题更关键:**进程怎么挂到这棵树上?**

### 9.3.1 朴素方案:每个 task_struct 直接挂 N 个 css

朴素地想,一个进程在某个 cgroup 节点上,那么 `task_struct` 里就该存"我在哪个 cgroup"。但一个进程**同时受多个 controller 管**(CPU + Memory + IO + PIDs + ...),所以应该是 N 个指针:

```c
/* 朴素的、糟糕的写法(示意,非源码) */
struct task_struct {
    struct cgroup *cpuset_cgrp;
    struct cgroup *cpu_cgrp;
    struct cgroup *memory_cgrp;
    /* ... 15 个 controller 15 个指针 ... */
};
```

这撞上两个墙:

1. **内存爆炸**:宿主上几千个任务,同一容器内 1000 个进程,**它们的 15 个 cgroup 归属完全相同**(都在同一个 mycontainer 的同一组 controller 上)。朴素方案会在每个 task_struct 里重复存 15 个相同的指针——1000 个进程 × 15 个指针 = 15000 次冗余。
2. **fork/exit 慢**:每次 fork 要对 15 个 cgroup 各 inc 一次引用计数、各 list_add 一次,exit 反着来——30 次原子操作 + 30 次链表操作,慢得不可接受。一个大 K8s 节点上每秒几千次 fork/exit,这套开销顶不住。

> **不这样会怎样**:朴素"每 task 存 N 个指针"的方案,在密度高的容器场景(单节点几百 pod,每个 pod 几十~几百进程)会**内存浪费 + fork/exit 锁竞争**双重崩盘。cgroup 必须找一个更聪明的挂法。

### 9.3.2 v2 的解法:中间加一层 `css_set`,哈希去重

Linux 的做法是**在 task 和 cgroup 之间加一层** [`struct css_set`](../linux/include/linux/cgroup-defs.h#L217)([cgroup-defs.h:217](../linux/include/linux/cgroup-defs.h#L217))。`task_struct->cgroups` 只存**一个**指针,指向 `css_set`;`css_set` 内部存那组 `subsys[15]` 数组。**归属完全相同的进程共享同一个 `css_set`**,通过一张全局哈希表 `css_set_table` 去重。

```c
/* include/linux/cgroup-defs.h:217-305(简化) */
struct css_set {
    /* 一组 css 指针,每个 controller 一个 —— 这组指针决定了任务的全套归属 */
    struct cgroup_subsys_state *subsys[CGROUP_SUBSYS_COUNT];   /* L223 */

    refcount_t refcount;          /* 多少任务共享这个 cset */
    struct css_set *dom_cset;     /* 域 cset;普通 cset 指向自己 */
    struct cgroup *dfl_cgrp;      /* 默认层级里这个 cset 归属的 cgroup */
    int nr_tasks;                 /* 共享此 cset 的任务数 */

    /* 串起所有用本 cset 的任务 */
    struct list_head tasks;         /* 稳态任务 */
    struct list_head mg_tasks;      /* 正在迁移中的任务 */
    struct list_head dying_tasks;   /* 正在退出的任务 */

    struct hlist_node hlist;        /* 挂进 css_set_table 哈希桶 */
    struct list_head cgrp_links;    /* 指向本 cset 涉及的所有 cgroup */
    /* ... 还有迁移用的 mg_* 字段,见 P2-10 ... */
    struct rcu_head rcu_head;       /* RCU 异步释放 */
};
```

([cgroup-defs.h:217-305](../linux/include/linux/cgroup-defs.h#L217-L305))

注意 [`cgroup-defs.h`](../linux/include/linux/cgroup-defs.h#L210-L216) 紧挨着结构定义的注释——**内核源码自己就讲了为什么要有这层**:

> A css_set is a structure holding pointers to a set of cgroup_subsys_state objects. This saves space in the task struct object and speeds up fork()/exit(), since a single inc/dec and a list_add()/del() can bump the reference count on the entire cgroup group for a task.
>
> (css_set 是一个保存一组 css 指针的结构。这节省了 task_struct 的空间,也加速了 fork/exit——一次 inc/dec + 一次 list_add/del 就能给整个 cgroup 组改引用计数。)

去重的具体机制是:**新建 cset 时,先在哈希表里找有没有"subsys 数组完全相同"的**,有就复用(refcount++)。这个查找在 [`find_existing_css_set`](../linux/kernel/cgroup/cgroup.c#L1051)([cgroup.c:1051](../linux/kernel/cgroup/cgroup.c#L1051))里:

```c
/* kernel/cgroup/cgroup.c:1051(简化) */
static struct css_set *find_existing_css_set(struct css_set *old_cset,
                struct cgroup *cgrp, struct cgroup_subsys_state **template)
{
    struct cgroup_root *root = cgrp->root;
    struct cgroup_subsys_state *ss;
    struct css_set *cset;
    unsigned long key;
    int i;

    /* 1) 先构造"目标 css 数组" template:
     *    要迁入的这棵树上启用的 controller 取目标 cgrp 的 effective css,
     *    没启用的 controller 沿用 old_cset 的(不变) */
    for_each_subsys(ss, i) {
        if (root->subsys_mask & (1UL << i))
            template[i] = cgroup_e_css_by_mask(cgrp, ss);
        else
            template[i] = old_cset->subsys[i];
    }

    /* 2) 算 template 的哈希值,在 css_set_table 里找 */
    key = css_set_hash(template);
    hash_for_each_possible(css_set_table, cset, hlist, key) {
        if (!compare_css_sets(cset, old_cset, cgrp, template))
            continue;
        return cset;     /* 找到现成的,复用 */
    }
    return NULL;          /* 没现成的,得新建 */
}
```

([cgroup.c:1051-1093](../linux/kernel/cgroup/cgroup.c#L1051-L1093))

`compare_css_sets`([cgroup.c:972](../linux/kernel/cgroup/cgroup.c#L972))做精确比对——先 `memcmp` 一组 subsys 指针(L985),再比对涉及的 cgroup 链表。哈希只是快速缩小候选范围,精确判定靠 `memcmp` 保证不假阳性。

如果 `find_existing_css_set` 返回 NULL(没有现成的可复用),才走 [`find_css_set`](../linux/kernel/cgroup/cgroup.c#L1170)([cgroup.c:1170](../linux/kernel/cgroup/cgroup.c#L1170))的下半段(`kzalloc` 新 cset → `memcpy(template)` → `hash_add` 进哈希表):

```c
/* kernel/cgroup/cgroup.c:1170(简化,核心新建路径) */
static struct css_set *find_css_set(struct css_set *old_cset, struct cgroup *cgrp)
{
    struct cgroup_subsys_state *template[CGROUP_SUBSYS_COUNT] = { };
    struct css_set *cset;
    /* ... */

    lockdep_assert_held(&cgroup_mutex);   /* 写路径,持有主锁 */

    /* 先尝试复用 */
    spin_lock_irq(&css_set_lock);
    cset = find_existing_css_set(old_cset, cgrp, template);
    if (cset)
        get_css_set(cset);                 /* refcount++ */
    spin_unlock_irq(&css_set_lock);
    if (cset)
        return cset;

    /* 没现成的,新建一个 */
    cset = kzalloc(sizeof(*cset), GFP_KERNEL);
    if (!cset)
        return NULL;
    /* ... 初始化各字段 refcount=1、各 list_head ... */

    memcpy(cset->subsys, template, sizeof(cset->subsys));   /* 拷贝那组 css */

    spin_lock_irq(&css_set_lock);
    /* 链接 cset ↔ 它涉及的各 cgroup(link_css_set) */
    /* ... */
    css_set_count++;
    key = css_set_hash(cset->subsys);
    hash_add(css_set_table, &cset->hlist, key);   /* 挂进哈希表 */
    /* ... 给每个 css 也 inc 引用计数 ... */
    spin_unlock_irq(&css_set_lock);

    return cset;
}
```

([cgroup.c:1170-1250](../linux/kernel/cgroup/cgroup.c#L1170-L1250))

> **钉死这件事**:`css_set` 是 cgroup v2 性能的命脉。同归属的任务共享一个 cset(哈希去重),意味着 ① **fork/exit 只需对 cset 改一次 refcount**(+一次 list_add/del),不是 15 次;② **迁移整组进程**(一个容器的 1000 个线程)只需把它们的 `task_struct->cgroups` 指针换成另一个 cset,而不是各改 15 个字段;③ **`css_set` 用 RCU + refcount 保护**,迭代器([`css_task_iter`](../linux/kernel/cgroup/cgroup.c#L4795))能安全遍历,迁移和迭代**不互锁**(详见 9.6)。这种"用一层间接 + 一张哈希表换内存和并发性"的思路,和第 8 本《内存分配器》的 per-CPU cache、《mm》的 per-cpu pageset、《调度器》的 per-CPU rq 是同一脉——**用结构设计消灭并发瓶颈**。

### 9.3.3 全局只有一张 `css_set` 表,数量由 `css_set_count` 记

哈希表本身是一张固定 128 桶的全局表:

```c
/* kernel/cgroup/cgroup.c:909-910 */
#define CSS_SET_HASH_BITS    7
static DEFINE_HASHTABLE(css_set_table, CSS_SET_HASH_BITS);   /* 2^7 = 128 桶 */
```

([cgroup.c:909-910](../linux/kernel/cgroup/cgroup.c#L909-L910))

哈希函数 [`css_set_hash`](../linux/kernel/cgroup/cgroup.c#L912)([cgroup.c:912](../linux/kernel/cgroup/cgroup.c#L912))朴素得惊人——把 15 个 css 指针的值累加,再高低位异或一下:

```c
/* kernel/cgroup/cgroup.c:912 */
static unsigned long css_set_hash(struct cgroup_subsys_state **css)
{
    unsigned long key = 0UL;
    struct cgroup_subsys *ss;
    int i;

    for_each_subsys(ss, i)
        key += (unsigned long)css[i];   /* 累加指针值 */
    key = (key >> 16) ^ key;            /* 折叠 */

    return key;
}
```

([cgroup.c:912-923](../linux/kernel/cgroup/cgroup.c#L912-L923))

为什么这么朴素可以?因为 css 指针是内核地址,本身分布已经相当分散(每个 css 来自不同 `kmem_cache_alloc`,地址随机化);把 15 个这样的值相加再折叠,分布已经够均匀。哈希表是为了"快速把候选范围从全机所有 cset 缩小到一个桶里的少数几个",精确判定还是靠 `compare_css_sets` 的 `memcmp`——哈希冲突不影响正确性,只影响性能。

整张表里有多少个 cset,由全局计数器 [`css_set_count`](../linux/kernel/cgroup/cgroup.c#L750)([cgroup.c:750](../linux/kernel/cgroup/cgroup.c#L750))记录,初始值是 1(为 `init_css_set` 预留):

```c
/* kernel/cgroup/cgroup.c:750 */
static int css_set_count = 1;   /* 1 for init_css_set */
```

([cgroup.c:750](../linux/kernel/cgroup/cgroup.c#L750))

`find_css_set` 新建一个 cset 时 `css_set_count++`([cgroup.c:1233](../linux/kernel/cgroup/cgroup.c#L1233));[`put_css_set_locked`](../linux/kernel/cgroup/cgroup.c#L925)([cgroup.c:925](../linux/kernel/cgroup/cgroup.c#L925))释放时 `css_set_count--`([cgroup.c:944](../linux/kernel/cgroup/cgroup.c#L944))。这个计数器有个妙用——迁移整组进程前,核心会用它**预估需要预分配多少个 `cgrp_cset_link` 结构**(见 [`allocate_cgrp_cset_links(2 * css_set_count, ...)`](../linux/kernel/cgroup/cgroup.c#L2085),[cgroup.c:2085](../linux/kernel/cgroup/cgroup.c#L2085)),一次性分配,避免迁移中途分配失败。

---

## 9.4 `struct cgroup`:树的节点长什么样

讲完了进程挂的那一层(`css_set`),现在看树本身。每个 `/sys/fs/cgroup/` 下的目录,内核里就是一个 [`struct cgroup`](../linux/include/linux/cgroup-defs.h#L397)([cgroup-defs.h:397](../linux/include/linux/cgroup-defs.h#L397))。

```c
/* include/linux/cgroup-defs.h:397-549(简化) */
struct cgroup {
    /* 自带的 css,ss 字段为 NULL,指回这个 cgroup 自己。
     * 这是 cgroup 核心"伪装成 css"的技巧,见 9.5.3 */
    struct cgroup_subsys_state self;          /* L399 */

    unsigned long flags;                       /* L401 */
    int level;                                 /* 在树里的深度,根是 0 */
    int max_depth;                             /* 允许的最大深度 */

    /* 子树规模统计 */
    int nr_descendants;
    int nr_dying_descendants;
    int max_descendants;

    /* 有多少 css_set(任务组)挂在本 cgroup 上 */
    int nr_populated_csets;
    int nr_populated_domain_children;
    int nr_populated_threaded_children;

    /* kernfs 节点 —— 这个 cgroup 在 /sys/fs/cgroup/ 里的目录 */
    struct kernfs_node *kn;                    /* L446 */
    struct cgroup_file procs_file;             /* "cgroup.procs" 文件 */
    struct cgroup_file events_file;            /* "cgroup.events" 文件 */

    /* 子树启用了哪些 controller(u16 位图) */
    u16 subtree_control;                       /* 用户配置的 */
    u16 subtree_ss_mask;                       /* effective(继承下来的) */

    /* 每个 controller 在本 cgroup 的 css(cgroup 核心 rcu 保护) */
    struct cgroup_subsys_state __rcu *subsys[CGROUP_SUBSYS_COUNT];   /* L466 */

    struct cgroup_root *root;                  /* L468,所属的根(几乎总是 cgrp_dfl_root) */

    /* 所有挂在本 cgroup 的 css_set(cgrp_cset_link 串起来) */
    struct list_head cset_links;               /* L474 */

    /* 每个 controller 的"effective css_sets"链表头数组 */
    struct list_head e_csets[CGROUP_SUBSYS_COUNT];   /* L483 */

    /* 域 cgroup;threaded 模式下指向最近的域祖先 */
    struct cgroup *dom_cgrp;                   /* L492 */

    /* rstat(per-cpu 递归资源统计,cpu.time/memory.current 等读它) */
    struct cgroup_rstat_cpu __percpu *rstat_cpu;
    struct list_head rstat_css_list;

    /* PSI(CPU/Memory/IO/IRQ pressure) */
    struct psi_group *psi;

    /* freezer 状态(P2-14) */
    struct cgroup_freezer_state freezer;

    /* 全部祖先(含自己),ancestors[0] = 根
     * 用这个数组可以 O(1) 判断"A 是不是 B 的祖先" */
    struct cgroup *ancestors[];                /* L548 */
};
```

([cgroup-defs.h:397-549](../linux/include/linux/cgroup-defs.h#L397-L549))

几个关键字段的"为什么":

**① `self` 字段(L399)**——这是 cgroup 核心"伪装成 css"的技巧。每个 cgroup 都有一个内嵌的 css(`struct cgroup_subsys_state self`),但它的 `ss` 字段填 NULL(表示"我不属于任何 controller,我就是 cgroup 本身")。为什么要这么做?因为 cgroup 的很多代码路径(迭代、引用计数、rstat 链表)是按 css 设计的——给 cgroup 一个"假 css"让它能复用同一套基础设施。这是 C 里常见的"基类嵌入"模式,我们下面 9.5 会再讲。

**② `subsys[CGROUP_SUBSYS_COUNT]` 数组(L466)**——和 `css_set.subsys[]` 对偶:`css_set.subsys[]` 是**任务视角**的"我归属哪 15 个 css",`cgroup.subsys[]` 是**cgroup 视角**的"我自己拥有哪 15 个 css"。一个 cgroup 启用某 controller 时(`cgroup.subtree_control += +memory`),这个 controller 的 css 就会被创建并放进 `cgroup->subsys[memory_cgrp_id]`。注意这是 `__rcu`,读路径走 RCU。

**③ `e_csets[CGROUP_SUBSYS_COUNT]` 数组(L483)**——这是 9.3 里提到的"effective csets"链表头。v2 里某 cgroup 没启用某 controller 时,**任务的 css 指针会向上指向最近的启用了该 controller 的祖先**。`e_csets[ssid]` 串起所有"虽然不在我这个 cgroup、但 effective css 是我"的 cset——这让"给定一个 cgroup,遍历所有受它管的任务"能 O(1) 入口。这是 v2 单一树 + 部分启用 controller 的精髓,我们在 P2-10 attach_task 里会用到。

**④ `ancestors[]` 数组(L548)**——这是 cgroup 层级判断的 O(1) 加速器。"进程 A 的 cgroup 是不是 B 的后代"这种判断在 cgroup 里高频(限额继承、权限检查)。朴素地要沿 `parent` 指针爬树,O(depth)。Linux 的做法:cgroup 创建时把从根到自己的整条祖先路径填进 `ancestors[]` 数组,判断"A 的祖先链里第 level 层是不是 B"变成 `A->ancestors[B->level] == B`,O(1)。

**⑤ `subtree_control` vs `subtree_ss_mask`(L460-461)**——前者是用户**显式配置**的(`echo "+memory" > cgroup.subtree_control`),后者是**有效启用**的(显式 + 父亲继承下来的)。两个位图分开,是因为 v2 的 controller 启用是"向下传递"的——你启用了 memory,你的所有后代都自动启用;但用户写的 `subtree_control` 只记自己这一层显式启用了哪些。

> **所以这样设计**:cgroup 的字段组织反映了两件事——① **它既是树的节点**(`level`/`max_depth`/`nr_descendants`/`ancestors[]`),**又是 controller 的容器**(`subsys[]`/`e_csets[]`/`subtree_control`);② **它的 hot path 字段被精心布局**(读多写少的 `kn`/`procs_file` 在前,统计的 `rstat_cpu` 单独加 `CACHELINE_PADDING` 隔到另一个 cacheline,L504)。这种布局是 cgroup 在大规模容器场景(几千 cgroup、几万任务)能扛住的根本。

---

## 9.5 三种"css"和那张函数指针表 `cgroup_subsys`

### 9.5.1 `struct cgroup_subsys_state`:controller 的"实例对象"

讲完了 cgroup(目录)和 css_set(进程挂的那层指针),第三个、也是**最具体**的结构是 [`struct cgroup_subsys_state`](../linux/include/linux/cgroup-defs.h#L160)([cgroup-defs.h:160](../linux/include/linux/cgroup-defs.h#L160)),简称 **css**。一个 css = "某个 controller 在某个 cgroup 里的实例"。

```c
/* include/linux/cgroup-defs.h:160-208(简化) */
struct cgroup_subsys_state {
    /* PI(public immutable)字段,可无锁直接读 */
    struct cgroup *cgroup;                /* 我属于哪个 cgroup */
    struct cgroup_subsys *ss;             /* 我属于哪个 controller(类) */
    struct percpu_ref refcnt;             /* per-cpu 引用计数 */
    struct cgroup_subsys_state *parent;   /* 父 css(层级) */

    /* 兄弟/孩子链表(挂在 parent->children / parent->sibling) */
    struct list_head sibling;
    struct list_head children;

    int id;                               /* subsystem-unique id(root=1) */
    unsigned int flags;                   /* CSS_ONLINE/CSS_DYING/... */
    u64 serial_nr;                        /* 全局单调递增序号 */
    atomic_t online_cnt;                  /* 在线计数 */

    /* 异步释放(percpu_ref kill → RCU → work) */
    struct work_struct destroy_work;
    struct rcu_work destroy_rwork;
};
```

([cgroup-defs.h:160-208](../linux/include/linux/cgroup-defs.h#L160-L208))

注意 [cgroup-defs.h:155-159](../linux/include/linux/cgroup-defs.h#L155-L159) 紧挨的注释:**"the fundamental structural building block that controllers deal with"**(controller 们打交道的基础结构块)。这就是 css 的定位——**controller 视角的"原子对象"**。它带着三样东西:① 我属于哪个 cgroup(`cgroup` 字段);② 我属于哪个 controller(`ss` 字段);③ 我的生命周期(refcount/online_cnt/destroy_*)。

为什么 css 要单独抽出来,而不是直接把 `mem_cgroup`/`task_group` 这些大结构塞进 `cgroup->subsys[]`?因为**cgroup 核心不需要知道 controller 的具体类型**——核心只需要"引用计数、上线/下线、挂在 cgroup 上、在 rstat 链表里"这套通用操作。把这套通用字段抽成 css,controller 各自的大结构**内嵌一个 css 当"头部"**,核心拿到的永远是指向 css 的指针,要用 controller 自己的数据时再 `container_of(css, struct mem_cgroup, css)` 反查。

```c
/* 真实例子(简化,展示 css 内嵌关系) */
struct mem_cgroup {                  /* mm/memcontrol.c */
    struct cgroup_subsys_state css;  /* ← 头部,cgroup 核心认这个 */
    /* ... memcg 自己的几十个字段 ... */
};

struct task_group {                  /* kernel/sched/sched.h */
    struct cgroup_subsys_state css;  /* ← cpu controller 的头部 */
    /* ... 调度组数据(se/cfs_bandwidth 等)... */
};
```

这是 C 里实现"面向对象多态"的经典手法:**基类嵌入派生类的开头**,通过 `container_of` 做 down-cast。cgroup 核心永远操作 `struct cgroup_subsys_state *`,只有具体 controller 的代码才 down-cast 到 `mem_cgroup`/`task_group`。

### 9.5.2 `cgroup_subsys`:controller 的"类"(函数指针表)

那"controller 自己是谁"?它是一张**函数指针表**——[`struct cgroup_subsys`](../linux/include/linux/cgroup-defs.h#L688)([cgroup-defs.h:688](../linux/include/linux/cgroup-defs.h#L688)):

```c
/* include/linux/cgroup-defs.h:688-774(简化,保留全部回调签名) */
struct cgroup_subsys {
    /* 生命周期:css 的创建/上线/下线/释放/重置 */
    struct cgroup_subsys_state *(*css_alloc)(struct cgroup_subsys_state *parent_css);
    int   (*css_online)(struct cgroup_subsys_state *css);
    void  (*css_offline)(struct cgroup_subsys_state *css);
    void  (*css_released)(struct cgroup_subsys_state *css);
    void  (*css_free)(struct cgroup_subsys_state *css);
    void  (*css_reset)(struct cgroup_subsys_state *css);
    void  (*css_rstat_flush)(struct cgroup_subsys_state *css, int cpu);
    int   (*css_extra_stat_show)(struct seq_file *seq, struct cgroup_subsys_state *css);
    int   (*css_local_stat_show)(struct seq_file *seq, struct cgroup_subsys_state *css);

    /* 迁移回调:进程要迁进/迁出本 controller 管的 cgroup */
    int   (*can_attach)(struct cgroup_taskset *tset);
    void  (*cancel_attach)(struct cgroup_taskset *tset);
    void  (*attach)(struct cgroup_taskset *tset);
    void  (*post_attach)(void);

    /* 进程生命周期回调:fork 时预检、fork 后通知、exit、release */
    int   (*can_fork)(struct task_struct *task, struct css_set *cset);
    void  (*cancel_fork)(struct task_struct *task, struct css_set *cset);
    void  (*fork)(struct task_struct *task);
    void  (*exit)(struct task_struct *task);
    void  (*release)(struct task_struct *task);
    void  (*bind)(struct cgroup_subsys_state *root_css);

    /* 元信息 */
    bool early_init:1;
    bool implicit_on_dfl:1;
    bool threaded:1;
    int  id;                         /* 自动分配 */
    const char *name;                /* 自动填,如 "memory" */
    const char *legacy_name;
    struct cgroup_root *root;
    struct idr css_idr;
    struct list_head cfts;
    struct cftype *dfl_cftypes;      /* v2 的控制文件(cpu.max/memory.max 等) */
    struct cftype *legacy_cftypes;   /* v1 的控制文件 */
    unsigned int depends_on;
};
```

([cgroup-defs.h:688-774](../linux/include/linux/cgroup-defs.h#L688-L774))

**这就是 cgroup v2 的核心架构**:`cgroup_subsys` 是一张函数指针表,定义了 controller 的**全部契约**——生命周期(css_alloc→css_online→...→css_free)、进程迁移(can_attach→attach→post_attach)、进程生命周期(can_fork→fork→exit→release)、统计(rstat_flush/stat_show)、控制文件(dfl_cftypes)。

每个 controller(memcg/cpu/io/...)各填一份。比如 memory controller 的实例大致是(简化):

```c
/* mm/memcontrol.c(简化,展示函数指针表填充) */
struct cgroup_subsys memory_cgrp_subsys = {
    .css_alloc = mem_cgroup_css_alloc,    /* 创建一个 mem_cgroup */
    .css_online = mem_cgroup_css_online,  /* 上线 */
    .css_offline = mem_cgroup_css_offline,
    .css_free = mem_cgroup_css_free,
    .can_attach = NULL,                    /* memcg 不关心 attach 时机 */
    .can_fork  = mem_cgroup_can_attach,   /* fork 时预 charge */
    .fork      = mem_cgroup_fork,
    .exit      = mem_cgroup_exit,
    .dfl_cftypes = memory_files,          /* memory.current/memory.max/... */
    .threaded  = true,
    /* ... */
};
```

cgroup 核心代码通过 `ss->css_alloc(...)`、`ss->fork(task)` 这种**间接调用**驱动所有 controller,完全不关心具体是哪个:

```c
/* kernel/cgroup/cgroup.c(简化,核心通过函数指针调 controller) */
css = ss->css_alloc(parent_css);     /* 让 controller 自己造 css */
/* ... 核心只管通用的 init_and_link_css、online_css ... */

/* fork 时:遍历所有有 fork 回调的 controller,各自调 */
for_each_subsys(ss, ssid) {
    if (have_fork_callback & (1UL << ssid))
        ss->fork(task);   /* 比如 memcg 的 fork → 给新进程 charge 初始内存 */
}
```

> **钉死这件事(函数指针多态)**:`cgroup_subsys` 是一张函数指针表,**定义了 controller 的契约**;每个 controller 填一份(`memory_cgrp_subsys`/`cpu_cgrp_subsys`/...);核心代码通过 `ss->xxx()` 间接调用。**新增一个 controller,核心代码一行都不用改**——只要写一个新 `struct cgroup_subsys` 实例、加进 `cgroup_subsys.h` 的 `SUBSYS()` 列表、注册它的 cftype 数组就行。这是 Linux 内核里"面向对象多态"的教科书级案例(C 没有 class,但用结构体内嵌 + 函数指针表实现等价语义),和 VFS 的 `struct file_operations`、调度器的 `struct sched_class`、netns 的 `pernet_ops` 是**同一套手法**。

### 9.5.3 `cgroup` 的"假 css":为什么 `self` 的 `ss` 是 NULL

回头看 9.4 里 cgroup 的 `self` 字段(L399)。它是 `struct cgroup_subsys_state` 类型,但 `self.ss = NULL`。为什么?

因为 cgroup 核心也想用 css 那套基础设施(refcount、rstat 链表、online/offline 生命周期)管理**目录本身**——但目录不属于任何 controller。给它一个 `ss = NULL` 的 css,既能让它复用 css 那套代码,又能通过 `ss == NULL` 区分"这是 cgroup 自己的 css,不是 controller 的"。这是一个"无类型哨兵"技巧:用 NULL 表示"我是 cgroup 本身",巧妙避开给 cgroup 单独再写一套引用计数/生命周期管理。

### 9.5.4 15 个 controller:一张表认全

[`include/linux/cgroup_subsys.h`](../linux/include/linux/cgroup_subsys.h)(整个文件)用 `SUBSYS(name)` 宏列出所有 controller:

```c
/* include/linux/cgroup_subsys.h:12-66(简化,挑主要 controller) */
#if IS_ENABLED(CONFIG_CPUSETS)
SUBSYS(cpuset)               /* 绑核/绑内存节点 */
#endif
#if IS_ENABLED(CONFIG_CGROUP_SCHED)
SUBSYS(cpu)                  /* cpu.max/weight → throttle */
#endif
#if IS_ENABLED(CONFIG_CGROUP_CPUACCT)
SUBSYS(cpuacct)              /* cpu 用量统计(v1 遗留,v2 已并入 cpu) */
#endif
#if IS_ENABLED(CONFIG_BLK_CGROUP)
SUBSYS(io)                   /* io.max → iocost/iolatency */
#endif
#if IS_ENABLED(CONFIG_MEMCG)
SUBSYS(memory)               /* memcg → charge/OOM */
#endif
#if IS_ENABLED(CONFIG_CGROUP_DEVICE)
SUBSYS(devices)              /* 设备访问黑白名单 */
#endif
#if IS_ENABLED(CONFIG_CGROUP_FREEZER)
SUBSYS(freezer)              /* 整组冻结 */
#endif
#if IS_ENABLED(CONFIG_CGROUP_NET_CLASSID)
SUBSYS(net_cls)              /* 网络分类标记(v1 用,v2 弱化) */
#endif
#if IS_ENABLED(CONFIG_CGROUP_PERF)
SUBSYS(perf_event)           /* per-cgroup perf event */
#endif
#if IS_ENABLED(CONFIG_CGROUP_NET_PRIO)
SUBSYS(net_prio)             /* 网络优先级(v1) */
#endif
#if IS_ENABLED(CONFIG_CGROUP_HUGETLB)
SUBSYS(hugetlb)              /* 大页限额 */
#endif
#if IS_ENABLED(CONFIG_CGROUP_PIDS)
SUBSYS(pids)                 /* 进程数上限 */
#endif
#if IS_ENABLED(CONFIG_CGROUP_RDMA)
SUBSYS(rdma)                 /* RDMA 资源 */
#endif
#if IS_ENABLED(CONFIG_CGROUP_MISC)
SUBSYS(misc)                 /* 通用杂项 */
#endif
/* debug 仅 v1 用,v2 不支持 */
#if IS_ENABLED(CONFIG_CGROUP_DEBUG)
SUBSYS(debug)
#endif
```

([cgroup_subsys.h:12-73](../linux/include/linux/cgroup_subsys.h#L12-L73))

这个文件很巧妙——它被**两种方式 include**:

1. 在 [`cgroup-defs.h`](../linux/include/linux/cgroup-defs.h#L42-L47) 里以 `#define SUBSYS(_x) _x ## _cgrp_id,` 然后 `#include` 进来,展开成 `enum cgroup_subsys_id { cpuset_cgrp_id, cpu_cgrp_id, ..., CGROUP_SUBSYS_COUNT, };`——这就是为什么 `CGROUP_SUBSYS_COUNT` 是 15(`include/linux/cgroup-defs.h#L45`)。
2. 在 `kernel/cgroup/cgroup.c` 里以 `#define SUBSYS(_x) &_x ## _cgrp_subsys,` 然后 `#include` 进来,展开成一个 `struct cgroup_subsys *cgroup_subsys[] = { &cpuset_cgrp_subsys, &cpu_cgrp_subsys, ..., };` 数组——这就是 [`for_each_subsys`](../linux/kernel/cgroup/cgroup.c) 宏遍历的来源。

所以**加一个 controller = 写一个 `struct cgroup_subsys xxx_cgrp_subsys` + 在 cgroup_subsys.h 里加一行 `SUBSYS(xxx)`**,枚举 id、数组索引、`for_each_subsys` 自动就通了。这种"用一份宏列表同时生成 enum 和数组"的技巧,内核里反复出现(sched_class 的 `for_each_class`、syscall 表的 `SYSCALL_DEFINE`)。

本书后续 5 章重点拆其中 5 个(对应 K8s/Docker 容器最常用):**cpu(P2-11)**、**memory/memcg(P2-12)**、**io(P2-13)**、**pids/freezer/cpuset(P2-14)**。`devices`/`hugetlb`/`rdma`/`misc` 用得少;`cpuacct`/`net_cls`/`net_prio`/`debug` 是 v1 遗留,v2 已弱化或并入其他 controller。

---

## 9.6 并发保护:两把锁 + RCU + percpu_ref

cgroup 是个**高并发**子系统:一边有几千个进程在 fork/exit(改自己的 css_set 引用),一边用户在 `echo $PID > cgroup.procs`(迁移进程),一边 systemd/docker 在 mkdir/rmdir cgroup(改树结构),一边 `cat cpu.stat` 在读统计,一边 OOM killer 在遍历 cgroup 找进程。要保证这些不撞,靠的是**三档并发控制**:

### 9.6.1 两把锁:`cgroup_mutex` 和 `css_set_lock`

[`cgroup.c:90-91`](../linux/kernel/cgroup/cgroup.c#L90-L91) 定义了两把核心锁:

```c
/* kernel/cgroup/cgroup.c:80-96(简化,保留注释) */
/*
 * cgroup_mutex is the master lock. Any modification to cgroup or its
 * hierarchy must be performed while holding it.
 *
 * css_set_lock protects task->cgroups pointer, the list of css_set
 * objects, and the chain of tasks off each css_set.
 */
DEFINE_MUTEX(cgroup_mutex);        /* L90 —— 主锁,改 cgroup/树结构必持 */
DEFINE_SPINLOCK(css_set_lock);     /* L91 —— 保护 task->cgroups、css_set 列表 */
```

([cgroup.c:80-96](../linux/kernel/cgroup/cgroup.c#L80-L96))

**层次很清楚**:

- **`cgroup_mutex`**(mutex,可睡):"粗粒度主锁"。任何会**改变树结构或 css 生命周期**的操作(mkdir/rmdir cgroup、启用/禁用 controller、css alloc/online/offline/free、迁移整组进程的 prepare 阶段)都要持有它。它是 mutex 不是 spinlock,因为持有期间可能分配内存、调 controller 回调(`ss->css_alloc` 可能睡眠)。
- **`css_set_lock`**(spinlock,不睡):"细粒度 css_set 锁"。只保护 css_set 这一层:`task->cgroups` 指针、css_set 的 `tasks`/`mg_tasks`/`dying_tasks` 链表、`css_set_table` 哈希表、`css_set_count`。它是 spinlock 且关中断(`spin_lock_irq`),因为 fork/exit 这种 hot path 不能睡。

**两把锁配合的模式是经典的"粗 + 细"**:写路径(迁移、创建)先拿 `cgroup_mutex` 把树锁住(防止别人改结构),再在涉及 css_set 的细粒度操作时短拿 `css_set_lock`(快速改完就放);fork/exit 这种不需要改结构的 hot path,只拿 `css_set_lock` 就够,不需要 `cgroup_mutex`。这样**fork/exit 不会被 mkdir/rmdir 阻塞**(它们争的是不同的锁),cgroup 在大规模 fork 场景(一个 K8s 节点每秒几千次 fork)才扛得住。

### 9.6.2 RCU 保护 `task->cgroups` 读路径

`task_struct->cgroups` 是 `__rcu` 注解的(P0-01 的 1.4 节见过,[`include/linux/sched.h`](../linux/include/linux/sched.h#L1234)),意思是:**读这个指针用 RCU,不要拿锁**。这样任何路径需要"知道这个任务在哪个 css_set"时(`task_css_set(task)`、`task_subsys_state(task, ssid)` 等),都能 RCU 无锁读,不会拖慢 hot path。

迁移时,核心用 [`css_set_move_task`](../linux/kernel/cgroup/cgroup.c#L870)([cgroup.c:870](../linux/kernel/cgroup/cgroup.c#L870))把任务从 `from_cset.tasks` 摘下、加进 `to_cset.tasks`(`list_del_init` + `list_add_tail`),同时改 `task->cgroups`——这套操作在 `css_set_lock` 下原子完成,外面读者用 RCU 永远看到一致状态(要么旧的 cset,要么新的 cset,不会"半新半旧")。

### 9.6.3 `percpu_ref`:css 的引用计数(无锁 hot path)

css 的 `refcnt` 不是普通 `atomic_t`,而是 **`struct percpu_ref`**([cgroup-defs.h:168](../linux/include/linux/cgroup-defs.h#L168))。这意味着每个 CPU 各有一个本地计数器,inc/dec 在本地 CPU 上**无锁原子操作**(只关本 CPU 中断),只在计数器要 kill(整个 css 要下线)时才把所有 CPU 的本地计数聚合回一个全局值。

为什么 css 用 percpu_ref 而不是普通 atomic?因为 css 的 inc/dec 是 cgroup 的**最热路径**——每个 page 分配要 `css_get(memcg->css)`(memcg charge)、每次 fork 要 `css_get` 一组 css。普通 atomic 在多核上会撞 cache line(每次 inc/dec 都 invalidate 别人的 cache),几千核的大机上会成为瓶颈。percpu_ref 让每个 CPU 改自己的计数器,**cache line 不互撞**,hot path 零争用;代价是 css 下线时要聚合(慢路径,能容忍)。

### 9.6.4 迭代器和迁移不互锁:迭代器 pin cset

还有一个并发性细节。cgroup 经常需要"遍历某个 cgroup 里的所有任务"(比如 freezer 要冻整组、OOM 要遍历可杀进程)。这通过 [`css_task_iter`](../linux/kernel/cgroup/cgroup.c#L4795) 迭代器完成。问题是:**迭代过程中,别人可能在迁移任务**(把任务从当前 cset 挪到另一个 cset)。

Linux 的解法是**迭代器 pin 住当前正在遍历的 cset**——具体在 [`css_task_iter_advance_css_set`](../linux/kernel/cgroup/cgroup.c#L4795)([cgroup.c:4795](../linux/kernel/cgroup/cgroup.c#L4795)):每走到一个 cset,就 `get_css_set(cset)`(cset 的 refcount+1),并把迭代器自己挂到 `cset->task_iters` 链表上。这样 [`css_set_move_task`](../linux/kernel/cgroup/cgroup.c#L870) 在搬任务时,**会检查这个 cset 上有没有迭代器**(`css_set_skip_task_iters`);如果有,且搬的恰好是迭代器正指向的下一个任务,就**先把迭代器推进到下一个任务再搬**。这样:

- 迁移不会让迭代器读到半坏的数据(任务要么还在旧 cset,要么已在新 cset);
- 迭代器和迁移**不互锁**(迭代器只 pin cset,不阻塞迁移);
- 迭代器走完后 `put_css_set`(cset 的 refcount-1),cset 才可能被释放。

> **钉死这件事(为什么 sound)**:cgroup 的并发设计是分层的——**结构修改用 `cgroup_mutex`,css_set 修改用 `css_set_lock`,读 task->cgroups 用 RCU,css 引用计数用 percpu_ref,迭代器和迁移用"pin cset + skip iterator"协调**。这套设计保证:① fork/exit hot path 不被 mkdir/rmdir 阻塞;② 迁移和迭代并发进行,不会读到半新半旧状态,不会丢任务,不会死锁;③ 几千核大机上 css 引用计数不撞 cache line。读者读到任何一段并发代码,都要问自己:**它持的是哪把锁?在保护什么?为什么不撞?**——这套问法贯穿后续 5 章。

---

## 9.7 boot 时从无到有:`cgroup_init_early` → `cgroup_init_subsys` → `cgroup_init`

讲完了静态结构,最后一个问题:**这套东西 boot 时怎么建起来?** 三个阶段。

### 9.7.1 `cgroup_init_early`:最早期的初始化

[`cgroup_init_early`](../linux/kernel/cgroup/cgroup.c#L6037)([cgroup.c:6037](../linux/kernel/cgroup/cgroup.c#L6037))在内核 boot 很早的阶段(start_kernel 早期,在 scheduler 起来之前)被调。它做四件事:

```c
/* kernel/cgroup/cgroup.c:6037-6066(简化) */
int __init cgroup_init_early(void)
{
    struct cgroup_fs_context ctx = { .root = &cgrp_dfl_root };
    struct cgroup_subsys *ss;
    int i;

    init_cgroup_root(&ctx);                           /* 初始化默认根 */
    cgrp_dfl_root.cgrp.self.flags |= CSS_NO_REF;     /* 根 css 不计引用 */

    RCU_INIT_POINTER(init_task.cgroups, &init_css_set); /* init_task 挂上 init_css_set */

    for_each_subsys(ss, i) {
        /* 校验每个 ss 的 css_alloc/css_free/name/id 字段合法 */
        ss->id = i;
        ss->name = cgroup_subsys_name[i];
        if (!ss->legacy_name)
            ss->legacy_name = cgroup_subsys_name[i];

        if (ss->early_init)                           /* 需要 early init 的(如 cpuset) */
            cgroup_init_subsys(ss, true);             /* 立刻初始化 */
    }
    return 0;
}
```

([cgroup.c:6037-6066](../linux/kernel/cgroup/cgroup.c#L6037-L6066))

两个关键动作:① **把 `init_task.cgroups` 指向 `init_css_set`**——从此所有从 `init_task` fork 出的进程(PID 1 kthreadd,以及所有后续用户进程)**默认继承 `init_css_set`**,这是整个系统的初始归属;② **注册需要 early_init 的 controller**(典型的有 cpuset,因为 SMP 初始化早期就需要它管 CPU)。

### 9.7.2 `cgroup_init_subsys`:每个 controller 的注册

核心循环在 [`cgroup_init_subsys`](../linux/kernel/cgroup/cgroup.c#L5978)([cgroup.c:5978](../linux/kernel/cgroup/cgroup.c#L5978))——给一个 controller 完成注册:

```c
/* kernel/cgroup/cgroup.c:5978-6029(简化) */
static void __init cgroup_init_subsys(struct cgroup_subsys *ss, bool early)
{
    struct cgroup_subsys_state *css;

    cgroup_lock();
    idr_init(&ss->css_idr);
    INIT_LIST_HEAD(&ss->cfts);

    ss->root = &cgrp_dfl_root;
    css = ss->css_alloc(NULL);            /* ← 函数指针调用!让 controller 自己造 root css */
    BUG_ON(IS_ERR(css));
    init_and_link_css(css, ss, &cgrp_dfl_root.cgrp);  /* 关联到根 cgroup */
    css->flags |= CSS_NO_REF;             /* root css 永不释放,关掉引用计数 */

    if (early)
        css->id = 1;
    else
        css->id = cgroup_idr_alloc(&ss->css_idr, css, 1, 2, GFP_KERNEL);

    /* 关键:把这个 root css 填进 init_css_set 的 subsys[id] 槽 */
    init_css_set.subsys[ss->id] = css;    /* L6014 */

    /* 标记哪些 controller 实现了 fork/exit/release/can_fork 回调,
     * 后续 fork/exit hot path 用位图快速判断要不要调 */
    have_fork_callback    |= (bool)ss->fork    << ss->id;
    have_exit_callback    |= (bool)ss->exit    << ss->id;
    have_release_callback |= (bool)ss->release << ss->id;
    have_canfork_callback |= (bool)ss->can_fork << ss->id;

    BUG_ON(online_css(css));               /* css 上线,触发 css_online 回调 */

    cgroup_unlock();
}
```

([cgroup.c:5978-6029](../linux/kernel/cgroup/cgroup.c#L5978-L6029))

四个关键点:① **`ss->css_alloc(NULL)`**——这就是 9.5.2 说的函数指针多态,核心让 controller 自己造它的 root 实例(memcg 造 `struct mem_cgroup`,cpu 造 `struct task_group`);② **`init_css_set.subsys[ss->id] = css`**(L6014)——这一行是命脉,它把每个 controller 的 root css 塞进系统的初始 css_set,从此所有继承 `init_css_set` 的进程都在 root cgroup;③ **`have_fork_callback` 等位图**——这又是性能优化,boot 时一次性标记哪些 controller 实现了 fork/exit 回调,后续 fork/exit hot path 不用遍历 15 个 controller 问"你实现了 fork 吗",直接位图一查;④ **`online_css`** 触发 controller 的 `css_online` 回调,让 controller 自己做后初始化(比如 memcg 这里会启动 OOM killer worker)。

### 9.7.3 `cgroup_init`:最后的注册 + cftype

[`cgroup_init`](../linux/kernel/cgroup/cgroup.c#L6074)([cgroup.c:6074](../linux/kernel/cgroup/cgroup.c#L6074))在 init 主流程较晚(在 init_main_thread 之前)被调,完成剩下的注册:

```c
/* kernel/cgroup/cgroup.c:6074-6170(简化,核心循环) */
int __init cgroup_init(void)
{
    struct cgroup_subsys *ss;
    int ssid;

    /* 注册 cgroup 核心的 base cftype(cgroup.procs/cgroup.controllers/...) */
    BUG_ON(cgroup_init_cftypes(NULL, cgroup_base_files));
    /* ... PSI/cgroup1 base cftype ... */

    cgroup_lock();

    /* 把 init_css_set 挂进 css_set_table 哈希表 */
    hash_add(css_set_table, &init_css_set.hlist,
         css_set_hash(init_css_set.subsys));        /* L6094-L6095 */

    BUG_ON(cgroup_setup_root(&cgrp_dfl_root, 0));   /* 设置根 cgroup 的 kernfs */
    cgroup_unlock();

    /* 遍历所有 controller,逐个收尾 */
    for_each_subsys(ss, ssid) {
        if (ss->early_init) {
            /* 已 early init 的,补分配 id */
            struct cgroup_subsys_state *css = init_css_set.subsys[ss->id];
            css->id = cgroup_idr_alloc(&ss->css_idr, css, 1, 2, GFP_KERNEL);
        } else {
            cgroup_init_subsys(ss, false);   /* 没 early init 的,现在初始化 */
        }

        /* 把 init_css_set 挂到根 cgroup 的 e_csets[ssid] 链表 */
        list_add_tail(&init_css_set.e_cset_node[ssid],
                  &cgrp_dfl_root.cgrp.e_csets[ssid]);

        /* 在默认根的 subsys_mask 里标记本 controller 启用 */
        cgrp_dfl_root.subsys_mask |= 1 << ss->id;

        /* 注册本 controller 的 cftype 数组(cpu.max/memory.max/...) */
        WARN_ON(cgroup_add_dfl_cftypes(ss, ss->dfl_cftypes));
        WARN_ON(cgroup_add_legacy_cftypes(ss, ss->legacy_cftypes));

        /* 调 bind 回调,通知 controller 已挂到默认根 */
        if (ss->bind)
            ss->bind(init_css_set.subsys[ssid]);

        css_populate_dir(init_css_set.subsys[ssid]);   /* 在 kernfs 创建文件 */
    }

    /* init_css_set.subsys[] 在循环里被填过,重新哈希 */
    hash_del(&init_css_set.hlist);
    hash_add(css_set_table, &init_css_set.hlist,
         css_set_hash(init_css_set.subsys));        /* L6157-L6159 */

    register_filesystem(&cgroup2_fs_type);          /* 注册 cgroup2 文件系统 */
    proc_create_single("cgroups", 0, NULL, proc_cgroupstats_show);  /* /proc/cgroups */

    return 0;
}
```

([cgroup.c:6074-6170](../linux/kernel/cgroup/cgroup.c#L6074-L6170))

注意 [L6156-L6159](../linux/kernel/cgroup/cgroup.c#L6156-L6159) 这两行**重哈希**——`init_css_set` 在 `cgroup_init_early` 阶段挂进哈希表时,它的 `subsys[]` 还是空的(每个槽都 NULL);循环里逐个 controller 把 root css 塞进 `subsys[id]`,内容变了,**哈希值也变了**,所以必须**删掉旧条目、按新 subsys 数组重哈希**。这是 cgroup boot 里一个细腻的细节——哈希表里的位置必须反映 cset 当前的真实 subsys 状态,否则后续 `find_existing_css_set` 会找不到。

### 9.7.4 初始化完成后的世界

boot 完成后,世界长这样:

- 一棵 cgroup 树(根 `cgrp_dfl_root.cgrp`),`/sys/fs/cgroup/` 挂载着 `cgroup2` 文件系统;
- 15 个 controller 全部注册,各自的 root css 在 `cgrp_dfl_root.cgrp.subsys[id]` 和 `init_css_set.subsys[id]`;
- `init_css_set` 挂在 `css_set_table` 哈希表里,`init_task.cgroups` 指向它,`refcount=1`、`nr_tasks=1`;
- `init_task` 之后 fork 出的所有进程(`kthreadd`→所有内核线程,`init`→所有用户进程)**默认继承 `init_css_set`**,都在 root cgroup;
- 用户 `mkdir /sys/fs/cgroup/mycontainer` 建子 cgroup,`echo "+memory +cpu" > cgroup.subtree_control` 启用 controller(触发 `css_alloc` 造 css),`echo $PID > cgroup.procs` 迁移进程(触发 `cgroup_attach_task` → `find_css_set` 造/复用新 cset)——这些就是 P2-10、P2-11~14 要讲的内容。

---

## 9.8 技巧精解:`css_set` 去重表 + 哈希查找 —— 任务↔cgroup 多对多的账本

本章最硬核的一个技巧,值得单独拆透:**为什么 `css_set` 是一张"去重表",不是简单的"每个任务一个结构"?**

### 朴素方案撞墙:内存 + fork 开销

一个 K8s 节点跑 300 个 pod,每个 pod 平均 30 个进程,9000 个 task_struct。每个任务归属"一组" css(15 个指针,因为可能有 15 个 controller)。如果每个任务独立存这 15 个指针:

```
  朴素方案(糟糕):
  task_struct A.subsys[15]   = {&cpuset_css_X, &cpu_css_X, &memcg_css_X, ...}
  task_struct B.subsys[15]   = {&cpuset_css_X, &cpu_css_X, &memcg_css_X, ...}   ← 完全相同!
  task_struct C.subsys[15]   = {&cpuset_css_X, &cpu_css_X, &memcg_css_X, ...}   ← 完全相同!
  ...   (同 pod 内 30 个进程,15 个指针完全一样,重复 30 份)
  ...
  9000 task × 15 指针 = 135000 次指针存储,99% 冗余

  fork 时:inc 15 个 css 的引用计数(15 次 atomic_inc,15 次 cache line 反弹)
  exit 时:dec 15 次
  迁移整 pod 时:遍历 30 个 task,每个改 15 个指针
```

问题集中在三点:① **内存冗余**(同归属的任务重复存 15 个指针);② **fork/exit 慢**(15 次原子操作);③ **迁移整组慢**(30 × 15 = 450 次指针修改)。

### cgroup 的解法:中间加 `css_set`,哈希去重

Linux 的做法是**把"一组归属"抽出来**,叫 `css_set`(cset)。所有归属完全相同的任务**共享同一个 cset**——通过一张全局哈希表 `css_set_table`([cgroup.c:910](../linux/kernel/cgroup/cgroup.c#L910))去重。

```
  css_set 去重方案:

  task_struct A ─┐
  task_struct B ─┼─► css_set X ┬─ subsys[cpuset]  ──► cpuset_css_X
  task_struct C ─┤             ├─ subsys[cpu]     ──► cpu_css_X
  ...           ─┤             ├─ subsys[memory]  ──► memcg_css_X
  task_struct Z ─┘             └─ subsys[...]      (其余 12 个)
                  refcount = 30, nr_tasks = 30

  30 个同 pod 进程共享一个 cset,15 个指针只存一份
  fork 时:对 cset 的 refcount 做一次 inc(1 次原子操作)
  exit 时:对 cset 的 refcount 做一次 dec
  迁移整 pod:30 个 task 的 cgroups 指针换成另一个 cset(已存在的就复用,30 次指针赋值)
```

### 关键代码:`find_existing_css_set` 的 template + 哈希查找

去重的核心是 [`find_existing_css_set`](../linux/kernel/cgroup/cgroup.c#L1051)([cgroup.c:1051](../linux/kernel/cgroup/cgroup.c#L1051)),它做两件事:

**第一步,构造 template**——"如果我把任务迁到目标 cgrp,它的新 subsys 数组应该长什么样?"对每个 controller:

- 如果这个 controller **在本树启用了**(`root->subsys_mask & (1<<i)`),template[i] = 目标 cgroup 的**有效 css**(`cgroup_e_css_by_mask`,可能是祖先 cgroup 的 css——v2 里没启用的层会向上找);
- 如果**没启用**,template[i] = 旧 cset 的对应槽(不变)。

这一步的精妙是:**template 是"虚拟构造"的,不分配任何内存**——它只是一个栈上的 `css[15]` 数组,记录"目标归属应该是什么"。这样在没确定要新建之前,不浪费任何分配。

**第二步,哈希查找**——用 template 算哈希,在 `css_set_table` 里找:

```c
key = css_set_hash(template);                          /* 算哈希 */
hash_for_each_possible(css_set_table, cset, hlist, key) {
    if (!compare_css_sets(cset, old_cset, cgrp, template))
        continue;                                       /* 哈希冲突,但内容不同 */
    return cset;                                        /* 找到现成的,复用 */
}
return NULL;                                            /* 没现成的,得新建 */
```

`compare_css_sets`([cgroup.c:972](../linux/kernel/cgroup/cgroup.c#L972))做精确判定:先 `memcmp(template, cset->subsys, sizeof(cset->subsys))` 比对 15 个指针(L985),再比对涉及的 cgroup 链表(因为不同 cgroup 可能 effective css 相同,需要区分)。

### 为什么这套设计 sound:三个不变式

去重表要 sound,必须保证三个不变式:

1. **相同 subsys 数组的 cset 全机唯一**——这是 `find_existing_css_set` 的契约:如果两个 cset 的 subsys 完全相同,它们就**应该是同一个 cset**(否则去重没意义)。代码通过"先查再建"保证:`find_css_set` 先查 `find_existing_css_set`,有就复用,没有才 `kzalloc` 新建并 `hash_add`。查询和插入都在 `css_set_lock` 下(虽然 `find_existing_css_set` 内部短持锁、外层 `find_css_set` 在 `cgroup_mutex` 下),保证不会两个 CPU 同时建两个相同的 cset。
2. **cset 的 subsys 数组创建后不变**(immutable after creation)——源码注释明说([cgroup-defs.h:218-222](../linux/include/linux/cgroup-defs.h#L218-L222)):"This array is immutable after creation apart from the init_css_set during subsystem registration"。这是为什么:如果 cset 的 subsys 能改,去重就会被破坏(本来相等的两个 cset,一个改了就不等了)。任务要换归属,**不是改 cset 的 subsys,而是换一个 task_struct 指向的 cset**——这是 cgroup 设计的核心约束。
3. **cset 用 refcount + RCU 释放**——`put_css_set_locked`([cgroup.c:925](../linux/kernel/cgroup/cgroup.c#L925))在 refcount 归零时,先从哈希表里删掉(`hash_del`)、给所有 css 解引用(`css_put`)、最后 `kfree_rcu(cset, rcu_head)`([cgroup.c:959](../linux/kernel/cgroup/cgroup.c#L959))——**RCU 延迟释放**,保证外面正在 RCU 读这个 cset 的路径(`task_css_set(task)` 在 RCU 临界区里)不会读到已释放的内存。

### 反面对比:不用去重的代价

如果朴素地"每 task 存 15 个指针",在大规模容器场景:

- **内存**:9000 task × 15 指针 × 8 字节 = 1.08 MB 纯指针(看起来不大,但每次 fork 都要分配);相比之下去重方案,300 个 pod × 1 cset/pod × 15 指针 × 8 字节 = 36 KB,**省 30 倍**。
- **fork/exit 锁竞争**:朴素方案每次 fork 要 inc 15 个 atomic(跨 CPU cache line),几千核大机上成为瓶颈;去重方案只 inc 一个 cset 的 refcount(单 cache line),且 cset 本身因为同 pod 共享,cache 命中率高。
- **迁移开销**:朴素方案迁移一个 pod 的 30 个进程要改 30 × 15 = 450 个字段(每个要原子操作);去重方案只要 30 个 task 改一个 `task->cgroups` 指针(普通赋值,RCU 保护读者),目标 cset 还可能复用现成的(省掉新建)。

> **反面对比**:如果不用 `css_set` 去重表,大 K8s 节点(几百 pod、上万进程)的 cgroup 在 fork/exit/迁移 hot path 上会**内存冗余 + 原子操作瓶颈 + 迁移开销**三重崩盘。去重表把"多对多归属"压缩成"少数几个等价类",用**一层间接 + 一张哈希表**同时消灭三个问题——这是 cgroup 能 scale 到云原生规模的根本。

> **钉死这件事**:`css_set` 去重表是"任务↔cgroup 多对多用一张表"的典范——它把 N 个任务 × M 个 controller 的归属关系,压缩成"少数几个等价类(同归属的任务共享一个 cset)"。背后的三个不变式(同 subsys 唯一、subsys 创建后不变、RCU 释放)保证了 sound。这种"用一层间接换内存 + 性能 + 并发性"的思路,和第 8 本《内存分配器》的 per-CPU cache、《mm》的 per-cpu pageset 是**同一脉的内核工程美学**——它会在 P2-10 的 `cgroup_attach_task` 四步迁移里再次出现(迁移整组 = 换一个 cset 指针)。

---

## 章末小结

这一章是第 2 篇的**地基**——我们没有钻任何具体 controller,但把 cgroup v2 的**骨架**立清楚了:

1. **统一层级**(unified hierarchy):所有 controller 共用一棵 cgroup 树,根是 `cgrp_dfl_root.cgrp`;一个进程在树上只属于一个 cgroup,它的所有资源限额都从这个节点的 controller 文件读出。这消灭了 v1 的"归属矛盾",让"一个容器 = 一个 cgroup 节点 + 一套限额文件"成立。
2. **`css_set` 去重表**:进程通过 `task_struct->cgroups` 间接挂在 cgroup 树上——这个 `css_set` 是一组 css 指针,同归属的任务**共享一个 cset**(哈希去重)。这是 cgroup 在大规模容器场景能 scale 的根本。
3. **css = controller 实例**:`struct cgroup_subsys_state` 是 controller 视角的"原子对象",`mem_cgroup`/`task_group` 这些大结构**内嵌一个 css 当头部**,通过 `container_of` 反查——这是 C 实现 OOP 多态的经典手法。
4. **`cgroup_subsys` 函数指针表**:controller 的"类",定义了 css 生命周期/迁移/fork/exit 全部契约;核心代码通过 `ss->css_alloc()` 这种间接调用驱动所有 controller,**新增 controller 不改核心**。这是内核"用函数指针表实现多态"的教科书案例。
5. **boot 三步建起来**:`cgroup_init_early`(早 init + `init_task.cgroups = &init_css_set`)→ `cgroup_init_subsys`(逐个 controller 调 `css_alloc` 造 root css、填进 `init_css_set.subsys[]`)→ `cgroup_init`(挂哈希表 + 注册 cftype + 重哈希)。

回到二分法:本章服务**资源(cgroup)那一面的支撑层**。cgroup 后续 5 章(attach_task/cpu/memcg/io/pids+freezer)全部建立在这套骨架上——理解了"一棵树 + 一张去重表 + 一张函数指针表",后续每章只是看"这套骨架上,某个具体操作(迁移/限 CPU/记账内存)怎么走"。

### 五个"为什么"清单

1. **为什么 cgroup v2 用一棵统一树,而不是 v1 那样每个 controller 一棵?** v1 多树结构下,一个任务在 N 棵树上各属一个 cgroup,归属"分崩离析",没法形成"一个容器 = 一组限额"的语义;容器编排(Docker/K8s 想给每个容器发一组同时生效的限额)根本对不齐。v2 用统一树,所有 controller 共用一个节点,一个进程的所有限额来自同一个 cgroup,盒子干净。
2. **为什么进程不直接挂 cgroup,要套一层 `css_set`?** 直接挂会在每个 task_struct 里重复存 15 个 css 指针(同归属的任务重复),内存浪费 + fork/exit 慢(15 次原子操作)。`css_set` 把同归属的任务压缩到一个等价类,哈希去重——fork/exit 只改一次 refcount,迁移整组只换一个指针。
3. **为什么 css 是单独的结构,不直接把 `mem_cgroup` 塞进 cgroup?** 因为 cgroup 核心不需要知道 controller 的具体类型——它只需要"引用计数、上线/下线、挂在 cgroup 上"这套通用操作。把这些通用字段抽成 css,controller 大结构内嵌一个 css 当头部,核心拿 css 指针操作,要用 controller 数据再 `container_of`——C 里实现 OOP 多态的标准手法。
4. **为什么 `cgroup_subsys` 是函数指针表?** 它定义了 controller 的全部契约(css_alloc/online/attach/fork/exit/...),核心通过 `ss->xxx()` 间接调用。**新增 controller 不改核心代码一行**——只要填一张新的 `cgroup_subsys` 实例 + 加进 `cgroup_subsys.h` 的 `SUBSYS()` 列表。这就是内核"可插拔架构"的典范(VFS/sched_class/pernet_ops 同脉)。
5. **cgroup v2 凭什么能在几千核大机上 scale?** 三件事:① `css_set` 去重把归属压缩到少数等价类(内存 + fork/exit hot path 都省);② `task->cgroups` 用 RCU 读、`css_set_lock` 写、`cgroup_mutex` 改结构,**hot path(fork/exit)不被结构修改阻塞**;③ css 的引用计数用 percpu_ref,**每个 CPU 改自己的计数器,不撞 cache line**。

### 想继续深入往哪钻

- 本章讲清了静态结构,下一章 P2-10 钻**动态迁移**:`cgroup_attach_task` 的四步(add_src → prepare_dst → migrate → finish)、`find_css_set` 怎么造/复用目标 cset、`cgroup_threadgroup_rwsem` 怎么保证 fork/exit 不抢跑。
- 各 controller 的 charge/throttle 路径:P2-11(cpu,回扣《调度器》)、P2-12(memcg,回扣《mm》)、P2-13(io,回扣《块设备》)、P2-14(pids/freezer/cpuset)。
- 想自己读源码,从这几个文件入手:① [`include/linux/cgroup-defs.h`](../linux/include/linux/cgroup-defs.h) —— 所有结构定义;② [`kernel/cgroup/cgroup.c`](../linux/kernel/cgroup/cgroup.c) 的 [`cgroup_init`](../linux/kernel/cgroup/cgroup.c#L6074)(L6074)、[`find_css_set`](../linux/kernel/cgroup/cgroup.c#L1170)(L1170)、[`cgroup_attach_task`](../linux/kernel/cgroup/cgroup.c#L2866)(L2866);③ [`include/linux/cgroup_subsys.h`](../linux/include/linux/cgroup_subsys.h)(整个文件)—— controller 清单。
- 想观测 cgroup,看 `/sys/fs/cgroup/`(v2 挂载点)、`cat /proc/cgroups`(列出所有已注册 controller)、`cat /proc/self/cgroup`(自己在哪个 cgroup)、`systemd-cgls`(树状显示 cgroup 及其进程)。
- 想理解 v2 的"no internal process"约束(本章只点到),读 P4-18 和内核文档 `Documentation/admin-guide/cgroup-v2.rst`。

### 引出下一章

骨架立好了,下一章看**动作**——一次 `echo $PID > cgroup.procs` 触发的完整迁移链。我们要钻进 [`cgroup_attach_task`](../linux/kernel/cgroup/cgroup.c#L2866)(L2866)的四步迁移:add_src(在 `css_set_lock` 下收集源 cset)→ prepare_dst(用 `find_css_set` 找/建目标 cset)→ migrate(`css_set_move_task` 改指针)→ finish(放引用),以及那把把 fork/exit 挡在迁移之外的 `cgroup_threadgroup_rwsem`。下一章 P2-10。
