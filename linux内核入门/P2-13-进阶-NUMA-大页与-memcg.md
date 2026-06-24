# 第 13 章 · 进阶:NUMA、大页与 memcg

> 前面 12 章把"单机、普通页、无限制"这个最朴素模型讲透了。这一章把模型推向真实的服务器世界:多 CPU 节点、超大内存、容器隔离——三个现代场景,逼出三组新机制。

---

## 章首 · 核心问题

到此为止,我们一直假设一个"理想世界":物理内存是一栋均匀的大楼,从任何房间取货一样快;每个房间都是 4KB 的标准间;进程想用多少内存,内核就尽力满足。

可现实的服务器完全不是这样。三个新场景,各打碎一条假设:

1. **多 CPU 服务器**:一颗 CPU 有几十个核,内存分成几个"块"挂在不同的 CPU 控制器下。访问挂在自己 CPU 下的内存快,访问挂在别的 CPU 下的内存慢。**"所有房间一样近"这个假设碎了。** —— 这是 **NUMA**。
2. **大内存数据库 / JVM**:几十上百 GB 的内存,几百万个 4KB 页。CPU 的 TLB(页表速查缓存)只有几千项,根本装不下这么多页的映射,频繁 TLB miss,性能塌方。**"4KB 页够用"这个假设碎了。** —— 这是 **大页(huge page)**。
3. **容器化(K8s/Docker)**:一台物理机跑几十个容器,每个容器要被限制内存用量,不能让一个容器吃光整台机器。**"进程想用多少给多少"这个假设碎了。** —— 这是 **memcg(内存 cgroup)**。

本章的核心困惑是:

> **这三个看似不相关的进阶机制,背后是不是同一个设计哲学?它们各自给"前面 12 章的朴素模型"打了什么补丁?**

**读完本章你会明白:**

- NUMA 是什么,"近的内存更快"在代码里怎么体现为"优先本地节点分配",mempolicy 怎么把决策权交给用户;
- 大页为什么救命(省页表、救 TLB),hugetlb(预留)和 THP(透明折叠)两种路线各自的取舍;
- memcg 怎么给每个容器单独开账,`memory.max` / `memory.high` / 局部 OOM 三道闸怎么分级把关;
- 为什么这三个机制其实是**同一个哲学的不同化身**:用"分类 + 亲和性 + 限额"对抗"真实硬件的物理约束"。

一句话概括本章在大楼比喻里的位置:**大楼升级成了"楼群"(NUMA),有些房间被打通成"大套间"(大页),每户住户被发了"限量消费卡"(memcg)。物业的活变细了,但底层那一套(伙伴系统、page cache、回收)一个没换——只是被这三组新规则"包了一层"。**

> **如果一读觉得太难**:先只记住三件事——
> ① **NUMA = 内存分块挂在不同 CPU 下,内核优先在"本 CPU 所在的节点"分配,跨节点是慢路**;
> ② **大页 = 把多个 4KB 页合并成 2MB/1GB 的大块,页表项少 N 倍、TLB 命中率高 N 倍**;
> ③ **memcg = 给每个容器一本独立账本,`memory.max` 是硬上限(超了杀进程)、`memory.high` 是软上限(超了限流)**。
>
> 这三件事的共同点是:**都是在"真实硬件约束/隔离需求"出现后,给前面 12 章的朴素分配器加的一道约束层**。其余细节(mempolicy 的几种模式、THP 的折叠算法、charge 批量化)第二遍配合 `numactl`/`/sys/fs/cgroup` 再抠。

---

# 第一部分 · NUMA:内存不再"一视同仁"

## 一、NUMA 是什么:一栋楼变成了"楼群"

### 先看硬件现实

小机器(笔记本、台式机)通常只有一个 CPU 插槽,所有内存条都挂在它下面,访问任何一条内存延迟都差不多——这叫 **UMA(Uniform Memory Access)**。

可服务器不一样。一台大服务器可能装 2 颗、4 颗、8 颗物理 CPU,每颗 CPU 旁边插着一圈自己的内存条。于是内存被**物理上分成了几块**,每块"归"某一颗 CPU 管:

```
        CPU 0                  CPU 1
      (节点 0)               (节点 1)
        │ 内存                  │ 内存
        │ 32GB                  │ 32GB
        │                       │
        └─────── 互联总线 ──────┘
            (跨节点访问慢)
```

- CPU 0 访问节点 0 的内存:走自己的内存控制器,**快**(约 80ns);
- CPU 0 访问节点 1 的内存:得跨**互联总线**(Intel 叫 UPI,AMD 叫 Infinity Fabric),**慢**(约 130ns+,而且要争抢总线带宽)。

这种"每颗 CPU 有本地内存、跨 CPU 访问更慢"的架构,叫 **NUMA(Non-Uniform Memory Access,非一致性内存访问)**。

> **比喻**:UMA 是"一栋大楼、一个总仓";NUMA 是"楼群,每栋楼有自己的小仓"。住户(进程)优先从自己楼的小仓取货(本地内存),去别栋楼的小仓取货要绕路、还要挤公共走廊(互联总线)。

### 软件怎么对应

内核里,每个 NUMA "块" 叫一个**节点(node)**,用 `pg_data_t`(`NODE_DATA(nid)`)描述。前面第 5 章讲的 ZONE,其实是**每个节点内部**再分 DMA/DMA32/NORMAL。也就是说,真实的内存布局是**两层的**:

```
   节点 0                     节点 1
   ├── ZONE_DMA               ├── ZONE_DMA
   ├── ZONE_DMA32             ├── ZONE_DMA32
   └── ZONE_NORMAL            └── ZONE_NORMAL
```

`/proc/buddyinfo` 里你会看到 `Node 0, Zone Normal`、`Node 1, Zone Normal` 这样的行——就是这台机器有几个节点、每个节点有哪些区。

## 二、为什么"近的内存更快":内核怎么倾向于本地节点

### 不这样会怎样

如果 NUMA 机器上,内核分配内存时**随机挑节点**(比如 CPU 0 上的进程经常分到节点 1 的内存),那每次访存都走慢路,性能直接塌——明明有近的内存不用,偏去用远的。在大内存数据库里,这种"远内存"能把延迟拉高几十个百分点。

### 所以这样设计:优先本地节点,实在不够才跨节点

内核分配内存的入口 `alloc_pages_noprof`,默认的"首选节点"是**当前 CPU 所在的节点**:

```c
struct page *alloc_pages_noprof(gfp_t gfp, unsigned int order)
{
    ...
    /* preferred_nid 默认来自 numa_node_id()(当前 CPU 所在节点),
       但 mempolicy 可能覆盖它 */
    ...
}
```

([mm/mempolicy.c:2359](../linux-6.14/mm/mempolicy.c#L2359) 的 `alloc_pages_noprof` 入口,默认 `nid = numa_node_id()`)

拿到 `preferred_nid` 后,准备分配上下文时,就用**这个节点的 zonelist**:

```c
static inline bool prepare_alloc_pages(gfp_t gfp_mask, unsigned int order,
        int preferred_nid, nodemask_t *nodemask,
        struct alloc_context *ac, ...)
{
    ac->highest_zoneidx = gfp_zone(gfp_mask);
    ac->zonelist = node_zonelist(preferred_nid, gfp_mask);   /* 用首选节点的 zonelist */
    ...
    ac->preferred_zoneref = first_zones_zonelist(ac->zonelist,
                    ac->highest_zoneidx, ac->nodemask);      /* 从本地节点开始找 */
    ...
}
```

([mm/page_alloc.c:4491-4529](../linux-6.14/mm/page_alloc.c#L4491-L4529),**简化示意**:省略 cpuset、CMA、fail-inject 分支,保留"按首选节点取 zonelist、定首选 zone"主逻辑)

关键在 `node_zonelist(preferred_nid, ...)`:它返回的"候选 zone 列表"是**为这个节点排好序的**——本地节点的 zone 排在最前,跨节点的 zone 按距离远近排在后面(这就是 `ZONELIST_FALLBACK`,见 [page_alloc.c:5134](../linux-6.14/mm/page_alloc.c#L5134))。分配时顺着这个列表找,自然就**先试本地、本地不够再试远程**。

> **这就是 NUMA "倾向本地"在代码里的全部体现**:不是什么神秘算法,只是**zonelist 里把本地节点排前面**。分配器还是那个伙伴系统,只是"先去哪个区找"被 NUMA 距离重排了。

## 三、mempolicy:把"在哪分配"的决策权交给用户

默认策略(优先本地)对大多数应用够用,但有些场景需要更精细的控制——比如数据库想固定在某几个节点上、或者想让一片内存**交错(round-robin)分布在所有节点**上以最大化带宽。内核提供 **mempolicy(memory policy)** 机制。

常见的几种策略:

| 策略 | 含义 | 适用场景 |
|---|---|---|
| `MPOL_DEFAULT` | 用系统默认(本地优先) | 绝大多数进程 |
| `MPOL_BIND` | 绑定到指定的一组节点,只能从这些节点分配 | 数据库固定节点 |
| `MPOL_INTERLEAVE` | 交错:轮流从各节点分配,均摊带宽 | 大数组、想要均匀带宽 |
| `MPOL_PREFERRED` | 偏好某节点,不够可回退 | 软偏好 |

mempolicy 在分配路径里覆盖默认的 `preferred_nid`。看 mempolicy.c 的入口逻辑:它从当前任务的 `mempolicy` 算出真正该用的节点和 nodemask:

```c
*nodemask = policy_nodemask(gfp_flags, *mpol, ilx, &nid);
```

([mm/mempolicy.c:2115](../linux-6.14/mm/mempolicy.c#L2115),在分配前把 policy 翻译成 `nid` + `nodemask`,塞进 `alloc_context`)

用户态怎么设?有三种方式:

- `numactl --membind=0 ...` 启动时给整个进程设策略;
- `set_mempolicy()` / `mbind()` 系统调用,运行时设(可针对某段地址范围);
- `numad` 守护进程,自动把进程迁到它用的内存所在的节点。

> **一个反直觉点**:NUMA 优化不是"把所有访问变快",而是"**避免把该快的访问变成慢的**"。本地访问是物理允许的最快值,你做不了什么让它更快;你能做的是别让数据跑错节点。所以 NUMA 调优的本质是**"少走远路"**,而不是"走近路"。

---

# 第二部分 · 大页:把"4KB 标准间"打通成"大套间"

## 四、为什么 4KB 页在大内存时会要命:TLB 压力

### 先理解 TLB 是什么

TLB(Translation Lookaside Buffer)是 CPU 芯片里的一个**小缓存**,专门缓存"虚拟页号 → 物理页号"的页表翻译结果。CPU 每次访存都要先查页表翻译地址,如果每次都去内存里查多级页表,慢得无法忍受——所以把最近查过的翻译结果记在 TLB 里。

> **小白补一句**:TLB 就是第 8 章讲的"台账(页表)的速查缓存"。它容量很小(典型几千项),但速度极快(和 CPU 缓存同量级)。

### 不这样会怎样

考虑一个 64GB 内存、跑数据库的服务器。如果全用 4KB 页,那是 1600 万个页——也就是 1600 万条页表映射。可 TLB 只能装几千条。结果:

- 数据库扫一张大表,几乎每次访问一个新页,**TLB miss**,得去内存查多级页表(4~5 次内存访问);
- TLB miss 的代价(memory stall)可能比实际数据访问还大;
- 1600 万个页表项本身就要占几十 MB 内存(页表也是要驻留的)。

这种"页太多、TLB 装不下"的病,在大内存机器上叫 **TLB 压力**。

### 所以这样设计:用大页,一个页表项管一大片

x86_64 支持几种大页:**2MB**(= 512 个 4KB 页合并)和 **1GB**(= 512 个 2MB 合并)。用 2MB 大页时:

- 一个 2MB 大页,在页表里只占**一个 PMD 项**(而不是 512 个 PTE);
- 64GB 内存用 2MB 页 = 32000 个大页——TLB 装得下了;
- 同样的 TLB 容量,大页能覆盖的内存大 512 倍,**TLB miss 暴跌**;
- 页表本身也小了 512 倍。

> **比喻**:标准间 4KB,一个门牌(页表项)一个房间;大套间 2MB,一个门牌管 512 个连通房间。住户找"201~712 号"这一大片,只要查一个门牌就够——台账(TLB)上记得少、查得快。

> **为什么大页没全面取代 4KB?** 因为它有代价:大页的粒度粗(2MB),分配一个就要 2MB 连续物理内存(碎片化时难凑),而且内部碎片大(只用 1 字节也占 2MB)。所以大页是**对"大块、顺序、长期驻留"的内存**最优,对"小而杂"的内存反而不利。4KB 仍是通用默认,大页是特定场景的优化。

## 五、两种大页路线:hugetlb vs THP

内核给了**两条完全不同的大页路线**,理解它们的区别是本节的关键:

| | hugetlb | THP(Transparent Huge Page) |
|---|---|---|
| 怎么得到 | **启动时预留**一片大页池 | **运行时透明**地把普通页合并成大页 |
| 谁负责 | 用户显式申请(`mmap` + `MAP_HUGETLB`) | 内核自动(对匿名页、或 `madvise(MADV_HUGEPAGE)`) |
| 能否回退 | 申请失败就失败(池子满了) | 失败了就用普通 4KB 页,**应用无感** |
| 内存浪费 | 预留的池子**独占**,别的用途用不了 | 按需合并、按需拆分,**灵活** |
| 适合 | 数据库、巨型 lookup 表,要确定性 | 通用透明加速,大多数情况 |

一句话:**hugetlb 是"VIP 预留大套间",THP 是"物业后台悄悄把零散房间打通"。**

## 六、hugetlb:预分配的"VIP 大套间"

hugetlb 的核心是**预留**:你在启动时(或运行时)告诉内核"给我预留 N 个 2MB 大页",内核就从伙伴系统里把这片大页**圈出来,单独管**,别的分配碰不到它们。用的时候,应用显式要(`mmap(..., MAP_HUGETLB)` 或 `shmget(..., SHM_HUGETLB)`),从这片预留池里拿。

内核里,每种大页尺寸对应一个 `struct hstate`(huge state):

```c
struct hstate {
    ...
    unsigned int order;                       /* 这个 hstate 的页大小 = 2^order,如 2MB → order=9 */
    unsigned long max_huge_pages;              /* 预留上限(用户设的 nr_hugepages) */
    unsigned long nr_huge_pages;               /* 当前实际预留的大页数 */
    unsigned long free_huge_pages;             /* 其中空闲的 */
    unsigned long resv_huge_pages;             /* 被预留(reservation)占去的 */
    unsigned long surplus_huge_pages;          /* 临时超分配的 */
    struct list_head hugepage_activelist;
    struct list_head hugepage_freelists[MAX_NUMNODES];  /* 每个节点一条空闲链 */
    ...
};
```

([include/linux/hugetlb.h:655-676](../linux-6.14/include/linux/hugetlb.h#L655-L676),**简化示意**:省略锁、demote、name,保留容量与每节点空闲链相关字段)

读这个结构,hugetlb 的设计哲学一目了然:**它有自己的计数器(`max/nr/free/resv/surplus`),完全独立于伙伴系统的常规分配**。这片池子是"VIP 专用",普通 `alloc_pages` 拿不到、回收也基本不动它(hugetlb 页是锁定的、不参与常规回收)。

你可以这样调它:

```bash
echo 1024 > /proc/sys/vm/nr_hugepages     # 预留 1024 个(默认 2MB)大页
cat /proc/meminfo | grep -i huge           # 看 HugetlbPages 总量、空闲
```

> **hugetlb 的痛点**:预留的池子是**独占**的——你留了 2GB 大页,这 2GB 就锁死,普通进程用不了。如果应用没真正用上,这 2GB 就浪费了。而且申请时如果池子满,**直接失败**(不像 THP 能回退)。所以 hugetlb 适合"我知道我要多少、要确定性"的重负载场景。

## 七、THP:透明折叠与 khugepaged

THP 走的是另一条路:**应用不用改代码、不用显式申请**,内核在后台尽量把"适合"的普通 4KB 页**合并(collapse)成 2MB 大页**,合并失败就老实用 4KB。整个过程对应用透明——所以叫 "Transparent"。

THP 的开关是一组全局 flag:

```c
/*
 * By default, transparent hugepage support is disabled in order to avoid
 * risking an increased memory footprint for applications that are not
 * guaranteed to benefit from it. When transparent hugepage support is
 * enabled, it is for all mappings, and khugepaged scans all mappings.
 * Defrag is invoked by khugepaged hugepage allocations and by page faults
 * for all hugepage allocations.
 */
unsigned long transparent_hugepage_flags __read_mostly =
#ifdef CONFIG_TRANSPARENT_HUGEPAGE_ALWAYS
    (1<<TRANSPARENT_HUGEPAGE_FLAG)|               /* 总是启用 */
#endif
#ifdef CONFIG_TRANSPARENT_HUGEPAGE_MADVISE
    (1<<TRANSPARENT_HUGEPAGE_REQ_MADV_FLAG)|      /* 只对 madvise 过的启用 */
#endif
    (1<<TRANSPARENT_HUGEPAGE_DEFRAG_REQ_MADV_FLAG)|
    (1<<TRANSPARENT_HUGEPAGE_DEFRAG_KHUGEPAGED_FLAG)|
    (1<<TRANSPARENT_HUGEPAGE_USE_ZERO_PAGE_FLAG);
```

([mm/huge_memory.c:52-69](../linux-6.14/mm/huge_memory.c#L52-L69))

这段注释把 THP 的三种模式说清楚了:

- **always**(默认):对所有映射尽量用大页;
- **madvise**:只对应用主动 `madvise(MADV_HUGEPAGE)` 标记过的区域用(保守,避免给不受益的应用增加内存占用);
- **never**:完全关掉。

THP 大页怎么"长"出来?两个时机:

1. **page fault 时**:进程访问一段匿名内存触发 fault,内核如果能找到 2MB 连续物理内存,就直接**分配一个大页**而不是 4KB 页(这叫 fault-time THP);
2. **khugepaged 后台线程**:有个内核线程 `khugepaged` 不断扫描进程的页表,**发现"一整片连续的 4KB 普通页"就尝试把它们折叠(collapse)成一个 2MB 大页**。这是"事后整理"。

> **khugepaged 就是"物业的整理工"**:白天住户们各用各的小房间,夜里整理工来巡查,看到"201~712 这一片都是同一户的、且连续",就悄悄把它们打通成一个大套间(改页表、迁移数据)。住户第二天醒来发现自己的房间变大了、查台账更快了,但完全不知道发生了什么。

> **THP 的痛点**:折叠需要"连续物理内存"——内存碎片化严重时,想合并却凑不出 2MB 连续块,反而要触发**同步内存整理(direct compaction)**,可能造成短暂卡顿。这就是 THP 在某些场景(尤其是低内存、高碎片)被诟病"引起延迟尖刺"的原因。可调:用 `madvise` 模式只对受益区域启用,或 `echo never > .../enabled` 关掉。

---

# 第三部分 · memcg:给每个容器单独开账

## 八、容器隔离:为什么需要"分户账本"

### 不这样会怎样

容器化的核心承诺是"隔离":一台机器跑 20 个容器,每个容器声明"我最多用 4GB"。如果没有 memcg,任何一个容器里的 bug 进程吃光了整机内存,就会触发**全局 OOM**(第 12 章),内核的 OOM killer 在**所有容器里**挑一个杀——可能杀错容器,杀一个无辜的邻居。隔离形同虚设。

### 所以这样设计:每个 cgroup 一本独立账本

**memcg(memory cgroup)** 给每个 cgroup(通常对应一个容器)挂一个 `struct mem_cgroup`,里面有一组 `page_counter`(计数器)记录这个组用了多少内存:

```c
struct mem_cgroup {
    struct mem_cgroup *parent;        /* cgroup 是树状的,子组继承父组限额 */
    ...
    struct page_counter memory;       /* 当前内存用量 + 限额(v1/v2 通用) */
    union {
        struct page_counter swap;     /* swap 用量(v2) */
        struct page_counter memsw;    /* 内存+swap 合计(v1) */
    };
    ...
    struct work_struct high_work;     /* memory.high 限流的延迟工作 */
    bool oom_group;                    /* 是否把 OOM 限制在本组内 */
    ...
};
```

([include/linux/memcontrol.h:183-...](../linux-6.14/include/linux/memcontrol.h#L183),**简化示意**:省略 event、zswap、stats 等大量字段,保留限额计数器与 high/oom 相关字段)

关键概念:`page_counter` 是树状的。子组的用量会**向上累加**到父组。所以你可以"给整个 K8s node 设上限、给每个 pod 设上限、给每个容器设上限",层层限额,任何一层超了都能被发现。

## 九、charge:每次分配都要"记账"

memcg 怎么知道某个组用了多少?答案是:**每一次内存分配(一个页进 page cache、一个匿名页、一个 slab 对象),内核都顺手做一笔 charge(记账)**,把这个页的用量加到当前进程所属的 memcg 上;释放时 uncharge(销账)。

> **小白补一句**:charge 不是"分配内存",而是"把这块内存的用量记到某本账上"。内存还是伙伴系统/page cache 在分配,memcg 只是在旁边记账。

记账的入口是 `try_charge_memcg`([memcontrol.c:2210](../linux-6.14/mm/memcontrol.c#L2210))。它干的事,正是"查限额 → 没超就记一笔 → 超了就回收/限流/杀":

```c
int try_charge_memcg(struct mem_cgroup *memcg, gfp_t gfp_mask,
             unsigned int nr_pages)
{
    unsigned int batch = max(MEMCG_CHARGE_BATCH, nr_pages);
    ...
retry:
    if (consume_stock(memcg, nr_pages))          /* ① 先用本地"库存"(批量预记的) */
        return 0;

    if (... page_counter_try_charge(&memcg->memory, batch, &counter))  /* ② 尝试加到账上 */
        goto done_restock;                        /*    没超限额 → 成功 */

    mem_over_limit = ...;                         /* 超限额了,记住是哪个 counter 卡住 */
    ...
    /* ③ 回收:试着从这个 memcg 里腾出 nr_pages 页 */
    nr_reclaimed = try_to_free_mem_cgroup_pages(mem_over_limit, nr_pages, ...);
    if (mem_cgroup_margin(mem_over_limit) >= nr_pages)
        goto retry;                               /* 回收够了 → 重试 */

    ...
    /* ④ 还不够 → 触发本组 OOM(见下一节) */
    if (mem_cgroup_oom(mem_over_limit, gfp_mask, ...)) {
        ...
    }
    ...
}
```

([mm/memcontrol.c:2210-2310](../linux-6.14/mm/memcontrol.c#L2210-L2310),**简化示意**:省略 PF_MEMALLOC 绕过、drain_stock、NORETRY/RETRY_MAYFAIL 等分支,保留"库存 → 记账 → 回收 → OOM"四级阶梯;`①`~`④` 为本书所加讲解标注,源码原文无)

注意这条路径和**全局**分配回收路径几乎一模一样(第 6/12 章讲的 `__alloc_pages_slowpath`:快路径 → 慢路径回收 → OOM)。这不是巧合——**memcg 把全局的那套"限额 → 回收 → OOM"机制,在每个 cgroup 内部又复制了一份**。这就是"局部 OOM"的由来。

## 十、三道闸:`memory.max` / `memory.high` / 局部 OOM

memcg 不是只有"杀"这一招。它有三道闸,从软到硬,对应 cgroup v2 的三个文件:

| 文件 | 作用 | 超了会怎样 |
|---|---|---|
| `memory.high` | **软上限** | 回收 + **限流**(让超用的进程睡一会儿,见下),不杀 |
| `memory.max` | **硬上限** | 回收,还不行 → **本组 OOM kill**(只在本组内挑进程杀) |
| `memory.current` | (只读)当前用量 | — |

### 局部 OOM:只杀本组的进程

`memory.max` 超了,`try_charge_memcg` 走到最后会调 `mem_cgroup_oom` → `mem_cgroup_out_of_memory`:

```c
static bool mem_cgroup_out_of_memory(struct mem_cgroup *memcg, gfp_t gfp_mask,
                                     int order)
{
    ...
    oom_mask = memcg_oom_mask(memcg);                       /* 只挑本组的进程 */
    oc = (struct oom_control) {
        .zonelist = NULL,
        .nodemask = NULL,
        .memcg = memcg,                                     /* OOM 限定在这个 memcg */
        .gfp_mask = gfp_mask,
        .order = order,
    };
    ...
}
```

([mm/memcontrol.c:1625-1657](../linux-6.14/mm/memcontrol.c#L1625-L1657),**简化示意**:省略 margin 检查与 GFP 判断,保留"OOM 上下文绑定到本 memcg"的核心)

注意 `.memcg = memcg`——OOM killer 被限制在**只在这个 cgroup 的进程里**挑牺牲品。这就是容器隔离的关键:**一个容器吃爆了自己的限额,只杀它自己内部的进程,绝不殃及邻居**。如果设了 `memory.oom_group`,甚至会杀掉整个组。

### `memory.high` 限流:不杀,只让你慢下来

`memory.high` 是个"温柔"的闸。超了不杀进程,而是让超用的进程**睡一段时间**(`schedule_timeout_killable`),相当于"用 CPU 时间的代价换内存"——你超用了,就让你跑慢点,逼你自我收敛:

```c
void mem_cgroup_handle_over_high(gfp_t gfp_mask)
{
    ...
    penalty_jiffies = calculate_high_delay(memcg, nr_pages,
                           mem_find_max_overage(memcg));   /* 超得越多,睡得越久 */
    ...
    psi_memstall_enter(&pflags);
    schedule_timeout_killable(penalty_jiffies);              /* 让进程睡 penalty_jiffies */
    psi_memstall_leave(&pflags);
    ...
}
```

([mm/memcontrol.c:2117-2208](../linux-6.14/mm/memcontrol.c#L2117-L2208),**简化示意**:省略 swap overage、retry、TASK_KILLABLE 细节,保留"按超用量算惩罚时长 → 睡眠"主逻辑)

`calculate_high_delay` 算出的 `penalty_jiffies` **和超用量成正比**——超得越多,睡得越久。这是一种负反馈:超用 → 睡 → 进度慢 → 单位时间内的超用量降下来。比直接 OOM 温和得多,适合"想限流但不想杀"的场景(比如给后台任务设 `memory.high`,让它自觉退让)。

> **为什么不直接用 `memory.max` 杀?** 因为很多负载(尤其是"突发性多分配然后释放"的)杀掉太可惜——它本来能自己收敛。`memory.high` 给它一个"慢下来、自我回收"的机会,而不是一棒打死。三道闸(无/high/max)是渐进式的"温柔→强硬"梯度。

## 十一、charge 的批量化:为什么记账也要优化

最后一个细节,但很体现工程功夫:记账(charge)是**每一次分配都发生**的高频操作。如果每次都去改 `page_counter`、检查限额,锁竞争会很重。

### 不这样会怎样

memcg 的 `page_counter` 是树状的、要一路加到根。每次分配都走一遍这个加法 + 限额检查,在多核高频分配下,锁竞争成瓶颈。

### 所以这样设计:per-cpu 批量预记(stock)

memcg 引入 **`MEMCG_CHARGE_BATCH`**:每个 CPU 给每个 memcg 预先"批记"一批(典型 32 页),记在本地一个叫 `stock` 的小库存里。后续分配**先从 stock 扣**,stock 用完了才去走完整的 `try_charge`(再预记一批)。

代码里 `try_charge_memcg` 第一行就是 `consume_stock`([memcontrol.c:2225](../linux-6.14/mm/memcontrol.c#L2225))——绝大多数 charge 命中 stock,**完全不走限额检查、不抢全局锁**。

> **这正是第 7 章 slab "per-cpu 快通道"思想的又一个化身**:把高频的记账操作,用 per-cpu 批量化摊薄锁竞争。代价是:限额统计会有 `MEMCG_CHARGE_BATCH × nr_cpus` 量级的误差(注释 [memcontrol.c:534](../linux-6.14/mm/memcontrol.c#L534) 明说了),但这对"限额"这个用途完全可接受——宁可统计稍糙,也要快。

---

## 关键源码精读:`try_charge_memcg` —— 一个"迷你版"的全局内存管理

本章的三个主题(NUMA、大页、memcg)里,**memcg 的 `try_charge_memcg` 最值得精读**——因为它把整本书前面讲的"快路径 → 回收 → OOM"这套机制,**在一个 cgroup 内部完整复刻了一遍**。读懂它,你就读懂了全局内存管理是怎么被"打包复用"的。

### 四级阶梯对应全书

回顾 [try_charge_memcg](../linux-6.14/mm/memcontrol.c#L2210) 的四级阶梯:

| 阶梯 | 代码 | 对应全书哪章的全局机制 |
|---|---|---|
| ① 本地 stock | `consume_stock(memcg, nr_pages)` | 类似 slab 的 per-cpu 快通道(第 7 章) |
| ② 直接记账 | `page_counter_try_charge(&memcg->memory, ...)` | 类似伙伴系统的快路径(第 6 章) |
| ③ 组内回收 | `try_to_free_mem_cgroup_pages(...)` | 就是第 12 章的回收,但范围限定在本组 |
| ④ 组内 OOM | `mem_cgroup_oom → mem_cgroup_out_of_memory` | 就是第 12 章的 OOM,但只在组内挑进程 |

> **这个对应关系是本章最重要的洞察**:memcg 没有发明任何新机制,它**复用了全书那套"限额 → 回收 → OOM"的内存管理范式**,只是把作用域从"整机"缩小到"一个 cgroup"。一旦你看懂这一点,memcg 就从"复杂的容器机制"变成了"全局内存管理的局部副本",豁然开朗。

### 三道闸的代码归属

| 闸 | 在 `try_charge_memcg` 里怎么触发 |
|---|---|
| `memory.high`(软) | 在**返回用户态时**异步检查(`mem_cgroup_handle_over_high`),不阻塞分配路径,只让进程之后睡 |
| `memory.max`(硬) | 在 `try_charge_memcg` 里**同步**触发:记账失败 → 回收 → 还不行 → `mem_cgroup_oom` |
| 组内 OOM | `mem_cgroup_out_of_memory` 把 `oom_control.memcg` 绑定到本组,OOM killer 只在本组选牺牲品 |

> **`memory.high` 为什么不放在 `try_charge_memcg` 里同步处理?** 因为 high 的本意是"限流而非阻止"——分配本身要成功(否则程序逻辑崩),惩罚是事后的"让你跑慢点"。所以 high 的延迟放在返回用户态的路径上(`mem_cgroup_handle_over_high`),不阻塞关键的分配链路。这是"软/硬"两种限额在代码结构上的根本区别。

---

## 章末小结:三组机制,一个哲学

回到大楼比喻,把这一章三个主题串起来:

> 大楼升级成了**楼群(NUMA)**:每栋楼有自己的小仓,住户优先从本楼取货,跨楼取货要绕远路;资深住户(数据库)可以用 mempolicy 申请"固定在 1 号楼"或"在所有楼间均摊"。为了应付大户人家,物业把**成片的 4KB 标准间打通成 2MB 大套间(大页)**——要么预留整层 VIP 套间(hugetlb,确定性高但独占),要么后台整理工 khugepaged 悄悄把零散房间合并(THP,灵活但可能卡顿)。最后,物业给每户发了**限量消费卡(memcg)**:软上限 high 让超用户自我限流、硬上限 max 在组内 OOM 杀进程——谁爆了自己的卡,只伤自己,不殃及邻居。

### 三个机制背后的同一个哲学

这三个看似无关的机制,其实贯彻了**同一条设计哲学**:

> **当真实世界的约束打破"理想模型"时,内核不是推翻重写,而是在原有机制外面"包一层规则"。**

- NUMA 没改伙伴系统,只是**重排了 zonelist 的顺序**(本地优先);
- 大页没改伙伴系统,只是**允许分配更大的 order**(order≥9)+ 加了折叠/预留两种获取方式;
- memcg 没改伙伴系统/回收/OOM,只是**把同一套机制在每个 cgroup 内部复制了一份**(charge → 组内回收 → 组内 OOM)。

换句话说:**第 6~12 章那套核心机制(伙伴系统、slab、page cache、回收、OOM)是地基,本章三组机制是地基上的"约束层"。** 地基不动,约束层叠加。这正是 Linux 内存管理能在 30 年里从单核 UMA 演进到多核 NUMA + 容器化、却没被推倒重写的根本原因——**架构从一开始就分层、可叠加**。

### 本章在全书主线中的位置

记住导言的二分法:**物理一侧管"切房间",虚拟一侧管"造幻觉"**。本章三个主题分别落在哪一侧?

- **NUMA**:物理侧的拓扑——"房间分布在几栋楼里",改的是物理房间分配时的"先去哪栋楼找";
- **大页**:横跨两侧——物理侧是"更大的连续块"(伙伴系统的 order 更高),虚拟侧是"一个页表项管更大片地址"(TLB 友好)。**它是物理切房间粒度的放大,服务于虚拟侧的查账效率**;
- **memcg**:既不纯物理也不纯虚拟——它是**记账层**,横跨物理页(page cache、匿名页、slab)和虚拟侧(每个 cgroup 的进程),给"用量"加约束。

也就是说:**本章是物理/虚拟两侧之上的"约束与优化层"。** 它们不改变"怎么切房间、怎么造幻觉"的基本逻辑,只是在真实硬件约束(NUMA 距离、TLB 容量)和运维需求(容器隔离)出现后,给基本逻辑加上"远近优先、粒度可选、用量限额"这三组规则。

### 全书收束

这是正文的最后一章。回头看导言那三句话,你现在应该能体会得更深:

1. **物理一侧管"切房间",虚拟一侧管"造幻觉"。** —— 第 1~7 章切房间(e820→memblock→ZONE→伙伴系统→slab),第 8~11 章造幻觉(虚拟内存→page fault→VMA→page cache),第 12 章管"房间不够时怎么办",第 13 章给真实场景加约束。全书一条主线,从未跑题。
2. **"懒"是核心哲学。** —— page fault 兑现、VMA 只圈地、page cache 延迟写回、memcg 的 high 软限流……每个机制都在"用延迟换效率"。
3. **所有复杂机制都是被四个本性逼出来的。** —— NUMA 是"RAM 有物理拓扑"的产物;大页是"RAM 慢(相对 CPU)+ TLB 有限"的产物;memcg 是"RAM 有限 + 要多租户共享"的产物。当你再遇到任何"为什么搞这么复杂"的设计,去找是哪个本性在作怪——这条规律,本章再一次验证了它。

### 想继续深入,该往哪钻

- **NUMA**:精读 [prepare_alloc_pages()](../linux-6.14/mm/page_alloc.c#L4491) 看 zonelist 怎么按节点排序;读 [mm/mempolicy.c](../linux-6.14/mm/mempolicy.c) 的 `policy_node` / `policy_nodemask`,看 BIND/INTERLEAVE/PREFERRED 怎么翻译成 `nid`。工具:`numactl -H`、`numastat`、`/sys/devices/system/node/`。
- **大页**:精读 [mm/huge_memory.c](../linux-6.14/mm/huge_memory.c) 的 `__thp_vma_allowable_orders`(判断哪些 VMA 能用大页)和 khugepaged 的折叠路径;读 [struct hstate](../linux-6.14/include/linux/hugetlb.h#L655) 和 `hugetlb_acct_memory`(预留/账)。工具:`cat /proc/meminfo | grep -i huge`、`/sys/kernel/mm/transparent_hugepage/`、`/proc/sys/vm/nr_hugepages`。
- **memcg**:精读 [try_charge_memcg()](../linux-6.14/mm/memcontrol.c#L2210)(四级阶梯)、[mem_cgroup_out_of_memory()](../linux-6.14/mm/memcontrol.c#L1625)(组内 OOM)、[mem_cgroup_handle_over_high()](../linux-6.14/mm/memcontrol.c#L2117)(high 限流)。工具:`cat /sys/fs/cgroup/<path>/memory.{current,high,max,events}`、` systemd-cgtop`、`memory.pressure`(PSI)。
- **把三章串起来的实验**:在一个容器里 `stress-ng --vm 2 --vm-bytes 8G` 制造内存压力,同时观察 `memory.events`(看到 `oom`/`oom_kill`)和 `memory.pressure`——你能亲眼看到本章讲的"组内回收 → 组内 OOM"全过程。

> 三组约束层讲完了。整本书的正文到此结束。如果你想要一份"把 13 章重新串成一张图、外加源码阅读路线和调试工具速查"的收束,翻到**附录 A · 全景脉络与设计哲学总结** 和 **附录 B · 源码阅读路线与调试工具**。
