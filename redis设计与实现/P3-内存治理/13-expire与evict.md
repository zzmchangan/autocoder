# 第十三章 · 过期与淘汰:双策略与采样近似

> 篇:P3 内存治理(收口章)
> 主轴呼应:本章是**取向①(把耗时从主线程解放)的集大成**。过期的"全库扫描"被拆成 lazy(访问时顺手删)+ active(限时段定时扫);淘汰的"全局排序"被采样近似替代。两个本来会长时间霸占主线程的操作,都被化整为零——这是 Redis 单线程能扛百万 key、又能保持亚毫秒延迟的隐形支柱。

---

## 读完本章你会明白

1. **为什么 Redis 不开一个后台线程扫过期 key**——因为那会和主线程抢 CPU、抢锁、抖动延迟,直接违背取向①。Redis 的答案是把过期拆成"访问时顺手删"(lazy)+ "限时占主线程一小段"(active)两条腿,谁也不能独占。
2. **active expire 凭什么不会卡死主线程**——它有一个用百分比算出来的时间预算(`config_cycle_slow_time_perc% × 1s / hz`),FAST 模式 1000μs,SLOW 模式 25ms,每 16 轮迭代查一次表,超时立刻退出、把控制权交还事件循环。
3. **为什么 Redis 不维护精确 LRU 链表**——百万 key 一人两个指针 = 十几 MB 内存,且每次 GET/SET 都要改链表污染缓存行。Redis 改用"采样 N 个 + 16 槽常驻淘汰池"的近似,信息量逼近精确 LRU,代价是零额外结构(就复用 redisObject 的 24 位 lru 字段)。
4. **★LFU 凭什么用 8 位就能记千万次访问**——这是本章的硬骨头。`LFULogIncr` 用 Morris-like 对数概率计数器:`p = 1/(counter×lfu_log_factor+1)`,counter 越大,下次真正 +1 的概率越低。这一条公式让 8 位计数器饱和在 255,却能区分"热"与"很热"——它的数学本质是把"线性计数"换成了"对数计数",本节会推给你看。
5. **LRU 24 位时钟为什么会回绕、回绕了怎么办**——24 位毫秒时钟约 194 天一圈,`estimateObjectIdleTime` 用一个简单的 `clock < o.lru` 判断检测回绕,加一圈减旧值即可。这是"用回绕换省内存"的典型工程取舍。
6. **淘汰池为什么是常驻 16 个、跨调用保留**——单次采样 5 个信息量太小,可能"运气差"淘汰了好 key。16 槽常驻池是个滚动 Top-K,把多次采样的结果累积起来,真正淘汰时从最右端取,等价于"有偏样本流上的近似"。
7. **lazy + active 双策略为什么缺一不可**——lazy 管正确性(读到的过期 key 必删),active 管内存(没人读的过期 key 也要被清)。单独用哪个都不行:只用 lazy 会让冷数据永不回收(内存泄漏);只用 active 永远扫不完亿级 key,且读路径不检查会出现"过期还能被读到"的窗口。

---

> **如果一读觉得太难:先只记住三件事**——
> ① 过期 = lazy(访问时删,`expireIfNeeded`)+ active(限时扫,`activeExpireCycle`),前者管正确、后者管内存;
> ② 淘汰 = 采样 N 个(`maxmemory-samples` 默认 5)进 16 槽常驻淘汰池,从池右端取最该淘汰的;
> ③ LRU 和 LFU 复用同一个 24 位 `redisObject.lru` 字段——LRU 用全部 24 位存时钟,LFU 拆 16 位时间 + 8 位对数频率计数器。
> 这三件事,就是本章的全部。

---

> **一句话点破:过期和淘汰本质是同一道题——"怎么在不卡死主线程的前提下,把该删的 key 删掉"。Redis 的解法是化整为零 + 概率近似:把"全库扫一遍"切成"每秒 10 次、每次 25ms 的小段",把"全局排序"换成"随机抓 5 个、塞进 16 槽滚动 Top-K",把"精确计数访问次数"换成"Morris 概率对数计数器、8 位表达千万次"。每一个"不精确"都换来了"主线程不被霸占",这是取向①的魂。**

## 13.1 这块要解决什么:两件"不能一次性做完"的事

Redis 是个内存数据库,有两件天然带着矛盾的活:**过期(expire)** 和 **淘汰(evict)**。

- **过期**:`EXPIRE key 60` 之后,这个 key 在 60 秒后逻辑上就该消失。问题是谁去删它?最直觉的答案是开一个后台线程扫全库——但这会和主线程抢 CPU、抢 dict 的锁、抖动命令延迟,直接违背取向①(单线程 + 事件循环,绝不让某个操作长时间霸占主线程)。可如果**完全不主动删**,过期 key 就会一直躺在哈希表里占内存,违背取向②(内存即数据库,每一字节都金贵)。
- **淘汰**:内存写满(`maxmemory`)了,必须挑几个 key 删掉腾地方。挑谁?最朴素的精确 LRU 是维护一个全局双向链表,每次访问把 key 挪到表头,淘汰表尾。但这意味着**每次读/写都要更新链表**——从 O(1) 退化成 O(1)+两次指针改写+可能的缓存行污染,在高 QPS 下放大成可观的尾延迟;而且百万 key 每人多两个指针 = 十几 MB 额外内存,违背取向④(简单优先)和取向②(省内存)。

> **不这样会怎样**:① 后台线程扫全库方案,在线程间共享 dict 时要加读写锁,百万 key 的扫描加上锁竞争,单次扫描可能耗时几百毫秒;期间主线程的命令全被阻塞,P99 延迟飙升。② 精确 LRU 全局链表方案,GET 路径从 O(1) 变成 O(1)+链表指针改写,在高 QPS(50 万 QPS)下,百万级 key 的链表分散在内存各处,每次 GET 都要 touch 两个新缓存行(前驱/后继节点),L3 cache miss 累计成毫秒级延迟。两条路都和"单线程亚毫秒延迟"的 Redis 标志性卖点正面冲突。

Redis 的回答是同一招的两种用法:**化整为零 + 概率近似**。

- 过期 = **lazy 删除(访问时顺手删)** + **active 删除(定时限量地扫)**,两条腿走路,缺一不可。
- 淘汰 = **采样近似(随机抓 N 个 key,挑最该淘汰的塞进 16 槽常驻池)**,不维护全局排序,只在需要淘汰时临时算。

这两套机制看起来朴素,但每一处都藏着精妙的工程权衡。本章把它们拆开,连同源码一行一行讲透。

## 13.2 lazy 过期:访问路径上的顺手清理

每条命令执行前都要先 `lookupKey` 把 key 取出来。取的时候顺带问一句:"这 key 过期了吗?"过期就当场删,返回"不存在"。这套机制的入口是 `expireIfNeeded`([db.c:2268](../../redis-8.0.2/src/db.c#L2268)):

```c
/* db.c:2268-2271 */
int expireIfNeeded(redisDb *db, robj *key, int flags) {
    return expireIfNeededWithSlot(db,key,flags,getKeySlot(key->ptr));
}
```

判定是否过期落在 `keyIsExpired`([db.c:2233](../../redis-8.0.2/src/db.c#L2233)),它从 `expires` 字典里取出存的过期时间戳,跟当前时间比一下:

```c
/* db.c:2233-2248,精简 */
int keyIsExpired(redisDb *db, robj *key) {
    return keyIsExpiredInternal(getExpire(db,key));
}
static inline int keyIsExpiredInternal(mstime_t when) {
    if (server.loading) return 0;       /* 载入阶段不判过期(RDB/AOF 还原时) */
    if (when < 0) return 0;             /* 没设过期时间 */
    const mstime_t now = commandTimeSnapshot();
    return now > when;                  /* 当前时间 > 过期点 ⇒ 已过期 */
}
```

`commandTimeSnapshot()` 是个值得注意的细节:它返回 Redis 的**时间缓存**(`server.mstime`,每轮事件循环在 `updateCachedTime` 里刷新一次),而不是每次都调 `gettimeofday`——这是为省系统调用做的常数优化(第二章 2.5 节讲过)。

判定过期后真正动手删的是 `deleteExpiredKeyAndPropagate`([db.c:2169](../../redis-8.0.2/src/db.c#L2169)),它走的是 `deleteKeyAndPropagate`([db.c:2118](../../redis-8.0.2/src/db.c#L2118))这个统一删除入口。注意 `deleteKeyAndPropagate` 里这两行的分流([db.c:2120-2121](../../redis-8.0.2/src/db.c#L2121)):

```c
/* db.c:2120-2121 */
int del_flag = notify_type == NOTIFY_EXPIRED ? DB_FLAG_KEY_EXPIRED : DB_FLAG_KEY_EVICTED;
int lazy_flag = notify_type == NOTIFY_EXPIRED ? server.lazyfree_lazy_expire
                                              : server.lazyfree_lazy_eviction;
```

这两个 flag 决定了删除的语义:**`DB_FLAG_KEY_EXPIRED`** 标记这次是"过期删除"(用于 keyspace 通知和副本复制时区分语义),**`lazyfree_lazy_expire`** 决定大对象(比如百万元素的 hash)是同步删还是丢给 BIO 后台线程异步删(见第十九章 lazyfree)。这是取向①在删除路径的延伸:即使要删一个 GB 级的大 key,也绝不阻塞主线程。

这套机制的好处直白:**只要 key 被读到,就绝不可能返回过期数据**(正确性硬保证)。坏处也直白:**没人读的过期 key 永远删不掉**,会像沉积物一样堆积占内存。于是需要第二条腿——active 过期。

> **钉死这件事**:lazy 过期保证正确性,代价是 O(1) 的一次时间比较,落在每条命令的 lookup 路径上。它不解决"没人读的过期 key 占内存"——那是 active 的事。`lazyfree_lazy_expire` 开关让大对象的删除走 BIO 后台线程,这是 Redis 4.0+ 才有的,早期的过期删除是纯同步。

## 13.3 active 过期:限时占主线程一小段

active 删除的入口挂在事件循环的心跳里。`serverCron` 每 `server.hz` 次/秒(默认 10Hz,即每 100ms 一次)被调度,它调用 `databasesCron`,后者启动**慢周期**:

```c
/* server.c:1149 节选 */
void databasesCron(void) {
    if (server.active_expire_enabled && iAmMaster()) {
        activeExpireCycle(ACTIVE_EXPIRE_CYCLE_SLOW);
    }
    /* ... 渐进式 rehash、active defrag ... */
}
```

此外,`beforeSleep`(每次事件循环醒来后、处理客户端前)还会插一个**快周期**:

```c
/* server.c:1772 */
if (server.active_expire_enabled && iAmMaster())
    activeExpireCycle(ACTIVE_EXPIRE_CYCLE_FAST);
```

### 13.3.1 时间预算:百分比 + 两档

慢周期与快周期的差别全在**时间预算**。看 `activeExpireCycle`([expire.c:187](../../redis-8.0.2/src/expire.c#L187))顶部四个常量,这是整个机制的标尺:

```c
/* expire.c:94-98 */
#define ACTIVE_EXPIRE_CYCLE_KEYS_PER_LOOP 20       /* 每轮每库采样 20 个      */
#define ACTIVE_EXPIRE_CYCLE_FAST_DURATION 1000     /* 快周期上限 1000 微秒    */
#define ACTIVE_EXPIRE_CYCLE_SLOW_TIME_PERC 25      /* 慢周期最多吃 25% CPU    */
#define ACTIVE_EXPIRE_CYCLE_ACCEPTABLE_STALE 10    /* 过期比例阈值 10%        */
```

最关键的一句是**时间预算的计算**([expire.c:247](../../redis-8.0.2/src/expire.c#L247)):

```c
/* expire.c:247 */
timelimit = config_cycle_slow_time_perc*1000000/server.hz/100;
```

`server.hz` 默认 10,`config_cycle_slow_time_perc` 默认 25,代入得:

```
timelimit = 25 × 1000000 / 10 / 100 = 25000 微秒 = 25 毫秒
```

也就是说,**每次 SLOW 周期最多干 25ms**——这是"25% CPU"的来源:每秒跑 10 次,每次 25ms,合计 250ms,占一秒的 25%。然后无论如何要把控制权交还事件循环。FAST 模式则直接覆盖([expire.c:251-252](../../redis-8.0.2/src/expire.c#L251)):

```c
/* expire.c:251-252 */
if (type == ACTIVE_EXPIRE_CYCLE_FAST)
    timelimit = config_cycle_fast_duration;   /* 1000 微秒 = 1ms */
```

循环内部每 16 次迭代查一次表([expire.c:383-390](../../redis-8.0.2/src/expire.c#L383)):

```c
/* expire.c:383-390 */
if ((iteration & 0xf) == 0) {            /* 每 16 轮查一次时间 */
    elapsed = ustime()-start;
    if (elapsed > timelimit) {
        timelimit_exit = 1;              /* 标记:这次是"超时退出"  */
        server.stat_expired_time_cap_reached_count++;
        break;                           /* 立即停,绝不恋战 */
    }
}
```

为什么是"每 16 轮查一次"而不是每轮?因为 `ustime()` 是系统调用,在 hot loop 里调用本身就是开销。每 16 次采样(每次 20 个 key)共查 320 个 key,大致对应几微秒到几十微秒的实际工作量,这个粒度足够精细地控制时间预算,又不会让查表本身成为瓶颈。`iteration & 0xf` 用位与代替模运算,是个老派的常数优化。

### 13.3.2 采样:沿游标随机走桶

采样本身不是全表遍历,而是用 `kvstoreScan` 在 `expires` 字典上**沿游标随机走桶**([expire.c:332](../../redis-8.0.2/src/expire.c#L332)),每个桶里遇到 entry 就交给回调 `expireScanCallback`([expire.c:112](../../redis-8.0.2/src/expire.c#L112)):

```c
/* expire.c:112-125 */
void expireScanCallback(void *privdata, const dictEntry *const_de) {
    dictEntry *de = (dictEntry *)const_de;
    expireScanData *data = privdata;
    long long ttl  = dictGetSignedIntegerVal(de) - data->now;
    if (activeExpireCycleTryExpire(data->db, de, data->now)) {
        data->expired++;                 /* 删成功就计数 */
    }
    if (ttl > 0) {
        data->ttl_sum += ttl;            /* 累积未过期 key 的 TTL,算 avg_ttl */
        data->ttl_samples++;
    }
    data->sampled++;
}
```

`activeExpireCycleTryExpire`([expire.c:38](../../redis-8.0.2/src/expire.c#L38))是真正动手的函数,它取出 entry 里存的过期时间,跟当前 `now` 比,过期了就调 `deleteExpiredKeyAndPropagate` 当场删:

```c
/* expire.c:38-52 */
int activeExpireCycleTryExpire(redisDb *db, dictEntry *de, long long now) {
    long long t = dictGetSignedIntegerVal(de);
    if (now < t) return 0;               /* 还没到点 */
    enterExecutionUnit(1, 0);
    sds key = dictGetKey(de);
    robj *keyobj = createStringObject(key,sdslen(key));
    deleteExpiredKeyAndPropagate(db,keyobj);   /* 走 13.2 节那条统一删除路径 */
    decrRefCount(keyobj);
    exitExecutionUnit();
    postExecutionUnitOperations();        /* 把 DEL 传播给 AOF/副本 */
    return 1;
}
```

### 13.3.3 acceptable_stale 自适应:脏库深耕,干净库跳过

active 过期最聪明的地方,是它的**自适应早停**。看 [expire.c:348](../../redis-8.0.2/src/expire.c#L348) 这一行:

```c
/* expire.c:348 */
repeat = db_done ? 0 : (data.sampled == 0 ||
        (data.expired * 100 / data.sampled) > config_cycle_acceptable_stale);
```

这条语句的语义是:**如果这一轮采样里过期比例 ≤ 10%(`config_cycle_acceptable_stale` 默认 10),说明这个库已经"挺干净了",`repeat=0` 跳出 do-while,`current_db++` 转下一个库;如果过期比例 > 10%,`repeat=1`,本库再扫一轮。** 这是个绝妙的自适应:把宝贵的 25ms 预算花在更脏的库上,而不是均匀撒胡椒面。

```text
过期比例的自适应(可调旋钮 active-expire-effort 影响 config_cycle_acceptable_stale):

  DB 0: 采样 20 个,过期 18 个 (90% > 10%)  → repeat=1,继续扫 DB 0
  DB 0: 采样 20 个,过期 15 个 (75% > 10%)  → repeat=1,继续扫 DB 0
  DB 0: 采样 20 个,过期 2 个  (10% ≤ 10%)  → repeat=0,转 DB 1
  DB 1: 采样 20 个,过期 0 个  (0%  ≤ 10%)  → repeat=0,转 DB 2
  DB 2: 空库,直接跳过                        → current_db++
```

这种"过期密集就深耕、稀疏就换库"的策略,本质是**把 CPU 预算按"回收收益"分配**。一个有亿级 key 但只有几千过期的库,active 周期扫几轮就跳走了;一个突然涌入大量短期 key(比如验证码、session)的库,会被深耕到基本干净。注释 [expire.c:54-57](../../redis-8.0.2/src/expire.c#L54) 开宗明义:"The algorithm used is adaptive and will use few CPU cycles if there are few expiring keys, otherwise it will get more aggressive."

还有一个旋钮:`active-expire-effort`(默认 1,最大 10,见 [config.c:3198](../../redis-8.0.2/src/config.c#L3198))。调大时,`config_keys_per_loop`、`config_cycle_slow_time_perc`、`config_cycle_fast_duration` 都会按比例放大,`config_cycle_acceptable_stale` 会缩小([expire.c:191-200](../../redis-8.0.2/src/expire.c#L191))——用更多 CPU 换更快的回收。这是给"过期压力极大"的业务的逃生通道。

> **钉死这件事**:active 过期 = "限时 + 自适应采样"。限时靠百分比预算(SLOW 25ms / FAST 1ms)+ 每 16 轮查表;自适应靠 `acceptable_stale` 阈值——过期比例 > 10% 就深耕本库,≤ 10% 就换下一个库。两者配合,让 active 周期既能"把脏库扫干净",又"绝不卡死主线程"。

### 13.3.4 SLOW 与 FAST 两档为什么都要

读者可能问:既然 SLOW 周期每 100ms 跑一次、每次 25ms,够用了,为什么还要在 `beforeSleep` 里插一个 FAST 周期(每次 1ms,每轮事件循环都跑)?

答案在于**响应性**。SLOW 周期每 100ms 才跑一次,如果业务在两次 SLOW 之间突然写入大量短期 key(比如秒杀场景瞬时写入 10 万个 1 秒过期的 key),SLOW 周期到来之前的这 100ms,内存可能被这些过期 key 撑爆。FAST 周期就是兜底:**每轮事件循环(几毫秒一次)都插一次 1ms 的快速扫描**,在过期压力上来时第一时间响应。

但 FAST 不是无条件跑的([expire.c:218-231](../../redis-8.0.2/src/expire.c#L218)):

```c
/* expire.c:223-228 */
if (!timelimit_exit &&
    server.stat_expired_stale_perc < config_cycle_acceptable_stale)
    return;   /* 上次 SLOW 没超时 + 全局过期比例低 → 跳过 FAST,省 CPU */

if (start < last_fast_cycle + (long long)config_cycle_fast_duration*2)
    return;   /* 距上次 FAST 不到 2ms → 跳过,避免 FAST 自己变成 busy loop */
```

这两条门禁保证 FAST 不会变成空转烧 CPU 的累赘:**只在"上次 SLOW 超时了"或"全局过期比例高"时才真正跑**,平时 SLOW 周期已经够用。这是典型的"分级响应"——常态下用便宜的 SLOW 兜底,异常时启用更高频的 FAST。

## 13.4 双策略配合:为什么缺一不可

讲到这里,可以把 lazy + active 两套机制的分工说清楚了:

- **lazy 管正确性**:任何读路径上的过期 key 必删,对外语义干净(绝不可能读到过期数据)。
- **active 管内存**:把没人读的过期 key 定量、限时地清掉,保证它们最终被回收。

单独用哪个都不行:

> **不这样会怎样**:① **只用 lazy**:正确,但冷数据过期后永不回收——一个写进去就再没人读的 key,设了 `EXPIRE 60` 也会永远躺在 dict 里,这是内存泄漏。② **只用 active**:能回收,但 25ms 永远扫不完一个亿级 key 的库;更致命的是,**读路径不检查过期**会出现"已过期但还能被读到"的窗口,破坏正确性。比如 active 周期还没扫到某个 key,客户端一次 GET 就把它读出来了——明明它 1 秒前就该消失。

这就是为什么 `expire.c` 文件头注释 [expire.c:15-20](../../redis-8.0.2/src/expire.c#L15) 写得那么直白:"*When keys are accessed they are expired on-access. However we need a mechanism in order to ensure keys are eventually removed when expired even if no access is performed on them.*"——一句话概括双策略。

还有一个细节值得点出:**lazy 和 active 走的是同一个删除入口** `deleteExpiredKeyAndPropagate`([db.c:2169](../../redis-8.0.2/src/db.c#L2169))。这意味着两者删除时的语义完全一致:都发 keyspace 通知、都传播 DEL 给 AOF 和副本、都根据 `lazyfree_lazy_expire` 决定是否走 BIO 异步释放。这种"路径统一"让正确性容易保证——不会因为删除路径不同而出现"lazy 删了通知发了、active 删了通知漏发"这种坑。

## 13.5 evict:触发点与八种策略

淘汰发生在**写命令执行前**。每条命令进来,`processCommand` 会先调 `performEvictions`([evict.c:521](../../redis-8.0.2/src/evict.c#L521))检查是否超 `maxmemory`;超了就按 `maxmemory_policy` 淘汰。策略八种,可归两类:

| 候选范围 | 算法 | 说明 |
|---|---|---|
| `allkeys-lru` / `volatile-lru` | 近似 LRU | 全部 key / 仅带 expire 的 key 里挑最近最少访问 |
| `allkeys-lfu` / `volatile-lfu` | 近似 LFU | 全部 key / 仅带 expire 的 key 里挑访问频率最低 |
| `allkeys-random` / `volatile-random` | 随机 | 完全随机挑一个 |
| `volatile-ttl` | TTL 最短 | 挑最快要过期的(相当于"本来就要删的先删") |
| `noeviction` | 不淘汰 | 内存满了直接拒绝写命令(返回 error) |

`volatile-*` 系列只在设了 expire 的 key 里挑——这适合"缓存场景"(只缓存带 TTL 的临时数据,永久 key 不动);`allkeys-*` 系列在全部 key 里挑——适合"纯缓存"(所有 key 都是缓存,删谁都行)。`noeviction` 是给"数据库场景"的:宁可拒绝写,也不丢数据。

### 13.5.1 为什么不维护精确 LRU

精确 LRU 要给每个 key 配一个双向链表节点:每次 GET/SET 都要把节点挪到表头,淘汰时取表尾。代价有二:① 每次 `lookupKey` 多一次链表指针改写 + 可能的缓存行失效,在高 QPS 下放大成可观的尾延迟;② 每个节点两个指针(16 字节),百万 key 就多 16MB 内存。两者都撞取向④(简单优先)和取向②(省内存)。

Redis 的做法:不维护任何全局结构,只在**需要淘汰时**临时随机抓 `maxmemory-samples` 个 key(默认 **5**,见 [config.c:3184](../../redis-8.0.2/src/config.c#L3184) 的 `createIntConfig("maxmemory-samples", ..., 1, 64, ..., 5, ...)`),挑里面"最该淘汰"的删一个。这就是著名的**采样近似 LRU**。

> **钉死这件事**:精确 LRU 的代价是"每次访问都要动全局链表",在高 QPS 下是不可承受的常数开销。采样近似把开销从"每次访问"挪到"每次淘汰"——淘汰远比访问稀疏(只有内存满时才触发),这个挪移是巨大的净收益。

### 13.5.2 复用 24 位 lru 字段:LRU 与 LFU 的共享家园

省内存的关键在于**复用 `redisObject` 里已有的 24 位 `lru` 字段**([server.h:1004](../../redis-8.0.2/src/server.h#L1004)):

```c
/* server.h:1000-1007 */
typedef struct redisObject {
    unsigned type:4;
    unsigned encoding:4;
    unsigned lru:LRU_BITS;   /* LRU time (relative to global lru_clock) OR
                                LFU data (LFU in the high 16 bits, freq in low 8). */
    int refcount;
    void *ptr;
} robj;
```

`LRU_BITS = 24`([server.h:994](../../redis-8.0.2/src/server.h#L994))。注意字段名虽然叫 `lru`,但注释明说它**同时承载 LRU 和 LFU 两种语义**——靠 `maxmemory_policy` 决定怎么解读。这 24 位是 Redis 唯一为淘汰算法付出的每 key 内存代价:**零额外结构,复用已有字段**。

```text
redisObject 的 24 位 lru 字段,两种解读:

  LRU 模式:全部 24 位存"上次访问的 LRU 时钟值"(毫秒级,会回绕)
  ┌────────────────────────────────┐
  │      24 bits: LRU clock        │
  └────────────────────────────────┘

  LFU 模式:拆成两段——高 16 位存"上次访问时间",低 8 位存"对数频率计数器"
  ┌─────────────────┬──────────────┐
  │  16 bits: LDT   │  8 bits:LOG_C│
  │  (分钟级时间戳) │ (对数频率)   │
  └─────────────────┴──────────────┘
```

这两种解读共用同一块 24 位内存,靠策略位掩码分流——这本身就是"一套字段、多种编码语义"的精神延续(呼应取向③ 编码自适应)。

## 13.6 技巧精解①:LRU 24 位时钟与回绕处理

LRU 模式下,这 24 位存的是"上次访问时的 LRU 时钟值"。LRU 时钟不是真实时间,而是一个**分辨率 1 秒、会回绕的精简时钟**。两个常量定义它([server.h:995-996](../../redis-8.0.2/src/server.h#L995)):

```c
/* server.h:995-996 */
#define LRU_CLOCK_MAX ((1<<LRU_BITS)-1)   /* 2^24 - 1 = 16777215 */
#define LRU_CLOCK_RESOLUTION 1000          /* LRU clock resolution in ms */
```

`LRU_CLOCK_RESOLUTION = 1000`ms 表示时钟每 1 秒跳一次。24 位最大值 `2^24 - 1 = 16777215`,所以时钟值在 0 到 16777215 之间循环。每 1 秒跳一次,跳满一圈需要 `16777216 × 1 秒 ≈ 16777216 秒 ≈ 194.18 天`。也就是说,**LRU 时钟大约每 194 天回绕一次**。

每次访问 key 时(`lookupKey`),Redis 把当前 LRU 时钟写进 `o->lru`:

```c
/* db.c:lookupKey 节选,实际宏 LFU/LRU 分流 */
if (server.maxmemory_policy & MAXMEMORY_FLAG_LFU) {
    updateLFU(o);
} else {
    o->lru = LRU_CLOCK();   /* LRU 模式:记下访问时刻的 LRU 时钟 */
}
```

需要估算"这个 key 空闲多久"时,看 `estimateObjectIdleTime`([evict.c:73](../../redis-8.0.2/src/evict.c#L73)):

```c
/* evict.c:73-81 */
unsigned long long estimateObjectIdleTime(robj *o) {
    unsigned long long lruclock = LRU_CLOCK();
    if (lruclock >= o->lru) {
        return (lruclock - o->lru) * LRU_CLOCK_RESOLUTION;   /* 正常情况 */
    } else {
        return (lruclock + (LRU_CLOCK_MAX - o->lru)) *
                    LRU_CLOCK_RESOLUTION;                     /* 处理回绕 */
    }
}
```

回绕处理的逻辑很直白:如果当前时钟 `lruclock` 比记录的 `o->lru` **小**,说明期间发生过一次回绕(o->lru 是回绕前的大值,lruclock 是回绕后的小值)。这时空闲时间 = `(lruclock + (LRU_CLOCK_MAX - o->lru)) × 分辨率`,等价于"从 o->lru 走到最大值,再从 0 走到 lruclock"的总跨度。

**为什么只处理一次回绕,不处理多次?** 因为 194 天的回绕周期远大于任何合理的空闲时间——一个 key 如果真的 194 天没被访问,它在第一次回绕之前早就被淘汰了。多次回绕需要 key 空闲 388 天以上,这在实际系统里不可能存在(早被 active 过期或 evict 清掉了)。所以"只处理一次回绕"是个务实的简化。

> **钉死这件事**:LRU 24 位时钟约 194 天回绕一圈。回绕检测靠 `lruclock < o->lru` 这一个比较——小了就说明回绕过一次,加一圈 `LRU_CLOCK_MAX` 减旧值即可。这是"用回绕换省内存"的典型工程取舍:精确时间戳要 64 位,Redis 用 24 位 + 回绕处理,省了 40 位/key,百万 key 省下 5MB。

`LRU_CLOCK()`([evict.c:61](../../redis-8.0.2/src/evict.c#L61))还有个小优化:如果事件循环频率够高(`1000/server.hz <= LRU_CLOCK_RESOLUTION`),就直接用 `server.lruclock`(在 `serverCron` 里周期性刷新的全局值),避免每次都调 `mstime()` 系统调用:

```c
/* evict.c:61-69 */
unsigned int LRU_CLOCK(void) {
    unsigned int lruclock;
    if (1000/server.hz <= LRU_CLOCK_RESOLUTION) {   /* 默认 hz=10,1000/10=100 ≤ 1000 ✓ */
        lruclock = server.lruclock;                  /* 用缓存的时钟 */
    } else {
        lruclock = getLRUClock();                    /* 低频时实时算 */
    }
    return lruclock;
}
```

默认 `server.hz = 10`,`1000/10 = 100 ≤ 1000`,走缓存分支。这意味着 `server.lruclock` 每 100ms(`serverCron` 周期)刷新一次,LRU 时间的精度也就是 100ms 级——对"估算空闲时间"这个用途完全够用(谁在乎一个 key 是空闲 10.0 秒还是 10.1 秒?)。

## 13.7 技巧精解②:★LFU 的 Morris-like 对数概率计数器

这是本章最硬、也最值得讲透的部分。LFU(Least Frequently Used,最少频率使用)和 LRU 的区别是:LRU 看"多久没访问"(时间),LFU 看"访问了多少次"(频率)。直觉上,频率计数器应该是个整数——每次访问 +1。但 8 位整数最多记 255 次,一个热点 key 一天被访问几百万次,255 根本不够。

Redis 的解法极其精妙:**用一个概率递增的对数计数器,8 位就能"记"千万次访问。** 核心函数是 `LFULogIncr`([evict.c:282](../../redis-8.0.2/src/evict.c#L282)):

```c
/* evict.c:282-290 */
uint8_t LFULogIncr(uint8_t counter) {
    if (counter == 255) return 255;                        /* 饱和保护 */
    double r = (double)rand()/RAND_MAX;                    /* 掷骰子 r ∈ [0,1) */
    double baseval = counter - LFU_INIT_VAL;               /* LFU_INIT_VAL = 5 */
    if (baseval < 0) baseval = 0;                          /* counter < 5 时 baseval=0 */
    double p = 1.0/(baseval*server.lfu_log_factor+1);      /* 递增概率 */
    if (r < p) counter++;                                  /* 命中才 +1 */
    return counter;
}
```

这里的 `p = 1/(baseval × lfu_log_factor + 1)` 就是 Morris-like 对数计数器的核心。它说的是:**counter 越大,下次访问真正 +1 的概率越低。** 具体地:

- counter 很小时(比如 5),`baseval = 0`,`p = 1/1 = 1.0`——每次访问必 +1。这是为了给新 key 一个"起步加速"(下面 13.7.2 节讲)。
- counter 长大到 50,`lfu_log_factor = 10`(默认),`p = 1/(50×10+1) = 1/501 ≈ 0.2%`——平均 500 次访问才 +1。
- counter 长大到 200,`p = 1/(200×10+1) = 1/2001 ≈ 0.05%`——平均 2000 次访问才 +1。
- counter 到 255,直接饱和,不再增长。

### 13.7.1 数学推导:为什么对数增长能压进 1 字节

这是本节的核心。我们要算清楚:**counter 从 5 涨到 255,平均需要多少次访问?**

设 `lfu_log_factor = λ`(默认 10),`LFU_INIT_VAL = 5`。当 counter 当前值为 `c`(c ≥ 5),`baseval = c - 5`,递增概率 `p(c) = 1/((c-5)·λ + 1)`。从 counter = c 涨到 c+1,期望需要的访问次数是 `1/p(c) = (c-5)·λ + 1`。

所以从 counter = 5 涨到 counter = N(5 ≤ N ≤ 255),**累积期望访问次数**是:

```
E[访问次数] = Σ (c=5 to N-1) 1/p(c)
            = Σ (c=5 to N-1) ((c-5)·λ + 1)
            = Σ (k=0 to N-6) (k·λ + 1)              换元 k = c-5
            = (N-5) + λ · Σ (k=0 to N-6) k
            = (N-5) + λ · (N-5)(N-6)/2
```

把 N = 255、λ = 10 代入:

```
E[访问次数] = (255-5) + 10 · (255-5)(255-6)/2
            = 250 + 10 · 250 · 249/2
            = 250 + 10 · 31125
            = 250 + 311250
            = 311500 次 ≈ 31 万次访问
```

也就是说,**一个 key 平均被访问 31 万次,LFU counter 才会饱和到 255。** 这就是 8 位计数器"记"千万次访问的真相——它不是真的记千万次(那需要 20+ 位),而是用一个**对数概率**把"访问次数"压缩进 8 位的动态范围。`lfu_log-factor` 调到更大(比如 100),饱和点会推到千万级访问;调到更小(比如 1),饱和点降到几千次——用户可以按负载调。

### 13.7.2 反面:线性计数 8 位只能记 256 次

如果不用对数计数,直接用 8 位整数记访问次数,会发生什么?

```text
线性计数(每次访问 +1,8 位饱和在 255):
  counter = 0,1,2,...,255 → 饱和
  能区分的访问次数:0 到 255,共 256 个等级
  一个热点 key 访问 10000 次和访问 100000 次,counter 都是 255,无法区分

对数计数(Morris-like,8 位 + lfu_log_factor=10):
  counter = 0,1,2,...,255 → 饱和
  能区分的访问次数:0 到 ~31 万,共 256 个对数等级
  一个 key 访问 10000 次 counter ≈ 160,访问 100000 次 counter ≈ 230,可区分
```

对数计数的本质是:**用"概率 +1"代替"确定 +1",让 counter 的增长速度随当前值递减。** counter 越大,越难再涨——这恰好匹配"区分冷热"的需求:冷 key(访问少)的 counter 精确增长(每次必 +1),热 key(访问多)的 counter 粗略增长(概率 +1)。在淘汰决策里,我们不需要知道"热点 key 到底被访问了 10 万次还是 20 万次",只需要知道"它比一个访问 1000 次的 key 热得多"——对数计数恰好提供了这个粒度。

这套思路不是 Redis 发明的,它来自 Morris 在 1978 年的论文"Counting Large Numbers of Events in Small Registers"——**用 1 字节近似计数任意大的事件数**,标准名称叫 **Morris 计数器**(Morris counter)。Redis 的变体是加了 `lfu_log_factor` 参数让用户可调"对数底数",以及 `LFU_INIT_VAL` 起步值——这两个微调让 LFU 在 Redis 的实际负载下表现更好。

> **钉死这件事**:LFU 的 8 位 counter 是 Morris-like 对数概率计数器,核心是 `LFULogIncr` 的 `p = 1/(baseval × lfu_log_factor + 1)`。counter 越大,+1 概率越低,8 位能"记"31 万次访问(默认 lfu_log_factor=10)。反面是线性计数 8 位只能记 256 次,无法区分热点。数学本质:把"线性计数"换成"对数计数",用概率换动态范围。

### 13.7.3 LFU 的 24 位复用:16 位时间 + 8 位 counter

回到 13.5.2 节那张图,LFU 模式下 24 位拆成两段:

- **低 8 位 `LOG_C`**:上面讲的对数频率计数器,饱和在 255。
- **高 16 位 `LDT`**(Last access Time in minutes):分钟级时间戳,用于计算衰减。

读取时怎么从 24 位字段里把两段拆出来?看 `LFUDecrAndReturn`([evict.c:302](../../redis-8.0.2/src/evict.c#L302)):

```c
/* evict.c:302-309 */
unsigned long LFUDecrAndReturn(robj *o) {
    unsigned long ldt = o->lru >> 8;              /* 高 16 位:时间戳 */
    unsigned long counter = o->lru & 255;          /* 低 8 位:频率计数器 */
    unsigned long num_periods = server.lfu_decay_time ?
                                LFUTimeElapsed(ldt) / server.lfu_decay_time : 0;
    if (num_periods)
        counter = (num_periods > counter) ? 0 : counter - num_periods;
    return counter;
}
```

`o->lru >> 8` 取高 16 位,`o->lru & 255` 取低 8 位——纯位运算,O(1)。这就是"24 位复用"的全部魔法。

### 13.7.4 LFU 惰性衰减:读时算衰减,但不落盘

`LFUDecrAndReturn` 的名字里有个 "Decr"(decrement,衰减),但它**不真的修改 `o->lru`**——它只是在读取时**算出"考虑衰减后的当前 counter 值"并返回**,不把衰减结果写回。这个"只读不写"的设计非常关键,看注释 [evict.c:292-296](../../redis-8.0.2/src/evict.c#L292):

```text
If the object's ldt (last access time) is reached, decrement the LFU counter but
do not update LFU fields of the object, we update the access time
and counter in an explicit way when the object is really accessed.
```

为什么不落盘?两个理由:

1. **避免 COW(Copy-On-Write)脏页**:Redis 在 RDB 持久化时会 fork 子进程(第十四章),父子进程共享内存页。如果 `LFUDecrAndReturn` 每次都写回 `o->lru`,就会触发 COW 把共享页复制一份——仅仅为了记一个衰减值,就让内存翻倍,不可接受。惰性衰减保证"只有真正访问 key 时才改 `o->lru`",把 COW 脏页降到最低。这个理由和第十四章 RDB 的 `bgsave` 紧密呼应。

2. **避免无效写入**:淘汰扫描时会遍历大量 key,每个都调 `LFUDecrAndReturn` 算 idle 值。如果每个都写回,会产生大量"扫一遍就把所有 key 的 counter 都衰减了"的副作用——而这些 key 可能根本不会被淘汰(只是路过算一下)。惰性衰减让"算衰减"和"真访问"解耦:算的时候只读,真访问时(`updateLFU`)才一次性更新时间戳和 counter。

衰减的速率由 `lfu-decay-time`(默认 1 分钟,见 [config.c:3181](../../redis-8.0.2/src/config.c#L3181))控制:每过 `lfu_decay_time` 分钟,counter 逻辑上 -1。配合 16 位时间戳 `LDT`,可以算出"距上次访问过了多少分钟",再除以 `lfu_decay_time` 得到衰减量。16 位分钟时间戳也会回绕(`LFUTimeElapsed` 处理一次回绕,逻辑和 13.6 节 LRU 时钟回绕对称,见 [evict.c:274](../../redis-8.0.2/src/evict.c#L274)):

```c
/* evict.c:274-278 */
unsigned long LFUTimeElapsed(unsigned long ldt) {
    unsigned long now = LFUGetTimeInMinutes();
    if (now >= ldt) return now-ldt;                       /* 正常 */
    return 65535-ldt+now;                                  /* 回绕一次 */
}
```

16 位分钟时间戳回绕周期 = `65536 分钟 ≈ 45.5 天`。比 LRU 的 194 天短,但同样远大于任何合理的"无访问"时长。

> **钉死这件事**:LFU 衰减是**惰性的**——`LFUDecrAndReturn` 读取时算出衰减后的 counter 但不写回 `o->lru`,只有真正访问 key 时(`updateLFU`)才更新。这样设计是为了避免 COW 脏页(fork 子进程做 RDB 时共享页不会被无谓复制),呼应第十四章 RDB 的 `bgsave`。16 位分钟时间戳 45.5 天回绕一次,`LFUTimeElapsed` 处理一次回绕,逻辑和 LRU 时钟回绕对称。

### 13.7.5 LFU_INIT_VAL:新 key 不从 0 开始

还有一个细节容易忽略:新 key 的 counter **不从 0 开始,而从 `LFU_INIT_VAL = 5`**([server.h:3759](../../redis-8.0.2/src/server.h#L3759))起步。注释 [evict.c:252-257](../../redis-8.0.2/src/evict.c#L252) 解释得很清楚:新 key 如果从 0 开始,它一写进来 counter=0,马上就是"频率最低"的,第一次淘汰扫描就可能被误删——根本没机会积累访问记录。从 5 开始,给它一个"5 次访问的缓冲",在这 5 次访问里 counter 每次必 +1(`baseval = counter - 5 < 0` 时 `baseval = 0`,`p = 1/(0+1) = 100%`),快速涨到 6、7、8……过了 5 之后才开始对数概率递增。

这个 `LFU_INIT_VAL` 是个"冷启动保护":新写入的 key 不会因为"还没来得及被访问"就被淘汰,给了它积累访问记录的机会。

## 13.8 技巧精解③:淘汰池——采样近似的"记忆"

只采 5 个就挑一个,信息量太小,可能"运气差"淘汰了好 key。Redis 加了个**常驻淘汰池** `EvictionPoolLRU`([evict.c:44](../../redis-8.0.2/src/evict.c#L44)),大小 `EVPOOL_SIZE = 16`([evict.c:34](../../redis-8.0.2/src/evict.c#L34)):

```c
/* evict.c:34-42 */
#define EVPOOL_SIZE 16
#define EVPOOL_CACHED_SDS_SIZE 255
struct evictionPoolEntry {
    unsigned long long idle;    /* idle 大的排右边(LFU 用反向频率) */
    sds key;                    /* key 名 */
    sds cached;                 /* 预分配的 255 字节 SDS,短 key 零 malloc */
    int dbid;                   /* key 所在 DB */
    int slot;                   /* key 所在 slot */
};
static struct evictionPoolEntry *EvictionPoolLRU;
```

### 13.8.1 池子的两个关键设计

**设计一:常驻 + 跨调用保留。** 池子是个 **static 全局变量**,不是每次淘汰新建的。每次 `evictionPoolPopulate`([evict.c:126](../../redis-8.0.2/src/evict.c#L126))采样 5 个 key,把它们**按 idle 升序**塞进池子(左边 idle 小=刚访问,右边 idle 大=最该淘汰)。池子满 16 个时,新进来的要把 idle 最小的挤出去。真正要淘汰时,从池子**右端取一个**([evict.c:604](../../redis-8.0.2/src/evict.c#L604)):

```c
/* evict.c:604-630,精简 */
for (k = EVPOOL_SIZE-1; k >= 0; k--) {     /* 从最该淘汰的往左找 */
    if (pool[k].key == NULL) continue;
    bestdbid = pool[k].dbid;
    /* ... 校验 key 还在(池里可能有"幽灵"已被删的 key) ... */
    de = kvstoreDictFind(kvs, pool[k].slot, pool[k].key);
    /* 从池中移除这个 entry */
    if (pool[k].key != pool[k].cached) sdsfree(pool[k].key);
    pool[k].key = NULL;
    pool[k].idle = 0;
    if (de) { bestkey = dictGetKey(de); break; }   /* 找到了 */
    /* 否则是幽灵,继续往左找 */
}
```

池子的价值在于**累积**:单次采样 5 个的瞬时信息量很小,但池子是个"滚动 Top-K",跨多次淘汰调用沉淀的是历史采样中 idle 最大的 16 个。要淘汰时从这 16 个里挑,等价于在一个**有偏的样本流**上做近似——效果远好于"每次重新采 5 个挑一个"。

**设计二:255 字节预分配 cached SDS,短 key 零 malloc。** 注意池子结构里有个 `cached` 字段,预分配了 255 字节([evict.c:112](../../redis-8.0.2/src/evict.c#L112))。塞 key 进池子时,如果 key 名 ≤ 255 字节,直接 `memcpy` 进 `cached`,不分配新内存([evict.c:212-219](../../redis-8.0.2/src/evict.c#L212)):

```c
/* evict.c:212-219 */
int klen = sdslen(key);
if (klen > EVPOOL_CACHED_SDS_SIZE) {
    pool[k].key = sdsdup(key);                       /* 长 key:分配新 sds */
} else {
    memcpy(pool[k].cached,key,klen+1);               /* 短 key:memcpy 进预分配 */
    sdssetlen(pool[k].cached,klen);
    pool[k].key = pool[k].cached;                    /* key 指向 cached */
}
```

为什么 255?因为 Redis 绝大多数 key 名都短(userId、product:123 之类几十字节),255 覆盖了 99%+ 的情况。这个预分配让"塞 key进池子"这个高频操作**完全避开了 malloc/free**——作者在注释 [evict.c:208-211](../../redis-8.0.2/src/evict.c#L208) 直白地写:"allocating and deallocating this object is costly (according to the profiler, **not my fantasy**. Remember: premature optimization bla bla bla."——"这是 profiler 跑出来的,不是我幻想的。记住:过早优化 blah blah blah(反讽那些教条主义者)。"

### 13.8.2 插入逻辑:维护升序的代价

`evictionPoolPopulate` 的插入逻辑([evict.c:174-206](../../redis-8.0.2/src/evict.c#L174))是池子最精巧的部分。它要维护"idle 升序"这个不变量,同时处理"池满"的情况。简化后逻辑是:

1. 找到第一个 `idle >= 当前 key 的 idle` 的位置 k。
2. 如果 k=0 且池子最右端非空(满了),说明当前 key 比池里所有人都冷,直接丢弃。
3. 如果 k 位置是空的,直接插进去。
4. 如果 k 位置非空,需要在 k 处插入,把后面的元素右移(`memmove`);如果最右端也满了,就把最左端(idle 最小)的挤出去。

这个 `memmove` 看起来 O(N)(N=16),但 N 是常数 16,实际是 16 次 `evictionPoolEntry` 结构体的内存搬移,几十纳秒级。相比之下,省下的"每次采样都重新挑"的开销,远远盖过这个 memmove。

> **钉死这件事**:淘汰池的两个关键设计——① 常驻 16 个、跨调用保留(滚动 Top-K,把多次采样的累积信息沉淀下来,避免单次采样运气差);② 255 字节预分配 cached SDS(短 key memcpy 零 malloc,这是 profiler 跑出来的真实优化,不是作者的"幻想")。这两个设计让"采样近似 LRU"的质量逼近精确 LRU,代价只是一个 16 槽的 static 数组。

## 13.9 idle 值的三种算法:LRU / LFU / TTL

idle 值(在池子里排升序)怎么算,取决于策略,全在 `evictionPoolPopulate`([evict.c:153-169](../../redis-8.0.2/src/evict.c#L153)):

```c
/* evict.c:153-169 */
if (server.maxmemory_policy & MAXMEMORY_FLAG_LRU) {
    idle = estimateObjectIdleTime(o);                      /* LRU:空闲越久越该淘汰 */
} else if (server.maxmemory_policy & MAXMEMORY_FLAG_LFU) {
    idle = 255 - LFUDecrAndReturn(o);                      /* LFU:频率越低越该淘汰 */
} else if (server.maxmemory_policy == MAXMEMORY_VOLATILE_TTL) {
    idle = ULLONG_MAX - dictGetSignedIntegerVal(de);       /* TTL 越短越该淘汰 */
} else {
    serverPanic("Unknown eviction policy");
}
```

三种策略统一映射到"idle 越大越该淘汰"这个语义:

- **LRU**:`idle = 空闲时间`(空闲越久,越该淘汰)。
- **LFU**:`idle = 255 - 频率`(频率越低,255-频率越大,idle 越大,越该淘汰)。这是个**取反映射**——LFU 本意是"频率越低越该淘汰",但池子是按 idle 升序排、从右端取的,所以取个反让低频 key 的 idle 变大,自然沉到右端。**一个减法统一了两种策略的排序方向**,极简。
- **volatile-ttl**:`idle = ULLONG_MAX - TTL`(TTL 越短,越快过期,`ULLONG_MAX - TTL` 越大,越该淘汰)。同样用减法把"TTL 越短越该淘汰"映射到"idle 越大越该淘汰"。

这三行代码的精妙在于:**用同一个 idle 字段和同一个升序池子,承载了三种完全不同的策略语义。** 不需要三个池子、不需要三套插入逻辑,只需要在算 idle 时做一次映射。这是"统一接口 + 策略分流"的典范。

## 13.10 evict 的时间预算与异步接力

`performEvictions` 同样遵循"绝不长时间霸占主线程"。它有一个 `maxmemory-eviction-tenacity`(默认 10,见 [config.c:3185](../../redis-8.0.2/src/config.c#L3185))折算出的时间上限 `evictionTimeLimitUs`([evict.c:480](../../redis-8.0.2/src/evict.c#L480)):

```c
/* evict.c:480-495 */
static unsigned long evictionTimeLimitUs(void) {
    if (server.maxmemory_eviction_tenacity <= 10) {
        return 50uL * server.maxmemory_eviction_tenacity;   /* 0..10 → 0..500us 线性 */
    }
    if (server.maxmemory_eviction_tenacity < 100) {
        /* 11..99 → 15% 几何增长,99 时约 2 分钟 */
        return (unsigned long)(500.0 * pow(1.15, server.maxmemory_eviction_tenacity - 10.0));
    }
    return ULONG_MAX;   /* 100 → 无上限 */
}
```

注意 tenacity 的折算是**分段**的:0-10 线性(0 到 500μs,温和),11-99 几何增长(15% 复利,99 时约 2 分钟,激进),100 无上限(交给用户全权决定)。这种分段让默认值(tenacity=10)对应 500μs 的温和上限,而调大到 99 可以让 Redis 在"宁可卡一会也要删够"的场景下激进回收。

每删 16 个 key 检查一次时间([evict.c:676-704](../../redis-8.0.2/src/evict.c#L676)):

```c
/* evict.c:676-704,精简 */
if (keys_freed % 16 == 0) {
    if (slaves) flushSlavesOutputBuffers();           /* 顺便给副本刷缓冲 */

    if (server.lazyfree_lazy_eviction) {
        if (getMaxmemoryState(NULL,NULL,NULL,NULL) == C_OK) break;  /* 异步删可能已释放 */
    }

    if (elapsedUs(evictionTimer) > eviction_time_limit_us) {
        startEvictionTimeProc();    /* 还没删够?挂个时间事件,下轮接着删 */
        break;
    }
}
```

这段代码藏着三个细节:

1. **`keys_freed % 16 == 0`**:和 active expire 的 `iteration & 0xf` 一样,每 16 次查一次时间,平衡时间检查开销和控制精度。
2. **`flushSlavesOutputBuffers`**:删 key 会产生 DEL 命令要复制给副本,这里主动刷一下副本缓冲,避免副本延迟积累。
3. **`lazyfree_lazy_eviction` 时重新查内存**:因为异步删除(BIO 后台线程)可能已经释放了内存,主线程不能只看自己同步删了多少,要重新查实际内存占用。

`startEvictionTimeProc`([evict.c:452](../../redis-8.0.2/src/evict.c#L452))注册一个 `evictionTimeProc`,让事件循环在空闲时**继续**分批淘汰,直到内存达标或无可淘汰。这就是 `EVICT_RUNNING` 状态的含义:本次没删完,但已把主线程让出来,后续事件循环接力。`performEvictions` 的返回值语义([evict.c:516-520](../../redis-8.0.2/src/evict.c#L516))也对应这三态:`EVICT_OK`(内存正常)、`EVICT_RUNNING`(正在异步接力淘汰)、`EVICT_FAIL`(删不动了,只能拒绝写)。

> **钉死这件事**:evict 的时间预算靠 `maxmemory-eviction-tenacity` 折算(分段:0-10 线性、11-99 几何增长、100 无上限),每删 16 个 key 查一次。超时就 `startEvictionTimeProc` 挂时间事件接力,主线程让出来。返回 `EVICT_RUNNING` 告诉上层"我在后台删,别急"。这是取向①"耗时化整为零"在淘汰路径的完美落地。

## 13.11 取舍放送:采样数为什么是 5

这一节专门讲一个常被问的取舍:**`maxmemory-samples` 默认为什么是 5?**

直觉上"随机抓 5 个挑最差的"听起来很糙,但配合淘汰池之后效果惊人。池子是个跨调用的"滚动 Top-K":每次喂 5 个新候选,长期看池子里沉淀的是历史采样中 idle 最大的 16 个。要淘汰时从这 16 个里挑,等价于在一个**有偏的样本流**上做近似。

Redis 作者实测过(见官方文档):采样数从 5 提到 10,近似质量接近精确 LRU(差异在个位数百分比);但 CPU 开销也线性增长。作者最终默认给 5,是典型的"够用就好、把 CPU 留给命令"的取向④取舍:

| `maxmemory-samples` | 近似质量(对比精确 LRU) | 每次 evict 的 CPU 开销 |
|---|---|---|
| 1 | 差(基本等于 random) | 最低 |
| **5(默认)** | **接近精确 LRU,差异 < 10%** | **低** |
| 10 | 几乎等同精确 LRU | 中 |
| 64(上限) | 等同精确 LRU | 高 |

`maxmemory-samples` 可在 1-64 间调([config.c:3184](../../redis-8.0.2/src/config.c#L3184)),让用户自己按延迟/质量权衡。**默认 5 是"绝大多数场景下质量够用、CPU 占用低"的 sweet spot。** 如果你的业务对缓存命中率极度敏感(比如命中率从 95% 掉到 90% 就是真金白银的损失),可以调到 10;如果追求极致吞吐且能容忍略低的命中率,可以降到 3。

> **钉死这件事**:采样数默认 5,是"近似质量 vs CPU 开销"的 sweet spot。配合淘汰池(跨调用累积),5 个采样的近似质量已经接近精确 LRU。`maxmemory-samples` 是可调旋钮(1-64),让用户按业务负载自己权衡。这个默认值不是拍脑袋,是作者用真实 benchmark 跑出来的。

## 13.12 LRU vs LFU 怎么选

两种近似算法各有适用场景:

- **LRU**(最近最少访问)适合**时间局部性强**的负载:热点会反复被碰,冷数据沉底。典型场景是"用户会话缓存"——活跃用户的 session 反复被访问,不活跃的自然淘汰。缺点是对"扫描式访问"敏感——一次全表 `SCAN` 或 `KEYS` 会把所有被扫到的 key 的 lru 字段刷新,让它们"看起来最近被访问过",把真正的热点挤到淘汰池左端。
- **LFU**(访问频率最低)按频率而非时间,对扫描更鲁棒(扫一次只让 counter 概率 +1,频率排序几乎不动);配合衰减,也能适应热点迁移。代价是计数器有概率噪声,参数(`lfu-log-factor`/`lfu-decay-time`)需要根据负载调。

经验法则:**缓存命中模式稳定、有明显冷热分层用 LRU;访问模式多变、有周期性扫描用 LFU。** Redis 4.0 引入 LFU,正是因为很多用户反馈 LRU 在"扫描型负载"下表现差——LFU 是对这类场景的补丁。

## 章末:回扣、五个为什么、往哪钻

### 主线回扣

这一章是**取向①(单线程 + 事件循环,耗时化整为零)的集大成**。过期的全库扫描被拆成"lazy 访问时顺手删 + active 25ms 限量扫";淘汰的"必须立刻删够"被拆成"本轮删一点 + 时间到就挂时间事件接力"。两个机制都用 `server.hz` 心跳和 `beforeSleep` 作为节拍器,把大任务切片塞进事件循环的缝隙。

同时它也是其他几条取向的交汇:

- **取向②(内存即数据库)**:过期 key 不主动清就是泄漏,所以必须有 active;淘汰是为了在有限内存里维持服务,采样近似用最少的额外结构(就一个 16 槽的池子 + 每个对象复用 24 位 lru 字段)换最大的回收效率。
- **取向③(编码自适应)**:LRU/LFU 复用同一个 24 位字段、靠 policy 位掩码分流,本身就是"一套字段、多种编码语义"的精神延续。
- **取向④(简单优先)**:不做精确 LRU、不维护链表、复用 `redisObject.lru`、LFU 用 8 位 Morris 对数计数器而非 64 位精确计数——每一步都在用"够用的近似"换"更少的代码和内存"。
- **取向⑤(可靠性)**:淘汰时主动剔除副本/AOF 缓冲的内存(`freeMemoryGetNotCountedMemory`,[evict.c:319](../../redis-8.0.2/src/evict.c#L319)),避免"为了给副本发 DEL 而触发更多淘汰"的反馈环;`noeviction` 策略在删不动时**拒绝写**而非默默丢数据,把选择权交给上层。

### 五个为什么

**Q1:active expire 的 25ms 时间预算,会不会让主线程每 100ms 卡 25ms?**

会有一部分延迟,但这是"可控的、可调的"。25ms 是上限(`config_cycle_slow_time_perc=25%` × 1s / `hz=10`),常态下 active 周期扫到 `acceptable_stale` 阈值就提前退出,远用不到 25ms。只有过期压力极大时才会逼近上限。如果业务对延迟极度敏感,可以调小 `active-expire-effort`(但默认已经是 1,最小值),或者调高 `server.hz`(让每次干更少的活、但更频繁)。反过来,如果可以接受更高延迟换更快回收,调大 `active-expire-effort` 到 10,时间预算会翻几倍。

**Q2:LFU 的 Morris 计数器,counter=160 到底代表访问了多少次?**

不能精确反推——Morris 计数器是**概率近似**,counter=160 对应的"期望访问次数"是个范围,不是确定值。但可以算期望:用 13.7.1 节的公式,`lfu_log_factor=10`、`counter` 从 5 涨到 160,期望访问次数 ≈ `(160-5) + 10·(160-5)(160-6)/2 = 155 + 10·119663 ≈ 120 万次`。所以 counter=160 大致代表"百万级访问"。这就是对数计数的特点:**不精确,但能区分数量级**——counter=160(百万级)和 counter=230(更高级别)的差距,远大于线性计数下 160 和 161 的差距。

**Q3:淘汰池里为什么会有"幽灵 key"(已被删的 key)?**

因为池子是个 static 数组,跨调用保留。一个 key 被塞进池子后,它可能在别处被删了(lazy 过期、`DEL` 命令、另一个 DB 的淘汰),但池子里的 entry 不会主动同步。所以从池子取 key 时,要重新 `kvstoreDictFind` 校验它还在([evict.c:614-629](../../redis-8.0.2/src/evict.c#L614))。如果是幽灵,跳过继续往左找。这个设计简化了池子维护(不用监听所有删除事件),代价是池子里可能有一些无效 entry——但最多 16 个,影响可忽略。

**Q4:LRU 24 位时钟 194 天回绕,真的不会出问题吗?**

实践上不会。一个 key 如果真的 194 天没被访问,它在第一次回绕之前早就被淘汰了(active 过期或 evict)。回绕处理的 `clock < o->lru` 只处理一次回绕——多次回绕需要 key 空闲 388 天以上,这在实际系统里不存在。所以"只处理一次回绕"是务实的简化,**为不存在的场景提前优化是过度设计**(这句话在第二章 ae 的"无序链表 vs 最小堆"里也出现过,是 Redis 一以贯之的哲学)。

**Q5:为什么 active expire 和 evict 都用"每 16 次查一次时间"?**

这是个常数优化。`ustime()`/`elapsedUs()` 是系统调用或读高精度时钟,在 hot loop 里每次都调会产生可观开销。每 16 次查一次,意味着每 16×20=320 个 key(active expire)或 16 个 key(evict)才查一次,把时间检查的开销摊薄到可忽略。`iteration & 0xf`(位与)比 `iteration % 16`(模运算)快几条指令——老派 C 程序员的常数优化。16 这个数是经验值:太小(比如 4)时间控制不够精细,太大(比如 64)单次时间检查间隔太长可能超预算。

### 想继续深入往哪钻

- 想看 lazy 过期的完整路径:读 [db.c](../../redis-8.0.2/src/db.c) 的 `expireIfNeeded` → `keyIsExpired` → `deleteExpiredKeyAndPropagate`,以及 `lookupKey` 里对所有读命令的拦截。
- 想理解 `lazyfree` 异步删除怎么和 BIO 后台线程配合:读 [lazyfree.c](../../redis-8.0.2/src/lazyfree.c) 和 [bio.c](../../redis-8.0.2/src/bio.c),这是第十九章的主线。重点看 `lazyfree_lazy_expire` 和 `lazyfree_lazy_eviction` 两个开关如何让大对象的释放走 BIO 线程。
- 想看 `kvstoreScan` 怎么在 dict 上沿游标随机走桶:读 [dict.c](../../redis-8.0.2/src/dict.c) 的 `dictScan` 实现,以及 `kvstore.c` 的 `kvstoreScan` 封装。注意游标是无状态的(每次返回下次的起点),这让 active expire 可以跨调用增量扫描。
- 想了解 Morris 计数器的理论:读 Robert Morris 1978 年的原始论文 "Counting Large Numbers of Events in Small Registers"。Redis 的 LFU 是它的工程化变体,加了 `lfu_log_factor` 可调参数和 `LFU_INIT_VAL` 起步值。
- 想对比"精确 LRU"怎么实现:看 Redis 早期版本(2.x)或其他缓存系统(Guava Cache、Caffeine)的 LRU 实现,它们维护 W-TinyLFU 或双向链表,代价是每次访问都要动链表。Redis 的近似 LRU 是对这条路的有意回避。

### 引出下一章

过期和淘汰都是"运行时如何让内存干净",但内存里的数据终究会随断电消失。**持久化**是另一条线:RDB 把某一时刻的内存快照写盘,AOF 把每条写命令追加。下一章我们从 **RDB** 讲起——你会看到,RDB 的 `bgsave` 同样离不开取向①:它 fork 子进程做 COPY-ON-WRITE 快照,父进程继续服务;而本章的 `lazyfree` 异步删除、`evictionTimeProc` 分批淘汰,正是同一思想在"内存治理"侧的投影。**特别地,LFU 的惰性衰减(13.7.4 节)为了避免 COW 脏页而设计——这个设计在 RDB 的 `bgsave` 期间才会显现其全部价值。** 从"化整为零地删"到"化整为零地存",思路一脉相承。

---

## 验证物:如何亲手确认本章的设计

> 说明:本书写作环境为 Windows,无法直接运行 redis-server(8.0 依赖 fork/epoll 等 Linux 特性)。以下 (1) gdb 断点脚本 (2) 源码常量锚点 (3) OBJECT ENCODING / INFO stats 观察项 均为可复现的精确指引,供读者在 Linux 环境对 redis-8.0.2 源码 `make no-opt` 编译后自行验证。**本书不附编造的运行输出**——凡未实跑的,只给脚本、预期观察变量与推导依据,不写具体数值。

### 1. gdb 断点脚本

编译:`cd redis-8.0.2 && make no-opt`
启动:`gdb ./src/redis-server`,另一终端 `redis-cli`。

```gdb
# === active expire 路径 ===
(gdb) break activeExpireCycle        # 主入口,expire.c:187
(gdb) break activeExpireCycleTryExpire  # 当场删过期 key,expire.c:38
(gdb) break expireScanCallback       # 采样回调,expire.c:112
(gdb) break expire.c:247             # timelimit 计算(SLOW 模式)
(gdb) break expire.c:348             # acceptable_stale 自适应判断

# === evict 路径 ===
(gdb) break performEvictions         # 淘汰主入口,evict.c:521
(gdb) break evictionPoolPopulate     # 填淘汰池,evict.c:126
(gdb) break estimateObjectIdleTime   # LRU 空闲时间,evict.c:73
(gdb) break LFULogIncr               # LFU 概率递增,evict.c:282
(gdb) break LFUDecrAndReturn         # LFU 衰减读取,evict.c:302
(gdb) break evictionTimeLimitUs      # 淘汰时间预算,evict.c:480

(gdb) run --port 6379

# === 场景一:观察 active expire 的时间预算 ===
# redis-cli 批量写入带 expire 的 key:
#   for i in $(seq 1 10000); do redis-cli SET k:$i v EX 1; done
# 然后等待 1 秒,key 全部过期,gdb 在 activeExpireCycle 停下:
(gdb) print type                     # 预期:0(FAST) 或 1(SLOW)
(gdb) print timelimit                # 预期:SLOW=25000(μs), Fast=1000(μs)
(gdb) print config_keys_per_loop     # 预期:20(effort=1 时)
(gdb) print config_cycle_acceptable_stale  # 预期:10(effort=1 时)
(gdb) print server.hz                # 预期:10(默认)
# 单步到 expire.c:348,观察自适应:
(gdb) print data.expired             # 预期:本轮删的过期 key 数
(gdb) print data.sampled             # 预期:本轮采样的 key 数(≤20)
(gdb) print data.expired * 100 / data.sampled  # 过期比例,决定 repeat

# === 场景二:观察 LFU 的概率递增 ===
# redis-cli:CONFIG SET maxmemory-policy allkeys-lfu
# redis-cli:SET hotkey v   (写一次,counter 从 LFU_INIT_VAL=5 开始)
(gdb) break LFULogIncr
(gdb) continue
# 每次访问 hotkey(GET hotkey)都会命中 LFULogIncr:
(gdb) print counter                  # 当前 counter 值
(gdb) print server.lfu_log_factor    # 预期:10(默认)
(gdb) print p                        # 本次递增概率 = 1/(baseval*10+1)
# 多次 GET 后,counter 会从 5 缓慢上涨,但越涨越慢(Morris 对数特性)

# === 场景三:观察淘汰池的累积 ===
# redis-cli:CONFIG SET maxmemory 1mb
# redis-cli:CONFIG SET maxmemory-policy allkeys-lru
# 批量写入超过 1mb 的 key 触发淘汰:
#   for i in $(seq 1 1000); do redis-cli SET k:$i $(head -c 1024 /dev/urandom | base64); done
(gdb) break evictionPoolPopulate
(gdb) continue
(gdb) print pool[15].idle            # 池子最右端(idle 最大)的值
(gdb) print pool[15].key             # 池子最右端的 key 名
(gdb) print pool[0].idle             # 池子最左端(idle 最小)的值
# 多次命中后,池子应该被填满(16 个非 NULL entry),idle 从左到右递增
```

**预期观察**(基于源码,本书未实跑):

- `activeExpireCycle` 的 `timelimit` 在 SLOW 模式下应为 25000μs(effort=1、hz=10 时),FAST 模式为 1000μs。
- `LFULogIncr` 的 `p` 值随 `counter` 增大而减小(counter=5 时 p=1.0 必增,counter=50 时 p≈0.2%,counter=200 时 p≈0.05%)。
- 淘汰池在持续淘汰下应被填满,`pool[15].idle > pool[0].idle`(升序排列)。

### 2. 源码常量锚点(带行号,从 redis-8.0.2 源码 Grep 核实)

| 常量/字段 | 位置 | 值/说明 |
|----------|------|---------|
| `LRU_BITS` | server.h:994 | 24(LRU/LFU 共用字段位数) |
| `LRU_CLOCK_MAX` | server.h:995 | `2^24-1 = 16777215`(24 位最大值,194 天回绕) |
| `LRU_CLOCK_RESOLUTION` | server.h:996 | 1000(ms,LRU 时钟分辨率) |
| `LFU_INIT_VAL` | server.h:3759 | 5(LFU 新 key 起步值) |
| `redisObject.lru` | server.h:1004 | `unsigned lru:LRU_BITS`(24 位复用字段) |
| `ACTIVE_EXPIRE_CYCLE_KEYS_PER_LOOP` | expire.c:94 | 20(每轮每库采样数) |
| `ACTIVE_EXPIRE_CYCLE_FAST_DURATION` | expire.c:95 | 1000(μs,FAST 模式上限) |
| `ACTIVE_EXPIRE_CYCLE_SLOW_TIME_PERC` | expire.c:96 | 25(% CPU 上限) |
| `ACTIVE_EXPIRE_CYCLE_ACCEPTABLE_STALE` | expire.c:97 | 10(% 过期比例阈值) |
| `activeExpireCycle` 时间预算 | expire.c:247 | `timelimit = perc*1000000/hz/100` |
| `acceptable_stale` 自适应 | expire.c:348 | `repeat = ... (expired*100/sampled) > threshold` |
| `EVPOOL_SIZE` | evict.c:34 | 16(淘汰池大小) |
| `EVPOOL_CACHED_SDS_SIZE` | evict.c:35 | 255(预分配 SDS,短 key 零 malloc) |
| `LFULogIncr` 概率公式 | evict.c:287 | `p = 1.0/(baseval*lfu_log_factor+1)` |
| `LFUDecrAndReturn` 24 位拆分 | evict.c:303-304 | `ldt = lru>>8`,`counter = lru&255` |
| `LFUTimeElapsed` 回绕处理 | evict.c:274-278 | 16 位分钟时钟,45.5 天回绕 |
| `estimateObjectIdleTime` 回绕 | evict.c:73-81 | 24 位 LRU 时钟,194 天回绕 |
| `LRU_CLOCK` 缓存优化 | evict.c:61-69 | `1000/hz <= 分辨率` 时用 `server.lruclock` |
| `evictionTimeLimitUs` 分段 | evict.c:480-495 | 0-10 线性、11-99 几何、100 无上限 |
| `performEvictions` 16-key 检查 | evict.c:676 | `keys_freed % 16 == 0` |
| `maxmemory-samples` 默认 | config.c:3184 | 5(范围 1-64) |
| `maxmemory-eviction-tenacity` 默认 | config.c:3185 | 10(范围 0-100) |
| `lfu-log-factor` 默认 | config.c:3180 | 10 |
| `lfu-decay-time` 默认 | config.c:3181 | 1(分钟) |
| `active-expire-effort` 默认 | config.c:3198 | 1(范围 1-10) |
| `lazyfree-lazy-expire` 默认 | config.c:3081 | 0(关,大 key 同步删) |

### 3. OBJECT ENCODING / INFO stats 观察项(需本地 redis-server)

> 以下操作需在 Linux 本地启动 redis-server 后用 redis-cli 执行。本书未实跑,仅列观察方法与预期(参数来自 config.c 默认值,可 `CONFIG GET` 确认)。

```text
# === 观察一:active expire 的统计(INFO stats) ===
127.0.0.1:6379> CONFIG GET hz                  # 预期:10
127.0.0.1:6379> CONFIG GET active-expire-effort # 预期:1

# 写入 1 万个 1 秒过期的 key:
# for i in $(seq 1 10000); do redis-cli SET k:$i v EX 1; done
# 等待 2 秒让 key 全部过期,然后查统计:
127.0.0.1:6379> INFO stats
# 预期关注字段(基于 active expire 行为):
#   expired_keys:10000                (本次过期清理的 key 数)
#   expired_time_cap_reached_count:N  (active 周期超时退出次数,N≥0)

# === 观察二:LFU counter 的对数增长(OBJECT FREQ / OBJECT ENCODING) ===
127.0.0.1:6379> CONFIG SET maxmemory-policy allkeys-lfu
127.0.0.1:6379> SET hotkey v
127.0.0.1:6379> OBJECT FREQ hotkey             # 预期:5(LFU_INIT_VAL)
# 访问 100 次:
# for i in $(seq 1 100); do redis-cli GET hotkey; done
127.0.0.1:6379> OBJECT FREQ hotkey             # 预期:>5 但 <<100(对数增长,远小于访问次数)
# 访问 1000 次:
# for i in $(seq 1 1000); do redis-cli GET hotkey; done
127.0.0.1:6379> OBJECT FREQ hotkey             # 预期:缓慢上涨(可能 50-100,远小于 1100)
# 观察:counter 增长速度随当前值递减,这是 Morris 对数计数器的特征

# === 观察三:LRU 空闲时间(OBJECT IDLETIME) ===
127.0.0.1:6379> CONFIG SET maxmemory-policy allkeys-lru
127.0.0.1:6379> SET idle_test v
127.0.0.1:6379> OBJECT IDLETIME idle_test      # 预期:0(刚写入)
# 等待 10 秒后:
127.0.0.1:6379> OBJECT IDLETIME idle_test      # 预期:≈10(秒,精度受 LRU_CLOCK_RESOLUTION 影响)
# OBJECT IDLETIME 读的是 24 位 lru 字段算空闲时间,不会刷新 lru(不算"访问")

# === 观察四:淘汰池累积的间接证据 ===
127.0.0.1:6379> CONFIG SET maxmemory 10mb
127.0.0.1:6379> CONFIG SET maxmemory-policy allkeys-lru
127.0.0.1:6379> CONFIG SET maxmemory-samples 5
# 写入超过 10mb 的 key(每个 1kb,写约 1 万个):
# for i in $(seq 1 10000); do redis-cli SET k:$i $(head -c 1024 /dev/urandom | base64); done
127.0.0.1:6379> INFO stats
# 预期关注字段:
#   evicted_keys:N                    (被淘汰的 key 数,N > 0)
#   keyspace_hits / keyspace_misses   (命中率,反映 LRU 近似质量)

# 调大采样数对比(可选):
127.0.0.1:6379> CONFIG SET maxmemory-samples 10
# 重新 FLUSHALL + 写入同样数据,观察 evicted_keys 和命中率的差异
# 预期:samples=10 时命中率略高(近似质量更好),但 CPU 开销也更高
```

标注:以上预期基于源码常量(expire.c:94-98 的四个 `ACTIVE_EXPIRE_CYCLE_*` 常量、evict.c:282 的 `LFULogIncr` 概率公式、config.c 的默认值)与 LFU 对数计数器的数学性质(13.7.1 节推导)推导。本书未在本地实跑;若你的 redis 版本/配置不同,具体数值可能偏移,以 `CONFIG GET` 实际值为准。LFU counter 的具体值是概率性的(取决于 `rand()`),每次运行可能略有不同,但"增长速度随当前值递减"这一对数特性不变。
