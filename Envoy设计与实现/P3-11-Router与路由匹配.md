# 第 3 篇 · 第 11 章 · Router 与路由匹配

> **核心问题**:一条 HTTP 请求穿过 HCM 的解码器链、再经过 `ratelimit`/`fault`/`jwt_authn` 这些 http filter 之后,链的最后一站通常是 `router` filter。它面对的核心问题极其朴素——**这条请求该转发到哪个 cluster?**。回答这个问题需要两张表:一张是**路由表**(route_config),把"什么样的请求该去哪个 cluster"写成规则;另一张是**匹配器**(matcher),按这套规则在请求到来时实时匹配出结果。这张路由表本身既可以写死在配置文件里,也可以由控制面通过 RDS 动态下发——本章讲数据面这一侧的 router 怎么用这张表,不讲 RDS 怎么传(那是第 5 篇 P5-17 的事)。

> **读完本章你会明白**:
> 1. **route_config 的三层结构**——为什么是 `RouteConfiguration → VirtualHost(按域名分) → Route(按 path/header 匹配) → Cluster` 这棵树,而不是一张扁平的规则表。一层层往下剪枝,既是"一个 Envoy 服务多个域名"的现实需要,也是性能上避免对每条请求都遍历所有规则的根。
> 2. **router 是 decoder 链的终点**——为什么 router 匹配出 cluster 后,不再 `Continue` 把请求交给下一个 filter,而是把请求交给 upstream 连接池:它是 downstream 与 upstream 的衔接点。
> 3. **weighted_clusters 加权路由**——为什么"一个 route 一个 cluster"无法做金丝雀发布,以及"一条 route 指向多个 cluster 按权重拆分"这套设计怎么实现灰度。
> 4. **新老两套 matcher 并存**——老的 route matcher(domain+prefix/path/regex+header 字段)和新引入的通用 matcher tree(`MatchTree`)在当前 Envoy 源码里是怎么分工的(关键:它们不是替代关系,是按 virtual_host 二选一),以及为什么 matcher tree 更通用。

> **如果一读觉得太难**:先只记住三件事——① 路由匹配按 `域名 → path/header` 两级剪枝,先选 `VirtualHost` 再在它里面按顺序找第一条能匹配的 `Route`;② `Route` 匹配出的结果可能是一个 `cluster` 名字,也可能是 `weighted_clusters`(加权)或 `direct_response`/`redirect`(不发后端);③ `route_config` 是数据面用的,但它本身可以由 RDS 动态下发(控制面),router filter 只负责"拿着这份表匹配",不关心表从哪来。

---

## 〇、一句话点破

> **Router 做的事,本质是:拿着控制面给的 route_config(一份按域名分层的"请求→cluster"映射表),在每条 HTTP 请求到达时,按"先域名再路径"两级剪枝地匹配出该转发到哪个 cluster,然后把这个 cluster 交给 upstream 连接池——它是 downstream 与 upstream 的衔接点。**

这是结论,不是理由。本章倒过来拆:先讲"为什么不能一张扁平表匹配所有规则"(引出三层结构),再讲"router 怎么从 HCM 拿到 route、又怎么把它解析成 cluster"(源码走一遍),然后讲"加权路由怎么做金丝雀、direct_response/redirect 怎么不发后端",最后讲"新引入的 matcher tree 跟老 route matcher 到底什么关系",以及 router 作为 decoder 链终点为什么把请求交给 upstream 而不是下一个 filter。

---

## 一、承接:router 是 http filter 链的终点

上一章 P3-10 讲了 http filter chain 的两向(decoder/encoder)责任链,以及每个 filter 通过 `Continue`/`StopIteration` 决定链怎么推进。结尾留了一个钩子:**decoder 链的最后一站,通常是 `router` filter——它决定流量去哪**。本章就是把这句话拆透。

先把 router 在整条链里的位置摆清楚:

```
   HTTP 请求(downstream 来)
     │
     ▼  decoder 链(http filter chain,P3-10)
   [鉴权 jwt_authn] → [限流 ratelimit] → [故障注入 fault] → ... → [router]
                                                                   │
                                                       匹配 route_config
                                                       选出 cluster(或 weighted_clusters)
                                                                   │
                                                                   ▼
                                                       交给 upstream 连接池(P4-12)
                                                                   │
                                                                   ▼
                                                       endpoint / LB(P4-13)
```

注意这条链里 router 的特殊地位:它前面的 filter 都是在"对请求做加工/判断"(鉴权能不能过、要不要限流、要不要注入故障),而 router 是**第一个真正决定"这条请求往哪个后端发"的角色**。一旦 router 匹配出 cluster 并发起 upstream 请求,decoder 链就到此为止——它不会 `Continue` 把请求交给"下一个 filter",因为后面没有"下一个 http filter"了,有的只是 upstream 的连接池。

> **钉死这件事**:router 在 http filter chain 里有两个身份——① 它是 **decoder 链的终点**,匹配出 cluster 后把请求交给 upstream 连接池(downstream 与 upstream 的衔接点);② 它同时负责 **encoder 链上的响应处理**(把 upstream 回来的响应往下游透传,见 router.cc 的 `encodeHeaders`/`encodeData`)。本章聚焦"匹配 cluster"这一半(decode 路径),响应回传那半放在 P4-12/13 讲完连接池和 LB 再串。

---

## 二、route_config 为什么是三层树:域名 → 路径 → cluster

讲 router 怎么匹配之前,先讲清楚它手里的那张表是怎么组织的。这是本章最关键的一块。

### 2.1 朴素方案:一张扁平的规则表

最直观的路由设计是这样的:给 Envoy 一张表,每条规则是"如果请求满足什么条件,就发到哪个 cluster":

```
   朴素的扁平路由表(假设方案)
   ┌──────────────────────────────────────────────────┬───────────────┐
   │ 条件                                              │ cluster        │
   ├──────────────────────────────────────────────────┼───────────────┤
   │ host=api.example.com  path=/v1/users             │ user_service   │
   │ host=api.example.com  path=/v1/orders            │ order_service  │
   │ host=admin.example.com path=/                    │ admin_service  │
   │ host=*.example.com    path=/health               │ health_check   │
   │ ...还有几千条...                                   │ ...            │
   └──────────────────────────────────────────────────┴───────────────┘
```

听起来够用。但仔细想想,这条路径上每来一条请求,都得遍历这张表找到第一条满足的规则。问题来了:

> **不这样会怎样**:把几千条规则平铺成一张表,意味着每条请求都要从头到尾扫一遍。更糟的是,**所有规则的匹配维度被混在一起**:有的规则关心 host(域名),有的关心 path,有的关心 header——把它们平铺,你既无法利用"同一域名的规则聚在一起"这种局部性,也无法做"先把范围缩小到一类规则、再在类里细查"这种剪枝。结果就是:① 匹配慢(O(规则总数));② 规则一多就难以维护(改 api.example.com 的全部规则得在一堆里挑);③ 语义混乱(同一域名下的规则优先级难以保证)。

### 2.2 第一层剪枝:VirtualHost(按域名分)

Envoy 的回答是先把规则按**域名(VirtualHost)**分桶。一个 Envoy 进程可能同时服务多个域名(它是反向代理,这是标配场景),把同一域名的规则聚成一个 `VirtualHost`,匹配时先用请求的 `Host` 头把范围缩到一个 VirtualHost 里,再在这个 VirtualHost 内部细查:

```
   route_config(RouteConfiguration)的三层树
   ┌──────────────────────────────────────────────────────────────────┐
   │ RouteConfiguration "main_route"                                  │
   │  ├─ VirtualHost (domains: ["api.example.com"])                   │
   │  │    ├─ Route { prefix: "/v1/users"  → cluster: user_service }  │
   │  │    ├─ Route { prefix: "/v1/orders" → cluster: order_service } │
   │  │    └─ Route { prefix: "/"         → cluster: default }        │
   │  ├─ VirtualHost (domains: ["admin.example.com"])                 │
   │  │    ├─ Route { prefix: "/" → cluster: admin_service }          │
   │  └─ VirtualHost (domains: ["*.example.com"])   ← 通配符虚拟主机  │
   │       └─ Route { path: "/health" → direct_response 200 }        │
   └──────────────────────────────────────────────────────────────────┘
```

> **所以这样设计**:一个 Envoy 进程对外是个多域名的代理网关,先把规则按域名分成若干 VirtualHost,匹配时**第一步就用 Host 头把范围缩到一个 VirtualHost**(O(1) hash 查找,见后文),后续的 path/header 匹配就只在这个 VirtualHost 内部进行。这把"几千条规则的扁平扫描"降成了"一次 hash + 一个 VirtualHost 内的小扫描"。

源码里这一层组织在 `RouteMatcher` 类([RouteMatcher 声明](../envoy/source/common/router/config_impl.h#L1290-L1335)):

```cpp
// source/common/router/config_impl.h:1318-1335(简化示意,非源码原文)
class RouteMatcher {
private:
  // 精确域名 → VirtualHost(O(1) hash 查找)
  absl::flat_hash_map<std::string, VirtualHostImplSharedPtr> virtual_hosts_;
  // 通配符域名:按"通配符长度"分组,从长(更具体)到短(更宽泛)遍历
  // std::greater<> 让 map 按 key 降序排,这是"先匹配更具体通配符"的小优化
  using WildcardVirtualHosts =
      std::map<int64_t, absl::flat_hash_map<std::string, VirtualHostImplSharedPtr>,
               std::greater<>>;
  WildcardVirtualHosts wildcard_virtual_host_suffixes_;  // *.example.com
  WildcardVirtualHosts wildcard_virtual_host_prefixes_;  // foo*.example.com
  VirtualHostImplSharedPtr default_virtual_host_;        // 域名 "*" 兜底
  const bool ignore_port_in_host_matching_{false};
  const Http::LowerCaseString vhost_header_;  // 可选:换个 header 当 host(默认是 :authority)
};
```

三个数据结构把域名分了三类:**精确域名**(如 `api.example.com`)进 hash 表,O(1) 查;**通配符域名**(如 `*.example.com`、`foo*.example.com`)按"通配符去掉 `*` 后的字符串长度"分桶、按长度**降序**排(长的更具体,先匹配);**兜底 `*`** 单独一个 `default_virtual_host_`。这是个细节优化——通配符匹配时按长度降序,保证 `*-bar.baz.com` 比 `*.baz.com` 先命中(最长通配符优先)。

> **技巧(为什么通配符按长度分桶)**:通配符域名无法用一张 hash 表直接查(`*.example.com` 这种 key 没法 hash),朴素做法是遍历所有通配符挨个试匹配——O(通配符数)。Envoy 用了个巧妙办法:把通配符按"去掉 `*` 的部分"分桶,key 是这个部分的**长度**,桶里再 hash 这个部分。匹配时从最长桶开始,看请求 host 的对应子串能不能 hash 命中桶里的某条。源码注释([config_impl.h#L1320-L1328](../envoy/source/common/router/config_impl.h#L1320-L1328))还诚实交代了性能权衡:本地 benchmark 显示,通配符少(<=4)时 vector 比 unordered_map 快(每条 vector 项约 20ns,空 unordered_map 起步就要 65ns),所以理论上少于 4 个通配符时 hash 表反而是浪费——但 Envoy 选了 hash 表,因为它假设通配符通常较多,且代码统一性更重要。

`findVirtualHost`([config_impl.cc#L1995-L2051](../envoy/source/common/router/config_impl.cc#L1995-L2051))就是这个分桶查找的实现:先 fast path(只有 default vhost 时直接返回),否则拿 Host 头、小写化、依次查精确表 → 后缀通配符表 → 前缀通配符表 → default。每一步命中就返回。

### 2.3 第二层:Route(按 path/header 匹配)

选到 VirtualHost 之后,在该 VirtualHost 内部按 **route 列表的顺序**逐条匹配。每条 route 长这样(精简的 proto 语义):

```
   Route {
     match: {
       prefix | path | path_separator_prefix | safe_regex  // 四选一:路径匹配
       headers: [...]            // header 匹配
       query_parameters: [...]   // query 参数匹配
       cookies: [...]            // cookie 匹配
       runtime_fraction: ...     // 按比例匹配(灰度用)
       ...
     }
     route: {
       cluster: "xxx"                    // 普通转发
       weighted_clusters: {...}          // 加权转发(金丝雀)
       cluster_header: "x-cluster"       // 从请求头读 cluster 名
       host_rewrite: ...
       timeout: ...
       retry_policy: ...
       ...
     }
     | redirect: {...}         // 重定向
     | direct_response: {...}  // 直接响应(不发后端)
   }
```

匹配路径分四种 `RouteEntryImplBase` 子类([config_impl.h#L1061-L1230](../envoy/source/common/router/config_impl.h#L1061-L1230)):

| 子类 | 触发条件 | 典型场景 |
|------|---------|---------|
| `PrefixRouteEntryImpl` | `match.prefix` | `/api/*` 这类前缀匹配,最常用 |
| `PathRouteEntryImpl` | `match.path` | 精确路径 `/health` |
| `PathSeparatedPrefixRouteEntryImpl` | `match.path_separated_prefix` | `/api` 匹配 `/api` 和 `/api/...` 但不匹配 `/apiFoo`(避免前缀误匹配) |
| `RegexRouteEntryImpl` | `match.safe_regex` | 正则匹配,最灵活但最慢 |
| `ConnectRouteEntryImpl` | CONNECT 方法特殊处理 | HTTP CONNECT(隧道) |
| `UriTemplateMatcherRouteEntryImpl` | `match.path_template` | `/api/{version}/users` 这类模板 |

每个子类的 `matches()` 方法负责判断路径能不能命中(比如 `PrefixRouteEntryImpl::matches` 就调 `path_matcher_->match(sanitizedPath)`)。路径命中后还要过一道 `RouteEntryImplBase::matchRoute()`([config_impl.cc#L864-L919](../envoy/source/common/router/config_impl.cc#L864-L919)),它负责路径之外的**所有其它维度**匹配:

```cpp
// source/common/router/config_impl.cc:864(简化示意,非源码原文)
bool RouteEntryImplBase::matchRoute(const RouteMatchContext& ctx,
                                    const StreamInfo::StreamInfo& stream_info,
                                    uint64_t random_value) const {
  bool matches = true;
  matches &= evaluateRuntimeMatch(random_value);    // runtime_fraction 按比例放行
  if (match_grpc_ && !ctx.isGrpc()) return false;   // gRPC 专属 route
  matches &= Http::HeaderUtility::matchHeaders(headers, config_headers_);  // header 匹配
  matches &= ConfigUtility::matchQueryParams(...);                        // query 参数
  matches &= ConfigUtility::matchCookies(...);                            // cookie
  matches &= evaluateTlsContextMatch(stream_info);                        // TLS 上下文
  for (const auto& m : dynamic_metadata_) matches &= m.match(...);        // 动态元数据
  for (const auto& m : filter_state_)      matches &= m.match(...);        // filter state
  return matches;
}
```

注意一个细节:**短路返回**。每个维度匹配失败就立即 `return false`,不浪费后续计算。`matchRoute` 是所有路径之外匹配维度的统一入口,六个 `RouteEntryImplBase` 子类各各在自己的 `matches()` 里先调路径匹配、再调这个 `matchRoute`。

> **钉死这件事(顺序匹配的契约)**:VirtualHost 内的 route 是**按配置顺序逐条匹配,第一条命中的胜出**。这不是 incidental,而是 Envoy 的明确语义——所以配置时要把**更具体的规则放前面**,把兜底规则(如 `prefix: "/"`)放最后。源码 `getRouteFromRoutes`([config_impl.cc#L1818-L1854](../envoy/source/common/router/config_impl.cc#L1818-L1854))就是一个 for 循环,从头到尾挨个 `(*route)->matches(...)`,第一个返回非空的就 return。这个顺序契约,跟 Nginx `location` 的"最长前缀优先"是**两套不同的设计哲学**(Nginx 倾向自动算优先级,Envoy 让用户显式排顺序,语义更可控)。

### 2.4 整棵树的匹配入口:`RouteMatcher::route()`

把三层串起来,一条请求的路由匹配完整路径是:

```mermaid
sequenceDiagram
    participant HF as http filter chain
    participant HCM as HCM ActiveStream
    participant SRC as snapped_route_config_<br/>(ConfigImpl)
    participant RM as RouteMatcher
    participant VH as VirtualHostImpl
    participant RE as RouteEntryImplBase(matches)
    participant CS as clusterEntry() / cluster_specifier_plugin
    participant RF as router filter<br/>(decodeHeaders)

    HF->>HCM: 解码完 header,触发 route 计算
    HCM->>SRC: route(cb, headers, stream_info, random)
    SRC->>RM: route(cb, headers, ...)
    RM->>RM: findVirtualHost(headers)<br/>(精确→通配符→default)
    RM->>VH: virtual_host->getRouteFromEntries(...)
    VH->>VH: (若 matcher_ 则走 matcher tree,<br/>否则遍历 routes_)
    loop 逐条 route 按顺序
        VH->>RE: route->matches(ctx, ...)
        RE->>RE: path 匹配 + matchRoute(headers/query/...)
        alt 命中
            RE->>CS: clusterEntry() 选 cluster<br/>(或 weighted_clusters 加权)
            CS-->>VH: RouteConstSharedPtr
        end
    end
    VH-->>RM: 命中的 route(或 nullptr)
    RM-->>SRC: VirtualHostRoute{vhost, route}
    SRC-->>HCM: VirtualHostRoute
    HCM-->>HF: cached_route_ = route
    HF->>RF: 调 router filter 的 decodeHeaders
    RF->>HCM: callbacks_->routeSharedPtr() 拿刚才算出的 route
    RF->>RF: 若 route 为空 → 404;有 direct_response → 直接响应;<br/>否则取 route_entry_->clusterName() 查 cluster
```

关键澄清一处容易混的点:**匹配 route 的不是 router filter 自己**,而是 HCM。HCM 在解码完 header 后会主动算一遍 route 并缓存(给前面那些可能也想看 route 的 filter 用,如 `jwt_authn` 想按 vhost/route 配鉴权策略),router filter 是通过 `callbacks_->routeSharedPtr()` 把这个**已经算好**的 route 取出来用。这个分工很重要——它解释了为什么"配错了 route 404"是 HCM 层面的事,而不是 router filter 的事。

源码入口在 HCM 的 `ActiveStream`([conn_manager_impl.cc#L1818-L1832](../envoy/source/common/http/conn_manager_impl.cc#L1818-L1832)):

```cpp
// source/common/http/conn_manager_impl.cc:1818(简化示意,非源码原文)
Router::VirtualHostRoute route_result;
if (request_headers_ != nullptr) {
  ...
  if (snapped_route_config_ != nullptr) {
    route_result = snapped_route_config_->route(cb, *request_headers_,
                                                filter_manager_.streamInfo(), stream_id_);
  }
}
setVirtualHostRoute(std::move(route_result));
```

这里 `snapped_route_config_` 是 thread-local 的 route_config 快照(控制面通过 RDS 推下来的那份,通过 thread-local 机制每个 worker 拿到自己的无锁副本——这是 P1-02 讲过的"thread-local 无锁"在 route_config 上的应用,本章不展开)。`stream_id_` 在这里被当作 `random_value` 传进去——这是 weighted_clusters 加权选择和 runtime_fraction 按比例放行的随机源。

---

## 三、router filter 的 `decodeHeaders`:拿到 route 之后干什么

现在 route 已经匹配好了(在 HCM 那一侧),看 router filter 自己拿到这个 route 之后做什么。核心入口是 `Filter::decodeHeaders`([router.cc#L477-L640](../envoy/source/common/router/router.cc#L477-L640))。我把它的主干抽出来:

```cpp
// source/common/router/router.cc:477(简化示意,非源码原文,保留关键分支)
Http::FilterHeadersStatus Filter::decodeHeaders(Http::RequestHeaderMap& headers, bool end_stream) {
  ...
  // 1) 拿 HCM 算好的 route(不是 router 自己匹配的)
  route_ = callbacks_->routeSharedPtr();

  // 2) 没匹配到 route → 404
  if (!route_) {
    stats_.no_route_.inc();
    callbacks_->sendLocalReply(Http::Code::NotFound, "", ...,
                               StreamInfo::ResponseCodeDetails::get().RouteNotFound);
    return Http::FilterHeadersStatus::StopIteration;
  }

  // 3) 命中的是 direct_response → 直接构造响应返回,不发后端
  const auto* direct_response = route_->directResponseEntry();
  if (direct_response != nullptr) {
    stats_.rq_direct_response_.inc();
    callbacks_->sendLocalReply(direct_response->responseCode(), ...);
    return Http::FilterHeadersStatus::StopIteration;
  }

  // 4) 正常转发:取 cluster 名,从 ClusterManager 查 cluster
  route_entry_ = route_->routeEntry();
  Upstream::ThreadLocalCluster* cluster =
      config_->cm_.getThreadLocalCluster(route_entry_->clusterName());

  // 5) cluster 不存在 → 用 clusterNotFoundResponseCode 返回
  if (!cluster) {
    stats_.no_cluster_.inc();
    callbacks_->sendLocalReply(route_entry_->clusterNotFoundResponseCode(), ...,
                               StreamInfo::ResponseCodeDetails::get().ClusterNotFound);
    return Http::FilterHeadersStatus::StopIteration;
  }
  cluster_ = cluster->info();

  // 6) 维护模式 / 过载丢弃等检查
  if (cluster_->maintenanceMode()) { ... return StopIteration; }
  if (checkDropOverload(*cluster)) { return StopIteration; }

  // 7) 一切就绪,把 cluster 交给连接池发起 upstream 请求(后续章节)
  ...
  return Http::FilterHeadersStatus::StopIteration;  // 注意:不再 Continue
}
```

这段代码藏着 router 设计的几条关键决定,逐条拆。

### 3.1 没有 route → 404(不是 502,不是 503)

如果 HCM 没匹配到 route,router 直接返回 **404 NotFound**,而不是常见的 5xx。这是个有意的语义选择:**404 表示"我不知道这条请求该去哪"**(路由配置层面的问题),而 5xx 通常暗示"后端有问题"。源码里 `ResponseCodeDetails` 是 `RouteFound`(详细字符串 `"route_not_found"`)。

> **不这样会怎样**:如果"没匹配到 route"返回 503,那它就和"cluster 存在但所有 endpoint 都挂了"的 503 混在一起,排障时分不清是路由配错了还是后端真挂了。404 把"路由层"的错误从"后端层"的错误切干净,排障时一眼能看出是配 route 的问题。这是个细小但重要的运维语义。

### 3.2 direct_response / redirect:不发后端,就地响应

`route_->directResponseEntry()` 不为空,说明这条 route 配的是 `direct_response`(直接返回一个固定响应)或 `redirect`(重定向)。这种 route **不转发到任何 cluster**,router 直接构造响应返回。

这两者最常见的用途:

- **direct_response**:健康检查端点(`/healthz` 直接返回 200)、维护页面(`/` 直接返回 503 加 HTML 说明)、灰度切流的兜底。
- **redirect**:HTTP→HTTPS 强制跳转(见前文 `ssl_requirements_` 触发的 `ssl_redirect_route_`,VirtualHost 配 `require_tls: ALL` 时,非 HTTPS 请求直接被重定向到 HTTPS)、域名跳转、路径规范化。

源码里这两者都走 `direct_response` 分支(同一个 `DirectResponseEntryImpl` 类,redirect 是 direct_response 的一个特例——通过响应码 301/302/303/307/308 + `Location` 头实现)。`sendLocalReply` 构造响应时,会调 `direct_response->newUri(...)` 算出 `Location` 头的值(redirect 情况),然后塞进响应头里。

> **钉死这件事**:direct_response / redirect 是 **route 层的短路**,它们让"不该发后端的流量"在 router filter 这一层就终止,根本不进 upstream 路径,既省了上游带宽又干净。这跟 router 前面那些 http filter 的 `StopIteration`(P3-10 讲的鉴权拦截 401)是同一类"链里中途截断"的机制,只是触发点不同——前面是 filter 自己决定截断,这里是 route 配置决定截断。

### 3.3 cluster 不存在 → `clusterNotFoundResponseCode`

route 匹配成功、取出 cluster 名字后,router 还要从 ClusterManager 查这个 cluster 到底存不存在。如果不存在(配置写了不存在的 cluster 名,或 RDS 和 CDS 不同步导致 route 引用的 cluster 还没创建出来),用 `route_entry_->clusterNotFoundResponseCode()` 配置的响应码返回(默认 503,可配)。

> **不这样会怎样**:如果不在 router 这一层检查 cluster 存在性、直接把请求扔给连接池,连接池会面临一个"cluster 不存在"的内部错误,处理路径复杂化(异常跨多层)。router 在这里提前 fail-fast,既清晰又有统一的错误统计(`stats_.no_cluster_.inc()`)。这也是数据面与控制面同步时序的一个出口——RDS 推的新 route 引用的 cluster 还没被 CDS 推过来,这里能体面地降级而不是 panic。

### 3.4 一切就绪 → 交给 upstream,不再 Continue

检查全过后,router 进入真正的转发流程:从 `cluster_->info()` 拿连接池、发起 upstream 请求(具体在 `upstream_request_` 上调 `encodeHeaders`/`encodeData`,这些在 P4-12 连接池一章细讲)。**此时 router 返回的是 `StopIteration` 而不是 `Continue`**——这是它作为 decoder 链终点的标志。

> **钉死这件事(router 是 decoder 链终点的根)**:router 返回 `StopIteration`,意味着 HCM 不再往 decoder 链后面传(后面也没有下一个 http filter 了,router 就是终点)。请求从此刻起进入 upstream 域——router 通过它持有的 upstream 连接池句柄,把这条 downstream 流"嫁接"到一条 upstream 流上。这是 router 作为 **downstream 与 upstream 衔接点**的本质体现。具体的 upstream 请求发起、连接池复用、负载均衡挑 endpoint,是 P4-12/P4-13 的内容,本章只到"router 把 cluster 名解析成 cluster、把请求交给 cm(cluster manager)"为止。

---

## 四、weighted_clusters:加权路由怎么做金丝雀

讲完主干,挑两个最值得拆的设计做技巧精解。第一个就是 **weighted_clusters**——一条 route 指向多个 cluster 按权重拆分流量,这是金丝雀发布(灰度)的核心机制。

### 4.1 朴素方案:一个 route 一个 cluster,为什么不行

最自然的 route 设计是"一条 route → 一个 cluster"。这够用直到你要做**金丝雀发布**:新版 `v2` 上线了,你想先放 5% 流量到 v2 集群、95% 留在 v1 集群观察,有问题立刻切回。如果一条 route 只能指向一个 cluster,怎么做?

> **不这样会怎样**:朴素方案下只有两个糟糕选择——① 给 v2 单独配一个新域名/vhost,让一部分客户端改域名指向 v2(客户端要改代码,违反"灰度对客户端透明");② 在 router 外面套一个上层 LB 做流量拆分(把 Envoy 退化回 dumb proxy,失去了 route 这一层做灰度的能力)。两者都不能让你**在配置里一句话说"这条 route 95% 去 v1、5% 去 v2"**,而这恰恰是微服务灰度发布最需要的能力。

### 4.2 加权路由的实现:`WeightedClusterSpecifierPlugin`

Envoy 的设计:允许一条 route 同时指向**多个 cluster**,每个 cluster 带一个权重:

```yaml
# 语义示例(非源码原文)
route_match:
  prefix: "/api"
route:
  weighted_clusters:
    clusters:
      - { name: v1_cluster, weight: 95 }
      - { name: v2_cluster, weight: 5 }
```

这条 route 命中后,router 会按 95:5 的比例随机地把请求分到 v1 或 v2。这里有个设计细节很值得注意——**`weighted_clusters` 不是 route 的一个独立字段,而是被实现成一种 `cluster_specifier_plugin`**。看源码 [config_impl.cc#L633-L651](../envoy/source/common/router/config_impl.cc#L633-L651):

```cpp
// source/common/router/config_impl.cc:633(简化示意,非源码原文)
if (route.route().has_weighted_clusters()) {
  cluster_specifier_plugin_ = std::make_shared<WeightedClusterSpecifierPlugin>(
      route.route().weighted_clusters(), metadata_match_criteria_.get(), route_name_,
      factory_context, creation_status);
} else if (route.route().has_inline_cluster_specifier_plugin()) {
  ...
} else if (route.route().has_cluster_specifier_plugin()) {
  ...
} else if (route.route().has_cluster_header()) {
  cluster_specifier_plugin_ = std::make_shared<HeaderClusterSpecifierPlugin>(...);
}
```

route 决定"cluster 名怎么来"有四种方式,它们被统一抽象成 `ClusterSpecifierPlugin` 接口:**①** 普通 `cluster` 字段(默认,plugin 为空);**②** `weighted_clusters`(加权);**③** `inline_cluster_specifier_plugin` / `cluster_specifier_plugin`(插件化的 cluster 选择);**④** `cluster_header`(从请求头读 cluster 名,动态指定)。把它们都抽象成 plugin 接口的好处是——`clusterEntry()` 调用时统一走 `cluster_specifier_plugin_->route(...)`([config_impl.cc#L1339-L1346](../envoy/source/common/router/config_impl.cc#L1339-L1346)):

```cpp
// source/common/router/config_impl.cc:1339(简化示意,非源码原文)
RouteConstSharedPtr RouteEntryImplBase::clusterEntry(const Http::RequestHeaderMap& headers,
                                                     const StreamInfo::StreamInfo& stream_info,
                                                     uint64_t random_value) const {
  if (cluster_specifier_plugin_ != nullptr) {
    return cluster_specifier_plugin_->route(shared_from_this(), headers, stream_info, random_value);
  }
  return shared_from_this();  // 普通 cluster,自己就是 route entry
}
```

> **技巧(为什么把 weighted_clusters 包成 plugin)**:朴素写法会在 `clusterEntry()` 里塞一堆 `if (has_weighted_clusters) ... else if (has_cluster_header) ...` 分支,route entry 的核心逻辑(路径匹配、header 改写、retry 策略)被这些"cluster 怎么选"的细节污染。把"cluster 怎么选"抽成 `ClusterSpecifierPlugin` 接口,route entry 只管"路径匹配完调一下 plugin",具体怎么选 cluster 由 plugin 各各实现——这是经典的**策略模式**,把"变化的维度"(cluster 选择方式)从"稳定的维度"(route 匹配与请求改写)里剥出来。后续要加新的 cluster 选择方式(比如未来基于 metadata 的动态路由),只需新加一个 plugin,不动 route entry 主干。

### 4.3 加权选择的算法:区间落点法

看 `WeightedClusterSpecifierPlugin::route()`([weighted_cluster_specifier.cc#L338-L401](../envoy/source/common/router/weighted_cluster_specifier.cc#L338-L401))怎么按权重选 cluster。核心是 `pickClusterIndex`([weighted_cluster_specifier.cc#L24-L73](../envoy/source/common/router/weighted_cluster_specifier.cc#L24-L73)):

```cpp
// source/common/router/weighted_cluster_specifier.cc:24(简化示意,非源码原文)
template <class T>
absl::optional<size_t> pickClusterIndex(absl::Span<T> weighed_clusters, uint64_t random_value,
                                        uint64_t total_cluster_weight, Runtime::Loader& loader) {
  // 如果总权重没缓存(runtime 可改权重时不能缓存),先求和
  const bool need_recompute_total_weight = total_cluster_weight == 0;
  absl::InlinedVector<uint32_t, 4> cluster_weights;
  if (need_recompute_total_weight) {
    for (const auto& cluster : weighed_clusters) {
      total_cluster_weight += cluster->clusterWeight(loader);
      ...
    }
  }
  // 把 random_value 投影到 [0, total_weight) 区间,看落在哪个子区间
  const uint64_t selected_value = random_value % total_cluster_weight;
  uint64_t begin = 0, end = 0;
  // 区间排布:[0, w1), [w1, w1+w2), [w1+w2, w1+w2+w3), ...
  for (size_t i = 0; i < weighed_clusters.size(); ++i) {
    end = begin + weighed_clusters[i]->clusterWeight(loader);
    if (selected_value >= begin && selected_value < end) {
      return weighed_clusters[i]->clusterIndex();
    }
    begin = end;
  }
  ...
}
```

算法是经典的**区间落点法**:

```
   假设 v1 权重 95、v2 权重 5,total_weight=100
   把 [0, 100) 区间分成两段:
   ┌────────────────────────────────────────┬─────┐
   │  v1_cluster:  [0, 95)                  │ v2  │
   │                                         │[95, │
   │                                         │100) │
   └────────────────────────────────────────┴─────┘
   random_value % 100 投到这个数轴上,落在哪段就去哪个 cluster
   95% 的概率落在 v1,5% 的概率落在 v2
```

`random_value % total_weight` 的结果会**均匀地**落在 `[0, total_weight)` 上(因为模运算),所以每个 cluster 的命中率正比于它的权重——这就是加权随机的数学保证。这个算法的妙处在于:**不需要预生成一个长度=total_weight 的数组再随机索引**(那种做法空间 O(total_weight),权重一大就爆),而是直接在原 cluster 列表上线性扫描——空间 O(cluster 数),时间也是 O(cluster 数)。

> **技巧(为什么不用数组+随机索引)**:朴素做法是"造一个长度 100 的数组,前 95 格填 v1、后 5 格填 v2,随机取一格"。这有两个问题:① 权重总和无约束(可能上百万),数组会爆内存;② 改权重(runtime 动态调整)要重建整个数组。区间落点法用纯算术把同样的语义实现成 O(cluster 数)——空间和权重大小无关,改权重也不需要重建任何结构(只是 `clusterWeight()` 的返回值变了)。这是用算法替代数据结构的典型节省。

`random_value` 从哪来?看 `WeightedClusterSpecifierPlugin::route()` 的三段([weighted_cluster_specifier.cc#L342-L381](../envoy/source/common/router/weighted_cluster_specifier.cc#L342-L381)):① 优先从配置的 header 读(`header_name_` 字段);② 否则若开了 `use_hash_policy`,从请求算 hash(让同一请求每次都路由到同一 cluster,**粘性灰度**);③ 都没有就用 HCM 传进来的 `stream_id_`(每请求一个随机数)。这三个层次各有用途:header 模式让客户端能控制灰度归属,hash 模式让灰度粘性(同一用户总看到同一版本),随机模式最均匀。

### 4.4 总权重缓存的小优化

最后看一个性能细节([weighted_cluster_specifier.cc#L166-L171](../envoy/source/common/router/weighted_cluster_specifier.cc#L166-L171)):

```cpp
// 如果没配 runtime_key_prefix,权重是静态的,总权重可以缓存
if (runtime_key_prefix.empty()) {
  total_cluster_weight_ = total_cluster_weight;
}
```

如果配了 `runtime_key_prefix`,每个 cluster 的权重可以**通过 runtime 在运行时动态改**(不用 reload 配置)。这种情况下总权重无法缓存(改了某个 cluster 的权重总和就变了),`pickClusterIndex` 每次都得重算求和(`need_recompute_total_weight = true`)。没配时缓存总权重,跳过每次的求和循环——一个小但实在的优化。

> **钉死这件事**:weighted_clusters 是 Envoy 路由层做金丝雀/灰度发布的根。它的设计有三层精妙:① 抽成 `ClusterSpecifierPlugin`,不污染 route entry 主干;② 区间落点法选 cluster,空间 O(cluster 数)不爆内存;③ 三档随机源(header/hash/stream_id),覆盖均匀灰度、粘性灰度、客户端指定三种场景。

---

## 五、技巧精解二:新 matcher tree vs 老 route matcher,到底什么关系

本章第二个最值得拆的,是 Envoy 路由匹配里**两套 matcher 并存**这件事。这是写作前最容易翻车的点——老资料(以及不少博客)要么只讲老的 route matcher,要么笼统说"matcher tree 替代了 route matcher",都不准确。源码为准。

### 5.1 老 route matcher 的局限

前面第二节讲的三层树(`RouteConfiguration → VirtualHost → Route → Cluster`),其中 VirtualHost 内部的 Route 匹配是**固定的几维**:`prefix`/`path`/`regex`/`path_template` 选一个做路径匹配,再叠加 `headers`/`query_parameters`/`cookies`/`runtime_fraction`/`tls_context`/动态 metadata/filter_state 这几个维度。

这个模型覆盖了 99% 的常见路由需求,但有局限:**匹配维度是写死在代码里的**。你想用一个新的维度(比如基于请求 body 里的某个字段、或基于一条外部规则表)来分流,没法在 route 配置里表达,得自己写 filter。

更本质的问题是:route matcher 只会输出 "route" 这一种结果(一个 cluster 或 weighted_clusters/direct_response)。如果你想要"匹配出某种结果后,触发一套自定义逻辑"(比如匹配出某个 metadata tag,后续 filter 据此决策),老 route matcher 表达不了。

### 5.2 新的通用 matcher tree(`MatchTree`)

Envoy 后来引入了一套通用的 **matcher tree** API(在 `source/common/matcher/`),它的设计哲学完全不同:**匹配什么、输出什么,都由用户组合**。它的核心抽象是 `MatchTree<DataType>`([matcher.h#L41-L44](../envoy/source/common/matcher/matcher.h#L41-L44)):

```cpp
// source/common/matcher/matcher.h:41(简化示意,非源码原文)
template <class DataType>
static inline ActionMatchResult evaluateMatch(MatchTree<DataType>& match_tree,
                                              const DataType& data,
                                              SkippedMatchCb skipped_match_cb = nullptr) {
  return match_tree.match(data, skipped_match_cb);
}
```

它是一个**泛型**匹配树:`DataType` 是"被匹配的数据"(比如 HTTP 请求、TCP 连接 metadata、任意自定义类型);树由若干节点组成,每个节点有一个 `DataInput`(从 data 里取一个字段)、一个 `FieldMatcher`(判断这个字段满不满足条件)、一个 `OnMatch`(满足时怎么办——可以是递归到子树,也可以是触发一个 Action)。匹配结果是 `ActionMatchResult`(命中的 action 集合)。

这套设计的通用性体现在:

- **输入任意**:可以是 HTTP 请求头、TCP metadata,也可以是任何实现了 `DataType` 接口的东西。`DataInput` 是可扩展的(通过 factory 注册新输入类型)。
- **输出任意**:匹配命中的结果不是"cluster",而是 `Action`——Action 是一个抽象类型,具体语义由 Action factory 定义(可以是"路由到某 cluster",也可以是"打个标签"等任意动作)。
- **嵌套组合**:`OnMatch` 可以递归指向另一棵子树,任意深的嵌套匹配都能表达。

### 5.3 关键澄清:当前 route 匹配主路径是什么

写作前最容易翻车的判断:**route 现在到底用不用 matcher tree?** 我用 Grep 核了源码,答案是——**两套并存,按 VirtualHost 二选一,不是替代关系**。证据在 `VirtualHostImpl::getRouteFromEntries`([config_impl.cc#L1884-L1912](../envoy/source/common/router/config_impl.cc#L1884-L1912)):

```cpp
// source/common/router/config_impl.cc:1884(简化示意,非源码原文)
if (matcher_) {
  // 路径 A:VirtualHost 配的是新 matcher tree
  Http::Matching::HttpMatchingDataImpl data(stream_info);
  data.onRequestHeaders(headers);
  Matcher::ActionMatchResult match_result =
      Matcher::evaluateMatch<Http::HttpMatchingData>(*matcher_, data);
  if (match_result.isMatch()) {
    const auto result = match_result.actionByMove();
    if (result->typeUrl() == RouteMatchAction::staticTypeUrl()) {
      // 命中的 action 是一个 Route
      return getRouteFromRoutes(cb, route_match_context, stream_info, random_value,
                                {std::dynamic_pointer_cast<const RouteEntryImplBase>(
                                    std::move(result))});
    } else if (result->typeUrl() == RouteListMatchAction::staticTypeUrl()) {
      // 命中的 action 是一组 Route(RouteList)
      const RouteListMatchAction& action = result->getTyped<RouteListMatchAction>();
      return getRouteFromRoutes(cb, route_match_context, stream_info, random_value,
                                action.routes());
    }
    PANIC("Action in router matcher should be Route or RouteList");
  }
  return nullptr;
}
// 路径 B:VirtualHost 配的是老 routes 列表
return getRouteFromRoutes(cb, route_match_context, stream_info, random_value, routes_);
```

而且 `matcher_` 和 `routes_` 是**互斥**的——VirtualHost 的 proto 里要么配 `matcher`、要么配 `routes`,不能同时配。证据在 `CommonVirtualHostImpl` 构造函数([config_impl.cc#L1667-L1671](../envoy/source/common/router/config_impl.cc#L1667-L1671)):

```cpp
// source/common/router/config_impl.cc:1667(原文逐字)
if (virtual_host.has_matcher() && !virtual_host.routes().empty()) {
  creation_status =
      absl::InvalidArgumentError("cannot set both matcher and routes on virtual host");
  return;
}
```

> **钉死这件事(纠正"matcher tree 替代了 route matcher"的说法)**:当前 master(`df2c77d`,1.39.0-dev)的路由匹配是**新老并存**——每个 VirtualHost 二选一,配了 `matcher` 字段就走 matcher tree 路径(路径 A),配了 `routes` 字段就走老的 route 列表路径(路径 B)。**老 route matcher 没有被废弃**,绝大多数现存配置(包括 Istio 生成的)都还在用 `routes` 字段,因为常见的 prefix/path/header 匹配用老 API 更直观、配置更短。matcher tree 主要用于需要复杂匹配逻辑(嵌套、自定义输入、非 cluster 的 action)的场景,比如基于 xDS 的细粒度匹配策略。把它们说成"替代关系"是过度简化,源码事实是"并存 + 互斥"。

为什么 matcher tree 路径 A 最终也调 `getRouteFromRoutes`?因为 matcher tree 命中后产出的 action 仍然是 `RouteMatchAction`(包装一个 `RouteEntryImplBase`)或 `RouteListMatchAction`(包装一组 Route)——**底层 RouteEntry 还是同一套**。matcher tree 替换的是"如何在 VirtualHost 内部找到候选 route"这一层(从"按顺序遍历 routes 列表"换成"在 matcher tree 上递归匹配"),但找到的 route 对象本身、以及 route 里的 cluster 选择逻辑(weighted_clusters 等),完全复用老路径。

```
   两套 matcher 的分工(当前源码事实)
   ┌──────────────────────────────────────────────────────────────┐
   │ RouteConfiguration                                           │
   │  └─ VirtualHost(domains: ["api.example.com"])               │
   │       ┌─────────────────────────┬─────────────────────────┐  │
   │       │ 互斥二选一               │                         │  │
   │       │                         │                         │  │
   │       │ 路径 B: routes 字段(老)  │ 路径 A: matcher 字段(新)│  │
   │       │  按 route 顺序遍历       │  MatchTree 递归匹配      │  │
   │       │  PrefixRouteEntry/      │  命中 → Action           │  │
   │       │  PathRouteEntry/...     │   (Route 或 RouteList)   │  │
   │       │   ↓                     │   ↓                      │  │
   │       │   都产出 RouteEntryImplBase 对象 ←─ 同一套 ──┘  │
   │       │   ↓                                                │
   │       └─→ getRouteFromRoutes() 统一处理                    │
   │           ↓                                                │
   │       RouteEntryImplBase::matches() + matchRoute()         │
   │           ↓                                                │
   │       clusterEntry() → cluster / weighted_clusters / ...   │
   └──────────────────────────────────────────────────────────────┘
```

> **为什么 Envoy 引入 matcher tree 而不是增强老 route matcher**:老 route matcher 的匹配维度是硬编码在 C++ 代码里的(prefix/path/header/...),加新维度要改 Envoy 源码。matcher tree 把"匹配什么、输出什么"完全数据化(由 proto 配置组合 `DataInput` + `FieldMatcher` + `Action`),用户不用改代码就能表达新匹配逻辑——这是把"路由匹配"从一个写死的功能,泛化成一个**通用的可编程匹配引擎**。代价是配置复杂度上升,所以常见场景老 route matcher 仍是首选。两套并存体现了 Envoy 的演进策略:**新能力以并行方式引入,老能力保留过渡**,不强制一刀切迁移(这跟 dynamic_modules vs Wasm 的并存是同一套思路,见 P6-22)。

---

## 六、route_config 与 RDS:控制面与数据面的衔接

本章讲的全是数据面的事——router filter 怎么用一份 route_config 做匹配。但这份 route_config **本身从哪来**,值得一节做衔接(不展开,详细在 P5-17)。

route_config 有三种来源,对应三个 provider:

| 来源 | Provider | 场景 |
|------|----------|------|
| 静态文件 | `StaticRouteConfigProviderImpl` | 配置写死在 bootstrap YAML 里 |
| RDS(动态) | `RdsRouteConfigProviderImpl` | 控制面通过 RDS xDS 动态下发 |
| VHDS(分层) | (按 VirtualHost 独立订阅) | vhost 数量极大,按需订阅 |

HCM 在初始化时拿一个 `RouteConfigProvider`,从它取当前的 `Config`(即 `ConfigImpl` 或类似实现)。这个 `Config` 是 thread-local 快照——控制面推一份新 route_config 下来,Envoy 在 main 线程解析后,**为每个 worker 生成一份 thread-local 副本**,worker 处理请求时读自己本地那份,完全无锁。这就是前面看到的 `snapped_route_config_` 的来源(它的名字就暗示了"快照")。

> **钉死这件事(本章是数据面、RDS 是控制面)**:本章只讲"router 拿到一份 route_config 之后怎么匹配"。至于"这份 route_config 怎么从控制面通过 RDS 推过来、怎么版本协商、怎么热更新替换 worker 的 thread-local 快照",全是 **P5-17 xDS 订阅与传输**的内容(承接《gRPC》P6-22 那本)。RDS 是五种 xDS(Listener/Route/Cluster/Endpoint/Secret)之一,本章是它的数据面消费端。

控制面与数据面的衔接点,就在 `snapped_route_config_` 这个 thread-local 指针上:控制面动它(写新版本),数据面读它(每请求查匹配),两边通过 thread-local 机制无锁交接。这个设计让"灰度切流"这种运维操作变成"控制面推一份新 route_config,所有 worker 秒级热更新",而不是 Nginx 那种"改配置文件 + reload"。

---

## 七、章末小结

### 回扣主线

本章是数据面这一侧。它讲了 http filter chain 上的终点 filter——router——怎么完成"决定流量去哪"这最后一跳。涉及的几个核心点都落在数据面:① route_config 的三层树(VirtualHost → Route → Cluster);② 匹配算法(精确 hash + 通配符分桶 + route 顺序匹配 + 加权随机);③ 新老两套 matcher 并存。

但本章同时埋了一个控制面的钩子:route_config 本身可由 RDS 动态下发。router filter 只负责"拿着这份表匹配",不关心表从哪来——这是数据面与控制面干净分离的体现。下一章 P4-12 进入 upstream 域,讲 router 选出的那个 cluster 怎么定义、里面的 endpoint 怎么发现、连接池怎么复用。

### 五个为什么

1. **为什么 route_config 是三层树(VirtualHost → Route → Cluster)而不是扁平表?** —— 一个 Envoy 服务多域名是标配,按域名先分桶把范围缩小到一个 VirtualHost(O(1) hash 查找),再在 VirtualHost 内按 route 顺序匹配,避免几千条规则扁平扫描;同时按域名分组也让规则维护更清晰(改一个域名的规则不动其它)。
2. **为什么 router 是 decoder 链的终点?** —— 它是第一个决定"流量去哪个后端"的角色,匹配出 cluster 后请求进入 upstream 域(交给连接池),没有"下一个 http filter"可 Continue 了。它返回 `StopIteration` 是 decoder 链终止的标志。
3. **为什么 weighted_clusters 要被实现成 `ClusterSpecifierPlugin`?** —— 把"cluster 怎么选"这个变化的维度(普通 cluster/加权/header 动态/插件)从"route 匹配与请求改写"这个稳定维度里剥出来,避免 route entry 主干被各种 if/else 分支污染(策略模式)。新加 cluster 选择方式只需新加 plugin。
4. **为什么加权选择用区间落点法而不是数组+随机索引?** —— 数组方案空间 O(总权重),权重总和大时爆内存,改权重要重建数组;区间落点法纯算术,空间 O(cluster 数),改权重无需重建任何结构(只是权重返回值变了)。用算法替代数据结构节省空间和更新成本。
5. **为什么当前 route 匹配有新老两套 matcher 并存?** —— 老 route matcher 维度硬编码(prefix/path/header/...),覆盖 99% 常见场景但无法表达新匹配维度;新 matcher tree 把"匹配什么、输出什么"完全数据化(可编程匹配引擎),用于复杂场景。Envoy 演进策略是并行引入新能力、保留老能力过渡,不强制迁移——所以源码里是按 VirtualHost 二选一(配 `matcher` 或配 `routes`,互斥),不是替代关系。

### 想继续深入往哪钻

- **想看 router filter 完整源码**:读 [router.cc](../envoy/source/common/router/router.cc) 的 `Filter::decodeHeaders`(L477)、`decodeData`、`encodeHeaders`/`encodeData`(响应回传),以及 [router.h](../envoy/source/common/router/router.h) 的 `Filter` 类(L299)。
- **想看 route_config 三层结构实现**:读 [config_impl.cc](../envoy/source/common/router/config_impl.cc) 的 `RouteMatcher`(L1948 构造、L1995 `findVirtualHost`、L2053 `route`)、`VirtualHostImpl::getRouteFromEntries`(L1856)、`RouteEntryImplBase::matchRoute`(L864)、各种 `*RouteEntryImpl::matches`(L1445 起)。
- **想看 weighted_clusters 实现**:读 [weighted_cluster_specifier.cc](../envoy/source/common/router/weighted_cluster_specifier.cc) 的 `pickClusterIndex`(L24,核心加权算法)、`WeightedClusterSpecifierPlugin::route`(L338)。
- **想看 matcher tree API**:读 [matcher.h](../envoy/source/common/matcher/matcher.h) 的 `evaluateMatch`(L41)、`MatchTreeFactory`(L130);以及 `source/common/matcher/` 下的 input/matcher/predicate 各种子类。
- **想看 RDS 动态下发(控制面侧)**:读 [rds_route_config_provider_impl.cc](../envoy/source/common/rds/rds_route_config_provider_impl.cc)、[rds_route_config_subscription.cc](../envoy/source/common/rds/rds_route_config_subscription.cc)。详细的 xDS 订阅与传输机制在第 5 篇 P5-17 拆透。
- **想动手感受**:配一个带 weighted_clusters 的 route(95% v1 / 5% v2),用 `curl` 多次请求观察 router stats(`cluster.v1.upstream_rq_total` vs `cluster.v2.upstream_rq_total`)的比例变化;或用 `istioctl proxy-config routes <pod>` 看 Istio 注入的真实 route_config 长什么样。

### 引出下一章

router 在 `decodeHeaders` 里匹配出 cluster、从 ClusterManager 查到 `ThreadLocalCluster` 后,把这个 cluster 交给 upstream 连接池发起请求——但"cluster 怎么定义、里面有哪些 endpoint、endpoint 怎么动态发现、HTTP/1 和 HTTP/2 的连接池怎么复用连接",这一整片 upstream 域还没讲。下一章 P4-12 **Cluster 与 Endpoint:后端集群与连接池**,从 router 选出的那个 cluster 名开始,拆 cluster 的类型(static/strict_dns/logical_dns/eds/original_dst)、endpoint 的服务发现、以及连接池的复用机制——这是数据面从 downstream 跨到 upstream 的核心一章。

> **下一章**:[P4-12 · Cluster 与 Endpoint:后端集群与连接池](P4-12-Cluster与Endpoint-后端集群与连接池.md)
