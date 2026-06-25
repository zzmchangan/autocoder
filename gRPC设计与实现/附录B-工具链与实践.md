# 附录 B · gRPC 工具链与实践

> 这个附录是读者**动手用 gRPC 时的操作手册**。它覆盖五块:① 构建(protoc + grpc plugin、bazel / cmake);② 调试(channelz 诊断树、grpcurl 动态调用、Wireshark 抓 HTTP/2 帧);③ benchmark(qps 工具);④ 集成(Envoy/Istio/xDS、OpenTelemetry、k8s);⑤ 线上问题排查清单(连接假活、地址不更新、负载不均、雪崩、性能、TLS 握手失败)。
>
> 它不是 API 文档的罗列,而是"遇到这类问题,该用什么工具、看哪个指标、查哪个源码/章节"。所有命令和配置示例都来自 gRPC 仓库 `examples/` 和 `test/` 下的真实用法,源码路径已在本书各章核实过(commit `2195e869`,1.83.0-dev)。
>
> **配套关系**:这个附录和[附录 A · 源码全景路线图](附录A-源码全景路线图.md)是姊妹篇——A 给"读源码的导航",B 给"动手排查的路径"。排查清单里每类问题,都指向本书对应章节 + 附录 A 的源码位置。

---

## 一、构建:protoc + grpc plugin、bazel / cmake

### 1.1 protoc + grpc plugin:从 .proto 生成 stub

gRPC 的代码生成链是:`.proto` → `protoc`(protobuf 编译器)+ `grpc_cpp_plugin`(gRPC 语言插件)→ 生成 `{name}.pb.cc`(消息序列化)+ `{name}.grpc.pb.cc`(stub)。

gRPC 各语言的 plugin 源码在 `src/compiler/`,C++ plugin 是 [`src/compiler/cpp_plugin.cc`](../grpc/src/compiler/cpp_plugin.cc)(`main()` 在行 23)。它读 `protoc` 传来的 AST,生成 service 的 stub 类、method dispatch、sync/async/callback 三套 API。

典型命令(C++ 为例):

```bash
# 编译 .proto 生成 C++ 消息 + gRPC stub
protoc --cpp_out=. --grpc_out=. \
  --plugin=protoc-gen-grpc=`which grpc_cpp_plugin` \
  -I protos/ \
  protos/helloworld.proto
```

输出两个文件:`helloworld.pb.cc/h`(消息)+ `helloworld.grpc.pb.cc/h`(stub)。

### 1.2 bazel:推荐的构建方式

gRPC 仓库本身用 bazel 构建,examples 也优先支持 bazel。helloworld 的 [`examples/cpp/helloworld/BUILD`](../grpc/examples/cpp/helloworld/BUILD) 里,客户端就是一个 `cc_binary`:

```python
cc_binary(
    name = "greeter_client",
    srcs = ["greeter_client.cc"],
    defines = ["BAZEL_BUILD"],         # 源码用这个宏切换 include 路径
    deps = [
        "//:grpc++",
        "@com_google_protobuf//:protobuf",
        "//examples/protos:hello_proto",
    ],
)
```

源码里用 `BAZEL_BUILD` 宏切换 include 路径(见 [`examples/cpp/helloworld/greeter_client.cc`](../grpc/examples/cpp/helloworld/greeter_client.cc) 的 `#ifdef BAZEL_BUILD`):

```cpp
#ifdef BAZEL_BUILD
#include "examples/protos/helloworld.grpc.pb.h"
#else
#include "helloworld.grpc.pb.h"
#endif
```

构建并运行:

```bash
# 在 grpc 仓库根目录
bazel build //examples/cpp/helloworld:greeter_server
bazel build //examples/cpp/helloworld:greeter_client

# 启动 server
bazel-bin/examples/cpp/helloworld/greeter_server

# 另一个终端,启动 client
bazel-bin/examples/cpp/helloworld/greeter_client
```

### 1.3 cmake:替代构建方式

如果项目用 cmake,examples 提供了 [`examples/cpp/helloworld/CMakeLists.txt`](../grpc/examples/cpp/helloworld/CMakeLists.txt) 作样板。它假设 protobuf 和 gRPC 已用 cmake 安装,然后用 `grpc_generate_cpp` 之类的宏生成代码并链接。完整流程见 examples/cpp/helloworld 的 README。

> **建议**:如果是新项目,优先 bazel——gRPC 主仓和 CI 都用 bazel,版本兼容性最好。如果项目已是 cmake,用 cmake 样板改。两种构建方式都完整支持 gRPC 的所有特性,选哪个看你的工程上下文。

---

## 二、调试:channelz、grpcurl、抓包

### 2.1 channelz:运行时诊断树(P6-20 主讲)

channelz 是 gRPC 内置的运行时自省机制,把 channel、SubChannel、socket 的实时状态做成一棵诊断树,通过一个特殊的 gRPC 服务暴露出来(`grpc.channelz.v1.Channelz`)。**它默认开启**,通过 channel arg `GRPC_ARG_ENABLE_CHANNELZ = "grpc.enable_channelz"` 控制(见 [`include/grpc/impl/channel_arg_names.h:258`](../grpc/include/grpc/impl/channel_arg_names.h#L258))。

#### 怎么开

C++ 服务端注册 channelz 服务:

```cpp
#include "src/cpp/server/channelz/channelz_service_plugin.h"

// main 里,builder.BuildBefore 之前
grpc::server_status::RegisterChannelzServicePlugin();
ServerBuilder builder;
// ... 注册你的业务 service ...
```

channelz 服务本体在 [`src/cpp/server/channelz/channelz_service.cc`](../grpc/src/cpp/server/channelz/channelz_service.cc)。注册后,你的 server 多了一个 `grpc.channelz.v1.Channelz` 服务,可以客户端调它查询。

#### 怎么看诊断树

channelz 暴露的 RPC 方法(都在 `grpc.channelz.v1.Channelz` 服务里):

- `GetTopChannels`:列出所有顶层 channel(包括它们的 SubChannel、socket 子节点)。
- `GetServers` / `GetServer`:列出所有 server 及其 listener。
- `GetChannel(id)`:查具体一个 channel 的状态——它的**连接状态**(IDLE/CONNECTING/READY/TRANSIENT_FAILURE)、**filter 栈**、**流控 window**、**HPACK 动态表命中率**、**trace 事件日志**(最近发生的 GOAWAY/RST_STREAM/连接重建等)。
- `GetSubchannel(id)`:查一个 SubChannel——它绑定的地址、连接尝试、当前连接状态。
- `GetSocket(id)`:查一个 socket——本地/远端地址、读写字节数、TCP 层指标。

用 grpcurl(下面 §2.2)调这些方法,能看到完整诊断树。channelz 的源码在 `src/core/channelz/`(`channelz.cc` 本体 + `channelz_registry.cc` 注册表 + `channel_trace.cc` 追踪 + `v2tov1/` 新 v2→老 v1 桥)。

> **钉死这件事**:channelz 是 gRPC 排查线上问题的第一利器。连接卡住、地址没更新、流控 window 异常、SubChannel 一直 TRANSIENT_FAILURE——这些都能从 channelz 的诊断树定位到。生产环境**务必开启**(默认就开,别关)并接入查询。

### 2.2 grpcurl:动态调用 gRPC(靠 server reflection)

HTTP/1.1 时代用 `curl` 调 REST API,gRPC 时代对应的是 [`grpcurl`](https://github.com/fullstorydev/grpcurl)——它不需要预先生成 stub,靠**服务端反射(server reflection)**动态发现 service/method,然后发起调用。

前提:服务端要开 reflection。C++ 服务端:

```cpp
#include "src/cpp/ext/proto_server_reflection_plugin.h"

grpc::reflection::InitProtoReflectionServerBuilderPlugin();
ServerBuilder builder;
// 注册业务 service + reflection
builder.RegisterService(&service);
auto server = builder.BuildAndStart();
```

reflection 实现在 [`src/cpp/ext/proto_server_reflection.cc`](../grpc/src/cpp/ext/proto_server_reflection.cc),暴露 `grpc.reflection.v1alpha.ServerReflection` 服务。

grpcurl 典型用法:

```bash
# 列出服务端所有 service
grpcurl -plaintext localhost:50051 list

# 列出某个 service 的所有 method
grpcurl -plaintext localhost:50051 list helloworld.Greeter

# 查某个 message 的 schema
grpcurl -plaintext localhost:50051 describe helloworld.HelloRequest

# 调用一个 unary method(传 JSON 参数)
grpcurl -plaintext -d '{"name":"gRPC"}' localhost:50051 helloworld.Greeter/SayHello

# 调用一个 server-streaming method(持续收响应)
grpcurl -plaintext -d '{"name":"gRPC"}' localhost:50051 helloworld.Greeter/SayHelloStream
```

`-plaintext` 表示不用 TLS(仅本地调试用,生产必加 TLS)。如果服务端没开 reflection,grpcurl 就得手动指定 proto 文件:`-proto helloworld.proto -import-path protos/`。

> **钉死这件事**:grpcurl 是 gRPC 版的 curl。本地调试一个新写的 service、验证 deployment 后能不能调通、快速复现一个 bug,grpcurl 是第一选择。前提是服务端开了 reflection(生产环境出于安全考虑可能关掉 reflection,那就得用 `-proto` 手动喂 schema)。

### 2.3 Wireshark 抓 HTTP/2 帧:看 HPACK、流控、帧的真实过线

HTTP/2 是二进制,人眼看不懂,但 **Wireshark 能解**。它会解析 9 字节帧头 + payload,把每个帧(DATA/HEADERS/SETTINGS/PING/GOAWAY/RST_STREAM/WINDOW_UPDATE)展开,甚至能解 HPACK 压缩后的头部(显示动态表索引、Huffman 解码后的文本)。

抓包步骤:

1. **启动 Wireshark,选网卡,过滤 `tcp.port == 50051`**(假设 gRPC 跑在 50051)。
2. 如果用 TLS,**Wireshark 解不了加密内容**(除非你有私钥且用了不完美的 TLS 解密配置)。本地调试建议先 `-plaintext`(明文 HTTP/2),Wireshark 能完整解。
3. 右键一条 TCP 流 → Follow → HTTP/2 Stream,Wireshark 把它解析成 HTTP/2 帧序列。

能看到的(对应本书 P2 篇):

- **客户端连上后先发 SETTINGS 帧**(协商 MAX_CONCURRENT_STREAMS、INITIAL_WINDOW_SIZE、HEADER_TABLE_SIZE 等)。注意 `INITIAL_WINDOW_SIZE` 默认是 65535(`kDefaultWindow`,不是 16384)。
- **第一条调用的 HEADERS 帧很大**(几百字节,字面量头部 + 入动态表);**第二条同样调用的 HEADERS 帧很小**(大部分命中动态表,只发索引号)——这就是 HPACK(P2-07)的真实效果。
- **DATA 帧前 5 字节是 gRPC framing**(`kGrpcHeaderSizeInBytes = 5`:1 字节压缩位 + 4 字节大端长度),见 [`frame.h:254`](../grpc/src/core/ext/transport/chttp2/transport/frame.h#L254)。
- **客户端发 PING 帧**(keepalive 探活,P5-17),服务端回 PING ACK;**服务端可能回 GOAWAY**(优雅关闭)或 RST_STREAM(异常取消某条流)。
- **WINDOW_UPDATE 帧频繁出现**——这是流控(P2-09)给信用的信号,看它的增量能推断窗口状态。

> **钉死这件事**:Wireshark + 明文 HTTP/2 是理解 gRPC 协议层的最佳实验场。配合本书 P2 篇五章读,你能把"HPACK 动态表怎么更新""双层 window 怎么给信用""gRPC framing 的 5 字节头长啥样"从源码抽象落到真实字节。建议读者至少抓一次包,亲眼看看这些帧。

---

## 三、benchmark:qps 工具

gRPC 仓库自带 QPS benchmark 工具,在 [`test/cpp/qps/`](../grpc/test/cpp/qps)。它支持同步/异步/callback 三种客户端(`client_sync.cc` / `client_async.cc` / `client_callback.cc`),能压测 unary 和 streaming 调用的 QPS、延迟分布。

主要文件:

- [`test/cpp/qps/client_sync.cc`](../grpc/test/cpp/qps/client_sync.cc) / `client_async.cc` / `client_callback.cc`:三种客户端实现。
- [`test/cpp/qps/driver.cc`](../grpc/test/cpp/qps/driver.cc):benchmark 驱动。
- [`test/cpp/qps/histogram.h`](../grpc/test/cpp/qps/histogram.h):延迟直方图。
- [`test/cpp/qps/benchmark_config.cc`](../grpc/test/cpp/qps/benchmark_config.cc):配置(消息大小、并发数、客户端数等)。

典型用法(bazel):

```bash
# 构建 qps worker(同时能跑 client 和 server)
bazel build //test/cpp/qps:qps_worker

# 启动 server worker(在 server 机器)
bazel-bin/test/cpp/qps/qps_worker --port=50051

# 启动 client worker(在 client 机器),驱动 benchmark
bazel-bin/test/cpp/qps/qps_json_driver --driver_port=51000 \
  --benchmarks=`cat my_benchmark.json`
```

`my_benchmark.json` 描述要跑的 benchmark(消息大小、并发、客户端数、LB 策略等),格式见 gRPC 官方的 benchmark 配置文档。

> **建议**:正式压测用 gRPC 官方的 [benchmark dashboard](https://github.com/grpc/grpc/blob/master/test/cpp/qps/README.md)(它有标准化的配置和报告格式);本地快速验证某个优化(比如换了 LB 策略、调了流控参数)的效果,直接用 qps_worker 即可。压测时务必配 channelz + otel,把诊断指标一起采下来,不然只看 QPS 数字看不出瓶颈在哪。

---

## 四、集成:Envoy/Istio/xDS、OpenTelemetry、k8s

### 4.1 Envoy / Istio / xDS(P6-22 主讲)

gRPC 客户端**内置 xDS client**,不依赖 Envoy sidecar,直接和 xDS 控制平面(Envoy/Istio/自研控制面)通信,动态收路由、LB、熔断、故障注入策略。bootstrap 配置通过环境变量 `GRPC_XDS_BOOTSTRAP` 指向一个 JSON 文件,源码解析在 [`src/core/xds/grpc/xds_bootstrap_grpc.cc`](../grpc/src/core/xds/grpc/xds_bootstrap_grpc.cc)。

典型 bootstrap JSON(简化):

```json
{
  "xds_servers": [
    {
      "server_uri": "traffic-director-ca.googleapis.com:443",
      "channel_creds": [{"type": "google_default"}],
      "server_features": ["xds_v3"]
    }
  ],
  "node": {
    "id": "projects/my-project/networks/default/nodes/my-node",
    "cluster": "my-cluster",
    "locality": { "region": "us-central1", "zone": "us-central1-a" }
  }
}
```

客户端用 `xds:///` scheme 触发 xDS 解析(`src/core/resolver/xds/xds_resolver.cc`),后续的路由/LB/熔断都由 xDS 下发的配置驱动。

两种部署模式:

- **Sidecar 模式(Envoy + Istio 经典)**:gRPC 客户端不知道 xDS,流量经过 Envoy sidecar,由 Envoy 做 xDS、路由、LB。gRPC 客户端就是个普通客户端。
- **Client-side xDS 模式(gRPC 原生)**:gRPC 客户端内置 xDS client,直接和控制平面通信,**不需要 sidecar**。这是 gRPC 的独特优势——省一个 sidecar 的资源开销和延迟。

> **钉死这件事**:gRPC 内置 xDS 是它和服务网格生态深度集成的根。选 sidecar 模式(Envoy 做 xDS)还是 client-side 模式(gRPC 自己做 xDS),看你的服务网格架构——如果全栈是 Istio,sidecar 模式统一管理;如果想省 sidecar,gRPC client-side xDS 是更轻量的选择。详见本书 P6-22。

### 4.2 OpenTelemetry(P6-20 主讲)

gRPC 内置 OpenTelemetry 插件,把调用级的指标(QPS、延迟、错误率、retry 次数)和 trace 自动上报到 otel collector。C++ 插件在 `src/cpp/ext/otel/`,主体是 [`otel_plugin.cc`](../grpc/src/cpp/ext/otel/otel_plugin.cc),提供 `OpenTelemetryPluginBuilder` 注册。

典型用法:

```cpp
#include "opentelemetry/sdk/...sdk_config.h"
grpc::internal::OpenTelemetryPluginBuilder otel_builder;
otel_builder.SetMeter(...);   // 接你的 otel meter
otel_builder.RegisterGlobal();  // 所有 channel 都上报
```

注册后,每次调用会自动埋点(`otel_client_call_tracer.cc` / `otel_server_call_tracer.cc`),上报指标包括:

- `grpc.client.attempt.started` / `grpc.client.attempt.duration`:每次调用尝试的计数和延迟。
- `grpc.client.call.duration`:整次调用(含 retry)的延迟。
- retry 相关、流控相关、LB 相关的指标。

> **建议**:生产环境务必接入 otel。gRPC 自带的埋点覆盖了调用全链路,配合 channelz(运行时诊断树)和 otel(指标 + trace),线上问题定位能从"瞎猜"变成"看图说话"。详见本书 P6-20。

### 4.3 kubernetes 集成

gRPC 在 k8s 上有几个注意点:

- **Service 后端用 headless Service + gRPC client-side LB**:k8s 的 ClusterIP service 默认走 kube-proxy(iptables/ipvs),对 gRPC 来说是"一个 VIP 后面多个 pod"。但 gRPC 客户端想自己做 LB(resolver + balancer),需要 **headless Service**(`clusterIP: None`),DNS 解析直接返回 pod IP 列表,gRPC 客户端用 `dns:///my-svc.my-ns.svc.cluster.local` 解析并自己做 pick_first/round_robin。
- **健康检查用 gRPC health check protocol**:k8s 1.24+ 的 kubelet 原生支持 gRPC health probe(`grpc_health_probe`),避免 HTTP liveness probe 的不准确。gRPC 的 health check 协议在 `src/cpp/server/health/`,P5-18 讲过。
- **连接保活注意云 LB 空闲超时**:很多云 LB(AWS NLB、GCP LB)默认空闲连接超时 350 秒左右,会把空闲 gRPC 连接默默关掉,但客户端可能感知不到(直到下一次调用失败)。配 keepalive(`GRPC_ARG_KEEPALIVE_TIME_MS` 小于云 LB 超时)能探到死连接,见下面 §5.1。

---

## 五、线上问题排查清单

这一节给六类最常见的线上问题,每类**排查路径 + 对应章节/源码**。遇到问题先来这里定位,再去本书对应章节深入。

### 5.1 连接假活(keepalive 不生效)

**症状**:客户端以为连接还在,实际云 LB 已经默默把空闲连接关了,下一次调用要等很久才超时失败,或者重连。

**根因**:TCP 半开连接、云 LB 空闲超时(AWS NLB 350s、GCP LB 等)会让连接"假活"——TCP 层没收到 RST,但中间设备已经把状态丢了。

**排查路径**:

1. **看 channelz**:调 `GetSubchannel` 看这条 SubChannel 的状态,如果是 READY 但调用一直失败,八成是假活。channelz 还能看到这条连接最近有没有 GOAWAY 事件。
2. **配 keepalive**:把 `GRPC_ARG_KEEPALIVE_TIME_MS`(默认 7200000ms = 2 小时,见 [`include/grpc/impl/channel_arg_names.h:153`](../grpc/include/grpc/impl/channel_arg_names.h#L153))调成小于云 LB 超时(比如 60s 或 120s),让 gRPC 定期发 PING 探活。`GRPC_ARG_KEEPALIVE_TIMEOUT_MS`(默认 20000ms)是 PING 没回 ACK 多久后关连接。
3. **看源码**:keepalive 的实现在 `src/core/ext/transport/chttp2/transport/keepalive.cc`,PING 限速在 `ping_rate_policy.cc`(防客户端滥用)。详见本书 P5-17。

**关键参数**(`channel_arg_names.h:150-162`):

| 参数 | 默认 | 含义 |
|------|------|------|
| `grpc.keepalive_time_ms` | 7200000(2 小时) | 多久没活动发一次 PING |
| `grpc.keepalive_timeout_ms` | 20000(20 秒) | PING 没 ACK 多久关连接 |
| `grpc.keepalive_permit_without_calls` | 0(false) | 没有活跃调用时是否允许 PING |

> **钉死这件事**:跨云 LB 的 gRPC 部署,**几乎必须**把 `keepalive_time_ms` 调小(小于云 LB 空闲超时),否则必然撞上"假活"。这是线上最常见的 gRPC 坑之一。

### 5.2 地址不更新 / 后端扩缩容客户端不知道

**症状**:后端扩容了新 pod,客户端还连着老的几个;后端缩容了,客户端还往已经关掉的 pod 发请求。

**根因**:resolver 没有及时收到地址更新,或者收到了但 SubChannel 状态机没跟上。

**排查路径**:

1. **看 resolver**:用 `dns:///` scheme 时,gRPC 客户端会定期重新解析 DNS(默认间隔约 30s,可配)。检查 DNS 记录是否真的更新了(k8s headless Service 的 DNS 由 CoreDNS 提供,可能传播慢)。
2. **看 channelz**:调 `GetChannel` 看 resolver 报告的地址列表,和实际后端 pod 列表对比。如果列表 stale,resolver 没生效。
3. **换 scheme**:如果用 k8s,考虑用 `xds:///`(配合控制平面,实时推送地址变更)替代 `dns:///`(定期轮询)。xDS 的 EDS 资源专门解决地址实时更新,见 P6-22。
4. **看源码**:resolver 异步回灌在 `src/core/resolver/polling_resolver.cc`(DNS 解析的通用基类),地址列表结构在 `endpoint_addresses.cc`。详见 P4-13。

### 5.3 负载不均

**症状**:多个后端实例,QPS 不均,有的被打爆、有的闲着。

**根因**:LB 策略选错、Picker 状态没跟上、SubChannel 状态机报错 READY 的子集不全。

**排查路径**:

1. **确认 LB 策略**:默认是 `pick_first`(连第一个能连的,失败才换下一个)——这在多后端场景下**会流量集中**。多后端要均匀分布,配 `round_robin`(或 `weighted_round_robin`)。策略通过 service config 或 channel arg 配。
2. **看 channelz**:调 `GetChannel` 看当前 Picker 报告的 READY SubChannel 列表。如果只有部分 SubChannel 是 READY,流量只会去那几个——可能是连接建不起来(看 §5.1、§5.6)。
3. **看 Picker 实现**:round_robin 的无锁 fast path 在 `src/core/load_balancing/round_robin/`,每次调用 `fetch_add` 选下标,见 P4-15。如果你以为有 `least_request` 策略(C 核心不存在,见 §1 地图说明),改用 `weighted_round_robin`。
4. **粘性会话用 ring_hash**:如果需要会话粘性(session affinity),用 `ring_hash`(一致性哈希),见 P4-15。

> **钉死这件事**:gRPC 客户端默认 `pick_first` 是个常见坑——它在"单后端"场景下合理(连一个就行),但在"多后端要均分"场景下会让流量集中。多后端务必显式配 `round_robin` 或更复杂的策略。

### 5.4 雪崩(重试放大)

**症状**:后端短暂变慢,客户端开始重试,重试流量放大,把后端彻底打死,级联到上游服务全崩。

**根因**:重试策略配错(无脑 `retry_on` + 大 `max_attempts`),没有 retry throttle。

**排查路径**:

1. **看 retry policy 配置**:service config 里 `methodConfig.retryPolicy`,确认 `retryableStatusCodes`(没有任何状态码默认可重试,UNAVAILABLE 也要显式写)、`maxAttempts`(默认 1 = 不重试)。见 P4-16。
2. **确认 retry throttle 生效**:retry throttle(`RetryThrottler` 类,per-channel,毫 token 单位)按成功率动态调重试比例——后端持续失败时自动收紧。源码在 [`src/core/client_channel/retry_throttle.h`](../grpc/src/core/client_channel/retry_throttle.h)。见 P4-16。
3. **退避必须加抖动**:`initialBackoff` / `maxBackoff` 配合,加抖动防同步重试风暴。
4. **看 otel 指标**:`grpc.client.attempt.started` vs `grpc.client.call.duration` 的比例——如果 attempts 远多于 calls,说明重试放大了。
5. **hedging 注意**:C 核心的 hedging 尚未实现(P4-16 拆过,全是 TODO),别指望它防尾延迟。

> **钉死这件事**:重试是双刃剑——网络抖动时救命,后端真挂时放大雪崩。retry throttle 令牌桶是防雪崩的核心防线,务必理解它怎么按成功率动态调比例。见 P4-16。

### 5.5 性能问题(slice/arena/压缩)

**症状**:QPS 上不去、CPU 高、内存涨、延迟毛刺。

**排查路径**:

1. **看消息大小**:大消息(几 MB)在 filter 栈里传递,即使 slice 零拷贝(P6-21),序列化/反序列化、压缩/解压的开销也大。看 service config 的 `maxSendMessageSize` / `maxReceiveMessageSize` 是否合理。message_size filter 在 `src/core/ext/filters/message_size/`。
2. **看压缩**:gRPC 支持 gzip/deflate(只有 zlib 系,**当前版本没有 zstd**)。compression filter 在 `src/core/ext/filters/`。压缩省带宽但耗 CPU,小消息别压(压缩比低还费 CPU)。
3. **看 slice/arena**:如果用 C core 直接集成(比如 Python grpcio 包 C core),关注 arena 分配——P6-21 讲过,arena 是 bump pointer 一次分配无 per-free,正常情况不用调。但如果你看到 RSS 持续涨,可能是 arena 生命周期泄漏(call 没正常结束)。
4. **看流控**:高 BDP 链路(跨机房、专线)如果窗口没调大,会"饿死"。看 channelz 的流控 window 指标,确认 BDP 估计有没有把窗口推到 MB 级。Promise 版 transport 的 BDP 还没接(8 秒定时器占位,见附录 A §2.10.3),跨高 BDP 链路暂时考虑用经典 transport 或配大 `INITIAL_WINDOW_SIZE`。
5. **看连接数**:gRPC 多路复用让一条连接跑海量调用,但如果客户端开了多个 channel,每个 channel 独立建连,连接数会涨。生产环境尽量复用 channel(一个目标一个 channel,SubChannel 复用池会共享底层连接)。

> **钉死这件事**:gRPC 性能问题的根,大多在 slice/arena(P6-21)、流控(P2-09)、连接复用(P4-14)。先看 channelz 诊断树把瓶颈定位到"是流控卡了、还是连接数多了、还是消息太大",再钻对应章节。

### 5.6 TLS 握手失败

**症状**:连接建不起来,报 `HANDSHAKE_FAILED`、`CERT_VERIFY_FAILED`、`ALPN_NEGOTIATION_FAILED`。

**排查路径**:

1. **看证书**:服务端证书是否过期、SAN 是否覆盖客户端连的域名/IP。用 `openssl s_client -connect host:443` 看证书链。
2. **看 ALPN**:gRPC over TLS 要求 ALPN 协商出 `h2`(HTTP/2)。如果中间设备(老 LB、老代理)不支持 ALPN 或只支持 `http/1.1`,握手会失败。ALPN 处理在 `src/core/ext/transport/chttp2/alpn/`(附录 A §2.10 提过)。
3. **看 channelz**:`GetSubchannel` 看 connection attempt 的失败原因,能看到具体的 TLS 错误码。
4. **看 tsi**:tsi 抽象层屏蔽 SSL/ALTS/local,握手状态机在 `src/core/handshaker/handshaker.cc`,SSL 实现在 `src/core/tsi/ssl_transport_security.cc`。见 P5-19。
5. **mTLS 双向认证**:如果用 mTLS,客户端证书 / CA 配置在 channel creds,见 P5-19。

> **钉死这件事**:TLS 握手失败的 90% 是证书问题(过期、SAN 不对、CA 不信任),10% 是 ALPN 不支持(中间设备太老)。先 `openssl s_client` 验证证书链,再排查 ALPN,最后才看 gRPC 自己的 tsi 层。

---

## 六、配套:本书章节与排查清单的映射

最后给一张"排查问题 → 看哪章 → 看哪个源码/工具"的映射表,方便你遇到问题时快速定位:

| 问题类别 | 看哪章 | 关键源码 | 关键工具 |
|---------|--------|---------|---------|
| 连接假活(keepalive) | P5-17 | `keepalive.cc` / `ping_rate_policy.cc` | channelz + `keepalive_time_ms` 调参 |
| 地址不更新(resolver) | P4-13 / P6-22 | `polling_resolver.cc` / `xds_resolver.cc` | channelz + 换 `xds:///` scheme |
| 负载不均(LB) | P4-15 | `load_balancing/round_robin/` | channelz + 配 `round_robin` |
| 雪崩(retry) | P4-16 | `retry_throttle.h` / `retry_interceptor.cc` | otel(retry 指标)+ service config |
| 性能(slice/arena) | P6-21 | `slice_refcount.h` / `arena.cc` | channelz + 压缩/消息大小调优 |
| TLS 握手失败 | P5-19 | `ssl_transport_security.cc` / `handshaker.cc` | `openssl s_client` + channelz |
| 流控异常(饿死/淹死) | P2-09 | `flow_control.cc` / `bdp_estimator.cc` | Wireshark(看 WINDOW_UPDATE)+ channelz |
| HPACK 压缩效果 | P2-07 | `hpack_encoder.cc` / `hpack_parser_table.cc` | Wireshark(解 HEADERS 帧)+ channelz(命中率) |
| filter 栈问题 | P3-11 | `call_filters.cc` / `filter_fusion.h` | channelz(看 filter 栈) |
| channelz 怎么用 | P6-20 | `channelz/` / `channelz_service.cc` | grpcurl 调 `grpc.channelz.v1.Channelz` |
| xDS 配置 | P6-22 | `xds_bootstrap_grpc.cc` / `xds_resolver.cc` | `GRPC_XDS_BOOTSTRAP` 环境变量 |

---

> 这个附录是"动手时的操作手册"。遇到问题先来这里定位排查路径,再去本书对应章节深入原理,最后用附录 A 的源码地图找到具体源码位置。gRPC 是个大工程,但只要抓住"一次方法调用变成一条流"这条主线,加上 channelz(诊断树)、grpcurl(动态调用)、otel(指标)、Wireshark(抓帧)这四件套,绝大多数线上问题都能从"瞎猜"变成"看图说话"。
>
> 全书到此结束。如果你从头读到尾,此刻你该能在脑子里放映出一次 `stub->GetUser(req)` 的全过程,讲清每一步的设计动机和实现技巧——你已经不是那个"翻过源码却一知半解"的人了。
