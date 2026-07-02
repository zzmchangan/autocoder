# 第 17 章 · serve 与监听器:从 Router 到上线

> **核心问题**:你已经写过无数次 `axum::serve(listener, router).await.unwrap()`,也大概知道它"底层走 hyper-util"。但这一行背后到底发生了什么?`serve` 函数为什么要把第二个参数抽象成一个 `MakeService`(而不是直接吃 `Router`)?它又是怎么做到"既能接 `tokio::net::TcpListener` 又能接 `tokio::net::UnixListener`,甚至还能接你自己造的假 IO"——而 hyper 原生 `serve_connection` 写死了 TCP 风格的 `AsyncRead + AsyncWrite`?graceful shutdown 凭什么"停 accept 新连接,但等在途请求跑完"——它怎么知道还有多少在途请求、什么时候才算全跑完?这些问题,是 axum 从"一个 `Router`"长成"一个能上线服务"的最后一公里。
>
> **读完本章你会明白**:
>
> 1. `axum::serve` 为什么是 hyper-util 的薄封装,以及这层封装具体封了什么(accept 循环、错误重试、`tokio::spawn` 每连接 task、auto 协商 HTTP/1+2、`TokioIo` 适配、graceful shutdown 编排),hyper 原生给你的是 `serve_connection(io, service)` 单连接版,axum 给你的是"无限循环 accept + 自动 spawn"的服务器版,差别就在这一层薄封装;
> 2. axum 为什么自己定义一个 `Listener` trait,而不直接用 `tokio::net::TcpListener`——这个 trait 怎么用"关联类型 `Io` + 关联类型 `Addr` + `accept` 返回 `(Io, Addr)`"三件套,把 `TcpListener` / `UnixListener` / 自定义假 IO(测试用 `tokio::io::duplex`)统一成同一种监听器,以及 `TcpListener::accept` 为什么在 axum 这层被包成了"永不返回 Err"的版本(遇 `EMFILE` 睡 1 秒重试);
> 3. `IntoMakeService` / `IntoMakeServiceWithConnectInfo` 两层适配到底干了什么——前者是"每连接 clone 一份 Router"(承 P1-02,本章只复习),后者多干一件事:"从对端地址造 `ConnectInfo<C>`,套一层 `Extension` Layer,让 handler 里 `ConnectInfo<SocketAddr>` 提取器能从 extensions 拿到对端 IP",以及 `Connected` trait 怎么把"从 `IncomingStream` 提取连接信息"抽象成可扩展点(给自定义 IO / 自定义 connect info 类型);
> 4. graceful shutdown 的"停 accept 等在途"语义为什么 sound——`with_graceful_shutdown(shutdown_signal)` 传一个 Future,这个 Future 完成时 serve `break` 跳出 accept 循环(不再接新连接),然后在途的每个连接 task 各自收到 `signal_tx.closed()` 信号、调 `conn.graceful_shutdown()` 让 hyper-util 协议机"做完手头这一轮就退出",最后主任务 `close_tx.closed().await` 等所有 task `drop(close_rx)` 才返回——这一套用 `tokio::sync::watch` 的"广播 + 等收件人归零"语义,是 axum 不丢在途请求的根本机制。
>
> 本章服务**总览**这一面——它是"把一个 Router 跑起来接客"的全部工程化封装,既不属路由与分发(那是第 2 篇的事),也不属提取与响应(那是第 3 篇的事),它是承接 P1-02 全景里"serve 那一段"的深挖。
>
> **写给谁读(读者画像)**:你用 axum 起过服务,大概知道 `axum::serve` 底层是 hyper,翻过几篇博客说"它就是 hyper-util 的封装"。但你讲不清:这个封装具体封了什么、`Listener` trait 凭什么能同时支持 TCP 和 Unix socket、graceful shutdown 怎么做到"不丢在途请求"、`IntoMakeServiceWithConnectInfo` 比 `IntoMakeService` 多干了什么。一句话,你"用过 serve,但没懂 serve"。本章就是来把这一层薄封装拆透。
>
> **前置知识**:假设你读过 P0-01(知道 axum 在 hyper+Tower 之上)、P1-02(知道全景时序,知道 `IntoMakeService` 是 MakeService 适配层)、P1-03(知道 Router/Route 是 Service)。熟悉 Rust 的 trait/泛型/`async fn`,听说过 `Future`/`Poll`。读过《hyper》《Tokio》最佳(没读过也行,hyper-util 的协议机/连接模型、Tokio 的 watch channel/task 调度,本章会一句带过指路)。
>
> **逃生阀(读不下去怎么办)**:本章涉及三个文件(`serve/mod.rs` / `serve/listener.rs` / `extract/connect_info.rs`)和 graceful shutdown 的时序编排,信息密度大。如果某一段绕晕你,记住三句话就够——**① `axum::serve` 是 hyper-util 的薄封装,加了一层 `Listener` trait 抽象监听器 + accept 循环 + 每连接 spawn task + graceful shutdown 编排;② `Listener` trait 把 TCP/Unix/自定义 IO 统一成"accept 返回 (Io, Addr)"的接口;③ graceful shutdown 靠 `tokio::sync::watch` 广播 shutdown 信号,主任务等所有连接 task `drop(close_rx)` 才返回,所以不丢在途请求**。带着这三句话,先跳到第三节的"serve 内部时序图"和第五节的"graceful shutdown 时序图"看运行画面,再回头读细节。

---

## 一句话点破

> **`axum::serve(listener, make_service)` 的全部,是 `loop { (io, addr) = listener.accept(); handle_connection(io, addr) }`,而 `handle_connection` 内部 `make_service.call(IncomingStream { io, addr })` 拿到一个克隆的 Service、用 `TowerToHyperService` 适配成 hyper Service、`tokio::spawn` 一个 task 跑 `hyper-util::auto::Builder::serve_connection_with_upgrades(io, hyper_service)`。监听器抽象成 `Listener` trait(关联类型 `Io` + `Addr`,`accept` 返回 `(Io, Addr)` 不返回 `Result`),让 TCP/Unix/自定义 IO 共用同一套 accept 循环;`MakeService` 抽象(承 P1-02)让"每连接造一个 Service"可扩展(纯克隆的 `IntoMakeService` / 带连接信息的 `IntoMakeServiceWithConnectInfo`);graceful shutdown 靠 `tokio::sync::watch` 广播信号,主任务 `close_tx.closed().await` 等所有连接 task `drop(close_rx)` 归零,所以"停 accept 等在途"——不丢一个在途请求。**

这是结论,不是理由。本章要倒过来拆:`serve` 函数签名里那一堆泛型约束(`L: Listener`、`M: Service<IncomingStream, Response = S>`、`S: Service<Request> + Clone + Send + 'static`)为什么是这样、`Listener` trait 凭什么用关联类型而不是泛型方法、`handle_connection` 那十几行到底按什么顺序跑、graceful shutdown 的时序编排怎么做到"等在途请求但不无限等"。这一章把 `serve` 这一层薄封装彻底摊开。

---

## 第一节:为什么需要把"serve"再封装一层

### 提问

`axum::serve(listener, router).await` 这一行,你写熟了。但 hyper 已经给了 `hyper-util::server::conn::auto::Builder::serve_connection`——直接拿一个 IO 和一个 Service 就能跑 HTTP 协议机。axum 为什么不让你直接调它,非要再包一层 `serve`?

这一节先把"为什么需要这层封装"钉死,后面几节再拆封装内部。

### hyper 给的是什么(以及它为什么不该再多做)

> **承接《hyper》[[hyper-source-facts]]**:hyper 1.0 把 server 拆成了"协议原语 + 策略"。`hyper-util::server::conn::auto::Builder::serve_connection_with_upgrades(io, service)` 是协议原语:给它一个 `io`(实现 `hyper::rt::Read/Write`)和一个 `service`(实现 `hyper::service::Service`,删了 `poll_ready`、`call(&self)`),它跑完这条连接上的 HTTP/1 或 HTTP/2 协议机,把字节流变成 `Request`,调 `service.call(req)`,把 `Response` 编码回字节流写回 io——直到连接关闭(对端 EOF、超时、协议错误)或 service future 完成。**这是单连接版的协议机**——一条 IO 跑一次。详见《hyper》P4-P5(连接管理),本章一句带过指路。

`serve_connection` 自己**不管 accept**。它不绑端口、不接新连接、不 spawn task——它只负责"已经握手的这一条 IO 上的 HTTP 协议"。这是合理的边界:协议层不该规定你怎么管理连接生命周期(单线程?线程池?每个连接一 task?)。可这留给你(框架/应用)的活就多了:

- 你要自己 `bind` 一个端口(`TcpListener::bind`)。
- 你要自己 `loop { listener.accept().await }`。
- 你要自己决定"每条连接怎么处理"(单线程串行?线程池?每连接一 task?)。
- 你要自己处理 accept 错误(连接被打断、`EMFILE` 文件描述符耗尽、`ECONNABORTED`)。
- 你要自己把 `tokio::net::TcpStream` 适配成 hyper 要的 IO(`TokioIo::new`)。
- 你要自己把 `tower::Service` 适配成 hyper Service(`TowerToHyperService::new`)。
- 你要自己实现 graceful shutdown("接到信号就别接新连接了,但让在途的跑完")。

每条都是"机械活 + 容易写错"。每一个想用 hyper 直接写服务器的项目,都要把这七步重写一遍——这就是 axum 把它封装一层的动机。

### 不这样会怎样:每个项目手写一遍 accept 循环

假设没有 `axum::serve`,你用裸 hyper-util 写一个 axum 服务(伪代码,非真实 axum):

```rust
// 朴素写法:裸 hyper-util + Tower + Router(简化示意,非源码原文)
let listener = tokio::net::TcpListener::bind("0.0.0.0:3000").await?;
let app: Router<()> = Router::new().route("/", get(handler)).with_state(state);

loop {
    let (tcp_stream, remote_addr) = match listener.accept().await {
        Ok(tup) => tup,
        Err(e) => {
            // ★ accept 错误自己处理:EMFILE 睡 1 秒重试,别的也得分情况
            if is_connection_error(&e) { continue; }
            tracing::error!("accept error: {e}");
            tokio::time::sleep(Duration::from_secs(1)).await;
            continue;
        }
    };

    let app = app.clone();  // ★ 每连接 clone 一份 Router
    tokio::spawn(async move {
        // ★ tokio AsyncRead/Write → hyper RT(每条连接都写一遍)
        let io = TokioIo::new(tcp_stream);
        // ★ Tower Service → hyper Service(每条连接都写一遍)
        let hyper_svc = TowerToHyperService::new(app);
        let mut builder = hyper_util::server::conn::auto::Builder::new(TokioExecutor::new());
        builder.http2().enable_connect_protocol();  // ★ HTTP/2 websocket CONNECT 协议
        // ★ 真正跑协议机
        if let Err(e) = builder.serve_connection_with_upgrades(io, hyper_svc).await {
            tracing::error!("conn error: {e}");
        }
    });
}
```

能用。但读一遍,问题立刻显现:

- **样板重复**:每条连接都要 `TokioIo::new` + `TowerToHyperService::new` + `Builder::new(TokioExecutor::new())` + `http2().enable_connect_protocol()`,十行模板代码在每条连接里跑一遍。每一个项目写一次,每个项目都不一样(有人忘了 `enable_connect_protocol`,HTTP/2 websocket 就不工作;有人忘了 `TokioIo`,编译过但运行 panic)。
- **accept 错误处理散**:每个项目自己写"哪些 err 重试、哪些 sleep、哪些 panic"。`EMFILE`(文件描述符耗尽)是经典的瞬时错误,要 sleep 1 秒重试,不能 panic——但每个项目都自己重写一遍这个判断。
- **graceful shutdown 要自己编**:你想做"接 SIGTERM 就别接新连接,但让在途的跑完"——你得自己起一个 shutdown signal,自己 `tokio::select!` 让 accept 和 signal 抢跑,自己维护一个"在途连接计数器"或 `tokio::sync::task::JoinSet`,自己等所有连接 task 结束。每个项目编一遍,容易编错(信号来了直接 drop listener,在途请求被强杀;信号没传到子 task,子 task 还在 accept)。
- **不能换监听器**:`TcpListener::accept` 写死在循环里,你想换 `UnixListener`、想做单元测试用 `tokio::io::duplex` 造假连接,都要把整个循环改一遍。

这不是危言耸听。这就是 2020-2022 年 Rust 异步 Web 生态的状态——hyper 1.0 还没出,hyper 0.14 自带了一个 `hyper::Server`(封装了 accept 循环 + graceful shutdown),但 1.0 把它拆掉了(协议原语留主仓、策略拆 hyper-util,见《hyper》1.0 重构章),留给框架层自己封。axum 封的就是这一层——而且封得格外薄、格外可复用。

**样板爆炸的真实代价**。把上面四条具体化:Rust 异步生态有 axum/actix-web/pingora 三个主流框架,各自要写 accept 循环 + graceful shutdown,三家各写一套就有三套 bug。axum 把这套封装抽到 `axum::serve`,一家维护,所有 axum 用户共享——你写 `axum::serve(listener, router).await`,这一行背后 200 行模板代码全免。

> **钉死这件事**:`axum::serve` 的全部存在意义,是把"hyper-util 给的单连接协议机"封装成"一个能上线接客的服务器"——封的是 accept 循环、错误重试、每连接 spawn task、`TokioIo`/`TowerToHyperService` 适配、HTTP/2 CONNECT 开关、graceful shutdown 编排。这一层 hyper 故意不做(协议层不该规定连接生命周期),axum 做了,而且做得薄。本书这一章就是拆这层薄封装。

### 所以这样设计:一个 `serve` 函数 + 一个 `Serve` 结构

axum 的封装长这样——一个函数 `serve`,返回一个 `Serve<L, M, S>` 结构(实现了 `IntoFuture`,`await` 它就跑起来)。先看签名(`axum/src/serve/mod.rs#L97-L109`):

```rust
// axum/src/serve/mod.rs#L97-L109(逐字摘录)
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

三个泛型参数,逐条拆:

- **`L: Listener`** —— 监听器,本章第二节详拆。这是 axum 自定义的 trait,不是 `tokio::net::TcpListener`。它是本章的招牌技巧之一。
- **`M: for<'a> Service<IncomingStream<'a, L>, Error = Infallible, Response = S>`** —— "MakeService",每来一个连接,调它的 `call`,吐出一个 `S`(真正处理请求的 Service)。这一层承 P1-02 已详拆(`IntoMakeService` / `IntoMakeServiceWithConnectInfo` / 直接传 Router),本章第三节复习 + 深挖 `IntoMakeServiceWithConnectInfo`。
- **`S: Service<Request, Response = Response, Error = Infallible> + Clone + Send + 'static`** —— 真正处理请求的 Service。`Clone` 是关键——因为每连接要 clone 一份(承 P1-02)。`Error = Infallible` 意味着 axum 在框架层把所有错误转成 `Response`(404/405/500),P5-18 详拆。

返回 `Serve<L, M, S>`(`mod.rs#L114-L118`):

```rust
// axum/src/serve/mod.rs#L114-L118(逐字摘录)
#[cfg(all(feature = "tokio", any(feature = "http1", feature = "http2")))]
#[must_use = "futures must be awaited or polled"]
pub struct Serve<L, M, S> {
    listener: L,
    make_service: M,
    _marker: PhantomData<fn() -> S>,
}
```

`Serve` 是一个 `must_use` 的 future(`await` 它才跑)。它有 `with_graceful_shutdown(signal)`(`mod.rs#L151-L161`)和 `local_addr()`(`mod.rs#L164-L166`)两个方法,实现了 `IntoFuture`(`mod.rs#L218-L233`)——`await` 它内部跑 `Serve::run`(`mod.rs#L179-L193`,第四节详拆)。

注意 `#[cfg(all(feature = "tokio", any(feature = "http1", feature = "http2")))]`——`serve` 同时依赖 `tokio` feature 和 (`http1` 或 `http2`) feature。没 `tokio` feature(只有 `hyper` 没运行时)或没 `http1`/`http2`(协议都没开)都编译不出 `serve`。这是 axum 把"serve 跑在 Tokio 上 + 协议在 hyper"这件事钉死在 feature flag 里的体现。

> **钉死这件事**:`axum::serve(listener, make_service)` 的三件套(`L: Listener` / `M: MakeService` / `S: Service`)是 axum 把"hyper-util 单连接协议机"封装成"服务器"的全部抽象入口。监听器抽象成 `Listener` trait(可换 TCP/Unix/自定义),Service 工厂抽象成 `MakeService`(可换 Router 直接传 / `IntoMakeService` / `IntoMakeServiceWithConnectInfo`),真正处理请求的 `S` 钉死成 `Service<Request, Error = Infallible> + Clone + Send + 'static`。这三个泛型参数是 axum serve 的全部可扩展点。

---

## 第二节:Listener trait——把监听器抽象成"接客器"

### 提问

`axum::serve` 的第一个参数是 `L: Listener`。为什么不是 `L: tokio::net::TcpListener`?为什么 axum 要自己定义一个 `Listener` trait?这一节是本章的招牌技巧之一——把 TCP/Unix/自定义 IO 统一成同一种监听器。

### 不这样会怎样:写死 `TcpListener` 会怎样

假设 axum 把 `serve` 的签名写死成 `TcpListener`:

```rust
// 假想的写死版本(非 axum 实际做法)
pub fn serve<M, S>(listener: TcpListener, make_service: M) -> Serve<TcpListener, M, S>
where ...
```

立刻出现三个问题:

1. **不能换 Unix domain socket**。Linux 生产环境里,反向代理(Nginx/Envoy)到后端服务用 Unix socket 比用 TCP 快(没 TCP 栈开销,没端口耗尽问题)。axum 写死 TCP,这些场景就没法用 `axum::serve`,只能退回手写 hyper-util。actix-web 早期就因为 HttpServer 写死 TCP,Unix socket 支持一直是个补丁。
2. **不能做单元测试**。测试里你想模拟"客户端发了一个请求,服务器响应了",但 `TcpListener::bind` 要真端口、真 socket,测试要起真 TCP 连接,慢且 flaky。理想是用 `tokio::io::duplex` 造一对内存里的假 IO(`duplex(1024)` 返回 `(client, server)`,往 `client` 写什么 `server` 就读到什么),把 `server` 塞给"假监听器",测试里就能不发真 TCP 包跑完整个 axum 协议链路。axum 写死 TCP 就做不到这个。
3. **不能自定义连接预处理**。你想给每个进来的 `TcpStream` 设 `TCP_NODELAY`(禁用 Nagle 算法,小包低延迟场景必须)——`TcpListener::accept` 返回的 `TcpStream` 默认开 Nagle,HTTP 这种"请求-响应"模式被 Nagle 拖。axum 写死 `TcpListener::accept`,你没机会在 accept 之后、`serve_connection` 之前插一步"改 socket 选项"。

### 所以这样设计:抽象出 `Listener` trait

axum 自己定义一个 `Listener` trait(`axum/src/serve/listener.rs#L9-L24`):

```rust
// axum/src/serve/listener.rs#L9-L24(逐字摘录)
/// Types that can listen for connections.
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

整个 trait 只有四个要素:

1. **`type Io: AsyncRead + AsyncWrite + Unpin + Send + 'static`** —— 这台监听器 accept 出来的"连接"是什么类型。对 `TcpListener`,`Io = tokio::net::TcpStream`;对 `UnixListener`,`Io = tokio::net::UnixStream`;对测试用的假监听器,`Io = tokio::io::DuplexStream`(或任何 `duplex` 返回的类型)。**关键是约束在 `AsyncRead + AsyncWrite`**——Tokio 的 IO trait(承《Tokio》,一句带过),任何能"读字节 + 写字节"的东西都行。
2. **`type Addr: Send`** —— 这台监听器的"地址"是什么类型。对 `TcpListener`,`Addr = std::net::SocketAddr`(IP+端口);对 `UnixListener`,`Addr = tokio::net::unix::SocketAddr`(文件系统路径);对假监听器,`Addr = ()`(没地址)。
3. **`fn accept(&mut self) -> impl Future<Output = (Self::Io, Self::Addr)> + Send`** —— 接一个连接,返回 `(IO, Addr)`。**注意返回值不是 `Result`**——这是关键的语义选择,下面专拆。
4. **`fn local_addr(&self) -> io::Result<Self::Addr>`** —— 拿本地绑的地址(给 `Serve::local_addr()` 用,生产环境要把 `0.0.0.0:0` 实际绑到的端口打日志)。

把这个 trait 摊开:

```text
                          Listener trait
                ┌──────────────────────────────────┐
                │  type Io:  AsyncRead + AsyncWrite │  ← 连接的字节流类型
                │  type Addr: Send                  │  ← 地址类型
                │  fn accept() -> (Io, Addr)        │  ← 接客(永不返回 Err)
                │  fn local_addr() -> Addr          │  ← 自己绑在哪
                └──────────────────────────────────┘
                                    ▲
                                    │ impl
            ┌───────────────────────┼────────────────────────┐
            │                       │                        │
   ┌────────────────────┐  ┌────────────────────┐  ┌────────────────────────┐
   │  tokio::net::      │  │  tokio::net::      │  │  TapIo<L, F>           │
   │  TcpListener       │  │  UnixListener      │  │  (ListenerExt::tap_io) │
   ├────────────────────┤  ├────────────────────┤  ├────────────────────────┤
   │  Io  = TcpStream   │  │  Io  = UnixStream  │  │  Io  = L::Io           │
   │  Addr= SocketAddr  │  │  Addr= unix::Addr  │  │  Addr= L::Addr         │
   └────────────────────┘  └────────────────────┘  │  accept: 多跑 tap_fn   │
                                                   └────────────────────────┘
                          ▲
                          │ (用户测试自定义)
              ┌───────────────────────────┐
              │  ReadyListener<T>         │
              │  (源码 tests 里)         │
              ├───────────────────────────┤
              │  Io  = T (任意 duplex 流) │
              │  Addr= ()                 │
              └───────────────────────────┘
```

图里三个具体实现:

**`TcpListener` 的实现**(`listener.rs#L26-L43`):

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
    fn local_addr(&self) -> io::Result<Self::Addr> {
        Self::local_addr(self)
    }
}
```

注意 `accept` 被包了一个 `loop`:遇到 Err 不返回,而是 `handle_accept_error(e).await`(下面专拆),只有 `Ok` 才 `return`。所以这个 `accept` **永不返回 Err**——错误在内部处理掉了。这就是为什么 trait 的 `accept` 签名返回 `(Io, Addr)` 而不是 `Result<(Io, Addr), io::Error>`。

**`UnixListener` 的实现**(`listener.rs#L45-L63`,`#[cfg(unix)]`):

```rust
// axum/src/serve/listener.rs#L45-L63(逐字摘录)
#[cfg(unix)]
impl Listener for tokio::net::UnixListener {
    type Io = tokio::net::UnixStream;
    type Addr = tokio::net::unix::SocketAddr;

    async fn accept(&mut self) -> (Self::Io, Self::Addr) {
        loop {
            match Self::accept(self).await {
                Ok(tup) => return tup,
                Err(e) => handle_accept_error(e).await,
            }
        }
    }

    #[inline]
    fn local_addr(&self) -> io::Result<Self::Addr> {
        Self::local_addr(self)
    }
}
```

和 `TcpListener` 一模一样的 `loop + handle_accept_error` 模板,只有 `Io` / `Addr` 关联类型不同。这就是 trait 抽象的威力——`serve::run` 里的 `listener.accept().await` 调用,对 `TcpListener` 和 `UnixListener` 是同一段代码,泛型单态化成两份具体实现,运行时零开销。

**`TapIo<L, F>` 的实现**(`listener.rs#L121-L138`):

```rust
// axum/src/serve/listener.rs#L121-L138(逐字摘录)
impl<L, F> Listener for TapIo<L, F>
where
    L: Listener,
    F: FnMut(&mut L::Io) + Send + 'static,
{
    type Io = L::Io;
    type Addr = L::Addr;

    async fn accept(&mut self) -> (Self::Io, Self::Addr) {
        let (mut io, addr) = self.listener.accept().await;
        (self.tap_fn)(&mut io);
        (io, addr)
    }

    fn local_addr(&self) -> io::Result<Self::Addr> {
        self.listener.local_addr()
    }
}
```

`TapIo` 是装饰器——包一个内层 `Listener`,在 `accept` 拿到 `(io, addr)` 后,对 `io` 跑一个 `tap_fn(&mut io)`(可变闭包),再返回。这是给"`TCP_NODELAY`"这种"每个 accept 出来的 socket 都要设一下"的场景用的。用法(`listener.rs#L67-L98`):

```rust
let listener = tokio::net::TcpListener::bind("0.0.0.0:3000").await?
    .tap_io(|tcp_stream| {
        if let Err(err) = tcp_stream.set_nodelay(true) {
            tracing::trace!("failed to set TCP_NODELAY: {err:#}");
        }
    });
axum::serve(listener, app).await?;
```

`ListenerExt::tap_io`(`listener.rs#L66-L98`)是个扩展方法,所有 `Listener` 都能调它,返回 `TapIo<Self, F>`——`TapIo` 自己也实现 `Listener`,所以可以继续传给 `axum::serve`。这是"装饰器模式"在监听器层的落地,承《Tower》Layer 的同类思想(组合而非继承),但这里是监听器组合不是 Service 组合。

### `accept` 为什么不返回 `Result`——错误吞在 trait 实现里

注意 `Listener::accept` 的签名是 `fn accept(&mut self) -> impl Future<Output = (Self::Io, Self::Addr)> + Send`,**没有 `Result`,没有 `Err`**。这和 `tokio::net::TcpListener::accept` 的 `async fn accept(&self) -> io::Result<(TcpStream, SocketAddr)>` 不一样——后者会返回 Err,axum 的版本不会。

为什么?注释写得很直白(`listener.rs#L17-L20`):

> If the underlying accept call can return an error, this function must take care of logging and retrying.

翻译:trait 的契约是"实现者负责处理错误"——你要么重试到成功,要么永远阻塞,**就是不能把错误冒泡给调用方**。`TcpListener`/`UnixListener` 的实现用 `loop { match ... Err(e) => handle_accept_error(e).await }` 实现这个契约——错误在内部被吞掉(记日志 + sleep 1 秒重试),只把 `Ok` 的 `(Io, Addr)` 返回。

`handle_accept_error`(`listener.rs#L140-L158`)是 axum 处理 accept 错误的核心:

```rust
// axum/src/serve/listener.rs#L140-L158(逐字摘录)
async fn handle_accept_error(e: io::Error) {
    if is_connection_error(&e) {
        return;
    }

    // [From `hyper::Server` in 0.14](https://github.com/hyperium/hyper/blob/v0.14.27/src/server/tcp.rs#L186)
    //
    // > A possible scenario is that the process has hit the max open files
    // > allowed, and so trying to accept a new connection will fail with
    // > `EMFILE`. In some cases, it's preferable to just wait for some time, if
    // > the application will likely close some files (or connections), and try
    // > to accept the connection again. If this option is `true`, the error
    // > will be logged at the `error` level, since it is still a big deal,
    // > and then the listener will sleep for 1 second.
    //
    // hyper allowed customizing this but axum does not.
    error!("accept error: {e}");
    tokio::time::sleep(Duration::from_secs(1)).await;
}

fn is_connection_error(e: &io::Error) -> bool {
    matches!(
        e.kind(),
        io::ErrorKind::ConnectionRefused
            | io::ErrorKind::ConnectionAborted
            | io::ErrorKind::ConnectionReset
    )
}
```

两类错误:

- **连接级错误**(`ConnectionRefused`/`ConnectionAborted`/`ConnectionReset`)——对端在握手阶段反悔了(防火墙 RST、客户端改主意),这种"丢了就丢了",直接 `return` 让外层 `loop` 立刻重 accept 下一个,不 sleep。
- **其他错误**(`EMFILE` 文件描述符耗尽、`ENOMEM`、`EINTR` 等系统级)——记 `error!` 日志,**睡 1 秒再重试**。

注释直接点明这段逻辑继承自 hyper 0.14 的 `hyper::Server`(链接给了 v0.14.27 的 `server/tcp.rs#L186`)——hyper 0.14 自带 server 时就内置了"sleep 1 秒重试 EMFILE"的逻辑,hyper 1.0 把 server 拆走后,axum 把这段逻辑原样继承过来。`EMFILE` 的真实场景:进程打开的文件描述符数(`ulimit -n`)到上限了,这时再 accept 新连接会失败。生产环境遇到流量突增或连接泄漏,经常踩到 `EMFILE`——这时直接 panic 或退出会让所有在途请求也挂掉,sleep 1 秒(等应用关掉一些旧连接释放 fd)重试是更稳的选择。注释最后一句也诚实:"hyper allowed customizing this but axum does not"——hyper 0.14 允许配置 sleep 时长,axum 写死 1 秒,不让配。这是 axum "薄封装但够用"的取舍(要更精细的控制就退回裸 hyper-util)。

> **钉死这件事**:`Listener::accept` 不返回 `Result`,错误在 trait 实现里被吞——`handle_accept_error` 区分"连接级错误(直接重试)"和"系统级错误(`EMFILE` 等,睡 1 秒重试)"。这段逻辑直接继承自 hyper 0.14 的 `Server::accept`。为什么不让错误冒泡?因为 `serve::run` 那个 `loop` 不知道怎么处理 accept 错误——它是无限循环,任何 Err 都只能"记日志 + 重试"或"panic 退出"。axum 选了前者,把重试逻辑封进 trait 实现,让 `serve::run` 的循环保持极简(下面会看到,就 4 行)。

### 真实测试:用假 IO 跑通整个 axum 协议链路

axum 源码 tests 里有一个 `serving_on_custom_io_type` 测试(`serve/mod.rs#L684-L727`),完整展示了 `Listener` 抽象的扩展性:

```rust
// axum/src/serve/mod.rs#L684-L727(逐字摘录,关键部分)
#[crate::test]
async fn serving_on_custom_io_type() {
    struct ReadyListener<T>(Option<T>);

    impl<T> Listener for ReadyListener<T>
    where
        T: AsyncRead + AsyncWrite + Unpin + Send + 'static,
    {
        type Io = T;
        type Addr = ();

        async fn accept(&mut self) -> (Self::Io, Self::Addr) {
            match self.0.take() {
                Some(server) => (server, ()),
                None => std::future::pending().await,
            }
        }

        fn local_addr(&self) -> io::Result<Self::Addr> {
            Ok(())
        }
    }

    let (client, server) = io::duplex(1024);
    let listener = ReadyListener(Some(server));

    let app = Router::new().route("/", get(|| async { "Hello, World!" }));

    tokio::spawn(serve(listener, app).into_future());

    let stream = TokioIo::new(client);
    let (mut sender, conn) = hyper::client::conn::http1::handshake(stream).await.unwrap();
    tokio::spawn(conn);

    let request = Request::builder().body(Body::empty()).unwrap();

    let response = sender.send_request(request).await.unwrap();
    assert_eq!(response.status(), StatusCode::OK);

    let body = Body::new(response.into_body());
    let body = to_bytes(body, usize::MAX).await.unwrap();
    let body = String::from_utf8(body.to_vec()).unwrap();
    assert_eq!(body, "Hello, World!");
}
```

这个测试是 `Listener` 抽象的最佳广告:

- 自定义一个 `ReadyListener<T>`,持有一个 `Option<T>`(只能 accept 一次,第二次 `pending().await` 永远阻塞)。
- `T: AsyncRead + AsyncWrite + Unpin + Send + 'static`——任何满足这些约束的 IO 都能当 `Io`(约束和 trait 定义一致)。
- 用 `tokio::io::duplex(1024)` 造一对内存里的假 IO(`client` / `server`),`server` 塞给 `ReadyListener`,`client` 拿来当客户端。
- `axum::serve(ReadyListener(Some(server)), app)` 直接跑——不发任何真 TCP 包,整个 axum 协议链路(accept → handle_connection → spawn → serve_connection → Router → handler → Response)在内存里跑完。

这就是 `Listener` 抽象的回报——单元测试能造一个"假监听器"直接跑 axum,不用真 TCP 端口、不用 `TestClient`(虽然 axum 提供了 `TestClient` 给更高级的测试用,见 `axum-extra`)。这一节是 `Listener` trait 招牌技巧的实战证据。

### 反面对比:hyper 原生 `serve_connection` 怎么做

为了让你感受 axum 封了什么,做个反面对比。hyper 原生(没 axum)怎么写一个支持 TCP + Unix 的服务器:

```rust
// 朴素写法:hyper 原生(简化示意,非源码原文)
async fn serve_tcp(listener: TcpListener, app: Router<()>) {
    loop {
        let (stream, addr) = listener.accept().await?;  // ★ 错误自己处理
        let io = TokioIo::new(stream);
        let svc = TowerToHyperService::new(app.clone());
        tokio::spawn(async move {
            let _ = Builder::new(TokioExecutor::new())
                .serve_connection_with_upgrades(io, svc).await;
        });
    }
}

async fn serve_unix(listener: UnixListener, app: Router<()>) {
    loop {
        let (stream, addr) = listener.accept().await?;  // ★ 几乎一样的循环
        let io = TokioIo::new(stream);
        let svc = TowerToHyperService::new(app.clone());
        tokio::spawn(async move {
            let _ = Builder::new(TokioExecutor::new())
                .serve_connection_with_upgrades(io, svc).await;
        });
    }
}
```

两个函数,除了 `TcpListener`/`UnixListener` 和 `TcpStream`/`UnixStream` 类型不同,逻辑一模一样。代码重复 100%。axum 用 `Listener` trait 把这两个统一成一段代码,泛型单态化成两份具体实现——你写一遍 `axum::serve`,TCP 和 Unix 都支持。这是抽象的经典回报:**消除重复 + 单态化零开销**。

### 对照 actix-web / go net/http 的监听器抽象

监听器抽象这事,不是 axum 独有。看看同类系统怎么做:

| 系统 | 监听器抽象 | TCP/Unix 切换 | 自定义 IO |
|------|------------|---------------|-----------|
| **axum** | `Listener` trait(关联类型 `Io`/`Addr`) | 一行换(`TcpListener` ↔ `UnixListener`) | 支持(`duplex` 假 IO) |
| **hyper 原生** | `AsyncRead + AsyncWrite` 约束(直接接 IO,不接 listener) | 自己写 accept 循环 | 支持(`TokioIo` 适配) |
| **actix-web** | `HttpServer::new(|| app).bind(addr)` / `bind_uds(path)` | 不同方法(`bind` vs `bind_uds`) | 不支持(自实现 accept,IO 类型写死) |
| **go net/http** | `net.Listener` interface(`Accept() (Conn, error)`) | 一行换(`net.Listen("tcp" vs "unix")`) | 支持(实现 `net.Listener` 接口) |

go 的 `net.Listener` interface 和 axum 的 `Listener` trait **思想完全一致**——都是"接客器"抽象,关联类型/返回类型是连接(`net.Conn` / `(Io, Addr)`)。差别在 go 的 `Accept() (Conn, error)` 返回 `error`(让上层自己处理),axum 的 `accept` 不返回 `Result`(实现者自己吞)。这是两种取舍:go 让错误冒泡给调用方决定(更灵活,但每个调用点都要写 `if err != nil`),axum 把错误处理钉死在 trait 实现里(更省心,但不灵活)。actix-web 最死板——`bind` 和 `bind_uds` 是两个方法,内部 accept 循环写死,自定义 IO 不支持。

> **钉死这件事**:axum 的 `Listener` trait(关联类型 `Io: AsyncRead+AsyncWrite` + `Addr` + `accept -> (Io, Addr)` + `local_addr`)把 TCP/Unix/自定义 IO 统一成同一种监听器,代价是 trait 实现者要自己处理 accept 错误(`handle_accept_error` 睡 1 秒重试 `EMFILE`)。回报是 `axum::serve` 一份代码支持所有监听器类型,泛型单态化零开销。对照 go `net.Listener` 接口同思路(但 go 返回 error、axum 不返回),对照 actix-web 写死 TCP(不支持自定义 IO)。这是 axum 比 hyper 原生便利、比 actix-web 灵活的关键设计点。

---

## 第三节:MakeService 适配——复习 P1-02 + 深挖 `IntoMakeServiceWithConnectInfo`

### 提问

`serve` 的第二个参数 `M: Service<IncomingStream, Response = S>` 是"MakeService"——每来一个连接,调它的 `call`,吐出一个真正处理请求的 Service `S`。这一层在 P1-02 已经拆过(`IntoMakeService` 是纯克隆工厂),本章只复习 + 深挖 P1-02 没展开的 `IntoMakeServiceWithConnectInfo`(它是 P5-17 的招牌之一:把对端信息塞进 Request extensions)。

### 复习:`IntoMakeService` 和"直接传 Router"

> **承接 P1-02**:`IntoMakeService<S>`(`axum/src/routing/into_make_service.rs#L1-L44`)是个纯克隆工厂——`call(_target)` 就一行 `self.svc.clone()`。它的存在纯粹是签名匹配,把"一个共享 `S`"翻译成"每连接一份"。P1-02 拆透了,本章不重复。

axum 给你三种"把 Router 交给 serve"的便利写法(`routing/mod.rs#L527-L541, L549-L566`):

```rust
// 写法一:直接传 Router(用 Router<()> impl Service<IncomingStream>)
axum::serve(listener, router.clone());

// 写法二:显式 MakeService
axum::serve(listener, router.into_make_service());

// 写法三:带连接信息
axum::serve(listener, router.into_make_service_with_connect_info::<SocketAddr>());
```

写法一的根是 `Router<()>` 直接实现了 `Service<IncomingStream<'_, L>>`(`routing/mod.rs#L549-L566`),它的 `call` 是 `self.clone().with_state(())`——克隆一份并消耗 state(把 handler 预先转成 `Route` 类型擦除,而不是每请求转一次)。写法二等价(`Router::into_make_service` 内部就是 `IntoMakeService::new(self.with_state(()))`,`routing/mod.rs#L528-L532`)。写法三是本章的深挖点。

### 深挖:`IntoMakeServiceWithConnectInfo` 多干了什么

`IntoMakeService<S>` 是纯克隆,不看 `target`(`_target: T`,连 `IncomingStream` 的 `remote_addr` 都忽略)。但有时候 handler 要知道"这个请求来自哪个 IP"——日志要记、限流要按 IP、白名单要判。这时用 `IntoMakeServiceWithConnectInfo`(`axum/src/extract/connect_info.rs#L28-L46, L114-L133`)。

结构定义(`connect_info.rs#L28-L31`):

```rust
// axum/src/extract/connect_info.rs#L28-L31(逐字摘录)
pub struct IntoMakeServiceWithConnectInfo<S, C> {
    svc: S,
    _connect_info: PhantomData<fn() -> C>,
}
```

两个泛型:`S` 是被包的 Service(通常是 `Router<()>`),`C` 是你想提取的"连接信息类型"(通常是 `SocketAddr`,也可以是自定义类型)。`PhantomData<fn() -> C>` 是个惯用法——用 `fn() -> C` 而不是裸 `C`,让结构体对 `C` **不变(invariant)**且不要求 `C: 'static`(裸 `PhantomData<C>` 会让 `C` 协变,某些场景类型推断会出错;`fn() -> C` 是更安全的 marker 写法,承 Rust 惯用法)。

`Service<T>` 实现(`connect_info.rs#L114-L133`):

```rust
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

和 `IntoMakeService` 的 `call` 对比,多干了两件事:

**第一件:`C::connect_info(target)`**。`Connected` trait(`connect_info.rs#L80-L83`)是从 `target` 提取连接信息的抽象:

```rust
// axum/src/extract/connect_info.rs#L80-L83(逐字摘录)
pub trait Connected<T>: Clone + Send + Sync + 'static {
    /// Create type holding information about the connection.
    fn connect_info(stream: T) -> Self;
}
```

`Connected<T>` 是个泛型 trait——"`T` 类型(通常 `IncomingStream`)可以产生 `Self`(连接信息)"。axum 给两个内置实现(`connect_info.rs#L85-L106`):

```rust
// axum/src/extract/connect_info.rs#L90-L94(逐字摘录)
impl Connected<serve::IncomingStream<'_, TcpListener>> for SocketAddr {
    fn connect_info(stream: serve::IncomingStream<'_, TcpListener>) -> Self {
        *stream.remote_addr()
    }
}
```

`SocketAddr: Connected<IncomingStream<'_, TcpListener>>`——从 `IncomingStream`(持有 `remote_addr: SocketAddr`)提取 `SocketAddr` 就一行 `*stream.remote_addr()`(解引用拷贝,因为 `SocketAddr: Copy`)。这就是"`ConnectInfo<SocketAddr>` 提取器能拿到对端 IP"的根——`IntoMakeServiceWithConnectInfo::<SocketAddr>` 在 MakeService 层把 `remote_addr` 抓出来,塞进后续每条连接的 Request extensions。

注意第二个实现(`connect_info.rs#L96-L105`):

```rust
// axum/src/extract/connect_info.rs#L96-L105(逐字摘录)
impl<'a, L, F> Connected<serve::IncomingStream<'a, serve::TapIo<L, F>>> for L::Addr
where
    L: serve::Listener,
    L::Addr: Clone + Sync + 'static,
    F: FnMut(&mut L::Io) + Send + 'static,
{
    fn connect_info(stream: serve::IncomingStream<'a, serve::TapIo<L, F>>) -> Self {
        stream.remote_addr().clone()
    }
}
```

这是给 `TapIo`(第二节讲的 `tap_io` 装饰器)用的——`Listener::tap_io` 返回的 `TapIo<L, F>` 也实现了 `Listener`,它的 `IncomingStream` 的 `remote_addr` 类型是 `L::Addr`(内层监听器的地址类型)。所以 `L::Addr: Connected<IncomingStream<TapIo<L, F>>>`,从 `TapIo` 的 stream 里提 `L::Addr`。这是装饰器模式在 `Connected` trait 上的延续——`tap_io` 不影响 `connect_info` 的提取逻辑,只是包了一层。

**第二件:`Extension(connect_info).layer(self.svc.clone())`**。拿到 `connect_info: ConnectInfo<C>` 后,用 Tower 的 `Extension` Layer(承《Tower》,就是往 Request extensions 塞一个值的 Layer)把它套在克隆出来的 Service 外面。返回 `AddExtension<S, ConnectInfo<C>>`——这是被 `Extension` Layer 装饰过的 Service,每次 `call(req)` 时会把 `ConnectInfo<C>` 塞进 `req.extensions_mut()`。

这样,handler 里写 `ConnectInfo<SocketAddr>` 提取器,就能从 extensions 拿到对端 IP。`ConnectInfo` 提取器的实现(`connect_info.rs#L150-L169`):

```rust
// axum/src/extract/connect_info.rs#L150-L169(逐字摘录)
#[derive(Clone, Copy, Debug)]
pub struct ConnectInfo<T>(pub T);

impl<S, T> FromRequestParts<S> for ConnectInfo<T>
where
    S: Send + Sync,
    T: Clone + Send + Sync + 'static,
{
    type Rejection = <Extension<Self> as FromRequestParts<S>>::Rejection;

    async fn from_request_parts(parts: &mut Parts, state: &S) -> Result<Self, Self::Rejection> {
        match Extension::<Self>::from_request_parts(parts, state).await {
            Ok(Extension(connect_info)) => Ok(connect_info),
            Err(err) => match parts.extensions.get::<MockConnectInfo<T>>() {
                Some(MockConnectInfo(connect_info)) => Ok(Self(connect_info.clone())),
                None => Err(err),
            },
        }
    }
}
```

`ConnectInfo<T>` 是个 newtype(`pub struct ConnectInfo<T>(pub T)`,字段 public,所以 `ConnectInfo<SocketAddr>` 可以直接解构成 `SocketAddr`)。`FromRequestParts` 实现内部用 `Extension::<Self>::from_request_parts`(承 P3-11 的 Extension 提取器,从 extensions 拿值)——如果 extensions 里有 `ConnectInfo<T>` 就拿到,否则查 `MockConnectInfo<T>`(测试用 mock,P5-17 不展开),都没有就返回 rejection(运行时错,因为 `into_make_service_with_connect_info` 没用)。

注意一个**关键陷阱**:`ConnectInfo` 提取器要求你**必须**用 `into_make_service_with_connect_info::<C>()` 启动 axum,否则 extensions 里没塞 `ConnectInfo`,提取器运行时 panic(返回 rejection)。这是 axum 文档反复强调的点(`connect_info.rs#L142-L149` 注释):

> Note this extractor requires you to use [`Router::into_make_service_with_connect_info`] to run your app otherwise it will fail at runtime.

源码 tests 里的 `socket_addr` 测试(`connect_info.rs#L240-L267`)完整演示了"启动时带 connect_info,handler 里提取"的全流程——本章不展开,留作扩展阅读。

### 三种 MakeService 形态的对照(承 P1-02 表格 + 本章深化)

| 形态 | 类型 | 怎么用 | `call` 干什么 | 用 ConnectInfo 提取器? |
|------|------|--------|---------------|----------------------|
| **直接 Router** | `Router<()>` impl `Service<IncomingStream>` | `axum::serve(listener, router)` | `self.clone().with_state(())` | 不能(没塞 ConnectInfo) |
| **IntoMakeService** | `IntoMakeService<Router<()>>` | `axum::serve(listener, router.into_make_service())` | `self.svc.clone()` | 不能(同上) |
| **IntoMakeServiceWithConnectInfo** | `IntoMakeServiceWithConnectInfo<Router<()>, C>` | `axum::serve(listener, router.into_make_service_with_connect_info::<SocketAddr>())` | `Extension(ConnectInfo).layer(self.svc.clone())` | 能(每连接塞 ConnectInfo 到 extensions) |

三种形态的 `call` 都返回一个 `Service<Request>`(克隆的 Router,可能套了 `Extension`),交给 hyper-util 的 `serve_connection` 反复 `call` 处理这条连接上的每个请求。差别只在"`call` 时是否看 target、是否预处理 extensions"。

> **钉死这件事**:`IntoMakeServiceWithConnectInfo<S, C>` 比 `IntoMakeService<S>` 多干两件事:① 用 `Connected` trait 从 `IncomingStream` 提取连接信息(对 `TcpListener` 就是 `*stream.remote_addr()`);② 用 Tower `Extension` Layer 把 `ConnectInfo<C>` 塞进每条连接的 Request extensions,让 handler 里 `ConnectInfo<C>` 提取器能拿到。`Connected<T>` 是可扩展点——你可以给自定义 IO 类型实现 `Connected<IncomingStream<YourListener>>`,提取自定义的连接信息(源码 tests 的 `custom` 测试演示了)。`ConnectInfo` 提取器要求**必须**用 `into_make_service_with_connect_info`,否则运行时 panic——这是 axum 文档反复强调的陷阱。

### `Connected` trait 的扩展点:自定义连接信息

`Connected<T>` trait 最值钱的地方是**可扩展**——你不只能提 `SocketAddr`,能提任何 `Clone + Send + Sync + 'static` 的类型。源码 tests 里的 `custom` 测试(`connect_info.rs#L269-L309`)演示:

```rust
// axum/src/extract/connect_info.rs#L271-L282(摘录)
#[derive(Clone, Debug)]
struct MyConnectInfo {
    value: &'static str,
}

impl Connected<IncomingStream<'_, TcpListener>> for MyConnectInfo {
    fn connect_info(_target: IncomingStream<'_, TcpListener>) -> Self {
        Self { value: "it worked!" }
    }
}

async fn handler(ConnectInfo(addr): ConnectInfo<MyConnectInfo>) -> &'static str {
    addr.value
}

let app = Router::new().route("/", get(handler));
crate::serve(
    listener,
    app.into_make_service_with_connect_info::<MyConnectInfo>(),
);
```

`MyConnectInfo` 持有任意字段(这里是 `value: &'static str`,实际可以是 TLS 证书信息、客户端 cert subject、TLS SNI hostname 等),给 `IncomingStream<'_, TcpListener>` 实现 `Connected`,然后 `into_make_service_with_connect_info::<MyConnectInfo>()`。handler 里 `ConnectInfo<MyConnectInfo>` 就能拿到自定义信息。

这是 axum 把"连接信息提取"做成可扩展 trait 的回报——TLS 终端(在 axum 之外做 TLS 卸载,把客户端证书信息塞进 `MyConnectInfo`)、自定义协议头(从某种 proxy protocol 提取真实客户端 IP,绕过 HTTP `X-Forwarded-For` 不可信问题)这些场景,都能用同一套 `Connected` + `ConnectInfo` 机制处理。对照 go net/http 的 `Request.RemoteAddr`(写死 string,要自己解析),axum 的可扩展性是明显的优势。

---

## 第四节:handle_connection——把字节流变成跑起来的连接 task

### 提问

第一二三节拆了 `serve` 的签名、`Listener` 抽象、MakeService 适配。现在看真正干活的 `handle_connection`——它是 axum 把"accept 到的字节流"变成"跑起来的连接 task"的全部逻辑。这一节是本章的运行画面核心。

### `handle_connection` 全貌

`handle_connection`(`axum/src/serve/mod.rs#L353-L416`)是 axum serve 的心脏,逐段拆:

```rust
// axum/src/serve/mod.rs#L353-L416(逐字摘录)
async fn handle_connection<L, M, S>(
    make_service: &mut M,
    signal_tx: &watch::Sender<()>,
    close_rx: &watch::Receiver<()>,
    io: <L as Listener>::Io,
    remote_addr: <L as Listener>::Addr,
) where
    L: Listener,
    L::Addr: Debug,
    M: for<'a> Service<IncomingStream<'a, L>, Error = Infallible, Response = S> + Send + 'static,
    for<'a> <M as Service<IncomingStream<'a, L>>>::Future: Send,
    S: Service<Request, Response = Response, Error = Infallible> + Clone + Send + 'static,
    S::Future: Send,
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

**第一步:`TokioIo::new(io)`**(`mod.rs#L367`)。`tokio::net::TcpStream` 实现 `tokio::io::AsyncRead/AsyncWrite`,但 hyper-util 的 `serve_connection` 期望的是 hyper 的 IO trait(`hyper::rt::Read/Write`,基于 `poll_read`/`poll_write` 但和 tokio 的签名略有不同)。`TokioIo` 是 hyper-util 提供的适配器,把 `tokio::io::AsyncRead/AsyncWrite` 适配成 hyper 的 IO trait。

> **承接《hyper》[[hyper-source-facts]] + 《Tokio》[[tokio-source-facts]]**:hyper 1.0 不直接接 tokio IO,要 `TokioIo` 适配(底层是 `hyper::rt` 的 `Read`/`Write` trait,tokio 是 `AsyncRead`/`AsyncWrite`,两者 `poll_read` 签名不同,`TokioIo` 做桥接)。tokio 的 `AsyncRead`/`AsyncWrite`/`poll_read` 在《Tokio》拆透了,一句带过指路。hyper 的 RT trait 在《hyper》拆透了,一句带过指路。

**第二步:`make_service.ready().await`**(`mod.rs#L371-L374`)。这是 Tower 的 `ServiceExt::ready`(`tower::ServiceExt`,承《Tower》P1-02,一句带过),内部 `poll_ready` 直到 `Ready`。`unwrap_or_else(|err| match err {})` 这种写法是因为 `Error = Infallible`,err 不可达,模式匹配空 `match` 把它消解掉——这是 Rust 处理 `Infallible` 的惯用法(承 P1-02)。

**第三步:`make_service.call(IncomingStream { io: &io, remote_addr }).await`**(`mod.rs#L376-L382`)。**这是"每连接调一次 `MakeService::call`"**。这一步吐出一个 `S`(真正的请求处理 Service)。注意 `IncomingStream` 持有 `&io`(IO 的引用)和 `remote_addr`(对端地址拷贝,`SocketAddr` 是 `Copy`)——这个引用只在 `make_service.call` 期间存活,Service 造完就不用 IO 了(IO 后续交给 `serve_connection`,第五步详拆)。

`IncomingStream`(`mod.rs#L424-L430`):

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

它提供 `io()` 和 `remote_addr()` 访问器(`mod.rs#L432-L445`),给 `Connected` trait 实现里用(第三节已拆)。

**第四步:`.map_request(|req: Request<Incoming>| req.map(Body::new))`**(`mod.rs#L383`)。这一步把"返回的 Service `S`"包装一下,让它能把 hyper 解析出来的 `Request<Incoming>`(body 类型是 `hyper::body::Incoming`)转换成 axum 的 `Request<axum_core::body::Body>`。`req.map(Body::new)` 是 `http::Request::map`,把 body 类型从 `Incoming` 换成 `Body`(axum 自己的 body 类型,包了 hyper 的 body)。`map_request` 是 Tower 的工具(`tower::util::MapRequestLayer`,承《Tower》),给每个进入的 Request 套一个转换函数。这一步承 P1-02 已拆,本章不重复。

**第五步:`TowerToHyperService::new(tower_service)`**(`mod.rs#L385`)。**axum 和 hyper-util 之间的最后一道桥**。axum 的 `tower_service` 是 `tower_service::Service<Request, ...>`(带 `poll_ready`、`&mut self`),但 hyper-util 的 `serve_connection` 期望的是 `hyper::service::Service`(**没 `poll_ready`**、`call(&self)`)。

> **承接《hyper》**:hyper 1.x 把自己的 Service trait 删了 `poll_ready`(承《hyper》P1-02 招牌对照)。hyper-util 提供 `TowerToHyperService` 这个适配器,把 `tower_service::Service`(带 `poll_ready` + `&mut self`)适配成 `hyper::service::Service`(无 `poll_ready` + `&self`)。它的实现是:在每次 `call(&self, req)` 时,内部 `clone` 一份 tower service(因为 tower 的 `call` 是 `&mut self`,要拿独占),跑它的 `poll_ready`(axum 的 `poll_ready` 无条件 Ready),然后 `call`。详见《hyper》P4-P5,本章一句带过。

**第六步:`tokio::spawn(async move { ... })`**(`mod.rs#L389-L415`)。**每个连接 spawn 一个独立 task**。task 里干四件事:

- `Builder::new(TokioExecutor::new())`(`mod.rs#L391`)——hyper-util 的 `auto::Builder`,自动协商 HTTP/1 + HTTP/2(承《hyper》P5,一句带过)。`auto` 意思是"看客户端发来的字节流前几个字节,如果是 `PRI * HTTP/2` 前奏就走 HTTP/2,否则走 HTTP/1"。这一步 axum 一行没写,全靠 hyper-util。
- `builder.http2().enable_connect_protocol()`(`mod.rs#L394`,`#[cfg(feature = "http2")]`)——开启 HTTP/2 CONNECT 协议(给 WebSocket over HTTP/2 用,详见《hyper》P2-07 + 本书 P5-19)。注释直接写明:`// CONNECT protocol needed for HTTP/2 websockets`。
- `let mut conn = pin!(builder.serve_connection_with_upgrades(io, hyper_service))`(`mod.rs#L396`)——hyper-util 的连接服务,把 IO 和 Service 拼起来,跑 HTTP 协议机。`pin!` 是 `tokio::macro::pin`(`std::pin::pin` 的 tokio 别名),把 future 钉在栈上,后续 `conn.as_mut()` 可变借用它(因为要循环 poll,且要调 `conn.graceful_shutdown()`)。
- `let mut signal_closed = pin!(signal_tx.closed().fuse())`(`mod.rs#L397`)——`signal_tx.closed()` 是 tokio `watch::Sender::closed`,返回一个 future,当所有 receiver 都 drop(或 sender drop)时完成。`.fuse()` 让这个 future 可以被多次 poll(`select!` 循环里要反复 poll,普通 future poll 一次后再 poll 是 UB,`Fuse` 包装后"已完成就永远完成",`select!` 安全)。
- 外层 `loop { tokio::select! { conn | signal_closed } }`(`mod.rs#L399-L412`)——让"连接处理"和"shutdown 信号"抢跑。`conn.as_mut()`(协议机跑完或出错)就 `break`;`signal_closed`(主循环发了 shutdown 信号)就调 `conn.as_mut().graceful_shutdown()`——这是 hyper-util 协议机的优雅关闭方法,"做完手头这一轮就退出,不再接新请求"。
- `drop(close_rx)`(`mod.rs#L414`)——task 结束时 drop 掉 close_rx 的 receiver,让主循环的 `close_tx.closed()` 能感知到"这个 task 结束了"(graceful shutdown 的收尾,第五节详拆)。

> **钉死这件事**:`handle_connection` 的六步是 axum 把字节流变成跑起来的连接 task 的全部逻辑:`TokioIo::new`(tokio IO → hyper IO)→ `make_service.ready`(Tower ServiceExt)→ `make_service.call(IncomingStream)`(克隆 Service)→ `map_request(Body::new)`(hyper body → axum body)→ `TowerToHyperService::new`(tower Service → hyper Service)→ `tokio::spawn`(每连接一 task 跑 `serve_connection`,内层 `select!` 让 conn 和 shutdown 信号抢跑)。这六步每一行都不可省——tokio IO 不适配 hyper 编译过运行 panic、map_request 不做 body 类型对不上、不 spawn task 就没法并发接多连接、不 select shutdown 信号就没法 graceful。axum 把这六步封进一个函数,你 `axum::serve(listener, router).await` 一行搞定。

### 把这一节画成图:serve 内部时序

```mermaid
sequenceDiagram
    autonum
    participant Loop as serve::run<br/>主循环
    participant L as listener<br/>(Listener)
    participant HC as handle_connection
    participant MS as make_service<br/>(IntoMakeService...)
    participant SP as tokio::spawn
    participant HU as hyper-util<br/>serve_connection

    Loop->>L: listener.accept().await
    L-->>Loop: (io, remote_addr) (永不返回 Err)
    Loop->>HC: handle_connection(io, remote_addr)
    HC->>HC: TokioIo::new(io) (tokio IO → hyper IO)
    HC->>MS: make_service.ready().await (Tower ServiceExt)
    HC->>MS: make_service.call(IncomingStream { io, remote_addr })
    Note over MS: 每连接 clone 一份 Service<br/>(Router<()> 内部 Arc::clone, 近乎零成本)
    MS-->>HC: tower_service: Service&lt;Request&gt;
    HC->>HC: map_request(Body::new) (hyper body → axum body)
    HC->>HC: TowerToHyperService::new (tower → hyper Service)
    HC->>SP: tokio::spawn(async move { ... })
    Note over SP: 主循环立刻返回, 继续接下一个连接
    SP->>HU: Builder::new(TokioExecutor).http2().enable_connect_protocol()
    HU->>HU: serve_connection_with_upgrades(io, hyper_service)
    Note over HU: 协议机解析字节流<br/>auto: HTTP/1 还是 HTTP/2
    HU->>HU: 调 hyper_service.call(&self, Request) (P1-02 下半场)
    HU-->>SP: Response (反复, 直到连接关闭)
    SP->>SP: drop(close_rx) (graceful shutdown 收尾)
```

注意时序图里两个关键点:(1)`handle_connection` 是 `async fn`,但它**内部 spawn 了 task**,所以 `handle_connection` 本身返回很快(主要等 `make_service.call` 那一步,通常 `ready(Ok(clone))` 一瞬完成),主循环立刻继续 accept 下一个——**accept 是串行的,但连接处理是并发的**(每连接独立 task);(2)`make_service.call` 返回的 Service 在 task 里被 `serve_connection` 反复 `call` 处理这条连接上的每个请求(HTTP/1 流水线、HTTP/2 多 stream),Service 是这条连接的整个生命周期里复用的——这就是为什么 Service 必须 `Clone + Send + 'static`(task 要 own 它,跨线程移动)。

### `Serve::run` 的最简循环

回看 `Serve::run`(`mod.rs#L179-L193`):

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

注意返回类型 `-> !`——这个函数**永远不返回**(never type)。它是个无限循环,要么永远跑下去,要么进程 panic/被 kill。这就是为什么 `serve` 文档(`mod.rs#L83-L88`)写:

> Although this future resolves to `io::Result<()>`, it will never actually complete or return an error. Errors on the TCP socket will be handled by sleeping for a short while (currently, one second).

`IntoFuture for Serve`(`mod.rs#L218-L233`)把 `run()` 包成 `private::ServeFuture`(返回 `io::Result<()>`),但实际上这个 `Ok(())` 永远到不了——`run` 是 `!` 类型。这是 Rust 类型系统表达"这个 future 永不完成"的方式(承 `!` never type,稳定于 Rust 1.41)。

`run` 里两个 `watch::channel(())`(`signal` 和 `close`):这一版(没 graceful shutdown)其实不用它们——`_signal_rx` 和 `_close_tx` 都带下划线前缀,表示"建出来但不用"(只是为了传给 `handle_connection` 签名匹配)。真正用上 signal/close 的是 `with_graceful_shutdown` 版本的 `run`(第五节详拆)。

主循环体就两行:`listener.accept().await` 拿 `(io, addr)`,`handle_connection(...).await` 处理。`handle_connection` 内部 spawn task 后立刻返回,主循环继续 accept——**这就是 axum 的并发模型**:

> **承接《Tokio》**:axum 用"每连接 spawn 一个 tokio task"的并发模型(承《Tokio》task 调度,一句带过指路 [[tokio-source-facts]])。Tokio 的 task 极轻(MIB 级内存,调度开销亚微秒),所以"每连接一 task"在万级并发下没问题。对照 actix-web 用 worker thread 模型(N 个 worker,连接在 worker 间分发),go 用 goroutine(也是每连接一 goroutine,和 axum 同思路)。这种模型的好处是"handler 写起来像同步代码"(`async fn` 里 `await`),Tokio 自动在 await 点切换 task,实现高并发。

---

## 第五节:graceful shutdown——不丢在途请求的时序编排

### 提问

`axum::serve(listener, router).with_graceful_shutdown(shutdown_signal()).await`——这一行里的 `shutdown_signal()` 是一个返回 `Future<Output = ()>` 的函数(典型实现是 `async { tokio::signal::ctrl_c().await.unwrap(); }`)。这个 future 完成时,serve 会"停止 accept 新连接,但等在途的连接跑完"。问题是:**怎么做到"等在途跑完"?它怎么知道有多少在途?怎么避免无限等?**

这一节是本章最值钱的部分——graceful shutdown 的"停 accept 等在途"语义怎么实现。

### 不这样会怎样:粗暴 shutdown 会怎样

假设 axum 不做 graceful shutdown,收到信号直接 `std::process::exit(0)` 或 drop listener + 所有 task:

- **在途请求被强杀**:一个 POST `/upload` 正在传 100MB 文件,传了 80MB,你 drop 掉这个 task——客户端拿到 connection reset,80MB 白传,要重传。一个 DELETE `/users/42` 删了一半数据库记录,你 drop 掉 task——数据库状态不一致。
- **keep-alive 连接被强断**:HTTP/1 的 keep-alive 连接、HTTP/2 的多路复用连接,客户端期望"这条连接可以继续发下一个请求",你强断,客户端要重连(慢 + 可能丢请求)。
- **WebSocket / SSE 长连接被强断**:WebSocket 是长连接,你强断,客户端要重连 + 重做握手 + 状态恢复(可能不可恢复)。

生产环境的部署(Kubernetes rolling update、SIGTERM 优雅下线)都要求 graceful shutdown——接到 SIGTERM 后,服务停止接新连接,等在途的跑完(或超时强杀)。这是上线服务的硬需求。

### 所以这样设计:`with_graceful_shutdown` + watch channel 编排

`with_graceful_shutdown` 的入口(`mod.rs#L151-L161`):

```rust
// axum/src/serve/mod.rs#L151-L161(逐字摘录)
pub fn with_graceful_shutdown<F>(self, signal: F) -> WithGracefulShutdown<L, M, S, F>
where
    F: Future<Output = ()> + Send + 'static,
{
    WithGracefulShutdown {
        listener: self.listener,
        make_service: self.make_service,
        signal,
        _marker: PhantomData,
    }
}
```

返回 `WithGracefulShutdown<L, M, S, F>`(另一结构,实现 `IntoFuture`)。`signal: F` 是用户传的 future(`Output = ()` + `Send + 'static`),完成时触发 graceful shutdown。

`WithGracefulShutdown::run`(`mod.rs#L267-L305`)是真正的编排:

```rust
// axum/src/serve/mod.rs#L267-L305(逐字摘录)
async fn run(self) {
    let Self {
        mut listener,
        mut make_service,
        signal,
        _marker,
    } = self;

    let (signal_tx, signal_rx) = watch::channel(());
    tokio::spawn(async move {
        signal.await;
        trace!("received graceful shutdown signal. Telling tasks to shutdown");
        drop(signal_rx);
    });

    let (close_tx, close_rx) = watch::channel(());

    loop {
        let (io, remote_addr) = tokio::select! {
            conn = listener.accept() => conn,
            _ = signal_tx.closed() => {
                trace!("signal received, not accepting new connections");
                break;
            }
        };

        handle_connection(&mut make_service, &signal_tx, &close_rx, io, remote_addr).await;
    }

    drop(close_rx);
    drop(listener);

    trace!(
        "waiting for {} task(s) to finish",
        close_tx.receiver_count()
    );
    close_tx.closed().await;
}
```

逐段拆,这是 graceful shutdown 的核心:

**第一步:起 signal 监听 task**(`mod.rs#L275-L280`):

```rust
let (signal_tx, signal_rx) = watch::channel(());
tokio::spawn(async move {
    signal.await;
    trace!("received graceful shutdown signal. Telling tasks to shutdown");
    drop(signal_rx);
});
```

建一个 `watch::channel(())`——`watch` 是 tokio 的"广播 channel,只保留最新值"原语(承《Tokio》一句带过)。`signal_tx` 是 sender,`signal_rx` 是 receiver。spawn 一个 task,在里面 `signal.await`(等用户的 shutdown signal 完成),完成后 `drop(signal_rx)`。

**为什么 drop signal_rx 就能触发 shutdown**?关键在 `watch::Sender::closed()` 的语义——它返回的 future 在**所有 receiver 都 drop(或 sender 自己 drop)** 时完成。`signal_rx` 是主循环持有的最后一个 receiver(`_signal_rx` 那个 underscore 是在没 graceful shutdown 的版本,这里 `signal_rx` 被 move 进了 spawn 的 task),所以 `signal_rx` drop 时,`signal_tx.closed()` 的 future 完成。

主循环的 `tokio::select!`(`mod.rs#L284-L291`)就是在等这个:

```rust
loop {
    let (io, remote_addr) = tokio::select! {
        conn = listener.accept() => conn,
        _ = signal_tx.closed() => {
            trace!("signal received, not accepting new connections");
            break;
        }
    };

    handle_connection(&mut make_service, &signal_tx, &close_rx, io, remote_addr).await;
}
```

`select!` 让 `listener.accept()` 和 `signal_tx.closed()` 抢跑——accept 到新连接就处理它,`signal_tx.closed()` 完成(用户的 shutdown signal 触发了)就 `break` 跳出循环。**跳出循环就是"停止 accept 新连接"**。

**第二步:break 出循环,等在途 task 结束**(`mod.rs#L296-L304`):

```rust
drop(close_rx);
drop(listener);

trace!(
    "waiting for {} task(s) to finish",
    close_tx.receiver_count()
);
close_tx.closed().await;
```

跳出循环后:

- `drop(close_rx)` —— 主循环自己持有一份 `close_rx`(在 `handle_connection` 调用时 clone 给每个 task,但主循环自己留了一份),drop 掉它,让 `close_tx.closed()` 的"等所有 receiver drop"的计数减一。
- `drop(listener)` —— 不再持有 listener,释放资源(端口、fd)。注意这时**已经没在 accept 了**(break 出了循环),drop listener 只是释放资源,不影响在途连接。
- `close_tx.receiver_count()` —— 打日志,显示还有多少个 task 持有 `close_rx`(多少在途连接)。
- `close_tx.closed().await` —— **这是"等在途跑完"的核心**。`close_tx.closed()` 的 future 在所有 `close_rx` 都 drop 时完成。每个 `handle_connection` spawn 的 task 持有一份 `close_rx`(在 `mod.rs#L387` 那里 `let close_rx = close_rx.clone();`),task 结束时 `drop(close_rx)`(`mod.rs#L414`)。所以 `close_tx.closed().await` 等的就是"所有在途连接 task 都结束"。

这就是 graceful shutdown 的"等在途"语义——**不是无限等,而是等所有 `close_rx` 归零**(每个 task 结束 drop 自己的 close_rx,最后一个 drop 时 `closed()` 完成)。

### 在途 task 内部:收到 signal 后怎么做

每个 `handle_connection` spawn 的 task(第四节已拆)内部,也有 `select!` 让 conn 和 shutdown 信号抢跑(`mod.rs#L399-L412`):

```rust
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
```

`signal_closed` 是 `signal_tx.closed().fuse()`(第四节已拆)——当用户的 shutdown signal 触发、`signal_rx` drop、`signal_tx.closed()` 完成时,所有 task 的 `signal_closed` 也完成。这时 task 调 `conn.as_mut().graceful_shutdown()`——hyper-util 协议机的优雅关闭方法。

> **承接《hyper》[[hyper-source-facts]]**:`hyper_util::server::conn::auto::Builder::serve_connection` 返回的 future 上有个 `graceful_shutdown(&mut self)` 方法——它告诉协议机"不要再接新请求了(HTTP/1 不读新的流水线请求、HTTP/2 不接新 stream),但让手头正在处理的请求跑完"。这是 hyper-util 提供的协议级 graceful shutdown,axum 一行没写,只是调它。详见《hyper》P4-P5,本章一句带过。

调完 `graceful_shutdown` 后,task 的 `select!` 循环继续——下一轮 `conn.as_mut()` 会 poll 到"协议机做完所有在途请求并关闭连接"完成,task `break` 退出,`drop(close_rx)` 让主循环的 `close_tx.closed()` 计数减一。

### 把这一节画成图:graceful shutdown 时序

```mermaid
sequenceDiagram
    autonum
    participant U as 用户 signal<br/>(ctrl_c / SIGTERM)
    participant Sig as signal 监听 task
    participant Loop as WithGracefulShutdown::run<br/>主循环
    participant Task1 as 连接 task 1<br/>(在途请求)
    participant Task2 as 连接 task 2<br/>(在途请求)
    participant HU as hyper-util<br/>serve_connection

    U->>Sig: signal.await 完成
    Sig->>Sig: drop(signal_rx)
    Note over Sig: signal_tx.closed() 现在可以完成了
    Loop->>Loop: select! { accept | signal_tx.closed() }
    Loop->>Loop: signal_tx.closed() 完成, break 出循环
    Loop->>Loop: drop(close_rx) (主循环自己的那份)
    Loop->>Loop: drop(listener) (不再 accept)
    Loop->>Loop: close_tx.closed().await (等所有 task drop close_rx)

    par 并发通知所有 task
        Loop-->>Task1: signal_tx.closed() 完成 (通过 watch 广播)
        Task1->>Task1: select! 捕获 signal_closed
        Task1->>HU: conn.graceful_shutdown()
        Note over HU: 协议机做完手头请求就退出<br/>不接新请求(HTTP/1 不读流水线/HTTP/2 不接新 stream)
        HU-->>Task1: conn 完成
        Task1->>Task1: drop(close_rx) ★
    and
        Loop-->>Task2: signal_tx.closed() 完成
        Task2->>Task2: select! 捕获 signal_closed
        Task2->>HU: conn.graceful_shutdown()
        HU-->>Task2: conn 完成
        Task2->>Task2: drop(close_rx) ★
    end

    Note over Loop: close_tx 的所有 receiver 都 drop 了
    Loop->>Loop: close_tx.closed() 完成
    Loop-->>U: run() 返回 (Ok(()))
```

这张图是 graceful shutdown 的全部时序。关键点:

1. **shutdown signal 通过 `watch` 广播**——`signal_tx.closed()` 是广播"signal 来了",所有 task 的 `signal_closed` future 同时完成。这是 `watch::Sender::closed()` 的语义(对所有 holder 都可见)。
2. **每个 task 自己决定怎么 graceful**——task 收到 signal 后,调 `conn.graceful_shutdown()`,让 hyper-util 协议机"做完手头请求就退出"。每个 task 独立做这件事,主循环不参与。
3. **`close_tx.closed().await` 等所有 task 结束**——每个 task 结束 `drop(close_rx)`,最后一个 drop 时 `closed()` 完成。这是"等在途"的机制。
4. **不会无限等**——如果某个 task 永远不结束(比如 handler 死循环、客户端永远不 close 连接),`close_tx.closed()` 永远不完成,主循环永远卡在 `await`。**这是 axum graceful shutdown 的一个已知限制**——没有总超时。生产环境通常在外层包一个 `tokio::time::timeout(Duration::from_secs(30), serve.await)` 兜底,超时强杀。

### graceful shutdown 的 sound 性:为什么不丢在途请求

把"为什么不丢在途"这件事钉死:

- **新请求**:break 出 accept 循环后,listener 不再 accept,新连接在内核队列里(`TCP backlog`)排队,最终被内核丢弃(对端拿到 connection refused)或超时。**axum 不会"accept 了又强杀"**——没 accept 就没启动 task,没启动 task 就没开始处理。所以新请求不会被"半处理"。
- **在途请求**:每个在途 task 收到 signal 后调 `conn.graceful_shutdown()`——hyper-util 协议机**做完手头这一轮**才退出。HTTP/1 的"手头这一轮"是"当前请求的 Response 发完",HTTP/2 的"手头这一轮"是"所有已开始的 stream 都完成"。协议机不会"请求处理一半就把连接断了"——它知道协议状态,会等 Response 写完才 close 连接。
- **handler 内部**:如果 handler 是 `async fn upload(file: Bytes) -> ...`,正在 `await` 收 body,graceful shutdown 不会中断这个 `await`——task 会等 handler 自然完成(返回 Response),协议机把这个 Response 发完,才 close。所以"上传到 80MB 被强杀"这种情况**不会发生**(除非加了总超时强杀)。

唯一可能"丢"的场景:**handler 自己 panic**。如果 handler panic,task 退出,`close_rx` 被 drop,主循环以为"这个 task 正常结束"——但这个请求实际上没正确响应(对端拿到 connection reset)。这是 panic 的固有行为,axum 提供 `tower-http::catch_panic` Layer 兜底(把 panic 转成 500 Response),P5-18 详拆。这不是 graceful shutdown 的问题,是 handler 的问题。

> **钉死这件事**:graceful shutdown 的"不丢在途"靠三件事:① break 出 accept 循环(不接新请求,没启动 task 就没开始处理);② 每个 task 调 `conn.graceful_shutdown()`(hyper-util 协议机做完手头这一轮才退出,不会请求处理一半就断);③ 主循环 `close_tx.closed().await` 等所有 task `drop(close_rx)`(等所有在途 task 自然结束)。这套机制 sound 的根是"hyper-util 协议机知道协议状态,会等 Response 发完才 close"——这是 hyper 协议层的保证,axum 只是编排。唯一限制是没总超时,生产环境要外层包 `tokio::time::timeout`。

---

## 第六节:和 go net/http / actix-web 的 serve 对照

### 提问

"启动一个 Web 服务器 + graceful shutdown"这事,不是 axum 独有。go net/http 有 `http.Server{Handler}.Serve(listener)` + `Shutdown(ctx)`,actix-web 有 `HttpServer::new(...).bind(...).run()`。它们和 axum 的 `serve` 有什么本质差别?

把对照钉死,你才能理解 axum serve 的独特之处。

### 对照 go net/http:内置 Server + 无 Listener trait

go 的标准库 Web 服务器:

```go
// go net/http(简化示意)
mux := http.NewServeMux()
mux.HandleFunc("/", handler)

server := &http.Server{
    Addr:    ":3000",
    Handler: mux,
}

go func() {
    if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
        log.Fatal(err)
    }
}()

// graceful shutdown
quit := make(chan os.Signal, 1)
signal.Notify(quit, syscall.SIGINT, syscall.SIGTERM)
<-quit

ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
defer cancel()
if err := server.Shutdown(ctx); err != nil {
    log.Fatal(err)
}
```

go 的 `http.Server` 是**完整的内置服务器**——accept 循环、graceful shutdown、HTTP/1 协议机全在标准库里。`ListenAndServe` 内部 `net.Listen("tcp", addr)` + `server.Serve(listener)`,后者 `for { conn := listener.Accept(); go server.serve(conn) }`。`server.Shutdown(ctx)` 做 graceful:停 accept + 等所有在途 goroutine,带 `ctx` 超时(axum 没内置超时,go 内置)。

关键差别:

1. **go 没 `Listener` trait 抽象**——`http.Server.Serve(listener net.Listener)` 接受任何实现 `net.Listener` 接口的类型(`Accept() (net.Conn, error)`),TCP/Unix 切换是 `net.Listen("tcp", addr)` vs `net.Listen("unix", path)`,得到不同的具体类型但都实现 `net.Listener`。思路和 axum `Listener` trait 一样(都是"接客器"抽象),差别是 go 用 interface(运行时虚分派),axum 用 trait + 关联类型(编译期单态化)。
2. **go 内置总超时**——`server.Shutdown(ctx)` 接 `context.Context`,超时强杀。axum 没内置超时,要外层包 `tokio::time::timeout`。这是 go 更便利的一点。
3. **go 不分层(协议+服务器全在标准库)**——`http.Server` 既是协议机又是服务器,没拆。axum 分层:hyper 是协议机,hyper-util 是策略,axum 是封装。axum 分层的好处是"协议机可被其他框架用"(tonic/Pingora 都用 hyper),go 的 `http.Server` 绑死 net/http 协议机,不能换。

### 对照 actix-web:HttpServer + 自实现 accept

actix-web 的服务器:

```rust
// actix-web(简化示意)
HttpServer::new(|| {
    App::new().route("/", web::get().to(handler))
})
.bind("0.0.0.0:3000")?     // 或 .bind_uds("/tmp/app.sock")?
.run()
.await?;
```

actix-web 的 `HttpServer` 是**自实现 accept + worker 模型**:

1. **worker thread 模型**:actix-web 默认起 N 个 worker thread(每个一个 tokio runtime thread),accept 在主 thread,连接在 worker 间分发(类似 Nginx 的 worker 模型)。对照 axum 是"每连接一 tokio task,共用 tokio runtime 的所有 worker thread"(task 不绑死 worker)。
2. **`bind` vs `bind_uds`**:TCP 和 Unix socket 是两个不同的方法(`HttpServer::bind` vs `HttpServer::bind_uds`),内部 accept 循环写死 IO 类型。不支持自定义 IO(不能 `duplex` 造假连接跑测试)。
3. **自实现 HTTP/1**:actix-web 历史上有自己的 HTTP/1 实现(`actix-http`),HTTP/2 用 `h2` crate。axum 全靠 hyper-util(自研 HTTP/1 + h2)。

axum 和 actix-web 的根本架构差异:**axum 全 Tokio + 全 hyper-util,actix-web 自实现 accept + 部分 HTTP 协议**。axum 的好处是"复用 hyper 生态、和 tonic/reqwest 共享底层",actix-web 的好处是"协议层可控、worker 模型成熟"。详见 P7-21 收束章对照。

### 三方 serve 对照表

| 维度 | axum | go net/http | actix-web |
|------|------|-------------|-----------|
| **协议机** | hyper-util(auto HTTP/1+2) | go 内置 net/http | actix-http(HTTP/1 自研)+ h2 |
| **监听器抽象** | `Listener` trait(关联类型 Io/Addr) | `net.Listener` interface | 写死(bind vs bind_uds) |
| **accept 模型** | 主循环串行 accept + 每连接 spawn task | 主循环串行 accept + 每连接 go goroutine | 主 thread accept + worker thread 分发 |
| **accept 错误** | trait 实现吞(sleep 1 秒重试 EMFILE) | 返回 error,调用方处理 | 自实现,内部处理 |
| **MakeService 适配** | 有(`IntoMakeService` 等,承 Tower Service 模型) | 无(Handler 共享,无 Clone 代价) | 有(App::new 闭包每 worker 调一次) |
| **graceful shutdown** | `with_graceful_shutdown(signal)`,watch 编排 | `server.Shutdown(ctx)`,带超时 | `HttpServer::handle()` + Signal trait |
| **总超时** | 无(外层包 `tokio::time::timeout`) | 有(`context.WithTimeout`) | 有(类似 go) |
| **自定义 IO 测试** | 支持(`duplex` 假 IO) | 支持(`net.Pipe`) | 不支持 |

这张表钉死后,你就理解了 axum serve 的独特之处:**全 Tokio + 全 hyper-util + `Listener` trait 抽象 + watch 编排 graceful shutdown**。它没有 go 的内置总超时(要外层包),也没有 actix-web 的 worker 模型(用 tokio task 替代),但它的"复用 hyper 生态 + 监听器可扩展 + graceful shutdown 不丢在途"这套组合,是 Rust 异步 Web 框架里最清爽的。

---

## 技巧精解

这一节挑两个本章最该被钉死的技巧,配真实源码 + 行号 + 反面对比,单独拆透。

### 技巧一:`Listener` trait 抽象——为什么用关联类型而非泛型方法

**它解决什么问题**:把 TCP/Unix/自定义 IO 统一成同一种监听器,让 `axum::serve` 一份代码支持所有监听器类型。

**反面对比:如果用泛型方法会怎样**:

`Listener` trait 的 `accept` 签名是 `fn accept(&mut self) -> impl Future<Output = (Self::Io, Self::Addr)> + Send`。`Io` 和 `Addr` 是**关联类型**(`type Io`、`type Addr`)。假设改成**泛型方法**:

```rust
// 假想的泛型方法版本(非 axum 实际做法)
pub trait Listener: Send + 'static {
    fn accept<Io: AsyncRead + AsyncWrite, Addr: Send>(
        &mut self,
    ) -> impl Future<Output = (Io, Addr)> + Send;
    // ...
}
```

立刻撞墙:

1. **每次 accept 可能返回不同 Io/Addr 类型**?泛型方法意味着同一个 `Listener` 可以在不同调用里返回不同的 `Io`/`Addr`——语义上不对(一个 TCP listener 永远 accept 出 `TcpStream`,不会这次 `TcpStream` 下次 `UnixStream`)。泛型方法表达不了"这个 listener 永远产出同一种 Io"的约束。
2. **`serve` 函数签名爆炸**:`serve<L: Listener, Io, Addr, M, S>` 要多两个泛型参数,且 `L::accept<Io, Addr>` 的约束写起来极复杂。
3. **类型推断失败**:`let (io, addr) = listener.accept().await;` 编译器不知道 `io`/`addr` 是什么类型,要显式标注(每次 accept 都标)。

关联类型解决这一切——`type Io`、`type Addr` 表示"这个 Listener 类型决定了唯一的 Io/Addr 类型",一个 `TcpListener` 永远 `Io = TcpStream`、`Addr = SocketAddr`,编译期钉死。`serve<L: Listener>` 一个泛型参数够,`L::Io`/`L::Addr` 自动推断。这是 Rust trait 设计的经典选择:**关联类型表达"唯一确定的类型",泛型表达"多种可能的类型"**。`Listener` 的 Io/Addr 是唯一的(一个监听器只产一种连接),所以用关联类型。

**真实实现**(`listener.rs#L9-L24`):

```rust
// axum/src/serve/listener.rs#L9-L24(逐字摘录)
pub trait Listener: Send + 'static {
    type Io: AsyncRead + AsyncWrite + Unpin + Send + 'static;
    type Addr: Send;

    fn accept(&mut self) -> impl Future<Output = (Self::Io, Self::Addr)> + Send;
    fn local_addr(&self) -> io::Result<Self::Addr>;
}
```

注意 `accept` 返回 `impl Future<...>`(RPITIT,承 Rust 1.75+),不是 `Pin<Box<dyn Future<...>>>`——零开销(没堆分配,单态化)。`+ Send` 让 future 可跨线程移动(承《Tokio》多线程调度,一句带过)。

**`type Io` 的约束为什么这么严**:`Io: AsyncRead + AsyncWrite + Unpin + Send + 'static`:

- `AsyncRead + AsyncWrite` —— 能读写字节流(给 hyper-util 协议机用)。
- `Unpin` —— 可以安全 `Pin<&mut Io>` 移动(因为 `AsyncRead::poll_read` 要 `Pin<&mut Self>`,Unpin 让这个 pin 不需要 unsafe)。
- `Send` —— 跨线程移动(`tokio::spawn` 的 task 要 Send)。
- `'static` —— 没非静态借用(同上,task 要 `'static`)。

这五个约束缺一不可,共同保证"`Io` 能塞进 hyper-util 的 `serve_connection` 跑起来"。`TcpStream`/`UnixStream`/`DuplexStream` 都满足(它们都是 `Owned + Send + 'static + Unpin` 的字节流)。

**对照"朴素地写死 TcpListener"会撞什么墙**:第二节已拆——不能换 Unix、不能造假 IO 测试、不能 `tap_io` 设 `TCP_NODELAY`。`Listener` trait 用关联类型 + 上述五约束,把"任何能读写字节的 owned Send IO"统一进来,代价只是 trait 实现者要自己处理 accept 错误(`handle_accept_error`)。

**对照 go `net.Listener` 接口**:go 用 interface(`Accept() (net.Conn, error)`),`net.Conn` 是另一个 interface(`Read/Write/Close/LocalAddr/RemoteAddr/SetDeadline`)。go 的接口是运行期虚分派(每次 `Accept`/`Read` 走 vtable),axum 的 trait + 关联类型是编译期单态化(每次 `accept` 直接调用 `TcpListener::accept`,没虚分派)。代价是 axum 的代码膨胀(每种 Listener 单态化一份),换来零运行时开销。这是 Rust vs Go 在抽象上的典型取舍。

> **钉死这件事**:`Listener` trait 用**关联类型** `type Io` / `type Addr`(不是泛型方法)表达"一个监听器类型唯一决定 Io/Addr 类型"的语义。`Io` 约束在 `AsyncRead + AsyncWrite + Unpin + Send + 'static`,把"任何 owned Send 字节流"统一进来。回报是 `axum::serve` 一份代码支持 TCP/Unix/自定义 IO,泛型单态化零开销。对照 go `net.Listener` interface(运行期虚分派)、actix-web 写死(`bind` vs `bind_uds`)。

### 技巧二:graceful shutdown 的 watch 编排——为什么用 watch 而非 mpsc/oneshot

**它解决什么问题**:在主循环和 N 个连接 task 之间编排"shutdown 信号广播 + 等所有 task 结束",做到不丢在途请求。

**反面对比:如果用 mpsc channel 会怎样**:

axum 用的是 `tokio::sync::watch::channel(())`——一个 sender,多个 receiver,广播"值变更"。假设换成 `mpsc::channel`:

```rust
// 假想的 mpsc 版本(非 axum 实际做法)
let (signal_tx, mut signal_rx) = mpsc::channel::<()>(1);
// 每个 task 持有一份 signal_tx,完成后发 ()
let signal_tx_for_task1 = signal_tx.clone();
tokio::spawn(async move {
    // ... 处理连接 ...
    let _ = signal_tx_for_task1.send(()).await;  // task 完成,发信号
});
// 主循环收 N 个信号
for _ in 0..num_tasks {
    signal_rx.recv().await;
}
```

立刻撞墙:

1. **要预先知道 task 数量**——主循环 `for _ in 0..num_tasks` 要知道有几个 task,但 task 是动态 spawn 的(每个 accept 都 spawn 一个),数量不固定。mpsc 的"收 N 个"模式不适用。
2. **信号方向反了**——mpsc 是"task 向主循环发",axum 需要的是"主循环向 task 广播 shutdown"(主循环触发 shutdown,task 接收)。mpsc 反过来不适配这个方向。
3. **每个 task clone sender 有开销**——`mpsc::Sender::clone` 分配内部 state,大量 task 时浪费。watch 的 receiver clone 是廉价的(Arc 共享)。

watch channel 完美匹配这个场景:

- **一个 sender,多个 receiver,广播值**——主循环持 `signal_tx`,每个 task 持 `signal_rx`(clone)。主循环 drop sender(或显式发值),所有 receiver 同时感知。
- **`Sender::closed()` future**——在所有 receiver drop 时完成。这就是"等所有 task 结束"的机制,不用数 task 数量。
- **receiver clone 廉价**——watch 的 receiver 内部是 `Arc` 共享 state,clone 近乎零成本。

**为什么不是 oneshot**:oneshot 是"一次性,一个 sender 一个 receiver",不能多 receiver。axum 要广播给 N 个 task,oneshot 不行。

**为什么 watch 而不是 broadcast**:`broadcast` 也能多 receiver,但它要保留消息历史(每个 receiver 收所有发过的值),开销大。watch 只保留**最新值**(receiver 只看当前值,不收历史),刚好匹配 axum 的需求(只关心"shutdown 了没",不关心历史)。

**真实实现**(`mod.rs#L275-L280, L282-L291, L296-L304`):

```rust
// axum/src/serve/mod.rs#L275-L280(逐字摘录)
let (signal_tx, signal_rx) = watch::channel(());
tokio::spawn(async move {
    signal.await;
    trace!("received graceful shutdown signal. Telling tasks to shutdown");
    drop(signal_rx);
});

// axum/src/serve/mod.rs#L282-L291(逐字摘录)
let (close_tx, close_rx) = watch::channel(());

loop {
    let (io, remote_addr) = tokio::select! {
        conn = listener.accept() => conn,
        _ = signal_tx.closed() => {
            trace!("signal received, not accepting new connections");
            break;
        }
    };
    handle_connection(&mut make_service, &signal_tx, &close_rx, io, remote_addr).await;
}

// axum/src/serve/mod.rs#L296-L304(逐字摘录)
drop(close_rx);
drop(listener);

trace!("waiting for {} task(s) to finish", close_tx.receiver_count());
close_tx.closed().await;
```

**两个 watch channel,两种角色**:

- **`signal` channel**(`signal_tx` / `signal_rx`):广播 shutdown 信号。spawn 一个监听 task,等用户 signal 完成后 `drop(signal_rx)`,触发 `signal_tx.closed()` 完成。主循环 `select!` 捕获这个完成,break 出循环。每个连接 task 也 `clone` 了一份 `signal_tx`(在 `handle_connection#L386` 那里 `let signal_tx = signal_tx.clone();`),task 内部 `signal_tx.closed()` 也完成,触发 `conn.graceful_shutdown()`。
- **`close` channel**(`close_tx` / `close_rx`):等所有连接 task 结束。每个连接 task 持有 `close_rx`(clone),结束时 `drop(close_rx)`。主循环 `drop(close_rx)`(自己的那份)后,`close_tx.closed()` 在所有 task 的 close_rx 都 drop 时完成。

**为什么用两个 channel**:`signal` 是"主循环 → task"方向的广播(触发 shutdown),`close` 是"task → 主循环"方向的归零(等 task 结束)。两个方向用两个 channel 各管一边,逻辑清晰。这是 axum 用最简原语(watch channel)编排复杂时序的典型例子。

> **承接《Tokio》[[tokio-source-facts]]**:tokio 的 `watch`/`mpsc`/`oneshot`/`broadcast` channel 全在《Tokio》拆透了(一句带过指路)。axum 选 watch 是因为它的"一个 sender 多 receiver + 只保留最新值 + `Sender::closed()` 在所有 receiver drop 时完成"语义,刚好匹配 graceful shutdown 的"广播 shutdown + 等所有 task 结束"场景。这是 tokio 原语选型的典型例子。

**对照 go net/http 的 `Shutdown`**:go 的 `server.Shutdown(ctx)` 内部用 `context.Context` 广播 shutdown + `sync.WaitGroup` 等 goroutine 结束。思路和 axum 一样(广播 + 等归零),差别是 go 用 Context + WaitGroup(语言级原语),axum 用 watch channel(async 原语)。go 的版本带 `ctx` 超时(强杀),axum 没内置超时。这是 go 更便利的一点(axum 要外层包 `tokio::time::timeout`)。

**对照 actix-web 的 graceful**:actix-web 用 `actix_rt::signal` + 自己的 worker 通知机制(worker 间用 actix message 传递)。比 axum 复杂(actix 的 actor 模型增加了一层间接),axum 直球用 tokio 原语。

> **钉死这件事**:axum graceful shutdown 用**两个 `tokio::sync::watch` channel** 编排——`signal`(主循环 → task 广播 shutdown)+ `close`(task → 主循环归零等结束)。watch 选型的根是"一个 sender 多 receiver + 只保留最新值 + `Sender::closed()` 在所有 receiver drop 时完成"。这是 axum 用最简 tokio 原语编排复杂时序的招牌。对照 go 用 Context+WaitGroup(带超时)、actix-web 用 actor message(更复杂)。axum 没内置总超时,要外层包 `tokio::time::timeout`——这是已知限制。

---

## 章末小结

回到全书的主轴:**路由与分发 vs 提取与响应**。

本章服务**总览**这一面——它不是路由(不解决 URL+method 找 handler),也不是提取响应(不解决参数怎么从 Request 来、返回值怎么变 Response),它是"把一个 Router 跑起来接客"的工程化封装。这一层封装:

- **`Listener` trait**(`serve/listener.rs`):抽象监听器,统一 TCP/Unix/自定义 IO,关联类型 `Io`/`Addr` + `accept -> (Io, Addr)` 三件套,泛型单态化零开销。这是本章招牌技巧之一。
- **`handle_connection`**(`serve/mod.rs#L353-L416`):accept 到的字节流变成跑起来的连接 task 的全部逻辑,六步(`TokioIo` 适配 → `MakeService::call` 克隆 Service → `map_request` 改 body → `TowerToHyperService` 适配 → `tokio::spawn` → 内层 `select!` 跑 `serve_connection`)。
- **`IntoMakeServiceWithConnectInfo`**(`extract/connect_info.rs`):比 `IntoMakeService` 多干"从 `IncomingStream` 提取连接信息(用 `Connected` trait)+ 套 `Extension` Layer 让 handler 能提取对端 IP"两件事,`Connected` 是可扩展点(支持自定义连接信息类型)。
- **graceful shutdown**(`WithGracefulShutdown::run`,`serve/mod.rs#L267-L305`):用两个 `tokio::sync::watch` channel 编排——`signal` 广播 shutdown(主循环 break 出 accept 循环 + 每个 task 调 `conn.graceful_shutdown()`),`close` 等所有 task `drop(close_rx)` 归零(主循环 `close_tx.closed().await`)。这套机制 sound 的根是 hyper-util 协议机"做完手头这一轮才退出"的协议级保证,所以不丢在途请求。

底层 hyper-util(协议机/连接管理/`serve_connection`/`graceful_shutdown`)、Tower(`Service`/`Layer`/`Extension`/`ServiceExt::ready`)、Tokio(`watch`/`mpsc`/`task`/`AsyncRead`/`AsyncWrite`)各承担一段——这些《hyper》《Tower》《Tokio》讲透的部分,本章一句带过指路。本章是 axum 在它们之上的薄封装,封的是 accept 循环、错误重试、每连接 spawn task、`TokioIo`/`TowerToHyperService` 适配、HTTP/2 CONNECT 开关、graceful shutdown 编排。

### 五个为什么清单

1. **为什么 `axum::serve` 是 hyper-util 的薄封装,而不是直接暴露 `serve_connection`?** 因为 hyper-util 给的是单连接协议机,要写一个能上线接客的服务器还要 accept 循环、错误重试、每连接 spawn task、IO/Service 适配、HTTP/2 CONNECT 开关、graceful shutdown 编排——这些机械活每个项目都要写一遍,axum 封进 `serve` 一家维护,所有 axum 用户共享。详见第一节。
2. **为什么 axum 自己定义 `Listener` trait,不直接用 `tokio::net::TcpListener`?** 因为要支持 TCP/Unix/自定义 IO(测试用 `duplex` 假 IO)。`Listener` trait 用关联类型 `Io`/`Addr` + `accept -> (Io, Addr)` 把这些统一进来,泛型单态化零开销。代价是 trait 实现者要自己处理 accept 错误(`handle_accept_error` 睡 1 秒重试 `EMFILE`)。详见第二节。
3. **为什么 `Listener::accept` 不返回 `Result`?** 因为 trait 契约是"实现者负责处理错误"——错误在 trait 实现里被吞(`handle_accept_error` 区分连接级错误直接重试 / 系统级错误睡 1 秒重试),让 `serve::run` 的循环保持极简(就 4 行:accept + handle_connection)。详见第二节。
4. **为什么 `IntoMakeServiceWithConnectInfo` 比 `IntoMakeService` 多两层逻辑?** 因为 handler 要拿对端 IP(日志/限流/白名单)——`IntoMakeServiceWithConnectInfo` 用 `Connected` trait 从 `IncomingStream` 提取连接信息(对 `TcpListener` 就是 `*stream.remote_addr()`),用 Tower `Extension` Layer 把 `ConnectInfo<C>` 塞进每条连接的 Request extensions。`ConnectInfo` 提取器要求**必须**用 `into_make_service_with_connect_info`,否则运行时 panic。详见第三节。
5. **为什么 graceful shutdown 不丢在途请求?** 靠三件事:① break 出 accept 循环(不接新请求,没启动 task 就没开始处理);② 每个 task 调 `conn.graceful_shutdown()`(hyper-util 协议机做完手头这一轮才退出);③ 主循环 `close_tx.closed().await` 等所有 task `drop(close_rx)`(等所有在途 task 自然结束)。这套用两个 `tokio::sync::watch` channel 编排,watch 选型的根是"一个 sender 多 receiver + `Sender::closed()` 在所有 receiver drop 时完成"。详见第五节。

### 想继续深入往哪钻

- **hyper-util 的 `auto::Builder::serve_connection` 内部怎么自动协商 HTTP/1+2、怎么做协议级 graceful shutdown、连接池/keep-alive**:→《hyper》P4-P5(连接管理),axum serve 是它的薄封装。
- **`TowerToHyperService` 怎么把 tower Service(`&mut self` + `poll_ready`)适配成 hyper Service(`&self` 无 `poll_ready`)、`TokioIo` 怎么适配 tokio/hyper IO trait**:→《hyper》P1-02(Service 招牌对照)+ P4-P5。
- **tokio 的 `watch`/`mpsc`/`oneshot`/`broadcast` channel 内部、`AsyncRead`/`AsyncWrite`/`poll_read`、`tokio::spawn` task 调度**:→《Tokio》,axum 全异步跑在 tokio 上,本章一句带过指路 [[tokio-source-facts]]。
- **Tower 的 `Service`/`Layer`/`Extension`/`ServiceExt::ready`/`ServiceBuilder`**:→《Tower》(成网后),axum 的中间件就是 Tower Layer,本章引用其用法。
- **WebSocket over HTTP/2 的 CONNECT 协议(`builder.http2().enable_connect_protocol()`)**:→ 本书 P5-19(WebSocket/SSE/流式响应),《hyper》P2-07。
- **错误处理:`Error = Infallible` 意味着什么、handler panic 怎么处理(`tower-http::catch_panic`)、`HandleErrorLayer`**:→ 本书 P5-18(错误处理)。
- **go net/http 的 `http.Server.Serve` + `Shutdown(ctx)` / actix-web 的 `HttpServer` + worker 模型 / tonic 的 gRPC server**:→ 本书 P7-21(全书收束双对照)。

### 引出下一章

本章你拿到了 `axum::serve` 的全部内部:`Listener` trait 抽象监听器、`handle_connection` 六步把字节流变连接 task、`IntoMakeServiceWithConnectInfo` 把对端信息塞 extensions、graceful shutdown 用两个 watch channel 编排不丢在途请求。但有一个我们刻意一笔带过的点——`S: Service<Request, Error = Infallible>` 里那个 `Error = Infallible`。为什么 axum 把 Service Error 钉死成 `Infallible`?如果某个 Tower 中间件(比如超时)返回了非 `Infallible` 的错误,怎么塞进 axum?handler panic 了怎么变 Response?这些问题,下一章 P5-18 会用真实源码 + 反例彻底拆开。axum 的"错误全转 Response"模型,是它在框架层做的最重要的一件事——它让 hyper-util 永远不会拿到一个 `Err`,所有错误(404/405/415/500/panic)都变成可发的 `Response`。

---

> **本章源码锚点(全部经本地 `../axum/` Grep/Read 核实,axum-v0.8.9 @ c59208c86fded335cd85e388030ad59347b0e5ae,axum 0.8.9 / axum-core 0.5.5 / axum-macros 0.5.1 / matchit 0.8.4)**:
>
> - [serve 函数签名 + Serve 结构(L97-L118)](../axum/axum/src/serve/mod.rs#L97-L118) —— `serve<L, M, S>(listener, make_service)`,三泛型 `L: Listener` / `M: MakeService` / `S: Service`。
> - [Serve::with_graceful_shutdown(L151-L161)](../axum/axum/src/serve/mod.rs#L151-L161) —— 入口,返回 `WithGracefulShutdown<L, M, S, F>`。
> - [Serve::run accept 循环(L179-L193)](../axum/axum/src/serve/mod.rs#L179-L193) —— `loop { accept; handle_connection }`,无 graceful 版本,返回 `!`(永不完成)。
> - [WithGracefulShutdown::run 编排(L267-L305)](../axum/axum/src/serve/mod.rs#L267-L305) —— 两个 watch channel(signal/close),`select!` 让 accept 和 signal 抢跑,`close_tx.closed().await` 等所有 task 结束。
> - [handle_connection 全景(L353-L416)](../axum/axum/src/serve/mod.rs#L353-L416) —— 六步:`TokioIo::new` → `make_service.ready` → `make_service.call(IncomingStream)` → `map_request(Body::new)` → `TowerToHyperService::new` → `tokio::spawn` 跑 `serve_connection` + 内层 `select!`。
> - [IncomingStream 结构(L424-L445)](../axum/axum/src/serve/mod.rs#L424-L445) —— MakeService 的输入,持 `&'a TokioIo<L::Io>` + `remote_addr: L::Addr`。
> - [Listener trait 定义(L9-L24)](../axum/axum/src/serve/listener.rs#L9-L24) —— 关联类型 `Io: AsyncRead+AsyncWrite+Unpin+Send+'static` / `Addr: Send`,`accept -> (Io, Addr)` 不返回 Result。
> - [TcpListener impl Listener(L26-L43)](../axum/axum/src/serve/listener.rs#L26-L43) —— `loop { accept; handle_accept_error }` 模板。
> - [UnixListener impl Listener(L45-L63)](../axum/axum/src/serve/listener.rs#L45-L63) —— `#[cfg(unix)]`,同模板。
> - [ListenerExt::tap_io + TapIo(L66-L138)](../axum/axum/src/serve/listener.rs#L66-L138) —— 装饰器,accept 后跑 `tap_fn(&mut io)`,给设 `TCP_NODELAY` 用。
> - [handle_accept_error(L140-L158)](../axum/axum/src/serve/listener.rs#L140-L158) —— 区分连接级错误(直接重试)/ 系统级错误(`EMFILE` 等,睡 1 秒重试),逻辑继承自 hyper 0.14。
> - [IntoMakeServiceWithConnectInfo 结构(connect_info.rs L28-L46)](../axum/axum/src/extract/connect_info.rs#L28-L46) —— `svc: S` + `PhantomData<fn() -> C>`。
> - [Connected trait(connect_info.rs L80-L83)](../axum/axum/src/extract/connect_info.rs#L80-L83) —— `fn connect_info(stream: T) -> Self`,可扩展点。
> - [SocketAddr: Connected&lt;IncomingStream&lt;TcpListener&gt;&gt;(connect_info.rs L90-L94)](../axum/axum/src/extract/connect_info.rs#L90-L94) —— `*stream.remote_addr()`。
> - [IntoMakeServiceWithConnectInfo: Service&lt;T&gt; impl(connect_info.rs L114-L133)](../axum/axum/src/extract/connect_info.rs#L114-L133) —— `call` 多干两件事:`C::connect_info(target)` + `Extension(ConnectInfo).layer(svc.clone())`。
> - [ConnectInfo&lt;T&gt; newtype + FromRequestParts(connect_info.rs L150-L169)](../axum/axum/src/extract/connect_info.rs#L150-L169) —— 提取器,从 extensions 拿 ConnectInfo(必须用 `into_make_service_with_connect_info` 启动)。
> - [MockConnectInfo(connect_info.rs L220-L232)](../axum/axum/src/extract/connect_info.rs#L220-L232) —— 测试用 mock Layer。
> - [custom connect_info 测试(connect_info.rs L269-L309)](../axum/axum/src/extract/connect_info.rs#L269-L309) —— 演示 `Connected` 可扩展点(自定义 `MyConnectInfo`)。
> - [serving_on_custom_io_type 测试(serve/mod.rs L684-L727)](../axum/axum/src/serve/mod.rs#L684-L727) —— `Listener` 抽象的广告,用 `duplex` 假 IO 跑通整个 axum。
> - [Router::into_make_service / into_make_service_with_connect_info(routing/mod.rs L527-L541)](../axum/axum/src/routing/mod.rs#L527-L541) —— 显式 MakeService 入口,内部都先 `with_state(())`。
> - [Router&lt;()&gt; impl Service&lt;IncomingStream&gt;(routing/mod.rs L549-L566)](../axum/axum/src/routing/mod.rs#L549-L566) —— 便利写法的根,`call` 是 `self.clone().with_state(())`。
>
> **承接**:hyper-util 的 accept/连接模型/HTTP/1+2 auto 协商/`serve_connection`/`graceful_shutdown` 承《hyper》P4-P5(一句带过);`tower_service::Service` trait 模型 + `tower::ServiceExt::ready` + `Extension` Layer + `TowerToHyperService` 适配承《hyper》P1-02 + 《Tower》P0-01(一句带过);Tokio 的 `watch`/`mpsc` channel + `AsyncRead`/`AsyncWrite`/`poll_read` + `tokio::spawn` task 调度 + `tokio::time::sleep` 承《Tokio》(一句带过指路 [[tokio-source-facts]]);跨语言对照 go net/http(`http.Server.Serve` + `Shutdown(ctx)` + `net.Listener` interface)、actix-web(`HttpServer` + worker 模型 + `bind`/`bind_uds`)、hyper 原生(`serve_connection` 单连接版)。
>
> **修正的源码印象**:本章核实过程中确认几处容易记错的细节:① `Serve::run`(无 graceful 版本)返回类型是 `-> !`(never type,永不完成),不是 `io::Result<()>`(`IntoFuture` 包装后才显示 `io::Result<()>`,但永远到不了);② `Listener::accept` 用 RPITIT(`impl Future<...> + Send`),不是 `Pin<Box<dyn Future<...> + Send>>`(零开销);③ `handle_accept_error` 区分"连接级错误(`ConnectionRefused`/`ConnectionAborted`/`ConnectionReset`)直接重试"和"其他错误睡 1 秒",不是所有错误都睡 1 秒;④ `into_make_service_with_connect_info` 内部也调 `with_state(())`(和 `into_make_service` 一样,把 handler 预先转 Route);⑤ `ConnectInfo` 提取器要**必须**用 `into_make_service_with_connect_info` 启动,否则运行时 rejection(不是编译错)。本书正文以源码真值为准。
