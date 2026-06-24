# 第十九章 · BIO 后台线程与 lazyfree:把脏活从主线程解放

> 篇:P6 后台与多线程
> 主轴呼应:这一章是**取向①(把耗时从主线程解放)的经典样板**——对象释放、AOF fsync、文件 close 这三个最常见的"长尾耗时"卡顿源,被一组后台线程接走,主线程只做"快且必须的摘除"。外包的方向是**长尾耗时**(对应下一章 IO 多线程外包的是**网络吞吐**),两个外包点切入的痛点完全不同,这是 P6 开篇先讲 BIO 的原因。

---

## 读完本章你会明白

1. **为什么 Redis 一边宣称"单线程无锁",一边在源码里用 `pthread_mutex`**——答案不在"它撒了谎",而在锁保护的资源域被它一刀切成了两个互不重叠的世界:BIO 队列归锁管,业务数据归"摘除 + 所有权移交"管。
2. **为什么 BIO 恰好启三个线程,而不是一个线程池**——按任务类型分线程(close / fsync / lazyfree 互不阻塞),换确定的隔离性,比一个动态线程池加优先级调度简单太多。
3. **"持锁取任务、放锁干任务"这个看似平凡的写法为什么值得点名**——它把 fsync 那几十毫秒的真正耗时挡在临界区外面,主线程投递新任务永远不会被一个慢 fsync 卡住。
4. **`LAZYFREE_THRESHOLD = 64` 这个魔法数字凭什么就是分界线**——小对象同步删更快(省了进队/唤醒/跨线程切换的一整套开销),太大才异步。这是"政治正确的全异步"被朴素阈值否决的活标本。
5. **`FLUSHALL ASYNC` 凭什么把"释放上百亿个 key"压成常数时间**——主线程只抓住旧三指针换新空表,O(1) 返回;真正的物理遍历释放全程在后台线程跑。这是"换壳"手法的极致。
6. **后台线程干完活怎么把消息送回单线程事件循环**——靠一根非阻塞 pipe + `aeCreateFileEvent`,后台线程只 `write(1 字节)`,主线程在 `epoll_wait` 醒来后在自己的串行上下文里读。这呼应第二章的 ae。

---

> **如果一读觉得太难:先只记住三件事**——
> ① BIO = 三个固定后台线程(各管 close / fsync / lazyfree 一摊)+ 三套队列(各一把锁一个条件变量);
> ② 主线程投递任务只做"指针进队 + `pthread_cond_signal`",几乎瞬间返回;后台线程"持锁取任务 → 放锁干任务 → 回锁摘链表";
> ③ `UNLINK` / `FLUSHALL ASYNC` / 五个 lazyfree-* 配置开关,都只是"是否走异步"的决策点,真正干活的还是同一套 BIO worker 2。
> 这三件事,就是 BIO 的全部。

---

> **一句话点破:BIO 不是"一个聪明的线程池调度器",而是"三个固定工人各守一根队列、各管一类脏活"的最小系统——它把"释放超大对象、fsync 大文件、close 大 fd"这三类天然又慢又对主线程当前数据无依赖的活,整体外包给后台线程;主线程的职责被收敛成"做决策、做摘除、做转发",绝不沾脏活。**

第八章我们看到 `DEL` 一个百万元素的 hash 走的是 skiplist 的 `zslFreeNode` 一路 `sdsfree`——但那个"一路释放"如果是同步在主线程里做,本身就是 O(N) 的长尾卡顿。那么 Redis 凭什么敢宣称"对大 key 删除也有毫秒级响应"?答案就在这一章:它根本没在主线程里删,它只是把对象**摘下来丢给后台线程**,自己立刻返回。本章是 P6 后台与多线程的开篇,也是取向①"把耗时从主线程解放"最纯粹的样板——下一章 IO 多线程切入的是另一个痛点(网络吞吐),两章对照才能看清 Redis 在不同痛点下对多线程的两种截然不同用法。

## 19.1 这块要解决什么:主线程里那几个又慢又能外包的活

Redis 的卖点是取向①"单线程 + 事件循环"。这意味着:主线程在任意一个瞬间只能做一件事,谁占了它的时间片,谁就让所有客户端排队。绝大多数操作——`GET`、`SET`、`HGET`、`LPUSH`——都是纳秒到微秒级的内存操作,事件循环应付得游刃有余。但有几个活天然又慢又不长眼:

**第一类:释放超大对象。** 一个有百万元素的 hash(编码是哈希表)或一个百万成员的 zset(跳表),`DEL` 它要做的是什么?要遍历每一个 entry,逐个 `zfree`/`sdsfree`。这不是常数时间,是 O(N) 时间。N 是百万级时,一次 `DEL` 能让主线程卡顿几十毫秒甚至上百毫秒——这段时间,其它成千上万个客户端的命令全在等。更要命的是:这个"慢"和"业务"毫无关系,纯粹是 jemalloc 在回收内存,完全可以推迟、外包。

**第二类:AOF fsync。** 看 `[aof.c:1320-1342](../../redis-8.0.2/src/aof.c#L1320)`,当策略是 `appendfsync always` 时,每条命令写完 AOF 缓冲都要在主线程里 `redis_fsync()` 把数据真正刷到磁盘:

```c
/* aof.c:1327-1336 */
/* redis_fsync is defined as fdatasync() for Linux in order to avoid
 * flushing the dirty pages used by other processes......(注释解释 Linux 用 fdatasync) */
if (redis_fsync(server.aof_fd) == -1) {
    serverLog(LL_WARNING,......);
} else {
    atomicSet(server.fsynced_reploff_pending, server.master_repl_offset);
}
```

`redis_fsync` 在 Linux 上就是 `fdatasync`(`[config.h:127](../../redis-8.0.2/src/config.h#L127)`),一次几毫秒到几十毫秒,取决于磁盘和文件系统。即使策略是 `everysec`(默认),也不能在主线程里每条命令同步 `fsync`,否则事件循环就废了——`everysec` 的真正实现是把 fsync 下放给 BIO worker 1,主线程只 `write` 不 `fsync`。`fsync` 这种"我只需要确认它最终成功"的活,天生适合后台。

**第三类:close 大文件描述符。** 这是最容易被忽略的一类。`close()` 一个 fd 看起来是常数时间,但如果这个 fd 是某个大文件的最后一个引用,内核要真正释放该文件的页缓存、回收 inode,这一步可能慢得惊人(尤其在稀疏文件、tmpfs、大 RDB 上)。看 `[bio.c:229-236](../../redis-8.0.2/src/bio.c#L229)` 的 `bioCreateCloseJob`,主从全量同步读取 RDB、AOF 重写关闭旧 AOF 这些场景,Redis 都把 `close` 丢给后台线程,而不是在主线程里干。

这三个活有一个共同特征:**慢,但对"主线程当前正在服务的那批数据结构"没有依赖。** 释放的对象已经从字典里摘掉了,fsync 的内容已经 `write` 进内核页缓存了,close 的文件主线程再也不碰了。它们和"快且必须串行"的核心逻辑之间没有真正的数据竞争——这是外包成立的根本前提,也是 19.3 节要讲清楚的精妙分界。

> **不这样会怎样**:如果这三类活全留在主线程,会怎样?第一,**长尾卡顿不可预测**——一个客户端 `DEL` 了个百万元素 hash,所有其它客户端的 `GET` 全等几十毫秒,延迟飙升;第二,**fsync 拖垮吞吐**——`appendfsync=always` 同步 fsync 会让每秒只能处理几百到几千条命令(磁盘 IO 决定),事件循环的优势全废;第三,**close 卡顿不可控**——某个大文件 close 时正撞上内核回收页缓存,主线程瞬间停顿。这三类活留在主线程,Redis 的毫秒级延迟承诺就守不住。

于是 Redis 设计了 **BIO(Background I/O)** 系统,一组后台线程,专门接走这三类活。

## 19.2 直球讲设计与源码:三个 worker、三套队列、一组投递 API

### 19.2.1 三个 worker,各管一摊

打开 `[bio.h:16](../../redis-8.0.2/src/bio.h#L16)`,BIO 的"工种"枚举写得清清楚楚:

```c
/* bio.h:16-21 */
typedef enum bio_worker_t {
    BIO_WORKER_CLOSE_FILE = 0,   // worker 0:专门 close 文件
    BIO_WORKER_AOF_FSYNC,        // worker 1:专门 fsync AOF
    BIO_WORKER_LAZY_FREE,        // worker 2:专门异步释放内存
    BIO_WORKER_NUM
} bio_worker_t;
```

再看 `[bio.c:51](../../redis-8.0.2/src/bio.c#L51)`,启动时的线程名就对应这三个 worker:

```c
/* bio.c:51-56 */
static char* bio_worker_title[] = {
    "bio_close_file",
    "bio_aof",
    "bio_lazy_free",
};
```

也就是说,Redis **恰好启三个 BIO 线程**,各管一摊。这和很多读者的直觉("一个线程池调度所有后台任务")不一样——Redis 选择**按任务类型分线程**,而不是按任务实例分线程。但这里有个微妙之处:`bio_worker_t`(worker 数,3 个)和 `bio_job_type_t`(opcode 数,8 个)不是一一对应的。看 `[bio.h:24](../../redis-8.0.2/src/bio.h#L24)`:

```c
/* bio.h:24-33 */
typedef enum bio_job_type_t {
    BIO_CLOSE_FILE = 0,          /* Deferred close(2) syscall. */
    BIO_AOF_FSYNC,               /* Deferred AOF fsync. */
    BIO_LAZY_FREE,               /* Deferred objects freeing. */
    BIO_CLOSE_AOF,               /* 关 AOF 文件:先 fsync 再 close */
    BIO_COMP_RQ_CLOSE_FILE,      /* 带回执的 close */
    BIO_COMP_RQ_AOF_FSYNC,       /* 带回执的 fsync */
    BIO_COMP_RQ_LAZY_FREE,       /* 带回执的 lazyfree */
    BIO_NUM_OPS
} bio_job_type_t;
```

8 个 opcode 但只有 3 个 worker。映射靠 `[bio.c:59](../../redis-8.0.2/src/bio.c#L59)` 的查表:

```c
/* bio.c:59-66 */
static unsigned int bio_job_to_worker[] = {
    [BIO_CLOSE_FILE] = 0,
    [BIO_AOF_FSYNC]  = 1,
    [BIO_CLOSE_AOF]  = 1,   // 关 AOF 文件,先 fsync 再 close,交给同一个 worker
    [BIO_LAZY_FREE]  = 2,
    [BIO_COMP_RQ_CLOSE_FILE] = 0,
    [BIO_COMP_RQ_AOF_FSYNC]  = 1,
    [BIO_COMP_RQ_LAZY_FREE]  = 2
};
```

注意 `BIO_CLOSE_AOF`(关闭 AOF 文件)被映射到 worker 1,而不是 worker 0——因为关 AOF 之前一定要先 fsync(看 `[bio.c:311-337](../../redis-8.0.2/src/bio.c#L311)`,worker 1 处理 `BIO_CLOSE_AOF` 时先 `redis_fsync` 再 `close`),两件事的语义是绑定的,放一个线程里串行做最自然。**类型分线程的好处是:同一类任务严格 FIFO,跨类任务互不阻塞。** fsync 慢不会拖累 lazyfree,某个百万元素 hash 释放慢不会拖累 fsync。

> **钉死这件事**:BIO 的"8 opcode → 3 worker"映射是"按资源域分线程"的教科书实践。把语义绑定的 opcode(如 CLOSE_AOF = fsync + close)合并到同一 worker,把语义独立的 opcode 分到不同 worker,换确定的隔离性。这比一个动态线程池加优先级调度简单太多——取向④"简单优先"在 worker 划分上的直接落地。

### 19.2.2 任务队列:每 worker 一把锁、一个条件变量

`[bio.c:69](../../redis-8.0.2/src/bio.c#L69)` 给每个 worker 准备了一套同步原语:

```c
/* bio.c:69-73 */
static pthread_t       bio_threads[BIO_WORKER_NUM];        // 线程句柄
static pthread_mutex_t bio_mutex[BIO_WORKER_NUM];          // 每个队列一把锁
static pthread_cond_t  bio_newjob_cond[BIO_WORKER_NUM];    // 每个队列一个条件变量
static list           *bio_jobs[BIO_WORKER_NUM];           // 每个队列一个双向链表
static unsigned long   bio_jobs_counter[BIO_NUM_OPS] = {0};// 按 opcode 统计待办数
```

**注意:锁是按 worker 分的,不是全局一把。** 这意味着 worker 0 在 close 文件时,worker 1 可以同时 fsync,worker 2 可以同时释放内存——三套队列三把锁,彼此独立。这是"按资源域分锁"的标准实践,锁粒度刚好等于"会被同一个 worker 串行处理的任务集合"。

`bioInit`([bio.c:127](../../redis-8.0.2/src/bio.c#L127)) 启动时一次性初始化这三套原语:

```c
/* bio.c:134-138 */
for (j = 0; j < BIO_WORKER_NUM; j++) {
    pthread_mutex_init(&bio_mutex[j],NULL);
    pthread_cond_init(&bio_newjob_cond[j],NULL);
    bio_jobs[j] = listCreate();
}
```

每个 worker 对应一个 `bio_jobs[j]` 双向链表(用 adlist.list,FIFO 队列)。然后 `[bio.c:171-178](../../redis-8.0.2/src/bio.c#L171)` 起 `pthread_create` 三个线程,每个线程跑同一个函数 `bioProcessBackgroundJobs`,但传入的 `arg` 是它负责的 worker 编号:

```c
/* bio.c:171-178 */
for (j = 0; j < BIO_WORKER_NUM; j++) {
    void *arg = (void*)(unsigned long) j;
    if (pthread_create(&thread,&attr,bioProcessBackgroundJobs,arg) != 0) {
        serverLog(LL_WARNING, "Fatal: Can't initialize Background Jobs. ...");
        exit(1);
    }
    bio_threads[j] = thread;
}
```

**所有 worker 共用一份代码,差异只在 `arg = worker_id`**——这是 Redis 把 worker 函数统一化的关键。worker 0 进函数后知道自己是 close 工人,worker 2 进函数后知道自己是 lazyfree 工人,各自的 `bio_mutex[j]` / `bio_jobs[j]` 互不干扰。

> **钉死这件事**:BIO 的同步原语矩阵 `bio_mutex[3]` / `bio_newjob_cond[3]` / `bio_jobs[3]` 是按"worker 编号"而不是"opcode 编号"分的。三个 worker 各自一条独立链表,加锁只锁自己那条,这意味着三类脏活在物理上就互相不阻塞。这是"按资源域分锁"在 BIO 里的最直接体现。

### 19.2.3 主线程投递:`bioSubmitJob` 只有五步

主线程把一个活外包出去,走的是 `[bio.c:181](../../redis-8.0.2/src/bio.c#L181)`:

```c
/* bio.c:181-189 */
void bioSubmitJob(int type, bio_job *job) {
    job->header.type = type;                        // 0. 打上 type tag
    unsigned long worker = bio_job_to_worker[type]; // 1. 查表:这个活归哪个 worker
    pthread_mutex_lock(&bio_mutex[worker]);         // 2. 加该 worker 的队列锁
    listAddNodeTail(bio_jobs[worker], job);         // 3. 任务进队尾
    bio_jobs_counter[type]++;                       // 4. 计数器 +1
    pthread_cond_signal(&bio_newjob_cond[worker]);  // 5. 唤醒可能在等条件变量的后台线程
    pthread_mutex_unlock(&bio_mutex[worker]);
}
```

注意第 3 步进队的是**指针**(`bio_job *`),不是对象本身——主线程根本没碰对象的内容,只把"以后请帮我释放它"这个委托丢进队列。第 5 步的 `pthread_cond_signal` 是个轻量操作,只是唤醒在 `pthread_cond_wait` 上阻塞的后台线程,不等待它干活。整段函数在主线程里几乎瞬间返回(几次原子操作 + 一次 syscall 都不到),这就是"投递代价极小"的关键。

为什么 `bio_jobs_counter` 按 opcode 而不是 worker 分?因为它要支持 `bioPendingJobsOfType(BIO_AOF_FSYNC)` 这种查询("AOF fsync 还有多少没干完")——`[bio.c:371-377](../../redis-8.0.2/src/bio.c#L371)` 直接读这个计数器。这种"按 opcode 查待办数"的需求是 AOF 重写、`WAIT AOF` 命令等场景要的。

> **钉死这件事**:`bioSubmitJob` 的五步是"打 tag → 查表分 worker → 持锁进队 → signal 唤醒 → 放锁"。主线程在临界区里只做"链表 append + 计数器自增 + signal",这些都是常数时间。真正耗时的工作(遍历释放 / fsync / close)永远在临界区之外执行——主线程投递的代价与对象大小完全无关。

### 19.2.4 后台线程消费:"持锁取任务、放锁干任务、回锁摘链表"

后台线程的主循环在 `[bio.c:257](../../redis-8.0.2/src/bio.c#L257)` 的 `bioProcessBackgroundJobs`。它的循环骨架极其标准,但藏着两个值得点名的设计:

```c
/* bio.c:271-367,精简后的关键骨架 */
pthread_mutex_lock(&bio_mutex[worker]);          // 进循环前先持锁
while(1) {
    /* 队列空就等条件变量:原子地释放锁 + 阻塞,被唤醒后自动重获锁 */
    if (listLength(bio_jobs[worker]) == 0) {
        pthread_cond_wait(&bio_newjob_cond[worker], &bio_mutex[worker]);
        continue;
    }
    ln  = listFirst(bio_jobs[worker]);            // 从队头取(FIFO)
    job = ln->value;
    pthread_mutex_unlock(&bio_mutex[worker]);     // ★ 关键:取出后立刻放锁!
    /* ===== 以下处理 job 期间完全不持队列锁 ===== */
    int job_type = job->header.type;
    if (job_type == BIO_CLOSE_FILE) {
        /* 需要时先 fsync 再 reclaim cache 再 close,见 bio.c:298-310 */
        close(job->fd_args.fd);
    } else if (job_type == BIO_AOF_FSYNC || job_type == BIO_CLOSE_AOF) {
        if (redis_fsync(job->fd_args.fd) == -1 && errno != EBADF && errno != EINVAL) {
            atomicSet(server.aof_bio_fsync_status, C_ERR);  // 失败回执
            atomicSet(server.aof_bio_fsync_errno, errno);
        } else {
            atomicSet(server.aof_bio_fsync_status, C_OK);   // 成功回执
            atomicSet(server.fsynced_reploff_pending, job->fd_args.offset);
        }
        if (job_type == BIO_CLOSE_AOF) close(job->fd_args.fd);
    } else if (job_type == BIO_LAZY_FREE) {
        job->free_args.free_fn(job->free_args.free_args);   // 调用注册的释放函数
    } else if (job_type 是 BIO_COMP_RQ_*) {
        /* 把回执项塞进 bio_comp_list,write(pipe) 唤醒主线程,见 19.3.2 */
    }
    zfree(job);
    pthread_mutex_lock(&bio_mutex[worker]);       // 干完活重新持锁
    listDelNode(bio_jobs[worker], ln);            // 从链表摘掉已处理节点
    bio_jobs_counter[job_type]--;                 // 计数器 -1
    pthread_cond_signal(&bio_newjob_cond[worker]);// 唤醒可能等"队列空"的 bioDrainWorker
}
```

第一个值得点名的设计:**"持锁取任务、放锁干任务"**。如果后台线程在 `redis_fsync()` 那几十毫秒里还抱着队列锁,主线程投递新任务就得在那几十毫秒里阻塞在 `bio_mutex[worker]` 上——这违背了"投递瞬间返回"的承诺。所以代码刻意把"取 job 指针"和"执行 job"分开,持锁区间缩到最短——只够做一次 `listFirst` 和一次指针读。这是并发编程里降低锁粒度的标准套路(`bio.c:289-293` 注释明说:"It is now possible to unlock the background system as we know have a stand alone job structure to process."),Redis 在这里用得干净利落。

第二个值得点名的设计:**干完活重新持锁摘链表 + 再 signal 一次**(`bio.c:363-366`)。为什么要再 signal?因为 `bioDrainWorker`([bio.c:382](../../redis-8.0.2/src/bio.c#L382)) 这个 API 会等"队列空"——它在 `pthread_cond_wait` 上睡,worker 处理完一个任务要让队列变短、且可能变空,所以必须 signal 唤醒它检查。`bioDrainWorker` 在哪用?AOF 重写、replication 切换这些"必须等后台 fsync 全部干完才能继续"的边界场景,主线程会显式 drain worker 1。

`pthread_cond_wait` 在等待时会原子地"释放 mutex + 阻塞线程",被 `signal` 唤醒后又原子地"重新持有 mutex + 返回"。这就保证了后台线程不空转、不忙等,主线程投递后立刻能被唤醒。

> **钉死这件事**:BIO worker 的循环骨架是并发编程里"降低锁粒度"的活标本:持锁区间被压到最短,只够取一个 job 指针;真正耗时的工作(fsync 几十毫秒、释放百万元素几毫秒)全部在临界区之外执行。这个写法保证了主线程投递新任务永远不会被一个慢 fsync 阻塞——它顶多在 `bio_mutex` 上排一小会(取任务那一瞬),立刻就能进队。

### 19.2.5 bio_job union:一个 tag + 三种 payload

`[bio.c:90](../../redis-8.0.2/src/bio.c#L90)` 的 `bio_job` 是个 union,但所有 union 成员的第一个字段都是 `int type`——这是 C 里实现"带 tag 的 union"的惯用法:

```c
/* bio.c:90-117 */
typedef union bio_job {
    struct {
        int type; /* Job-type tag. 必须是所有 union 成员的第一个字段 */
    } header;

    /* fd 类 job(close / fsync / close_aof)的 payload */
    struct {
        int type;
        int fd;                            /* 文件描述符 */
        long long offset;                  /* AOF 专用:fsync 进度偏移 */
        unsigned need_fsync:1;             /* close 前要不要先 fsync */
        unsigned need_reclaim_cache:1;     /* close 前要不要回收页缓存 */
    } fd_args;

    /* lazyfree 类 job 的 payload:回调 + 柔性数组尾巴 */
    struct {
        int type;
        lazy_free_fn *free_fn;             /* 注册的释放函数 */
        void *free_args[];                 /* C99 柔性数组,携带任意个参数指针 */
    } free_args;

    /* 带回执的 job(comp_rq) */
    struct {
        int type;
        comp_fn *fn;                       /* 完成后回主线程调的回调 */
        uint64_t arg;
        void *ptr;
    } comp_rq;
} bio_job;
```

**这套设计有三处精妙**:

第一,**只要拿到 `bio_job*`,先读 `header.type` 就能知道这是什么 job**。worker 不需要复杂的类型分发,一个 `switch(job->header.type)` 就够。这是"带 tag 的 union"在 C 里的标准用法(对应 Rust 的 enum、TypeScript 的 discriminated union)。

第二,**`free_args` 成员用了 C99 柔性数组**(`void *free_args[]`)。一个 lazyfree job 可以携带任意个参数指针——`lazyfreeFreeObject` 带 1 个 obj,`lazyfreeFreeDatabase` 带 3 个(oldkeys/oldexpires/oldHfe),`lazyFreeLuaScripts` 带 3 个(lua_scripts/lru_list/lua)。`bioCreateLazyFreeJob` 用变参 `va_list` 把它们逐个填进去(`[bio.c:191-204](../../redis-8.0.2/src/bio.c#L191)`):

```c
/* bio.c:191-204 */
void bioCreateLazyFreeJob(lazy_free_fn free_fn, int arg_count, ...) {
    va_list valist;
    bio_job *job = zmalloc(sizeof(*job) + sizeof(void *) * (arg_count));
    job->free_args.free_fn = free_fn;
    va_start(valist, arg_count);
    for (int i = 0; i < arg_count; i++) {
        job->free_args.free_args[i] = va_arg(valist, void *);
    }
    va_end(valist);
    bioSubmitJob(BIO_LAZY_FREE, job);
}
```

注意 `zmalloc(sizeof(*job) + sizeof(void *) * arg_count)` 这一行——柔性数组按需分配,带几个参数就多分配几个指针位。这是把"异构任务"统一进同一队列的关键。

第三,**union 本身省内存**。三种 payload 共用同一片空间,同一个时刻只用一种。一个 `bio_job` 的固定开销就是 header(4 字节)对齐后的大小,加上按需分配的 free_args 尾巴,比"每个 job 都带最大字段集"省。

> **钉死这件事**:BIO 的 `bio_job` 用"union + 头部 type tag + 柔性数组尾巴"三件套,把 close/fsync/lazyfree/comp_rq 四类异构任务统一进同一个 `bio_job*` 队列。worker 一律 `job->header.type` 分发,case 内部按需读 `fd_args` / `free_args` / `comp_rq`。这是 C 里"无继承多态"的标准打法,对应到现代语言就是带 payload 的 enum。

### 19.2.6 lazyfree:DEL 的小弟 UNLINK,以及那五个配置开关

光有 BIO 线程还不够,得有用户层面的入口。最直接的就是 `UNLINK` 命令。看 `[db.c:941](../../redis-8.0.2/src/db.c#L941)`:

```c
/* db.c:960-965 */
void delCommand(client *c) {
    delGenericCommand(c, server.lazyfree_lazy_user_del);  // 由配置决定同步还是异步
}
void unlinkCommand(client *c) {
    delGenericCommand(c, 1);   // 第二个参数 lazy=1,强制走异步
}
```

`UNLINK` 就是"我知道这个 key 可能很大,我宁可晚一点回收内存,也不要卡主线程"的 `DEL`。两者的差异只在 `dbGenericDelete` 的 `async` 参数上([db.c:471](../../redis-8.0.2/src/db.c#L471))。这个函数的真实逻辑比初学者想象的更精细:

```c
/* db.c:471-510,精简 */
int dbGenericDelete(redisDb *db, robj *key, int async, int flags) {
    int slot = getKeySlot(key->ptr);
    dictEntry *de = kvstoreDictTwoPhaseUnlinkFind(db->keys, slot, key->ptr, &plink, &table);
    if (de) {
        robj *val = dictGetVal(de);

        /* ★ 先 incr 一次,保护 val 在 module 通知期间不被意外释放 */
        incrRefCount(val);
        moduleNotifyKeyUnlink(key, val, db->id, flags);  /* 模块通知 */
        signalDeletedKeyAsReady(db, key, val->type);     /* 唤醒阻塞客户端 */
        /* ★ 抵消刚才的 incr,refcount 回到原值 */
        decrRefCount(val);
        /* 注释@493-494 明说:"We should call decr before freeObjAsync.
         * If not, the refcount may be greater than 1, so freeObjAsync doesn't work" */
        if (async) {
            freeObjAsync(key, dictGetVal(de), db->id);       /* 异步释放 */
            kvstoreDictSetVal(db->keys, slot, de, NULL);     /* ★ 先把 val 置 NULL */
        }
        kvstoreDictDelete(db->expires, slot, key->ptr);
        kvstoreDictTwoPhaseUnlinkFree(db->keys, slot, de, plink, table);  /* 摘除字典 entry */
        return 1;
    }
}
```

这里有三个细节值得挨个点:

第一,**`incrRefCount(val)` / `decrRefCount(val)` 这一对保护**(`db.c:488` / `db.c:495`)。`moduleNotifyKeyUnlink` 可能调用模块代码,模块代码可能调用 `dbUnshareStringValue` 之类会触发 `decrRefCount` 的函数。如果不先 incr 把 val 保护起来,模块可能在通知期间就把 val 释放了——主线程后面拿到的就是悬垂指针。

第二,**注释 @493-494 明说先 decr 再 freeObjAsync**。如果先调 `freeObjAsync` 再 decr,此刻 refcount 还是 2(`incrRefCount` 加过 1),`freeObjAsync` 内部的 `obj->refcount == 1` 判断就失败,会走同步释放路径而不是异步——失去 lazyfree 的意义。所以必须先 decr 让 refcount 回到原值(通常 1),再 freeObjAsync。

第三,**`kvstoreDictSetVal(db->keys, slot, de, NULL)` 这一行极其关键**(`db.c:499`)。它在摘除字典 entry 之前,先把 entry 里的 value 指针清成 NULL。这样后续 `kvstoreDictTwoPhaseUnlinkFree` 释放字典 entry 本身时,绝不会误把这个大对象在主线程里 `decrRefCount` 一次。大对象的所有权已经在 `freeObjAsync` 里转交给了 BIO 线程,主线程这边的字典里只剩一个 NULL 壳。

`freeObjAsync` 的本体在 `[lazyfree.c:184](../../redis-8.0.2/src/lazyfree.c#L184)`,核心就一个判断:

```c
/* lazyfree.c:181-196 */
#define LAZYFREE_THRESHOLD 64     /* lazyfree.c:181 */

void freeObjAsync(robj *key, robj *obj, int dbid) {
    size_t free_effort = lazyfreeGetFreeEffort(key, obj, dbid);
    /* 注释@186-189:"Note that if the object is shared, to reclaim it now it is not
     * possible. This rarely happens......sometimes the implementation of parts of
     * the Redis core may call incrRefCount() to protect objects, and then call dbDelete()." */
    if (free_effort > LAZYFREE_THRESHOLD && obj->refcount == 1) {
        atomicIncr(lazyfree_objects, 1);
        bioCreateLazyFreeJob(lazyfreeFreeObject, 1, obj);  // 投给 BIO worker 2
    } else {
        decrRefCount(obj);   // 小对象直接同步释放更快
    }
}
```

这里有一个极精彩的工程权衡——`LAZYFREE_THRESHOLD = 64`。`lazyfreeGetFreeEffort`([lazyfree.c:129](../../redis-8.0.2/src/lazyfree.c#L129))按编码估算"释放这个对象需要几次 zfree":

```c
/* lazyfree.c:129-174,按编码分流估算 */
size_t lazyfreeGetFreeEffort(robj *key, robj *obj, int dbid) {
    if (obj->type == OBJ_LIST && obj->encoding == OBJ_ENCODING_QUICKLIST) {
        return ((quicklist *)obj->ptr)->len;       /* quicklist 节点数 */
    } else if (obj->type == OBJ_SET && obj->encoding == OBJ_ENCODING_HT) {
        return dictSize((dict *)obj->ptr);          /* 哈希表元素数 */
    } else if (obj->type == OBJ_ZSET && obj->encoding == OBJ_ENCODING_SKIPLIST){
        return ((zset *)obj->ptr)->zsl->length;     /* 跳表长度 */
    } else if (obj->type == OBJ_HASH && obj->encoding == OBJ_ENCODING_HT) {
        return dictSize((dict *)obj->ptr);          /* 哈希表元素数 */
    } else if (obj->type == OBJ_STREAM) {
        /* stream:粗估 rax 节点数 + 消费者组 × PEL 大小 */
        return effort;
    } else if (obj->type == OBJ_MODULE) {
        return moduleGetFreeEffort(key, obj, dbid);
    } else {
        return 1; /* 其它(listpack 编码、字符串等)恒为 1 */
    }
}
```

**低于 64 次释放的对象,异步反而更慢**——因为要走加锁、进队列、唤醒、跨线程切换、回调这一整套开销。`lazyfree.c:176-180` 的注释写得直白:"If the value is composed of a few allocations, **to free in a lazy way is actually just slower**... So under a certain limit we just free the object synchronously."。这是取向④"简单优先"里很朴素的取舍:别为了政治正确的"全异步"而增加无谓开销,该同步就同步。

`obj->refcount == 1` 这个条件是安全边界:只有当对象没被任何地方共享(`incrRefCount` 保护过的不算)时,异步释放才安全。否则后台线程释放它时,主线程或别的引用方可能还在用——这就是 19.3 要展开的"能异步 vs 不能异步"的边界。

**五个配置开关。** 除了显式的 `UNLINK` / `FLUSHALL ASYNC`,Redis 还给了五个全局配置(`[redis.conf:1283-1300](../../redis-8.0.2/redis.conf#L1283)` 一带),让"该走异步的内部删除路径"自动走异步:

```text
# redis.conf:1283-1300
lazyfree-lazy-eviction no       # 内存满了被动淘汰 key 时
lazyfree-lazy-expire no         # 周期性淘汰过期 key 时
lazyfree-lazy-server-del no     # RENAME / SET 覆盖旧 key / SUNIONSTORE 这类"命令副作用删除"时
lazyfree-lazy-user-del no       # 把用户的 DEL 默认行为改成 UNLINK
lazyfree-lazy-user-flush no     # FLUSHALL/FLUSHDB 不带参数时的默认行为
```

对照源码:`[expire.c:709](../../redis-8.0.2/src/expire.c#L709)` 用 `server.lazyfree_lazy_expire`(`activeExpireCycle` 里删过期 key 时);`[evict.c:690](../../redis-8.0.2/src/evict.c#L690)` 用 `server.lazyfree_lazy_eviction`(内存满了被动淘汰时);`[db.c:370-371](../../redis-8.0.2/src/db.c#L370)` 和 `[db.c:526](../../redis-8.0.2/src/db.c#L526)` 的 `dbDelete` 用 `server.lazyfree_lazy_server_del`(`RENAME`、`SET` 覆盖旧 key 等命令副作用删除时)。**这五个开关不是五个新机制,只是把"是否调 `dbAsyncDelete`/`freeObjAsync`"的决策点从命令里提到配置。** 真正干活的还是同一套 BIO worker 2。

> **钉死这件事**:`LAZYFREE_THRESHOLD = 64` 是 Redis 在"全异步的政治正确"和"该同步就同步的工程务实"之间的明确分界线。释放代价 < 64 次的对象同步删更快(省了进队/唤醒/跨线程切换的一整套开销),> 64 才异步。这个数字不是拍脑袋——它对应的是"主线程一次 O(N) 释放的延迟能容忍到几毫秒"的工程经验值。同时 `obj->refcount == 1` 是安全边界:被共享的对象不能异步释放,否则就是 use-after-free。

### 19.2.7 FLUSHALL ASYNC:整个库换壳

`FLUSHALL` 删的是整库,可能是上亿个 key。这时候连"逐个摘除 + 走 freeObjAsync"都不可接受——逐个摘一亿次本身就要几秒。所以 `emptyDbAsync`([lazyfree.c:201](../../redis-8.0.2/src/lazyfree.c#L201))用的是另一招——**换壳**:

```c
/* lazyfree.c:201-215 */
void emptyDbAsync(redisDb *db) {
    int slot_count_bits = 0;
    int flags = KVSTORE_ALLOCATE_DICTS_ON_DEMAND;
    if (server.cluster_enabled) {
        slot_count_bits = CLUSTER_SLOT_MASK_BITS;
        flags |= KVSTORE_FREE_EMPTY_DICTS;
    }
    kvstore *oldkeys = db->keys, *oldexpires = db->expires;   // 抓住旧的整张表
    ebuckets oldHfe = db->hexpires;
    db->keys    = kvstoreCreate(&dbDictType, slot_count_bits, flags | KVSTORE_ALLOC_META_KEYS_HIST);
    db->expires = kvstoreCreate(&dbExpiresDictType, slot_count_bits, flags);  // 主线程:瞬间换上一张空表
    db->hexpires = ebCreate();
    atomicIncr(lazyfree_objects, kvstoreSize(oldkeys));        // 计数器一次性累加
    bioCreateLazyFreeJob(lazyfreeFreeDatabase, 3, oldkeys, oldexpires, oldHfe);
}
```

主线程做的事只有"抓住旧表指针 + 换一张新空表 + 进队",这是常数时间!**上百亿个 key 的释放被压缩成了三个指针赋值 + 一次进队。** 真正遍历释放整张 `kvstore` 的 `lazyfreeFreeDatabase`([lazyfree.c:23](../../redis-8.0.2/src/lazyfree.c#L23)) 全程在 BIO worker 2 里跑:

```c
/* lazyfree.c:23-41 */
void lazyfreeFreeDatabase(void *args[]) {
    kvstore *da1 = args[0];
    kvstore *da2 = args[1];
    ebuckets oldHfe = args[2];
    ebDestroy(&oldHfe, &hashExpireBucketsType, NULL);
    size_t numkeys = kvstoreSize(da1);
    kvstoreRelease(da1);    /* 真正的遍历释放,可能几秒 */
    kvstoreRelease(da2);
    atomicDecr(lazyfree_objects, numkeys);
    atomicIncr(lazyfreed_objects, numkeys);
#if defined(USE_JEMALLOC)
    /* 顺手把 jemalloc 的线程 cache 清掉,让回收的内存真正还给 OS */
    je_mallctl("thread.tcache.flush", NULL, NULL, NULL, 0);
    jemalloc_purge();
#endif
}
```

注意末尾的 jemalloc 清理:`je_mallctl("thread.tcache.flush", ...)` 清 worker 线程的 tcache,`jemalloc_purge()` 让 arena 把空闲页归还给 OS。这是"换壳"之后真正释放物理内存的最后一公里——没有它,内存还在 jemalloc 的 arena 里挂着,操作系统看不到。

这个"换壳"的思路不止用在 `emptyDbAsync`。`lazyfree.c` 里还有一组"释放整个内部结构"的换壳函数,都用同一个阈值判断:`freeTrackingRadixTreeAsync`(lazyfree.c:219)、`freeErrorsRadixTreeAsync`(lazyfree.c:231)、`freeLuaScriptsAsync`(lazyfree.c:243)、`freeFunctionsAsync`(lazyfree.c:253)、`freeReplicationBacklogRefMemAsync`(lazyfree.c:264)。它们各自判断 `> LAZYFREE_THRESHOLD` 才异步——这是同一个工程权衡的复用。

这是取向①"把耗时从主线程解放"最极致的体现——主线程的语义是"清空完成",但物理回收发生在后台。

```text
                  FLUSHALL ASYNC 的换壳瞬间
                  ===========================

   主线程(同步,O(1))                  BIO worker 2(异步,O(N))
   ─────────────────                   ─────────────────────────
                                       
   db->keys ──────┐                    oldkeys(dict 1亿 entry)
   db->expires ───┼──抓走→ bioCreateLazyFreeJob ──→ lazyfreeFreeDatabase
   db->hexpires ──┘    (3 个指针 + job)                │
                                                       ▼
   db->keys    = kvstoreCreate(空)             遍历 1亿 entry 逐个 zfree
   db->expires = kvstoreCreate(空)              (可能几秒,主线程完全无感)
   db->hexpires = ebCreate()                    
                                               kvstoreRelease(oldkeys)
                                               jemalloc_purge()  ← 还给 OS
                                               
   立刻返回 OK 给客户端                       完成后 atomicDecr(lazyfree_objects)
```

> **钉死这件事**:`emptyDbAsync` 用"换壳"把"释放整库"压成 O(1):主线程抓住旧三指针、换一张新空表、进队,BIO worker 后台慢慢遍历释放旧表。这是取向①最极致的体现——主线程的对外语义是"清空完成",但真正的物理回收发生在后台。同样的换壳思路被复用在 tracking 表、errors 表、lua_scripts、functions ctx、replication backlog 这一族"释放整个内部结构"的路径上。

## 19.3 精妙技巧点拨

### 19.3.1 为什么后台线程敢加锁,主线程却不敢?

这是理解整个 BIO 设计的核心问题。回顾全书主线:Redis 主线程之所以敢不加锁地访问所有数据结构,是因为它独占这些数据——所有客户端命令都在主线程的事件循环里串行执行,没有并发访问者,自然不需要锁。

BIO 线程之所以敢用 `pthread_mutex`,是因为它们碰的数据**和主线程当前正在服务的数据之间没有重叠**:

- **lazyfree worker** 释放的是已经被 `kvstoreDictSetVal(..., NULL)` 清空、并从字典里 `kvstoreDictTwoPhaseUnlinkFree` 摘掉的对象。主线程再也找不到它(字典里没引用了),也不会有别的客户端命令命中它(它已经不在 keyspace 里了)。这个对象的所有权被"原子地"移交给了后台线程。
- **fsync worker** 调 `redis_fsync(aof_fd)`,操作的是内核里那个 fd 的页缓存。主线程后续还会往这个 fd `write` 新数据——但 `write` 和 `fdatasync` 对同一个 fd 是允许并发的(内核会处理一致性),且 Redis 用 `aof_bio_fsync_status` / `aof_bio_fsync_errno` / `fsynced_reploff_pending` 这一组原子变量(`[server.h:1959-1960](../../redis-8.0.2/src/server.h#L1959)` 和 `[server.h:2026](../../redis-8.0.2/src/server.h#L2026)`)把状态传回主线程,避免共用普通变量。worker 的状态回写在 `[bio.c:319-328](../../redis-8.0.2/src/bio.c#L319)`:成功时 `atomicSet(aof_bio_fsync_status, C_OK)` + `atomicSet(fsynced_reploff_pending, offset)`,失败时 `atomicSet(aof_bio_fsync_status, C_ERR)` + `atomicSet(aof_bio_fsync_errno, errno)`。主线程在 `flushAppendOnlyFile` 里通过 `atomicGet(aof_bio_fsync_status, ...)`(`[aof.c:1059-1064](../../redis-8.0.2/src/aof.c#L1059)`)轮询这个状态,发现失败就报错甚至 `exit(1)`(`appendfsync=always` 下)。
- **close worker** 关的是主线程已经不再使用的 fd,所有权同样已经移交。

所以加锁的分界非常清楚:**锁只保护"BIO 队列"本身这个共享数据结构(主线程写队尾、worker 读队头),不保护 Redis 的业务数据**。业务数据是通过"摘除 + 所有权移交"来实现无锁传递的,根本不进入临界区。这就是为什么 Redis 可以一边宣称"单线程无锁",一边在 BIO 里用 `pthread_mutex`——两者保护的是完全不同的资源域。

> **钉死这件事**:BIO 的锁只保护"BIO 队列本身"(主线程写队尾、worker 读队头),**绝不保护业务数据**。业务数据是通过"摘除 + 所有权移交"实现无锁跨线程传递的:lazyfree 对象先从字典里 TwoPhaseUnlink 摘掉(主线程再也找不到)、val 置 NULL(主线程不会误释放),再交给 worker;fsync 操作的是内核 fd,write 和 fdatasync 内核允许并发,失败状态用原子变量回传。这就是"单线程无锁"和"BIO 用锁"并存不矛盾的根本——两个锁保护的资源域互不重叠。

### 19.3.2 任务完成的回执:pipe 唤醒事件循环

后台线程干完活,有时主线程需要知道——比如某个 `BIO_COMP_RQ_*`(带 completion request 的 job)需要 worker 完成后回调一个主线程函数。但主线程在事件循环里阻塞,后台线程不能直接调它的回调——那会破坏"主线程串行执行"的契约(同一个 dict 被两个线程同时碰,没有锁就是灾难)。

`[bio.c:75-80](../../redis-8.0.2/src/bio.c#L75)` 的做法很 Unix:用一根 pipe + 一把独立的锁 + 一个完成回执列表。

```c
/* bio.c:75-80 */
/* The bio_comp_list is used to hold completion job responses and to handover
 * the processing of them to the main thread. */
static list *bio_comp_list;            // 完成回执列表
static pthread_mutex_t bio_mutex_comp; // 保护这个列表的锁(独立于 bio_mutex)
static int job_comp_pipe[2];           // 唤醒事件循环的 pipe
```

`bioInit` 里(`[bio.c:149-159](../../redis-8.0.2/src/bio.c#L149)`)这根 pipe 用 `anetPipe` 创建成**两端都非阻塞 + close-on-exec**,然后把读端注册成主线程事件循环的一个 `AE_READABLE` 事件,回调是 `bioPipeReadJobCompList`:

```c
/* bio.c:149-159 */
if (anetPipe(job_comp_pipe, O_CLOEXEC|O_NONBLOCK, O_CLOEXEC|O_NONBLOCK) == -1) {
    serverLog(LL_WARNING, "Can't create the pipe for bio thread: %s", strerror(errno));
    exit(1);
}
/* Register a readable event for the pipe used to awake the event loop on job completion */
if (aeCreateFileEvent(server.el, job_comp_pipe[0], AE_READABLE,
                      bioPipeReadJobCompList, NULL) == AE_ERR) {
    serverPanic("Error registering the readable event for the bio pipe.");
}
```

后台线程干完一个 `BIO_COMP_RQ_*` job,把回执项塞进 `bio_comp_list` 并 `write(job_comp_pipe[1], "A", 1)`(`[bio.c:343-355](../../redis-8.0.2/src/bio.c#L343)`):

```c
/* bio.c:343-355 */
bio_comp_item *comp_rsp = zmalloc(sizeof(bio_comp_item));
comp_rsp->func = job->comp_rq.fn;
comp_rsp->arg  = job->comp_rq.arg;
comp_rsp->ptr  = job->comp_rq.ptr;

pthread_mutex_lock(&bio_mutex_comp);
listAddNodeTail(bio_comp_list, comp_rsp);
pthread_mutex_unlock(&bio_mutex_comp);

if (write(job_comp_pipe[1],"A",1) != 1) {
    /* Pipe is non-blocking, write() may fail if it's full. */
}
```

主线程的事件循环被 pipe 唤醒后,跑 `bioPipeReadJobCompList`([bio.c:415](../../redis-8.0.2/src/bio.c#L415))。它干三件:**`read` 把 pipe 里的字节清空**(可能多个 worker 同时写,一次性读 128 字节清光)、**持 `bio_mutex_comp` 把 `bio_comp_list` 整个换出来**(经典"swap list"——主线程拿到 tmp_list 后立刻放锁,后续回调在锁外执行)、**逐个调 `comp_rsp->func(arg, ptr)`** 完成回执分发:

```c
/* bio.c:415-444,精简 */
void bioPipeReadJobCompList(aeEventLoop *el, int fd, void *privdata, int mask) {
    char buf[128];
    list *tmp_list = NULL;

    while (read(fd, buf, sizeof(buf)) == sizeof(buf));   /* 清空 pipe */

    pthread_mutex_lock(&bio_mutex_comp);
    if (listLength(bio_comp_list)) {
        tmp_list = bio_comp_list;                         /* ★ 整个 list 换出来 */
        bio_comp_list = listCreate();                     /* 给 worker 一个新的空 list */
    }
    pthread_mutex_unlock(&bio_mutex_comp);

    if (!tmp_list) return;

    /* 后续回调在 bio_mutex_comp 之外执行——主线程串行上下文,无需加业务锁 */
    while (listLength(tmp_list)) {
        listNode *ln = listFirst(tmp_list);
        bio_comp_item *rsp = ln->value;
        listDelNode(tmp_list, ln);
        rsp->func(rsp->arg, rsp->ptr);                    /* 调主线程注册的回调 */
        zfree(rsp);
    }
    listRelease(tmp_list);
}
```

这套设计有四个精彩之处:

第一,**后台线程的"通知"退化成一次 `write(1 字节)`**——极快,不阻塞。
第二,**主线程在自己的事件循环里、在自己的串行上下文里处理回执**,**和别的命令处理在同一个线程**,无需加业务锁——这呼应第二章 ae 的"所有串行"契约。
第三,**pipe 是非阻塞的**——就算主线程暂时没读,pipe 缓冲满了,后台线程的 `write` 静默失败也无伤大雅,因为真正的回执在 `bio_comp_list` 里,不会丢(下次有 worker 写成功就唤醒)。
第四,**"swap list"技巧**(`tmp_list = bio_comp_list; bio_comp_list = listCreate()`)把主线程持 `bio_mutex_comp` 的时间压到最短——只够做一次指针交换。后续逐个调回执回调可能很慢(回调里可能改业务数据),但那已经在锁外,不阻塞 worker 投递新回执。

这是"跨线程通知单线程事件循环"的标准解法,和第二章的事件循环、第二十章的 IO 多线程会形成呼应。

> **钉死这件事**:BIO 用"非阻塞 pipe + 独立锁 + swap list"三件套,实现"后台线程通知单线程事件循环"。后台线程的 `write(pipe, 1字节)` 只负责唤醒,真正的回执数据在 `bio_comp_list` 里;主线程被唤醒后用 swap list 把整个回执列表换出来,持锁时间压到一次指针交换,后续回调在自己的串行上下文里执行,和别的命令处理无并发。这呼应第二章 ae——所有跨线程通知最终都要落到"唤醒主线程的事件循环"这一招上。

### 19.3.3 BIO 和 IO 多线程(下一章)的根本区别

容易混淆的一点:BIO 线程和 Redis 8.0 的 IO 多线程(见 `[iothread.c:14](../../redis-8.0.2/src/iothread.c#L14)` 的 `IOThreads[IO_THREADS_MAX_NUM]`)都是后台线程,但它们解决的问题完全不同:

| 维度 | BIO 线程 | IO 多线程 |
|---|---|---|
| 数量 | 固定 3 个,按任务类型分 | 可配(`io-threads`,默认 1=单线程),按 client 分 |
| 处理对象 | **已脱离主结构的对象、文件 fd** | **活跃 client 的读写缓冲** |
| 与主线程的关系 | 完全异步,主线程不等待 | 主线程和 IO 线程组成流水线协作 |
| 是否碰业务数据 | 不碰(对象已摘除) | 读命令时解析协议、写时序列化 reply,会碰 client 状态 |
| 锁的范围 | 队列锁 + 原子状态变量 | client 列表的细粒度锁 + paused 状态机 |
| 解决的痛点 | **长尾耗时**(单次操作几十毫秒) | **网络吞吐瓶颈**(总读写量太大) |

一句话:**BIO 是"把脏活外包,主线程不回头等"**;**IO 多线程是"把网络读写并行化,主线程和 IO 线程组成一条流水线一起往前走"**。两者都服务于取向①,但切入的痛点不同——BIO 砍的是"长尾耗时"(一个 fsync 几十毫秒、一个百万元素释放几十毫秒,这些"突然一下很慢"的事),IO 多线程砍的是"吞吐瓶颈"(总网络 IO 量太大,主线程一个人 readv/writev 不过来)。

这也是为什么本章作为 P6 的开篇:先讲清楚最纯粹、最经典的"后台线程外包"模型,下一章再讲更复杂的主从协作模型。

> **钉死这件事**:BIO 和 IO 多线程都是后台线程,但痛点不同——BIO 砍长尾耗时(单次操作几十毫秒),IO 多线程砍吞吐瓶颈(总网络量太大)。BIO 主线程"投递完就走",IO 多线程主线程"分发后等齐才继续"。这是同一台机器上两种截然不同的多线程用法,理解它们的分野比理解它们各自怎么实现更重要。

### 19.3.4 fsync 失败的状态回传:异步不等于不可靠

这是初学者最容易忽略、却是 Redis 守住"可靠性"底线的关键细节。`appendfsync=everysec` 时,fsync 在 BIO worker 1 里跑,如果它失败了怎么办?如果什么都不做,主线程根本不知道,继续 write 新数据,直到崩溃——这时 AOF 可能已经损坏,但用户毫无察觉。

Redis 的处理在 `[bio.c:315-329](../../redis-8.0.2/src/bio.c#L315)`:fsync 失败时,worker 用 `atomicSet` 把失败状态写进两个原子变量:

```c
/* bio.c:315-329,精简 */
if (redis_fsync(job->fd_args.fd) == -1 &&
    errno != EBADF && errno != EINVAL)        /* EBADF/EINVAL:fd 已被主线程关掉重用,不是真失败 */
{
    int last_status;
    atomicGet(server.aof_bio_fsync_status, last_status);
    atomicSet(server.aof_bio_fsync_status, C_ERR);    /* 失败标记 */
    atomicSet(server.aof_bio_fsync_errno, errno);     /* 失败 errno */
    if (last_status == C_OK) {                         /* 只在"上次成功→这次失败"的边沿报一次 */
        serverLog(LL_WARNING, "Fail to fsync the AOF file: %s", strerror(errno));
    }
} else {
    atomicSet(server.aof_bio_fsync_status, C_OK);     /* 成功标记 */
    atomicSet(server.fsynced_reploff_pending, job->fd_args.offset);  /* 推进 fsync 进度 */
}
```

注意三个细节:

第一,**`errno != EBADF && errno != EINVAL` 这个过滤**。为什么 fsync 一个 fd 可能返回 EBADF?因为这个 fd 可能已经被主线程关掉(`aof_fd` 在 AOF 重写期间会换)、然后被内核重新分配给另一个 socket 或文件。这种"fd 失效"不是 fsync 真的失败,只是 worker 慢了一拍,fd 已经被复用——所以过滤掉,不算失败。

第二,**`last_status == C_OK` 边沿检测**。fsync 持续失败时,只在"成功→失败"的第一次跳变打一条 warning 日志,不重复刷屏。这是个贴心的细节——失败可能持续几千轮,刷屏会淹没真正重要的日志。

第三,**`fsynced_reploff_pending`**。fsync 成功时,worker 把"已 fsync 到哪个 offset"写进这个原子变量。主线程在 AOF 持久化进度推进、`WAIT AOF` 命令回复等场景读它——这是"异步 fsync 完成进度"的官方渠道。

主线程在 `flushAppendOnlyFile` 里(`[aof.c:1059-1064](../../redis-8.0.2/src/aof.c#L1059)`)轮询这个状态:

```c
/* aof.c:1059-1064 */
int aof_bio_fsync_status;
atomicGet(server.aof_bio_fsync_status, aof_bio_fsync_status);
if (aof_bio_fsync_status == C_ERR) {
    /* 处理 AOF fsync 失败:打印日志、可能 exit(1)(appendfsync=always 下) */
    ......
    atomicSet(server.aof_bio_fsync_status, C_OK);  /* 重置,等下次失败再报 */
}
```

`appendfsync=always` 下 fsync 失败会触发 `exit(1)`——这是"宁可挂掉也不接受不可靠"的硬姿态。`everysec` 下失败只报日志不退出,因为每秒重试一次,有机会自愈。

> **钉死这件事**:BIO 的异步 fsync 用三个原子变量(`aof_bio_fsync_status` / `aof_bio_fsync_errno` / `fsynced_reploff_pending`)把 worker 的成败进度传回主线程。EBADF/EINVAL 被过滤(fd 复用不是真失败)、边沿检测避免日志刷屏、`always` 模式失败直接 `exit(1)`。异步不等于不可靠——后台线程的成败必须能被主线程感知并处理,这是取向⑤(可靠性)在 BIO 上的直接落地。

## 19.4 几个散点:drain、kill、SIGALRM 屏蔽

**`bioDrainWorker`:等队列空。** `[bio.c:382-390](../../redis-8.0.2/src/bio.c#L382)` 提供了一个"等某个 worker 把队列里所有任务干完"的 API:

```c
/* bio.c:382-390 */
void bioDrainWorker(int job_type) {
    unsigned long worker = bio_job_to_worker[job_type];
    pthread_mutex_lock(&bio_mutex[worker]);
    while (listLength(bio_jobs[worker]) > 0) {
        pthread_cond_wait(&bio_newjob_cond[worker], &bio_mutex[worker]);
    }
    pthread_mutex_unlock(&bio_mutex[worker]);
}
```

它在 AOF 重写(`[aof.c:2561](../../redis-8.0.2/src/aof.c#L2561)` 显式 `bioDrainWorker(BIO_AOF_FSYNC)`)、replication 切换这些"必须等后台 fsync 全部干完才能继续"的边界场景被调。这就是为什么 19.2.4 节 worker 干完活要再 `pthread_cond_signal(&bio_newjob_cond[worker])`——drain 在等这个 signal 才能检查"队列是否空了"。

**`bioKillThreads`:关停时取消线程。** `[bio.c:396](../../redis-8.0.2/src/bio.c#L396)` 用 `pthread_cancel` + `pthread_join` 优雅关停所有 BIO 线程。Redis 关闭流程里会调它,保证 worker 不会在主线程已经释放了 server 数据结构之后还去碰 bio 队列。

**SIGALRM 屏蔽。** `[bio.c:272-278](../../redis-8.0.2/src/bio.c#L272)` 在 worker 进主循环前调 `pthread_sigmask(SIG_BLOCK, &sigset, NULL)` 屏蔽 SIGALRM,注释明说:"so we are sure that only the main thread will receive the watchdog signal"。Redis 的 watchdog(看门狗)是主线程用来检测事件循环卡顿的——如果让 worker 也收到 SIGALRM,watchdog 的语义就乱了。这是"信号分发要按线程切"的标准实践。

**栈大小 4MB。** `[bio.c:124](../../redis-8.0.2/src/bio.c#L124)` 定义 `REDIS_THREAD_STACK_SIZE (1024*1024*4)` = 4MB,`bioInit` 里把 worker 的栈强制设到这么大(`[bio.c:161-166](../../redis-8.0.2/src/bio.c#L161)`)。注释说:"Make sure we have enough stack to perform all the things we do in the main thread."。因为 worker 里会跑 `lazyfreeFreeObject` → `decrRefCount`,后者会递归触发各种对象的释放函数,栈消耗可能很大——主线程栈默认就大,worker 也要对齐,否则深递归会爆栈。

> **钉死这件事**:BIO 的散点里藏着四个工程细节:`bioDrainWorker` 用 `pthread_cond_wait` 等"队列空"(配合 worker 干完活的 signal);`bioKillThreads` 用 `pthread_cancel` + `pthread_join` 优雅关停;SIGALRM 屏蔽保证 watchdog 只给主线程;4MB 栈对齐主线程,防止深递归爆栈。这些都是"让三个后台线程跑得稳"的工程兜底,不是炫技。

## 章末:回扣、五个为什么、往哪钻

### 主线回扣

本章是**取向①(把耗时从主线程解放)的经典样板**。整章讲的就是"为了让主线程始终在毫秒级响应客户端,把哪些活赶出去"。对象释放、AOF fsync、文件 close——这三件事任何一个留在主线程,都会在某个瞬间让整个 Redis 卡几十毫秒。BIO 用三个固定的后台线程、三套队列锁、一组条件变量,把这三类活稳稳接走。主线程的职责被收敛成"做决策、做摘除、做转发",脏活累活一概不沾。

同时它还串起其它几条取向:

- **取向②(内存即数据库)** 倒逼了 lazyfree 的存在。正因为数据全在内存、对象就是数据库本身,一个 key 才可能"大到释放一次都要几十毫秒"。如果是基于磁盘的数据库,删除一行只是改个标记,真正的物理回收交给后台 compaction,根本没有这个问题。Redis 把内存当数据库,就必须自己解决内存回收的延迟问题——`UNLINK`、`emptyDbAsync` 的换壳、`LAZYFREE_THRESHOLD` 的阈值,都是为了在"内存即数据库"的前提下,守住取向①的延迟承诺。

- **取向③(编码自适应)** 在 `lazyfreeGetFreeEffort` 里间接出场。释放代价怎么估?得看编码——hash 表编码的 hash 释放代价是元素数,listpack 编码的 hash 释放代价就是 1(走 else 分支)。同一个逻辑类型,不同编码决定它走同步还是异步。编码自适应不仅影响读写性能,还影响释放路径的选择。

- **取向④(简单优先)** 体现在四处:三个固定线程而非线程池;按类型分线程而非优先级调度;`LAZYFREE_THRESHOLD` 这种"够用就同步、太大才异步"的朴素阈值;`bio_job` 用 union + type tag 而非继承多态。Redis 没有把 BIO 做成一个通用任务调度框架,而是把它做成了"刚好能解决这三个痛点"的最小系统。

- **取向⑤(可靠性靠持久化 + 复制)** 体现在 fsync worker 的状态回传:`aof_bio_fsync_status` 和 `aof_bio_fsync_errno` 用原子变量把 fsync 失败信息带回主线程(`[bio.c:319-321](../../redis-8.0.2/src/bio.c#L319)`),`appendfsync=always` 失败直接 `exit(1)`。异步不等于不可靠——后台线程的成败必须能被主线程感知并处理。`fsynced_reploff_pending` 还把 fsync 进度回传给主线程,支撑 `WAIT AOF` 命令和复制偏移量推进。

### 五个为什么

**Q1:为什么 BIO 恰好三个 worker,而不是一个动态线程池?**

按任务类型分线程换的是"确定的隔离性":fsync 慢永远不会拖累 lazyfree,某个百万元素 hash 释放慢永远不会拖累 fsync。一个动态线程池需要优先级调度、需要防饿死、需要负载均衡,代码量和 bug 面都大得多。Redis 算清了账:三类活天然互不依赖,三个固定线程 + 三套独立队列,问题就解决了——这是取向④的典型落地。

**Q2:`LAZYFREE_THRESHOLD = 64` 这个数怎么定的?**

这是"主线程一次同步释放的延迟能容忍到几毫秒"的工程经验值。一次 `zfree` 大约几十纳秒,64 次就是几微秒,远远低于主线程一轮事件循环的典型耗时(几十微秒到几毫秒)。所以 < 64 的对象同步删,主线程几乎感觉不到;> 64 才进队异步删,省掉进队/唤醒/跨线程切换的开销。这个数字不是理论推导,是 antirez 的工程经验值,Redis 至今没改过。

**Q3:fsync worker 失败时主线程怎么知道?会不会丢失败状态?**

不会丢。worker 用 `atomicSet(server.aof_bio_fsync_status, C_ERR)` 把失败写进原子变量(`bio.c:320`),这个状态会一直保留到主线程读它。主线程在 `flushAppendOnlyFile` 里 `atomicGet(server.aof_bio_fsync_status, ...)` 轮询(`aof.c:1059`),读到 C_ERR 就处理(打印日志 / `exit(1)`),然后 `atomicSet(..., C_OK)` 重置。这种"原子变量 + 轮询"是"异步状态回传"的最简方案——不需要 pipe、不需要条件变量,因为失败本来就是低频事件。

**Q4:`emptyDbAsync` 把旧三指针丢给后台,主线程立刻换新表,这中间主线程会访问到旧表吗?**

不会。换壳的瞬间,`db->keys` / `db->expires` / `db->hexpires` 三个字段被原子地赋成新空表的指针(主线程的视角),旧表只剩 worker 持有引用。主线程后续的所有命令访问的都是新表,根本找不到旧表的入口。这是"所有权原子移交"的最干净形式——没有锁,但不会 race,因为赋值之后主线程不再有路径访问旧表。

**Q5:BIO 的 pipe 唤醒机制和第二章 ae 的 `epoll_wait` 是什么关系?**

直接关系。BIO 在 `bioInit` 里把 pipe 读端注册成主线程事件循环的一个 `AE_READABLE` 事件(`bio.c:156`),它和客户端连接的 fd 是平等的——都被 `epoll_wait` 盯着。后台线程 `write(pipe, 1字节)` 后,主线程下一轮 `epoll_wait` 醒来,`fired[]` 数组里就有 pipe 的 fd,分派到 `bioPipeReadJobCompList` 回调。所以 BIO 的跨线程通知最终落到"第二章 ae 的 `epoll_wait` 把 pipe 当一个普通 fd 盯着"这一招上——这是"任何跨线程通知单线程事件循环"的标准解法。

### 想继续深入往哪钻

- 想看 BIO 队列状态的查询接口:读 `[bio.c](../../redis-8.0.2/src/bio.c) 的 `bioPendingJobsOfType`(bio.c:371,按 opcode 查待办数)、`bioDrainWorker`(bio.c:382,等队列空)、`bioKillThreads`(bio.c:396,优雅关停)。它们是 BIO 对外暴露的运维接口。
- 想看 AOF 重写期间怎么和 BIO 协作:读 `[aof.c](../../redis-8.0.2/src/aof.c) 的 `rewriteAppendOnlyFile`、`aof_fsync_status` 轮询路径(aof.c:1059)、`bioDrainWorker(BIO_AOF_FSYNC)`(aof.c:2561)。这是"主线程等后台 fsync 干完"的实战。
- 想理解 Redis 8.0 的 IO 多线程和 BIO 的协作:读 `[iothread.c](../../redis-8.0.2/src/iothread.c) 的 `IOThreads[IO_THREADS_MAX_NUM]`(iothread.c:14)、`sendPendingClientsToIOThreads`(iothread.c:293)、`processClientsOfAllIOThreads`(iothread.c:443)。这是下一章的主线。
- 想看 lazyfree 还用在了哪些"释放整个内部结构"的换壳路径:读 `[lazyfree.c](../../redis-8.0.2/src/lazyfree.c) 的 `freeTrackingRadixTreeAsync`(lazyfree.c:219)、`freeErrorsRadixTreeAsync`(lazyfree.c:231)、`freeLuaScriptsAsync`(lazyfree.c:243)、`freeFunctionsAsync`(lazyfree.c:253)、`freeReplicationBacklogRefMemAsync`(lazyfree.c:264)。它们都用同一个 `LAZYFREE_THRESHOLD` 判断。

### 引出下一章

至此 BIO 我们讲完了:三个固定 worker、三套队列、一个 union、一根 pipe,把"释放对象、fsync、close"这三类长尾脏活外包得干干净净。但它有个隐含前提——这些活**彼此独立、可以慢慢做**。可如果痛点是"网络读写吞吐量上不去,主线程在 `readv`/`writev` 上花太多时间"呢?这时候 BIO 的"丢进队列异步干"模型不灵了——因为网络读写必须和主线程的命令处理紧密协作(读到一半就要准备解析,写完一批才能继续处理下一个 client)。

下一章讲的 IO 多线程,会展示一种完全不同的多线程协作模型:主线程和 IO 线程组成一条流水线,用更精细的同步点(pause/resume 状态机)、更细的锁粒度(per-client 锁),把网络这一层也并发起来。从"BIO 的纯异步外包"到"IO 多线程的紧耦合协作",读者会看到 Redis 在不同痛点下对多线程的两种截然不同的用法——这也是 P6 这一篇要对照讲清楚的核心。

---

## 验证物:如何亲手确认本章的设计

> 说明:本书写作环境为 Windows,无法直接运行 redis-server(8.0 依赖 fork/epoll 等 Linux 特性)。以下 (1) gdb 断点脚本 (2) 源码常量锚点 (3) INFO 观察项 均为可复现的精确指引,供读者在 Linux 环境(Ubuntu 22.04 / CentOS 8 等)对 redis-8.0.2 源码 `make no-opt`(Makefile 里 no-opt 目标会去掉 -O2 加 -g)编译后自行验证。**本书不附编造的运行输出**——凡未实跑的,只给脚本与预期观察变量,不写具体数值。

### 1. gdb 断点脚本

编译:`cd redis-8.0.2 && make no-opt`(带 -g)
启动:`gdb ./src/redis-server`,另一终端 `redis-cli`。

```gdb
(gdb) break bioSubmitJob                # 主线程投递入口,bio.c:181
(gdb) break bioProcessBackgroundJobs    # worker 主循环,bio.c:257
(gdb) break bio.c:289                   # ln = listFirst(bio_jobs[worker]) —— 持锁取任务
(gdb) break bio.c:293                   # pthread_mutex_unlock —— 放锁干任务分界
(gdb) break bio.c:339                   # job->free_args.free_fn(job->free_args.free_args) —— lazyfree 真正释放
(gdb) break bioPipeReadJobCompList      # 主线程读 pipe 回执,bio.c:415
(gdb) break freeObjAsync                # lazyfree.c:184,看阈值判断
(gdb) break lazyfreeGetFreeEffort       # lazyfree.c:129,看按编码估算
(gdb) break emptyDbAsync                # lazyfree.c:201,看换壳
(gdb) break lazyfreeFreeDatabase        # lazyfree.c:23,后台真正释放整库
(gdb) run --port 6379

# redis-cli 执行:UNLINK bigkey(bigkey 是一个百万元素的 hash)
# gdb 在 bioSubmitJob 停下,观察投递:
(gdb) print type                        # 预期:2(BIO_LAZY_FREE)
(gdb) print worker                      # 预期:2(lazyfree worker)
(gdb) print job->free_args.free_fn      # 预期:lazyfreeFreeObject
# 单步到 bio.c:185,观察进队:
(gdb) print listLength(bio_jobs[worker])  # 预期:进队前长度,进队后 +1

# 在 bioProcessBackgroundJobs 停下(worker 线程):
(gdb) print worker                      # 预期:2
(gdb) info threads                      # 预期:能看到 bio_lazy_free / bio_aof / bio_close_file 三个线程
# 单步到 bio.c:293(pthread_mutex_unlock),观察"放锁干任务":
(gdb) print job->free_args.free_fn      # 预期:lazyfreeFreeObject
```

**预期观察**(基于源码 [bio.c:257-367](../../redis-8.0.2/src/bio.c#L257) 的循环骨架,本书未实跑):worker 在 `bio_mutex[worker]` 上持锁取 `ln = listFirst(bio_jobs[worker])`,然后 `pthread_mutex_unlock` 放锁,真正调 `free_fn` 期间不持队列锁。`UNLINK bigkey` 后,`freeObjAsync` 内部 `lazyfreeGetFreeEffort` 返回值 > 64(因为百万元素 hash 的 effort = 元素数),走 `bioCreateLazyFreeJob` 异步路径。

### 2. 源码常量锚点(带行号,从 redis-8.0.2 源码 Grep 核实)

| 常量/字段 | 位置 | 值/说明 |
|----------|------|---------|
| `bio_worker_t` 枚举 | bio.h:16-21 | CLOSE_FILE=0 / AOF_FSYNC=1 / LAZY_FREE=2 / NUM=3 |
| `bio_job_type_t` 枚举 | bio.h:24-33 | 8 个 opcode(CLOSE_FILE/AOF_FSYNC/LAZY_FREE/CLOSE_AOF/COMP_RQ_*×3),NUM_OPS=8 |
| `bio_worker_title` | bio.c:51-56 | "bio_close_file" / "bio_aof" / "bio_lazy_free" |
| `bio_job_to_worker` 映射 | bio.c:59-66 | 8 opcode → 3 worker(BIO_CLOSE_AOF→worker 1) |
| `bio_mutex`/`bio_newjob_cond`/`bio_jobs` | bio.c:69-73 | 按 worker 分的三套同步原语 |
| `bio_mutex_comp`/`bio_comp_list`/`job_comp_pipe` | bio.c:78-80 | 完成回执的独立锁 + 列表 + pipe |
| `bio_job` union | bio.c:90-117 | header/fd_args/free_args(柔性数组)/comp_rq 四种 payload |
| `bioInit` | bio.c:127 | 初始化三套原语 + 起 3 个 worker 线程 |
| `bioSubmitJob` 五步 | bio.c:181-189 | 打tag→查表→持锁进队→signal→放锁 |
| `bioProcessBackgroundJobs` | bio.c:257-367 | 持锁取→放锁干→回锁摘链表→signal |
| `bioPipeReadJobCompList` swap list | bio.c:415-444 | 主线程读 pipe 回执,swap list 持锁最短 |
| `LAZYFREE_THRESHOLD` | lazyfree.c:181 | 64(同步异步分界) |
| `lazyfreeGetFreeEffort` 按编码分流 | lazyfree.c:129-174 | quicklist=len / HT=dictSize / skiplist=length / stream=估算 / 其它=1 |
| `freeObjAsync` | lazyfree.c:184-196 | effort > 64 && refcount == 1 才异步 |
| `emptyDbAsync` 换壳 | lazyfree.c:201-215 | 旧三指针 + 新空表 + bioCreateLazyFreeJob |
| `lazyfreeFreeDatabase` | lazyfree.c:23-41 | 后台 kvstoreRelease + jemalloc purge |
| `aof_bio_fsync_status`/`errno` | server.h:1959-1960 | 原子变量回传 fsync 成败 |
| `fsynced_reploff_pending` | server.h:2026 | 原子变量回传 fsync 进度 |
| `redis_fsync = fdatasync`(Linux) | config.h:127 | Linux 用 fdatasync 非 fsync |
| 五个 lazyfree-* 配置 | redis.conf:1283-1300 | eviction/expire/server-del/user-del/user-flush |

### 3. INFO 与 OBJECT ENCODING 观察项(需本地 redis-server)

> 以下操作需在 Linux 本地启动 redis-server 后用 redis-cli 执行。本书未实跑,仅列观察方法与预期(阈值来自源码常量,可 `CONFIG GET` 确认)。

```text
# 观察 lazyfree 计数器(对应 lazyfree_objects 原子变量,lazyfree.c:8):
127.0.0.1:6379> CONFIG GET lazyfree-lazy-user-del   # 预期 no(默认)
127.0.0.1:6379> INFO stats | grep -i lazyfree
# 预期(基于 lazyfree.c:9 的 lazyfreed_objects 计数器):
#   lazyfree_pending_objects:0    (当前待释放对象数,对应 lazyfree_objects)
#   lazyfreed_objects:0           (累计已释放对象数,对应 lazyfreed_objects)

# 制造一个异步释放:UNLINK 一个大 hash(> 64 元素触发异步):
127.0.0.1:6379> HDEBUG POPULATE myhash 100000     # 10万元素 hash(8.0 新命令)
127.0.0.1:6379> UNLINK myhash
# 紧接着查 INFO(可能还没释放完):
127.0.0.1:6379> INFO stats | grep lazyfree_pending
# 预期:短暂的 > 0,然后回到 0(后台释放完后 atomicDecr)
# 预期:lazyfreed_objects 累计 +100000(atomicIncr 一次性累加)

# 对比 DEL(同步删除):
127.0.0.1:6379> HDEBUG POPULATE synchash 100000
127.0.0.1:6379> DEL synchash
# 预期:lazyfree_pending_objects 不变(Del 走同步路径,不进队)

# 观察 FLUSHALL ASYNC 的换壳瞬间:
127.0.0.1:6379> HDEBUG POPULATE big1 100000
127.0.0.1:6379> FLUSHALL ASYNC
# 预期:命令立刻返回 OK(O(1) 换壳),后台慢慢释放
# 紧接着 INFO:
127.0.0.1:6379> INFO stats | grep lazyfree_pending
# 预期:短暂的 > 0(kvstoreSize 一次性 atomicIncr),几秒后回到 0

# 观察五个 lazyfree 配置开关(默认全 no,生产建议全 yes):
127.0.0.1:6379> CONFIG GET lazyfree-lazy-eviction
127.0.0.1:6379> CONFIG GET lazyfree-lazy-expire
127.0.0.1:6379> CONFIG GET lazyfree-lazy-server-del
127.0.0.1:6379> CONFIG GET lazyfree-lazy-user-del
127.0.0.1:6379> CONFIG GET lazyfree-lazy-user-flush
```

标注:以上预期基于源码常量与 [lazyfree.c](../../redis-8.0.2/src/lazyfree.c) / [bio.c](../../redis-8.0.2/src/bio.c) 的逻辑推导,本书未在本地实跑;若你的 redis 版本不同(`HDEBUG POPULATE` 是 8.0 新命令,7.x 没有),可用 `DEBUG POPULATE` 或脚本循环 `HSET` 替代。`lazyfree_pending_objects` / `lazyfreed_objects` 字段名以你实际版本 `INFO stats` 输出为准。
