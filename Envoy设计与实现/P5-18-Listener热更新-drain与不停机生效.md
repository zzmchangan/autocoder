# 第 5 篇 · 第 18 章 · Listener 热更新:drain 与不停机生效

> **核心问题**:第 17 章讲清了 xDS 怎么把配置"传"过来——LDS 在一条 gRPC 双向流上把新版本 listener 推给 Envoy,带 `version_info`,Envoy 回 ACK/NACK。但 ACK 的时候,新 listener **还没真正生效**——它只是被收到了。真正的问题是:**一份新 listener 配置(filter chain 变了、新 listener 加了、TLS 证书换了),怎么在不停机、不掐断在途流量、不让客户端看到 503 的前提下,把正在线上跑的旧配置替掉?** Envoy 的答案分两条:① listener **不是一上来就 active**,先在 `warming_listeners_` 里"热身"(等 listener-local 的 init manager 把 SDS 证书、子 init 都拉起来),热身完才进 `active_listeners_`,这一步替换时旧的走 drain;② 替换的方式分**两条路径**——如果只动了 filter chain(`filterChainOnlyChange`),走 **in-place filter chain 更新**(同 listener tag、同 socket、新 filter chain,旧 filter chain 单独 drain);否则走**完整 listener 替换**(起一份新 listener,旧 listener 整体 drain)。两条路径都靠 drain 保证在途流量有序收尾。

> **读完本章你会明白**:
> 1. **warming(热身)这步为什么不能省**:新 listener 不直接 active,先要等它自己 listener-local 的 init manager 把依赖(典型是 SDS 拉证书、Redis 集群就绪等)拉起来。不等就 active,会出现"listener 已在接连接、但证书还没到 → TLS 握手失败"这种半生不熟状态。warming→active 的转换是 `onListenerWarmed`,转换那一刻才让 worker 把旧 listener drain 掉。
> 2. **两条更新路径的判别条件**:`supportUpdateFilterChain` 怎么用 `ListenerMessageUtil::filterChainOnlyChange` 判断"这次 LDS 只动了 filter chain",从而走 `inPlaceFilterChainUpdate`(原地更新,不拆 socket、不重 accept);否则走完整 listener 替换(走 warming + drainListener)。判错路径要么性能损失(全量重建)、要么正确性坏(把该重建的当原地更新)。
> 3. **filter chain 的"按代际热替换"**:旧 filter chain 不立刻销毁,而是 `startDraining()` 把自己标成 draining,新连接 `findFilterChain` 匹配到新 chain,**老连接继续绑在它 accept 那一刻匹配到的老 chain 上**(filter chain 一旦选中、整个连接生命周期不变)。这是"在途流量不受影响"的不变式落点。drain 超时兜底后,残留老连接才被 `removeFilterChain` 强制 `NoFlush` 关掉。
> 4. **drain 的两层(listener drain vs connection drain)+ 渐进式 drainClose**:listener drain(停 accept 新连接,新连接不再往这个 listener 投)是第一层;在途连接也开始 drain(HCM 给 HTTP/2 客户端发 GOAWAY、HTTP/1.1 不再接新请求并回 `Connection: close`)是第二层。`DrainManagerImpl::drainClose` 不是"立刻关",而是**渐进式**——按 `elapsed/drain_time` 概率关,让客户端有时间收到 GOAWAY 平滑切流,而不是瞬间全军覆没。这两层 + 渐进,是 Envoy "不停机生效"三件套。

> **如果一读觉得太难**:先只记住三件事——① 新 listener 收到不立刻生效,先在 `warming_listeners_` 里等它自己的子 init(证书等)完成,才进 `active_listeners_` 替换旧的;② 只动 filter chain 的 LDS 更新走"原地更新"(同 listener tag),其它走"完整替换"(新 listener 起来、旧的 drain);③ 旧 filter chain 标成 draining,新连接走新 chain、老连接继续用老 chain 直到处理完(或 drain 超时强制关),在途流量不受影响。

---

## 〇、一句话点破

> **LDS 下发的新 listener 配置,Envoy 不立刻替换线上版本:先在 `warming_listeners_` 里把它自己的子 init(SDS 证书、依赖集群)拉起来,热身完了(`onListenerWarmed`)才让 N 个 worker 各自把新 listener `addListener`,同时把旧 listener(或旧 filter chain)丢进 drain——旧 listener 停止 accept 新连接、旧 filter chain 标 draining,新连接全走新版本;老连接继续用它们 accept 那一刻绑定的老版本,经 HCM 的 GOAWAY / HTTP/1.1 的 `Connection: close` 让客户端有序迁移,drain 超时兜底强制收尾。整个过程中没有一刻"接不到新连接",也没有一个在途请求被无故掐断。**

这是结论,不是理由。本章倒过来拆:先讲为什么替换前要 warming(P5-17 只讲"收到",没讲"热身"),再拆两条更新路径(filter chain 原地更新 vs 完整 listener 替换)的判别与各自流程,然后钻到 filter chain 按"代际"热替换的机制(为什么在途连接不受影响),最后讲 drain 的两层(listener / connection)和 `DrainManagerImpl` 的渐进式 drainClose,把"不停机生效"这件事钉死。

> **承接提醒**:P2-05 第五节已经把 listener drain 的**基本机制**(停 accept → 通知在途连接 onDrain → 等超时 → removeListener)讲透了,讲清了"为什么不能直接 `close(listen_fd)`"。本章**不重复 P2-05**——P2-05 的视角是"单个 listener 的生命周期"在数据面这一侧;本章的视角是**控制面**:**LDS 怎么驱动 listener 的增删改、新旧版本怎么交接、为什么这套流程能做到不停机**。两边在 `drainListener` 这个函数上汇合,但本章的关注点是"warming / 两条更新路径 / filter chain 代际热替换 / 渐进式 drainClose"这些 P2-05 没讲的部分。

---

## 一、LDS 收到新 listener 之后:warming 这一步为什么不能省

第 17 章讲到 LDS 在 gRPC 流上把新 listener 推给 Envoy,Envoy 回 ACK。但 ACK 这一刻,新 listener **还没在端口上跑**。控制面侧的 `LdsApiImpl` 把 proto 交给 `ListenerManagerImpl::addOrUpdateListener`([`listener_manager_impl.cc:510`](../envoy/source/common/listener_manager/listener_manager_impl.cc#L510)),真正"生效"的工作从这里开始。而这一步的第一道关卡,是 **warming(热身)**。

### 新 listener 为什么不能一收就 active

朴素想法:LDS 给我一份新 listener,我直接 `bind` + `listen` + 在 worker 上跑起来不就行了?为什么不?因为 listener 不是孤立的——它带着一堆**依赖**要在生效前就绪:

- **transport socket(TLS)的证书**:listener 的 filter chain 可能配 `transport_socket: tls`,证书从哪来?可能是静态文件,也可能是 **SDS(Secret Discovery Service)动态拉**。如果是 SDS,新 listener 实例化那一刻证书**还没到**——SDS 是一次异步的 gRPC 交互,要等控制面把 Secret 推过来。如果这时候 listener 已经在接连接,客户端的 TLS 握手就会因为"没有证书"直接失败。
- **listener-local 的 init manager**:Envoy 的每个 listener 有自己的 `dynamic_init_manager_`(`ListenerImpl` 构造时建,见 [`listener_impl.cc:360`](../envoy/source/common/listener_manager/listener_impl.cc#L355-L361))。listener 的子依赖(典型是 SDS、某些 listener filter 自己的 init)注册进这个 manager,manager 全部 ready 了 listener 才算"热身完"。这是 Envoy 通用的 **init manager 模式**(承接《TiKV》批处理那本没讲的、Envoy 特有的"目标就绪前不暴露"机制)。

> **不这样会怎样(直接 active 会撞什么墙)**:假设新 listener 一收就 active、马上 `bind`+`listen`,会发生什么?① **TLS 证书还没到**:客户端 SYN 进来、TCP 三次握手完成、开始 TLS 握手,Envoy 这边 transport socket 还没证书,握手失败,客户端看到 `handshake_failure`。如果这条 listener 是给生产流量用的,这期间所有进来的请求全 503/握手失败。② **listener filter 的 init 没就绪**:比如某个 listener filter 要先跟一个外部控制面拉一份配置,如果 listener 已经在接连接、filter 还没就绪,每条新连接都被这个 filter 卡住或拒绝。③ **更糟的**:如果新 listener 起来时,旧 listener 已经被销毁(为了"腾地方"),那这个窗口期**新连接失败、老连接也没了**——彻底的停机。

warming 就是把这道风险堵住:**新 listener 先实例化、把它的子依赖(SDS 等)注册进 listener-local init manager,等子依赖全部 ready,才允许它进入 `active_listeners_`、才让 worker 真的 `bind`+`listen`+`accept`**。在子依赖 ready 之前,新 listener 待在 `warming_listeners_`,**不接任何连接**。

### warming 的代码落点

`addOrUpdateListenerInternal`([`listener_manager_impl.cc:590`](../envoy/source/common/listener_manager/listener_manager_impl.cc#L590))是核心。它根据"workers 是否已启动"和"是否已有同名 listener"决定把新 listener 放哪:

```cpp
// source/common/listener_manager/listener_manager_impl.cc (addOrUpdateListenerInternal,简化)
if (existing_warming_listener != warming_listeners_.end()) {
  // 已有同名 warming listener,直接替换它(warming 里没生效,可以原地换)
  *existing_warming_listener = std::move(new_listener);
} else if (existing_active_listener != active_listeners_.end()) {
  // 已有同名 active listener,这是"更新"场景
  if (workers_started_) {
    warming_listeners_.emplace_back(std::move(new_listener));   // workers 已启动 → 进 warming
  } else {
    *existing_active_listener = std::move(new_listener);        // workers 没启动 → 直接替 active
  }
} else {
  // 全新 listener
  if (workers_started_) {
    warming_listeners_.emplace_back(std::move(new_listener));   // workers 已启动 → 进 warming
  } else {
    active_listeners_.emplace_back(std::move(new_listener));    // workers 没启动 → 直接 active
  }
}
// ...
new_listener_ref.initialize();    // 触发 listener-local init manager 开始拉子依赖
```

([`listener_manager_impl.cc:652-698`](../envoy/source/common/listener_manager/listener_manager_impl.cc#L652-L698))

关键分支是 **`workers_started_`** 这个布尔——它表示"N 个 worker 的事件循环是否已经跑起来"。Envoy 启动时,**所有静态 listener 先实例化进 `active_listeners_`、worker 启动后才开始接连接**;启动完成之后(`workers_started_ = true`),所有新来的 listener(无论 LDS 推的还是 API 加的)都先进 `warming_listeners_`,热身完了才进 active。这个分支的意义是:

- **启动期(没 started)**:进程还没开始接流量,可以直接把 listener 放 active,反正没有在途流量会被影响。启动期间整体走 server 的 `init_manager`,等所有 listener、cluster 的 init 都 ready 了才 `startWorkers`。
- **运行期(started)**:已经在接流量了,新 listener 必须先 warming,而且**和同名旧 listener 并存一段时间**(旧 listener 在 active 接连接、新 listener 在 warming 等子依赖),warming 完成那一刻才替换。

> **钉死这件事**:**workers 已启动后,所有 LDS 推来的 listener 都先进 `warming_listeners_`,绝不直接 active**。warming 期间它和同名旧 listener 并存——旧 listener 继续在 active 接连接,新 listener 在 warming 等自己的子 init(SDS 证书等)。这套并存是后面"不停机替换"的物理前提:不存在"新旧都没在接连接"的窗口。

### warming 怎么转 active:`onListenerWarmed`

新 listener `initialize()` 触发 listener-local init manager 拉子依赖,全部 ready 后回调 `onListenerWarmed`([`listener_manager_impl.cc:861`](../envoy/source/common/listener_manager/listener_manager_impl.cc#L861-L893))。这是 warming→active 的转折点:

```cpp
// source/common/listener_manager/listener_manager_impl.cc (onListenerWarmed,简化)
void ListenerManagerImpl::onListenerWarmed(ListenerImpl& listener) {
  // The warmed listener should be added first so that the worker will accept new connections
  // when it stops listening on the old listener.
  if (!doFinalPreWorkerListenerInit(listener)) {
    incListenerCreateFailureStat();
    removeListenerInternal(listener.name(), true);
    return;
  }
  // ① 先让所有 worker 把新 listener addListener 进来(此时新旧 listener 并存,都在 accept)
  for (const auto& worker : workers_) {
    addListenerToWorker(*worker, absl::nullopt, listener, nullptr);
  }

  // ② 把 warming listener 提升为 active,替换掉同名旧 listener
  auto existing_active_listener = getListenerByName(active_listeners_, listener.name());
  auto existing_warming_listener = getListenerByName(warming_listeners_, listener.name());
  if (existing_active_listener != active_listeners_.end()) {
    auto old_listener = std::move(*existing_active_listener);
    *existing_active_listener = std::move(*existing_warming_listener);  // 新的替到 active 位
    drainListener(std::move(old_listener));                            // 旧的进 drain
  } else {
    active_listeners_.emplace_back(std::move(*existing_warming_listener));
  }
  warming_listeners_.erase(existing_warming_listener);
  // ...
}
```

注意注释那句**"The warmed listener should be added first so that the worker will accept new connections when it stops listening on the old listener"**——这是顺序敏感的:**先 add 新 listener(让新 listener 在 worker 上开始 accept),再 drain 旧 listener**。如果反过来——先 drain 旧 listener(它停止 accept)再 add 新 listener——会有一个窗口期"旧的不接、新的还没接",新 SYN 内核回 RST,客户端 503。先 add 后 drain,这个窗口就被填满了:**新旧短暂并存都在 accept,然后旧的才慢慢停**。

注释也揭示了"为什么不直接 add 完新 listener 就立刻 drain 旧的"——`addListenerToWorker` 走 `dispatcher().post(lambda)` 把"add listener"投递到 worker 线程异步执行(承接 P1-02 第五节,MainThread 不直接碰 worker 数据)。所以 `onListenerWarmed` 里 ① 和 ② 之间,worker 线程可能还没真正完成 add。这没事,因为旧 listener 此时还在 accept,新 listener 起来后只是多了一个 listen socket(`SO_REUSEPORT` 允许同址并存,内核负载均衡在新旧之间分),不存在"接不到"的窗口。drain 旧 listener 是另一个异步流程,等它停 accept 时,新 listener 早就起来了。

> **钉死这件事**:**warming→active 的转换顺序是"先 add 新、再 drain 旧"**。这是"替换期间无空窗"的源码保证。配合 `SO_REUSEPORT` 允许新旧 listener 短暂并存(`onListenerWarmed` 注释明说),内核在新旧 listen socket 之间分新连接,直到旧的停 accept、彻底销毁。

### hash 去重:blockLdsUpdate

补一个常被忽略的细节:LDS 重发同一份 listener(版本号可能变了但内容 hash 不变)时,Envoy **不重建 listener**。判断靠 listener 的 hash:

```cpp
// source/common/listener_manager/listener_impl.h (blockLdsUpdate)
bool blockLdsUpdate(uint64_t new_hash) {
  // we should not block the update if FCDS is configured, regardless of the hash.
  return (!configInternal().has_fcds_config() && new_hash == maybe_stale_hash_) ||
         (configInternal().has_fcds_config() && new_hash == maybe_stale_hash_ &&
          /* FCDS 下还要比对额外字段,此处简化 */);
}
```

([`listener_impl.h:248`](../envoy/source/common/listener_manager/listener_impl.h#L248-L256))

`maybe_stale_hash_` 是 listener 实例化时对 config proto 算的 `MessageUtil::hash(config)`(见 [`listener_manager_impl.cc:602`](../envoy/source/common/listener_manager/listener_manager_impl.cc#L602))。LDS 再推一份相同 hash 的,`addOrUpdateListenerInternal` 在 [`L621-627`](../envoy/source/common/listener_manager/listener_manager_impl.cc#L621-L627) 命中 `blockLdsUpdate`,直接返回 `false`(no add/update),不重建。

> **不这样会怎样**:如果 LDS 每次心跳(控制面经常定期重推)都触发完整重建,会出现:每份相同 listener 反复进 warming、反复 drain 同名旧 listener——drain 是有代价的(在途连接要发 GOAWAY、要等超时),这种"无意义的 drain 风暴"会让客户端看到周期性的连接抖动。hash 去重把"内容真的没变"的 LDS 更新挡在重建之外。

> **钉死这件事**:hash 去重是 LDS 更新的"省功阀"。LDS 重推相同内容的 listener,Envoy 用 `blockLdsUpdate(hash)` 直接挡掉,不进 warming、不 drain。这个机制也解释了为什么 `listener_in_place_updated_`、`listener_modified_` 这些 stats 能区分"真改了"和"控制面只是重发"。

---

## 二、两条更新路径:filter chain 原地更新 vs 完整 listener 替换

warming 这一步解决了"新 listener 什么时候生效"的时序问题。但还有更深一层:LDS 推一份新 listener,它和旧 listener 的**差异**可能很小(只改了 filter chain),也可能很大(改了端口、改了 transport socket 类型、改了 listener filter)。这两种差异,Envoy 走**两条不同的更新路径**。判别这一步的,是 `supportUpdateFilterChain`。

### 为什么不全走"完整替换"

朴素想法:不管 LDS 改了什么,统一走"起一份新 listener、drain 旧 listener"——简单一致,为什么不?因为"完整替换"的代价在某些场景下是**不必要的浪费**:

- 完整替换意味着**新 listener 要重新 `bind`+`listen`**——拿一个新 listen fd、新 listener tag、新 socket factory。如果端口没变(`SO_REUSEPORT` 允许新旧并存),内核会在新旧 fd 间分连接,这本身没问题;但如果配置里其实只动了一个 filter chain(比如加了一个 http filter),**socket 层面的所有东西(ip:port、SO_REUSEPORT、transport socket 类型)全没变**,重新 bind 一遍纯粹是浪费。
- 更关键的是,完整替换会让**所有 worker 都重新走一遍 addListener**(N 个 worker × `dispatcher().post`),还会触发 drain——drain 又要在 worker 上扫所有在途连接发 onDrain。如果这次 LDS 只改了一个 route_config(通过 HCM filter chain 里挂的 RDS provider),触发完整 drain 会让所有 HTTP/2 长连接都收到 GOAWAY、客户端被迫重连,而**这些连接其实根本不受这次改动影响**(route 是动态拉的,不必动连接)。

> **不这样会怎样**:如果只改了 filter chain 也走完整替换,会引发**无谓的连接 churn**:客户端所有 HTTP/2 长连接收到 GOAWAY,被迫重建连接,在 K8s 里如果客户端是 Envoy sidecar,这个 churn 会级联(下游 sidecar 重连,它自己的连接池打满)。一次本可以原地完成的小配置变更,引发一次小型"连接风暴"。这就是为什么 Envoy 专门识别"只动 filter chain"的场景,走更轻量的原地更新。

### `supportUpdateFilterChain`:判别能不能原地更新

`ListenerImpl::supportUpdateFilterChain`([`listener_impl.cc:1098`](../envoy/source/common/listener_manager/listener_impl.cc#L1098-L1133))是判别函数。它返回 true,才走原地更新:

```cpp
// source/common/listener_manager/listener_impl.cc (supportUpdateFilterChain,简化)
bool ListenerImpl::supportUpdateFilterChain(const envoy::config::listener::v3::Listener& new_config,
                                            bool worker_started) {
  // 原地更新需要 worker 上已有 active listener,worker_started 保证这一点
  if (!worker_started) {
    return false;
  }
  // 用了 FCDS(Filter Chain Discovery Service)的话,LDS 更新一律走全量
  // 因为 FCDS 的预期就是 filter chain 通过 FCDS 单独动态推,不走 LDS
  if (configInternal().has_fcds_config()) {
    return false;
  }
  // TCP listener 必须至少有一个 filter chain(全量更新拒绝 0 chain,这里保持一致)
  if (new_config.filter_chains_size() == 0) {
    return false;
  }
  // proxy_protocol listener filter 的有无变了,要走全量(因为它影响 socket 层)
  if (usesProxyProto(configInternal()) ^ usesProxyProto(new_config)) {
    return false;
  }
  // 核心判别:除了 filter_chains / default_filter_chain / filter_chain_matcher 之外,
  // 其它字段全没变,才算"只动 filter chain"
  if (ListenerMessageUtil::filterChainOnlyChange(configInternal(), new_config)) {
    // 还要确认 reuse_port 没变(reuse_port 改了要走全量,因为 socket 行为变了)
    return reuse_port_ == getReusePortOrDefault(parent_.server_, new_config, socket_type_);
  }
  return false;
}
```

`ListenerMessageUtil::filterChainOnlyChange`([`listener_impl.cc:1342`](../envoy/source/common/listener_manager/listener_impl.cc#L1342-L1361))用 protobuf 的 `MessageDifferencer` 做字段比对,**忽略** `filter_chains`、`default_filter_chain`、`filter_chain_matcher` 三个字段,比对其它所有字段(address、stat_prefix、listener_filters、transport_socket_factory、listener_filters_timeout、drain_type...)。如果忽略这三个字段后新旧相等,就说明"这次 LDS 只动了 filter chain 相关",可以走原地更新。

注意几个**不能走原地更新的硬条件**:

- **`worker_started == false`**:启动期,worker 上还没有 active listener(`updateListenerConfig` 需要一个已存在的 listener 去更新),只能走全量。
- **用了 FCDS**:`filter_chain_discovery_service` 是单独的 xDS 类型(P5-19 会讲),它的设计就是 filter chain 单独动态推。如果 listener 用了 FCDS,LDS 推全量时就不再原地更新 filter chain,而是让 FCDS 去管。这是协议层的分工。
- **`proxy_protocol` listener filter 有无变了**:这个 listener filter 在 socket 层(读 socket 字节、还原 PROXY protocol header),它影响的是 accept 之后的第一步 socket 处理。这个 filter 加了或去了,要走全量重建。
- **`reuse_port` 改了**:`SO_REUSEPORT` 是 socket option,改了意味着 socket 行为变了(连接分发模型变了,P2-05 第三节),要走全量。
- **`address`/端口变了**:显然要走全量(根本不是同一个 socket 了)。

> **钉死这件事**:**`supportUpdateFilterChain` 判别"除了 filter chain 相关字段,其它字段是否全没变"**。只有全没变,才允许原地更新;任何一个非 filter chain 字段(address、reuse_port、listener_filters、proxy_protocol...)变了,都强制走全量替换。这是正确性的硬约束:原地更新假设"socket 层、listener filter 层都不变,只换 filter chain",这个假设破了就不能走原地更新。

### 路径一:原地 filter chain 更新(`inPlaceFilterChainUpdate`)

判别通过,`addOrUpdateListenerInternal` 在 [`L632-640`](../envoy/source/common/listener_manager/listener_manager_impl.cc#L632-L640) 走这条路:

```cpp
// source/common/listener_manager/listener_manager_impl.cc (addOrUpdateListenerInternal,简化)
if (existing_active_listener != active_listeners_.end() &&
    (*existing_active_listener)->supportUpdateFilterChain(config, workers_started_)) {
  // 用现有 listener 的 socket factory / tag,只换 filter chain 配置
  auto listener_or_error =
      (*existing_active_listener)->newListenerWithFilterChain(config, workers_started_, hash);
  new_listener = std::move(*listener_or_error);
  stats_.listener_in_place_updated_.inc();    // 记一个 stat:这次是原地更新
}
```

`newListenerWithFilterChain`([`listener_impl.cc:1135`](../envoy/source/common/listener_manager/listener_impl.cc#L1135-L1147))构造一个新 `ListenerImpl`,但**从现有 listener 复制 socket factory、listener tag、地址**——本质上"借用"旧 listener 的所有 socket 资源,只把 filter chain 换成新的。

新 listener 实例化后,进 warming(因为它有自己的 listener-local init manager,要拉子依赖——比如新 filter chain 里的 transport socket 可能引用 SDS 新证书)。warming 完成后,回调走的不是 `onListenerWarmed`,而是 **`inPlaceFilterChainUpdate`**([`listener_manager_impl.cc:895`](../envoy/source/common/listener_manager/listener_manager_impl.cc#L895-L923)):

```cpp
// source/common/listener_manager/listener_manager_impl.cc (inPlaceFilterChainUpdate,简化)
void ListenerManagerImpl::inPlaceFilterChainUpdate(ListenerImpl& listener) {
  auto existing_active_listener = getListenerByName(active_listeners_, listener.name());
  auto existing_warming_listener = getListenerByName(warming_listeners_, listener.name());

  // ① 用"旧 listener 的 listener_tag"作为 overridden_listener,
  //    让 worker 把这个 tag 的 active listener 的 config 指针换成新的(updateListenerConfig)
  for (const auto& worker : workers_) {
    addListenerToWorker(*worker, listener.listenerTag(), listener, nullptr);
  }

  // ② 把新 listener 提到 active,旧 listener 拿出来
  auto previous_listener = std::move(*existing_active_listener);
  *existing_active_listener = std::move(*existing_warming_listener);

  // ③ 关键!不是 drainListener(整个 listener drain),而是 drainFilterChains
  //    只 drain 旧 listener 里"新 listener 没有"的那些 filter chain
  drainFilterChains(std::move(previous_listener), **existing_active_listener);

  warming_listeners_.erase(existing_warming_listener);
  // ...
}
```

注意 ① 的 `addListenerToWorker(*worker, listener.listenerTag(), ...)`——第二个参数是 **`overridden_listener`(旧 listener 的 tag)**。这个 tag 进 `WorkerImpl::addListener` → `ConnectionHandlerImpl::addListener`,命中 [`connection_handler_impl.cc:43-51`](../envoy/source/common/listener_manager/connection_handler_impl.cc#L43-L51) 的"updateListenerConfig"分支:

```cpp
// source/common/listener_manager/connection_handler_impl.cc (addListener,简化)
void ConnectionHandlerImpl::addListener(absl::optional<uint64_t> overridden_listener,
                                        Network::ListenerConfig& config, ...) {
  if (overridden_listener.has_value()) {
    ActiveListenerDetailsOptRef listener_detail =
        findActiveListenerByTag(overridden_listener.value());
    listener_detail->get().invokeListenerMethod(
        [&config](Network::ConnectionHandler::ActiveListener& listener) {
          listener.updateListenerConfig(config);    // 只换 config 指针!不拆 socket,不重 accept
        });
    return;
  }
  // ... 否则才是真的 add 一个新 listener ...
}
```

**`updateListenerConfig(config)` 是原地更新的灵魂**——它不拆 socket、不重新 `bind`+`listen`、不重新 `accept`,**只把这个 worker 上 active listener 持有的 config 指针换成新的**(新的 config 包含新 filter chain manager)。旧 listen socket、旧 `TcpListenerImpl`、旧 epoll 注册全都保留。从这一刻起,**这个 worker 上 accept 出的新连接,`findFilterChain` 会去新 filter chain manager 里匹配**(下节详述);老连接还绑在它们 accept 那一刻匹配到的老 filter chain 上,不受影响。

这是承接 P2-05 第五节"updateListenerConfig 是性能优化"那句话的**源码落点**:P2-05 只点到"对于只改 filter chain 的小改动,不用拆 socket、不用重新 accept,原地替换 config 即可",本章把它拆透——它走的是 `inPlaceFilterChainUpdate` + `overridden_listener` + `updateListenerConfig`,配套的是 `drainFilterChains`(不是 `drainListener`)。

### 路径二:完整 listener 替换(`drainListener`)

判别不通过(`supportUpdateFilterChain` 返回 false),走全量替换。`addOrUpdateListenerInternal` 走 [`L641-647`](../envoy/source/common/listener_manager/listener_manager_impl.cc#L641-L647) 的 else 分支,`ListenerImpl::create` 一份全新 listener(新 socket factory、新 tag、可能新地址),进 warming。warming 完成后,走标准的 `onListenerWarmed`([第一节](#warming-怎么转-activeonlistenerwarmed)讲过),里面调 `drainListener`(整个旧 listener drain,不是只 drain filter chain)。

`drainListener` 的完整流程 P2-05 第五节已经拆透(停 accept → 通知在途连接 onDrain → 等 drain 超时 → removeListener),本章不重复。这里只点出一个关键区别:

```
   路径一(in-place filter chain 更新)           路径二(完整 listener 替换)
   ──────────────────────────────────           ──────────────────────────────
   LDS 推新 listener,只动了 filter chain         LDS 推新 listener,动了非 filter chain 字段
            │                                              │
            ▼                                              ▼
   newListenerWithFilterChain(借用旧 socket)     ListenerImpl::create(全新 socket)
            │                                              │
            ▼                                              ▼
   进 warming(等新 filter chain 的子 init)        进 warming(等新 listener 的子 init)
            │                                              │
            ▼                                              ▼
   inPlaceFilterChainUpdate                     onListenerWarmed
            │                                              │
            ▼                                              ▼
   addListenerToWorker(overridden=旧 tag)        addListenerToWorker(overridden=nullopt,新 listener)
   → updateListenerConfig(只换 config 指针)       → 真的 add 新 listener(新 listen socket)
            │                                              │
            ▼                                              ▼
   drainFilterChains(只 drain 旧 chain)          drainListener(整个旧 listener drain)
```

两条路径的差异,本质是"socket / listener filter 层是否变了"——没变走路径一(轻),变了走路径二(重)。这个判别 + 分流,是 Envoy 在"配置生效粒度"上的精细化:**改什么生效什么,没改的不动**。这也是为什么 Istio 那种"频繁小步推 LDS"的场景,大部分更新其实都走路径一(只动 filter chain),不会引发大规模连接 churn。

> **钉死这件事**:**两条路径分流,是 Envoy "最小化变更影响"的体现**。路径一(原地 filter chain 更新)只换 config 指针 + drain 旧 filter chain,socket / listener filter 全不动;路径二(完整替换)才重新 bind socket、整体 drain 旧 listener。LDS 推什么、走哪条路径,由 `supportUpdateFilterChain` 自动判别,运维不用操心。**绝大多数 filter chain 小改走路径一**,只动 filter chain 的在途连接收尾,socket 层完全无感。

---

## 三、filter chain 的"按代际热替换":为什么在途连接不受影响

第二节讲了"原地 filter chain 更新会 `drainFilterChains`",但没拆透**为什么这套能做到在途流量不受影响**。这一节钻进去:filter chain 不是"全局热替换",而是**按连接代际(generation)**——每条连接在 accept 那一刻绑定它当时匹配到的 filter chain,这个绑定**贯穿连接整个生命周期**,不受后续 filter chain 更新影响。

### filter chain 不是全局热替换,是"按连接绑定"

朴素想法:LDS 推了新 filter chain,Envoy 是不是把全局的 filter chain manager 替换掉,所有现有连接下一秒就用新 chain?——**绝对不是**。如果是这样,在途连接会撞墙:

- 假设一条 HTTP/2 长连接上,客户端已经发了 10 个并发 stream,Envoy 这边每个 stream 走在 http filter chain 里(比如 jwt_authn 已经验过 token、ratelimit 已经查过限流)。如果这时候全局热替换 filter chain,把 jwt_authn 换成了 oauth2,正在处理的 stream 会不会突然被新的 oauth2 filter 接管?它的状态(token、限流计数)全错乱。
- 更糟的是,filter chain 上的每个 filter 是**有状态的对象**——每个 filter 实例挂在自己的 stream 上,持有解码进度、buffer、计数。如果 filter chain manager 全局替换,这些 filter 实例要么被销毁(悬空引用、crash),要么被孤立(在途 stream 再也找不到它的 filter)。

Envoy 的实际机制是**按连接代际绑定**:

1. 一条新连接 accept 后,`ActiveStreamListenerBase::newConnection` 会调 `findFilterChain(socket)` 在**当时**的 filter chain manager 里匹配一条 filter chain(P2-05 第二节第 5 步,见 [`active_stream_listener_base.cc:28`](../envoy/source/common/listener_manager/active_stream_listener_base.cc#L27-L67))。
2. 匹配到的 filter chain 被存进这个连接的 `ActiveConnections` 容器(按 filter chain 分组的连接列表),见 [`active_stream_listener_base.cc:144-152`](../envoy/source/common/listener_manager/active_stream_listener_base.cc#L144-L153) 的 `createOrGetActiveConnectionsForFilterChain`。
3. **这条连接后续的所有处理(创建 transport socket、挂 network filter 链、HCM、http filter 链),都基于这个匹配到的 filter chain**。filter chain manager 后续怎么变,这条连接都不受影响——它持的是它那一刻匹配到的那个 filter chain 对象的引用。

> **不这样会怎样(全局热替换会撞什么墙)**:① 在途连接的 filter 状态错乱——已经验过 token 的 stream 被新 filter 接管,新 filter 又验一遍或干脆不认。② filter 实例悬空——全局替换销毁老 filter chain manager,在途 stream 持的 filter 指针成野指针,下次操作 segfault。③ HCM 的连接级状态(connection-level header、stream 计数)和新 filter chain 的语义不匹配(比如老 chain 是 HTTP/1.1、新 chain 是 HTTP/2,codec 类型都不一样)。**全局热替换在语义上根本说不通,因为它违背了"一条连接的处理逻辑在连接生命周期内不变"这个根本契约**。

### 新连接走新 chain,老连接继续用老 chain

这套按连接绑定的机制,叠加 filter chain 更新时的 `drainFilterChains`,产生的效果是:

```
   filter chain 更新那一刻(原地更新 inPlaceFilterChainUpdate 之后):

   ┌──────────────────────────────────────────────────────────────┐
   │  worker 上的 active listener(同一个,TAG 没变)               │
   │                                                              │
   │   新 filter chain manager(updateListenerConfig 换上去的):    │
   │     filter_chain_v2 (新): accept 新连接后 findFilterChain    │
   │                            匹配到这里 → 走新 chain            │
   │                                                              │
   │   老 filter chain(标了 draining,startDraining 置位):       │
   │     filter_chain_v1 (旧): 已有的在途连接继续绑在这里          │
   │                            新连接不会匹配到这里               │
   │                            (虽然还在 manager 里,但 is_draining_)│
   └──────────────────────────────────────────────────────────────┘

   时间线:
   t0: 旧 filter chain v1 接连接
   t1: LDS 推新 config,inPlaceFilterChainUpdate
       - updateListenerConfig 换 config 指针(新 manager 上线)
       - 老 v1 标 draining
   t1+: 新 accept 的连接 → findFilterChain → v2(新)
        t1 之前 accept 的连接 → 还在 v1 上(继续处理完)
   t1 + drain_timeout: v1 上残留连接强制关(removeFilterChain)
```

关键在 `findFilterChain` 的行为——它在新 filter chain manager 里匹配,新 manager 里有 v2 但没有 v1(v1 在旧 manager 里,虽然旧 manager 还在但 `updateListenerConfig` 之后 `findFilterChain` 走的是新 manager)。所以 t1 之后的新连接,只会匹配到 v2。老连接因为持着 v1 的引用,继续在 v1 上跑。

draining 的标记在 filter chain 这一粒度——`FilterChainManagerImpl` 里的 `startDraining()`([`filter_chain_manager_impl.h:72`](../envoy/source/common/listener_manager/filter_chain_manager_impl.h#L72))只是把一个原子布尔置位:

```cpp
// source/common/listener_manager/filter_chain_manager_impl.h (简化)
void startDraining() override { is_draining_.store(true); }
```

这个标志位的作用是告诉 listener manager"这条 filter chain 该 drain 了",触发后续的 `onFilterChainDrainStart`(给这条 chain 上的在途连接发 onDrain)+ `removeFilterChains`(超时后强制清理)。它**不影响在途连接的处理**——在途连接不会因为 chain 标了 draining 就停止处理当前请求,它只是收到一个"开始 drain"的通知(典型是 HCM 发 GOAWAY)。

> **钉死这件事**:**filter chain 热替换的不变式是"按连接代际绑定"**——每条连接在 accept 那一刻匹配到一个 filter chain,这个绑定贯穿连接生命周期。filter chain manager 后续怎么更新,都不影响已绑定的连接;新连接才会匹配到新 chain。这是"在途流量不受影响"的源码级保证,也是为什么 filter chain 可以在运行时热替换的根本。

### drainFilterChains:只 drain 差异部分

第二节提到原地更新调 `drainFilterChains` 而不是 `drainListener`。它的逻辑([`listener_manager_impl.cc:925`](../envoy/source/common/listener_manager/listener_manager_impl.cc#L925-L980))只 drain "旧 listener 有、新 listener 没有"的 filter chain:

```cpp
// source/common/listener_manager/listener_manager_impl.cc (drainFilterChains,简化)
void ListenerManagerImpl::drainFilterChains(ListenerImplPtr&& draining_listener,
                                            ListenerImpl& new_listener) {
  // ① 加进 draining_filter_chains_manager_ 列表
  auto draining_group = draining_filter_chains_manager_.emplace(
      draining_filter_chains_manager_.begin(), std::move(draining_listener), workers_.size());

  // ② 关键!diffFilterChain:比对旧 listener 和新 listener,
  //    找出"旧有、新没有"(或新旧 message 不同)的 filter chain
  draining_group->getDrainingListener().diffFilterChain(
      new_listener, [&draining_group](Network::DrainableFilterChain& filter_chain) mutable {
        filter_chain.startDraining();             // 标 draining
        draining_group->addFilterChainToDrain(filter_chain);
      });

  // ③ 通知所有 worker:这些 filter chain 上的在途连接开始 drain
  for (const auto& worker : workers_) {
    worker->onFilterChainDrain(draining_group->getDrainingListenerTag(),
                               draining_group->getDrainingFilterChains());
  }

  // ④ 启动 drain 序列,drain_time 超时后强制 removeFilterChains
  draining_group->startDrainSequence(
      server_.options().drainTime(), server_.dispatcher(), [this, draining_group]() {
        for (const auto& worker : workers_) {
          worker->removeFilterChains(
              draining_group->getDrainingListenerTag(), draining_group->getDrainingFilterChains(),
              [this, draining_group]() {
                // 每个 worker 移除完 post 回主线程,全部移除完才销毁 draining_group
                server_.dispatcher().post([this, draining_group]() {
                  if (draining_group->decWorkersPendingRemoval() == 0) {
                    draining_filter_chains_manager_.erase(draining_group);
                  }
                });
              });
        }
      });
}
```

`diffFilterChain`([`listener_impl.cc:1149`](../envoy/source/common/listener_manager/listener_impl.cc#L1149-L1164))比对旧 listener 的 filter chain manager 和新 listener 的,**只挑出有差异的**:

```cpp
// source/common/listener_manager/listener_impl.cc (diffFilterChain,简化)
void ListenerImpl::diffFilterChain(const ListenerImpl& another_listener,
                                   std::function<void(Network::DrainableFilterChain&)> callback) {
  // 旧 manager 里标了 draining 的 filter chain,都回调(这些是要 drain 的)
  for (const auto& draining_filter_chain : filter_chain_manager_->drainingFilterChains()) {
    callback(*draining_filter_chain);
  }
  // default filter chain 如果新旧不一致,旧的回调
  if (filter_chain_manager_->defaultFilterChainMessage().has_value() &&
      (!another_listener.filter_chain_manager_->defaultFilterChainMessage().has_value() ||
       !eq(*filter_chain_manager_->defaultFilterChainMessage(),
           *filter_chain_manager_->defaultFilterChainMessage()))) {
    callback(*filter_chain_manager_->defaultFilterChain());
  }
}
```

这意味着:**新旧 listener 共有的、message 完全相同的 filter chain,不会被 drain**——它们在 `updateListenerConfig` 之后由新 manager 继承,在途连接继续用、新连接也继续匹配到它们。只有真正变了(或新增了)的 filter chain 才走 drain 流程。

> **不这样会怎样(无差别 drain 所有 filter chain 会撞什么墙)**:假设一次 LDS 更新里,filter chain A 改了、filter chain B 没变、filter chain C 是新增的。如果无差别地把所有 filter chain 都 drain,会发生什么?**filter chain B 上所有在途连接都被强制发 GOAWAY、被 drain**——而 B 根本没变,这些连接本可以继续正常处理。这是巨大的无谓 churn。`diffFilterChain` 精确到"只 drain 变了的部分",把没变的 filter chain 留在 manager 里继续服务,这是 Envoy "最小化 drain 范围"的体现。

> **钉死这件事**:**`drainFilterChains` 配合 `diffFilterChain`,只 drain 真正变了的 filter chain**。没变的 filter chain 在 `updateListenerConfig` 之后由新 manager 继承,在途连接无感、新连接继续匹配。这是"filter chain 热替换"能做到精细化的根——它不是粗粒度地"换掉整个 manager",而是细粒度地"只 drain 差异"。

---

## 四、drain 的两层:listener drain vs connection drain

到目前为止,drain 这个词出现了好几次。它其实有**两层**,经常被混为一谈。这一节把它拆清。

### 第一层:listener drain(不再 accept 新连接)

第一层 drain 是**listener 粒度**——"这个 listener 停止接受新连接"。它的触发场景:

- 完整 listener 替换(`drainListener`):旧 listener 整体要下线,第一步就是 stopListener(`shutdownListener` 把 listen fd 从 epoll 摘掉),让新 SYN 不再被这个 listener 接。
- 进程级 drain:`Envoy` 收到 SIGTERM / drain 命令,所有 listener 进入 drain(P6-21 hot restart 会讲)。
- LDS 显式删除某个 listener:走 `removeListener` → `drainListener`。

这一层 drain 的效果是**新连接层面**的:这个 listener 不再 accept,客户端发来的新 SYN 要么被内核 RST(如果 listen socket 已关)、要么走别的同址 listener(`SO_REUSEPORT` 并存的新 listener)。**在途连接不受第一层 drain 影响**——它们已经 accept 出来了,fd 不在 listen socket 上。

### 第二层:connection drain(在途连接也优雅结束)

第一层 drain 不影响在途连接。但在途连接也不能就这么一直挂着——它们最终也要收尾。这就是第二层 drain,**connection 粒度**:

- `onListenerDrainStart`(整个 listener drain 触发)或 `onFilterChainDrainStart`(filter chain drain 触发),会遍历这个 listener / 这条 filter chain 上的**所有在途连接**,给每条调 `connection_->onDrain()`(见 [`active_stream_listener_base.cc:155`](../envoy/source/common/listener_manager/active_stream_listener_base.cc#L155-L192))。
- `connection_->onDrain()` 的效果取决于这条连接跑的 filter chain:
  - **HTTP/2 / HTTP/3(HCM)**:`onDrain` 让 codec 进入 drain 状态,**给客户端发 GOAWAY 帧**,告诉它"别再开新流了,现有流我会处理完"。客户端收到 GOAWAY 知道要换连接,而不是继续往这个连接发请求然后被拒。承接 P3-08 HCM。
  - **HTTP/1.1**:HCM 在当前请求处理完后**不再接新请求**,并在响应里加 `Connection: close` 头,告诉客户端处理完这个就关。
  - **tcp_proxy(纯 TCP 代理)**:取决于配置,通常是半关连接、等对端发完。
  - **Redis / Mongo / Thrift 等专用协议 filter**:各自定义 drain 行为(比如 Redis proxy 给客户端发特定错误码让它重连)。

这两层是**叠加的**:listener drain 之后,这个 listener 既不接新连接(第一层),又给在途连接发 drain 通知让它们有序结束(第二层)。两层配合,做到"新流量平滑到新 listener,老流量有序收尾"。

```
   listener drain 的两层(完整替换 drainListener 场景):

   ┌──────────────────────────────────────────────────────────────┐
   │ 第一层:listener drain(stopListener → shutdownListener)       │
   │   - listen fd 从 epoll 摘掉                                   │
   │   - 新 SYN 不再被这个 listener accept                          │
   │   - 新 SYN 走同址新 listener(或被 RST,如果没新 listener)     │
   │                                                              │
   │   影响:新连接层面。在途连接不受影响。                          │
   └──────────────────────────────────────────────────────────────┘
                              │
                              ▼ (onListenerDrainStart 遍历在途连接)
   ┌──────────────────────────────────────────────────────────────┐
   │ 第二层:connection drain(connection->onDrain)                 │
   │   - HTTP/2: codec 发 GOAWAY,客户端别再开新流                  │
   │   - HTTP/1.1: 处理完当前请求,回 Connection: close             │
   │   - tcp_proxy: 半关,等对端发完                                │
   │                                                              │
   │   影响:在途连接层面。让客户端有序迁移,而不是被掐断 503。       │
   └──────────────────────────────────────────────────────────────┘
                              │
                              ▼ (drain_time 超时)
   ┌──────────────────────────────────────────────────────────────┐
   │ 兜底:removeFilterChain / removeListener                      │
   │   - 残留连接强制 close(NoFlush,不刷数据)                      │
   │   - 销毁 listener 对象 / filter chain 对象                    │
   │                                                              │
   │   防止"卡死的连接永远卡着,listener 永远销毁不掉"。            │
   └──────────────────────────────────────────────────────────────┘
```

> **钉死这件事**:**listener drain(第一层,停 accept)和 connection drain(第二层,在途连接发 GOAWAY)是两件事,经常被混为一谈**。第一层管新连接,第二层管在途连接。两层叠加,加上超时兜底强制清理,构成"在途流量有序收尾、新流量平滑到新 listener、无 503、无 crash"的完整保证。P2-05 第五节讲的 drain 基本机制,主要是这两层的执行细节;本章的控制面视角,关注的是"什么时候触发、drain 范围怎么界定"。

---

## 五、`DrainManagerImpl`:渐进式 drainClose,为什么不是"立刻全关"

第二层 drain(connection drain)有个微妙的设计:**不是"立刻全关所有在途连接",而是渐进式的**。这个渐进式由 `DrainManagerImpl::drainClose` 实现,它是 Envoy "不停机生效"三件套里最容易被忽略、但最影响客户端体验的一环。

### 为什么不能"立刻全关"

朴素想法:listener 要 drain 了,把所有在途连接立刻 close 掉,简单粗暴,反正都要关。为什么不?

- **客户端瞬间重连风暴**:假设有 1000 条 HTTP/2 长连接,立刻全关,客户端同时收到连接被关,同时重连。重连请求涌入(可能是同一个 Envoy 或上游),造成瞬时尖峰,可能压垮后端。
- **GOAWAY 来不及发**:HTTP/2 的 GOAWAY 是个控制帧,要等连接的写窗口有空才能发。如果立刻 close,GOAWAY 可能还没发出去连接就断了,客户端根本不知道"该换连接了",它会把在途的请求当作网络错误,触发应用层重试,重试又加剧风暴。
- **健康检查来不及生效**:在 K8s 里,Envoy drain 期间通常会标自己 NotReady(通过 `--drain-time-s` 配合 readiness probe),让 endpoint controller 把这个 Pod 从 Service 后端摘掉。摘掉需要时间(kubelet 上报 → apiserver → endpoint controller → 各客户端 watcher 收到)。如果立刻 close,客户端还没收到"这个 Pod 该摘掉"的通知,还在往这个 Pod 发流量,全失败。渐进式 drain 给这个"摘除传播"留出时间窗口。

> **不这样会怎样(立刻全关会撞什么墙)**:① 客户端重连风暴,后端被瞬时尖峰压垮(实测案例:某团队 drain 时不配 drain time 用默认立即关,客户端是 Envoy sidecar,连接池瞬间打满,下游服务也跟着抖动)。② GOAWAY 没发出去,客户端按网络错误处理,业务层无差别重试,雪崩。③ K8s readiness 摘除没传播完,客户端还在发流量到已 drain 的 Pod,大量 5xx。**立刻全关违背"优雅下线"的本意——它把"下线"的代价全压给客户端**。

### 渐进式 drainClose 的概率模型

`DrainManagerImpl::drainClose`([`drain_manager_impl.cc:44`](../envoy/source/server/drain_manager_impl.cc#L44-L99))是渐进式的核心。它返回一个布尔——"这条连接该不该现在关"。HCM 在处理每个请求时会查这个决策(见 [`conn_manager_impl.cc:1959`](../envoy/source/common/http/conn_manager_impl.cc#L1959-L1968) 的 `drain_close_.drainClose(drain_scope)`):

```cpp
// source/server/drain_manager_impl.cc (drainClose,简化)
bool DrainManagerImpl::drainClose(Network::DrainDirection direction) const {
  // 健康检查失败 + DEFAULT drain type → 立刻 drain close
  if (drain_type_ == envoy::config::listener::v3::Listener::DEFAULT &&
      server_.healthCheckFailed()) {
    return true;
  }

  auto current_drain = draining_.load();
  if (!current_drain.first) {
    return false;                       // 没在 drain,不关
  }

  // 方向不匹配(比如 drain 方向是 InboundOnly,但这条连接是 Outbound),不关
  if (direction == Network::DrainDirection::None || direction > current_drain.second) {
    return false;
  }

  // 立即策略:配置了 drain_strategy: Immediate 就立刻关
  if (server_.options().drainStrategy() == Server::DrainStrategy::Immediate) {
    return true;
  }

  // 渐进策略(默认):
  // P(关) = elapsed_time / drain_time
  // 超过 deadline 就 100% 关
  const MonotonicTime current_time = dispatcher_.timeSource().monotonicTime();
  auto deadline = drain_deadlines_.find(direction);
  if (current_time >= deadline->second) {
    return true;                        // 超时,必关
  }

  const auto remaining_time =
      std::chrono::duration_cast<std::chrono::seconds>(deadline->second - current_time);
  const auto drain_time = server_.options().drainTime();
  const auto drain_time_count = drain_time.count();
  if (drain_time_count == 0) {
    return true;                        // 没配 drain time,立刻关
  }
  const auto elapsed_time = drain_time - remaining_time;
  // 关键!P(关) = elapsed / drain_time,用 random % drain_time_count 做随机抽样
  return static_cast<uint64_t>(elapsed_time.count()) >
         (server_.api().randomGenerator().random() % drain_time_count);
}
```

这个概率模型很精妙——**P(关) = elapsed_time / drain_time**:

- drain 刚开始(elapsed = 0):P(关) = 0,几乎不关任何连接。
- drain 进行到一半(elapsed = drain_time/2):P(关) = 50%,大约一半连接被关。
- drain 接近超时(elapsed ≈ drain_time):P(关) ≈ 100%,几乎所有连接被关。
- drain 超时(current_time >= deadline):100% 关,强制清理。

随机抽样(`random() % drain_time_count`)保证不是"按顺序关",而是"随机挑一部分关"——这样不同连接被关的时机分散,避免瞬时尖峰。

> **承接一个细节**:这个概率模型不是简单线性,而是 `elapsed > random() % drain_time_count`。把它数学化:P(关) = P(elapsed > U),其中 U ~ Uniform{0, ..., drain_time_count - 1}。当 elapsed = k,P(关) = P(U < k) = k / drain_time_count = elapsed / drain_time。线性增长,符合直觉。**这个渐进的概率 ramp,让客户端有时间收到 GOAWAY、K8s 有时间摘除 Pod、应用层有时间感知 drain,而不是瞬间全军覆没。**

### drainClose 的回调机制:addOnDrainCloseCb

除了"每次请求查 drainClose"(polling 式),`DrainManagerImpl` 还提供 `addOnDrainCloseCb`([`drain_manager_impl.cc:101`](../envoy/source/server/drain_manager_impl.cc#L101-L130)),让 filter 注册一个回调,在 drain 开始时被通知。回调可以拿到一个 `drain_delay`(随机延迟),在延迟后再真正开始 drain 自己——这是让不同 filter 错峰 drain 的机制:

```cpp
// source/server/drain_manager_impl.cc (addOnDrainCloseCb,简化)
Common::CallbackHandlePtr DrainManagerImpl::addOnDrainCloseCb(Network::DrainDirection direction,
                                                              DrainCloseCb cb) const {
  auto current_drain = draining_.load();
  if (current_drain.first && direction <= current_drain.second) {
    // 已经在 drain 了,算一个随机 delay,延迟后回调
    std::chrono::milliseconds drain_delay{0};
    if (server_.options().drainStrategy() != Server::DrainStrategy::Immediate) {
      const auto delta = drain_deadlines_.find(direction)->second - current_time;
      const auto ms = std::chrono::duration_cast<std::chrono::milliseconds>(delta).count();
      if (ms > 0) {
        drain_delay = std::chrono::milliseconds(random() % ms);   // 随机 delay
      }
    }
    cb(drain_delay);
    return nullptr;
  }
  return cbs_.add(cb);    // 还没 drain,先注册,drain 开始时一起回调
}
```

`startDrainSequence`([`drain_manager_impl.cc:147`](../envoy/source/server/drain_manager_impl.cc#L147-L216))在 drain 开始时,**把所有注册的回调按"错峰延迟"调用**——延迟分布在 drain 窗口的**前 1/4**(`step_count / 4 / num_cbs`),确保 drain 在窗口前段就启动,留足时间给后续优雅收尾:

```cpp
// source/server/drain_manager_impl.cc (startDrainSequence 里的错峰回调,简化)
uint32_t step_count = 0;
size_t num_cbs = cbs_.size();
cbs_.runCallbacksWith([&]() {
  // 把 callbacks 错峰分布在 drain 窗口的前 1/4
  std::chrono::milliseconds delay{static_cast<int64_t>(
      static_cast<double>(step_count) / 4 / num_cbs *
      std::chrono::duration_cast<std::chrono::milliseconds>(remaining_time).count())};
  step_count++;
  return delay;
});
```

这个"前 1/4 错峰"的设计很讲究——如果错峰分布在整个 drain 窗口,可能 drain 都快超时了还有 callback 没被调,留给优雅收尾的时间不够。集中在前 1/4,保证 drain 早期所有 filter 都知道"开始了",剩下 3/4 留给它们处理在途、发 GOAWAY、等客户端迁移。

> **钉死这件事**:**`DrainManagerImpl` 的渐进式 drainClose 是 Envoy "不停机生效"的客户端体验保证**。它用 `P(关) = elapsed/drain_time` 的概率 ramp + 随机抽样,让连接被关的时机分散在整个 drain 窗口,避免瞬时尖峰;用"前 1/4 错峰回调"让 filter 早期就知道 drain 开始,留足收尾时间。这套机制的对立面是"立即全关"(配置 `drain_strategy: Immediate` 或不配 drain time),那是给"紧急下线"场景的逃生阀,正常生产用渐进式。

### DrainDirection:All vs InboundOnly

`drainClose` 里有个 `direction` 参数,涉及 `DrainDirection` 的设计——Envoy 支持两种 drain 方向:

- **`DrainDirection::All`**:所有连接(入站 + 出站)都 drain。这是完整 listener 替换、进程级 drain 的默认。
- **`DrainDirection::InboundOnly`**:只 drain 入站连接(downstream → Envoy),出站连接(Envoy → upstream)不 drain。这在 sidecar 场景有用——你想 drain 这个 sidecar 接受新 downstream 连接,但**不想中断已经发到 upstream 的请求**(upstream 那边可能还在处理,中断会让 upstream 的工作白费)。

`drainClose` 里的判断 `direction > current_drain.second`(L66)实现这个:如果当前 drain 方向是 InboundOnly,一条 Outbound 连接来查 drainClose,direction(Outbound)> current_drain.second(InboundOnly),返回 false,不 drain。这个方向过滤让 drain 可以精细到"只 drain 某个方向",在 sidecar / 双向代理场景很有用。

> **钉死这件事**:**`DrainDirection` 让 drain 可以精细到方向粒度**。InboundOnly 只 drain 入站(保护在途的 upstream 请求),All 全 drain。这个设计在 sidecar 场景尤其重要——sidecar 同时是 downstream 的 server 和 upstream 的 client,两个方向的 drain 策略可能不同。

---

## 六、整体时序:LDS 下发 → warming → 替换 → drain

把前面几节串起来,一次完整的"LDS 推新 listener,Envoy 不停机生效"的时序是这样的(以**完整 listener 替换**为例,filter chain 原地更新的时序类似但更轻):

```mermaid
sequenceDiagram
    participant CP as 控制面 (Istio)
    participant LDS as LdsApiImpl<br/>(MainThread)
    participant LM as ListenerManagerImpl<br/>(MainThread)
    participant W as N 个 worker<br/>(各自线程)
    participant C as Client

    Note over CP,LDS: 第 17 章:LDS 在 gRPC 流上下发
    CP->>LDS: StreamListenersResponse<br/>(新 listener proto + version_info)
    LDS->>LM: addOrUpdateListener(config, version_info)

    Note over LM: 第一节:warming
    LM->>LM: addOrUpdateListenerInternal
    LM->>LM: supportUpdateFilterChain?<br/>否(动了非 filter chain 字段)→ 完整替换
    LM->>LM: ListenerImpl::create(全新 socket factory, 新 tag)
    LM->>LM: warming_listeners_.emplace_back(new_listener)
    LM->>LM: new_listener.initialize()<br/>(触发 listener-local init manager 拉 SDS 等)

    Note over LM: 等子依赖(SDS 证书等)就绪
    Note over LM: 期间:旧 listener 仍在 active 接连接
    C->>W: 新 SYN(被旧 listener accept)
    W-->>C: 正常处理

    Note over LM: 子依赖就绪,onListenerWarmed
    LM->>LM: onListenerWarmed(new_listener)
    LM->>W: addListenerToWorker(overridden=nullopt)<br/>每个 worker post 一个 add 新 listener 的 lambda
    Note over W: 新 listener bind+listen(SO_REUSEPORT 同址并存)<br/>内核在新旧 listen socket 间分新连接

    LM->>LM: drainListener(旧 listener)
    Note over LM: P2-05 第五节:stopListener → onListenerDrain → 等超时 → removeListener

    LM->>W: stopListener(旧 tag)<br/>每个 worker 把旧 listen fd 从 epoll 摘掉
    Note over W: 第一层 listener drain:旧 listener 不再 accept<br/>新 SYN 全走新 listener
    C->>W: 新 SYN(走新 listener)
    W-->>C: 用新 filter chain 处理

    LM->>W: onListenerDrain(旧 tag)<br/>通知旧 listener 在途连接
    Note over W: 第二层 connection drain:遍历旧 listener 在途连接<br/>每条调 connection->onDrain()
    W-->>C: HTTP/2: 发 GOAWAY;HTTP/1.1: 回 Connection: close

    Note over C: 客户端收到 GOAWAY,开始往新连接迁移<br/>(但旧连接的现有流继续处理完)

    Note over LM,W: DrainManagerImpl::startDrainSequence 启动定时器<br/>渐进式 drainClose: P(关)=elapsed/drain_time

    Note over LM: drain_time 超时
    LM->>W: removeListener(旧 tag)<br/>每个 worker 销毁旧 listener 对象
    W->>W: 残留连接强制 close(NoFlush)
    W-->>LM: completion callback(post 回主线程)
    Note over LM: 所有 worker 都摘干净,draining_listeners_.erase
```

这张时序图覆盖了完整 listener 替换的全过程。filter chain 原地更新的时序差异在于:

- 不走 `ListenerImpl::create`(全新 socket),走 `newListenerWithFilterChain`(借用旧 socket)。
- `onListenerWarmed` 换成 `inPlaceFilterChainUpdate`。
- `addListenerToWorker` 带 `overridden_listener = listenerTag()`,worker 上走 `updateListenerConfig`(只换 config 指针,不重 bind)。
- `drainListener` 换成 `drainFilterChains`(只 drain diff 出来的旧 filter chain)。

两条路径的"骨架"是一样的(warming → 替换 → drain),差异在每一步的粒度(socket 级 vs config 指针级、listener 级 vs filter chain 级)。

---

## 七、技巧精解:filter chain 代际热替换 + 渐进式 drainClose 的"为什么 sound"

本章最硬核的两个技巧,单独拎出来配源码 + 反面对比拆透。

### 技巧一:filter chain 按"连接代际"绑定,而非全局热替换

**这个技巧要解决的问题**:filter chain 配置变了(LDS 推了新 filter chain),怎么让"新连接用新 chain、老连接继续用老 chain、互不影响"?朴素方案是"全局热替换 filter chain manager",但会撞墙(第二节讲过:在途连接 filter 状态错乱、filter 实例悬空、HCM 连接级状态和新 chain 语义不匹配)。

**手段**:三件事配合——

1. **每条连接在 accept 那一刻绑定它匹配到的 filter chain**:`ActiveStreamListenerBase::newConnection` 里 `findFilterChain` 匹配一条 chain,这条 chain 被存进连接的 `ActiveConnections` 容器(按 chain 分组),贯穿连接生命周期。
2. **filter chain 更新时不改老连接的绑定**,只换 filter chain manager(`updateListenerConfig` 换 listener 持的 config 指针)。新 manager 里有新 chain,但老连接持的是老 chain 对象的引用,新 manager 怎么变都不影响它们。
3. **老 filter chain 单独 drain**:`drainFilterChains` + `diffFilterChain` 只 drain 真正变了的 chain,标 `startDraining()`。在途连接(绑在老 chain 上)收到 `onFilterChainDrainStart` → `connection->onDrain()`,HCM 发 GOAWAY。新连接走新 chain,完全不受影响。

**为什么 sound(三层)**:

1. **连接绑定的原子性**:一条连接一旦 accept 完成、`findFilterChain` 匹配、`createNetworkFilterChain` 挂上 filter 链,它的处理逻辑就**冻结**在那一刻的 filter chain 上。后续 manager 更新是"换 manager 指针",不是"改这条连接的 filter"。这是源码级的保证——连接持的 filter chain 引用不会因为 manager 换了而失效。
2. **新连接天然走新 chain**:`updateListenerConfig` 之后,worker 的 active listener 持的是新 config(新 manager),`findFilterChain` 在新 manager 里匹配,自然匹配到新 chain。这是"新连接用新"的落点。
3. **老连接 drain 不阻塞新连接**:老 chain 的 drain(GOAWAY、等超时)是老连接自己的事,新连接根本不经过老 chain。新旧 chain 并存,各管各的连接,直到老 chain 的连接全清掉、老 chain 被销毁。

**源码佐证**:

- 连接绑定 filter chain:[`active_stream_listener_base.cc:27 newConnection`](../envoy/source/common/listener_manager/active_stream_listener_base.cc#L27-L67) 里 `findFilterChain` 匹配 + `createNetworkFilterChain` 挂链 + `newActiveConnection(*filter_chain, ...)` 存进 `ActiveConnections`(按 chain 分组,见 [`active_stream_listener_base.cc:144`](../envoy/source/common/listener_manager/active_stream_listener_base.cc#L144-L153))。
- 只换 config 指针:[`connection_handler_impl.cc:43-51 addListener`](../envoy/source/common/listener_manager/connection_handler_impl.cc#L43-L51) 的 `overridden_listener` 分支调 `updateListenerConfig`。
- 老 chain drain:[`listener_manager_impl.cc:925 drainFilterChains`](../envoy/source/common/listener_manager/listener_manager_impl.cc#L925-L980) + [`listener_impl.cc:1149 diffFilterChain`](../envoy/source/common/listener_manager/listener_impl.cc#L1149-L1164)。
- 在途连接 onDrain:[`active_stream_listener_base.cc:155 onFilterChainDrainStart`](../envoy/source/common/listener_manager/active_stream_listener_base.cc#L155-L177) 只遍历"draining 的 filter chain 上的连接",不碰新 chain 的连接。

**反面对比(朴素全局热替换会撞什么墙)**:

| 方面 | 朴素全局热替换 | **Envoy 按连接代际绑定** |
|------|---------------|------------------------|
| 在途连接 filter 状态 | 错乱(被新 filter 接管,状态不匹配) | **不变**(绑死在老 chain) |
| filter 实例生命周期 | 悬空(老 manager 销毁,引用成野指针) | **安全**(老 chain 对象保留到所有连接清完) |
| HCM 连接级状态(codec、stream 计数) | 和新 chain 语义不匹配 | **一致**(连接生命周期内 codec 不变) |
| 新连接 | 也走老 chain(直到 manager 替换) | **走新 chain**(manager 替换后) |
| drain 范围 | 全部连接(无差别) | **只 drain 老 chain 的连接** |

> **钉死这件事**:**filter chain 按连接代际绑定,是 Envoy "filter chain 可以热替换"的根本不变式**。它的妙处不在"换 manager"(那是表面),而在"换 manager 不影响已绑定的连接"——这是源码级的对象引用语义保证的。朴素全局热替换违背"连接处理逻辑在生命周期内不变"这个契约,必然撞墙。这套机制也解释了为什么 Envoy 可以在不丢一个在途请求的前提下,换掉正在线上跑的 filter chain——这是 Nginx reload(整个 worker 进程换班)做不到的粒度。

### 技巧二:渐进式 drainClose 的概率 ramp,而非立即全关

**这个技巧要解决的问题**:第二层 connection drain 要让在途连接结束,但"立刻全关"会引发重连风暴、GOAWAY 没发出去、K8s 摘除没传播完(第五节讲过)。需要一个机制,让连接"分散地、有序地"被关,而不是瞬间全军覆没。

**手段**:`DrainManagerImpl::drainClose` 用一个概率函数 `P(关) = elapsed_time / drain_time`,HCM 在处理每个请求时查这个决策。配合"前 1/4 错峰回调"让 filter 早期感知 drain。

**为什么 sound(三层)**:

1. **概率 ramp 让连接被关的时机分散**:drain 刚开始 P(关)≈0,几乎不关;中间 P(关)=50%;接近超时 P(关)≈100%。随机抽样(`random() % drain_time_count`)保证不是"按顺序关",而是"随机挑一部分",进一步分散。
2. **GOAWAY 有时间发**:连接被关之前,HCM 已经(通过 `onDrain`)给客户端发了 GOAWAY。客户端收到 GOAWAY 知道"别开新流了",开始往新连接迁移。渐进式让这个迁移分散在整个 drain 窗口,而不是瞬间。
3. **超时兜底**:超过 `drain_deadline` 后 P(关)=100%,强制清理。防止"卡死的连接永远卡着"。

**源码佐证**:

- 概率 ramp:[`drain_manager_impl.cc:84-98`](../envoy/source/server/drain_manager_impl.cc#L84-L98),`elapsed_time.count() > random() % drain_time_count`,deadline 超过则必关。
- HCM 查询决策:[`conn_manager_impl.cc:1959`](../envoy/source/common/http/conn_manager_impl.cc#L1959-L1968),`drain_close_.drainClose(drain_scope)` 决定是否给这条连接发 GOAWAY。
- 前 1/4 错峰回调:[`drain_manager_impl.cc:206-215`](../envoy/source/server/drain_manager_impl.cc#L206-L215),`step_count / 4 / num_cbs * remaining_time` 把 callbacks 错峰在窗口前 1/4。

**反面对比(立即全关会撞什么墙)**:

| 方面 | 立即全关(Immediate) | **渐进式 drainClose(Gradual)** |
|------|---------------------|-------------------------------|
| 客户端重连 | 瞬间风暴,压垮后端 | **分散**,后端无感 |
| GOAWAY | 可能没发出去连接就断 | **有时间发**,客户端有序迁移 |
| K8s 摘除 | 没传播完,客户端还在发 | **窗口内传播完** |
| 卡死连接 | 也立即关(其实没问题) | 超时兜底也关 |

立即全关不是没用——它是"紧急下线"(配置 `drain_strategy: Immediate` 或 `healthCheckFailed`)的逃生阀:Envoy 自己健康检查失败了,赶紧关所有连接,让客户端去别的健康实例。但正常 drain 用渐进式,代价由 Envoy 自己承担(多挂一会儿连接),客户端体验好。

> **钉死这件事**:**渐进式 drainClose 的概率 ramp,是 Envoy 把"下线代价"从客户端挪回服务端的设计**。立即全关把代价全压给客户端(重连风暴、5xx);渐进式让 Envoy 多挂一会儿、慢慢关,客户端无感迁移。这个设计哲学和 P4-14 outlier detection(被动踢出后端,给后端恢复时间)、P4-15 retry budget(限制重试,不无限重试压垮后端)一脉相承——**Envoy 在每一个"可能引发风暴"的地方,都主动加平滑机制**。

---

## 八、章末小结

### 回扣主线

本章是**控制面**这一侧——回答"LDS 下发的 listener 配置,怎么在不停机、不影响在途流量的前提下生效"。它承接第 17 章(xDS 怎么传)和 P2-05(listener drain 的数据面机制),把"配置动态生效"这件事钉死在源码级:

- **控制面这一面**:warming(`warming_listeners_` 等子 init)是"新 listener 不直接 active"的时序保证;两条更新路径(`supportUpdateFilterChain` 判别,filter chain 原地更新 vs 完整 listener 替换)是"最小化变更影响"的精细化;filter chain 按代际绑定 + `drainFilterChains` 是"在途流量不受影响"的不变式落点;`DrainManagerImpl` 渐进式 drainClose 是"客户端体验"的平滑保证。
- **数据面这一面(衔接)**:drain 的具体执行(停 accept、遍历连接 onDrain、removeListener)在 P2-05 已拆透,本章只在控制面视角引用。HCM 收到 `onDrain` 发 GOAWAY 的细节在 P3-08。

这正好接上第 17 章末尾的悬念:"LDS 把 listener 推过来了,然后呢?"——答案是 **warming → 替换(两条路径)→ drain(两层 + 渐进式),全程不停机**。listener 这条线讲完,下一章 P5-19 转到 cluster / endpoint 的动态发现(CDS/EDS),看后端实例增减怎么实时反映到 Envoy——那是另一类 xDS(CDS 加减 cluster、EDS 加减 endpoint),它的热更新机制和本章的 listener drain 又有不同(cluster drain 是连接池层面的,不是 listener 层面)。

### 五个为什么

1. **为什么新 listener 收到不直接 active,要先 warming?**——listener 不是孤立的,它带着子依赖(典型 SDS 证书、某些 listener filter 的 init)。直接 active 会出现"listener 已在接连接、但证书还没到 → TLS 握手失败"的半生不熟状态。warming 让新 listener 先在 `warming_listeners_` 把子依赖拉起来,ready 了才进 active 替换旧的。

2. **为什么 LDS 更新分两条路径(filter chain 原地更新 vs 完整 listener 替换)?**——完整替换代价大(重新 bind socket、N 个 worker 重新 addListener、整体 drain),如果只动了 filter chain 也走完整替换会引发无谓的连接 churn(所有 HTTP/2 长连接收 GOAWAY)。`supportUpdateFilterChain` 判别"只动 filter chain"的场景,走轻量的原地更新(`updateListenerConfig` 只换 config 指针,`drainFilterChains` 只 drain 差异)。判错路径要么性能损失(全量重建)、要么正确性坏(把该重建的当原地更新)。

3. **为什么 filter chain 是"按连接代际绑定"而不是全局热替换?**——全局热替换违背"连接处理逻辑在生命周期内不变"这个契约:在途连接的 filter 状态错乱、filter 实例悬空、HCM 连接级状态和新 chain 语义不匹配。按代际绑定:每条连接在 accept 那一刻匹配到一个 filter chain,这个绑定贯穿连接生命周期;新连接走新 chain,老连接继续用老 chain 直到处理完(或 drain 超时强制关)。这是"在途流量不受影响"的源码级保证。

4. **为什么 drain 分两层(listener drain + connection drain)?**——listener drain(第一层)管新连接:停 accept,新 SYN 走别的 listener;但它不影响在途连接(它们已 accept 出来,fd 不在 listen socket 上)。connection drain(第二层)管在途连接:遍历调 `connection->onDrain()`,HCM 发 GOAWAY、HTTP/1.1 回 `Connection: close`。两层叠加,新流量平滑到新 listener,老流量有序收尾,无 503、无 crash。

5. **为什么 drainClose 是渐进式概率 ramp 而不是立即全关?**——立即全关把"下线代价"全压给客户端:重连风暴、GOAWAY 没发出去、K8s 摘除没传播完。渐进式 `P(关) = elapsed/drain_time` + 随机抽样,让连接被关的时机分散在整个 drain 窗口,客户端有时间收到 GOAWAY、K8s 有时间摘除 Pod、应用层有时间感知。立即全关是"紧急下线"(`drain_strategy: Immediate` 或 `healthCheckFailed`)的逃生阀,正常生产用渐进式。

### 想继续深入往哪钻

- **想看 warming / init manager 机制**:`source/common/listener_manager/listener_impl.cc`(`ListenerImpl::initialize` @ L1064、`dynamic_init_manager_` 构造 @ L360)、`source/common/init/`(Init::Manager / Init::Target / Init::Watcher,Envoy 通用的"目标就绪前不暴露"机制)。
- **想看两条更新路径**:`source/common/listener_manager/listener_manager_impl.cc`(`addOrUpdateListenerInternal` @ L590 判别、`inPlaceFilterChainUpdate` @ L895、`onListenerWarmed` @ L861、`drainFilterChains` @ L925、`drainListener` @ L724)、`source/common/listener_manager/listener_impl.cc`(`supportUpdateFilterChain` @ L1098、`newListenerWithFilterChain` @ L1135、`diffFilterChain` @ L1149、`filterChainOnlyChange` @ L1342)。
- **想看 updateListenerConfig(原地 config 指针替换)**:`source/common/listener_manager/connection_handler_impl.cc`(`addListener` 的 `overridden_listener` 分支 @ L43-L51)。
- **想看 DrainManager**:`source/server/drain_manager_impl.cc`(`drainClose` 渐进式概率 @ L44、`addOnDrainCloseCb` 错峰回调 @ L101、`startDrainSequence` @ L147)、`source/server/drain_manager_impl.h`(DrainPair / DrainDirection / drain_deadlines_)、`envoy/server/drain_manager.h`(DrainManager 接口)、`envoy/network/drain_decision.h`(DrainDecision 接口,`drainClose` 的接口定义)。
- **想看 HCM 怎么响应 drain**:`source/common/http/conn_manager_impl.cc`(L1700 `drain_state_ = Draining`、L1959 查 `drainClose` 决定发 GOAWAY、L836 shutdownNotice/goAway 流程),细节在 P3-08。
- **想看 drain 在配置层的样子**:`envoy/config/listener/v3/listener.proto`(`DrainType` 枚举:DEFAULT vs MODIFY_ONLY)、`envoy/config/bootstrap/v3/bootstrap.proto`(`drain_time` / `drain_strategy`、`parent_shutdown_time`)、Envoy CLI `--drain-time-s`、`--drain-strategy`。
- **Envoy 官方文档**:Envoy docs "Listener / LDS" 章节、"Draining" 章节(讲 listener drain 的运维视角)、`source/docs/listener.md`(架构文档)。
- **承接**:`source/server/hot_restart_impl.cc`(进程级 hot restart,和 listener drain 的关系,P6-21 详讲——hot restart 是整个进程换班,通过 fd 传递让新进程接管 socket,新进程起来后旧进程走 drain)。

### 引出下一章

我们搞清楚了"LDS 下发的 listener 怎么不停机生效"——warming 等子依赖、两条更新路径、filter chain 代际绑定、drain 两层 + 渐进式。但 listener 只是"接连接的入口",真正的流量要转发到后端 cluster / endpoint。后端实例在 K8s 里频繁上下线(扩容、缩容、滚动发布),Envoy 怎么**秒级感知**后端的增减?这就是 **CDS(Cluster Discovery Service)+ EDS(Endpoint Discovery Service)** 的事。下一章 P5-19,我们讲 cluster 和 endpoint 的动态发现:CDS 加减 cluster、EDS 加减 endpoint(秒级服务发现的核心),以及 cluster 的热更新机制(它和 listener drain 不同——cluster 变了影响的是连接池和负载均衡,不是 listener accept)。

> **下一章**:[P5-19 · Cluster/Endpoint 动态发现:CDS/EDS](P5-19-Cluster-Endpoint动态发现-CDS-EDS.md)
