# 第 6 篇 · 第 20 章 · 元数据、channelz、stats / otel

> **核心问题**:一次 gRPC 调用,从来不是孤零零的两个字节流在两点之间流动。它随身带着一捆"上下文":鉴权用的令牌、跨服务串联用的 trace-id、告诉对方"我最多等你 5 秒"的 deadline、表明身份的 user-agent、以及用于压缩协商的 grpc-encoding。这些上下文怎么在调用两端高效地传?更关键的是,当线上某个 channel 突然连不上后端、或者某条 SubChannel 的 socket 一直重连失败,你怎么把它**看清楚**——不是看一堆日志猜,而是把运行时那一整棵"channel → SubChannel → socket"的诊断树直接拉出来,带着每一步发生的 trace 事件?最后,每次调用的延迟、每个 cluster 的请求量、每条流的错误码,这些指标怎么稳定地导出到 Prometheus / OpenTelemetry?

> **读完本章你会明白**:
> 1. 为什么 gRPC 的元数据不存成 `map<string, string>`,而要做成一个**类型化的、编译期打表的 metadata_batch**——以及这套设计怎么让"传几十个 header"在热路径上几乎零分配。
> 2. channelz 怎么把运行时那一整棵 channel/SubChannel/socket/server 的活体状态,做成一棵**带 trace 事件的可查询诊断树**,以及为什么它内部已经迁到 v2(protobuf)却还要保留一层 v1(JSON)兼容壳。
> 3. 为什么 OpenCensus 和 OpenTelemetry 在 gRPC 里是**两套并行、各走各路**的集成,以及它们怎么都挂在 `src/core/telemetry/` 这套通用的 CallTracer 接口上,而不是各写各的 filter。
> 4. 元数据怎么用 metadata_batch 在 P3-12 讲过的 call spine 三段式里被传递,以及 trace-id 怎么跟着它一路从客户端穿到服务端再穿回来。

> **如果一读觉得太难**:先只记住三件事——① 元数据是**类型化打包**的一批 header,不是 map;② channelz 是把运行时连成一棵**可查询的诊断树**,出连接问题先问它;③ gRPC 的指标都从 `src/core/telemetry/` 这个通用出口走,otel 是其中一个后端。

---

## 〇、一句话点破

> **元数据是 call 随身带的"上下文捆",它被做成类型化的一批而不是 map;channelz 把运行时那棵"谁连着谁、每条连接怎么了"的活体树直接递到你手里;指标从一套通用 CallTracer 接口出,otel 是其中一个标准后端。**

这是结论,不是理由。本章倒过来拆:先讲清"传上下文"为什么不能朴素地用一个 `map<string,string>`,再讲 metadata_batch 怎么用编译期打表把它做对;然后讲 channelz 怎么把运行时实体注册成一棵诊断树、trace 事件怎么挂;最后讲指标怎么从 CallTracer 通用出口流到 otel。三章二分法归属:**可用 / 可观测**——这一章是框架层"把网络字节变回可调用、可治理的方法"里,"可观测"那一面的总结。

---

## 一、元数据:从"一捆 header"到"类型化的一批"

### 一次调用随身带了什么

一次 gRPC 调用,真正在 HTTP/2 上跑的,除了 protobuf 编码的 message 本体,还有一大堆 header。回忆 P2-07(HPACK)和 P3-12(四种调用模式):每次调用都从发送 HEADERS 帧开始,里面塞着:

- `:path` / `:authority` / `:method` / `:scheme` —— HTTP/2 伪头,定位"调哪个方法"。
- `te: trailers` —— gRPC 协议标志,告诉对方"我用 trailer 收尾"。
- `content-type: application/grpc` —— 内容类型。
- `grpc-timeout: 5S` —— deadline,告诉服务端"我最多等 5 秒"。
- `grpc-encoding: gzip` —— 我希望消息被压缩成 gzip。
- `grpc-trace-bin: <二进制>` —— OpenCensus/otel 的 trace 上下文,跨服务串联 trace。
- `authorization: Bearer <token>` —— 鉴权令牌。
- `user-agent: grpc-c/1.83.0` —— 身份。

这些 header,在 gRPC core 内部统一叫**元数据(metadata)**。它们和 message 本体不一样:message 是业务数据(那个 protobuf 的 `User` 对象),元数据是**围绕这次调用的上下文**。理解 gRPC,必须把"元数据"和"message"这两件东西分开看:元数据先发(HEADERS 帧),message 跟在后面(DATA 帧),收尾的状态码又走 trailer(还是 HEADERS 帧)。P3-12 把这条流拆成 `send-metadata → send-message → recv-message → recv-status` 三段,其中第一段发的就是这捆元数据。

> **钉死这件事**:gRPC 把"围绕一次调用的上下文"和"业务消息本体"彻底分开。上下文叫元数据,走 HEADERS;消息叫 message,走 DATA。这种分离,是 gRPC 能在流上拼出 deadline、trace、鉴权、压缩协商这些非业务信息的前提——它们不污染 protobuf 编码,各自有独立的生命周期。

### 不这样想会怎样:朴素地用 map<string,string>

那这捆元数据,在 gRPC core 内部怎么表示?最直觉的答案:用一个 `std::map<std::string, std::string>`(或者 `unordered_map`)。key 是头名,value 是头值,完事。许多 RPC 框架就是这么干的,看起来再自然不过。

但是,把这条路放到 gRPC 的真实约束下,会撞上三堵墙:

**第一堵墙:热路径上的字符串查找开销**。一次调用要读 `grpc-timeout`、`grpc-encoding`、`grpc-trace-bin` 这些已知 header。如果元数据是 map,那"读 grpc-timeout"就是 `map.find("grpc-timeout")`——一次字符串哈希 + 比较。一次调用要读十几个 header,而且这十几个 header 的名字**每次都一样**(`grpc-timeout` 永远叫 `grpc-timeout`)。每次都重新哈希一遍同样的字符串,纯属浪费。

**第二堵墙:值要被反复解析**。header 在线上是文本(或二进制),但 gRPC 内部要用的是结构化值。比如 `grpc-timeout: 5S` 在线上是字符串 `"5S"`,内部要用的是一个 `grpc_core::Duration`(纳秒级的整数)。`grpc-status: 0` 在线上是字符 `'0'`,内部要用的是一个 enum。如果元数据是 `map<string,string>`,那每次要用 timeout 都得重新解析一遍 `"5S"` → `Duration`,这解析工作重复做无数次。

**第三堵墙:拷贝**。元数据要从客户端 filter 栈一路传到 transport,中间被十几个 filter 看、改、加。如果它是 `map<string,string>`,每次传递要么拷贝整个 map(深拷贝每个 string),要么用共享指针加锁。前者慢,后者更慢。

> **不这样会怎样**:朴素 map 的世界,是一次调用 = 一次次字符串哈希查找 + 一次次重复解析 + 一次次拷贝。在一个每秒百万次调用的系统里,光是处理元数据就能吃掉可观的 CPU。

### 所以 gRPC 这样设计:类型化的 metadata_batch

gRPC 的答案,核心是两步:**类型化**和**打表**。

**第一步:类型化(每个已知 header 对应一个 trait)**。gRPC 给每个已知的、有限的 header 定义一个**类型 trait**。比如 `grpc-timeout` 对应 `GrpcTimeoutMetadata` trait,它的 `ValueType` 是 `grpc_core::Duration`;`grpc-status` 对应 `GrpcStatusMetadata`,值是 `grpc_status_code` 枚举;`grpc-encoding` 对应 `GrpcEncodingMetadata`,值是压缩算法枚举。看真实代码(`src/core/call/metadata_batch.h`):

```cpp
// 简化示意,非源码原文:GrpcTimeoutMetadata trait 的核心
struct GrpcTimeoutMetadata {
  static constexpr bool kRepeatable = false;        // 不可重复(只有一个值)
  using ValueType = Duration;                       // 内部类型是 Duration
  using MementoType = Duration;
  static absl::string_view key() { return "grpc-timeout"; }
  static ValueType ParseMemento(Slice value, ...) {  // 把线上 "5S" 解析成 Duration,只解析一次
    return ParseTimeout(value);
  }
  static ValueType MementoToValue(MementoType m) { return m; }
  static Slice Encode(ValueType x) { ... }          // 序列化回 "5S"
};
```

关键在于:**解析只发生一次**。当 HPACK 解码器从网络上读到 `grpc-timeout: 5S` 这个 header,它调用 `MetadataMap::Parse`(`metadata_batch.h:1662`),这个函数在编译期就生成了"key → trait"的分派表(`NameLookup`),找到 `GrpcTimeoutMetadata`,调它的 `ParseMemento` 把 `"5S"` 变成 `Duration` 存起来。从这以后,gRPC 内部要拿 timeout,直接拿到的是一个 `Duration`,**再也不用碰那个字符串**。这下第二堵墙(重复解析)就没了。

**第二步:打表(类型化的紧凑表替代 map)**。这些已知 header 集合是**有限且固定的**(`grpc-timeout`、`grpc-status`、`content-type`……也就几十个)。既然有限,gRPC 干脆不用哈希表,而是给每个 trait 分配一个**编译期槽位**,做成一张紧凑表。看 `metadata_batch.h:1707-1709` 的真实成员定义:

```cpp
// src/core/call/metadata_batch.h:1707-1709 (MetadataMap 的私有成员)
class MetadataMap : ... {
  ...
 private:
  PackedTable<Value<Traits>...> table_;                    // 已知 trait 的紧凑表
  metadata_detail::UnknownMap unknown_;                    // 未知的、用户自定义 header 兜底
};
```

`PackedTable`(定义在 `src/core/util/packed_table.h`)是一张**按 trait 类型在编译期生成**的表:每个 trait 一个槽位,值内联存储,访问是 `O(1)` 的、且无需任何字符串比较。要拿 timeout?直接访问 `GrpcTimeoutMetadata` 这个槽位,拿到 `Duration`。这下第一堵墙(字符串查找)也没了。

那 `UnknownMap` 又是什么?它是给**未知 header** 留的兜底——用户自定义的、gRPC 不认识的 header(比如 `x-custom-header: foo`)。这些没有对应的 trait,只能退化成 `vector<pair<Slice, Slice>>`(`metadata_batch.h:1238`)。但注意:这一路是**冷路径**,绝大多数生产 header 都有 trait,走的是热路径的 PackedTable。

整个 `grpc_metadata_batch` 类型,在 `metadata_batch.h:1764` 是个近乎空壳的 struct:

```cpp
// src/core/call/metadata_batch.h:1764
struct grpc_metadata_batch : public grpc_metadata_batch_base {
  using grpc_metadata_batch_base::grpc_metadata_batch_base;  // 只继承构造函数
};
```

而 `grpc_metadata_batch_base`(L1735)是 `MetadataMap<grpc_metadata_batch, /* 30 来个 trait */>` 的 typedef。这就是"metadata_batch 不是 map,是 CRTP 模板 + 编译期表"的真相。

> **钉死这件事**:gRPC 的元数据表示,核心是两个动作——**把已知 header 类型化(每个一个 trait),把 trait 打成编译期紧凑表**。已知 header 走热路径零查找零重复解析,未知 header 退化成 vector 兜底。这是 gRPC 在元数据热路径上做到几乎零分配的根。`map<string,string>` 是它的反例。

### 元数据怎么传:零拷贝,跟 call 生命周期

元数据这捆东西,要从客户端 filter 栈一路传到 transport 编码成 HTTP/2 帧,服务端解出来再传上去。这一路怎么传?

答案是和 message 一样——**靠移动,不靠拷贝**。回忆 P3-11 filter 栈:一次调用穿过十几个 filter,每个 filter 是 `MakePollablePromise` 之类串起来的一条流水线,filter 之间传的是 `ClientMetadata`(就是 `grpc_metadata_batch` 的别名,见 `metadata.h:32`)的**句柄**(`ClientMetadataHandle = Arena::PoolPtr<ClientMetadata>`)。这个句柄是 move 语义的,move 一个 metadata_batch 只挪几个指针(table_ 和 unknown_ 的内部指针),不拷字节。下一章 P6-21 会专门拆这种零拷贝是怎么用 arena + slice 做到的,这里先记住结论:**元数据像 message 一样,在 filter 栈里是挪指针不拷字节的**。

而且,元数据和 call 同生命周期:**它从 arena 分配(P6-21 的主题),call 结束整个 arena 一起释放,无需逐个 header free**。这是 gRPC 把"上下文捆"做成 arena 上一个对象,而不是堆上一堆散字符串的另一个原因——和 call 同生共死,省掉一堆内存管理。

### parsed_metadata:一个 header 的类型擦除

上面讲的是"一捆"元数据(metadata_batch)。那"一个" header,在网络边界怎么表示?因为网络上的 header 是裸的 `key: value` 字符串,gRPC 解析时还不知道它属于哪个 trait。这时就用 `ParsedMetadata`(`src/core/call/parsed_metadata.h:115`)——一个**类型擦除**的"一个 header"载体:

```cpp
// 简化示意,非源码原文:ParsedMetadata 的核心
template <typename MetadataContainer>
class ParsedMetadata {
  struct VTable {                          // parsed_metadata.h:217
    void (*destroy)(...);
    void (*set)(ParsedMetadata*, MetadataContainer*);
    bool is_binary_header;
    // ...
  };
  union Buffer {                           // parsed_metadata.h:62
    uint8_t trivial[sizeof(grpc_slice)];
    grpc_slice slice;
    void* pointer;
  };
  const VTable* vtable_;
  Buffer buffer_;
};
```

它用一个小 union 缓存小值(内联)、用 vtable 函数指针实现多态,这样不管 header 值是 trivial 整数、是 Slice、还是需要堆分配的对象,都能用同一个 `ParsedMetadata` 类型承载。HPACK 解码器解出来的每个 header,先变成一个 `ParsedMetadata`,然后 `MetadataMap::Set(ParsedMetadata)`(`metadata_batch.h:1672`)把它放进对应的 trait 槽位(或 unknown 兜底)。`transport_size()`(`parsed_metadata.h:203`)按 `key_size + value_size + 32` 算这个 header 占线上多少字节,供 HPACK 表大小协商用(P2-07)。

> **不这样会怎样**:如果没有 `ParsedMetadata` 这种类型擦除的中间态,HPACK 解码器就得"边解边知道这个 header 是哪个 trait",这把网络解析层和 trait 定义耦合死了。有了它,解码器只产出通用的 `ParsedMetadata`,trait 分派发生在 `Set` 进 metadata_batch 时,层次清晰。

---

## 二、channelz:把运行时连成一棵诊断树

### 出问题了怎么办

假设线上某个 gRPC client 突然所有调用都超时。你登录机器,看到进程在跑、CPU 不高、内存正常、日志里只有一堆 `Deadline Exceeded`。然后呢?问题在哪?是 DNS 解析错了吗?是连上了后端但 TLS 握手失败?是 SubChannel 重连一直在退避?还是某条 socket 半死了 keepalive 没探测出来?

如果没有 channelz,你只能猜:翻日志、抓包、加埋点。但 gRPC runtime 里其实**已经知道这一切**——它知道自己建了几条 channel、每条 channel 派生了哪些 SubChannel、每条 SubChannel 当前是 IDLE 还是 CONNECTING 还是 TRANSIENT_FAILURE、每条 socket 上次握手花了多久、最近有哪些连接事件。问题在于,这些信息散在 runtime 各处,没有一个统一的出口。

channelz 就是这个出口。它把 runtime 里所有 channel、SubChannel、socket、server 这些实体,**注册到一棵全局的诊断树**里,你可以通过一个 gRPC service 查询这棵树的任意一个节点,看它的状态、子节点、以及它身上发生过的 trace 事件。

### channelz 的实体类型:一棵十种节点的树

channelz 里的所有实体都继承自 `BaseNode`(`src/core/channelz/channelz.h:119`),它用 `DualRefCounted` 做引用计数。每个实体有一个单调递增的 UUID,注册到全局 `ChannelzRegistry`(`channelz_registry.h:42`,单例)。看真实的实体类型枚举(`channelz.h:125-136`):

```cpp
// src/core/channelz/channelz.h:125-136
enum class EntityType {
  kTopLevelChannel,        // 用户自己建的 channel
  kInternalChannel,        // gRPC 内部的 channel(如 resolver 用的)
  kSubchannel,             // 一条后端连接(SubChannel,P4-14)
  kServer,                 // 一个 gRPC server
  kListenSocket,           // 监听 socket
  kSocket,                 // 一条已建立的 socket(TCP 连接)
  kCall,                   // 一次 call(粒度最细)
  kResourceQuota,          // 资源配额(memory quota)
  kMetricsDomain,          // 指标域
  kMetricsDomainStorage,   // 指标域存储
};
```

每个类型对应一个 `final` 子类:`ChannelNode`(L499)、`SubchannelNode`(L566)、`ServerNode`(L620)、`SocketNode`(L675)、`ListenSocketNode`(L793)、`CallNode`(L806)等。这些节点通过"父子关系"自然形成一棵树:

```
   Server(用户建的 gRPC server)
   ├─ ListenSocket(:8080)
   │   └─ Socket(已连接的客户端 conn 1)
   │   └─ Socket(已连接的客户端 conn 2)
   ...

   TopLevelChannel(用户建的 client channel)
   ├─ Subchannel(后端 A:10.0.0.1:8080)
   │   └─ Socket(连到 10.0.0.1 的 TCP conn)
   ├─ Subchannel(后端 B:10.0.0.2:8080)
   │   └─ Socket(连到 10.0.0.2 的 TCP conn,状态 TRANSIENT_FAILURE)
   └─ InternalChannel(resolver 用的)
```

> **钉死这件事**:channelz 的核心抽象是 `BaseNode`(L119)+ 单例 `ChannelzRegistry`(L42)。runtime 里每创建一个 channel/SubChannel/socket,就 new 一个对应的 Node、注册到 registry 拿一个 UUID。这棵树的拓扑,精确反映 runtime 里"谁连着谁"的真实关系。

### 查询这棵树:一个 gRPC service

channelz 对外暴露的方式,是一个 **gRPC service**——这非常 gRPC:用 gRPC 自己查 gRPC 自己。看 `src/cpp/server/channelz/channelz_service.h`:

```cpp
// src/cpp/server/channelz/channelz_service.h:31, 64
class ChannelzService final : public channelz::v1::Channelz::Service {  // v1(JSON)
  Status GetTopChannels(...) override;
  Status GetServers(...) override;
  Status GetServer(...) override;
  Status GetServerSockets(...) override;
  Status GetSubchannel(...) override;
  Status GetSocket(...) override;
};
class ChannelzV2Service final : public channelz::v2::Channelz::Service {  // v2(protobuf)
  ...
};
```

用户在自己的 server 上注册 `ChannelzServicePlugin`(`channelz_service_plugin.cc:34`),然后就可以用 `grpc_cli channelz`、`grpcurl` 或任何 channelz 客户端,调 `GetTopChannels` 拉出顶层 channel 列表、调 `GetSubchannel <uuid>` 看某条 SubChannel 的状态、调 `GetSocket <uuid>` 看某条 socket 的握手延迟和传输字节数。生产排查连接问题,这是第一利器——不用抓包,直接问 runtime。

### trace 事件:每条节点身上的"事件流"

光看静态状态(连接是 READY 还是 TRANSIENT_FAILURE)还不够,你还要知道"它经历了什么"——比如这条 SubChannel 三次连接失败的原因、那次 RST_STREAM 是几点发生的。channelz 给每个节点配了一个 `ChannelTrace`(`src/core/channelz/channel_trace.h:151`),按**树形结构**记录事件(L165 注释:"Nodes form a tree structure, allowing for hierarchical tracing"):

```cpp
// src/core/channelz/channel_trace.h:151, 318
class ChannelTrace {
  struct EntryRef { ... };           // 一个 trace 事件的引用
  struct Entry { ... };              // 真正的事件记录(注释 L312-316 说明大小对内存关键)
  void ForEachTraceEvent(...);       // L284 遍历事件
};
```

trace 事件比如:"SubChannel 进入 CONNECTING 状态"、"socket 握手完成,耗时 23ms"、"收到 GOAWAY"、"连接被 keepalive 探测判定为死连接而关闭"。这些事件带时间戳,渲染成 `grpc_channelz_v2_TraceEvent` upb 消息后返回给查询者。**这就是为什么 channelz 比抓包更直接**——它不抓字节,它把 runtime 内部的高层语义事件直接告诉你。

> **不这样会怎样**:没有 channelz,连接问题的排查是"黑盒考古":抓包看 TCP 三次握手、看 TLS alert、猜哪一步失败。channelz 把这些步骤**直接命名化、结构化**地暴露出来——不是让你从字节流里反推,而是 runtime 主动告诉你"我这一步做了什么、结果如何"。

### property_list:实体的属性怎么序列化

每个实体的状态(连接状态、地址、握手时间、传输字节数……)要能查出来,得有个结构化的表达。channelz 用 `PropertyList`(`src/core/channelz/property_list.h:132`):

```cpp
// src/core/channelz/property_list.h:132
class PropertyList final : public OtherPropertyValue {
  std::vector<std::pair<std::string, PropertyValue>> property_list_;  // L155
  void Set(key, value);   // L137
  json TakeJsonObject();  // L146 当前输出 JSON
  void FillUpbProto(...); // L147 后续填 channelz-v2 protobuf
};
```

它当前以 JSON 表达(注释 L129-131 说后续 channelz-v2 会改用 protobuf 直接捕获)。`MetadataToChannelzProperties`(`src/core/call/metadata_channelz.cc:24`)这个桥函数,把一个 metadata_batch 转成 channelz 的 PropertyList——所以你在 channelz 里能看到一条 call 携带了哪些元数据(敏感 key 会被 `IsMetadataKeyAllowedInDebugOutput` 过滤掉,避免泄露 token)。

### 新老并存:v2 是真身,v1 是兼容壳

这里有个必须诚实交代的演进状态。channelz 在 gRPC 里经历了 v1(JSON)到 v2(protobuf 原生)的迁移。当前 commit(1.83.0-dev)的真实情况是:

- **内部已是 v2**:所有 entity 的真身都用 upb/protobuf 表达,`BaseNode::SerializeEntity`(`channelz.cc:178`)直接序列化成 protobuf。
- **对外保留 v1 兼容**:`v2tov1/` 目录(`src/core/channelz/v2tov1/`)是**桥接层**,`convert.h:43-51` 的 `ConvertChannel/ConvertServer/ConvertSocket/...` 把 v2 protobuf 实体转成 v1 JSON 字符串,`legacy_api.h/cc` 提供旧 v1 风格的查询入口。

为什么这样?因为大量现有工具(`grpc_cli channelz` 老版本、各种运维脚本)还在调 v1 的 JSON API,直接砍掉会破坏兼容。所以 gRPC 选择**内部迁到 v2,外面留一层 v1 兼容壳**——这是大型系统演进的典型套路:新的真身先就位,老的接口慢慢退役。

> **钉死这件事**:channelz 当前是 v2(protobuf)为真身 + v1(JSON)为兼容壳。读源码别被老资料误导——以为 channelz 还是纯 JSON 的,那是 v1 时代。同时,`src/core/channelz/zviz/` 那一堆 `entity.cc/layout_html.cc/html.cc` 是 channelz 的可视化层(把 entity 渲染成 HTML),`ztrace_collector.h` 的 `ZTraceCollector`(L73)负责按需收集 trace 事件流——这些是 channelz 较新的扩展能力。

---

## 三、指标与 trace:CallTracer 通用出口 + otel / census 后端

### 指标要往哪出

光有 channelz 这种"按需查询"还不够,你还要把指标(每次调用的延迟、每个 method 的 QPS、每个 cluster 的错误率)持续导出到 Prometheus、Jaeger、Cloud Trace 这些后端,做成 dashboard 和告警。这就是 OpenTelemetry(otel)和它的前辈 OpenCensus 干的事。

这里有个 gRPC 设计上很漂亮的地方:**otel 和 census 不是各写各的 filter**,它们都挂在 `src/core/telemetry/` 这套**通用的 CallTracer 接口**上。

### 通用层:`src/core/telemetry/` 的 CallTracer

`src/core/telemetry/` 是 gRPC 的通用遥测抽象层(transport 无关)。核心是几个接口:

```cpp
// src/core/telemetry/call_tracer.h(简化示意)
class CallTracerInterface { ... };                          // L143 基类
class ClientCallTracerInterface : public CallTracerInterface {  // L195
  class CallAttemptTracer {                                  // L201 每次 attempt 一个
    virtual void RecordEnd(...) = 0;                         // L226 一次 attempt 结束时调
  };
};
class ServerCallTracerInterface : public CallTracerInterface {  // L247
  virtual void RecordEnd(...) = 0;                           // L256 服务端 call 结束时调
};
class ServerCallTracerFactory {                              // L261 工厂模式
  static void RegisterGlobal(...);                           // 注册全局工厂
  virtual ServerCallTracerInterface* CreateNewServerCallTracer(...) = 0;  // L267
};
```

关键设计:**指标记录的"插入点"是 call attempt 的生命周期**。每次 client 发起一次调用(或重试的一次 attempt),拿一个 `CallAttemptTracer`;这次 attempt 结束(成功/失败/取消)时,调 `RecordEnd`,把这次 attempt 的延迟、状态码、方法名等记下来。服务端同理,每次 server call 拿一个 `ServerCallTracer`,call 结束时 `RecordEnd`。

`instrument.h`(L51-62)定义了通用的指标原语:Counter(计数)、Histogram(直方图,如延迟分布)、Gauge(瞬时值)。所有内置指标在 `stats_data.yaml` 里声明式定义,用 `tools/codegen/core/gen_stats_data.py` 生成 `stats_data.h`。这是 gRPC 的"指标中央仓库"。

### 后端 1:OpenTelemetry(otel,现代标准)

OpenTelemetry 是 CNCF 主推的可观测标准(gRPC 内部仍兼容老的 census,但新代码主推 otel)。它的集成在 `src/cpp/ext/otel/`:

```cpp
// src/cpp/ext/otel/otel_plugin.h:126, 190
class OpenTelemetryPluginBuilderImpl {
  void SetMeterProvider(...);           // L131 设指标后端
  void SetTracerProvider(...);          // L177 设 trace 后端
  void SetTextMapPropagator(...);       // L181 设 trace 上下文传播(W3C TraceContext 等)
  void EnableMetrics(); / DisableMetrics();   // L143/145
  void BuildAndRegisterGlobal();        // L190 注册全局插件
};
```

otel 的注册入口是 `BuildAndRegisterGlobal()`(`otel_plugin.cc:269`)。注意它**不直接注册 filter**,而是构造一个 `OpenTelemetryPluginImpl`,通过 channel args + StatsPlugin + CallTracer 工厂机制织入。具体到每次调用,otel 给客户端 call 配 `OpenTelemetryPluginImpl::ClientCallTracerInterface`(`otel_client_call_tracer.h:48`),里面有个 `CallAttemptTracer`(L52)持有一个 `opentelemetry::trace::Span span_`(L134)——每次 attempt 开始 create span,`RecordEnd`(L104)时 end span 并打上方法名、状态码、目标等属性。服务端同理(`otel_server_call_tracer.h:33`)。

trace 上下文怎么跨服务传?靠元数据里的 `grpc-trace-bin`(或 W3C 的 `traceparent`)。otel 的 `LabelsInjector`(`otel_plugin.h:74`)把当前 span 的 trace-id 注入到出站 metadata,服务端解析时提取出来,作为自己 span 的 parent。**这条 trace 链,就是顺着元数据一路传的**——这是为什么本章要把"元数据"和"otel"放一起讲:trace 的串联,根上靠的就是 metadata_batch 里那个 `GrpcTraceBinMetadata` trait。

### 后端 2:OpenCensus(census,Google 老的)

OpenCensus 是 Google 早年的遥测库,otel 出来后它逐渐被取代,但 gRPC 还保留着它的集成。这里有个**重要的源码修正**:census 的 filter 不在 `src/core/ext/filters/census/`(那个目录只剩个 legacy 的 `grpc_context.cc`),而在 **`src/cpp/ext/filters/census/`**(C++ 层):

```cpp
// src/cpp/ext/filters/census/grpc_plugin.cc:41
void RegisterOpenCensusPlugin() {
  ServerCallTracerFactory::RegisterGlobal(...);    // L42 注册服务端 call tracer 工厂
  CoreConfiguration::RegisterEphemeralBuilder([](Builder* builder) {
    builder->RegisterFilter(GRPC_CLIENT_CHANNEL, &OpenCensusClientFilter::kFilter)  // L47
           .Before<ClientLoggingFilter>();          // L48 织入 client channel filter 栈
  });
}
```

它走两条路:**服务端用 ServerCallTracerFactory(L42),客户端用 filter(L47)**。census 用的 trace 库是 `opencensus/trace/span.h`,生成 `Span` 和 `TagKey`(`grpc_plugin.cc:89-109` 定义了 `grpc_client_method` / `grpc_client_status` / `grpc_server_method` / `grpc_server_status` 这些 tag)。

### census 和 otel 是什么关系

关键修正一个常见误解:**census 不是 otel 的桥,它们是两套并行、互相独立的遥测后端集成**。

- **OpenCensus**(`src/cpp/ext/filters/census/`):Google 早期遥测库,依赖外部 opencensus-cpp。
- **OpenTelemetry**(`src/cpp/ext/otel/`):CNCF 现代标准,不依赖 census。
- 两者**都建立在 `src/core/telemetry/` 的通用 CallTracer / Stats 接口之上**,共用同一套插入点(call attempt 生命周期),但各自实现自己的 tracer / span / 指标导出。

> **钉死这件事**:gRPC 的可观测后端是"一个通用出口 + 多个后端"的设计。通用出口是 `src/core/telemetry/` 的 CallTracer(记录 call attempt 生命周期);后端是 otel(主推)和 census(兼容遗留),各自实现 tracer 并织入。新项目用 otel,老项目可能还在 census,但底下的 CallTracer 接口是同一套。读源码别在 `src/core/ext/filters/census/` 里找 filter——那个目录只剩 `grpc_context.cc` 残留,真身在 `src/cpp/ext/filters/census/`。

---

## 四、技巧精解:metadata_batch 凭什么比 map 快

本章最硬的技巧,是 metadata_batch 那套"类型化 + 编译期打表"的设计。我们把它单独拆透。

### 朴素方案:map<string,string>

假设我们用最朴素的 `std::map<std::string, std::string>` 表示元数据。一次调用的元数据处理,要做这些事:

1. **HPACK 解码**:每收到一个 header,`map["grpc-timeout"] = "5S"`——一次 string 构造(可能分配)+ 一次红黑树插入(比较 key 字符串)。
2. **读已知 header**:filter 栈里要拿 timeout,`auto it = map.find("grpc-timeout")`——一次哈希/比较。`grpc-status`、`grpc-encoding` 各一次。十几个已知 header,就是十几次字符串查找。
3. **解析值**:`Duration timeout = ParseTimeout(it->second)`——每次都把 `"5S"` 解析成 `Duration`。同样的事在十几个 filter 里重复做。
4. **传递**:metadata 要从 filter A 传到 filter B,要么 `map copy`(深拷贝每个 string,慢),要么共享指针 + 锁(更慢)。
5. **HPACK 编码**(发送侧):把 metadata 重新序列化成 header 文本,又一次遍历。

这一套下来,一次调用在元数据上的开销 = 字符串分配 + 十几次查找 + 十几次重复解析 + 拷贝。在海量 QPS 下,这是实打实的 CPU 浪费。

### metadata_batch 的方案:类型化 + 编译期表

gRPC 的方案分三步反击:

**第一步:每个已知 header 对应一个 trait(类型化)**。`GrpcTimeoutMetadata` trait 的 `ValueType` 是 `Duration`,而不是 string。这意味着:**解析只发生一次**——HPACK 解码时,`MetadataMap::Parse`(`metadata_batch.h:1662`)经编译期生成的 `NameLookup` 分派表找到 `GrpcTimeoutMetadata`,调它的 `ParseMemento` 把 `"5S"` 变成 `Duration` 存进表里。从这以后,所有 filter 拿到的都是 `Duration`,再也不碰字符串。

```cpp
// 简化示意:编译期分派
auto value = metadata_batch.Parse("grpc-timeout", slice);  // 触发 ParseMemento,得到 Duration
metadata_batch.Set(GrpcTimeoutMetadata{}, value);          // 存进 GrpcTimeoutMetadata 槽位
...
// 后续 filter 拿 timeout,直接是 Duration
std::optional<Duration> timeout = metadata_batch.get(GrpcTimeoutMetadata{});  // L1575,零字符串查找
```

第二步反击:**已知 header 打成编译期紧凑表(PackedTable)**。这是 `metadata_batch.h:1707` 的 `table_` 成员。PackedTable 不是哈希表,是按 trait 类型在编译期生成的一维结构,每个 trait 一个固定槽位。访问 `GrpcTimeoutMetadata` 就是访问它的槽位,`O(1)`、无字符串比较、缓存友好。十几个已知 header 的读取,从十几次字符串查找变成十次数组下标。

第三步反击:**未知 header 才退化成 vector**。用户自定义的、gRPC 不认识的 header(`x-custom-header`),走 `UnknownMap = vector<pair<Slice, Slice>>`(L1238)。但这是冷路径,生产 header 几乎都有 trait。而且,即使用 vector 存,value 也是 Slice(下一章 P6-21 的零拷贝载体),不是裸 string,移动起来也是挪指针。

### 三个动作的合力

| 操作 | 朴素 map | metadata_batch |
|------|---------|----------------|
| 读已知 header | `map.find("grpc-timeout")` 字符串哈希/比较 | `table_[GrpcTimeoutMetadata槽位]` 数组下标 |
| 解析值 | 每次 `ParseTimeout("5S")` 重复解析 | HPACK 解码时一次解析,后续直接拿 `Duration` |
| 传递 | 深拷贝 map 或加锁共享 | move 句柄,只挪指针 |
| 内存 | 每个 string 独立分配 | 从 arena 分配,call 结束整体释放 |

> **钉死这件事**:metadata_batch 比朴素 map 快的根,是三个动作的合力——**类型化(解析只一次)+ 编译期打表(零字符串查找)+ 移动语义(零拷贝传递)**。这套设计把"传几十个 header"这件每次调用都做的事,压到了热路径上几乎零分配。这是 gRPC 在元数据层做出高性能的关键,也是它区别于"用 map 装 header"的朴素 RPC 框架的根。

---

## 五、章末小结

### 回扣主线

本章服务二分法的**可用 / 可观测**那一面。它讲了 gRPC 在"把网络字节变回可调用、可治理的方法"这条框架层主线上,最后两块拼图:

1. **元数据**:把围绕一次调用的所有上下文(deadline、trace、鉴权、压缩协商),做成类型化的 metadata_batch,让它能在 filter 栈里零拷贝、零重复解析地传递。这是"调用怎么携带上下文"的答案。
2. **channelz + CallTracer**:把运行时的活体状态(channel/SubChannel/socket 树)和每次调用的指标(延迟、状态码),通过两个出口暴露出去——channelz 是按需查询的诊断树,CallTracer 是持续导出的指标流。这是"调用怎么被观测"的答案。

### 五个为什么

1. **为什么元数据不用 map<string,string>?**——因为 map 的字符串查找、重复解析、深拷贝,在海量 QPS 下是实打实的浪费;metadata_batch 用类型化 trait + 编译期 PackedTable,把热路径压到零查找零重复解析。
2. **为什么 metadata_batch 要分已知 header 和未知 header 两路?**——已知 header 集合有限且固定,值得编译期打表;未知 header(用户自定义)无法预知,只能退化成 vector 兜底。两路分开,热路径走表,冷路径兜底。
3. **为什么 channelz 内部是 v2 却保留 v1 兼容壳?**——v2(protobuf)是真身,但大量现有工具调 v1(JSON)API,直接砍会破坏兼容;v2tov1/ 桥接层让新真身就位、老接口慢慢退役,这是大型系统演进的稳妥套路。
4. **为什么 census 和 otel 是两套独立集成,不是一个桥?**——census(老)和 otel(新)是两个不同时代的遥测库,各自有完整的 tracer/span/指标体系;gRPC 让它们都挂在通用 CallTracer 接口下,而不是互相桥接,避免耦合。
5. **为什么 trace-id 能跨服务串联?**——因为它跟着元数据走(元数据里的 `grpc-trace-bin` 或 W3C `traceparent` 头),otel/census 在出站时注入、入站时提取,span 的 parent-child 关系就这么顺着元数据一路传。

### 想继续深入往哪钻

- 想看 metadata_batch 的 trait 全集:读 `src/core/call/metadata_batch.h` 的 L1737-1761(30 来个 trait 的声明)。
- 想看 PackedTable 实现:读 `src/core/util/packed_table.h`。
- 想看 channelz 怎么用:写一个 server 注册 `ChannelzServicePlugin`,用 `grpc_cli channelz GetTopChannels` 拉树。
- 想看 channelz v1↔v2 桥:读 `src/core/channelz/v2tov1/convert.cc`。
- 想接入 otel:读 `src/cpp/ext/otel/otel_plugin.cc` 的 `BuildAndRegisterGlobal`(L269),配 OpenTelemetry SDK 的 MeterProvider/TracerProvider。
- 想看 census 的 filter 织入点:读 `src/cpp/ext/filters/census/grpc_plugin.cc:41` 的 `RegisterOpenCensusPlugin`。

### 引出下一章

元数据和 message 都从 arena 分配、都用 slice 零拷贝传递——本章多次提到这两点,但没展开。那 arena 到底怎么做到"一次 call 内分配无回收"?slice 的引用计数又怎么让消息穿十层 filter 不拷字节?这正是 gRPC 性能招牌的核心。下一章 P6-21,我们钻进 gRPC core 最硬的性能基础设施:**slice 引用计数零拷贝** + **arena 一次性分配无回收** + **压缩**,拆到源码级,配 ASCII 框图和反例,讲清 gRPC 跨网络跨语言还这么快的根。

> **下一章**:[P6-21 · 性能:slice、arena、零拷贝、压缩](P6-21-性能-slice-arena-零拷贝-压缩.md)
