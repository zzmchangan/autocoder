# 第 10 章 · FromRequest 与 FromRequestParts:提取器的二元划分

> **核心问题**:你写 `async fn create_user(State(db): State<Db>, Path(id): Path<u32>, Json(payload): Json<CreateUser>) -> impl IntoResponse`,三个提取器并列摆在参数列表里,看起来"地位平等"。可源码里它们根本不是一类东西——`State` 和 `Path` 实现的是 `FromRequestParts`(只读 `&mut Parts`,可以跑任意多次),`Json` 实现的是 `FromRequest`(按值拿走整个 `Request`,body 只能被消费一次)。为什么 axum 要把提取器一刀切成两个 trait?那个 `FromRequest<S, M = ViaRequest>` 上的第二个类型参数 `M` 是干什么用的?为什么 `impl FromRequest<S, ViaParts> for T where T: FromRequestParts` 这个 blanket impl 一定要存在,而且它选的是 `ViaParts` 这个**空 enum** 而不是一个普通 struct marker?为什么 `from_request` / `from_request_parts` 的返回类型是 `impl Future<...> + Send` 而不是 `#[async_trait]` 那种 `Pin<Box<dyn Future>>`?——这些问题合起来,就是"axum 提取器凭什么 sound、凭什么零成本、凭什么让任意 `async fn` 都能当 handler"的全部地基。
>
> **读完本章你会明白**:
>
> 1. 为什么 `http::Request` 拆开是 `Parts`(method/uri/headers/extensions 的可复用镜像)+ `Body`(一个只能往前拉一次的 `Stream`),以及这个不对称怎么决定 axum 必须把提取器分成"只读 parts 可多次跑"和"消费 body 只能一次"两类——**这是二元划分的物理根**;
> 2. `FromRequestParts<S>`(签名 `fn from_request_parts(parts: &mut Parts, state: &S) -> impl Future<...>`)和 `FromRequest<S, M = ViaRequest>`(签名 `fn from_request(req: Request, state: &S) -> impl Future<...>`)各自承担什么、为什么 `&mut Parts` 可以被多个提取器接力共享、为什么 `Request` 必须按值交出;
> 3. `M = ViaRequest` 这个默认参数和那个 `impl FromRequest<S, private::ViaParts> for T where T: FromRequestParts` 的 blanket impl 在干什么——它让任何只读 parts 的提取器(比如 `Path`)自动获得 `FromRequest<S, ViaParts>` 的实现,从而能站到 `impl_handler!` 宏的"最后一个参数"位置上;以及为什么 `ViaParts` / `ViaRequest` 是两个**永远构造不出来的空 enum**(不是普通 marker struct),这个细节比你想得更重要;
> 4. 为什么 axum 0.8 把 `#[async_trait]` 换成了 `impl Future<...> + Send`(RPITIT,PR #2308,MSRV 1.75),以及这个改动怎么把每个提取器调用上的 `Box::pin` 一次堆分配省掉了——这是 axum 演进史里最值的一笔性能账;
> 5. `Result<T, Rejection>`(Rejection 自动变 `Infallible`)、`Option<T>`(走单独的 `OptionalFromRequest` / `OptionalFromRequestParts` trait)、tuple(同时 impl 两个 trait)这三套自动桥接是怎么在不动用户代码的前提下,把"提取失败"这件事从"返回 4xx"分别改成"传 `Err` 进 handler"和"传 `None` 进 handler"的。
>
> 本章与 P3-09 并列,是全书"**提取与响应**"这一面的招牌章。P3-09 把 `Handler<T, S>` trait 的 `T` 占位 tuple + `impl_handler!` 宏展开拆透了——宏对前 N-1 个参数要求 `FromRequestParts<S>`、对最后一个参数要求 `FromRequest<S, M>`,这个约定是 P3-10 的入口。本章要回答的是:**这两个 trait 凭什么这么划分、为什么 sound、那个 `M` 是什么、RPITIT 改了什么**。P3-09 立了 Handler 的地基,本章补全链上每个提取器用的两个 trait。
>
> **写给谁读**:你写过 `async fn(State, Path, Json) -> impl IntoResponse`,知道这三个参数"自动从 Request 来",但你讲不清:为什么 `Json` 只能放最后一个、`Path` 放哪都行?为什么有时候 `async fn handler(Json<_>, Json<_>)` 编译过、运行却出问题?为什么你给自定义提取器 impl 了 `FromRequestParts` 之后,它就能单独当 handler 的唯一参数,根本不用再 impl `FromRequest`?这一章治这些"会用没懂"。
>
> **前置衔接**:上一章(P3-09)拆 `impl_handler!` 宏时,你看到了关键的一行约束——`$last: FromRequest<S, M> + Send`(最后一个参数)、`$ty: FromRequestParts<S> + Send`(其余参数)。当时留了三个问号:`FromRequest` 和 `FromRequestParts` 凭什么要分成两个?那个 `M` 是什么?为什么最后一个特殊?这一章把这三个问号全部拆开。
>
> **逃生阀(读不下去怎么办)**:本章类型密度大——两个 trait、一个 marker 类型参数、一个 blanket impl、两个空 enum、三套桥接、一套 RPITIT 改造。如果一次读不下来,记住三句话就够往下走:① **`FromRequestParts` 只读 parts(请求头/路径/query),可以多个提取器接力共享;`FromRequest` 可能消费 body,只能跑一次**——这就是为什么宏把"可能消费 body 的提取器"放最后一个。② **`M` 是 marker,把"这个类型是通过 parts 还是 body 提的"编码进类型;`ViaParts` blanket impl 让只读 parts 的提取器自动也是 `FromRequest`,所以能放最后一个参数**。③ **`impl Future` 不是 `#[async_trait]`,是 RPITIT,零开销**。带着这三句跳到第四节看 blanket impl、第六节看 RPITIT,再回头读细节。本章处处承《hyper》(Body as Stream)、《Tokio》(Future/Poll),读过那两本收获翻倍,但不是硬性前提。

---

## 一句话点破

> **axum 把提取器一刀切成两个 trait,不是因为"API 好看",是因为 `http::Request` 物理上就是 `Parts` + `Body` 两块——`Parts` 可以被 `&mut` 共享给任意多个提取器接力读,`Body` 是一个只能往前拉一次的 `Stream`、谁先消费谁独占。`FromRequestParts` 拿 `&mut Parts`(只读、可多次跑、Path/Query/State/Headers 都在这类),`FromRequest` 按值拿 `Request`(可消费 body、只能一次、Json/Form/Bytes/String 都在这类);`impl_handler!` 宏把"可能消费 body 的"钉在最后一个参数,前 N-1 个只读 parts,编译期就保证 body 不被重复消费。中间那个 `M = ViaRequest` marker 类型参数加上 `impl FromRequest<S, ViaParts> for T where T: FromRequestParts` 的 blanket impl,让"只读 parts 的提取器也能站到最后一个参数位置",这样你写 `async fn handler(Path(id): Path<u32>)`(只有 Path 一个参数)也能编译过——`Path` 经桥接自动获得 `FromRequest<S, ViaParts>`,`M` 被推断成 `ViaParts`。这套设计让"body 不被重复消费"这件事编译期钉死,运行时不可能出错。**

这是结论,不是理由。本章倒过来拆五件事:① `http::Request` 为什么物理上就是 `Parts` + `Body` 这种不对称的两块(承《hyper》);② 这两块的不对称怎么逼出 `FromRequestParts` 和 `FromRequest` 两个 trait;③ `M` marker 为什么必须存在,以及 `ViaParts` / `ViaRequest` 为什么是空 enum 而不是 struct;④ `Option` / `Result` / tuple 三套自动桥接;⑤ RPITIT 改造到底省了什么。

---

## 第一节:从 P3-09 的 `from_request_parts` / `from_request` 说起

### 提问

P3-09 拆 `impl_handler!` 宏时,我们贴过这一段([`axum/src/handler/mod.rs#L221-L260`](../axum/axum/src/handler/mod.rs#L221-L260),逐字摘录关键部分):

```rust
macro_rules! impl_handler {
    ([$($ty:ident),*], $last:ident) => {
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

宏对 1~16 个参数全部展开。每个展开里,你能看到三件硬规矩:

1. **`req.into_parts()`** —— 把 `Request` 一刀切成 `(mut parts, body)`,这是入口动作。
2. **前 N-1 个参数**逐个调 `$ty::from_request_parts(&mut parts, &state).await`——注意是 `&mut parts`,所有提取器共享同一个 `&mut Parts`,接力改它。
3. **最后一个参数**调 `$last::from_request(req, &state).await`——注意这里先把 `parts` 和 `body` 拼回 `Request`(`Request::from_parts(parts, body)`),再**按值**交给 `from_request`。

当时留了三个问号:`from_request_parts` 凭什么拿 `&mut Parts`、`from_request` 凭什么按值拿 `Request`、那个 `$last: FromRequest<S, M>` 里的 `M` 是什么。这一章把这三个问号拆透,先把第一根线拉出来——`Request` 为什么物理上就是 `Parts` + `Body` 两块。

> **承接 P3-09**:P3-09 拆了 `Handler<T, S>` 的 `T` 占位 tuple 和 `impl_handler!` 宏的展开骨架,点出了"前 N-1 个 `FromRequestParts` + 最后一个 `FromRequest<S, M>`"的约定。本章补全这条约定背后的全部细节——为什么是两个 trait、`M` 是什么、为什么 sound。P3-09 是 Handler 地基,本章是提取器地基,两章合起来才是"async fn 凭什么当 handler"的完整答案。

### `http::Request` 物理上就是 `Parts` + `Body`(承《hyper》)

axum 收到的 `Request`,类型是 `axum_core::Request`(默认 body 类型 `Body`)。它其实就是 `http::Request<axum_core::body::Body>` 的别名([`axum-core/src/extract/mod.rs#L29`](../axum/axum-core/src/extract/mod.rs#L29)):

```rust
pub type Request<T = Body> = http::Request<T>;
```

`http::Request` 这个类型(在 `http` crate,hyper 也用它)有一个招牌方法 `into_parts()`,它把一个 `Request<B>` 拆成 `(Parts, B)`。这是 axum 提取器二元划分的物理根,所以必须先看清 `Parts` 和 `Body` 各自是什么。

```text
                           http::Request<B>
                          ┌──────────────────────────────────────────────┐
                          │                                              │
                          │   ┌───────────────────────────────────────┐  │
                          │   │ Parts(可独立持有、可 clone 镜像)       │  │
                          │   │                                       │  │
                          │   │   method:  Method        (Copy)       │  │
                          │   │   uri:      Uri          (Clone)      │  │
                          │   │   version:  Version      (Copy)       │  │
                          │   │   headers:  HeaderMap    (Clone)      │  │
                          │   │   extensions: Extensions (可 &mut)    │  │
                          │   │   (私有的 body 取走后的标记)            │  │
                          │   └───────────────────────────────────────┘  │
                          │                  +                            │
                          │   ┌───────────────────────────────────────┐  │
                          │   │ B = Body(只能往前拉一次的 Stream)       │  │
                          │   │                                       │  │
                          │   │   poll_frame(&mut self) -> Poll<Option>│  │
                          │   │   ↑ 每次 .await 拿一个 Frame<Data | Trailers>
                          │   │   ↑ chunk 拿走就没了,不能"倒带"        │  │
                          │   └───────────────────────────────────────┘  │
                          │                                              │
                          └──────────────────────────────────────────────┘
                                  into_parts() 把这两块拆开
```

这两块的不对称是关键。**`Parts` 是一组可复制的镜像**(method 是 `Copy`,uri/headers 是 `Clone`,extensions 可以 `&mut`),它本质上是"请求头那一坨元数据"的容器——你可以 clone 一份(`Parts` 实现了 `Clone`,见 [`axum-core/src/extract/request_parts.rs#L138-L148`](../axum/axum-core/src/extract/request_parts.rs#L138-L148) 那个 `impl FromRequestParts for Parts` 就是 `parts.clone()`),也可以把 `&mut Parts` 借给一个又一个提取器,大家接力改(比如 `Path` 往 `extensions` 里塞 URL 参数,下一个提取器从同一个 `extensions` 里读)。`Parts` 不持有 body,所以它可以被"看很多次",每次看都拿到一致(或者你故意修改后的新)状态。

**`Body` 完全是另一种东西**。它是一个 `Stream`——内部是异步的、有状态的(帧分帧状态机、chunked encoding 的剩余长度、可能压着的 trailer),你只能 `body.frame().await` 一次拿一个数据帧,拿走就没了。`Body` 没有"倒带",没有 `peek`,你不能"先读一遍试试,再读一遍解析"——一次拉完,就是它了。这是 HTTP body 的物理属性决定的(字节从 socket 流过来,内存里你不可能留着整段 body 的副本,否则大文件上传直接 OOM;hyper 选择"边收边吐",body 抽象成 Stream)。这一点《hyper》第二、第三篇拆得透——HTTP/1 的 body 分帧(手写 `ChunkedState` 状态机)、HTTP/2 的 body(h2 crate per-stream 的 DATA 帧)、Body as Stream 的全部语义,本章一句带过指路。对 axum 来说,只需要知道一件事:**`Body` 是只能消费一次的 Stream,谁先消费谁独占**。

> **承接《hyper》**:`http::Request` 是 hyper 解析完协议后给 axum 的结构化产物,`into_parts()` 是 `http` crate 提供的方法。body 的"Stream、只能往前拉一次"语义,根在 hyper 的协议机(HTTP/1 `ChunkedState` 13 态、HTTP/2 h2 per-stream DATA 帧),详见《hyper》P2(HTTP/1 body 分帧)+ P3(HTTP/2 body)。`axum_core::body::Body` 是包了 hyper body 的薄封装。本章不重讲这些,只承其结论"body 是只能消费一次的 Stream"。

### 不这样会怎样:如果只有一个 trait

假设 axum 只有 `FromRequest` 一个 trait,签名是 `fn from_request(req: Request, state: &S) -> impl Future<...>`(按值拿走整个 `Request`)。看起来"统一",听起来"简洁"。可一旦你坐下来写 `async fn create_user(State(db): State<Db>, Path(id): Path<u32>, Json(payload): Json<CreateUser>)`,问题立刻爆掉:

1. **第一个参数 `State<Db>` 跑 `from_request(req, state)`——它按值拿走了 `req`**。`State` 这个提取器其实根本不读 request,它只从 `state` 里 clone 一份 `Db`(承 P1-04),可签名逼它按值收下整个 `Request`(含 body)。它收下之后,要么把 body drop 掉(那第二个参数 `Path` 就拿不到 request 了)、要么把 request 再传出去(可它已经按值收下了,要么 move 出去要么 clone——clone 一整个 request 含 body 是大开销,而且 body 可能根本不能 clone)。
2. **如果第一个参数把 body drop 了,第二个参数 `Path` 拿什么?**——`Path` 只想读 uri 里的路径参数,根本不碰 body,可现在它连 request 都看不到。
3. **如果硬规定"所有提取器都不能 drop body、必须把 request 完整地传给下一个"**,那每个提取器都得写一堆"我收下了 request,但我只用 method/uri/headers,我没动 body,请把 request 还给我"的样板——而且 body 是 Stream,你不能 `&Body` 借着不动,你必须能 `&mut Body` 才能 `poll_frame`,可多个提取器同时持有 `&mut Body` 是 alias 错。
4. **更糟的是 body 被消费两次的隐患**:如果两个提取器都 `from_request(req, state)`,第一个把 body 收完(比如 `Bytes::from_request`),第二个再 `from_request` 时 body 已经空了——运行期要么拿到空 body、要么 panic。这是 axum 必须在类型层杜绝的事。

朴素实现的代价具体到什么程度?假设你写 `async fn h(a: State<_>, b: Path<_>, c: Query<_>, d: Json<_>)`,四个参数都走"按值 Request"的统一 trait:

- `a`(State)收下 Request,clone state,把 Request 传给 `b`——可 `a` 已经按值收下,要么 move 要么 clone,签名逼它做选择。
- 即便 `a` 把 Request move 给 `b`,`b`(Path)读 uri 后还要 move 给 `c`……一路 move 下去,任何中间提取器想"我先存一份 Request 等会儿再读"都不可能(所有权已经转走)。
- `d`(Json)终于轮到它消费 body,可它前面三个提取器都得参与"传递 Request"的游戏,签名复杂、错误率高。

这就是"单一 trait"的死法。axum 不能这么干。

### 所以 axum 把它一刀切两半

axum 的解法直接对应 `Parts` 和 `Body` 的物理不对称:**两个 trait**,`FromRequestParts` 拿 `&mut Parts`(只读 parts 那块,不碰 body),`FromRequest` 按 `&S`、按值拿 `Request`(可消费 body)。看真实定义([`axum-core/src/extract/mod.rs#L39-L89`](../axum/axum-core/src/extract/mod.rs#L39-L89)):

```rust
mod private {
    #[derive(Debug, Clone, Copy)]
    pub enum ViaParts {}

    #[derive(Debug, Clone, Copy)]
    pub enum ViaRequest {}
}

/// Types that can be created from request parts.
///
/// Extractors that implement `FromRequestParts` cannot consume the request body and can thus be
/// run in any order for handlers.
pub trait FromRequestParts<S>: Sized {
    type Rejection: IntoResponse;

    fn from_request_parts(
        parts: &mut Parts,
        state: &S,
    ) -> impl Future<Output = Result<Self, Self::Rejection>> + Send;
}

/// Types that can be created from requests.
///
/// Extractors that implement `FromRequest` can consume the request body and can thus only be run
/// once for handlers.
pub trait FromRequest<S, M = private::ViaRequest>: Sized {
    type Rejection: IntoResponse;

    fn from_request(
        req: Request,
        state: &S,
    ) -> impl Future<Output = Result<Self, Self::Rejection>> + Send;
}
```

先把这两段签名逐字读三遍,有几件事必须钉死:

1. **`FromRequestParts<S>::from_request_parts(parts: &mut Parts, state: &S)`**——只读 `&mut Parts`(注意是 `&mut`,不是 `&`;为什么允许 `&mut` 而不是 `&`,因为有些提取器要往 `parts.extensions` 里写东西,比如 URL 参数匹配后塞进 extensions 给后续 `Path` 提取器读——这个细节后面拆)。它**不接 body**,因为 `Parts` 里压根没有 body(`into_parts` 已经把 body 拆走了)。它返回 `impl Future<Output = Result<Self, Rejection>> + Send`。
2. **`FromRequest<S, M = ViaRequest>::from_request(req: Request, state: &S)`**——按值拿 `Request`(整个,含 body)。`Request` 一旦交给你,你想 `req.into_body()` 把 body 拿走、`req.into_parts()` 拆开、`Bytes::from_request(req, state)` 转手给别人,都行——你拥有这个 `Request` 的所有权。
3. **`FromRequest` 比 `FromRequestParts` 多一个类型参数 `M`**,默认 `M = ViaRequest`。这个 `M` 是 marker——它不参与方法签名,只在类型层标记"这个 `FromRequest` 是怎么实现的"。`axum-core` 自己只定义两个 marker 值:`ViaRequest` 和 `ViaParts`,都是空 enum(注意是 **enum**,不是 struct)。这是本章技巧精解的重点,第四节拆。
4. **两个 trait 都用 `impl Future<...> + Send` 作返回类型**——这是 RPITIT(Return Position Impl Trait In Trait,Rust 1.75 稳定),不是 `#[async_trait]`。axum 0.8 把 `#[async_trait]` 全面换成了 RPITIT,第六节拆这笔性能账。

把这两个签名对照 `impl_handler!` 宏展开的代码看一遍,你会看到 axum 的二元划分怎么落到地上:宏展开后,`req.into_parts()` 拆出 `(mut parts, body)`,前 N-1 个参数共享 `&mut parts` 接力跑 `from_request_parts`,最后一个参数把 `Request::from_parts(parts, body)` 拼回去、按值交给 `from_request`。两块各走各的,`Parts` 可共享、`Body` 独占最后一个。

> **钉死这件事**:`FromRequestParts`(只读 `&mut Parts`,可多次跑)vs `FromRequest`(按值拿 `Request`,可消费 body,只能一次)的二元划分,根在 `http::Request` 物理上就是 `Parts`(可复用的镜像)+ `Body`(只能消费一次的 Stream)的不对称。`FromRequestParts` 对应"读元数据"那批提取器(Path/Query/State/Headers/Method/Uri),`FromRequest` 对应"消费 body"那批提取器(Json/Form/Bytes/String/Request 本身)。`impl_handler!` 宏钉死"可能消费 body 的提取器"只能在最后一个参数位置,前 N-1 个只读 parts——这套设计编译期保证 body 不被重复消费。

---

## 第二节:FromRequestParts——只读 `&mut Parts`,可以跑任意多次

### 提问

`FromRequestParts` 这一面,先看清楚。它的签名刚才贴过:

```rust
pub trait FromRequestParts<S>: Sized {
    type Rejection: IntoResponse;
    fn from_request_parts(
        parts: &mut Parts,
        state: &S,
    ) -> impl Future<Output = Result<Self, Self::Rejection>> + Send;
}
```

三个细节要回答:① 为什么 `parts` 是 `&mut Parts` 而不是 `&Parts`?——既然叫"只读",为什么允许 `&mut`?② 为什么这个 trait 可以被"接力跑多次",而 `FromRequest` 不行?——可多次跑的"sound"在哪?③ 哪些提取器实现 `FromRequestParts`,它们各自怎么用 `&mut Parts`?

### 为什么是 `&mut Parts`,不是 `&Parts`

直觉上,"只读"应该是 `&Parts`(不可变借用)。但 axum 选了 `&mut Parts`。原因藏在一个具体场景里:**URL 参数匹配后,axum 要把匹配出来的路径参数(`{id}` 捕获到的值)塞进 `parts.extensions`,让后续的 `Path` 提取器从 `extensions` 里读出来**。

这件事发生在路由层(`PathRouter::call_with_state` 调 matchit 匹配路径后,把捕获的参数通过 `UrlParams` extractor state 塞进 `extensions`),早于提取器链。但更一般地,`&mut Parts` 还允许一种"提取器之间通过 extensions 互通消息"的模式——比如某个中间件先在 `from_extractor` 阶段往 `parts.extensions` 里塞一个 `CurrentUser`,后面的 `FromRequestParts` 提取器就能从 `extensions.get::<CurrentUser>()` 读出来。如果 parts 是 `&Parts`,extensions 不能 `&mut`,这种"提取器之间互通"就做不了。

`&mut Parts` 不破坏"可多次跑"的 sound,因为:`parts.method` / `parts.uri` / `parts.headers` 这些字段,提取器一般是**读后不改**(`Method` 提取器 `Ok(parts.method.clone())`,见 [`axum-core/src/extract/request_parts.rs#L19-L28`](../axum/axum-core/src/extract/request_parts.rs#L19-L28));`parts.extensions` 允许写,但写进去的通常是"给后续提取器用"的数据(URL 参数、CurrentUser),后续提取器读出来不影响前面提取器已经 clone 走的值。换句话说,**`&mut` 给的是"接力写 extensions"的能力,不是"修改 method/uri"的口子**——你硬要 `parts.method = Method::POST` 也能编译过,但这是反模式(axum 没有任何内置提取器这么干)。

看一组真实的 `FromRequestParts` 实现就明白这个分工。`Method` 提取器([`axum-core/src/extract/request_parts.rs#L19-L28`](../axum/axum-core/src/extract/request_parts.rs#L19-L28)):

```rust
impl<S> FromRequestParts<S> for Method
where
    S: Send + Sync,
{
    type Rejection = Infallible;

    async fn from_request_parts(parts: &mut Parts, _: &S) -> Result<Self, Self::Rejection> {
        Ok(parts.method.clone())
    }
}
```

`Method` 就是 clone 走一份 method。`Infallible` 表示"这个提取器不可能失败"(method 永远存在)。`HeaderMap` 提取器同理(`Ok(parts.headers.clone())`,[`axum-core/src/extract/request_parts.rs#L57-L66`](../axum/axum-core/src/extract/request_parts.rs#L57-L66))——把整个 header 表 clone 走一份。这两个都是"纯读,不改"。

`Path` 提取器(在 `axum` crate,实现在 [`axum/src/extract/path/mod.rs#L157`](../axum/axum/src/extract/path/mod.rs#L157))干的事是:从 `parts.extensions` 里读出路由层塞进去的 URL 参数(`UrlParams` 类型),交给 serde 反序列化成目标类型。它也是"读 extensions,不改"。`State` 提取器([`axum/src/extract/state.rs#L303`](../axum/axum/src/extract/state.rs#L303))更极端——它压根不读 `parts`,只 `state: &S` 上 clone 一份:

```rust
// (简化示意,逐字见 axum/src/extract/state.rs#L303-)
impl<OuterState, InnerState> FromRequestParts<OuterState> for State<InnerState>
where
    InnerState: FromRef<OuterState>,
{
    type Rejection = Infallible;

    async fn from_request_parts(_: &mut Parts, state: &OuterState) -> Result<Self, Self::Rejection> {
        Ok(State(FromRef::from_ref(state)))
    }
}
```

`State` 把 `parts` 参数直接 `_` 丢掉——它"是个 `FromRequestParts`"只是为了让 `impl_handler!` 宏统一处理(宏要求前 N-1 个参数都 `FromRequestParts`)。这是 P1-04 拆过的细节,本章只点出"`State` 是 `FromRequestParts` 但不读 parts 这个特殊点"。

### 为什么这个 trait 可以"接力跑多次"

可多次跑的 sound,在两点:

1. **`FromRequestParts` 拿的是 `&mut Parts`,不是按值 `Parts`**。前一个提取器跑完,`parts` 还在(只是可能被改了 extensions),后一个提取器接着用同一个 `&mut parts`。所有权没动。
2. **每个提取器返回的 `Self` 是它自己构造的独立值**(`Method` 是 `parts.method.clone()` 出来的一个新 `Method`,`HeaderMap` 是 clone 出来的新 `HeaderMap`),不持有 `parts` 的引用。这意味着提取器返回后,`parts` 可以继续被借给下一个提取器——没有生命周期纠缠。

对照 `FromRequest` 看:`FromRequest` 按值拿 `Request`,一旦某个提取器 `req.into_body()` 把 body 拿走,或者 `Bytes::from_request(req, state)` 把整个 body 收完,后续提取器就再也拿不到 body 了——`Request` 这个值已经被消费。这就是"只能跑一次"的物理根。

把两个 trait 的"可重入性"对照画成图:

```text
                     FromRequestParts(可多次跑)
   ┌──────────────────────────────────────────────────────────┐
   │                                                          │
   │    parts: &mut Parts  ◄── 借用,不转移所有权              │
   │           │                                              │
   │           ▼                                              │
   │    [Path 提取器]    ──► 返回 Self(Path<T>)                │
   │           │                  (独立值,不持 parts 引用)     │
   │           ▼                                              │
   │    [Query 提取器]   ──► 返回 Self(Query<T>)               │
   │           │                                              │
   │           ▼                                              │
   │    [State 提取器]   ──► 返回 Self(State<T>)               │
   │           │                                              │
   │           ▼                                              │
   │    parts 还在(可能 extensions 被改了),继续借给下一个     │
   │                                                          │
   └──────────────────────────────────────────────────────────┘

                     FromRequest(只能一次)
   ┌──────────────────────────────────────────────────────────┐
   │                                                          │
   │    req: Request  ◄── 按值,所有权转移进来                  │
   │           │                                              │
   │           ▼                                              │
   │    [Json 提取器]                                         │
   │       req.into_limited_body()  ◄── body 被拿走             │
   │       body.collect().await     ◄── body 全收完             │
   │       serde_json::from_slice   ◄── 反序列化                │
   │       ──► 返回 Self(Json<T>)                              │
   │                                                          │
   │    req 已经被消费,body 没了                                │
   │    再调 from_request(req, ...) ── 编译期: req 已 moved     │
   │                                                          │
   └──────────────────────────────────────────────────────────┘
```

`FromRequestParts` 那条线,`parts` 像一根接力棒,一个提取器传给下一个,每个提取器只读它需要的那一两个字节,然后返回独立值。`FromRequest` 那条线,`Request` 是一次性筹码,交给一个提取器就消费掉了。

### 内置的 `FromRequestParts` 都在干什么

把 axum 内置的 `FromRequestParts` 实现罗列一下,你能看到一个清晰的分工:

| 提取器 | 提的是什么 | 怎么提 | Rejection |
|---------|-----------|--------|-----------|
| `Method` | HTTP method | `parts.method.clone()` | `Infallible` |
| `Uri` | 完整 URI | `parts.uri.clone()` | `Infallible` |
| `Version` | HTTP 版本 | `parts.version`(Copy) | `Infallible` |
| `HeaderMap` | 所有 headers | `parts.headers.clone()` | `Infallible` |
| `Parts`(http 的) | 整个 parts 镜像 | `parts.clone()` | `Infallible` |
| `Extensions` | 所有 extensions | `parts.extensions.clone()` | `Infallible` |
| `Path<T>` | URL 路径参数 | 从 `parts.extensions` 读路由层塞的 `UrlParams`,serde 反序列化 | `PathRejection` |
| `Query<T>` | query string | 从 `parts.uri.query()` 解析,serde_urlencoded 反序列化 | `QueryRejection` |
| `State<T>` | 应用状态 | 不读 parts,`FromRef::from_ref(state)` 从 state clone | `Infallible` |
| `ConnectInfo<T>` | 对端地址 | 从 `parts.extensions` 读 `ConnectInfo`(由 `into_make_service_with_connect_info` 塞入) | `Infallible` |
| `MatchedPath` | 匹配到的路由模板 | 从 `parts.extensions` 读 | `MatchedPathRejection` |
| `OriginalUri` | 原始 URI(未 strip prefix) | 从 `parts.extensions` 读 | `Infallible` |
| `Extension<T>` | 单个 extension | `parts.extensions.get::<T>().cloned()` | `MissingExtension` |
| `RawQuery` | 原始 query 字符串 | `parts.uri.query().map(String::from)` | `Infallible` |

(这些实现的精确源码位置:P1-04 拆过 `State`;`Path` 在 [`axum/src/extract/path/mod.rs#L157`](../axum/axum/src/extract/path/mod.rs#L157);`Query` 在 [`axum/src/extract/query.rs#L53`](../axum/axum/src/extract/query.rs#L53);`Method`/`Uri`/`HeaderMap` 等基础类型在 [`axum-core/src/extract/request_parts.rs`](../axum/axum-core/src/extract/request_parts.rs)。)

看这张表你能总结出三类:

1. **直接 clone 一个 Copy/Clone 字段**(`Method`/`Uri`/`Version`/`HeaderMap`/`Parts`/`Extensions`)——零成本或一份 clone,Infallible。
2. **从 extensions 里读路由层/中间件塞进去的东西**(`Path`/`ConnectInfo`/`MatchedPath`/`OriginalUri`/`Extension`)——这些值不是请求本身带的,是 axum 在路由层或某个中间件里"塞进 extensions"的,提取器只是取出来。
3. **从 state 派生**(`State<T>`)——根本不看 parts,只看 state。

这三类都不碰 body,都能 `&mut Parts` 共享,都能跑任意多次。这就是 `FromRequestParts` 的领地。

> **钉死这件事**:`FromRequestParts` 用 `&mut Parts` 而不是 `&Parts`,是为了允许"提取器之间通过 extensions 互通消息"(路由层塞 URL 参数、中间件塞 CurrentUser 等)。`parts.method`/`parts.uri` 这些字段,内置提取器一律是"读后不改"。这个 trait 可多次跑的 sound 在两点:① `&mut` 是借用不转移所有权,提取器返回后 parts 还在;② 提取器返回的 `Self` 是独立值,不持 parts 引用。

---

## 第三节:FromRequest——按值拿 `Request`,可能消费 body

### 提问

`FromRequest` 这一面,签名重贴一遍:

```rust
pub trait FromRequest<S, M = private::ViaRequest>: Sized {
    type Rejection: IntoResponse;
    fn from_request(
        req: Request,
        state: &S,
    ) -> impl Future<Output = Result<Self, Self::Rejection>> + Send;
}
```

三个细节要回答:① 为什么按值拿 `Request`(不是 `&Request`、不是 `&mut Request`)?② `M = ViaRequest` 这个默认 marker 是什么?现在先讲 `ViaRequest` 这一面(具体类型实现 `FromRequest` 时,`M` 推断成默认值 `ViaRequest`),`ViaParts` 桥接留第四节。③ 哪些提取器实现 `FromRequest`(用 `ViaRequest` marker),它们各自怎么消费 body?

### 为什么按值拿 `Request`

`Body` 是 Stream,要 `poll_frame(&mut self)`——你必须能 `&mut Body` 才能 await 它。`&Request` 只能拿到 `&Body`,不能 poll;`&mut Request` 理论上能拿到 `&mut Body`,但 Rust 的 `&mut` 借用规则会让"多个提取器接力持有 `&mut Request`"无法成立(只能有一个 `&mut`,且生命周期纠缠)。最干净的解法是**按值拿 `Request`**——你拥有这个 `Request`,你想 `req.into_body()`(消耗 Request 拿 body)、`req.into_parts()`(消耗 Request 拆 parts+body)、`req.headers()`(只读 headers 不动 body)都随你,所有权清清楚楚。

代价是:这个 `Request` 一旦交出去,就没了。所以 `FromRequest` 提取器天然只能跑一次。axum 把这个"只能一次"的约束编码进宏——`impl_handler!` 钉死 `FromRequest` 只在最后一个参数位置。

### 内置的 `FromRequest`(走 ViaRequest marker)都在干什么

罗列内置的 `FromRequest` 实现(注意这些都是 `M = ViaRequest`,即直接 impl `FromRequest<S>` 不带 marker,第四节会看到 `ViaParts` 这另一面):

| 提取器 | 干的事 | 怎么消费 body | Rejection |
|---------|--------|--------------|-----------|
| `Request` | 整个 Request 原样返回 | 不消费,`Ok(req)` | `Infallible` |
| `Body` | 拿走 body | `req.into_body()` | `Infallible` |
| `Bytes` | body 全收成 `Bytes` | `req.into_limited_body().collect().await.to_bytes()` | `BytesRejection` |
| `BytesMut` | body 全收成 `BytesMut` | `body_to_bytes_mut` 循环 `frame().await` | `BytesRejection` |
| `String` | body 收完 + UTF-8 校验 | 复用 `Bytes::from_request`,再 `String::from_utf8` | `StringRejection` |
| `Json<T>` | body 收完 + Content-Type 校验 + serde_json | 复用 `Bytes::from_request`,再 `serde_json::from_slice` | `JsonRejection` |
| `Form<T>` | body 收完 + Content-Type 校验 + serde_urlencoded | 复用 `Bytes::from_request` | `FormRejection` |
| `RawForm` | body 收完(原样 bytes,只校验 Content-Type) | 同上 | `RawFormRejection` |

(精确实现:`Bytes` 在 [`axum-core/src/extract/request_parts.rs#L99-L115`](../axum/axum-core/src/extract/request_parts.rs#L99-L115);`String` 在 [`axum-core/src/extract/request_parts.rs#L117-L136`](../axum/axum-core/src/extract/request_parts.rs#L117-L136);`Json` 在 [`axum/src/json.rs#L99-L114`](../axum/axum/src/json.rs#L99-L114);`Request` 自身在 [`axum-core/src/extract/request_parts.rs#L8-L17`](../axum/axum-core/src/extract/request_parts.rs#L8-L17);`Body` 在 [`axum-core/src/extract/request_parts.rs#L162-L171`](../axum/axum-core/src/extract/request_parts.rs#L162-L171)。)

挑 `Json` 这个最典型的拆透,因为它最能体现"`FromRequest` = 消费 body + 一堆校验"的招牌模式。看真实实现([`axum/src/json.rs#L99-L114`](../axum/axum/src/json.rs#L99-L114)):

```rust
impl<T, S> FromRequest<S> for Json<T>
where
    T: DeserializeOwned,
    S: Send + Sync,
{
    type Rejection = JsonRejection;

    async fn from_request(req: Request, state: &S) -> Result<Self, Self::Rejection> {
        if !json_content_type(req.headers()) {
            return Err(MissingJsonContentType.into());
        }

        let bytes = Bytes::from_request(req, state).await?;
        Self::from_bytes(&bytes)
    }
}
```

三步:

1. **Content-Type 校验**:`json_content_type(req.headers())` 检查 `Content-Type: application/json`(或 `application/...+json` 这种 JSON 扩展类型)。不通过直接返回 `MissingJsonContentType`(默认映射到 415 Unsupported Media Type)。这一步只读 headers,不消费 body。
2. **body 收完**:`Bytes::from_request(req, state).await` 把 body 全收成 `Bytes`。这一步**消费 `req`**(按值传入),body Stream 被拉到尽头,所有帧拼成一段连续字节。
3. **反序列化**:`Self::from_bytes(&bytes)` 调 `serde_path_to_error::deserialize` 包装的 `serde_json::from_slice`,把字节反序列化成 `T`。失败按"数据错"(`JsonDataError`,400)或"语法错"(`JsonSyntaxError`,400)分类返回 rejection。

这三步合起来,就是"消费 body 的提取器"的招牌形态:**先用 headers 校验(不消费 body),再消费 body 收成 bytes,最后反序列化成目标类型**。`Form`、`RawForm`、`String` 都是类似套路(只是 Content-Type 要求不同、反序列化器不同)。

注意一个细节:`Json::from_request` 是直接 impl `FromRequest<S>`,**没标 `M`**——这意味着 `M` 用了默认值 `ViaRequest`。换句话说,`Json<T>: FromRequest<S, ViaRequest>`。`impl_handler!` 宏在处理 `async fn h(_: Json<T>)`(只有一个参数,这个参数是 `$last`)时,编译器看到 `$last: FromRequest<S, M>` 这个约束,会推断 `M = ViaRequest`(因为 `Json` 实现的是 `FromRequest<S, ViaRequest>`)。第四节会看到,如果最后一个参数是 `Path` 这种只读 parts 的提取器,`M` 会被推断成 `ViaParts`(经 blanket impl 桥接)——这是 `M` 这套设计的核心动机。

### 反面对比:actix-web 的单一 trait 怎么处理 body

为了感受 axum 二元划分的妙处,做个对照。actix-web 也有一个 `FromRequest` trait(注意名字一样,签名完全不同),它把 parts 和 body 揉在一个 trait 里:

```rust
// actix-web 的 FromRequest(简化示意,非源码原文,签名以 actix-web 0.13 文档为准)
pub trait FromRequest: Sized {
    type Error: Into<Error>;
    type Future: Future<Output = Result<Self, Self::Error>>;

    fn from_request(req: &HttpRequest, payload: &mut Payload) -> Self::Future;
}
```

(注:actix-web 的 `FromRequest` 签名以 actix-web 文档为准,本书不编行号。要点是它接 `&HttpRequest`(只读引用)+ `&mut Payload`(body 流的 `&mut`)——它没有把"只读 parts"和"消费 body"分成两个 trait。)

这个设计的后果:

1. **每个提取器都得处理 `payload`**——即便 `Path`(只读 URL 参数)这种压根不碰 body 的提取器,签名也逼它收下 `&mut Payload`。它要么忽略 payload(那 `Json` 想消费 body 时,payload 还在——但已经被前一个提取器持有过 `&mut`,谁保证它没动过?)、要么消费 payload(那它就不是"只读 parts"了)。
2. **body 重复消费的风险**:`Json` 这种要消费 body 的提取器,从 `&mut Payload` 里读 body。如果两个 `Json` 提取器并列(`async fn h(a: Json<A>, b: Json<B>)`),第一个读完 payload,第二个读到空——actix-web 靠"用户自己保证不写两个 body 提取器"来避免,运行期可能拿到空 body 或报错。axum 在编译期就杜绝(`impl_handler!` 要求前 N-1 个是 `FromRequestParts`,`Json` 不是 `FromRequestParts`,所以 `async fn h(Json<_>, Json<_>)` 编译过不去)。
3. **没有 parts 共享的清晰通道**:actium 的 extensions 在 `HttpRequest` 里(通过 `&HttpRequest` 共享),修改 extensions 要走另一套机制。axum 直接 `&mut Parts.extensions`,干净。

axum 的二元划分把"能不能消费 body"这件事编码进 trait 选择:`FromRequestParts` 一定不碰 body,`FromRequest` 可能消费 body。宏的"最后一个参数 FromRequest"约定,加上"`Json` 不是 `FromRequestParts`"这个事实,让"两个 body 提取器并列"在编译期就被拦下。这是类型层杜绝 body 重复消费的根。

> **钉死这件事**:`FromRequest` 按值拿 `Request`(不是 `&mut`),因为 body 是 Stream 要 `poll_frame(&mut self)`,按值拿最干净。`FromRequest` 提取器的招牌形态是"先校验 headers(不消费 body),再消费 body 收成 bytes,再反序列化"——`Json`/`Form`/`String` 都是这个套路。actix-web 把 parts 和 body 揉在一个 trait,靠用户自觉避免 body 重复消费;axum 用二元划分 + 宏约定,在编译期杜绝。

---

## 第四节:ViaParts 桥接——为什么 `Path` 能当唯一参数

### 提问

到这儿你看出一个问题。`impl_handler!` 宏要求**最后一个参数** `$last: FromRequest<S, M>`。可如果 handler 长这样:

```rust
async fn handler(Path(id): Path<u32>) -> impl IntoResponse { /* ... */ }
```

只有一个参数,它就是 `$last`。可 `Path<u32>` 实现的是 `FromRequestParts`(它不消费 body),**根本没实现 `FromRequest<S, ViaRequest>`**(它没标 `M`,没写 `impl FromRequest for Path`)。那这个 handler 怎么编译过?宏要求 `$last: FromRequest<S, M>`,可 `Path` 只有 `FromRequestParts` 啊。

这就是 `M` 这套设计的核心动机,也是 axum 类型系统最巧的一笔——`ViaParts` blanket 桥接。

### `M` 类型参数:把"怎么实现 FromRequest"编码进类型

先看 `FromRequest` 的完整签名,带 `M`:

```rust
pub trait FromRequest<S, M = private::ViaRequest>: Sized {
    type Rejection: IntoResponse;
    fn from_request(req: Request, state: &S)
        -> impl Future<Output = Result<Self, Self::Rejection>> + Send;
}
```

`M` 是个类型参数,默认 `ViaRequest`。它在方法签名里**完全不出现**(`from_request` 的签名只用了 `S`、`Self`、`Request`)。它的唯一作用是**在类型层区分"这个 `FromRequest` 是怎么来的"**——是类型直接 impl 的,还是通过 blanket impl 桥接的。

axum 定义两个 marker 值,在 `mod private` 里([`axum-core/src/extract/mod.rs#L31-L37`](../axum/axum-core/src/extract/mod.rs#L31-L37)):

```rust
mod private {
    #[derive(Debug, Clone, Copy)]
    pub enum ViaParts {}

    #[derive(Debug, Clone, Copy)]
    pub enum ViaRequest {}
}
```

这两个 marker 都是**空 enum**(empty enum,没有任何变体)。`ViaRequest` 是默认 marker——任何**直接** impl `FromRequest<S>` 的类型(`Json`/`Form`/`Bytes` 等),`M` 推断成 `ViaRequest`。`ViaParts` 是桥接 marker——任何**通过 blanket impl** 间接获得 `FromRequest<S>` 的类型(`Path`/`Query`/`State` 等只读 parts 的提取器),`M` 推断成 `ViaParts`。

> **为什么是空 enum 而不是 struct?** 这是个细节,但很值得讲。空 enum(`enum ViaParts {}`,没有变体)在 Rust 里是**永远构造不出值**的类型(它没有 `()` 这种唯一值,也不占内存,更不能被实例化)。axum 选空 enum 而不是 `pub struct ViaRequest;`(unit struct,有唯一值 `()`)作 marker,意义在于:**`M` 永远不会有真实值**——`from_request` 方法签名里没有 `M` 参数,`M` 纯粹是编译期的类型标记,运行时不存在任何 `ViaRequest` / `ViaParts` 的实例。空 enum 强化了"这是编译期 phantom marker"的语义,虽然 unit struct 也能做到(`PhantomData<ViaRequest>` 那种),空 enum 更直白地表达"这个类型永远不出现一个值"。这是 axum 选空 enum 的理由(核 `#[derive(Debug, Clone, Copy)]` 这两个 derive 对空 enum 也合法,只是这些 trait impl 永远不会被实际调用)。

### `ViaParts` blanket impl:让 `FromRequestParts` 自动满足 `FromRequest`

看 blanket impl 原文([`axum-core/src/extract/mod.rs#L91-L105`](../axum/axum-core/src/extract/mod.rs#L91-L105)):

```rust
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

这个 blanket impl 在说:**任何实现了 `FromRequestParts<S>` 的类型 `T`,自动也实现 `FromRequest<S, ViaParts>`**。它的 `from_request` 实现很简单——把 `Request` 拆成 `(parts, _)`,**把 body 直接 `_` 丢掉**(注意这个丢弃),然后调 `Self::from_request_parts(&mut parts, state)`(只读 parts,不碰已经丢掉的 body)。

所以 `Path<u32>` 虽然没直接 impl `FromRequest`,但通过这个 blanket impl 自动获得 `FromRequest<S, ViaParts>` 的实现——它的 `from_request` 等价于"拆 parts、丢 body、跑 from_request_parts"。`Rejection` 类型直接复用 `FromRequestParts::Rejection`(`PathRejection`),没有引入新错误类型。

### 这套设计在 `impl_handler!` 里怎么落地

回到 `async fn handler(Path(id): Path<u32>) -> impl IntoResponse`。`impl_handler!` 宏展开后,约束是 `$last: FromRequest<S, M>`,这里 `$last = Path<u32>`。编译器要找一个 `M` 让 `Path<u32>: FromRequest<S, M>` 成立。两个候选:

- `M = ViaRequest`(默认):`Path` 没直接 impl `FromRequest<S, ViaRequest>`,这个不成立。
- `M = ViaParts`(blanket impl):`Path` 实 `FromRequestParts<S>`,经 blanket 自动获得 `FromRequest<S, ViaParts>`,这个成立。

编译器推断 `M = ViaParts`。宏展开的代码变成(简化):

```rust
// (展开后的概念示意,非源码原文)
let req = Request::from_parts(parts, body);  // parts 和 body 拼回 req
let path = match Path::<u32>::from_request(req, &state).await {
    // 这里调的实际上是 ViaParts blanket impl 的 from_request
    // 它内部: req.into_parts() 拆出 (parts, _),把 body 丢掉,调 Path::from_request_parts
    Ok(value) => value,
    Err(rejection) => return rejection.into_response(),
};
self(path).await.into_response()
```

注意一个看起来浪费的细节:宏代码先 `Request::from_parts(parts, body)` 把 parts 和 body 拼回 `Request`,然后 blanket impl 的 `from_request` 又 `req.into_parts()` 把它拆开,再把 body 丢掉。这是两次 `into_parts` / `from_parts` 的来回——看起来白干。这是 axum 为了"统一调用接口"(宏生成的代码统一调 `from_request`)付的一点小开销,编译器优化后基本消除(`from_parts`/`into_parts` 都是零拷贝的字段移动,`body` 被 drop 也是零开销)。axum 选择了"统一接口"而不是"分支优化",这是工程取舍。

### 反面对比:如果没有 `M`、没有 blanket impl,会怎样

假设 axum 把 `FromRequest` 的 `M` 参数删掉,签名简化成 `trait FromRequest<S>`,同时也没有那个 `impl FromRequest<S> for T where T: FromRequestParts` 的 blanket impl。会出现两个问题:

1. **`async fn handler(Path(id): Path<u32>)` 编译不过**——`Path` 只 impl `FromRequestParts`,没 impl `FromRequest`,不满足宏的 `$last: FromRequest` 约束。用户必须给 `Path` 单独手写一份 `impl FromRequest for Path`(内部就是丢 body + 调 from_request_parts),或者宏在"最后一个参数是 `FromRequestParts`"时生成两套不同的代码(分支)。前者样板爆炸(每个 `FromRequestParts` 类型都要重复写一份 `FromRequest`),后者宏复杂度爆炸。
2. **更糟的是 tuple impl 的冲突**。看 `tuple.rs` 里 tuple 的实现([`axum-core/src/extract/tuple.rs#L46-L73`](../axum/axum-core/src/extract/tuple.rs#L46-L73))。tuple `($($ty,)* $last,)` 同时 impl `FromRequestParts`(所有元素都 `FromRequestParts`)和 `FromRequest`(前 N-1 个 `FromRequestParts` + 最后一个 `FromRequest`)。如果 `FromRequest` 没有 `M` 参数,tuple 的 `impl FromRequest<S> for (...)?` 就会和 `impl FromRequest<S, ViaParts> for T where T: FromRequestParts` 的 blanket impl **冲突**——因为 tuple 也 impl `FromRequestParts`,blanket 就给它一个 `FromRequest<S>` 实现,可 tuple 自己又手写了一个 `FromRequest<S>`,两个重叠,Rust 拒绝。

`axum-core/src/extract/tuple.rs#L46-L47` 的注释把这件事点破了:

```rust
// This impl must not be generic over M, otherwise it would conflict with the blanket
// implementation of `FromRequest<S, Mut>` for `T: FromRequestParts<S>`.
```

(注释里 `Mut` 是笔误,源码就是这样写的,意思是 `M`。)翻译:**tuple 的 `FromRequest` impl 不能泛型 `M`,否则会和 blanket impl 冲突**。所以 tuple 的 `FromRequest` impl 写死 `impl FromRequest<S> for (...)`(用默认 `M = ViaRequest`),不写 `M`。这样 tuple 直接 impl `FromRequest<S, ViaRequest>`,不和 blanket impl(给 `FromRequest<S, ViaParts>`)冲突——`M` 不同,两个 impl 落在不同的具体类型上。

`M` 这套设计,本质是**让 `FromRequest` 和 `ViaParts` blanket impl 都能存在而不冲突**——直接 impl 的走 `ViaRequest`(tuple、Json、Bytes 等具体类型),blanket 桥接的走 `ViaParts`(Path、Query、State 等只读 parts 类型)。`M` 把"这两种来源"编码进类型系统,Rust 的孤儿规则看到 `M` 不同就允许两个 impl 共存。

### 把整张关系图画出来

```text
                          ┌─────────────────────────────────────┐
                          │   FromRequestParts<S>(只读 parts)    │
                          │   fn from_request_parts(             │
                          │       parts: &mut Parts,             │
                          │       state: &S                      │
                          │   ) -> impl Future<...>              │
                          └──────────────┬──────────────────────┘
                                         │
                          实现这个 trait 的类型:
                          Method, Uri, HeaderMap, Path<T>,
                          Query<T>, State<T>, Extensions, ...
                                         │
                                         │ ★ ViaParts blanket impl
                                         │   (axum-core/src/extract/mod.rs#L91-105)
                                         │   for T where T: FromRequestParts<S>
                                         │   把 req 拆 parts+_, 丢 body,
                                         │   调 from_request_parts
                                         ▼
              ┌────────────────────────────────────────────────────────┐
              │  FromRequest<S, M = ViaRequest>(按值 Request,可消费 body)│
              │  fn from_request(req: Request, state: &S)              │
              │     -> impl Future<...>                                │
              └──────────────┬────────────────────────────┬───────────┘
                             │                            │
              M = ViaParts   │                            │  M = ViaRequest(默认)
              (经桥接)        │                            │  (直接 impl)
                             ▼                            ▼
              Path, Query, State, Method,           Json, Form, Bytes,
              HeaderMap, ...                        String, Request, Body,
              (它们的 from_request                 BytesMut, RawForm ...
               = 丢 body + 跑 from_request_parts)   (它们的 from_request
                                                   = 真的消费 body)
```

这张图把二元划分 + marker + blanket 三件事一锅端:`FromRequestParts`(只读 parts)→ blanket 桥接 → `FromRequest<S, ViaParts>`(用桥接的 from_request);`Json` 等 → 直接 impl → `FromRequest<S, ViaRequest>`(真消费 body)。两条路在 `FromRequest` 这个 trait 里汇合,`M` 把它们区分开,`impl_handler!` 宏统一调 `from_request`,编译器按 `M` 分派到对的实现。

> **钉死这件事**:`M = ViaRequest` 这个 marker 类型参数加上 `impl FromRequest<S, ViaParts> for T where T: FromRequestParts` 的 blanket impl,是 axum 类型系统最巧的一笔。它让"只读 parts 的提取器"(Path/Query/State)自动获得 `FromRequest` 实现,从而能站到 `impl_handler!` 宏的最后一个参数位置。`M` 把"这个 FromRequest 是直接 impl 还是经桥接来的"编码进类型,避免孤儿规则冲突(尤其 tuple 的 FromRequest impl 和这个 blanket 在 `M` 上岔开)。`ViaParts` / `ViaRequest` 是空 enum,纯粹编译期 phantom marker,运行时不存在实例。

---

## 第五节:`Result`、`Option`、tuple——三套自动桥接

### 提问

到这里二元划分讲透了。但还有三件事得拆:① 你写 `async fn handler(result: Result<Json<T>, JsonRejection>)`,axum 凭什么让 `Result` 也能当提取器?② `Option<Json<T>>` 又是怎么回事(提取失败返回 `None` 而不是 rejection)?③ tuple 怎么既能 `FromRequestParts` 又能 `FromRequest`?这三套都是 axum 在二元划分之上加的"自动桥接",让用户少写样板。

### `Result<T, Rejection>`:把失败改成"传 Err 进 handler"

看 blanket impl([`axum-core/src/extract/mod.rs#L107-L129`](../axum/axum-core/src/extract/mod.rs#L107-L129)):

```rust
impl<S, T> FromRequestParts<S> for Result<T, T::Rejection>
where
    T: FromRequestParts<S>,
    S: Send + Sync,
{
    type Rejection = Infallible;

    async fn from_request_parts(parts: &mut Parts, state: &S) -> Result<Self, Self::Rejection> {
        Ok(T::from_request_parts(parts, state).await)
    }
}

impl<S, T> FromRequest<S> for Result<T, T::Rejection>
where
    T: FromRequest<S>,
    S: Send + Sync,
{
    type Rejection = Infallible;

    async fn from_request(req: Request, state: &S) -> Result<Self, Self::Rejection> {
        Ok(T::from_request(req, state).await)
    }
}
```

这里有两个 blanket impl——一个给 `FromRequestParts`,一个给 `FromRequest`(注意后者是 `FromRequest<S>`,用默认 `M = ViaRequest`,不泛型 `M`,原因和 tuple 一样:避免和 `ViaParts` blanket 冲突)。

它干的事:**把内层 `T` 的提取结果包成 `Result<Self, Infallible>`,内层 `Result<T, T::Rejection>` 的成功或失败都被 `Ok(...)` 包一层**。注意 `Self::Rejection = Infallible`——`Result<T, _>` 这个提取器本身**永远不会失败**(它的"失败"被 `Ok` 包成了 handler 收到的 `Err`)。

具体看。假设你写:

```rust
async fn handler(payload: Result<Json<CreateUser>, JsonRejection>) -> impl IntoResponse {
    match payload {
        Ok(json) => /* 正常处理 */,
        Err(rejection) => /* 自己决定怎么响应,比如返回 400 或别的 */,
    }
}
```

宏展开后,`payload` 是 `$last`(只有一个参数),走 `Json<CreateUser>::from_request` 不行——`payload` 的类型是 `Result<Json<CreateUser>, JsonRejection>`,不是 `Json<CreateUser>`。但上面的 `impl FromRequest<S> for Result<T, T::Rejection>` 让 `Result<Json<CreateUser>, JsonRejection>` 自动满足 `FromRequest<S, ViaRequest>`。它的 `from_request` 调 `Json::from_request(req, state).await`——如果 `Json` 提取成功,返回 `Ok(Ok(json))`(外层 `Ok` 是 blanket 包的,内层 `Ok(json)` 是 Json 的结果);如果 `Json` 提取失败(比如 Content-Type 不对),返回 `Ok(Err(rejection))`(外层 `Ok` 表示"提取器没失败",内层 `Err(rejection)` 是 Json 的失败)。

所以 handler 收到的 `payload: Result<Json<CreateUser>, JsonRejection>` 是"已经提取完的结果",handler 自己 `match` 决定怎么响应。这让你可以"在 handler 里自定义错误响应",而不是被框架的默认 rejection 响应绑死。

`Self::Rejection = Infallible` 这一笔也值得钉死——它告诉编译器"这个提取器链永远不会返回 Err 给宏",宏的 `match ... { Err(rejection) => return rejection.into_response() }` 那个分支对 `Infallible` 来说是 dead code(编译器知道 `Infallible` 没有变体,这个 match 分支永远不会触发),会被优化掉。

### `Option<T>`:把失败改成"传 None 进 handler"

`Option` 比 `Result` 复杂一点,因为 axum 把它单独拆了一组 trait:`OptionalFromRequest` 和 `OptionalFromRequestParts`([`axum-core/src/extract/option.rs#L11-L36`](../axum/axum-core/src/extract/option.rs#L11-L36)):

```rust
pub trait OptionalFromRequestParts<S>: Sized {
    type Rejection: IntoResponse;
    fn from_request_parts(
        parts: &mut Parts,
        state: &S,
    ) -> impl Future<Output = Result<Option<Self>, Self::Rejection>> + Send;
}

pub trait OptionalFromRequest<S, M = private::ViaRequest>: Sized {
    type Rejection: IntoResponse;
    fn from_request(
        req: Request,
        state: &S,
    ) -> impl Future<Output = Result<Option<Self>, Self::Rejection>> + Send;
}
```

为什么 `Option` 要单独拆一组 trait,不像 `Result` 那样直接 blanket?因为 `Option` 的语义是"提取失败时返回 `None`"——这需要内层类型知道"怎么在失败时优雅退化"。`OptionalFromRequest` 这个 trait 把"提取结果可能是 `Option<Self>`"显式表达,让实现者决定什么时候返回 `None`。

看 `Option<T>` 的 blanket impl([`axum-core/src/extract/option.rs#L40-L67`](../axum/axum-core/src/extract/option.rs#L40-L67)):

```rust
impl<S, T> FromRequestParts<S> for Option<T>
where
    T: OptionalFromRequestParts<S>,
    S: Send + Sync,
{
    type Rejection = T::Rejection;

    fn from_request_parts(
        parts: &mut Parts,
        state: &S,
    ) -> impl Future<Output = Result<Option<T>, Self::Rejection>> {
        T::from_request_parts(parts, state)
    }
}

impl<S, T> FromRequest<S> for Option<T>
where
    T: OptionalFromRequest<S>,
    S: Send + Sync,
{
    type Rejection = T::Rejection;

    async fn from_request(req: Request, state: &S) -> Result<Option<T>, Self::Rejection> {
        T::from_request(req, state).await
    }
}
```

`Option<T>` 的提取委托给 `T: OptionalFromRequest(S)`。所以你写 `async fn handler(maybe: Option<Json<T>>)`,`maybe` 是 `$last`(走 `FromRequest`),需要 `Json: OptionalFromRequest<S>`——这个实现 axum 真的写了([`axum/src/json.rs#L116-L136`](../axum/axum/src/json.rs#L116-L136)):

```rust
impl<T, S> OptionalFromRequest<S> for Json<T>
where
    T: DeserializeOwned,
    S: Send + Sync,
{
    type Rejection = JsonRejection;

    async fn from_request(req: Request, state: &S) -> Result<Option<Self>, Self::Rejection> {
        let headers = req.headers();
        if headers.get(header::CONTENT_TYPE).is_some() {
            if json_content_type(headers) {
                let bytes = Bytes::from_request(req, state).await?;
                Ok(Some(Self::from_bytes(&bytes)?))
            } else {
                Err(MissingJsonContentType.into())
            }
        } else {
            Ok(None)   // ★ 没有 Content-Type 就返回 None
        }
    }
}
```

注意 `OptionalFromRequest` 的语义比 `FromRequest` 宽松:`Json::OptionalFromRequest` 在"没有 Content-Type"时返回 `Ok(None)`(handler 收到 `None`),而 `Json::FromRequest` 在同样情况下返回 `Err(MissingJsonContentType)`(handler 收到 rejection)。这就是 `Option<Json<T>>` 比 `Json<T>` "更宽容"的根——`Option` 版本允许请求不带 Content-Type(你 handler 自己处理 None),`Json` 版本要求必须带正确的 Content-Type。

`axum-core/src/extract/option.rs#L40` 的 `#[diagnostic::do_not_recommend]` 注解也值得点一笔——这个 attribute 告诉编译器"在错误信息里不要推荐这个 impl",因为 `Option<T>` 的 blanket impl 出现在类型不匹配的错误信息里会让用户困惑(用户写错 handler 签名时,编译器可能推荐"你 impl OptionalFromRequest 了吗",这是反方向)。这是 axum 在诊断体验上的小细节。

### tuple:同时是 `FromRequestParts` 和 `FromRequest`

tuple 的实现刚才贴过([`axum-core/src/extract/tuple.rs#L18-L75`](../axum/axum-core/src/extract/tuple.rs#L18-L75)),要点重述:

1. tuple `($($ty,)* $last,)` 同时 impl `FromRequestParts`(所有元素都 `FromRequestParts`,见 L24-L44)和 `FromRequest`(前 N-1 个 `FromRequestParts` + 最后一个 `FromRequest`,见 L50-L73)。
2. tuple 的 `FromRequest` impl **不泛型 `M`**(写死 `impl FromRequest<S> for (...)`,用默认 `M = ViaRequest`),避免和 `ViaParts` blanket impl 冲突(L46-L47 的注释明说)。
3. tuple 的 `Rejection = Response`(不是具体的 rejection 类型)——因为 tuple 元素可能各自有不同的 Rejection 类型,统一擦成 `Response`(通过 `err.into_response()`)。

tuple 这个能力主要服务 `from_extractor` 中间件(P4-15 会拆)和"把多个提取器打包成一个组合提取器"的场景。比如你想定义 `struct Auth(User, Permissions)`,可以 `impl FromRequestParts for Auth` 委托给 `(User, Permissions)` 的 tuple impl——`(User, Permissions): FromRequestParts` 自动成立(只要 `User` 和 `Permissions` 都 `FromRequestParts`)。

tuple impl 的 `FromRequest` 内部干的事,和 `impl_handler!` 宏展开几乎一样:拆 parts + body,前 N-1 个走 from_request_parts,最后一个走 from_request。这意味着 tuple 也能"消费 body"(如果最后一个元素是 `FromRequest` 类型)。但注意 tuple 自己 `FromRequest` impl 用的是 `ViaRequest` marker,所以一个 `(Path<u32>, Json<T>)` 这样的 tuple 是 `FromRequest<S, ViaRequest>`,不是 `FromRequest<S, ViaParts>`——即便 `Path` 经 blanket 也是 `FromRequest<S, ViaParts>`,tuple 这一层用了 `ViaRequest`。这是为了避免冲突的取舍。

### 三套桥接合起来

```text
   内层 T: FromRequestParts            ──►  Result<T, T::Rejection>: FromRequestParts  (Rejection=Infallible)
                                          (Ok 包一层,失败变 handler 的 Err)

   内层 T: FromRequest                  ──►  Result<T, T::Rejection>: FromRequest  (Rejection=Infallible)

   内层 T: OptionalFromRequestParts     ──►  Option<T>: FromRequestParts  (Rejection=T::Rejection)
                                          (失败时返回 None)

   内层 T: OptionalFromRequest          ──►  Option<T>: FromRequest  (Rejection=T::Rejection)

   (T1, ..., Tn): 全 FromRequestParts   ──►  tuple: FromRequestParts  (Rejection=Response)
   (T1, ..., Tn): 前 n-1 FRP + 最后 FR   ──►  tuple: FromRequest<S, ViaRequest>  (Rejection=Response)
```

这三套桥接让用户写"我想让 handler 自己处理错误"(`Result`)、"我想让提取器宽容点"(`Option`)、"我想把多个提取器打包成一个"(tuple)都不用写样板——直接用 Rust 标准类型包一下,自动获得对应 trait。

> **钉死这件事**:axum 在二元划分之上加了三套自动桥接:① `Result<T, Rejection>`(Rejection 变 Infallible,失败变 handler 的 `Err`);② `Option<T>`(走单独的 `OptionalFromRequest(S)` trait,失败变 `None`,语义更宽容);③ tuple(同时 impl 两个 trait,Rejection 统一擦成 Response)。这三套让"自定义错误响应"、"宽容提取"、"组合提取器"都不用写样板。

---

## 第六节:`impl Future` 而不是 `#[async_trait]`——axum 0.8 的 RPITIT 改造

### 提问

最后一个细节。两个 trait 的方法返回类型都是 `impl Future<...> + Send`,不是 `Pin<Box<dyn Future<...> + Send>>`。这意味着什么?为什么 axum 0.8 把 `#[async_trait]` 全面换成了这种写法?这笔账值多少?

### RPITIT 是什么,稳定在哪个版本

Rust 1.75(2023-12-28 稳定)引入了 **RPITIT(Return Position Impl Trait In Trait)**——允许 trait 方法用 `fn foo(...) -> impl Trait` 而不是 `fn foo(...) -> Box<dyn Trait>` 或 `#[async_trait]`。`async fn` in traits 也同时稳定(`async fn foo(...)` 在 trait 里直接可用,等价于 `fn foo(...) -> impl Future<...>`)。

axum 0.8 / axum-core 0.5 的这次改造,CHANGELOG 里写得清楚([`axum-core/CHANGELOG.md`](../axum/axum-core/CHANGELOG.md) 0.5 段):

> **breaking:** Replace `#[async_trait]` with return-position `impl Trait` in traits (RPITIT) ([#2308])
> **change:** Update minimum rust version to 1.75 ([#2943])

(0.5 的 alpha.1 段最先记这次改动,RC 段又重申;PR #2308 是改动主 PR,MSRV 升 1.75 是 #2943。)

这是 0.7 → 0.8 之间最关键的 breaking change 之一。0.7 时代的 `FromRequest` / `FromRequestParts` 长这样(简化示意,非源码原文):

```rust
// axum 0.7 时代(简化示意)
#[async_trait]
pub trait FromRequest<S, B, M = ViaRequest>: Sized {
    type Rejection: IntoResponse;
    async fn from_request(req: Request<B>, state: &S) -> Result<Self, Self::Rejection>;
}
```

`#[async_trait]` 这个过程宏会把 `async fn from_request` 改写成:

```rust
// #[async_trait] 展开后(简化示意)
fn from_request<'a, 'b, 'c>(req: Request<B>, state: &'a S)
    -> Pin<Box<dyn Future<Output = Result<Self, Self::Rejection>> + Send + 'c>>
where 'a: 'c, 'b: 'c
{ /* Box::pin(async move { ... }) */ }
```

注意返回类型是 `Pin<Box<dyn Future + Send>>`——一个装箱的 trait object。每次调用 `from_request` 都要 `Box::pin` 一次,也就是一次堆分配。

### 为什么 `#[async_trait]` 是开销

axum 的提取器是**热路径**。一次请求进来,`impl_handler!` 宏展开的代码会调 `from_request_parts` N-1 次 + `from_request` 1 次,共 N 次 trait 方法调用。每次调用走 `#[async_trait]` 都要 `Box::pin` 一次 Future——N 次堆分配。对一个高 QPS 的服务(比如 10 万 QPS,每个请求 4 个提取器),就是每秒 40 万次堆分配,压在 allocator 上(jemalloc/mimalloc/系统 allocator),还有 cache miss。

更要命的是,`dyn Future` 是**虚分派**——每次 `poll` 这个 Future 都要走 vtable 查一次,编译器没法内联。而提取器的 Future 往往很短(`Method::from_request_parts` 就是 clone 一下,Future 几乎立刻 Ready;`State::from_request_parts` 同理),虚分派的开销相对实际工作量占比很高。

RPITIT 把这两笔开销都省掉。看现在的签名:

```rust
pub trait FromRequestParts<S>: Sized {
    type Rejection: IntoResponse;
    fn from_request_parts(
        parts: &mut Parts,
        state: &S,
    ) -> impl Future<Output = Result<Self, Self::Rejection>> + Send;
}
```

`-> impl Future<...> + Send` 是 RPSCIT 的写法。它的语义是:**返回的具体 Future 类型由实现者定,调用者拿到的是这个具体类型,不是 `dyn Future`**。编译器为每个实现者单态化一份代码——`Method::from_request_parts` 的 Future 类型是 `Ready<Result<Method, Infallible>>`(因为方法体是 `Ok(parts.method.clone())`,本质同步,Rust 会用 `std::future::ready`),编译器直接内联,零堆分配,零虚分派。`Json::from_request` 的 Future 是一个真实的 async 状态机(因为要 await body 收集),编译器生成一个具名 Future 类型(你看不到名字,但它是单态化的),poll 是直接调用,不查 vtable。

`+ Send` 这个约束很重要——它要求实现者返回的 Future 跨线程 Send(axum 的 handler 在 Tokio 多线程运行时上跑,Future 必须能在 worker 线程间 move)。`#[async_trait]` 时代是 `Box<dyn Future + Send>`,RPITIT 时代是 `impl Future + Send`,后者是编译期检查具体类型是不是 Send,零运行时开销。

### 反面对比:如果 axum 0.8 还用 `#[async_trait]`

假设 axum 0.8 不做 RPITIT 改造,继续用 `#[async_trait]`。代价具体化:

- 每个 `from_request_parts` / `from_request` 调用:`Box::pin` 一次 → 堆分配(64-128 字节起的 Future 装箱)。
- 每次 `poll`:`dyn Future::poll` → vtable 查找 + 间接调用,不能内联。
- 一个 4 提取器的 handler:4 次堆分配 + 4 次虚分派 per request。
- 10 万 QPS:40 万次堆分配 + 40 万次虚分派 per second。

RPITIT 之后:

- 每个 `from_request_parts` / `from_request` 调用:返回具体 Future 类型(栈上),零堆分配。
- 每次 `poll`:直接调用,可内联(尤其 `Ready` Future)。
- 一个 4 提取器的 handler:0 次堆分配 + 0 次虚分派。
- 10 万 QPS:0 次堆分配 + 0 次虚分派 per second。

这是一笔巨大的性能账。axum 0.8 的 RPITIT 改造,把提取器链从"每次都装箱"变成"编译期单态化零开销",这是 axum 在 hyper 之上能保持高性能的关键一笔。

代价是 MSRV 升到 1.75——这对大多数用户透明(2024 年的 rustc 早过 1.75),但对一些嵌入式或长 LTS 环境可能有影响。axum 选了"性能优先",这是 Tokio 官方团队的一贯姿态。

### 一个细节:RPITIT 和 `async fn` in trait 的区别

Rust 1.75 同时稳定了 RPITIT 和 `async fn` in trait。axum 选了 `fn ... -> impl Future<...> + Send` 而不是 `async fn ...`,有一个具体原因:**`+ Send` 显式控制**。

`async fn in trait` 默认返回的 Future 是 `impl Future<Output = ...>`(不带 `+ Send`)。要让这个 Future Send,要么实现者写 `async fn`(编译器自动推断 Send 性,可能不 Send),要么用 desugared 写法 `fn ... -> impl Future<...> + Send` 显式要求 Send。axum 选了后者,因为提取器的 Future **必须 Send**(跨 Tokio worker),不能让实现者"忘加 Send"。`fn from_request_parts(...) -> impl Future<...> + Send` 这个签名,把 `+ Send` 钉死在 trait 定义里,实现者必须返回 Send 的 Future,否则编译期报错。这是 axum 在并发安全上的硬约束。

> **钉死这件事**:axum 0.8 / axum-core 0.5(PR #2308,MSRV 1.75)把 `#[async_trait]` 全面换成 `impl Future<...> + Send`——RPITIT。每个提取器调用从"`Box::pin` 一次堆分配 + 虚分派"变成"编译期单态化零开销"。对一个 4 提取器 handler 在 10 万 QPS 下,每秒省下 40 万次堆分配和 40 万次虚分派。这是 axum 在 hyper 之上保持高性能的关键一笔。`+ Send` 显式钉死,保证提取器 Future 跨 Tokio worker Send。

---

## 技巧精解

这一节挑两个最该被钉死的技巧,配真实源码 + 反面对比,单独拆透。

### 技巧一:二元划分为什么 sound——body 不被重复消费的编译期保证

**它解决什么问题**:让"哪些提取器能消费 body、哪些不能"在编译期钉死,运行时不可能出现 body 被重复消费或被空消费。

**反面对比:如果只有一个 trait(像 actix-web)会怎样**:

假设 axum 只有 `FromRequest` 一个 trait,签名按值拿 `Request`。看会发生什么:

```rust
// 假想的"单一 trait"axum(非实际做法)
pub trait FromRequest<S>: Sized {
    type Rejection: IntoResponse;
    async fn from_request(req: Request, state: &S) -> Result<Self, Self::Rejection>;
}

// 用户写两个 body 提取器
async fn handler(a: Json<A>, b: Json<B>) -> impl IntoResponse { /* ... */ }
```

宏展开后,`a` 走 `Json::<A>::from_request(req, state)`——它把 body 全收完。然后 `b` 走 `Json::<B>::from_request(req, state)`——可 `req` 已经被 `a` 按值消费了(`req` 是 move 进 `a::from_request` 的),编译期 `req` 已 moved,`b::from_request(req, ...)` 编译不过。看起来 Rust 的所有权机制会拦住?

实际上拦不住,因为宏会重新构造 req。看 `impl_handler!` 的真实展开(`axum/src/handler/mod.rs#L238-L256`):

```rust
let (mut parts, body) = req.into_parts();
Box::pin(async move {
    $(
        let $ty = match $ty::from_request_parts(&mut parts, &state).await { ... };
    )*
    let req = Request::from_parts(parts, body);   // ★ parts 和 body 拼回 req
    let $last = match $last::from_request(req, &state).await { ... };
    // ...
})
```

宏的展开是**固定模式**——前 N-1 个走 `from_request_parts`,最后一个走 `from_request`。如果只有一个 trait,宏要怎么展开?它要么把所有参数都走 `from_request`(那前 N-1 个提取器按值拿 req,后续提取器拿不到),要么允许混合但需要某种机制区分"这个提取器是只读还是消费 body"。

axum 的二元划分,本质是**把"只读还是消费 body"编码进 trait**。`FromRequestParts` 一定是只读,`FromRequest` 可能消费 body。宏的约束 `$ty: FromRequestParts<S>`(前 N-1)+ `$last: FromRequest<S, M>`(最后)钉死了"只有最后一个可能消费 body"。

现在看为什么 `async fn handler(a: Json<A>, b: Json<B>)` 编译过不去。两个 `Json`,假设 `a` 是 `$ty`(前 N-1 之一),它要满足 `$ty: FromRequestParts<S>`——可 `Json<A>` **没实现 `FromRequestParts`**(它只实现 `FromRequest`,因为消费 body)。约束不满足,编译期报错。错误信息大概是"`Json<A>: FromRequestParts<S>` not satisfied",可能配上 `#[diagnostic::on_unimplemented]` 的提示("Function argument is not a valid axum extractor")。这是 axum 在编译期杜绝"两个 body 提取器并列"的机制。

**为什么 sound**:`FromRequestParts` 签名保证它拿不到 body(只 `&mut Parts`,parts 里没 body),所以前 N-1 个提取器在物理上不可能碰 body。`FromRequest` 只在最后一个位置,只有它可能消费 body。body 是 Stream 只能消费一次,而只有一个提取器有机会碰它——不会重复消费。这条链是**编译期类型层保证**的,不是运行期检查的。

**朴素地写会撞什么墙**:不用二元划分,你要么:① 让所有提取器都能消费 body(运行期可能重复消费,要么 panic 要么拿到空 body);② 让所有提取器都只读 parts(写不出 `Json`);③ 用户自己保证"只有一个 body 提取器"(运行期可能出错)。axum 选了第四条路:二元划分 + 宏约束,编译期钉死。

> **钉死这件事**:二元划分的 sound,在两点:① `FromRequestParts` 签名拿不到 body(只 `&mut Parts`),物理上不可能消费;② 宏约束"前 N-1 个 FromRequestParts,最后一个 FromRequest",只有最后一个可能消费 body。所以 body 不被重复消费是编译期保证的,运行时不可能出错。`async fn(Json<_>, Json<_>)` 这种代码在编译期就被拦下(`Json` 不 `FromRequestParts`)。这是 axum 类型系统最 sound 的一笔。

### 技巧二:ViaParts blanket 桥接为什么必要——避免给每个只读提取器重复写 FromRequest

**它解决什么问题**:让 `Path` / `Query` / `State` 这些只读 parts 的提取器,不用每个都手写一份"丢 body + 调 from_request_parts"的 `FromRequest` 实现,自动获得 `FromRequest<S, ViaParts>`。

**反面对比:如果没有 blanket 桥接会怎样**:

假设 axum 没有 `impl FromRequest<S, ViaParts> for T where T: FromRequestParts` 这个 blanket impl。你想写 `async fn handler(Path(id): Path<u32>)`(只一个参数,是 `$last`),`Path<u32>` 必须满足 `$last: FromRequest<S, M>`。可 `Path` 只 impl `FromRequestParts`,不 impl `FromRequest`。怎么办?

候选方案 A:**给每个 `FromRequestParts` 类型手写一份 `FromRequest`**。`Path` 写一份,`Query` 写一份,`State` 写一份,`Method` 写一份,`HeaderMap` 写一份……每份都是"req.into_parts() 拆开,丢 body,调 from_request_parts"。这是 13+ 份几乎一样的样板(看上面那张内置 FromRequestParts 表)。任何后续加的只读提取器(自定义提取器)也得写一份。样板爆炸,且容易抄错。

候选方案 B:**宏生成两套分支**。`impl_handler!` 在"最后一个参数是 FromRequestParts"和"是 FromRequest"时生成不同的代码。这让宏复杂度翻倍,而且对"参数类型既实 FromRequestParts 又实 FromRequest"(比如 tuple)的场景无法处理。

候选方案 C:**给 `FromRequest` 加 `M` 参数 + blanket impl**(axum 的实际做法)。一个 blanket impl 服务所有 `FromRequestParts` 类型,自动获得 `FromRequest<S, ViaParts>`。零样板,宏统一,`M` 把"直接 impl"和"桥接"区分开避免冲突。

axum 选了 C。这一笔的精妙在于:**用 Rust 的 blanket impl 把"只读 parts 的提取器自动获得 FromRequest"这件事做成零成本抽象**——用户写 `Path` 提取器只 impl `FromRequestParts`,自动获得 `FromRequest` 经桥接的实现,编译器单态化,运行时开销是一次 `req.into_parts()` + 一次 `body` drop(都是零成本字段移动)。

**反面对比:为什么 `M` 必须存在**:

假设 axum 不要 `M`,直接写 `impl FromRequest<S> for T where T: FromRequestParts`(不带 marker)。看起来更简洁,但会撞孤儿规则。看 tuple 的实现([`axum-core/src/extract/tuple.rs#L50-L54`](../axum/axum-core/src/extract/tuple.rs#L50-L54)):

```rust
impl<S, $($ty,)* $last> FromRequest<S> for ($($ty,)* $last,)
where
    $( $ty: FromRequestParts<S> + Send, )*
    $last: FromRequest<S> + Send,
    S: Send + Sync,
{ /* ... */ }
```

tuple 直接 impl `FromRequest<S>`(不泛型 M,用默认 ViaRequest)。如果同时存在 `impl FromRequest<S> for T where T: FromRequestParts` 这个不带 marker 的 blanket impl,那么对于 `(Method,)` 这个 tuple(它既 impl `FromRequestParts` 又想直接 impl `FromRequest<S>`),两个 impl 会重叠——blanket 给它一个 `FromRequest<S>` 实现(因为 tuple impl `FromRequestParts`),tuple 自己又写了一个 `FromRequest<S>` 实现,Rust 拒绝。

`M` 的存在把两个 impl 岔开:`ViaParts` blanket impl 给的是 `FromRequest<S, ViaParts>`,tuple 自己写的是 `FromRequest<S, ViaRequest>`(默认 M),`M` 不同,两个 impl 落在不同具体类型上,不重叠。这就是 `M` 必须存在的根本理由——不是"好看",是"避免孤儿规则冲突"。

`axum-core/src/extract/tuple.rs#L46-L47` 的注释把这件事点破:"This impl must not be generic over M, otherwise it would conflict with the blanket implementation"。tuple 的 `FromRequest` impl 不能泛型 `M`,否则就和 blanket 冲突。

**为什么 `ViaParts` / `ViaRequest` 是空 enum**:

这两个 marker 是空 enum(`enum ViaParts {}` / `enum ViaRequest {}`,无变体)。空 enum 在 Rust 里是**永远构造不出值**的类型——它没有 `()` 那种唯一实例,也不占内存。axum 选空 enum 而不是 `pub struct ViaRequest;`(unit struct,有 `()` 实例),语义上更强:

- **`M` 永远不会有真实值**。`from_request` 方法签名里没有 `M` 参数(它只出现在 trait 的类型参数列表),`M` 纯粹是编译期 phantom 标记,运行时不存在任何 `ViaRequest` 或 `ViaParts` 的实例。空 enum 强化了"这个类型永远不出值"的语义。
- **避免误用**。如果 `ViaRequest` 是 unit struct,有人可能写出 `let x = ViaRequest;` 这种代码(虽然没意义但合法)。空 enum 不允许任何构造尝试,`let x = ViaRequest;` 编译不过(没有变体可构造),更强地表达"这是 marker,不是值"。
- **derive 合法**。`#[derive(Debug, Clone, Copy)]` 对空 enum 合法(虽然 `Debug::fmt` 永远不会被调用,因为没有实例可格式化),这是 Rust 的细节。axum 加这些 derive 主要是 marker 类型一致风格(`axum` 仓里很多 marker 都这么写)。

这是细节,但体现 axum 在类型设计上的严谨——选空 enum 而不是 unit struct,把"marker 不存在运行时值"的语义钉死。

**朴素地写会撞什么墙**:不要 `M`,直接 blanket impl,撞孤儿规则(tuple impl 冲突)。用 unit struct 代替空 enum,语义弱一点(允许构造无意义的实例)。axum 的实际做法(`M` + 空 enum + blanket)是 Rust 类型系统能给出的最干净解法。

> **钉死这件事**:`M = ViaRequest` 这个 marker 加 `impl FromRequest<S, ViaParts> for T where T: FromRequestParts` 这个 blanket impl,是 axum 类型系统最巧的一笔。它让只读 parts 的提取器自动获得 `FromRequest`(零样板),`M` 把"直接 impl"和"桥接"区分开避免孤儿规则冲突(尤其 tuple 和 blanket 的冲突)。`ViaParts` / `ViaRequest` 是空 enum,纯粹编译期 phantom marker,运行时不存在值。这套设计让"只读 parts 的提取器能站到最后一个参数位置"零成本成立。

---

## 章末小结

回到全书的主轴:**路由与分发 vs 提取与响应**。

本章服务"提取与响应"这一面。具体说,本章拆了 `Handler::call`(由 `impl_handler!` 宏生成,承 P3-09)内部那条提取器链上每个提取器用的两个 trait——`FromRequestParts`(只读 parts,可多次跑)和 `FromRequest`(按值 Request,可消费 body,只能一次)。这两个 trait 的二元划分,是 axum 提取器的灵魂,是把 `http::Request` 物理上的 `Parts + Body` 不对称编码进类型系统的招牌样本。`M = ViaRequest` marker 加 `ViaParts` blanket 桥接,让只读 parts 的提取器自动获得 `FromRequest`,从而能站到最后一个参数位置。RPITIT 改造(PR #2308)把 `#[async_trait]` 的每次堆分配 + 虚分派省掉,提取器链变成编译期单态化零开销。

承 P3-09:P3-09 立了 Handler trait 地基(`T` 占位 tuple + `impl_handler!` 宏展开),讲清了"前 N-1 个 FromRequestParts + 最后一个 FromRequest"的约定。本章补全这条约定背后的全部细节——为什么是两个 trait、那个 `M` 是什么、为什么 sound、为什么 0.8 改 RPITIT。两章合起来,才是"async fn 凭什么当 handler"的完整答案。下一章 P3-11 会拆内置提取器的具体实现(`Path` 怎么 serde URL 参数、`Json` 怎么消费 body + Content-Type 校验、`Form` 怎么和 `serde_urlencoded` 配合),把本章的二元划分落到每个提取器的源码。

承《hyper》:`http::Request` 是 hyper 解析完协议后给 axum 的结构化产物,`into_parts()` 拆 `Parts + Body` 是 `http` crate 的方法。`Body` 作为"只能往前拉一次的 Stream"的语义,根在 hyper 的协议机(HTTP/1 的 `ChunkedState` 分帧、HTTP/2 的 h2 per-stream DATA 帧),详见《hyper》P2-P3。本章只承其结论"body 是只能消费一次的 Stream",不重讲。

承《Tower》/《Tokio》:`impl Future<...> + Send` 是 RPITIT(承 Rust 1.75),Future/Poll 在标准库 `core::future`/`core::task`(承《Tokio》一句带过)。`+ Send` 钉死保证 Future 跨 Tokio worker Send。

### 五个为什么清单

1. **为什么 axum 把提取器一刀切两个 trait,而不是一个统一的 `FromRequest`?** 因为 `http::Request` 物理上就是 `Parts`(可复用镜像)+ `Body`(只能消费一次的 Stream)的不对称。`FromRequestParts` 对应"只读 parts 那批"(Path/Query/State/Headers),可多次跑;`FromRequest` 对应"可能消费 body 那批"(Json/Form/Bytes),只能一次。一个 trait 没法表达这种不对称。
2. **为什么 `FromRequestParts` 用 `&mut Parts`,不是 `&Parts`?** 因为允许"提取器之间通过 extensions 互通消息"——路由层把 URL 参数塞进 `parts.extensions`,`Path` 提取器从那里读;中间件往 extensions 塞 `CurrentUser`,后续提取器读。`&mut` 给的是"接力写 extensions"的能力,不是"改 method/uri"的口子(内置提取器一律读后不改)。
3. **为什么 `FromRequest` 有个 `M = ViaRequest` 类型参数?** 为了让 `impl FromRequest<S, ViaParts> for T where T: FromRequestParts` 这个 blanket impl 和具体类型直接 impl 的 `FromRequest<S, ViaRequest>` 共存而不撞孤儿规则。`M` 把"这个 FromRequest 是直接 impl 的还是经桥接来的"编码进类型,tuple 的 FromRequest impl 写死 `ViaRequest`,blanket 给的是 `ViaParts`,两者 `M` 不同不重叠。
4. **为什么 `Path` 能当 handler 的唯一参数(`async fn handler(Path(id): Path<u32>)`)?** `Path` 只 impl `FromRequestParts`,但通过 `ViaParts` blanket impl 自动获得 `FromRequest<S, ViaParts>`。`impl_handler!` 宏的约束 `$last: FromRequest<S, M>` 在编译期推断 `M = ViaParts`,宏统一调 `from_request`,blanket 内部把 req 拆 parts、丢 body、调 `from_request_parts`。零样板。
5. **为什么 axum 0.8 把 `#[async_trait]` 换成 `impl Future<...> + Send`?** RPITIT(PR #2308,MSRV 1.75)。`#[async_trait]` 每次调用 `Box::pin` 一次堆分配 + 虚分派;RPITIT 编译期单态化,零堆分配零虚分派。一个 4 提取器 handler 在 10 万 QPS 下,每秒省 40 万次堆分配和 40 万次虚分派。`+ Send` 钉死保证 Future 跨 Tokio worker Send。

### 想继续深入往哪钻

- **`impl_handler!` 宏怎么把任意 arity 的 async fn 全部 impl Handler,T 占位 tuple 怎么绕孤儿规则**:→ 第 9 章(P3-09),★★双招牌章,Handler trait 地基。本章承它,两章合起来才是完整答案。
- **内置提取器 `Path`/`Query`/`State`/`Json`/`Form` 的具体实现**(`Path` 怎么 serde URL 参数、`Json` 怎么消费 body + Content-Type 校验 + 大小限制、`Form` 怎么和 serde_urlencoded 配合):→ 第 11 章(P3-11),把本章的二元划分落到每个提取器源码。
- **`IntoResponse` trait 怎么把任意返回值变 Response、tuple 怎么链式拼装、`IntoResponseParts` 怎么先写 parts 再写 body**:→ 第 12 章(P3-12),响应侧招牌。
- **怎么写自定义提取器、`#[axum::debug_handler]` 宏怎么改善 handler 类型错信息**:→ 第 13 章(P3-13)。
- **hyper 的 `Request<B>` 怎么从协议字节流来,`Body` 作为 Stream 的全部语义(HTTP/1 ChunkedState 分帧、HTTP/2 h2 per-stream DATA)**:→《hyper》P2-P3,本章承其结论。
- **Tokio 的 Future/Poll/Waker,async/await 怎么 desugar 成状态机,RPITIT 在编译期怎么单态化**:→《Tokio》运行时机制章,本章承其结论。
- **actix-web 的 `FromRequest`(单一 trait,parts+body 揉一起)、rocket 的 `FromRequest` + `FromData`(二元但无 marker 桥接)、go net/http 的 `req.Body.Read`(全手动,body 只能读一次无类型保护)对照**:→ 第 21 章(P7-21)收束章,全景对照。

### 引出下一章

本章你拿到了 axum 提取器的二元划分地基:`FromRequestParts`(只读 parts,可多次跑)vs `FromRequest`(按值 Request,可消费 body,只能一次)两个 trait 的物理根(`http::Request = Parts + Body` 不对称)、`M = ViaRequest` marker 加 `ViaParts` blanket 桥接(让只读 parts 的提取器自动获得 `FromRequest`)、三套自动桥接(Result/Option/tuple)、RPITIT 改造(零堆分配零虚分派)。但有一个最具体的细节我们刻意留到了这里——那些内置提取器**具体怎么实现**?`Path` 怎么从 `parts.extensions` 读出路由层塞的 URL 参数、交给 serde 反序列化?`Json` 怎么在 `Bytes::from_request` 收完 body 后,用 `serde_path_to_error` 把反序列化错误路径标出来?`Form` 怎么和 `serde_urlencoded` 配合、Content-Type 要 application/x-www-form-urlencoded?大小限制(`DefaultBodyLimit`)怎么在 `into_limited_body` 那一步生效?这些问题,下一章 P3-11 会用每个内置提取器的源码 + 真实反序列化路径彻底拆开。那是把本章的二元划分落到"每个提取器怎么消费 parts / 怎么消费 body"的第一手源码,也是写自定义提取器之前该看一遍的参照。

---

> **本章源码锚点(全部经本地 `../axum/` Grep/Read 核实,版本 axum 0.8.9 / axum-core 0.5.5 / axum-macros 0.5.1,commit c59208c86fded335cd85e388030ad59347b0e5ae)**:
>
> - [FromRequestParts / FromRequest 定义 + ViaParts/ViaRequest marker + ViaParts blanket impl + Result 桥接](../axum/axum-core/src/extract/mod.rs#L31-L129) —— 二元划分的全部核心。`mod private { enum ViaParts {} enum ViaRequest {} }` @ L31-L37,`FromRequestParts` @ L53-L63,`FromRequest<S, M = ViaRequest>` @ L79-L89,`impl FromRequest<S, ViaParts> for T where T: FromRequestParts` @ L91-L105,`impl FromRequestParts for Result<T, _>` @ L107-L117,`impl FromRequest for Result<T, _>` @ L119-L129。
> - [OptionalFromRequestParts / OptionalFromRequest trait + Option<T> 桥接](../axum/axum-core/src/extract/option.rs#L11-L67) —— `Option` 走单独的 OptionalFromRequest(S) trait,失败变 None。`OptionalFromRequestParts` @ L11-L22,`OptionalFromRequest<S, M = ViaRequest>` @ L25-L36,`impl FromRequestParts for Option<T>` @ L40-L54,`impl FromRequest for Option<T>` @ L56-L67。
> - [tuple 的 FromRequestParts + FromRequest(写死 ViaRequest 不泛型 M)](../axum/axum-core/src/extract/tuple.rs#L7-L77) —— L7-L16 是 `()` 的 FromRequestParts,`impl_from_request!` 宏 @ L18-L75,tuple 同时 impl 两个 trait;L46-L47 注释明说"tuple 的 FromRequest 不能泛型 M,否则冲突 blanket"。
> - [Request/Method/Uri/Version/HeaderMap/Bytes/String/Body 的 FromRequest(Parts) 实现](../axum/axum-core/src/extract/request_parts.rs#L1-L197) —— `Request` 自身 @ L8-L17,`Method` @ L19-L28,`Uri` @ L30-L39,`Version` @ L41-L50,`HeaderMap` @ L57-L66,`BytesMut` @ L68-L97,`Bytes` @ L99-L115,`String` @ L117-L136,`Parts` @ L138-L148,`Extensions` @ L150-L160,`Body` @ L162-L171。
> - [FromRef trait + blanket impl for T: Clone](../axum/axum-core/src/extract/from_ref.rs#L14-L26) —— `State<T>: FromRequestParts<S> where T: FromRef<S>` 的地基(承 P1-04)。
> - [impl_handler! 宏展开($last: FromRequest<S, M> + M 推断)](../axum/axum/src/handler/mod.rs#L221-L260) —— `req.into_parts()` 拆 parts+body,前 N-1 个 from_request_parts 共享 `&mut parts`,最后一个 from_request 消费 req。承 P3-09。
> - [Json 的 FromRequest + OptionalFromRequest(Content-Type 校验 + Bytes 复用 + serde_path_to_error)](../axum/axum/src/json.rs#L99-L136) —— FromRequest @ L99-L114(Content-Type 校验失败返 MissingJsonContentType,成功收 body 反序列化),OptionalFromRequest @ L116-L136(无 Content-Type 返 None)。
> - [Path 的 FromRequestParts(从 extensions 读 UrlParams)](../axum/axum/src/extract/path/mod.rs#L157) —— Path 不消费 body,只读 extensions。
> - [Query 的 FromRequestParts](../axum/axum/src/extract/query.rs#L53) —— 解析 uri.query() + serde_urlencoded。
> - [State 的 FromRequestParts(不读 parts,只 FromRef)](../axum/axum/src/extract/state.rs#L303) —— 承 P1-04。
> - [axum-core CHANGELOG:RPITIT 改造(PR #2308)+ MSRV 1.75(#2943)+ OptionalFromRequest(#2475)](../axum/axum-core/CHANGELOG.md) —— 0.5 段,alpha.1 段记 RPITIT,rc.1 段记 OptionalFromRequest。
>
> **承接**:`http::Request<B>::into_parts()` 拆 `Parts + Body` 是 `http` crate 方法,`Body` 作为"只能消费一次的 Stream"的语义根在 hyper 协议机(HTTP/1 `ChunkedState` 分帧、HTTP/2 h2 per-stream DATA 帧),详见《hyper》P2-P3 [[hyper-source-facts]] —— 本章一句带过指路,axum 收到的 `Request<Body>` 已经是 hyper 解析好的;`impl Future<...> + Send` 是 RPITIT(Rust 1.75 稳定),Future/Poll 在标准库 `core::future`/`core::task`,承《Tokio》一句带过指路 [[tokio-source-facts]];Handler trait + impl_handler! 宏承 P3-09,本章补全提取器链上两个 trait 的细节。
>
> **修正总纲一处不精准**:总纲(P3-10 章节描述)称 ViaRequest/ViaParts 是"marker struct"。经核实源码 [`axum-core/src/extract/mod.rs#L31-L37`](../axum/axum-core/src/extract/mod.rs#L31-L37),二者是**空 enum**(`pub enum ViaParts {}` / `pub enum ViaRequest {}`,无变体),不是 unit struct。空 enum 永远构造不出值,语义比 unit struct 更强地表达"纯编译期 phantom marker,运行时无实例"。本书正文以源码真值为准。
