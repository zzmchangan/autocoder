# 第 2 章 · axum 全景:一次请求穿过哪些层

> **核心问题**:一次 axum 请求,从 `axum::serve` 在监听器上 accept 到一个 TCP 连接,到 handler 函数返回 `Response` 交回 hyper 写回对端,这条链上到底穿过了哪些层?`Router`、`MethodRouter`、`Handler`、`IntoMakeService` 这些名字,谁在前谁在后,各自干了哪一段活?
>
> **读完本章你会明白**:
>
> 1. `axum::serve(listener, router)` 这一行背后,accept 循环长什么样,每个连接是怎么被 `tokio::spawn` 成独立 task 的,以及为什么 axum 在这里要套一层 `IntoMakeService`(或者说,直接传 `Router` 时它自己怎么充当那层适配);
> 2. 一个请求进入 `Router::call` 之后,是怎样依次穿过 `PathRouter`(matchit 字典树匹配路径) → `MethodRouter`(按 HTTP method 选 handler) → `Handler::call`(宏展开的 tuple 提取器链)的,每一层把请求"加工"成什么样子传给下一层;
> 3. handler 的参数(`Path`、`Query`、`State`、`Json` 等)是怎么被 `FromRequestParts` / `FromRequest` 逐个从 `Request` 里抽出来的,为什么这套机制天生保证"body 只被消费一次",以及返回值是怎么经 `IntoResponse` 拼回 `Response` 的;
> 4. 这条链底下 hyper-util、Tower、Tokio 各自承担了哪一段(协议机、`Service` 抽象、运行时),对照 go net/http、actix-web 的全景有什么本质差别。
>
> 本章是**全景鸟瞰图**——后面的路由与分发(第 2 篇)、提取与响应(第 3 篇)每一章都是在这一章的某一段里往下钻。读不懂细节没关系,先把这条链在脑子里跑通一遍。
>
> **写给谁读(读者画像)**:你写过 `axum::serve(listener, Router::new().route("/", get(handler))).await`,但说不清"我传进去的 `Router` 到底是怎么变成 hyper-util 能用的 `Service` 的"、"我写的 `async fn handler(State<AppState>, Path<i32>) -> impl IntoResponse`,那一串参数到底在哪个时机被填充"。你能调通 axum,但 axum 内部那条链对你是个黑盒。本章就是来把黑盒拆成一条时序线。
>
> **前置知识**:假设你读过 P0-01(知道 axum 在 hyper+Tower 之上做了"路由 + 提取 + 响应"),熟悉 Rust 基本 trait/泛型/`async fn`,听说过 `Service` / `Future` / `Poll`。读过《hyper》和《Tower》最佳(没读过也行,本章会一句带过指路)。
>
> **逃生阀**:本章信息密度大,涉及 6 个以上的源码文件。如果某一段暂时绕晕你,记住一句话就够——**axum 的全景是"一个共享的 Router Service 服务所有连接,每个连接里用 matchit 选路径、按 method 选 handler、用提取器链拆 Request、用 IntoResponse 拼 Response"**。带着这句话先看第二节的全景时序图,再回头读细节。本章每个具体机制(matchit 双层、Handler 宏、FromRequest 二元划分)都有后续招牌章专讲,本章只放电影、不深挖。

---

## 一句话点破

> **axum 一次请求的全景,是 `hyper-util accept 连接 → 每个 spawn 一个 task → Router(一个共享的 Service)被克隆一份交给这个 task → matchit 匹配路径 → MethodRouter 匹配方法 → Handler::call(宏展开)把 Request 拆成 parts + body 逐个跑提取器链 → handler fn 执行 → IntoResponse 拼回 Response → 交回 hyper-util 写回对端`。`IntoMakeService` 在中间,是为了把"一个 Router 服务所有连接"翻译成"每连接拿一个 Service"的适配层——因为 hyper-util 的连接模型是后者。**

这是结论,不是理由。本章要倒过来拆:为什么 hyper-util 的连接模型要求"每连接一个 Service"?axum 的 Router 凭什么既是"一个共享 Service"又能"每连接克隆一份"?IntoMakeService 这层适配为什么不可省?matchit 和 MethodRouter 是怎么串起来的?Handler::call 的提取器链是怎么把 Request 一点点拆光的?这一章把这条链放电影一样放一遍。

---

## 第一节:从 `axum::serve` 说起——accept 循环长什么样

### 提问

一切的开端是这一行:

```rust
let listener = tokio::net::TcpListener::bind("0.0.0.0:3000").await.unwrap();
axum::serve(listener, router).await.unwrap();
```

这行代码背后,axum 做了什么?它怎么把一个 `Router` 和一个 `TcpListener` 拼成一个能跑的服务?

我们先把最外层拆开——`axum::serve` 函数本身的签名和它内部那个 accept 循环。这一节不深入路由、不深入 handler,只看"连接是怎么被接受、被交给谁的"。

### `axum::serve` 的签名:抽象出 `Listener` 和 `MakeService`

先看 `serve` 函数的签名(`axum/src/serve/mod.rs` L96-L109):

```rust
// axum/src/serve/mod.rs#L96-L109(逐字摘录)
#[cfg(all(feature = "tokio", any(feature = "http1", feature = "http2")))]
pub fn serve<L, M, S>(listener: L, make_service: M) -> Serve<L, M, S>
where
    L: Listener,
    M: for<'a> Service<IncomingStream<'a, L>, Error = Infallible, Response = S>,
    S: Service<Request, Response = Response, Error = Infallible> + Clone + Send + 'static,
    S::Future: Send,
{
    Serve {
        listener,
        make_service,
        _marker: PhantomData,
    }
}
```

信息量很大,逐条拆。这个签名暴露了 axum serve 设计的三件核心事情:

**第一件:监听器被抽象成 `Listener` trait,不绑死 `TcpListener`。** `L: Listener` 而不是 `L: TcpListener`,这意味着 axum 能接受任何实现 `Listener` 的东西。看 `Listener` trait 的定义(`axum/src/serve/listener.rs` L9-L24):

```rust
// axum/src/serve/listener.rs#L9-L24(逐字摘录)
pub trait Listener: Send + 'static {
    /// The listener's IO type.
    type Io: AsyncRead + AsyncWrite + Unpin + Send + 'static;

    /// The listener's address type.
    type Addr: Send;

    /// Accept a new incoming connection to this listener.
    ///
    /// If the underlying accept call can return an error, this function must
    /// take care of logging and retrying.
    fn accept(&mut self) -> impl Future<Output = (Self::Io, Self::Addr)> + Send;

    /// Returns the local address that this listener is bound to.
    fn local_addr(&self) -> io::Result<Self::Addr>;
}
```

`Listener` 只有两个关联类型(`Io` 是连接的字节流类型,`Addr` 是地址类型)和两个方法(`accept` 返回 `(Io, Addr)`,`local_addr` 返回本地地址)。注意 `accept` 的返回值是 `(Self::Io, Self::Addr)`,**不是 `Result`**——这意味着实现者要自己在 `accept` 里处理错误(重试或吞掉),不能把错误冒泡出去。看 `TcpListener` 的实现(`axum/src/serve/listener.rs` L26-L43):

```rust
// axum/src/serve/listener.rs#L26-L43(逐字摘录)
impl Listener for TcpListener {
    type Io = TcpStream;
    type Addr = std::net::SocketAddr;

    async fn accept(&mut self) -> (Self::Io, Self::Addr) {
        loop {
            match Self::accept(self).await {
                Ok(tup) => return tup,
                Err(e) => handle_accept_error(e).await,
            }
        }
    }

    #[inline]
    fn local_addr(&mut self) -> io::Result<Self::Addr> {
        Self::local_addr(self)
    }
}
```

`TcpListener::accept` 在 `axum::serve` 这层被包了一个 `loop`:遇到 `EMFILE`(打开文件数超限)这类瞬时错误,**不冒泡,而是 `handle_accept_error` 里 `tokio::time::sleep(Duration::from_secs(1))` 睡一秒重试**(`axum/src/serve/listener.rs` L140-L158)。注释直接引用了 hyper 0.14 的旧实现——这段逻辑本来就是从 hyper 继承来的。`#[cfg(unix)]` 还为 `tokio::net::UnixListener` 实现了 `Listener`(L46-L63),所以 `axum::serve` 能直接接 Unix domain socket。

> **钉死这件事**:axum 的 `Listener` 抽象让它能接 `TcpListener` / `UnixListener` / 自定义 IO 类型(测试里就用 `tokio::io::duplex` 造的假连接,见 `serve/mod.rs` L685-L727 的 `serving_on_custom_io_type` 测试)。`tap_io`(`ListenerExt::tap_io`,L66-L98)还能在每个 accept 的 IO 上跑一个闭包(比如给 `TcpStream` 设 `TCP_NODELAY`)。这是 axum 比 `hyper-util` 裸用更便利的一点——`hyper-util` 的 accept 循环要自己写,axum 把它包成了 trait。

**第二件:`make_service` 是一个 `Service<IncomingStream, Response = S>`——这是 hyper-util 连接模型的根。** `M: for<'a> Service<IncomingStream<'a, L>, Error = Infallible, Response = S>`。这一行是全书理解 axum serve 的钥匙。它的意思是:**你传进来的 `make_service` 不是一个直接处理请求的 Service,而是一个"每来一个连接,就调它的 `call`,吐出一个真正处理请求的 Service `S`"的工厂**。这正是 Tower 里 `MakeService` 的概念(承《Tower》P5-13,一句带过)——一个"Service 工厂",它的输入是连接元信息,输出是处理请求的 Service。

`IncomingStream`(`axum/src/serve/mod.rs` L424-L430)是这个工厂的输入:

```rust
// axum/src/serve/mod.rs#L424-L430(逐字摘录)
pub struct IncomingStream<'a, L>
where
    L: Listener,
{
    io: &'a TokioIo<L::Io>,
    remote_addr: L::Addr,
}
```

它把"这个连接的 IO 引用"和"这个连接的对端地址"打包,作为 `make_service.call(...)` 的参数。这个设计是给 `IntoMakeServiceWithConnectInfo` 用的——后者需要从对端地址造 `ConnectInfo<SocketAddr>`,塞进请求 extensions 让 handler 能提取(详见第 4 章 State 和 P5-17)。

**第三件:真正处理请求的 Service `S` 必须是 `Clone + Send + 'static`。** `S: Service<Request, Response = Response, Error = Infallible> + Clone + Send + 'static`。`Clone` 是关键——因为每个连接都要拿一份 `S`(每连接 spawn 一个 task,task 里要持有自己的 Service 实例)。`Error = Infallible` 意味着 axum 在框架层把所有错误都转成 `Response`(错误处理详见 P5-18),`Response = Response` 意味着 axum 的 Service 永远吐 `axum_core::response::Response`。`S::Future: Send` 让这个 future 能跨线程移动(承《Tokio》多线程调度器,一句带过)。

### 不这样会怎样:如果 hyper-util 直接接 `Router`

为什么要把 `Router` 包成 `MakeService`?为什么不让 hyper-util 直接拿一个 `Service<Request>` 用?

> **承接《hyper》**:hyper-util 的连接模型是"每连接独立跑 HTTP 协议机,协议机把字节流变成 `Request`,交给一个 Service 处理"(详见《hyper》P2-P3 的协议机、P4-P5 的连接管理)。`hyper-util::server::conn::auto::Builder::serve_connection` 的签名要求传入一个 `hyper::service::Service`(它删了 `poll_ready`,`call(&self)` 非 `&mut self`,承《Tower》P0-01 的对照)。这个 Service 在"这条连接的整个生命周期"内被复用——HTTP/1 的流水线、HTTP/2 的多 stream,都调同一个 Service 实例的 `call`。

hyper-util 的 `serve_connection(io, service)` 期望的是:给我一个 `Service`,`Service::call(&self, req)` 返回 `Future<Response>`。注意 hyper 的 Service 是 `call(&self)`(不可变借用,承《hyper》P1-02 的"删 poll_ready + `&self`"对照)——所以一个 `Service` 实例可以被同一条连接上的多个请求**并发调用**(HTTP/2 多 stream、HTTP/1 流水线)。

那为什么 axum 还要套一层 `MakeService`?**因为一个 `Service` 实例绑一条连接**。hyper-util 不持有"全局的 Service",它每 accept 一个连接,需要"造一个 Service 给这条连接"。如果你只有一个全局 `Router`,`Router` 又被某条连接独占持有(就算 `&self` 能并发,axum 也不希望让 hyper-util 那层知道 `Router` 的存在),axum 想做的事是:**我有一个全局的 `Router`,每来一个连接,我"克隆一份"交给这条连接的 task**。

这就是 `MakeService` 的角色——**把"一个共享的 Router"翻译成"每连接一个 Service 实例"**。`MakeService::call(连接元信息) -> Service`,每来一个连接调一次,吐出一个新的(克隆出来的)Service。axum 的 `Router<()>` 是 `Clone + Send + 'static`(内部 `Arc<RouterInner>`,克隆就是 `Arc::clone`,近乎零成本),所以"克隆一份给每条连接"在 axum 里是廉价的。

### 所以这样设计:accept 循环 + handle_connection

现在看 `serve` 内部那个 accept 循环。`Serve::run`(`axum/src/serve/mod.rs` L179-L193)是最简形态:

```rust
// axum/src/serve/mod.rs#L179-L193(逐字摘录)
async fn run(self) -> ! {
    let Self {
        mut listener,
        mut make_service,
        _marker,
    } = self;

    let (signal_tx, _signal_rx) = watch::channel(());
    let (_close_tx, close_rx) = watch::channel(());

    loop {
        let (io, remote_addr) = listener.accept().await;
        handle_connection(&mut make_service, &signal_tx, &close_rx, io, remote_addr).await;
    }
}
```

核心就这几行。一个无限循环:`listener.accept().await` 拿到 `(io, remote_addr)`,然后调 `handle_connection`。注意这里**没有并发 accept**——accept 是串行的,但 `handle_connection` 内部会 `tokio::spawn` 把"处理这条连接"变成独立 task,所以主循环只管"接客",实际"陪聊"是每个连接自己的 task。这是 Tokio 异步服务器的标准模式(承《Tokio》task 调度,一句带过)。

`with_graceful_shutdown` 版本的 `run`(L267-L305)多了一步:`tokio::select!` 让"accept"和"shutdown 信号"抢跑,信号来了就 `break` 跳出循环,然后 `drop(listener)` 不再接新连接,`close_tx.closed().await` 等所有现有连接 task 结束。这是优雅关闭的核心(`Serve::with_graceful_shutdown`),P5-17 详拆。

现在看真正干活的 `handle_connection`(`axum/src/serve/mod.rs` L353-L416)。这是 axum 把"accept 到的字节流"变成"跑起来的连接 task"的全部逻辑:

```rust
// axum/src/serve/mod.rs#L353-L416(逐字摘录,关键部分)
async fn handle_connection<L, M, S>(
    make_service: &mut M,
    signal_tx: &watch::Sender<()>,
    close_rx: &watch::Receiver<()>,
    io: <L as Listener>::Io,
    remote_addr: <L as Listener>::Addr,
) where
    // ... 约束同 serve 签名 ...
{
    let io = TokioIo::new(io);

    trace!("connection {remote_addr:?} accepted");

    make_service
        .ready()
        .await
        .unwrap_or_else(|err| match err {});

    let tower_service = make_service
        .call(IncomingStream {
            io: &io,
            remote_addr,
        })
        .await
        .unwrap_or_else(|err| match err {})
        .map_request(|req: Request<Incoming>| req.map(Body::new));

    let hyper_service = TowerToHyperService::new(tower_service);
    let signal_tx = signal_tx.clone();
    let close_rx = close_rx.clone();

    tokio::spawn(async move {
        #[allow(unused_mut)]
        let mut builder = Builder::new(TokioExecutor::new());
        // CONNECT protocol needed for HTTP/2 websockets
        #[cfg(feature = "http2")]
        builder.http2().enable_connect_protocol();

        let mut conn = pin!(builder.serve_connection_with_upgrades(io, hyper_service));
        let mut signal_closed = pin!(signal_tx.closed().fuse());

        loop {
            tokio::select! {
                result = conn.as_mut() => {
                    if let Err(_err) = result {
                        trace!("failed to serve connection: {_err:#}");
                    }
                    break;
                }
                _ = &mut signal_closed => {
                    trace!("signal received in task, starting graceful shutdown");
                    conn.as_mut().graceful_shutdown();
                }
            }
        }

        drop(close_rx);
    });
}
```

逐段拆,这是本章的核心源码:

**第一步:`TokioIo::new(io)`**。`tokio::net::TcpStream` 实现 `tokio::io::AsyncRead/AsyncWrite`,但 hyper-util 的 `serve_connection` 期望的是它自己的 IO trait(基于 `hyper::rt::Read/Write`)。`TokioIo` 是 hyper-util 提供的适配器,把 `tokio::io::AsyncRead/AsyncWrite` 适配成 hyper 的 IO trait。这一步是必要的桥接(承《Tokio》AsyncRead、承《hyper》rt::Read,一句带过)。

**第二步:`make_service.ready().await`**。这是 Tower 的 `ServiceExt::ready`(`tower::ServiceExt`,承《Tower》P1-02,一句带过),它内部 `poll_ready` 直到 `Ready`。`unwrap_or_else(|err| match err {})` 这种写法是因为 `Error = Infallible`,err 是不可达的,模式匹配空 `match` 把它消解掉——这是 Rust 处理 `Infallible` 的惯用法。

**第三步:`make_service.call(IncomingStream { io: &io, remote_addr }).await`**。**这就是"每连接调一次 `MakeService::call`"**。这一步会吐出一个 `S`(真正的请求处理 Service)。注意 `IncomingStream` 持有 `&io`(IO 的引用)和 `remote_addr`(对端地址拷贝,`SocketAddr` 是 `Copy`)——这个引用只在 `make_service.call` 期间存活,Service 造完就不用 IO 了(IO 后续交给 `serve_connection`)。

**第四步:`.map_request(|req: Request<Incoming>| req.map(Body::new))`**。这一步把"返回的 Service `S`"包装一下,让它能把 hyper 解析出来的 `Request<Incoming>`(body 类型是 `hyper::body::Incoming`)转换成 axum 的 `Request<axum_core::body::Body>`。`req.map(Body::new)` 是 `http::Request::map`,把 body 类型从 `Incoming` 换成 `Body`(axum 自己的 body 类型,包了 hyper 的 body,详见《hyper》Body as Stream 和本书 P3-12)。`map_request` 是 Tower 的工具(`tower::util::MapRequestLayer`,承《Tower》),给每个进入的 Request 套一个转换函数。

**第五步:`TowerToHyperService::new(tower_service)`**。**这一步是 axum 和 hyper-util 之间的最后一道桥**。axum 的 `tower_service` 是一个 `tower_service::Service<Request, ...>`(带 `poll_ready`、`&mut self`),但 hyper-util 的 `serve_connection` 期望的是 `hyper::service::Service`(**没 `poll_ready`**、`call(&self)`)。

> **承接《hyper》**:hyper 1.x 把自己的 Service trait 删了 `poll_ready`(`hyper/src/service/service.rs`,承《hyper》P1-02 招牌对照)。背压被挪到 HTTP 协议自身(HTTP/1 的 `in_flight` 单槽、HTTP/2 的 h2 流控,详见《hyper》)。hyper-util 提供 `TowerToHyperService` 这个适配器,把 `tower_service::Service`(带 `poll_ready` + `&mut self`)适配成 `hyper::service::Service`(无 `poll_ready` + `&self`)。它的实现是:在每次 `call(&self, req)` 时,内部 `clone` 一份 tower service,跑它的 `poll_ready`(Tower 的无条件 Ready,见下文),然后 `call`。

**第六步:`tokio::spawn(async move { ... })`**。**每个连接 spawn 一个独立 task**。task 里:
- `Builder::new(TokioExecutor::new())` —— hyper-util 的 `auto::Builder`,自动协商 HTTP/1 + HTTP/2(承《hyper》P5,一句带过);
- `builder.http2().enable_connect_protocol()` —— 开启 HTTP/2 CONNECT 协议(给 WebSocket over HTTP/2 用,详见《hyper》P2-07 + 本书 P5-19);
- `builder.serve_connection_with_upgrades(io, hyper_service)` —— hyper-util 的连接服务,把 IO 和 Service 拼起来,跑 HTTP 协议机;
- 外层 `loop { tokio::select! { conn | signal_closed } }` —— 让"连接处理"和"shutdown 信号"抢跑,信号来了调 `conn.graceful_shutdown()`;
- `drop(close_rx)` —— task 结束时 drop 掉 close_rx 的 receiver,让主循环的 `close_tx.closed()` 能感知到"所有 task 都结束了"(graceful shutdown 的收尾)。

> **钉死这件事**:`axum::serve` 的核心循环就是 `accept → make_service.call(连接元信息) → 拿到一个 Service → TowerToHyperService 适配 → spawn 一个 task 跑 hyper-util 的 serve_connection`。每条连接是一个独立 task,task 里跑 hyper-util 的 HTTP 协议机,协议机把字节流变成 `Request`,交给那个适配过的 Service 处理。这是 axum 全景的"上半场"——从字节流到 Request。下半场(Request 怎么穿过 Router 找到 handler)是下一节的事。

### 把上半场画成图

```mermaid
sequenceDiagram
    autonum
    participant OS as 操作系统 TCP
    participant L as TcpListener<br/>(Listener)
    participant MS as make_service<br/>(IntoMakeService / Router 直接)
    participant SP as tokio::spawn<br/>(每连接一 task)
    participant HU as hyper-util<br/>serve_connection
    participant R as Router::call<br/>(axum Service)

    OS->>L: 新连接到达
    L->>L: accept() 返回 (TcpStream, SocketAddr)
    L->>MS: make_service.ready().await (Tower ServiceExt)
    MS-->>MS: poll_ready 无条件 Ready
    L->>MS: make_service.call(IncomingStream { io, remote_addr })
    Note over MS: 克隆一份 Router<()> (Arc::clone, 近乎零成本)
    MS-->>L: 返回 tower_service: Service<Request>
    L->>L: map_request: Request<Incoming> → Request<Body>
    L->>L: TowerToHyperService::new (Tower Service → hyper Service)
    L->>SP: tokio::spawn(async move { ... })
    Note over SP: 主循环继续 accept 下一个连接
    SP->>HU: Builder::new(TokioExecutor).serve_connection_with_upgrades(io, hyper_service)
    Note over HU: hyper 协议机解析字节流<br/>HTTP/1 流水线 / HTTP/2 多 stream
    HU->>R: hyper_service.call(&self, Request)
    Note over R: 下半场开始:Router 处理 Request
```

这张图的上半部分(`OS → L → MS → SP → HU`)就是这一节的内容。下半场(`HU → R → ...`)从下一节开始。注意一个关键点:**`make_service` 在主循环里被反复 `call`,每次 `call` 吐出一个新的(克隆的)Service,这个 Service 在 task 里被 hyper-util 反复 `call` 处理这条连接上的每个请求**。`MakeService`(连接级工厂)和 `Service`(请求级处理)是两层,这是 axum 把"一个共享 Router"塞进 hyper-util 连接模型的核心机制。

---

## 第二节:`IntoMakeService`——为什么 Router 要先包一层

### 提问

上一节我们看到,`axum::serve` 期望第二个参数是 `M: Service<IncomingStream, Response = S>`——一个"每连接吐一个 Service"的工厂。可你写 `axum::serve(listener, router)` 时,直接把 `Router` 传进去了——`Router` 自己实现 `Service<IncomingStream>` 吗?还是 axum 偷偷帮你包了一层?

这一节拆 `IntoMakeService`,以及"直接传 Router"这种便利写法背后的机制。

### `IntoMakeService` 是什么

最直白的 `MakeService` 实现就是 `IntoMakeService`(`axum/src/routing/into_make_service.rs` L1-L44):

```rust
// axum/src/routing/into_make_service.rs#L1-L44(逐字摘录)
use std::{
    convert::Infallible,
    future::ready,
    task::{Context, Poll},
};
use tower_service::Service;

/// A [`MakeService`] that produces axum router services.
///
/// [`MakeService`]: tower::make::MakeService
#[derive(Debug, Clone)]
pub struct IntoMakeService<S> {
    svc: S,
}

impl<S> IntoMakeService<S> {
    pub(crate) fn new(svc: S) -> Self {
        Self { svc }
    }
}

impl<S, T> Service<T> for IntoMakeService<S>
where
    S: Clone,
{
    type Response = S;
    type Error = Infallible;
    type Future = IntoMakeServiceFuture<S>;

    #[inline]
    fn poll_ready(&mut self, _cx: &mut Context<'_>) -> Poll<Result<(), Self::Error>> {
        Poll::Ready(Ok(()))
    }

    fn call(&mut self, _target: T) -> Self::Future {
        IntoMakeServiceFuture::new(ready(Ok(self.svc.clone())))
    }
}

opaque_future! {
    /// Response future for [`IntoMakeService`].
    pub type IntoMakeServiceFuture<S> =
        std::future::Ready<Result<S, Infallible>>;
}
```

整个类型只有 57 行,逻辑极简:**`IntoMakeService<S>` 持有一个 `svc: S`,每次 `call(target)` 就 `self.svc.clone()` 返回一份克隆**。`poll_ready` 永远 `Ready`(因为克隆不需要准备资源)。`_target: T` 是泛型参数,`IntoMakeService` 不关心 target 是什么(连 ` IncomingStream` 都不看),它就是个"克隆工厂"。

> **钉死这件事**:`IntoMakeService<S>` 的全部行为就是"`call` 一次,克隆一份 `S` 返回"。它的存在纯粹是为了把"一个共享的 `S`"翻译成"`MakeService`(每连接一个 Service)"的形态,以适配 `axum::serve` / hyper-util 的连接模型。**它不做任何路由、不做任何提取,就是克隆**。这是"适配层"最纯粹的形态——存在意义就是签名匹配。

### 不这样会怎样:为什么不让 Router 直接当 MakeService

一个自然的疑问:`IntoMakeService` 这么薄(就克隆一下),为什么不直接给 `Router` 实现 `Service<IncomingStream>`?这样 `axum::serve(listener, router)` 就不用包一层了。

答案是:**axum 真的这么做了**。看 `axum/src/routing/mod.rs` L544-L567:

```rust
// axum/src/routing/mod.rs#L544-L567(逐字摘录)
// for `axum::serve(listener, router)`
#[cfg(all(feature = "tokio", any(feature = "http1", feature = "http2")))]
const _: () = {
    use crate::serve;

    impl<L> Service<serve::IncomingStream<'_, L>> for Router<()>
    where
        L: serve::Listener,
    {
        type Response = Self;
        type Error = Infallible;
        type Future = std::future::Ready<Result<Self::Response, Self::Error>>;

        fn poll_ready(&mut self, _cx: &mut Context<'_>) -> Poll<Result<(), Self::Error>> {
            Poll::Ready(Ok(()))
        }

        fn call(&mut self, _req: serve::IncomingStream<'_, L>) -> Self::Future {
            // call `Router::with_state` such that everything is turned into `Route` eagerly
            // rather than doing that per request
            std::future::ready(Ok(self.clone().with_state(())))
        }
    }
};
```

**`Router<()>` 直接实现了 `Service<IncomingStream<'_, L>>`**——这就是为什么 `axum::serve(listener, router)` 不需要 `.into_make_service()`。它的 `call` 做的事是 `self.clone().with_state(())`,注释解释得很清楚:"call `Router::with_state` such that everything is turned into `Route` eagerly rather than doing that per request"——克隆一份 Router 并调用 `with_state(())`,把内部所有 handler 都**预先**转成 `Route`(类型擦除的 Service,详见 P1-03),而不是每个请求都转一次。

那 `IntoMakeService` 还有用吗?**有用,用在显式 API**。看 `Router::into_make_service`(`axum/src/routing/mod.rs` L527-L532):

```rust
// axum/src/routing/mod.rs#L527-L532(逐字摘录)
#[must_use]
pub fn into_make_service(self) -> IntoMakeService<Self> {
    // call `Router::with_state` such that everything is turned into `Route` eagerly
    // rather than doing that per request
    IntoMakeService::new(self.with_state(()))
}
```

`router.into_make_service()` 显式返回 `IntoMakeService<Router<()>>`,这是给那些"想要显式 `MakeService` 类型"的场景用的(比如和 hyper-util 的其他 server API 集成、测试代码里要拿 `MakeService` 类型)。两种写法等价:

```rust
// 写法一:便利形态,Router 直接当 MakeService(用 L544 那个 impl)
axum::serve(listener, router.clone()).await?;

// 写法二:显式 MakeService
axum::serve(listener, router.into_make_service()).await?;
```

写法一的优势是简洁——你不用记 `.into_make_service()`。写法二的优势是类型明确(返回值是 `IntoMakeService<Router<()>>`,这个类型可以出现在函数签名、结构体字段里)。axum 给你两个开关,自己选。

> **钉死这件事**:`Router<()>` 在 axum 0.8 里**同时实现了两个 Service**:① `Service<IncomingStream>`(L549,给 `axum::serve(listener, router)` 直接传 Router 用);② `Service<Request<B>>` for `Router<()>`(L569,真正处理请求)。两个 impl 通过泛型参数 `L` / `B` 区分,这是 Rust 的 trait dispatch 静态分流的典型用法。这种"同一个类型实现多个 Service<不同 Request>"的模式在 axum 里反复出现(`MethodRouter` 也这么干),承《hyper》HTTP/1 client-server 共用 `Conn` 靠 trait 静态分流的同类思想(见 hyper-source-facts)。

### `IntoMakeServiceWithConnectInfo`:需要拿到对端信息时

`IntoMakeService` 不看 `target`(连 `IncomingStream` 的 `remote_addr` 都忽略)。但有时候 handler 需要知道"这个请求来自哪个 IP"——日志、限流、白名单都要对端地址。这时用 `IntoMakeServiceWithConnectInfo`(`axum/src/extract/connect_info.rs` L28-L46, L114-L133):

```rust
// axum/src/extract/connect_info.rs#L28-L46(逐字摘录)
pub struct IntoMakeServiceWithConnectInfo<S, C> {
    svc: S,
    _connect_info: PhantomData<fn() -> C>,
}

impl<S, C> IntoMakeServiceWithConnectInfo<S, C> {
    pub(crate) fn new(svc: S) -> Self {
        Self {
            svc,
            _connect_info: PhantomData,
        }
    }
}

// ...

// axum/src/extract/connect_info.rs#L114-L133(逐字摘录)
impl<S, C, T> Service<T> for IntoMakeServiceWithConnectInfo<S, C>
where
    S: Clone,
    C: Connected<T>,
{
    type Response = AddExtension<S, ConnectInfo<C>>;
    type Error = Infallible;
    type Future = ResponseFuture<S, C>;

    #[inline]
    fn poll_ready(&mut self, _cx: &mut Context<'_>) -> Poll<Result<(), Self::Error>> {
        Poll::Ready(Ok(()))
    }

    fn call(&mut self, target: T) -> Self::Future {
        let connect_info = ConnectInfo(C::connect_info(target));
        let svc = Extension(connect_info).layer(self.svc.clone());
        ResponseFuture::new(ready(Ok(svc)))
    }
}
```

和 `IntoMakeService` 的差别在 `call`:

1. `C::connect_info(target)` —— 用 `Connected` trait(`connect_info.rs` L80-L83)从 `target`(也就是 `IncomingStream`)里提取连接信息。对 `TcpListener`,`Connected<IncomingStream<'_, TcpListener>>` for `SocketAddr` 的实现(`connect_info.rs` L90-L94)就是 `*stream.remote_addr()`——直接拿对端地址。
2. `ConnectInfo(C::connect_info(target))` —— 包成 `ConnectInfo<C>` newtype(这是一个 Extension,会塞进 Request 的 extensions)。
3. `Extension(connect_info).layer(self.svc.clone())` —— 用 Tower 的 `Extension` Layer(承《Tower》,就是往 Request extensions 里塞一个值的 Layer)把 `ConnectInfo` 套在克隆出来的 Service 外面。这样 handler 里 `ConnectInfo<SocketAddr>` 提取器就能从 extensions 拿到对端地址。

用 `Router::into_make_service_with_connect_info::<SocketAddr>()` 显式声明你要拿什么类型的连接信息(P5-17 详拆)。本章只要知道:**这层 MakeService 比 `IntoMakeService` 多干一件事——把对端信息塞进 Request extensions,让下游 handler 能提取**。

### 三种 MakeService 形态的对照

把三种"把 Router 交给 axum::serve"的形态钉成一张表:

| 形态 | 类型 | 怎么用 | call 干什么 |
|------|------|--------|-------------|
| **直接 Router** | `Router<()>` 直接 impl `Service<IncomingStream>` | `axum::serve(listener, router)` | `self.clone().with_state(())` |
| **IntoMakeService** | `IntoMakeService<Router<()>>` | `axum::serve(listener, router.into_make_service())` | `self.svc.clone()` |
| **IntoMakeServiceWithConnectInfo** | `IntoMakeServiceWithConnectInfo<Router<()>, C>` | `axum::serve(listener, router.into_make_service_with_connect_info::<SocketAddr>())` | `Extension(ConnectInfo).layer(self.svc.clone())` |

三种形态的 `call` 都返回一个 `Service<Request>`(克隆的 Router,可能套了 Extension),交给 hyper-util 的 `serve_connection` 反复 `call` 处理这条连接上的每个请求。差别只在"call 时是否看 target、是否预处理"。

> **钉死这件事**:axum 在"Router 怎么交给 serve"这件事上给了三层便利:① 最简——直接传 Router(用 `Router<()>` 的 `Service<IncomingStream>` impl);② 显式 MakeService——`.into_make_service()`(给要拿 MakeService 类型的场景);③ 带连接信息——`.into_make_service_with_connect_info::<C>()`(给要拿对端 IP 的场景)。三层都是适配层,把"一个共享 Router"翻译成"每连接一个 Service"。**真正的请求处理逻辑(路由、提取、响应)全在 Service `S` 内部**,适配层不掺和。

---

## 第三节:一个 Request 穿过 Router 的全景

### 提问

上半场(`axum::serve` accept 连接、spawn task、跑 hyper-util 协议机)我们已经拆完了。现在假设 hyper-util 协议机解析好了一个请求,调 `hyper_service.call(&self, request)`,这个 `request` 接下来怎么穿过 axum 的内部分发,找到对应的 handler?

这就是全景的"下半场"。这一节把这条链放电影一样放一遍:Router → PathRouter → MethodRouter → Handler → 提取器链 → handler fn → IntoResponse。每一层只讲"它在这一步干了什么、把请求加工成什么传给下一层",深入细节(matchit 字典树、Handler 宏展开、FromRequest 二元划分)留给后续招牌章。

### 下半场全景时序图

```mermaid
sequenceDiagram
    autonum
    participant HU as hyper-util<br/>serve_connection
    participant TTH as TowerToHyperService<br/>(适配)
    participant R as Router<()>::call<br/>(Service<Request>)
    participant PR as PathRouter::<br/>call_with_state
    participant MT as matchit::Router<br/>(字典树 at)
    participant MR as MethodRouter::<br/>call_with_state
    participant H as Handler::call<br/>(宏展开 tuple)
    participant FRP as FromRequestParts<br/>(提取器链 1..n-1)
    participant FR as FromRequest<br/>(最后一个, 可能消费 body)
    participant HF as handler fn<br/>(async fn)
    participant IR as IntoResponse::<br/>into_response

    HU->>TTH: hyper_service.call(&self, Request<Incoming>)
    TTH->>R: clone + poll_ready(Ready) + call(req)
    Note over R: req.map(Body::new)<br/>Request<Incoming> → Request<Body>
    R->>PR: call_with_state(req, state=())
    Note over PR: 注入 OriginalUri 到 extensions
    PR->>PR: req.into_parts() → (parts, body)
    PR->>MT: node.at(parts.uri.path())
    alt 匹配成功
        MT-->>PR: Ok(Match { value: &RouteId, params })
        Note over PR: 用 RouteId 索引 Vec<Endpoint><br/>set_matched_path + insert_url_params
        PR->>MR: method_router.call_with_state(req, state)
        Note over MR: 按 req.method() 选 MethodEndpoint
        MR->>H: route.oneshot_inner_owned(req)<br/>或 BoxedHandler.into_route(state)
        H->>H: req.into_parts() → (parts, body)
        loop 对前 n-1 个提取器
            H->>FRP: T::from_request_parts(&mut parts, &state).await
            FRP-->>H: Ok(value) 或 Err(rejection)
            Note over H: rejection 直接 into_response 返回
        end
        H->>FR: req = from_parts(parts, body)
        H->>FR: LastT::from_request(req, &state).await
        FR-->>H: Ok(last) 或 Err(rejection)
        H->>HF: self(t1, t2, ..., tn).await
        HF-->>H: 返回值 Res
        H->>IR: Res::into_response(返回值)
        IR-->>H: Response
        H-->>MR: Response
        MR-->>PR: Response (via RouteFuture)
    else 匹配失败 (MatchError::NotFound)
        MT-->>PR: Err(MatchError::NotFound)
        Note over PR: 返回 Err((req, state))<br/>交给 fallback_router
        PR->>PR: fallback_router.call_with_state(req, state)
        Note over PR: 走 catch_all_fallback<br/>(P2-08 详拆)
    end
    PR-->>R: RouteFuture<Infallible>
    R-->>TTH: Future<Response>
    TTH-->>HU: Future<Response>
    HU->>HU: 协议机编码 Response 写回 IO
```

这张图是本章的核心。下面逐层拆解。

### 第一层:Router::call —— 把请求交给 PathRouter

hyper-util 协议机解析好一个请求,通过 `TowerToHyperService` 适配,最终调到 `Router<()>` 的 `Service<Request<B>>::call`(`axum/src/routing/mod.rs` L569-L588):

```rust
// axum/src/routing/mod.rs#L569-L588(逐字摘录)
impl<B> Service<Request<B>> for Router<()>
where
    B: HttpBody<Data = bytes::Bytes> + Send + 'static,
    B::Error: Into<axum_core::BoxError>,
{
    type Response = Response;
    type Error = Infallible;
    type Future = RouteFuture<Infallible>;

    #[inline]
    fn poll_ready(&mut self, _: &mut Context<'_>) -> Poll<Result<(), Self::Error>> {
        Poll::Ready(Ok(()))
    }

    #[inline]
    fn call(&mut self, req: Request<B>) -> Self::Future {
        let req = req.map(Body::new);
        self.call_with_state(req, ())
    }
}
```

三件事:

1. `poll_ready` **无条件 `Ready`** —— 这是 axum 的统一选择(承《Tower》P0-01 的对照,axum 不在框架层做背压,把背压交给 hyper-util 协议机的 HTTP 流控)。这一点 P1-03 详拆。
2. `req.map(Body::new)` —— 把 body 类型从 `B`(可能是 `hyper::body::Incoming` 或其他)统一转换成 `axum_core::body::Body`。`Body::new` 把 hyper 的 body 包一层。
3. `self.call_with_state(req, ())` —— `Router<()>` 的 state 是 `()`,所以直接传 `()`。**真正的路由逻辑在 `call_with_state` 里**。

`Router::call_with_state`(`axum/src/routing/mod.rs` L417-L432)是 axum 路由分发的总入口:

```rust
// axum/src/routing/mod.rs#L417-L432(逐字摘录)
pub(crate) fn call_with_state(&self, req: Request, state: S) -> RouteFuture<Infallible> {
    let (req, state) = match self.inner.path_router.call_with_state(req, state) {
        Ok(future) => return future,
        Err((req, state)) => (req, state),
    };

    let (req, state) = match self.inner.fallback_router.call_with_state(req, state) {
        Ok(future) => return future,
        Err((req, state)) => (req, state),
    };

    self.inner
        .catch_all_fallback
        .clone()
        .call_with_state(req, state)
}
```

`RouterInner`(`mod.rs` L80-L85)持有三个分发组件:`path_router`(主路由)、`fallback_router`(默认 fallback,404)、`catch_all_fallback`(用户自定义的全局 fallback)。`call_with_state` 的逻辑是:**先试主路由,匹配失败(返回 `Err`)就试 fallback_router,fallback_router 也"失败"(理论上不会,它注册了 catch-all `/{*fallback}`)就走 catch_all_fallback**。

注意这个 `match` 模式:`path_router.call_with_state` 返回 `Result<RouteFuture, (Request, S)>`。`Ok(future)` 表示匹配到了,直接 `return`;`Err((req, state))` 表示没匹配(把 `req` 和 `state` 原样还回来,给下一个尝试用)。这种"返回 `Result`,Err 带回输入"的模式在 axum 里反复出现,是"链式尝试"的优雅写法——**没有提前丢弃 Request,而是把它"传递"给下一个候选**。这一点 P2-08 fallback 章会详拆。

`RouterInner` 的结构(`mod.rs` L80-L85):

```rust
// axum/src/routing/mod.rs#L80-L85(逐字摘录)
struct RouterInner<S> {
    path_router: PathRouter<S, false>,
    fallback_router: PathRouter<S, true>,
    default_fallback: bool,
    catch_all_fallback: Fallback<S>,
}
```

`PathRouter<S, false>` 是主路由(常量泛型 `IS_FALLBACK = false`),`PathRouter<S, true>` 是 fallback 路由(常量泛型 `true`)——同一个类型用常量泛型区分两种用途,这是 Rust 编译期分流的技巧。`default_fallback` 标记"用户有没有显式设过 fallback",`catch_all_fallback` 是用户自定义的兜底 Service。

### 第二层:PathRouter::call_with_state —— matchit 字典树匹配

主路由 `path_router.call_with_state(req, state)`(`axum/src/routing/path_router.rs` L371-L420)是 axum 路径匹配的核心:

```rust
// axum/src/routing/path_router.rs#L371-L420(逐字摘录)
pub(super) fn call_with_state(
    &self,
    #[cfg_attr(not(feature = "original-uri"), allow(unused_mut))] mut req: Request,
    state: S,
) -> Result<RouteFuture<Infallible>, (Request, S)> {
    #[cfg(feature = "original-uri")]
    {
        use crate::extract::OriginalUri;

        if req.extensions().get::<OriginalUri>().is_none() {
            let original_uri = OriginalUri(req.uri().clone());
            req.extensions_mut().insert(original_uri);
        }
    }

    let (mut parts, body) = req.into_parts();

    match self.node.at(parts.uri.path()) {
        Ok(match_) => {
            let id = *match_.value;

            if !IS_FALLBACK {
                #[cfg(feature = "matched-path")]
                crate::extract::matched_path::set_matched_path_for_request(
                    id,
                    &self.node.route_id_to_path,
                    &mut parts.extensions,
                );
            }

            url_params::insert_url_params(&mut parts.extensions, match_.params);

            let endpoint = self
                .routes
                .get(&id)
                .expect("no route for id. This is a bug in axum. Please file an issue");

            let req = Request::from_parts(parts, body);
            match endpoint {
                Endpoint::MethodRouter(method_router) => {
                    Ok(method_router.call_with_state(req, state))
                }
                Endpoint::Route(route) => Ok(route.clone().call_owned(req)),
            }
        }
        // explicitly handle all variants in case matchit adds
        // new ones we need to handle differently
        Err(MatchError::NotFound) => Err((Request::from_parts(parts, body), state)),
    }
}
```

逐段拆:

**第一步:注入 `OriginalUri` 到 extensions**(`#[cfg(feature = "original-uri")]` 那段)。`OriginalUri`(`req.uri().clone()`)记录"请求原始 URI"——因为后面 `nest` 会改写 URI(剥前缀),handler 想知道原始 URI 就从 `OriginalUri` 提取器拿。这一步只注入一次(检查 `extensions().get::<OriginalUri>().is_none()`)。

**第二步:`req.into_parts()`**。`http::Request::into_parts()` 把请求拆成 `(parts, body)`——`parts: Parts`(包含 method、uri、version、headers、extensions)和 `body: B`。**这一步是后续提取器链的基础**:`FromRequestParts` 只读 `&mut Parts`(可多次跑),`FromRequest` 消费整个 Request(包括 body,只能跑一次)。这个二元划分(P3-10 招牌)的根就在这里。

**第三步:`self.node.at(parts.uri.path())`**。**这是 matchit 字典树匹配的调用点**。`node` 是 `Arc<Node>`,`Node`(`path_router.rs` L478-L481)包装了 `matchit::Router<RouteId>`:

```rust
// axum/src/routing/path_router.rs#L476-L481(逐字摘录)
/// Wrapper around `matchit::Router` that supports merging two `Router`s.
#[derive(Clone, Default)]
struct Node {
    inner: matchit::Router<RouteId>,
    route_id_to_path: HashMap<RouteId, Arc<str>>,
    path_to_route_id: HashMap<Arc<str>, RouteId>,
}
```

`matchit::Router<RouteId>` 是外部 crate(matchit 0.8.4,axum 钉死在 `=0.8.4`,`axum/Cargo.toml` L62)实现的基数树(radix tree / 字典树)路径匹配器。`node.at(path)` 返回 `Match<'n, 'p, &RouteId>` 或 `MatchError::NotFound`。`Match` 带两样东西:`value: &RouteId`(注册时绑定的 ID)和 `params`(URL 参数,如 `/users/{id}` 匹配 `/users/42` 时 `params` 是 `{"id": "42"}`)。matchit 内部怎么做基数树匹配(P2-05 招牌章详拆),本章只要知道:**它是一个 O(路径段数)的字典树查找,把 URL 路径映射到一个 `RouteId`**。

`RouteId`(`mod.rs` L57-L58)是 axum 自己的:

```rust
// axum/src/routing/mod.rs#L57-L58(逐字摘录)
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub(crate) struct RouteId(u32);
```

`RouteId(u32)` 是个新类型,索引 `self.routes: HashMap<RouteId, Endpoint<S>>`(`path_router.rs` L17)。**axum 用一个 HashMap 把 `RouteId` 映射到 `Endpoint`**——matchit 只管"路径到 ID"的映射,真正的 handler 在 axum 这边的 HashMap 里。这是 axum 和 matchit 的边界:**matchit 负责路径匹配(返回 ID),axum 负责 ID → Endpoint 查找**。两个 HashMap(`route_id_to_path`、`path_to_route_id`)是双向映射,给"matched path 提取"和"路径冲突检测"用,P2-05 详拆。

**第四步:匹配成功时,处理 URL 参数和 matched path**。

- `set_matched_path_for_request(id, &self.node.route_id_to_path, &mut parts.extensions)` —— 把"匹配到的路由模板路径"(如 `/users/{id}`)塞进 extensions,handler 里 `MatchedPath` 提取器能拿(用于 metrics/日志,知道"这个请求匹配的是哪条路由")。只在 `!IS_FALLBACK`(主路由,不是 fallback)时做。
- `url_params::insert_url_params(&mut parts.extensions, match_.params)` —— 把 matchit 解析出来的 URL 参数(如 `{"id": "42"}`)塞进 extensions,handler 里 `Path<i32>` 提取器会从这里拿。这是 URL 参数怎么传到 handler 的核心机制(P2-05 详拆)。

**第五步:索引 `Endpoint`,按类型分发**。

```rust
// 摘自 path_router.rs#L409-L414
let req = Request::from_parts(parts, body);
match endpoint {
    Endpoint::MethodRouter(method_router) => {
        Ok(method_router.call_with_state(req, state))
    }
    Endpoint::Route(route) => Ok(route.clone().call_owned(req)),
}
```

`Endpoint`(`mod.rs` L753-L754)有两个变体:`MethodRouter(MethodRouter<S>)`(用 `get`/`post` 等 handler 构造的)和 `Route(Route)`(用 `route_service` 直接塞一个任意 Service,绕过 method 分发)。**绝大多数情况是 `MethodRouter`**——你写 `.route("/", get(handler))`,`get(handler)` 构造的就是一个 `MethodRouter`,只注册了 GET method。`Route` 变体是 0.8 新增的 `route_service` 的产物(把任意 `Service<Request>` 挂到一个路径,不再按 method 分发,P6-20 详拆)。两种变体走不同的分发路径:`MethodRouter` 调 `method_router.call_with_state`(下一层),`Route` 直接 `route.clone().call_owned(req)`(跳过 method 分发,直接调那个 Service)。

**第六步:匹配失败(`MatchError::NotFound`)**。返回 `Err((Request::from_parts(parts, body), state))`——**把 Request 和 state 原样还回去**(注意 parts 和 body 重新组装),给 `Router::call_with_state` 里下一层的 fallback_router 用。这种"匹配失败不消耗 Request"的设计,让 fallback 链式尝试成为可能。

### 第三层:MethodRouter::call_with_state —— 按 method 选 handler

`MethodRouter::call_with_state`(`axum/src/routing/method_routing.rs` L1120-L1175)是 HTTP method 分发:

```rust
// axum/src/routing/method_routing.rs#L1120-L1175(逐字摘录,关键部分)
pub(crate) fn call_with_state(&self, req: Request, state: S) -> RouteFuture<E> {
    macro_rules! call {
        (
            $req:expr,
            $method_variant:ident,
            $svc:expr
        ) => {
            if *req.method() == Method::$method_variant {
                match $svc {
                    MethodEndpoint::None => {}
                    MethodEndpoint::Route(route) => {
                        return route.clone().oneshot_inner_owned($req);
                    }
                    MethodEndpoint::BoxedHandler(handler) => {
                        let route = handler.clone().into_route(state);
                        return route.oneshot_inner_owned($req);
                    }
                }
            }
        };
    }

    // written with a pattern match like this to ensure we call all routes
    let Self {
        get,
        head,
        delete,
        options,
        patch,
        post,
        put,
        trace,
        connect,
        fallback,
        allow_header,
    } = self;

    call!(req, HEAD, head);
    call!(req, HEAD, get);
    call!(req, GET, get);
    call!(req, POST, post);
    call!(req, OPTIONS, options);
    call!(req, PATCH, patch);
    call!(req, PUT, put);
    call!(req, DELETE, delete);
    call!(req, TRACE, trace);
    call!(req, CONNECT, connect);

    let future = fallback.clone().call_with_state(req, state);

    match allow_header {
        AllowHeader::None => future.allow_header(Bytes::new()),
        AllowHeader::Skip => future,
        AllowHeader::Bytes(allow_header) => future.allow_header(allow_header.clone().freeze()),
    }
}
```

`MethodRouter`(`method_routing.rs` L547-L557)按 HTTP method 持有 9 个 `MethodEndpoint` 字段(get/head/delete/options/patch/post/put/trace/connect)+ `fallback` + `allow_header`。`MethodEndpoint`(`method_routing.rs` L1225-L1229)三态:

```rust
// axum/src/routing/method_routing.rs#L1225-L1229(逐字摘录)
enum MethodEndpoint<S, E> {
    None,
    Route(Route<E>),
    BoxedHandler(BoxedIntoRoute<S, E>),
}
```

`None`(没注册这个 method)、`Route(Route)`(用 `route_service` 直接塞的)、`BoxedHandler(BoxedIntoRoute)`(用 `get(handler)` 等 handler 构造的,`handler.rs` L639 显示 `MethodEndpoint::BoxedHandler(BoxedIntoRoute::from_handler(handler))`)。

`call!` 宏按 method 顺序检查:**先匹配 method,匹配上了看 `MethodEndpoint` 是哪种变体,`Route` 直接 oneshot、`BoxedHandler` 先 `into_route(state)` 转 Route 再 oneshot**。注意两个细节:

1. **`HEAD` 先试 `head`,再试 `get`**(`call!(req, HEAD, head); call!(req, HEAD, get);`)——HTTP 语义里 HEAD 可以复用 GET 的 handler(只返回 header 不返回 body),axum 自动支持。
2. **`BoxedHandler` 在每次 `call` 时 `into_route(state)`** —— 因为 handler 是带状态的(`Handler<T, S>`),要先把 state 注入,转成一个无状态的 `Route`(类型擦除的 Service)。`Route`(`routing/route.rs` L31)是 `BoxCloneSyncService<Request, Response, E>`(类型擦除的 Tower Service,承《Tower》P6-17 BoxCloneSyncService,P1-03 详拆)。

**method 都不匹配时走 `fallback`**(`call!(...)` 全 fall through 后的 `fallback.clone().call_with_state(req, state)`)。`MethodRouter` 的 `fallback`(`method_routing.rs` L557)和 `Router` 的 `catch_all_fallback` 是两套——前者是"路径匹配但 method 不匹配"时用(默认返回 405 Method Not Allowed),后者是"路径完全不匹配"时用(默认返回 404 Not Found)。这两套 fallback 的区别和优先级是 P2-08 的主题。

`allow_header` 是给 405 响应拼 `Allow` 头用的(告诉客户端"这个路径支持哪些 method"),`AllowHeader` 三态(`None`/`Skip`/`Bytes`)区分"用默认空 Allow"/"跳过 Allow"/"用预拼的 Allow"。

### 第四层:Route::oneshot_inner_owned —— 类型擦除 Service 的执行

无论从 `MethodRouter` 还是 `Route` 变体,最终都调到 `Route::oneshot_inner_owned`(`axum/src/routing/route.rs` L54-L58):

```rust
// axum/src/routing/route.rs#L54-L58(逐字摘录)
/// Variant of [`Route::oneshot_inner`] that takes ownership of the route to avoid cloning.
pub(crate) fn oneshot_inner_owned(self, req: Request) -> RouteFuture<E> {
    let method = req.method().clone();
    RouteFuture::new(method, self.0.oneshot(req))
}
```

`self.0` 是 `BoxCloneSyncService<Request, Response, E>`(承《Tower》,类型擦除的 Tower Service,内部用 trait object 把不同类型的 handler Service 统一成同一类型)。`.oneshot(req)` 是 `tower::ServiceExt::oneshot`(承《Tower》P1-04,等价于 `poll_ready` + `call` + await),返回一个 `Oneshot` future。

> **承接《Tower》**:`BoxCloneSyncService<Req, Res, Err>` 是 Tower 提供的类型擦除 Service——它把任意 `Service<Req, Response = Res, Error = Err> + Clone + Send + Sync` 擦除成一个统一类型,代价是 `call` 走虚分派(动态分派)。axum 用它是因为**路由表要存不同类型的 handler**(每个 handler 的提取器 tuple 类型不同,生成的 `HandlerService` 类型不同),必须擦除成同一类型才能存进同一个 `HashMap`/字段。这一点 P1-03 详拆(`Route = BoxCloneSyncService`)。

`RouteFuture`(`route.rs` L108-L117)是 axum 自己的 future 包装:

```rust
// axum/src/routing/route.rs#L108-L117(逐字摘录)
pin_project! {
    /// Response future for [`Route`].
    pub struct RouteFuture<E> {
        #[pin]
        inner: Oneshot<BoxCloneSyncService<Request, Response, E>, Request>,
        method: Method,
        allow_header: Option<Bytes>,
        top_level: bool,
    }
}
```

`inner` 是那个 `Oneshot` future,`method` 留着用于拼 405 的 Allow 头,`allow_header` 是预拼的 Allow 值,`top_level` 标记"这是不是顶层路由"(影响是否拼默认 405/404 响应)。`RouteFuture` 实现 `Future<Output = Result<Response, E>>`,poll 时驱动 `inner`,结束时根据 `method`/`allow_header` 给响应拼头。这部分细节 P1-03 详拆。

### 第五层:Handler::call —— 宏展开的 tuple 提取器链

终于到了 handler 函数本身。`BoxedHandler` 在 `into_route(state)` 时,会把 handler 包成一个 `HandlerService`(`handler/service.rs` L22-L26),`HandlerService::call`(`handler/service.rs` L167-L178)调 `Handler::call`:

```rust
// axum/src/handler/service.rs#L167-L178(逐字摘录)
fn call(&mut self, req: Request<B>) -> Self::Future {
    use futures_util::future::FutureExt;

    let req = req.map(Body::new);

    let handler = self.handler.clone();
    let future = Handler::call(handler, req, self.state.clone());
    let future = future.map(Ok as _);

    super::future::IntoServiceFuture::new(future)
}
```

注意 `handler.clone()`——**handler 在每个请求都被 clone 一份**(因为 `call(&mut self)` 要消耗 self,但 Service 要能反复 call)。这要求 `Handler: Clone`(`handler/mod.rs` L148:`pub trait Handler<T, S>: Clone + Send + Sync + Sized + 'static`)——所以你的 handler `async fn` 必须能 Clone(闭包捕获的状态要 Clone)。

`Handler::call` 的真正实现在 `impl_handler!` 宏展开的代码里(`handler/mod.rs` L221-L262)。我们看一个"2 个参数"的展开(实际宏展开会用具体类型替换 `$ty` 和 `$last`):

```rust
// axum/src/handler/mod.rs#L221-L260(逐字摘录,宏模板)
macro_rules! impl_handler {
    (
        [$($ty:ident),*], $last:ident
    ) => {
        #[diagnostic::do_not_recommend]
        #[allow(non_snake_case, unused_mut)]
        impl<F, Fut, S, Res, M, $($ty,)* $last> Handler<(M, $($ty,)* $last,), S> for F
        where
            F: FnOnce($($ty,)* $last,) -> Fut + Clone + Send + Sync + 'static,
            Fut: Future<Output = Res> + Send,
            S: Send + Sync + 'static,
            Res: IntoResponse,
            $( $ty: FromRequestParts<S> + Send, )*
            $last: FromRequest<S, M> + Send,
        {
            type Future = Pin<Box<dyn Future<Output = Response> + Send>>;

            fn call(self, req: Request, state: S) -> Self::Future {
                let (mut parts, body) = req.into_parts();
                Box::pin(async move {
                    $(
                        let $ty = match $ty::from_request_parts(&mut parts, &state).await {
                            Ok(value) => value,
                            Err(rejection) => return rejection.into_response(),
                        };
                    )*

                    let req = Request::from_parts(parts, body);

                    let $last = match $last::from_request(req, &state).await {
                        Ok(value) => value,
                        Err(rejection) => return rejection.into_response(),
                    };

                    self($($ty,)* $last,).await.into_response()
                })
            }
        }
    };
}

all_the_tuples!(impl_handler);
```

这是 axum 最精妙的源码段之一。逐段拆:

**签名:`Handler<(M, $($ty,)* $last,), S> for F`**。`F` 是任意 `async fn`(满足那一串 `where` 约束),`T = (M, $($ty,)* $last,)` 是一个 tuple 类型参数。`$ty`(可能有 0~15 个)是前几个参数,`$last` 是最后一个参数。`M` 是个 marker 类型(给 `FromRequest<S, M>` 用,承 P3-10)。**这个 tuple 类型参数 `T` 不是 handler 的某个具体类型,而是 coherence 占位**——它让 Rust 能对"不同参数个数的 `async fn`"都实现 `Handler`,绕开孤儿规则(`handler/mod.rs` L137-L144 注释解释了这一点)。这是 axum 最值得讲的技巧之一,P3-09 招牌章专讲。

**约束**:
- 前 `$ty` 个参数:`$ty: FromRequestParts<S> + Send` —— 只读 parts 的提取器(`Path`、`Query`、`State`、`Header` 等)。
- 最后一个 `$last`:`$last: FromRequest<S, M> + Send` —— 可能消费 body 的提取器(`Json`、`Form`、`String`、`Bytes` 等)。
- 返回值 `Res: IntoResponse` —— handler 返回值要能转成 Response。

**这个"前 n-1 个 FromRequestParts + 最后一个 FromRequest"的划分,就是 axum 提取器二元划分的根**。为什么这么分?因为 body 只能被消费一次——只能有一个提取器拿走 body(最后一个),前面的提取器只能读 parts(headers/uri/extensions,可以反复读)。这个设计 P3-10 招牌章详拆。

**`call` 函数体**:

1. `let (mut parts, body) = req.into_parts();` —— 把请求拆成 parts + body(注意 parts 是 `mut`,因为提取器会改它,比如往 extensions 里塞东西)。
2. `Box::pin(async move { ... })` —— 把整个提取器链 + handler 调用包成一个 future。`Box::pin` 是因为 future 类型无法具名(异步闭包的 future 是匿名类型),用 `Box<dyn Future>` 擦除。
3. **前 n-1 个提取器逐个 await**:`$ty::from_request_parts(&mut parts, &state).await`。**注意是顺序 await,不是并发**——每个提取器拿到 `&mut parts`,改完(可能塞 extensions),下一个提取器才跑。`Ok(value)` 拿到值,`Err(rejection)` 直接 `return rejection.into_response()`——**提取失败立刻 short-circuit 返回错误响应,后面的提取器和 handler 都不跑**。这是为什么 `Path<i32>` 提取失败(比如 URL 参数不是数字)直接返回 400,handler 根本不会被调用。
4. **最后一个提取器**:`let req = Request::from_parts(parts, body);` 把 parts 和 body 重新组装,`$last::from_request(req, &state).await`——**消费整个 Request(包括 body)**。这是为什么 `FromRequest` 只能跑一次:它拿走了 body 的所有权。
5. **调用真正的 handler**:`self($($ty,)* $last,).await`——把所有提取出来的参数传给你的 `async fn`,await 它。
6. **拼 Response**:`.into_response()`——handler 返回值经 `IntoResponse` trait 转成 `Response`。`String` 的 IntoResponse 会设 `Content-Type: text/plain`,body 是字符串字节;`Json<T>` 的 IntoResponse 会设 `Content-Type: application/json` + serde 序列化;`(StatusCode, T)` 会设状态码再套 T 的 IntoResponse。详见 P3-12。

**宏展开覆盖 0~16 个参数**:`all_the_tuples!(impl_handler)`(`handler/mod.rs` L262)是个递归宏,对 `()`、`(T1,)`、`(T1, T2)`、... 一直到 16 个参数的 tuple 都生成一份 impl。这就是为什么 axum 的 handler 最多 16 个参数(其中一个可以是 `FromRequest`,其余 `FromRequestParts`)。`all_the_tuples!` 在 `macros.rs` 里,P3-09 详拆。

还有两个特例的 Handler impl:

- **0 参数 handler**(`handler/mod.rs` L207-L219):`impl<F, Fut, Res, S> Handler<((),), S> for F where F: FnOnce() -> Fut`。tuple 类型参数是 `((),)`(单元素 tuple,里面是 unit)。`call` 直接 `self().await.into_response()`,不提取任何东西。
- **直接 IntoResponse 当 handler**(`handler/mod.rs` L270-L279):`impl<T, S> Handler<private::IntoResponseHandler, S> for T where T: IntoResponse`。这让 `.route("/", get("Hello"))` 这种"直接返回值不写 fn"的写法成立。marker 类型 `private::IntoResponseHandler` 是个空 enum,区分其他 impl。

### 第六层:返回值 IntoResponse 拼回 Response

handler 返回值 `Res: IntoResponse`,`.into_response()` 把它转成 `Response`。`Response`(`axum_core::response::Response = http::Response<axum_core::body::Body>`)是 axum 统一的响应类型。

`IntoResponse` trait(`axum-core/src/response/into_response.rs`,P3-12 详拆)极简:

```rust
// axum-core/src/response/into_response.rs(简化示意,非源码原文)
pub trait IntoResponse {
    fn into_response(self) -> Response;
}
```

axum 给一堆类型实现了 `IntoResponse`:`String`、`&str`、`StatusCode`、`()`、`Result<T, E where T: IntoResponse, E: IntoResponse>`、`(StatusCode, T)`、`(HeaderMap, T)`、`Json<T>`、`Redirect`、`Sse`、`Response` 自身、……(P3-12 详拆)。handler 返回什么,就调对应 impl 的 `into_response`,拼出 `Response`(status + headers + body)。

这个 `Response` 一路返回到 `RouteFuture`,再到 `MethodRouter::call_with_state`(`RouteFuture`),再到 `PathRouter::call_with_state`,再到 `Router::call`,再到 `TowerToHyperService`,再到 hyper-util 的 `serve_connection`。hyper-util 的协议机拿到 `Response`,按 HTTP/1 或 HTTP/2 编码(status line + headers + body),写回 `TokioIo`(也就是 `TcpStream`),送到对端。

> **钉死这件事**:下半场全景是 `Request → Router::call → PathRouter::call_with_state(matchit 匹配拿 RouteId) → 索引 Endpoint → MethodRouter::call_with_state(按 method 选 handler) → Route::oneshot_inner_owned → HandlerService::call → Handler::call(宏展开,req.into_parts → 前n-1个 FromRequestParts 顺序 await → 最后一个 FromRequest 消费 body → handler fn → IntoResponse::into_response) → Response 一路返回`。每一步都在为下一步"加工" Request:matchit 给 extensions 塞 URL 参数和 matched path,MethodRouter 给 RouteFuture 塞 method/allow_header,Handler 把 parts 拆开逐个跑提取器。**所有这些"加工"都是为了最后那个 `self(t1, ..., tn).await` 能拿到正确参数调你的 handler fn**。

---

## 第四节:和 go net/http、actix-web 的全景对照

### 提问

axum 的全景已经拆完了。但"一次请求穿过哪些层"这件事,go net/http、actix-web 也都得做。它们的全景和 axum 有什么本质差别?为什么 axum 非要套 `IntoMakeService` 这层,而 go 不用?

把对照钉死,你才能理解 axum 全景的独特之处。

### 对照 go net/http:无 MakeService 适配层

go net/http 的服务器全景极简:

```go
// go 的 Web 服务器全景(简化示意)
listener, _ := net.Listen("tcp", ":3000")
mux := http.NewServeMux()
mux.HandleFunc("/users/", func(w http.ResponseWriter, r *http.Request) {
    // handler
})
server := &http.Server{Handler: mux}
server.Serve(listener)
```

`server.Serve(listener)` 内部:`for { conn := listener.Accept(); go server.serve(conn) }`——每连接一个 goroutine(对应 axum 的 `tokio::spawn`),goroutine 里跑 HTTP 协议机(读请求、调 Handler、写响应)。

关键差别:**go 的 Handler 是 `http.Handler` 接口,签名 `ServeHTTP(w http.ResponseWriter, r *http.Request)`**。这个接口直接被 server 调,每个请求调一次。**没有"MakeService 适配层"**——为什么?因为 go 的 `Handler` 接口没有"每个连接独占一份"的需求:`Handler` 是无状态的(或者用闭包/外部变量持有状态),所有连接共享同一个 `Handler` 实例,每个请求调它的 `ServeHTTP`。

axum 为什么不行?**因为 Tower 的 `Service` trait 是 `&mut self`**(承《Tower》P0-01,`poll_ready` 预留资源 + `call` 消耗),一个 Service 实例同时只能处理一个"准备态"。hyper 把 `poll_ready` 删了改成 `&self`,所以 hyper-util 的 Service 可以一个实例并发处理多请求。但 axum 内部用的是 Tower Service(`Router` impl `tower_service::Service`,`&mut self`),为了让"一个连接独占一份 Service 实例",就得每连接 clone 一份——这就是 `IntoMakeService` 的根。

> **钉死这件事**:go 的 `Handler.ServeHTTP` 是 `&self`(go 接口方法默认可调用,但 `http.Handler` 实践中按值传,等价 `&self`),无状态、可并发调用,不需要"每连接 clone"。axum 的 `tower_service::Service::call` 是 `&mut self`(预留资源语义),需要每连接独占,所以套了 `IntoMakeService` 做"每连接 clone"。**这个差别不是 axum 多此一举,而是 Tower Service 模型的代价**——`&mut self` 带来了背压通道(承《Tower》),代价是要 Clone。P1-03 会拆 axum 的 `poll_ready` 无条件 Ready,实际上 axum 没用背压通道,但仍然付了 Clone 的代价——这是它选 Tower Service 模型的连带结果。

### 对照 actix-web:actor + 自运行时

actix-web 的全景更复杂:

```rust
// actix-web 的服务器全景(简化示意)
HttpServer::new(|| {
    App::new()
        .route("/users", web::get().to(users_handler))
})
.bind("0.0.0.0:3000")?
.run()
.await?;
```

actix-web 的关键差别:

1. **actor 模型**:actix-web 内部用 actix framework 的 actor,handler 是 actor message,通过消息传递(不是直接函数调用)。每个连接、每个 worker 都可能是 actor。
2. **自运行时**:actix-web 历史上用自己的异步运行时(actix-rt,后来基于 tokio 但有自己的 actor 调度),不像 axum 直接跑在 tokio 上。
3. **worker 模型**:actix-web 默认起 N 个 worker(每个一个 tokio runtime thread),连接在 worker 间分发,handler 在 worker thread 里跑。

axum 全用 tokio,handler 是普通 `async fn`(不是 actor message),每个连接一个 tokio task(不是 actix worker)。这让 axum 的全景更直白:**没有 actor 框架这一层,直接 hyper-util + Tower Service + 你的 async fn**。

代价是 axum 没有 actix-web 的 actor 抽象——如果你的业务逻辑天然适合 actor 模型(比如长连接状态机),actix-web 更顺手。axum 你得自己用 channel + task 模拟。

### 三方全景对照表

| 维度 | axum | go net/http | actix-web |
|------|------|-------------|-----------|
| **运行时** | Tokio(直接跑) | go runtime(内置) | actix-rt(基于 tokio + actor 调度) |
| **协议机** | hyper-util(HTTP/1+2 auto) | go 内置 net/http | actix-http(自研 HTTP/1,h2 用 h2 crate) |
| **抽象模型** | Tower Service(`&mut self`) | http.Handler(`ServeHTTP`) | actor message |
| **请求处理单元** | async fn(宏变 Service) | func(ResponseWriter, *Request) | actor handler |
| **每连接** | tokio::spawn task | go func(goroutine) | actix worker / arbiter |
| **MakeService 适配** | 有(`IntoMakeService`) | 无(Handler 共享) | 有(App::new 闭包每 worker 调一次) |
| **路由匹配** | matchit 字典树 | ServeMux(前缀 + Go 1.22 加模式) | Resource + Scope |
| **提取器** | FromRequestParts / FromRequest | 手写 r.URL.Query / r.Body | FromRequest trait(类似 axum) |
| **响应** | IntoResponse | 手写 w.Write / w.WriteHeader | Responder trait |

这张表钉死后,你就理解了 axum 全景的独特之处:**axum 全栈选 Tokio + hyper-util + Tower,handler 是普通 async fn,MakeService 适配层是 Tower Service 模型的连带代价**。它没有 go 的"Handler 共享"那么直白(多了 IntoMakeService),也没有 actix 的 actor 那么重(没有 actor 框架)。这是 axum 在"高性能 + 类型安全 + 写起来像普通 async fn"之间做的取舍。

---

## 技巧精解

这一节挑两个本章最该被钉死的技巧,配真实源码 + 行号 + 反面对比,单独拆透。

### 技巧一:`IntoMakeService` 适配层为什么不可省

**它解决什么问题**:把"一个共享的 Router"翻译成"hyper-util 连接模型期望的每连接一个 Service"。

**反面对比:如果直接让 hyper-util 持有 Router 共享引用会怎样**:

假设 axum 不套 `IntoMakeService`,直接让 hyper-util 的 `serve_connection` 持有 `&Router<()>`(共享引用):

```rust
// 假想的写法(非 axum 实际做法)
let router: Router<()> = Router::new().route("/", get(handler));
hyper_util::server::conn::auto::Builder::new(TokioExecutor::new())
    .serve_connection_with_upgrades(io, &router);  // 直接传 &Router
```

这要求 `Router<()>` 实现 `hyper::service::Service`(无 `poll_ready`,`call(&self)`)。从签名上看能成立(`Router<()>` 内部 `Arc<RouterInner>`,`&self` 完全够用,因为 `Arc` 共享 + 内部可变性)。那 axum 为什么不这么干?

三个理由:

1. **hyper-util 的 `serve_connection` 期望 Service **拥有**(不是借用)**。`serve_connection_with_upgrades(io, service)` 的 `service` 是按值传入,因为连接 task 要持有它整个生命周期(可能很长,HTTP/2 keep-alive 几小时)。借用 `&Router` 要求借用的生命周期比连接 task 长——主循环里 `Router` 是被 `serve` 函数持有的,确实够长,但 hyper-util 的 API 不接受借用,它要 own。所以必须 clone 一份给 task。

2. **每连接独立状态**。虽然 axum 的 `Router<()>` 共享一份(内部 `Arc`),但 hyper-util 的连接 task 是独立 task,要有自己的 Service 持有(不能跨 task 共享 `&mut`,会违反 `Send`/`Sync`)。`Router<()>: Clone`(`Arc::clone`)让"每连接 clone 一份"廉价,但 clone 这一步必须有。

3. **`IntoMakeServiceWithConnectInfo` 要在 Service 上套 Extension**。这层适配不仅 clone,还可能改 Service(套 `Extension(ConnectInfo)` Layer)。如果直接共享 `&Router`,没法在"每连接"维度套不同的 Extension(每个连接的对端 IP 不同,Extension 内容不同)。

**所以 `IntoMakeService` 这层不可省**。它干三件事:① clone Service(给 task own);② (变体)套 Extension(给 connect info);③ 签名匹配(让 `Router` 能塞进 `serve` 的 `M: Service<IncomingStream>` 参数)。

**真实的实现**:`IntoMakeService::call`(`into_make_service.rs` L35-L37)就一行:

```rust
// axum/src/routing/into_make_service.rs#L35-L37(逐字摘录)
fn call(&mut self, _target: T) -> Self::Future {
    IntoMakeServiceFuture::new(ready(Ok(self.svc.clone())))
}
```

`self.svc.clone()`——就这一行核心逻辑。`ready(Ok(...))` 包成 future(因为 `Service::call` 返回 Future)。`IntoMakeServiceFuture` 是 `std::future::Ready<Result<S, Infallible>>` 的 opaque future 包装(`opaque_future!` 宏,防类型泄漏)。

**对照"朴素地写"会撞什么墙**:假设你不用 `IntoMakeService`,自己在主循环里 clone:

```rust
// 假想的朴素写法
loop {
    let (io, addr) = listener.accept().await;
    let svc = router.clone();  // 手动 clone
    tokio::spawn(async move {
        let hyper_svc = TowerToHyperService::new(svc);
        Builder::new(TokioExecutor::new())
            .serve_connection_with_upgrades(TokioIo::new(io), hyper_svc).await;
    });
}
```

这其实就是 axum 的 `handle_connection` 在干的事!只不过 axum 把"怎么 clone Service"这步抽象成了 `MakeService` trait,让你能传入"不只是 Router"的东西:

- `Router::into_make_service()` → `IntoMakeService<Router<()>>`(纯 clone)
- `Router::into_make_service_with_connect_info::<SocketAddr>()` → `IntoMakeServiceWithConnectInfo<Router<()>, SocketAddr>`(clone + 套 ConnectInfo Extension)
- `HandlerService::into_make_service()` → `IntoMakeService<HandlerService<...>>`(给"单 handler 没 Router"的场景用)

三种 MakeService 都是 `Service<IncomingStream, Response = S>`,但 `call` 干不同的活。**`MakeService` trait 是扩展点**——你想在"每连接 clone Service"这步干点别的(比如记连接数、改 Service),自己实现一个 `MakeService` 传进去。这是 axum 把 `handle_connection` 逻辑做成泛型 `M` 的根本理由。

**为什么不这么写会出问题**:如果 axum 把 `handle_connection` 写死成"只接 `Router`",就没法支持 `IntoMakeServiceWithConnectInfo`、`HandlerService::into_make_service()`(单 handler serve)这些场景。泛型 `M: Service<IncomingStream>` 让 axum::serve 是可扩展的——任何 `MakeService` 都能塞进来。这是 axum 比"写死 Router"更灵活的设计点,代价是签名看起来复杂(一堆泛型约束)。

### 技巧二:matchit + RouteId 双层映射——为什么 matchit 不直接返回 Endpoint

**它解决什么问题**:axum 用 matchit 做路径匹配,但 matchit 返回的不是 axum 的 `Endpoint`,而是一个 `RouteId`(u32)。这层间接是怎么设计的,为什么不让 matchit 直接持有 `Endpoint`?

**反面对比:如果 matchit 直接持有 Endpoint 会怎样**:

matchit 0.8.4 的 `Router<T>` 是泛型的——`T` 可以是任意类型。假设 axum 直接用 `matchit::Router<Endpoint<S>>`:

```rust
// 假想的写法(非 axum 实际做法)
struct PathRouter<S> {
    node: matchit::Router<Endpoint<S>>,  // 直接持有 Endpoint
}
```

看起来更直接——`node.at(path)` 直接返回 `&Endpoint`,不用再做一次 HashMap 查找。但 axum 不这么干,理由:

1. **`Endpoint` 不是 `Clone + Copy`**。`Endpoint` 持有 `MethodRouter<S>` 或 `Route`(后者内部是 `BoxCloneSyncService`, Clone 但有堆分配)。matchit 内部要把 `T` 存在字典树节点里,如果 `T` 是 `Endpoint`,每次 `at` 返回 `&Endpoint`,axum 还是要 clone 一份给请求处理(因为 `Service::call` 要 `&mut self` 或消耗 self)。引入 `RouteId(u32)`(Copy),`at` 返回 `&RouteId`(便宜),axum 自己用 HashMap 查 `Endpoint`,clone 一份。

2. **`merge` 操作**。axum 的 `Router::merge` 要合并两个 Router,这涉及"重新编号 RouteId"(因为两个 Router 各有自己的 `RouteId` 序列,合并后不能冲突)。如果 matchit 直接持有 `Endpoint`,merge 时要清空字典树重新插入(因为 matchit 不支持"批量改 T")。用 `RouteId` 间接,axum 可以保留 matchit 的字典树结构,只在自己的 HashMap 里重映射 `RouteId → Endpoint`(P2-07 详拆)。

3. **`matched_path` 双向映射**。axum 提供 `MatchedPath` 提取器(给 metrics 用,知道"匹配的是哪条路由模板")。这需要 `RouteId → 路径模板` 的反向查找——`route_id_to_path: HashMap<RouteId, Arc<str>>`(`path_router.rs` L480)。如果 matchit 直接持有 Endpoint,要做反向查找就得遍历字典树(O(n)),用 HashMap 是 O(1)。

4. **路径冲突检测**。注册路由时,axum 要检测"同一路径已注册"(避免重复)。`path_to_route_id: HashMap<Arc<str>, RouteId>`(`path_router.rs` L481)做正向查找,O(1) 判断"这个路径注册过没"。如果只用 matchit,matchit 的 `insert` 会报冲突错误,但 axum 想在冲突时给出更友好的错误信息(指出"哪条路径已注册"),需要自己的 HashMap。

**所以 axum 的设计是 matchit + 双 HashMap**。`Node`(`path_router.rs` L478-L481):

```rust
// axum/src/routing/path_router.rs#L476-L481(逐字摘录)
/// Wrapper around `matchit::Router` that supports merging two `Router`s.
#[derive(Clone, Default)]
struct Node {
    inner: matchit::Router<RouteId>,
    route_id_to_path: HashMap<RouteId, Arc<str>>,
    path_to_route_id: HashMap<Arc<str>, RouteId>,
}
```

- `inner: matchit::Router<RouteId>` —— 路径匹配,返回 `RouteId`。
- `route_id_to_path` —— 反向映射,给 `MatchedPath` 用。
- `path_to_route_id` —— 正向映射(路径 → ID),给冲突检测用。

加上 `PathRouter.routes: HashMap<RouteId, Endpoint<S>>`(`path_router.rs` L17),一共**三个 HashMap + 一个字典树**。matchit 负责"路径 → ID"(字典树,支持模式匹配如 `{id}`),HashMap 负责"ID → 各种东西"。这是 axum 在 matchit 之上做的工程化封装,P2-05 招牌章详拆。

**对照"朴素地只用 matchit"会撞什么墙**:假设你只用 matchit,不要 HashMap。`matched_path` 提取要遍历字典树找 ID 对应的路径模板(O(n),慢);merge 两个 Router 要重建字典树(慢);路径冲突检测要在 insert 时靠 matchit 报错(错误信息不友好)。axum 用多一份内存(三个 HashMap)换来了 O(1) 的各种辅助查找 + 友好的错误 + 高效的 merge。这是典型的"用空间换时间 + 工程化"取舍。

**真实流程回顾**(`path_router.rs` L388-L414):

```rust
// 摘自 path_router.rs#L388-L414
match self.node.at(parts.uri.path()) {
    Ok(match_) => {
        let id = *match_.value;  // matchit 返回 &RouteId
        // ... set_matched_path 用 route_id_to_path 反查路径模板 ...
        // ... insert_url_params 用 match_.params 塞 URL 参数 ...
        let endpoint = self.routes.get(&id).expect("...");  // HashMap 查 Endpoint
        let req = Request::from_parts(parts, body);
        match endpoint {
            Endpoint::MethodRouter(method_router) => Ok(method_router.call_with_state(req, state)),
            Endpoint::Route(route) => Ok(route.clone().call_owned(req)),
        }
    }
    Err(MatchError::NotFound) => Err((Request::from_parts(parts, body), state)),
}
```

`match_.value` 是 `&RouteId`,`*match_.value` 解引用成 `RouteId`(Copy),`self.routes.get(&id)` 在 HashMap 里查 `Endpoint`。**两层查找:matchit 字典树 → RouteId → HashMap → Endpoint**。这就是 axum 路径匹配的全貌。

> **钉死这件事**:axum 的路由匹配是"matchit 字典树(返回 RouteId)+ HashMap(ID → Endpoint)"双层结构,不是 matchit 直接返回 Endpoint。这层间接是为了:① Endpoint 不是 Copy,clone 便宜;② merge 时重映射 ID 而不重建字典树;③ MatchedPath 提取器 O(1) 反查路径模板;④ 路径冲突检测 O(1) + 友好错误。代价是多三个 HashMap 的内存(每条路由一个 entry),换时间 + 工程化收益。P2-05 招牌章会把 `RouteId(usize)` 索引 `Vec<Endpoint>` 的设计、`url_params::insert_url_params` 的细节、matchit 基数树原理全拆透。

---

## 章末小结

回到全书的主轴:**路由与分发 vs 提取与响应**。

本章是**全景鸟瞰图**,把这条链放电影一样放了一遍:

- **上半场**(`axum::serve` accept 连接):`TcpListener::accept → handle_connection → make_service.call(连接元信息) → 拿到一个克隆的 Service → TowerToHyperService 适配 → tokio::spawn task 跑 hyper-util serve_connection`。每连接一个 task,task 里跑 HTTP 协议机,把字节流变成 Request。
- **下半场**(Request 穿过 Router):`Request → Router::call → PathRouter::call_with_state(matchit 字典树匹配路径拿 RouteId → 索引 Endpoint) → MethodRouter::call_with_state(按 method 选 handler) → Route::oneshot_inner_owned → HandlerService::call → Handler::call(宏展开:req.into_parts → 前n-1个 FromRequestParts 顺序 await → 最后一个 FromRequest 消费 body → handler fn → IntoResponse::into_response) → Response 一路返回到 hyper-util 写回 IO`。

这条链上,**路由与分发这一面**(Router/PathRouter/matchit/MethodRouter)负责"按 URL + method 找到 handler fn",**提取与响应这一面**(Handler trait 宏展开/FromRequestParts/FromRequest/IntoResponse)负责"把 Request 拆成 handler 参数、把返回值拼成 Response"。两条线在 `Handler::call` 那一刻汇合——路由分发找到了 handler,提取器链拆好了参数,handler fn 终于被调用。

底层 hyper-util(协议机/连接管理)、Tower(`Service`/`Layer`/`BoxCloneSyncService`)、Tokio(运行时/task/channel)各承担一段——这些《hyper》《Tower》《Tokio》讲透的部分,本章一句带过指路。本章是全景,每个具体机制都有后续招牌章专讲。

### 五个为什么清单

1. **为什么 `axum::serve` 期望 `MakeService` 而不是直接 `Service`?** 因为 hyper-util 的连接模型是"每连接独立 task 跑协议机",每个 task 要持有自己的 Service(`tower_service::Service` 是 `&mut self`,一个实例不能跨 task 共享)。`MakeService` 是"每连接调一次,吐一个 Service"的工厂,负责把"一个共享 Router"翻译成"每连接一份"。详见第一节。
2. **为什么 `Router<()>` 直接传给 `axum::serve` 不用 `.into_make_service()`?** 因为 axum 给 `Router<()>` 实现了 `Service<IncomingStream>`(`mod.rs` L549-L566),它的 `call` 就是 `self.clone().with_state(())`。这是便利写法,等价于 `router.into_make_service()`。详见第二节。
3. **为什么 axum 用 matchit + RouteId 双层,不直接让 matchit 持有 Endpoint?** 四个理由:Endpoint 不是 Copy/MatchedPath 反查/merge 重映射/冲突检测友好。代价是多三个 HashMap,换 O(1) 辅助查找 + 工程化收益。详见技巧精解技巧二,P2-05 详拆。
4. **为什么 handler 的提取器是"前 n-1 个 FromRequestParts + 最后一个 FromRequest"?** 因为 body 只能被消费一次——只能有一个提取器拿走 body(最后一个),前面的只能读 parts(headers/uri/extensions,可反复读)。这是 axum 提取器二元划分的根,Handler trait 宏展开直接体现。详见第三节第五层,P3-10 招牌章详拆。
5. **为什么 axum 全景比 go net/http 多一层 `IntoMakeService`?** 因为 Tower Service 是 `&mut self`(预留资源 + 消耗),需要每连接独占一份,所以 clone 一步必须有。go 的 `Handler.ServeHTTP` 是 `&self` 无状态共享,不需要 clone。这是 axum 选 Tower Service 模型的连带代价(带来背压通道,代价是 Clone)。详见第四节对照。

### 想继续深入往哪钻

- **`Router<()>` 的 `Service<Request>` impl 细节、`Route` 作为 `BoxCloneSyncService` 的类型擦除、`RouteFuture` 状态机、`poll_ready` 无条件 Ready**:→ 第 3 章(P1-03),Router 与 Route 的 Service 适配招牌章。
- **matchit 字典树原理、`PathRouter` + `Node` 双向映射、`RouteId` 索引、URL 参数怎么塞 extensions**:→ 第 5 章(P2-05),PathRouter 路由招牌章。
- **`MethodRouter` 按 method 持有 `MethodEndpoint`、`MethodFilter` 位运算、重复 route 走 merge**:→ 第 6 章(P2-06)。
- **`Handler<T, S>` trait 的 `T` coherence 占位、`impl_handler!` + `all_the_tuples!` 宏对 0~16 参数展开**:→ 第 9 章(P3-09),Handler trait 招牌章(全书精华)。
- **`FromRequestParts`(只读 parts)vs `FromRequest`(消费 body)二元划分、`ViaParts` marker 桥接**:→ 第 10 章(P3-10),FromRequest 招牌章。
- **`IntoResponse` trait + tuple 组合响应(`(StatusCode, T)`、`(HeaderMap, T)`)+ `IntoResponseParts`**:→ 第 12 章(P3-12)。
- **`axum::serve` 的 `Listener` trait、`with_graceful_shutdown`、`Connected` trait 提取对端信息、`auto::Builder` 自动协商 HTTP/1+2**:→ 第 17 章(P5-17),serve 详解。
- **hyper-util 怎么把字节流变成 Request、连接池/keep-alive、TowerToHyperService 适配 hyper/Tower Service**:→《hyper》P2-P5(协议机 + 连接管理)。
- **go net/http 的 ServeMux + Handler.ServeHTTP、actix-web 的 actor 模型全景差异**:→ 第 21 章(P7-21),全书收束双对照。

### 引出下一章

本章你拿到了 axum 的全景:从 `axum::serve` accept 连接到 handler 返回 Response,一条链上 6 个核心源码文件串起来的时序图。但有一个最关键的细节我们刻意留到了这里——**`Router<()>` 自己实现 `tower::Service<Request>`,意味着它是一个 Service;可它内部又持有 `path_router` / `fallback_router` / `catch_all_fallback`,这些怎么也都是 Service?`Route` 内部那个 `BoxCloneSyncService` 到底是什么?为什么 axum 的所有 `poll_ready` 都无条件 Ready(忽略 Tower 中间件的背压)?这些问题,下一章 P1-03 会用真实源码彻底拆开。`Router`/`Route`/`MethodRouter` 怎么层层套成 Service,是理解 axum 在 hyper+Tower 之上怎么"长出来"的钥匙。

---

> **本章源码锚点(全部经本地 `../axum/` Grep/Read 核实,axum-v0.8.9 @ c59208c8)**:
>
> - [serve 函数签名(L96-L109)](../axum/axum/src/serve/mod.rs#L96-L109) —— `M: Service<IncomingStream, Response = S>` 是 MakeService 适配的根。
> - [Serve::run accept 循环(L179-L193)](../axum/axum/src/serve/mod.rs#L179-L193) —— `loop { accept; handle_connection }`。
> - [handle_connection 全景(L353-L416)](../axum/axum/src/serve/mod.rs#L353-L416) —— make_service.call → TowerToHyperService → tokio::spawn → serve_connection。
> - [IncomingStream(L424-L430)](../axum/axum/src/serve/mod.rs#L424-L430) —— MakeService 的输入(io + remote_addr)。
> - [Listener trait 定义(L9-L24)](../axum/axum/src/serve/listener.rs#L9-L24) —— 抽象监听器,支持 Tcp/Unix。
> - [TcpListener impl Listener(L26-L43)](../axum/axum/src/serve/listener.rs#L26-L43) —— accept 错误重试 1 秒。
> - [IntoMakeService 定义 + Service impl(L1-L44)](../axum/axum/src/routing/into_make_service.rs#L1-L44) —— 纯克隆工厂,call 就一行 `self.svc.clone()`。
> - [Router<()> impl Service&lt;IncomingStream&gt;(L544-L567)](../axum/axum/src/routing/mod.rs#L544-L567) —— 便利写法的根:直接传 Router 用。
> - [Router<()> impl Service&lt;Request&gt;(L569-L588)](../axum/axum/src/routing/mod.rs#L569-L588) —— poll_ready 无条件 Ready,call 调 call_with_state。
> - [RouterInner 结构(L80-L85)](../axum/axum/src/routing/mod.rs#L80-L85) —— path_router + fallback_router + catch_all_fallback 三件套。
> - [Router::call_with_state(L417-L432)](../axum/axum/src/routing/mod.rs#L417-L432) —— 链式尝试主路由 → fallback_router → catch_all_fallback。
> - [RouteId 定义(L57-L58)](../axum/axum/src/routing/mod.rs#L57-L58) —— `RouteId(u32)` 索引。
> - [Endpoint 枚举(L753-L754)](../axum/axum/src/routing/mod.rs#L753-L754) —— MethodRouter | Route 两变体。
> - [PathRouter 结构(path_router.rs L16-L21)](../axum/axum/src/routing/path_router.rs#L16-L21) —— routes HashMap + node + prev_route_id + v7_checks。
> - [PathRouter::call_with_state(L371-L420)](../axum/axum/src/routing/path_router.rs#L371-L420) —— matchit at + set_matched_path + insert_url_params + 索引 Endpoint。
> - [Node 双向映射(path_router.rs L476-L481)](../axum/axum/src/routing/path_router.rs#L476-L481) —— matchit::Router&lt;RouteId&gt; + 两个 HashMap。
> - [MethodRouter 结构(method_routing.rs L547-L557)](../axum/axum/src/routing/method_routing.rs#L547-L557) —— 按 method 持 9 个 MethodEndpoint。
> - [MethodRouter::call_with_state(L1120-L1175)](../axum/axum/src/routing/method_routing.rs#L1120-L1175) —— call! 宏按 method 顺序检查。
> - [MethodEndpoint 枚举(method_routing.rs L1225-L1229)](../axum/axum/src/routing/method_routing.rs#L1225-L1229) —— None | Route | BoxedHandler 三态。
> - [Route = BoxCloneSyncService(route.rs L31)](../axum/axum/src/routing/route.rs#L31) —— 类型擦除的 Tower Service。
> - [Route::oneshot_inner_owned(route.rs L54-L58)](../axum/axum/src/routing/route.rs#L54-L58) —— MethodRouter/Route 最终都调到这。
> - [RouteFuture 结构(route.rs L108-L117)](../axum/axum/src/routing/route.rs#L108-L117) —— inner Oneshot + method + allow_header + top_level。
> - [Handler trait 定义(handler/mod.rs L148-L205)](../axum/axum/src/handler/mod.rs#L148-L205) —— `Handler<T, S>` 的 T 是 coherence 占位,P3-09 详拆。
> - [impl_handler! 宏(handler/mod.rs L221-L262)](../axum/axum/src/handler/mod.rs#L221-L262) —— 前n-1个 FromRequestParts + 最后一个 FromRequest,提取器二元划分的根。
> - [0 参数 Handler impl(handler/mod.rs L207-L219)](../axum/axum/src/handler/mod.rs#L207-L219) —— `Handler<((),), S>` for F。
> - [IntoResponse 当 Handler(handler/mod.rs L270-L279)](../axum/axum/src/handler/mod.rs#L270-L279) —— `.route("/", get("Hello"))` 的根。
> - [HandlerService 结构 + impl(handler/service.rs L22-L26, L148-L178)](../axum/axum/src/handler/service.rs#L22-L26) —— handler + state + marker,call 调 Handler::call。
> - [IntoMakeServiceWithConnectInfo(connect_info.rs L28-L46, L114-L133)](../axum/axum/src/extract/connect_info.rs#L28-L46) —— clone + 套 Extension(ConnectInfo)。
> - [Connected trait(connect_info.rs L80-L83)](../axum/axum/src/extract/connect_info.rs#L80-L83) —— 从 IncomingStream 提取连接信息。
>
> **承接**:hyper-util 的 accept/连接模型/HTTP/1+2 协议机承《hyper》P2-P5(一句带过);`tower_service::Service` trait 模型承《hyper》P1-02 +《Tower》P0-01(一句带过);`BoxCloneSyncService` 类型擦除承《Tower》P6-17(一句带过,本书引用其用法);`tower::ServiceExt::ready`/`oneshot` 承《Tower》P1-02/P1-04(一句带过);Tokio 运行时(task 调度/AsyncRead/mpsc channel/watch channel)承《Tokio》(一句带过);跨语言对照 go net/http(`Handler.ServeHTTP` 无 MakeService 适配)、actix-web(actor + 自运行时)。
>
> **修正总纲一处笔误**:总纲称 axum-core 是 "0.5.6"——经核实 `axum-core/Cargo.toml` L12 实际是 `version = "0.5.5"`。matchit 实际是 `=0.8.4`(`axum/Cargo.toml` L62),总纲写 "0.9.2" 是笔误。本书以源码为准:axum 0.8.9、axum-core 0.5.5、axum-macros 0.5.1、matchit 0.8.4。
