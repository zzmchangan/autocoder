# 第十四章 · RDB 持久化:fork 子进程与写时复制

> 篇:P4 持久化
> 主轴呼应:本章是**取向⑤(可靠性)**与**取向①(把耗时从主线程解放)**的联手——持久化的重活交给 fork 出来的子进程,主线程照常服务,靠 COW 白捡一份一致快照,不需要锁、不需要 MVCC、不需要暂停世界。同时它也兑现**取向④(简单优先)**:用一个系统调用 `fork()`,把"冻结快照"和"搬走重活"两件事一并解决,而不是自己造一套并发快照逻辑。

---

## 读完本章你会明白

1. **为什么 Redis 持久化非 `fork()` 不可**——线程共享地址空间会逼你给所有数据结构加锁,等于推倒单线程模型;fork 出来的子进程和父进程天然隔离,COW 让"一致性快照"变成内核赠品。这套方案是 Unix 给的礼物,不是 Redis 自己的发明,但它和 Redis 的取向①完美咬合。
2. **`BGSAVE` 从触发到落盘的全流程是怎么走的**——`serverCron` 检查 `save` 规则 → `rdbSaveBackground` fork 子进程 → 子进程 `rdbSave` 写 `temp-<pid>.rdb` → `rename` 原子替换 `dump.rdb`,父进程毫秒级返回继续接客。
3. **一个 RDB 文件到底长什么样**——9 字节 magic `REDIS0012`、一组 AUX 辅助字段、可选的 FUNCTIONS 段、若干 SELECTDB/RESIZEDB/数据区、EOF 字节、8 字节 CRC64;每个 key 前可能带 EXPIRETIME_MS/IDLE/FREQ 三类前缀 opcode,后面跟 type byte + key + value。这一章会画出完整的字节布局。
4. **加载(`rdbLoad`)是怎么把这份字节流重建回内存的**——它是 `rdbSave` 的镜像,按 opcode 逐个解析;加载时还会做两件 save 端不做的事:**已过期的 key 直接丢弃不入库**、**重复 key 触发 panic**(RDB 逻辑上不应有重复)。
5. **`dismissMemory` 这一个 `madvise(MADV_DONTNEED)` 调用,凭什么能反向利用 COW 把 fork 期间的内存放大压下去**——这是 Redis 源码里最漂亮的几个内核机制利用之一,思路是"子进程写完的对象主动归还物理页,从源头消灭父进程写时触发 COW 的动机"。
6. **`rio` 抽象 + auto-sync + 临时文件 + rename + CRC64 这五个小机制,各自解决了一个独立的可靠性/吞吐问题**——它们单独看都不复杂,叠在一起才构成生产可用的持久化。

---

> **如果一读觉得太难:先只记住三件事**——
> ① RDB = "某一刻的全量内存快照",靠 `fork()` + COW 把写盘的重活搬出主线程、同时白捡一份一致快照;
> ② 一个 RDB 文件 = `REDIS0012` 魔数 + AUX 元信息 + 每个 db 的 SELECTDB/RESIZEDB/数据区 + EOF(`0xFF`) + 8 字节 CRC64;
> ③ 加载是写入的镜像,启动时 `loadDataFromDisk` → `rdbLoad` → `rdbLoadRio` 按 opcode 逐个解析重建内存,过期 key 加载时直接丢弃。
> 这三件事,就是 RDB 的全部。

---

> **一句话点破:Redis 选 fork 不选线程,是因为 fork 之后父子进程物理隔离,子进程靠 COW 看到的内存永远定格在 `fork()` 返回那一刻——这份"一致性"是内核给的、不要钱、不需要锁;代价是 fork 本身要复制页表(瞬时阻塞主线程)、COW 在父进程猛写时内存会放大,这两个坑生产事故最多,本章会讲透。**

---

## 14.1 这块要解决什么:把内存快照安全落盘,又不打断服务

内存数据库有一道原罪:**掉电即归零**。Redis 把全部数据放在内存里(取向②),读写得快是它全部的价值,但这同时意味着进程一退出、机器一断电,所有数据就真的没了。所以持久化不是可选项,而是 Redis 想被用在生产环境的入场券。

持久化要解决的问题很朴素:**在不打断服务的前提下,把某一刻的内存快照安全地写到磁盘**。这句朴素的话里藏着两个互相打架的要求:

- **"不打断服务"** —— 写盘是慢活(几百 MB 的数据可能要写好几秒),而 Redis 主线程是单线程事件循环(取向①),主线程一旦去写盘,这几秒内所有客户端命令都得排队等着,等同于服务挂了。
- **"某一刻的快照、并且一致"** —— 写盘需要时间,写盘期间数据库还在被新命令修改。如果写到一半某个 key 被删了、又被改了,那写出来的就是"半新半旧"的脏数据,恢复出来是错的。

这两条一挤压,设计就被逼到了一个特定的方向上。我们先看 Redis **没有**选什么,再看它选了什么,这是理解 RDB 的钥匙。

**不选①:主线程自己写盘。** 直接 `rdbSave` 在主线程跑(对应 [`saveCommand`](../../redis-8.0.2/src/rdb.c#L4030)),写盘期间整个 Redis 阻塞,这就是为什么线上严禁用 `SAVE` 命令——它就是给 debug 和 shutdown 用的。`SAVE` 这条命令的存在本身就是为了反衬"为什么必须有 `BGSAVE`"。

**不选②:开一个工作线程去写盘。** 这是 Java/Go 世界最自然的想法,但 Redis 没这么干,原因深一层:线程和主线程**共享同一片内存地址空间**。工作线程一边遍历哈希表一边写盘,主线程一边在改这个哈希表(插入、删除、rehash),两边踩同一块内存,不全程加锁就会读到撕裂的数据,加锁就把单线程模型的好处(无锁、无竞争、可重入)全废了。Redis 内部数据结构(listpack、intset、dict)全是"为了单线程无锁"设计的,塞进多线程里等于推倒重来。

**选③:fork 一个子进程。** 这是 Unix 给的礼物,也是 Redis 持久化设计的命脉。`fork()` 出来的子进程是父进程的"克隆",但它和父进程**共享物理内存页**(不是立刻拷贝),然后子进程可以慢悠悠地把这堆内存写进 `dump.rdb`,父进程继续接客。父子进程之间天然隔离——子进程改不了父进程的内存,父进程改不了子进程的内存,根本不需要锁。配合下一节讲的写时复制,子进程看到的永远是 fork 那一刻的快照,一致性白送。

所以"为什么是 fork"这个问题,真正的答案是:**fork 是唯一能在不引入多线程锁的前提下,既把数据快照冻结、又把写盘的重活搬出主线程的办法。** 它把"持久化"这件耗时的事(取向①要解放的东西)和"保证一致"这件可靠性要求(取向⑤)用一个系统调用同时解决了。

> **不这样会怎样**:如果选线程方案,Redis 必须给 dict/listpack/intset 全部加读写锁。dict 在 rehash 时尤其敏感——rehash 进行到一半,工作线程遍历到的可能是"旧表已删、新表未填"的中间态。要么加全局锁串行化(吞吐归零),要么自己造一套 MVCC/快照隔离逻辑(巨复杂且易错)。fork + COW 用一个系统调用把这一切全绕过去了。

## 14.2 fork 的真身:`rdbSaveBackground` 与父子的分工

无论哪条路径触发 RDB(配置规则、`BGSAVE` 命令、`SHUTDOWN`、主从全量同步),最终都汇聚到同一个函数 [`rdbSaveBackground`](../../redis-8.0.2/src/rdb.c#L1642)。这个函数本身极短,因为它只干一件事——fork,然后把活全甩给子进程:

```c
/* rdb.c:1642-1680,精简 */
int rdbSaveBackground(int req, char *filename, rdbSaveInfo *rsi, int rdbflags) {
    pid_t childpid;

    if (hasActiveChildProcess()) return C_ERR;   /* 已有子进程在跑就不开 */
    server.stat_rdb_saves++;
    server.dirty_before_bgsave = server.dirty;   /* 记下 fork 前的 dirty */
    server.lastbgsave_try = time(NULL);

    if ((childpid = redisFork(CHILD_TYPE_RDB)) == 0) {
        /* —— 以下是子进程 —— */
        int retval;
        redisSetProcTitle("redis-rdb-bgsave");
        redisSetCpuAffinity(server.bgsave_cpulist);
        retval = rdbSave(req, filename,rsi,rdbflags);   /* 干活的全在这里 */
        if (retval == C_OK)
            sendChildCowInfo(CHILD_INFO_TYPE_RDB_COW_SIZE, "RDB"); /* 上报 COW 内存 */
        exitFromChild((retval == C_OK) ? 0 : 1);
    } else {
        /* —— 以下是父进程 —— */
        if (childpid == -1) { server.lastbgsave_status = C_ERR; return C_ERR; }
        serverLog(LL_NOTICE,"Background saving started by pid %ld",(long) childpid);
        server.rdb_save_time_start = time(NULL);
        server.rdb_child_type = RDB_CHILD_TYPE_DISK;
        return C_OK;
    }
}
```

注意父进程分支几乎什么都不做——记录一下开始时间、设个状态就 `return` 了。fork 之后父进程立刻回去继续服务客户端,毫秒级返回。这正是取向①"把耗时从主线程解放"的活样本:重活在子进程,主线程 fork 完就撒手。

`hasActiveChildProcess()` 那一行的检查很关键:Redis 同一时刻**只允许一个子进程**在跑(BGSAVE、BGREWRITEAOF、模块 fork、异步 lazyfree 共用这个名额)。这避免了"BGSAVE 写盘 + BGREWRITEAOF 写盘 + COW 放大"三件事叠加把内存打爆。哪个先抢到名额哪个先跑,另一个会等。

真正调用 `fork()` 的是 [`redisFork`](../../redis-8.0.2/src/server.c#L6760),它在 `fork()` 前后包了一层:打开父子进程通信的管道、在子进程里调 OOM 分数调整、设信号处理器。关键代码:

```c
/* server.c:6760 附近 */
if ((childpid = fork()) == 0) {
    /* Child. */
    server.in_fork_child = purpose;
    setupChildSignalHandlers();
    setOOMScoreAdj(CONFIG_OOM_BGCHILD);
    updateDictResizePolicy();
    dismissMemoryInChild();   /* ← 见 14.6 节:COW 优化 */
    ...
}
```

四件事各司其职:`server.in_fork_child` 标志位告诉通用路径"我是 fork 子进程,可以更激进地归还内存"(14.6 节会用);`setOOMScoreAdj(CONFIG_OOM_BGCHILD)` 把子进程的 OOM 分数调低,万一内存吃紧内核优先杀子进程保护主服务(为 14.8 节"COW 内存放大"那个坑兜底);`updateDictResizePolicy()` 在子进程里禁掉 dict 的 rehash——子进程只是读快照写盘,绝不能在写盘过程中触发 rehash 影响遍历正确性;`dismissMemoryInChild()` 是 14.6 节的主角:子进程主动把内存标记为 `MADV_DONTNEED`,从源头降低 COW,这里先按下不表。

> **钉死这件事**:`rdbSaveBackground` 的设计是教科书式的"fork + 分工"——父进程只负责 fork 和记账,所有重活留给子进程。父进程从 `redisFork` 返回到 `return C_OK` 只有寥寥几行,这意味着无论 BGSAVE 持续多久(几秒到几分钟),主线程被它阻塞的时间只等于 `fork()` 本身的耗时(复制页表,见 14.8 节)。这是"把耗时从主线程解放"的最纯粹形态:重活在子进程,主线程只付一次 fork 的代价。

## 14.3 触发点:`save` 配置规则的判定

RDB 的触发有四条路径:`save` 配置规则自动触发、`BGSAVE` 命令手动触发、`SHUTDOWN` 时触发、主从全量同步时触发。前三条最终都汇聚到 `rdbSaveBackground`,第四条稍有不同(走 socket 而非文件)。

最常用的是配置文件里的 `save` 规则,比如默认的:

```
save 3600 1     # 3600 秒内有 1 次改动 → 触发
save 300 100    # 300 秒内有 100 次改动 → 触发
save 60 10000   # 60 秒内有 10000 次改动 → 触发
```

这三条是"或"的关系——任意一条满足就触发。它们在 Redis 的周期任务 `serverCron`(每秒跑几次)里被检查。核心判定逻辑在 [`server.c:1504`](../../redis-8.0.2/src/server.c#L1504):

```c
/* server.c:1504-1525,精简 */
for (j = 0; j < server.saveparamslen; j++) {
    struct saveparam *sp = server.saveparams+j;
    /* 改动数 >= 规则要求 && 距上次成功保存的时间 > 规则秒数
     * && (上次失败但已过重试间隔 || 上次成功) */
    if (server.dirty >= sp->changes &&
        server.unixtime-server.lastsave > sp->seconds &&
        (server.unixtime-server.lastbgsave_try >
         CONFIG_BGSAVE_RETRY_DELAY ||
         server.lastbgsave_status == C_OK))
    {
        serverLog(LL_NOTICE,"%d changes in %d seconds. Saving...",
            sp->changes, (int)sp->seconds);
        rdbSaveBackground(SLAVE_REQ_NONE,server.rdb_filename,rsiptr,RDBFLAGS_NONE);
        break;
    }
}
```

三个条件一起判断:`server.dirty` 是自上次保存以来的累计改动数(每次写命令都会给 `dirty` 加一),`lastsave` 是上次成功保存的时间戳。

注意最后那个"重试间隔"条件:如果上一次 BGSAVE 失败了(比如磁盘满),Redis 不会立刻疯了一直重试,而是等够 `CONFIG_BGSAVE_RETRY_DELAY` 秒再说——这是一个小而实在的可靠性细节,避免故障被放大。`break` 那一行说明多条规则按顺序检查,**一旦某条触发就不再检查其它**(避免一次 cron 同时触发多个 BGSAVE)。

> **钉死这件事**:`save` 规则用"改动数 × 时间窗口"二维判定,本质是"积攒够多改动再保存"和"超过一定时间没保存就保存一次"的折中。`dirty` 计数器是这条逻辑的物理载体——每条写命令在命令处理函数里都会调 `server.dirty++`(或加若干),这是 RDB 触发判定与命令执行路径的耦合点。配 `save ""`(空字符串)就关掉自动 RDB,纯内存模式。

## 14.4 子进程干的全活:`rdbSave` → `rdbSaveRio`

子进程拿到控制权后调 [`rdbSave`](../../redis-8.0.2/src/rdb.c#L1599)。它先写一个临时文件 `temp-<pid>.rdb`,写完后用 `rename` 原子替换掉正式文件:

```c
/* rdb.c:1599-1635,精简 */
int rdbSave(int req, char *filename, rdbSaveInfo *rsi, int rdbflags) {
    char tmpfile[256];
    startSaving(rdbflags);
    snprintf(tmpfile,256,"temp-%d.rdb", (int) getpid());   /* 临时文件名带 pid */

    if (rdbSaveInternal(req,tmpfile,rsi,rdbflags) != C_OK) { /* 实际写盘 */
        stopSaving(0);
        return C_ERR;
    }

    /* rename 是原子的:要么整个成功,要么文件还是旧的那个 */
    if (rename(tmpfile,filename) == -1) { ... return C_ERR; }
    if (fsyncFileDir(filename) != 0) { ... return C_ERR; }  /* 同步目录,保证 rename 落盘 */

    server.dirty = 0;                  /* 重置脏计数 */
    server.lastsave = time(NULL);      /* 记录本次保存时间 */
    server.lastbgsave_status = C_OK;
    return C_OK;
}
```

`rename` 这个细节值得停一下:为什么先写临时文件再 rename?因为直接覆写 `dump.rdb`,如果写到一半进程崩了,留下的就是个半截文件,比没持久化还糟——下次启动会加载出错。先写 `temp-xxx.rdb`,写完整才原子地换名字,这样 `dump.rdb` 永远要么是上一份完整的、要么是新一份完整的,不存在"半截"状态。

更精确地说,`rename` 在同一文件系统上是原子的(POSIX 保证),它只改目录项,不动文件内容。哪怕此刻机器断电,重启后目录项要么指向旧 `dump.rdb`、要么指向新 `dump.rdb`,绝不会指向半截文件。这是可靠性(取向⑤)最朴素也最有效的招式。

那 `fsyncFileDir(filename)` 又是干什么的?`rename` 改的是**目录**这个数据结构本身(目录也是文件),而目录的修改也需要落盘。如果只 rename 不同步目录,机器断电后可能 rename 这个操作本身就丢了——目录项还在旧文件名上。所以 `fsyncFileDir` 调 `fsync` 同步 `dump.rdb` 所在的目录文件,把 rename 的元数据改动也持久化掉。这是"崩溃一致性"的标准做法(LevelDB/etcd 写 WAL 也都 fsync 目录),不能省。

真正把数据库变字节流的是 [`rdbSaveRio`](../../redis-8.0.2/src/rdb.c#L1458),它定义了 RDB 文件的完整格式。这一节是本章的核心,我们一段段拆:

```c
/* rdb.c:1458-1496,精简 */
int rdbSaveRio(int req, rio *rdb, int *error, int rdbflags, rdbSaveInfo *rsi) {
    char magic[10];
    uint64_t cksum;
    long key_counter = 0;
    int j;

    if (server.rdb_checksum)
        rdb->update_cksum = rioGenericUpdateChecksum;   /* 开启 CRC64 */

    snprintf(magic,sizeof(magic),"REDIS%04d",RDB_VERSION); /* 魔数:REDIS0012 */
    if (rdbWriteRaw(rdb,magic,9) == -1) goto werr;                         /* ① */
    if (rdbSaveInfoAuxFields(rdb,rdbflags,rsi) == -1) goto werr;           /* ② AUX */
    if (!(req & SLAVE_REQ_RDB_EXCLUDE_DATA) &&
        rdbSaveModulesAux(rdb, REDISMODULE_AUX_BEFORE_RDB) == -1) goto werr; /* ③ module before */

    if (!(req & SLAVE_REQ_RDB_EXCLUDE_FUNCTIONS) &&
        rdbSaveFunctions(rdb) == -1) goto werr;                            /* ④ functions */

    if (!(req & SLAVE_REQ_RDB_EXCLUDE_DATA)) {
        for (j = 0; j < server.dbnum; j++) {
            if (rdbSaveDb(rdb, j, rdbflags, &key_counter) == -1) goto werr; /* ⑤ 每个 db */
        }
    }

    if (!(req & SLAVE_REQ_RDB_EXCLUDE_DATA) &&
        rdbSaveModulesAux(rdb, REDISMODULE_AUX_AFTER_RDB) == -1) goto werr; /* ⑥ module after */

    if (rdbSaveType(rdb,RDB_OPCODE_EOF) == -1) goto werr;                  /* ⑦ EOF=0xFF */

    /* 末尾 8 字节 CRC64 校验和;关闭校验时这里是 0,加载时跳过检查 */
    cksum = rdb->cksum;
    memrev64ifbe(&cksum);
    if (rioWrite(rdb,&cksum,8) == 0) goto werr;                            /* ⑧ */
    return C_OK;
}
```

八段顺序写入,一一对应到 RDB 文件的字节布局。注意一个常被讲错的细节:**`rdb->update_cksum = rioGenericUpdateChecksum` 一旦挂上,之后所有的 `rioWrite` 都会顺带更新 `rdb->cksum`**(由 rio 的内联实现保证,见 14.7 节),所以最后写出的 `cksum` 覆盖了**除这 8 字节本身之外**的整段文件。CRC64 是织进每一次写入的,不需要业务代码关心。

> **钉死这件事**:`rdbSaveRio` 是 RDB 文件格式的定义者。它的八段顺序——magic、AUX、module before、functions、各 db、module after、EOF、CRC64——一字不差地决定了 14.5 节那张字节布局图,也一字不差地对应 14.9 节 `rdbLoadRioWithLoadingCtx` 的 opcode 解析链(save 端写什么顺序,load 端就按什么顺序读)。**改这段代码就等于改 RDB 文件版本号**,所以 RDB_VERSION 在 [`rdb.h:21`](../../redis-8.0.2/src/rdb.h#L21) 定义为 12,任何"破坏向后兼容"的格式改动都要让它递增。

## 14.5 RDB 文件字节布局:一个完整的二进制格式

这一节是本章最直观的部分,我们把 14.4 节的八段顺序展开成具体的字节布局,并解释每个 opcode 的含义和值。先看全貌:

```text
偏移      内容                                opcode/格式                              说明
─────────────────────────────────────────────────────────────────────────────────────────
0         "REDIS0012"                        9 字节 ASCII,REDIS%04d 格式化 RDB_VERSION  魔数+版本
9..       AUX 辅助字段(若干个)              每个: RDB_OPCODE_AUX=250 (1B) + key + val   元信息
..        MODULE_AUX(若干个,模块才有)        RDB_OPCODE_MODULE_AUX=247 (1B)+...         模块自定义 before
..        FUNCTIONS(可选)                    RDB_OPCODE_FUNCTION2=245 (1B) + 库代码      函数库(Lua/Functions)
..        ┌─ DB 0 段 ────────────────────────────────────────────────────────────┐
..        │  RDB_OPCODE_SELECTDB=254 (1B) + dbid (length 编码)                    │
..        │  RDB_OPCODE_RESIZEDB=251 (1B) + db_size + expires_size (两个 length) │
..        │  [cluster] RDB_OPCODE_SLOT_INFO=244 (1B)+slot_id+slot_size+exp_size  │
..        │  ── 重复 db_size 次 ──                                                │
..        │  [可选] RDB_OPCODE_EXPIRETIME_MS=252 (1B) + 8B ms 时间戳            │
..        │  [可选] RDB_OPCODE_IDLE=248 (1B) + LRU 空闲秒数                      │
..        │  [可选] RDB_OPCODE_FREQ=249 (1B) + 1B LFU 计数                       │
..        │  type byte (0~25,1B) + key (len+data) + value (按 type 序列化)        │
..        └────────────────────────────────────────────────────────────────────┘
..        DB 1 段、DB 2 段、... (空 db 整段跳过,rdb.c:1381)
..        MODULE_AUX after(若干个,模块才有)
..        RDB_OPCODE_EOF = 255 (1B,即 0xFF)
end-8..   CRC64 (8 字节,小端;rdb_checksum=no 时为 8 个 0)
```

这就是 `rdbSaveRio` 八段顺序在字节流上的展开。下面把每个 opcode 单独讲清楚。

### 14.5.1 所有 opcode 的字节值清单

opcode 是单字节(0-255),其中 **0-243 范围与 type byte 共用**(实际 type 只用到 0-25),**244-255 是专用 opcode**。定义在 [`rdb.h:87-98`](../../redis-8.0.2/src/rdb.h#L87):

| opcode 宏 | 值 | 行号 | 含义 | 8.0 是否写出 |
|---|---|---|---|---|
| `RDB_OPCODE_SLOT_INFO` | 244 | rdb.h:87 | cluster 模式下每个 slot 的 key 数/expires 数 | 仅 cluster |
| `RDB_OPCODE_FUNCTION2` | 245 | rdb.h:88 | 函数库(Lua/Functions) | 有 functions 才写 |
| `RDB_OPCODE_FUNCTION_PRE_GA` | 246 | rdb.h:89 | 7.0 RC1/RC2 旧函数库 | **只读不写** |
| `RDB_OPCODE_MODULE_AUX` | 247 | rdb.h:90 | 模块自定义辅助数据 | 模块注册了才写 |
| `RDB_OPCODE_IDLE` | 248 | rdb.h:91 | key 的 LRU 空闲秒数 | 仅 LRU 策略写 |
| `RDB_OPCODE_FREQ` | 249 | rdb.h:92 | key 的 LFU 频率(1 字节) | 仅 LFU 策略写 |
| `RDB_OPCODE_AUX` | 250 | rdb.h:93 | RDB 辅助字段 | 总是写若干个 |
| `RDB_OPCODE_RESIZEDB` | 251 | rdb.h:94 | db 大小 hint,加载时预分配哈希表 | 每个 db 段开头写 |
| `RDB_OPCODE_EXPIRETIME_MS` | 252 | rdb.h:95 | 过期时间(8 字节毫秒) | **8.0 唯一写过期的 opcode** |
| `RDB_OPCODE_EXPIRETIME` | 253 | rdb.h:96 | 旧版过期(4 字节秒级) | **8.0 只读不写** |
| `RDB_OPCODE_SELECTDB` | 254 | rdb.h:97 | db 号切换 | 每个 db 段开头写 |
| `RDB_OPCODE_EOF` | 255 | rdb.h:98 | 文件数据区结束 | 总是写 |

**一处常被讲错的事**:`RDB_OPCODE_EXPIRETIME`(253)在 8.0 写出端**完全不出现**,写出端只用 `RDB_OPCODE_EXPIRETIME_MS`(252)+8 字节毫秒。253 只保留给加载端读旧版 RDB(4 字节秒级)。证据在 [`rdb.c:114-118`](../../redis-8.0.2/src/rdb.c#L114) 注释:"This is only used to load old databases stored with the RDB_OPCODE_EXPIRETIME opcode. New versions of Redis store using the RDB_OPCODE_EXPIRETIME_MS opcode."

### 14.5.2 AUX 辅助字段:RDB 文件的"文件头元信息"

AUX 段紧接在 magic 之后,由若干个 `RDB_OPCODE_AUX` 单元组成,每个单元格式是:

```text
[ RDB_OPCODE_AUX = 250 (1B) ] [ key (rdbSaveRawString) ] [ val (rdbSaveRawString) ]
```

`rdbSaveRawString` 内部会先尝试整数编码/压缩,不行就用 `[rdbSaveLen(len)][raw bytes]`。所以 AUX 的 key 名(短字符串)通常走最紧凑的 length 编码。

写入哪些 AUX 字段由 [`rdbSaveInfoAuxFields`](../../redis-8.0.2/src/rdb.c#L1264) 决定,完整清单:

```c
/* rdb.c:1264-1285,精简 */
int rdbSaveInfoAuxFields(rio *rdb, int rdbflags, rdbSaveInfo *rsi) {
    int redis_bits = (sizeof(void*) == 8) ? 64 : 32;
    int aof_base = (rdbflags & RDBFLAGS_AOF_PREAMBLE) != 0;

    if (rdbSaveAuxFieldStrStr(rdb,"redis-ver",REDIS_VERSION) == -1) return -1;  /* 总是写 */
    if (rdbSaveAuxFieldStrInt(rdb,"redis-bits",redis_bits) == -1) return -1;    /* 总是写 */
    if (rdbSaveAuxFieldStrInt(rdb,"ctime",time(NULL)) == -1) return -1;         /* 总是写 */
    if (rdbSaveAuxFieldStrInt(rdb,"used-mem",zmalloc_used_memory()) == -1) return -1; /* 总是写 */

    if (rsi) {                                                                  /* 复制场景才写 */
        if (rdbSaveAuxFieldStrInt(rdb,"repl-stream-db",rsi->repl_stream_db) == -1) return -1;
        if (rdbSaveAuxFieldStrStr(rdb,"repl-id",server.replid) == -1) return -1;
        if (rdbSaveAuxFieldStrInt(rdb,"repl-offset",server.master_repl_offset) == -1) return -1;
    }
    if (rdbSaveAuxFieldStrInt(rdb, "aof-base", aof_base) == -1) return -1;      /* 总是写 */
    return 1;
}
```

| 字段名 | 值 | 写入条件 | 加载时怎么用 |
|---|---|---|---|
| `redis-ver` | 编译期常量如 `"8.0.2"` | 总是写 | 仅日志 |
| `redis-bits` | `64` 或 `32`(指针宽度) | 总是写 | 校验位宽 |
| `ctime` | `time(NULL)` Unix 秒 | 总是写 | 仅日志 |
| `used-mem` | `zmalloc_used_memory()` 字节 | 总是写 | 对比加载前后内存 |
| `repl-stream-db` | 复制流所在 db | 仅 `rsi != NULL`(复制场景) | 填 `rsi->repl_stream_db` |
| `repl-id` | 40 字符 hex | 仅 `rsi != NULL` | 填 `rsi->repl_id`,复制接力 |
| `repl-offset` | master 复制偏移 | 仅 `rsi != NULL` | 填 `rsi->repl_offset` |
| `aof-base` | `0` 或 `1` | 总是写 | 加载时知道这份 RDB 是不是 AOF 的 base |

AUX 段的核心价值是"**自描述 + 向后兼容**"。新加字段直接 append 一个 AUX 单元,老版本加载端遇到不认识的 key 就跳过(`rdb.c:3444` 的 `strcasecmp` 链对未知字段走 DEBUG 日志静默丢弃),不会破坏加载。`redis-check-rdb` 工具就是靠读 AUX 段打印这些信息的。

### 14.5.3 SELECTDB + RESIZEDB:每个 db 的头

每个非空 db 段以 `SELECTDB` 开头、紧接 `RESIZEDB`。看 [`rdbSaveDb`](../../redis-8.0.2/src/rdb.c#L1371):

```c
/* rdb.c:1371-1396,精简 */
ssize_t rdbSaveDb(rio *rdb, int dbid, int rdbflags, long *key_counter) {
    redisDb *db = server.db + dbid;
    unsigned long long int db_size = kvstoreSize(db->keys);
    if (db_size == 0) return 0;                        /* 空 db 直接跳过,不写任何字节 */

    /* Write the SELECT DB opcode */
    if ((res = rdbSaveType(rdb,RDB_OPCODE_SELECTDB)) < 0) goto werr;   /* 254, 1B */
    if ((res = rdbSaveLen(rdb, dbid)) < 0) goto werr;                  /* dbid 用 length 编码 */

    /* Write the RESIZE DB opcode. */
    unsigned long long expires_size = kvstoreSize(db->expires);
    if ((res = rdbSaveType(rdb,RDB_OPCODE_RESIZEDB)) < 0) goto werr;   /* 251, 1B */
    if ((res = rdbSaveLen(rdb,db_size)) < 0) goto werr;                /* 主表大小 */
    if ((res = rdbSaveLen(rdb,expires_size)) < 0) goto werr;           /* 过期表大小 */
    ...
}
```

注意三个细节:

第一,**空 db 整段跳过**(`if (db_size == 0) return 0`)。8.0 不会为空 db 写 SELECTDB/RESIZEDB,这避免了"16 个 db 都写空 header"的浪费。加载端怎么知道某个 db 是空的?它根本读不到那个 dbid 的 SELECTDB,自然不会切过去——默认 db 还停留在加载的上一个 db,逻辑上正确。

第二,`dbid` 用 `rdbSaveLen` 写入,是**变长 length 编码**,不是固定 4 字节。dbid 0-63 占 1 字节(6bit 编码),64-16383 占 2 字节(14bit 编码)。绝大多数场景 dbid 就是 0,1 字节搞定。

第三,**RESIZEDB 是给加载端的 hint**(提示),不是必需信息。它告诉加载方"这个 db 大概有 db_size 个 key、expires_size 个过期项",加载方据此一次性 `dbExpand` 预分配哈希表大小,避免边加载边 rehash。如果没有这个 hint,加载 100 万个 key 的过程中 dict 可能 rehash 十几次,每次 rehash 都是双倍内存 + 一次迁移,加载速度大打折扣。这是"**为加载端优化**"的典型设计——保存时多写两个字段,加载时省下大量 rehash。

### 14.5.4 数据区:每个 key-value 的字节布局

每个 key-value 在数据区是一组连续字节,可能带三类"前缀 opcode"(过期/LRU/LFU),然后是 type byte + key + value。完整定义在 [`rdbSaveKeyValuePair`](../../redis-8.0.2/src/rdb.c#L1196)(注意:`rdb.h:151` 只是声明,定义在 `rdb.c:1196`):

```c
/* rdb.c:1196-1235,精简 */
int rdbSaveKeyValuePair(rio *rdb, robj *key, robj *val, long long expiretime, int dbid) {
    int savelru = server.maxmemory_policy & MAXMEMORY_FLAG_LRU;
    int savelfu = server.maxmemory_policy & MAXMEMORY_FLAG_LFU;

    /* Save the expire time */
    if (expiretime != -1) {
        if (rdbSaveType(rdb,RDB_OPCODE_EXPIRETIME_MS) == -1) return -1;   /* 252, 1B */
        if (rdbSaveMillisecondTime(rdb,expiretime) == -1) return -1;      /* 8B 小端 */
    }

    /* Save the LRU info. */
    if (savelru) {
        uint64_t idletime = estimateObjectIdleTime(val);
        idletime /= 1000; /* Using seconds is enough and requires less space.*/
        if (rdbSaveType(rdb,RDB_OPCODE_IDLE) == -1) return -1;            /* 248, 1B */
        if (rdbSaveLen(rdb,idletime) == -1) return -1;                   /* length 编码的秒数 */
    }

    /* Save the LFU info. */
    if (savelfu) {
        uint8_t buf[1];
        buf[0] = LFUDecrAndReturn(val);
        if (rdbSaveType(rdb,RDB_OPCODE_FREQ) == -1) return -1;            /* 249, 1B */
        if (rdbWriteRaw(rdb,buf,1) == -1) return -1;                     /* 1B 8-bit 计数 */
    }

    /* Save type, key, value */
    if (rdbSaveObjectType(rdb,val) == -1) return -1;     /* type byte 0~25, 1B */
    if (rdbSaveStringObject(rdb,key) == -1) return -1;   /* [len][key bytes] */
    if (rdbSaveObject(rdb,val,key,dbid) == -1) return -1;/* value,结构随 type 变化 */
    ...
    return 1;
}
```

单个 key-value 的字节布局(按写入顺序):

```text
[ OPCODE_EXPIRETIME_MS  1B = 252 ]   ┐ 仅当 expiretime != -1
[ expiretime            8B LE int64 ] ┘
[ OPCODE_IDLE           1B = 248 ]   ┐ 仅当 maxmemory_policy 命中 LRU
[ idletime_sec          length编码 ] ┘
[ OPCODE_FREQ           1B = 249 ]   ┐ 仅当 maxmemory_policy 命中 LFU
[ freq_counter          1B uint8 ]   ┘
[ type byte             1B ]           rdbSaveObjectType, 0~25
[ key                   [len][data] ]  rdbSaveStringObject
[ value                 type 决定 ]   rdbSaveObject
```

三类前缀 opcode(EXPIRETIME_MS / IDLE / FREQ)的顺序是**固定的:过期 → LRU → LFU**。LRU 和 LFU 是 `maxmemory_policy` 的互斥标志位(只能选一个淘汰策略),所以实际上 IDLE 和 FREQ 不会同时出现。但代码用两个独立 `if` 而非 `if/else`,这是为了未来灵活性——万一某天策略同时含两个标志也能工作。

type byte 写在所有前缀 opcode 之后、key/value 之前。这是 RDB 格式的核心约定:加载端 `rdbLoadType` 先读 1 字节,**如果是 244-255 就是 opcode 进对应分支,否则当 type byte 处理**(进 KV 加载分支)。这种"opcode 和 type 共用一个字节空间"的设计让格式紧凑——大多数 key 没有过期、没开淘汰策略,它的数据区就只有 `type + key + value` 三段,没有任何冗余的 opcode 标记。

> **钉死这件事**:RDB 的格式设计是"**opcode 标记可选属性 + 共享字节空间**"。前缀 opcode 只在属性存在时才写,这让"无过期、无淘汰策略"的普通 key(占绝大多数)只需要 `type + key + value` 三段,极省空间。type byte 用 0-25 这个低值区,opcode 用 244-255 这个高值区,加载端靠"读一个字节判断它落在哪个区"来分流。这是 Redis 在二进制格式上的"省字节"功夫——和 listpack/intset 的紧凑编码是同一个取向②(内存即数据库)的延伸:存储格式也要抠字节。

## 14.6 精妙技巧①:COW 是一致性的根基,dismissMemory 是反向利用 COW

### 14.6.1 COW 为什么能保一致性

回到开头那个核心问题:fork 之后父子共享内存,父进程还在不停地改,子进程怎么保证看到的是"快照"?

答案是操作系统的**写时复制(Copy-On-Write,COW)**。`fork()` 之后,内核并不真的复制父进程的全部内存给子进程,而是让父子进程的页表指向**同一批物理页**,并把这些页都标记为只读。只要没人写,这批页就是一份,父子都读得到。

一旦父进程(或子进程)试图写某个页,因为页是只读的,CPU 触发缺页异常(page fault),内核介入:**复制这一页**出一个新物理页,让写操作发生在新页上,另一方的页表还指向旧页。于是:

- 父进程改一个 key → 这个 key 所在的那一页被复制 → 父进程看到新值,子进程页表还指旧页 → 子进程看到的还是旧值。
- 没被父进程改的 key → 父子共享同一物理页 → 省内存。

所以子进程在 `rdbSave` 全程,它通过自己的页表读到的内存内容,**永远定格在 `fork()` 返回的那一瞬间**。这不是 Redis 自己实现的,是 Linux 内核给的保证。Redis 只要用 fork,就白捡了一个一致的全量快照,不需要任何锁、不需要暂停主线程、不需要自己记录变更日志。

这就是为什么 Redis 选 fork 而不是线程:**COW 让"一致性"变成操作系统级别的免费赠品**。如果是线程,要么加锁(废掉单线程模型),要么自己实现一套 MVCC/快照逻辑(巨复杂且易错)。

### 14.6.2 dismissMemory:反向利用 COW 把内存放大压下去

COW 是把双刃剑(见 14.8 节"两个坑")。Redis 在源码里埋了一个很漂亮的优化来缓解其中一面——子进程在序列化完一个大对象后,主动把它"丢弃"。看 [`server.c:6847`](../../redis-8.0.2/src/server.c#L6847):

```c
/* server.c:6847-6853 */
/* 试图把页直接还给 OS(绕过分配器),以降低 fork 期间的 COW。
 * 对于小对象,凑不满一页就放不掉。*/
void dismissMemory(void* ptr, size_t size_hint) {
    if (ptr == NULL) return;
    /* madvise(MADV_DONTNEED) 对太小的内存放不掉,半页以下直接放弃 */
    if (size_hint && size_hint <= server.page_size/2) return;
    zmadvise_dontneed(ptr);   /* 关键:madvise(MADV_DONTNEED) */
}
```

`madvise(MADV_DONTNEED)` 告诉内核:这片内存我暂时不要了,内核可以立刻把对应的物理页回收(下次访问会重新给一个零页)。为什么这能降低 COW?想清楚这个因果链:

子进程遍历数据库写盘,写完第一个大 hash,这个 hash 的内存子进程以后再也不会读了。如果什么都不做,父进程在 BGSAVE 期间一旦修改了这个 hash(它有权修改,因为父进程页表可写),内核就要为子进程复制那几页——但子进程根本不需要这些页了,复制纯属浪费。所以子进程写完一个对象就 `madvise` 掉它,内核直接把子进程侧的物理页回收;之后父进程再改这个对象时,因为子进程那一侧已经没有"需要保护的旧页"了,**根本不会触发 COW 复制**。

这是一个反向利用 COW 机制的精妙设计:**COW 的代价来自"子进程需要保留旧页",那我就主动告诉内核"这些旧页我不要了",从源头把复制动机消灭掉**。

`dismissObject` 只在 `server.in_fork_child` 为真时调用([`rdb.c:1429`](../../redis-8.0.2/src/rdb.c#L1429)),正常路径(比如主线程的 `SAVE`)不调,因为那时候没有 COW 问题——主线程自己写的对象自己还要用。

```c
/* rdb.c:1425-1429 */
/* In fork child process, we can try to release memory back to the
 * OS and possibly avoid or decrease COW. We give the dismiss
 * mechanism a hint about an estimated size of the object we stored. */
size_t dump_size = rdb->processed_bytes - rdb_bytes_before_key;
if (server.in_fork_child) dismissObject(o, dump_size);
```

注意一个细节:`dismissObject` 传入的是 `dump_size`(刚写出的字节数)作为"size hint",这是给 `dismissMemory` 判断"这片内存够不够大、值不值得 madvise"用的。半页以下(默认页大小 4KB,即 2KB 以下)直接跳过——因为 madvise 是按页对齐的,凑不满一页放不掉,调它纯属浪费 syscall。

> **钉死这件事**:`dismissMemory` 是 Redis 把"COW 的内存放大"这个坑反向利用的妙手。它的思路不是"减少父进程的写"(做不到),也不是"减少子进程的读"(子进程必须遍历读完),而是"**让子进程在读完之后立刻归还物理页**"——这样父进程后续修改这些页时,内核发现子进程那一侧已经没有需要保留的旧页,根本不复制。`madvise(MADV_DONTNEED)` 这一个系统调用,把 fork 期间的 COW 内存放大从"理论最坏翻倍"压到"接近实际写入量"。这是 Redis 源码里对内核机制利用最漂亮的一处,和 Linux mm 子系列的 `MADV_DONTNEED` 回收路径是同一个机制的两面。

## 14.7 精妙技巧②:rio 抽象、auto-sync 与 CRC64 三件套

### 14.7.1 rio:一个极简的 I/O 抽象层

[`rio`](../../redis-8.0.2/src/rio.h#L33) 是 Redis 自己写的一个极简 I/O 抽象层,只有几个函数指针(`read`/`write`/`tell`/`flush`/`update_cksum`),但它的复用价值极大。结构体定义在 `rio.h:33`:

```c
/* rio.h:33-97,精简 */
struct _rio {
    /* 后端函数:返回 0 表示失败,非 0 表示成功 */
    size_t (*read)(struct _rio *, void *buf, size_t len);
    size_t (*write)(struct _rio *, const void *buf, size_t len);
    off_t (*tell)(struct _rio *);
    int (*flush)(struct _rio *);
    void (*update_cksum)(struct _rio *, const void *buf, size_t len);

    uint64_t cksum, flags;
    size_t processed_bytes;
    size_t max_processing_chunk;

    union { /* 五种后端,共用同一片内存 */
        struct { sds ptr; off_t pos; } buffer;                                          /* 内存 buffer */
        struct { FILE *fp; off_t buffered; off_t autosync; unsigned reclaim_cache:1; } file; /* stdio FILE */
        struct { connection *conn; off_t pos; sds buf; size_t read_limit; size_t read_so_far; } conn; /* socket 连接 */
        struct { int fd; off_t pos; sds buf; } fd;                                      /* fd(管道) */
        struct { struct { connection *conn; int failed; } *dst; size_t n_dst; off_t pos; sds buf; } connset; /* 多连接广播 */
    } io;
};
```

五种后端,对应的初始化函数:

| 后端 | 初始化函数 | 行号 | 典型场景 |
|---|---|---|---|
| file | `rioInitWithFile` | rio.c:185 | 磁盘 RDB 文件读写、AOF BASE rewrite |
| buffer | `rioInitWithBuffer` | rio.c:88 | 内存里组装 RDB 镜像(diskless sync 攒 buffer、DEBUG) |
| conn(只读) | `rioInitWithConn` | rio.c:289 | diskless replication 从节点:从 socket 读 RDB 流 |
| fd(写) | `rioInitWithFd` | rio.c:406 | 把 RDB 通过管道写给子进程 |
| connset(广播写) | `rioInitWithConnset` | rio.c:534 | diskless replication 主节点:一份 RDB 扇出给多个 replica |

`rdbSaveRio` 只依赖 `rioWrite`/`rdbWriteRaw` 这些抽象接口,完全不关心底层是文件、socket 还是内存 buffer。于是同一套 RDB 序列化代码被复用在三个完全不同的场景:

1. **RDB 落盘**:`rioInitWithFile(&rdb, fp)`,后端是 stdio 的 `FILE*`(见 [`rdbSaveInternal`](../../redis-8.0.2/src/rdb.c#L1550))。
2. **主从无盘同步(diskless replication)**:`rioInitWithFd`(主节点写到管道)或 `rioInitWithConnset`(主节点同时扇出给多个 replica),后端是 socket,主节点直接把 RDB 流推向从节点,中间不落地磁盘。
3. **内存 buffer**:`rioInitWithBuffer`,例如 `DEBUG` 命令、AOF preamble 的 BASE 段在内存里组装。

这是**取向④(简单优先)**在 I/O 层的落地:写一份序列化代码,靠几个函数指针切后端,而不是为"文件版 RDB"、"socket 版 RDB"、"内存版 RDB"写三套。

### 14.7.2 update_cksum 钩子:CRC64 织进每一次写入

`update_cksum` 这个钩子设计得巧妙。`rioWrite` 的内联实现([`rio.h:105`](../../redis-8.0.2/src/rio.h#L105))在每次写之前,如果挂了 `update_cksum` 回调,就顺手把这块数据喂给 [`rioGenericUpdateChecksum`](../../redis-8.0.2/src/rio.c#L555)(内部就是 `crc64`):

```c
/* rio.h:105-119,精简 */
static inline size_t rioWrite(rio *r, const void *buf, size_t len) {
    if (r->flags & (RIO_FLAG_WRITE_ERROR | RIO_FLAG_ABORT)) return 0;
    while (len) {
        size_t bytes_to_write = (r->max_processing_chunk && r->max_processing_chunk < len) ? r->max_processing_chunk : len;
        if (r->update_cksum) r->update_cksum(r,buf,bytes_to_write);   /* 先算 checksum */
        if (r->write(r,buf,bytes_to_write) == 0) {                     /* 再写 */
            r->flags |= RIO_FLAG_WRITE_ERROR;
            return 0;
        }
        buf = (char*)buf + bytes_to_write;
        len -= bytes_to_write;
        r->processed_bytes += bytes_to_write;
    }
    return 1;
}
```

```c
/* rio.c:555-557 */
void rioGenericUpdateChecksum(rio *r, const void *buf, size_t len) {
    r->cksum = crc64(r->cksum,buf,len);
}
```

于是 CRC64 的计算被织进每一次写入,完全不需要业务代码关心——你只管写,checksum 自动累加,最后 `rdbSaveRio` 把 `rdb->cksum` 一次性写到文件末尾。换后端时 checksum 行为不变,因为钩子挂在抽象层而不是后端。

注意 read 路径的顺序相反:**read 是先读后算,write 是先算后写**——但两者校验的都是真实流经的字节,最终的 `r->cksum` 值在两端一致。

### 14.7.3 auto-sync:平摊 I/O 压力

文件后端还有一个值得讲的细节 [`rioSetAutoSync`](../../redis-8.0.2/src/rio.c#L567)。RDB 写盘默认开启 `rdb_save_incremental_fsync`,它会让文件 rio 每写满 `REDIS_AUTOSYNC_BYTES`(定义在 [`server.h:169`](../../redis-8.0.2/src/server.h#L169) = `4*1024*1024` = 4MB)字节就主动 `fflush` 一次。

```c
/* rio.c:567-570 */
void rioSetAutoSync(rio *r, off_t bytes) {
    if(r->write != rioFileIO.write) return;   /* 只有 file 后端才生效 */
    r->io.file.autosync = bytes;
}
```

为什么?注释说得很清楚:如果全靠 OS 的 write buffer 攒着,几 GB 的脏页会在某一瞬间集中落盘,造成 I/O 抖动尖峰;主动周期性 flush 把压力平摊到整个 BGSAVE 过程。这是吞吐与延迟的经典权衡——宁可每次多一点点 flush 开销,也别让最后那一拨写盘把磁盘 IO 打满、拖慢主线程的响应(因为主线程虽然不写盘,但和子进程共享同一个内核 I/O 栈)。

注意那个守卫 `if(r->write != rioFileIO.write) return;`:autosync 概念只对 FILE 后端有意义,对 socket/buffer 是 no-op。这是"概念边界"的硬保证——autosync 是文件系统语义,强行套到 socket 上毫无意义。

8.0 还有一个配套的 `rioSetReclaimCache`(rio.c:577),设置 `r->io.file.reclaim_cache` 标志,autosync 触发 fsync 之后会调 `reclaimFilePageCache` 释放页缓存(Linux 下用 `posix_fadvise(DONTNEED)`),见 rio.c:140-149。这是"写完后别占着页缓存"的优化——RDB 文件几 GB,占满 page cache 会挤掉业务热数据,主动归还更友好。

> **钉死这件事**:rio 这套设计是"**薄抽象层换来巨复用**"的范本。结构体本身只有几个函数指针 + 一个 union,核心代码不超过 200 行,但它让 RDB 序列化逻辑同时服务三个完全不同的场景(文件/socket/内存),还把 CRC64 织进每一次读写、把 autosync 限制在文件后端、把 page cache 回收挂在 fsync 之后。每一处都解决一个具体的小问题,叠在一起就是生产可用的 I/O 层。这是取向④(简单优先)的极致:不写一个庞大的 I/O 框架,只写一个最小的可插拔接口。

## 14.8 两个绕不开的坑:fork 阻塞与 COW 内存放大

讲到这里,RDB 的设计主线已经清晰:**fork 把写盘的重活搬出主线程(取向①),COW 把"一致性快照"变成内核赠品(取向⑤),rio 抽象让序列化代码三处复用(取向④)**。但可靠性从来不是免费的,fork + COW 这套方案有两个绕不开的代价,生产事故大多栽在这两个坑里,必须讲透。

### 14.8.1 坑一:fork 本身会短暂阻塞主线程

很多人以为 fork 是"立即返回、零代价",这是个误解。`fork()` 要复制父进程的**页表**(不是内存数据,是页表这个映射结构)。一个用了 50GB 内存的 Redis,页表本身就可能有上百 MB,fork 时内核得把这些页表项拷一份给子进程,这段时间是**拷贝页表、且持有内存写锁**的,主线程在这个窗口里是被阻塞的。大内存实例 fork 耗时几百毫秒到秒级都不稀奇,期间所有客户端命令延迟飙升。

这就是为什么 Redis 文档反复强调:**单实例内存不要太大**(建议控制在 10GB 以内,经验上 20GB 以上 fork 延迟就开始明显)。应对办法有两条:

一是开 Linux 的**透明大页(THP)要关掉**——这是一个反直觉的取舍。THP 让页表项变少(2MB 一页而不是 4KB),fork 确实快了,但 COW 时一个 2MB 大页里只要有一个字节被改,内核就得复制整页 2MB,COW 放大更严重。生产上是宁可 fork 慢点(单次几百毫秒),也不要 THP(BGSAVE 期间内存翻倍)。

二是接受 fork 的短暂阻塞,把它当作可靠性要交的税,配合监控告警及时发现。`INFO stats` 里的 `latest_fork_usec` 字段记录了最近一次 fork 的耗时(微秒),生产监控要盯住它——超过 1 秒就该报警。

### 14.8.2 坑二:COW 的内存放大——BGSAVE 期间父进程猛写,内存可能翻倍

这是更隐蔽也更危险的坑。fork 之后父子共享物理页,只要父进程不写,内存占用基本不变。但如果 BGSAVE 期间(可能持续几十秒),业务在疯狂写入(比如大促、热点 key 被反复更新),每一处写入都触发一次 COW 页复制:父进程改的页被复制一份留给子进程,子进程那一份是旧值。结果就是**父子两边的物理内存加起来,可能逼近原来的两倍**。

举个例子:20GB 的 Redis,BGREWRITEAOF/BGSAVE 进行中,业务还在以每秒几十万次写,父进程改了大量页,这些页都被复制了一份给子进程持有。这时候系统总内存可能从 20GB 飙到 35GB+,一旦触发 OOM,内核(还记得子进程的 OOM 分数被调低了吗?优先杀子进程)会杀掉子进程,这次持久化失败;更糟的是父进程也可能被殃及。这是大内存 Redis 最常见的事故类型。

Redis 自身在源码里对坑二做了两件事:一是前面 14.6 节讲的 `dismissObject`/`dismissMemoryInChild`,子进程写完的对象主动 `MADV_DONTNEED`,从源头降低 COW 复制量;二是 BGSAVE 结束后子进程会上报自己实际占用的内存(`sendChildCowInfo`,见 [`rdb.c:1659`](../../redis-8.0.2/src/rdb.c#L1659) 的 `CHILD_INFO_TYPE_RDB_COW_SIZE`),父进程据此在 `INFO memory` 里暴露 `mem_not_counted_for_evict` 和 `cow_size`,让运维能监控 COW 实际放大了多少。

运营上应对坑二的铁律是:**BGSAVE / BGREWRITEAOF 期间尽量压低写入速率**,或者在内存规划时给 COW 留出 50% 的余量。也可以错峰 BGSAVE——`save` 规则设宽一点(比如 `save 3600 1`),让 BGSAVE 不要在业务高峰期触发。

> **钉死这件事**:这两个坑不是 RDB 设计的 bug,而是 fork + COW 这套方案的固有代价——它把"一致性快照"做成了内核赠品,但赠品的成本是"fork 瞬时阻塞 + COW 内存放大"。Redis 的处理是**接受这些代价、但把它们限制在可控范围**(dismiss 优化、cow_size 监控、OOM 分数调整、临时文件 + rename、`latest_fork_usec` 监控),而不是回避。这正契合取向⑤:可靠性的本质不是零故障,而是把故障模式想清楚、把影响面收窄。

## 14.9 RDB 加载:rdbSave 的镜像

讲完了写入,加载(`rdbLoad`)就是它的镜像——按 opcode 逐个解析,把字节流重建回内存。这一节回答三个问题:启动时从哪进加载?加载主循环怎么解析 opcode?加载时和写入时有什么不对称?

### 14.9.1 启动调用链:loadDataFromDisk → rdbLoad → rdbLoadRio

Redis 进程启动时,在 `main` 里调 [`loadDataFromDisk`](../../redis-8.0.2/src/server.c#L6942),它根据 AOF 是否开启决定走 AOF 还是 RDB:

```c
/* server.c:6942-6963,精简 */
void loadDataFromDisk(void) {
    if (server.aof_state == AOF_ON) {
        /* AOF 优先 */
        ...
    } else {
        rdbSaveInfo rsi = RDB_SAVE_INFO_INIT;
        int rdb_flags = RDBFLAGS_NONE;
        if (iAmMaster()) {
            createReplicationBacklog();
            rdb_flags |= RDBFLAGS_FEED_REPL;   /* master 重启要喂 replication backlog */
        }
        int rdb_load_ret = rdbLoad(server.rdb_filename, &rsi, rdb_flags);
        ...
    }
}
```

`RDBFLAGS_FEED_REPL` 这个标志后面会用到——它告诉加载端:"如果加载时丢弃了过期 key,要往 replication backlog 喂一条 DEL,让 replica 也删掉"。`main` 调用 `loadDataFromDisk` 在 [`server.c:7515`](../../redis-8.0.2/src/server.c#L7515)。

完整调用链:**server.c:7515 `main()` → server.c:6942 `loadDataFromDisk()` → server.c:6962 `rdbLoad()` → [`rdb.c:3720`](../../redis-8.0.2/src/rdb.c#L3720) `rdbLoad`(打开文件建 rio)→ [`rdb.c:3330`](../../redis-8.0.2/src/rdb.c#L3330) `rdbLoadRio`(瘦包装)→ [`rdb.c:3342`](../../redis-8.0.2/src/rdb.c#L3342) `rdbLoadRioWithLoadingCtx`(真正主循环)**。

### 14.9.2 加载主循环:opcode-driven 的解析链

`rdbLoadRioWithLoadingCtx` 是 `rdbSaveRio` 的镜像,但它不是按 save 端的固定顺序写,而是用 **opcode-driven 的循环**——读一个字节,根据值决定走哪个分支。先读 9 字节 magic 校验签名 + 解析版本号,然后进 `while(1)` 循环:

```c
/* rdb.c:3342-3376,精简 */
int rdbLoadRioWithLoadingCtx(rio *rdb, int rdbflags, rdbSaveInfo *rsi, rdbLoadingCtx *ctx) {
    char buf[1024];
    int rdbver;
    ...
    /* 读 magic */
    if (rioRead(rdb,buf,9) == 0) goto eoferr;
    buf[9] = '\0';
    if (memcmp(buf,"REDIS",5) != 0) { ... goto eoferr; }
    rdbver = atoi(buf+5);
    if (rdbver < 1 || rdbver > RDB_VERSION) { ... goto eoferr; }   /* RDB_VERSION=12 */

    uint64_t dbid = 0;
    int should_expand_db = 0;
    ...
    while(1) {
        /* 读一个字节,opcode 或 type */
        int type;
        if ((type = rdbLoadType(rdb)) == -1) goto eoferr;
        ...
```

主循环每轮先 `rdbLoadType`(读 1 字节),然后用 `if / else if` 长链判断它是 opcode 还是 type byte。分支结构(`rdb.c:3378-3570`):

| opcode/type | 行号 | 处理 |
|---|---|---|
| `RDB_OPCODE_EXPIRETIME`(253) | rdb.c:3378 | 读 4 字节秒级过期(老格式),×1000 转毫秒存 `expiretime`,`continue` |
| `RDB_OPCODE_EXPIRETIME_MS`(252) | rdb.c:3386 | 读 8 字节毫秒过期,存 `expiretime`,`continue` |
| `RDB_OPCODE_FREQ`(249) | rdb.c:3392 | 读 1 字节 LFU 频率,存 `lfu_freq`,`continue` |
| `RDB_OPCODE_IDLE`(248) | rdb.c:3398 | 读 LRU 空闲时长(`rdbLoadLen`),存 `lru_idle`,`continue` |
| `RDB_OPCODE_EOF`(255) | rdb.c:3404 | **`break` 跳出主循环** |
| `RDB_OPCODE_SELECTDB`(254) | rdb.c:3407 | 读 `dbid`(`rdbLoadLen`),校验 `< server.dbnum`,切 `db` 指针,`continue` |
| `RDB_OPCODE_RESIZEDB`(251) | rdb.c:3419 | 读 `db_size` + `expires_size` 两个 hint,置 `should_expand_db=1`,`continue`(真正 expand 延后到 rdb.c:3574) |
| `RDB_OPCODE_SLOT_INFO`(244) | rdb.c:3428 | cluster 模式:读 slot_id/slot_size/expires_slot_size,`kvstoreDictExpand` 各 slot 字典;非 cluster 静默跳过 |
| `RDB_OPCODE_AUX`(250) | rdb.c:3444 | 读 `auxkey`+`auxval`,按 key 名分发:`repl-stream-db`/`repl-id`/`repl-offset` 填 `rsi`;`redis-ver`/`ctime`/`used-mem`/`aof-base` 仅日志;**未知字段 DEBUG 日志静默跳过**(向后兼容契约),`continue` |
| `RDB_OPCODE_MODULE_AUX`(247) | rdb.c:3506 | 读 moduleid/when,查模块 `mt`,若支持 `aux_load` 则调用,`continue` |
| `RDB_OPCODE_FUNCTION_PRE_GA`(246) | rdb.c:3559 | 报错退出(仅 7.0 RC 用过,8.0 不再支持) |
| `RDB_OPCODE_FUNCTION2`(245) | rdb.c:3562 | 调 [`rdbFunctionLoad`](../../redis-8.0.2/src/rdb.c#L3292) 加载 functions 库,`continue` |
| **否则:type 是数据类型,进 KV 加载** | rdb.c:3572 | 见 14.9.3 |

注意 EXPIRETIME_MS/IDLE/FREQ 这三个 key 级前缀 opcode 在 save 端是写在 type byte 之前的,在 load 端它们被解析后**只暂存进局部变量**(`expiretime`、`lru_idle`、`lfu_freq`),`continue` 继续读下一个 opcode,直到读到真正的 type byte,才用这些暂存的属性把 key 入库。这是"前缀 opcode + 数据区"的镜像处理。

### 14.9.3 KV 加载逻辑(内联在主循环里)

8.0 的一个重要重构:**没有独立的 `rdbLoadDatabase` 函数**,KV 加载循环直接内联在 `rdbLoadRioWithLoadingCtx` 主循环末尾(`rdb.c:3572-3677`)。流程是:

1. **延迟 expand**(rdb.c:3574):若之前 RESIZEDB 置了 `should_expand_db`,现在 `dbExpand(db, db_size, 0)` + `dbExpandExpires(db, expires_size, 0)`。放在这里是因为 RESIZEDB 一定先于本 db 的第一个 key 出现,延后到第一个 key 之前做正好。
2. **读 key**(rdb.c:3581):`key = rdbGenericLoadStringObject(rdb, RDB_LOAD_SDS, NULL)`(读 SDS 字符串)。
3. **读 value**(rdb.c:3584):`val = rdbLoadObject(type, rdb, key, db->id, &error)` —— 按 type byte 进对象加载。

然后是三个处置分支:

**分支 A:val == NULL 且空 key(rdb.c:3594-3606)**——历史曾有 bug 产生空 key(#8453),容错:前 10 个打 NOTICE 日志,`sdsfree(key)` 后**静默丢弃**,继续加载;其他错误 `goto eoferr`。

**分支 B:加载时过期检查——过期则丢弃(rdb.c:3607-3625)** ⭐ 这是 save 端没有的逻辑:

```c
/* rdb.c:3607-3625,精简 */
} else if (iAmMaster() &&
    !(rdbflags&RDBFLAGS_AOF_PREAMBLE) &&
    expiretime != -1 && expiretime < now)
{
    /* This key is already expired, skip it. */
    if (rdbflags & RDBFLAGS_FEED_REPL) {
        robj keyobj;
        initStaticStringObject(keyobj,key);
        /* 喂 DEL/UNLINK 给 replication backlog */
        replicationFeedSlaves(server.slaves, dbid, ...);
    }
    sdsfree(key);
    decrRefCount(val);
    server.rdb_last_load_keys_expired++;
    continue;   /* 不入库 */
}
```

三重条件:**当前是 master** + **不是 AOF preamble** + **有 expiretime 且已过期**。为什么只有 master 这么做?注释(rdb.c:3586-3593)解释:从 master 收 RDB 的 replica 不应主动过期(master 负责过期决策);AOF preamble 也不该过期(incr AOF 假定基线 keyspace 原样)。动作:丢弃 key 不入库;若带 `RDBFLAGS_FEED_REPL`,喂一条 `DEL`/`UNLINK` 给 replication backlog,保证 replica 同步删除。

**分支 C:正常入库(rdb.c:3626-3665)** ⭐:

- **`dbAddRDBLoad(db, key, val)`** —— 入库点(注意是 `dbAddRDBLoad` 不是 `dbAdd`,这是 RDB 加载专用入口,内部处理 key 所有权转移);`server.rdb_last_load_keys_loaded++`
- 若 `dbAddRDBLoad` 返回 0(重复 key):`RDBFLAGS_ALLOW_DUP` 时 `dbSyncDelete` 后重加(DEBUG RELOAD 用),否则 `serverPanic("Duplicated key found in RDB file")`(rdb.c:3643)—— **RDB 逻辑上不应有重复,有了就直接 panic**
- **`setExpire(NULL, db, &keyobj, expiretime)`** —— 还原 key 级 TTL
- **`objectSetLRUOrLFU(val, lfu_freq, lru_idle, lru_clock, 1000)`** —— 还原 LRU/LFU 淘汰元数据
- **`moduleNotifyKeyspaceEvent(NOTIFY_LOADED, ...)`** —— 给 module 的 key 加载通知

每个 key 处理完,`expiretime = -1; lfu_freq = -1; lru_idle = -1;` 重置(rdb.c:3674),进入下一个 key。

### 14.9.4 CRC64 校验:边读边算 + 末尾比对

CRC64 在加载时的机制和写入对称:

- **边读边算**:`rdbLoadRioWithLoadingCtx` 在主循环前 `rdb->update_cksum = rdbLoadProgressCallback`(rdb.c:3352),这个回调(`rdb.c:3268`)在每次 read 后调 `rioGenericUpdateChecksum` 累加 `r->cksum`,顺带按 `loading_process_events_interval_bytes` 间隔喂事件循环(`processEventsWhileBlocked`)、报加载进度。

- **末尾比对**(rdb.c:3678-3696):

```c
/* rdb.c:3678-3696,精简 */
if (rdbver >= 5) {
    uint64_t cksum, expected = r->cksum;          /* 累积值 */
    if (rioRead(rdb,&cksum,8) == 0) goto eoferr;  /* 读文件末 8 字节 */
    if (server.rdb_checksum && !server.skip_checksum_validation) {
        memrev64ifbe(&cksum);                      /* 字节序 */
        if (cksum == 0) {                          /* 文件里是 0 = 保存时关了 checksum */
            serverLog(LL_NOTICE,"RDB file was saved with checksum disabled...");
        } else if (cksum != expected) {            /* 不匹配 */
            serverLog(LL_WARNING,"Wrong RDB checksum...");
            rdbReportCorruptRDB("RDB CRC error");
            return C_ERR;
        }
    }
}
```

三个要点:第一,只对 rdbver ≥ 5 校验(RDB v5 引入 CRC64);第二,文件末 8 字节若是 0,说明**保存时** `rdb_checksum` 关闭,加载方优雅跳过校验(向后兼容);第三,双开关 `server.rdb_checksum` + `server.skip_checksum_validation` 同时为真才校验——前者是配置,后者是显式跳过(比如 `redis-check-rdb` 工具调试时)。

### 14.9.5 加载和写入的不对称点

加载是写入的镜像,但有几处刻意的"不对称":

1. **加载时过期检查**(rdb.c:3607)只在 master 且非 AOF preamble 时执行——save 端无此概念。
2. **空 key 容错**(rdb.c:3599)——load 端容忍历史 bug 产生的空 key,save 端不会产生。
3. **重复 key panic**(rdb.c:3643)——load 端对非 DEBUG 模式下的重复 key 直接 panic,因为 RDB 逻辑上不应有重复;save 端不可能产生重复(它遍历的是 dict)。
4. **未知 AUX 字段静默跳过**(rdb.c:3444 的 `strcasecmp` 链尾部)——这是向后兼容契约,新版本加的 AUX 字段,老版本加载时直接忽略,不报错。save 端无此概念。

> **钉死这件事**:RDB 的加载(`rdbLoadRioWithLoadingCtx`)和写入(`rdbSaveRio`)是严格的镜像——opcode 集合、文件头、CRC64 校验完全对应。但加载端多了三件事:**加载时过期 key 丢弃**(让重启后的 keyspace 立即"清理掉"过期数据,不占内存)、**重复 key panic**(RDB 不允许重复,有了说明文件损坏)、**未知 AUX 字段静默跳过**(保证新版本写的 RDB 老版本能读,向后兼容)。这三件事是"加载端比写入端更聪明"的体现——写入端只负责忠实地序列化当前内存,加载端要负责"重建一个健康、正确、向前兼容的内存状态"。

## 14.10 RDB 与 AOF 的混合:MP-AOF 的 base 段用 RDB 二进制

RDB 解决了"掉电不丢某一刻的状态",但有一个先天不足:**两次 BGSAVE 之间的写入会丢**。默认配置下最坏可能丢失几分钟数据。对很多场景(缓存、会话)这够了,但对"金融、订单"这类要求"一秒都不丢"的场景就不够。

Redis 的另一条持久化路线是 **AOF(Append-Only File)**:它不存快照,而是把每一条写命令追加到日志,用"重放"代替"快照"。AOF 的细节是下一章的事,这里只讲它和 RDB 的交叉点——**混合持久化**。

### 14.10.1 aof-use-rdb-preamble:base 用 RDB 二进制

Redis 4.0 引入了 `aof-use-rdb-preamble` 配置,**默认 yes**(见 [`config.c:3093`](../../redis-8.0.2/src/config.c#L3093) 第 4 个参数 = 1)。它的语义是:AOF rewrite 产生的 base 文件,用 RDB 二进制格式而不是 RESP 文本格式。

为什么?RDB 二进制比 RESP 文本紧凑得多(整数编码、LZF 压缩、length 编码都比 RESP 的逐字节 ASCII 省空间),加载也快得多——`rdbLoadRio` 直接反序列化为内存对象,无需走命令分发(`processCommand` 那条慢路径)。对于"全量重写"这种一次性产生几 GB 文件的场景,RDB 二进制的优势压倒性。

写入分支在 [`rewriteAppendOnlyFile`](../../redis-8.0.2/src/aof.c#L2454)(aof.c:2475-2485):

```c
/* aof.c:2475-2485,精简 */
startSaving(RDBFLAGS_AOF_PREAMBLE);

if (server.aof_use_rdb_preamble) {
    int error;
    if (rdbSaveRio(SLAVE_REQ_NONE,&aof,&error,RDBFLAGS_AOF_PREAMBLE,NULL) == C_ERR) {
        errno = error;
        goto werr;
    }
} else {
    if (rewriteAppendOnlyFileRio(&aof) == C_ERR) goto werr;
}
```

注意 `rdbSaveRio` 的第四个参数 `RDBFLAGS_AOF_PREAMBLE`——它会传到 `rdbSaveInfoAuxFields`,在 AUX 段写一个 `aof-base=1` 字段(rdb.c:1283),让加载端知道"这是一份 AOF 的 base,不是独立 RDB"。

### 14.10.2 MP-AOF:base 和 incr 是分离的文件

一个常被讲错的事:Redis 7.0 起改成了 **MP-AOF(Multi-Part AOF)** 架构,BASE 和 INCR 是物理分离的多份文件,不再像老版本那样"同一文件 RDB 头 + RESP 尾"拼接。

- **base 文件**:由 AOF rewrite 产生,文件名形如 `appendonly.aof.1.base.rdb`(preamble=yes 时)或 `.base.aof`(preamble=no 时)。它就是一份完整的 RDB 文件(或一份 RESP 格式的全量重写)。
- **incr 文件**:由父进程在 rewrite 期间持续追加的增量命令,文件名形如 `appendonly.aof.1.incr.aof`。增量永远是 RESP 文本,无论 preamble 开关——因为增量要追加,而 RDB 是定长二进制,不适合追加。
- **manifest 文件**:`appendonly.aof.manifest`,记录 base 和所有 incr 文件的清单和加载顺序。

加载逻辑在 [`loadSingleAppendOnlyFile`](../../redis-8.0.2/src/aof.c#L1480)(aof.c:1520-1551),它先读文件头 5 字节判断是不是 RDB:

```c
/* aof.c:1523-1551,精简 */
char sig[5]; /* "REDIS" */
if (fread(sig,1,5,fp) != 5 || memcmp(sig,"REDIS",5) != 0) {
    /* 不是 RDB 格式,seek 回开头,按 RESP 加载 */
    if (fseek(fp,0,SEEK_SET) == -1) goto readerr;
} else {
    /* 是 RDB 格式(base 文件或老式 preamble AOF),调 rdbLoadRio 加载 */
    rio rdb;
    ...
    rioInitWithFile(&rdb,fp);
    if (rdbLoadRio(&rdb,RDBFLAGS_AOF_PREAMBLE,NULL) != C_OK) { ... }
    if (old_style) serverLog(LL_NOTICE, "Reading the remaining AOF tail...");
}
```

识别逻辑很优雅:**读文件头 5 字节,看是不是 "REDIS"**。是就走 `rdbLoadRio` 加载 base;不是就按 RESP 加载。这种"魔术数识别"让同一份加载代码同时处理 RDB base 和 RESP incr,非常干净。

### 14.10.3 为什么要混合

把 RDB 和 AOF 混起来,本质是**两者的优势互补**:

- **RDB 的强项**:全量快照紧凑、加载快(直接反序列化,不走命令分发)、fork+COW 保证一致性。
- **AOF 的强项**:增量日志不丢数据(每秒 fsync 最多丢 1 秒)、RESP 文本可读、可 replay。

混合后:**base 用 RDB**(全量快照,紧凑加载快),**incr 用 RESP**(增量追加,不丢数据)。重启加载时先 `rdbLoadRio` 加载 base(几秒内重建基线 keyspace),再 replay 几个 incr 文件(重放最近的增量命令)。这比"纯 AOF"(几十 GB 的 RESP 命令一条条 replay,可能几分钟)快得多,也比"纯 RDB"(两次 BGSAVE 之间数据丢失)安全得多。

AOF 的完整细节(appendfsync 策略、rewrite 触发、加载 replay)是下一章的主线,这里只点了它和 RDB 的交叉点。RDB 这章到这就算讲完了。

> **钉死这件事**:RDB 和 AOF 不是二选一的关系,4.0 之后它们在 MP-AOF 里合流了——base 段是 RDB 二进制,incr 段是 RESP 文本。`aof-use-rdb-preamble` 默认 yes,因为 RDB 二进制几乎总是更优(紧凑、加载快)。这是 Redis 持久化设计的成熟形态:不强迫用户在"快但不丢太多"和"不丢但慢"之间二选一,而是让两种格式各司其职、互补短板。这也是为什么 14.9 节加载逻辑里那个 `RDBFLAGS_AOF_PREAMBLE` 标志那么重要——它告诉加载端"这是一份 AOF base,加载时不要做过期 key 丢弃"(incr AOF 假定基线 keyspace 原样),这是 RDB 和 AOF 语义差异的精确协调点。

## 章末:回扣、五个为什么、往哪钻

### 主线回扣

这一章是**取向⑤(可靠性)**与**取向①(把耗时从主线程解放)**的联手。Redis 用 `fork()` 这一个系统调用,把"持久化"这件耗时的事(BGSAVE 期间子进程写盘几秒到几分钟)从主线程整体搬走,同时靠 COW 把"一致性快照"变成内核赠品——不需要锁、不需要 MVCC、不需要暂停世界。主线程只付一次 fork 的代价(复制页表,见 14.8 节),其余时间照常接客。

这套方案不是没有代价:fork 瞬时阻塞主线程(坑一)、BGSAVE 期间 COW 内存放大(坑二)。Redis 的处理是**接受代价但收窄影响面**——`dismissMemory` 从源头压低 COW、`sendChildCowInfo` 暴露 cow_size 给监控、`setOOMScoreAdj` 保护主进程不被 OOM、临时文件 + rename 保证崩溃一致性、CRC64 校验保证文件完整。每一处都是工程上的小功夫,叠在一起就是生产可用的可靠性。

它也是**取向④(简单优先)**的招牌:整个 RDB 子系统(rdb.c)不到 5000 行,核心序列化逻辑(rdbSaveRio + rdbSaveDb + rdbSaveKeyValuePair)加起来不到 300 行;rio 抽象层用几个函数指针就让 RDB 序列化代码复用在文件/socket/内存三个场景。这是"用一个简单的机制(fork)解决一个复杂的问题(并发快照一致性)"的范本。

### 五个为什么

**Q1:为什么 fork 而不是线程?**

线程和主线程共享地址空间,工作线程遍历 dict 写盘时,主线程可能在 rehash、插入、删除同一个 dict,不全程加锁就会读到撕裂数据。加锁就把单线程模型(无锁、无竞争、可重入)全废了。fork 出来的子进程和父进程物理隔离(COW),子进程看到的永远是 fork 那一刻的快照,不需要锁也不需要 MVCC。这是 fork 相对线程的根本优势——**用进程级隔离换数据级一致性**。

**Q2:RDB 文件里 EXPIRETIME(253)和 EXPIRETIME_MS(252)到底用哪个?**

8.0 写出端**只用 EXPIRETIME_MS(252)+8 字节毫秒**(rdb.c:1202),253 只保留给加载端读旧版 RDB(4 字节秒级)。证据在 rdb.c:114-118 的注释:"This is only used to load old databases stored with the RDB_OPCODE_EXPIRETIME opcode."。所以你看一份新写的 RDB 文件,数据区里只会出现 252,不会出现 253。这是 8.0 相对老资料的一处关键差异,讲课时不能混淆。

**Q3:RESIZEDB 那个 hint 不要行不行?**

行,加载端有兜底——没有 hint 就用默认大小初始化 dict,边加载边 rehash。代价是加载 100 万 key 的过程中 dict 可能 rehash 十几次,每次 rehash 都是双倍内存 + 一次渐进式迁移,加载速度大打折扣。RESIZEDB 让加载方一次性 `dbExpand` 到正确大小,避免 rehash。这是"为加载端优化保存端"的典型设计——保存时多写两个字段,加载时省下大量 rehash 开销。所以它叫 hint(提示)不是必需,但没有它加载会显著变慢。

**Q4:dismissMemory 的 madvise(MADV_DONTNEED) 和 free 有什么区别?**

`free`(更准确说是 `munmap`)是把虚拟内存映射整个解除,地址空间都没了,以后访问会段错误。`madvise(MADV_DONTNEED)` 是告诉内核"这片内存我暂时不要了,物理页可以回收,但虚拟地址映射保留"——内核立刻把对应的物理页回收(下次访问会重新给一个零页)。dismissMemory 要的是后者:子进程以后可能还要读这块内存(虽然概率低,但对象内部某些指针可能间接引用),不能 munmap 掉虚拟地址;但它可以告诉内核"物理页不要了",让父进程后续修改时不触发 COW 复制。这是"归还物理页但保留虚拟地址"的精细操作。

**Q5:RDB 加载时,过期 key 是直接丢弃,那 replica 从 master 收到的 RDB 里也有过期 key 怎么办?**

加载端的过期检查(rdb.c:3607)有三重条件:**当前是 master** + **不是 AOF preamble** + **有 expiretime 且已过期**。第二、三个条件好理解,关键是第一个——**只有 master 才在加载时丢弃过期 key**。replica 从 master 收到 RDB 时,它不是 master(它是 replica),所以不会主动丢弃过期 key,而是原样入库。为什么?因为过期决策权在 master,master 觉得这个 key 还没到该删的时候(哪怕看起来已过期,可能是时钟偏移),replica 不能越权。master 自己重启时丢弃已过期 key 是合理的(它要重建"健康"的 keyspace),replica 同步时要忠实地复刻 master 状态,包括那些 master 还没删的"过期但未清理"的 key。

### 想继续深入往哪钻

- 想看 `redisFork` 的完整实现:读 [server.c](../../redis-8.0.2/src/server.c) 的 `redisFork`(server.c:6760)、`dismissMemoryInChild`、`sendChildCowInfo` 这三个函数,看父子进程通信管道(`openChildInfoPipe`)怎么传 COW 内存数据。
- 想看 RDB 文件实际长什么样:在 Linux 上跑 `redis-cli BGSAVE`,然后用 `hexdump -C dump.rdb | head -50` 看文件头——你会看到 `REDIS0012` magic、AUX 字段、SELECTDB/RESIZEDB、key-value 区、末尾的 CRC64。也可以用 `redis-check-rdb dump.rdb` 工具,它会按格式解析打印每个字段。
- 想理解不同对象类型(listpack/dict/skiplist/quicklist/intset)在 RDB 里怎么序列化:读 [rdb.c](../../redis-8.0.2/src/rdb.c) 的 `rdbSaveObject`(按 type byte 分支到各类型的序列化)和 `rdbLoadObject`(rdb.c:1930,反向)。注意 8.0 的 hash 有 `_EX`/`_METADATA` 新类型(字段级过期),type byte 22-25。
- 想看 diskless replication 怎么用 rio 的 socket 后端:读 [rio.c](../../redis-8.0.2/src/rio.c) 的 `rioInitWithConnset`(rio.c:534)和 `rioInitWithConn`(rio.c:289),以及 replication.c 里 `sendBulkToSlave` 怎么调 `rdbSaveRio` 用 connset 后端一份 RDB 扇出给多个 replica。
- 想看 RDB 和 AOF 怎么在 MP-AOF 里协作:读 [aof.c](../../redis-8.0.2/src/aof.c) 的 `rewriteAppendOnlyFile`(aof.c:2454)看 base 怎么调 `rdbSaveRio`,以及 `loadSingleAppendOnlyFile`(aof.c:1480)看怎么按 magic 识别 RDB 还是 RESP。下一章会完整展开 AOF。

### 引出下一章

至此 RDB 这条线我们讲完了:fork 把重活搬出主线程(取向①),COW 把一致性快照变内核赠品(取向⑤),rio 抽象让序列化代码三处复用(取向④),dismissMemory 反向利用 COW 把内存放大压下去,临时文件 + rename + fsync 目录 + CRC64 四件套保证崩溃一致性和文件完整性。RDB 解决了"掉电不丢某一刻的状态"。

但 RDB 有一个先天不足:**两次 BGSAVE 之间的写入会丢**。默认配置下最坏可能丢失几分钟数据。对"金融、订单"这类要求"一秒都不丢"的场景,RDB 不够。下一章我们看 Redis 的另一条持久化路线——**AOF(Append-Only File)**:它不存快照,而是把每一条写命令追加到日志,用"重放"代替"快照",把数据丢失窗口压到一秒以内(`appendfsync everysec`)。AOF 同样要 fork(rewrite 时),同样要 COW,但它把"一致性"的语义从"某一刻"推进到了"每一个写操作",是 RDB 思路的延伸而非否定。两者最终在 4.0 之后合流成 MP-AOF(base=RDB + incr=RESP),那就是本章 14.10 节已经预告过的故事。

---

## 验证物:如何亲手确认本章的设计

> 说明:本书写作环境为 Windows,无法直接运行 redis-server(8.0 依赖 fork/epoll 等 Linux 特性)。以下 (1) gdb 断点脚本 (2) 源码常量锚点 (3) `INFO persistence` 观察项 均为可复现的精确指引,供读者在 Linux 环境(Ubuntu 22.04 / CentOS 8 等)对 redis-8.0.2 源码 `make no-opt` 编译后自行验证。**本书不附编造的运行输出**——凡未实跑的,只给脚本与预期观察变量,不写具体数值。

### 1. gdb 断点脚本

编译:`cd redis-8.0.2 && make no-opt`(带 -g)
启动:`gdb ./src/redis-server`

```gdb
# BGSAVE 主流程
(gdb) break rdbSaveBackground     # rdb.c:1642,fork 入口
(gdb) break rdbSave               # rdb.c:1599,子进程写盘入口
(gdb) break rdbSaveRio            # rdb.c:1458,序列化主函数
(gdb) break rdbSaveDb             # rdb.c:1371,每个 db 的写出
(gdb) break rdbSaveKeyValuePair   # rdb.c:1196,单 key-value 写入
(gdb) break dismissMemory         # server.c:6847,COW 优化
(gdb) break redisFork             # server.c:6760,fork 包装

# 加载路径
(gdb) break rdbLoad               # rdb.c:3720,启动加载入口
(gdb) break rdbLoadRioWithLoadingCtx  # rdb.c:3342,加载主循环
(gdb) break dbAddRDBLoad          # 入库点(rdb.c:3631 附近调用)

# 运行(加 save 规则让 BGSAVE 快速触发)
(gdb) run --save "" --port 6379 --daemonize no
# 另开终端,触发 BGSAVE:
# redis-cli -p 6379 SET k1 v1
# redis-cli -p 6379 BGSAVE

# 预期在 rdbSaveBackground 停下,观察父子分支:
(gdb) print server.dirty_before_bgsave   # 预期:fork 前的 dirty 累计值
(gdb) print server.in_fork_child         # 预期:0(父进程分支,父进程不设这个标志)
# continue 到 rdbSave(子进程才会进):
(gdb) print server.in_fork_child         # 预期:1(子进程,CHILD_TYPE_RDB)
# continue 到 rdbSaveKeyValuePair:
(gdb) print expiretime                   # 预期:-1(无过期)或毫秒时间戳
(gdb) print val->type                    # 预期:OBJ_STRING(0)/LIST(4)/HASH(2)/...
```

**预期观察**(基于源码 [rdb.c:1642-1680](../../redis-8.0.2/src/rdb.c#L1642) 的父子分工,本书未实跑):`redisFork` 返回后,父进程分支只执行 5-6 行(`serverLog` + 时间戳 + 状态 + return),子进程分支才进 `rdbSave`。这是 fork + 分工的活证据。

### 2. 源码常量锚点(带行号,从 redis-8.0.2 源码 Grep 核实)

| 常量/字段 | 位置 | 值/说明 |
|----------|------|---------|
| `RDB_VERSION` | rdb.h:21 | 12(magic 写成 `REDIS0012`) |
| opcode 244-255 | rdb.h:87-98 | SLOT_INFO/FUNCTION2/.../SELECTDB/EOF,见 14.5.1 表 |
| `RDB_OPCODE_EXPIRETIME_MS` | rdb.h:95 | 252(**8.0 唯一写过期的 opcode**)+ 8 字节毫秒 |
| `RDB_OPCODE_EXPIRETIME` | rdb.h:96 | 253(**8.0 只读不写**,读旧版秒级过期) |
| `RDB_TYPE_STRING` | rdb.h:55 | 0 |
| `RDB_TYPE_HASH_METADATA` | rdb.h:79 | 24(8.0 hash 字段级过期) |
| `RDB_TYPE_HASH_LISTPACK_EX` | rdb.h:80 | 25(8.0 hash LP 字段级过期) |
| `REDIS_AUTOSYNC_BYTES` | server.h:169 | 4MB |
| `rdbSaveBackground` | rdb.c:1642 | BGSAVE 入口,父进程毫秒级返回 |
| `redisFork` | server.c:6760 | fork 包装,设 `in_fork_child` + OOM 分数 |
| `dismissMemory` | server.c:6847 | `madvise(MADV_DONTNEED)`,半页以下跳过 |
| `sendChildCowInfo` | rdb.c:1659 | CHILD_INFO_TYPE_RDB_COW_SIZE,上报 COW 内存 |
| `rioSetAutoSync` | rio.c:567 | 仅 file 后端生效,4MB 一次 fsync |
| `rioGenericUpdateChecksum` | rio.c:555 | CRC64 增量,织进每次 rioWrite |
| `rioWrite`(内联) | rio.h:105-119 | 先 update_cksum 后 write |
| `rioInitWithFile/Buffer/Fd/Conn/Connset` | rio.c:185/88/406/289/534 | 五种后端初始化 |
| `loadDataFromDisk` | server.c:6942 | 启动加载入口,AOF 优先 |
| `rdbLoadRioWithLoadingCtx` | rdb.c:3342 | 加载主循环,opcode-driven |
| 加载时过期检查 | rdb.c:3607 | 三重条件:master + 非 AOF preamble + 已过期 |
| CRC64 末尾校验 | rdb.c:3678-3696 | rdbver≥5 才校验;文件末 8B=0 表示保存时关了 checksum |
| `aof_use_rdb_preamble` 默认值 | config.c:3093 | 1(yes) |
| `rewriteAppendOnlyFile` 调 rdbSaveRio | aof.c:2479 | 混合持久化写入分支 |
| `loadSingleAppendOnlyFile` 识别 RDB magic | aof.c:1523 | 读 5 字节 "REDIS" 判断格式 |

### 3. INFO persistence 观察项(需本地 redis-server)

> 以下操作需在 Linux 本地启动 redis-server 后用 redis-cli 执行。本书未实跑,仅列观察方法。`INFO persistence` 段是 RDB 状态的运维窗口。

```text
127.0.0.1:6379> CONFIG SET save "60 1"     # 60 秒内有 1 次改动就 BGSAVE,方便快速触发
127.0.0.1:6379> SET k1 v1                  # 触发 dirty 计数
# 等待 60 秒,让 save 规则命中触发 BGSAVE
127.0.0.1:6379> INFO persistence
# 关注以下字段(基于源码 server.c 的 fields 定义,本书未实跑):
#   rdb_bgsave_in_progress:0 或 1(当前是否在 BGSAVE)
#   rdb_last_save_time:Unix 时间戳,上次成功 BGSAVE 完成时间(对应 server.lastsave)
#   rdb_last_bgsave_status:ok 或 err(对应 server.lastbgsave_status)
#   rdb_last_bgsave_time_sec:上次 BGSAVE 耗时(秒)
#   rdb_current_bgsave_time_sec:当前 BGSAVE 已耗时(秒)
#   rdb_last_cow_size:上次 BGSAVE 的 COW 内存(字节,来自 sendChildCowInfo)

127.0.0.1:6379> INFO stats
# 关注:
#   latest_fork_usec:最近一次 fork 耗时(微秒)——这是 14.8.1 节"坑一"的监控点
#   total_forks:进程启动以来 fork 总次数

# 验证 BGSAVE 的父子分工(在另一个终端 strace):
# strace -f -e fork,clone -p <redis-server-pid>
# 触发 BGSAVE,预期看到一次 clone/fork 系统调用,然后父进程立刻回到 epoll_wait,
# 子进程进 write/writefs 系统调用写 temp-<pid>.rdb。这正是 14.2 节父子分工的活证据。

# 验证 RDB 文件格式(hexdump 看字节布局):
# redis-cli BGSAVE
# hexdump -C dump.rdb | head -5
# 预期:第一行前 9 字节是 "REDIS0012"(52 45 44 49 53 30 30 31 32),
# 然后是 250(0xFA,AUX opcode)+ AUX key/val,以此类推。
# 用 redis-check-rdb dump.rdb 看完整解析。
```

标注:以上预期基于源码常量与 [server.c](../../redis-8.0.2/src/server.c) 的 `INFO persistence` 字段定义推导,本书未在本地实跑;若你的 redis 版本/配置不同,字段值可能不同,但"`rdb_bgsave_in_progress` 在 BGSAVE 期间为 1、`latest_fork_usec` 反映 fork 阻塞时间"这两条不变。
