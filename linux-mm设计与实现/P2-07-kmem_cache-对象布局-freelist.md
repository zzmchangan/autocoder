# 第七章 · kmem_cache、对象布局、freelist

> 篇:第 2 篇 · slab/slub(分配·小对象)
> 主线呼应:第 1 篇讲完 buddy——它把物理页(4KB)整页整页地分出去。但内核自己到处要的是**小对象**:`task_struct`、`inode`、`dentry`、`struct file`……几十到几百字节。要 64 字节给一整页 4KB,内部碎片 99%。本章打开 slab 分配器(更准确地说是 **SLUB**,Linux 6.9 的默认实现)怎么解决这件事:为每种固定大小建一个 `kmem_cache` 对象池,在 buddy 给的(若干)页上紧凑摆满同型号对象,空闲对象用一根 **freelist** 串起来,分配/释放只是摘/挂链头,O(1) 且不碎。这是 mm 把内存**分出去**的第二层,站在 buddy 给的页之上。

## 核心问题

**内核到处要分配几十到几百字节的小对象(task_struct、inode……),buddy 只会按页(4KB)给,直接用太浪费(64 字节占 4KB,内部碎片 99%)——怎么办?**

读完本章你会明白:

1. **slab 的核心抽象 `kmem_cache`** —— 一个"专门装某种固定大小对象"的池子,它怎么由 `kmem_cache_create` 创建、字段怎么决定对象布局。
2. **一个 slab(若干页)里怎么紧凑摆满同型号对象** —— `size`、`inuse`、`offset`、`align` 这些字段分别管什么、`calculate_sizes` 怎么把它们算出来。
3. **空闲对象怎么用一根内嵌 freelist 串起来** —— freelist 指针藏在每个空闲对象体内(不额外占空间),分配=摘头、释放=挂头,O(1)。
4. **freelist 指针为什么还要混淆**(`CONFIG_SLAB_FREELIST_HARDENED`)—— `ptr ^ random ^ swab(ptr_addr)` 防堆溢出攻击。
5. **`oo` 字段怎么一个 `unsigned int` 同时编码 order 和对象数** —— `(order << 16) + objects`。

> **逃生阀**:如果你对"对象池""freelist"这些词感到陌生,别慌。本章会先用一个最朴素的问题("要 64 字节给一整页会怎样")逼出 slab 的设计,再一步步落到 `kmem_cache_create`→`calculate_sizes`→`allocate_slab` 的源码。`set_freepointer` 那行 `ptr ^ random ^ swab(ptr_addr)` 是本章的"aha 时刻",我们会把它拆到你能背下来。

---

## 7.1 一个被忽视的事实:内核自己是个"小对象大户"

第 1 篇讲完 buddy,你也许觉得分配的事基本搞定了——`alloc_pages` 要几页给几页,够用了。但仔细想想内核日常要分配的东西:

- 创建一个进程,要一个 `struct task_struct`(x86_64 上约 9500 字节)。
- 打开一个文件,要一个 `struct inode`(约 600 字节)、一个 `struct file`(约 256 字节)。
- 走一层目录,要一个 `struct dentry`(约 192 字节)。
- 收一个网络包,要一个 `sk_buff`(约 240 字节)。
- 内核模块到处 `kmalloc(64)`、`kmalloc(128)` 拿小内存。

**这些对象有几个共同点**:

1. **大小固定**:每种对象的大小是编译期已知的常量(`sizeof(struct task_struct)`)。
2. **频繁分配/释放**:进程在 fork/exit,文件在 open/close,网络包在收/发——每秒成千上万次。
3. **小**:几十到几千字节,远小于一页(4KB)。
4. **同型号**:成千上万个 `task_struct` 长得一模一样,字段布局完全一致。

第 1~3 点意味着,**直接找 buddy 要页太浪费**:一个 `inode` 600 字节,buddy 最少给 1 页(4KB),内部碎片率 85%;一个 `dentry` 192 字节,碎片率 95%。第 4 点是关键线索——**既然成千上万个对象同型号,为什么不专门为它们开一个"对象池"**,在一页里紧凑摆它 20 个、50 个,而不是一页摆一个?

这就是 slab 分配器要解决的事。

> **不这样会怎样**:假设内核只有 buddy,所有小对象都直接 `alloc_pages`。后果:
>
> 1. **内部碎片爆炸**:一台机器几百万个 `dentry`,每个独占 4KB,光目录项就吃掉几十 GB——而每个 4KB 里只用了 192 字节,95% 浪费。
> 2. **buddy 被榨干**:小对象把页全占走,真正要大块连续页(大页、DMA)的请求反而拿不到。
> 3. **缓存局部性差**:同型号对象散落在不同的页,每次访问都要碰不同的 cache line。
>
> 这是 mm 早期(SLAB 之前)真实遇到过的问题。slab 分配器(最早由 Mark Britton、Jeff Bonwick 在 SunOS 5.4 提出,Linux 1999 年由 Matthew Kirkwood 移植)就是为解决它而生。Linux 6.9 里默认实现叫 **SLUB**(由 Christoph Lameter 2007 年替换上来),老 SLAB 已被移除——**本书所有 slab 细节都以 `mm/slub.c` 为准**。

---

## 7.2 一句话点破

> **slab 的核心招数是:为每种固定大小的对象建一个 `kmem_cache`(对象池),在 buddy 给的若干页(一个 "slab")上紧凑摆满同型号对象,每个空闲对象体内藏一个指向"下一个空闲对象"的指针(内嵌式 freelist),分配就是从 freelist 头摘一个、释放就是挂回头——O(1)、不碎、缓存友好。**

这是结论,不是理由。本章倒过来拆:先看 `kmem_cache` 这个抽象长什么样、它的字段怎么决定一切;再看一个 slab 里对象怎么摆、freelist 怎么串;然后落到 `kmem_cache_create`→`calculate_sizes`→`allocate_slab` 的源码;最后讲 freelist 混淆和 `oo` 编码这两个 SLUB 的招牌技巧。

---

## 7.3 `kmem_cache`:一个"专门装某种对象"的池子

先建立最顶层的抽象。slab 分配器的核心数据结构是 [`struct kmem_cache`](../linux/mm/slab.h#L251)([slab.h:251](../linux/mm/slab.h#L251))——一个 `kmem_cache` 就是"一种固定大小对象的池子"。比如内核里有:

- `task_struct` 的 cache(叫 `task_struct`)。
- `inode` 的 cache(`inode_cache`)。
- 一组通用 `kmalloc-64`、`kmalloc-128`、`kmalloc-192`……cache(给 `kmalloc` 用,第 9 章详讲)。

每个 `kmem_cache` 不直接存对象,它只是个**描述符**——记录"这种对象多大、怎么对齐、freelist 指针放哪、有没有构造函数",以及"我手里有哪些 slab(页)、每个 slab 在哪个 node"。真正的对象,躺在它名下的若干个 **slab** 里(一个 slab = buddy 给的若干连续页 = 紧凑摆满同型号对象的一块内存)。

先看 `struct kmem_cache` 最核心的几个字段([slab.h:251-308](../linux/mm/slab.h#L251-L308),简化示意):

```c
// mm/slab.h#L251-L308 (简化示意,只列本章关心的字段)
struct kmem_cache {
    struct kmem_cache_cpu __percpu *cpu_slab;   // 每 CPU 一个,管"当前正在分配的 slab"
    slab_flags_t flags;
    unsigned long min_partial;                  // 每 node partial 队列最少保留几个 slab

    unsigned int size;              // ★ 对象实际占的字节数(含可能的元数据/padding)
    unsigned int object_size;       // 用户要的对象大小(不含元数据)
    unsigned int inuse;             // 对象"实际使用"的结尾偏移(freeptr 决策用)
    unsigned int offset;            // ★ freelist 指针在对象内的偏移(藏在哪)
    unsigned int align;             // 对齐要求

    struct kmem_cache_order_objects oo;   // ★ 一个 unsigned int 编码 order+对象数
    struct kmem_cache_order_objects min;  // 内存紧张时退守的最小 order
    gfp_t allocflags;                     // 找 buddy 要页时用的 GFP 标志
    void (*ctor)(void *object);           // 构造函数(可选)

    const char *name;              // 名字(给 /proc/slabinfo 看)
    struct list_head list;         // 串在全局 slab cache 链上

#ifdef CONFIG_SLAB_FREELIST_HARDENED
    unsigned long random;          // ★ freelist 混淆用的随机数
#endif
    struct kmem_cache_node *node[MAX_NUMNODES];   // 每 NUMA node 一个,管 partial 队列
};
```

这张字段表是本章剩余部分的地图。我们一个个拆。

### 7.3.1 `cpu_slab` 和 `node[]`:两条缓存的分工

`kmem_cache` 不直接管 slab,它通过两个间接层:

- [`cpu_slab`](../linux/mm/slab.h#L253):**每 CPU 一个**的 [`struct kmem_cache_cpu`](../linux/mm/slub.c#L384)([slub.c:384](../linux/mm/slub.c#L384))。它记着"**这个 CPU 当前从哪个 slab 摘对象**"(字段 `c->slab` 和 `c->freelist`)。第 8 章会讲它怎么靠这个把分配做到几乎无锁——本章先把它当成"当前 CPU 的热 slab"。
- [`node[MAX_NUMNODES]`](../linux/mm/slab.h#L307):**每 NUMA node 一个**的 [`struct kmem_cache_node`](../linux/mm/slub.c#L425)([slub.c:425](../linux/mm/slub.c#L425)),里面是一条 **partial 队列**——挂着一堆"用了一部分、还有空位"的 slab,当 cpu_slab 的 slab 用完时从这里取。

```c
// mm/slub.c#L425-L434 (struct kmem_cache_node)
struct kmem_cache_node {
    spinlock_t list_lock;       // 保护 partial 队列的自旋锁
    unsigned long nr_partial;   // partial 队列里 slab 的个数
    struct list_head partial;   // ★ partial slab 链
    ...
};
```

```c
// mm/slub.c#L384-L400 (struct kmem_cache_cpu)
struct kmem_cache_cpu {
    union {
        struct {
            void **freelist;    // ★ 当前 slab 的空闲对象链头
            unsigned long tid;  // 事务 id,防 CPU 切换竞态(第 8 章详讲)
        };
        freelist_aba_t freelist_tid;
    };
    struct slab *slab;          // ★ 当前 CPU 正在分配的 slab
    struct slab *partial;       // per-cpu partial 链(第 8 章)
    local_lock_t lock;
    ...
};
```

一个心智模型:

```
kmem_cache("task_struct")
├── cpu_slab[0] (CPU 0)
│     slab = slab_A (当前从这摘对象)
│     freelist = obj_A3 → obj_A7 → obj_A1 → NULL   (内嵌 freelist)
│     partial = slab_B → slab_C → ...              (per-cpu partial)
├── cpu_slab[1] (CPU 1)
│     slab = slab_D
│     freelist = obj_D2 → ...
├── ...
└── node[0] (NUMA node 0)
      partial: slab_E ↔ slab_F ↔ slab_G ↔ ...      (per-node partial,自旋锁保护)
```

第 8 章会专门讲 `cpu_slab` 怎么靠 cmpxchg 做到无锁快路径、partial 队列怎么回填。本章我们先把注意力放在 **`size`/`inuse`/`offset`/`oo` 这几个"决定对象布局"的字段**——它们是 `kmem_cache_create` 算出来的,一旦算定,所有这个 cache 名下的 slab 都按这个布局摆对象。

### 7.3.2 `oo`/`min`/`max`:一个 int 编码 order 和对象数

[`struct kmem_cache_order_objects`](../linux/mm/slab.h#L244)([slab.h:244](../linux/mm/slab.h#L244))这个名字看起来怪,其实就一个 `unsigned int x`:

```c
// mm/slab.h#L244-L246
struct kmem_cache_order_objects {
    unsigned int x;
};
```

但这个 `x` 不普通——它**一个 32 位整数同时编码了 order 和"每 slab 对象数"两个值**。这是靠两个常量做到的([slub.c:303-304](../linux/mm/slub.c#L303-L304)):

```c
// mm/slub.c#L303-L304
#define OO_SHIFT    16
#define OO_MASK     ((1 << OO_SHIFT) - 1)
```

编码/解码函数([slub.c:591-608](../linux/mm/slub.c#L591-L608)):

```c
// mm/slub.c#L586-L608 (简化示意)
static inline unsigned int order_objects(unsigned int order, unsigned int size)
{
    return ((unsigned int)PAGE_SIZE << order) / size;   // 一页(order=0)=4KB,order=N 就是 4KB<<N
}

static inline struct kmem_cache_order_objects
oo_make(unsigned int order, unsigned int size)
{
    struct kmem_cache_order_objects x = {
        (order << OO_SHIFT) + order_objects(order, size)  // ★ 高 16 位=order,低 16 位=对象数
    };
    return x;
}

static inline unsigned int oo_order(struct kmem_cache_order_objects x)
{
    return x.x >> OO_SHIFT;        // 取高 16 位
}

static inline unsigned int oo_objects(struct kmem_cache_order_objects x)
{
    return x.x & OO_MASK;          // 取低 16 位
}
```

所以一个 `oo.x = 0x0004_0032` 表示 **order=4(16 页 = 64KB)、每 slab 0x32=50 个对象**。`kmem_cache` 有三个这样的字段:

- `oo`:**正常情况下**用的 order/对象数(尽力让碎片最少)。
- `min`:**内存紧张退守**时的最小 order(至少能放下一个对象)。
- (历史上还有 `max`,6.9 里已合并进 `oo`,本书不展开。)

> **为什么要把两个字段挤进一个 `unsigned int`?** 这是内核"用约束换紧凑"的常见手法——SLUB 的 `kmem_cache_order_objects` 在热路径上要被频繁读(每次 `___slab_alloc` 取 slab 都要读 `oo`),挤进一个 int 意味着**一次内存访问就能同时拿到 order 和对象数**,比开两个字段省一条 cache line 的占用。低 16 位能装到 65535 个对象(实际受 `MAX_OBJS_PER_PAGE=32767` 限制,[slub.c:305](../linux/mm/slub.c#L305)),高 16 位能装到 order 65535(远超 `MAX_PAGE_ORDER=10`),所以编码空间绰绰有余。第 8 章会看到,`cmpxchg` 无锁快路径还要把 `freelist` 指针和 `counters` 也挤进一个双字做"双字段原子更新",`oo` 这种编码只是同思路的小练习。

---

## 7.4 一个 slab 长什么样:`struct slab` 与对象布局

现在钻进一个 slab 内部。一个 slab 是 buddy 给的若干连续页,SLUB 用 [`struct slab`](../linux/mm/slab.h#L52)([slab.h:52](../linux/mm/slab.h#L52))来描述它——**这个结构体不是新分配的内存,它是 `struct page` 的"另一种视图"**(slab.h:51 注释明确写 "Reuses the bits in `struct page`"):

```c
// mm/slab.h#L52-L93 (简化示意,去掉 RCU/memcg 分支)
struct slab {
    unsigned long __page_flags;        // 复用 page->flags

    struct kmem_cache *slab_cache;     // ★ 这个 slab 属于哪个 cache

    union {
        struct {
            union {
                struct list_head slab_list;    // 挂在 partial 链上用
#ifdef CONFIG_SLUB_CPU_PARTIAL
                struct {
                    struct slab *next;         // per-cpu partial 用单链
                    int slabs;
                };
#endif
            };
            union {
                struct {
                    void *freelist;            // ★ 当前 slab 的空闲对象链头
                    union {
                        unsigned long counters; // 一次性原子读/写下面三个
                        struct {
                            unsigned inuse:16;   // 已分配对象数
                            unsigned objects:15; // 总对象数
                            unsigned frozen:1;   // ★ 是否被某 CPU 独占(frozen)
                        };
                    };
                };
#ifdef system_has_freelist_aba
                freelist_aba_t freelist_counter;  // 给 cmpxchg_double 用
#endif
            };
        };
        struct rcu_head rcu_head;
    };
    unsigned int __unused;
    atomic_t __page_refcount;
    ...
};
```

`slab.h:104` 有一行 `static_assert(sizeof(struct slab) <= sizeof(struct page));`——**保证 `struct slab` 不会比 `struct page` 大**,所以 buddy 分配出来的页,其 `struct page` 内存可以**原地把头部 reinterpret 成 `struct slab`** 用(`folio_slab`/`page_slab` 这些转换宏就是干这个的)。这是 mm "一个 folio 多种角色视图"思想的延续(参见第 2 章 `struct page` 的 union 复用)。

`struct slab` 里本章最关心三个字段:

- [`slab->freelist`](../linux/mm/slab.h#L70):**这个 slab 的空闲对象链头**(指向第一个空闲对象)。
- [`slab->inuse`](../linux/mm/slab.h#L74):这个 slab 里已分配出去的对象数(16 位)。
- [`slab->objects`](../linux/mm/slab.h#L75):这个 slab 总共能装的对象数(15 位)。
- [`slab->frozen`](../linux/mm/slab.h#L76):**这个 slab 是否被某个 CPU 独占**(frozen=1 表示"我在某 CPU 的 cpu_slab 里,别人别动")。frozen 是第 8 章无锁快路径的核心标志,本章先记住它"表示被某 CPU 独占"。

### 7.4.1 对象在 slab 里怎么摆:紧凑等间距

一个 slab 是一块 `(PAGE_SIZE << order)` 字节的连续内存,被切成 `slab->objects` 个等大小的槽,每个槽是一个对象:

```
slab (假设 order=0, 1 页 = 4096 字节, object size=80 字节)
地址      0       80      160     240     320    ...   4000
        ┌────────┬────────┬────────┬────────┬────────┬─────┬────────┐
        │  obj0  │  obj1  │  obj2  │  obj3  │  obj4  │ ... │ obj50  │
        └────────┴────────┴────────┴────────┴────────┴─────┴────────┘
         ↑       ↑        ↑        ↑        ↑               ↑
        4096 / 80 = 51 个槽(最后一个槽可能不足 80,作 padding)

每个槽大小 = s->size (= 80,已含对齐 padding)
对象"实际用"的大小 = s->inuse (= 76,含对象体 + 可能的内嵌 freeptr 区)
```

注意三个"大小"概念容易混:

| 字段 | 含义 | 典型值(对象 76 字节) |
|------|------|----------------------|
| `s->object_size` | 用户要的对象大小 | 76 |
| `s->inuse` | 实际占用(含 freeptr 区,不含尾部 redzone) | 76 或 80 |
| `s->size` | **槽大小**(每两个对象中心的间距) | 80(对齐到 `align`) |

每个槽 `s->size` 字节,相邻槽中心相距 `s->size`。给定一个对象指针 `obj`,它的下一个对象就是 `obj + s->size`。SLUB 没有"对象头"——对象的元数据(`freelist` 指针)**就藏在对象体内**或紧接其后,这是它区别于老 SLAB 的关键。

### 7.4.2 内嵌 freelist:空闲对象自己串自己

现在讲 slab 最妙的招——**freelist 不占额外空间,它藏在空闲对象的体内**。

思路:一个对象在"被分配使用"时,它的所有字节都是用户数据;但在"空闲"状态时,反正用户也不会读它,那它的体内某个位置就可以临时存一个指针——指向"下一个空闲对象"。所有空闲对象就这样一根单链串起来,链头存在 `slab->freelist`。

```
slab->freelist
      │
      ▼
┌────────┬────────┬────────┬────────┬────────┐
│  obj0  │  obj1  │  obj2  │  obj3  │  obj4  │   (每个对象 s->size 字节)
│ ALLOC  │  FREE  │ ALLOC  │  FREE  │  FREE  │
│ user.. │ [fp→3] │ user.. │ [fp→4] │ [fp→N] │   ([fp] 是内嵌 freeptr)
└────────┴───┬────┴────────┴───┬────┴───┬────┘
             │                 │        │
             └─────────────────┘        │
                                        ▼
                                       NULL
```

(图中 `[fp→N]` 表示这个空闲对象体内 offset 处存着指向下一个空闲对象的指针。)

链头是 `slab->freelist`(指向 obj1),obj1 体内的 offset 处存 obj3,obj3 体内存 obj4,obj4 体内存 NULL。**所有"ALLOC"对象体内的对应位置是用户数据,我们不碰**;只有"FREE"对象的那几个字节临时当指针用——一旦这个对象被分配出去,用户数据会覆盖那几个字节,freelist 自然就把它从链上"摘"了(因为分配时我们已经读出 next 并更新了链头)。

这个"指针藏在哪"由 [`s->offset`](../linux/mm/slab.h#L261) 决定。**SLUB 默认把 freelist 指针藏在对象中间**(不是开头也不是末尾!),原因下面 7.5 节讲 `calculate_sizes` 时细拆。先看 freelist 读写代码——这是全章最核心的一组函数。

---

## 7.5 freelist 的读/写:`set_freepointer` / `get_freepointer`

读/写 freelist 指针的代码在 [`mm/slub.c#L468-L558`](../linux/mm/slub.c#L468-L558)。我们一段段拆。

### 7.5.1 `freeptr_t`:可能被混淆的指针

SLUB 先定义一个 [`freeptr_t`](../linux/mm/slub.c#L472)([slub.c:472](../linux/mm/slub.c#L472))类型来表示"freelist 指针":

```c
// mm/slub.c#L468-L472
/*
 * freeptr_t represents a SLUB freelist pointer, which might be encoded
 * and not dereferenceable if CONFIG_SLAB_FREELIST_HARDENED is enabled.
 */
typedef struct { unsigned long v; } freeptr_t;
```

注释说得很清楚:**它可能不是真的指针值,是被编码过的**——开启 `CONFIG_SLAB_FREELIST_HARDENED` 后,内存里存的是混淆后的值,直接 dereference 会拿到垃圾。要拿到真指针,必须 decode。

编码和解码成对出现([slub.c:479-503](../linux/mm/slub.c#L479-L503)):

```c
// mm/slub.c#L479-L503
static inline freeptr_t freelist_ptr_encode(const struct kmem_cache *s,
                                            void *ptr, unsigned long ptr_addr)
{
    unsigned long encoded;

#ifdef CONFIG_SLAB_FREELIST_HARDENED
    encoded = (unsigned long)ptr ^ s->random ^ swab(ptr_addr);
#else
    encoded = (unsigned long)ptr;
#endif
    return (freeptr_t){.v = encoded};
}

static inline void *freelist_ptr_decode(const struct kmem_cache *s,
                                        freeptr_t ptr, unsigned long ptr_addr)
{
    void *decoded;

#ifdef CONFIG_SLAB_FREELIST_HARDENED
    decoded = (void *)(ptr.v ^ s->random ^ swab(ptr_addr));
#else
    decoded = (void *)ptr.v;
#endif
    return decoded;
}
```

不开混淆时,`encode`/`decode` 就是裸指针,什么也不做。开了混淆后:

```
encoded = ptr ^ s->random ^ swab(ptr_addr)
decoded = ptr.v ^ s->random ^ swab(ptr_addr)   (同一个 ptr_addr 时还原成 ptr)
```

`^` 是自反的(同一个值异或两次就还原),所以 encode 和 decode 用的是**同一个公式**,只是 encode 把真 `ptr` 异或成乱码、decode 把乱码异或回真 `ptr`。这里有两个看起来奇怪的细节:

1. **`s->random`**:`kmem_cache_create` 时给每个 cache 随机生成的一个 `unsigned long`([slub.c:5094-5095](../linux/mm/slub.c#L5094-L5095)),`kmem_cache_open` 里 `s->random = get_random_long();`。**不同 cache 的 `random` 不同**——攻击者即使在 cache A 上推断出 random,也用不到 cache B。
2. **`swab(ptr_addr)`**:`ptr_addr` 是"这个 freeptr 在内存中的地址本身"(也就是 `&object + s->offset`),`swab` 把它的字节序翻转。把**指针本身的存储地址**也混进编码,是 SLUB 的精妙一笔——**同样一个"指向 obj X"的 freeptr,存在对象 A 里和存在对象 B 里,编码值完全不同**(因为 A 和 B 的地址不同)。这把"把一个对象的 freeptr 复制粘贴到另一个对象"的简单攻击挡死了。

为什么这一坨东西有用,我们留到 7.8 技巧精解专门拆。先看完整的读写函数。

### 7.5.2 `get_freepointer`:从对象读出下一个空闲对象

[`get_freepointer`](../linux/mm/slub.c#L505-L514)([slub.c:505](../linux/mm/slub.c#L505))干的事:**给定一个对象指针,返回它的"下一个空闲对象"**。

```c
// mm/slub.c#L505-L514
static inline void *get_freepointer(struct kmem_cache *s, void *object)
{
    unsigned long ptr_addr;
    freeptr_t p;

    object = kasan_reset_tag(object);
    ptr_addr = (unsigned long)object + s->offset;   // freeptr 藏在 object 偏移 s->offset 处
    p = *(freeptr_t *)(ptr_addr);                    // 读出(可能是混淆值)
    return freelist_ptr_decode(s, p, ptr_addr);      // 解码出真指针
}
```

三步:

1. 算 `ptr_addr = object + s->offset`——找到"这个对象体内藏着 freeptr 的位置"。
2. 从 `ptr_addr` 读出一个 `freeptr_t`(可能已经是混淆值)。
3. `freelist_ptr_decode` 把它解码成真指针。

[`get_freepointer_safe`](../linux/mm/slub.c#L534-L546)([slub.c:534](../linux/mm/slub.c#L534))是它的"安全版"——在 `debug_pagealloc` 开启时,某些页可能被 unmap 了(用来抓越界访问),不能直接 dereference,得用 `copy_from_kernel_nofault`:

```c
// mm/slub.c#L534-L546 (简化示意)
static inline void *get_freepointer_safe(struct kmem_cache *s, void *object)
{
    unsigned long freepointer_addr;
    freeptr_t p;

    if (!debug_pagealloc_enabled_static())
        return get_freepointer(s, object);   // 不开 debug 就走快版

    object = kasan_reset_tag(object);
    freepointer_addr = (unsigned long)object + s->offset;
    copy_from_kernel_nofault(&p, (freeptr_t *)freepointer_addr, sizeof(p));
    return freelist_ptr_decode(s, p, freepointer_addr);
}
```

### 7.5.3 `set_freepointer`:往对象里写下一个空闲对象

[`set_freepointer`](../linux/mm/slub.c#L548-L558)([slub.c:548](../linux/mm/slub.c#L548))反着来:**给定一个对象和"下一个空闲对象"指针,把后者写进前者的 freeptr 位置**。

```c
// mm/slub.c#L548-L558
static inline void set_freepointer(struct kmem_cache *s, void *object, void *fp)
{
    unsigned long freeptr_addr = (unsigned long)object + s->offset;

#ifdef CONFIG_SLAB_FREELIST_HARDENED
    BUG_ON(object == fp); /* naive detection of double free or corruption */
#endif

    freeptr_addr = (unsigned long)kasan_reset_tag((void *)freeptr_addr);
    *(freeptr_t *)freeptr_addr = freelist_ptr_encode(s, fp, freeptr_addr);
}
```

注意三件事:

1. `BUG_ON(object == fp)`:**混淆模式下,把一个对象"释放回它自己"会被检测为 double-free 而崩溃**。这是个朴素的检测——合法的 freeptr 链里,一个对象绝不会指向自己。开了混淆后这变得可能(因为混淆值乱七八糟,正常情况不会出现 `object == fp`,一旦出现就是有人写坏了)。这是 SLUB 在安全模式下的小防线之一。
2. 写入的是 `freelist_ptr_encode` 的结果——**内存里存的是混淆值,不是真指针**。
3. `freeptr_addr` 经过了 `kasan_reset_tag`(KASAN 的地址标签清理,跟 ARM MTE 有关,本章不展开)。

有了 `get_freepointer`/`set_freepointer` 这对工具,slab 的分配/释放逻辑就极其简单了。

---

## 7.6 分配/释放:摘 freelist 头 / 挂 freelist 头

现在把"对象布局 + freelist"组装成完整的分配/释放语义。本节先讲**概念上的快路径**(摘头/挂头),真实代码里这套逻辑在 SLUB 的快路径(cmpxchg)和慢路径两种地方都有体现——**第 8 章讲 cmpxchg 快路径,本章先用慢路径 `___slab_alloc` 和 `__slab_free` 的代码看清"摘头/挂头"到底怎么操作 freelist**。

### 7.6.1 分配 = 摘 freelist 头

从概念上讲,slab 分配一个对象就是:

```
1. 拿当前 slab 的 freelist 头(指向第一个空闲对象 obj)
2. 算出 next = get_freepointer(s, obj)   (obj 体内存的下一个空闲对象)
3. 把 slab->freelist 更新成 next
4. 返回 obj
```

**摘的是链头**,所以是 O(1)。在 SLUB 的真实代码里,这套操作有两种实现:

- **快路径**(无锁 cmpxchg):[`__slab_alloc_node`](../linux/mm/slub.c#L3622) 里那段 `object = c->freelist; ... __update_cpu_freelist_fast(s, object, next_object, tid)`([slub.c:3663-3692](../linux/mm/slub.c#L3663-L3692)),靠原子比较交换同时更新 freelist 和 tid,无锁。第 8 章详讲。
- **慢路径**(加 local_lock):[`___slab_alloc`](../linux/mm/slub.c#L3376-L3594) 里 [`load_freelist`](../linux/mm/slub.c#L3443-L3456) 标号那段:

```c
// mm/slub.c#L3443-L3456 (load_freelist 段,简化)
load_freelist:
    lockdep_assert_held(this_cpu_ptr(&s->cpu_slab->lock));

    /*
     * freelist is pointing to the list of objects to be used.
     * slab is pointing to the slab from which the objects are obtained.
     * That slab must be frozen for per cpu allocations to work.
     */
    VM_BUG_ON(!c->slab->frozen);
    c->freelist = get_freepointer(s, freelist);   // ★ 把链头摘下来,链头变成第二个
    c->tid = next_tid(c->tid);
    local_unlock_irqrestore(&s->cpu_slab->lock, flags);
    return freelist;
```

注意 `c->freelist = get_freepointer(s, freelist)` 这一行——它就是"摘头":把当前链头 `freelist` 的下一个对象,设为新的链头。返回的是旧的链头(被摘走的那个对象)。`VM_BUG_ON(!c->slab->frozen)` 是断言:能走这条路径的 slab 必须被某 CPU 独占(frozen),否则并发会乱。

> **慢路径在什么时候触发?** 当快路径 cmpxchg 失败(被别的 CPU 抢先了)、或当前 cpu_slab 的 slab 用完了/不匹配 NUMA 节点了,就要走 `___slab_alloc`。它要重新获取一个有空间的 slab——要么从 per-cpu partial 拿、要么从 per-node partial 拿、要么找 buddy 要新页(`new_slab`)。这部分逻辑见 [slub.c:3376-3594](../linux/mm/slub.c#L3376-L3594),第 8 章会专门拆。本章我们只关注"拿到 slab 之后怎么摘 freelist 头"这一步。

### 7.6.2 释放 = 挂 freelist 头

释放是把对象"挂回"freelist 头:

```
1. 拿当前 slab 的 freelist 头 old_head
2. set_freepointer(s, 释放的对象, old_head)   (把释放对象体内的 freeptr 指向 old_head)
3. 把 slab->freelist 更新成 释放的对象
```

同样 O(1)。真实代码里,释放也有快路径(cmpxchg)和慢路径(`__slab_free`)。慢路径 [`__slab_free`](../linux/mm/slub.c#L4089-L4199) 的核心几行:

```c
// mm/slub.c#L4114-L4142 (简化示意,去掉锁重试细节)
do {
    prior = slab->freelist;                  // 当前链头
    counters = slab->counters;
    set_freepointer(s, tail, prior);         // ★ 把释放对象挂到链头前,指向 prior
    new.counters = counters;
    was_frozen = new.frozen;
    new.inuse -= cnt;
    ...
} while (!slab_update_freelist(s, slab,
            prior, counters,
            head, new.counters,              // ★ 原子地把 slab->freelist 更新成 head
            "__slab_free"));
```

`set_freepointer(s, tail, prior)` 把释放的对象体内 freeptr 指向旧的链头 `prior`;然后 `slab_update_freelist`(底层是 `cmpxchg`)原子地把 `slab->freelist` 从 `prior` 改成 `head`(释放的对象)。如果中间别的 CPU 改过 `slab->freelist`(prior 变了),cmpxchg 失败,循环重来。这个 cmpxchg 重试循环是 SLUB 释放路径并发正确性的根——**两个 CPU 同时往同一个 slab 释放对象,只有一个能成功改 `slab->freelist`,另一个重试时 prior 已经是新值,不会丢对象**。

快路径 [`do_slab_free`](../linux/mm/slub.c#L4217-L4270) 里对应那段更紧凑:

```c
// mm/slub.c#L4243-L4251 (USE_LOCKLESS_FAST_PATH 分支)
freelist = READ_ONCE(c->freelist);

set_freepointer(s, tail, freelist);   // 挂到当前 cpu slab 的 freelist 头

if (unlikely(!__update_cpu_freelist_fast(s, freelist, head, tid))) {
    note_cmpxchg_failure("slab_free", s, tid);
    goto redo;
}
```

第 8 章会专门讲 `tid` 和这个 cmpxchg 怎么保证并发正确。本章只需建立概念:**分配摘 freelist 头,释放挂 freelist 头,都是 O(1)**。

### 7.6.3 用 mermaid 把摘/挂画清

```mermaid
flowchart TD
    subgraph 分配["分配:摘 freelist 头"]
        A1["读 slab->freelist → obj"] --> A2["next = get_freepointer(s, obj)<br/>(从 obj 体内读出下一个空闲)"]
        A2 --> A3["slab->freelist = next<br/>(原子更新)"]
        A3 --> A4["返回 obj 给调用者"]
    end
    subgraph 释放["释放:挂 freelist 头"]
        F1["要释放 obj"] --> F2["old = slab->freelist<br/>(读当前链头)"]
        F2 --> F3["set_freepointer(s, obj, old)<br/>(把 obj 体内的 freeptr 指向 old)"]
        F3 --> F4["slab->freelist = obj<br/>(原子更新,把 obj 设为新链头)"]
    end
    classDef op fill:#dbeafe,stroke:#2563eb
    class A1,A2,A3,A4,F1,F2,F3,F4 op
```

> **钉死这件事**:slab 分配/释放的 O(1) 性,完全来自"freelist 是个单链、操作只在链头"这件事。摘头 O(1)、挂头 O(1)、不需要扫描、不需要 buddy 那种"找伙伴合并"。代价是 slab **不主动合并相邻空闲对象**(它们就是 freelist 上的节点,你随时可以再分配出去),所以 slab 不像 buddy 那样"抗外碎片"——它**没有外碎片问题**(每个对象槽位大小固定,要么空闲要么占用),也没有 buddy 那种"伙伴被卡住"的合并难题。slab 的"碎片"是另一种:一个 slab 用了一半,剩一半空闲,这个 slab 整体不能还给 buddy——这是 slab 的"per-slab 内部碎片",靠 partial 队列管理和 `min_partial` 参数控制(第 8 章)。

---

## 7.7 一个 slab 怎么从无到有:`kmem_cache_create` → `calculate_sizes` → `allocate_slab`

现在倒过来——一个 `kmem_cache` 是怎么被创建出来的?它的 `size`/`inuse`/`offset`/`oo` 这些字段是怎么算出来的?一个崭新的 slab 又是怎么从 buddy 拿到页、把对象摆满、串好 freelist 的?这三步对应三个函数。

### 7.7.1 `kmem_cache_create`:对外入口

内核里创建一个 cache 的标准 API 是 [`kmem_cache_create`](../linux/mm/slab_common.c#L387-L391)([slab_common.c:387](../linux/mm/slab_common.c#L387)),它是个薄壳,转给 [`kmem_cache_create_usercopy`](../linux/mm/slab_common.c#L273)([slab_common.c:273](../linux/mm/slab_common.c#L273)):

```c
// mm/slab_common.c#L387-L391
kmem_cache_create(const char *name, unsigned int size, unsigned int align,
                  slab_flags_t flags, void (*ctor)(void *))
{
    return kmem_cache_create_usercopy(name, size, align, flags, 0, 0, ctor);
}
```

`kmem_cache_create_usercopy` 干几件事(slab_common.c:273-358):

1. **参数校验**(名字、size、flags 合法性)。
2. **对齐计算**:[`calculate_alignment(flags, align, size)`](../linux/mm/slab_common.c#L336)——把用户传的 `align` 规整成"至少 `sizeof(void *)`,且是 2 的幂"。
3. **尝试别名合并**:[`__kmem_cache_alias`](../linux/mm/slab_common.c#L325)——如果已存在一个 size/flags 完全相同的 cache,就不新建,复用旧的(避免同型号 cache 重复)。
4. **真正创建**:[`create_cache`](../linux/mm/slab_common.c#L335) → 内部调 [`__kmem_cache_create`](../linux/mm/slub.c#L5702)。

`__kmem_cache_create` 在 SLUB 里的实现就是 [`kmem_cache_open`](../linux/mm/slub.c#L5091)([slub.c:5702-5706](../linux/mm/slub.c#L5702-L5706)):

```c
// mm/slub.c#L5702-L5724
int __kmem_cache_create(struct kmem_cache *s, slab_flags_t flags)
{
    int err;

    err = kmem_cache_open(s, flags);   // ★ 核心:算字段、建 node 数组
    if (err)
        return err;
    ...
}
```

### 7.7.2 `kmem_cache_open`:算一切字段

[`kmem_cache_open`](../linux/mm/slub.c#L5091)([slub.c:5091](../linux/mm/slub.c#L5091))是 SLUB 的"开张"函数,它先给混淆用的 `random` 取个随机值,然后调 [`calculate_sizes`](../linux/mm/slub.c#L4954) 算所有布局字段:

```c
// mm/slub.c#L5091-L5100
static int kmem_cache_open(struct kmem_cache *s, slab_flags_t flags)
{
    s->flags = kmem_cache_flags(flags, s->name);
#ifdef CONFIG_SLAB_FREELIST_HARDENED
    s->random = get_random_long();          // ★ 每个 cache 一个独立随机数
#endif

    if (!calculate_sizes(s))
        goto error;
    ...
}
```

### 7.7.3 `calculate_sizes`:决定 size、inuse、offset、oo

这是本章第二个最值得逐行读的函数([slub.c:4954-5089](../linux/mm/slub.c#L4954-L5089))。它一步一步把"用户要的对象大小"算成"slab 实际怎么摆"。我们跟它的逻辑走。

**第 1 步:大小对齐到 word**。

```c
// mm/slub.c#L4965
size = ALIGN(size, sizeof(void *));
```

`object_size` 先对齐到 `sizeof(void *)`(64 位上是 8 字节)。因为 freelist 指针要放在 word 对齐的位置,对象大小至少得是 word 的倍数。

**第 2 步:确定 `inuse`**。

```c
// mm/slub.c#L4993
s->inuse = size;
```

`inuse` 是"对象实际占用的结尾偏移"——不含调试 redzone、不含尾部 track,但**可能含藏 freeptr 的那块区域**(看第 3 步)。不开调试时 `inuse = ALIGN(object_size, word)`。

**第 3 步:决定 freelist 指针藏在哪(`s->offset`)——本章最关键的一步**。

```c
// mm/slub.c#L4995-L5022
if (slub_debug_orig_size(s) ||
    (flags & (SLAB_TYPESAFE_BY_RCU | SLAB_POISON)) ||
    ((flags & SLAB_RED_ZONE) && s->object_size < sizeof(void *)) ||
    s->ctor) {
    /*
     * Relocate free pointer after the object if it is not
     * permitted to overwrite the first word of the object on
     * kmem_cache_free.
     */
    s->offset = size;                  // ★ freeptr 放在对象体后(占用额外 sizeof(void*) 字节)
    size += sizeof(void *);
} else {
    /*
     * Store freelist pointer near middle of object to keep
     * it away from the edges of the object to avoid small
     * sized over/underflows from neighboring allocations.
     */
    s->offset = ALIGN_DOWN(s->object_size / 2, sizeof(void *));   // ★ freeptr 藏在对象中间
}
```

**这是 SLUB 区别于老 SLAB 最关键的决策之一**。两种情况:

- **默认情况(else 分支)**:freelist 指针藏在**对象正中间**(`object_size / 2` 往下对齐到 word)。**不额外占任何字节**——它就是借用对象体内本来归用户用的字节。
- **特殊情况(if 分支)**:如果这个 cache 有构造函数(`ctor`)、或开了 RCU/POISON 调试、或对象比一个 word 还小,那么 freelist 不能藏在对象体内(因为构造函数会初始化整个对象、覆盖掉 freeptr),只能**放在对象体之后**——这时 `s->offset = size; size += sizeof(void *)`,每个对象多占一个 word。

**为什么默认要藏在"中间"而不是"开头"或"末尾"?** 注释直说了:"to keep it away from the edges of the object to avoid small sized over/underflows from neighboring allocations"。意思是:

- 如果藏在**开头**(对象第 0 字节),那么相邻**前一个对象**越界写**一点点**(比如越界 8 字节)就会覆盖到本对象的 freeptr——堆溢出攻击最常见的姿态就是"越界一两个 word 改 freeptr 劫持 freelist"。
- 如果藏在**末尾**,那么本对象越界写一点点会覆盖**下一个对象**的 freeptr。
- 藏在**中间**,本对象或相邻对象的小幅越界都打不到 freeptr——**攻击者要溢出半个对象才能碰到 freeptr**,大幅提高了利用难度。

这是个零成本(不占额外字节)的安全增益。同时配合 `CONFIG_SLAB_FREELIST_HARDENED`(7.8 节讲)的双重防护,SLUB 的 freelist 抗攻击能力远强于老 SLAB。

**第 4 步:对齐到 `align`,确定最终 `s->size`**。

```c
// mm/slub.c#L5061-L5063
size = ALIGN(size, s->align);
s->size = size;
s->reciprocal_size = reciprocal_value(size);
```

`reciprocal_value` 是为了用"乘法 + 移位"替代除法算"对象在 slab 里的索引",`__obj_to_index` 用它做 O(1) 反查(给定一个指针,算它是 slab 里第几个对象)。

**第 5 步:算 order,确定 `oo`/`min`**。

```c
// mm/slub.c#L5064-L5088
order = calculate_order(size);

if ((int)order < 0)
    return 0;

s->allocflags = 0;
if (order)
    s->allocflags |= __GFP_COMP;          // 多页要标 __GFP_COMP(复合页)
...
s->oo = oo_make(order, size);             // ★ 正常 order 编码
s->min = oo_make(get_order(size), size);  // 退守到"至少放下一个对象"的最小 order
```

`calculate_order` 是个**带"碎片率"约束的搜索**(下一小节专讲)。算完后 `s->oo` 编码了"正常用一个 `order` 阶、`order_objects` 个对象的 slab";`s->min` 是退守方案——内存紧张时,只要能放下一个对象(`get_order(size)`)的最小 order 就行。

### 7.7.4 `calculate_order`:在碎片和 order 之间找平衡

[`calculate_order`](../linux/mm/slub.c#L4722-L4783)([slub.c:4722](../linux/mm/slub.c#L4722))是个有意思的启发式搜索。它要回答:**用多大 order 的 slab,既不会内部碎片太多,又不会 slab 太大不好管理?**

```c
// mm/slub.c#L4722-L4783 (简化示意)
static inline int calculate_order(unsigned int size)
{
    unsigned int order, min_objects, min_order;

    /* 1. 估算"每 slab 至少几个对象"——按 CPU 数线性增加 */
    min_objects = slub_min_objects;
    if (!min_objects) {
        unsigned int nr_cpus = num_present_cpus();
        if (nr_cpus <= 1)
            nr_cpus = nr_cpu_ids;
        min_objects = 4 * (fls(nr_cpus) + 1);   // CPU 越多,要求每 slab 对象越多(减少跨 CPU 抢 slab)
    }
    ...
    min_order = max_t(unsigned int, slub_min_order,
                      get_order(min_objects * size));   // 至少能放下 min_objects 个对象的 order

    /* 2. 从"1/16 浪费率"开始,逐步放宽到 1/4、1/8 ... 1/2,找第一个满足的 order */
    for (unsigned int fraction = 16; fraction > 1; fraction /= 2) {
        order = calc_slab_order(size, min_order, slub_max_order, fraction);
        if (order <= slub_max_order)
            return order;
    }

    /* 3. 实在不行,只要求能放下一个对象 */
    order = get_order(size);
    if (order <= MAX_PAGE_ORDER)            // ★ 全书级统一:6.x 里叫 MAX_PAGE_ORDER
        return order;
    return -ENOSYS;
}
```

逻辑:

1. **先估算"每 slab 至少几个对象"**——这是为了让多 CPU 系统(每 CPU 都可能拿到一个 slab)不会频繁找 buddy 要新 slab。CPU 越多,要求每 slab 装的对象越多,这样每个 slab 能撑更久。`min_objects = 4 * (fls(nr_cpus) + 1)`,典型 8 核机器 `fls(8)=4`,`min_objects = 4*5 = 20`。
2. **从最严的 1/16 浪费率开始搜**——要求"slab 末尾 padding 不超过总大小的 1/16"。从 `min_order` 起往上试到 `slub_max_order`(默认 3,即 32KB),找到第一个满足"浪费率 ≤ 1/16 且 ≥ min_objects 个对象"的 order 就返回。
3. **找不到,放宽到 1/8、1/4、1/2**——直到接受任意浪费,只保证至少 min_objects 个对象。
4. **还不行,只要能放下一个对象**——`get_order(size)` 是最小能放下一个对象的 order。
5. **连这都不行,返回负值表示失败**(对象比 `MAX_PAGE_ORDER` 页还大,slab 不适合,该直接用 buddy)。

这套"从严到宽逐步退让"的策略,让 SLUB 在大多数情况下能拿到一个**碎片率低、对象数够、order 不太大**的好配置。`slub_max_order` 默认 3 意味着单个 slab 一般不超过 8 页(32KB),既够紧凑、又不会因为单个 slab 太大而难以管理(过大 slab 释放时一次性还给 buddy 也压力大)。

### 7.7.5 `allocate_slab`:从 buddy 拿页、摆满对象、串 freelist

最后一步——`kmem_cache` 创建完、字段都算定后,真的需要一个新 slab 时,调 [`allocate_slab`](../linux/mm/slub.c#L2322-L2387)([slub.c:2322](../linux/mm/slub.c#L2322)):

```c
// mm/slub.c#L2322-L2387 (简化示意,去掉 pfmemalloc/debug 分支)
static struct slab *allocate_slab(struct kmem_cache *s, gfp_t flags, int node)
{
    struct slab *slab;
    struct kmem_cache_order_objects oo = s->oo;
    void *start, *p, *next;
    int idx;

    flags |= s->allocflags;

    /* 1. 找 buddy 要页(优先用 oo 的 order,失败退守 min) */
    slab = alloc_slab_page(alloc_gfp, node, oo);
    if (unlikely(!slab)) {
        oo = s->min;
        slab = alloc_slab_page(alloc_gfp, node, oo);   // 退守到 min order
        if (unlikely(!slab))
            return NULL;
        stat(s, ORDER_FALLBACK);
    }

    slab->objects = oo_objects(oo);
    slab->inuse = 0;                // 新 slab,全部空闲
    slab->frozen = 0;               // 还没被任何 CPU 独占

    slab->slab_cache = s;
    kasan_poison_slab(slab);

    start = slab_address(slab);
    setup_slab_debug(s, slab, start);

    /* 2. 串 freelist —— 这里是本章的高潮 */
    if (!shuffle_freelist(s, slab)) {           // 默认不开 FREELIST_RANDOM 走这条
        start = fixup_red_left(s, start);
        start = setup_object(s, start);
        slab->freelist = start;                  // ★ 链头 = 第一个对象
        for (idx = 0, p = start; idx < slab->objects - 1; idx++) {
            next = p + s->size;                  // 下一个对象(地址相邻)
            next = setup_object(s, next);
            set_freepointer(s, p, next);         // ★ 把 p 体内的 freeptr 指向 next
            p = next;
        }
        set_freepointer(s, p, NULL);             // 最后一个对象指向 NULL
    }

    return slab;
}
```

这段代码可视化一下,就是一个崭新的 slab 从"一片空白页"变成"摆满对象、freelist 串好"的过程:

```
allocate_slab 之前:buddy 给了 1 页(4096 字节),struct slab 已初始化
─────────────────────────────────────────────────────────────────
地址: 0    80   160  240  ...  4000  4080  4096
      ┌────┬────┬────┬────┬─────┬────┬────┐
      │    │    │    │    │ ... │    │    │   (一片空白)
      └────┴────┴────┴────┴─────┴────┴────┘

allocate_slab 之后:每个槽是一个对象,空闲对象体内 freeptr 串成链
─────────────────────────────────────────────────────────────────
      ┌────┬────┬────┬────┬─────┬────┬────┐
      │[→1]│[→2]│[→3]│[→4]│ ... │[→N]│NULL│   ([→k] 表示 freeptr 指向第 k 个对象)
      └─┬──┴────┴────┴────┴─────┴────┴────┘
        │
        ▼
   slab->freelist = obj0  (链头)

所有对象都空闲,freelist 串成 obj0 → obj1 → obj2 → ... → objN → NULL
```

**这就是 slab 把"一片页"变成"对象池"的全部魔法**——一个循环、`set_freepointer` 把相邻对象串起来,链头存进 `slab->freelist`。之后第一次分配就摘链头(`load_freelist`),返回 obj0;`slab->freelist` 变成 obj1。依此类推,直到链空( slab 用完),再去 partial 队列拿或新建。

如果开了 `CONFIG_SLAB_FREELIST_RANDOM`,会走 [`shuffle_freelist`](../linux/mm/slub.c#L2258-L2289) 那条路——用 `s->random_seq`(一个随机排好的对象下标序列)把对象按随机顺序串起来,而不是按地址顺序。**这让相邻分配出去的对象,在物理上不一定是相邻的**,给"利用相邻对象溢出"的攻击再添一道门槛。本章不展开,知道有这回事即可。

---

## 7.8 技巧精解:freelist 混淆与"指针藏在中间"

本章挑两个最硬核的技巧,配真实源码 + 反面对比拆透。

### 技巧一:freelist 指针藏在对象中间(零成本抗溢出)

这是 7.7.3 节 `calculate_sizes` 里那行 `s->offset = ALIGN_DOWN(s->object_size / 2, sizeof(void *))` 背后的设计。我们用反面对比让它的妙处显形。

> **反面对比·朴素方案 A(freelist 放对象开头)**:假设 SLUB 把 freelist 指针放在每个对象的**第 0 字节**(offset=0)。这样**相邻对象的越界写**极易打到 freeptr。考虑这样的内存布局:
>
> ```
> ... │ obj_k (已分配,用户在写) │ obj_{k+1} (空闲,[fp]在 offset=0) │ ...
> ```
>
> 用户对 `obj_k` 的代码有一个 off-by-one 漏洞,多写了 8 字节——直接覆盖了 `obj_{k+1}` 的 freeptr。下次分配,SLUB 顺着被改过的 freeptr 走,把一个"攻击者指定地址"当对象返回——这就是经典的 **freelist 劫持** heap exploit。老 SLAB 在很多布局下就是这个样子,曾是 Linux 内核堆攻击的高发地。

> **反面对比·朴素方案 B(独立维护一个 freelist 数组)**:假设不把 freeptr 藏对象体内,而是给每个 slab 额外开一个数组:`void *free_array[N]`,串空闲对象。后果:
>
> 1. **内存开销**:每个 slab 多占 `N * sizeof(void *)` 字节,大 slab 几百个对象就是几 KB 额外开销,内部碎片反而比内嵌式更糟。
> 2. **缓存局部性差**:分配一个对象要访问 slab 头(读 `free_array`)+ 访问对象本体,两次 cache line miss;内嵌式因为 freeptr 就在对象体内,摘头时一次 cache line 命中。

SLUB 的方案(C,**当前 Linux**):

- **freeptr 藏在对象正中间**(默认情况),借用对象体内本来归用户用的字节,**零额外开销**。
- 攻击者要改 freeptr,得**从对象开头溢出半个对象的大小**(或从末尾倒过来溢出半个对象)——绝大多数堆溢出漏洞都是小幅越界(off-by-one、off-by-few),够不到中间。

```c
// mm/slub.c#L5021 (这是"freeptr 藏中间"的全部代码)
s->offset = ALIGN_DOWN(s->object_size / 2, sizeof(void *));
```

> **钉死这件事**:SLUB 的"freeptr 藏中间"是零内存成本的安全增益。它利用了一个事实:**空闲对象的体内反正没人读,那几个字节临时当指针用**。把这几个字节放在对象中间而不是边缘,等于免费给堆溢出攻击设了一道路障。这是 SLUB 设计上一处看似不起眼、实则用心良苦的细节——老 SLAB 没有这个意识,freeptr 位置相对固定,是堆漏洞利用的主要入口。

注意特殊情况(`ctor`/RCU/POISON)下 freeptr 还是会被放到对象体后——因为构造函数会初始化整个对象、覆盖掉中间的 freeptr,这时藏中间不行,只能加 sizeof(void*) 放尾部。这是"安全"和"功能"的取舍——SLUB 在能藏中间时坚决藏中间。

### 技巧二:freelist 混淆 `ptr ^ random ^ swab(ptr_addr)`

这是 7.5.1 节那段 [`freelist_ptr_encode`](../linux/mm/slub.c#L479-L490) 背后的安全机制,本章第二个招牌技巧。

```c
// mm/slub.c#L484-L489 (CONFIG_SLAB_FREELIST_HARDENED 开启时)
encoded = (unsigned long)ptr ^ s->random ^ swab(ptr_addr);
```

这个公式有三个部分,每一部分都防一种攻击:

| 部分 | 防什么 |
|------|--------|
| `ptr ^` | 自反性:encode 和 decode 用同一个公式即可互逆 |
| `^ s->random` | **防"跨 cache 复用"**:每个 cache 的 random 不同,在 cache A 上推断出的混淆规律用不到 cache B |
| `^ swab(ptr_addr)` | **防"对象内复制粘贴"**:同一个真指针 `ptr`,存在对象 X 和对象 Y 里(它们的 `ptr_addr` 不同),编码值完全不同;攻击者把一个对象的 freeptr 字节原样拷到另一个对象,解出来是垃圾 |

`swab` 是"字节序翻转"(`bswap`)——在小端机器上把 `0x0123456789ABCDEF` 翻成 `0xEFCDAB8967452301`。它的作用是把 `ptr_addr` 的位充分打散,让它和 `ptr` 本身(也是一个地址,可能数值上接近)的相关性降到最低。

> **反面对比·不开混淆**:不开 `CONFIG_SLAB_FREELIST_HARDENED` 时,`encoded = (unsigned long)ptr`——内存里存的就是裸指针。攻击者只要读到任何对象的 freeptr 字节,就知道了一个合法的" slab 内对象地址",可以精心伪造一个 freeptr 把分配引向任意地址。开混淆后:
>
> - **读不到真指针**:内存里存的是 `ptr ^ random ^ swab(addr)`,看起来是乱码,攻击者无法直接知道下一个空闲对象在哪。
> - **跨 cache 不可复用**:即使他在某个 cache 上花大力气反推出了 `random`,这个 `random` 也只对这个 cache 有效。
> - **对象间不可复制**:把对象 X 的 freeptr 字节拷到对象 Y,解出来是 `ptr ^ random ^ swab(&X+offset)` 用 `&Y+offset` 算的 swab ——完全不相等,Y 的 freeptr 解码后是垃圾,无法被正常分配使用,反而会触发 BUG_ON。

这个机制不是完美的——如果攻击者能反复触发分配/释放并观察行为,理论上还是能反推。但它把"5 分钟脚本能写出来的 exploit"提升到"需要内核内信息泄露 + 仔细分析"的难度,大幅缩小了可利用漏洞的面。**这是 Linux 内核在 Spectre/Meltdown 之后"纵深防御"思路在 slab 层的体现**——单个机制挡不住所有攻击,但每个机制都让攻击成本上一个台阶。

> **钉死这件事**:SLUB 的 freelist 混淆 `ptr ^ random ^ swab(ptr_addr)` 是 CONFIG_SLAB_FREELIST_HARDENED 开启后的安全增强。它靠 (a) 每 cache 独立 random、(b) 把指针存储地址也混进编码,挡住了"读 freeptr 推断布局""跨 cache 复用""对象间复制粘贴"三类常见堆攻击。开它有微小性能成本(一次异或 + 一次 swab),大多数发行版默认开。

---

## 7.9 ★ 对照第 8 本《内存分配器》

(本章标 ★,这里给轻量对照。完整对照在第 10、21 章。)

如果你读过第 8 本《内存分配器设计与实现深入浅出》,会发现 SLUB 和 tcmalloc/jemalloc 在"小对象分配"这件事上**惊人地相似**——它们是同一个问题(把页/span 切成小对象)在内核态和用户态的两种解:

| 概念 | SLUB(本书·内核态) | tcmalloc/jemalloc(第 8 本·用户态) |
|------|-------------------|-----------------------------------|
| "固定大小对象池" | **kmem_cache**(`task_struct` cache、`kmalloc-64` cache…) | **size class**(`SizeMap::ClassToSize`,8/16/32/48/64…字节一档) |
| 切对象的页/块 | **slab**(buddy 给的若干页) | **span**(PageHeap 给的若干页) |
| 空闲对象怎么串 | **内嵌 freelist**(freeptr 藏对象中间,默认混淆) | **free list**(每 size class 一条,挂 in span 或 thread cache) |
| per-X 缓存 | per-**cpu** partial(第 8 章) | per-**thread** cache(ThreadCache) |
| 安全增强 | `SLAB_FREELIST_HARDENED` 混淆 | 用户态分配器基本不做(进程内信任边界不同) |

最值得品味的对照:**slab 的 `kmem_cache` = tcmalloc 的 size class**。两者都是"为某种固定大小开一个池子"的抽象。差别是——内核的 `kmem_cache` 是**每个对象类型一个**(`task_struct` 一个、`inode` 一个),粒度细;用户态的 size class 是**把所有 8 字节、16 字节的请求归并到几个固定档位**(`kmalloc-64` 类似,但通用 `malloc` 不能预知你要的是什么类型)。第 9 章讲 `kmalloc` 时会细讲这种"通用 size class"模式,第 10 章给完整对照。

另一个对照:**slab 在 buddy 页上切,tcmalloc 在 PageHeap 给的 span 上切**——两者都把"页级管理"和"对象级管理"分了两层。slab 的 freelist 内嵌在对象体内(零额外开销),tcmalloc 的 free list 也是类似思路(对象体内的前几个字节当 next 指针)。**内核态和用户态在"小对象分配"上殊途同归**——这不是巧合,是这个问题的最优解本来就长这样。

---

## 章末小结

这一章我们把 slab 分配器(更准确说是 SLUB,Linux 6.9 的默认实现)的核心拆透了。它不是什么神秘算法,核心就四件事:

1. **`kmem_cache` 抽象**:一种固定大小的对象就建一个 cache,字段 `size`/`inuse`/`offset`/`oo` 决定一切布局,由 `kmem_cache_create`→`calculate_sizes` 算出来。
2. **对象紧凑摆放**:一个 slab(buddy 给的若干页)被切成 `objects` 个等大小槽,相邻对象相距 `s->size` 字节,无对象头。
3. **内嵌 freelist**:空闲对象体内的 `s->offset` 位置(默认是对象中间)临时存"下一个空闲对象"指针,所有空闲对象串成单链,链头在 `slab->freelist`。
4. **分配摘头/释放挂头**:都是 O(1) 的链头操作,SLUB 靠 cmpxchg 做无锁快路径(第 8 章)、靠 local_lock/`__slab_free` 做慢路径回退。

这套设计让"在 buddy 给的页上切小对象"变得既快(O(1) 摘/挂)、又省(内嵌 freelist 零额外开销)、又安全(藏中间 + 混淆双重防护)。这是 mm 把内存**分出去**的第二层,站在 buddy 给的页之上——buddy 管"页级",slab 管"页内对象级"。

本章服务二分法的**分配**那一面:slab 是把小对象**分出去**的核心机制。但它还没解决"怎么快"的问题——本章我们看到的 `___slab_alloc` 慢路径每次分配要拿 local_lock、要扫 freelist,在多 CPU 高并发下锁竞争会成为瓶颈。第 8 章会讲 SLUB 怎么靠 **per-cpu frozen slab + cmpxchg 无锁快路径**把分配做到几乎无锁——这是 SLUB 区别于老 SLAB 的最大性能优势所在。

### 五个"为什么"清单

1. **为什么内核要 slab?buddy 不够吗?** buddy 最少给一页(4KB),而内核到处要几十到几百字节的小对象(`task_struct`、`inode`…),直接用 buddy 内部碎片率 85%~99%。slab 在 buddy 给的页上紧凑摆满同型号对象,把内部碎片降到接近 0。

2. **freelist 指针为什么不额外开数组存,而要藏对象体内?** 额外数组有内存开销(N 个对象多 N×8 字节)和缓存局部性损失(分配时要访问两块内存)。藏体内零开销,且摘头时一次 cache line 命中。代价是对象的几个字节在空闲时归 slab 用、在分配后归用户用——但用户反正不读空闲对象,无冲突。

3. **freeptr 默认藏在对象中间,为什么不是开头或末尾?** 抗堆溢出攻击。藏在中间,小幅越界(相邻对象的 off-by-one)够不到 freeptr;藏在开头/末尾,off-by-one 直接覆盖 freeptr,可被劫持。这是零内存成本的安全增益。特殊情况(有 ctor/RCU/POISON)才放尾部,因为构造函数会覆盖中间。

4. **`CONFIG_SLAB_FREELIST_HARDENED` 那个 `ptr ^ random ^ swab(ptr_addr)` 各部分防什么?** `^ random` 防"跨 cache 复用"(每 cache random 不同);`^ swab(ptr_addr)` 防"对象间复制粘贴"(同一真指针在不同对象地址里编码值不同);整体异或让内存里看不到真指针,挡住"读 freeptr 推断布局"。

5. **`oo` 字段为什么把 order 和对象数挤进一个 unsigned int?** 热路径频繁读,挤进一个 int 意味着一次内存访问同时拿到两个值,省 cache line。低 16 位放对象数(最多 32767,够用),高 16 位放 order(远超 `MAX_PAGE_ORDER=10`)。这是内核"用约束换紧凑"的常见手法。

### 想继续深入往哪钻

- **源码**:
  - [`mm/slab.h`](../linux/mm/slab.h) 的 `struct kmem_cache`(L251)、`struct slab`(L52)、`struct kmem_cache_order_objects`(L244)。
  - [`mm/slub.c`](../linux/mm/slub.c) 的 `struct kmem_cache_cpu`(L384)、`struct kmem_cache_node`(L425)、`freelist_ptr_encode`/`decode`(L479/L492)、`get_freepointer`/`get_freepointer_safe`/`set_freepointer`(L505/L534/L548)、`oo_make`/`oo_order`/`oo_objects`(L591/L601/L606)、`calculate_sizes`(L4954)、`calculate_order`(L4722)、`kmem_cache_open`(L5091)、`__kmem_cache_create`(L5702)、`allocate_slab`(L2322)、`shuffle_freelist`(L2258)、`___slab_alloc`(L3376,慢路径)、`__slab_free`(L4089,释放慢路径)、`do_slab_free`(L4217,释放快路径入口)。
  - [`mm/slab_common.c`](../linux/mm/slab_common.c) 的 `kmem_cache_create`/`kmem_cache_create_usercopy`(L387/L273)、`create_cache`、`calculate_alignment`(L336)。
  - [`include/linux/slab.h`](../linux/include/linux/slab.h) 的 `kmalloc`(L619)、`kmem_cache_zalloc`(L737)、`kmalloc_caches`(L417)——第 9 章详讲。
- **观测**:
  - `cat /proc/slabinfo`——列出所有 `kmem_cache`,显示每个 cache 的 active/total objects、对象大小、每 slab 对象数、order 等。`task_struct`、`inode_cache`、`kmalloc-64` 都能看到。
  - `cat /sys/kernel/slab/<cache-name>/` ——每个 cache 一个目录,里面是详细属性(objects、order、objs_per_slab、offset、size、align、slabs_cpu_partial…),开 CONFIG_SLUB_DEBUG 后还能看 freelist 内容。
  - `slabtop`——`/proc/slabinfo` 的可视化,按占用大小排序。
  - `ftrace` 的 `kmem_cache_alloc`/`kmem_cache_free` tracepoint,看每次分配/释放的 cache 名和对象地址。
- **延伸**:Bonwick 1994 年的论文(*Slab: An Object-Caching Kernel Memory Allocator*,USENIX Summer 1994)是 slab 的起源(SunOS 5.4);Christoph Lameter 2007 年的 SLUB 替换 SLAB 进主线;`Documentation/mm/slub.rst`(内核源码自带)是 SLUB 设计文档。slab vs slub vs slob 三种实现的差异见内核 `mm/Kconfig`。

### 引出下一章

本章讲了 slab 的"静态结构"——`kmem_cache` 字段、对象布局、freelist 怎么串,以及分配/释放"概念上"是摘头/挂头。但我们一直绕开一个问题:**多 CPU 高并发下,这套摘/挂头操作怎么不互相打架?** `___slab_alloc` 的慢路径每次要拿 local_lock,在多核竞争下会成为瓶颈。SLUB 的真正性能优势,来自它的**快路径**——per-cpu frozen slab + `cmpxchg` 双字段原子更新,让分配几乎完全无锁。下一章我们就拆 SLUB 的快慢路径,看它怎么在"无锁"和"正确"之间找到那个精妙的平衡点。
