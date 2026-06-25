# 第 2 篇 · 第 7 章 · HPACK:头部压缩的招牌

> **核心问题**:每次 gRPC 调用都要发一批 HTTP/2 头部——`:method: POST`、`:scheme: http`、`:path: /UserService/GetUser`、`:authority: user-svc:8080`、`content-type: application/grpc`、`te: trailers`、`grpc-encoding`、`grpc-timeout`、`user-agent`……这些头部**几乎每次调用都长一样**,如果每次都全文发送,海量调用下头部开销会吃掉可观带宽。HTTP/2 的 HPACK(RFC 7541)凭什么把这件事压到**几乎零字节**?gRPC 的 chttp2 又是怎么用源码实现这套三重压缩的——而且实现里藏着哪些教科书不会告诉你的工程优化?

> **读完本章你会明白**:
> 1. HPACK 的三重压缩(静态表 + 动态表 + Huffman)各回答了什么问题,为什么偏偏是这三重、缺一不可。
> 2. gRPC 的动态表在编码端和解码端**用了两套不对称的数据结构**——编码端只存条目大小(`tail_remote_index_` + 取模环形缓冲),解码端存完整 memento(`first_entry_` + `num_entries_` 的环形缓冲)。为什么不对称?
> 3. **gRPC 的静态表查找根本没有运行时 hash 查找**——靠 C++ 模板特化把每个头部类型对应的索引号在编译期写死(`EmitIndexed(3)` 一行代码搞定 `:method: POST`)。这是把"协议查表"在编译期消除的极致优化。
> 4. **gRPC 的 Huffman 解码不是教科书的状态机**,而是 codegen 工具生成的"多级查表 + bit-refill"解码器(主循环 30 行,但配套表数据 4371 行)——为什么这么干、妙在哪。
> 5. 敏感头(authorization token 等)为什么不进动态表,对得上 RFC 7541 §7.2.3 的 never-indexed——以及 gRPC 在这件事上的一个反直觉选择。

> **如果一读觉得太难**:先只记住三件事——① **静态表**:61 项最常用头,发 1 字节索引号代表整条头;② **动态表**:同连接学到的头存进表,下次只发索引号 → 同连接重复调用头部几乎零字节;③ **Huffman**:实在要发的文本用变长编码再压一道。这三件叠起来,就是 HPACK 把头部压到几乎零字节的全部魔法。细节(环形缓冲怎么实现、Huffman 状态机怎么多级查表)是给"想看穿实现"的读者准备的,逃不出去也照样能用 gRPC。

---

## 〇、一句话点破

> **HPACK 把每次调用重复的头部压到几乎零字节,靠的是三重压缩:静态表(发索引号代替整条头)、动态表(同连接学到的头下次也只发索引号)、Huffman(实在要发的文本用变长编码再压)。其中动态表是 RPC 场景的杀手锏——同连接重复调用,头部几乎零字节。这套机制的精妙不在三重压缩本身(那是 RFC 7541 规定的),而在 gRPC 的实现:编码端只存条目大小、静态表查找靠编译期模板特化、Huffman 解码靠 codegen 多级查表。**

这是结论,不是理由。本章倒过来拆:先讲三重压缩各解决什么、缺一不可,再钻进 chttp2 源码看每重压缩的实现——并且顺手纠正几个被博客反复抄错的事实(比如"gRPC 的动态表是双指针环形缓冲"——错,编码端根本不存内容)。

---

## 一、为什么 HTTP/1.1 的头部开销是个真问题

讲 HPACK 之前,先让读者真的"感到痛"。HTTP/1.1 时代的请求头长这样:

```
POST /UserService/GetUser HTTP/1.1
Host: user-svc:8080
Content-Type: application/grpc
User-Agent: grpc-python/1.62.0
te: trailers
grpc-timeout: 100m
grpc-encoding: identity
authorization: Bearer eyJhbGciOi...

(空行)
(请求体)
```

这是 ASCII 文本,每行以 `\r\n` 结尾,字段名 + 冒号 + 空格 + 值。一个典型的 gRPC 调用头部加起来 300~500 字节,而且**几乎每次调用都长一样**(只有 authorization 的 token 偶尔变)。如果一秒发一万次调用,光头部就是每秒 3~5 MB 的纯文本开销——而这些字节**几乎不带任何信息**(都是重复的)。

> **不这样会怎样**:HTTP/1.1 没有头部压缩,每次请求都全文发。在浏览器场景这已经是个问题(每个静态资源请求都带几 KB 的 cookie),在 RPC 场景更是灾难——一秒上万次调用,头部开销吃掉可观带宽,而且这些带宽**全是冗余**。SPDY(Google 2009 年的 HTTP/2 前身)就是为了解决这个问题发明了头部压缩,后来演化成 HPACK(RFC 7541)。

HPACK 的设计目标很明确:**让"同连接重复头部"这件事的开销趋近于零**。它用三重压缩达成这个目标,每一重各戳一个痛点。

---

## 二、第一重压缩:静态表——把"协议规定必须支持的头"写死成 1 字节索引

### 它解决什么问题

HTTP 头部里有一批"几乎所有人都会发、而且值很固定"的头:`:method: GET`、`:method: POST`、`:path: /`、`:scheme: http`、`:scheme: https`、`:status: 200`、`content-type`、`accept-encoding`......这些头的"name + value"组合是有限且高频的。如果每次都全文发,就是每次重复发几十字节。

### HPACK 的解法:静态表

RFC 7541 Appendix A 预定义了 **61 项静态表**,把最常用的"name + value"组合编号 1~61。发送方要发 `:method: POST`,**不发文本,只发索引号 3**——对方按 3 查静态表,就知道是 `:method: POST`。索引号用 varint 编码,**最常见的情况只占 1 字节**。

静态表前 10 项(RFC 7541 Appendix A,gRPC 实现完全对齐):

| 索引 | name | value |
|---|---|---|
| 1 | `:authority` | (空) |
| 2 | `:method` | `GET` |
| 3 | `:method` | `POST` |
| 4 | `:path` | `/` |
| 5 | `:path` | `/index.html` |
| 6 | `:scheme` | `http` |
| 7 | `:scheme` | `https` |
| 8 | `:status` | `200` |
| 9 | `:status` | `204` |
| 10 | `:status` | `206` |

在 gRPC 的 chttp2 里,这 61 项静态表的真实数据定义在 [`hpack_parser_table.cc`](../grpc/src/core/ext/transport/chttp2/transport/hpack_parser_table.cc#L211-L221),是个 `StaticTableEntry` 数组:

```cpp
const StaticTableEntry kStaticTable[hpack_constants::kLastStaticEntry] = {
    {":authority", ""},
    {":method", "GET"},
    {":method", "POST"},
    {":path", "/"},
    {":path", "/index.html"},
    {":scheme", "http"},
    {":scheme", "https"},
    {":status", "200"},
    {":status", "204"},
    {":status", "206"},
    ...
};
```

注意几件事:

1. **静态表的项数 = 61**,这个数字定义在 [`hpack_constants.h`](../grpc/src/core/ext/transport/chttp2/transport/hpack_constants.h#L31):

   ```cpp
   // last index in the static table
   static constexpr uint32_t kLastStaticEntry = 61;
   ```

   这和 RFC 7541 Appendix A 完全对齐。所以任何"HPACK 静态表 60 项"的博客都是错的——是 61 项。

2. **静态表是双向共享的**:客户端和服务端都有同一份静态表(RFC 规定),所以发索引号双方都能解。动态表才是连接级学习来的(下一节)。

3. **gRPC 相关的几个关键索引**:`:path: /` 是索引 4(gRPC 的 path 是 `/UserService/GetUser`,**前缀 `/` 命中静态表 4 但完整 path 不命中**,所以 path 通常走动态表或字面量)、`:method: POST` 是索引 3(gRPC 全是 POST,**这个 100% 命中静态表**)、`content-type` 是索引 31(但值 `application/grpc` 不在静态表,要走字面量)。一个反直觉的事实:**`te: trailers` 不在静态表**——gRPC 每次发它都是字面量或动态表项。

> **钉死这件事**:静态表用 1 字节索引号代替几十字节的"name + value"文本,把"协议必须支持的高频头"压缩到几乎零开销。但它的覆盖面有限——只 61 项,且很多项只有 name 没有 value(如 `:authority`、`content-type`),具体值还得发。**真正把"同连接重复调用"压到零字节的,是下一重的动态表**。

### gRPC 的极致优化:静态表查找靠编译期模板特化

这里要讲一个**大多数博客都讲错、但 gRPC 实现里非常硬核的优化**。教科书讲 HPACK 编码,会说"发送方收到一个 header,先查静态表有没有匹配的,有就发索引号"。这种叙述暗示发送方有一个运行时的"查静态表"函数(可能用 hash 表加速)。

**但 gRPC 的实现根本没有运行时静态表查找**。打开 [`hpack_encoder.cc`](../grpc/src/core/ext/transport/chttp2/transport/hpack_encoder.cc#L481-L502) 看 `:method` 头怎么编码:

```cpp
void Compressor<HttpMethodMetadata, HttpMethodCompressor>::EncodeWith(
    HttpMethodMetadata, HttpMethodMetadata::ValueType method,
    Encoder* encoder) {
  switch (method) {
    case HttpMethodMetadata::ValueType::kPost:
      encoder->EmitIndexed(3);  // :method: POST
      break;
    case HttpMethodMetadata::ValueType::kGet:
      encoder->EmitIndexed(2);  // :method: GET
      break;
    case HttpMethodMetadata::ValueType::kPut:
      // Right now, we only emit PUT as a method for testing purposes, so it's
      // fine to not index it.
      encoder->EmitLitHdrWithNonBinaryStringKeyNotIdx(
          Slice::FromStaticString(":method"), Slice::FromStaticString("PUT"));
      break;
    ...
  }
}
```

注意 `EmitIndexed(3)`——这是一个**硬编码的常量**。gRPC 用 C++ 模板为每个头部类型(`HttpMethodMetadata`、`HttpStatusMetadata`、`HttpSchemeMetadata` 等)写了一个特化的 `Compressor`,在**编译期**就把"这个头部类型对应静态表的哪个索引"写死了。运行时没有 hash 查找,没有线性扫,只有一个 switch 跳转。

这种优化叫"**编译期协议查表消除**":既然静态表是 RFC 固定的 61 项,既然每个头部类型在 gRPC 里都是一个独立的 C++ 类型(`HttpMethodMetadata` 是个类型,不是字符串),那么"这个类型对应静态表索引几"就是**编译期可知的常量**,没必要留到运行时查。`:scheme: http` 对应 `EmitIndexed(6)`、`:scheme: https` 对应 `EmitIndexed(7)`、`:status: 200` 对应 `EmitIndexed(8)`......全部写死在各类型的 `Compressor::EncodeWith` 里。

> **不这样会怎样**:如果用运行时 hash 查静态表,每次编码一个头部都要算 hash、查桶、比较字符串——百万 QPS 下光这一项就是可观的 CPU 开销。gRPC 的模板特化把"查静态表"这件事在**编译期**完全消除,运行时只剩一个 switch + 一个常量参数的函数调用。这是 HPACK 编码性能的隐形冠军,**也是大多数博客讲 HPACK 时完全忽略的优化**。

### 解码端的静态表:单例 + O(1) 数组下标

解码端反过来,收到索引号要查回"name + value"。gRPC 用一个进程级单例 `StaticMementos` 持有 61 个预解析的 memento,定义在 [`hpack_parser_table.h`](../grpc/src/core/ext/transport/chttp2/transport/hpack_parser_table.h#L100-L103):

```cpp
struct StaticMementos {
  StaticMementos();
  Memento memento[hpack_constants::kLastStaticEntry];  // 61 项
};
```

单例构造在 [`hpack_parser_table.cc`](../grpc/src/core/ext/transport/chttp2/transport/hpack_parser_table.cc#L289-L293),启动时把 61 项字符串预解析成 `ParsedMetadata`:

```cpp
HPackTable::StaticMementos::StaticMementos() {
  for (uint32_t i = 0; i < hpack_constants::kLastStaticEntry; i++) {
    memento[i] = MakeMemento(i);
  }
}
```

收到索引号时,查表逻辑在 [`hpack_parser_table.h`](../grpc/src/core/ext/transport/chttp2/transport/hpack_parser_table.h#L68-L80),直接数组下标访问:

```cpp
const Memento* Lookup(uint32_t index) {
  // Static table comes first, just return an entry from it.
  if (index <= hpack_constants::kLastStaticEntry) {
    return &static_mementos_->memento[index - 1];     // O(1) 数组下标
  } else {
    return LookupDynamic(index);
  }
}
```

注意几个细节:

1. **`<=` 不是 `<`**:静态表索引是 1..61,所以 `index <= 61` 是静态表,`index >= 62` 是动态表。
2. **`memento[index - 1]`**:数组下标 0..60 对应 HPACK 索引 1..61,所以减 1。
3. **`NoDestruct` 单例**:`StaticMementos` 用 gRPC 自家的 `NoDestruct<>` 封装([`hpack_parser_table.h:171-174`](../grpc/src/core/ext/transport/chttp2/transport/hpack_parser_table.h#L171-L174)),保证只构造一次、进程退出不析构(避免析构顺序竞态)。

整个静态表查找是 **O(1) 数组下标**,没有 hash、没有比较。这是把"协议表查找"优化到极致——既在编码端编译期消除,又在解码端 O(1) 化。

---

## 三、第二重压缩:动态表——同连接学到的头,下次只发索引号

### 它解决什么问题

静态表只能覆盖协议预定义的 61 项,但 RPC 场景里,真正高频重复的是 **gRPC 特有的头**:`:path: /UserService/GetUser`(每次调同一个方法,这个 path 完全不变)、`content-type: application/grpc`(每次都一样)、`user-agent: grpc-python/1.62.0`(同进程每次一样)。这些头**静态表里没有**,但又**每次调用都重复**——这是 RPC 场景下头部冗余的真正大头。

### HPACK 的解法:动态表

HPACK 让每条连接维护一张**动态表**:发送方第一次发某个头(`:path: /UserService/GetUser`),把它**存进动态表**;下次同连接再发同一个头,**只发动态表的索引号**(从 62 开始,紧跟静态表 61 之后)。这样同连接的第二次起调用,这个头就只占 1 字节。

动态表的几条核心性质(对得上 RFC 7541 §2.3.2 / §4):

1. **连接级共享**:一张动态表在一条连接的"出"方向上被发送方填充、在"入"方向上被接收方填充(双向独立),双方各维护一份镜像。
2. **FIFO**:新条目从表的"前端"(索引号小的方向)插入,老条目从"后端"被挤出。
3. **有尺寸上限**:动态表的总字节数有上限,默认 4096 字节(由 SETTINGS_HEADER_TABLE_SIZE 协商,可改)。超限就挤老的。
4. **每个条目算 size = key.length + value.length + 32**:那个 +32 是 RFC 7541 §4.1 规定的"entry overhead",模拟条目在内存里的额外开销。

### 一个具体例子:同连接 100 次调用同一个 RPC

假设客户端在一条连接上,连续 100 次调 `UserService.GetUser`,头部都是同一批(`:path: /UserService/GetUser`、`content-type: application/grpc`、`te: trailers`、`authorization: Bearer xxx`、`grpc-timeout` 等)。HPACK 的压缩效果:

- **第 1 次调用**:`:method: POST` 命中静态表(发 1 字节索引 3);`:path: /UserService/GetUser` 是字面量 + 加入动态表(发完整 path 字符串 + 标记"存进动态表");`content-type: application/grpc` 字面量 + 入表;其他类似。
- **第 2~100 次调用**:`:method: POST` 还是静态表 1 字节;`:path: /UserService/GetUser` 这次**命中动态表**(发 1 字节动态表索引号);`content-type: application/grpc` 命中动态表;`authorization` 不进动态表(敏感头,后面讲)......**除了每次变化的(grpc-timeout、authorization),其他全部 1 字节搞定**。

这就是 HPACK 在 RPC 场景下的杀手锏:**同连接重复调用,头部几乎零字节**。一个原本 300~500 字节的头部块,稳定后压到几十字节甚至十几字节,压缩比 10~30 倍。

```
   同连接 100 次 GetUser 的头部开销(gRPC 典型)
   ┌────────────────────────────────────────────────┐
   │ 第 1 次:  ~400 字节(字面量 + 入动态表)        │
   │ 第 2 次:  ~30 字节 (大部分命中动态表)         │
   │ 第 3 次:  ~30 字节                            │  ← 稳定后几乎
   │ ...                                            │     零字节开销
   │ 第 100 次:~30 字节                            │
   └────────────────────────────────────────────────┘
```

> **钉死这件事**:静态表解决"协议预定义头"的压缩,动态表解决"同连接学到的高频头"的压缩。**动态表是 RPC 场景的杀手锏**——同客户端反复调同一个服务(这是 RPC 的典型负载模式),头部在第二次起就几乎零字节。这是 gRPC 在 HTTP/2 上做到高吞吐的关键之一。

### 动态表的尺寸协商

动态表不是越大越好——它占用内存,而且发送方填充时接收方也得有同样的镜像(否则索引号就对不上了)。所以动态表的 size 上限是**双方协商**的:

- 连接建立时,双方各自在 SETTINGS 帧里发 `SETTINGS_HEADER_TABLE_SIZE`(Wire ID = 1),告诉对方"我这边动态表最多能装多少字节"。
- 默认值 4096 字节(对得上 RFC 7541 §6.3,定义在 [`hpack_constants.h`](../grpc/src/core/ext/transport/chttp2/transport/hpack_constants.h#L28)):

  ```cpp
  // Initial table size as per the spec
  static constexpr uint32_t kInitialTableSize = 4096;
  ```

- 发送方编码时,**用对方声明的 size 作为自己动态表的上限**(否则发个对方装不下的索引号就乱了)。这个"对方声明的 size"通过 SETTINGS 协商传递(本书 P2-05 讲过 SETTINGS 机制)。
- 还可以在连接中途发"dynamic table size update"信号(HPACK 自己的语义,在 HEADERS 帧的 payload 里),临时缩小动态表。

那个 +32 的 entry overhead 定义在 [`hpack_constants.h`](../grpc/src/core/ext/transport/chttp2/transport/hpack_constants.h#L26):

```cpp
// Per entry overhead bytes as per the spec
static constexpr uint32_t kEntryOverhead = 32;
```

配套的两个换算函数(行 33-40):

```cpp
static constexpr uint32_t EntriesForBytes(uint32_t bytes) noexcept {
  return (bytes + kEntryOverhead - 1) / kEntryOverhead;       // 字节→最多条目数
}
static constexpr size_t SizeForEntry(size_t key_length,
                                     size_t value_length) noexcept {
  return key_length + value_length + kEntryOverhead;          // 单条目的 size
}
```

默认 4096 字节、每条目至少 32 字节,所以默认动态表最多 128 条目(`kInitialTableEntries = EntriesForBytes(4096) = 128`)。

> **不这样会怎样**:如果动态表没有 size 协商,发送方可能填充一个对方装不下的表,发的索引号对方查不到 → 协议错乱。+32 的 entry overhead 是 RFC 7541 §4.1 的硬性规定——它让"表的字节占用"反映真实内存开销(每个条目除了 key+value,还有指针、链表节点等),避免一方塞进 1000 个 1 字节小条目撑爆另一方的内存。

---

## 四、第三重压缩:Huffman 编码——实在要发的文本用变长编码再压

### 它解决什么问题

静态表和动态表能压掉"已知头部",但总有些头是**第一次发、且不会进动态表**(比如敏感的 authorization、或者太长不值得入表的头)。这些头的 key/value 是 ASCII 文本,直接发有冗余——ASCII 每个字符定长 8 bit,但文本里字符出现频率极不均匀(`e`、`t`、空格高频,`z`、`q`、控制字符低频)。

### HPACK 的解法:Huffman 编码

HPACK 给了一张 257 项的 Huffman 编码表(RFC 7541 Appendix B),把每个 ASCII 字符(0~255 + 一个 EOS padding 符号)映射到一个变长 bit 串:**高频字符短编码**(比如空格 `' '` 只要 6 bit)、**低频字符长编码**(比如 ASCII 0 是 13 bit)。要发的文本先转成 Huffman 比特流,再塞进 HPACK 字面量的 value 字段(前面加 1 bit 前缀标记"这是 Huffman 编码的")。

gRPC 的 Huffman 编码符号表定义在 [`huffsyms.cc`](../grpc/src/core/ext/transport/chttp2/transport/huffsyms.cc),共 257 项,每项是 `{bits, length}`(bits 是编码值,length 是位数)。前几项(对应 ASCII 0x00~0x04,低频控制字符)的码很长:

```cpp
// (huffsyms.cc:26-30 附近,简化示意)
{0x1ff8, 13},     // ASCII 0x00 → 13 bit
{0x7fffd8, 23},   // ASCII 0x01 → 23 bit
{0xfffffe2, 28},  // ASCII 0x02 → 28 bit
{0xfffffe3, 28},  // ASCII 0x03 → 28 bit
{0xfffffe4, 28},  // ASCII 0x04 → 28 bit
...
```

而高频字符(如空格 `' '` 在表的中后段)的码很短,只有 6 bit。这样平均下来,文本长度能压缩 20%~40%。

> **钉死这件事**:Huffman 是 HPACK 的第三重压缩,处理"静态表和动态表都没命中的文本"。它不是 RPC 场景的主力(RPC 的主力是动态表),但能再压一道。值得注意的是——**gRPC 的 HPACK 编码器并没有对普通字符串头部主动 Huffman 编码**,只对 `-bin` 后缀的二进制头做了 base64+Huffman(下面 §六会讲为什么)。这是 gRPC 的一个工程取舍。

### gRPC 的 Huffman 解码:codegen 多级查表

讲 Huffman 编码简单(查表),讲 Huffman **解码**才是难点。编码是从字符查 bit 串(直接查 `huffsyms` 表),解码反过来——给一段 bit 流,要识别出"这是哪个字符"。问题在于 Huffman 是变长编码,你不知道下一个字符是 5 bit 还是 30 bit,得边读边判断。

朴素解法是**逐 bit 走一棵 Huffman 树**(每个内部节点 2 个子节点,叶子是字符)。但 HPACK 的 Huffman 树很扁、码长 5~30 bit,逐 bit 走树意味着每个字符要 5~30 次比较——慢。

教科书优化是**一张大查表**:把所有可能的高 N bit 组合列成一张表,一次查 N bit 就知道"这是哪个字符、消耗了几 bit"。但 HPACK 的码长到 30 bit,2^30 项的表是 10 亿项——内存爆炸。

gRPC 用了一个**非常硬核的折中:codegen 工具生成的多级查表**。这套表由 `tools/codegen/core/gen_huffman_decompressor.cc` 自动生成,主逻辑在 [`decode_huff.h`](../grpc/src/core/ext/transport/chttp2/transport/decode_huff.h)(2330 行!),配套表数据在 [`decode_huff.cc`](../grpc/src/core/ext/transport/chttp2/transport/decode_huff.cc)(4371 行,全是数组)。

主循环 `HuffDecoder::Run()` 在 [`decode_huff.h`](../grpc/src/core/ext/transport/chttp2/transport/decode_huff.h#L1179-L1211):

```cpp
bool Run() {
  while (!done_) {
    if (!RefillTo14()) {                // 把 buffer 补到 ≥14 bit
      Done0();
      break;
    }
    const auto index = (buffer_ >> (buffer_len_-14)) & 0x3fff;  // 取高 14 位
    const auto op = GetOp1(index);      // 查第一级表(16384 项)
    const int consumed = op & 15;       // 低 4 位 = 消耗的 bit 数
    buffer_len_ -= consumed;
    const auto emit_ofs = op >> 6;      // 高位 = emit 表偏移
    switch ((op >> 4) & 3) {            // 中间 2 位 = 动作类型
      case 0: {                          // 双字节直发
        sink_(GetEmit1(index, emit_ofs + 0));
        sink_(GetEmit1(index, emit_ofs + 1));
        break;
      }
      case 1: {                          // 单字节直发
        sink_(GetEmit1(index, emit_ofs + 0));
        break;
      }
      case 2: {                          // 下钻到子状态机 DecodeStep0
        DecodeStep0();
        break;
      }
      case 3: {                          // 下钻到子状态机 DecodeStep1
        DecodeStep1();
        break;
      }
    }
  }
  return ok_;
}
```

这个主循环是 HPACK 解码器的灵魂,值得逐句拆:

1. **`RefillTo14()`** 在 [`decode_huff.h`](../grpc/src/core/ext/transport/chttp2/transport/decode_huff.h#L1214-L1240),按 buffer 当前剩余 bit 数分桶调用不同 refill 组合,确保至少有 14 bit 可读:

   ```cpp
   GPR_ATTRIBUTE_ALWAYS_INLINE_FUNCTION bool RefillTo14() {
     switch (buffer_len_) {
       case 0:           return Read2to8Bytes();
       case 1: case 2: case 3: case 4: case 5:
                         return Read2to7Bytes();
       case 6: case 7: case 8:
                         return Read1to7Bytes();
       case 9: case 10: case 11: case 12: case 13:
                         return Read1to6Bytes();
     }
     return true;
   }
   ```

2. **取高 14 位作索引**:`buffer_ >> (buffer_len_-14) & 0x3fff`,14 位有 16384 种组合。
3. **`GetOp1(index)` 查第一级表**:这个表有 16384 项,每项是一个紧凑编码的 `op`(uint):
   - **低 4 位 (`op & 15`)**:本次消耗多少 bit(1~15)。
   - **中间 2 位 (`(op >> 4) & 3`)**:动作类型——0=双字节直发,1=单字节直发,2=转 DecodeStep0,3=转 DecodeStep1。
   - **高位 (`op >> 6`)**:emit 表偏移(动作是 0/1 时,去 emit 表取要输出的字节)。
4. **switch 分发**:大部分情况(case 0/1)直接从 emit 表取字节输出,**一次循环解码 1~2 个字节**;少数情况(case 2/3)下钻到子状态机,处理那些"14 bit 还不够判断"的长码字。

子状态机 `DecodeStep0` 在 [`decode_huff.h`](../grpc/src/core/ext/transport/chttp2/transport/decode_huff.h#L1619-L1630):

```cpp
GPR_ATTRIBUTE_ALWAYS_INLINE_FUNCTION void DecodeStep0() {
  if (!RefillTo1()) {                  // 按需 refill 到 1 bit
    Done1();
    return;
  }
  const auto index = (buffer_ >> (buffer_len_ - 1)) & 0x1;   // 取 1 bit
  const auto op = GetOp11(index);      // 查第二级表(2 项!)
  const int consumed = op & 1;
  buffer_len_ -= consumed;
  const auto emit_ofs = op >> 1;
  sink_(GetEmit11(index, emit_ofs + 0));
}
```

注意它的"宽度递减"——第一级查 14 bit(16384 项表),第二级只查 1 bit(2 项表)。这是因为到了子状态机,已经知道剩下的码字属于某个小子集,只需要再读 1 bit 就能区分。这就是"多级查表"的精髓——**每级表的宽度按剩余不确定性精确选择**,避免一张大表的全展开。

整个解码器有 `DecodeStep0` 到 `DecodeStep14` 共十几个子状态机,配套 `table2_*` / `table3_*` / ... / `table10_*` 几十张表(全在 `decode_huff.cc` 的 4371 行里)。每张表都是 codegen 工具针对某个 bit 宽度生成的紧凑数组。

### 为什么用多级查表而非教科书状态机

> **不这样会怎样**:如果用单一大查表,2^30 项的表是 10 亿项,内存爆炸;如果用逐 bit 走 Huffman 树,每个字符 5~30 次比较,性能差 5~30 倍。gRPC 的多级查表把"高位先查一次(16384 项)再按需下钻",**表总大小可控(几千项)、性能接近单次查表**(大部分字符在第一级 14 bit 就解决了),这是 Huffman 解码在工程上的精品实现。

更妙的是,主循环和所有子状态机都用 `GPR_ATTRIBUTE_ALWAYS_INLINE_FUNCTION` 强制内联——编译器会把它们展开成连续的 jump table,分支预测器能很好预测,**性能接近手写的逐字符处理**。这是把通用算法压到极致性能的工程范例。

> **钉死这件事**:gRPC 的 Huffman 解码不是教科书的 NFA/DFA 状态机,是 codegen 工具生成的"多级查表 + bit-refill"。它把"2^30 项大查表内存爆炸"和"逐 bit 走树性能差"两个极端都避开,在中间找到了甜点——**表总大小可控、大部分字符一次查表搞定、长码字按需下钻**。这是 HPACK 实现里最硬核的一块,也是 gRPC 把协议层性能压榨到极致的活样本。

---

## 五、敏感头:为什么不进动态表——对得上 RFC 7541 §7.2.3

### 它解决什么问题

动态表会让"同连接的不同调用共享学到的头部"。这在大多数场景下是优化,但有一个**安全副作用**:`authorization: Bearer xxx` 这种携带凭据的头,如果进了动态表,**理论上可以让攻击者通过"探测哪个索引号有数据"关联跨请求**。比如攻击者控制客户端发一个带特定 token 的请求,token 进了动态表,后续请求即使不发 token,通过引用索引号也能关联——这让"跨请求的 token 隔离"失效。

RFC 7541 §7.2.3 为此规定了 **never-indexed literal**:某些头(认证凭据、cookie 等)发的时候用一种特殊标记(首字节 `0001xxxx`),**告诉接收方"这条头永远不要加入动态表"**。这样每个请求都得全文发这条头,但换取了"跨请求无法通过动态表索引关联"的安全性。

### gRPC 的实现:用 `add_to_table` 布尔透传

gRPC 的 HPACK 解码端在 [`hpack_parser.cc`](../grpc/src/core/ext/transport/chttp2/transport/hpack_parser.cc#L604-L697) 的 `ParseTop` 函数里按首字节高 4 位分流。这是 HPACK 协议的精髓——所有头部表示都通过首字节的高位区分:

```cpp
bool ParseTop() {
  GRPC_DCHECK(state_.parse_state == ParseState::kTop);
  auto cur = *input_->Next();
  input_->ClearFieldError();
  switch (cur >> 4) {
      // Literal header not indexed - First byte format: 0000xxxx
      // Literal header never indexed - First byte format: 0001xxxx
      // Where xxxx:
      //   0000  - literal key
      //   1111  - indexed key, varint encoded index
      //   other - indexed key, inline encoded index
    case 0:
    case 1:                                          // ← not-indexed / never-indexed
      switch (cur & 0xf) {
        case 0:  return StartParseLiteralKey(false);     // add_to_table=false
        case 0xf: return StartVarIdxKey(0xf, false);
        default:  return StartIdxKey(cur & 0xf, false);
      }
      // Update max table size. First byte format: 001xxxxx
    case 2:
      return FinishMaxTableSize(cur & 0x1f);
    case 3:
      if (cur == 0x3f) {
        return FinishMaxTableSize(input_->ParseVarint(0x1f));
      } else {
        return FinishMaxTableSize(cur & 0x1f);
      }
      // Literal header with incremental indexing.
      // First byte format: 01xxxxxx
    case 4:
      if (cur == 0x40) {
        return StartParseLiteralKey(true);
      }
      [[fallthrough]];
    case 5:
    case 6:
      return StartIdxKey(cur & 0x3f, true);             // ← add_to_table=true
    case 7:
      if (cur == 0x7f) {
        return StartVarIdxKey(0x3f, true);
      } else {
        return StartIdxKey(cur & 0x3f, true);
      }
      // Indexed Header Field Representation. First byte format: 1xxxxxxx
    case 8:
      if (cur == 0x80) {
        input_->SetErrorAndStopParsing(HpackParseResult::IllegalHpackOpCode());
        return false;
      }
      [[fallthrough]];
    case 9: case 10: case 11: case 12: case 13: case 14:
      return FinishIndexed(cur & 0x7f);                 // ← 直接查静态/动态表
    case 15:
      if (cur == 0xff) {
        return FinishIndexed(input_->ParseVarint(0x7f));
      } else {
        return FinishIndexed(cur & 0x7f);
      }
  }
  GPR_UNREACHABLE_CODE(abort());
}
```

把这五种 HPACK 头部表示对齐 RFC 7541 §7:

| 首字节高 4 位 | HPACK 表示 | RFC 7541 | `add_to_table` | 用途 |
|---|---|---|---|---|
| `0000` | Literal without indexing | §7.2.2 | false | 不入表(普通字面量) |
| `0001` | **Literal never indexed** | **§7.2.3** | **false** | **敏感头,跨请求不关联** |
| `0010/0011` | Dynamic table size update | §6.3 | (不是头部) | 缩小动态表 |
| `0100~0111` | Literal with incremental indexing | §7.2.1 | **true** | 入表(常用头) |
| `1000~1111` | Indexed header field | §7.1 | (直接索引) | 发索引号 |

注意 case 0 和 case 1 都传 `false`(不入表),但语义不同——case 0 是"普通字面量不入表"(可能因为太长、或者发送方觉得不划算),case 1 是"**敏感头,明确要求接收方也不得加入它自己的动态表**(如果接收方作为后续的发送方)"。case 1 的 RFC 强制力比 case 0 强:中间代理看到 case 1 必须保持 never-indexed,看到 case 0 可以自行决定。

### gRPC 的一个反直觉选择:编码端不主动发 never-indexed

这里有个**博客很少提到、但很真实的细节**:gRPC 的 HPACK **编码端不主动使用 never-indexed 表示**。打开 `hpack_encoder.cc` grep `0x10`(never-indexed 前缀)或 `EmitLitHdrWith*NeverIdx`,**找不到显式的"敏感头走 never-indexed"路径**。gRPC 的处理方式是:

- `-bin` 后缀的二进制头:走 **without indexing**(首字节 `0x00`,case 0),不入表。
- 普通头:要么命中静态/动态表(发索引),要么走 **incremental indexing**(case 4~7,入表)。
- 没有专门的"这条头是敏感的,必须 never-indexed"逻辑。

意思是 gRPC 的实现里,authorization 这种敏感头**默认会进动态表**(走 incremental indexing)。这在 RFC 7541 §7.2.3 的"建议"层面是个折中——RFC 说"should use never-indexed for sensitive headers",gRPC 没强制这么做。理由可能是:在 gRPC 的典型场景(内部服务间调用、已用 TLS 加密、token 在连接生命周期内本来就要复用),never-indexed 的安全收益不明显,而强制 never-indexed 会损失动态表对 authorization 的压缩收益。这是个**工程取舍而非协议违规**——RFC 7541 §7.2.3 本来就是"SHOULD"而非"MUST"。

> **钉死这件事**:HPACK 的 never-indexed 表示(`0001xxxx`,RFC 7541 §7.2.3)是为了防"跨请求通过动态表索引关联敏感头"。gRPC 解码端正确识别它(`add_to_table=false`),但编码端不主动使用——authorization 等敏感头默认会进动态表,这是 gRPC 在"安全建议"和"压缩收益"之间的工程取舍。读者在排查"我的 token 是否被跨请求关联"时要知道这个细节。

---

## 六、二进制头(`-bin` 后缀)与 base64+Huffman

HPACK 是为文本头设计的,但 gRPC 有一种特殊头——**二进制头**(key 以 `-bin` 结尾,如 `grpc-status-details-bin`),value 是任意二进制字节。HPACK 的字符串字段是 ASCII,直接塞二进制会破坏协议(控制字符、HPACK 的字节边界)。gRPC 的处理:

- **传统路径(`use_true_binary_metadata=false`)**:把二进制 value 先 **base64 编码**成 ASCII,再对 base64 串做 **Huffman 压缩**(因为 base64 的字符分布不均匀,Huffman 还能再压一道)。前面加 1 bit 前缀 `0x80` 标记"Huffman 编码过的"。
- **新路径(`use_true_binary_metadata=true`,通过 SETTINGS 扩展 65027 协商)**:直接发原始二进制(用首字节前缀 `0x08` 区分),省掉 base64 的 33% 膨胀。这是 gRPC 的协议扩展,需要双方都支持。

base64+Huffman 的实现在 [`bin_encoder.cc`](../grpc/src/core/ext/transport/chttp2/transport/bin_encoder.cc)(234 行)和 [`bin_decoder.cc`](../grpc/src/core/ext/transport/chttp2/transport/bin_decoder.cc)(240 行)。注意 `bin_encoder.cc` 自带一张 64 项的 Huffman 表(`huff_alphabet`),**只对 base64 的 64 个字符做 Huffman**——比通用 ASCII 的 257 项表小得多。这是因为 base64 字符集只有 64 个,专用表更紧凑、查表更快。

这部分是 HPACK 的辅助层,不是主线,本章不展开。读者只要知道:`-bin` 头走的是 base64+Huffman 或真二进制两条路之一,具体走哪条由 SETTINGS 协商决定。

---

## 七、技巧精解:动态表的环形缓冲——编码端和解码端两套不对称布局

本章挑两个最硬核的技巧单独拆透。第一个是动态表的环形缓冲实现——这是 HPACK 性能与正确性的关键,也是 gRPC 实现里最容易被博客讲错的地方。

### 教科书的讲法和 gRPC 的实际

教科书讲 HPACK 动态表,会说"它是个 FIFO 队列,新条目插入前端、老条目从后端挤出,用环形缓冲实现"。这个描述**对**但不**精确**——它暗示发送方和接收方用同一套数据结构。**实际打开 gRPC 的源码,会发现编码端和解码端用了两套完全不对称的环形缓冲**。这是 gRPC 实现里一个非常硬核的优化。

### 编码端:只存条目大小,不存内容

编码端动态表在 [`hpack_encoder_table.cc`](../grpc/src/core/ext/transport/chttp2/transport/hpack_encoder_table.cc)(全 91 行)。核心字段在 [`hpack_encoder_table.h`](../grpc/src/core/ext/transport/chttp2/transport/hpack_encoder_table.h#L69-L75):

```cpp
// one before the lowest usable table index
uint32_t tail_remote_index_ = 0;                  // 队尾的逻辑索引
uint32_t max_table_size_ = ...kInitialTableSize;  // 表容量上限
uint32_t table_elems_ = 0;                        // 当前条目数
uint32_t table_size_ = 0;                         // 当前已用字节数
std::vector<EntrySize> elem_size_;                // 环形缓冲本体(只存 size!)
```

注意 `elem_size_` 是 `std::vector<EntrySize>`,`EntrySize` 是 `uint16_t`(只存条目的字节数,**不存 key/value 内容**)。为什么?因为**编码端不需要知道条目的内容**——它只需要知道"这个索引号对应的条目还在不在表里、占多大空间"。具体内容编码端从应用层拿(每次编码时上层传入 header 的 key/value),不重复存。

环形缓冲的核心函数 `AllocateIndex` 在 [`hpack_encoder_table.cc`](../grpc/src/core/ext/transport/chttp2/transport/hpack_encoder_table.cc#L25-L51):

```cpp
uint32_t HPackEncoderTable::AllocateIndex(size_t element_size) {
  GRPC_DCHECK_GE(element_size, 32u);

  uint32_t new_index = tail_remote_index_ + table_elems_ + 1;
  GRPC_DCHECK_LE(element_size, MaxEntrySize());

  if (element_size > max_table_size_) {            // 比整表还大 → 清空
    while (table_size_ > 0) {
      EvictOne();
    }
    return 0;
  }

  // Reserve space for this element in the remote table: if this overflows
  // the current table, drop elements until it fits, matching the decompressor
  // algorithm.
  while (table_size_ + element_size > max_table_size_) {
    EvictOne();                                    // 驱逐老的直到塞得下
  }
  GRPC_CHECK(table_elems_ < elem_size_.size());
  elem_size_[new_index % elem_size_.size()] =      // ★ 环形写入(取模)
      static_cast<uint16_t>(element_size);
  table_size_ += element_size;
  table_elems_++;

  return new_index;
}
```

驱逐函数 `EvictOne` 在 [`hpack_encoder_table.cc`](../grpc/src/core/ext/transport/chttp2/transport/hpack_encoder_table.cc#L71-L79):

```cpp
void HPackEncoderTable::EvictOne() {
  tail_remote_index_++;                            // ★ 只推进逻辑队尾
  GRPC_CHECK_GT(tail_remote_index_, 0u);
  GRPC_CHECK_GT(table_elems_, 0u);
  auto removing_size = elem_size_[tail_remote_index_ % elem_size_.size()];
  GRPC_CHECK(table_size_ >= removing_size);
  table_size_ -= removing_size;
  table_elems_--;
}
```

注意编码端的环形缓冲实现:**没有 `first_` / `last_` 双指针,只有一个 `tail_remote_index_`(队尾逻辑索引)+ 对 vector 取模**。插入位置是 `(tail_remote_index_ + table_elems_ + 1) % elem_size_.size()`,驱逐只是 `tail_remote_index_++`——物理 vector 从不搬移,只是逻辑索引推进。这是"逻辑环形缓冲"的极致紧凑实现。

索引号换算公式在 [`hpack_encoder_table.h`](../grpc/src/core/ext/transport/chttp2/transport/hpack_encoder_table.h#L55-L58):

```cpp
return 1 + hpack_constants::kLastStaticEntry + tail_remote_index_
       + table_elems_ - index;
```

即:**线上动态表索引 = 62 + tail_remote_index_ + table_elems_ - 内部 index**(动态表索引从 62 开始,紧跟静态表 61 之后)。

### 解码端:存完整 memento 的环形缓冲

解码端反过来——它收到索引号后要把"name + value"还给上层,所以必须存完整内容。数据结构在 [`hpack_parser_table.h`](../grpc/src/core/ext/transport/chttp2/transport/hpack_parser_table.h#L105-L161),是一个嵌套类 `MementoRingBuffer`:

```cpp
uint32_t first_entry_ = 0;      // 队头逻辑索引
uint32_t num_entries_ = 0;      // 当前条目数
uint32_t max_entries_ = ...kInitialTableEntries;   // 容量上限(条目数)
std::vector<Memento> entries_;  // 环形缓冲本体(存完整 Memento!)
```

注意字段名和编码端**完全不同**——编码端是 `tail_remote_index_` + `table_elems_`,解码端是 `first_entry_` + `num_entries_`。这两套命名是 gRPC 实现里非常容易让读者困惑的点,**博客常把它们混为一谈**。

解码端收到"literal with incremental indexing"时,插入逻辑在 [`hpack_parser_table.cc`](../grpc/src/core/ext/transport/chttp2/transport/hpack_parser_table.cc#L158-L177):

```cpp
bool HPackTable::Add(Memento md) {
  if (current_table_bytes_ > max_bytes_) return false;

  // we can't add elements bigger than the max table size
  if (md.md.transport_size() > current_table_bytes_) {
    AddLargerThanCurrentTableSize();                // 比整表还大 → 清空
    return true;
  }

  // evict entries to ensure no overflow
  while (md.md.transport_size() >
         static_cast<size_t>(current_table_bytes_) - mem_used_) {
    EvictOne();                                     // 驱逐老的直到塞得下
  }

  // copy the finalized entry in
  mem_used_ += md.md.transport_size();
  entries_.Put(std::move(md));                      // ★ 环形缓冲 Put
  return true;
}
```

环形缓冲的 `Put` 和 `PopOne` 在 [`hpack_parser_table.cc`](../grpc/src/core/ext/transport/chttp2/transport/hpack_parser_table.cc#L43-L73):

```cpp
void HPackTable::MementoRingBuffer::Put(Memento m) {
  GRPC_CHECK_LT(num_entries_, max_entries_);
  if (entries_.size() < max_entries_) {
    ++num_entries_;
    return entries_.push_back(std::move(m));        // vector 未满 → push_back
  }
  size_t index = (first_entry_ + num_entries_) % max_entries_;   // 满后覆写
  if (timestamp_index_ == kNoTimestamp) {
    timestamp_index_ = index;
    timestamp_ = Timestamp::Now();
  }
  entries_[index] = std::move(m);
  ++num_entries_;
}

auto HPackTable::MementoRingBuffer::PopOne() -> Memento {
  GRPC_CHECK_GT(num_entries_, 0u);
  size_t index = first_entry_ % max_entries_;       // 队头取模定位
  if (index == timestamp_index_) {
    http2_stats_collector_->IncrementHttp2HpackEntryLifetime(
        (Timestamp::Now() - timestamp_).millis());  // 统计条目生命周期
    timestamp_index_ = kNoTimestamp;
  }
  ++first_entry_;                                   // 推进逻辑队头
  --num_entries_;
  auto& entry = entries_[index];
  if (!entry.parse_status.TestBit(Memento::kUsedBit)) {
    http2_stats_collector_->IncrementHttp2HpackMisses();   // 统计未命中
  }
  return std::move(entry);
}
```

解码端 `PopOne` 里有个细节——它顺便收集了**HPACK 命中/未命中统计**(`kUsedBit`、`IncrementHttp2HpackMisses`)和**条目生命周期统计**(`timestamp_index_`)。这些统计会上报到 channelz(P6-20 会讲),让运维能看到"这条连接的 HPACK 动态表命中率多少、平均条目活多久"。这是把可观测性嵌进协议实现细节的工程巧思。

### 两套布局的镜像性

虽然编码端和解码端用了**两套不同的数据结构**(只存 size vs 存完整 memento、`tail_remote_index_` vs `first_entry_`),但它们维护的是**同一张逻辑动态表**——发送方插入条目,接收方镜像插入;发送方驱逐老条目,接收方也镜像驱逐。两边的驱逐算法(`while 超限就 EvictOne`)必须**完全对齐**,否则索引号就对不上。这种"算法对称、数据结构不对称"是工程优化的典范——**两边各存自己需要的最小信息**,不浪费内存。

```
   动态表的镜像:编码端和解码端用不同数据结构维护同一张逻辑表
   ┌─────────────────────────────────────────────────────────────┐
   │ 编码端 HPackEncoderTable(只存 size)                        │
   │   tail_remote_index_  +  table_elems_                        │
   │   elem_size_: vector<uint16_t>     ← 每个 entry 只 2 字节    │
   │   作用:算"还能不能塞""索引号怎么发"                          │
   ├─────────────────────────────────────────────────────────────┤
   │           (双方算法对齐:while 超限 EvictOne)                │
   ├─────────────────────────────────────────────────────────────┤
   │ 解码端 HPackTable::MementoRingBuffer(存完整 memento)        │
   │   first_entry_  +  num_entries_                              │
   │   entries_: vector<Memento>        ← 每 entry 含 key+value   │
   │   作用:收到索引号查回完整 name+value 给上层                 │
   └─────────────────────────────────────────────────────────────┘
```

> **不这样会怎样**:如果两端用同一套数据结构(都存完整 memento),编码端会白白浪费内存存一份它根本不需要的 key/value 内容(它每次编码都从应用层拿到 key/value)。gRPC 的不对称实现让编码端只花 2 字节/条目(只存 size),解码端才存完整内容。这是 HPACK 实现里一个非常硬核的内存优化,**也是博客最常讲错的地方**(都套用"双指针环形缓冲"的笼统描述)。

---

## 八、技巧精解:动态表的索引复用——`StableValueCompressor` 的快慢路径

第二个技巧是动态表的索引复用策略。HPACK 的动态表压缩要兑现,关键在"**发送方知道这个 header 上次发过、且还在表里**"。gRPC 的实现用了一个叫 `StableValueCompressor` 的模板组件,在 [`hpack_encoder.h`](../grpc/src/core/ext/transport/chttp2/transport/hpack_encoder.h#L190-L221):

```cpp
template <typename MetadataTrait>
class Compressor<MetadataTrait, StableValueCompressor> {
 public:
  void EncodeWith(MetadataTrait, const typename MetadataTrait::ValueType& value,
                  Encoder* encoder) {
    auto& table = encoder->hpack_table();
    if (previously_sent_value_ == value &&                           // ★ 快路径
        table.ConvertibleToDynamicIndex(previously_sent_index_)) {
      encoder->EmitIndexed(table.DynamicIndex(previously_sent_index_));  // 发 1 字节索引
      return;
    }
    previously_sent_index_ = 0;
    auto key = MetadataTrait::key();
    const Slice& value_slice = MetadataValueAsSlice<MetadataTrait>(value);
    if (hpack_constants::SizeForEntry(key.size(), value_slice.size()) >
        HPackEncoderTable::MaxEntrySize()) {
      encoder->EmitLitHdrWithNonBinaryStringKeyNotIdx(             // 太长不入表
          Slice::FromStaticString(key), value_slice.Ref());
      return;
    }
    encoder->EncodeAlwaysIndexed(                                    // ★ 慢路径
        &previously_sent_index_, key, value_slice.Ref(),
        hpack_constants::SizeForEntry(key.size(), value_slice.size()));
    SaveCopyTo(value, previously_sent_value_);
  }

 private:
  typename MetadataTrait::ValueType previously_sent_value_{};       // 记住上次发的值
  uint32_t previously_sent_index_ = 0;                              // 上次的索引号
};
```

这个组件的逻辑是经典的"**快慢路径**":

- **快路径**(行 196-200):本次 value 和上次发的完全一样(`previously_sent_value_ == value`)且上次分配的索引还在动态表里(`ConvertibleToDynamicIndex`)→ **直接发 1 字节索引号**,完事。这是同连接重复调用能"几乎零字节"的物理实现。
- **失败清零**(行 201):只要快路径任一条件不满足,立即把 `previously_sent_index_ = 0`(0 是哨兵,下次快路径肯定走不到)。
- **太长检查**(行 204-209):如果这个 entry 比动态表最大条目还大,根本入不了表,走 not-indexed 字面量。
- **慢路径**(行 210-213):走 `EncodeAlwaysIndexed` 发字面量并分配新索引,最后把本次 value 存进 `previously_sent_value_` 供下次比对。

配套的 `EncodeAlwaysIndexed` 在 [`hpack_encoder.cc`](../grpc/src/core/ext/transport/chttp2/transport/hpack_encoder.cc#L504-L512):

```cpp
void Encoder::EncodeAlwaysIndexed(uint32_t* index, absl::string_view key,
                                  Slice value, size_t) {
  if (compressor_->table_.ConvertibleToDynamicIndex(*index)) {
    EmitIndexed(compressor_->table_.DynamicIndex(*index));          // 能复用就发索引
  } else {
    *index = EmitLitHdrWithNonBinaryStringKeyIncIdx(                // 否则发字面量+入表
        Slice::FromStaticString(key), std::move(value));
  }
}
```

这个设计的妙处在 `previously_sent_value_` 的存储——它**每个 MetadataTrait 类型一份**(因为 Compressor 是模板特化),用 `SaveCopyTo` 模板对 Slice 自动 `Ref()`(引用计数,零拷贝)、对平凡类型直接赋值。这让"记住上次发的值"几乎零开销。

> **不这样会怎样**:如果每次编码都查动态表"这个 key/value 在不在里面",就要线性扫整张表(O(N) 比较),百万 QPS 下直接拖垮 CPU。`StableValueCompressor` 的快路径是 O(1) 比较(一个 `==` + 一个索引有效性检查),让"同连接重复调用"在编码端**几乎零开销**。这是 HPACK 性能红利的另一面——不只是压缩比高,而且**压缩本身几乎不耗 CPU**。

> **钉死这件事**:HPACK 的两个核心技巧——**动态表环形缓冲用两套不对称布局省内存**(编码端只存 size)、**索引复用用快慢路径 O(1) 命中**(StableValueCompressor)——组合起来,让 HPACK 既压缩比高(同连接重复头部几乎零字节)又 CPU 开销低(编码端接近零成本)。这是 gRPC 在 HTTP/2 上做到高吞吐的招牌实现,也是协议层最硬核的一块。

---

## 九、章末小结

### 回扣主线

本章拆的是 HPACK——HTTP/2 头部压缩的招牌,属于二分法的**协议层招牌**那一面(把方法调用的头部编码成网络上几乎零字节的流)。HPACK 是 gRPC 在 HTTP/2 上做到高吞吐的关键,也是本书选 C++ core 作源码的根(grpc-go/grpc-java 复用语言库,HPACK 真身不在自己代码里)。

回到全书主线:**把一次方法调用变成 HTTP/2 上的一条可控的流**。本章拆的是这条流的"**头部编码**"——`:path`、`content-type`、`te: trailers` 这些每次调用重复的文本,怎么被 HPACK 三重压缩到几乎零字节。下一章 P2-08 会拆这条流的"**消息编码**"——protobuf 序列化后的字节怎么塞进 DATA 帧(gRPC 的 Length-Prefixed-Message framing),以及 parsing/writing 双状态机。

### 五个为什么

1. **为什么 HPACK 要三重压缩(静态表 + 动态表 + Huffman)?**——静态表覆盖"协议预定义的高频头"(发 1 字节索引),动态表覆盖"同连接学到的高频头"(同连接重复调用几乎零字节),Huffman 压缩"实在要发的文本"(变长编码再压一道)。三者各戳一个痛点,缺一不可。
2. **为什么 gRPC 的静态表查找没有运行时 hash?**——靠 C++ 模板特化,每个头部类型对应的索引号在编译期写死(`EmitIndexed(3)` 一行代码搞定 `:method: POST`)。这是把"协议查表"在编译期消除的极致优化。
3. **为什么 gRPC 的动态表编码端和解码端用了两套不对称数据结构?**——编码端只需要"索引号对应条目还在不在表里、占多大空间",只存条目大小(2 字节/条目);解码端收到索引号要查回完整 name+value,得存完整 memento。两边算法对齐(驱逐规则一致)、数据结构不对称,**各存自己需要的最小信息**。
4. **为什么 gRPC 的 Huffman 解码不是教科书状态机?**——HPACK 码长 5~30 bit,单一大查表会 2^30 项内存爆炸,逐 bit 走树性能差 5~30 倍。gRPC 用 codegen 多级查表(第一级 14 bit 16384 项,按需下钻到子状态机),表总大小可控、性能接近单次查表。
5. **为什么 gRPC 编码端不主动用 never-indexed?**——RFC 7541 §7.2.3 的 never-indexed 是"SHOULD"而非"MUST"。gRPC 在典型场景(内部服务间、已 TLS 加密、token 在连接生命周期内本来就要复用)下,选择牺牲 never-indexed 的安全建议换动态表对 authorization 的压缩收益。这是工程取舍,排查"token 跨请求关联"时要知道这个细节。

### 想继续深入往哪钻

- **想读协议标准**:RFC 7541(HPACK,2015)是本章的全部依据。注意 HPACK 的 RFC 没有像 HTTP/2 那样更新到 9113,还是 7541。读它的 §2(静态/动态表)、§4(动态表 size 协商)、§6(头部表示)、Appendix A(静态表 61 项)、Appendix B(Huffman 表)。
- **想看 gRPC 的 HPACK 编码器**:读 [`hpack_encoder.cc`](../grpc/src/core/ext/transport/chttp2/transport/hpack_encoder.cc)(625 行)+ [`hpack_encoder.h`](../grpc/src/core/ext/transport/chttp2/transport/hpack_encoder.h)(511 行,模板 Compressor 体系)+ [`hpack_encoder_table.cc`](../grpc/src/core/ext/transport/chttp2/transport/hpack_encoder_table.cc)(91 行,编码端动态表)。
- **想看 gRPC 的 HPACK 解码器**:读 [`hpack_parser.cc`](../grpc/src/core/ext/transport/chttp2/transport/hpack_parser.cc)(1236 行,主状态机)+ [`hpack_parser_table.cc`](../grpc/src/core/ext/transport/chttp2/transport/hpack_parser_table.cc)(295 行,解码端动态表)。
- **想看 Huffman 解码的实现**:读 [`decode_huff.h`](../grpc/src/core/ext/transport/chttp2/transport/decode_huff.h)(2330 行,主逻辑)+ [`decode_huff.cc`](../grpc/src/core/ext/transport/chttp2/transport/decode_huff.cc)(4371 行,纯表数据)+ codegen 工具 `tools/codegen/core/gen_huffman_decompressor.cc`(看表是怎么生成的)。
- **想看二进制头(`-bin`)处理**:读 [`bin_encoder.cc`](../grpc/src/core/ext/transport/chttp2/transport/bin_encoder.cc) / [`bin_decoder.cc`](../grpc/src/core/ext/transport/chttp2/transport/bin_decoder.cc)。
- **想看 HPACK 在 channelz 里的可观测**:本书 P6-20 会讲 channelz 怎么暴露 HPACK 命中率、条目生命周期等指标。
- **想抓包看 HPACK 的真实压缩效果**:用 Wireshark 解 HTTP/2,过滤一条 gRPC 连续多次调用的连接,看第二次起的 HEADERS 帧有多小(几乎全是索引号)。

### 引出下一章

我们搞清楚了 HPACK 怎么把头部压到几乎零字节。但一次 gRPC 调用除了头部,还有**消息体**(protobuf 序列化后的字节)。这些字节怎么塞进 HTTP/2 的 DATA 帧?gRPC 的 5 字节 Length-Prefixed-Message framing(1 字节压缩位 + 4 字节长度 + 消息体)是怎么设计的?读侧的 parsing 状态机和写侧的 writing 攒批怎么配合?各种控制帧(SETTINGS 协商、PING 探活、GOAWAY 优雅关闭、RST_STREAM 异常终止)各什么时候触发?下一章 P2-08,我们钻进 [`parsing.cc`](../grpc/src/core/ext/transport/chttp2/transport/parsing.cc) + [`writing.cc`](../grpc/src/core/ext/transport/chttp2/transport/writing.cc),拆透 gRPC framing 与双状态机。

> **下一章**:[P2-08 · gRPC framing 与帧解析](P2-08-gRPC-framing与帧解析.md)
