# 附录 A · 怎么读 Redis 源码

本书一路追到 Redis 的设计动机与实现细节。但读源码这件事,光看别人讲是不够的——你得自己打开代码、亲手跑、亲手打断点。本附录给你一份"上手指南":怎么把 Redis 拉下来编成可调试的样子、哪段代码该读哪个文件、gdb 该在哪里下断点、redis-cli 哪些命令能验证本书的论断,以及一条推荐的阅读路径。读完它,你应该能独立在源码里漫游。

## A.1 获取与编译

**拉源码**,锁定 8.0.2 这个 tag,保证你看到的就是本书引用的那一份:

```bash
# 克隆仓库并切到 8.0.2 这个精确版本
git clone https://github.com/redis/redis.git
cd redis
git checkout 8.0.2           # 与本书源码行号一一对应
```

**直接 `make` 不行**。Redis 的 Makefile 默认开 `-O3`(甚至开 LTO 链接期优化),见 [src/Makefile:27](../../redis-8.0.2/src/Makefile#L27) 的 `OPTIMIZATION?=-O3`。优化一开,变量被寄存器吃掉、函数被内联,gdb 里 `p 变量` 看到 `<optimized out>`,单步跳来跳去,根本没法读。

Redis 给你准备了现成的 `noopt` 伪目标,一行搞定,见 [src/Makefile:519](../../redis-8.0.2/src/Makefile#L519):

```bash
make noopt                   # 关掉优化,编译出可调试的 redis-server
```

它内部就是 `make OPTIMIZATION="-O0"`(见下一行),所以你也可以手动:

```bash
make OPTIMIZATION="-O0"     # 等价写法,或者再补 -g
make MALLOC=libc            # 跑不了 jemalloc 时改用 libc(简化调试)
```

几个常用可选项,按需开关:

| 选项 | 作用 | 何时用 |
|---|---|---|
| `make BUILD_TLS=yes` | 编进 OpenSSL,TLS 连接 | 看加密链路、TLS 握手相关代码 |
| `make MALLOC=libc` | 改用 libc malloc | 调内存时不想被 jemalloc 干扰 |
| `make BUILD_WITH_MODULES=yes` | 编模块接口支持 | 看 module API |
| `make test` | 跑测试套件 | 改完代码回归验证 |

编译产物是 `src/redis-server`、`src/redis-cli` 等。**调试时务必用 `noopt` 版本**,本书给的行号都是在未优化源码上定位的。

## A.2 源码地图:我想看 X,该读哪个文件

这张表按本书篇章组织。每个条目给出**入口函数名**(真实存在,已在 8.0.2 源码核实),你直接 `grep -n` 跳进去就能跟。

| 你想看的主题 | 文件 | 关键入口函数 |
|---|---|---|
| **命令骨架**(事件循环) | `ae.c` | `aeMain` ([ae.c:492](../../redis-8.0.2/src/ae.c#L492))、`aeProcessEvents` ([ae.c:360](../../redis-8.0.2/src/ae.c#L360)) |
| **主循环前后钩子** | `server.c` | `beforeSleep` ([server.c:1717](../../redis-8.0.2/src/server.c#L1717))、`serverCron`(定时任务) |
| **进程启动** | `server.c` | `main` ([server.c:7219](../../redis-8.0.2/src/server.c#L7219)) → `initServer` ([server.c:2685](../../redis-8.0.2/src/server.c#L2685)) → `aeMain` ([server.c:7559](../../redis-8.0.2/src/server.c#L7559)) |
| **命令解析与分派** | `server.c` | `processCommand` ([server.c:3985](../../redis-8.0.2/src/server.c#L3985))、`call` ([server.c:3624](../../redis-8.0.2/src/server.c#L3624)) |
| **命令查找** | `server.c` | `lookupCommand` ([server.c:3295](../../redis-8.0.2/src/server.c#L3295)) → `lookupCommandLogic` ([server.c:3279](../../redis-8.0.2/src/server.c#L3279)) |
| **客户端读** | `networking.c` | `readQueryFromClient` ([networking.c:2884](../../redis-8.0.2/src/networking.c#L2884)) |
| **客户端写回** | `networking.c` | `prepareClientToWrite` ([networking.c:328](../../redis-8.0.2/src/networking.c#L328))、`handleClientsWithPendingWrites` ([networking.c:2195](../../redis-8.0.2/src/networking.c#L2195)) |
| **sds 字符串** | `sds.c` | `sdsnewlen` 及 `SDS_TYPE_*` 分支 ([sds.c:24](../../redis-8.0.2/src/sds.c#L24)) |
| **dict 哈希表** | `dict.c` | `dictRehash` ([dict.c:393](../../redis-8.0.2/src/dict.c#L393))、`dictRehashMicroseconds` ([dict.c:434](../../redis-8.0.2/src/dict.c#L434))(渐进式 rehash) |
| **listpack** | `listpack.c` | `lpNew` ([listpack.c:221](../../redis-8.0.2/src/listpack.c#L221)) |
| **quicklist** | `quicklist.c` | `quicklistCreate` ([quicklist.c:127](../../redis-8.0.2/src/quicklist.c#L127))、`quicklistNew` ([quicklist.c:166](../../redis-8.0.2/src/quicklist.c#L166)) |
| **跳表 / zset** | `t_zset.c` | `zslInsert` ([t_zset.c:122](../../redis-8.0.2/src/t_zset.c#L122))(跳表插入) |
| **RDB 持久化** | `rdb.c` | `rdbSaveBackground` ([rdb.c:1642](../../redis-8.0.2/src/rdb.c#L1642))、`rdbSave` ([rdb.c:1599](../../redis-8.0.2/src/rdb.c#L1599)) |
| **AOF 持久化** | `aof.c` | `flushAppendOnlyFile` ([aof.c:1143](../../redis-8.0.2/src/aof.c#L1143))(AOF 刷盘策略入口) |
| **复制** | `replication.c` | `replicationFeedSlaves` ([replication.c:496](../../redis-8.0.2/src/replication.c#L496))(主节点向从节点推送) |
| **集群** | `cluster.c` | `clusterCommand` ([cluster.c:941](../../redis-8.0.2/src/cluster.c#L941)) |
| **后台 BIO 线程** | `bio.c` | `bioInit` ([bio.c:127](../../redis-8.0.2/src/bio.c#L127))、`bioProcessBackgroundJobs` ([bio.c:257](../../redis-8.0.2/src/bio.c#L257)) |
| **多线程 IO**(8.0 新) | `iothread.c` | `IOThreads` 数组与 `mainThreadPendingClientsToIOThreads` 等链表 ([iothread.c:14](../../redis-8.0.2/src/iothread.c#L14)) |

读这张表的窍门:**别一上来读全文**。先 `grep -n "入口函数名" 文件.c`,跳到定义,再顺着它的调用者/被调用者一路展开。Redis 函数命名非常一致(动词开头、对象结尾),`grep` 几乎能当索引用。

## A.3 gdb 实战:跟着命令走一遍

编译完 `make noopt`,先起一个 server:

```bash
./src/redis-server --port 6399      # 用一个不冲突的端口,别影响系统 redis
```

再开一个终端,attach 上去:

```bash
gdb -p $(pidof redis-server)        # attach 到运行中的进程
# 或者:gdb ./src/redis-server,然后在 gdb 里 run --port 6399
```

一组**关键断点**,跟着本书章节下,能直接看到对应的现象:

```
(gdb) b aeMain                       # 主循环入口,看事件循环骨架
(gdb) b aeProcessEvents              # 每轮事件处理,看 epoll_wait→回调
(gdb) b readQueryFromClient          # 客户端数据到达,看命令如何被读进来
(gdb) b processCommand               # 命令分发入口,看校验/查表
(gdb) b lookupCommandLogic           # 命令表查找,看 redisCommand 结构
(gdb) b call                         # 真正执行命令的地方,呼应"命令执行链"
(gdb) b dictRehash                   # 渐进式 rehash 一次搬迁
(gdb) b dictRehashMicroseconds       # 按 1ms 预算限时 rehash,看时间控制
(gdb) b rdbSaveBackground            # BGSAVE fork 出去的瞬间
(gdb) b replicationFeedSlaves        # 主节点把写命令推给从节点
(gdb) b flushAppendOnlyFile          # AOF 刷盘,看 always/everysec/no 三档
(gdb) b beforeSleep                  # 每轮事件循环尾部,看刷盘/写回/清理
```

**看 client 结构**是读 Redis 的基本功。`processCommand` 命中后,当前客户端就在参数 `c` 里:

```
(gdb) b processCommand
(gdb) continue
# 在 redis-cli 里敲一句:SET foo bar
(gdb) p *c                          # 打印整个 client 结构
(gdb) p c->argc                     # 参数个数,应为 3
(gdb) p c->argv[0]->ptr             # 第一个参数,应为 "SET"
(gdb) p c->argv[1]->ptr             # "foo"
(gdb) p c->cmd->name                # 解析出来的命令名
(gdb) p c->db->id                   # 它所在的 db 编号
```

`c->cmd` 是 `redisCommand *` 结构指针,里面有 `proc`(命令处理函数指针)、`arity`、`flags`、复杂度声明等。本书讲命令时反复提到这些字段,gdb 里 `p *c->cmd` 一打全出来,印象立刻立体。

想看一条命令的完整生命周期?顺序大致是:`readQueryFromClient` → 解析成 `argc/argv` → `processCommand` → `lookupCommandLogic` 解析出 `c->cmd` → `call` → `c->cmd->proc(c)`(真正干活的函数,如 `setCommand`)→ `addReply*` 把结果挂到 client 的回复缓冲 → `beforeSleep` 里 `handleClientsWithPendingWrites` 把回复写出去。把这串断点全部下上,敲一个 `GET foo` 走一遍,Redis 的骨架就活了。

## A.4 redis-cli 观察利器

光读代码不跑,很多设计体会不到。redis-cli 自带一批**调试/观察命令**,直接对应本书的论断。注意:下面只描述你会看到什么现象,**具体数字以你本地实测为准,本书不编造输出**。

**看编码切换**(呼应数据结构章节):Redis 的对象底层有 listpack/intset/hashtable/skiplist 等多种编码,会随元素多少自动切换。

```text
127.0.0.1:6399> OBJECT ENCODING myhash     # 小 hash:可能是 listpack
127.0.0.1:6399> HSET myhash f1 v1 f2 v2 ... # 灌入大量字段
127.0.0.1:6399> OBJECT ENCODING myhash     # 超过阈值后:会变成 hashtable
```

你会在某个临界点看到编码从紧凑型(listpack)切到查询型(hashtable/skiplist)。这正是本书反复强调的"用空间和时间换:小数据省内存、大数据要性能"。

**看慢查询**:`SLOWLOG GET` 列出执行时间超过阈值的命令,带执行时长和精确参数。本书讲"为什么命令要 O(1)/O(log N)",慢查询就是验证工具。`DEBUG SLEEP 2` 会让 server 阻塞 2 秒(模拟慢命令),随后 `SLOWLOG GET` 必然能看到它。

**看内存碎片**(呼应内存管理章节):

```text
127.0.0.1:6399> INFO memory
# 关注 mem_fragmentation_ratio(used_memory_rss / used_memory)
# 大量删除后再看,碎片率会升高,体现 jemalloc 不能立刻还给 OS
```

`DEBUG OBJECT key` 会给出该 key 的内部信息(序列化长度、引用计数、LRU 时钟等),看 `serializedlength` 能直观感受不同编码的体积差。

**看客户端与集群状态**:`CLIENT LIST` 列出所有连接,含 `laddr`、`cmd`、`age`、`idle`、`flags`(主从标记 `S`/`M`)等,排查连接问题首选。集群模式下 `CLUSTER NODES` 输出一张节点表(自己、主从角色、槽位范围、主从关系、PONG/FAIL 状态),是理解 cluster 拓扑的最快入口。本书讲复制与集群时,这些命令是你"看见拓扑"的眼睛。

一句话总结这一节:**读完一章,grep 一个入口,跑一个 redis-cli 命令验证**。三步循环,比单纯翻代码高效十倍。

## A.5 一条推荐阅读路径

面对百万行级代码,新人最容易卡在"从哪开始"。给你一条亲测有效的路径,分三步:

**第一步:读骨架,15 分钟**。打开 [server.c:7219](../../redis-8.0.2/src/server.c#L7219) 的 `main`,顺着走:`main` → 加载配置 → `initServer` ([server.c:2685](../../redis-8.0.2/src/server.c#L2685)) 注册各种回调 → `aeMain(server.el)` ([server.c:7559](../../redis-8.0.2/src/server.c#L7559)) 进入事件循环。到这里你就握住了 Redis 的脊椎:一个 `while(1)` 的事件循环,所有事情都从它派生。别纠结细节,只看主干。

**第二步:跟着一条命令走血肉,半天**。在 `redis-cli` 敲 `SET foo bar`,同时 gdb 在 `readQueryFromClient` → `processCommand` → `lookupCommandLogic` → `call` 上断点。一次单步,你会看清:字节流怎么变 `argc/argv`、命令表怎么查、`call` 怎么真正调到 `setCommand`、结果怎么挂回 client。走完这一遍,Redis 对你不再是黑盒。

**第三步:钻数据结构练内功,按需**。本书主体已经把 sds/dict/listpack/quicklist/跳表讲透了,这里只需"对号入座"。想理解渐进式 rehash,去 [dict.c:393](../../redis-8.0.2/src/dict.c#L393) 的 `dictRehash` 配合 `dictRehashMicroseconds` 看 1ms 预算怎么卡时间;想看 zset 跳表插入,[t_zset.c:122](../../redis-8.0.2/src/t_zset.c#L122) 的 `zslInsert` 一目了然。**每读一个数据结构,回到第二步的命令路径,问一句"这个命令用到它了吗"**——这样内功才接得上血肉。

## A.6 全书 gdb 断点速查(按章)

A.3 给了一组"骨架级"断点。这一节按本书章节顺序,把每章最值得下断点的函数列出来——读到哪章,就照着在哪下断点,亲手验证那一章的论断。所有断点都对 redis-8.0.2 源码核实过行号。

| 章 | 主题 | 关键断点 | 你会看到什么 |
|---|---|---|---|
| Ch1 | networking | `readQueryFromClient`、`processMultibulkBuffer`、`_addReplyToBufferOrList` | 字节流进 querybuf、RESP 啃成 argv、回复先进静态缓冲 |
| Ch2 | ae 事件循环 | `aeMain`、`aeProcessEvents`、`aeApiPoll`、`beforeSleep`、`ae.c:426`(invert) | 一轮循环六步、epoll_wait 睡与醒、先读后写/AE_BARRIER 反转 |
| Ch3 | processCommand | `processCommand`、`call`、`lookupCommandLogic` | 命令查表、关卡检查、`c->cmd->proc(c)` 函数指针分派 |
| Ch4 | SDS | `_sdsMakeRoomFor`、`_sdsnewlen`、`createEmbeddedStringObject` | 5 种 header 选型、预分配翻倍、embstr 44 阈值 |
| Ch5 | dict | `dictRehash`、`dictScanDefrag`、`dictTwoPhaseUnlinkFind` | 渐进 rehash 搬桶、reverse-bit 游标、两阶段删除 |
| Ch6 | listpack | `lpPrepend`、`lpEncodeIntegerGetType`、`lpDecodeBacklen` | 紧凑编码、整数变长编码、backlen 反向遍历 |
| Ch7 | quicklist | `quicklistPush`、`quicklistLzfCompress`(`lzf_c.c:lzf_compress`) | 双层结构、LZF 压缩、两端不压中间压 |
| Ch8 | skiplist | `zslInsert`、`zslRandomLevel`、`zslDeleteNode`、`zslGetRank` | span 切分公式、掷骰子定层、借还对称、累加算排名 |
| Ch9 | rax | `raxLowWalk`、`raxInsert`、`raxRemove` | 节点分裂三段切、压缩 vs 扇出布局、前缀共享 |
| Ch10 | object/keymeta | `createObject`、`createEmbeddedStringObject`、`decrRefCount` | robj 16 字节、embstr→raw 转换、refcount GC |
| Ch11 | kvstore | `kvstoreScan`、`kvstoreTryResizeDicts`、`addDictIndexToCursor` | Fenwick 找非空 slot、SCAN 两段式游标、轮转 rehash |
| Ch12 | zmalloc/defrag | `activeDefragAlloc`、`defragStageDbKeys`、`je_get_defrag_hint` | 在线碎片整理、限时预算、jemalloc 专用接口 |
| Ch13 | expire/evict | `activeExpireCycle`、`LFULogIncr`、`evictionPoolPopulate` | 限时过期扫描、Morris 概率计数器、采样池淘汰 |
| Ch14 | RDB | `rdbSaveBackground`、`rdbSaveRio`、`dismissMemory`(`server.c:6847`) | fork+COW、rio 抽象、MADV_DONTNEED 反向降 COW |
| Ch15 | AOF | `flushAppendOnlyFile`、`backgroundRewriteDoneHandler`、`aof.c:1163`(空 buffer 补 fsync) | 三档 fsync、MP-AOF base/incr 切换、隐蔽 bug 修复点 |
| Ch16 | 主从复制 | `masterTryPartialResynchronization`、`feedReplicationBuffer`、`incrementalTrimReplicationBacklog` | psync 双校验、全局共享缓冲、引用计数不裁剪 |
| Ch17 | Cluster | `clusterSendPing`、`keyHashSlot`、`clusterRedirectClient` | gossip 1/10 概率、crc16&0x3FFF、MOVED/ASK |
| Ch18 | Sentinel | `sentinelStartFailover`、`sentinelVoteLeader`、`sentinelGetLeader` | ODOWN→选举、类 Raft 先到先得、统票绝对多数 |
| Ch19 | BIO/lazyfree | `bioProcessBackgroundJobs`、`lazyfreeGetFreeEffort`、`emptyDbAsync` | 三类 worker、同步异步阈值 64、换壳释放 |
| Ch20 | IO 多线程 | `handleClientsWithPendingReadsUsingThreads`、`IOThread`、`sendPendingClientsToIOThreads` | 解析下放执行不下放、每线程独立 ae、暂停等齐 |
| Ch21 | 事务/Lua/阻塞 | `touchWatchedKey`、`lua_sethook`、`signalKeyAsReady`、`handleClientsBlockedOnKeys` | WATCH 打脏、Lua 超时钩子+嵌套事件循环、BLPOP 唤醒 |

用法:读到第 N 章,grep 出上表对应函数名,在 gdb 里 `break 函数名`,再 `redis-cli` 发一条触发命令,断点命中后用 `p 变量` 看本章论断的内部状态(如 Ch8 在 `zslInsert` 停下,`p rank[0]`、`p update[i]->level[i].span` 看 span 切分)。这比读一百遍代码都管用——**本书的验证物,就是这张表的手感**。

---

最后送一句经验:Redis 源码以"直球"著称——少有花哨的抽象,函数基本是过程式的,逻辑顺着读就行。真正的难点不在语法,而在**设计动机**(为什么这么做)和**实现技巧**(怎么把性能榨出来)。本书主体解决前者,本附录帮你亲手验证后者。把 `make noopt`、`gdb`、`redis-cli` 这三件套用熟,你就能在源码里自由漫游了。
