# 第 2 篇 · 第 8 章 · gRPC framing 与帧解析

> **核心问题**:HPACK 把头部压到几乎零字节(P2-07)之后,**消息体**(protobuf 序列化后的字节)怎么塞进 HTTP/2 的 DATA 帧?除了 DATA,一条 HTTP/2 连接上还跑着 SETTINGS、PING、GOAWAY、RST_STREAM、WINDOW_UPDATE 一堆控制帧,它们各司什么职、什么时候发、谁先谁后?服务端处理完一个调用,`grpc-status: 0` 这种状态码又是怎么随 trailer 回到客户端的?更尖锐一点——gRPC 在 HTTP/2 这套 9 字节帧头之上,自己又叠了一层"**5 字节 Length-Prefixed-Message**",为什么不直接用 protobuf 字节?这 5 字节里有什么学问?

> **读完本章你会明白**:
> 1. gRPC 在 HTTP/2 DATA 帧之上叠的那层 framing 长什么样:**1 字节压缩标志 + 4 字节大端长度 + payload**(`kGrpcHeaderSizeInBytes = 5`),而且**这 4 字节长度是定长大端、不是 varint**——这和 protobuf 自己的 varint 编码是两回事,故意不一致。
> 2. chttp2 读侧的字节级状态机(`parsing.cc` 的 `grpc_chttp2_perform_read`)怎么把 TCP 字节流先切成 9 字节帧头、再切 payload、再按帧类型分发到 `frame_data.cc` / `frame_settings.cc` / `frame_ping.cc` 等处理器——以及为什么是逐字节状态机而非"读完整帧再处理"。
> 3. 六种控制帧各管什么事:SETTINGS 协商 6 项标准 + 3 项 gRPC 私有参数;PING 探活与 RTT 测量;GOAWAY 优雅关闭;RST_STREAM 异常终止单条流;WINDOW_UPDATE 给流控加信用;还有 gRPC 自己加的 SECURITY 帧(Type=200)。
> 4. 经典版 `frame_*.cc`(每帧一个 parser)和新版 `frame.cc`(集中式 `ParseFramePayload` 用 `std::variant` 统一分发)两套解析器并存的现实——这正是 gRPC "换骨架"在 framing 层的现场。

> **如果一读觉得太难**:先只记住三件事——① **gRPC 消息 = 5 字节头(1 字节压缩位 + 4 字节大端长度)+ protobuf 字节**,这层 framing 叠在 HTTP/2 DATA 帧的 payload 里;② **HTTP/2 帧分两类**:连接级(SETTINGS/PING/GOAWAY,stream_id 必须 = 0)和流级(DATA/HEADERS/RST_STREAM/WINDOW_UPDATE,stream_id ≠ 0);③ **gRPC 调用的状态码走 trailer**(`grpc-status` / `grpc-message` 作为 HEADERS 帧的尾部块发回),这是 HTTP/2 让 gRPC "复用 HTTP 语义"的关键。

---

## 〇、一句话点破

> **gRPC framing 干两件事:① 在 HTTP/2 DATA 帧的 payload 里再套一层 5 字节的 gRPC 私有头(1 字节压缩位 + 4 字节大端长度),用来在一条流里切分出"一条调用里的 N 条 protobuf 消息";② 让 HTTP/2 那 10 种帧各司其职——DATA 载消息、HEADERS 载头部和 trailer、SETTINGS 协商参数、PING 探活、GOAWAY 关连接、RST_STREAM 关单流、WINDOW_UPDATE 调流控。读侧用一个逐字节状态机把 TCP 字节流切成帧,写侧把多条流的帧攒成一批发出去。**

这是结论,不是理由。本章倒过来拆:先讲"为什么 gRPC 要在 DATA 帧之上再叠一层 framing",再讲这层 framing 的字节布局和 5 字节头的学问,然后钻进读侧状态机和写侧攒批,最后把六种控制帧的职责和源码逐一摊开——并诚实交代 chttp2 平行存在的两套解析器。

---

## 一、为什么 gRPC 要在 DATA 帧之上再叠一层 framing

P2-07 把 HPACK 拆透了——头部压缩到几乎零字节。但一次 gRPC 调用除了头部,还有**消息体**(protobuf 序列化后的字节流)。HTTP/2 已经给了 DATA 帧来传应用数据,看起来直接塞进去就行,为什么 gRPC 还要在上面再套一层?

### 先看一条流里到底要传什么

回想 P2-05 讲的"gRPC 四种调用模式本质都是流"。一条流上的数据,按 HTTP/2 视角是:

```
   一条 HTTP/2 流(stream id = 1,客户端 → 服务端)
   ┌──────────────────────────────────────────┐
   │ HEADERS 帧(:path, content-type, ...)    │  ← 初始元数据(请求头)
   │ DATA 帧(消息 1 的字节)                  │
   │ DATA 帧(消息 2 的字节,client-streaming)│
   │ DATA 帧(...)                            │
   │ HEADERS 帧(grpc-status, END_STREAM)     │  ← trailer
   └──────────────────────────────────────────┘
```

HTTP/2 的 DATA 帧本身**只是"一段属于某条流的字节"**,它不带任何"这一段字节是哪条消息""这条消息多大""有没有被压缩"的信息。HTTP/2 把"字节属于哪条流"解决了(Stream ID),但**没解决"一条流里的字节怎么切分成消息"**——这是上层协议的责任。

### 不套一层 framing 会怎样

> **不这样会怎样**:如果 gRPC 直接把 protobuf 字节塞进 DATA 帧不加分隔,服务端收到一条 `client-streaming` 调用(客户端发多条消息)时,**根本不知道这条 DATA 帧里的字节是 1 条消息还是 2 条粘在一起,也不知道第 1 条消息在哪结束、第 2 条从哪开始**。TCP 是字节流,内核 read 可能一次返回"半条消息",也可能返回"1.5 条消息",应用层必须自己有"消息边界"的约定。

这正是所有 RPC 协议都要解决的问题——"**length-prefix**"(长度前缀)是最朴素也最有效的答案:**每条消息前面加个长度字段,告诉对方"这条消息有 N 字节"**。gRPC 沿用了这个套路,但它选了一种**特别紧凑、特别定长**的格式。

### gRPC 的答案:5 字节 Length-Prefixed-Message

gRPC 官方协议规定([github.com/grpc/grpc/blob/master/doc/PROTOCOL-HTTP2.md](https://github.com/grpc/grpc/blob/master/doc/PROTOCOL-HTTP2.md)),一条 gRPC 消息在 HTTP/2 DATA 帧的 payload 里这样编码:

```
   Length-Prefixed-Message(对得上 gRPC over HTTP/2 协议)
   ┌─────────────┬─────────────────────────────┬───────────────────┐
   │ Compressed- │        Message-Length        │   Message         │
   │ Flag (1 B)  │        (4 B, big-endian)     │   (Length 字节)   │
   └─────────────┴─────────────────────────────┴───────────────────┘
        共 5 字节的 gRPC 头(kGrpcHeaderSizeInBytes)
```

- **Compressed-Flag(1 字节)**:`0` = 未压缩,`1` = 用 `grpc-encoding` 声明的算法(gzip/zstd 等)压缩过。**只有最低位有意义**,其他位必须为 0。
- **Message-Length(4 字节,大端序)**:后面 Message 的字节数。4 字节 → 单条消息最大 2^32 - 1 ≈ 4 GB(gRPC 默认上限 4 MB,可调)。
- **Message(变长)**:protobuf 序列化后的字节(或压缩后的字节)。

这个 5 字节头的常量定义在 [`frame.h`](../grpc/src/core/ext/transport/chttp2/transport/frame.h#L254-L261):

```cpp
constexpr uint8_t kGrpcHeaderSizeInBytes = 5;
constexpr uint8_t kGrpcMessageHeaderNoFlags = 0;
constexpr uint8_t kGrpcMessageHeaderWriteInternalCompress = 1;

struct GrpcMessageHeader {
  uint32_t flags = 0;
  uint32_t length = 0;
};
```

配套两个辅助函数(下一节钻进源码):`ExtractGrpcHeader` 从 payload 头部解出 5 字节,`AppendGrpcHeaderToSliceBuffer` 把 5 字节头加在 payload 前面。

> **钉死这件事**:gRPC 在 HTTP/2 DATA 帧之上叠了一层 5 字节的 Length-Prefixed-Message framing,**解决了"一条流里怎么切分多条 protobuf 消息"的问题**。HTTP/2 的 DATA 帧只管"这段字节属于哪条流",gRPC 的 5 字节头管"这条消息多大、有没有压"。两层各司其职。

---

## 二、5 字节头的学问:为什么是"压缩位 + 定长大端"而不是 varint

这一节回答一个看过 protobuf 编码(P1-03)的人自然会问的问题:**protobuf 自己用 varint(变长整数)压字段长度,为什么 gRPC 这层 framing 反而用定长 4 字节大端?**这不是不一致吗?这是个非常有意思的协议设计取舍。

### 先回顾 protobuf 自己的 varint

P1-03 讲过,protobuf 的字段长度用 varint 编码——每 7 位 + 1 续位,小消息长度只占 1 字节,大消息才占多字节。varint 的设计目标是"**省字节**":大多数 protobuf 消息的字段值小,varint 能把 `length=5` 压成 1 字节(而不是定长 4 字节)。

如果 gRPC framing 也用 varint,理论上能省 3 字节/消息——在百万 QPS 下,这是可观的带宽。但 gRPC **故意不用 varint**。为什么?

### 不用 varint 会怎样

> **不这样会怎样**:用 varint 有三个隐形代价。

**第一,解析时要"边读边判断"。** varint 是变长的,解析方不知道下一个 length 字段是 1 字节还是 5 字节,得**逐字节读 + 测高位续位**。HTTP/2 DATA 帧从 TCP 来时,可能一次 read 返回任意字节(半帧、1.5 帧、3 帧粘一起),解析方要先"凑够 varint 的所有字节"才能算出 length,中间状态多。

**第二,大消息(>127 字节)反而比定长还长。** varint 1 字节只能表示 0~127,超过 127 就要 2 字节,超过 16383 要 3 字节,以此类推。gRPC 的典型消息是几百字节到几 MB,大部分 > 127 → varint 至少 2 字节,2 字节 varint 上限是 16383,稍大点的消息就要 3 字节。**对 KB 级消息,varint 只比定长 4 字节省 1~2 字节,但付出了"解析复杂度"的代价**。

**第三,定长大端可以一次 `memcpy` + 一次 `memcmp`,零分支。** gRPC 选 4 字节定长大端,解析方"凑够 5 字节"后,一次 `Read4b` 读出 length,**没有循环、没有续位判断、没有分支预测失败**。这对百万 QPS 下解析 framing 的 CPU 开销是决定性的。

### gRPC 的源码:5 字节头是怎么解的

看新版 `frame.cc` 里 `ExtractGrpcHeader` 的真实实现,在 [`frame.cc`](../grpc/src/core/ext/transport/chttp2/transport/frame.cc#L805-L818):

```cpp
ValueOrHttp2Status<GrpcMessageHeader> ExtractGrpcHeader(SliceBuffer& payload) {
  GRPC_CHECK_GE(payload.Length(), kGrpcHeaderSizeInBytes);
  uint8_t buffer[kGrpcHeaderSizeInBytes];
  payload.CopyFirstNBytesIntoBuffer(kGrpcHeaderSizeInBytes, buffer);
  GrpcMessageHeader header;
  header.flags = ParseGrpcMessageFlags(buffer[0]).value();   // byte[0] = 压缩位
  header.length = Read4b(buffer + 1);                        // byte[1..4] = 大端长度
  return header;
}
```

配套的 `Read4b`(大端读 4 字节)在 [`frame.cc`](../grpc/src/core/ext/transport/chttp2/transport/frame.cc#L159-L166):

```cpp
uint32_t Read4b(const uint8_t* input) {
  return static_cast<uint32_t>(input[0]) << 24 |
         static_cast<uint32_t>(input[1]) << 16 |
         static_cast<uint32_t>(input[2]) << 8 |
         static_cast<uint32_t>(input[3]);
}
```

写侧对称,`AppendGrpcHeaderToSliceBuffer` 在 [`frame.cc`](../grpc/src/core/ext/transport/chttp2/transport/frame.cc#L820-L825):

```cpp
void AppendGrpcHeaderToSliceBuffer(SliceBuffer& payload, const uint32_t flags,
                                   const uint32_t length) {
  uint8_t* frame_hdr = payload.AddTiny(kGrpcHeaderSizeInBytes);
  frame_hdr[0] = SerializeGrpcMessageFlags(flags);   // 1 字节压缩位
  Write4b(length, frame_hdr + 1);                    // 4 字节大端长度
}
```

整段代码无循环、无续位、无分支——5 字节头就是一个固定 layout 的 struct。

### 这层 framing 在两种"代码形态"里的物理位置

gRPC 把这层 framing 放在哪儿,经典版和新 Promise 版略有不同:

- **经典版**:5 字节头的解析逻辑嵌在 `frame_data.cc` 的 `grpc_deframe_unprocessed_incoming_frames` 里(下一节会看到),和 DATA 帧的处理混在一起。
- **Promise 版**(新版):抽出来成一个独立的 `GrpcMessageAssembler` / `GrpcMessageDisassembler`(在 [`message_assembler.h`](../grpc/src/core/ext/transport/chttp2/transport/message_assembler.h)),它**只负责"5 字节 gRPC 头 ↔ gRPC Message"的转换**,和 HTTP/2 DATA 帧解耦。

```cpp
// message_assembler.h(简化,展示发送侧的攒帧)
void PrepareMessageForSending(MessageHandle message) {
  AppendGrpcHeaderToSliceBuffer(message_, message->flags(),
                                message->payload()->Length());
  message_.Append(*(message->payload()));
}
```

接收侧 `ExtractMessage` 在累积的 DATA payload 里反复切 5 字节头 + length 字节消息:

```cpp
// message_assembler.h(简化,展示接收侧的拆帧)
MessageHandle ExtractMessage() {
  ...
  auto header = ExtractGrpcHeader(message_buffer_);
  uint32_t header_length = header.value().length;
  // 丢弃 5 字节 gRPC 头
  SliceBuffer discard;
  message_buffer_.MoveFirstNBytesIntoSliceBuffer(kGrpcHeaderSizeInBytes, discard);
  // 切出 header_length 字节作为 Message
  MessageHandle grpc_message = Arena::MakePooled<Message>();
  message_buffer_.MoveFirstNBytesIntoSliceBuffer(header_length,
                                                  *(grpc_message->payload()));
  grpc_message->mutable_flags() = header.value().flags;
  return std::move(grpc_message);
}
```

> **钉死这件事**:gRPC framing 用"1 字节压缩位 + 4 字节定长大端长度",**故意不用 protobuf 那套 varint**。这不是不一致,而是**协议层的取舍**:解析简单性(零分支定长读)和省 1~3 字节之间,gRPC 选了前者——因为这条路径在百万 QPS 下被走无数次,CPU 开销比几字节带宽值钱。**这是 gRPC 把"协议解析性能"置于"极致压缩比"之上的一个真实取舍**,和 HPACK 那种"压到几乎零字节"的极致压缩形成有趣对照——头部压得狠(因为重复),消息体不压(因为每条都不同,定长解析更快)。

### 补一刀:gRPC 自家的 varint 只用在 HPACK

读者可能困惑:gRPC 源码里确实有个 `varint.cc`,如果不用在消息 framing,用在哪?**只用在 HPACK 整数编码**(RFC 7541 §5.1 的 7/6/5/4-bit prefix 变长整数)和 `grpc-message` trailer 值的长度。打开 [`varint.h`](../grpc/src/core/ext/transport/chttp2/transport/varint.h) 第 28 行,注释写得明明白白:`// Helpers for hpack varint encoding`。`hpack_encoder.cc` 引它编 HPACK 整数,`chttp2_transport.cc` 第 2847 行用 `VarintWriter<1>` 编 `grpc-message` trailer 值的长度(这是 HPACK 字面量 value 的 varint,对得上 RFC 7541)。**gRPC 的 5 字节消息 framing 完全不走 varint**。

---

## 三、读侧状态机:把 TCP 字节流切成帧

讲完了 gRPC 的 5 字节头,现在往上退一层——HTTP/2 自己的 9 字节帧头(P2-05 讲过)是怎么从 TCP 字节流里切出来的。这是 `parsing.cc` 的职责。

### 一个 TCP read 可能返回任意字节

HTTP/2 跑在 TCP 上,TCP 是**字节流**——一次 `read` 可能返回任意字节数,可能:

- 只返回半个 9 字节帧头(下次 read 才有另一半)。
- 返回 1.5 个帧(头 + 半个 payload)。
- 返回 5 个完整帧粘一起。

解析方**不能假设"一次 read 正好一个帧"**。它必须维护"我读到这个帧的第几个字节了"的状态,等字节慢慢凑齐。这就是**字节级状态机**的职责。

### gRPC 经典版的状态机:`grpc_chttp2_perform_read`

经典版读侧的主入口是 [`grpc_chttp2_perform_read`](../grpc/src/core/ext/transport/chttp2/transport/parsing.cc#L215-L422),它的形态是一个**巨型 switch + goto fallthrough**,不是一个高级的"读帧循环"。状态枚举在 [`internal.h`](../grpc/src/core/ext/transport/chttp2/transport/internal.h#L166-L206):

```
GRPC_DTS_CLIENT_PREFIX_0..23   // 24 个状态:逐字节匹配连接 preface
GRPC_DTS_FH_0..8               // 9 个状态:逐字节拼 9 字节帧头
GRPC_DTS_FRAME                 // 帧头完整,处理 payload
```

三阶段的工作:

**阶段 1:吃掉客户端 connection preface**(24 字节 magic)。状态 `GRPC_DTS_CLIENT_PREFIX_0..23`(`parsing.cc` 行 228-271),逐字节匹配 `PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n`。这 24 字节是 HTTP/2 客户端必须先发的"暗号",服务端逐字节校验,任何一个字节不对就 PROTOCOL_ERROR 关连接(对得上 RFC 9113 §3.4)。

**阶段 2:拼出 9 字节帧头**。状态 `GRPC_DTS_FH_0..8`(`parsing.cc` 行 278-344):

- `FH_0/1/2`:拼出 24 位 `incoming_frame_size`(大端,逐字节 `<<16` / `<<8` / OR)。
- `FH_3`:`incoming_frame_type`(DATA=0、HEADERS=1、RST_STREAM=3、SETTINGS=4、PING=6、GOAWAY=7、WINDOW_UPDATE=8、SECURITY=200)。
- `FH_4`:`incoming_frame_flags`。
- `FH_5..8`:31 位 `incoming_stream_id`(最高位是 Reserved,必须为 0,在 `& 0x7f` 掩码)。

到 `FH_8` 末尾,调 `init_frame_parser`(`parsing.cc` 行 350),按帧类型初始化对应的 parser,然后转 `GRPC_DTS_FRAME`。

**阶段 3:处理帧体**。状态 `GRPC_DTS_FRAME`(`parsing.cc` 行 376-417),把后续字节喂给阶段 2 选定的 parser(`parse_frame_slice`),直到吃够 `incoming_frame_size` 字节。吃完一个帧,`goto dts_fh_0` 回到帧头阶段,开始下一个帧。

### 帧类型分发:init_frame_parser

帧头拼齐后,`init_frame_parser` 按帧类型分发,在 [`parsing.cc`](../grpc/src/core/ext/transport/chttp2/transport/parsing.cc#L463-L493):

```cpp
switch (t->incoming_frame_type) {
  case GRPC_CHTTP2_FRAME_DATA:
    return init_data_frame_parser(t);                              // frame_data.cc
  case GRPC_CHTTP2_FRAME_HEADER:
    return init_header_frame_parser(t, 0, requests_started);       // 触发 HPACK
  case GRPC_CHTTP2_FRAME_CONTINUATION:
    return GRPC_ERROR_CREATE("Unexpected CONTINUATION frame");
  case GRPC_CHTTP2_FRAME_RST_STREAM:
    return init_rst_stream_parser(t);                              // frame_rst_stream.cc
  case GRPC_CHTTP2_FRAME_SETTINGS:
    return init_settings_frame_parser(t);                          // frame_settings.cc
  case GRPC_CHTTP2_FRAME_WINDOW_UPDATE:
    return init_window_update_parser(t);                           // frame_window_update.cc
  case GRPC_CHTTP2_FRAME_PING:
    return init_ping_parser(t);                                    // frame_ping.cc
  case GRPC_CHTTP2_FRAME_GOAWAY:
    return init_goaway_parser(t);                                  // frame_goaway.cc
  case GRPC_CHTTP2_FRAME_SECURITY:                                 // gRPC 扩展 Type=200
    return init_security_frame_parser(t);
  default:
    return init_non_header_skip_frame_parser(t);                   // 跳过未知帧
}
```

每个 `init_*` 把 transport 上的 `parser` 字段指向对应 `frame_*.cc` 里的 `*_parser_parse` 函数。每种帧有独立的 parser 对象,按当前帧类型激活对应的那个。

> **不这样会怎样**:如果不是逐字节状态机,而用"一次性把整帧读出来再处理",解析方要么阻塞等够字节(失去流式解析能力,网络慢时连接卡死),要么在"半帧到达"时丢字节(协议错乱)。逐字节状态机让 chttp2 **能处理任意粒度的 TCP read**,字节到一点处理一点,状态完整保留——这是流式协议解析的标准手法,也是 HTTP/2 能跑在高延迟网络上的根。

---

## 四、经典版 vs 新版:两套解析器并存

P2-06 已经讲过 gRPC transport 的"换骨架"(经典 callback 版 vs Promise 版),framing 这一层也不例外——**两套解析器平行存在**:

### 经典版:每帧一个 parser 文件

经典版是上面看到的那套——`parsing.cc` 的字节级状态机 + 每种帧一个独立文件:

| 帧类型 | 经典版文件 | 入口函数 |
|---|---|---|
| DATA | `frame_data.cc` | `init_data_frame_parser` / `grpc_chttp2_data_parser_parse` |
| HEADERS | `hpack_parser.cc`(via `init_header_frame_parser`) | HPACK 解码 |
| RST_STREAM | `frame_rst_stream.cc` | `init_rst_stream_parser` |
| SETTINGS | `frame_settings.cc` | `init_settings_frame_parser` |
| PING | `frame_ping.cc` | `init_ping_parser` |
| GOAWAY | `frame_goaway.cc` | `init_goaway_parser` |
| WINDOW_UPDATE | `frame_window_update.cc` | `init_window_update_parser` |
| SECURITY | `frame_security.cc` | `init_security_frame_parser` |

每个 `frame_*.cc` 内部又有自己的小状态机(`*_parser_begin_frame` 校验帧头、`*_parser_parse` 逐字节吃 payload)。这些文件都是 **legacy** 形态——它们用 `grpc_error*` 返回错误(老错误处理),用裸指针和 C 风格 struct,带 `_locked` 后缀的函数在 combiner 里跑。

### 新版(Promise 版):集中式 `ParseFramePayload` + `std::variant`

新版把所有帧类型建模成一个 `std::variant<Http2DataFrame, Http2HeaderFrame, ...>`(叫 `Http2Frame`,见 P2-05 的 `frame.h`),用一个集中的 `ParseFramePayload` 函数分发。在 [`frame.cc`](../grpc/src/core/ext/transport/chttp2/transport/frame.cc#L726-L756):

```cpp
switch (static_cast<FrameType>(hdr.type)) {
  case FrameType::kData:         return ParseDataFrame(hdr, payload);
  case FrameType::kHeader:       return ParseHeaderFrame(hdr, payload);
  case FrameType::kContinuation: return ParseContinuationFrame(hdr, payload);
  case FrameType::kRstStream:    return ParseRstStreamFrame(hdr, payload);
  case FrameType::kSettings:     return ParseSettingsFrame(hdr, payload);
  case FrameType::kPing:         return ParsePingFrame(hdr, payload);
  case FrameType::kGoaway:       return ParseGoawayFrame(hdr, payload);
  case FrameType::kWindowUpdate: return ParseWindowUpdateFrame(hdr, payload);
  case FrameType::kPushPromise:
    return Http2Status::Http2ConnectionError(... kNoPushPromise ...);
  case FrameType::kCustomSecurity: return ParseSecurityFrame(hdr, payload);
  default: return ValueOrHttp2Status<Http2Frame>(Http2UnknownFrame{});
}
```

每个 `ParseXxxFrame` 返回 `ValueOrHttp2Status<Http2Frame>`——要么是一个解析好的帧 variant,要么是一个连接级/流级错误。帧类型枚举 `FrameType` 在 [`frame.cc`](../grpc/src/core/ext/transport/chttp2/transport/frame.cc#L99-L111):

```cpp
enum class FrameType : uint8_t {
  kData = 0,
  kHeader = 1,
  // type 2 was Priority which has been deprecated.   ← RFC 9113 弃用
  kRstStream = 3,
  kSettings = 4,
  kPushPromise = 5,
  kPing = 6,
  kGoaway = 7,
  kWindowUpdate = 8,
  kContinuation = 9,
  kCustomSecurity = 200,  // Custom Frame Type
};
```

注意 Type=2 的注释:`Priority which has been deprecated`——PRIORITY 帧在 RFC 9113 里被明确弃用,gRPC 的 FrameType 枚举也跳过了 2。

### 两套并存的现实

读者会问:两套都在,生产用哪套?答案是**默认用经典版**,Promise 版在 experiment flag(`IsPh2ClientEnabled` / `IsPh2ServerEnabled`)后面(P2-06 已讲)。Promise 版的 `http2_client_transport.cc` / `http2_server_transport.cc` 走 `frame.cc` 的 `ParseFramePayload`;经典版的 `chttp2_transport.cc` 走 `parsing.cc` + `frame_*.cc`。**两套解析器各自独立,不共享代码**,只共享无状态的工具头(`frame.h` 的 struct 定义、`http2_settings.h` 的常量)。

> **钉死这件事**:framing 这层是 gRPC "换骨架"的一个典型现场——**经典版 `parsing.cc` + `frame_*.cc`(每帧一文件)与新版 `frame.cc` 的 `ParseFramePayload`(集中式 variant 分发)平行存在**。协议机制(DATA/HEADERS/SETTINGS 各管什么)两层完全一致,差异在代码组织:经典版是 C 风格散文件,新版是 C++ variant + 集中分发。本书后续以经典版为主线(它是生产默认),在关键处点出 Promise 版差异。

---

## 五、写侧:把多条流的帧攒成一批发出去

P2-06 已经讲过写循环攒批的核心(`writing.cc` 的 `grpc_chttp2_begin_write` + `WriteContext`),这里只补 framing 视角的几个要点。

### DATA 帧是怎么攒出来的

写循环走到一条流有数据要发时,会调 `StreamWriteContext::FlushData`(在 [`writing.cc`](../grpc/src/core/ext/transport/chttp2/transport/writing.cc#L531-L578)),它干的事是:

1. 检查这条流有没有发过初始元数据(没发 HEADERS 的流不能发 DATA)。
2. 检查 `flow_controlled_buffer`(待发字节)有没有东西。
3. **检查流控 window**(连接级 + 流级),决定这次能发多少字节。
4. 在 window 范围内,从 `flow_controlled_buffer` 切出 N 字节,**先套上 5 字节 gRPC 头**(如果这条流正在开始一条新消息),再切成 HTTP/2 DATA 帧(每帧不超过 `MAX_FRAME_SIZE`),序列化进 `outbuf`。
5. 如果 window 用完了,把这条流挂到 `STALLED_BY_TRANSPORT` 或 `STALLED_BY_STREAM` 列表,等下次 WINDOW_UPDATE 来了再恢复(P2-09 拆透)。

注意第 4 步的层次:**应用层消息(Message)→ 5 字节 gRPC 头 + payload → HTTP/2 DATA 帧的 payload → 9 字节 HTTP/2 帧头**。这是三层嵌套。

### Promise 版的攒批:分 urgent / regular 两类帧

Promise 版的攒批换了载体——[`write_cycle.cc`](../grpc/src/core/ext/transport/chttp2/transport/write_cycle.cc) 里的 `WriteBufferTracker`,把帧分两类攒批:`regular_frames_` 和 `urgent_frames_`(两个 `absl::InlinedVector`)。为什么分两类?

- **urgent 帧**(PING ACK、SETTINGS ACK、RST_STREAM):**必须立刻发**,不能攒。收到 PING 立刻回 ACK,这是协议礼仪(RFC 9113 §6.7 要求 PING ACK "as soon as possible")。
- **regular 帧**(DATA、HEADERS):可以攒批,攒够 `target_write_size` 再发。

经典版也有类似区分(`outbuf` vs `qbuf`,后者装来不及发的 ack 类小帧),Promise 版把它显式建模成两类帧的列表,语义更清晰。`SerializeRegularFrames` / `SerializeUrgentFrames` 分别序列化,urgent 先发。

### gRPC 消息拆帧:一条消息跨多个 DATA 帧

值得点出来的一种情况:**一条 gRPC 消息(比如 100 KB)比 `MAX_FRAME_SIZE`(默认 16384 字节)大时,会被切成多个 DATA 帧**。这不是 gRPC 的特殊设计,是 HTTP/2 的硬性规定——每个 DATA 帧的 payload 不能超过 `SETTINGS_MAX_FRAME_SIZE`(对得上 RFC 9113 §4.2)。

```
   一条 100 KB 的 gRPC 消息(MAX_FRAME_SIZE=16384)
   ┌────────────────────────────────────────────────┐
   │ 5 字节 gRPC 头 + 100 KB payload(应用层视角)   │
   ├────────────────────────────────────────────────┤
   │ HTTP/2 切成 7 个 DATA 帧(线缆视角)            │
   │  DATA 1: 16384 B  (stream_id=1)                │
   │  DATA 2: 16384 B  (stream_id=1)                │
   │  DATA 3: 16384 B  (stream_id=1)                │
   │  ...                                           │
   │  DATA 7: ~5 KB + END_STREAM(stream_id=1)       │
   └────────────────────────────────────────────────┘
```

注意 5 字节 gRPC 头**只在第 1 个 DATA 帧的开头**,后续 DATA 帧纯粹是消息字节的延续。接收方累积所有 DATA 帧的 payload,按 5 字节头切分消息。这是 `GrpcMessageAssembler` 的职责(`message_assembler.h` 第 47-54 行的注释明确列出了"1 msg/1 frame、N msgs/1 frame、1 msg/N frames、mixed"四种映射情况)。

> **钉死这件事**:写侧的 framing 是三层嵌套——**应用消息 → 5 字节 gRPC 头 + payload → HTTP/2 DATA 帧(每个 ≤ MAX_FRAME_SIZE)→ 9 字节 HTTP/2 帧头**。写循环把多条流的帧攒成一批发出去(省系统调用),urgent 帧(PING/SETTINGS ACK)优先。

---

## 六、六种控制帧:各司其职

讲完了 DATA 帧(载 gRPC 消息),现在把另外几种控制帧摊开。这些帧都服从 P2-05 讲的 9 字节帧头格式,差异在 Type、Flags、payload 的语义。

### 6.1 SETTINGS:连接级参数协商

SETTINGS 是 HTTP/2 的"连接配置文件"——双方各自在连接开始时发一个,告诉对方"我希望这条连接按这些参数运作"。**SETTINGS 帧的 stream_id 必须 = 0**(连接级,对得上 RFC 9113 §6.5)。payload 是一组 `(id: 2 字节, value: 4 字节)` 对。

gRPC 协商的参数定义在 [`http2_settings.h`](../grpc/src/core/ext/transport/chttp2/transport/http2_settings.h#L37-L69):

| Wire ID | 设置项 | RFC 9113 默认 | gRPC chttp2 默认 | 作用 |
|---|---|---|---|---|
| 1 | HEADER_TABLE_SIZE | 4096 | 4096 | HPACK 动态表大小上限(P2-07) |
| 2 | ENABLE_PUSH | true(1) | **false(0)** | gRPC 强制关 server push |
| 3 | MAX_CONCURRENT_STREAMS | ∞ | ∞(UINT32_MAX) | 最大并发流数 |
| 4 | INITIAL_WINDOW_SIZE | 65535 | 65535 | 流级流控初始窗口(P2-09) |
| 5 | MAX_FRAME_SIZE | 16384 | 16384 | 单帧 payload 最大字节 |
| 6 | MAX_HEADER_LIST_SIZE | ∞ | **16 MB** | 头部列表大小上限(防恶意大头部) |
| 65027 | GRPC_ALLOW_TRUE_BINARY_METADATA | (RFC 无) | **false** | gRPC 私有:是否允许真二进制头 |
| 65028 | GRPC_PREFERRED_RECEIVE_CRYPTO_FRAME_SIZE | (RFC 无) | **0** | gRPC 私有:首选 RX 加密帧大小 |
| 65029 | GRPC_ALLOW_SECURITY_FRAME | (RFC 无) | **false** | gRPC 私有:是否允许 Type=200 SECURITY 帧 |

几个要点:

1. **gRPC 强制 `ENABLE_PUSH = false`**。gRPC 的流只由客户端发起,服务端 push 在 gRPC 模型里没意义,关掉它还省一条攻击面。这一条 gRPC 必发,对得上 RFC 9113 §6.5.2 的"客户端 MUST 设置 ENABLE_PUSH=0 来禁用 push"。
2. **INITIAL_WINDOW_SIZE 默认 65535**——这正是 RFC 9113 §6.5.2 规定的默认值(2^16 - 1 = 65535)。**这里要修正一个常见误区**:很多博客和早期资料说"RFC 默认 INITIAL_WINDOW_SIZE 是 16384,gRPC 调到 65535"——这是**错的**。16384 是 `MAX_FRAME_SIZE` 的默认值,**INITIAL_WINDOW_SIZE 的 RFC 默认就是 65535**。gRPC 这里的默认值和 RFC 9113 一致,没有调大。真正被 gRPC 调大的,是运行时通过 BDP 估计动态推高的 `target_initial_window_size_`(P2-09 会拆透),不是初始 SETTINGS 里的值。
3. **gRPC 扩展了 3 个私有 SETTINGS**(65027/65028/65029)。HTTP/2 允许这种扩展——Wire ID 是 16 bit,空间绰绰有余,RFC 9113 §6.5.3 明确说"未识别的 SETTINGS 必须被忽略"。所以 gRPC 发私有 SETTINGS,对方不认识就跳过,认识的就协商。这 3 个分别协商:是否允许真二进制头(`-bin` 头直接发原始字节,不走 base64)、首选加密帧大小、是否允许 SECURITY 帧。

> **钉死这件事**:SETTINGS 是 HTTP/2 的连接配置文件,协商 6 项 RFC 标准 + 3 项 gRPC 私有参数。**INITIAL_WINDOW_SIZE 的 RFC 9113 默认值就是 65535,gRPC 的初始默认值也是 65535,一致**(老博客说"gRPC 调大"是把 BDP 运行时调整混进了初始值)。gRPC 强制关 ENABLE_PUSH,扩展 3 个私有 SETTINGS——这是"HTTP/2 可扩展性"在 RPC 场景的真实落地。

#### SETTINGS 的收发:settings_manager 的状态机

新版 settings 的收发由 [`http2_settings_manager.cc`](../grpc/src/core/ext/transport/chttp2/transport/http2_settings_manager.cc) 的 `Http2SettingsManager` 管。它持有 4 份快照:

- `local_`:本地改了、还没发的设置。
- `sent_`:已经发给对方、还没收到 ACK 的设置。
- `acked_`:对方 ACK 过的、可以执行的设置。
- `peer_`:对方告诉我的设置。

状态机 `UpdateState{kFirst, kSending, kIdle}`(`http2_settings_manager.h` 行 116-121)管发送节奏:

```cpp
// http2_settings_manager.cc(简化)
absl::optional<Http2SettingsFrame> MaybeSendUpdate() {
  if (state_ == kIdle && local_ != sent_) {
    Http2SettingsFrame f;
    local_.Diff(sent_, [&](uint16_t id, uint32_t val) {  // 只发变化的项
      f.settings.emplace_back(id, val);
    });
    sent_ = local_;
    state_ = kSending;
    return f;
  }
  return nullopt;   // kSending 时等 ACK,不发新帧
}

void AckLastSend() {
  state_ = kSending -> kIdle;
  acked_ = sent_;
}
```

注意 `local_.Diff(sent_, ...)`——**只发变化的设置项**,不重发整张表。这是 SETTINGS 的协议优化(RFC 9113 §6.5 允许):第一次发完整的,后续只发 diff。

经典版 settings 的解析在 [`frame_settings.cc`](../grpc/src/core/ext/transport/chttp2/transport/frame_settings.cc),用一套 `GRPC_CHTTP2_SPS_*` 状态机逐字节读 `(2 字节 id, 4 字节 value)` 对,每读一对就 `Apply` 一下,读完整个 SETTINGS 帧后入队一个空的 SETTINGS ACK(Flags 的 ACK bit = 1,payload 空)。

### 6.2 PING:探活与 RTT 测量

PING 帧用来"问对方在不在"和"测一来回要多久"。**PING 的 stream_id 必须 = 0**(连接级),payload 固定 **8 字节 opaque data**(对得上 RFC 9113 §6.7)。

发送方在 PING 里塞一个 8 字节的随机值,接收方**必须立刻回一个 PING ACK**(ACK bit = 1,opaque 原样回填)。发送方靠 opaque 配对"哪个 ACK 对应哪次 PING",从而测 RTT。`frame_ping.cc` 的 `grpc_chttp2_ping_create`([`frame_ping.cc`](../grpc/src/core/ext/transport/chttp2/transport/frame_ping.cc#L38-L61))序列化 PING 帧:

```cpp
// 简化示意,非源码原文
uint8_t* hdr = output.AddTiny(9);           // 9 字节帧头
// length=8, type=6(PING), flags=ack?0x1:0, stream_id=0
put_9byte_header(hdr, 8, GRPC_CHTTP2_FRAME_PING, ack ? 0x1 : 0, 0);
uint8_t* data = output.AddTiny(8);          // 8 字节 opaque
for (int i = 0; i < 8; i++) {
    data[i] = static_cast<uint8_t>(opaque >> (56 - 8 * i));   // 大端
}
```

PING 的作用有两个:

1. **keepalive 探活**(P5-17 拆透):长时间没数据的连接,TCP 可能"假活"(中间负载均衡器超时关了,但 TCP 不知道)。客户端定期发 PING,服务端回 ACK,确认连接还通。
2. **BDP 估计的 RTT 测量**(P2-09 拆透):gRPC 用 PING 测一个 RTT,乘以带宽估算 BDP,从而动态调流控 window。

但 PING 也是个**潜在的攻击面**——恶意客户端可以狂发 PING 把服务端 CPU 打爆。所以 gRPC 有 `ping_rate_policy` 和 `ping_abuse_policy` 限速(P2-09 拆透)。

#### Promise 版的 PingManager

新版的 PING 收发由 [`ping_promise.cc`](../grpc/src/core/ext/transport/chttp2/transport/ping_promise.cc) 的 `PingManager` 管。它把 `Chttp2PingCallbacks`(配对 ACK 的共享原语)、`Chttp2PingRatePolicy`(出站 PING 限速)、`Chttp2PingAbusePolicy`(入站 PING 防滥用)组合成一个 promise-based 系统:

```cpp
// ping_promise.cc:MaybeGetSerializedPingFrames(简化)
absl::optional<std::vector<Http2PingFrame>> MaybeGetSerializedPingFrames(...) {
  std::vector<Http2PingFrame> frames;
  // 1. 先 flush 待发的 PING ACK(urgent,必须立刻发)
  for (uint64_t opaque : pending_ping_acks_) {
    frames.push_back(GetHttp2PingFrame(/*ack=*/true, opaque));
  }
  pending_ping_acks_.clear();
  // 2. 再判断要不要发新 PING
  if (NeedToPing(...)) {
    uint64_t opaque = ping_callbacks_.StartPing(...);
    frames.push_back(GetHttp2PingFrame(/*ack=*/false, opaque));
  }
  return frames;
}
```

注意"先 flush ACK,再发新 PING"的顺序——这是协议礼仪,ACK 不能拖延。

### 6.3 GOAWAY:优雅关闭整条连接

GOAWAY 是 HTTP/2 的"**整条连接要关了**"信号。**stream_id 必须 = 0**。payload 是 `(last_stream_id: 4 字节, error_code: 4 字节, debug_data: 变长)`(对得上 RFC 9113 §6.8)。

发送方发 GOAWAY 时,`last_stream_id` 表示"**我这边不会再接受 stream id > last_stream_id 的新流**"。已经接受过的流(< = last_stream_id)可以继续处理完。这是一种**优雅关闭**——告诉对方"老流跑完,新流别发了"。

`frame_goaway.cc` 的解析逻辑(行 56-155)用一个状态机逐字节读 4 字节 `last_stream_id`、4 字节 `error_code`、剩下的当 debug data。收到 GOAWAY 后,`grpc_chttp2_add_incoming_goaway`(在 `chttp2_transport.cc` 行 1374-1433)做几件事:

1. 把这条连接标记为"goaway 收到",后续 `InitStream` 创建的新流直接失败。
2. **取消所有 stream id > last_stream_id 的流**(它们对端不会处理了)。
3. 构造一个 `UNAVAILABLE` 错误传给上层。
4. 特殊情况:如果 `error_code == ENHANCE_YOUR_CALM` 且 debug data 含 `"too_many_pings"`,触发 keepalive 退避(这是 gRPC 对"客户端 ping 太频繁"的服务端反馈,对得上 RFC 9113 的 ENHANCE_YOUR_CALM 错误码)。

gRPC 还实现了**两段式优雅关闭**(`GracefulGoaway` 在 `chttp2_transport.cc` 行 2128+):先发一个 `last_stream_id = 2^31-1` 的 GOAWAY(含义是"暂时不拒新流,但我要关了"),等一段让在飞流跑完,再发真正的 `last_stream_id = 当前最大 stream id` 的 GOAWAY,最后关连接。这避免了"突然关连接导致在飞流全失败"。

Promise 版由 [`goaway.cc`](../grpc/src/core/ext/transport/chttp2/transport/goaway.cc) 的 `GoawayManager` 管,用一个状态机 `kIdle → kInitialGracefulGoawayScheduled → kFinalGracefulGoawayScheduled → kDone` 编排两段式关闭。

### 6.4 RST_STREAM:异常终止单条流

RST_STREAM 是"**这条流我不要了,立刻关**"。**stream_id 必须 ≠ 0**(流级),payload 固定 **4 字节 error_code**(对得上 RFC 9113 §6.4)。

RST_STREAM 和 GOAWAY 的区别:**GOAWAY 关整条连接,RST_STREAM 只关一条流**。客户端取消一次调用(`call->Cancel()`)就发一个 RST_STREAM 给那条流的 stream id,**同连接的其他调用完全不受影响**——这就是 P2-05 讲的"精准取消"的物理实现。

`frame_rst_stream.cc` 的解析(行 101-156)读 4 字节 error code,然后调 `grpc_chttp2_mark_stream_closed(t, s, true, true, error)`(行 152),把这条流标记成"双向关闭",触发上层的回调(通常是 cancel completion)。

服务端收到 RST_STREAM 还有个有趣的行为:**以一定概率(`ping_on_rst_stream_percent`)主动请求一次 keepalive ping**——因为客户端取消可能是死代码或超时的征兆,服务端趁机探一下连接健康。

### 6.5 WINDOW_UPDATE:给流控加信用

WINDOW_UPDATE 是 HTTP/2 流控的核心——**接收方处理完数据后,用它告诉发送方"我又能接受 N 字节了"**。**stream_id = 0 是连接级 window,stream_id ≠ 0 是流级 window**(对得上 RFC 9113 §6.9)。payload 固定 **4 字节 increment**(增量,不是绝对值)。

`frame_window_update.cc` 的解析(行 76-146)读 4 字节 increment,**掩掉最高位**(`& 0x7fffffff`,行 98——最高位是 Reserved,必须忽略,对得上 RFC 9113),拒绝 increment=0(行 99,这是协议错误),然后分流:

- `stream_id != 0`(流级):`StreamFlowControl::OutgoingUpdateContext::RecvUpdate(increment)`(行 116-118),给这条流的发送 window 加 increment。如果这条流之前被流控卡住(`STALLED_BY_STREAM`),恢复到 WRITABLE。
- `stream_id == 0`(连接级):`TransportFlowControl::OutgoingUpdateContext::RecvUpdate(increment)`(行 127-137),给整条连接的发送 window 加 increment。如果连接级流控从 0 变正(`kUnstalled`),所有被 `STALLED_BY_TRANSPORT` 的流都恢复。

WINDOW_UPDATE 是 P2-09 流控章的主角,这里只点它在 framing 层的形态——4 字节 increment、连接级 vs 流级、最高位掩码、拒绝 0。它的"信用模型"在 P2-09 拆透。

### 6.6 SECURITY(Type=200):gRPC 的协议扩展实例

回看 P2-05 讲过的 `FrameType` 枚举,有一条 `kCustomSecurity = 200`。这是 gRPC **自己扩展的 HTTP/2 帧类型**,不在 RFC 9113 里。HTTP/2 规定:**跳过未知 Type 的帧**(对得上 RFC 9113 §5.5.1),所以 gRPC 加自己的帧,对方不认识就跳过。但 gRPC 的两端都认识它(通过 SETTINGS 65029 协商),用来在 HTTP/2 上**前置交换安全握手数据**(ALTS 等),避免 TLS 握手开销。

`frame_security.cc` 的解析器([`frame_security.cc`](../grpc/src/core/ext/transport/chttp2/transport/frame_security.cc#L55-L67))校验大小上限:`kMaxSecurityFrameSize = 16 * 1024`(16 KB,定义在 `frame.h` 行 447)。超过就报 `SecurityFrameTooLarge` 错误。这个上限是个安全措施——防止恶意对端发超大 SECURITY 帧打爆内存。

> **钉死这件事**:六种控制帧各司其职——**SETTINGS 协商参数(连接级)、PING 探活与测 RTT(连接级)、GOAWAY 优雅关连接(连接级)、RST_STREAM 异常关单流(流级)、WINDOW_UPDATE 调流控(连接级或流级)、SECURITY(Type=200)协议扩展**。它们和 DATA 帧一起,在一条 HTTP/2 连接上交织过线,撑起 gRPC 的全部传输语义。下一章 P2-09 会专门拆 WINDOW_UPDATE 背后的双层流控。

---

## 七、grpc-status 怎么随 trailer 回来

最后讲一个 gRPC framing 的关键细节:**一次调用的 `grpc-status` 状态码是怎么从服务端回到客户端的**。

### gRPC 把状态码塞进 trailer

gRPC 规定:服务端处理完一个调用,把状态码塞进两个特殊的 trailer:

- `grpc-status`(整数,0 = OK,其他 = 各种错误码):**走 trailer**,作为 HEADERS 帧的尾部块发送。
- `grpc-message`(可选,人类可读的错误描述):同样走 trailer。

对 unary 调用,服务端发回的响应长这样:

```
   服务端 → 客户端,一条流(stream id = 1)
   ┌──────────────────────────────────────────────────────────┐
   │ HEADERS 帧(:status=200, content-type=application/grpc) │  ← 初始元数据
   │ DATA 帧(5 字节 gRPC 头 + User protobuf 字节)          │  ← 响应消息
   │ HEADERS 帧(grpc-status=0, END_STREAM)                  │  ← trailer
   └──────────────────────────────────────────────────────────┘
```

第三个 HEADERS 帧(带 END_STREAM)**就是 trailer**——它是 HEADERS 帧,但语义上是"尾部块",携带 `grpc-status`、`grpc-message` 这些 gRPC 私有头部。HTTP/2 没有专门的 TRAILER 帧类型,trailer 就是"流结束前的最后一个 HEADERS 帧"(带 END_STREAM flag)。

### 为什么用 trailer 而不是 header

> **不这样会怎样**:为什么状态码不放响应的初始 HEADERS(第一个 HEADERS 帧)?因为 gRPC 的 streaming 调用里,**状态码只有在整个流结束才能确定**——比如 server-streaming,服务端发到第 100 条消息时网络断了,这时状态码是 `UNAVAILABLE`,但第一个 HEADERS 帧早就发出去了(里面只有 `:status=200`)。**状态码必须等流结束才知道,所以必须放在流末尾的 trailer**。

这是 gRPC 复用 HTTP 语义的一个漂亮设计:HTTP/2 的"trailer"概念(尾部头部块)恰好契合 RPC 的"调用结束才知道最终状态"的语义。gRPC 借力 HTTP/2 的 trailer,不需要再造一个"状态帧"。

### grpc-status 的 HPACK 编码

`grpc-status` 在 HPACK 编码时是个普通的字面量头部(key="grpc-status", value="0"),走 HPACK 的字面量表示(P2-07 讲过)。因为 `grpc-status` 不在静态表,且值多变(0、1、2、... 16 种状态码),通常不进动态表,走 `without indexing`(case 0,首字节 `0000xxxx`)。`grpc-message` 同理。

如果一个调用出错,服务端可能只在 trailer 里塞 `grpc-status: <非0>` 和 `grpc-message: <错误描述>`,**没有 DATA 帧**——客户端收到 trailer 就知道"这次调用失败了,状态码 X"。

> **钉死这件事**:gRPC 的状态码走 HTTP/2 的 trailer(流末尾的 HEADERS 帧,带 END_STREAM)。这是 gRPC 复用 HTTP 语义的漂亮一笔——HTTP/2 的 trailer 概念恰好契合 RPC 的"流结束才知道最终状态"。grpc-status / grpc-message 是普通 HPACK 字面量,通常不入动态表。

---

## 八、技巧精解

本章挑两个最硬核的技巧单独拆透。

### 技巧一:5 字节 Length-Prefixed-Message 为什么定长而非 varint——协议解析的"零分支"哲学

第一个技巧在第二节已经展开过,这里把它拔高到"协议设计哲学"的层面再钉死一次。

gRPC 的 5 字节头(1 字节压缩位 + 4 字节大端长度)和 protobuf 自己用的 varint(P1-03)是**两种对立的设计哲学**:

- **varint**:为了"省字节",宁可让解析方"逐字节读 + 测续位"。适合"单次序列化/反序列化,字段数有限"的场景(protobuf message 内部)。
- **定长大端**:为了"解析零分支",宁可多花几字节。适合"被走无数次、每次都要解析"的场景(每条 gRPC 消息都要解 framing)。

gRPC 选定长大的深层原因:**framing 这条路径在百万 QPS 下被走无数次,每次解析的 CPU 开销比省 1~3 字节带宽值钱得多**。看 `Read4b` 的实现:

```cpp
uint32_t Read4b(const uint8_t* input) {
  return static_cast<uint32_t>(input[0]) << 24 |
         static_cast<uint32_t>(input[1]) << 16 |
         static_cast<uint32_t>(input[2]) << 8 |
         static_cast<uint32_t>(input[3]);
}
```

4 次内存读 + 3 次移位 + 3 次 OR,**零循环、零分支**。现代 CPU 的流水线和分支预测器最喜欢这种"固定模式"的代码——它可以完全流水线化,一个时钟周期就能完成。对比 varint 的"while (byte & 0x80) { 读下一字节; }",每次解析都有不可预测的分支(消息长度跨阈值时分支预测失败),CPU 流水线被打断。

> **不这样会怎样**:如果用 varint,百万 QPS 下 gRPC 消息 framing 的解析会引入大量分支预测失败,CPU 流水线频繁打断,性能损失可观。定长大端让 framing 解析成为"零分支热路径",这是 gRPC 把"协议层性能压榨到极致"的体现。**协议设计的取舍不只是"省字节",更是"省 CPU"**——这是教科书很少讲但工程上决定性的维度。

### 技巧二:逐字节状态机 + `goto fallthrough`——流式协议解析的经典手法

第二个技巧是经典版 `parsing.cc` 的实现风格。看 `grpc_chttp2_perform_read` 的整体形态(`parsing.cc` 行 215-422),它是一个**巨型 switch**,每个状态对应一个 case,处理完一个字节后**用 `goto` 跳到下一个状态**(或 fallthrough 到下一个 case)。这不是结构化编程的反模式,而是**流式协议解析的性能优化**。

为什么用 `goto`?因为"处理完一个字节,下一个状态可能取决于当前字节的内容"。比如读帧头第 3 字节(帧类型)时,如果当前帧类型是 DATA,下一阶段要切到 DATA parser;如果是 SETTINGS,切到 SETTINGS parser。这种"根据数据内容动态决定下一状态"的逻辑,用 `goto` 直接跳过去,**比用一个"state 变量 + while 循环 + switch on state"的结构快**——后者每次循环都要重新 switch,而 `goto` 是直接的指令跳转。

```cpp
// grpc_chttp2_perform_read 的形态(简化,展示 goto fallthrough)
switch (t->deframe_state) {
  case GRPC_DTS_FH_0:
    // 处理帧头第 0 字节
    t->incoming_frame_size = static_cast<uint32_t>(c) << 16;
    t->deframe_state = GRPC_DTS_FH_1;
    break;   // 等下一字节
  case GRPC_DTS_FH_1:
    t->incoming_frame_size |= static_cast<uint32_t>(c) << 8;
    t->deframe_state = GRPC_DTS_FH_2;
    break;
  ...
  case GRPC_DTS_FH_8:
    // 帧头读完,初始化 parser
    init_frame_parser(t);
    t->deframe_state = GRPC_DTS_FRAME;
    goto dts_frame;   // ★ 直接跳到帧体处理,不等下一字节
  case GRPC_DTS_FRAME:
   dts_frame:
    // 处理帧体字节
    ...
}
```

注意 `goto dts_frame`——读完帧头最后一字节后,**不浪费一次循环**,直接跳到帧体处理。这种"读完一个阶段立刻处理下一阶段"的 fallthrough,让一次 `read` 收到的多个字节能被连续处理,**减少循环次数**。

这种风格的代价是**代码可读性差**——巨型 switch + goto 跳转,新人很难读。但它的性能优势在高 QPS 下是实打实的。新版 Promise transport 的 `ParseFramePayload` 放弃了这种风格,改成"一次解析完整帧"(因为它收到的已经是 SliceBuffer,不是逐字节),可读性大幅提升,但牺牲了经典版"任意粒度字节流"的灵活性——新版需要一个前置的"字节累积层"先凑够整帧。

> **不这样会怎样**:如果用"while 循环 + state 变量 + switch on state"的标准结构化写法,每次状态转换都要重新进 switch、重新匹配 case,在百万 QPS 下是多可观的 CPU 开销。`goto fallthrough` 让状态转换几乎是零成本(直接指令跳转),这是 chttp2 在 framing 解析性能上压榨到极致的体现。**代价是可读性**——所以新版用更可读的"整帧解析"换了这种灵活性,这是个工程取舍。

> **钉死这件事**:本章的两个技巧——**5 字节定长大端头的零分支哲学**(省 CPU 重于省字节)、**逐字节状态机 + goto fallthrough 的流式解析**(性能极致但可读性差)——都是 chttp2 把协议层性能压榨到极致的体现。它们让 gRPC 在 HTTP/2 上做到百万 QPS,代价是经典版代码极难读——这也是 Promise 重构的动机之一。

---

## 九、章末小结

### 回扣主线

本章拆的是 gRPC framing 与帧解析,属于二分法的**协议层**那一面(把方法调用的消息体和控制语义编码成 HTTP/2 上流动的字节)。从层次看,本章在 P2-07 HPACK(头部编码)之上——HPACK 解决"头部怎么压",本章解决"消息体怎么塞进 DATA 帧、各种控制帧怎么调度"。

回到全书主线:**把一次方法调用变成 HTTP/2 上的一条可控的流**。本章拆的是这条流的两层 framing——**HTTP/2 的 9 字节帧头(切字节流成帧)+ gRPC 的 5 字节 Length-Prefixed-Message(切 DATA payload 成消息)**——以及六种控制帧怎么调度这条流的节奏。下一章 P2-09 会钻进这条流的"**节奏控制**":WINDOW_UPDATE 背后的双层流控、BDP 自适应调窗、ping 限速,这是 gRPC 在 HTTP/2 流控之上加的招牌工程。

### 五个为什么

1. **为什么 gRPC 在 HTTP/2 DATA 帧之上还要叠一层 5 字节 framing?**——HTTP/2 DATA 帧只管"字节属于哪条流",gRPC 要在一条流里切分多条 protobuf 消息(client-streaming / bidi),必须有自己的"消息边界"约定。5 字节头(1 压缩位 + 4 大端长度)是最简洁的 length-prefix 方案。

2. **为什么 gRPC framing 用定长大端而非 varint(和 protobuf 不一致)?**——framing 这条路径在百万 QPS 下被走无数次,定长大端的"零分支解析"(4 次内存读 + 3 次移位,无循环无续位)比 varint 的省 1~3 字节更值钱。**协议设计的取舍不只是省字节,更是省 CPU**。

3. **为什么经典版 parsing.cc 用逐字节状态机 + goto fallthrough?**——HTTP/2 跑在 TCP 上,一次 read 可能返回任意字节(半帧、1.5 帧),解析方必须能处理任意粒度。逐字节状态机让字节到一点处理一点、状态完整保留;goto fallthrough 让状态转换几乎零成本(直接指令跳转)。代价是可读性差——这是 Promise 重构的动机之一。

4. **为什么经典版和新版两套解析器并存?**——gRPC 正在"换骨架",经典版 `parsing.cc` + `frame_*.cc`(C 风格散文件)与新版 `frame.cc` 的 `ParseFramePayload`(C++ variant 集中分发)平行存在。协议机制一致,差异在代码组织。新版在 experiment flag 后,生产默认经典版。

5. **为什么 grpc-status 走 trailer 而不是 header?**——gRPC 的 streaming 调用,状态码只有流结束才能确定(server-streaming 发到第 N 条消息时网络断了,状态码这时才变)。HTTP/2 的 trailer 概念恰好契合"流结束才知道最终状态",gRPC 借力 HTTP 语义,不造专门的"状态帧"。

### 想继续深入往哪钻

- **想读 gRPC over HTTP/2 协议规范**:官方文档 [doc/PROTOCOL-HTTP2.md](https://github.com/grpc/grpc/blob/master/doc/PROTOCOL-HTTP2.md) 是 gRPC framing 的权威定义,5 字节 Length-Prefixed-Message、grpc-status trailer、错误码都在这。
- **想看 5 字节头的源码**:新版 [`frame.cc`](../grpc/src/core/ext/transport/chttp2/transport/frame.cc) 的 `ExtractGrpcHeader` / `AppendGrpcHeaderToSliceBuffer` / `Read4b` / `Write4b`(行 119-166, 805-825);Promise 版 [`message_assembler.h`](../grpc/src/core/ext/transport/chttp2/transport/message_assembler.h) 的 `GrpcMessageAssembler` / `GrpcMessageDisassembler`。
- **想看读侧状态机**:经典版 [`parsing.cc`](../grpc/src/core/ext/transport/chttp2/transport/parsing.cc) 的 `grpc_chttp2_perform_read`(行 215-422)+ `init_frame_parser`(行 463-493);新版 [`frame.cc`](../grpc/src/core/ext/transport/chttp2/transport/frame.cc) 的 `ParseFramePayload`(行 726-756)。
- **想看各帧的语义处理**:`frame_data.cc` / `frame_settings.cc` / `frame_ping.cc` / `frame_goaway.cc` / `frame_rst_stream.cc` / `frame_window_update.cc` / `frame_security.cc`,每个文件一个 begin_frame + parse。
- **想看 SETTINGS 协商**:新版 [`http2_settings_manager.cc`](../grpc/src/core/ext/transport/chttp2/transport/http2_settings_manager.cc) 的 `Http2SettingsManager`(4 份快照 + 状态机);[`http2_settings.h`](../grpc/src/core/ext/transport/chttp2/transport/http2_settings.h) 的 6 标准 + 3 私有 SETTINGS 定义。
- **想看 PING 收发**:Promise 版 [`ping_promise.cc`](../grpc/src/core/ext/transport/chttp2/transport/ping_promise.cc) 的 `PingManager`;经典版 [`ping_callbacks.cc`](../grpc/src/core/ext/transport/chttp2/transport/ping_callbacks.cc) 的 `Chttp2PingCallbacks`。
- **想抓包看 framing**:用 Wireshark 解 HTTP/2,过滤一条 gRPC unary 调用,看 HEADERS(初始)+ DATA(5 字节 gRPC 头 + payload)+ HEADERS(trailer, grpc-status=0)三段。
- **想理解流控(本章 WINDOW_UPDATE 的深度)**:下一章 P2-09。

### 引出下一章

我们搞清楚了 gRPC 的两层 framing(HTTP/2 9 字节帧头 + gRPC 5 字节消息头)和六种控制帧各管什么事。但 WINDOW_UPDATE 背后的流控,我们只点到了"4 字节 increment、连接级 vs 流级"——**它真正的"双层 window 信用模型"、gRPC 在它之上叠的 BDP 自适应调窗、以及 ping_rate / ping_abuse 限速防滥用,是 gRPC 在 HTTP/2 流控之上的招牌工程**。下一章 P2-09,我们钻进 [`flow_control.cc`](../grpc/src/core/ext/transport/chttp2/transport/flow_control.cc) + [`bdp_estimator.cc`](../grpc/src/core/lib/transport/bdp_estimator.cc),拆透"不淹不饿"的流量控制。

> **下一章**:[P2-09 · 流量控制:不淹不饿](P2-09-流量控制-不淹不饿.md)
