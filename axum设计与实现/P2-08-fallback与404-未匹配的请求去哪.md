# 第 8 章 · fallback 与 404:未匹配的请求去哪

> **核心问题**:前四章(P2-05~P2-07)你看到 `PathRouter` 用 matchit 字典树匹配 URL,`MethodRouter` 按 HTTP method 分发,nest/merge 把多棵路由树拼起来。但有一个问题一直被刻意按下没讲——**一个请求来了,既不在任何注册路径上、或者路径匹配上了但 method 不对,它去哪?**裸 hyper 你只能自己写一个 `else { 404 }` 的尾巴,可 axum 给了你一套完整的兜底机制:既能给"所有未匹配请求"统一装一个 fallback handler,又能区分"路径完全不匹配"(走 `catch_all_fallback`)和"路径匹配但 method 不匹配"(走 `method_not_allowed_fallback`,默认返回 `405 Method Not Allowed`)。更微妙的是,`Router::layer` 套的中间件会作用到 fallback 上,而 `Router::route_layer` 套的中间件**只作用到匹配上的路由,不碰 fallback**——为什么这两条 API 的作用域不一样?这一章拆透 axum 的"兜底机器":它不是一个简单的 `if let Err`,而是一棵**复用 PathRouter 的、用 const generic 标记的、独立注册的 fallback 路由树**。
>
> **读完本章你会明白**:
>
> 1. 一个未匹配请求在 axum 里到底走哪条路:Router::call_with_state 的三段分发(path_router → fallback_router → catch_all_fallback),以及为什么是**三段**而不是两段(每段解决不同的"未匹配"语义);
> 2. **fallback 复用 PathRouter** 这个设计的妙处:axum 没有为 fallback 单独写一套匹配逻辑,而是用 const generic `IS_FALLBACK=true` 标记一棵**和普通路由树同型的** PathRouter,把 fallback handler 用一个特殊 catch-all 路径 `FALLBACK_PARAM_PATH`(= `/{*__private__axum_fallback}`)注册进去——这样 fallback 也走统一的 matchit 匹配 + Layer 链,逻辑零分叉;
> 3. **catch_all_fallback vs method_not_allowed_fallback** 两层兜底的精确边界:路径完全不匹配 → catch_all_fallback(你 `.fallback(h)` 设的,默认返回 404);路径匹配上了但 MethodRouter 里没装这个 method 的 handler → method_not_allowed_fallback(你 `.method_not_allowed_fallback(h)` 设的,默认返回 405 带 `Allow` header),两者**互不混淆**;
> 4. **Fallback 三态**(Default / Service / BoxedHandler)各存什么、`default_fallback: bool` 标志位为什么是 merge 时检测冲突的关键,以及为什么 `route_layer` 的中间件不作用到 fallback 上(对照 `layer` 作用到全部)。
>
> **逃生阀(读不下去怎么办)**:本章有四个互相缠绕的点(三段分发、fallback 复用 PathRouter、catch_all vs method_not_allowed 两条兜底线、route_layer 作用域)。如果一时绕不开,记住三句话就够——**① 未匹配请求先试主路由树 path_router,再试 fallback 路由树 fallback_router,最后试 catch_all_fallback;② fallback_router 本质就是一棵 `PathRouter<S, IS_FALLBACK=true>`,fallback handler 用一个特殊 catch-all 路径注册进去,所以匹配逻辑和普通路由一模一样;③ 路径完全不匹配走 catch_all(默认 404),路径匹配但 method 不对走 method_not_allowed(默认 405 带 Allow header),这是两层独立兜底**。带着这三句话跳到对应小节细读。本章处处承《hyper》(404/405 状态码一句带过)和《Tower》(Layer 套娃一句带过),读过那些收获翻倍,但不是硬性前提。

---

## 一句话点破

> **axum 的 fallback 不是一个"if 路由失败的尾巴",而是一棵**复用 PathRouter** 的、用 const generic `IS_FALLBACK=true` 标记的**独立路由树**。你 `.fallback(h)` 设的 handler 会被注册到两个地方:一棵 `fallback_router: PathRouter<S, true>` 里(用特殊 catch-all 路径 `/{*__private__axum_fallback}`,这样无论请求什么路径都能匹配上这棵 fallback 树),以及一个独立的 `catch_all_fallback: Fallback<S>` 字段里(处理 CONNECT 空 path 这种特殊场景)。请求来了先试主路由树 path_router,没匹配上再试 fallback_router,最后才试 catch_all_fallback——三段分发,每段解决一种"未匹配"语义。而"路径匹配但 method 不对"是另一个完全独立的兜底:它发生在 MethodRouter 内部,默认返回 `405 Method Not Allowed` 带 `Allow` header,你可以用 `.method_not_allowed_fallback(h)` 改写它,它跟 catch_all_fallback 互不混淆。Fallback 有三态(Default 默认 404 / Service 用户设的 Service / BoxedHandler 用户设的 handler fn),用 `default_fallback: bool` 标志位让 merge 时检测"两个 Router 都自定义了 fallback"这种冲突。**

这是结论,不是理由。本章倒过来拆:为什么 fallback 不是简单的 if-else、为什么 axum 要复用 PathRouter、两层兜底(catch_all / method_not_allowed)的边界怎么划、route_layer 的作用域为什么不含 fallback。

---

## 第一节:从上一章的 nest/merge 回到一个被按下的问题

### 提问

P2-05~P2-07 你看到 axum 的路由核心:`PathRouter` 用 matchit 字典树做路径匹配,`MethodRouter` 按 HTTP method 分发,nest 给子路由套 `StripPrefix + SetNestedPath` 两个 Layer,merge 把两棵路由树重编号合并。这套机制对**匹配上的请求**处理得滴水不漏。

但任何一个真实的 Web 服务都会面对一个无法回避的问题:**不是所有请求都能匹配上**。客户端可能打错 URL(`/usr/42` 而不是 `/users/42`)、可能用了你接口没实现的方法(`DELETE /users` 但你只注册了 GET/POST)、可能在爬一个根本不存在的路径。这些请求去哪了?

裸 hyper 你只能自己写一个 match 的尾巴:

```rust
// 朴素写法:裸 hyper,手写 404 尾巴(简化示意,非源码原文)
async fn handler(req: Request<Body>) -> Result<Response<Body>, Infallible> {
    if req.uri().path() == "/users" {
        // ...
    } else if req.uri().path().starts_with("/users/") {
        // ...
    } else {
        // ★ 手写 404 尾巴
        return Ok(Response::builder()
            .status(404)
            .body(Body::from("not found"))
            .unwrap());
    }
}
```

能用。但有几个问题:

1. **404 是"硬编码"的尾巴**:你想自定义 404 页面、想给未匹配请求记日志、想把 `/api/*` 的未匹配返回 JSON 而 `/static/*` 的未匹配返回 HTML——都要在每个 handler 里手写,没法统一。
2. **路径不匹配(404)和方法不匹配(405)混在一起**:裸 hyper 你不知道"`/users` 路径存在但 method 不对"和"`/usr/42` 路径根本不存在"的区别,要么都返回 404,要么自己手动维护一张"哪些路径注册了哪些 method"的表来算 405 + `Allow` header。
3. **中间件作用域混乱**:你想给"所有未匹配请求"套一个日志中间件,但不想让它作用到匹配上的路由(因为匹配上的路由有自己的日志),或者反过来——你想给匹配上的路由套鉴权中间件,但不希望未匹配请求也被鉴权(否则 404 变成 401,误导客户端)。

axum 把这套兜底机器做成了**一等公民**:有专门的 API(`.fallback` / `.fallback_service` / `.method_not_allowed_fallback`)、专门的数据结构(`fallback_router` + `catch_all_fallback` + `default_fallback` 标志位)、专门的作用域规则(`layer` 含 fallback、`route_layer` 不含)。这一章拆透这套机器。

### 不这样会怎样:把 fallback 当 if-else 尾巴会怎样

假设 axum 没有 fallback 这套抽象,只在 `PathRouter::call_with_state` 末尾加一个"匹配不上就返回 NotFound"的尾巴。会怎样?

撞几堵墙:

1. **用户没法自定义 404 页面**。每个项目的 404 长得不一样(JSON API 想返回 `{"error": "not found"}`,HTML 站点想返回一个漂亮的 404 页面,RESTful 服务可能想返回 RFC 7807 的 `application/problem+json`)。axum 必须让用户能换掉这个尾巴。
2. **方法不匹配(405)和路径不匹配(404)没法区分**。RFC 9110 明确规定:路径存在但 method 不支持,应返回 `405 Method Not Allowed` 并带一个 `Allow` header 列出支持的方法;路径根本不存在,返回 `404 Not Found`。这俩状态码语义不同,客户端(浏览器、SDK、重试逻辑)行为也不同。如果都返回 404,客户端无法区分"我打错路径了"和"我用错方法了"。
3. **fallback 上的中间件没法独立配置**。你给主路由树套的鉴权 Layer,可能不该作用到 fallback(否则未匹配请求先被 401 挡掉,变成"未授权"而不是"未找到");但有些 Layer(比如全局的日志/trace)又**应该**作用到 fallback(否则未匹配请求没日志,排查困难)。这两种需求是矛盾的,axum 必须给用户选择权(`layer` vs `route_layer`)。

> **钉死这件事**:axum 的 fallback 不是一个 `else { 404 }` 的尾巴,而是一套完整的兜底机器——能自定义 404、能区分 404/405、能给 fallback 独立套中间件。这套机器的设计动机,来自"路径不匹配"和"方法不匹配"在 HTTP 语义上是**两件不同的事**,以及"中间件在 fallback 上的作用域"是用户真实的需求。

### 所以 axum 这么设计

axum 的 Router 内部有**四个**和 fallback 相关的字段(`axum/src/routing/mod.rs#L80-L85`):

```rust
// axum/src/routing/mod.rs#L80-L85(逐字摘录)
struct RouterInner<S> {
    path_router: PathRouter<S, false>,
    fallback_router: PathRouter<S, true>,
    default_fallback: bool,
    catch_all_fallback: Fallback<S>,
}
```

四个字段,四个角色:

1. **`path_router: PathRouter<S, false>`**——主路由表,存你 `.route(...)` 注册的所有路径。第二个 const generic 参数 `false` 标记"这不是 fallback 路由器"。
2. **`fallback_router: PathRouter<S, true>`**——**fallback 路由表**,存你 `.fallback(h)` / `.fallback_service(s)` 注册的兜底。`true` 标记"这是 fallback 路由器"。注意:它和 path_router 是**同型**的(都是 `PathRouter<S, IS_FALLBACK>`),只是 const generic 不同。这是本章的重头戏——fallback 复用 PathRouter。
3. **`default_fallback: bool`**——标志位,fallback 是不是默认的(你没用过 `.fallback` 就是 true,用过就是 false)。merge 时用它检测"两个 Router 都自定义了 fallback"这种冲突(后面详拆)。
4. **`catch_all_fallback: Fallback<S>`**——终极兜底,处理一些特殊场景(下面解释为什么需要它,而不仅仅是 fallback_router)。

请求来了,`Router::call_with_state` 做三段分发(`axum/src/routing/mod.rs#L417-L432`):

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

三段分发的语义:

- **第一段** `path_router.call_with_state`:把请求交给主路由表。matchit 字典树匹配路径,匹配上就返回 future(交给对应的 MethodRouter 或 Route),匹配不上返回 `Err((req, state))`(把 req 和 state 退回来,给下一段用)。
- **第二段** `fallback_router.call_with_state`:路径在主路由表没匹配上,交给 fallback 路由表。fallback 路由表内部也跑 matchit 匹配(因为它就是一棵 `PathRouter<S, true>`)。匹配上返回 future,匹配不上返回 Err。
- **第三段** `catch_all_fallback.call_with_state`:终极兜底,直接调(不再做 matchit 匹配,它就是个单一的 Fallback)。处理一些 fallback_router 也匹配不上的边缘场景。

为什么需要**三段**而不是两段(path_router + 一个兜底)?这个问题留到第三节拆 catch_all_fallback 时回答。先记住三段的存在。

这一节先聚焦一个问题:**fallback_router 凭什么也是一棵 PathRouter?** 这是本章最值得讲的一笔。

> **承接《hyper》[[hyper-source-facts]]**:404 Not Found / 405 Method Not Allowed / Allow header 都是 HTTP 协议规定的状态码(详见 RFC 9110 / 《hyper》P2 协议机一章一句带过)。axum 不重新定义这些状态码,只是按照协议语义决定何时返回它们。本章不重复 HTTP 状态码的定义,只讲 axum 怎么在路由层实现"何时 404 / 何时 405"。

---

## 第二节:fallback 复用 PathRouter——为什么 fallback 也是一棵路由树

### 提问

上一节你看到 `fallback_router: PathRouter<S, true>`——它和 `path_router: PathRouter<S, false>` 是**同型**,只差一个 const generic 参数。这不是巧合,是 axum 刻意的设计:**fallback 复用 PathRouter,不做单独的匹配逻辑**。

这一节拆透:为什么这么做、它怎么用 const generic `IS_FALLBACK` 区分、fallback handler 到底怎么注册进这棵树。

### 不这样会怎样:fallback 单独写一套不走路由树会怎样

假设 axum 不复用 PathRouter,而是给 fallback 单独写一个简单的"匹配不上就调这个 handler"的尾巴。会怎样?

```rust
// 假想的"独立 fallback"(非 axum 实际做法)
struct RouterInner<S> {
    path_router: PathRouter<S>,
    fallback: Option<Route>,   // ★ 单独一个 Route,不是 PathRouter
}

impl RouterInner {
    fn call_with_state(...) {
        match self.path_router.call_with_state(req, state) {
            Ok(future) => future,
            Err((req, state)) => {
                // 匹配不上,直接调 fallback Route
                self.fallback.clone().unwrap().oneshot_inner_owned(req)
            }
        }
    }
}
```

听起来更简单。但撞几堵墙:

1. **fallback 拿不到 URL 参数**。假设你的 fallback 想"未匹配请求记录它打的 URL",你得从 req.uri() 手动解析。可如果你想要 `{*path}` 这种 catch-all 参数(把整个未匹配路径作为一个参数提出来),得自己写解析。而 matchit 的 `{*path}` 已经能做这件事(它是 catch-all wildcard)。axum 让 fallback 走 matchit,意味着 fallback handler 也能用 `Path` 提取器拿 URL 参数——和普通 handler 体验一致。
2. **fallback 拿不到 Layer 链**。主路由树的每条路由可以套 Layer(`.layer` / `.route_layer`),fallback 如果不走路由树,就没法套同样的 Layer。axum 的设计是:`Router::layer` 套的 Layer **同时作用到 path_router 和 fallback_router**(mod.rs#L311-L317)——这要求 fallback_router 也能存 Layer 化的 Endpoint,而 Endpoint 只在 PathRouter 里。fallback 必须是 PathRouter 才能享受 Layer 链。
3. **nest 的 fallback 没法正确合并**。你 nest 一个子 Router,子 Router 可能有自己的 fallback;父 Router 的 fallback 和子 Router 的 fallback 怎么互动?如果 fallback 是独立的 Route,nest 时父子 fallback 怎么合并很别扭。如果 fallback 是 PathRouter,nest 时子 fallback 路由器可以作为父 fallback 路由器的一部分合并(merge 的逻辑已经在 PathRouter 层实现了),逻辑零分叉。
4. **fallback 想做路径前缀匹配做不到**。假设你想"所有 `/api/*` 的未匹配请求返回 JSON,其他返回 HTML",你需要给 fallback 也做路径前缀匹配。独立 Route 做不到,PathRouter 做得到(它就是干这个的)。

axum 的取舍:**fallback 复用 PathRouter,零逻辑分叉**。fallback handler 走 matchit 匹配,能拿 URL 参数,能套 Layer,nest/merge 时和普通路由用同一套合并逻辑。代价是多一个 const generic 参数和一棵额外的路由树——这点内存和代码复杂度,换来的是 fallback 的"全功能"。

### 所以 axum 这么设计:const generic `IS_FALLBACK` 区分两棵树

来看 `PathRouter` 的定义(`axum/src/routing/path_router.rs#L16-L21`):

```rust
// axum/src/routing/path_router.rs#L16-L21(逐字摘录)
pub(super) struct PathRouter<S, const IS_FALLBACK: bool> {
    routes: HashMap<RouteId, Endpoint<S>>,
    node: Arc<Node>,
    prev_route_id: RouteId,
    v7_checks: bool,
}
```

第二个泛型参数 `const IS_FALLBACK: bool` 是一个**编译期常量**,标记这棵 PathRouter 是不是 fallback 路由器。`path_router: PathRouter<S, false>` 和 `fallback_router: PathRouter<S, true>` 在类型层面就**不是同一个类型**——编译器把它们当成两种不同的 PathRouter。

为什么要用 const generic 而不是一个运行期 bool 字段?因为 const generic 让编译器**在编译期就知道**这棵树是不是 fallback,从而可以:

1. **给 `PathRouter<S, true>` 单独实现一些方法**。比如 `new_fallback` / `set_fallback` 只对 `PathRouter<S, true>` 实现(`path_router.rs#L23-L37`),普通 PathRouter 调不到这些方法——类型层面就禁止了"给主路由树调 set_fallback"这种误用。
2. **call_with_state 内部根据 IS_FALLBACK 做条件分支**。来看 `PathRouter::call_with_state`(`path_router.rs#L388-L399`):

   ```rust
   // axum/src/routing/path_router.rs#L388-L414(摘录关键分支)
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
           // ... 拿 endpoint,调它的 call_with_state
       }
       Err(MatchError::NotFound) => Err((Request::from_parts(parts, body), state)),
   }
   ```

   注意 `if !IS_FALLBACK` 这一行——**只有主路由树(path_router)才设置 `matched-path` 扩展**,fallback 路由树不设置。为什么?因为 `MatchedPath` 提取器(P2-05 详拆)返回的是"这个请求匹配上的真实路径",而 fallback 匹配的不是真实路由,是 fallback 占位路径——设置 matched-path 会误导用户(他以为请求匹配上了 `/users/{id}`,实际匹配的是 fallback)。这个分支用 const generic 在编译期就决定了,零运行期开销。

3. **类型安全**。`PathRouter<S, false>` 和 `PathRouter<S, true>` 不能互相赋值,不能混用。如果你想错误地把 fallback_router 当主路由用,编译器直接拒绝。这是 const generic 给的编译期保证。

### fallback 怎么注册进 fallback_router:一个特殊的 catch-all 路径

关键问题来了:fallback handler 要"匹配任何路径",可 matchit 字典树是按路径匹配的——怎么让一个 handler 匹配**所有**路径?

axum 的解法:用一个**特殊的 catch-all 路径**注册 fallback。来看常量(`mod.rs#L110-L111`):

```rust
// axum/src/routing/mod.rs#L107-L111(逐字摘录)
pub(crate) const NEST_TAIL_PARAM: &str = "__private__axum_nest_tail_param";
#[cfg(feature = "matched-path")]
pub(crate) const NEST_TAIL_PARAM_CAPTURE: &str = "/{*__private__axum_nest_tail_param}";
pub(crate) const FALLBACK_PARAM: &str = "__private__axum_fallback";
pub(crate) const FALLBACK_PARAM_PATH: &str = "/{*__private__axum_fallback}";
```

`FALLBACK_PARAM_PATH = "/{*__private__axum_fallback}"`——这是一个 matchit 的 catch-all wildcard 路径(`{*name}` 语法,匹配任意路径段序列,P2-05 详拆 matchit 语法)。这个路径会匹配**任何**请求路径,因为 catch-all 把整个剩余路径都吃掉。

来看 `PathRouter::set_fallback`(`path_router.rs#L33-L36`),这是 `PathRouter<S, true>` 专属方法:

```rust
// axum/src/routing/path_router.rs#L33-L36(逐字摘录)
pub(super) fn set_fallback(&mut self, endpoint: Endpoint<S>) {
    self.replace_endpoint("/", endpoint.clone());
    self.replace_endpoint(FALLBACK_PARAM_PATH, endpoint);
}
```

`set_fallback` 把 fallback endpoint 注册到**两个**路径:`/`(根路径)和 `FALLBACK_PARAM_PATH`(catch-all)。为什么注册两个?

- **`/`**:处理 `GET /` 这种请求根路径但主路由树没注册 `/` 的情况。matchit 对 `/` 的匹配是精确的,不注册 `/` 的话 `GET /` 会 NotFound。
- **`FALLBACK_PARAM_PATH`**(catch-all):处理**任意其他**未匹配路径。catch-all 会匹配所有未匹配的路径,把整个路径作为 `__private__axum_fallback` 参数捕获(虽然这个参数用户基本不会主动提取,它只是 matchit 匹配的副产物)。

注册两个是为了覆盖所有情况:根路径 `/` 和其他路径分开处理。这是 matchit 匹配规则的细节,本质是"catch-all `/{*x}` 会不会匹配 `/`"这种边界——axum 为了保险,两个都注册。

`replace_endpoint` 的实现(`path_router.rs#L422-L432`):如果路径已存在就替换 endpoint,不存在就插入新路由。这意味着多次调 `.fallback(h)` 会覆盖前一个 fallback(最后一次生效),而不是叠加。

来看 `new_fallback`(`path_router.rs#L27-L31`),这是 `Router::new()` 初始化 fallback_router 的方法:

```rust
// axum/src/routing/path_router.rs#L27-L31(逐字摘录)
pub(super) fn new_fallback() -> Self {
    let mut this = Self::default();
    this.set_fallback(Endpoint::Route(Route::new(NotFound)));
    this
}
```

`new_fallback` 创建一个默认的 PathRouter,然后 `set_fallback(Endpoint::Route(Route::new(NotFound)))`——把默认 fallback 设成 `NotFound` 服务。`NotFound` 是 axum 内部的一个 Service(`not_found.rs#L16-L34`),它的 `call` 返回 `StatusCode::NOT_FOUND.into_response()`(就是 404 空响应):

```rust
// axum/src/routing/not_found.rs#L15-L34(逐字摘录)
#[derive(Clone, Copy, Debug)]
pub(super) struct NotFound;

impl<B> Service<Request<B>> for NotFound
where
    B: Send + 'static,
{
    type Response = Response;
    type Error = Infallible;
    type Future = std::future::Ready<Result<Response, Self::Error>>;

    #[inline]
    fn poll_ready(&mut self, _cx: &mut Context<'_>) -> Poll<Result<(), Self::Error>> {
        Poll::Ready(Ok(()))
    }

    fn call(&mut self, _req: Request<B>) -> Self::Future {
        ready(Ok(StatusCode::NOT_FOUND.into_response()))
    }
}
```

所以**默认 fallback 返回 404**,这是 axum 开箱即用的行为。你不调 `.fallback`,未匹配请求就走 NotFound → 404。

### 把 fallback 注册画出来

用 ASCII 把 fallback_router 内部的注册画清楚:

```
RouterInner<S> {
  path_router:     PathRouter<S, false>,   ← 主路由树(你 .route 注册的)
  fallback_router: PathRouter<S, true>,    ← fallback 路由树
  ...
}

fallback_router 内部(PathRouter<S, true>):
┌─────────────────────────────────────────────────────────────────┐
│  routes: HashMap<RouteId, Endpoint<S>>                           │
│    ├─ RouteId(1) → Endpoint::Route(Route::new(NotFound))         │
│    │                (注册在 "/" 路径)                              │
│    └─ RouteId(2) → Endpoint::Route(Route::new(NotFound))         │
│                     (注册在 FALLBACK_PARAM_PATH)                  │
│                                                                  │
│  node: matchit::Router<RouteId>                                 │
│    ├─ "/"                   → RouteId(1)                         │
│    └─ "/{*__private__axum_fallback}" → RouteId(2)  ← catch-all  │
└─────────────────────────────────────────────────────────────────┘
```

注意 fallback_router 内部**两条路由指向同一个 endpoint**(默认是 NotFound,你 `.fallback(h)` 后是 h 包装的 MethodRouter)。无论请求什么路径,matchit 要么精确匹配 `/`(走 RouteId 1),要么被 catch-all 吃掉(走 RouteId 2),都到同一个 fallback endpoint。这就是"fallback 匹配任意路径"的实现——**用 catch-all wildcard 在 matchit 字典树里注册一个能匹配一切的特殊路径**。

> **钉死这件事**:fallback 复用 PathRouter,fallback handler 用一个特殊 catch-all 路径 `FALLBACK_PARAM_PATH`(`/ {*__private__axum_fallback}`)注册进 `fallback_router: PathRouter<S, true>`。这样 fallback 走统一的 matchit 匹配(能拿 URL 参数)、能套 Layer(`Router::layer` 同时作用到 path_router 和 fallback_router)、nest/merge 时和普通路由用同一套合并逻辑。const generic `IS_FALLBACK` 在编译期区分主路由树和 fallback 路由树,还能给 `PathRouter<S, true>` 专属方法(`new_fallback` / `set_fallback`),并在 `call_with_state` 里用 `if !IS_FALLBACK` 跳过 fallback 的 matched-path 设置(避免误导用户)。

---

## 第三节:catch_all_fallback vs method_not_allowed_fallback——两层兜底的精确边界

### 提问

上一节你看到 fallback_router 这棵独立的路由树。但 `RouterInner` 里还有第四个字段 `catch_all_fallback: Fallback<S>`——它和 fallback_router 有什么区别?为什么需要它,而不仅仅是 fallback_router?

更要紧的是:axum 有两个公开的 fallback API——`.fallback(h)` 和 `.method_not_allowed_fallback(h)`。它们的边界在哪?一个未匹配请求到底走哪个?

这一节把两层兜底的精确边界划清楚。这是本章最容易让人混淆的地方,也是最容易在源码里看走眼的地方。

### 两条兜底线:catch_all 和 method_not_allowed

先把 axum 的两条兜底线钉死:

| 兜底线 | 触发条件 | 默认行为 | 设置 API | 内部存储 |
|--------|---------|---------|---------|---------|
| **catch_all_fallback** | 路径**完全不匹配**(主路由树和 fallback 路由树都没匹配上) | 返回 **404 Not Found** | `.fallback(h)` / `.fallback_service(s)` | `fallback_router: PathRouter<S, true>` + `catch_all_fallback: Fallback<S>` |
| **method_not_allowed_fallback** | 路径**匹配上了**但 MethodRouter 里**没装这个 method** 的 handler | 返回 **405 Method Not Allowed** 带 `Allow` header | `.method_not_allowed_fallback(h)` | 每个 `MethodRouter` 内部的 `fallback` 字段 |

两条兜底线,**完全独立**。一个请求要么走 catch_all(路径不匹配),要么走 method_not_allowed(路径匹配但 method 不对),不会两个都走。

来看官方文档怎么描述这两条边界(`axum/src/docs/routing/fallback.md#L24-L29`):

> Fallbacks only apply to routes that aren't matched by anything in the router. If a handler is matched by a request but returns 404 the fallback is not called. Note that this applies to `MethodRouter`s too: if the request hits a valid path but the `MethodRouter` does not have an appropriate method handler installed, the fallback is not called (use `MethodRouter::fallback` for this purpose instead).

翻译:fallback(catch_all)**只**作用在"没有任何路由匹配"的请求上。如果一个请求命中了一个有效路径,但 MethodRouter 没有对应的 method handler,**catch_all fallback 不会被调用**——这时走的是 method_not_allowed(MethodRouter 内部的 fallback)。这是两条兜底线的核心区别。

用一张图把两条兜底线的触发条件画清楚:

```mermaid
flowchart TB
    REQ["请求进来<br/>path + method"]
    PR["path_router.call_with_state<br/>matchit 匹配路径"]
    PR -->|"匹配上"| MR["MethodRouter.call_with_state<br/>按 method 选 handler"]
    PR -->|"没匹配上 (MatchError::NotFound)"| FR["fallback_router.call_with_state<br/>fallback 路由树"]
    MR -->|"method 匹配上"| HANDLER["调对应 handler"]
    MR -->|"method 没匹配上<br/>(所有 MethodEndpoint::None)"| MNAF["MethodRouter 内部 fallback<br/>默认 405 + Allow header<br/>可被 method_not_allowed_fallback 改写"]
    FR -->|"匹配上(注册了 / 和 catch-all)"| FB["调 fallback endpoint<br/>默认 NotFound → 404"]
    FR -.->|"理论上不会没匹配上<br/>(注册了 catch-all)| CA["catch_all_fallback<br/>(第三段,处理边缘场景)"]
    style PR fill:#dbeafe,stroke:#2563eb
    style MR fill:#dcfce7,stroke:#16a34a
    style MNAF fill:#fef9c3,stroke:#ca8a04
    style FB fill:#fee2e2,stroke:#dc2626
```

图里两条兜底线用不同颜色标出:**method_not_allowed** 在 MethodRouter 内部(黄色),**catch_all fallback** 在 fallback_router(红色)。一个请求绝不会同时走两条。

### method_not_allowed:MethodRouter 内部的 fallback

先看 method_not_allowed 这条线。它发生在 `MethodRouter::call_with_state` 内部(`method_routing.rs#L1120-L1175`)。

回顾 P2-06:MethodRouter 按 HTTP method 持有 9 个 `MethodEndpoint`(get/head/delete/options/patch/post/put/trace/connect)。请求来了,MethodRouter 用一个宏 `call!` 逐个 method 检查(`method_routing.rs#L1121-L1140`):

```rust
// axum/src/routing/method_routing.rs#L1120-L1175(摘录关键结构)
pub(crate) fn call_with_state(&self, req: Request, state: S) -> RouteFuture<E> {
    macro_rules! call {
        ($req:expr, $method_variant:ident, $svc:expr) => {
            if *req.method() == Method::$method_variant {
                match $svc {
                    MethodEndpoint::None => {}   // 该 method 没注册,继续试下一个
                    MethodEndpoint::Route(route) => {
                        return route.clone().oneshot_inner_owned($req);  // 命中!
                    }
                    MethodEndpoint::BoxedHandler(handler) => {
                        let route = handler.clone().into_route(state);
                        return route.oneshot_inner_owned($req);          // 命中!
                    }
                }
            }
        };
    }

    // 逐个 method 试(注意 HEAD 先试 head 再试 get,RFC 9110 约定)
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

    // 所有 method 都没匹配上,走 MethodRouter 内部的 fallback
    let future = fallback.clone().call_with_state(req, state);

    match allow_header {
        AllowHeader::None => future.allow_header(Bytes::new()),
        AllowHeader::Skip => future,
        AllowHeader::Bytes(allow_header) => future.allow_header(allow_header.clone().freeze()),
    }
}
```

如果请求的 method 在所有 9 个 MethodEndpoint 里都没命中(要么是 None,要么 method 不在 GET/POST/PUT/...里),就走最后的 `fallback.clone().call_with_state(req, state)`——这是 MethodRouter 内部的 fallback。

这个 fallback 字段是什么?看 MethodRouter 的定义(`method_routing.rs#L547-L559`):

```rust
// axum/src/routing/method_routing.rs#L547-L559(逐字摘录)
pub struct MethodRouter<S = (), E = Infallible> {
    get: MethodEndpoint<S, E>,
    head: MethodEndpoint<S, E>,
    delete: MethodEndpoint<S, E>,
    options: MethodEndpoint<S, E>,
    patch: MethodEndpoint<S, E>,
    post: MethodEndpoint<S, E>,
    put: MethodEndpoint<S, E>,
    trace: MethodEndpoint<S, E>,
    connect: MethodEndpoint<S, E>,
    fallback: Fallback<S, E>,   // ★ MethodRouter 内部也有一个 fallback!
    allow_header: AllowHeader,
}
```

**MethodRouter 内部也有一个 `fallback: Fallback<S, E>` 字段**——它就是 method_not_allowed 的存储位置。注意:这个 `Fallback` 类型和 mod.rs 的 `Fallback` 是**同一个**(method_routing.rs 第 13 行 `use super::{..., Fallback, ...}` 从 mod.rs 导入),三态结构一致。

来看默认 MethodRouter 的 fallback 是什么(`method_routing.rs#L752-L770`):

```rust
// axum/src/routing/method_routing.rs#L750-L770(逐字摘录)
/// Create a default `MethodRouter` that will respond with `405 Method Not Allowed` to all
/// requests.
pub fn new() -> Self {
    let fallback = Route::new(service_fn(|_: Request| async {
        Ok(StatusCode::METHOD_NOT_ALLOWED)
    }));

    Self {
        get: MethodEndpoint::None,
        head: MethodEndpoint::None,
        delete: MethodEndpoint::None,
        options: MethodEndpoint::None,
        patch: MethodEndpoint::None,
        post: MethodEndpoint::None,
        put: MethodEndpoint::None,
        trace: MethodEndpoint::None,
        connect: MethodEndpoint::None,
        allow_header: AllowHeader::None,
        fallback: Fallback::Default(fallback),   // ★ 默认 405
    }
}
```

**默认 MethodRouter 的 fallback 是一个返回 `405 Method Not Allowed` 的 service_fn**。所以当你 `.route("/", get(handler))`,如果有人 `POST /`,路径 `/` 匹配上了(get 的 MethodRouter 在),但 MethodRouter 没有 post 的 handler(`post: MethodEndpoint::None`),所有 method 试完都没命中,走 MethodRouter 内部 fallback → 默认返回 405。

这就是 method_not_allowed 的默认行为——返回 405。Rust 注释甚至明说"will respond with `405 Method Not Allowed` to all requests"(在没有 method 匹配时)。

### Allow header:405 必须带的"我支持哪些方法"

注意 `call_with_state` 末尾还有一段(`method_routing.rs#L1170-L1174`):

```rust
match allow_header {
    AllowHeader::None => future.allow_header(Bytes::new()),
    AllowHeader::Skip => future,
    AllowHeader::Bytes(allow_header) => future.allow_header(allow_header.clone().freeze()),
}
```

这是给 405 响应附上 `Allow` header(HTTP 协议规定 405 必须带 Allow,列出这个路径支持的方法)。`allow_header` 字段在 MethodRouter 注册 handler 时被填充——每注册一个 method(比如 `get(h)`),`append_allow_header(&mut self.allow_header, "GET")`(`method_routing.rs#L858`)就把 "GET" 加进 allow_header。merge 两个 MethodRouter 时 allow_header 也合并(`method_routing.rs#L1084`)。

`AllowHeader` 是个三态枚举(`method_routing.rs#L562-L569`):

- `None`:还没注册任何 method(默认)。
- `Skip`:跳过 Allow header(用于 `any(h)` 这种"匹配所有方法"的场景,后面解释)。
- `Bytes(BytesMut)`:已经累积的 Allow 值(比如 `GET,HEAD,POST`)。

所以默认 405 响应带 `Allow: GET, HEAD, POST`(如果你注册了 get + post)。这是 RFC 9110 的硬性规定,axum 在路由层自动算出来,你不用手动维护。

### method_not_allowed_fallback API:改写默认 405

你可以用 `.method_not_allowed_fallback(h)` 改写默认的 405 行为(`mod.rs#L373-L383`):

```rust
// axum/src/routing/mod.rs#L373-L383(逐字摘录)
pub fn method_not_allowed_fallback<H, T>(self, handler: H) -> Self
where
    H: Handler<T, S>,
    T: 'static,
{
    tap_inner!(self, mut this => {
        this.path_router
            .method_not_allowed_fallback(handler.clone());
    })
}
```

注意它调的是 `path_router.method_not_allowed_fallback`(主路由树,不是 fallback_router)。来看 PathRouter 的实现(`path_router.rs#L116-L126`):

```rust
// axum/src/routing/path_router.rs#L116-L126(逐字摘录)
pub(super) fn method_not_allowed_fallback<H, T>(&mut self, handler: H)
where
    H: Handler<T, S>,
    T: 'static,
{
    for (_, endpoint) in self.routes.iter_mut() {
        if let Endpoint::MethodRouter(rt) = endpoint {
            *rt = rt.clone().default_fallback(handler.clone());
        }
    }
}
```

关键!`method_not_allowed_fallback` **遍历主路由树的所有 Endpoint**,对每个 `Endpoint::MethodRouter`,调 `rt.clone().default_fallback(handler.clone())`——把每个 MethodRouter 的内部 fallback 改成你给的 handler。

来看 `MethodRouter::default_fallback`(`method_routing.rs#L665-L675`):

```rust
// axum/src/routing/method_routing.rs#L664-L675(逐字摘录)
pub(crate) fn default_fallback<H, T>(self, handler: H) -> Self
where
    H: Handler<T, S>,
    T: 'static,
    S: Send + Sync + 'static,
{
    match self.fallback {
        Fallback::Default(_) => self.fallback(handler),
        _ => self,
    }
}
```

`default_fallback` 的逻辑:**只有当 MethodRouter 的 fallback 还是 Default(默认 405)时,才替换成你给的 handler**;如果 fallback 已经被改过(比如你之前调过 `.fallback(h)` 或 `MethodRouter::fallback`),就**不动**。

这个 `default_fallback` 的语义很微妙——它尊重用户在 MethodRouter 层面的显式设置。如果你在某条路由上显式 `.fallback(h)`,这条路由的 method_not_allowed 就用 h,不会被 Router 层的 `.method_not_allowed_fallback` 覆盖。这是 axum 在"全局兜底"和"局部显式设置"之间的优先级约定:**局部显式 > 全局默认**。

### catch_all_fallback:为什么需要第三个字段

现在回到 catch_all_fallback。你已经看到 fallback_router(`PathRouter<S, true>`)能处理绝大多数未匹配请求(它注册了 `/` 和 catch-all,匹配任何路径)。那为什么 `RouterInner` 还要第四个字段 `catch_all_fallback: Fallback<S>`?

答案在 `Router::call_with_state` 的第三段(mod.rs#L428-L431):

```rust
// 第三段:catch_all_fallback
self.inner
    .catch_all_fallback
    .clone()
    .call_with_state(req, state)
```

第三段什么时候会被触发?按理说 fallback_router 注册了 catch-all `/{*__private__axum_fallback}`,应该能匹配任何路径,第二段就不会返回 Err。那第三段是干什么用的?

关键在 **`PathRouter::call_with_state` 的返回值**(`path_router.rs#L388-L419`):它只在 matchit `MatchError::NotFound` 时返回 Err。可 fallback_router 注册了 catch-all,理论上不会 NotFound。那什么情况下 fallback_router 会返回 Err?

答案:**当请求的路径有特殊形式,matchit 拒绝匹配时**。一个典型场景是 **CONNECT 请求的空 path**。CONNECT 方法(用于 HTTPS 隧道)的 URI 通常是 `authority:port` 形式(比如 `CONNECT example.com:443`),它的 path 部分是**空的**。matchit 的路径匹配要求路径以 `/` 开头(`path_router.rs#L40-L44` 的 `validate_path`),空 path 会匹配失败。

这时 fallback_router 也匹配不上,返回 Err,请求落到第三段 catch_all_fallback。catch_all_fallback 是一个**单一的 Fallback**(不是 PathRouter),它直接调用,不做路径匹配——所以它能处理这种"路径形式异常"的边缘场景。

来看 `Router::fallback` 怎么同时设置 fallback_router 和 catch_all_fallback(`mod.rs#L345-L355`):

```rust
// axum/src/routing/mod.rs#L343-L355(逐字摘录)
pub fn fallback<H, T>(self, handler: H) -> Self
where
    H: Handler<T, S>,
    T: 'static,
{
    tap_inner!(self, mut this => {
        this.catch_all_fallback =
            Fallback::BoxedHandler(BoxedIntoRoute::from_handler(handler.clone()));
    })
    .fallback_endpoint(Endpoint::MethodRouter(any(handler)))
}
```

注意 `Router::fallback` 做了**两件事**:

1. 把 `catch_all_fallback` 设成 `Fallback::BoxedHandler(BoxedIntoRoute::from_handler(handler))`——handler 被装箱成 `BoxedIntoRoute`(类型擦除,承 P1-03 的 BoxCloneSyncService 一脉,axum 在 `boxed.rs` 里实现)。
2. 调 `fallback_endpoint(Endpoint::MethodRouter(any(handler)))`——把同一个 handler 包成 `any(handler)`(一个匹配所有 method 的 MethodRouter),塞进 fallback_router。

为什么要塞两个地方(catch_all_fallback 和 fallback_router)?因为:

- **fallback_router** 处理"正常路径但没注册"的请求(走 matchit 匹配)。
- **catch_all_fallback** 处理"路径形式异常"的边缘请求(空 path、CONNECT 隧道等,matchit 拒绝匹配)。

两个用同一个 handler,保证用户 `.fallback(h)` 的语义是"所有未匹配请求都走 h",无论路径形式正常还是异常。这是 axum 对用户承诺的统一性——你设一个 fallback,所有兜底场景都走它,不用关心路径形式的边界。

来看 `any(handler)` 是什么(`method_routing.rs#L508-L515`):

```rust
// axum/src/routing/method_routing.rs#L508-L515(逐字摘录)
pub fn any<H, T, S>(handler: H) -> MethodRouter<S, Infallible>
where
    H: Handler<T, S>,
    T: 'static,
    S: Clone + Send + Sync + 'static,
{
    MethodRouter::new().fallback(handler).skip_allow_header()
}
```

`any(h)` = `MethodRouter::new().fallback(h).skip_allow_header()`——它把 h 设成 MethodRouter 的 fallback(注意是 MethodRouter 内部的 fallback,不是 Router 的),然后 `skip_allow_header()`(设置 `AllowHeader::Skip`,因为 any 匹配所有方法,不该带 Allow header——Allow 是给 405 用的,any 不返回 405)。

这里有一个微妙之处:`any(h)` 把 h 放在 **MethodRouter 的 fallback 位置**(不是任何具体的 method 位置)。这意味着 fallback_router 里的这个 MethodRouter,它的 9 个 MethodEndpoint 都是 None,但 fallback 是 h。所以**无论请求什么 method**,call_with_state 里所有 `call!` 都不命中(都是 None),最后走 MethodRouter 内部 fallback = h。

这就是 fallback handler 能匹配**任意 method** 的实现——它不是真的"注册了所有 method",而是利用 MethodRouter 的 fallback 机制,让所有 method 都落到 fallback 上。这是一个精妙的复用:**fallback handler 借用 MethodRouter 的 method-not-allowed 兜底机制,反过来实现了"匹配所有 method"**。

### fallback_service:catch_all_fallback 用 Service 而不是 handler

还有 `Router::fallback_service`(`mod.rs#L360-L371`),它接一个 Service 而不是 handler:

```rust
// axum/src/routing/mod.rs#L360-L371(逐字摘录)
pub fn fallback_service<T>(self, service: T) -> Self
where
    T: Service<Request, Error = Infallible> + Clone + Send + Sync + 'static,
    T::Response: IntoResponse,
    T::Future: Send + 'static,
{
    let route = Route::new(service);
    tap_inner!(self, mut this => {
        this.catch_all_fallback = Fallback::Service(route.clone());
    })
    .fallback_endpoint(Endpoint::Route(route))
}
```

`fallback_service` 的差别:catch_all_fallback 设成 `Fallback::Service(route)`(而不是 `Fallback::BoxedHandler`),fallback_endpoint 用 `Endpoint::Route(route)`(而不是 `Endpoint::MethodRouter(any(h))`)。这是因为 Service 已经是一个完整的 Service,不需要再走 handler → Service 的转换(没有 state 注入的步骤)。Route 直接注册到 fallback_router,catch_all_fallback 直接持有 Route 副本。

这就是 Fallback 三态存在的理由之一——**Default 默认 404、Service 用户给的现成 Service、BoxedHandler 用户给的 handler fn(需要 state 注入才能变 Route)**。三种来源,三态存储。

### 把两层兜底画出来

用一张更完整的图把两层兜底和它们的存储位置画清楚:

```
请求 path + method 进来
        │
        ▼
┌─────────────────────────────────────────────────────────────────┐
│  Router::call_with_state 三段分发                                 │
│                                                                  │
│  ① path_router.call_with_state (PathRouter<S, false>)            │
│     │ matchit 匹配路径                                            │
│     ├─ 匹配上 → 拿 Endpoint → ②                                  │
│     │     Endpoint::MethodRouter → MethodRouter.call_with_state  │
│     │       │ 逐个 method 试                                       │
│     │       ├─ method 命中 → 调 handler ★匹配成功                  │
│     │       └─ method 全没命中 → MethodRouter.fallback             │
│     │            ★ method_not_allowed 兜底(默认 405 + Allow)      │
│     │            可被 .method_not_allowed_fallback 改写           │
│     └─ 没匹配上 (MatchError::NotFound) → ③                       │
│                                                                  │
│  ③ fallback_router.call_with_state (PathRouter<S, true>)         │
│     │ matchit 匹配(注册了 / 和 catch-all)                        │
│     ├─ 匹配上 → 拿 Endpoint(默认 NotFound,或 .fallback 设的)     │
│     │     → MethodRouter(any(h)) → 所有 method 走 fallback = h    │
│     │     ★ catch_all 兜底(默认 404)                              │
│     └─ 没匹配上(路径形式异常,如 CONNECT 空 path)→ ④            │
│                                                                  │
│  ④ catch_all_fallback.call_with_state (单一 Fallback<S>)         │
│     直接调,不做路径匹配                                          │
│     ★ 终极兜底(默认 NotFound,或 .fallback 设的)                 │
└─────────────────────────────────────────────────────────────────┘

两层兜底:
  catch_all:       ③ + ④ (路径完全不匹配) → 默认 404
  method_not_allowed: ② (路径匹配但 method 不对) → 默认 405 + Allow
```

这张图把两条兜底线的触发条件和存储位置钉死了。记住:**catch_all 在 Router 层(fallback_router + catch_all_fallback),method_not_allowed 在 MethodRouter 层(每个 MethodRouter 的 fallback 字段)**。它们是两个独立的兜底机制,通过"路径是否匹配"这个条件分流。

> **钉死这件事**:catch_all_fallback 和 method_not_allowed_fallback 是两条**独立**的兜底线。catch_all 处理"路径完全不匹配"(在 Router 层,默认 404),method_not_allowed 处理"路径匹配但 method 不对"(在 MethodRouter 层,默认 405 带 Allow header)。`.fallback(h)` 同时设置 catch_all_fallback 和 fallback_router(用 `any(h)` 借用 MethodRouter 的 fallback 机制匹配所有 method),`.method_not_allowed_fallback(h)` 只改写主路由树里每个 MethodRouter 的 fallback。两者互不混淆——一个请求要么路径匹配(可能走 method_not_allowed),要么路径不匹配(走 catch_all),不会同时发生。

---

## 第四节:Fallback 三态——Default / Service / BoxedHandler

### 提问

前面几节反复提到 Fallback 三态。这一节把它单独拆透:三态各存什么、为什么是这三态、`default_fallback: bool` 标志位跟它什么关系。

### Fallback enum 的三态

来看 Fallback 的定义(`mod.rs#L680-L684`):

```rust
// axum/src/routing/mod.rs#L680-L684(逐字摘录)
enum Fallback<S, E = Infallible> {
    Default(Route<E>),
    Service(Route<E>),
    BoxedHandler(BoxedIntoRoute<S, E>),
}
```

三态:

1. **`Default(Route<E>)`**——默认 fallback,持有的是一个 Route。这是"还没被用户改过的"状态。Router::new() 时 catch_all_fallback = `Fallback::Default(Route::new(NotFound))`(NotFound 返回 404),MethodRouter::new() 时 fallback = `Fallback::Default(Route::new(405 service_fn))`。**Default 不代表"返回 404",它代表"用框架给的默认 Route"**——这个默认 Route 在 catch_all 位置返回 404,在 MethodRouter 位置返回 405。
2. **`Service(Route<E>)`**——用户通过 `.fallback_service(s)` 设了一个现成的 Service。这个 Service 已经是完整的 Service(不需要 state 注入),直接包成 Route 存起来。
3. **`BoxedHandler(BoxedIntoRoute<S, E>)`**——用户通过 `.fallback(h)` 设了一个 handler fn。handler fn 还需要 state 注入才能变 Route(`Handler::with_state(handler, state) -> HandlerService -> Route`),所以它被装箱成 `BoxedIntoRoute<S, E>`——一个类型擦除的"延迟物化"对象,等 `with_state(state)` 被调用时才真正变成 Route。

### 为什么是这三态

为什么 Fallback 要三态,而不是统一存一个 Route?因为 fallback 的**三个来源**有不同的"何时变成 Route"的时机:

| 来源 | 何时变成 Route | 存储形态 |
|------|---------------|---------|
| 框架默认(NotFound / 405) | 编译期就是 Route | `Default(Route)` |
| `.fallback_service(s)`(用户给现成 Service) | 调用时就变成 Route | `Service(Route)` |
| `.fallback(h)`(用户给 handler fn) | **要等 with_state(state) 才能变 Route**(因为 handler 需要 state) | `BoxedHandler(BoxedIntoRoute)` |

第三种是关键。handler fn(`async fn(State<Db>, ...) -> ...`)需要 state 才能变成 Service,可你在 `Router<S>` 阶段(还没 with_state)就调 `.fallback(h)`——这时 handler 还不能变 Route。axum 的解法是把它装箱成 `BoxedIntoRoute<S, E>`,**延迟到 with_state 时才物化**。

来看 `Fallback::with_state`(`mod.rs#L712-L718`)怎么处理三态:

```rust
// axum/src/routing/mod.rs#L712-L718(逐字摘录)
fn with_state<S2>(self, state: S) -> Fallback<S2, E> {
    match self {
        Fallback::Default(route) => Fallback::Default(route),       // 已经是 Route,不动
        Fallback::Service(route) => Fallback::Service(route),       // 已经是 Route,不动
        Fallback::BoxedHandler(handler) => Fallback::Service(handler.into_route(state)),  // ★ 物化!
    }
}
```

只有 `BoxedHandler` 在 with_state 时被物化:调 `handler.into_route(state)`(`BoxedIntoRoute::into_route`,`boxed.rs#L44-L46`),内部调 `Handler::with_state(handler, state)` 把 handler fn 变成 HandlerService,再包成 Route。物化后它变成 `Fallback::Service(route)`——从此它也是一个现成的 Route 了。

`Default` 和 `Service` 在 with_state 时**不变**——它们持有的 Route 本来就不依赖 state(默认 Route 是 NotFound/405 这种无状态 Service,`.fallback_service` 给的 Service 在调用时已经独立于 Router 的 state)。

这是三态存在的本质理由:**区分"已经是 Route"和"等 state 才能变 Route"两种状态,让 with_state 的物化只发生在后者**。

### call_with_state:三态怎么调

来看 `Fallback::call_with_state`(`mod.rs#L720-L728`),这是 catch_all_fallback 第三段分发时调的方法:

```rust
// axum/src/routing/mod.rs#L720-L728(逐字摘录)
fn call_with_state(self, req: Request, state: S) -> RouteFuture<E> {
    match self {
        Fallback::Default(route) | Fallback::Service(route) => route.oneshot_inner_owned(req),
        Fallback::BoxedHandler(handler) => {
            let route = handler.clone().into_route(state);
            route.oneshot_inner_owned(req)
        }
    }
}
```

三态的处理:

- **Default / Service**:都已经持有 Route,直接 `route.oneshot_inner_owned(req)`(clone Route 再 oneshot,承 P1-03 的 clone-on-call)。
- **BoxedHandler**:先 `handler.clone().into_route(state)` 把 handler 物化成 Route(每次请求都物化一次,因为 BoxedIntoRoute 是 Clone 的,clone 一份再物化),再 oneshot。

注意 BoxedHandler 在每次请求时都重新物化——这看起来浪费(为什么不在 with_state 时一次性物化成 Service?)。原因是:`Fallback::call_with_state` 这个方法在 `Router<S>` 还没 with_state 时也可能被调(比如 nest 场景下,子 Router 的 fallback 被父 Router 调用时,子 Router 可能还是 `Router<S>`)。所以 BoxedHandler 必须支持"带 state 物化"的调用方式。一旦 Router 经历了 with_state,Fallback 里的 BoxedHandler 已经被 with_state 转成 Service 了(mod.rs#L716),后续 call_with_state 走的就是 Default/Service 分支,不会每次请求都物化。这是性能和灵活性的折中。

### default_fallback: bool——merge 冲突检测

`RouterInner` 还有一个 `default_fallback: bool` 字段(`mod.rs#L83`)。它跟 Fallback 三态什么关系?

`default_fallback` 标记的是"catch_all_fallback 是不是还是默认的(NotFound)"。Router::new() 时是 true,你调过 `.fallback` 或 `.fallback_service` 后变 false(`fallback_endpoint` 方法里 `this.default_fallback = false`,mod.rs#L403)。

它的用途在 **merge 冲突检测**。来看 `Router::merge` 怎么用它(`mod.rs#L270-L296` 摘录关键):

```rust
// axum/src/routing/mod.rs#L270-L296(摘录关键分支)
match (this.default_fallback, other.default_fallback) {
    // 两个都是默认,随便取一个
    (true, true) => { /* 保持 this 的默认 */ }
    // this 默认,other 自定义 → 取 other 的
    (true, false) => { /* 用 other 的 fallback */ }
    // this 自定义,other 默认 → 取 this 的
    (false, true) => { /* 用 this 的 fallback */ }
    // 两个都自定义 → panic!
    (false, false) => {
        panic!("Cannot merge two `Router`s that both have a fallback")
    }
}
```

merge 两个 Router 时,如果**两个都自定义了 fallback**(都是 `default_fallback = false`),axum **panic**。为什么?因为 axum 不知道该用哪个 fallback——两个都是用户显式设置的,合并后语义不清。axum 的选择是**直接 panic,强迫用户用 `reset_fallback` 显式丢弃一个**(`mod.rs#L392-L398` 的 reset_fallback 方法)。

`default_fallback: bool` 就是这个检测的依据。它跟 Fallback 三态**不直接对应**——它是一个独立的标志位,语义是"用户有没有动过 fallback"。Default 三态是"框架给的默认 Route",但 default_fallback 标志位跟 Fallback enum 是两个独立的字段(catch_all_fallback 是 Fallback enum,default_fallback 是 bool)。

这个设计有点微妙:为什么不用 `matches!(catch_all_fallback, Fallback::Default(_))` 来判断"是不是默认"?因为 `.fallback_service(s)` 把 catch_all_fallback 设成 `Fallback::Service`(不是 Default),但用户确实动过 fallback——这时 default_fallback 应该是 false。所以 default_fallback 必须是一个独立的 bool,不能从 Fallback enum 推断。这是 axum 在"三态存储"和"是否动过标志位"之间的清晰分离。

> **钉死这件事**:Fallback 三态(Default / Service / BoxedHandler)区分的是 fallback 的**三个来源**和"何时变成 Route"的时机:Default 是框架默认(404 或 405,已是 Route)、Service 是用户给的现成 Service(已是 Route)、BoxedHandler 是用户给的 handler fn(等 with_state 才物化成 Route)。`default_fallback: bool` 是一个**独立**的标志位,标记"用户有没有动过 fallback",用于 merge 冲突检测(两个都自定义就 panic)。三态存储和标志位分离,因为 `.fallback_service` 把 Fallback 设成 Service(不是 Default),但用户确实动过,default_fallback 必须独立追踪。

---

## 第五节:route_layer 为什么不作用到 fallback——两种 Layer 作用域

### 提问

前面几节你看到 fallback 是一棵独立的路由树(fallback_router),它有自己的 Endpoint、能套 Layer。但 axum 给 Layer 套用提供了两个 API:`Router::layer` 和 `Router::route_layer`——它们的差别就在**作用域含不含 fallback**:

- `Router::layer`:**作用到所有路由,包括 fallback**。
- `Router::route_layer`:**只作用到匹配上的路由,不碰 fallback**。

为什么这两个 API 的作用域不一样?什么时候用哪个?这一节拆透。

### 不这样会怎样:Layer 作用域分不开会怎样

假设 axum 只有一个 `Router::layer`,没有 `route_layer`。会怎样?

考虑一个常见场景:你想给所有"匹配上的路由"套一个**鉴权 Layer**(`ValidateRequestHeaderLayer::bearer("password")`),只有带正确 token 的请求才能访问。你写:

```rust
let app = Router::new()
    .route("/foo", get(|| async {}))
    .layer(ValidateRequestHeaderLayer::bearer("password"));
```

如果 `layer` 作用到所有路由(包括 fallback),会发生一件尴尬的事:一个请求 `GET /not-found`(路径不存在),本来应该返回 404,但因为先过了鉴权 Layer(没带 token),被挡成 401 Unauthorized。客户端看到 401,以为"我没权限",实际是"路径根本不存在"——这误导客户端,也可能泄露信息(攻击者通过 401/404 的区别探测哪些路径存在)。

正确的语义是:**鉴权 Layer 只应该作用到匹配上的路由**。未匹配请求直接 404,不该被鉴权。这时你需要 `route_layer`:

```rust
let app = Router::new()
    .route("/foo", get(|| async {}))
    .route_layer(ValidateRequestHeaderLayer::bearer("password"));
// GET /foo 带正确 token → 200
// GET /foo 不带 token → 401
// GET /not-found 不带 token → 404(不被鉴权 Layer 挡)
```

`route_layer` 只作用到主路由树(path_router),不碰 fallback_router——所以未匹配请求(走 fallback)不会过鉴权 Layer。这是 axum 官方文档明说的语义(`docs/routing/route_layer.md#L9-L13`):

> This is useful for middleware that return early (such as authorization) which might otherwise convert a `404 Not Found` into a `401 Unauthorized`.

反过来,有些 Layer **应该**作用到 fallback——比如全局的日志/trace Layer(你希望未匹配请求也有日志)。这时用 `layer`:

```rust
let app = Router::new()
    .route("/foo", get(|| async {}))
    .layer(TraceLayer::new_for_http());
// GET /foo → 过 trace → 200
// GET /not-found → 过 trace → 404(也有 trace 日志)
```

`layer` 同时作用到 path_router 和 fallback_router,未匹配请求也过 trace。两种 API,两种作用域,对应两种真实需求。

### 所以 axum 这么设计:layer 含 fallback,route_layer 不含

来看两个 API 的实现。先看 `Router::layer`(`mod.rs#L302-L317`):

```rust
// axum/src/routing/mod.rs#L302-L317(逐字摘录)
pub fn layer<L>(self, layer: L) -> Router<S>
where
    L: Layer<Route> + Clone + Send + Sync + 'static,
    L::Service: Service<Request> + Clone + Send + Sync + 'static,
    <L::Service as Service<Request>>::Response: IntoResponse + 'static,
    <L::Service as Service<Request>>::Error: Into<Infallible> + 'static,
    <L::Service as Service<Request>>::Future: Send + 'static,
{
    map_inner!(self, this => RouterInner {
        path_router: this.path_router.layer(layer.clone()),       // ★ 主路由树套 Layer
        fallback_router: this.fallback_router.layer(layer.clone()), // ★ fallback 树也套 Layer
        default_fallback: this.default_fallback,
        catch_all_fallback: this.catch_all_fallback.map(|route| route.layer(layer)), // ★ catch_all 也套
    })
}
```

`layer` 给**三处**都套了 Layer:path_router、fallback_router、catch_all_fallback。所以无论请求匹配上还是走 fallback,都会过这个 Layer。

再看 `Router::route_layer`(`mod.rs#L319-L335`):

```rust
// axum/src/routing/mod.rs#L319-L335(逐字摘录)
pub fn route_layer<L>(self, layer: L) -> Self
where
    L: Layer<Route> + Clone + Send + Sync + 'static,
    L::Service: Service<Request> + Clone + Send + Sync + 'static,
    <L::Service as Service<Request>>::Response: IntoResponse + 'static,
    <L::Service as Service<Request>>::Error: Into<Infallible> + 'static,
    <L::Service as Service<Request>>::Future: Send + 'static,
{
    map_inner!(self, this => RouterInner {
        path_router: this.path_router.route_layer(layer),  // ★ 只给主路由树套
        fallback_router: this.fallback_router,             // ★ fallback 树不动!
        default_fallback: this.default_fallback,
        catch_all_fallback: this.catch_all_fallback,        // ★ catch_all 也不动!
    })
}
```

`route_layer` **只给 path_router 套 Layer**,fallback_router 和 catch_all_fallback **原样不动**。这就是"route_layer 不作用到 fallback"的实现——它就是在 map_inner 里只改 path_router,其他字段保持不变。

为什么 route_layer 能做到"只作用到匹配上的路由"?因为请求的路径如果在主路由树匹配上,它就走 path_router 里那条套了 Layer 的 Endpoint;如果没匹配上,它走 fallback_router(没套 Layer)。所以"套了 Layer 的路径"和"匹配上的路径"是同一批——Layer 自然只作用到匹配上的请求。

来看 PathRouter 层面的 route_layer 实现(`path_router.rs#L311-L340`),确认它只改 routes:

```rust
// axum/src/routing/path_router.rs#L311-L340(摘录关键)
pub(super) fn route_layer<L>(self, layer: L) -> Self
where
    L: Layer<Route> + Clone + Send + Sync + 'static,
    // ...
{
    if self.routes.is_empty() {
        panic!(
            "Adding a route_layer before any routes is a no-op. \
             Add the routes you want the layer to apply to first."
        );
    }

    let routes = self
        .routes
        .into_iter()
        .map(|(id, endpoint)| {
            let route = endpoint.layer(layer.clone());  // ★ 给每个 endpoint 套 Layer
            (id, route)
        })
        .collect();

    PathRouter {
        routes,
        node: self.node,
        prev_route_id: self.prev_route_id,
        v7_checks: self.v7_checks,
    }
}
```

`route_layer` 遍历 path_router 的所有 Endpoint,给每个套 Layer。注意它有一个 panic:如果 routes 为空(还没注册任何路由)就 panic——因为这时套 Layer 是 no-op(没路由可套),通常是 bug(用户可能把 route_layer 写在 route 前面了)。这个 panic 是 axum 的贴心之处,避免用户无意义的 Layer 套用。

### route_layer 含 fallback 会怎样:反面对比

假设 `route_layer` 也作用到 fallback(像 layer 那样)。会发生什么?

回到鉴权场景:`.route("/foo", get(h)).route_layer(AuthLayer)`。如果 route_layer 含 fallback,`GET /not-found` 会先过 AuthLayer(被挡 401),再到 fallback NotFound(404)。结果客户端看到 401——**鉴权 Layer 把 404 变成了 401**,误导客户端。

这正是 axum 用两个 API 的理由。`route_layer` 的语义是"只作用到有路由的路径",它保护 fallback 不被误伤。官方文档一句话点破(`docs/routing/route_layer.md#L11-L13`):

> This is useful for middleware that return early (such as authorization) which might otherwise convert a `404 Not Found` into a `401 Unauthorized`.

反过来,`layer` 的语义是"全局兜底",日志/trace/压缩这些 Layer 应该作用到所有请求(包括 fallback),这时用 layer。

### 两个 API 的选择指南

用一个表把两个 API 的适用场景钉死:

| API | 作用域 | 适用场景 | 反例(用错了会怎样) |
|-----|--------|---------|---------------------|
| `Router::layer` | path_router + fallback_router + catch_all_fallback(全部) | 日志/trace/压缩/全局错误处理 | 用于鉴权 → 未匹配请求被 401,404 变 401 |
| `Router::route_layer` | 只 path_router(匹配上的路由) | 鉴权/限流/提前返回的中间件 | 用于全局日志 → 未匹配请求没日志,排查困难 |

选择规则一句话:**"提前返回"类中间件(鉴权、限流)用 route_layer,"全局观察"类中间件(日志、trace、压缩)用 layer**。

还有 `MethodRouter::layer` 和 `Handler::layer`(单条路由或单个 handler 套 Layer),它们的作用域更小——只作用到那一个 MethodRouter 或 handler。这是四种作用域(`Router::layer` / `Router::route_layer` / `MethodRouter::layer` / `Handler::layer`),P4-16 中间件链章详拆,本章只聚焦 Router 层的两个。

> **承《Tower》**:Layer 套娃、Service 包装的 poll_ready 语义、Stack 类型,承《Tower》P0-01 一句带过。axum 的 `Router::layer` / `route_layer` 都接 `tower_layer::Layer<Route>`,中间件链就是 Tower 的 Service 套娃。本章只讲 axum 怎么用 Layer 控制作用域,不重复 Tower 的 Layer 内部机制。

> **钉死这件事**:`Router::layer` 作用到全部(path_router + fallback_router + catch_all_fallback),`Router::route_layer` 只作用到 path_router(不碰 fallback)。选择规则:鉴权/限流等"提前返回"类用 route_layer(避免 404 变 401),日志/trace/压缩等"全局观察"类用 layer。route_layer 在没有路由时 panic(避免无意义套用)。这是 axum 在 Layer 作用域上给用户的精细控制。

---

## 第六节:对照 go net/http、actix-web、Express——fallback 一等公民的稀缺性

### 提问

"未匹配请求走 fallback"这件事,不是 axum 发明的。但 axum 把 fallback 做成**一等公民**(有专门的数据结构、专门的 API、专门的作用域规则),这在 Web 框架里其实并不普遍。这一节对照 go net/http、actix-web、Express,看 fallback 在不同框架里的地位,理解 axum 的取舍。

### go net/http:没有 fallback 概念,要自己包 middleware

go 标准库 `net/http` 的 ServeMux **没有 fallback 概念**。未匹配请求直接返回 `404 Not Found`,你想自定义 404,只能自己包一层 middleware:

```go
// go net/http:自定义 404 要自己包(简化示意)
func main() {
    mux := http.NewServeMux()
    mux.HandleFunc("GET /users", getUsers)

    // 自定义 404:包一层
    handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        h, pattern := mux.Handler(r)
        if pattern == "" {
            // 没匹配上,自定义 404
            w.WriteHeader(404)
            w.Write([]byte(`{"error": "not found"}`))
            return
        }
        h.ServeHTTP(w, r)
    })

    http.ListenAndServe(":3000", handler)
}
```

go 的做法有几个问题:

1. **404 是手写的尾巴**:你要自己判断 `pattern == ""`(没匹配),自己写 404 响应。每个项目都要重写一遍。
2. **没有 method_not_allowed 概念**:go 1.22 的 ServeMux 支持 method(`GET /users`),但如果 method 不匹配,行为是"当作没匹配"(返回 404),**不是 405**。这跟 RFC 9110 的约定(路径存在但 method 不对应 405)不符。你要自己手动算 405 + Allow header。
3. **没有 fallback 上的 Layer 作用域**:你想给 fallback 单独套 Layer,go 没有 Layer 抽象,要自己写 middleware 链。

axum 把 fallback 做成一等公民:有 `.fallback(h)` / `.method_not_allowed_fallback(h)` 两个独立 API,有 fallback_router 专门的数据结构,有 layer/route_layer 区分作用域。这是 axum 在 Web 框架设计上的精细一笔——go 的 ServeMux 是"够用就行",axum 是"把兜底机器做透"。

### actix-web:default resource

actix-web 有一个类似的概念叫 **default resource**。你注册一个 resource,可以给它设一个 default handler:

```rust
// actix-web 风格(非 axum 实际 API)
App::new()
    .service(
        web::resource("/users/{id}")
            .route(web::get().to(get_user))
            .default_service(web::get(|| async { "default" }))
    )
    // 全局 default
    .default_service(web::to(not_found));
```

actix-web 的 `default_service` 是每个 resource 的"该 resource 没匹配 method 时走这里",以及 App 级别的 `default_service`(全局 fallback)。这跟 axum 的 method_not_allowed_fallback(每个 MethodRouter 的 fallback)和 fallback(App 级别)概念上对应。

差别在:

- actix-web 的 default_service 是 per-resource 的,你要在每个 resource 上单独设;axum 的 method_not_allowed_fallback 是全局的(一次设置,所有 MethodRouter 生效,通过 `default_fallback` 遍历实现)。
- actix-web 没有像 axum 这样明确的 catch_all vs method_not_allowed 二分(actix 的 default_service 概念更模糊,既处理路径不匹配也处理 method 不匹配,看你怎么设)。
- actix-web 没有 route_layer 这种作用域控制(actix 的中间件是 Transform/Service,作用域规则跟 axum 不同)。

axum 的设计更清晰:两条兜底线(catch_all / method_not_allowed)语义明确,API 命名直白,作用域规则文档化。这是 axum 在 API 人体工学上的一贯优势。

### Express(Node.js):app.use('*')

Express 有一个常见的"catch-all middleware"模式:

```javascript
// Express:catch-all middleware
const express = require('express');
const app = express();

app.get('/users', (req, res) => { /* ... */ });

// catch-all:匹配所有路径
app.use('*', (req, res) => {
    res.status(404).json({ error: 'not found' });
});
```

Express 的 `app.use('*', handler)` 是一个"匹配所有路径的 middleware",语义上接近 axum 的 `.fallback(h)`。但 Express 的模型是"middleware 链"(所有 middleware 按注册顺序执行,直到某个 send 响应),跟 axum 的"路由表 + 独立 fallback 路由树"不同。

差别:

- Express 的 catch-all 是 middleware 顺序链的一部分,执行顺序取决于注册顺序;axum 的 fallback 是独立的兜底机器,只在"主路由树没匹配"时触发,顺序明确。
- Express 没有 method_not_allowed 的独立概念(method 不匹配走 catch-all,不会自动 405);axum 区分 catch_all 和 method_not_allowed,自动算 405 + Allow。
- Express 的 middleware 作用域靠注册顺序和路径模式控制,没有 axum 的 layer/route_layer 二分。

### 对照表

| 框架 | fallback 概念 | method_not_allowed | 作用域控制 |
|------|--------------|-------------------|-----------|
| **axum** | 一等公民(`.fallback` / `.fallback_service`,独立 fallback_router) | 独立(`.method_not_allowed_fallback`,自动 405 + Allow) | layer(全部) / route_layer(只匹配的) |
| **actix-web** | default_service(per-resource + App 级) | 概念模糊(default_service 兼任) | Transform/Service(规则不同) |
| **go net/http** | 无(手写 404 尾巴或包 middleware) | 无(1.22 ServeMux method 不匹配返回 404,不是 405) | 无 Layer 抽象 |
| **Express** | app.use('*') catch-all middleware | 无(走 catch-all) | 注册顺序 + 路径模式 |

axum 的独特之处:**fallback 是一等公民,有独立数据结构、独立 API、明确的两层兜底(catch_all / method_not_allowed)、精细的 Layer 作用域控制**。这来自 axum 把"未匹配请求"当成一件值得专门设计的事,而不是顺手一个 `else { 404 }`。

> **钉死这件事**:fallback 作为一等公民(有专门数据结构、专门 API、专门作用域规则),在 Web 框架里并不普遍。go net/http 没有 fallback 概念(手写 404 尾巴),actix-web 有 default_service 但概念模糊,Express 有 catch-all middleware 但靠注册顺序。axum 把 fallback 做成独立机器(catch_all + method_not_allowed 两层,layer/route_layer 两种作用域),是它在 Web 框架设计精细度上的体现。

---

## 技巧精解

这一节挑本章最硬核的两个技巧,配真实源码 + 反面对比,单独拆透。

### 技巧一:fallback 复用 PathRouter 的 IS_FALLBACK const generic——为什么 fallback 也是一棵路由树

**它解决什么问题**:fallback(未匹配请求的兜底)需要"匹配任意路径、能套 Layer、能拿 URL 参数、nest/merge 时和普通路由统一处理"。如果给 fallback 单独写一套匹配逻辑,会跟主路由的逻辑分叉(维护两套代码)。axum 的解法是**让 fallback 复用 PathRouter**,用 const generic `IS_FALLBACK` 在编译期区分两棵树。

**反面对比:fallback 单独写一套不走路由树会怎样**:

```rust
// 假想的"独立 fallback"(非 axum 实际做法)
struct RouterInner<S> {
    path_router: PathRouter<S>,
    fallback: Option<Route>,   // ★ 单独一个 Route
}

impl<S> RouterInner<S> {
    fn call_with_state(&self, req: Request, state: S) -> RouteFuture {
        match self.path_router.call_with_state(req, state) {
            Ok(future) => future,
            Err((req, state)) => {
                // 匹配不上,直接调 fallback Route
                self.fallback.clone().unwrap().oneshot_inner_owned(req)
            }
        }
    }
}
```

撞墙:

1. **fallback 拿不到 URL 参数**:用户想在 fallback handler 里 `Path<{*path}>: Path<MyCatchAll>` 提取整个未匹配路径,但独立 Route 不走 matchit,没法提供 URL 参数。axum 让 fallback 走 matchit(注册 catch-all `/{*__private__axum_fallback}`),fallback handler 就能用 `Path` 提取器拿 URL——和普通 handler 体验一致。
2. **fallback 拿不到 Layer 链**:`Router::layer` 要同时作用到 path_router 和 fallback。如果 fallback 不是 PathRouter,它没有 Endpoint 结构,Layer 套不上去。axum 让 fallback 是 PathRouter,Layer 套用逻辑(`path_router.layer(layer)` 和 `fallback_router.layer(layer)` 调同一个方法)零分叉。
3. **nest/merge 时父子 fallback 合并逻辑要单独写**:如果 fallback 是独立 Route,nest 时子 Router 的 fallback 怎么跟父 Router 合并?要单独写一套合并规则。axum 让 fallback 是 PathRouter,合并走 PathRouter 的 merge 逻辑(已实现),零额外代码。
4. **fallback 想做路径前缀匹配做不到**:用户想"`/api/*` 的未匹配返回 JSON,其他返回 HTML",需要 fallback 也做路径前缀匹配。独立 Route 做不到,PathRouter 做得到。

**axum 的解法**:fallback 复用 PathRouter,用 const generic `IS_FALLBACK` 区分。来看核心代码(`path_router.rs#L16-L37`):

```rust
// axum/src/routing/path_router.rs#L16-L37(逐字摘录)
pub(super) struct PathRouter<S, const IS_FALLBACK: bool> {
    routes: HashMap<RouteId, Endpoint<S>>,
    node: Arc<Node>,
    prev_route_id: RouteId,
    v7_checks: bool,
}

impl<S> PathRouter<S, true>           // ★ 只有 IS_FALLBACK=true 才有这些方法
where
    S: Clone + Send + Sync + 'static,
{
    pub(super) fn new_fallback() -> Self {
        let mut this = Self::default();
        this.set_fallback(Endpoint::Route(Route::new(NotFound)));
        this
    }

    pub(super) fn set_fallback(&mut self, endpoint: Endpoint<S>) {
        self.replace_endpoint("/", endpoint.clone());
        self.replace_endpoint(FALLBACK_PARAM_PATH, endpoint);   // ★ catch-all 注册
    }
}
```

关键在三件事:

1. **`const IS_FALLBACK: bool`** 是编译期常量。`PathRouter<S, false>`(主路由树)和 `PathRouter<S, true>`(fallback 路由树)是**两个不同的类型**——编译器把它们当不同类型处理,不能互相赋值。
2. **`new_fallback` / `set_fallback` 只对 `PathRouter<S, true>` 实现**(impl 块的 `impl<S> PathRouter<S, true>`)。主路由树(`PathRouter<S, false>`)调不到这些方法——类型层面禁止"给主路由树调 set_fallback"这种误用。这是 const generic 给的类型安全。
3. **call_with_state 内部用 `if !IS_FALLBACK` 做条件分支**(`path_router.rs#L392-L399`):只有主路由树才设置 `matched-path` 扩展,fallback 路由树跳过(避免误导用户)。这个分支在编译期就被 const generic 决定,零运行期开销——编译器看到 `IS_FALLBACK=true` 就直接优化掉 `if !IS_FALLBACK { ... }` 块。

**fallback 怎么"匹配任意路径"**:`set_fallback` 用一个特殊 catch-all 路径 `FALLBACK_PARAM_PATH`(=`/{*__private__axum_fallback}`,mod.rs#L111)注册。matchit 的 `{*name}` 语法是 catch-all wildcard,匹配任意路径段序列——所以这个路径会匹配**任何**请求路径。fallback handler 借这个 catch-all,实现了"匹配一切"。

**为什么妙**:这套设计让 fallback 享受 PathRouter 的全部能力(matchit 匹配、URL 参数、Layer 套用、merge/nest 合并),**零逻辑分叉**——fallback 和普通路由用同一套 PathRouter 代码,只是 const generic 不同。代价是多一个 const generic 参数和一棵额外的路由树(几十字节内存)。这是 Rust const generic 在"同型结构 + 不同行为"场景的典型用法——编译期区分,运行期零开销。

**朴素地写会撞什么墙**:不用 const generic,你要么给 fallback 单独写一个结构(撞前面四堵墙),要么用运行期 bool 字段(失去编译期类型安全,主路由树可能误调 fallback 方法)。axum 选 const generic,把"两棵同型树"在类型层面钉死,运行期零分支开销。

> **钉死这件事**:fallback 复用 PathRouter,用 const generic `IS_FALLBACK` 在编译期区分主路由树(`PathRouter<S, false>`)和 fallback 路由树(`PathRouter<S, true>`)。fallback handler 用特殊 catch-all 路径 `/{*__private__axum_fallback}` 注册进 fallback 路由树,走统一的 matchit 匹配。const generic 给三件事:① 类型安全(主路由树调不到 set_fallback);② 专属方法(new_fallback/set_fallback 只对 IS_FALLBACK=true 实现);③ 编译期条件分支(call_with_state 里 `if !IS_FALLBACK` 跳过 matched-path)。这是 Rust const generic 的典型用法,零运行期开销。

### 技巧二:catch_all vs method_not_allowed 两层兜底 + route_layer 作用域——为什么是两个独立机制

**它解决什么问题**:"路径不匹配"(404)和"方法不匹配"(405)在 HTTP 语义上是两件不同的事(RFC 9110 区分 404 和 405),客户端对它们的处理也不同(405 带 Allow header 告诉客户端"我支持哪些方法",客户端可能重试;404 是"路径根本不存在",客户端不该重试)。axum 必须把这两层兜底**独立**实现,不能混为一谈。

**反面对比:catch_all 和 method_not_allowed 混在一起会怎样**:

假设 axum 只有一个 fallback,不区分 catch_all 和 method_not_allowed。一个请求 `DELETE /users`(你只注册了 GET/POST)会怎样?

- 如果都走 catch_all:返回 404。但路径 `/users` **存在**(你注册了),只是 method 不对——返回 404 误导客户端,它以为路径不存在。
- 客户端看到 404,可能放弃;看到 405 + `Allow: GET, POST`,知道"路径存在但 method 不对",可能改用 GET 重试。

RFC 9110 明确规定:路径存在但 method 不支持,返回 `405 Method Not Allowed` 并带 `Allow` header。axum 必须在路由层自动区分这两种情况,不能让用户手动算。

**axum 的解法**:两层独立兜底。

第一层 **method_not_allowed**(在 MethodRouter 内部):

```rust
// axum/src/routing/method_routing.rs#L750-L770(逐字摘录)
pub fn new() -> Self {
    let fallback = Route::new(service_fn(|_: Request| async {
        Ok(StatusCode::METHOD_NOT_ALLOWED)   // ★ 默认 405
    }));
    Self {
        // ... 9 个 MethodEndpoint::None ...
        allow_header: AllowHeader::None,
        fallback: Fallback::Default(fallback),
    }
}
```

每个 MethodRouter 默认的 fallback 是一个返回 405 的 service_fn。请求路径匹配上了(到了 MethodRouter),但 method 没命中任何 MethodEndpoint,就走这个 fallback → 405。同时 `allow_header` 累积了注册过的 method(每注册一个 method,append_allow_header 加进去),405 响应带 `Allow: GET, POST, ...`。

第二层 **catch_all**(在 Router 层):

```rust
// axum/src/routing/mod.rs#L146-L155(逐字摘录,Router::new)
pub fn new() -> Self {
    Self {
        inner: Arc::new(RouterInner {
            path_router: Default::default(),
            fallback_router: PathRouter::new_fallback(),    // ★ 默认 NotFound(404)
            default_fallback: true,
            catch_all_fallback: Fallback::Default(Route::new(NotFound)),  // ★ 404
        }),
    }
}
```

Router 层的 fallback_router 默认是 NotFound(返回 404)。请求路径在主路由树没匹配上,走 fallback_router → 404。

**两层独立的实现关键**:method_not_allowed 在 MethodRouter 内部(每个 MethodRouter 有自己的 fallback 字段),catch_all 在 Router 层(fallback_router + catch_all_fallback)。它们通过"路径是否匹配"这个条件分流——路径匹配上(到 MethodRouter)就可能在 MethodRouter 内部走 method_not_allowed;路径没匹配上(没到 MethodRouter)就走 Router 层的 catch_all。**一个请求只会走其中一条**,因为路径要么匹配要么不匹配,非此即彼。

**route_layer 作用域的配合**:两层兜底独立的另一个体现是 Layer 作用域。`route_layer` 只作用到 path_router(匹配上的路由),不碰 fallback。这意味着:

- 鉴权 Layer 用 route_layer:只作用到匹配上的路由(可能走 method_not_allowed 的请求,鉴权是合理的,因为它命中了一个有效路径);不作用到 catch_all fallback(未匹配请求不该被鉴权,避免 404 变 401)。

注意一个细节:method_not_allowed 发生在"路径匹配上"之后,所以它会过 route_layer 套的 Layer(因为 route_layer 套在 path_router 的 Endpoint 上,MethodRouter 在 Endpoint 里,method_not_allowed 是 MethodRouter 内部的事)。这是合理的——命中的路径走鉴权,method 不对被鉴权挡或被 405 挡,都是"命中路径"的语义。而 catch_all 是"路径根本没命中",不该走 route_layer。

**朴素地写会撞什么墙**:不分两层,你要么都返回 404(误导客户端,不符合 RFC),要么自己手动维护一张"哪些路径注册了哪些 method"的表来算 405 + Allow(每个项目重写)。axum 把这两层做成独立机制,自动算 405 + Allow,用户只关心业务 handler。这是 axum 在 HTTP 语义正确性上的一贯坚持——它替你处理协议细节,你写业务。

> **钉死这件事**:catch_all(路径不匹配,默认 404)和 method_not_allowed(路径匹配但 method 不对,默认 405 + Allow header)是两个**独立**的兜底机制。它们通过"路径是否匹配"分流,一个请求只走一条。method_not_allowed 在 MethodRouter 内部,catch_all 在 Router 层(fallback_router + catch_all_fallback)。route_layer 只作用到 path_router(含 method_not_allowed,因为 method_not_allowed 发生在路径匹配之后),不碰 catch_all fallback。这是 axum 在 HTTP 语义正确性(RFC 9110 的 404 vs 405 区分)上的实现。

---

## 章末小结

回到全书的二分法:**路由与分发 vs 提取与响应**。本章服务的**路由这一面**——具体说,是路由侧的**兜底机器**。

你看到了:

- **fallback 不是一个 if-else 尾巴,而是一套完整的机器**:Router::call_with_state 三段分发(path_router → fallback_router → catch_all_fallback),每段解决一种"未匹配"语义。
- **fallback 复用 PathRouter**:fallback_router 是 `PathRouter<S, true>`,用 const generic `IS_FALLBACK` 在编译期区分主路由树和 fallback 路由树。fallback handler 用特殊 catch-all 路径 `FALLBACK_PARAM_PATH`(=`/{*__private__axum_fallback}`)注册,走统一的 matchit 匹配(能拿 URL 参数)、能套 Layer、nest/merge 时和普通路由用同一套合并逻辑。
- **catch_all vs method_not_allowed 两层独立兜底**:catch_all(路径完全不匹配,默认 404)在 Router 层(fallback_router + catch_all_fallback),method_not_allowed(路径匹配但 method 不对,默认 405 + Allow header)在 MethodRouter 层(每个 MethodRouter 的 fallback 字段)。一个请求通过"路径是否匹配"分流,只走其中一条。
- **Fallback 三态(Default / Service / BoxedHandler)**:区分 fallback 的三个来源和"何时变成 Route"的时机。`default_fallback: bool` 是独立的标志位,用于 merge 冲突检测(两个都自定义就 panic)。
- **route_layer vs layer 两种作用域**:layer 作用到全部(path_router + fallback_router + catch_all_fallback),route_layer 只作用到 path_router(不碰 fallback)。鉴权/限流等"提前返回"类用 route_layer(避免 404 变 401),日志/trace/压缩等"全局观察"类用 layer。

承《hyper》(404/405 状态码、Allow header 的 HTTP 语义一句带过指路 P2 协议机章);承《Tower》(Layer 套娃、Service 包装、Stack 类型一句带过指路 P0-01);承《Tokio》(运行时一句带过)。

### 五个为什么清单

1. **为什么 fallback 是一棵独立的 PathRouter,而不是一个 Route?** 因为 fallback 需要"匹配任意路径、能套 Layer、能拿 URL 参数、nest/merge 时和普通路由统一处理"。复用 PathRouter 让 fallback 享受全部能力(matchit 匹配、Layer、merge),零逻辑分叉。const generic `IS_FALLBACK` 在编译期区分两棵树,给类型安全 + 专属方法 + 编译期条件分支。

2. **为什么 fallback handler 能匹配任意路径?** 因为它用特殊 catch-all 路径 `FALLBACK_PARAM_PATH`(=`/{*__private__axum_fallback}`)注册进 fallback_router。matchit 的 `{*name}` 是 catch-all wildcard,匹配任意路径段序列。同时 fallback_endpoint 用 `any(h)`(=`MethodRouter::new().fallback(h).skip_allow_header()`)让 fallback 匹配任意 method(借用 MethodRouter 的 fallback 机制)。

3. **为什么 catch_all_fallback 和 method_not_allowed_fallback 要分两层?** 因为"路径不匹配"(404)和"方法不匹配"(405)在 HTTP 语义上是两件不同的事(RFC 9110)。405 带 Allow header 告诉客户端"我支持哪些方法",客户端可能重试;404 是"路径不存在",客户端不该重试。axum 自动区分,不让用户手动算。catch_all 在 Router 层,method_not_allowed 在 MethodRouter 层,通过"路径是否匹配"分流。

4. **为什么 Fallback 要三态(Default / Service / BoxedHandler)?** 因为 fallback 有三个来源:框架默认(NotFound/405,已是 Route)、用户给现成 Service(`.fallback_service`,已是 Route)、用户给 handler fn(`.fallback`,等 with_state 才物化成 Route)。三态区分"何时变成 Route"。with_state 时 BoxedHandler 被物化成 Service,Default 和 Service 不变。

5. **为什么 route_layer 不作用到 fallback,而 layer 作用到全部?** 因为鉴权/限流等"提前返回"类中间件只该作用到匹配上的路由(否则未匹配请求被 401 挡掉,404 变 401,误导客户端);而日志/trace/压缩等"全局观察"类中间件该作用到所有请求(包括 fallback)。route_layer 只改 path_router(不碰 fallback_router 和 catch_all_fallback),layer 改三处。这是 axum 在 Layer 作用域上给用户的精细控制。

### 想继续深入往哪钻

- **PathRouter 的 matchit 字典树匹配原理 + RouteId 双向映射**:→ 第 5 章(P2-05),PathRouter 招招牌章,catch-all wildcard `/{*x}` 的匹配规则在那里详拆。
- **MethodRouter 按 method 分发 + merge_for_path**:→ 第 6 章(P2-06),MethodRouter 的 9 个 MethodEndpoint + Allow header 累积逻辑在那里拆透。
- **nest/merge 时 fallback 怎么合并(子 Router 的 fallback 跟父 Router 怎么互动)**:→ 第 7 章(P2-07),nest 的 StripPrefix + SetNestedPath,merge 的路由 ID 重编号和 fallback 冲突检测。
- **Handler trait 怎么把任意 async fn 变 Service(fallback handler 也是 handler,走同一套宏展开)**:→ 第 9 章(P3-09),Handler trait 的 T 参数 + impl_handler! 宏。
- **Layer/ServiceBuilder 怎么叠中间件(fallback 上的 Layer 链怎么套娃)**:→ 第 16 章(P4-16),中间件链与 ServiceBuilder,四种作用域(Router::layer / route_layer / MethodRouter::layer / Handler::layer)全拆。
- **HTTP 404/405/Allow header 的协议语义**:→《hyper》P2 协议机章,状态码和 header 的协议层处理(本书一句带过)。
- **Tower 的 Layer/Service 套娃内部**:→《Tower》P0-01,Service×Layer 双抽象(成网后)。

### 引出下一章

本章你拿到了 axum 路由侧的兜底机器:fallback 复用 PathRouter(const generic IS_FALLBACK 区分),catch_all vs method_not_allowed 两层独立兜底,Fallback 三态,route_layer vs layer 两种作用域。至此,第 2 篇路由与分发四连章(P2-05 PathRouter / P2-06 MethodRouter / P2-07 nest/merge / P2-08 fallback)全部讲完——你能在脑子里放映出"一个请求按 URL + method 怎么找到 handler fn,匹配不上走哪条兜底"的完整画面。

但二分法的**另一面**——提取与响应——还没开始。一个请求匹配上了 handler fn,handler 的参数怎么自动从 Request 提?返回值怎么自动变成 Response?这就是第 3 篇的事。下一章 P3-09(★★双招牌章)会拆透 axum 最值得讲也最容易看不懂的地方:`Handler<T, S>` trait 怎么用 `T` coherence 占位 tuple + `impl_handler!` + `all_the_tuples!` 宏,把任意 `async fn(State, Path, Json) -> impl IntoResponse` 在编译期变成一个 `tower::Service`。那是全书最难也最值的一章,从这里开始,我们从"路由与分发"转向"提取与响应"。

---

> **本章源码锚点(全部经本地 `../axum/` Grep/Read 核实,版本 axum 0.8.9 / axum-core 0.5.5 / axum-macros 0.5.1 / matchit 0.8.4,commit c59208c86fded335cd85e388030ad59347b0e5ae)**:
>
> - [RouterInner 结构(path_router + fallback_router + default_fallback + catch_all_fallback)](../axum/axum/src/routing/mod.rs#L80-L85) —— 四个字段,fallback_router 是 `PathRouter<S, true>`。
> - [FALLBACK_PARAM_PATH 常量](../axum/axum/src/routing/mod.rs#L110-L111) —— `/{*__private__axum_fallback}`,catch-all 注册路径。
> - [Router::call_with_state 三段分发](../axum/axum/src/routing/mod.rs#L417-L432) —— path_router → fallback_router → catch_all_fallback。
> - [Router::layer(作用到全部)](../axum/axum/src/routing/mod.rs#L302-L317) —— path_router + fallback_router + catch_all_fallback 都套 Layer。
> - [Router::route_layer(只 path_router)](../axum/axum/src/routing/mod.rs#L319-L335) —— 只改 path_router,fallback_router/catch_all_fallback 不动。
> - [Router::fallback(同时设 catch_all + fallback_router)](../axum/axum/src/routing/mod.rs#L343-L355) —— catch_all_fallback = BoxedHandler,fallback_endpoint(any(h))。
> - [Router::fallback_service(catch_all + fallback_router 用 Route)](../axum/axum/src/routing/mod.rs#L360-L371) —— catch_all_fallback = Service,fallback_endpoint(Route)。
> - [Router::method_not_allowed_fallback](../axum/axum/src/routing/mod.rs#L373-L383) —— 委托 path_router.method_not_allowed_fallback。
> - [Router::fallback_endpoint(私有,设 fallback_router + default_fallback=false)](../axum/axum/src/routing/mod.rs#L399-L405)。
> - [Router::reset_fallback(恢复默认)](../axum/axum/src/routing/mod.rs#L392-L398) —— 重置 fallback_router + default_fallback=true + catch_all=NotFound。
> - [Router::new(fallback_router 初始化为 NotFound,catch_all=Default(NotFound))](../axum/axum/src/routing/mod.rs#L146-L155)。
> - [Fallback enum 三态(Default/Service/BoxedHandler)](../axum/axum/src/routing/mod.rs#L680-L684)。
> - [Fallback::with_state(BoxedHandler 物化成 Service)](../axum/axum/src/routing/mod.rs#L712-L718)。
> - [Fallback::call_with_state(三态分发)](../axum/axum/src/routing/mod.rs#L720-L728)。
> - [Fallback::merge(merge 冲突检测)](../axum/axum/src/routing/mod.rs#L690-L696)。
> - [PathRouter 结构 + const IS_FALLBACK](../axum/axum/src/routing/path_router.rs#L16-L21) —— `PathRouter<S, const IS_FALLBACK: bool>`。
> - [PathRouter::new_fallback(IS_FALLBACK=true 专属)](../axum/axum/src/routing/path_router.rs#L23-L31) —— set_fallback(NotFound)。
> - [PathRouter::set_fallback(注册 / 和 FALLBACK_PARAM_PATH)](../axum/axum/src/routing/path_router.rs#L33-L36)。
> - [PathRouter::method_not_allowed_fallback(遍历 routes,改每个 MethodRouter 的 fallback)](../axum/axum/src/routing/path_router.rs#L116-L126)。
> - [PathRouter::call_with_state(matchit 匹配 + if !IS_FALLBACK 跳过 matched-path)](../axum/axum/src/routing/path_router.rs#L370-L420)。
> - [PathRouter::replace_endpoint(已存在替换,不存在插入)](../axum/axum/src/routing/path_router.rs#L422-L432)。
> - [PathRouter::layer(给所有 endpoint 套 Layer)](../axum/axum/src/routing/path_router.rs#L285-L308)。
> - [PathRouter::route_layer(同 layer,但 routes 空时 panic)](../axum/axum/src/routing/path_router.rs#L311-L340)。
> - [MethodRouter 结构(9 MethodEndpoint + fallback + allow_header)](../axum/axum/src/routing/method_routing.rs#L547-L559) —— fallback 字段是 `Fallback<S, E>`(从 mod.rs 导入,三态同源)。
> - [MethodRouter::new(默认 fallback 返回 405)](../axum/axum/src/routing/method_routing.rs#L750-L770) —— service_fn 返回 StatusCode::METHOD_NOT_ALLOWED。
> - [MethodRouter::call_with_state(逐个 method 试 + 走 fallback + Allow header)](../axum/axum/src/routing/method_routing.rs#L1120-L1175)。
> - [MethodRouter::fallback(设内部 fallback)](../axum/axum/src/routing/method_routing.rs#L654-L662)。
> - [MethodRouter::default_fallback(只在 Default 时替换)](../axum/axum/src/routing/method_routing.rs#L664-L675) —— 尊重用户显式设置。
> - [AllowHeader 三态(None/Skip/Bytes)](../axum/axum/src/routing/method_routing.rs#L561-L569)。
> - [any(handler) = MethodRouter::new().fallback(h).skip_allow_header()](../axum/axum/src/routing/method_routing.rs#L508-L515) —— 借 MethodRouter fallback 匹配所有 method。
> - [NotFound 服务(返回 404)](../axum/axum/src/routing/not_found.rs#L15-L34)。
> - [BoxedIntoRoute(handler 类型擦除 + 延迟物化)](../axum/axum/src/boxed.rs#L12-L178) —— from_handler / into_route / map。
>
> **外部 crate(诚实标注,非 axum 源码)**:本章未深入外部 crate 内部,涉及 Tower Layer/Service 承《Tower》一句带过,涉及 hyper 状态码承《hyper》一句带过。
>
> **承接**:
> - **承《hyper》[[hyper-source-facts]]**:404/405 状态码、Allow header 的 HTTP 协议语义(RFC 9110)承《hyper》P2 协议机章(一句带过)。axum 不重新定义状态码,只按协议语义决定何时返回。
> - **承《Tower》[[tower-source-facts]]**:Layer 套娃、Service 包装、Stack 类型、poll_ready 语义承《Tower》P0-01(一句带过)。axum 的 `Router::layer` / `route_layer` 都接 `tower_layer::Layer<Route>`,中间件链就是 Tower Service 套娃。本章只讲 axum 怎么用 Layer 控制作用域。
> - **承《Tokio》[[tokio-source-facts]]**:运行时一句带过(fallback handler 是 async fn,跑在 Tokio 上)。
>
> **核实并修正的源码印象**(以源码为准):
> - **Fallback 三态真值**:`enum Fallback<S, E = Infallible> { Default(Route<E>), Service(Route<E>), BoxedHandler(BoxedIntoRoute<S, E>) }`(mod.rs#L680-L684)。任务书提到的"Default/Service/BoxedHandler"三态名**完全正确**。注意 `Default(Route)` 里持有的是 Route(不是 NotFound 结构本身),`Service(Route)` 也是 Route,两者只差语义标志(是否被用户改过)。
> - **MethodRouter 的 fallback 字段不是独立 enum**:它**复用** mod.rs 的 `Fallback<S, E>` 三态(method_routing.rs#L13 `use super::{..., Fallback, ...}`,#L557 `fallback: Fallback<S, E>`)。即 MethodRouter 内部的 fallback 和 Router 的 catch_all_fallback 是**同一个 Fallback 类型**,三态结构一致。这一点容易看走眼以为是两个独立 enum,实际是同一个。
> - **IS_FALLBACK const generic 怎么用**:`PathRouter<S, const IS_FALLBACK: bool>`(path_router.rs#L16),`path_router: PathRouter<S, false>` + `fallback_router: PathRouter<S, true>`(mod.rs#L81-L82)。`new_fallback` / `set_fallback` 只对 `PathRouter<S, true>` 实现(path_router.rs#L23-L37)。`call_with_state` 里 `if !IS_FALLBACK` 跳过 matched-path(path_router.rs#L392-L399)。任务书提示的"fallback 复用 PathRouter 用 IS_FALLBACK=true 标记"**完全正确**,且额外发现 const generic 还用于条件分支(不只是标记)。
> - **fallback 注册两个路径**:`set_fallback` 同时注册 `/` 和 `FALLBACK_PARAM_PATH`(path_router.rs#L34-L35),不是只注册 catch-all。这是 matchit 匹配 `/` 和其他路径的边界处理。
> - **catch_all_fallback 的真实用途**:处理"路径形式异常"(如 CONNECT 空 path,matchit 拒绝匹配)的边缘场景,不是冗余字段。`.fallback(h)` 同时设 catch_all_fallback 和 fallback_router(mod.rs#L350-L354),保证用户 `.fallback(h)` 对所有未匹配场景生效。
> - **method_not_allowed_fallback 通过 default_fallback 实现**:它调 `MethodRouter::default_fallback`(method_routing.rs#L665-L675),只在 fallback 还是 Default 时替换,**尊重用户在 MethodRouter 层的显式设置**(局部显式 > 全局默认)。
> - **any(h) 的实现**:`MethodRouter::new().fallback(h).skip_allow_header()`(method_routing.rs#L514)——借用 MethodRouter 的 fallback 机制实现"匹配所有 method",并跳过 Allow header(因为 any 不返回 405)。这是 fallback handler 能匹配任意 method 的根本。
