# 第 2 篇 · 第 5 章 · HTTP/2 基础:为什么是 gRPC 的天然底座

> **核心问题**:gRPC 明明可以像老式 RPC 那样,自己发明一套私有 TCP 协议跑得飞快,也可以直接复用最广泛的 HTTP/1.1。它为什么偏偏选了 HTTP/2?更具体一点——HTTP/2 的**二进制分帧、流、多路复用、HPACK、流量控制**这五件东西,各解决了 HTTP/1.1 与私有协议的什么致命缺陷?它们又凭什么让"一条 TCP 连接同时跑成千上万个调用"这件事,从"工程奇迹"变成"协议内置"。

> **读完本章你会明白**:
> 1. 为什么 HTTP/1.1 的"一问一答独占连接"扛不住现代 RPC 的海量并发,而"开 N 条连接"又会撞上另一堵墙(队头阻塞的两个面)。
> 2. HTTP/2 的"流"是什么,**为什么它一旦被发明出来,RPC 就再也不该回到没有流的时代**——它是 gRPC 四种调用模式的同一根载体。
> 3. 9 字节帧头里那个"长度 + 类型 + 标志 + 流 ID"为什么这么排,DATA/HEADERS/SETTINGS/PING/GOAWAY/RST_STREAM/WINDOW_UPDATE 各自管哪一类事,以及对得上 RFC 哪一条。
> 4. 为什么 gRPC 自实现的 chttp2 已经从 RFC 7540 全量迁移到 RFC 9113(HTTP/2 bis),老博客里"对得上 RFC 7540"的引用大片过时——这是个被大多数人忽略的硬事实。

> **如果一读觉得太难**:先只记住三件事——① HTTP/2 在一条 TCP 连接里塞了**很多条并发的流**,每条流是一次调用,互不阻塞;② 每个东西都是**帧**(9 字节帧头 + payload),帧带着"我属于哪条流",这是流的物理形态;③ 协议层这块招牌,只有 gRPC C++ core(chttp2)讲得透,因为 grpc-go/grpc-java 把 HTTP/2 外包给了语言库。

---

## 〇、一句话点破

> **HTTP/2 对 gRPC 来说不是"一个传输选择",而是"流的载体"。HTTP/2 的全部发明——二进制分帧、流、多路复用、HPACK、双层流量控制——都是为了让"一条连接上能同时跑很多条独立的流"这件事变得正确、可背压、可取消。这恰好是 RPC 在海量并发下最需要的。**

这是结论,不是理由。本章倒过来拆:先讲 HTTP/1.1 和私有协议各自撞上什么墙,再讲 HTTP/2 用哪五件东西把这些墙拆了,最后钻进 chttp2 的源码,看 gRPC 是怎么一行行落实这五件东西的——并且顺手纠正一个被大多数博客忽略的硬事实:gRPC 的 HTTP/2 已经从 RFC 7540 迁到了 RFC 9113。

---

## 一、先把"为什么不是别的"讲透:HTTP/1.1 与私有协议的两堵墙

gRPC 在 2015 年开源时,选 HTTP/2 是一个看起来"很重"的决定——HTTP/2 比 HTTP/1.1 复杂得多,解析、流控、HPACK 全得自己实现。这个选择不是赶时髦,而是 HTTP/1.1 和私有协议各自有一堵过不去的墙。

### 第一堵墙:HTTP/1.1 的队头阻塞(两个面)

HTTP/1.1 的语义是"**请求-响应独占**":客户端发一个完整请求,服务端回一个完整响应,这条往返就独占了这条连接。语义上,同一连接同一时刻**只能有一个未完成的请求**。如果你想在一条 HTTP/1.1 连接上同时发 1000 个调用,只有两个选择,而且都很难看:

- **串行**:第 2 个请求必须等第 1 个响应回来才能发。一个慢请求(比如某次 `GetUser` 卡了 500 ms)会**堵死后面所有人**——这就是"队头阻塞"(head-of-line blocking)的请求面。
- **并行**:开 1000 条连接绕开它。但每条连接要做 TCP 三次握手 + TLS 握手(动辄几轮 RTT),内核要维护 1000 份 socket 状态,而且**连接内**的队头阻塞没消失——只是被搬到了"每条连接内部"。

更阴险的是第二面:**TCP 层的队头阻塞**。HTTP/1.1 把整条响应当一个不可分割的字节流塞给 TCP。TCP 是可靠传输,一旦某个包丢了,TCP 会停下来等重传,**后面已经到了的字节也被卡在内核缓冲里上不了应用层**——哪怕那些字节属于一个完全不相关的请求。HTTP/1.1 对此无能为力,因为它看不到"字节属于哪条请求",它眼里只有一条字节河。

```
   HTTP/1.1:一个连接,一个时刻只有一个请求在飞
   ┌─────────────────────────────────────────┐
   │  请求1 ─── (等响应1) ─── 响应1           │  ← 慢请求堵死请求2
   │  请求2 ────────────────────── 响应2      │
   └─────────────────────────────────────────┘
```

> **不这样会怎样**:gRPC 的典型场景是"一个客户端进程,每秒发起上万次调用,每次调用几十毫秒到几百毫秒"。如果跑在 HTTP/1.1 上,你要么串行(吞吐被队头阻塞吃光),要么开上万条连接(每条连接都要握手、占内存、占端口、占服务端 fd),而 TCP 层的丢包队头阻塞还在底下等着。**HTTP/1.1 在物理上扛不住现代 RPC 的并发密度**。

### 第二堵墙:私有协议的生态割裂

那为什么不学老式 RPC(早期 Thrift、Dubbo 的 TCP 私有协议、Google 内部的 Stubby),自己设计一套跑在裸 TCP 上的二进制协议?性能上完全可以做得很好——实际上 Stubby 在 Google 内部扛了几百亿次调用/天。但 gRPC 开源时,Google 做了一个决定性的选择:**走公开标准,不再造私有协议**。原因有四:

1. **生态割裂**:每种私有 RPC 一套协议、一套工具(`tcpdump` 抓包看不懂、负载均衡器不认识、监控/网关/APM 全得自己造)。Stubby 走不出 Google,正是因为这个。
2. **基础设施复用**:HTTP/2 的帧能穿过任何 HTTP 中间设施——LB(Nginx、Envoy、AWS ALB)、API 网关、CDN、WAF、curl、浏览器(虽然 gRPC 不用浏览器,但 HTTP/2 的 framing 让它**能**)。私有协议走任何一跳都要再造一遍。
3. **标准化的演进**:HTTP/2 是 IETF 标准,有 RFC、有共识机制、有大量实现互相验证。私有协议一旦设计错了(比如流控语义有漏洞),只能自己扛。
4. **跨语言友好**:HTTP/2 是公开的,任何语言都有现成实现(虽然 gRPC C++ core 不复用,但这是另一个故事)。

> **所以这样设计**:gRPC 选 HTTP/2 是**"公开标准 + 成熟生态"换可走出的协议**——用一套被全世界验证过的多路复用 + 流控 + 头部压缩,换掉 Stubby 那个走不出 Google 的私有 TCP 协议。性能上付出"HTTP/2 比 Stubby 略重"的代价,换回来的是 gRPC 能跑在 Nginx 后面、能用 `grpcurl` 调试、能被 Envoy 透明代理。**这是 gRPC 从"Google 内部基础设施"变成"跨语言 RPC 事实标准"的根**。

### 那为什么不是 HTTP/3(QUIC)?

gRPC 诞生在 2015 年,那时 HTTP/2 才随 gRPC 同年标准化(RFC 7540 在 2015 年 5 月发布,gRPC 在 2015 年 3 月开源)。HTTP/3 当时还在很早期(HTTP/3 / QUIC 的 RFC 9114 / 9000 要到 2021~2022 年才发布)。HTTP/3 基于 UDP+QUIC,理论上有"连接迁移""无 TCP 层队头阻塞"等优势,但 2015 年的生态远未成熟。gRPC 选了那时**成熟 + 公开 + 生态完善**的 HTTP/2。这是工程上的甜点选择,不是技术盲点。

> **钉死这件事**:HTTP/1.1 死在"独占连接 + 文本 + 无流",私有协议死在"生态割裂"。HTTP/2 同时解决了两件事:它**有流**(消除 HTTP/1.1 队头阻塞)、**是公开标准**(消除私有协议割裂)、**是二进制**(省解析成本)。这就是 gRPC 选它的根。下面五件东西,都是这个选择的展开。

---

## 二、HTTP/2 的五件发明,各拆穿一个 HTTP/1.1 的硬伤

HTTP/2 不是 HTTP/1.1 的"小升级",它是一次结构性重写。从 RFC 角度看,HTTP/2 的语义层(method、path、status、header 这些)**和 HTTP/1.1 一致**——所以 HTTP 中间设施能通用;但**语法层**(这些语义怎么编码成字节)被彻底重写了。重写带来五件发明,每件都对应 HTTP/1.1 的一个硬伤。

### 发明一:二进制分帧——让"字节属于哪条流"可见

HTTP/1.1 的传输单元是"一条完整的 ASCII 报文",以 `\r\n` 分行、空行分头和体。解析它要逐字符扫引号、冒号、`\r\n`,慢;更糟的是,它**没有"这条字节属于哪次请求"的标识**——所以一条连接只能跑一个请求。

HTTP/2 把传输单元改成**帧(frame)**。所有数据,不管是 header 还是 body,都被切成一个个固定格式的帧。每个帧的头部有 9 个字节,这 9 个字节就是 HTTP/2 的"宇宙常数":

```
   HTTP/2 帧的通用格式(9 字节帧头 + payload,对得上 RFC 9113 §4.1)
   ┌────────────────┬────────┬────────┬─────────────────────┬──────────────┐
   │ Length (24 bit)│ Type   │ Flags  │ Reserved │ Stream ID │  Payload ... │
   │     3 字节      │ 1 字节 │ 1 字节 │   1 bit  │  31 bit   │  Length 字节 │
   └────────────────┴────────┴────────┴─────────────────────┴──────────────┘
```

四个字段的含义:

- **Length(24 bit)**:payload 的字节数。一个帧的 payload 最多 2^24-1 = 16 MB(但实际由 SETTINGS_MAX_FRAME_SIZE 协商,默认 16384)。**注意 Length 不包含帧头那 9 字节,只数 payload**。
- **Type(8 bit)**:帧类型。HTTP/2 定义了 10 种类型(DATA=0、HEADERS=1、PRIORITY 已弃用=2、RST_STREAM=3、SETTINGS=4、PUSH_PROMISE=5、PING=6、GOAWAY=7、WINDOW_UPDATE=8、CONTINUATION=9),还允许扩展(比如 gRPC 自己加的 SECURITY=200)。
- **Flags(8 bit)**:每个 bit 是一个开关,语义随 Type 变(比如 DATA 的 END_STREAM=1 表示"这条流我没东西发了",HEADERS 的 END_HEADERS=4 表示"头部块到此结束")。
- **Stream ID(31 bit)**:**这是整个 HTTP/2 设计的核心**。每个帧都标着"我属于哪条流"。客户端发起的流是奇数(1、3、5...),服务端发起的流是偶数(0 保留给连接级)。**Stream ID = 0 的帧是"整条连接的",不属于任何一条流**(SETTINGS、PING、GOAWAY 都走 0)。

> **所以这样设计**:帧头那 9 字节,把"字节属于哪条流"这件事**显式编码进了传输单元**。从此一条 TCP 连接上的字节不再是一条无差别的河,而是被 9 字节帧头切成了"这条流的一帧、那条流的一帧、连接级的一帧"。**这是多路复用的物理基础**——没有 Stream ID,就没有多路复用。

在 gRPC 的 chttp2 实现里,这 9 字节帧头就是一个 `Http2FrameHeader` 结构体,定义在 [`frame.h`](../grpc/src/core/ext/transport/chttp2/transport/frame.h#L206-L222):

```cpp
// Define a struct for the frame header.
// Parsing this type is the first step in parsing a frame.
struct Http2FrameHeader {
  uint32_t length;
  uint8_t type;
  uint8_t flags;
  uint32_t stream_id;
  // Serialize header to 9 byte long buffer output
  // Crashes if length > 16777215 (as this is unencodable)
  void Serialize(uint8_t* output) const;
  // Parse header from 9 byte long buffer input
  static Http2FrameHeader Parse(const uint8_t* input);
  ...
};
```

注释里那句 "the first step in parsing a frame" 点出了它的角色:任何进来的字节,先按这 9 字节切成帧,才有后面"这个帧属于哪条流、要干什么"的处理。

### 发明二:流——把"一次请求-响应"升级成"一条有方向的字节流"

有了 Stream ID,HTTP/2 就能在一条 TCP 连接里**同时跑很多条流**,每条流是一个独立的请求-响应。一条流由**双向的帧序列**组成:客户端发的 HEADERS + DATA,服务端回的 HEADERS + DATA,都属于同一条流(同一个 Stream ID)。

流的几条核心性质(对得上 RFC 9113 §5.1):

- **每条流有唯一 ID**:客户端发起的流是奇数,服务端用 PUSH_PROMISE 发起的流是偶数(gRPC 不用 server push,所以 gRPC 里几乎只有奇数流)。
- **流是单向递增的**:一旦用了某个 stream ID,后续这条连接上不能再开比它小的 ID。
- **流有生命周期**:idle → open(收到 HEADERS)→ half-closed(一方发了 END_STREAM)→ closed(双方都发完 END_STREAM,或任一方发 RST_STREAM)。
- **流之间独立**:一条流卡住(等数据、等处理)**不会阻塞同连接的其他流**——这是多路复用的核心。

> **不这样会怎样**:如果"流"这个抽象不存在,gRPC 要为 unary(一问一答)和 streaming(连续问/答)造**两套传输机制**:unary 走老式请求-响应,streaming 走长连接或 WebSocket。两种调用模式各自一套客户端、一套服务端、一套负载均衡、一套重试逻辑——工程上爆炸。HTTP/2 的流让 gRPC 能"**一切皆流**":unary 是"只有一进一出的流",双向流是"可以进很多出很多的流",**底层载体完全一样**。这是 gRPC 四种调用模式(unary / server-streaming / client-streaming / bidi-streaming)能共用一套传输、流控、负载均衡、重试的根。

> **钉死这件事**:**HTTP/2 把"一次请求-响应"从"一个 HTTP 报文"升级成了"一条有方向的流"**。这个升级一旦发生,RPC 就再也不该回到没有流的时代——因为流带来的"多路复用、背压、可取消"三件礼物,是任何 RPC 都想要的。

### 发明三:多路复用——一条连接,海量并发流

"流"的物理形态有了,把多条流塞进**同一条 TCP 连接**就是水到渠成:每条流的帧轮流过线,接收端按 Stream ID 把帧分发回各自所属的流。这就是多路复用(multiplexing)。

```
   HTTP/2:一条 TCP 连接,多条并发流,帧交错
   ┌──────────────────────────────────────────────┐
   │ stream 1 (GetUser)    ▓▓▓▓ 响应              │
   │ stream 3 (GetOrder)   ▓▓▓▓▓▓ 响应            │  ← 同一条连接,
   │ stream 5 (Update)     ▓▓ 响应                │     海量并发调用,
   │ stream 7 (Stream)     ▓▓▓▓▓▓▓▓...            │     帧交错过线,
   │ (连接级)SETTINGS/PING/GOAWAY                  │     互不阻塞
   └──────────────────────────────────────────────┘
```

这里要讲清一件容易混的事:**多路复用消除了 HTTP/1.1 的"应用层队头阻塞",但没有消除 TCP 层的队头阻塞**。如果 TCP 丢了包,内核仍然会停下来等重传,所有流的帧都卡在内核里。这是 HTTP/3(QUIC 跑在 UDP 上)要解决的问题——gRPC 现在不用 HTTP/3,所以 TCP 层的队头阻塞还在,但应用层多路复用已经让"一条慢请求堵死同连接其他请求"这件事消失了。

在 chttp2 里,这条"分发回各自流"的逻辑,核心是一个 `stream_map`——一个连接上所有 stream 的花名册,按 stream_id 查找。这是下一章 P2-06 的主角,这里先点一下。

### 发明四:HPACK——把每次调用重复的头部压到几乎零字节

HTTP/1.1 每次请求都要全文发头部(`Host:`、`Content-Type:`、`User-Agent:`、`Cookie:`......),这些头部**几乎每次都一样**,而且都是 ASCII 文本。在高频 RPC 下,头部开销惊人——想象一下,gRPC 每次调用都要发 `:path`、`content-type: application/grpc`、`te: trailers`、`:authority`,这些都是几乎不变的字面量。

HPACK(RFC 7541)用**三重压缩**把这件事做到极致:

- **静态表**:RFC 7541 预定义了 61 项最常用头(`:method: GET`、`:path: /`、`content-type` 等),发个 1 字节索引号代表整条头。
- **动态表**:本次连接**学到的**头(比如客户端发过一次的 `:path: /UserService/GetUser`)存进动态表,下次同连接只发索引号。**同连接重复调用,头部几乎零字节**。
- **Huffman 编码**:实在要发的文本用 Huffman 编码(高频字符短编码)再压一道。

HPACK 是 gRPC 在 HTTP/2 上做到高吞吐的关键之一,本书 P2-07 会拆到源码级(静态表 61 项、动态表的环形缓冲、Huffman 的多级查表解码)。本章只把它列在"五件发明"里——它解决的是"HTTP/1.1 文本头部冗余"这个硬伤。

> **钉死这件事**:HPACK 不是可有可无的优化,它是**让头部从"每次全文发"变成"同连接几乎零字节"**的关键。gRPC 的典型调用头部(`:path`、`content-type`、`te`、`:authority`、`grpc-encoding`...)在动态表稳定后,基本只发几个索引号。这是 HTTP/2 头部压缩比 HTTP/1.1 高一个数量级的根。

### 发明五:双层流量控制——背压,而不是 OOM

HTTP/1. 没有"流控",它的"流控"是 TCP 自己的窗口——但 TCP 窗口是**整条连接**的,粒度太粗,挡不住"一个慢请求拖累整条连接"。HTTP/2 发明了**双层流量控制**(RFC 9113 §6.9):

- **连接级 window**:整条连接的总信用额度。
- **流级 window**:**每条流**独立的信用额度。

发送方每发一个 DATA 帧就扣 window,接收方处理完就回 WINDOW_UPDATE 帧加 window。两层独立,意味着**一条流发太快可以单独被限流,不影响同连接其他流**——这就是**精准背压**。这是 gRPC 在 P2-09 流控章要拆到源码级的东西(gRPC 还在 HTTP/2 流控之上叠了 BDP 估计和 ping 限速),这里先列进"五件发明"。

```
   HTTP/2 双层 window(对得上 RFC 9113 §6.9)
   ┌─────────────────────────────────────────┐
   │ 连接级 window(整条连接的总额度)          │
   ├─────────────────────────────────────────┤
   │ 流级 window:stream1 │ stream3 │ ...     │  ← 每条流独立额度
   └─────────────────────────────────────────┘
   发送方:发一帧 DATA → 两层 window 都扣
   接收方:消费完 → 回 WINDOW_UPDATE 给两层加信用
```

> **钉死这件事**:HTTP/2 的五件发明,件件都戳在 HTTP/1.1 与私有协议的痛点上——**二进制分帧**让"字节属于哪条流"可见;**流**把请求-响应升级成有方向的字节流;**多路复用**让一条连接跑海量并发流;**HPACK**把每次重复的头部压到几乎零字节;**双层流量控制**让背压精准到单条流。**这五件东西组合起来,恰好是 RPC 在海量并发下最需要的"传输套餐"。**

---

## 三、钻进 chttp2:这些协议机制是怎么落到代码里的

讲完了"为什么",现在钻进 chttp2 看"怎么做的"。本章不展开每个帧的解析细节(那是 P2-08 framing 章的事),只看三件事:**帧的通用结构、每种帧各管什么、它们对 stream_id 的约束**。这三件事足够让读者在脑子里建起 HTTP/2 协议的骨架。

### 3.1 帧的通用结构:`Http2Frame` 这个 variant

HTTP/2 的 10 种帧,在 chttp2 里被建模成一个 `std::variant`——一个"可能是这 10 种之一"的类型。这个定义在 [`frame.h`](../grpc/src/core/ext/transport/chttp2/transport/frame.h#L69-L197):

```cpp
// DATA frame
struct Http2DataFrame {
  uint32_t stream_id = 0;
  bool end_stream = false;
  SliceBuffer payload;
  ...
};

// SETTINGS frame
struct Http2SettingsFrame {
  struct Setting { ... uint16_t id; uint32_t value; ... };
  bool ack = false;
  std::vector<Setting> settings;
  ...
};

// PING frame
struct Http2PingFrame { bool ack = false; uint64_t opaque = 0; ... };

// WINDOW_UPDATE frame
struct Http2WindowUpdateFrame {
  uint32_t stream_id = 0;
  uint32_t increment = 0;
  ...
};
... (HEADERS / CONTINUATION / RST_STREAM / GOAWAY / SECURITY / UNKNOWN / EMPTY)

using Http2Frame = std::variant<
    Http2DataFrame, Http2HeaderFrame, Http2ContinuationFrame,
    Http2RstStreamFrame, Http2SettingsFrame, Http2PingFrame,
    Http2GoawayFrame, Http2WindowUpdateFrame, Http2SecurityFrame,
    Http2UnknownFrame, Http2EmptyFrame>;
```

注意几件事:

1. **每个帧只保留"语义层关心的字段"**。比如 DATA 帧只留 `stream_id`、`end_stream`、`payload`——HTTP/2 spec 里 DATA 帧还有 padding、prioritization 字段,但 chttp2 **在 framing 层就把它们处理掉了**,语义层看不到,避免污染上层逻辑。文件头注释直接说了:`Each struct gets the members defined by the HTTP/2 spec for that frame type that the semantic layers of chttp2 need to reason about`。
2. **不用 bitfield,用一个 bool per flag**。文件头注释明说:`Instead of carrying bitfields of flags like the wire format, we instead declare a bool per flag`。线上是 bit,内存里是 bool——为了"producing/consuming code easier to write"。这是个典型工程取舍:**线上紧凑(带宽贵)、内存里宽松(代码清晰)**。
3. **是否有 stream_id,代表"它是连接级还是流级"**。DATA/HEADERS/RST_STREAM/WINDOW_UPDATE 有 `stream_id`(WINDOW_UPDATE 的可以是 0,表示连接级);SETTINGS/PING/GOAWAY 没有(它们天然是连接级,stream_id 必须 = 0)。

> **不这样会怎样**:如果每种帧都保留 spec 里的全部 bit,上层代码会不断处理 padding、prioritization 这些与业务无关的细节。chttp2 在 framing 层就把它们剥掉,**让上层只看到"这个帧属于哪条流、要干什么"**。这是协议实现的层次感——分帧层是字节级的,语义层是逻辑级的。

### 3.2 每种帧各管什么:一张帧职责表

把六种最常见的帧讲清楚(其余的 P2-08 会展开):

| 帧类型 | Type | 谁发、干什么 | stream_id 约束 | 对应 RFC |
|---|---|---|---|---|
| DATA | 0 | 双向:传应用数据(请求体 / 响应体) | 必须 ≠ 0 且奇数 | §6.1 |
| HEADERS | 1 | 双向:传头部块(请求头 / 响应头 / trailer) | 必须 ≠ 0 且奇数 | §6.2 |
| RST_STREAM | 3 | 双向:**异常终止一条流**(流级错误) | 必须 ≠ 0 且奇数 | §6.4 |
| SETTINGS | 4 | 双向:协商连接级参数;ACK 表示收到 | 必须 = 0 | §6.5 |
| PING | 6 | 双向:探活 + 测 RTT;ACK 表示收到 | 必须 = 0 | §6.7 |
| GOAWAY | 7 | 双向:**优雅关闭整条连接**(连接级错误或正常 shutdown) | 必须 = 0 | §6.8 |
| WINDOW_UPDATE | 8 | 双向:给流级或连接级 window 加信用 | 可 = 0(连接级)或奇数(流级) | §6.9 |

注意两个细节:

- **连接级帧(SETTINGS/PING/GOAWAY)必须 stream_id = 0**。如果服务端收到一个 stream_id ≠ 0 的 SETTINGS,这是协议错误,必须返回 GOAWAY 关连接。这条约束在 chttp2 里被严格检查(下面 3.3 会看到源码)。
- **流级帧(DATA/HEADERS/CONTINUATION/RST_STREAM)必须 stream_id ≠ 0 且为奇数**。为什么奇数?因为客户端发起的流是奇数,gRPC 的流只由客户端发起,所以 gRPC 里这些帧几乎全是奇数 stream_id。偶数 stream_id 的帧要么是协议错误,要么是服务端 PUSH(gRPC 不用)。

这些帧在 chttp2 里都有对应的处理文件:`frame_data.cc`、`frame_settings.cc`、`frame_ping.cc`、`frame_goaway.cc`、`frame_rst_stream.cc`、`frame_window_update.cc`。每个文件里有一个 `*_parser_begin_frame` 和 `*_parser_parse` 函数——这是 legacy 解析器(下面 3.4 会讲为什么有"两套"解析)。

### 3.3 stream_id 约束的强制:协议正确性的第一道防线

HTTP/2 协议要求 endpoint **必须**校验收到的帧是否符合 stream_id 约束(对得上 RFC 9113 §5.1.1 / §6.x)。这是协议正确性的第一道防线——一旦放过错误的帧,后续状态机会被搞乱。在 chttp2 里,这套校验集中在新版 `frame.cc` 的 `ParseFramePayload` 分发器里。我们看 PING 的例子:

```cpp
// ParsePingFrame(frame.cc:576 附近,简化示意,非源码原文)
http2::ValueOrHttp2Status<Http2Frame> ParsePingFrame(
    const Http2FrameHeader& hdr, SliceBuffer&& payload) {
  if (hdr.stream_id != 0) {                      // PING 必须 stream_id = 0
    return ConnectionError(ProtocolError,
                           std::string(RFC9113::kPingStreamIdMustBeZero));
  }
  if (hdr.length != 8) {                         // PING payload 固定 8 字节
    return ConnectionError(FrameSizeError,
                           std::string(RFC9113::kPingLength8));
  }
  ... // 解析 opaque 字段
}
```

那个 `RFC9113::kPingStreamIdMustBeZero` 是一条预定义的错误字符串,定义在 [`frame.h`](../grpc/src/core/ext/transport/chttp2/transport/frame.h#L327-L329):

```cpp
// 6.
// Stream Identifier related errors
// ...
inline constexpr absl::string_view kPingStreamIdMustBeZero =
    "RFC9113: If a PING frame is received with a Stream Identifier field "
    "value other than 0x00, the recipient MUST respond with a connection error";
```

注意字符串里的 `RFC9113`——这是 HTTP/2 的更新版标准(RFC 9113,2022 年发布,替代了 2015 年的 RFC 7540)。**chttp2 已经全量迁移到 RFC 9113**。这件事下一节单独讲。

stream_id 的约束全表(从 chttp2 源码归纳):

| 帧 | stream_id = 0 | stream_id ≠ 0 且奇数 |
|---|---|---|
| SETTINGS | ✅ 必须 | ❌ PROTOCOL_ERROR |
| PING | ✅ 必须 | ❌ PROTOCOL_ERROR |
| GOAWAY | ✅ 必须 | ❌ PROTOCOL_ERROR |
| DATA | ❌ | ✅ 必须 |
| HEADERS | ❌ | ✅ 必须 |
| CONTINUATION | ❌ | ✅ 必须 |
| RST_STREAM | ❌ | ✅ 必须 |
| WINDOW_UPDATE | ✅ 连接级 | ✅ 流级 |

### 3.4 一个被大多数博客忽略的硬事实:chttp2 已迁移到 RFC 9113

写到这一节,我必须先做一件事:**修正一个我自己写总纲时也差点踩的坑**。本书的总纲和很多老博客都说"HTTP/2 对得上 RFC 7540",但**实际打开 gRPC 1.83 的 chttp2 源码 grep,会发现 `RFC 7540` 这个字符串在整个 `src/core/ext/transport/chttp2/transport/` 目录里出现 0 次,而 `RFC 9113` 出现了 124 次,分布在 18 个文件里**。

这意味着什么?**HTTP/2 协议在 2022 年被 IETF 重新发布为 RFC 9113(HTTP/2 bis),它修正了 RFC 7540 的一些歧义、收紧了一些约束、删掉了已经被弃用的东西(比如 PRIORITY 帧,Type=2,在 RFC 9113 里被明确标记为弃用),但语义层面**和 RFC 7540 高度兼容**。gRPC 的 chttp2 实现已经全量采用 RFC 9113 作为协议对齐的参照——所有协议错误字符串都封装在 `frame.h:308-424` 的 `namespace RFC9113` 里。

这件事对读者有两个影响:

1. **读老博客要警惕**:任何拿 RFC 7540 当 HTTP/2 唯一参照的资料,可能没注意到 gRPC 已经迁到 RFC 9113。RFC 9113 对一些细节(PRIORITY 弃用、CONTINUATION 攻击防护、stream_id 严格校验)的约束比 7540 更严。
2. **本书从这一章起,统一用 RFC 9113 作 HTTP/2 的参照**,把 RFC 7540 当历史背景提及。这是按真实源码改写的诚实标注——本书的写作铁律。

在 chttp2 源码里,这个迁移的痕迹随处可见。比如 PRIORITY 帧(Type=2)在新版 `frame.cc` 的 FrameType 枚举里有一行注释明确标记弃用:

```cpp
enum class FrameType : uint8_t {
  kData = 0, kHeader = 1,
  // type 2 was Priority which has been deprecated.   ← RFC 9113 弃用
  kRstStream = 3, kSettings = 4, kPushPromise = 5,
  kPing = 6, kGoaway = 7, kWindowUpdate = 8, kContinuation = 9,
  kCustomSecurity = 200,  // Custom Frame Type
};
```

> **钉死这件事**:本书从这一章起,所有 HTTP/2 机制都**对得上 RFC 9113**(HTTP/2 bis,2022),而不再对 RFC 7540(2015)。这不是凑新潮,而是 gRPC 1.83 的源码就是这么写的——`RFC 7540` 引用归零,`RFC 9113` 引用 124 处。这是写 HTTP/2 协议层时一个被忽略的硬事实,本书按真实源码诚实标注。

---

## 四、连接的建立:preface 与 SETTINGS 协商

讲完帧的静态结构,看一眼"一条 HTTP/2 连接是怎么建立的"——这一段在 TCP 握手和 TLS 握手之上,是 HTTP/2 自己的握手。

### 客户端 connection preface:magic 24 字节

HTTP/2 的客户端在 TCP/TLS 握手完成后,**第一件事必须发一个固定的 24 字节序列**:

```
PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n
```

这串看起来像 HTTP/1.1 请求行的字符串,叫**客户端 connection preface**。它的设计很巧妙:任何 HTTP/1.1 服务端收到这串"伪请求行"会立刻识别出"这不是合法的 HTTP/1.1 请求"并断开,从而防止 HTTP/1.1 和 HTTP/2 在同一条连接上混淆。这个 magic 字符串在 chttp2 里这样定义([`transport_common.h`](../grpc/src/core/ext/transport/chttp2/transport/transport_common.h#L27-L29)):

```cpp
#define GRPC_CHTTP2_CLIENT_CONNECT_STRING "PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"
#define GRPC_CHTTP2_CLIENT_CONNECT_STRLEN \
  (sizeof(GRPC_CHTTP2_CLIENT_CONNECT_STRING) - 1)
```

服务端收到这串,就确认"对方要谈 HTTP/2",否则按 RFC 9113 §3.4 必须返回 `PROTOCOL_ERROR` 关连接。服务端校验入站 preface 的逻辑在 [`http2_transport.cc`](../grpc/src/core/ext/transport/chttp2/transport/http2_transport.cc#L97-L111)(Promise 版的 helper)。

### 双方都发 SETTINGS:协商连接级参数

preface 之后,**客户端和服务端各自必须立刻发一个 SETTINGS 帧**——这是双方对连接级参数的初始协商。SETTINGS 帧里携带一组 `(id, value)` 对,告诉对方"我希望这条连接按这些参数运作"。最重要的几项(在 chttp2 里定义于 [`http2_settings.h`](../grpc/src/core/ext/transport/chttp2/transport/http2_settings.h#L37-L69)):

| ID | 设置项 | RFC 9113 默认值 | gRPC chttp2 默认值 |
|---|---|---|---|
| 1 | SETTINGS_HEADER_TABLE_SIZE | 4096 | 4096 |
| 2 | SETTINGS_ENABLE_PUSH | true | false(gRPC 强制关) |
| 3 | SETTINGS_MAX_CONCURRENT_STREAMS | ∞(不限) | ∞ |
| 4 | SETTINGS_INITIAL_WINDOW_SIZE | 65535(2^16) | 65535 |
| 5 | SETTINGS_MAX_FRAME_SIZE | 16384(2^14) | 16384 |
| 6 | SETTINGS_MAX_HEADER_LIST_SIZE | ∞ | 16 MB |

注意几件事:

1. **gRPC 强制 `ENABLE_PUSH = false`**(客户端禁止服务端 push)。在 chttp2 的 `InitLocalSettings` 里直接 `SetEnablePush(false)` + `SetMaxConcurrentStreams(0)` 双保险。因为 gRPC 的流只由客户端发起,服务端 push 在 gRPC 模型里没有意义,关掉它还能省一条攻击面。
2. **INITIAL_WINDOW_SIZE = 65535,正是 RFC 9113 的默认值**(别和 MAX_FRAME_SIZE 的默认 16384 混淆——那是另一个设置项)。gRPC 在静态 SETTINGS 上**没有**改动这个初始窗口,它真正的流控调整是**运行时**按 BDP 估计动态推高的目标窗口(可远超 65535):用 SETTINGS_INITIAL_WINDOW_SIZE 落地、靠 delta 让 O(1) 更新。这才是 gRPC 在 HTTP/2 流控之上的"自有调整",P2-09 会拆透。
3. **gRPC 还扩展了三个私有 setting**(Wire ID 65027/65028/65029):`GRPC_ALLOW_TRUE_BINARY_METADATA`、`GRPC_PREFERRED_RECEIVE_CRYPTO_FRAME_SIZE`、`GRPC_ALLOW_SECURITY_FRAME`。这些是 gRPC 自有的协议扩展,不在 RFC 9113 里——HTTP/2 允许这种扩展,因为是 Wire ID 32 bit,空间绰绰有余。

SETTINGS 协商完(双方都收到对方的 SETTINGS 并回了 ACK),这条连接就**就绪(READY)**,可以开流跑 RPC 了。这是 SubChannel 连接状态机里 "CONNECTING → READY" 这一跳的协议层实质(P4-14 会回扣)。

---

## 五、HTTP/2 给 gRPC 的三件礼物:背压、流式、取消

讲完协议机制,回到主线——为什么 HTTP/2 对 gRPC 来说是"天然底座"。除了消除队头阻塞、生态通用这两件直接收益,HTTP/2 的"流"还给 gRPC 带来三件 HTTP/1.1 老式 RPC 给不了的礼物。这三件礼物,正是 gRPC 把"一次方法调用变成一条流"后,得到的真实回报。

### 礼物一:背压——消费者跟不上,生产者自动减速

流自带**信用制流量控制**(发明五)。当服务端 handler 处理不过来时,它**只是不回 WINDOW_UPDATE**,客户端的 window 就会被 DATA 帧扣到 0,自动停止发送。这是一种天然的"消费者驱动的背压"——不需要任何额外的限流逻辑,协议本身就把"淹死消费者"挡在了门外。

> **不这样会怎样**:如果没有流的背压,生产者发了 100 GB 数据,消费者只来得及处理 1 GB,剩下 99 GB 全堆在服务端内存里——OOM 是迟早的事。HTTP/1.1 时代的应用层限流都得自己写(令牌桶、信号量、队列长度),写错了就 OOM。HTTP/2 的双层 window 让这件事**协议内置**。这是 gRPC 在高并发下稳定性的根。

### 礼物二:流式——大数据分批发,不必一次到位

流的双向字节流特性,让"分批发"成为一等公民。`server-streaming`(订阅股价)就是"客户端发 1 个请求,服务端回 N 个 DATA 帧";`bidi-streaming`(实时聊天)就是"两边都在发 DATA 帧"。**这些模式在 HTTP/2 上没有任何特殊处理**,它们就是流的自然形态。

> **不这样会怎样**:如果用 HTTP/1.1 做 streaming,要么用 chunked transfer(语义不对,它是响应分块,不是双向流),要么另起炉灶(WebSocket)。gRPC 的"一切皆流"让 unary 和 streaming **共享同一套传输、流控、负载均衡**——这是它 API 简洁的根。

### 礼物三:精准取消——关一条流不影响同连接其他流

HTTP/2 的 RST_STREAM 帧可以**单条流地终止**——客户端取消一次调用,只发一个 RST_STREAM 给那条流的 stream_id,**同连接的其他调用完全不受影响**。这是 HTTP/1.1 给不了的:HTTP/1.1 取消一个请求通常意味着关掉那条连接,而那条连接上可能没有别的调用(因为独占),也可能有(用 pipeline,但 pipeline 几乎没人用)。

> **钉死这件事**:HTTP/2 给 gRPC 的三件礼物——**背压**(协议内置限流,免 OOM)、**流式**(分批发是一等公民)、**精准取消**(RST_STREAM 单条流终止)——都是 HTTP/1.1 老式 RPC 给不了的。它们组合起来,正好是 RPC 在生产环境里最想要的"传输套餐"。

---

## 六、技巧精解:9 字节帧头为什么这么排 + gRPC 为什么自加 SECURITY 帧

本章挑两个最硬核的技巧单独拆透。

### 技巧一:9 字节帧头的字段顺序——一个被精心设计的"宇宙常数"

回头看 9 字节帧头:`Length(3) | Type(1) | Flags(1) | Reserved(1bit) | Stream ID(31bit)`。这个顺序不是随便排的,它有几个精心设计的细节:

**1. Length 放最前面,3 字节。** 这让接收端**先读 3 字节就知道"这一帧有多大"**,从而可以精确地分配缓冲、提前判断是否超过 SETTINGS_MAX_FRAME_SIZE。如果 Length 放后面,接收端得先读完整帧才能知道大小——那就失去了"流式解析"的意义。

**2. Type 在 Flags 前,各 1 字节。** 解析时先知道"这是什么帧"(Type),再按 Type 解释 Flags 的语义(DATA 的 END_STREAM 和 HEADERS 的 END_HEADERS 都是 bit 0,但语义不同)。**Flags 的语义是 Type 决定的**——这是 HTTP/2 的协议紧凑性:用 1 字节复用了 8 个开关给所有帧类型。

**3. Reserved 1 bit + Stream ID 31 bit。** 这个 Reserved bit 是 HTTP/2 留的"未来扩展位",必须发 0、收 1 视为 PROTOCOL_ERROR。Stream ID 31 bit 意味着一条连接上最多 2^31 条流(实际还受 MAX_CONCURRENT_STREAMS 限制,gRPC 默认 ∞)。**31 bit 而非 32 bit**,是因为最高位被 Reserved 占了——这是协议设计里常见的"留一位保命"。

**4. Stream ID 用网络字节序(大端),且最高位是 Reserved。** 解析时 chttp2 用 `Read31bits` 取低 31 位。这个细节在 [`frame.cc`](../grpc/src/core/ext/transport/chttp2/transport/frame.cc) 的 `Http2FrameHeader::Parse` 里。

> **不这样会怎样**:如果 Length 放后面,或者 Stream ID 用 32 bit 不留 Reserved,都会让协议变得不灵活或解析变慢。**9 字节帧头是 HTTP/2 协议设计里最紧凑、最前瞻的一个细节**——它用 9 字节编码了"这一帧多大、是什么、带什么标志、属于哪条流",而且字段顺序是性能与扩展性的折中。

### 技巧二:gRPC 自加的 SECURITY 帧(Type=200)——协议扩展的真实案例

回看前面的 FrameType 枚举,有一条 `kCustomSecurity = 200`。这是 gRPC **自己扩展的 HTTP/2 帧类型**,不在 RFC 9113 里。HTTP/2 规定:跳过未知 Type 的帧(对得上 RFC 9113 §5.5),所以 gRPC 可以加自己的帧,只要对方不认识就跳过。但 gRPC 的两端都认识它——它用来在 HTTP/2 上**前置交换安全握手数据**(ALTS 等),避免 TLS 握手的开销或兼容性问题。

这个扩展的存在,说明一个事:**HTTP/2 是可扩展的协议**——Type 8 bit 给了 256 种帧的空间,RFC 9113 只占了 10 种,剩下的留给应用扩展。gRPC 用 Type=200 走自己路,既复用了 HTTP/2 的 framing,又拿到了自定义协议的灵活性。这是"公开标准 + 自有扩展"的甜点——既不割裂生态(对方不认识就跳过),又能塞进自己的东西。

> **钉死这件事**:本章的两个技巧——9 字节帧头的字段顺序(紧凑 + 前瞻)、gRPC 的 SECURITY 帧(协议扩展的真实案例)——都是 HTTP/2 协议设计里的精品。它们让 HTTP/2 在"标准化"和"可扩展"之间取得了平衡,gRPC 恰好是这个平衡的受益者。

---

## 七、章末小结

### 回扣主线

本章是第 2 篇(HTTP/2 传输)的入口。它讲的是 gRPC 三件套里"**HTTP/2 流**"这一件的协议基础——属于二分法的**协议层**那一面(把方法调用编码成网络上流动的字节)。

回到全书主线:**如何让一次跨网络、跨语言的方法调用像本地调用一样自然,又不假装网络不存在?——核心是把一次方法调用变成 HTTP/2 上的一条可控的"流"**。本章拆的是这条流的**协议载体**:HTTP/2 的二进制分帧、流、多路复用、HPACK、双层流量控制。下一章 P2-06 会钻进 chttp2 transport,看一个 TCP 连接怎么同时管理成千上万条流;P2-07 拆 HPACK 到源码级;P2-08 拆 framing;P2-09 拆流量控制。

### 五个为什么

1. **为什么 gRPC 选 HTTP/2 而非 HTTP/1.1?**——HTTP/1.1 的"独占连接 + 文本 + 无流"扛不住海量并发(应用层队头阻塞),TCP 层队头阻塞还在底下。HTTP/2 的二进制分帧 + 流 + 多路复用把应用层队头阻塞彻底消除。
2. **为什么 gRPC 选 HTTP/2 而非私有 TCP 协议?**——私有协议(早期 Thrift/Dubbo/Stubby)性能可以很好,但**生态割裂**——一种协议一套工具、监控、网关。HTTP/2 是公开标准,能穿过任何 HTTP 设施,让 gRPC 走出 Google 成为跨语言标准。
3. **为什么"流"是 RPC 的第一性概念?**——流带来多路复用(省连接)、背压(协议内置限流)、精准取消(RST_STREAM 单条流终止),以及"一切皆流"的统一(unary/streaming 共用一套传输)。这些都是 RPC 在生产环境最想要的。
4. **为什么 9 字节帧头这么排?**——Length 放前面让接收端先知道大小、Type 在 Flags 前让 Flags 语义由 Type 决定、Stream ID 31 bit 留 1 bit Reserved 保扩展性。这是协议设计的紧凑与前瞻折中。
5. **为什么本书从这一章起对 RFC 9113 而非 RFC 7540?**——gRPC 1.83 的 chttp2 源码里 `RFC 7540` 引用归零、`RFC 9113` 引用 124 处,协议层已全量迁移到 HTTP/2 bis(RFC 9113,2022)。这是按真实源码诚实标注,老博客大片过时。

### 想继续深入往哪钻

- **想读协议标准本身**:RFC 9113(HTTP/2 bis,2022)的 §4(Frame Format)、§5(Streams)、§6(Frame Definitions)、§6.9(Flow Control)是本章协议机制的权威定义。
- **想看 chttp2 怎么解析帧**:读 [`frame.cc`](../grpc/src/core/ext/transport/chttp2/transport/frame.cc) 的 `ParseFramePayload`(集中式分发)和 [`frame.h`](../grpc/src/core/ext/transport/chttp2/transport/frame.h) 的 `Http2FrameHeader`。
- **想看各帧的语义处理**:`frame_data.cc` / `frame_settings.cc` / `frame_ping.cc` / `frame_goaway.cc` / `frame_rst_stream.cc` / `frame_window_update.cc`,每个文件一个 begin_frame + parse。
- **想理解 HPACK**:RFC 7541(注意 HPACK 的 RFC 没有变,还是 7541)和本书 P2-07。
- **想理解流量控制**:RFC 9113 §6.9 和本书 P2-09。
- **想看连接怎么建立**:读 chttp2 的 connection preface 校验([`http2_transport.cc`](../grpc/src/core/ext/transport/chttp2/transport/http2_transport.cc))和 SETTINGS 协商([`http2_settings.cc`](../grpc/src/core/ext/transport/chttp2/transport/http2_settings.cc) / [`http2_settings_manager.cc`](../grpc/src/core/ext/transport/chttp2/transport/http2_settings_manager.cc))。

### 引出下一章

我们搞清楚了 HTTP/2 的五件发明和它们在 chttp2 里的协议骨架。但还有一个问题没回答:**一个 TCP 连接,怎么同时管理成千上万条并发流?谁在记录哪条流的状态、谁在把多条流的数据攒成一批发出去?** 这就是 chttp2 transport 的核心职责——stream 表(花名册)、写循环攒批、stream 的生命周期。下一章 P2-06,我们钻进 [`chttp2_transport.cc`](../grpc/src/core/ext/transport/chttp2/transport/chttp2_transport.cc),跟着一条流从生到死,看 transport 怎么在一条 TCP 上调度出海量并发。

> **下一章**:[P2-06 · chttp2 transport 全貌:一条流的一生](P2-06-chttp2-transport全貌-一条流的一生.md)
