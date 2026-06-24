# 附录 B · 命令 ↔ 源码函数映射表

这是一张工具型速查表。当你在某章看到一个命令(例如 `SET`、`ZADD`、`XADD`),想跳到源码里读它的真正实现时,用它定位:**命令名 → proc 函数 → 所在文件 → 行号**。

表中每一条映射都经过对 Redis 8.0.2 源码的 `commands.def`(命令注册表)与各 `t_*.c`(类型实现)的真实 `grep` 核实,行号均为函数定义所在行。`commands/` 目录下的 JSON 文件与 `commands.def` 是**声明式定义**(参数 schema、flag、复杂度),真正干活的是 `t_*.c` 里的 `xxxCommand` 函数——它才是本书反复提到的"proc"。

## B.1 命令的三层结构

读者要分清三层,本书第三章对此有详解:

| 层次 | 文件 | 作用 |
|------|------|------|
| 声明 | `src/commands/*.json` | 每条命令一份 JSON:参数、flag、复杂度、ACL 类别、文档。人写。 |
| 汇总 | `src/commands.def`(`commands.c` 包含) | 由上述 JSON **代码生成**的 C 结构体数组 `redisCommandTable[]`,每条 `MAKE_CMD(...)` 绑定命令名与 proc 函数指针。 |
| 实现 | `src/t_*.c`、`server.c`、`db.c` 等 | `xxxCommand(client *c)` 函数体,命令的真正逻辑。 |

## B.2 分类映射表

### String(字符串) — `t_string.c`

| 命令 | proc 函数 | 文件:行号 | 本书章节 |
|------|-----------|-----------|----------|
| `SET` | `setCommand` | [t_string.c:276](../../redis-8.0.2/src/t_string.c#L276) | 第 4 章 |
| `SETNX` | `setnxCommand` | [t_string.c:289](../../redis-8.0.2/src/t_string.c#L289) | 第 4 章 |
| `SETEX` | `setexCommand` | [t_string.c:294](../../redis-8.0.2/src/t_string.c#L294) | 第 4 章 |
| `PSETEX` | `psetexCommand` | [t_string.c:299](../../redis-8.0.2/src/t_string.c#L299) | 第 4 章 |
| `GET` | `getCommand` | [t_string.c:318](../../redis-8.0.2/src/t_string.c#L318) | 第 4 章 |
| `GETEX` | `getexCommand` | [t_string.c:342](../../redis-8.0.2/src/t_string.c#L342) | 第 4 章 |
| `GETDEL` | `getdelCommand` | [t_string.c:401](../../redis-8.0.2/src/t_string.c#L401) | 第 4 章 |
| `GETSET` | `getsetCommand` | [t_string.c:412](../../redis-8.0.2/src/t_string.c#L412) | 第 4 章 |
| `SETRANGE` | `setrangeCommand` | [t_string.c:423](../../redis-8.0.2/src/t_string.c#L423) | 第 4 章 |
| `GETRANGE` | `getrangeCommand` | [t_string.c:487](../../redis-8.0.2/src/t_string.c#L487) | 第 4 章 |
| `MGET` | `mgetCommand` | [t_string.c:528](../../redis-8.0.2/src/t_string.c#L528) | 第 4 章 |
| `MSET` | `msetCommand` | [t_string.c:578](../../redis-8.0.2/src/t_string.c#L578) | 第 4 章 |
| `MSETNX` | `msetnxCommand` | [t_string.c:582](../../redis-8.0.2/src/t_string.c#L582) | 第 4 章 |
| `INCR` | `incrCommand` | [t_string.c:622](../../redis-8.0.2/src/t_string.c#L622) | 第 4 章 |
| `DECR` | `decrCommand` | [t_string.c:626](../../redis-8.0.2/src/t_string.c#L626) | 第 4 章 |
| `INCRBY` | `incrbyCommand` | [t_string.c:630](../../redis-8.0.2/src/t_string.c#L630) | 第 4 章 |
| `DECRBY` | `decrbyCommand` | [t_string.c:637](../../redis-8.0.2/src/t_string.c#L637) | 第 4 章 |
| `INCRBYFLOAT` | `incrbyfloatCommand` | [t_string.c:649](../../redis-8.0.2/src/t_string.c#L649) | 第 4 章 |
| `APPEND` | `appendCommand` | [t_string.c:683](../../redis-8.0.2/src/t_string.c#L683) | 第 4 章 |
| `STRLEN` | `strlenCommand` | [t_string.c:718](../../redis-8.0.2/src/t_string.c#L718) | 第 4 章 |
| `LCS` | `lcsCommand` | [t_string.c:726](../../redis-8.0.2/src/t_string.c#L726) | 第 4 章 |

### List(列表) — `t_list.c`

| 命令 | proc 函数 | 文件:行号 | 本书章节 |
|------|-----------|-----------|----------|
| `LPUSH` | `lpushCommand` | [t_list.c:498](../../redis-8.0.2/src/t_list.c#L498) | 第 5 章 |
| `RPUSH` | `rpushCommand` | [t_list.c:503](../../redis-8.0.2/src/t_list.c#L503) | 第 5 章 |
| `LPUSHX` | `lpushxCommand` | [t_list.c:508](../../redis-8.0.2/src/t_list.c#L508) | 第 5 章 |
| `RPUSHX` | `rpushxCommand` | [t_list.c:513](../../redis-8.0.2/src/t_list.c#L513) | 第 5 章 |
| `LINSERT` | `linsertCommand` | [t_list.c:518](../../redis-8.0.2/src/t_list.c#L518) | 第 5 章 |
| `LLEN` | `llenCommand` | [t_list.c:573](../../redis-8.0.2/src/t_list.c#L573) | 第 5 章 |
| `LINDEX` | `lindexCommand` | [t_list.c:580](../../redis-8.0.2/src/t_list.c#L580) | 第 5 章 |
| `LSET` | `lsetCommand` | [t_list.c:609](../../redis-8.0.2/src/t_list.c#L609) | 第 5 章 |
| `LPOP` | `lpopCommand` | [t_list.c:853](../../redis-8.0.2/src/t_list.c#L853) | 第 5 章 |
| `RPOP` | `rpopCommand` | [t_list.c:858](../../redis-8.0.2/src/t_list.c#L858) | 第 5 章 |
| `LRANGE` | `lrangeCommand` | [t_list.c:863](../../redis-8.0.2/src/t_list.c#L863) | 第 5 章 |
| `LTRIM` | `ltrimCommand` | [t_list.c:877](../../redis-8.0.2/src/t_list.c#L877) | 第 5 章 |
| `LPOS` | `lposCommand` | [t_list.c:946](../../redis-8.0.2/src/t_list.c#L946) | 第 5 章 |
| `LREM` | `lremCommand` | [t_list.c:1043](../../redis-8.0.2/src/t_list.c#L1043) | 第 5 章 |
| `LMOVE` | `lmoveCommand` | [t_list.c:1168](../../redis-8.0.2/src/t_list.c#L1168) | 第 5 章 |
| `RPOPLPUSH` | `rpoplpushCommand` | [t_list.c:1192](../../redis-8.0.2/src/t_list.c#L1192) | 第 5 章 |
| `LMPOP` | `lmpopCommand` | [t_list.c:1374](../../redis-8.0.2/src/t_list.c#L1374) | 第 5 章 |
| `BLPOP` | `blpopCommand` | [t_list.c:1271](../../redis-8.0.2/src/t_list.c#L1271) | 第 5 章 / 阻塞 |
| `BRPOP` | `brpopCommand` | [t_list.c:1276](../../redis-8.0.2/src/t_list.c#L1276) | 第 5 章 / 阻塞 |
| `BLMOVE` | `blmoveCommand` | [t_list.c:1302](../../redis-8.0.2/src/t_list.c#L1302) | 第 5 章 / 阻塞 |
| `BRPOPLPUSH` | `brpoplpushCommand` | [t_list.c:1315](../../redis-8.0.2/src/t_list.c#L1315) | 第 5 章 / 阻塞 |
| `BLMPOP` | `blmpopCommand` | [t_list.c:1379](../../redis-8.0.2/src/t_list.c#L1379) | 第 5 章 / 阻塞 |

### Hash(哈希) — `t_hash.c`

| 命令 | proc 函数 | 文件:行号 | 本书章节 |
|------|-----------|-----------|----------|
| `HSETNX` | `hsetnxCommand` | [t_hash.c:2152](../../redis-8.0.2/src/t_hash.c#L2152) | 第 6 章 |
| `HSET` | `hsetCommand` | [t_hash.c:2179](../../redis-8.0.2/src/t_hash.c#L2179) | 第 6 章 |
| `HINCRBY` | `hincrbyCommand` | [t_hash.c:2508](../../redis-8.0.2/src/t_hash.c#L2508) | 第 6 章 |
| `HINCRBYFLOAT` | `hincrbyfloatCommand` | [t_hash.c:2554](../../redis-8.0.2/src/t_hash.c#L2554) | 第 6 章 |
| `HGET` | `hgetCommand` | [t_hash.c:2639](../../redis-8.0.2/src/t_hash.c#L2639) | 第 6 章 |
| `HMGET` | `hmgetCommand` | [t_hash.c:2648](../../redis-8.0.2/src/t_hash.c#L2648) | 第 6 章 |
| `HDEL` | `hdelCommand` | [t_hash.c:2929](../../redis-8.0.2/src/t_hash.c#L2929) | 第 6 章 |
| `HLEN` | `hlenCommand` | [t_hash.c:2973](../../redis-8.0.2/src/t_hash.c#L2973) | 第 6 章 |
| `HSTRLEN` | `hstrlenCommand` | [t_hash.c:2982](../../redis-8.0.2/src/t_hash.c#L2982) | 第 6 章 |
| `HKEYS` | `hkeysCommand` | [t_hash.c:3069](../../redis-8.0.2/src/t_hash.c#L3069) | 第 6 章 |
| `HVALS` | `hvalsCommand` | [t_hash.c:3073](../../redis-8.0.2/src/t_hash.c#L3073) | 第 6 章 |
| `HGETALL` | `hgetallCommand` | [t_hash.c:3077](../../redis-8.0.2/src/t_hash.c#L3077) | 第 6 章 |
| `HEXISTS` | `hexistsCommand` | [t_hash.c:3081](../../redis-8.0.2/src/t_hash.c#L3081) | 第 6 章 |
| `HSCAN` | `hscanCommand` | [t_hash.c:3090](../../redis-8.0.2/src/t_hash.c#L3090) | 第 6 章 / SCAN |
| `HRANDFIELD` | `hrandfieldCommand` | [t_hash.c:3373](../../redis-8.0.2/src/t_hash.c#L3373) | 第 6 章 |
| `HPERSIST` | `hpersistCommand` | [t_hash.c:3877](../../redis-8.0.2/src/t_hash.c#L3877) | 第 6 章 / 字段过期 |

注:`HMSET` 已弃用,`commands.def` 中复用 `hsetCommand`([commands.def:11177](../../redis-8.0.2/src/commands.def#L11177))。

### Set(集合) — `t_set.c`

| 命令 | proc 函数 | 文件:行号 | 本书章节 |
|------|-----------|-----------|----------|
| `SADD` | `saddCommand` | [t_set.c:589](../../redis-8.0.2/src/t_set.c#L589) | 第 7 章 |
| `SREM` | `sremCommand` | [t_set.c:616](../../redis-8.0.2/src/t_set.c#L616) | 第 7 章 |
| `SMOVE` | `smoveCommand` | [t_set.c:648](../../redis-8.0.2/src/t_set.c#L648) | 第 7 章 |
| `SISMEMBER` | `sismemberCommand` | [t_set.c:709](../../redis-8.0.2/src/t_set.c#L709) | 第 7 章 |
| `SCARD` | `scardCommand` | [t_set.c:740](../../redis-8.0.2/src/t_set.c#L740) | 第 7 章 |
| `SPOP` | `spopCommand` | [t_set.c:966](../../redis-8.0.2/src/t_set.c#L966) | 第 7 章 |
| `SRANDMEMBER` | `srandmemberCommand` | [t_set.c:1227](../../redis-8.0.2/src/t_set.c#L1227) | 第 7 章 |
| `SINTER` | `sinterCommand` | [t_set.c:1443](../../redis-8.0.2/src/t_set.c#L1443) | 第 7 章 |
| `SMEMBERS` | `smembersCommand` | [t_set.c:1448](../../redis-8.0.2/src/t_set.c#L1448) | 第 7 章 |
| `SINTERCARD` | `sinterCardCommand` | [t_set.c:1478](../../redis-8.0.2/src/t_set.c#L1478) | 第 7 章 |
| `SINTERSTORE` | `sinterstoreCommand` | [t_set.c:1510](../../redis-8.0.2/src/t_set.c#L1510) | 第 7 章 |
| `SUNION` | `sunionCommand` | [t_set.c:1707](../../redis-8.0.2/src/t_set.c#L1707) | 第 7 章 |
| `SUNIONSTORE` | `sunionstoreCommand` | [t_set.c:1712](../../redis-8.0.2/src/t_set.c#L1712) | 第 7 章 |
| `SDIFF` | `sdiffCommand` | [t_set.c:1717](../../redis-8.0.2/src/t_set.c#L1717) | 第 7 章 |
| `SDIFFSTORE` | `sdiffstoreCommand` | [t_set.c:1722](../../redis-8.0.2/src/t_set.c#L1722) | 第 7 章 |
| `SSCAN` | `sscanCommand` | [t_set.c:1726](../../redis-8.0.2/src/t_set.c#L1726) | 第 7 章 / SCAN |

### ZSet(有序集合) — `t_zset.c`

| 命令 | proc 函数 | 文件:行号 | 本书章节 |
|------|-----------|-----------|----------|
| `ZADD` | `zaddCommand` | [t_zset.c:1886](../../redis-8.0.2/src/t_zset.c#L1886) | 第 8 章 |
| `ZINCRBY` | `zincrbyCommand` | [t_zset.c:1890](../../redis-8.0.2/src/t_zset.c#L1890) | 第 8 章 |
| `ZREM` | `zremCommand` | [t_zset.c:1894](../../redis-8.0.2/src/t_zset.c#L1894) | 第 8 章 |
| `ZUNIONSTORE` | `zunionstoreCommand` | [t_zset.c:2916](../../redis-8.0.2/src/t_zset.c#L2916) | 第 8 章 |
| `ZINTERSTORE` | `zinterstoreCommand` | [t_zset.c:2921](../../redis-8.0.2/src/t_zset.c#L2921) | 第 8 章 |
| `ZUNION` / `ZINTER` / `ZDIFF` | `zunionCommand` / `zinterCommand` / `zdiffCommand` | [t_zset.c:2931](../../redis-8.0.2/src/t_zset.c#L2931) / [:2936](../../redis-8.0.2/src/t_zset.c#L2936) / [:2946](../../redis-8.0.2/src/t_zset.c#L2946) | 第 8 章 |
| `ZINTERCARD` | `zinterCardCommand` | [t_zset.c:2941](../../redis-8.0.2/src/t_zset.c#L2941) | 第 8 章 |
| `ZRANGE` | `zrangeCommand` | [t_zset.c:3238](../../redis-8.0.2/src/t_zset.c#L3238) | 第 8 章 |
| `ZREVRANGE` | `zrevrangeCommand` | [t_zset.c:3245](../../redis-8.0.2/src/t_zset.c#L3245) | 第 8 章 |
| `ZCOUNT` | `zcountCommand` | [t_zset.c:3369](../../redis-8.0.2/src/t_zset.c#L3369) | 第 8 章 |
| `ZLEXCOUNT` | `zlexcountCommand` | [t_zset.c:3446](../../redis-8.0.2/src/t_zset.c#L3446) | 第 8 章 |
| `ZCARD` | `zcardCommand` | [t_zset.c:3798](../../redis-8.0.2/src/t_zset.c#L3798) | 第 8 章 |
| `ZSCORE` | `zscoreCommand` | [t_zset.c:3808](../../redis-8.0.2/src/t_zset.c#L3808) | 第 8 章 |
| `ZMSCORE` | `zmscoreCommand` | [t_zset.c:3823](../../redis-8.0.2/src/t_zset.c#L3823) | 第 8 章 |
| `ZRANK` | `zrankCommand` | [t_zset.c:3885](../../redis-8.0.2/src/t_zset.c#L3885) | 第 8 章 |
| `ZREVRANK` | `zrevrankCommand` | [t_zset.c:3889](../../redis-8.0.2/src/t_zset.c#L3889) | 第 8 章 |
| `ZSCAN` | `zscanCommand` | [t_zset.c:3893](../../redis-8.0.2/src/t_zset.c#L3893) | 第 8 章 / SCAN |
| `ZPOPMIN` | `zpopminCommand` | [t_zset.c:4082](../../redis-8.0.2/src/t_zset.c#L4082) | 第 8 章 |
| `ZPOPMAX` | `zpopmaxCommand` | [t_zset.c:4087](../../redis-8.0.2/src/t_zset.c#L4087) | 第 8 章 |
| `BZPOPMIN` | `bzpopminCommand` | [t_zset.c:4158](../../redis-8.0.2/src/t_zset.c#L4158) | 第 8 章 / 阻塞 |
| `BZPOPMAX` | `bzpopmaxCommand` | [t_zset.c:4163](../../redis-8.0.2/src/t_zset.c#L4163) | 第 8 章 / 阻塞 |
| `ZRANDMEMBER` | `zrandmemberCommand` | [t_zset.c:4399](../../redis-8.0.2/src/t_zset.c#L4399) | 第 8 章 |
| `ZMPOP` | `zmpopCommand` | [t_zset.c:4489](../../redis-8.0.2/src/t_zset.c#L4489) | 第 8 章 |
| `BZMPOP` | `bzmpopCommand` | [t_zset.c:4494](../../redis-8.0.2/src/t_zset.c#L4494) | 第 8 章 / 阻塞 |

### Bitmap(位图) — `bitops.c`

位图复用 String 编码,但 proc 在 `bitops.c`。

| 命令 | proc 函数 | 文件:行号 | 本书章节 |
|------|-----------|-----------|----------|
| `SETBIT` | `setbitCommand` | [bitops.c:550](../../redis-8.0.2/src/bitops.c#L550) | 第 9 章 |
| `GETBIT` | `getbitCommand` | [bitops.c:605](../../redis-8.0.2/src/bitops.c#L605) | 第 9 章 |
| `BITOP` | `bitopCommand` | [bitops.c:633](../../redis-8.0.2/src/bitops.c#L633) | 第 9 章 |
| `BITCOUNT` | `bitcountCommand` | [bitops.c:822](../../redis-8.0.2/src/bitops.c#L822) | 第 9 章 |
| `BITPOS` | `bitposCommand` | [bitops.c:914](../../redis-8.0.2/src/bitops.c#L914) | 第 9 章 |
| `BITFIELD` | `bitfieldCommand` | [bitops.c:1319](../../redis-8.0.2/src/bitops.c#L1319) | 第 9 章 |
| `BITFIELD_RO` | `bitfieldroCommand` | [bitops.c:1323](../../redis-8.0.2/src/bitops.c#L1323) | 第 9 章 |

### Stream(流) — `t_stream.c`

| 命令 | proc 函数 | 文件:行号 | 本书章节 |
|------|-----------|-----------|----------|
| `XADD` | `xaddCommand` | [t_stream.c:2001](../../redis-8.0.2/src/t_stream.c#L2001) | 第 10 章 |
| `XRANGE` | `xrangeCommand` | [t_stream.c:2152](../../redis-8.0.2/src/t_stream.c#L2152) | 第 10 章 |
| `XREVRANGE` | `xrevrangeCommand` | [t_stream.c:2157](../../redis-8.0.2/src/t_stream.c#L2157) | 第 10 章 |
| `XLEN` | `xlenCommand` | [t_stream.c:2162](../../redis-8.0.2/src/t_stream.c#L2162) | 第 10 章 |
| `XREAD` / `XREADGROUP` | `xreadCommand`(共用) | [t_stream.c:2178](../../redis-8.0.2/src/t_stream.c#L2178) | 第 10 章 |
| `XGROUP` | `xgroupCommand`(容器) | [t_stream.c:2607](../../redis-8.0.2/src/t_stream.c#L2607) | 第 10 章 |
| `XSETID` | `xsetidCommand` | [t_stream.c:2774](../../redis-8.0.2/src/t_stream.c#L2774) | 第 10 章 |
| `XACK` | `xackCommand` | [t_stream.c:2853](../../redis-8.0.2/src/t_stream.c#L2853) | 第 10 章 |
| `XPENDING` | `xpendingCommand` | [t_stream.c:2912](../../redis-8.0.2/src/t_stream.c#L2912) | 第 10 章 |
| `XCLAIM` | `xclaimCommand` | [t_stream.c:3153](../../redis-8.0.2/src/t_stream.c#L3153) | 第 10 章 |
| `XDEL` | `xdelCommand` | [t_stream.c:3553](../../redis-8.0.2/src/t_stream.c#L3553) | 第 10 章 |
| `XTRIM` | `xtrimCommand` | [t_stream.c:3635](../../redis-8.0.2/src/t_stream.c#L3635) | 第 10 章 |
| `XINFO` | `xinfoCommand`(容器) | [t_stream.c:3881](../../redis-8.0.2/src/t_stream.c#L3881) | 第 10 章 |

注:`XREADGROUP` 与 `XREAD` 在 `commands.def` 中都绑定 `xreadCommand`([commands.def:11336](../../redis-8.0.2/src/commands.def#L11336)),由 GROUP 关键字分支区分。

### 键与过期(Keyspace & Expiration)

| 命令 | proc 函数 | 文件:行号 | 本书章节 |
|------|-----------|-----------|----------|
| `DEL` | `delCommand` | [db.c:960](../../redis-8.0.2/src/db.c#L960) | 第 3 章 |
| `UNLINK` | `unlinkCommand` | db.c(异步删除) | 第 3 章 |
| `EXISTS` | `existsCommand` | [db.c:970](../../redis-8.0.2/src/db.c#L970) | 第 3 章 |
| `TYPE` | `typeCommand` | [db.c:1544](../../redis-8.0.2/src/db.c#L1544) | 第 3 章 |
| `KEYS` | `keysCommand` | [db.c:1014](../../redis-8.0.2/src/db.c#L1014) | 第 3 章 |
| `SCAN` | `scanCommand` | [db.c:1530](../../redis-8.0.2/src/db.c#L1530) | 第 3 章 / SCAN |
| `RANDOMKEY` | `randomkeyCommand` | [db.c:1002](../../redis-8.0.2/src/db.c#L1002) | 第 3 章 |
| `RENAME` | `renameCommand` | [db.c:1666](../../redis-8.0.2/src/db.c#L1666) | 第 3 章 |
| `RENAMENX` | `renamenxCommand` | [db.c:1670](../../redis-8.0.2/src/db.c#L1670) | 第 3 章 |
| `MOVE` | `moveCommand` | [db.c:1674](../../redis-8.0.2/src/db.c#L1674) | 第 3 章 |
| `COPY` | `copyCommand` | [db.c:1753](../../redis-8.0.2/src/db.c#L1753) | 第 3 章 |
| `EXPIRE` | `expireCommand` | [expire.c:744](../../redis-8.0.2/src/expire.c#L744) | 第 11 章 |
| `EXPIREAT` | `expireatCommand` | [expire.c:749](../../redis-8.0.2/src/expire.c#L749) | 第 11 章 |
| `PEXPIRE` | `pexpireCommand` | [expire.c:754](../../redis-8.0.2/src/expire.c#L754) | 第 11 章 |
| `PEXPIREAT` | `pexpireatCommand` | [expire.c:759](../../redis-8.0.2/src/expire.c#L759) | 第 11 章 |
| `TTL` | `ttlCommand` | [expire.c:788](../../redis-8.0.2/src/expire.c#L788) | 第 11 章 |
| `PTTL` | `pttlCommand` | [expire.c:793](../../redis-8.0.2/src/expire.c#L793) | 第 11 章 |
| `EXPIRETIME` | `expiretimeCommand` | [expire.c:798](../../redis-8.0.2/src/expire.c#L798) | 第 11 章 |
| `PEXPIRETIME` | `pexpiretimeCommand` | [expire.c:803](../../redis-8.0.2/src/expire.c#L803) | 第 11 章 |
| `PERSIST` | `persistCommand` | [expire.c:808](../../redis-8.0.2/src/expire.c#L808) | 第 11 章 |
| `OBJECT` | (容器,子命令 `OBJECT ENCODING` 等) | db.c(`objectCommand`) | 第 2 章 / 对象 |

### 连接与服务器(Connection & Server)

| 命令 | proc 函数 | 文件:行号 | 本书章节 |
|------|-----------|-----------|----------|
| `PING` | `pingCommand` | [server.c:4769](../../redis-8.0.2/src/server.c#L4769) | 第 12 章 |
| `ECHO` | `echoCommand` | [server.c:4791](../../redis-8.0.2/src/server.c#L4791) | 第 12 章 |
| `TIME` | `timeCommand` | [server.c:4795](../../redis-8.0.2/src/server.c#L4795) | 第 12 章 |
| `SELECT` | `selectCommand` | [db.c:980](../../redis-8.0.2/src/db.c#L980) | 第 12 章 |
| `SWAPDB` | `swapdbCommand` | [db.c:2019](../../redis-8.0.2/src/db.c#L2019) | 第 12 章 |
| `QUIT` | `quitCommand` | [networking.c:3309](../../redis-8.0.2/src/networking.c#L3309) | 第 12 章 |
| `CLIENT` | `clientCommand`(容器) | [networking.c:3314](../../redis-8.0.2/src/networking.c#L3314) | 第 12 章 |
| `HELLO` | `helloCommand` | [networking.c:3875](../../redis-8.0.2/src/networking.c#L3875) | 第 12 章 / RESP3 |
| `AUTH` | `authCommand` | [acl.c:3236](../../redis-8.0.2/src/acl.c#L3236) | 第 14 章 / ACL |
| `INFO` | `infoCommand` | [server.c:6457](../../redis-8.0.2/src/server.c#L6457) | 第 12 章 |
| `COMMAND` | `commandCommand` | [server.c:5313](../../redis-8.0.2/src/server.c#L5313) | 第 3 章 / 自省 |
| `DEBUG` | `debugCommand`(容器) | [debug.c:384](../../redis-8.0.2/src/debug.c#L384) | 第 12 章 |
| `CONFIG` | (容器 `CONFIG GET`/`SET`) | config.c | 第 12 章 |
| `DBSIZE` | `dbsizeCommand` | [db.c:1536](../../redis-8.0.2/src/db.c#L1536) | 第 12 章 |
| `LASTSAVE` | `lastsaveCommand` | [db.c:1540](../../redis-8.0.2/src/db.c#L1540) | 第 12 章 |
| `SHUTDOWN` | `shutdownCommand` | [db.c:1550](../../redis-8.0.2/src/db.c#L1550) | 第 12 章 |
| `FLUSHDB` | `flushdbCommand` | [db.c:930](../../redis-8.0.2/src/db.c#L930) | 第 3 章 |
| `FLUSHALL` | `flushallCommand` | [db.c:918](../../redis-8.0.2/src/db.c#L918) | 第 3 章 |
| `MEMORY` | (容器 `MEMORY USAGE` 等) | object.c | 第 13 章 / 内存 |

### 事务与脚本(Transactions & Scripting)

| 命令 | proc 函数 | 文件:行号 | 本书章节 |
|------|-----------|-----------|----------|
| `MULTI` | `multiCommand` | [multi.c:92](../../redis-8.0.2/src/multi.c#L92) | 第 15 章 |
| `EXEC` | `execCommand` | [multi.c:128](../../redis-8.0.2/src/multi.c#L128) | 第 15 章 |
| `DISCARD` | `discardCommand` | [multi.c:102](../../redis-8.0.2/src/multi.c#L102) | 第 15 章 |
| `WATCH` | `watchCommand` | [multi.c:459](../../redis-8.0.2/src/multi.c#L459) | 第 15 章 |
| `UNWATCH` | `unwatchCommand` | [multi.c:476](../../redis-8.0.2/src/multi.c#L476) | 第 15 章 |
| `EVAL` | `evalCommand` | [eval.c:627](../../redis-8.0.2/src/eval.c#L627) | 第 16 章 |
| `EVALSHA` | `evalShaCommand` | [eval.c:641](../../redis-8.0.2/src/eval.c#L641) | 第 16 章 |
| `SCRIPT` | `scriptCommand`(容器) | [eval.c:665](../../redis-8.0.2/src/eval.c#L665) | 第 16 章 |
| `FCALL` | `fcallCommand` | [functions.c:656](../../redis-8.0.2/src/functions.c#L656) | 第 16 章 |
| `FCALL_RO` | `fcallroCommand` | [functions.c:663](../../redis-8.0.2/src/functions.c#L663) | 第 16 章 |
| `FUNCTION` | (容器,proc 为 NULL) | functions.c | 第 16 章 |

注:`EVAL`/`EVALSHA` 在 8.0 已迁出 `script.c`,实际定义在新模块 `eval.c`。`FUNCTION` 是 proc=NULL 的容器命令,其子命令处理器散落在 `functions.c`。

### 发布订阅(Pub/Sub) — `pubsub.c`

| 命令 | proc 函数 | 文件:行号 | 本书章节 |
|------|-----------|-----------|----------|
| `SUBSCRIBE` | `subscribeCommand` | [pubsub.c:521](../../redis-8.0.2/src/pubsub.c#L521) | 第 17 章 |
| `UNSUBSCRIBE` | `unsubscribeCommand` | [pubsub.c:540](../../redis-8.0.2/src/pubsub.c#L540) | 第 17 章 |
| `PSUBSCRIBE` | `psubscribeCommand` | [pubsub.c:555](../../redis-8.0.2/src/pubsub.c#L555) | 第 17 章 |
| `PUNSUBSCRIBE` | `punsubscribeCommand` | [pubsub.c:575](../../redis-8.0.2/src/pubsub.c#L575) | 第 17 章 |
| `PUBLISH` | `publishCommand` | [pubsub.c:599](../../redis-8.0.2/src/pubsub.c#L599) | 第 17 章 |
| `PUBSUB` | `pubsubCommand`(容器) | [pubsub.c:612](../../redis-8.0.2/src/pubsub.c#L612) | 第 17 章 |
| `SSUBSCRIBE` | `ssubscribeCommand` | [pubsub.c:707](../../redis-8.0.2/src/pubsub.c#L707) | 第 17 章 / 分片 |
| `SUNSUBSCRIBE` | `sunsubscribeCommand` | [pubsub.c:722](../../redis-8.0.2/src/pubsub.c#L722) | 第 17 章 / 分片 |
| `SPUBLISH` | `spublishCommand` | [pubsub.c:699](../../redis-8.0.2/src/pubsub.c#L699) | 第 17 章 / 分片 |

### 持久化(Persistence)

| 命令 | proc 函数 | 文件:行号 | 本书章节 |
|------|-----------|-----------|----------|
| `SAVE` | `saveCommand` | [rdb.c:4030](../../redis-8.0.2/src/rdb.c#L4030) | 第 18 章 / RDB |
| `BGSAVE` | `bgsaveCommand` | [rdb.c:4048](../../redis-8.0.2/src/rdb.c#L4048) | 第 18 章 / RDB |
| `BGREWRITEAOF` | `bgrewriteaofCommand` | [aof.c:2604](../../redis-8.0.2/src/aof.c#L2604) | 第 18 章 / AOF |

### 复制与集群(Replication & Cluster)

| 命令 | proc 函数 | 文件:行号 | 本书章节 |
|------|-----------|-----------|----------|
| `REPLICAOF` | `replicaofCommand` | [replication.c:3983](../../redis-8.0.2/src/replication.c#L3983) | 第 19 章 |
| `SLAVEOF` | `replicaofCommand`(共用) | [replication.c:3983](../../redis-8.0.2/src/replication.c#L3983) | 第 19 章 |
| `ROLE` | `roleCommand` | [replication.c:4047](../../redis-8.0.2/src/replication.c#L4047) | 第 19 章 |
| `FAILOVER` | `failoverCommand` | [replication.c:4909](../../redis-8.0.2/src/replication.c#L4909) | 第 19 章 |
| `CLUSTER` | `clusterCommand`(容器) | [cluster.c:941](../../redis-8.0.2/src/cluster.c#L941) | 第 20 章 |
| `CLUSTER INFO` / `NODES` / `SLOTS` | `clusterCommand` 子命令分支 | [cluster.c:941](../../redis-8.0.2/src/cluster.c#L941) | 第 20 章 |
| `WAIT` | `waitCommand` | replication.c | 第 19 章 |

注:`CLUSTER` 是单入口容器命令,所有子命令(`CLUSTER NODES`、`CLUSTER MEET`、`CLUSTER SLOTS` 等)都进入 `clusterCommand`,在函数体内按子命令名分发。`SLAVEOF` 已弃用,`commands.def` 中同样绑定 `replicaofCommand`([commands.def:11266](../../redis-8.0.2/src/commands.def#L11266))。

## B.3 命令是怎么被"找到"的(呼应第三章)

读者执行任何一条命令,服务端最终都要**按命令名查到 proc 函数指针**。这条链路的核心是 `server.commands` 字典与 `lookupCommand`:

1. **建表**——`server.c` 的 `populateCommandTable()`([server.c:3170](../../redis-8.0.2/src/server.c#L3170))遍历代码生成的 `redisCommandTable[]`(即 `commands.def` 里那一长串 `MAKE_CMD(...)`),把每条命令以其全名(`SET`、`CLUSTER` 等)为 key、`struct redisCommand *` 为 value,插入 `server.commands` 和 `server.orig_commands` 两个 dict([server.c:3185](../../redis-8.0.2/src/server.c#L3185))。后者保留原始命令表,不受配置文件 `rename-command` 影响。
2. **查找**——处理客户端请求时,`processCommand` 调用 [server.c:3295 `lookupCommand`](../../redis-8.0.2/src/server.c#L3295),它从 `server.commands` 里按命令名 SDS 取出对应 `redisCommand`,进而拿到 `cmd->proc` 函数指针并调用。这就是本书第三章讲的"`lookupCommand` 命中字典 → 取 proc → 执行"。
3. **容器命令**——`CLUSTER`、`CLIENT`、`FUNCTION`、`PUBSUB` 等是容器(proc 为 NULL 或自身分发),它们的子命令(`CLUSTER NODES`、`CLIENT LIST`)在 `commands.def` 中以 `.subcommands=...` 列出,查找时父命令命中后由父 proc(如 `clusterCommand`)按子命令名二次分发。

> 一个常被忽略的事实:`commands/` 下的 JSON 是**人写的描述**,`commands.def` 是**机器生成的注册表**,改了 JSON 要跑 `utils/generate-command-code.py` 重新生成 `commands.def`,改命令逻辑才轮到 `t_*.c`。本书读源码时,只要记住一行——**proc 指针在 `redisCommand` 结构里,值就是上表那个函数**——就能在"命令"与"代码"之间自由穿梭。

## B.4 没列出的命令去哪查

本书只选了 ~110 条最常用、最能串起全书的命令,Redis 8.0.2 实际注册了 200+ 条。没列出的命令,按下面三步定位:

1. 在 `commands.def` 里 `grep '"命令名"'`,读那行 `MAKE_CMD(...)` 里的 proc 字段(倒数第几个参数,形如 `xxxCommand`)。
2. 用 `grep -nE '^void xxxCommand\('` 在 `src/` 下找真实文件与行号。
3. 也可直接 `grep '"命令名"' src/commands/命令名.json`,看 JSON 里的 `since`、`complexity`、`group` 字段确认分类。

掌握这三步,任何 Redis 命令的源码都能在一分钟内定位到。
