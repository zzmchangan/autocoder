# 第九章 · rax、编码自适应,与 P2 收口

> 篇:P2 数据结构的执掌(收口章)
> 主轴呼应:这一章是**取向②(内存即数据库)与取向③(编码自适应)的收口**。rax(radix tree,基数树)用前缀压缩让"前缀高度重叠的字符串集合"在内存里近乎免费地装下,直接落地取向②;`object.c` 的编码自适应——"对外一套命令、对内多套编码,按数据形态自动选最优底层"——则正是取向③的命名来源。读完这章,你不仅会看懂 rax,还会看懂整个 P2 为什么是这样一张"按访问模式选结构"的全景图。

---

## 读完本章你会明白

1. **rax 凭什么用一个 `iscompr` 比特,把"压缩节点"和"扇出节点"塞进同一个 `raxNode` 结构**——这不是省一个分支那么简单,而是为了让两种节点共享同一套分配/遍历/分裂代码,把内存紧凑和实现简洁同时做到位。
2. **往一个压缩节点中间插分叉时,那段公共前缀是怎么被砍成三段(共享前缀/分叉点/后缀)的**——源码里有一段长达 50 行的注释,把五种情形画得清清楚楚,这一章会把它的几何意义拆开。
3. **删一个 key 之后,那条"只剩一个孩子的单链"是怎么重新收缩回一个压缩节点的**——`raxRemoveChild` 与 `raxRemove` 的回溯压缩是一对镜像算法。
4. **`raxLowWalk` 这个名字里带 "Low" 的函数,凭什么被 insert/find/remove/iterator 四处共用**——它一次扫描同时吐出停止节点、父链接、压缩节点里的分裂位置,把"一棵树的全部写操作"压到一个共享骨架上。
5. **rax 到底用在哪些地方,哪些地方"听起来像 rax"其实不是**——stream 主索引和消费者组的 PEL 是 rax,client-side caching 的 PrefixTable 是 rax,但 **cluster 的 slot→node 映射不是 rax(是直接数组),SCAN 的底层迭代也不是 rax**。这一章会把这条边界划清,免得你被"Redis 哪都靠 rax"的传言带偏。
6. **为什么用户只敲 `HSET`、`ZADD`,从不需要关心底层是 listpack 还是 hashtable**——因为写入路径上有"TryConversion 预判"和"Convert 兜底"两道闸门,而所有阈值都是 `MODIFIABLE_CONFIG`、可在运行时调。
7. **P2 的六种结构(SDS/dict/listpack/quicklist/skiplist/rax)是怎么被 `redisObject` 这一个 16 字节小盒子粘成"一套接口、多种底层"的**——这是数据结构篇的总收口。

---

> **如果一读觉得太难:先只记住四件事**——
> ① rax = 一棵做了"前缀压缩"的 trie:公共前缀只存一次,且无分叉的字符链被压进一个"压缩节点";分叉处才用一个"扇出节点"把多个孩子并列摆开。同一个 `raxNode` 结构靠一个 `iscompr` 比特区分这两种布局([rax.h:78](../../redis-8.0.2/src/rax.h#L78))。
> ② 插入时如果在压缩节点中间撞上不匹配字符,就按"分裂位置 splitpos"把它砍成"前缀/分叉点/后缀"三段;删除后如果某段退化成"单孩子非 key 链",就反向重新压回一个压缩节点。
> ③ `raxLowWalk`([rax.c:437](../../redis-8.0.2/src/rax.c#L437))是 insert/find/remove/iterate 共用的底层走树函数,一次扫描同时返回停止节点 + 父链接 + 分裂位置——"一次遍历,多处复用"。
> ④ Redis 对外五种类型(string/list/hash/set/zset),对内是"多种编码 × 按数据形态自动切换":hash 默认 512 field/64 字节就升 hashtable,zset 128/64 升 skiplist,set intset 512 升 listpack/hashtable;切换在写入路径自动发生,用户无感。
> 这四件事,就是本章的全部。

---

> **一句话点破:rax 不是一棵"更快"的树,它是一棵"更省"的树——它用前缀压缩换内存,用"iscompr 一个比特区分两种节点布局"换实现紧凑,用 raxLowWalk 一次遍历喂四处调用换代码简单;而编码自适应则把这套"按形态选结构"的哲学从单个数据结构推广到整个 Redis——用户眼里只有五种类型,内核里其实是六种结构在按数据自己说的算,实时切换。**

第七章 SDS、第八章 dict 和 skiplist,我们已经攒了一抽屉的家伙。本章做两件事:一是补上 P2 最后一块拼图——**rax**,讲清它在前缀高度重叠的字符串场景里如何省内存、它的两种节点布局、分裂与合并算法;二是把这堆结构用 `object.c` 的**编码自适应**粘起来,把 P2 收口。这两件事在内核里其实是同一个取向的两面:**数据常驻内存,所以每一份冗余都要省掉**;**形态会变,所以底层要能跟着换**。

## 9.1 这块要解决什么:前缀冗余,与"一套接口多种底层"

先看 rax 要解决的痛点。Stream 的每条消息由一个 ID 标识,ID 形如 `1234567890-0`、`1234567890-1`、`1234567891-0`……注意它们的**公共前缀极长**:同一个毫秒内产生的多条消息,前 13 个字符几乎完全一样。一个活跃的 Stream 可能有几百万条消息,如果用普通哈希表存 key,key 整段重复拷贝——光存"同一个毫秒的前 13 个字符"就要重复几百万份。

> **不这样会怎样(三种坏方案)**:
> 第一,用 dict(哈希表)——key 完整拷贝,前缀冗余爆炸,百万条 Stream 消息光 key 就几百 MB;且哈希表无序,`XRANGE`/`XREAD` 这种范围扫描根本做不了。
> 第二,用朴素 trie(字典树,每字符一节点)——查找是 O(keylen) 优秀,但指针开销淹没字符:存一百万个长 13 字节的 Stream ID,光"每字符一个 next 指针"就 13×8×100 万 ≈ 100 MB,存 1 字节的字符花 8 字节的指针,亏到离谱。
> 第三,用红黑树/跳表按 key 全序排——能做范围扫描,但每条 key 还是要完整存一份,前缀冗余一点没省。
> **单一结构在前缀冗余 + 大规模 + 需要范围扫描这三件事之间必然顾此失彼。**

rax 的思路很直球:**公共前缀只存一次,而且不一个字符一个节点地存——把一段无分叉的字符序列压进一个"压缩节点"里**。这样 `1234567890-` 这段公共前缀在整棵树里只出现一次,后面分叉时才拆开。结果:存一百万条 Stream 消息的索引,前缀那十几个字节只占一份拷贝,而不是一百万份。

再看编码自适应要解决的痛点。Redis 对外暴露的类型只有五种(`string/list/hash/set/zset`),但底层每种都有两三种编码。以 hash 为例:

- 小 hash(`HSET user:1 name alice`)用 **listpack**:一段连续内存,field/value 紧挨着存,省指针、cache 友好;
- 大 hash 用 **hashtable**(dict):O(1) 查找,抗冲突。

用户不该、也不想知道这个切换。所以 Redis 把"什么时候切、怎么切"全塞进写入路径,对上层透明。**一个接口、按数据形态自动选最优底层**——这是取向③的本质,也是本章要讲清的核心。

那么"按 score 有序"这个角色已经有 skiplist 了,为什么还要 rax?因为 **skiplist 解决的是"按值有序",rax 解决的是"按字符串前缀有序且前缀高度重叠"**——这是两类完全不同的访问模式。skiplist 节点存的是完整的 `ele` 字符串(8.7 节那块共享 sds),它不压缩前缀;rax 节点存的是"前缀的一段"。当 key 是 Stream ID、消费者组名、客户端缓存前缀这种"几十万条共享十几字节前缀"的形态,只有 rax 能把内存压下来。

## 9.2 几何直觉:前缀压缩的树

先忘掉代码,画个画面。假设我们要存三个 key:`foo`、`foobar`、`footer`。它们共享前缀 `foo`,然后分叉:`foo|bar`、`foo|ter`、`foo`(单独成 key)。朴素 trie 会画成这样(每字符一节点):

```text
            (f) ""
              \
              (o) "f"
                \
                (o) "fo"
                  \
                [t   b] "foo"        ← t 和 b 是两条边,挂在同一个分叉节点上
                /     \
       "foot" (e)     (a) "foob"
              /         \
     "foote" (r)         (r) "fooba"
              /             \
    "footer" []             [] "foobar"
```

这棵树里,从根 `(f)` 到 `(o)` 到 `(o)` 是一条**无分叉的单链**——三个节点每个只有一个孩子。朴素 trie 给这条单链的每一段都分配一个节点(每个节点 4 字节 header + 1 字节字符 + padding + 1 个 8 字节孩子指针 ≈ 24 字节),三个节点就是 72 字节,而它实际承载的信息只是"foo 这 3 个字符"。

rax 的优化是:**把这条无分叉的单链压进一个"压缩节点"**——`["foo"]` 一个节点里存 3 个字符,只挂一个孩子指针。`footer` 那条分支的 `er`、`foobar` 那条分支的 `ar` 同理。压缩后的树长这样:

```text
              ["foo"] ""                 ← 压缩节点:存 "foo" 三字符,1 个孩子指针
                 |
              [t   b] "foo"              ← 扇出节点:2 条边,2 个孩子指针
              /     \
    "foot" ("er")    ("ar") "foob"       ← 两个压缩节点
              /          \
    "footer" []          [] "foobar"
```

`rax.h` 开头那段大注释([rax.h:16-75](../../redis-8.0.2/src/rax.h#L16))把这张图原原本本画了出来,还顺手演示了"插入 `first` 时要在 `["foo"]` 节点中间分叉,把它砍成 `[f] → [i o] → ...`"的节点分裂场景。这是读懂 rax 全部分裂/合并算法的几何基础。

几何上的精妙在一点:**字符存在"边"上,不存在"节点"里**。`[t b]` 这个扇出节点,字符 `t` 和 `b` 是它**作为父节点时挂在它身上的两条边**的标签——节点本身存的是这些边标签 + 它们各自指向的孩子指针。`["foo"]` 这个压缩节点,字符 `f`、`o`、`o` 是**一串隐式串联的边**的标签,只有最后那个字符 `o` 对应一个真实的孩子指针。这个"字符在边上"的视角,是 raxNode 内存布局的来由。

> **钉死这件事**:rax 的几何本质是"字符在边上、不在节点里",这与朴素 trie"字符在节点里"恰好相反。这条几何约定直接决定了 9.3 节的两种内存布局——扇出节点的字符段就是"挂在我身上的所有边的标签",压缩节点的字符段就是"我这条隐式串联链上所有边的标签"。一旦把"边"而不是"节点"当成第一公民,所有后续设计(为什么压缩节点只有 1 个孩子指针、为什么 splitpos 在压缩节点中间才有意义、为什么 raxLowWalk 走扇出节点是"在孩子字符里找匹配")都顺理成章。理解 rax 的最短路径,就是先理解这条几何约定。

## 9.3 数据结构:一个 `raxNode`,两种布局,一个 iscompr 比特

rax 最精明的地方:**压缩节点和扇出节点用同一个 `raxNode` 结构,只靠一个 `iscompr` 比特区分**。看 `rax.h` 的定义([rax.h:78](../../redis-8.0.2/src/rax.h#L78)):

```c
/* rax.h:78-111 */
typedef struct raxNode {
    uint32_t iskey:1;     /* 本节点是否代表一个完整 key(可挂在任何层,不止叶) */
    uint32_t isnull:1;    /* 关联值是否为 NULL(为 NULL 则不存 value 指针) */
    uint32_t iscompr:1;   /* 1=压缩节点,0=扇出节点 */
    uint32_t size:29;     /* 扇出:孩子个数;压缩:字符串长度 */
    /* Data layout is as follows:
     * [header iscompr=0][abc][a-ptr][b-ptr][c-ptr](value-ptr?)   扇出节点
     * [header iscompr=1][xyz][z-ptr](value-ptr?)                  压缩节点
     */
    unsigned char data[];  /* 柔性数组:字符 + 孩子指针 + (可选)value 指针 */
} raxNode;
```

整个 header 只有 4 字节(三个 1 位标志 + 一个 29 位 size,合计 32 位)。`size` 字段被两种节点**复用**:扇出节点里它是"孩子个数",压缩节点里它是"字符串长度"。`size:29` 上限是 `RAX_NODE_MAX_SIZE = (1<<29)-1`([rax.h:77](../../redis-8.0.2/src/rax.h#L77)),约 5 亿字符——对任何一个压缩节点都绰绰有余。

关键在 `data[]` 这个柔性数组的**两种内存布局**。源码注释([rax.h:83-109](../../redis-8.0.2/src/rax.h#L83))用两行字符画把它画了出来:

```text
扇出节点 (iscompr=0):  [HDR][a b c][a-ptr][b-ptr][c-ptr](value-ptr?)
压缩节点 (iscompr=1):  [HDR][x y z][z-ptr](value-ptr?)
```

画成字节级的 ASCII 布局图(64 位系统,指针 8 字节,padding 让指针落在 8 字节对齐地址):

```text
扇出节点 iscompr=0,size=3,孩子字符 a/b/c,带 value:
┌────────┬───┬───┬───┬───┬───┬───┬───┬──────────┬──────────┬──────────┬──────────┐
│ HDR 4B │ a │ b │ c │ . │ . │ . │ . │  a-ptr   │  b-ptr   │  c-ptr   │ value-ptr│
│ 4 bit  │   │   │   │←──── padding ────→│  8 字节   │  8 字节   │  8 字节   │  8 字节   │
└────────┴───┴───┴───┴───┴───┴───┴───┴──────────┴──────────┴──────────┴──────────┘
          ←─ size 个字符 ─→  ←raxPadding→  ←── size 个孩子指针 ──→  ← 可选 ──→

压缩节点 iscompr=1,size=3,字符串 xyz,带 value:
┌────────┬───┬───┬───┬───┬───┬───┬───┬──────────┬──────────┐
│ HDR 4B │ x │ y │ z │ . │ . │ . │ . │  z-ptr   │ value-ptr│
│        │   │   │   │← padding →│   │  仅 1 个   │  可选     │
└────────┴───┴───┴───┴───┴───┴───┴───┴──────────┴──────────┘
          ← size 个字符 →  ←padding→  ←只有最后字符有孩子→
```

两种布局的差别只有一处:**孩子指针的数量**。扇出节点有 `size` 个孩子指针(每个字符一个);压缩节点**只有 1 个**孩子指针(指向字符串末尾字符之后的那个节点,中间字符隐式串联,不占指针)。字符段后面的 `raxPadding` 是为了让孩子指针落在 8 字节对齐的地址上(避免未对齐访问的开销或崩溃)——它的算法在 [rax.c:130](../../redis-8.0.2/src/rax.c#L130):

```c
/* rax.c:126-130 注释 + 宏 */
/* Return the padding needed in the characters section of a node having size
 * 'nodesize'. ... Note that we add 4 to the node size because the node has
 * a four bytes header. */
#define raxPadding(nodesize) \
    ((sizeof(void*)-(((nodesize)+4) % sizeof(void*))) & (sizeof(void*)-1))
```

这个宏的几何含义:`sizeof(void*)=8`(64 位),`(nodesize+4) % 8` 是"header(4 字节)+ 字符段"总长度末尾离下一个 8 字节边界的距离,`8 - 那个距离` 就是还要补几个字节。最外层 `& (sizeof(void*)-1)` 是为了把"刚好对齐时应该补 0 而不是补 8"这个边界处理对——`(8 - 0) & 7 = 0`。一个宏同时管"对齐量"和"零对齐时归零"两个语义,是位运算的小巧思。

节点总长度的计算同样藏在宏里([rax.c:150](../../redis-8.0.2/src/rax.c#L150)):

```c
/* rax.c:147-155 */
#define raxNodeCurrentLength(n) ( \
    sizeof(raxNode)+(n)->size+ \                              /* header + 字符段 */
    raxPadding((n)->size)+ \                                  /* 对齐 padding */
    ((n)->iscompr ? sizeof(raxNode*) : sizeof(raxNode*)*(n)->size)+ \  /* 孩子指针 */
    (((n)->iskey && !(n)->isnull)*sizeof(void*)) \            /* 可选 value 指针 */
)
```

这一行宏把"两种节点布局 + 可选 value"四种组合的长度都算对了。注意第四行:只有 `iskey && !isnull`(本节点是 key 且 value 不是 NULL)时才多 8 字节存 value 指针——`isnull=1` 时即使 iskey=1 也不占 value 空间,这是 rax 为"只需要 key 不需要 value"的场景(比如纯集合)留的省内存口子。

> **钉死这件事**:`raxNode` 用一个 `iscompr` 比特区分两种内存布局,不是省一个 union 那么简单——它让"压缩节点"和"扇出节点"共享同一套分配、长度计算、遍历代码。`raxNodeCurrentLength` 一个宏管四种组合(压缩/扇出 × 有无 value),`raxPadding` 一个宏同时算对齐量并处理零对齐边界。这套"按位运算把变长布局压进一个 struct"的写法,是 rax 节点内存紧凑到极致的物理基础。同一个 struct 复用两种语义,代码量减半,bug 面也减半。

那么 key 完整存在哪?**沿着根走到某个节点的路径上,所有边的字符拼起来,就是这个节点代表的 key**。注意源码注释([rax.h:101-103](../../redis-8.0.2/src/rax.h#L101))的一句话:"Both compressed and not compressed nodes can represent a key with associated data in the radix tree at any level (not just terminal nodes)."——**任何层的节点都可以是 key**,不止叶子。这让 rax 能优雅地表达"foo"和"foobar"同时是 key 的语义:`["foo"]` 这个压缩节点的 `iskey=1`(代表 key `foo`),它的孩子 `[b]` 又能继续走到代表 `foobar` 的节点。普通 trie 要表达"一个 key 是另一个 key 的前缀"得绕弯,rax 用一个 iskey 比特就解决了。

最后看 rax 整体的句柄([rax.h:113](../../redis-8.0.2/src/rax.h#L113)):

```c
/* rax.h:113-118 */
typedef struct rax {
    raxNode *head;       /* 根节点 */
    uint64_t numele;     /* key 个数 */
    uint64_t numnodes;   /* 节点个数(含内部节点,用于碎片分析) */
    void *metadata[];    /* 可选元数据(扩展用) */
} rax;
```

注意 `numele` 和 `numnodes` 是分开记的。`numele` 是逻辑 key 数,`numnodes` 是物理节点数——`numnodes / numele` 这个比值反映了树的"压缩效率":比值越接近 1 说明每个 key 几乎独占一个节点(前缀重叠少),比值越大说明压缩越狠(很多 key 共享节点)。`raxNew`([rax.c:176](../../redis-8.0.2/src/rax.c#L176))建一棵空树时给 `numnodes=1`(预分配一个空的根节点):

```c
/* rax.c:181-193 */
rax *raxNewWithMetadata(int metaSize) {
    rax *rax = rax_malloc(sizeof(*rax) + metaSize);
    if (rax == NULL) return NULL;
    rax->numele = 0;
    rax->numnodes = 1;
    rax->head = raxNewNode(0, 0);   /* 空根节点:0 孩子,无 value */
    ...
}
```

## 9.4 技巧精解①:raxLowWalk——一次遍历,喂四处调用

这是 rax 全书最值得讲透的函数。`raxLowWalk`([rax.c:437](../../redis-8.0.2/src/rax.c#L437))是 insert、find、remove、iterator 四个上层操作**共用**的底层走树函数。它的签名本身就透露了"一次遍历多处复用"的设计意图:

```c
/* rax.c:408-437 注释节选 + 函数签名 */
/* Low level function that walks the tree looking for the string 's' of 'len'
 * bytes. The function returns the number of characters of the key that was
 * possible to process: ... The node where the search ended ... is returned
 * by reference as '*stopnode' ... This node link in the parent's node is
 * returned as '*plink' ... Finally, if the search stopped in a compressed
 * node, '*splitpos' returns the index inside the compressed node where the
 * search ended. This is useful to know where to split the node for insertion. */
static inline size_t raxLowWalk(rax *rax, unsigned char *s, size_t len,
        raxNode **stopnode, raxNode ***plink, int *splitpos, raxStack *ts) {
    raxNode *h = rax->head;
    raxNode **parentlink = &rax->head;
    size_t i = 0;  /* 在输入字符串里的位置 */
    size_t j = 0;  /* 在当前节点字符段里的位置 */
    while(h->size && i < len) {
        unsigned char *v = h->data;
        if (h->iscompr) {
            for (j = 0; j < h->size && i < len; j++, i++) {
                if (v[j] != s[i]) break;   /* 压缩节点:逐字符比对,一不匹配就停 */
            }
            if (j != h->size) break;       /* 没走完整段,说明中途不匹配 */
        } else {
            for (j = 0; j < h->size; j++) {
                if (v[j] == s[i]) break;   /* 扇出节点:在孩子字符里线性扫找匹配 */
            }
            if (j == h->size) break;       /* 没找到匹配字符 */
            i++;
        }
        if (ts) raxStackPush(ts,h);        /* 可选:把父节点压栈,供回溯用 */
        raxNode **children = raxNodeFirstChildPtr(h);
        if (h->iscompr) j = 0;             /* 压缩节点只有 1 个孩子,在 index 0 */
        memcpy(&h,children+j,sizeof(h));   /* 走到孩子 */
        parentlink = children+j;
        j = 0;
    }
    if (stopnode) *stopnode = h;
    if (plink) *plink = parentlink;
    if (splitpos && h->iscompr) *splitpos = j;   /* 仅压缩节点才有 splitpos */
    return i;
}
```

看它**一次扫描同时吐出的四样东西**:

1. **返回值 `i`**:输入字符串里被成功匹配的字符数。`i == len` 表示整串都走完了(可能命中 key,也可能停在压缩节点中间);`i != len` 表示中途就不匹配了。
2. **`*stopnode`**:搜索停下来的那个节点。无论是"走完整串"还是"中途不匹配",都会停在一个节点上。
3. **`*plink`**:`stopnode` 在它父节点里的"指向我的那个指针"的地址(`raxNode **`)。这是给上层"原地替换/重新挂接孩子"用的——比如插入时如果 realloc 改了节点地址,要把 `*plink` 改成新地址。
4. **`*splitpos`**:如果停在压缩节点中间,这是"在该节点的字符段里停在第几个字符"。这是给"节点分裂"用的——上层插入逻辑要靠它知道从哪把压缩节点砍开。

外加一个可选参数 `raxStack *ts`:如果传了非 NULL,走树过程中会把沿途的每个父节点压栈。这是给"删除后回溯压缩"用的——raxNode 结构里**故意不存 parent 指针**(省 8 字节/节点,对百万级树是 GB 级节省),需要回溯时靠这个外部栈临时记路径。栈还有个小优化(`rax.h:123-131`):前 32 个父节点用栈上的静态数组 `static_items`,超过才 `rax_malloc`——绝大多数树的深度 < 32(因为路径被压缩节点大大缩短),这一句"32 之内零分配"是热路径优化。

为什么这套设计精妙?**因为 insert、find、remove、iterate 四件事的"前半段"全是同一件事——按字符串走树,停在某个节点**。差别只在"停下来之后干什么":

- `find`:看 `stopnode->iskey` 和 `i==len`,决定 key 在不在。
- `insert`:如果 `i==len` 且没停在压缩节点中间,直接给 stopnode 加 value 指针;如果停在压缩节点中间(撞上不匹配),靠 `splitpos` 砍开它再插。
- `remove`:同样走树,找到 stopnode 后清掉 `iskey`,然后靠 ts 栈回溯压缩。
- `iterator`(`raxSeek`/`raxNext`):走树定位起点,后续遍历靠"走到最左/最右 key"或"中序遍历"。

**一套走树骨架,喂四处调用**——这是把"树的查找逻辑"和"找到之后的动作"解耦的经典做法,和 Tokio 那种"事件循环骨架 + 回调"是同一种设计哲学。

> **钉死这件事**:`raxLowWalk` 一次扫描返回四样东西(停止节点 / 父链接 / 分裂位置 / 沿途父节点栈),让 insert/find/remove/iterator 四个上层操作共享同一套走树代码。注意 `splitpos` **只在压缩节点有意义**——扇出节点没有"中间位置"这个概念,因为它的字符就是孩子边,不匹配直接停在孩子字符 `j` 上(等价于"第 j 条边不匹配",而不是"压缩节点中间某字符不匹配")。这一处条件分支(`if (splitpos && h->iscompr)`)把"压缩节点要分裂、扇出节点不分裂"这两类语义干净地分流了。

源码注释([rax.c:453-455](../../redis-8.0.2/src/rax.c#L453))还顺手提了一个反直觉的细节:扇出节点找孩子用的是**线性扫描**而不是二分查找——`Even when h->size is large, linear scan provides good performances compared to other approaches that are in theory more sounding, like performing a binary search.`。理由和 listpack 一样:孩子数通常很小(一个扇出节点平均 2-4 个孩子),线性扫描的常数(一次 `v[j]==s[i]` 比较)远小于二分的"算 mid + 多次比较 + 分支预测失败"开销。这是又一次"在 N 很小时,O(N) 击败 O(log N)"。

## 9.5 技巧精解②:节点分裂——压缩节点的三段切割

这一节是整章最硬的部分。插入一个新 key 时,如果走树撞上一个压缩节点,且在它中间某个字符不匹配,就要把这个压缩节点**砍成三段**。源码 `raxGenericInsert`([rax.c:487](../../redis-8.0.2/src/rax.c#L487))里有一段长达 50 行的注释([rax.c:530-652](../../redis-8.0.2/src/rax.c#L530)),把所有可能的分裂情形画得清清楚楚。这段注释是 rax 全书最值得读的段落之一——它不像普通注释那样只说"做什么",而是把每种情形的几何图画了出来。

先看注释里那个标准例子。假设当前压缩节点是 `"ANNIBALE"`,它表示一条隐式链 `A→N→N→I→B→A→L→E`,末尾孩子指针指向 `E` 之后的节点。整个子树长这样:

```text
    "ANNIBALE" ──→ "SCO" ──→ []
```

现在要插入 `"ANNIENTARE"`(和 `"ANNIBALE"` 共享前缀 `ANNI`,在第 5 个字符 `B/E` 处分叉)。注释把它叫"case 1"。分裂后的几何图([rax.c:554-557](../../redis-8.0.2/src/rax.c#L554)):

```text
               |B| ──→ "ALE" ──→ "SCO" ──→ []
    "ANNI" ──→ |-|
               |E| ──→ (... continue algo ...) "NTARE" ──→ []
```

把这条隐式链还原成显式的 raxNode 布局,分裂算法把原始的压缩节点 `"ANNIBALE"`(1 个节点)替换成**三个新节点**:

```text
分裂前(1 个压缩节点):
┌─────────────────────────────────────┐
│ HDR iscompr=1 size=8 │ A N N I B A L E │ next-ptr │
└─────────────────────────────────────┘
                  ↓ next 指向
              "SCO" → []

分裂后(3 个节点):
(1) 前缀节点 "ANNI"(压缩,iscompr=1,size=4):
    ┌──────────────────┐
    │ HDR iscompr=1 size=4 │ A N N I │ split-ptr │
    └──────────────────┘
                            ↓ 指向分叉节点
(2) 分叉节点 [B E](扇出,iscompr=0,size=2):
    ┌────────────────────────────────┐
    │ HDR iscompr=0 size=2 │ B E │ B-ptr E-ptr │
    └────────────────────────────────┘
                  ↓ B-ptr                ↓ E-ptr
(3a) 后缀节点 "ALE"(压缩,iscompr=1,size=3):  (3b) 新插入路径 "NTARE"(继续插入算法):
    ┌──────────────────┐
    │ HDR iscompr=1 size=3 │ A L E │ next-ptr │   ← next 接原 "ANNIBALE" 的 next,"SCO"→[]
    └──────────────────┘
```

这套"砍成三段"的几何含义:**共享前缀**(`ANNI`,长度 = splitpos)、**分叉点**(`B` 和 `E` 两条边挂在一个扇出节点上)、**原节点的后缀**(`ALE`,长度 = 原长度 - splitpos - 1,减 1 是因为分叉字符 `B` 已经被吸进分叉节点)。新插入的字符串的后缀(`NTARE`)则在分叉节点的另一条边上继续往下走,走的是普通插入算法。

注释还列了另外四种情形([rax.c:558-578](../../redis-8.0.2/src/rax.c#L558)),几何上都是这套三段切割的变体:

- **case 2(`ANNIBALI`,后缀只剩 1 字符)**:后缀节点 `"I"` 长度 = 1,按规则 `set iscompr to 0`——一个字符的"压缩节点"等价于一个 1-孩子的扇出节点,但布局上把 iscompr 设 0 让代码统一(因为单字符压缩的 next 指针和孩子指针在内存里位置一样)。
- **case 3(`AGO`,前缀为空 splitpos=0)**:前缀节点不存在,直接用分叉节点替换原节点。分叉字符是原节点的首字符 `N` 和新字符串的 `G`。
- **case 4(`CIAO`,首字符就不匹配 splitpos=0)**:分叉节点直接是 `[A C]`,原节点变成 `"NNIBALE"` 压缩节点挂在 A 这条边上。
- **case 5(`ANNI`,新字符串是原压缩节点的前缀,无不匹配)**:走的是另一套算法 ALGO 2——不创建分叉节点,只把原压缩节点切成 `"ANNI"`(变 key)+ `"BALE"`(后缀)两段。

源码把这套算法分两段实现。ALGO 1 的入口([rax.c:655](../../redis-8.0.2/src/rax.c#L655)):

```c
/* rax.c:655-680 节选 */
if (h->iscompr && i != len) {        /* 停在压缩节点且字符串没走完 = 中途不匹配 */
    /* 1: Save next pointer. 保存原压缩节点末尾的孩子指针 */
    raxNode **childfield = raxNodeLastChildPtr(h);
    raxNode *next;
    memcpy(&next,childfield,sizeof(next));

    /* 关键长度计算 */
    size_t trimmedlen = j;           /* 前缀长度 = splitpos */
    size_t postfixlen = h->size - j - 1;  /* 后缀长度 = 原长 - splitpos - 1 */

    /* 2: 创建分叉节点(2 个孩子:原节点的分叉字符 + 新字符串的分叉字符) */
    raxNode *splitnode = raxNewNode(1, split_node_is_key);
    raxNode *trimmed = NULL;
    raxNode *postfix = NULL;
    if (trimmedlen) { ... trimmed = rax_malloc(...); }   /* 前缀节点(若 splitpos>0) */
    if (postfixlen) { ... postfix = rax_malloc(...); }   /* 后缀节点(若还有字符) */
    ...
}
```

三个关键变量 `trimmedlen`/`postfixlen`/`splitpos` 一字排开,几何含义清清楚楚。注意 `postfixlen = h->size - j - 1` 里那个 `-1`:分叉字符本身(原节点的 `v[j]`)被吸进了分叉节点,不归前缀也不归后缀。

ALGO 2(对应 case 5,新字符串是压缩节点的前缀)的入口在 [rax.c:813](../../redis-8.0.2/src/rax.c#L813) 附近,逻辑更简单:不建分叉节点,只把原节点砍成"前缀(变 key)+ 后缀"两段。

> **钉死这件事**:压缩节点分裂的本质是"按 splitpos 把一段隐式字符链切成三段"。`trimmedlen = j`(splitpos)= 共享前缀长度;`postfixlen = size - j - 1` = 原节点后缀长度(那个 -1 是因为分叉字符被吸进新建的扇出节点)。三种长度算清了,三段节点(前缀压缩节点 / 分叉扇出节点 / 后缀压缩节点)的分配和挂接就顺理成章。注释里那句 `If new compressed node len is just 1, set iscompr to 0` 不是优化,是正确性——单字符压缩节点和孩子指针的相对位置,和单孩子扇出节点是字节级一致的,设 iscompr=0 让后续代码不需要为"1 字符压缩"特判。

## 9.6 技巧精解③:节点合并——删除后的单链收缩

有分裂就有合并。删一个 key 之后,如果某段路径退化成"一条无分叉、沿途节点都不是 key"的单链,rax 要把它重新压回一个压缩节点——否则树会随着增删越来越稀疏,失去前缀压缩的意义。这套合并算法分两层:**raxRemoveChild**(摘孩子,单个节点层面)+ **raxRemove 主流程里的回溯压缩**(链路层面)。

先看 `raxRemoveChild`([rax.c:928](../../redis-8.0.2/src/rax.c#L928))。它的注释段([rax.c:920-927](../../redis-8.0.2/src/rax.c#L920))说明:这个函数把 parent 里的某个 child 摘掉,**返回可能 realloc 后的新 parent 指针**(因为摘孩子后节点变小,可能 realloc 回收空间,地址会变)。函数对压缩 parent 和扇出 parent 分两种处理:

```c
/* rax.c:928-997 节选 */
raxNode *raxRemoveChild(raxNode *parent, raxNode *child) {
    /* 如果 parent 是压缩节点(只有 1 个孩子),摘掉孩子 = 把它变成 0 孩子的空节点 */
    if (parent->iscompr) {
        void *data = NULL;
        if (parent->iskey) data = raxGetData(parent);
        parent->isnull = 0;
        parent->iscompr = 0;        /* 降级成扇出节点(size=0) */
        parent->size = 0;
        if (parent->iskey) raxSetData(parent,data);
        return parent;
    }
    /* 否则 parent 是扇出节点:线性扫找 child 在哪,memmove 把它前后的字符和指针挤回去 */
    raxNode **cp = raxNodeFirstChildPtr(parent);
    raxNode **c = cp;
    unsigned char *e = parent->data;
    while(1) { ... 找到 child ... }
    int taillen = parent->size - (e - parent->data) - 1;
    memmove(e,e+1,taillen);         /* 字符段:把孩子字符挤掉 */
    /* 指针段:同样 memmove 挤掉孩子指针,还要处理 padding 变化导致的 shift */
    size_t shift = ((parent->size+4) % sizeof(void*)) == 1 ? sizeof(void*) : 0;
    ...
    parent->size--;
    raxNode *newnode = rax_realloc(parent,raxNodeCurrentLength(parent));  /* 缩容 */
    return newnode ? newnode : parent;   /* realloc 失败也不影响正确性,只是暂时多占点 */
}
```

压缩 parent 的处理特别优雅:**直接把它降级成 size=0 的扇出节点**(iscompr=0)。因为压缩节点只有 1 个孩子,摘掉这个孩子后它就是个"光杆节点"——而光杆节点就是 size=0 的扇出节点,两种布局在 size=0 时字节级一致。一个赋值 `parent->iscompr = 0` 完成所有工作,不用 realloc。

再看 `raxRemove`([rax.c:1001](../../redis-8.0.2/src/rax.c#L1001))主流程里的回溯压缩。删掉一个 key 后(`h->iskey = 0`),如果 `h->size == 0`(叶子节点),要一路向上 free,直到遇到"有多个孩子"或"自己也是 key"的祖先——那条向上路径上,所有"单孩子非 key"的节点都是死节点,该 free:

```c
/* rax.c:1026-1066 节选 */
if (h->size == 0) {
    raxNode *child = NULL;
    while(h != rax->head) {
        child = h;
        rax_free(child);
        rax->numnodes--;
        h = raxStackPop(&ts);          /* 靠 raxLowWalk 留下的栈回溯 */
        if (h->iskey || (!h->iscompr && h->size != 1)) break;  /* 多孩子或 key,停 */
    }
    if (child) {
        raxNode *new = raxRemoveChild(h,child);   /* 把 child 从父里摘掉 */
        ...
        /* 摘完后如果父变成单孩子非 key,标记 trycompress,待会尝试重新压缩 */
        if (new->size == 1 && new->iskey == 0) {
            trycompress = 1;
            h = new;
        }
    }
} else if (h->size == 1) {
    /* 被删节点本来就只有 1 个孩子(只是把 iskey 清了),也可能触发压缩 */
    trycompress = 1;
}
```

`trycompress=1` 之后的实际压缩逻辑([rax.c:1068-1180](../../redis-8.0.2/src/rax.c#L1068))是整段合并算法的核心:从 `h` 开始往下收集所有"单孩子非 key"的节点,把它们对应的字符攒成一段连续字符串,然后用 `raxCompressNode`([rax.c:375](../../redis-8.0.2/src/rax.c#L375))重新压成一个压缩节点。收集过程中如果遇到 iskey 节点(意味着这条链不能无损压缩,因为 key 信息必须保留在节点边界)就停下来,把链在此截断。

`raxCompressNode`([rax.c:375](../../redis-8.0.2/src/rax.c#L375))的注释([rax.c:367-374](../../redis-8.0.2/src/rax.c#L367))点出了一个微妙约束:它只能压缩"每个都恰好 1 个孩子"的链,**链尾的 0-孩子节点不能被压进去**——因为压缩节点必须有一个"末尾孩子指针"指向后续节点,而 0-孩子节点没有后续。所以压缩算法总是返回一个新创建的孩子节点(`*child = raxNewNode(0,0)`)承接链尾。

> **钉死这件事**:rax 的删除路径用"先 free 死节点 + 标记 trycompress + 重新收集压缩"三步完成合并。其中 `raxRemoveChild` 对"压缩 parent 摘孩子"的处理是**一个赋值 `iscompr=0` 把它降级成 size=0 的扇出节点**——因为 size=0 时两种布局字节级一致,根本不用 realloc。这是"iscompr 一个比特区分两种布局"这套设计的另一面红利:不只省了 union,还让"节点形态转换"在某些情形下退化成一次位翻转。压缩 parent 摘孩子如此,9.5 节的"1 字符压缩节点 iscompr 设 0"也如此——同一种 trick 的不同应用。

## 9.7 几个散点:迭代器、key 在中间节点、stream 里的真实用处

**raxIterator 与 `raxSeek("^")` / `raxNext` 顺序遍历。** rax 提供完整的迭代器接口([rax.h:156-167](../../redis-8.0.2/src/rax.h#L156))。`raxStart` 初始化、`raxSeek` 定位、`raxNext`/`raxPrev` 前后走、`raxEOF` 判结尾、`raxStop` 释放。最常见的范围扫描模式是 `raxSeek("^")` 走到第一个 key([rax.c:1536](../../redis-8.0.2/src/rax.c#L1536) 的 `^` 操作符),然后循环 `raxNext` 顺序遍历([rax.c:1682](../../redis-8.0.2/src/rax.c#L1682))。`raxSeek` 支持 `=`/`>=`/`>`/`<=`/`<`/`^`(头)/`$`(尾)七种操作符([rax.c:1527-1543](../../redis-8.0.2/src/rax.c#L1527)),全部基于 raxLowWalk 或"走到最左/最右 key"实现。`raxNext` 内部走 `raxIteratorNextStep`([rax.c:1683](../../redis-8.0.2/src/rax.c#L1683)),沿着树深度优先走,每次停在 `iskey=1` 的节点吐出一个 key——这就是 stream `XRANGE` 范围扫描的底层。`raxPrev` 走 `raxIteratorPrevStep`([rax.c:1698](../../redis-8.0.2/src/rax.c#L1698))对称的反向遍历。

**key 可以挂在任何中间节点。** 9.3 节提过:任何层的节点 `iskey=1` 都代表一个 key,不止叶子。这让 rax 能自然表达"foo"和"foobar"同时是 key。stream 利用了这一点:消息 ID `1234567890-0` 和 `1234567890-1` 共享前缀 `1234567890-`,在 rax 里 `["1234567890-"]` 这段被压成一个压缩节点(或一段压缩链),它的某个中间节点 iskey=1 代表一个或多个消息。每个 rax 节点的 value 指针指向一个 listpack,里面装一批消息——这正是 `OBJ_ENCODING_STREAM 10 /* Encoded as a radix tree of listpacks */`([server.h:990](../../redis-8.0.2/src/server.h#L990))这条枚举注释的字面含义:**radix tree of listpacks**,radix 树的每个 key 节点挂一个 listpack。这是 Redis 8.0 stream 的核心存储模型。

**rax 在 Redis 里的真实用处(核实过的)。** rax 不是 Redis 哪都用的万金油,它只出现在"前缀高度重叠 + 字符串 key + 需要范围或前缀查找"的几处:

- **Stream 主索引**:`streamNew` 里 `s->rax = raxNew()`([t_stream.c:48](../../redis-8.0.2/src/t_stream.c#L48)),key 是消息 ID,`stream.h:17` 的注释 `rax *rax; /* The radix tree holding the stream. */` 一语道破。每条消息由 ID 索引,value 是一个 listpack 装一批消息(降低节点数)。
- **Stream 消费者组的 PEL(Pending Entries List)**:`stream.h:64` 的注释 `rax *pel; /* Pending entries list. This is a radix tree that ... */`——待确认消息的 ID 同样前缀高度重叠。
- **Stream 消费者组名表**:`stream.h:23` 的 `rax *cgroups; /* Consumer groups dictionary: name -> streamCG */`——消费者组名按前缀压缩。
- **Stream 单个消费者的 PEL**:`stream.h:82` 同样是 rax。
- **client-side caching 的 PrefixTable**:[tracking.c:25](../../redis-8.0.2/src/tracking.c#L25) 的 `rax *PrefixTable = NULL;`——按"客户端订阅的 key 前缀"建索引,key 是前缀字符串(高度重叠),value 是订阅了这个前缀的客户端集合。
- **ACL 命令表**:`acl.c` 用 rax 存命令名(命令名前缀重叠,如 `get`/`getset`/`getdel`)。
- **ebuckets**(过期桶)、**defrag**(碎片整理时的节点遍历)也用到 rax。

**两条必须澄清的"听起来像 rax 其实不是"。** 这是本章要诚实标注的边界,免得读者被"Redis 哪都靠 rax"的传言带偏:

> **钉死这件事(修正印象)**:第一,**cluster 的 slot→node 映射不是 rax,是直接数组**。看 `cluster_legacy.h:343` 的 `clusterNode *slots[CLUSTER_SLOTS];`——16384 个 slot,每个 slot 一个指针,直接以 slot 号为下标,O(1) 索引,无哈希无树。理由很直白:slot 号是 0-16383 的小整数,连续紧凑,数组是最优解;用 rax 反而是杀鸡用牛刀。第二,**SCAN 命令的底层迭代不是 rax,是 dict 的扫描**。`db.c` 里 SCAN 走的是 dict 的二进制位反向扫描(`dictScan`),目的是"在 rehash 过程中稳定地遍历所有 bucket",和 rax 无关。rax 在 Redis 里只用于上面列出的"前缀重叠 + 范围扫描"场景,不要把它当成 Redis 的通用索引。这两处是写作时核实源码后**主动修正的印象**——很多二手资料会把 cluster slot 和 SCAN 也算到 rax 头上,源码不撒谎。

## 9.8 编码自适应:谁触发,阈值从哪来

现在看编码自适应怎么落地。这一节是 P2 收口的核心——它把前面所有章节讲的"小用紧凑编码、大用专业编码"这套哲学,收口到 `object.c` 的写入路径。核心是两类函数:**TryConversion**(写之前预判,批量)和 **Convert**(真正搬数据,兜底)。

以 hash 为例。`hashTypeTryConversion`([t_hash.c:605](../../redis-8.0.2/src/t_hash.c#L605))在写命令(如 `HMSET` 一次塞多对)进入实现前被调用,扫描待写入的 field/value,只要有一个超阈值就转:

```c
/* t_hash.c:605 节选 */
void hashTypeTryConversion(redisDb *db, robj *o, robj **argv, int start, int end) {
    if (o->encoding != OBJ_ENCODING_LISTPACK && o->encoding != OBJ_ENCODING_LISTPACK_EX)
        return;                       /* 已经是 hashtable,无需转 */
    size_t new_fields = (end - start + 1) / 2;
    if (new_fields > server.hash_max_listpack_entries) {   /* 元素个数超阈值 */
        hashTypeConvert(o, OBJ_ENCODING_HT, &db->hexpires);
        dictExpand(o->ptr, new_fields);
        return;
    }
    for (i = start; i <= end; i++) {
        size_t len = sdslen(argv[i]->ptr);
        if (len > server.hash_max_listpack_value) {        /* 单元素大小超阈值 */
            hashTypeConvert(o, OBJ_ENCODING_HT, &db->hexpires);
            return;
        }
        ...
    }
    if (!lpSafeToAdd(hashTypeListpackGetLp(o), sum))       /* listpack 总字节超上限 */
        hashTypeConvert(o, OBJ_ENCODING_HT, &db->hexpires);
}
```

这道闸门检查**三件事**:元素个数超阈值、单元素大小超阈值、listpack 总字节超安全上限。任何一条触发,就调 `hashTypeConvert`([t_hash.c:1687](../../redis-8.0.2/src/t_hash.c#L1687))把数据从 listpack 搬到 hashtable。

阈值从哪来?`server.hash_max_listpack_entries` 和 `server.hash_max_listpack_value` 是 `redisServer` 的字段,默认值在 `config.c` 写死([config.c:3238](../../redis-8.0.2/src/config.c#L3238)):

```c
/* config.c:3238-3246 节选 */
createSizeTConfig("hash-max-listpack-entries", "hash-max-ziplist-entries",
    MODIFIABLE_CONFIG, 0, LONG_MAX, server.hash_max_listpack_entries, 512, ...);
createSizeTConfig("hash-max-listpack-value", "hash-max-ziplist-value",
    MODIFIABLE_CONFIG, 0, LONG_MAX, server.hash_max_listpack_value, 64, ...);
createSizeTConfig("zset-max-listpack-entries", "zset-max-ziplist-entries",
    MODIFIABLE_CONFIG, 0, LONG_MAX, server.zset_max_listpack_entries, 128, ...);
createSizeTConfig("set-max-intset-entries", NULL,
    MODIFIABLE_CONFIG, 0, LONG_MAX, server.set_max_intset_entries, 512, ...);
```

即:**hash 默认 512 个 field、或单个 field/value 超过 64 字节,就转 hashtable**。所有阈值都标了 `MODIFIABLE_CONFIG`——可在运行时 `CONFIG SET` 修改,无需重启。

第二条触发路径在每次单条写入里。`hashTypeSet`([t_hash.c:895](../../redis-8.0.2/src/t_hash.c#L895))是所有 hash 写入的公共落点,它在写入后当场检查:

```c
/* t_hash.c:895 节选 */
int hashTypeSet(redisDb *db, robj *o, sds field, sds value, int flags) {
    /* HINCRBY* 走这条路(绕过了 TryConversion),所以这里要补检单元素大小 */
    if (o->encoding == OBJ_ENCODING_LISTPACK || o->encoding == OBJ_ENCODING_LISTPACK_EX) {
        if (sdslen(field) > server.hash_max_listpack_value ||
            sdslen(value) > server.hash_max_listpack_value)
            hashTypeConvert(o, OBJ_ENCODING_HT, &db->hexpires);
    }
    ... /* 写入 listpack 或 hashtable */
    if (hashTypeLength(o, 0) > server.hash_max_listpack_entries)   /* 个数再次校验 */
        hashTypeConvert(o, OBJ_ENCODING_HT, &db->hexpires);
}
```

源码注释那句"HINCRBY* case since in other commands this is handled early"很关键——它揭示了触发点的分工:`HMSET` 这类批量命令靠 `TryConversion` 提前预判(避免一边写一边转,转换成本分摊到一次),`HINCRBY` 这种无法预判长度的数值命令靠 `hashTypeSet` 内部兜底。**两道闸门互补,任何写入路径都漏不掉**。

> **钉死这件事**:编码转换有两道闸门——**TryConversion 在命令入口预判**(适合批量命令,一次转换全部),**Convert 在写入函数兜底**(适合单条命令和无法预判长度的 HINCRBY)。两道闸门覆盖了所有写入路径,没有漏网之鱼。这种"预判 + 兜底"的双层防御,是 Redis 编码自适应可靠运作的工程保障——任何一条单独都不够,TryConversion 漏掉 HINCRBY 这种长值命令,Convert 兜底漏掉批量命令的效率(会一边写一边转)。两条联手,既正确又高效。

这套模式不是 hash 独有。各类型的转换函数和阈值汇总:

| 类型 | 紧凑编码 | 大数据编码 | 转换函数 | 个数阈值 | 单元素阈值 |
|---|---|---|---|---|---|
| hash | listpack / listpack_ex | hashtable(dict) | `hashTypeConvert` @t_hash.c:1687 | 512 | 64 字节 |
| zset | listpack | skiplist + dict | `zsetConvert` @t_zset.c:1250 | 128 | 64 字节 |
| set | intset / listpack | hashtable | `setTypeConvert` @t_set.c:479 | intset 512 / listpack 128 | 64 字节 |
| list | listpack | quicklist | `listTypeTryConvert` | 按 list-max-listpack-size=-2(字节) | — |

阈值默认值全部来自 config.c:

- `hash-max-listpack-entries=512`、`hash-max-listpack-value=64`([config.c:3238](../../redis-8.0.2/src/config.c#L3238)、[config.c:3244](../../redis-8.0.2/src/config.c#L3244))
- `zset-max-listpack-entries=128`、`zset-max-listpack-value=64`([config.c:3242](../../redis-8.0.2/src/config.c#L3242)、[config.c:3246](../../redis-8.0.2/src/config.c#L3246))
- `set-max-intset-entries=512`、`set-max-listpack-entries=128`、`set-max-listpack-value=64`([config.c:3239-3241](../../redis-8.0.2/src/config.c#L3239))
- `list-max-listpack-size=-2`(每个 listpack 节点不超过 8KB)、`list-compress-depth=0`([config.c:3173](../../redis-8.0.2/src/config.c#L3173)、[config.c:3195](../../redis-8.0.2/src/config.c#L3195))

set 是最复杂的,有三段编码:`intset`(全是整数且 ≤512 个)→ `listpack`(开始有字符串,但 ≤128 个)→ `hashtable`(>128 个)。`setTypeConvert`([t_set.c:479](../../redis-8.0.2/src/t_set.c#L479))和 `setTypeConvertAndExpand`([t_set.c:487](../../redis-8.0.2/src/t_set.c#L487))负责这三段切换。

真正搬数据时还有两个细节值得点出。第一,**预扩容**:`zsetConvertAndExpand`([t_zset.c:1255](../../redis-8.0.2/src/t_zset.c#L1255))在转 skiplist 前先 `dictExpand(zs->dict, cap)` 预扩容,避免转换过程中频繁 rehash——"一次性铺好"的思路在所有 Convert 函数里都看得到。第二,**迟滞防抖动**:`listTypeTryConvertQuicklist` 在 quicklist↔listpack 之间设了"缩到阈值一半才回转"的迟滞(`shrinking` 分支),防止数据在阈值附近抖动时频繁双向转换。**转换是重操作,要么一次做对,要么不做**——这是编码自适应背后的运行时哲学。

## 9.9 radix vs trie vs hash:三者放一起比,与自适应阈值的取舍

**为什么 stream 主索引是 rax,不是 trie、不是 hash?** 三者对比能讲清取舍:

| 维度 | rax(radix) | 朴素 trie | hash 表(dict) |
|------|----------|-----------|----------------|
| 查找复杂度 | O(keylen) | O(keylen) | **O(1)** |
| 范围扫描 | **中序遍历即字典序,O(范围大小)** | 中序遍历,但节点稀疏常数大 | **不支持**(无序) |
| 前缀重叠时的内存 | **公共前缀压成一段,极省** | 每字符一节点,指针淹没字符 | key 完整拷贝,前缀冗余爆炸 |
| 增删代价 | O(keylen)+ 可能分裂/合并 | O(keylen) | O(1) 平均 |
| 适用场景 | 前缀重叠 + 范围扫描 + 大规模 | 教学,前缀差异大 | 单点查找,无范围需求 |

stream 的核心操作 `XRANGE`/`XREAD` 是范围扫描(按 ID 区间取消息),hash 表直接出局;朴素 trie 在百万级 Stream 消息下指针开销不可承受;rax 兼顾:压缩前缀省内存(指针数从 13 个降到 1 个量级)、保持 key 有序(中序遍历即字典序)、支持范围迭代(`raxSeek("^")` 到头、`raxNext` 顺序走,见 [t_stream.c](../../redis-8.0.2/src/t_stream.c) 的 stream 迭代实现)。**这是"有序 + 前缀重叠 + 大规模"三角下的唯一解**。

**自适应阈值的取舍**同样值得品味。为什么 hash 是 512、zset 是 128?这不是拍脑袋。listpack 的查找是**线性扫描** O(n),hashtable 是 O(1);线性扫描在 n 小时因 cache 局部性反而更快(一次内存访问 vs 多次指针跳转 + 哈希计算)。阈值就是这两条性能曲线的**经验交点**:hash 字段访问多偏随机、512 内 listpack 仍快;zset 因要同时维护有序(扫描时还要比 score),128 就到头。单元素 64 字节则是为了堵死"一个超大 value 把整个 listpack 撑爆、甚至触发频繁 realloc"的口子。

> **钉死这件事**:自适应阈值不是拍脑袋,是 listpack 线性扫描(小 n 时 cache 友好)和 dict/skiplist 高效结构(大 n 时复杂度占优)两条性能曲线的经验交点。hash 512 比 zset 128 大,是因为 hash 访问偏随机单点、listpack 的紧凑布局优势能撑更久;zset 同时维护有序,扫描代价高,更早切换。这种"阈值反映数据访问模式"的思路,比"统一 128"或"统一 512"要聪明——它承认不同类型的访问模式不同,该在不同的点切换。

更要紧的是阈值**可在线修改**(`MODIFIABLE_CONFIG`)。运维发现某类 hash 查找变慢,可以 `CONFIG SET hash-max-listpack-entries 256` 收紧;反过来,全是几十字节的短 hash、内存敏感,可以放宽到 1024。**把策略参数化、默认值保守、运行时可调**——这是 Redis 设计哲学里反复出现的味道(和 ae 的 `usUntilEarliestTimer` 注释那句 "not needed by Redis so far" 一脉相承:默认保守,但留口子)。

## 章末:回扣、五个为什么、往哪钻

### 主线回扣

本章是**取向②(内存即数据库)和取向③(编码自适应)的收口**。rax 用前缀压缩让"百万级前缀重叠的字符串集合"在内存里近乎免费地装下,直接落地取向②——既然数据常驻内存,省内存就是省一切。`iscompr` 一个比特区分两种节点布局、`raxLowWalk` 一次遍历喂四处调用、分裂按 splitpos 三段切割、合并靠 raxRemoveChild 降级——这四件事是 rax 在"省内存 + 代码简单"两个维度同时做到位的物理基础。编码自适应则是取向③的命名来源:**对外一套命令、对内多套编码,按数据形态自动选最优底层**。它做到三件事:用户无感(敲 `HSET` 永远成立)、小数据省内存(紧凑编码 listpack/intset)、大数据保性能(高效结构 dict/skiplist/quicklist)。**两道闸门(TryConversion 预判 + Convert 兜底)覆盖所有写入路径,阈值 MODIFIABLE_CONFIG 运行时可调**——这是"一个接口、多种实现"在系统设计里最朴素也最成功的实践。

### P2 收口:六种结构,各管一类访问模式

现在把 P2 串起来收口。从第七章到这里,我们其实拼出了一张"按访问模式选结构"的全景表:

| 访问模式 | 结构 | 章节 | 主轴取向 |
|---|---|---|---|
| 变长字符串(二进制安全、O(1) 取长) | **SDS** | 第七章 | ②③ |
| 键值/哈希单点查找(O(1)) | **dict** | 第八章 | ② |
| 小数据紧凑(无指针、cache 友好) | **listpack / intset** | 第六章/第八章 | ② |
| 长列表(两端 push/pop + 随机访问) | **quicklist** | 第六章 | ② |
| 有序集合(排名/范围/单点) | **skiplist + dict** | 第八章 | ②④ |
| 前缀重叠的字符串索引 | **rax** | 本章 | ② |

这六种结构各管一类访问模式,**而 `object.c` 的编码自适应是粘合剂**——它在写入路径的每个分叉点检查数据形态,决定让哪个结构顶上。用户眼中 Redis 是"五种类型",内核里其实是"六种结构 × 多种编码"的组合拳,且组合的选择是数据自己说了算。把这一切统一起来的是 `redisObject` 这个 16 字节小盒子([server.h](../../redis-8.0.2/src/server.h) 的 `redisObject` 定义):它打包了 type、encoding、refcount、LRU 时钟,让"五种类型 × 多种编码"在对外接口上看起来就是一个 `robj *`。这就是 P2 的全部:**数据结构的执掌,执掌的是每一种访问模式的最优解**。

### 五个为什么

**Q1:raxNode 为什么不存 parent 指针?删除回溯时怎么找到父节点?**
不存。每个节点省 8 字节,百万级树就是 GB 级节省。回溯靠 `raxLowWalk` 的 `raxStack *ts` 参数——走树时把沿途父节点临时压进一个外部栈(`raxStack`,[rax.h:124](../../redis-8.0.2/src/rax.h#L124))。这个栈还有个小优化:前 32 个父节点用栈上静态数组 `static_items`,超过才堆分配(`RAX_STACK_STATIC_ITEMS=32`,[rax.h:123](../../redis-8.0.2/src/rax.h#L123))。绝大多数树的深度 < 32(因为压缩节点大大缩短了路径),所以这条"32 之内零分配"的热路径优化很有价值。

**Q2:节点分裂时,如果新 key 完全不匹配压缩节点的首字符(case 4),前缀节点会怎样?**
不创建前缀节点。看 [rax.c:602-605](../../redis-8.0.2/src/rax.c#L602) 的 ALGO 1 注释 case 3/4:`IF $SPLITPOS == 0: Replace the old node with the split node`。splitpos=0 意味着没有共享前缀,直接用新建的扇出节点替换原压缩节点。这是 `if (trimmedlen) { trimmed = rax_malloc(...); }` 那个条件分支的作用——trimmedlen=0 时不分配前缀节点。代码用一个 if 把四种 case 的"前缀节点存不存在"统一掉了。

**Q3:`raxLowWalk` 为什么是 `static inline`?**
`static` 是因为它只在本文件用,不导出。`inline` 是性能考虑——rax 的所有操作(insert/find/remove/iterate)都要先调它,是最高频的内部函数。inline 展开省去一次函数调用的开销(参数入栈、寄存器保存、返回地址跳转),在百万级 Stream 操作下累积可观。这是 C 程序员"对热路径锱铢必较"的典型做法,和 dict 的 `dictHash` 也常用 inline 是同一思路。

**Q4:为什么 cluster slot→node 映射不用 rax?16384 个 slot 也不算少。**
因为 slot 号是 0-16383 的**连续小整数**,直接数组 `slots[CLUSTER_SLOTS]`(`cluster_legacy.h:343`)O(1) 索引,无哈希无树,16384 × 8 字节 = 128KB,常驻内存零成本。rax 的优势是"前缀压缩 + 范围扫描",但 slot 号是数字不是字符串、没有前缀重叠、也不需要范围扫描(只查单点),用 rax 是杀鸡用牛刀,常数还更大。**数据结构选型要看访问模式,不是看"听起来多高级"**——这是 P2 整篇反复强调的取向。

**Q5:编码自适应切换编码时,会不会阻塞主线程?**
小数据切换(listpack → hashtable)是同步的,但数据量小(刚超阈值),搬数据耗时很短(微秒级),不构成阻塞。真正可能慢的是大 zset 从 listpack 转 skiplist——要一次性建整个 skiplist 和 dict,元素越多越慢。但转 skiplist 的阈值是 128,转的时候最多 128 个元素,也是微秒级。**阈值的存在本身就是"切换成本可控"的保证**——只在数据量小到能快速切换时才切,切完之后再有新元素直接进新编码。所以用户永远不会遇到"一条命令触发了百万级数据的同步搬动"。这是编码自适应能"对用户透明"的底层保障。

### 想继续深入往哪钻

- 想看 rax 节点分裂的完整算法:读 [rax.c](../../redis-8.0.2/src/rax.c) 的 `raxGenericInsert`([rax.c:487](../../redis-8.0.2/src/rax.c#L487)),特别是它那段长达 50 行的注释([rax.c:530-652](../../redis-8.0.2/src/rax.c#L530)),把五种分裂情形画得清清楚楚——这是 Redis 源码里最值得读的注释之一。
- 想理解 stream 怎么用 rax:读 [t_stream.c](../../redis-8.0.2/src/t_stream.c) 的 `streamNew`([t_stream.c:48](../../redis-8.0.2/src/t_stream.c#L48))、`streamAppendItem`、`xrangeCommand`——看消息怎么挂到 rax、范围扫描怎么走。
- 想看 rax 的迭代器完整实现:读 [rax.c](../../redis-8.0.2/src/rax.c) 的 `raxIteratorNextStep`/`raxIteratorPrevStep`(`raxNext` 内部调的函数),以及 `raxSeek`([rax.c:1518](../../redis-8.0.2/src/rax.c#L1518))的七种操作符分支。
- 想对比"语言级泛型"怎么解"一套接口多种底层":看本系列《Tokio 设计与实现》的 trait object 章节——Tokio 用 Rust trait 表达"多种异步 IO 后端",Redis 用 `robj->encoding` 字段 + 函数指针分发表达"多种底层编码",思路相通但语言机制不同。
- 想理解 client-side caching 怎么用 rax:读 [tracking.c](../../redis-8.0.2/src/tracking.c) 的 `PrefixTable`([tracking.c:25](../../redis-8.0.2/src/tracking.c#L25))和 `trackingRememberKeyToBroadcast`——rax 按前缀建索引,key 是订阅前缀(高度重叠)。

### 引出下一章

至此 P2 收口。我们已经看清 Redis 数据结构的全部执掌:SDS 管字符串、dict 管哈希、listpack/intset 管小数据紧凑、quicklist 管长列表、skiplist+dict 管有序、rax 管前缀重叠。但还有个问题悬着:每种类型有多种编码、每个 key 又属于某个 db、db 里又有过期、有 LRU/LFU 淘汰——这些**元信息**存在哪?robj 又是怎么把 type、encoding、refcount、LRU 时钟打包成一个 16 字节头的?当编码切换发生时,robj 头要不要动?为什么 Redis 的字符串对象有"共享整数池"这种优化,而 hash 对象没有?这些问题的答案都在 `server.h` 的 `redisObject` 定义和 `object.c` 的 create* 系列里——那是 P3 的舞台。下一章我们打开 `redisObject`,看清这个把"五种类型 × 多种编码"统一起来的、16 字节的小盒子,以及它如何承载 Redis 的淘汰与共享机制。

---

## 验证物:如何亲手确认本章的设计

> 说明:本书写作环境为 Windows,无法直接运行 redis-server(8.0 依赖 fork/epoll 等 Linux 特性)。以下 (1) gdb 断点脚本 (2) 源码常量锚点 (3) OBJECT ENCODING 观察项 均为可复现的精确指引,供读者在 Linux 环境(Ubuntu 22.04 / CentOS 8 等)对 redis-8.0.2 源码 `make no-opt`(Makefile 里 no-opt 目标会去掉 -O2 加 -g)编译后自行验证。**本书不附编造的运行输出**——凡未实跑的,只给脚本与预期观察变量,不写具体数值。

### 1. gdb 断点脚本

编译:`cd redis-8.0.2 && make no-opt`(带 -g)
启动:`gdb ./src/redis-server`,另一终端 `redis-cli`。

```gdb
(gdb) break raxNew              # 建空 rax,rax.c:176
(gdb) break raxLowWalk          # 共用走树函数,rax.c:437
(gdb) break raxGenericInsert    # 插入主流程(含分裂),rax.c:487
(gdb) break rax.c:655           # ALGO 1 入口:h->iscompr && i != len
(gdb) break raxRemove           # 删除主流程(含合并),rax.c:1001
(gdb) break raxRemoveChild      # 摘孩子,rax.c:928
(gdb) break raxCompressNode     # 重新压缩,rax.c:375
(gdb) break raxSeek             # 迭代器定位,rax.c:1518
(gdb) break raxNext             # 顺序遍历,rax.c:1682
(gdb) run --port 6379

# redis-cli 执行: XADD mystream '*' field1 value1
# gdb 在 raxGenericInsert 停下:
(gdb) print len                 # 预期:输入 key 长度(Stream ID 字符串长度)
(gdb) print i                   # 预期:raxLowWalk 返回的匹配字符数
(gdb) print h->iscompr          # 预期:停止节点是否压缩
(gdb) print h->size             # 预期:节点 size(压缩=字符串长/扇出=孩子数)
(gdb) print j                   # 预期:若 h->iscompr,这是 splitpos
# 继续到 XADD mystream '*' field2 value2(不同 ID,可能触发分裂):
(gdb) continue                  # 在 rax.c:655 停下时,观察 trimmedlen/postfixlen:
(gdb) print trimmedlen          # 预期:splitpos = 共享前缀长度
(gdb) print postfixlen          # 预期:原节点后缀长度(size - splitpos - 1)
```

**预期观察**(基于源码 [rax.c:655-700](../../redis-8.0.2/src/rax.c#L655) 的分裂算法,本书未实跑):往有内容的 stream 里 XADD 不同毫秒 ID 的消息,如果新 ID 与已有 ID 共享前缀但在中间字符分叉,`trimmedlen + postfixlen + 1 == 原节点 size`(`+1` 是分叉字符)。若新 ID 是已有 ID 的前缀(走到 case 5/ALGO 2),不进 rax.c:655 分支。

### 2. 源码常量锚点(带行号,从 redis-8.0.2 源码 Grep 核实)

| 常量/字段 | 位置 | 值/说明 |
|----------|------|---------|
| `raxNode` 结构 | rax.h:78-111 | iskey/isnull/iscompr/size:29 位段 + data[] 柔性数组 |
| `raxNode` 两种布局注释 | rax.h:83-109 | 扇出 `[abc][a/b/c-ptr]` vs 压缩 `[xyz][z-ptr]` |
| `RAX_NODE_MAX_SIZE` | rax.h:77 | (1<<29)-1,size 字段上限 |
| `raxPadding` 宏 | rax.c:130 | 按指针对齐补 padding |
| `raxNodeCurrentLength` 宏 | rax.c:150 | 两种布局 + 可选 value 四种组合 |
| `raxNewNode` | rax.c:161 | 分配非压缩节点 |
| `raxNew` / `raxNewWithMetadata` | rax.c:176 / 181 | 建空树,numnodes=1 |
| `raxCompressNode` | rax.c:375 | 把 0-孩子节点变压缩节点 |
| `raxLowWalk` | rax.c:437 | 共用走树,返回 i/stopnode/plink/splitpos |
| `raxGenericInsert` | rax.c:487 | 插入主流程,分裂算法入口 |
| 分裂算法注释(5 种 case) | rax.c:530-652 | "ANNIBALE" 系列例子 |
| ALGO 1 入口 | rax.c:655 | `if (h->iscompr && i != len)` |
| `raxRemoveChild` | rax.c:928 | 摘孩子,压缩 parent 降级为扇出 |
| `raxRemove` | rax.c:1001 | 删除主流程,回溯压缩 |
| 回溯压缩逻辑 | rax.c:1068-1180 | trycompress + 收集单孩子链 |
| `raxSeek` | rax.c:1518 | 七种操作符 =/>/</>=/<=/^/$ |
| `raxNext` / `raxPrev` | rax.c:1682 / 1697 | 调 raxIteratorNextStep/PrevStep |
| `raxStack`(RAX_STACK_STATIC_ITEMS=32) | rax.h:123-131 | 前 32 个父节点栈上零分配 |
| `OBJ_ENCODING_STREAM` 注释 | server.h:990 | "radix tree of listpacks" |
| Stream 主索引 `s->rax` | stream.h:17 / t_stream.c:48 | raxNew() |
| Stream PEL `rax *pel` | stream.h:64 / 82 | 消费者组/消费者 PEL |
| tracking PrefixTable | tracking.c:25 | `rax *PrefixTable` |
| **cluster slot→node(不是 rax)** | cluster_legacy.h:343 | `clusterNode *slots[CLUSTER_SLOTS]` 直接数组 |
| hash 阈值 | config.c:3238 / 3244 | 512 entries / 64 value |
| zset 阈值 | config.c:3242 / 3246 | 128 entries / 64 value |
| set 阈值 | config.c:3239-3241 | intset 512 / listpack 128 / value 64 |
| list 阈值 | config.c:3173 / 3195 | size -2 / depth 0 |
| `hashTypeTryConversion` | t_hash.c:605 | 预判闸门 |
| `hashTypeSet` | t_hash.c:895 | 兜底闸门 |
| `hashTypeConvert` | t_hash.c:1687 | listpack→hashtable |
| `zsetConvert` / `zsetConvertAndExpand` | t_zset.c:1250 / 1255 | listpack↔skiplist |
| `setTypeConvert` / `setTypeConvertAndExpand` | t_set.c:479 / 487 | intset/listpack/hashtable 三级 |

### 3. OBJECT ENCODING 观察项(需本地 redis-server)

> 以下操作需在 Linux 本地启动 redis-server 后用 redis-cli 执行。本书未实跑,仅列观察方法与预期切换点(阈值来自 config.c 默认值,可 `CONFIG GET` 确认)。

```text
# 观察 hash 编码从 listpack → hashtable 的切换:
127.0.0.1:6379> CONFIG GET hash-max-listpack-entries   # 预期 512
127.0.0.1:6379> CONFIG GET hash-max-listpack-value     # 预期 64
127.0.0.1:6379> DEL myhash
127.0.0.1:6379> HSET myhash f1 v1                      # 1 个字段
127.0.0.1:6379> OBJECT ENCODING myhash                 # 预期 listpack
# 循环 HSET 到 513 个字段(超过阈值 512):
127.0.0.1:6379> OBJECT ENCODING myhash                 # 预期 hashtable(超 512 升级)

# 观察 hash 单元素大小触发切换:
127.0.0.1:6379> DEL bighash
127.0.0.1:6379> HSET bighash f1 $(head -c 100 /dev/urandom | base64)  # value > 64 字节
127.0.0.1:6379> OBJECT ENCODING bighash                # 预期 hashtable(单 value 超 64 字节)

# 观察 zset 编码切换:
127.0.0.1:6379> CONFIG GET zset-max-listpack-entries   # 预期 128
127.0.0.1:6379> DEL myzset
127.0.0.1:6379> ZADD myzset 1 a                        # 1 个元素
127.0.0.1:6379> OBJECT ENCODING myzset                 # 预期 listpack
# 循环 ZADD 到 129 个元素(超过阈值 128):
127.0.0.1:6379> OBJECT ENCODING myzset                 # 预期 skiplist(升级为 dict+skiplist)

# 观察 set 三段编码切换(intset → listpack → hashtable):
127.0.0.1:6379> CONFIG GET set-max-intset-entries      # 预期 512
127.0.0.1:6379> CONFIG GET set-max-listpack-entries    # 预期 128
127.0.0.1:6379> DEL myset
127.0.0.1:6379> SADD myset 1 2 3                       # 全是整数
127.0.0.1:6379> OBJECT ENCODING myset                  # 预期 intset
127.0.0.1:6379> SADD myset hello                       # 加入字符串
127.0.0.1:6379> OBJECT ENCODING myset                  # 预期 listpack(非整数,但 < 128)
# 循环 SADD 字符串元素到 129 个:
127.0.0.1:6379> OBJECT ENCODING myset                  # 预期 hashtable(超 128 升级)

# 观察 stream 的 OBJECT ENCODING(stream 永远是 radix tree of listpacks):
127.0.0.1:6379> XADD s '*' f v
127.0.0.1:6379> OBJECT ENCODING s                      # 预期 stream(OBJ_ENCODING_STREAM=10)
```

标注:以上预期基于源码常量与 [config.c](../../redis-8.0.2/src/config.c) 默认阈值推导,本书未在本地实跑;若你的 redis 版本/配置不同,切换点可能偏移,以 `CONFIG GET` 实际值为准。stream 的编码是固定的 `OBJ_ENCODING_STREAM`,不会切换(它就是"radix tree of listpacks"一种编码)。

---

> P2 完。第九章交付:raxNode 两种内存布局(iscompr 比特 + 字节级 ASCII 图)、raxLowWalk 一次遍历喂四处调用、节点分裂三段切割(splitpos/trimmedlen/postfixlen)、节点合并(raxRemoveChild 降级 + 回溯压缩)、rax 真实用处(stream 主索引/PEL/消费者组名/tracking/acl)+ **诚实标注 cluster slot 用直接数组、SCAN 用 dict 不用 rax**、编码自适应两道闸门(TryConversion 预判 + Convert 兜底)、各类型阈值一览(hash 512/64、zset 128/64、set intset512/listpack128、list -2)、P2 六结构收口表、章末五为什么、验证物三段。
