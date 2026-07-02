# 第 9 章 · Handler trait:把 async fn 变 Service

> **核心问题**:为什么随便一个 `async fn create_user(State<AppState>, Path<i32>, Json<CreateUser>) -> impl IntoResponse`,直接写进 `Router::new().route("/", post(create_user))` 就能当 handler?它没有 `impl Service for ...`,没有 `#[handler]` 这种过程宏注解,就是一个普通的 `async fn`。axum 凭什么在编译期把这种"任意参数个数、任意参数类型"的 `async fn` 变成一个满足 `tower_service::Service<Request>` 的对象?那个 `Handler<T, S>` trait 的 `T` 参数到底在编码什么?那一对 `impl_handler!` + `all_the_tuples!` 宏在源码里到底生成了什么?
>
> 这是全书最值得讲、也最容易看不懂的一章。axum 整个"声明式 handler"的人体工学——你只要写个 `async fn` 就完事,参数自动从 Request 提取、返回值自动变 Response——全部建立在一个巧得离谱的 Rust 类型系统手段上:用一个 tuple 当 trait 的类型参数,绕开孤儿规则,然后让宏对 0~16 个参数各生成一份 `impl Handler`。讲不清这套机制,等于没讲 axum。
>
> **读完本章你会明白**:
>
> 1. 为什么 `Handler<T, S>` trait 有两个类型参数——`S` 是状态(承 P1-04,handler 处理请求时需要的 state),`T` 是一个**coherence 占位 tuple**——它存在的唯一目的,是让宏能为"0~16 个不同参数个数"的 `async fn` **各写一份** `impl Handler for F`,而每一份 impl 的"参数列表类型"必须不同,否则会撞 Rust 的孤儿规则(对同一 `F` 不能多次 impl 同一个 trait);
> 2. `impl_handler!` + `all_the_tuples!` 两个宏怎么用 **16 行手写** 展开 16 个 `impl Handler` 块(对应 1~16 参数),外加一个单独手写的 0 参数 impl(`Handler<((),), S>`)——一共 17 份 impl,每一份里"前 n-1 个参数走 `FromRequestParts`(只读 parts、可重复跑),最后一个走 `FromRequest`(消费 body、只能跑一次)";
> 3. `Handler::call(self, req, state)` 内部到底干了什么——把 `req` 拆成 `parts + body`,逐个对前 n-1 个参数 `await from_request_parts(&mut parts, &state)`(失败短路返回 Rejection),再把重组的 req 交给最后一个参数 `await from_request(req, &state)`(消费 body),全部成功后调真 `async fn`,返回值 `into_response` 拼成 `Response`——以及为什么"最后一个参数才能消费 body"是 sound 的(body 是 `Stream`,只能被消费一次);
> 4. `HandlerService` 怎么把 `Handler` 包成 `tower::Service`(承 P1-03 一句带过),`Handler::layer` 怎么把一个 Tower Layer 套到单个 handler 上(`Layered` 结构体),以及 `HandlerService::poll_ready` 为什么无条件 Ready(注释原话:"async functions which are always ready")。
>
> 本章是"**提取与响应**"这一面的地基招牌。从这里开始,你才真正看到 axum 怎么用 Rust 的泛型 + 宏展开,把"任意 async fn"变成"满足 Service trait 的对象"——这是 axum 整个声明式人体工学的技术底座。
>
> **写给谁读**:你写过 `async fn handler(State(s): State<AppState>, Path(id): Path<i32>) -> String`,把它塞进 `get(handler)`,跑得起来,但你讲不清:`handler` 这个 `async fn` 凭什么能传给 `get`?它有没有"实现某个 trait"?为什么参数顺序变了编译器就报一个看不懂的 trait bound 错?为什么最多只能 16 个参数?这一章治这些"会用没懂"。
>
> **前置衔接**:从 P1-03(Router/Route 都是 Service)、P1-04(State 用泛型编码缺状态)接过来。前两章你看到 `Router<()>` impl `Service`,`call` 调 `call_with_state(req, ())`,一路下发到 `MethodRouter` 选 handler。但"handler 凭什么是个能被调用的东西"这个问题一直悬着——本章就是来填这个洞的。
>
> **逃生阀(读不下去怎么办)**:本章是全书最难、信息密度最大的一章,宏展开 + 类型系统 + 孤儿规则三件事缠在一起。如果一时绕不开,记住三句话就够——
>
> **① `Handler<T, S>` 的 `T` 是个占位 tuple,作用是让宏能为不同参数个数的 async fn 各写一份 `impl`,绕开"对同一类型多次 impl 同一 trait"的禁令;② 宏对 0~16 参数各展开一份 impl,每份里前面的参数用 `FromRequestParts`(可重复跑、只读 parts),最后一个用 `FromRequest`(消费 body、只能一次);③ `call` 内部把 req 拆 parts+body、按参数顺序逐个提取、最后调真 fn,任一步失败短路返回 Rejection。**
>
> 带着这三句话跳到第二节看 trait 签名、第四节看宏展开、第五节看提取器链状态机,再回头读主线。如果你只想懂"为什么我写个 async fn 就能当 handler",直接跳到技巧精解看 T 参数那节。本章处处承《hyper》P1-02、《Tower》P0-01,读过收获翻倍,但不是硬性前提。

---

## 一句话点破

> **axum 的 Handler trait 不是"你给 handler 实现的 trait",而是"axum 用宏替你的 async fn 实现的 trait"。`Handler<T, S>` 的 `T` 是一个 coherence 占位 tuple——它把"这个 handler 有几个参数"编码进类型,让宏能为 0~16 个参数各写一份 `impl Handler for F`(F 是你的 async fn 闭包类型),而每一份 impl 的 trait 头(`Handler<(M, T1, T2, ...), S>`)类型不同,从而绕开 Rust 孤儿规则。每份 impl 的 `call` 把 req 拆 parts+body,逐个跑 `FromRequestParts`(只读 parts)、最后一个跑 `FromRequest`(消费 body),全部成功才调真 fn。这套机制是纯编译期的——零运行时开销,你的 async fn 单态化成一个独一无二的实现类型,axum 不为每个 handler 付任何虚分派成本。**

这是结论。本章倒过来拆三件事:① trait 签名为什么长那样(`T` 和 `S` 各是什么);② 两个宏怎么用 16 行手写展开 17 份 impl;③ `call` 内部的提取器链状态机怎么保证 sound(不重复消费 body、编译期保证顺序、类型推断不爆)。

---

## 第一节:从 MethodRouter 选了 handler 说起

### 提问

回到 P1-03/P1-04 的全景:hyper accept 一个连接 → 跑 HTTP 协议机 → 把解析好的 `Request` 交给 `Router<()>`(一个 `tower::Service`)→ `Router::call` 调 `call_with_state(req, ())` → `PathRouter::call_with_state` 用 matchit 字典树匹配路径拿 `RouteId` → 索引 `Vec<Endpoint>` 拿到 `MethodRouter` → `MethodRouter::call_with_state` 按 HTTP method 选 handler。

注意那个 "选 handler"——`MethodRouter` 按方法选出的是个什么东西?它怎么被调用?这就是本章的入口。

来看 `MethodRouter` 的核心数据结构(简化示意,完整在 `axum/src/routing/method_routing.rs`,承 P2-06 详拆):

```text
(简化示意,非源码原文)
struct MethodRouter<S> {
    get: Option<MethodEndpoint<S>>,
    post: Option<MethodEndpoint<S>>,
    // ... 其他 HTTP method ...
    fallback: Fallback<S>,
}
```

每个 HTTP method 对应一个 `Option<MethodEndpoint<S>>`。你写 `get(handler_a)` 时,`handler_a` 被存进 `get` 那个槽。请求来了,`MethodRouter::call_with_state` 查 `req.method()`,选对应的槽位,调它的 `call`。

可问题来了:**`handler_a` 是个 `async fn`,它的类型是闭包生成的某个独一无二的类型(比如 `fn(AppState, i32) -> impl Future<Output = String>`),它根本没 `impl Service`,也没有一个固定的 `call(&mut self, req: Request) -> Future` 方法**。它有的是 `fn handler_a(state: AppState, id: i32) -> impl Future<Output = String>`,参数列表是 `(AppState, i32)`,跟 `Request` 八竿子打不着。

那 axum 怎么调它?

> **承接《hyper》/《Tower》**:`Service` trait 的签名是 `call(&mut self, req: Request) -> Future<Output = Result<Response, Error>>`(带 `poll_ready`),承《Tower》P0-01、《hyper》P1-02,本章一句带过。`Service::call` 的签名塞不下"额外传 state"(承 P1-03 的 `call_with_state` 那节),所以 axum 的 Router 内部有个 `call_with_state` 私有方法。问题是 handler 这一层连 Service 都没 impl——它只是个 async fn。

### 不这样会怎样:朴素地要求用户手写 impl Service

假设 axum 不做任何"编译期魔法",要求每个 handler 显式实现 `Service<Request>`。那 hello-world 长这样:

```rust
// 假想的"朴素 axum"(非真实 axum 写法)
struct GetUserHandler { state: AppState }

impl Service<Request> for GetUserHandler {
    type Response = Response;
    type Error = Infallible;
    type Future = BoxFuture<'static, Result<Response, Infallible>>;

    fn poll_ready(&mut self, _: &mut Context<'_>) -> Poll<Result<(), Self::Error>> {
        Poll::Ready(Ok(()))
    }

    fn call(&mut self, req: Request) -> Self::Future {
        let state = self.state.clone();
        Box::pin(async move {
            // 手写:从 req.uri().path() 解析 id
            // 手写:state.get_user(id).await
            // 手写:把结果 into_response
            todo!()
        })
    }
}

// 然后注册
Router::new().route("/users/{id}", get_service(GetUserHandler { state }))
```

这正是 actix-web 早期、几乎所有"trait-based handler"框架让你干的活。问题立刻显现:

1. **样板爆炸**:每个 handler 都要写一个 struct + 一坨 `impl Service`,真正业务逻辑(`get_user(id).await`)被埋在样板里。
2. **参数提取手工化**:`req` 是一个 `Request<Body>`,要拿 path 参数、要消费 body、要解 query——全是手写,容易错(Content-Type 判没判?body 限没限大小?字段名拼对了?)。
3. **类型擦不掉**:如果你想在不同路由注册不同的 handler,每个 handler 是不同类型,你存不进同一张路由表(承 P1-03 的 `Route = BoxCloneSyncService` 类型擦除,这里一句带过)。

axum 想做的事,是让你**只写 `async fn`,参数自动从 Request 提、返回值自动变 Response,框架替你把 fn 包成 Service**。这就需要解决三个问题:

- **问题 A**:怎么让"任意参数个数、任意参数类型"的 async fn,自动实现一个统一的 trait(`Handler`)?
- **问题 B**:怎么把 Request 自动拆开,把参数填进 fn 的参数列表(提取器链)?
- **问题 C**:怎么把这个 `Handler` 包成 `tower::Service`,让 Router/MethodRouter 能像调普通 Service 一样调它?

这一章按 A → B → C 的顺序拆。A 是最硬核的(技巧精解专讲),B 是 `call` 内部的提取器链状态机(第五节),C 是 `HandlerService`(第六节)。

> **钉死这件事**:axum 的"声明式 handler"不是 syntactic sugar,是**编译期类型系统魔法**——它替你的 async fn 在编译期生成一份 `impl Handler for F`(F 是你的 fn 闭包类型),里面把 Request 拆解成参数、调 fn、把返回值拼成 Response。这一切零运行时开销(单态化),你写的 async fn 不付任何虚分派成本。这套魔法的核心是 `Handler<T, S>` trait 的 `T` 参数 + `impl_handler!`/`all_the_tuples!` 宏展开,下面逐层拆。

---

## 第二节:Handler<T, S> trait 的签名逐行拆

### 提问

先看 `Handler` trait 长什么样。它的定义在 `axum/src/handler/mod.rs#L148-L205`,逐字摘录关键部分:

```rust
// axum/src/handler/mod.rs#L145-L205(逐字摘录,关键部分)
#[diagnostic::on_unimplemented(note = "Consider using `#[axum::debug_handler]` to improve the error message")]
pub trait Handler<T, S>: Clone + Send + Sync + Sized + 'static {
    /// The type of future calling this handler returns.
    type Future: Future<Output = Response> + Send + 'static;

    /// Call the handler with the given request.
    fn call(self, req: Request, state: S) -> Self::Future;

    /// Apply a [`tower::Layer`] to the handler.
    fn layer<L>(self, layer: L) -> Layered<L, Self, T, S>
    where
        L: Layer<HandlerService<Self, T, S>> + Clone,
        L::Service: Service<Request>,
    {
        // 默认实现(略)
    }

    /// Convert the handler into a [`Service`] by providing the state
    fn with_state(self, state: S) -> HandlerService<Self, T, S> {
        HandlerService::new(self, state)
    }
}
```

先盯住三件事:trait 头的 `<T, S>`、supertrait 约束、`call` 的签名。逐个拆。

### `<T, S>`:两个类型参数各是什么

trait 头是 `pub trait Handler<T, S>`。**注意 `T` 和 `S` 都没有默认值**——不像 `FromRequest<S, M = ViaRequest>` 那样有默认模式参数。`Handler<T, S>` 是两个裸泛型参数,用户写代码时永远不需要写 `Handler<...>`,这两个参数是给**宏展开的 impl 块**用的。

**`S` 是状态类型**。它和 `Router<S>` 的 `S`(承 P1-04)、`HandlerService<H, T, S>` 的 `S` 是同一个东西——handler 处理请求时需要的 state。`call` 的第二个参数就是 `state: S`。例如,如果你用 `Router<AppState>` 起路由,handler 的 `State<AppState>` 提取器最终拿到的 state 就是 `AppState`,这个 handler 实现的 `Handler<T, AppState>` 的 `S` 就是 `AppState`。

`S` 的真实角色是:让 `Handler::call` 能拿到 state,然后 `State<T>` 提取器在提取器链里从 `state: &S` 拿值(承 P1-04 的 `State` 提取器、`FromRef` 派生)。这一点本章第五节详拆。

**`T` 是 coherence 占位 tuple**。这是本章最硬核、最容易讲混的点,技巧精解会专门拆透。这里先建立直觉:`T` 不是 handler 的某个具体类型,它是一个**占位符**——它的存在让宏能为"不同参数个数的 async fn"各写一份 impl,而每一份 impl 的 trait 头(类型签名)不同,从而绕开孤儿规则(对同一类型不能多次 impl 同一 trait)。

先看几个真实的 T 长什么样:

- 0 参数的 async fn(如 `async fn health() -> &'static str`),T 是 `((),)`——一个单元素 tuple,里面是 unit。
- 1 参数的 async fn(如 `async fn h(State<AppState>) -> String`),T 是 `(M, T1)`——一个 2 元 tuple,`M` 是个 marker(后面解释),`T1` 是那个参数的类型。
- 3 参数的 async fn,T 是 `(M, T1, T2, T3)`——一个 4 元 tuple。

为什么 T 这么"奇怪"?这要在第三节专门拆。这里你先记住:**T 的形状(tuple 长度)编码了"这个 handler 有几个参数",让宏展开的每份 impl 有一个独一无二的 trait 头类型**。

### supertrait 约束:`Clone + Send + Sync + Sized + 'static`

`Handler` 的 supertrait 是 `Clone + Send + Sync + Sized + 'static`,五个约束都有用:

- **`Clone`**:Handler 要能被 Clone。为什么?因为 Router 可能要把 handler 包进 `BoxCloneSyncService`(承 P1-03 类型擦除),而那个装箱要求 Service 能 Clone(每来一个请求,Router 要 Clone 一份 handler 处理)。注意 `async fn` 闭包类型天生 `Copy + Clone`(它是零捕获的 fn pointer 等价物),这个约束自动满足。
- **`Send + Sync`**:多线程 Tokio 运行时,handler 要能跨线程共享和发送。同样,`async fn` 闭包自动满足。
- **`Sized`**:为了让 `Handler` 能作为 `Self` 在 `call(self, ...)` 里按值消费(handler 被调一次后即消费,符合"每个请求 Clone 一份再调"的语义)。
- **`'static`**:handler 不能借用任何非 'static 的数据(因为请求处理 future 要能在任意时刻被 spawn,不能带非 'static 借用)。

> **承接《Tower》**:Service trait 本身**不要求** Clone/Send/Sync(承《Tower》P0-01)。axum 在 `Handler` 这一层加这些约束,是因为框架内部要用 `BoxCloneSyncService` 把 handler 类型擦除(承 P1-03),那个装箱要求 Service 是 `Clone + Send + Sync`。所以约束是"axum 的路由分发要求"逼出来的,不是 Service trait 本身要的。

### `call(self, req: Request, state: S)`:签名为什么这么怪

`call` 的签名是 `fn call(self, req: Request, state: S) -> Self::Future`。三个反直觉的点:

1. **`self` 按值消费,不是 `&self`/`&mut self`**:这跟 `tower::Service::call(&mut self, req)` 不一样!为什么?因为 handler 在 axum 里是"每个请求 Clone 一份,调一次,然后扔掉"。`call` 消费 `self`,符合这个语义——你不会两次调同一个 handler 实例,Router 每次 Clone 一个新实例调它。
2. **第二个参数是 `req: Request`,不是 `&Request`**:因为 handler 要消费 req(拆成 parts + body 提取参数),所以按值传。
3. **第三个参数是 `state: S`,不是 `&S`**:因为 state 也是按值传给 handler 的(`call_with_state(req, state: S)` 承 P1-03)。提取器链里 `from_request_parts(parts, &state)` 用 `&state` 借用,但 state 本身按值流过整个分发链。

**关键对比**:Handler trait 和 tower::Service trait 的 `call` 签名**不一样**。Service 是 `fn call(&mut self, req: Request) -> Future`,Handler 是 `fn call(self, req: Request, state: S) -> Future`。两个差异:`self` vs `&mut self`(Handler 按 self 消费,Service 借用 mut);Handler 多了个 `state: S` 参数。

为什么不一样?因为 Handler 是 axum **内部的** trait,它知道 handler 需要 state;而 Service 是 Tower/hyper 的**外部** trait,签名钉死,不能塞 state。axum 的解法是:**Handler 在内,Service 在外**——`HandlerService` 包住 Handler,对外实现 `Service::call(&mut self, req)`,对内调 `Handler::call(self, req, state)`(state 从 `HandlerService.state` 字段拿)。这个包装在第六节详拆。

### 关联类型 `Future: Future<Output = Response> + Send + 'static`

`type Future` 的 Output 是 `Response`(不是 `Result<Response, Error>`)。这又是个 axum 的取舍:handler 的 future 直接产 `Response`,不产 Result——因为 handler 内部任何错误(提取器失败、业务 panic)都被转成 Response(404/400/500 都是一个正常的 Response)。这跟 `Router::Service::Error = Infallible` 是一体两面(承 P1-03、P5-18 错误处理章)。

`Send + 'static`:future 要能跨线程 spawn、不持有非 'static 借用。

### 两个默认方法:`layer` 和 `with_state`

`Handler` trait 有两个带默认实现的**非核心**方法:

**`with_state(self, state: S) -> HandlerService<Self, T, S>`**:把 handler 包成 `HandlerService`(同时注入 state)。这是 handler 变 Service 的入口,第六节详拆。注意它消费 `self`——一旦调 `with_state`,handler 就被搬进了 HandlerService,原 handler 不复存在。

**`layer<L>(self, layer: L) -> Layered<L, Self, T, S>`**:给单个 handler 套一个 Tower Layer。这个返回的 `Layered` 也是一个 Handler(它 impl Handler),所以你还可以再 `.layer(...)`,或者最后 `.with_state(state)` 变 Service。`Handler::layer` 是"给一条路由的 handler 单独加中间件"的入口,和 `Router::layer`(全局)、`MethodRouter::layer`(同路径所有 method)、`route_layer`(只匹配路由不含 fallback)是四种作用域(承 P4-16 详拆)。

> **钉死这件事**:`Handler<T, S>` trait 的设计有三个要点:① **T 是占位 tuple**(下节专讲),② **S 是 state 类型**(承 P1-04),③ **call 签名是 `(self, req, state)` 不是 Service 的 `(&mut self, req)`**——Handler 是 axum 内部 trait,知道 state;Service 是外部 trait,不知道 state,中间靠 `HandlerService` 适配。整个 axum 的 handler 抽象,建立在这三个要点之上。

---

## 第三节:T 参数——为什么是个 tuple 占位符

### 提问

第二节说 `T` 是占位 tuple,但还没讲清楚"为什么是 tuple、占什么位"。这一节专门拆这个——它是 axum 最反直觉、也最巧的一个设计,不讲清这个等于没讲 axum。

回到一个朴素的问题:axum 想给"0~16 个参数的 async fn"全部实现 `Handler`。也就是说,对每个参数个数 N(0 ≤ N ≤ 16),都要有一份:

```rust
impl<F, Fut, S, Res, T1, T2, ..., TN> Handler<???> for F
where F: FnOnce(T1, T2, ..., TN) -> Fut + ...,
      Fut: Future<Output = Res> + Send,
      ...
{
    fn call(self, req: Request, state: S) -> Self::Future { ... }
}
```

问题是 `???` 这个位置填什么?

### 不这样会怎样:朴素地写 `impl Handler<()> for F` 会撞墙

假设 axum 偷懒,所有参数个数都写 `impl Handler<()> for F`(T 永远是 unit)。那编译器立刻报错:

```text
error[E0119]: conflicting implementations of trait `Handler<(), S>` for type `F`
   --> 1 参 impl: impl<F, ...> Handler<(), S> for F where F: FnOnce(T1) -> Fut, ...
   --> 2 参 impl: impl<F, ...> Handler<(), S> for F where F: FnOnce(T1, T2) -> Fut, ...
note: conflicting implementation in crate `core`
```

为什么报错?因为 Rust 的孤儿规则(coherence 规则)要求:**对同一个 `(Type, Trait)` 组合,只能有一份 impl**。`Handler<(), S> for F` 这个组合,只要 F 同时满足"FnOnce(T1)->Fut"和"FnOnce(T1, T2)->Fut"两个 impl 的约束(实际上同一个 F 不可能同时满足,但 Rust 的 trait resolver 不会去做这种"不可同时满足"的推理,它只看 impl 头部的 trait+类型签名),编译器就认为它们"潜在冲突",直接拒绝。

> **承接 Rust 类型系统**:孤儿规则(coherence)是 Rust 保证"两个 crate 不会对同一类型实现同一 trait 引发冲突"的机制。它的核心:对 `impl Trait for Type`,要么 Trait 在你 crate 里,要么 Type 在你 crate 里(否则孤儿)。两个 impl 头部的"类型签名"相同(trait 一样、type 一样),就被认为冲突,即使约束不重叠。这是 Rust 类型系统的硬规矩,axum 必须绕开它,才能为不同参数个数各写一份 impl。

更深的麻烦:即使你能说服编译器"这两个 impl 不会同时满足"(它不信),还有第二个问题——**类型推断会爆**。用户写 `async fn h(s: State<X>, id: i32) -> String`,你写 `impl Handler<()> for F`,编译器看到一个 `F: FnOnce(...)` 的约束,但它**没有任何信息**告诉它"这个 F 的参数列表是 `(State<X>, i32)`"——因为 `()` 这个 T 类型不携带参数列表信息。编译器要靠"试每个 impl 的约束能否匹配"来推断,在 17 份 impl 全都写 `Handler<()>` 的情况下,推断会非常慢、且报错信息会极其糟糕(一堆 `FnOnce` 约束失败,用户根本看不懂)。

### 所以 axum 这么设计:T 把参数列表"编进类型"

axum 的解法:让 T **携带参数列表信息**。具体来说,T 是一个 tuple,长度等于"参数个数 + 1"(多一个 marker `M` 在第一位,后面解释),每个元素对应一个参数的类型。

来看真实的 T(逐个对照源码,0 参数的 `T = ((),)` 在 `mod.rs#L208`,N 参数的 `T = (M, T1, ..., TN)` 在 `mod.rs#L227`):

| 参数个数 | 用户写的 async fn | 宏展开的 T |
|----------|-------------------|-----------|
| 0 | `async fn h() -> &'static str` | `((),)` |
| 1 | `async fn h(State<X>) -> String` | `(M, T1)` |
| 2 | `async fn h(State<X>, Path<i32>) -> String` | `(M, T1, T2)` |
| 3 | `async fn h(State<X>, Path<i32>, Json<Body>) -> String` | `(M, T1, T2, T3)` |
| ... | ... | ... |
| 16 | `async fn h(t1, t2, ..., t16) -> R` | `(M, T1, T2, ..., T16)` |

(注意 0 参数是 `((),)`——一个单元素 tuple 里面是 unit,不是裸 `()`。这是为了和"1 参数的 T = (M, T1)"保持"至少有一个元素"的结构一致性,后面会讲为什么。)

现在看宏展开的 impl 头(`mod.rs#L227`):

```rust
impl<F, Fut, S, Res, M, $($ty,)* $last> Handler<(M, $($ty,)* $last,), S> for F
where
    F: FnOnce($($ty,)* $last,) -> Fut + ...,
    ...
```

`Handler<(M, T1, T2, ..., TN,), S>`——这个 trait 头的 T 是 `(M, T1, ..., TN)`,每个不同的 N 都对应一个**不同的 tuple 类型**。Rust 的孤儿规则这下满意了:每份 impl 的 trait 头类型签名不同(`Handler<(M, T1), S> for F` vs `Handler<(M, T1, T2), S> for F`),它们不冲突。

而且类型推断也顺了:用户写 `async fn h(s: State<X>, id: i32)`,编译器看到 `F` 是这个 fn 的类型,它要找一份 `impl Handler<???, S> for F`。它会试着匹配每一份 impl 的约束——`Handler<(M, T1), S>` 要求 `F: FnOnce(T1) -> Fut`,`Handler<(M, T1, T2), S>` 要求 `F: FnOnce(T1, T2) -> Fut`,以此类推。`F` 是 `FnOnce(State<X>, i32) -> ...`,只有 2 参数那份 impl 能匹配,编译器顺利推断出 `T1 = State<X>`,`T2 = i32`,T 整体是 `(M, State<X>, i32)`。

**这就是 T 的本质:把 handler 的参数列表编进 trait 的类型参数,让每份 impl 有独一无二的签名,既绕开孤儿规则,又给编译器类型推断提供充足信息。**

### 那个 `M` 是什么?——`FromRequest` 的 marker

细心的你可能注意到:T 的第一个元素是 `M`,不是参数类型。这个 `M` 是 `FromRequest` 的第二个类型参数(`FromRequest<S, M = ViaRequest>`,承 P3-10 详拆),用来区分"最后一个参数是 ViaRequest(消费 body)还是 ViaParts(只读 parts)"。

为什么 M 要在 T 里?因为宏生成的 impl 里,**最后一个参数**用 `FromRequest<S, M>`(`mod.rs#L234`),这个 M 是泛型的——编译器要根据最后一个参数的类型推断出 M 是 `ViaRequest`(默认)还是 `ViaParts`(通过桥接 impl `impl FromRequest<S, ViaParts> for T where T: FromRequestParts` 获得的)。M 在 T 里,让"推断出 M"和"推断出整个 T"是同一个推断过程,一举两得。

> **承接 P3-10**:`FromRequest<S, M>` 的 `M` marker、`ViaRequest`/`ViaParts` 二元划分、桥接 impl 的细节,本章只用、不深拆——深度留 P3-10(FromRequest/FromRequestParts 二元划分招牌章)。这里你只需知道:M 是 `FromRequest` 的模式参数,T 把它放在第一位(在参数类型之前)。

### 0 参数为什么是 `((),)` 而不是 `()`

这是个容易看走眼的细节。来看 0 参数的手写 impl(`mod.rs#L207-L219`):

```rust
// axum/src/handler/mod.rs#L207-L219(逐字摘录)
#[diagnostic::do_not_recommend]
impl<F, Fut, Res, S> Handler<((),), S> for F
where
    F: FnOnce() -> Fut + Clone + Send + Sync + 'static,
    Fut: Future<Output = Res> + Send,
    Res: IntoResponse,
{
    type Future = Pin<Box<dyn Future<Output = Response> + Send>>;

    fn call(self, _req: Request, _state: S) -> Self::Future {
        Box::pin(async move { self().await.into_response() })
    }
}
```

注意 L208 是 `Handler<((),), S>`,不是 `Handler<(), S>`。`((),)` 是"一个单元素 tuple,元素是 unit";`()` 是"裸 unit"。这两者**是不同类型**——`((),)` 的 `TypeId` 和 `()` 的 `TypeId` 不一样。

为什么 axum 选 `((),)` 而不是 `()`?为了让"0 参数"的 T 和"N 参数"的 T 结构一致:

- N 参数(N≥1)的 T 是 `(M, T1, ..., TN)`——至少 2 个元素(因为 M 一定在)。
- 0 参数的 T 如果是 `()`,那就是裸 unit,和 N 参数的"tuple"结构不一致。

axum 选了 `((),)`——一个 1 元 tuple。这样所有 T 都是 tuple(没有裸 unit),内部宏处理时不需要特殊分支。这个选择是品味问题,但它让宏的实现更干净(0 参数的 impl 单独手写,不用走宏)。

而且更重要的:0 参数的 impl **是单独手写的,不走 `impl_handler!` 宏**(看 `mod.rs#L207-L219`,它直接写 impl,没宏调用)。原因是宏的签名 `[$($ty:ident),*], $last:ident` 要求至少有一个 `$last`,0 参数没有 `$last`,宏无法表达。所以 0 参数手写,N≥1 参数走宏。这个分工在第四节详拆。

### 一个完整对照表:T、M、参数列表、impl 来源

| N | 用户 async fn 签名 | 宏展开的 T | M 推断为 | impl 来源 |
|---|--------------------|-----------|---------|----------|
| 0 | `async fn() -> R` | `((),)` | (无 M) | 手写 `mod.rs#L207-L219` |
| 1 | `async fn(T1) -> R` | `(M, T1)` | ViaRequest/ViaParts | 宏 `mod.rs#L221-L260` |
| 2 | `async fn(T1, T2) -> R` | `(M, T1, T2)` | ViaRequest/ViaParts | 宏 |
| ... | ... | ... | ... | 宏 |
| 16 | `async fn(T1, ..., T16) -> R` | `(M, T1, ..., T16)` | ViaRequest/ViaParts | 宏 |

> **钉死这件事**:`Handler<T, S>` 的 T 是个 tuple,长度 = 参数个数 + 1(0 参数是 `((),)` 一个特例)。第一个元素是 `M`(`FromRequest` 的模式 marker,推断为 `ViaRequest` 或 `ViaParts`),后面是参数类型列表。T 的形状(tuple 长度)编码了"参数个数",让宏展开的每份 impl 有独一无二的 trait 头,绕开孤儿规则。这是 axum 最巧的一笔,也是最容易看不懂的一笔。技巧精解会再拆一次。

---

## 第四节:impl_handler! + all_the_tuples! 两个宏怎么展开 0~16 参数

### 提问

第三节讲了 T 是占位 tuple,现在来看那 17 份 impl(N=0 手写,N=1~16 宏展开)具体怎么生成。这涉及两个宏:`impl_handler!`(宏规则,定义一份 impl 的模板)和 `all_the_tuples!`(元宏,调用 `impl_handler!` 16 次,每次给不同的参数列表)。

### `impl_handler!` 宏的定义逐行拆

`impl_handler!` 的定义在 `axum/src/handler/mod.rs#L221-L260`,逐字摘录:

```rust
// axum/src/handler/mod.rs#L221-L260(逐字摘录)
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
```

逐段拆这个宏:

**宏签名 `[$($ty:ident),*], $last:ident`**(L222-L224):这个宏接收两个参数——一个 ident 列表 `$($ty:ident),*`(可能是空),和一个 ident `$last`。例如 `impl_handler!([], T1)` 表示"0 个 `$ty`,$last = T1"(对应 1 参数 handler);`impl_handler!([T1, T2], T3)` 表示"2 个 `$ty` = T1, T2,$last = T3"(对应 3 参数 handler)。

**`#[diagnostic::do_not_recommend]`**(L225):这是 Rust 1.78+ 的一个诊断属性,告诉编译器"当 trait 解析失败时,不要推荐这个 impl 给用户"(因为它会让错误信息更糟)。这是 axum 用来改善"用户 handler 类型错"时错误信息的手段之一(承 P3-13 `#[axum::debug_handler]` 详拆)。

**`#[allow(non_snake_case, unused_mut)]`**(L226):宏展开后,`$ty` 和 `$last` 会作为变量名出现(如 `let T1 = ...`),它们不是 snake_case,所以 `non_snake_case` lint 要 allow。`unused_mut` 是因为 `let mut parts` 在某些参数个数下可能没被用(0 个 FromRequestParts 时),要 allow。

**impl 头 `Handler<(M, $($ty,)* $last,), S> for F`**(L227):这就是第三节讲的 T tuple。`$($ty,)*` 展开成 `T1, T2, ...` 后面跟逗号,`$last` 是最后一个。整个 T 是 `(M, T1, T2, ..., TN)`,逗号结尾(`(M, T1, T2, ...,)`)——Rust 允许 tuple 末尾逗号。

**约束块**(L228-L234):

```rust
where
    F: FnOnce($($ty,)* $last,) -> Fut + Clone + Send + Sync + 'static,
    Fut: Future<Output = Res> + Send,
    S: Send + Sync + 'static,
    Res: IntoResponse,
    $( $ty: FromRequestParts<S> + Send, )*
    $last: FromRequest<S, M> + Send,
```

五条约束:

1. **`F: FnOnce(...)`**(L229):F 是用户那个 async fn 的闭包类型。`FnOnce($($ty,)* $last,) -> Fut`——参数列表是宏展开的参数,返回类型是 Fut(future)。后面跟 `+ Clone + Send + Sync + 'static`,这是 `Handler` 的 supertrait 在 trait bound 上的体现(承第二节)。
2. **`Fut: Future<Output = Res> + Send`**(L230):Fut 是 future,产 `Res`(handler 的返回类型),要 Send。
3. **`S: Send + Sync + 'static`**(L231):state 类型约束。
4. **`Res: IntoResponse`**(L232):返回值要能转 Response(承 P3-12 详拆 IntoResponse)。
5. **`$( $ty: FromRequestParts<S> + Send, )*`**(L233):**关键!** 对前面所有 `$ty`(前 n-1 个参数),要求它们实现 `FromRequestParts<S>`——只读 parts 的提取器(可重复跑,因为只 `&mut Parts`)。
6. **`$last: FromRequest<S, M> + Send`**(L234):**关键!** 对最后一个参数 `$last`,要求它实现 `FromRequest<S, M>`——可消费 body 的提取器(只能跑一次,因为 body 是 Stream)。

**这个"前 n-1 个 FromRequestParts、最后一个 FromRequest"的二元划分,是 axum 提取器链 sound 的核心**——body 只能被消费一次,所以只有最后一个参数能消费 body,前面的参数只能从 parts(请求头、URI、method、extensions 等不涉及 body 的部分)提取。这个二元划分的深度拆留 P3-10,本章只用。

**`type Future = Pin<Box<dyn Future<Output = Response> + Send>>`**(L236):Handler::Future 是个装箱的动态 future。为什么装箱?因为 `call` 内部是个 `async move` 块(下面 L240),它的 future 类型是编译器生成的匿名类型,无法在 trait 关联类型里命名,只能 `Box::pin` 擦除。这是 trait 里用 async block 的标准做法(RPITIT 之前的标准模式),承第七节演进史。

**`fn call` 内部**(L238-L257):这是核心,提取器链状态机,第五节专拆。这里先看大结构:

```rust
fn call(self, req: Request, state: S) -> Self::Future {
    let (mut parts, body) = req.into_parts();    // L239 拆 parts+body
    Box::pin(async move {
        // L241-L246: 前 n-1 个参数,逐个 from_request_parts
        $(
            let $ty = match $ty::from_request_parts(&mut parts, &state).await {
                Ok(value) => value,
                Err(rejection) => return rejection.into_response(),
            };
        )*

        // L248: 重组 req(把没用完的 parts 和原 body 拼回)
        let req = Request::from_parts(parts, body);

        // L250-L253: 最后一个参数 from_request(消费 body)
        let $last = match $last::from_request(req, &state).await {
            Ok(value) => value,
            Err(rejection) => return rejection.into_response(),
        };

        // L255: 全部成功,调真 fn
        self($($ty,)* $last,).await.into_response()
    })
}
```

五步:拆 → 逐个 parts 提取 → 重组 → last 提取(消费 body)→ 调 fn。失败任一步短路 `return rejection.into_response()`(把 Rejection 转成 Response 返回)。第五节详拆这个状态机。

### `all_the_tuples!` 宏:手写 16 行调用

`impl_handler!` 只定义了模板,要生成 16 份 impl(N=1 到 N=16),需要调用它 16 次,每次传不同的参数列表。这就是 `all_the_tuples!` 干的事。

`all_the_tuples!` 定义在 `axum/src/macros.rs#L48-L68`,逐字摘录:

```rust
// axum/src/macros.rs#L48-L68(逐字摘录)
#[rustfmt::skip]
macro_rules! all_the_tuples {
    ($name:ident) => {
        $name!([], T1);
        $name!([T1], T2);
        $name!([T1, T2], T3);
        $name!([T1, T2, T3], T4);
        $name!([T1, T2, T3, T4], T5);
        $name!([T1, T2, T3, T4, T5], T6);
        $name!([T1, T2, T3, T4, T5, T6], T7);
        $name!([T1, T2, T3, T4, T5, T6, T7], T8);
        $name!([T1, T2, T3, T4, T5, T6, T7, T8], T9);
        $name!([T1, T2, T3, T4, T5, T6, T7, T8, T9], T10);
        $name!([T1, T2, T3, T4, T5, T6, T7, T8, T9, T10], T11);
        $name!([T1, T2, T3, T4, T5, T6, T7, T8, T9, T10, T11], T12);
        $name!([T1, T2, T3, T4, T5, T6, T7, T8, T9, T10, T11, T12], T13);
        $name!([T1, T2, T3, T4, T5, T6, T7, T8, T9, T10, T11, T12, T13], T14);
        $name!([T1, T2, T3, T4, T5, T6, T7, T8, T9, T10, T11, T12, T13, T14], T15);
        $name!([T1, T2, T3, T4, T5, T6, T7, T8, T9, T10, T11, T12, T13, T14, T15], T16);
    };
}
```

注意几个关键点:

1. **不是递归,是手写 16 行**:很多人以为 axum 用了递归 macro_rules,实际上没有。它就是手写 16 行,每行调一次 `$name!`,传"已累积的 ident 列表"和"下一个新 ident"。这个写法看起来笨,但比递归更可靠(递归 macro_rules 在老 Rust 上限制多)。
2. **第 1 行 `$name!([], T1)`**:0 个 `$ty`,$last = T1——对应 1 参数 handler。
3. **第 16 行 `$name!([T1..T15], T16)`**:15 个 `$ty`,$last = T16——对应 16 参数 handler。
4. **`#[rustfmt::skip]`**(L48):rustfmt 不要格式化这个宏(rustfmt 会把这 16 行重新排列,破坏可读性)。

调用点是 `mod.rs#L262`:`all_the_tuples!(impl_handler);`。这一行展开成 16 次 `impl_handler!{...}` 调用,每次生成一份 `impl Handler<(M, T1, ..., TN), S> for F`。

加上手写的 0 参数 impl(`mod.rs#L207-L219`),axum 一共生成 **17 份 impl**——0 参数 1 份 + 1~16 参数 16 份。这就是为什么 axum handler **最多 16 个参数**(超过会编译错,因为没有 17 参数的 impl)。

> **钉死这件事**:axum 的"任意参数个数 handler"靠两个宏实现——`impl_handler!` 定义模板(一份 impl 怎么生成),`all_the_tuples!` 手写 16 行调用模板(传不同参数列表),展开成 16 份 impl(1~16 参数),外加手写的 0 参数 impl,一共 17 份。这套机制是纯编译期的,运行时零开销——你的 async fn 单态化成一个独一无二的实现类型,17 份 impl 里只有一份(参数个数匹配的那份)被编译器选中,其他 16 份在 trait 解析时被丢弃。

### 手动展开两个例子,看清楚生成的代码长啥样

宏最难懂的地方是"展开后到底长什么样"。来手动展开两个例子。

**例 1:0 参数 handler(手写 impl,`mod.rs#L207-L219`)**:

用户写 `async fn health() -> &'static str`。对应的 impl 已经手写在源码里(`mod.rs#L207-L219`),展开就是它本身:

```rust
// 用户代码
async fn health() -> &'static str { "ok" }

// axum 已经手写的 impl(直接命中)
impl<F, Fut, Res, S> Handler<((),), S> for F
where
    F: FnOnce() -> Fut + Clone + Send + Sync + 'static,
    Fut: Future<Output = Res> + Send,
    Res: IntoResponse,
{
    type Future = Pin<Box<dyn Future<Output = Response> + Send>>;

    fn call(self, _req: Request, _state: S) -> Self::Future {
        Box::pin(async move { self().await.into_response() })
    }
}
```

注意 `call` 内部:**直接调 `self().await.into_response()`**,没有提取器链(因为 0 参数,什么都不提取,req 和 state 都被丢弃 `_req`/`_state`)。这是 0 参数 handler 的特例——最简单的 impl。

**例 2:2 参数 handler(宏展开,`all_the_tuples!(impl_handler)` 第 2 行 `$name!([T1], T2)`)**:

用户写 `async fn h(State<AppState>, Path<i32>) -> String`。`all_the_tuples!` 第 2 行 `$name!([T1], T2)` 展开成 `impl_handler!{ [T1], T2 }`,把 `$ty = [T1]`、`$last = T2` 代入宏模板。展开后:

```rust
#[diagnostic::do_not_recommend]
#[allow(non_snake_case, unused_mut)]
impl<F, Fut, S, Res, M, T1, T2> Handler<(M, T1, T2,), S> for F
where
    F: FnOnce(T1, T2,) -> Fut + Clone + Send + Sync + 'static,
    Fut: Future<Output = Res> + Send,
    S: Send + Sync + 'static,
    Res: IntoResponse,
    T1: FromRequestParts<S> + Send,
    T2: FromRequest<S, M> + Send,
{
    type Future = Pin<Box<dyn Future<Output = Response> + Send>>;

    fn call(self, req: Request, state: S) -> Self::Future {
        let (mut parts, body) = req.into_parts();
        Box::pin(async move {
            // T1 走 FromRequestParts(只读 parts)
            let T1 = match T1::from_request_parts(&mut parts, &state).await {
                Ok(value) => value,
                Err(rejection) => return rejection.into_response(),
            };

            // 重组 req(把没用完的 parts + 原 body 拼回)
            let req = Request::from_parts(parts, body);

            // T2 走 FromRequest(消费 body)
            let T2 = match T2::from_request(req, &state).await {
                Ok(value) => value,
                Err(rejection) => return rejection.into_response(),
            };

            // 全部成功,调真 fn
            self(T1, T2,).await.into_response()
        })
    }
}
```

对照用户代码 `async fn h(State<AppState>, Path<i32>) -> String`,编译器推断出 `T1 = State<AppState>`(实现 `FromRequestParts`,只读 parts)、`T2 = Path<i32>`(通过桥接 impl 实现 `FromRequest<S, ViaParts>`,所以 `M = ViaParts`)。整个 T 推断为 `(ViaParts, State<AppState>, Path<i32>)`。

**注意两个细节**:

1. **变量名是 `T1`/`T2`(不是 snake_case)**:这就是为什么宏要 `#[allow(non_snake_case)]`。宏展开后 `let T1 = ...` 把提取出来的值绑定到名为 `T1` 的变量,这个变量名和类型名相同(`T1` 既是类型参数,又是变量名)。Rust 允许这样(类型和值在不同命名空间),但 lint 会警告 non_snake_case,所以宏显式 allow。
2. **`self(T1, T2,).await.into_response()`**:最后调 `self` 时把提取出来的 `T1`、`T2` 作为参数传进去,`await` 拿到 handler 返回值(Res),再 `into_response()` 转成 `Response`。注意末尾逗号 `self(T1, T2,)`——宏模板里 `$($ty,)* $last,` 每个参数后面都有逗号,允许尾逗号。

**例 3:16 参数 handler(`all_the_tuples!` 最后一行)**:

用户写一个 16 参数的 `async fn`(很少见,但合法)。`all_the_tuples!` 最后一行 `$name!([T1..T15], T16)` 展开成 `impl_handler!{ [T1, T2, ..., T15], T16 }`,生成一份 16 参数的 impl,trait 头是 `Handler<(M, T1, T2, ..., T16,), S>`。这份 impl 里的 `call` 内部会有 15 个 `from_request_parts`(T1 到 T15,逐个提取),然后 1 个 `from_request`(T16,消费 body),最后 `self(T1, T2, ..., T16,).await.into_response()`。

这是 axum 支持的最大参数个数。如果你想写 17 参数的 handler,编译器找不到匹配的 impl,报一个"`F: FnOnce(T1, ..., T17) -> Fut` does not satisfy `Handler<?, S>`"之类的错(实际上错误信息会更乱,因为 trait resolver 会试遍 17 份 impl 都失败)。`#[axum::debug_handler]` 宏(P3-13)能改善这种错误信息。

### 为什么是 16 个参数,不是 32 或 8

这是个工程取舍。Rust 标准库的 tuple 最大 12 个元素(`(T1, ..., T12)`),但 axum 选 16,是为了让大多数实际 handler 能放下。同时 16 是 2 的幂(虽然没有技术原因要求是 2 的幂),`all_the_tuples!` 的 16 行手写也合理可读。

超过 16 参数的 handler 在实践中几乎不存在(handler 参数太多通常是设计问题,应该拆分),所以 axum 选 16 是合理的上限。如果你真需要更多参数,axum 提供了一个逃生阀:**`Vec<T>` 或 tuple 提取器**(用 `(T1, T2)` 作为一个参数,实现 `FromRequestParts`),可以把多个提取器打包成一个,变相突破 16 上限。承 P3-13 自定义提取器章。

> **钉死这件事**:axum 的两个宏——`impl_handler!`(模板)+ `all_the_tuples!`(16 行调用)——展开成 16 份 impl(1~16 参数),外加手写的 0 参数 impl,一共 17 份。每份 impl 的 trait 头 T 不同,绕开孤儿规则。这套机制是纯编译期的,运行时零开销。手动展开 0 参数(直接调 fn,无提取器链)和 2 参数(parts 提取 T1 → body 提取 T2 → 调 fn)两个例子,你就看清了宏展开后生成的代码长什么样——本质是"为每种参数个数各写一份样板",宏只是把样板模板化了。

---

## 第五节:call 内部的提取器链状态机

### 提问

第四节看到 `call` 内部长这样(以 2 参数为例):

```rust
fn call(self, req: Request, state: S) -> Self::Future {
    let (mut parts, body) = req.into_parts();
    Box::pin(async move {
        let T1 = match T1::from_request_parts(&mut parts, &state).await {
            Ok(value) => value,
            Err(rejection) => return rejection.into_response(),
        };

        let req = Request::from_parts(parts, body);

        let T2 = match T2::from_request(req, &state).await {
            Ok(value) => value,
            Err(rejection) => return rejection.into_response(),
        };

        self(T1, T2,).await.into_response()
    })
}
```

这一节拆这个 async block 内部的"提取器链状态机"——为什么这么设计、为什么 sound(不重复消费 body、编译期保证顺序、类型推断不爆)。

### 拆 req:parts + body

第一步 `let (mut parts, body) = req.into_parts();`(`mod.rs#L239`):把 `Request` 拆成 `Parts` 和 `Body`。`http::request::Parts` 包含 method、uri、version、headers、extensions(一个 `http::Extensions`,本质是 TypeMap)——这些是"请求的元数据",不涉及 body。`Body` 是请求体(`axum_core::body::Body`,承 hyper body),本质是字节流的 `Stream`。

为什么要拆?因为 axum 的提取器链需要**分别处理** parts 和 body:

- **`FromRequestParts`** 提取器(`State`、`Path`、`Query`、`HeaderMap`、自定义 parts 提取器等)只看 parts,**不碰 body**。它们的 `from_request_parts(parts: &mut Parts, state: &S)` 接收 `&mut Parts`(可变借用),可以从 parts 读/写(比如 `Path` 把解析的 URL 参数塞进 parts.extensions,`State` 读 state)。多个 `FromRequestParts` 提取器可以**顺序跑**,因为它们只 `&mut Parts`(不消费 parts),parts 可以被反复借用。
- **`FromRequest`** 提取器(`Json<T>`、`Form<T>`、`String`、`Bytes`、自定义 body 提取器)需要 **body**。它们的 `from_request(req: Request, state: &S)` 接收完整的 `Request`(含 body),**按值消费** body。一旦消费,body 就没了,后续提取器拿不到 body。

这个"只读 parts 可多次跑 vs 消费 body 只能一次"的二元划分,是 axum 提取器链 sound 的核心,深度拆留 P3-10,本章只用。

### 逐个 FromRequestParts:前 n-1 个参数

`call` 内部第二步,对前 n-1 个参数(宏模板里 `$($ty),*`),逐个调 `from_request_parts`:

```rust
$(
    let $ty = match $ty::from_request_parts(&mut parts, &state).await {
        Ok(value) => value,
        Err(rejection) => return rejection.into_response(),
    };
)*
```

这是宏的重复展开 `$($(...)+)*`——对每个 `$ty`(T1, T2, ..., T(n-1)),生成一个 `let $ty = match ... { Ok => value, Err => return ... }` 块。

每个块做三件事:

1. **调提取器的 `from_request_parts(&mut parts, &state)`**:把 `&mut parts` 和 `&state` 借给提取器,提取器异步返回 `Result<Self, Rejection>`。注意 `&mut parts` 是可变借用——提取器可以修改 parts(比如 `Path` 把 URL 参数塞进 extensions,`MatchedPath` 把匹配路径塞进去)。
2. **成功取值**:`Ok(value) => value`,把提取出来的值绑定到 `$ty`(名为 T1/T2/.../T(n-1) 的变量)。
3. **失败短路返回**:`Err(rejection) => return rejection.into_response()`,把 Rejection(提取失败)转成 Response(比如 `JsonRejection` 转 400 Bad Request),直接 `return`(短路退出 async block,后面的提取器和 fn 调用都不执行)。

**为什么可以顺序跑多个 FromRequestParts?** 因为它们都 `&mut Parts`(借用 parts,不消费)。第一个提取器 `from_request_parts(&mut parts, ...)` 跑完,parts 还在(只是被修改了),第二个提取器 `from_request_parts(&mut parts, ...)` 可以继续跑——它看到的是第一个提取器修改后的 parts。这就是"提取器链"的实现机制:**每个 FromRequestParts 提取器在前一个的基础上修改 parts,后面的提取器看到累积的修改**。

一个真实的例子:你写 `async fn h(Path(id): Path<i32>, MatchedPath(p): MatchedPath)`。`Path` 提取器跑时,把 URL 参数解析塞进 `parts.extensions`(承 P2-05 matchit 匹配);`MatchedPath` 提取器跑时,从 `parts.extensions` 读出匹配路径。两个都 `FromRequestParts`,顺序跑没问题。

### 重组 req:把没用完的 parts 和 body 拼回

第三步 `let req = Request::from_parts(parts, body);`(`mod.rs#L248`):把 parts 和 body 重新拼成一个 `Request`。

为什么要重组?因为最后一个参数要 `from_request(req, &state)`——它需要的是完整 `Request`(含 body),不是裸 parts+body。这一步是把"被前 n-1 个提取器修改过的 parts"+ "原 body" 拼回 Request,交给最后一个提取器。

注意 parts 已经被前面的提取器修改过了(比如 extensions 里塞了 URL 参数),重组的 req 携带这些修改。所以最后一个提取器(`FromRequest`)看到的是"经过前面提取器处理的 req"——这也是提取器链的"累积修改"语义的一部分。

### 最后一个 FromRequest:消费 body

第四步,对最后一个参数 `$last`,调 `from_request`(不是 `from_request_parts`):

```rust
let $last = match $last::from_request(req, &state).await {
    Ok(value) => value,
    Err(rejection) => return rejection.into_response(),
};
```

关键区别:`from_request(req, &state)` **按值接收 req**(含 body)。提取器消费 body(比如 `Json<T>` 把 body 字节读出来,serde_json 反序列化,然后丢弃 body)。一旦消费,后续没有"再消费一次"的机会——body 已经被读空了。

**为什么只有最后一个参数能 FromRequest?** 因为 body 只能被消费一次。如果允许两个参数都 FromRequest,第二个 FromRequest 拿到的是"已经被第一个消费过的空 body",反序列化失败(serde_json 解析空字节会报 EOF)。axum 的设计是:**最后一个参数才能消费 body,前面的参数只能 FromRequestParts(只读 parts)**。这是 sound 的保证——编译期就钉死,你不可能写出一个"中间参数消费 body"的 handler。

> **承接 P3-10**:`FromRequestParts`(只读 parts)vs `FromRequest`(消费 body)的二元划分、`ViaParts` marker 桥接(让只读 parts 的提取器也能当 FromRequest 用)、`Result<T, Rejection>` 和 `Option<T>` 的提取器自动桥接——这些深度拆留 P3-10。本章只用"最后一个参数 FromRequest、其余 FromRequestParts"这个事实,讲清 Handler trait 这一层怎么把它们串起来。

### 全部成功:调真 fn

第五步 `self($($ty,)* $last,).await.into_response()`(`mod.rs#L255`):所有提取器都成功,把提取出来的值作为参数调真 fn。`await` 拿到 fn 返回的 future,等它产 Res(用户写的 async fn 的返回类型,比如 `String`、`(StatusCode, Json<T>)`、`impl IntoResponse`)。最后 `into_response()`(trait bound `Res: IntoResponse` 保证这步成立)把 Res 转成 Response。

注意这一步**没有任何错误处理**(没有 `match`/`?`)——因为前面的提取器已经把可能的 Rejection 短路返回了,走到这一步说明所有参数都成功。fn 返回的 future 产 Res(用户返回值),`into_response` 把 Res 转 Response。如果 fn 内部 panic,panic 会沿调用栈传上去(被 Tower 的 catch_panic 或 Tokio 的 task panic 处理机制捕获,承 P5-18)。

### 提取器链状态机:mermaid 状态图

把这个过程画成状态图(N=3 参数,前 2 个 FromRequestParts,最后一个 FromRequest):

```mermaid
stateDiagram-v2
    [*] --> 拆req
    拆req --> 提取T1: parts + body
    提取T1 --> 提取T2: T1::from_request_parts OK
    提取T1 --> 返回Rejection: T1 失败
    提取T2 --> 重组req: T2::from_request_parts OK
    提取T2 --> 返回Rejection: T2 失败
    重组req --> 提取T3: parts(已修改) + body
    提取T3 --> 调fn: T3::from_request OK
    提取T3 --> 返回Rejection: T3 失败
    调fn --> into_response: fn() .await 产 Res
    into_response --> [*]: Response
    返回Rejection --> [*]: rejection.into_response()
```

状态图的几个关键转换:

- **拆 req → 提取 T1**:`req.into_parts()` 拆出 parts+body,进入提取器链。
- **提取 T1 → 提取 T2**(T1 成功):T1 是 `FromRequestParts`,只 `&mut parts`,parts 还在,继续提取 T2。
- **提取 T1 → 返回 Rejection**(T1 失败):短路,`return rejection.into_response()`,退出 async block。
- **重组 req**:前 n-1 个 FromRequestParts 都成功,把修改过的 parts + body 拼回 Request。
- **提取 T3**(最后一个,FromRequest):`from_request(req, &state)` 消费 body。
- **调 fn**:T3 成功,把 T1、T2、T3 作为参数调 `self(...)`,await 拿 Res。
- **into_response**:Res 转 Response,返回。

**任一提取器失败,短路返回 Rejection——这是为什么 axum handler 的错误处理这么自然**:你写 `Path<i32>`,如果 URL 参数不是数字,`Path::from_request_parts` 返回 `Err(PathRejection::FailedToDeserializePath(...))`,axum 短路返回 `rejection.into_response()`(默认 400 Bad Request + 错误信息)。你不用手写"if path 解析失败 return 400",提取器自己处理。承 P3-11 提取器实战章详拆每个内置提取器的 Rejection。

### sound 的三个保证

这个提取器链设计为什么 sound?三个保证:

1. **不重复消费 body**:body 只被最后一个 `FromRequest` 消费一次,前面的 `FromRequestParts` 只 `&mut parts`,不碰 body。这是编译期保证的——宏模板里 `$($ty: FromRequestParts),*`(前面参数)和 `$last: FromRequest`(最后一个)的约束不同,你不可能让前面参数也消费 body(它们没实现 FromRequest,只实现 FromRequestParts)。承 P3-10 的桥接 impl 让只读 parts 的提取器也能当 FromRequest 用,但 axum 在宏里强制最后一个走 `FromRequest::from_request(req, state)`,body 消费只此一次。
2. **编译期保证顺序**:提取器链的顺序就是用户写 async fn 的参数顺序。你写 `async fn h(Path, Json)`,提取顺序是先 Path(FromRequestParts,只读 parts)再 Json(FromRequest,消费 body)。如果你写反了 `async fn h(Json, Path)`,Json 在前(FromRequest,消费 body),Path 在后(FromRequestParts,只读 parts)——但**这会编译错!** 因为宏约束"前 n-1 个 FromRequestParts、最后一个 FromRequest",你写 `Json, Path` 时,Json 是第 1 个参数(FromRequestParts 约束),但 `Json<T>` 不实现 FromRequestParts(它要消费 body)——编译期 trait bound 失败。这就是为什么 axum 强制"body 消费的提取器必须放最后"。承 P3-13 `#[axum::debug_handler]` 改善这种错误信息。
3. **类型推断不爆**:第三节讲过,T 携带参数列表信息,编译器能精确匹配到一份 impl,推断出每个参数类型。17 份 impl 看起来多,但 trait resolver 在用户写 `async fn(State<X>, Path<i32>)` 时,只可能匹配 2 参数那份 impl(其他份 FnOnce 约束不匹配),推断高效。

> **钉死这件事**:`call` 内部的提取器链是个 5 步状态机——拆 req → 逐个 FromRequestParts → 重组 → 最后 FromRequest → 调 fn。这个设计 sound 的三个保证:① body 只被最后一个 FromRequest 消费(编译期约束前 n-1 个是 FromRequestParts);② 提取顺序 = 参数顺序(写反了 body 消费提取器放中间会编译错);③ 类型推断精确(T 携带参数列表)。这套机制让你写 `async fn(State, Path, Json)` 时,框架替你按顺序提取,失败短路返回 Rejection,成功调 fn——零运行时开销,纯编译期生成。

---

## 第六节:HandlerService——把 Handler 包成 Service

### 提问

前面五节讲了 `Handler` trait 怎么把任意 async fn 编译期变 Handler。但 `MethodRouter` 选出来的 handler 要被 Router 当 `tower::Service` 调(承 P1-03)。Handler 不是 Service(签名不一样,第二节讲了)——中间需要一个适配器,把 Handler 包成 Service。这就是 `HandlerService`。

### HandlerService 的结构

`HandlerService` 定义在 `axum/src/handler/service.rs#L22-L26`,逐字摘录:

```rust
// axum/src/handler/service.rs#L22-L26(逐字摘录)
pub struct HandlerService<H, T, S> {
    handler: H,
    state: S,
    _marker: PhantomData<fn() -> T>,
}
```

三个字段:

- **`handler: H`**:真正的 handler,类型 H 实现 `Handler<T, S>`。
- **`state: S`**:注入的 state,在 `with_state` 时存进来。
- **`_marker: PhantomData<fn() -> T>`**:phantom,让 T 在类型签名里出现(协变,`fn() -> T` 让 T 协变)。T 不占运行时空间(它是 tuple 占位符,承第三节),只是编译期标记。

`HandlerService<H, T, S>` 表示"一个已经准备好 state 的 handler,马上可以变 Service"。它由 `Handler::with_state(self, state: S)` 创建(`mod.rs#L202-L204` 调 `HandlerService::new(self, state)`):

```rust
// axum/src/handler/mod.rs#L202-L204(在 Handler trait 里)
fn with_state(self, state: S) -> HandlerService<Self, T, S> {
    HandlerService::new(self, state)
}
```

`HandlerService::new` 是 `pub(super)`(`service.rs#L118-L126`):

```rust
// axum/src/handler/service.rs#L118-L126(逐字摘录)
impl<H, T, S> HandlerService<H, T, S> {
    pub(super) fn new(handler: H, state: S) -> Self {
        Self {
            handler,
            state,
            _marker: PhantomData,
        }
    }
}
```

### impl Service for HandlerService

核心是 `HandlerService` 实现 `tower::Service<Request>`(`service.rs#L148-L178`),逐字摘录:

```rust
// axum/src/handler/service.rs#L148-L178(逐字摘录)
impl<H, T, S, B> Service<Request<B>> for HandlerService<H, T, S>
where
    H: Handler<T, S> + Clone + Send + 'static,
    B: HttpBody<Data = Bytes> + Send + 'static,
    B::Error: Into<BoxError>,
    S: Clone + Send + Sync,
{
    type Response = Response;
    type Error = Infallible;
    type Future = super::future::IntoServiceFuture<H::Future>;

    #[inline]
    fn poll_ready(&mut self, _cx: &mut Context<'_>) -> Poll<Result<(), Self::Error>> {
        // `IntoService` can only be constructed from async functions which are always ready, or
        // from `Layered` which buffers in `<Layered as Handler>::call` and is therefore
        // also always ready.
        Poll::Ready(Ok(()))
    }

    fn call(&mut self, req: Request<B>) -> Self::Future {
        use futures_util::future::FutureExt;

        let req = req.map(Body::new);

        let handler = self.handler.clone();
        let future = Handler::call(handler, req, self.state.clone());
        let future = future.map(Ok as _);

        super::future::IntoServiceFuture::new(future)
    }
}
```

逐段拆:

**impl 头**:`impl<H, T, S, B> Service<Request<B>> for HandlerService<H, T, S>`。注意 `B` 泛型——HandlerService 对**任意 body 类型 B** 实现 Service(只要 `B: HttpBody<Data = Bytes> + Send + 'static`)。这是为了兼容 hyper 1.x 的 `Request<Incoming>`(承 P1-03 的 Router impl Service 也是 `<B>` 泛型,同思路)。

**约束**:

- `H: Handler<T, S> + Clone + Send + 'static`:H 是 handler(实现 Handler),要 Clone(每请求 Clone 一份调)、Send(跨线程)。
- `S: Clone + Send + Sync`:state 要 Clone(每请求 Clone 一份传给 handler)、Send + Sync(跨线程共享)。
- B 的约束同 Router。

**关联类型**:

- `type Response = Response`:产 Response。
- `type Error = Infallible`:不产 Service Error(错误都转成 Response,承 P5-18)。
- `type Future = IntoServiceFuture<H::Future>`:Future 类型,用 `IntoServiceFuture`(承 future.rs,下面拆)。

**`poll_ready`**——注意那个注释:

```rust
fn poll_ready(&mut self, _cx: &mut Context<'_>) -> Poll<Result<(), Self::Error>> {
    // `IntoService` can only be constructed from async functions which are always ready, or
    // from `Layered` which buffers in `<Layered as Handler>::call` and is therefore
    // also always ready.
    Poll::Ready(Ok(()))
}
```

注释原文(在 `service.rs#L161-L163`)说:HandlerService 只能从 async fn 构造,而 async fn 总是 ready(没有资源预留、没有背压);或者从 Layered 构造,Layered 在 `Handler::call` 里 buffer(buffer 满了才 poll_ready 不 ready,但 Layered 的 buffer 是无界的或者一次性计算,这里逻辑稍复杂,承 P4-16 中间件链章),所以也 always ready。

**`poll_ready` 无条件返回 Ready**,和 Router/Route 一样(承 P1-03)。理由:axum 的 handler 是 async fn,没有 `poll_ready` 概念(async fn 不持有"准备状态",每次调用都是新的);Layered 虽然套了 Tower Layer,但 Layer 的 buffer 在 call 时一次性处理(不靠 poll_ready 背压)。所以 HandlerService 的 `poll_ready` 永远 Ready。

> **承接《Tower》/《hyper》**:Service trait 的 `poll_ready` 是背压通道(承《Tower》P0-01、《hyper》P1-02)。axum 在 Router/Route/MethodRouter/HandlerService 这一整层都把 `poll_ready` 无视掉(无条件 Ready),理由是 Web 框架的背压由更外层承担——hyper 的连接数限制(HTTP/1 in_flight 单槽、HTTP/2 h2 流控)、Tokio 的 task 数。Service 层再传背压是冗余,所以 axum 故意忽略它。这个取舍贯穿 P1-03,本章一句带过。

**`call`**:

```rust
fn call(&mut self, req: Request<B>) -> Self::Future {
    use futures_util::future::FutureExt;

    let req = req.map(Body::new);                    // ① 把任意 body 包成 axum Body

    let handler = self.handler.clone();              // ② Clone 一份 handler
    let future = Handler::call(handler, req, self.state.clone());  // ③ 调 Handler::call
    let future = future.map(Ok as _);                // ④ 把 Response 包成 Result<Response, Infallible>

    super::future::IntoServiceFuture::new(future)    // ⑤ 包成 IntoServiceFuture
}
```

五步:

1. **`req.map(Body::new)`**:把任意 body 类型 B 转成 axum 的 `Body`(`axum_core::body::Body`)。这一步是 body 类型归一化(承 P1-03 同样处理)。
2. **`self.handler.clone()`**:**Clone 一份 handler**。为什么 Clone?因为 `Service::call(&mut self, ...)` 是借用 self,不能消费 handler;但 `Handler::call(self, ...)` 是按值消费 handler。所以 HandlerService::call 在调 Handler::call 之前,先 Clone 一份 handler 出来消费,原 handler 留在 HandlerService 里给下一个请求用。注意 Handler 的 supertrait 含 `Clone`(`mod.rs#L148`),这一步成立。
3. **`Handler::call(handler, req, self.state.clone())`**:调 Handler::call(第二节讲过签名 `fn call(self, req, state)`)。把 Clone 出来的 handler、req、Clone 出来的 state 传进去。返回 `H::Future`(产 Response)。
4. **`future.map(Ok as _)`**:把 `Future<Output = Response>` 包成 `Future<Output = Result<Response, Infallible>>`——加一层 `Ok`,因为 Service::Future 的 Output 是 `Result<Response, Error>`,Error 是 Infallible,所以要包成 `Ok(response)`。
5. **`IntoServiceFuture::new(future)`**:把 future 包成 `IntoServiceFuture` 类型(关联类型 `type Future = IntoServiceFuture<H::Future>`)。

### IntoServiceFuture:Future 类型的适配

`IntoServiceFuture` 定义在 `axum/src/handler/future.rs#L11-L18`,用 `opaque_future!` 宏生成:

```rust
// axum/src/handler/future.rs#L11-L18(逐字摘录)
opaque_future! {
    /// The response future for [`IntoService`](super::IntoService).
    pub type IntoServiceFuture<F> =
        Map<
            F,
            fn(Response) -> Result<Response, Infallible>,
        >;
}
```

`opaque_future!` 宏(在 `macros.rs#L4-L46`,本章不深拆)把这个 type alias 转成一个 `pub struct IntoServiceFuture<F> { #[pin] future: Map<F, fn(Response) -> Result<Response, Infallible>> }`,带 `new`/`Debug`/`Future` impl。底层 future 类型是 `Map<F, fn(Response) -> Result<Response, Infallible>>`——把 `F`(产 Response)map 成产 `Result<Response, Infallible>`。

这个 `Map` + `Ok` 的包装,就是上面 `call` 里第 ④⑤ 步干的事——把 Handler future(产 Response)适配成 Service future(产 `Result<Response, Infallible>`)。这是 Handler 和 Service 两个 trait 之间 Output 类型的桥梁。

### HandlerService 的对内对外两个 call

回过头看,HandlerService 也有"对内对外两个 call"的模式(承 P1-03):

| 角色 | 签名 | 干什么 |
|------|------|--------|
| **对外 `Service::call`** | `fn call(&mut self, req: Request<B>) -> IntoServiceFuture<H::Future>` | Clone handler + Clone state + 调 `Handler::call` + 包 Result |
| **对内 `Handler::call`** | `fn call(self, req: Request, state: S) -> H::Future` | 拆 req → 提取器链 → 调真 fn → into_response |

Service::call 是 Tower/hyper 调的(签名固定,`&mut self, req`);Handler::call 是 axum 内部调的(签名 `self, req, state`)。HandlerService 是这两层之间的适配器:对外暴露 Service::call(让 MethodRouter 能调它),对内调 Handler::call(执行提取器链 + fn)。

> **钉死这件事**:HandlerService 是 Handler 到 Service 的适配器。它把 Handler 的 `call(self, req, state)` 包装成 Service 的 `call(&mut self, req)`,关键三步:① Clone handler(因为 Service::call 借用 self,Handler::call 消费 self,中间靠 Clone 桥接);② Clone state(同理);③ 把 Response 包成 `Ok(Response)`(因为 Service::Future 的 Output 是 Result,Handler::Future 的 Output 是 Response)。HandlerService 的 `poll_ready` 无条件 Ready(理由:async fn always ready,承 P1-03)。

### MethodRouter 怎么把 handler 存进路由表

最后说一下 handler 怎么从 `get(handler_a)` 一路存进 MethodRouter。这一段承 P2-06 详拆,这里只点到:

1. 你写 `get(handler_a)`,`get` 函数把 `handler_a` 包成 `MethodRouter`(`method_routing.rs` 里 `get` 是 `MethodRouter::new` 的语法糖,把 handler 存进 `get` 槽)。
2. 这时 handler 存的形式是 `MethodEndpoint::HandlerRoute` 还是 `MethodEndpoint::Router`?在 axum 0.8.x,handler 存成 `BoxedHandler<S>`(承 P2-06),本质是个类型擦除的 handler(类似 Route 的 BoxCloneSyncService,但专门给 handler 用)。
3. `MethodRouter::with_state(state)` 把每个 `BoxedHandler<S>` 物化成 `Route`(类型擦除的 Service),存进 `MethodEndpoint::Route`(承 P1-03 Route 类型擦除)。
4. 请求来了,`MethodRouter::call_with_state(req, state)` 按 method 选 MethodEndpoint,调它的 Service::call。

整个链路:`async fn` → `MethodRouter`(BoxedHandler)→ `with_state` → `Route`(BoxCloneSyncService)→ MethodRouter::call → Service::call。HandlerService 在 `with_state` 这一步出现(它就是 `BoxedHandler` 物化成的具体 Service 类型之一,承 P2-06)。本章不深拆这条链路,只关注 HandlerService 这一层。

---

## 第七节:Layered——给单个 handler 套 Tower Layer

### 提问

第二节看到 `Handler::layer<L>(self, layer: L) -> Layered<L, Self, T, S>`,这是"给单个 handler 套一个 Tower Layer"的入口。这一节拆 Layered 怎么实现。

### Layered 的结构

`Layered` 定义在 `axum/src/handler/mod.rs#L285-L289`,逐字摘录:

```rust
// axum/src/handler/mod.rs#L285-L289(逐字摘录)
pub struct Layered<L, H, T, S> {
    layer: L,
    handler: H,
    _marker: PhantomData<fn() -> (T, S)>,
}
```

注意泛型顺序是 `<L, H, T, S>`——L 是 Layer,H 是 handler,T/S 是 Handler 的两个类型参数(承第二节)。

三个字段:

- **`layer: L`**:Tower Layer(`tower_layer::Layer`),配置好但还没应用。
- **`handler: H`**:原 handler。
- **`_marker: PhantomData<fn() -> (T, S)>>`:phantom,协变。

Layered 自己也是 Handler(`mod.rs#L316-L350` 实现 Handler for Layered),它的 call(`mod.rs#L327-L349`)长这样(简化):

```rust
// axum/src/handler/mod.rs#L316-L350(简化示意,摘关键部分)
impl<L, H, T, S> Handler<T, S> for Layered<L, H, T, S>
where
    L: Layer<HandlerService<H, T, S>> + Clone + Send + Sync + 'static,
    L::Service: Service<Request> + Clone + Send + Sync + 'static,
    <L::Service as Service<Request>>::Future: Send,
    H: Handler<T, S> + Clone + Send + Sync + 'static,
{
    type Future = future::LayeredFuture<L::Service>;

    fn call(self, req: Request, state: S) -> Self::Future {
        // ① 把 handler 包成 HandlerService(注入 state)
        let svc = HandlerService::new(self.handler, state);
        // ② 应用 Layer:layer.layer(svc) 得到装饰过的 Service
        let svc = self.layer.layer(svc);
        // ③ 用 tower::ServiceExt::oneshot 跑这个 Service,产 Response
        future::LayeredFuture::new(svc.oneshot(req).map(...))
    }
}
```

**Layered::call 的逻辑**:

1. 把 handler + state 包成 HandlerService(`HandlerService::new(handler, state)`,承第六节)。
2. 应用 Layer:`self.layer.layer(svc)` 得到装饰过的 Service(L::Service)。这个 Service 是原 HandlerService 套了 Layer 之后的产物(比如套了 TimeoutLayer 就是 `Timeout<HandlerService<...>>`)。
3. 用 `tower::ServiceExt::oneshot(req)` 调这个 Service(oneshot 是 Tower 的便利方法,把 Service 转成 future 一次性消费),产 `Result<L::Response, L::Error>`。
4. 把 Result unwrap 成 Response(L::Error 要能转 Response,通常 Layered 的约束保证这点)。

注意 Layered 的 Handler::Future 是 `LayeredFuture<L::Service>`(`future.rs#L20-L29` 用 `pin_project!` 定义),底层是 `Map<Oneshot<L::Service, Request>, fn(Result<L::Response, L::Error>) -> Response>`——Oneshot 跑 Service,Result map 成 Response。

### 为什么 Layered 也是 Handler

Layered 实现了 Handler,意味着你可以链式调用 `.layer()` 多次:`handler.layer(A).layer(B).with_state(state)`。每次 `.layer()` 都产生一个新的 Layered(包了一层),最后 `with_state` 把它变 Service。

这是 Handler::layer 和 Router::layer 的区别:

- **`Handler::layer`**:给**单个 handler** 套 Layer,只影响这一个 handler。
- **`Router::layer`**:给**整个 Router 的所有路由**(包括 fallback)套 Layer。
- **`MethodRouter::layer`**:给**同路径的所有 method handler** 套 Layer。
- **`Router::route_layer`**:给**匹配的路由**套 Layer(不含 fallback)。

这四种作用域是 P4-16 详拆的内容,本章只关注 Handler::layer 这一种。Layered 让 Handler::layer 返回的还是 Handler,保持链式组合性。

### Layered 的 poll_ready 为什么也 always ready

回到第六节那个 HandlerService 的 poll_ready 注释,提到 Layered 也 always ready:

> `IntoService` can only be constructed from async functions which are always ready, or
> from `Layered` which buffers in `<Layered as Handler>::call` and is therefore
> also always ready.

"Layered which buffers"——Layered 在 `Handler::call`(第七节简化代码)里,直接 `svc.oneshot(req)` 一次性消费 Service,不在 `poll_ready` 上做背压。Layered 内部的 LayeredFuture 是 Oneshot 的 future,它的 poll 直接 poll 内层 Service 的 future(不调 poll_ready)。

这是 axum 的有意取舍:Layered 不传播 Layer 的背压(虽然 Layer 可能有自己的背压,比如 ConcurrencyLimit 的 permit),因为 axum 在路由层已经把背压交给外层(hyper 连接数、Tokio task 数)管了。这个取舍和 P1-03 的 Router/Route poll_ready always Ready 一致,贯穿全书。

> **承接 P4-16**:Layered 怎么和 ServiceBuilder 配合、中间件链怎么叠、四种 layer 作用域的区别——这些深度拆留 P4-16 中间件链招牌章。本章只关注 Layered 作为 Handler::layer 返回值的角色。

---

## 第八节:HandlerWithoutStateExt 和 IntoResponseHandler——两个特殊 impl

### 提问

除了宏展开的 17 份 impl(0~16 参数),Handler 还有两个特殊 impl:`Handler<private::IntoResponseHandler, S> for T where T: IntoResponse`(让任何 IntoResponse 类型直接当 handler)和 `HandlerWithoutStateExt`(给无 state handler 的便利方法)。这一节简短拆。

### IntoResponseHandler:让 `&'static str`、`StatusCode` 直接当 handler

来看 `mod.rs#L264-L280`,逐字摘录:

```rust
// axum/src/handler/mod.rs#L264-L280(逐字摘录)
mod private {
    // Marker type for `impl<T: IntoResponse> Handler for T`
    #[allow(missing_debug_implementations)]
    pub enum IntoResponseHandler {}
}

#[diagnostic::do_not_recommend]
impl<T, S> Handler<private::IntoResponseHandler, S> for T
where
    T: IntoResponse + Clone + Send + Sync + 'static,
{
    type Future = std::future::Ready<Response>;

    fn call(self, _req: Request, _state: S) -> Self::Future {
        std::future::ready(self.into_response())
    }
}
```

这个 impl 让任何实现 `IntoResponse` 的类型 `T`(如 `&'static str`、`String`、`StatusCode`、`(StatusCode, Json<T>)` 等)直接当 handler。T 类型(占位 tuple)是 `private::IntoResponseHandler`——一个空 enum,作为 marker(和 ViaRequest/ViaParts 同思路,承 P3-10)。

**用法**:你可以写 `Router::new().route("/health", get("ok"))`——直接传个 `"ok"` 字符串字面量当 handler。这时 `"ok": &'static str` 实现了 IntoResponse,通过这个 impl 自动实现 Handler。`call` 内部:`std::future::ready(self.into_response())`——立即把 `&'static str` 转 Response(设 text/plain、body = "ok"),返回一个立即就绪的 future(`std::future::Ready`)。

**这个 impl 的 T 类型 `IntoResponseHandler` 和宏展开的 T 类型 `(M, T1, ...)` 不冲突**(孤儿规则):因为 `IntoResponseHandler` 是私有空 enum,只在这里用,不会和 tuple 重叠。所以这个特殊 impl 可以和 17 份宏展开的 impl 共存。

### HandlerWithoutStateExt:无 state handler 的便利方法

`HandlerWithoutStateExt` 定义在 `mod.rs#L352-L378`,逐字摘录关键部分:

```rust
// axum/src/handler/mod.rs#L352-L378(逐字摘录,关键部分)
pub trait HandlerWithoutStateExt<T>: Handler<T, ()> {
    /// Convert the handler into a [`Service`] and no state.
    fn into_service(self) -> HandlerService<Self, T, ()>;

    /// Convert the handler into a [`MakeService`] and no state.
    fn into_make_service(self) -> IntoMakeService<HandlerService<Self, T, ()>>;

    /// Convert the handler into a [`MakeService`] which stores information
    #[cfg(feature = "tokio")]
    fn into_make_service_with_connect_info<C>(
        self,
    ) -> IntoMakeServiceWithConnectInfo<HandlerService<Self, T, ()>, C>;
}
```

这个 trait 是给**无 state**(state 类型是 `()`)的 handler 提供便利方法——`into_service`、`into_make_service`、`into_make_service_with_connect_info`。它约束 `Handler<T, ()>`(S 必须是 `()`),即 handler 不需要 state。

它的 blanket impl 在 `mod.rs#L380-L398`(任何 `Handler<T, ()>` 自动实现 HandlerWithoutStateExt)。用法:`handler.into_service()` 直接变 Service(不需要 Router),可以塞进任何 Tower 工具链。

> **钉死这两个特殊 impl 的位置**:它们是 axum handler 抽象的两个边角——`IntoResponseHandler` 让 IntoResponse 类型直接当 handler(常用于静态响应);`HandlerWithoutStateExt` 给无 state handler 提供便利方法。技巧精解会再提 `IntoResponseHandler` 的 T 设计(它和宏展开的 T 共存,绕开孤儿规则)。

---

## 第九节:演进史——从 async-trait 到 RPITIT

### 提问

axum 0.5 时代,`Handler`、`FromRequest`、`FromRequestParts` 这些 trait 用的是 `#[async_trait]`——一个把 `async fn` 在 trait 里转成 `Box<dyn Future>` 的过程宏。到 axum 0.6/0.7,改成了"原生 async fn in trait"(RPITIT,Return Position Impl Trait In Trait,Rust 1.75 稳定)。这个演进为什么发生、对 Handler trait 有什么影响?

### async-trait 时代:每个 trait 方法返回 Box<dyn Future>

`#[async_trait]` 是 dtolnay 写的过程宏,它把 trait 里的 `async fn foo()` 转成 `fn foo<'a>(&'a self, ...) -> Pin<Box<dyn Future<Output = ...> + Send + 'a>>`。每个 trait 方法调用都走一次 Box 堆分配(把 future 装箱)。

对 Handler trait,这意味 `call` 返回 `Pin<Box<dyn Future<Output = Response> + Send>>`——每次调 handler 都堆分配一次。在 Web 框架里,每请求一次堆分配不算大开销(相比 hyper 的连接管理、Tokio 的 task 调度),但累积起来也是性能损失。

### RPITIT 时代:trait 关联类型 + impl Future

Rust 1.75(2023-12)稳定了 RPITIT,trait 里可以直接写 `fn foo(&self) -> impl Future<...>`(实际上是 `type Future: Future<...>; fn foo(&self) -> Self::Future;` 的语法糖)。这让 trait 方法返回的具体 future 类型可以被命名(作为关联类型),不需要 Box。

axum 0.6 起把 Handler、FromRequest、FromRequestParts 这些 trait 改成"关联类型 Future + 具体 future 类型"。来看 `FromRequest::from_request` 的现代签名(`axum-core/src/extract/mod.rs#L85-L88`):

```rust
// axum-core/src/extract/mod.rs#L85-L88(逐字摘录)
fn from_request(
    req: Request,
    state: &S,
) -> impl Future<Output = Result<Self, Self::Rejection>> + Send;
```

注意 `impl Future<...> + Send`——这是 RPITIT。提取器实现这个方法时,返回的 future 类型由编译器推断(具体类型是实现细节),不需要 Box。

但是,**Handler trait 的 `type Future` 还是 `Pin<Box<dyn Future<Output = Response> + Send>>`**(`mod.rs#L236`)。为什么 Handler 没用 RPITIT?

答案在第五节看到——`Handler::call` 内部是 `Box::pin(async move { ... })`,那个 async block 的 future 类型是匿名、无法命名的(它捕获了 parts、state、handler 等局部变量),所以必须 `Box::pin` 擦除。即使有 RPITIT,这种"在 trait 方法里写 async block"的场景,关联类型 Future 还是得 Box。

> **承接《Tower》/《hyper》**:Tower 的 Service trait 也是 `type Future: Future<...>`(关联类型,不 Box),承《Tower》P0-01。axum 的 Handler 因为 call 内部是 async block,Future 仍是 Box。这是一个工程取舍——Handler::Future 装箱的代价(每请求一次堆分配)被 Tokio 的内存分配器(jemalloc/mimalloc 风格)摊薄,实际开销很小。

### axum 0.7→0.8 的 Handler 变化

axum 0.7 到 0.8,Handler trait 本身变化不大(主要是路由 API 变动:`route` 只接 MethodRouter、路径参数 `{foo}`、nest 在 `/` 不支持,承 P6-20)。Handler trait 的核心设计(T 占位 tuple、宏展开 0~16 参数、call 提取器链)从 0.6 起就稳定了。

一个小的演进:`#[diagnostic::on_unimplemented(note = ...)]`(`mod.rs#L145`)和 `#[diagnostic::do_not_recommend]`(`mod.rs#L225`)是 Rust 1.78+ 的诊断属性,axum 用它们改善"handler 类型错"时的错误信息。承 P3-13 `#[axum::debug_handler]` 详拆错误信息改善。

---

## 第十节:跨语言对照——actix/rocket/go 怎么做 handler

### 提问

"声明式 handler"(写个 async fn 自动当 handler)不是 axum 独有的想法。actix-web、rocket、go net/http 都有类似机制。它们和 axum 的 Handler trait 比,有什么本质区别?

把这张对照钉死,你就理解了 axum 的"宏展开 0~16 参数 + 编译期提取器链"在工程上妙在哪里。

### 四套 handler 抽象对照

| 系统 | 语言 | handler 抽象 | 参数提取 | 编译期/运行期 |
|------|------|------------|---------|---------------|
| **axum** | Rust | `Handler<T, S>` trait + 宏展开 0~16 参数 | `FromRequest`/`FromRequestParts` 提取器链(编译期生成) | 编译期 |
| **actix-web** | Rust | `Handler` trait + Actor 模型 | `FromRequest` trait,但要手写或用 `#[derive]` | 运行期分发 |
| **rocket** | Rust | 过程宏 `#[get("/")]` 注入 | `FromRequest` trait,request guard | 编译期(过程宏) |
| **go net/http** | Go | `Handler` interface{ServeHTTP(w, r)} | 手解 `r.URL.Query()`、`r.Body` | 运行期 |

逐条拆:

**axum**:你写 `async fn h(State(s): State<X>, Path(id): Path<i32>) -> String`,axum 用宏在编译期为这个签名生成一份 `impl Handler<(M, State<X>, Path<i32>), X> for F`。生成代码里:① 拆 req;② State 提取(FromRequestParts,从 state 拿);③ Path 提取(FromRequestParts,从 parts.extensions 拿 URL 参数);④ 调 fn;⑤ into_response。零运行时开销(单态化),编译期类型安全(参数类型错编译报错)。

**actix-web**:actix 的 handler 也用 trait(`actix_web::dev::Handler`),但它**没有宏批量生成 impl**。每个 handler 签名要手写或用 `#[derive(actix_web::FromRequest)]`。actix 的参数提取用 `FromRequest` trait(和 axum 同名,但不同实现),它在运行期通过 App data(state)和 HttpRequest 提取。actix 还用 Actor 模型(actor 持有 state,handler 是 message),比 axum 的"fn + state 参数"复杂。

关键区别:axum 的宏替你写 17 份 impl(覆盖 0~16 参数),你写 `async fn(State, Path, Json)` 自动命中 3 参数那份;actix 没有 16 参数的批量 impl,要么手写 `impl Handler for MyHandler`,要么用 `#[derive]`(每个自定义提取器要 derive)。axum 的宏是"通用模板",actix 是"每个类型各自实现"。

**rocket**:rocket 用过程宏 `#[get("/users/{id}")]` 注入到 handler fn。编译期,过程宏把 fn 改写成"实现某个内部 trait",参数(request guard)在 fn 调用前自动从 Request 提取。rocket 的机制也是编译期的(过程宏),但它绑死了路由宏(`#[get("/path")]`)和 handler——你必须在 fn 上加 `#[get(...)]` 才能让它当 handler。axum 把路由(`get(handler)`)和 handler 分开,handler 是普通 async fn(不需要注解),路由是 `Router::new().route(path, get(handler))`。

rocket 的 request guard 类似 axum 的 FromRequest,但 rocket 用 trait `FromRequest`(同名),且依赖过程宏注入。axum 的提取器链是宏生成的(impl_handler! 模板),不需要过程宏注解 handler fn。

**go net/http**:go 的 handler 是 `interface { ServeHTTP(w http.ResponseWriter, r *http.Request) }`。参数提取**全手写**——`r.URL.Query().Get("id")`、`json.NewDecoder(r.Body).Decode(&payload)`。没有提取器抽象,没有自动反序列化。go 的设计哲学是"简单直接",代价是每个 handler 都要手写参数提取(容易错,字段名拼错运行期才发现)。

### 一个具体的对照:同一 handler 四种写法

来看"GET /users/{id} 返回用户"这个简单 handler,四种框架怎么写:

**axum**:
```rust
async fn get_user(Path(id): Path<i32>, State(db): State<Db>) -> impl IntoResponse {
    Json(db.get_user(id).await)
}
Router::new().route("/users/{id}", get(get_user))
```

**actix-web**(简化):
```rust
async fn get_user(path: web::Path<i32>, db: web::Data<Db>) -> impl Responder {
    HttpResponse::Ok().json(db.get_user(*path).await)
}
App::new().service(web::resource("/users/{id}").route(web::get().to(get_user)))
```
(actix 的 `web::Path`、`web::Data` 是提取器,但和 axum 的 FromRequest trait 实现不同;actix 没有 16 参数宏批量生成,每个签名要走 `web::get().to(fn)` 的路由注册)

**rocket**(简化):
```rust
#[get("/users/<id>")]
async fn get_user(id: i32, db: &State<Db>) -> Json<User> {
    Json(db.get_user(id).await)
}
Rocket::new().mount("/", routes![get_user])
```
(rocket 的 `#[get("/users/<id>")]` 过程宏编译期注入,`<id>` 是路径参数占位符;handler 加注解才能当 handler)

**go net/http**(简化):
```go
func getUser(w http.ResponseWriter, r *http.Request) {
    vars := mux.Vars(r)  // 用 gorilla/mux
    id, _ := strconv.Atoi(vars["id"])
    user := db.GetUser(id)
    json.NewEncoder(w).Encode(user)
}
r.HandleFunc("/users/{id}", getUser)
```
(全手写:提取 path 参数、调 db、序列化、写 ResponseWriter)

四个对照看出 axum 的妙处:**只有 axum 让你写普通 async fn(不加注解)+ 自动提取参数(编译期生成)+ 自动序列化响应(IntoResponse)**。actix 要在路由注册时显式 `.to(fn)`,rocket 要在 fn 上加 `#[get]` 注解,go 要全手写。axum 的"声明式"是最纯粹的——你写 `async fn`,axum 替你生成一切。

> **钉死这件事**:axum 的 Handler trait + 宏展开 0~16 参数,是 Rust 类型系统能给出的最优雅的"声明式 handler"实现——你写普通 async fn,axum 在编译期替你生成 impl、提取器链、Service 适配。对照 actix(每个签名手写/derive)、rocket(过程宏注解)、go(全手写),axum 的"零注解 + 编译期生成 + 类型安全"是独一份的。这套机制的代价是:宏展开的代码用户看不到(出错时错误信息难懂,要靠 `#[axum::debug_handler]` 改善,承 P3-13),理解 Handler trait 的 T 参数需要懂 Rust 类型系统(这就是本书这一章存在的理由)。

---

## 技巧精解

这一节挑两个最该被钉死的技巧,配真实源码 + 反面对比,单独拆透。

### 技巧一:Handler<T, S> 的 T 参数——为什么必须是 tuple 占位符

**它解决什么问题**:axum 想给"0~16 个参数的 async fn"全部实现 Handler,但 Rust 的孤儿规则不允许"对同一类型多次 impl 同一 trait"。怎么让 17 份 impl 共存?

**反面对比 1:朴素地写 `impl Handler<()> for F` 会撞孤儿规则**

假设 axum 偷懒,所有参数个数都写 `impl Handler<()> for F`:

```rust
// 假想的朴素写法(非 axum 实际做法,会编译错)
impl<F, Fut, Res, S, T1> Handler<(), S> for F
where F: FnOnce(T1) -> Fut, Fut: Future<Output = Res>, Res: IntoResponse {
    fn call(self, req: Request, state: S) -> Self::Future { /* 提取 T1, 调 fn */ }
}

impl<F, Fut, Res, S, T1, T2> Handler<(), S> for F
where F: FnOnce(T1, T2) -> Fut, Fut: Future<Output = Res>, Res: IntoResponse {
    fn call(self, req: Request, state: S) -> Self::Future { /* 提取 T1, T2, 调 fn */ }
}
```

编译器立刻报错 `E0119: conflicting implementations of trait Handler<(), S> for type F`。为什么?因为这两份 impl 的 trait 头都是 `Handler<(), S> for F`(trait 一样、type 一样),Rust 的 coherence 规则认为它们"潜在冲突",直接拒绝——即使两个 `where` 约束(F: FnOnce(T1) vs F: FnOnce(T1, T2))实际上不可能同时满足(同一个 F 不可能既是 1 参 fn 又是 2 参 fn),编译器不做这种"不可同时满足"的推理。

**反面对比 2:用独立的 marker trait 也能绕,但代价大**

有人可能想:那我给每个参数个数定义一个独立 marker trait,比如:

```rust
// 假想方案 2(非 axum 实际做法)
pub trait Handler1<S> { ... }
pub trait Handler2<S> { ... }
// ... Handler0 到 Handler16
```

这样能绕开孤儿规则(每个 trait 独立),但代价巨大:① API 碎片化(用户要写 `impl Handler3<AppState> for F`,不能统一);② Router 没法接受统一的 Handler 类型(它要存不同参数个数的 handler,需要 17 个不同的 trait object 类型);③ 完全失去"`Handler<T, S>` 一个 trait 容纳所有参数个数"的统一性。axum 选了一个 trait,用 T 区分参数个数,是最优雅的方案。

**所以 axum 这么设计:T 是 tuple,长度编码参数个数**

`Handler<T, S>` 的 T 是个 tuple,长度 = 参数个数 + 1(第一个元素是 M,FromRequest 的 marker)。来看真实的 T:

| 参数个数 | T |
|----------|---|
| 0 | `((),)` |
| 1 | `(M, T1)` |
| 2 | `(M, T1, T2)` |
| ... | ... |
| 16 | `(M, T1, ..., T16)` |

每份 impl 的 trait 头(`mod.rs#L227`)是 `Handler<(M, T1, ..., TN,), S> for F`——tuple 类型不同,trait 头签名不同,孤儿规则满意。这是 Rust 类型系统能给出的最优雅解法:**用一个类型参数(T)的"形状"(tuple 长度)编码一个维度(参数个数),让多份 impl 共存**。

**这个技巧的"妙处"在哪**:

1. **零运行时开销**:T 是 PhantomData(tuple 不占内存,`_marker: PhantomData<fn() -> T>`),运行时不存在。17 份 impl 里只有一份(参数个数匹配的)被编译器选中,其他 16 份在 trait 解析时丢弃。最终代码里,你的 async fn 单态化成一个独一无二的 Handler 实现,没有任何虚分派。
2. **类型推断精确**:T 携带参数列表信息,编译器看到 `async fn h(State<X>, Path<i32>)`,能精确匹配 2 参数那份 impl,推断出 `T1 = State<X>`、`T2 = Path<i32>`、`M = ViaParts`(通过桥接 impl)。推断高效,不会爆。
3. **错误信息可改善**:当用户写错(比如参数类型不实现 FromRequest),编译器报 trait bound 错。`#[diagnostic::on_unimplemented]`(`mod.rs#L145`)和 `#[axum::debug_handler]`(P3-13)能改善错误信息。

**为什么是 tuple,不是 struct**:tuple 的优势是"长度可变"——`(M, T1)`、`(M, T1, T2)`、`(M, T1, T2, T3)` 是不同类型,编译器自动识别长度。如果用 struct,要为每个参数个数定义一个 struct(`Handler1Args<M, T1>`、`Handler2Args<M, T1, T2>`、...),冗长。tuple 让 17 份 impl 共享一个"形状模式",`all_the_tuples!` 宏的 16 行手写就够。

**反面对比 3:如果不用宏,手写 17 份 impl 会怎样**

宏 `impl_handler!` + `all_the_tuples!` 替你写了 16 份 impl(1~16 参数),手写 0 参数 1 份。如果不用宏,要手写 17 份——每份约 40 行(`mod.rs#L221-L260` 模板展开后),总共 680 行重复代码。宏把这 680 行压成 40 行(模板)+ 16 行(`all_the_tuples!` 调用)= 56 行。这是宏的最大价值——消除重复,让"为每种参数个数各写一份 impl"变得可维护。

> **钉死这个技巧**:`Handler<T, S>` 的 T 是 tuple 占位符,长度编码参数个数。这个设计解决三件事:① 绕开孤儿规则(每份 impl trait 头类型不同);② 类型推断精确(T 携带参数列表);③ 零运行时开销(T 是 PhantomData)。配合 `impl_handler!` + `all_the_tuples!` 宏,17 份 impl 在 56 行宏代码里生成。这是 Rust 类型系统 + 宏展开能给出的最优雅"声明式 handler"实现,axum 的招牌。

### 技巧二:最后一个参数才能 FromRequest——body 消费 sound 的编译期保证

**它解决什么问题**:body 是 Stream,只能被消费一次。怎么保证"提取器链里只有一个参数消费 body,前面的参数只读 parts"?

**反面对比 1:允许任意参数消费 body 会怎样**

假设 axum 允许任意参数都 FromRequest(消费 body)。你写 `async fn h(Json<A>, Json<B>)`(两个 Json 提取器),会发生:

1. Json<A> 跑 `from_request(req, state)`,消费 body,反序列化成 A。body 现在是空的。
2. Json<B> 跑 `from_request(req, state)`,但 body 已经被消费了,它拿到的是空 body。serde_json 反序列化空字节,报 EOF 错。
3. 用户看到莫名其妙的 400 Bad Request(JSON 反序列化失败),但代码看起来"两个 Json 参数都合理"。

这种 bug 极难排查——表面看代码没问题,实际是"body 被消费两次"。axum 的设计**在编译期就堵死这个 bug**。

**所以 axum 这么设计:宏约束前 n-1 个 FromRequestParts、最后一个 FromRequest**

`impl_handler!` 宏的约束(`mod.rs#L233-L234`):

```rust
$( $ty: FromRequestParts<S> + Send, )*   // 前 n-1 个参数:FromRequestParts(只读 parts)
$last: FromRequest<S, M> + Send,         // 最后一个:FromRequest(可消费 body)
```

这个约束是**编译期**的。如果你写 `async fn h(Json<A>, Path<i32>)`(Json 在前,Path 在后),编译器:

1. 推断 `T1 = Json<A>`,要求 `Json<A>: FromRequestParts<S>`(因为 T1 是前 n-1 个参数)。
2. 但 `Json<A>` **不实现 FromRequestParts**(它要消费 body,只能 FromRequest)。编译错:`Json<A>: FromRequestParts<S>` not satisfied。

你看到编译错,知道"Json 必须放最后"。修复:`async fn h(Path<i32>, Json<A>)`,Path 在前(FromRequestParts,只读 parts),Json 在后(FromRequest,消费 body)。编译通过。

这就是 axum 的**编译期 body 消费 sound 保证**:宏约束"前 n-1 个 FromRequestParts、最后一个 FromRequest",你不可能写出一个"中间参数消费 body"的 handler——编译期就拦住。

**反面对比 2:如果用运行期检查会怎样**

假设 axum 用运行期检查(body 被消费过就返回错误):

```rust
// 假想的运行期检查(非 axum 实际做法)
fn call(self, req: Request, state: S) -> Self::Future {
    Box::pin(async move {
        let mut body_consumed = false;
        // T1 提取
        let T1 = if T1::needs_body() {
            if body_consumed { return Err(BodyAlreadyConsumed); }
            body_consumed = true;
            T1::from_request(req_with_body, state).await
        } else {
            T1::from_request_parts(parts, state).await
        };
        // ... 类似 T2, T3 ...
    })
}
```

这个方案的问题:① 运行期开销(每个提取器要判 needs_body + body_consumed 标志);② 错误延迟到运行期(用户写错参数顺序,运行时才发现,可能上线后某个请求触发);③ 错误信息糟糕("BodyAlreadyConsumed"对用户没意义,他不知道哪个参数消费了 body)。axum 选编译期保证,这三个问题都没有。

**这个技巧的"妙处"**:

1. **编译期保证**:你不可能写出"中间参数消费 body"的 handler,编译就错。
2. **零运行时开销**:没有 needs_body 标志、没有 body_consumed 检查,直接调 from_request_parts 或 from_request。
3. **错误信息友好**:编译错 + `#[axum::debug_handler]`(P3-13)改善,用户立刻知道哪个参数类型错。

**桥接 impl:让只读 parts 的提取器也能当 FromRequest 用**

但有个问题:你写 `async fn h(Path<i32>)`(1 参数,Path 是 FromRequestParts),宏约束 `$last: FromRequest<S, M>`,Path 不实现 FromRequest(它只实现 FromRequestParts)!这怎么办?

axum-core 提供了一个桥接 blanket impl(`axum-core/src/extract/mod.rs#L91-L105`,逐字摘录):

```rust
// axum-core/src/extract/mod.rs#L91-L105(逐字摘录)
impl<S, T> FromRequest<S, private::ViaParts> for T
where
    S: Send + Sync,
    T: FromRequestParts<S>,
{
    type Rejection = <Self as FromRequestParts<S>>::Rejection;

    fn from_request(
        req: Request,
        state: &S,
    ) -> impl Future<Output = Result<Self, Self::Rejection>> {
        let (mut parts, _) = req.into_parts();
        async move { Self::from_request_parts(&mut parts, state).await }
    }
}
```

这个 blanket impl 让任何 `FromRequestParts<S>` 的类型 T,自动实现 `FromRequest<S, ViaParts>`(M = ViaParts)。它的 `from_request` 实现:把 req 拆 parts/body,**丢弃 body**(`_`),调 `from_request_parts`。

所以你写 `async fn h(Path<i32>)`,宏展开时 `$last = Path<i32>`,约束 `$last: FromRequest<S, M>`,编译器推断 `M = ViaParts`(通过桥接 impl),`Path<i32>: FromRequest<S, ViaParts>` 成立。call 内部 `Path::from_request(req, state)` 走桥接 impl——拆 req、丢 body、调 from_request_parts。

这个桥接 impl 是"只读 parts 的提取器也能当最后一个参数"的关键,深度拆留 P3-10,本章只用。

> **钉死这个技巧**:宏约束"前 n-1 个 FromRequestParts、最后一个 FromRequest",让"body 只能被消费一次"成为编译期保证——你不可能写错,错了编译报错。配合桥接 impl(让 FromRequestParts 自动获得 FromRequest<S, ViaParts>),只读 parts 的提取器也能放最后(它通过桥接 impl"消费"body,但实际丢弃)。这是 axum 提取器链 sound 的核心,也是 Rust 类型系统保证 sound 的经典样本。

### 反例剖析:用户最容易踩的两个坑

来看两个用户最容易踩的坑,加深理解。

**坑 1:body 消费提取器放中间**

```rust
// 错误写法:Json(消费 body)放中间,Path 在后
async fn bad_handler(Json<payload>: Json<Body>, Path(id): Path<i32>) -> String { ... }
```

编译错:`Json<Body>: FromRequestParts<S>` not satisfied。因为宏约束前 n-1 个参数(这里是 Json)是 FromRequestParts,但 Json 不实现它。用户必须把 Json 放最后:`async fn good_handler(Path(id): Path<i32>, Json<payload>: Json<Body>)`。

这个坑是新手最常见的,`#[axum::debug_handler]`(P3-13)能给出更友好的错误信息(直接说"Json must be the last argument because it consumes the request body")。

**坑 2:参数超过 16 个**

```rust
// 错误写法:17 参数
async fn too_many(t1: T1, t2: T2, ..., t17: T17) -> R { ... }
```

编译错:`Handler<?, S>` not implemented for `fn(t1, ..., t17) -> ...`。因为 axum 只有 0~16 参数的 impl(`all_the_tuples!` 16 行 + 手写 0 参数),17 参数没匹配的 impl。修复:用 tuple 提取器把多个参数打包(`async fn h((a, b, c): (T1, T2, T3), ...)`),或者重构 handler(参数太多说明设计有问题)。

**坑 3(更隐蔽):参数类型没实现 Send**

```rust
// 错误写法:某参数类型不是 Send
async fn bad(RcCell: Rc<RefCell<X>>) -> String { ... }  // Rc 不是 Send
```

宏约束 `$ty: FromRequestParts<S> + Send`(或 `$last: FromRequest<S, M> + Send`),`Rc<RefCell<X>>` 不 Send,编译错。axum 的 future 要跨线程 spawn(承 Tokio 运行时),所有提取器类型都要 Send。这个约束是硬性的,不能用 `Rc`(用 `Arc`)。

> **钉死这两个坑**:① body 消费提取器(Json/Form/String/Bytes)必须放最后;② 最多 16 参数。这两个是新手最容易踩的,理解了宏约束(FromRequestParts vs FromRequest,0~16 参数)就明白为什么。

---

## 章末小结

回到全书的主轴:**路由与分发 vs 提取与响应**。

本章拆的是**提取与响应这一面的地基招牌**——Handler trait 怎么用宏把任意 async fn 在编译期变 Service。具体讲了:

- **Handler<T, S> trait 的签名**(第二节):T 是占位 tuple,S 是 state;call 签名 `(self, req, state)` 和 Service 的 `(&mut self, req)` 不一样,中间靠 HandlerService 适配。
- **T 参数是 coherence 占位 tuple**(第三节):让 17 份 impl(0~16 参数)trait 头签名不同,绕开孤儿规则。
- **impl_handler! + all_the_tuples! 宏展开**(第四节):模板 + 16 行手写调用,生成 17 份 impl,纯编译期零开销。
- **call 内部提取器链状态机**(第五节):拆 req → 逐个 FromRequestParts → 重组 → 最后 FromRequest → 调 fn。sound 三保证:body 只消费一次、编译期保证顺序、类型推断精确。
- **HandlerService 把 Handler 包成 Service**(第六节):Clone handler + Clone state + 调 Handler::call + 包 Result。poll_ready 无条件 Ready(async fn always ready)。
- **Layered 给单个 handler 套 Layer**(第七节):Handler::layer 返回 Layered,Layered 也是 Handler,可链式组合。
- **演进史 + 跨语言对照**(第九节、第十节):axum 0.6 起 RPITIT;对照 actix/rocket/go,axum 的"零注解 + 编译期生成"是最纯粹的声明式 handler。

Handler trait 是 axum 整个声明式人体工学的技术底座——从这里开始,你才真正看到 axum 怎么用 Rust 的泛型 + 宏展开,把"任意 async fn"变成"满足 Service trait 的对象",零运行时开销,编译期类型安全。

### 五个为什么清单

1. **为什么 Handler<T, S> 有两个类型参数?** T 是占位 tuple(编码参数个数,绕开孤儿规则),S 是 state 类型(handler 处理请求需要的 state,承 P1-04)。两个参数都给宏展开的 impl 用,用户写代码时永远不直接写 `Handler<...>`。
2. **为什么 T 是 tuple 而不是别的类型?** tuple 的"长度可变"让 17 份 impl 共享一个形状模式(`(M, T1, ..., TN)`),配合宏 16 行手写就生成全部。如果用 struct,要为每个参数个数定义一个 struct,冗长。tuple 是 Rust 类型系统能给出的最优雅"变长类型参数列表"。
3. **为什么最后一个参数才能 FromRequest(消费 body)?** body 是 Stream,只能消费一次。宏约束"前 n-1 个 FromRequestParts(只读 parts)、最后一个 FromRequest(消费 body)",编译期保证 body 不被重复消费。你写错参数顺序(Json 放中间)编译就错。
4. **为什么 Handler 和 Service 的 call 签名不一样?** Handler 是 axum 内部 trait(知道 state),`call(self, req, state)`;Service 是 Tower/hyper 外部 trait(签名钉死),`call(&mut self, req)`。中间靠 HandlerService 适配(Clone handler、Clone state、调 Handler::call、包 Result)。这个适配是"axum 长在 Tower 上"的关键接口。
5. **为什么最多 16 参数?** `all_the_tuples!` 手写 16 行(1~16 参数)+ 手写 0 参数,一共 17 份 impl。16 是工程取舍(标准库 tuple 最大 12 元素,axum 选 16 留余量;超过 16 参数的 handler 几乎不存在)。如果你想写 17 参数,用 tuple 提取器打包多个参数,或重构 handler。

### 想继续深入往哪钻

- **FromRequest vs FromRequestParts 的二元划分、ViaParts marker 桥接、Result/Option 提取器自动桥接**:→ 第 10 章(P3-10),提取器二元划分招牌章,本章的"前 n-1 个 FromRequestParts、最后一个 FromRequest"在那章拆透。
- **Path/Query/State/Json/Form 具体怎么实现提取、各自 Rejection 长啥样**:→ 第 11 章(P3-11),提取器实战章。
- **handler 报类型错怎么办、`#[axum::debug_handler]` 怎么改善错误信息、自定义提取器步骤**:→ 第 13 章(P3-13),自定义提取器 + debug_handler 宏章。
- **Layered 怎么和 ServiceBuilder 配合、四种 layer 作用域(Handler::layer/Router::layer/route_layer/MethodRouter::layer)**:→ 第 16 章(P4-16),中间件链招牌章。
- **`opaque_future!` 宏怎么把 type alias 转成 struct(IntoServiceFuture 的生成机制)**:→ `axum/src/macros.rs#L4-L46`,本章一句带过,深入看源码。
- **Handler trait 0.x 的 async-trait→RPITIT 改造**:→ 第 20 章(P6-20),axum 演进史章。
- **对照 actix-web 的 Handler(Actor + FromRequest 手写)、rocket 的 request guard(过程宏注入)、go net/http(全手写)**:→ 第 21 章(P7-21),全书收束双对照章。

### 引出下一章

本章你拿到了 axum 的招牌:Handler trait 怎么用宏把任意 async fn 编译期变 Service。但你一定有个问号悬着——"前 n-1 个 FromRequestParts、最后一个 FromRequest"这个二元划分,具体怎么工作?ViaParts marker 桥接是什么?为什么 `Json<T>` 不实现 FromRequestParts?为什么 `Result<T, Rejection>` 和 `Option<T>` 能自动当提取器?这些问题,下一章 P3-10 会用真实源码 + 反例彻底拆开。那是 axum 提取器链的另一半灵魂,和本章合成完整的"提取与响应"地基。

---

> **本章源码锚点(全部经本地 `../axum/` Grep/Read 核实,版本 axum-v0.8.9 @ c59208c86fded335cd85e388030ad59347b0e5ae)**:
>
> - [Handler trait 定义](../axum/axum/src/handler/mod.rs#L145-L205) —— `<T, S>` 两个类型参数,supertrait `Clone + Send + Sync + Sized + 'static`,`call(self, req, state)`,`layer`/`with_state` 默认方法。
> - [0 参数手写 impl Handler<((),), S> for F](../axum/axum/src/handler/mod.rs#L207-L219) —— 单独手写,不走宏。
> - [impl_handler! 宏定义](../axum/axum/src/handler/mod.rs#L221-L260) —— 模板,`[$($ty:ident),*], $last:ident`,生成 N 参数(1~16)impl。
> - [all_the_tuples!(impl_handler) 调用点](../axum/axum/src/handler/mod.rs#L262) —— 单行调用。
> - [private::IntoResponseHandler marker](../axum/axum/src/handler/mod.rs#L264-L268) —— 空私有 enum。
> - [impl Handler<IntoResponseHandler, S> for T where T: IntoResponse](../axum/axum/src/handler/mod.rs#L270-L280) —— 让 IntoResponse 类型直接当 handler。
> - [Layered 结构体](../axum/axum/src/handler/mod.rs#L285-L289) —— 泛型 `<L, H, T, S>`,字段 layer/handler/_marker。
> - [impl Handler for Layered](../axum/axum/src/handler/mod.rs#L316-L350) —— Layered::call 用 oneshot 跑套了 Layer 的 Service。
> - [HandlerWithoutStateExt trait](../axum/axum/src/handler/mod.rs#L352-L378) —— 给无 state handler 的便利方法(into_service 等)。
> - [all_the_tuples! 宏定义](../axum/axum/src/macros.rs#L48-L68) —— 手写 16 行,不是递归,`#[rustfmt::skip]`。
> - [HandlerService 结构体](../axum/axum/src/handler/service.rs#L22-L26) —— `<H, T, S>`,字段 handler/state/_marker。
> - [HandlerService::new(pub(super))](../axum/axum/src/handler/service.rs#L118-L126) —— 由 Handler::with_state 调用。
> - [impl Service<Request<B>> for HandlerService](../axum/axum/src/handler/service.rs#L148-L178) —— poll_ready 无条件 Ready(L161-L163 注释 "async fn always ready"),call Clone handler + Clone state + 调 Handler::call + 包 Result。
> - [IntoServiceFuture(opaque_future! 生成)](../axum/axum/src/handler/future.rs#L11-L18) —— `Map<F, fn(Response) -> Result<Response, Infallible>>`。
> - [LayeredFuture(pin_project! 生成)](../axum/axum/src/handler/future.rs#L20-L29) —— `Map<Oneshot<S, Request>, fn(Result<S::Response, S::Error>) -> Response>`。
> - [FromRequestParts trait 定义](../axum/axum-core/src/extract/mod.rs#L50-L63) —— `from_request_parts(parts: &mut Parts, state: &S) -> impl Future<...> + Send`。
> - [FromRequest<S, M = ViaRequest> trait 定义](../axum/axum-core/src/extract/mod.rs#L76-L89) —— `from_request(req: Request, state: &S) -> impl Future<...> + Send`,默认 M = private::ViaRequest。
> - [桥接 impl FromRequest<S, ViaParts> for T where T: FromRequestParts](../axum/axum-core/src/extract/mod.rs#L91-L105) —— 关键 blanket impl,拆 req 丢 body 调 from_request_parts。
> - [ViaParts/ViaRequest marker](../axum/axum-core/src/extract/mod.rs#L31-L37) —— 私有空 enum。
> - [Result<T, T::Rejection> 的 FromRequestParts impl](../axum/axum-core/src/extract/mod.rs#L107-L117) —— Infallible Rejection,Ok 包装。
> - [Result<T, T::Rejection> 的 FromRequest impl](../axum/axum-core/src/extract/mod.rs#L119-L129) —— 同上,ViaRequest 模式。
>
> **承接**:
>
> - **Service trait** 承《hyper》P1-02(`call(&self, Request) -> Future`,hyper 删 poll_ready)、承《Tower》P0-01(`call(&mut self, Request) -> Future` + `poll_ready` 背压),本章一句带过。
> - **FromRequest/FromRequestParts 的二元划分、ViaParts 桥接、Result/Option 提取器自动桥接** 本章只用、深度留 P3-10。
> - **State 提取器 + FromRef** 承 P1-04(本章一句带过)。
> - **hyper 把 Request 交上来** 承《hyper》P2-P3(协议机一句带过)。
> - **Tokio async fn→Future** 一句带过。
> - **中间件链 Layered × ServiceBuilder** 本章一句带过,深度留 P4-16。
> - **#[axum::debug_handler] 改善错误信息** 本章一句带过,深度留 P3-13。
>
> **修正总纲/常见误解几处**:
>
> 1. **0 参数的 T 是 `((),)` 不是 `()`**——单元素 tuple 含 unit,不是裸 unit(`mod.rs#L208` 核实)。
> 2. **`Handler<T, S>` 没有默认泛型参数**——`mod.rs#L148` 是 `pub trait Handler<T, S>:` 裸泛型,不是 `Handler<T, S = ()>`。
> 3. **`all_the_tuples!` 不是递归宏**,是手写 16 行 `$name!([已累积类型], $last)` 调用(`macros.rs#L48-L68` 核实);它只支持一个 arm `($name:ident)`,没有 `{...}`/`pub` 变体。
> 4. **`Layered` 泛型顺序是 `<L, H, T, S>`**,没有 `B`(`mod.rs#L285` 核实);`B` 是 `HandlerService` impl Service 时才出现的 body 泛型参数。
> 5. **"async fn is always ready" 注释在 `service.rs#L161-L163`**,不在 `mod.rs`。原文是 "`IntoService` can only be constructed from async functions which are always ready, or from `Layered` which buffers in `<Layered as Handler>::call` and is therefore also always ready."
> 6. **Handler trait 的 supertrait 含 `Sync`**——`Clone + Send + Sync + Sized + 'static`(`mod.rs#L148`),不是只 `Send`。
> 7. **`IntoResponseHandler` 是私有空 enum 在 `mod.rs#L264-L268` 的 `mod private`** 里,作为 marker(和 ViaRequest/ViaParts 同思路)。
