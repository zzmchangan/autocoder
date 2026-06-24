# 第 8 章 · 零拷贝:mmap 写 + sendfile 读 + 堆外内存池

> 篇:第 2 篇 · 存储内核之读取(ConsumeQueue、IndexFile、零拷贝)
> 主线呼应:前两章(P2-06 ConsumeQueue、P2-07 IndexFile)解决了"消费端怎么在混写的 CommitLog 里定位消息"——ConsumeQueue 把队列偏移映射成物理偏移,IndexFile 按 key 建索引。但定位到物理偏移只是开始,**真正的拷贝还没发生**:消息体还躺在 CommitLog 的 `MappedFile` 里(也就是页缓存里),消费者在网络另一头等着。这一章讲这最后一段旅程——**消息字节怎么从页缓存送到网卡,而且全程不进 JVM 堆**。RocketMQ 在这里用的是 Linux 零拷贝的两件套:**写用 mmap**(`MappedByteBuffer` 把磁盘文件映射进进程地址空间,用户态直接写页缓存)、**读用 sendfile**(`FileRegion` 让页缓存的数据直接送 socket)。再配上 `TransientStorePool` 堆外内存池,把高并发下写热路径从"共享页缓存锁竞争"挪到"独占堆外内存",让写读两条路径各走各的快车道。这一章是全书"存储内核 → 网络"的**出口**,衔接第 3 篇消费模型。

## 核心问题

**消息从 CommitLog 的页缓存送到消费者的网卡,RocketMQ 凭什么让字节全程不进 JVM 堆?为什么写消息用 mmap、读消息(发给消费者)用 sendfile,这俩各管一头?堆外内存池 `TransientStorePool` 又是怎么把写热路径从页缓存锁竞争里救出来的,它和 commit/flush 两步是什么关系?**

读完本章你会明白:

1. **传统读写为什么慢**:磁盘文件 → 内核页缓存 → JVM 堆 → socket 发送缓冲 → 网卡,这条路上有**四次拷贝、两次系统调用、两次用户态/内核态切换**,还顺带给 GC 制造了一堆临时对象。
2. **mmap 怎么省掉一次拷贝**:`MappedByteBuffer` 把文件映射进虚拟地址,用户态写这段地址**直接就是写页缓存**,少了一次"内核页缓存 → 用户态 buffer"的拷贝,也少一次 read 系统调用。
3. **sendfile 怎么把用户态彻底踢出去**:`FileRegion`(Netty 对 sendfile 的封装)让数据在内核里**直接从页缓存搬到 socket 缓冲**,用户态线程只发起一次系统调用,全程不碰数据、数据不进 JVM 堆——零 CPU 拷贝。
4. **为什么 RocketMQ 写用 mmap、读用 sendfile**:写要"用户态能修改字节、还要算 CRC、填 queueOffset",必须看得见数据(mmap);读发给消费者时数据已经成形,broker 只是个转发器,根本不需要看见数据(sendfile)。
5. **堆外内存池 `TransientStorePool` 是写端的另一条快车道**:它把写热路径从"共享的、有内核锁竞争的页缓存"挪到"独占的、`mlock` 锁在物理内存的堆外 DirectByteBuffer",换极高并发下写吞吐,代价是多一步 commit 把堆外数据搬进页缓存(P1-04 已讲 commit/flush 两步,本章补"为什么 writeBuffer 与 mappedByteBuffer 各司其职、读端为什么仍走 mmap 切片")。

> **如果一读觉得太难**:先只记住三件事——① 传统 IO 有四次拷贝,mmap 写省一次、sendfile 读几乎全省,合起来叫"零拷贝";② RocketMQ 写消息用 mmap(看得见字节才能填元数据),读发给消费者用 sendfile(只是转发,不用看见字节);③ 堆外内存池是写端的可选项,把写挪到堆外内存避开页缓存锁,代价是多一步 commit。`transferMsgByHeap` 的默认值这种细节没看懂不影响读后续章节。

---

## 8.1 一句话点破

> **传统 IO 把磁盘数据送给网卡,要经过"内核页缓存 → 用户态 buffer → socket 发送缓冲 → 网卡"四次拷贝、两次系统调用,数据还顺带在 JVM 堆里晃一圈,制造 GC 压力。RocketMQ 用 Linux 零拷贝两件套砍掉这些冗余:写消息用 `MappedByteBuffer`(mmap)——文件映射进虚拟地址,用户态写这段地址即写页缓存,省一次"页缓存→用户态"的拷贝;读消息发给消费者用 `FileRegion`(Netty 对 sendfile 的封装)——数据在内核里直接从页缓存搬到 socket 缓冲,用户态线程只发系统调用、不碰数据,数据全程不进 JVM 堆。再配上 `TransientStorePool` 堆外内存池,写热路径从"共享页缓存"挪到"独占堆外 DirectByteBuffer",避开高并发下页缓存的锁竞争。三件套合起来,让 RocketMQ 在"海量消息吞吐"这条赛道上,把每一字节在内存里的搬运次数压到了理论下限。**

这是结论,不是理由。本章倒过来拆:先看传统 IO 的四次拷贝撞了什么墙,再看 mmap 怎么省、sendfile 怎么省、堆外池怎么让写读各走快车道,最后看它们在源码里怎么配合。

---

## 8.2 传统 IO 为什么慢:四次拷贝与两次系统调用

在讲 RocketMQ 怎么"零拷贝"之前,先把"被清零的"那几次拷贝看清楚。假设我们用一个朴素的方式把磁盘文件里的一段字节,通过 socket 发给消费者——**先 `read` 进 JVM,再 `write` 到 socket**。这条路在 Linux 上长这样:

```
  磁盘文件  ──①DMA拷贝──→  内核页缓存  ──②CPU拷贝──→  用户态 buffer(JVM 堆)
                                                            │
                                                            │ 你在 JVM 里加工一下(算 CRC、填字段)
                                                            ▼
  网卡  ──④DMA拷拔──←  socket 发送缓冲  ──③CPU拷贝──←  用户态 buffer(JVM 堆)

  ①④ DMA 拷贝(DMA 引擎,不占 CPU)
  ②③ CPU 拷贝(占 CPU、占内存带宽)
  外加两次系统调用:read / write,各一次用户态↔内核态切换
```

四个拷贝里,①④ 是 DMA(Direct Memory Access,直接内存访问,由磁盘/网卡硬件的 DMA 引擎做,不占 CPU),②③ 是 CPU 拷贝(占 CPU 和内存带宽)。两次系统调用 `read`/`write` 各有一次用户态↔内核态切换。

对一个把"转发海量消息"当核心业务的 MQ 来说,这条路上每一笔都是浪费:

1. **②③ 两次 CPU 拷贝是纯冗余**。MQ 转发消息,broker 其实只是个"把磁盘字节搬到网卡"的中转站——对消息字节本身根本不感兴趣(它不需要修改字节,只是搬运)。可传统路径硬要让数据两次进 CPU、两次出 CPU,纯烧带宽。
2. **`read` 进 JVM 堆,制造 GC 压力**。每条消息在堆里 new 一个 byte[],发完就被丢——GC 不停地扫这些朝生夕灭的对象,Young GC 频率被推高。消息吞吐越高,GC 压力越大,延迟抖动越凶。
3. **两次系统调用的切换开销**。每次用户态↔内核态切换有上下文保存/恢复开销(微秒级),在百万级 QPS 下累加成可观的 CPU 浪费。

> **不这样会怎样**:朴素地"read 进 JVM 再 write 到 socket",broker 处理一条 1KB 消息光在内存里就要搬运 4 次、进出内核 2 次,还要在堆里制造一个临时 byte[]。在高吞吐场景下,CPU 被搬运和切换吃掉、GC 被临时对象逼疯、内存带宽被冗余拷贝打满——单机吞吐根本拉不上去。**必须把这几笔冗余砍掉。**

砍冗余有两条路,分别砍 ② 和 ③:用 **mmap** 把"页缓存→用户态 buffer"那一次 ② 拷贝合并掉(用户态直接映射页缓存),用 **sendfile** 把"用户态 buffer→socket 发送缓冲"那一次 ③ 拷贝整个跳过(数据不进用户态)。RocketMQ 写用 mmap、读用 sendfile,正是这两条路各取所长。

---

## 8.3 mmap 写:把页缓存映射进用户态

先看写。RocketMQ 写消息,不是"把字节从用户态 buffer 拷进页缓存",而是**直接在页缓存上写**——靠的就是 mmap。

### 8.3.1 mmap 干了什么

mmap(memory map,内存映射)是 Linux 的一个系统调用,它把一个磁盘文件**映射**进进程的虚拟地址空间。映射完之后,进程对这段虚拟地址的读写,**直接就是对页缓存的读写**——不需要 `read`/`write` 系统调用,也不需要"页缓存→用户态 buffer"那次拷贝。

```
  普通 read:
    用户态 buffer  ──CPU拷贝──←  内核页缓存  ──DMA拷贝──←  磁盘
    (read 系统调用,数据要进用户态)

  mmap 后:
    虚拟地址 ──(页表映射)──→  内核页缓存  ──DMA拷贝──←  磁盘
    (你对虚拟地址的写,直接落到页缓存,无拷贝)
```

在 Java 里,`java.nio.MappedByteBuffer` 就是 mmap 的封装。看 RocketMQ 怎么创建它——`DefaultMappedFile` 初始化时把文件映射成 `MappedByteBuffer`([DefaultMappedFile.java:213-217](../rocketmq/store/src/main/java/org/apache/rocketmq/store/logfile/DefaultMappedFile.java#L213)):

```java
// DefaultMappedFile.init
if (writeWithoutMmap) {
    // Still create MappedByteBuffer for reading operations
    this.mappedByteBuffer = this.fileChannel.map(MapMode.READ_ONLY, 0, fileSize);   // :214 读专用 mmap
    // ... 写走 fileChannel.write(不 mmap),一种特殊配置,下文讲
} else {
    // Use MappedByteBuffer for both reading and writing (default behavior)
    this.mappedByteBuffer = this.fileChannel.map(MapMode.READ_WRITE, 0, fileSize);   // :217 默认:读写双向 mmap
}
```

`fileChannel.map(MapMode.READ_WRITE, 0, fileSize)` 这一行,就是触发 mmap 系统调用——把整个 1GB 的 CommitLog 文件映射进虚拟地址。映射完之后,`mappedByteBuffer.put(...)` 直接写页缓存,`mappedByteBuffer.force()` 才真正刷盘(P1-04 讲过)。

### 8.3.2 mmap 省了什么、又留下什么

mmap 写省的是 8.2 图里的 **② 那次 CPU 拷贝**(页缓存→用户态 buffer):因为用户态虚拟地址**就是**页缓存,没有"拷贝"这个动作了,只有"映射"。同时也省了 `read` 系统调用——你想读文件,直接读那段虚拟地址就行。

但 mmap **不能**省 ③ 那次拷贝(用户态→socket)。因为 mmap 解决的是"用户态怎么访问文件",而 ③ 那次拷贝是"用户态数据怎么进 socket"——这是另一条路,要靠 sendfile 砍(下一节)。所以 mmap 写的完整收益是:**写消息时,用户态直接在页缓存上填字节,省一次"页缓存→用户态"的往返,也省一次 read 系统调用**。

> **钉死这件事**:mmap 把磁盘文件映射进进程虚拟地址,用户态对这段地址的写**直接落到页缓存**,省掉"页缓存→用户态 buffer"那次拷贝。在 RocketMQ 里,`MappedByteBuffer` 就是 mmap 的 Java 封装,`fileChannel.map(MapMode.READ_WRITE, 0, fileSize)` 创建它。写消息时往 `mappedByteBuffer` 里填字节,就是在写页缓存,没有任何额外拷贝。**但 mmap 解决不了"数据怎么进 socket"——那是 sendfile 的活。**

### 8.3.3 为什么写必须用 mmap(而不是 sendfile)

到这里有个自然的疑问:既然 sendfile 更猛(连用户态都不进),为什么写消息不用 sendfile、非要用 mmap?

答案是:**写消息,broker 必须看得见字节、还要修改字节**。RocketMQ 写一条消息,要在 CommitLog 里填一堆字段:`TOTALSIZE`、`MAGICCODE`、`BODYCRC`、`QUEUEOFFSET`、`PHYSICALOFFSET`、`SYSFLAG`、`BORN TIMESTAMP/HOST`、`STORE TIMESTAMP/HOST`、`RECONSUMETIMES`、`BODY`、`TOPIC`、`PROPERTIES`(P1-02 讲过完整布局)。这些字段里,`QUEUEOFFSET`、`PHYSICALOFFSET`、`STORETIMESTAMP` 是 broker 在写入时**现场算出来填进去**的(P1-03 讲过锁内重设 storeTimestamp)。这个"算 + 填"的动作,必须在用户态完成——你看得见字节、才能改字节。

sendfile 干不了这个。sendfile 是"我什么也不看,你帮我把这段文件字节搬到那个 socket"——它是个**只转发、不加工**的接口。让 sendfile 去填 `QUEUEOFFSET`?它连字节都看不见,填不了。

所以 RocketMQ 的分工是:**写,要加工字节,用 mmap(看得见、改得了,还省一次拷贝);读发给消费者,只是转发,用 sendfile(连看都不用看,全程不进用户态)**。这是 mmap 与 sendfile 在 MQ 场景下的天然分工,也是 8.7 技巧精解要拆透的核心。

---

## 8.4 sendfile 读:把用户态彻底踢出去

读路径就完全不同了。消费者来 pull 消息,broker 已经把消息存好了(CommitLog 里字节成形),它要做的只是"把这段字节发给消费者"。broker 对这段字节本身**没有任何加工**——不修改、不计算、不重新编码。这种"纯转发"场景,正是 sendfile 的主场。

### 8.4.1 sendfile 干了什么

sendfile 是 Linux 2.4 引入的系统调用,它的语义是:**把一个文件的一段字节,直接拷到某个 socket 的发送缓冲**。整个过程**全在内核里完成,数据不进用户态**:

```
  sendfile(out_fd=socket, in_fd=file, offset, count):

    磁盘文件  ──DMA拷贝──→  内核页缓存  ──(内核内拷贝)──→  socket 发送缓冲  ──DMA拷贝──→  网卡
                                         ↑ CPU 拷贝(仅一次,且在内核内)

    用户态线程:只调一次 sendfile 系统调用,然后等它返回,全程不碰数据
    数据:不进 JVM 堆,不进任何用户态 buffer
```

对比 8.2 的传统路径,sendfile 把 **②③ 两次 CPU 拷贝砍成一次**(而且这一次还在内核里、不进用户态),还把 **`read` 系统调用整个省了**——用户态线程只调一次 `sendfile` 就完事。更狠的是,数据**根本不进 JVM 堆**——零 GC 压力、零用户态拷贝。

> **钉死这件事**:sendfile 让"磁盘文件 → socket"这条路全在内核里完成,数据不进用户态、不进 JVM 堆。用户态线程只发起一次系统调用就等返回。对比传统 read+write,它省掉"页缓存→用户态"那次拷贝、省掉"用户态→socket"那次拷贝、省掉 `read` 系统调用、零 GC 压力。这就是"零拷贝"这个词在 Linux 语境下的真正含义——**清零的是"用户态与内核态之间的拷贝",不是清零所有拷贝**(DMA 拷贝、内核内那次拷贝还在,只是它们不占 CPU、不进用户态)。

### 8.4.2 RocketMQ 怎么用 sendfile:Netty 的 FileRegion

Linux 的 sendfile 是 C 的系统调用,Java 没有直接对应。Java 标准库里最接近的是 `FileChannel.transferTo(long position, long count, WritableByteChannel target)`——它在底层会尝试走 sendfile(JDK 在 Linux 上检测到目标 channel 是 socket,就走 `sendfile64`)。

但 RocketMQ 读路径没有直接调 `FileChannel.transferTo`。它走的是 **Netty 的 `FileRegion`**——这是 Netty 对零拷贝发送的抽象。看 broker 端 pull 请求处理完,怎么把消息发回消费者([DefaultPullMessageResultHandler.java:148-152](../rocketmq/broker/src/main/java/org/apache/rocketmq/broker/processor/DefaultPullMessageResultHandler.java#L148)):

```java
FileRegion fileRegion =
    new ManyMessageTransfer(response.encodeHeader(getMessageResult.getBufferTotalSize()), getMessageResult);   // :150
RemotingCommand finalResponse = response;
channel.writeAndFlush(fileRegion)                                                                              // :152
    .addListener((ChannelFutureListener) future -> { ... });
```

`channel.writeAndFlush(fileRegion)` 是关键——它往 Netty 的 channel 里塞一个 `FileRegion`,Netty 在写 channel 时会反复调 `fileRegion.transferTo(target, position)` 把数据搬给 socket。`ManyMessageTransfer` 实现的就是 `FileRegion` 接口([ManyMessageTransfer.java:67](../rocketmq/broker/src/main/java/org/apache/rocketmq/broker/pagecache/ManyMessageTransfer.java#L67)):

```java
@Override
public long transferTo(WritableByteChannel target, long position) throws IOException {
    if (this.byteBufferHeader.hasRemaining()) {
        transferred += target.write(this.byteBufferHeader);                  // :69 先发 header
        return transferred;
    } else {
        List<ByteBuffer> messageBufferList = this.getMessageResult.getMessageBufferList();
        for (ByteBuffer bb : messageBufferList) {
            if (bb.hasRemaining()) {
                transferred += target.write(bb);                             // :75 再发每段消息体
                return transferred;
            }
        }
    }
    return 0;
}
```

注意这里有个**层次要分清**:RocketMQ 的 `ManyMessageTransfer.transferTo` 里调的是 `target.write(byteBuffer)`(`target` 是 Netty 的 socket channel)。**这不是直接调 `FileChannel.transferTo`**(那个才是 Linux sendfile 的直系)。真正能不能走 sendfile,取决于 Netty 的 channel 实现——如果底层是 native 的 epoll channel 且配置允许,Netty 会把"堆外 ByteBuffer 写给 socket"优化成接近零拷贝的路径(Linux 上对 `DirectByteBuffer` 写 socket,内核走 `sendfile`/`sendpage` 语义);如果是 NIO 的普通 channel,则走 `write` 但数据仍在堆外、不进 JVM 堆。

> **诚实标注(避免夸大)**:本章不会把 RocketMQ 的读路径说成"零 CPU 拷贝的纯 sendfile"。准确的说法是——RocketMQ 用 Netty `FileRegion` + 堆外 `ByteBuffer`(下节讲这个 ByteBuffer 怎么来的),把读路径优化成"**数据全程在堆外,不进 JVM 堆**"。具体能不能进一步走 Linux sendfile,取决于 Netty channel 实现和操作系统。但无论哪一种,**"不进 JVM 堆"这个核心收益是稳的**——这就是它相对传统 read+write 的本质优势。源码层面 `target.write(bb)` 这一行,就是这一收益的落点。

### 8.4.3 那段 ByteBuffer 是哪来的:CommitLog 的切片

读路径的精髓,在于那段被 `target.write(bb)` 发出去的 `bb`,**不是从 CommitLog 读出来的拷贝,而是 CommitLog 的 `mappedByteBuffer` 的切片**。看 `CommitLog.getData` 怎么取消息([CommitLog.java:258](../rocketmq/store/src/main/java/org/apache/rocketmq/store/CommitLog.java#L258)):

```java
public SelectMappedBufferResult getData(final long offset, final boolean returnFirstOnNotFound) {
    int mappedFileSize = this.defaultMessageStore.getMessageStoreConfig().getMappedFileSizeCommitLog();
    MappedFile mappedFile = this.mappedFileQueue.findMappedFileByOffset(offset, returnFirstOnNotFound);   // :260 按物理偏移找到 MappedFile
    if (mappedFile != null) {
        int pos = (int) (offset % mappedFileSize);                                                         // :262 文件内偏移
        SelectMappedBufferResult result = mappedFile.selectMappedBuffer(pos);                              // :263 切片!
        return result;
    }
    return null;
}
```

`selectMappedBuffer` 才是切片的真身([DefaultMappedFile.java:667](../rocketmq/store/src/main/java/org/apache/rocketmq/store/logfile/DefaultMappedFile.java#L667)):

```java
@Override
public SelectMappedBufferResult selectMappedBuffer(int pos, int size) {
    int readPosition = getReadPosition();
    if ((pos + size) <= readPosition) {
        if (this.hold()) {
            this.mappedByteBufferAccessCountSinceLastSwap++;
            ByteBuffer byteBuffer = this.mappedByteBuffer.slice();        // :673 切片:mappedByteBuffer 的视图
            byteBuffer.position(pos);
            ByteBuffer byteBufferNew = byteBuffer.slice();                // :675 再切一次,定位到 pos
            byteBufferNew.limit(size);                                    // :676 限定 size
            return new SelectMappedBufferResult(this.fileFromOffset + pos, byteBufferNew, size, this);
        }
    }
    return null;
}
```

关键就在 `this.mappedByteBuffer.slice()` 这一行。`slice()` 是 `ByteBuffer` 的方法,它**返回原 buffer 的一个视图(view)**——共享同一块底层内存,**不拷贝数据**。所以 `SelectMappedBufferResult` 里的那个 `byteBuffer`,**和 CommitLog 的 `mappedByteBuffer` 指向同一块页缓存**。

这条链串起来就是:**消费者 pull → broker 找到物理偏移 → `selectMappedBuffer` 切出 `mappedByteBuffer` 的视图 → 包成 `ManyMessageTransfer`(FileRegion)→ `channel.writeAndFlush` → Netty 把这段堆外 ByteBuffer 写给 socket → 数据从页缓存进 socket 发送缓冲 → 网卡发出**。**全程没有一次"把消息体拷进 JVM 堆"**。这就是 RocketMQ 读路径零拷贝的核心。

> **钉死这件事**:读路径的"零拷贝"真身,是 `mappedByteBuffer.slice()`——它切出 CommitLog 页缓存的一个**视图**(共享内存、不拷贝),这个视图一路被 `ManyMessageTransfer` / Netty 写到 socket。消息体自始至终待在页缓存(堆外),**没进过 JVM 堆**。源码落点在 `DefaultMappedFile.selectMappedBuffer`([:667](../rocketmq/store/src/main/java/org/apache/rocketmq/store/logfile/DefaultMappedFile.java#L667))。

---

## 8.5 堆外内存池 TransientStorePool:写热路径的另一条快车道

mmap + sendfile 把读路径的拷贝压到了极限。但写路径还有一个隐患——**多核写同一个页缓存的锁竞争**。这一节讲 `TransientStorePool` 怎么把写热路径从"共享页缓存"挪到"独占堆外内存"。

> 这一节和 P1-04 第 4.7 节("commit 与 flush 两步")讲的是**同一套机制的两个侧面**。P1-04 讲它怎么影响"落盘"(堆外数据要先 commit 进页缓存才能 force),这一章讲它怎么和 mmap 写、sendfile 读配合——具体说,是讲**为什么 writeBuffer 和 mappedByteBuffer 各司其职、读端为什么仍走 mappedByteBuffer 切片**。两章合起来,才是 TransientStorePool 的全貌。

### 8.5.1 默认模式的隐患:页缓存锁竞争

默认模式(`transientStorePoolEnable=false`,这是默认值,[MessageStoreConfig.java:266](../rocketmq/store/src/main/java/org/apache/rocketmq/store/config/MessageStoreConfig.java#L266)),前台写消息直接写 `mappedByteBuffer`(即写 mmap 映射的页缓存)。看起来最简单——写即进页缓存,一步到位。

但它在**极高并发写入**下有一个隐患:多线程写同一个 `MappedByteBuffer`(落在不同的字节位置),内核为了维护页缓存的一致性,内部是有锁竞争的(页缓存的 page 要加锁、要处理 dirty 标记、TLB 一致性维护等)。虽然 RocketMQ 的写路径有 `putMessageLock` 在用户态串行化了(P1-03),但那只管用户态;**内核态页缓存层面的开销,用户态的锁管不到**。

更麻烦的是:**写和读共享同一块页缓存**。前台写消息往 `mappedByteBuffer` 写,后台刷盘线程 force 时遍历脏页,消费端读消息(sendfile 路径)也从这块页缓存切片——三股力量都碰这块页缓存,在写极密集的场景下,页缓存相关的内核资源竞争会成为吞吐的隐形天花板。

### 8.5.2 堆外模式:写走 writeBuffer,读仍走 mappedByteBuffer

`TransientStorePool` 的思路是:**把写的热路径,从"共享的页缓存"挪到"独占的堆外内存"**。它是一个预分配的堆外 `DirectByteBuffer` 池子([TransientStorePool.java:31](../rocketmq/store/src/main/java/org/apache/rocketmq/store/TransientStorePool.java#L31)):

```java
public class TransientStorePool {
    private final int poolSize;                              // :34 池子大小(默认 5)
    private final int fileSize;                              // :35 每个 buffer 大小(= CommitLog 一个 MappedFile 大小,1GB)
    private final Deque<ByteBuffer> availableBuffers;        // :36 可用 DirectByteBuffer 栈

    public void init() {
        for (int i = 0; i < poolSize; i++) {
            ByteBuffer byteBuffer = ByteBuffer.allocateDirect(fileSize);    // :50 堆外分配
            final long address = PlatformDependent.directBufferAddress(byteBuffer);
            Pointer pointer = new Pointer(address);
            LibC.INSTANCE.mlock(pointer, new NativeLong(fileSize));         // :54 mlock 锁内存,防被 swap 出去
            availableBuffers.offer(byteBuffer);
        }
    }
    // borrowBuffer / returnBuffer ...
}
```

两个细节值得停一下:

- **`ByteBuffer.allocateDirect`**:堆外内存,不在 JVM 堆里,GC 管不到它(只有 DirectByteBuffer 对象本身受 GC,它指向的堆外内存要靠 `Cleaner` 或手动释放)。写消息往堆外写,没有 GC 压力。
- **`LibC.INSTANCE.mlock`**:这是 JNA 调 Linux 的 `mlock`,把这段堆外内存**锁在物理内存里,禁止被 swap 出去**。为什么?因为这块堆外内存是写消息的热路径,如果被 swap 到磁盘,那写消息又退化成写磁盘了——白费功夫。`mlock` 保证它永远在物理内存。

堆外模式开启后,`DefaultMappedFile.init` 会从池子借一个 `writeBuffer`([DefaultMappedFile.java:194](../rocketmq/store/src/main/java/org/apache/rocketmq/store/logfile/DefaultMappedFile.java#L194)):

```java
if (transientStorePool != null) {
    this.writeBuffer = transientStorePool.borrowBuffer();    // :194 借一个堆外 buffer
    this.transientStorePool = transientStorePool;
}
```

于是前台写消息时,`appendMessage` 实际往这块堆外 `writeBuffer` 写。看 `appendMessageBuffer()` 这个方法([DefaultMappedFile.java:423](../rocketmq/store/src/main/java/org/apache/rocketmq/store/logfile/DefaultMappedFile.java#L423)):

```java
protected ByteBuffer appendMessageBuffer() {
    this.mappedByteBufferAccessCountSinceLastSwap++;
    return writeBuffer != null ? writeBuffer : this.mappedByteBuffer;   // :425 有 writeBuffer 用它,否则用 mmap
}
```

**这一行是堆外模式的灵魂**:有 `writeBuffer` 就写堆外,否则写 `mappedByteBuffer`(页缓存)。前台写往哪写,就由这个三元运算决定。

> **钉死这件事(关键 nuance)**:堆外模式下,**写**走 `writeBuffer`(堆外 DirectByteBuffer),**读**仍走 `mappedByteBuffer`(页缓存)。这是 `DefaultMappedFile.appendMessageBuffer()`(:425)和 `selectMappedBuffer`(:667 用 `this.mappedByteBuffer.slice()`)各管一头的结果。也就是说——`writeBuffer` 和 `mappedByteBuffer` 是两块**不同的内存**,写进 writeBuffer 的数据,要等后台 `CommitRealTimeService` commit 进 `fileChannel`(= 进页缓存 = 进 mappedByteBuffer 对应的那块),读端才能从 mappedByteBuffer 切片读到。这就是为什么堆外模式必须多一步 commit(P1-04 详讲):commit 把数据从 writeBuffer 搬进页缓存,读端才看得见。

### 8.5.3 为什么这样能避开页缓存锁竞争

读到这里你可能会问:**读端还是走 mappedByteBuffer(页缓存),那页缓存的锁竞争不是还在吗?**

答案是:**竞争被大幅缓解了,因为写这条最热的路径挪走了**。在写极密集的 MQ 场景里,写远比读频繁(典型几万 QPS 的写 vs 几千 QPS 的读),而且写是"前台阻塞"的(在 `putMessageLock` 锁内,慢不得),读是"后台异步"的(可以等)。把写从页缓存挪到堆外,意味着:

1. **前台写不再碰页缓存**——它在堆外 DirectByteBuffer 上写,这块内存是它独占的(每个 MappedFile 借一个 writeBuffer),没有别的线程碰,没有内核页缓存锁、没有 TLB 一致性维护。
2. **后台 commit 是批量的、定时的**(每 200ms,`CommitRealTimeService`)——它一次性把 writeBuffer 的内容写进 `fileChannel`(进页缓存),这个写虽然是页缓存操作,但频率低、批量大,页缓存锁竞争被摊薄。
3. **读端走 mappedByteBuffer(页缓存)**,因为读本来就不密集,页缓存锁竞争不严重;而且读走 mmap 切片才能零拷贝送 socket(8.4.3)。

合起来:堆外模式把"写这条最热、最不能等的路径"从共享页缓存里救出来,挪到独占堆外内存;读这条不那么热、且必须走页缓存(为了零拷贝)的路径,仍留在页缓存。**两条路径各走各的快车道,写读不再在页缓存这一层抢。**代价就是多一步 commit——但 commit 是后台的、批量的,对前台写没影响。

> **不这样会怎样**:如果不开堆外模式,在写极密集(比如单 broker 几十万 QPS 写)的场景下,前台写 `mappedByteBuffer` + 后台刷盘 force + 消费读 sendfile,三股力量都碰页缓存。前台写在 `putMessageLock` 锁内,被页缓存锁拖慢一点,整个写吞吐就受影响。堆外模式把前台写挪走,让锁内的写不碰页缓存——这是它在高并发下的核心收益。当然,代价是多一步 commit(延迟略升)、堆外内存占用(每个 writeBuffer 1GB × 池大小 5 = 5GB 堆外)、实现复杂。所以默认不开(`transientStorePoolEnable=false`),适合对写吞吐极致敏感的场景。

### 8.5.4 堆外模式的开关:不只是个布尔值

最后说一个容易踩的坑。`transientStorePoolEnable` 这个开关,不是"配成 true 就生效"那么简单。看 `DefaultMessageStore.isTransientStorePoolEnable`([DefaultMessageStore.java:3217](../rocketmq/store/src/main/java/org/apache/rocketmq/store/DefaultMessageStore.java#L3217)):

```java
public boolean isTransientStorePoolEnable() {
    return this.messageStoreConfig.isTransientStorePoolEnable() &&
        (this.brokerConfig.isEnableControllerMode() || this.messageStoreConfig.getBrokerRole() != BrokerRole.SLAVE)
        && !messageStoreConfig.isWriteWithoutMmap();
}
```

三个条件都得满足,堆外模式才真生效:

1. `messageStoreConfig.isTransientStorePoolEnable()` 为 true(配置开关)。
2. **broker 是 master 或开了 ControllerMode**——slave 默认不开堆外模式。为什么?因为 slave 主要职责是接收 master 的复制数据写本地,它的写不是前台高并发场景,且 slave 的性能瓶颈通常不在这。让 slave 用更简单的默认模式(直接 mmap 写),减少复杂度。
3. **没有开 `writeWithoutMmap`**——这是另一种特殊写模式(走 `FileChannel.write` + 共享 ByteBuffer,完全不用 mmap),和堆外模式互斥。

> **钉死这件事**:堆外模式真正生效要满足三个条件:配置开关为 true、broker 是 master(或开了 ControllerMode)、没有开 writeWithoutMmap。slave 默认不开——这是因为 slave 的写场景不一样,且 slave 性能瓶颈不在此。这个细节是 Grep 源码时核对出来的,容易踩"配了 true 却不生效"的坑。

---

## 8.6 三条路径合起来:一张全景图

把本章三条路径画在一张图里,看清它们怎么分工:

```
                          ┌──────────── 写路径(8.3 + 8.5)────────────┐
                          │                                            │
  Producer send           ▼                                            │
       │                                                             │
       ▼                                                             │
  SendMessageProcessor                                               │
       │                                                             │
       ▼                                                             │
  CommitLog.asyncPutMessage(putMessageLock 锁内)                     │
       │                                                             │
       ▼                                                             │
  mappedFile.appendMessage ──→ appendMessageBuffer()                 │
       │                          │                                  │
       │           ┌──────────────┴───────────────┐                  │
       │           ▼                              ▼                  │
       │  默认模式:写 mappedByteBuffer      堆外模式:写 writeBuffer │
       │  (= 直接写页缓存)                 (= 写堆外 DirectByteBuffer,│
       │                                   mlock 锁物理内存)         │
       │           │                              │                  │
       │           │                              │ 后台 CommitRealTimeService
       │           │                              │ 每 200ms:         │
       │           │                              │ fileChannel.write│
       │           │                              │ (writeBuffer→页缓存)
       │           │                              │  ↓               │
       │           │                              │ wakeUpFlush()    │
       │           │                              ▼                  │
       │           └─────────────→ 页缓存(mappedByteBuffer 映射那块)│
       │                                  │                           │
       │                                  │ 后台 FlushRealTimeService │
       │                                  │ / GroupCommitService force│
       │                                  ▼                           │
       │                              磁盘(真正落盘)                 │
       └────────────────────────────────────────────────────────────┘

                          ┌──────────── 读路径(8.4)─────────────────┐
                          │                                            │
  Consumer pull           ▼                                            │
       │                                                             │
       ▼                                                             │
  PullMessageProcessor → ConsumeQueue 查物理偏移 → CommitLog.getData  │
       │                                                             │
       ▼                                                             │
  mappedFile.selectMappedBuffer(pos)                                 │
       │                                                             │
       ▼                                                             │
  mappedByteBuffer.slice()  ← 切片,共享页缓存内存,不拷贝!          │
       │                                                             │
       ▼                                                             │
  SelectMappedBufferResult(byteBuffer = mmap 视图)                   │
       │                                                             │
       ▼                                                             │
  ManyMessageTransfer(header, GetMessageResult) —— FileRegion        │
       │                                                             │
       ▼                                                             │
  channel.writeAndFlush(fileRegion)                                  │
       │                                                             │
       ▼                                                             │
  target.write(bb) —— bb 是堆外(页缓存)ByteBuffer                  │
       │                                                             │
       ▼                                                             │
  socket 发送缓冲 → 网卡 → Consumer                                  │
       │                                                             │
       │  全程:消息体不进 JVM 堆,零 GC 压力,零用户态拷贝           │
       └────────────────────────────────────────────────────────────┘
```

记住这张图的三个分叉:

1. **写路径的两条路**:默认写 `mappedByteBuffer`(直写页缓存),堆外模式写 `writeBuffer`(堆外,再 commit 进页缓存)。这两条路最终都汇入页缓存,只是写热路径的位置不同。
2. **读路径永远走 `mappedByteBuffer.slice()`**:无论写端是哪种模式,读端都从页缓存切片——因为读要走零拷贝送 socket,而零拷贝的根基就是"数据在页缓存里"。
3. **页缓存是写读的交汇点**:写把数据搬进页缓存(直写或 commit),读从页缓存切片送 socket。堆外模式的价值是把"写的热路径"从交汇点挪开,让写读在交汇点上的竞争最小化。

---

## 8.7 技巧精解:mmap vs sendfile 分工 + 堆外池让写读各走快车道

本章最硬核的两个技巧,在这一节单独拆透。

### 8.7.1 技巧一:mmap 写 + sendfile 读的分工,凭什么这么分

RocketMQ 的零拷贝,不是"全程一种零拷贝技术",而是**写用 mmap、读用 sendfile,两条路径各用各的**。这个分工不是随意的,而是 MQ 业务场景的必然。

**写为什么必须 mmap、不能用 sendfile?** 因为写消息时,broker 要**看得见字节、改得了字节**——填 `QUEUEOFFSET`、`PHYSICALOFFSET`、`STORETIMESTAMP`,算 `BODYCRC`。这些"算 + 填"必须在用户态完成。sendfile 是"我只转发、不加工"的接口,它连字节都看不见,填不了字段。mmap 让用户态虚拟地址直接映射页缓存,broker 既看得见字节(能改)、又省了"页缓存→用户态"那次拷贝——这是写场景的最佳解。

**读为什么必须 sendfile(或 FileRegion)、不能用 mmap?** 严格说,读也可以 mmap(8.4.3 的切片就是 mmap 视图),但"读到 socket 发出去"这一步,如果还用 mmap + 手动 write,会多一次"用户态 buffer → socket"的拷贝。sendfile/FileRegion 把这一步在内核里完成,数据不进用户态——这才是读场景的最佳解。读时 broker 对消息字节没有任何加工(只是转发),正适合"不加工、只搬运"的 sendfile。

> **反面对比**:假设读也用 mmap + write(传统方式),会怎样?消费者 pull 1000 条消息,broker 要把 1000 段字节从页缓存读进 JVM 堆(一次 CPU 拷贝),再 write 到 socket(又一次 CPU 拷贝),还在堆里制造 1000 个临时 byte[]。单机几万 QPS 的 pull 量,光这些拷贝就把 CPU 和 GC 吃光。而 FileRegion + mmap 切片,这 1000 段字节自始至终待在页缓存(堆外),Netty 把它们零拷贝写给 socket——CPU 不用搬运、GC 不用扫、内存带宽不浪费。这就是分工的收益。

**这个分工的深层洞察**:mmap 解决"用户态怎么访问文件"(看得见、改得了),sendfile 解决"文件怎么进 socket"(不进用户态、只转发)。一个面向"加工",一个面向"转发"。MQ 场景写要加工、读只转发,所以 mmap 配写、sendfile 配读,是天然契合。Kafka 也是这个分工(Kafka 用 `FileRecords.writeTo` 走 sendfile、写用 mmap),这不是巧合,而是 Linux 零拷贝语义和 MQ 业务场景的必然对齐。

### 8.7.2 技巧二:堆外池让写读在页缓存这一层各走快车道

第二个硬核技巧是 `TransientStorePool`。它解决的不是"单次写或读的拷贝次数",而是**"多核高并发下,写读两条路径在页缓存这一层的锁竞争"**。

**问题是什么?** 默认模式下,写、刷盘、读三股力量都碰页缓存:

- 前台写:在 `putMessageLock` 锁内写 `mappedByteBuffer`(页缓存)。
- 后台刷盘:`mappedByteBuffer.force()` 遍历脏页(页缓存)。
- 消费读:`mappedByteBuffer.slice()`(页缓存)。

写是前台阻塞的(在锁内,慢不得),被页缓存锁拖慢一点,整个写吞吐就受影响。在写极密集的场景下,这是隐形天花板。

**堆外池用了什么手段?** 把前台写这条最热、最不能等的路径,从页缓存挪到独占的堆外 DirectByteBuffer。具体机制是 `DefaultMappedFile` 持有两块内存:

- `writeBuffer`(堆外 DirectByteBuffer,仅堆外模式有):前台写往这里写。这块内存是每个 MappedFile 独占的(从池子借),没有别的线程碰,没有内核页缓存锁、没有 TLB 一致性维护。`mlock` 还把它锁在物理内存,连 swap 都不会。
- `mappedByteBuffer`(页缓存,mmap 映射):读端从这切片(零拷贝送 socket)。后台 commit 把 writeBuffer 的数据搬进 `fileChannel`(= 进这块页缓存)。

这样,**写读不再在页缓存这一层抢**——写在堆外,读在页缓存,各走各的。后台 commit 是批量的、定时的(每 200ms),虽然它也碰页缓存,但频率低、批量大,竞争被摊薄。

> **反面对比(朴素方案会撞什么墙)**:假设不开堆外模式,在单 broker 几十万 QPS 写的场景下,前台写 `mappedByteBuffer` 被页缓存锁拖慢。`putMessageLock` 是全局串行的(P1-03),锁内每慢 1 微秒,前台写吞吐就掉一截。堆外模式把锁内的写挪到独占堆外内存,锁内不再碰页缓存——锁内耗时下降,写吞吐上升。代价是多一步 commit(延迟略升)、堆外内存占用(5GB)、实现复杂。这个交易在"写极密集、对吞吐极致敏感"的场景下是划算的,在普通场景不划算(所以默认不开)。

**这个技巧的深层洞察**:它和 P1-04 的 commit/flush 两步、P1-03 的 `putMessageLock` 三选一,是同一组优化思想的不同侧面——**RocketMQ 永远在把"前台不能等的快路径"和"后台可以等的慢路径"分开**。写路径要快(前台阻塞),所以挪到堆外独占内存;commit/flush 可以慢(后台异步),所以留它在页缓存;锁内要快(物理追加),所以用最省的锁;锁外可以慢(queueOffset 推进),所以放锁外。这种"前台快、脏活甩后台"的哲学,在 RocketMQ 存储内核里反复出现,是它高吞吐的根本。

---

## 8.8 一个容易被忽略的真相:`transferMsgByHeap` 默认是 true

讲完零拷贝的精妙,本章必须诚实地点出一个源码事实——**RocketMQ 默认的读路径,其实不是零拷贝的 FileRegion 路径,而是堆拷贝路径**。

看 `DefaultPullMessageResultHandler` 处理 pull 结果时的分支([DefaultPullMessageResultHandler.java:140](../rocketmq/broker/src/main/java/org/apache/rocketmq/broker/processor/DefaultPullMessageResultHandler.java#L140)):

```java
if (this.brokerController.getBrokerConfig().isTransferMsgByHeap()) {
    final byte[] r = this.readGetMessageResult(getMessageResult, requestHeader.getConsumerGroup(),
        requestHeader.getTopic(), requestHeader.getQueueId());                    // :141 堆拷贝
    // ...
    response.setBody(r);                                                          // :145 塞进 response body
    return response;
} else {
    try {
        FileRegion fileRegion =
            new ManyMessageTransfer(response.encodeHeader(getMessageResult.getBufferTotalSize()), getMessageResult);   // :150 零拷贝
        // ...
        channel.writeAndFlush(fileRegion)                                         // :152
            .addListener(...);
    }
    return null;
}
```

这个分支的开关是 `transferMsgByHeap`,它的默认值在 [common/BrokerConfig.java:134](../rocketmq/common/src/main/java/org/apache/rocketmq/common/BrokerConfig.java#L134):

```java
private boolean transferMsgByHeap = true;   // :134 默认 true!
```

**默认 `true`**,也就是默认走 `readGetMessageResult` 那条堆拷贝路径,而不是 FileRegion 零拷贝路径。看堆拷贝路径干了什么([DefaultPullMessageResultHandler.java:237](../rocketmq/broker/src/main/java/org/apache/rocketmq/broker/processor/DefaultPullMessageResultHandler.java#L237)):

```java
protected byte[] readGetMessageResult(final GetMessageResult getMessageResult, final String group,
    final String topic, final int queueId) {
    final ByteBuffer byteBuffer = ByteBuffer.allocate(getMessageResult.getBufferTotalSize());   // :240 在 JVM 堆里分配!
    // ...
    List<ByteBuffer> messageBufferList = getMessageResult.getMessageBufferList();
    for (ByteBuffer bb : messageBufferList) {
        byteBuffer.put(bb);                                                                     // :247 把堆外 bb 拷进堆内 byteBuffer
        // ...
    }
    // ...
    return byteBuffer.array();                                                                  // :269 返回堆内 byte[]
}
```

`ByteBuffer.allocate(...)` 在 JVM 堆里分配,`byteBuffer.put(bb)` 把堆外的 `mappedByteBuffer` 切片**拷贝**进堆——这一次拷贝,正是零拷贝想避免的。然后 `response.setBody(r)` 把这个 byte[] 塞进 `RemotingCommand`,走正常的 Netty 编码路径(还要再从堆 byte[] 拷进 Netty 的堆外 ByteBuf 发出去)。

> **为什么默认走堆拷贝?** 因为零拷贝路径有个代价:**它绕过了 `RemotingCommand` 的正常编码流程**。走 FileRegion,broker 直接 `channel.writeAndFlush(fileRegion)`,header(`response.encodeHeader`)和 body(消息体)是分开写的——header 一个 ByteBuffer,消息体若干 ByteBuffer,`ManyMessageTransfer.transferTo` 里挨个 `target.write`。这意味着响应不是个完整的 `RemotingCommand`,client 端的解码也要特殊处理(读 header 再拼 body)。而堆拷贝路径把消息体塞进 `response.setBody`,走标准 `RemotingCommand` 编码,client 端标准解码——简单、通用、不易出错。RocketMQ 默认选简单可靠,把零拷贝作为可选优化(配 `transferMsgByHeap=false` 才开),这是个务实的取舍。

> **钉死这件事(修正一个常见误解)**:很多讲 RocketMQ 零拷贝的材料,会把它说成"默认用 sendfile 零拷贝读"。**这与源码不符**。`transferMsgByHeap` 默认 `true`(common/BrokerConfig.java:134),默认走的是 `readGetMessageResult` 的堆拷贝路径,消息体会被拷进 JVM 堆。零拷贝(FileRegion)路径要配 `transferMsgByHeap=false` 才启用,适合对读吞吐极致敏感的场景。同样,`transientStorePoolEnable` 默认 `false`(MessageStoreConfig.java:266),堆外内存池也要显式开。**零拷贝在 RocketMQ 里是可选的性能优化,不是默认行为**——这是写作时 Grep 源码修正的一处关键事实。

---

## 章末小结

这一章讲的是 RocketMQ 存储内核到网络的**出口**——消息字节从 CommitLog 的页缓存送到消费者的网卡。我们立起了几个关键事实:

1. **传统 IO 有四次拷贝**:磁盘→页缓存→用户态 buffer→socket 发送缓冲→网卡,外加两次系统调用和 JVM 堆的 GC 压力。这是零拷贝要清零的冗余。
2. **mmap 写省一次拷贝**:`MappedByteBuffer` 把文件映射进虚拟地址,用户态写这段地址即写页缓存,省"页缓存→用户态"那次拷贝。RocketMQ 写用 mmap,因为写要看得见字节、改得了字段。
3. **sendfile 读几乎全省**:`FileRegion`(Netty 对 sendfile 的封装)让数据在内核里直接从页缓存到 socket,用户态线程不碰数据,数据不进 JVM 堆。RocketMQ 读用 sendfile,因为读只是转发、不加工。读路径的零拷贝真身是 `mappedByteBuffer.slice()`——切出页缓存的视图,共享内存、不拷贝。
4. **mmap 与 sendfile 的分工是业务场景的必然**:写要加工(填字段、算 CRC),用看得见字节的 mmap;读只转发,用不碰数据的 sendfile。
5. **堆外内存池 `TransientStorePool` 让写读各走快车道**:把前台写这条最热的路径,从共享页缓存挪到独占堆外 DirectByteBuffer(`mlock` 锁物理内存),避开高并发下页缓存的锁竞争;读仍走 mmap 切片(必须走页缓存才能零拷贝送 socket)。代价是多一步 commit(P1-04 讲过)。
6. **诚实修正**:`transferMsgByHeap` 默认 `true`(默认走堆拷贝)、`transientStorePoolEnable` 默认 `false`。零拷贝在 RocketMQ 里是可选优化,不是默认行为——这是个务实的取舍(简单可靠优先,极致性能可配)。

### 二分法归属

这一章是**存储内核到分布式骨架的衔接点**——技术上它横跨 store 模块(`MappedFile`、`TransientStorePool`、`SelectMappedBufferResult`)和 broker 模块(`DefaultPullMessageResultHandler`、`pagecache/` 三个 transfer 类),语义上它服务于"消息怎么从存储内核高效地送出去给消费者"。它和第 2 篇前两章(P2-06 ConsumeQueue、P2-07 IndexFile)合起来,完成了"消费端怎么从混写的 CommitLog 高效取出消息"的全部故事——前两章解决"怎么定位",这一章解决"怎么送出去"。下一章(P3-09)就开始讲消费端本身:Push 为什么本质是 Pull 长轮询。

### 五个"为什么"清单

1. **为什么传统 IO 慢?** 磁盘→页缓存→用户态 buffer→socket 缓冲→网卡四次拷贝(其中两次 CPU 拷贝),外加两次系统调用、用户态/内核态切换,还有 JVM 堆里的 GC 压力。MQ 高吞吐下,这几笔冗余累加成 CPU 和内存带宽的瓶颈。
2. **为什么 RocketMQ 写用 mmap、读用 sendfile?** 写要加工字节(填 QUEUEOFFSET/PHYSICALOFFSET/STORETIMESTAMP、算 CRC),必须看得见、改得了——mmap 让用户态虚拟地址直接映射页缓存,既看得见又省一次拷贝。读只是转发(消息字节已成形,broker 不加工),用不碰数据的 sendfile 让数据在内核里直接进 socket、不进 JVM 堆。这是"加工 vs 转发"业务场景的天然分工。
3. **读路径的"零拷贝"真身是什么?** 是 `DefaultMappedFile.selectMappedBuffer`([:667](../rocketmq/store/src/main/java/org/apache/rocketmq/store/logfile/DefaultMappedFile.java#L667))里的 `this.mappedByteBuffer.slice()`——它切出 CommitLog 页缓存的视图,共享内存、不拷贝。这个视图一路被 `ManyMessageTransfer`(FileRegion)写给 Netty channel,消息体全程不进 JVM 堆。
4. **堆外内存池 `TransientStorePool` 解决什么问题?** 解决高并发下写、刷盘、读三股力量都碰页缓存导致的锁竞争。它把前台写这条最热、最不能等的路径,从共享页缓存挪到独占堆外 DirectByteBuffer(`mlock` 锁物理内存),让写读在页缓存这一层各走各的快车道。代价是多一步 commit(P1-04 讲过)把堆外数据搬进页缓存,读端才能看见。
5. **零拷贝是 RocketMQ 的默认行为吗?** **不是。** `transferMsgByHeap` 默认 `true`(common/BrokerConfig.java:134),默认走 `readGetMessageResult` 堆拷贝路径(消息体拷进 JVM 堆);`transientStorePoolEnable` 默认 `false`(MessageStoreConfig.java:266),堆外池要显式开。零拷贝是可选的性能优化——默认选简单可靠的 `RemotingCommand` 标准编码,极致性能场景才配 `transferMsgByHeap=false` 启用 FileRegion。

### 想继续深入往哪钻

- **本章核心源码(store 端)**:[DefaultMappedFile.java](../rocketmq/store/src/main/java/org/apache/rocketmq/store/logfile/DefaultMappedFile.java) 的 `selectMappedBuffer`(:667)、`appendMessageBuffer`(:423)、`getFileChannel`(:288)、`init`(:189 借 writeBuffer);[SelectMappedBufferResult.java](../rocketmq/store/src/main/java/org/apache/rocketmq/store/SelectMappedBufferResult.java)(切片结果,持 `mappedByteBuffer` 视图);[TransientStorePool.java](../rocketmq/store/src/main/java/org/apache/rocketmq/store/TransientStorePool.java)(堆外池,`allocateDirect` + `mlock`)。
- **本章核心源码(broker 端)**:[DefaultPullMessageResultHandler.java](../rocketmq/broker/src/main/java/org/apache/rocketmq/broker/processor/DefaultPullMessageResultHandler.java) 的 `transferMsgByHeap` 分支(:140)、`readGetMessageResult`(:237);`broker/pagecache/` 三个 transfer 类([ManyMessageTransfer](../rocketmq/broker/src/main/java/org/apache/rocketmq/broker/pagecache/ManyMessageTransfer.java)、[OneMessageTransfer](../rocketmq/broker/src/main/java/org/apache/rocketmq/broker/pagecache/OneMessageTransfer.java)、[QueryMessageTransfer](../rocketmq/broker/src/main/java/org/apache/rocketmq/broker/pagecache/QueryMessageTransfer.java))都实现 Netty `FileRegion`。配 `transferMsgByHeap=false` 时,PopMessageProcessor(:468)、PeekMessageProcessor(:202)、QueryMessageProcessor(:113/:159)都走 FileRegion 零拷贝。
- **mmap 与 sendfile 的内核机制**:`mmap` 系统调用、页表映射、`sendfile64` 系统调用、Linux 的 zero-copy(`sendfile`/`splice`/`tee`/`sendpage`)对比。推荐读 Linux man page 和《Linux 内核设计》相关章节。注意 Java 的 `FileChannel.transferTo` 在 Linux 上走 `sendfile64`,但 RocketMQ 读路径用的是 Netty `FileRegion` + 堆外 ByteBuffer,具体能否进一步走 sendfile 取决于 Netty channel 实现——这是个层次问题,别笼统说"零 CPU 拷贝"。
- **堆外内存与 GC**:`DirectByteBuffer` 不受 GC 直接管理(靠 `Cleaner` 间接释放),`mlock` 锁物理内存防 swap。这部分和《Tokio》的堆外/`Pin`、LevelDB 的 arena 是同源思想——把热路径挪出 GC 管理的内存。
- **延伸到 Kafka**:Kafka 也是"写 mmap + 读 sendfile"的分工(Kafka 的 `FileRecords.writeTo` 走 `FileChannel.transferTo` = sendfile,写用 mmap 映射 log segment)。这是 Linux 零拷贝语义在 MQ 场景的通用解,不是 RocketMQ 独创。但 RocketMQ 的 `TransientStorePool` 堆外池是它相对 Kafka 的一个特色——把写热路径从页缓存挪到堆外独占内存,这是 RocketMQ 在"所有消息混写一个 CommitLog、写串行化"这个特殊架构下做的针对性优化。
- **延伸到 5.x 分级存储**:5.x 的 `TieredStore` 把冷数据下沉到对象存储,读冷数据时不再走 CommitLog 的页缓存零拷贝,而是从远端对象存储拉——零拷贝路径只对热数据有效。这部分在 P8-23 详讲。

### 引出下一章

存储内核的故事到这里收束了——第 1 篇讲写(P1-02 编码 → P1-03 CommitLog → P1-04 刷盘 → P1-05 Reput),第 2 篇讲读(P2-06 ConsumeQueue → P2-07 IndexFile → P2-08 零拷贝)。一条消息怎么进 CommitLog、怎么被异步分发成索引、怎么被高效读出来送网卡,这条链走完了。但"broker 把消息发出去"只是一半——**消费者那边怎么收、怎么决定从哪个 queue 拉、拉不到时怎么办**?下一章(P3-09 消费模型)讲这个:用户调的是 `DefaultMQPushConsumer`(push 语义),底层却是 `PullMessageService` 单线程不停拉(本质 Pull);没有消息时 broker 端 `PullRequestHoldService` 把请求挂起(长轮询),消息到达由 `NotifyMessageArrivingListener` 唤醒返回。**Push 本质是 Pull**——这是消费端的第一个反直觉,也是第 3 篇的开端。
