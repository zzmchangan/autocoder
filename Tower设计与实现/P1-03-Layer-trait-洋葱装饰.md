# 第 3 章 · Layer trait:洋葱装饰

> 第 1 篇 · 核心 trait:Service 与 Layer(Tower 灵魂)· 组合单元

---

## 章首

**核心问题**:你已经在前一章(P1-02)看到 `Service = poll_ready(&mut self) + call(&mut self, req) -> Future`,知道它是一个"持有资源、有背压、能被层层嵌套"的执行单元。可是真实服务从来不是一个 Service 就完事——鉴权、日志、压缩、限流、超时、重试,这些"横切关注点"每个都要在请求进出的路径上动一刀。如果照着 P1-02 的样子,给每个横切关注点都去改一遍业务 Service 的 `call`,业务代码会被埋在"先打日志、再校验 token、再压缩、再限流、最后才到业务"的杂烩里,改一处要碰十个文件。Tower 怎么让"鉴权 / 日志 / 压缩 / 限流"这些跨协议、跨框架、跨业务复用的横切关注点,**完全不侵入业务 Service**?又怎么把多层横切关注点像洋葱一样层层套、还能在 Rust 的类型系统里编译期嵌套成零运行时开销的栈?

读完本章你会明白:

1. **Layer 凭什么"装饰而不侵入"**——它是一个 `Layer<S>` trait,签名 `fn layer(&self, inner: S) -> Self::Service`,把"吃一个 Service、吐一个装饰过的 Service"本身抽象成一类一等公民。业务 Service 完全不知道自己被谁装饰过,中间件也完全不知道自己装饰的是谁。这种"装饰工厂"的解耦,是横切关注点能复用的根。
2. **`Stack<Inner, Outer>` 凭什么把多层 Layer 在编译期嵌套成零开销洋葱**——它自己也是一个 `Layer`,递归 `impl`,把两层 Layer 串成一层;`Stack<A, Stack<B, Stack<C, Identity>>>` 这种类型就是一棵洋葱树。整棵树单态化后没有运行期链表、没有 next 指针、没有虚分派,全部内联成直接调用。这是 Rust 把"洋葱中间件"做进类型系统的胜利,也是 Tower 区别于 gRPC/Envoy/Go 等运行期链表方案的根本特征。
3. **`Stack::new(inner, outer)` 参数顺序到底意味着什么,以及"装饰顺序 vs 执行顺序"为什么是同一件事**——`Stack` 的 `layer()` 先 apply `inner`(新加的 Layer)、再 apply `outer`(更早加的旧栈),apply 越晚包得越外。所以"链式先加的 Layer 在最外层,请求最先穿过它"。这个顺序是 P1-04 `ServiceBuilder` 的语义根,本章一次性钉死,和 P1-04 完全对齐。
4. **`Identity`(空 Layer)和 `LayerFn`(闭包当 Layer)凭什么"零开销"**——`Identity` 用 `_p: ()` 零字节字段、`layer(inner)` 直接返回 `inner`,是 Layer 世界的单位元;`LayerFn<F>` 把 `Fn(S) -> Out` 闭包当 Layer,是 Layer 版的 `service_fn`。两个组合起来,让"写一个最简 Layer"既不需要写 struct 也不需要写 impl 块。

**逃生阀(本章有点绕)**:如果你被"类型级洋葱"绕晕了,先记住一句话就够——**Layer 是一个把 Service 变成新 Service 的工厂,Stack 把多个工厂在编译期拼成一个大工厂,Identity 是什么也不做的空工厂**。带着这句话跳到第三节看 `Stack` 源码,再看第四节那张"装饰顺序 vs 执行顺序"的对照表,主线就清晰了。本章直球为主,只在描述请求穿过顺序时偶尔用"洋葱"点睛一句(那是 P0-01 已建好的比喻),不靠比喻做主线。读完本章,P1-04 那条 `.buffer().timeout().retry()` 链式 API 的所有底层原语你就全拿到了。

**前置知识**:假设你读过 P0-01(执行单元 Service × 组合单元 Layer 的全景)和 P1-02(`Service` trait 的 `&mut self` 与 `poll_ready` 背压)。本章反复回扣 P1-02 的"`poll_ready` 透传背压"——一个中间件 Service 的 `poll_ready` 通常就是把请求转给内层 `inner.poll_ready(cx)`,这章默认你懂这一条。不需要你写过 Tower 源码,但需要你见过 Rust 的泛型、关联类型、trait bound。

---

## 一句话点破

> **Layer 是 Tower 把"装饰请求处理"做成一等公民的那一笔:一个 `Layer<S>` 不持有 Service、只持有"装饰配置"(超时长度 / 重试策略 / 限流速率),它的 `layer(inner)` 把一个 Service 装饰成新 Service。多个 Layer 用 `Stack<Inner, Outer>` 在类型系统里编译期嵌套成一棵洋葱树,运行期零开销。Layer 解耦了"装饰"和"被装饰",让同一套鉴权 / 日志 / 压缩能套到任意 Service 上、且能在 axum/tonic/reqwest/Pingora 之间复用——这是横切关注点不侵入业务代码的全部秘密。**

这是结论,不是理由。本章倒过来拆:先看"横切关注点不侵入业务"这事儿朴素地做会撞什么墙 → 再看 Layer trait 凭什么这个签名(`fn layer(&self, inner: S) -> Self::Service`)就能解决 → 然后看 `Stack<Inner, Outer>` 怎么把多层 Layer 在类型系统里编译期嵌套 → 再看 `Stack::new(inner, outer)` 的参数顺序怎么决定"装饰顺序 = 执行顺序" → 最后看 `Identity` / `LayerFn` / 元组 Layer 这些"零开销原语"怎么让最简情况也好写。结尾技巧精解单独拆透 `Stack` 的递归嵌套 + 单态化,配反面对比"运行期链表 vs 编译期 Stack"。

---

## 第一节:横切关注点为什么不能塞进业务 Service

### 1.1 提问:日志这种事,直接写进 `call` 不行吗

设想你写一个最朴素的 Rust 异步 HTTP 服务,业务是把请求体里的用户名转大写回传。最直白的写法是把鉴权、日志、压缩、限流全揉进 `call`:

```rust
// 朴素写法:业务 + 横切全揉在 call 里(简化示意)
fn call(&mut self, req: String) -> Self::Future {
    if !is_authorized(&req) { return ready(Err(...)); }     // 横切一:鉴权
    println!("[req] {}", req);                              // 横切二:日志
    let ctx = CompressionCtx::new();                        // 横切三:压缩准备
    if !rate_limiter().try_take() { return ready(Err(...)); } // 横切四:限流
    let resp = req.to_uppercase();                          // ← 业务只有这一行
    let resp = ctx.compress(resp);                          // 横切三收尾
    println!("[resp] {}", resp);                            // 横切二收尾
    ready(Ok(resp))
}
```

能跑。但这是噩梦的开始。

### 1.2 不这样会怎样:横切关注点和业务揉在一起的四种病

**病一:业务代码被横切逻辑淹没**。上面那个 `call`,业务只有一行 `req.to_uppercase()`,其余全是横切。真实服务里横切常常是业务的 5-10 倍,业务淹没得更彻底,新人接手半天找不到业务到底干了什么。

**病二:横切逻辑无法复用**。下一个 Service(比如 `ReverseSvc`)也要鉴权、日志、压缩、限流,你把那套横切抄一份过去。十几个 Service 抄十几份。某个策略改了(比如日志格式从 `println!` 改成 `tracing::info!`),要改十几个文件,漏改一个就是不一致。

**病三:横切顺序无法统一调整**。某天发现"应该先限流再鉴权"(先挡流量风暴再校验 token,省得限流前就花 CPU)。你得在十几个 Service 里把限流挪到鉴权前。挪错一个,行为分裂。

**病四:横切逻辑无法跨协议 / 跨框架复用**。你给 axum 写的鉴权,搬到 tonic 想用?axum 的 `call` 签名和 tonic 不一样,搬不动。给 HTTP 写的日志,想给 gRPC 用?gRPC 请求是 protobuf 帧,日志格式得改。横切逻辑被协议和框架绑死。

共同根因:**横切关注点和业务揉在同一个 `call` 里,装饰(横切)和被装饰(业务)没解耦**。横切逻辑是"跨多个业务、跨多个协议、跨多个框架"的,业务逻辑是"特定于某个场景"的,两者本该分开。

> **钉死这件事**:横切关注点(cross-cutting concerns)是软件工程经典问题——它们不属于任何一个业务模块,却要切进每一个。OOP 用 AOP / 动态代理,Go 用 `func(http.Handler) http.Handler` 闭包链,Rust 异步生态用 Tower 的 `Layer`:编译期类型嵌套把横切逻辑织进 Service 栈,运行期零开销。

### 1.3 Tokio / hyper / gRPC / Go 怎么做(承接,一句带过)

横切关注点的组装不是 Tower 发明的问题,各框架各语言都自己定了一套:

- **Tokio**:运行时,只管跑 Future,不认识"请求"。给你 `sleep` / `Semaphore` / `mpsc` 这些原语,但不告诉你"超时套哪一层""permit 在哪里 acquire"。Tokio 不解决组装问题,只提供原语。内部机制(reactor / scheduler / time wheel / Semaphore)《Tokio》已拆透,一句带过指路 [[tokio-source-facts]]。
- **hyper**:HTTP 协议层,自己的 `service::Service` 简化版(删了 `poll_ready`,背压挪协议层,这个对照贯穿全书)。hyper 中间件绑死 HTTP 语义,不能跨协议复用。**承接《hyper》**:P1-02 讲 Service 入门、P1-03 讲 Tower 中间件链入门(对照 gRPC filter),本章深化 Layer 这一面,不重复 hyper 讲过的,详见 [[hyper-series-project]] / [[hyper-source-facts]]。
- **gRPC / Envoy**:运行期洋葱(filter 链表 / `shared_ptr` 链 + overload manager)。承接《gRPC》[[grpc-source-facts]] /《Envoy》[[envoy-source-facts]],本章后半和 Tower 编译期 `Stack` 对照。
- **Go**:`func(http.Handler) http.Handler` 运行期闭包链,无背压(靠 GC / channel 兜底)。

Rust 异步生态有 axum / tonic / reqwest / Pingora 多个框架,如果每个都自定一套中间件抽象,你写一个鉴权要适配四套——这就是 P0-01 讲过的碎片化。Tower 治法是定义 `Service`(执行)+ `Layer`(组合)两个 trait,所有框架挂同一套抽象,横切写一次四框架通用。

> **钉死这件事**:Tower 的 Layer trait 解决的不是"横切关注点怎么实现"(那是 timeout / retry / 限流各自的事),而是"横切关注点怎么**组装到 Service 上**而不侵入业务"。把"装饰 Service"本身抽象成 trait(`Layer<S>`),让装饰成为一类一等公民,可以单独组装、传递、复用。横切逻辑是装饰的内容,Layer 是装饰的形式——形式和内容解耦,横切就能跨协议、跨框架、跨业务复用。

---

## 第二节:Layer trait 凭什么这个签名就能解决

### 2.1 真身就这么多:trait + 一个 blanket impl

`tower-layer` crate 的全部公开 API,就 `Layer` trait 加 `Identity` / `Stack` / `LayerFn` / `layer_fn` / 元组 Layer 几个辅助类型。先看 trait 本体,在 `tower-layer/src/lib.rs` 第 95 到 101 行:

```rust
// tower-layer/src/lib.rs#L95-L101(逐字摘录)
pub trait Layer<S> {
    /// The wrapped service
    type Service;
    /// Wrap the given service with the middleware, returning a new service
    /// that has been decorated with the middleware.
    fn layer(&self, inner: S) -> Self::Service;
}
```

四行就完了。把这套签名逐字拆开:

1. **`Layer<S>` 是泛型参数 `S`**——不是 `Layer<Request>` 这种请求类型,而是"被装饰的 Service 的类型"。一个 `Layer<MySvc>` 表示"我能装饰 `MySvc` 这种 Service"。这种"`S` = 被装饰的 Service 类型"的设计,让 Layer 可以对任意 Service 类型泛化,不绑死请求 / 响应类型。
2. **`type Service` 是关联类型**——Layer 装饰完吐出的新 Service 类型。`TimeoutLayer` 装饰 `MySvc` 吐出 `Timeout<MySvc>`,所以 `TimeoutLayer::Service = Timeout<MySvc>`。关联类型(而不是泛型)表示"对固定的 `(Layer 类型, S 类型)`,装饰结果唯一确定"。
3. **`fn layer(&self, inner: S) -> Self::Service`**——这是 Layer 的唯一方法。`&self` 表示 Layer 本身只读(可复用、可 Clone),`inner: S` 是按值传入(装饰产生全新 Service,不修改原 Service),`Self::Service` 是装饰结果。
4. **没有别的方法**——Layer 不处理请求,不返回 Future,不做背压。它纯粹是"装饰工厂",和 Service(执行单元)职责彻底分开。

文件末尾(第 103-112 行)还有一个 blanket impl,给 `&'a T` 也实现 `Layer`,委托给 `T`:

```rust
// tower-layer/src/lib.rs#L103-L112
impl<'a, T, S> Layer<S> for &'a T
where
    T: ?Sized + Layer<S>,
{
    type Service = T::Service;

    fn layer(&self, inner: S) -> Self::Service {
        (**self).layer(inner)
    }
}
```

这个 blanket impl 的价值:`&TimeoutLayer` 也能当 `Layer` 用,无需重复 impl。在 ServiceBuilder / Stack 这种"Layer 被借用复用"的场景(`Stack` 的 `layer(&self)` 借用自己,内部把 `self.inner` / `self.outer` 当 Layer 调 `layer`)很关键——`self.inner` 是 `Inner` 类型(可能是 `Layer` 也可能是 `&Layer`),有了 blanket impl,两种情况都走同一条路径。

> **承接 P1-02**:`Service` trait 的方法都是 `&mut self`(消费资源、改内部状态),`Layer` trait 的 `layer` 是 `&self`(只读、装饰配置不可变)。这两者的对比不是巧合——Service 是"会变的执行单元",Layer 是"不变的装饰工厂"。这种"可变执行 vs 不变装饰"的拆分,是 Tower 双抽象(Service × Layer)的根本设计动机。

### 2.2 把签名读成一句话:Layer = "Service 的装饰器工厂"

`Layer<S>` 这套签名,用一句话概括:**给我一个 S 类型的 Service,我给你一个装饰过的新 Service(`Self::Service`)**。

注意三个"不":

- **Layer 不持有 Service**。它持有的只是"装饰配置"——超时长度 `Duration`、重试策略 `Policy`、限流速率 `u64`、日志 target `&'static str`。装饰配置和被装饰的 Service 完全分开。
- **Layer 不处理请求**。它没有 `poll_ready`、没有 `call`、不返回 Future。它的全部职责就是"造一个新 Service",新 Service 怎么处理请求是 Service 自己的事(新 Service 通常内部持有原 Service + 装饰逻辑)。
- **Layer 不修改原 Service**。`inner: S` 是按值传入(被 move 进 Layer),`layer()` 返回的是全新 Service,原 Service 在 `layer()` 内部被 move 进新 Service 的字段(或者被某个中间状态消费)。装饰是"产生新值",不是"原地修改"。

这三条合起来,就是"装饰器工厂"的语义:Layer 是一个工厂,你给它一个 Service 原料,它给你一个装饰过的 Service 产品。工厂自己不变(只存配置),原料(原 Service)被消费,产品(新 Service)是新的。

### 2.3 文档里的 LogLayer 示例:Layer + Service 一对

`tower-layer/src/lib.rs` 第 42 到 87 行的文档注释给了一个完整的 LogLayer / LogService 示例,是讲"Layer + 它包出来的 Service 长什么样"的范本。我们逐段拆:

```rust
// tower-layer/src/lib.rs#L42-L87(逐字摘录,关键部分)
pub struct LogLayer {
    target: &'static str,
}

impl<S> Layer<S> for LogLayer {
    type Service = LogService<S>;

    fn layer(&self, service: S) -> Self::Service {
        LogService {
            target: self.target,
            service,
        }
    }
}

// This service implements the Log behavior
pub struct LogService<S> {
    target: &'static str,
    service: S,
}

impl<S, Request> Service<Request> for LogService<S>
where
    S: Service<Request>,
    Request: fmt::Debug,
{
    type Response = S::Response;
    type Error = S::Error;
    type Future = S::Future;

    fn poll_ready(&mut self, cx: &mut Context<'_>) -> Poll<Result<(), Self::Error>> {
        self.service.poll_ready(cx)
    }

    fn call(&mut self, request: Request) -> Self::Future {
        // Insert log statement here or other functionality
        println!("request = {:?}, target = {:?}", request, self.target);
        self.service.call(request)
    }
}
```

把它拆成"工厂 + 产品"两部分:

**工厂:LogLayer**。字段只有一个 `target: &'static str`——日志打到哪里。`LogLayer` 自己不持 Service,它只持有"日志配置"。`Layer<S>::layer()` 干的事:把 `target` 和传入的 `service` 一起塞进 `LogService`,返回 `LogService<S>`。注意 `LogLayer` 没有 `S` 这个泛型参数(它是 `LogLayer`,不是 `LogLayer<S>`),因为它对不同类型的 `S` 都能装饰(`impl<S> Layer<S> for LogLayer`),装饰结果 `LogService<S>` 才带 `S`。这种"Layer 不带 S 泛型、Service 带 S 泛型"是 Tower 里常见的写法。

**产品:LogService**。字段 `target: &'static str`(从 Layer 复制过来)+ `service: S`(被装饰的原 Service)。`LogService<S>` 实现了 `Service<Request>`——它的 `poll_ready` 直接调 `self.service.poll_ready(cx)`(背压透传,这是 P1-02 讲过的招牌:中间件 Service 的 `poll_ready` 通常就是把请求转给内层),`call` 先打印一行日志再调 `self.service.call(request)` 把请求转发给内层。

**关键观察:Layer 和 Service 是两个 struct,职责彻底分开**。`LogLayer` 是装饰工厂(只存配置),`LogService` 是装饰产品(持有原 Service + 装饰逻辑)。一个 Layer 类 + 一个 Service 类,这是写 Tower 中间件的标准模板。你以后看 `TimeoutLayer` / `Timeout`、`RetryLayer` / `Retry`、`BufferLayer` / `Buffer`、`RateLimitLayer` / `RateLimit`,全是这个套路:Layer 配置 + Service 实现。

### 2.4 反面对比:如果只有 Service 没有 Layer 会怎样

`Layer` 这个抽象存在的意义,要在反面对比里才看得清。假设 Tower 只有 `Service` trait,没有 `Layer`,你想给 `MySvc` 套个超时。最朴素的做法:

```rust
// 没有 Layer 的朴素写法:装饰和服务耦合
let with_timeout = Timeout::new(MySvc, Duration::from_secs(5));
```

看起来也行,但有三个问题立刻冒出来(这和 P0-01 第一节末尾那段呼应):

**问题一:无法延迟组装**。`Timeout::new(MySvc, dur)` 要求你**此刻就有 MySvc**。可很多场景里,你想先声明"我要套这些装饰"(比如配置文件解析阶段),等业务 Service 准备好了(比如连接池建立完了)再一次性套上去。没有 Layer,装饰和构造绑死,你做不到"先攒装饰、后套服务"。

**问题二:装饰链无法复用**。同一套"超时 + 重试 + 限流"装饰,你想套到 `UserService` 也想套到 `OrderService`。没有 Layer,你得分别写:

```rust
let user_svc = Timeout::new(Retry::new(UserService, policy), timeout);
let order_svc = Timeout::new(Retry::new(OrderService, policy), timeout);
// 改顺序?两处都改。policy 变了?两处都改。
```

装饰信息(`timeout`、`policy`)在两处重复声明,改一处要碰两处。要是 10 个 Service 套同一套装饰,改一处碰 10 处。

**问题三:装饰信息揉进了具体 Service 类型**。你想写一个函数,接受"任意被 `Timeout` 装饰过的 Service":`fn handle<S>(svc: Timeout<S>)`。这要求你拿到 `Timeout<S>`,但你拿不到"它是被什么 Layer 装饰的"这个抽象——装饰信息全揉进了 `Timeout<S>` 这个具体类型。你想把"装饰链"当一等公民传递(比如配置文件里写 `[[middleware]]` 列表,运行期组装),没有 Layer 抽象,装饰链无法被独立描述。

`Layer` 解决这三个问题:

- **延迟组装**:Layer 不持 Service,只持配置。你可以先把一堆 Layer 组装成 `Stack`,等 Service 来了再 `stack.layer(svc)`。`ServiceBuilder`(P1-04)就是这种"先攒 Layer 后套 Service"的链式 API。
- **装饰链复用**:同一个 `Stack` 可以套不同 Service,因为 `Stack: Layer<S>` 对任意满足约束的 `S` 都成立。`stack.layer(user_svc)` / `stack.layer(order_svc)` 共享同一套装饰配置。
- **装饰链一等公民**:`Stack<TimeoutLayer, Stack<RetryLayer, ...>>` 这个类型本身就是"装饰链的描述",可以当变量传递、存进 struct、序列化、配置。

> **钉死这件事**:Layer 把"装饰 Service"从"被装饰的 Service"里抽离出来,成为一类一等公民。装饰(横切关注点的配置)和被装饰(业务 Service)解耦,才有延迟组装、装饰链复用、装饰链当一等公民这三件事。这是横切关注点不侵入业务的根本机制——业务 Service 完全不知道自己被谁装饰过,装饰逻辑也完全不知道自己装饰的是谁。

### 2.5 拆 LogLayer 的 `poll_ready`:背压是怎么透传的

`LogService::poll_ready` 那一行 `self.service.poll_ready(cx)` 看似平淡,藏着 Tower 中间件的灵魂——**背压透传**。P1-02 已经把这个机制拆透,这里只用 LogService 这个最简例子印证一次。

设想 LogService 套在一个 `ConcurrencyLimit<MySvc>` 外面(`LogService<ConcurrencyLimit<MySvc>>`)。一次请求进来:

1. 调用方调 `log_svc.poll_ready(cx)`。
2. `LogService::poll_ready` 调 `self.service.poll_ready(cx)`,也就是 `ConcurrencyLimit::poll_ready`。
3. `ConcurrencyLimit::poll_ready` 尝试 acquire 一个 permit,如果并发满了返回 `Pending`。
4. 这个 `Pending` 一路传回 `LogService::poll_ready`(`self.service.poll_ready(cx)` 直接返回),再到调用方。
5. 调用方拿到 `Pending`,知道"现在别塞请求",注册 Waker 等待。
6. 某个 in-flight 请求完成,permit 还回 Semaphore,Semaphore 唤醒 Waker。
7. 调用方重新 `poll_ready`,这次 `ConcurrencyLimit` 拿到 permit 返回 `Ready(Ok)`,`LogService` 也返回 `Ready(Ok)`,调用方才 `call`。

这就是背压传染——最内层的资源约束(ConcurrencyLimit 的并发上限)通过 `poll_ready` 链一路传到最外层调用方,中间每一层(LogService)不增不减地透传。LogService 自己不持资源(它只有 `target` 和 `service`),所以它的 `poll_ready` 就是直接转发,这是"纯转发型"中间件的标准实现。

**关键:Layer 包出来的中间件 Service,`poll_ready` 必须正确透传**。这是 P1-02 反复强调的"`poll_ready` 是背压,不是辅助函数"在组合单元这一面的体现。Layer 的 `layer()` 负责造新 Service,新 Service 的 `poll_ready` 负责把背压传上去——两件事各司其职,缺一不可。如果你写一个 Layer,它包出来的 Service 在 `poll_ready` 里"忘记"调内层 `poll_ready`(直接返回 `Ready`),背压链就断了,内层满了调用方不知道,请求堆积,内存爆。这是写 Tower 中间件最常见的正确性陷阱,P1-02 的反例剖析(`mem::replace` 惯用法)和后续章节(Buffer / ConcurrencyLimit)都会反复回扣。

---

## 第三节:Stack<Inner, Outer> 把多层 Layer 在类型系统里编译期嵌套

到这里你已经知道:Layer 是"装饰工厂",可以单独存在。但真实场景从来不是一个 Layer——你要"超时 + 重试 + 限流 + 鉴权"好几个 Layer。怎么把多个 Layer 拼成一个能用的整体?

Tower 的答案是 `Stack<Inner, Outer>`:把两个 Layer 在**类型系统**里拼成一个新的 Layer。多个 Layer 用 Stack 层层嵌套,就在编译期搭出一棵洋葱树,运行期零开销。这是 Tower 组合单元的招牌技巧,也是它区别于运行期链表方案(gRPC / Envoy / Go)的根本特征。

### 3.1 真身:30 行源码,价值连城

`tower-layer/src/stack.rs` 全文 63 行(含 Debug impl),核心就 30 行。逐字贴:

```rust
// tower-layer/src/stack.rs#L1-L30(逐字摘录)
use super::Layer;
use std::fmt;

/// Two middlewares chained together.
#[derive(Clone)]
pub struct Stack<Inner, Outer> {
    inner: Inner,
    outer: Outer,
}

impl<Inner, Outer> Stack<Inner, Outer> {
    /// Create a new `Stack`.
    pub const fn new(inner: Inner, outer: Outer) -> Self {
        Stack { inner, outer }
    }
}

impl<S, Inner, Outer> Layer<S> for Stack<Inner, Outer>
where
    Inner: Layer<S>,
    Outer: Layer<Inner::Service>,
{
    type Service = Outer::Service;

    fn layer(&self, service: S) -> Self::Service {
        let inner = self.inner.layer(service);

        self.outer.layer(inner)
    }
}
```

把这套 30 行逐段拆开,每一行都值得停一下:

**结构体 `Stack<Inner, Outer>`**(`#L6-L9`):两个字段,`inner: Inner` 和 `outer: Outer`。两个字段类型都是泛型参数,意味着 Stack 可以装任意两个 Layer。`#[derive(Clone)]` 表示只要 `Inner` 和 `Outer` 都 `Clone`,Stack 也 `Clone`——这让 Stack 可以像 Layer 配置一样被复用 / 复制。

**`Stack::new(inner, outer)`**(`#L11-L16`):构造函数,`const fn`(编译期可执行)。**参数顺序是 `(inner, outer)`——第一个参数进 `inner` 位,第二个进 `outer` 位**。这个顺序至关重要,它决定了 Stack 怎么 apply,稍后专门拆。`const fn` 意味着 `Stack::new(SomeLayer, Identity::new())` 这种调用可以在编译期求值,运行期零开销。

**`impl<S, Inner, Outer> Layer<S> for Stack<Inner, Outer>`**(`#L18-L30`):这是整章最关键的 impl 块。**Stack 自己也是一个 Layer**——它实现了 `Layer<S>` trait。这意味着 Stack 可以像单个 Layer 一样被传递、被套到 Service 上、被嵌套进更大的 Stack。三层含义:

- **bound `Inner: Layer<S>`**:Stack 的 `inner` 字段必须能装饰 `S` 类型 Service。
- **bound `Outer: Layer<Inner::Service>`**:Stack 的 `outer` 字段必须能装饰 `inner` 装饰后的结果(`Inner::Service`)。这个 bound 把两层 Layer 的"输出 / 输入"对上了——`inner` 先装饰,结果给 `outer` 再装饰。
- **`type Service = Outer::Service`**:Stack 装饰 `S` 后的结果类型,等于 `outer` 装饰完的类型。换句话说,Stack 的最终产品由 `outer` 决定(`outer` 在最外层)。

**`fn layer(&self, service: S) -> Self::Service`**(`#L25-L29`):这是 Stack 的 apply 逻辑,整章最核心的三行:

```rust
fn layer(&self, service: S) -> Self::Service {
    let inner = self.inner.layer(service);     // 第一步:inner 先 apply
    self.outer.layer(inner)                     // 第二步:outer 后 apply
}
```

三行拆开:

1. `self.inner.layer(service)`——让 `inner` Layer 先装饰 `service`,得到中间结果 `inner`(局部变量,类型是 `Inner::Service`)。**inner 先 apply,它装饰得离原 service 最近,所以它在更内层。**
2. `self.outer.layer(inner)`——让 `outer` Layer 装饰"已经被 inner 装饰过的"中间结果,得到最终 Service(类型 `Outer::Service`)。**outer 后 apply,它装饰得离原 service 远,所以它在更外层。**
3. 返回 `Outer::Service`——最终的、被两层装饰过的 Service。

**这就是 Stack 把两层 Layer 串起来的全部机制**。它不是运行期链表,没有 next 指针,没有循环——就两行 `layer()` 调用,在编译期被单态化成两次直接构造。

### 3.2 嵌套:Stack<A, Stack<B, Stack<C, Identity>>> 是一棵洋葱树

Stack 真正的威力在嵌套。因为 Stack 自己也是 Layer,Stack 可以套 Stack,层层嵌套:

```rust
// 三层 Layer 的 Stack(伪代码,展示类型嵌套)
let stack: Stack<LayerA, Stack<LayerB, Stack<LayerC, Identity>>> = ...;
```

这个类型 `Stack<LayerA, Stack<LayerB, Stack<LayerC, Identity>>>` 本身就是一棵洋葱树:

```
Stack<LayerA, Stack<LayerB, Stack<LayerC, Identity>>>
   │
   ├─ inner: LayerA                    (顶层 Stack 的 inner)
   └─ outer: Stack<LayerB, Stack<LayerC, Identity>>
              │
              ├─ inner: LayerB          (二层 Stack 的 inner)
              └─ outer: Stack<LayerC, Identity>
                         │
                         ├─ inner: LayerC   (三层 Stack 的 inner)
                         └─ outer: Identity (单位元,空 Layer)
```

调 `stack.layer(svc)` 时(假设 svc 类型是 `S`),递归展开:

- **顶层 `Stack<A, Stack<B, Stack<C, Identity>>>::layer(svc)`**:
  - `self.inner` = LayerA,先 `LayerA.layer(svc)` → 得 `A<S>`(LayerA 装饰 S 的结果,假设记为 `SvcA`)。
  - `self.outer` = `Stack<B, Stack<C, Identity>>`,后 `Stack<B, Stack<C, Identity>>::layer(SvcA)`:
    - `self.inner` = LayerB,先 `LayerB.layer(SvcA)` → 得 `B<A<S>>`(记为 `SvcB`)。
    - `self.outer` = `Stack<C, Identity>`,后 `Stack<C, Identity>::layer(SvcB)`:
      - `self.inner` = LayerC,先 `LayerC.layer(SvcB)` → 得 `C<B<A<S>>>`(记为 `SvcC`)。
      - `self.outer` = Identity,后 `Identity.layer(SvcC)` → 直接返回 `SvcC`(Identity 什么也不做,见第四节)。
    - 返回 `SvcC`(类型 `C<B<A<S>>>`)。
  - 返回 `SvcC`(类型 `C<B<A<S>>>`)。

**最终类型:`C<B<A<S>>>`**——LayerA 装饰得最内(A 紧贴 S),LayerC 装饰得最外(C 在最外层)。注意三个 Layer 的嵌套顺序:

- 顶层 Stack 的 `inner` = LayerA,在最终类型里是**最内层**(`A<S>`)。
- 顶层 Stack 的 `outer` 的 `inner` = LayerB,在最终类型里是**中间层**(`B<...>`)。
- 顶层 Stack 的 `outer` 的 `outer` 的 `inner` = LayerC,在最终类型里是**最外层**(`C<...>`)。

这个顺序是 Stack 的 `layer()` "先 apply inner 后 apply outer" 决定的——**越靠 apply 末尾(越晚 apply)的 Layer,装饰得越外**。顶层 Stack 的 outer 子树的 inner(LayerC)是最后被 apply 的(因为它在递归最深的 outer 链上),所以它在最外层。

> **钉死这件事**:`Stack<Inner, Outer>` 的类型嵌套,直接编码了洋葱结构。`Stack<A, Stack<B, Stack<C, Identity>>>` 这棵类型树,apply 后的最终类型是 `C<B<A<S>>>`(C 最外、A 最内)。类型即结构,结构即行为——编译期就把洋葱钉死了。运行期没有链表遍历,没有 next 指针,apply 顺序被编译器编译成一连串直接构造。

### 3.3 单态化后:Stack 就是一串直接调用,零运行期开销

到这里你可能会问:这套类型嵌套,运行起来是不是要遍历类型树、查找虚函数?答案是**完全不用**。

Stack 的 `Layer::layer()` 是泛型方法,不是 trait object 方法。当编译器看到 `Stack<A, Stack<B, Stack<C, Identity>>>::layer(svc)` 时,它把这套泛型代码**单态化**——为 `A` / `B` / `C` / `Identity` 这些具体类型生成一份专属的机器码。单态化后的 `layer()` 长这样(伪代码,展示单态化效果):

```rust
// 单态化后(简化示意,非源码原文)
fn layer_stack_a_b_c_identity(svc: S) -> C<B<A<S>>> {
    let svc_a = LayerA::layer(&layer_a_config, svc);              // 直接调用,可内联
    let svc_b = LayerB::layer(&layer_b_config, svc_a);            // 直接调用,可内联
    let svc_c = LayerC::layer(&layer_c_config, svc_b);            // 直接调用,可内联
    let svc_final = Identity::layer(&identity, svc_c);            // 直接返回 svc_c
    svc_final
}
```

四行直接调用,**没有虚分派、没有链表遍历、没有 next 指针解引用**。如果 `LayerA::layer` / `LayerB::layer` / `LayerC::layer` 的实现足够简单(像 LogLayer 就几行),编译器甚至会把它们**内联**进 `layer_stack_a_b_c_identity` 里,最终生成的机器码和手写:

```rust
// 手写等价(简化示意)
let svc_a = LogService { target: t1, service: svc };
let svc_b = LogService { target: t2, service: svc_a };
let svc_c = LogService { target: t3, service: svc_b };
```

几乎完全一样。这就是"零成本抽象"——你写的是高层的 `Stack<A, Stack<B, Stack<C, Identity>>>`,编译器生成的代码和你手写嵌套一样快。

> **承接《Tokio》[[tokio-source-facts]]**:这种"泛型单态化 + 内联"的零成本抽象,是 Rust 的招牌。Tokio 的 Future 组合子(`future.and_then(f).map(g)`)、《hyper》的 Service 嵌套、标准库 Iterator(`iter.map(f).filter(p)`)都是同一招——类型嵌套表达处理链,单态化把链编译成直接调用。Tower 的 Stack 把这套范式搬到了"装饰 Service"上,代价是类型签名长(被链式 API 和类型推导藏住),收益是零运行期开销。

### 3.4 反面对比:运行期链表 / 闭包链(gRPC / Envoy / Go 那样)会怎样

Stack 的"编译期零开销"要在反面对比里才看得清。跨语言对照(承接 P0-01 第八节,这里只点要害):

- **gRPC C++ filter stack**([[grpc-source-facts]]):filter 链是运行期 `std::vector<std::unique_ptr<Filter>>`,filter 间用 next 指针串联。每次请求遍历链表,每个 filter 的 `Process` 是虚调用(vtable 查找 + 间接调用),N 层 filter 就 N 次虚调用 + 堆分配。
- **Envoy filter chain**([[envoy-source-facts]]):Network / HTTP filter 用 `shared_ptr` 在运行期组装,可以热配置、可以挂 overload manager。开销和 gRPC 类似——虚调用 + 链表遍历 + 堆分配。
- **Go middleware**:Go 的 `func(http.Handler) http.Handler` 是运行期闭包链(`TimeoutMiddleware(d)(RetryMiddleware(3)(finalHandler))`),每次请求穿过 N 层闭包(N 次间接调用 + 闭包捕获内存访问 + GC 压力),且没有背压概念(靠 channel 兜底)。

三种方案的共同点:**洋葱结构在运行期才组装成链表 / 闭包链,运行期每次请求都要遍历**。Tower 不一样——Stack 把洋葱做进**类型系统**,编译期单态化后是一串直接调用,运行期零开销(具体反面对比"`Vec<Box<dyn Layer>>` 朴素版会撞什么墙"放到技巧精解里展开)。

Tower 选编译期的代价是:① 类型签名爆炸(`Stack<TimeoutLayer, Stack<RetryLayer, ...>>` 套 10 层长到没法看);② 洋葱结构编译期固定,运行期不能动态增减中间件(要按请求内容动态决定套哪几层,得用 `BoxService`,P6-17);③ 单态化代码量随组合数膨胀。换来的是运行期零开销——这是 Rust 零成本抽象哲学的典型,也是 Tower 区别于 gRPC / Envoy / Go 等运行期链表方案的根本特征(详见技巧精解)。

> **钉死这件事**:同一思想(洋葱中间件)在四种语言里有四种落地。C++ / Go 把洋葱做成运行期链表(虚调用 / 闭包开销),Rust 用 `Stack<Inner, Outer>` 把洋葱做进类型系统(编译期单态化、零开销)。代价是类型丑、编译慢,Tower 用 `BoxService` 家族(P6-17)作"运行期擦除"逃生阀。这是 Tower"丑陋但快"的根源,P0-01 第八节有完整四方案对照表。

---

## 第四节:Stack::new(inner, outer) 参数顺序与"装饰顺序 = 执行顺序"

`Stack::new(inner: Inner, outer: Outer)` 这个参数顺序,是 Tower 最容易翻车的细节之一,也是 P1-04 `ServiceBuilder` 的语义根。这一节用源码 + 图 + 推演,一次钉死,和 P1-04 完全对齐。

### 4.1 参数顺序:新 Layer 进 inner 位,旧栈进 outer 位

回忆 `Stack::new` 的签名(`tower-layer/src/stack.rs#L13`):

```rust
pub const fn new(inner: Inner, outer: Outer) -> Self {
    Stack { inner, outer }
}
```

**第一个参数进 `inner` 位,第二个参数进 `outer` 位**。Tower 里调 `Stack::new` 的地方主要是 `ServiceBuilder::layer`(P1-04 详拆,`tower/src/builder/mod.rs#L125-L130`):

```rust
// tower/src/builder/mod.rs#L125-L130(P1-04 已拆,这里只看 Stack::new 的调用)
pub fn layer<T>(self, layer: T) -> ServiceBuilder<Stack<T, L>> {
    ServiceBuilder {
        layer: Stack::new(layer, self.layer),    // ← 注意参数顺序
    }
}
```

`Stack::new(layer, self.layer)` —— 第一个参数是**新加的 layer**(`T`),第二个参数是**旧的累积层**(`self.layer`,类型 `L`)。所以:

- **新加的 layer → `inner` 位**。
- **旧栈(更早加的 Layer 们)→ `outer` 位**。

这个调用约定是 P1-04 "先 `.layer()` 的在外层" 的根。我们来推演一次,看清楚为什么。

### 4.2 推演:两次 `.layer()` 后,谁在内谁在外

假设你写(P1-04 1.4 节的例子,我们再推一遍):

```rust
// 注意:这是 ServiceBuilder 的写法,P1-04 详拆。这里只看 Stack 的演化
let builder = ServiceBuilder::new()           // builder.layer = Identity
    .timeout(Duration::from_secs(1))           // 步骤 A:加 TimeoutLayer
    .retry(policy);                            // 步骤 B:加 RetryLayer
// 然后 builder.service(my_svc) 套到 my_svc 上
```

**初始**:`ServiceBuilder::new()` 内部 `layer: Identity::new()`,类型 `ServiceBuilder<Identity>`。

**步骤 A `.timeout(d)`**:内部调 `.layer(TimeoutLayer::new(d))`,即 `Stack::new(TimeoutLayer, Identity)`。`inner = TimeoutLayer`,`outer = Identity`。builder 类型变成 `ServiceBuilder<Stack<TimeoutLayer, Identity>>`。

**步骤 B `.retry(p)`**:内部调 `.layer(RetryLayer::new(p))`,即 `Stack::new(RetryLayer, <上一步的 Stack<TimeoutLayer, Identity>>)`。`inner = RetryLayer`,`outer = Stack<TimeoutLayer, Identity>`。builder 类型变成 `ServiceBuilder<Stack<RetryLayer, Stack<TimeoutLayer, Identity>>>`。

注意类型签名的嵌套:`Stack<RetryLayer, Stack<TimeoutLayer, Identity>>`。**后加的 RetryLayer 在顶层 Stack 的 `inner` 位,先加的 TimeoutLayer 在顶层 Stack 的 `outer` 子树里。**

**调 `.service(my_svc)`**:触发 `Stack<RetryLayer, Stack<TimeoutLayer, Identity>>::layer(my_svc)`,代入 Stack 的 `Layer` impl(`tower-layer/src/stack.rs#L25-L29`):

- `self.inner` = RetryLayer,先 `RetryLayer.layer(my_svc)` → 得 `Retry<my_svc>`(RetryLayer 装饰 my_svc 的结果)。
- `self.outer` = `Stack<TimeoutLayer, Identity>`,后 `Stack<TimeoutLayer, Identity>::layer(Retry<my_svc>)`:
  - `self.inner` = TimeoutLayer,先 `TimeoutLayer.layer(Retry<my_svc>)` → 得 `Timeout<Retry<my_svc>>`。
  - `self.outer` = Identity,后 `Identity.layer(Timeout<Retry<my_svc>>)` → 直接返回 `Timeout<Retry<my_svc>>`(Identity 是空 Layer)。
- 最终返回 `Timeout<Retry<my_svc>>`。

**最终类型:`Timeout<Retry<my_svc>>`**。Timeout 在最外层,Retry 在中间,my_svc 在最内层。

**关键观察**:

- 先 `.timeout()` 加的 TimeoutLayer,在最终类型里是**最外层**(包在最外面)。
- 后 `.retry()` 加的 RetryLayer,在最终类型里是**中间层**(被 Timeout 包着,又包着 my_svc)。
- `my_svc`(业务 Service)在最内层。

**口诀**:**"先 `.layer()` 的在外层,后 `.layer()` 的在内层"**。或者从请求视角说:**"链式从上往下写 = 请求从外往里穿"**——先写的 Layer 在最外层,请求进来第一个碰到它。

### 4.3 为什么是这个顺序:apply 越晚包得越外

这个顺序的根,在 Stack 的 `layer()` "先 apply inner 后 apply outer"(`tower-layer/src/stack.rs#L25-L29`):

```rust
fn layer(&self, service: S) -> Self::Service {
    let inner = self.inner.layer(service);     // ① inner 先 apply
    self.outer.layer(inner)                     // ② outer 后 apply
}
```

**inner 先 apply,意味着 inner 装饰得离原 service 近(inner 在更内层)**。
**outer 后 apply,意味着 outer 装饰得离原 service 远(outer 在更外层)**。

结合 `.layer(T)` 的调用约定 `Stack::new(T, old_stack)`(`T` 进 inner 位、`old_stack` 进 outer 位):

- 新加的 `T` 进 inner 位 → 先 apply → 在更内层。
- 旧栈 `old_stack`(更早加的 Layer 们)进 outer 位 → 后 apply → 在更外层。

所以**先加的 Layer 被 apply 得越晚(因为它在每次新 `.layer()` 后被推到更深的 outer 子树里),apply 越晚包得越外,所以先加的在外层**。反过来,**后加的 Layer 在最近一次 `.layer()` 里进 inner 位,apply 得最早,在内层**。

这就是"先加的在外、后加的在内"的完整推演。它由两个事实合起来:

1. `Stack::new(新T, 旧L)`:新 T 进 inner、旧 L 进 outer。
2. `Stack::layer`:inner 先 apply、outer 后 apply,apply 越晚包得越外。

> **钉死这件事**:`Stack::new(inner, outer)` 的参数顺序 + `Stack::layer` "先 inner 后 outer" 的 apply 顺序,合起来决定了"链式 `.layer()` 先加的在外层"。这是 P1-04 `ServiceBuilder` 的语义根,本章钉死后,P1-04 直接用结论。这两个顺序是 Tower 最容易翻车的细节之一,写自定义 Layer / 自己组装 Stack 时,务必想清楚"这个 Layer 进 inner 位还是 outer 位、apply 顺序是什么、最终在洋葱的哪一层"。

### 4.4 装饰顺序 = 执行顺序:poll_ready / call 都按洋葱穿

讲完了类型层面的"谁在外谁在内",再来看运行时——请求实际穿过洋葱时,谁先 `poll_ready` / `call`?答案和装饰顺序一致。

假设最终类型是 `Timeout<Retry<my_svc>>`(先 `.timeout()` 在外、后 `.retry()` 在中):

```
外层 Timeout<inner>
  └─ inner: Retry<inner'>
       └─ inner': my_svc(业务)
```

一次请求进来:

**poll_ready 阶段**(由外向内穿透):

```rust
// Timeout::poll_ready(简化,P1-02 讲过中间件 poll_ready 通常透传)
fn poll_ready(&mut self, cx: &mut Context<'_>) -> Poll<Result<(), Self::Error>> {
    self.inner.poll_ready(cx)        // 转给 Retry
}
// Retry::poll_ready 也透传给 my_svc
```

调用方调 `timeout_svc.poll_ready(cx)` → `Timeout` 调 `self.inner.poll_ready` 即 `retry_svc.poll_ready` → `Retry` 调 `self.inner.poll_ready` 即 `my_svc.poll_ready` → `my_svc` 返回 Ready / Pending。背压从最内层 `my_svc` 一路透传到最外层调用方。如果 `my_svc` 满载返回 Pending,整条链都 Pending;如果 `my_svc` Ready,整条链 Ready。

**call 阶段**(也是由外向内):

```rust
// 调用方
let fut = timeout_svc.call(req);
// Timeout::call(简化,P0-01 拆过)
fn call(&mut self, req: Request) -> Self::Future {
    let fut = self.inner.call(req);          // 转给 Retry
    let sleep = tokio::time::sleep(self.timeout);
    Box::pin(async move {
        tokio::select! {
            res = fut => res,
            _ = sleep => Err(Expired),
        }
    })
}
// Retry::call 也调 self.inner.call(req) 转给 my_svc,可能重试多次
```

调用方 `call(req)` → `Timeout::call` 调 `self.inner.call` 即 `Retry::call` → `Retry::call` 调 `self.inner.call` 即 `my_svc.call`,拿到 Future。然后每层的 Future 在外面包自己的逻辑(Timeout 包 select! 超时、Retry 包重试)。

**响应 resolve 阶段**(由内向外):`my_svc` 的 Future 先 resolve → `Retry` 的 Future 拿到结果决定是否重试 → `Timeout` 的 Future 拿到结果(或 sleep 先到返回 Expired)→ 调用方拿到最终 response。

把这套时序画出来(承接 P1-04 的同款时序图,这里换个具体例子):

```mermaid
sequenceDiagram
    autonumber
    participant Caller as 调用方
    participant T as Timeout&lt;Retry&lt;MySvc&gt;&gt;<br/>(外层,先加)
    participant R as Retry&lt;MySvc&gt;<br/>(中间,后加)
    participant M as MySvc<br/>(内层,业务)

    Note over Caller,M: poll_ready 由外向内(背压由内向外)
    Caller->>T: poll_ready(cx)
    T->>R: poll_ready(cx)
    R->>M: poll_ready(cx)
    M-->>R: Ready(Ok)
    R-->>T: Ready(Ok)
    T-->>Caller: Ready(Ok)

    Note over Caller,M: call 由外向内
    Caller->>T: call(req)
    T->>R: call(req)[同时启动 sleep 计时]
    R->>M: call(req)
    M-->>R: 返回 Future
    R-->>T: 返回 Future(可能含重试逻辑)
    T-->>Caller: 返回 Future(含 select! 超时)

    Note over Caller,M: 响应 resolve 由内向外
    M-->>R: response(或 err)
    R-->>T: response(可能重试过)
    T-->>Caller: response(或 Expired)
```

**关键观察**:

- **`poll_ready` 由外向内穿透,背压由内向外传染**。最内层 `my_svc` 满载,整条链 Pending。这是 P1-02 讲过的背压语义在多层洋葱里的自然传播。
- **`call` 由外向内**。每层 Service 的 `call` 调内层的 `call`,请求层层下传。
- **响应 Future 由内向外 resolve**。最内层 Future 先完成,外层 Future 拿到结果再完成。

**所以装饰顺序(谁包谁) = 执行顺序(poll_ready / call / resolve 谁先)**。这三件事在 Tower 里是同一件事的三种描述:

| 描述 | 例子(Timeout 在外、Retry 在中) |
|---|---|
| 类型嵌套(装饰顺序) | `Timeout<Retry<MySvc>>` |
| `poll_ready` 穿透顺序 | Timeout 先 poll_ready,转给 Retry,转给 MySvc |
| `call` 穿透顺序 | Timeout 先 call,转给 Retry,转给 MySvc |
| 响应 resolve 顺序 | MySvc 先 resolve,Retry 后 resolve,Timeout 最后 |

口诀:**"链式从上往下写 = 类型从外往里嵌 = 请求从外往里穿"**。三者一致。

### 4.5 顺序错了语义就变:Timeout 在外 vs Retry 在外

这个对照太重要,单独列个表钉死(承接 P1-04 1.4 节的同款对照):

| 链式写法 | 最终类型 | 谁在外层 | 超时语义 | 重试语义 |
|---|---|---|---|---|
| `.timeout(1s).retry(p).service(svc)` | `Timeout<Retry<MySvc>>` | Timeout 在外 | **总超时 1 秒**(整个重试链路共享一个 sleep,1 秒到了整条链砍掉) | 失败就重试,直到总超时砍掉整条链 |
| `.retry(p).timeout(1s).service(svc)` | `Retry<Timeout<MySvc>>` | Retry 在外 | **每次重试各自 1 秒**(每次 call 新启 sleep,单次 1 秒超时算一次失败) | 单次 1 秒超时算一次失败,失败就重试,可能跑 N × 1 秒 |

两种写法只差一个顺序,语义天差地别。前者是"总超时 1 秒",后者是"每次重试 1 秒"(可能跑 N 秒)。生产事故的真实来源:本想"总超时 1 秒"写成了 `.retry().timeout()`,一个慢请求重试 3 次实际跑了 3 秒,p99 飙升。

**Tower 0.5 不会拦你写错顺序**——编译器只看类型对不对,不看语义合不合理。所以中间件顺序是 code review 必查项,本书附录 B 有排查清单。P1-04 ServiceBuilder 的源码 doctest(`tower/src/builder/mod.rs#L36-L49`)有一句白纸黑字的提示,我们也贴过来印证:

> In the above example, the buffer layer receives the request first followed by concurrency_limit.

先 `.buffer()` 的在最外层,请求先经过 buffer,再经过 concurrency_limit。源码注释 L26-27 还有一句更直白的:**"Layers that are added first will be called with the request first."**(先加的 Layer 先收到请求。)——这正是我们这一节钉死的结论。

> **钉死这件事**:Layer 添加顺序 = Stack 嵌套顺序 = `poll_ready` / `call` 的穿透顺序,这三者在 Tower 里是同一件事。口诀:**"链式从上往下写 = 请求从外往里穿"**。写错顺序语义就变,编译器不拦,所以中间件顺序是生产 code review 的必查项。这条规则由 `Stack::new(inner, outer)` 的参数顺序 + `Stack::layer` "先 inner 后 outer" 的 apply 顺序共同决定,本章钉死,P1-04 直接用。

---

## 第五节:Identity、LayerFn、元组 Layer——零开销的组合原语

讲完 Stack 这个招牌,补三个"零开销原语":`Identity`(空 Layer)、`LayerFn`(闭包当 Layer)、元组 Layer。它们让最简情况也好写,且各有妙用。

### 5.1 Identity:Layer 世界的单位元

`tower-layer/src/identity.rs` 全文 38 行,核心是:

```rust
// tower-layer/src/identity.rs#L10-L31(逐字摘录)
#[derive(Default, Clone)]
pub struct Identity {
    _p: (),
}

impl Identity {
    /// Create a new [`Identity`] value
    pub const fn new() -> Identity {
        Identity { _p: () }
    }
}

impl<S> Layer<S> for Identity {
    type Service = S;

    fn layer(&self, inner: S) -> Self::Service {
        inner
    }
}
```

三个关键点:

**第一,字段 `_p: ()`——零字节**。`Identity` 这个 struct 只有一个字段,类型是单元类型 `()`(零大小类型 ZST,Zero-Sized Type)。`Identity::new()` 创建的值在内存里占 0 字节(实际上是编译期常量,根本不占运行期内存)。`#[derive(Default, Clone)]` 给它免费派生了 `Default`(可以 `Identity::default()`)和 `Clone`(可以 `Identity.clone()`,虽然克隆 ZST 也是 ZST)。

**第二,`impl<S> Layer<S> for Identity` 的 `layer` 直接返回 `inner`**。`type Service = S`,装饰结果类型就是原 Service 类型;`fn layer(&self, inner: S) -> Self::Service { inner }`,什么也不做,把 `inner` 原样返回。这是"什么都不装饰"的 Layer。

**第三,它是 Layer 世界的"单位元"**。数学里,单位元 e 满足 `e ∘ x = x ∘ e = x`(对任意 x)。`Identity` 是 Layer 的单位元——`Identity.layer(svc)` 返回 `svc`(不动),`stack.layer(svc)` 和 `(stack ∘ Identity).layer(svc)` 结果一样(只要 Identity 在合适位置)。这让它成为 Stack 嵌套的"终止符":

```
Stack<A, Stack<B, Stack<C, Identity>>>
                              ↑
                       Identity 是这棵嵌套树的叶子
```

没有 Identity,Stack 嵌套到最后一层就没法收尾(你得用 `Option<Layer>` 或者写两个不同的 Stack 类型,丑且慢)。有了 Identity,嵌套树总有一个"什么也不做"的叶子,递归 apply 自然终止。

**用途**:`ServiceBuilder::new()` 内部就用 `Identity::new()`(`tower/src/builder/mod.rs#L110-L114`),表示"还没加任何 Layer"。每次 `.layer(T)` 把 `Identity` 替换成 `Stack<T, Identity>`,再加一层变成 `Stack<U, Stack<T, Identity>>>`……最终 `.service(svc)` 时,Identity 在递归 apply 里被碰到,直接返回 inner,什么也不做。

> **承接数学**:Identity 是 Layer 幺半群的单位元。Layer 的组合(`Stack`)构成一个幺半群(monoid):有单位元(Identity)、有二元操作(Stack 嵌套)、满足结合律(`Stack<A, Stack<B, C>>` 和 `Stack<Stack<A, B>, C>` 在 apply 后等价)。这种"类型构成代数结构"的现象在 Rust 类型级编程里很常见(`Option` 是 Option 幺半群、`Result` 是 Result 幺半群),Tower 的 Layer 是 Service 装饰的幺半群。理解这一点,你就能解释为什么 Stack 设计得这么"代数化"——它在表达一个代数结构。

### 5.2 LayerFn:把闭包当 Layer

`tower-layer/src/layer_fn.rs` 全文 115 行(含测试),核心是:

```rust
// tower-layer/src/layer_fn.rs#L67-L86(逐字摘录)
pub fn layer_fn<T>(f: T) -> LayerFn<T> {
    LayerFn { f }
}

/// A `Layer` implemented by a closure. See the docs for [`layer_fn`] for more details.
#[derive(Clone, Copy)]
pub struct LayerFn<F> {
    f: F,
}

impl<F, S, Out> Layer<S> for LayerFn<F>
where
    F: Fn(S) -> Out,
{
    type Service = Out;

    fn layer(&self, inner: S) -> Self::Service {
        (self.f)(inner)
    }
}
```

`LayerFn<F>` 是 newtype 模式——把一个闭包 `F: Fn(S) -> Out` 包成一个 struct,然后给这个 struct 实现 `Layer`。`layer_fn(f)` 是构造函数,把闭包 `f` 包成 `LayerFn<F>`。

**用法**:不想写一个 struct + impl Layer,只想用闭包当 Layer,就用 `layer_fn`。文档示例(`tower-layer/src/layer_fn.rs#L48-L63`)给了一个完整例子:

```rust
// 文档示例(摘录)
let log_layer = layer_fn(|service| {
    LogService {
        service,
        target: "tower-docs",
    }
});

let wrapped_service = log_layer.layer(uppercase_service);
```

`log_layer` 是个 `LayerFn<closure>`,它实现了 `Layer`(因为闭包是 `Fn(S) -> Out`)。`log_layer.layer(svc)` 调闭包,返回 `LogService`。这比写一个 `LogLayer` struct + impl Layer 简洁很多——适合"Layer 没有配置、只是把 S 包成 LogService"的简单场景。

**`#[derive(Clone, Copy)]`**:`LayerFn` 派生了 `Clone` 和 `Copy`——只要内部闭包 `F` 是 `Clone` / `Copy`(闭包通常是 `Copy`,如果它捕获的都是 `Copy` 数据),`LayerFn` 也是。这让 `LayerFn` 可以像 Layer 配置一样复用 / 复制。

**对照 `service_fn`**:`LayerFn` 是 Layer 版的 `service_fn`(P6-18 详拆 `service_fn`,把闭包当 Service)。两者都是 newtype + blanket impl 的套路——`LayerFn<F>` 把 `Fn(S)->Out` 闭包当 Layer,`ServiceFn<F>` 把 `Fn(Req)->Future` 闭包当 Service。Rust 异步生态的"函数当 trait 实现"惯用法,在这两个地方各用一次。

> **钉死这件事**:`LayerFn` 是 Layer 的"零开销语法糖"——你写 `layer_fn(|svc| LogService { service: svc, target: "..." })`,编译器生成的是 `LayerFn<closure>` 类型,单态化后和手写 `LogLayer` struct + impl Layer 几乎一样快(闭包被单态化、内联)。这是 Rust"用 newtype 把闭包变成 trait 实现"的标准惯用法,既减少了样板代码,又不引入运行期开销。

### 5.3 元组 Layer:把多个 Layer 当一个 Layer 用

`tower-layer/src/tuple.rs` 全文 331 行,给 1 到 16 个 Layer 的元组都实现了 `Layer`。看二元组的例子(`tower-layer/src/tuple.rs#L23-L34`):

```rust
// tower-layer/src/tuple.rs#L23-L34(逐字摘录)
impl<S, L1, L2> Layer<S> for (L1, L2)
where
    L1: Layer<L2::Service>,
    L2: Layer<S>,
{
    type Service = L1::Service;

    fn layer(&self, service: S) -> Self::Service {
        let (l1, l2) = self;
        l1.layer(l2.layer(service))
    }
}
```

`(L1, L2)` 这个二元组也是一个 Layer——它的 `layer` 先让 `l2`(元组右边的 Layer)装饰 `service`,再让 `l1`(元组左边的 Layer)装饰结果。最终类型 `L1::Service`。

**apply 顺序:从右往左**。`(L1, L2).layer(svc)` = `l1.layer(l2.layer(svc))`:

1. `l2.layer(svc)` 先跑(L2 装饰 svc,得 L2 装饰结果)。
2. `l1.layer(...)` 后跑(L1 装饰 L2 的结果,得最终)。

所以 **L2 装饰得内(紧贴 svc)、L1 装饰得外**。这和 `Stack<Inner, Outer>` 的 "inner 先 apply、outer 后 apply,inner 在内、outer 在外" 呼应——元组的"右边对应 Stack 的 inner(先 apply、在内)、左边对应 Stack 的 outer(后 apply、在外)"。

更高元元组用同样的递归模式。三元组(`tower-layer/src/tuple.rs#L36-L48`):

```rust
// tower-layer/src/tuple.rs#L36-L48(逐字摘录)
impl<S, L1, L2, L3> Layer<S> for (L1, L2, L3)
where
    L1: Layer<L2::Service>,
    L2: Layer<L3::Service>,
    L3: Layer<S>,
{
    type Service = L1::Service;

    fn layer(&self, service: S) -> Self::Service {
        let (l1, l2, l3) = self;
        l1.layer((l2, l3).layer(service))
    }
}
```

`(L1, L2, L3).layer(svc)` = `l1.layer((l2, l3).layer(svc))`——把后两个 Layer `(l2, l3)` 当成二元组先装饰 svc,再让 `l1` 装饰结果。这是递归的:任意 N 元元组的 `layer` 都可以拆成"第一个 Layer + (剩余 N-1 个 Layer 组成的元组)"。源码为 1 到 16 元都写了 impl(用宏或手写),覆盖了实际使用中几乎所有元组大小。

**用途**:元组 Layer 让你可以把"一组 Layer"当成一个 Layer 传递。比如某个 API 想接受"一个或多个 Layer",你可以写 `fn build<L: Layer<S>>(layers: L)`,传单个 Layer 或元组 Layer 都行(元组也是 Layer)。这种"元组即 Layer"的设计,在某些 API 设计场景比 Stack 更顺手(比如 axum 某些内部 API 用元组 Layer 表达"路由的多层中间件")。

**对照 Stack**:Stack 和元组 Layer 都是"把多个 Layer 拼成一个"的方式,差别:

| 维度 | `Stack<Inner, Outer>` | `(L1, L2, ..., LN)` |
|---|---|---|
| 类型表达 | 递归嵌套(每个 Stack 两层) | 扁平台账(元组语法) |
| apply 顺序 | inner 先、outer 后(嵌套结构决定) | 右边先、左边后(元组位置决定) |
| 类型签名 | `Stack<A, Stack<B, Stack<C, Identity>>>` | `(A, B, C)` |
| 终止符 | 需要 Identity | 不需要(元组天然有长度) |
| Tower 主线 | ServiceBuilder 用这个 | axum 等场景用这个 |

Tower 的 `ServiceBuilder`(P1-04)内部用 Stack(因为它要"链式加 Layer,每次加一层",Stack 的递归嵌套天然适合),但元组 Layer 也是公开 API,在需要"一次性传一组 Layer"的场景有用。

> **钉死这件事**:Tower 给了两种"把多个 Layer 拼成一个"的原语——`Stack<Inner, Outer>`(递归嵌套,配 Identity 终止)和元组 `(L1, ..., LN)`(扁平台账,无需终止符)。两者 apply 顺序都是"右边 / inner 先 apply、左边 / outer 后 apply",对应"右边 / inner 在内层、左边 / outer 在外层"。ServiceBuilder 用 Stack,某些场景(如 axum 路由)用元组 Layer。两者底层都是类型嵌套 + 单态化,零运行期开销。

---

## 第六节:Layer 在 Tower 生态里的位置

讲完了 Layer trait + Stack + Identity + LayerFn + 元组 Layer,把视角拉高,看 Layer 在 Tower 生态和 Rust 异步栈里的位置。

### 6.1 Layer 是 Tower 双抽象的一半

回到 P0-01 的全书主轴:**执行单元(Service)vs 组合单元(Layer)**。这一章讲的是组合单元那一半:

- **执行单元**(`tower-service`):`Service<Request>` = `poll_ready(&mut self)`(背压)+ `call(&mut self, req) -> Future`(发请求)。它把"处理一个请求"抽象成一个带背压通道的异步执行单元。这是 P1-02 的事。
- **组合单元**(`tower-layer`):`Layer<S>` = `fn layer(&self, inner: S) -> Service`(装饰)。它把"装饰请求处理"抽象成一个 Service 工厂。这是本章 P1-03 的事。

两个 trait 加起来,就是 Tower 的全部地基。后面 17 章,都是在这地基上盖各种中间件:

- **timeout**:`TimeoutLayer` 装饰出 `Timeout<S>`,`Timeout` 的 `call` 用 `tokio::time::sleep` + `select!` 和内层 Future 抢跑(P3-08)。
- **retry**:`RetryLayer` 装饰出 `Retry<S, Policy>`,`Retry` 的 `call` 按 `Policy` 重试,`poll_ready` 检查 `Budget`(P4-11)。
- **buffer**:`BufferLayer` 装饰出 `Buffer<S>`,`Buffer` 的 `call` 把请求经 mpsc 转发给唯一 worker task(P2-05)。
- **concurrency_limit**:`ConcurrencyLimitLayer` 装饰出 `ConcurrencyLimit<S>`,`poll_ready` 里 acquire Semaphore permit(P3-09)。
- **rate_limit**:`RateLimitLayer` 装饰出 `RateLimit<S>`,令牌桶算法 + `tokio::time::Interval` 补充令牌(P3-10)。
- **balance**:`BalanceLayer` 装饰出 `Balance<D, L>`,用 P2C 在多个后端选负载小的(P5-15)。

每一个中间件,都是一个 `Layer`(配置 + 装饰工厂)+ 它包出来的 `Service`(执行单元)。这是 Tower 一切中间件的标准模板。

### 6.2 Layer 是 axum / tonic / reqwest / Pingora 的共同中间件语言

Layer 的"装饰工厂"抽象,让横切关注点可以跨框架复用。这是 Tower 成为 Rust 异步网络栈枢纽的根本:

- **axum**:`Router::layer(L)` 把 Tower Layer 套在路由上。你写的鉴权 / 日志 Layer,axum 直接用。
- **tonic**:`interceptor` 是 Tower Layer 的特化(只拦截 gRPC 请求)。gRPC interceptor 是 Tower Layer 的子集。
- **reqwest**:`ClientBuilder::layer(L)` 套 Tower middleware。HTTP 客户端的"超时 + 重试 + 连接池"用 Tower Layer 表达。
- **Pingora**:proxy filter 基于 Tower Service。proxy 的"鉴权 + 改写 + 限流"用 Tower Layer。

四个框架,都建立在 Tower 的 Service / Layer 之上。你写一个 `TimeoutLayer`(它是个 `Layer`),axum / tonic / reqwest / Pingora 全都能用——因为它实现的是通用的 `Layer<S>`,不绑任何框架。这就是 P0-01 讲过的"集成点的力量":一套中间件,四个框架通用。Layer 是这个集成点的具体形式。

> **钉死这件事**:`tower-layer` 的 `Layer` trait 刻意被钉死在 0.3.3 长期不动(从 2019 至今,7 年),正是因为它是 axum / tonic / reqwest / Pingora 等所有下游框架的共同集成点。breaking change 会震碎整个生态。这种"核心 trait 极简 + 极度稳定 + 生态在稳定核心上长出来"的设计,是 Tower 能成为 Rust 异步网络栈枢纽的根本原因。`tower` 这个大 crate(中间件集合)可以频繁演进(0.4 → 0.5 → 0.5.2),但 `tower-service` / `tower-layer` 这两个核心 trait crate 不动——这是刻意的工程取舍。

### 6.3 演进:Layer trait 从 0.3.3 至今几乎没动

对照 `tower` 主 crate 的频繁演进(0.4 合并子 crate、0.5 trait 化 Budget、0.5.2 加 BoxCloneSyncService),`tower-layer` 这个核心 trait crate 几乎是"凝固的"。从 0.3.3(2019)到 2024 的 0.5.2 发布,`Layer` trait 的签名一行没改:

```rust
// 0.3.3 至 0.5.2,Layer trait 签名一字未改
pub trait Layer<S> {
    type Service;
    fn layer(&self, inner: S) -> Self::Service;
}
```

这不是停滞,而是刻意——核心 trait 是集成点,改它就震碎下游。改的都是"辅助类型":

- `Identity`:从 0.3.x 就有,字段从 `()` 优化成 `_p: ()`(显式 ZST 标记)。
- `Stack`:签名稳定,Debug impl 优化过(`tower-layer/src/stack.rs#L32-L62` 的 Debug 输出特意扁平化,避免嵌套缩进)。
- `LayerFn`:稳定。
- 元组 Layer:稳定。

这种"核心 trait 凝固 + 辅助类型渐进优化"的演进模式,是 Tower 工程哲学的体现——核心稳如磐石,生态在稳定核心上自由演进。读老资料时,出现的 `Layer` trait 签名、`Stack` / `Identity` 用法至今有效,不会过时。

---

## 技巧精解

正文把 Layer trait 的设计动机和 Stack 的类型嵌套讲完了。这一节单独拆透两个最硬核的技巧:**Stack 的递归类型嵌套 + 单态化**,以及 **Identity / LayerFn 的"零开销原语"设计**。两者都配反面对比——朴素实现会撞什么墙。

### 技巧一:Stack 的递归类型嵌套 + 单态化,把洋葱做进类型系统

**它解决什么问题**:多个 Layer 怎么拼成一个能用的整体,且运行期零开销?

**反面对比一:运行期 Vec<Box<dyn Layer>>**。朴素地,一个 builder 可以这么设计:

```rust
// 朴素反面(非 Tower 实现,简化示意)
struct NaiveBuilder {
    layers: Vec<Box<dyn Layer<dyn Any>>>,  // 运行期动态列表
}

impl NaiveBuilder {
    fn layer(&mut self, l: Box<dyn Layer<dyn Any>>) { self.layers.push(l); }

    fn service(&self, svc: impl Service) -> Box<dyn Service> {
        let mut s: Box<dyn Service> = Box::new(svc);
        for layer in &self.layers {
            s = layer.layer(s);  // 运行期循环 + 动态分发
        }
        s
    }
}
```

这个反面有三个问题(第三节末尾讲过,这里收束):

1. **运行期动态分发**:每次 `.service()` 要遍历 `Vec`,每层 `layer()` 是 trait object 的虚函数调用,无法内联。10 层 Layer 就是 10 次虚调用。
2. **类型擦除**:最终返回 `Box<dyn Service>`,丢掉了所有具体类型信息,后续无法静态优化。
3. **`dyn Any` 地狱**:Layer 要包 `dyn Any` 才能塞进异构 Vec,到处是 downcast,既丑又慢。

**Stack 的解法**。看 Stack 的 `Layer` impl(`tower-layer/src/stack.rs#L18-L30`):

```rust
// tower-layer/src/stack.rs#L18-L30(逐字摘录)
impl<S, Inner, Outer> Layer<S> for Stack<Inner, Outer>
where
    Inner: Layer<S>,
    Outer: Layer<Inner::Service>,
{
    type Service = Outer::Service;

    fn layer(&self, service: S) -> Self::Service {
        let inner = self.inner.layer(service);
        self.outer.layer(inner)
    }
}
```

整套机制的妙处:

**第一,递归类型嵌套表达洋葱结构**。`Stack<A, Stack<B, Stack<C, Identity>>>` 这棵类型树本身就是洋葱结构。`Stack` 的 `inner` 字段是"先 apply 的 Layer",`outer` 字段是"后 apply 的 Layer(或更深的嵌套)"。类型签名在编译期就编码了"谁在外谁在内"。

**第二,trait bound 把两层 Layer 的输入 / 输出对上**。`Inner: Layer<S>`(内层装饰 S)、`Outer: Layer<Inner::Service>`(外层装饰内层结果)。这两个 bound 把两层 Layer 串起来,编译器检查类型匹配——你写错 Layer 顺序(比如外层不能接受内层的输出类型),编译期就报错,不用等运行期。

**第三,单态化 + 内联,零运行期开销**。Stack 的 `layer()` 是泛型方法,编译期为每套具体的 `(Inner, Outer, S)` 组合生成专属代码。单态化后,`Stack<A, Stack<B, Stack<C, Identity>>>::layer(svc)` 变成一串直接调用:

```rust
// 单态化后(简化示意,非源码原文)
fn layer(svc: S) -> C<B<A<S>>> {
    let svc_a = A::layer(&a_config, svc);     // 直接调用
    let svc_b = B::layer(&b_config, svc_a);   // 直接调用
    let svc_c = C::layer(&c_config, svc_b);   // 直接调用
    Identity::layer(&identity, svc_c)         // 直接返回 svc_c(ZST,无开销)
}
```

每层 `layer()` 是直接调用,可内联。如果 `A::layer` / `B::layer` / `C::layer` 实现简单(像 LogLayer 就几行),编译器把它们内联进 `layer` 函数,最终生成的机器码和手写嵌套一字不差。

跨语言对照(gRPC / Envoy / Go 运行期链表方案)在第三节已点过、P0-01 第八节有完整对照表,这里不重复。要点是:C++ / Go 因为类型系统表达力不够(或运行期多态是常态),把洋葱做成运行期链表;Rust 用类型嵌套把洋葱做进编译期。同一思想的两种落地。

**为什么 sound**:Stack 的递归类型嵌套不破坏任何 Rust 安全保证。`Stack<Inner, Outer>` 是普通泛型 struct,`Layer` impl 由 trait bound 保证(`Inner: Layer<S>, Outer: Layer<Inner::Service>`),所有权靠 `&self` 借用传递(`layer` 是 `&self`,不消费 Stack),没有 unsafe,没有运行期反射。整个机制的"安全性"完全由 Rust 类型系统在编译期背书。

**为什么类型签名会爆炸,以及 Tower 怎么治**。Stack 套 10 层,类型签名长到没法看:`Stack<A, Stack<B, Stack<C, Stack<D, Stack<E, Stack<F, Stack<G, Stack<H, Stack<I, Stack<J, Identity>>>>>>>>>>`。这是类型级洋葱的代价。Tower 治这个病的方式:

- **链式 API + 类型推导**:`ServiceBuilder::new().a().b().c()...` 写起来顺,`let builder = ...` 不标类型,类型推导把丑类型藏住(局部变量不暴露)。
- **`BoxService` 家族擦除类型**(P6-17):跨函数边界 / 存进 struct / 放进容器时,用 `BoxCloneService<Req, Res, Err>` 把巨大嵌套类型擦成统一类型。代价是引入虚分派。这是"编译期零开销 vs 运行期灵活"的开关,Tower 两个都给你,自己选。

> **钉死这件事**:Stack 的递归类型嵌套 + 单态化,是 Tower 把"洋葱中间件"做进 Rust 类型系统的核心技巧。类型嵌套表达洋葱结构,单态化把洋葱编译成直接调用。代价是类型签名爆炸(被链式 API + 类型推导 + BoxService 治住),收益是零运行期开销。这是 Tower 区别于 gRPC / Envoy / Go 等运行期链表方案的根本特征,也是它"丑陋但快"的根源。

### 技巧二:Identity / LayerFn 的"零开销原语"设计

**它解决什么问题**:最简情况(空 Layer、闭包 Layer)怎么写才不引入开销?

**Identity:Layer 世界的单位元,ZST + 直接返回 inner**。看 Identity 的源码(`tower-layer/src/identity.rs#L10-L31`):

```rust
// tower-layer/src/identity.rs#L10-L31(逐字摘录)
#[derive(Default, Clone)]
pub struct Identity {
    _p: (),
}

impl Identity {
    pub const fn new() -> Identity {
        Identity { _p: () }
    }
}

impl<S> Layer<S> for Identity {
    type Service = S;

    fn layer(&self, inner: S) -> Self::Service {
        inner
    }
}
```

三个零开销设计:

**第一,字段 `_p: ()` 是 ZST**。`Identity` 在内存里占 0 字节。`Identity::new()` 是 `const fn`,编译期求值,运行期根本不分配内存。一个 `Stack<A, Stack<B, Identity>>` 里的 `Identity` 字段占 0 字节,不增加 Stack 的内存大小。

**第二,`layer(&self, inner: S) -> Self::Service { inner }` 直接返回 inner**。没有构造新 Service、没有调用其他方法、没有任何计算——就是把 `inner` 原样返回。单态化后这个方法被编译成"什么都不做"(可能是 zero instruction,或者一个 register move)。

**第三,`impl<S> Layer<S> for Identity` 对任意 S 都成立**。Identity 不挑 Service 类型,任何 Service 它都能"装饰"(实际不装饰)。这让 Identity 能当 Stack 嵌套的终止符,无论嵌套多深、内层是什么类型。

合起来,Identity 在运行期"几乎不存在"——0 字节内存、0 计算开销、单态化后可能被编译器完全优化掉。这是 Layer 世界的"数学单位元"在工程上的完美实现。

**反面对比:用 Option<Layer> 表达"可能没有 Layer"**。朴素地,如果想表达"这个位置可能没 Layer",可以写 `Option<Layer>`:

```rust
// 朴素反面(非 Tower 实现,简化示意)
struct NaiveStack<Inner, Outer> {
    inner: Inner,
    outer: Option<Outer>,  // Option 表达"可能没 outer"
}
```

代价:`Option<Outer>` 多一个 tag 字段(1 字节,但内存对齐可能多 8 字节),每次访问要 `match opt { Some(o) => ..., None => ... }`(运行期分支)。Stack 套 10 层,每层一个 Option 分支,真开销。

Identity 不需要 Option——它就是一个"什么也不做"的具体类型,字段 ZST、方法直接返回、没有分支。Stack 嵌套到 Identity 自然终止,没有 Option、没有 match、没有开销。这是"用类型系统表达'空'而不是用 Option 表达'可能没有'"的标准 Rust 惯用法——`()` / `PhantomData` / `Identity` 都是 ZST 单位元。

**LayerFn:闭包当 Layer,newtype + blanket impl**。看 LayerFn 的源码(`tower-layer/src/layer_fn.rs#L67-L86`):

```rust
// tower-layer/src/layer_fn.rs#L67-L86(逐字摘录)
pub fn layer_fn<T>(f: T) -> LayerFn<T> {
    LayerFn { f }
}

#[derive(Clone, Copy)]
pub struct LayerFn<F> {
    f: F,
}

impl<F, S, Out> Layer<S> for LayerFn<F>
where
    F: Fn(S) -> Out,
{
    type Service = Out;

    fn layer(&self, inner: S) -> Self::Service {
        (self.f)(inner)
    }
}
```

三个零开销设计:

**第一,newtype 模式包闭包**。`LayerFn<F>` 只有一个字段 `f: F`,没有额外开销。newtype 在 Rust 里是 ZST 包裹(如果 F 是 ZST,LayerFn<F> 也是 ZST;否则 LayerFn<F> 大小等于 F)。`#[derive(Clone, Copy)]` 让 LayerFn 跟随 F 的 Clone / Copy。

**第二,blanket impl `impl<F, S, Out> Layer<S> for LayerFn<F> where F: Fn(S) -> Out`**。任何 `Fn(S) -> Out` 闭包(包括函数指针、闭包、实现 Fn 的 struct)都能被包成 Layer。这让 `layer_fn(closure)` 一行就能造一个 Layer,不用写 struct + impl Layer。

**第三,`(self.f)(inner)` 直接调用闭包**。没有虚分派(闭包类型在编译期已知),单态化后被内联成直接代码。如果闭包体简单(像 LogLayer 的 `|service| LogService { service, target }`),内联后和手写 LogLayer struct + impl Layer 几乎一样快。

**反面对比:写 struct + impl Layer 的样板代码**。不用 LayerFn,写一个最简 Layer 要:

```rust
// 不用 LayerFn 的朴素写法(样板代码)
pub struct MyLayer {
    target: &'static str,
}

impl<S> Layer<S> for MyLayer {
    type Service = MyService<S>;
    fn layer(&self, service: S) -> Self::Service {
        MyService { target: self.target, service }
    }
}
```

struct 定义 + impl Layer,8-10 行样板代码。如果 Layer 没有配置(只是把 S 包成 MyService),这 8-10 行全是机械重复。LayerFn 把它压缩到一行:

```rust
// 用 LayerFn 的简洁写法
let my_layer = layer_fn(|service| MyService { service, target: "..." });
```

一行搞定。这是"用 newtype + blanket impl 减少样板代码"的标准 Rust 惯用法。

**为什么 sound**:Identity 和 LayerFn 都不破坏任何 Rust 安全保证。Identity 是 ZST + 直接返回,零运行期副作用;LayerFn 是 newtype + blanket impl,闭包类型在编译期已知,单态化后无虚分派。两者都不用 unsafe,不用运行期反射,完全由 Rust 类型系统背书。

> **承接《Tokio》[[tokio-source-facts]]**:Tokio / 标准库的 Iterator 组合子(`.map(f).filter(p)`)、Future 组合子(`.and_then(f).map(g)`),也是同一套"newtype + blanket impl + 单态化"范式。Tower 的 Identity / LayerFn / Stack 是这套范式在 Layer 域的实例。理解了这一招,你就在 Rust 异步生态的设计模式里看穿了重复结构。

---

## 章末小结

> **回扣主线**:本章服务的是**组合单元**那一面——Layer trait 怎么把"装饰请求处理"抽象成一类一等公民。把整章收束成一句:
>
> **`Layer<S>` 是一个"装饰 Service"的工厂:`layer(&self, inner: S) -> Self::Service`,只持有装饰配置、不持有 Service。多个 Layer 用 `Stack<Inner, Outer>` 在类型系统里编译期嵌套成一棵洋葱树,运行期零开销。`Stack::new(inner, outer)` + `Stack::layer` "先 inner 后 outer" 的 apply 顺序,决定了"链式 `.layer()` 先加的在外层"。`Identity`(ZST 单位元)+ `LayerFn`(闭包当 Layer)+ 元组 Layer 是三个零开销原语,让最简情况也好写。Layer 解耦了"装饰"和"被装饰",让横切关注点能跨协议 / 跨框架 / 跨业务复用——这是 axum / tonic / reqwest / Pingora 都挂在 Tower 上的根本原因。**

这是 Tower 组合单元的全部内核。本章和 P1-02(执行单元 Service)合成第 1 篇的核心两章——Service 是"会变、持资源、有背压"的执行单元,Layer 是"不变、只存配置、造新 Service"的组合单元。两者职责彻底分开,合起来就是 Tower 的双抽象。

下一章 P1-04 把这两个原语工程化:`ServiceBuilder` 用链式 API 把 Layer 攒成 Stack,`ServiceExt` 用 blanket impl 给 Service 发组合子。P1-04 用的所有底层原语(`Layer` / `Stack` / `Identity` / `LayerFn`),本章已经全部钉死。读完 P1-04,Tower 第 1 篇(核心 trait)收束,后面 15 章,都是在这个地基上盖具体的中间件——背压类(Buffer / SpawnReady / LoadShed)、限流超时类(Timeout / ConcurrencyLimit / RateLimit)、韧性类(Retry / Hedge / Reconnect)、路由负载均衡类(Discover / Balance / Steer)、工程化(BoxService / util / 集成)。每一个中间件,都是一个 Layer + 它包出来的 Service,都能用 ServiceBuilder 链式套上去。

### 五个"为什么"清单

1. **为什么需要 Layer 抽象,不能把横切关注点直接写进业务 Service 的 `call`?**
   因为横切关注点(鉴权 / 日志 / 压缩 / 限流)是跨业务、跨协议、跨框架复用的,揉进业务 `call` 会导致业务淹没、无法复用、顺序无法统一调整、跨协议搬不动。Layer 把"装饰 Service"抽象成一类一等公民,装饰和被装饰解耦,横切逻辑写一次,跨框架复用。

2. **为什么 Layer trait 的 `layer` 是 `&self` 不是 `&mut self`?**
   因为 Layer 是"不变的装饰工厂",只持有装饰配置(Duration / Policy / target),配置不可变。Layer 不消费资源(那是 Service 的 `&mut self` + `poll_ready` 的事),不修改内部状态。`&self` 让 Layer 可以 Clone、可以借用、可以复用——同一套装饰配置可以套到多个 Service 上。

3. **为什么 Tower 用类型级 `Stack<Inner, Outer>` 而不是运行期链表(gRPC / Envoy / Go 那样)?**
   因为 Rust 的类型系统能把洋葱结构表达成类型嵌套,编译期单态化后零运行期开销(直接调用 + 可内联)。运行期链表有虚调用 / 闭包间接调用开销。代价是 Stack 套多层后类型签名爆炸,Tower 用链式 API + 类型推导 + BoxService(P6-17)治。这是"编译期零开销 vs 运行期灵活"的取舍,Tower 选前者,把后者作为逃生阀留给你。

4. **为什么 `Stack::new(inner, outer)` 把新 Layer 放 inner 位、旧栈放 outer 位?这导致什么顺序?**
   这是 `ServiceBuilder::layer(T)` 的调用约定 `Stack::new(layer, self.layer)`(新 T 是第一个参数、旧栈是第二个)。结合 Stack 的 `Layer::layer` "先 apply inner 后 apply outer"(`self.inner.layer(svc)` 先跑、`self.outer.layer(...)` 后跑),inner 先 apply 装饰得近(在内层)、outer 后 apply 装饰得远(在外层)。所以**先 `.layer()` 加的 Layer(被推到更深的 outer 子树)apply 越晚、在外层;后 `.layer()` 加的 Layer(在最近一次 inner 位)apply 越早、在内层**。口诀:"链式从上往下写 = 请求从外往里穿"。

5. **为什么 `Identity` 用 `_p: ()` ZST,不用 `Option<Layer>` 表达"可能没 Layer"?**
   因为 Identity 是 Layer 世界的单位元(ZST + `layer(inner)` 直接返回 inner),在 Stack 嵌套树里自然终止递归,零运行期开销。`Option<Layer>` 多一个 tag 字段 + 每次 `match` 分支,Stack 套多层累积真开销。Rust 惯用法是"用类型系统表达'空'而不是用 Option 表达'可能没有'"——`()` / `PhantomData` / `Identity` 都是 ZST 单位元。

### 想继续深入往哪钻

- **源码**:`tower-layer/src/lib.rs`(Layer trait + LogLayer 文档示例,全文 113 行,建议通读)、`tower-layer/src/stack.rs`(Stack 全文 63 行,核心就 30 行,整章最该逐行读的文件)、`tower-layer/src/identity.rs`(Identity 全文 38 行,ZST 单位元的范本)、`tower-layer/src/layer_fn.rs`(LayerFn 全文 115 行,newtype + blanket impl 的范本)、`tower-layer/src/tuple.rs`(元组 Layer,331 行,展示递归 impl 模式)。整个 `tower-layer` crate 一共 5 个源码文件、约 660 行,一个下午能通读,是理解 Tower 组合单元的最佳入口。
- **承接《Tokio》[[tokio-source-facts]]**:Stack 的"递归类型嵌套 + 单态化"和 Tokio 的 Future 组合子(`and_then` / `map`)、标准库 Iterator(`.map().filter()`)是同一套范式。想深究泛型单态化、类型级编程、ZST 设计,看《Tokio》讲 Future 组合子的章节。
- **承接《hyper》[[hyper-series-project]] / [[hyper-source-facts]]**:hyper 的 Service 删了 `poll_ready`(背压挪协议层),所以 hyper 的中间件链在背压语义上和 Tower 不同。hyper P1-02 讲了 Service trait 入门、P1-03 讲了 Tower 中间件链入门(对照 gRPC filter),本章深化 Layer 这一面,不重复 hyper 讲过的 Service 入门。
- **对照《gRPC》[[grpc-source-facts]]**:gRPC C++ filter stack 是运行期链表,Tower Layer 是编译期 Stack,同一思想(洋葱中间件)的两种语言落地。本章第三节、技巧精解都做了对照,P7-20 收束章会双对照讲透。
- **对照《Envoy》[[envoy-source-facts]]**:Envoy filter chain(Network / HTTP,HCM)C++ 运行期组装 + overload manager,Tower 是 Rust 编译期单态化。对照"零成本抽象 vs 运行期灵活"。
- **对照 Go**:Go 的 `func(http.Handler) http.Handler` 闭包链是运行期动态,无背压(靠 GC / channel 兜底)。Tower Layer 是静态编译期 Stack,有 `poll_ready` 背压。P7-20 会展开这个跨语言对照。
- **下一章 P1-04 ServiceBuilder 与 ServiceExt**:本章钉死了 Layer / Stack / Identity / LayerFn / 元组 Layer 这些原语,P1-04 把它们工程化——`ServiceBuilder` 链式 API 把 Layer 攒成 Stack(`.buffer().timeout().retry()` 一行链),`ServiceExt` 用 blanket impl 给 Service 发组合子(`oneshot` / `map_response` / `and_then` / `call_all`)。P1-04 用到的所有底层原语本章已钉死,读 P1-04 时回查本章即可。

### 一句话引出下一章

> Layer 是"装饰 Service"的工厂,Stack 是"把多个工厂拼成一个大工厂"的类型级洋葱。但 `Stack<TimeoutLayer, Stack<RetryLayer, Stack<BufferLayer, Identity>>>` 这种类型手写起来要命,顺序错了语义就变。下一章 **P1-04 ServiceBuilder 与 ServiceExt** 给你两个工具:`ServiceBuilder` 用链式 API(`.buffer(100).timeout(1s).retry(policy).service(svc)`)把 Layer 攒成 Stack,`ServiceExt` 用一句 blanket impl 给所有 Service 发放 `oneshot` / `map_response` / `and_then` 组合子。两者底层就是本章的 Layer / Stack / Identity,工程化包装后用起来像 Iterator 一样顺。

---

> 本章源码引用(tower-layer @ 0.3.3,tower @ tower-0.5.2,commit `7dc533e`):
>
> - [Layer trait 定义 + 文档](../tower/tower-layer/src/lib.rs#L95-L101) —— `fn layer(&self, inner: S) -> Self::Service`。
> - [`impl<'a, T, S> Layer<S> for &'a T` blanket impl(委托给 T)](../tower/tower-layer/src/lib.rs#L103-L112)。
> - [LogLayer / LogService 文档示例(Layer 工厂 + Service 产品)](../tower/tower-layer/src/lib.rs#L42-L87)。
> - [`mod { identity, layer_fn, stack, tuple }` 声明](../tower/tower-layer/src/lib.rs#L19-L22) + [`pub use { Identity, layer_fn, LayerFn, Stack }`](../tower/tower-layer/src/lib.rs#L24-L28)。
> - [Stack<Inner, Outer> 结构体 + Stack::new + Layer impl(整章核心 30 行)](../tower/tower-layer/src/stack.rs#L1-L30)。
> - [Stack 的 Debug impl(扁平化输出,注释解释 outer/inner 顺序)](../tower/tower-layer/src/stack.rs#L32-L62)。
> - [Identity(ZST 单位元,`_p: ()` + `layer(inner) -> inner`)](../tower/tower-layer/src/identity.rs#L10-L31)。
> - [layer_fn + LayerFn(闭包当 Layer,newtype + blanket impl)](../tower/tower-layer/src/layer_fn.rs#L67-L86) + [LayerFn 文档示例](../tower/tower-layer/src/layer_fn.rs#L14-L63)。
> - [元组 Layer(二元组 apply 顺序:右先左后)](../tower/tower-layer/src/tuple.rs#L23-L34) + [三元组递归模式](../tower/tower-layer/src/tuple.rs#L36-L48)。
> - [ServiceBuilder::layer(P1-04 详拆,这里引用其 Stack::new 调用约定)](../tower/tower/src/builder/mod.rs#L125-L130)。
> - [ServiceBuilder::service(fold 终点,触发 Stack 递归 apply)](../tower/tower/src/builder/mod.rs#L484-L494)。
>
> **承接**:Service 基于 `Future` / `Poll`(标准库 `core::future` / `core::task`,承《Tokio》[[tokio-source-facts]],一句带过);hyper 的 `service::Service` 是 tower-service 简化版(删了 `poll_ready`,背压挪协议层,这个对照贯穿全书,详见《hyper》P1-02 / P1-03 [[hyper-series-project]] [[hyper-source-facts]]);跨语言对照 gRPC filter stack([[grpc-source-facts]])、Envoy filter chain([[envoy-source-facts]])、Go middleware。
>
> **源码印象核实(写本章前 Grep / Read 复核)**:
>
> - **Stack 参数顺序**:核实 `tower-layer/src/stack.rs#L13` 的 `Stack::new(inner: Inner, outer: Outer)`,第一个参数进 `inner` 位、第二个进 `outer` 位。结合 `Layer::layer`(L25-29)"先 `self.inner.layer(service)` 后 `self.outer.layer(inner)`",inner 先 apply(在内层)、outer 后 apply(在外层)。这与 P1-04 1.4 节"先加的在外层、后加的在内层"的结论完全一致——本章推演后确认对齐。
> - **Identity 字段**:`_p: ()`(不是 `()` 也不是 `Option<()>`),ZST,`#[derive(Default, Clone)]`。
> - **LayerFn blanket impl**:`impl<F, S, Out> Layer<S> for LayerFn<F> where F: Fn(S) -> Out`(返回类型是 `Out`,不是关联类型的复杂形式),`#[derive(Clone, Copy)]`。
> - **元组 Layer apply 顺序**:二元组 `(L1, L2).layer(svc) = l1.layer(l2.layer(svc))`,L2(右)先 apply 在内、L1(左)后 apply 在外。三元组递归:`(L1, L2, L3).layer(svc) = l1.layer((l2, l3).layer(svc))`。源码为 1 到 16 元都写了 impl。
> - **Layer trait 自 0.3.3 至 0.5.2 签名一字未改**(L95-L101),核心 trait 凝固是刻意的工程取舍。
