# 第七章 · quicklist:list 的双层结构与 LZF 压缩

> 篇:P2 数据结构的执掌
> 主轴呼应:这一章是**取向②(内存即数据库)**与**取向④(简单优先 / 折中)**的合奏——用一个"链表套 listpack"的双层结构,同时拿到链表的插入效率和紧凑布局的省内存;再用一个几百行的 LZF 把中间冷数据压扁。每一字节都是数据库本身,所以每一字节都值得抠。

---

## 读完本章你会明白

1. **为什么 list 的底层不是单一结构,而是"链表套 listpack"的双层 quicklist**——因为纯链表每元素两个指针太费内存,纯 listpack 单段太大时插入要 memmove 太慢,两层叠起来各取所长。
2. **quicklist 凭什么让 `LPUSH`/`RPUSH` 还是 O(1),却能装几百万元素**——外层链表负责头尾 O(1) 增删,内层 listpack 负责紧凑省内存,32 字节的节点开销摊到几十上百个元素上几近于无。
3. **为什么 quicklist 的中间节点要压缩、头尾节点永远裸奔**——list 的访问天然偏向两端,把热节点压着等于每次 PUSH 都要解压;中间冷数据压着,空间收益最大、解压代价最少。这是"按访问局部性做空间/时间取舍"的教科书级范例。
4. **LZF 这个压缩算法到底怎么工作,Redis 为什么选它而不是 gzip/zstd**——LZF 是 LZ77 的极简变体,用一张 65536 槽的 hash 表找重复,解压零额外内存、实现才几百行;选它是取向④(简单优先)的活标本。
5. **`SIZE_ESTIMATE_OVERHEAD = 8` 这个常量凭什么定成 8**——它是 listpack entry 头的 worst-case 上界,宁可高估让节点早一点分裂,也不低估破坏边界。

---

> **一句话点破:quicklist 不是"一个聪明的数据结构",而是"两个有缺陷的数据结构叠在一起各取所长"——外层链表换 O(1) 头尾增删,内层 listpack 换紧凑布局,中间冷数据用 LZF 压扁。它没有发明新理论,只是把三种已知的、各有短板的手段拼成一个折中。**

第六章我们看完 listpack 怎么把一段紧凑内存当数组使,它解决了 ziplist 的连锁更新噩梦。但 listpack 有它自己撑不住的场合:整个 list 长到几十万上百万元素时,一段连续内存再紧凑,任意位置插入还是要 memmove 几十 MB,延迟不可接受。Redis 的 list 类型(消息队列、最新动态、操作日志)恰恰要装这种规模。这一章讲 quicklist 怎么把"链表"和"listpack"叠起来,外加一个 LZF 压缩,把这道题解了。

## 7.1 这块要解决什么:既想要 O(1) 头尾增删,又想要紧凑省内存

Redis 的 list 类型(`LPUSH`/`RPUSH`/`LRANGE`/`LINSERT`)是用的最频繁的数据结构之一。它的底层编码必须同时扛住两种相反的访问模式:

- **两端高频增删**:`LPUSH`/`RPUSH`/`LPOP`/`RPOP` 是 O(1) 操作,要求头尾插入几乎零成本。消息队列场景每秒几千上万次 PUSH,头尾必须快。
- **海量元素**:`LRANGE`、`LINSERT` 要按索引取数据,list 还得能装下百万级元素。一段几十 MB 的连续内存随便 memmove,主线程会卡死。
- **内存即数据库**(取向②):Redis 没有磁盘做后盾,每个字节都是常驻内存,指针和碎片都是真金白银。

这三条诉求,在 Redis 演化史上先后踩过两套方案的坑,quicklist 是最终的折中。

**第一套:双向链表 adlist(`OBJ_ENCODING_LINKEDLIST`,7.0 已彻底删除)。** 经典做法,每个元素是一个 `listNode`,内含 `prev`/`next` 两个指针和 `void *value`。优点是头尾插入 O(1)、中间插入只要拿到节点也 O(1)。缺点有三个,且都很致命:

1. **每个元素两个指针**(64 位系统上 16 字节),外加节点结构体本身的开销。存 1 亿个短字符串,光指针就吃掉 1.6 GB。
2. **节点在内存里零散分布**,CPU 缓存行根本 prefetch 不上,`LRANGE` 一扫全是 cache miss。
3. **`void *value` 是指针跳转**,取一个元素要 deref 两次。

> **不这样会怎样**:线程数不是问题,内存才是。纯链表把"省内存"这条彻底放弃了。100 万元素 × 24 字节(两指针 + value 指针 + 节点头) = 24 MB 起步,这还没算 value 自己。一个 list 撑百万元素,几十个这种 list 就把内存撑爆。更要命的是 cache miss——链表节点在堆里东一个西一个,`LRANGE 0 10` 这种范围扫描要十个 cache miss,而 listpack 是顺序扫一个 cache 行。

**第二套:ziplist / listpack 单段紧凑编码(`OBJ_ENCODING_LISTPACK`)。** 把整个 list 压成一段连续内存,元素紧挨着放,没有指针。优点反过来:省内存、缓存友好、`LRANGE` 顺序扫极快。缺点是**单段太大时,任意位置插入仍是 O(N)**,因为它要在一段连续内存里 memmove。第六章我们看过 listpack 修掉了 ziplist 的连锁更新,但 memmove 这条还在——一段 64 KB 的 listpack,中间插一个元素最坏要搬 64 KB,百万级 list 这种搬移会让主线程卡顿到无法接受。

> **不这样会怎样**:如果 list 永远是 listpack,那 list 类型只能装小数据。生产环境里一个消息队列 list 轻松几十万上百万条,每条几 KB,单段 listpack 要几百 MB,每次 LPUSH 在尾部追加还好(append 只动尾巴),但 `LINSERT` 在中间插、或 `LSET` 改中间元素,memmove 几十 MB 是常态。Redis 是单线程,这种 memmove 直接卡住所有其他连接。这条路在生产上行不通。

所以两条单一路线都走死了:链表费内存,listpack 怕大。8.0 的答案是 quicklist——**把"链表"和"listpack"叠起来**:外层是一个双向链表,但每个链表节点不再装一个元素,而是装一整个 listpack(一段紧凑内存,通常装几十到几千个元素)。链表负责灵活的 O(1) 头尾操作,listpack 负责省内存和缓存友好。这就是本章的全部动机。

> **钉死这件事**:quicklist 的本质不是"一个新结构",而是"两个有缺陷的结构叠在一起"。链表的"每元素两指针"被摊薄到"每节点两指针 + 节点内几十元素",listpack 的"单段太大 memmove 贵"被切成"每段最多 8 KB,memmove 局限在段内"。**两条短路的拼起来,各取所长——这是工程折中的最高境界,也是取向④(简单优先)的活标本**。

## 7.2 双层结构:quicklist + quicklistNode

外层是 `quicklist` 结构体([quicklist.h:107](../../redis-8.0.2/src/quicklist.h#L107)):

```c
/* quicklist.h:107-116,64 位上 40 字节 */
typedef struct quicklist {
    quicklistNode *head;
    quicklistNode *tail;
    unsigned long count;        /* 所有 listpack 里元素的总数           */
    unsigned long len;          /* quicklistNode 的个数                */
    signed int fill : QL_FILL_BITS;       /* 单节点的填充策略            */
    unsigned int compress : QL_COMP_BITS; /* 两端不压缩的节点深度;0=关  */
    unsigned int bookmark_count: QL_BM_BITS;
    quicklistBookmark bookmarks[];
} quicklist;
```

注释 [quicklist.h:99](../../redis-8.0.2/src/quicklist.h#L99) 直白:"quicklist is a 40 byte struct (on 64-bit systems)"。注意它只有 40 字节,且 `count` 是**全局总数**——这正是 `LLEN` 能 O(1) 的根本原因:直接返回 `ql->count`,不用遍历所有节点求和。

内层节点 `quicklistNode`([quicklist.h:47](../../redis-8.0.2/src/quicklist.h#L47))是另一段值得逐字段看的设计:

```c
/* quicklist.h:38-59,刻意压到 32 字节 */
typedef struct quicklistNode {
    struct quicklistNode *prev;
    struct quicklistNode *next;
    unsigned char *entry;       /* 指向 listpack / LZF 压缩块 / 单大元素 */
    size_t sz;                  /* entry 占多少字节(未压缩时)          */
    unsigned int count : 16;    /* 本节点里的元素数(< 32k)            */
    unsigned int encoding : 2;  /* RAW=1 / LZF=2                       */
    unsigned int container : 2; /* PLAIN=1(单大元素)/ PACKED=2(listpack)*/
    unsigned int recompress : 1;/* 临时解压、用完待重新压缩            */
    unsigned int attempted_compress : 1;
    unsigned int dont_compress : 1; /* 标记"这次别压我"                */
    unsigned int extra : 9;
} quicklistNode;
```

整段结构被刻意压到 **32 字节**(注释 [quicklist.h:38](../../redis-8.0.2/src/quicklist.h#L38) 明说 "We use bit fields keep the quicklistNode at 32 bytes"):用位域把 encoding/container/recompress 等一堆布尔/枚举塞进一个 `unsigned int`。`count` 只有 16 位够吗?够——因为单个节点最多 64 KB(`optimization_level` 上限),就算每个元素 1 字节也撑不到 65536(注释 [quicklist.h:40](../../redis-8.0.2/src/quicklist.h#L40) "count: 16 bits, max 65536 (max lp bytes is 65k, so max count actually < 32k)")。这是"内存即数据库"取向下,字节都抠到极致的典型。

`entry` 字段是这一切的灵魂:它指向一段 **listpack**(或被 LZF 压缩过的 LZF 块,或单个大元素的裸字节,见 7.4 节)。也就是说,**一个 quicklistNode 内部,装着几十上百个连续紧凑的元素**。外层链表的"指针浪费"被摊薄到了每个节点内部几十个元素上,平均每元素只剩 `32/元素数 + prev/next` 字节的 overhead——元素越多越省。

把这两层画出来,就是 quicklist 的全景:

```text
                  quicklist  (40 字节)
        ┌─────────────────────────────────────────────┐
        │ head                                tail    │
        │  │                                   │     │
        │  ▼                                   ▼     │
        │ count(全局总数)  len(节点数)  fill  compress │
        └──┼────────────────────────────────────┼────┘
           │                                    │
           ▼                                    ▼
   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
   │ quicklistNode│◀──│ quicklistNode│──▶│ quicklistNode│   外层:双向链表
   │  prev/next   │   │  prev/next   │   │  prev/next   │   (每个节点 32 字节)
   │  entry ──────┼─▶ │  entry ──────┼─▶ │  entry ──────┼─▶
   │  sz/count    │   │  sz/count    │   │  sz/count    │
   │  encoding    │   │  encoding=LZF│   │  encoding    │
   │  container   │   │  =PACKED     │   │  container   │
   │  =PACKED     │   │  (中间被压缩)│   │  =PACKED     │
   └──────────────┘   └──────────────┘   └──────────────┘
          │                  │                   │
          ▼                  ▼                   ▼
   ┌────────────────┐ ┌─────────────────┐ ┌────────────────┐
   │   listpack     │ │  LZF 压缩块     │ │   listpack     │
   │ ┌──┬──┬──┬──┐  │ │  (解压时还原    │ │ ┌──┬──┬──┬──┐  │  内层:每节点一段
   │ │e1│e2│e3│e4│  │ │  成 listpack)   │ │ │e1│e2│e3│e4│  │  连续紧凑内存
   │ └──┴──┴──┴──┘  │ │                 │ │ └──┴──┴──┴──┘  │
   └────────────────┘ └─────────────────┘ └────────────────┘
   头节点(裸)         中间节点(压缩)          尾节点(裸)
                      ↑ compress=1 时两端各留 1 个不压
```

这张图是后面所有讨论的舞台:外层链表决定头尾 O(1),内层 listpack 决定省内存和缓存友好,LZF 压缩决定中间冷数据再省一档。

> **钉死这件事**:`quicklistNode` 的 32 字节是"内存即数据库"取向最赤裸的体现——用位域把 encoding(2 bit)、container(2 bit)、recompress(1 bit)、attempted_compress(1 bit)、dont_compress(1 bit)、extra(9 bit)全部塞进一个 `unsigned int`,外加 16 位的 count。如果按"结构体对齐"放开写,这节点至少要 48 字节。一千万个节点省下 160 MB,这就是抠字节的真实收益。

## 7.3 fill:每个节点装多少,有讲究

双层结构的成败系于一个问题:**每个 quicklistNode 内的 listpack 该装多少?** 装太少,外层节点就多,32 字节的节点开销 + prev/next 指针的 cache miss 都会冒头;装太多,单段 listpack 又退回 7.1 节 memmove 的坑。`fill` 字段就是这道题的旋钮。

它分两类语义([quicklist.c:488](../../redis-8.0.2/src/quicklist.c#L488) `quicklistNodeExceedsLimit`、[quicklist.c:462](../../redis-8.0.2/src/quicklist.c#L462) `quicklistNodeNegFillLimit`):

- **`fill >= 0`:按元素个数限制。** 比如 `fill = 128` 就是每节点最多 128 个元素。`fill = 0` 退化为每节点 1 个元素(实际用作测试,生产不会这么配)。
- **`fill < 0`:按字节数限制。** 这是生产默认。`-1` 到 `-5` 对应一张查表([quicklist.c:49](../../redis-8.0.2/src/quicklist.c#L49)):

```c
/* quicklist.c:49 */
static const size_t optimization_level[] = {4096, 8192, 16384, 32768, 65536};
```

即 `fill = -1` → 每节点最多 4 KB,`-2` → 8 KB,以此类推,`-5` → 64 KB。`fill = -5` 是下限,再低也不许(`quicklistSetFill` 在 `fill < -5` 时钳为 -5,[quicklist.c:151](../../redis-8.0.2/src/quicklist.c#L151))。

配置项 `list-max-listpack-size` 就直接喂给 `fill`,默认 `-2`(8 KB)([config.c:3173](../../redis-8.0.2/src/config.c#L3173))。为什么默认 8 KB 而不是 64 KB?**因为单节点越大,中间任意位置插入时的 memmove 越贵;越小,外层节点越多、指针浪费越多。** 8 KB 是经验上的甜点——既保证一段 listpack 不会大到 memmove 卡顿(8 KB 的 memmove 在现代 CPU 上几十纳秒),又保证外层节点数不会爆炸(100 万元素 ÷ 每节点几百元素 = 几千个节点,链表遍历开销可接受)。

此外有一道硬保险 `SIZE_SAFETY_LIMIT = 8192`([quicklist.c:69](../../redis-8.0.2/src/quicklist.c#L69))。看 `quicklistNodeExceedsLimit`([quicklist.c:488](../../redis-8.0.2/src/quicklist.c#L488)):

```c
/* quicklist.c:488-503,精简 */
int quicklistNodeExceedsLimit(int fill, size_t new_sz, unsigned int new_count) {
    if (fill >= 0) {
        /* 按个数限制:但即使个数没到,字节超 SIZE_SAFETY_LIMIT 也算超 */
        return new_count > (unsigned int)fill
            || (new_sz > SIZE_SAFETY_LIMIT && fill > 0);
    } else {
        size_t offset = (-fill) - 1;
        /* 偏移超表则钳到最后一档 */
        if (offset >= sizeof(optimization_level)/sizeof(*optimization_level))
            offset = sizeof(optimization_level)/sizeof(*optimization_level) - 1;
        return new_sz > optimization_level[offset];
    }
}
```

注释 [quicklist.c:64-68](../../redis-8.0.2/src/quicklist.c#L64) 解释了 SIZE_SAFETY_LIMIT 的角色:"Maximum size in bytes of any multi-element listpack. Larger values will live in their own isolated listpacks. ... 8k is a recommended / default size limit"。它兜底了"按个数限制时,元素都很大导致节点总字节爆"的边界——即使 `fill = 128` 允许 128 个元素,只要这 128 个元素总字节超过 8192,也算超限。这是个双重保险。

> **钉死这件事**:`optimization_level[] = {4096, 8192, 16384, 32768, 65536}` 是 2 的幂逐级翻倍。为什么用查表而不是 `4096 * (1 << (-fill-1))`?**因为查表是纯数组下标取值,没有乘法没有移位**,在每次插入都要判的热路径上(`_quicklistNodeAllowInsert` 每次插入都调一次),这就是可见的微优化。同时 5 档上限也直接对应"过大就不该用 listpack"的工程直觉——超过 64 KB 还想紧凑,就该考虑别的编码了。

## 7.4 技巧精解①:插入路径——能塞就塞,塞不下就分裂

`quicklistPushTail` 是最典型的写入路径([quicklist.c:611](../../redis-8.0.2/src/quicklist.c#L611))。它把"能塞就塞,塞不下就分裂,超大单元素独占一节点"三件事压在一个函数里:

```c
/* quicklist.c:611-632 */
int quicklistPushTail(quicklist *quicklist, void *value, size_t sz) {
    quicklistNode *orig_tail = quicklist->tail;
    /* 1. 单个元素就大到塞不进任何节点 → 走 PLAIN 容器,独占一个节点 */
    if (unlikely(isLargeElement(sz, quicklist->fill))) {
        __quicklistInsertPlainNode(quicklist, quicklist->tail, value, sz, 1);
        return 1;
    }
    /* 2. 当前尾节点还能塞?能就在它的 listpack 里 append */
    if (likely(_quicklistNodeAllowInsert(quicklist->tail, quicklist->fill, sz))) {
        quicklist->tail->entry = lpAppend(quicklist->tail->entry, value, sz);
        quicklistNodeUpdateSz(quicklist->tail);
    } else {
        /* 3. 塞不下 → 新建节点,挂到链表尾 */
        quicklistNode *node = quicklistCreateNode();
        node->entry = lpAppend(lpNew(0), value, sz);
        quicklistNodeUpdateSz(node);
        _quicklistInsertNodeAfter(quicklist, quicklist->tail, node);
    }
    quicklist->count++;            /* 全局计数 +1,O(1) */
    quicklist->tail->count++;      /* 节点局部计数 +1 */
    return (orig_tail != quicklist->tail);
}
```

`quicklistPushHead`([quicklist.c:583](../../redis-8.0.2/src/quicklist.c#L583)) 是它的镜像,把 `tail` 换成 `head`、`lpAppend` 换成 `lpPrepend`、`_quicklistInsertNodeAfter` 换成 `_quicklistInsertNodeBefore`,其余一字不差。两端 O(1) 的承诺,就在这两段代码里兑现。

这条路径里有三个细节,每一个都体现了 quicklist 的设计取向:

**细节一:超大的单个元素不进 listpack,而是用 PLAIN 容器独占节点。** 看 `isLargeElement`([quicklist.c:508](../../redis-8.0.2/src/quicklist.c#L508)):

```c
/* quicklist.c:508-514 */
static int isLargeElement(size_t sz, int fill) {
    if (unlikely(packed_threshold != 0)) return sz >= packed_threshold;
    if (fill >= 0)
        return !sizeMeetsSafetyLimit(sz);          /* 按个数限制时看 SIZE_SAFETY_LIMIT */
    else
        return sz > quicklistNodeNegFillLimit(fill); /* 按字节限制时看查表 */
}
```

如果一个 value 自己就比单节点上限还大(比如一个 1 MB 的 blob),塞进 listpack 会撑爆所有按 8 KB 分桶的策略。这时 `__quicklistInsertPlainNode`([quicklist.c:571](../../redis-8.0.2/src/quicklist.c#L571)) 创建一个 `container = QUICKLIST_NODE_CONTAINER_PLAIN` 的节点,直接 `zmalloc(sz)` + `memcpy` 把这个大元素原样放进去——不进 listpack,不参与 LZF 压缩,独占一个节点。

```c
/* quicklist.c:557-569,PLAIN 节点的创建 */
static quicklistNode* __quicklistCreateNode(int container, void *value, size_t sz) {
    quicklistNode *new_node = quicklistCreateNode();
    new_node->container = container;
    if (container == QUICKLIST_NODE_CONTAINER_PLAIN) {
        new_node->entry = zmalloc(sz);             /* 单大元素:裸字节,不进 listpack */
        memcpy(new_node->entry, value, sz);
    } else {
        new_node->entry = lpPrepend(lpNew(0), value, sz);
    }
    new_node->sz = sz;
    new_node->count++;
    return new_node;
}
```

> **钉死这件事**:`container` 字段区分两类节点——`PACKED=2`(正常 listpack,装多个元素)和 `PLAIN=1`(单个大元素,裸字节)。这是 quicklist 对"大小不均的元素"的让步:如果强行把 1 MB 的 blob 塞进 listpack,要么 listpack 撑到 1 MB(回到 memmove 噩梦),要么这个 blob 把整个节点撑爆后所有后续元素都得另起节点。PLAIN 容器让大元素自成一节点,不污染正常的 listpack 节点——**这是"宁可破坏规整,也不破坏性能"的取舍**。

**细节二:`_quicklistNodeAllowInsert` 用估算而非精确字节。** 看 [quicklist.c:516](../../redis-8.0.2/src/quicklist.c#L516):

```c
/* quicklist.c:516-533 */
REDIS_STATIC int _quicklistNodeAllowInsert(const quicklistNode *node,
                                           const int fill, const size_t sz) {
    if (unlikely(!node))
        return 0;
    if (unlikely(QL_NODE_IS_PLAIN(node) || isLargeElement(sz, fill)))
        return 0;
    /* 估算新增这条 entry 后,listpack 会大多少。
     * 宁可高估,最坏让节点比 4k 下限略小几字节,也不低估破坏边界。 */
    size_t new_sz = node->sz + sz + SIZE_ESTIMATE_OVERHEAD;   /* ← 8 字节 overhead */
    if (unlikely(quicklistNodeExceedsLimit(fill, new_sz, node->count + 1)))
        return 0;
    return 1;
}
```

这里用的是 `node->sz + sz + SIZE_ESTIMATE_OVERHEAD(=8)` 而不是精确计算 listpack entry 编码后的长度。注释 [quicklist.c:524-528](../../redis-8.0.2/src/quicklist.c#L524) 写得明白:"We prefer an overestimation, which would at worse lead to a few bytes below the lowest limit of 4k"。多算几字节只会让节点比 4 KB 下限略小,无伤大雅,却省掉了精确计算 listpack entry 头长度的成本——那个计算要判元素是整数还是字符串、整数能不能编码进 4/8 字节、字符串要不要变长编码,每条都要几个分支。在每次插入都判的热路径上,这就是可见的微优化。

`SIZE_ESTIMATE_OVERHEAD = 8` 这个常量([quicklist.c:75](../../redis-8.0.2/src/quicklist.c#L75)) 是 listpack entry 头的 worst-case 上界。注释 [quicklist.c:71-75](../../redis-8.0.2/src/quicklist.c#L71) 解释:"Maximum estimate of the listpack entry overhead. Although in the worst case(sz < 64), we will waste 6 bytes in one quicklistNode, but can avoid memory waste due to internal fragmentation when the listpack exceeds the size limit by a few bytes (e.g. being 16388)"。

**细节三:插入完成后顺手压缩。** `_quicklistInsertNodeAfter`/`Before` 最终都走 `__quicklistInsertNode`([quicklist.c:408](../../redis-8.0.2/src/quicklist.c#L408)),它在结尾调 `quicklistCompress(quicklist, new_node)`([quicklist.c:443](../../redis-8.0.2/src/quicklist.c#L443)) 和 `quicklistCompress(quicklist, old_node)`([quicklist.c:441](../../redis-8.0.2/src/quicklist.c#L441))。这正是下一节 7.6 的入口——每次插入后,顺手检查这个新节点该不该被压缩。

中间插入(`LINSERT`)走的是另一条路径——先定位到目标节点,在它的 listpack 里插;如果插入后这个 listpack 过大,会 `_quicklistSplitNode` 把它从中间劈成两个节点,劈完再 `_quicklistMergeNodes` 尝试和邻居合并。**分裂与合并,是把"链表"的灵活性嫁接到"紧凑编码"上的两把手术刀。** 这条路径代码长且分支多,本章不展开(留给想钻源码的读者),只需记住:quicklist 通过"段内 listpack + 段间链表"的组合,把任意位置插入的代价从"整段 memmove"降到"段内 memmove + 偶尔分裂一个段"。

## 7.5 技巧精解②:LZF 压缩原理——几百行的 LZ77 变体

讲压缩策略(7.6 节)之前,必须先讲清 LZF 这个算法本身怎么工作——这是现有 quicklist 资料里几乎没人讲透的一块,但它是 quicklist 中间节点压缩的物理基础。不讲清它,"quicklist 用 LZF 压缩"就只是一句口号。

LZF 是 Marc Alexander Lehmann 写的一个**极简 LZ77 变体**,Redis 直接把 `lzf_c.c`/`lzf_d.c`/`lzfP.h`/`lzf.h` 四个文件搬进 `src/`,代码总量不到 500 行。它的压缩原理一句话:**用一张小 hash 表找"当前字节序列在前面是否出现过",出现过就输出(距离,长度)引用,否则输出字面字节。** 这就是 LZ77 的核心思想,LZF 是它最精简的实现之一。

先看压缩端 `lzf_compress`([lzf_c.c:108](../../redis-8.0.2/src/lzf_c.c#L108))。它维护一张 hash 表:

```c
/* lzf_c.c:108-117,入口签名 */
NO_SANITIZE("alignment")
size_t
lzf_compress (const void *const in_data, size_t in_len,
              void *out_data, size_t out_len
#if LZF_STATE_ARG
              , LZF_STATE htab
#endif
              )
{
#if !LZF_STATE_ARG
  LZF_STATE htab;    /* 在栈上/函数内分配的 hash 表 */
#endif
```

`LZF_STATE` 是什么?看 [lzfP.h:171](../../redis-8.0.2/src/lzfP.h#L171):

```c
/* lzfP.h:171 */
typedef LZF_HSLOT LZF_STATE[1 << (HLOG)];
```

而 `HLOG = 16`([lzfP.h:55](../../redis-8.0.2/src/lzfP.h#L55)),所以 `LZF_STATE` 是一个 **65536 个槽的数组**。每个槽存一个"之前见过的位置"(在 64 位 Redis 上用 `unsigned int` 存偏移,见 [lzfP.h:165](../../redis-8.0.2/src/lzfP.h#L165) `LZF_USE_OFFSETS` 那段)。

压缩主循环逐字节扫输入,对每个位置算一个 3 字节的 hash,查表看这个 3 字节序列前面有没有出现过:

```c
/* lzf_c.c:149-157,hash 计算与查表(精简) */
hval = FRST (ip);                              /* FRST(p) = (p[0]<<8)|p[1] */
while (ip < in_end - 2) {
    hval = NEXT (hval, ip);                    /* NEXT(v,p) = (v<<8)|p[2] —— 滚动 3 字节 hash */
    hslot = htab + IDX (hval);                 /* IDX 把 hash 折叠进 [0, 65535] */
    ref = *hslot ? (*hslot + LZF_HSLOT_BIAS) : NULL;
    *hslot = ip - LZF_HSLOT_BIAS;              /* 记下"这个 3 字节序列出现在 ip" */

    if (... /* 距离 < MAX_OFF,且 ref 处和 ip 处前 3 字节真相等 */) {
        /* 命中:输出 (距离,长度) 引用 */
    } else {
        /* 未命中:输出字面字节 */
    }
}
```

注意三个关键设计:

**第一,hash 是 3 字节滚动 hash。** `FRST(p) = (p[0]<<8)|p[1]` 取前两字节([lzf_c.c:48](../../redis-8.0.2/src/lzf_c.c#L48)),`NEXT(v,p) = (v<<8)|p[2]` 每往后挪一字节就把 hash 左移 8 位再拼上新字节([lzf_c.c:49](../../redis-8.0.2/src/lzf_c.c#L49))。这样每扫一个新位置,hash 增量更新,不用重新算——这是压缩能跑得快的物理基础。3 字节是因为 LZF 的最短匹配长度也是 3(见下面的编码格式)。

**第二,`IDX(h)` 把 24 位的 hash 折叠进 16 位的表下标。** 看 [lzf_c.c:50-56](../../redis-8.0.2/src/lzf_c.c#L50):

```c
/* lzf_c.c:50-56,VERY_FAST 模式 */
# elif VERY_FAST
#  define IDX(h) ((( h >> (3*8 - HLOG)) - h*5) & (HSIZE - 1))
```

`VERY_FAST = 1`([lzfP.h:64](../../redis-8.0.2/src/lzfP.h#L64)) 是 Redis 用的模式。这个折叠函数 `((h >> 8) - h*5) & 65535` 是一个精巧的整数 hash——既用高位又用低位,还乘个 5 打散,最后 `& (HSIZE-1)` 落进表里。注释 [lzf_c.c:58-65](../../redis-8.0.2/src/lzf_c.c#L58) 说它"works because it is very similar to a multiplicative hash"。

**第三,hash 表只存"每个 3 字节序列最近一次出现的位置"。** 这意味着同一个 hash 槽被后来的位置覆盖——LZF 牺牲了"找最远匹配"的能力,换来 O(1) 查找。这是 LZF 比 gzip/zstd 压缩率低的根因,但也是它解压快、实现短的根因。

命中匹配后,压缩端往两边延伸找最长公共前缀([lzf_c.c:185-215](../../redis-8.0.2/src/lzf_c.c#L185))。注意它用了一个**手写循环展开**——8 行 `len++; if (ref[len] != ip[len]) break;` 连写 16 遍([lzf_c.c:187-208](../../redis-8.0.2/src/lzf_c.c#L187)),再退回普通 `while`。这是为减少分支预测失败的微优化:大多数匹配长度在 3-20 字节之间,前 16 字节手写展开让 CPU 流水线稳定往前推。

找到匹配长度后,LZF 输出的是一个**变长编码的引用**。它的编码格式在源码注释里写得清清楚楚([lzf_c.c:99-106](../../redis-8.0.2/src/lzf_c.c#L99)):

```text
/* lzf_c.c:99-106,压缩格式 */
compressed format

000LLLLL <L+1>               ; literal run, L+1=1..33 字面字节
LLLooooo oooooooo            ; backref L+1=1..7 字节,o+1=1..4096 偏移
111ooooo LLLLLLLL oooooooo   ; backref L+8 字节,o+1=1..4096 偏移
```

三种编码:

1. **字面 run(literal)**:首字节高位 3 位是 000,低 5 位是 L,后跟 L+1 个字面字节(1..33 个)。如果连续 32 个字节都没匹配,LZF 就把它们打成一段字面 run 输出。
2. **短回引(short backref)**:首字节高 3 位是长度(L+1=1..7),低 5 位是偏移高位,第二字节是偏移低位。共 2 字节编码一个"在 1..4096 字节之前、长度 1..7"的引用。
3. **长回引(long backref)**:首字节高 3 位是 111,低 5 位是偏移高位,第二字节是长度(L+8,即 8..263),第三字节是偏移低位。共 3 字节编码一个"长度 8..263"的引用。

为什么这样设计?**为了让小匹配也省空间。** 一个 3 字节的重复序列,用短回引只要 2 字节就编码了(省 1 字节);一个 10 字节的重复,用长回引只要 3 字节(省 7 字节)。LZF 的最大匹配长度是 `MAX_REF = (1<<8) + (1<<3) = 264`([lzf_c.c:76](../../redis-8.0.2/src/lzf_c.c#L76)),最大偏移是 `MAX_OFF = 1<<13 = 8192`([lzf_c.c:75](../../redis-8.0.2/src/lzf_c.c#L75))。这意味着 LZF 只能引用"过去 8 KB 之内"的匹配——这是它压缩率不如 gzip 的另一个原因(gzip 用 32 KB 滑窗),但对 Redis 单个 listpack 节点(最多 64 KB,通常 8 KB)来说,8 KB 偏移已经够覆盖大部分重复模式了。

解压端 `lzf_decompress`([lzf_d.c:59](../../redis-8.0.2/src/lzf_d.c#L59))就更简单了——一个 while 循环,读首字节判类型:

```c
/* lzf_d.c:68-73,解压主循环开头 */
while (ip < in_end) {
    unsigned int ctrl;
    ctrl = *ip++;
    if (ctrl < (1 << 5)) /* literal run: ctrl 是字面字节数-1 */ {
        ctrl++;
        /* ... 直接 memcpy ctrl 个字节 ... */
    }
    else /* back reference */ {
        unsigned int len = ctrl >> 5;       /* 长度编码在高 3 位 */
        u8 *ref = op - ((ctrl & 0x1f) << 8) - 1;
        /* ... */
    }
}
```

注意 `ctrl < (1<<5)` 即首字节高 3 位是 000,判为字面 run;否则高 3 位非零,判为回引。回引的"距离"通过 `(ctrl & 0x1f) << 8` + 下一字节拼出,然后从已解压的输出缓冲往前回 `ref` 字节,复制 `len+2` 字节过来——**纯字节拷贝,没有 hash 表,没有额外内存**。注释 [lzf.h:93](../../redis-8.0.2/src/lzf.h#L93) 写得直白:"This function is very fast, about as fast as a copying loop"(这个函数非常快,几乎和一段拷贝循环一样快)。

解压还有一个细节:lzf_decompress 用 `op - offset` 作为回引的源,这意味着**回引指向的是"已解压的输出",不是"压缩输入"**。这是个聪明的复用——解压端不需要维护额外的"已解压窗口",输出缓冲本身就是窗口。代价是回引长度受 `MAX_OFF = 8192` 限制(只能往回看 8 KB),但这正好和压缩端的限制对称。

现在回答最关键的问题:**Redis 为什么选 LZF,而不是 gzip/zstd?**

| 维度 | LZF | gzip(zlib) | zstd |
|------|-----|------------|------|
| 实现行数 | ~500 行 | ~万行 | 数万行 |
| 解压额外内存 | **零**(就一段输出缓冲) | 几十 KB 滑窗 + Huffman 表 | 几百 KB 字典/表 |
| 压缩速度 | **极快**(滚动 hash + 一次查表) | 慢(构建 Huffman 树) | 快(但比 LZF 复杂) |
| 解压速度 | **极快**(纯字节拷贝) | 中等(Huffman 解码) | 快 |
| 压缩率 | 中等(8 KB 偏移窗 + 3 字节最小匹配) | 好(32 KB 滑窗 + Huffman) | **最好**(有限状态熵编码) |
| 外部依赖 | **零**(4 个 .c/.h 文件) | zlib 库 | libzstd 库 |

Redis 选 LZF 的理由,源码注释 [lzf.h:42-46](../../redis-8.0.2/src/lzf.h#L42) 一语道破:"an extremely fast/free compression/decompression-method ... This algorithm is believed to be patent-free"。三个关键词:**极快**(extremely fast)、**免费**(free, BSD/GPL 双授权)、**无专利**(patent-free)。

> **不这样会怎样(用 gzip 的反面)**:如果 quicklist 用 gzip 压缩中间节点,会发生什么?第一,**解压慢**——gzip 要重建 Huffman 树,每次访问中间节点都要几十微秒,而 list 的中间节点访问(如 `LRANGE 100 200`)是高频操作,延迟立刻可见;第二,**解压要额外内存**——Huffman 表 + 32 KB 滑窗,每个被解压的节点临时分配几十 KB,频繁分配释放,内存碎片严重;第三,**依赖大**——要引入 zlib,Redis 一直以"零外部依赖、纯 C 一份源码"为傲(LZF 是它少数的内嵌第三方算法之一,正因为够小)。LZF 用一半的压缩率,换来 10 倍的解压速度和零额外内存——**对 quicklist 这个场景(数据有局部重复、要频繁解压),这个交换极其划算**。

> **钉死这件事**:LZF 的核心是"3 字节滚动 hash + 65536 槽表 + 变长引用编码"。它牺牲了压缩率(只看 8 KB 窗、只找最近一次匹配),换来了实现极简(500 行)、解压零额外内存、解压几乎和 memcpy 一样快。Redis 选它而非 gzip/zstd,是取向④(简单优先)在压缩算法层的具体落地——**对 list 这个"数据有局部重复、要频繁解压"的场景,LZF 的"轻"比 gzip 的"压得狠"重要得多**。

## 7.6 技巧精解③:两端不压中间压——按访问频率分层

讲清 LZF 算法后,quicklist 最有想法的一招才能讲透:**节点级压缩,且只压中间,两端裸奔**。

quicklist 的节点级压缩用 `quicklistLZF` 结构存压缩后的数据([quicklist.h:66](../../redis-8.0.2/src/quicklist.h#L66)):

```c
/* quicklist.h:61-69,8+N 字节 */
typedef struct quicklistLZF {
    size_t sz;          /* 压缩后字节数 */
    char compressed[];  /* LZF 压缩数据(柔性数组) */
} quicklistLZF;
```

注意一个巧妙的复用,注释 [quicklist.h:64](../../redis-8.0.2/src/quicklist.h#L64) 明说:"uncompressed length is stored in quicklistNode->sz"。**`node->sz` 永远存未压缩的字节数**,压缩后真正占多少由 `quicklistLZF->sz` 存。这样解压时 `zmalloc(node->sz)` 直接拿到目标缓冲区大小,不用额外字段记录原长度。压缩前后节点内存布局的对比:

```text
压缩前(RAW,listpack):
quicklistNode { entry ──────────────────────┐ }
                                          │
                                          ▼
              ┌─────────────────────────────────────┐
              │ listpack header + e1 e2 e3 ... + EOF │  node->sz = N(未压缩)
              └─────────────────────────────────────┘
              encoding = RAW(1),  container = PACKED(2)

压缩后(LZF):
quicklistNode { entry ──────────────────────┐ }
                                          │
                                          ▼
              ┌─────────────────────────────────────┐
              │ quicklistLZF { sz=M; compressed[M] } │  node->sz 仍是 N(未压缩,供解压分配)
              └─────────────────────────────────────┘  quicklistLZF->sz = M(压缩后实际占)
              encoding = LZF(2), container = PACKED(2)
```

压缩函数 `__quicklistCompressNode`([quicklist.c:214](../../redis-8.0.2/src/quicklist.c#L214))有几个硬门槛,每一条都是血泪换来的工程约束:

```c
/* quicklist.c:214-244 */
REDIS_STATIC int __quicklistCompressNode(quicklistNode *node) {
#ifdef REDIS_TEST
    node->attempted_compress = 1;
#endif
    if (node->dont_compress) return 0;              /* ① 被临时保护,跳过 */

    /* validate that the node is neither tail nor head */
    assert(node->prev && node->next);               /* ② 头尾节点永远不许压 */

    node->recompress = 0;
    if (node->sz < MIN_COMPRESS_BYTES) return 0;    /* ③ 太小(< 48 字节)不值得 */

    quicklistLZF *lzf = zmalloc(sizeof(*lzf) + node->sz);

    /* ④ 压完收益不够(< 8 字节)就放弃 */
    if (((lzf->sz = lzf_compress(node->entry, node->sz, lzf->compressed,
                                 node->sz)) == 0) ||
        lzf->sz + MIN_COMPRESS_IMPROVE >= node->sz) {
        zfree(lzf);
        return 0;
    }
    lzf = zrealloc(lzf, sizeof(*lzf) + lzf->sz);
    zfree(node->entry);
    node->entry = (unsigned char *)lzf;
    node->encoding = QUICKLIST_NODE_ENCODING_LZF;
    return 1;
}
```

四个硬门槛:

- **① `dont_compress` 标志位为真就跳过**。某些操作(比如即将修改这个节点)会临时把这个标志置 1,避免反复压/解压。
- **② `assert(node->prev && node->next)`——头尾节点永远不许压缩**。这是 quicklist 最重要的不变式,7.7 节会展开。
- **③ 节点太小(`< MIN_COMPRESS_BYTES = 48`,[quicklist.c:78](../../redis-8.0.2/src/quicklist.c#L78))不值得压**。压缩本身要分配 `quicklistLZF` 头(8 字节)+ 压缩数据,48 字节以下压缩收益盖不过这 8 字节头。
- **④ 压完收益不够(`< MIN_COMPRESS_IMPROVE = 8` 字节,即压完没省到 8 字节就放弃,[quicklist.c:83](../../redis-8.0.2/src/quicklist.c#L83))**。这是为了不让"压完反而更大"的退化数据(随机字节压不动)占内存——压缩率不到 1,就当没压。

核心策略在 `__quicklistCompress`([quicklist.c:307](../../redis-8.0.2/src/quicklist.c#L307))。配置项 `list-compress-depth`(默认 0=关,[config.c:3195](../../redis-8.0.2/src/config.c#L3195))表示"两端各留多少个节点不压缩"。设为 1 就是头尾各 1 个裸节点,其余全压;设为 2 就是各 2 个。源码逻辑很直白:

```c
/* quicklist.c:307-378,精简 */
REDIS_STATIC void __quicklistCompress(const quicklist *quicklist,
                                      quicklistNode *node) {
    if (quicklist->len == 0) return;

    /* 头尾的 recompress 必须是 0(永远不压缩头尾) */
    assert(quicklist->head->recompress == 0 && quicklist->tail->recompress == 0);

    /* 节点总数 < compress*2 时,两端"保护区"已覆盖整个链表,无可压节点 */
    if (!quicklistAllowsCompression(quicklist) ||
        quicklist->len < (unsigned int)(quicklist->compress * 2))
        return;

    quicklistNode *forward = quicklist->head;
    quicklistNode *reverse = quicklist->tail;
    int depth = 0;
    int in_depth = 0;
    while (depth++ < quicklist->compress) {
        quicklistDecompressNode(forward);           /* 两端保护区内强制解压 */
        quicklistDecompressNode(reverse);
        if (forward == node || reverse == node)
            in_depth = 1;
        /* 两端指针相遇或相邻 → 整个链表都在保护区,无可压 */
        if (forward == reverse || forward->next == reverse)
            return;
        forward = forward->next;
        reverse = reverse->prev;
    }

    if (!in_depth)
        quicklistCompressNode(node);                /* node 在中间,压它 */

    /* 此时 forward/reverse 刚走出保护区,它们也该被压 */
    quicklistCompressNode(forward);
    quicklistCompressNode(reverse);
}
```

把这段逻辑画成图(设 `compress = 1`):

```text
list-compress-depth = 1(两端各留 1 个节点不压):

   head                                              tail
     │                                                 │
     ▼                                                 ▼
  ┌──────┐  ┌──────┐  ┌──────┐  ┌──────┐  ┌──────┐  ┌──────┐
  │ 裸   │─▶│ LZF  │─▶│ LZF  │─▶│ LZF  │─▶│ LZF  │─▶│ 裸   │
  │listpk│◀─│压 缩 │◀─│压 缩 │◀─│压 缩 │◀─│压 缩 │◀─│listpk│
  └──────┘  └──────┘  └──────┘  └──────┘  └──────┘  └──────┘
   ↑ 保护                                            ↑ 保护
   ↑ LPUSH/RPOP 直到这里,不用解压                   ↑ RPUSH/LPOP 直到这里
   ↑ 中间冷数据全压着,只有遍历时才解压
```

**为什么是"两端不压、只压中间"?** 因为 list 的访问天然偏向两端:

- `LPUSH`/`RPUSH`/`LPOP`/`RPOP` 全打在头尾,这是 list 最高频的操作。
- `LRANGE 0 10`、`LRANGE -10 -1` 也基本是看头尾(消息队列看最新 N 条、看最旧 N 条)。
- 中间节点只有在 `LRANGE` 大范围扫描、或 `LINSERT`/`LSET` 指定索引时才碰到,频率低得多。

> **不这样会怎样(全压的反面)**:如果两端也压着,每次 `LPUSH` 都要先 `lzf_decompress` 头节点(几十微秒)、改 listpack、再 `lzf_compress` 压回去(又是几十微秒)。一个 PUSH 操作原本只要几十纳秒(`lpPrepend` 在 8 KB listpack 上是常数级),现在被压解压拖到几十微秒,慢了 1000 倍。消息队列场景每秒几千次 PUSH,主线程直接卡死。**把热节点压着,等于拿 CPU 换回了本来就该省的内存——亏到姥姥家。**

> **不这样会怎样(全不压的反面)**:如果完全不压,百万级 list 几千个节点 × 8 KB = 几十 MB 全裸放着。对 Redis 这个"内存即数据库"来说,几十 MB 是真金白银。尤其 list 数据(日志、消息)重复度高,LZF 通常能压到 30-50%,省下的内存够再装一个 list。**中间冷数据压着,空间收益最大、解压代价最少——这是教科书级的"按访问局部性做空间/时间取舍"。**

> **钉死这件事**:quicklist 的压缩策略是"两端 N 个节点裸奔,中间全压"。这不是拍脑袋,而是算清了 list 的访问模式——两端是热点(PUSH/POP/头部 LRANGE),中间是冷数据。`list-compress-depth` 默认 0(关),因为开压缩有解压延迟,默认保守;但生产环境如果 list 很长且重复度高,设成 1 是显著省内存的甜点。**这是"按访问频率分层"思想在数据结构层的落地——热数据用 CPU 换时间,冷数据用时间换空间,各得其所。**

## 7.7 临时解压与 recompress:用完请把我压回去

被压缩的中间节点偶尔要被读写(比如 `LRANGE 100 200` 要扫到中间、`LSET mylist 500 newval` 要改中间元素)。这时 `quicklistDecompressNodeForUse` 会解压并把 `node->recompress = 1`([quicklist.c:284](../../redis-8.0.2/src/quicklist.c#L284)),意思是"我临时解开了,用完请把我压回去":

```c
/* quicklist.c:284-290 */
#define quicklistDecompressNodeForUse(_node)                                   \
    do {                                                                       \
        if ((_node) && (_node)->encoding == QUICKLIST_NODE_ENCODING_LZF) {     \
            __quicklistDecompressNode((_node));                                \
            (_node)->recompress = 1;           /* 标记"待重压" */              \
        }                                                                      \
    } while (0)
```

操作完成后,`quicklistRecompressOnly` 宏([quicklist.c:398](../../redis-8.0.2/src/quicklist.c#L398)) 检查这个标志,有就重新压回去:

```c
/* quicklist.c:398-402 */
#define quicklistRecompressOnly(_node)                                         \
    do {                                                                       \
        if ((_node)->recompress)                                               \
            quicklistCompressNode((_node));    /* 直接压,不再判 depth */      \
    } while (0)
```

注意一个细节:`quicklistRecompressOnly` **不再检查节点是否在"两端保护区"内**(对比 `quicklistCompress` 宏 [quicklist.c:389](../../redis-8.0.2/src/quicklist.c#L389),后者会调 `__quicklistCompress` 走 depth 判定)。因为 `recompress=1` 的节点原本就是从压缩态临时解开的,它"本来就该被压回去",再判 depth 是浪费——这条快路径是性能关键。

`quicklistDelRange`([quicklist.c:1160](../../redis-8.0.2/src/quicklist.c#L1160)) 就是这套机制的用户之一。删除中间节点里的部分元素时:

```c
/* quicklist.c:1223-1234,DelRange 的节点内删除分支 */
if (delete_entire_node || QL_NODE_IS_PLAIN(node)) {
    __quicklistDelNode(quicklist, node);           /* 删整个节点 */
} else {
    quicklistDecompressNodeForUse(node);           /* 临时解开,标 recompress=1 */
    node->entry = lpDeleteRange(node->entry, offset, del);
    quicklistNodeUpdateSz(node);
    node->count -= del;
    quicklist->count -= del;
    quicklistDeleteIfEmpty(quicklist, node);
    if (node)
        quicklistRecompressOnly(node);             /* 用完,压回去 */
}
```

这段代码体现了 quicklist 对中间节点的"按需解压"流程:**临时解 → 改 → 重新压**。整个过程中,这个节点在内存里短暂地以 listpack 形态存在(被改),改完立刻压回 LZF。这就是取向②(内存即数据库)的极致——内存永远只给"正在用的数据"留位,用完立刻收回。

一个**绝对不能踩**的坑:头尾节点的 `recompress` 永远是 0。`__quicklistCompress` 开头就 assert 这点([quicklist.c:312](../../redis-8.0.2/src/quicklist.c#L312)):

```c
/* quicklist.c:312 */
assert(quicklist->head->recompress == 0 && quicklist->tail->recompress == 0);
```

注释 [quicklist.c:382-388](../../redis-8.0.2/src/quicklist.c#L382)(在 `quicklistCompress` 宏上方)解释了为什么:"it's important to ensure that the 'recompress' flag of head and tail is always false, as we always assume that head and tail are not compressed"。因为代码里很多地方假设"头尾总是裸的、可直接 `lpAppend`/`lpPrepend`"——`quicklistPushHead`/`quicklistPushTail` 直接对 `quicklist->head->entry`/`tail->entry` 操作,如果头尾被标成待重压(意味着它当前是解开的,但理论上该被压),就会触发逻辑混乱。**这个 invariant 是整套压缩机制能正确运转的地基。**

> **钉死这件事**:quicklist 的压缩是"数据可能压缩存放、用时才解压"的 lazy 思想——中间节点长期以 LZF 形态待在内存里,只在被访问时临时解开(`recompress=1`),改完立刻压回去。`recompress` 标志只用于"原本就该压缩、只是临时解开"的中间节点,**头尾节点永远 `recompress=0`,因为它们永远裸着**。这条 invariant 被 assert 死守,是 quicklist 正确性的隐形地基。

## 7.8 技巧精解④:几个散点——为何 node 内是 listpack、估算 overhead、bookmark

**为何 node 内是 listpack 而非 ziplist?** 呼应第六章:ziplist 的每个 entry 头里存了"前一 entry 的长度",前一 entry 长度变化会引发后一 entry 头连锁更新,最坏 O(N²)。quicklist 的节点会被频繁增删(LPUSH/RPUSH/LINSERT 都在动节点内的 listpack),如果用 ziplist,频繁增删会触发频繁的连锁更新,延迟不可控。**listpack 修掉了连锁更新**(每个 entry 头只存自己的长度,不依赖前一个),用在 quicklist 节点这种高频增删场景,稳定性远胜 ziplist。这是 7.0 之后 Redis 把所有 ziplist 换成 listpack 的根本原因,quicklist 是受益者之一。

**`SIZE_ESTIMATE_OVERHEAD = 8` 的取舍。** 插入判定时给每条新增 entry 预留 8 字节 overhead([quicklist.c:529](../../redis-8.0.2/src/quicklist.c#L529))。这是 listpack entry 头的 worst-case 上界。宁可高估让节点早一点分裂,也不低估让节点超出物理边界——**在"宁可浪费几字节、绝不破坏不变式"和"尽量塞满"之间,Redis 一贯选前者**,这正是取向④(简单优先 / 可靠性)的具体落地。注释 [quicklist.c:71-75](../../redis-8.0.2/src/quicklist.c#L71) 算得清清楚楚:"in the worst case(sz < 64), we will waste 6 bytes in one quicklistNode, but can avoid memory waste due to internal fragmentation"。

**`bookmark` 是干什么的?** 看 [quicklist.h:71-78](../../redis-8.0.2/src/quicklist.h#L71) 的注释:"Bookmarks are padded with realloc at the end of the quicklist struct. They should only be used for very big lists if thousands of nodes were the excess memory usage is negligible, and there's a real need to iterate on them in portions"。bookmark 是给"超大 list"用的——一个百万节点的 list,要分批迭代(比如 `LRANGE` 分页),bookmark 让你记住"上次迭代到哪个节点了",下次直接跳过去,不用从头扫。代价是每次节点删除要搜 bookmark 列表更新。所以注释强调"only be used for very big lists"——小 list 用不上,白白增加删除开销。这是个"为极端场景留的口子",日常用不到。

**`MIN_COMPRESS_IMPROVE = 8` 的取舍。** 压完没省到 8 字节就放弃([quicklist.c:234](../../redis-8.0.2/src/quicklist.c#L234))。这 8 字节是 `quicklistLZF` 头(8 字节 `size_t sz`)的代价——如果压缩省下的还不够盖过这个头,那压了反而更大。这是个"压缩的盈亏平衡点"。

## 7.9 quicklist vs 纯链表 vs 纯 listpack:三者放一起比

把"装海量元素 + 两端 O(1) 增删 + 省内存"这道题,换成三种候选,看取舍:

| 维度 | 纯链表(adlist) | 纯 listpack | quicklist |
|------|----------------|-------------|-----------|
| 头尾 O(1) 增删 | **O(1)** | 头尾 append/prepend O(1),但超阈值要 realloc | **O(1)**(外层链表) |
| 中间插入 | **O(1)**(拿到节点的话) | O(N)(单段 memmove) | O(段内元素数 + 偶尔分裂段) |
| 每元素内存 overhead | 高(两指针 + 节点头 ≈ 24 B) | **最低**(只一个 entry 头) | 低(节点头摊薄到段内几十元素) |
| 缓存友好性 | 差(节点零散) | **最好**(整段连续) | 好(段内连续,段间跳) |
| 海量元素(百万级) | 内存爆 | memmove 卡顿 | **稳** |
| 中间冷数据压缩 | 不能 | 整段一起压,改一个全解 | **段级压缩,只解要改的段** |

Redis 选 quicklist,**不是因为它在某个维度上赢,而是因为它把三个维度的"够用"同时做到了**:

- 头尾 O(1) 增删:链表保证。
- 省内存:listpack 保证(每元素 overhead 极低)。
- 海量元素不卡顿:外层链表把单段限制在 8 KB,memmove 局限在段内。
- 冷数据压缩:LZF 在段级压缩,改一个段只解一个段。

> **钉死这件事**:quicklist 的精髓是"分摊"和"分层"。单看 `quicklistNode`,32 字节加两个指针,装一个元素时比 adlist 还浪费。但它的设计前提是**每个节点至少装几十个元素**——把 32 字节 overhead 摊到 100 个元素上,每元素只剩 0.32 字节,接近 listpack 的紧凑度;同时头尾插入还是 O(1)(只动外层链表),随机访问只需先在链表上跳 O(节点数)再在 listpack 上 O(节点内元素数)。**链表负责灵活,listpack 负责省内存,LZF 负责把冷数据再省一档,各司其职**。

**对比 Java `LinkedList`:** Java 的 `LinkedList<E>` 每个元素都是一个 `Node<E>` 对象,内含 `item`/`prev`/`next` 三个引用外加对象头,64 位 JVM 上一个空节点就 48 字节。100 万元素的链表,光节点结构体就 48 MB,且全散在堆里,GC 扫描和缓存命中都受罪。quicklist 用"节点内部 listpack"这一层,直接把同样 100 万元素压进几百个连续内存段,内存省一个数量级,缓存命中率天差地别。**这是"为 in-memory 数据库量身定做"和"通用语言库"在数据结构选择上的根本分野。**

## 章末:回扣、五个为什么、往哪钻

### 主线回扣

本章是**取向②(内存即数据库)**与**取向④(简单优先 / 折中)**的合奏。quicklist 没有发明新理论,它只是把三种已知的、各有短板的手段拼成一个折中:

- **取向②**:外层 `quicklistNode` 抠到 32 字节、`count` 用 16 位位域、LZF 压缩中间节点、`MIN_COMPRESS_IMPROVE = 8` 连 8 字节收益都不放过,全是为了把内存省下来。Redis 没有磁盘做后盾,每个字节都是数据库本身。
- **取向④**:quicklist 本身就是"链表 vs listpack"的折中产物。`fill` 用查表、判定用估算 overhead、LZF 选轻量算法(500 行而非 gzip 的万行),都是"够用、简单、可预测"胜过"精确但复杂"的选择。LZF 的选型更是这条取向的活标本——宁可压缩率不如 gzip,也要解压快、实现短、零外部依赖。
- **取向③(编码自适应)**:listpack 与 quicklist 之间双向自动转换([t_list.c:23](../../redis-8.0.2/src/t_list.c#L23) `listTypeTryConvertListpack` 升级、[t_list.c:67](../../redis-8.0.2/src/t_list.c#L67) `listTypeTryConvertQuicklist` 降级),用户完全无感——小 list 用紧凑的 listpack,大 list 自动升级 quicklist,缩回去再降级(降级阈值减半避免反复横跳)。

### 五个为什么

**Q1:为什么默认 `list-compress-depth = 0`(关压缩),而不是默认开?**

开压缩有解压延迟。中间节点被访问时要 `lzf_decompress`(几十微秒),虽然比 gzip 快得多,但仍是开销。Redis 默认保守——大多数 list 不会大到压一下能省很多内存,默认关压缩让访问路径零额外开销。生产环境如果 list 很长(几十万节点)且数据重复度高(日志、消息),设成 1 通常能省 30-50% 内存,这点解压延迟值得换。

**Q2:LZF 的 hash 表 65536 槽,会不会很大?**

会,但它在栈上分配,压缩完就释放(`lzf_compress` 返回后 `htab` 出作用域)。每次压缩一个节点临时用一下,不在堆上长期占用。注释 [lzfP.h:48-50](../../redis-8.0.2/src/lzfP.h#L48) 说:"Size of hashtable is (1 << HLOG) * sizeof (char *)",64 位上 65536 × 8 = 512 KB 临时栈空间——这个数字看着大,但 Redis 单线程,栈够用,且只在压缩瞬间存在。

**Q3:`quicklistNode` 的 `sz` 永远存未压缩字节数,解压时怎么知道压完是多少字节?**

看 `quicklistLZF` 结构([quicklist.h:66](../../redis-8.0.2/src/quicklist.h#L66))——它有自己的 `sz` 字段存压缩后字节数。所以压缩后一个节点有两份大小信息:`node->sz`(未压缩,供解压分配缓冲)+ `quicklistLZF->sz`(压缩后,实际占用)。这个冗余是刻意的——解压时 `zmalloc(node->sz)` 一步到位,不用反推。

**Q4:LPUSH 一个超大元素(1 MB),会怎样?**

走 PLAIN 容器分支。`isLargeElement`([quicklist.c:508](../../redis-8.0.2/src/quicklist.c#L508)) 判定它超过节点上限,`__quicklistInsertPlainNode`([quicklist.c:571](../../redis-8.0.2/src/quicklist.c#L571)) 创建一个 `container = PLAIN` 的节点,直接 `zmalloc(1MB)` + `memcpy` 把它原样放进去,不进 listpack、不参与 LZF 压缩。这个节点独占 1 MB 内存,是 quicklist 对"大小不均元素"的让步——宁可让大元素自成一节点,也不污染正常 listpack 节点的紧凑布局。

**Q5:LZF 解压时怎么处理"回引指向还未解压的位置"?**

不会发生。LZF 的回引指向的是"已解压的输出缓冲"(解压代码 `u8 *ref = op - offset`,[lzf_d.c:111](../../redis-8.0.2/src/lzf_d.c#L111)),而压缩端保证回引的距离 ≤ `MAX_OFF = 8192` 且只指向"过去"(ref < op)。所以解压顺序扫输入,每解一个回引,它指向的位置必然已经解出来。这是 LZ77 族算法的共同保证——回引永远指向"过去",解压是单向流式的。

### 想继续深入往哪钻

- 想看中间插入(`LINSERT`)的完整路径:读 [quicklist.c](../../redis-8.0.2/src/quicklist.c) 的 `quicklistInsertAfter`/`quicklistInsertBefore`([quicklist.c:1030](../../redis-8.0.2/src/quicklist.c#L1030) 附近),以及 `_quicklistSplitNode`(节点分裂)、`_quicklistMergeNodes`(节点合并)两个内部函数。这是 quicklist 最复杂的代码段。
- 想理解 listpack ↔ quicklist 的双向切换:读 [t_list.c](../../redis-8.0.2/src/t_list.c) 的 `listTypeTryConvertListpack`([t_list.c:23](../../redis-8.0.2/src/t_list.c#L23),升级)和 `listTypeTryConvertQuicklist`([t_list.c:67](../../redis-8.0.2/src/t_list.c#L67),降级,缩到阈值一半才降,避免反复横跳)。
- 想看 LZF 在极端输入下的行为:读 [lzf_c.c](../../redis-8.0.2/src/lzf_c.c) 的 `lzf_compress` 出口([lzf_c.c:283-300](../../redis-8.0.2/src/lzf_c.c#L283),处理尾部剩余字面字节、`lit == MAX_LIT` 时切段)。
- 想了解 LZ77 族算法的全谱:对比 gzip(zlib,32 KB 滑窗 + Huffman 熵编码)、LZ4(极快,类似 LZF 但更激进)、zstd(FSE 熵编码,压缩率最好)。LZF 是这条谱上"最简最轻"的一端。

### 引出下一章

至此 list 类型的底层我们讲完了:小 list 用 listpack(第六章),大 list 用 quicklist(本章)。list 的核心矛盾是**顺序访问 vs 随机访问、紧凑 vs 灵活**的折中,quicklist 用双层结构 + LZF 压缩给出了答案。下一章我们要面对的是另一种全然不同的诉求:**带权重的有序集合**——既要按 score 排好序,又要按成员名 O(log N) 查,还要支持范围扫描。quicklist 的双层结构救不了这种场景,我们需要一种能同时把"有序"和"快速定位"装进一个结构里的东西。

那就是 zset 的底层之一:**skiplist(跳跃表)**。它用概率代替平衡、用多级索引代替树形旋转,在 Redis 里和 dict 配合,撑起了 `ZADD`/`ZRANGEBYSCORE`/`ZRANK` 这一整套 API。我们下一章见。

---

## 验证物:如何亲手确认本章的设计

> 说明:本书写作环境为 Windows,无法直接运行 redis-server。以下 (1) gdb 断点脚本 (2) 源码常量锚点 (3) OBJECT ENCODING 观察项 均为可复现的精确指引,供读者在 Linux 环境对 redis-8.0.2 源码 `make no-opt` 编译后自行验证。**本书不附编造的运行输出**——凡未实跑的,只给脚本、预期观察变量与推导依据,不写具体数值。

### 1. gdb 断点脚本

编译:`cd redis-8.0.2 && make no-opt`
启动:`gdb ./src/redis-server`,另一终端 `redis-cli`。

```gdb
(gdb) break quicklistPushTail       # 尾部插入主路径,quicklist.c:611
(gdb) break quicklistPushHead       # 头部插入主路径,quicklist.c:583
(gdb) break _quicklistNodeAllowInsert # 节点容量判定,quicklist.c:516
(gdb) break __quicklistCompressNode  # 单节点压缩入口,quicklist.c:214
(gdb) break __quicklistCompress      # 两端不压中间压策略,quicklist.c:307
(gdb) break lzf_compress             # LZF 压缩本体,lzf_c.c:108
(gdb) break lzf_decompress           # LZF 解压本体,lzf_d.c:59
(gdb) break quicklistDelRange        # 范围删除(含临时解压/重压),quicklist.c:1160
(gdb) run --port 6379

# redis-cli 执行:RPUSH mylist a b c d e(连续 PUSH,观察分裂)
# gdb 在 quicklistPushTail 停下:
(gdb) print quicklist->tail->sz      # 预期:当前尾节点 listpack 字节数
(gdb) print quicklist->tail->count   # 预期:当前尾节点元素数
(gdb) print quicklist->count         # 预期:全局总数
(gdb) print quicklist->fill          # 预期:-2(默认)
# 单步到 _quicklistNodeAllowInit 内,观察估算:
(gdb) print new_sz                   # 预期:node->sz + sz + 8(SIZE_ESTIMATE_OVERHEAD)

# 大量 PUSH 触发压缩(list-compress-depth 设为 1):
# (redis-cli) CONFIG SET list-compress-depth 1
# (redis-cli) 循环 RPUSH 到几千元素,gdb 在 __quicklistCompress 停下:
(gdb) print quicklist->compress      # 预期:1
(gdb) print node->encoding           # 预期:1(RAW)→ 压缩后变 2(LZF)
(gdb) print node->sz                 # 预期:未压缩字节数(压缩后仍存原值)
# 进 lzf_compress 后,ip 指向 listpack 数据,htab 是 65536 槽:
(gdb) print sizeof(htab)             # 预期:65536 × sizeof(LZF_HSLOT)
```

**预期观察**(基于源码 [quicklist.c:307-378](../../redis-8.0.2/src/quicklist.c#L307) 的压缩策略,本书未实跑):`list-compress-depth = 1` 时,head 和 tail 节点的 `encoding` 恒为 1(RAW),`recompress` 恒为 0;中间节点的 `encoding` 为 2(LZF)。`LRANGE` 扫到中间节点时,`quicklistDecompressNodeForUse` 会把它临时解开(`recompress` 置 1),扫完 `quicklistRecompressOnly` 把它压回去(`recompress` 归 0)。

### 2. 源码常量锚点(带行号,从 redis-8.0.2 源码 Grep 核实)

| 常量/字段 | 位置 | 值/说明 |
|----------|------|---------|
| `optimization_level[]` | quicklist.c:49 | {4096,8192,16384,32768,65536},fill=-1..-5 的字节上限 |
| `SIZE_SAFETY_LIMIT` | quicklist.c:69 | 8192(按个数限制时的字节兜底) |
| `SIZE_ESTIMATE_OVERHEAD` | quicklist.c:75 | 8(插入判定的高估 overhead) |
| `MIN_COMPRESS_BYTES` | quicklist.c:78 | 48(小于此值不压) |
| `MIN_COMPRESS_IMPROVE` | quicklist.c:83 | 8(压完没省 8 字节就放弃) |
| `HLOG` | lzfP.h:55 | 16(hash 表 65536 槽) |
| `VERY_FAST` | lzfP.h:64 | 1(Redis 用的 LZF 模式) |
| `MAX_LIT` / `MAX_OFF` / `MAX_REF` | lzf_c.c:74-76 | 32 / 8192 / 264(LZF 编码上限) |
| LZF 压缩格式注释 | lzf_c.c:99-106 | 三种编码:字面 run / 短回引 / 长回引 |
| `lzf_compress` | lzf_c.c:108 | LZF 压缩入口 |
| `lzf_decompress` | lzf_d.c:59 | LZF 解压入口("as fast as a copying loop") |
| `quicklistNode`(32 字节) | quicklist.h:47-59 | prev/next/entry/sz/count16/encoding2/container2/recompress1/... |
| `quicklist`(40 字节) | quicklist.h:107-116 | head/tail/count/len/fill/compress/bookmark_count |
| `quicklistLZF` | quicklist.h:66-69 | sz + compressed[](node->sz 存未压缩长度) |
| `__quicklistCompressNode` | quicklist.c:214 | 四门槛:dont_compress/头尾assert/<48/<8字节收益 |
| `__quicklistCompress` | quicklist.c:307 | 两端 N 个裸,中间全压;头尾 recompress 必须 0 |
| `quicklistPushTail` | quicklist.c:611 | 大元素→PLAIN,塞得下→lpAppend,塞不下→新节点 |
| `quicklistPushHead` | quicklist.c:583 | PushTail 的镜像(lpPrepend + InsertNodeBefore) |
| `quicklistDelRange` | quicklist.c:1160 | 删整节点 vs 段内删(临时解压/重压) |
| `_quicklistNodeAllowInsert` | quicklist.c:516 | new_sz = node->sz + sz + 8(估算) |
| `isLargeElement` | quicklist.c:508 | 超限 → PLAIN 容器 |
| `list-max-listpack-size` 默认 | config.c:3173 | -2(每节点 8 KB) |
| `list-compress-depth` 默认 | config.c:3195 | 0(关压缩) |
| listpack→quicklist 升级 | t_list.c:23 | listTypeTryConvertListpack(超 quicklistNodeExceedsLimit 触发) |
| quicklist→listpack 降级 | t_list.c:67 | listTypeTryConvertQuicklist(shrinking 时阈值减半) |

### 3. OBJECT ENCODING 观察项(需本地 redis-server)

> 以下操作需在 Linux 本地启动 redis-server 后用 redis-cli 执行。本书未实跑,仅列观察方法与预期切换点(阈值来自 config.c 默认值,可 `CONFIG GET` 确认)。

```text
# 观察 list 编码从 listpack → quicklist 的切换:
127.0.0.1:6379> CONFIG GET list-max-listpack-size   # 预期 -2(每节点 8 KB)
127.0.0.1:6379> CONFIG GET list-compress-depth       # 预期 0(关压缩)
127.0.0.1:6379> DEL mylist
127.0.0.1:6379> RPUSH mylist a                       # 1 个元素
127.0.0.1:6379> OBJECT ENCODING mylist               # 预期 listpack(单节点装得下)
# 循环 RPUSH 到总字节超过 8 KB(fill=-2 的单节点上限):
127.0.0.1:6379> OBJECT ENCODING mylist               # 预期 quicklist(升级为双层)

# 观察 PLAIN 容器(单个超大元素独占一节点):
127.0.0.1:6379> DEL biglist
127.0.0.1:6379> RPUSH biglist $(head -c 100000 /dev/urandom | base64)  # 100KB 元素
# 此时 quicklistNode 是 container=PLAIN,encoding=RAW(不进 listpack,不压缩)
# OBJECT ENCODING 仍显示 quicklist,但内部节点形态不同

# 观察两端不压中间压(list-compress-depth = 1):
127.0.0.1:6379> CONFIG SET list-compress-depth 1
127.0.0.1:6379> DEL loglist
# 循环 RPUSH 几千条重复日志(如 "user_login user_id=42" 重复千次)
# 预期:head/tail 节点裸(listpack),中间节点被 LZF 压缩
# 间接证据:MEMORY USAGE loglist 显著小于不压缩时(重复数据 LZF 压缩率高)

# 观察降级(shrinking 时阈值减半):
127.0.0.1:6379> DEL shrinklist
# RPUSH 到刚好超过单节点 8 KB 上限 → 升级 quicklist(2 个节点)
# 再 RPOP 到只剩 1 个节点且总字节 < 4 KB(阈值减半)→ 降级回 listpack
127.0.0.1:6379> OBJECT ENCODING shrinklist           # 预期从 quicklist 变回 listpack
```

标注:以上预期基于源码常量与 [config.c](../../redis-8.0.2/src/config.c) 默认阈值推导,本书未在本地实跑;若你的 redis 版本/配置不同,切换点可能偏移,以 `CONFIG GET` 实际值为准。特别地,**`OBJECT ENCODING` 对 list 类型只可能返回 `listpack` 或 `quicklist` 两种**——adlist(老纯链表)在 7.0 已彻底删除,ziplist 在 7.0 后被 listpack 全面替代,这两种编码在 8.0 里都看不到了。
