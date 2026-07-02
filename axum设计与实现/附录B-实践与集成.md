# 附录 B · axum 实践与集成

> **核心问题**:全书 21 章把 axum 每一个机制(路由匹配、Handler trait 宏展开、FromRequest/FromRequestParts 提取器链、IntoResponse 响应器、from_fn 中间件、serve 内部)都拆透了。可真到了"我要用 axum 写一个能上线的服务",你大概率还会卡在五件事上:① 一个 RESTful 服务的骨架到底怎么搭?② 鉴权/日志/压缩/限流这些横切怎么用 tower-http 拼上去、顺序怎么排?③ 怎么绕开 `axum::serve` 直接用 hyper 做低层控制?④ 怎么写测试(单 handler、整 Router、带 ConnectInfo)?⑤ 从 actix-web/rocket 迁移过来,API 怎么对应?以及凌晨三点线上出事了——handler 编译报"Handler not implemented"、State 类型对不上、中间件顺序错、body 重复消费——怎么照着清单追到根因?
>
> 本附录是**实践手册 + 集成速查 + 排查清单**。代码骨架全部基于 `axum 0.8.9`(`@ c59208c8`)的真实 API,引用的示例全部是 `axum/examples/` 目录下**真实存在的文件**(动手前已 `ls` 核实,共 60+ 示例)。排查清单每条都回扣正文讲透的机制(指路 P-xx),让你照着症状追到根因。

---

## 附录首语 · 为什么单独写一篇"实践与集成"

读完前 21 章,你已经在脑子里搭起了一座 axum 的全景大厦:从 `axum::serve`(P5-17,内部走 hyper-util 的 `auto::Builder` 自动协商 HTTP/1+2)到 `Router::call`(P1-03,impl `tower::Service<Request>`)、`PathRouter::call_with_state`(P2-05,matchit 字典树匹配路径拿 `RouteId`)、`MethodRouter::call_with_state`(P2-06,按 HTTP method 选 handler)、`Handler::call`(P3-09,`impl_handler!` + `all_the_tuples!` 宏对 0~16 参数展开的 tuple 提取器链)、`FromRequestParts`/`FromRequest` 二元划分(P3-10)、`IntoResponse`(P3-12)、`from_fn` 中间件(P4-14)、`Infallible` 错误模型(P5-18)、WebSocket/SSE/流式(P5-19)、0.7→0.8 演进(P6-20)。这套机制你都能讲清楚了。

可真到了要**写代码**的时候,你大概率还会卡在五件事上:

1. **"一个真实的 RESTful 服务到底怎么搭?"** —— 你知道 `Router::new().route("/", get(handler))` 起步,知道提取器、State、Json,可把它们拼成一个"有路由、有状态、有错误处理、有中间件、能上线"的完整服务骨架,还是要过一遍。这一篇给一个基于 `examples/todos` 的真实骨架。
2. **"tower-http 怎么拼?顺序怎么排?"** —— `tower-http`(外部 crate,社区维护,版本 `0.6.x`)提供了 `CorsLayer`/`CompressionLayer`/`TimeoutLayer`/`TraceLayer`/`SetRequestIdLayer` 这一整套现成中间件。它们怎么和 axum 的 `ServiceBuilder` 拼起来?顺序为什么重要(CORS 在最外、compression 在内、Timeout 包住业务)?这一篇给一个生产级的中间件栈骨架。
3. **"怎么绕开 `axum::serve` 直接用 hyper?"** —— `axum::serve` 是简单封装,你要做 TLS 终止、自定义协议升级、连接级过滤、用 hyper 的低层 API 时,要直接手写 `hyper_util::server::conn::auto::Builder::serve_connection`。`examples/serve-with-hyper` 就是这个模板。这一篇拆它的桥接技巧。
4. **"怎么写测试?"** —— axum 的 Router 自己就实现 `tower::Service<Request>`,所以测试不用起真服务器,`Router::oneshot(Request)` 就能测。要测带 `ConnectInfo` 的 handler 怎么办?要跑真 HTTP 集成测怎么办?这一篇给四种测试套路(oneshot、`into_service` + `ready`/`call` 多请求、真服务器、`MockConnectInfo`)。
5. **"线上出事了怎么查?"** —— handler 编译报 `Handler not implemented for ...`(P3-09/13 的类型约束没满足)、State 类型对不上(`Router<S>` 没 `with_state`,P1-04)、中间件顺序反了(`.layer()` 链"后加先执行",P4-16)、两个消费 body 的提取器同时出现(P3-10 二元划分)、404 走了鉴权(该用 `route_layer` 不该用 `layer`,P2-08)、中间件错误没 `HandleError`(P5-18)——这些**真实生产事故的每一种**,根因都能回扣正文某章讲透的机制。这一篇的精华,就是把这些"症状 → 根因 → 解法 → 指路"的排查清单钉死。

本附录的定位是**可操作手册 + 集成速查 + 排查清单**。所有代码骨架基于 axum 0.8.9 真实 API,所有示例引用都是 `axum/examples/` 下**真实存在的目录**(动手前 `ls` 核实过,共 60+ 个示例,本附录引用其中 20+ 个),排查清单每条都回扣正文机制。

> **诚实标注铁律**(承 P5-17/P5-19 的做法,动手前先看清楚):本附录大量涉及 **tower-http**(外部 crate,社区维护,版本 `0.6.x`)、**hyper/hyper-util**(外部 crate,承《hyper》)、**tower**(承《Tower》)、**reqwest**(外部 crate,`TestClient` 内部用它)、**serde/serde_json**(外部 crate)、**tokio**(承《Tokio》)这六个外部 crate。凡是引用它们的代码,只用它们的**公开 API 和文档用法**,**不编内部行号**——比如我会写"`tower_http::cors::CorsLayer`(外部 crate,公开 API)",但绝不编"`CorsLayer` 在 tower-http 某文件某行"。只有 `axum/` 仓内的文件(`axum/src/`、`axum-core/src/`、`axum-macros/src/`),我才标精确行号(基于本地 `../axum/` @ `c59208c8`,版本 0.8.9)。这是本附录最易翻车的点,我守住这条线。
>
> **版本钉死**:全书以 **axum @ `c59208c8`(0.8.9,axum-core 0.5.6,axum-macros 0.5.1)** 为准。`examples/` 下的示例代码就是 0.8.9 版本(`{foo}` 路径参数、`route` 只接 `MethodRouter`、`Router<()>` 交给 serve)。如果你拿到的 axum 是 0.7(`:foo` 路径参数),那是老版本,差异在 P6-20 专门讲。

---

## 第一节 · 写一个 RESTful 服务:从 hello-world 到完整骨架

这一节从最小的 hello-world 起步,一步一步加路由、提取器、State、Json、错误处理、中间件,直到一个完整的 CRUD 服务骨架。骨架对齐 `examples/todos`(axum 官方的 RESTful todo 示例,逐字核实过)。

### 1.1 第零步:hello-world——三行起一个服务

最小可用 axum 服务(`examples/hello-world/src/main.rs` 的核心):

```rust
// 对齐 examples/hello-world/src/main.rs(0.8.9 真实示例,简化展示)
use axum::{routing::get, Router};

#[tokio::main]
async fn main() {
    let app = Router::new().route("/", get(|| async { "Hello, World!" }));

    let listener = tokio::net::TcpListener::bind("0.0.0.0:3000").await.unwrap();
    axum::serve(listener, app).await.unwrap();
}
```

就这三件套:**`Router::new().route("/", get(handler))`** 挂路由、**`tokio::net::TcpListener::bind`** 绑端口、**`axum::serve(listener, app)`** 跑服务。这三件分别承:

- **`Router` + `route` + `get`**:承 P1-03(Router 是 Service)、P2-05(PathRouter matchit 匹配)、P2-06(MethodRouter 按 method 分发)。`get(handler)` 是 `MethodRouter` 的构造,承 P2-06。
- **`TcpListener::bind`**:这是 tokio 的 API(承《Tokio》),`axum::serve` 接的 `listener` 是个抽象(承 P5-17 的 `Listener` trait,支持 `TcpListener`/`UnixListener`)。
- **`axum::serve(listener, app)`**:承 P5-17,内部走 `hyper-util` 的 `server::conn::auto::Builder` 自动协商 HTTP/1+2,每连接 spawn 一个 task 跑协议机。`Router<()>`(默认状态是 `()`)才能交给 `serve`,承 P1-04。

`async fn || async { "Hello, World!" }` 这个闭包凭什么能当 handler?因为它实现了 `Handler<T, ()>` trait——`Handler` 的 blanket impl 由 `impl_handler!` 宏 + `all_the_tuples!` 对 0~16 参数展开,把任意 `async fn` 编译期变成 `tower::Service<Request>`(承 P3-09 招牌章)。返回值 `"Hello, World!"` 是 `&'static str`,实现了 `IntoResponse`(设 `text/plain`,承 P3-12)。

> **钉死这件事**:三件套(`Router::route`/`TcpListener::bind`/`axum::serve`)是 axum 服务的最小骨架。Router/MethodRouter/get 都是 axum 的(承 P1-03/P2-05/P2-06),TcpListener 是 tokio 的(承《Tokio》),`axum::serve` 是 axum 的薄封装(承 P5-17,内部 hyper-util)。底层全是 hyper+Tower+Tokio 的 Service 链——axum 只在它上面加了路由+提取器+响应器这一层。

### 1.2 第一步:加路由、提取器、State

hello-world 太简单,真实服务要有路径参数(`GET /users/{id}`)、query 参数(`GET /todos?done=true`)、共享状态(数据库连接池)。看一个稍微完整点的:

```rust
// 加路由 + 提取器 + State
use axum::{
    extract::{Path, Query, State},
    routing::get,
    Json, Router,
};
use serde::Deserialize;
use std::sync::Arc;

#[derive(Clone)]
struct AppState {
    db: Arc<DbPool>,   // 假想的数据库连接池
}

#[derive(Deserialize)]
struct Pagination { page: Option<u32>, per_page: Option<u32> }

async fn get_user(
    Path(id): Path<u64>,                          // ★ 路径参数提取器(承 P3-11)
    State(state): State<AppState>,                // ★ 共享状态提取器(承 P1-04/P3-11)
) -> Json<User> {
    let user = state.db.find_user(id).await;
    Json(user)
}

async fn list_users(
    Query(pagination): Query<Pagination>,         // ★ query string 提取器(承 P3-11)
    State(state): State<AppState>,
) -> Json<Vec<User>> {
    Json(state.db.list_users(pagination.page.unwrap_or(1)).await)
}

let app = Router::new()
    .route("/users/{id}", get(get_user))           // ★ 0.8 路径参数语法 {id}(承 P6-20)
    .route("/users", get(list_users))
    .with_state(AppState { db: Arc::new(db_pool) });   // ★ with_state 把 Router<AppState> 变 Router<()>
```

四个新东西:

1. **`Path<u64>`**:路径参数提取器。`/users/{id}` 里的 `{id}` 被 matchit 匹配后塞进 `Request::extensions()`(承 P2-05),`Path` 用 serde 反序列化成 `u64`(承 P3-11)。**注意 0.8 语法是 `{id}`,0.7 的 `:id` 会 panic**(除非 `without_v07_checks`,承 P6-20)。
2. **`Query<Pagination>`**:query string 提取器,用 serde 反序列化 `?page=2&per_page=10`(承 P3-11)。`Option<u32>` 让参数可选。
3. **`State<AppState>`**:共享状态提取器,从 `Router` 的状态里拿 `AppState`(承 P1-04 State 泛型编码、P3-11)。`AppState` 必须 `Clone + Send + Sync + 'static`(承 P1-04 的 `FromRef`)。
4. **`.with_state(...)`**:把 `Router<AppState>` 消耗掉变成 `Router<()>`——只有 `Router<()>` 才能交给 `axum::serve`(承 P1-04 招牌)。这一步是"用泛型把缺状态编码进类型"的关键收尾。

handler 的参数顺序有讲究:**`FromRequestParts` 提取器(`Path`/`Query`/`State`)在前,`FromRequest` 提取器(消费 body 的,如 `Json<T>`、`Form<T>`、`Request` 自己)在后**,因为前者只读 `&mut Parts` 可多次跑、后者消费 body 只能跑一次(承 P3-10 二元划分)。下面加 `Json` 时你会看到这个顺序的硬约束。

> **钉死这件事**:提取器顺序的硬约束来自 P3-10——`Handler::call` 内部把 `Request` 拆 `parts + body`,前 N-1 个参数走 `FromRequestParts::from_request_parts(&mut parts, state)`(只读 parts、可多次跑),最后一个参数走 `FromRequest::from_request(req, state)`(消费 body、只能一次)。所以"消费 body 的提取器必须在最后一个参数"是编译期保证的(承 P3-09 的 `impl_handler!` 宏 where 约束)。这是排查项四(body 重复消费)的根因。

### 1.3 第二步:加 Json 提取器与 IntoResponse 返回值

RESTful 服务的 POST/PATCH 要接 JSON body,返回值也要是 JSON。看 `examples/todos` 的真实写法(逐字核实):

```rust
// 对齐 examples/todos/src/main.rs 的 handler 风格(0.8.9 真实示例)
use axum::{extract::State, routing::{get, post}, Json, Router};
use serde::{Deserialize, Serialize};

#[derive(Deserialize)]
struct CreateUser { name: String }

#[derive(Serialize)]
struct User { id: u64, name: String }

async fn users_create(
    State(state): State<AppState>,
    Json(payload): Json<CreateUser>,     // ★ 最后一个参数,消费 body(承 P3-10/P3-11)
) -> Json<User> {                        // ★ 返回值实现 IntoResponse(承 P3-12)
    let user = User { id: state.next_id(), name: payload.name };
    Json(user)                           // ★ Json<User> 自动设 Content-Type: application/json + serde 序列化
}

let app = Router::new()
    .route("/users", post(users_create).get(list_users));
```

注意 `Json<CreateUser>` 在 `users_create` 的**最后一个参数**——因为 `Json` 是 `FromRequest`(消费 body,承 P3-10/P3-11)。如果你把 `State` 放在 `Json` 后面:

```rust
// ❌ 编译错:State 必须在 Json 之前
async fn users_create(
    Json(payload): Json<CreateUser>,     // 消费 body 的提取器在前
    State(state): State<AppState>,       // State 在后
) -> Json<User> { ... }
```

编译器会报"State doesn't implement FromRequest"——因为 `impl_handler!` 宏要求"最后一个参数 `FromRequest`,其余 `FromRequestParts`",`State` 只实现了 `FromRequestParts`(承 P3-09/P3-10)。这正是 P3-13 的 `#[axum::debug_handler]` 宏要帮你定位的错误(排查项一会详拆)。

返回值 `Json<User>` 实现了 `IntoResponse`:它把 `User` 用 serde_json 序列化成字节,设 `Content-Type: application/json`,塞进 `Response` 的 body(承 P3-12)。任何实现了 `IntoResponse` 的类型都能当返回值——`String`(`text/plain`)、`&'static str`、`StatusCode`、`(StatusCode, Json<T>)`(组合响应,承 P3-12 的 tuple 链式拼装)、`Result<T, E>`(T/E 都 IntoResponse)都可以。

### 1.4 第三步:加错误处理——AppError + IntoResponse

真实服务会出错。数据库失败、第三方库失败、用户输入错——这些都要变成合适的 HTTP 响应。`examples/error-handling` 给了 axum 官方推荐的做法:**自定义 `AppError` 枚举 + 实现 `IntoResponse`**。逐字核实的关键部分:

```rust
// 对齐 examples/error-handling/src/main.rs(0.8.9 真实示例,简化展示)
use axum::{
    extract::{rejection::JsonRejection, FromRequest, Request, State},
    http::StatusCode,
    response::{IntoResponse, Response},
    routing::post,
    Router,
};
use serde::Serialize;

// 应用错误枚举
enum AppError {
    JsonRejection(JsonRejection),    // 用户输入 JSON 错
    TimeError(TimeError),            // 第三方库错
}

// 告诉 axum 怎么把 AppError 变成 Response
impl IntoResponse for AppError {
    fn into_response(self) -> Response {
        #[derive(Serialize)]
        struct ErrorResponse { message: String }

        let (status, message) = match self {
            AppError::JsonRejection(rejection) => {
                // 用户输入错,不记日志(预期内)
                (rejection.status(), rejection.body_text())
            }
            AppError::TimeError(err) => {
                // 第三方库错,记日志(TraceLayer 的 span 自带 method/uri)
                tracing::error!(%err, "error from time_library");
                (StatusCode::INTERNAL_SERVER_ERROR, "Something went wrong".to_owned())
            }
        };

        (status, AppJson(ErrorResponse { message })).into_response()
    }
}

// From<...> for AppError 让 handler 里能用 ? 自动转
impl From<JsonRejection> for AppError {
    fn from(rejection: JsonRejection) -> Self { Self::JsonRejection(rejection) }
}
impl From<TimeError> for AppError {
    fn from(error: TimeError) -> Self { Self::TimeError(error) }
}
```

handler 用法:

```rust
async fn users_create(
    State(state): State<AppState>,
    AppJson(params): AppJson<UserParams>,    // 自定义 JSON 提取器(下面解释)
) -> Result<AppJson<User>, AppError> {       // ★ 返回 Result,Err 自动 IntoResponse
    let created_at = Timestamp::now()?;      // ★ ? 自动把 TimeError 转 AppError
    // ...
    Ok(AppJson(user))
}
```

这套做法有三个要点:

1. **`AppError` 是应用自己的错误类型**,集中管理所有可能的错。每个错知道"该回什么 status、该不该记日志、消息要不要暴露给客户端"。
2. **`impl IntoResponse for AppError`** 让 `Result<T, AppError>` 能当返回值——axum 对 `Result<T, E>` 实现 `IntoResponse`(只要 T 和 E 都 `IntoResponse`,承 P3-12),`Err(e)` 自动调 `e.into_response()`。
3. **`From<X> for AppError`** 让 handler 里能用 `?` 自动转错。承 P5-18 的"Infallible 错误模型":Router 的 Service `Error` 永远是 `Infallible`(框架层把所有错误转成 Response),`AppError` 在 `into_response` 这一步变成 Response,不会冒到 hyper 那层。

`examples/error-handling` 还有个细节值得学:**自定义 `AppJson<T>` 提取器**,用 `#[from_request]` 宏(承 P3-13)包一层 `axum::Json`,把默认的 `JsonRejection`(纯文本错误消息)换成自己的 `AppError`:

```rust
// 对齐 examples/error-handling/src/main.rs(0.8.9 真实示例)
#[derive(FromRequest)]
#[from_request(via(axum::Json), rejection(AppError))]
struct AppJson<T>(T);
```

这一行宏展开等价于 `impl FromRequest for AppJson<T>` —— 提取时调 `axum::Json::from_request`,失败把 `JsonRejection` 转成 `AppError`。这样 handler 拿到的就是统一的 `AppError`,而不是 axum 默认的 `JsonRejection`(纯文本,不带结构)。承 P3-13 的自定义提取器与 `#[from_request]` 宏。

### 1.5 第四步:完整的 CRUD 骨架(对齐 examples/todos)

把前面几步拼起来,就是一个完整的 RESTful 服务。`examples/todos` 是 axum 官方的 todo 示例,API 是:

- `GET /todos`:返回所有 todo
- `POST /todos`:创建 todo
- `PATCH /todos/{id}`:更新 todo
- `DELETE /todos/{id}`:删除 todo

它的核心结构(逐字核实后简化展示):

```rust
// 对齐 examples/todos/src/main.rs(0.8.9 真实示例,简化展示)
use axum::{
    error_handling::HandleErrorLayer,
    extract::{Path, Query, State},
    http::StatusCode,
    routing::{get, patch},
    Json, Router,
};
use std::{collections::HashMap, sync::{Arc, RwLock}, time::Duration};
use tower::{BoxError, ServiceBuilder};
use tower_http::trace::TraceLayer;

type Db = Arc<RwLock<HashMap<Uuid, Todo>>>;

#[tokio::main]
async fn main() {
    let db = Db::default();

    let app = Router::new()
        .route("/todos", get(todos_index).post(todos_create))
        .route("/todos/{id}", patch(todos_update).delete(todos_delete))
        // ★ 中间件栈:HandleErrorLayer 兜底 + Timeout + Trace
        .layer(
            ServiceBuilder::new()
                .layer(HandleErrorLayer::new(|error: BoxError| async move {
                    if error.is::<tower::timeout::error::Elapsed>() {
                        Ok(StatusCode::REQUEST_TIMEOUT)
                    } else {
                        Err((StatusCode::INTERNAL_SERVER_ERROR, format!("Unhandled: {error}")))
                    }
                }))
                .timeout(Duration::from_secs(10))
                .layer(TraceLayer::new_for_http())
                .into_inner(),
        )
        .with_state(db);

    let listener = tokio::net::TcpListener::bind("127.0.0.1:3000").await.unwrap();
    axum::serve(listener, app).await.unwrap();
}
```

这个骨架有六个关键点:

1. **`.route("/todos", get(...).post(...))`**:同一个路径挂多个 method,`get(a).post(b)` 是 `MethodRouter` 的链式构造(承 P2-06,`MethodRouter` 按 method 持 `MethodEndpoint`)。`get`/`post`/`patch`/`delete` 都是 `MethodRouter` 的构造函数。
2. **`.route("/todos/{id}", patch(...).delete(...))`**:带路径参数的路由,`{id}` 是 0.8 语法(承 P2-05/P6-20)。`Path<Uuid>` 提取器在 handler 里拿这个 id。
3. **`HandleErrorLayer` + `Timeout`**:`tower::timeout::Timeout`(在 tower crate,承《Tower》)的 `Error` 是 `BoxError`(不是 `Infallible`),直接套到 axum Router 上**编译不过**——因为 Router 要求 `Error = Infallible`(承 P5-18)。`HandleErrorLayer` 是 axum 提供的桥接:它把内层的非 `Infallible` 错误兜底,调你给的闭包把错误转成 `Response`。承 P5-18 的 `HandleErrorLayer`,下面第二节详拆。
4. **`ServiceBuilder::new().layer(A).layer(B).layer(C).into_inner()`**:`ServiceBuilder`(在 tower crate,承《Tower》)把多个 Layer 类型级组合成一个。`.into_inner()` 取出组合后的单个 Layer 交给 `Router::layer`。承 P4-16 的中间件链与 ServiceBuilder。**顺序**是 `.layer()` 先加的在最外层(请求先穿)——这里 `HandleErrorLayer` 在最外、`Trace` 在最内(承 P4-16,下面第二节详拆顺序)。
5. **`TraceLayer::new_for_http()`**:来自 `tower-http`(外部 crate,版本 `0.6.x`)。它给每个请求记 `DEBUG` 日志(method/uri/matched_path/latency/status),是 axum 服务的标配。承 P4-14 的横切关注。
6. **`.with_state(db)`**:把 `Router<Db>` 变 `Router<()>`,只有 `Router<()>` 能交给 `serve`(承 P1-04)。

这是一个能上线的最小骨架。本附录后面所有实践(中间件、tower-http 集成、测试、迁移、排查)都基于这个骨架扩展。

> **钉死这件事**:RESTful 服务骨架 = `Router::new().route(...).layer(中间件栈).with_state(state)` + `axum::serve(listener, app)`。中间件栈的核心是 `HandleErrorLayer`(把非 Infallible 错误兜底)+ 业务中间件(Timeout/Trace/CORS)。`with_state` 是把"缺状态"的 `Router<S>` 消耗成能上线的 `Router<()>` 的收尾动作(承 P1-04)。完整真实示例看 `examples/todos`。

---

## 第二节 · 写中间件:from_fn / from_extractor / ServiceBuilder

第一节那个骨架里的中间件栈(`HandleErrorLayer` + `Timeout` + `Trace`)是"用现成 Layer 拼起来"。但很多时候你要写**自己的中间件**——鉴权、日志、压缩、请求计时、注入 request_id。axum 给三种写法:`from_fn`(最自由,承 P4-14)、`from_extractor`(用提取器校验,承 P4-15)、`ServiceBuilder` 叠多个(承 P4-16)。本节把这三种的真实用法钉死。

### 2.1 from_fn:用 async fn 闭包写中间件

`from_fn` 是 axum 中间件最顺手的写法。承 P4-14(招牌章),你只要写一个 `async fn(提取器..., Request, Next) -> impl IntoResponse` 闭包,`middleware::from_fn` 就把它变成一个 Tower `Layer`。看一个鉴权中间件的真实骨架(承 P4-14 第三节):

```rust
// from_fn 鉴权中间件(承 P4-14)
use axum::{
    extract::Request,
    http::{header::AUTHORIZATION, HeaderMap, StatusCode},
    middleware::{from_fn, Next},
    response::Response,
    routing::get,
    Router,
};

async fn auth_middleware(
    headers: HeaderMap,           // ★ FromRequestParts 提取器(只读 parts,可多次跑)
    request: Request,             // ★ FromRequest 提取器(倒数第二,可消费 body)
    next: Next,                   // ★ Next 固定最后
) -> Result<Response, StatusCode> {   // ★ 返回 impl IntoResponse
    let token = headers.get(AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .ok_or(StatusCode::UNAUTHORIZED)?;
    if !verify_token(token) {
        return Err(StatusCode::UNAUTHORIZED);   // ★ 短路,不调 next
    }
    Ok(next.run(request).await)                   // ★ 通过,调内层
}

fn verify_token(token: &str) -> bool { /* ... */ true }

let app = Router::new()
    .route("/admin/users", get(admin_list_users))
    .route_layer(from_fn(auth_middleware));   // ★ route_layer 只影响已注册路由,不影响 fallback
```

这套写法的精髓在 `next.run(request).await` 这一句——承 P4-14 第四节的"洋葱分界线":

- **`next.run` 之前**:改 request(这里做鉴权判断)。能短路(返回 `Err` 不调 next)。
- **`next.run(request).await`**:调内层(下一层中间件 + handler)。`await` 返回内层产出的 `Response`。
- **`next.run` 之后**:改 response(这里没做)。

这是 `from_fn` 的全部魔力——一个 `async fn` 闭包,用顺序代码同时表达"改 request / 调内层 / 改 response"三件事,远胜 Express 的回调地狱(承 P4-14 第二节对照)。

**注意 `.route_layer` 而不是 `.layer`**:这是 P2-08 拆过的招牌差别——`route_layer` 只影响"已注册的路由",不影响 fallback;`layer` 影响所有(含 fallback)。鉴权中间件要用 `route_layer`,这样未鉴权的请求打不存在的路径直接 404(不走鉴权),而不是先吃 401。承 P2-08 的 route_layer 作用域,排查项五会详拆。

`from_fn` 闭包的签名约束(承 P4-14 第二节,逐条核过源码 `axum/src/middleware/from_fn.rs#L21-L30`):

1. 必须 `async fn`。
2. 前 N-2 个参数是 `FromRequestParts` 提取器(只读 parts)。
3. 倒数第二个参数是 `FromRequest` 提取器(通常是 `Request`)。
4. 最后一个参数是 `Next`。
5. 返回值 `impl IntoResponse`。

### 2.2 from_extractor:用提取器校验当中间件

有时你的鉴权逻辑已经写成了一个提取器(像 `examples/jwt` 里的 `Claims` 提取器),你想让一组路由都自动走这个校验——这时用 `from_extractor`,它把 `FromRequestParts` 提取器包成 Layer。源码签名(`axum/src/middleware/from_extractor.rs#L24-L32`)明确:"If the extractor succeeds the value will be discarded and the inner service will be called. If the extractor fails the rejection will be returned and the inner service will _not_ be called."

看 `examples/jwt` 的真实用法(逐字核实后简化):jwt 示例实际上是**把 `Claims` 直接当 handler 参数**(自定义 `FromRequestParts`),没用 `from_extractor`。但 `from_extractor` 的典型场景是这样的:

```rust
// from_extractor:把提取器当中件(承 P4-15)
use axum::{
    extract::FromRequestParts,
    http::{header, request::Parts, StatusCode},
    middleware::from_extractor,
    routing::get,
    Router,
};

// 一个鉴权提取器
struct RequireAuth;
impl<S> FromRequestParts<S> for RequireAuth
where S: Send + Sync,
{
    type Rejection = StatusCode;
    async fn from_request_parts(parts: &mut Parts, _state: &S) -> Result<Self, Self::Rejection> {
        let auth = parts.headers.get(header::AUTHORIZATION).and_then(|v| v.to_str().ok());
        match auth {
            Some(t) if token_is_valid(t) => Ok(Self),     // 通过,值丢弃
            _ => Err(StatusCode::UNAUTHORIZED),           // 失败,rejection 返回,内层不调
        }
    }
}

let app = Router::new()
    .route("/protected", get(handler))
    .route_layer(from_extractor::<RequireAuth>());   // ★ 这组路由都走 RequireAuth 校验
```

`from_extractor` 和 `from_fn` 的差别(承 P4-15):

| 维度 | `from_fn` | `from_extractor` |
|------|-----------|------------------|
| 接什么 | `async fn` 闭包 | `FromRequestParts` 提取器 |
| 能改 request | 是 | 否(提取器只读 parts) |
| 能短路 | 是(返回 Err) | 是(rejection 即短路) |
| 能改 response | 是(next.run 之后) | **否**(内层调完就完,不给你 response) |
| 典型场景 | 鉴权+日志+计时(前后都做) | 纯校验(只判断过不过) |

选型:**需要"前后都做"或"改 response"** → `from_fn`;**只需要校验(过/不过)** → `from_extractor`。`from_extractor` 的价值是用更窄的 API 表达更窄的意图(只校验,不碰 response),承 P4-15。

**`from_extractor` 的一个关键细节**(源码 `from_extractor.rs` 的 `call`):提取器只跑 `FromRequestParts`(不跑 `FromRequest`),所以**即使你给一个消费 body 的提取器(如 `String`),body 也不会被消费**——源码注释明确说"if the extractor consumes the request body, as String or Bytes does, an empty body will be left in its place"。这意味着用 `from_extractor` 做 body 校验要小心:提取器消费了 body 又丢弃,内层 handler 拿到的是空 body。承 P4-15、P3-10 的 body 一次性消费约束。

### 2.3 examples/jwt 的真实做法:把鉴权提取器当 handler 参数

`examples/jwt`(逐字核实)的做法更直接——它**没用 `from_extractor`**,而是把鉴权逻辑写成一个 `Claims` 提取器(实现 `FromRequestParts`),然后 handler 直接把 `Claims` 当参数:

```rust
// 对齐 examples/jwt/src/main.rs(0.8.9 真实示例,简化展示)
impl<S> FromRequestParts<S> for Claims
where S: Send + Sync,
{
    type Rejection = AuthError;
    async fn from_request_parts(parts: &mut Parts, _state: &S) -> Result<Self, Self::Rejection> {
        // 1. 从 Authorization header 提 Bearer token
        let TypedHeader(Authorization(bearer)) = parts
            .extract::<TypedHeader<Authorization<Bearer>>>().await
            .map_err(|_| AuthError::InvalidToken)?;
        // 2. 用 jsonwebtoken 解码
        let token_data = decode::<Claims>(bearer.token(), &KEYS.decoding, &Validation::default())
            .map_err(|_| AuthError::InvalidToken)?;
        Ok(token_data.claims)
    }
}

// protected handler 直接把 Claims 当参数(提取失败就返回 AuthError)
async fn protected(claims: Claims) -> Result<String, AuthError> {
    Ok(format!("Welcome. Your data:\n{claims}"))
}

let app = Router::new()
    .route("/protected", get(protected))    // ★ 鉴权在提取器层做,handler 干净
    .route("/authorize", post(authorize));
```

这种做法比 `from_extractor` 更简洁——不需要 `.layer(from_extractor::<Claims>())`,只要 handler 把 `Claims` 写进参数列表,提取器自动跑。承 P3-09 的 Handler trait 提取器链、P3-13 的自定义提取器。`AuthError` 实现 `IntoResponse`(承 P3-12),提取失败自动变成 401/400 Response。

**选型建议**(jwt 示例 vs from_extractor vs from_fn):

- **只有几个 handler 要鉴权** → 像 jwt 示例那样,把鉴权提取器当 handler 参数(最简洁)。
- **一组路由都要鉴权,且鉴权逻辑独立** → `from_extractor`(一次挂载,整组路由生效)。
- **鉴权 + 日志 + 改 response 等多需求** → `from_fn`(最自由)。

### 2.4 ServiceBuilder 叠多个中间件:顺序是关键

真实服务要叠多个中间件(CORS + compression + timeout + trace + 鉴权)。承 P4-16,用 `tower::ServiceBuilder`(在 tower crate)把它们类型级组合成一个 Layer:

```rust
// ServiceBuilder 叠多个中间件(承 P4-16)
use std::time::Duration;
use axum::{middleware::from_fn, Router};
use tower::ServiceBuilder;
use tower_http::{
    compression::CompressionLayer,
    cors::CorsLayer,
    timeout::TimeoutLayer,
    trace::TraceLayer,
};

let app = Router::new()
    .route("/", get(handler))
    .layer(
        ServiceBuilder::new()
            .layer(TraceLayer::new_for_http())              // ① 最外层:先记日志
            .layer(CorsLayer::permissive())                 // ② CORS
            .layer(TimeoutLayer::new(Duration::from_secs(10)))  // ③ 超时
            .layer(from_fn(auth_middleware))                 // ④ 鉴权(只这组路由)
            .layer(CompressionLayer::new()),                 // ⑤ 最内:压缩 response
    );
```

**顺序规则**(承 P4-16 招牌对照,这是最容易踩的坑):`ServiceBuilder` 链式从上往下写 = 请求从外往里穿 = **先 `.layer()` 的在最外层,请求先碰**。所以上面请求进来先穿 `TraceLayer`(记日志)→ CORS → Timeout → 鉴权 → Compression → handler。response 反向穿回。

这个顺序为什么这么排?有几个实战考量:

- **`TraceLayer` 在最外**:要在"请求进 + 响应出"两端都记日志,所以它必须包住所有其他中间件(包括 Timeout)。否则 Timeout 砍掉的请求 TraceLayer 看不到。
- **CORS 在外**:浏览器跨域预检(OPTIONS)要在鉴权之前回,否则预检请求会被鉴权挡掉。CORS 在 Timeout 外或内都行,看你要不要给 CORS 请求也限时。
- **Timeout 在鉴权外**:总超时(含鉴权耗时)。如果你只想给 handler 超时,Timeout 放鉴权内。
- **Compression 在最内**:压缩是改 response body,要在 handler 产出 response 之后做。它不能在最外,否则压缩了 TraceLayer 看不到原始 size。承 `examples/compression` 的用法。

> **钉死这件事**:中间件顺序的规则——`ServiceBuilder` 链式"先写先执行(请求阶段)"。这是排查项三(中间件顺序错)的根因。改顺序就改行为:`.timeout(10s).layer(auth)` 是"总超时含鉴权";`.layer(auth).timeout(10s)` 是"只给 handler 后面的链超时"。承 P4-16 的招牌对照表,排查清单会反复回扣。

### 2.5 中间件出错了怎么办:HandleErrorLayer

第一节那个 todos 骨架里,`tower::timeout::Timeout` 的 `Error` 是 `BoxError`(不是 `Infallible`)。但 axum 的 Router 要求中间件的 `Error = Infallible`(承 P5-18,框架层把所有错误转成 Response)。直接套编译不过:

```rust
// ❌ 编译错:Timeout 的 Error 是 BoxError,不是 Infallible
.layer(tower::timeout::TimeoutLayer::new(Duration::from_secs(10)))
```

axum 的解法是 `HandleErrorLayer`——它是个桥接,把内层非 Infallible 的错误兜底,调你给的闭包把错误转成 `Response`。看源码(`axum/src/error_handling/mod.rs#L115-L149`):

```rust
// axum/src/error_handling/mod.rs#L115-L149(逐字摘录)
impl<S, F, B, Fut, Res> Service<Request<B>> for HandleError<S, F, ()>
where
    S: Service<Request<B>> + Clone + Send + 'static,
    S::Response: IntoResponse + Send,
    S::Error: Send,
    S::Future: Send,
    F: FnOnce(S::Error) -> Fut + Clone + Send + 'static,
    Fut: Future<Output = Res> + Send,
    Res: IntoResponse,
    B: Send + 'static,
{
    type Response = Response;
    type Error = Infallible;            // ★ HandleError 把任何 Error 变 Infallible
    type Future = future::HandleErrorFuture;

    fn poll_ready(&mut self, _: &mut Context<'_>) -> Poll<Result<(), Self::Error>> {
        Poll::Ready(Ok(()))             // ★ poll_ready 无条件 Ready(承 P1-03)
    }

    fn call(&mut self, req: Request<B>) -> Self::Future {
        let f = self.f.clone();
        let clone = self.inner.clone();
        let inner = std::mem::replace(&mut self.inner, clone);   // ★ Tower 招牌 idiom(承 P4-14)

        let future = Box::pin(async move {
            match inner.oneshot(req).await {
                Ok(res) => Ok(res.into_response()),              // 内层成功 → Response
                Err(err) => Ok(f(err).await.into_response()),    // 内层失败 → 调你的闭包转 Response
            }
        });
        future::HandleErrorFuture { future }
    }
}
```

读法:`HandleError::call` 用 `inner.oneshot(req)` 调内层(内层 `Error` 可能不是 `Infallible`),`Ok` 直接 `into_response`,`Err` 调你给的闭包 `f(err)` 把错误转成 `IntoResponse`(再 `into_response`)。最终 `HandleError` 自己的 `Error = Infallible`——它把任何错误都吞成了 `Response`。这就是 axum 框架层"错误全转 Response"约定的落地(承 P5-18 的 Infallible 错误模型)。

`examples/todos` 里 `HandleErrorLayer` 的闭包是:

```rust
// 对齐 examples/todos/src/main.rs(0.8.9 真实示例)
.layer(
    ServiceBuilder::new()
        .layer(HandleErrorLayer::new(|error: BoxError| async move {
            if error.is::<tower::timeout::error::Elapsed>() {
                Ok(StatusCode::REQUEST_TIMEOUT)              // 超时 → 408
            } else {
                Err((
                    StatusCode::INTERNAL_SERVER_ERROR,
                    format!("Unhandled internal error: {error}"),
                ))
            }
        }))
        .timeout(Duration::from_secs(10))
        // ...
)
```

闭包签名是 `FnOnce(BoxError) -> impl IntoResponse`。它 match 错误类型:超时(`Elapsed`)回 408,其他回 500。承 P5-18。

**什么情况要 `HandleErrorLayer`?** 任何中间件的 `Error` 不是 `Infallible` 时。axum 自带的 `from_fn`/`from_extractor`/`map_request`/`map_response` 的 `Error` 都是 `Infallible`(承 P4-14),不用套。但 `tower::timeout::Timeout`、`tower::limit::*`、`tower::buffer::Buffer` 这些 tower 中间件的 `Error` 是 `BoxError`,要套 `HandleErrorLayer`。`tower-http` 的多数 Layer(`TraceLayer`/`CorsLayer`/`CompressionLayer`)的 `Error` 是 `Infallible`,不用套。排查项六会详拆"什么情况套 HandleErrorLayer"。

> **钉死这件事**:`HandleErrorLayer` 是 axum 把"非 Infallible 错误"桥接到"Router 要求的 Infallible"的工具。它内部用 `inner.oneshot(req)` 调内层,失败调你的闭包把错误转 `Response`。`tower-http` 的 `TraceLayer`/`CorsLayer`/`CompressionLayer` 的 `Error` 是 `Infallible`,不用套;`tower::timeout::Timeout`/`tower::limit::*` 的 `Error` 是 `BoxError`,要套。承 P5-18,排查项六的根因。

---

## 第三节 · 集成 tower-http:CORS / 压缩 / 限流 / 超时 / trace / 请求 ID

`tower-http`(外部 crate,社区维护,版本 `0.6.x`)是 axum 生态最常用的中间件集合,提供 CORS、压缩、限流、超时、trace、请求 ID、限体等现成 Layer。本节给真实用法 + 注意事项。**所有 Layer 都在 `tower-http` crate,不在 axum 仓**——诚实标注。

### 3.1 CORS:tower_http::cors::CorsLayer

浏览器跨域请求要回 `Access-Control-Allow-Origin` 等 header。`examples/cors` 的真实写法(逐字核实):

```rust
// 对齐 examples/cors/src/main.rs(0.8.9 真实示例)
use axum::{http::{HeaderValue, Method}, routing::get, Json, Router};
use tower_http::cors::CorsLayer;

let app = Router::new().route("/json", get(json)).layer(
    CorsLayer::new()
        .allow_origin("http://localhost:3000".parse::<HeaderValue>().unwrap())
        .allow_methods([Method::GET]),
);
```

`CorsLayer::new()` 是空配置(什么都不允许),你链式加 `.allow_origin(...)`/`.allow_methods(...)`/`.allow_headers(...)`。`CorsLayer::permissive()` 是最宽松(允许一切,只适合开发)。

**实战注意事项**:

1. **`Content-Type` header 要单独 allow**。`examples/cors` 的注释明确说:"for some request types like posting content-type: application/json it is required to add `.allow_headers([http::header::CONTENT_TYPE])`"。否则 POST JSON 会被 CORS 挡掉。
2. **CORS 要在鉴权外**。浏览器跨域预检(OPTIONS)不带鉴权 header,如果 CORS 在鉴权内,预检请求会被鉴权挡掉,浏览器拿不到 CORS 响应。承第二节 2.4 的顺序规则。
3. **`allow_origin` 用具体值,不要用 `*`(除非公开 API)**。`*` 不能配合 cookie(`Access-Control-Allow-Credentials: true` 时 `*` 无效)。

### 3.2 压缩:tower_http::compression::CompressionLayer

response body 大于阈值就 gzip/br/deflate 压一下,根据 `Accept-Encoding` header。`examples/compression` 的真实写法:

```rust
// 对齐 examples/compression/src/main.rs(0.8.9 真实示例)
use axum::{routing::post, Json, Router};
use tower::ServiceBuilder;
use tower_http::{compression::CompressionLayer, decompression::RequestDecompressionLayer};

fn app() -> Router {
    Router::new().route("/", post(root)).layer(
        ServiceBuilder::new()
            .layer(RequestDecompressionLayer::new())   // 解压请求 body(客户端压缩上传)
            .layer(CompressionLayer::new()),            // 压缩响应 body
    )
}
```

`CompressionLayer::new()` 默认启用 gzip/br/deflate/zstd(看 feature flag,`compression-full` 全开)。`examples/compression` 的 `Cargo.toml` 用 `features = ["compression-full", "decompression-full"]`。

**实战注意事项**:

1. **CompressionLayer 要在 body 流式前套**。如果你 handler 返回一个 `Body::from_stream`(流式响应,承 P5-19),CompressionLayer 会逐 chunk 压缩——这是支持的,但要注意压缩是流式的,不能在 compression 后再读完整 body。
2. **Compression 要在 TraceLayer 内**(或 TraceLayer 配置不记 size)。否则 TraceLayer 记的 response size 是压缩前的(误导)。
3. **`CompressionLayer` 的 `Error` 是 `Infallible`**,不用套 `HandleErrorLayer`。承 P5-18。
4. **请求解压要单独用 `RequestDecompressionLayer`**(客户端压缩上传时),它和 `CompressionLayer` 是两个独立的 Layer。

### 3.3 限流与并发限制

`tower-http` **不提供**限流 Layer(它的 scope 是 HTTP 语义相关的中间件)。限流在 **`tower` crate**(承《Tower》):

- **`tower::limit::ConcurrencyLimitLayer`**:限并发(Semaphore permit),承《Tower》P3-09。
- **`tower::limit::RateLimitLayer`**:限速率(令牌桶),承《Tower》P3-10。

在 axum 里的用法:

```rust
// 限流(在 tower crate,承《Tower》)
use std::time::Duration;
use axum::{error_handling::HandleErrorLayer, Router};
use tower::{BoxError, ServiceBuilder};
use tower::limit::{ConcurrencyLimitLayer, RateLimitLayer};

let app = Router::new()
    .route("/", get(handler))
    .layer(
        ServiceBuilder::new()
            .layer(HandleErrorLayer::new(|err: BoxError| async move {
                // ConcurrencyLimit/RateLimit 满了会返回错误,要 HandleError 兜底
                (axum::http::StatusCode::SERVICE_UNAVAILABLE, format!("{err}")).into_response()
            }))
            .concurrency_limit(100)                          // 并发上限 100(承《Tower》P3-09)
            .rate_limit(1000, Duration::from_secs(1)),       // 每秒 1000 请求(承《Tower》P3-10)
    );
```

注意 `ConcurrencyLimit`/`RateLimit` 的 `Error` 不是 `Infallible`(Semaphore/令牌桶满了会返回错误),所以**要套 `HandleErrorLayer`**——这是排查项六的根因之一。限流参数怎么调(并发 vs 速率选哪个、Buffer 容量怎么设),看《Tower》附录 B 第三节的调优经验(承《Tower》)。

> **承接《Tower》**:ConcurrencyLimit/RateLimit/Buffer/Timeout 这些限流/缓冲中间件的内部机制(Semaphore permit、令牌桶、worker task + mpsc、`select!` + sleep),《Tower》全书拆透了,本附录一句带过指路。axum 这里只是"用",用法和参数选择看《Tower》。

### 3.4 超时:tower_http::timeout::TimeoutLayer

`tower-http` 提供了**带 status code 的 `TimeoutLayer`**(比 `tower::timeout::TimeoutLayer` 更顺手)。`examples/graceful-shutdown` 用的就是这个:

```rust
// 对齐 examples/graceful-shutdown/src/main.rs(0.8.9 真实示例)
use std::time::Duration;
use axum::{http::StatusCode, routing::get, Router};
use tower_http::timeout::TimeoutLayer;
use tower_http::trace::TraceLayer;

let app = Router::new()
    .route("/slow", get(|| sleep(Duration::from_secs(5))))
    .layer((
        TraceLayer::new_for_http(),
        TimeoutLayer::with_status_code(StatusCode::REQUEST_TIMEOUT, Duration::from_secs(10)),
    ));
```

`TimeoutLayer::with_status_code` 超时直接回指定 status(这里是 408),不用你写 `HandleErrorLayer` 兜底——`tower-http` 版本已经把错误处理内化了(`Error` 是 `Infallible`)。这比 `tower::timeout::TimeoutLayer`(要套 `HandleErrorLayer`)省心。注意两者的区别:`tower_http::timeout::TimeoutLayer` 超时回固定 status;`tower::timeout::TimeoutLayer` 超时返回 `Elapsed` 错误(你要自己 match)。

**注意 `examples/graceful-shutdown` 用了 `.layer((A, B))` 元组语法**:这是 axum 0.8 的便利写法——`.layer((TraceLayer, TimeoutLayer))` 等价于 `.layer(TraceLayer).layer(TimeoutLayer)`(顺序:元组前面的在外层)。承 P4-16。

### 3.5 链路追踪:tower_http::trace::TraceLayer

`TraceLayer` 是 axum 服务的事实标配,几乎每个 example 都用。它给每个请求记 `DEBUG`/`INFO` 日志(method/uri/matched_path/latency/status),配合 `tracing` 生态(`tracing-subscriber` 等)。`examples/error-handling` 的进阶用法(自定义 span,带 matched_path):

```rust
// 对齐 examples/error-handling/src/main.rs(0.8.9 真实示例,简化展示)
use axum::{extract::{MatchedPath, Request}, routing::post, Router};
use tower_http::trace::TraceLayer;

let app = Router::new()
    .route("/users", post(users_create))
    .layer(
        TraceLayer::new_for_http()
            .make_span_with(|req: &Request| {
                let method = req.method();
                let uri = req.uri();
                // matched_path 是 axum 在路由匹配后自动塞进 extensions 的(承 P2-05)
                let matched_path = req.extensions().get::<MatchedPath>().map(|p| p.as_str());
                tracing::debug_span!("request", %method, %uri, matched_path)
            })
            .on_failure(()),    // 关掉默认的 5xx 失败日志(自己做)
    );
```

三个要点:

1. **`MatchedPath`** 是 axum 在路由匹配后自动塞进 `Request::extensions()` 的(承 P2-05 的 url_params),它告诉你"这个请求匹配到了哪条路由"(如 `/users/{id}`)。在 span 里带上它,日志能按路由聚合。
2. **`.on_failure(())`** 关掉默认的 5xx 失败日志——如果你自己做错误日志(像 `examples/error-handling` 那样在 `AppError::into_response` 里记),关掉避免双份。
3. **`TraceLayer` 的 `Error` 是 `Infallible`**,不用套 `HandleErrorLayer`。

### 3.6 请求 ID:tower_http::request_id

给每个请求分配一个唯一 ID,塞进 header 和 `tracing` span,方便日志聚合。`examples/request-id` 的完整做法:

```rust
// 对齐 examples/request-id/src/main.rs(0.8.9 真实示例)
use axum::{http::{HeaderName, Request}, routing::get, Router};
use tower::ServiceBuilder;
use tower_http::{
    request_id::{MakeRequestUuid, PropagateRequestIdLayer, SetRequestIdLayer},
    trace::TraceLayer,
};

const REQUEST_ID_HEADER: &str = "x-request-id";

let x_request_id = HeaderName::from_static(REQUEST_ID_HEADER);

let middleware = ServiceBuilder::new()
    .layer(SetRequestIdLayer::new(x_request_id.clone(), MakeRequestUuid))   // ① 生成 ID 塞 request header
    .layer(
        TraceLayer::new_for_http().make_span_with(|request: &Request<_>| {
            let request_id = request.headers().get(REQUEST_ID_HEADER);       // ② span 里带上 ID
            match request_id {
                Some(id) => tracing::info_span!("http_request", request_id = ?id),
                None => tracing::info_span!("http_request"),
            }
        }),
    )
    .layer(PropagateRequestIdLayer::new(x_request_id));                       // ③ 把 ID 从 request 传到 response

let app = Router::new().route("/", get(handler)).layer(middleware);
```

三步:**`SetRequestIdLayer`**(生成 UUID 塞 request header)→ **`TraceLayer`**(span 里读这个 header)→ **`PropagateRequestIdLayer`**(把 ID 传到 response header,客户端能看到)。注意 `examples/request-id` 的 `Cargo.toml` 要开 tower-http 的 `request-id` 和 `trace` feature。

**顺序很重要**:`SetRequestIdLayer` 要在 `TraceLayer` 外(先生成 ID,TraceLayer 才能读到)。承 P4-16 的顺序规则——`ServiceBuilder` 先 `.layer()` 的在外层。所以这里的写法 `.layer(SetRequestIdLayer).layer(TraceLayer).layer(PropagateRequestIdLayer)` 表示请求进来先 `SetRequestId`(最外)→ `Trace` → `Propagate` → handler。注意 `PropagateRequestIdLayer` 是 response 阶段的(把 request 的 ID 复制到 response),它在最内不影响生成,因为它在 response 反向穿时执行最早(承 P4-14 的洋葱模型)。

### 3.7 限体:DefaultBodyLimit

axum 0.8 内置了 `DefaultBodyLimit`(在 `axum-core`),限制请求 body 大小(默认 2MB)。`Json`/`Form` 这些消费 body 的提取器都受这个限制保护(承 P3-11 的 Json 大小限制)。要改默认:

```rust
// 改默认 body 限制(承 P3-11)
use axum::extract::DefaultBodyLimit;

let app = Router::new()
    .route("/upload", post(upload))
    .layer(DefaultBodyLimit::max(1024 * 1024 * 10));   // 10MB
```

`DefaultBodyLimit` 不在 tower-http,在 `axum-core`(axum 自己的 crate)。承 P3-11。

### 3.8 一张速查表:tower-http 常用 Layer

把本节的 Layer 汇总成速查表(全部在 `tower-http` crate,版本 `0.6.x`,诚实标注):

| Layer | 模块 | 作用 | Error 类型 | 要 HandleError? | 真实示例 |
|------|------|------|-----------|----------------|---------|
| `CorsLayer` | `tower_http::cors` | CORS 跨域头 | Infallible | 否 | `examples/cors` |
| `CompressionLayer` | `tower_http::compression` | gzip/br/deflate/zstd 压响应 | Infallible | 否 | `examples/compression` |
| `RequestDecompressionLayer` | `tower_http::decompression` | 解压请求 body | Infallible | 否 | `examples/compression` |
| `TimeoutLayer` | `tower_http::timeout` | 超时回固定 status | Infallible | 否 | `examples/graceful-shutdown` |
| `TraceLayer` | `tower_http::trace` | 链路追踪日志 | Infallible | 否 | 几乎所有示例 |
| `SetRequestIdLayer` | `tower_http::request_id` | 生成请求 ID | Infallible | 否 | `examples/request-id` |
| `PropagateRequestIdLayer` | `tower_http::request_id` | 请求 ID 传响应 | Infallible | 否 | `examples/request-id` |
| `CatchPanicLayer` | `tower_http::catch_panic` | 捕获 handler panic | Infallible | 否 | (无官方示例,文档) |

`tower-http` 的 Layer **`Error` 都是 `Infallible`**(社区维护者刻意设计成不渗漏错误),所以都不用套 `HandleErrorLayer`。要套 `HandleErrorLayer` 的是 `tower::timeout::Timeout`、`tower::limit::*`、`tower::buffer::Buffer` 这些 tower 自带中间件(承 P5-18、第二节 2.5)。这是排查项六的关键判别。

> **钉死这件事**:tower-http 的 Layer 全是 `Error = Infallible`,不用 `HandleErrorLayer`;tower 自带的 Timeout/ConcurrencyLimit/RateLimit/Buffer 的 Error 是 BoxError,要 `HandleErrorLayer`。这个判别是排查项六的根因。所有 Layer 的用法都诚实标注"在 tower-http crate(外部)",不编内部行号。

---

## 第四节 · 与 hyper 直接集成:serve-with-hyper

`axum::serve` 是个简单封装,只支持 `TcpListener`、不直接支持 TLS、不让你做连接级控制。要绕开它直接用 hyper,看 `examples/serve-with-hyper`。这个示例是 axum 官方的"低层控制"模板,逐字核实过。

### 4.1 为什么要绕开 axum::serve

`axum::serve`(承 P5-17)内部走 `hyper-util` 的 `auto::Builder::serve_connection_with_upgrades`,把"accept 连接 + 跑协议机 + 调 Router"全打包。但它有几个限制:

1. **不直接支持 TLS**。生产环境要 HTTPS,要用 `examples/tls-rustls`/`examples/low-level-rustls`/`examples/tls-graceful-shutdown` 这些示例的做法(手动 accept TLS 流)。
2. **不让你做连接级控制**。比如按对端 IP 限流、按 TLS SNI 分流、自定义协议升级——这些要在 accept 之后、跑协议机之前插一手。
3. **不让你选 HTTP 版本**。`axum::serve` 自动协商 HTTP/1+2,你要只跑 HTTP/1 或 HTTP/3 要自己控制。

这些场景下,直接用 `hyper-util` 的低层 API。

### 4.2 serve-with-hyper 的核心:service_fn 桥接 Tower↔hyper

`examples/serve-with-hyper` 的核心思路是:**手动 accept 连接,用 `hyper::service::service_fn` 把 axum 的 Tower Service 包成 hyper Service**。逐字核实后的简化骨架:

```rust
// 对齐 examples/serve-with-hyper/src/main.rs(0.8.9 真实示例,简化展示)
use axum::{extract::Request, routing::get, Router};
use hyper::body::Incoming;
use hyper_util::rt::{TokioExecutor, TokioIo};
use hyper_util::server;
use tokio::net::TcpListener;
use tower::{Service, ServiceExt};   // ★ for call / oneshot / clone

async fn serve_plain() {
    let app = Router::new().route("/", get(|| async { "Hello!" }));
    let listener = TcpListener::bind("0.0.0.0:3000").await.unwrap();

    loop {
        let (socket, _remote_addr) = listener.accept().await.unwrap();
        let tower_service = app.clone();   // ★ Router: Clone,每个连接 clone 一份

        tokio::spawn(async move {
            // ① TokioIo 把 tokio 的 AsyncRead/AsyncWrite 转成 hyper 的(承《hyper》)
            let socket = TokioIo::new(socket);

            // ② service_fn 把 Tower Service 包成 hyper Service
            let hyper_service = hyper::service::service_fn(move |request: Request<Incoming>| {
                // hyper Service 用 &self,Tower Service 要 &mut self,所以 clone 后 call
                // Router 永远 ready,不用 poll_ready(承 P1-03)
                tower_service.clone().call(request)
            });

            // ③ hyper-util 的 auto::Builder 自动协商 HTTP/1+2
            if let Err(err) = server::conn::auto::Builder::new(TokioExecutor::new())
                .serve_connection_with_upgrades(socket, hyper_service)
                .await
            {
                eprintln!("failed to serve connection: {err:#}");
            }
        });
    }
}
```

三个关键点:

1. **`TokioIo::new(socket)`**:hyper 有自己的 `AsyncRead`/`AsyncWrite` trait(不用 tokio 的,承《hyper》),`TokioIo` 是 `hyper-util` 提供的适配器,把 tokio 的 IO 转成 hyper 的。承《hyper》P4。
2. **`hyper::service::service_fn(move |request| tower_service.clone().call(request))`**:这是桥接的核心。hyper 的 Service trait 用 `&self`(承《hyper》),Tower 的 Service trait 用 `&mut self`(承《Tower》)——签名不兼容。`service_fn` 包一个闭包,闭包里 `tower_service.clone().call(request)` 解决矛盾:每次请求 clone 一份 Tower Service 来 call。Router 永远 ready(`poll_ready` 无条件 `Ready`,承 P1-03),所以不用调 `poll_ready`。
3. **`server::conn::auto::Builder::new(TokioExecutor::new()).serve_connection_with_upgrades(socket, hyper_service)`**:这是 `hyper-util` 的连接级 API,`auto::Builder` 自动协商 HTTP/1+2,`TokioExecutor` 告诉 hyper 用 `tokio::spawn` 派 task,`serve_connection_with_upgrades` 支持 WebSocket 升级(承 P5-19)。承《hyper》P5。

### 4.3 axum::serve 内部就是这么做的

有趣的是,`axum::serve` 内部用的就是这套桥接——只不过它把 `service_fn` 换成了 `hyper-util` 提供的现成适配器 `TowerToHyperService`。看 `axum/src/serve/mod.rs` 的 `handle_connection` 函数(逐字核实):

```rust
// axum/src/serve/mod.rs handle_connection 函数(简化展示,逐字核过)
async fn handle_connection<L, M, S>(make_service: &mut M, signal_tx: &watch::Sender<()>, close_rx: &watch::Receiver<()>, io: <L as Listener>::Io, remote_addr: <L as Listener>::Addr)
where /* ... */
{
    let io = TokioIo::new(io);
    // make_service 拿到 tower_service(就是你的 Router)
    let tower_service = make_service.call(/* IncomingStream */).await...;
    // ★ TowerToHyperService:hyper-util 提供的桥接(等价于 service_fn 那段闭包)
    let hyper_service = TowerToHyperService::new(tower_service);

    tokio::spawn(async move {
        let mut builder = Builder::new(TokioExecutor::new());
        #[cfg(feature = "http2")]
        builder.http2().enable_connect_protocol();   // HTTP/2 WebSocket CONNECT 协议

        let mut conn = pin!(builder.serve_connection_with_upgrades(io, hyper_service));
        let mut signal_closed = pin!(signal_tx.closed().fuse());

        loop {
            tokio::select! {
                result = conn.as_mut() => { if let Err(_e) = result { break; } }
                _ = &mut signal_closed => {
                    conn.as_mut().graceful_shutdown();   // ★ 收到信号,优雅关连接
                }
            }
        }
        drop(close_rx);
    });
}
```

`TowerToHyperService::new(tower_service)` 就是 `examples/serve-with-hyper` 里那段 `service_fn(move |req| tower_service.clone().call(req))` 的封装版——`hyper-util` 把这个常用模式做成了类型。所以 `axum::serve` 内部 = `examples/serve-with-hyper` 的循环 + `signal_tx.closed()` 的优雅关 + `enable_connect_protocol` 的 HTTP/2 WebSocket 支持。

**这意味着**:`examples/serve-with-hyper` 不是"另一种跑 axum 的方式",而是"`axum::serve` 的展开版"——你拿到的是 `axum::serve` 内部的全部控制权。要加 TLS?在 `TokioIo::new(...)` 外面套一层 `tokio_rustls::server::TlsAcceptor`(承 `examples/tls-rustls`)。要按对端 IP 限流?在 `listener.accept().await` 后插一手。要做 HTTP/3?换 `quinn` + `h3`。承 P5-17。

### 4.4 带 ConnectInfo 的低层做法

`examples/serve-with-hyper` 还有第二个函数 `serve_with_connect_info`,演示怎么在低层 API 下拿到对端地址(给 `ConnectInfo` 提取器用,承 P5-17)。逐字核实后的简化骨架:

```rust
// 对齐 examples/serve-with-hyper/src/main.rs 的 serve_with_connect_info(简化展示)
async fn serve_with_connect_info() {
    let app: Router = Router::new().route("/", get(
        |ConnectInfo(remote_addr): ConnectInfo<SocketAddr>| async move {
            format!("Hello {remote_addr}")
        },
    ));

    // ★ into_make_service_with_connect_info 让 Router 知道对端地址
    let mut make_service = app.into_make_service_with_connect_info::<SocketAddr>();
    let listener = TcpListener::bind("0.0.0.0:3001").await.unwrap();

    loop {
        let (socket, remote_addr) = listener.accept().await.unwrap();
        // ★ make_service.call(remote_addr) 产出带 ConnectInfo 的 tower_service
        let tower_service = unwrap_infallible(make_service.call(remote_addr).await);

        tokio::spawn(async move {
            let socket = TokioIo::new(socket);
            let hyper_service = hyper::service::service_fn(move |request: Request<Incoming>| {
                tower_service.clone().oneshot(request)   // ★ 这里用 oneshot,因为拿不到 &mut
            });
            // ... serve_connection_with_upgrades ...
        });
    }
}

fn unwrap_infallible<T>(result: Result<T, Infallible>) -> T {
    match result { Ok(v) => v, Err(err) => match err {} }
}
```

关键差别:

1. **`app.into_make_service_with_connect_info::<SocketAddr>()`**:把 `Router` 变成一个 `MakeService`——它接 `remote_addr`,产出"已经把 `ConnectInfo(remote_addr)` 塞进 extensions 的 Router"。承 P5-17 的 `IntoMakeServiceWithConnectInfo`。
2. **`make_service.call(remote_addr).await`**:用对端地址造一个带 `ConnectInfo` 的 tower_service。
3. **闭包里用 `oneshot` 而不是 `call`**:因为 `tower_service.clone()` 是 owned 的(没有 `&mut`),用 `oneshot`(内部 `mem::replace` + `call`,承《Tower》)调一次。

`axum::serve(listener, app.into_make_service_with_connect_info::<SocketAddr>())` 是高层等价物——`axum::serve` 内部 `handle_connection` 用的 `make_service.call(IncomingStream { io, remote_addr })` 就是这套。承 P5-17。

> **钉死这件事**:与 hyper 直接集成 = 手动 accept + `TokioIo` 转 IO + `service_fn`(或 `TowerToHyperService`)桥接 Tower↔hyper + `auto::Builder::serve_connection_with_upgrades` 跑协议机。`axum::serve` 内部就是这么做的(`handle_connection` 函数),`examples/serve-with-hyper` 是它的"展开版"。要 TLS/连接级控制/HTTP/3,在 `TokioIo` 外或 `accept` 后插一手。承 P5-17、《hyper》P4-P5。

---

## 第五节 · 测试:oneshot / into_service / 真服务器 / MockConnectInfo

axum 的 Router 自己实现 `tower::Service<Request<Body>>`(承 P1-03),所以测试**不用起真 HTTP 服务器**——`Router::oneshot(Request)` 直接测。`examples/testing` 是 axum 官方测试示例,逐字核实过。本节给四种测试套路。

### 5.1 套路一:oneshot 单请求(最常用)

`Router` 实现 `tower::ServiceExt::oneshot`(在 tower crate,承《Tower》),`oneshot(req)` 跑一次返回 `Response`。`examples/testing` 的 `hello_world` 测试:

```rust
// 对齐 examples/testing/src/main.rs 的 hello_world 测试(0.8.9 真实示例)
use axum::{body::Body, http::{Request, StatusCode}, routing::get, Router};
use http_body_util::BodyExt;     // for collect
use tower::ServiceExt;           // for oneshot

fn app() -> Router {
    Router::new().route("/", get(|| async { "Hello, World!" }))
}

#[tokio::test]
async fn hello_world() {
    let app = app();

    // ★ Router 实现 Service<Request<Body>>,直接 call,不起 HTTP 服务器
    let response = app
        .oneshot(Request::builder().uri("/").body(Body::empty()).unwrap())
        .await
        .unwrap();

    assert_eq!(response.status(), StatusCode::OK);

    let body = response.into_body().collect().await.unwrap().to_bytes();
    assert_eq!(&body[..], b"Hello, World!");
}
```

要点:

1. **`app().oneshot(Request::builder().uri("/").body(Body::empty()).unwrap())`**:`oneshot` 消费 `app`,跑一次,返回 `Result<Response, Infallible>`(`unwrap` 拿 Response)。承 P1-03 的 Router impl Service。
2. **`response.into_body().collect().await.unwrap().to_bytes()`**:body 是 `axum_core::body::Body`(承《hyper》Body as Stream),要 `collect`(在 `http-body-util` crate)聚成 `Bytes` 才能断言。注意 `collect` 消费 body。
3. **POST JSON 测试**:`examples/testing` 的 `json` 测试展示怎么构造带 body 和 header 的 Request:

```rust
// 对齐 examples/testing/src/main.rs 的 json 测试
let response = app
    .oneshot(
        Request::builder()
            .method(http::Method::POST)
            .uri("/json")
            .header(http::header::CONTENT_TYPE, mime::APPLICATION_JSON.as_ref())
            .body(Body::from(serde_json::to_vec(&json!([1, 2, 3, 4])).unwrap()))
            .unwrap(),
    )
    .await
    .unwrap();
```

注意 `Content-Type: application/json` header 必须设,否则 `Json` 提取器会拒绝(承 P3-11 的 Content-Type 校验)。

4. **404 测试**:`examples/testing` 的 `not_found` 测试展示测 fallback:

```rust
// 对齐 examples/testing 的 not_found 测试
let response = app
    .oneshot(Request::builder().uri("/does-not-exist").body(Body::empty()).unwrap())
    .await
    .unwrap();
assert_eq!(response.status(), StatusCode::NOT_FOUND);
```

承 P2-08 的 fallback 行为。

### 5.2 套路二:into_service + ready/call 多请求

`oneshot` 消费 `app`,要测多个请求要每次重建 `app`,或者用 `into_service` + `ready`/`call`。`examples/testing` 的 `multiple_request` 测试:

```rust
// 对齐 examples/testing/src/main.rs 的 multiple_request 测试(0.8.9 真实示例)
use tower::{Service, ServiceExt};   // for call / ready

#[tokio::test]
async fn multiple_request() {
    let mut app = app().into_service();   // ★ into_service 把 Router 变成可反复 call 的 Service

    let request = Request::builder().uri("/").body(Body::empty()).unwrap();
    let response = ServiceExt::<Request<Body>>::ready(&mut app).await.unwrap().call(request).await.unwrap();
    assert_eq!(response.status(), StatusCode::OK);

    let request = Request::builder().uri("/").body(Body::empty()).unwrap();
    let response = ServiceExt::<Request<Body>>::ready(&mut app).await.unwrap().call(request).await.unwrap();
    assert_eq!(response.status(), StatusCode::OK);
}
```

`Router::into_service(self)`(`axum/src/routing/mod.rs#L500`)把 `Router<S>` 变成一个 `RouterIntoService`(impl `Service<Request>`)——它支持 `&mut self` 的 `call`,可以反复调用(每次先 `ready().await` 再 `call`)。这是 Tower Service 的标准用法(承《Tower》的 `poll_ready` + `call` 两步)。Router 的 `poll_ready` 无条件 `Ready`(承 P1-03),所以 `ready().await` 立即返回。

### 5.3 套路三:真服务器(hyper-util client)

有些场景要起真 HTTP 服务器测(比如测 keep-alive、测真实的 Content-Type 协商、测中间件和真协议机的交互)。`examples/testing` 的 `the_real_deal` 测试:

```rust
// 对齐 examples/testing/src/main.rs 的 the_real_deal 测试(0.8.9 真实示例)
use tokio::net::TcpListener;

#[tokio::test]
async fn the_real_deal() {
    let listener = TcpListener::bind("0.0.0.0:0").await.unwrap();   // ★ 0 = 随机端口
    let addr = listener.local_addr().unwrap();

    tokio::spawn(async move {
        axum::serve(listener, app()).await.unwrap();
    });

    // ★ 用 hyper-util 的 client 发真请求
    let client = hyper_util::client::legacy::Client::builder(hyper_util::rt::TokioExecutor::new()).build_http();
    let response = client
        .request(
            Request::builder()
                .uri(format!("http://{addr}"))
                .header("Host", "localhost")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();

    let body = response.into_body().collect().await.unwrap().to_bytes();
    assert_eq!(&body[..], b"Hello, World!");
}
```

要点:

1. **`TcpListener::bind("0.0.0.0:0")`**:绑随机端口(`0` 让 OS 分配),避免端口冲突。
2. **`tokio::spawn` 跑 server**:server 在后台 task 跑,测试用 client 连它。
3. **`hyper_util::client::legacy::Client`**:这是 hyper-util 的客户端,发真 HTTP 请求。注意 `examples/testing` 的 `Cargo.toml` 把 `hyper-util` 放 `[dependencies]`(不是 dev-deps),因为 main.rs 也用——但测试里用没问题。

这套路的代价是要起真服务器(更慢、要管端口),但能测到真协议机的行为(承《hyper》P2-P3)。一般优先用套路一(oneshot),只在必要时用套路三。

### 5.4 套路四:MockConnectInfo(测带 ConnectInfo 的 handler)

handler 用了 `ConnectInfo<SocketAddr>` 提取器(承 P5-17),在 `oneshot` 测试里要怎么提供?——`ConnectInfo` 平时由 `Router::into_make_service_with_connect_info` 在 accept 时塞进 extensions(承第四节 4.4),测试里没 accept,塞不进去。axum 提供了 `MockConnectInfo` Layer:

```rust
// 对齐 examples/testing/src/main.rs 的 with_into_make_service_with_connect_info 测试
use axum::extract::connect_info::MockConnectInfo;

#[tokio::test]
async fn with_into_make_service_with_connect_info() {
    let mut app = app()
        .layer(MockConnectInfo(SocketAddr::from(([0, 0, 0, 0], 3000))))   // ★ 模拟 ConnectInfo
        .into_service();

    let request = Request::builder().uri("/requires-connect-info").body(Body::empty()).unwrap();
    let response = app.ready().await.unwrap().call(request).await.unwrap();
    assert_eq!(response.status(), StatusCode::OK);
}
```

`MockConnectInfo<T>(pub T)`(`axum/src/extract/connect_info.rs#L221`)是个 Layer,它把指定的 `ConnectInfo` 塞进 `extensions`,让 `ConnectInfo<T>` 提取器在测试里能提到。源码(`connect_info.rs#L160-L168`):

```rust
// axum/src/extract/connect_info.rs#L160-L168(逐字摘录)
async fn from_request_parts(parts: &mut Parts, state: &S) -> Result<Self, Self::Rejection> {
    match Extension::<Self>::from_request_parts(parts, state).await {
        Ok(Extension(connect_info)) => Ok(connect_info),
        Err(err) => match parts.extensions.get::<MockConnectInfo<T>>() {
            Some(MockConnectInfo(connect_info)) => Ok(Self(connect_info.clone())),   // ★ MockConnectInfo 兜底
            None => Err(err),
        },
    }
}
```

读法:`ConnectInfo::from_request_parts` 先试 `Extension<ConnectInfo>`(正常路径),失败后看 extensions 里有没有 `MockConnectInfo<T>`,有就用它的值。所以测试里套 `MockConnectInfo(假地址)`,`ConnectInfo` 提取器就能提到这个假地址。源码注释还提了个细节:`MockConnectInfo` 和 `into_make_service_with_connect_info` 同时用,后者优先(extensions 里 `Extension<ConnectInfo>` 命中第一条)。

### 5.5 关于 TestClient 的诚实标注

你可能在 axum 源码的 `test_helpers` 模块看到过 `TestClient`(`axum/src/test_helpers/test_client.rs#L32`),它是个用 `reqwest` 包的真客户端,API 更顺手(`client.get("/").body(...).send().await`)。但**它不是稳定公开 API**——源码 `axum/src/lib.rs#L503-L505` 明确:

```rust
// axum/src/lib.rs#L503-L505(逐字摘录)
#[cfg(any(test, feature = "__private"))]
#[allow(missing_docs, missing_debug_implementations, clippy::print_stdout)]
pub mod test_helpers;
```

`test_helpers` 由 `cfg(any(test, feature = "__private"))` 守护——意味着只有在 axum 自己的测试里、或者你显式启用 `__private` feature(这是个**内部 feature**,名字就告诉你它是私有的,随时可能改)时才可用。**生产代码不要依赖 `axum::test_helpers::TestClient`**——它没有稳定性保证,API 可能随版本变。

那生产代码测试用什么?用本节套路一(`tower::ServiceExt::oneshot`)或套路三(`hyper-util` client 起真服务器)。`examples/testing` 的官方推荐就是套路一+套路三,没有用 `TestClient`——`examples/testing/src/main.rs` 里全是 `app.oneshot(...)` 和真服务器套路。这是诚实标注:本书不教你用不稳定的 `TestClient`,教你用稳定可生产的套路。

> **钉死这件事**:测试四套路——① `oneshot` 单请求(最常用,承 P1-03 的 Router impl Service)、② `into_service` + `ready`/`call` 多请求、③ 起真服务器用 hyper-util client(测真协议机)、④ `MockConnectInfo` 模拟对端地址(测带 ConnectInfo 的 handler)。`axum::test_helpers::TestClient` 在 `cfg(any(test, feature = "__private"))` 下,**非稳定公开 API,生产不要依赖**。所有套路真实示例对齐 `examples/testing`(0.8.9)。

---

## 第六节 · 从 actix-web / rocket 迁移

从其他 Rust Web 框架迁过来,API 怎么对应?本节给两张对照表(actix-web、rocket),帮你快速翻译。注意:actix-web 和 rocket 自己也在演进,本表基于 2026 年 7 月的版本(actix-web 4.x、rocket 0.5.x),具体细节以它们的官方文档为准。

### 6.1 actix-web → axum 对照表

actix-web 4.x 和 axum 0.8.9 的 API 对照:

| 维度 | actix-web 4.x | axum 0.8.9 | 迁移注意 |
|------|---------------|------------|---------|
| JSON 提取器 | `web::Json<T>` | `Json<T>`(`axum::Json`) | 签名一致,改名即可 |
| 共享状态 | `web::Data<T>` | `State<T>`(`axum::extract::State`) | actix 用 `Arc` 包,axum 直接 T(要 `Clone`) |
| 路径参数 | `web::Path<T>` | `Path<T>`(`axum::extract::Path`) | 签名一致 |
| Query 参数 | `web::Query<T>` | `Query<T>` | 签名一致 |
| Form | `web::Form<T>` | `Form<T>` | 签名一致 |
| handler 注册 | `App::new().service(scope::resource("/users").route(get().to(handler)))` | `Router::new().route("/users", get(handler))` | axum 更简洁,不用 `service`/`resource`/`scope` 三层 |
| 路径参数语法 | `/{id}` | `{id}` | ★ 0.8 用 `{id}`,actix 用 `{id}` 一样(0.7 是 `:id`) |
| 中间件 | `wrap(middleware)` | `.layer(L)` 或 `.route_layer(L)` | ★ axum 的 `layer` 含 fallback,`route_layer` 不含(承 P2-08) |
| 中间件写法 | `impl Transform<S, ServiceRequest>` + `Future` | `from_fn` 闭包 或 手写 Layer | axum 的 `from_fn` 远比 actix 的手写 Transform 简洁(承 P4-14) |
| 状态注入 | `App::new().app_data(Data::new(state))` | `Router::new().with_state(state)` | axum 用泛型把"缺状态"编码进类型(承 P1-04) |
| 错误响应 | `error::Error*` 系列 + `ResponseError` trait | 自定义 `AppError` + `IntoResponse` | axum 更自由(承 P3-12、第一节 1.4) |
| 运行时 | actix 自己的运行时(基于 tokio) | tokio | axum 全 tokio,actix 4 也基于 tokio 但有自己的抽象层 |
| WebSocket | `actix_web_actors::ws` | `axum::extract::ws::WebSocket` | axum 用 hyper 协议升级(承 P5-19) |
| actor 模型 | 有(actix 框架基于 actor) | **无** | axum 不用 actor,纯 async fn handler |

迁移的几个关键点:

1. **handler 签名几乎一样**:`async fn(State<T>, Path<i32>) -> impl IntoResponse` 在两边都能跑(改名)。actix 的 `web::Data<T>` 换成 `axum::extract::State<T>`。
2. **中间件写法差异大**:actix 要手写 `Transform` + `Service` + `Future` 三件套,axum 用 `from_fn` 一个闭包搞定。承 P4-14。
3. **状态管理哲学不同**:actix 用 `app_data` 运行期注入(任意类型),axum 用泛型 `Router<S>` 编译期保证有状态(承 P1-04)。axum 的方式更类型安全(忘了 `with_state` 编译就过不了),但要适应泛型。
4. **路径参数 0.8 用 `{id}`**:actix 也是 `{id}`,0.7 的 `:id` 是 axum 老语法(P6-20)。

### 6.2 rocket → axum 对照表

rocket 0.5.x 和 axum 0.8.9 的 API 对照:

| 维度 | rocket 0.5.x | axum 0.8.9 | 迁移注意 |
|------|--------------|------------|---------|
| JSON 提取器 | `Json<T>`(`rocket::serde::json::Json`) | `Json<T>` | 签名一致 |
| 共享状态 | `&State<T>`(rocket 的 State) | `State<T>`(`axum::extract::State`) | rocket 用引用,axum 用提取器 |
| 路径参数 | `<id>` in route + 函数参数 | `{id}` in route + `Path<T>` 提取器 | ★ rocket 把路径参数当函数参数,axum 用 `Path` 提取器 |
| Query 参数 | `<param>` in route + 函数参数 | `Query<T>` 提取器 | rocket 直接函数参数,axum 用 `Query` |
| request guard | `FromRequest` trait(rocket 的招牌) | `FromRequestParts` / `FromRequest`(axum) | 名字像但语义不同(承 P3-10) |
| handler 注册 | `#[get("/users/<id>")]` 宏 | `Router::new().route("/users/{id}", get(handler))` | rocket 用过程宏,axum 用显式 Router |
| 路由宏 | `#[get("/")]`/`#[post("/", data="<form>")]` | `get`/`post` 函数(不是宏) | rocket 是"宏驱动",axum 是"显式 API" |
| 中间件 | `Fairing` trait | `.layer(L)` / `from_fn` | rocket 的 Fairing 是事件驱动,on_request/on_response;axum 是 Tower Layer 洋葱 |
| 错误响应 | `Responder` trait + `Catcher` | `IntoResponse` + 自定义 `AppError` | axum 的 `IntoResponse` 和 rocket 的 `Responder` 概念等价 |
| 状态注入 | `Rocket::manage(state)` | `Router::with_state(state)` | rocket 运行期注入,axum 编译期泛型(承 P1-04) |
| 运行时 | tokio(0.5 起) | tokio | 都基于 tokio |
| WebSocket | `rocket::ws::Message` | `axum::extract::ws::WebSocket` | 都基于 tokio-tungstenite |

迁移的几个关键点:

1. **路由风格差异大**:rocket 用过程宏(`#[get("/users/<id>")]`),axum 用显式 `Router::route("/users/{id}", get(handler))`。rocket 的宏把路径参数当函数参数(`async fn handler(id: i32)`),axum 要用 `Path` 提取器(`async fn handler(Path(id): Path<i32>)`)。
2. **request guard vs 提取器**:rocket 的 `FromRequest` 和 axum 的 `FromRequestParts`/`FromRequest` 名字像,但 axum 有**二元划分**(只读 parts vs 消费 body,承 P3-10),rocket 没有。axum 的提取器顺序有硬约束(消费 body 的最后),rocket 没有。
3. **中间件哲学不同**:rocket 的 `Fairing` 是事件驱动(`on_request`/`on_response` 两个回调),axum 是 Tower Layer 洋葱(一个 Service 套一个,承 P4-14/P4-16)。洋葱模型更灵活(能短路、能改 request 和 response),Fairing 更简单但能力弱。
4. **状态管理**:rocket 用 `Rocket::manage(state)` 运行期注入任意类型,axum 用 `Router<S>` 泛型编译期保证(承 P1-04)。

### 6.3 迁移的通用建议

不管从哪个框架迁,几个通用建议:

1. **handler 签名最容易迁**(改名而已):JSON/Path/Query/Form 这些提取器在三个框架签名几乎一样。
2. **中间件写法差异最大**:actix 的 Transform、rocket 的 Fairing、axum 的 `from_fn` —— 完全重写,但 axum 的 `from_fn` 最简洁。
3. **状态管理要适应**:axum 用泛型把"缺状态"编码进类型(承 P1-04),这是它最独特的点,迁过来要理解 `Router<S>` → `Router<()>` 的 `with_state` 收尾。
4. **路径参数 0.8 用 `{id}`**:不是 `:id`(那是 0.7,承 P6-20)。
5. **错误处理用 `IntoResponse`**:axum 没有 actix 的 `ResponseError` 或 rocket 的 `Responder` 那样的"框架提供错误类型",要自定义 `AppError` + `IntoResponse`(承第一节 1.4、P3-12、P5-18)。
6. **测试用 `oneshot`**:别想着起 actix/rocket 那种测试服务器,axum 的 `Router::oneshot` 直接测更轻量(承第五节)。

> **钉死这件事**:迁移对照表是"翻译字典",不是"等价证明"。三个框架(handler 签名/提取器/状态/中间件/错误处理)各有哲学,翻译过去要理解 axum 的独特点(泛型 State、FromRequest 二元划分、from_fn 闭包、Tower Layer 洋葱)。承 P7-21 的全栈对照。

---

## 第七节 · ★ 线上问题排查清单(本附录精华)

这是本附录的精华——八条真实生产事故的排查清单,每条给"现象 → 根因(回扣正文机制)→ 解法 → 指路本书哪章"。这一节是"凌晨三点收到报警时,照着清单从症状追到根因"的手册。

> **格式说明**:每条排查项用表格列"现象 / 根因 / 解法 / 对应章节"。根因必须回扣正文讲透的机制——理解了原理才能排查,这是本附录的设计哲学。前面有总览的排查决策树,后面是逐条详拆。

### 7.0 排查决策树(总览)

收到报警先按这张决策树定位:

```text
                        ┌─ handler 编译报
                        │  "Handler not implemented"?
                        │  → 排查项一(用 #[axum::debug_handler])
                        │
                        ├─ 程序跑起来但
                        │  handler 收不到 State?
                        │  → 排查项二(Router<S> 没 with_state)
                        │
                        ├─ 中间件行为反了
                        │  (CORS/压缩/超时 顺序不对)?
            报警 →     │  → 排查项三(洋葱顺序)
                        │
                        ├─ handler 报"request body has been taken"?
                        │  → 排查项四(body 重复消费)
                        │
                        ├─ 404 走了鉴权?
                        │  → 排查项五(layer vs route_layer)
                        │
                        ├─ 中间件编译报
                        │  "expected Infallible, found BoxError"?
                        │  → 排查项六(漏 HandleErrorLayer)
                        │
                        ├─ graceful shutdown 时
                        │  请求被砍 / 没等到?
                        │  → 排查项七(Timeout 配合)
                        │
                        └─ panic 直接崩服务?
                           → 排查项八(无 catch_panic)
```

### 7.1 排查项一:handler 编译报 "Handler not implemented"

**症状速记**:`Router::new().route("/", get(my_handler))` 编译报 `Handler not implemented for ...`,几十行类型约束错误,看不懂哪条没满足。

| 维度 | 内容 |
|---|---|
| **现象** | 写了一个 `async fn my_handler(...) -> ...`,挂到 `Router::route("/", get(my_handler))`,编译报 `the trait Handler<_, _> is not implemented`,后面跟一长串 `where` 约束(`T: FromRequestParts<S>`, `T: FromRequest<S>`, `S: Clone + Send + Sync + 'static` 等等),看不出哪条没满足。 |
| **根因** | handler 的某个参数没满足 `Handler` trait 的约束。承 P3-09 的 `impl_handler!` 宏:对 0~16 参数的 `async fn`,要求"前 N-1 个参数 `FromRequestParts<S> + Send`,最后一个参数 `FromRequest<S> + Send`,返回值 `IntoResponse + 'static`,所有参数 `Send + 'static`,state 类型 `S: Clone + Send + Sync + 'static`"。常见违规:① 把消费 body 的提取器(如 `Json<T>`)放在非最后位置(承 P3-10);② handler 用了自定义提取器但没实现 `FromRequestParts`(承 P3-13);③ handler 返回类型没实现 `IntoResponse`(承 P3-12);④ handler 是 `fn` 不是 `async fn`(承 P3-09);⑤ handler 不是 `Send`(里面用了 `!Send` 类型);⑥ 提取器 `!Send`。 |
| **解法** | ★ **用 `#[axum::debug_handler]` 宏定位**——把它加到 handler 上(`#[axum::debug_handler] async fn my_handler(...) {}`),它会把 handler 包成命名函数,把模糊的 `Handler not implemented` 换成具体哪个参数/返回值没满足哪个约束。承 P3-13。常见修法:① 把消费 body 的提取器移到最后一个参数;② 给自定义提取器实现 `FromRequestParts`;③ handler 加 `async`;④ 确保所有参数 `Send`。 |
| **对应章节** | P3-09(Handler trait 宏展开)、P3-10(FromRequest/Parts 二元)、P3-13(`#[axum::debug_handler]` 宏) |

### 7.2 排查项二:State 类型不匹配 / Router 没法 serve

**症状速记**:`axum::serve(listener, app)` 编译报类型错,或 handler 提不到 State。

| 维度 | 内容 |
|---|---|
| **现象** | 两种症状:① `axum::serve(listener, app)` 编译报"expected `Router<()>`, found `Router<MyState>`"——`Router<MyState>` 没法 serve;② handler 用 `State<MyState>` 提取,但运行时拿到的是默认值/panic/提取不到。 |
| **根因** | `Router<S>` 的 `S` 是"缺失的状态类型"(承 P1-04 招牌章)——`Router<MyState>` 表示"我还需要 `MyState` 才能服务"。`axum::serve` 只接受 `Router<()>`(无缺失状态)。`with_state(state: S)` 是消耗 `Router<S>` 变 `Router<()>` 的动作。如果你忘了 `.with_state(...)`,`Router<MyState>` 没变 `Router<()>`,serve 编译不过。如果 handler 用了错误的 State 类型(如 `State<AppState>` 但 Router 是 `Router<OtherState>`),提取器提取的类型对不上,运行时拿不到正确状态。承 P1-04。 |
| **解法** | ① 在 `axum::serve` 前加 `.with_state(state)`:`Router::new().route(...).with_state(my_state)`,把 `Router<MyState>` 变 `Router<()>`;② 确认 handler 的 `State<T>` 的 `T` 和 `with_state` 传的类型一致;③ 如果有子状态(如 handler 要 `State<DbPool>` 但顶层是 `AppState`),实现 `FromRef<AppState> for DbPool`(承 P1-04 的 `FromRef`)。 |
| **对应章节** | P1-04(State 泛型编码 + FromRef)、P3-11(State 提取器) |

### 7.3 排查项三:中间件顺序错了

**症状速记**:CORS 预检被鉴权挡掉、压缩后 trace 的 size 不对、Timeout 把 CORS 也超时了。

| 维度 | 内容 |
|---|---|
| **现象** | 配了多个中间件,行为反了:① CORS 预检(OPTIONS)被鉴权挡掉,浏览器拿不到 CORS 响应;② TraceLayer 记的 response size 是压缩后的(看不出原始 size)或压缩前的(看不出实际传输);③ Timeout 把整个请求(含 CORS、鉴权)都超时了,但你只想给 handler 超时;④ 鉴权失败但日志没记(TraceLayer 在鉴权内)。 |
| **根因** | 中间件顺序错。承 P4-16 招牌章:`ServiceBuilder` 链式"先 `.layer()` 的在最外层,请求先穿"。所以 `.layer(A).layer(B).layer(C)` 请求进来先穿 A → B → C → handler。常见错:CORS 在鉴权内(应该在外)、Compression 在 Trace 外(应该在内)、Timeout 在最外(可能你想在中间)。 |
| **解法** | 记住顺序规则(承第二节 2.4):`TraceLayer`(最外,记全部)→ CORS(预检要在鉴权前)→ Timeout(总超时)→ 鉴权(只这组路由)→ Compression(最内,改 response)。用 `ServiceBuilder::new().layer(Trace).layer(CORS).layer(Timeout).layer(auth).layer(Compression)` 这个顺序写。改顺序就改行为:`.timeout(10s).layer(auth)` 是"总超时含鉴权";`.layer(auth).timeout(10s)` 是"鉴权后才超时"。 |
| **对应章节** | P4-14(from_fn 洋葱)、P4-16(ServiceBuilder 顺序招牌对照) |

### 7.4 排查项四:body 重复消费

**症状速记**:handler 报 "request body has been taken out of request" 或第二个消费 body 的提取器拿不到数据。

| 维度 | 内容 |
|---|---|
| **现象** | handler 有两个提取器都想消费 body(如 `String` + `Json<T>`、或 `bytes::Bytes` + `Form<T>`),第二个提取器报错 "request body has been taken out of request" 或拿不到数据 / 拿到空 body。或者中间件用 `Request` 提取器读了 body,handler 再用 `Json` 提取器提不到。 |
| **根因** | body 只能消费一次。承 P3-10 的二元划分:`FromRequest`(消费 body)只能跑一次,`FromRequestParts`(只读 parts)可多次跑。`impl_handler!` 宏约束"最后一个参数 `FromRequest`,其余 `FromRequestParts`"——所以你写不出"两个消费 body 的提取器"在 handler 参数里(编译就过不了)。但中间件可能提前消费 body:像 `from_extractor` 用了消费 body 的提取器(如 `String`),body 被消费后"留一个空 body"(源码注释明确,承第二节 2.2),handler 的 `Json` 提取器提空 body 失败。或者两个中间件都读 body。承 P3-10。 |
| **解法** | ① handler 不要写两个消费 body 的提取器(编译会过不了,但要知道为什么);② 中间件读 body 后,要么把解析结果塞进 `Request::extensions()` 让 handler 用 `Extension<T>` 取(承 P3-11),要么用 `from_extractor` 时注意它会留空 body(源码注释,承第二节 2.2);③ 如果中间件和 handler 都要 body,中间件读完后重新塞一个 body 回去(`req.map(|body| Body::new(re_encoded_body))`),但这是高级用法;④ 用 `axum::body::Body` 的 `collect` 一次性读到 `Bytes`,塞进 extensions,handler 取出来反序列化。 |
| **对应章节** | P3-10(FromRequest/Parts 二元划分)、P3-11(提取器实战)、P4-15(from_extractor 留空 body) |

### 7.5 排查项五:404 走了鉴权

**症状速记**:未鉴权的请求打不存在的路径,期望 404,实际吃 401。

| 维度 | 内容 |
|---|---|
| **现象** | 配了鉴权中间件,期望"鉴权失败的请求打存在路径 → 401,打不存在路径 → 404"。实际:未鉴权的请求打不存在路径,先吃 401(因为 fallback 也走了鉴权),不是 404。或者反过来,期望所有请求(含 404)都走鉴权,实际 404 不走。 |
| **根因** | `Router::layer` vs `Router::route_layer` 的作用域差别。承 P2-08 招牌章:`layer` 影响所有(含 fallback),`route_layer` 只影响"已注册的路由"不含 fallback。鉴权用 `layer` → fallback(404)也走鉴权 → 未鉴权打不存在路径吃 401 而不是 404。源码(`axum/src/routing/mod.rs#L302-L335`)明确:`layer` 给 `path_router`/`fallback_router`/`catch_all_fallback` 全套,`route_layer` 只给 `path_router`。承 P2-08。 |
| **解法** | ① 鉴权中间件用 `.route_layer(...)` 而不是 `.layer(...)`,这样未鉴权的请求打不存在路径直接 404(fallback 不走鉴权),打存在路径但鉴权失败才 401;② 反过来,如果你想"所有请求都走鉴权"(含 404),用 `.layer(...)`。多数场景是前者(404 不该走鉴权),所以鉴权默认用 `route_layer`。承 P2-08。 |
| **对应章节** | P2-08(fallback 与 404,route_layer 作用域)、P4-14(from_fn)、P4-16(四种 layer 方法) |

### 7.6 排查项六:中间件编译报 "expected Infallible, found BoxError"

**症状速记**:套了 `tower::timeout::TimeoutLayer` 或 `tower::limit::ConcurrencyLimitLayer` 后编译报 Infallible 类型不匹配。

| 维度 | 内容 |
|---|---|
| **现象** | 给 Router 套 `tower::timeout::TimeoutLayer::new(...)` 或 `tower::limit::ConcurrencyLimitLayer::new(...)` 或 `tower::buffer::BufferLayer::new(...)`,编译报"expected `Infallible`, found `Box<dyn Error + Send + Sync>`"或类似——tower 中间件的 `Error` 是 `BoxError`,但 axum Router 要求 `Error = Infallible`。 |
| **根因** | axum 框架层的"Infallible 错误模型"(承 P5-18):Router/Route 的 `Service::Error` 永远是 `Infallible`(框架层把所有错误转成 Response)。但 tower 自带的 `Timeout`/`ConcurrencyLimit`/`RateLimit`/`Buffer` 这些中间件的 `Error` 是 `BoxError`(可能返回 `Elapsed`/`Permit` 错误)。直接套到 Router 上类型对不上——Router 期望内层 Service 的 Error 也是 Infallible(这样整条链都 Infallible),但 tower 中间件不是。注意:**`tower-http` 的 Layer 都是 `Error = Infallible`**(承第三节 3.8),所以 CORS/Compression/Trace/TimeoutLayer(tower-http 版)/RequestID 都不会有这个问题。 |
| **解法** | ★ **套 `HandleErrorLayer`**——它是个桥接,把非 Infallible 的错误兜底,调你给的闭包转 Response(承第二节 2.5、P5-18)。写法:`ServiceBuilder::new().layer(HandleErrorLayer::new(|err: BoxError| async move { ... })).timeout(...).concurrency_limit(...)`。`HandleErrorLayer` 必须在这些 tower 中间件**外层**(先 layer,承 P4-16 顺序),这样它包住整个会出错的链。`tower-http` 的 Layer 不用套(它们 Infallible)。`examples/todos` 就是这么写的(承第一节 1.5)。 |
| **对应章节** | P5-18(Infallible 错误模型 + HandleErrorLayer)、第二节 2.5、第三节 3.8 |

### 7.7 排查项七:graceful shutdown 时请求被砍 / 没等到

**症状速记**:收到 SIGTERM 后,在途请求被立即砍(没跑完),或服务不退出(在途请求 hang 住)。

| 维度 | 内容 |
|---|---|
| **现象** | 用 `axum::serve(listener, app).with_graceful_shutdown(shutdown_signal())` 跑服务,收到 shutdown 信号后:① 在途请求被立即砍(用户看到连接重置),没等它跑完;② 或者反过来,某个请求 hang 住(如 `std::future::pending`),graceful shutdown 永远等不到,服务不退出。 |
| **根因** | `with_graceful_shutdown` 的行为(承 P5-17、第四节 4.3):收到信号后① **停止 accept 新连接**(`tokio::select! { conn = listener.accept() => ..., _ = signal_tx.closed() => break }`,源码 `serve/mod.rs#L289-L298`);② **对每个在途连接调 `conn.graceful_shutdown()`**(hyper-util 的 API,告诉协议机"处理完当前请求就关",源码 `serve/mod.rs#L419`);③ **等所有连接 task 退出**(`close_tx.closed().await`,源码 `serve/mod.rs#L304`)。问题在于:① 如果在途请求本身很慢(没超时),`graceful_shutdown` 会一直等,服务不退出;② 如果某个请求是 `std::future::pending`(永不完成),graceful shutdown 永远等不到。 |
| **解法** | ★ **配合 `TimeoutLayer` 给每个请求设上限**——这样 graceful shutdown 时,在途请求最多再跑 Timeout 时长就被砍,服务能在有限时间内退出。`examples/graceful-shutdown` 就是这么做的(`TimeoutLayer::with_status_code(REQUEST_TIMEOUT, 10s)`,承第三节 3.4)。同时 shutdown_signal 要正确监听 ctrl_c 和 SIGTERM(承 `examples/graceful-shutdown` 的 `shutdown_signal` 函数)。如果某些请求就是慢(如大文件上传),要么调大 Timeout,要么接受"这些请求被砍"。承 P5-17。 |
| **对应章节** | P5-17(serve + graceful shutdown)、第四节 4.3、第三节 3.4(TimeoutLayer) |

### 7.8 排查项八:handler panic 直接崩服务

**症状速记**:handler 里 `unwrap()` 一个 `None`,整个连接 task panic,服务可能崩或日志里看到 panic。

| 维度 | 内容 |
|---|---|
| **现象** | handler 里某个 `Option`/`Result` 用 `unwrap()`/`expect()` 失败 panic,这个请求的连接 task panic。症状:① 这个请求的客户端看到连接被重置(没 Response);② 日志里看到 panic backtrace;③ 严重时(如果 panic 在某些位置)可能影响其他连接(但 tokio 的 task 隔离通常让单个 task panic 不影响别的)。 |
| **根因** | axum 默认不捕获 handler panic。panic 沿着 `Future::poll` 冒泡,task 退出(ttokio 的 task 隔离让单个 task panic 不影响 runtime),但这个请求的客户端拿不到 Response(连接被重置)。承 P5-18:Router 的 Service Error 是 `Infallible`,但 panic 不是 Error(panic 和 Result 是两条路径),`Infallible` 管不到 panic。 |
| **解法** | ★ **套 `tower_http::catch_panic::CatchPanicLayer`**——它捕获 handler panic,转成 500 Response(可配置)。`tower-http` 提供(承第三节)。写法:`.layer(tower_http::catch_panic::CatchPanicLayer::custom(|panic| (StatusCode::INTERNAL_SERVER_ERROR, format!("panic: {panic}")).into_response()))`。或者更根本的:**handler 里不要 `unwrap()`**——用 `?` 或 match 处理 `Option`/`Result`,把 panic 变成 `AppError`(承第一节 1.4)。承 P5-18。 |
| **对应章节** | P5-18(panic 处理)、第三节(tower-http catch_panic) |

### 7.9 八条排查清单汇总表

把八条排查项汇总成速查表,排查时从"现象"列快速定位:

| # | 症状速记 | 根因(一句话) | 对应章节 |
|---|---|---|---|
| 1 | handler 编译报 "Handler not implemented" | 提取器顺序错/自定义提取器没 impl/返回值没 IntoResponse | P3-09/10/13 |
| 2 | Router 没法 serve / State 提不到 | `Router<S>` 没 `with_state` 变 `Router<()>` | P1-04 |
| 3 | 中间件顺序错(CORS/压缩/超时反了) | `ServiceBuilder` 先 layer 的在最外,顺序反了语义全变 | P4-14/16 |
| 4 | body 重复消费 / 第二个提取器拿空 body | FromRequest 只能一次,中间件消费后留空 body | P3-10/11/15 |
| 5 | 404 走了鉴权 | `layer`(含 fallback)vs `route_layer`(不含),鉴权该用后者 | P2-08 |
| 6 | 中间件编译报 Infallible 不匹配 | tower 中间件 Error 是 BoxError,要套 HandleErrorLayer | P5-18 |
| 7 | graceful shutdown 砍请求 / 不退出 | 没配 Timeout,在途请求 hang 住 graceful shutdown 等不到 | P5-17 |
| 8 | handler panic 崩连接 | axum 默认不捕 panic,套 CatchPanicLayer 或用 ? 代替 unwrap | P5-18 |

> **排查清单的设计哲学**:这八条根因每条都回扣正文讲透的机制——你只有理解了 `impl_handler!` 宏的 where 约束(P3-09)、`Router<S>` 的 S 是"缺失状态"(P1-04)、`ServiceBuilder` 的 apply 顺序(P4-16)、`FromRequest` 一次性消费(P3-10)、`route_layer` 不含 fallback(P2-08)、`Infallible` 错误模型(P5-18)、graceful shutdown 的两阶段(P5-17),才能在凌晨三点照着症状追到根因。这正是本书从头到尾强调的"理解了原理才能排查"——axum 不是黑盒,每个线上问题都能回到源码机制。

---

## 第八节 · 配图与实践流程

### 8.1 实践流程图:从 hello-world 到上线

把第一到第五节的实践串成一张流程图:

```text
┌─────────────────────────────────────────────────────────────────┐
│  写一个 axum 服务的步骤                                          │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ① hello-world 起步(第一节 1.1)                                │
│     Router::new().route("/", get(handler))                      │
│     + TcpListener::bind + axum::serve                           │
│              │                                                  │
│              ▼                                                  │
│  ② 加路由/提取器/State(第一节 1.2)                              │
│     Path/Query/State 提取器 + with_state                        │
│     ★ 提取器顺序:FromRequestParts 在前,FromRequest(消费body)在后│
│              │                                                  │
│              ▼                                                  │
│  ③ 加 Json 提取器和 IntoResponse 返回值(第一节 1.3)              │
│     Json<T> 提 body + Json<User> 返回                           │
│              │                                                  │
│              ▼                                                  │
│  ④ 加错误处理(第一节 1.4)                                       │
│     自定义 AppError + impl IntoResponse + From<X>               │
│     handler 返回 Result<T, AppError>                            │
│              │                                                  │
│              ▼                                                  │
│  ⑤ 加中间件(第二节)                                            │
│     from_fn(鉴权/日志) / from_extractor(校验)                 │
│     ServiceBuilder 叠 tower-http(CORS/压缩/超时/trace)         │
│     ★ 顺序:Trace→CORS→Timeout→鉴权→Compression                │
│     ★ tower 中间件要套 HandleErrorLayer                         │
│              │                                                  │
│              ▼                                                  │
│  ⑥ 测试(第五节)                                                │
│     oneshot 单请求 / into_service 多请求                        │
│     / 真服务器 / MockConnectInfo                                │
│              │                                                  │
│              ▼                                                  │
│  ⑦ 上线(可选:与 hyper 直接集成,第四节)                       │
│     axum::serve 或 serve-with-hyper(要 TLS/低层控制时)          │
│     + graceful shutdown + Timeout 兜底                          │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### 8.2 排查决策树(详版)

把第七节 7.0 的决策树展开,带解法:

```text
                        ┌─ handler 编译报
                        │  "Handler not implemented"?
                        │   ├─ 消费 body 提取器没在最后? → 移到最后(承 P3-10)
                        │   ├─ 自定义提取器没 impl?     → impl FromRequestParts(承 P3-13)
                        │   ├─ 返回值没 impl IntoResponse? → 检查/加 impl(承 P3-12)
                        │   └─ 用 #[axum::debug_handler] 定位(承 P3-13)
                        │
                        ├─ handler 收不到 State?
                        │   ├─ Router<S> 没 with_state?  → .with_state(state)(承 P1-04)
                        │   ├─ State<T> 类型对不上?     → 改 with_state 类型或用 FromRef
                        │   └─ 子状态?                  → impl FromRef(承 P1-04)
                        │
                        ├─ 中间件顺序反了?
            报警 →     │   ├─ CORS 预检被鉴权挡?       → CORS 移到鉴权外
                        │   ├─ Trace size 不对?         → Compression 移到 Trace 内
                        │   ├─ Timeout 范围不对?        → 调整 Timeout 内外位置
                        │   └─ 记住:ServiceBuilder 先 layer 在最外(承 P4-16)
                        │
                        ├─ body 重复消费?
                        │   ├─ handler 两个消费 body 提取器? → 编译应报错,改设计
                        │   ├─ 中间件消费了 body?      → 塞 extensions 或重塞 body
                        │   └─ from_extractor 留空 body?→ 换 from_fn(承 P4-15)
                        │
                        ├─ 404 走了鉴权?
                        │   └─ layer→route_layer(承 P2-08)
                        │
                        ├─ 编译报 Infallible 不匹配?
                        │   └─ tower 中间件套 HandleErrorLayer(承 P5-18)
                        │      ★ tower-http 的 Layer 不用套(Infallible)
                        │
                        ├─ graceful shutdown 不退出?
                        │   └─ 配 TimeoutLayer 兜底(承 P5-17)
                        │
                        └─ handler panic 崩连接?
                            ├─ 套 CatchPanicLayer(tower-http)
                            └─ handler 内用 ? 代替 unwrap(承 P5-18)
```

### 8.3 迁移对照表(汇总)

把第六节两张表的核心汇总:

```text
┌──────────────┬────────────────────┬────────────────────┬────────────────────┐
│ 维度         │ actix-web 4.x      │ rocket 0.5.x       │ axum 0.8.9         │
├──────────────┼────────────────────┼────────────────────┼────────────────────┤
│ JSON 提取器  │ web::Json<T>       │ Json<T>            │ Json<T>            │
│ 共享状态     │ web::Data<T>(Arc)  │ &State<T>          │ State<T>           │
│ 路径参数语法 │ {id}               │ <id> in macro      │ {id}               │
│ 路径参数提取 │ web::Path<T>       │ 函数参数           │ Path<T> 提取器     │
│ handler 注册 │ service/resource/  │ #[get("/")]宏      │ Router::route      │
│              │ scope 三层         │                    │                    │
│ 中间件       │ wrap(Transform)    │ Fairing(事件)     │ layer/from_fn(洋葱)│
│ 中间件作用域 │ wrap 影响范围      │ Fairing 全局       │ layer/route_layer  │
│              │                    │                    │ (含/不含 fallback) │
│ 状态注入     │ app_data(运行期)  │ manage(运行期)    │ with_state(编译期) │
│ 错误响应     │ ResponseError      │ Responder          │ IntoResponse       │
│ 运行时       │ actix runtime      │ tokio              │ tokio              │
│ actor 模型   │ 有                 │ 无                 │ 无                 │
│ WebSocket    │ actix_web_actors   │ rocket::ws         │ axum::extract::ws  │
└──────────────┴────────────────────┴────────────────────┴────────────────────┘
```

---

## 附录 B 小结

本附录把全书 21 章的理论落到了"怎么真用"——一个完整的 RESTful 服务骨架(基于 `examples/todos`)、三种中间件写法(`from_fn`/`from_extractor`/`ServiceBuilder`)、tower-http 集成(CORS/压缩/限流/超时/trace/请求 ID)、与 hyper 直接集成(`serve-with-hyper`)、四种测试套路(oneshot/`into_service`/真服务器/`MockConnectInfo`)、从 actix-web/rocket 迁移的对照表、八条线上排查清单。

把这些拼起来,你会看到一个清晰的图景:**axum 不是某个孤立的 Web 框架,它是 Tokio 异步生态请求处理的"Web 框架语言"**。底层全是 hyper+Tower+Tokio 的 Service 链——axum 只在它上面加了路由+提取器+响应器+Handler trait 宏展开这一层(承 P0-01)。你写 handler,axum 用宏把它编译期变成 Tower Service;你写中间件,axum 把闭包变成 Tower Layer;你套 `tower-http`,这些现成 Layer 直接拼上去(因为都是 Tower Service);你测 Router,`oneshot` 直接当 Tower Service 测;你与 hyper 集成,`service_fn`/`TowerToHyperService` 桥接 Tower↔hyper。整套生态共用 `Service × Layer` 这套通用语言(承《Tower》)。

你排查的每一个线上问题,根因都能回到正文讲透的机制——`impl_handler!` 宏的 where 约束(P3-09)、`Router<S>` 的 S 是"缺失状态"(P1-04)、`ServiceBuilder` 的 apply 顺序(P4-16)、`FromRequest` 一次性消费(P3-10)、`route_layer` 不含 fallback(P2-08)、`Infallible` 错误模型(P5-18)、graceful shutdown 两阶段(P5-17)。这正是本书从头到尾强调的"理解了原理才能排查"——axum 不是黑盒。

> **回扣全书**:本附录是全书的实践收束。前 21 章拆透了机制(P0-01 第一性原理 / P1-02~04 框架地基 / P2-05~08 路由 / P3-09~13 提取响应 / P4-14~16 中间件 / P5-17~19 服务高级 / P6-20 演进 / P7-21 收束),本附录告诉你怎么把它们拼成一个真实可用的系统。配合附录 A(源码全景路线图),你该能在脑子里放映出:从"写一个 handler"到"凌晨三点排查线上问题"的完整旅程——以及每一步 hyper/Tower/Tokio 怎么被用、对照 actix/rocket/go 怎么做。

---

## 附录 B 源码引用与诚实标注

> **本附录引用的 axum 源码**(本地 `../axum/`,版本 `axum-v0.8.9 @ c59208c8`,已核实):
>
> - [axum::serve / Serve / WithGracefulShutdown / handle_connection(`TowerToHyperService` 桥接 + `signal_tx.closed()` 优雅关)](../axum/axum/src/serve/mod.rs) —— 第四节与 hyper 集成、排查项七
> - [HandleErrorLayer / HandleError(`oneshot` 兜底非 Infallible 错误)](../axum/axum/src/error_handling/mod.rs#L115-L149) —— 第二节 2.5、排查项六
> - [from_fn / FromFnLayer / FromFn / Next(闭包变 Layer)](../axum/axum/src/middleware/from_fn.rs) —— 第二节 2.1、承 P4-14
> - [from_extractor / FromExtractor(只跑 FromRequestParts,失败 rejection 短路)](../axum/axum/src/middleware/from_extractor.rs) —— 第二节 2.2、承 P4-15
> - [ConnectInfo / MockConnectInfo(test_helpers 提供假对端地址)](../axum/axum/src/extract/connect_info.rs#L150-L232) —— 第五节 5.4、承 P5-17
> - [Router::layer vs Router::route_layer(`mod.rs#L302-L335`,layer 含 fallback,route_layer 不含)](../axum/axum/src/routing/mod.rs) —— 第二节 2.1、排查项五、承 P2-08
> - [Router::into_service / into_make_service / into_make_service_with_connect_info(`mod.rs#L500-L540`)](../axum/axum/src/routing/mod.rs) —— 第五节 5.2、第四节 4.4
> - [test_helpers 模块由 `cfg(any(test, feature = "__private"))` 守护(`lib.rs#L503-L505`),`TestClient` 非稳定公开 API](../axum/axum/src/lib.rs) —— 第五节 5.5 诚实标注
> - [test_helpers::TestClient(内部测试辅助,基于 reqwest)](../axum/axum/src/test_helpers/test_client.rs#L32) —— 第五节 5.5
>
> **真实示例引用**(本地 `../axum/examples/`,共 60+ 示例,本附录引用 20+,全部 `ls` 核实过存在):
>
> - `examples/hello-world`(第一节 1.1 最小骨架)
> - `examples/todos`(第一节 1.5 完整 RESTful CRUD 骨架,带 HandleErrorLayer + Timeout + Trace)
> - `examples/error-handling`(第一节 1.4 自定义 AppError + IntoResponse + `#[from_request]` 宏)
> - `examples/jwt`(第二节 2.3 把鉴权提取器当 handler 参数,自定义 FromRequestParts)
> - `examples/cors`(第三节 3.1 CORS,CorsLayer)
> - `examples/compression`(第三节 3.2 压缩,CompressionLayer + RequestDecompressionLayer)
> - `examples/graceful-shutdown`(第三节 3.4 + 排查项七,tower-http TimeoutLayer + with_graceful_shutdown)
> - `examples/request-id`(第三节 3.6 请求 ID,SetRequestIdLayer + PropagateRequestIdLayer + TraceLayer)
> - `examples/serve-with-hyper`(第四节 与 hyper 直接集成,service_fn 桥接 Tower↔hyper)
> - `examples/testing`(第五节 四种测试套路全部对齐此示例)
> - `examples/versioning`(第五节 自定义 FromRequestParts 提取器测试)
> - 其他被引用的示例:`tls-rustls`/`tls-graceful-shutdown`/`low-level-rustls`(第四节 TLS)、`chat`/`websockets`/`sse`(承 P5-19)、`sqlx-postgres`/`diesel-async-postgres`(数据库集成)、`validator`(校验)、`dependency-injection`(依赖注入)等
>
> **外部 crate(诚实标注,引用公开 API/用法,不编内部行号)**:
>
> - **tower-http**(外部 crate,社区维护,版本 `0.6.x`):`CorsLayer`/`CompressionLayer`/`RequestDecompressionLayer`/`TimeoutLayer`/`TraceLayer`/`SetRequestIdLayer`/`PropagateRequestIdLayer`/`MakeRequestUuid`/`CatchPanicLayer`——全部引用公开 API,作为"现成 Layer"的例子。所有 Layer 的 `Error` 都是 `Infallible`(不用套 HandleErrorLayer)。
> - **hyper / hyper-util**(外部 crate,承《hyper》):`hyper::service::service_fn`、`hyper_util::rt::{TokioExecutor, TokioIo}`、`hyper_util::server::conn::auto::Builder::serve_connection_with_upgrades`、`hyper_util::service::TowerToHyperService`、`hyper_util::client::legacy::Client`——引用公开 API,内部协议机/连接管理承《hyper》P2-P5。
> - **tower**(外部 crate,承《Tower》):`ServiceBuilder`/`ServiceExt::oneshot`/`Service::call`/`ServiceExt::ready`/`timeout::TimeoutLayer`/`limit::ConcurrencyLimitLayer`/`limit::RateLimitLayer`/`BoxError`——引用公开 API,内部机制(Service/Layer/poll_ready/ServiceBuilder/Buffer/限流)承《Tower》全书。
> - **reqwest**(外部 crate):`TestClient` 内部用它,生产测试可用 `reqwest` 直接发请求——引用公开 API。
> - **serde / serde_json**(外部 crate):`Json` 提取器用它反序列化——引用公开 API。
> - **tokio**(外部 crate,承《Tokio》):`TcpListener::bind`/`accept`、`tokio::spawn`、`tokio::select!`、`tokio::signal`、`tokio::sync::watch`——引用公开 API,运行时机制承《Tokio》[[tokio-source-facts]]。
> - **http-body-util**(外部 crate):`BodyExt::collect`(测试聚 body)——引用公开 API。
> - **jsonwebtoken**(外部 crate,`examples/jwt` 用):JWT 编解码——引用公开 API。
>
> **回扣的正文章节**:
>
> - **P1-03**(Router impl Service + Route 类型擦除)—— 第五节测试套路一/二
> - **P1-04**(State 泛型编码 + FromRef)—— 第一节 1.2、排查项二
> - **P2-05**(PathRouter matchit 匹配 + MatchedPath)—— 第一节 1.2、第三节 3.5
> - **P2-06**(MethodRouter 按 method 分发)—— 第一节 1.1、1.5
> - **P2-08**(fallback 与 404,route_layer 作用域)—— 第二节 2.1、排查项五
> - **P3-09**(Handler trait 宏展开)—— 第一节 1.1、排查项一
> - **P3-10**(FromRequest/FromRequestParts 二元划分)—— 第一节 1.2/1.3、第二节 2.2、排查项四
> - **P3-11**(提取器实战 Path/Query/State/Json)—— 第一节 1.2/1.3、第三节 3.7
> - **P3-12**(IntoResponse)—— 第一节 1.3/1.4
> - **P3-13**(自定义提取器 + `#[axum::debug_handler]` 宏)—— 第一节 1.4、第二节 2.3、排查项一
> - **P4-14**(from_fn 把闭包变中间件)—— 第二节 2.1/2.4
> - **P4-15**(from_extractor)—— 第二节 2.2
> - **P4-16**(中间件链与 ServiceBuilder,四种 layer 作用域)—— 第二节 2.4、第三节、排查项三
> - **P5-17**(serve 与监听器,graceful shutdown)—— 第一节 1.1、第四节、排查项七
> - **P5-18**(Infallible 错误模型 + HandleErrorLayer + panic)—— 第一节 1.4、第二节 2.5、排查项六/八
> - **P5-19**(WebSocket/SSE/流式)—— 第四节 4.2(`serve_connection_with_upgrades`)
> - **P6-20**(0.7→0.8 演进,`{id}` 路径参数)—— 第一节 1.2、第六节迁移注意
> - **P7-21**(全书收束,栈定位 + 多对照)—— 第六节迁移对照表
>
> **承接**:
>
> - **承《hyper》[[hyper-source-facts]]**:协议机(HTTP/1 状态机/HTTP/2 via h2)、连接管理(accept/keep-alive)、Service trait 本身(`call(&self) -> Future`,hyper 删 poll_ready 招牌对照)、Body as Stream、buffered IO、`TokioIo` 适配——这些《hyper》拆透,本附录一句带过指路(协议机指 P2-P3、Service 指 P1-02、连接管理指 P4-P5)。第四节与 hyper 集成用到 `TokioIo`/`auto::Builder`/`TowerToHyperService`,都是 hyper-util 的公开 API。
> - **承《Tower》**:Service/Layer/poll_ready/ServiceBuilder/Buffer/ConcurrencyLimit/RateLimit/Timeout/BoxCloneSyncService/oneshot/ready/call/`mem::replace` idiom——这些《Tower》拆透(成网后),本附录一句带过指路。第二节中间件、第三节限流、第五节测试用到 Tower 的公开 API。
> - **承《Tokio》[[tokio-source-facts]]**:运行时(task 调度/AsyncRead/AsyncWrite/timer/signal/watch channel/spawn/select!)——这些《Tokio》拆透,本附录一句带过指路。`TcpListener`/`tokio::spawn`/`tokio::select!`/`tokio::signal`/`tokio::sync::watch` 都是 tokio 的公开 API。
