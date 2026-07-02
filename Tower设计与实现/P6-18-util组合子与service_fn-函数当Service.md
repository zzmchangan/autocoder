# 第 18 章 · util 组合子与 service_fn:函数当 Service

> 第 6 篇 · 工程化:类型擦除与集成 · 组合章

---

## 核心问题

你已经把 `Service` trait(P1-02)、`Layer` 洋葱(P1-03)、`ServiceBuilder`/`ServiceExt`(P1-04)、`BoxService` 类型擦除(P6-17)读完了。可真要落手写一个最普通的业务 handler,你会撞到一个朴素的尴尬:为了把"读一个 user 出来,顺便记一条日志"写成 `Service`,你难道要专门定义一个 struct,再写一段 `impl Service<Request> for MyHandler`,把 `poll_ready`/`call`/`Future` 三件套全抄一遍?一段三行业务逻辑,要几十行 struct + impl 托底。更糟的是,大部分 handler 根本不持有资源、不需要背压——`poll_ready` 永远返回 `Ready(Ok)`,`call` 就是把请求丢进一个 `async fn`。为这种"无状态、纯转发"的逻辑套整套 Service trait 样板,值吗?

这一章要钉死的就是这个问题。具体地说:

1. `service_fn` 凭什么把一个 `FnMut(Request) -> Future` 的闭包/`async fn` 直接包成一个 `Service`,中间不用写一个 struct?
2. `service_fn` 怎么绕过 P1-02 讲得那么重的 `poll_ready` 背压——它的 `poll_ready` 到底是什么?
3. `map_request`/`map_response`/`map_err`/`map_result`/`map_future`/`and_then`/`then` 这七个 Service 版的 Iterator 组合子,凭什么能像 `.map()`/`.filter()` 一样把 Service 链起来?它们的 `poll_ready` 怎么处理(为什么有的组合子闭包要 `Clone`、有的只要 `FnMut`)?
4. `Either`/`Optional`/`call_all`/`future_service` 各自在什么场景下救命——`Either` 怎么把两个不同 Service 类型擦成一个、`Optional` 怎么优雅处理"内层 Service 还没构造出来"、`call_all` 怎么用一个 Stream 喂 Service、`future_service` 怎么把"异步构造 Service"这件事本身当 Service?

读完本章你会明白:

1. **`service_fn` 是 Rust 异步生态"闭包当 trait object"惯用法的 Service 版**:它把"业务逻辑写成 async fn"和"Service trait 的 `poll_ready`/`call` 契约"之间隔的那层 struct 样板,用一个泛型 `ServiceFn<T>` 垫平。代价是这个 Service 没有背压(永远 ready),收益是写起来像写普通 async fn。
2. **`service_fn` 的 `poll_ready` 永远返回 `Poll::Ready(Ok(()))`**,这是它"函数当 Service"的根本:函数不持有资源、不预留 permit,自然永远 ready。这一节会反过来印证 P1-02 讲的"poll_ready 是资源预留"——没有资源要预留的 Service,poll_ready 退化为永远的 Ready。
3. **map_* / and_then / then 七个组合子的 `poll_ready` 都是"原样转发给内层"**,它们不引入自己的资源约束,只改数据流。但闭包 trait 约束分两类:作用在 `call` 输出上的(`map_response`/`map_err`/`map_result`/`and_then`/`then`)用 `FnOnce + Clone`,因为 `Layer::layer(&self)` 要 clone 闭包;作用在 `call` 入口或 Future 本身的(`map_request`/`map_future`)用 `FnMut`,因为它们直接吃 `&mut self`,不需要 clone。这个差别是 Service 组合子区别于 Iterator 组合子的关键。
4. **`Either`/`Optional`/`call_all`/`future_service` 是四个救场角色**:`Either` 用 enum 把两个 Service 类型捏成一个(编译期分发,对照 trait object 运行期分发)、`Optional` 给"可能还没构造好的 Service"一个安全占位、`call_all` 把单请求 Service 变成吃 Stream 的处理器、`future_service` 把"异步构造 Service"当 Service 用(在 `poll_ready` 里驱动那个构造 Future)。

> **逃生阀**:本章预设你熟 P1-02(`Service` 的 `&mut self`/`poll_ready` 背压/`mem::replace` 惯用法)、P1-04(`ServiceExt` blanket impl/`MapResponse` 转发 poll_ready)。如果你只是想知道"怎么少写 struct",直接读"一句话点破"和第 1 节 `service_fn`;如果你想搞清组合子闭包的 `Clone`/`FnMut` 区别,读第 3 节;`Either`/`Optional`/`call_all` 是救场,可按需读。本章无招牌章那种深度门槛,但闭包 trait(`Fn`/`FnMut`/`FnOnce`)的细节是关键,不熟 Rust 闭包 trait 的建议先补。

---

## 一句话点破

> **`service_fn` 用一个泛型 struct `ServiceFn<T>` 把 `T: FnMut(Request) -> Future` 包起来,给 `poll_ready` 写死一句 `Poll::Ready(Ok(()))`(函数不持有资源,永远 ready),给 `call` 写一句 `(self.f)(req)`(直接调闭包)。就这两句,任何 `async fn` 都变成了 `Service`,你再也不用为每个三行 handler 写一个 struct。剩下的 `map_*`/`and_then`/`then` 七个组合子是 Service 版的 Iterator 组合子:`poll_ready` 一律转发给内层(背压透传,不破坏 P1-02 的语义),`call` 里用 `f` 把数据流改一下。`Either`/`Optional`/`call_all`/`future_service` 是四个救场:类型二选一、可能为空的占位、Stream 喂 Service、异步构造 Service。util 模块的全部哲学,就是"让 Service 像闭包和 Iterator 一样好写,又不丢 Service 的背压契约"。**

这是结论。本章倒过来拆:先看"为每个 handler 写 struct"撞什么墙,再看 Rust 标准 Iterator/闭包范式怎么解决"轻量组合"的(承 Tokio 一句带过),然后讲 Tower 为什么必须发明 service_fn + map_*/and_then/Either,最后用源码钉死每个闭包 trait 约束的精妙——尤其 `FnMut` vs `FnOnce + Clone` 的取舍,这是 Service 组合子最容易写错的地方。

---

## 正文

### 第一节:为每个 handler 写一个 struct,样板到让人想放弃

#### 1.1 提出问题:三行业务逻辑,几十行 struct 托底

设想你要写一个最朴素的 handler:接收一个 `GetUser` 请求,查个数据库,返回 `User`。用 P1-02 学的 Service trait,最朴素写法是这样:

```rust
// 朴素版:每个 handler 一个 struct + 一段 impl(简化示意)
struct GetUserHandler {
    db: Arc<DbPool>,
}

impl Service<GetUser> for GetUserHandler {
    type Response = User;
    type Error = AppError;
    type Future = Pin<Box<dyn Future<Output = Result<User, AppError>> + Send>>;

    fn poll_ready(&mut self, _cx: &mut Context<'_>) -> Poll<Result<(), AppError>> {
        Poll::Ready(Ok(()))   // handler 不持有可耗尽资源,永远 ready
    }

    fn call(&mut self, req: GetUser) -> Self::Future {
        let db = self.db.clone();
        Box::pin(async move {
            let user = db.find_user(req.id).await?;
            Ok(user)
        })
    }
}
```

三行业务逻辑(`db.find_user(req.id).await?`),套了 18 行 struct + impl 样板。而且样板里有几个"永远一样"的部分:

- `poll_ready` 永远返回 `Poll::Ready(Ok(()))`——因为这个 handler 不持有可耗尽资源(数据库连接池是 `Arc<DbPool>` 共享的,不在这里 acquire permit);
- `call` 内部就是把请求丢进一个 `async move` 块,返回一个 boxed Future;
- `type Future` 几乎永远是 `Pin<Box<dyn Future<Output = Result<...>> + Send>>`。

每写一个 handler,这三段都得重抄一遍。一个 axum 应用,handler 少说几十个,多则上百个,全是这种样板——业务逻辑淹没在 trait 实现里。

更糟的是,很多 handler 还要被 Layer 装饰(套 timeout/retry/限流),你想在装饰链上插一个"小逻辑"(比如把请求里的 user_id 字符串解析成 u64 再传给内层),又得写一个 struct:

```rust
// 又一个 struct,只为了在请求进内层前改一下类型
struct ParseUserId<S> { inner: S }

impl<S> Service<String> for ParseUserId<S>
where S: Service<u64>
{
    type Response = S::Response;
    type Error = S::Error;
    type Future = S::Future;

    fn poll_ready(&mut self, cx: &mut Context<'_>) -> Poll<Result<(), S::Error>> {
        self.inner.poll_ready(cx)   // 转发
    }

    fn call(&mut self, req: String) -> Self::Future {
        self.inner.call(req.parse().unwrap())   // 改请求,转发
    }
}
```

又是 18 行,真正的逻辑只有 `req.parse().unwrap()` 一行。这种"小转换器"在中间件链里到处都是,每个都开一个 struct 命名(还得想个不重复的名字),开发体验塌方。

> **不这样会怎样**:样板代码的代价不是"行数多",而是"业务逻辑被噪声淹没"。新人读一个 handler,要先翻过 18 行 struct/impl,才能找到那 3 行真正干活的代码;改一处业务逻辑,要在噪声里定位;code review 时,真正该审的业务逻辑被样板盖住,reviewer 容易跳过。一个中间件库如果逼用户每写一段逻辑都要开 struct,没人会用第二遍。Rust 的 Iterator 生态之所以用得开,就是因为 `.map(|x| x + 1)` 写起来像写普通函数——Tower 必须给 Service 同样的体验。

#### 1.2 承接《Tokio》:Iterator/Future 组合子,轻量组合的范本

> **承接《Tokio》[[tokio-source-facts]]**:本节只回顾"组合子 + 单态化 + 闭包当一等公民"这个 Rust 惯用范本,Tokio 的 Future 组合子(`map`/`then`)内部机制、《Tokio》已拆透,一句带过指路。篇幅全留 Tower 独有。

标准库的 `Iterator` 给所有迭代器免费发了 `.map(f)`/`.filter(p)`/`.take(n)`,每个组合子吃一个**闭包**(`Fn`/`FnMut`/`FnOnce`),返回一个新迭代器。这套设计能成立,根因是 Rust 的闭包是**一等公民**——闭包有自己唯一的类型(编译器生成的匿名 `FnOnce`/`FnMut`/`Fn` 实现),能当泛型参数传(`impl Iterator<Item = T>`),能被单态化进具体类型。

```rust
let v = vec![1, 2, 3];
let it = v.iter()
    .map(|x| x + 1)       // 闭包当参数,类型是 Map<Iter, closure>
    .filter(|x| *x > 1);  // 又一个闭包,类型是 Filter<Map<...>, closure>
```

这套设计三个精妙之处(P1-04 已讲透,这里只列):① 每个组合子返回新类型,类型嵌套表达处理链;② 链式调用,可读;③ 泛型单态化,零运行期开销。

Tokio/标准库的 Future 组合子(`future.map(f)`/`future.and_then(g)`)、《hyper》讲过的 `Future` trait,都是同一套思路。Tower 要做的,就是把这套"链式 + 闭包 + 单态化"范式,从 Iterator/Future 搬到 Service 上。但 Service 有 Iterator/Future 没有的两个难点:

1. **Service 是带状态的(`&mut self`、`poll_ready` 背压)**——Iterator 的 `.map()` 只调一次闭包(消费迭代器),Future 的 `.map()` 也只调一次;Service 的 `.map_response()` 要在多次 `call` 里复用,所以闭包要么 `Clone`,要么用 `&mut self`。
2. **Service 有 `poll_ready` 这个前置钩子**——Iterator/Future 没有这一步,组合子只要处理"数据怎么流";Service 组合子要同时处理"就绪状态怎么传染 + 数据怎么流"两件事。

所以 Tower 不能直接照抄 `.map()` 那一招,它得发明两套东西:**`service_fn` 把闭包包成 Service**(解决"不想写 struct"),**map_*/and_then/Either 把 Service 装饰成新 Service**(解决"想轻量组合")。两套东西的闭包 trait 约束还不一样——这就是本章技巧密度最高的地方。下面分别拆。

### 第二节:service_fn——把闭包/async fn 包成 Service

#### 2.1 真身就这几行

`service_fn` 的全部源码,在 `tower/src/util/service_fn.rs`,加上 struct 定义和 impl,一共 82 行。先看核心三段:

```rust
// tower/src/util/service_fn.rs#L46-L48
pub fn service_fn<T>(f: T) -> ServiceFn<T> {
    ServiceFn { f }
}

// tower/src/util/service_fn.rs#L53-L56
#[derive(Copy, Clone)]
pub struct ServiceFn<T> {
    f: T,
}
```

`service_fn(f)` 把闭包 `f` 包进一个 `ServiceFn<T>`——就这么一句。`ServiceFn<T>` 是个泛型 struct,只有一个字段 `f: T`,`T` 是闭包的类型。`#[derive(Copy, Clone)]` 让 `ServiceFn` 自动满足 `Clone`(以及 `Copy`),只要 `T` 满足——这很重要,后面会讲为什么 `ServiceFn` 要 `Clone`(被 Layer 装饰、被 `Buffer` 包装都需要)。

然后是整章最核心的一段 impl:

```rust
// tower/src/util/service_fn.rs#L66-L82
impl<T, F, Request, R, E> Service<Request> for ServiceFn<T>
where
    T: FnMut(Request) -> F,
    F: Future<Output = Result<R, E>>,
{
    type Response = R;
    type Error = E;
    type Future = F;

    fn poll_ready(&mut self, _: &mut Context<'_>) -> Poll<Result<(), E>> {
        Ok(()).into()          // ← 永远 Ready(Ok),函数不持有资源
    }

    fn call(&mut self, req: Request) -> Self::Future {
        (self.f)(req)           // ← 直接调闭包
    }
}
```

四件事钉死:

**第一,泛型约束 `T: FnMut(Request) -> F, F: Future<Output = Result<R, E>>`**。这里 `T` 是闭包/`async fn` 的类型,它必须能被 `&mut self` 调用(`FnMut`),吃一个 `Request`,返回一个 `Future`。注意是 **`FnMut` 不是 `Fn`**——这个差别后面技巧精解会展开,先记住"`FnMut` 允许闭包有可变捕获的状态"。

**第二,关联类型直接把闭包的输出类型透传出去**。`type Response = R`、`type Error = E`、`type Future = F`——`ServiceFn` 的 Service 关联类型,完全由闭包 `T` 的签名决定。你传 `|req: GetUser| async move { Ok(User { ... }) }`,那 `R = User`、`E = Infallible`(或你声明的错误类型)、`F` 就是那个 `async move` 块生成的 Future 类型。

**第三,`poll_ready` 永远返回 `Poll::Ready(Ok(()))`**。`Ok(()).into()` 把 `Result<(), E>` 转成 `Poll<Result<(), E>>` 的 `Ready` 变体。这是 `service_fn` 最关键的设计决定:**函数不持有资源,所以永远 ready**。这一句反过来印证了 P1-02 讲的"`poll_ready` 是资源预留"——没有资源要预留的 Service,poll_ready 退化为永远的 Ready。`service_fn` 包出来的 Service 没有背压,因为它压根没有可耗尽的资源。

**第四,`call` 就一句 `(self.f)(req)`**。直接调闭包,把请求丢进去,拿到闭包返回的 Future(`F`),原样返回。`type Future = F` 这个关联类型让 `call` 不需要把 Future 装箱——`F` 就是闭包返回的具体 Future 类型,编译期已知,单态化后零开销。

#### 2.2 怎么用:三行写一个 Service

有了 `service_fn`,第 1.1 节那个 `GetUserHandler` 就可以重写成:

```rust
use tower::service_fn;

// 业务逻辑直接写成 async fn
async fn get_user(req: GetUser, db: Arc<DbPool>) -> Result<User, AppError> {
    Ok(db.find_user(req.id).await?)
}

// 包成 Service
fn make_get_user_service(db: Arc<DbPool>) -> impl Service<GetUser, Response = User, Error = AppError> {
    service_fn(move |req: GetUser| {
        let db = db.clone();
        async move { get_user(req, db).await }
    })
}
```

三行业务逻辑(`get_user` 函数体),加上一句 `service_fn(...)` 包装,就成了一个 `Service`。没有 struct 定义,没有 `impl Service for ...`,没有 `poll_ready` 样板。`service_fn` 把所有样板都吃掉了。

`service_fn` 的 doctest(源码第 12-45 行)给了一个更简洁的例子:

```rust
// tower/src/util/service_fn.rs#L28-L41(doctest 简化)
async fn handle(request: Request) -> Result<Response, BoxError> {
    let response = Response::new("Hello, World!");
    Ok(response)
}

let mut service = service_fn(handle);

let response = service
    .ready()              // ← service_fn 的 poll_ready 永远 Ready,这一步立即返回
    .await?
    .call(Request::new())
    .await?;
```

注意 `.ready().await?` 这一步——对 `service_fn` 包出来的 Service,这一步**立即返回**(因为 poll_ready 永远 Ready)。但对一个普通的 Service(比如 `ConcurrencyLimit`),`.ready().await` 可能要等 permit(P3-09)。`service_fn` 包出来的 Service 跳过了这一步的等待,但调用方代码长得一模一样——这就是 `service_fn` 的妙处:**调用方不用关心你这个 Service 是不是 service_fn 包的,反正它实现了 Service trait,该 `.ready().await` 就 `.ready().await`**。

#### 2.3 `service_fn` 凭什么绕过 poll_ready 背压

这是 `service_fn` 最值得拆透的点。P1-02 花了整整一章讲 `poll_ready` 是背压的核心、是资源预留、是 Service 区别于 `async fn` 的根本。可 `service_fn` 的 `poll_ready` 直接返回 `Ready(Ok)`,看起来完全无视背压。这矛盾吗?

不矛盾,理由有三:

**理由一:`service_fn` 表达的就是"无状态、无资源"的 Service**。`service_fn` 包的闭包,典型是 `|req| async move { ... }` 这种——闭包捕获的是 `Arc<DbPool>`/`Arc<HttpClient>` 这种**共享、可重入、不会耗尽**的句柄(连接池内部自己管并发,不靠 Service 层 poll_ready)。这种 Service 没有"可耗尽的资源",自然没有背压要表达。`poll_ready` 返回 Ready,是诚实的——它确实永远 ready。

**理由二:背压责任下推到内层**。`service_fn` 包的 async 块内部,如果真的调用了一个会背压的东西(比如内层 `db.find_user()` 内部限并发),那个背压是 db 层、Future 层的事,不是 Service 层的事。Future 层的 await 会自然阻塞(异步等待),不需要 Service 层的 `poll_ready` 介入。`service_fn` 把"就绪"这件事简化到"Future 跑起来就完事",适合那些不需要 Service 层精细背压的场景。

**理由三:需要背压的 Service 不该用 `service_fn` 写**。如果你写的 Service 真的需要 poll_ready 预留资源(比如要 acquire 一个 permit、要等一个连接槽),那你应该手写 struct + impl Service,在 `poll_ready` 里正确实现资源预留状态机(P1-02 技巧精解讲的两态状态机)。`service_fn` 不是银弹,它专给"纯转发、无资源"的 Service 用。Tower 生态里,真正的中间件(Timeout/Retry/Buffer/ConcurrencyLimit)全是手写 struct + impl,没用 `service_fn`——因为它们都需要 poll_ready 做事。`service_fn` 是业务 handler 的快捷方式,不是中间件的快捷方式。

> **钉死这件事**:`service_fn` 的 `poll_ready` 永远 `Ready(Ok)`,不是"忘了写背压",是"诚实地表达这个 Service 没有资源要预留"。P1-02 讲的 `poll_ready` 背压契约,在 `service_fn` 这里退化但不被违反——背压还在(P1-02 钉的契约是"`poll_ready` 返回 `Ready(Ok)` 后,`call` 之前重复 `poll_ready` 必须继续返回 `Ready(Ok)`",`service_fn` 满足这条,因为它永远 Ready)。`service_fn` 适合"无状态 handler",不适合"持资源中间件",这是它的设计边界。

#### 2.4 `service_fn` 与 axum/tonic 的关系

`service_fn` 看起来是个小工具,但它在整个 Rust 异步生态里是**"业务代码到 Service 的标准桥梁"**。axum 的 `from_fn`(把一个 async fn 包成 Tower Service)、tonic 的 interceptor、reqwest 的中间件,底层都是 `service_fn` 这一招——把闭包/async fn 包成 Service,塞进 Tower 中间件链。

具体到 axum:`axum::handler::Handler` trait 是 axum 自己定义的,但它通过 `into_service()` 方法(在 `axum::handler` 模块)把一个 Handler 转成 Tower Service,底层就是 `service_fn`。tonic 的 `RequestStream` 处理、reqwest 的 `Middleware`(reqwest-middleware crate),同样依赖把闭包包成 Service 这一招。这一章讲的 `service_fn`,是 P6-19"四框架集成"的底层砖块——没有 `service_fn`,axum/tonic 没法把用户的 async fn handler 接进 Tower 中间件栈。P6-19 会展开,这里先记住"`service_fn` 是业务到 Service 的标准入口"。

### 第三节:map_*/and_then/then——Service 版 Iterator 组合子

`service_fn` 解决了"把闭包当 Service"。但很多时候你不是要造一个新 Service,而是已经有一个 Service,想给它加一点小逻辑——改改请求类型、改改响应、出错时把错误转一下、成功后再链一个异步计算。这些就是 `map_*`/`and_then`/`then` 七个组合子的事。它们是 Service 版的 Iterator 组合子,P1-04 已经预告过(`ServiceExt` blanket impl + `MapResponse` 转发 poll_ready)。这一节把七个组合子的源码和闭包约束钉死。

#### 3.1 七个组合子的全景

先把七个组合子摆一张表(基于 P1-04 的对照表细化,加入源码核实的闭包约束):

| 组合子 | 作用面 | 闭包 trait(源码核实) | 改变什么 | 典型场景 |
|---|---|---|---|---|
| `map_request(f)` | 请求(入口) | `F: FnMut(R1) -> R2` | 改 Request 类型 | 把 `String` 解析成 `u64` 再传给内层 |
| `map_response(f)` | 响应(成功) | `F: FnOnce(S::Response) -> Response + Clone` | 改 Response 类型 | 给响应加一个字段、转成另一种 DTO |
| `map_err(f)` | 错误(失败) | `F: FnOnce(S::Error) -> Error + Clone` | 改 Error 类型 | 把内层错误包成应用层 AppError |
| `map_result(f)` | 整个 Result | `F: FnOnce(Result<S::Response, S::Error>) -> Result<Response, Error> + Clone` | 可同时改 Response 和 Error | 把某种错误恢复成成功值 |
| `map_future(f)` | Future 本身 | `F: FnMut(S::Future) -> Fut` | 把内层 Future 包成新 Future | 给 Future 套一个 timeout/取消逻辑 |
| `and_then(f)` | 响应(成功,异步) | `F: FnOnce(S::Response) -> Fut + Clone, Fut: TryFuture` | 改 Response,链一个异步 | 成功后查另一个服务 |
| `then(f)` | 整个 Result(异步) | `F: FnOnce(Result<S::Response, S::Error>) -> Fut + Clone, Fut: Future` | 可同时改 Response 和 Error,链异步 | 失败时异步恢复 |

这张表里最值得钉死的是**闭包 trait 那一列**。它分两类,源码核实的差别是:

- **`map_request` 和 `map_future` 用 `FnMut`**(不需要 `Clone`)。
- **`map_response`/`map_err`/`map_result`/`and_then`/`then` 用 `FnOnce + Clone`**。

这个差别不是随便选的,它根植于 Service 的 `&mut self` 语义和 Layer 的 `&self` 语义。下面分别拆。

#### 3.2 `map_response`:作用在 `call` 输出,闭包要 `FnOnce + Clone`

先看 `map_response` 的 impl,这是七个里最典型的"作用在响应面"组合子:

```rust
// tower/src/util/map_response.rs#L59-L77
impl<S, F, Request, Response> Service<Request> for MapResponse<S, F>
where
    S: Service<Request>,
    F: FnOnce(S::Response) -> Response + Clone,
{
    type Response = Response;
    type Error = S::Error;
    type Future = MapResponseFuture<S::Future, F>;

    #[inline]
    fn poll_ready(&mut self, cx: &mut Context<'_>) -> Poll<Result<(), Self::Error>> {
        self.inner.poll_ready(cx)              // ← 背压原样转发
    }

    #[inline]
    fn call(&mut self, request: Request) -> Self::Future {
        MapResponseFuture::new(self.inner.call(request).map_ok(self.f.clone()))
    }
}
```

三件事:

**第一,`poll_ready` 直接转给 `self.inner`**。`MapResponse` 不引入自己的就绪条件,内层 ready 它就 ready。背压原样透传,不破坏 P1-02 的语义。这是所有 map_* 组合子的共同模式(下面会看到 `map_err` 是唯一例外,它在 poll_ready 的错误上也 apply `f`)。

**第二,`call` 把内层返回的 Future 用 `.map_ok(self.f.clone())` 包一层**。`map_ok` 是 `futures_util::TryFutureExt` 的方法,它把一个 `Future<Output = Result<T, E>>` 变成 `Future<Output = Result<U, E>>`,在内层成功时 apply `f` 把 `T` 转成 `U`。`f.clone()` 是因为 `map_ok` 要消费 `f`(它 move 进返回的 MapOk Future),但 `call(&mut self)` 可能被调多次,每次都要一份 `f`——所以必须 clone。

**第三,`F: FnOnce + Clone`**。为什么是 `FnOnce`?因为 `f` 被消费(move 进 MapOk Future)后调用,只调一次,这是 `FnOnce` 的语义。为什么又要 `Clone`?因为 `call` 可能被调多次(同一个 Service 发多个请求),每次都要一份新的 `f`。所以约束是 `FnOnce(...) -> Response + Clone`——既能被消费调用,又能被复制复用。

> **钉死这件事**:`map_response` 的 `F: FnOnce + Clone` 这个组合约束,根因是 Service 的 `call(&mut self)` 可能被调多次。Iterator 的 `.map(f)` 只调一次闭包(消费迭代器),所以 `FnMut` 就够;Service 的 `.map_response(f)` 要在多次 call 里复用,所以 `f` 必须能 clone。这是 Service 组合子区别于 Iterator 组合子的核心差别。P1-04 讲过这个点,这里用源码钉死。

#### 3.3 `map_err`:在 `poll_ready` 和 `call` 两个地方都 apply `f`

`map_err` 是七个组合子里唯一一个**在 `poll_ready` 也 apply `f`** 的,这一点 P1-04 没展开,这里钉死:

```rust
// tower/src/util/map_err.rs#L59-L77
impl<S, F, Request, Error> Service<Request> for MapErr<S, F>
where
    S: Service<Request>,
    F: FnOnce(S::Error) -> Error + Clone,
{
    type Response = S::Response;
    type Error = Error;
    type Future = MapErrFuture<S::Future, F>;

    #[inline]
    fn poll_ready(&mut self, cx: &mut Context<'_>) -> Poll<Result<(), Self::Error>> {
        self.inner.poll_ready(cx).map_err(self.f.clone())   // ← 这里也 apply f!
    }

    #[inline]
    fn call(&mut self, request: Request) -> Self::Future {
        MapErrFuture::new(self.inner.call(request).map_err(self.f.clone()))
    }
}
```

注意 `poll_ready` 那一行:`self.inner.poll_ready(cx).map_err(self.f.clone())`。`Poll<Result<(), S::Error>>` 被 `map_err` 转成 `Poll<Result<(), Error>>`——也就是说,如果内层 `poll_ready` 返回 `Ready(Err(e))`(P1-02 讲的"服务死透"那一态),这个错误也会被 `f` 转换。

为什么 `map_err` 要在 `poll_ready` 也 apply `f`,而 `map_response` 不用?因为错误有两个来源:**`poll_ready` 的错误**(服务结构性失败)和**`call` 返回的 Future 的错误**(请求级失败)。`map_err` 的语义是"把内层的所有错误都转换一下",所以两个来源都要 cover。`map_response` 只管成功响应,而 `poll_ready` 不产生 Response(它只产生 `Result<(), Error>`),所以 `map_response` 不需要在 poll_ready 介入。

这个细节实际写代码时容易踩坑:你以为 `map_err(f)` 只转换 `call` Future 的错误,结果发现 `poll_ready` 报的错也走了 `f`。如果你 `f` 里假设错误一定来自请求处理(比如假设有 `req` 上下文),在 `poll_ready` 错误上调用就会出问题。源码核实地告诉你:`map_err` 两个来源都 cover,写 `f` 时要假设它可能收到任何内层错误(包括 poll_ready 的"服务死透"错误)。

#### 3.4 `map_request` 和 `map_future`:为什么用 `FnMut` 而不是 `FnOnce + Clone`

这是本章技巧密度最高的差别。`map_request` 和 `map_future` 的闭包约束是 `FnMut`,不是 `FnOnce + Clone`。看源码:

```rust
// tower/src/util/map_request.rs#L43-L61
impl<S, F, R1, R2> Service<R1> for MapRequest<S, F>
where
    S: Service<R2>,
    F: FnMut(R1) -> R2,
{
    type Response = S::Response;
    type Error = S::Error;
    type Future = S::Future;

    #[inline]
    fn poll_ready(&mut self, cx: &mut Context<'_>) -> Poll<Result<(), S::Error>> {
        self.inner.poll_ready(cx)
    }

    #[inline]
    fn call(&mut self, request: R1) -> S::Future {
        self.inner.call((self.f)(request))   // ← &mut self 调 f,不 clone
    }
}
```

```rust
// tower/src/util/map_future.rs#L49-L67
impl<R, S, F, T, E, Fut> Service<R> for MapFuture<S, F>
where
    S: Service<R>,
    F: FnMut(S::Future) -> Fut,
    E: From<S::Error>,
    Fut: Future<Output = Result<T, E>>,
{
    type Response = T;
    type Error = E;
    type Future = Fut;

    fn poll_ready(&mut self, cx: &mut Context<'_>) -> Poll<Result<(), Self::Error>> {
        self.inner.poll_ready(cx).map_err(From::from)
    }

    fn call(&mut self, req: R) -> Self::Future {
        (self.f)(self.inner.call(req))   // ← &mut self 调 f,不 clone
    }
}
```

关键差别:`map_request` 和 `map_future` 的 `call` 里,`f` 是用 `(self.f)(...)` 调的——**直接用 `&mut self.f`,不 clone**。这是因为 `map_request`/`map_future` 的 `f` 作用在"请求还在 Service 手里、Future 还没出去"的时刻,`f` 可以反复借用(`&mut self.f`),不需要 move 进返回的 Future。

对比一下 `map_response`:`map_response` 的 `f` 作用在"内层 Future 成功之后",而内层 Future 是要被 await 的(可能在另一个 task 上 poll),所以 `f` 必须 move 进返回的 MapOk Future——这就要 clone。`map_request` 不一样:它的 `f` 作用在请求入口,转换后的请求立刻 `self.inner.call(...)` 转发出去,`f` 用完就完,不需要 move 进 Future。所以 `FnMut` 就够,不需要 `Clone`。

这个差别用一张图钉死:

```mermaid
flowchart LR
    Req["Request 进来"] --> MR["map_request.f<br/>FnMut, &mut self 调<br/>(入口同步转换)"]
    MR --> Inner["inner.call 转发"]
    Inner --> Fut["inner 返回 Future"]
    Fut --> MF["map_future.f<br/>FnMut, &mut self 调<br/>(包 Future 同步)"]
    MF --> Fut2["包装后的 Future"]
    Fut2 --> Await["await (可能跨 task)"]
    Await --> Resp["Response"]
    Resp --> MR2["map_response.f<br/>FnOnce + Clone<br/>(成功后异步转换)<br/>f 必须 move 进 Future → 要 Clone"]
    MR2 --> Out["最终 Response"]

    classDef mut fill:#dbeafe,stroke:#2563eb
    classDef clone fill:#dcfce7,stroke:#16a34a
    class MR,MF mut
    class MR2 clone
```

口诀:**"作用在 Future 出去之前(入口同步)的,用 FnMut;作用在 Future 出去之后(异步 await 后)的,用 FnOnce + Clone"**。

为什么 `map_future` 也是 `FnMut`?因为 `map_future` 的 `f` 虽然作用在 Future 上,但它是"同步地把 inner Future 转成新 Future"——`(self.f)(self.inner.call(req))` 这一句,`f` 拿到 inner Future,返回一个新 Future,`f` 本身用完就完(没 move 进返回值),新 Future 才是 move 出去的。所以 `f` 可以 `&mut self` 调,`FnMut` 够。

> **钉死这件事**:`map_request`/`map_future` 用 `FnMut`,`map_response`/`map_err`/`map_result`/`and_then`/`then` 用 `FnOnce + Clone`。差别根因是 `f` 要不要 move 进返回的 Future:要 move(异步作用在 Future 输出上)就必须 Clone(因为 call 多次);不要 move(同步作用在入口/Future 本身)就 FnMut 够(直接 `&mut self` 调)。这是写自定义 Service 组合子最容易写错的地方——选错闭包 trait,要么编译过不了(该 Clone 没 Clone),要么多此一举(该 FnMut 的强求 Clone,限制了用户能传的闭包)。

#### 3.5 `and_then` 和 `then`:异步链,用 `FnOnce + Clone`

`and_then` 和 `then` 是两个"异步链"组合子——它们的 `f` 返回一个 Future,组合子要 await 这个 Future。看 `and_then`:

```rust
// tower/src/util/and_then.rs#L91-L109
impl<S, F, Request, Fut> Service<Request> for AndThen<S, F>
where
    S: Service<Request>,
    S::Error: Into<Fut::Error>,
    F: FnOnce(S::Response) -> Fut + Clone,
    Fut: TryFuture,
{
    type Response = Fut::Ok;
    type Error = Fut::Error;
    type Future = AndThenFuture<S::Future, Fut, F>;

    fn poll_ready(&mut self, cx: &mut Context<'_>) -> Poll<Result<(), Self::Error>> {
        self.inner.poll_ready(cx).map_err(Into::into)
    }

    fn call(&mut self, request: Request) -> Self::Future {
        AndThenFuture::new(self.inner.call(request).err_into().and_then(self.f.clone()))
    }
}
```

注意 `call` 里的 `.and_then(self.f.clone())`——这是 `futures_util::TryFutureExt::and_then`,它把内层 Future 成功后的值,喂给 `f`,await `f` 返回的 Future。`f.clone()` 同样是因为 `call` 多次要复用。

`and_then` 的 Future 类型 `AndThenFuture` 用了 `pin_project_lite` 手写一个包装 Future(`and_then.rs#L31-39`),内部包了 `future::AndThen<future::ErrInto<F1, F2::Error>, F2, N>`——这是 `futures_util` 的 `AndThen` 组合子 Future。为什么 `and_then` 要手写包装,而 `map_response` 用 `opaque_future!`?因为 `and_then` 的 Future 类型嵌套更深(`ErrInto<AndThen<...>>`),手写一层包装让对外暴露的类型名简洁(`AndThenFuture` 而不是那一长串),也方便统一加 `Debug`。

`then` 和 `and_then` 几乎一样,差别只在 `f` 的签名:

```rust
// tower/src/util/then.rs#L62-L82
impl<S, F, Request, Response, Error, Fut> Service<Request> for Then<S, F>
where
    S: Service<Request>,
    S::Error: Into<Error>,
    F: FnOnce(Result<S::Response, S::Error>) -> Fut + Clone,   // ← f 吃 Result,不论成败
    Fut: Future<Output = Result<Response, Error>>,
{
    // ...
    fn call(&mut self, request: Request) -> Self::Future {
        ThenFuture::new(self.inner.call(request).then(self.f.clone()))
    }
}
```

`and_then` 的 `f: FnOnce(S::Response) -> Fut`(只成功时调),`then` 的 `f: FnOnce(Result<S::Response, S::Error>) -> Fut`(不论成败都调)。`then` 的 `f` 收到的是整个 `Result`,可以自己做模式匹配(比如把 `Err` 转成 `Ok`,做异步错误恢复)。

两个都用了 `futures_util` 的 Future 组合子(`future::AndThen`/`future::Then`)——这是承 Tokio/futures 生态的组合子,P1-04 已讲过,一句带过指路 [[tokio-source-facts]]。

#### 3.6 `opaque_future!`:把组合子 Future 类型藏起来

讲完七个组合子,补一个它们共用的宏 `opaque_future!`。看 `map_response`:

```rust
// tower/src/util/map_response.rs#L36-L41
opaque_future! {
    /// Response future from [`MapResponse`] services.
    pub type MapResponseFuture<F, N> = MapOk<F, N>;
}
```

`opaque_future!` 是 tower 自己的宏,定义在 `tower/src/macros.rs#L7-L42`:

```rust
// tower/src/macros.rs#L7-L42(略简化)
macro_rules! opaque_future {
    ($(#[$m:meta])* pub type $name:ident<$($param:ident),+> = $actual:ty;) => {
        pin_project_lite::pin_project! {
            $(#[$m])*
            pub struct $name<$($param),+> {
                #[pin]
                inner: $actual
            }
        }
        // ... new() / Debug / Future impl,统统转发给 inner
    };
}
```

这个宏干的事:把一个具体的 Future 类型(`MapOk<F, N>`,来自 `futures_util`)包进一个新的 struct(`MapResponseFuture`),对外只暴露 `MapResponseFuture` 这个名字,内部的具体类型(`MapOk`)藏起来。

为什么要藏?两个理由:

**第一,API 稳定性**。如果 `MapResponse::Future` 直接是 `MapOk<F, N>`(futures_util 的类型),那 tower 升级 futures_util 版本时,这个类型变了,用户的代码(`let f: MapOk<...> = ...`)就 break 了。包一层 `MapResponseFuture`,内部换 futures_util 版本,对外类型名不变,API 稳定。这是"newtype 包装隐藏实现"的 Rust 惯用法。

**第二,统一加 `Debug`**。闭包不实现 `Debug`(Rust 的 closure 永远不 impl Debug),所以 `MapOk<F, N>` 不实现 `Debug`。但 Service 的 Future 通常要有 `Debug`(用于 tracing/调试)。`opaque_future!` 给包装类型手写一个 `Debug`(`debug_tuple(stringify!($name)).field(&format_args!("..."))`,显示成 `MapResponseFuture(...)`),绕过闭包不能 Debug 的限制。

`and_then` 没用 `opaque_future!`,而是手写 `AndThenFuture`(`and_then.rs#L31-65`),原因是它的 Future 嵌套更深(`ErrInto<AndThen<...>>`),手写更灵活。但思路一样:把内部 Future 类型藏起来,对外暴露简洁的 `AndThenFuture` 名字。

> **技巧点**:`opaque_future!` 是 Tower 把"`pin_project_lite` + newtype 包装 + 统一 Debug"这件事自动化的工具。它不是必需的(可以手写),但 tower/util 里大量组合子 Future 都用它,免重复代码。读懂这个宏,就读懂了 tower/util 里所有 `XxxFuture` 类型的来源。

### 第四节:Either——两个 Service 类型二选一

#### 4.1 真身:enum 包装 + Service impl

`Either` 解决的问题是"我有一组 Service,类型各不相同,但要塞进同一个字段/同一个返回类型"。比如路由:匹配到 path A 用 `ServiceA`,匹配到 path B 用 `ServiceB`,但路由表里要存统一类型。看源码:

```rust
// tower/src/util/either.rs#L19-L25
#[derive(Clone, Copy, Debug)]
pub enum Either<A, B> {
    #[allow(missing_docs)]
    Left(A),
    #[allow(missing_docs)]
    Right(B),
}
```

`Either<A, B>` 就是个标准 enum,`Left(A)` 或 `Right(B)`。注意 `#[derive(Clone, Copy, Debug)]`——它派生了 `Copy`(只要 A、B 都 Copy)。这个 `Copy` 看起来奇怪(Service 不是 `&mut self` 吗,怎么能 Copy?),其实合法:Copy 一个 `Either<ServiceA, ServiceB>` 复制的是当前那个变体的 Service 副本,前提是 ServiceA/ServiceB 自己 Copy。实际场景里,被 `Either` 包的 Service 通常是 `Clone` 但不 `Copy`(比如 axum 的 Router),所以 `Either` 在那些场景下也只是 `Clone` 不 `Copy`——derive 在这里只是"如果底层 Copy 我就 Copy",不强制。

然后是关键的 Service impl:

```rust
// tower/src/util/either.rs#L27-L57
impl<A, B, Request> Service<Request> for Either<A, B>
where
    A: Service<Request>,
    B: Service<Request, Response = A::Response, Error = A::Error>,
{
    type Response = A::Response;
    type Error = A::Error;
    type Future = EitherResponseFuture<A::Future, B::Future>;

    fn poll_ready(&mut self, cx: &mut Context<'_>) -> Poll<Result<(), Self::Error>> {
        match self {
            Either::Left(service) => service.poll_ready(cx),
            Either::Right(service) => service.poll_ready(cx),
        }
    }

    fn call(&mut self, request: Request) -> Self::Future {
        match self {
            Either::Left(service) => EitherResponseFuture {
                kind: Kind::Left { inner: service.call(request) },
            },
            Either::Right(service) => EitherResponseFuture {
                kind: Kind::Right { inner: service.call(request) },
            },
        }
    }
}
```

四件事:

**第一,关键约束 `B: Service<Request, Response = A::Response, Error = A::Error>`**。`Either` 的两个 Service 必须有**相同的 Request、Response、Error 类型**。为什么?因为 `Either<A, B>` 作为一个 Service,它的关联类型 `Response`/`Error` 是唯一的——你不能 `Left` 返回 `Response = String`、`Right` 返回 `Response = u32`,那 `Either` 的 `Response` 关联类型没法定。所以 `Either` 强制两个 Service 的 Response、Error 相同(通过约束 `B::Response = A::Response, B::Error = A::Error`)。Request 是泛型参数,自然相同。

**第二,`poll_ready` 按 enum 变体分发**。`match self`,Left 就 poll A 的 ready,Right 就 poll B 的 ready。背压原样转发(只转当前那个变体的)。注意:**只 poll 当前变体的 Service**,另一个变体的 Service 不被 poll——这是合理的,因为只有当前变体会被 `call`。

**第三,`call` 返回 `EitherResponseFuture`,内部又是 enum**。`Either` 的 Future 也是个 enum(`EitherResponseFuture`,内部 `Kind::Left { inner: A::Future }` 或 `Kind::Right { inner: B::Future }`),用 `pin_project_lite` 手写(`either.rs#L59-88`)。Future 的 poll 按 kind 分发,调内层 Future 的 poll。

**第四,Future 的约束 `B: Future<Output = A::Output>`**。`EitherResponseFuture<A, B>` 的 impl 里(`either.rs#L75-88`),`B: Future<Output = A::Output>`——两个 Future 的 Output 必须相同,这呼应 Service 的 Response/Error 相同约束。

#### 4.2 Either 的典型场景:条件路由

`Either` 最典型的场景是"按条件分发到不同类型的 Service"。比如:

```rust
// 根据请求路径选 ServiceA 或 ServiceB,两者类型不同
fn route(req: &Request) -> Either<ServiceA, ServiceB> {
    if req.path() == "/a" {
        Either::Left(service_a.clone())
    } else {
        Either::Right(service_b.clone())
    }
}

// 路由表里存统一类型 Either<ServiceA, ServiceB>
let dispatcher: impl Service<Request, Response = Resp, Error = Err> = service_fn(route);
```

这里 `ServiceA` 和 `ServiceB` 类型不同,但只要它们 `Response = Resp, Error = Err` 相同,就能塞进 `Either<ServiceA, ServiceB>`,作为统一类型用。

> **对照 trait object(P6-17)**:`Either` 是**编译期**分发,enum 变体在编译期固定(虽然运行期选哪个),`match` 在单态化后被编译成跳转表,零运行期开销(除了 enum tag)。对比 `Box<dyn Service>`(P6-17 讲的 trait object),那是**运行期**分发,每次 `call` 是虚函数调用。`Either` 适合"分支数有限且编译期已知"的场景(2 个 Service 用 Either,3 个用 `Either<A, Either<B, C>>` 嵌套),trait object 适合"分支数动态/类型擦除需求强"的场景。`Either` 的代价是类型签名嵌套(`Either<A, Either<B, C>>` 写起来长),trait object 的代价是运行期开销和 Send/Sync 约束复杂。两者对照,是 Tower 在"零开销静态"和"运行期灵活"之间的标准取舍。

#### 4.3 Either 还实现了 Layer

`Either` 不光是 Service,还实现了 Layer(`either.rs#L90-103`):

```rust
// tower/src/util/either.rs#L90-L103
impl<S, A, B> Layer<S> for Either<A, B>
where
    A: Layer<S>,
    B: Layer<S>,
{
    type Service = Either<A::Service, B::Service>;

    fn layer(&self, inner: S) -> Self::Service {
        match self {
            Either::Left(layer) => Either::Left(layer.layer(inner)),
            Either::Right(layer) => Either::Right(layer.layer(inner)),
        }
    }
}
```

这让 `Either<LayerA, LayerB>` 可以当 Layer 用——`layer(inner)` 返回 `Either<LayerA::Service, LayerB::Service>`。这是 0.4.6 加入的特性(CHANGELOG 核实,"Implement Layer for Either<A, B> ([#531])")。意义:你可以"按条件选一个 Layer",选出来的 Either Layer 套到 Service 上,得到 Either Service。这让"运行期选中间件配置"在编译期可表达(不用 trait object)。

> **关于 Either 的变体命名**:0.5.x 源码里 Either 的变体是 `Left(A)` 和 `Right(B)`(源码核实,`either.rs#L20-L25`)。两个泛型参数也叫 `A`、`B`(不是 `L`/`R`)。这是 Rust 生态 `Either` 类型的标准命名(对照 `std::result::Result` 的 `Ok`/`Err`、`std::option::Option` 的 `Some`/`None`,Either crate 的 `Left`/`Right`)。如果你的代码里见过 `EitherService` 或别的命名,那可能是更老的版本或第三方封装——本书以 0.5.2 源码为准。

### 第五节:Optional——可能为空的 Service 占位

#### 5.1 真身:Option<Service> 包装

`Optional` 解决的问题是"我的 Service 可能在某个时刻还没构造好(或被销毁),用一个 `None` 占位"。看源码:

```rust
// tower/src/util/optional/mod.rs#L19-L22
#[derive(Debug)]
pub struct Optional<T> {
    inner: Option<T>,
}
```

`Optional<T>` 就是 `Option<T>` 的 newtype 包装。它的 Service impl:

```rust
// tower/src/util/optional/mod.rs#L35-L59
impl<T, Request> Service<Request> for Optional<T>
where
    T: Service<Request>,
    T::Error: Into<crate::BoxError>,
{
    type Response = T::Response;
    type Error = crate::BoxError;
    type Future = ResponseFuture<T::Future>;

    fn poll_ready(&mut self, cx: &mut Context<'_>) -> Poll<Result<(), Self::Error>> {
        match self.inner {
            Some(ref mut inner) => match inner.poll_ready(cx) {
                Poll::Ready(r) => Poll::Ready(r.map_err(Into::into)),
                Poll::Pending => Poll::Pending,
            },
            // None services are always ready
            None => Poll::Ready(Ok(())),    // ← None 永远 ready
        }
    }

    fn call(&mut self, request: Request) -> Self::Future {
        let inner = self.inner.as_mut().map(|i| i.call(request));
        ResponseFuture::new(inner)
    }
}
```

关键三件事:

**第一,`None` 时 `poll_ready` 返回 `Ready(Ok(()))`**。源码注释明说:"None services are always ready"(None 服务永远就绪)。这是合理的——None 服务没有内层 Service 要等,自然 ready。

**第二,`None` 时 `call` 返回一个立刻 resolve 成错误的 Future**。看 `ResponseFuture`:

```rust
// tower/src/util/optional/future.rs#L27-L39
impl<F, T, E> Future for ResponseFuture<F>
where
    F: Future<Output = Result<T, E>>,
    E: Into<crate::BoxError>,
{
    type Output = Result<T, crate::BoxError>;

    fn poll(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Self::Output> {
        match self.project().inner.as_pin_mut() {
            Some(inner) => Poll::Ready(Ok(ready!(inner.poll(cx)).map_err(Into::into)?)),
            None => Poll::Ready(Err(error::None::new().into())),   // ← None 立刻返回错误
        }
    }
}
```

`None` 时,Future 立刻返回 `Err(error::None)`——这是个特殊的错误类型(`optional/error.rs#L7-L21`),Display 就是字符串 `"None"`。语义:**"你这个 Service 还没构造好,我给你返回个错误,你处理吧"**。

**第三,错误类型是 `BoxError`**。`Optional` 把内层错误统一转成 `crate::BoxError`(`Box<dyn Error + Send + Sync>`),这是 tower 的通用错误类型。这是因为 None 情况要返回一个 `error::None`(它不是 `T::Error`),所以 `Optional::Error` 必须是一个能同时容纳 `T::Error` 和 `error::None` 的类型——选了 `BoxError` 作为这个统一类型。

#### 5.2 Optional 的典型场景:可选中间件

`Optional` 最典型的场景是"这个中间件可能没启用"。比如:

```rust
// 鉴权中间件,可能 enabled 或 disabled
let auth_service: Optional<AuthService> = if config.auth_enabled {
    Optional::new(Some(AuthService::new(...)))
} else {
    Optional::new(None)   // 占位,永远返回错误(或在外层处理 None)
};
```

或者更微妙的场景:axum/tonic 里,某些 Service 是"懒构造"的——启动时还不知道配置,等运行期才能造出来。在那之前用 `Optional::new(None)` 占位,等 Service 造好后 `inner = Some(svc)`。这个"占位 + 后续填"的模型,比"启动时阻塞等 Service 就绪"更灵活。

> **钉死这件事**:`Optional` 是"优雅处理 None"的标准包装。它把 `Option<Service>` 变成一个合法的 Service(None 时永远 ready 但 call 立刻返回 None 错误),让"可能为空的 Service"能塞进任何需要 `Service` 的地方(比如 ServiceBuilder 链、Layer 包装)。代价是错误类型被擦成 `BoxError`(丢失原 Error 类型精度),收益是不用自己手写 Option 分支。

### 第六节:call_all——用 Stream 喂 Service

#### 6.1 真身:Service + Stream = Stream

`call_all` 解决的问题是"我有一堆请求(在一个 Stream 里),想逐个喂给同一个 Service,把响应收集成一个 Stream"。这是 P1-04 预告过、P1-02 第 5 节"`serve_many` 模式"的正式封装。看签名:

```rust
// tower/src/util/mod.rs#L104-L110
fn call_all<S>(self, reqs: S) -> CallAll<Self, S>
where
    Self: Sized,
    S: futures_core::Stream<Item = Request>,
{
    CallAll::new(self, reqs)
}
```

`call_all` 把 `Service + Stream<Item=Request>` 变成 `CallAll`,而 `CallAll` **是一个 Stream**(不是 Future),`Stream<Item = Result<Response, Error>>`。这是关键差别——`call_all` 不消费成单个值(像 `oneshot`),它产出一个响应流。

核心结构在 `call_all/common.rs`:

```rust
// tower/src/util/call_all/common.rs#L11-L24
pin_project! {
    pub(crate) struct CallAll<Svc, S, Q>
    where S: Stream,
    {
        service: Option<Svc>,
        #[pin]
        stream: S,
        queue: Q,           // Q: Drive<Svc::Future>
        eof: bool,
        curr_req: Option<S::Item>
    }
}
```

字段:`service: Option<Svc>`(Option 是为了能 `take_service` 取回 Service)、`stream: S`(请求流)、`queue: Q`(响应 Future 队列,ordered 用 FuturesOrdered、unordered 用 FuturesUnordered)、`eof: bool`(请求流是否结束)、`curr_req: Option<S::Item>`(暂存的当前请求)。

`CallAll` 的 `Stream::poll_next` 是核心循环(`common.rs#L92-L140`):

```rust
// tower/src/util/call_all/common.rs#L92-L140(略简化)
fn poll_next(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Option<Self::Item>> {
    let mut this = self.project();
    loop {
        // 1. 先看队列里有没有完成的响应可以 yield
        if let Poll::Ready(r) = this.queue.poll(cx) {
            if let Some(rsp) = r.transpose()? {
                return Poll::Ready(Some(Ok(rsp)));
            }
        }
        // 2. 队列空 + 请求流也结束,整个 Stream 结束
        if *this.eof {
            if this.queue.is_empty() {
                return Poll::Ready(None);
            } else {
                return Poll::Pending;
            }
        }
        // 3. 没暂存请求,从 stream 拿下一个请求
        if this.curr_req.is_none() {
            *this.curr_req = match ready!(this.stream.as_mut().poll_next(cx)) {
                Some(next_req) => Some(next_req),
                None => { *this.eof = true; continue; }
            };
        }
        // 4. 等 Service ready
        let svc = this.service.as_mut().expect("...");
        if let Err(e) = ready!(svc.poll_ready(cx)) {
            *this.eof = true;
            return Poll::Ready(Some(Err(e)));
        }
        // 5. call,把 Future push 进队列
        this.queue.push(svc.call(this.curr_req.take().unwrap()));
    }
}
```

五步循环:`poll 队列 yield → 检查 EOF → 拿请求 → poll_ready → push call Future`。这个循环把 P1-02 讲的"`poll_ready → call` 协作时序"封装成了一个 Stream 的 `poll_next`——每次 `poll_next` 都可能推进多个请求(call 完一个立刻 call 下一个),也可能 yield 一个完成的响应。

#### 6.2 call_all 的两个变体:ordered 和 unordered

`call_all` 有两个变体,通过 `Drive<F>` trait(`common.rs#L40-L46`)区分:

```rust
// tower/src/util/call_all/common.rs#L40-L46
pub(crate) trait Drive<F: Future> {
    fn is_empty(&self) -> bool;
    fn push(&mut self, future: F);
    fn poll(&mut self, cx: &mut Context<'_>) -> Poll<Option<F::Output>>;
}
```

`Drive<F>` 是个内部 trait,描述"一个能装一堆 Future 的队列"。两个实现:

- **`FuturesOrdered<F>`**(`call_all/ordered.rs#L165-L177`):保序队列,响应按请求顺序 yield(FIFO)。底层是 `futures_util::stream::FuturesOrdered`。
- **`FuturesUnordered<F>`**(`call_all/unordered.rs#L86-L97`):无序队列,响应谁先完成谁先 yield。底层是 `futures_util::stream::FuturesUnordered`。

两个实现是同一份 `CallAll` 逻辑(`common.rs`),只是 `Q` 类型不同。`CallAll::new`(ordered)用 `FuturesOrdered::new()`,调用 `.unordered()`(common.rs#L77-L82)切换到 `FuturesUnordered`。这是用 trait( `Drive<F>`)做"策略模式",把队列策略从主循环逻辑里解耦——一个非常工整的设计。

#### 6.3 call_all 的背压:Service 满了就停 pull

`call_all` 的一个精妙之处是**它自动尊重 Service 的背压**。看第 4 步:`ready!(svc.poll_ready(cx))`——如果 Service 还没 ready(返回 Pending),整个 `poll_next` 返回 Pending,不继续从 stream pull 请求。这意味着:**如果 Service 满了(比如内层 ConcurrencyLimit 没有空闲 permit),call_all 不会从 stream 里继续拉请求塞进队列,而是等 Service ready 再拉**。

这是 P1-02 讲的"背压传染"在 call_all 里的体现:Service 的背压,自动反向传到 stream 的消费速率上。stream 快、Service 慢,call_all 自然慢下来(stream 在 call_all 这边积压,反向传到 stream 生产端)。这个性质让 `call_all` 成为一个"背压安全"的请求处理器——你不用担心 stream 快把 Service 淹没,call_all 会自动 throttle。

#### 6.4 call_all 的典型场景:消息队列消费者

`call_all` 的典型场景是"从消息队列/socket 读一堆请求,逐个处理":

```rust
// 从 Kafka/Redis Stream 读请求,喂给业务 Service
let requests: impl Stream<Item = Request> = message_queue.subscribe();
let responses: impl Stream<Item = Result<Response, Error>> = business_service.call_all(requests);

while let Some(resp) = responses.next().await {
    match resp {
        Ok(r) => send_response(r).await,
        Err(e) => log_error(e),
    }
}
```

或者 hyper server 端,一个连接收到一堆 HTTP/2 请求(多路复用),用 `call_all` 把请求流喂给业务 Service——这是 axum/tonic 内部处理多路复用请求的雏形(虽然它们实际实现更复杂)。

`call_all` 还提供了 `into_inner()`/`take_service()` 方法(`common.rs#L65-L75`),让你在 Stream 处理完后取回 Service(因为 Service 存在 `Option<Svc>` 里)。这是"用完 Service 想复用"的逃生口。

### 第七节:future_service——把异步构造 Service 当 Service

#### 7.1 真身:状态机 Future → Service

`future_service` 解决的问题是"我的 Service 要异步构造(比如启动时要先连数据库,连上了才能造 Service),但在 Service 造好之前,我已经需要一个 Service 占位塞进调用链"。看签名:

```rust
// tower/src/util/future_service.rs#L48-L54
pub fn future_service<F, S, R, E>(future: F) -> FutureService<F, S>
where
    F: Future<Output = Result<S, E>> + Unpin,
    S: Service<R, Error = E>,
{
    FutureService::new(future)
}
```

`future_service(future)` 把一个"返回 Service 的 Future"包成 `FutureService`。`FutureService` 内部是个状态机:

```rust
// tower/src/util/future_service.rs#L122-L126
#[derive(Clone)]
enum State<F, S> {
    Future(F),
    Service(S),
}
```

两个状态:`Future(F)`(还没跑完,持有构造 Future)和 `Service(S)`(跑完了,持有构造好的 Service)。Service impl:

```rust
// tower/src/util/future_service.rs#L143-L172
impl<F, S, R, E> Service<R> for FutureService<F, S>
where
    F: Future<Output = Result<S, E>> + Unpin,
    S: Service<R, Error = E>,
{
    type Response = S::Response;
    type Error = E;
    type Future = S::Future;

    fn poll_ready(&mut self, cx: &mut Context<'_>) -> Poll<Result<(), Self::Error>> {
        loop {
            self.state = match &mut self.state {
                State::Future(fut) => {
                    let fut = Pin::new(fut);
                    let svc = futures_core::ready!(fut.poll(cx)?);   // ← poll 构造 Future
                    State::Service(svc)                              // ← 切到 Service 态
                }
                State::Service(svc) => return svc.poll_ready(cx),    // ← 转发内层
            };
        }
    }

    fn call(&mut self, req: R) -> Self::Future {
        if let State::Service(svc) = &mut self.state {
            svc.call(req)
        } else {
            panic!("FutureService::call was called before FutureService::poll_ready")
        }
    }
}
```

两件事:

**第一,`poll_ready` 驱动构造 Future**。在 `State::Future` 态,`poll_ready` poll 那个构造 Future(`Pin::new(fut); fut.poll(cx)`)。如果构造 Future 返回 Pending,`ready!` 让 `poll_ready` 返回 Pending(把"等构造"这件事表达成 Service 的"等就绪")。如果构造 Future 完成,切到 `State::Service`,继续 poll 内层 Service 的 poll_ready(用 `loop` 一次 poll 推进两个状态)。这就是把"异步构造 Service"塞进 `poll_ready` 的精妙——调用方 `.ready().await` 的时候,既在等构造 Future 完成,又在等内层 Service ready,两件事合一。

**第二,`call` 在没 ready 时 panic**。如果 `poll_ready` 没跑完(还在 `State::Future`),直接 `call` 会 panic("FutureService::call was called before FutureService::poll_ready")。这是 P1-02 讲的 Service 契约("必须先 poll_ready 拿到 Ready 才能 call")的强制执行——`FutureService` 用 panic 守住这个契约。

#### 7.2 `Unpin` 约束:poll_ready 拿不到 Pin

`future_service` 的签名有个 `F: Unpin` 约束(源码 `future_service.rs#L50`)。这个约束看起来多余,源码注释专门解释了(`future_service.rs#L40-L47`):

> "The `Unpin` bound on `F` is necessary because the future will be polled in `Service::poll_ready` which doesn't have a pinned receiver (it takes `&mut self` and not `self: Pin<&mut Self>`). So we cannot put the future into a `Pin` without requiring `Unpin`."

翻译:`Service::poll_ready` 的签名是 `&mut self`,不是 `Pin<&mut Self>`。要在 `poll_ready` 里 poll 一个 Future,需要 `Pin<&mut F>`,但 `&mut self.state`(拿到 `&mut F`)不能直接 Pin(那不 sound,除非 F 是 Unpin)。所以约束 `F: Unpin`,才能 `Pin::new(fut)` 安全地拿到 `Pin<&mut F>`。

这是 `Service` trait 的 `poll_ready` 签名(`&mut self` 而非 `Pin<&mut Self>`)带来的约束传导。源码注释建议:如果你传的是 `async {}` 块(它不 Unpin),用 `Box::pin(async { ... })` 包一下(`BoxFuture` 是 Unpin 的)。

> **钉死这件事**:`future_service` 把"异步构造 Service"塞进 `poll_ready`,用状态机(`Future → Service`)把构造 Future 的推进和内层 Service 的就绪合一。`Unpin` 约束是 `poll_ready` 签名(`&mut self`)的必然结果——这是 `Service` trait 设计(`poll_ready` 不带 Pin)对实现者的一个微妙约束。典型场景:启动时连数据库、连了才造 Service,但启动早期就需要 Service 占位塞进调用链。

---

## 技巧精解

正文把 util 组合子和四个救场角色讲完了。这一节单独拆透两个最容易写错的实现技巧:**`service_fn` 的 `FnMut` 约束为什么不破坏 Service 契约**,以及 **`map_*` 组合子闭包 trait 的 `FnMut` vs `FnOnce + Clone` 取舍**。两个都配反面对比——朴素写法会撞什么墙。

### 技巧一:`service_fn` 的 `FnMut` 约束,凭什么不破坏 `call(&mut self)` 契约

**这个技巧在干什么**:`service_fn` 用 `T: FnMut(Request) -> F` 把闭包包成 Service,`call` 里 `(self.f)(req)` 调闭包。为什么不要求 `Fn`(只能不可变借用)或 `FnOnce`(只能调一次)?`FnMut` 凭什么够?

**为什么妙**:先回顾 Rust 的三个闭包 trait:

| trait | 调用方式 | 能捕获什么 | 能调几次 |
|---|---|---|---|
| `FnOnce` | `self`(消费) | 任意(包括 move) | 一次 |
| `FnMut` | `&mut self` | 可变借用 | 多次 |
| `Fn` | `&self` | 不可变借用(含 `Sync` 友好) | 多次(可并发) |

`service_fn` 选 `FnMut`,理由有二:

**理由一:`call(&mut self)` 天然适合 `FnMut`**。Service 的 `call` 是 `fn call(&mut self, req: Request) -> Self::Future`,它在 `&mut self` 上调用。`self.f` 是 `&mut T`(T 是闭包类型),`(self.f)(req)` 就是 `FnMut::call_mut(&mut self.f, req)`——`FnMut` 的调用签名正是 `&mut self`,完美匹配。不需要额外的 clone(对比 `map_response` 的 `FnOnce + Clone`,因为 `map_response` 的 `f` 要 move 进返回 Future)。

**理由二:`FnMut` 允许闭包捕获可变状态**。这是 `Fn` 做不到的。设想一个带计数器的 handler:

```rust
let mut count = 0u64;
let svc = service_fn(move |req: Request| {
    count += 1;   // ← 闭包可变捕获 count
    async move { Ok(Response { id: count, ... }) }
});
```

这个闭包 `move` 捕获了 `count`,每次调用 `count += 1`。这要求闭包能 `&mut self` 调用——`FnMut`。如果 `service_fn` 要求 `Fn`(只能 `&self`),这个闭包就写不出来(`count += 1` 需要 `&mut count`,而 `Fn` 只给 `&self`)。

`FnMut` 的代价是:**`ServiceFn<T>` 不能在多个线程间并发 call**(因为 `&mut` 不允许别名)。但这正好匹配 Service 的 `&mut self` 语义——P1-02 讲过,Service 的 `call(&mut self)` 本来就要求独占,不能并发 call 同一个实例。要并发,Clone 出多个 ServiceFn 实例(每个实例独立的闭包状态),各自独占地 call。`service_fn` 派生 `Clone`(`#[derive(Copy, Clone)]`),正是为了支持这个——但要求 `T: Clone`(闭包 Clone)。

**反面对比一:如果要求 `Fn`**。那 `service_fn` 只能包"不可变捕获"的闭包,带计数器/缓存/可变状态的 handler 全部写不出来。用户被迫把状态塞进 `Arc<Mutex<...>>`(运行期锁),这破坏了 Rust 的零开销抽象,也引入了锁开销。`FnMut` 是更宽松的约束,允许更多闭包类型。

**反面对比二:如果要求 `FnOnce`**。那 `FnOnce` 的闭包只能调一次,但 Service 的 `call` 可能被调多次(同一个 Service 发多个请求)——`FnOnce` 不够。除非每次 `call` 都 clone 一个新闭包(`FnOnce + Clone`,像 `map_response` 那样),但那要求闭包 Clone,对带状态的闭包(每次状态不同)不友好。`FnMut` 既允许可变状态,又允许反复调用,是 Service 的 `&mut self` 语义下最自然的约束。

**反面对比三:`map_response` 为什么不能也用 `FnMut`**。对比 `map_response` 的 `call`:`self.inner.call(request).map_ok(self.f.clone())`。这里 `f` 要 move 进 `map_ok` 返回的 MapOk Future(因为 MapOk 要在内层 Future 完成后,在另一个 task 上调 `f`)。`move` 出 `f` 意味着 `f` 不能是 `&mut self.f`(那只是借用),必须是 `f` 的所有权——这就要求 `f` 是 Clone 的(每次 call 要一份新 `f`)。`FnMut` 不够,因为 `FnMut` 不保证 Clone。所以 `map_response` 必须 `FnOnce + Clone`。`service_fn` 没有"move 进返回 Future"这一步(它返回的 Future 就是闭包直接返回的 `F`,`f` 不 move 进去),所以 `FnMut` 够。

> **钉死这件事**:`service_fn` 的 `FnMut` 约束,是 Service 的 `call(&mut self)` 语义下最自然的选择——它匹配 `&mut self` 调用方式,允许可变状态闭包,不要求 Clone。`map_response` 的 `FnOnce + Clone`,是因为 `f` 要 move 进返回 Future,必须 Clone。两者的差别根植于"闭包用在哪一步":同步调用(入口/转发)用 `FnMut`,异步 await 后用 `FnOnce + Clone`。这是 Service 组合子设计最精微的约束取舍。

### 技巧二:`Either` 的编译期分发 vs trait object 的运行期分发

**这个技巧在干什么**:`Either<A, B>` 用 enum 把两个不同 Service 类型捏成一个,在编译期分发(`match`),零运行期开销。对比 `Box<dyn Service>`(P6-17)的运行期分发(虚函数)。

**为什么妙**:设想一个路由场景,有 3 个不同类型的 Service,要塞进统一类型。两种写法:

**写法一:Either 嵌套(编译期分发)**。

```rust
type ThreeWay = Either<ServiceA, Either<ServiceB, ServiceC>>;

fn route(req: &Request) -> ThreeWay {
    match req.path() {
        "/a" => Either::Left(svc_a.clone()),
        "/b" => Either::Right(Either::Left(svc_b.clone())),
        _    => Either::Right(Either::Right(svc_c.clone())),
    }
}
```

`ThreeWay` 是个具体类型,编译期固定。`route()` 返回 `ThreeWay`,调用方拿到后 `poll_ready`/`call`,内部两层 `match`(外层 Left/Right、内层 Left/Right),单态化后被编译成跳转指令。运行期开销:两个 enum tag 检查(几个时钟周期),没有虚函数调用。

**写法二:Box<dyn Service>(运行期分发)**。

```rust
type Erased = BoxCloneService<Request, Response, Error>;  // P6-17 讲的 trait object

fn route(req: &Request) -> Erased {
    match req.path() {
        "/a" => svc_a.clone().boxed_clone(),
        "/b" => svc_b.clone().boxed_clone(),
        _    => svc_c.clone().boxed_clone(),
    }
}
```

`Erased` 是 trait object(`BoxCloneService` 内部是 `Box<dyn CloneService<...>>`,P6-17 讲)。调用方 `poll_ready`/`call` 都是虚函数调用(查 vtable),每次有一次间接跳转。运行期开销:vtable 查找 + 间接调用。

**两者取舍**:

| 维度 | Either(编译期分发) | Box<dyn Service>(运行期分发) |
|---|---|---|
| 运行期开销 | enum tag 检查(几纳秒) | vtable 查找 + 间接调用(稍高) |
| 类型签名 | 嵌套长(`Either<A, Either<B, C>>`) | 简洁统一(`BoxCloneService<...>`) |
| 分支数限制 | 编译期固定(N 个要嵌套 N-1 层 Either) | 无限(运行期动态) |
| 类型信息 | 保留(单态化后内联) | 擦除(丢具体类型) |
| Clone/Send/Sync | 跟着底层(底层都 Clone 则 Either Clone) | 要专门构造(BoxCloneService 见 P6-17) |
| 适用场景 | 分支数少且编译期已知 | 分支动态/需类型擦除/跨函数统一 |

`Either` 适合"分支数少(2~5 个)、编译期已知、追求零开销"的场景。比如 axum 的路由,内部用 `Either` 嵌套表达"匹配到不同 path 用不同 handler Service"。`Box<dyn Service>` 适合"分支动态、需要把异构 Service 塞进容器/统一返回类型"的场景。比如 ServiceBuilder 套出来的巨大嵌套类型要存进 struct 字段,就 `boxed_clone()` 擦除成 `BoxCloneService`(P6-17)。

**反面对比:gRPC/Envoy 的运行期 filter chain**(对照《gRPC》/《Envoy》)。gRPC C++ core 的 filter stack、Envoy 的 HCM filter chain,都是运行期链表/vector,每次请求遍历链表(`for (auto& f : filters_) f->...`),每个 filter 是虚函数调用。它们选运行期,是因为 C++ 的模板嵌套太深会爆编译时间/错误信息,且 RPC 的 IO 开销远大于虚函数开销,运行期分发可接受。Tower 的 `Either` 选编译期,是因为 Rust 的单态化 + trait 约束能把嵌套类型错误信息控制得可读(`Either<A, Either<B, C>>` 这种),且 Rust 生态对零开销抽象有执念。同一思想(类型分发),两种语言,不同取舍。P7-20 收束章会双对照讲透。

**为什么 sound**:`Either` 是普通 enum,`Service` 的 impl 由 trait 约束保证(`A: Service<Request>`, `B: Service<Request, Response = A::Response, Error = A::Error>`)。`poll_ready`/`call` 按 enum 变体分发,只 poll/call 当前变体的 Service——这是安全的(另一个变体不被访问)。Future 类型 `EitherResponseFuture` 也是 enum,poll 按 kind 分发,内部用 `pin_project_lite` 保证 `#[pin]` 字段(内层 Future)不被 move。没有 unsafe,没有运行期反射,类型系统全程背书。

---

## 组合子决策表

把本章讲的所有 util 组合子和救场角色,按"什么时候用"汇总成一张决策表,方便实际写代码时查:

| 你想做的事 | 用什么 | 闭包约束(源码核实) | 返回类型 |
|---|---|---|---|
| 把一个 async fn / 闭包变成 Service | `service_fn(f)` | `T: FnMut(Request) -> Future<Output = Result<R, E>>` | `ServiceFn<T>` |
| 把"异步构造 Service 的 Future"当 Service | `future_service(fut)` | `F: Future<Output = Result<S, E>> + Unpin` | `FutureService<F, S>` |
| 请求进 Service 前,改一下 Request 类型 | `.map_request(f)` | `F: FnMut(R1) -> R2` | `MapRequest<S, F>` |
| Service 成功后,改一下 Response 类型 | `.map_response(f)` | `F: FnOnce(S::Response) -> Response + Clone` | `MapResponse<S, F>` |
| Service 失败后,改一下 Error 类型(含 poll_ready 错误) | `.map_err(f)` | `F: FnOnce(S::Error) -> Error + Clone` | `MapErr<S, F>` |
| 同时改 Response 和 Error,接收整个 Result | `.map_result(f)` | `F: FnOnce(Result<S::Response, S::Error>) -> Result<Response, Error> + Clone` | `MapResult<S, F>` |
| 给 Service 返回的 Future 套一层(如 timeout/取消) | `.map_future(f)` | `F: FnMut(S::Future) -> Fut` | `MapFuture<S, F>` |
| Service 成功后,链一个返回 Future 的异步计算 | `.and_then(f)` | `F: FnOnce(S::Response) -> Fut + Clone, Fut: TryFuture` | `AndThen<S, F>` |
| Service 完成后(不论成败),链一个返回 Future 的异步 | `.then(f)` | `F: FnOnce(Result<S::Response, S::Error>) -> Fut + Clone, Fut: Future` | `Then<S, F>` |
| 两个不同类型 Service 二选一(编译期分发) | `Either::Left(svc_a)` / `Either::Right(svc_b)` | 两个 Service 必须 `Response`、`Error` 相同 | `Either<A, B>` |
| 一个可能为 None 的 Service 占位 | `Optional::new(Some(svc))` 或 `Optional::new(None)` | `T: Service<Request>, T::Error: Into<BoxError>` | `Optional<T>` |
| 用一个 Stream 喂 Service,产响应 Stream(保序) | `svc.call_all(stream)` | `S: Stream<Item = Request>` | `CallAll<Svc, S>`(是 Stream) |
| 用 Stream 喂 Service,响应无序 | `svc.call_all(stream).unordered()` | 同上 | `CallAllUnordered<Svc, S>` |
| 消费 Service,发一个请求拿响应 | `svc.oneshot(req)` | — | `Oneshot<S, Req>`(是 Future) |
| 等 Service ready(借用,不消费) | `svc.ready()` | — | `Ready<'_, S, Req>`(是 Future) |
| 等 Service ready(消费,拿回所有权) | `svc.ready_oneshot()` | — | `ReadyOneshot<S, Req>`(是 Future) |
| 把 Service 擦成 Box<dyn Service> 类型 | `.boxed()` / `.boxed_clone()` | — | `BoxService`/`BoxCloneService`(P6-17) |

决策口诀:

- **要造新 Service(无资源)** → `service_fn`。
- **要在请求/响应/错误上动手脚** → `map_request`/`map_response`/`map_err`/`map_result`。
- **要在 Future 上动手脚(套 timeout)** → `map_future`(或直接用 Timeout 中间件)。
- **要链异步后续** → `and_then`(只成功)/ `then`(不论成败)。
- **类型二选一** → `Either`(2 个)/ `Either<A, Either<B, C>>`(3 个)/ `boxed_clone()`(动态多)。
- **Service 可能为空** → `Optional`。
- **批量喂请求** → `call_all`(保序)/ `call_all().unordered()`(无序)。
- **一次发一个请求** → `oneshot`。
- **异步构造 Service** → `future_service`。

---

## 章末小结

### 回扣组合单元主线

这一章服务的是 **组合单元** 这一面——util 组合子怎么让 Service 像 Iterator 一样可组合、可链式、可轻量构造。把整章收束成一句:

> **`service_fn` 用一个泛型 `ServiceFn<T>` 把闭包/async fn 包成 Service(`poll_ready` 永远 Ready,函数不持有资源);`map_*`/`and_then`/`then` 七个组合子是 Service 版 Iterator 组合子(`poll_ready` 原样转发内层,背压透传,`call` 里用闭包改数据流,闭包约束按"同步入口用 FnMut、异步 await 用 FnOnce+Clone"分两类);`Either`/`Optional`/`call_all`/`future_service` 四个救场角色覆盖了"类型二选一/可能为空/Stream 喂 Service/异步构造"四种实战场景。util 模块的全部哲学,是"让 Service 像闭包和 Iterator 一样好写,又不丢 Service 的背压契约"。**

这是 Tower 组合单元的最后一层:Service trait(P1-02)给执行单元,Layer/Stack(P1-03)给组合原语,ServiceBuilder/ServiceExt(P1-04)给组合工程化,本章 util 组合子给"轻量构造 + 救场角色"。到此,Tower 的组合语言全部铺完——你能用 `service_fn` 三行写一个 handler,能用 `.map_request().map_response()` 给它挂装饰,能用 `Either`/`Optional` 处理特殊场景,能用 `call_all` 喂 Stream,能用 `future_service` 异步构造。后面 P6-19,就是看 axum/tonic/hyper/Pingora 怎么用这套组合语言搭真实框架。

### 五个"为什么"清单

1. **为什么 `service_fn` 的 `poll_ready` 永远返回 `Ready(Ok)`,这违反 P1-02 的背压契约吗?**
   不违反。`service_fn` 表达的是"无状态、不持有可耗尽资源"的 Service(闭包捕获的通常是 `Arc<...>` 共享句柄),没有资源要预留,自然永远 ready。P1-02 的契约是"poll_ready 返回 Ready 后,call 之前重复 poll 必须继续返回 Ready",`service_fn` 满足这条(它永远 Ready)。`service_fn` 适合业务 handler,不适合持资源中间件(那些要手写 struct + impl)。

2. **为什么 `map_response`/`map_err`/`map_result`/`and_then`/`then` 用 `FnOnce + Clone`,而 `map_request`/`map_future` 用 `FnMut`?**
   根因是闭包要不要 move 进返回的 Future。`map_response` 等五个的闭包作用在"内层 Future 异步 await 后",要 move 进返回 Future,所以必须 Clone(因为 call 多次);`map_request`/`map_future` 的闭包作用在"入口同步"或"包 Future 同步",`&mut self` 调用就够,FnMut 够。这是 Service 组合子区别于 Iterator 组合子(`.map` 只要 FnMut)的关键——Service 的 call 多次复用,异步作用的闭包要 Clone。

3. **为什么 `map_err` 在 `poll_ready` 也 apply `f`,而 `map_response` 不用?**
   因为错误有两个来源:`poll_ready` 的错误(服务结构性失败,如连接断)和 `call` Future 的错误(请求级失败)。`map_err` 的语义是"转换所有错误",所以两个来源都 cover。`map_response` 只管成功响应,而 `poll_ready` 不产生 Response,所以不介入。写 `map_err` 的 `f` 时要假设它可能收到 poll_ready 的错误。

4. **为什么 `Either` 强制两个 Service 的 Response、Error 相同?**
   因为 `Either<A, B>` 作为一个 Service,关联类型 `Response`/`Error` 是唯一的,不能 Left 返回 `String`、Right 返回 `u32`。所以约束 `B: Service<Request, Response = A::Response, Error = A::Error>`,强制两个 Service 的 Response、Error 相同。Request 是泛型参数,自然相同。这让 Either 成为"分支数有限、编译期分发、零开销"的类型统一方案,对照 trait object(P6-17)的运行期分发。

5. **为什么 `future_service` 要求 `F: Unpin`,而 `service_fn` 不用?**
   因为 `future_service` 要在 `Service::poll_ready` 里 poll 那个构造 Future,而 `poll_ready` 签名是 `&mut self`(不是 `Pin<&mut Self>`)。要在 `&mut self` 上拿 `Pin<&mut F>`,需要 `F: Unpin`(`Pin::new(fut)` 才 sound)。`service_fn` 不在 poll_ready 里 poll Future(它的 Future 是 call 时闭包直接返回的),所以没这个约束。源码注释建议:`async {}` 块不 Unpin,用 `Box::pin(async { ... })` 包成 BoxFuture(Unpin)再传。

### 想继续深入往哪钻

- **源码**:`tower/src/util/service_fn.rs`(本章主角之一,82 行,通读)、`tower/src/util/map_response.rs` + `map_err.rs` + `map_result.rs` + `map_request.rs` + `map_future.rs`(七个组合子的 impl,对比闭包约束)、`tower/src/util/and_then.rs` + `then.rs`(异步链组合子,看 `pin_project_lite` 手写 Future)、`tower/src/util/either.rs`(`Either` enum + Service impl + Layer impl)、`tower/src/util/optional/{mod,future,error}.rs`(`Optional` 三件套)、`tower/src/util/call_all/{common,ordered,unordered}.rs`(`Drive<F>` trait + 两种队列)、`tower/src/util/future_service.rs`(状态机 Service)、`tower/src/util/ready.rs` + `oneshot.rs`(P1-04 讲过,配合本章读)、`tower/src/macros.rs#L7-L42`(`opaque_future!` 宏)。
- **承接《Tokio》[[tokio-source-facts]]**:本章的"闭包当一等公民 + 组合子 + 单态化"范式,正是标准库 Iterator 和 Tokio Future 组合子的同款。`futures_util::TryFutureExt::map_ok`/`and_then`/`then` 是 `map_response`/`and_then`/`then` 的底层(P1-04 已讲)。`FuturesOrdered`/`FuturesUnordered` 是 `call_all` 的队列底层。这些 Tokio/futures 已拆透,一句带过指路。
- **承接《hyper》[[hyper-source-facts]]**:hyper 的 Service 删了 poll_ready,所以 hyper 用闭包当 Service 时(比如 `hyper::service::service_fn`),它的 service_fn 也不用处理 poll_ready。Tower 的 `service_fn` 保留 poll_ready(虽然永远 Ready),这是"通用层保留契约"vs"协议层简化"的对照。hyper 怎么把闭包当 Service,见《hyper》P1-02/P1-03。
- **承接 P1-04**:本章是 P1-04 `ServiceExt` 组合子的深化。P1-04 讲了 `MapResponse` 转发 poll_ready、`Oneshot` 状态机,本章把七个组合子的闭包约束(`FnMut` vs `FnOnce+Clone`)钉死,补全 P1-04 没展开的细节。
- **对照《gRPC》[[grpc-source-facts]]/《Envoy》[[envoy-source-facts]]**:gRPC filter stack 和 Envoy filter chain 是运行期链表,Tower `Either` 是编译期 enum 分发,同一思想两种语言落地。P7-20 收束章会双对照讲透。
- **下一章 P6-19**:本章讲完了 util 组合子(轻量构造 + 救场),下一章是第 6 篇收束——**Tower 在 axum/tonic/hyper/Pingora 怎么落地**。axum 的 `from_fn`(把 async fn 包成 Tower Service,底层就是本章的 `service_fn` 思路)、tonic 的 interceptor、hyper 1.x 的 Service(删了 poll_ready)、Pingora 的 proxy filter,四个真实框架怎么用 Tower 这套组合语言。届时你会看到,本章讲的 `service_fn` + map_*,是 axum/tonic 把用户业务代码接进 Tower 中间件栈的底层砖块。

### 一句话引出下一章

> util 组合子讲完了"怎么轻量写 Service",但 Tower 真正的力量,在于它是 axum/tonic/hyper/Pingora 的共同骨架——四个真实框架,怎么用 `service_fn` + ServiceBuilder + Layer 这套组合语言,把用户的业务 handler 接进中间件栈?hyper 1.x 删了 poll_ready,它怎么和保留 poll_ready 的 Tower Service 对接?axum 的 Router 怎么用 Tower Layer 组装?下一章 **P6-19《Tower 在 axum/tonic/hyper/Pingora 怎么落地》** 收束第 6 篇,把这本书讲的全部 Tower 抽象,落到四个真实框架的真实用法上。

---

> 本章源码引用(tower @ tower-0.5.2):
> - [service_fn 函数 + ServiceFn struct](../tower/tower/src/util/service_fn.rs#L46-L56)
> - [ServiceFn 的 Service impl(poll_ready 永远 Ready,call 调闭包)](../tower/tower/src/util/service_fn.rs#L66-L82)
> - [MapResponse impl(poll_ready 转发,call 用 map_ok + Clone)](../tower/tower/src/util/map_response.rs#L59-L77)
> - [MapErr impl(poll_ready 也 apply f)](../tower/tower/src/util/map_err.rs#L59-L77)
> - [MapRequest impl(FnMut,call 不 clone)](../tower/tower/src/util/map_request.rs#L43-L61)
> - [MapFuture impl(FnMut,call 包 Future)](../tower/tower/src/util/map_future.rs#L49-L67)
> - [MapResult impl(FnOnce + Clone,改整个 Result)](../tower/tower/src/util/map_result.rs#L59-L78)
> - [AndThen impl(FnOnce + Clone,异步链)](../tower/tower/src/util/and_then.rs#L91-L109)
> - [Then impl(FnOnce + Clone,不论成败异步链)](../tower/tower/src/util/then.rs#L62-L82)
> - [Either enum + Service impl(强制 Response/Error 相同)](../tower/tower/src/util/either.rs#L19-L57)
> - [Either 的 Layer impl](../tower/tower/src/util/either.rs#L90-L103)
> - [EitherResponseFuture + Kind(pin_project)](../tower/tower/src/util/either.rs#L59-L88)
> - [Optional struct + Service impl(None 永远 ready)](../tower/tower/src/util/optional/mod.rs#L19-L59)
> - [Optional ResponseFuture(None 立刻返回错误)](../tower/tower/src/util/optional/future.rs#L27-L39)
> - [Optional error::None](../tower/tower/src/util/optional/error.rs#L7-L21)
> - [CallAll 核心结构 + Drive trait](../tower/tower/src/util/call_all/common.rs#L11-L46)
> - [CallAll Stream::poll_next 五步循环](../tower/tower/src/util/call_all/common.rs#L92-L140)
> - [CallAll ordered(FuturesOrdered)](../tower/tower/src/util/call_all/ordered.rs)
> - [CallAll unordered(FuturesUnordered)](../tower/tower/src/util/call_all/unordered.rs)
> - [future_service 函数 + FutureService 状态机](../tower/tower/src/util/future_service.rs#L48-L172)
> - [ServiceExt trait(oneshot/call_all/map_* 等)](../tower/tower/src/util/mod.rs#L71-L110)
> - [opaque_future! 宏](../tower/tower/src/macros.rs#L7-L42)
> - [ReadyOneshot / Ready(P1-04 讲过,配合本章)](../tower/tower/src/util/ready.rs#L16-L103)
> - [CHANGELOG:Either Layer impl 0.4.6 加入](../tower/tower/CHANGELOG.md#L221)
