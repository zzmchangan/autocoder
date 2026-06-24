# 第十三篇 · 第 P2-13 章 · io 子系统:io.max 与 IOPS/BPS 限额

> 篇:P2 cgroup 资源控制
> 主线呼应:这一章服务二分法的**资源**那一面。前面 cpu.max(P2-11)限的是"时间片"、memory.max(P2-12)限的是"页数",这一章限的是**一根进程针对一块盘产生的字节流和 IO 次数**。容器里跑一个 MySQL,你想让它最多吃 50MB/s 的写、200 IOPS 的随机读,以免把宿主那块共享 SSD 抽干,拖垮同一台机器上别的租户——内核的 io cgroup 就是为这件事生的。但 io 和 cpu/memory 有一个根本差异:**cpu 和内存是均匀的资源**(一个 tick 就是一个 tick,一个 page 就是一个 page),而 **IO 不是**:SSD 上一次 4K 随机读和一次 1M 顺序写,对设备的代价天差地别。所以 io cgroup 的故事,先是一场"按字节/按次数"的朴素限流(	io.max → blk-throttle),再是一场"按真实代价"的精算限流(io.cost → blk-iocost)。本章把两条路都拆透,并回扣《Linux 块设备》那条 bio 旅程。

## 核心问题

**io cgroup 凭什么能给"一个进程对一块盘的 IO"单独限额?一个 bio 从 `submit_bio` 进去,内核怎么知道它该算到哪个 cgroup 的账上、该不该挡住、挡多久?为什么 6.9 上同时存在 blk-throttle 和 blk-iocost 两套机制,它们各自解决什么朴素限流撞墙的问题?**

读完本章你会明白:

1. **per-device per-cgroup 的二维账本**:io cgroup 的核心数据结构是 `blkcg_gq`(blkcg × request_queue 的二元组),一个进程对不同的盘各有独立的限额状态——这和 cpu/memory 那种"全局一份配额"完全不同。
2. **bio → blkg 的反查路径**:`submit_bio` 时内核通过 `bio_blkcg_css` → `bio_associate_blkg_from_css` → `blkg_lookup_create` 把 bio 挂到正确的 `blkcg_gq`,后续所有策略都读 `bio->bi_blkg`。
3. **io.max 的 token bucket**:`rbps/wbps/riops/wiops` 四个数翻译成"时间片内的字节/IO 配额",超了就 sleep,bio 在 `throtl_service_queue` 里排队等定时器叫醒。
4. **io.cost 的 vtime token 模型**:iocost 把每个 bio 的代价抽象成 vtime(顺序 IO 便宜、随机 IO 贵、大 IO 按页累加),按权重分配设备吞吐,用动态 vrate 反馈控制——这是"按真实代价限流"的精算版本。
5. ★ 对照 runc:Docker 的 `--device-read-bps`、K8s 的 `limits.storage`/`blkio` 资源,最终都落到内核的 `io.max` / `io.weight` 文件。

> **逃生阀**:如果觉得"token bucket、vtime、hweight"这些词太密,先抓一句话——**io.max 是"朴素按字节/次数限流"(挡 bio 等时间),io.cost 是"按真实设备代价限流"(挡 bio 等 token)**。前者简单粗暴,后者公平精算。本章先讲前者(它是 io.max 的直接实现),再讲后者(它是 io.max 撞墙后的进化)。

---

## 13.1 一句话点破

> **io cgroup 的本质是给每个 (cgroup, 块设备) 二元组单独建一本账,每个 bio 进来先查它属于哪本账、再按那本账上的限额决定"放行还是挡住、挡多久"。朴素地按字节数限(blk-throttle)对 SSD 不公平——4K 随机和 1M 顺序代价不同却算一样,所以内核又做了一套按设备真实代价计费的 iocost。**

这是结论,不是理由。本章倒过来拆:先看 io cgroup 的二维账本是怎么组织的(blkg = blkcg × request_queue),再看 bio 怎么挂到账上,然后钻进 blk-throttle 看 io.max 的 token bucket,最后看 iocost 怎么把"字节数"升级成"vtime token"。

---

## 13.2 二维账本:blkcg × request_queue = blkcg_gq

cpu cgroup 的 `cpu.max` 是一份配额,memory cgroup 的 `memory.max` 也是一份配额——它们都是**一维**的:cgroup 这个东西,有一个 CPU 配额、一个内存配额。但 io 不一样:同一个容器,可能对 `/dev/nvme0n1` 限 100MB/s,对 `/dev/sda` 限 10MB/s,对 `/dev/loop0` 不限。**限额是 per-device 的**。

这就逼出了 io cgroup 的核心数据结构 [`struct blkcg_gq`](../linux/block/blk-cgroup.h#L56-L92)([blk-cgroup.h:56](../linux/block/blk-cgroup.h#L56)),读作 "blkcg-gq",意为 "block cgroup - group queue":

```c
/* block/blk-cgroup.h:56(简化) */
struct blkcg_gq {
    struct request_queue     *q;          /* 关联到哪个块设备的队列 */
    struct blkcg             *blkcg;      /* 关联到哪个 blkcg */
    struct blkcg_gq          *parent;     /* 父 cgroup 对同一个设备的 blkg */
    struct percpu_ref         refcnt;     /* 引用计数 */
    bool                      online;

    struct blkg_iostat_set __percpu *iostat_cpu;  /* per-cpu 的 io 统计 */
    struct blkg_iostat_set         iostat;        /* 聚合后的统计 */

    struct blkg_policy_data     *pd[BLKCG_MAX_POLS];  /* 每个策略一份私有数据 */

    atomic_t          use_delay;     /* iolatency 的延迟惩罚计数 */
    atomic64_t        delay_nsec;    /* 累计延迟纳秒 */
    ...
};
```

注意第一行的 `q` 和 `blkcg`——**这个结构是 blkcg 和 request_queue 的笛卡尔积**:每个 (blkcg, 块设备) 二元组各有一个 `blkcg_gq`。一台宿主上有 N 个块设备、M 个 cgroup,就可能有 N×M 个 blkg。这个二维性是 io cgroup 和其他所有 cgroup controller 最根本的区别。

> **不这样会怎样**:如果 io cgroup 也像 cpu/memory 那样一个 cgroup 只存一份配额,你就没法说"这个容器对 nvme 限 100MB/s、对 sda 限 10MB/s"——你只能给一个全局 io 限额,这对一台机器上有多块盘(系统盘 + 数据盘 + 备份盘)的场景完全没用。`blkcg_gq` 的二维性,就是为了让限额粒度精细到"每个 cgroup 对每块盘"。

`struct blkcg` 本身([blk-cgroup.h:94](../linux/block/blk-cgroup.h#L94))挂的是 css 的那层(每个 cgroup 一个):

```c
/* block/blk-cgroup.h:94(简化) */
struct blkcg {
    struct cgroup_subsys_state   css;        /* 嵌入 css,多态接入 */
    spinlock_t                   lock;
    struct radix_tree_root       blkg_tree;  /* q_id → blkg 的基数树 */
    struct blkcg_gq __rcu       *blkg_hint;  /* 上次查到的 blkg 缓存 */
    struct hlist_head            blkg_list;  /* 这个 blkcg 下所有 blkg */
    struct blkcg_policy_data    *cpd[BLKCG_MAX_POLS];
    ...
};
```

`blkg_tree` 是一张 radix tree,key 是 `request_queue->id`,value 是这个 blkcg 对该 queue 的 blkg。同一个 blkcg 下所有设备各一个 blkg,挂在这棵树上。`blkg_hint` 是个快速缓存——大部分 IO 都是冲着同一块盘去的,上次查到的 blkg 这次大概率还是它,RCU 读 + 缓存命中省掉一次 radix tree 查找。

### blkg 的查找:`blkg_lookup` 三级跳

bio 进来要找到自己的 blkg,这是 io cgroup 记账的第一步。查找路径在 [`blkg_lookup`](../linux/block/blk-cgroup.h#L249-L266)([blk-cgroup.h:249](../linux/block/blk-cgroup.h#L249))——它是个 inline 函数,被设计成**热路径里能内联掉**:

```c
/* block/blk-cgroup.h:249(简化) */
static inline struct blkcg_gq *blkg_lookup(struct blkcg *blkcg,
                                           struct request_queue *q)
{
    struct blkcg_gq *blkg;

    if (blkcg == &blkcg_root)
        return q->root_blkg;                 /* ① 根 cgroup 直接返回 */

    blkg = rcu_dereference_check(blkcg->blkg_hint,  /* ② 查缓存 */
            lockdep_is_held(&q->queue_lock));
    if (blkg && blkg->q == q)
        return blkg;

    blkg = radix_tree_lookup(&blkcg->blkg_tree, q->id);  /* ③ 兜底 radix tree */
    if (blkg && blkg->q != q)
        blkg = NULL;
    return blkg;
}
```

三级跳:① 根 cgroup 走快速通道(根 blkg 在 queue 上有专门指针);② 看 `blkg_hint` 缓存(同一进程的 IO 通常冲同一块盘,命中率极高);③ 兜底走 radix tree。**整个查找在 RCU 临界区内,无锁**。

> **钉死这件事**:`blkg_lookup` 是 io cgroup 记账的入口——bio 要找到"我属于哪个 cgroup 对哪块盘的 blkg",才能进一步查限额、扣 token、记统计。这个查找被精心设计成"RCU + 三级缓存",因为它是 submit_bio 热路径上的一环——每个 bio 都要走一次,慢一点整机 IO 性能就掉。这种"热路径用 RCU + hint 缓存"的思路,和《调度器》里 `task_struct->cgroups` 指针直接拿到 `css_set`、内存分配器 per-CPU cache 的 fast path,是同一套工程设计哲学。

### bio 挂账:`bio_associate_blkg_from_css`

bio 在 `submit_bio` 之前,要先知道"我是谁发的、要打到哪块盘",才能挂到正确的 blkg。这条挂账路径在 [`bio_associate_blkg_from_css`](../linux/block/blk-cgroup.c#L2042)([blk-cgroup.c:2042](../linux/block/blk-cgroup.c#L2042))和它调用的 [`blkg_tryget_closest`](../linux/block/blk-cgroup.c#L2009)([blk-cgroup.c:2009](../linux/block/blk-cgroup.c#L2009)):

```c
/* block/blk-cgroup.c:2009(简化) */
static inline struct blkcg_gq *blkg_tryget_closest(struct bio *bio,
        struct cgroup_subsys_state *css)
{
    struct blkcg_gq *blkg, *ret_blkg = NULL;

    rcu_read_lock();
    blkg = blkg_lookup_create(css_to_blkcg(css), bio->bi_bdev->bd_disk);
    while (blkg) {
        if (blkg_tryget(blkg)) {            /* 拿到引用 */
            ret_blkg = blkg;
            break;
        }
        blkg = blkg->parent;                /* 这个 blkg 正在死,往上找父 */
    }
    rcu_read_unlock();
    return ret_blkg;
}
```

注意 `blkg->parent` 这一行的兜底——如果当前 cgroup 的 blkg 正在被销毁(`blkg_tryget` 失败),内核不会让 bio 失败,而是**往上找父 cgroup 的 blkg**,把 IO 记到父账上。这是 io cgroup 的一个 sound 设计:**bio 永远能找到一个可用的 blkg,即使原 cgroup 正在死**。

`blkg_lookup_create`([blk-cgroup.c:471](../linux/block/blk-cgroup.c#L471))比 `blkg_lookup` 多一步——找不到就创建,创建时要保证**从 blkcg_root 到目标 blkcg 的整条父子链都有 blkg**(否则 `blkg->parent` 链断裂):

```c
/* block/blk-cgroup.c:471(简化) */
static struct blkcg_gq *blkg_lookup_create(struct blkcg *blkcg,
                                            struct gendisk *disk)
{
    ...
    blkg = blkg_lookup(blkcg, q);
    if (blkg) return blkg;                  /* 命中 */

    spin_lock_irqsave(&q->queue_lock, flags);
    blkg = blkg_lookup(blkcg, q);           /* 双检 */
    if (blkg) { ...更新 hint...; goto found; }

    /* 从 root 往下逐层创建,保证 parent 链完整 */
    while (true) {
        struct blkcg *pos = blkcg, *parent = blkcg_parent(blkcg);
        struct blkcg_gq *ret_blkg = q->root_blkg;

        while (parent) {
            blkg = blkg_lookup(parent, q);
            if (blkg) { ret_blkg = blkg; break; }
            pos = parent;
            parent = blkcg_parent(parent);
        }
        blkg = blkg_create(pos, disk, NULL);   /* 真正创建 */
        if (IS_ERR(blkg)) { blkg = ret_blkg; break; }
        if (pos == blkcg) break;
    }
    ...
}
```

> **反面对比**:如果创建时不保证父子链完整,直接 `blkg_create(blkcg, disk, NULL)` 一把梭,那么 blkg 的 `parent` 可能是 NULL,后续做**层级 throttle**(子超了限父也跟着限)和**io.stat 累加到父**就没法做——父子链一断,层级资源约束全废。Linux 这里宁可麻烦一点也要保证 `parent` 永远非 NULL(根 blkg 的 parent 是 NULL,因为根没有父),这是"层级"能在 io cgroup 成立的根基。这和 memcg 的"charge 累加到所有祖先"、cpu cgroup 的"组调度嵌套"是同一个设计要求——**层级是 cgroup 的命,断了层级就不是 cgroup**。

bio 挂账完成后,`bio->bi_blkg` 就指向了正确的 `blkcg_gq`。后续 submit_bio 路径里所有的 io 策略(throttle / iocost / iolatency)都读 `bio->bi_blkg` 拿到这个 bio 的归属,不用再查 css。

---

## 13.3 函数指针多态:`blkcg_policy` 和 `rq_qos_ops`

`blkcg_gq` 里有一个字段 `pd[BLKCG_MAX_POLS]`——这是个指针数组,每个槽对应一个 io 策略(policy)。6.9 上有三种 io 策略:

- **blk-throttle**(io.max / io.low 的 BPS/IOPS 限流)
- **blk-iocost**(io.cost 的按代价权重分配)
- **blk-iolatency**(io.latency 的延迟保证)

每个策略各填一份 [`struct blkcg_policy`](../linux/block/blk-cgroup.h#L170-L187)([blk-cgroup.h:170](../linux/block/blk-cgroup.h#L170)),这是一张函数指针表:

```c
/* block/blk-cgroup.h:170(简化) */
struct blkcg_policy {
    int                plid;            /* policy id */
    struct cftype     *dfl_cftypes;     /* 这个策略的 cgroup 文件 */

    /* operations —— 每个策略各填一份 */
    blkcg_pol_alloc_cpd_fn   *cpd_alloc_fn;
    blkcg_pol_alloc_pd_fn    *pd_alloc_fn;
    blkcg_pol_init_pd_fn     *pd_init_fn;
    blkcg_pol_online_pd_fn   *pd_online_fn;
    blkcg_pol_offline_pd_fn  *pd_offline_fn;
    blkcg_pol_free_pd_fn     *pd_free_fn;
    blkcg_pol_reset_pd_stats_fn *pd_reset_stats_fn;
    blkcg_pol_stat_pd_fn     *pd_stat_fn;
};
```

每个策略实现这张表,比如 [`blkcg_policy_throtl`](../linux/block/blk-throttle.c#L1708-L1718)([blk-throttle.c:1708](../linux/block/blk-throttle.c#L1708)):

```c
/* block/blk-throttle.c:1708(简化) */
struct blkcg_policy blkcg_policy_throtl = {
    .dfl_cftypes    = throtl_files,        /* io.max 文件 */
    .legacy_cftypes = throtl_legacy_files,
    .pd_alloc_fn    = throtl_pd_alloc,
    .pd_init_fn     = throtl_pd_init,
    ...
};
```

注册时调 [`blkcg_policy_register(&blkcg_policy_throtl)`](../linux/block/blk-cgroup.c#L1499),把这张表挂进全局 `blkcg_policy[]` 数组。**核心路径调用时只走 `pol->pd_init_fn(...)`、`pol->pd_alloc_fn(...)` 这种间接调用**——cgroup 核心代码完全不知道具体是 throttle 还是 iocost,新增一个 io 策略不用改 cgroup 核心。

> **钉死这件事(函数指针多态)**:`blkcg_policy` 是 io cgroup 的"插件接口"。三个策略(throttle/iocost/iolatency)各填一张函数指针表,核心代码 `blkg_create` 在 [blk-cgroup.c:416](../linux/block/blk-cgroup.c#L416) 遍历所有 policy 调 `pol->pd_init_fn(blkg->pd[i])` —— 这是典型的"函数指针多态"。这和 `cgroup_subsys`(`cpu_cgroup_subsys`、`memory_cgroup_subsys` 各填一份 css_alloc/attach/can_fork)、`sched_class`(fair_class/rt_class/deadline_class 各填一份 enqueue_task/pick_next_task)是同一套内核工程范式——**用函数指针表实现多态,新增一族策略不改核心**。如果你没看懂这层间接,就会觉得"`pd_init_fn` 到底在干什么"云里雾里;一旦看清,整张 io cgroup 的架构图就清楚了:核心(blkcg_gq + blkcg)管二维账本和生命周期,策略(policy + pd)管具体的限流算法。

### 另一层多态:rq_qos_ops

io cgroup 的三个策略除了走 `blkcg_policy`,还各自实现了一份 [`struct rq_qos_ops`](../linux/block/blk-rq-qos.h#L37-L49)([blk-rq-qos.h:37](../linux/block/blk-rq-qos.h#L37)):

```c
/* block/blk-rq-qos.h:37(简化) */
struct rq_qos_ops {
    void (*throttle)(struct rq_qos *, struct bio *);
    void (*track)(struct rq_qos *, struct request *, struct bio *);
    void (*merge)(struct rq_qos *, struct request *, struct bio *);
    void (*issue)(struct rq_qos *, struct request *);
    void (*requeue)(struct rq_qos *, struct request *);
    void (*done)(struct rq_qos *, struct request *);
    void (*done_bio)(struct rq_qos *, struct bio *);
    ...
};
```

bio 走到 `submit_bio` 路径时,会调 [`rq_qos_throttle(q, bio)`](../linux/block/blk-rq-qos.h#L147)([blk-rq-qos.h:147](../linux/block/blk-rq-qos.h#L147)),它遍历 request_queue 上挂的所有 rq_qos 节点(每个节点是一个策略实例),依次调 `rqos->ops->throttle(rqos, bio)`。

为什么 io cgroup 需要两套多态?——**职责不同**:

- `blkcg_policy` 管 cgroup 树相关的生命周期(blkg 的创建/销毁/统计),和 css 绑定;
- `rq_qos_ops` 管每个 bio 的实际限流动作(throttle/done_bio/track),和 request_queue 绑定。

iocost 和 iolatency 是 rq_qos 框架下的"客人"(它们要 hook 每个 bio 的旅程,所以必须实现 rq_qos_ops);而 blk-throttle 是个特例——它**不走 rq_qos**,而是在 `submit_bio_noacct` 的 [blk-core.c:837](../linux/block/blk-core.c#L837) 直接调 [`blk_throtl_bio(bio)`](../linux/block/blk-throttle.h#L207)([blk-throttle.h:207](../linux/block/blk-throttle.h#L207))。这是历史遗留——blk-throttle 比 rq_qos 框架早,而且 throttle 最早是 cgroup-v1 时代的 blkio 限定接口,语义已经和 cgroup 树深度绑定。

```
 submit_bio() 路径上的 io 限流三道闸(blk-core.c:837 起):

   ┌──────────────────────────────────────────────────────────┐
   │ submit_bio_noacct  (blk-core.c:743)                     │
   │   │                                                      │
   │   ├─▶ blk_throtl_bio(bio)       ── ① io.max 直接限流    │
   │   │      (blk-throttle.h:207)                            │
   │   │      走 blkg_to_tg(bio->bi_blkg)                     │
   │   │                                                      │
   │   └─▶ submit_bio_noacct_nocheck                          │
   │         │                                                │
   │         └─▶ __submit_bio → blk_mq_submit_bio             │
   │               │                                          │
   │               └─▶ rq_qos_throttle(q, bio)                │
   │                     ├─▶ iocost.throttle  ── ② io.cost   │
   │                     └─▶ iolatency.throttle ── ③ io.lat   │
   └──────────────────────────────────────────────────────────┘
```

这就是为什么同一台机器上 io.max 和 io.cost 可以共存——它们在不同的 hook 点,做不同维度的限流。生产环境一般按需启用一种(或都启),很少混用。

---

## 13.4 io.max 的 token bucket:blk-throttle 的四元限流

现在钻进第一个限流路径:blk-throttle。这是 `io.max` 的直接实现,也是读者最常碰到的 io cgroup 文件。

### 四元限额:rbps / wbps / riops / wiops

[`io.max`](../linux/block/blk-throttle.c#L1681-L1698) 的 cftype 定义在 [blk-throttle.c:1681](../linux/block/blk-throttle.c#L1681):

```c
/* block/blk-throttle.c:1681(简化) */
static struct cftype throtl_files[] = {
    {
        .name = "max",
        .private = LIMIT_MAX,
        .seq_show = tg_print_limit,
        .write = tg_set_limit,           /* 解析 "rbps= wbps= riops= wiops=" */
    },
    {
        .name = "low",
        .private = LIMIT_LOW,
        ...
    },
    {}
};
```

用户写 `echo "8:16 rbps=10485760 wbps=max riops=100 wiops=100" > io.max`,内核的 [`tg_set_limit`](../linux/block/blk-throttle.c#L1562)([blk-throttle.c:1562](../linux/block/blk-throttle.c#L1562))解析这个字符串:

```c
/* block/blk-throttle.c:1612(简化,这是 tg_set_limit 内部的解析循环) */
while (true) {
    char tok[27];  /* wiops=18446744073709551616 */
    char *p;
    u64 val = U64_MAX;
    ...
    strsep(&p, "=");
    if (!p || (sscanf(p, "%llu", &val) != 1 && strcmp(p, "max")))
        goto out_finish;

    if (!strcmp(tok, "rbps") && val > 1)        v[0] = val;
    else if (!strcmp(tok, "wbps") && val > 1)   v[1] = val;
    else if (!strcmp(tok, "riops") && val > 1)  v[2] = min_t(u64, val, UINT_MAX);
    else if (!strcmp(tok, "wiops") && val > 1)  v[3] = min_t(u64, val, UINT_MAX);
    ...
}
```

四个维度——**读字节、写字节、读次数、写次数**——这是 io cgroup 最朴素的限流方式:把 IO 拆成"读了多少字节、写了多少字节、读了多少次、写了多少次"四个独立的水表,各自配额。

> **不这样会怎样**:为什么是四个独立水表而不是一个统一限额?因为读和写、字节和次数对设备的压力不一样。一个 100MB/s 的随机 4K 读(IOPS=25600)和一个 100MB/s 的顺序 1M 写(IOPS=100),字节数相同但 IOPS 差 256 倍,对 SSD 的磨损和延迟冲击天差地别。如果只用一个"字节配额",你限不住高 IOPS 的随机小 IO;如果只用一个"次数配额",你限不住一次读 1M 的大 IO。四个独立水表,让你可以精确表达"这个容器:大块读写不限字节但限制次数(低 IOPS 大块)、小块读写限制字节(避免随机小 IO 把 SSD 抽干)"这种细粒度策略。

### token bucket:时间片 + 已派发累计

四个水表怎么实现?经典的 **token bucket** 算法。`throtl_grp` 在 [blk-throttle.h:67](../linux/block/blk-throttle.h#L67) 存的就是这套状态:

```c
/* block/blk-throttle.h:67(简化) */
struct throtl_grp {
    struct blkg_policy_data    pd;            /* 嵌入,多态接入 */
    struct throtl_data        *td;            /* 所属 request_queue 的 throttle 全局 */
    struct throtl_service_queue service_queue; /* bio 排队的地方 */

    uint64_t   bps[2][LIMIT_CNT];             /* READ/WRITE × LIMIT_LOW/MAX */
    uint64_t   bps_conf[2][LIMIT_CNT];        /* 用户配置值(内部可能调整) */
    unsigned int iops[2][LIMIT_CNT];
    unsigned int iops_conf[2][LIMIT_CNT];

    uint64_t    bytes_disp[2];                /* 本时间片已派发字节 */
    unsigned int io_disp[2];                  /* 本时间片已派发 IO 数 */

    unsigned long slice_start[2];             /* 本时间片起始 jiffies */
    unsigned long slice_end[2];

    long long   carryover_bytes[2];           /* 切限额时,旧的等待量继承 */
    int         carryover_ios[2];

    bool        has_rules_bps[2];             /* 读/写是否有 BPS 规则 */
    bool        has_rules_iops[2];
    ...
};
```

核心是 `bytes_disp[2]` 和 `io_disp[2]`——**当前时间片内已经放出去的字节数和 IO 数**。每个 bio 进来,内核问:"在当前时间片内,放出去的累计有没有超过限额 * 已经过去的时间比例?"超了就 sleep,没超就放。

这个判断在 [`tg_within_bps_limit`](../linux/block/blk-throttle.c#L856)([blk-throttle.c:856](../linux/block/blk-throttle.c#L856)):

```c
/* block/blk-throttle.c:856(简化) */
static unsigned long tg_within_bps_limit(struct throtl_grp *tg, struct bio *bio,
                                          u64 bps_limit)
{
    bool rw = bio_data_dir(bio);
    long long bytes_allowed;
    u64 extra_bytes;
    unsigned long jiffy_elapsed, jiffy_elapsed_rnd, jiffy_wait;
    unsigned int bio_size = throtl_bio_data_size(bio);

    if (bps_limit == U64_MAX || bio_flagged(bio, BIO_BPS_THROTTLED))
        return 0;                          /* 无限或已记账,直接放 */

    jiffy_elapsed = jiffy_elapsed_rnd = jiffies - tg->slice_start[rw];
    if (!jiffy_elapsed)
        jiffy_elapsed_rnd = tg->td->throtl_slice;   /* 刚开始,取一个完整片 */

    jiffy_elapsed_rnd = roundup(jiffy_elapsed_rnd, tg->td->throtl_slice);
    /* 计算到当前为止,允许放出去的字节数 */
    bytes_allowed = calculate_bytes_allowed(bps_limit, jiffy_elapsed_rnd) +
                    tg->carryover_bytes[rw];
    if (bytes_allowed > 0 && tg->bytes_disp[rw] + bio_size <= bytes_allowed)
        return 0;                          /* 没超,放 */

    /* 超了,算要等多久 */
    extra_bytes = tg->bytes_disp[rw] + bio_size - bytes_allowed;
    jiffy_wait = div64_u64(extra_bytes * HZ, bps_limit);
    if (!jiffy_wait) jiffy_wait = 1;
    jiffy_wait = jiffy_wait + (jiffy_elapsed_rnd - jiffy_elapsed);
    return jiffy_wait;
}
```

`calculate_bytes_allowed` 算的是"`bps_limit` 在 `jiffy_elapsed_rnd` 这么长时间内允许放多少字节"——也就是把配额按时间比例展开。比如 `rbps=10MB/s`,过了 100ms,允许放出去的字节就是 10MB × 100ms/1000ms = 1MB。

bio 被挡住后,会进 [`tg_may_dispatch`](../linux/block/blk-throttle.c#L901)([blk-throttle.c:901](../linux/block/blk-throttle.c#L901))的判断:`bps_wait` 和 `iops_wait` 取最大值,作为这个 bio 要等的时间。然后 bio 被挂到 `tg->service_queue.queued[]`,throtl_grp 自己被挂到父队列的 pending_tree(按 `disptime` 排序的红黑树),一个 `pending_timer` 在最早该醒的时间触发 [`throtl_select_dispatch`](../linux/block/blk-throttle.c#L1121)([blk-throttle.c:1121](../linux/block/blk-throttle.c#L1121))把到点的 bio 放出去。

### BIO_BPS_THROTTLED:避免重复记账

注意 [`tg_within_bps_limit`](../linux/block/blk-throttle.c#L866) 里这个判断:

```c
if (bps_limit == U64_MAX || bio_flagged(bio, BIO_BPS_THROTTLED))
    return 0;
```

`BIO_BPS_THROTTLED` 这个 flag 在 [`tg_dispatch_one_bio`](../linux/block/blk-throttle.c#L1075)([blk-throttle.c:1075](../linux/block/blk-throttle.c#L1075))里被设置——当一个 bio 从子 tg 派发到父 tg 时:

```c
/* block/blk-throttle.c:1071-1080(简化) */
if (parent_tg) {
    /* bio 从子 tg 转到父 tg,父也要检查 */
    throtl_add_bio_tg(bio, &tg->qnode_on_parent[rw], parent_tg);
    start_parent_slice_with_credit(tg, parent_tg, rw);
} else {
    /* 到顶了,放出去 */
    bio_set_flag(bio, BIO_BPS_THROTTLED);
    throtl_qnode_add_bio(bio, &tg->qnode_on_parent[rw], &parent_sq->queued[rw]);
    ...
}
```

> **钉死这件事(BIO_BPS_THROTTLED 的语义)**:bio 在层级 tg 树里从叶子往根走,每一层都要过一次 token bucket 检查。但**字节不能重复扣**——子 tg 已经扣过这个 bio 的字节,父 tg 再扣一遍就是双重记账。`BIO_BPS_THROTTLED` 标记"这个 bio 的字节数已经在某一层扣过了,父层不要再扣字节",但**IO 次数(`io_disp`)还是要扣**——因为 iops 限制的语义是"对设备的请求次数",无论 bio 走到哪一层,最终都是一次设备请求。这种"BPS 只扣一次、IOPS 每层都扣"的设计,是 blk-throttle 处理层级记账的一个微妙细节,朴素地写很容易在这儿错。

---

## 13.5 io.cost 的 vtime token:把 IO 代价抽象成统一货币

blk-throttle 解决了"限多少字节/多少次",但它有一个根本缺陷——**它把所有字节当等价的,所有 IO 次数也当等价的**。这对 HDD 时代够用(HDD 的字节吞吐和 IO 次数基本线性),但 SSD 时代就不对了:

- SSD 上一次 4K 随机读和一次 1M 顺序读,字节数差 256 倍,但对设备延迟的贡献差不多(都是一次寻址 + 一次 NAND 读取,顺序读只是省了寻址开销);
- 反过来,256 次 4K 随机读和 1 次 1M 顺序读,字节数相同,但前者的设备延迟是后者的几十倍。

**朴素地按字节数限,会让随机小 IO 的容器占便宜(同样的字节数配额,它能打出几十倍的设备压力),让顺序大 IO 的容器吃亏。**

6.9 内核的 [`blk-iocost`](../linux/block/blk-iocost.c) 就是来解决这个问题的。它的核心思想是:**把每个 bio 的代价抽象成一个统一的虚拟时间(vtime),顺序 IO 便宜、随机 IO 贵、大 IO 按页累加,然后按权重分配设备的总 vtime 预算**。

### iocost 的核心数据结构

[`struct ioc`](../linux/block/blk-iocost.c#L406-L448)([blk-iocost.c:406](../linux/block/blk-iocost.c#L406))是**per-device** 的——每个启用 iocost 的块设备一个:

```c
/* block/blk-iocost.c:406(简化) */
struct ioc {
    struct rq_qos           rqos;            /* 接入 rq_qos 框架 */
    bool                    enabled;

    struct ioc_params       params;          /* cost model 系数 */
    struct ioc_margins      margins;
    u32                     period_us;       /* 一个周期多少微秒(默认 ~77ms) */
    u64                     vrate_min;       /* vrate 的下限 */
    u64                     vrate_max;

    spinlock_t              lock;
    struct timer_list       timer;           /* 周期定时器 */
    struct list_head        active_iocgs;    /* 活跃的 iocg 链表 */

    enum ioc_running        running;
    atomic64_t              vtime_rate;      /* 当前 vtime 速率(每周期增多少 vtime) */
    u64                     vtime_base_rate;
    s64                     vtime_err;       /* 反馈误差,用于调 vrate */

    seqcount_spinlock_t     period_seqcount;
    u64                     period_at;       /* 本周期起始 wallclock */
    u64                     period_at_vtime; /* 本周期起始 vtime */

    int                     busy_level;      /* 设备饱和度历史 */
    atomic_t                hweight_gen;     /* hweight 懒更新代际 */
    ...
};
```

关键字段是 `vtime_rate` —— **设备的"虚拟时间发行速率"**。设备每周期会"印"出一定数量的 vtime,这些 vtime 按 cgroup 的权重分给各个 iocg。cgroup 用完自己的 vtime,新的 bio 就要等。

[`struct ioc_gq`](../linux/block/blk-iocost.c#L462-L551)([blk-iocost.c:462](../linux/block/blk-iocost.c#L462))是 **per-device per-cgroup** 的——和 `blkcg_gq` 一一对应(它就嵌在 blkg 的 `pd` 里):

```c
/* block/blk-iocost.c:462(简化) */
struct ioc_gq {
    struct blkg_policy_data  pd;             /* 嵌入 blkg->pd[iocost_plid] */
    struct ioc              *ioc;

    u32   cfg_weight;        /* io.weight 配置的权重 */
    u32   weight;            /* 有效权重 */
    u32   active;            /* 当前激活的权重 */
    u32   inuse;             /* 实际使用的权重(surplus 调整后) */

    sector_t  cursor;        /* 上一个 IO 的末尾扇区,检测随机 IO */

    atomic64_t  vtime;       /* 本 iocg 的 vtime 游标,推进表示消费 */
    atomic64_t  done_vtime;  /* IO 完成时推进的 vtime */
    u64         abs_vdebt;   /* 欠的 vtime(优先级反转时产生) */

    u64   delay;             /* 当前被罚延迟多少纳秒 */
    u64   delay_at;

    atomic64_t   active_period;
    struct list_head  active_list;

    int   hweight_gen;       /* hweight 懒更新代际 */
    u32   hweight_active;    /* 本 iocg 占设备的硬件权重比(1M 为满) */
    u32   hweight_inuse;     /* 实际使用的硬件权重比 */

    struct wait_queue_head  waitq;           /* 等待 vtime 的 bio 在这里睡 */
    struct hrtimer          waitq_timer;
    ...
};
```

`vtime` 是核心——每个 iocg 有一个 vtime 游标,设备也有一个 vtime 游标(由 `vtime_rate` 推进)。**iocg 的 vtime 落后于设备 vtime,说明它还有预算;超前,说明它超用了**。这是 iocost 的记账原语。

### vtime cost:线性模型 + 随机检测

每个 bio 的 vtime 代价,在 [`calc_vtime_cost_builtin`](../linux/block/blk-iocost.c#L2521-L2564)([blk-iocost.c:2521](../linux/block/blk-iocost.c#L2521))算:

```c
/* block/blk-iocost.c:2521(简化) */
static void calc_vtime_cost_builtin(struct bio *bio, struct ioc_gq *iocg,
                                     bool is_merge, u64 *costp)
{
    struct ioc *ioc = iocg->ioc;
    u64 coef_seqio, coef_randio, coef_page;
    u64 pages = max_t(u64, bio_sectors(bio) >> IOC_SECT_TO_PAGE_SHIFT, 1);
    u64 seek_pages = 0;
    u64 cost = 0;

    if (!bio->bi_iter.bi_size) goto out;

    switch (bio_op(bio)) {
    case REQ_OP_READ:
        coef_seqio  = ioc->params.lcoefs[LCOEF_RSEQIO];
        coef_randio = ioc->params.lcoefs[LCOEF_RRANDIO];
        coef_page   = ioc->params.lcoefs[LCOEF_RPAGE];
        break;
    case REQ_OP_WRITE:
        coef_seqio  = ioc->params.lcoefs[LCOEF_WSEQIO];
        coef_randio = ioc->params.lcoefs[LCOEF_WRANDIO];
        coef_page   = ioc->params.lcoefs[LCOEF_WPAGE];
        break;
    default: goto out;
    }

    if (iocg->cursor) {                          /* 用上一个 IO 的末尾检测随机 */
        seek_pages = abs(bio->bi_iter.bi_sector - iocg->cursor);
        seek_pages >>= IOC_SECT_TO_PAGE_SHIFT;
    }

    if (!is_merge) {
        if (seek_pages > LCOEF_RANDIO_PAGES)     /* 跨页太多 = 随机 */
            cost += coef_randio;                 /* 一次性代价:随机 */
        else
            cost += coef_seqio;                  /* 一次性代价:顺序 */
    }
    cost += pages * coef_page;                   /* 按页累加 */
out:
    *costp = cost;
}
```

公式是 **`cost = (一次性代价: seqio 或 randio) + pages * page_cost`**。三件事:

1. **一次性代价**:`coef_seqio` 或 `coef_randio`,表示"发起一次 IO 的固定开销"。随机比顺序贵(因为 HDD 要寻道、SSD 也要切 die);
2. **按页累加**:每个 4K 页加一份 `coef_page`,表示"传输一页数据的时间";
3. **随机检测**:用 `iocg->cursor`(上一个 IO 的末尾扇区)和当前 bio 的起始扇区比较,如果跨页超过 `LCOEF_RANDIO_PAGES`,就算随机。

这套系数(blk-iocost.c 的 `lcoefs[]`)可以根据设备类型自动选择(AUTOP_HDD / AUTOP_SSD_QD1 / AUTOP_SSD_DFL / AUTOP_SSD_FAST,iocost.c:369),内核启动时自动检测设备类型并套用合适的系数。也可以手动通过 `io.cost.model` 调。

> **钉死这件事(为什么是线性模型)**:朴素地按字节数限,是 `cost = bytes`(只看一项)。iocost 升级成 `cost = oneshot + pages * page_cost`(两项加性模型)。为什么是加性而不是乘性?因为真实设备的 IO 延迟本来就是"固定开销 + 按数据量线性增长"——HDD 是"寻道时间 + 传输时间",SSD 是"命令处理 + NAND 读取"。线性加性模型能很好地拟合真实延迟。这就是 iocost 比 blk-throttle 公平的根本——**它用真实设备延迟模型估算代价,而不是用字节数这种粗糙代理**。

### throttle 路径:预算检查 + 等 vtime

bio 走到 iocost 的 [`ioc_rqos_throttle`](../linux/block/blk-iocost.c#L2599)([blk-iocost.c:2599](../linux/block/blk-iocost.c#L2599))时:

```c
/* block/blk-iocost.c:2599(简化) */
static void ioc_rqos_throttle(struct rq_qos *rqos, struct bio *bio)
{
    struct blkcg_gq *blkg = bio->bi_blkg;
    struct ioc *ioc = rqos_to_ioc(rqos);
    struct ioc_gq *iocg = blkg_to_iocg(blkg);
    struct ioc_now now;
    struct iocg_wait wait;
    u64 abs_cost, cost, vtime;
    ...

    abs_cost = calc_vtime_cost(bio, iocg, false);   /* 算这个 bio 的 vtime 代价 */
    if (!abs_cost) return;

    if (!iocg_activate(iocg, &now)) return;          /* 激活 iocg(首次 IO) */

    iocg->cursor = bio_end_sector(bio);
    vtime = atomic64_read(&iocg->vtime);
    cost = adjust_inuse_and_calc_cost(iocg, vtime, abs_cost, &now);

    /* 预算够,立刻放行 */
    if (!waitqueue_active(&iocg->waitq) && !iocg->abs_vdebt &&
        time_before_eq64(vtime + cost, now.vnow)) {
        iocg_commit_bio(iocg, bio, abs_cost, cost);  /* 扣 vtime */
        return;
    }

    /* 预算不够,挂 waitq 等 */
    ...
    init_waitqueue_func_entry(&wait.wait, iocg_wake_fn);
    wait.bio = bio;
    wait.abs_cost = abs_cost;
    __add_wait_queue_entry_tail(&iocg->waitq, &wait.wait);
    iocg_kick_waitq(iocg, ioc_locked, &now);          /* 设定时器,到点叫醒 */

    while (true) {
        set_current_state(TASK_UNINTERRUPTIBLE);
        if (wait.committed) break;
        io_schedule();                                 /* 睡 */
    }
    finish_wait(&iocg->waitq, &wait.wait);
}
```

预算检查就一行:`time_before_eq64(vtime + cost, now.vnow)`——**"我的 vtime 加这个 bio 的 cost,不超过设备当前 vtime(`now.vnow`)"就放行**。不放行的 bio 挂在 `iocg->waitq` 上睡觉,`iocg_kick_waitq` 设一个 hrtimer,到点根据 vrate 推进的 vtime 重新评估能不能叫醒。

> **反面对比**:朴素地写 blk-throttle 是"`bytes_disp + bio_size <= bytes_allowed`"——纯字节数比较。iocost 升级成"`vtime + cost <= vnow`"——用真实代价模型算的 cost 比较同一份预算。看起来只多了一步"算 cost",但这一步把"4K 随机"和"1M 顺序"区分开了——前者 cost 高(走了 randio 系数),后者 cost 低(只算 page × page_cost,均摊后便宜)。这就是 iocost 能让"随机 IO 大户"和"顺序 IO 大户"在同一权重下公平竞争的原因。

---

## 13.6 io.latency:保证延迟的第三条路

除了 io.max(限字节/次数)和 io.cost(按权重分配),6.9 还有第三条路 [`io.latency`](../linux/block/blk-iolatency.c#L1041)——**直接保证 IO 延迟**。用户写 `echo "8:16 target=10000" > io.latency`(目标延迟 10ms),内核就会监控这个 cgroup 的实际 IO 延迟,延迟超了就惩罚(用 `blkcg_add_delay` 让后续 bio 在 submit 时强行睡一会),延迟好了就放开。

这条路径的核心是 [`blkcg_iolatency_throttle`](../linux/block/blk-iolatency.c#L463)([blk-iolatency.c:463](../linux/block/blk-iolatency.c#L463))和 [`iolatency_record_time`](../linux/block/blk-iolatency.c#L488)([blk-iolatency.c:488](../linux/block/blk-iolatency.c#L488)):

```c
/* block/blk-iolatency.c:488(简化) */
static void iolatency_record_time(struct iolatency_grp *iolat,
                                   struct bio_issue *issue, u64 now,
                                   bool issue_as_root)
{
    u64 start = bio_issue_time(issue);
    u64 req_time;
    ...
    if (now <= start) return;
    req_time = now - start;                  /* 这个 bio 实际花了多久 */

    if (unlikely(issue_as_root && iolat->max_depth != UINT_MAX)) {
        u64 sub = iolat->min_lat_nsec;
        if (req_time < sub)
            blkcg_add_delay(lat_to_blkg(iolat), now, sub - req_time);  /* 罚 */
        return;
    }
    latency_stat_record_time(iolat, req_time);   /* 记录,用于统计判断 */
}
```

iolatency 不算 token、不限字节,它限的是**并发深度**(max_depth)——通过 `rq_qos_wait` 让超发的 bio 排队等。如果实际延迟持续超 target,它就把 `max_depth` 调小(限并发);如果延迟好了,就放开。这是一种**反馈控制**:不预设字节数或权重,而是观察结果(延迟)动态调整。

iolatency 用 [`blkcg_add_delay`](../linux/block/blk-cgroup.c)(blkcg_gq 的 `delay_nsec` 字段)直接给 bio 加延迟——这是 io cgroup 提供给所有策略共享的"延迟惩罚原语",iocost 在 [`iocg_kick_delay`](../linux/block/blk-iocost.c#L1346)([blk-iocost.c:1346](../linux/block/blk-iocost.c#L1346))也用同一套机制。

> **钉死这件事**:io.max(blk-throttle)是**绝对限额**(你只能吃这么多),io.cost(iocost)是**比例分配**(按权重分蛋糕),io.latency(iolatency)是**结果保证**(延迟不超 target)。三者解决的是不同维度的问题,可以按场景选:数据库要稳定延迟,选 io.latency;多租户要按权重分吞吐,选 io.cost;简单限速,选 io.max。这是 6.9 io cgroup 的三套工具,合在一起覆盖了"IO 资源控制"的主要诉求。

---

## 13.7 io.stat:per-device 统计怎么攒

最后说 [`io.stat`](../linux/block/blk-cgroup.c#L1177-L1183) 这个文件——它输出每个 blkg 的 IO 统计:

```c
/* block/blk-cgroup.c:1177(简化) */
static struct cftype blkcg_files[] = {
    {
        .name = "stat",                /* cgroup v2 自动加 "io." 前缀 → "io.stat" */
        .seq_show = blkcg_print_stat,
    },
    { }
};
```

读 `io.stat` 会走 [`blkcg_print_stat`](../linux/block/blk-cgroup.c#L1157)([blk-cgroup.c:1157](../linux/block/blk-cgroup.c#L1157)),它先 `cgroup_rstat_flush` 把 per-cpu 的统计刷一下(避免读到旧值),然后遍历这个 blkcg 下所有 blkg(每个设备一个),调 [`blkcg_print_one_stat`](../linux/block/blk-cgroup.c#L1105)([blk-cgroup.c:1105](../linux/block/blk-cgroup.c#L1105))输出:

```c
/* block/blk-cgroup.c:1134(简化,这是 blkcg_print_one_stat 内部) */
seq_printf(s, "rbytes=%llu wbytes=%llu rios=%llu wios=%llu dbytes=%llu dios=%llu",
           rbytes, wbytes, rios, wios, dbytes, dios);
```

输出形如 `8:16 rbytes=12345678 wbytes=87654321 rios=100 wios=200 dbytes=0 dios=0`——**每块设备一行,带 r/w/d(读/写/discard)的 bytes 和 ios**。

### per-cpu 攒 + rstat flush

统计本身是怎么攒的?bio 完成时,在 [`__blkcg_rstat_flush`](../linux/block/blk-cgroup.c#L997)([blk-cgroup.c:997](../linux/block/blk-cgroup.c#L997))里,内核用 per-cpu 的 `blkg->iostat_cpu` 累加,然后通过 cgroup rstat 框架向上传播。`blkg_iostat_set` 用 `u64_stats_sync`(seqcount)保护 64 位统计的读一致性——这是经典的"per-cpu 计数器 + seqcount 读写"无锁模式:

```c
/* block/blk-cgroup.c:989-994(简化) */
flags = u64_stats_update_begin_irqsave(&blkg->iostat.sync);
blkg_iostat_add(&blkg->iostat.cur, &delta);
blkg_iostat_add(last, &delta);
u64_stats_update_end_irqrestore(&blkg->iostat.sync, flags);
```

> **钉死这件事(为什么用 per-cpu)**:IO 统计是热路径——每个 bio 完成都要累加。如果用一把全局自旋锁,多核并发性能会塌掉(《调度器》里讲过 per-CPU rq->lock、《内存分配器》讲过 per-CPU cache,都是同一思路)。blk-cgroup 用 `__percpu` 的 `iostat_cpu`,每个 CPU 各自累加自己的副本,读 `io.stat` 时通过 cgroup rstat 框架一次性 flush 汇总——**写路径无锁,读路径偶尔 flush**。这是内核统计接口的标准做法,/proc/diskstats、/proc/stat 都是这套路。

---

## 13.8 技巧精解:per-device 限额 + iocost token 模型

本章最硬核的两个技巧,单独拆透。

### 技巧一:`blkcg_gq` 二维账本 + radix_tree + RCU hint 缓存

io cgroup 的二维性(每个 (cgroup, 块设备) 各一份 blkg)带来一个工程问题:**bio 进 submit_bio 时怎么快速找到自己的 blkg?**这是热路径,慢一点整机 IO 就掉。

朴素地写,会用一张二维哈希表 `(blkcg, q) → blkg`。但 Linux 没这么写,而是用了**radix tree + hint + RCU** 三层组合:

```c
/* block/blk-cgroup.h:249(简化,这是热路径的内联查找) */
static inline struct blkcg_gq *blkg_lookup(struct blkcg *blkcg,
                                           struct request_queue *q)
{
    struct blkcg_gq *blkg;

    if (blkcg == &blkcg_root)
        return q->root_blkg;                         /* ① 根 cgroup 走专有指针 */

    blkg = rcu_dereference_check(blkcg->blkg_hint,   /* ② 先看 hint 缓存 */
            lockdep_is_held(&q->queue_lock));
    if (blkg && blkg->q == q)
        return blkg;

    blkg = radix_tree_lookup(&blkcg->blkg_tree, q->id);  /* ③ 兜底 radix tree */
    ...
    return blkg;
}
```

三层各自的用意:

| 层 | 数据结构 | 为什么这么设计 |
|---|---------|---------------|
| ① 根 cgroup 快速通道 | `q->root_blkg` 直接指针 | 宿主 init 进程的 IO 几乎全走根,直接指针省一次查找 |
| ② hint 缓存 | `blkcg->blkg_hint`(RCU 保护) | 同一进程的 IO 通常冲同一块盘(数据盘),上次查到的这次大概率还是它,命中率 >99% |
| ③ radix tree 兜底 | `blkcg->blkg_tree`(key=q_id) | 缓存未命中时按设备 id 查;radix tree 对稀疏 id 高效,且内存紧凑 |

**为什么用 radix tree 而不是哈希表?** 因为块设备 id(`request_queue->id`)是连续分配的整数,radix tree 在这种场景下内存占用比哈希表小、查找速度也快(无 hash 计算、无冲突链)。这和《Linux mm》里页表用多级 radix tree 是同一思路——**对连续稀疏整数 key,radix tree 完美**。

**为什么 hint 要 RCU 保护?** 因为 hint 是个单指针,可能在查找时被其他 CPU 并发更新(比如 blkg 创建时更新 hint)。RCU 读让查找完全无锁,更新时用 `rcu_assign_pointer` 原子写。这样热路径(读)完全无锁,慢路径(更新 blkg_hint)才需要同步。

> **反面对比**:如果朴素地用一把全局自旋锁保护一张哈希表,每个 bio 查 blkg 都要拿锁——多核高并发 IO 场景(比如 NVMe 上几十万 IOPS),锁竞争会让 cgroup 记账的开销超过 IO 本身。Linux 的"RCU + hint + radix tree"三层组合,把热路径压到一次 RCU 读 + 一次指针解引用,几乎零开销。这是 io cgroup 能在高性能存储场景生存的根基。

### 技巧二:iocost 的 vtime token + 动态 vrate 反馈

iocost 的 token 模型分两部分:**静态的 vtime 记账**(把 bio 代价抽象成 vtime)和**动态的 vrate 反馈**(根据设备饱和度调整 vtime 发行速率)。

**静态部分**:每个 iocg 有 `vtime` 游标,设备有 `period_at_vtime + vrate × elapsed`。bio 的 cost 由 `calc_vtime_cost` 算(seqio/randio + pages × page_cost),[`iocg_commit_bio`](../linux/block/blk-iocost.c#L2633)([blk-iocost.c:2633](../linux/block/blk-iocost.c#L2633))把 cost 加到 iocg 的 vtime 上。设备的 vtime 由 `vtime_rate` 推进——**vtime_rate 越高,设备"印 token"越快,所有 iocg 的预算都涨**。

**动态部分**:每个周期(period_us,默认约 77ms),[`ioc_timer_fn`](../linux/block/blk-iocost.c#L2234)([blk-iocost.c:2234](../linux/block/blk-iocost.c#L2234))被定时器触发,做三件事:

1. 看设备忙不忙(`busy_level`,基于 `rq_wait_pct` 等指标);
2. 调 [`ioc_adjust_base_vrate`](../linux/block/blk-iocost.c#L993)([blk-iocost.c:993](../linux/block/blk-iocost.c#L993)):设备太忙就降 vrate(整体收紧),设备太闲就升 vrate(整体放松);
3. 重新分配每个 iocg 的 hweight([`__propagate_weights`](../linux/block/blk-iocost.c#L1084) + [`current_hweight`](../linux/block/blk-iocost.c#L1166))。

这是个**闭环反馈控制系统**:测输出(设备延迟、队列深度)→ 调输入(vrate)→ 影响输出。和《调度器》里 EEVDF 的动态调整、《内存分配器》里 GC 的触发水位,是同一类设计——**用反馈控制逼近真实系统行为,而不是死板地按预设值跑**。

### hweight:层级权重的懒更新

iocost 还有一个值得提的技巧——**hweight(硬件权重)的懒更新**。每个 iocg 的 `hweight_active` / `hweight_inuse` 表示它占设备的实际权重比(1M 为满)。但如果每次有 iocg 加入/退出都立刻重算所有 iocg 的 hweight,在几百个 cgroup 的场景下会很慢。

iocost 用 [`hweight_gen`](../linux/block/blk-iocost.c#L523) 代际标记:权重变了就 `atomic_inc(&ioc->hweight_gen)`,各 iocg 读自己的 `hweight_gen`,发现过期了才用 [`current_hweight`](../linux/block/blk-iocost.c#L1166) 重算。**懒更新**——只在要用的时候才算,而不是变更时就算。这是《Linux mm》里 page flag 懒清理、《调度器》里 load 懒更新同一套思路。

---

## ★ 对照 runc / Docker / K8s

内核提供 `io.max` / `io.weight` / `io.latency` 文件,runc 按 OCI 规范把它们用到容器上。Docker 的 `--device-read-bps`、`--device-write-iops`、`--blkio-weight` 这些 flag,在 runc 里最终翻译成写 cgroup 文件:

| Docker / K8s 接口 | runc 动作 | 内核文件 | 对应机制 |
|---|---|---|---|
| `docker run --device-read-bps=/dev/sda:10mb` | 写 `io.max` "MAJ:MIN rbps=10485760" | `blk-throttle.c:tg_set_limit` | io.max BPS 限流 |
| `docker run --device-write-iops=/dev/sda:100` | 写 `io.max` "MAJ:MIN wiops=100" | `blk-throttle.c:tg_set_limit` | io.max IOPS 限流 |
| `docker run --blkio-weight=500` | 写 `io.weight` "500" | `blk-iocost.c` 权重 | iocost 比例分配 |
| K8s pod `resources.limits.blkio` | 同上 | 同上 | — |

runc 在 [`libcontainer/cgroups`](../linux-container设计与实现) 下有 `devices.go`、`blkio.go`(cgroup v1)或 `v2/io.go`(cgroup v2)负责写这些文件。cgroup v2 的 `io.max` 格式是 `"MAJ:MIN rbps=... wbps=... riops=... wiops=..."`,runc 直接把 Docker 的 flag 拼成这个字符串写进去。**Docker/K8s 的 IO 限额,本质就是给 runc 一个数字,runc 把它写成 `io.max` 那行字符串**——内核在 `tg_set_limit` 里解析这行字符串,落到 `throtl_grp->bps[READ][LIMIT_MAX]` 这个字段上。

生产环境实战注意:① `io.max` 限的是"提交到块设备的字节/次数",如果走 page cache(`write()` 后没 fsync),实际 IO 是延迟异步发生的,throttle 在 writeback 路径生效;② iocost 需要显式启用(写 `io.cost.qos`),默认不开;③ io.latency 是 4.19+ 才稳定,老内核没有。这三点在排查"为什么 IO 限额没生效"时常常是坑。

---

## 章末小结

这一章讲 io cgroup,服务二分法的**资源**那一面——和 cpu.max(P2-11)、memory.max(P2-12)并列,回答"进程能用多少"。但 io 有它独特的地方:资源是**二维的**(per-cgroup × per-device),代价是**不均匀的**(4K 随机 ≠ 1M 顺序)。这两个独特性,造就了 io cgroup 的两个招牌设计——`blkcg_gq` 二维账本和 iocost vtime token。

1. **二维账本**:`blkcg_gq = blkcg × request_queue` 的笛卡尔积。bio 通过 `bio_blkcg_css` → `bio_associate_blkg_from_css` → `blkg_lookup_create`(radix_tree + hint + RCU)挂到正确的 blkg。
2. **函数指针多态**:`blkcg_policy`(cgroup 生命周期)+ `rq_qos_ops`(bio 旅程 hook)两套接口,throttle/iocost/iolatency 各填一份。
3. **io.max 的 token bucket**:`rbps/wbps/riops/wiops` 四元限额,`bytes_disp/io_disp` 在时间片内累加,超了 sleep。`BIO_BPS_THROTTLED` 防止层级双重记账。
4. **io.cost 的 vtime token**:`cost = seqio/randio + pages × page_cost` 线性模型,把 IO 真实代价抽象成 vtime,按权重分配;动态 vrate 反馈控制设备饱和度。
5. **io.latency 的延迟保证**:用 `blkcg_add_delay` 反馈控制实际延迟,不预设字节/权重。
6. ★ 对照 runc:Docker `--device-read-bps` → `io.max` 字符串,内核 `tg_set_limit` 解析。

### 五个"为什么"清单

1. **为什么 io cgroup 是二维的(每个 cgroup 对每块盘各一份配额)?** 因为同一容器对不同的盘限额需求不同(系统盘不限、数据盘限、备份盘更严)。如果像 cpu/memory 那样一维,就无法表达这种粒度。
2. **为什么 io.max 有四个数(rbps/wbps/riops/wiops)?** 因为读和写、字节和次数对设备压力不同。高 IOPS 随机小 IO 用字节限不住,大块顺序 IO 用次数限不住——四元独立配额才能精确表达策略。
3. **为什么还要有 io.cost,io.max 不够吗?** 因为 io.max 把所有字节当等价,SSD 上 4K 随机和 1M 顺序代价不同却算一样,不公平。iocost 用真实延迟模型算 vtime,让随机 IO 大户和顺序 IO 大户在同权重下公平竞争。
4. **为什么 blkg_lookup 要三层(radix_tree + hint + RCU)?** 因为这是 submit_bio 热路径,每个 bio 都要走。radix_tree 兜底、hint 命中(>99%)、RCU 无锁读——把热路径压到接近零开销。
5. **为什么 io cgroup 用两套函数指针多态(blkcg_policy + rq_qos_ops)?** 因为职责不同:`blkcg_policy` 管 cgroup 树生命周期(创建/销毁/统计),`rq_qos_ops` 管 bio 旅程 hook(throttle/done_bio/track)。blk-throttle 是历史遗留不走 rq_qos,直接在 submit_bio 调 `blk_throtl_bio`。

### 想继续深入往哪钻

- **源码阅读路线**:① 先读 [`block/blk-cgroup.h`](../linux/block/blk-cgroup.h#L56) 看数据结构(blkcg_gq@56, blkcg@94, blkcg_policy@170, blkg_lookup@249);② 再读 [`block/blk-cgroup.c`](../linux/block/blk-cgroup.c) 的 `blkg_create`@375 / `blkg_lookup_create`@471 / `blkcg_print_stat`@1157;③ 钻 [`block/blk-throttle.c`](../linux/block/blk-throttle.c) 的 `tg_set_limit`@1562 / `tg_may_dispatch`@901 / `throtl_select_dispatch`@1121;④ 进阶读 [`block/blk-iocost.c`](../linux/block/blk-iocost.c) 的 `calc_vtime_cost_builtin`@2521 / `ioc_rqos_throttle`@2599 / `ioc_timer_fn`@2234;⑤ 可选 [`block/blk-iolatency.c`](../linux/block/blk-iolatency.c) 的 `blkcg_iolatency_throttle`@463 / `iolatency_check_latencies`@523。
- **观测**:`cat /sys/fs/cgroup/<path>/io.stat`(看每个 cgroup 对每块盘的 IO 统计)、`cat /sys/fs/cgroup/<path>/io.max`(看限额)、`iostat -x 1`(设备级 IO)、`blktrace`(bio 旅程)、`perf trace -e block:*`(block 层事件)。
- **延伸阅读**:内核文档 `Documentation/admin-guide/cgroup-v2.rst` 的 IO 章节;`Documentation/block/iocost.rst`(iocost 设计文档,讲 vrate 反馈控制);`Documentation/accounting/blkio`(blkio 统计)。
- **对照阅读**:回扣《Linux 块设备》系列讲 bio→request→dispatch 的旅程,本章是把 cgroup 限流这层叠在 bio 旅程的 submit 入口;《调度器》P6-19 的 cpu.max throttle 是时间片维度,本章是字节/次数维度,两者机制很像(token bucket + sleep),对比着看更清楚。

### 引出下一章

io cgroup 讲完了,cgroup 的六大 controller(cpu/memory/io/pids/freezer/cpuset)也讲了三个重头(cpu/memory/io)。下一章 P2-14 我们把剩下三个相对简单的(pids 限进程数、freezer 冻结整组、cpuset 绑核/绑内存节点)合并讲完——它们各自的实现技巧不如 cpu/memory/io 复杂,但各有招牌(pids 的 counter、freezer 的 TIF_FROZEN 状态机、cpuset 的 cpumask),合在一起讲收束"资源控制"这一篇。然后第 3 篇(P3-15~17)我们正式进入"容器组装"——clone/unshare/setns + 写 cgroup.procs + pivot_root,看 runc 怎么把这些内核积木拼成一个真正的容器。
