# 第 1 篇 · 第 3 章 · protobuf 编码:为什么又小又快

> **核心问题**:P1-02 讲清了"字段靠编号定位",可那个编号怎么变成线上实实在在的字节?为什么同样是序列化,JSON 又大又慢、protobuf 又小又快?而且 protobuf 还天生前后兼容(加字段不破坏老代码),这套本事到底从哪来?答案藏在 protobuf 的 **wire format(线上字节布局)** 里——它用 **varint 变长整数**、**zigzag 有符号编码**、**tag-length-value** 三件法宝,把每个字段压到最少字节,又让解析定长、无需回溯、不丢未知字段。本章把这套编码拆到字节级,并对照 gRPC **自己**写的变长整数实现 [`varint.cc`](../grpc/src/core/ext/transport/chttp2/transport/varint.cc)——你会发现,"变长编码"这个思想,在 gRPC 自家的 HTTP/2 协议里也落地过一遍。

> **读完本章你会明白**:
> 1. JSON/XML 文本编码为什么又大又慢(字段名重复、文本表示数字、解析要逐字符扫引号转义),protobuf 的二进制 wire format 凭什么小一个数量级、快几倍。
> 2. **varint** 为什么是"每 7 位 + 1 续位",这 7 位不是拍脑袋,是 8 位字节里留 1 位当"还要不要继续读"的信号;**zigzag** 为什么有符号整数不直接 varint 而要先"之字形"变换(不然 -1 这种小负数会撑成 10 字节)。
> 3. **tag-length-value** 布局怎么让解析定长、无需回溯;**packed repeated** 怎么把一串标量值打包省 tag 开销;**unknown fields 透传**在字节层面怎么做到不丢。
> 4. 为什么字段号(P1-02 讲过)直接影响编码大小——小号省字节的字节级原因。

> **如果一读觉得太难**:先只记住三件事——① protobuf 用 varint(每 7 位 + 1 续位)把整数压短,小数字只占 1 字节;② 有符号整数先用 zigzag 之字形变换避免负数撑爆;③ 每个字段在线上是"tag(字段号+类型)+ 值",字段名不进字节;不认识的字段(unknown)原样保留,这就是兼容性的字节级根。

---

## 〇、一句话点破

> **protobuf 编码快和小,根在三件事:varint 让"小数字占小字节"、tag 让"字段按编号自描述、名字不进字节"、unknown fields 让"不认识的字段原样保留"。三者合一,既省带宽,又天生前后兼容。**

这是结论。本章倒过来拆:先看 JSON 慢在哪、大在哪,再一个一个拆 protobuf 的法宝(varint → zigzag → tag → packed → unknown),最后对照 gRPC 自家的 varint 实现把"变长编码为什么这么设计"钉到源码级。

本章服务的二分法是**协议层**——它定义"字节怎么过线"。它承接 P1-02 的"字段号"概念,讲清"字段号怎么变成字节";它又给 P2-08(gRPC framing)埋下伏笔——gRPC 自己的消息帧也有一套长度编码,但选了**和 protobuf 不同**的设计(定长大端,而非变长),这个对照本身就是工程取舍的好教材。

> **诚实标注**:本章讲的 protobuf wire format 是 **protobuf 规范**(定义在 `google/protobuf` 项目里),不是 gRPC 源码里能直接 grep 到的。gRPC 仓库不含 protobuf 库源码(P1-02 已说)。所以本章用 protobuf 官方规范讲清"字段号怎么变成字节",并对照 gRPC **自己**写的、设计同源的变长整数实现 [`varint.cc`](../grpc/src/core/ext/transport/chttp2/transport/varint.cc) 来落地直觉——那可是 gRPC 自家代码,逐行可查。

---

## 一、JSON 慢在哪、大在哪

要理解 protobuf 为什么又小又快,先看它的对手 JSON 慢在哪、大在哪。

### 大在冗余

一个简单的 user 对象,JSON 长这样:

```json
{"user_id": "u123", "name": "Alice", "age": 30}
```

这一行字节的"重量"在哪里?

1. **字段名重复出现**:`"user_id"`、`"name"`、`"age"` 这三串字符,每条消息都要原样发一遍。发 100 万次 user,就发 100 万次 `"user_id"`。字段名越长(很多业务字段名比值还长),浪费越狠。
2. **文本表示数字**:`30` 这个整数,JSON 写成两个 ASCII 字符 `'3'` `'0'`(0x33 0x30),占 2 字节;而它在内存里本来只要 1 字节(0x1E)就够。
3. **结构开销**:引号 `"`、冒号 `:`、逗号 `,`、花括号 `{}`——这些"语法糖"每个都是字节,加起来可观。

### 慢在解析

JSON 解析慢,根在它是**文本**:

1. **逐字符扫描**:解析器要从头到尾一个字符一个字符扫,看见 `"` 知道开始一个字符串,扫到下一个 `"` 知道结束,中间还要处理转义(`\"`、`\\`、`\n`……)。这是纯字符级状态机,慢。
2. **数字解析要算**:`30` 要从 ASCII 字符 `'3'` `'0'` 算回数值 30(`3*10 + 0`),大数字更慢。
3. **不能随机访问**:想读 `age` 字段,你得先把前面的 `user_id`、`name` 扫完(因为 JSON 是顺序的,字段位置不固定)。没法"直接跳到第 N 个字段"。

> **不这样会怎样**:JSON 这些特性,对"人读"是优点(可读、可手写、通用),对"机器高频传输"全是缺点。100 万次调用,JSON 光是重复发字段名,就能吃掉可观带宽;解析时逐字符扫描,在百万 QPS 下吃 CPU。这就是 gRPC 不选 JSON 的根——**它要在跨网络、海量调用的场景下,既省带宽、又省 CPU**。

### protobuf 二进制怎么解决

protobuf 的回答是**二进制 wire format**:

- **字段名不进字节**:字段靠编号(P1-02 讲过)定位,编号是数字,在线上又短又紧凑。`"user_id"` 这 7 个字符,protobuf 只发 1 个字节的 tag(编号 1 + 类型)。
- **数字用变长整数**:30 这个数,protobuf 用 varint 编码只占 1 字节(0x1E),不是文本的 2 字节,更不是 int32 定长的 4 字节。
- **解析是定长、无回溯的**:每个字段前面有 tag(编号+类型),解析器读到 tag 就知道"这是几号字段、什么类型、值多长",直接按长度读,不用逐字符扫描。

下面三节,把这三件法宝一个一个拆透。

---

## 二、varint:为什么是"每 7 位 + 1 续位"

protobuf 编码的第一件法宝,是**变长整数(varint)**。它解决一个根本矛盾:**整数的大小差异巨大,但定长编码(int32 固定 4 字节、int64 固定 8 字节)对"小数字"严重浪费**。

### 定长编码的浪费

内存里的 `int32 age = 30`,占 4 字节:`0x00 0x00 0x00 0x1E`(小端)。前 3 个字节全是 0——**30 这个数字本来 1 字节就装得下,却要占 4 字节**。在网络传输里,这是纯浪费。

真实数据里,小数字占多数:年龄、计数、状态码、字段号、bool——这些值通常都在 0~127 之间。如果都用定长 4 字节,海量小数字会浪费惊人的带宽。

### varint 的解法:每 7 位一组,留 1 位当续位信号

varint 的思路:**用字节数反映数值大小**——小数字用少字节,大数字用多字节。具体规则:

- 把整数按**每 7 位**一组,从低位到高位切片。
- 每组放进一个字节的**低 7 位**。
- 那个字节的**最高位(MSB,most significant bit)**当"续位信号":**1 表示后面还有字节,0 表示这是最后一字节**。

举几个例子:

```
数值 1 (0b0000001):
  varint: 0x01                    ← 1 字节,MSB=0,结束
                                 
数值 300 (0b100101100):
  切成 7 位:低7位 0101100, 高2位 10
  varint: 0xAC 0x02              ← 2 字节
    字节1: 1_0101100  (MSB=1,还有;低7位是 0101100)
    字节2: 0_0000010  (MSB=0,结束;低7位是 0000010)
  解析: 0101100 | (0000010 << 7) = 0x12C = 300 ✓
                                 
数值 150 (0b10010110):
  varint: 0x96 0x01              ← 2 字节
```

效果:**1 以内**的数字(varint 的 7 位能放下的范围是 0~127)只占 1 字节;128~16383 占 2 字节;以此类推。这正好对应"小数字多、大数字少"的真实分布,把定长编码的浪费榨干。

### 为什么是 7 位,不是 6 位或 4 位?

这是个关键的"为什么"。答案是:**8 位字节里,必须留 1 位当续位信号,所以一组最多 7 位有效数据**。

> **不这样会怎样(留更多续位)**:如果一组只放 4 位(留 4 位当控制),那 1 字节只能表 0~15,稍大的数字(比如 100)就要 2 字节——压缩比差,小数字都不省。压缩不够。
>
> **不这样会怎样(留更少续位)**:如果一组放 8 位(不留续位),那就没有续位信号,解析器无法知道"这个 varint 到底几字节",得靠别的机制(比如外部给长度)——那就不是"自描述变长"了。所以**7 位是 8 位字节能提供的最大有效位,留 1 位续位是最低开销的自描述方案**。这是带宽和自描述能力的甜点。

### 对照 gRPC 自家的 varint 实现

protobuf 的 varint 是 protobuf 库实现的,不在 gRPC 源码里。但 gRPC **自己**也写了一套变长整数——在 HTTP/2 的 HPACK 头部压缩里(RFC 7541)。来看 gRPC 的实现 [`src/core/ext/transport/chttp2/transport/varint.h`](../grpc/src/core/ext/transport/chttp2/transport/varint.h#L30-L36):

```cpp
namespace grpc_core {

// maximum value that can be bitpacked with the opcode if the opcode has a
// prefix of length prefix_bits
constexpr uint32_t MaxInVarintPrefix(uint8_t prefix_bits) {
  return (1 << (8 - prefix_bits)) - 1;
}
```

注意这个 `MaxInVarintPrefix`——它揭示了 HPACK varint 和 protobuf varint 的一个关键区别。HPACK 的 varint 不是"每 7 位一组"那么纯粹,它是**"前缀位 + 续位"**模式:操作码字节的高位是操作码,低位是前缀(能塞下的小数字),塞不下才用续位字节。

这个 `prefix_bits` 是"前缀占几位"。比如 `prefix_bits = 1`,意味着操作码字节里有 1 位是操作码、7 位是前缀,前缀能放下的最大值是 `(1 << 7) - 1 = 127`。如果数值 ≤ 127,直接塞进前缀(1 字节搞定);如果 > 127,前缀全填 1(表示"满溢,看续位"),剩下的值走续位字节。这正是 HPACK 整数编码(RFC 7541 §5.1)的实现。

看 HPACK 怎么用它,在 [`hpack_encoder.cc`](../grpc/src/core/ext/transport/chttp2/transport/hpack_encoder.cc#L227)(字符串值的长度编码):

```cpp
class NonBinaryStringValue {
 public:
  explicit NonBinaryStringValue(Slice value)
      : value_(std::move(value)), len_val_(value_.length()) {}
  ...
 private:
  Slice value_;
  VarintWriter<1> len_val_;   // ← 1 位前缀的 varint,编码字符串长度
};
```

`VarintWriter<1>` 就是用 1 位前缀的 varint writer。它编码的是"这个 header value 的字符串长度"。看它怎么写 [`varint.h`](../grpc/src/core/ext/transport/chttp2/transport/varint.h#L44-L65):

```cpp
template <uint8_t kPrefixBits>
class VarintWriter {
 public:
  static constexpr uint32_t kMaxInPrefix = MaxInVarintPrefix(kPrefixBits);

  explicit VarintWriter(size_t value)
      : value_(value),
        length_(value < kMaxInPrefix ? 1 : VarintLength(value - kMaxInPrefix)) {
    GRPC_CHECK(value <= UINT32_MAX);
  }
  ...
  void Write(uint8_t prefix, uint8_t* target) const {
    if (length_ == 1) {
      target[0] = prefix | value_;            // 小值,直接塞前缀
    } else {
      target[0] = prefix | kMaxInPrefix;      // 大值,前缀填满
      VarintWriteTail(value_ - kMaxInPrefix, target + 1, length_ - 1);
    }
  }
  ...
};
```

读这段代码,能看出 HPACK varint 的精妙:

1. **构造时就算好长度**(`length_`):小值 `length_ = 1`,大值算续位字节数。这让写之前就知道要多少字节,可以一次性分配缓冲,避免反复 realloc。
2. **写的时候分两条路**:小值(能塞进前缀)走快路,1 字节搞定;大值走慢路,前缀填满 + 续位字节。
3. **续位字节的写法** `VarintWriteTail`,看 [`varint.cc`](../grpc/src/core/ext/transport/chttp2/transport/varint.cc#L41-L59):

```cpp
void VarintWriteTail(size_t tail_value, uint8_t* target, size_t tail_length) {
  switch (tail_length) {
    case 5:
      target[4] = static_cast<uint8_t>((tail_value >> 28) | 0x80);
      [[fallthrough]];
    case 4:
      target[3] = static_cast<uint8_t>((tail_value >> 21) | 0x80);
      [[fallthrough]];
    ...
    case 1:
      target[0] = static_cast<uint8_t>((tail_value) | 0x80);
  }
  target[tail_length - 1] &= 0x7f;   // ← 最后一字节的 MSB 清 0,表示结束
}
```

注意三个细节:

- **每组 7 位**(`>> 28`、`>> 21`、`>> 14`、`>> 7`),和 protobuf varint 完全同源——**7 位数据 + 1 位续位**。
- **`| 0x80`**:把 MSB 置 1,表示"后面还有"。最后一字节靠最后一行 `&= 0x7f` 清掉 MSB,表示"我这里是结束"。
- **`[[fallthrough]]` + switch**:用 switch 的 fallthrough 特性,从 `tail_length` 对应的 case 一路写下来,避免循环——这是性能优化,编译后是一段直线代码(streamlined),没有分支预测开销。

> **钉死这件事**:gRPC 自家的 varint(`varint.cc`)和 protobuf 的 varint,**思想完全同源**——都是"每 7 位 + 1 续位"。区别只在 gRPC 的版本多了个"前缀位"概念(为了和 HPACK 操作码共享一个字节)。这一节你看懂了 `varint.cc`,就等于看懂了 protobuf varint 的字节布局。这是 P1-02 埋的伏笔("P1-03 会看到 gRPC 自己的 HTTP/2 也用了同款 varint")的兑现——**同一套变长编码思想,在 protobuf 和 gRPC HTTP/2 两个地方各落地一遍**。

---

## 三、zigzag:为什么有符号整数不直接 varint

varint 解决了"无符号整数"的变长编码。但 protobuf 还有有符号整数(`int32`、`int64`、`sint32`、`sint64`)。这里有个陷阱:**直接对负数做 varint,会撑成满长度**。

### 负数的 varint 陷阱

计算机里,负数用**补码**表示。比如 `-1` 作为 `int32`,补码是 `0xFFFFFFFF`(32 个 1);作为 `int64`,补码是 `0xFFFFFFFFFFFFFFFF`(64 个 1)。

如果直接对这个补码做 varint 编码,会发生什么?varint 是"每组 7 位,只要高位还有 1 就继续",而 `-1` 的高位全是 1——**所以 `-1` 的 varint 会一路撑到 10 字节**(64 位 / 7 位 ≈ 10 组)。

这太荒谬了。`-1` 这么小的数(绝对值就是 1),本来 1 字节该够,却撑成 10 字节,比定长的 4 字节还大。

> **不这样会怎样**:如果在 RPC 接口里用 `int32` 表示"温度偏移量",值大多是 `-5` 到 `+5` 这种小数(包括负数),直接 varint 编码下每个负数都撑成 10 字节,带宽浪费比定长还狠。varint 对小负数完全失效。

### zigzag 的解法:之字形变换

protobuf 的 `sint32` / `sint64` 类型用了 **zigzag(之字形)变换**来解决这个问题。思路极其巧妙:

**把"有符号整数"映射成"无符号整数",映射规则是让绝对值小的数(无论正负)映射成小的无符号数。**

具体映射:

```
原值      zigzag 映射后的无符号值
 0   →    0
-1   →    1
 1   →    2
-2   →    3
 2   →    4
-3   →    5
 3   →    6
 ...
```

看出来了吗?它在数轴上"之字形"地走:`0, -1, 1, -2, 2, -3, 3, ...`,把这些数按"绝对值从小到大"排成 `0, 1, 2, 3, 4, 5, 6, ...`。这样,`-1` 映射成 `1`,`-2` 映射成 `3`——全是小无符号数,varint 编码下只占 1 字节。

数学公式(对 `sint32`):

```
zigzag(n) = (n << 1) ^ (n >> 31)       // 编码
unzigzag(u) = (u >> 1) ^ -(u & 1)      // 解码
```

`n >> 31` 是算术右移,负数的话高位全填 1(变成 `0xFFFFFFFF`),正数全填 0。这个 `^` 异或,本质上是在"对负数做取反操作、对正数保持不变",实现之字形映射。

> **钉死这件事**:zigzag 的妙处是它**没有改变 varint 的机制**(还是每 7 位 + 续位),只是在 varint **之前**加一层映射,把"小负数 → 小无符号数"。这样 varint 对小负数也高效了。`sint32` / `sint64` 是 protobuf 里专门配 zigzag 的类型——如果你的字段可能取负值且绝对值不大(比如温度偏移、坐标增量),用 `sint32` 比 `int32` 省字节得多。这是 protobuf 字段类型选择里一个实战要点。

### 普通 int32 负数怎么办?

注意,protobuf 的 `int32` / `int64`(不带 `s`)类型,**不做 zigzag**——它们直接把补码当无符号数 varint 编码。所以 `int32` 类型的 `-1` 还是会撑成 10 字节。

这是 protobuf 的一个设计权衡:**它不替你决定"要不要 zigzag",而是给你两种类型让你按场景选**。字段总是非负(比如计数、ID)→ 用 `int32`/`uint32`;字段可能取负且绝对值小 → 用 `sint32`。这种"把选择权交给用户"的设计,是 protobuf 在"通用性"和"效率"之间的权衡。

---

## 四、tag-length-value:每个字段自描述

varint 和 zigzag 解决了"单个数值怎么编码"。可一个 message 有多个字段,怎么把"这是几号字段、什么类型、值是多少"打包进字节?protobuf 的答案是 **tag-length-value(TLV)** 布局。

### 每个 field = tag + value(或 tag + length + value)

protobuf 序列化一个 message 时,把它当成"一串字段",每个字段按这个布局编码:

```
[field1 的 tag][field1 的 value]   [field2 的 tag][field2 的 value]   ...
```

其中 **tag** 是一个 varint,打包了两样东西:

- **字段号**(P1-02 讲的那个编号):`field_number << 3`
- **wire type**(线上类型,3 位):`wire_type`

公式:`tag = (field_number << 3) | wire_type`。

wire type 只有几种(protobuf 规范定义):

| wire type | 含义 | 用法 |
|-----------|------|------|
| 0 | Varint | int32, int64, uint32, uint64, sint32, sint64, bool, enum |
| 1 | 64-bit | fixed64, sfixed64, double |
| 2 | Length-delimited | string, bytes, embedded message, packed repeated |
| 5 | 32-bit | fixed32, sfixed32, float |

(3、4 是已废弃的 group 类型,不用管。)

**value** 部分按 wire type 不同:

- **wire type 0(varint)**:value 就是 varint 编码的数值,定长可推(读到 MSB=0 为止)。
- **wire type 1(64-bit)**:value 是固定 8 字节。
- **wire type 5(32-bit)**:value 是固定 4 字节。
- **wire type 2(length-delimited)**:value 是 `[长度 varint][实际字节]`。字符串、bytes、嵌套 message、packed repeated 都用这个——因为它们长度可变,得显式给长度。

### 一个完整例子

回到 P1-02 的 `HelloRequest { string name = 1; }`,序列化 `name = "world"`:

```
tag:        field_number=1, wire_type=2 (string)
            tag = (1 << 3) | 2 = 0x0A
length:     "world" 长度 5, varint = 0x05
value:      "world" 的 ASCII = 77 6F 72 6C 64

完整字节:   0A 05 77 6F 72 6C 64
            ↑tag ↑len ←—— "world" ———→
```

7 个字节,编码了"1 号字段、字符串类型、长度 5、值 world"。**字段名 "name" 完全没出现**——它靠编号 1 被识别。

> **钉死这件事**:TLV 布局让 protobuf 解析**定长、无需回溯**。解析器读到 tag,立刻知道:① 这是几号字段(查 descriptor 就知道名字);② 什么 wire type;③ 值占多长(varint 类型读到 MSB=0、定长类型固定字节、length-delimited 类型先读长度再按长度读)。读完一个字段,直接跳到下一个 tag,从头到尾扫一遍就解析完。**不需要像 JSON 那样逐字符扫描引号、转义**。这就是 protobuf 解析比 JSON 快几倍的根。

### 字段顺序无关

注意 TLV 布局的一个推论:**字段在线上的先后顺序,不影响正确性**。收方按 tag 找字段,不是按位置——tag=1 的字段在线上排第几都行,只要 tag 在就能认。

这让 protobuf 有两个优化空间:

1. **重新排列字段**:序列化时可以按字段号或类型重新排,不影响解析(只要收方按 tag 找)。比如把同类型字段挨着放,可以批量处理。
2. **packed repeated**(下节讲):把一串同类型值挤在一起,省掉重复的 tag。

---

## 五、packed repeated:一串标量打包省 tag

`repeated`(数组)字段如果每个值都带一个 tag,在"值很小但个数很多"时会浪费:1000 个 `repeated int32 = [1, 2, 3, ...]`,每个值 1 字节,但每个值前面还要 1 字节 tag,tag 开销和值开销五五开。

protobuf 的解法是 **packed repeated**:对**标量数字类型**的 repeated 字段(proto3 默认 packed),把所有值**打包成一坨**,共享一个 tag 和一个总长度:

```
// 朴素(不 packed):
repeated int32 values = [1, 2, 3]
字节: [tag1][1][tag2][2][tag3][3]      ← 3 个 tag

// packed:
repeated int32 values = [1, 2, 3]
字节: [tag][总长度=3][1][2][3]          ← 1 个 tag + 1 个长度
```

1000 个小整数,packed 下只 1 个 tag + 1 个总长度 + 1000 个值;不 packed 下要 1000 个 tag。**packed 把 tag 开销摊薄到几乎为零**。

> **不这样会怎样**:海量小数字的数组(比如一个机器学习模型的权重索引、一组 sensor 读数、一串 feature id),不 packed 的话 tag 开销能吃掉一半带宽。packed 让这种"小值多量"场景的编码效率提升一倍。这是 protobuf 在真实业务(尤其是 ML、监控、统计)里省带宽的实战技巧。

packed 只对**标量数字类型**(varint/32-bit/64-bit)生效,对 length-delimited 类型(string、message)不 packed——因为它们各自带长度,打包反而要更复杂的索引,得不偿失。这是 protobuf 的克制:**只对能明确省的地方优化**。

---

## 六、unknown fields 透传:兼容性的字节级根

P1-02 讲了"加字段不破坏老代码",靠的是 unknown fields 透传。现在拆它的字节级实现。

### 老代码遇到新字段会怎样

设想 P1-02 那个场景:新服务端发 `User { user_id=1, name=2, email=3 }`,老客户端只认 1 和 2,不认 3。

老客户端解析字节时,按 TLV 一个个读:

```
字节: [tag1 user_id][val]  [tag2 name][val]  [tag3 email][val]
                                                    ↑
                              老客户端不认识 tag3(字段号 3 没在老 .proto 里)
```

老客户端读到 tag3,解析出"字段号 3、wire type X"。它查自己的 descriptor(由老 .proto 生成),发现没有字段号 3——这是个 **unknown field**。

protobuf 的处理:**不报错、不丢弃,把整个字段的原始字节(tag + length + value)原样保留**。这就是 "unknown fields 透传"。

### 透传的字节级做法

具体怎么"原样保留"?因为 TLV 布局下,每个字段是**自描述定长**的(tag 给出 wire type,wire type 决定 value 怎么读),所以解析器遇到 unknown field 时,**能精确算出这个字段占多少字节**(varint 读到 MSB=0、定长读固定字节、length-delimited 先读长度)。它把这段字节复制下来,挂在解析出来的 message 对象的一个 "unknown fields" 区。

之后如果这个 message 又被序列化回去(比如一个代理服务收到再转发),protobuf 会把 unknown fields 区的字节**原样拼回输出**。于是新字段 `email` 穿过老客户端,毫发无损地到达下游。

> **钉死这件事**:unknown fields 透传能在字节级工作,**全靠 TLV 布局让每个字段自描述、定长可跳过**。位置定位的协议(前面 P1-02 技巧精解讲过)做不到这点——它没法表达"这里有个你不认识的额外字段",只能读到末尾停,新字段无声丢失。TLV 是兼容性的字节级根基,P1-02 讲的"字段号换兼容",靠的就是这个字节级机制兜底。

### proto3 一度丢 unknown fields 的坑

补一个历史坑:proto3 刚出来时,默认行为是**丢弃** unknown fields(为了"省内存"),不像 proto2 那样保留。这破坏了"透传"的兼容性承诺,被用户大量吐槽。后来 protobuf 团队改回了"默认保留 unknown fields"(proto3.5 起),这才和 proto2 行为一致。这个坑说明:**兼容性是 protobuf 的核心承诺,任何破坏它的"优化"都会被回退**。

---

## 七、技巧精解:`varint.cc` 的 fallthrough 写法,以及它和 gRPC framing 的对照

本章技巧精解,钉两个东西:一是 gRPC 自家 `varint.cc` 里那段 switch fallthrough 写法的性能精妙;二是 protobuf varint 和 gRPC 自身 framing 长度编码的**设计差异**——这是同一类问题(编码长度)的两种答案,理解它你就理解了"协议设计里没有银弹"。

### 技巧一:`VarintWriteTail` 的 fallthrough 直线代码

回头看 [`varint.cc`](../grpc/src/core/ext/transport/chttp2/transport/varint.cc#L41-L59) 的 `VarintWriteTail`:

```cpp
void VarintWriteTail(size_t tail_value, uint8_t* target, size_t tail_length) {
  switch (tail_length) {
    case 5:
      target[4] = static_cast<uint8_t>((tail_value >> 28) | 0x80);
      [[fallthrough]];
    case 4:
      target[3] = static_cast<uint8_t>((tail_value >> 21) | 0x80);
      [[fallthrough]];
    case 3:
      target[2] = static_cast<uint8_t>((tail_value >> 14) | 0x80);
      [[fallthrough]];
    case 2:
      target[1] = static_cast<uint8_t>((tail_value >> 7) | 0x80);
      [[fallthrough]];
    case 1:
      target[0] = static_cast<uint8_t>((tail_value) | 0x80);
  }
  target[tail_length - 1] &= 0x7f;
}
```

#### 朴素地写会撞什么墙

朴素地写这段,会写成循环:

```cpp
// 朴素写法(示意,非源码原文)
void VarintWriteTailNaive(size_t tail_value, uint8_t* target, size_t tail_length) {
  for (size_t i = 0; i < tail_length; i++) {
    target[i] = (tail_value >> (7 * i)) | 0x80;
  }
  target[tail_length - 1] &= 0x7f;
}
```

这个循环版本的问题:

1. **每次循环有分支判断**(`i < tail_length`)——CPU 分支预测虽然通常准,但在 hot path 里仍有开销。
2. **循环变量 `i` 要参与计算 `7 * i`**(移位量)——多一次乘法(虽然编译器可能优化成加法)。
3. **编译器不一定能展开**——尾部长度 `tail_length` 是运行时值,循环边界动态,编译器很难自动展开成直线代码。

#### switch fallthrough 的妙处

gRPC 的写法用 `switch(tail_length)`,从对应的 case **fallthrough 一路写下来**:

- `tail_length = 5` 时,从 `case 5` 进,依次写 target[4]、target[3]、target[2]、target[1]、target[0]——5 次赋值,顺序固定。
- `tail_length = 3` 时,从 `case 3` 进,写 target[2]、target[1]、target[0]——3 次赋值。
- 每条 case 里,移位量是**编译期常量**(`>> 28`、`>> 21`、`>> 14`、`>> 7`),没有运行时乘法。
- `[[fallthrough]]` 显式标注(避免编译器警告),告诉编译器"这是有意的穿透"。

编译后,这是一段**直线代码(straight-line code)**——没有循环、没有分支判断,就是一串赋值指令。CPU 流水线畅行无阻,指令缓存友好。

> **不这样会怎样**:HPACK varint 的写是 gRPC 写循环里的 hot path(每个 header 都要写长度),用朴素循环版本,在海量 header 编码下会吃掉可观的 CPU。switch fallthrough 是教科书级的"用编译期常量 + 顺序代码换运行时分支"优化。这是 gRPC core 里那种"看起来奇怪、细想是性能精妙"的代码——本书后面 HPACK(P2-07)、flow control(P2-09)还有大量这种技巧,这里先建立直觉。

#### 一个细节:为什么 `tail_length` 最大 5

看 [`varint.cc`](../grpc/src/core/ext/transport/chttp2/transport/varint.cc#L27-L39) 的 `VarintLength`:

```cpp
size_t VarintLength(size_t tail_value) {
  if (tail_value < (1 << 7)) {
    return 2;
  } else if (tail_value < (1 << 14)) {
    return 3;
  } else if (tail_value < (1 << 21)) {
    return 4;
  } else if (tail_value < (1 << 28)) {
    return 5;
  } else {
    return 6;
  }
}
```

注意它返回的最小值是 **2**(不是 1)。为什么?因为这个 `VarintLength` 算的是"前缀溢出后的续位字节数 + 1(操作码字节)"。HPACK 的 varint 总有 1 字节操作码(里面含前缀),溢出后续位最多 5 字节(因为 HPACK 整数最大 32 位,32/7 ≈ 5),所以总长度 1(操作码)+ 5(续位)= 6 字节上限。这和 protobuf 的"纯 varint"不同——protobuf 没有操作码字节,纯粹是续位字节。这个差异是 HPACK 整数和 protobuf varint 的结构区别,源码里 `VarintLength` 返回 2~6 这个范围,忠实地反映了 HPACK 的结构。

### 技巧二:protobuf varint vs gRPC framing 长度——两种答案

这一节钉一个容易混淆、但极有教益的对照。前面讲 protobuf 用 varint 编码字段值和字段长度。那 gRPC **自己**的消息帧(把 protobuf 消息塞进 HTTP/2 DATA 帧的那层封装)的长度,也用 varint 吗?

**不用。gRPC framing 的长度是 4 字节定长大端序。** 看 gRPC 自己的 [`frame.cc`](../grpc/src/core/ext/transport/chttp2/transport/frame.cc#L155-L159) 和 [`frame.h`](../grpc/src/core/ext/transport/chttp2/transport/frame.h#L254):

```cpp
// frame.h
constexpr uint8_t kGrpcHeaderSizeInBytes = 5;   // gRPC 消息帧头:5 字节

// frame.cc
void Write4b(uint32_t x, uint8_t* output) {
  output[0] = static_cast<uint8_t>(x >> 24);    // 大端!高位在前
  output[1] = static_cast<uint8_t>(x >> 16);
  output[2] = static_cast<uint8_t>(x >> 8);
  output[3] = static_cast<uint8_t>(x);
}
```

gRPC 的消息帧是 **5 字节固定头:1 字节压缩标志 + 4 字节大端长度**(见 [`AppendGrpcHeaderToSliceBuffer`](../grpc/src/core/ext/transport/chttp2/transport/frame.cc#L820-L824))。这个长度**永远是 4 字节,不用 varint**。

#### 为什么 gRPC framing 选定长,protobuf 选变长?

这是个绝佳的"同一问题两种答案"对照。它揭示了:**协议设计里没有银弹,选哪种编码,取决于"长度值的特点"**。

| 维度 | protobuf 字段长度 | gRPC framing 消息长度 |
|------|-------------------|----------------------|
| 取值分布 | 多数小(string 短、message 小) | 跨度大(几 B 到几 MB) |
| 是否需要边界预知 | 不需要(收方边解析边读) | **需要**(收到帧头就知道这条消息多大,要不要等更多 DATA 帧) |
| 压缩收益 | 高(海量小字段) | 低(一条消息一个长度,省不出几个字节) |

protobuf 选 varint,因为字段长度多数很小,变长能省可观字节;而且字段是 TLV 流式解析,不需要预先知道总长。

gRPC framing 选 4 字节定长大端,因为:

1. **消息长度跨度大**:一条 gRPC 消息可能是几十字节(一个 status),也可能是几兆字节(一个大文件块)。varint 对"小长度"省字节,但消息长度的分布不像字段长度那样集中在小值——大消息的长度本身就是个大数,varint 编码反而可能比定长还长。
2. **需要边界预知**:gRPC 在 HTTP/2 上传输时,一条消息可能跨多个 DATA 帧(被 HTTP/2 的 max frame size 切开)。接收方需要在**收到第一个字节时就知道这条消息多长**,好决定"还要不要继续攒后续 DATA 帧"。4 字节定长头让这个判断是 O(1) 的——读前 5 字节立刻知道。varint 的话,长度本身的字节数是变的,解析器要先解析 varint(可能 1 字节、可能 5 字节),多一层间接。
3. **对齐和网络字节序**:大端序是网络字节序的传统(TCP/IP 头也是大端),4 字节定长可以 `ntohl` 一次转换,处理器友好。

> **钉死这件事**:protobuf varint 和 gRPC framing 定长,不是"一个对一个错",而是**针对不同场景的合理选择**。protobuf 字段长度"小而多",varint 省字节收益高;gRPC 消息长度"大而少",定长换解析简单和边界预知。**这是协议设计"没有银弹"的活教材**:选哪种编码,要看值的分布、看是否需要预知边界、看解析开销。理解这个对照,你就理解了为什么 P2-08(gRPC framing)会用定长、而 protobuf 字段用变长——同一套"编码长度"的问题,两个层各选了最适合自己的答案。

#### 一个细节:gRPC 还在哪用了 varint

补充一个事实,避免混淆:gRPC **确实**在自家代码里用了 varint(`varint.cc`),但那是给 **HPACK 头部压缩**(RFC 7541)和 **grpc-message trailer 长度**用的,不是给消息帧长度。看 [`chttp2_transport.cc:2847`](../grpc/src/core/ext/transport/chttp2/transport/chttp2_transport.cc#L2847) 这段(写 grpc-message trailer):

```cpp
grpc_core::VarintWriter<1> msg_len_writer(
    static_cast<uint32_t>(msg_len));
message_pfx = GRPC_SLICE_MALLOC(14 + msg_len_writer.length());
p = GRPC_SLICE_START_PTR(message_pfx);
*p++ = 0x00;  // literal header, not indexed
*p++ = 12;    // len(grpc-message)
// ... 写 "grpc-message" 这 12 个字符 ...
msg_len_writer.Write(0, p);   // ← 用 varint 写 grpc-message 值的长度
```

这里用 `VarintWriter<1>` 写的是 `grpc-message` 这个 trailer 头部的**值的长度**(因为 HPACK 字符串头部要带长度)。这是 HPACK 的规则,不是 gRPC framing 的规则。gRPC framing 的长度(4 字节大端)在另一处(`AppendGrpcHeaderToSliceBuffer`)。两者井水不犯河水——**HPACK 用 varint,gRPC framing 用定长,protobuf 字段用 varint**,三套编码在同一个 gRPC 调用里各管一段。这是本章最值得钉死的"全局图景"。

---

## 八、章末小结

### 回扣主线

本章是协议层的第二块地基,承接 P1-02 的"字段号"、讲清"字段号怎么变成字节"。protobuf 编码的三件法宝:

- **varint**(每 7 位 + 1 续位)让小数字占小字节,榨干定长编码的浪费;
- **zigzag**(之字形变换)让小负数不被补码撑爆;
- **tag-length-value** 让每个字段自描述、解析定长无回溯、字段名不进字节。

再加 **packed repeated**(省 tag 开销)和 **unknown fields 透传**(兼容性的字节级根),protobuf 在"省带宽 + 省 CPU + 前后兼容"三件事上,全面碾压 JSON。

本章还做了一件 P1-02 埋伏笔的事:**对照 gRPC 自家的 varint 实现**(`varint.cc`)。protobuf varint 和 gRPC HPACK varint 思想同源(都是每 7 位 + 续位),而 gRPC framing 长度选了定长大端——同一类问题(编码长度)的不同答案,揭示了协议设计"没有银弹"。

### 五个为什么

1. **为什么 varint 是"每 7 位 + 1 续位"?**——8 位字节必须留 1 位当续位信号(表示"后面还有没有"),所以一组最多 7 位有效数据。这是"8 位字节能提供的最大有效位 + 最低开销的自描述"的甜点。留更多位压缩比差,留更少位没法自描述。

2. **为什么有符号整数要 zigzag 而不直接 varint?**——补码下 `-1` 高位全是 1,直接 varint 会撑成 10 字节(64 位 / 7 位)。zigzag 把"小负数 → 小无符号数"(`-1→1, -2→3`),varint 才能对小负数高效。`sint32`/`sint64` 是专门配 zigzag 的类型,字段可能取小负值时用它。

3. **为什么 protobuf 解析比 JSON 快?**——JSON 要逐字符扫引号、转义、算数字;protobuf 是 TLV,每个字段 tag 给出 wire type,wire type 决定 value 怎么读(定长或读 varint),解析器读完一个字段直接跳下一个 tag,从头扫一遍就完。定长、无需回溯、无字符级状态机。

4. **为什么 gRPC framing 用 4 字节定长大端,而 protobuf 字段用 varint?**——protobuf 字段长度"小而多",varint 省字节收益高、且 TLV 流式解析不需预知总长;gRPC 消息长度"大而少",定长换"收到帧头就知消息多大"(跨 DATA 帧攒消息时必需)、换大端序的 `ntohl` 一次转换。同一类问题(编码长度),两种合理答案。

5. **为什么 unknown fields 透传能在字节级工作?**——TLV 布局让每个字段自描述、定长可跳过。解析器遇到 unknown field,能从 tag 的 wire type 精确算出这字段占多少字节,把这段字节原样保留挂在 message 的 unknown fields 区,转发时原样拼回。位置定位协议做不到这点(没法表达"这里有个你不认识的额外字段")。

### 想继续深入往哪钻

- **想看 protobuf wire format 全规范**:读 protobuf 官方文档 "Protocol Buffers Encoding"(也叫 wire format 说明),有完整的 wire type 表和编码例子。
- **想动手算字节**:用 `protoc --decode_raw` 命令,可以 dump 一段 protobuf 字节的字段号/wire type/值,亲手算一遍最深刻。
- **想看 gRPC 自家 varint 实现**:逐行读 [`src/core/ext/transport/chttp2/transport/varint.h`](../grpc/src/core/ext/transport/chttp2/transport/varint.h) 和 [`varint.cc`](../grpc/src/core/ext/transport/chttp2/transport/varint.cc),再读 [`hpack_encoder.cc`](../grpc/src/core/ext/transport/chttp2/transport/hpack_encoder.cc) 看 `VarintWriter<1>` 怎么用。这是 gRPC 自家代码,可以钉到源码级。
- **想看 gRPC framing 的定长实现**:读 [`frame.cc`](../grpc/src/core/ext/transport/chttp2/transport/frame.cc#L155-L166) 的 `Write4b`/`Read4b` 和 [`frame.h`](../grpc/src/core/ext/transport/chttp2/transport/frame.h#L254-L261) 的 `kGrpcHeaderSizeInBytes`,P2-08 会拆透。
- **想深入 HPACK 整数编码**:读 RFC 7541 §5(HPACK 整数编码),对照 gRPC 的 `varint.cc` 实现,本书 P2-07 会拆到源码级。

### 引出下一章

我们搞清楚了"字段号怎么变成又小又快又兼容的字节"。可这份字节,怎么变成 C++ 能 `stub->SayHello(req)` 调用、Java 能 `stub.sayHello(req)` 调用、Go 能 `stub.SayHello(ctx, req)` 调用的**本地代码**?那份语言无关的 `.proto`,怎么一次生成出**五种语言**的 Stub,还给每种语言**一次给齐 sync / async / callback 三套 API**?下一章 P1-04,我们拆代码生成:protoc + `grpc_cpp_plugin` 怎么把契约变出可调用的 Stub,以及服务端怎么按方法名路由到 handler。

> **下一章**:[P1-04 · 代码生成与 Stub](P1-04-代码生成与Stub.md)
