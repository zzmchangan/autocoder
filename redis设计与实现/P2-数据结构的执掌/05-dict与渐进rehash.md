# 第五章 · dict 与渐进式 rehash:百万级字典扩容如何不卡主线程

> 篇:P2 数据结构的执掌
> 主轴呼应:这一章是**取向①(把耗时从主线程解放)在数据结构层的头号案例**——百万级字典的扩容,被拆成无数个 O(1) 的小动作,绝不阻塞主线程。同时它也是取向②(内存即数据库)的底座:Redis 的每一对键值、每一个 hash 的 field-value、每一个 set 的成员,都住在 dict 里。这张表快不快、稳不稳,直接等于 Redis 快不快、稳不稳。

---

## 读完本章你会明白

1. **为什么 Redis 的 dict 要同时挂着两张哈希表**——不是为了冗余备份,而是为了让"扩容"这件事能从一次几秒的大手术,改造成无数次 O(1) 的小擦伤,这是渐进式 rehash 的物理前提。
2. **`rehashidx` 这个游标凭什么能把一次百万级的搬运摊成平原**——两条路并行(每操作搬一桶 + serverCron 限时 1ms),复杂度没消失,只是从一次尖峰摊成了无数次微动作。
3. **`SCAN` 命令的游标为什么要做"位反转"(reverse-bit)递增**——这是 dict 章最被低估、最被面试问的算法。普通正向游标在表扩容时会漏 key,位反转递增让"扩容分裂出的新桶"永远落在游标的未扫过侧,可能重但绝不漏。
4. **`dictTwoPhaseUnlinkFind` 为什么要分成 Find 和 Free 两阶段**——和第二章 ae 的 `AE_DELETED_EVENT_ID` 同构:rehash 会让"前驱指针"失效,查找和摘链之间若插一个 keyspace event 回调,一步删就是 use-after-free。
5. **指针低 3 位编码和 no_value 字典是怎么把每一字节榨干的**——8 字节对齐的指针低 3 位本是 0,Redis 把"这是 key 本身还是 dictEntry"塞进去,让 SET 这种只关心存在性的字典省掉一整次 `dictEntry` 分配。

---

> **如果一读觉得太难:先只记住三件事**——
> ① dict = 两张桶数组(`ht_table[0]` 老表 / `ht_table[1]` 新表)+ 一个 `rehashidx` 进度游标([dict.h:107-122](../../redis-8.0.2/src/dict.h#L107))。没在 rehash 时 `rehashidx == -1`,只有一张表生效;
> ② 扩容时**只分配新表、设 `rehashidx = 0` 就返回**([dict.c:264](../../redis-8.0.2/src/dict.c#L264)),不搬任何元素;之后每次增删改查顺手搬一桶,serverCron 再限时 1ms 兜底搬一批,百万级扩容就这样被摊平;
> ③ rehash 期间读要**两张表都查**,写新元素**一律进新表**——这是渐进式 rehash 不丢不乱的全部规矩。
> 这三件事,就是 dict 的全部。

---

> **一句话点破:dict 用"两张表 + 一个游标"把扩容从"一次大阻塞"摊成"无数次 O(1) 微动作";复杂度一点没少,只是从一次几秒的尖峰,摊成了整段扩容期间每条命令多花几微秒的平原——这是取向①在数据结构层的最美落地。**

第三章我们看到 `db` 里每个键值对住在 dict 里;第六、七章会看到 `HASH` 的 field-value、`SET` 的成员也都住在 dict 里。可以说 **dict 是 Redis 内存世界的地基**。地基不稳,整栋楼塌。所以这张表有四条硬指标必须同时满足:

1. **O(1) 平均查找**——百万 key 也要常数时间命中;
2. **抗碰撞攻击**——用户塞进来的 key 不可信,不能让人用精心构造的 key 把表打成一条长链;
3. **内存自适应**——元素多就扩、少就缩,内存就是数据库,浪费一桶就是浪费真金白银;
4. **扩缩容不能阻塞主线程**——这是最要命的一条。Redis 主线程单线程串行执行命令,一次扩容要是像 Java `HashMap` 那样把整张表原地重排,百万级 dict 会卡住主线程好几秒,直接违反 SLA。

前三条是任何合格哈希表都要解决的常规问题,第四条是 Redis 的"独门铁律"。本章的灵魂,就是 Redis 工程师如何在不动这条铁律的前提下,把扩容这件事从"一次大手术"改造成"无数次小擦伤"——这就是**渐进式 rehash**。

## 5.1 dict 的结构:两张表、一个进度条

先看 Redis 8.0 的 `dict` 定义。相比 7.x,8.0 做了一次精简——把原来嵌套的 `struct dictht` 子结构拆掉了,字段直接铺平进 `dict`,并用指数代替了绝对大小:

```c
/* dict.h:107-122 */
struct dict {
    dictType *type;
    dictEntry **ht_table[2];        /* 两张桶数组,索引 0 是老表,1 是新表 */
    unsigned long ht_used[2];       /* 每张表里实际装了多少个 entry */
    long rehashidx;                 /* 渐进 rehash 的进度游标,-1 表示没在搬 */
    unsigned pauserehash : 15;      /* >0 时暂停 rehash(迭代器/两阶段删除等) */
    unsigned useStoredKeyApi : 1;
    signed char ht_size_exp[2];     /* 桶大小 = 1<<exp,用指数存,省内存 */
    int16_t pauseAutoResize;        /* >0 时禁止自动扩缩容 */
    void *metadata[];
};
```

几个关键点,对照源码逐条说清:

- **桶数组是 `dictEntry **`,桶里挂链表**。这是教科书式的**拉链法(separate chaining)**解决冲突。`dictEntry` 的 `next` 指针把同一个桶里的元素串成单链表。

> **提出问题**:为什么要留**两张**表?一张不够吗?
> **不这样会怎样**:如果只有一张表,扩容就必须"原地重排"——先分配新桶数组,把老表所有元素一个个算新位置塞进去,然后丢弃老表。这是一次**原子的大动作**:要么全搬完,要么一条命令都响应不了。百万级 dict 这一瞬要卡几秒。
> **所以这样设计**:`ht_table[0]` 是当前生效的老表,`ht_table[1]` 是 rehash 期间才分配的目标表。两张表并存,正是"边走边搬"的物理前提——读的时候两张表都得看一眼,写的时候新元素一律落新表,搬运则按 `rehashidx` 一桶一桶推进。
>
> **钉死这件事**:两张表并存不是冗余,是"让扩容可中断"的硬件基础。没有第二张表,就没有"搬一半也能正确响应"这件事,渐进式 rehash 根本无从谈起。

- **为什么用 `ht_size_exp` 存指数而不是绝对大小?** Redis 把桶大小永远取成 2 的幂,于是桶掩码就是 `size-1`,定位桶只需一次按位与 `hash & mask`。存指数既省字节(`signed char` 够用),又让"算大小"变成一次移位:`DICTHT_SIZE(exp) = 1<<exp`([dict.h:104](../../redis-8.0.2/src/dict.h#L104))。

- **`rehashidx` 是整个机制的心脏**。它是一个游标,语义是:"老表里下标 `< rehashidx` 的桶,已经全部搬完;`>= rehashidx` 的桶,可能还没搬。"当且仅当它等于 -1 时,dict 不在 rehash。初始大小 `DICT_HT_INITIAL_EXP = 2`([dict.h:156](../../redis-8.0.2/src/dict.h#L156)),即第一张表上来就是 4 个桶。

桶大小恒为 2 的幂还有个副产品,在本章后面会反复兑现:**缩容时一个 entry 在新桶里的位置,就是老位置去掉高位**——所以缩容搬桶连哈希都不用重算,直接拿老下标对新掩码按位与即可([dict.c:331-336](../../redis-8.0.2/src/dict.c#L331))。

```text
                dict 结构(rehash 进行中)
   ┌──────────────────────────────────────────────────────┐
   │ type → dictType(hash/keyCmp/keyDest/.../no_value)    │
   │                                                       │
   │ ht_table[0] ──→ 老桶数组(大小 = 1<<exp[0])           │
   │   [0]→NULL  [1]→k3→k7  [2]→NULL  [3]→k1   ←已搬[0]   │
   │   [4]→k2→k9  [5]→NULL  [6]→k4   [7]→NULL              │
   │                  ↑ rehashidx = 4(这桶还没搬)          │
   │ ht_used[0] = 5     ht_size_exp[0] = 3 (8 桶)          │
   │                                                       │
   │ ht_table[1] ──→ 新桶数组(大小 = 1<<exp[1]=16 桶)      │
   │   [0]..[3] 已搬来的 entry, [4]..[15] 大多空           │
   │ ht_used[1] = 3     ht_size_exp[1] = 4                 │
   │                                                       │
   │ rehashidx = 4   pauserehash = 0                       │
   └──────────────────────────────────────────────────────┘
   读:k 在哪?——两张表都查一遍(dictFindByHash @ dict.c:770)
   写:新元素一律进 ht_table[1](dictFindPositionForInsert @ dict.c:1669)
   搬:rehashidx 往后爬,一次一桶(dictRehash @ dict.c:393)
```

## 5.2 哈希算法:SipHash,为了不被打穿

定位一个 key 的第一步是算哈希。Redis 默认走 `dictGenHashFunction`:

```c
/* dict.c:110-112 */
uint64_t dictGenHashFunction(const void *key, size_t len) {
    return siphash(key, len, dict_hash_function_seed);
}
```

这里用的是 **SipHash**(SipHash-2-4 变体),不是 `djb2`、`MurmurHash` 这类**非加密学哈希**。为什么?因为哈希表一旦被恶意用户用**哈希碰撞攻击**打中,会把大量 key 全塞进同一个桶,拉出一条超长链表,把 O(1) 拖成 O(n)。2003 年的 Crosby & Wallach 论文证明,只要算法的哈希函数是确定且公开的,攻击者可以离线构造一批碰撞 key,让一台 Web 服务器 CPU 飙满——这就是著名的 *algorithmic complexity attack*。

SipHash 的对策有两手:

1. **它是带密钥的(keyed)**。`dict_hash_function_seed` 是 16 字节的随机种子,在进程启动时由 `dictSetHashFunctionSeed` 写入([dict.c:96](../../redis-8.0.2/src/dict.c#L96))。攻击者不知道这个种子,就**无法离线预测哪些 key 会碰撞**。
2. **它是密码学意义上的伪随机函数(PRF)**。即便知道了输入,也推不出输出分布,碰撞构造在计算上不可行。

代价是 SipHash 比 MurmurHash 慢一些(每 key 多几纳秒)。但 Redis 选择"安全换速度"——百万级 QPS 下,这点开销远小于一次被攻击打挂的损失。

> **钉死这件事**:SipHash 用"每 key 几纳秒"的固定成本,换来了"对碰撞攻击免疫"这一确定性收益。它不试图让哈希更快,而是让哈希**不可被针对性打慢**——这是 Redis 少数主动选择"不那么快"的地方,因为它换的是安全。

## 5.3 负载因子与扩缩容:什么时候动表

负载因子(load factor)= `ht_used / ht_size`,即"每个桶平均挂几个 entry"。Redis 用它来决定何时扩、何时缩。扩容的判定在 `dictExpandIfNeeded`([dict.c:1542](../../redis-8.0.2/src/dict.c#L1542)):

```c
/* dict.c:1556-1564 —— 满足任一条件就扩 */
if ((dict_can_resize == DICT_RESIZE_ENABLE &&
     d->ht_used[0] >= DICTHT_SIZE(d->ht_size_exp[0])) ||          /* 负载因子 >= 1 */
    (dict_can_resize != DICT_RESIZE_FORBID &&
     d->ht_used[0] >= dict_force_resize_ratio * DICTHT_SIZE(d->ht_size_exp[0])))  /* >= 4(强制) */
{
    if (dictTypeResizeAllowed(d, d->ht_used[0] + 1))
        dictExpand(d, d->ht_used[0] + 1);
    return DICT_OK;
}
```

这里出现了两个阈值,是本章最容易被忽略、却最体现工程权衡的地方:

- **平时(`DICT_RESIZE_ENABLE`)阈值是 1**:桶装满了就扩,保证平均链长不超过 1,查找 O(1) 稳得住。
- **有子进程时(`DICT_RESIZE_AVOID`)阈值是 `dict_force_resize_ratio = 4`**([dict.c:43](../../redis-8.0.2/src/dict.c#L43)):除非已经挤到 4 倍,否则**不扩**。

> **提出问题**:为什么有子进程就要"忍一忍",宁可让负载因子冲到 4?
> **不这样会怎样**:Redis 做 `BGSAVE`/`BGREWRITEAOF` 时 fork 出子进程,父子**共享物理内存页**,靠 **copy-on-write(COW)** 隔离。这时要是疯狂 rehash,每次搬桶都会改写老表和新表的桶指针,把大量原本共享的页触发写时复制,内存翻倍——父进程内存涨爆,fork 的内存优势荡然无存。
> **所以这样设计**:`updateDictResizePolicy` 在有子进程时设 `AVOID`,自己是子进程时设 `FORBID`,否则 `ENABLE`。AVOID 下阈值放宽到 4,FORBID 下干脆禁止。宁可让查找多走几步链表,也要护住 COW。
>
> **钉死这件事**:为了不在 BGSAVE 期间引爆 COW,Redis 主动容忍负载因子短期冲到 4,牺牲一点查找效率,换取内存不爆。这是"内存即数据库"(取向②)和"可靠性靠持久化"(取向⑤)之间的微妙平衡——同一份物理内存,既要给业务用,又要给 RDB 子进程快照用,rehash 这个"写内存大户"必须给它让路。

扩到多大?由 `_dictNextExp`([dict.c:1625](../../redis-8.0.2/src/dict.c#L1625))决定——取 **≥ 入参的第一个 2 的幂**:

```c
/* dict.c:1625-1631 */
static signed char _dictNextExp(unsigned long size)
{
    if (size <= DICT_HT_INITIAL_SIZE) return DICT_HT_INITIAL_EXP;
    if (size >= LONG_MAX) return (8*sizeof(long)-1);

    return 8*sizeof(long) - __builtin_clzl(size-1);   /* GCC 内建函数,1 条指令找到最高位 */
}
```

`__builtin_clzl` 是编译器内建函数,在 x86 上编译成单条 `BSR`/`LZCNT` 指令,一行算出"≥ size 的最小 2 的幂"的指数。传 `used+1` 进来,效果就是"至少装得下当前所有元素加一个"。

缩容判定在 `dictShrinkIfNeeded`([dict.c:1579](../../redis-8.0.2/src/dict.c#L1579)),逻辑对称,阈值是 `HASHTABLE_MIN_FILL = 8`([dict.h:28](../../redis-8.0.2/src/dict.h#L28)),即负载因子低于 1/8 才缩(有子进程时是 1/32)。低于初始大小 `DICT_HT_INITIAL_SIZE` 时永不缩([dict.c:1584](../../redis-8.0.2/src/dict.c#L1584))。

## 5.4 渐进式 rehash:本章的灵魂

扩容一旦决定,真正干活的是 `_dictResize`([dict.c:227](../../redis-8.0.2/src/dict.c#L227))。注意它**只分配新表、设 `rehashidx = 0`,然后就返回了**——它不搬任何一个元素:

```c
/* dict.c:261-264 —— 分配完新表,挂个进度条就走 */
d->ht_size_exp[1] = new_ht_size_exp;
d->ht_used[1] = new_ht_used;
d->ht_table[1]  = new_ht_table;
d->rehashidx = 0;
```

从这一刻起,这张 dict 进入了 rehash 中间态:老表 `ht_table[0]` 还装着全部元素,新表 `ht_table[1]` 空着,`rehashidx` 从 0 开始往后爬。接下来谁负责搬?**两条路并行走**。

**第一条路:增删改查顺手搬一桶。** 每次查找都会调 `_dictRehashStepIfNeeded`([dict.c:1609](../../redis-8.0.2/src/dict.c#L1609)),它判断:如果这次访问命中的桶在老表、且下标 `>= rehashidx`,就**就地搬这一桶**(缓存友好);否则退而求其次,调用 `dictRehash(d, 1)` 搬 `rehashidx` 指向的那一桶([dict.c:1620](../../redis-8.0.2/src/dict.c#L1620))。换句话说,**用户每访问一次表,Redis 就偷偷还一桶债**。插入路径同理,`dictFindPositionForInsert` 在 [dict.c:1645](../../redis-8.0.2/src/dict.c#L1645) 先 `_dictRehashStepIfNeeded` 再做正事。

**第二条路:serverCron 定时搬一批。** 光靠用户访问还不够——万一某张表冷门没人查,岂不是永远搬不完?所以 Redis 还在 `serverCron` 周期任务里加了 `databasesCron`,它走 `kvstoreIncrementallyRehash`,底层调 `dictRehashMicroseconds`([dict.c:434](../../redis-8.0.2/src/dict.c#L434))——**限时 1 毫秒**,搬满 1ms 或搬完就停。这就是兜底。

真正搬桶的引擎是 `dictRehash(d, n)`([dict.c:393](../../redis-8.0.2/src/dict.c#L393))——**每次最多搬 n 个非空桶**:

```c
/* dict.c:393-422,精简后保留骨架 */
int dictRehash(dict *d, int n) {
    int empty_visits = n*10;            /* 最多跳过的空桶数,防止一次跳太久 */
    /* ...AVOID/FORBID 判定,略... */
    while(n-- && d->ht_used[0] != 0) {
        while(d->ht_table[0][d->rehashidx] == NULL) {  /* 跳过空桶 */
            d->rehashidx++;
            if (--empty_visits == 0) return 1;          /* 空桶跳太多就先撤 */
        }
        rehashEntriesInBucketAtIndex(d, d->rehashidx);  /* 把这一桶全搬过去 */
        d->rehashidx++;
    }
    return !dictCheckRehashingCompleted(d);             /* 搬完则收尾 */
}
```

三处细节体现了"绝不阻塞"的偏执:

- **`empty_visits = n*10`**([dict.c:394](../../redis-8.0.2/src/dict.c#L394))。一次只搬 n 桶,但万一连续上千个桶都是空的呢?Redis 限定"最多跳 10n 个空桶",防止在一个稀疏表上空转太久。这就是注释里那句 *the amount of work it does would be unbound*([dict.c:390-391](../../redis-8.0.2/src/dict.c#L390))要堵的窟窿。

> **提出问题**:为什么要给"跳空桶"封顶?反正空桶也不用搬啊。
> **不这样会怎样**:想象一张刚扩容的表,老表 100 万桶,但 99 万都是空的(业务场景:大量 key 被 EXPIRE 掉了)。一次 `dictRehash(d, 1)` 要搬 1 个非空桶,却可能要先跳过 99 万个空桶才找到它——这一跳就是 99 万次 `rehashidx++` 和数组访问,主线程直接卡死。
> **所以这样设计**:`empty_visits = n*10` 给"跳空桶"封顶,搬 1 桶最多跳 10 桶。跳超了就先返回,把剩下的空桶留给下一轮 serverCron 或下一次用户访问。**宁可多花几轮事件循环,也不在一轮里卡住。**
>
> **钉死这件事**:`empty_visits = n*10` 是"渐进"二字不被稀疏表拖垮成"卡顿"的关键护栏。没有它,一张稀疏老表的 rehash 会让主线程一次性空转几十万次。

- **搬一桶不是搬一个元素**。`rehashEntriesInBucketAtIndex`([dict.c:321](../../redis-8.0.2/src/dict.c#L321))把整条链一起搬,链长多少都一次清掉,因为同一桶里的元素反正都要动,顺手清空比拆开更省 `rehashidx` 推进次数。

- **缩容搬桶不重算哈希**。`rehashEntriesInBucketAtIndex` 在缩容分支([dict.c:331-336](../../redis-8.0.2/src/dict.c#L331))直接用 `h = idx & DICTHT_SIZE_MASK(d->ht_size_exp[1])`——拿老桶下标对新掩码按位与,就是新桶下标。这是"桶大小恒为 2 的幂"埋下的伏笔,在这里兑现成一处精妙的常数优化。

收尾由 `dictCheckRehashingCompleted`([dict.c:370](../../redis-8.0.2/src/dict.c#L370))负责:等 `ht_used[0]` 归零,先触发 `rehashingCompleted` 回调(详见 5.7),再释放老表、把新表"提升"为 `ht_table[0]`、`rehashidx = -1`,rehash 正式结束。

## 5.5 rehash 期间的读写:两张表都要照顾

这是渐进式 rehash 最容易写错、也最能体现工程功底的地方。看 `dictFindByHash`([dict.c:770](../../redis-8.0.2/src/dict.c#L770))的查找逻辑:

```c
/* dict.c:787-810,精简 */
for (table = 0; table <= 1; table++) {
    if (table == 0 && (long)idx < d->rehashidx) continue;  /* 这桶已搬,跳老表 */
    idx = hash & DICTHT_SIZE_MASK(d->ht_size_exp[table]);
    he = d->ht_table[table][idx];
    while(he) { /* ...比对 key... */ he = dictGetNext(he); }
    if (!dictIsRehashing(d)) return NULL;                  /* 没 rehash,看一张表就够 */
}
```

两个动作环环相扣:

- **`if (table == 0 && (long)idx < d->rehashidx) continue;`**([dict.c:788](../../redis-8.0.2/src/dict.c#L788))。如果老表上这个桶的下标小于 `rehashidx`,说明它已经被搬走了,直接去新表找,省一次空跑。
- **否则两张表都查**。元素可能还在老表(没轮到搬),也可能已经在新表。所以 `for (table = 0; table <= 1; table++)`。

插入则是另一套规则:**新元素一律进新表**(`ht_table[1]`),见 `dictFindPositionForInsert` 末尾 [dict.c:1669](../../redis-8.0.2/src/dict.c#L1669):`bucket = &d->ht_table[dictIsRehashing(d) ? 1 : 0][idx]`。

> **钉死这件事**:rehash 期间的规矩只有两条——**读两张表都查,写一律进新表**。前者保证不漏(元素可能在新也可能在老),后者保证进度只进不退(不会把新写的 key 又塞回即将要搬的老表)。这两条规矩加上 `rehashidx` 单调递增,就是渐进式 rehash "不丢不乱"的全部保证。

删除、改值同理,都得两张表都看一遍。这些"照顾两张表"的代价,就是渐进式 rehash 在运行期支付的利息:rehash 期间每次操作的平均成本比平时略高一点点(多查一张表),但**每一笔都是 O(1),没有一笔是大阻塞**。

## 5.6 技巧精解①:dictScan 的 reverse-bit 游标——rehash 中遍历如何不漏 key

这一节是整章最被低估、却最被问的算法。`SCAN` 命令(以及 `HSCAN`/`SSCAN`/`ZSCAN`)要在 rehash 进行中遍历整张表,既要**不漏**(漏一个 key 对业务是不可接受的——`SCAN` 常用于清理、迁移、对账),又要尽量**不重**(重复返回 key 业务能容忍,去重即可)。Redis 给出的解法是 dict 章最精妙的一笔:**reverse-bit(位反转)游标**。

先看普通正向游标为什么不行,再看位反转怎么破。

### 5.6.1 问题:rehash 期间遍历,正向游标会漏

`SCAN` 是无状态迭代:客户端拿着一个 `cursor`(就是个整数)进来,服务器返回一批 key 和一个新 `cursor`,客户端用新 cursor 再来,直到 cursor 回到 0。服务器不保存任何迭代状态——这样客户端断了重连、服务器重启都能继续,实现极简。

问题来了:表大小是 2 的幂,一个 key 落在哪个桶,由 `hash & (size-1)` 决定,即只取哈希值的**低位**(低位几位取决于表大小)。当表在迭代过程中扩容(从 size=4 翻倍到 size=8),**老桶会分裂成两个新桶**:老桶编号 `b` 的元素,扩容后会被分到新桶 `b` 和新桶 `b + 老表大小`。

```text
扩容前(size=4, mask=0b011):         扩容后(size=8, mask=0b111):
桶 0 (00): a, e                      桶 0 (000): a     ← e 因 hash 第 3 位=1 分到桶 4
桶 1 (01): b                         桶 1 (001): b
桶 2 (10): c                         桶 2 (010): c
桶 3 (11): d                         桶 3 (011): d
                                     桶 4 (100): e     ← 老桶 0 分裂出的高位桶
                                     桶 5..7: 空
```

现在用**正向游标**(低位递增,即 0→1→2→3→...)遍历老表:假设已经扫了桶 0(拿到 a、e),cursor 推进到 1。这时表扩容到 8 桶。客户端拿着 cursor=1 继续,服务器在新表上从桶 1 扫到桶 7——**漏了桶 4 里的 e!** 因为桶 4 编号(4)落在"已扫过"的区间(0..3)之后被分裂出来,正向游标从 1 往后走,只会扫 1..7,再也回不到"概念上已扫过的"桶 0 的高位分裂桶。

**根因**:正向游标是"按低位递增",而扩容分裂是"在高位加一位"。已扫过的低编号桶分裂出的高编号桶,会落在正向游标的"未来"区间被漏掉;反过来,未扫过的高编号桶分裂出的也偏低,可能落到"已扫过"区间被重——重了可以容忍,漏了不行。

### 5.6.2 招:位反转游标——从高位向低位递增

Redis 的破法在 `dictScanDefrag`([dict.c:1435](../../redis-8.0.2/src/dict.c#L1435))里,核心是这三行([dict.c:1470-1472](../../redis-8.0.2/src/dict.c#L1470) 非 rehash 分支,[dict.c:1514-1516](../../redis-8.0.2/src/dict.c#L1514) rehash 分支):

```c
v = rev(v);    /* 位反转:把 v 的位顺序整个倒过来 */
v++;           /* 反转后的值 +1 */
v = rev(v);    /* 再反转回来 */
```

`rev` 函数([dict.c:1325](../../redis-8.0.2/src/dict.c#L1325))是把一个 `unsigned long` 的所有位整个倒序——第 0 位和第 63 位交换、第 1 位和第 62 位交换……它用的是经典的**对折位交换**算法:

```c
/* dict.c:1325-1333 —— 算法来自 Stanford bithacks: ReverseParallel */
static unsigned long rev(unsigned long v) {
    unsigned long s = CHAR_BIT * sizeof(v); // bit size; must be power of 2
    unsigned long mask = ~0UL;
    while ((s >>= 1) > 0) {
        mask ^= (mask << s);
        v = ((v >> s) & mask) | ((v << s) & ~mask);
    }
    return v;
}
```

每次循环把位宽对折一半,先交换最高/最低半段,再缩到四分之一……O(log W) 次(W 是字宽,64 位机器是 log₂64 = 6 次循环)。注释 `Algorithm from: http://graphics.stanford.edu/~seander/bithacks.html#ReverseParallel` 点明了出处。

`v = rev(v); v++; v = rev(v);` 这三步的**效果**是什么?是把 v 的**位反转后**加 1,再反转回来。等价于"**从最高位向最低位递增**"——也就是把"进位"的方向从低位翻到高位。

举个 4 位游标的例子。普通递增:0000→0001→0010→0011→0100→...→1111(低位先进位)。位反转递增:0000→1000→0100→1100→0010→...→1111(高位先进位)。**遍历桶的顺序完全反过来了**:先扫高位地址桶,后扫低位地址桶。

为什么这样就能不漏?关键在**扩容分裂的方向**。老桶 `b` 扩容后分裂成 `b` 和 `b + 老表大小`——后者是在**更高位**加了一个 1。位反转游标先扫高位,意味着"高位所有组合"会被优先遍历完。当表在迭代中扩容时,已扫过的高位桶分裂出的低位桶,落在游标的"未来"侧(因为游标还没扫到低位);未扫过的低位桶分裂出的高位桶,本来就在游标的已扫侧(会被重),但**绝不会漏**。

用前面的例子验证:老表 size=4,位反转游标顺序是 桶0(00)→桶2(10)→桶1(01)→桶3(11)。假设扫完桶0拿到 a,游标推进到"下一个"=桶2。表扩容到 size=8,e(hash 第3位=1)分到桶4(100)。新表位反转游标从当前值继续:下一组高位组合已经覆盖了 100(=桶4)。**e 不会被漏**——它会被重扫一次(因为 e 在老表桶0已被扫过,新表桶4又扫一次),但重是可以容忍的,漏不行。

> **钉死这件事**:`SCAN` 用位反转游标(`rev(v); v++; v = rev(v)`),本质是"从高位向低位递增"。这样扩容时老桶分裂出的新桶(在高位加一位)永远落在游标的未扫侧——**可能重,绝不漏**。重了业务层去重即可,漏了无法挽回。这是 dict 章里"宁可笨一点也不能错"哲学的又一次落地,和第二章 ae 选 LT 不选 ET 是同一种气质。

### 5.6.3 rehash 进行中:小表先扫,大表扩展位补扫

dictScan 还有一层复杂性:rehash 期间有两张表,大小不同。`dictScanDefrag` 的处理([dict.c:1474-1519](../../redis-8.0.2/src/dict.c#L1474))是:**把小表当作主表,大表当作小表的"扩展"**。先扫小表上 `v & m0` 的桶(rehash 期间老表是小表),再扫大表上所有"扩展位"对应的桶。

扩展位是什么?如果小表掩码 `m0 = 0b011`(size=4),大表掩码 `m1 = 0b111`(size=8),那么差值 `m0 ^ m1 = 0b100` 就是"大表比小表多出的那一位"。对这一位填 0 和 1,就得到大表上两个对应的桶:`v & m1`(填0)和 `(v | 0b100) & m1`(填1)。这就是源码里这个 do-while 循环([dict.c:1500-1519](../../redis-8.0.2/src/dict.c#L1500))的语义:扫大表上所有"游标在差值位上的组合",直到这些位翻完。

```c
/* dict.c:1512-1519 —— 大表上扫扩展位 */
do {
    /* Emit entries at cursor */ 
    de = d->ht_table[htidx1][v & m1];
    while (de) { /* ...fn(privdata, de)... */ }
    /* Increment the reverse cursor not covered by the smaller mask.*/
    v |= ~m1;
    v = rev(v);
    v++;
    v = rev(v);
    /* Continue while bits covered by mask difference is non-zero */
} while (v & (m0 ^ m1));
```

终止条件 `v & (m0 ^ m1)`([dict.c:1519](../../redis-8.0.2/src/dict.c#L1519))是:只要游标在"差值位"上还有非零位,就说明大表还有扩展桶没扫完,继续。这与小表"只扫一桶"形成对照——rehash 中每次 SCAN 可能返回多桶,正是 5.6 节开头注释里说的 "The iterator must return multiple elements per call"(迭代器必须每次返回多桶),目的就是确保大表上分裂出的桶不被漏。

> **不这样会怎样**:如果 rehash 期间 SCAN 只扫小表当前桶、不管大表扩展桶,那么"刚从老表搬到新表的元素"就会被漏——它们在老表已经没了(搬走了),在新表的高位桶里,而 SCAN 只看了小表的低位桶。反之,扫了扩展桶,就会把这些元素**重扫**(因为它们在老表时可能已被扫过)。又是"重可忍,漏不可忍"。

还有一个关键防守:进入 `dictScanDefrag` 第一件事是 `dictPauseRehashing(d)`([dict.c:1448](../../redis-8.0.2/src/dict.c#L1448)),退出时 `dictResumeRehashing(d)`([dict.c:1522](../../redis-8.0.2/src/dict.c#L1522))。这是因为 SCAN 的回调 `fn` 里业务可能反过来调 `dictFind` 等操作,这些操作会触发 `_dictRehashStepIfNeeded` 推进 rehash——一旦 rehash 推进,两张表的桶布局就变了,SCAN 的"不漏"保证就破了。所以 SCAN 期间暂停 rehash,保证这一轮 SCAN 看到的是一致的桶布局快照。

> **钉死这件事**:`dictScan` 在两张表大小不等时,把小表当主表、大表当扩展,扫小表一桶 + 大表所有扩展位桶;并用 `pauserehash` 锁住这一轮 SCAN 期间的桶布局。这是"两张表并存"这个物理结构在遍历语义上的兑现——不漏 key 的代价是每次 SCAN 可能返回重复元素,业务层去重即可。

### 5.6.4 rev 的代价与无状态的好处

`rev` 是 O(log W) = O(log 64) = 6 次循环,常数极小。相比"无状态迭代"带来的好处(客户端断了重连、服务器重启都能继续、不用为每个 SCAN 客户端维护迭代器状态、零额外内存),这点代价微不足道。注释([dict.c:1404-1417](../../redis-8.0.2/src/dict.c#L1404))说得很直白:*This iterator is completely stateless, and this is a huge advantage, including no additional memory used.* 代价是两条:(1)可能重;(2)必须每次返回多桶。这两条业务都能容忍。

```text
  位反转游标在 4 位表(size=16)上的遍历顺序
  (普通递增:0,1,2,...,15;位反转:高位先变)

  普通正向(会漏):          位反转反向(不漏):
  step  cursor   桶         step  cursor(rev递增后)  桶
   0    0000  →  桶0          0    0000  →  桶0
   1    0001  →  桶1          1    1000  →  桶8      ← 先扫高位!
   2    0010  →  桶2          2    0100  →  桶4
   3    0011  →  桶3          3    1100  →  桶12
   4    0100  →  桶4          4    0010  →  桶2
   ...                       ...
  若 step=1 后表从 size=4 扩到 size=8,
  桶0 分裂出桶4(e 落桶4),
  正向游标下一步扫 1..7,漏桶4。
  位反转游标下一步扫高位,
  桶4 自然被覆盖到——重,不漏。
```

## 5.7 技巧精解②:dictTwoPhaseUnlinkFind——为什么删除必须分两阶段

第二章讲 ae 时,我们见过一个"两阶段操作"范式:`aeDeleteTimeEvent` 不就地释放定时事件节点,而是先打 `AE_DELETED_EVENT_ID` 标记,下一轮扫描才摘链释放——因为 `timeProc` 回调可能递归操作定时事件列表,就地释放就是 use-after-free。dict 里有一个**完全同构**的两阶段操作:`dictTwoPhaseUnlinkFind` / `dictTwoPhaseUnlinkFree`,但它要防的"竞态"不是递归,而是 **rehash 让前驱指针失效**。

### 5.7.1 场景:删除一个 key 前要先发 keyspace notification

Redis 的很多删除路径不是"找到就删"这么简单。考虑 `DEL key`:找到 key、从 dict 摘链、释放 key 和 value、发 keyspace notification 通知订阅者(可能触发模块、复制、AOF)。这里"摘链"和"发通知"是两件有副作用的事,中间还会插别的回调。

如果用一步删除(像 `dictDelete` 那样 `dictFind` + `dictGenericDelete` 合并),问题在于:**找到 key 时拿到的"前驱指针" `prev`,在发通知回调执行的间隙,可能被 rehash 改掉**。

具体怎么改掉?rehash 搬桶(`rehashEntriesInBucketAtIndex`)会把整条链拆散,重新挂到新表的不同桶里。搬完后,老 `prev->next` 指向的位置可能已经空了(元素被搬到新表),或者 `prev` 自己都被搬到别的桶、不再是我们目标 key 的前驱。这时还按 `prev->next = target->next` 去摘链,要么摘错节点,要么写到已释放内存——use-after-free。

### 5.7.2 招:Find 阶段暂停 rehash,返回前驱指针的指针

Redis 的解法是把这个操作拆成两阶段,源码在 [dict.c:844-870](../../redis-8.0.2/src/dict.c#L844):

```c
/* dict.c:844-870 —— Find 阶段 */
dictEntry *dictTwoPhaseUnlinkFind(dict *d, const void *key, dictEntry ***plink, int *table_index) {
    uint64_t h, idx, table;

    if (dictSize(d) == 0) return NULL; /* dict is empty */
    if (dictIsRehashing(d)) _dictRehashStep(d);

    h = dictHashKey(d, key, d->useStoredKeyApi);
    keyCmpFunc cmpFunc = dictGetKeyCmpFunc(d);

    for (table = 0; table <= 1; table++) {
        idx = h & DICTHT_SIZE_MASK(d->ht_size_exp[table]);
        if (table == 0 && (long)idx < d->rehashidx) continue;
        dictEntry **ref = &d->ht_table[table][idx];          /* 从桶头开始的"指针的指针" */
        while (ref && *ref) {
            void *de_key = dictGetKey(*ref);
            if (key == de_key || cmpFunc(d, key, de_key)) {
                *table_index = table;
                *plink = ref;                                /* 记下:目标的前驱的 next 字段地址 */
                dictPauseRehashing(d);                       /* ★暂停 rehash */
                return *ref;
            }
            ref = dictGetNextRef(*ref);                      /* 往链表深处走 */
        }
        if (!dictIsRehashing(d)) return NULL;
    }
    return NULL;
}
```

注意三个关键设计:

**第一,返回的是 `dictEntry **plink`(指针的指针),不是 `dictEntry *`。** 这不是多此一举——`plink` 指向的是"目标节点前驱的 `next` 字段"(或桶头的指针)。摘链时只要写 `*plink = dictGetNext(target)` 一步到位,不需要再从头遍历找前驱。这是拉链哈希表删除的常数优化,把"找前驱"这个 O(链长) 动作压成 O(1)。

**第二,找到的瞬间 `dictPauseRehashing(d)`**([dict.c:862](../../redis-8.0.2/src/dict.c#L862))。从这一刻起,`pauserehash` 计数器 +1,rehash 被冻结,桶布局不会再变。这保证 Free 阶段拿到的 `plink` 仍然有效——它指向的那个 `next` 字段,没有被 rehash 搬动过。

**第三,Free 阶段才真正摘链、释放、恢复 rehash**([dict.c:872-881](../../redis-8.0.2/src/dict.c#L872)):

```c
/* dict.c:872-881 —— Free 阶段 */
void dictTwoPhaseUnlinkFree(dict *d, dictEntry *he, dictEntry **plink, int table_index) {
    if (he == NULL) return;
    d->ht_used[table_index]--;
    *plink = dictGetNext(he);     /* ★一步摘链:前驱的 next 直接指向目标的 next */
    dictFreeKey(d, he);
    dictFreeVal(d, he);
    if (!entryIsKey(he)) zfree(decodeMaskedPtr(he));
    _dictShrinkIfNeeded(d);
    dictResumeRehashing(d);       /* ★恢复 rehash */
}
```

`*plink = dictGetNext(he)` 这一行是整个两阶段的核心:因为 Find 阶段已经暂停了 rehash,这里的 `plink` 仍然精确指向目标的前驱;摘链只需一次指针赋值,不会写到错误的节点。

### 5.7.3 为什么不能一步删:算清这笔账

> **不这样会怎样(一步删的反面)**:假设把 Find 和 Free 合并成一个 `dictFindAndDelete`,中间没有暂停 rehash。流程是:遍历找到目标、记下 `prev`、调用 `dictFreeKey`/`dictFreeVal`(这两个回调里业务可能触发别的 dict 操作,而那些操作会调 `_dictRehashStepIfNeeded` 推进 rehash)、然后 `prev->next = target->next` 摘链。问题在于:在 `dictFreeVal` 回调执行期间,rehash 把整条链搬走了,`prev` 现在指向的是新表里某个毫不相干的节点(或者 `prev` 自己被搬到了别的桶),`prev->next = ...` 这一写,要么改错了别人的链,要么写进已释放内存。
>
> **所以这样设计**:Find 阶段拿到 `plink` 的**瞬间**暂停 rehash,把桶布局冻结;Free 阶段在冻结的布局里安全摘链,完事再恢复 rehash。两阶段之间的"间隙"是给业务的回调用的(发 keyspace notification、触发模块 hook、写 AOF),这些回调可以放心地做有副作用的事,因为 rehash 被锁住了,`plink` 不会失效。

这是不是和第二章 ae 的 `AE_DELETED_EVENT_ID` 一脉相承?是的。两者的共性是:**在"找到目标"和"完成操作"之间有副作用回调,而回调可能改变容器本身的结构,这时绝不能假设容器不变**。ae 用"标记 + 延迟摘链 + refcount"三件套,dict 用"暂停 rehash + 指针的指针 + 两阶段配对"——形态不同,命脉相同。

> **钉死这件事**:`dictTwoPhaseUnlinkFind` 用"找到即暂停 rehash + 返回前驱指针的指针 + Free 阶段才摘链"三件套,堵住了"两阶段之间 rehash 让前驱指针失效"这个 use-after-free 窗口。它和 ae 的 `AE_DELETED_EVENT_ID`、第十九章 lazyfree 的异步释放,是 Redis 里"两阶段删除"范式的三个变体——**在可能被回调修改的数据结构上删除元素,先冻结再摘链,绝不就地释放**。

### 5.7.4 `pauserehash` 为什么是计数器而非布尔

顺带说清 [dict.h:116](../../redis-8.0.2/src/dict.h#L116) 那个 `unsigned pauserehash : 15` 为什么是 15 位的计数器,而不是 1 位布尔。因为暂停 rehash 的需求会**嵌套**:SCAN 暂停了一次(`dictPauseRehashing`),SCAN 回调里业务又调了 `dictTwoPhaseUnlinkFind` 又暂停一次——这时 `pauserehash` 变成 2。两阶段 Free 结束 `dictResumeRehashing` 减到 1,SCAN 结束再减到 0,这时 rehash 才真正恢复。如果用布尔,内层的 resume 会把外层的 pause 也清掉,留下一个"以为恢复了其实还在迭代"的窗口。计数器支持嵌套,15 位绰绰有余(同时嵌套 32767 层 pause 不可能发生)。

## 5.8 no_value 字典与指针低 3 位编码:把每一字节榨干

讲完两个重量级技巧,这一节看 dict 怎么在"每个 entry"粒度上省内存。这关系到 `dictType` 里的两个标志位:`no_value` 和 `keys_are_odd`([dict.h:62-65](../../redis-8.0.2/src/dict.h#L62)),以及 dict.c 开头那一组"指针位技巧"宏([dict.c:118-128](../../redis-8.0.2/src/dict.c#L118))。

### 5.8.1 问题:SET 字典为什么要为每个成员分配一个 dictEntry

Redis 的 `SET` 对象,底层在元素多时编码为 hashtable,本质就是一个 dict。但 SET 只关心"成员在不在",**不需要 value**。如果还按通用 dict 那样,每个成员分配一个 `dictEntry`(key + val + next,加上 malloc 开销 ~32 字节),百万级 SET 光 entry 就是 32MB,太亏。

> **不这样会怎样**:为每个 SET 成员分配完整 `dictEntry`,value 字段永远空着不用,白白占 8 字节;加上每个 entry 一次 `zmalloc` 的元数据开销(16 字节对齐填充、分配器元数据),百万级 SET 的内存里一半是"空壳"。

### 5.8.2 招一:no_value 标志 + dictEntryNoValue 结构

dictType 有个 `no_value:1` 标志([dict.h:62](../../redis-8.0.2/src/dict.h#L62))。设了它,dict 就知道"这个字典不需要 value",于是用一种**更瘦的 entry**:`dictEntryNoValue`(只有 key + next,没有 value union)。创建时走 `createEntryNoValue`([dict.c:149](../../redis-8.0.2/src/dict.c#L149)):

```c
/* dict.c:148-154 */
static inline dictEntry *createEntryNoValue(void *key, dictEntry *next) {
    dictEntryNoValue *entry = zmalloc(sizeof(*entry));
    entry->key = key;
    entry->next = next;
    return (dictEntry *)(void *)((uintptr_t)(void *)entry | ENTRY_PTR_NO_VALUE);
}
```

注意最后一行:`entry | ENTRY_PTR_NO_VALUE`——它把返回的指针**最低 3 位塞进 `100`**(`ENTRY_PTR_NO_VALUE = 4`),标记"这是一个 no_value entry"。后面 dict 内部代码靠这个标记区分 entry 的真实结构(详见 5.8.3)。

### 5.8.3 招二:桶里只有一个 key 时,连 entry 都不分配

更激进的是 `dictInsertAtPosition`([dict.c:557](../../redis-8.0.2/src/dict.c#L557))的 no_value 分支([dict.c:565-580](../../redis-8.0.2/src/dict.c#L565)):如果目标桶**当前是空的**,Redis 连 `dictEntryNoValue` 都不分配,**直接把 key 的指针塞进桶**:

```c
/* dict.c:565-580,精简 */
if (d->type->no_value) {
    if (!*bucket) {
        /* 桶是空的 —— 直接把 key 指针当 entry 塞进去,省一次 malloc */
        if (d->type->keys_are_odd) {
            entry = key;                          /* key 地址本身就是奇数,低位含 1,天然标记 */
            assert(entryIsKey(entry));
        } else {
            entry = encodeMaskedPtr(key, ENTRY_PTR_IS_EVEN_KEY);  /* 给偶数 key 强制塞低位标记 */
        }
    } else {
        entry = createEntryNoValue(key, *bucket); /* 桶非空,只能分配 entry 串进链表 */
    }
}
```

这里出现了 dict.c 开头那一组宏([dict.c:118-128](../../redis-8.0.2/src/dict.c#L118))的全部语义:

```c
/* dict.c:124-128 */
#define ENTRY_PTR_MASK        7 /* 111 */
#define ENTRY_PTR_NORMAL      0 /* 000 : 正常带 value 的 dictEntry */
#define ENTRY_PTR_IS_ODD_KEY  1 /* XX1 : 直接是奇数地址的 key 指针 */
#define ENTRY_PTR_IS_EVEN_KEY 2 /* 010 : 偶数地址的 key 指针(强制塞标记) */
#define ENTRY_PTR_NO_VALUE    4 /* 100 : 无 value 的瘦 entry(dictEntryNoValue) */
```

原理:**malloc 返回的指针必然 8 字节对齐,低 3 位永远是 0**。这 3 个"白送的"位,Redis 用来标记"这个指针到底指向什么":

- 低 3 位 = `000`:指向完整 `dictEntry`(key + val + next),用于普通 hash/ZSET 等需要 value 的字典。
- 低 3 位 = `100`:指向 `dictEntryNoValue`(key + next),用于 no_value 字典里桶非空、需要链表串接的场景。
- 低 3 位 = `XX1`(最低位为 1):指针直接就是 key 本身(奇数地址,无需额外标记,低位天然是 1)。
- 低 3 位 = `010`:指针直接就是 key 本身,但 key 地址是偶数,需要强制塞 `010` 标记。

读取时 `entryIsKey`/`entryIsNormal`/`entryIsNoValue`([dict.c:132-146](../../redis-8.0.2/src/dict.c#L132))用一次按位与就把类别判出来,然后用 `decodeMaskedPtr`(把低 3 位清零)还原真实指针。

> **提出问题**:为什么 `keys_are_odd` 这个标志存在?奇数 key 不用额外标记,偶数 key 要塞 `010`,为什么不统一塞?
> **不这样会怎样**:如果所有"桶里直接放 key"的情况都塞标记,那 key 地址得是 `xxx...x100` 或 `xxx...x010` 这种,但 key 来自 sds/client 等各种分配,sds 的地址不保证低 3 位是什么。奇数地址(低位含 1)天然满足"低位非零 = 这是 key"的判别,零成本;偶数地址必须强制塞 `010`(`encodeMaskedPtr`),读出来再 `decodeMaskedPtr` 还原。`keys_are_odd` 这个 flag 就是告诉 dict:"我的 key 地址都是奇数,你不用塞标记",省掉一次 encode/decode。Redis 内部 sds 字符串分配在 jemalloc 下经常返回 8/16 对齐地址(偶数),所以大多数情况走 `ENTRY_PTR_IS_EVEN_KEY` 分支;某些特殊场景(比如把整数 key 直接编码进指针)才用 `keys_are_odd`。
>
> **钉死这件事**:8 字节对齐指针的低 3 位本是无用的零,Redis 把它变成"entry 类别的 tag"——`000`/`100`/`XX1`/`010` 四种。这让 no_value 字典在桶里只有一个 key 时**完全跳过 dictEntry 分配**,直接把 key 指针塞进桶。百万级 SET 里大半桶都只挂一个成员(拉链法负载因子 1),这个优化省下的内存是实打实的几十 MB。这是"内存即数据库"(取向②)把每一字节榨干的极致。

### 5.8.4 no_value 与 rehash 的协作

no_value 字典在 rehash 时也有特殊处理,就在 `rehashEntriesInBucketAtIndex`([dict.c:337-357](../../redis-8.0.2/src/dict.c#L337))里:搬到新桶时,如果新桶是空的,就把原本可能是 `dictEntryNoValue` 的 entry "降级"回直接 key 指针(释放掉旧的 entry 内存);如果新桶非空,就把 key 串进链表(可能要升级成 `dictEntryNoValue`)。这套逻辑保证 no_value 字典在 rehash 全程都维持"能省则省"的紧凑形态。

## 5.9 rehashingCompleted 回调:层间靠回调解耦

最后看一个"软件工程"层面的设计:`rehashingCompleted` 回调。它在 rehash 完成时被触发,**通知上层(调用方)rehash 结束了**。源码里一共四处调用([dict.c:271](../../redis-8.0.2/src/dict.c#L271) 初始化空表、[dict.c:373](../../redis-8.0.2/src/dict.c#L373) 正常搬完、[dict.c:759](../../redis-8.0.2/src/dict.c#L759) dict 释放、[dict.c:1677](../../redis-8.0.2/src/dict.c#L1677) 重建)。

为什么需要这个回调?因为 Redis 8.0 引入了 **kvstore**(多 dict 分片)架构,集群模式下每个 slot 有自己的 dict,rehash 完成意味着"这张 dict 的桶数组换了新地址",上层 kvstore 据此重建 slot→dict 的映射、更新统计、触发内存整理。dict 本身不知道上层是谁,只通过 `dictType->rehashingCompleted` 这个函数指针通知。

这是经典的**控制反转**:`dict` 作为底层通用数据结构,不硬编码任何上层逻辑(不知道 kvstore、不知道 slot),只提供"rehash 完成了"这个事件钩子,由 `dictType` 决定怎么响应。同理 `rehashingStarted`([dict.c:265](../../redis-8.0.2/src/dict.c#L265))在 rehash 开始时触发,让上层做"新表已分配好、可以记下信息"的准备。

> **钉死这件事**:`rehashingStarted`/`rehashingCompleted` 回调是 dict 和上层(kvstore/db)之间的解耦点。dict 只管"我 rehash 了"这个事实,具体怎么响应是上层的事。这让 dict 能复用于 SET、HASH、ZSET、db keyspace、cluster slot 各种场景,而不用为每种场景改 dict 源码。这是"简单优先"(取向④)在 API 设计上的落地——**用一个函数指针代替一组 if-else 分支**。

## 5.10 几个值得记住的小技巧(汇总)

- **指数存大小,移位算掩码**。`ht_size_exp` 用 `signed char` 存指数,`DICTHT_SIZE_MASK` 一次移位出掩码([dict.h:105](../../redis-8.0.2/src/dict.h#L105))。把"求掩码"这件高频事压成 1 个 CPU 周期。
- **缩容不重算哈希**。老桶下标对新掩码按位与,直接得到新桶下标([dict.c:335](../../redis-8.0.2/src/dict.c#L335))。能省一次 SipHash(几十纳秒)就省。
- **`__builtin_clzl` 一条指令定容量**([dict.c:1630](../../redis-8.0.2/src/dict.c#L1630))。求"≥ size 的最小 2 的幂"用编译器内建,编译成单条 `BSR`/`LZCNT`。
- **`empty_visits = n*10` 给空跳封顶**([dict.c:394](../../redis-8.0.2/src/dict.c#L394))。看似不起眼,却是"渐进"不被稀疏表拖垮成"卡顿"的关键护栏。
- **`pauserehash` 计数器而非布尔**([dict.h:116](../../redis-8.0.2/src/dict.h#L116))。迭代器、SCAN、`dictTwoPhaseUnlinkFind` 等多处可能同时要求暂停,用计数器才能正确支持嵌套——`pause++`/`resume--`,归零才真正恢复。
- **指针低 3 位编码 entry 种类**([dict.c:118-128](../../redis-8.0.2/src/dict.c#L118))。8 字节对齐的指针低 3 位本是 0,Redis 把"这是 key 本身还是 dictEntry"塞进去,让 no_value 字典省掉一次 `dictEntry` 分配。
- **`redis_prefetch_read` 预取**([dict.c:792](../../redis-8.0.2/src/dict.c#L792))。`dictFindByHash` 在访问桶和下一个 entry 前都发了 prefetch 指令,让 CPU 在比对当前 key 时提前把下一个 entry 拉进缓存,掩盖链表遍历的 cache miss 延迟。

## 5.11 摊还分析:为什么渐进仍然 O(1)

有人会担心:rehash 期间每次操作都要"顺手搬一桶 + 两张表都查",会不会让平均复杂度退化?结论是**不会,依然是 O(1) 摊还**。

证明直觉(聚合法):假设表从 N 扩到 2N,总共要搬 N 个元素。搬运工作分两部分——(1)用户每次访问顺手搬一桶,每个元素在它被搬的那一桶里被搬一次;(2)serverCron 限时 1ms 兜底搬一批。把整个 rehash 周期(从触发扩容到 `rehashidx` 归 -1)内所有的搬运成本加起来,是 Θ(N);而这段时间内用户操作总数也是 Θ(N) 量级(否则表不会扩)。**总搬运成本 ÷ 操作总数 = Θ(N)/Θ(N) = Θ(1)**,每个操作摊到的搬运成本是常数。

更直观地算一笔账:百万级 dict 一次性扩容,老办法(Java HashMap 风格)要主线程串行搬运 100 万次,假设每次 100ns(算哈希 + 改指针),合计 100ms 主线程阻塞——对 Redis 这是灾难。渐进式把这 100ms 摊到扩容周期的每条命令上,假设扩容周期内来了 10 万条命令,每条命令分摊 1μs——**每条命令贵了 1μs,但没有任何一条卡住**。从"一次卡 100ms"变成"十万条各贵 1μs",这是摊还分析在工程上最美的应用之一。

> **钉死这件事**:渐进式 rehash 的平均复杂度是 O(1) 摊还,不是 O(1) 最坏——每条命令最坏可能赶上"搬一长串桶",但概率极低;平均下来每条命令只多花常数时间。这是"聚合法摊还分析"的教科书例子:**总工作量 ÷ 操作数 = 平均每次的工作量**,只要总工作量随操作数线性增长,平均就是 O(1)。

## 5.12 对比 Java HashMap:一次性 vs 渐进式

把本章的 dict 和最熟悉的 Java `HashMap` 放一起对比,能把"为什么 Redis 这么设计"看得更清:

| 维度 | Java HashMap | Redis dict |
|------|-------------|-----------|
| 冲突解决 | 拉链法(链表→红黑树) | 拉链法(纯链表) |
| 扩容时机 | 负载因子 > 0.75 | 负载因子 > 1(平时)/ > 4(有子进程) |
| 扩容方式 | **一次性 `resize()`**:遍历老表,所有元素算新位置塞新表 | **渐进式**:只分配新表设 `rehashidx=0`,之后每操作搬一桶 + 定时搬一批 |
| 扩容阻塞 | 单线程下 O(N) 阻塞 | **永不阻塞**,每次 O(1) |
| 表大小 | 2 的幂 | 2 的幂 |
| rehash 中间态 | 无(原子切换) | 两张表并存,读两张都查、写进新表 |
| 哈希函数 | `Object.hashCode()`(可被攻击) | SipHash(带密钥,抗攻击) |
| 并发 | 非线程安全(`ConcurrentHashMap` 另说) | 单线程访问,无需锁 |
| 遍历 | `Iterator`(有状态,*fail-fast*) | `SCAN`(无状态,可能重不漏) |

Java HashMap 的设计假设是"通用、单线程、元素量有限(几十万到顶)"——一次性 resize 几毫秒能接受。Redis 的约束完全不同:**单线程、元素量可能百万级、扩容阻塞 = SLA 违约**。所以 Redis 必须把一次性摊成渐进式,代价是引入两张表的复杂中间态、SCAN 的位反转游标、两阶段删除等一系列配套设计。**复杂度没消失,只是从"一次大阻塞"转移成了"无数次小动作 + 更复杂的数据结构维护"**——这正是本书反复出现的"复杂度守恒"在 dict 上的具体落地。

## 章末:回扣、五个为什么、往哪钻

### 主线回扣

本章是**取向①(把耗时从主线程解放)在数据结构层的头号案例**。但请注意一个微妙之处:dict 的渐进式 rehash **并没有把工作挪到别的线程**(那是后面 I/O 多线程、异步删除 `lazyfree` 干的事),而是**把工作切碎摊进主线程的无数次空闲缝隙里**。这是 Redis 在"单线程"约束下解同一道题的另一种姿势——不是逃离主线程,而是让主线程感觉不到这件大事。百万级字典的扩容,就这样被两个机制(每操作搬一桶 + serverCron 限时 1ms)拆成了无数个 O(1) 微动作,没有一次卡顿。

涉及的设计取向再点一遍:

- **取向①(单线程+事件循环)**:渐进式 rehash 的全部存在意义,就是不让扩容阻塞主线程。如果 Redis 是多线程的,直接开个后台线程一次性搬完就行,dict 根本不用设计得这么复杂。
- **取向②(内存即数据库)**:扩缩容阈值(1 / 4 / 1/8 / 1/32)的精心挑选,BGSAVE 期间主动 `AVOID`,no_value 字典 + 指针低 3 位编码省下每 entry 几十字节——都是为了把内存这个稀缺资源管到极致。
- **取向④(简单优先)**:用拉链法而非开放寻址,用 2 的幂而非素数桶数,牺牲一点理论优雅换实现简单。SipHash 抗攻击是少数 Redis 选择"不那么简单"的地方,因为它换的是安全。
- **取向⑤(可靠性靠持久化+复制)**:`updateDictResizePolicy` 在 fork 子进程期间禁扩容,是为了不破坏 COW、保护 RDB/AOF 子进程的内存视图;`pauserehash` 在迭代期间停搬,是为了让 `SCAN` 这类命令看到一致快照。

### 五个为什么

**Q1:`rehashidx` 为什么是从 0 往后递增,而不是从最大值往前?**
从 0 往后递增,保证"老表前部已搬、后部待搬"是一个单调推进的进度条——`dictFindByHash` 里 `if (table==0 && idx < rehashidx) continue` 这一行才能成立(下标小于 rehashidx 的桶一定已搬,直接跳老表去新表找)。如果反过来从后往前,这个判定就得改成 `idx > rehashidx`,而且新元素落新表的规则要相应调整。两种方向数学上等价,Redis 选了"从前往后"这种更符合直觉的进度条语义。

**Q2:`empty_visits = n*10`,这个 10 是怎么定的?**
这是经验值,源码没解释。直觉是:大多数哈希表负载因子在 0.5~1 之间,空桶比例约 50%,平均每搬 1 个非空桶要跳过 1 个空桶。给 10 倍余量,是为了在异常稀疏的表(比如大量 key 被 EXPIRE 后)上也能一次推进几个桶,不至于一次调用只跳空不搬。这个值太大(比如 1000)会让一次 rehash 步骤耗时过长失去"渐进"意义,太小(比如 1)会在稀疏表上频繁空手而归。

**Q3:SCAN 在 rehash 期间真的能保证不漏吗?位反转游标的边界在哪?**
能保证不漏,但有两个前提:(1)表大小只在 2 的幂之间切换(这是 dict 的硬约束);(2)SCAN 期间用 `pauserehash` 锁住当前轮的桶布局。边界情况:如果 SCAN 客户端两次调用之间表连续扩容多次(从 4→8→16),位反转游标仍然能正确续上——因为每次扩容分裂出的新桶都在高位,而游标已经在遍历高位。唯一的代价是重复元素增多。SCAN 的官方保证是 *weak consistency*:不漏、可能重、不保证快照一致。

**Q4:`dictTwoPhaseUnlinkFind` 和 `dictDelete` 有什么区别?什么时候用前者?**
`dictDelete` 是"找到 + 摘链 + 释放"一气呵成,中间没有回调间隙,rehash 不会在中间插进来(因为 `_dictRehashStep` 只在 dict API 入口调,`dictDelete` 内部不再调)。它用于"纯删除、无副作用回调"的场景。`dictTwoPhaseUnlinkFind` 用于"删除前后要发 keyspace notification、触发模块 hook、写 AOF 等有副作用回调"的场景——这些回调可能反过来操作 dict(触发 rehash),必须在 Find 阶段冻结 rehash,在 Free 阶段才摘链。Redis 的 `DEL`/`UNLINK` 命令路径、keyspace notification 触发点都走两阶段。

**Q5:为什么 dict 用拉链法而不用开放寻址法?后者缓存更友好啊。**
开放寻址法(线性探测/二次探测)在内存里确实缓存友好(连续数组),但它有个致命问题:**删除复杂**。开放寻址删除一个元素不能直接置空(会打断探测链),要用"墓碑标记",墓碑多了查找效率下降,需要定期 rehash 清理——这和 Redis 的渐进式 rehash 冲突(渐进 rehash 假设"搬完一桶就清空一桶",墓碑会让"清空"语义复杂化)。拉链法删除就是链表节点摘除,O(1) 且无墓碑,和渐进式 rehash 天然契合。另外拉链法对"负载因子 > 1"的容忍度更高(链表可以拉长),而开放寻址一旦满了就必死。Redis 选拉链法,是用" cache 友好性"换"删除和 rehash 的简单性",符合取向④。

### 想继续深入往哪钻

- 想看 SCAN 在生产里的真实行为:用 `redis-cli --scan --pattern '*'` 在百万级 db 上跑,观察返回的 cursor 跳跃规律(会看到 cursor 不是连续递增,而是位反转递增);用 `DEBUG CHANGE-REPL-ID` 配合大量写入触发 rehash,再 SCAN,观察重复 key。
- 想理解 kvstore 怎么用 `rehashingCompleted` 回调:读 [kvstore.c](../../redis-8.0.2/src/kvstore.c) 里 `kvstoreDictGetHash` 和 `kvstoreRehashingCompleted`(它据此更新 slot→dict 映射,是 Redis 7.0 集群性能优化的关键)。
- 想看 dict 如何被 db、SET、HASH、ZSET 复用:读 [server.c](../../redis-8.0.2/src/server.c) 里 `dbDictType`、`setDictType`、`hashDictType`、`zsetDictType` 的定义,看它们怎么通过 `dictType` 定制 hash/keyCmp/keyDest/no_value。
- 想对比"多线程哈希表"怎么解同一道题:看 Java `ConcurrentHashMap`(分段锁/CAS)、Go `sync.Map`(读多写少的双 map)——它们都为了并发改了 dict 的形态,而 Redis 因为单线程,dict 形态保持最简。
- 想看 SipHash 的具体实现:读 [siphash.c](../../redis-8.0.2/src/siphash.c),它是 SipHash-2-4 的参考实现,核心是 2 轮 SipMix 的 ARX(加-旋-异或)结构。

### 引出下一章

dict 解决的是"键值对怎么存"——但这是"大而专业"的结构。Redis 还有另一类对象:元素**很少**的小集合、小哈希、小列表。用 dict 存一个只有 3 个 field 的 `HASH`,光两张桶数组的指针(4 桶 × 8 字节 = 32 字节)就比数据本身还大,加上每个 entry 的 malloc 开销,太亏。所以 Redis 在 dict 之外,又准备了一族"小而紧凑"的编码——把整个容器塞进一块连续内存,不用指针、不用桶数组、不用 malloc 节点。下一章我们就走进 `listpack`:看 Redis 如何用一个紧凑的字节序列,把小数据结构的内存开销压到极致,并在它长大后,再优雅地升格成 dict 或 quicklist。这是取向③(编码自适应)的开篇,也是"小用简单结构、大用专业结构"这条主线第一次正式登场。

---

## 验证物:如何亲手确认本章的设计

> 说明:本书写作环境为 Windows,无法直接运行 redis-server(8.0 依赖 fork/epoll 等 Linux 特性)。以下 (1) gdb 断点脚本 (2) 源码常量锚点 (3) OBJECT ENCODING 观察项 均为可复现的精确指引,供读者在 Linux 环境(Ubuntu 22.04 / CentOS 8 等)对 redis-8.0.2 源码 `make no-opt`(Makefile 里 no-opt 目标会去掉 -O2 加 -g)编译后自行验证。**本书不附编造的运行输出**——凡未实跑的,只给脚本与预期观察变量,不写具体数值。

### 1. gdb 断点脚本

编译:`cd redis-8.0.2 && make no-opt`(带 -g)
启动:`gdb ./src/redis-server`,另一终端 `redis-cli`。

```gdb
# === 验证 dictRehash 的 rehashidx 推进 ===
(gdb) break dictRehash             # 搬桶引擎,dict.c:393
(gdb) break rehashEntriesInBucketAtIndex  # 搬单桶,dict.c:321
(gdb) break dictCheckRehashingCompleted   # 收尾,dict.c:370
(gdb) break _dictResize            # 扩容入口,dict.c:227(看 264 行设 rehashidx=0)
(gdb) run --port 6379

# redis-cli 大量 SET 触发扩容:
#   for i in $(seq 1 100000); do redis-cli SET key$i v; done
# gdb 在 _dictResize 停下(第一次扩容):
(gdb) print d->ht_size_exp[0]      # 预期:2(老表 4 桶)
(gdb) print d->ht_used[0]          # 预期:4(刚满)
(gdb) next                         # 走到 264 行后:
(gdb) print d->rehashidx           # 预期:0(开始 rehash)
(gdb) print d->ht_size_exp[1]      # 预期:3(新表 8 桶)
# 在 dictRehash 停下,观察 empty_visits 和 rehashidx 推进:
(gdb) print empty_visits           # 预期:n*10(如 n=1 则为 10)
(gdb) print d->rehashidx           # 预期:逐步递增,直到 ht_used[0]==0

# === 验证 dictScan 的位反转游标 ===
(gdb) break dictScanDefrag         # SCAN 主体,dict.c:1435
(gdb) break rev                    # 位反转函数,dict.c:1325
(gdb) break dict.c:1470            # v = rev(v) 非 rehash 分支
# redis-cli 执行:SCAN 0 COUNT 10
# gdb 在 dictScanDefrag 停下:
(gdb) print v                      # 预期:客户端传来的 cursor(首次为 0)
(gdb) print m0                     # 预期:当前表的掩码(size-1)
# 单步到 1470-1472,观察位反转递增:
(gdb) print v                      # 反转前
(gdb) next 3                       # 走完 rev/++/rev 三步
(gdb) print v                      # 预期:高位递增后的新 cursor(不是简单的 v+1)

# === 验证 dictTwoPhaseUnlinkFind 的两阶段 ===
(gdb) break dictTwoPhaseUnlinkFind  # Find 阶段,dict.c:844
(gdb) break dictTwoPhaseUnlinkFree  # Free 阶段,dict.c:872
(gdb) break dict.c:862             # dictPauseRehashing 那一行
# redis-cli 执行:DEL key1(若 key1 存在)
# gdb 在 dictTwoPhaseUnlinkFind 停下:
(gdb) print d->pauserehash         # 预期:进入前的值
(gdb) next                         # 走到 862 行(dictPauseRehashing)后:
(gdb) print d->pauserehash         # 预期:+1(Find 阶段冻结了 rehash)
# 在 dictTwoPhaseUnlinkFree 停下,观察摘链和恢复:
(gdb) print *plink                 # 预期:指向目标 entry 的前驱 next 字段
(gdb) next                         # 走过 *plink = dictGetNext(he) 后,目标被摘链
(gdb) print d->pauserehash         # 走到 880 行(dictResumeRehashing)后:预期 -1 恢复
```

**预期观察**(基于源码 [dict.c:393-422](../../redis-8.0.2/src/dict.c#L393) / [1435-1525](../../redis-8.0.2/src/dict.c#L1435) / [844-881](../../redis-8.0.2/src/dict.c#L844),本书未实跑):扩容时 `rehashidx` 从 0 单调递增到老表大小,期间 `ht_used[0]` 单调递减、`ht_used[1]` 单调递增;SCAN 的 cursor 在位反转递增后呈现"高位先变"的非连续跳跃;两阶段删除期间 `pauserehash` 先 +1(Find)后 -1(Free)。

### 2. 源码常量锚点(带行号,从 redis-8.0.2 源码 Grep 核实)

| 常量/字段 | 位置 | 值/说明 |
|----------|------|---------|
| `struct dict` | dict.h:107-122 | ht_table[2]/ht_used[2]/rehashidx/pauserehash:15/ht_size_exp[2]/pauseAutoResize/metadata |
| `DICT_HT_INITIAL_EXP` | dict.h:156 | 2(初始 4 桶) |
| `HASHTABLE_MIN_FILL` | dict.h:28 | 8(缩容阈值 1/8) |
| `dict_force_resize_ratio` | dict.c:43 | 4(强制扩容阈值,有子进程时) |
| `dictGenHashFunction`→siphash | dict.c:110-112 | 默认哈希,SipHash-2-4 带密钥 |
| `dictSetHashFunctionSeed` | dict.c:96 | 启动时写 16 字节随机种子 |
| `_dictNextExp`(`__builtin_clzl`) | dict.c:1625-1631 | 一条指令定容量(≥size 的最小 2 的幂) |
| `_dictResize`(设 rehashidx=0) | dict.c:227, 264 | 只分配新表设游标就返回 |
| `dictExpandIfNeeded`(双阈值) | dict.c:1542, 1556-1564 | 1 或 4 触发扩容 |
| `dictShrinkIfNeeded`(1/8 阈值) | dict.c:1579, 1584 | 低于初始大小不缩 |
| `dictRehash`(empty_visits=n*10) | dict.c:393, 394 | 搬 n 个非空桶,空跳封顶 10n |
| `dictRehashMicroseconds`(1ms) | dict.c:434 | serverCron 兜底限时搬运 |
| `rehashEntriesInBucketAtIndex`(缩容不重算哈希) | dict.c:321, 331-336 | `h = idx & 新mask` |
| `dictCheckRehashingCompleted` | dict.c:370-382 | 收尾:触发回调+释放老表+rehashidx=-1 |
| `dictFindByHash`(两表都查) | dict.c:770, 787-810 | `idx < rehashidx` 跳老表 |
| `dictFindPositionForInsert`(新元素进新表) | dict.c:1637, 1669 | `bucket = &ht_table[rehashing?1:0][idx]` |
| `_dictRehashStepIfNeeded` | dict.c:1609-1622 | 命中桶>=rehashidx 就地搬,否则搬 rehashidx |
| `rev`(位反转,SCAN 灵魂) | dict.c:1325-1333 | 对折位交换,O(log W) |
| `dictScanDefrag`(SCAN 主体) | dict.c:1435, 1470-1472, 1514-1516, 1519 | `v=rev(v);v++;v=rev(v)` 位反转递增 |
| `dictTwoPhaseUnlinkFind`/`Free` | dict.c:844-870 / 872-881 | 两阶段删除,Find 暂停 rehash@862,Free 摘链+恢复@880 |
| 指针低 3 位编码宏 | dict.c:118-128 | ENTRY_PTR_NORMAL=0/IS_ODD_KEY=1/IS_EVEN_KEY=2/NO_VALUE=4 |
| `createEntryNoValue`(no_value 瘦 entry) | dict.c:148-154 | 仅 key+next,低 3 位塞 100 |
| `rehashingCompleted` 回调四处 | dict.c:271 / 373 / 759 / 1677 | rehash 完成通知上层(kvstore 重建映射) |

### 3. OBJECT ENCODING 观察项(需本地 redis-server)

> 以下操作需在 Linux 本地启动 redis-server 后用 redis-cli 执行。本书未实跑,仅列观察方法与预期切换点(阈值来自 config 默认值,可 `CONFIG GET` 确认)。

```text
# 观察 HASH 编码从 listpack → hashtable 的切换(hash-max-listpack-entries 默认 128):
127.0.0.1:6379> CONFIG GET hash-max-listpack-entries    # 预期 128
127.0.0.1:6379> DEL myhash
127.0.0.1:6379> HSET myhash f1 v1                       # 1 个 field
127.0.0.1:6379> OBJECT ENCODING myhash                  # 预期 listpack
# 循环 HSET 到 129 个 field(超过阈值):
127.0.0.1:6379> OBJECT ENCODING myhash                  # 预期 hashtable(升级为 dict)

# 观察 SET 编码切换 + 间接印证 no_value dict:
127.0.0.1:6379> CONFIG GET set-max-listpack-entries     # 预期 128
127.0.0.1:6379> DEL myset
127.0.0.1:6379> SADD myset m1
127.0.0.1:6379> OBJECT ENCODING myset                   # 预期 listpack
# 循环 SADD 到 129 个成员:
127.0.0.1:6379> OBJECT ENCODING myset                   # 预期 hashtable
# myset 现在是 no_value=1 的 dict,每个成员不分配完整 dictEntry
# (源码层面:dictInsertAtPosition @ dict.c:565 的 no_value 分支,
#  桶里只挂一个成员时直接塞 key 指针,见 dict.c:566-576)

# 观察 SCAN 在 rehash 期间的 cursor 跳跃(印证位反转递增):
127.0.0.1:6379> FLUSHDB
127.0.0.1:6379> DEBUG SET-ACTIVE-EXPIRE 0               # 关闭后台过期,稳定布局
# 先灌入足够多 key 触发扩容(让 db->dict 处于 rehash 中间态):
127.0.0.1:6379> for i in $(seq 1 100000); do redis-cli SET k$i v; done
127.0.0.1:6379> SCAN 0 COUNT 10                         # 预期:返回 cursor 不是 1/2/3 这种连续值
                                                         # 而是类似 4096/8192/... 的高位跳变值
127.0.0.1:6379> SCAN <上一轮返回的cursor> COUNT 10       # 继续,观察 cursor 序列呈位反转递增
```

标注:以上预期基于源码常量与 [config.c](../../redis-8.0.2/src/config.c) 默认阈值推导,本书未在本地实跑;若你的 redis 版本/配置不同,切换点可能偏移,以 `CONFIG GET` 实际值为准。SCAN cursor 的具体数值取决于 db->dict 当前的表大小和 rehash 状态,但"非连续高位跳变"这一规律(位反转递增的体现)在任意大小表上都成立。
