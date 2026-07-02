# 第 1 篇 · 第 4 章 · Body as Stream:请求体怎么"流"进来,又怎么"流"出去

> **核心问题**:前两章我们把"处理一个 HTTP 请求"抽象成了一个 `Service = Fn(Request) -> Future<Output=Response>`,把横切关注点梳成了中间件链。可一个 HTTP 请求不止有头部——它还有 **body**。一个文件上传的 body 可能几个 G,一个视频流式响应可能永远不结束,一个 gRPC trailer 要在 body 末尾才送达。如果 hyper 像新手想的那样"先把整个 body 读进一个 `Vec<u8>` 再交给 Service",那 1 万个上传请求就能把内存撑爆,流式响应干脆没法做。所以 body 必须"流"——边到边走,边读边处理。问题是:**怎么让 body 既能流式,又不丢字节,又能让协议机知道 body 何时结束?** HTTP/1 的 `Content-Length` 已知长度和 `Transfer-Encoding: chunked` 未知长度,两条解码路径有什么本质差别?HTTP/2 多路复用下 body 又长什么样?以及——hyper 1.0 为什么要把 Body 整个推倒重做,从老的"Chunk / Stream 二选一"变成今天这套 `Frame`-based 的东西?

> **读完本章你会明白**:
> 1. 为什么 body 必须是一个**异步 Stream**而非一次性 buffer,以及 hyper 用什么抽象来表达它(答案:不是 `tokio::stream::Stream`,而是 `http_body::Body`,一个 `poll_frame` 出 `Frame` 的 trait)。
> 2. 为什么 hyper 不直接复用 Tokio 的 `Stream`,而要自己定义一个"能流出 **data 帧和 trailers 帧**"的 `Body` trait——这是 hyper 1.0 body 重做的核心动机。
> 3. `Incoming` 这个具体类型(hyper 收 body 用的"接收流")内部长什么样:`Empty` / `Chan`(HTTP/1)/ `H2`(HTTP/2)三种 `Kind`,各自怎么把字节变成 `Frame<Bytes>`。
> 4. 长度已知(`Content-Length`)/ 未知(`chunked` / `close-delimited` / HTTP/2 END_STREAM)四条解码路径的差别,以及 hyper 用一个 `DecodedLength` 把它们全塞进一个 `u64` 的精妙编码。
> 5. 为什么这套设计是 **sound** 的:流式 body 怎么保证不丢字节(背压 + channel 容量 0)、长度怎么跟踪(`sub_if`)、chunked 边界怎么定(逐字节状态机)、协议机和 `Body` trait 怎么用 `Frame` 这一个类型无缝对接。

> **如果一读觉得太难**:先只记住三件事——① body 是一个能 `poll_frame` 出 `Frame` 的流,`Frame` 要么是 `data`(数据字节)要么是 `trailers`(尾部头部);② hyper 收 body 用 `Incoming`,内部按 HTTP/1 还是 HTTP/2 分 `Chan` / `H2` 两套;③ 长度已知 vs 未知,hyper 用 `DecodedLength` 这一个 `u64` 编码(`CHUNKED` / `CLOSE_DELIMITED` 占两个哨兵值)。

---

## 〇、一句话点破

> **HTTP 的 body 不是"一段数据",而是"一串帧"——绝大多数帧是 data,最后一帧可能是 trailers。hyper 的 Body 就是"能异步 `poll_frame` 出这些帧"的 trait;hyper 收 body 时,协议机把字节切成 `Frame` 喂给一个 channel,`Incoming` 从 channel 里 `poll_frame` 出来给你;发 body 时,你的 `Service` 返回的 `Response<Body>` 被 dispatcher 反过来 `poll_frame` 出 `Frame`,再由协议机编回字节写出去。`Frame` 既统一了"数据"和"尾部头部",又让协议机(它本来就只认帧)和 trait(它本来就要异步)无缝对接。**

这是结论。本章倒过来拆:先讲 body 凭什么必须是流、Tokio 的 Stream 给了什么,再讲 hyper 为什么不直接用 Stream 而要造 `Frame`-based 的 `Body`,然后拆 `Incoming` 的三套内部实现和 `DecodedLength` 的长度编码,最后讲发 body 的反向路径和 1.0 重做的根因,辅以两个最硬的技巧。

---

## 一、body 凭什么必须是流,而不是一次性 buffer

### 提出问题

很多人第一次写 Rust Web 服务,会直觉地以为 `Request` 里有个 `body: Vec<u8>` 字段——读完头部顺便把 body 也读完,塞进去,完事。这个想法朴素,但撑不住真实负载。我们来看三个真实场景,逐个论证"body 必须流"。

**场景一:大文件上传。** 一个用户上传 4GB 的视频到你用 hyper 写的 server。如果 hyper 把整个 body 读进一个 `Vec<u8>`,那这一个请求就要吃 4GB 内存。100 个并发上传,400GB——服务器当场 OOM。真实需求是:每读进来一块(比如 8KB),就立刻转发给对象存储 / 写到磁盘 / 算个哈希,**从不让整个文件同时在内存里**。

**场景二:流式响应。** 你写一个直播推流的代理,后端源源不断吐 HLS 分片。这个响应"没有尽头"(或者至少,结束时刻由直播本身决定,不由一个预先声明的长度决定)。如果 body 必须是 `Vec<u8>`,你要么先把整个直播录完才能发响应(荒谬),要么得另搞一套"流式响应"的特殊 API(把简单问题复杂化)。

**场景三:trailer 头部。** gRPC 在 body 流完之后,还要在尾部带一个 trailer(放最终的状态码 `grpc-status` 和消息)。如果 body 是 `Vec<u8>`,那 trailer 要么得另开一个字段(破坏 HTTP 的"body 之后还有 trailer"的顺序),要么干脆表达不出来。

这三个场景合在一起,逼出一个结论:**body 是一个"按需、增量、可以带尾注"的流,不是一块预成的 buffer。**

### Tokio 给了什么(Stream),不这样会怎样

异步世界里"增量产出值"的标准抽象是 **Stream**(对应同步世界里的 `Iterator`)。`Stream` 的核心是一个 `poll_next`:

```rust
// 简化示意,非源码原文(标准 trait,在 futures_core / tokio 里)
trait Stream {
    type Item;
    fn poll_next(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Option<Self::Item>>;
}
```

一次 `poll_next`,要么 `Ready(Some(item))`(有一个新值),要么 `Ready(None)`(流结束),要么 `Pending`(暂时没值,等会儿再来唤醒我)。这就是"按需、增量"的化身——消费者想要一个值就 poll 一次,不想 poll 就不 poll(背压自然产生);流没值就让出线程,值来了 reactor 唤醒。

> **承接《Tokio》**:Stream 的 poll 模型、`Poll::Pending` + `Waker` 的挂起/唤醒、`Pin` 的自引用安全——这些在《Tokio 设计与实现》已拆到源码级。本书**一句带过,不重讲**。我们只关心 hyper 在 Stream 模型之上做了什么取舍。

那么,直接用 `Stream<Item = Bytes>` 当 body 行不行?——表面上行,真用起来会撞墙。这就是 hyper 1.0 要重做 body 的根因,我们留到第三节专门拆透。先记住结论:**裸 `Stream<Item = Bytes>` 只能流出 data 字节,表达不出 trailer。**

### 所以 hyper 这么设计:Body 是"能流出 Frame 的流"

hyper(以及整个 `http-body` 生态,hyper 1.0 之后把 trait 提到了独立的 `http-body` crate)定义了一个自己的 trait,叫 `Body`:

```rust
// 源码在 http-body crate(外部依赖,不在 hyper 仓),hyper 通过 pub use 复用
// 见 hyper/src/body/mod.rs:23-25 的 pub use http_body::{Body, Frame, SizeHint}
pub trait Body {
    type Data: Buf;
    type Error;

    // 唯一必须实现的方法
    fn poll_frame(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
    ) -> Poll<Option<Result<Frame<Self::Data>, Self::Error>>>;

    // 提供默认实现,可覆盖
    fn is_end_stream(&self) -> bool { /* ... */ }
    fn size_hint(&self) -> SizeHint { /* ... */ }
}
```

(`Body` / `Frame` / `SizeHint` 都来自外部 `http-body` crate,hyper 在 [`src/body/mod.rs`](../hyper/src/body/mod.rs#L22-L26) 里 `pub use http_body::{Body, Frame, SizeHint}` 把它们再导出。本书引用其用法,不在 hyper 仓里编行号。)

注意它和 `Stream` 的**两点关键差别**:

1. **`poll_next` 变成了 `poll_frame`,产出的不是裸 `Item`,而是 `Frame<Self::Data>`。** `Frame` 是一个能区分"data 帧"和"trailers 帧"的类型(下一节拆透)。这一改,流就能在末尾带 trailer 了。
2. **多了 `is_end_stream` 和 `size_hint` 两个 hint。** `is_end_stream` 让协议机提前知道"这个 body 是不是已经到头了"(不用非得 poll 一次才知道),`size_hint` 让协议机在写响应头时能决定到底写 `Content-Length` 还是 `Transfer-Encoding: chunked`(下一节讲透)。

> **钉死这件事**:`Body` trait = `Stream` 的"按需增量"语义 + "帧不止 data 一种"的扩展 + "长度/结束 hint"。它不是凭空造的轮子,是 `Stream<Item=Bytes>` 之上加了两层真实需求(多帧类型 + 长度信息)。

> **承接《Tokio》**:`Body` 的 poll 模型完全继承自 `Stream`(继承自 `Future`)。`Poll::Pending` 让出线程、`Waker` 唤醒、`Pin` 保证自引用安全——这些底下全是 Tokio 那一套。本书篇幅全留"hyper 为什么多出 Frame/SizeHint、body 怎么和协议机配合",不重讲 poll 机制。

### 源码佐证:hyper 怎么 import 和复用

hyper 自己**不定义** `Body` / `Frame` / `SizeHint` 这三个,全从 `http-body` crate 复用。看 [`src/body/mod.rs`](../hyper/src/body/mod.rs#L1-L42) 开头的 module 文档和 re-export:

```rust
//! Streaming bodies for Requests and Responses.
//!
//! For both [Clients](crate::client) and [Servers](crate::server), requests and
//! responses use streaming bodies, instead of complete buffering. This
//! allows applications to not use memory they don't need, and allows exerting
//! back-pressure on connections by only reading when asked.
//!
//! There are two pieces to this in hyper:
//!
//! - **The [`Body`] trait** describes all possible bodies.
//! - **The [`Incoming`] concrete type**, which is an implementation
//!   of `Body`, and returned by hyper as a "receive stream" ...
pub use bytes::{Buf, Bytes};
pub use http_body::Body;
pub use http_body::Frame;
pub use http_body::SizeHint;

pub use self::incoming::Incoming;
```

这段文档把 hyper 的 body 哲学一句话讲完了:**"streaming bodies, instead of complete buffering. This allows applications to not use memory they don't need, and allows exerting back-pressure on connections by only reading when asked."** 三个关键词:**streaming**(流式)、**not use memory they don't need**(不预占内存)、**back-pressure by only reading when asked**(按需读即背压)。

> **钉死这件事**:body = streaming + 不预占内存 + 按需读即背压。这三条是 hyper body 设计的总纲,后续每一处实现(为什么 channel 容量是 0、为什么 `poll_ready` 要先等消费者 poll 过、为什么 `size_hint` 决定编码)都是这三条的展开。

模块文件本身非常薄(50 行不到),只做 re-export 和 Send/Sync 的编译期断言:

```rust
// 见 src/body/mod.rs:44-50
fn _assert_send_sync() {
    fn _assert_send<T: Send>() {}
    fn _assert_sync<T: Sync>() {}

    _assert_send::<Incoming>();
    _assert_sync::<Incoming>();
}
```

这个 `_assert_send_sync` 是 Rust 源码里常见的"编译期保险丝":如果哪天有人改 `Incoming` 改得它不再是 `Send + Sync`,这个函数立即编译失败。`Incoming` 必须跨 task(跨 await 点)传递,必须 `Send + Sync`,这是它作为 body 类型的硬约束。

---

## 二、为什么是 Frame,不是裸 Stream:hyper 1.0 重做 body 的根因

这一节是本章的灵魂。把它看懂,你就懂了 hyper 1.0 为什么非要把 body 推倒重做。

### 提出问题:裸 `Stream<Item = Bytes>` 到底缺了什么

假设 hyper 0.14(1.0 之前)就直接用 `Stream<Item = Result<Bytes, Error>>` 当 body。看起来很美:`Stream` 是 Tokio 标配,生态里全是 combinator(`map` / `filter` / `chunks`),用户上手零成本。可一旦你要把这种 body 真的接到 HTTP 协议机上,会撞三堵墙。

**第一堵墙:trailer 没地方放。** HTTP 协议明确规定:body 之后**可以**跟一组 trailer 头部(HTTP/1 用 chunked + `Trailer:` 头声明,HTTP/2 用 HEADERS 帧跟在 DATA 帧后面)。gRPC、一些 RPC 框架重度依赖 trailer(放最终状态码)。可 `Stream<Item = Bytes>` 只能流出 `Bytes`——你没法在流的末尾表达"接下来这一坨不是数据,是 trailer"。你可以说"那我 `Stream<Item = BodyChunk>` 其中 `BodyChunk = Data(Bytes) | Trailers(HeaderMap)`"——对,可这不就是你自己造了一个 `Frame` enum 吗?等于把 trait 要解决的问题下推给每个用户。

**第二堵墙:协议机不知道 body 何时结束、有多长。** HTTP 协议机要写响应头,它必须在头部里写**到底用哪种成帧方式**:
- 如果 body 总长度已知,写 `Content-Length: 12345`,对方读完 12345 字节就知道 body 结束。
- 如果长度未知,写 `Transfer-Encoding: chunked`,靠 `0\r\n\r\n` 终止符。
- 如果是 HTTP/1.0 又没长度,只能靠关连接(close-delimited)。

协议机写头部这一刻,**body 一个字节都还没开始流**。所以它得问 body:"你大概多长?"。`Stream<Item=Bytes>` 没有 `size_hint`——你只能"先 poll 一把试试",可 poll 了就得消费第一个 chunk,而头部还没写出去,字节往哪儿放?死锁。

**第三堵墙:提前判断"body 是不是空的"。** 对一个 GET 请求,响应往往是 `204 No Content` 或 `304 Not Modified`,这些响应**根本不能有 body**。协议机收到头部之后、开始流 body 之前,想问一句"这个 body 已经 end 了吗?"——`Stream` 没有这个方法,你又只能 poll。可 poll 一个空 body 的第一个 chunk,语义上又怪又慢(每个空响应都得排一次 task 唤醒)。

### gRPC 怎么做:对照一个"裸字节流"的真实代价

> **对照《gRPC》**:gRPC 的消息体是 protobuf 字节流。在 gRPC 里,trailer 走的是**单独的帧类型**(HTTP/2 HEADERS 帧,带 END_STREAM),DATA 帧只装数据。也就是说 gRPC 的协议层本来就把"data"和"trailer"分得很清楚——它的底层(自实现的 chttp2)天然有"消息帧"的概念。可当一个 gRPC 框架想把"用户业务逻辑"和"协议层"解耦时,它暴露给用户的 body 抽象如果只是字节流,就得自己发明一套"trailer 怎么从用户层传到协议层"的机制(典型的就是 gRPC 的 `Status` trailer 在最后单独 set)。这套机制在 Rust 异步生态里被反复重造,直到 `http-body` 把它标准化成 `Frame`。

**不这样会怎样**:如果 hyper 沿用 `Stream<Item = Bytes>`,每个想发 trailer 的用户都得:① 自己定义一个 enum 包装 data/trailer,② 自己实现 Stream,③ hyper 再加一套适配层把这个 enum 拆回 hyper 认识的 trailer。每个上层框架(axum/tonic/reqwest)都得各自造。一个 trailer 概念,被生态里五六家各重造一遍——这正是 hyper 1.0 要消灭的重复。

### 所以这样设计:Frame = data 帧 ∪ trailers 帧

hyper(连同 `http-body` crate)的解法,是把"body 里能流出的东西"抽象成一个统一的 `Frame<T>` 类型,然后让 `Body::poll_frame` 流出 `Frame<Self::Data>`。

`Frame<T>` 在 `http-body` crate 里是一个 struct(内部封装了一个标记它是 data 还是 trailers 的 kind),对外暴露这些构造和判断方法(来自 docs.rs/http-body):

```rust
// 源码在 http-body crate(外部依赖,不在 hyper 仓)
pub struct Frame<T> { /* private fields */ }

impl<T> Frame<T> {
    pub fn data(data: T) -> Frame<T>;          // 造一个 data 帧
    pub fn trailers(trailers: HeaderMap) -> Frame<T>; // 造一个 trailers 帧
    pub fn map_data<F, U>(self, f: F) -> Frame<U>;    // data 帧的类型变换

    pub fn is_data(&self) -> bool;
    pub fn is_trailers(&self) -> bool;
    pub fn into_data(self) -> Result<T, Frame<T>>;       // 是 data 就拿出 T,否则原样返回
    pub fn into_trailers(self) -> Result<HeaderMap, Frame<T>>;
    // ... 还有 data_ref / data_mut / trailers_ref / trailers_mut
}
```

设计上有几个漂亮的地方:

1. **它是一个 struct 而不是 `enum`,但语义上是 data ∪ trailers。** 内部用 kind 标记。`into_data` / `into_trailers` 返回 `Result<T, Frame<T>>`(失败时把原 Frame 还回来),这是 Rust 里"尝试按某个 variant 拆解"的标准惯用法——和 `Mutex::try_lock` 之类一脉相承。
2. **`map_data` 让 data 帧可以类型变换,而 trailers 帧原样穿过。** 比如 `Frame<Bytes>::map_data(|b| b.chunk())` 得到 `Frame<&[u8]>`。这在写 body 适配器(把一种 Body 变成另一种 Body)时极有用——你只需要变换 data 部分,trailer 照搬。
3. **`Frame<T>` 的 `T` 就是 `Body::Data`。** 所以一个 `Body` 流出的所有 data 帧里装的都是同一种 `Buf` 类型(hyper 的 `Incoming` 用 `Bytes`),trailer 帧统一是 `HeaderMap`,没有泛型泄漏。

> **钉死这件事**:`Body` 流出的是 `Frame`,不是裸 `Bytes`。`Frame` 把"data 和 trailers 是两种不同的东西"这件事,做进了类型系统。一个 body 的合法输出顺序是:零或多个 data 帧 → 可选的一个 trailers 帧 → `None`(流结束)。协议机靠 `is_data()` / `is_trailers()` 分流处理。

### 源码佐证:协议机怎么消费 Frame

抽象好不好,看协议机怎么用。hyper 的 HTTP/1 dispatcher 在写 body 的循环里([`src/proto/h1/dispatch.rs`](../hyper/src/proto/h1/dispatch.rs#L378-L437)),对每个 `poll_frame` 出来的 frame 做**精确的三路分流**:

```rust
// 见 src/proto/h1/dispatch.rs:392-429(简化摘录,省略 OptGuard 和 clear_body)
let item = ready!(body.as_mut().poll_frame(cx));
if let Some(item) = item {
    let frame = item.map_err(|e| { *clear_body = true; crate::Error::new_user_body(e) })?;

    if frame.is_data() {
        let chunk = frame.into_data().unwrap_or_else(|_| unreachable!());
        let eos = body.is_end_stream();
        if eos {
            *clear_body = true;
            if chunk.remaining() == 0 {
                self.conn.end_body()?;
            } else {
                self.conn.write_body_and_end(chunk);
            }
        } else if chunk.remaining() == 0 {
            continue;                                  // 丢弃空 chunk
        } else {
            self.conn.write_body(chunk);
        }
    } else if frame.is_trailers() {
        *clear_body = true;
        self.conn.write_trailers(
            frame.into_trailers().unwrap_or_else(|_| unreachable!()),
        );
    } else {
        trace!("discarding unknown frame");            // 防御性:未来帧类型
        continue;
    }
} else {
    *clear_body = true;
    self.conn.end_body()?;                             // 流结束:补终止符
}
```

把这段读出声来,你就看懂了 `Frame` 设计的全部价值:

- **`frame.is_data()` → 走数据写入路径**(`write_body` / `write_body_and_end`),用 H1 的 Encoder 把字节编出去。
- **`frame.is_trailers()` → 走 trailer 写入路径**(`write_trailers`),H1 下编成 chunked 的终止 chunk + trailer 头。
- **都不是(未来帧类型)→ 防御性丢弃**。这个 `else` 分支是给 `http-body` 未来可能扩展第三种帧留的逃生阀——今天的 hyper 不认,但不会 panic。
- **`None`(流结束)→ `end_body`** 补上 chunked 的 `0\r\n\r\n` 或 length 的对齐收尾。

**注意这段循环里没有一个字节是"先把整个 body 读进来"的**。每 `poll_frame` 出一帧,立刻编成字节写出去(写到 `self.io` 的 buffer,buffer 满了再 flush)。这是真正的边到边流式:用户的 `Service` 在异步产出帧,协议机在异步消费帧,中间只隔一个 `poll_frame` 调用,没有全量 buffer。

> **钉死这件事**:看 [`dispatch.rs:392-429`](../hyper/src/proto/h1/dispatch.rs#L392-L429) 这段——`Body` trait 的 `Frame` 设计,**直接对应协议机的一个 `match`**。`is_data` / `is_trailers` / `None` 三路分流,就是 `Frame` 的 data/trailers/end 三态。trait 的形状是由协议机倒推出来的,这是"框架适配协议"的最佳例证。

---

## 三、Incoming:hyper 收 body 的"接收流"

`Body` 是 trait(描述所有可能的 body),用户写的 `Service` 返回的 `Response<B>` 里那个 `B` 是用户自己挑的(可以是 `Full<Bytes>`、`Empty`、自定义)。但当 hyper **自己**作为 server 收请求、或作为 client 收响应时,它要给你一个**具体的 body 类型**——这个类型叫 `Incoming`。这是 hyper 内部最重要的 body 实现,也是本章最值得拆透的东西。

### 提出问题:一个类型怎么同时表达 HTTP/1 和 HTTP/2 的 body

`Incoming` 是 hyper 在 `Request<Incoming>`(server 收到的请求)和 `Response<Incoming>`(client 收到的响应)里塞的那个 body。它要应对三种来源:

1. **空 body**(`GET` 请求、`204` 响应、HEAD 响应等)。
2. **HTTP/1 的 body**——字节从连接读进来,被 H1 Decoder 切成 chunk,要喂给消费者。
3. **HTTP/2 的 body**——h2 crate 已经帮你把帧解好了,给你一个 `h2::RecvStream`,你 poll 它就有 data。

HTTP/1 和 HTTP/2 的 body 来源**完全不同**:H1 是自己从字节流里切(Decoder),H2 是 h2 已经切好给你(RecvStream)。一个 `Incoming` 类型怎么同时容纳这三种?

### Tokio / gRPC 怎么做(参照)

Tokio 的世界里有现成的"消费端/生产端分离"模式:**channel**。生产端塞值,消费端 poll 值,中间一个有界队列做背压。HTTP/1 的 body 完全契合这个模式——协议机是生产端(读到字节就塞),`Incoming` 是消费端(用户 poll 就拿)。所以 hyper 给 HTTP/1 的 body 用了一个 `mpsc::channel` 桥接。

HTTP/2 不一样。h2 crate 本身就提供了一个 `RecvStream`(它内部已经是 poll-based 的流),没必要再套一层 channel——直接把 `RecvStream` 包进 `Incoming` 里就行。所以 hyper 给 HTTP/2 的 body 用的是**直接包装**。

### 所以这样设计:Incoming 内部用 enum 分 Kind

看 [`src/body/incoming.rs`](../hyper/src/body/incoming.rs#L52-L74) 里 `Incoming` 的真实定义:

```rust
// 见 src/body/incoming.rs:52-74
#[must_use = "streams do nothing unless polled"]
pub struct Incoming {
    kind: Kind,
}

enum Kind {
    Empty,
    #[cfg(all(feature = "http1", any(feature = "client", feature = "server")))]
    Chan {
        content_length: DecodedLength,
        want_tx: watch::Sender,
        data_rx: mpsc::Receiver<Result<Bytes, crate::Error>>,
        trailers_rx: oneshot::Receiver<HeaderMap>,
    },
    #[cfg(all(feature = "http2", any(feature = "client", feature = "server")))]
    H2 {
        content_length: DecodedLength,
        data_done: bool,
        ping: ping::Recorder,
        recv: h2::RecvStream,
    },
    #[cfg(feature = "ffi")]
    Ffi(crate::ffi::UserBody),
}
```

`Incoming` 就是一个 enum 包了一层(`struct Incoming { kind: Kind }`),enum 的三个主 variant 对应三种来源。这是 Rust 里"一个类型表达多种后端"的标准手法(和 `tokio::net::TcpStream` / `UnixStream` 的内部 enum 同理)。逐个看每个 variant 装了什么:

```
┌──────────────────────────────────────────────────────────────────────┐
│                      Incoming { kind: Kind }                          │
├──────────────────────────────────────────────────────────────────────┤
│                                                                       │
│  ┌─── Empty ──────────────────────┐   没有字段,纯标记                  │
│  │   (无 body)                     │   is_end_stream() = true 立即返回  │
│  └─────────────────────────────────┘                                  │
│                                                                       │
│  ┌─── Chan (HTTP/1) ──────────────┐   生产端=H1 协议机(Dispatcher)    │
│  │  content_length: DecodedLength │   消费端=用户(poll_frame)          │
│  │  want_tx: watch::Sender        │   中间靠 mpsc(0)+oneshot 桥接     │
│  │  data_rx: mpsc::Receiver<...>  │   - data 走 mpsc(容量 0,强背压)   │
│  │  trailers_rx: oneshot::Rcv<HM> │   - trailers 走 oneshot(只发一次) │
│  └─────────────────────────────────┘                                  │
│                                                                       │
│  ┌─── H2 (HTTP/2) ────────────────┐   生产端=h2 crate(RecvStream)     │
│  │  content_length: DecodedLength │   消费端=用户(poll_frame)          │
│  │  data_done: bool               │   data_done: data 已流完,转 trailers│
│  │  ping: ping::Recorder          │   ping: 记录流量给流控用(承 P3-11)│
│  │  recv: h2::RecvStream          │   recv: h2 的接收流,直接 poll_data │
│  └─────────────────────────────────┘                                  │
│                                                                       │
└──────────────────────────────────────────────────────────────────────┘
```

> **钉死这件事**:`Incoming` = `enum Kind`(Empty / Chan / H2)+ 一层 newtype。HTTP/1 用 channel 桥(因为协议机自己切字节),HTTP/2 直接包 `h2::RecvStream`(因为 h2 已经切好)。一个类型,两种完全不同的后端,对外统一一个 `poll_frame`。

注意几个字段在每个 variant 里都出现 / 都不出现的精妙:

- **`content_length: DecodedLength` 在 `Chan` 和 `H2` 里都有**(下一节拆透),用来跟踪长度。
- **HTTP/1 的 `Chan` 有 `want_tx`**(watch channel),这是给"背压"用的——消费者没 poll 之前,生产端不许塞。第五节技巧精解拆透。
- **HTTP/2 的 `H2` 有 `ping: Recorder`**——HTTP/2 的流控和 ping 测延迟,需要知道这条 body 流过了多少字节。这是 hyper 在 body 层埋的钩子,承接到《gRPC》和本书 P3-11。
- **HTTP/2 的 `H2` 没有 `trailers_rx`,而是直接从 `recv.poll_trailers(cx)` 拿**——因为 h2 把 data 和 trailers 都装在 RecvStream 里了,一个 poll 对象全管。

### 源码佐证:三种 Kind 各自的 poll_frame

`Incoming` 自己实现了 `Body` trait([`src/body/incoming.rs:189-326`](../hyper/src/body/incoming.rs#L189-L326))。`poll_frame` 内部就是 match `kind` 三路分发。我们逐路看。

#### Empty 路:最简单

```rust
// 见 src/body/incoming.rs:211-212
match self.kind {
    Kind::Empty => Poll::Ready(None),
    ...
}
```

空 body,`poll_frame` 立即返回 `None`(流结束)。没有 data,没有 trailers,一个 `None` 搞定。`is_end_stream` 对应也立即返回 `true`([`incoming.rs:291-301`](../hyper/src/body/incoming.rs#L291-L301))。所以一个 `Empty` body 永远不会让消费者 `await`,poll 一次就完——这是空响应能"零开销"的根。

#### H1 Chan 路:data 先行,trailers 收尾

```rust
// 见 src/body/incoming.rs:213-234
Kind::Chan {
    content_length: ref mut len,
    ref mut data_rx,
    ref mut want_tx,
    ref mut trailers_rx,
} => {
    want_tx.send(WANT_READY);                        // ① 告诉生产端"我想要数据"

    if !data_rx.is_terminated() {                    // ② data 没流完
        if let Some(chunk) = ready!(Pin::new(data_rx).poll_next(cx)?) {
            len.sub_if(chunk.len() as u64);          // ③ 扣减已知长度
            return Poll::Ready(Some(Ok(Frame::data(chunk))));  // ④ 包成 data 帧返回
        }
    }

    // check trailers after data is terminated        ⑤ data 流完了,查 trailers
    match ready!(Pin::new(trailers_rx).poll(cx)) {
        Ok(t) => Poll::Ready(Some(Ok(Frame::trailers(t)))),
        Err(_) => Poll::Ready(None),                 // 没有 trailers(oneshot 被 drop)
    }
}
```

这五步把 HTTP/1 body 的接收语义钉死了:

1. **`want_tx.send(WANT_READY)`**——发个信号给生产端(协议机那边的 `Sender`),"我现在在 poll 了,你可以塞数据"。这是背压机制的核心,第五节细拆。
2. **`data_rx.is_terminated()` 判断**——mpsc 的 `FusedStream` 能力,告诉你这个 channel 是不是已经"终结"(对端 drop 了)。如果没终结,优先 poll data。
3. **`len.sub_if(chunk.len() as u64)`**——每收到一块,从已知长度里扣掉。这一步是"长度跟踪",保证协议机能校验"实际收到的字节数 == 声明的 Content-Length"。下一节拆 `sub_if`。
4. **`Frame::data(chunk)`**——把 raw `Bytes` 包成 data 帧返回。注意 `chunk` 是 `Bytes`(`bytes` crate 的引用计数零拷贝缓冲),这里**没有任何拷贝**。
5. **data 流完后转 trailers**——`oneshot::Receiver<HeaderMap>` 只能收一次,trailers 来了就包成 `Frame::trailers`,没来(对端 drop,说明这个 body 压根没 trailer)就返回 `None` 结束。

**注意 ② 的顺序:先 data 后 trailers,严格遵循 HTTP 协议的帧顺序。** 这是 trait 的隐式契约——一个 body 流出的帧必须是 data* 后 trailers?,协议机靠这个顺序正确编码。`Incoming` 在 trait 层强制保证了这个顺序。

> **钉死这件事**:H1 body 的 `poll_frame` = poll mpsc data 流 + 收完转 oneshot trailers。中间的 mpsc 和 oneshot 是**生产端(协议机)和消费端(用户)之间的桥**。生产端在 dispatcher 那边把字节塞进 mpsc,消费端这边 poll 出来包成 `Frame`。两边的 task 用 channel 解耦,可以各自按自己的节奏跑。

#### H2 路:直接 delegate 给 h2

```rust
// 见 src/body/incoming.rs:235-284
Kind::H2 {
    ref mut data_done,
    ref ping,
    recv: ref mut h2,
    content_length: ref mut len,
} => {
    if !*data_done {                                 // ① data 阶段
        match ready!(h2.poll_data(cx)) {
            Some(Ok(bytes)) => {
                let _ = h2.flow_control().release_capacity(bytes.len());  // ② 释放流控额度
                len.sub_if(bytes.len() as u64);      // ③ 长度跟踪
                ping.record_data(bytes.len());       // ④ 记录流量给 ping/流控
                return Poll::Ready(Some(Ok(Frame::data(bytes))));
            }
            Some(Err(e)) => {
                if let Some(h2::Reason::NO_ERROR) = e.reason() {
                    return Poll::Ready(None);        // ⑤ RST_STREAM NO_ERROR = 提前结束,不算错
                } else {
                    return Poll::Ready(Some(Err(crate::Error::new_body(e))));
                }
            }
            None => {
                *data_done = true;                   // ⑥ data 流完,转 trailers
            }
        }
    }

    // ⑦ data 完了,查 trailers
    match ready!(h2.poll_trailers(cx)) {
        Ok(t) => {
            ping.record_non_data();
            Poll::Ready(Ok(t.map(Frame::trailers)).transpose())
        }
        Err(e) => { /* 同样的 NO_ERROR 处理 */ }
    }
}
```

H2 路的结构和 Chan 路惊人地对称:**data 阶段 → data 流完 → trailers 阶段**。差别在每一步的具体动作:

- ② **`release_capacity`**——HTTP/2 流控的精髓。h2 给每条 stream 一个流量额度窗口,你收了多少字节,得"还"多少额度回去(否则对端没法继续发)。这里收完一块就立刻 release——这是 hyper 在 body 层对 HTTP/2 流控的尊重。承《gRPC》P2-09(BDP/window 已拆透),本书 P3-11 再细拆 hyper 的 ping/流控策略。
- ④ **`ping.record_data`**——记一笔流量给 hyper 的 ping recorder(测 RTT、算 BDP 用)。这是 hyper 在 H2 body 路径上埋的"可观测性钩子"。
- ⑤ **`RST_STREAM NO_ERROR` 当作正常结束**——这是 HTTP/2 的一个细节(承 RFC 7540 §8.1):server 提前返回响应会发 RST_STREAM NO_ERROR,client 收到不该当错,而是当 body 正常结束。hyper 在 body 这一层就替你处理了这个协议怪癖——你上层永远看不到这个"假错误"。

**为什么要分 `data_done` 这个 bool**——因为 h2 的 `RecvStream` 同时管 data 和 trailers,但你不能反复 poll_data(它终究会返回 None 表示 data 结束)。用一个 bool 记"我已经知道 data 结束了",后续 poll_frame 直接跳过 data 阶段、只查 trailers。这是一个微小的状态机优化,避免每次 poll_frame 都先 poll 一次 data 才知道"哦 data 早完了"。

> **钉死这件事**:H2 body 的 `poll_frame` = poll `h2::RecvStream` 的 data + 收完转 trailers + 每步维护流控/ping。HTTP/2 的流控细节(窗口额度、BDP)承《gRPC》,本书 P3-11 续拆。这里只看 hyper 在 body 层怎么尊重这套机制——`release_capacity` 一次都不能漏,漏了连接就死锁。

---

## 四、DecodedLength:用一个 u64 编码四种长度语义

这一节讲透 hyper 怎么跟踪 body 长度。这是"为什么 sound"的关键之一——协议机要校验 body 实际收到的字节数和声明的一致,要正确判断 body 何时结束,要给 `size_hint` 一个准确的答案,全靠这一个看似平凡的 `DecodedLength`。

### 提出问题:body 的"长度"有几种

乍一看 body 长度就是"一个数字"——`Content-Length: 12345`。可真实 HTTP 里"长度"有四种语义:

1. **已知精确长度**:`Content-Length: 12345`,body 就是 12345 字节。
2. **零长度**:没 body,或者 `Content-Length: 0`。
3. **未知长度(chunked)**:`Transfer-Encoding: chunked`,靠 chunk 终止符(`0\r\n\r\n`)定边界,总长度事先不知道。
4. **未知长度(close-delimited)**:HTTP/1.0 响应既没 Content-Length 也没 chunked,只能靠"服务端关连接"定边界。

这四种语义,**协议机要分别用不同的 Decoder/Encoder**:
- 已知长度 → 一个计数器,读到剩 0 为止。
- chunked → 一个逐字节状态机,解析 chunk size 行、chunk data、终止 chunk。
- close-delimited → 一直读到 EOF。
- 零长度 → 直接跳过(没有 body 要读)。

那么,hyper 怎么用**一个类型**把这四种语义全表达出来,还能让 `Incoming` 在每收到一块 data 时正确更新?

### 不这样会怎样(朴素方案)

朴素方案是用 `Option<u64>`:Some 是已知长度,None 是未知。可这表达不出"chunked 未知"和"close-delimited 未知"的差别——而 hyper 的 H1 Encoder 对这两种未知长度的处理**不同**(chunked 编码有终止符,close-delimited 没有,要靠关连接)。所以 Option 不够。

更朴素的方案是搞个 enum:

```rust
// 简化示意,非源码原文
enum LengthNaive {
    Known(u64),
    Chunked,
    CloseDelimited,
}
```

这就对了——但 hyper 没这么写。它用了更紧凑、更"位打编码"的方式。

### 所以这样设计:DecodedLength 用 u64 的两个哨兵值

看 [`src/body/length.rs`](../hyper/src/body/length.rs#L1-L23):

```rust
// 见 src/body/length.rs:3-4
#[derive(Clone, Copy, PartialEq, Eq)]
pub(crate) struct DecodedLength(u64);

impl DecodedLength {
    pub(crate) const CLOSE_DELIMITED: DecodedLength = DecodedLength(u64::MAX);
    pub(crate) const CHUNKED: DecodedLength = DecodedLength(u64::MAX - 1);
    pub(crate) const ZERO: DecodedLength = DecodedLength(0);
    ...
}
```

`DecodedLength` 就是 `u64` 的一层 newtype。但它的取值空间被切成三段:

```
┌────────────────────────────────────────────────────────────────────────┐
│                    DecodedLength(u64) 取值空间                          │
├────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│   0 ──────────── ZERO(body 空,已知长度 0)                              │
│                                                                         │
│   1 ──────┐                                                            │
│           │                                                             │
│   2^64-3  │  真实长度区段:每个 u64 值 = 一个精确已知的字节数            │
│           │  (从协议头里 parse 出的 Content-Length)                     │
│   ────────┘                                                            │
│                                                                         │
│   2^64-2 ─── CHUNKED(Transfer-Encoding: chunked,长度未知)             │
│   2^64-1 ─── CLOSE_DELIMITED(HTTP/1.0 无长度,靠关连接定边界)          │
│                                                                         │
└────────────────────────────────────────────────────────────────────────┘
```

这个设计的精妙之处:

1. **零开销**。`DecodedLength` 就是 `u64`,`Clone + Copy + PartialEq + Eq`,传值就是传一个 u64。和 enum(带 tag,通常 9 字节)比起来更紧凑、对齐更好。
2. **真实长度区段是 `[0, 2^64-3]`**。这意味着 hyper 能接受的最大 Content-Length 是 `u64::MAX - 2`,即 `2^64 - 3` 字节(约 18 EB)——远远超过任何真实场景。看 [`length.rs:18`](../hyper/src/body/length.rs#L18):

   ```rust
   // 见 src/body/length.rs:17-18
   #[cfg(any(feature = "http1", feature = "http2", test))]
   const MAX_LEN: u64 = u64::MAX - 2;
   ```

   这个 `MAX_LEN` 留出 `MAX-1` 和 `MAX` 两个哨兵值给 CHUNKED 和 CLOSE_DELIMITED,真实长度永远到不了那两个值。
3. **`checked_new` 防御**:从协议头 parse 出一个超过 `MAX_LEN` 的"长度"会被拒绝(`Parse::TooLarge`):

   ```rust
   // 见 src/body/length.rs:54-63
   pub(crate) fn checked_new(len: u64) -> Result<Self, crate::error::Parse> {
       if len <= MAX_LEN {
           Ok(DecodedLength(len))
       } else {
           warn!("content-length bigger than maximum: {} > {}", len, MAX_LEN);
           Err(crate::error::Parse::TooLarge)
       }
   }
   ```

   这一刀防止了恶意构造的"超长 Content-Length"(其实只是个 DoS 防护,18 EB 不可能真传,但解析阶段就要挡住)。

> **钉死这件事**:`DecodedLength = u64 + 两个哨兵值`。`MAX-1` = CHUNKED,`MAX` = CLOSE_DELIMITED,中间是真实长度。一个 u64 表达四种语义,零开销零浪费。这是 Rust 里"newtype + 哨兵值编码"的经典用法——和 Linux 内核里 `ERR_PTR` 把错误码塞进指针高位是同一种思路(承《Linux 内核》系列)。

### 关键操作:sub_if 和 into_opt

`DecodedLength` 最关键的两个方法——一个更新长度,一个查询长度:

```rust
// 见 src/body/length.rs:65-76
pub(crate) fn sub_if(&mut self, amt: u64) {
    match *self {
        DecodedLength::CHUNKED | DecodedLength::CLOSE_DELIMITED => (),
        DecodedLength(ref mut known) => {
            *known -= amt;
        }
    }
}

// 见 src/body/length.rs:42-52
pub(crate) fn into_opt(self) -> Option<u64> {
    match self {
        DecodedLength::CHUNKED | DecodedLength::CLOSE_DELIMITED => None,
        DecodedLength(known) => Some(known),
    }
}
```

`sub_if` 是"如果是已知长度就扣,chunked/close-delimited 就什么都不做"。这就是为什么 `Incoming::poll_frame` 在 Chan 路([`incoming.rs:224`](../hyper/src/body/incoming.rs#L224))和 H2 路([`incoming.rs:246`](../hyper/src/body/incoming.rs#L246))里都调 `len.sub_if(bytes.len() as u64)`——它**对未知长度是 no-op**,所以同一份代码可以同时跑在 chunked 和 Content-Length 两种 body 上。这是一个消除分支的漂亮设计:不写 `if length.is_known() { length -= amt }`,直接 `sub_if`,内部 match 一次了事。

`into_opt` 是"已知长度返回 Some,未知返回 None",给 `size_hint` 用——下一小节看。

### 源码佐证:size_hint 怎么把 DecodedLength 翻译成 SizeHint

`Body::size_hint` 的默认实现返回一个"什么都不知道"的 `SizeHint`(lower=0, upper=None)。`Incoming` 覆盖了它([`incoming.rs:303-325`](../hyper/src/body/incoming.rs#L303-L325)):

```rust
// 见 src/body/incoming.rs:303-325
fn size_hint(&self) -> SizeHint {
    fn opt_len(decoded_length: DecodedLength) -> SizeHint {
        if let Some(content_length) = decoded_length.into_opt() {
            SizeHint::with_exact(content_length)
        } else {
            SizeHint::default()        // chunked/close-delimited: 上下界都未知
        }
    }

    match self.kind {
        Kind::Empty => SizeHint::with_exact(0),
        Kind::Chan { content_length, .. } => opt_len(content_length),
        Kind::H2 { content_length, .. } => opt_len(content_length),
        Kind::Ffi(..) => SizeHint::default(),
    }
}
```

这里 `SizeHint`(`http-body` crate)的语义是:
- `SizeHint::with_exact(n)` = lower = upper = n(已知精确长度)。
- `SizeHint::default()` = lower = 0, upper = None(完全未知)。

所以 `Incoming::size_hint` 把 `DecodedLength` 翻译成 `SizeHint`:已知长度给精确,未知给 default。`Empty` 永远是 `with_exact(0)`。

**这个 size_hint 是协议机写响应头的关键决策依据。** 看 dispatcher 在写头部时([`dispatch.rs:358-370`](../hyper/src/proto/h1/dispatch.rs#L358-L370)):

```rust
// 见 src/proto/h1/dispatch.rs:358-370(简化摘录)
let body_type = if body.is_end_stream() {
    self.body_rx.set(None);
    None                                          // body 已 end,头部不带 body 编码
} else {
    let btype = body
        .size_hint()
        .exact()                                   // 问 body:你精确长度是多少?
        .map(BodyLength::Known)                    // 知道 → Known(n)
        .or(Some(BodyLength::Unknown));            // 不知道 → Unknown(chunked)
    self.body_rx.set(Some(body));
    btype
};
self.conn.write_head(head, body_type);
```

协议机在写头部那一刻,问 body 两件事:
1. `is_end_stream()`——你已经结束了吗?(对应零 body 或 Empty)
2. `size_hint().exact()`——你知道自己精确多长吗?

根据答案,它决定写 `Content-Length`(Known)还是 `Transfer-Encoding: chunked`(Unknown)。`BodyLength` 是 hyper 自己的中间 enum([`proto/mod.rs:47-52`](../hyper/src/proto/mod.rs#L47-L52)):

```rust
// 见 src/proto/mod.rs:46-52
#[derive(Debug)]
#[cfg(feature = "http1")]
pub(crate) enum BodyLength {
    /// `Content-Length`.
    Known(u64),
    /// `Transfer-Encoding: chunked` (if h1).
    Unknown,
}
```

注意 `BodyLength` 只有两种(Known / Unknown),而 `DecodedLength` 有四种(含 CLOSE_DELIMITED 和 ZERO)。差别在哪?——`BodyLength` 是"协议机在写头部时要做的二选一决策":要么写 Content-Length,要么写 chunked。而 ZERO/CLOSE_DELIMITED 是"读取端的语义"(已经在前面的 `is_end_stream` 判断里分流掉了),到写头部时只剩下这两种选择。这是两种 enum 各自的职责切分:`DecodedLength` 描述**接收到的**完整语义,`BodyLength` 描述**要发出去的**编码选择。

> **钉死这件事**:`size_hint().exact()` → 协议机决定写 `Content-Length` 还是 `chunked`。这是一个**纯由 body trait 驱动协议机决策**的范例——trait 的形状(提供 size_hint)直接决定了协议层的编码选择。body 不是被动的"数据容器",它是协议机的**信息源**。

---

## 五、技巧精解

本章有两个最硬核的技巧值得单独拆透:**(一)HTTP/1 body 用容量 0 的 mpsc + watch channel 做的背压机制;(二)DecodedLength 用 u64 哨兵值编码四种长度语义**。第二个上一节已拆透,这一节集中拆第一个——它是 hyper body "为什么不丢字节、不爆内存"的核心。

### 技巧一:容量 0 的 mpsc + watch = 完美背压

#### 问题:生产端和消费端怎么不被对方拖垮

回到 H1 body 的接收场景。一边是协议机(dispatcher),它从连接读字节、跑 Decoder 切成 chunk;另一边是用户的 `Service`(异步业务代码),它消费 chunk。这两个跑在**同一个 task 里**(每连接一个 task,承 P0-01),按什么节奏配合?

如果生产端(协议机)读得太快,消费端还没处理,字节就堆在内存里——可能堆成几 GB。如果生产端读得太慢,消费端一直在 await,吞吐就上不去。需要一种机制:**生产端只在消费端"想要"的时候才读,消费端想要就给、不想要就停**。这就是背压(back-pressure)。

#### 朴素方案:无界队列

最朴素的方案是无界 channel——生产端随便塞,消费端按需取。可这等于没背压:生产端可以无限堆,内存爆。pass。

#### 朴素方案:有界队列(容量 N)

容量 N 的 channel,生产端塞满 N 个就 Pending,等消费端取走一个再塞。比无界好,但有两个问题:
- **N 取多少都是错的**。N=1 还是有 1 chunk 的延迟(消费端还没 poll,生产端已经塞了 1 个);N=0 又太严(生产端永远塞不进去)。
- **生产端怎么知道消费端"准备好了"**。容量 N 的 channel,poll_ready 返回 Ready 不代表消费端真的在 poll_next——只代表"队列有空位"。生产端可能往一个**根本没人消费**的队列里塞满了 N 个,然后 Pending,然后那个连接就这么挂着,字节堆着,直到消费者出现。

hyper 要的是**严格的"消费端 poll 过我才塞"**语义——你要我才给,你不要我绝不给。

#### hyper 的解法:mpsc 容量 0 + watch 信号

看 [`incoming.rs:114-137`](../hyper/src/body/incoming.rs#L114-L137) 怎么造 channel:

```rust
// 见 src/body/incoming.rs:114-137
pub(crate) fn new_channel(content_length: DecodedLength, wanter: bool) -> (Sender, Incoming) {
    let (data_tx, data_rx) = mpsc::channel(0);            // ① mpsc 容量 0
    let (trailers_tx, trailers_rx) = oneshot::channel();

    // If wanter is true, `Sender::poll_ready()` won't becoming ready
    // until the `Body` has been polled for data once.
    let want = if wanter { WANT_PENDING } else { WANT_READY };

    let (want_tx, want_rx) = watch::channel(want);        // ② watch 信号 channel

    let tx = Sender {
        want_rx,
        data_tx,
        trailers_tx: Some(trailers_tx),
    };
    ...
}
```

两个 channel 串联:

- **`mpsc::channel(0)`**——容量 0 的多生产单消费 channel。容量 0 意味着:**生产端的 `try_send` / `poll_ready` 只有在消费端**正在 await 接收**时才成功**。这是最严格的背压——一个 chunk 要"当场交接",不存任何队列。
- **`watch::channel(want)`**——一个 SPSC 的"信号灯"。值只能取 `WANT_PENDING`(消费端还没 poll 过)/ `WANT_READY`(消费端 poll 过了,可以塞)/ `CLOSED`(消费端 drop 了,见 [`watch.rs:16`](../hyper/src/common/watch.rs#L16))。

`watch` 是 hyper 自己写的一个极简 SPSC broadcast,看 [`common/watch.rs`](../hyper/src/common/watch.rs#L1-L73) 全文就 73 行:

```rust
// 见 src/common/watch.rs:14-73
type Value = usize;

pub(crate) const CLOSED: usize = 0;

struct Shared {
    value: AtomicUsize,
    waker: AtomicWaker,
}

impl Sender {
    pub(crate) fn send(&mut self, value: Value) {
        if self.shared.value.swap(value, Ordering::SeqCst) != value {
            self.shared.waker.wake();           // 值变了才唤醒
        }
    }
}

impl Drop for Sender {
    fn drop(&mut self) {
        self.send(CLOSED);                       // drop 时发 CLOSED
    }
}

impl Receiver {
    pub(crate) fn load(&mut self, cx: &mut task::Context<'_>) -> Value {
        self.shared.waker.register(cx.waker());  // 注册 waker
        self.shared.value.load(Ordering::SeqCst)
    }
    pub(crate) fn peek(&self) -> Value {
        self.shared.value.load(Ordering::Relaxed)
    }
}
```

`watch` 就是一个 `AtomicUsize` + `AtomicWaker`。`send` 用 `swap` 改值,变了才 `wake`。`load` 注册当前 task 的 waker 再读值(经典"先注册后读"避免丢唤醒)。

#### 两个 channel 怎么配合

生产端(`Sender`)要先 `poll_ready` 才能塞数据。看 [`incoming.rs:362-377`](../hyper/src/body/incoming.rs#L362-L377):

```rust
// 见 src/body/incoming.rs:362-377
pub(crate) fn poll_ready(&mut self, cx: &mut Context<'_>) -> Poll<crate::Result<()>> {
    // Check if the receiver end has tried polling for the body yet
    ready!(self.poll_want(cx)?);                   // ① 先看消费端要不要
    self.data_tx
        .poll_ready(cx)                            // ② 再看 mpsc 有没有空位
        .map_err(|_| crate::Error::new_closed())
}

fn poll_want(&mut self, cx: &mut Context<'_>) -> Poll<crate::Result<()>> {
    match self.want_rx.load(cx) {
        WANT_READY => Poll::Ready(Ok(())),          // 消费端 poll 过了
        WANT_PENDING => Poll::Pending,              // 消费端没 poll 过,我等着
        watch::CLOSED => Poll::Ready(Err(crate::Error::new_closed())),  // 消费端 drop 了
        unexpected => unreachable!("want_rx value: {}", unexpected),
    }
}
```

生产端的 `poll_ready` 是**两道闸**:

1. **第一道:watch 信号**。消费端没 poll 过(`WANT_PENDING`),生产端直接 `Pending`——绝不超前生产。消费端 poll 过(`WANT_READY`),放行。
2. **第二道:mpsc::poll_ready**。即便消费端 poll 过,容量 0 的 mpsc 也只有消费端**正在 await 接收**时才 Ready。

两道闸合起来,语义是:**"消费端明确表示要数据" 且 "消费端此刻正在 await 接收" 才能塞**。这是 Rust 异步里能表达的最严格背压。

消费端那边怎么"表示要数据"?——`poll_frame` 进来的第一行就 `want_tx.send(WANT_READY)`(见 [`incoming.rs:220`](../hyper/src/body/incoming.rs#L220))。这一句把信号灯翻成 READY,唤醒生产端的 `poll_ready`,然后 mpsc 的 `poll_next` 真的等数据。

```
┌─ 消费端 (Incoming::poll_frame) ──────────────────────────────────────┐
│                                                                       │
│  poll_frame(cx) {                                                     │
│      want_tx.send(WANT_READY); ────────────────────┐  ① 翻信号灯       │
│      data_rx.poll_next(cx)  ◄────────────────────  │  ② 等 mpsc 数据   │
│          │                                          │                   │
│          ▼                                          │                   │
│      Frame::data(chunk)                             │                   │
│  }                                                  │                   │
│                                                      ▼                   │
└──────────────────────────────────────────────────────────────────────┘
                                                       (watch: PENDING → READY)
┌─ 生产端 (Dispatcher::poll_read → Sender::poll_ready) ────────────────┐
│                                                                       │
│  poll_ready(cx) {                                                     │
│      poll_want(cx) ──────────────────────────── ① 看信号灯             │
│          │   (WANT_PENDING → Pending,等消费端先 poll)                  │
│          │   (WANT_READY   → 放行)                                     │
│          ▼                                                             │
│      data_tx.poll_ready(cx) ─────────────────── ② 看 mpsc 有无空位     │
│          │   (消费端没在 await → Pending)                              │
│          │   (消费端在 await → Ready)                                  │
│          ▼                                                             │
│      try_send_data(chunk)  ─────────────────── ③ 当场交接(容量 0)     │
│  }                                                                     │
└──────────────────────────────────────────────────────────────────────┘
```

#### 这个设计的几个 sound 保证

1. **不丢字节**。生产端只在消费端"在 await"时才塞,塞了立刻被取走。中间没有"队列里堆积但消费者消失"的窗口——如果消费者 drop,watch 立刻变 CLOSED,生产端 `poll_ready` 立刻返回 Err(`new_closed`)。
2. **不爆内存**。容量 0 的 mpsc,任何时候在途的 chunk 至多 1 个(交接中的那个)。一个 8KB chunk 跨 1 万个连接也才 80MB,可控。
3. **背压自然传导到 TCP**。生产端(协议机)Pending 了,就不再从连接读字节,TCP 接收窗口填满后 ACK 变慢,对端 socket buffer 满,对端 send 阻塞——背压一路传回发送方。这是流式 body 不淹内存的根本保障,承《Tokio》的 budget 思想。
4. **`wanter` 参数的精妙**。注意 [`new_channel`](../hyper/src/body/incoming.rs#L114) 的 `wanter: bool` 参数。默认 false(`WANT_READY`),意味着生产端一开始就能塞(不等消费端 poll)。但当 `wants.contains(Wants::EXPECT)` 时(Expect: 100-continue 场景,承 P2-07),传 true(`WANT_PENDING`),生产端必须等消费端先 poll——因为 100-continue 要等 server 同意之后 client 才发 body,server 这边在同意之前不能消费 body。一个 bool 参数,精准切换两种语义。

#### 反面对比:如果只用容量 N 的 mpsc

如果 hyper 用 `mpsc::channel(8)` 而不加 watch:生产端 poll_ready 在队列未满时永远 Ready,即便消费端压根没 poll 过。结果就是连接刚进来,协议机疯狂读字节塞满 8 个 chunk 的缓冲(64KB),然后 Pending。这 64KB 一直堆在内存里,直到消费端来。100 万空闲连接 × 64KB = 64GB——爆。

容量 0 + watch 的设计把这个窗口压到 0:**消费端没说要,生产端一个字节都不读**。

> **钉死这件事**:容量 0 的 mpsc + watch 信号 = "你要我才给"的严格背压。这是 hyper body 不丢字节、不爆内存的核心机制。一个看似平凡的 channel 组合,实现了 Rust 异步里能表达的最严格生产消费同步。承《Tokio》的 budget 思想(budget=128 让单个 task 不霸占线程,这里 body channel 让单个连接不霸占内存),都是"按需驱动"的同一种哲学。

### 技巧二:DecodedLength 的哨兵编码(已在第四节拆透,这里钉死要点)

上一节已拆。钉死三件事:

1. **`DecodedLength = u64 newtype`,MAX-1 = CHUNKED,MAX = CLOSE_DELIMITED,中间是真实长度**。一个 u64 表达四种语义,零开销零浪费。
2. **`sub_if` 对未知长度是 no-op**,所以同一份"收到 chunk 扣长度"的代码可以无差别跑在 Content-Length / chunked / close-delimited 三种 body 上。这是消除运行时分支的优雅设计。
3. **`into_opt` 把 DecodedLength 翻译成 `Option<u64>`**,喂给 `SizeHint::with_exact` 或 `SizeHint::default()`。这个 SizeHint 又被协议机用来决定写 `Content-Length` 还是 `chunked`——一个 u64 的取值,最终驱动了 HTTP 头部的编码选择。

这是 Rust newtype + 哨兵值编码的典范用法,和 Linux 内核 `ERR_PTR`、Redis 里 `sds` 的类型标记高位是同一种思路。

---

## 六、协议机 ↔ Body 的双向桥:读路径与写路径

到这里,我们已经拆透了 `Body` trait、`Frame`、`Incoming`、`DecodedLength`。现在把它们串起来,看完整的"协议机 ↔ Body"双向数据流。

### 读路径(server 收请求 body / client 收响应 body)

```
┌─ 网络字节 ─────────────────────────────────────────────────────────────┐
│                                                                         │
│   TCP 字节 → tokio AsyncRead → hyper BufferedIo                        │
│                                                                         │
│   ▼                                                                     │
│   ┌─ Conn::poll_read_body (proto/h1/conn.rs:363) ──────────────────┐  │
│   │  调 Decoder::decode (proto/h1/decode.rs:144)                    │  │
│   │  根据 DecodedLength 选 Length / Chunked / Eof 三种解码:        │  │
│   │    Length(n): 从 buffered io 读 min(n, avail) 字节,返回 Frame::data│
│   │    Chunked:    逐字节状态机解析 chunk size + data + 终止 chunk    │  │
│   │                终止时 decode_trailers → Frame::trailers          │  │
│   │    Eof:        读到连接 EOF,返回 Frame::data(空=结束)          │  │
│   └─────────────────────────────────────────────────────────────────┘  │
│                                                                         │
│   ▼ poll_read_body 返回 Frame<Bytes>                                   │
│   ┌─ Dispatcher::poll_read (proto/h1/dispatch.rs:216) ─────────────┐  │
│   │  拿到 body_tx(Sender),先 poll_ready(背压,见技巧一)            │  │
│   │  frame.is_data()  → try_send_data(chunk)  塞进 mpsc              │  │
│   │  frame.is_trailers() → try_send_trailers(hm) 塞进 oneshot        │  │
│   │  poll_read_body None → drop body_tx,channel 自然关闭             │  │
│   └─────────────────────────────────────────────────────────────────┘  │
│                                                                         │
│   ▼ mpsc / oneshot                                                      │
│   ┌─ Incoming::poll_frame (body/incoming.rs:193, Chan 路) ──────────┐  │
│   │  want_tx.send(WANT_READY)                                       │  │
│   │  data_rx.poll_next → Frame::data(chunk)  + len.sub_if(...)      │  │
│   │  data 终结 → trailers_rx.poll → Frame::trailers 或 None         │  │
│   └─────────────────────────────────────────────────────────────────┘  │
│                                                                         │
│   ▼ 用户 Service 拿到 Frame<Bytes>                                     │
└─────────────────────────────────────────────────────────────────────────┘
```

读路径的关键:**协议机(Dispatcher)是生产端,用户的 Service 是消费端**。协议机把字节切成 `Frame`,通过 mpsc/oneshot 桥交给 `Incoming`,用户 `poll_frame` 出来用。`DecodedLength` 在 `Incoming` 这边跟踪"还剩多少字节"(只在已知长度时有意义)。

注意 dispatcher 那边 [`poll_read`](../hyper/src/proto/h1/dispatch.rs#L238-L270) 的三路分流:

```rust
// 见 src/proto/h1/dispatch.rs:238-271(简化摘录)
match self.conn.poll_read_body(cx) {
    Poll::Ready(Some(Ok(frame))) => {
        if frame.is_data() {
            let chunk = frame.into_data().unwrap_or_else(|_| unreachable!());
            match body.try_send_data(chunk) { ... }
        } else if frame.is_trailers() {
            let trailers = frame.into_trailers().unwrap_or_else(|_| unreachable!());
            match body.try_send_trailers(trailers) { ... }
        } else {
            error!("unexpected frame");              // 防御
        }
    }
    Poll::Ready(None) => { /* just drop, body 自动关 */ }
    Poll::Pending => { self.body_tx.set(body); return Poll::Pending; }
    Poll::Ready(Some(Err(e))) => { body.send_error(crate::Error::new_body(e)); }
}
```

`poll_read_body` 返回的就是 `Frame<Bytes>`(Decoder 切出来的),dispatcher 用 `is_data` / `is_trailers` 分流到 mpsc / oneshot。**`Frame` 这个类型,从 Decoder(协议机最底层)到 dispatcher(协议机调度层)到 Incoming(trait 层),一以贯之**。这是为什么 `Frame` 必须既装 data 又装 trailers——因为协议机的解码器在 chunked 终止时本来就要产出一个"trailers"的东西,它需要一个统一的返回类型。

### 写路径(server 发响应 body / client 发请求 body)

写路径是反过来——**用户的 Service 是生产端,协议机是消费端**。

```
┌─ 用户 Service 返回 Response<B>(B: Body) ──────────────────────────────┐
│                                                                         │
│   ▼ Dispatcher::poll_write (proto/h1/dispatch.rs:347)                  │
│   ┌─ 决定 body_type ────────────────────────────────────────────────┐ │
│   │  body.is_end_stream() → None(空 body,头部不带编码信息)          │ │
│   │  body.size_hint().exact() → Some(n) → BodyLength::Known(n)       │ │
│   │                            → None    → BodyLength::Unknown       │ │
│   │  write_head(head, body_type)                                     │ │
│   │    Known → 写 Content-Length: n                                  │ │
│   │    Unknown → 写 Transfer-Encoding: chunked                       │ │
│   └─────────────────────────────────────────────────────────────────┘ │
│                                                                         │
│   ▼ loop: poll_frame 出 Frame                                          │
│   ┌─ poll_frame 分流 (dispatch.rs:392) ─────────────────────────────┐ │
│   │  Frame::data(chunk):                                            │ │
│   │    is_end_stream? → write_body_and_end (一次性收尾)             │ │
│   │    else           → write_body (普通 chunk,Encoder 编码)        │ │
│   │  Frame::trailers(hm): → write_trailers (chunked 终止 + trailer) │ │
│   │  None(流结束):       → end_body (补 chunked 终止符或对齐)      │ │
│   └─────────────────────────────────────────────────────────────────┘ │
│                                                                         │
│   ▼ Conn::write_body / write_trailers / end_body (proto/h1/conn.rs:704+)│
│   ┌─ Encoder::encode (proto/h1/encode.rs:128) ─────────────────────┐  │
│   │  Length(n):  截断到剩余长度,直接写 (BufKind::Exact/Limited)    │  │
│   │  Chunked:    前缀 chunk size 行 + data + "\r\n" (BufKind::Chunked)│  │
│   │  CloseDelim: 直接写,不编码                                      │  │
│   │  Encoder::end: 写 "0\r\n\r\n" (chunked 终止) 或校验 length 归 0 │  │
│   │  encode_trailers: 写 "0\r\n" + trailer 头 + "\r\n"              │  │
│   └─────────────────────────────────────────────────────────────────┘  │
│                                                                         │
│   ▼ 写到 io buffer → flush → tokio AsyncWrite → TCP                    │
└─────────────────────────────────────────────────────────────────────────┘
```

写路径的关键决策点:

1. **头部写出去之前,先问 body 两件事**:`is_end_stream`(你是不是已经空了)和 `size_hint().exact()`(你知道精确长度吗)。这两问决定了 `Content-Length` 还是 `chunked`。
2. **poll_frame 出来的每帧,立刻编码写出去**。data 帧 → Encoder 编码(chunked 加前缀,length 截断)→ io buffer。trailers 帧 → encode_trailers(chunked 终止 chunk + trailer 头)。None → end_body(补终止符)。**全程没有一个"先把整个 body 收齐再写"的步骤**。

看 [`conn.rs:704-727`](../hyper/src/proto/h1/conn.rs#L704-L727) 的 `write_body`:

```rust
// 见 src/proto/h1/conn.rs:704-727
pub(crate) fn write_body(&mut self, chunk: B) {
    debug_assert!(self.can_write_body() && self.can_buffer_body());
    debug_assert!(chunk.remaining() != 0);

    let state = match self.state.writing {
        Writing::Body(ref mut encoder) => {
            self.io.buffer(encoder.encode(chunk));        // 用 Encoder 编码后塞 io buffer

            if !encoder.is_eof() {
                return;                                    // 没编完(Eof),继续等下一块
            }

            if encoder.is_last() {
                Writing::Closed                            // Length 编完 = 关连接
            } else {
                Writing::KeepAlive                         // chunked 编完 = 复用连接
            }
        }
        _ => unreachable!("write_body invalid state: {:?}", self.state.writing),
    };

    self.state.writing = state;
}
```

注意 `encoder.encode(chunk)` 这一步——它返回一个 `EncodedBuf<B>`,这个 `EncodedBuf` 根据 Encoder 的 kind(`Chunked` / `Length` / `CloseDelimited`)是**不同的字节布局**([`encode.rs:51-58`](../hyper/src/proto/h1/encode.rs#L51-L58)):

```rust
// 见 src/proto/h1/encode.rs:51-58
enum BufKind<B> {
    Exact(B),                                                    // Length: 直接就是原 chunk
    Limited(Take<B>),                                            // Length 超长:截断
    Chunked(Chain<Chain<ChunkSize, B>, StaticBuf>),              // Chunked: size行 + data + \r\n
    ChunkedEnd(StaticBuf),                                       // Chunked 终止: b"0\r\n\r\n"
    Trailers(Chain<Chain<StaticBuf, Bytes>, StaticBuf>),         // Trailers: b"0\r\n" + 头 + \r\n
}
```

`bytes::buf::Chain` 把多个 `Buf` 逻辑串起来,**不拷贝**。所以一个 chunked 编码的 body chunk,在内存里是三个独立 buffer 的逻辑串联(chunk size 行 + 用户 data + `\r\n`),写出去时用 `IoSlice` 做 vectored write(writev)——零拷贝。承《内存分配器》`bytes::Bytes` 的引用计数零拷贝,本书 P6-17 招牌章续拆。

> **钉死这件事**:写路径里,Encoder 把 `Frame::data` 编成字节用的是 `Chain` 零拷贝串联——chunk size 行、用户 data、`\r\n` 各自独立,合起来一次 writev 写出去。这是 hyper 高性能的微观基础之一,承 P6-17。

---

## 七、hyper 1.0 为什么重做 Body:从 Chunk/Stream 到 Frame

这一节回答章首的第四个问题:1.0 body 重做的根因。

### 0.x 时代:Chunk 还是 Stream,二选一

hyper 0.14(及更早)的 `Body` 类型是一个**具体类型**(不是 trait),它内部可以是两种东西之一:

- 一个 `Chunk`(一次性,等价于今天的 `Full<Bytes>`)。
- 一个 `Stream<Item = Result<Chunk, Error>>`(流式)。

这个设计的问题:

1. **trailer 没法表达**。一个 `Stream<Item = Chunk>` 只能流出 `Chunk`,没法在末尾带 trailer。所以 0.x 时代发 trailer 要走特殊 API(`Body::wrap_stream` 加各种适配),不优雅。
2. **`Body` 是具体类型,不是 trait**。用户想自定义 body(比如一个直接包装 mmap 文件的 body),没法实现 `Body` trait——只能用 hyper 提供的几种构造器。这把 hyper 锁死成了"黑盒",axum/tonic/reqwest 想扩展 body 类型很难。
3. **`Chunk` 和 `Stream` 两套语义混在一个类型里**。每次操作 body 都要判断它"现在是 Chunk 模式还是 Stream 模式",代码复杂。

### 1.0 的重构:trait + Frame + 提到 http-body crate

1.0 做了三件事:

1. **把 `Body` 从具体类型变成 trait**(提到独立的 `http-body` crate)。任何实现了 `poll_frame` 出 `Frame` 的类型都可以是 body。用户可以自己造,axum/tonic 可以自己造。
2. **引入 `Frame<T>`**。`Frame` 既装 data 又装 trailers,统一了"body 里能流出的东西"。
3. **`Incoming` 作为 hyper 自己的"接收流"实现**。它实现了 `Body` trait,但内部按 Empty/Chan/H2 分后端。

这个重构的深层动机,是**可组合性**(composability)。hyper 1.0 的口号是"composable building blocks for HTTP"(承 P6-19 演进章)。把 body 变成 trait,意味着:
- axum 可以提供自己的 body 类型(`axum::body::Body`),包装 hyper 的 `Incoming`。
- tonic 可以提供自己的 body 类型(基于 `Incoming` 加 gRPC trailer 处理)。
- reqwest 可以提供自己的 body 类型(流式上传)。
- 用户可以写一个 body 适配器(比如 `MapBody<B>` 把 `Body<Data=Bytes>` 变成 `Body<Data=&[u8]>`),靠 `Frame::map_data`。

这些都是 0.x 时代做不到的。**trait + Frame = body 的可组合性根基**。

> **钉死这件事**:1.0 重做 body 的根因 = **可组合性**。把 body 从具体类型变 trait + 引入 Frame 统一帧类型,让上层框架(axum/tonic/reqwest)能各自扩展 body,而不是被 hyper 锁死。这是 1.0 "composable building blocks" 主张的核心一环。承 P6-19 演进章续拆。

### 一个对照:旧的 wrap_stream 怎么变成今天的 Body::map_frame

0.x 时代,要把一个 `Stream<Item = Result<Chunk, Error>>` 变成 hyper body,要用 `Body::wrap_stream(s)`——一个工厂方法,内部包一层。trailer? 没法加。

1.0 时代,`http-body-util` crate 提供了一堆 body 适配器(承外部 crate,引用说明):

- `Full<T>`:一次性已知长度的 body(等价 0.x 的 Chunk)。
- `Empty`:空 body。
- `BoxBody<T, E>`:类型擦除的 body(给 axum 这种返回类型不确定的框架用)。
- `Combinators::map_frame` / `map_data` / `map_err`:把一个 Body 变成另一个 Body。
- `BodyStream`:反向,把 Body 变成 Stream(给想要 Stream 接口的用户用)。

这套适配器全建在 `Body` trait + `Frame` 之上。`map_data` 内部就是 `Frame::map_data`——拿到一个 frame,如果是 data 就变换,如果是 trailers 就原样穿过。trait + Frame 让适配器写起来优雅,这是 0.x 时代做不到的。

---

## 八、为什么这套设计是 sound 的

这一节钉死"为什么不丢字节、长度怎么跟踪、chunked 边界怎么定"这三个 sound 保证。

### 不丢字节:三重保障

1. **背压机制(技巧一)**。生产端(协议机)只在消费端"在 await"时才塞,塞了立刻被取走。中间没有"队列里堆积但消费者消失"的窗口。如果消费者 drop,watch 立刻 CLOSED,生产端 `poll_ready` 立刻 Err。
2. **mpsc 的语义保证**。`futures_channel::mpsc` 是一个标准的 SPSC/MPSC channel,它的契约是"塞进去的值要么被消费端收到,要么 channel 关闭时丢弃并通知生产端"。hyper 用 `try_send_data` 返回 `Result<(), Bytes>`——失败时把原 Bytes 还回来([`incoming.rs:417-421`](../hyper/src/body/incoming.rs#L417-L421)),dispatcher 拿到这个错就知道 body 没收下,关连接。没有任何"塞进去就丢了"的路径。
3. **length 校验**。已知长度的 body,每收到一块就 `len.sub_if(...)`,如果最终 `len` 没归零,说明 body 不完整——协议机会报错(IncompleteBody)。这是协议层的字节完整性保证。

看 `Decoder::decode` 在 Length 路径对"提前 EOF"的处理([`decode.rs:151-170`](../hyper/src/proto/h1/decode.rs#L151-L170)):

```rust
// 见 src/proto/h1/decode.rs:151-170(简化摘录)
Length(ref mut remaining) => {
    if *remaining == 0 {
        Poll::Ready(Ok(Frame::data(Bytes::new())))           // 读够长度,返回空 = 结束
    } else {
        let to_read = usize::try_from(*remaining).unwrap_or(usize::MAX);
        let buf = ready!(body.read_mem(cx, to_read))?;
        let num = buf.as_ref().len() as u64;
        if num > *remaining {
            *remaining = 0;                                   // 读超了(不可能,但防御)
        } else if num == 0 {
            return Poll::Ready(Err(io::Error::new(           // 没读够就 EOF = body 不完整
                io::ErrorKind::UnexpectedEof,
                IncompleteBody,
            )));
        } else {
            *remaining -= num;                                // 正常扣减
        }
        Poll::Ready(Ok(Frame::data(buf)))
    }
}
```

注意 `num == 0`(读到 EOF 但 remaining 还 >0)返回 `IncompleteBody` 错误——这是协议机在"不丢字节"上的最后一道防线:即便 channel 和背压都正常,如果底层连接被对端提前关了(body 没传完),协议机也会报错,绝不让一个不完整的 body 假装完整地交给用户。

### 长度怎么跟踪:DecodedLength + sub_if(已在第四节拆透)

钉死:已知长度用 `sub_if` 扣减,未知长度(chunked/close-delimited)`sub_if` 是 no-op,所以同一份代码无差别跑三种 body。

### chunked 边界怎么定:逐字节状态机

chunked body 没有预声明长度,靠每块前缀的 chunk size 行 + 终止 chunk 定边界。这是 H1 Decoder 的 `Chunked` variant 干的事([`decode.rs:171-222`](../hyper/src/proto/h1/decode.rs#L171-L222)),内部跑一个 13 态的 `ChunkedState` 状态机([`decode.rs:69-84`](../hyper/src/proto/h1/decode.rs#L69-L84)):

```rust
// 见 src/proto/h1/decode.rs:69-84
#[derive(Debug, PartialEq, Clone, Copy)]
enum ChunkedState {
    Start,        // 初始
    Size,         // 读 chunk size 的数字
    SizeLws,      // 跳过 size 后的空白
    Extension,    // chunk extension(;xxx=yyy)
    SizeLf,       // size 行的 \n
    Body,         // chunk 数据本体
    BodyCr,       // chunk 数据后的 \r
    BodyLf,       // chunk 数据后的 \n
    Trailer,      // trailer 头
    TrailerLf,    // trailer 后的 \n
    EndCr,        // 终止 \r
    EndLf,        // 终止 \n
    End,          // 完全结束
}
```

这个状态机逐字节推进:读到 chunk size 行(如 `1a\r\n`)→ 读 0x1a 字节的 body → 读 `\r\n` → 回到 Start 等下一块 → 读到 `0\r\n` → 进 Trailer → 读 trailer 头(可选)→ 读 `\r\n` 结束。

> **承接**:chunked 状态机的细节(chunk size 行解析、extension 跳过、trailer 解码)是 P2-07 招牌章的内容,本章不展开。这里只钉死一点:**chunked 的"边界"不是靠长度,是靠这个 13 态状态机逐字节切出来的**。每一块的前缀(`size\r\n`)和后缀(`\r\n`)、终止 chunk(`0\r\n`)、可选 trailer,全是状态机推进出来的。这是为什么 chunked body 的 `DecodedLength` 是 CHUNKED 哨兵值——总长度事先根本不知道,得一块块切。

---

## 九、章末小结

### 回扣主线

本章是框架地基的第二根柱子(第一根是 Service trait,P1-02)。它回答了"请求/响应体怎么流"这个框架侧的核心问题。回到全书的协议侧 / 框架侧二分法:

- **本章属于框架侧**。Body 是 hyper 把"流式数据"抽象成 trait 的产物,和 Service(把请求处理抽象成 Future)并列为框架地基的两大招牌。
- **但 Body 又和协议侧紧密咬合**:`Frame` 这个类型(data + trailers)直接对应 HTTP 协议的帧语义,`size_hint` 直接驱动协议机的编码决策(`Content-Length` vs `chunked`),`DecodedLength` 的四种取值直接对应 H1 Decoder 的三种 Decoder(Length/Chunked/Eof)。Body 是**框架侧和协议侧的接缝**——它把协议的字节流抽象成 trait,让协议机和用户 Service 各管一段。

一句话:**Body = Stream 的 poll 模型 + Frame 的多帧类型 + SizeHint 的长度信息 + DecodedLength 的协议语义编码**。这四样东西拼起来,就是 hyper 1.0 body 的全貌。

### 五个为什么

1. **为什么 body 必须是流,不能是一次性 buffer?**——大文件上传会爆内存、流式响应没法表达、trailer 没处放。流式让 body"边到边走",按需消费即背压。
2. **为什么 hyper 不直接用 `Stream<Item=Bytes>`,而要造 `Body` trait 流出 `Frame`?**——`Stream` 只能流 data,表达不出 trailers;`Stream` 没法在写头部前告诉协议机长度(`size_hint`)。`Body` = Stream + Frame(data/trailers 统一)+ SizeHint(长度信息)。
3. **为什么 `Incoming` 内部要分 Empty/Chan/H2 三种 Kind?**——HTTP/1 的 body 来自协议机切字节(用 mpsc/oneshot channel 桥),HTTP/2 的 body 来自 h2 crate 已切好的 RecvStream(直接包装),空 body 是纯标记。一个 enum 容纳三种来源,对外统一 `poll_frame`。
4. **为什么 `DecodedLength` 用 u64 哨兵值编码四种长度语义,而不是用 enum?**——零开销(Copy 的 u64 vs 带 tag 的 enum)+ 紧凑对齐 + `sub_if` 对未知长度 no-op 让代码无分支。哨兵值占两个高位(MAX-1, MAX),真实长度在 `[0, MAX-2]`。
5. **为什么 hyper 1.0 要重做 body(从 Chunk/Stream 二选一变 trait+Frame)?**——可组合性。把 body 变 trait,axum/tonic/reqwest 才能各自扩展 body 类型;引入 Frame,trailer 才能优雅表达。这是 1.0 "composable building blocks" 主张的核心。

### 想继续深入往哪钻

- **想看 chunked 状态机的逐字节细节**:读本书 P2-07(chunked、100-continue 与升级),那是协议侧的招牌章,会把 `ChunkedState` 的 13 个状态逐一拆透。
- **想看 H1 Decoder/Encoder 的完整实现**:读 [`src/proto/h1/decode.rs`](../hyper/src/proto/h1/decode.rs) 和 [`src/proto/h1/encode.rs`](../hyper/src/proto/h1/encode.rs) 全文,对照本章的 Frame/DecodedLength 讲解。
- **想看 `http-body` trait 和 `Frame`/`SizeHint` 的完整 API**:读 [docs.rs/http-body](https://docs.rs/http-body/latest/http_body/),以及 `http-body-util` crate(Empty/Full/BoxBody/Combinators 等适配器)。
- **想看 H2 body 的流控和 ping**:本书 P3-11(HTTP/2 ping 与流控),会拆透 `ping::Recorder` 和 `release_capacity` 的协作。
- **想看 `bytes::Bytes` 的零拷贝**:本书 P6-17(bytes 零拷贝与 buffered IO)招牌章,拆透 `Chain` + `IoSlice` 的 vectored write。
- **想动手感受**:用 hyper 写一个 server,handler 里 `req.into_body().frame().await` 逐帧打印(配合 `http-body-util` 的 `BodyExt`),再用 curl 发一个 chunked 请求,观察帧是怎么一块块到的。

### Tokio 怎么支撑(承接点)

- **`Body` 的 poll 模型完全继承自 Stream/Future**:`Poll::Pending` 让出线程、`Waker` 唤醒、`Pin` 保证自引用安全。这些底子是《Tokio》讲透的,本章一句带过。
- **mpsc/oneshot channel 来自 `futures_channel`**(Tokio 生态),容量 0 的 mpsc 是严格背压的载体。
- **背压传导到 TCP**(生产端不读 → TCP 接收窗口满 → 对端阻塞)承《Tokio》的 reactor/budget 思想。
- **`bytes::Bytes` 的引用计数零拷贝**承《内存分配器》和《Tokio》bytes 章节,P6-17 续拆。

### 引出下一章

我们拆透了框架地基的两大招牌:Service(把请求处理抽象成 Future,P1-02)和 Body(把请求体抽象成 Frame 流,本章)。框架侧的地基到此铺完。接下来要穿过"协议侧 vs 框架侧"的接缝,进入协议侧——HTTP/1 协议机。一条 HTTP/1 连接怎么循环处理多个请求(keep-alive)?协议机的 dispatch 循环长什么样?协议机怎么和本章拆的 Body / DecodedLength 配合?下一章 P2-05,我们从 **HTTP/1 连接与 keep-alive** 开始,正式进入协议招牌篇。

> **下一章**:[P2-05 · HTTP/1 连接与 keep-alive](P2-05-HTTP1-连接与keep-alive.md)
