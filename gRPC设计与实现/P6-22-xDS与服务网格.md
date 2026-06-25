# 第 6 篇 · 第 22 章 · xDS 与服务网格

> **核心问题**:前面的章节里,我们讲的 resolver(P4-13)和负载均衡(P4-15)都是"静态"或"半动态"的——`dns:///` 解析一次拿一批地址,`round_robin` 在这些地址里轮询。但在真正的微服务 / 服务网格场景里,这些配置是**控制面动态下发**的:路由规则(哪个 API 转到哪个集群)、负载均衡策略(locality 加权、一致性哈希)、熔断阈值(最大并发请求数)、后端端点列表(健康检查驱动的增减),都可能每秒在变。这套动态配置,业界事实标准是 Envoy 定义的 **xDS 协议**(LDS/RDS/CDS/EDS)。传统做法是在每个服务旁边部署一个 Envoy **sidecar**(代理),sidecar 收 xDS、做路由和 LB,服务只和 sidecar 说话。但这要付出"多一跳、多一份资源、多一层延迟"的代价。那 gRPC 怎么做到**不依赖 sidecar,client 进程自己内置一个 xDS client**,直接收控制面下发的配置、自己做路由和 LB,还能和 Envoy/Istio 互通?这套机制在 gRPC core 源码里是怎么落地的?

> **读完本章你会明白**:
> 1. xDS 协议的四类资源(LDS/RDS/CDS/EDS)各下发什么,以及它们怎么形成一条"Listener → Route → Cluster → Endpoint"的依赖链。
> 2. gRPC 怎么把 xDS client **直接内嵌到 client 进程**,不经 sidecar——这是 gRPC 区别于传统 service mesh 的关键,也是它省掉代理开销的根。
> 3. `xds://` 这个 URI scheme 怎么被 `XdsResolver` 解析,`XdsDependencyManager` 怎么管 LDS→RDS→CDS→EDS 的依赖链,resolver 怎么把结果**合成一份 service config** 交给标准 LB 链路。
> 4. xDS LB 策略(cds/xds_cluster_impl/xds_wrr_locality)怎么层层包装普通 round_robin,落地 CDS/EDS 下发的后端列表、locality 加权、熔断和 drop。
> 5. bootstrap 怎么配、gRPC 怎么复用 Envoy 的 proto 和 Envoy/Istio 协议级互通。

> **如果一读觉得太难**:先只记住三件事——① xDS 四类资源 LDS→RDS→CDS→EDS 是一条依赖链(监听器→路由→集群→端点);② gRPC client 内置 xDS client,**不经 sidecar**,用 `xds://` URI 触发;③ resolver 把 xDS 结果合成 service config,LB 层(cds→xds_cluster_impl→xds_wrr_locality→round_robin)层层落地。本章 gRPC 独有细节讲透,Envoy 通用细节(控制面架构、xDS 协议演化)指路《Envoy 设计与实现》。

---

## 〇、一句话点破

> **gRPC 内置一个完整的 xDS client:用 `xds://` URI 触发,resolver 订阅 LDS→RDS→CDS→EDS 依赖链,把结果合成 service config,LB 层层层落地路由、locality 加权、熔断、drop。整个过程在 client 进程内完成,不经 Envoy sidecar,还和 Envoy/Istio 用同一套 xDS 协议互通。**

这是结论,不是理由。本章倒过来拆:先讲清 sidecar 模式撞上什么墙,再讲 xDS 协议本身(LDS/RDS/CDS/EDS),然后讲 gRPC 怎么把 xDS client 内嵌进 resolver+LB 两层,最后讲 bootstrap 和与 Envoy 的互通。三章二分法归属:**治理 / 衔接**——这一章是"客户端治理"那条线的动态化终章(P4-13~P4-16 是静态/半动态治理,xDS 是控制面驱动的动态治理),也是本书通往《Envoy 设计与实现》那本的接口。

---

## 一、为什么不能只靠 sidecar

### sidecar 模式及其代价

先回顾传统 service mesh 的部署模式。Envoy 作为 service mesh 的数据面,典型部署是 **sidecar**:每个服务实例旁边,并排跑一个 Envoy 进程,两者共享 localhost 网络。服务发出去的调用,先发给 sidecar(localhost,一个 hop),sidecar 根据控制面下发的 xDS 配置做路由、LB、熔断、可观测,再转发给真正的后端。

```
   传统 sidecar 模式
   ┌─────────────────────────┐    ┌─────────────────────────┐
   │ 节点 A                   │    │ 节点 B                   │
   │  ┌─────────┐  ┌───────┐ │    │  ┌───────┐  ┌─────────┐ │
   │  │ 服务 A  │→│Envoy A│─┼───→│  │Envoy B│←│ 服务 B  │ │
   │  └─────────┘  └───────┘ │    │  └───────┘  └─────────┘ │
   └─────────────────────────┘    └─────────────────────────┘
        ↑ xDS                          ↑ xDS
        │                              │
   ┌────┴──────────────────────────────┴────┐
   │ 控制面(Istio / 操作员)                  │
   └────────────────────────────────────────┘
```

这套模式的优点:服务本身**不用感知 mesh**——它只管往 localhost 发请求,所有 mesh 逻辑(路由/LB/熔断/遥测)都在 sidecar 里,语言无关、框架无关。Envoy 也因此成了 service mesh 数据面的事实标准。

但 sidecar 有实打实的代价:

**代价一:多一跳**。每次调用,客户端服务 → 客户端 sidecar → 服务端 sidecar → 服务端服务,本来一次直连,现在多了两跳(localhost 虽快,但仍是两次 syscall + 序列化)。对于延迟敏感的调用(比如内部 RPC 的 p99),这是不可忽视的开销。

**代价二:多一份资源**。每个服务实例配一个 Envoy sidecar,Envoy 本身要占 CPU/内存(几十 MB 起)。在大规模集群(成千上万实例)下,这些 sidecar 吃掉的资源总量惊人。

**代价三:多一层运维**。sidecar 要单独注入(istioctl / mutating webhook)、单独升级、单独排查。sidecar 挂了,服务也跟着挂(因为它依赖 sidecar 转发)。

> **不这样会怎样**:sidecar 模式在"语言无关、框架无关"上有优势,但它的三重代价(多一跳、多资源、多运维)是实打实的。如果一个服务的 RPC 框架本身**就能直接说 xDS**,那它根本不需要 sidecar——它自己就是数据面。这正是 gRPC 走的路。

### gRPC 的回答:client 自己就是数据面

gRPC C++ core 内置了一个完整的 xDS client。这意味着:**一个用 gRPC 的服务,本身就能直接收控制面下发的 xDS 配置、自己做路由和 LB,不需要任何 sidecar**。

```
   gRPC 无 sidecar 模式(client 内置 xDS)
   ┌─────────────────────────┐    ┌─────────────────────────┐
   │ 节点 A                   │    │ 节点 B                   │
   │  ┌─────────────────────┐│    │  ┌─────────────────────┐│
   │  │ 服务 A(gRPC client)││    │  │ 服务 B(gRPC server)││
   │  │  内置 xDS client    ││───→│  │                     ││
   │  │  内置 xDS resolver  ││    │  └─────────────────────┘│
   │  │  内置 xDS LB        ││    └─────────────────────────┘
   │  └─────────────────────┘│
   └──────────┬──────────────┘
              │ xDS(直接,不经 sidecar)
   ┌──────────┴──────────────┐
   │ 控制面(Istio / 操作员)   │
   └─────────────────────────┘
```

调用直接从 gRPC client 发到 gRPC server,只有一跳。xDS 配置直接由 gRPC client 接收和处理,没有中间代理。这是 gRPC 区别于"sidecar-based service mesh"的根本——**它把数据面逻辑织进了 RPC 框架本身**。

> **钉死这件事**:gRPC 内置 xDS client 是"无 sidecar service mesh"的关键。服务用 gRPC,本身就能说 xDS,不需要 Envoy 代理。省掉的是 sidecar 的三重代价(多一跳、多资源、多运维),换来的是"client 必须用 gRPC(或支持 xDS 的框架)"的约束。在大规模内部微服务(都用 gRPC)场景,这个 trade-off 极其划算。

---

## 二、xDS 协议:四类资源的依赖链

### LDS/RDS/CDS/EDS 各下发什么

xDS 是 Envoy 定义的"动态配置下发协议"统称,核心是四类资源,每类管一层:

| 资源类型 | 全称 | 下发什么 | gRPC 里对应 |
|---------|------|---------|------------|
| **LDS** | Listener Discovery Service | 监听器配置:监听什么、用什么 filter 链 | `XdsListenerResource`(`xds_listener.h`) |
| **RDS** | Route Discovery Service | 路由配置:URL 路径匹配到哪个集群 | `XdsRouteConfigResource`(`xds_route_config.h`) |
| **CDS** | Cluster Discovery Service | 集群配置:集群的 LB 策略、熔断、连接超时 | `XdsClusterResource`(`xds_cluster.h`) |
| **EDS** | Endpoint Discovery Service | 端点配置:集群里的具体后端地址列表(带 locality/权重/健康) | `XdsEndpointResource`(`xds_endpoint.h`) |

它们形成一条自然的依赖链:**LDS 定义监听器,监听器引用 RDS 做路由,路由把不同路径指向 CDS 集群,集群用 EDS 拿到具体端点**。一个 HTTP 请求进来,Envoy(或 gRPC)按这条链层层查表:Listener → Route → Cluster → Endpoint。

```
   xDS 依赖链:LDS → RDS → CDS → EDS
   ┌─────────────────────────────────────────────────┐
   │ LDS: Listener (监听器)                          │
   │   "监听 :8080,用这套路由配置"                    │
   │     │                                            │
   │     ▼ 引用 RDS                                   │
   │ RDS: RouteConfiguration (路由表)                │
   │   "/user/*  → cluster: user-service              │
   │    /order/* → cluster: order-service             │
   │    /pay/*   → cluster: pay-service (熔断 1000)"  │
   │     │                                            │
   │     ▼ 引用 CDS                                   │
   │ CDS: Cluster (集群)                              │
   │   "user-service: LB=wrr_locality, 熔断=1000并发"  │
   │     │                                            │
   │     ▼ 引用 EDS                                   │
   │ EDS: Endpoint (端点列表)                         │
   │   "user-service 的端点:                          │
   │     10.0.1.1:8080 (locality=us-east-1a, w=1)     │
   │     10.0.1.2:8080 (locality=us-east-1a, w=1)     │
   │     10.0.2.1:8080 (locality=us-east-1b, w=1)"    │
   └─────────────────────────────────────────────────┘
```

这套协议的精妙在于:**每一层都可以独立动态下发**。后端扩容了,只更新 EDS(新端点列表),不用动 LDS/RDS/CDS;改路由规则,只更新 RDS;调熔断阈值,只更新 CDS。控制面按需下发,数据面按需订阅,粒度精细。

> **钉死这件事**:xDS 四类资源是"监听器→路由→集群→端点"的依赖链。LDS 引用 RDS,RDS 引用 CDS,CDS 引用 EDS。这套分层让动态配置可以按粒度下发(扩容只动 EDS,改路由只动 RDS)。Envoy 通用细节(协议演化、ADS 聚合、Delta xDS)详见《Envoy 设计与实现》,本章聚焦 gRPC 怎么消费这套协议。

### ADS:聚合的流式订阅

实际部署里,四类资源不是四个独立连接分别拉,而是用一个 **ADS(Aggregated Discovery Service)** 的双向流统一订阅。客户端在一条 gRPC streaming call 上,告诉控制面"我要这些 LDS/RDS/CDS/EDS 资源",控制面在同一个流上推更新。gRPC 的 ADS 方法名是标准的 `/envoy.service.discovery.v3.AggregatedDiscoveryService/StreamAggregatedResources`(`src/core/xds/xds_client/xds_client.cc:734-742`)。

注意一个关键点:**gRPC 用自己的 gRPC channel 连接控制面跑 ADS**(`xds_transport_grpc.cc:278` 的 `grpc_channel_create(...)`)。也就是说,gRPC 用 gRPC 自己,从控制面拉 gRPC(xDS)需要的配置。这条 ADS 流的 channel creds 来自 bootstrap 配置(下文讲)。

---

## 三、gRPC 内置 xDS client:resolver 层

那 gRPC client 怎么触发 xDS?答案是 `xds://` 这个 URI scheme。回忆 P4-13 resolver:URI 的 scheme 决定用哪个 resolver(`dns:///` 用 dns resolver,`passthrough:///` 用 passthrough resolver)。`xds://` 就触发 `XdsResolver`。

### XdsResolver:把 xds:// 解析成 service config

`src/core/resolver/xds/xds_resolver.cc:103` 定义了 `XdsResolver`(注意:**没有 `xds_resolver.h`**,整个类在 .cc 里):

```cpp
// src/core/resolver/xds/xds_resolver.cc:103
class XdsResolver final : public Resolver {
  ...
 private:
  void StartLocked() override;   // 核心入口
  ...
};
```

核心逻辑在 `StartLocked()`(`xds_resolver.cc:838-921`)。它做的事:

1. `GrpcXdsClient::GetOrCreate(...)`(`xds_resolver.cc:840`):每个 data-plane authority 拿一个 `XdsClient`(`src/core/xds/xds_client/xds_client.h:60`,`DualRefCounted`)。
2. **构造 LDS 资源名**:用 URI path + bootstrap 的 `client_default_listener_resource_name_template`(默认 `%s`)拼接(`xds_resolver.cc:855-900`)。比如 `xds://foo` → LDS 资源名 `foo`(或 federation 下的 `xdstp://.../envoy.config.listener.v3.Listener/foo`)。
3. 创建 `XdsDependencyManager` 并传入 `lds_resource_name_`(`xds_resolver.cc:903-905`),**由它负责订阅整条 LDS→RDS→CDS→EDS 依赖链**。

关键:xds_resolver **不直接订阅 LDS/RDS**,也不直接产出"地址列表"(它的 `result.addresses` 是空的,`xds_resolver.cc:~1003`)。它把订阅和依赖管理交给 `XdsDependencyManager`,把地址解析交给 LB 层。它自己产出的,是一份**合成的 service config**(下文讲)。

### XdsDependencyManager:管 LDS→RDS→CDS→EDS 依赖链

`src/core/resolver/xds/xds_dependency_manager.h:36` 的 `XdsDependencyManager`,注释明确写着:"Watches all xDS resources and handles dependencies between them."(h:34)。它是 xDS 依赖链的**编排者**。内部有四个 watcher,层层触发(`xds_dependency_manager.cc`):

```cpp
// 简化示意:XdsDependencyManager 的四个 watcher(层层触发)
class XdsDependencyManager {
  class ListenerWatcher {       // cc:47 收到 LDS
    // 从 LDS 拿 RDS 名或内联 route_config;需动态 RDS 则创建 RouteConfigWatcher(cc:343)
  };
  class RouteConfigWatcher {    // cc:82 收到 RDS
    // 解析出引用的 cluster 名,为每个 cluster 创建 ClusterWatcher(cc:682)
  };
  class ClusterWatcher {        // cc:122 收到 CDS
    // 从 CDS 的 eds_service_name 决定 EDS 资源名(cc:707),创建 EndpointWatcher(cc:716)
  };
  class EndpointWatcher {       // cc:160 收到 EDS
    // 所有依赖齐了,OnUpdate 把 XdsConfig 一次性回调给 resolver(h:42, h:49)
  };
};
```

这是一条**精确的依赖链触发**:

1. 收到 LDS → 知道要用哪个 RDS(或 LDS 内联了 route_config)。
2. 订阅 RDS → 收到后,解析路由表,知道引用了哪些 cluster 名。
3. 为每个 cluster 订阅 CDS → 收到后,知道这个 cluster 的 eds_service_name(去哪拿端点)。
4. 订阅 EDS → 收到后,**所有依赖齐了**,把整个 `XdsConfig`(`xds_config.h:39`,持有所有 cluster 的配置快照)一次性回调给 resolver。

> **钉死这件事**:XdsDependencyManager 是 xDS 依赖链的编排者。它不是"并行订阅四类资源",而是"**按依赖触发**:LDS 拿到才知道订阅哪个 RDS,RDS 拿到才知道订阅哪些 CDS,CDS 拿到才知道订阅哪个 EDS"。这种级联订阅,避免了"一次订阅四类但不知道具体资源名"的浪费。所有资源齐了,一次性回调 `XdsConfig` 给 resolver。

### XdsConfig:依赖解析结果的快照

`src/core/resolver/xds/xds_config.h:39` 的 `XdsConfig`,是依赖链解析完成后的**配置快照**:

```cpp
// src/core/resolver/xds/xds_config.h:39, 48, 53, 67, 98(简化)
struct XdsConfig : public RefCounted<XdsConfig> {
  struct EndpointConfig { ... };          // L53 EDS 结果
  struct AggregateConfig { ... };         // L67 aggregate cluster
  struct ClusterConfig {
    absl::StatusOr<EndpointConfig> endpoint_config;   // 挂的 EDS 结果
    // ... CDS 配置
  };
  absl::flat_hash_map<std::string, absl::StatusOr<ClusterConfig>> clusters;  // L98 所有 cluster
};
```

它通过 channel args 传给 LB 层(`xds_config.h:105` 的 `ChannelArgsCompare`)。LB 层从这份 config 里拿到每个 cluster 的具体配置(CDS + EDS),落地成实际的 SubChannel 和 Picker。

### resolver 怎么把结果交给 LB:合成 service config

这里有个非常 gRPC 的设计:**xds_resolver 不直接选 LB,而是合成一份 service config(LB config),让 gRPC 的标准 LB 链路去执行**。看 `XdsResolver::CreateServiceConfig()`(`xds_resolver.cc:940-993`,在 `xds_resolver.cc:995` 调用 `result.service_config = CreateServiceConfig()`):

生成的 JSON service config 大致长这样:

```json
{
  "loadBalancingConfig": [
    {
      "xds_cluster_manager_experimental": {
        "children": {
          "cluster:user-service": {
            "childPolicy": [
              { "cds_experimental": { "cluster": "user-service" } }
            ]
          },
          "cluster:order-service": {
            "childPolicy": [
              { "cds_experimental": { "cluster": "order-service" } }
            ]
          }
        }
      }
    }
  ]
}
```

即:每条路由的 cluster 被包成一个 `xds_cluster_manager` 的 child,其 childPolicy 是 `cds_experimental`(`xds_resolver.cc:956-976`)。**这份 service config 喂给 gRPC 标准的 LB 框架,触发 xDS LB 链路**。

路由→cluster 的选择发生在每次 RPC 时,由 `XdsConfigSelector::GetCallConfig`(`xds_resolver.cc:644-708`)执行:匹配请求路径 → 选 cluster → 把 cluster 名写进 call metadata。

> **钉死这件事**:xds_resolver 不自己选 LB,它合成一份 service config(LB config),把决策权交给 gRPC 标准 LB 框架。这种"resolver 产出 LB config"的模式,让 xDS 能复用 gRPC 既有的 LB 基础设施(ChildPolicyHandler、各 LB policy),不用另起炉灶。

---

## 四、gRPC 内置 xDS client:LB 层

resolver 合成的 service config 触发的是 `xds_cluster_manager_experimental`,这进入 LB 层。xDS 的 LB 是**层层包装**普通 LB 的 delegating 策略。

### xDS LB 链路:层层包装

`src/core/load_balancing/xds/` 下有一串 LB policy,形成一条包装链:

| LB policy | 文件 | name() | 干什么 |
|-----------|------|--------|--------|
| `CdsLb` | `cds.cc:208` | `cds_experimental` | 订阅 CDS,按 endpoint priority 生成 priority children |
| `XdsClusterImplLb` | `xds_cluster_impl.cc:177` | `xds_cluster_impl_experimental` | 落地熔断、drop、LRS 上报 |
| `XdsClusterManagerLb` | `xds_cluster_manager.cc:106` | `xds_cluster_manager_experimental` | 管理多个 cluster(路由到哪个) |
| `XdsWrrLocalityLb` | `xds_wrr_locality.cc` | `xds_wrr_locality_experimental` | locality 加权 round robin |
| `XdsOverrideHostLb` | `xds_override_host.cc` | `xds_override_host` | 指定 host 覆盖 |

完整链路(从 service config 触发开始):

```
   xds_cluster_manager_experimental (管多个 cluster,路由到哪个)
        │ 每个 cluster 一个 child
        ▼
   cds_experimental (CdsLb)
        │ 订阅 CDS+EDS(XdsConfig),拿 cluster 配置和 endpoint 列表
        │ 按 endpoint priority 生成 priority children
        ▼
   xds_cluster_impl_experimental (XdsClusterImplLb)
        │ 落地熔断(max_concurrent_requests)、drop(drop_config)、LRS 上报
        ▼
   xds_wrr_locality_experimental (XdsWrrLocalityLb)
        │ 按 locality 加权,合成 weighted_target child config
        ▼
   weighted_target_experimental (gRPC 通用加权 LB)
        │ 每个 weight target 内部
        ▼
   round_robin / pick_first (普通 LB,真正在 endpoint 间挑)
```

> **钉死这件事**:xDS LB 是"层层包装"普通 LB 的 delegating 策略。最外层 `xds_cluster_manager` 管路由(哪个 cluster),`cds` 订阅 CDS/EDS,`xds_cluster_impl` 做熔断/drop,`xds_wrr_locality` 做 locality 加权,最内层是普通 `round_robin`。xDS 层负责"按 locality 加权 + 熔断 + drop",真正在 endpoint 间挑由底层 round_robin 做。

### 熔断和 drop:在 xds_cluster_impl 落地

熔断(circuit breaker)和 drop 是 xDS 的核心治理能力,落地在 `XdsClusterImplLb`(`xds_cluster_impl.cc`):

**配置来源**:CDS 解析时,`xds_cluster_parser.cc:577-591` 读 `envoy.config.cluster.v3.CircuitBreakers.thresholds`,取 `max_requests` 存入 `cds_update->max_concurrent_requests`(L591)。drop 配置来自 EDS 的 `DropConfig`。

**执行**:在 `XdsClusterImplLb` 的 `Picker` 里(`xds_cluster_impl.cc:319` 的 `SubchannelCallTracker`,核心拒绝逻辑在 cc:399-411):

```cpp
// src/core/load_balancing/xds/xds_cluster_impl.cc:399-411 (简化)
PickResult Pick(PickArgs args) {
  // 1. 按 drop_config 按 category drop(主动丢弃)
  if (drop_config_->ShouldDrop(...)) {
    drop_stats_->AddCallDropped(...);   // L400-403
    return PickResult(...);             // 返回 drop
  }
  // 2. 熔断:并发超限则拒绝
  if (call_counter_->Load() >= max_concurrent_requests_) {
    drop_stats_->AddUncategorizedDrops();  // L410-411
    return PickResult(...);                // 返回熔断
  }
  // 3. 正常 pick,交给底层 round_robin
  return helper_->PickSubchannel(args);
}
```

drop 是控制面主动配置的"按比例丢弃"(比如后端过载时,drop 10% 的请求保护后端);熔断是"并发数超阈值就拒绝新请求"(防止压垮后端)。两者都在 Picker 的 fast path 上,靠原子计数判断,开销极低。drop 和熔断的统计通过 LRS(load reporting service)上报给控制面(`LrsClient::ClusterDropStats`,`xds_cluster_impl.cc:251,377`)。

### 一个重要修正:没有 xds_cluster_resolver

这里诚实交代一个源码事实:**当前 commit 没有 `xds_cluster_resolver` 这个 LB policy**。早期 gRPC(Java/C++)有 `XDS_CLUSTER_RESOLVER` 这一层,做 DNS/logical-DNS/EDS 聚合;但本版本的 C++ core 把 CDS 资源拉取 + endpoint 解析**直接做进了 `CdsLb`**(`cds.cc`),不再单独分层。全仓 grep `xds_cluster_resolver` 只在 `src/core/load_balancing/xds/xds_channel_args.h:28` 出现过一句注释。读老资料如果提到 `xds_cluster_resolver`,要知道那在 C++ 当前版本已经被合并进 `cds` 了。

---

## 五、bootstrap:启动时配 xDS server

gRPC client 怎么知道控制面在哪、用什么身份连?答案是 **bootstrap 配置**。

### 环境变量

gRPC 通过两个环境变量读 bootstrap(`src/core/xds/grpc/xds_client_grpc.cc:201-228`):

- `GRPC_XDS_BOOTSTRAP`:指向一个 bootstrap JSON 文件路径。
- `GRPC_XDS_BOOTSTRAP_CONFIG`:直接内联 bootstrap JSON 字符串。

两者都没有就报错(`xds_client_grpc.cc:228`)——用 `xds://` 必须配 bootstrap。

### bootstrap JSON 结构

bootstrap JSON 的 schema 在 `GrpcXdsBootstrap`(`src/core/xds/grpc/xds_bootstrap_grpc.h/cc`)的 `JsonObjectLoader`(`xds_bootstrap_grpc.cc:241-258`)里定义。核心字段:

```json
{
  "xds_servers": [                          // 必填:xDS server 列表
    {
      "server_uri": "trafficdirector.googleapis.com:443",  // 控制面地址
      "channel_creds": [                    // 连控制面用的 creds
        { "google_default": {} }
      ]
    }
  ],
  "node": {                                 // 本节点身份
    "id": "...",
    "cluster": "...",
    "locality": { ... }
  },
  "server_listener_resource_name_template": "...",  // LDS 资源名模板
  "authorities": { ... }                    // federation(多 authority)
}
```

`xds_servers`(必填,验证在 `xds_bootstrap_grpc.cc` ~L265 的 `JsonPostLoad`)告诉 gRPC 控制面在哪、用什么 channel creds 连。`node` 是本节点身份,会随每个 DiscoveryRequest 发给控制面(让控制面知道是谁在订阅)。`server_listener_resource_name_template` 和 `client_default_listener_resource_name_template` 是 LDS 资源名的模板(xds_resolver 用它构造资源名)。

> **钉死这件事**:bootstrap 是 gRPC client 连接 xDS 控制面的启动配置,通过 `GRPC_XDS_BOOTSTRAP(_CONFIG)` 环境变量读 JSON。它告诉 gRPC:控制面在哪(xds_servers.server_uri)、用什么身份连(channel_creds)、我是谁(node)。没配 bootstrap 就用 `xds://` 会报错。

---

## 六、与 Envoy/Istio 互通

gRPC 内置 xDS client 的一个关键收益,是和 Envoy 生态**协议级互通**。

### 直接复用 Envoy 的 proto

gRPC 没有重新发明 xDS 协议,而是**直接复用 Envoy 的 data-plane-api proto**(用 upb 生成 C 代码)。依赖在 `bazel/grpc_deps.bzl:213-220`(`envoy_api` → `envoyproxy/data-plane-api`)和 `MODULE.bazel:45`(`envoy_api`)、`MODULE.bazel:87`(`com_github_cncf_xds`)。

看真实的 include,`src/core/xds/grpc/xds_cluster_parser.cc:22-37` 一长串:

```cpp
#include "envoy/config/cluster/v3/circuit_breaker.upb.h"
#include "envoy/config/cluster/v3/cluster.upb.h"
#include "envoy/config/cluster/v3/outlier_detection.upb.h"
// ... 等
```

ADS 消息(`xds_client.cc:33-34`)用 `envoy/service/discovery/v3/discovery.upb`。**这意味着同一个控制面(Istio、Traffic Director、自研 operator),可以同时给 Envoy sidecar 和 gRPC client 下发同一套 xDS 配置**——它们说的是同一种语言。

四类资源的 type_url 也是 Envoy 标准的:

| 资源 | type_url |
|------|---------|
| LDS | `envoy.config.listener.v3.Listener` |
| RDS | `envoy.config.route.v3.RouteConfiguration` |
| CDS | `envoy.config.cluster.v3.Cluster` |
| EDS | `envoy.config.endpoint.v3.ClusterLoadAssignment` |

(type_url 定义见 `src/core/xds/grpc/*_parser.h` 的 `type_url()`,如 `xds_listener_parser.h:37-38`。)

### gRPC 自己的扩展

除了标准 Envoy xDS,gRPC 还有几个自己的扩展:

- **HTTP filter 扩展**:`xds_http_rbac_filter`、`xds_http_fault_filter`、`xds_http_gcp_authn_filter`、`xds_http_stateful_session_filter`、`xds_http_composite_filter`(都在 `src/core/xds/grpc/`),这些是 gRPC client 能识别的 HTTP filter 类型。
- **cluster_specifier_plugin**:grpc 专属的 cluster specifier(可以让路由不走标准 cluster,走自定义解析)。
- **RLS(Routing Lookup Service)**:`src/core/load_balancing/rls/`(`rls.cc:189` 的 `RlsLb`),grpc 专属的路由查找服务(`grpc.lookup.v1.RouteLookupService`,`rls.proto`)。每次 RPC 用请求 key 查 RLS server,返回目标 cluster 名。它和 xDS **正交**,但能被 xDS 引用(`XdsDependencyManager` 的 `GetClusterSubscription` 让 RLS 把它路由的 cluster 纳入 xDS 依赖管理,`xds_dependency_manager.h:72-77`)。

> **钉死这件事**:gRPC 内置 xDS client 直接复用 Envoy 的 data-plane-api proto(upb),和 Envoy/Istio 协议级互通——同一个控制面能同时服务 Envoy sidecar 和 gRPC client。gRPC 自己还有 HTTP filter 扩展、cluster_specifier_plugin、RLS 等专属扩展。这套互通性是无 sidecar mesh 能落地的生态前提。

---

## 七、技巧精解:gRPC 内置 xDS client 怎么省掉 sidecar

本章最硬的技巧,是"client 内置 xDS"这套架构本身。我们把它和 sidecar 模式单独对比,钉死它的精妙。

### 朴素方案:每个服务配一个 Envoy sidecar

标准 service mesh 部署:每个服务实例旁边一个 Envoy sidecar。服务 → sidecar → sidecar → 服务,两跳。Envoy 收 xDS、做路由/LB/熔断,服务无感知。

这套的优点是语言无关(任何服务都能配 sidecar)。但代价前面讲过:多一跳、多一份资源、多一层运维。在大规模集群里,光 sidecar 吃掉的资源可能占整个集群的 10%~20%。

### gRPC 方案:client 内置 xDS client

gRPC 的方案:把 xDS client 直接织进 client 进程。具体在源码里落地为三个层次:

**层次一:resolver 层**。`xds://` URI 触发 `XdsResolver`(`xds_resolver.cc:103`),它用 `XdsDependencyManager`(`xds_dependency_manager.h:36`)订阅 LDS→RDS→CDS→EDS 依赖链,把结果合成 service config。这一层替代了 Envoy 的 listener + route 配置加载。

**层次二:LB 层**。service config 触发 `xds_cluster_manager` → `cds` → `xds_cluster_impl` → `xds_wrr_locality` → `round_robin` 的包装链。这一层替代了 Envoy 的 cluster + endpoint LB 逻辑(包括熔断、drop、locality 加权)。

**层次三:通信层**。gRPC 用自己的 channel 连控制面跑 ADS(`xds_transport_grpc.cc:278` 的 `grpc_channel_create`),channel creds 来自 bootstrap。这一层替代了 Envoy 和控制面的 xDS 连接。

### 省掉的是什么

对比 sidecar,gRPC 内置 xDS 省掉的是:

1. **sidecar 进程本身**:不用部署 Envoy,省 CPU/内存(每个实例省几十 MB 起)。
2. **额外的一跳**:调用直接 client → server,不经 localhost 代理。
3. **sidecar 运维**:不用注入、升级、排查 sidecar。

换来的是约束:**服务必须用 gRPC(或支持 xDS 的框架)**。如果服务用别的 RPC(比如裸 HTTP),它还得靠 sidecar。但在"gRPC 已经是内部 RPC 标准"的大规模微服务场景,这个约束天然满足,trade-off 极其划算。

```
   sidecar 模式 vs gRPC 内置 xDS
   ┌──────────────────────────┐    ┌──────────────────────────┐
   │ sidecar 模式              │    │ gRPC 内置 xDS            │
   │  服务 → Envoy → Envoy → 服务│    │  gRPC client → gRPC server│
   │  (2 跳 + 2 个 Envoy)       │    │  (1 跳,client 自带 xDS)  │
   ├──────────────────────────┤    ├──────────────────────────┤
   │ + 语言无关                 │    │ + 少一跳,省资源          │
   │ - 多 2 个 Envoy 进程       │    │ + 无 sidecar 运维         │
   │ - 多 1 跳延迟              │    │ - client 必须支持 xDS     │
   │ - sidecar 运维负担         │    │   (gRPC 天然满足)         │
   └──────────────────────────┘    └──────────────────────────┘
```

> **钉死这件事**:gRPC 内置 xDS client 在源码里落地为三层——resolver 层(XdsResolver + XdsDependencyManager 管依赖链)、LB 层(层层包装的 xDS LB policy)、通信层(用 gRPC 自己连控制面)。它省掉 sidecar 的三重代价,前提是 client 支持 xDS。这是"无 sidecar service mesh"的根,也是 gRPC 在治理生态里的核心竞争力。

---

## 八、章末小结

### 回扣主线

本章服务二分法的**治理 / 衔接**那一面。它是"客户端治理"那条线的动态化终章:

- P4-13 Resolver 讲的是**静态/半动态**解析(`dns:///`、`passthrough:///`)。
- P4-15 负载均衡讲的是**固定策略**(round_robin、pick_first、ring_hash)。
- **本章 xDS** 讲的是**控制面驱动的动态治理**:路由、LB 策略、熔断、端点列表,都由控制面实时下发。这是治理的"动态化"终态。

同时,本章是本书通往《Envoy 设计与实现》那本的**接口**。gRPC 内置 xDS client 直接复用 Envoy 的 proto,和 Envoy/Istio 协议级互通。xDS 协议本身的设计(为什么是 LDS/RDS/CDS/EDS 四类、ADS 怎么聚合、Delta xDS 怎么增量、控制面怎么实现),是《Envoy》那本的主线;本章只讲 gRPC 作为 xDS **消费端**的独有细节(resolver 怎么解析 `xds://`、LB 怎么层层包装、bootstrap 怎么配),Envoy 通用细节请移步那本。

### 五个为什么

1. **为什么 gRPC 要内置 xDS client 而不只用 sidecar?**——sidecar 有三重代价(多一跳、多资源、多运维);gRPC 内置 xDS 让 client 自己就是数据面,省掉这些代价,前提是 client 支持 xDS(gRPC 天然满足)。
2. **为什么 xDS 是 LDS→RDS→CDS→EDS 的依赖链?**——因为这正是"监听器→路由→集群→端点"的自然层次;每层可独立动态下发(扩容只动 EDS,改路由只动 RDS),粒度精细。
3. **为什么 xds_resolver 不直接产出地址列表?**——因为它把地址解析交给 LB 层(从 EDS 拿),自己只合成 service config 触发 LB 链路。这种"resolver 产出 LB config"让 xDS 复用 gRPC 既有 LB 基础设施。
4. **为什么 XdsDependencyManager 要级联订阅而不是并行?**——因为只有拿到 LDS 才知道订阅哪个 RDS,拿到 RDS 才知道订阅哪些 CDS——资源名是上一级决定的,无法并行。级联订阅避免"订阅了用不上的资源"。
5. **为什么 gRPC 能和 Envoy/Istio 互通?**——因为 gRPC 直接复用 Envoy 的 data-plane-api proto(upb),说同一种 xDS 语言。同一个控制面能同时服务 Envoy sidecar 和 gRPC client。

### 想继续深入往哪钻

- 想看 xDS client 核心:读 `src/core/xds/xds_client/xds_client.h:60` 的 `XdsClient` + `xds_client.cc:734` 的 ADS 调用。
- 想看 resolver 怎么触发:读 `src/core/resolver/xds/xds_resolver.cc:103` 的 `XdsResolver` + `xds_resolver.cc:838` 的 `StartLocked`。
- 想看依赖链编排:读 `src/core/resolver/xds/xds_dependency_manager.cc` 的四个 watcher(L47/82/122/160)。
- 想看 LB 层包装:读 `src/core/load_balancing/xds/cds.cc:208` 的 `CdsLb`、`xds_cluster_impl.cc:177` 的熔断/drop、`xds_wrr_locality.cc` 的 locality 加权。
- 想看 bootstrap:读 `src/core/xds/grpc/xds_bootstrap_grpc.cc:241` 的 schema + `xds_client_grpc.cc:201` 的环境变量读取。
- **想深入 xDS 协议本身、控制面实现、ADS/Delta xDS 演化**:移步《Envoy 设计与实现》那本。本章是那本的接口。

### 引出下一章

xDS 讲完,本书第 6 篇(可观测、性能、生态)就收尾了。整个 gRPC core 的源码之旅,从 P0-01 的"为什么需要 gRPC",经过契约(第 1 篇)、HTTP/2 传输(第 2 篇)、调用生命周期(第 3 篇)、客户端治理(第 4 篇)、生产可用(第 5 篇)、到本篇的可观测/性能/生态,主线已经走完。最后一章 P7-23,我们做全书收束:把"把方法调用变成一条流"这件事得到的(多路复用、背压、流式、跨语言)和付出的(HTTP/2 复杂度、调试难)算一笔总账,对照 REST/Dubbo/Thrift,并展望 gRPC core 的 Promise-based 重构把这套带向何方。

> **下一章**:[P7-23 · 全书收束:RPC 的演化与"流"的代价](P7-23-全书收束-RPC的演化与流的代价.md)
