# 第二十三章 · 5.x 新架构:Proxy 与 gRPC、RocksDB 存储、分级存储

> 篇:P8 · 5.x 新架构
> 主线呼应:前 7 篇我们走完了"一条消息在经典架构里的一生"——CommitLog 混写、ConsumeQueue 重建逻辑队列、零拷贝、Remoting、NameServer、HA、顺序/延时/事务。这一章换一个视角:**RocketMQ 5.x 在这套已经打磨了十年的经典架构之上,又叠加了哪些新东西?** 三个方向——**Proxy**(gRPC 新接入层)、**RocksDB 存储**(用 LSM 替掉 ConsumeQueue 的文件数组)、**TieredStore 分级存储**(冷数据下沉到对象存储)。每一样都不是推翻经典,而是在经典架构"够用但不够好"的边界上做的针对性补强。读完这一章,你会看到"混写一个 CommitLog"这条主线在 5.x 时代如何延续,以及它在哪里被悄悄换了骨头。

## 核心问题

**5.x 在经典架构之上引入 Proxy、RocksDB 存储、分级存储,这三样各自要补经典架构的什么短板?凭什么要这样补,而不是另起炉灶?**

读完本章你会明白:

1. **Proxy 是什么、为什么 5.x 要在 Remoting 之外再叠一层 gRPC 接入**:多语言友好、协议更清晰、计算与存储可分离(可独立扩缩),以及它和经典 Remoting 是**并存**而非取代。
2. **RocksDB 存储到底替了经典架构的哪一块**(诚实标注:只替了 ConsumeQueue,CommitLog 仍是经典 mmap),**凭什么在"海量 Topic/Queue"场景 LSM 反而比"每 queue 一个文件"更优**——这条直接呼应《LevelDB》的 LSM 与写放大。
3. **分级存储怎么做到"冷数据下沉到对象存储、热路径性能不降"**:装饰器模式套在底层 store 外面,只有"本地没有"的消息才走远程拉,本地有的直接走经典热路径。

> **如果一读觉得太难**:先只记住三件事——① Proxy 是 gRPC 新接入层,和 Remoting 并存,Local 模式嵌 Broker、Cluster 模式独立部署;② RocksDB 在 RocketMQ 里**只替了 ConsumeQueue**(把"每 queue 一个文件"换成"所有 queue 的索引塞进一个 LSM"),CommitLog 没动;③ 分级存储是个装饰器,套在原 store 外,冷数据下沉对象存储,热数据还在本地 CommitLog。

---

## 23.1 一句话点破

> **5.x 没有推翻经典架构,而是在它的三个"够用但不够好"的边界上做了补强:Remoting 协议对多语言不友好,于是叠一层 Proxy 用 gRPC 把接入层做干净;经典 ConsumeQueue 是"每 queue 一个文件",百万 Queue 时文件数爆炸,于是用 RocksDB 的 LSM 把所有 queue 的索引收敛进一个 KV 存储;本地 CommitLog 只能存热数据(冷数据占盘成本高),于是套一层 TieredStore 把冷数据异步下沉到对象存储。三样东西都是"在经典架构的瓶颈上做加法",写路径的灵魂——混写一个 CommitLog 换纯顺序写——原封不动。**

这是结论。本章倒过来拆:先看 Proxy 在补什么、怎么补;再看 RocksDB 存储到底替了什么(并诚实修正一个常见的误解——它替的是 ConsumeQueue 不是 CommitLog);最后看分级存储怎么做到冷热分离又不拖慢热路径。

---

## 23.2 Proxy:在 Remoting 之外叠一层 gRPC 接入

### 23.2.1 经典 Remoting 的痛点

第 4 篇讲透了 RocketMQ 自研的 `RemotingCommand` 协议:所有请求响应都是一个 `RemotingCommand`,wire 格式 `[4字节 totalLength][4字节 headerLength(高1位塞 SerializeType)][header][body]`,header 用 Java 的 `CommandCustomHeader` + 反射互转。这套协议在 Java 生态里跑得很顺,但出了 Java 就尴尬了:

- **协议绑定 Java**:`CommandCustomHeader` 是 Java 类,header 字段的序列化靠 RocketMQ 自定义的 `RocketMQSerializable`(以及 JSON 兜底)。C++/Go/Python/Rust 的客户端要接入,得重新实现这套协议——而且 `RequestCode` 是个整数枚举,字段全塞在 `extFields` 这个 `HashMap<String, String>` 里,跨语言对接又啰嗦又易错。
- **协议不清晰**:客户端和 Broker 之间的契约藏在代码里(每个 `RequestCode` 对应一个 `CommandCustomHeader` 子类),没有独立的 IDL(接口描述语言),新接入的人得翻 Java 源码才能搞清楚"发一条消息到底要塞哪些字段"。
- **计算与存储耦合**:经典架构里 Broker 既做接入(Netty Remoting)又做存储(CommitLog),接入层和存储层共享进程、共享 JVM。在云原生场景,接入层(无状态、可水平扩)和存储层(有状态、要稳定)的扩缩节奏完全不同——双十一流量来了,你想先把接入层扩 10 倍扛量、存储层不动,经典 Broker 做不到。

### 23.2.2 Proxy 的答案:gRPC + 双模式

5.x 的 `proxy` 模块给出答案:**在 Remoting 之外,再叠一层基于 gRPC 的接入层,叫 Proxy。** 它解决上面三个痛点:

1. **gRPC 是跨语言的**:用 Protocol Buffers 做 IDL,官方给几乎所有主流语言都生成了 stub。C++/Go/Python/Rust 客户端不再要自己实现 `RemotingCommand` 协议,直接用 gRPC stub 调用。
2. **协议清晰**:RocketMQ 5.x 定义了一套独立的 `.proto`(MessagingService / Producer / Consumer 等),所有请求响应的字段、类型都在 IDL 里写死,契约一目了然。
3. **接入与存储可分离**:Proxy 可以独立于 Broker 部署,做一个纯接入层,后面挂多个 Broker。

但 RocketMQ 团队做了一个克制的决定:**Proxy 和经典 Remoting 并存,不是取代。** 看 `ProxyStartup`([ProxyStartup.java:78-99](../rocketmq/proxy/src/main/java/org/apache/rocketmq/proxy/ProxyStartup.java#L78-L99))——它**同时**启动了一个 gRPC server 和一个 Remoting server:

```java
MessagingProcessor messagingProcessor = createMessagingProcessor();        // :78
// ... create grpcServer
GrpcServer grpcServer = GrpcServerBuilder.newBuilder(executor,             // :85
        ConfigurationManager.getProxyConfig().getGrpcServerPort(), tlsCertificateManager)
    // ... build
// ...
RemotingProtocolServer remotingServer = new RemotingProtocolServer(messagingProcessor, tlsCertificateManager);  // :95
PROXY_START_AND_SHUTDOWN.appendStartAndShutdown(grpcServer);               // :93
PROXY_START_AND_SHUTDOWN.appendStartAndShutdown(remotingServer);           // :96
PROXY_START_AND_SHUTDOWN.start();                                          // :99
```

> **钉死这件事**:Proxy 不是"用 gRPC 替掉 Remoting",而是"在 Remoting 旁边再加一个 gRPC 入口"。同一个 `MessagingProcessor` 同时被 gRPC 和 Remoting 两条入口复用——老 Java 客户端走 Remoting 不用改,新多语言客户端走 gRPC,两条路最终汇到同一套业务处理逻辑。这是对存量生态的尊重。

克制的另一面是**双模式**。Proxy 有两种部署形态([ProxyMode.java:20-23](../rocketmq/proxy/src/main/java/org/apache/rocketmq/proxy/ProxyMode.java#L20-L23)):

```java
public enum ProxyMode {
    LOCAL("LOCAL"),
    CLUSTER("CLUSTER");
    // ...
}
```

- **Local 模式**:Proxy 进程**内嵌**一个 `BrokerController`,接入和存储跑在同一个 JVM 里(和经典 Broker 一样,只是多了个 gRPC 入口)。这是给"想用 gRPC 但不想拆架构"的用户的平滑过渡路径。
- **Cluster 模式**:Proxy 进程**不内嵌** Broker,纯做接入转发,后面挂独立的 Broker 集群。这才是"计算与存储分离"的形态——接入层可独立扩缩。

切换哪个模式,看 `createMessagingProcessor`([ProxyStartup.java:173-205](../rocketmq/proxy/src/main/java/org/apache/rocketmq/proxy/ProxyStartup.java#L173-L205)):

```java
if (ProxyMode.isClusterMode(proxyModeStr)) {                            // :177
    messagingProcessor = DefaultMessagingProcessor.createForClusterMode();   // 纯接入,不建 BrokerController
    // ...
} else if (ProxyMode.isLocalMode(proxyModeStr)) {                       // :181
    BrokerController brokerController = createBrokerController();           // 内嵌一个完整 Broker
    // ... 把 brokerController.start() 包进 StartAndShutdown
    messagingProcessor = DefaultMessagingProcessor.createForLocalMode(brokerController);  // :202
}
```

> **不这样会怎样**:假设 5.x 强行只留 Cluster 模式、逼所有用户拆成 Proxy + Broker 两层,那存量用户从 4.x 升级 5.x 的迁移成本会极高(运维要重新搭一套接入层)。Local 模式给了一条"原地升级、先享受 gRPC,以后再拆"的平滑路——这是工程上对存量生态的尊重,和 etcd 升级 Raft 时保留旧 API 一个道理。

两种模式的拓扑对照如下:

```
Local 模式(接入+存储同进程,平滑过渡):
┌─────────────────────────────────────────┐
│  Proxy 进程(JVM)                       │
│  ┌──────────┐  ┌──────────────────────┐ │
│  │ gRPC     │  │ Remoting 入口        │ │
│  │ 入口     │  │(Netty)              │ │
│  └────┬─────┘  └─────────┬────────────┘ │
│       └──────┬───────────┘              │
│         MessagingProcessor               │
│              │                          │
│         BrokerController(内嵌)          │
│              │ CommitLog/ConsumeQueue    │
└──────────────┼───────────────────────────┘

Cluster 模式(接入与存储分离,可独立扩缩):
┌──────────────────┐         ┌──────────────────┐
│ Proxy 进程 1     │         │ Broker 1         │
│ gRPC + Remoting  │ ─ ─ ─ ─ │ CommitLog/CQ     │
│ 纯接入,无状态   │         │ 有状态           │
└──────────────────┘         └──────────────────┘
┌──────────────────┐         ┌──────────────────┐
│ Proxy 进程 2     │ ─ ─ ─ ─ │ Broker 2         │
│ (水平扩)        │         │                  │
└──────────────────┘         └──────────────────┘
```

### 23.2.3 gRPC 的处理链:拦截器 + Pipeline

Proxy 的 gRPC 这条路,在请求到达业务逻辑之前,经过一条**拦截器链 + Pipeline**。`GrpcServerBuilder`([GrpcServerBuilder.java:60-90](../rocketmq/proxy/src/main/java/org/apache/rocketmq/proxy/grpc/GrpcServerBuilder.java#L60-L90))把多个 interceptor 注册到 gRPC server:

```java
serverBuilder.protocolNegotiator(new ProxyAndTlsProtocolNegotiator());          // Proxy 协议嗅探 + TLS
int bossLoopNum = config.getGrpcBossLoopNum();       // GrpcServerBuilder.java:63
int workerLoopNum = config.getGrpcWorkerLoopNum();   // :64
if (config.isEnableGrpcEpoll()) {
    serverBuilder.bossEventLoopGroup(new EpollEventLoopGroup(bossLoopNum))
        .workerEventLoopGroup(new EpollEventLoopGroup(workerLoopNum))
        .channelType(EpollServerSocketChannel.class)
        .executor(executor);
} else {
    serverBuilder.bossEventLoopGroup(new NioEventLoopGroup(bossLoopNum))
        .workerEventLoopGroup(new NioEventLoopGroup(workerLoopNum))
        .channelType(NioServerSocketChannel.class)
        .executor(executor);
}
```

注意 gRPC server 自己也是一套 Netty 主从 Reactor(boss accept / worker IO / executor 业务),和第 13 章讲 Remoting 的 Netty 模型同构——只不过 gRPC 把"协议编解码 + 服务路由"包了一层,业务侧只看到 stub 方法调用。

请求进来后,业务侧是一条 `RequestPipeline`([RequestPipeline.java:30-43](../rocketmq/proxy/src/main/java/org/apache/rocketmq/proxy/grpc/pipeline/RequestPipeline.java#L30-L43)):

```java
public interface RequestPipeline {
    void execute(ProxyContext context, Metadata headers, GeneratedMessageV3 request);
    default RequestPipeline pipe(RequestPipeline source) {
        return (ctx, headers, request) -> {
            source.execute(ctx, headers, request);
            execute(ctx, headers, request);
        };
    }
}
```

它是一个**可拼接的责任链**——`pipe` 方法把两个 pipeline 串起来,前一个先执行。`ContextInitPipeline`(初始化上下文)→ `AuthenticationPipeline`(认证)→ `AuthorizationPipeline`(鉴权)依次 `pipe` 下去,最后到具体的 Activity(`ReceiveMessageActivity` / `SendMessageActivity`)。这套设计和经典 Remoting 那条"Handshake → Encoder → Decoder → IdleState → ConnectManage → ServerHandler"的 Netty pipeline 是一个思路——**横切关注点(认证、鉴权、上下文、限流)用责任链解耦,业务逻辑在链尾**。对比第 14 章 Remoting 的 `processorTable` 按 RequestCode 路由:Remoting 用"一个 Map 路由到不同 Processor",gRPC 这边用"一个 stub 方法对应一个 Activity + 共享 pipeline 做横切",殊途同归。

> **钉死这件事**:Proxy 的 gRPC 这条路,核心抽象是 `MessagingProcessor`([MessagingProcessor.java:51](../rocketmq/proxy/src/main/java/org/apache/rocketmq/proxy/processor/MessagingProcessor.java#L51))——一个接口,定义了 `sendMessage` / `receiveMessage` / `ackMessage` 等业务方法。gRPC 的 Activity 和 Remoting 的 Processor 都调到同一个 `MessagingProcessor`,这就是"双协议入口、单业务内核"的解耦点。换协议不动业务,换业务不动协议。

---

## 23.3 RocksDB 存储:把 ConsumeQueue 从"文件数组"换成"LSM"

这是本章最需要诚实的一节,因为有一个广泛流传的误解需要修正。

### 23.3.1 诚实标注:RocksDB 到底替了什么

总纲和一些 5.x 介绍里常说"RocksDB 存储替掉了 CommitLog + ConsumeQueue"。但落到源码,这个说法是不准确的。看 `RocksDBMessageStore` 的全部实现([RocksDBMessageStore.java:28-40](../rocketmq/store/src/main/java/org/apache/rocketmq/store/RocksDBMessageStore.java#L28-L40)):

```java
public class RocksDBMessageStore extends DefaultMessageStore {

    public RocksDBMessageStore(final MessageStoreConfig messageStoreConfig, final BrokerStatsManager brokerStatsManager,
        final MessageArrivingListener messageArrivingListener, final BrokerConfig brokerConfig,
        final ConcurrentMap<String, TopicConfig> topicConfigTable) throws IOException {
        super(messageStoreConfig, brokerStatsManager, messageArrivingListener, brokerConfig, topicConfigTable);
    }

    @Override
    public ConsumeQueueStoreInterface createConsumeQueueStore() {
        return new RocksDBConsumeQueueStore(this);              // :38 —— 只 override 这一个工厂方法
    }
}
```

**全部代码就这么多。** `RocksDBMessageStore` 继承 `DefaultMessageStore`,只 override 了 `createConsumeQueueStore()` 这一个工厂方法,返回 `RocksDBConsumeQueueStore`。也就是说:

> **RocksDB 在 RocketMQ 里,只替掉了 ConsumeQueue,CommitLog 原封不动仍是经典 mmap 的 `MappedFileQueue`。** 写路径(`CommitLog.asyncPutMessage`、`putMessageLock`、`beginTimeInLock`)一个字没改。第 1~3 章讲的那套"混写一个 CommitLog 换纯顺序写"的灵魂,在 RocksDB 存储模式下完全保留。

什么时候启用?看 `BrokerController.initializeMessageStore`([BrokerController.java:871-875](../rocketmq/broker/src/main/java/org/apache/rocketmq/broker/BrokerController.java#L871-L875)):

```java
DefaultMessageStore defaultMessageStore;
if (this.messageStoreConfig.isEnableRocksDBStore()) {           // :871 —— 一个配置开关
    defaultMessageStore = new RocksDBMessageStore(...);          // :874 —— 用 RocksDB ConsumeQueue
} else {
    defaultMessageStore = new DefaultMessageStore(...);          // 经典文件 ConsumeQueue
}
```

一个 `messageStoreConfig.isEnableRocksDBStore()` 开关决定。这是 5.x 给"海量 Queue"用户准备的可选项,不是默认。

> **修正总纲的描述**:总纲第 153 行说"RocksDB 存储(`RocksDBMessageStore` 作为 `CommitLog + ConsumeQueue` 的可选替代实现)"——经源码核实,**RocksDB 只替了 ConsumeQueue,CommitLog 仍是经典 mmap**。本节以源码为准。仓库里确实还有一个 `MessageRocksDBStorage`([MessageRocksDBStorage.java:60](../rocketmq/store/src/main/java/org/apache/rocketmq/store/rocksdb/MessageRocksDBStorage.java#L60)),但它装的是 **timer / trans / index 三个 ColumnFamily**(延时消息、事务消息、key 索引的 RocksDB 实现),**不是 CommitLog**。这一点务必分清。

### 23.3.2 为什么要替 ConsumeQueue:经典文件数组在百万 Queue 下的痛点

第 6 章讲过经典 ConsumeQueue 的结构:每个 `topic-queue` 一个 ConsumeQueue 目录,目录下一组滚动文件,每个文件存定长 20 字节单元(8 字节 CommitLog 物理偏移 + 4 字节消息长 + 8 字节 tag hash)。consumeOffset 就是数组下标,定位 O(1)。

这套设计在"Topic/Queue 数量适中"时极优雅。但有一个边界:**每个 topic-queue 一个目录、一组文件**。设想一个多租户平台,10 万个 Topic,每个 Topic 平均 16 个 queue,那就是 **160 万个 ConsumeQueue**,每个一个目录、若干 `MappedFile`。痛点:

- **文件句柄爆炸**:160 万个 queue,每个至少一个 active `MappedFile` 映射在内存里,文件句柄和 mmap 区域都吃紧。
- **Reput 分发要随机写海量文件**:第 5 章讲过,后台 Reput 线程顺着 CommitLog 读,把每条消息分发进对应 topic-queue 的 ConsumeQueue。经典 ConsumeQueue 是"每 queue 一个文件",Reput 写 ConsumeQueue 就是在**百万个文件之间随机写**——虽然每个文件内部是顺序追加,但宏观上是海量文件间的跳写,页缓存压力巨大。
- **删除过期 ConsumeQueue 开销大**:消息过期要删老的 ConsumeQueue 文件,百万级 queue 各自管理文件生命周期,运维和 IO 开销都不小。

> **钉死这件事**:经典 ConsumeQueue 的痛点不在"单 queue 慢"(单 queue 仍是 O(1) 定位,很快),而在"queue 数量爆炸时,文件管理本身成了负担"。这和第 1 章 Kafka 的痛点是同构的——Kafka 是"每 Partition 一个文件,百万 Partition 写随机化",经典 RocketMQ 在百万 Queue 时 ConsumeQueue 也有类似的"海量文件"问题,只不过它在 CommitLog 这一侧靠"混写一个文件"已经赢了,ConsumeQueue 这一侧的"海量文件"留到了 5.x 用 RocksDB 解决。

### 23.3.3 RocksDB 的解法:所有 queue 的索引塞进一个 LSM

`RocksDBConsumeQueueStore`([RocksDBConsumeQueueStore.java:58](../rocketmq/store/src/main/java/org/apache/rocketmq/store/queue/RocksDBConsumeQueueStore.java#L58))把所有 topic-queue 的索引收敛进**一个 RocksDB 实例**,用**两个 ColumnFamily**(见源码注释 :69-73):

```java
// we use two tables with different ColumnFamilyHandle,
// called RocksDBConsumeQueueTable and RocksDBConsumeQueueOffsetTable.
// 1.RocksDBConsumeQueueTable stores CqUnit[physicalOffset, msgSize, tagHashCode, msgStoreTime]
// 2.RocksDBConsumeQueueOffsetTable stores physicalOffset and consumeQueueOffset of topic-queueId
```

- **default CF(`RocksDBConsumeQueueTable`)**:存 CqUnit。key = `topic | queueId | consumeQueueOffset`,value = 28 字节的 CqUnit(`CommitLog 物理偏移 8 + Body Size 4 + Tag HashCode 8 + Msg StoreTime 8`,见 `RocksDBConsumeQueueTable.java:75` 的 `CQ_UNIT_SIZE = 28`)。注意这比经典 ConsumeQueue 的 20 字节多了 8 字节的 `MsgStoreTime`。
- **offset CF(`RocksDBConsumeQueueOffsetTable`)**:存每个 topic-queue 当前最大的 consumeQueueOffset(和物理偏移的映射)。key = `topic | queueId`,value = `PhyAndCQOffset`。

消费时怎么定位?给定 `(topic, queueId, consumeOffset)`,拼成 key 去 default CF 点查(`RocksDBConsumeQueueTable.getCQInKV`,[:118](../rocketmq/store/src/main/java/org/apache/rocketmq/store/queue/RocksDBConsumeQueueTable.java#L118)),拿到 28 字节 CqUnit,里面的 `CommitLog 物理偏移` 回 CommitLog 取消息体。和经典 ConsumeQueue 的"offset×20 算偏移"相比,这里多了一次 LSM 的点查(memtable → SST 多层),但**LSM 内部有 BloomFilter + 紧凑存储,点查很快**;换来的是"百万 queue 也不再有海量文件"。

为什么两个 CF 而不是一个?**职责分离 + compaction 互不干扰**。default CF 是高写入吞吐的索引流(每条消息一条),需要频繁 compaction;offset CF 是低频更新的"水位"信息(每 queue 一条),几乎不 compaction。混在一个 CF 里,后者的少量数据会被前者的 compaction 反复重写(写放大)。拆成两个 CF,各用各的 compaction 策略——这正是《LevelDB》讲过的"不同访问模式的数据分开存储,减少写放大"在 RocketMQ 里的落地。

> **不这样会怎样**:假设把 CqUnit 和 offset 水位塞在同一个 CF,offset 这点元数据会被 CqUnit 的 compaction 带着反复重写。RocksDB 的 LSM compaction 是把整层重写一遍——offset 这种"几乎不变"的数据被反复搬动,纯属浪费 I/O。拆 CF 是 LSM 时代的常识,但对没读过《LevelDB》的读者值得点一句:这是 RocksDB 的"列族"机制和关系库的"表"不是一回事,它更像是"同一个 LSM 里多个独立的 compaction 单元"。

### 23.3.4 Reput 和刷盘怎么配合

第 5 章讲过,经典架构里 ConsumeQueue 由后台 Reput 线程异步从 CommitLog 分发建。RocksDB 模式下这个分工保留,但实现换了:

- Reput 仍然顺着 CommitLog 读,但 dispatcher 写进的是 `RocksDBConsumeQueueStore`,走 RocksDB 的 `WriteBatch` 批量写(见 `RocksDBConsumeQueueTable.buildAndPutCQByteBuffer`,[:107-116](../rocketmq/store/src/main/java/org/apache/rocketmq/store/queue/RocksDBConsumeQueueTable.java#L107-L116),把一批 CqUnit 攒进一个 WriteBatch 一次提交)。
- RocksDB 有自己的 WAL 和 memtable flush 机制,RocketMQ 还额外加了一个 `RocksGroupCommitService`([RocksDBConsumeQueueStore.java:88](../rocketmq/store/src/main/java/org/apache/rocketmq/store/queue/RocksDBConsumeQueueStore.java#L88))做"组提交",攒一批 CqUnit 一次性写,降低 RocksDB 写放大,同时唤醒等消息的长轮询消费者(源码注释 :96-104 明说:RocksDB CQ 由 RocksGroupCommitService 建,所以 Reput 那条路不需要再通知长轮询)。

注意一个微妙之处:RocksDB 模式下 `DefaultMessageStore` 构造时设置了 `messageStore.setNotifyMessageArriveInBatch(true)`([:107](../rocketmq/store/src/main/java/org/apache/rocketmq/store/queue/RocksDBConsumeQueueStore.java#L107))——因为 CqUnit 是攒批写的,长轮询唤醒也要按批来,不能像经典模式那样逐条唤醒。这是"攒批写"和"实时唤醒"之间的取舍。

---

## 23.4 分级存储:冷数据下沉,热路径不降

### 23.4.1 为什么本地 CommitLog 存不下所有历史

经典架构里,消息一直存在本地 CommitLog(一组 1GB 的 `MappedFile`)。`DefaultMessageStore` 靠"文件过期删除"控制磁盘占用——默认保留一段时间(比如 72 小时)的消息,老的 CommitLog 文件整个删掉。

这套机制在"热数据为主、历史不长"的场景没问题。但有两个场景卡壳:

- **长留存需求**:有些业务(审计、回溯、合规)要保留几个月甚至几年的消息。全堆在本地 SSD,存储成本爆炸。
- **冷热分明**:大多数消息只在写入后的短时间内被消费(热),之后极少被访问(冷)。冷数据占着昂贵的本地 SSD,纯属浪费。

朴素解法是"加磁盘",但成本扛不住。5.x 的 `tieredstore` 模块给出**分级存储**:热数据留本地 CommitLog,冷数据异步下沉到廉价的对象存储(OSS / S3)或低速介质,消费时按需从远端拉回来。

### 23.4.2 装饰器模式:套在原 store 外,不动内核

分级存储的实现用了**装饰器(Decorator)模式**——`TieredMessageStore extends AbstractPluginMessageStore`([TieredMessageStore.java:80](../rocketmq/tieredstore/src/main/java/org/apache/rocketmq/tieredstore/TieredMessageStore.java#L80)),构造时传一个 `next`(底层的 `DefaultMessageStore` 或 `RocksDBMessageStore`):

```java
public TieredMessageStore(MessageStorePluginContext context, MessageStore next) {
    super(context, next);                                  // :82 —— next 是底层真实 store
    // ...
    this.defaultStore = next;                              // :95
    this.messageRocksDBStorage = defaultStore.getMessageRocksDBStorage();   // :96
    this.metadataStore = this.getMetadataStore(this.storeConfig);
    // ...
    this.flatFileStore = new FlatFileStore(...);           // 分级文件管理
    this.indexService = new IndexStoreService(...);        // 远端索引
    this.fetcher = new MessageStoreFetcherImpl(this);      // 远端拉取
    this.dispatcher = new MessageStoreDispatcherImpl(this);// 后台下沉分发
    next.addDispatcher(dispatcher);                        // :106 —— 把下沉 dispatcher 挂到底层 store 的 Reput 链
}
```

注意最后一行 `next.addDispatcher(dispatcher)`——这是把"下沉"做成了底层 store 的**一个额外 dispatcher**(回顾第 5 章:`dispatcherList` 是责任链,加一种索引只加一个 dispatcher)。所以分级存储的"下沉",本质是**在经典 Reput 的 dispatcher 链上又挂了一个 dispatcher**,它把消息异步推到远端对象存储——**写路径零改动,CommitLog 照样纯顺序写**。

> **钉死这件事**:分级存储不是"另写一套存储引擎",而是"在原 store 外套一层装饰器"。`TieredMessageStore` 把所有它不关心的操作(写消息、刷盘、主从复制)直接转发给 `next`(底层 store),只 override 它要插手的——主要是 `getMessageAsync`(冷热分流读)和挂 dispatcher(异步下沉)。装饰器模式让"分级"这件事对内核零侵入,这是它能作为可插拔选项存在的前提。

### 23.4.3 冷热分流读:热路径不降的秘诀

分级存储最关键的问题是:**加了这一层,会不会拖慢热路径读?** 答案藏在 `getMessageAsync`([TieredMessageStore.java:217-232](../rocketmq/tieredstore/src/main/java/org/apache/rocketmq/tieredstore/TieredMessageStore.java#L217-L232)):

```java
public CompletableFuture<GetMessageResult> getMessageAsync(String group, String topic,
    int queueId, long offset, int maxMsgNums, MessageFilter messageFilter) {

    // for system topic, force reading from local store                  // :221
    if (topicFilter.filterTopic(topic)) {
        return next.getMessageAsync(group, topic, queueId, offset, maxMsgNums, messageFilter);
    }

    if (fetchFromCurrentStore(topic, queueId, offset, maxMsgNums)) {     // :225 —— 判定要不要走远端
        log.trace("GetMessageAsync from remote store, ...");
    } else {
        log.trace("GetMessageAsync from next store, ...");
        return next.getMessageAsync(group, topic, queueId, offset, maxMsgNums, messageFilter);  // :231 —— 本地有,直接走热路径
    }
    // ... 走远端 fetcher.getMessageAsync ...
}
```

热路径的秘诀在 `fetchFromCurrentStore` 的判定([:177-208](../rocketmq/tieredstore/src/main/java/org/apache/rocketmq/tieredstore/TieredMessageStore.java#L177-L208)):

```java
if (offset >= flatFile.getConsumeQueueCommitOffset()) {     // :187 —— offset 还没下沉到远端,本地一定有
    return false;
}
if (storageLevel.check(TieredStorageLevel.NOT_IN_DISK)) {   // :192 —— 配置成"本地没有才走远端"
    if (next.getCommitLog().getMinOffset() < 0L) return true;
    if (!next.checkInStoreByConsumeOffset(topic, queueId, offset)) return true;   // :198 —— 本地确实没有
}
if (storageLevel.check(TieredStorageLevel.NOT_IN_MEM)       // :203 —— 配置成"内存没有就走远端"
    && !next.checkInMemByConsumeOffset(topic, queueId, offset, batchSize)) {
    return true;
}
return false;
```

逻辑很直白:**本地 CommitLog 还有的消息,直接走 `next.getMessageAsync`(经典热路径,零拷贝 sendfile,毫秒级);只有本地已经删掉、确实没有的消息,才走远端 fetcher 去对象存储拉。** 配置项 `TieredStorageLevel` 控制"什么时候才算本地没有"——`NOT_IN_DISK`(本地磁盘没有就走远端)或 `NOT_IN_MEM`(本地内存没有就走远端,更激进)。

> **不这样会怎样**:假设分级存储对每个读请求都"先问远端有没有",那热路径每次都要多一次网络 RTT,经典 RocketMQ 的低延迟优势全毁。`fetchFromCurrentStore` 这道判定是命门——**它让本地命中的读请求完全不感知分级存储的存在**,热路径性能和不开分级存储时一模一样。这是"加功能不拖慢老路径"的经典做法,和第 5 章 Reput 用"后台异步建索引、写路径零改动"是同一个思路。

还有一道保险:远端拉取失败或没找到,会 **fallback 回 next store**([:246-255](../rocketmq/tieredstore/src/main/java/org/apache/rocketmq/tieredstore/TieredMessageStore.java#L246-L255))。`OFFSET_FOUND_NULL` 时如果 `next.checkInStoreByConsumeOffset` 说本地其实有(可能是下沉还没完成、offset 边界还没推进),就回到本地读。这保证了"下沉过程中的过渡态"不会让消费失败——**分级存储是最终一致,本地是兜底真相**。

### 23.4.4 下沉的载体:FlatFileStore 与对象存储

下沉到远端的数据结构是 `FlatMessageFile` / `FlatFileStore`([tieredstore/file/](../rocketmq/tieredstore/src/main/java/org/apache/rocketmq/tieredstore/file/)),它把一个 topic-queue 的消息"摊平"成两个文件:`FlatCommitLogFile`(消息体)+ `FlatConsumeQueueFile`(索引)。底层真正存数据的 `FileSegment` 有多种实现——`MemoryFileSegment`(内存,测试用)、`PosixFileSegment`(本地 POSIX 文件)、以及对接对象存储的 provider(看 `tieredstore/provider/` 目录)。这就是"分级"的字面含义:不同层级用不同介质,热的本地 SSD,冷的远端对象存储。

后台 `MessageStoreDispatcherImpl` 按 topic-queue 攒批,把"已经过了热保留期"的消息从本地 CommitLog 切到远端 FlatFile,同时更新 metadata(记录每个 topic-queue 下沉到哪个 offset)。这个 dispatcher 就是 23.4.2 里挂到 `next.addDispatcher` 的那个——**它和经典 ConsumeQueue/IndexFile 的 dispatcher 是平级的,共享同一条 Reput 分发链**。这是 RocketMQ 责任链设计的复用红利:加一种"下沉到远端"的索引,写路径一个字没改。

---

## 23.5 技巧精解:5.x 为什么用 LSM(RocksDB)替掉 ConsumeQueue,而不替 CommitLog

本章最值得深挖的一组对照:**既然 RocksDB 是个强大的 LSM,为什么 5.x 只拿它替了 ConsumeQueue,而把吞吐更关键的 CommitLog 留给了经典 mmap?** 这背后是 RocketMQ 团队对"哪种负载适合 LSM、哪种适合 append-only 大文件"的清醒判断。

### 23.5.1 经典 CommitLog vs RocksDB LSM:谁更适合"海量写入"

先立清两种存储的写路径模型(对标《LevelDB》第 1 章):

| 维度 | 经典 CommitLog(mmap `MappedFile`) | RocksDB LSM |
|------|-----------------------------------|-------------|
| 写入本质 | 一个大文件纯顺序追加 | memtable(WAL)→ L0 → L1...Ln 多层归并 |
| 写放大 | 约 1×(写一次落盘) | 远大于 1×(每层 compaction 重写) |
| 适合的访问模式 | 顺序写、按物理偏移随机读 | 点查、范围查、海量 key |
| Topic/Queue 数量影响 | 写不受影响(永远一个文件追加) | 写基本不受影响(所有 key 进同一个 LSM) |

CommitLog 的负载是**纯顺序写海量消息体**(每条几 KB 到几 MB),写放大越低越好——mmap 的"一个文件追加"写放大接近 1×,是这种负载的教科书级最优解。如果换成 RocksDB 存消息体,每条消息要先写 WAL、再写 memtable、再一层层 compaction,写放大可能到 10×~30×,对高吞吐写入是灾难。

> **钉死这件事**:RocketMQ 把"存消息体"留给 mmap、把"存索引"交给 RocksDB,不是随便分的——**这是按"写放大敏感度"做的分工**。消息体是大对象、写入吞吐生死攸关,绝不能用 LSM 的写放大;索引是小记录(28 字节)、点查为主、且天然有"按 key 排序"的需求(ConsumeQueue 本来就按 `topic|queueId|offset` 排),LSM 是它的天选结构。这条判断和《LevelDB》"为什么 LevelDB 用 LSM 而不是 B+ 树"是同源的——**LSM 换顺序写吞吐、代价是读放大和写放大,适合写多读点查的负载**。

### 23.5.2 经典 ConsumeQueue vs RocksDB ConsumeQueue:百万 Queue 下的反转

反过来,为什么 ConsumeQueue 适合换 RocksDB?对比"百万 Queue"这个特定场景下两者的痛点:

| 维度 | 经典 ConsumeQueue(每 queue 一个文件) | RocksDB ConsumeQueue(一个 LSM) |
|------|-------------------------------------|-------------------------------|
| 文件数 | 百万 Queue → 百万文件/目录 → 句柄爆炸 | 一个 LSM 实例,文件数恒定(几层 SST) |
| Reput 写索引 | 百万文件间跳写,页缓存压力 | 全进一个 memtable,顺序写 WAL + 内存 |
| 单次点查 | offset×20 算偏移,O(1),极快 | LSM 点查(memtable+Bloom+SST),略慢但仍是亚毫秒 |
| 过期清理 | 删单个 queue 的老文件,百万级管理 | compaction 时按 key 前缀过滤,统一处理 |
| 写放大 | 1×(纯追加) | >1×(compaction) |

经典 ConsumeQueue 在"单次点查 O(1)"这一点上完胜,但它的优势依赖"每 queue 一个文件、offset 是数组下标"。**一旦 queue 数量爆炸,文件管理本身的成本(句柄、页缓存、Reput 跳写)就反噬了 O(1) 的优势。** RocksDB 用"写放大换文件数收敛"——百万 Queue 在 RocksDB 里只是一百万种不同的 key 前缀,SST 文件数和 queue 数无关。

> **反面对比**:假设有一个云原生 PaaS 平台,10 万租户,每租户 1 个 Topic 16 个 queue = 160 万 queue。经典 ConsumeQueue 要建 160 万个目录、维护 160 万组 `MappedFile`,Reput 写索引在百万文件间跳。RocksDB 模式下,这 160 万 queue 的索引全在一个 LSM 里,Reput 写进一个 memtable(顺序写 WAL),compaction 在后台统一处理——**文件数从"随 queue 数线性增长"收敛成"恒定"**。这就是 5.x RocksDB 存储存在的理由。它对标的是 Kafka 在百万 Partition 下的痛点(第 1 章),只不过 Kafka 痛在 Partition 文件本身,RocketMQ 把消息体这一侧用"混写一个 CommitLog"已经解决了,ConsumeQueue 这一侧留到 5.x 用 LSM 收尾。

### 23.5.3 取舍的总账

把"为什么只替 ConsumeQueue"的总账算清楚:

- **CommitLog 不替**:消息体是写吞吐命门, mmap 追加是写放大最低的方案,LSM 的写放大换不来等价的收益。混写一个 CommitLog 的灵魂不动。
- **ConsumeQueue 替**:索引是小记录、点查负载、且"百万 queue 文件数爆炸"是经典架构的唯一硬伤——LSM 用可控的写放大换"文件数恒定",在百万 Queue 场景净赚。
- **IndexFile / Timer / Trans 也替**(见 `MessageRocksDBStorage` 的 index/timer/trans 三个 CF):同理,都是小记录、点查负载,适合 LSM。

这条判断对读过《LevelDB》的读者应该特别亲切——**LevelDB 的设计哲学就是"用 LSM 收敛写放大、用紧凑 SST 换读",RocksDB 继承了它**。RocketMQ 在"索引类、点查类、海量 key"的存储上全面拥抱 RocksDB,在"大对象、纯顺序写"的 CommitLog 上坚守 mmap,是按负载特性做的精准分工,不是跟风。

---

## 章末小结

这一章讲的是 5.x 在经典架构之上的三处演进。把它们放回全书主线:

- **Proxy**(gRPC 接入层)落在**分布式骨架**那一面——它解决的是"接入"问题(多语言、协议清晰、计算存储分离),不动存储内核。
- **RocksDB 存储**(替 ConsumeQueue)落在**存储内核**那一面——它解决的是"百万 Queue 文件数爆炸"问题,但写路径的灵魂(混写一个 CommitLog)原封不动,只换了 ConsumeQueue 这一层索引的实现。
- **分级存储**(TieredStore)落在**存储内核 + 分布式骨架的衔接处**——它用装饰器套在原 store 外,把冷数据下沉到对象存储,热路径性能不降。

三样东西有个共同的姿态:**在经典架构的瓶颈上做加法,不推翻重来。** Proxy 叠在 Remoting 旁(Remoting 保留),RocksDB 替 ConsumeQueue(CommitLog 保留),TieredStore 装饰原 store(底层 store 保留)。这是工程上对存量生态和已验证设计的双重尊重——也是 RocketMQ 作为一个生产十年、支撑双十一的系统能持续演进的根基。

> **回扣主线**:全书第一性原理是"用混写一个 CommitLog 换纯顺序写吞吐,代价是读随机化,靠 ConsumeQueue/IndexFile/零拷贝收敛"。5.x 的三样演进,**没有一条动这个灵魂**——CommitLog 仍混写,ConsumeQueue 仍重建逻辑队列(只是从文件数组换成了 LSM),零拷贝仍在,分级存储的热路径仍走经典 sendfile。5.x 是在经典架构够用的边界上做了针对性补强,不是另起炉灶。读完前 7 篇你能讲清一条消息在经典架构里的一生;读完这一章,你知道这一套在 5.x 时代如何延续、在哪里被悄悄换了骨头。

### 五个"为什么"清单

1. **为什么 5.x 要在 Remoting 之外再叠一层 gRPC Proxy,而不是替掉 Remoting?** Remoting 绑定 Java(`CommandCustomHeader` + 自研序列化),多语言对接难、协议不清晰;gRPC 用 Protocol Buffers 做跨语言 IDL。但 Remoting 有海量存量 Java 客户端,强行替换迁移成本极高——所以 Proxy 和 Remoting **并存**,同一套 `MessagingProcessor` 双协议复用,老客户端零改动。
2. **Proxy 的 Local 模式和 Cluster 模式有什么区别,为什么留两个?** Local 模式 Proxy 进程内嵌一个 `BrokerController`(接入+存储同 JVM,平滑过渡);Cluster 模式 Proxy 纯做接入、后挂独立 Broker 集群(接入与存储分离,可独立扩缩)。留两个模式给"想用 gRPC 但不想拆架构"的用户一条原地升级的路。
3. **RocksDB 存储到底替了经典架构的什么?** 诚实回答:**只替了 ConsumeQueue**(以及 timer/trans/index 等索引类存储),**CommitLog 仍是经典 mmap**。`RocksDBMessageStore` 只 override `createConsumeQueueStore()` 一个工厂方法,写路径一个字没改。这是对"RocksDB 替 CommitLog+ConsumeQueue"这个常见说法的修正。
4. **为什么 CommitLog 不替成 RocksDB,ConsumeQueue 要替?** CommitLog 是写吞吐命门(mmap 追加写放大≈1×,LSM 写放大 10×~30×,对大对象写入是灾难);ConsumeQueue 是小记录、点查负载,且"百万 Queue 文件数爆炸"是经典架构唯一硬伤——LSM 用可控写放大换"文件数恒定",在百万 Queue 场景净赚。这是按写放大敏感度做的精准分工。
5. **分级存储怎么做到"冷数据下沉、热路径不降"?** 装饰器模式套在原 store 外,`getMessageAsync` 先用 `fetchFromCurrentStore` 判定:本地 CommitLog 还有的消息直接走 `next.getMessageAsync`(经典热路径,零拷贝 sendfile);只有本地已删的消息才走远端 fetcher。本地命中的读请求完全不感知分级存储,热路径性能和不开分级时一样。下沉做成 Reput 链上一个额外 dispatcher,写路径零改动。

### 想继续深入往哪钻

- **Proxy 的双协议接入**:读 `proxy/src/main/java/org/apache/rocketmq/proxy/ProxyStartup.java`([:78-99](../rocketmq/proxy/src/main/java/org/apache/rocketmq/proxy/ProxyStartup.java#L78-L99))看 gRPC 和 Remoting 如何同时启动;`MessagingProcessor.java`([:51](../rocketmq/proxy/src/main/java/org/apache/rocketmq/proxy/processor/MessagingProcessor.java#L51))看双协议如何汇到同一套业务方法。
- **RocksDB ConsumeQueue 的 KV 模型**:读 `RocksDBConsumeQueueStore.java`([:58](../rocketmq/store/src/main/java/org/apache/rocketmq/store/queue/RocksDBConsumeQueueStore.java#L58))看两个 CF 的分工;`RocksDBConsumeQueueTable.java`([:43](../rocketmq/store/src/main/java/org/apache/rocketmq/store/queue/RocksDBConsumeQueueTable.java#L43))看 CqUnit 的 28 字节 value 布局和 key 拼装;`ConsumeQueueRocksDBStorage.java`([:38](../rocketmq/store/src/main/java/org/apache/rocketmq/store/rocksdb/ConsumeQueueRocksDBStorage.java#L38))看 RocksDBOptions 怎么调。
- **分级存储的冷热分流**:读 `TieredMessageStore.java`([:80](../rocketmq/tieredstore/src/main/java/org/apache/rocketmq/tieredstore/TieredMessageStore.java#L80))的装饰器结构和 `getMessageAsync`([:217](../rocketmq/tieredstore/src/main/java/org/apache/rocketmq/tieredstore/TieredMessageStore.java#L217))的判定逻辑;`FlatFileStore` / `FlatMessageFile` 看下沉的数据结构;`MessageStoreDispatcherImpl` 看后台下沉 dispatcher。
- **横向呼应**:RocksDB ConsumeQueue 的 LSM 与写放大,直接对标《LevelDB》全书的 LSM 设计;分级存储的"冷热分层 + 装饰器",可以类比《Linux 内存管理》里"热页留 LRU 活跃链、冷页回收"的思路——不同介质、不同访问频度的数据,用分层存储收敛成本。

### 引出下一章

我们走完了 RocketMQ 5.x 的三处架构演进。这本书的最后一章(P9-24),要把全书收束成几条权衡哲学:混写一个 CommitLog 换纯顺序写、写与建索引异步解耦、零拷贝清零磁盘到网卡的拷贝、AP 心跳注册中心换可用性、长轮询换实时与无状态、至少一次 + 业务幂等换实现简单;再画一张 RocketMQ vs Kafka 的根本分野总表,以及 5.x 相对 4.x 的演进回顾。一条消息从 Producer 到 Consumer 的端到端时序总图,会在那里一次性铺开。
