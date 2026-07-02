# 附录 B · 实践与调优:用 Pingora 搭代理、钩子开发、与 Nginx/Envoy 对照、性能调优

> **这份附录解决什么问题**:这本书正文 20 章(P0-01 ~ P7-20)拆透了 Pingora 的"为什么"——`ProxyHttp` 钩子链的灵魂、`TransportConnector` 连接池的招牌、Ketama 一致性哈希的精妙、自研 HTTP/1 与委托 h2 的取舍、`NoStealRuntime` 的独特运行时、TLS 四后端的可插换、缓存与 graceful upgrade。但那是"理解",不是"上手"。这份附录是**实战落地**——当你真的要拿 Pingora 搭一个生产反向代理时,该怎么写代码、怎么选型、怎么调优、怎么排查线上问题。
>
> 正文讲透的,这里一句带过指路;正文没讲的"怎么把零件装成一台能跑的代理",是这份附录的全部。如果你没读正文,先读 P1-02(`ProxyHttp` trait 全貌)+ P2-06(连接池招牌)+ P5-15(`NoStealRuntime` 招牌),再回来;如果你读过正文,这里把它们串成一份能直接用的工程手册。
>
> **读完这份附录你会明白**:
>
> 1. 怎么用 `ProxyHttp` 从零写一个生产可用的反向代理——鉴权(`request_filter` 短路)、改 header(`upstream_request_filter`)、限流(`pingora-limits` + 钩子决策)、负载均衡(`LoadBalancer` + `upstream_peer`),贴可运行的 Rust 代码框架。
> 2. `ProxyHttp` 的 ~30 个钩子,每个什么时候触发、用来做什么、有什么坑——一份钩子开发 checklist。
> 3. 同一个功能(鉴权/改 header/限流/LB/graceful upgrade)在 **Nginx 配置**、**Envoy 配置**、**Pingora 代码**里分别怎么写——三向写法对照表,直接做技术选型。
> 4. 几个关键选型:`NoSteal` vs `Steal`(默认 `work_stealing = true`,Cloudflare 生产关掉)、TLS 四后端(openssl/boringssl/rustls/s2n)怎么选、连接池参数(`keepalive_pool_size` 默认 128 / `offload_threadpool` / h2 `H2_WINDOW_SIZE` 8MB)怎么调。
> 5. 线上问题怎么排查:502、连接泄漏、HTTP smuggling、cache 投毒、限流失效、graceful upgrade 失败——一份症状-原因-排查-修复清单。
>
> 这份附录不写新机制,只做"把正文的零件装成一台能跑、能调、能排查的代理"。所有代码示例都贴近 `pingora/pingora-proxy/examples/` 下的真实示例,引用经本地 `../pingora/`(版本 `v0.8.1`,commit `719ef6cd`)Grep/Read 核实。

---

## 一句话点破

> **正文告诉你"为什么",这份附录告诉你"怎么动手"。用 Pingora 搭一个反向代理 = 实现 `ProxyHttp` trait 的最小三件套(`type CTX` + `new_ctx` + `upstream_peer`)+ 在需要的钩子里写业务逻辑(`request_filter` 鉴权 / `upstream_request_filter` 改 header / `upstream_peer` 里调 `LoadBalancer.select` 选后端 / `response_filter` 改响应)+ 把 service 挂到 `Server` 上跑起来。选型上记住三件事:`work_stealing` 默认 `true`(Cloudflare 生产关掉用 NoSteal),TLS 四后端按场景选(Cloudflare 选 BoringSSL),连接池 `keepalive_pool_size` 默认 128(高 QPS 要调)。排查上记住五个高频坑:502 看 `test_reusable_stream` 和 keepalive、smuggling 看 content-length 校验、cache 投毒看 `cache_key_callback`、限流失效看钩子决策、upgrade 失败看 SIGQUIT + fd 传递。**

这是结论。下面七节展开:② 从零搭反向代理(完整代码框架)③ 钩子开发 checklist ④ 三向写法对照(Nginx/Envoy/Pingora)⑤ 选型决策 ⑥ 调优手册 ⑦ 线上排查清单。

---

## 第一节:这份实战手册怎么用

### 为什么需要一份实战附录

Pingora 的官方仓库 `cloudflare/pingora` 在 `pingora-proxy/examples/` 下提供了十来个示例(`load_balancer.rs` / `gateway.rs` / `rate_limiter.rs` / `modify_response.rs` / `use_module.rs` / `connection_filter.rs` / `multi_lb.rs` / `backoff_retry.rs` / `grpc_web_module.rs` / `virtual_l4.rs` / `ctx.rs`)。这些示例各自演示一个特性(负载均衡、鉴权网关、限流、改响应、自定义 module、连接过滤、多集群路由、重试退避、gRPC-Web、虚拟 L4、CTX 跨钩子),但**没有一个示例把这些拼成一个完整的、生产可用的反向代理**——鉴权 + 改 header + 限流 + 负载均衡同时存在的那个"完整版",是这份附录要补的。

这份附录的定位是**"把 examples/ 拼成一台能跑的代理"的工程指南**。它不是 API 文档(那是官方文档的事),也不是源码精解(那是正文 20 章的事),而是:

- **怎么起步**:从 `cargo new` 到一个能 `curl` 通的反向代理,完整代码框架。
- **怎么写钩子**:`ProxyHttp` 的 ~30 个钩子,逐个说清什么时候用、注意什么。
- **怎么选型**:NoSteal vs Steal、TLS 四后端、连接池参数——选错了性能差一截。
- **怎么调优**:连接池、h2、缓存、TLS 的调参表。
- **怎么排查**:线上 502 / 连接泄漏 / smuggling / cache 投毒 / 限流失效 / upgrade 失败,症状-原因-排查-修复。

### 怎么读这份附录

- **第一次用 Pingora 搭代理**:读第二节(从零搭反向代理),复制代码框架,改业务逻辑,跑起来。然后读第三节(钩子 checklist)查漏补缺。
- **正在做技术选型(Nginx vs Envoy vs Pingora)**:直接跳第四节(三向写法对照),看同一个功能在三者里分别怎么写。
- **已经搭好,要上生产**:读第五节(选型决策)+ 第六节(调优手册),把默认参数调成适合你负载的值。
- **线上出问题了**:直接跳第七节(排查清单),按症状找原因和修复方案。

> **承接铁律**:正文讲透的(`ProxyHttp` trait 的设计动机、`TransportConnector` 连接池的 `test_reusable_stream` 技巧、`NoStealRuntime` 为什么不要 work-stealing、自研 HTTP/1 的 smuggling 防护、Ketama 与 Nginx 兼容的算法),这里一句带过指路;这份附录只讲"怎么动手"。Tokio 的运行时机制(reactor/scheduler/time wheel/budget)承《Tokio》系列,一句带过;hyper 的 HTTP/1 状态机承《hyper》系列 P2-06,一句带过;Envoy 的 filter chain 承《Envoy》系列第 3 篇,一句带过指路 [[envoy-source-facts]]。

---

## 第二节:从零搭一个反向代理——完整 `ProxyHttp` 实现

### 一个生产反向代理要做什么

一个生产可用的反向代理,通常要做五件事:

1. **接连接**(TCP/TLS 监听):`ListeningService` + `add_tcp` / `add_tls_with_settings`,框架自管。
2. **鉴权 / 访问控制**:有的请求放行,有的拒绝(403/401)。→ `request_filter` 钩子,`Ok(true)` 短路。
3. **限流**:按 client / appid / path 限制 QPS。→ `pingora-limits` 的 `Rate` 估算 + `request_filter` 钩子决策。
4. **改请求 / 改响应**:注入鉴权 token、改 Host、加 X-Forwarded-For、改 Server header。→ `upstream_request_filter` / `response_filter`。
5. **选后端(负载均衡)**:从一堆 upstream 里挑一个。→ `upstream_peer` 钩子里调 `LoadBalancer.select`。

这五件事,前 4 件在钩子链里(第二节归属"钩子链"),第 5 件的"挑"在钩子里但"挑的设施"(`LoadBalancer`)在转发设施里(横跨两面,见 P3-09~11)。下面把这五件事拼成一个完整的 `ProxyHttp` 实现。

### 最小三件套:`type CTX` + `new_ctx` + `upstream_peer`

实现 `ProxyHttp` 的最小代理,只需要实现三件套(`pingora-proxy/src/proxy_trait.rs#L31-L46`):

```rust
// 简化示意,基于 pingora-proxy/examples/load_balancer.rs#L29-L44
use async_trait::async_trait;
use pingora_core::server::configuration::Opt;
use pingora_core::server::Server;
use pingora_core::upstreams::peer::HttpPeer;
use pingora_core::Result;
use pingora_proxy::{ProxyHttp, Session};

pub struct MyProxy;

#[async_trait]
impl ProxyHttp for MyProxy {
    // ① 每请求的状态容器(贯穿全链,见 P1-02)
    type CTX = ();
    // ② 每请求创建一个 CTX
    fn new_ctx(&self) -> Self::CTX {}

    // ③ 唯一必须实现的钩子:告诉框架转发到哪
    async fn upstream_peer(
        &self,
        _session: &mut Session,
        _ctx: &mut Self::CTX,
    ) -> Result<Box<HttpPeer>> {
        let peer = Box::new(HttpPeer::new(
            ("1.1.1.1", 443),         // (addr, port)
            true,                      // use_tls
            "one.one.one.one".to_string(), // SNI
        ));
        Ok(peer)
    }
}
```

这就是一个能跑的反向代理。`type CTX = ()` 表示这个代理不需要跨钩子共享状态;`new_ctx` 返回空;`upstream_peer` 硬编码转发到 `1.1.1.1:443`。`HttpPeer::new` 的三个参数是 `(addr, use_tls, sni)`,见 P1-04。

把它挂到 `Server` 上跑起来:

```rust
// 基于 pingora-proxy/examples/load_balancer.rs#L60-L95 简化
fn main() {
    env_logger::init();
    let opt = Opt::parse_args();
    let mut my_server = Server::new(Some(opt)).unwrap();
    my_server.bootstrap();

    let mut my_proxy = pingora_proxy::http_proxy_service(
        &my_server.configuration,
        MyProxy,
    );
    my_proxy.add_tcp("0.0.0.0:6188"); // 监听 HTTP
    my_server.add_service(my_proxy);
    my_server.run_forever();
}
```

`Server::new` + `bootstrap` + `add_service` + `run_forever` 是 Pingora 的服务装配四步。`http_proxy_service` 把 `MyProxy`(实现 `ProxyHttp`)包成一个 `ListeningService`,`add_tcp` 给它加一个 TCP 监听端口。`run_forever` 进入事件循环(底层是 `NoStealRuntime` 的多个 `current_thread` runtime,见 P5-15)。

`cargo run` 后,`curl http://127.0.0.1:6188/` 就会把请求转发到 `1.1.1.1:443`(注意这里 downstream 是 HTTP、upstream 是 HTTPS,框架的 `bridge/` 会做协议转换,见 P4-14)。

### 加鉴权:`request_filter` 短路响应

最小代理跑起来后,加第一个业务逻辑——鉴权。鉴权要在请求进 upstream 之前做,做的位置是 `request_filter`。这个钩子的关键语义:**返回 `Ok(true)` = 短路**(已经响应了,不要再转发),**返回 `Ok(false)` = 继续**(见 P1-03)。

```rust
// 基于 pingora-proxy/examples/gateway.rs#L41-L52 简化
use bytes::Bytes;
use pingora_http::ResponseHeader;

fn check_login(req: &pingora_http::RequestHeader) -> bool {
    // 简化示意:Authorization 头匹配 "password"
    req.headers.get("Authorization").map(|v| v.as_bytes()) == Some(b"password")
}

#[async_trait]
impl ProxyHttp for MyProxy {
    type CTX = ();
    fn new_ctx(&self) -> Self::CTX {}

    // ★ request_filter:鉴权,Ok(true) = 短路
    async fn request_filter(
        &self,
        session: &mut Session,
        _ctx: &mut Self::CTX,
    ) -> Result<bool> {
        if !check_login(session.req_header()) {
            // 鉴权失败:写 403 响应,然后短路
            let _ = session
                .respond_error_with_body(403, Bytes::from_static(b"no way!"))
                .await;
            return Ok(true); // ★ true = 已响应,跳过 upstream
        }
        Ok(false) // 继续转发
    }

    async fn upstream_peer(
        &self,
        _session: &mut Session,
        _ctx: &mut Self::CTX,
    ) -> Result<Box<HttpPeer>> {
        let peer = Box::new(HttpPeer::new(
            ("1.1.1.1", 443),
            true,
            "one.one.one.one".to_string(),
        ));
        Ok(peer)
    }
}
```

> **坑点(短路语义)**:`request_filter` 的 `Ok(true)` 是**短路**(不再转发),`Ok(false)` 是**继续**。这个语义和直觉相反(很多人以为 `true` 是"通过"),记住:**`Ok(true)` = "我已经处理了(响应了),不要再转发"**。和 Envoy 的 `StopIteration` 对照(见 P1-03)。短路后 `logging` 钩子仍然会跑(收尾打日志),其余 upstream 钩子全部跳过。

### 加限流:`pingora-limits` + 钩子决策

限流是反向代理的高频需求。Pingora 的限流分两层:`pingora-limits` 提供估算器(`Rate` / `Inflight`),**只估算不限流**——它告诉你"这个 key 当前窗口的请求数是多少",**决策(放行还是拒绝)在钩子里自己做**(见 P6-19)。

`Rate` 用 CMS(Count-Min Sketch)+ 双缓冲(red/blue slot)估算,无锁,比朴素 `HashMap<key, AtomicUsize>` 省内存、抗高并发(见 P6-19 技巧)。`Rate::observe(&key, 1)` 返回当前窗口累计请求数。

```rust
// 基于 pingora-proxy/examples/rate_limiter.rs#L53-L116 简化
use once_cell::sync::Lazy;
use pingora_limits::rate::Rate;
use pingora_http::ResponseHeader;
use std::time::Duration;

// 全局 Rate 估算器:1 秒一个窗口
static RATE_LIMITER: Lazy<Rate> = Lazy::new(|| Rate::new(Duration::from_secs(1)));
// 每个 appid 每秒最多 1 个请求(演示用,生产按需调)
static MAX_REQ_PER_SEC: isize = 1;

fn get_request_appid(session: &Session) -> Option<String> {
    session
        .req_header()
        .headers
        .get("appid")
        .and_then(|v| v.to_str().ok())
        .map(|s| s.to_string())
}

#[async_trait]
impl ProxyHttp for MyProxy {
    type CTX = ();
    fn new_ctx(&self) -> Self::CTX {}

    async fn request_filter(
        &self,
        session: &mut Session,
        _ctx: &mut Self::CTX,
    ) -> Result<bool> {
        let appid = match get_request_appid(session) {
            None => return Ok(false), // 无 appid,不限流
            Some(a) => a,
        };
        // observe:记录 1 次,返回当前窗口累计数
        let curr = RATE_LIMITER.observe(&appid, 1);
        if curr > MAX_REQ_PER_SEC {
            // 超限:返回 429,短路
            let mut h = ResponseHeader::build(429, None).unwrap();
            h.insert_header("X-Rate-Limit-Limit", MAX_REQ_PER_SEC.to_string()).unwrap();
            h.insert_header("X-Rate-Limit-Remaining", "0").unwrap();
            session.set_keepalive(None); // 关 keepalive
            session.write_response_header(Box::new(h), true).await?;
            return Ok(true); // ★ 短路
        }
        Ok(false)
    }

    async fn upstream_peer(&self, _: &mut Session, _: &mut ()) -> Result<Box<HttpPeer>> {
        let peer = Box::new(HttpPeer::new(("1.1.1.1", 443), true, "one.one.one.one".to_string()));
        Ok(peer)
    }
}
```

> **关键理解**:`pingora-limits` **不替你限流**,它只给你一个无锁的、抗高并发的估算器。限流的"决策"——"超了多少算超"、"超了怎么办(拒绝 / 排队 / 降级)"——全在钩子里你自己写。这种"估算器 + 业务决策分离"的设计,让你能表达复杂策略(按请求体大小加权、按响应码降级、按时间段放宽),代价是简单场景也要自己写几行决策逻辑。`Rate` 的双缓冲(当前 slot 计数、上一 slot 报告)解决了"计数器重置瞬间丢数据"的问题,见 P6-19。

### 加改请求 / 改响应:`upstream_request_filter` / `response_filter`

反向代理常要改 header:给 upstream 注入鉴权 token、改 Host、加 X-Forwarded-For;给 downstream 改 Server header、删 alt-svc。这两件事在两个钩子里:

- `upstream_request_filter`:改要发给 upstream 的请求 header(在选完 peer 之后、发请求之前,见 P1-04)。
- `response_filter`:改要发给 downstream 的响应 header(缓存之后,见 P1-05)。

```rust
// 基于 pingora-proxy/examples/load_balancer.rs#L46-L57 + gateway.rs#L71-L88 简化
use pingora_http::{RequestHeader, ResponseHeader};

#[async_trait]
impl ProxyHttp for MyProxy {
    type CTX = ();
    fn new_ctx(&self) -> Self::CTX {}

    // 改要发给 upstream 的请求 header
    async fn upstream_request_filter(
        &self,
        _session: &mut Session,
        upstream_request: &mut RequestHeader,
        _ctx: &mut Self::CTX,
    ) -> Result<()> {
        // 改 Host(很多 upstream 用 Host 做虚拟主机路由)
        upstream_request.insert_header("Host", "one.one.one.one").unwrap();
        // 加 X-Forwarded-For(让 upstream 知道真实 client IP)
        upstream_request.insert_header("X-Forwarded-For", "203.0.113.1").unwrap();
        Ok(())
    }

    // 改要发给 downstream 的响应 header(缓存之后)
    async fn response_filter(
        &self,
        _session: &mut Session,
        upstream_response: &mut ResponseHeader,
        _ctx: &mut Self::CTX,
    ) -> Result<()> {
        // 改 Server header(隐藏 upstream 真实身份)
        upstream_response.insert_header("Server", "MyGateway").unwrap();
        // 删 alt-svc(不暴露 h3)
        upstream_response.remove_header("alt-svc");
        Ok(())
    }

    async fn upstream_peer(&self, _: &mut Session, _: &mut ()) -> Result<Box<HttpPeer>> {
        let peer = Box::new(HttpPeer::new(("1.1.1.1", 443), true, "one.one.one.one".to_string()));
        Ok(peer)
    }
}
```

> **缓存前/后分开**:`response_filter` 是**缓存后**改(改的内容不进缓存)。如果你要"改的内容进缓存"(影响所有命中这个缓存的请求),用 `upstream_response_filter`(缓存前,见 P1-05)。这个区分是 filter 顺序设计的精髓:缓存前改 = 影响缓存内容,缓存后改 = 只影响当前请求。坑点:`response_filter` 会被缓存命中的响应也触发,`upstream_response_filter` 不会(缓存命中没 upstream 响应),详见 P1-05 / P6-17。

### 加负载均衡:`upstream_peer` 里调 `LoadBalancer`

最后,把硬编码的 `1.1.1.1:443` 换成从一堆后端里挑一个。`LoadBalancer<S: BackendSelection>` 是 Pingora 的负载均衡器(`pingora-load-balancing/src/lib.rs`),`select(&self, key: &[u8], max_iterations: usize) -> Option<Backend>` 按 key 选后端(见 P3-09)。

```rust
// 基于 pingora-proxy/examples/load_balancer.rs 完整简化
use std::sync::Arc;
use std::time::Duration;
use pingora_core::services::background::background_service;
use pingora_load_balancing::{health_check::TcpHealthCheck, selection::RoundRobin, LoadBalancer};

pub struct LB(Arc<LoadBalancer<RoundRobin>>);

#[async_trait]
impl ProxyHttp for LB {
    type CTX = ();
    fn new_ctx(&self) -> Self::CTX {}

    async fn upstream_peer(
        &self,
        _session: &mut Session,
        _ctx: &mut (),
    ) -> Result<Box<HttpPeer>> {
        // select(key, max_iterations):RoundRobin 不看 key,Ketama 用 key 做 hash
        let upstream = self.0.select(b"", 256).unwrap();
        // (use_tls=true, SNI=one.one.one.one)
        let peer = Box::new(HttpPeer::new(upstream, true, "one.one.one.one".to_string()));
        Ok(peer)
    }

    async fn upstream_request_filter(
        &self, _: &mut Session, req: &mut RequestHeader, _: &mut (),
    ) -> Result<()> {
        req.insert_header("Host", "one.one.one.one").unwrap();
        Ok(())
    }
}

fn main() {
    env_logger::init();
    let opt = Opt::parse_args();
    let mut my_server = Server::new(Some(opt)).unwrap();
    my_server.bootstrap();

    // 后端列表(127.0.0.1:343 是故意的坏后端,演示健康检查)
    let mut upstreams =
        LoadBalancer::try_from_iter(["1.1.1.1:443", "1.0.0.1:443", "127.0.0.1:343"]).unwrap();

    // 主动健康检查:每 1 秒 TCP 探活一次,坏的后端不被选中
    let hc = TcpHealthCheck::new();
    upstreams.set_health_check(hc);
    upstreams.health_check_frequency = Some(Duration::from_secs(1));

    // 健康检查跑在 background_service 里(BackgroundService,见 P3-11)
    let background = background_service("health check", upstreams);
    let upstreams = background.task(); // Arc<LoadBalancer>,原子更新

    let mut lb = pingora_proxy::http_proxy_service(&my_server.configuration, LB(upstreams));
    lb.add_tcp("0.0.0.0:6188");

    my_server.add_service(lb);
    my_server.add_service(background); // 别忘了把 background service 也加进去
    my_server.run_forever();
}
```

几个要点:

- **`select(b"", 256)`**:两参版本(见 `pingora-load-balancing/src/lib.rs#L408`)。第一个 `key` 用于一致性哈希(Ketama,见 P3-10),RoundRobin/Random 不看 key 传 `b""` 即可;第二个 `max_iterations` 限制最大迭代次数(去重 + 限步,见 `UniqueIterator`,P3-09),传 256 是惯例值。`pingora-load-balancing` **没有内置 P2C(Power of Two Choices)**,这是 TODO;`LoadBalancer<S>` 根据 `S` 的不同选 RoundRobin / Random / FNVHash / KetamaHashing(见 P3-10)。
- **`background_service` + `task()`**:健康检查是 `BackgroundService`(独立 task 周期跑,见 P3-11),它持有 `LoadBalancer` 的所有权,`task()` 返回 `Arc<LoadBalancer>`,业务持有这个 `Arc` 在 `upstream_peer` 里调 `select`。健康检查发现坏后端后,用 `ArcSwap` 原子更新 selector(见 P3-09),业务无感知。
- **健康检查只内置 `TcpHealthCheck`**(TCP 探活),`HealthCheck` trait 可以自己 impl(比如 HTTP 健康检查),见 P3-11。
- **服务发现只内置 `Static`**(从迭代器静态给),DNS 是 TODO,要动态服务发现(Consul / xDS / k8s)自己 impl `ServiceDiscovery` trait(见 P3-11)。

### 一个完整的反向代理:五合一

把上面五件事拼起来,就是一个生产可用的反向代理骨架。下面这个版本整合了**鉴权 + 限流 + 改 header + 改响应 + 负载均衡**:

```rust
// 整合示例:鉴权 + 限流 + 改 header + 负载均衡(贴近 examples/,生产可用骨架)
use async_trait::async_trait;
use bytes::Bytes;
use once_cell::sync::Lazy;
use std::sync::Arc;
use std::time::Duration;

use pingora_core::server::configuration::Opt;
use pingora_core::server::Server;
use pingora_core::services::background::background_service;
use pingora_core::upstreams::peer::HttpPeer;
use pingora_core::Result;
use pingora_http::{RequestHeader, ResponseHeader};
use pingora_limits::rate::Rate;
use pingora_load_balancing::{health_check::TcpHealthCheck, selection::RoundRobin, LoadBalancer};
use pingora_proxy::{ProxyHttp, Session};

static RATE_LIMITER: Lazy<Rate> = Lazy::new(|| Rate::new(Duration::from_secs(1)));
const MAX_REQ_PER_SEC: isize = 100;

pub struct MyGateway {
    lb: Arc<LoadBalancer<RoundRobin>>,
}

#[async_trait]
impl ProxyHttp for MyGateway {
    // CTX:跨钩子共享的状态容器(这里记录是否命中限流,供 logging 用)
    type CTX = GatewayCtx;
    fn new_ctx(&self) -> Self::CTX {
        GatewayCtx::default()
    }

    // ① request_filter:鉴权 + 限流(可短路)
    async fn request_filter(
        &self,
        session: &mut Session,
        ctx: &mut Self::CTX,
    ) -> Result<bool> {
        // 鉴权:Authorization: password
        if session.req_header().headers.get("Authorization")
            .map(|v| v.as_bytes()) != Some(b"password")
        {
            let _ = session.respond_error_with_body(403, Bytes::from_static(b"forbidden")).await;
            return Ok(true); // 短路
        }

        // 限流:按 client IP(appid 简化为 IP)每秒 MAX_REQ_PER_SEC
        let key = session
            .client_addr()
            .map(|a| a.to_string())
            .unwrap_or_else(|| "unknown".into());
        let curr = RATE_LIMITER.observe(&key, 1);
        if curr > MAX_REQ_PER_SEC {
            ctx.rate_limited = true;
            let mut h = ResponseHeader::build(429, None).unwrap();
            h.insert_header("Retry-After", "1").unwrap();
            session.set_keepalive(None);
            session.write_response_header(Box::new(h), true).await?;
            return Ok(true); // 短路
        }
        Ok(false) // 继续
    }

    // ② upstream_peer:负载均衡选后端(必实现)
    async fn upstream_peer(
        &self, _: &mut Session, _: &mut Self::CTX,
    ) -> Result<Box<HttpPeer>> {
        let upstream = self.lb.select(b"", 256).unwrap();
        let peer = Box::new(HttpPeer::new(upstream, true, "one.one.one.one".to_string()));
        Ok(peer)
    }

    // ③ upstream_request_filter:改要发给 upstream 的请求 header
    async fn upstream_request_filter(
        &self, _session: &mut Session, req: &mut RequestHeader, _: &mut Self::CTX,
    ) -> Result<()> {
        req.insert_header("Host", "one.one.one.one").unwrap();
        req.insert_header("X-Forwarded-Proto", "https").unwrap();
        Ok(())
    }

    // ④ response_filter:改要发给 downstream 的响应 header(缓存后)
    async fn response_filter(
        &self, _: &mut Session, resp: &mut ResponseHeader, _: &mut Self::CTX,
    ) -> Result<()> {
        resp.insert_header("Server", "MyGateway").unwrap();
        resp.remove_header("alt-svc");
        Ok(())
    }

    // ⑤ logging:收尾打日志(即使短路也跑)
    async fn logging(
        &self, session: &mut Session, e: Option<&pingora_core::Error>, ctx: &mut Self::CTX,
    ) {
        let status = session.response_written().map_or(0, |r| r.status.as_u16());
        let limited = if ctx.rate_limited { " [RATE LIMITED]" } else { "" };
        log::info!("{} -> {}{}", self.request_summary(session, ctx), status, limited);
        if let Some(err) = e { log::warn!("  error: {err}"); }
    }
}

#[derive(Default)]
struct GatewayCtx {
    rate_limited: bool,
}

fn main() {
    env_logger::init();
    let opt = Opt::parse_args();
    let mut server = Server::new(Some(opt)).unwrap();
    server.bootstrap();

    let mut upstreams =
        LoadBalancer::try_from_iter(["1.1.1.1:443", "1.0.0.1:443"]).unwrap();
    let hc = TcpHealthCheck::new();
    upstreams.set_health_check(hc);
    upstreams.health_check_frequency = Some(Duration::from_secs(1));

    let bg = background_service("health check", upstreams);
    let upstreams = bg.task();

    let mut proxy = pingora_proxy::http_proxy_service(
        &server.configuration,
        MyGateway { lb: upstreams },
    );
    proxy.add_tcp("0.0.0.0:6188");
    server.add_service(proxy);
    server.add_service(bg);
    server.run_forever();
}
```

> **注意上面这个整合版**:`type CTX = GatewayCtx`(不是 `()`),因为 `request_filter` 里设置的 `rate_limited` 标志要在 `logging` 里读——这就是 `type CTX` 关联类型的核心价值(贯穿全链的 per-request 状态容器,无锁,见 P1-02)。如果跨**请求**要共享状态(比如全局计数器),用 `Arc<Mutex<T>>` 放在 struct 字段里,不要放在 CTX 里——**CTX 是 per-request 的,不跨请求**(`persist_connection_context` 这个名字在 Pingora 里**不存在**,跨请求状态自己用 `Arc/Mutex`)。

### 启用 TLS 监听(可选)

上面的代理只监听 HTTP(`add_tcp`)。如果要监听 HTTPS,加 TLS:

```rust
// 基于 pingora-proxy/examples/load_balancer.rs#L84-L90
let cert_path = format!("{}/tests/keys/server.crt", env!("CARGO_MANIFEST_DIR"));
let key_path = format!("{}/tests/keys/key.pem", env!("CARGO_MANIFEST_DIR"));
let mut tls_settings = pingora_core::listeners::tls::TlsSettings::intermediate(&cert_path, &key_path).unwrap();
tls_settings.enable_h2(); // 允许 h2(ALPN 协商)
proxy.add_tls_with_settings("0.0.0.0:6189", None, tls_settings);
```

TLS 监听的细节(证书、ALPN、特性)见 P5-16。TLS 后端(openssl/boringssl/rustls/s2n)的选型见第五节。

### 启用缓存(可选,谨慎)

启用缓存要小心——`cache_key_callback` **必须你自己实现**,默认实现会 panic(`proxy_trait.rs#L156-L158`),这是防缓存投毒的防御(0.8.0 移除 `CacheKey::default`,见 P6-17)。

```rust
// 启用缓存(谨慎,cache_key_callback 必须实现)
use pingora_cache::CacheKey;

impl ProxyHttp for MyProxy {
    // ...其他钩子...

    // 决定是否缓存这个请求(在 request_cache_filter 里启用 session.cache)
    fn request_cache_filter(&self, session: &mut Session, _ctx: &mut Self::CTX) -> Result<()> {
        // 只缓存 GET
        if session.req_header().method == http::Method::GET {
            session.cache.enable(pingora_cache::CacheMeta::default())?;
        }
        Ok(())
    }

    // ★ 必须!默认 panic,防投毒
    fn cache_key_callback(
        &self, session: &Session, _ctx: &mut Self::CTX,
    ) -> Result<CacheKey> {
        // 关键:cache key 要包含所有影响 upstream 响应的维度
        // 这里用 method + host + path + query,Vary header 单独处理(variance,见 P6-17)
        let req = session.req_header();
        let key = format!(
            "{}|{}|{}",
            req.method,
            req.uri.host().unwrap_or(""),
            req.uri.path_and_query().unwrap_or("").to_string(),
        );
        Ok(CacheKey::new("", &key, ""))
    }
}
```

> **cache 投毒防御**:cache key 必须包含**所有影响 upstream 响应的维度**(method / host / path / query / 关键 header)。如果漏了某个维度(比如只 hash path 不 hash method),攻击者能让不同请求共享同一个缓存条目——POST 的响应被 GET 请求命中,数据泄露。这就是为什么 0.8.0 移除默认实现强制 user 写。第七节排查清单有 cache 投毒的详细排查。

---

## 第三节:钩子开发 checklist——逐钩子何时用、注意什么

`ProxyHttp` trait 有 ~30 个方法(`proxy_trait.rs`),除了 `type CTX` / `new_ctx` / `upstream_peer` 必实现,其余都有默认实现(大多是空操作)。这一节给一份 checklist:每个钩子什么时候触发、用来做什么、有什么坑。

### 必实现的三件套

| 钩子 / 关联类型 | 何时触发 | 用来做什么 | 注意什么 |
|----------------|---------|-----------|---------|
| `type CTX` | — | 每请求状态容器,贯穿全链 | 不跨请求!跨请求用 `Arc<Mutex>`。`()` 表示无状态 |
| `new_ctx(&self) -> CTX` | 每请求开始 | 创建 CTX 实例 | 轻量,别在这做重活(每请求都跑) |
| `upstream_peer(&self, session, ctx) -> Result<Box<HttpPeer>>` | cache miss 后,选后端 | **唯一必实现的钩子**,返回转发目标 | `HttpPeer::new(addr, use_tls, sni)`,见 P1-04 |

### 请求前半段钩子(选 upstream 之前)

| 钩子 | 何时触发 | 用来做什么 | 坑 / 注意 |
|------|---------|-----------|----------|
| `early_request_filter` | 解析完请求,**在所有下游模块前** | 在模块前介入(如设模块需要的标志) | 注意:在 access control / rate limit 模块**之前**跑,所以这里别放安全逻辑(放 `request_filter`),除非你要绕过模块 |
| `init_downstream_modules` | 服务器启动**一次**(不是每请求) | 注册下游 module(compression / grpc_web / 自定义 ACL) | 不是 async,不是 filter。用 `modules.add_module(...)` 注册,见 `examples/use_module.rs` |
| **`request_filter`** | early_filter 之后,模块之后 | **鉴权 / 限流 / 直接响应** | ★ **`Ok(true)` = 短路**!`Ok(false)` = 继续。短路后 `logging` 仍跑,其余 upstream 钩子跳过 |
| `request_body_filter` | 收到请求体块(每块一次) | 逐块处理 / 缓存请求体 / WAF 检测 | `body: &mut Option<Bytes>` 是**当前块**,不是整个 body。`end_of_stream` 标记最后一块。重活可 offload 到 blocking 线程 |
| `allow_spawning_subrequest` | early_filter 之后 | 是否允许这个 session 产生子请求 | 默认 `false`。开缓存后台重验证需要 `true` |

### 缓存相关钩子(启用缓存时)

| 钩子 | 何时触发 | 用来做什么 | 坑 / 注意 |
|------|---------|-----------|----------|
| `request_cache_filter` | request_filter 之后 | 决定是否缓存(`session.cache.enable(...)`) | 默认不启用缓存,要手动 enable |
| **`cache_key_callback`** | 启用缓存后 | 生成 cache key | ★ **默认 panic!必须实现**。漏维度 = 缓存投毒。包含所有影响响应的维度 |
| `cache_miss` | 缓存未命中,即将去 upstream | 标记 / 记录 | 默认调 `session.cache.cache_miss()` |
| `cache_hit_filter` | 缓存命中 | 决定是否强制失效 / 调整 body reader | 返回 `Some(ForcedFreshness::...)` 强制失效 |
| `response_cache_filter` | upstream 响应回来 | 决定是否写缓存 | 返回 `RespCacheable::Cacheable(...)` 或 `Uncacheable(...)` |
| `cache_vary_filter` | 决定 vary key | 按 Vary header 区分变体 | 默认 `None`(不区分变体) |
| `cache_not_modified_filter` | 条件请求(If-None-Match) | 决定是否返回 304 | 默认实现已处理 ETag |
| `range_header_filter` | Range 请求 | 决定返回哪个字节范围 | 默认处理单 range,最多 200 个 |
| `should_serve_stale` | upstream 出错 / 重验证时 | 是否返回过期缓存 | 默认:upstream 错才返回 stale |

### 选后端 + 改请求钩子

| 钩子 | 何时触发 | 用来做什么 | 坑 / 注意 |
|------|---------|-----------|----------|
| `proxy_upstream_filter` | cache miss 后,**upstream_peer 之前** | 最后一次决定是否真的转发 | ★ **`Ok(true)` = 放行!**(默认),`Ok(false)` = 不转发(短路)。**方向和 `request_filter` 相反!**API 瑕疵注意 |
| **`upstream_peer`** | proxy_upstream_filter 之后 | **选后端,返回 `Box<HttpPeer>`** | 必实现。重试时会再调(配 `fail_to_connect` / `error_while_proxy`) |
| `upstream_request_filter` | 选完 peer,发请求前 | 改要发给 upstream 的请求 header | 注入 token / 改 Host / 加 X-Forwarded-For |

> **★ API 瑕疵提醒(必背)**:
> - `request_filter`:**`Ok(true)` = 短路**(已响应,不转发)。
> - `proxy_upstream_filter`:**`Ok(true)` = 放行**(继续转发),`Ok(false)` = 不转发。
>
> 两个钩子的 `Ok(true)` 方向**相反**!这是 Pingora API 的一个历史瑕疵,实操时容易记混。记忆法:`request_filter` 的 true 是"我处理完了"(早返回),`proxy_upstream_filter` 的 true 是"我同意放行"(后置检查)。如果搞混,要么该转发的被拒绝(502),要么该拒绝的被放行(安全漏洞)。

### 响应与收尾钩子(原路返回)

| 钩子 | 何时触发 | 用来做什么 | 坑 / 注意 |
|------|---------|-----------|----------|
| `upstream_response_filter` | upstream 响应 header 回来 | 改 upstream 响应 header | ★ **缓存前**!改的内容**会进缓存**。缓存命中不触发 |
| `upstream_response_body_filter` | upstream 响应体块 | 逐块改响应体 | `Option<Duration>` 返回值用于延迟(可选) |
| `upstream_response_trailer_filter` | upstream trailer | 改 trailer | h2 才有 trailer,h1 一般没有 |
| `response_cache_filter` | upstream 响应回来 | 决定是否写缓存 | 见上表 |
| `response_filter` | 发给 downstream 前 | 改要发给 downstream 的响应 header | ★ **缓存后**!改的内容**不进缓存**。缓存命中也触发 |
| `response_body_filter` | 发响应体块 | 逐块改响应体(发给 downstream 的) | 缓存命中也触发(从缓存读出来再改) |
| `response_trailer_filter` | 发 trailer | 改发给 downstream 的 trailer | — |
| `logging` | 请求结束(无论成功失败,即使短路) | 打访问日志 / 记 metrics | ★ 即使 `request_filter` 短路也跑!这是收尾的唯一可靠介入点 |

### 连接与错误处理钩子

| 钩子 | 何时触发 | 用来做什么 | 坑 / 注意 |
|------|---------|-----------|----------|
| `connected_to_upstream` | 连接到 upstream 成功(新建或复用) | 记时延 / 日志 | `reused: bool` 标记是否复用 keepalive 连接 |
| `error_while_proxy` | 连接建立后出错(传输中) | 处理错误,决定是否重试 | 默认:复用的 client 连接 + retry buffer 没截断 → 允许重试 |
| `fail_to_connect` | 建立连接失败 | 决定是否重试(改 error 的 retry 标志) | 重试会再调 `upstream_peer`(可换后端) |
| `fail_to_proxy` | 请求致命错误 | 给 downstream 写错误响应 | 默认按 error 类型映射状态码(502/500/400/0) |
| `request_summary` | 错误日志需要时 | 生成请求摘要字符串 | 默认调 session 的 `request_summary` |
| `suppress_error_log` | 错误日志要生成时 | 决定是否抑制错误日志 | 默认 `false`(不抑制) |
| `is_purge` | 每请求 | 是否是 purge 请求(清缓存) | 默认 `false` |

### 钩子开发通用 checklist

写钩子时,过一遍这个 checklist:

- [ ] 这个逻辑该放在**哪个钩子**?(参考时序图:P1-02 / P7-20 第二节)放错位置 = 时序错乱。
- [ ] 需要**短路**吗?(`request_filter` 的 `Ok(true)` / `proxy_upstream_filter` 的 `Ok(false)`)。短路后 `logging` 仍跑。
- [ ] 需要**跨钩子共享状态**吗?用 `type CTX`(per-request),不要用全局变量。
- [ ] 需要**跨请求共享状态**吗?(全局计数器 / 配置)用 `Arc<Mutex<T>>` / `Arc<ArcSwap<T>>` 放 struct 字段,**不要放 CTX**(CTX 不跨请求)。
- [ ] 钩子是 **async** 的吗?重逻辑(WAF / 加密 / 大计算)用 `tokio::task::spawn_blocking` offload,别阻塞 reactor。
- [ ] 改响应的**缓存前/后**选对了吗?(`upstream_response_filter` 缓存前 / `response_filter` 缓存后)。
- [ ] 错误处理对了吗?`Result::Err` 会让请求失败 + 记错误日志,谨慎使用。

---

## 第四节:三向写法对照——Nginx / Envoy / Pingora 怎么写同一个功能

技术选型时,同一个功能在三套系统里分别怎么写,是最直观的对比。这一节挑五个高频功能(鉴权 / 改 header / 限流 / 负载均衡 / graceful upgrade),给出三套写法对照。这一节是技术选型的利器——你看完,该选谁一目了然。

### 功能一:鉴权(按 Authorization 头放行 / 拒绝)

**Nginx 配置**(用 map + if):

```nginx
# nginx.conf
map $http_authorization $auth_ok {
    default 0;
    "password" 1;
}

server {
    listen 80;
    location / {
        if ($auth_ok = 0) {
            return 403;
        }
        proxy_pass http://backend;
    }
}
```

**Envoy 配置**(用 Lua filter 或 ext_authz):

```yaml
# envoy.yaml(简化,用 ext_authz 调外部鉴权服务)
static_resources:
  listeners:
  - address: { socket_address: { address: 0.0.0.0, port_value: 80 } }
    filter_chains:
    - filters:
      - name: envoy.filters.network.http_connection_manager
        typed_config:
          stat_prefix: ingress
          route_config:
            virtual_hosts:
            - name: backend
              routes: [{ match: { prefix: "/" }, route: { cluster: backend } }]
          http_filters:
          - name: envoy.filters.http.ext_authz          # 外部鉴权
            typed_config:
              "@type": type.googleapis.com/envoy.extensions.filters.http.ext_authz.v3.ExtAuthz
              http_service: { server_uri: { uri: auth:9000, cluster: auth } }
          - name: envoy.filters.http.router
```

**Pingora 代码**(实现 `request_filter`):

```rust
// 见第二节"加鉴权"
async fn request_filter(&self, session: &mut Session, _: &mut Self::CTX) -> Result<bool> {
    if session.req_header().headers.get("Authorization").map(|v| v.as_bytes()) != Some(b"password") {
        let _ = session.respond_error_with_body(403, Bytes::from_static(b"forbidden")).await;
        return Ok(true); // 短路
    }
    Ok(false)
}
```

**对照**:

| 维度 | Nginx | Envoy | Pingora |
|------|-------|-------|---------|
| 表达方式 | 配置(map + if) | 配置(ext_authz filter) | Rust 代码(钩子) |
| 复杂逻辑 | 弱(if/map 表达力有限,复杂要 lua) | 中(filter + 外部服务) | 强(任意 Rust 代码) |
| 动态性 | reload 配置 | xDS 推 / 外部服务 | 改代码重新部署 |
| 性能 | 配置阶段 hook(C) | 虚分派 + ext_authz RPC | async 钩子(可 spawn_blocking) |

### 功能二:改 header(加 X-Forwarded-For / 改 Server)

**Nginx 配置**:

```nginx
server {
    location / {
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header Host $host;
        proxy_pass http://backend;
    }
}
# 改响应 Server:用 more_set_headers (nginx-module-headers-more) 或 add_header
```

**Envoy 配置**(用 request_headers_to_add / response_headers_to_add):

```yaml
route_config:
  virtual_hosts:
  - name: backend
    request_headers_to_add:
    - header: { key: "x-forwarded-for", value: "%DOWNSTREAM_REMOTE_ADDRESS%" }
    response_headers_to_add:
    - header: { key: "server", value: "MyGateway" }
    routes: [{ match: { prefix: "/" }, route: { cluster: backend } }]
```

**Pingora 代码**(`upstream_request_filter` + `response_filter`):

```rust
// 见第二节"加改请求/改响应"
async fn upstream_request_filter(&self, _: &mut Session, req: &mut RequestHeader, _: &mut ()) -> Result<()> {
    req.insert_header("X-Forwarded-For", "203.0.113.1").unwrap();
    Ok(())
}
async fn response_filter(&self, _: &mut Session, resp: &mut ResponseHeader, _: &mut ()) -> Result<()> {
    resp.insert_header("Server", "MyGateway").unwrap();
    Ok(())
}
```

**对照**:

| 维度 | Nginx | Envoy | Pingora |
|------|-------|-------|---------|
| 改请求 header | `proxy_set_header`(配置) | `request_headers_to_add`(配置) | `upstream_request_filter`(代码) |
| 改响应 header | `add_header` / lua | `response_headers_to_add` | `response_filter`(代码) |
| 条件改写 | 弱(if + 变量) | 中(CEL / lua) | 强(任意 Rust 条件) |

### 功能三:限流(按 client IP 限 QPS)

**Nginx 配置**(用 limit_req):

```nginx
# nginx.conf
limit_req_zone $binary_remote_addr zone=mylimit:10m rate=100r/s;
server {
    location / {
        limit_req zone=mylimit burst=20 nodelay;
        proxy_pass http://backend;
    }
}
```

**Envoy 配置**(用 local_rate_limit filter):

```yaml
http_filters:
- name: envoy.filters.http.local_ratelimit
  typed_config:
    "@type": type.googleapis.com/envoy.extensions.filters.http.local_ratelimit.v3.LocalRateLimit
    stat_prefix: ratelimit
    token_bucket: { max_tokens: 100, tokens_per_fill: 100, fill_interval: 1s }
    filter_enabled: { runtime_key: rl, default_value: { numerator: 100 } }
    filter_enforced: { runtime_key: rl, default_value: { numerator: 100 } }
```

**Pingora 代码**(`pingora-limits::Rate` + `request_filter`):

```rust
// 见第二节"加限流"
static RATE_LIMITER: Lazy<Rate> = Lazy::new(|| Rate::new(Duration::from_secs(1)));
async fn request_filter(&self, session: &mut Session, _: &mut ()) -> Result<bool> {
    let key = session.client_addr().map(|a| a.to_string()).unwrap_or_default();
    if RATE_LIMITER.observe(&key, 1) > 100 {
        // 返回 429,短路
        let mut h = ResponseHeader::build(429, None).unwrap();
        session.set_keepalive(None);
        session.write_response_header(Box::new(h), true).await?;
        return Ok(true);
    }
    Ok(false)
}
```

**对照**:

| 维度 | Nginx | Envoy | Pingora |
|------|-------|-------|---------|
| 估算器 | 内置(共享内存 zone) | 内置(token_bucket) | `pingora-limits`(CMS + 双缓冲) |
| 决策 | 配置驱动(超了直接拒) | 配置驱动(filter 拒) | **代码驱动**(钩子自己决定超了怎么办) |
| 分布式 | 不支持(每 worker 独立) | 不支持(thread-local) | 不支持(每 runtime 独立) |
| 复杂策略 | 弱 | 中(运行期百分比) | 强(任意 Rust 逻辑) |

> **关键差异**:Nginx / Envoy 的限流是**配置驱动 + 框架决策**(超了框架自动拒),Pingora 是**估算器 + 业务决策**(`pingora-limits` 只给数字,拒不拒绝钩子里自己定)。Pingora 的设计给你最大灵活(按请求体加权 / 按响应码降级 / 分级限流),代价是简单场景也要写几行决策代码。分布式限流(跨机器)三者都不内置,要接 Redis / 外部服务。

### 功能四:负载均衡(RoundRobin + 健康检查 + 一致性哈希)

**Nginx 配置**:

```nginx
upstream backend {
    least_conn;                    # 或默认 RoundRobin
    server 1.1.1.1:443 max_fails=3 fail_timeout=30s;   # 被动健康检查
    server 1.0.0.1:443 max_fails=3 fail_timeout=30s;
    # hash $request_uri consistent;   # 一致性哈希(取消注释启用)
    keepalive 32;                  # upstream keepalive
}
server {
    location / { proxy_pass http://backend; }
}
```

**Envoy 配置**:

```yaml
clusters:
- name: backend
  connect_timeout: 0.25s
  type: STRICT_DNS                  # 服务发现
  lb_policy: ROUND_ROBIN            # 或 LEAST_REQUEST / RING_HASH / MAGLEV
  health_checks:
  - timeout: 1s
    interval: 1s
    healthy_threshold: 1
    unhealthy_threshold: 3
    tcp_health_check: {}
  load_assignment:
    cluster_name: backend
    endpoints:
    - lb_endpoints:
      - { endpoint: { address: { socket_address: { address: 1.1.1.1, port_value: 443 } } } }
      - { endpoint: { address: { socket_address: { address: 1.0.0.1, port_value: 443 } } } }
  circuit_breakers:                # 熔断
    thresholds:
    - { max_connections: 1000, max_pending_requests: 1000 }
```

**Pingora 代码**:

```rust
// 见第二节"加负载均衡"
let mut upstreams = LoadBalancer::try_from_iter(["1.1.1.1:443", "1.0.0.1:443"]).unwrap();
let hc = TcpHealthCheck::new();           // 主动健康检查
upstreams.set_health_check(hc);
upstreams.health_check_frequency = Some(Duration::from_secs(1));
let bg = background_service("health check", upstreams);
let upstreams = bg.task();

// 一致性哈希:把 RoundRobin 换成 Ketama
// use pingora_load_balancing::selection::Consistent;
// let mut upstreams: LoadBalancer<Consistent> = LoadBalancer::try_from_iter([...]).unwrap();
// 然后 upstreams.select(b"some_key", 256)  // key 参与 hash
```

**对照**:

| 维度 | Nginx | Envoy | Pingora |
|------|-------|-------|---------|
| RoundRobin | 默认 | `lb_policy: ROUND_ROBIN` | `LoadBalancer::<RoundRobin>` |
| 一致性哈希 | `hash ... consistent` | `RING_HASH` / `MAGLEV` | `LoadBalancer::<Consistent>`(Ketama) |
| 主动健康检查 | 商业版才有 | 内置(配置) | `TcpHealthCheck`(内置)+ 自定义 `HealthCheck` trait |
| 服务发现 | `upstream`(静态 / DNS) | xDS(EDS)严格 | `ServiceDiscovery` trait(内置 Static,DNS TODO) |
| 熔断 | `max_fails`(被动) | `circuit_breakers`(主动) | 无内置,钩子里自己写(基于 `Rate` / `Inflight`) |
| P2C | 无 | `LEAST_REQUEST`(类似) | **无**(TODO) |

> **一致性哈希兼容性**:Pingora 的 Ketama(`Consistent = KetamaHashing`)与 Nginx 的 `hash consistent` **结果级兼容**(都基于 CRC32,每后端 160 points),所以 Pingora 能无缝替换 Nginx(同一请求 hash 到同一后端,迁移不丢会话粘性),见 P3-10。Envoy 的 `RING_HASH` 用不同的 hash 函数,与 Nginx/Pingora 不兼容。

### 功能五:graceful upgrade(零停机重启)

**Nginx**:`nginx -s reload` 发 SIGHUP,master 进程 fork 新 worker,旧 worker 处理完已有连接后退出。**有损**(断 keepalive,因为新 worker 是新进程,连接 fd 不传递)。

```bash
nginx -s reload     # SIGHUP,fork 新 worker
```

**Envoy**:hot restart,通过 SCM_RIGHTS(unix domain socket)在新旧进程间传递 listener fd,新进程接 accept,旧进程处理完已有连接后退出。**无损**。

```bash
# Envoy 启动时指定 --restart-epoch / --restart-echo
envoy --restart-epoch 1 --base-id 0 ...
# 升级:启动新进程,旧进程通过 hot restart 协议把 fd 传过去
```

**Pingora**:graceful upgrade,发 **SIGQUIT**(`pingora-core/src/server/mod.rs#L145-L166`),旧进程通过 `transfer_fd`(unix domain socket recvmsg)把 listener fd 传给新进程,新进程接 accept,旧进程处理完已有连接后退出。**无损**。

```bash
# 启动 v1
./my_proxy -d -c config.yaml &

# 升级:启动 v2,v2 通过 transfer_fd 从 v1 拿 listener fd
kill -SIGQUIT $(pidof my_proxy)   # ★ SIGQUIT,不是 SIGHUP!
# v2 接管 accept,v1 处理完已有连接后退出
```

**对照**:

| 维度 | Nginx | Envoy | Pingora |
|------|-------|-------|---------|
| 信号 | SIGHUP(reload) | 自定义 hot restart 协议 | **SIGQUIT**(`server/mod.rs#L155`) |
| fd 传递 | fork(fork 出的子进程继承 fd) | SCM_RIGHTS | `transfer_fd`(recvmsg unix socket) |
| keepalive | **断**(新 worker 不持有旧连接) | 不断 | 不断 |
| 有损吗 | 有损(断 keepalive) | 无损 | 无损 |
| 跨请求状态 | 无(连接重启) | thread-local 重建 | `Arc` 重建(**`persist_connection_context` 不存在**,跨请求状态自己用 Arc/Mutex) |

> **★ 信号别记错**:Pingora 的 graceful upgrade 信号是 **SIGQUIT**,不是 Nginx 的 SIGHUP。发错信号(`kill -HUP`)Pingora 不会响应升级(它不监听 HUP)。SIGTERM 是 graceful terminate(关闭,不是升级),SIGINT 是 fast shutdown(立即停)。这三个别混。

### 三向写法对照总表

| 功能 | Nginx | Envoy | Pingora |
|------|-------|-------|---------|
| 鉴权 | map + if(配置) | ext_authz filter(配置 + 外部服务) | `request_filter`(Rust 代码) |
| 改 header | `proxy_set_header`(配置) | `request_headers_to_add`(配置) | `upstream_request_filter`(代码) |
| 限流 | `limit_req`(配置 + 框架决策) | `local_ratelimit`(配置 + 框架决策) | `Rate` + `request_filter`(估算器 + 业务决策) |
| RoundRobin | 默认 | `lb_policy: ROUND_ROBIN` | `LoadBalancer::<RoundRobin>` |
| 一致性哈希 | `hash consistent` | `RING_HASH` | `LoadBalancer::<Consistent>`(与 Nginx 兼容) |
| 主动健康检查 | 商业版 | 配置(`health_checks`) | `TcpHealthCheck` + background_service |
| 服务发现 | `upstream`(静态/DNS) | xDS(EDS) | `ServiceDiscovery` trait(Static 内置,DNS TODO) |
| 熔断 | 被动(`max_fails`) | `circuit_breakers`(主动) | 无内置,钩子里写 |
| graceful upgrade | SIGHUP(reload,有损) | hot restart(无损) | **SIGQUIT**(`transfer_fd`,无损) |
| keepalive 不丢 | 断 | 不断 | 不断 |

> **怎么用这张表**:选型时,先看你的**组织心智**——运维驱动(Nginx 配置)、平台驱动(Envoy xDS)、还是开发者驱动(Pingora 代码)。再看**具体功能**——如果功能都能用配置表达(Nginx 场景甜蜜点),Nginx 最省事;如果需要 xDS 动态下发 + 服务网格,Envoy;如果业务逻辑复杂多变(按请求选后端 / 复杂改写 / WAF),Pingora 让你用 Rust 写,编译器防守内存安全。详见 P0-01 / P7-20 第三节的三向对照总表。

---

## 第五节:选型决策——NoSteal vs Steal、TLS 四后端、连接池

搭好代理后,上生产前要过一遍选型。这一节讲三个关键选型:运行时(NoSteal vs Steal)、TLS 后端(openssl/boringssl/rustls/s2n)、连接池参数。

### 选型一:NoSteal vs Steal(work_stealing)

Pingora 的运行时有两种 flavor(`pingora-runtime/src/lib.rs#L40-L83`):

- **`Steal`**:标准 Tokio 多线程 work-stealing runtime(`Builder::new_multi_thread()`)。task 可以被任意 worker 线程执行,work-stealing 调度。
- **`NoSteal`**:多个 Tokio `current_thread` runtime 池(`Builder::new_current_thread()` × N),**不做 work stealing**。`get_handle` / `current_handle` 随机选一个线程的 handle 来 spawn task(P5-15)。

**默认是 `work_stealing = true`(Steal)**(`pingora-core/src/server/configuration/mod.rs#L139`),但 **Cloudflare 生产环境关掉,用 NoSteal**。

#### 为什么 NoSteal 更适合 Pingora 的负载

Pingora 的负载特征(承接 P5-15 详拆):

- **每请求一个独立 task**,task 之间几乎无共享(每个连接独立处理)。
- **task 没有长尾**(代理请求处理快,几十毫秒级别)。
- **task 之间不需要协作**(没有 producer-consumer 链)。

在这种负载下,work-stealing 是**净开销**:

- 偷 task 要加锁(steal queue 是 lock-free 的,但跨线程访问仍有 cache 同步开销)。
- 偷过去要迁移 task 栈(复制 task 状态)。
- 跨线程执行破坏 cache 局部性(task 的数据在原线程的 cache 里更热)。

而 NoSteal 用 N 个 `current_thread` runtime:

- 每个 task 落在哪个线程就在哪个线程跑完,**不偷**。
- 单线程 runtime 没有跨线程同步开销(reactor / scheduler / timer 全是单线程的)。
- 既有多核能力(N 个线程),又保持单线程的高效。

**代价**:某个线程的 task 恰好都是重逻辑,这个线程会过载,其他线程帮不上忙。Pingora 的缓解:

- `offload_threadpool`:把 TCP+TLS 握手这种 CPU 密集活 offload 到独立线程池(P2-06)。
- `spawn_blocking`:在钩子里把重逻辑(WAF / 加密)offload 到 blocking 线程池。

#### 什么时候用 Steal(默认)

虽然 Cloudflare 用 NoSteal,但 Steal 适合**负载不均匀**的场景:

- task 之间有大长尾(有些请求几百毫秒,有些几秒)。
- task 之间有协作(子请求、pipeline)。
- 你不熟悉 NoSteal 的取舍,想用最稳妥的 Tokio 多线程。

Steal 是 Tokio 的默认,行为可预期(任意 task 可被任意线程执行),调试工具齐全。

#### 怎么选

| 你的场景 | 选哪个 | 怎么配 |
|---------|--------|--------|
| CDN / 高 QPS 代理(每请求独立 task) | NoSteal | 配置 `work_stealing: false`(Cloudflare 同款) |
| 网关 / 有长尾请求(某些请求很慢) | Steal(默认) | 配置 `work_stealing: true`(默认值,不用改) |
| 不确定 | Steal(默认) | 先用默认,压测后看是否切 NoSteal |

```yaml
# config.yaml
work_stealing: false   # 关掉 work-stealing,用 NoSteal(Cloudflare 同款)
# work_stealing: true  # 默认,标准 Tokio 多线程
threads: 0             # 0 = 自动按 CPU 核数
```

> **daemonize 的坑**:NoStealRuntime 的 pools 是**延迟初始化**的(`init_pools` 用 `OnceCell`),注释明说"Lazily init the runtimes so that they are created after pingora daemonize itself. Otherwise the runtime threads are lost."(`pingora-runtime/src/lib.rs#L112-L114`)。原因:`daemonize` 会 fork,fork 出的子进程只保留一个线程(调用 fork 的那个),其他线程的 runtime 就丢了。所以 Pingora 在 daemonize 之后才 init pools。**实操影响**:如果你自己改 Pingora 启动流程,别在 daemonize 前 spawn runtime 线程,否则 daemonize 后线程丢失。详见 P5-15。

### 选型二:TLS 四后端(openssl / boringssl / rustls / s2n)

Pingora 的 TLS 是四后端 feature flag 可插换(`pingora-core/Cargo.toml#L23-L30, L104-L107`):

```toml
# pingora-core/Cargo.toml#L104-L107
[features]
openssl = ["pingora-openssl", "openssl_derived"]
boringssl = ["pingora-boringssl", "openssl_derived"]
rustls = ["pingora-rustls", "any_tls", "dep:x509-parser", "ouroboros"]
s2n = ["pingora-s2n", "any_tls", "dep:x509-parser", "ouroboros", "lru"]
```

`openssl` 和 `boringssl` 共享 `openssl_derived`(都派生自 OpenSSL 系),`rustls` 和 `s2n` 走 `any_tls`(纯 Rust 或非 OpenSSL 系)。**编译期选一个,不能同时编译两个**。

#### 四后端怎么选

| 后端 | 谁的 | 特点 | 选它的场景 |
|------|------|------|-----------|
| **boringssl** | Google(Chrome/Chromium 用) | 性能最好,FIPS 合规可选,API 不稳定(无 semver) | **生产 CDN / 高性能**(Cloudflare 用这个),FIPS 合规场景 |
| **openssl** | OpenSSL 项目 | 生态最广(老牌),性能中等,API 稳定 | 传统场景,兼容性优先 |
| **rustls** | Rust 社区 | **纯 Rust**,内存安全(编译器防守),无 C 依赖 | 安全敏感场景,不想引 C 依赖 |
| **s2n** | AWS(s2n-tls) | FIPS 合规,AWS 主推,QUIC 原生 TLS | AWS 上云,合规场景,HTTP/3 准备 |

**Cloudflare 生产用 BoringSSL**(性能 + FIPS),Pingora 为此重写了 `pingora-boringssl/src/boring_tokio.rs` 把 BoringSSL 的同步 API 异步化(P5-16)。

#### 怎么配

在 `Cargo.toml` 里选 feature:

```toml
# 你的项目 Cargo.toml
[dependencies]
pingora = { version = "0.8.1", features = ["boringssl"] }  # 或 openssl / rustls / s2n
# pingora = { version = "0.8.1", default-features = false, features = ["rustls"] }
```

TLS 监听的代码不变(都是 `TlsSettings::intermediate`),feature flag 决定底层用哪个后端。

```rust
// 无论哪个后端,API 一样
let mut tls = pingora_core::listeners::tls::TlsSettings::intermediate(&cert, &key).unwrap();
tls.enable_h2();  // ALPN 协商 h2
proxy.add_tls_with_settings("0.0.0.0:443", None, tls);
```

> **选型建议**:不确定就选 `boringssl`(性能 + Cloudflare 验证过),要 FIPS 也选 boringssl 或 s2n,要纯 Rust 内存安全选 rustls,传统场景兼容性优先选 openssl。HTTP/3 还没原生集成(见 P7-20 第五节演展望),但选 s2n 为 HTTP/3 做准备。

### 选型三:连接池参数

`TransportConnector` 的连接池有几个关键参数(`pingora-core/src/connectors/mod.rs#L44-L82`):

| 参数 | 默认值 | 含义 | 调优方向 |
|------|--------|------|---------|
| `keepalive_pool_size` | **128**(`DEFAULT_POOL_SIZE`,`pingora-pool/src/lib.rs#L151`) | 每 PoolNode 的 keepalive 连接池大小 | 高 QPS / 多后端要调大;内存敏感调小 |
| `offload_threadpool` | `Some((2, 2))`(测试配置) | TCP+TLS 握手 offload 线程池(线程数, 队列深度) | TLS 握手多要调大;CPU 紧张调小 |
| `idle_timeout` | 框架管理(`idle_poll` 探测) | 空闲连接超时(被 `idle_poll` 探测后回收) | 长 keepalive 调大;防死连接调小 |
| h2 `H2_WINDOW_SIZE` | **8MB**(`1 << 23`,`connectors/http/v2.rs#L424`) | h2 stream / connection 初始窗口 | 大响应体调大;防流量滥用调小 |
| h2 `max_concurrent_streams` | **100**(`DEFAULT_MAX_CONCURRENT_STREAMS`,`v2/server.rs#L47`,downstream) | h2 单连接最大并发 stream | 高并发下游调大;防滥用调小 |
| h2 `max_header_list_size` | **64KB**(`DEFAULT_MAX_HEADER_LIST_SIZE`,`v2/server.rs#L46`) | h2 header list 上限(防 header 滥用) | 大 header 调大;安全敏感调小 |

#### keepalive_pool_size 调多少

默认 128 是"通用值"。生产调优:

- **高 QPS + 少后端**(比如 10 万 QPS 到 3 个 upstream):每后端 128 池可能不够,连接频繁建连/回收。调到 256 ~ 512。
- **多后端**(比如 1000 个 upstream):每后端 128 池 = 12.8 万连接,内存吃紧。调小到 32 ~ 64,或按"每后端 QPS × RTT"估算(`池大小 ≈ QPS_per_backend × RTT_seconds`)。
- **内存敏感**:调小(每连接占内存,128 个 keepalive 连接占 KB-MB 级)。

```yaml
# config.yaml
upstream_keepalive_pool_size: 256   # 默认 128,高 QPS 调大
```

#### offload_threadpool 调多少

`offload_threadpool` 把 TCP+TLS 握手 offload 到独立线程池(见 P2-06),避免建连的 CPU 开销拖累主 reactor:

- **TLS 握手多**(大量新连接):调大线程数(比如 (4, 4) = 4 线程 × 队列深度 4)。
- **CPU 紧张**(主 reactor 也忙):别调太大(offload 线程抢 CPU)。
- **纯内网**(TCP 握手快,无 TLS):可以关掉(`None`),省线程开销。

```yaml
upstream_connect_offload_threadpools: 4   # (threads, queue_depth) — 实际配置格式见 configuration
```

#### h2 流控调多少

h2 的 `H2_WINDOW_SIZE = 8MB`(upstream 侧,`connectors/http/v2.rs#L424`)和 `max_concurrent_streams = 100`(downstream 侧,`v2/server.rs#L47`)是默认值:

- **大响应体**(视频流 / 大文件):`H2_WINDOW_SIZE` 调大(避免流控阻塞),或者干脆用 h1。
- **高并发下游**(一个 client 开很多 stream):`max_concurrent_streams` 调大。
- **防滥用**(单个 client 不能占太多资源):两个都调小。

```yaml
# config.yaml(downstream h2 server 选项,如果暴露)
# 注意:downstream 的 h2 默认是 default_h2_options()(64KB header list + 100 streams)
# upstream 的 h2 是 H2Options::default() + initial_window_size(8MB)
```

> **0.8.1 安全加固**:`Bound default HTTP/2 server limits to mitigate memory exhaustion`(0.8.1 CHANGELOG)——downstream 侧的 h2 默认限制了 `max_header_list_size`(64KB)和 `max_concurrent_streams`(100),防 HTTP/2 Rapid Reset 类攻击(内存耗尽)。这是 `default_h2_options()` 做的(`v2/server.rs#L53-L58`),你定制 h2 options 时**别去掉这两个限制**,否则等于打开了攻击面。

> **test_reusable_stream 的价值**(`pingora-core/src/connectors/mod.rs#L379-L400`):连接池复用 keepalive 连接前,用 1 字节 unconstrained `now_or_never` 非阻塞读探测连接死活——读到字节(活但有数据,异常)/ EOF(死)/ 阻塞(活)。这避免了"复用了一条已被 upstream 关掉的死连接 → 502"的经典坑(见 P2-06)。这个探测**默认开启**,你不用配,知道它在防 502 就行。

---

## 第六节:调优手册——连接池 / h2 / 缓存 / TLS 调参表

搭好 + 选完型,进入调优阶段。这一节给一张调参表,按维度列参数、默认值、调优方向、坑点。

### 调参总表

| 维度 | 参数 | 默认值 | 调优方向 | 坑点 |
|------|------|--------|---------|------|
| **运行时** | `threads` | 0(自动 = CPU 核数) | CPU 密集负载调小,IO 密集调大 | NoSteal 模式下,线程数 = runtime 池数 |
| | `work_stealing` | `true` | CDN 场景关掉(false) | 关掉后单线程过载无救助 |
| | blocking 线程池 | Tokio 默认(512) | 钩子有重逻辑调大 | 别在钩子里同步阻塞,offload 到 blocking |
| **upstream 连接池** | `keepalive_pool_size` | 128 | 高 QPS 调大,多后端调小 | 池太大占内存,太小频繁建连 |
| | `offload_threadpool` | (2, 2) 测试 | TLS 握手多调大 | 调大抢 CPU |
| | `test_reusable_stream` | 开 | 不用动 | 防 keepalive 502,别关 |
| | `idle_poll` | 框架管理 | 不用动 | 探测空闲连接,自动回收死连接 |
| **h2(downstream)** | `max_header_list_size` | 64KB | 大 header 调大 | **别去掉**(防内存耗尽攻击,0.8.1 加固) |
| | `max_concurrent_streams` | 100 | 高并发下游调大 | **别去掉**(同上) |
| **h2(upstream)** | `H2_WINDOW_SIZE` | 8MB | 大响应体调大 | 调小防滥用,但影响吞吐 |
| | `max_concurrent_streams` | upstream 决定 | — | 受 upstream 限制 |
| **缓存** | `cache_key_callback` | **panic** | 必须自己实现 | 漏维度 = 投毒(见第七节) |
| | eviction(tinyufo) | LRU | 热 key 多调大 | cache miss 多可能是 LRU 太小 |
| | cache lock | 内置 | 防 cache stampede | 默认开,长响应可能锁超时 |
| | stale-while-revalidate | `should_serve_stale` | upstream 错误时返 stale | 默认 upstream 错才返,业务可调 |
| **TLS** | 后端 | 编译期选 | boringssl 性能,rustls 安全 | 选一个,不能同时编译 |
| | ALPN | h1/h2 协商 | enable_h2() | 不调 enable_h2 则只有 h1 |
| | 证书 | intermediate | — | 证书过期会让 TLS 握手失败 |
| **listener** | backlog | OS 默认 | 高并发调大 | SYN 积压,backlog 满丢连接 |
| | connection_filter | 无 | IP 黑白名单 | 同步阻塞,轻量 |

### 调优步骤

1. **基线压测**:用默认配置跑压测(wrk / vegeta / hyperfoil),记 QPS / p99 / CPU / 内存。
2. **找瓶颈**:
   - CPU 满 → 看是不是 TLS 握手(offload)/ 钩子重逻辑(spawn_blocking)/ work-stealing(切 NoSteal)。
   - p99 高 → 看是不是建连慢(keepalive 池不够)/ GC(无,但看 Arc 分配)/ 锁竞争。
   - 502 多 → 看连接池(死连接)/ upstream 健康(健康检查)/ 重试配置。
3. **一次调一个参数**,压测对比,记下效果。别一次调一堆(分不清哪个有用)。
4. **回归验证**:调完跑业务流量,看是否有副作用(比如调大池 → 内存涨,调小窗口 → 吞吐降)。

### 高频调优场景

**场景一:CPU 满了(主 reactor 上不去)**

- 看是不是 TLS 握手在主 reactor 上做 → 配 `offload_threadpool`。
- 看是不是钩子有重逻辑(WAF / 加密)→ 用 `tokio::task::spawn_blocking` offload。
- 看是不是 work-stealing 开销大 → 切 NoSteal(`work_stealing: false`)。

**场景二:p99 高(尾延迟)**

- 看是不是建连慢(连接池命中率低)→ 调大 `keepalive_pool_size`,看是否 `test_reusable_stream` 误判。
- 看是不是某个 upstream 慢 → 健康检查 / 熔断(钩子里基于 `Rate`/`Inflight` 实现)。
- 看是不是 NoSteal 单线程过载 → 切 Steal(`work_stealing: true`)。

**场景三:502 多**

- 看连接池死连接 → `test_reusable_stream` 应该防住,检查是不是关了。
- 看 upstream 健康 → 配主动健康检查(`TcpHealthCheck`)。
- 看重试配置 → `fail_to_connect` 里标 retry,`upstream_peer` 重试时换后端。

**场景四:内存涨**

- 看连接池太大 → 调小 `keepalive_pool_size`。
- 看缓存太大 → 调小 eviction(tinyufo LRU 容量)。
- 看 CTX 太重 → CTX 是每请求一个,重 CTX × 高并发 = 内存爆炸,CTX 保持轻量。

---

## 第七节:线上排查清单——症状、原因、排查、修复

这一节是实战手册的"急救包"。线上出问题时,按症状找原因,按原因排查,按原因修复。

### 排查清单总表

| 症状 | 可能原因 | 排查方法 | 修复 |
|------|---------|---------|------|
| **间歇性 502** | keepalive 复用死连接 / upstream 重启 | 看日志是否连接错误,看 `test_reusable_stream` 是否误判 | 确认 `test_reusable_stream` 开启;配主动健康检查;调 `fail_to_connect` retry |
| **持续 502** | upstream 全挂 / 连接池耗尽 / TLS 握手失败 | curl upstream 直连,看连接池状态,看 TLS 日志 | 修 upstream;调连接池大小;修证书 / TLS 后端 |
| **连接泄漏(fd 耗尽)** | 连接没归还池 / session 泄漏 | `lsof -p PID | wc -l` 看 fd 数,看是否持续涨 | 检查钩子是否 panic 导致连接没归还;调 fd ulimit;排查 keepalive 配置 |
| **HTTP smuggling** | content-length 校验失败(0.8.0 前默认不校验) | 发畸形 content-length 请求看是否被拒 | 升级到 ≥ 0.8.0(已加 content-length 校验);不自定义解析 |
| **缓存投毒** | `cache_key_callback` 漏维度 | 检查 cache key 是否包含 method/host/path/query/关键 header | 完善 `cache_key_callback`,包含所有影响响应的维度 |
| **限流失效** | `Rate` observe 调用位置错 / 决策逻辑错 | 打日志看 observe 返回值,看决策条件 | 在 `request_filter` 里 observe,超阈值 `Ok(true)` 短路 |
| **graceful upgrade 失败** | 信号错(发 SIGHUP)/ fd 传递失败 / daemonize 顺序错 | 确认发 SIGQUIT,看 transfer_fd 日志,看 daemonize 后 init pools | 发 SIGQUIT 不是 SIGHUP;确认 transfer_fd unix socket;daemonize 后才 init runtime pools |
| **p99 飙高** | NoSteal 单线程过载 / keepalive 池小 | 看单线程 CPU,看连接池命中率 | 切 Steal;调大 `keepalive_pool_size` |
| **内存涨** | 连接池大 / 缓存大 / CTX 重 | 看连接数 / 缓存大小 / CTX 大小 | 调小池 / 缓存;CTX 减负 |

### 症状一:502(Bad Gateway)

**症状**:downstream 收到 502,日志显示 upstream 错误。

**可能原因**:

1. **keepalive 复用了死连接**(最常见):upstream 把连接关了(超时 / 重启 / 负载均衡器踢了),但 Pingora 的池里还留着这条连接,下次请求复用它发请求 → upstream 回 RST / EOF → 502。`test_reusable_stream` 应该防住,但极端情况(连接在探测后、发请求前的瞬间被关)仍可能漏。
2. **upstream 全挂**:所有后端健康检查失败 / 网络分区。
3. **连接池耗尽**:并发请求超过池大小,新请求等不到连接。
4. **TLS 握手失败**:证书过期 / TLS 版本不匹配 / 后端选错。

**排查**:

```bash
# 1. 看 Pingora 日志,确认 502 的错误类型
journalctl -u my_proxy | grep -E "502|upstream error|connection"

# 2. 直连 upstream 看是否健康
curl -vk https://1.1.1.1:443/

# 3. 看连接池状态(如果有 metrics)
curl http://127.0.0.1:6192/  # pingora-prometheus 的 metrics 端口

# 4. 看 fd 数(连接泄漏?)
ls /proc/$(pidof my_proxy)/fd | wc -l

# 5. 看 upstream 健康检查日志
journalctl -u my_proxy | grep "health check"
```

**修复**:

- **keepalive 502**:确认 `test_reusable_stream` 没被关(它是默认行为,一般不会关)。配主动健康检查(`TcpHealthCheck`),坏的 upstream 不被选中。在 `fail_to_connect` 里标 retry,`upstream_peer` 重试时换后端。
- **upstream 全挂**:修 upstream。配主动健康检查 + 多后端(单点故障要避免)。
- **连接池耗尽**:调大 `keepalive_pool_size`,或加后端(分散连接)。
- **TLS 握手失败**:检查证书有效期(`openssl s_client -connect 1.1.1.1:443`),确认 TLS 后端 feature 匹配 upstream 的 TLS 版本。

### 症状二:连接泄漏(fd 持续涨)

**症状**:进程的 fd 数持续涨,最终 `Too many open files`,请求失败。

**可能原因**:

1. **钩子 panic 导致连接没归还**:`request_filter` / `upstream_peer` 里 panic,连接没走到 `release_stream`。
2. **session 泄漏**:长连接(h2)client 不发请求但保持连接,session 不释放。
3. **连接池配置问题**:`keepalive_pool_size` 过大 × 多后端,fd 累积。

**排查**:

```bash
# 1. 看 fd 数随时间变化
while true; do ls /proc/$(pidof my_proxy)/fd | wc -l; sleep 60; done

# 2. 看 fd 类型(是 TCP 连接还是文件?)
ls -la /proc/$(pidof my_proxy)/fd | awk '{print $NF}' | sort | uniq -c

# 3. 看连接的远端(是 client 还是 upstream?)
ss -p | grep $(pidof my_proxy) | awk '{print $5}' | cut -d: -f1 | sort | uniq -c

# 4. 看是否 panic
journalctl -u my_proxy | grep -i panic
```

**修复**:

- **panic 修复**:钩子里别 panic(用 `Result` 表达错误,`Err` 让请求失败但连接会正常归还)。如果用 `unwrap()`,改成 `?` 或显式处理。
- **session 泄漏**:配 h2 的 idle timeout(让空闲 h2 连接超时关闭),或调 `max_concurrent_streams` 限制。
- **连接池过大**:调小 `keepalive_pool_size` × 后端数,确保总连接数 < fd ulimit。
- **fd ulimit**:调高 `ulimit -n`(比如 100 万),但这是兜底,根因要查泄漏。

### 症状三:HTTP smuggling(请求走私)

**症状**:upstream 收到畸形请求,可能被注入恶意请求(request smuggling 攻击)。

**可能原因**:

1. **content-length 校验失败**:0.8.0 之前的 Pingora 默认不严格校验 content-length,攻击者发畸形 content-length(如 `Content-Length: 5\r\nContent-Length: 100`)绕过 framing。
2. **自定义 HTTP/1 解析**:如果你自己 fork 了 Pingora 的 h1 解析改了校验逻辑。

**排查**:

```bash
# 1. 发畸形 content-length 请求看是否被拒(0.8.0+ 应该拒)
printf "GET / HTTP/1.1\r\nHost: a\r\nContent-Length: 5\r\nContent-Length: 100\r\n\r\nhello" | nc 127.0.0.1 6188

# 2. 看 Pingora 版本(0.8.0+ 已加 content-length 校验)
my_proxy --version

# 3. 看 CHANGELOG:0.8.0 "Reject invalid content-length http/1 requests to eliminate ambiguous request framing"
```

**修复**:

- **升级到 ≥ 0.8.0**:Pingora 0.8.0 加了 content-length 校验(`Reject invalid content-length http/1 requests to eliminate ambiguous request framing`,CHANGELOG),严格拒绝歧义的 content-length。这是 HTTP/1 smuggling 防护。
- **不自定义 HTTP/1 解析**:用 Pingora 自带的(基于 `httparse`,已做 smuggling 防护),别自己 fork 改校验逻辑。

> **澄清一个常见误传**:有些资料提到 `RUSTSEC-2026-0034` 作为 smuggling 防护依据。**这个编号在 RUSTSEC 数据库里不存在**。Pingora 的 smuggling 防护实际是 0.8.0 的 content-length 校验(CHANGELOG 明文),不是某个 RUSTSEC 公告。0.8.1 的安全加固是 `Bound default HTTP/2 server limits to mitigate memory exhaustion`(HTTP/2 内存耗尽防护,与 smuggling 无关)。排查时以 CHANGELOG 为准,别信谣传的 RUSTSEC 编号。

### 症状四:缓存投毒

**症状**:用户 A 的请求命中了用户 B 的缓存(数据泄露),或者 GET 请求命中了 POST 的缓存。

**可能原因**:

1. **`cache_key_callback` 漏维度**:cache key 只 hash 了 path,没 hash method → POST 和 GET 共享缓存。
2. **cache key 漏了影响响应的 header**:比如上游按 `Accept-Language` 返回不同内容,但 cache key 没 hash `Accept-Language`。

**排查**:

```rust
// 检查你的 cache_key_callback 实现
fn cache_key_callback(&self, session: &Session, _: &mut Self::CTX) -> Result<CacheKey> {
    let req = session.req_header();
    // ❌ 错误:只 hash path
    // Ok(CacheKey::new("", req.uri.path(), ""))
    // ✅ 正确:method + host + path + query
    let key = format!("{}|{}|{}",
        req.method,
        req.uri.host().unwrap_or(""),
        req.uri.path_and_query().unwrap_or("").to_string(),
    );
    Ok(CacheKey::new("", &key, ""))
}
```

**修复**:

- **完善 `cache_key_callback`**:cache key 必须包含**所有影响 upstream 响应的维度**:
  - `method`(GET / POST / etc.)
  - `host`(虚拟主机)
  - `path` + `query`(资源定位)
  - 影响响应的请求 header(用 `cache_vary_filter` 处理 `Vary` header,见 P6-17)。
- **测试**:用不同 method / header 发请求,确认 cache key 不同。
- **默认 panic 是好事**:0.8.0 移除 `CacheKey::default`(默认 panic,`proxy_trait.rs#L156-L158`),强制你写安全的 key。别图省事自己写个不安全的 default。

### 症状五:限流失效

**症状**:配了限流,但超量的请求没被拒。

**可能原因**:

1. **observe 调用位置错**:不在 `request_filter` 里 observe,而在其他钩子(那可能已经过了 upstream)。
2. **决策逻辑错**:`observe` 返回当前窗口累计数,但你判断条件写反了(比如 `< MAX` 应该放行却写成 `> MAX` 拒绝,或反过来)。
3. **`Rate` 是 per-process 的**:多进程(NoSteal 多 runtime)下,每个 runtime 有自己的 `Rate`,限流不全局生效。

**排查**:

```rust
// 1. 打日志看 observe 返回值
let curr = RATE_LIMITER.observe(&key, 1);
log::debug!("key={} curr={} max={}", key, curr, MAX_REQ_PER_SEC);
if curr > MAX_REQ_PER_SEC { /* 拒绝 */ }

// 2. 确认 observe 在 request_filter 里(短路前)
async fn request_filter(&self, session: &mut Session, _: &mut ()) -> Result<bool> {
    let key = get_key(session);
    let curr = RATE_LIMITER.observe(&key, 1);  // ★ 在这里
    if curr > MAX_REQ_PER_SEC {
        // 拒绝 + 短路
        return Ok(true);
    }
    Ok(false)
}
```

**修复**:

- **observe 在 `request_filter`**:这是限流的正确位置(短路前)。
- **决策逻辑**:`observe` 返回当前窗口累计数,`> MAX` 就是超限,拒绝(`Ok(true)` 短路 + 写 429 响应)。
- **全局限流**:Pingora 的 `Rate` 是 per-process 的(`static` 全局变量,每进程独立)。NoSteal 模式下,每个 runtime 线程有自己的 `Rate`?不——`Rate` 用 `static Lazy`(进程级,所有线程共享一个),所以单进程内是全局的。但多进程部署(多个 Pingora 实例)要接 Redis / 外部共享存储做全局限流。这点和 Nginx(`limit_req_zone` 共享内存,但每 worker 独立)/ Envoy(thread-local,每 worker 独立)类似——**分布式限流三者都不内置**。

### 症状六:graceful upgrade 失败

**症状**:发了升级信号,但新进程没接管 / 旧进程没退出 / 连接断了。

**可能原因**:

1. **信号错**:发了 SIGHUP(Nginx 的信号),Pingora 不响应(Pingora 监听 SIGQUIT)。
2. **fd 传递失败**:`transfer_fd` unix socket 权限 / 路径问题。
3. **daemonize 顺序错**:NoSteal runtime 在 daemonize 前 init 了,fork 后线程丢失。

**排查**:

```bash
# 1. 确认信号是 SIGQUIT(不是 SIGHUP!)
kill -SIGQUIT $(pidof my_proxy)
# journalctl 应该看到 "graceful upgrade" 相关日志

# 2. 看 transfer_fd 日志
journalctl -u my_proxy | grep -i "transfer_fd\|fd\|upgrade"

# 3. 看新进程是否启动
ps aux | grep my_proxy   # 应该看到两个进程(旧的处理完连接后退出)

# 4. 看连接是否断(keepalive 不应该断)
curl -v http://127.0.0.1:6188/  # 升级期间发请求,应该正常
```

**修复**:

- **发 SIGQUIT**:`kill -SIGQUIT PID`。不是 SIGHUP(Nginx 的 reload 信号),不是 SIGTERM(graceful terminate,会关闭不是升级),不是 SIGINT(fast shutdown,立即停)。Pingora 的信号定义见 `pingora-core/src/server/mod.rs#L145-L166`:`SIGQUIT` = graceful upgrade,`SIGTERM` = graceful terminate,`SIGINT` = fast shutdown。
- **transfer_fd 路径**:`transfer_fd` 用 unix domain socket(`pingora-core/src/server/transfer_fd/mod.rs`),旧进程把 listener fd 通过 recvmsg 传给新进程。确认 socket 路径权限正确(两个进程都能访问)。
- **daemonize 后 init pools**:NoStealRuntime 的 pools 是延迟初始化的(`init_pools` 用 `OnceCell`,注释 `L112-L114`),必须在 daemonize 之后才 init。如果你改了启动流程,在 daemonize 前 spawn 了 runtime 线程,fork 后线程会丢失。Pingora 的 `Server::bootstrap` 已经处理了这个顺序,别自己改。

### 症状七:p99 飙高(尾延迟)

**症状**:平均延迟正常,但 p99 / p999 高(部分请求慢)。

**可能原因**:

1. **NoSteal 单线程过载**:某个 runtime 线程的 task 恰好都是重逻辑,过载,其他线程帮不上忙。
2. **keepalive 池小**:并发请求超过池大小,部分请求要新建连接(TCP+TLS 握手慢)。
3. **某个 upstream 慢**:个别后端响应慢,拖高 p99。

**排查**:

```bash
# 1. 看每个 runtime 线程的 CPU(NoSteal 模式)
top -H -p $(pidof my_proxy)   # 看线程级 CPU,是否某个线程 100%

# 2. 看连接池命中率(keepalive 复用率)
# 通过 pingora-prometheus metrics 看

# 3. 看 upstream 响应时间分布
curl -w "%{time_total}\n" -o /dev/null -s http://127.0.0.1:6188/
```

**修复**:

- **NoSteal 过载**:切 Steal(`work_stealing: true`),让 work-stealing 帮忙调度。或者用 `spawn_blocking` 把重逻辑 offload。
- **keepalive 池小**:调大 `keepalive_pool_size`,提高复用率。
- **upstream 慢**:配主动健康检查 + 熔断(钩子里基于 `Rate` / `Inflight` 标记慢后端)。

### 症状八:内存涨

**症状**:进程内存持续涨,最终 OOM。

**可能原因**:

1. **连接池大**:`keepalive_pool_size` × 后端数 × 每连接内存。
2. **缓存大**:tinyufo LRU 容量大,缓存太多。
3. **CTX 重**:CTX 每请求一个,重 CTX × 高并发 = 内存爆炸。
4. **连接泄漏**:见症状二。

**排查**:

```bash
# 1. 看进程内存
ps -o pid,rss,vsz -p $(pidof my_proxy)

# 2. 看连接数(连接池)
ss -p | grep $(pidof my_proxy) | wc -l

# 3. (开发时)用 valgrind / heaptrack 看 Rust 分配
```

**修复**:

- **连接池大**:调小 `keepalive_pool_size`。
- **缓存大**:调小 tinyufo LRU 容量。
- **CTX 重**:CTX 保持轻量(几个字段,大数据用 `Arc` 共享,不要每请求 copy)。
- **连接泄漏**:见症状二。

---

## 收束:这份实战手册的核心

这份附录把正文的零件装成了一台能跑、能调、能排查的代理。核心几件事:

1. **搭代理 = 实现三件套**(`type CTX` + `new_ctx` + `upstream_peer`)+ 在需要的钩子里写业务(`request_filter` 鉴权 / `upstream_request_filter` 改请求 / `response_filter` 改响应 / `upstream_peer` 里调 `LoadBalancer.select`)。代码贴近 `pingora/pingora-proxy/examples/` 下的真实示例。
2. **钩子开发**:`request_filter` 的 `Ok(true)` 是短路,`proxy_upstream_filter` 的 `Ok(true)` 是放行——**方向相反,API 瑕疵注意**。缓存前(`upstream_response_filter`)vs 缓存后(`response_filter`)选对。CTX 是 per-request,跨请求用 `Arc/Mutex`(`persist_connection_context` 不存在)。
3. **三向写法**:Nginx(配置)、Envoy(xDS / filter)、Pingora(Rust 代码)——选型看你的组织心智(运维 / 平台 / 开发者)。
4. **选型**:`work_stealing` 默认 `true`(Cloudflare 生产关掉用 NoSteal);TLS 四后端(boringssl 性能 / rustls 安全 / s2n 合规 / openssl 传统);连接池 `keepalive_pool_size` 默认 128,高 QPS 调大。
5. **排查**:502 看 `test_reusable_stream` + 健康检查;smuggling 看 content-length 校验(0.8.0+ 已防);cache 投毒看 `cache_key_callback`(必须自己实现,默认 panic);限流失效看 observe 位置 + 决策逻辑;upgrade 失败看 SIGQUIT(不是 SIGHUP)+ transfer_fd。

> **回扣全书**:这份附录是 P7-20(收束章)的实战落地。P7-20 讲"Pingora 在 Rust 异步栈的位置 + 三向对照总表",这份附录讲"怎么拿这些知识动手搭代理"。正文 20 章拆透的"为什么"(`ProxyHttp` 钩子链的灵魂、`TransportConnector` 连接池的招牌、Ketama 一致性哈希、自研 HTTP/1 + 委托 h2、`NoStealRuntime`、TLS 四后端、缓存),在这里变成"怎么用"。如果你正文读懂了,这份附录是你的速查手册;如果还没读,这份附录是入口,指路正文章节深入。
>
> 这本书的全部到此结束。附录 A 是源码全景路线图(读源码用),这份附录 B 是实战手册(搭代理用)。读完,你该能用 Pingora 从零搭一个生产反向代理,调到适合你负载的参数,并在出问题时快速排查。

---

> **本附录源码锚点(全部经本地 `../pingora/` Grep/Read 核实,版本 `v0.8.1` `719ef6cd54e40b530127751bab6c1afc5ae815a8`)**:
>
> - [ProxyHttp trait(~30 个钩子,最小三件套)](../pingora/pingora-proxy/src/proxy_trait.rs#L31-L46) —— `type CTX` @ L33,`new_ctx` @ L36,`upstream_peer -> Result<Box<HttpPeer>>` @ L42-L46。
> - [request_filter(Ok(true) = 短路)](../pingora/pingora-proxy/src/proxy_trait.rs#L68-L73) —— 默认 `Ok(false)`。
> - [proxy_upstream_filter(Ok(true) = 放行,API 瑕疵)](../pingora/pingora-proxy/src/proxy_trait.rs#L198-L207) —— 默认 `Ok(true)`,与 request_filter 方向相反。
> - [cache_key_callback(默认 panic,防投毒)](../pingora/pingora-proxy/src/proxy_trait.rs#L156-L158) —— `unimplemented!`,0.8.0 移除 `CacheKey::default`。
> - [upstream_response_filter(缓存前)vs response_filter(缓存后)](../pingora/pingora-proxy/src/proxy_trait.rs#L302-L328)。
> - [logging(收尾,即使短路也跑)](../pingora/pingora-proxy/src/proxy_trait.rs#L435-L439)。
> - [fail_to_connect / error_while_proxy(重试决策)](../pingora/pingora-proxy/src/proxy_trait.rs#L448-L479)。
> - [examples/load_balancer.rs(完整 LB 示例)](../pingora/pingora-proxy/examples/load_balancer.rs) —— `select(b"", 256)` @ L37。
> - [examples/rate_limiter.rs(限流示例)](../pingora/pingora-proxy/examples/rate_limiter.rs) —— `Rate::observe` @ L100。
> - [examples/gateway.rs(鉴权 + 改响应)](../pingora/pingora-proxy/examples/gateway.rs) —— `request_filter` 短路 @ L41-L52,`response_filter` @ L71-L88。
> - [examples/modify_response.rs(CTX + body filter)](../pingora/pingora-proxy/examples/modify_response.rs) —— `type CTX = MyCtx` @ L44,`response_body_filter` @ L87。
> - [examples/multi_lb.rs(多集群路由)](../pingora/pingora-proxy/examples/multi_lb.rs) —— 按 path 选 cluster。
> - [examples/use_module.rs(自定义 ACL module)](../pingora/pingora-proxy/examples/use_module.rs) —— `init_downstream_modules` @ L88。
> - [examples/connection_filter.rs(IP 过滤)](../pingora/pingora-proxy/examples/connection_filter.rs)。
> - [LoadBalancer::select(两参,key + max_iterations)](../pingora/pingora-load-balancing/src/lib.rs#L408) —— `select(&self, key: &[u8], max_iterations: usize)`。
> - [selection 算法(RoundRobin/Random/Ketama)](../pingora/pingora-load-balancing/src/selection/algorithms.rs) —— RoundRobin @ L38。
> - [DEFAULT_POOL_SIZE = 128](../pingora/pingora-pool/src/lib.rs#L151)。
> - [test_reusable_stream(1 字节探测)](../pingora/pingora-core/src/connectors/mod.rs#L379-L400) —— `unconstrained` + `now_or_never`。
> - [H2_WINDOW_SIZE = 8MB(1<<23)](../pingora/pingora-core/src/connectors/http/v2.rs#L424) —— upstream h2 窗口。
> - [default_h2_options(downstream h2 限制,0.8.1 加固)](../pingora/pingora-core/src/protocols/http/v2/server.rs#L46-L58) —— `max_header_list_size=64KB` + `max_concurrent_streams=100`。
> - [work_stealing 默认 true](../pingora/pingora-core/src/server/configuration/mod.rs#L139) —— Cloudflare 生产关掉用 NoSteal。
> - [Runtime enum(Steal vs NoSteal)](../pingora/pingora-runtime/src/lib.rs#L40-L83) —— `NoStealRuntime` 多 current_thread 池,`get_handle` 随机选。
> - [NoStealRuntime.daemonize 后 init pools(注释 L112-L114)](../pingora/pingora-runtime/src/lib.rs#L112-L152) —— fork 丢线程,故延迟 init。
> - [SIGQUIT graceful upgrade(不是 SIGHUP)](../pingora/pingora-core/src/server/mod.rs#L145-L166) —— SIGQUIT=upgrade,SIGTERM=terminate,SIGINT=fast shutdown。
> - [transfer_fd(listener fd 传递)](../pingora/pingora-core/src/server/transfer_fd/mod.rs) —— unix socket recvmsg 传 fd。
> - [TLS 四后端 feature flag](../pingora/pingora-core/Cargo.toml#L104-L107) —— `openssl`/`boringssl`/`rustls`/`s2n`。
> - [pingora-limits Rate(CMS + 双缓冲,只估算不限流)](../pingora/pingora-limits/src/rate.rs#L63-L140) —— `observe` @ L137,红/蓝 slot @ L65-L67。
> - [CHANGELOG 0.8.1(HTTP/2 内存耗尽防护)](../pingora/CHANGELOG.md) —— `Bound default HTTP/2 server limits`。
> - [CHANGELOG 0.8.0(content-length smuggling 防护 + cache_key 移除 default)](../pingora/CHANGELOG.md) —— `Reject invalid content-length http/1 requests` + `Remove CacheKey::default impl`。
>
> **修正凭印象的旧认知(本附录写作过程中核实并钉死)**:
>
> 1. **`RUSTSEC-2026-0034` 不存在**。排查 smuggling 讲 0.8.0 的 content-length 校验(CHANGELOG 明文),cache 投毒讲 0.8.0 移除 `CacheKey::default`(与 RUSTSEC 无关),0.8.1 的安全加固是 HTTP/2 limits(与 smuggling 无关)。
> 2. **`persist_connection_context` 不存在**。CTX 是 per-request,跨请求状态用 `Arc/Mutex` 放 struct 字段。
> 3. **`proxy_upstream_filter` 的 `Ok(true)` = 放行**(与 `request_filter` 的 `Ok(true)` = 短路方向相反),API 瑕疵,实操易记混。
> 4. **`LoadBalancer.select` 是两参**(`key: &[u8], max_iterations: usize`),示例 `select(b"", 256)`,无 P2C(TODO)。
> 5. **服务发现只内置 Static**,DNS 是 TODO,要动态发现自己 impl `ServiceDiscovery` trait。
> 6. **`work_stealing` 默认 `true`**(Steal),Cloudflare 生产关掉用 NoSteal,不是默认 NoSteal。
> 7. **graceful upgrade 信号是 SIGQUIT**(不是 Nginx 的 SIGHUP)。
> 8. **`cache_key_callback` 默认 panic**(不是有空实现),启用缓存必须自己实现,防投毒。
> 9. **`pingora-limits` 只估算不限流**,决策在钩子里;`pingora-prometheus` 内置在 pingora-core 不是独立 crate。
> 10. **`H2_WINDOW_SIZE = 8MB`**(upstream,`1<<23`),downstream h2 默认 64KB header list + 100 streams(0.8.1 加固)。
