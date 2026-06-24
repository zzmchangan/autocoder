# 第 7 章 · slab/slub:小对象的高效复用

> 伙伴系统发的是"整面整面的墙"(4KB 起),可内核天天要的是"一块一块的砖头"(几十字节)。这一章讲的是:内核如何把大墙切成规整的小砖,还让取砖、还砖快到几乎不用上锁。

---

## 章首 · 核心问题

上一章我们盖好了伙伴系统([mm/page_alloc.c](../linux-6.14/mm/page_alloc.c)),它会发"连续的物理页",最小一页 4KB。看起来物理侧的大问题解决了。但请你马上想一个场景:

> 内核里每时每刻都在创建/销毁 `task_struct`(进程描述符,约 9000 字节)、`inode`(文件索引节点,约 600 字节)、`dentry`(目录项)、`file`(打开的文件对象,约 200 字节)、`sk_buff`(网络包)……这些对象**几乎全是几十字节到几 KB 的小东西**。

把这个问题抛给伙伴系统,会发生什么?

- 我要一个 200 字节的 `file` 结构 → 伙伴系统说"我最小发一页,4KB,拿去"。
- 我用完还回来 → 这一整页 4KB 就被标记为空闲,可其实只被用过 200 字节。

这就是本章要解决的困惑:

> **伙伴系统的最小粒度是"页(4KB)",可内核里绝大多数内存需求是"几十字节"。这个 50 倍以上的粒度鸿沟,内核怎么填?填法为什么不是"在伙伴系统上随便改改",而是另起炉灶搞了一整套 slab/slub?**

**读完本章你会明白:**

- 为什么不能拿伙伴系统直接分小对象(三个致命痛点:内部碎片、无构造缓存、全局锁竞争);
- "面向对象的池化"思想:每种高频小物件一个专属工厂(`kmem_cache`);
- 空闲链表为什么"不占额外内存"——空闲对象自己当链表节点;
- per-cpu 快通道为什么能**几乎无锁**(`this_cpu_cmpxchg` + tid 事务号);
- 构造函数(ctor)为什么只跑一次,以及它省下了什么;
- `SLAB_TYPESAFE_BY_RCU` 这个 flag 为什么是为 RCU 量身定做的"延迟回收保险";

一句话概括本章在大楼比喻里的位置:**伙伴系统是大楼里的"砖厂",每次烧一面墙那么大的整砖运过来。slab 是大楼里另一道工序——把整砖按尺寸切成一块块小预制件(`file` 件、`inode` 件……),还预先喷好漆、摆进样板间,谁要直接拿走,还回来也不拆、留着下次复用。**

> **如果一读觉得太难**:先只记住三件事——
> ① **slab 是伙伴系统的上层客户**:它从伙伴系统批发整页,切成等大格子(一种对象一个 `kmem_cache`),摆好家具(ctor)反复复用——这就是它存在的全部理由;
> ② **per-cpu 快通道 + `cmpxchg`/`tid` 让绝大多数分配和释放不抢任何全局锁**,这是它快的根本;
> ③ **空闲对象的"下一个在哪"就藏在它自己体内**(嵌入式 freelist),零额外内存开销,还加密防伪造。
>
> 其余细节(慢路径换货阶梯、`SLAB_TYPESAFE_BY_RCU`、kmalloc 档位)第二遍配合 `/proc/slabinfo` 再抠,不影响理解后续章节。

> **一个名词澄清**:你会在源码和文档里看到 `slab`、`slob`、`slub` 三个名字。它们是 Linux 历史上先后出现的三套"小对象分配器",都实现同一套接口(`kmem_cache_alloc` 等)。`slab` 是最早(IBM,1996,Solaris 风格)、`slob` 给嵌入式小内存、`slub` 是现在主流发行版默认用的(2007,Christoph Lameter)。今天我们讲的就是 **slub**,源码在 [mm/slub.c](../linux-6.14/mm/slub.c),但接口名和历史叫法仍沿用 "slab"。本章这两个词基本混用,你当成同一回事即可。

---

## 一、为什么不能直接用伙伴系统分小对象

### 不这样会怎样

我们老老实实算一笔账,看看"硬用伙伴系统分小对象"会出什么乱子。假设内核要用一个 200 字节的 `file` 结构:

**痛点一:内部碎片(internal fragmentation)把内存浪费到离谱。**

伙伴系统最小发一页 4KB。一个 200 字节的对象占掉一页,**有效利用率 = 200/4096 ≈ 5%**。剩下 95% 的房间空着,谁也用不了(因为这一页已经被标记成"这个对象独占")。

更进一步,内核里这种几十字节的小对象**极多**。粗略算:一台机器上几万个 `dentry`、几千个 `inode`、几百个 `task_struct`、无数个 `sk_buff`。如果每个都独占一页,16GB 的内存可能一眨眼就被这些"5% 利用率的页"吃光。这就是**内部碎片**——分配出去的房间内部大量闲置。

**痛点二:没有"构造缓存",每次都得从头初始化。**

很多内核对象的初始化不便宜。`inode` 要初始化各种锁、链表头、时间戳;`task_struct` 更是一大堆字段。如果每次分配都从一页空内存开始,每次都得**从头跑一遍构造函数**——这些字段一个个清零、一个个初始化,代价不小。可是这些对象常常是"用完→还→再要一个→还→再要",**内容根本不需要每次重来**。

**痛点三:全局锁竞争。**

伙伴系统的核心分配路径(`__alloc_pages`)是全局的,有 zone 锁。如果每个 200 字节的小对象分配都去抢 zone 锁,在多核机器上,核一多,锁就成了吞吐量天花板。小对象分配是内核里**最频繁的操作之一**(每收一个网络包就 alloc 一堆),绝不能每次都走全局锁。

### 所以这样设计:另起炉灶

三个痛点合起来,逼出了一个结论:**小对象分配必须单独一层**,而且这一层的设计目标和伙伴系统完全不同:

| | 伙伴系统 | slab/slub |
|---|---|---|
| 粒度 | 页(4KB) | 几十字节 |
| 关心 | 物理连续、抗碎片 | **类型、复用、无锁快** |
| 来源 | 直接管物理页 | **从伙伴系统批发整页,再切成小件** |
| 寿命 | 长期、通用 | 短期、高频反复 |

也就是说,slab 不是伙伴系统的替代,而是它的**上层客户**:slab 先从伙伴系统批发几页整墙,然后在墙里**切出固定大小的小格子**,摆好家具(预初始化),谁来要就给一个现成的。

> **比喻**:伙伴系统是"砖厂",只卖整面墙那么大的大砖。slab 是"预制件车间":它从砖厂批几面大砖进来,切成一块块标准的 `file` 件、`inode` 件,每块件都已经喷好漆、装好螺丝(构造函数预初始化),整齐码在样板间里。车间要发货,直接拿一块现成的走;客户退货,把件**原样放回样板间**(不拆漆、不卸螺丝),下个客户直接复用。

---

## 二、面向对象的池化:`kmem_cache` 这个"样板间工厂"

### 核心数据结构:一种对象,一个 `kmem_cache`

slab 的设计灵魂是这句话:**每种高频小对象,配一个专属的池。**

这些池在内核里叫 `kmem_cache`。比如,内核启动时会建一个专门生产 `task_struct` 的 `kmem_cache`,一个专门生产 `inode` 的,一个专门生产 `dentry` 的……它们各管各的,互不干扰。

```c
struct kmem_cache {
#ifndef CONFIG_SLUB_TINY
    struct kmem_cache_cpu __percpu *cpu_slab;
#endif
    slab_flags_t flags;
    unsigned long min_partial;
    unsigned int size;           /* Object size including metadata */
    unsigned int object_size;    /* Object size without metadata */
    unsigned int offset;         /* Free pointer offset */
    struct kmem_cache_order_objects oo;
    void (*ctor)(void *object);  /* Object constructor */
    const char *name;            /* Name (only for display!) */
    struct list_head list;       /* List of slab caches */
    struct kmem_cache_node *node[MAX_NUMNODES];
};
```

([mm/slab.h:258-315](../linux-6.14/mm/slab.h#L258-L315),**以下为简化示意**:仅摘录与本章讲解相关的字段、保留源码原注释;省略了 `red_left_pad`、`random`、`useroffset`、调试与 NUMA 相关字段)

几个关键字段先认一下,后面会用到:

- **`cpu_slab`** 是 `__percpu` 的——意思是**每个 CPU 各有一份独立副本**。这就是"快通道",下一节详讲。
- **`size` vs `object_size`**:`object_size` 是用户要的纯大小(比如 200),`size` 是含元数据(空闲指针、调试 redzone)的实际格子大小。slab 按 `size` 切格子。
- **`ctor`** 是构造函数指针,第六节讲。
- **`node[]`** 每个 NUMA 节点一个,里面挂的是"部分满的 slab"(partial 列表)。

### "一张 slab"长什么样

一个 `kmem_cache` 管着很多**张 slab**。一张 slab,本质上就是**从伙伴系统批来的 1 页或几页连续内存**,被切成 N 个等大的格子,每个格子放一个对象。

> **小白补一句**:这里的 "slab" 是个**实体名词**(一张装满格子的内存页),而 "slab allocator" 是整套机制的名字,别搞混。源码里一张 slab 的描述符是 `struct slab`(由底层 `struct folio`/`struct page` 复用而来)。

一张刚从伙伴系统批来的 slab,初始化过程在 [allocate_slab()](../linux-6.14/mm/slub.c#L2566):

```c
static struct slab *allocate_slab(struct kmem_cache *s, gfp_t flags, int node)
{
    ...
    slab = alloc_slab_page(alloc_gfp, node, oo);   /* ① 找伙伴系统批发几页 */
    ...
    slab->objects = oo_objects(oo);                 /* ② 这张 slab 能切几个格子 */
    slab->inuse = 0;                                /*    目前用了 0 个 */
    slab->frozen = 0;
    ...
    start = slab_address(slab);
    ...
    shuffle = shuffle_freelist(s, slab);            /* ③ 可选:打乱格子顺序(安全) */

    if (!shuffle) {
        start = fixup_red_left(s, start);
        start = setup_object(s, start);             /* ④ 对每个格子跑构造函数 */
        slab->freelist = start;                     /*    串成空闲链表 */
        for (idx = 0, p = start; idx < slab->objects - 1; idx++) {
            next = p + s->size;
            next = setup_object(s, next);
            set_freepointer(s, p, next);            /* ⑤ p 的下一个 = next */
            p = next;
        }
        set_freepointer(s, p, NULL);                /*    最后一个的下一个 = NULL */
    }
    return slab;
}
```

([mm/slub.c:2566-2631](../linux-6.14/mm/slub.c#L2566-L2631))

这段代码干的事,正是"把一面大砖切成格子并串成链表"(其中 `① ② ③ ④ ⑤` 为本书所加的讲解标注,源码原文无):

- ① `alloc_slab_page` 找伙伴系统批发(`alloc_frozen_pages`),批发量由 `oo` 决定(order + objects,即"几页、切几个");
- ④⑤ 从 `start` 开始,每隔 `s->size` 字节就是一个格子,用 `set_freepointer` 把它们一个个**串成一条空闲链表**,链表头记在 `slab->freelist`。

> **为什么要 `shuffle`(打乱顺序)?** 这是安全特性 `CONFIG_SLAB_FREELIST_RANDOM`。如果不打乱,格子的分配顺序永远是从前往后固定顺序,攻击者能预测"下一个分到的对象挨着谁",这对某些堆利用攻击(KASLR 绕过、堆喷)是帮助。打乱顺序,让分配序列不可预测。**一个细节,但体现了"安全性"也是 slab 的设计目标之一。**

到这一步,一张装满可用格子、串好空闲链表的 slab 就准备好了,挂到 `kmem_cache` 名下,等着被 CPU 的快通道取用。

---

## 三、空闲链表的妙处:空闲对象自己当链表节点

slab 最省钱的一个设计,是它的**空闲链表不占任何额外内存**。我们仔细看一眼。

### 不这样会怎样

如果一张 slab 有 100 个空闲格子,你想知道"下一个该分谁",最朴素的做法是:另外维护一个数组或链表,记录"哪些格子是空的"。可这就要**额外**占内存——又回到浪费老路上去了。

### 所以这样设计:把"下一个的地址"藏在空闲格子里

slab 的洞察是:**一个格子空闲时,它的内存没人用,那这块内存正好可以拿来存"下一个空闲格子的地址"。**

具体说,每个对象内部有一个固定位置(`s->offset` 字节处),叫**freepointer(空闲指针)**:

- 当这个对象**空闲**时,这个位置存的是"下一个空闲对象的地址";
- 当这个对象**被分配出去**时,这块内存归用户用,空闲指针的位置被用户数据覆盖——但无所谓,因为分配出去的对象已经不在空闲链表里了,不需要"下一个"指针。

读 freepointer 的代码:

```c
static inline void *get_freepointer(struct kmem_cache *s, void *object)
{
    unsigned long ptr_addr;
    freeptr_t p;

    object = kasan_reset_tag(object);
    ptr_addr = (unsigned long)object + s->offset;   /* 空闲指针就藏在 object+offset 处 */
    p = *(freeptr_t *)(ptr_addr);
    return freelist_ptr_decode(s, p, ptr_addr);
}
```

([mm/slub.c:497-506](../linux-6.14/mm/slub.c#L497-L506))

写 freepointer:

```c
static inline void set_freepointer(struct kmem_cache *s, void *object, void *fp)
{
    unsigned long freeptr_addr = (unsigned long)object + s->offset;

#ifdef CONFIG_SLAB_FREELIST_HARDENED
    BUG_ON(object == fp);  /* 朴素的双释放/损坏检测 */
#endif
    ...
    *(freeptr_t *)freeptr_addr = freelist_ptr_encode(s, fp, freeptr_addr);
}
```

([mm/slub.c:540-550](../linux-6.14/mm/slub.c#L540-L550))

注意 `freelist_ptr_encode`/`freelist_ptr_decode`——这俩不是简单存指针,而是**加密**的。看 decode:

```c
decoded = (void *)(ptr.v ^ s->random ^ swab(ptr_addr));
```

([mm/slub.c:490](../linux-6.14/mm/slub.c#L490),`CONFIG_SLAB_FREELIST_HARDENED` 开启时)

空闲指针被存成"真实地址 XOR 一个随机数 XOR(字节序反转后的自身地址)"。为什么要这么麻烦?

> **不这样会怎样**:如果空闲指针明文存放,攻击者一旦能往某个对象里写任意值,就能**伪造一个空闲指针**,让 slab 把"下一个空闲对象"指向任意内核地址——这就是经典的"slab freelist 劫持"提权手法。把指针加密(而且掺进自身地址,让每个位置的密钥都不同),伪造难度就上去了。这就是 `CONFIG_SLAB_FREELIST_HARDENED` 的用意——`s->random` 是每个 cache 一个的随机密钥,启动时生成。

> **比喻**:样板间里每块空闲砖头上贴了张纸条,写着"下一块空闲砖在 X 号架位"。纸条的字是密码写的,密码每个车间不同、还跟贴纸的位置有关——外人就算拿到一块砖,也伪造不出能用的"下一块"地址。

这条"空闲对象自己当链表节点"的链,是后面快路径能跑起来的基础。

---

## 四、per-cpu 快通道:为什么几乎无锁

这是 slab 最让内核性能工程师得意的设计。理解了它,你就理解了"为什么 slab 分配在多核上极快"。

### 不这样会怎样

一张 slab 是多个 CPU 共享的。如果每个 CPU 分配时都要去改这张 slab 的空闲链表头(`slab->freelist`),那必须加锁。在 64 核机器上,每分一个小对象都抢这把锁,锁等待会拖垮吞吐。

### 所以这样设计:每个 CPU 独占一张 slab

slab 的解法是**"预分配一张 slab 给每个 CPU 独占"**。这就是 `kmem_cache_cpu`(`cpu_slab` 指向它):

```c
struct kmem_cache_cpu {
    union {
        struct {
            void **freelist;       /* 本 CPU 专用空闲链表头 */
            unsigned long tid;     /* 事务号 */
        };
        freelist_aba_t freelist_tid;
    };
    struct slab *slab;             /* 本 CPU 当前独占的那张 slab */
#ifdef CONFIG_SLUB_CPU_PARTIAL
    struct slab *partial;          /* 本 CPU 的备用半满 slab 链 */
#endif
    local_lock_t lock;
};
```

([mm/slub.c:382-398](../linux-6.14/mm/slub.c#L382-L398))

每个 CPU 各有一份 `freelist` 和 `slab`。分配时,CPU 只从**自己那份**取,根本不碰别人的——天然无需跨核锁。

但这有个技术难点:**怎么在"不加锁"的前提下,保证"读 freelist → 改 freelist"这个动作是原子的、不会被抢占/迁移打断?** 这就是 `tid`(事务号)和 `cmpxchg` 的戏份。

### 快路径:一次 `cmpxchg` 搞定

分配的快路径入口是 `kmem_cache_alloc`([slub.c:4169](../linux-6.14/mm/slub.c#L4169)),它层层下钻到 `__slab_alloc_node`([slub.c:3923](../linux-6.14/mm/slub.c#L3923))。快路径的核心就这几步:

```c
c = raw_cpu_ptr(s->cpu_slab);     /* 取本 CPU 的快通道 */
tid = READ_ONCE(c->tid);          /* ① 记下事务号 */
barrier();

object = c->freelist;             /* ② 读当前空闲链表头 */
slab = c->slab;
...
if (!USE_LOCKLESS_FAST_PATH() ||
    unlikely(!object || !slab || !node_match(slab, node))) {
    object = __slab_alloc(...);   /* 条件不满足,走慢路径 */
} else {
    void *next_object = get_freepointer_safe(s, object);

    /* ③ cmpxchg:原子地"如果 tid 和 freelist 都没变,就把 freelist 改成 next" */
    if (unlikely(!__update_cpu_freelist_fast(s, object, next_object, tid))) {
        note_cmpxchg_failure("slab_alloc", s, tid);
        goto redo;                /* 被人改过,重做 */
    }
}
```

([mm/slub.c:3944-4012](../linux-6.14/mm/slub.c#L3944-L4012),精简)

这段无锁算法的精妙之处(`① ② ③ ④ ⑤` 为本书所加的讲解标注,源码原文无):

1. **`tid` 事务号**:每个 CPU 一个、每次操作递增。它的作用是"检测在我操作期间,有没有别人(其实是被抢占后切到别的 CPU,或中断/抢占改了我的快通道)动过我的 freelist"。
2. **读 freelist 前先读 tid,操作后用 `cmpxchg` 同时校验 tid + 改 freelist**:如果这两步之间 `tid` 变了(说明本 CPU 的快通道被别的上下文动过),`cmpxchg` 失败,`goto redo` 重来。**保证"读-改"是一个原子事务,但不靠锁,靠乐观重试。**

> **为什么这是"几乎无锁"而不是"完全无锁"?** 因为绝大多数情况下 `cmpxchg` 第一次就成功,不进慢路径、不抢 `local_lock`,所以叫"快路径"。统计枚举里专门有计数器:

```c
enum stat_item {
    ALLOC_FASTPATH,   /* 从 cpu slab 分配 */
    ALLOC_SLOWPATH,   /* 需要新换一张 cpu slab */
    FREE_FASTPATH,    /* 还到 cpu slab */
    FREE_SLOWPATH,
    ...
};
```

([mm/slub.c:347-375](../linux-6.14/mm/slub.c#L347-L375))

> **为什么独占 slab 要标 `frozen`(冻结)?** 你会在快路径里看到 `VM_BUG_ON(!c->slab->frozen)`([slub.c:3733](../linux-6.14/mm/slub.c#L3733))。"冻结"的意思是"这张 slab 被某个 CPU 独占了,别人不能从它这里偷对象、也不能把它还回节点 partial 列表"。这是个免锁标记:别人看到 frozen,就绕道走自己的快通道,不碰这张。**用标记换锁,是并发的常见套路。**

### 慢路径:快通道空了怎么办

当本 CPU 的 freelist 空了(`!object`),走 `__slab_alloc` → `___slab_alloc`([slub.c:3656](../linux-6.14/mm/slub.c#L3656))。慢路径做的事是个"换货"过程:

1. 当前独占 slab 用完了 → 把它(如果还有部分空闲)挂回节点的 partial 列表,或者整张还掉;
2. 先看本 CPU 的 `partial` 链有没有半满 slab → 有就拿来当新的独占 slab;
3. 本 CPU partial 也空了 → 去节点 `kmem_cache_node` 的 partial 列表拿一张;
4. 节点也空了 → `new_slab`,真正找伙伴系统批发一张全新的(见第二节 `allocate_slab`)。

这条链从快到慢:**自己的 freelist(无锁)→ 自己的 partial → 节点 partial → 批发新页**。越靠后越贵,所以设计上拼命把"热的"留在最前面、最便宜的那级。

---

## 五、构造函数只跑一次:复用换来的优化

回看第一节的"痛点二":每次从头初始化对象很贵。slab 怎么解?

### 不这样会怎样

如果一个 `inode` 对象每次分配都要把所有字段清零、所有链表头初始化、所有自旋锁初始化,这开销在小对象高频分配里会非常刺眼。可这些初始化**对一个还没用过的对象才必要**——对象如果是"用完还回来、原样复用"的,它里面的字段状态基本是好的,**不必重跑构造**。

### 所以这样设计:ctor 只在"出生"时跑一次

`kmem_cache` 有个 `ctor` 字段(构造函数指针)。它**只在一张 slab 第一次被切格子时,对每个格子各跑一次**——之后就再也不跑了:

```c
static void *setup_object(struct kmem_cache *s, void *object)
{
    setup_object_debug(s, object);
    object = kasan_init_slab_obj(s, object);
    if (unlikely(s->ctor)) {
        kasan_unpoison_new_object(s, object);
        s->ctor(object);                /* 构造函数:在这里跑,且只在新建 slab 时 */
        kasan_poison_new_object(s, object);
    }
    return object;
}
```

([mm/slub.c:2400-2410](../linux-6.14/mm/slub.c#L2400-L2410))

而 `setup_object` 只在 `allocate_slab`(新建 slab)里被调用,见 [slub.c:2619-2623](../linux-6.14/mm/slub.c#L2619)。也就是说:

> **一张 slab 切出来的格子,每个 ctor 跑过一次,从此就一直是"构造好的"状态。对象被分配出去、用完、还回来,ctor 不再跑——还回来的对象带着它上次的状态,下次直接复用。**

这是个很妙的复用:构造成本被**摊薄到这张 slab 的整个生命周期**。如果一张 slab 切了 20 个 `inode`、被反复分配释放上千次,那 ctor 只跑了 20 次,而不是上千次。

> **细节**:`allocate_slab` 里有一句 `WARN_ON_ONCE(s->ctor && (flags & __GFP_ZERO))`([slub.c:2638](../linux-6.14/mm/slub.c#L2638))。意思是"有构造函数的 cache,不允许要求清零分配"——因为清零会把 ctor 的成果抹掉,自相矛盾。这个 WARN 暴露了 ctor 设计的内在约束。

> **比喻**:样板间的预制件,出厂时机器统一喷好漆、装好螺丝(ctor,出厂一次)。客户买走、用脏了、退回来,车间**不重新喷漆**,只是把它放回货架。下一个客户拿到的就是"能用、但带着上一个客户痕迹"的件。对于内核对象来说,这点"痕迹"无所谓——反正用户拿到后会自己重写关键字段。

---

## 六、`SLAB_TYPESAFE_BY_RCU`:为 RCU 量身定做的"延迟回收保险"

这一节讲一个看起来古怪、但深刻体现"为什么"的 flag。

### 不这样会怎样

内核里有一种并发模式叫 **RCU(Read-Copy-Update)**:读者不加锁、极快;写者把旧数据"标记为可回收",然后**等所有现存的读者都退出后**,才真正释放旧数据。

问题来了:很多 RCU 保护的对象(比如 `struct file`、某些 dentry)是 slab 分配的。RCU 的回收时机是"所有读者退出后",可 slab 的工作方式是"对象一还回来,可能马上就被另一个 CPU 重新分配出去用了"。于是出现一个冲突:

> **RCU 说"这块内存我现在还不能动,可能有读者还在读"**;**slab 说"这块内存我刚回收,马上要分给别人"**。如果 slab 真的分给别人并写了新内容,RCU 的老读者就**读到一半被换了内容**——典型的 use-after-free 症状。

### 所以这样设计:用 RCU 延迟整张 slab 的回收

slab 提供了 `SLAB_TYPESAFE_BY_RCU` 这个 flag。开了它之后,**slab 释放整张 slab 页给伙伴系统时,走 RCU 延迟**:

```c
static void free_slab(struct kmem_cache *s, struct slab *slab)
{
    ...
    if (unlikely(s->flags & SLAB_TYPESAFE_BY_RCU))
        call_rcu(&slab->rcu_head, rcu_free_slab);   /* 等 RCU 宽限期过后才真释放 */
    else
        __free_slab(s, slab);
}
```

([mm/slub.c:2665-2679](../linux-6.14/mm/slub.c#L2665-L2679))

`call_rcu` 的含义是:"登记一个回调,等当前所有读者都退出(RCU 宽限期 grace period 过后),再调 `rcu_free_slab` 真正释放"。这样,即使 slab 内部把对象还回 freelist、甚至整张 slab 要被丢弃,**真正的页释放也被 RCU 兜底延迟**,保证了"宽限期内,这块物理内存的内容不会被破坏"。

> **一个关键区分**:`SLAB_TYPESAFE_BY_RCU` 保证的是**整张 slab 页级别**的回收安全(页不会被伙伴系统马上复用),**不**保证"对象还回来后内容不变"。也就是说,RCU 的老读者拿到的指针,在宽限期内指向的那块内存**还在、还能读**,但具体内容可能已被 slab 内部复用了。所以使用这个 flag 的代码,必须靠 RCU 的常规手段(如 `rcu_dereference`)拿到指针、并在宽限期内只做"类型安全"的访问(读到的可能是个新对象,但至少是个同类型对象、不会是别的内核结构)。这就是名字 **TYPESAFE**(类型安全)的来历。

> **为什么叫"为 RCU 量身定做"?** 因为它精确地满足 RCU 的需要("释放要延迟到宽限期后"),又不多不少:不延迟对象级复用(那会浪费),只延迟页级回收(那才卡在 RCU 的痛点上)。这种"刚好"的权衡,是深度理解 RCU 之后才能做出的设计。

---

## 七、kmalloc:通用缓存,按大小分档

到这里你可能会问:内核里很多地方只是想"给我几百字节临时用用",并不关心是什么类型。难道每次都要先 `kmem_cache_create` 建个专属 cache?

当然不。内核提供 `kmalloc(size, flags)` 这个通用接口——它背后是一组**按大小预先建好的通用 `kmem_cache`**。

### 不这样会怎样

如果 `kmalloc` 也临时建 cache,那到处都是 cache,管理爆炸。如果 `kmalloc` 每次都找最接近 size 的那个固定档——这就是答案。

### 所以这样设计:固定档位 + 向上取整

内核启动时预先建了一组 `kmalloc-<size>` cache,比如 `kmalloc-64`、`kmalloc-128`、`kmalloc-192`、`kmalloc-256`……一直到大档位。`kmalloc(150)` 的请求,会查表找到最小的"≥150 的档"(这里是 `kmalloc-192`),从它里面分一个 192 字节的格子给你。

> **为什么有 `kmalloc-192` 这种"奇怪"档位?** 因为页面利用率。4KB 一页,切成 64 字节正好 64 个、切 128 字节正好 32 个——整数倍,无浪费。但 192 字节一页能切 21 个还剩点尾巴。引入 192 这一档,是为了覆盖"128~192 字节"这个常见区间,避免 200 字节的需求被迫跳到 256 档浪费一半。档位是**精心挑过的**,在"档位数量"和"内部碎片"之间取平衡。

> **超过最大档位怎么办?** 大对象直接走伙伴系统。看 [slub.c:4229 `___kmalloc_large_node`](../linux-6.14/mm/slub.c#L4229)——它注释写得直白:"为了省事,大块请求直接透传给 page allocator"。这就是"slab 只管小的,大的还回去找伙伴系统"的清晰分工。

你可以亲眼看到这些档位:

```bash
cat /proc/slabinfo | grep kmalloc
# 或
cat /proc/slabinfo          # 每行就是一个 kmem_cache
```

---

## 关键源码精读:分配快路径与释放快路径

本章最值得逐行品的两段代码,正好是一对:**分配快路径**和**释放快路径**。它们一起撑起了 slab 的性能。

### 1. 分配:`slab_alloc_node` → `__slab_alloc_node`

最外层入口:

```c
void *kmem_cache_alloc_noprof(struct kmem_cache *s, gfp_t gfpflags)
{
    void *ret = slab_alloc_node(s, NULL, gfpflags, NUMA_NO_NODE, _RET_IP_,
                                s->object_size);
    ...
    return ret;
}
```

([mm/slub.c:4169-4178](../linux-6.14/mm/slub.c#L4169-L4178))

`s->object_size` 是这个 cache 里对象的"真实大小",传进去是为了在 `kzalloc` 等场景下只清零真正用到的字节数(优化)。`slab_alloc_node` 是个壳([slub.c:4138-4167](../linux-6.14/mm/slub.c#L4138-L4167)),干的是 kfence(检测工具)插桩、KASAN 钩子、分配后清零等收尾,真正的分配逻辑在 `__slab_alloc_node`。我们重看一遍快路径核心(**简化示意**:省略了 NUMA/strict_numa 分支,`①`~`⑤` 为本书所加的讲解标注,源码原文无):

```c
redo:
    c = raw_cpu_ptr(s->cpu_slab);     /* ① 拿本 CPU 快通道 */
    tid = READ_ONCE(c->tid);          /* ② 事务号 */
    barrier();
    object = c->freelist;             /* ③ 读空闲链表头 */
    slab = c->slab;

    if (!USE_LOCKLESS_FAST_PATH() ||
        unlikely(!object || !slab || !node_match(slab, node))) {
        object = __slab_alloc(...);   /*    不满足快路径条件 → 慢路径 */
    } else {
        void *next_object = get_freepointer_safe(s, object);  /* ④ 算下一个 */
        if (unlikely(!__update_cpu_freelist_fast(s, object, next_object, tid))) {
            goto redo;                /* ⑤ cmpxchg 失败 → 重做 */
        }
    }
```

对应的设计:①本 CPU 独占(免跨核锁)②③④⑤乐观的无锁事务。**绝大多数分配,这条路径只执行几条指令,一次 `cmpxchg` 成功就返回**——这就是 slab 快的根本原因。

慢路径 `___slab_alloc`([slub.c:3656](../linux-6.14/mm/slub.c#L3656))负责"换货":当前 slab 空了,从 partial 链拿一张,或新建一张。它的入口判断很完整:

```c
slab = READ_ONCE(c->slab);
if (!slab) {
    goto new_slab;             /* 本 CPU 连独占 slab 都没有 */
}
if (unlikely(!node_match(slab, node))) {
    goto deactivate_slab;      /* NUMA 节点不对,弃用当前 slab */
}
...
freelist = c->freelist;
if (freelist)
    goto load_freelist;        /* 快速路:freelist 还有货,直接用 */

freelist = get_freelist(s, slab);  /* 当前 slab 的 freelist 空,从整张 slab 再要 */
```

([mm/slub.c:3669-3722](../linux-6.14/mm/slub.c#L3669-L3722))

每一句都对应"快路径走不通时的某一种情况",逻辑非常干净。

### 2. 释放:`do_slab_free`

释放的快路径是对称的,而且**更短**——这是 slab 的另一个特点:释放比分配还快,因为释放总是往自己的快通道里塞,几乎不查 partial:

```c
static __always_inline void do_slab_free(struct kmem_cache *s,
                struct slab *slab, void *head, void *tail, int cnt, unsigned long addr)
{
    struct kmem_cache_cpu *c;
    unsigned long tid;
    void **freelist;

redo:
    c = raw_cpu_ptr(s->cpu_slab);
    tid = READ_ONCE(c->tid);
    barrier();

    if (unlikely(slab != c->slab)) {
        __slab_free(s, slab, head, tail, cnt, addr);
        return;
    }

    if (USE_LOCKLESS_FAST_PATH()) {
        freelist = READ_ONCE(c->freelist);
        set_freepointer(s, tail, freelist);

        if (unlikely(!__update_cpu_freelist_fast(s, freelist, head, tid))) {
            goto redo;
        }
    } else { /* ... 本地锁分支,此处略 ... */ }
    stat_add(s, FREE_FASTPATH, cnt);
}
```

([mm/slub.c:4539-4592](../linux-6.14/mm/slub.c#L4539-L4592),**简化示意**:省略了 `else` 锁分支的实现,其余为源码原文;下面用 `① ② ③` 标注讲解)

释放的精髓在 ②:`set_freepointer(s, tail, freelist)` 把被释放对象的"下一个空闲"指向原来的 freelist 头,然后 ③ 用 cmpxchg 把 freelist 头改成被释放对象。**等价于"把对象插到空闲链表最前面",整个操作一次 cmpxchg。**

> **为什么释放有 `slab != c->slab` 的慢路径?** 因为一个对象可能是在别的 CPU 上分配的,本 CPU 想还,但本 CPU 当前独占的 slab 不是它。这时候不能往自己 freelist 塞(会破坏 slab 的 inuse 计数和归属),要走 `__slab_free` 慢路径,正确处理"远程释放"。这个判断虽然简单,却是正确性的关键。

### 3. 这对快路径解决了什么

回头看第一节的三个痛点,这对快路径逐一化解:

- **内部碎片** → slab 在页内切成精确格子,一页塞满 N 个对象,利用率高;
- **无构造缓存** → ctor 只跑一次,还回来的对象直接复用;
- **全局锁竞争** → per-cpu 快通道 + cmpxchg,绝大多数分配/释放**不抢任何全局锁**。

这三条加起来,就是为什么 slab 能在每秒百万次小对象分配的压力下还跑得飞快。

---

## 章末小结

回到大楼比喻:

> 伙伴系统这个"砖厂"只卖整面墙大的砖(页)。可大楼里到处要用小预制件——`file` 件、`inode` 件、`task_struct` 件。于是有了 slab 这道"预制件车间":它从砖厂批发整墙进来,按类型切成等大格子(一种对象一个 `kmem_cache`),出厂时喷好漆装好螺丝(ctor 构造函数,只跑一次)。每个 CPU 有自己的样板间(per-cpu 快通道),要货直接从样板间拿一块,还货直接插回样板间,不用排队、几乎不抢锁(`cmpxchg` + tid)。空闲的预制件自己充当"下一个在哪"的标签(嵌入式空闲链表),连额外的账本都不用开。要安全,标签还加密(`FREELIST_HARDENED`)、出厂顺序还打乱(`FREELIST_RANDOM`)。对象用完退回来不拆漆(复用),省下了一轮又一轮的初始化。

### 本章在全书主线中的位置

记住导言的二分法:**物理一侧管"切房间",虚拟一侧管"造幻觉"**。本章在哪一侧?

**物理侧,且是物理侧"切房间"的最后一道精修。** 伙伴系统把物理内存切成"页级"的块,slab 在页级之上再切一层"对象级"的小格子。它是物理资源被切到最细、最贴近"单个内核数据结构"的那一级:

- 往**下**看,slab 是伙伴系统的客户(每张 slab 都是从伙伴系统批来的几页);
- 往**上看**,slab 分配出来的对象,正是后面虚拟侧(page fault、VMA、page cache)要操作的"实物"——`task_struct` 描述进程、`vm_area_struct` 描述 VMA、`inode` 描述文件、`folio`/`page` 描述物理页本身……这些结构体本身就是 slab 分配的。

也就是说:**slab 既造物理侧的实物,又造虚拟侧记账用的账本。** 它是连接两侧的"构件工厂"。没有它,伙伴系统太粗(只能发页),虚拟侧的对象就无处安放;没有它,内核里海量的数据结构就无从分配。

### 五个"为什么"清单

1. **为什么不直接用伙伴系统分小对象**:内部碎片(5% 利用率)、无构造缓存(反复初始化)、全局锁(多核竞争)——三个痛点,逼出独立一层。
2. **为什么空闲链表不占额外内存**:空闲对象的内存正好用来存"下一个空闲对象的地址",嵌入式 freelist,零额外开销(还加密防伪造)。
3. **为什么 per-cpu 快通道几乎无锁**:每个 CPU 独占一张 slab,只从自己 freelist 取,用 `cmpxchg + tid` 做"读-改"的乐观原子事务,绝大多数路径不抢全局锁。
4. **为什么 ctor 只跑一次**:对象出生时构造,之后反复复用,把构造成本摊薄到整张 slab 的生命周期,省掉海量重复初始化。
5. **为什么有 `SLAB_TYPESAFE_BY_RCU`**:RCU 释放要求"延迟到宽限期后",slab 用 `call_rcu` 把页级回收延迟,刚好满足 RCU 的类型安全需求——不多不少。

### 想继续深入,该往哪钻

- **看一张 slab 怎么出生**:精读 [allocate_slab()](../linux-6.14/mm/slub.c#L2566) → `alloc_slab_page` → `setup_object`,把"批发页→切格子→跑 ctor→串 freelist"这条链走一遍。
- **看快路径的并发正确性**:精读 [__slab_alloc_node](../linux-6.14/mm/slub.c#L3923) 和 [do_slab_free](../linux-6.14/mm/slub.c#L4539),特别注意 `tid`、`barrier()`、`cmpxchg` 三者怎么配合。注释里关于"为什么先读 tid 再读 freelist"的解释([slub.c:3947-3955](../linux-6.14/mm/slub.c#L3947))非常精彩。
- **看慢路径的"换货"逻辑**:精读 [___slab_alloc](../linux-6.14/mm/slub.c#L3656),跟着 `new_slab` → partial → node partial → `new_slab` 这条递进的代价阶梯。
- **看 slab 的可观测面**:`cat /proc/slabinfo`、`cat /sys/kernel/slab/<name>/...`(每个 cache 一个目录,能看到 object_size、order、partial 数等)。配合 `/proc/meminfo` 里的 `Slab:` 一行,看 slab 总共吃了多少内存。
- **看 slab 与回收的接口**:跳到第 12 章,看 `kmem_cache_shrink` 和 slab 对象如何参与回收收缩。

> 预制件车间运转起来了,大楼里要什么小构件都能秒出。可到目前为止,我们说的全是"物理那一侧怎么切房间"。住户(进程)登场后,它看到的根本不是这些物理房间,而是一个"独占整栋楼"的虚拟幻象。这个幻象是怎么造的?——翻到**第 8 章 · 虚拟内存与多级页表:为什么要"骗"进程**。
