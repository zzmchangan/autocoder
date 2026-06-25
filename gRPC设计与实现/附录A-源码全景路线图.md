# 附录 A · gRPC C++ core 源码全景路线图

> 这个附录是读者**读 gRPC 源码的导航**。它给一张"一次调用从 surface API 到字节的完整全栈地图",把每一层的关键文件、一句话作用、架构归属(经典 / 新 Promise)标清楚,并给出推荐的源码阅读顺序。
>
> 全书正文 23 章是"把每块讲到源码级",这个附录是"先把地图给你,让你知道每块在地图的哪一格"。读正文时迷路了,回到这张图定位;读完正文想精读源码了,按这里的顺序下钻。
>
> **源码版本**:本书所有引用钉死在 `grpc/grpc` commit [`2195e869`](https://github.com/grpc/grpc/commit/2195e8698d32980b4e73b6ccc13f6b34414463f9)(版本 `1.83.0-dev`,见 [`include/grpcpp/version_info.h:25`](../grpc/include/grpcpp/version_info.h#L25))。本地 clone 在 `../grpc/`。引用格式 `[描述](../grpc/路径#L起-L止)`,VSCode 可点击跳转。
>
> **⚠️ 架构演进**:gRPC core 正处经典(callback + completion queue + filter stack)→ 新(Promise-based + call spine + filter fusion)迁移期。地图里每层都标注【经典】或【新 Promise】或【两者并存】,带 `*_legacy` 后缀的是经典残留。读源码时分清两套,不要拿讲老 callback API 的博客当依据。

---

## 一、一次调用的全栈地图

下面这张图,是一次 gRPC unary 调用从客户端 surface API 到字节过线、再到服务端 surface API 收到的**完整旅程**。每一行是一个层,左侧标注它在"协议层 / 框架层"二分法的哪一面,右侧标注架构归属。

```
   一次 gRPC 调用的全栈旅程(commit 2195e869,1.83.0-dev)

   【客户端】                                    架构归属
   ┌──────────────────────────────────────────────────────────────────┐
   │ 业务代码:stub->GetUser(req)                                      │
   │   ↕                                                              │
   │ 【框架·surface API】src/cpp/client/                                │
   │   Channel、ClientContext、stub 的 C++ 封装                         │  C++ 表层
   │     channel_cc.cc / client_context.cc / client_callback.cc        │
   │   ↕                                                              │
   │ 【框架·channel】src/core/lib/surface/channel.cc + lib/channel/     │  两者并存
   │   Channel 是 filter 栈的容器,初始化时把 filter 串成栈             │
   │   ↕                                                              │
   │ 【框架·枢纽】src/core/client_channel/client_channel.cc             │  两者并存
   │   ★ client_channel filter:框架层通往协议层的枢纽                  │
   │   它把 resolver、balancer、SubChannel 接入,把 call 派发出去       │
   │   ↕                                                              │
   │ 【框架·call】src/core/call/                                        │  新 Promise 主
   │   call_spine.cc        —— call 主干,promise 编排                 │  【新】
   │   call_filters.cc      —— filter 栈运行时驱动                     │  两者并存
   │   filter_fusion.h      —— filter 编译期融合                        │  【新·招牌】
   │   client_call.cc       —— 客户端 call                              │
   │   server_call.cc       —— 服务端 call                              │
   │   metadata_batch.cc    —— 一次打包多个 header                      │
   │   message.cc           —— 一条消息(slice 容器)                   │
   │   interception_chain.cc —— 拦截链                                 │
   │   call_arena_allocator.cc —— call 级 arena 分配器                 │
   │   ↕                                                              │
   │ 【框架·filter】src/core/filter/ + src/core/ext/filters/            │  两者并存
   │   filter_chain.h / fused_filters.cc / composite/  (框架)          │
   │   各 filter(ext/filters/):                                       │
   │     rbac(鉴权)、fault_injection(故障注入)、                      │
   │     message_size(大小校验)、logging(日志)、                      │
   │     census/otel(遥测)、channel_idle(空闲管理)、                  │
   │     stateful_session、http(HTTP 语义)、                            │
   │     backend_metrics、load_reporting、gcp_authentication            │
   │   ↕                                                              │
   │ 【治理·resolver】src/core/resolver/                                │
   │   dns/ sockaddr/ xds/ google_c2p/ fake/  (各 scheme)              │
   │   polling_resolver.cc   —— 异步解析的通用基类                      │
   │   resolver_registry.cc  —— scheme 注册表                          │
   │   endpoint_addresses.cc —— 解析结果(地址列表)                    │
   │   ↕  把名字变地址                                                 │
   │ 【治理·load balancing】src/core/load_balancing/                    │
   │   lb_policy.cc          —— LB 策略基类(SubchannelPicker)          │
   │   pick_first/ round_robin/ weighted_round_robin/ ring_hash/        │
   │   rls/ xds/ outlier_detection/ grpclb/ priority/ weighted_target/ │
   │   ★ 注意:C 核心没有 least_request(Java/Go 才有)                 │
   │   ↕  Picker 选一个 SubChannel                                     │
   │ 【治理·SubChannel】src/core/client_channel/                        │
   │   subchannel.cc                —— SubChannel 本体(状态机)         │
   │   global_subchannel_pool.cc    —— 全局复用池                       │
   │   local_subchannel_pool.cc     —— channel 级池                     │
   │   subchannel_pool_interface.cc —— 池接口                          │
   │   load_balanced_call_destination.cc —— 【新 Promise】call 目的地   │
   │   subchannel_stream_client.cc  —— 复用流探健康/health check        │
   │   ↕                                                              │
   │ 【衔接·connector 建连】src/core/client_channel/connector.h         │
   │   实现见 src/core/ext/transport/chttp2/client/chttp2_connector.cc │
   │   ↢ TCP + TLS 握手(hook handshaker)                              │
   │   ↕                                                              │
   │ 【协议·chttp2 transport】src/core/ext/transport/chttp2/transport/  │  ★全书最硬
   │   ┌── 经典形态 ──────────────────────────────────────────────┐    │
   │   │ chttp2_transport.cc (3801 行!)  transport 主体              │    │  【经典】
   │   └────────────────────────────────────────────────────────────┘    │
   │   ┌── 新 Promise 形态 ────────────────────────────────────────┐    │
   │   │ http2_client_transport.cc + http2_server_transport.cc      │    │  【新】
   │   │   (BDP 流控还没接,8 秒定时器占位,见 P2-09)              │    │
   │   └────────────────────────────────────────────────────────────┘    │
   │   HPACK(招牌,P2-07):                                             │
   │     hpack_encoder.cc + hpack_encoder_table.cc  (编码端,只存 size) │
   │     hpack_parser.cc + hpack_parser_table.cc    (解码端,存 memento)│
   │     hpack_constants.h + hpack_tables.txt       (静态表 61 项)      │
   │     decode_huff.cc + huffsyms.cc               (Huffman 多级查表) │
   │     bin_encoder.cc / bin_decoder.cc            (-bin 二进制头)     │
   │   流控(招牌,P2-09):                                             │
   │     flow_control.cc + flow_control.h / flow_control_manager.h     │
   │     ping_rate_policy.cc / ping_abuse_policy.cc  (PING 限速/防滥用) │
   │     write_size_policy.cc                        (攒批上限动态调)   │
   │   帧处理(P2-05/08):                                              │
   │     frame.cc / frame.h                          (帧通用 + 5字节头) │
   │     frame_data.cc frame_settings.cc frame_ping.cc                  │
   │     frame_goaway.cc frame_rst_stream.cc frame_window_update.cc     │
   │     frame_security.cc                                              │
   │     parsing.cc / writing.cc / write_cycle.cc   (读/写状态机)       │
   │     keepalive.cc                               (keepalive,P5-17)  │
   │     http2_settings.cc / http2_settings_manager.cc                  │
   │     varint.cc                                  (HTTP/2 varint)     │
   │   ↕                                                              │
   │ 【协议·BDP 估计】src/core/lib/transport/bdp_estimator.cc           │  跨 transport 复用
   │   (经典 transport 用;Promise 版还没接)                           │
   │   ↕                                                              │
   │ 【性能基础设施·slice/arena】                                       │  ★招牌 P6-21
   │   src/core/lib/slice/slice.cc + slice_refcount.h  (零拷贝核心)    │
   │   src/core/lib/slice/slice_buffer.cc                              │
   │   src/core/lib/resource_quota/arena.cc  (底层 arena)              │
   │   src/core/call/call_arena_allocator.cc (call 级 arena 封装)      │
   │   ↕                                                              │
   │ 【经典·CQ】src/core/lib/surface/completion_queue.cc                │  【经典】
   │          + filter_stack_call.cc (StartBatch 真身)                 │
   │   ↕  call.cc 是薄包装                                            │
   │   字节过线(HTTP/2 帧)                                            │
   └──────────────────────────────────────────────────────────────────┘
                            ↓ HTTP/2 over TCP(+TLS) ↓
   ┌──────────────────────────────────────────────────────────────────┐
   │ 【服务端】对称镜像                                                 │
   │   chttp2 transport(server 版)解析帧 → filter 栈 → handler         │
   │   src/cpp/server/server_cc.cc(注册 service)                       │
   │   src/cpp/server/health/(健康检查 P5-18)                           │
   │   src/cpp/ext/proto_server_reflection.cc(反射 P5-18)              │
   └──────────────────────────────────────────────────────────────────┘

   【横切(每层都可能用到)】
   【安全·tsi】src/core/tsi/(transport_security_interface.h 接口)       P5-19
     ssl_transport_security.cc / alts/ / local_transport_security.cc
     + src/core/credentials/(channel/call creds)+ src/core/handshaker/
   【可观测】src/core/channelz/(channelz.cc + channelz_registry.cc       P6-20
     + channel_trace.cc + v2tov1/ 桥 + zviz/) 【新 v2 → 老 v1】
     src/core/telemetry/(call_tracer.cc ...) + src/cpp/ext/otel/
   【xDS】src/core/xds/(grpc/ + xds_client/)                             P6-22
     + src/core/resolver/xds/(xds_resolver.cc + xds_config.cc)
     + src/core/load_balancing/xds/
     bootstrap:src/core/xds/grpc/xds_bootstrap_grpc.cc +
                src/core/xds/xds_client/xds_bootstrap.cc
   【新 transport 押注】src/core/ext/transport/chaotic_good/             P3 篇提及
     (控制平面/数据平面分离,client_transport.cc + server_transport.cc)
   【legacy 残留(读源码时分清)】
     src/core/client_channel/retry_filter_legacy_call_data.cc           P4-16
     src/core/ext/filters/channel_idle/legacy_channel_idle_filter.cc
     src/core/ext/transport/inproc/legacy_inproc_transport.cc
     src/core/channelz/v2tov1/(新 v2 → 老 v1 桥)
```

---

## 二、各层关键文件与一句话作用

把地图里每层的关键文件展开,标清楚一句话作用 + 所属章 + 架构归属。

### 2.1 surface API(框架层表层)

C++ 用户直接接触的 API。这一层是薄封装,把 C core 的能力包成 C++ 对象。

| 文件 | 作用 | 章 |
|------|------|----|
| [`src/cpp/client/channel_cc.cc`](../grpc/src/cpp/client/channel_cc.cc) | Channel 的 C++ 封装(创建、参数传递) | P3 |
| [`src/cpp/client/client_context.cc`](../grpc/src/cpp/client/client_context.cc) | ClientContext(超时、metadata、取消) | P1-04 |
| [`src/cpp/client/client_callback.cc`](../grpc/src/cpp/client/client_callback.cc) | callback 模式客户端 API | P3-10 |
| [`src/cpp/server/server_cc.cc`](../grpc/src/cpp/server/server_cc.cc) | Server 构建、service 注册 | P1-04 |
| [`include/grpcpp/version_info.h`](../grpc/include/grpcpp/version_info.h#L25) | 版本宏(`1.83.0-dev`,行 25) | — |

### 2.2 channel(框架层,filter 栈容器)

Channel 是 gRPC 最核心的抽象之一:它是 filter 栈的容器,初始化时把一串 filter 串成栈,所有 call 都穿过这条栈。

| 文件 | 作用 | 章 | 架构 |
|------|------|----|------|
| [`src/core/lib/surface/channel.cc`](../grpc/src/core/lib/surface/channel.cc) | Channel 创建、filter 栈初始化 | P3-11 | 两者并存 |
| `src/core/lib/channel/`(目录) | channel_stack.h、channel_args 等 filter 框架 | P3-11 | 两者并存 |

### 2.3 client_channel(框架层枢纽,通往协议层的桥)

整个客户端最关键的一个 filter——它把 resolver、balancer、SubChannel 接入,把 call 派发到具体的 SubChannel。P4 篇四章都围着它转。

| 文件 | 作用 | 章 | 架构 |
|------|------|----|------|
| [`src/core/client_channel/client_channel.cc`](../grpc/src/core/client_channel/client_channel.cc) | client_channel filter 本体(枢纽) | P4-13 | 两者并存 |
| [`src/core/client_channel/client_channel_filter.h`](../grpc/src/core/client_channel/client_channel_filter.h) | filter 接口 | P3-11 | — |

### 2.4 call(框架层核心,新 Promise 主战场)

一次方法调用的抽象。新架构下 call 用 call spine(promise 编排)组织,P3-12 主讲。

| 文件 | 作用 | 章 | 架构 |
|------|------|----|------|
| [`src/core/call/call_spine.cc`](../grpc/src/core/call/call_spine.cc) | call 主干,promise 编排 | P3-12 | 【新】 |
| [`src/core/call/call_filters.cc`](../grpc/src/core/call/call_filters.cc) | filter 栈运行时驱动 | P3-11 | 两者并存 |
| [`src/core/call/filter_fusion.h`](../grpc/src/core/call/filter_fusion.h) | ★ filter 编译期融合(招牌) | P3-11 | 【新·招牌】 |
| [`src/core/call/client_call.cc`](../grpc/src/core/call/client_call.cc) | 客户端 call | P3-12 | 【新】 |
| [`src/core/call/server_call.cc`](../grpc/src/core/call/server_call.cc) | 服务端 call | P3-12 | 【新】 |
| [`src/core/call/metadata_batch.cc`](../grpc/src/core/call/metadata_batch.cc) | metadata 打包成一批 | P6-20 | — |
| [`src/core/call/message.cc`](../grpc/src/core/call/message.cc) | 一条消息(slice 容器) | P6-21 | — |
| [`src/core/call/interception_chain.cc`](../grpc/src/core/call/interception_chain.cc) | 拦截链 | P3-11 | 【新】 |
| [`src/core/call/call_arena_allocator.cc`](../grpc/src/core/call/call_arena_allocator.cc) | call 级 arena 分配器 | P6-21 | — |

### 2.5 filter 框架 + 各 filter(框架层招牌)

横切关注点的载体。P3-11 主讲 filter stack + filter fusion。

| 文件 | 作用 | 章 |
|------|------|----|
| `src/core/filter/filter_chain.h` / `fused_filters.cc` / `composite/` | filter 框架 | P3-11 |
| `src/core/ext/filters/http/` | HTTP 语义 filter(`:path` → 方法名) | P3-11 |
| `src/core/ext/filters/rbac/` | 鉴权 filter | P3-11/P5-19 |
| `src/core/ext/filters/fault_injection/` | 故障注入 filter | P3-11 |
| `src/core/ext/filters/message_size/` | 消息大小校验 filter | P3-11 |
| `src/core/ext/filters/census/` | census 遥测 filter | P6-20 |
| `src/core/ext/filters/channel_idle/` | 空闲连接管理(有 legacy 版) | — |
| `src/core/ext/filters/stateful_session/` | 有状态会话 filter | — |
| `src/core/ext/filters/backend_metrics/` | 后端指标 filter | P6-20 |
| `src/core/ext/filters/load_reporting/` | 负载上报 filter | P6-20 |
| `src/core/ext/filters/gcp_authentication/` | GCP 鉴权 filter | P5-19 |

### 2.6 resolver(治理层,名字到地址)

P4-13 主讲。把 `dns:///user-svc` 变成 `[ip:port]` 列表,顺带下发 service config。

| 文件 | 作用 | 章 |
|------|------|----|
| `src/core/resolver/dns/` | DNS 解析(最常用) | P4-13 |
| `src/core/resolver/sockaddr/` | 直连 IP(sockaddr scheme) | P4-13 |
| `src/core/resolver/xds/` | xDS 解析器 + [`xds_resolver.cc`](../grpc/src/core/resolver/xds/xds_resolver.cc) + [`xds_config.cc`](../grpc/src/core/resolver/xds/xds_config.cc) | P6-22 |
| `src/core/resolver/google_c2p/` | Google C2P scheme | — |
| `src/core/resolver/fake/` | 测试用 fake scheme | — |
| [`src/core/resolver/polling_resolver.cc`](../grpc/src/core/resolver/polling_resolver.cc) | 异步解析通用基类 | P4-13 |
| [`src/core/resolver/resolver_registry.cc`](../grpc/src/core/resolver/resolver_registry.cc) | scheme 注册表 | P4-13 |
| [`src/core/resolver/endpoint_addresses.cc`](../grpc/src/core/resolver/endpoint_addresses.cc) | 解析结果(地址列表) | P4-13 |

> **注意**:全树无 `passthrough` scheme 目录(本书 §1 的"无 passthrough scheme")。

### 2.7 load balancing(治理层招牌)

P4-15 主讲。控制平面/数据平面分离,Picker 用无锁 fast path 每次调用选一个 SubChannel。

| 文件 | 作用 | 章 |
|------|------|----|
| [`src/core/load_balancing/lb_policy.cc`](../grpc/src/core/load_balancing/lb_policy.cc) | LB 策略基类(`SubchannelPicker`) | P4-15 |
| `src/core/load_balancing/pick_first/` | pick_first 策略(默认) | P4-15 |
| `src/core/load_balancing/round_robin/` | round_robin 策略(无锁 fast path) | P4-15 |
| `src/core/load_balancing/weighted_round_robin/` | 加权轮询 | P4-15 |
| `src/core/load_balancing/ring_hash/` | 一致性哈希(粘性会话) | P4-15 |
| `src/core/load_balancing/rls/` | RLS(Routing Lookup Service) | P4-15 |
| `src/core/load_balancing/xds/` | xDS 下发的 LB 策略 | P6-22 |
| `src/core/load_balancing/outlier_detection/` | 异常节点剔除 | P4-15 |
| `src/core/load_balancing/grpclb/` | gRPC-LB 协议(老) | P4-15 |
| `src/core/load_balancing/priority/` | 优先级(故障转移) | P4-15 |
| `src/core/load_balancing/weighted_target/` | 加权目标(配合 priority) | P4-15 |

> **★ C 核心没有 `least_request` 策略**(全树 grep 零命中),Java/Go 才有。要类似效果用 `weighted_round_robin`。

### 2.8 SubChannel(治理层衔接)

P4-14 主讲。一个后端实例抽象成可建连/复用/重连的 SubChannel,多个 channel 共享连接。

| 文件 | 作用 | 章 |
|------|------|----|
| [`src/core/client_channel/subchannel.cc`](../grpc/src/core/client_channel/subchannel.cc) | SubChannel 本体(连接状态机) | P4-14 |
| [`src/core/client_channel/global_subchannel_pool.cc`](../grpc/src/core/client_channel/global_subchannel_pool.cc) | 全局复用池 | P4-14 |
| [`src/core/client_channel/local_subchannel_pool.cc`](../grpc/src/core/client_channel/local_subchannel_pool.cc) | channel 级池 | P4-14 |
| [`src/core/client_channel/subchannel_pool_interface.cc`](../grpc/src/core/client_channel/subchannel_pool_interface.cc) | 池接口 | P4-14 |
| [`src/core/client_channel/load_balanced_call_destination.cc`](../grpc/src/core/client_channel/load_balanced_call_destination.cc) | 【新 Promise】call 目的地在 LB 之后 | P4-15 | 【新】 |
| [`src/core/client_channel/subchannel_stream_client.cc`](../grpc/src/core/client_channel/subchannel_stream_client.cc) | 复用流探健康/health check | P5-18 |
| [`src/core/client_channel/connector.h`](../grpc/src/core/client_channel/connector.h) | 建连 connector 接口 | P4-14 |

### 2.9 connector 建连(衔接层)

把 SubChannel 的"我要一条连接"变成实际的 TCP + TLS 握手。

| 文件 | 作用 | 章 |
|------|------|----|
| [`src/core/ext/transport/chttp2/client/chttp2_connector.cc`](../grpc/src/core/ext/transport/chttp2/client/chttp2_connector.cc) | chttp2 建连(实现 connector.h) | P4-14 |

### 2.10 chttp2 transport(协议层招牌,全书最硬)

第 2 篇五章主场。gRPC C++ core 自己用 C 实现的一整套 HTTP/2。

#### 2.10.1 transport 主体

| 文件 | 作用 | 章 | 架构 |
|------|------|----|------|
| [`src/core/ext/transport/chttp2/transport/chttp2_transport.cc`](../grpc/src/core/ext/transport/chttp2/transport/chttp2_transport.cc) | 经典 transport 主体(**3801 行**) | P2-06 | 【经典】 |
| [`src/core/ext/transport/chttp2/transport/http2_client_transport.cc`](../grpc/src/core/ext/transport/chttp2/transport/http2_client_transport.cc) | Promise 版客户端 transport(BDP 还没接) | P2-06/09 | 【新】 |
| [`src/core/ext/transport/chttp2/transport/http2_server_transport.cc`](../grpc/src/core/ext/transport/chttp2/transport/http2_server_transport.cc) | Promise 版服务端 transport | P2-06 | 【新】 |

#### 2.10.2 HPACK(头部压缩招牌,P2-07)

| 文件 | 作用 |
|------|------|
| [`src/core/ext/transport/chttp2/transport/hpack_encoder.cc`](../grpc/src/core/ext/transport/chttp2/transport/hpack_encoder.cc) | 编码器(模板特化 Compressor 体系) |
| [`src/core/ext/transport/chttp2/transport/hpack_encoder_table.cc`](../grpc/src/core/ext/transport/chttp2/transport/hpack_encoder_table.cc) | 编码端动态表(只存 size) |
| [`src/core/ext/transport/chttp2/transport/hpack_parser.cc`](../grpc/src/core/ext/transport/chttp2/transport/hpack_parser.cc) | 解码器主状态机 |
| [`src/core/ext/transport/chttp2/transport/hpack_parser_table.cc`](../grpc/src/core/ext/transport/chttp2/transport/hpack_parser_table.cc) | 解码端动态表(存完整 memento) |
| [`src/core/ext/transport/chttp2/transport/hpack_constants.h`](../grpc/src/core/ext/transport/chttp2/transport/hpack_constants.h#L31) | 常量(`kLastStaticEntry = 61` 等) |
| `src/core/ext/transport/chttp2/transport/hpack_tables.txt` | 静态表数据(61 项) |
| [`src/core/ext/transport/chttp2/transport/decode_huff.cc`](../grpc/src/core/ext/transport/chttp2/transport/decode_huff.cc) | Huffman 解码表数据(4371 行) |
| `src/core/ext/transport/chttp2/transport/decode_huff.h` | Huffman 解码主逻辑(多级查表) |
| [`src/core/ext/transport/chttp2/transport/huffsyms.cc`](../grpc/src/core/ext/transport/chttp2/transport/huffsyms.cc) | Huffman 符号表(257 项) |
| [`src/core/ext/transport/chttp2/transport/bin_encoder.cc`](../grpc/src/core/ext/transport/chttp2/transport/bin_encoder.cc) | `-bin` 二进制头编码(base64+Huffman) |
| [`src/core/ext/transport/chttp2/transport/bin_decoder.cc`](../grpc/src/core/ext/transport/chttp2/transport/bin_decoder.cc) | `-bin` 二进制头解码 |

#### 2.10.3 流控(招牌,P2-09)

| 文件 | 作用 |
|------|------|
| `src/core/ext/transport/chttp2/transport/flow_control.cc` / `flow_control.h` / `flow_control_manager.h` | 双层 window + BDP + 攒批策略 |
| [`src/core/ext/transport/chttp2/transport/ping_rate_policy.cc`](../grpc/src/core/ext/transport/chttp2/transport/ping_rate_policy.cc) | 出站 PING 限速 |
| [`src/core/ext/transport/chttp2/transport/ping_abuse_policy.cc`](../grpc/src/core/ext/transport/chttp2/transport/ping_abuse_policy.cc) | 入站 PING 防滥用(strike 制度) |
| `src/core/ext/transport/chttp2/transport/write_size_policy.cc` | 攒批上限动态调 |
| [`src/core/lib/transport/bdp_estimator.cc`](../grpc/src/core/lib/transport/bdp_estimator.cc) | BDP 估计(跨 transport 复用) |

> **关键常量**(都已核实):
> - `kGrpcHeaderSizeInBytes = 5`(gRPC 消息帧头:1 字节压缩位 + 4 字节大端长度),见 [`frame.h:254`](../grpc/src/core/ext/transport/chttp2/transport/frame.h#L254)。
> - `kDefaultWindow = 65535`(INITIAL_WINDOW_SIZE 的 RFC 默认),见 [`flow_control.h:50`](../grpc/src/core/ext/transport/chttp2/transport/flow_control.h#L50) + [`frame.h:422`](../grpc/src/core/ext/transport/chttp2/transport/frame.h#L422)。**不是 16384**——16384 是 `kDefaultFrameSize`(MAX_FRAME_SIZE 默认),见 [`flow_control.h:51`](../grpc/src/core/ext/transport/chttp2/transport/flow_control.h#L51)。
> - `kFlowControlPeriodicUpdateTimer = Duration::Seconds(8)`(Promise 版 transport 的 BDP 占位定时器),见 [`flow_control.h:62`](../grpc/src/core/ext/transport/chttp2/transport/flow_control.h#L62)。

#### 2.10.4 帧处理(P2-05/08)

| 文件 | 作用 |
|------|------|
| [`src/core/ext/transport/chttp2/transport/frame.cc`](../grpc/src/core/ext/transport/chttp2/transport/frame.cc#L806) / `frame.h` | 帧通用 + gRPC 5 字节消息帧解析(`kGrpcHeaderSizeInBytes`) |
| [`src/core/ext/transport/chttp2/transport/frame_data.cc`](../grpc/src/core/ext/transport/chttp2/transport/frame_data.cc) | DATA 帧 |
| [`src/core/ext/transport/chttp2/transport/frame_settings.cc`](../grpc/src/core/ext/transport/chttp2/transport/frame_settings.cc) | SETTINGS 帧(协商) |
| [`src/core/ext/transport/chttp2/transport/frame_ping.cc`](../grpc/src/core/ext/transport/chttp2/transport/frame_ping.cc) | PING 帧(探活) |
| [`src/core/ext/transport/chttp2/transport/frame_goaway.cc`](../grpc/src/core/ext/transport/chttp2/transport/frame_goaway.cc) | GOAWAY 帧(优雅关闭) |
| [`src/core/ext/transport/chttp2/transport/frame_rst_stream.cc`](../grpc/src/core/ext/transport/chttp2/transport/frame_rst_stream.cc) | RST_STREAM 帧(异常终止/取消) |
| [`src/core/ext/transport/chttp2/transport/frame_window_update.cc`](../grpc/src/core/ext/transport/chttp2/transport/frame_window_update.cc) | WINDOW_UPDATE 帧(给信用) |
| [`src/core/ext/transport/chttp2/transport/frame_security.cc`](../grpc/src/core/ext/transport/chttp2/transport/frame_security.cc) | 安全相关帧处理 |
| `src/core/ext/transport/chttp2/transport/parsing.cc` | 读侧状态机 |
| `src/core/ext/transport/chttp2/transport/writing.cc` + `write_cycle.cc` | 写侧攒批状态机 |
| `src/core/ext/transport/chttp2/transport/keepalive.cc` | keepalive(P5-17) |
| `src/core/ext/transport/chttp2/transport/http2_settings.cc` + `http2_settings_manager.cc` | SETTINGS 管理 |
| `src/core/ext/transport/chttp2/transport/varint.cc` | HTTP/2 varint(P1-03 对照) |

### 2.11 性能基础设施:slice / arena(招牌,P6-21)

gRPC 又快又省的根:slice 用原子引用计数让字节零拷贝,arena 用 bump pointer 让临时对象分配极快。

| 文件 | 作用 | 章 |
|------|------|----|
| [`src/core/lib/slice/slice.cc`](../grpc/src/core/lib/slice/slice.cc) | slice 实现 | P6-21 |
| [`src/core/lib/slice/slice_refcount.h`](../grpc/src/core/lib/slice/slice_refcount.h) | ★ slice 引用计数核心(零拷贝根) | P6-21 |
| [`src/core/lib/slice/slice_buffer.cc`](../grpc/src/core/lib/slice/slice_buffer.cc) | slice 缓冲(多个 slice 串成链) | P6-21 |
| [`src/core/lib/resource_quota/arena.cc`](../grpc/src/core/lib/resource_quota/arena.cc) | 底层 arena(bump pointer) | P6-21 |
| [`src/core/call/call_arena_allocator.cc`](../grpc/src/core/call/call_arena_allocator.cc) | call 级 arena 封装 | P6-21 |

### 2.12 经典 CQ 与 StartBatch(经典架构)

P3-10 主讲。新架构下 call 不再走这里,但大量现存代码和文档仍是这个形态。

| 文件 | 作用 | 章 | 架构 |
|------|------|----|------|
| [`src/core/lib/surface/completion_queue.cc`](../grpc/src/core/lib/surface/completion_queue.cc) | completion queue 事件队列 | P3-10 | 【经典】 |
| [`src/core/lib/surface/filter_stack_call.cc`](../grpc/src/core/lib/surface/filter_stack_call.cc) | ★ `StartBatch` 真身(call.cc 是薄包装) | P3-10 | 【经典】 |

### 2.13 安全:tsi / credentials / handshaker(P5-19)

| 文件 | 作用 | 章 |
|------|------|----|
| [`src/core/tsi/transport_security_interface.h`](../grpc/src/core/tsi/transport_security_interface.h) | tsi 抽象接口(屏蔽 SSL/ALTS/local) | P5-19 |
| [`src/core/tsi/ssl_transport_security.cc`](../grpc/src/core/tsi/ssl_transport_security.cc) | TLS 实现 | P5-19 |
| `src/core/tsi/alts/` | ALTS(Google 内部 mTLS 替代) | P5-19 |
| `src/core/tsi/local_transport_security.cc` | 本地传输安全 | P5-19 |
| `src/core/credentials/` | channel creds(连接级)/ call creds(调用级) | P5-19 |
| [`src/core/handshaker/handshaker.cc`](../grpc/src/core/handshaker/handshaker.cc) | 握手状态机 | P5-19 |

### 2.14 可观测:channelz / telemetry / otel(P6-20)

| 文件 | 作用 | 章 |
|------|------|----|
| [`src/core/channelz/channelz.cc`](../grpc/src/core/channelz/channelz.cc) | channelz 本体(运行时诊断树) | P6-20 |
| [`src/core/channelz/channelz_registry.cc`](../grpc/src/core/channelz/channelz_registry.cc) | channelz 注册表(entity ID 分配) | P6-20 |
| [`src/core/channelz/channel_trace.cc`](../grpc/src/core/channelz/channel_trace.cc) | channel 追踪(事件日志) | P6-20 |
| `src/core/channelz/v2tov1/` | 【新 v2 → 老 v1 桥】(重构中) | P6-20 |
| `src/core/channelz/zviz/` | channelz 可视化 | P6-20 |
| `src/core/telemetry/call_tracer.cc` | 调用级遥测 | P6-20 |
| `src/cpp/ext/otel/otel_plugin.cc` | OpenTelemetry 插件 | P6-20 |
| `src/cpp/server/channelz/channelz_service.cc` | channelz 服务(C++ 端,暴露给 grpcurl 等) | P6-20 |

> **channelz 默认开启**,通过 `GRPC_ARG_ENABLE_CHANNELZ = "grpc.enable_channelz"` 控制,见 [`include/grpc/impl/channel_arg_names.h:258`](../grpc/include/grpc/impl/channel_arg_names.h#L258)。

### 2.15 xDS 与服务网格(P6-22)

| 文件 | 作用 | 章 |
|------|------|----|
| `src/core/xds/grpc/xds_bootstrap_grpc.cc` | gRPC bootstrap 解析 | P6-22 |
| `src/core/xds/xds_client/xds_bootstrap.cc` | bootstrap 抽象 | P6-22 |
| `src/core/xds/xds_client/xds_api.cc` | xDS API(LDS/RDS/CDS/EDS) | P6-22 |
| `src/core/xds/grpc/` | gRPC 特化的 xDS 资源 | P6-22 |
| [`src/core/resolver/xds/xds_resolver.cc`](../grpc/src/core/resolver/xds/xds_resolver.cc) | xDS resolver | P6-22 |
| `src/core/load_balancing/xds/` | xDS 下发的 LB 策略 | P6-22 |

### 2.16 新 transport 押注:chaotic_good(P3 篇提及)

gRPC 针对未来"海量数据 + 高 BDP 网络"的押注——控制平面/数据平面分离到不同 TCP 连接。

| 文件 | 作用 |
|------|------|
| [`src/core/ext/transport/chaotic_good/chaotic_good.cc`](../grpc/src/core/ext/transport/chaotic_good/chaotic_good.cc) | 入口 |
| `src/core/ext/transport/chaotic_good/client_transport.cc` | 客户端 transport |
| `src/core/ext/transport/chaotic_good/server_transport.cc` | 服务端 transport |
| `src/core/ext/transport/chaotic_good/control_endpoint.cc` | 控制平面(HEADERS、流控信号) |
| `src/core/ext/transport/chaotic_good/data_endpoints.cc` | 数据平面(消息字节) |

### 2.17 legacy 残留(读源码时分清)

经典架构不会一夜消失,这些是迁移中的残留,读源码时要分清"经典 vs 新":

| 文件 | 作用 |
|------|------|
| [`src/core/client_channel/retry_filter_legacy_call_data.cc`](../grpc/src/core/client_channel/retry_filter_legacy_call_data.cc) | 重试的经典 closure 路径(P4-16) |
| [`src/core/ext/filters/channel_idle/legacy_channel_idle_filter.cc`](../grpc/src/core/ext/filters/channel_idle/legacy_channel_idle_filter.cc) | channel_idle 的经典版 |
| [`src/core/ext/transport/inproc/legacy_inproc_transport.cc`](../grpc/src/core/ext/transport/inproc/legacy_inproc_transport.cc) | inproc transport 的经典版 |
| `src/core/channelz/v2tov1/` | channelz 新 v2 → 老 v1 桥 |

---

## 三、推荐的源码阅读顺序

地图有了,但 gRPC 源码量大(光 chttp2 transport 就上千行),新手容易迷路。这里给三条推荐路线,按你的目标选。

### 3.1 路线 A:主线全景(推荐,跟着本书章节顺序)

这是"跟着本书章节走"的路线,最省力:

1. **先读 P0-01**,建立"一次方法调用 → 一条流"的主线直觉。
2. **第 1 篇契约**(`src/core/ext/transport/chttp2/transport/varint.cc` 对照 protobuf wire format):理解一份 `.proto` 怎么变字节。
3. **第 2 篇 HTTP/2**(本路线精华):按 P2-05 → P2-06 → P2-07 → P2-08 → P2-09 顺序,从帧基础 → transport 全貌 → HPACK → framing → 流控,层层递进。**这一篇不要跳着读**,依赖紧密。
4. **第 3 篇 call**:P3-10(CQ 经典)→ P3-11(filter stack + filter fusion)→ P3-12(call spine)。
5. **第 4 篇治理**:P4-13 → P4-14 → P4-15 → P4-16,看调用怎么发出去、发去哪、失败了怎么办。
6. **第 5~6 篇**:P5-17~19(可用)、P6-20~22(可观测/性能/生态)。

读完这 22 章 + 这个附录的地图,你该能在脑子里放映出一次调用的全过程。

### 3.2 路线 B:一条调用链下钻(适合"想读穿 gRPC 怎么跑"的工程师)

这是"跟着一条调用从 surface 到字节"的下钻路线,适合想真正读穿 gRPC 的工程师:

```
   channel → client_channel → subchannel → connector → chttp2

   1. src/cpp/client/channel_cc.cc          (Channel C++ 封装)
        ↓ Channel 怎么创建 filter 栈?
   2. src/core/lib/surface/channel.cc       (filter 栈初始化)
        ↓ client_channel filter 是枢纽
   3. src/core/client_channel/client_channel.cc  (★ 枢纽:resolver + balancer 接入)
        ↓ 怎么解析地址?
   4. src/core/resolver/dns/dns_resolver.cc (DNS 解析,异步回灌)
        ↓ 怎么选 SubChannel?
   5. src/core/load_balancing/round_robin/round_robin.cc  (★ Picker 无锁 fast path)
        ↓ SubChannel 怎么建连?
   6. src/core/client_channel/subchannel.cc (SubChannel 状态机)
        ↓ 建连怎么做?
   7. src/core/ext/transport/chttp2/client/chttp2_connector.cc  (TCP + TLS 握手)
        ↓ 字节怎么编成帧?
   8. src/core/ext/transport/chttp2/transport/chttp2_transport.cc  (★ transport 主体,3801 行)
        ↓ 头部怎么压缩?
   9. src/core/ext/transport/chttp2/transport/hpack_encoder.cc  (★ HPACK 编码)
        ↓ 怎么过线?
   10. (HTTP/2 over TCP)  →  对面 server.cc 解帧 → filter 栈 → handler
```

这条线读完,你能讲清"一次 `stub->GetUser(req)` 从客户端发起到服务端 handler 收到的每一步"。建议每跳配本书对应章节一起读。

### 3.3 路线 C:按招牌技巧专题(适合"想看穿某个招牌实现"的工程师)

这是"按招牌技巧"专题下钻的路线,适合已经懂 gRPC 整体、想深挖某块:

- **HPACK 招牌**:读 [`hpack_encoder.cc`](../grpc/src/core/ext/transport/chttp2/transport/hpack_encoder.cc) + [`hpack_encoder_table.cc`](../grpc/src/core/ext/transport/chttp2/transport/hpack_encoder_table.cc)(编码端只存 size)+ [`hpack_parser.cc`](../grpc/src/core/ext/transport/chttp2/transport/hpack_parser.cc) + [`hpack_parser_table.cc`](../grpc/src/core/ext/transport/chttp2/transport/hpack_parser_table.cc)(解码端存 memento)+ [`decode_huff.cc`](../grpc/src/core/ext/transport/chttp2/transport/decode_huff.cc)(Huffman 多级查表 4371 行表数据)+ 配 P2-07。
- **流控招牌**:读 `flow_control.cc`(双层 window + BDP)+ [`ping_rate_policy.cc`](../grpc/src/core/ext/transport/chttp2/transport/ping_rate_policy.cc) + [`ping_abuse_policy.cc`](../grpc/src/core/ext/transport/chttp2/transport/ping_abuse_policy.cc) + [`bdp_estimator.cc`](../grpc/src/core/lib/transport/bdp_estimator.cc) + 配 P2-09。
- **filter fusion 招牌**:读 [`filter_fusion.h`](../grpc/src/core/call/filter_fusion.h) + [`call_filters.cc`](../grpc/src/core/call/call_filters.cc)(SFINAE + TrySeq + NoInterceptor 三态剪枝)+ 配 P3-11。
- **负载均衡招牌**:读 [`lb_policy.cc`](../grpc/src/core/load_balancing/lb_policy.cc)(SubchannelPicker 基类)+ `src/core/load_balancing/round_robin/`(`fetch_add` 无锁 fast path)+ 配 P4-15。
- **重试招牌**:读 [`src/core/client_channel/retry_interceptor.cc`](../grpc/src/core/client_channel/retry_interceptor.cc)(新 Promise 主路径)+ [`retry_throttle.h`](../grpc/src/core/client_channel/retry_throttle.h)(令牌桶 `RetryThrottler` 类)+ 配 P4-16。
- **slice/arena 招牌**:读 [`slice_refcount.h`](../grpc/src/core/lib/slice/slice_refcount.h)(零拷贝核心)+ [`arena.cc`](../grpc/src/core/lib/resource_quota/arena.cc)(bump pointer)+ [`call_arena_allocator.cc`](../grpc/src/core/call/call_arena_allocator.cc) + 配 P6-21。
- **Promise 重构**:读 [`call_spine.cc`](../grpc/src/core/call/call_spine.cc)(call 主干)+ [`http2_client_transport.cc`](../grpc/src/core/ext/transport/chttp2/transport/http2_client_transport.cc)(Promise 版 transport,行 748/760/1211 看 BDP TODO)+ [`chaotic_good/`](../grpc/src/core/ext/transport/chaotic_good/)(新 transport)。

---

## 四、读源码的几个忠告

最后,给读 gRPC 源码的人几个忠告,避免踩本书写作时反复撞见的坑:

1. **不要拿讲老 callback API 的博客当唯一依据**。gRPC `src/core/` 已扁平化重构,老的 `src/core/lib/` 层级大片过时;大量博客讲的是 callback + completion queue 时代,而新版是 Promise + call spine。以本地 clone 的 commit `2195e869` 源码为准。
2. **分清经典 vs 新 Promise**。带 `*_legacy` 后缀的是经典残留;`call_spine.cc`、`filter_fusion.h`、`http2_*_transport.cc`、`load_balanced_call_destination.cc`、`chaotic_good/` 是新形态。读的时候先问"这是经典还是新",避免把两套搞混。
3. **涉及 HTTP/2 的机制,对得上 RFC**。HPACK 对 RFC 7541,帧/流控对 RFC 9113(HTTP/2 bis,2022,取代老 RFC 7540——整个 chttp2 目录对 7540 引用 0 处、对 9113 引用 19 文件)。讲错 RFC 是硬伤。
4. **关键常量以源码为准,别凭记忆**。比如 `kGrpcHeaderSizeInBytes = 5`(不是 varint,是 1 字节压缩位 + 4 字节定长大端)、`kDefaultWindow = 65535`(不是 16384)、`kLastStaticEntry = 61`(HPACK 静态表 61 项,不是 60)。这些本书各章都核实过源码。
5. **不确定精确行号时,只标文件不标行号**。gRPC 重构频繁,行号会变;本书钉死在 `2195e869`,之后的演进要自己跟。
6. **重构中的缺口要诚实**。Promise 版 transport 的 BDP 还没接(8 秒定时器占位)、hedging 尚未实现、大量 legacy 文件并存——这些不是"忘了",是"还在路上"。读源码时看到 `[PH2][P2][BDP]` 这种 TODO,就知道是 Promise transport 待补的坑。

> 这个附录是地图,不是教科书。地图的作用是让你定位——读正文迷路了回来定位,读源码卡住了回来找下一站。gRPC 是个大工程,但只要抓住"一次方法调用变成一条流"这条主线,每块在地图上的位置就清晰了。
