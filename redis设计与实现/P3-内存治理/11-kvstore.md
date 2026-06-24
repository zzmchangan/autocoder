# 第十一章 · kvstore:8.0 的分片字典,凭什么把 cluster 键空间切成 16384 份

> 篇:P3 内存治理
> 主轴呼应:这一章是**取向①(把耗时从主线程解放)在 cluster 键空间的落地**。8.0 之前,一个 `redisDb` 只有一个 `dict` 装天下;cluster 模式一来,迁移、过期、淘汰、SCAN 全都被迫盯着"整个键空间"这一种粒度做。kvstore 的解法是:**为每个 hash slot 配一个独立的 dict**,把所有按 slot 粒度的操作从 O(全库) 降到 O(该 slot 键数);把一个"大 dict 的渐进 rehash"换成"很多小 dict 轮流渐进且时间预算可控"。它是 cluster 时代 Redis 的存储骨架,也是"化整为零"这条主轴最干净的一次落地。

---

## 读完本章你会明白

1. **为什么 8.0 要把一个 dict 拆成每 slot 一个 dict**——因为 cluster 把键空间切成 16384 个物理 slot,迁移/过期/淘汰/SCAN 都该按 slot 粒度操作,而单 dict 模型强行把"键空间"和"slot 边界"这两件正交的事耦合在一起,迁移一个 slot 要扫全库。
2. **为什么 kvstore 要用一棵 Fenwick 树(二叉索引树)来记每个 slot 的键数**——它能把"全局第 k 个 key 落在哪个 slot"这道 RANDOMKEY/淘汰抽样的难题,从 O(n) 线性扫压到 O(log n) 反查,而且增删也是 O(log n)。
3. **SCAN 游标那 64 位是怎么被劈成两半的**——低 `num_dicts_bits` 位装 slot 号,高位装 dict 内部的 reverse-bit 桶游标,两层各管各、组合正确。这是分层抽象在游标设计上的一次胜利。
4. **16384 个 dict 同时要 rehash,kvstore 凭什么不把它们一次性做完、也不让任何一个饿死**——靠一个时间闸 `threshold_us` + 一个 rehashing 链表的队首轮转 + 一个 `resize_cursor` 的环形公平游标,把"一次大手术"切成"很多次小缝合"。
5. **这笔账到底值不值**——16384 个 `dict*` 指针 128 KB + Fenwick 树 ~128 KB,是 cluster 模式的固定开销;换来的是迁移 O(slot 键数)、过期精度到 slot、SCAN 可钉单 slot。算清楚这笔账,你才会明白为什么 8.0 走这条路。

---

> **如果一读觉得太难:先只记住三件事**——
> ① kvstore = 一个 `dict **dicts` 数组 + 一圈计数器,**每个 slot(或每个分片)对应一个独立的 dict**,cluster 下是 16384 个,单机退化成 1 个;
> ② 全局统计(总键数、第 k 个 key 在哪)靠一棵 **Fenwick 树** `dict_size_index`,增删查都是 O(log n);
> ③ rehash 不再是一次做完——`rehashing` 链表队首轮转 + `threshold_us` 时间闸 + `resize_cursor` 环形游标,保证每个子 dict 都有机会被伺候、主线程绝不被卡。
> 这三件事,就是 kvstore 的全部。

---

> **一句话点破:kvstore 不是新发明的哈希表,它是 dict 的"分片容器"——把一个原本该装全库的大 dict,拆成 16384 个 slot 粒度的小 dict,让 cluster 下每一件按 slot 粒度该干的事(迁移、过期、淘汰、SCAN)都从 O(全库) 降到 O(slot 键数);代价是多花 ~256 KB 固定开销和一棵 Fenwick 树的 O(log n) 维护,换来的是 cluster 时代键空间操作的物理隔离。**

第五章我们看清了 `dict` 这张哈希表本身——拉链法、渐进式 rehash、reverse-bit 游标。可是,**Redis 真正存 key 的地方,从来不是"一个 dict",而是"一组 dict"**。这一章就要走进这"一组 dict"的总管:`kvstore.c`。它是 8.0 cluster 模式下整个键空间的物理骨架。

## 11.1 这块要解决什么:cluster 键空间,一个 dict 装天下为什么不行

第五章讲过,单机模式下,一个 `redisDb` 只需要两个 dict:`db->keys` 存键值,`db->expires` 存过期时间。一切岁月静好。可一旦打开 `cluster-enabled yes`,世界就变了。

Cluster 把整个键空间切成 **16384 个 hash slot**([cluster.h:9](../../redis-8.0.2/src/cluster.h#L9),`CLUSTER_SLOTS = 1<<14`)。每个 key 由 CRC16 取模落进某个 slot;集群再把 slot 分配到不同节点。这意味着一件极关键的事:**属于同一个 slot 的 key,命运是绑在一起的**——它们要么整体属于本节点,要么被整体迁移到另一个节点。slot 是 cluster 的物理边界。

迁移是 cluster 的日常。扩容、缩容、rebalance,都伴随着 slot 在节点间的搬移。而 slot 迁移的本质操作是"把这个 slot 里所有的 key 捞出来,搬到对端,再从本地删掉"。这里就撞上了旧版 Redis(7.x 及以前)的痛:

旧版一个 `redisDb` 只有一个 `dict` 装所有 key。要迁移 slot N,你必须**遍历整个大 dict,逐个判断每个 key 属不属于 slot N**,符合的才搬。16384 个 slot 共用一个 dict,迁移一个 slot 却要扫全部 key——这是 O(总键数) 的操作,而不是 O(该 slot 键数)。一个有上千万 key 的节点,搬一个 slot 要把千万级 dict 全过一遍,而且每一把都是主线程上的阻塞点。

> **不这样会怎样**:不只是迁移痛。过期扫描(active expire)、LRU 淘汰抽样、SCAN 命令、`RANDOMKEY`、`countkeysinslot`——这些原本就该"按 slot 粒度"操作的事,在单 dict 模型下全被搅成一锅粥,只能看到"所有 key"这一种粒度。比如 cluster 下客户端发 `CLUSTER COUNTKEYSINSLOT N`,你只能 O(全库) 扫一遍大 dict 数属 slot N 的 key;而这条命令的语义本该是 O(slot 键数) 的局部操作。粒度错位,是所有这些痛的根。

痛点的根因只有一句话:**单个 dict 把"键空间"和"slot 边界"这两件正交的事情耦合在了一起**。8.0 的解法直球而彻底——既然 slot 是天然的物理边界,那就**为每个 slot 配一个独立的 dict**,让"键空间"在结构上就按 slot 切好。slot 是几号,就去找 `dicts[slot]`;迁移 slot N,只动 `dicts[N]` 这一个 dict;过期扫描、淘汰抽样、SCAN,统统可以钉在单 slot。这就是 kvstore。

> **钉死这件事**:kvstore 解决的不是"哈希表怎么实现"——那是第五章 dict 的事;它解决的是"键空间怎么按 slot 切片"。cluster 下 slot 是物理边界,迁移/过期/淘汰/SCAN 都该按 slot 粒度操作,而单 dict 模型强行把"键空间"和"slot 边界"耦合,导致所有按 slot 的操作都被迫升级成 O(全库)。kvstore 把这种耦合从根上解开:**结构上就按 slot 切好,操作自然落到 slot 粒度**。

## 11.2 数据结构:一个 dict 数组的总管

### 11.2.1 结构总览:dict 数组 + 一圈计数器

kvstore 不是新发明的哈希表,它是一组 `dict` 的**容器与管理者**。核心结构在 [kvstore.c:37](../../redis-8.0.2/src/kvstore.c#L37):

```c
/* kvstore.c:37-53 */
struct _kvstore {
    int flags;
    dictType dtype;
    dict **dicts;                          /* dict 数组:每 slot(或每分片)一个 */
    long long num_dicts;                   /* 数组长度 = 2^num_dicts_bits */
    long long num_dicts_bits;              /* log2(数组长度),决定游标布局 */
    list *rehashing;                       /* 正在 rehash 的子 dict 链表 */
    int resize_cursor;                     /* 轮转扩缩容的环形游标 */
    int allocated_dicts;                   /* 已分配的子 dict 数(按需分配) */
    int non_empty_dicts;                   /* 非空子 dict 数 */
    unsigned long long key_count;          /* 全库总键数(O(1) 读取) */
    unsigned long long bucket_count;       /* 全库总桶数 */
    unsigned long long *dict_size_index;   /* Fenwick 树,按 slot 累加键数 */
    size_t overhead_hashtable_lut;         /* 桶指针开销 */
    size_t overhead_hashtable_rehashing;   /* rehash 中旧表开销 */
    void *metadata[];
};
```

这张表是本章后面所有动作的舞台。先记住四个最关键的字段:

- **`dicts`**:核心。一个 `dict*` 指针数组,长度 `num_dicts`。cluster 下是 16384;单机退化成 1。slot 是几号,就找 `dicts[slot]`。
- **`dict_size_index`**:一棵 **Fenwick 树(二叉索引树,BIT)**,把"每个 slot 有多少 key"组织成可 O(log n) 查询、O(log n) 增删的前缀和结构。它是 RANDOMKEY 和淘汰抽样的命门,11.3 节专讲。
- **`rehashing`**:一个链表,挂着所有"正在 rehash 还没搬完"的子 dict。增量 rehash 时从这里队首取,11.5 节专讲。
- **`resize_cursor`**:一个环形游标,`kvstoreTryResizeDicts` 用它公平地轮转检查所有子 dict 的扩缩容需求,11.5 节专讲。

把这四个字段连同它们的关系画出来,就是 kvstore 的全貌:

```text
                       struct _kvstore  (kvstore.c:37)
 ┌──────────────────────────────────────────────────────────────────────┐
 │  num_dicts = 16384 (cluster) / 1 (standalone)    num_dicts_bits = 14 │
 │  key_count = 1,234,567   (O(1) 读全库总键数)                          │
 │  resize_cursor ──┐    (环形,公平轮转)                                  │
 │                  │                                                     │
 │  dicts[] ──┐     │                                                     │
 │            │     │      rehashing (链表,队首轮转)                      │
 │            ↓     ↓         ↓                                           │
 │  ┌──┬──┬──┬──┬──┬──┬...┬────┬...┬──┐   ┌───┐ ┌───┐ ┌───┐           │
 │  │D0│D1│D2│D3│..│..│..│Dk  │.. │..│←─│D3 │→│D7 │→│D9 │→ NULL      │
 │  └──┴──┴──┴──┴──┴──┴...┴────┴...┴──┘   └───┘ └───┘ └───┘           │
 │   ↑  ↑  ↑   ↑       ↑                                                  │
 │   │  │  │   │       └─ slot k 的 dict(可能正在 rehash,挂在链表里)    │
 │   │  │  │   └─ 未分配(NULL,空 slot,cluster 下 slot 还没迁过来)      │
 │   │  │  └─ slot 2 的 dict                                              │
 │   │  └─ slot 1 的 dict                                                 │
 │   └─ slot 0 的 dict                                                    │
 │                                                                        │
 │  dict_size_index[] ── Fenwick 树 (长度 num_dicts+1,1-based)           │
 │  ┌──┬──┬──┬──┬──┬...┬─────┬...┬──────┐                                │
 │  │ 0│C1│C2│C3│C4│...│ Ck  │...│ Cn   │   Ci = 区间和,lowbit 爬升      │
 │  └──┴──┴──┴──┴──┴...┴─────┴...┴──────┘   支持"第 target 个 key 在哪"  │
 └──────────────────────────────────────────────────────────────────────┘
```

### 11.2.2 按需分配 + 空了即还:`KVSTORE_ALLOCATE_DICTS_ON_DEMAND` / `KVSTORE_FREE_EMPTY_DICTS`

注意一个关键设计:`dicts` 是裸指针数组,**不是预分配的 dict**。配合标志位 `KVSTORE_ALLOCATE_DICTS_ON_DEMAND`([kvstore.h:43](../../redis-8.0.2/src/kvstore.h#L43)),子 dict 是**第一次写入时才创建**的——`createDictIfNeeded`([kvstore.c:163](../../redis-8.0.2/src/kvstore.c#L163)):

```c
/* kvstore.c:163-170 */
static dict *createDictIfNeeded(kvstore *kvs, int didx) {
    dict *d = kvstoreGetDict(kvs, didx);
    if (d) return d;
    kvs->dicts[didx] = dictCreate(&kvs->dtype);   /* 懒创建 */
    kvs->allocated_dicts++;
    return kvs->dicts[didx];
}
```

16384 个 slot 在 cluster 下并不都会被本节点持有——一个节点通常只负责几千个 slot。预分配 16384 个空 dict 是纯浪费(每个 dict 结构 ~96 字节,全分配就是 ~1.5 MB 的死重)。懒分配让 `dicts` 数组本身(16384 个 `dict*` 指针,128 KB)始终占着,但每个空 slot 的 dict 结构只在真有 key 写入时才落地。

对称地,空 dict 可被回收。对应标志 `KVSTORE_FREE_EMPTY_DICTS`([kvstore.h:44](../../redis-8.0.2/src/kvstore.h#L44)),当某 slot 的最后一个 key 被删,`freeDictIfNeeded` 会把整个 dict 释放掉([kvstore.c:179](../../redis-8.0.2/src/kvstore.c#L179)):

```c
/* kvstore.c:179-188 */
static void freeDictIfNeeded(kvstore *kvs, int didx) {
    if (!(kvs->flags & KVSTORE_FREE_EMPTY_DICTS) ||
        !kvstoreGetDict(kvs, didx) ||
        kvstoreDictSize(kvs, didx) != 0 ||
        kvstoreDictIsRehashingPaused(kvs, didx))
        return;
    dictRelease(kvs->dicts[didx]);
    kvs->dicts[didx] = NULL;
    kvs->allocated_dicts--;
}
```

cluster 模式下 slot 迁走后,dict 即时消失,内存归还。注意 11.6 节会讲到一个微妙的点:`freeDictIfNeeded` 在 safe iterator / Scan 上下文里**不会立即释放**——`kvstoreDictIsRehashingPaused` 的检查把这件事挡住了,注释([kvstore.c:172-178](../../redis-8.0.2/src/kvstore.c#L172))明说:"for rehashing dicts, that is, in the case of safe iterators and Scan, we won't delete the dict. We will check whether it needs to be deleted when we're releasing the iterator."

### 11.2.3 cluster 与非 cluster:同一个 kvstore

这是优雅的统一。看 `initTempDb`([db.c:667](../../redis-8.0.2/src/db.c#L667))怎么建库:

```c
/* db.c:667-680,精简 */
redisDb *initTempDb(void) {
    int slot_count_bits = 0;                                  /* 非 cluster:2^0 = 1 个 dict */
    int flags = KVSTORE_ALLOCATE_DICTS_ON_DEMAND;
    if (server.cluster_enabled) {
        slot_count_bits = CLUSTER_SLOT_MASK_BITS;            /* 14 → 2^14 = 16384 个 dict */
        flags |= KVSTORE_FREE_EMPTY_DICTS;
    }
    /* ... */
    tempDb[i].keys    = kvstoreCreate(&dbDictType, slot_count_bits, flags | KVSTORE_ALLOC_META_KEYS_HIST);
    tempDb[i].expires = kvstoreCreate(&dbExpiresDictType, slot_count_bits, flags);
    /* ... */
}
```

`CLUSTER_SLOT_MASK_BITS = 14`([cluster.h:8](../../redis-8.0.2/src/cluster.h#L8)),`2^14 = 16384`,正好是 cluster 的 slot 总数。非 cluster 则 `num_dicts_bits = 0`、`num_dicts = 1`——**退化成单 dict**。

kvstore 内部处处有 `if (kvs->num_dicts == 1)` 的快路径:`cumulativeKeyCountRead`([kvstore.c:104](../../redis-8.0.2/src/kvstore.c#L104))、`addDictIndexToCursor`([kvstore.c:118](../../redis-8.0.2/src/kvstore.c#L118))、`cumulativeKeyCountAdd`([kvstore.c:148](../../redis-8.0.2/src/kvstore.c#L148))、`kvstoreSize`([kvstore.c:349](../../redis-8.0.2/src/kvstore.c#L349))、`kvstoreFindDictIndexByKeyIndex`([kvstore.c:540](../../redis-8.0.2/src/kvstore.c#L540))、`kvstoreGetNextNonEmptyDictIndex`([kvstore.c:571](../../redis-8.0.2/src/kvstore.c#L571))。单 dict 模式下不走 Fenwick 树、不切游标,开销和旧版一字不差。**同一份代码、同一条调用链,cluster 多 dict、单机单 dict**——这是取向④(简单优先)的典型体现:不为 cluster 的复杂度拖累单机。

而 key 落到哪个 dict,由 slot 决定。`calculateKeySlot` 在 cluster 下算 CRC16 取模([db.c:284](../../redis-8.0.2/src/db.c#L284)),非 cluster 恒返回 0:

```c
/* db.c:284-286 */
int calculateKeySlot(sds key) {
    return server.cluster_enabled ? keyHashSlot(key, (int) sdslen(key)) : 0;
}
```

于是 `dbAdd` 写一个 key,内部走 `dbAddInternal`([db.c:258](../../redis-8.0.2/src/db.c#L258))→ `kvstoreDictAddRaw(db->keys, slot, ...)`([kvstore.c:860](../../redis-8.0.2/src/kvstore.c#L860)),slot 就是 dict 的下标。这一步是 O(1) 的——数组下标寻址,无哈希、无搜索。

> **钉死这件事**:kvstore 用 `num_dicts_bits` 一个参数把 cluster 多 dict 和单机单 dict 统一进同一条代码路径,`num_dicts == 1` 时处处走快路径不碰 Fenwick、不切游标。这是"复杂度按需付钱"的范式——cluster 的开销只在 cluster 真打开时才付,单机用户一分钱不多花。

## 11.3 技巧精解①:Fenwick 树——把"第 k 个 key 在哪个 slot"压到 O(log n)

### 11.3.1 这块要解决什么:全局均匀抽样

把一个大 dict 拆成 16384 个小 dict,立刻带来一个新问题:**旧的 O(1) 全局统计没了**。

`RANDOMKEY` 命令、LRU 淘汰抽样,都需要回答一个隐含的问题:**在全库里"均匀"地抽一个 key**。"均匀"的意思是按 key 数加权——slot A 有 1000 个 key、slot B 有 10 个 key,那么抽到的概率应该正比于键数,A 是 B 的 100 倍。

如果没有 kvstore 这层,一个大 dict 里 `dictGetRandomKey` 一步搞定。但现在 16384 个独立的小 dict,你怎么"均匀"?

最笨的两种办法都不行:

- **办法一:线性扫。** 每次抽样前扫一遍 16384 个 dict 求前缀和,累计出全库总数,再 `random() % total`,再线性找落在哪个 slot。**这是 O(n),n=16384。** `RANDOMKEY` 每次都 O(n),淘汰批量抽样时更是雪上加霜。
- **办法二:维护前缀和数组。** 开一个 16384 长度的数组,`prefix[i] = slot 0..i-1 的键数之和`。查询是 O(log n) 二分,但**增删一个 key 要 O(n) 重建后缀**——写入路径每次多花 O(n),不可接受。

Fenwick 树(二叉索引树,Binary Indexed Tree)就是这两者的甜蜜点:**O(log n) 增删 + O(log n) 查询,常数极小,内存就一个长度 n+1 的数组。**

### 11.3.2 Fenwick 树是什么:lowbit 把数组切成"区间管辖区"

Fenwick 树的核心思想是:**用一个长度 n+1 的数组 `tree[]`,让 `tree[i]` 管"从 i 往前数 lowbit(i) 个元素的和"这段区间**。`lowbit(i) = i & (-i)`,即 i 的二进制最低位的 1。

举一个 n=8 的小例子,每个 slot 的真实键数是 `a[1..8]`:

```text
Fenwick 树的"区间管辖区" (n=8)

  原数组下标:   1     2     3     4     5     6     7     8
  原数组 a[]:   a1    a2    a3    a4    a5    a6    a7    a8

  tree[i] 管辖区间 (长度 = lowbit(i)):
  tree[1] 管领 [1]               ← lowbit(1)=1,管 1 个
  tree[2] 管领 [1,2]             ← lowbit(2)=2,管 2 个
  tree[3] 管领 [3]               ← lowbit(3)=1,管 1 个
  tree[4] 管领 [1,2,3,4]         ← lowbit(4)=4,管 4 个
  tree[5] 管领 [5]               ← lowbit(5)=1,管 1 个
  tree[6] 管领 [5,6]             ← lowbit(6)=2,管 2 个
  tree[7] 管领 [7]               ← lowbit(7)=1,管 1 个
  tree[8] 管领 [1..8]            ← lowbit(8)=8,管 8 个

  画成树形(每个节点覆盖正下方一段):

              tree[8] ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
              [1..8]                                              │
                                  tree[4] ━━━━━━━━━━━━━━━━━━━┓    │
                                  [1..4]                      │    │
                    tree[2] ━━━━━━━━━━━┓                      │    │
                    [1..2]              │                      │    │
              tree[1] [1]               │ tree[3] [3]          │    │
                                  ───────────────────────  tree[6] ━┓ [5..6]
                                                            tree[5]│ [5]
                                                                  ││ tree[7] [7]
```

两个魔法操作(都是 O(log n)):

**① 查询前缀和 `prefix(i) = a[1] + ... + a[i]`**——从 i 往 lowbit 方向"下降",一路累加。kvstore 的实现是 `cumulativeKeyCountRead`([kvstore.c:103](../../redis-8.0.2/src/kvstore.c#L103)):

```c
/* kvstore.c:108-114 */
int idx = didx + 1;                /* BIT 是 1-based,slot 是 0-based,所以 +1 */
unsigned long long sum = 0;
while (idx > 0) {
    sum += kvs->dict_size_index[idx];
    idx -= (idx & -idx);           /* lowbit:抹掉最低位的 1,往下降 */
}
return sum;
```

比如查 `prefix(7)`:idx=7(管 [7])→ idx=7-1=6(管 [5,6])→ idx=6-2=4(管 [1..4])→ idx=4-4=0 停。累加 `tree[7]+tree[6]+tree[4] = a7 + (a5+a6) + (a1+a2+a3+a4) = a1..a7`。三次跳转覆盖 7 个 slot。

**② 单点更新 `a[i] += delta`**——从 i 往 lowbit 方向"爬升",一路把所有管辖区包含 i 的 tree 节点都加上 delta。kvstore 的实现是 `cumulativeKeyCountAdd`([kvstore.c:137](../../redis-8.0.2/src/kvstore.c#L137)),注释([kvstore.c:134-136](../../redis-8.0.2/src/kvstore.c#L134))还贴心地给了 Fenwick 树的维基百科链接:

```c
/* kvstore.c:134-136 原注释 */
/* Updates binary index tree (also known as Fenwick tree), increasing key count for a given dict.
 * You can read more about this data structure here https://en.wikipedia.org/wiki/Fenwick_tree
 * Time complexity is O(log(kvs->num_dicts)). */

/* kvstore.c:152-159 */
int idx = didx + 1;                /* BIT 1-based */
while (idx <= kvs->num_dicts) {
    if (delta < 0) {
        assert(kvs->dict_size_index[idx] >= (unsigned long long)labs(delta));
    }
    kvs->dict_size_index[idx] += delta;
    idx += (idx & -idx);           /* lowbit:加上最低位的 1,往上升 */
}
```

比如更新 `a[5] += 1`:idx=5(管 [5])→ idx=5+1=6(管 [5,6])→ idx=6+2=8(管 [1..8])→ idx=8+8=16 越界停。所有这三个节点的管辖区都"覆盖 slot 5",都要 +1。三次跳转。

**关键洞察:查询走"减 lowbit"下降,更新走"加 lowbit"上升——是对称的两个方向,都用 `idx & -idx` 这个位运算技巧在 O(log n) 次跳转里走完。** `idx & -idx` 利用补码表示,一条指令就把最低位的 1 抠出来。

### 11.3.3 反查:O(log n) 找"第 k 个 key 落在哪个 slot"

Fenwick 树最惊艳的能力是**反查**:给定一个全局序号 `target`,找它落在哪个 slot。这就是 `kvstoreFindDictIndexByKeyIndex`([kvstore.c:539](../../redis-8.0.2/src/kvstore.c#L539))。源码注释([kvstore.c:522-538](../../redis-8.0.2/src/kvstore.c#L522))给了一个漂亮的图示:

```text
/*  源码注释原图(kvstore.c:523-527):
 *  Finds a dict containing target element in a key space ordered by dict index.
 *  Consider this example. Dictionaries are represented by brackets and keys by dots:
 *   #0   #1   #2     #3    #4
 *  [..][....][...][.......][.]
 *                     ^
 *                  target
 *  In this case dict #3 contains key that we are trying to find.
 */
```

算法本身是一个**从高位到低位贪心走**的二进制分解([kvstore.c:544-553](../../redis-8.0.2/src/kvstore.c#L544)):

```c
/* kvstore.c:544-553 */
int result = 0, bit_mask = 1 << kvs->num_dicts_bits;
for (int i = bit_mask; i != 0; i >>= 1) {
    int current = result + i;
    /* 当 target 大于 'current' 节点值时,我们更新 target 并在 'current' 节点子树里继续找 */
    if (target > kvs->dict_size_index[current]) {
        target -= kvs->dict_size_index[current];
        result = current;
    }
}
return result;
```

这是一个标准的 Fenwick 树"二分查找"技巧。从最高位开始,逐位尝试把 `result` 的某一位置 1;如果"目标值还能容纳下 `tree[result + i]` 这个区间和",就跳过这个区间(从 target 里扣掉它,把 result 推进到这个区间之后)。log₂n 次比较就能定位。n=16384 时只有 14 次比较,极快。

注释([kvstore.c:554-560](../../redis-8.0.2/src/kvstore.c#L554))还专门解释了一个微妙的 ±1:`result += 1`(BIT 1-based)、`result -= 1`(slot 0-based),两者抵消,直接返回 result 即可。这是 Fenwick 树实现里很容易写错的一个点——Redis 源码把它讲得清清楚楚。

把这个反查能力接到 `RANDOMKEY` 上,就是 `kvstoreGetFairRandomDictIndex`([kvstore.c:470](../../redis-8.0.2/src/kvstore.c#L470)):

```c
/* kvstore.c:470-473 */
int kvstoreGetFairRandomDictIndex(kvstore *kvs) {
    unsigned long target = kvstoreSize(kvs) ? (randomULong() % kvstoreSize(kvs)) + 1 : 0;
    return kvstoreFindDictIndexByKeyIndex(kvs, target);
}
```

逻辑分三步:① 在 `[1, kvstoreSize()]` 均匀抽一个全局序号 `target`(注意 +1,因为 BIT 是 1-based);② 用 Fenwick 反查它落在哪个 slot(O(log n));③ 在那个 slot 的 dict 里调 `dictGetRandomKey` 抽一个具体 key。这样 `RANDOMKEY` 和 LRU 淘汰抽样都能做到**全局按 key 数加权均匀**——slot A 键数是 slot B 的 100 倍,抽中 A 的概率就是 B 的 100 倍。

### 11.3.4 反面对比:为什么不用 bitmap + 线性扫,也不用前缀和数组

很多人第一次看 kvstore 会问:这套 BIT 看着挺绕,为啥不用更直白的办法?把三种方案放一起比:

| 方案 | 单点增删 | 查"第 k 个" | 内存 | 评价 |
|------|---------|------------|------|------|
| bitmap(只记 slot 是否空)+ 线性扫 | O(1) | O(n)(要扫到第 k 个非空) | n bit | 增删快,但抽样慢死;且 bitmap 不记键数只记"空/非空",做不到按 key 数加权 |
| 前缀和数组 `prefix[i]` | **O(n)**(后缀全改) | O(log n)(二分) | n 个 long | 查询快,但每次写入都要 O(n) 重建后缀,写入路径承受不了 |
| **Fenwick 树** | **O(log n)** | **O(log n)** | n+1 个 long | 增删查都是 O(log n),常数极小,两全 |

> **不这样会怎样(为什么不能用前缀和数组)**:前缀和数组的致命伤在更新。slot 5 增一个 key,`prefix[5], prefix[6], ..., prefix[16384]` 全部 +1——后缀长度 16379,每次写入都做这么多工作,Redis 写入吞吐直接崩。Fenwick 树的 O(log n) 增删把这个成本压到 14 次跳转,差别是 1000 倍以上。

> **钉死这件事**:Fenwick 树是"动态有序集合找第 k 个"问题的教科书答案。它用 lowbit 这个位运算把一个数组切成"层层嵌套的区间管辖区",查询走下降、更新走上升,对称且都 O(log n)。Redis 用它把 16384 个 dict 的全局统计和反查都压到 14 次操作以内,这是 RANDOMKEY 和 LRU 抽样在 cluster 模式下还能保持全局均匀的物理前提。源码注释里那句维基百科链接,是 Redis 罕见地"指路到外部资料"——作者也觉得这个数据结构值得你点开看一眼。

### 11.3.5 增删链路:每次写入/删除都顺手维护 BIT

最后看 BIT 是怎么挂在增删路径上的。`kvstoreDictAddRaw`([kvstore.c:860](../../redis-8.0.2/src/kvstore.c#L860))插入成功后调 `cumulativeKeyCountAdd(kvs, didx, 1)`:

```c
/* kvstore.c:860-866 */
dictEntry *kvstoreDictAddRaw(kvstore *kvs, int didx, void *key, dictEntry **existing) {
    dict *d = createDictIfNeeded(kvs, didx);
    dictEntry *ret = dictAddRaw(d, key, existing);
    if (ret)
        cumulativeKeyCountAdd(kvs, didx, 1);   /* BIT 上升一路 +1 */
    return ret;
}
```

对称地,`kvstoreDictDelete`([kvstore.c:892](../../redis-8.0.2/src/kvstore.c#L892))删除成功后调 `cumulativeKeyCountAdd(kvs, didx, -1)`,以及 `kvstoreDictTwoPhaseUnlinkFree`([kvstore.c:885](../../redis-8.0.2/src/kvstore.c#L885))也调。注意 `cumulativeKeyCountAdd` 里还有一个**关键副作用**——它顺便维护了 `non_empty_dicts` 计数([kvstore.c:144-145](../../redis-8.0.2/src/kvstore.c#L144)):首次插入(`dsize==1 && delta>0`)时 +1,删空(`dsize==0`)时 -1。这个计数让 `kvstoreNumNonEmptyDicts`([kvstore.c:579](../../redis-8.0.2/src/kvstore.c#L579))O(1) 返回,服务于"slot 使用率"统计(`CLUSTER INFO` 里能看到)。

## 11.4 技巧精解②:SCAN 游标——低位 slot,高位桶,两层正交

### 11.4.1 这块要解决什么:多 dict 上的无状态游走

SCAN 命令要在整个键空间上做"无状态、尽量不重不漏"的游走。第五章讲过,单 dict 上 SCAN 的灵魂是 **reverse-bit 游标**(`dictScan` 里 `v = rev(v)`,见 [dict.c:1470](../../redis-8.0.2/src/dict.c#L1470)):游标按"二进制位反转后的递增"顺序扫桶,这样即便扫描期间 dict 在 rehash(表大小翻倍),桶之间的映射关系仍能正确对齐,不漏 key。

现在多 dict 了,SCAN 还要回答一个新问题:**游标走到一个 slot 的末尾了,怎么切到下一个 slot?** 而且整个 SCAN 是无状态的(只靠一个 64 位游标),不能在游标之外记"我现在在第几个 slot"。

kvstore 的解法极其优雅:**把一个 64 位游标劈成两段——低 `num_dicts_bits` 位装 slot 号,其余高位装 dict 内部的桶游标**。两段各自独立、组合起来又能完整表达"现在在哪个 slot 的哪个桶"。

### 11.4.2 游标的位拼接:addDictIndexToCursor

先看游标的"组装"。`addDictIndexToCursor`([kvstore.c:117](../../redis-8.0.2/src/kvstore.c#L117))把 slot 号塞进游标低位:

```c
/* kvstore.c:117-124 */
static void addDictIndexToCursor(kvstore *kvs, int didx, unsigned long long *cursor) {
    if (kvs->num_dicts == 1)
        return;                                  /* 单 dict 不需要 slot 位 */
    /* didx can be -1 when iteration is over and there are no more dicts to visit. */
    if (didx < 0)
        return;
    *cursor = (*cursor << kvs->num_dicts_bits) | didx;   /* 高位桶游标,低位 slot */
}
```

最后一行是灵魂:`(*cursor << num_dicts_bits) | didx`。**把当前 dict 内部的桶游标(`*cursor`,来自 `dictScan` 的返回值)左移 `num_dicts_bits` 位腾出低位,再把 slot 号 `didx` 或进低位。** 拼出的 64 位数就是新游标。

反向操作是 `getAndClearDictIndexFromCursor`([kvstore.c:126](../../redis-8.0.2/src/kvstore.c#L126)):

```c
/* kvstore.c:126-132 */
static int getAndClearDictIndexFromCursor(kvstore *kvs, unsigned long long *cursor) {
    if (kvs->num_dicts == 1)
        return 0;
    int didx = (int) (*cursor & (kvs->num_dicts-1));    /* 抠出低 num_dicts_bits 位 */
    *cursor = *cursor >> kvs->num_dicts_bits;           /* 游标右移,只剩桶游标 */
    return didx;
}
```

抠出低位的 slot 号(`& (num_dicts-1)` 等价于 `& ((1<<num_dicts_bits)-1)`,因为 `num_dicts` 是 2 的幂),然后把游标右移,留出来的高位就是 dict 内部的桶游标,可以直接喂给 `dictScan`。

这就是"低位 slot,高位桶"的全部魔法。把它画出来:

```text
SCAN 游标的 64 位布局 (cluster 模式,num_dicts_bits = 14)

  ┌──────────────── 64 bit cursor ────────────────┐
  │  高 50 位:dict 内部桶游标(reverse-bit)       │ 低 14 位:slot 号 (didx) │
  │  0000...0000 10110...1                        │ 00...10110100101         │
  │       ↑                                       ↑                          │
  │       │                                       │                          │
  │       │ *cursor << 14 时左移腾出低位           │ didx |= 进来            │
  │       │ (addDictIndexToCursor@123)            │ (addDictIndexToCursor)   │
  │       │                                       │                          │
  │       │ ← *cursor >> 14(右移还原桶游标) ← ───┘                          │
  │       │   (getAndClearDictIndexFromCursor@130)│                          │
  │       │                  ↑ ↑ ↑ ↑ ─── 抠出来给 dictScan 当 v              │
  │       │                  └────────────  & (num_dicts-1) 抠出 didx         │
  └────────────────────────────────────────────────┘

  num_dicts_bits <= 16 的硬约束 ←── kvstoreCreate assert @253
  (桶游标至少留 48 位,够 2^48 个桶)
```

`num_dicts_bits <= 16` 是一条硬约束。`kvstoreCreate` 一上来就 `assert(num_dicts_bits <= 16)`([kvstore.c:253](../../redis-8.0.2/src/kvstore.c#L253)),注释([kvstore.c:251-252](../../redis-8.0.2/src/kvstore.c#L251))直说原因:"We can't support more than 2^16 dicts because we want to save 48 bits for the dict cursor, see kvstoreScan"。cluster 用 14 位,留 50 位给桶游标——足够 2^50 个桶(远超任何实际需求)。

### 11.4.3 kvstoreScan:三段式扫描

把上面两段魔法接起来,就是 SCAN 的主体 `kvstoreScan`([kvstore.c:400](../../redis-8.0.2/src/kvstore.c#L400))。源码注释([kvstore.c:387-399](../../redis-8.0.2/src/kvstore.c#L387))明说这是一个"three pronged approach"(三段式):

```c
/* kvstore.c:400-442,精简 */
unsigned long long kvstoreScan(kvstore *kvs, unsigned long long cursor,
                               int onlydidx, dictScanFunction *scan_cb,
                               kvstoreScanShouldSkipDict *skip_cb,
                               void *privdata)
{
    unsigned long long _cursor = 0;
    /* kvstore.c:406-409 原注释:
     * During dictionary traversal, 48 upper bits in the cursor are used for positioning in the HT.
     * Following lower bits are used for the dict index number, ranging from 0 to 2^num_dicts_bits-1.
     * Dict index is always 0 at the start of iteration and can be incremented only if there are
     * multiple dicts.
     */
    int didx = getAndClearDictIndexFromCursor(kvs, &cursor);   /* ① 从游标抠 slot */
    /* ... onlydidx 快进逻辑(见 11.4.5) ... */

    dict *d = kvstoreGetDict(kvs, didx);
    int skip = !d || (skip_cb && skip_cb(d));
    if (!skip) {
        _cursor = dictScan(d, cursor, scan_cb, privdata);      /* ② 扫该 slot 一批桶 */
        freeDictIfNeeded(kvs, didx);
    }
    if (_cursor == 0 || skip) {                                 /* ③ 该 slot 扫完 */
        if (onlydidx >= 0) return 0;
        didx = kvstoreGetNextNonEmptyDictIndex(kvs, didx);     /*    Fenwick 找下一非空 slot */
    }
    if (didx == -1) return 0;                                   /*    全扫完 */
    addDictIndexToCursor(kvs, didx, &_cursor);                 /*    新 slot 塞回游标 */
    return _cursor;
}
```

三段:① `getAndClearDictIndexFromCursor` 从游标抠出当前 `didx`,游标剩余部分就是 dict 内部桶游标;② 把桶游标喂给 `dictScan` 扫一批桶,拿到下一轮的桶游标 `_cursor`;③ 若该 slot 扫完(`_cursor == 0`),调 `kvstoreGetNextNonEmptyDictIndex` 用 Fenwick 找下一个非空 slot,把新 `didx` 重新塞进游标返回。

注意第 ② 步调的是第五章的 `dictScan`——**kvstore 完全没有重写 dict 的扫描逻辑**。dict 内部的 reverse-bit 游标机制继续在桶层发挥作用,kvstore 只是在外面套了一层 slot 调度。这是分层抽象的胜利:`dictScan` 管"一个 dict 内部桶之间怎么游走不重不漏",kvstore 管"slot 之间怎么切换不漏",两层各管各的、组合起来正确。

### 11.4.4 找下一个非空 slot:kvstoreGetNextNonEmptyDictIndex

第 ③ 步那个"找下一个非空 slot"也用了 Fenwick。`kvstoreGetNextNonEmptyDictIndex`([kvstore.c:570](../../redis-8.0.2/src/kvstore.c#L570)):

```c
/* kvstore.c:570-577 */
int kvstoreGetNextNonEmptyDictIndex(kvstore *kvs, int didx) {
    if (kvs->num_dicts == 1) {
        assert(didx == 0);
        return -1;                              /* 单 dict:扫完即 -1 */
    }
    unsigned long long next_key = cumulativeKeyCountRead(kvs, didx) + 1;
    return next_key <= kvstoreSize(kvs) ? kvstoreFindDictIndexByKeyIndex(kvs, next_key) : -1;
}
```

巧妙之处:`cumulativeKeyCountRead(kvs, didx)` 给出"slot 0..didx 的累计键数",`+1` 就是"严格在 didx 之后的第一个全局 key 序号"。用 Fenwick 反查这个序号落在哪个 slot,就是下一个非空 slot。一次 O(log n) 查询,跳过中间所有空 slot——如果有连续 1000 个空 slot,`kvstoreGetNextNonEmptyDictIndex` 不会一个一个扫,直接跳到下一个有 key 的 slot。这是 Fenwick 相对"bitmap + 线性扫"的又一个大优势。

### 11.4.5 onlydidx:把 SCAN 钉死在单 slot

SCAN 还有一个 cluster 专属优化:`onlydidx` 参数。如果客户端的 pattern 的 hash slot 可算(`{tag}key` 这种带 hashtag 的 key,slot 固定),SCAN 就把扫描**钉死在单 slot**,不必在全库 16384 个 dict 上游走。看 [db.c:1333](../../redis-8.0.2/src/db.c#L1333):

```c
/* db.c:1333-1341,精简 */
int onlydidx = -1;
if (o == NULL && use_pattern && server.cluster_enabled) {
    onlydidx = patternHashSlot(pat, patlen);   /* pattern 限定到单 slot */
    /* ... onlydidx 不可算时回退到 -1 ... */
}
cursor = kvstoreScan(c->db->keys, cursor, onlydidx, scanCallback, NULL, &data);
```

`kvstoreScan` 里 `onlydidx >= 0` 时([kvstore.c:411-421](../../redis-8.0.2/src/kvstore.c#L411)):如果游标的 `didx < onlydidx`,快进到 `onlydidx`;如果 `didx > onlydidx`,直接返回 0 结束。整个 SCAN 只在一个 slot 的 dict 上扫,效率等同单机 SCAN。这是 cluster 下"按 slot 扫描"语义的原生支持。

> **钉死这件事**:SCAN 游标"低位 slot,高位桶"的位拼接,是一次干净的分层抽象——外层 kvstore 用 slot 段调度"走哪个 dict",内层 dictScan 用桶段(reverse-bit)调度"走 dict 内哪个桶",两层完全正交,组合起来正确。kvstore 没有重写 dictScan 一行代码,只是在游标的低位塞了个 slot 号、在 slot 切换时调一次 Fenwick 找下一站。这是"在已有抽象上叠一层"的教科书做法。

## 11.5 技巧精解③:轮转 rehash——把"一次大手术"切成"很多次小缝合"

### 11.5.1 两层渐进:每个子 dict 内部渐进 + 全局轮流渐进

第五章讲过单 dict 的渐进式 rehash:每写一次顺手搬一个桶(`_dictRehashStepIfNeeded`),另外 serverCron 里用 `dictRehashMicroseconds` 限时搬一批。kvstore 下,这变成**两层渐进**:

- **内层**:每个子 dict 自己仍然按第五章那套渐进 rehash(写操作顺手搬一桶 + 限时批量搬)。
- **外层**:kvstore 用一个 `rehashing` 链表挂起所有正在 rehash 的子 dict,serverCron 给一个时间预算 `threshold_us`,kvstore 在预算内**从链表队首逐个喂**——每个 dict 喂一会,搬完一个摘一个,时间用完就走人。

外层是这个章节的新东西。先看子 dict 怎么登记。rehash 启动时,dict 通过回调 `kvstoreDictRehashingStarted`([kvstore.c:200](../../redis-8.0.2/src/kvstore.c#L200))把自己挂进 `kvs->rehashing` 链表尾部:

```c
/* kvstore.c:200-211 */
static void kvstoreDictRehashingStarted(dict *d) {
    kvstore *kvs = d->type->userdata;
    kvstoreDictMetaBase *metadata = (kvstoreDictMetaBase *)dictMetadata(d);
    listAddNodeTail(kvs->rehashing, d);
    metadata->rehashing_node = listLast(kvs->rehashing);   /* 记下自己的节点,便于 O(1) 摘 */
    /* ... 顺便维护 bucket_count / overhead 统计 ... */
}
```

完成后 `kvstoreDictRehashingCompleted`([kvstore.c:217](../../redis-8.0.2/src/kvstore.c#L217))摘掉。注意一个精巧的细节:`metadata->rehashing_node` 把子 dict 在链表里的节点指针**缓存在自己的 metadata 里**,这样摘链时 `listDelNode(kvs->rehashing, metadata->rehashing_node)` 是 O(1) 的——不必扫整条链表找自己。这是用空间换时间的常见手法(每个 dict 多 8 字节存一个 `listNode*`),在 rehashing 链表可能很长(cluster 下几千个 slot 同时 rehash 是常态)时收益巨大。

### 11.5.2 时间闸 + 队首轮转:kvstoreIncrementallyRehash

真正的轮转发生在 `kvstoreIncrementallyRehash`([kvstore.c:681](../../redis-8.0.2/src/kvstore.c#L681)):

```c
/* kvstore.c:681-700 */
uint64_t kvstoreIncrementallyRehash(kvstore *kvs, uint64_t threshold_us) {
    if (listLength(kvs->rehashing) == 0)
        return 0;

    /* Our goal is to rehash as many dictionaries as we can before reaching threshold_us,
     * after each dictionary completes rehashing, it removes itself from the list. */
    listNode *node;
    monotime timer;
    uint64_t elapsed_us = 0;
    elapsedStart(&timer);
    while ((node = listFirst(kvs->rehashing))) {                  /* 队首取 */
        dictRehashMicroseconds(listNodeValue(node), threshold_us - elapsed_us);
        elapsed_us = elapsedUs(timer);
        if (elapsed_us >= threshold_us) {
            break;                                                /* 时间到就走人 */
        }
    }
    return elapsed_us;
}
```

这是 8.0 渐进 rehash 的精髓。**serverCron 每次给一个 `threshold_us` 的时间预算,kvstore 在预算内从 rehashing 链表队首逐个喂 dict——每个 dict `dictRehashMicroseconds` 喂一会,搬完一个摘一个(`kvstoreDictRehashingCompleted` 自动摘),时间用完就 break,绝不恋战。**

注意传给 `dictRehashMicroseconds` 的时间预算是 `threshold_us - elapsed_us`——**剩余时间**。这意味着如果第一个 dict 用光了预算,第二个 dict 这次就不会被喂;下次 serverCron 再来,继续从队首(此时可能是没搬完的第一个 dict,也可能是新加入的)接着干。注释里那句"after each dictionary completes rehashing, it removes itself from the list"说得很清楚:dict 自己搬完自己摘链表,管理零负担。

调用方在 `serverCron` 里([server.c:1185-1197](../../redis-8.0.2/src/server.c#L1185)):

```c
/* server.c:1185-1197,精简 */
if (server.activerehashing) {
    uint64_t elapsed_us = 0;
    for (j = 0; j < dbs_per_call; j++) {
        redisDb *db = &server.db[rehash_db % server.dbnum];
        elapsed_us += kvstoreIncrementallyRehash(db->keys, INCREMENTAL_REHASHING_THRESHOLD_US - elapsed_us);
        if (elapsed_us >= INCREMENTAL_REHASHING_THRESHOLD_US) break;
        elapsed_us += kvstoreIncrementallyRehash(db->expires, INCREMENTAL_REHASHING_THRESHOLD_US - elapsed_us);
        if (elapsed_us >= INCREMENTAL_REHASHING_THRESHOLD_US) break;
        rehash_db++;
    }
}
```

`INCREMENTAL_REHASHING_THRESHOLD_US = 1000`([server.h:127](../../redis-8.0.2/src/server.h#L127)),即 1ms。每次 serverCron 里 rehash 的总预算是 1ms,跨多个 db 分摊,每个 db 内 keys/expires 又分摊。**1ms 是铁闸**——无论有多少个 slot 在 rehash、每个 dict 多大,主线程在 rehash 上单轮耗时不会超过 1ms。

### 11.5.3 resize_cursor:环形公平游标

公平性不止 rehash。`kvstoreTryResizeDicts`([kvstore.c:660](../../redis-8.0.2/src/kvstore.c#L660))用 `resize_cursor` 做扩缩容的轮转:

```c
/* kvstore.c:660-672 */
void kvstoreTryResizeDicts(kvstore *kvs, int limit) {
    if (limit > kvs->num_dicts)
        limit = kvs->num_dicts;

    for (int i = 0; i < limit; i++) {
        int didx = kvs->resize_cursor;
        dict *d = kvstoreGetDict(kvs, didx);
        if (d && dictShrinkIfNeeded(d) == DICT_ERR) {    /* 先尝试缩,不行再扩 */
            dictExpandIfNeeded(d);
        }
        kvs->resize_cursor = (didx + 1) % kvs->num_dicts;   /* 环形推进 */
    }
}
```

每次 serverCron 调用([server.c:1180-1181](../../redis-8.0.2/src/server.c#L1180)),limit 是 `CRON_DICTS_PER_DB = 16`([server.h:104](../../redis-8.0.2/src/server.h#L104))——每轮只检查 16 个 slot。`resize_cursor` 在 16384 个 slot 上环形推进,`(didx + 1) % num_dicts` 循环往复。每个 slot 平均每 1024 轮 serverCron(约 100 秒,按 `server.hz=10`)就被检查一次扩缩容需求——**所有 slot 都有同等机会被检查,不会一直盯着 slot 0 而让 slot 15000 的 dict 撑到爆**。这是把"公平"做进了数据结构本身。

> **不这样会怎样**:如果没有 `resize_cursor` 的环形轮转,而是每次都从 slot 0 线性扫,那 slot 0 永远第一个被检查、第一个被扩缩,slot 16383 永远最后——负载严重不均。环形游标让每个 slot 的"被检查时刻"在时间轴上均匀分布,这是多 dict 场景下"公平"二字的物理实现。

### 11.5.4 与第五章 dict 渐进 rehash 的对比

把第五章和这一章的渐进 rehash 放一起比,能看清"两层渐进"的层次:

| 维度 | 第五章 dict(单表渐进) | 第十一章 kvstore(多表轮流渐进) |
|------|----------------------|------------------------------|
| 渐进单位 | 一个桶(`rehashidx` 指向的桶) | 一个子 dict 的若干桶 × 轮转的多个子 dict |
| 触发 | 每次写操作顺手搬一桶 + serverCron 限时搬 | 每次写操作子 dict 自己搬一桶 + serverCron 里 kvstore 轮流喂各子 dict |
| 公平性 | 单表无公平性问题(就一个表) | **rehashing 链表队首轮转 + resize_cursor 环形游标,保证每子 dict 都被伺候** |
| 时间闸 | `dictRehashMicroseconds` 1ms | `kvstoreIncrementallyRehash` 1ms(继承自 `INCREMENTAL_REHASHING_THRESHOLD_US`) |
| 饥饿风险 | 无 | 有(多 dict 时),靠轮转 + 时间闸消除 |

关键洞察:**第五章的渐进是"桶级",第十一章的渐进是"dict 级 × 桶级"两层**。每个子 dict 内部仍然按第五章那套桶级渐进(写操作顺手搬一桶),kvstore 在外面又加了一层 dict 级的轮流(时间预算内从链表队首逐个喂)。一个有 5000 个 slot 在 rehash 的 db,这 5000 个 dict 会在多次 serverCron 里轮流被喂,每个都拿到自己的那一份时间,没有一个会饿死。

> **钉死这件事**:kvstore 的渐进 rehash 是"两层"——内层每个子 dict 自己按第五章那套桶级渐进,外层 kvstore 用 rehashing 链表队首轮转 + `threshold_us` 时间闸 + `resize_cursor` 环形游标,保证多 dict 公平、主线程不被卡。`INCREMENTAL_REHASHING_THRESHOLD_US = 1000`(1ms)是铁闸,无论多少 slot 在 rehash,单轮主线程在 rehash 上耗时不超过 1ms。这是取向①"把耗时从主线程解放"在 rehash 路径上的具体落地。

## 11.6 SCAN 游标正确性的微妙之处

多 dict 上的 SCAN 要保证一个性质:**不能漏、尽量不重**。漏 key 在 SCAN 语义里是不可接受的(虽然规范上允许,但实际行为要尽量稳)。kvstore 在这件事上有几个微妙的处理,值得单独拎出来讲。

**① 空 dict 在迭代中不立即释放。** `freeDictIfNeeded`([kvstore.c:179](../../redis-8.0.2/src/kvstore.c#L179))有个守护条件 `kvstoreDictIsRehashingPaused(kvs, didx)`——如果 dict 处于 rehashing paused 状态(正是 safe iterator / Scan 上下文里的状态),不释放。注释([kvstore.c:172-178](../../redis-8.0.2/src/kvstore.c#L172))明说:"for rehashing dicts, that is, in the case of safe iterators and Scan, we won't delete the dict. We will check whether it needs to be deleted when we're releasing the iterator." 为什么?因为 SCAN 上下文可能正持有这个 dict 的迭代器,如果中途 slot 被删空、dict 被释放,迭代器就指向了悬挂内存——use-after-free。推迟到迭代器释放时再检查,就避开了这个坑。

**② 游标里的 slot 号是"已扫过 slot 的上界"。** `kvstoreGetNextNonEmptyDictIndex` 找的是"严格大于当前 didx 的下一个非空 slot"。所以即便 SCAN 中途有新 slot 长出来(比如别的客户端往 slot X 写了 key),只要 X 大于当前 didx,X 就会被"未来某次"扫到;反之 X 小于当前 didx,就不会被这次 SCAN 扫到(它会被"未来某次从头开始的 SCAN"扫到)。这是 SCAN 弱保证("尽量不重不漏")的具体体现——不保证强一致快照,但保证最终一致。

**③ 与 dictScan reverse-bit 游标的正交性。** 第五章讲过,单 dict 的 `dictScan` 用 reverse-bit 游标(`v = rev(v)` 见 [dict.c:1470](../../redis-8.0.2/src/dict.c#L1470))应对 rehash 中的表扩展——按位反转后递增,即使桶数翻倍,新表桶也能正确映射回旧游标。kvstore 没有动这套机制,它只是在 `dictScan` 返回的桶游标外面又套了一层 slot 调度。**两层正交**:dictScan 保证一个 dict 内部桶之间游走正确,kvstore 保证 slot 之间切换正确,两层各管各的、组合起来正确。这是分层抽象的胜利。

> **钉死这件事**:kvstore 的 SCAN 正确性建立在两层正交的游标机制上——内层 dictScan 的 reverse-bit 游标(dict.c:1470)管"dict 内部桶之间怎么走不重不漏"(第五章已讲),外层 kvstore 的"低位 slot 高位桶"位拼接(kvstore.c:117)管"slot 之间怎么切换不漏"。kvstore 没有重写 dictScan 一行,只是在外面套了一层 slot 调度。这是"在已有抽象上叠一层"的范式,也是为什么 cluster SCAN 能复用单机 SCAN 全部正确性证明的根子。

## 11.7 迁移:只动那一个 dict

回到本章最初的痛——slot 迁移。现在再看 `delKeysInSlot`([cluster_legacy.c:5785](../../redis-8.0.2/src/cluster_legacy.c#L5785)):

```c
/* cluster_legacy.c:5785-5813,精简 */
unsigned int delKeysInSlot(unsigned int hashslot) {
    if (!kvstoreDictSize(server.db->keys, hashslot))   /* 该 slot 空就直接返回 */
        return 0;

    unsigned int j = 0;
    kvstoreDictIterator *kvs_di = kvstoreGetDictSafeIterator(server.db->keys, hashslot);
    dictEntry *de;
    while ((de = kvstoreDictIteratorNext(kvs_di)) != NULL) {
        sds sdskey = dictGetKey(de);
        robj *key = createStringObject(sdskey, sdslen(sdskey));
        dbDelete(&server.db[0], key);                   /* 删本地 */
        propagateDeletion(&server.db[0], key, server.lazyfree_lazy_server_del);
        /* ... keyspace notification ... */
        j++;
        server.dirty++;
    }
    kvstoreReleaseDictIterator(kvs_di);
    return j;
}
```

它直接拿 `hashslot` 当下标,只迭代那一个子 dict。**成本从 O(全库) 降到 O(该 slot 键数)**。slot 迁移、slot 过期扫描、slot 粒度的 key 计数(`CLUSTER COUNTKEYSINSLOT`),统统变成局部操作,这是 kvstore 最直接的收益。

注意一个细节:`kvstoreGetDictSafeIterator`([kvstore.c:731](../../redis-8.0.2/src/kvstore.c#L731))拿到的是 **safe iterator**——它允许在迭代过程中删除元素(这正是 `dbDelete` 在循环里干的事)。"safe"的代价是迭代器会 pauserehash(见 `freeDictIfNeeded` 那个 `kvstoreDictIsRehashingPaused` 守护),防止 rehash 中途改变结构让迭代器走到悬挂位置。这个机制和第五章 dict 的 safe iterator 一脉相承,kvstore 只是把它从单 dict 推广到"按 slot 锁定的单 dict"。

> **钉死这件事**:cluster 下 slot 迁移的成本,从单 dict 时代的 O(全库)(要扫整个大 dict 判断每个 key 属不属于目标 slot)降到 O(slot 键数)(直接拿 slot 当下标迭代那一个子 dict)。这是 kvstore 存在价值的最直接体现——结构上的隔离天然带来操作上的局部性,而局部性是"不阻塞主线程"的物理前提。

## 11.8 内存开销估算:这笔账到底值不值

讲了这么多好处,该算算代价了。cluster 模式下 kvstore 的固定开销:

**① `dicts` 指针数组。** 16384 个 `dict*` 指针,每个 8 字节,合计 **128 KB**。这是始终占着的死重,无论本节点持有多少 slot。

**② Fenwick 树 `dict_size_index`。** 长度 `num_dicts + 1 = 16385` 个 `unsigned long long`,每个 8 字节,合计 **~128 KB**。也是始终占着。

**③ 每个已分配子 dict 的 `dict` 结构。** 每个 dict 结构本体约 96 字节(`struct dict` 含两张 ht_table 指针、两个 used 计数、rehashidx、dictType 等,见第五章 dict.h:107-122)。本节点持有 N 个非空 slot,这份开销是 `N × 96 字节`。典型节点持有 5000~8000 slot,对应 ~480 KB ~ 768 KB。

**④ 每个非空子 dict 的桶数组。** 每个 dict 有自己的 `ht_table[0]`(和 rehash 时的 `ht_table[1]`),桶数随该 slot 键数动态伸缩。这部分是"有效开销"——存 key 必须花的,不算 kvstore 特有的代价。

**⑤ rehashing 链表节点。** 每个 listNode ~24 字节,只在 rehash 中产生,瞬时存在。

**合计固定开销**(无论持有多少 slot 都要花的):**128 KB(dicts 数组)+ 128 KB(Fenwick)= 256 KB**。对一个内存动辄几十 GB 的 Redis 实例,256 KB 是 0.001% 量级,微不足道。

和它换来的一比:

- **slot 迁移**:从 O(全库) 降到 O(slot 键数)。一个 1 亿 key 的节点搬一个 1000 key 的 slot,从扫 1 亿降到扫 1000——**10 万倍提速**。
- **过期扫描精度**:可以钉在单 slot,不必全库扫。
- **淘汰抽样全局均匀**:Fenwick 让 RANDOMKEY/淘汰做到按 key 数加权,14 次操作反查。
- **SCAN 可钉单 slot**:`onlydidx` 让带 hashtag 的 pattern 扫描只扫一个 dict。

> **钉死这件事**:kvstore 在 cluster 模式下的固定开销是 ~256 KB(dicts 数组 128 KB + Fenwick 树 128 KB),换来的收益是 slot 迁移 O(全库)→O(slot 键数)、SCAN 可钉单 slot、过期/淘汰精度到 slot。256 KB 对几十 GB 内存的 Redis 是 0.001% 量级——这笔账极其划算,这是 8.0 走 kvstore 这条路的经济学根据。取向②(内存即数据库)在这里的具体体现是:**愿意花点结构内存,换操作粒度的精细化**。

## 11.9 几个散点:并发迭代器、LUT defrag、生命周期

**① 并发迭代器的 refcount。** kvstore 的迭代器(`kvstoreIterator` / `kvstoreDictIterator`)内部嵌了一个 `dictIterator di`([kvstore.c:60/67](../../redis-8.0.2/src/kvstore.c#L60)),复用第五章 dict 的迭代器机制。safe iterator 会让 dict pauserehash,迭代器释放时 resume。这和第五章的 safe iterator 完全一致,kvstore 只是"按 slot 选 dict"再调 dict 的迭代器。

**② `kvstoreDictLUTDefrag`:dict 结构本身的重排。** 这是一个比较少人注意但很巧妙的设计。`kvstoreDictLUTDefrag`([kvstore.c:821](../../redis-8.0.2/src/kvstore.c#L821))用于 active defrag——它不整理 dict 里的 key/value,而是**重排 dict 结构体本身的内存**(用提供的分配函数重新分配)。注释([kvstore.c:815-820](../../redis-8.0.2/src/kvstore.c#L815))解释:16384 个 dict 结构体长期增删后,在 jemalloc 里会散落在各种碎片里,active defrag 把它们重新紧凑排布。这用了"游标分批"的策略(`return (didx + 1)`,下次接着干),避免一次性重排 16384 个结构卡住主线程。这是下一章 active defrag 的前奏。

**③ rehashing_node 缓存的妙用。** 前面 11.5.1 提过,`kvstoreDictMetaBase`([kvstore.c:71-73](../../redis-8.0.2/src/kvstore.c#L71))缓存了子 dict 在 rehashing 链表里的节点指针 `rehashing_node`。`kvstoreDictLUTDefrag` 重排 dict 后会顺手更新这个指针([kvstore.c:831-834](../../redis-8.0.2/src/kvstore.c#L831))——因为 dict 结构体搬家了,它内部指向的 listNode 没动,但反过来链表节点 `value` 字段指向的 dict 地址变了,要同步更新(`metadata->rehashing_node->value = *d`)。这是"有反向指针缓存"带来的同步维护成本,但换来的是 O(1) 摘链的收益,值得。

**④ `initTempDb` 的统一入口。** 看 [db.c:667](../../redis-8.0.2/src/db.c#L667) 的注释:"Initialize temporary db on replica for use during diskless replication." 这个函数是**副本在 diskless 复制期间切换 db 用的**——它新建一组 kvstore,复制完替换老 db。kvstore 的设计让"整个 db 切换"变成"创建新 kvstore + 填数据 + 释放老 kvstore",整套操作干净利落。这是结构上的隔离带来的运维便利。

## 章末:回扣、五个为什么、往哪钻

### 主线回扣

这一章每一个决策,都在呼应设计取向:

**取向①(把耗时从主线程解放)是本章的灵魂。** slot 迁移从 O(全库) 变 O(slot 键数);rehash 从"一个大 dict 渐进"变"很多小 dict 轮流渐进且时间预算可控"(`INCREMENTAL_REHASHING_THRESHOLD_US = 1ms` 铁闸);SCAN 可钉在单 slot;过期扫描精度到 slot。每一步都在把可能的大块工作化整为零,塞进主线程的零碎时间里。轮转 rehash 的 `threshold_us` 时间闸、`resize_cursor` 的环形轮转、rehashing 链表的队首取,都是这条主轴的具体化身。这是"不能外包给别的线程的活,就切成小块塞进主线程零碎时间"的典范——和第二章 beforesleep 把 20 件事塞进 epoll_wait 等待时间是同一种哲学。

**取向②(内存即数据库)。** kvstore 愿意花 ~256 KB 固定开销(dicts 数组 + Fenwick 树)换操作粒度的精细化。对内存即数据库的 Redis,这点结构内存是"为效率花的必要代价"。

**取向③(编码自适应)。** 这里是"结构自适应":同一个 kvstore,cluster 下长成 16384 个小 dict,单机下退化成 1 个大 dict,由 `num_dicts_bits` 一个参数切换。代码路径里处处是 `num_dicts == 1` 的快路径,不让 cluster 的复杂度拖累单机。

**取向④(简单优先)。** kvstore 没有发明新哈希表。它只是 dict 的容器,真正的哈希算法、rehash 机制、reverse-bit 游标、迭代器全沿用第五章那套,只是在外面加了 slot 调度和 Fenwick 计数。Fenwick 树是唯一新增的数据结构,而它是教科书级的经典(源码注释里都给了维基百科链接),学习成本极低。

**取向⑤(可靠性)。** 迁移的正确性靠结构隔离:`delKeysInSlot` 只动一个 dict,绝不会误删别的 slot 的 key;SCAN 在迁移进行中也能保证不漏(空 dict 推迟释放、游标 slot 号单调递增)。结构上的隔离天然带来语义上的安全。

值得点出的是:kvstore 管的只是"键到 dictEntry 的映射"这一层。dictEntry 里 value 指向的具体对象(string、list、hash……)该是什么编码还是什么编码,**取向③的编码自适应发生在更内层**,两者正交。一个 slot 的 dict 里,key 可能指向一个 listpack 编码的小 hash,也可能指向一个 skiplist 编码的大 zset——这都不是 kvstore 的事。kvstore 只管"key 在哪个 slot 的哪个桶",value 那层交给第六~八章的对象编码机制。

### 五个为什么

**Q1:为什么 Fenwick 树能做到 O(log n) 反查"第 k 个",而前缀和数组不行?**

前缀和数组查询是 O(log n) 二分(也能查第 k 个),但**更新是 O(n)**——slot i 增一个 key,`prefix[i..n]` 全部 +1。Fenwick 树用 lowbit 把数组切成"层层嵌套的区间管辖区":更新时只走"加 lowbit 上升"路径上的 O(log n) 个节点,查询时走"减 lowbit 下降"路径上的 O(log n) 个节点。它牺牲了"任意前缀和都 O(1) 可读"(前缀和数组可以),换来了"增删也是 O(log n)"。对 kvstore 这种写入频繁的场景,这个 trade-off 是对的。

**Q2:SCAN 游标"低位 slot 高位桶",为什么不是"高位 slot 低位桶"?**

两种拼法数学上都对,但 kvstore 选了"低位 slot"。好处是:**slot 切换时,新的 slot 号直接覆盖低位、不影响高位桶游标**。`addDictIndexToCursor` 是 `*cursor = (*cursor << num_dicts_bits) | didx`——把新 slot 塞进空出来的低位,不必清零。反过来如果是"高位 slot",切换 slot 要清掉高位、保留低位,操作更绕。这是个实现细节,但选低位让代码更简洁。本质上两种拼法是等价的(都是无状态的 64 位编码),区别只在代码写起来顺不顺手。

**Q3:16384 个 dict 同时 rehash,rehashing 链表会不会很长、队首轮转会不会饿死后面的 dict?**

不会饿死,但确实会有等待。rehashing 链表可能挂几千个 dict(cluster 下大量 slot 同时扩容是常态)。但每轮 serverCron 给 1ms 预算,从队首逐个喂,搬完一个摘一个;下一轮 serverCron 继续,队首可能是没搬完的老 dict 或新加入的。关键是——**每个 dict 都会随着队首轮转被伺候到**,只是不是每轮都被喂。配合内层"写操作顺手搬一桶"的渐进,实际上每个 dict 都在持续前进。如果担心某个 dict 太大搬不完,`dictRehashMicroseconds` 内部也有自己的 1ms 子预算和 empty_visits 控制,不会卡在一个 dict 上。两层时间闸叠加,保证公平又不卡。

**Q4:为什么 `num_dicts_bits <= 16` 是硬约束?这 16 怎么算出来的?**

因为游标是 64 位,要给 dict 内部的桶游标留足够位数。cluster 用 14 位 slot,留 50 位桶游标——够 2^50 个桶(远超实际)。如果允许 num_dicts_bits = 16,桶游标还有 48 位,也够。但再大就不够了(比如 20 位 slot 只剩 44 位桶,虽然理论够,但 dictScan 的 reverse-bit 游标在桶数极大时性能会下降)。Redis 选 16 作为上限是个保守的工程决定,留足余量。cluster 的 14 位远在安全区。

**Q5:kvstore 这套机制,和 LevelDB 的 MemTable + SST 多层、Tokio 的分离调度有什么本质区别?**

kvstore 是"同一个数据结构(dict)的分片容器",分片依据是外部的 slot(由 key 的 CRC16 决定),目的是让 cluster 的 slot 操作能局部化。LevelDB 的 MemTable + SST 多层是"不同数据结构(memtable 跳表/SST 有序块)按时间分层",目的是把随机写变顺序写、把热数据和冷数据分开。Tokio 的分离调度是"运行时把不同类型的任务(IO/计时/阻塞)分到不同组件"。三者的共同思想都是"分而治之"——把一个大的耦合的东西,按某种正交的维度切开,让每件操作只动它该动的那一片。kvstore 切的是"键空间"(按 slot),LevelDB 切的是"时间/温度"(MemTable→L0→L1…),Tokio 切的是"任务类型"。这是复杂度守恒原理的具体体现:复杂度不消灭,只转移——把"全库扫描"的复杂度转移成"维护 16384 个小 dict + 一棵 Fenwick 树"的复杂度,而后者是可控的、局部的。

### 想继续深入往哪钻

- 想看 Fenwick 树的理论:读 Peter Fenwick 的原始论文 "A new data structure for cumulative frequency tables"(1994),或维基百科 [Fenwick tree](https://en.wikipedia.org/wiki/Fenwick_tree)(源码注释 [kvstore.c:135](../../redis-8.0.2/src/kvstore.c#L135) 直接给了这个链接)。注意它和"线段树"是两种不同的结构——线段树是显式树形,Fenwick 树是隐式数组,后者常数更小。
- 想看 SCAN 在 cluster 下的完整路径:读 [db.c](../../redis-8.0.2/src/db.c) 的 `scanGenericCommand`(约 db.c:1300 附近),以及 `scanCallback` 回调。注意 `onlydidx` 是怎么从 pattern 算 slot 的(`patternHashSlot`)——只有 pattern 的 hash slot 可算(比如 `{tag}user:*` 这种带 hashtag 的)才能钉单 slot。
- 想看 slot 迁移的完整流程:读 [cluster_legacy.c](../../redis-8.0.2/src/cluster_legacy.c) 的 `migrateAsyncCommand` / `clusterSetSlot` 一族函数。`delKeysInSlot` 是迁出方清理本地的最后一步,迁入方靠 `dbAdd` 把 key 装进对应 slot 的 dict。
- 想对比"键空间分片"在其他系统的做法:看本系列《LevelDB 设计与实现深入浅出》——LevelDB 用一个 SkipList(MemTable)+ 多层 SST 组织键空间,分片依据是 key 范围;Redis kvstore 分片依据是 key 的 hash slot。两种分片哲学不同:Redis 要"均匀打散"(cluster 的 slot 是 hash 取模),LevelDB 要"有序"(便于范围扫描和 compaction)。

### 引出下一章

至此我们已经看清 Redis 在内存里组织 key 的整套骨架:每个 db 是一个 kvstore(本章),kvstore 里每个 slot 是一个 dict(第五章),dict 里每个 key 指向一个对象,对象内部按编码自适应选 listpack/intset/skiplist 等结构(第六~八章)。但当我们顺着"键的内存"继续往下挖,会发现另一个不可回避的问题:**这些 dict、dictEntry、sds key 本身的内存,在长期增删后会变得支离破碎**。jemalloc 固然优秀,但它看不到 Redis 的对象语义,碎片依然会产生——尤其是 16384 个 dict 结构体散落在各处时。下一章我们就离开 kvstore 这一层,走进 `zmalloc` 与 active defrag——看 Redis 如何在"绝不阻塞主线程"的前提下,把那些散落的小块内存重新拼紧(包括用 `kvstoreDictLUTDefrag` 把这 16384 个 dict 结构体重新紧凑排布)。碎片治理,正是内存治理的另一半。

---

## 验证物:如何亲手确认本章的设计

> 说明:本书写作环境为 Windows,无法直接运行 redis-server(8.0 依赖 fork/epoll 等 Linux 特性)。以下 (1) gdb 断点脚本 (2) 源码常量锚点 (3) CLUSTER INFO / DEBUG 观察项 均为可复现的精确指引,供读者在 Linux 环境(Ubuntu 22.04 / CentOS 8 等)对 redis-8.0.2 源码 `make no-opt`(Makefile 里 no-opt 目标会去掉 -O2 加 -g)编译后自行验证。**本书不附编造的运行输出**——凡未实跑的,只给脚本与预期观察变量,不写具体数值。

### 1. gdb 断点脚本

编译:`cd redis-8.0.2 && make no-opt`(带 -g)
启动:`gdb ./src/redis-server --enable-debug-command yes`,另一终端 `redis-cli`。

**前提**:必须用 cluster 模式启动(`redis-server --cluster-enabled yes --port 7000 ...`),否则 `num_dicts == 1`,所有 Fenwick / slot 路径都走快路径不触发。建议搭一个 3 节点 cluster 或单节点 cluster(`redis-cli --cluster create 127.0.0.1:7000 --cluster-replicas 0`)。

```gdb
(gdb) break kvstoreCreate             # kvstore.c:250,看 num_dicts_bits / dicts 数组分配
(gdb) break cumulativeKeyCountAdd     # kvstore.c:137,BIT 上升更新
(gdb) break cumulativeKeyCountRead    # kvstore.c:103,BIT 下降查询
(gdb) break kvstoreFindDictIndexByKeyIndex  # kvstore.c:539,反查"第 k 个在哪个 slot"
(gdb) break kvstoreGetFairRandomDictIndex  # kvstore.c:470,RANDOMKEY 入口
(gdb) break addDictIndexToCursor      # kvstore.c:117,SCAN 游标位拼接
(gdb) break kvstoreScan               # kvstore.c:400,SCAN 三段式主体
(gdb) break kvstoreIncrementallyRehash  # kvstore.c:681,轮转 rehash 时间闸
(gdb) break kvstoreTryResizeDicts     # kvstore.c:660,resize_cursor 环形推进
(gdb) break delKeysInSlot             # cluster_legacy.c:5785,slot 迁移清理
(gdb) run --cluster-enabled yes --port 7000 ...

# redis-cli 执行(先往多个 slot 写 key):
# SET {tag1}key1 v1 ; SET {tag1}key2 v2 ; SET {tag2}key3 v3 ; SET {tag3}key4 v4
# RANDOMKEY
# SCAN 0
# CLUSTER COUNTKEYSINSLOT <slot>

# gdb 在 cumulativeKeyCountAdd 停下(每次 SET 都会进):
(gdb) print didx                       # 预期:key 所属的 slot 号(CRC16 取模)
(gdb) print kvs->num_dicts             # 预期:16384(cluster 模式)
(gdb) print kvs->num_dicts_bits        # 预期:14
(gdb) print kvs->key_count             # 预期:随 SET 递增
(gdb) print idx                        # 预期:didx+1(BIT 1-based)
# 单步进 while 循环,观察 lowbit 爬升:
(gdb) print (idx & -idx)               # 预期:idx 的 lowbit,每次循环往上升

# 在 kvstoreFindDictIndexByKeyIndex 停下(RANDOMKEY 触发):
(gdb) print target                     # 预期:1 到 key_count 之间的随机数
(gdb) print kvs->num_dicts_bits        # 预期:14
(gdb) print bit_mask                   # 预期:1 << 14 = 16384
# 单步看 14 次比较的贪心二分过程,result 从 0 逐位构造

# 在 addDictIndexToCursor 停下(SCAN 触发):
(gdb) print didx                       # 预期:当前 slot 号
(gdb) print kvs->num_dicts_bits        # 预期:14
(gdb) print *cursor                    # 预期:dictScan 返回的桶游标(高位)
# 单步执行 *cursor = (*cursor << 14) | didx 后:
(gdb) print *cursor                    # 预期:低 14 位是 didx,高位是桶游标
```

**预期观察**(基于 [kvstore.c:137-160](../../redis-8.0.2/src/kvstore.c#L137) 的 BIT 更新与 [kvstore.c:539-561](../../redis-8.0.2/src/kvstore.c#L539) 的反查逻辑,本书未实跑):cluster 模式下 `num_dicts=16384`、`num_dicts_bits=14`;每次 SET 触发 `cumulativeKeyCountAdd`,idx 从 `didx+1` 开始按 lowbit 上升 14 次左右;RANDOMKEY 触发 `kvstoreFindDictIndexByKeyIndex`,14 次比较定位 slot;SCAN 返回的游标低 14 位是 slot 号。

### 2. 源码常量锚点(带行号,从 redis-8.0.2 源码 Grep/Read 核实)

| 常量/字段 | 位置 | 值/说明 |
|----------|------|---------|
| `KVSTORE_ALLOCATE_DICTS_ON_DEMAND` | kvstore.h:43 | `1<<0`,按需分配子 dict |
| `KVSTORE_FREE_EMPTY_DICTS` | kvstore.h:44 | `1<<1`,空 dict 即时回收 |
| `KVSTORE_ALLOC_META_KEYS_HIST` | kvstore.h:45 | `1<<2`,分配 keysizes 直方图 |
| `struct _kvstore` | kvstore.c:37-53 | dicts/num_dicts/num_dicts_bits/rehashing/resize_cursor/dict_size_index |
| `CLUSTER_SLOT_MASK_BITS` | cluster.h:8 | 14(`2^14=16384` 个 slot) |
| `CLUSTER_SLOTS` | cluster.h:9 | 16384 |
| `assert(num_dicts_bits <= 16)` | kvstore.c:253 | 游标位宽硬约束(留 48 位给桶) |
| `cumulativeKeyCountRead`(BIT 下降) | kvstore.c:103-115 | O(log n) 前缀和查询 |
| `cumulativeKeyCountAdd`(BIT 上升) | kvstore.c:137-160 | O(log n) 单点更新,带维基百科链接注释@134-136 |
| `addDictIndexToCursor`(游标位拼接) | kvstore.c:117-124 | `*cursor = (*cursor << num_dicts_bits) \| didx` |
| `getAndClearDictIndexFromCursor` | kvstore.c:126-132 | 游标反向拆解 |
| `kvstoreScan`(三段式) | kvstore.c:400-442 | "48 upper bits"注释@406-409 |
| `kvstoreFindDictIndexByKeyIndex`(BIT 反查) | kvstore.c:539-562 | 14 次比较贪心二分,源码注释图@522-538 |
| `kvstoreGetNextNonEmptyDictIndex` | kvstore.c:570-577 | Fenwick 跳过空 slot 找下一个非空 |
| `kvstoreTryResizeDicts`(环形游标) | kvstore.c:660-672 | `resize_cursor = (didx+1) % num_dicts`@670 |
| `kvstoreIncrementallyRehash`(时间闸) | kvstore.c:681-700 | `elapsed_us >= threshold_us` break@695 |
| `INCREMENTAL_REHASHING_THRESHOLD_US` | server.h:127 | 1000(1ms,rehash 时间预算铁闸) |
| `CRON_DICTS_PER_DB` | server.h:104 | 16(每轮 serverCron 检查 16 个 slot 的扩缩容) |
| `kvstoreDictRehashing_started/Completed` | kvstore.c:200/217 | 子 dict 自挂/自摘 rehashing 链表 |
| `kvstoreDictMetaBase.rehashing_node` | kvstore.c:71-73 | 缓存链表节点指针,O(1) 摘链 |
| `calculateKeySlot` | db.c:284-286 | cluster 下 CRC16 取模,非 cluster 恒 0 |
| `initTempDb`(cluster/非 cluster 统一建库) | db.c:667-684 | `slot_count_bits = CLUSTER_SLOT_MASK_BITS` |
| `delKeysInSlot`(slot 迁移清理) | cluster_legacy.c:5785-5813 | 只迭代单 slot 的 dict,O(slot 键数) |
| serverCron rehash 调用 | server.c:1185-1197 | `kvstoreIncrementallyRehash(keys/expires, ...)` |
| serverCron resize 调用 | server.c:1180-1181 | `kvstoreTryResizeDicts(keys/expires, CRON_DICTS_PER_DB)` |
| SCAN `onlydidx` 优化 | db.c:1333-1341 | `patternHashSlot` 限定单 slot |

### 3. CLUSTER INFO / DEBUG 观察项(需本地 redis-server cluster 模式)

> 以下操作需在 Linux 本地启动 redis-server cluster 模式后用 redis-cli 执行。本书未实跑,仅列观察方法与预期(基于源码常量推导)。

```text
# 1. 观察集群 slot 分配(证实 16384 个 slot 被切成多份):
127.0.0.1:7000> CLUSTER INFO
# 预期:cluster_state:ok / cluster_slots_assigned:16384 / cluster_slots_ok:16384

# 2. 观察单 slot key 计数(证明 kvstore 让 countkeysinslot 是 O(slot 键数) 局部操作):
127.0.0.1:7000> SET {user:42}name alice
127.0.0.1:7000> SET {user:42}email a@b.c
127.0.0.1:7000> SET {user:42}age 30
127.0.0.1:7000> CLUSTER KEYSLOT {user:42}name       # 预期:某 slot 号(同 tag 同 slot)
127.0.0.1:7000> CLUSTER COUNTKEYSINSLOT <slot>      # 预期:3(只数该 slot 的 dict)

# 3. 观察 RANDOMKEY 的全局均匀性(证明 Fenwick 树按 key 数加权):
#    往 slot A 写 1000 个 key,slot B 写 10 个 key,RANDOMKEY 一万次统计分布
127.0.0.1:7000> DEBUG SLEEP 0   # 占位,实际用脚本循环 RANDOMKEY 并统计 key 所属 slot

# 4. 观察 SCAN 在 cluster 下的游标(证明"低位 slot 高位桶"):
127.0.0.1:7000> SCAN 0 COUNT 1
# 预期:返回的游标低 14 位是 slot 号,高位是桶游标(本本书未实跑,仅基于 kvstore.c:123 推导)

# 5. 观察 rehash 进度(需大 key 量触发扩容):
127.0.0.1:7000> DEBUG GETKEYS-IN-SLOT <slot> <count>  # 取某 slot 的 key
127.0.0.1:7000> INFO keyspace                          # 看 db 的 keys/expires 计数
127.0.0.1:7000> MEMORY STATS                           # 看 overhead_hashtable_lut / rehashing
# 预期:overhead_hashtable_lut 反映所有 dict 桶指针开销;
#      overhead_hashtable_rehashing 反映正在 rehash 的旧表开销(对应 kvstore.c:50/51 字段)

# 6. 观察 dict 结构开销(cluster 模式固定 ~256 KB):
127.0.0.1:7000> MEMORY MALLOC-STATS
# 阴基:16384 个 dict* 指针(128 KB)+ Fenwick 树(~128 KB)在 jemalloc 统计里能看到
```

标注:以上预期基于源码常量([kvstore.c:37](../../redis-8.0.2/src/kvstore.c#L37) 结构、[cluster.h:8](../../redis-8.0.2/src/cluster.h#L8) slot 数、[server.h:127](../../redis-8.0.2/src/server.h#L127) 时间闸)与 [CLUSTER](../../redis-8.0.2/src/cluster.c) / [DEBUG](../../redis-8.0.2/src/debug.c) 命令实现推导,本书未在本地实跑;若你的 redis 版本/配置/集群拓扑不同,具体数值可能偏移,以 `CLUSTER INFO` / `MEMORY STATS` 实际输出为准。`DEBUG GETKEYS-IN-SLOT` 等命令需 `--enable-debug-command yes` 启动才可用。
