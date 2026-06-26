# 第 5 篇 · 第 16 章 · xDS 协议总览:LDS/RDS/CDS/EDS/SDS

> **核心问题**:前面 15 章,我们讲完了数据面——一条流量怎么从 listener 进来、穿过 filter chain、被 router 选到 cluster、负载均衡挑一个 endpoint、连上后端、响应原路返回。但有个问题一直悬着:**所有这些 listener、filter chain、route_config、cluster、endpoint,配置到底是从哪来的?** 第 0 章里我们点过一句:它们不是写死的,而是控制面(control plane)通过 **xDS** 动态下发的。可 xDS 到底是个什么东西?有几类?每类管什么?控制面"推一份配置"给数据面,怎么保证数据面真的收到了、真的生效了、出错了控制面还知道?这一章,我们就把 xDS 这个控制面的"通用语言"彻底拆开——从协议(proto)、到五类资源(LDS/RDS/CDS/EDS/SDS)、再到它最关键的招牌机制 **resource version 协商 + ACK/NACK**。

> **读完本章你会明白**:
> 1. xDS 到底是什么——它不是"一个 API",而是一套**控制面与数据面之间的配置下发契约**,定义在 proto 里,跑在 gRPC 双向流上。为什么这套契约能让"任何会说话的控制面(Istio/Consul/OSM)都能驱动任何会说话的数据面(Envoy)"。
> 2. 五类 xDS 各管什么——LDS=Listener、RDS=RouteConfiguration、CDS=Cluster、EDS=Endpoint(服务发现的核心)、SDS=Secret(证书)。它们不是随意分的,而是按**资源的语义边界**切的:独立下发、独立版本、独立热更新。还有它们之间的**引用依赖**(LDS→RDS→CDS→EDS)为什么这么设计。
> 3. **resource version 协商 + ACK/NACK**——为什么 xDS 不是"单向 best-effort 推送",而是"带确认的可靠下发":控制面每份配置带 `version_info`,数据面应用成功回 ACK(带回该 version)、失败回 NACK(带 `error_detail`,保持旧版本)。这套"版本握手"为什么 sound(最终一致、不丢更新、可观测),朴素地"单向推不管收没收到"会撞什么墙。
> 4. **type_url + 泛型 DiscoveryRequest/Response**——为什么 xDS 用"一个统一的 gRPC 方法 + `type_url` 字段区分资源类型",而不是给每类资源单独定义一个 gRPC 服务(其实它两者都有,看你怎么用)。这是"泛型接口 + 类型标签"的经典设计。
> 5. SotW(State of the World)与 Delta xDS 的区别,以及为什么 Delta 是演进方向——为下一章 P5-17 铺路。

> **如果一读觉得太难**:先只记住三件事——① xDS 是控制面给数据面动态下发配置的协议,有五类(L/R/C/E/S),各管一种资源;② 控制面每推一份配置带 `version_info`,数据面应用成功回 ACK、失败回 NACK,这就是"版本握手",保证一致性;③ 五类之间有依赖:LDS 里的 listener 引用 RDS 的 route_config,route_config 引用 CDS 的 cluster,cluster 引用 EDS 的 endpoint——所以 EDS 变了不必重发 LDS,各层独立热更新。

---

## 〇、一句话点破

> **xDS 是控制面(control plane)和数据面(data plane)之间的一套配置下发契约:控制面按"资源类型"(LDS/RDS/CDS/EDS/SDS 五类)把配置打成带版本号的包,通过 gRPC 双向流推给数据面;数据面应用成功回 ACK、失败回 NACK,控制面据此知道每个数据面到底生效到哪个版本。这不是"best-effort 单向推",而是"带版本握手的可靠下发"——这就是 Envoy 能不停机、可观测地动态更新配置的根。**

这是结论,不是理由。本章倒过来拆:先讲"配置为什么要动态下发"这个问题本身(承接 P0-01 点的那一句),再讲 xDS 把配置拆成五类资源的道理,然后讲 resource version 协商这个招牌机制凭什么保证一致,最后把 type_url 泛型接口、SotW vs Delta 的演进一次铺开。

---

## 一、回到起点:配置为什么要"动态下发"(承接 P0-01)

第 0 章我们讲过,Nginx 这类经典代理的痛,第一道就是**静态配置 + reload 才生效**:后端实例每天频繁上下线,每次变更都得改配置文件、发 `SIGHUP` 让 worker 重新加载,而 reload 本身有 worker 交接抖动(新老 worker 并存、内存翻倍、长连接可能被中断、p99 飙升)。微服务的"动态现实"和"静态配置"根本对不上。

Envoy 的回答,是把"配置"从静态文件,变成**控制面动态下发**的东西。这就引出一个根本问题:

> **控制面和数据面,得有一套共同的语言——控制面按什么格式推配置?数据面按什么格式回?推了之后,数据面到底收到没有、生效没有、出错没出错,控制面怎么知道?**

这套"共同的语言",就是 **xDS**。它不是某个具体的 API 函数,而是一套**协议(proto 定义)+ 交互规则(版本协商)**。我们这一整篇(第 5 篇四章),都在拆这套东西。本章先把"协议总览"立起来:它有哪几类、每类管什么、版本怎么协商。下一章 P5-17 拆"怎么传"(SotW/Delta/ADS 三种传输模式),P5-18 拆 listener 怎么不停机热更新,P5-19 拆 CDS/EDS 的动态发现。

### xDS 走 gRPC 双向流——承接《gRPC》

一个关键事实:**xDS 协议最初就是 Envoy 自己设计的**(proto 在 `api/envoy/service/discovery/v3/`),它跑在 **gRPC 双向流(bidirectional streaming)** 上。这一点,和《gRPC》那本 P6-22 讲过的"gRPC 客户端内置 xDS client"是同一套协议——gRPC 客户端可以不经过 Envoy sidecar,直接作为 xDS client 从 Istio 拿配置,自己做负载均衡。**xDS 成了控制面与数据面之间的通用契约**,这是 Envoy 对整个云原生生态的贡献。

本书讲的是 **Envoy 作为 xDS 消费端(client 端)**——它怎么订阅、怎么收配置、怎么 ACK/NACK、怎么把收到的配置热更新到 filter chain。至于 gRPC 双向流本身的机制(HTTP/2 多路复用、流控),那是《gRPC》那本 P2 的内容,本书**不重复**,只承接、指路:你可以把 xDS 流想成一条长期打开的 gRPC 双向流,控制面随时推 `DiscoveryResponse`,数据面随时回 `DiscoveryRequest`,就这么简单。

### 为什么是 gRPC 双向流,而不是 HTTP 轮询或消息队列

xDS 选 gRPC 双向流,不是随手挑的,而是几个因素叠加:

1. **低延迟的服务端推送**:服务发现要秒级感知实例上下线,这要求"控制面知道有变更时能立刻推",而不是等数据面来轮询。gRPC 双向流天然支持服务端主动推(StreamAggregatedResources 服务端随时 write)。
2. **双向、有状态**:ACK/NACK 需要数据面回信,且每个 type_url 有独立的版本状态——这要求一条**有状态的双向连接**,而不是无状态的 HTTP 请求。gRPC 双向流正合适。
3. **多路复用**:一条 HTTP/2 连接上可以同时跑多个 xDS 流(LDS 流、RDS 流...),或者用 ADS 把所有类型复用进一条流——HTTP/2 多路复用是基础(承接《gRPC》讲的 HTTP/2 帧)。
4. **proto 强类型**:gRPC 和 protobuf 天生一对,配置是强类型 proto,比 JSON 配置安全、紧凑。

> **对照"用消息队列(Kafka)下发配置"**:有人想过用 Kafka 这种消息队列做配置下发——天然有序、有消费位点。但消息队列的模型是"事件流",不适合"配置快照"(配置关心的是"当前值",不是"变更历史"),而且消息队列做"每个客户端独立订阅 + 独立版本状态"不如 gRPC 流直接。**gRPC 双向流 + version 协商,是"配置下发"场景的最优解**——它兼具推送的低延迟、双向的确认、proto 的强类型、HTTP/2 的多路复用。

> **钉死这件事**:理解 xDS 的起点,不是"它有哪些 YAML 字段",而是**它是一套跑在 gRPC 双向流上的、带版本握手的、分类型的配置下发契约**。控制面会说这套话,数据面也会说这套话,任何会说的控制面都能驱动任何会说的数据面。

---

## 二、五种 xDS:配置怎么按"资源类型"切的

xDS 的 "x" 历史上就是个占位符——"什么 Discovery Service"。最早只有 **LDS**(Listener Discovery Service)和 **SDS**(Secret Discovery Service),后来陆续加了 **RDS**(Route)、**CDS**(Cluster)、**EDS**(Endpoint)。这五类**不是拍脑袋分的**,而是按配置的**语义边界**切的:每一类对应一种独立的资源,有独立的生命周期、独立的变更频率、独立的热更新方式。

### 五类资源一张表

| xDS | 资源 proto | type_url | 管什么 | 变更频率 | 动态价值 |
|-----|-----------|----------|--------|---------|---------|
| **LDS** | `envoy.config.listener.v3.Listener` | `type.googleapis.com/envoy.config.listener.v3.Listener` | 监听器:端口 + filter chain 骨架 | 低(加新服务时) | 不重启起新 listener |
| **RDS** | `envoy.config.route.v3.RouteConfiguration` | `…route.v3.RouteConfiguration` | 路由表:virtual host + route 规则 | 中(灰度/切流) | 热更新路由,不停机 |
| **CDS** | `envoy.config.cluster.v3.Cluster` | `…cluster.v3.Cluster` | 上游集群:集群定义、LB 策略 | 低(加新集群) | 动态加集群 |
| **EDS** | `envoy.config.endpoint.v3.ClusterLoadAssignment` | `…endpoint.v3.ClusterLoadAssignment` | 端点:后端实例列表 | **高(实例上下线)** | **秒级服务发现,核心** |
| **SDS** | `envoy.extensions.transport_sockets.tls.v3.Secret` | `…transport_sockets.tls.v3.Secret` | 密钥:TLS 证书/私钥 | 极低(证书轮换) | 不重启换证书 |

```
   控制面 (Istio / 自研 control plane)
   ┌─────────────────────────────────────────────┐
   │   LDS  ─┐                                    │
   │   RDS  ─┤   各自独立下发、独立版本、独立热更新  │
   │   CDS  ─┤   (走 gRPC 双向流)                  │
   │   EDS  ─┤                                    │
   │   SDS  ─┘                                    │
   └─────────────────────────────────────────────┘
           │ xDS (DiscoveryResponse)
           ▼
   数据面 (Envoy)
   ┌─────────────────────────────────────────────┐
   │  Listener Manager  ← LDS                    │
   │  Route Provider    ← RDS                    │
   │  Cluster Manager   ← CDS + EDS              │
   │  Secret Manager    ← SDS                    │
   └─────────────────────────────────────────────┘
           │ ACK/NACK (DiscoveryRequest)
           ▲
```

### 为什么是五类独立资源,而不是"一个大配置"

这是第一个要想清楚的 why。朴素的想法:配置就是配置,控制面把整个 Envoy 的配置(所有 listener、所有 route、所有 cluster、所有 endpoint、所有证书)打成一个大 JSON,一把推给数据面,多简单?

> **不这样会怎样**(把所有配置打成一个大包):
> 1. **变更粒度太粗**:后端扩容了一个 Pod(只有 EDS 里一个 endpoint 变了),你得把整个配置(几百个 listener、上千个 route、几百个 cluster)全部重发一遍。配置一大,序列化、传输、解析、应用的开销全都上去了。一个实例变更触发全量重发,在几百个实例频繁上下线的微服务里,带宽和 CPU 会被吃光。
> 2. **热更新粒度太粗**:一个大配置包,你没法"只热更新 endpoint,不动 listener"。要么全 reload(回到 Nginx 的老路),要么自己想办法拆。而 listener 的热更新(drain 旧连接)和 endpoint 的热更新(改个 upstream 列表)代价天差地别——前者要优雅退出在途连接,后者只是改个内存里的 vector。混在一起,只能按最重的来。
> 3. **版本管理混乱**:一个大包只有一个版本号,你没法说"我的 listener 是 v3、但 endpoint 是 v7"。一旦某个子配置出问题,整个大包要么全用要么全不用,没法精细控制。
> 4. **故障爆炸半径大**:一个大配置里有一处写错(比如某个 route 的正则非法),整个大包被拒绝,**所有配置都更新不了**——包括那些没问题的。

> **所以这样设计**:xDS 按**语义边界**把配置切成五类独立资源,每类有独立的 `type_url`、独立的版本、独立的订阅、独立的 ACK/NACK。EDS 变了只发 EDS,CDS 没变就不重发 CDS;EDS 出问题 NACK 掉,不影响 LDS/RDS/SDS 继续工作。**独立下发、独立版本、独立热更新**——这就是"五类"存在的根本理由。

举个具体的场景,感受"五类独立"的价值:假设你的网格有 200 个 cluster,每个 cluster 有 50 个 endpoint,总共 10000 个 endpoint。某个 cluster(叫 `pay-svc`)扩容了 1 个 Pod——只有这个 cluster 的 EDS 需要更新。

- **五类独立**:控制面只推一份 `ClusterLoadAssignment(cluster_name="pay-svc", endpoints=[新增的])`,几十字节,秒级生效。其它 199 个 cluster、所有 listener、所有 route,一个字节都不动。
- **如果是一个大包**:控制面要把 200 个 cluster + 10000 个 endpoint + 所有 listener/route 全序列化(几 MB),数据面全量解析、和旧版本 diff、逐个应用——CPU 飙升、网络带宽吃满,而且应用周期里整个数据面可能"半新半旧"(部分 cluster 更新了,部分还没),一致性的灰区被放大。

这就是为什么 xDS 在大规模服务网格里(Istio 动辄几千个 service、几万个 endpoint)能撑住——**粒度切得细,变更才轻**。这个"按资源类型独立管理"的思路,和 Kubernetes 里"每个对象(Pod/Service/Deployment)独立 watch、独立版本、独立 reconcile"是同一个范式。**这是控制面设计的一个通用模式:不要把所有状态揉成一坨,要按语义边界拆开。**

### 五类之间的引用依赖:LDS → RDS → CDS → EDS

这五类不是完全平行的,它们之间有**引用关系**——一个 listener 引用一个 route_config,一个 route_config 引用一个 cluster,一个 cluster 引用一组 endpoint。串起来是这样:

```
   LDS: Listener "listen_0"
         └─ 引用 → RDS: RouteConfiguration "route_0"
                     └─ route.action.cluster → CDS: Cluster "cluster_backend"
                                                  └─ 引用 → EDS: ClusterLoadAssignment "cluster_backend"
                                                              └─ endpoints: [ep1, ep2, ep3]

   SDS: Secret "server_cert"  ← 被 LDS 里的 listener 的 transport_socket 引用(独立,不在主链上)
```

也就是说,一条流量的"决策链"是:**listener(LDS)决定用哪条 filter chain → router 按 route_config(RDS)选 cluster → cluster(CDS)的 LB 策略挑 endpoint → endpoint 列表(EDS)**。证书(SDS)是横向的,被 listener 或 cluster 的 transport_socket 引用。

这个依赖关系决定了**几件重要的事**:

1. **EDS 是变更最频繁的**(实例上下线),它挂在依赖链最底层——EDS 变了,不需要重发 LDS/RDS/CDS,只要推一份新的 `ClusterLoadAssignment` 就行。这就是"独立下发"的最大红利:高频变更(EDS)和低频变更(LDS)解耦。
2. **资源发现是"按需"的**:Envoy 不是一开始就把所有 EDS 都订阅了,而是 LDS 发来了一个 listener、里面引用了某个 cluster,Envoy 才去订阅那个 cluster 对应的 EDS。这种"按引用链拉取"的设计,避免了订阅一堆用不上的资源。proto 里 `DiscoveryRequest` 的注释把这点说得很清楚:LDS/CDS 的 `resource_names` 可以为空(返回所有),而 EDS/RDS 的 `resource_names` 是 LDS/CDS 响应**推导出来**的。
3. **依赖关系带来顺序问题**:如果 LDS、RDS、CDS、EDS 走**各自独立的 gRPC 流**,那"先到哪个后到哪个"是不可控的——可能出现"RDS 引用了一个 cluster,但 CDS 还没把这个 cluster 推过来"的空窗。这就是为什么 Envoy 又搞了 **ADS(Aggregated Discovery Service,聚合订阅)**——用一个流拉所有类型,控制面能保证下发顺序。ADS 是 P5-17 的主角,这里先埋个钩子。

> **钉死这件事**:五类 xDS 是按"资源语义边界"切的,各自独立下发、独立版本、独立热更新;它们之间有引用依赖(LDS→RDS→CDS→EDS),这决定了 EDS(最底层、最频繁)可以独立变更而不牵动上层,也决定了"按引用链按需订阅"的设计。

---

## 三、xDS 的协议骨架:一个泛型的 DiscoveryRequest/Response

讲完了"五类",我们看 xDS 协议本身长什么样。它的核心,是两个 proto 消息:**`DiscoveryRequest`(数据面 → 控制面)和 `DiscoveryResponse`(控制面 → 数据面)**。这两个消息定义在 [`api/envoy/service/discovery/v3/discovery.proto`](../envoy/api/envoy/service/discovery/v3/discovery.proto)。

### DiscoveryResponse:控制面推一份配置

控制面推给数据面的 `DiscoveryResponse`,关键字段(`[#next-free-field: 8]`):

```protobuf
message DiscoveryResponse {
  // 配置的版本号(整个响应一个版本)
  string version_info = 1;
  // 这一批资源(LDS 的若干 Listener、EDS 的若干 ClusterLoadAssignment...)
  repeated google.protobuf.Any resources = 2;
  // 资源类型,用 type_url 区分(见下)
  string type_url = 4;
  // nonce:这次响应的唯一标识,数据面 ACK/NACK 时要带回来
  string nonce = 5;
  // ...canary / control_plane / resource_errors 等少用字段
}
```

最关键的两个字段是 `version_info`(版本号)和 `nonce`(防重复的标识)。注意 `resources` 是 `repeated google.protobuf.Any`——这是个**类型擦除**的字段,`Any` 能装任何 proto 消息,具体是什么类型,靠 `type_url` 标识。

### DiscoveryRequest:数据面的订阅、ACK、NACK

数据面发给控制面的 `DiscoveryRequest`(`[#next-free-field: 8]`),一个消息身兼三职:

```protobuf
message DiscoveryRequest {
  // 上一次成功应用的版本(ACK 时带新版本,NACK 时带旧版本)
  string version_info = 1;
  config.core.v3.Node node = 2;                  // 我是谁(节点标识)
  // 我订阅哪些资源(按名字:LDS/CDS 可空=全部;EDS/RDS 列名字)
  repeated string resource_names = 3;
  string type_url = 4;                           // 这一次请求是哪类资源
  // 对应上一次响应的 nonce(ACK/NACK 都要带)
  string response_nonce = 5;
  // NACK 时填这个:为什么拒绝(google.rpc.Status)
  google.rpc.Status error_detail = 6;
}
```

这一条消息,在不同时机表达三种不同意思:

1. **首次订阅**:`version_info` 和 `response_nonce` 为空,`resource_names` 列出想要哪些资源,`type_url` 说明是哪类。
2. **ACK**(应用成功):`version_info` = 刚成功应用的版本,`response_nonce` = 刚收到响应的 nonce,`error_detail` 不填。
3. **NACK**(应用失败):`version_info` = **保持上一次成功的旧版本**(不更新),`response_nonce` = 刚收到响应的 nonce,`error_detail` = 失败原因。

**妙就妙在:同一个 proto 消息,靠 `version_info` / `error_detail` 这两个字段的有无和取值,表达了完全不同的语义。** 协议设计极其紧凑——没有单独的 `AckMessage`、`NackMessage`、`SubscribeMessage`,全塞进一个 `DiscoveryRequest`。

### type_url:泛型接口 + 类型标签

注意 `DiscoveryRequest` 和 `DiscoveryResponse` 里都有一个 `type_url` 字段,值长这样:`type.googleapis.com/envoy.config.listener.v3.Listener`。这就是 type_url——**一个字符串,标识这批 resources 是什么类型**。

为什么这么设计?因为 xDS 的五类资源(LDS/RDS/CDS/EDS/SDS)用的是**同一个 proto 消息**(`DiscoveryRequest`/`DiscoveryResponse`),只是 `resources` 里的 `Any` 装的东西不同。type_url 就是那个"区分标签":

- `type.googleapis.com/envoy.config.listener.v3.Listener` → LDS
- `type.googleapis.com/envoy.config.route.v3.RouteConfiguration` → RDS
- `type.googleapis.com/envoy.config.cluster.v3.Cluster` → CDS
- `type.googleapis.com/envoy.config.endpoint.v3.ClusterLoadAssignment` → EDS
- `type.googleapis.com/envoy.extensions.transport_sockets.tls.v3.Secret` → SDS

`type.googleapis.com/` 这个前缀,是 protobuf 的 `Any` 的标准约定——任何 `Any` 都有一个 `type_url`,前缀是类型的全限定名(包名 + 消息名)。Envoy 把它直接拿来当 xDS 的类型标识,一举两得。

> **不这样会怎样**(给每类资源单独定义一个 gRPC 服务):其实 Envoy **也**这么做了——`api/envoy/service/listener/v3/lds.proto` 里有个 `ListenerDiscoveryService`,`route/v3/rds.proto` 里有 `RouteDiscoveryService`……每个资源类型一个 gRPC 服务,各有 `StreamXxx` / `FetchXxx` 方法。这是"专用接口"路线,适合每类资源走自己独立的 gRPC 流(叫 singleton xDS)。但 Envoy 又定义了 [`AggregatedDiscoveryService`(ADS)](../envoy/api/envoy/service/discovery/v3/ads.proto):一个 gRPC 流复用所有类型,这时候就得靠 `type_url` 区分了——`StreamAggregatedResources` 收到的每个 `DiscoveryRequest` 都带 `type_url`,服务端据此分拣。**所以"泛型接口 + type_url"是为 ADS 聚合订阅准备的**;singleton 模式下 type_url 是冗余但不冲突。这是"xDS 协议演进"留下的历史层。

### 源码里 type_url 怎么来的:模板,不是常量

一个反直觉的源码事实:Envoy 源码里**没有** `kLdsTypeUrl = "type.googleapis.com/..."` 这样的具名常量。type_url 是**运行时从 proto 反射动态拼出来的**。看 [`source/common/config/resource_name.h`](../envoy/source/common/config/resource_name.h#L14-L23):

```cpp
// Get resource name from api type.
template <typename Current> std::string getResourceName() {
  return std::string(createReflectableMessage(Current())->GetDescriptor()->full_name());
}

// Get type url from api type.
template <typename Current> std::string getTypeUrl() {
  return "type.googleapis.com/" + getResourceName<Current>();
}
```

`getTypeUrl<envoy::config::listener::v3::Listener>()` 编译期展开,`Current` 是 proto 类型,`GetDescriptor()->full_name()` 拿到 proto 全限定名(`envoy.config.listener.v3.Listener`),前面拼上 `type.googleapis.com/`。整个 type_url 从 C++ 类型**一行模板生成**。

> **技巧点睛(为什么用模板而不是常量)**:如果用常量(`constexpr const char* kLdsTypeUrl = "..."`),每加一类资源就得手写一个常量、还容易和 proto 全名写错对不上。用模板,只要把 proto 类型传进去,type_url 自动和 proto 定义绑定——proto 改了名字,模板自动跟上,**类型安全**(C++ 类型一旦对不上 proto,编译就过不去)。这是 C++ 模板元编程在"配置协议绑定"上的典型用法。代价是 type_url 是 `std::string`(运行时构造),但这是一次性的、不在热路径,无所谓。

各处的用法就是把这个模板实例化。比如 CDS 在 [`source/common/upstream/cluster_manager_impl.cc`](../envoy/source/common/upstream/cluster_manager_impl.cc)(订阅 cluster 时):

```cpp
Config::getTypeUrl<envoy::config::cluster::v3::Cluster>()
```

SDS 因为有特殊处理(见后),在 [`subscription_factory_impl.cc`](../envoy/source/common/config/subscription_factory_impl.cc#L66-L67) 里被专门识别:

```cpp
if (type_url != Envoy::Config::getTypeUrl<envoy::extensions::transport_sockets::tls::v3::Secret>()) {
  // 非 SDS,需要检查 backing cluster;SDS 跳过(见下)
}
```

---

## 四、招牌机制:resource version 协商 + ACK/NACK

讲完了协议骨架,我们来到 xDS **最核心、最招牌**的机制:**resource version 协商 + ACK/NACK**。这是"xDS 凭什么保证配置一致"的根,也是本章技巧精解的主角。我们先讲清它解决什么问题,再讲它怎么实现。

### 不这样会怎样:单向推送的"一致性陷阱"

假设 xDS 是个朴素的"单向推送":控制面推一份配置,数据面收到就用,不回任何确认。听起来够用,但在分布式系统里会撞上三堵墙:

**墙一:网络抖动,不知道谁收到了。** 控制面发了 v2(摘除故障实例的 EDS),网络抖了一下,某个 Envoy 没收到。控制面以为"所有 Envoy 都切到 v2 了",其实有的还在用 v1——还在往那个故障实例发流量。等告警来了才发现,已经 5xx 一片。**没有确认机制,控制面对数据面的真实状态一无所知。**

**墙二:配置非法,不知道谁失败了。** 控制面推了一份新 route_config,但里面某条 route 的正则写错了。数据面应用失败,**默默丢弃**,继续用旧的。控制面以为"都切到新版了",其实数据面还停在旧版——灰度发布"以为切了 10%,其实切了 0%"。**没有 NACK,失败是静默的。**

**墙三:重连后状态丢失。** gRPC 流断了重连。重连后,控制面不知道数据面现在生效到哪个版本——是该重发 v1 还是直接发 v3?数据面也不知道控制面有没有新版本。**没有版本握手,重连就是一次状态归零。**

> **钉死这件事**:在分布式系统里,**没有确认的单向推送,本质上是不可靠的**——它把"配置一致性"这个本该由协议保证的性质,甩给了"网络永远可靠"这个错误假设(《gRPC》《TiKV》里讲过的网络八条谬误,这里又中了一条)。生产级的动态配置,**必须有确认机制**。

### 所以这样设计:版本握手——每份配置带版本,应用成功带版本回 ACK

Envoy 的解法,是给每次下发加一个**版本握手**:

1. **控制面每份 `DiscoveryResponse` 带两个东西**:`version_info`(整批资源的版本号,控制面自己定的,单调递增或时间戳都行,只要能区分先后)、`nonce`(这次响应的唯一标识,数据面回信时要用)。
2. **数据面收到后尝试应用**(把 resources 解析、校验、塞进 listener manager / cluster manager / route provider)。
3. **应用成功 → 回 ACK**:发一个 `DiscoveryRequest`,`version_info` 填**刚成功应用的版本**、`response_nonce` 填**这次响应的 nonce**、`error_detail` 不填。这等于告诉控制面:"v2 我收到了、应用成功了,我现在生效在 v2。"
4. **应用失败 → 回 NACK**:发一个 `DiscoveryRequest`,`version_info` 填**上一次成功的旧版本**(不更新!)、`response_nonce` 填这次响应的 nonce、`error_detail` 填失败原因。这等于告诉控制面:"v2 我收到了,但应用失败了(原因是 XXX),我还停在 v1。"
5. **控制面据此知道每个数据面的真实状态**:收到 ACK v2 → 这个 Envoy 在 v2;收到 NACK(error_detail 有内容)→ 这个 Envoy 还在 v1 且告诉你为什么。

这套机制的关键在于:**ACK 和 NACK 长得几乎一样(都是 DiscoveryRequest),区别只在 `version_info` 和 `error_detail`**——

| | version_info | response_nonce | error_detail |
|---|---|---|---|
| **ACK** | 新版本(刚应用的) | 这次响应的 nonce | 不填 |
| **NACK** | **旧版本**(保持上次的) | 这次响应的 nonce | 填(google.rpc.Status,code+message) |

控制面收到一个 `DiscoveryRequest`,看 `error_detail` 有没有内容就能区分 ACK 还是 NACK;看 `version_info` 就知道数据面生效在哪个版本。**一个字段(`error_detail` 的有无)决定语义,一个字段(`version_info`)报告状态**——极其紧凑。

```
   控制面                                       数据面 (Envoy)
     │                                            │
     │  DiscoveryResponse(v2, nonce=A)            │
     │ ─────────────────────────────────────────▶ │  尝试应用 v2
     │                                            │  ✓ 成功
     │  DiscoveryRequest(version=v2, nonce=A)     │  ← ACK
     │ ◀───────────────────────────────────────── │
     │                                            │
     │  (控制面知道:这个 Envoy 生效在 v2)          │
     │                                            │
     │  DiscoveryResponse(v3, nonce=B)            │
     │ ─────────────────────────────────────────▶ │  尝试应用 v3
     │                                            │  ✗ 失败(route 正则非法)
     │  DiscoveryRequest(version=v2, nonce=B,     │  ← NACK(版本回退到 v2!)
     │    error_detail={code:Internal, msg:...})  │
     │ ◀───────────────────────────────────────── │
     │                                            │
     │  (控制面知道:这个 Envoy 还在 v2,且 v3 失败) │
```

### 为什么这套"版本握手"是 sound 的

这套机制为什么能保证一致性?我们从三个角度验证:

**1. 不丢更新。** 控制面每发一个 `DiscoveryResponse` 都带新 nonce,数据面必须用对应 nonce 回 `DiscoveryRequest` 才算"这次响应处理完了"。如果数据面没回(网络断了),控制面的流断了重连,会重新发——因为数据面重连后发的第一个 `DiscoveryRequest` 里的 `version_info` 反映它真实生效的版本,控制面据此判断要不要重发。**版本号是"我现在在哪"的真相来源。**

**2. 可观测。** 控制面不是"推完就忘",而是**收到 ACK 才算"这个数据面确认到这个版本了"**。NACK 还带 `error_detail`,控制面知道谁失败了、为什么失败。这在故障摘除、灰度发布场景至关重要——你可以从控制面看到"50 个 Envoy 里 48 个 ACK 了 v2(故障实例已摘除),2 个 NACK 了(配置解析失败),这 2 个还在往故障实例发流量,得修"。**没有这套握手,这种可观测性根本不存在。**

**3. 最终一致。** 数据面应用成功才更新 `version_info`,失败就回退。这意味着任何时候,数据面的 `version_info` 都反映它**真实生效**的版本——不是"收到的最新版本",而是"应用成功的最新版本"。控制面拿到的是真相,不是幻觉。配合控制面的重发(retry),系统最终会收敛到"所有数据面要么在最新版本,要么在某个已知旧版本 + 已知失败原因"——这就是**最终一致**。

> **钉死这件事**:resource version 协商 + ACK/NACK 不是"花架子",它是 xDS 作为**生产级动态配置协议**的底线。它把"配置一致性"从"网络假设"里拿回来,变成"协议保证":每份配置有版本、每次应用有确认、每次失败有原因、每个数据面的真实状态控制面都知道。**这是 xDS 能让 Envoy 不停机、可观测地动态更新的根。**

---

## 五、源码里 ACK/NACK 怎么构造的

讲清了机制,我们看源码里这套握手是怎么实现的。Envoy 的 xDS 订阅代码正在重构(本书以 commit `df2c77d` 为准),有两套并行实现:legacy 的在 `source/extensions/config_subscription/grpc/` 顶层,新的在 `source/extensions/config_subscription/grpc/xds_mux/` 子目录。我们看新的(更清晰)。

### 一个统一的"带 ACK 的请求"构造器

xDS 订阅的"构造下一个要发的 `DiscoveryRequest`"逻辑,分两步:先构造一个**不带 ACK 信息的骨架**(填 `type_url`、`resource_names`、`version_info`),再**叠上 ACK 信息**(`response_nonce` + 可选 `error_detail`)。这第二步在一个共用的模板方法里,见 [`xds_mux/subscription_state.h`](../envoy/source/extensions/config_subscription/grpc/xds_mux/subscription_state.h#L98-L107):

```cpp
// The WithAck version first calls the ack-less version, then adds in the passed-in ack.
std::unique_ptr<RQ> getNextRequestWithAck(const UpdateAck& ack) {
  auto request = getNextRequestInternal();          // 先构造骨架(type_url/resource_names/version_info)
  request->set_response_nonce(ack.nonce_);          // 叠上 nonce
  ENVOY_LOG(debug, "ACK for {} will have nonce {}", typeUrl(), ack.nonce_);
  if (ack.error_detail_.code() != Grpc::Status::WellKnownGrpcStatus::Ok) {
    // Don't needlessly make the field present-but-empty if status is ok.
    request->mutable_error_detail()->CopyFrom(ack.error_detail_);   // NACK 才填 error_detail
  }
  return request;
}
```

这就是 ACK/NACK 的分叉点:**`ack.error_detail_` 的 code 是不是 `Ok`**——是 `Ok`(正常)就不填 `error_detail`(这是 ACK),不是 `Ok`(失败)就把 `error_detail` 拷进去(这是 NACK)。无论 ACK 还是 NACK,`response_nonce` 都带上。`version_info` 是在骨架 `getNextRequestInternal()` 里填的(成功时填新版本,失败时填旧版本——下面看)。

### SotW 怎么填 version_info:记住"上一次成功的版本"

`getNextRequestInternal()` 在 SotW 实现里( [`xds_mux/sotw_subscription_state.cc`](../envoy/source/extensions/config_subscription/grpc/xds_mux/sotw_subscription_state.cc#L158-L175) ):

```cpp
std::unique_ptr<envoy::service::discovery::v3::DiscoveryRequest>
SotwSubscriptionState::getNextRequestInternal() {
  auto request = std::make_unique<envoy::service::discovery::v3::DiscoveryRequest>();
  request->set_type_url(typeUrl());
  std::copy(names_tracked_.begin(), names_tracked_.end(),
            Protobuf::RepeatedFieldBackInserter(request->mutable_resource_names()));
  if (last_good_version_info_.has_value()) {
    request->set_version_info(last_good_version_info_.value());   // 关键:填"上一次成功的版本"
  }
  // Default response_nonce to the last known good one. If we are being called by
  // getNextRequestWithAck(), this value will be overwritten.
  if (last_good_nonce_.has_value()) {
    request->set_response_nonce(last_good_nonce_.value());
  }
  update_pending_ = false;
  return request;
}
```

核心是那个 `last_good_version_info_`——**"上一次成功的版本"**。Envoy 不记"收到的最新版本",只记"成功应用的最新版本"。这保证了无论发多少次失败响应,`version_info` 始终反映数据面**真实生效**的状态:

- 应用 v2 成功 → `last_good_version_info_` 更新成 v2 → 下个请求 `version_info = v2`(ACK)
- 应用 v3 失败 → `last_good_version_info_` **不更新**,还是 v2 → 下个请求 `version_info = v2`(NACK,但版本字段如实反映"我还停在 v2")
- `getNextRequestWithAck` 再把失败的 `error_detail` 叠上,控制面一看:version=v2 + error_detail 有内容 → "这个 Envoy NACK 了 v3,还停在 v2"。

> **技巧点睛(`last_good_version_info_` 而不是 `last_version_info_`)**:这个命名里的 **"good"** 是精髓。它不是"最后收到的版本",而是"最后**成功**的版本"。失败时绝不更新它。这一个命名选择,就保证了 ACK/NACK 的语义正确——数据面报告的永远是"我真实生效的版本",而不是"我最后看到的版本"。如果记成 `last_version_info_`(失败也更新),NACK 时 version 就会变成"v3"——控制面会误以为"这个 Envoy 在 v3",但实际它在 v2,**幻觉**。**"good" 这个字,是 xDS 一致性的命门。**

### NACK 的 error_detail 怎么填:legacy 实现里的现场

我们再看 legacy 实现(还在用)里,处理一个 `DiscoveryResponse` 时的 ACK/NACK 分叉——它在 [`grpc_mux_impl.cc`](../envoy/source/extensions/config_subscription/grpc/grpc_mux_impl.cc) 的 `onDiscoveryResponse` 方法里。成功路径(简化):

```cpp
// 成功应用:更新 version_info 和 nonce(行 391/398/459,简化示意,非源码原文)
api_state.request_.set_response_nonce(message->nonce());
api_state.request_.set_version_info(message->version_info());   // ACK:version 推进
```

失败路径(CATCH 块, [`行 444-457`](../envoy/source/extensions/config_subscription/grpc/grpc_mux_impl.cc#L444-L457) ):

```cpp
CATCH(const EnvoyException& e, {
  for (auto watch : api_state.watches_) {
    watch->callbacks_.onConfigUpdateFailed(
        Envoy::Config::ConfigUpdateFailureReason::UpdateRejected, &e);
  }
  ::google::rpc::Status* error_detail = api_state.request_.mutable_error_detail();
  error_detail->set_code(Grpc::Status::WellKnownGrpcStatus::Internal);
  error_detail->set_message(Config::Utility::truncateGrpcStatusMessage(e.what()));
  // ...注意:这里没有 set_version_info!version_info 保持上一次成功的值
});
api_state.previously_fetched_data_ = true;
api_state.request_.set_response_nonce(message->nonce());   // 无论 ACK/NACK,nonce 都更新
queueDiscoveryRequest(type_url);
```

注意失败路径里**没有调用 `set_version_info`**——这就是 NACK 的实现:`version_info` 保持上一次成功的值(旧版本),只更新 `response_nonce` 和 `error_detail`。成功路径才会推进 `version_info`。**ACK 还是 NACK,就看你更不更新 `version_info` + 填不填 `error_detail`。**

### ACK/NACK 的完整时序(用 mermaid 把握手画清楚)

文字描述容易绕,我们用一张 mermaid 时序图把"正常 ACK → 再来一个失败的 NACK → 控制面据此决策"的完整握手画出来:

```mermaid
sequenceDiagram
    participant CP as 控制面 (Istio)
    participant EN as Envoy (数据面)
    Note over EN: 当前生效版本 = v1 (last_good_version_info_ = "v1")
    CP->>EN: DiscoveryResponse(version_info="v2", nonce="A", resources=[新 cluster 列表])
    Note over EN: 解析 + 校验 + 应用到 ClusterManager ... 成功
    Note over EN: last_good_version_info_ 推进到 "v2"
    EN->>CP: DiscoveryRequest(version_info="v2", response_nonce="A")  ← ACK
    Note over CP: 收到:这个 Envoy 已生效在 v2 ✓
    CP->>EN: DiscoveryResponse(version_info="v3", nonce="B", resources=[新 route, 正则非法])
    Note over EN: 解析 ... 抛 EnvoyException(route 正则非法)
    Note over EN: onConfigUpdateFailed;last_good_version_info_ 不变(还是 v2!)
    EN->>CP: DiscoveryRequest(version_info="v2", response_nonce="B",<br/>error_detail={code:Internal, message:"invalid regex"})
    Note over CP: 收到:error_detail 非空 → NACK;version 还是 v2 → 这个 Envoy 仍在 v2 ✗
    Note over CP: 决策:记日志/告警;修配置后重发 v4
```

这张图把前面讲的三个不变量都体现出来了:**(1)** ACK 后 `last_good_version_info_` 推进到新版本;**(2)** NACK 后 `last_good_version_info_` **不动**,version 字段如实反映"我还停在 v2";**(3)** NACK 带 `error_detail`,控制面据此知道失败原因。控制面在任何时刻都能回答"这个 Envoy 到底生效在哪个版本"——这就是可观测性。

### NACK 之后会怎样:控制面不会"卡住"

一个常见的疑问:NACK 之后呢?控制面会因为一个数据面 NACK 就再也不发新配置了吗?不会。xDS 的设计是:**NACK 是"针对这一次响应的反馈",不影响控制面继续推后续配置**。控制面收到 NACK 后,可以:

1. **记日志、告警**(运维该知道哪个 Envoy 拒绝了配置、为什么)。
2. **修复配置后推新版本**(比如 v4,改正了那个非法正则)——数据面如果应用 v4 成功,`last_good_version_info_` 直接跳到 v4(中间的 v3 被跳过,因为没成功)。
3. **也可以不修,继续推 v5**——只要 v5 数据面能应用成功,`last_good_version_info_` 就跳到 v5。**失败的版本会被"跳过",不会卡住整个流。**

这背后的设计哲学是:**配置下发是"尽力而为 + 显式确认"的流**,不是"事务性的全有全无"。某个版本失败了,数据面停留在上一个成功的版本上继续服务(旧配置仍然能处理流量),控制面有时间去修。这比"一个失败就整个系统卡死"健壮得多。

> **对照 Nginx 的 reload 失败**:Nginx reload 如果新配置非法,会**拒绝 reload**(老 worker 继续跑老配置),运维得改了再试——这其实和 NACK 的语义一样(失败就保持旧配置)。区别在于:Nginx 是本地文件 + 手动 reload,失败反馈靠日志;xDS 是远程推送 + 协议级 ACK/NACK,失败反馈在协议里,控制面能**集中看到所有数据面的状态**。**xDS 把"配置生效确认"从"运维看日志"提升到了"控制面全局可观测"。**

---

## 六、DecodedResource:资源解出来后长什么样

控制面推下来的 `resources` 是一堆 `google.protobuf.Any`,Envoy 收到后要解包成具体的 proto(Listener、Cluster...)。这一层抽象,是 [`DecodedResource`](../envoy/envoy/config/subscription.h#L32-L68) 接口:

```cpp
class DecodedResource {
public:
  virtual const std::string& name() const PURE;        // 资源名字(Listener.name / ClusterLoadAssignment.cluster_name...)
  virtual const std::vector<std::string>& aliases() const PURE;
  virtual const std::string& version() const PURE;     // 这个资源的版本(用于 Delta)
  virtual const Protobuf::Message& resource() const PURE;  // 解出来的 proto 消息
  virtual bool hasResource() const PURE;
  virtual absl::optional<std::chrono::milliseconds> ttl() const PURE;  // TTL(见下)
  // ...
};
```

每个资源解出来,都有 `name`、`version`、`resource` 三个核心字段。这里有个**和 SotW 不一样的地方**:SotW 里整个响应一个 `version_info`(所有资源共享),而 [`Resource` 包装消息](../envoy/api/envoy/service/discovery/v3/discovery.proto#L386-L440)(Delta xDS 用)给**每个资源单独**带 `version` 字段——这就是 Delta 能做"per-resource version"的基础。`DecodedResource::version()` 在 SotW 时填响应的 `version_info`,在 Delta 时填资源自己的 `version`。**同一个抽象,适配两种传输模式。**

### 怎么从 Any 解出资源 + 提取名字:又一个模板技巧

解包 + 提名字的逻辑,在一个模板类 [`OpaqueResourceDecoderImpl<T>`](../envoy/source/common/config/opaque_resource_decoder_impl.h#L10-L34) 里:

```cpp
template <typename Current> class OpaqueResourceDecoderImpl : public Config::OpaqueResourceDecoder {
public:
  OpaqueResourceDecoderImpl(ProtobufMessage::ValidationVisitor& validation_visitor,
                            absl::string_view name_field)
      : validation_visitor_(validation_visitor), name_field_(name_field) {}

  ProtobufTypes::MessagePtr decodeResource(const Protobuf::Any& resource) override {
    auto typed_message = std::make_unique<Current>();   // Current = Listener / Cluster / ...
    if (!resource.type_url().empty()) {
      MessageUtil::anyConvertAndValidate<Current>(resource, *typed_message, validation_visitor_);
    }
    return typed_message;
  }

  std::string resourceName(const Protobuf::Message& resource) override {
    return MessageUtil::getStringField(resource, name_field_);   // 按字段名取
  }
  // ...
};
```

这里有两个设计点:

1. **`Current` 模板参数**:解包的目标类型(Listener?Cluster?RouteConfiguration?)。`decodeResource` 把 `Any` 转成 `Current` 类型,带校验。一个模板类,五种资源通用。
2. **`name_field_` 字符串**:每个资源的"名字"字段名不一样——Listener.name、RouteConfiguration.name、Cluster.name 都是 `name`,但 **ClusterLoadAssignment 的名字字段叫 `cluster_name`**(见 [`endpoint.proto:126`](../envoy/api/envoy/config/endpoint/v3/endpoint.proto#L126) 的 `string cluster_name = 1`)。所以构造 decoder 时要把字段名传进去:Listener 传 `"name"`,EDS 传 `"cluster_name"`。`MessageUtil::getStringField` 用反射按字段名取值。

各处的实例化(都是这个模式):

| 资源 | 实例化处 | name_field |
|------|---------|-----------|
| LDS (Listener) | `lds_api.h:52` 的 `ResourceTypeHelper<...Listener>` | `"name"` |
| RDS (RouteConfiguration) | [`rds_impl.cc:204-205`](../envoy/source/common/router/rds_impl.cc#L204-L205) | `"name"` |
| CDS (Cluster) | [`cds_api_impl.cc:32`](../envoy/source/common/upstream/cds_api_impl.cc#L32) | `"name"` |
| EDS (ClusterLoadAssignment) | (按 `cluster_name`) | `"cluster_name"` |
| SDS (Secret) | [`sds_api.cc:27`](../envoy/source/common/secret/sds_api.cc#L27) | `"name"` |

> **技巧点睛(`name_field` 参数化)**:五种资源的"名字"字段不统一(四个叫 `name`,EDS 叫 `cluster_name`),这本来是个麻烦——要么每个资源写一个 decoder(代码重复),要么强行改 proto 让所有资源都有 `name` 字段(破坏既有 proto)。Envoy 的选择是**把字段名当参数传进去**,用反射 `getStringField(resource, name_field_)` 取值。一个模板类 + 一个字符串参数,五种资源全搞定。**这是"用反射消化协议不一致"的典型手法**——不强迫协议统一,而在客户端用参数化抹平差异。代价是失去编译期字段检查(`name_field_` 拼错运行时才发现),但这是个低频路径,可接受。

### SDS 的特殊之处:跳过 backing cluster 检查

SDS 有个和其它四类不一样的地方。其它四类(LDS/RDS/CDS/EDS)走 gRPC 订阅时,Envoy 会检查"这个 xDS server 对应的 cluster 存不存在"(你总得先有个 cluster 连得上控制面吧)。但 SDS 是拿**证书**的,而证书可能在 Envoy **启动早期**就需要(比如 listener 的 TLS 配置引用了 SDS 提供的证书),那时候 cluster 还没完全初始化好。所以 SDS 被特殊豁免,见 [`subscription_factory_impl.cc`](../envoy/source/common/config/subscription_factory_impl.cc#L65-L67):

```cpp
if (type_url !=
    Envoy::Config::getTypeUrl<envoy::extensions::transport_sockets::tls::v3::Secret>()) {
  RETURN_IF_NOT_OK(Utility::checkApiConfigSourceSubscriptionBackingCluster(
      cm_.primaryClusters(), api_config_source));   // 非 SDS 才检查 backing cluster
}
```

这是个**鸡生蛋问题的解法**:SDS 提供 TLS 证书 → cluster 用 TLS 证书建连 → SDS 自己也是通过一个 cluster 去拿的。如果"连 SDS 都得等 cluster 完全 ready",就死锁了(证书没拿到,cluster 连不上;cluster 连不上,证书拿不到)。**SDS 跳过检查,打破循环依赖。** 这是 SDS 区别于其它四类的一个不起眼但关键的细节。

---

## 七、nonce:防什么?防的是"过期的请求"

回到 proto——`DiscoveryResponse` 有 `nonce`,`DiscoveryRequest` 有 `response_nonce`(ACK 时带回来)。这个 nonce 是干嘛的?proto 注释说得很直白( [`discovery.proto` 的 nonce 字段](../envoy/api/envoy/service/discovery/v3/discovery.proto) ):

> The nonce allows the management server to ignore any further `DiscoveryRequest`s for the previous version until a `DiscoveryRequest` bearing the nonce.

翻译过来:**nonce 让控制面能忽略"针对旧版本的迟到请求",直到收到带新 nonce 的请求为止。**

什么场景?想象这个时序:

```
   控制面                                     数据面
     │                                          │
     │  Response(v2, nonce=A)                   │
     │ ──────────────────────────────────────▶  │  数据面在处理 v2...
     │                                          │
     │  Response(v3, nonce=B)  (v2 还没 ACK)    │
     │ ──────────────────────────────────────▶  │  v2 处理完,回 ACK
     │                                          │
     │  Request(version=v2, nonce=A)  ← 迟到的 ACK!
     │ ◀──────────────────────────────────────  │
     │                                          │
     │  (控制面:这是针对 v2(nonce A)的迟到确认,  │
     │   但我已经发了 v3(nonce B),忽略它)        │
```

控制面发了 v2,还没等数据面 ACK 就又发了 v3(可能配置又变了)。数据面这时才把针对 v2 的 ACK 发回来(`nonce=A`)。控制面一看 nonce=A,但当前最新是 nonce=B——**这个 ACK 是针对旧版本的,过时了,忽略**。控制面只认"带当前最新 nonce 的请求"。

> **不这样会怎样**(没有 nonce):控制面收到的每个 `DiscoveryRequest` 都带 `version_info`,看起来都是"我生效在 vN"。如果控制面分不清"这个请求是针对哪个响应的回执",就可能被迟到的旧 ACK 误导——以为数据面在 v2,其实它已经在处理 v3 了。nonce 就是给每个响应一个唯一标识,让 ACK/NACK 能精确对应到"我是回的哪一次响应"。**它是版本握手的"序列号",防止乱序/迟到请求污染状态。**

注意 proto 还说了:**nonce 是 gRPC streaming 才需要,REST/file-based xDS 不需要**——因为 REST 是一问一答,没有"多个响应排队"的问题。这又印证了 nonce 是为"双向流上的多次异步握手"准备的。

### file / REST / gRPC:xDS 的三种后端

讲到这里,顺带交代 xDS 的三种"传输后端"。前面一直说 xDS 走 gRPC 双向流,但 Envoy 其实支持三种从控制面拿配置的方式,在 [`subscription_factory_impl.cc`](../envoy/source/common/config/subscription_factory_impl.cc#L72-L99) 里按 `ApiConfigSource.api_type` 分发:

| api_type | 实现 | 特点 |
|----------|------|------|
| `GRPC` | SotW gRPC 流 | 双向流,全量推送,带 ACK/NACK(本章主讲) |
| `DELTA_GRPC` | Delta gRPC 流 | 双向流,增量推送(P5-17 主讲) |
| `REST` | HTTP 长轮询 | 一问一答,无 nonce,简单但不实时 |
| (文件) | filesystem watch | 本地文件变化触发重载,无网络 |

`filesystem` 模式不是 xDS 协议,是 Envoy 直接 watch 本地配置文件——这是"静态配置 + 不停机重载"的中间形态,介于 Nginx 的 reload 和真正的 xDS 之间。`REST` 模式是 Envoy 定期 HTTP POST 一个 `DiscoveryRequest` 到控制面 URL,控制面 HTTP 响应一个 `DiscoveryResponse`——简单但延迟高(靠轮询间隔),且没有流的概念。**生产环境几乎都用 gRPC 流**(低延迟、服务端可主动推),REST/file 多用于开发调试。

`ADS`(`kAds` 分支)是另一种维度:它不是第四种传输,而是"用一条 gRPC 流复用所有资源类型",底层还是 SotW 或 Delta。ADS 是 P5-17 的重头。

> **钉死这件事**:xDS 的"协议"(DiscoveryRequest/Response + 版本协商)和"传输"(gRPC/REST/file/ADS)是解耦的——同一套协议,可以跑在不同传输上。生产用 gRPC 流(低延迟、可推、有 ACK),调试用 file/REST(简单)。这个解耦让 xDS 既能用于云原生的实时服务发现,也能用于最简单的单机配置管理。

---

## 八、技巧精解:两个最硬核的洞察

本章正文讲完了 xDS 的协议骨架、五类资源、版本协商。这里把两个最硬核的洞察单独拆透——它们是理解 xDS 设计哲学的钥匙。

### 技巧一:resource version 协商为什么"sound"——三个不变量

我们已经讲了 ACK/NACK 的机制。这里把它上升到"为什么这套设计是 sound(正确)的"——它守住了三个不变量:

**不变量 1:`version_info` 永远等于"数据面真实生效的版本",而非"最后看到的版本"。**

源码里那个 `last_good_version_info_`(注意 "good")是命门。应用失败时,**绝不**更新它。这一个选择,保证了数据面报告给控制面的版本是**真相**——不是幻觉。如果记成"最后看到的版本",NACK 时 version 会变成失败的版本,控制面会以为数据面切过去了——一致性就破了。

**不变量 2:每个 `DiscoveryResponse` 都有一个唯一 nonce,且必须被对应 nonce 的 `DiscoveryRequest` 确认。**

这保证了"每一次下发都有一次确认",不会丢更新。控制面发了一个响应,没收到带这个 nonce 的回执,就知道"这次下发还没被处理完"——要么重发,要么等。流断了重连,数据面发的第一个请求带 `version_info`(真实生效版本)+ `response_nonce`(上次成功的 nonce),控制面据此重建状态。**nonce 让"重连"不是"状态归零",而是"状态对齐"。**

**不变量 3:`error_detail` 的有无,是 ACK/NACK 的唯一分叉,且 NACK 不推进 version。**

一个字段决定语义,极其紧凑。NACK 时 `version_info` 保持旧值 + `error_detail` 填原因——这两个动作一起,把"我失败了,我还停在旧版,原因是 X"完整传达。如果 NACK 也推进 version(像 ACK 那样),控制面会以为数据面切到新版了,但实际它还在旧版——幻觉。**"NACK 不动 version"这一条,和不变量 1 一脉相承。**

> **反面对比**(朴素的单向推送):没有 version、没有 nonce、没有 ACK/NACK——控制面推完就忘,数据面收没收到、应用没应用成功、为啥失败,控制面一概不知。故障摘除"以为摘了其实没摘"、灰度发布"以为切了其实没切"、配置错误"静默丢弃无任何反馈"。这种系统在生产环境根本不可用。**ACK/NACK 这套"看起来啰嗦"的握手,是动态配置从"演示"走向"生产"的分水岭。**

这三个不变量合起来,让 xDS 成为一种**可证明一致**的配置下发协议——它不依赖"网络可靠"的假设,而是用协议本身(version + nonce + ACK/NACK)在不可靠的网络上搭建出可靠的配置同步。这和《TiKV》里 Raft 用 term + log index 在不可靠网络上搭建出共识,是同一种工程哲学。

### 技巧二:五类资源的"依赖链 + 按需订阅"——为什么 EDS 是核心

五类 xDS 的依赖关系是 LDS → RDS → CDS → EDS(横向 SDS)。这个依赖链不是装饰,它决定了**整个 xDS 系统的变更效率**:

```
   变更频率:    LDS (极低) ── RDS (低) ── CDS (低) ── EDS (高!) ──→ 时间
   依赖方向:    LDS ─引用─▶ RDS ─引用─▶ CDS ─引用─▶ EDS
   热更新代价:  重(drain)    中(换表)    中(建连)    轻(改 vector)
```

**EDS 是整个系统变更最频繁的部分**(微服务后端天天扩缩容),而它恰好在依赖链**最底层**、热更新**最轻**(只是改 cluster 里的 endpoint vector,不动 listener、不动 route、不动 cluster 定义)。这意味着:后端扩容一个 Pod,只需要推一份新的 `ClusterLoadAssignment`(EDS),几百字节,秒级生效,不动其它任何配置。**这个解耦,是 xDS 能扛住微服务高频变更的根。**

如果配置是一个大包(前面"墙"里讲的),后端扩容一个实例得重发整个配置,几百 KB 到几 MB,数据面全量重解析重应用——和 Nginx reload 没本质区别。xDS 把最频繁的变更(EDS)切出来,做成轻量级的独立订阅,这就是"分层 + 按需"的威力。

而且,Envoy 是**按引用链按需订阅**的:不是一开始就订阅所有 EDS,而是 LDS 发来 listener → listener 里引用了某个 route_config → 订阅这个 RDS → RDS 的 route 引用了某个 cluster → 订阅这个 CDS → cluster 是 EDS 类型 → 才订阅这个 cluster 的 EDS。**没有引用的,不订阅。** 这避免了"订阅一堆用不上的资源",在几百个集群的大规模场景下省带宽、省内存、省 CPU。proto 注释里说得很清楚:LDS/CDS 的 `resource_names` 可以为空(返回所有),EDS/RDS 的是从上层响应**推导**出来的。

> **钉死这件事**:五类 xDS 的依赖链 LDS→RDS→CDS→EDS,不是文档里的箭头,而是**变更效率的工程设计**:最频繁的变更(EDS)在最底层、最轻量、最独立;最重的变更(LDS)在最顶层、最低频、热更新代价最大(drain)。**这个"按变更频率和代价分层"的设计,是 xDS 能高效动态化的灵魂。**

---

## 八点五、xDS 的历史与演进:从 v2 到 v3,从裸字符串到 xdstp://

理解一个协议,看它的演进史往往比看它的现状更透。xDS 不是一天设计成今天这样的,它经过了几次大演进,每一步都解决上一步的痛点。这里把脉络理一遍,帮你看老博客时不被带偏。

### v2 时代:Envoy API v2,基于 proto2

xDS 最早是 **Envoy API v2**(2017 年左右),proto 包名是 `envoy.api.v2`。你可能注意到,`DiscoveryRequest` 的 proto 注释里有这么一句:

```protobuf
option (udpa.annotations.versioning).previous_message_type = "envoy.api.v2.DiscoveryRequest";
```

这个 `previous_message_type` 标注就是版本迁移的痕迹——v3 的 `DiscoveryRequest` 显式声明"我对应 v2 的同名消息"。这是 UDPA(Universal Data Plane API,Envoy 牵头的通用数据面 API 标准化)的版本化机制,**新老 proto 可以共存、平滑迁移**。老博客里看到的 `envoy.api.v2.Cluster`、`type.googleapis.com/envoy.api.v2.ClusterLoadAssignment` 是 v2 的 type_url——本书以 v3 为准(`envoy.config.cluster.v3.Cluster`),v2 已废弃。

### v3 时代:现在的标准

现在的 **Envoy API v3**(2020 年起稳定),proto 包名 `envoy.config.{listener,route,cluster,endpoint,...}.v3`,type_url 前缀 `type.googleapis.com/envoy.config.xxx.v3.Yyy`。本书全部基于 v3(commit `df2c77d`,1.39.0-dev)。v3 相比 v2 不只是 proto 包名变,还引入了 `Resource` 包装消息(支持 per-resource version,为 Delta 铺路)、TTL(资源可过期)等新特性。

### xdstp://:资源命名的 URL 化

最新的演进里,xDS 在搞**资源命名的 URL 化**——用 `xdstp://` 这种 URN/URL 来命名资源(类似 `xdstp://envoy.config.listener.v3.Listener/my-listener`),而不是裸字符串名字。这为了支持:

- **多 authority(控制面分权)**:不同资源可以来自不同的控制面(authority),比如 listener 来自一个控制面,secret 来自另一个。`xdstp://` 的 authority 字段区分。
- **动态参数(dynamic parameters)**:同一个资源名可以有多个"变体",靠 `dynamic_parameters` 区分。这是 [`ResourceLocator`](../envoy/api/envoy/service/discovery/v3/discovery.proto#L20-L33) 和 `ResourceName` 引入的新机制(还在逐步落地,源码里有 `[#not-implemented-hide:]` 标注的字段属于这批)。

源码里这套 URN 编解码在 [`source/common/config/xds_resource.h`](../envoy/source/common/config/xds_resource.h) 的 `XdsResourceIdentifier` 类(`encodeUrn`/`decodeUrn`/`encodeUrl`/`decodeUrl`)。**这是 xDS 的未来方向,但还没完全普及**——大部分生产部署还在用裸字符串名字。本书讲的是当前的"主流形态"(裸字符串 + type_url),`xdstp://` 作为演进方向点到。

> **本书态度**:以 v3 + 裸字符串命名为准(当前主流),v2 标注为废弃,`xdstp://` 标注为演进中。这样你看老博客(讲 v2)和新文档(讲 xdstp)都不会迷路。

### 一句话对照:gRPC 复用 xDS

最后再强调一次承接关系:**xDS 是 Envoy 定义的协议,gRPC 把它拿过去用了**。gRPC 客户端内置 xDS client(《gRPC》P6-22),可以直接对接 Envoy/Istio 控制面,不经过 Envoy sidecar 自己做负载均衡和路由。这意味着 xDS 不只是 "Envoy 的私有协议",而是**云原生生态的通用配置下发标准**——学透 xDS,等于学透 service mesh 控制面与数据面之间的"通用语言"。本书讲 Envoy 作为消费端,《gRPC》讲 gRPC 作为消费端,两边对照着看,能发现 xDS 协议在不同语言、不同实现里的共性(C++ 的 Envoy 和 Go 的 gRPC xDS client,解的是同一套 proto)。

---

## 九、SotW vs Delta:协议的演进方向(为 P5-17 铺路)

讲到这里,我们已经覆盖了 xDS 协议的核心。但还有一个演进维度必须交代——xDS 协议本身在演化,从 **SotW(State of the World)** 到 **Delta xDS**。本章主要讲的是 SotW(经典模式),Delta 是 P5-17 的主角,这里先点破区别。

### SotW:全量推送

**SotW(State of the World,世界快照)** 就是前面讲的那套:每次 `DiscoveryResponse` 把**该类型的所有资源**全发一遍。你订阅了 100 个 cluster 的 EDS,后端扩容了一个,控制面发 `ClusterLoadAssignment` 时,**100 个全发**(包括没变的 99 个)。

```
   SotW: 哪怕只变了一个 endpoint,整批全发
   控制面 ──Response(v2, [cla_0, cla_1, ..., cla_99 全部])──▶ 数据面
```

简单、直观、状态自洽(每次都是完整快照,不依赖历史)。但在大规模场景(几百上千个资源)下,带宽和 CPU 浪费严重——99% 的内容没变,却要序列化、传输、解析、比对。

### Delta xDS:只发变更

**Delta xDS** 改成**只发变更**:用 `DeltaDiscoveryRequest` / `DeltaDiscoveryResponse`,增量同步。变了的资源放在 `resources` 里(每个带自己的 `version`),删了的放在 `removed_resources` 里。后端扩容一个 endpoint,只发那一个变了的 `ClusterLoadAssignment`。

```
   Delta: 只发变更
   控制面 ──DeltaResponse(system_version, resources=[cla_3 变了的], removed=[])──▶ 数据面
```

Delta 的 proto([`discovery.proto` 的 DeltaDiscoveryRequest/Response`](../envoy/api/envoy/service/discovery/v3/discovery.proto))和 SotW 的几个关键区别:

1. **per-resource version**:Delta 里每个资源有自己的 `version`([`Resource` 消息的 version 字段](../envoy/api/envoy/service/discovery/v3/discovery.proto#L416)),不再是整批一个 `version_info`。这样能精确到"cla_3 是 v3,cla_7 还是 v1"。
2. **subscribe/unsubscribe 增量**:Delta 用 `resource_names_subscribe` / `resource_names_unsubscribe` 增量加减订阅,不像 SotW 每次发完整 `resource_names` 列表。源码在 [`xds_mux/delta_subscription_state.cc`](../envoy/source/extensions/config_subscription/grpc/xds_mux/delta_subscription_state.cc#L289-L292):

   ```cpp
   std::copy(names_added_.begin(), names_added_.end(),
             Protobuf::RepeatedFieldBackInserter(request->mutable_resource_names_subscribe()));
   std::copy(names_removed_.begin(), names_removed_.end(),
             Protobuf::RepeatedFieldBackInserter(request->mutable_resource_names_unsubscribe()));
   ```

3. **`initial_resource_versions`**:Delta 流重连时,数据面把"我已有的每个资源 + 它的版本"告诉控制面,控制面只补差。这让重连不再是"全量重发"。

> **不这样会怎样**(只用 SotW):几百个 cluster、每个几百个 endpoint 的大规模服务网格,后端天天变,SotW 全量推送会让 xDS 流量吃掉可观带宽(实测大规模 Istio 里 EDS SotW 响应能到几 MB),数据面反复全量解析也吃 CPU。Delta 只发变更,把这个开销降到最低。**Delta 是 xDS 为大规模场景做的演进**,新部署的 Istio 默认走 Delta。老资料如果只讲 SotW,是过时的——本书两种都讲,标清演进动机。

> **本书态度**:SotW 是基础,Delta 是演进,ADS(聚合)是另一维度的优化。下一章 P5-17 我们把"传输模式"彻底拆开:SotW、Delta、ADS 三种怎么选、ADS 为什么能保证多类型下发顺序。

---

## 九点五、横向对照:xDS 和其它"配置/发现"系统比,凭什么

讲透了 xDS 本身,我们把它和几个"看起来类似"的系统横向对照一下——这能让你看清 xDS 在设计空间里的位置,以及它每个选择的理由。

### 对照一:xDS vs Kubernetes informer(都是 watch + 版本)

Kubernetes 的 controller 用 **informer** 机制 watch API server:informer 维护一个本地 cache,API server 有变更就推过来(带 `resourceVersion`),informer 更新 cache。这套和 xDS 的"订阅 + 版本"惊人地像:

| 维度 | Kubernetes informer | xDS |
|------|--------------------|----|
| 传输 | HTTP/2 长连接 watch(分块传输) | gRPC 双向流 |
| 版本 | `resourceVersion`(per-resource) | SotW 整批 `version_info` / Delta per-resource `version` |
| 确认 | 客户端不显式 ACK(靠 watch 的持续连接) | **显式 ACK/NACK**(xDS 的招牌) |
| 失败处理 | 连接断了重连,重新 list | NACK 带 error_detail,version 保持旧值 |

关键差异在**确认机制**:Kubernetes informer 假设"只要 watch 连接活着,我就收到了所有变更"(靠 HTTP/2 的可靠传输 + resourceVersion 顺序),**不做应用层 ACK**——因为 informer 的客户端(controller)只是更新内存 cache,不"应用"配置(真正应用是 controller 的 reconcile 循环,异步的)。而 xDS 的客户端(Envoy)收到配置要**立即应用到 filter chain / cluster manager**,应用可能失败(配置非法),所以**必须显式 ACK/NACK**,让控制面知道"应用成功没"。**xDS 的 ACK/NACK 是为"配置要立即应用且可能失败"这个场景设计的**,比 informer 多了一层应用确认。

### 对照二:xDS vs ZooKeeper/etcd watch(都是推送 + 版本)

etcd(《etcd》那本讲过)和 ZooKeeper 也做"配置/服务发现"——客户端 watch 一个 key,变化时推送。etcd 用 **MVCC + revision**(全局单调递增),客户端记住自己 watch 到哪个 revision,断了重连从那个 revision 继续。这和 xDS 的 version 协商也是一脉相承的思路:

- etcd:全局 revision,客户端带 revision 续 watch。
- xDS:per-type version_info(SotW)或 per-resource version(Delta),客户端带 version ACK/NACK。

差异:**etcd 的 watch 是"裸 KV 变更流",不做 ACK**(etcd 假设 gRPC 流可靠);**xDS 多了 ACK/NACK,因为配置应用可能失败**。而且 etcd 推的是 KV(无类型),xDS 推的是**强类型 proto + type_url**——因为 Envoy 要把配置结构化地塞进各种 manager,不是存个字符串就行。**xDS 是"为可编程数据面量身定做的、带类型 + 带应用确认的发现协议"**,比通用 KV watch 更贴近"配置下发"这个场景。

### 对照三:xDS vs Nginx 的"动态 upstream"(为什么 xDS 是降维打击)

Nginx 也有动态 upstream 的尝试——比如 `nginx-plus` 的 API 动态增删 upstream、或者 OpenResty 用 lua 动态改 upstream。但这些方案有几个根本短板:

1. **只动态了 endpoint(类似只有 EDS)**:listener、route、filter 都还得 reload。没有 LDS/RDS/CDS 的分层动态化。
2. **没有版本协商和 ACK**:改了就改了,改错了无反馈,控制面对数据面状态一无所知。
3. **不可编程数据面**:filter chain 是编译期固定的,没法运行时插自定义逻辑(得靠 OpenResty/lua 这种 hack)。

xDS 把"动态"做到了**所有五类资源 + 版本协商 + 可编程 filter chain**,这是降维打击。Nginx-plus 的动态 upstream,只是 xDS 的一个子集(EDS 的阉割版)。

> **钉死这件事**:xDS 不是凭空发明的,它站在 Kubernetes informer、etcd watch 这些"推送 + 版本"机制的基础上,但针对"可编程数据面的配置下发"这个场景做了三个关键增强:**(1)** 强类型 proto + type_url(配置是结构化的);**(2)** 显式 ACK/NACK(配置要应用、可能失败);**(3)** 五类资源按语义边界分层(独立下发、独立热更新)。这三条,让 xDS 成为 service mesh 时代控制面与数据面的**通用契约**。

---

## 十、章末小结

### 回扣主线

这一章是**控制面(招牌)的开篇**。前面 15 章我们讲完了数据面——一条流量怎么被 filter chain 处理。但那条 filter chain 上所有的配置(listener、filter 链、route_config、cluster、endpoint、证书),不是写死的,而是**控制面通过 xDS 动态下发**的。本章回答了三个根本问题:

1. **xDS 是什么?**——一套跑在 gRPC 双向流上的、带版本握手的、分类型的配置下发契约。
2. **有几类?各管什么?**——五类(LDS/RDS/CDS/EDS/SDS),按资源语义边界切,独立下发、独立版本、独立热更新,有 LDS→RDS→CDS→EDS 的依赖链。
3. **配置怎么保证一致?**——resource version 协商 + ACK/NACK:每份配置带 version,应用成功回 ACK(带新版本),失败回 NACK(保持旧版本 + error_detail),控制面据此知道每个数据面的真实状态。

回到全书二分法:本章服务**控制面**这一面。它把"配置从哪来、怎么可靠下发"这个问题立了起来——这是 Envoy 区别于 Nginx("配置写死 + reload")的根本所在。

### 五个为什么

1. **为什么 xDS 要把配置切成五类独立资源?**——独立下发、独立版本、独立热更新;避免"一处变更全量重发"、避免"一处错误全包拒绝"、让高频变更(EDS)和低频变更(LDS)解耦。按语义边界切,是控制面设计的通用模式。
2. **为什么 xDS 不是单向推送而是 ACK/NACK?**——分布式系统里单向推送不可靠(网络抖不知道谁收到、配置非法不知道谁失败、重连状态丢失)。version 协商 + ACK/NACK 是"带确认的可靠下发",保证最终一致、不丢更新、可观测——生产级动态配置的底线。
3. **为什么 NACK 时 `version_info` 保持旧版本?**——因为数据面报告的必须是"真实生效的版本",不是"最后看到的版本"。失败时绝不推进 version(源码里 `last_good_version_info_` 的 "good" 是命门),否则控制面会产生"数据面切过去了"的幻觉。
4. **为什么用 `type_url` + 泛型 DiscoveryRequest/Response?**——一套 proto 消息适配五类资源,靠 `type_url` 区分;同时支持 ADS(一个流复用所有类型)。这是"泛型接口 + 类型标签"的经典设计,比"每类一个 gRPC 服务"更灵活。
5. **为什么 EDS 是 xDS 的核心?**——微服务后端频繁上下线,EDS(服务发现)是变更最频繁的;它在依赖链最底层、热更新最轻(只改 endpoint vector),能秒级感知实例上下线而不牵动上层——这是 xDS 能扛住微服务动态现实的关键。

### 想继续深入往哪钻

- **协议定义**:`api/envoy/service/discovery/v3/discovery.proto`(DiscoveryRequest/Response/Delta/Resource 全在这)、`ads.proto`(ADS 服务定义)。逐字段读 proto 注释,这是最权威的协议文档。
- **xDS 官方架构文档**:Envoy repo 的 `docs/`(尤其是 `docs/root/configuration/overview/xds_overview.rst` 类文件)有协议演进、版本协商的官方说明。
- **ACK/NACK 源码**:`source/extensions/config_subscription/grpc/xds_mux/subscription_state.h`(ACK/NACK 分叉)、`sotw_subscription_state.cc`(version 填充)、legacy 的 `grpc_mux_impl.cc:444-457`(NACK 现场)。
- **想看控制面侧**:本书只讲 Envoy 作为 xDS 消费端。想看控制面怎么实现,读 Istio 的 istiod(go-control-plane 库),它实现了 xDS 服务端。这超出本书范围。
- **想看 gRPC xDS client**:《gRPC》P6-22 讲过 gRPC 客户端内置 xDS client 对接 Envoy/Istio,那本书深入。

### 引出下一章

我们搞清楚了 xDS 协议**是什么**(五类资源 + 版本协商),但还有一个维度没拆:**xDS 到底怎么"传"**?SotW(全量)、Delta(增量)、ADS(聚合订阅)这三种传输模式,各自怎么跑?ADS 为什么能保证多类型下发的顺序(避免"RDS 引用了一个还没 CDS 推过来的 cluster")?xDS 后端除了 gRPC,还有 file、rest 怎么选?这些,是下一章 P5-17 的主角——**xDS 订阅与传输:grpc streaming / delta / ADS**。

> **下一章**:[P5-17 · xDS 订阅与传输:grpc streaming / delta / ADS](P5-17-xDS订阅与传输-grpc-streaming-delta-ADS.md)
