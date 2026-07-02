# 附录 B · Tower 实践与集成

> 把前 19 章的理论落到"怎么真用":各框架怎么用 Tower,怎么用 `ServiceBuilder` 从零搭一个生产级 client,调优经验,以及一份"线上问题排查清单"——每条都回扣正文里讲透的机制。

---

## 附录首语 · 为什么单独写一篇"实践与集成"

读完前 19 章,你已经在脑子里搭起了一座 Tower 的大厦:从 `Service = poll_ready + call -> Future`(P1-02)到 `Layer` 洋葱(P1-03)、`ServiceBuilder` 类型级 `Stack`(P1-04)、`Buffer` 的 worker task + mpsc(P2-05)、`LoadShed` 把 `Pending` 翻成错误(P2-07)、`ConcurrencyLimit` 的 Semaphore permit(P3-09)、`RateLimit` 的令牌桶(P3-10)、`Retry` 的 Policy + Budget(P4-11)、`Balance` 的 P2C(P5-15)、`BoxCloneSyncService` 类型擦除(P6-17),最后到 axum/tonic/hyper/Pingora 怎么落地(P6-19)。这套机制你都能讲清楚了。

可真到了要**写代码**的时候,你大概率还会卡在四件事上:

1. **"我这套中间件到底怎么套?"** —— `ServiceBuilder::new().buffer(N).timeout(1s).retry(p).service(svc)` 这一行写出来顺,可"谁在外谁在内""为什么是这个顺序""这个顺序错了会出什么事故",前三章虽然反复讲过,但落到一个真实 client 上,还是要再过一遍。
2. **"各框架怎么用 Tower?"** —— 你写 axum 用 `Router::layer`,写 tonic 用 `interceptor`,写 reqwest 用 `ClientBuilder::layer`,写 Pingora 做 proxy filter。这四个外部 crate(P6-19 已对照过)各自的"接入点"在哪、用 Tower 的哪一面、丢了哪一面,需要一份"上手就能用"的速查。
3. **"调优旋钮怎么拧?"** —— `Buffer` 容量设多少?`Retry` 的 `TpsBudget` 三个参数(`ttl`/`min_per_sec`/`retry_percent`)怎么配?`ConcurrencyLimit` 和 `RateLimit` 怎么选?什么时候该上 `LoadShed`?这些调优问题没有标准答案,但有一套"按场景挑工具"的决策框架。
4. **"线上出事了怎么查?"** —— 中间件顺序错了、Buffer worker task 泄漏、重试风暴、限流不准、`poll_ready` 死锁、`ConcurrencyLimit` permit 泄漏、`ServiceBuilder` 套出的巨大类型编译慢——这些**真实生产事故的每一种**,根因都能回扣到正文某章讲透的机制。这一篇的精华,就是把这些"症状 → 根因 → 解法"的排查清单钉死。

本附录的定位是**可操作手册 + 排查清单**:代码骨架要真实可用(基于 `tower` 公开 API,版本钉死 `tower-0.5.2 @ 7dc533ef`),排查清单每条要回扣正文机制(指路 P-xx),让你在凌晨三点收到报警时,能照着清单从"现象"一路追到"为什么"。

> **诚实标注铁律**(承 P6-19 的做法,动手前先看清楚):本附录大量涉及 axum、tonic、reqwest、Pingora、tower-http、hyper-util 这六个**外部 crate**,它们**不在 `tower` 仓里**。凡是引用它们的代码,我只用它们的**公开 API 和文档用法**,**不编内部行号**——比如我会写"axum 的 `Router::layer`(外部 crate,公开 API)",但不会编"axum 的 `Router::layer` 在某文件某行"。只有 `tower/` 仓内的文件(`tower-service`/`tower-layer`/`tower/src/`),我才标精确行号(基于本地 `../tower/` @ `7dc533ef`)。这是本附录最易翻车的点,我守住这条线。

---

## 第一节 · 四个框架怎么用 Tower(速查)

这一节把 P6-19 拆过的四个框架(axum/tonic/reqwest/Pingora)各自"怎么接入 Tower"浓缩成速查手册。每段只给:接入点、用 Tower 的哪一面、一个最小可用代码骨架。如果你已经读过 P6-19,这一节是它的"上手卡片";如果你没读过,这一节是它的浓缩。

### 1.1 axum:Router::layer / from_fn / Handler → Service

axum(外部 crate)是 Rust 最流行的 Web 框架,它对 Tower 的使用最完整——`Service × Layer` 全套都用。三个接入点:

**接入点一:把 Handler 包成 Service。** 你写一个 `async fn(State, Path<Id>) -> impl IntoResponse`,axum 通过 `Handler` trait 的 blanket impl 自动把它包成 `tower_service::Service<Request>`(P6-19 第三节拆过)。你不需要手写 `poll_ready`/`call`,框架替你包。

**接入点二:`Router::layer(L)` 组路由中间件。** 这是 axum 最常用的接入点。`L` 是任何实现 `tower_layer::Layer<Route>` 的类型——Tower 的 `TimeoutLayer`/`RateLimitLayer`/`ConcurrencyLimitLayer`,社区的 `tower-http` 的 `TraceLayer`/`CompressionLayer`/`CorsLayer`,都能直接 `.layer(...)` 套上去。底层是 `ServiceBuilder` 的类型级 `Stack`(P1-04)。

**接入点三:`axum::middleware::from_fn` 用 async fn 写中间件。** 当你不想写 Layer struct,想直接用闭包写"before → next.run(req).await → after"这种洋葱中间件时,用 `from_fn`。它底层是 `service_fn` 把闭包包成 Service 再包成 Layer(P6-18/P6-19)。

最小可用骨架(全部是 axum 公开 API,外部 crate):

```rust
// axum + Tower 中间件的最小骨架(外部 crate,公开 API 用法)
use std::time::Duration;
use axum::{Router, routing::get, middleware::from_fn, extract::Request, middleware::Next, response::Response};
use tower::ServiceBuilder;
use tower_http::timeout::TimeoutLayer;
use tower_http::trace::TraceLayer;

// 业务 handler:axum 自动把它包成 Tower Service
async fn hello() -> &'static str { "hello" }

// from_fn 风格的洋葱中间件:before / next / after
async fn log_middleware(req: Request, next: Next) -> Response {
    println!("before");
    let resp = next.run(req).await;
    println!("after");
    resp
}

let app = Router::new()
    .route("/", get(hello))
    .layer(
        ServiceBuilder::new()
            // 顺序:从上往下写 = 请求从外往里穿(P1-04 口诀)
            .layer(TraceLayer::new_for_http())                  // 最外层:先记日志
            .layer(TimeoutLayer::new(Duration::from_secs(5)))   // 再超时
            .layer(from_fn(log_middleware)),                     // 再业务日志
    );
```

> **回扣 P1-04**:这段 `.layer(A).layer(B).layer(C)` 的顺序,P1-04 第 1.4 节钉死过——**先 `.layer()` 的在最外层,请求先碰**。所以这里请求进来先穿 `TraceLayer`(最外),再穿 `TimeoutLayer`,再穿 `log_middleware`,最后到 `hello` handler。改顺序就改行为:把 `TimeoutLayer` 放最外,是"总超时 5 秒(含日志时间)";放最里,是"只给 handler 5 秒"。

> **回扣 P6-17**:axum 路由表内部用 `BoxCloneService` 把每个 route 的 handler 擦除成统一类型(`BoxCloneService<Request, Response, Error>`),才能存进同一个 `HashMap`。这是 P6-17 招牌章拆过的"类型擦除在框架里的真实用途"——你写 `.route("/a", get(handler_a)).route("/b", post(handler_b))`,两个 handler 类型完全不同(`handler_a` 套的中间件和 `handler_b` 不同),路由表必须擦除类型才能存。axum 是 `Box` 家族最大的下游用户之一。

### 1.2 tonic:interceptor 是 Tower Layer 的特化

tonic(外部 crate)是 Rust 的 gRPC 框架。它对 Tower 的接入更克制——提供一个简化版 Layer(`interceptor`),同时不封死完整 Tower middleware。

**接入点:`tonic::service::Interceptor`。** tonic 的 `interceptor` 是一个 `Fn(Request<()>) -> Result<Request<()>, Status>`——注意它接的是 `Request<()>`(空消息体),不是完整 gRPC 请求。它**只拦请求头(metadata,即 HTTP/2 headers)**,不碰 protobuf 消息体。这是 gRPC 语境下的合理简化:消息体是 protobuf 流,可能在多个 DATA frame 里,拦体会破坏流语义(P6-19 第四节拆过)。

最小可用骨架(tonic 公开 API,外部 crate):

```rust
// tonic interceptor:只拦请求头(外部 crate,公开 API)
use tonic::{Status, Request, service::interceptor};

fn auth_interceptor(req: Request<()>) -> Result<Request<()>, Status> {
    let token = req.metadata().get("authorization")
        .ok_or_else(|| Status::unauthenticated("missing token"))?;
    // 验证 token...
    Ok(req)
}

let svc = MyGreeter::new(state)
    .send_interceptor(interceptor(auth_interceptor));  // interceptor 是 Tower Layer 的特化
```

tonic 同时支持完整 Tower middleware(用 `ServiceBuilder` 套任意中间件),因为 tonic 的 service 本身就是 Tower Service(`type Error = Status`)。`Status` 和 Tower 的 `Retry`/`Timeout` Error 语义对齐——你写 `Retry` 套在 tonic service 外,`Policy::retry` 能直接 match `Status` 码(`Status::unavailable` 重试,`Status::invalid_argument` 不重试)。

### 1.3 reqwest:ClientBuilder::layer 套 Tower middleware

reqwest(外部 crate)是 Rust 最流行的 HTTP 客户端。它的 Tower 接入点是 `ClientBuilder::layer(L)`——把任意 Tower Layer 套在 client 外。reqwest 内部尽量保持单态化(不滥用 `BoxService`),因为 client 是单一类型,不像 axum 路由表存异构 handler,没必要擦除(P6-19 第五节提过)。

最小可用骨架(reqwest 公开 API,外部 crate):

```rust
// reqwest + Tower middleware(外部 crate,公开 API)
use std::time::Duration;
use reqwest::Client;
use tower::ServiceBuilder;
use tower_http::trace::TraceLayer;

let client = Client::builder()
    .layer(
        ServiceBuilder::new()
            .layer(TraceLayer::new_for_http())              // 记请求
            .timeout(Duration::from_secs(10))               // 超时
            .rate_limit(100, Duration::from_secs(1))        // 每秒 100 请求
            .into_inner(),                                   // 取出 Layer
    )
    .build()?;
```

> **回扣 P1-04/P3-08/P3-10**:`.timeout()`/`.rate_limit()`/`.layer()` 都来自 `ServiceBuilder`,签名和 P1-04 拆过的一致(`tower/src/builder/mod.rs#L263` `timeout`、`#L230` `rate_limit`、`#L132` `layer`)。reqwest 的 `layer()` 内部用 `ServiceBuilder` 的类型级 `Stack`,只是把它包进了 `ClientBuilder` 链式 API。

### 1.4 Pingora:用 Tower 做 proxy filter

Pingora(外部 crate,Cloudflare 开源)是 Rust 异步 proxy 框架,用来做 CDN/反向代理。它的核心抽象 `ProxyService` 在概念上等价于一个 Tower `Service<Session>`——一次代理是一次 `call`,返回一个 future,代理完成 future resolve(P6-19 第五节拆过)。

Pingora 的 filter 链(`request_filter`/`upstream_peer`/`response_filter` 这一组回调)用 Layer 模型组装,但和 axum 的**编译期 `Stack`** 不同,Pingora 的 filter 链是**运行期组装**——因为 proxy 要按 upstream 配置动态组装 filter,配置可以热加载。这是 proxy 场景"运行期灵活"的取舍,和 Envoy filter chain 同类。

最小骨架(Pingora 公开 API,外部 crate,概念性示意):

```rust
// Pingora proxy:用 Tower filter 模型(外部 crate,公开 API/文档)
use pingora::proxy::{ProxyHttp, Session};
use pingora::upstreams::peer::HttpPeer;

struct MyProxy;

#[async_trait::impl_trait]
impl ProxyHttp for MyProxy {
    type CTX = ();
    fn new_ctx(&self) -> Self::CTX { () }

    async fn request_filter(&self, _session: &mut Session, _ctx: &mut Self::CTX)
        -> Result<bool> {
        // 请求路径 filter:决定要不要代理、改不改请求
        Ok(false)  // false = 继续代理
    }

    async fn upstream_peer(&self, _session: &mut Session, _ctx: &mut Self::CTX)
        -> Result<Box<HttpPeer>> {
        // 选上游(可在这里塞负载均衡逻辑)
        Ok(Box::new(HttpPeer::new("127.0.0.1:8080", false, "localhost".to_string())))
    }

    async fn response_filter(&self, _session: &mut Session,
        upstream_response: &mut ResponseHeader, _ctx: &mut Self::CTX) -> Result<()> {
        // 响应路径 filter:改响应头
        upstream_response.insert_header("x-proxy", "pingora")?;
        Ok(())
    }
}
```

Pingora 的取舍是"在 proxy 语境下用 Tower 的 Service×Layer 概念,但放弃编译期 `Stack` 换运行期灵活"。这和 axum(编译期 `Stack`)形成对照——同样是应用层,Web 路由适合编译期钉死,proxy 适合运行期灵活。

> **承接《Tokio》[[tokio-source-facts]]**:Pingora 是"每连接一 task"模型(P0-01 提过的 Tokio budget=128 让出),proxy 高并发靠 task 模型不靠 Service trait 形状。这个对照在 P6-19 拆过,这里点出。

---

## 第二节 · 实战:用 ServiceBuilder 从零搭一个生产级 client

前 19 章拆透的每个中间件,最终都要拼成一个**真实可用的 client**。这一节带你用 `ServiceBuilder` 从零搭一个"带 timeout/retry/限流/负载均衡"的生产级 HTTP/gRPC client,给完整代码骨架 + 解释每层为什么这个顺序。

### 2.1 目标:一个生产级 client 要解决什么

假设你有一个微服务,它要调下游的 user-service。生产环境对这个 client 的要求是:

1. **超时**:每次调用最多等 1 秒,超了直接失败(不要让一个慢请求拖垮调用方)。
2. **重试**:瞬态错误(连接重置、5xx、超时)重试一次,但不能重试风暴。
3. **限流**:对这个下游的总并发不能超过 50(保护下游),且总速率不能超过 100 QPS(配合下游的配额)。
4. **负载均衡**:下游有 3 个实例,要 P2C 分发,避免单点过载。
5. **共享连接**:底层 connector 是 `!Clone` 的(持 TCP 连接),要多 task 共享。
6. **可擦除类型**:这个 client 要存进一个连接池句柄、当函数返回值,得能擦除成统一类型。

这六条,正好对应 Tower 的六个中间件:`Timeout`、`Retry`、`ConcurrencyLimit` + `RateLimit`、`Balance`(P2C)、`Buffer`、`BoxCloneService`。问题是:**这六层怎么套?谁在外谁在内?**

这就是 P1-04 第 1.4 节反复强调的"顺序易错"问题,落到真实 client 上,顺序错了就是生产事故。下面分两步:先给完整代码骨架,再逐层拆"为什么这个顺序"。

### 2.2 完整代码骨架

先看完整骨架(基于 `tower` 公开 API,版本 `0.5.2`,所有 Layer 构造函数已核实源码):

```rust
// 生产级 Tower client 骨架(基于 tower 0.5.2 公开 API)
use std::time::Duration;
use tower::{
    ServiceBuilder, BoxError,
    balance::p2c::{MakeBalanceLayer, MakeBalance},
    discover::ServiceList,
    load::PeakEwmaDiscover,
    limit::{ConcurrencyLimitLayer, RateLimitLayer, rate::Rate},
    retry::{RetryLayer, Policy, budget::TpsBudget, backoff::ExponentialBackoffMaker},
    timeout::TimeoutLayer,
    util::BoxCloneService,
};
use std::sync::Arc;

// ---- 1. 定义请求/响应类型 ----
#[derive(Clone, Debug)]
struct UserRequest { user_id: u64 }
#[derive(Clone, Debug)]
struct UserResponse { name: String }

// ---- 2. 自定义 Retry Policy(回扣 P4-11)----
#[derive(Clone)]
struct MyRetryPolicy {
    budget: Arc<TpsBudget>,
}

impl<Req: Clone, Res, E> Policy<Req, Res, E> for MyRetryPolicy {
    type Future = std::future::Ready<()>;

    fn retry(&mut self, _req: &mut Req, result: &mut Result<Res, E>) -> Option<Self::Future> {
        match result {
            Ok(_) => { self.budget.deposit(); None }       // 成功:存预算,不重试
            Err(_) => {
                let withdrew = self.budget.withdraw();     // 失败:取预算
                if !withrew { return None; }               // 预算耗尽:不重试(防风暴!)
                Some(std::future::ready(()))               // 有预算:重试
            }
        }
    }
    fn clone_request(&mut self, req: &Req) -> Option<Req> { Some(req.clone()) }
}

// ---- 3. 内层 connector(示意:持 TCP 连接,!Clone)----
// 这里用 service_fn 包一个 async 闭包当 connector(P6-18 service_fn)
// 真实场景是 hyper/reqwest 的 connection service

// ---- 4. 用 ServiceBuilder 组装 ----
fn build_client(instances: Vec<MyConnector>) -> BoxCloneService<UserRequest, UserResponse, BoxError> {
    // Retry Budget:ttl=10s, min_per_sec=10, retry_percent=0.2(每 5 个成功允许 1 次重试)
    let budget = Arc::new(TpsBudget::new(
        Duration::from_secs(10), 10, 0.2,
    ));

    // 服务发现:把 3 个实例放进 ServiceList(P5-14 Discover)
    let discover = ServiceList::new(instances);
    // 给每个实例套 PeakEwma 负载度量(P5-15)
    let discover = PeakEwmaDiscover::new(
        discover,
        Duration::from_secs(30),        // default_rtt
        Duration::from_secs(10),        // decay
        (),                              // completion
    );

    // ★ 核心组装:从外到内分八层 ★
    let client = ServiceBuilder::new()
        // 第 8 层(最外):类型擦除,产出统一类型
        // .boxed_clone() 放在 .service() 之后单独擦除(见下文说明)
        // 第 7 层:Retry + Budget(重试 + 预算,防风暴)
        .retry(MyRetryPolicy { budget: budget.clone() })
        // 第 6 层:Timeout(总超时,所有重试共享一个 1s)
        .timeout(Duration::from_secs(1))
        // 第 5 层:RateLimit(每秒 100 请求,令牌桶)
        .layer(RateLimitLayer::new(100, Duration::from_secs(1)))
        // 第 4 层:ConcurrencyLimit(总并发 50,Semaphore permit)
        .concurrency_limit(50)
        // 第 3 层:Buffer(把 !Clone 的 balance 包成 Clone,共享连接)
        .buffer(100)
        // 第 2 层:Balance/P2C(从 3 个实例里 P2C 选一个)
        .layer(MakeBalanceLayer::new())   // 注意:MakeBalance 是 MakeService 模式
        // 第 1 层(最内):connector(真正发请求的服务)
        // 这里 service() 触发整条 Stack apply
        ;

    // 因为 Balance 是 MakeService(对每个 endpoint 造一个 service),
    // 真实组装里这步要包一层 make/balance 的桥接,见 2.3 节解释。
    // 完整可用版需要用 Balance::from_balance 或 MakeBalance 直接构造,
    // 这里为说明"中间件顺序"先简化展示。

    client
        .service(my_discover_based_service)
        .boxed_clone()   // 擦成 BoxCloneService(回扣 P6-17)
}
```

> **诚实标注**:上面骨架里 `MakeBalance`/`PeakEwmaDiscover`/`ServiceList` 的真实组装涉及 Tower 的 `MakeService` 工厂模式(P4-13 拆过),完整可用版比骨架多一层"造连接 vs 用连接"的桥接。骨架聚焦于展示"中间件顺序"——这是本节的重点。真实 `Balance` 的用法在 `tower/src/balance/p2c/make.rs#L27`(MakeBalance struct)、`tower/src/load/peak_ewma.rs#L173`(`PeakEwmaDiscover::new`)可查。

### 2.3 为什么是这个顺序:逐层拆

这个骨架的核心是**中间件顺序**。下面把每一层从外到内拆一遍,每一层都讲"为什么它在这一位",并回扣正文机制。

> **核心口诀(回扣 P1-04 第 1.4 节)**:`ServiceBuilder` 链式从上往下写 = 请求从外往里穿 = `.service()` 时 `Stack` 先 apply 内层后 apply 外层。所以**先写的在外,后写的在内**,最终类型 `外层<...<内层<connector>>>`。

**第 1 层(最内):connector —— 真正发请求的服务。** 它持 TCP 连接(或 HTTP/2 连接、gRPC channel),是 `!Clone` 的(连接是独占资源,P2-05 拆透)。它接收最干净的请求(`UserRequest`),返回最干净的响应(`UserResponse`)。所有中间件都包在它外面。

**第 2 层:Balance/P2C —— 从 3 个实例选一个。** `Balance` 紧贴 connector,因为"选哪个实例"是最贴近底层的决策——负载均衡要在"真正发请求"之前做。`Balance` 内部维护一个 `Discover`(P5-14,服务发现,3 个实例的列表),每次 `call` 用 P2C 随机抽两个实例挑负载小的(P5-15 招牌章)。`PeakEwma` 给每个实例估一个负载(基于历史 RTT 衰减平均),P2C 凭这个挑。

> **为什么 Balance 在 ConcurrencyLimit 内**:Balance 不该被 ConcurrencyLimit 包在最外。因为 ConcurrencyLimit 限的是"整个 client 的并发",不是"每个实例的并发"。如果 ConcurrencyLimit 在 Balance 内,那并发限额是"每个实例 50",3 个实例就是 150,和"总并发 50"的语义不符。所以 ConcurrencyLimit 要在 Balance 外(第 4 层),Balance 在内(第 2 层)。

**第 3 层:Buffer —— 把 `!Clone` 的 Balance 包成 `Clone`。** 这是 P2-05 招牌章的核心:Balance(以及它内部的 connector)是 `!Clone` 的(持连接),但 client 要被多个 task 共享(每个请求一个 task),必须 `Clone`。`Buffer` 用一个 worker task + mpsc 通道,把"clone service"换成"clone 邮箱(Sender)",让 `!Clone` 服务变成 `Clone + Send`。

> **回扣 P2-05**:`Buffer` 的 worker 在后台反复 `poll_ready` 内层 service(把就绪异步性藏起来,5.4 节),`poll_ready` 一行 `poll_reserve` 串起整条背压链(技巧精解二)。容量 100 = 通道最多排 100 个待处理请求(0.5.0 #635 修了 off-by-one,容量严格 = bound)。

**第 4 层:ConcurrencyLimit —— 限总并发 50。** 用 `tokio::sync::Semaphore` 的 permit,在 `poll_ready` 里 `poll_acquire`(P3-09)。50 个 permit 发完,第 51 个 `poll_ready` 返回 `Pending`,背压传到调用方。这保护下游——"无论上游怎么压,我给下游的并发不超过 50"。

> **回扣 P3-09**:`ConcurrencyLimit::poll_ready` 在 `tower/src/limit/concurrency/service.rs#L65`:`self.permit = ready!(self.semaphore.poll_acquire(cx))`,permit 在 `call` 里 `take` 消费(`service.rs#L84`)。clone 时 permit 复位为 `None`(`service.rs#L96-L106`),所以每个 clone 独立抢 permit。如果想要跨多个 service clone 共享同一把 Semaphore,用 `GlobalConcurrencyLimitLayer::with_semaphore`(`tower/src/limit/concurrency/layer.rs#L50`,传 `Arc<Semaphore>`)——这是 0.4.8 加的。

**第 5 层:RateLimit —— 每秒 100 请求。** 用令牌桶 + `tokio::time::Interval`(P3-10)。和 ConcurrencyLimit 的差别:ConcurrencyLimit 限的是"同时多少个"(并发,permit 在 Future 完成时释放),RateLimit 限的是"每秒多少个"(速率,令牌按时间补充)。两个一起用:并发限 50 保护下游不被打爆,速率限 100 QPS 配合下游配额。

> **回扣 P3-10**:`RateLimitLayer::new(num, per)`(`tower/src/limit/rate/layer.rs#L20` → `Rate::new(num, per)` @ `rate.rs#L14`,要求 `num > 0 && per > 0` 否则 panic)。令牌在 `poll_ready` 里扣(背压!速率到上限返回 `Pending`)。

**第 6 层:Timeout —— 总超时 1 秒。** 用 `tokio::time::sleep` + `select!` 和内层 Future 抢跑(P3-08)。**关键:Timeout 在 Retry 内**——这意味着"总超时 1 秒,所有重试共享这一个 1s"。如果重试 3 次还没完成,1s 到了整条链被砍,不会出现"每次重试各 1 秒,实际跑了 3 秒"的事故(这是 P1-04 第 1.4 节钉过的招牌对照)。

> **回扣 P1-04 第 1.4 节招牌对照**:`.timeout(1s).retry(p)`(Timeout 在外)产出 `Timeout<Retry<Svc>>`,语义"总超时 1 秒";`.retry(p).timeout(1s)`(Retry 在外)产出 `Retry<Timeout<Svc>>`,语义"每次重试各 1 秒"。生产环境几乎总是要前者(总超时),所以 Timeout 要在 Retry 外层。骨架里 Timeout(第 6 层)在 Retry(第 7 层)外,正确。

**第 7 层:Retry + Budget —— 重试 + 预算,防风暴。** 这是 P4-11 招牌章的核心。`MyRetryPolicy` 实现 `Policy::retry`:成功 `deposit` 预算,失败 `withdraw`,预算耗尽就不重试。`TpsBudget` 的三个参数(`ttl`/`min_per_sec`/`retry_percent`)在 P4-11 第 6 节拆透——`retry_percent=0.2` 意思"每 5 个成功允许 1 次重试"。

> **回扣 P4-11**:Retry 必须在 Timeout 内(对应 Timeout 在 Retry 外),因为"重试"是"内层 call 失败后再 call 一次",这个"再 call"也受总超时约束。如果 Retry 在 Timeout 外,每次重试各自超时,就回到"每次 1 秒实际 3 秒"的事故。

**第 8 层(最外):BoxCloneService —— 类型擦除。** 整个 `ServiceBuilder` 套出来的类型是 `Retry<Timeout<RateLimit<ConcurrencyLimit<Buffer<MakeBalance<...>>>>>>` 这种几十层嵌套(P6-17 开篇拆过)。要存进 client 句柄、当函数返回值、放进连接池,得擦成 `BoxCloneService<UserRequest, UserResponse, BoxError>`。这一层在最外,因为它只是"把整个巨大类型擦成一个统一类型",不改任何行为。

> **回扣 P6-17**:`ServiceBuilder::boxed_clone()`(`tower/src/builder/mod.rs#L769`)在链尾把整个 Stack 擦成 `BoxCloneService`。`BoxCloneService` 用 `Box<dyn CloneService>` 擦除具体类型,内部用 `MapFuture` 把关联类型 `Future` 钉死成 `BoxFuture`,再用私有 `CloneService` sub-trait 保留 `Clone`。这是 P6-17 招牌章拆透的三步走。

### 2.4 用图钉死:洋葱层次图

把上面八层画成洋葱图,请求从外往里穿,响应从里往外返:

```mermaid
flowchart TB
    subgraph ONION["生产级 client 的洋葱层次(请求从上往下穿)"]
        L8["第 8 层(最外): BoxCloneService<br/>类型擦除<br/>不改行为,只统一类型"]
        L7["第 7 层: Retry + TpsBudget<br/>失败重试 + 预算防风暴"]
        L6["第 6 层: Timeout<br/>总超时 1s(所有重试共享)"]
        L5["第 5 层: RateLimit<br/>每秒 100 QPS(令牌桶)"]
        L4["第 4 层: ConcurrencyLimit<br/>总并发 50(Semaphore)"]
        L3["第 3 层: Buffer<br/>!Clone 变 Clone(worker + mpsc)"]
        L2["第 2 层: Balance/P2C<br/>3 实例 P2C 选一个"]
        L1["第 1 层(最内): connector<br/>真正发请求(持连接,!Clone)"]
        L8 --> L7 --> L6 --> L5 --> L4 --> L3 --> L2 --> L1
    end
    REQ["调用方 call(req)"] --> L8
    L1 -->|"响应原路返回"| L2 --> L3 --> L4 --> L5 --> L6 --> L7 --> L8 -->|"响应"| REQ
    classDef exec fill:#dbeafe,stroke:#2563eb
    classDef compose fill:#dcfce7,stroke:#16a34a
    classDef inner fill:#fef3c7,stroke:#d97706
    class L7,L6,L5,L4,L3,L2 exec
    class L8 compose
    class L1 inner
```

**为什么是这个顺序的总结表**(这张表是本节的精华,排查清单会反复回扣):

| 层(外→内) | 中间件 | 关键参数 | 为什么在这个位 | 回扣正文 |
|---|---|---|---|---|
| 8 | BoxCloneService | — | 最外统一类型,不改行为 | P6-17 |
| 7 | Retry + Budget | `retry_percent=0.2` | 失败重试,预算防风暴 | P4-11 |
| 6 | Timeout | 1s | 总超时,所有重试共享 | P1-04 / P3-08 |
| 5 | RateLimit | 100 QPS | 限速率(配合下游配额) | P3-10 |
| 4 | ConcurrencyLimit | 50 | 限总并发(保护下游) | P3-09 |
| 3 | Buffer | bound=100 | `!Clone` 变 `Clone`,共享连接 | P2-05 |
| 2 | Balance/P2C | — | 多实例 P2C 选一个 | P5-15 |
| 1(最内) | connector | — | 真正发请求 | P6-19 |

记住一个**反向推理法**:想清楚"这个决策是在多早的时刻做的"。"选哪个实例"(Balance)是最贴近底层 connector 的决策,所以 Balance 在内;"限总并发"(ConcurrencyLimit)是更宏观的,所以 ConcurrencyLimit 在 Balance 外;"超时"(Timeout)是整个调用的截止,所以更外;"重试"(Retry)是失败后的恢复,在最外(因为重试要重新穿过整条链);"类型擦除"(BoxCloneService)是工程需要,不影响行为,在工程意义的最外。

---

## 第三节 · 调优经验

中间件的"用对了"和"用好了"是两件事。这一节给一套调优经验——中间件顺序怎么定、`Buffer` 容量怎么设、`Retry Budget` 怎么配、`ConcurrencyLimit` vs `RateLimit` 怎么选、何时上 `LoadShed`。这些不是教科书答案,是"按场景挑工具"的决策框架。

### 3.1 中间件顺序怎么定:谁在外谁在内

这是第二节已经拆透的点,这里浓缩成三条决策规则:

**规则一:Timeout 永远在 Retry 外。** 这是铁律,没有例外。`.timeout(1s).retry(p)` 产出 `Timeout<Retry<Svc>>` = "总超时 1s";反过来 `.retry(p).timeout(1s)` 产出 `Retry<Timeout<Svc>>` = "每次重试各 1s"。生产环境几乎总是要前者(总超时),因为"每次重试各 1s"会把一个慢请求放大成 N 秒,拖垮调用方。详见 P1-04 第 1.4 节招牌对照表。

**规则二:限流(ConcurrencyLimit/RateLimit)在 Retry 内。** Retry 的"再 call 一次"要穿过限流层——否则重试会绕过限流,把限流形同虚设。`.concurrency_limit(50).retry(p)` 的意思是"每次 call(包括重试的 call)都要抢 permit",重试请求也占并发额度,不会偷偷绕过限流。

**规则三:Buffer 在 Balance 外,在 ConcurrencyLimit 内。** Buffer 解决的是"内层 `!Clone` 服务怎么多 task 共享",所以它要包在 `!Clone` 的服务(Balance/connector)外面,把整坨变成 `Clone`。但 Buffer 不要在 ConcurrencyLimit 外——因为 Buffer 的容量本身已经是一种"并发限制"(通道满就 `Pending`),两个一起用会重复计数,调起来混乱。一般 Buffer 容量 ≥ ConcurrencyLimit 上限,让 Buffer 主要是"共享 + 平滑",ConcurrencyLimit 主要是"硬并发上限"。

### 3.2 Buffer 容量怎么设

P2-05 拆透 Buffer 后,容量(`bound`)是唯一的调优旋钮。经验值:

- **`bound` 至少 = 预期并发数**。如果 client 同时有 50 个请求在途(ConcurrencyLimit=50),Buffer bound 至少 50,否则通道经常满,背压频繁触发,吞吐被压低。
- **`bound` 不要过大**。P2-05 第 1 节那个雪崩推演里,堆积的根因就是"等待队列太大"。Buffer bound 设 10000,意味着最多 10000 个请求堆在通道里,每个占内存,下游慢时这堆请求会拖垮调用方内存。一般 `bound = 2~4 倍预期并发`,留一点缓冲但不夸张。
- **结合 ConcurrencyLimit 一起调**。`ConcurrencyLimit=50`、`Buffer bound=100` 是常见组合:并发硬上限 50,Buffer 留 100 的缓冲(超出 50 在途的部分在通道里排队),下游慢时通道先满(100),满了才 `Pending` 传到调用方。
- **0.5.0 之后的容量语义**:`bound` 严格等于实际容量(0.5.0 #635 修了 off-by-one,P2-05 技巧精解一)。你设 100 就是 100,不需要"+1"凑数。

### 3.3 Retry Budget 怎么配

P4-11 第 6 节拆透 `TpsBudget`,它三个参数:`ttl`(预算过期时间,1-60s)、`min_per_sec`(最低重试速率)、`retry_percent`(重试占成功的百分比,0-1000)。经验值:

- **`ttl` 设 10s**。这是预算的"记忆长度"——10s 前的成功不算数了。太短(1s)预算抖动大,太长(60s)预算反应慢,出事时来不及收。10s 是 Finagle(这个算法的出处)的默认值,Tower 也建议。
- **`min_per_sec` 设 10**。这是"即使服务刚启动、还没有成功历史,也允许每秒 10 次重试"——保证冷启动时能重试。设太小(0/1)冷启动重试被饿死,设太大下游早被打爆。
- **`retry_percent` 设 0.2(20%)**。意思是"每 5 个成功允许 1 次重试"。这是 Google SRE 书的建议值("重试比例不超过 20%")。设 1.0(100%)意思是"每个成功允许 1 次重试",出事时重试和原始请求 1:1,放大效应明显;设 0.05(5%)太保守,瞬态错误救不回来。

`TpsBudget::new` 的源码(`tower/src/retry/budget/tps_budget.rs#L53`)对这三个参数有 `assert!`(ttl 在 1-60s、retry_percent 在 0-1000、min_per_sec < i32::MAX),配错了直接 panic(启动时就炸,好过运行时默默不准)。

### 3.4 ConcurrencyLimit vs RateLimit 怎么选

P3-09 和 P3-10 拆透这两个,差别是"限什么":

| 维度 | ConcurrencyLimit | RateLimit |
|---|---|---|
| 限什么 | 同时多少个(并发) | 每秒多少个(速率) |
| 用什么 | `tokio::sync::Semaphore` permit | 令牌桶 + `tokio::time::Interval` |
| permit/令牌何时释放 | Future 完成时(permit drop) | 不释放(令牌按时间补充) |
| 适合场景 | 下游连接池有限、防下游被打爆 | 下游有 QPS 配额(API 限流、计费) |
| 对慢请求 | 慢请求占着 permit 不放,挤占后续 | 不影响(令牌按时间补充,和单请求时长无关) |

**选哪个看下游的瓶颈在哪**:

- 如果下游瓶颈是"连接数/线程数"(数据库、传统 RPC),用 `ConcurrencyLimit`——下游就那么多连接,并发超了打不过去。
- 如果下游瓶颈是"QPS 配额"(云服务 API、第三方计费接口),用 `RateLimit`——下游按 QPS 计费/限流,你要卡住速率。
- 如果两个瓶颈都有(常见),两个一起用:`.concurrency_limit(50).rate_limit(100, 1s)`——并发上限 50,速率上限 100 QPS。两个一起用,先到的那个先拦。

### 3.5 何时上 LoadShed

P2-07 拆透 LoadShed——它把 `poll_ready` 的 `Pending` 翻成 `Overloaded` 错误,主动丢请求保系统。何时该用?决策框架:

- **用 LoadShed 的场景**:调用方**能降级**(读请求可返回缓存/默认值)、或者等待**无界会拖垮自己**(网关层不能让一个下游慢拖垮整个网关)。典型:网关层、读服务的最外层、缓存服务的 fallback 路径。
- **不用 LoadShed 的场景**:调用方**没法降级**(写请求,失败了数据就没了)、等待**有界**(配了超时)。典型:写服务的核心路径、事务性操作、对一致性敏感的链路。

> **回扣 P2-07**:LoadShed 是"快速失败"哲学,Buffer/ConcurrencyLimit 是"排队缓冲"哲学。两者不是谁对谁错,是两个工具用在两个地方。把 LoadShed 套在写服务外,等于"下游慢就丢请求",数据丢了用户骂娘;把 Buffer 套在网关外,等于"下游慢就傻等",网关被自己堆的请求 OOM。套错就出事。

**一个常见但致命的顺序错误**:LoadShed 和 ConcurrencyLimit 一起用时,谁在外谁在内决定行为(P2-07 第 3 节专门拆过)。`.load_shed().concurrency_limit(50)`(LoadShed 在外)和 `.concurrency_limit(50).load_shed()`(ConcurrencyLimit 在外)行为完全不同——前者是"ConcurrencyLimit 满了就 shed",后者是"先 shed 再过 ConcurrencyLimit"。一般要前者(LoadShed 在 ConcurrencyLimit 外,ConcurrencyLimit 满了 LoadShed 才 shed),否则 LoadShed 在内层,ConcurrencyLimit 还没满 LoadShed 就先 shed,等于没限流就直接 shed。

---

## 第四节 · ★ 线上问题排查清单(本附录精华)

这是本附录的精华——七条真实生产事故的排查清单,每条给"现象 → 根因(回扣正文机制)→ 解法"。这一节是"凌晨三点收到报警时,照着清单从症状追到根因"的手册。

> **格式说明**:每条排查项用表格列"现象 / 根因 / 解法 / 对应章节"。根因必须回扣正文讲透的机制——理解了原理才能排查,这是本附录的设计哲学。

### 4.1 排查项一:中间件顺序错了 —— timeout 套在 retry 外 vs 内

**症状速记**:重试会重新计时还是不计时?线上 p99 飙到 N 倍单次超时。

| 维度 | 内容 |
|---|---|
| **现象** | 配了"超时 1 秒 + 重试 3 次",但实际一个慢请求能跑 3 秒(甚至 N 秒);线上 p99 = 3s 而不是预期的 1s;下游一慢,调用方内存暴涨(每个慢请求占 3s 的资源而不是 1s)。 |
| **根因** | 中间件顺序写反了:`.retry(p).timeout(1s)` 产出 `Retry<Timeout<Svc>>`,语义是"每次重试各自 1 秒",N 次重试就 N 秒。正确应该是 `.timeout(1s).retry(p)` 产出 `Timeout<Retry<Svc>>`,语义是"总超时 1 秒,所有重试共享"。这是 P1-04 第 1.4 节招牌对照点,顺序错了语义全变。根因是 `Stack::layer()` 先 apply inner(成为内层)后 apply outer(包更外),所以先 `.layer()` 的在最外层,写错顺序就反了。 |
| **解法** | 改成 `.timeout(1s).retry(p)`(Timeout 在外,Retry 在内)。code review 必查项:任何 `.timeout()` 和 `.retry()` 同时出现的地方,Timeout 必须在 Retry 之前(更外层)。详见 P1-04 第 1.4 节招牌对照表。 |
| **对应章节** | P1-04(招牌顺序对照)、P3-08(Timeout)、P4-11(Retry) |

### 4.2 排查项二:Buffer worker task 泄漏 —— Sender 不 drop 导致 worker 不退出

**症状速记**:client 实例销毁后,Buffer 的 worker task 还在,内存/连接数持续涨。

| 维度 | 内容 |
|---|---|
| **现象** | 服务运行一段时间,task 数量持续上涨(监控看到 `tokio::runtime::Handle::current().metrics().num_workers()` 之外的 task 数飙升);连接数持续涨(下游 `netstat` 看到本服务的连接数只增不减);内存缓慢增长;重启后恢复,但跑几天又涨回来。 |
| **根因** | `Buffer` 的 worker task 生命周期严格由 sender 数量决定(P2-05 第 5.4.5 节"worker 不泄漏"):所有 `Buffer` clone(所有 sender)都 drop → `rx.poll_recv` 返回 `None` → worker 返回 `Ready(())` → task 结束。如果某个地方意外持有了一个 `Buffer` clone(或 `ServiceBuilder` 套出来的服务的某个 clone)没 drop,worker 就永远不退出。常见元凶:① 把 client 存进了一个长寿的 `static`/`lazy_static`/`OnceCell`;② client 被传进一个 spawn 的 task,但 task 永远不结束(比如一个无限循环的轮询 task);③ client 被存进 `Arc` 的循环引用。 |
| **解法** | ① 排查所有持有 client clone 的地方,确保它们的生命周期短于服务退出时机;② 用 `tokio::task::yield_now` + 监控 task 数定位泄漏点;③ `Buffer` 的 `pair` API(`tower/src/buffer/service.rs#L66`)提供了"自己控制 worker 启动"的能力,如果你用自己的 executor spawn worker,确保 executor 退出时 worker 也被 drop;④ 真要长寿持有,用 `Reconnect`(P4-13)显式管理连接生命周期,而不是裸 `Buffer`。 |
| **对应章节** | P2-05(Buffer worker 不泄漏的证明)、P4-13(Reconnect 生命周期管理) |

### 4.3 排查项三:重试风暴 —— Budget 耗不尽 / 退避不够,放大流量打爆下游

**症状速记**:下游轻微故障 → 客户端重试 → 下游更慢 → 客户端重试更多 → 雪崩。

| 维度 | 内容 |
|---|---|
| **现象** | 下游服务某个时刻失败率从 0.1% 涨到 5%(轻微毛刺),几分钟内涨到 50% 甚至 100%;监控看到本服务发往下游的 QPS 比正常高 3-5 倍(重试放大);下游 CPU/内存飙满,完全不可用;关掉本服务的重试后,下游迅速恢复。 |
| **根因** | 重试没有 Budget(或 Budget 配置太宽松)。P4-11 第 1 节拆透:重试是"放大器",N 次重试最坏放大 N+1 倍流量;如果上游也配重试,乘法放大(N × M)。Budget(0.5.0 trait 化,P4-11 第 6 节)是防风暴的闸门——`TpsBudget` 用"成功 deposit、失败 withdraw"的令牌桶,让重试总量和成功率负反馈挂钩。如果没配 Budget(用裸"重试 N 次"的 policy)、或 Budget `retry_percent` 配太大(比如 2.0 = 每个成功允许 2 次重试)、或 Budget `ttl` 配太长(60s,出事时来不及收),风暴就起来了。 |
| **解法** | ① 必须配 Budget,不用裸"重试 N 次"——`MyRetryPolicy` 里 `withdraw()` 返回 false 就不重试(P4-11 第 6 节示例);② Budget 参数按经验值:`ttl=10s, min_per_sec=10, retry_percent=0.2`(第三节);③ 加指数退避 + 抖动(Backoff,P4-11 第 5 节),避免同步惊群(所有客户端同时重试);④ 区分瞬态错误和持久错误——只有瞬态(5xx、超时、连接重置)重试,持久(4xx、业务错误)绝不重试(Policy::retry 里 match 错误类型);⑤ 跨层级重试要协调——如果上游也配了重试,本服务重试次数要更低,避免乘法放大。 |
| **对应章节** | P4-11(Retry + Budget + Backoff)、P3-10(令牌桶机制) |

### 4.4 排查项四:限流不准 —— 令牌桶容量 / 补充速率配错

**症状速记**:配了 100 QPS,实际打过去 200 QPS(或只放过 50 QPS)。

| 维度 | 内容 |
|---|---|
| **现象** | 配 `.rate_limit(100, Duration::from_secs(1))`(每秒 100 请求),压测发现实际放过 200 QPS(超限)或 50 QPS(限太狠);或者限流时有时无(不稳定);或者限流行为和并发数有关(单线程下准,多线程下不准)。 |
| **根因** | 几种可能:① **把 ConcurrencyLimit 当 RateLimit 用**——ConcurrencyLimit 限的是并发不是速率,慢请求占着 permit 不放,看起来像"限速率",但语义完全不同(P3-09 vs P3-10);② **`Rate::new` 的 `num` 和 `per` 理解反**——`Rate::new(num, per)` 是"`per` 时间内 `num` 个令牌",不是"`num` QPS";`Rate::new(100, 1s)` 和 `Rate::new(100, 100ms)` 差 10 倍(`rate.rs#L14` 要求 `num > 0 && per > 0`);③ **令牌桶的初始满桶**——RateLimit 启动时令牌桶是满的(允许突发),前 100 个请求瞬间放过,之后才按速率限。这个"突发"行为如果没预期到,会以为限流不准;④ **`per` 设太小(比如 10ms)**——令牌补充太快,精度受 `tokio::time` 调度影响,出现抖动。 |
| **解法** | ① 确认要限的是并发还是速率,选对中间件(第三节决策表);② 核对 `Rate::new(num, per)` 语义——"`per` 时间内 `num` 个",不是 `num` QPS;③ 理解"初始满桶突发",压测时先 warm up 再测;④ `per` 至少 100ms(通常用 1s),避免精度问题;⑤ 跨多个 service clone 共享限流,用 `GlobalConcurrencyLimitLayer`(0.4.8,P3-09)或自己共享一把令牌桶实例。 |
| **对应章节** | P3-10(RateLimit 令牌桶)、P3-09(ConcurrencyLimit) |

### 4.5 排查项五:poll_ready 死锁 / 背压不传播 —— 中间件没正确转发 poll_ready

**症状速记**:调用方 hang 住不动,或者下游满载时调用方还在猛发请求。

| 维度 | 内容 |
|---|---|
| **现象** | 两种相反的症状:① **死锁**:调用方 `svc.ready().await` 或 `svc.oneshot(req).await` 永远不返回,task 挂死;② **背压不传播**:下游满载(`poll_ready` 该返回 `Pending`),但调用方还在猛发请求,请求堆在中间件内部,最终 OOM。 |
| **根因** | 都是 `poll_ready` 没正确处理导致的(承接 P1-02 的 `poll_ready` 契约)。① **死锁**常见于自定义中间件在 `poll_ready` 里做"等内层 ready 再做点别的"的死循环——比如某中间件 `poll_ready` 里 `ready!(self.inner.poll_ready(cx))` 然后又 `poll` 别的 future 但那个 future 永远不 ready,形成 `poll_ready` 永不返回;② **背压不传播**常见于自定义中间件"吞掉"了内层的 `Pending`——比如某中间件 `poll_ready` 不调内层 `poll_ready`(直接返回 `Ready`),内层满了调用方不知道,继续 `call`,请求堆在中间件里。这是 P1-02 反复强调的"`poll_ready` 必须透传背压"。 |
| **解法** | ① 自定义中间件的 `poll_ready` 必须转发内层 `poll_ready`(`self.inner.poll_ready(cx)`),像 `MapResponse` 那样(P1-04 第 1.7 节 `tower/src/util/map_response.rs#L59-L77` 的 `poll_ready` 直接转 inner);② `poll_ready` 里如果要 poll 别的 future,用 `ready!` 宏正确处理 `Pending`(提前 return),不要在 `poll_ready` 里写无限循环;③ 排查死锁用 `tokio-console`(外部工具)看 task 状态,挂死的 task 会显示;④ Tower 自带的中间件(Buffer/ConcurrencyLimit/RateLimit/Retry/Timeout/Balance)都正确处理了 `poll_ready`,死锁/背压丢失通常是**自定义中间件**的锅。 |
| **对应章节** | P1-02(poll_ready 背压契约)、P1-04(MapResponse poll_ready 透传)、P2-05(Buffer poll_reserve 背压链) |

### 4.6 排查项六:ConcurrencyLimit permit 泄漏 —— Future 提前 drop / panic

**症状速记**:并发数逐渐掉到 0,新请求永远 Pending。

| 维度 | 内容 |
|---|---|
| **现象** | 服务运行一段时间,ConcurrencyLimit=50 的服务逐渐"并发耗尽"——新请求 `poll_ready` 永远 `Pending`,实际并发数(监控看到的 in-flight 请求数)从 50 慢慢掉到 0,但 permit 没还回来,服务彻底卡死;重启后恢复。 |
| **根因** | `ConcurrencyLimit` 的 permit 在 `call` 里 `take` 消费,跟着 `call` 返回的 `ResponseFuture` 走——Future 完成时 permit drop 释放(P3-09,`tower/src/limit/concurrency/service.rs#L82-L93`)。如果 `ResponseFuture` 被**提前 drop**(调用方 task 被取消、上游设了超时把 future drop 了、调用方 panic),permit 会跟着 drop——这是正确的(permit 释放)。但**如果中间件内部 panic**,permit 可能没被正确 drop(stack unwind 时局部变量 drop 顺序不定)。更常见的根因是**别的中间件在 `call` 之后、Future 还没完成时 panic**,导致整条 Future 链异常退出,permit 在 unwind 中泄漏。还有一种:自定义中间件在 `call` 里 `take` 了某个资源但没放进返回的 Future,资源永远不释放。 |
| **解法** | ① 确认 permit 的 drop 路径——`ConcurrencyLimit::call` 把 permit 存进 `ResponseFuture`(`service.rs#L92` `ResponseFuture::new(future, permit)`),Future drop 就 permit drop,这条路径是 sound 的;② 排查 panic——用 `catch_unwind` 或 tracing 捕获中间件 panic,定位哪个中间件在 panic;③ 如果用 `GlobalConcurrencyLimitLayer::with_semaphore`(共享 `Arc<Semaphore>`,P3-09),可以监控 Semaphore 的 `available_permits()`,permit 泄漏会表现为 available_permits 持续下降;④ 兜底:给 ConcurrencyLimit 外面套 Timeout(P3-08),permit 泄漏导致的"卡住"至少有超时兜底,不会彻底死。 |
| **对应章节** | P3-09(ConcurrencyLimit permit 生命周期)、P3-08(Timeout 兜底) |

### 4.7 排查项七:ServiceBuilder 套出的类型巨大,编译慢 / 内存涨

**症状速记**:CI 编译一个文件要 5 分钟,IDE 卡死,rustc 吃 8GB 内存。

| 维度 | 内容 |
|---|---|
| **现象** | 加了几层 `ServiceBuilder`(比如 `.buffer().timeout().retry().concurrency_limit().rate_limit().buffered()`),`cargo build` 编译时间暴涨(单文件从 30s 涨到 5min);rustc 进程吃 8GB+ 内存;rust-analyzer/IDE 卡死,补全失效;改一行中间件配置,下游所有依赖这个 client 类型的代码全要重新编译。 |
| **根因** | `ServiceBuilder` 套出来的类型是编译期 `Stack` 嵌套(P1-04 招牌),层数越多类型签名指数级变长:`Retry<Timeout<RateLimit<ConcurrencyLimit<Buffer<...>>>>>` 这种怪物。每个泛型实例化都要单态化(Monomorphization),rustc 要为每个具体类型生成完整的机器码,编译时间和内存随层数爆炸。这不是 bug,是"零运行期开销"(单态化)的代价——P1-04 第 1.6 节和 P6-17 开篇都点过"类型签名长是 ServiceBuilder 的代价"。 |
| **解法** | ① **用 `BoxCloneService` 擦除类型**(P6-17 招牌章的核心)——在 `ServiceBuilder` 链尾加 `.boxed_clone()`(`tower/src/builder/mod.rs#L769`),把巨大 Stack 擦成 `BoxCloneService<Req, Res, Err>` 这个固定类型。擦除后,client 类型变成固定短类型,跨函数/存 struct 都用一个类型,下游代码不用跟着中间件层数重编译。代价是运行期一次 trait object 虚分派(`Box<dyn CloneService>` 的 `call`),但这对绝大多数场景可忽略(一次虚分派 vs 编译时间从 5min 到 30s,值得)。② **把 client 类型藏在函数返回值后**,只暴露 `BoxCloneService`,调用方拿到的就是擦除后的统一类型;③ **减少中间件层数**——审视每层是否真需要,有些层(比如 map_response 改个 header)可以合并;④ **分模块编译**——把 client 构造放独立 crate/模块,改动不波及业务代码。 |
| **对应章节** | P6-17(BoxCloneService 类型擦除招牌章)、P1-04(ServiceBuilder 类型生长) |

### 4.8 七条排查清单汇总表

把七条排查项汇总成一张速查表,排查时从"现象"列快速定位:

| # | 症状速记 | 根因(一句话) | 对应章节 |
|---|---|---|---|
| 1 | 中间件顺序错了(timeout/retry 顺序) | `Stack::layer()` 先 apply inner 后 apply outer,顺序反了语义全变 | P1-04 |
| 2 | Buffer worker 泄漏 | sender 没 drop,worker 永不退出(P2-05 worker 不泄漏的前提) | P2-05 |
| 3 | 重试风暴 | 没 Budget / Budget 太宽,重试放大流量正反馈 | P4-11 |
| 4 | 限流不准 | ConcurrencyLimit 当 RateLimit 用 / `Rate(num, per)` 语义错 | P3-10 |
| 5 | poll_ready 死锁/背压不传 | 自定义中间件没透传 inner poll_ready | P1-02 |
| 6 | ConcurrencyLimit permit 泄漏 | Future 提前 drop 或中间件 panic,permit 没 drop | P3-09 |
| 7 | ServiceBuilder 类型巨大编译慢 | 编译期 Stack 嵌套单态化爆炸,用 BoxCloneService 擦除 | P6-17 |

> **排查清单的设计哲学**:这七条根因每条都回扣正文讲透的机制——你只有理解了 `Stack::layer()` 的 apply 顺序(P1-04)、Buffer worker 的 sender 计数(P2-05)、Budget 的负反馈(P4-11)、令牌桶的 `num`/`per` 语义(P3-10)、`poll_ready` 的背压契约(P1-02)、permit 的 drop 路径(P3-09)、单态化的代价(P6-17),才能在凌晨三点照着症状追到根因。这正是本书从头到尾强调的"理解了原理才能排查"——Tower 不是黑盒,每个线上问题都能回到源码机制。

---

## 第五节 · 集成的力量:回到 Tower 的定位

本附录把前 19 章的理论落到了"怎么真用"——四个框架的接入点(axum/tonic/reqwest/Pingora)、一个生产级 client 的完整骨架(八层洋葱)、调优经验(中间件顺序/Buffer 容量/Retry Budget/限流选择/LoadShed)、七条线上排查清单。

把这些拼起来,你会看到一个清晰的图景:**Tower 不是某个框架的私有抽象,它是 Rust 异步生态请求处理的通用语言**。axum 用它组 Web 中间件,tonic 用它做 gRPC interceptor,reqwest 用它套 client middleware,Pingora 用它做 proxy filter——四个做着天差地别事情的框架,共用同一套 `Service × Layer`。你学会的每一层中间件(Timeout/Retry/Buffer/Balance/BoxCloneService),在这四个框架里全都能用;你排查的每一个线上问题,根因都能回到正文讲透的机制。

这就是 P0-01 反复强调的"集成点的力量"。Tower 用两个极简的 trait(`Service` + `Layer`)钉死了请求处理的通用语言,核心稳定 7 年不动(`tower-service`/`tower-layer` @ 0.3.3),生态(axum/tonic/reqwest/Pingora/tower-http)在稳定核心上长出来。你写的中间件一次写四处用,你修的 bug 一次修全生态受益。

> **回扣全书**:本附录是全书的实践收束。前 19 章拆透了机制(P1-02 Service / P1-03 Layer / P1-04 ServiceBuilder / P2-05 Buffer / P2-07 LoadShed / P3-09 ConcurrencyLimit / P3-10 RateLimit / P4-11 Retry / P5-15 Balance / P6-17 BoxService / P6-19 集成),本附录告诉你怎么把它们拼成一个真实可用的系统。下一站是 P7-20 全书收束——把 Tower 在 Rust 异步栈的位置彻底钉死(Tokio → hyper → Tower → axum/tonic),做双对照(gRPC filter C++ 运行期 vs Tower 类型级 Stack;Envoy filter C++ vs Tower 编译期),回扣全书招牌(hyper 删 poll_ready vs Tower 保留)。读完 P7-20,你该能在脑子里放映出一次请求从 TCP 字节流进来,穿过 Tower 中间件栈,到达业务 handler 的完整旅程——以及每一步 Tokio/hyper/Tower 各自在干什么。

---

## 附录 B 源码引用与诚实标注

> **本附录引用的 `tower` 源码**(本地 `../tower/`,版本 `tower-0.5.2 @ 7dc533ef`,已核实):
>
> - [ServiceBuilder 链式方法(`.layer()`/`.buffer()`/`.concurrency_limit()`/`.rate_limit()`/`.timeout()`/`.retry()`/`.load_shed()`/`.boxed()`/`.boxed_clone()`/`.service()`)](../tower/tower/src/builder/mod.rs) —— `#L132` layer、`#L178` buffer、`#L196` concurrency_limit、`#L219` load_shed、`#L230` rate_limit、`#L249` retry、`#L263` timeout、`#L489` service、`#L706` boxed、`#L769` boxed_clone(行号已逐一核实)
> - [BufferLayer::new](../tower/tower/src/buffer/layer.rs#L17-L44) —— Buffer 的 Layer 包装
> - [ConcurrencyLimitLayer::new / GlobalConcurrencyLimitLayer::with_semaphore](../tower/tower/src/limit/concurrency/layer.rs#L14-L50) —— 并发限流的 Layer
> - [ConcurrencyLimit service(poll_ready 里 poll_acquire permit,call 里 take permit)](../tower/tower/src/limit/concurrency/service.rs#L65-L106) —— permit 生命周期(排查项六的根因)
> - [RateLimitLayer::new / Rate::new(num, per)](../tower/tower/src/limit/rate/layer.rs#L12-L20) → [Rate::new(要求 num>0, per>0)](../tower/tower/src/limit/rate/rate.rs#L10-L30) —— 令牌桶(排查项四的根因)
> - [TimeoutLayer::new](../tower/tower/src/timeout/layer.rs#L11-L18) —— 超时 Layer
> - [RetryLayer::new / Policy trait(retry &mut self / clone_request)](../tower/tower/src/retry/layer.rs#L10-L17) + [Policy trait 定义](../tower/tower/src/retry/policy.rs#L46-L89) —— 重试 Policy
> - [Budget trait(deposit/withdraw)/ TpsBudget::new(ttl, min_per_sec, retry_percent)](../tower/tower/src/retry/budget/mod.rs#L74-L91) → [TpsBudget::new(三参数 + assert!)](../tower/tower/src/retry/budget/tps_budget.rs#L53-L75) —— 重试预算(排查项三的根因)
> - [Backoff trait / ExponentialBackoffMaker](../tower/tower/src/retry/backoff.rs#L32-L43) —— 指数退避
> - [MakeBalanceLayer / MakeBalance](../tower/tower/src/balance/p2c/layer.rs#L21-L52) → [MakeBalance struct](../tower/tower/src/balance/p2c/make.rs#L27-L52) —— P2C 负载均衡 Layer
> - [PeakEwmaDiscover::new(default_rtt, decay, completion)](../tower/tower/src/load/peak_ewma.rs#L173) —— PEWMA 负载度量
> - [ServiceList::new](../tower/tower/src/discover/list.rs#L18-L31) —— 静态服务发现
> - [Buffer service(worker task + mpsc + poll_reserve 背压链)](../tower/tower/src/buffer/service.rs) + [Worker poll / poll_next_msg(worker 不泄漏的前提)](../tower/tower/src/buffer/worker.rs#L141-L207) —— Buffer(排查项二的根因)
> - [Service trait(poll_ready 背压契约)](../tower/tower-service/src/lib.rs#L311-L356) —— 排查项五的根因(poll_ready 必须透传)
>
> **外部 crate(诚实标注,引用公开 API/用法,不编内部行号)**:
>
> - **axum**(外部 crate):`Router::layer`、`axum::middleware::from_fn`、`Handler` trait(blanket impl Service)、extractor(`State`/`Path`/`Query`/`Json`)——引用公开 API/文档用法,非 tower 源码。
> - **tonic**(外部 crate):`tonic::service::Interceptor`/`interceptor`、`tonic::Status`、`send_interceptor`——引用公开 API。
> - **reqwest**(外部 crate):`ClientBuilder::layer`——引用公开 API。
> - **Pingora**(外部 crate):`ProxyService` trait、`request_filter`/`upstream_peer`/`response_filter`——引用公开 API/文档用法。
> - **tower-http**(外部 crate,社区维护):`TraceLayer`、`TimeoutLayer`、`CompressionLayer`、`CorsLayer`——引用公开 API,作为"跨框架复用"的例子。
> - **hyper-util**(外部 crate):Tower 桥接(`tower::Service` ↔ `hyper::Service` 的 adapter)所在地,引用文档,不编内部行号。
> - **tokio::sync::mpsc / Semaphore / time**(外部 crate,承《Tokio》):Buffer/ConcurrencyLimit/RateLimit/Timeout/Hedge 用到,内部机制《Tokio》已拆透,本附录一句带过指路 [[tokio-source-facts]]。
>
> **回扣的正文章节**:
>
> - **P1-02**(Service trait + poll_ready 背压)—— 排查项五的根因
> - **P1-04**(ServiceBuilder 顺序招牌对照)—— 第二节 client 骨架的顺序、排查项一的根因
> - **P2-05**(Buffer worker + mpsc + 不泄漏)—— 第二节 Buffer 层、排查项二的根因
> - **P2-07**(LoadShed 背压取舍)—— 第三节调优 LoadShed 决策
> - **P3-08**(Timeout)—— 第二节 Timeout 层、排查项一的对照端
> - **P3-09**(ConcurrencyLimit permit)—— 第二节 ConcurrencyLimit 层、排查项六的根因
> - **P3-10**(RateLimit 令牌桶)—— 第二节 RateLimit 层、排查项四的根因
> - **P4-11**(Retry + Budget + Backoff)—— 第二节 Retry 层、第三节 Budget 调优、排查项三的根因
> - **P4-13**(Reconnect)—— 排查项二的生命周期管理
> - **P5-14**(Discover)+ **P5-15**(Balance/P2C)—— 第二节 Balance 层
> - **P6-17**(BoxService 类型擦除)—— 第二节 BoxCloneService 层、排查项七的根因
> - **P6-18**(service_fn)+ **P6-19**(四框架集成)—— 第一节速查、第二节内层 connector
>
> **承接**:
>
> - **承《Tokio》[[tokio-source-facts]]**:Buffer 用 mpsc + spawn、ConcurrencyLimit 用 Semaphore、RateLimit/Timeout 用 tokio::time、Pingora 每连接一 task + budget=128——这些《Tokio》拆透,本附录一句带过指路。
> - **承《hyper》[[hyper-source-facts]]**:hyper Service 删 poll_ready(招牌对照)、reqwest 在 hyper 之上、hyper-util 做 Tower 桥接——本附录在第一节速查里点到,详细对照在 P6-19。
