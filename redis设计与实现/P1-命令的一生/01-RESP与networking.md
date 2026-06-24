# 第一章 · 字节流进门:RESP 协议与 networking.c

> 篇:P1 命令的一生
> 主轴呼应:这一章是**取向①(单线程 + 事件循环)在网络 IO 层的具体落地**——读用事件回调,数据来了才读、绝不阻塞;解析够快,留在主线程一刀切完;回复先攒缓冲、再批量写,把 `write` 的系统调用次数压到最低。8.0 之后,读和解析这两件最枯燥的字节搬运下放给了 IO 线程,但有一条铁律贯穿全书——**命令执行(`processCommand`/`call`)死死钉在主线程**,因为它要读写共享的 db 字典和各种对象。

---

## 读完本章你会明白

1. **为什么 Redis 的网络协议 RESP 是纯文本,而不是看起来更"先进"的二进制协议**——答案不是性能,而是**可调试、可手敲、解析够用就行**,这是取向④(简单优先)在协议层的诚实选择。
2. **一条 `SET key value` 的字节流是怎么进门、怎么变成 `argc/argv` 数组的**——从 `readQueryFromClient` 注册、到 multibulk 逐段啃字节、到 `argv[]` 凑齐,这整段路一行源码都不跳过。
3. **8.0 为什么给每个 IO 线程配了一个"可复用 query buffer"(`thread_reusable_qb`)**——为了在百万级短连接场景下,把每个 client 各自 `malloc`/`free` 一份 16KB 输入缓冲的 allocator 压力,压成"每线程一份、借了就还"。
4. **一个 1MB 的大 value 是怎么做到进门"零拷贝"的**——靠的是"让 bulk 恰好独占 querybuf 尾部、直接把 querybuf 过户成对象"这一招,省下一次 `memcpy`。
5. **回复为什么是"先填 16KB 静态缓冲、装不下才上链表"的双层结构**——绝大多数回复就几字节,一次 `memcpy` 进静态缓冲搞定;只有 `LRANGE` 这种大回复才动用链表。这是"快速路径 + 慢速路径"的经典分层。
6. **8.0 的 client 内存桶式驱逐凭什么不会误杀**——靠 19 个"按 2 的幂递增"的内存桶(32KB → 4GB+),超限时从最大桶往下扫、专挑最胖的 client 下手,而且驱逐前重新估一次内存防止桶滞后误判。

---

> **如果一读觉得太难:先只记住三件事**——
> ① RESP 是**文本协议**,一条命令长这样:`*3\r\n$3\r\nSET\r\n$3\r\nkey\r\n$5\r\nvalue\r\n`,每段前面标长度,`\r\n` 分隔,拿 `telnet` 都能手敲;
> ② 进门路径:**epoll 通知可读 → `readQueryFromClient` 读 16KB 进 `querybuf` → `processMultibulkBuffer` 啃成 `argv[]` → `processCommand` 执行**(详见第三章);
> ③ 出门路径:**命令执行时调 `addReply` 把回复塞进 16KB 静态缓冲 `c->buf` → 装不下溢出到链表 `c->reply` → 事件循环 `beforeSleep` 时批量 `writeToClient` flush 出去**。
> 这三件事,就是 networking 的全部。

---

> **一句话点破:networking.c 把"主线程的时间很贵"这件事刻进了每一行代码——能不阻塞就不阻塞(事件回调)、能不拷贝就不拷贝(大 bulk 过户)、能不 malloc 就不 malloc(复用 querybuf / 预分配 16KB reply buffer)、能不 write 就不 write(攒缓冲批量 flush)。四件省事的活攒在一起,就是单线程撑高并发的物理前提。**

序章结尾我们留了一个问题:一条 `SET key value`,从客户端敲下去,到 Redis 存好、回一句 `OK`,中间发生了什么?这一章,我们从它进门的那个瞬间讲起——字节流是怎么进来的,又是怎么出去的。

## 1.1 这块要解决什么:一个数据库服务器最原始的两件事

一个数据库服务器,最原始的职责只有两条:

1. 在一条 TCP 连接上,**读懂**客户端发来的字节流;
2. 把处理结果**写回去**。

听起来简单,落到工程里全是问题。协议用什么格式——文本还是二进制?读的时候怎么做到不卡住别的客户端(有人发了个 1MB 的大 value,总不能让全服务器陪它等)?读进来的字节放哪儿,缓冲区设多大、谁来分配、谁来回收?回复是边算边写,还是攒一批再写?写不出去怎么办(socket 缓冲满了)?怎么防止一个恶意客户端把内存撑爆?

> **不这样会怎样**:最笨的做法是**为每个客户端起一个线程,阻塞 `read`**——读到啥算啥,处理完 `write` 回去。这条路 Java 传统网络编程(thread-per-connection)走过,代价是:1 万连接 = 1 万线程 = GB 级栈内存 + 海量上下文切换 + 共享数据结构(dict、对象)的全套锁。结果是几千并发就到顶,而且锁竞争本身吃掉一大半 CPU。这是取向①(单线程 + 事件循环)要彻底避开的反面教材。

Redis 给出的答案非常有它的性格:**协议极简(RESP,纯文本)、读用事件驱动(谁有数据才读谁、绝不阻塞)、回复先攒缓冲、再批量写(把系统调用次数压到最低)、缓冲按需复用(每线程一份 querybuf、每 client 一份 16KB 静态 reply buffer)**。这一章我们只盯住"进门"和"出门"这两段路——`networking.c` 里的故事。命令真正怎么执行,留到第三章 `processCommand`。

## 1.2 RESP 协议:文本到可以用 telnet 手敲

先把协议本身讲清楚。一条 `SET key value` 在 RESP 里长这样(每个 `$` 后面跟这段内容的字节长度,`\r\n` 是分隔):

```text
*3\r\n$3\r\nSET\r\n$3\r\nkey\r\n$5\r\nvalue\r\n
```

逐段拆:

```text
*3        ← 一共 3 个参数(multibulk count)
\r\n
$3        ← 第 1 个参数,长度 3 字节
\r\n
SET       ← 参数内容
\r\n
$3        ← 第 2 个参数,长度 3 字节
\r\n
key
\r\n
$5        ← 第 3 个参数,长度 5 字节
\r\n
value
\r\n
```

把字节布局画成图,就是一张 RESP 报文骨架:

```text
字节布局(SET key value 的 RESP multibulk 编码):

偏移  字节内容                  含义
─────────────────────────────────────────────────
 0    '*'                       multibulk 起始符
 1    '3'                       参数个数(ASCII 数字)
 2-3  \r\n                      分隔
 4    '$'                       bulk 起始符
 5    '3'                       本段长度(ASCII 数字)
 6-7  \r\n                      分隔
 8-10 'S','E','T'              第 1 段内容(3 字节)
11-12 \r\n                      分隔
13    '$'                       下一段开始
14    '3'
15-16 \r\n
17-19 'k','e','y'
20-21 \r\n
22    '$'
23    '5'
24-25 \r\n
26-30 'v','a','l','u','e'
31-32 \r\n
─────────────────────────────────────────────────
总长 33 字节,纯 ASCII,可打印,可 telnet 手敲
```

这种"先报长度再报内容"的格式叫 **length-prefixed**(长度前缀)。它的好处有两个:① **解析端不必扫描整个内容找分隔符**(只在 `$` 和 `\r\n` 之间扫一次数字),知道长度后直接 `memcpy` 或指针搬运;② **内容里可以随便出现 `\r\n` 或任何字节**(包括二进制),因为长度已经告诉了解析端"这段就这么长,别管里面是啥"。所以 RESP 虽然是"文本协议",却能装二进制 value——这是它比"一行一个命令、用换行分隔"的老式 inline 协议强的地方。

> **钉死这件事**:RESP 选文本不是因为性能(二进制解析更快、更省带宽),而是因为**可调试、可手敲、解析足够便宜**。你拿 `telnet 127.0.0.1 6379` 或 `nc` 连上 redis-server,手敲 `*3\r\n$3\r\nSET\r\n$3\r\nkey\r\n$5\r\nvalue\r\n` 就能跑通——这份"对人友好"是 Redis 早期社区传播的隐形资产。在 Redis 整体瓶颈(网络 IO、内存访问)面前,RESP 解析的常数开销根本排不上号,选简单的就对了。

inline 协议(不以 `*` 打头)是老式的"一行一个命令"格式,今天只用于 `telnet` 手敲的退化场景,生产客户端默认都走 multibulk。processInputBuffer 根据 `querybuf` 第一个字节是不是 `*` 来分派([networking.c:2800-2806](../../redis-8.0.2/src/networking.c#L2800)),我们这一章主角是 multibulk。

## 1.3 连接进门:createClient 与那行 connSetReadHandler

一个新客户端 `accept` 出来一条连接,Redis 立刻调 `createClient`([networking.c:120](../../redis-8.0.2/src/networking.c#L120))。这个函数的核心动作就两件——**给客户端把字段初始化好,再把"这条连接可读时调谁"注册出去**:

```c
/* networking.c:127-134 */
if (conn) {
    connEnableTcpNoDelay(conn);                          /* 关掉 Nagle,小回复立刻发 */
    if (server.tcpkeepalive)
        connKeepAlive(conn, server.tcpkeepalive);
    connSetReadHandler(conn, readQueryFromClient);       /* 注册:可读时调 readQueryFromClient */
    connSetPrivateData(conn, c);
}
c->buf = zmalloc_usable(PROTO_REPLY_CHUNK_BYTES, &c->buf_usable_size);  /* 预分配 16KB reply buf */
```

`connSetReadHandler(conn, readQueryFromClient)`([networking.c:131](../../redis-8.0.2/src/networking.c#L131))是整个 networking 的**起手式**——它的意思是:"这条连接上只要有数据可读,就叫我,我给的回调是 `readQueryFromClient`。" 这就是事件驱动(reactor)的核心动作:**注册**,而不是**阻塞等待**。Redis 不会傻乎乎地停在这里等数据,它把"等待"这件事交给了操作系统的 `epoll`(下一章讲 `ae` 时细说),自己该干嘛干嘛。

紧接着 `zmalloc_usable(PROTO_REPLY_CHUNK_BYTES, ...)`([networking.c:134](../../redis-8.0.2/src/networking.c#L134))给每个客户端**预分配 16KB 的回复缓冲**(`PROTO_REPLY_CHUNK_BYTES = 16*1024`,[server.h:164](../../redis-8.0.2/src/server.h#L164))。`zmalloc_usable` 比 `zmalloc` 多回一个"实际能用的字节数"——因为 jemalloc 这类分配器会按 size class 对齐,你要 16KB 它可能给你 16384、也可能给 16392,Redis 把这个真实大小记在 `c->buf_usable_size` 里,后面塞回复时一点不浪费。

> **钉死这件事**:`createClient` 在新连接一进门就做两件事——**把读回调注册到 epoll**(这是"事件驱动"的核心动作,数据来了才被动响应),和**预分配 16KB reply buffer**(这是把"回复时再 malloc"这件事提前一次性做完)。两个动作的本质都是**把耗时从主线程的热路径里挪走**:等待挪给了内核,malloc 挪到了建连那一刻。一个高并发 Redis 实例每秒建几万条连接,这两处"挪走"省下的主线程时间,直接体现在吞吐曲线上。

注意一个 8.0 才有的字段初始化:

```c
/* networking.c:139-140 */
c->tid = IOTHREAD_MAIN_THREAD_ID;          /* 这条 client 归属哪个线程 */
c->running_tid = IOTHREAD_MAIN_THREAD_ID;  /* 这条 client 当前正在哪个线程里跑 */
```

`tid` 是 client 的"户籍",`running_tid` 是它"此刻在哪"。两者默认都是主线程;8.0 IO 多线程打开后,client 在被 IO 线程读取/解析的瞬间 `running_tid` 会改成那个 IO 线程的 id,处理完再改回来。这俩字段是 8.0 的伏笔,我们会在 1.8 节和第二十章把它彻底讲透。本章后面凡是涉及 `c->running_tid != IOTHREAD_MAIN_THREAD_ID` 的判断,都先理解为"这条 client 现在在 IO 线程手里"。

## 1.4 读:`readQueryFromClient` 与那 16KB

当 `epoll` 通知"这条连接有数据可读了",事件循环就会回调 `readQueryFromClient`([networking.c:2884](../../redis-8.0.2/src/networking.c#L2884))。它干的事看起来朴素——读、解析——但每一步都藏着小心机。先把主干拉出来:

```c
/* networking.c:2884-2894,精简 */
void readQueryFromClient(connection *conn) {
    client *c = connGetPrivateData(conn);
    int nread, big_arg = 0;
    size_t qblen, readlen;
    if (!(c->io_flags & CLIENT_IO_READ_ENABLED)) return;   /* IO 线程没开启就什么都不做 */
    ...
    readlen = PROTO_IOBUF_LEN;                              /* 默认读 16KB */
    ...
}
```

`readlen = PROTO_IOBUF_LEN` 这一行把"一次 read 读多少"钉死在 16KB(`PROTO_IOBUF_LEN = 1024*16`,[server.h:163](../../redis-8.0.2/src/server.h#L163))。为什么是 16KB?这是一个工程甜点——太小(比如 1KB)会让一次大命令需要很多次 `read` syscall;太大(比如 1MB)会让每个 client 的 querybuf 起步就吃 1MB,几万连接就是几十 GB。16KB 大到能装下绝大多数完整命令一次读完,又小到不会让 querybuf 显著膨胀。

但这里有一个分支会改变 `readlen`,**而且这个分支是 1.5 节零拷贝的伏笔**:当 client 正在发一个**单参数 ≥ 32KB**的大 bulk(`PROTO_MBULK_BIG_ARG = 1024*32`,[server.h:166](../../redis-8.0.2/src/server.h#L166))时,`readlen` 会被调成"刚好够读完这个参数":

```c
/* networking.c:2901-2914 */
if (c->reqtype == PROTO_REQ_MULTIBULK && c->multibulklen && c->bulklen != -1
    && c->bulklen >= PROTO_MBULK_BIG_ARG)
{
    /* For big argv, the client always uses its private query buffer. */
    if (!c->querybuf) c->querybuf = sdsempty();
    ssize_t remaining = (size_t)(c->bulklen+2)-(sdslen(c->querybuf)-c->qb_pos);
    big_arg = 1;
    if (remaining > 0) readlen = remaining;
    ...
}
```

注意 `if (!c->querybuf) c->querybuf = sdsempty();`——大参数路径**显式不用复用 querybuf**,而是给 client 自己开一份私有 buffer。为什么?注释 @2904-2906 写得直白:"Using the reusable query buffer would eventually expand it beyond 32k, causing the client to take ownership of the reusable query buffer."(如果用复用 buffer,会被撑到 32KB 以上,结果 client 还是得把它"过户"成自己的——那复用就没意义了)。所以大参数直接走私有,把复用 buffer 留给"短命令"路径。这是 1.6 节复用机制能成立的前提。

紧接着是 querybuf 的"按需扩容",这里又有一处 Redis 的精打细算:

```c
/* networking.c:2942-2959 */
qblen = sdslen(c->querybuf);
if (!(c->flags & CLIENT_MASTER) &&    /* master client's querybuf can grow greedy. */
    (big_arg || sdsalloc(c->querybuf) < PROTO_IOBUF_LEN)) {
    /* 非贪婪扩展:只扩到刚好够 */
    c->querybuf = sdsMakeRoomForNonGreedy(c->querybuf, readlen);
    if (c->querybuf_peak < qblen + readlen) c->querybuf_peak = qblen + readlen;
} else {
    c->querybuf = sdsMakeRoomFor(c->querybuf, readlen);    /* 贪婪扩展:翻倍 */
    readlen = sdsavail(c->querybuf);                       /* 顺手把 readlen 撑到能用满 */
}
nread = connRead(c->conn, c->querybuf+qblen, readlen);
```

这里有两条路径:**普通 client + 大参数 / 初始小 buffer,走"非贪婪"(`sdsMakeRoomForNonGreedy`,只要够);其他情况走"贪婪"(`sdsMakeRoomFor`,翻倍)**。这两条路为什么必须并存,我们在 1.7 节讲完 querybuf 的生命周期后讲透。

读完之后,直接交给解析:

```c
/* networking.c:3003 */
if (processInputBuffer(c) == C_ERR)
     c = NULL;
```

> **钉死这件事**:`readQueryFromClient` 的本质是"被动响应 + 按需扩容 + 立刻解析"三连——epoll 叫醒我才读,绝不空等;readlen 钉死 16KB 平衡 syscall 次数和内存;大参数路径单独把 readlen 调到"刚好够",为 1.5 节的零拷贝铺路;querybuf 扩容分贪婪/非贪婪两条路,各管一段场景。每一行都在贯彻"主线程时间贵、内存也贵,两头都得省"的取向。

## 1.5 技巧精解①:大 bulk 的零拷贝过户——把 querybuf 直接变成对象

这是 networking.c 里最值得讲透的一处优化。看 `processMultibulkBuffer`([networking.c:2440](../../redis-8.0.2/src/networking.c#L2440))在啃完一个 bulk 时的代码:

```c
/* networking.c:2569-2590 */
/* Optimization: if a non-master client's buffer contains JUST our bulk element
 * instead of creating a new object by *copying* the sds we
 * just use the current sds string. */
if (!(c->flags & CLIENT_MASTER) &&
    c->qb_pos == 0 &&
    c->bulklen >= PROTO_MBULK_BIG_ARG &&
    querybuf_len == (size_t)(c->bulklen+2))
{
    c->argv[c->argc++] = createObject(OBJ_STRING,c->querybuf);  /* 直接把 querybuf 过户 */
    c->argv_len_sum += c->bulklen;
    sdsIncrLen(c->querybuf,-2);                                 /* 去掉尾部的 \r\n */
    /* Assume that if we saw a fat argument we'll see another one likely... */
    c->querybuf = sdsnewlen(SDS_NOINIT,c->bulklen+2);           /* 重新开一份空 buffer */
    sdsclear(c->querybuf);
    querybuf_len = sdslen(c->querybuf);
} else {
    c->argv[c->argc++] =
        createStringObject(c->querybuf+c->qb_pos,c->bulklen);   /* fallback:拷贝 */
    c->argv_len_sum += c->bulklen;
    c->qb_pos += c->bulklen+2;
}
```

这段代码的意思是:**当一个 bulk 满足三个条件——非 master client、bulk 在 querybuf 头部(`qb_pos==0`)、bulk 长度 ≥ 32KB 且恰好独占整个 querybuf(`querybuf_len == bulklen+2`)——就不拷贝,直接把 querybuf 这块 sds "过户"成 argv 里的对象**。具体三步:

1. `createObject(OBJ_STRING, c->querybuf)` 把整块 querybuf sds 包成一个 `robj`,塞进 `argv[]`。**没有 memcpy,没有新分配**——robj 里的 ptr 字段直接指向原来的 querybuf 内存。
2. `sdsIncrLen(c->querybuf, -2)` 把末尾的 `\r\n` 长度减掉(让对象看到的内容是干净的 value,不带分隔符)。
3. `c->querybuf = sdsnewlen(SDS_NOINIT, c->bulklen+2)` 重新开一份新的空 querybuf,留待下次 read 用。注意 `SDS_NOINIT`——这块新 buffer **不 memset 清零**,因为接下来 `sdsclear` 会把 sds 的长度字段归零、内容区反正会被后续 read 覆盖,清零是纯浪费。

这一个 1MB 的 value,就省下了 1MB 的 `memcpy` 和一次 malloc/free 配对。对单线程的 Redis,这种省法积少成多,直接体现在吞吐上。

那要满足那三个条件怎么办?这就是 1.4 节"`readlen` 调到刚好够"和 1.6 节"大参数走私有 buffer"联手的目的——它俩合力,让"bulk 独占 querybuf 尾部"这件事尽可能成立。具体:① 读大参数前,先 `sdsrange` 把 querybuf 修剪到"未解析数据从偏移 0 开始"(`processMultibulkBuffer` 里 [networking.c:2542-2545](../../redis-8.0.2/src/networking.c#L2542));② 然后 `sdsMakeRoomForNonGreedy` 只扩到"刚好能装下这个 bulk";③ 读进来后整个 querybuf 就是这一段 bulk——条件 `querybuf_len == bulklen+2` 成立,过户。

> **钉死这件事**:大 bulk 零拷贝的本质,是"让 querybuf 在那一刻恰好只装这一个 bulk,然后整块过户"。三个条件缺一不可——非 master(master 的 querybuf 要给副本转发用,不能过户)、`qb_pos==0`(从头开始)、bulk 独占(`querybuf_len==bulklen+2`)。Redis 为此专门做了两件事:大参数读时把 readlen 调到刚好够、大参数不走复用 buffer。整套设计只为了一个目标——**省掉那一次 memcpy**。在单线程模型下,主线程省下的每一纳秒都是 throughput。

## 1.6 技巧精解②:thread_reusable_qb——每线程一份、借了就还的复用 querybuf

这是 8.0 引入的一个明显缓解 allocator 压力的优化。先看那两行不起眼的声明:

```c
/* networking.c:35-37 */
__thread sds thread_reusable_qb = NULL;
__thread int thread_reusable_qb_used = 0; /* Avoid multiple clients using reusable query
                                             buffer at the same time. */
```

`__thread` 是 GCC/Clang 的线程局部存储(TLS)修饰符——意思是**每个 IO 线程各有一份自己的 `thread_reusable_qb` 和 `thread_reusable_qb_used`**,线程之间互不相干。`thread_reusable_qb` 是一份最多 16KB 的 sds,`thread_reusable_qb_used` 是"这份 buffer 当前有没有被某个 client 借走"的标志位。

它怎么用?看 `readQueryFromClient` 里 querybuf 为 NULL 时的分支:

```c
/* networking.c:2920-2940 */
} else if (c->querybuf == NULL) {
    if (unlikely(thread_reusable_qb_used)) {
        /* 复用 buffer 已被别的 client 借走,这个 client 只能自己开一份 */
        c->querybuf = sdsnewlen(NULL, PROTO_IOBUF_LEN);
        sdsclear(c->querybuf);
    } else {
        /* 还没建就建一份,建过就直接借 */
        if (!thread_reusable_qb) {
            thread_reusable_qb = sdsnewlen(NULL, PROTO_IOBUF_LEN);
            sdsclear(thread_reusable_qb);
        }
        serverAssert(sdslen(thread_reusable_qb) == 0);     /* 借出来时必须是空的 */
        c->querybuf = thread_reusable_qb;                  /* 借出 */
        c->io_flags |= CLIENT_IO_REUSABLE_QUERYBUFFER;     /* 记号:我的 querybuf 是借来的 */
        thread_reusable_qb_used = 1;                       /* 标记已借出 */
    }
}
```

逻辑清晰:**querybuf 为 NULL 时,优先借复用 buffer;只有复用 buffer 已被别的 client 借走(同一时刻只能借给一个),才退回到"自己开一份"**。被借走的复用 buffer,什么时候还?在 client 的内存更新和回收路径里([networking.c:1662-1676](../../redis-8.0.2/src/networking.c#L1662)):

```c
/* networking.c:1662-1676 */
if (c->querybuf != thread_reusable_qb || sdslen(c->querybuf) > c->qb_pos) {
    /* 不归还:要么不是借的,要么还有未消费数据 */
    ...
} else {
    /* 归还:清空但不释放,留给下个 client 借 */
    sdsclear(thread_reusable_qb);
    ...
    thread_reusable_qb_used = 0;
}
```

归还时只 `sdsclear`(把长度字段归零、内容区保留),**不 free**——这就是"复用"的本质:16KB 这块内存,在 IO 线程的整个生命周期里只 malloc 一次,后续无数个短连接 client 进进出出,都在这一块内存上"借了用、用了还"。

> **钉死这件事**:`thread_reusable_qb` 的本质是"每线程一份的输入缓冲内存池"。它针对的是**高并发短连接**场景(比如用完就断的 HTTP 短连接代理、监控探针、lambda 函数)——这种场景下,如果每个 client 各自 `sdsnewlen` 一份 querybuf 再 `sdsfree`,allocator(jemalloc)要不停地切线程本地 cache 和中心堆,锁竞争会随并发线性上升。复用一份,把每 client 的 malloc/free 配对压成"每线程每 N 个 client 一次",allocator 压力骤减。代价是:同一时刻每个 IO 线程只能有一个 client 借走复用 buffer,其余退回到私有——这是个微小的损耗,换来的是更大的吞吐稳定性。

注意一个限制:1.5 节的大参数路径**显式跳过复用**(`if (!c->querybuf) c->querybuf = sdsempty();` @2907,而不是走 2920 的复用分支)。理由前面讲过——大参数会把 buffer 撑到 32KB 以上,撑大后就没法"还回去"复用了。所以**复用 buffer 只服务短命令路径,大参数永远走私有**。这是一个清晰的职责切分。

## 1.7 技巧精解③:贪婪 vs 非贪婪扩容——querybuf 的两个生长曲线

回到 1.4 节那个"贪婪 vs 非贪婪"的分支。`sdsMakeRoomFor` 是 SDS 的标准扩容函数——**翻倍**(具体策略:小于 1MB 时翻倍,大于 1MB 时每次 +1MB)。`sdsMakeRoomForNonGreedy` 是 SDS 在 networking.c 这里专门用的变体——**只扩到刚好够**(不多给)。为什么 querybuf 要同时有这两条扩容路径?

querybuf 是一个**临时缓冲**——它装的是"刚从 socket 读进来、还没解析完"的字节。一旦 `processMultibulkBuffer` 啃完一条命令,querybuf 里的内容就会被消费掉、`sdsrange` 修剪掉(@2869-2872)。换句话说,querybuf 的"长期占用大小"约等于"一次未解析命令的最大长度",而不是"历史累积流量"。

考虑两种极端:

- **如果是贪婪扩容(翻倍)**:一个 client 偶尔发了一条 1MB 的大 value 命令,querybuf 被翻倍扩到 2MB。命令执行完,sdsrange 修剪只动 length 字段、不释放底层内存(@2869 `sdsrange(c->querybuf,c->qb_pos,-1)` 只逻辑截断)。这 2MB 的物理内存就一直占着,直到 client 断连。这是"一次性大命令后,querybuf 永久占大头"的内存放大。
- **如果是非贪婪扩容(刚好够)**:同样一条 1MB 命令,querybuf 只扩到 1MB + 一点点(刚好够装下 bulk),命令执行完修剪后,长期占用就约等于这次命令的大小。下一次如果发的是 100 字节的小命令,querybuf 也不会缩(底层内存还在),但至少不会因为"曾经大过一次"就永久占 2MB。

所以 Redis 的选择是:**"小命令 + 初始 buffer + 大参数"路径走非贪婪(防放大);只有 master client 走贪婪**(因为 master 的 querybuf 不仅用来解析,还要保留原始字节流转发给 sub-replica,会持续增长,翻倍反而省扩容次数)。看 1.4 节那个判断:

```c
/* networking.c:2943-2944 */
if (!(c->flags & CLIENT_MASTER) &&            /* master 除外 */
    (big_arg || sdsalloc(c->querybuf) < PROTO_IOBUF_LEN)) {  /* 大参数或初始小 buffer */
    c->querybuf = sdsMakeRoomForNonGreedy(c->querybuf, readlen);   /* 非贪婪 */
    ...
} else {
    c->querybuf = sdsMakeRoomFor(c->querybuf, readlen);            /* 贪婪:翻倍 */
    readlen = sdsavail(c->querybuf);                               /* 用满 */
}
```

非贪婪的触发条件有两块:**① `big_arg`(当前正在读大参数)——这是 1.5 节零拷贝的配套,要把 buffer 撑到刚好装下 bulk;② `sdsalloc(querybuf) < PROTO_IOBUF_LEN`(当前 querybuf 物理分配还小于 16KB)——这是初始阶段的小 buffer,走非贪婪避免与 SDS 的 RESIZE_THRESHOLD 机制冲突(注释 @2948 "in order to avoid collision with the RESIZE_THRESHOLD mechanism")。其余情况(普通 client 已经 ≥ 16KB 的 querybuf,且不在读大参数)走贪婪翻倍——因为这时 buffer 已经在正常工作区间,翻倍能减少后续 read 的扩容次数。

> **钉死这件事**:querybuf 的扩容有两条路,是 Redis 对"临时缓冲的内存放大风险"的精细对策。**非贪婪(刚好够)** 用于"一次性大命令"路径,防它把 buffer 永久撑大;**贪婪(翻倍)** 用于 master client 和已稳定的普通 buffer,追求 read syscall 次数最少。这是取向②(内存即数据库,内寸是稀缺资源)和取向①(主线程时间贵,syscall 要少)在 querybuf 这一处的具体平衡。

## 1.8 技巧精解④:processInputBuffer——一循环啃完所有完整命令

读完之后立刻解析。`processInputBuffer`([networking.c:2776](../../redis-8.0.2/src/networking.c#L2776))是一个 while 循环,**只要 querybuf 里还有未消费字节、且没碰到阻塞条件,就一直解析下去**:

```c
/* networking.c:2778-2806 */
while(c->qb_pos < sdslen(c->querybuf)) {
    if (c->flags & CLIENT_BLOCKED) break;                 /* 阻塞中,先停 */
    if (c->flags & CLIENT_PENDING_COMMAND) break;         /* 已有待执行命令,先停 */
    if (c->flags & CLIENT_MASTER && isInsideYieldingLongCommand()) break;
    if (c->flags & (CLIENT_CLOSE_AFTER_REPLY|CLIENT_CLOSE_ASAP)) break;

    /* 判断协议类型(只在首次看) */
    if (!c->reqtype) {
        if (c->querybuf[c->qb_pos] == '*') {
            c->reqtype = PROTO_REQ_MULTIBULK;
        } else {
            c->reqtype = PROTO_REQ_INLINE;
        }
    }

    if (c->reqtype == PROTO_REQ_INLINE) {
        if (processInlineBuffer(c) != C_OK) { ... break; }
    } else if (c->reqtype == PROTO_REQ_MULTIBULK) {
        if (processMultibulkBuffer(c) != C_OK) { ... break; }
    }
    ...
}
```

注意 `c->qb_pos < sdslen(c->querybuf)` 这个循环条件——`qb_pos` 是"已解析到 querybuf 的哪个字节",`sdslen` 是"querybuf 当前装了多少字节"。两者之差就是"还有多少没解析"。Redis 客户端可以**pipelining**(流水线):一次 `write` 把好几条命令的字节全发过来,比如 `SET k1 v1\r\nSET k2 v2\r\nSET k3 v3\r\n`。这种情况下,一次 read 进来的 querybuf 里就有 3 条完整命令——`processInputBuffer` 的循环会连续解析 3 次,每次解析完一条立刻交给 `processCommand` 执行,直到 querybuf 见底或剩余不够一条完整命令才退出。

这里有 8.0 IO 线程的关键分支:

```c
/* networking.c:2830-2848 */
} else {
    /* If we are in the context of an I/O thread, we can't really
     * execute the command here. All we can do is to flag the client
     * as one that needs to process the command. */
    if (c->running_tid != IOTHREAD_MAIN_THREAD_ID) {
        c->io_flags |= CLIENT_IO_PENDING_COMMAND;        /* 打标:有待执行命令 */
        c->iolookedcmd = lookupCommand(c->argv, c->argc);/* 顺手查命令表(只读操作) */
        enqueuePendingClientsToMainThread(c, 0);          /* 入队,等主线程执行 */
        break;
    }

    /* We are finally ready to execute the command. */
    if (processCommandAndResetClient(c) == C_ERR) {
        return C_ERR;
    }
}
```

这段就是那条**贯穿全书的铁律**的具体落地:**IO 线程可以读、可以解析、可以查命令表(只读),但绝不能执行命令**——`processCommand`(在 [server.c:3985](../../redis-8.0.2/src/server.c#L3985))只在主线程跑。IO 线程解析完一条命令后,不 `call`,而是给 client 打上 `CLIENT_IO_PENDING_COMMAND` 标志、塞进"待主线程处理"队列、然后 break 跳出循环。主线程从队列里取出这些 client,统一在 `processCommand` 里执行——执行要读写共享的 db 字典和各种对象,这一步必须单线程。

> **钉死这件事**:8.0 的 IO 多线程有一条铁律——**读、解析、查命令表可以下放给 IO 线程,但 `processCommand` 永远钉在主线程**。看 `processInputBuffer` 里 @2834 的 `if (c->running_tid != IOTHREAD_MAIN_THREAD_ID)` 分支:IO 线程把 client 打上 `CLIENT_IO_PENDING_COMMAND`、入队、break;主线程稍后从队列取出,统一 `call`。这条分界线是"单线程无锁访问 db"的最后防线——多线程碰共享数据就要加锁,一加锁就背叛了 Redis 的纯粹。第二十章会讲透它怎么和事件循环协作。

循环结束后,还有一个关键动作——**querybuf 的物理修剪**:

```c
/* networking.c:2851-2873 */
if (c->flags & CLIENT_MASTER) {
    /* master 用 repl_applied 修剪(因为它的 querybuf 还要转发给 sub-replica) */
    if (c->repl_applied) {
        sdsrange(c->querybuf,c->repl_applied,-1);
        c->qb_pos -= c->repl_applied;
        c->repl_applied = 0;
    }
} else if (c->qb_pos) {
    /* 普通 client 用 qb_pos 修剪 */
    sdsrange(c->querybuf,c->qb_pos,-1);
    c->qb_pos = 0;
}
```

注意一个容易讲错的点——**这里不是"延迟压缩",而是"每完成一轮解析就立刻 sdsrange 修剪一次"**。`sdsrange` 是 SDS 的"逻辑截断"——它把 `sds` 的起始指针往前挪、长度字段减相应字节数。底层的物理内存(那块 malloc 出来的连续区域)不动,但逻辑上从 `qb_pos` 开始的字节被"消费掉了"。下一次 read 进来的字节会从新的 querybuf 尾部追加。

那 `qb_pos` 在解析过程中是干什么的?它是 **"当前解析进度游标"**——bulk 没读完(一次 read 没把整个 bulk 读进来)时,`qb_pos` 标记"已啃到哪儿",下次 read 接着啃;此时 querybuf 不修剪(因为后面的内容还没消费完,修了就读不全了)。一旦某轮解析完整消化掉了所有完整命令(while 循环退出),才用 `qb_pos` 把已消费部分一次性 `sdsrange` 修剪掉。所以"渐进消费"指的是"游标推进、不每段 memmove";真正的物理压缩发生在"一轮解析结束时一次性做"。

> **钉死这件事**:`processInputBuffer` 是一个 while 循环,把 querybuf 里所有完整命令一次啃完,支持客户端 pipelining。解析过程中用 `qb_pos` 游标推进、不每段 memmove;一轮结束时一次性 `sdsrange` 修剪已消费部分。8.0 的铁律在 @2834 那一行落地——IO 线程解析完命令不 `call`,而是入队让主线程执行。这是"读/解析可以并行,执行必须串行"的精确分界。

## 1.9 回复路径①:addReply 与双层缓冲

进门讲完,讲出门。命令执行时,Redis **不会**每算出一点就往 socket 里 `write`——那会产生海量系统调用。它的做法是"先记账":执行过程中调 `addReply` / `addReplyProto` / `addReplySds` 等函数,把回复字节塞进客户端的缓冲区,等这条连接"有空写了",再一次性 flush 出去。

塞回复的核心是 `_addReplyToBufferOrList`([networking.c:384](../../redis-8.0.2/src/networking.c#L384))。它的策略很清晰——**先填静态缓冲,溢出了才上链表**:

```c
/* networking.c:416-429 */
const size_t available = c->buf_usable_size - c->bufpos;   /* 静态 buf 还剩多少 */

size_t reply_len = 0;
/* If there already are entries in the reply list, we cannot
 * add anything more to the static buffer. */
if (listLength(c->reply) < 1) {
    reply_len = len > available ? available : len;          /* 这一段能塞多少进静态 buf */
    memcpy(c->buf+c->bufpos,s,reply_len);                  /* 一次 memcpy 搞定 */
    c->bufpos+=reply_len;
    c->buf_peak = max(c->buf_peak,(size_t)c->bufpos);
}

if (len > reply_len) _addReplyProtoToList(c,c->reply,s+reply_len,len-reply_len);  /* 装不下才挂链表 */
```

逻辑分两层:

- **快速路径(静态缓冲 `c->buf`)**:回复长度 ≤ `available`(静态缓冲剩余空间),一次 `memcpy` 进去就完事。绝大多数命令的回复就几字节到几十字节(一句 `+OK\r\n` 才 5 字节,一个 `:1234\r\n` 才 7 字节),永远停留在静态缓冲里。`memcpy` 是 CPU 流水线里的"几乎免费"操作,这一层几乎零成本。
- **慢速路径(链表 `c->reply`)**:回复真的很大(比如 `LRANGE` 返回一万条数据),静态缓冲装不下,溢出的部分挂到 `c->reply` 链表上。链表的每个节点也是一个 SDS 块,大小按 `PROTO_REPLY_CHUNK_BYTES`(16KB)配。

注意一个细节(@421 的 `if (listLength(c->reply) < 1)`):**一旦链表里已经有节点,后续回复就不再往静态缓冲塞了,全上链表**。为什么?因为 flush 出去的顺序必须是"先静态缓冲后链表"(下一节 `writeToClient` 会看到),如果静态缓冲在链表非空时继续追加,flush 顺序就乱了——静态缓冲里夹着比链表第一个节点更晚的回复。所以 Redis 的规则是:**静态缓冲和链表是严格的"前后两段"——要么全在静态缓冲(链表空),要么静态缓冲满了之后追加的部分全进链表**。

把双层 reply buffer 画出来:

```text
client c
┌──────────────────────────────────────────────────────────────────────────┐
│                                                                          │
│  c->buf (静态缓冲,预分配 16KB,zmalloc_usable 可能略大)                  │
│  ┌────────────────────────────────────────────────────┐                  │
│  │ +OK\r\n___________ 已写 ___________...未写区域...   │                  │
│  └────────────────────────────────────────────────────┘                  │
│  ↑                                                   ↑                   │
│  bufpos=5                          buf_usable_size (~16KB)               │
│                                                                          │
│  c->reply (SDS 链表,仅静态缓冲装不下时启用)                              │
│  ┌──────────┐    ┌──────────┐    ┌──────────┐                           │
│  │ node 0   │ →  │ node 1   │ →  │ node 2   │ → NULL                     │
│  │ SDS ≤16K │    │ SDS ≤16K │    │ SDS ≤16K │                           │
│  └──────────┘    └──────────┘    └──────────┘                           │
│                                                                          │
│  flush 顺序:  先写 c->buf[0..bufpos),再依次写 c->reply 的每个节点        │
│  写策略:      _addReplyToBufferOrList 先 memcpy 进 c->buf,溢出才挂链表   │
│              一旦链表非空,新回复全上链表(保 flush 顺序)                │
└──────────────────────────────────────────────────────────────────────────┘
```

> **钉死这件事**:双层 reply buffer 是"快速路径 + 慢速路径"分层的经典例子——**绝大多数回复走静态缓冲(一次 memcpy,几乎免费),只有大回复溢出才走链表**。关键规则是"链表非空时新回复全上链表",保 flush 顺序严格"先静态后链表"。这套设计让 `LRANGE` 一万条这种大回复也能稳稳接住,同时不让普通命令为链表的 malloc 开销买单——每条普通命令的回复就一次 memcpy 进静态缓冲,零 malloc。

那"什么时候真的 `write` 到 socket"?——不是在命令执行时,而是在事件循环每一轮的 `beforeSleep` 里([server.c:1717](../../redis-8.0.2/src/server.c#L1717),详见 Ch2 §2.5):主线程把所有"有回复待发"的 client 收集起来,批量地、一个接一个地把缓冲 flush 出去(`writeToClient`,[networking.c:2097](../../redis-8.0.2/src/networking.c#L2097))。这是把"写"也做成了批量操作——一万条命令的回复,可能在一次 beforeSleep 里一口气 flush 出去,`write` syscall 的次数被压到最低。

## 1.10 回复路径②:writeToClient 与 flush 顺序

`writeToClient`([networking.c:2097](../../redis-8.0.2/src/networking.c#L2097))是把缓冲真正写出去的地方。它的结构(精简):

```c
/* networking.c:2115-2142 */
const int is_normal_client = !(c->flags & CLIENT_SLAVE);
while (_clientHasPendingRepliesNonSlave(c)) {
    int ret = _writeToClientNonSlave(c, &nwritten);   /* 实际写一次 */
    if (ret == C_ERR) break;
    totwritten += nwritten;
    /* 避免单个 client 霸占主线程:写超过 NET_MAX_WRITES_PER_EVENT 字节就让一让 */
    if (totwritten > NET_MAX_WRITES_PER_EVENT &&
        (server.maxmemory == 0 ||
         zmalloc_used_memory() < server.maxmemory) &&
        is_normal_client) break;
}
```

`_writeToClientNonSlave` 里真正的 flush 顺序就是 1.9 节说的"先静态缓冲后链表"——先把 `c->buf[0..bufpos)` 尽量 `write` 出去,写完了再把 `c->reply` 链表里每个节点的 SDS 依次写出去。注意 `sentlen` 字段——它记"这一次写到一半停在哪",下次接着写。如果 socket 缓冲满了(`write` 返回 `EAGAIN`),`writeToClient` 就先停,client 留在"待写队列"里,等下一轮 epoll 通知"socket 可写了"再继续。

这里有一个防止单 client 霸占主线程的机制——`NET_MAX_WRITES_PER_EVENT` 上限。如果一个 client 的回复非常大(比如 `KEYS *` 返回百万级 key、`LRANGE` 返回十万条),写它一个就可能耗尽这一轮事件循环的所有时间,其他 client 全饿着。Redis 的对策是:**单个 client 一次 flush 写到 `NET_MAX_WRITES_PER_EVENT` 字节就停,让一让,主线程去处理别的 client,下一轮再回来继续写**。这个上限对 slave 和 monitor 不生效(@2133-2135 注释:"on high-speed traffic, the output buffer will grow indefinitely"),因为复制流必须尽快排空,不能拖。

写完后,如果 client 没有待发回复了(@2160 `!clientHasPendingReplies(c)`),会 `connSetWriteHandler(c->conn, NULL)`(@2168)——**把"可写"回调注销掉**。这是 reactor 的另一个核心动作:**只在"有东西要发"时才注册可写事件**,没事不挂。否则 epoll 会因为 socket 一直可写而每轮都唤醒主线程,白烧 CPU。`addReply` 路径上的 `_prepareClientToWrite`([networking.c:299](../../redis-8.0.2/src/networking.c#L299) 周边)负责在第一次有回复时把可写回调注册回去——一来一回,事件注册/注销形成闭环。

> **钉死这件事**:`writeToClient` 的 flush 顺序严格"先静态缓冲后链表",写到一半 socket 满了用 `sentlen` 记进度下次续写。两个防守性设计:**单 client 写到 `NET_MAX_WRITES_PER_EVENT` 就让一让**(防霸占主线程)、**没待发回复就立刻注销可写事件**(防 epoll 空转烧 CPU)。reactor 的"按需注册"原则在这里和"批量 flush"联手,让网络写出路径既快又公平。

## 1.11 技巧精解⑤:client 内存桶式驱逐——超内存时专挑最胖的下手

最后讲一个保护机制。一个 Redis 实例可能挂着几万个 client,每个 client 都有自己的 querybuf、buf、reply 链表——这些内存算在 `client` 总内存里。如果某个 client 发疯(比如 `KEYS *` 拉出几十 GB 的回复、或一个慢消费端让 reply 链表无限增长),Redis 就可能被它的 client 内存拖垮。

Redis 的对策是**桶式驱逐**(bucket eviction)。看常量定义:

```c
/* server.h:129-133 */
/* Bucket sizes for client eviction pools. Each bucket stores clients with
 * memory usage of up to twice the size of the bucket below it. */
#define CLIENT_MEM_USAGE_BUCKET_MIN_LOG 15 /* Bucket sizes start at up to 32KB (2^15) */
#define CLIENT_MEM_USAGE_BUCKET_MAX_LOG 33 /* Bucket for largest clients: sizes above 4GB (2^32) */
#define CLIENT_MEM_USAGE_BUCKETS (1+CLIENT_MEM_USAGE_BUCKET_MAX_LOG-CLIENT_MEM_USAGE_BUCKET_MIN_LOG)
```

`CLIENT_MEM_USAGE_BUCKETS = 1 + 33 - 15 = 19` 个桶。每个桶装"内存用量在一个 2 的幂区间内"的 client——第 0 桶装 ≤ 32KB(2^15)的,第 1 桶装 ≤ 64KB(2^16)的,以此类推,第 18 桶装 > 4GB(2^32)的大户。一个 client 用多少内存,通过 `updateClientMemUsageAndBucket`([server.c:999](../../redis-8.0.2/src/server.c#L999))算出来,然后挂到对应桶里。

驱逐逻辑在 `evictClients`([networking.c:4532](../../redis-8.0.2/src/networking.c#L4532)),它在每轮 beforeSleep 里被调([server.c:1858](../../redis-8.0.2/src/server.c#L1858)):

```c
/* networking.c:4532-4584,精简 */
void evictClients(void) {
    if (!server.client_mem_usage_buckets) return;
    int curr_bucket = CLIENT_MEM_USAGE_BUCKETS-1;          /* 从最大桶开始 */
    ...
    size_t client_eviction_limit = getClientEvictionLimit();
    if (client_eviction_limit == 0) return;
    while (server.stat_clients_type_memory[CLIENT_TYPE_NORMAL] +
           server.stat_clients_type_memory[CLIENT_TYPE_PUBSUB] >= client_eviction_limit) {
        listNode *ln = listNext(&bucket_iter);
        if (ln) {
            client *c = ln->value;
            size_t last_memory = c->last_memory_usage;
            int tid = c->running_tid;
            if (tid != IOTHREAD_MAIN_THREAD_ID) {          /* IO 线程手中的 client 先 pause 再看 */
                pauseIOThread(tid);
                updateClientMemUsageAndBucket(c);          /* 重新估一次,防桶滞后 */
            }
            /* 只有内存没减且桶没变,才真驱逐 */
            if (c->last_memory_usage >= last_memory ||
                c->mem_usage_bucket == &server.client_mem_usage_buckets[curr_bucket])
            {
                ...
                freeClient(c);
                server.stat_evictedclients++;
            }
            if (tid != IOTHREAD_MAIN_THREAD_ID) {
                resumeIOThread(tid);
                listRewind(server.client_mem_usage_buckets[curr_bucket].clients, &bucket_iter);
            }
        } else {
            curr_bucket--;                                   /* 这个桶空了,往小桶走 */
            if (curr_bucket < 0) {
                serverLog(LL_WARNING, "Over client maxmemory after evicting all evictable clients");
                break;
            }
            listRewind(server.client_mem_usage_buckets[curr_bucket].clients, &bucket_iter);
        }
    }
}
```

逻辑很有讲究:

1. **从最大桶(CLIENT_MEM_USAGE_BUCKETS-1)往下扫**——驱逐时优先挑最胖的 client。这很合理:同样数量,干掉几个大户比干掉一堆小户更省内存;而且大户往往就是"出问题"的那个(慢消费端、`KEYS *`、巨大 pipelining)。
2. **驱逐前重新估一次内存**——一个 client 可能挂在最大桶里是因为它"曾经"很大,但现在内存已经降下来了(回复被消费完、querybuf 修剪了)。如果直接按桶驱逐,会误杀已经变小的 client。所以 @4558 重新调 `updateClientMemUsageAndBucket`,只有"内存没减 且 桶没变"才真驱逐(@4560)。
3. **IO 线程手里的 client 要先 pause**——因为 IO 线程正在用它,直接 free 会 use-after-free。`pauseIOThread(tid)` 让那个 IO 线程停下来,安全 free,然后 `resumeIOThread` 唤醒。

> **钉死这件事**:client 桶式驱逐是"按 2 的幂分桶、从最大桶往下扫、驱逐前重估防误杀"的三件套。19 个桶覆盖 32KB 到 4GB+,开销极低(挂 list 而非排序树),但精度足够——驱逐只需要找"最胖的几个",不需要精确排序。驱逐前重新估内存这一步是关键防御——内存是动态的,挂进桶的时刻和真正驱逐的时刻之间,client 可能已经变小了,不重估就会误杀已经恢复的 client。

## 1.12 整章回扣:七件事攒在一起

整章读完,你会发现 networking.c 的每一行都在贯彻同一个潜意识:**主线程的时间很贵,任何能省的 syscall、能省的 memcpy、能省的 malloc/free,都要省掉**。读用事件回调(不阻塞)、解析够快(留主线程)、回复先攒后写(压 syscall)、缓冲按需复用(压 allocator)、大 bulk 零拷贝(压 memcpy)、双层 reply buffer(快速路径 + 慢速路径)、桶式驱逐(防内存失控)。七件事攒在一起,就是单线程撑高并发的物理前提。

(所有常量、字段、函数的精确行号锚点表,见章末验证物 §2。)

最后埋一个伏笔,留给第二十章:8.0 里,`networking.c` 到处是 `c->running_tid != IOTHREAD_MAIN_THREAD_ID` 这样的判断(比如 [networking.c:2810](../../redis-8.0.2/src/networking.c#L2810)、[networking.c:2834](../../redis-8.0.2/src/networking.c#L2834)、[networking.c:2888](../../redis-8.0.2/src/networking.c#L2888))——这意味着"读"和"写"这两件最枯燥的字节搬运,在 8.0 里可以外包给 IO 线程。事实上 8.0 走得更远:连**协议解析**也下放到了 IO 线程(解析只动 client 自己的 `querybuf`/`argv`,不碰共享数据)。但有一条**铁律**贯穿全书且从未动摇:**只有"命令执行"(`processCommand`/`call`)死死钉在主线程**——因为它要读写共享的 db 字典和各种对象,多线程碰它们就要加锁,一加锁就背叛了"单线程无锁"的纯粹。这条分界线,我们到 Ch20 会彻底讲透。

---

字节进来了,参数也解析成了 `argc/argv`。下一章,我们往上一层,看那个把"可读就回调 `readQueryFromClient`"这件事真正跑起来的心脏——事件循环 `ae.c`。它是 Redis 单线程模型的中枢,理解了它,你才理解 Redis 凭什么用一条线程撑起一切。

---

## 章末:回扣、五个为什么、往哪钻

### 主线回扣

这一章几乎全部落在**取向①(单线程 + 事件循环)**上,而且讲清了它的一个具体侧面——**主线程怎么处理网络 IO 才不浪费时间**:

- **读**:用事件回调(`readQueryFromClient` 注册到 `epoll`),数据来了才读,不空等;8.0 起连"读"本身都能下放 IO 线程;
- **解析**:multibulk 同步解析,够快,且 8.0 起也下放 IO 线程;但执行钉主线程;
- **写**:回复先攒 16KB 静态缓冲,溢出才上链表,一轮循环结束批量 flush,把 `write` 的系统调用次数压到最低。

每一步都在贯彻"主线程时间贵"——能不阻塞就不阻塞、能不拷贝就不拷贝、能不 malloc 就不 malloc、能不 write 就不 write。同时它也是**取向④(简单优先)**的体现:协议选文本而非二进制、定时器(第二章)选链表而非最小堆、reply buffer 选双层而非复杂的可变结构——都是"够用就好"的工程成熟度。8.0 把读和解析下放 IO 线程,但严格守住"执行钉主线程"这条铁律——这是取向①在面对"多核利用"诱惑时的最后坚持。

### 五个为什么

**Q1:为什么 RESP 是文本协议,而不是二进制?**
文本协议解析常数略高于二进制(要 `string2ll` 把 ASCII 数字转 long long),但在 Redis 整体瓶颈(网络 IO、内存访问)面前根本排不上号。选文本的真实收益是:① 可 telnet 手敲、对人友好,早期社区传播快;② 解析逻辑极简(length-prefixed,逐段啃),bug 面小;③ 长度前缀让它能装二进制 value,功能不输二进制协议。这是取向④的诚实选择——能用简单的,就不上复杂的。

**Q2:大 bulk 零拷贝那三个条件(`qb_pos==0`、`bulklen>=32KB`、`querybuf_len==bulklen+2`)缺一个会怎样?**
三个条件合起来表达一个意思——"bulk 恰好独占整个 querybuf"。`qb_pos==0` 保证 bulk 在 querybuf 头部(前面没有别的命令残留);`querybuf_len==bulklen+2` 保证 bulk 后面没有别的数据(后面没有别的命令);`bulklen>=32KB` 是阈值——小于 32KB 的 bulk 走零拷贝省下的内存,抵不过 createObject + sdsnewlen 的固定开销(过户也要开新 buffer),所以只对大户开启。缺任何一个,过户都不成立——比如 `qb_pos>0` 时,querybuf 里 bulk 前面还有别的命令的字节,过户会把它们一起卷走。

**Q3:`thread_reusable_qb` 既然只让一个 client 借走,意义有多大?看起来很受限。**
它的意义不在"一个 client 借走"(那确实受限),而在"每线程只 malloc 一次 16KB,后续无数个短连接 client 进进出出都在这一块内存上借还"。这针对的是**高并发短连接**场景——每秒几万条连接建了断、断了建,如果每条都各自 `sdsnewlen(16KB)` + `sdsfree`,jemalloc 的线程本地 cache 频繁溢出回中心堆,锁竞争随并发上升。复用一份,把这个压力压到几乎为零。同一时刻"只能一个 client 借"的代价,在 pipeline/长连接占主导的真实负载下基本看不到。

**Q4:双层 reply buffer 为什么静态满了之后,新回复全上链表,而不是继续往静态塞?**
保 flush 顺序。`writeToClient` 是"先静态缓冲后链表"——静态缓冲里的内容必须比链表里的早发出去。如果静态缓冲在链表非空时继续追加,那静态缓冲里就会混入"比链表第一个节点更晚产生的回复",flush 时这部分早发的回复就跑到链表那些更早的回复前面去了——顺序乱。所以 Redis 的规则是严格的"前后两段":要么全静态(链表空),要么静态满后追加全上链表。

**Q5:8.0 既然把读和解析都下放 IO 线程了,为什么执行(`processCommand`)还钉主线程?**
因为执行要读写共享数据——db 字典、expires 字典、对象引用计数、watched_keys 等等。多线程碰这些就要加锁,一加锁就破坏了 Redis"单线程无锁访问所有数据结构"的纯粹。锁竞争、缓存行失效、原子操作的开销,加上并发 bug 的风险,会把 Redis 的简单性和可预测性全毁掉。所以 8.0 的取舍是:**只把"无共享数据访问"的字节搬运(读、解析、查命令表)下放,执行和它要碰的所有共享数据,永远钉在主线程**。这条铁律是"单线程模型"的最后防线。

### 想继续深入往哪钻

- 想看 SDS 的扩容策略(贪婪翻倍 vs 非贪婪)和 `sdsrange` 的逻辑截断不释放底层内存的细节:读 [sds.c](../../redis-8.0.2/src/sds.c) 的 `sdsMakeRoomFor` / `sdsMakeRoomForNonGreedy` / `sdsrange`。
- 想理解 8.0 IO 线程怎么和 ae 协作:读 [networking.c](../../redis-8.0.2/src/networking.c) 的 `handleClientsWithPendingReadsUsingThreads`、`handleClientsWithPendingWritesUsingThreads`,以及 `afterSleep` 里挂的读分发——这是第二十章的主线。
- 想对比"语言级异步运行时"怎么处理网络 IO:看本系列《Tokio 设计与实现深入浅出》的 mio 事件循环章——Tokio 用 epoll/io_uring 收事件,但 `.await` 的"等待"是靠 Rust 的 async/await 状态机在用户态挂起任务,和 Redis 的"线程级 epoll_wait 阻塞"是两种不同尺度的方案。

### 引出下一章

至此你已经看清 Redis 的 networking 骨架:字节流进门被解析成 `argc/argv`,出门先攒双层缓冲再批量 flush。可是**谁在盯着成千上万个连接、判断"哪个现在可读了"?又是谁,在恰当的时候把 `readQueryFromClient` 调起来的?** 下一章,我们走进 `ae.c`——Redis 的心脏。它是事件循环的中枢,理解了它,你才理解 Redis 凭什么用一条线程,同时照看几万个连接。

---

## 验证物:如何亲手确认本章的设计

> 说明:本书写作环境为 Windows,无法直接运行 redis-server(8.0 依赖 fork/epoll 等 Linux 特性)。以下 (1) gdb 断点脚本 (2) 源码常量锚点 (3) CLIENT LIST / OBJECT ENCODING 观察项 均为可复现的精确指引,供读者在 Linux 环境(Ubuntu 22.04 / CentOS 8 等)对 redis-8.0.2 源码 `make no-opt`(Makefile 里 no-opt 目标会去掉 -O2 加 -g)编译后自行验证。**本书不附编造的运行输出**——凡未实跑的,只给脚本、预期观察变量与推导依据,不写具体数值。

### 1. gdb 断点脚本

编译:`cd redis-8.0.2 && make no-opt`(带 -g)
启动:`gdb ./src/redis-server`,另一终端 `redis-cli`。

```gdb
(gdb) break createClient               # 建连初始化,networking.c:120
(gdb) break readQueryFromClient        # 读主干,networking.c:2884
(gdb) break processMultibulkBuffer     # multibulk 解析,networking.c:2440
(gdb) break networking.c:2577          # createObject(querybuf) 零拷贝过户
(gdb) break _addReplyToBufferOrList    # 双层 reply,networking.c:384
(gdb) break writeToClient              # flush 出 socket,networking.c:2097
(gdb) break evictClients               # 桶式驱逐,networking.c:4532
(gdb) run --port 6379

# redis-cli 执行:SET foo bar,gdb 会在 createClient 停下(连接建立)
(gdb) print c->buf_usable_size      # 预期:PROTO_REPLY_CHUNK_BYTES (16KB) 附近(jemalloc 对齐后可能略大)
(gdb) print c->tid                  # 预期:IOTHREAD_MAIN_THREAD_ID
(gdb) print c->io_flags             # 预期:CLIENT_IO_READ_ENABLED | CLIENT_IO_WRITE_ENABLED
(gdb) continue

# SET foo bar 命令到达,readQueryFromClient 停下:
(gdb) print readlen                 # 预期:PROTO_IOBUF_LEN (16KB)
(gdb) print c->bulklen              # 预期:-1(还没开始啃 bulk)
(gdb) continue                       # 进入 processMultibulkBuffer:
(gdb) print c->multibulklen          # 预期:3(参数个数)
(gdb) print c->argc                  # 预期:随解析推进从 0 涨到 3
(gdb) print c->argv[0]               # 预期:OBJ_STRING,内容 "SET"
(gdb) print c->argv[1]               # 预期:OBJ_STRING,内容 "foo"
(gdb) print c->argv[2]               # 预期:OBJ_STRING,内容 "bar"

# 验证大 bulk 零拷贝:发一个 1MB value 的 SET
# redis-cli 执行:SET big $(head -c 1048576 /dev/urandom | base64)
# gdb 在 networking.c:2577 停下(条件满足:qb_pos==0 && bulklen>=32KB && querybuf_len==bulklen+2)
(gdb) print c->bulklen               # 预期:~1048576(1MB value 的长度)
(gdb) print c->querybuf              # 预期:非 NULL,即将被过户
(gdb) step                           # 执行 createObject(OBJ_STRING,c->querybuf) 后:
(gdb) print c->argv[c->argc-1]       # 预期:robj,ptr 指向原 querybuf 内存(零拷贝)
(gdb) print c->querybuf              # 预期:新 sdsnewlen(SDS_NOINIT,...) 出来的新空 buffer

# 验证双层 reply buffer:_addReplyToBufferOrList 在 SET 执行时被调
(gdb) print c->bufpos                # 预期:"+OK\r\n" 5 字节(命令执行完 addReply "+OK\r\n")
(gdb) print listLength(c->reply)     # 预期:0(5 字节装得下静态缓冲,不溢出)
```

**预期观察**(基于源码 [networking.c:384-430](../../redis-8.0.2/src/networking.c#L384) 的双层逻辑与 [networking.c:2572-2584](../../redis-8.0.2/src/networking.c#L2572) 的零拷贝逻辑,本书未实跑):普通短命令的回复始终停留在 `c->buf` 静态缓冲,链表 `c->reply` 长度为 0;大 bulk(≥32KB)的 SET 命令会触发零拷贝过户路径,`c->argv[2]->ptr` 直接指向原 querybuf 内存,后续 `c->querybuf` 是新分配的空 buffer。

### 2. 源码常量锚点(带行号,从 redis-8.0.2 源码 Grep 核实)

| 常量/字段 | 位置 | 值/说明 |
|----------|------|---------|
| `PROTO_IOBUF_LEN` | server.h:163 | `1024*16`(16KB,一次 read 默认) |
| `PROTO_REPLY_CHUNK_BYTES` | server.h:164 | `16*1024`(静态 reply buf 预分配) |
| `PROTO_INLINE_MAX_SIZE` | server.h:165 | `1024*64`(inline 协议单次读上限) |
| `PROTO_MBULK_BIG_ARG` | server.h:166 | `1024*32`(≥32KB 走零拷贝) |
| `CLIENT_MEM_USAGE_BUCKETS` | server.h:133 | `19`(15→33,32KB→4GB+) |
| `thread_reusable_qb` 声明 | networking.c:35-37 | `__thread sds`,每 IO 线程一份 |
| `createClient` 注册读回调 | networking.c:131 | `connSetReadHandler(conn, readQueryFromClient)` |
| `createClient` 预分配 buf | networking.c:134 | `zmalloc_usable(PROTO_REPLY_CHUNK_BYTES, ...)` |
| `readQueryFromClient` 大参数分支 | networking.c:2901-2919 | readlen 调到刚好够,显式走私有 buffer |
| `thread_reusable_qb` 借出逻辑 | networking.c:2920-2940 | 优先复用,@2936 `c->querybuf = thread_reusable_qb` |
| 贪婪/非贪婪扩容 | networking.c:2942-2959 | master+大 buf 走贪婪,其他走非贪婪 |
| 零拷贝过户三条件 | networking.c:2572-2575 | `!MASTER && qb_pos==0 && bulklen>=BIG && querybuf_len==bulklen+2` |
| 零拷贝过户主体 | networking.c:2577-2584 | createObject + sdsIncrLen(-2) + sdsnewlen(SDS_NOINIT) |
| `_addReplyToBufferOrList` | networking.c:384-430 | 双层:静态 memcpy@423,溢出挂链表@429 |
| `writeToClient` 单 client 上限 | networking.c:2137 | `totwritten > NET_MAX_WRITES_PER_EVENT` 就 break |
| `evictClients` 桶式驱逐 | networking.c:4532-4584 | 从最大桶扫@4536,驱逐前重估@4558 |
| `processCommand`(执行入口) | server.c:3985 | 全书铁律:只在主线程跑 |

### 3. CLIENT LIST / OBJECT ENCODING 观察项(需本地 redis-server)

> 以下操作需在 Linux 本地启动 redis-server 后用 redis-cli 执行。本书未实跑,仅列观察方法与预期推导依据。

```text
# 观察 client 状态(验证 reply buffer 字段):
127.0.0.1:6379> CLIENT LIST
# 预期输出每行包含:omem=0(无待发回复内存)、tot-mem=...(client 总内存)、cmd=client|set|...
# 在执行大回复命令(如 LRANGE 大 key)后立即再 CLIENT LIST:
#   预期 omem>0(回复挂链表后待发)、tot-mem 涨(链表节点吃内存)

# 观察 querybuf 大小(client 内存统计):
127.0.0.1:6379> CONFIG GET client-output-buffer-limit   # 默认 reply buf 限制
127.0.0.1:6379> CONFIG GET maxmemory-clients            # client 总内存阈值,触发 evictClients

# 触发桶式驱逐(警告:这会让 client 被踢,生产慎做):
127.0.0.1:6379> CONFIG SET maxmemory-clients 1m         # 把阈值调到 1MB
# 然后用一个慢消费端不读 socket,Redis 一直往它 reply 链表塞
# 预期:CLIENT LIST 里 omem 持续增长,达阈值后该 client 被 freeClient,evictedclients 计数+1
127.0.0.1:6379> INFO clients                            # 预期 evicted_clients 计数上涨

# 观察对象编码(零拷贝过户后 value 的对象形态):
127.0.0.1:6379> SET small hello                         # 5 字节 value
127.0.0.1:6379> OBJECT ENCODING small                   # 预期 embstr(小字符串共享编码)
127.0.0.1:6379> SET big $(head -c 1000000 /dev/urandom | base64)   # 1MB value
127.0.0.1:6379> OBJECT ENCODING big                     # 预期 raw(大字符串独立编码,走零拷贝过户路径)
```

标注:以上预期基于源码常量([server.h:163-166](../../redis-8.0.2/src/server.h#L163)、[server.h:133](../../redis-8.0.2/src/server.h#L133))与 [networking.c](../../redis-8.0.2/src/networking.c) 各路径行为推导,本书未在本地实跑;若你的 redis 版本/配置不同(如 jemalloc 对齐策略、client-output-buffer-limit 默认值),具体数值可能偏移,以 `CONFIG GET` 实际值为准。桶式驱逐部分尤其要谨慎——把 `maxmemory-clients` 调小会真的踢 client,生产环境勿试。
