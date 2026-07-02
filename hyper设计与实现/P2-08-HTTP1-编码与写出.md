# 第 2 篇 · 第 8 章 · HTTP/1 编码与写出

> **核心问题**:前面三章我们一直在讲"字节进来"——一条 HTTP/1 连接怎么循环(P2-05)、TCP 字节怎么被解析状态机切成请求行/头/body(P2-06)、chunked/100-continue/upgrade 这些边角协议怎么解(P2-07)。现在把镜头反过来:Service 算出了一个 `Response`(状态码、头部、一个 Body Stream),这一坨结构化的东西,怎么被**编回字节流**写出去?写出去的时候,`Transfer-Encoding` 是怎么自动定的——body 长度已知就 `Content-Length`、未知就 `chunked`、HTTP/1.0 不懂 chunked 就只能 `close-delimited`(靠关连接标结束)?body 是**流式**的(一个 Frame 接一个 Frame 来),编码怎么能做到"不等整个 body 到齐就边编边写"?最后,字节攒在写缓冲里,什么时候 flush——写完头就 flush 让 client 早点收到,还是和 body 攒一起一次写出?这一章把"编码"这条线和前 3 章闭环,作为第 2 篇的收尾。

> **读完本章你会明白**:
> 1. 一个响应(状态行 + 头部 + body)怎么被 `Server::encode`(或 client 侧的 `Client::encode`)编成字节,字节先落到一个 `headers_buf`(就是个 `Vec<u8>`)里,再随 body 一起 flush 出去。
> 2. `Transfer-Encoding` 三态——`Length(u64)` / `Chunked` / `CloseDelimited`——怎么由 `Encoder::Kind` 这个枚举表达,以及它们各自怎么定:有 `Content-Length` 用 length、长度未知且能 chunked 用 chunked、HTTP/1.0 退化成 close-delimited。client 和 server 两侧的判定逻辑差异(`Client::set_length` vs `Server::encode_headers` 里那段 `match msg.body`)。
> 3. body 流式编码的精髓:`Encoder::encode(chunk)` 把一个 body chunk 包成 `EncodedBuf`(chunked 就包成 `ChunkSize + chunk + \r\n` 的 Chain,length 就原样或截断,close-delimited 就原样),`EncodedBuf` 实现了 `Buf`,塞进 `WriteBuf` 的队列,**不拷贝字节**。头部先写出去、body 以 Frame 边编码边写,不等 body 到齐。
> 4. 零拷贝写出:`BufKind` 五种变体(`Exact` / `Limited` / `Chunked` / `ChunkedEnd` / `Trailers`),核心是 `bytes::Chain` 把"chunk 大小行 + 用户 body + 结尾 `\r\n`"三段链成一个 `Buf`,一次 `writev` 吐出去,中间一字节都不拷贝。对照 `bytes::Chain` 和 Envoy HCM 的 encode 路径。
> 5. flush 策略三层:`poll_flush` 攒到 `MAX_BUF_LIST_BUFFERS=16` 个 buf 或 `max_buf_size` 字节才强制 flush(背压),`flush_pipeline` 模式(server 配置)让"写完头立刻 flush"换更低延迟(牺牲 writev 批量),以及 `wants_write_again` 防止写侧自挂起。为什么 flush 时机只影响延迟不影响正确性。
> 6. 为什么这一切是 **sound** 的:body 流式编码不破坏 chunked 边界(每帧独立 `encode` → 独立 `EncodedBuf` → 独立边界标记)、零拷贝 chain 不拷贝字节(`Chain` 只是三个指针)、flush 时机由缓冲水位决定而非协议决定(协议只要求"字节顺序对",缓冲攒多少再吐是延迟权衡)。

> **如果一读觉得太难**:先记三件事——① 响应编码 = 状态行 + 头写进 `headers_buf`(一个 `Vec<u8>`)+ body 一个 chunk 一个 `EncodedBuf` 塞进 `WriteBuf` 队列,flush 时一次 `writev` 吐出去;② `Transfer-Encoding` 三态由 `Encoder::Kind` 表达:已知长度 `Length`、未知且能 chunked `Chunked`、HTTP/1.0 退化 `CloseDelimited`,判定全在 `Server::encode_headers` / `Client::set_length`;③ flush 时机不影响正确性只影响延迟,hyper 默认攒够 16 个 buf 或 max_buf_size 字节才强制 flush,`pipeline_flush` 模式则写完头就 flush 换低延迟。这三条抓住了,后面看 `encode.rs`/`role.rs` 就有了挂靠点。

---

## 〇、一句话点破

> **HTTP/1 编码就是把"状态行 + 头 + body"重新变成字节流:状态行和头是一个字符串拼接活儿(写到 `headers_buf`,FastWrite/extend 一路 `extend_from_slice`),body 是流式编码活儿(每个 chunk 经 `Encoder::encode` 包成 `EncodedBuf`,chunked 包成 `ChunkSize + chunk + \r\n` 的 Chain,length 原样或截断,close-delimited 原样),所有 `EncodedBuf` 塞进 `WriteBuf` 队列,flush 时一次 `writev` 吐到 socket。整个过程的关键是零拷贝(chain 不拷字节)和流式(不等 body 到齐)、以及 `Transfer-Encoding` 三态的自动判定。**

这是结论。本章倒过来拆:先讲"编码"和"解析"为什么是对称的两面、编码要解决什么独有问题;再拆 `Encoder::Kind` 三态和 `Transfer-Encoding` 怎么自动选;然后拆 body 流式编码的状态机(`Writing::Init → Body(Encoder) → KeepAlive/Closed`)和 `EncodedBuf`/`BufKind` 怎么做到零拷贝;接着拆 flush 策略三层;最后是技巧精解,把"Buf::chain 零拷贝"和"`Transfer-Encoding` 自动选择"两个最硬核的拆透。

> **承接《Tokio》**:写出的底层是 `AsyncWrite::poll_write` / `poll_write_vectored` / `poll_flush`,以及 Tokio 给的 reactor(epoll/kqueue 边沿触发,等 socket 可写)、task 调度、budget 让出——这些《Tokio》拆透的机制一句带过。本章篇幅全留 hyper 独有:**怎么在 `AsyncWrite` 之上,搭起一个零拷贝、流式、自动选 `Transfer-Encoding` 的 HTTP/1 编码器**。
>
> **承接《gRPC》**:HTTP/2 的编码(HPACK 压缩头部、DATA 帧分 stream、流控窗口)在《gRPC》第 2 篇已拆透,一句带过。本章只讲 HTTP/1,HTTP/1 没有 HPACK(头部就是明文拼接)、没有多路复用(一条连接一个响应)、没有流控(靠 TCP 自己的窗口)。HTTP/1 编码的硬骨头全在"零拷贝 chain + 流式 + Transfer-Encoding 选择"上,和 HTTP/2 完全不同维度。

---

## 一、编码与解析:对称的两面,但各有独有问题

第 2 篇前 3 章我们一直在拆"解析"(`decode.rs` + `io.rs::parse` + chunked 解码):TCP 字节进来,状态机逐字节推进,切成请求行/头/body。这一章拆"编码"(`encode.rs` + `role.rs::encode` + `conn.rs::write_*`):Response 出去,编回字节。

### 1.1 为什么编码比解析"看起来简单",实则更难

直觉上,编码是解析的反向,应该更简单——解析要处理"字节流半截断在头中间"的复杂情况,编码只要把结构化的东西拼成字节。但实际写起来,编码有它**独有的难处**,解析反而不需要操心:

- **零拷贝的诉求更强**:解析时,字节已经在 `read_buf` 里,切个 slice 就拿到头部引用;编码时,头部是 `HeaderMap`(一个哈希表),body 是 `Body` Stream(一个 Frame 一个 chunk),要把它们拼成连续字节流写出去——朴素做法是拼一个大 `Vec<u8>` 再 `write`,但这样每个 body chunk 都要拷贝一次,大文件场景下灾难。hyper 用 `Chain` + `writev` 做到零拷贝,这是编码独有的硬骨头。
- **流式的耦合更深**:解析时,字节已经在缓冲里,可以一次性切完整个头;编码时,body 是**边算边来**的(Service 的 Future 还在跑,body 一个 Frame 一个 Frame 产出),不能"等整个 body 到齐再编"。编码器必须支持"先编头写出去,再编第一个 chunk 写出去,再编第二个 chunk ……",这要求编码状态机能"挂起"在 body 中间。
- **`Transfer-Encoding` 要在编头时就定**:body 长度可能未知(Service 算到一半才知道),但头部要先写出去——头部里要写 `Content-Length: 1234` 还是 `Transfer-Encoding: chunked`,**必须在写头那一刻决定**。如果 body 长度未知,就只能选 chunked;如果 HTTP/1.0 不支持 chunked,就只能 close-delimited。这个"在头部时刻就要对未来 body 的编码方式做承诺"的约束,是编码独有的难点。

> **钉死这件事**:编码不是"解析的反向"那么简单。解析的难点是"字节半截断",编码的难点是"零拷贝 + 流式 + 头部时刻承诺 Transfer-Encoding"。这三个难点,正好对应本章三个主菜:`Encoder::Kind` 状态机(承诺 Transfer-Encoding)、`EncodedBuf`/`BufKind`(零拷贝)、`Writing` 状态机 + body 流式 poll(流式编码)。

### 2.1 节会拆 `Transfer-Encoding` 选择,第三节拆零拷贝,第四节拆流式。先从 `Encoder` 这个类型本身看起。

---

## 二、Encoder:一个承诺 Transfer-Encoding 的状态机

### 2.1 `Encoder` 长什么样

`Encoder`(`proto/h1/encode.rs:22`)是 hyper 对"这个 body 用什么 Transfer-Encoding"的编码器抽象:

```rust
// hyper/src/proto/h1/encode.rs:22-49
/// Encoders to handle different Transfer-Encodings.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct Encoder {
    kind: Kind,
    is_last: bool,
}

#[derive(Debug, PartialEq, Clone)]
enum Kind {
    /// An Encoder for when Transfer-Encoding includes `chunked`.
    Chunked(Option<Vec<HeaderName>>),
    /// An Encoder for when Content-Length is set.
    ///
    /// Enforces that the body is not longer than the Content-Length header.
    Length(u64),
    /// An Encoder for when neither Content-Length nor Chunked encoding is set.
    ///
    /// This is mostly only used with HTTP/1.0 with a length. This kind requires
    /// the connection to be closed when the body is finished.
    #[cfg(feature = "server")]
    CloseDelimited,
}
```

两个标志:

- `kind: Kind`——三种 Transfer-Encoding 模式。`Chunked` 带一个 `Option<Vec<HeaderName>>`,是"允许的 trailer 字段名列表"(只有声明在 `Trailer:` 头里的字段,才会被实际编到 chunked 末尾当 trailer,见 P2-07)。`Length(u64)` 带剩余字节数,边编码边扣。`CloseDelimited` 什么都不带(就是"原样写,写完关连接")。
- `is_last: bool`——这个 body 写完后,连接要不要关。`true` = 写完关(`Writing::Closed`),`false` = 写完 keep-alive(`Writing::KeepAlive`)。它对应响应头里的 `Connection: close`(P2-05 拆过 `KA::Disabled`)。

`Encoder` 有三个工厂方法(`encode.rs:60-78`):

```rust
// hyper/src/proto/h1/encode.rs:60-78 (摘录)
fn new(kind: Kind) -> Encoder {
    Encoder { kind, is_last: false }
}
pub(crate) fn chunked() -> Encoder {
    Encoder::new(Kind::Chunked(None))
}
pub(crate) fn length(len: u64) -> Encoder {
    Encoder::new(Kind::Length(len))
}
#[cfg(feature = "server")]
pub(crate) fn close_delimited() -> Encoder {
    Encoder::new(Kind::CloseDelimited)
}
```

`is_last` 默认 `false`,由 `set_last(is_last)`(`encode.rs:95`)在编码头部时根据 keep-alive 状态设定。整个 `Encoder` 就是一个"我知道这个 body 怎么编、编完要不要关连接"的小状态机。

> **钉死这件事**:`Encoder` 是"对一个 body 的编码承诺"。它在**写头部的那一刻**就被创建出来(下面 2.3 看 `encode_head` 怎么调 `Server::encode` 拿到 `Encoder`),然后跟着整个 body 走完——每个 body chunk 经 `encoder.encode(chunk)` 包一下,body 结束经 `encoder.end()` 收尾(chunked 写 `0\r\n\r\n`,length 检查有没有写够)。`Encoder` 的生命周期 = 一个 body 的生命周期 = `Writing::Body(Encoder)` 状态机的 `Body` 这一段。

### 2.2 `Transfer-Encoding` 三态:语义和迁移

三种 `Kind` 的语义,先钉死:

**`Length(u64)`**——对应 `Content-Length: N`。body 是定长的,编出来的字节就是 body 原样(不加任何边界标记),但编码器会**扣账**:每 `encode(chunk)` 扣掉 chunk 长度,如果 chunk 比 remaining 大,就 `take(limit)` 截断(`encode.rs:148`),防止"body 实际比声明的 Content-Length 长"。body 结束时 `encoder.end()` 检查 remaining 是否为 0,不为 0 就返回 `Err(NotEof(remaining))`——意思"你承诺了 N 字节,实际只给了 N - remaining 字节,这是用户 body 提前 EOF 的错误"(`encode.rs:124`)。

**`Chunked(Option<Vec<HeaderName>>)`**——对应 `Transfer-Encoding: chunked`。body 是分块的,每个 chunk 编成"`{hex 长度}\r\n` + chunk 数据 + `\r\n`",body 结束编 `0\r\n\r\n`(可能带 trailer)。`Option<Vec<HeaderName>>` 是"允许哪些字段当 trailer"——只有声明在 `Trailer:` 头里的字段,`encode_trailers` 才会真正编进去(`encode.rs:170-191`),防止用户误把不该当 trailer 的字段(如 `Authorization`、`Content-Length`,见 `is_valid_trailer_field` `encode.rs:264`)编到末尾。

**`CloseDelimited`**(仅 server,`#[cfg(feature = "server")]`)——对应"既没有 Content-Length 也没有 chunked"。body 原样写,写完**关连接**——client 怎么知道 body 结束?靠"连接关了 = EOF = body 结束"。这是 HTTP/1.0 的兜底(HTTP/1.0 不懂 chunked),也是 HTTP/1.1 在某些退化场景(比如 server 不支持 chunked)的兜底。注意它只在 server 侧出现——client 侧发请求时,`Client::set_length`(`role.rs:1313`)对 HTTP/1.0 + body 长度未知的情况是直接 `Encoder::length(0)`(意思是"HTTP/1.0 client 不能发未知长度的 body,GET/HEAD/CONNECT 假设没 body,其他 method 用户得自己设头"),不会用 close-delimited(因为 client 发请求不能靠"我关连接"来标 body 结束,那样就收不到响应了)。

> **对照《gRPC》**:gRPC 在 HTTP/2 上,DATA 帧带 `END_STREAM` flag 标结束,根本不需要"close-delimited"这种粗暴方式——HTTP/2 一条连接多路复用,关一条 stream 不用关连接。HTTP/1 的 close-delimited 是"用连接关换 body 边界"的无奈之举,代价是这条连接不能 keep-alive 了(P2-05 拆过,`CloseDelimited` 的 `is_close_delimited()` 返回 true,`end_body` 里走 `Writing::Closed`)。这是 HTTP/1 协议本身的局限,hyper 老实遵守。

### 2.3 自动选择:`Server::encode_headers` 和 `Client::set_length`

`Transfer-Encoding` 三态不是用户手动选的,是 hyper 在编头部时**根据 body 长度信息和 HTTP 版本自动判定**的。判定的入口是 `encode_head`(`conn.rs:607`):

```rust
// hyper/src/proto/h1/conn.rs:607-653 (摘录)
fn encode_head(
    &mut self,
    mut head: MessageHead<T::Outgoing>,
    body: Option<BodyLength>,
) -> Option<Encoder> {
    debug_assert!(self.can_write_head());
    if !T::should_read_first() {
        self.state.busy();
    }
    self.enforce_version(&mut head);
    let buf = self.io.headers_buf();
    match super::role::encode_headers::<T>(
        Encode {
            head: &mut head,
            body,
            #[cfg(feature = "server")]
            keep_alive: self.state.wants_keep_alive(),
            req_method: &mut self.state.method,
            title_case_headers: self.state.title_case_headers,
            #[cfg(feature = "server")]
            date_header: self.state.date_header,
        },
        buf,
    ) {
        Ok(encoder) => { /* ... 缓存 headers, 返回 encoder */ Some(encoder) }
        Err(err) => {
            self.state.error = Some(err);
            self.state.writing = Writing::Closed;
            None
        }
    }
}
```

注意三个关键点:

1. `body: Option<BodyLength>`——这是 body 的长度信息。`BodyLength`(`proto/mod.rs:47`)就两态:`Known(u64)`(用户 body 的 `size_hint` 给出了精确长度)和 `Unknown`(给不出精确长度)。**这是判定 chunked vs length 的关键输入**。这个值在 `Dispatcher::poll_write`(`dispatch.rs:362`)里由 `body.size_hint().exact()` 算出来。
2. `Encode { head, body, keep_alive, req_method, title_case_headers, date_header }`——把所有编码需要的上下文打包成 `Encode` 结构(`mod.rs:85`),传给 `T::encode`(server 是 `Server::encode`,`role.rs:370`;client 是 `Client::encode`,`role.rs:1186`)。
3. `buf = self.io.headers_buf()`——头部要写到的地方,就是 `WriteBuf.headers`(一个 `Cursor<Vec<u8>>`)。`Server::encode` 会把状态行和头部直接 `extend` 进这个 `Vec<u8>`,同时**在内部决定 `Transfer-Encoding`**,返回 `Encoder`。

现在看 `Server::encode`(`role.rs:370`)怎么自动选 `Transfer-Encoding`。核心在它调的 `Server::encode_headers`(`role.rs:641`)里那段 `if !wrote_len { ... }`(`role.rs:907`):

```rust
// hyper/src/proto/h1/role.rs:907-951 (摘录, 简化)
if !wrote_len {
    encoder = match msg.body {
        Some(BodyLength::Unknown) => {
            // body 长度未知
            if msg.head.version == Version::HTTP_10
                || !Server::can_chunked(msg.req_method.as_ref(), msg.head.subject)
            {
                // HTTP/1.0 不懂 chunked, 或者这个状态码/method 不能 chunked → 退化 close-delimited
                Encoder::close_delimited()
            } else {
                // HTTP/1.1 + 能 chunked → 写 transfer-encoding: chunked, 用 Chunked encoder
                header_name_writer.write_full_header_line(
                    dst,
                    "transfer-encoding: chunked\r\n",
                    (header::TRANSFER_ENCODING, ": chunked\r\n"),
                );
                Encoder::chunked()
            }
        }
        None | Some(BodyLength::Known(0)) => {
            // 没 body, 或 body 长度已知为 0
            if Server::can_have_implicit_zero_content_length(
                msg.req_method.as_ref(),
                msg.head.subject,
            ) {
                // 写 content-length: 0, 用 Length(0)
                header_name_writer.write_full_header_line(
                    dst,
                    "content-length: 0\r\n",
                    (header::CONTENT_LENGTH, ": 0\r\n"),
                );
            }
            Encoder::length(0)
        }
        Some(BodyLength::Known(len)) => {
            // body 长度已知且 > 0
            if !Server::can_have_content_length(msg.req_method.as_ref(), msg.head.subject) {
                Encoder::length(0)
            } else {
                // 写 content-length: {len}, 用 Length(len)
                header_name_writer.write_header_name_with_colon(
                    dst,
                    "content-length: ",
                    header::CONTENT_LENGTH,
                );
                extend(dst, ::itoa::Buffer::new().format(len).as_bytes());
                extend(dst, b"\r\n");
                Encoder::length(len)
            }
        }
    };
}
```

这段是整章最该逐分支嚼的地方。它回答"server 怎么自动选 `Transfer-Encoding`":

- **`wrote_len` 这个标志的含义**:整个 `encode_headers` 函数在遍历用户提供的 `HeaderMap` 时,如果用户**自己**设了 `Content-Length` 或 `Transfer-Encoding`,就把 `wrote_len = true`,并且直接用用户的值建 `Encoder`(不会走到 `if !wrote_len` 这段)。`if !wrote_len` 处理的是"用户没设长度相关的头,hyper 帮你补"。这是 hyper 的"尊重用户设置,缺了才补"策略(`role.rs:1325` 有一句注释:"If the user already set specific headers, we should respect them")。
- **body 长度已知 (`Some(BodyLength::Known(len))`)**:写 `content-length: {len}`(用 `itoa` 把 u64 格式化成 ASCII,比 `format!` 快),建 `Encoder::length(len)`。这是最优路径——client 收到 `Content-Length` 就知道 body 多长,不用解析 chunked 边界。
- **body 长度未知 (`Some(BodyLength::Unknown)`)**:能 chunked 就 `transfer-encoding: chunked` + `Encoder::chunked()`,不能(HTTP/1.0 或状态码/method 不允许 chunked)就 `Encoder::close_delimited()`(啥头都不加,靠关连接标结束)。
- **没 body 或长度为 0 (`None | Some(BodyLength::Known(0))`)**:补 `content-length: 0`,建 `Encoder::length(0)`。

> **钉死这件事**:server 的 `Transfer-Encoding` 选择是一个**纯函数**,输入是 `(body 长度信息, HTTP 版本, method, 状态码)`,输出是 `Encoder`。它不依赖网络、不依赖 IO、不依赖时间——编头的那一刻就能定。这是"头部时刻承诺 Transfer-Encoding"这个约束的体现:头部要写出去,Transfer-Encoding 就得在那一刻定死,后续 body 必须按这个 encoder 走(承诺了 length 就必须写够 N 字节,承诺了 chunked 就每个 chunk 都按 chunked 格式)。

### 2.4 `can_chunked` / `can_have_content_length`:状态码和 method 的约束

注意 `Server::encode_headers` 里那些 `Server::can_chunked` / `can_have_content_length` / `can_have_implicit_zero_content_length` 判断(`role.rs:503-528`)。它们编码的是 HTTP 协议里"哪些响应**不允许**有 body / **不允许** chunked / **不允许** Content-Length"的规则:

```rust
// hyper/src/proto/h1/role.rs:503-528
fn can_have_body(method: Option<&Method>, status: StatusCode) -> bool {
    Server::can_chunked(method, status)
}
fn can_chunked(method: Option<&Method>, status: StatusCode) -> bool {
    if method == Some(&Method::HEAD)
        || method == Some(&Method::CONNECT) && status.is_success()
        || status.is_informational()
    {
        false
    } else {
        !matches!(status, StatusCode::NO_CONTENT | StatusCode::NOT_MODIFIED)
    }
}
fn can_have_content_length(method: Option<&Method>, status: StatusCode) -> bool {
    if status.is_informational() || method == Some(&Method::CONNECT) && status.is_success() {
        false
    } else {
        !matches!(status, StatusCode::NO_CONTENT | StatusCode::NOT_MODIFIED)
    }
}
fn can_have_implicit_zero_content_length(method: Option<&Method>, status: StatusCode) -> bool {
    Server::can_have_content_length(method, status) && method != Some(&Method::HEAD)
}
```

这些规则来自 RFC 7230/7231:

- `HEAD` 请求的响应**不能有 body**(但可以有 `Content-Length` 表示"如果这是 GET,我会有这么多 body")。所以 `can_chunked` 对 HEAD 返回 false(HEAD 响应不可能 chunked),但 `can_have_content_length` 对 HEAD 不拒绝(HEAD 响应可以带 Content-Length 标"实体本来的长度")。`can_have_implicit_zero_content_length` 对 HEAD 返回 false,意思是"HEAD 响应,hyper 不主动加 `content-length: 0`"(因为用户可能自己设了实体的真实长度)。
- `CONNECT` 成功(2xx)的响应**不能有 body**(body 是后续隧道的数据)。所以 `can_chunked` 和 `can_have_content_length` 都对 CONNECT+2xx 返回 false。
- 1xx(informational,比如 100 Continue)、204 No Content、304 Not Modified **不能有 body**。`can_chunked` 和 `can_have_content_length` 都拒绝。

这些规则不是 hyper 拍脑袋的,是协议规定。hyper 把它们编进 `can_xxx` 函数,在 `encode_headers` 里层层调用,保证编出来的响应**协议合法**。

> **不这样会怎样**:如果不管状态码/method,一律给 body 未知的响应上 chunked,那对 HEAD 请求的响应会编出 `transfer-encoding: chunked` + body——但 HEAD 响应不能有 body,client 会按 chunked 解析一个不存在的 body,协议错乱。`can_chunked` 这些函数是"协议合法性"的守护者,hyper 宁可退化成 close-delimited(对不能 chunked 的场景)也不违反协议。

### 2.5 client 侧:`Client::set_length` 的独立逻辑

client 发请求时,`Transfer-Encoding` 选择逻辑在 `Client::set_length`(`role.rs:1313`),和 server 不完全一样。关键差异:

```rust
// hyper/src/proto/h1/role.rs:1313-1350 (摘录)
fn set_length(head: &mut RequestHead, body: Option<BodyLength>) -> Encoder {
    let body = if let Some(body) = body {
        body
    } else {
        head.headers.remove(header::TRANSFER_ENCODING);
        return Encoder::length(0);
    };

    // HTTP/1.0 doesn't know about chunked
    let can_chunked = head.version == Version::HTTP_11;
    let headers = &mut head.headers;

    let existing_con_len = headers::content_length_parse_all(headers);
    let mut should_remove_con_len = false;

    if !can_chunked {
        // Chunked isn't legal, so if it is set, we need to remove it.
        if headers.remove(header::TRANSFER_ENCODING).is_some() {
            trace!("removing illegal transfer-encoding header");
        }
        return if let Some(len) = existing_con_len {
            Encoder::length(len)
        } else if let BodyLength::Known(len) = body {
            set_content_length(headers, len)
        } else {
            // HTTP/1.0 client requests without a content-length
            // cannot have any body at all.
            Encoder::length(0)
        };
    }
    // ... HTTP/1.1 分支: 优先尊重用户 transfer-encoding, 否则按 existing_con_len, 否则按 body 长度
```

两个 server 没有的特殊处理:

1. **HTTP/1.0 client 不能发未知长度的 body**:server 侧 HTTP/1.0 + 长度未知退化成 close-delimited(关连接标结束),但 client 侧不能这么干——client 发完请求还要等响应,不能"发完 body 就关连接"。所以 `Client::set_length` 对 HTTP/1.0 + body 长度未知的情况,直接 `Encoder::length(0)`(`role.rs:1348`),意思是"HTTP/1.0 client 不发 body"(GET/HEAD/CONNECT 本来就没 body,其他 method 用户得自己设 Content-Length)。
2. **GET/HEAD/CONNECT 默认不发 body**(`role.rs:1391`):HTTP/1.1 client,body 长度未知时,对 GET/HEAD/CONNECT 用 `Encoder::length(0)`(假设没 body),对其他 method(POST/PUT/PATCH)才用 chunked。注释明说:"GET, HEAD, and CONNECT almost never have bodies. So instead of sending a 'chunked' body with a 0-chunk, assume no body here. If you *must* send a body, set the headers explicitly."

> **钉死这件事**:client 和 server 的 `Transfer-Encoding` 选择逻辑**不一样**——server 倾向"能 chunked 就 chunked,不能就 close-delimited"(因为 server 可以关连接标结束),client 倾向"能不发 body 就不发,非要发且长度未知才 chunked"(因为 client 不能关连接)。这个差异体现在 `Client::set_length` 和 `Server::encode_headers` 的不同分支上。两者都通过 `Http1Transaction::encode` 这个 trait 方法统一调用,差异完全静态分流(承 P2-05 的 `Http1Transaction` 设计)。

### 2.6 编码状态机图

把 `Encoder` 三态、`Writing` 四态、`is_last` 两态合起来,就是 HTTP/1 编码的完整状态机:

```mermaid
stateDiagram-v2
    [*] --> Init: encode_head 之前<br/>writing=Init
    Init --> BodyLength: encode_head 返回 Encoder<br/>body 长度已知 → Length(len)
    Init --> BodyChunked: encode_head 返回 Encoder<br/>body 长度未知 + 能 chunked → Chunked
    Init --> BodyClose: encode_head 返回 Encoder<br/>HTTP/1.0 + 长度未知 → CloseDelimited<br/>(仅 server)
    Init --> Done0: encode_head 返回 Encoder<br/>body 长度 0 → Length(0)<br/>writing 直接 KeepAlive/Closed

    state BodyLength {
        [*] --> WriteChunk: write_body(chunk)<br/>扣 remaining
        WriteChunk --> WriteChunk: remaining > 0
        WriteChunk --> [*]: remaining == 0<br/>或 end_body
    }
    state BodyChunked {
        [*] --> EncodeChunk: write_body(chunk)<br/>包 ChunkSize+chunk+\r\n
        EncodeChunk --> EncodeChunk: 还有 chunk
        EncodeChunk --> [*]: end_body<br/>写 0\r\n\r\n
    }
    state BodyClose {
        [*] --> WriteRaw: write_body(chunk)<br/>原样
        WriteRaw --> WriteRaw: 还有 chunk
        WriteRaw --> [*]: end_body<br/>(啥都不写)
    }

    BodyLength --> KeepAlive: end_body 且 !is_last
    BodyLength --> Closed: end_body 且 is_last<br/>(Connection: close)
    BodyLength --> Closed: end_body err<br/>(没写够 N 字节, NotEof)
    BodyChunked --> KeepAlive: end_body 且 !is_last
    BodyChunked --> Closed: end_body 且 is_last
    BodyClose --> Closed: end_body<br/>(close-delimited 必关)
    KeepAlive --> Init: try_keep_alive<br/>双侧都 KeepAlive<br/>→ 重置
    Closed --> [*]: poll_shutdown
    Done0 --> KeepAlive: !is_last
    Done0 --> Closed: is_last
```

> **钉死这张图**:`Encoder::Kind` 三态(`Length`/`Chunked`/`CloseDelimited`)决定"body 怎么编",`is_last` 两态决定"编完要不要关连接",`Writing` 四态(`Init`/`Body(Encoder)`/`KeepAlive`/`Closed`)决定"这条连接写到哪里了"。三者正交:同一个 `Writing::Body(encoder)` 状态,encoder 可能是三种 Kind 之一;同一个 Kind,end 时可能进 KeepAlive 也可能进 Closed(取决于 `is_last`)。编码状态机的全部复杂度,就是这三个维度组合出来的。

---

## 三、零拷贝写出:EncodedBuf 与 BufKind 的 Chain 艺术

第二节讲 `Encoder` 怎么选 `Transfer-Encoding`。现在拆"选定了 encoder,一个 body chunk 怎么被编成字节"。核心是 `EncodedBuf` 和它的 `BufKind`,以及背后的 `bytes::Chain`。

### 3.1 朴素做法的代价:为什么不能"拼一个大 Vec"

最朴素的编码方式是:申请一个大 `Vec<u8>`,把"chunk 大小行 + chunk 数据 + 结尾 \r\n"拼进去,再 `write` 出去。对 chunked:

```
朴素做法(每 chunk 都拷贝):
  chunk1 = "hello"  → Vec: "5\r\nhello\r\n"
  chunk2 = "world"  → Vec: "5\r\nhello\r\n5\r\nworld\r\n"
                       ^^^^^^^^^^^^^^^^^^^ 已有数据要 memmove
```

问题在"已有数据要 memmove":每来一个 chunk,都要把 chunk 的字节拷贝到那个大 Vec 里。对一个 1GB 的文件流式响应(分成 64KB 一个 chunk,约 16000 个 chunk),就是 1GB 的内存拷贝——纯浪费,因为 body 数据本身已经在用户的 `Bytes` 里了,只是要"前后加点边界标记"。

> **不这样会怎样**:朴素拼接的代价是 O(N) 的内存拷贝(N = body 总字节数)。对大文件流式响应,这个拷贝会把 CPU 和内存带宽吃光,性能远不如"零拷贝"。hyper 的目标是用 `writev`(对应 Linux 的 `writev(2)` 系统调用,一次系统调用写多段不连续内存)做到"body 数据原样不动,只在前后挂边界标记,一次 writev 吐出去"。

### 3.2 BufKind 五变体:每种编码一个 Buf 布局

`BufKind`(`encode.rs:51`)是 `EncodedBuf` 内部的五种"字节布局":

```rust
// hyper/src/proto/h1/encode.rs:51-58
#[derive(Debug)]
enum BufKind<B> {
    Exact(B),
    Limited(Take<B>),
    Chunked(Chain<Chain<ChunkSize, B>, StaticBuf>),
    ChunkedEnd(StaticBuf),
    Trailers(Chain<Chain<StaticBuf, Bytes>, StaticBuf>),
}
```

逐个看:

- **`Exact(B)`**:body 原样。用在 `Length` encoder 当 chunk 长度 ≤ remaining 时(`encode.rs:151`),和 `CloseDelimited` encoder(`encode.rs:157`)。就是用户 body 那个 `B`(通常是 `Bytes`),一字节不加。
- **`Limited(Take<B>)`**:body 截断。用在 `Length` encoder 当 chunk 长度 > remaining 时(`encode.rs:148`),`msg.take(limit)` 把 body 限制到 remaining 字节,防止"body 实际比声明的 Content-Length 长"。`Take<B>` 是 `bytes` crate 的包装器,实现 `Buf` 但 `remaining()` 返回 `min(inner.remaining(), limit)`——它不拷贝,只是"假装"原 buf 只有 limit 那么长。
- **`Chunked(Chain<Chain<ChunkSize, B>, StaticBuf>)`**:chunked 编码的一个 chunk。这是零拷贝的精华,下面单拆。
- **`ChunkedEnd(StaticBuf)`**:chunked 的结束标记 `0\r\n\r\n`(`encode.rs:120`),就是个 5 字节的 `&'static [u8]`。
- **`Trailers(Chain<Chain<StaticBuf, Bytes>, StaticBuf>)`**:chunked 带 trailer 的结束,`0\r\n` + trailer 头部 + `\r\n`(`encode.rs:205`)。

`EncodedBuf<B>`(`encode.rs:28`)就是 `BufKind<B>` 的包装,它实现了 `Buf` trait(`encode.rs:282-329`),所有方法都 `match self.kind` 转发到内部的 `Chain`/`Take`/`B`。这样 `EncodedBuf` 对外就是一个统一的 `Buf`,`WriteBuf` 不用关心内部是哪种布局。

### 3.3 Chunked 的零拷贝:Chain 三段链

`Chunked` 变体的核心是 `Chain<Chain<ChunkSize, B>, StaticBuf>`。展开就是"三段链":

```
Chunked 编码一个 chunk 的字节布局(零拷贝 Chain):

  ┌─ChunkSize─┐  ┌──── B (用户 body chunk) ────┐  ┌StaticBuf┐
  │ "5\r\n"   │  │ "hello"                     │  │ "\r\n"  │
  └───────────┘  └─────────────────────────────┘  └─────────┘
       ▲                       ▲                       ▲
       │                       │                       │
   栈上数组              用户 Bytes 的引用          &'static [u8]
  (ChunkSize             (不拷贝, 只持有引用)       (编译期常量)
   结构体)

  Chain.chain 把三段链成一个 Buf, 剩余长度 = 5 + 5 + 2 = 12
  writev 一次吐出 12 字节, 中间一字节都不拷贝
```

这三段是怎么链起来的?看 `Encoder::encode`(`encode.rs:128`):

```rust
// hyper/src/proto/h1/encode.rs:128-161 (摘录)
pub(crate) fn encode<B>(&mut self, msg: B) -> EncodedBuf<B>
where
    B: Buf,
{
    let len = msg.remaining();
    debug_assert!(len > 0, "encode() called with empty buf");

    let kind = match self.kind {
        Kind::Chunked(_) => {
            trace!("encoding chunked {}B", len);
            let buf = ChunkSize::new(len)      // 1. 建 chunk 大小行: "{len:X}\r\n"
                .chain(msg)                    // 2. 链上用户 body: Chain<ChunkSize, B>
                .chain(b"\r\n" as &'static [u8]); // 3. 链上结尾: Chain<Chain<ChunkSize, B>, &str>
            BufKind::Chunked(buf)
        }
        Kind::Length(ref mut remaining) => {
            trace!("sized write, len = {}", len);
            if len as u64 > *remaining {
                let limit = *remaining as usize;
                *remaining = 0;
                BufKind::Limited(msg.take(limit))   // 截断
            } else {
                *remaining -= len as u64;
                BufKind::Exact(msg)                 // 原样
            }
        }
        #[cfg(feature = "server")]
        Kind::CloseDelimited => {
            trace!("close delimited write {}B", len);
            BufKind::Exact(msg)                     // 原样
        }
    };
    EncodedBuf { kind }
}
```

关键两行:

```rust
let buf = ChunkSize::new(len)               // ChunkSize: 栈上 [u8; 18], 持有 "{len:X}\r\n"
    .chain(msg)                             // Chain<ChunkSize, B>: 两段
    .chain(b"\r\n" as &'static [u8]);       // Chain<Chain<ChunkSize, B>, &'static [u8]>: 三段
```

`Chain` 是 `bytes` crate 的类型(`bytes::buf::Chain`),它持两个 `Buf`,实现 `Buf` trait 时:`remaining()` = 两段之和,`chunk()` 返回第一段(第一段空了返回第二段),`advance()` 先推进第一段、空了推进第二段,`chunks_vectored()` 把两段的 slice 都填进 iov 数组。`A.chain(B).chain(C)` 嵌套两层 `Chain`,就是三段链。

> **对照 `bytes::Chain`**:`Chain` 是 `bytes` crate 提供的零拷贝拼接原语(本书 P6-17 招牌章会深拆 `bytes`)。它的精髓是"不拷贝字节,只持引用/所有权":`Chain<ChunkSize, B>` 持有 `ChunkSize`(栈上数组,by value)和 `B`(用户 body,by value,通常 `B = Bytes` 是引用计数的),两者各自的内存不动,`Chain` 只是把它们"逻辑上"拼一起。`chunks_vectored` 把两段的 slice 填进 `IoSlice` 数组,`writev` 一次系统调用写出去——内核负责把不连续的内存段拼成连续的字节流发到网卡,用户态零拷贝。

### 3.4 ChunkSize:栈上的 hex 格式化小能手

`ChunkSize`(`encode.rs:341`)是 chunked 编码里"chunk 大小行"(`{hex 长度}\r\n`)的载体。它是个栈上定长数组,不分配堆内存:

```rust
// hyper/src/proto/h1/encode.rs:331-358 (摘录)
#[cfg(target_pointer_width = "32")]
const USIZE_BYTES: usize = 4;
#[cfg(target_pointer_width = "64")]
const USIZE_BYTES: usize = 8;
// each byte will become 2 hex
const CHUNK_SIZE_MAX_BYTES: usize = USIZE_BYTES * 2;

#[derive(Clone, Copy)]
struct ChunkSize {
    bytes: [u8; CHUNK_SIZE_MAX_BYTES + 2],   // 64 位上 18 字节: 最多 16 hex + "\r\n"
    pos: u8,
    len: u8,
}

impl ChunkSize {
    fn new(len: usize) -> ChunkSize {
        use std::fmt::Write;
        let mut size = ChunkSize {
            bytes: [0; CHUNK_SIZE_MAX_BYTES + 2],
            pos: 0,
            len: 0,
        };
        write!(&mut size, "{len:X}\r\n").expect("CHUNK_SIZE_MAX_BYTES should fit any usize");
        size
    }
}
```

两个细节:

1. **`CHUNK_SIZE_MAX_BYTES = USIZE_BYTES * 2`**:64 位上 usize 最多 16 个 hex 字符(2^64 - 1 = FFFFFFFFFFFFFFFF),加 `\r\n` 共 18 字节。这个数组大小是**精确**算出来的——`expect` 那句"CHUNK_SIZE_MAX_BYTES should fit any usize"就是断言"任何 usize 的 hex 表示都装得下"。
2. **`write!(&mut size, "{len:X}\r\n")`**:`ChunkSize` 实现了 `fmt::Write`(`encode.rs:387`),所以可以用 `write!` 宏把 `len` 格式化成大写 hex(`{len:X}`,比如 255 → "FF")写进 `bytes` 数组。这比 `format!("{len:X}\r\n")` 快——后者要分配一个 `String`,`ChunkSize` 直接写栈数组。

> **钉死这件事**:`ChunkSize` 是"栈上、零分配、精确大小"的 hex 格式化 buffer。它体现了 hyper 在编码路径上"抠每一纳秒"的功夫——chunk 大小行每个 chunk 都要编一次,如果每次都 `format!` 分配 String,大文件流式响应(上万 chunk)就是上万次堆分配。`ChunkSize` 用栈数组 + `fmt::Write` 把这次分配消掉了。读 hyper 编码路径,会看到很多这样的"小而精"的优化。

### 3.5 Chain 怎么配合 writev:零拷贝的最后一公里

`EncodedBuf` 实现了 `Buf` 的 `chunks_vectored`(`encode.rs:320`),它转发到内部 `Chain` 的 `chunks_vectored`。`Chain` 的 `chunks_vectored` 会把两段(或嵌套三段)的 slice 都填进 `IoSlice` 数组。`WriteBuf` 也实现了 `chunks_vectored`(`io.rs:635`),它把 `headers`(一段)和 `queue` 里所有 `EncodedBuf`(每段最多若干 slice)都填进 iov 数组。

最终,`Buffered::poll_flush`(`io.rs:266`)在 Queue 策略下走 `poll_write_vectored`:

```rust
// hyper/src/proto/h1/io.rs:266-300 (摘录)
pub(crate) fn poll_flush(&mut self, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
    if self.flush_pipeline && !self.read_buf.is_empty() {
        Poll::Ready(Ok(()))
    } else if self.write_buf.remaining() == 0 {
        Pin::new(&mut self.io).poll_flush(cx)
    } else {
        if let WriteStrategy::Flatten = self.write_buf.strategy {
            return self.poll_flush_flattened(cx);
        }
        const MAX_WRITEV_BUFS: usize = 64;
        loop {
            let n = {
                let mut iovs = [IoSlice::new(&[]); MAX_WRITEV_BUFS];
                let len = self.write_buf.chunks_vectored(&mut iovs);
                ready!(Pin::new(&mut self.io).poll_write_vectored(cx, &iovs[..len]))?
            };
            self.write_buf.advance(n);
            debug!("flushed {} bytes", n);
            if self.write_buf.remaining() == 0 {
                break;
            } else if n == 0 {
                /* ... WriteZero 错误 */
            }
        }
        Pin::new(&mut self.io).poll_flush(cx)
    }
}
```

`MAX_WRITEV_BUFS = 64`:一次 `writev` 最多写 64 段不连续内存。这和 Linux 内核 `writev` 的 `UIO_MAXIOV`(1024)比很小,但够用——一个响应的"头部 + 几个 body chunk"通常不到 64 段。如果 `chunks_vectored` 填满 64 段还有剩余,这个 `loop` 会多转几圈,每圈 advance 已写的、继续填 iov。

> **承接《Tokio》**:`poll_write_vectored` 是 `AsyncWrite` 的扩展方法(在 hyper 的 `rt::Write` trait 里,Tokio 的 `AsyncWrite` 提供)。它对应底层 `writev(2)` 系统调用——一次 syscall 写多段内存到 fd。Tokio 的 reactor 负责"socket 不可写时挂起 task,可写时唤醒",这个机制《Tokio》拆透了,本章一句带过。hyper 的工作是在 `poll_flush` 里把 `WriteBuf` 的所有段(头部 + 各 body chunk 的 Chain 各段)填进 iov 数组,让一次 `writev` 尽量吐更多字节——这是"批量写"换 syscall 数量的招牌优化。

### 3.6 零拷贝的边界:什么时候会拷贝

零拷贝不是绝对的,有两种情况 hyper 会"拷贝":

1. **Flatten 策略**(`io.rs:544-567`):如果底层 IO 不支持 `writev`(`io.is_write_vectored()` 返回 false,某些自定义 IO),hyper 退化成 Flatten 策略——`WriteBuf::buffer` 把每个 `EncodedBuf` 的字节 `extend_from_slice` 拷贝到 `headers` 那个 `Vec<u8>` 里,flush 时一次 `poll_write`(单段)。这损失了零拷贝,但兼容不支持 writev 的 IO。`set_flush_pipeline` 也会强制 Flatten(`io.rs:82`)。
2. **headers 本身就是 `Vec<u8>`**:头部(状态行 + 头部)是 hyper 自己拼的(`extend` 一路写到 `headers_buf`),这部分本来就是"拷贝"——从 `HeaderMap` 拷到 `Vec<u8>`。但头部字节数小(几十到几百字节),拷贝开销可忽略;真正大的是 body,body 走 Chain 零拷贝。

> **钉死这件事**:hyper 的零拷贝是"body 零拷贝,头部小拷贝"。头部必须拷(从 HeaderMap 拼成连续字节流),但它小;body 用 Chain + writev 做到真正零拷贝,它大。这个分工是性能最优的:不为小数据搞复杂的零拷贝(Chain 的开销可能比拷贝还大),不为大数据搞朴素的拷贝(那会吃光内存带宽)。

---

## 四、流式编码:Writing 状态机与 body 的边编边写

第三节讲"一个 chunk 怎么零拷贝编成字节"。现在拆"整个 body 怎么流式编——header 先写、body 一个 Frame 一个 Frame 编、最后 end"。核心是 `Writing` 状态机和 `Dispatcher::poll_write` 的 body 分支。

### 4.1 为什么不能"等 body 到齐再编"

第二节提过,body 是流式的——Service 的 Future 算出 Response 时,Response 的 body 是个 `Body`(`http-body` crate 的 trait),它按 Frame 产出数据。一个 Frame 可能是 `data`(一个 chunk 的字节)、`trailers`(结尾的 trailer 头部)。body 不一定立刻就绪——比如 body 来自数据库查询的流式结果,要等 DB 返回一行才能产出一个 Frame。

如果"等整个 body 到齐再编",意味着 `Dispatcher` 要把所有 Frame 攒到一个 `Vec` 里,body 多大就占多大内存——对 1GB 的文件响应,就是 1GB 内存。而且 Service 算 body 可能要 10 秒,这 10 秒里 client 一个字节都收不到,TTFB(Time To First Byte)灾难。

> **不这样会怎样**:等 body 到齐再编,① 内存爆炸(body 多大占多大),② TTFB 灾难(client 等 body 算完才收到第一个字节),③ 流式语义丧失(server-side event / chunked 的"边产边发"优势没了)。HTTP/1 的 chunked 编码设计初衷就是"支持流式 body",hyper 必须把这个优势发挥出来。

### 4.2 Writing 状态机:Body 这一段

`Writing`(`conn.rs:972`,P2-05 拆过)四态:`Init` / `Body(Encoder)` / `KeepAlive` / `Closed`。编码侧主要看 `Body(Encoder)` 这一段——它持有 `Encoder`,body 的每个 chunk 经 `encoder.encode(chunk)` 包成 `EncodedBuf`,塞进 `WriteBuf`,`Encoder` 内部扣账(Length)或不变(Chunked/CloseDelimited)。

`write_head`(`conn.rs:595`)是进入 `Body` 状态的入口:

```rust
// hyper/src/proto/h1/conn.rs:595-605
pub(crate) fn write_head(&mut self, head: MessageHead<T::Outgoing>, body: Option<BodyLength>) {
    if let Some(encoder) = self.encode_head(head, body) {
        self.state.writing = if !encoder.is_eof() {
            Writing::Body(encoder)        // body 没结束 → 进入 Body 状态, 持有 encoder
        } else if encoder.is_last() {
            Writing::Closed                // body 长度 0 + 要关连接 → 直接 Closed
        } else {
            Writing::KeepAlive             // body 长度 0 + keep-alive → 直接 KeepAlive
        };
    }
}
```

注意 `encoder.is_eof()`(`encode.rs:90`):只有 `Kind::Length(0)` 返回 true(长度为 0 的 body,encoder 一建出来就"EOF"了)。chunked 和 close-delimited 的 `is_eof()` 永远 false(它们要等 `end_body` 才真结束)。所以:

- **body 长度已知为 0**(`Encoder::length(0)`):`is_eof()` true,`write_head` 直接进 `KeepAlive` 或 `Closed`,不进 `Body`——没有 body 要写,头部编完这条响应就完了。
- **body 长度已知 > 0**(`Encoder::length(len)`,len > 0)或 **chunked** 或 **close-delimited**:`is_eof()` false,进 `Body(encoder)`,等后续 `write_body` 一个一个写。

### 4.3 write_body:一个 chunk 的编码入队

`write_body`(`conn.rs:704`)把一个 chunk 经 encoder 编码,塞进 `WriteBuf`:

```rust
// hyper/src/proto/h1/conn.rs:704-727
pub(crate) fn write_body(&mut self, chunk: B) {
    debug_assert!(self.can_write_body() && self.can_buffer_body());
    // empty chunks should be discarded at Dispatcher level
    debug_assert!(chunk.remaining() != 0);

    let state = match self.state.writing {
        Writing::Body(ref mut encoder) => {
            self.io.buffer(encoder.encode(chunk));   // 编码 + 入队

            if !encoder.is_eof() {
                return;                              // encoder 没 EOF, 继续 Body
            }

            if encoder.is_last() {
                Writing::Closed                      // length 写够了 + is_last → Closed
            } else {
                Writing::KeepAlive                   // length 写够了 + !is_last → KeepAlive
            }
        }
        _ => unreachable!("write_body invalid state: {:?}", self.state.writing),
    };

    self.state.writing = state;
}
```

关键三步:

1. **`encoder.encode(chunk)`**:返回 `EncodedBuf<B>`(零拷贝 Chain,第三节拆过)。
2. **`self.io.buffer(encoded_buf)`**:塞进 `WriteBuf` 的队列(Queue 策略)或 extend 进 `headers`(Flatten 策略)。`buffer`(`io.rs:148`)转发到 `WriteBuf::buffer`(`io.rs:542`)。
3. **检查 `encoder.is_eof()`**:`Length` encoder 写够 remaining 字节后 `is_eof()` 返回 true(remaining 归 0),这时根据 `is_last` 进 `KeepAlive` 或 `Closed`。`Chunked` 和 `CloseDelimited` 的 `is_eof()` 永远 false(它们要等 `end_body`)——所以 chunked 的 `write_body` 永远 `return` 在 `if !encoder.is_eof()` 那行,状态保持 `Body`,直到 `end_body` 显式收尾。

> **钉死这件事**:`write_body` 是"编码一个 chunk + 入队"的原子操作。它**不 flush**——字节进了 `WriteBuf` 但没到 socket,要等 `poll_flush`(第五节拆)。这是"攒一批再 flush"的设计:多个 chunk 可以攒在 `WriteBuf` 里,一次 `writev` 吐出去,比"每个 chunk 一次 write"省 syscall。`write_body` 只负责"编 + 入队",flush 时机由 `Dispatcher::poll_loop` 的 `poll_flush` 轮决定。

### 4.4 end_body:chunked 的收尾

`end_body`(`conn.rs:774`)是 body 流结束的显式收尾。它调用 `encoder.end()`(`encode.rs:116`):

```rust
// hyper/src/proto/h1/encode.rs:116-126
pub(crate) fn end<B>(&self) -> Result<Option<EncodedBuf<B>>, NotEof> {
    match self.kind {
        Kind::Length(0) => Ok(None),                              // length 已写够, 啥都不加
        Kind::Chunked(_) => Ok(Some(EncodedBuf {                  // chunked 写 0\r\n\r\n
            kind: BufKind::ChunkedEnd(b"0\r\n\r\n"),
        })),
        #[cfg(feature = "server")]
        Kind::CloseDelimited => Ok(None),                        // close-delimited 啥都不加
        Kind::Length(n) => Err(NotEof(n)),                        // length 没写够, 错误
    }
}
```

三种 `Kind` 的收尾:

- **`Length(0)`**:body 已经写够(remaining 归 0),`end` 返回 `Ok(None)`——不加任何字节。
- **`Chunked`**:返回 `Some(ChunkedEnd(b"0\r\n\r\n"))`——5 字节的 chunked 结束标记 `0\r\n\r\n`(0 长度 chunk + 结尾 `\r\n`)。这个 `EncodedBuf` 被 `end_body` 塞进 `WriteBuf`(`conn.rs:786`),flush 时写出。
- **`CloseDelimited`**:返回 `Ok(None)`——close-delimited 靠关连接标结束,不加任何字节。
- **`Length(n)`(n > 0)**:返回 `Err(NotEof(n))`——意思是"你承诺了 N 字节,实际只给了 N - n 字节"。这是用户 body 提前 EOF 的错误,`end_body`(`conn.rs:798`)把它转成 `crate::Error::new_body_write_aborted().with(not_eof)`,状态进 `Writing::Closed`,连接关闭。

`end_body`(`conn.rs:774`)本身:

```rust
// hyper/src/proto/h1/conn.rs:774-802 (摘录)
pub(crate) fn end_body(&mut self) -> crate::Result<()> {
    debug_assert!(self.can_write_body());
    let encoder = match self.state.writing {
        Writing::Body(ref mut enc) => enc,
        _ => return Ok(()),
    };
    match encoder.end() {
        Ok(end) => {
            if let Some(end) = end {
                self.io.buffer(end);                    // 把 0\r\n\r\n 入队
            }
            self.state.writing = if encoder.is_last() || encoder.is_close_delimited() {
                Writing::Closed                         // close-delimited 或 is_last → Closed
            } else {
                Writing::KeepAlive                      // 否则 KeepAlive
            };
            Ok(())
        }
        Err(not_eof) => {
            self.state.writing = Writing::Closed;
            Err(crate::Error::new_body_write_aborted().with(not_eof))
        }
    }
}
```

注意 `encoder.is_close_delimited()`(`encode.rs:104`):只有 `Kind::CloseDelimited` 返回 true。所以 close-delimited 的 `end_body` 即使 `encoder.end()` 返回 `Ok(None)`(不加字节),状态也进 `Closed`——因为 close-delimited 必须关连接(这是它的语义)。

### 4.5 write_body_and_end:一个 chunk 一次写完的快路径

`write_body_and_end`(`conn.rs:754`)是"一个 chunk 就是整个 body"的快路径——body 只有一个 chunk,编码 + 收尾一次完成:

```rust
// hyper/src/proto/h1/conn.rs:754-772
pub(crate) fn write_body_and_end(&mut self, chunk: B) {
    debug_assert!(self.can_write_body() && self.can_buffer_body());
    debug_assert!(chunk.remaining() != 0);

    let state = match self.state.writing {
        Writing::Body(ref encoder) => {
            let can_keep_alive = encoder.encode_and_end(chunk, self.io.write_buf());
            if can_keep_alive {
                Writing::KeepAlive
            } else {
                Writing::Closed
            }
        }
        _ => unreachable!("write_body invalid state: {:?}", self.state.writing),
    };
    self.state.writing = state;
}
```

`encode_and_end`(`encode.rs:219`)是 `encode` + `end` 的融合版,关键在 chunked:它把"chunk + `\r\n` + 结束标记 `0\r\n\r\n`"链成**一个** Chain,一次入队:

```rust
// hyper/src/proto/h1/encode.rs:219-262 (摘录 chunked 分支)
pub(super) fn encode_and_end<B>(&self, msg: B, dst: &mut WriteBuf<EncodedBuf<B>>) -> bool
where
    B: Buf,
{
    let len = msg.remaining();
    match self.kind {
        Kind::Chunked(_) => {
            let buf = ChunkSize::new(len)
                .chain(msg)
                .chain(b"\r\n0\r\n\r\n" as &'static [u8]);   // 一个 Chain: chunk + \r\n + 0\r\n\r\n
            dst.buffer(buf);
            !self.is_last
        }
        Kind::Length(remaining) => { /* ... 三分支: Equal/Greater/Less */ }
        #[cfg(feature = "server")]
        Kind::CloseDelimited => { /* ... */ }
    }
}
```

对比 `write_body` + `end_body` 两步(编一个 chunk、再编结束标记,两个 `EncodedBuf`),`encode_and_end` 一步把 chunk 和结束标记链成**一个** Chain——少一个 `EncodedBuf`,少一次入队操作,writev 时少几段 iov。对"body 只有一个 chunk"(常见的小响应,如 JSON API)这是明显的快路径。

`Dispatcher::poll_write`(`dispatch.rs:402`)在 body `is_end_stream()` 且 chunk 非空时走这条快路径:

```rust
// hyper/src/proto/h1/dispatch.rs:399-409 (摘录)
if frame.is_data() {
    let chunk = frame.into_data().unwrap_or_else(|_| unreachable!());
    let eos = body.is_end_stream();
    if eos {
        *clear_body = true;
        if chunk.remaining() == 0 {
            trace!("discarding empty chunk");
            self.conn.end_body()?;
        } else {
            self.conn.write_body_and_end(chunk);    // 快路径: 一个 chunk 就是整个 body
        }
    } else {
        if chunk.remaining() == 0 {
            trace!("discarding empty chunk");
            continue;
        }
        self.conn.write_body(chunk);                // 普通路径: 还会有 chunk
    }
}
```

注意还有个细节:**空 chunk 被丢弃**(`chunk.remaining() == 0` 时 `continue` 或 `end_body`)。这是因为 chunked 编码里"空 chunk"会编出 `0\r\n\r\n`(被误当成结束标记)或 `0\r\n` + 数据(混乱),所以 hyper 在 Dispatcher 层就把空 chunk 丢了(`dispatch.rs:859` 有个测试专门验证这点)。这是个协议正确性的小守护。

> **钉死这件事**:body 流式编码有三条路径——① `write_body`(普通 chunk,编 + 入队,encoder 不 EOF),② `end_body`(body 流结束,chunked 加 `0\r\n\r\n`,length 检查没写够),③ `write_body_and_end`(快路径,一个 chunk 就是整个 body,编 + 结束一次完成)。三条路径都"编 + 入队",不 flush;都由 `Writing::Body(Encoder)` 状态机的不同迁移触发。空 chunk 在 Dispatcher 层被丢弃,防止 chunked 协议错乱。

### 4.6 流式编码时序:一个 chunked 响应的完整编码

把前几节合起来,看一个"chunked 响应,3 个 chunk"的完整编码时序:

```mermaid
sequenceDiagram
    autonumber
    participant Svc as Service<br/>(Body Stream)
    participant Disp as Dispatcher<br/>poll_write
    participant Conn as Conn<br/>(Writing 状态机)
    participant Enc as Encoder<br/>(Chunked)
    participant WB as WriteBuf<br/>(headers + queue)
    participant IO as Buffered IO<br/>poll_flush → writev

    Note over Svc,IO: 阶段 1: 编头 (body 长度未知 → chunked)
    Svc-->>Disp: poll_msg → (ResponseHead, Body)
    Disp->>Disp: body.size_hint().exact() = None<br/>→ BodyLength::Unknown
    Disp->>Conn: write_head(head, Some(Unknown))
    Conn->>Conn: encode_head → Server::encode<br/>写 "HTTP/1.1 200 OK\r\n" + 头<br/>+ "transfer-encoding: chunked\r\n\r\n" 到 headers_buf
    Conn->>Enc: Encoder::chunked() → is_eof() false
    Conn->>Conn: writing = Body(Encoder::chunked())

    Note over Svc,IO: 阶段 2: body 流式编码 (一个 Frame 一个 chunk)
    loop chunk 1, 2, 3
        Disp->>Svc: body.poll_frame
        Svc-->>Disp: Frame::data(chunk)
        Disp->>Conn: write_body(chunk)
        Conn->>Enc: encode(chunk) → EncodedBuf<br/>Chain<ChunkSize, chunk, "\r\n">
        Conn->>WB: buffer(encoded_buf) → queue.push
        Note over Conn: writing 仍 Body<br/>encoder.is_eof() false
    end

    Note over Svc,IO: 阶段 3: body 结束
    Svc-->>Disp: poll_frame → None (body EOF)
    Disp->>Conn: end_body()
    Conn->>Enc: end() → Some(ChunkedEnd "0\r\n\r\n")
    Conn->>WB: buffer(chunked_end) → queue.push
    Conn->>Conn: writing = KeepAlive (is_last false)

    Note over Svc,IO: 阶段 4: flush (一次 writev 吐全部)
    Disp->>Conn: poll_flush (poll_loop 触发)
    Conn->>IO: poll_flush
    IO->>WB: chunks_vectored → 填 iov<br/>[headers, ChunkSize1, chunk1, "\r\n",<br/> ChunkSize2, chunk2, "\r\n", ...<br/> "0\r\n\r\n"]
    IO->>IO: poll_write_vectored(iovs)<br/>一次 syscall 吐全部字节
    IO-->>Conn: flushed N bytes
```

这张时序图就是"chunked 响应流式编码"的完整画面。注意几个"为什么 sound":

- **头部先编、body 边来边编**:头部在 `write_head` 那一刻编完(写进 `headers_buf`),body 的每个 chunk 在 `write_body` 边来边编(各编各的 `EncodedBuf` 入队)。头部和 body 的 `EncodedBuf` 在 `WriteBuf` 里按顺序排好(headers 在前,queue 里 body chunk 在后),flush 时 `chunks_vectored` 按顺序填 iov,保证字节顺序对。
- **chunked 边界不混乱**:每个 chunk 独立 `encode`,独立 `EncodedBuf`(Chain: ChunkSize + chunk + \r\n),边界标记和 chunk 数据绑死在一个 `EncodedBuf` 里——不可能"chunk3 的边界标记跑到 chunk4 后面"。这是 `EncodedBuf` 作为"原子编码单元"的 sound 保证。
- **结束标记 `0\r\n\r\n` 一定在最后**:`end_body` 在 body 流 EOF(`poll_frame` 返回 None)时调用,它编出的 `ChunkedEnd` 入队,排在所有 chunk 后面。flush 时它最后写出,client 收到 `0\r\n\r\n` 就知道 body 结束。

> **钉死这张图**:这是"流式编码 sound"的完整证明。① 头部先编(头部时刻承诺 Transfer-Encoding),② body 边来边编(每个 chunk 独立 EncodedBuf,边界不混乱),③ 结束标记最后编(紧跟 body EOF)。三者靠 `WriteBuf` 的 FIFO 队列保证字节顺序,靠 `EncodedBuf` 的 Chain 保证边界标记和数据绑定。这就是 hyper 怎么在"body 流式、不等到齐"的前提下,编出协议合法的 chunked 响应。

---

## 五、flush 策略:什么时候把字节吐到 socket

第四节讲"编 + 入队",字节进了 `WriteBuf` 但没到 socket。现在拆"什么时候 flush"——这是延迟和吞吐的权衡。

### 5.1 flush 的三层触发

`poll_flush` 在 `Dispatcher::poll_loop`(`dispatch.rs:175`)里每圈都调:

```rust
// hyper/src/proto/h1/dispatch.rs:170-190 (摘录)
for _ in 0..16 {
    let _ = self.poll_read(cx)?;
    let write_ready = self.poll_write(cx)?.is_ready();
    let flush_ready = self.poll_flush(cx)?.is_ready();    // 每圈都 flush
    let wants_write_again = self.can_write_again() && (write_ready || flush_ready);
    let wants_read_again = self.conn.wants_read_again();
    if !(wants_write_again || wants_read_again) {
        return Poll::Ready(Ok(()));
    }
    /* ... */
}
```

但"每圈调 `poll_flush`"不等于"每圈都真吐字节"——`Buffered::poll_flush`(`io.rs:266`)有自己的判断:

1. **`write_buf.remaining() == 0`**:写缓冲空,没什么可吐,直接转发 `io.poll_flush`(把 Tokio 内部 buffer 的字节推到 OS)。
2. **`flush_pipeline && !read_buf.is_empty()`**:pipeline 模式 + 读缓冲有字节,跳过 flush(下面 5.3 拆)。
3. **否则**:真吐字节——Queue 策略走 `poll_write_vectored` loop,Flatten 策略走 `poll_flush_flattened`。

所以"flush 时机"实际由三个条件控制:

- **背压**:`can_buffer_body()`(`conn.rs:591` → `io.rs:152` → `WriteBuf::can_buffer` `io.rs:579`)返回 false 时,`Dispatcher::poll_write` 会先 `poll_flush` 腾地方(`dispatch.rs:376`)。`can_buffer` 在 Queue 策略下是"`queue.bufs_cnt() < MAX_BUF_LIST_BUFFERS(16)` 且 `remaining < max_buf_size`"——攒够 16 个 buf 或 max_buf_size 字节就强制 flush,防写缓冲无限增长吃光内存。
- **`poll_loop` 每圈 flush**:每圈(最多 16 圈)都 `poll_flush`,把能吐的都吐了。
- **`pipeline_flush` 模式**:server 配置项,开启后"写完头立刻 flush",换更低 TTFB。

### 5.2 背压:MAX_BUF_LIST_BUFFERS 和 max_buf_size

`WriteBuf::can_buffer`(`io.rs:579`)是背压的核心:

```rust
// hyper/src/proto/h1/io.rs:579-586
fn can_buffer(&self) -> bool {
    match self.strategy {
        WriteStrategy::Flatten => self.remaining() < self.max_buf_size,
        WriteStrategy::Queue => {
            self.queue.bufs_cnt() < MAX_BUF_LIST_BUFFERS && self.remaining() < self.max_buf_size
        }
    }
}
```

两个阈值:

- **`MAX_BUF_LIST_BUFFERS = 16`**(`io.rs:30`):Queue 策略下,`WriteBuf.queue` 里最多攒 16 个 `EncodedBuf`(每个对应一个 body chunk 或头部或结束标记)。超过就 `can_buffer` 返回 false,`Dispatcher::poll_write` 强制 flush。
- **`max_buf_size`**(默认 `DEFAULT_MAX_BUFFER_SIZE = 8192 + 4096 * 100 = 417792`,约 408KB,`io.rs:23`):写缓冲总字节数上限。超过就强制 flush。

> **钉死这件事**:`MAX_BUF_LIST_BUFFERS = 16` 是个"批量化"的权衡——攒 16 个 buf 一次 writev,比每个 buf 一次 write 省 15 次 syscall;但也不能攒太多(比如 1000 个),否则延迟太高(client 等 1000 个 chunk 攒齐才收到第一个字节)。16 和 `poll_loop` 的 `for _ in 0..16` 是一个数量级,都是 hyper 在"批量"和"延迟"之间的经验值。`max_buf_size` 408KB 是内存背压——防一个连接的写缓冲吃光内存。

### 5.3 pipeline_flush:写完头立刻 flush 的低延迟模式

`set_flush_pipeline`(`io.rs:78`)是 server 的一个配置项(`server/conn/http1.rs:491` 的 `self.pipeline_flush`)。开启后:

```rust
// hyper/src/proto/h1/io.rs:78-84
#[cfg(feature = "server")]
pub(crate) fn set_flush_pipeline(&mut self, enabled: bool) {
    debug_assert!(!self.write_buf.has_remaining());
    self.flush_pipeline = enabled;
    if enabled {
        self.set_write_strategy_flatten();   // pipeline 模式强制 Flatten
    }
}
```

两个效果:

1. **`flush_pipeline = true`**:`poll_flush`(`io.rs:267`)看到 `flush_pipeline && !read_buf.is_empty()` 时**跳过 flush**(返回 Ready Ok)——这是"读侧有字节时,别在写侧浪费时间 flush,赶紧去读"。配合 `poll_loop` 的"读优先",让 server 在流水线场景下优先读下一个请求。
2. **强制 Flatten**:`pipeline_flush` 模式下 `set_write_strategy_flatten`,所有字节拷贝到 `headers` 那个 `Vec<u8>`。为什么?因为 pipeline 模式下 hyper 想"每个响应立刻完整 flush 出去"(头部 + body 一起),不攒——Flatten 把所有字节拼一个连续 `Vec`,一次 `poll_write`(单段)就吐完,比 Queue 的多段 writev 更适合"小响应立刻发"。

`pipeline_flush` 的语义其实是 "HTTP/1.1 流水线模式的优化":开启后 server 更激进地 flush(每个响应尽量早发),牺牲一点写吞吐(writev 批量小)换更低 TTFB。默认关闭——大多数场景(非流水线)用 Queue + 攒批量更高效。

> **不这样会怎样**:不开 `pipeline_flush`,默认 Queue 策略攒 16 个 buf 才 flush——对小响应(JSON API,一个 chunk),可能要等"16 个响应的字节攒齐"才 flush,TTFB 高。开 `pipeline_flush`,每个响应写完立刻 flush,TTFB 低,但 syscall 多(每个响应一次 write)。这是延迟 vs 吞吐的经典权衡,hyper 把选择权交给用户(`Http::pipeline_flush(true)`)。

### 5.4 flush 时机为什么不影响正确性

一个关键的 sound 保证:**flush 时机只影响延迟,不影响协议正确性**。为什么?

因为协议(HTTP/1.1)只要求"字节顺序对"——client 按字节流解析,只要 `HTTP/1.1 200 OK\r\n...\r\n\r\n` 在前、body 字节在后、chunked 的 `0\r\n\r\n` 在最后,client 就能正确解析。至于这些字节是"一次 write 全发"还是"分十次 write 发"、是"写完头立刻 flush"还是"攒 16 个 buf 一起 flush",client 不关心——TCP 保证字节流顺序,client 看到的就是连续的字节流。

hyper 的 `WriteBuf` FIFO 队列保证字节顺序:头部先入队(`headers_buf`)、body chunk 按 `write_body` 顺序入队(`queue.push`)、结束标记最后入队(`end_body`)。flush 时 `chunks_vectored` 按队列顺序填 iov,writev 按 iov 顺序写字节。所以无论 flush 多频繁,字节顺序永远对。

> **钉死这件事**:flush 时机是**纯延迟权衡**,不是正确性问题。hyper 的设计把"编码(保证字节顺序和协议合法)"和"flush(决定何时吐到 socket)"完全解耦——编码只管"编对了入队",flush 只管"什么时候吐"。这让 hyper 可以在不同场景灵活调 flush 策略(`pipeline_flush` 换低延迟,默认攒批量换高吞吐),而不影响协议正确性。这是分层抽象的红利:协议层和 IO 层各管一段。

---

## 六、对照:gRPC chttp2 与 Envoy HCM 的编码路径

讲完 hyper,对照另外两个系统的编码路径,凸显 hyper 的取舍。

### 6.1 对照 gRPC chttp2 的序列化

gRPC 的 C++ core 自己实现 HTTP/2(chttp2),它的"编码"和 hyper 完全不同维度:

| 维度 | hyper HTTP/1 | gRPC chttp2 HTTP/2 |
|------|--------------|---------------------|
| 头部编码 | 明文拼接(`extend` 到 `Vec<u8>`) | HPACK 压缩(静态表/动态表/Huffman,承《gRPC》P2-06) |
| body 分帧 | chunked(`{hex}\r\n` + data + `\r\n`)或 length(原样) | DATA 帧(9 字节帧头: 长度/type/flags/streamId + payload) |
| 多路复用 | 无(一条连接一个响应,串行) | 有(一条连接并发多条 stream,每帧带 streamId) |
| 流控 | 无(靠 TCP 窗口) | 有(WINDOW_UPDATE/SETTINGS_INITIAL_WINDOW_SIZE,承《gRPC》P2-09) |
| 结束标记 | chunked `0\r\n\r\n` 或 close-delimited(关连接) | DATA 帧带 END_STREAM flag 或 HEADERS 帧带 END_STREAM |
| 零拷贝 | `bytes::Chain` + writev | grpc_slice 引用计数 + 切片(承《内存分配器》) |

> **承接《gRPC》**:HTTP/2 的帧/流/HPACK/流控在《gRPC》第 2 篇拆透了,本章一句带过。关键差异是:HTTP/1 编码是"明文 + 边界标记",简单但无压缩无多路复用;HTTP/2 编码是"二进制帧 + HPACK 压缩 + 流控",复杂但高效。hyper 在 HTTP/1 上做到的"零拷贝 Chain + 流式",gRPC 在 HTTP/2 上用 `grpc_slice` + 帧分装做到——思路相通(不拷贝 body 数据,只加边界标记),实现不同(Chain vs slice,writev vs DATA 帧)。

### 6.2 对照 Envoy HCM 的 encode 路径

Envoy 的 HTTP Connection Manager(HCM)是 C++ 写的 HTTP/1(+HTTP/2)状态机,它的 encode 路径和 hyper 有趣的对照:

- **filter chain**:HCM 的 encode 是一条 filter chain(`encoder_filters`),响应经过鉴权/压缩/日志等 filter 层层处理,每层可以改 headers/body。hyper 的对应是 Tower middleware(P1-03 拆),也是层 filter,但 hyper 的 filter 作用在 `Response` 还没编码之前(改 `HeaderMap`/`Body`),HCM 的 filter 可以作用在编码后的字节流(更底层)。这是设计取舍:hyper 让 middleware 在高层改(类型安全),HCM 在底层改(更灵活但易错)。
- **零拷贝**:HCM 用 `Buffer::Instance`(类似 `Bytes`),支持切片引用,encode 路径也尽量零拷贝。hyper 用 `bytes::Chain`/`Bytes`,思路一致。两者都把"body 数据原样不动,只加边界标记"作为零拷贝的核心。
- **flush 策略**:HCM 有 `flush_access_log_on`、`stream_idle_timeout` 等配置控制 flush 时机,和 hyper 的 `pipeline_flush` 类似——都是延迟 vs 吞吐的权衡,交给用户配。

> **钉死这件事**:hyper、gRPC、Envoy 三个系统在"编码"上的共同点是:都追求零拷贝(body 数据不动)、都支持流式(边来边编)、都把"编码"和"flush"解耦。差异在协议层(HTTP/1 明文 vs HTTP/2 帧的维度)和生态(Rust Chain/Bytes vs C++ Buffer::Instance vs C grpc_slice)。读 hyper 编码路径,等于看到"一个高性能协议库的编码层"在 Rust 异步栈上的典型实现,可以横向迁移到理解 gRPC/Envoy。

---

## 七、技巧精解:两个最硬核的技巧

本章正文后,挑两个最硬核的技巧单独拆透。

### 技巧一:Buf::chain 三段零拷贝——把"边界标记 + body + 边界标记"变成一个 Buf

这是本章最硬核的技巧,也是 hyper HTTP/1 编码性能的命脉。

**动机**:chunked 编码每个 chunk 要编成"`{hex}\r\n` + chunk 数据 + `\r\n`"三段。朴素做法是拼一个 `Vec<u8>`(三段拷一起),但每个 chunk 都拷贝 body 数据,大文件流式响应灾难。要做到"零拷贝",得让"边界标记"(ChunkSize 和 `\r\n`,小)和"body 数据"(大)各自待在原地,逻辑上拼一起,flush 时一次 writev 吐出去。

**`bytes` crate 怎么做(参照)**:`bytes` crate 提供 `Chain<A, B>`(`bytes::buf::Chain`),它持两个 `Buf`,实现 `Buf` trait 时把它们当"逻辑连续"的一段。`A.chain(B)` 返回 `Chain<A, B>`。`Chain` 的 `chunks_vectored` 会把 A 和 B 的 slice 都填进 `IoSlice` 数组,`writev` 一次写两段。

**hyper 怎么实现**:嵌套两层 `Chain` 做到三段:

```rust
// hyper/src/proto/h1/encode.rs:136-142 (摘录)
Kind::Chunked(_) => {
    let buf = ChunkSize::new(len)              // 第一段: 栈上 [u8; 18], "{len:X}\r\n"
        .chain(msg)                            // 第二段: 用户 body (Bytes, 引用计数)
        .chain(b"\r\n" as &'static [u8]);      // 第三段: 编译期常量 "\r\n"
    BufKind::Chunked(buf)
}
```

`ChunkSize::new(len).chain(msg)` 返回 `Chain<ChunkSize, B>`(两段)。`.chain(b"\r\n")` 再返回 `Chain<Chain<ChunkSize, B>, &'static [u8]>`(三段)。这个嵌套 Chain 实现 `Buf`,`remaining()` = 三段之和,`chunks_vectored` 填三段 slice 进 iov。

**flush 时**:这个三段 Chain 作为一个 `EncodedBuf` 入 `WriteBuf.queue`。`WriteBuf::chunks_vectored`(`io.rs:635`)把 `headers`(一段)+ `queue` 里所有 `EncodedBuf`(每个最多三段 slice)填进 64 长的 iov 数组。`Buffered::poll_flush` 调 `poll_write_vectored(iovs)`,一次 `writev` 吐出"头部 + 所有 chunk"的全部字节,body 数据原样不动(只是被 `Bytes` 的引用计数持有)。

**反面对比:不这样会怎样**:

- **朴素拼接(每 chunk 拷到一个大 Vec)**:每个 chunk 拷贝一次 body 数据。1GB 文件分 64KB chunk = 16000 次拷贝 = 1GB 内存拷贝。CPU 和内存带宽吃光,性能远不如零拷贝。
- **不用 Chain,用 `Vec<IoSlice>` 自己拼 iov**:可以做到零拷贝,但要手动管理"哪些 slice 属于哪个 chunk",生命周期复杂(ChunkSize 是栈上数组,出了作用域 slice 就悬空)。`Chain` 把生命周期管理交给类型系统(`Chain<A, B>` 持有 A 和 B 的所有权),安全且零成本。
- **不用 writev,每段一次 write**:三段三次 syscall(chunked 每个 chunk 三次),syscall 开销爆炸。writev 一次 syscall 写三段,内核负责拼接。

> **钉死这件事**:`Buf::chain` 三段零拷贝是 hyper HTTP/1 编码的性能命脉。它把"加边界标记"这个本该需要拷贝的操作,变成"逻辑链接 + writev"——body 数据原样不动,只在前后挂栈上/静态的边界标记。这是 `bytes` crate 的 `Chain` + `writev(2)` 系统调用 + Rust 所有权模型三者合力的招牌技巧。读 hyper 看到 `.chain(...).chain(...)`,要知道它不是"链表",是"零拷贝的字节拼接"。

### 技巧二:`Transfer-Encoding` 自动选择的"尊重用户 + 缺了才补"策略

第二个硬核技巧是 `Server::encode_headers` 里那段 `if !wrote_len` 的"尊重用户 + 缺了才补"策略(`role.rs:907`)。

**动机**:用户写 axum handler 时,通常不手动设 `Content-Length` 或 `Transfer-Encoding`——他知道 body 多大就 `Response::new(full_body)`,不知道就 `Response::new(Body::wrap_stream(...))`。hyper 应该**自动**选合适的 `Transfer-Encoding`。但偶尔用户会自己设(比如想强制 chunked,或想设特定的 Content-Length),这时 hyper 要**尊重用户**。这两件事要同时满足:"用户没设就自动补,用户设了就尊重。"

**朴素做法(参照)**:写一个 `if user_set_content_length { ... } else if user_set_transfer_encoding { ... } else { 自动选 }`。但这要求先扫一遍 `HeaderMap` 看用户设了啥,再决定——扫两遍,效率低,而且逻辑分散。

**hyper 怎么实现**:用一个巧妙的"遍历中分流"——`Server::encode_headers`(`role.rs:686`)用一个 `'headers: for (opt_name, value) in msg.head.headers.drain()` 遍历用户 headers,遇到 `Content-Length`/`Transfer-Encoding`/`Connection`/`Date`/`Trailer` 这些"特殊头"时,**就地处理**:

```rust
// hyper/src/proto/h1/role.rs:686-903 (结构示意, 简化)
'headers: for (opt_name, value) in msg.head.headers.drain() {
    match *name {
        header::CONTENT_LENGTH => {
            // 用户设了 Content-Length
            // 根据 body 长度信息, 信任用户的值, 建 Encoder::length(用户的值)
            // 设 wrote_len = true, 标记"长度已确定"
        }
        header::TRANSFER_ENCODING => {
            // 用户设了 Transfer-Encoding
            // 检查是否以 chunked 结尾, 是则 Encoder::chunked()
            // 否则补 chunked(headers::add_chunked)
            // 设 wrote_len = true
        }
        header::CONNECTION => { /* 处理 keep-alive/close */ }
        header::DATE => { wrote_date = true; /* 继续当普通头写 */ }
        header::TRAILER => { /* 收集 allowed_trailer_fields */ }
        _ => {
            // 普通头: 写 "name: value\r\n"
        }
    }
}

// 遍历完后, 如果 wrote_len 还是 false (用户没设长度相关头)
if !wrote_len {
    encoder = match msg.body {
        Some(BodyLength::Unknown) => { /* 自动选 chunked 或 close-delimited */ }
        Some(BodyLength::Known(len)) => { /* 自动写 content-length: {len} */ }
        None => { /* 自动写 content-length: 0 */ }
    };
}
```

精髓在"遍历中就地处理特殊头 + 设 `wrote_len` 标志":一遍遍历既写完了所有普通头、又处理了特殊头、还记录了"用户有没有设长度"。遍历完看 `wrote_len`,false 才走"自动补"分支。一遍扫描完成所有工作,效率高,逻辑集中。

**"尊重用户"体现在哪**:在 `Content-Length` 分支(`role.rs:700-732`),如果用户设的值和 body 的 `BodyLength::Known(known_len)` 不一致,hyper 在 debug build 下 `assert!(len == known_len)`(`role.rs:713`,帮开发者找 bug),但在 release 下**信任用户的值**("For performance reasons, we are just going to trust that the values match",`role.rs:704` 注释)。这是"用户优先"——用户显式设的,hyper 不擅自改。

**"缺了才补"体现在哪**:`if !wrote_len` 那段(`role.rs:907-951`)只在用户没设长度相关头时触发,根据 body 长度信息自动补 `Content-Length` 或 `Transfer-Encoding: chunked`。

**反面对比:不这样会怎样**:

- **扫两遍(先扫看用户设了啥,再写)**:效率低(两遍遍历 HeaderMap),而且要在两遍之间保存"用户设了啥"的中间状态,代码啰嗦。hyper 的"一遍遍历 + 标志位"是一遍过,零中间状态。
- **不尊重用户,hyper 一律自动选**:用户没法强制 chunked(比如想测 chunked 解码),也没法设特定 Content-Length(比如 HEAD 请求想设实体长度)。灵活性丧失。
- **不自动补,要求用户必须设**:用户负担重,容易忘(忘了设 Content-Length 又是未知长度 body,响应就 close-delimited 了,不能 keep-alive)。hyper 的"缺了才补"让用户在 99% 场景不用操心 `Transfer-Encoding`。

> **钉死这件事**:`Transfer-Encoding` 自动选择是 hyper 编码层的"用户友好 + 协议合法"双保证。它用"一遍遍历 + `wrote_len` 标志"做到"尊重用户(用户设了就用)、缺了才补(没设就按 body 长度自动选)"。这个策略让 axum/reqwest 用户几乎不用关心 `Transfer-Encoding`——hyper 自动选对——同时在需要时允许用户显式覆盖。这是"易用 + 灵活"的招牌设计。

---

## 八、章末小结

### 回扣主线

本章是第 2 篇(协议侧)的收尾,把"响应怎么编成字节写出去"讲到底,和前 3 章(连接/解析/chunked)闭环。回到全书的二分法:

- **协议侧**:`Encoder`(`Kind` 三态 Length/Chunked/CloseDelimited)、`EncodedBuf`/`BufKind`(五变体,Chain 零拷贝)、`Server::encode`/`Client::encode`(状态行/请求行 + 头部拼接 + Transfer-Encoding 自动选择)、`Writing::Body(Encoder)` 状态机——这些决定"HTTP 字节怎么编"。
- **框架侧的接合点**:`Dispatcher::poll_write` 调 `write_head`/`write_body`/`end_body`/`write_trailers`,从 `Dispatch::poll_msg`(Service 返回的 Response)拿 body Stream,一个 Frame 一个 Frame 编码入队——这是协议侧(编码)和框架侧(Body Stream)的接合。
- **承接 Tokio**:`poll_write`/`poll_write_vectored`/`poll_flush` 都是 `AsyncWrite` 的方法,底层是 Tokio 的 reactor(socket 可写唤醒)和 `writev(2)` syscall——一句带过。hyper 独有的是"在这之上搭零拷贝 Chain + 流式编码 + Transfer-Encoding 自动选择"。
- **承接 gRPC**:HTTP/2 的 HPACK/DATA 帧/流控在《gRPC》拆透,一句带过。本章只讲 HTTP/1,HTTP/1 编码的硬骨头是"零拷贝 + 流式 + Transfer-Encoding 选择",和 HTTP/2 不同维度。

第 2 篇四章闭环:P2-05 讲连接循环骨架(`Dispatcher::poll_loop`)、P2-06 讲字节解析(`decode.rs` 状态机)、P2-07 讲 chunked/100-continue/upgrade 边角协议、P2-08 讲字节编码(`encode.rs` 状态机)。读完这四章,你应该能在脑子里放映出"一条 HTTP/1 连接从字节进来、解析成请求、交 Service、响应编回字节、写出去"的完整全过程。第 2 篇至此收尾,下一节进入第 3 篇(HTTP/2 via h2)。

### 五个为什么

1. **为什么 `Encoder` 要在写头部的那一刻就定下来 `Transfer-Encoding`?**——头部要先写出去,头部里有 `Content-Length` 或 `Transfer-Encoding: chunked`,这决定了 body 怎么编。头部时刻必须对"未来 body 怎么编"做承诺,否则头部和 body 编码方式不一致,协议错乱。`Encoder` 就是这个承诺的载体。
2. **为什么用 `Chain` 而不是拼一个大 Vec?**——零拷贝。`Chain` 只持各段的引用/所有权,不拷贝字节;flush 时 `writev` 一次吐多段。拼大 Vec 每个 chunk 都拷贝 body 数据,大文件流式响应灾难。`Chain` + `writev` 是"加边界标记不拷贝 body"的招牌实现。
3. **为什么 `Transfer-Encoding` 选择是"尊重用户 + 缺了才补"?**——99% 场景用户不关心(axum handler 不设长度头),hyper 自动选对(长度已知 length、未知 chunked、HTTP/1.0 close-delimited);1% 场景用户想强制(测 chunked、HEAD 设实体长度),hyper 尊重。一遍遍历 + `wrote_len` 标志同时满足两者,效率高。
4. **为什么 flush 时机不影响正确性只影响延迟?**——协议只要求字节顺序对,`WriteBuf` FIFO 队列保证顺序(头部先入队、body 按 `write_body` 顺序、结束标记最后)。TCP 保证字节流顺序,client 看到的是连续字节流,不关心"几次 write 发的"。flush 频率只影响 TTFB 和 syscall 数量,不影响协议合法性。
5. **为什么 `MAX_BUF_LIST_BUFFERS = 16`?**——批量 vs 延迟的权衡。攒 16 个 buf 一次 writev,比每个 buf 一次 write 省 15 次 syscall(吞吐);但不超过 16,避免 TTFB 灾难(client 等 16 个 chunk 攒齐才收到)。和 `poll_loop` 的 `for _ in 0..16` 一个数量级,都是 hyper 的经验值。

### 想继续深入往哪钻

- **想看 HTTP/2 怎么编码(HPACK/DATA 帧)**:第 3 篇 P3-09/P3-10,以及《gRPC》第 2 篇(chttp2 自实现 HTTP/2 的对照)。
- **想看 `bytes::Chain`/`Bytes` 的零拷贝机制**:第 6 篇 P6-17(bytes 零拷贝招牌章),以及《内存分配器》。
- **想看 client 侧怎么发请求(编码请求行)**:第 4 篇 P4-13(client/conn 的协议机循环,client 侧的 `Client::encode` 写请求行)。
- **想看 chunked 解码(对称的另一面)**:回顾 P2-07,chunked 解码状态机。
- **想自己感受**:用 hyper 写一个流式响应 server(`Response::new(Body::wrap_stream(...))`),`curl --http1.1 -v` 看 `Transfer-Encoding: chunked` 头和 chunked body 的 `{hex}\r\n` + data + `\r\n` 格式。再写一个长度已知的响应,看 `Content-Length`。用 `strace -e writev` 看 server 进程,确认一次 writev 吐多段。

### 引出下一章

第 2 篇(HTTP/1 协议机)至此收尾——连接循环(P2-05)、字节解析(P2-06)、chunked/upgrade 边角(P2-07)、字节编码(P2-08)四章闭环。HTTP/1 是 hyper 自己实现的协议机,讲透了。接下来进入第 3 篇:**HTTP/2 via h2**。HTTP/2 hyper 不自己实现,委托 `h2` crate——为什么?因为 HTTP/2 复杂得多(帧/流/HPACK/流控),Rust 生态有成熟的 h2,hyper 做适配层。下一章 P3-09 · **HTTP/2 帧与多路复用**,从"为什么一条连接能并发跑多个请求"讲起,拆 hyper 怎么用 h2 把"一个 hyper 请求"映射成"一条 HTTP/2 stream"。HTTP/2 的帧/流/HPACK/流控在《gRPC》第 2 篇已拆透,本章一句带过,篇幅留"hyper 怎么用 h2"。

> **下一章**:[P3-09 · HTTP/2 帧与多路复用](P3-09-HTTP2-帧与多路复用.md)
