# 第四章 · SDS:为什么 Redis 不用 char\*

> 篇:P2 数据结构的执掌
> 主轴呼应:这一章是**取向②(内存即数据库)和取向④(简单优先)的最小样本**。Redis 的整个数据库就是内存里的一堆字符串——键名是字符串,值一大半是字符串,List/Hash/Set/ZSet 的元素也都靠字符串承载。所以"字符串"这件事必须做到极致:既要快,又要省,还要扛得住任意字节。SDS(Simple Dynamic String)就是 Redis 给出的答案,它用一种小到不可思议的方式,把 C 语言的 `char*` 升级成了一个记长度、二进制安全、能预分配摊还、还能按长度自动选型省内存的动态字符串。

---

## 读完本章你会明白

1. **为什么 Redis 连字符串都要自己造一个,而不直接用 C 的 `char*`**——因为 `char*` 有四个绕不开的毛病(取长度 O(n)、不二进制安全、每次追加都 realloc、容易溢出),而 SDS 用一个"header 放前面"的小改动,把这四个问题一次性解决。
2. **为什么 SDS 要分五种 header(sdshdr5/8/16/32/64),而不是统一用一个 `size_t` 记长度**——因为海量小键场景下,统一 8 字节头会让一亿个键白白多花 1.4 GB,而这本该是 Redis 数据库本身的内存。
3. **为什么 Redis 要给字符串加预分配(翻倍 / 加 1MB),而不是每次追加就 realloc 一次**——因为翻倍策略把"逐字节追加到 N"的 realloc 次数从 O(N) 压到 O(log N),这是经典的几何级数摊还 O(1),而 1MB 上限是把"内存峰值膨胀"按住的工程阀门。
4. **为什么 sdsMakeRoomFor 在小字符串上翻倍、大字符串上只加 1MB,而不是无脑翻倍**——因为翻倍到 MB 级,内存峰值翻一番的代价会超过省下的那几次 realloc;1MB 是 Redis 在实践中调出来的"再翻一次就难以承受"的心理门槛。
5. **`__attribute__((packed))` 这一个编译属性,凭什么能给 sdshdr8 省下 5 字节、给 sdshdr16 省下 6 字节**——它取消了结构体的对齐填充,让字段紧密相邻;代价是非对齐访问慢一点,但 SDS 的 header 本来就只在创建和扩容时写几次,这点代价完全可以接受。

---

> **一句话点破:SDS 之于 `char*`,就是把"靠找零终止符才能读完的纸条",换成"开头写了页数、还留了空白页能往后续"的活页纸——对外它仍然是个 `char*`,什么 C 库函数都能直接喂,但前面那一小段 header 把长度、剩余空间、类型全记下了,从此取长度 O(1)、二进制安全、追加摊还 O(1)、按长度自动选型省内存。**

第一章我们看到客户端发来的命令字节流,经过 `processInlineBuffer`/`processMultibulkBuffer` 切分后,变成一个个 `robj`(Redis 对象),这些 `robj` 的 `ptr` 大多指向 SDS;第二章我们看到事件循环里的 `client->querybuf`、`client->buf` 也是 SDS。第三章的命令执行路径上,SET 的 value、HGET 的 field、LPUSH 的元素——全是 SDS。**SDS 是 Redis 整个数据世界的最小积木**。但它为什么是这个样子?为什么不直接用 `char*`?这一章我们把这块积木彻底拆开。

## 4.1 这块要解决什么:`char*` 的四个毛病

Redis 是个内存数据库。**它的数据库本身,就是内存里的一堆字符串**。一个 Redis 实例跑起来,几千万上亿个 key/value 都是字符串——粗略地说,Redis 跑业务时,99% 的热度路径上都在操作字符串。所以字符串这件事必须做得极度好。

C 语言的 `char*` 是个"裸"字符串:就是一段以 `'\0'` 结尾的字节流,什么元数据都不带。它有四个绕不开的毛病:

**毛病 1:取长度是 O(n)。** C 字符串不记长度,想知道它有多长,得从开头往后数,直到碰到 `'\0'`。`strlen()` 就这么实现的:

```c
/* C 标准库 strlen 的等价实现 */
size_t strlen(const char *s) {
    const char *p = s;
    while (*p) p++;      /* 一个字节一个字节地往后扫,直到 '\0' */
    return p - s;
}
```

对一个 1MB 的字符串反复取长度,每次都要扫一遍 1MB——这在数据库里完全不可接受(Redis 几乎每条命令都要知道 key 和 value 的长度)。

**毛病 2:不二进制安全。** `'\0'` 既是字符串内容的一部分(一张 JPEG、一段 protobuf 序列化数据里到处是 0 字节),又是 C 字符串的"结束标记"。二者冲突。只要数据里出现一个 `0x00`,`strlen`、`strcpy`、`strcmp` 全部在那个位置截断。Redis 要存的二进制数据(序列化后的值、协议帧、用户上传的图片缩略图、二进制 hash 字段)直接被腰斩。

**毛病 3:每次改长度都要 realloc。** C 字符串后面没有预留空间,你往里 `strcat` 一字节,底层的 `malloc/free` 就被调一次。频繁追加时,内存分配器成了瓶颈。

**毛病 4:容易溢出。** `strcat(s, t)` 不知道 `s` 后面还有多少地方,它只管往里写。写到别人地盘上,就是缓冲区溢出——这是无数 C 程序安全漏洞的根源。

Redis 要的是一个**记长度、能扛任意字节、能预分配、能感知剩余空间**的字符串。

> **不这样会怎样(反面:硬用 `char*`)**:如果 Redis 真的硬用 `char*`,那么 `STRLEN key` 这条命令本身就要 O(n)——客户端每发一次,主线程就要扫一遍整个 value。一个 1MB 的 value,客户端发一万次 STRLEN,主线程就要扫 10 GB 的字节。这还没完:存二进制值会丢数据(`'\0'` 截断),追加一个字节就要调一次 `realloc`,海量小追加能让分配器先崩。这四件事任挑一件,Redis 都活不下去。

所以 Redis 自己造了一个 SDS。源码就两个文件:[sds.h](../../redis-8.0.2/src/sds.h) 和 [sds.c](../../redis-8.0.2/src/sds.c)。一个看似不起眼的小库,撑起了整个 Redis 的字符串抽象。

## 4.2 一个 sds 其实是个 `char*`:header 藏在前面

第一件让你意外的事:[sds.h:21](../../redis-8.0.2/src/sds.h#L21) 把 `sds` 直接 typedef 成了 `char*`:

```c
/* sds.h:21 */
typedef char *sds;
```

也就是说,**你拿到的一个 `sds` 指针,从类型上看,跟一个 `char*` 没有任何区别**。你可以直接 `printf("%s", s)`、把它丢给 `strtok`、用 `strcmp` 跟一个 C 字符串比较——只要内容里没有中途的 `'\0'`。这就是 SDS 与 C 生态无缝兼容的关键:**对外的接口就是一个 `char*`**。

那"长度、剩余空间"这些元数据存在哪?存在这个 `char*` **前面**。SDS 的内存布局是这样的:

```text
┌─────────────────────────────────────────┐  ← sh(分配返回的真正起点)
│  len(已用) │ alloc(已分配) │ flags │   ←  header(sdshdr8 示例)
├─────────────────────────────────────────┤
│  b u f  . . .  用 户 数 据  . . .  \0   │  ←  buf[]
└─────────────────────────────────────────┘
                ^
                s = (char*)sh + hdrlen  ← sds 指针就指这里!
```

`buf` 是真正的字符串缓冲区,以 `'\0'` 结尾。`sds` 这个 `char*` 指向的是 `buf` 的起点,**而不是整个结构的起点**。这个设计的关键在于:

- **往前退一个字节,就是 `flags`**(类型标志位)——不管哪种 header,`flags` 永远紧贴 `buf` 前面。`sdslen()` 的入口就是 `unsigned char flags = s[-1]`,见 [sds.h:65-66](../../redis-8.0.2/src/sds.h#L65)。
- **往前退整个 header 那么多字节,就是完整的 header**,里面有 `len`(已用)和 `alloc`(不含头和终止符的已分配量)。

为什么不直接用 `struct sds { size_t len; size_t alloc; char *buf; }` + `s->buf` 访问?这种"独立结构体 + buf 指针"的设计有两个问题:一是每次访问数据多一次指针解引用(对缓存不友好);二是和现有 C 字符串生态完全断裂,所有 `printf("%s")`、`strcmp`、系统调用全得改。SDS 的"头在前面、对外暴露 buf"把这两件事都解决了——**它对 C 来说就是个 `char*`,只是带了私货**。

> **钉死这件事**:SDS 把 header 藏在 `buf` 前面、对外只暴露 `char*`,这一个设计同时换来了三件事——① 与整个 C 字符串生态(`printf`/`strcmp`/syscall)零摩擦兼容;② 取长度/取剩余空间是 O(1)(直接读 header 字段);③ 内存只有一次分配(header 和 buf 是同一块连续内存,cache 友好)。这套"伪装成 `char*`"的 trick 是 SDS 全部设计的总开关。

## 4.3 五种 header:按长度自动选型省内存

元数据本身也要占内存。如果固定用一个 `uint64_t` 存长度,那么存一个 3 字节的键 `"foo"` 就要搭上 8 字节记长度——浪费。Redis 的做法是**按字符串长度分五档**,每档用尽量窄的类型。看 [sds.h:25-52](../../redis-8.0.2/src/sds.h#L25):

```c
/* sds.h:25-52 */
struct __attribute__ ((__packed__)) sdshdr5 {
    unsigned char flags; /* 3 lsb of type, 5 msb of string length */
    char buf[];
};
struct __attribute__ ((__packed__)) sdshdr8 {
    uint8_t len;        /* used */
    uint8_t alloc;      /* excluding the header and null terminator */
    unsigned char flags; /* 3 lsb of type, 5 unused bits */
    char buf[];
};
struct __attribute__ ((__packed__)) sdshdr16 {
    uint16_t len;
    uint16_t alloc;
    unsigned char flags;
    char buf[];
};
/* sdshdr32 / sdshdr64 同构,只是把 uint16_t 换成 uint32_t / uint64_t */
```

五个类型常量定义在 [sds.h:54-58](../../redis-8.0.2/src/sds.h#L54):`SDS_TYPE_5=0`、`SDS_TYPE_8=1`、`SDS_TYPE_16=2`、`SDS_TYPE_32=3`、`SDS_TYPE_64=4`。各档能存的最大长度按 2 的幂递增,在 [sds.c:39-53](../../redis-8.0.2/src/sds.c#L39) 的 `sdsReqType` 里写得很直白:

```c
/* sds.c:39-53 */
static inline char sdsReqType(size_t string_size) {
    if (string_size < 1<<5)    return SDS_TYPE_5;   // < 32 字节
    if (string_size < 1<<8)    return SDS_TYPE_8;   // < 256 字节
    if (string_size < 1<<16)   return SDS_TYPE_16;  // < 65536 字节(64KB)
    if (string_size < 1ll<<32) return SDS_TYPE_32;  // < 4GB
    return SDS_TYPE_64;
}
```

各个 header 的实际大小如下(注意 `__packed__`,取消编译器对齐填充,下一节展开):

| 类型 | 最大长度 | header 字节数(packed 后) | 典型用途 |
|---|---|---|---|
| sdshdr5  | 31     | 1(只有 `flags`,长度塞进高 5 位) | 极少用(无 alloc,不能预分配) |
| sdshdr8  | 255    | 3(`len`1 + `alloc`1 + `flags`1) | **绝大多数 Redis 键名** |
| sdshdr16 | 65535  | 5 | 中等长度 value、协议帧 |
| sdshdr32 | 4G-1   | 9 | 大 value(bulk 字节流) |
| sdshdr64 | 2^64-1 | 17 | 几乎不用(单值 4GB 已远超常理) |

**这个分档省下的内存,在海量小键的场景下是真金白银**。Redis 里键名通常很短(几十字节以内),绝大多数命中 `sdshdr8`,header 只要 3 字节。如果统一用 17 字节的 `sdshdr64`,每个键多花 14 字节。一亿个键,就是 1.4 GB 纯粹的浪费——而**内存就是 Redis 的数据库本身**,这种浪费不可接受。

### 4.3.1 字段顺序的小心机:flags 为什么紧贴 buf

仔细看 [sds.h:29-34](../../redis-8.0.2/src/sds.h#L29) 的字段顺序:`len`、`alloc`、`flags`、`buf[]`。`flags` 紧贴 `buf`,而不是放在 header 开头。这不是随便写的。

> **钉死这件事**:`flags` 必须放在 header 的"靠近 buf 那一侧"。原因:从 `buf` 往前退 1 字节就一定是 `flags`,跟 header 类型无关。`sdslen`、`sdsavail`、`sdssetlen` 全部以 `s[-1]` 起步([sds.h:66](../../redis-8.0.2/src/sds.h#L66)、[sds.h:83](../../redis-8.0.2/src/sds.h#L83)、[sds.h:109](../../redis-8.0.2/src/sds.h#L109)),**先读 flags,再根据 flags 反查 header 大小**。如果 `flags` 放在 header 开头,你想读 flags 得先知道 header 多大,而 header 多大又取决于 flags——死循环。这个字段排布是 SDS 整个"伪装成 `char*`"把戏能成立的前提。

## 4.4 直球讲技巧①:`flags` 的位运算——3 位编码类型、5 位编码长度

这是 SDS 最紧凑的一段位操作,值得单独看清楚。`flags` 是一个 `unsigned char`,共 8 位。它的**低 3 位记录当前用的是哪种 header**——`SDS_TYPE_MASK = 7`([sds.h:59](../../redis-8.0.2/src/sds.h#L59))、`SDS_TYPE_BITS = 3`([sds.h:60](../../redis-8.0.2/src/sds.h#L60))。3 位能编码 8 种值,SDS 只用了 5 种(0~4),还留了 3 种余量。判断类型就是一次 AND:

```c
/* sds.h:66-67 —— sdslen() 入口 */
static inline size_t sdslen(const sds s) {
    unsigned char flags = s[-1];            /* buf 前一个字节就是 flags */
    switch(flags & SDS_TYPE_MASK) {         /* 低 3 位 → 类型 */
        case SDS_TYPE_5:  return SDS_TYPE_5_LEN(flags);
        case SDS_TYPE_8:  return SDS_HDR(8,s)->len;
        /* ...16/32/64 同理 */
    }
}
```

这一行 `s[-1] & SDS_TYPE_MASK` 在 SDS 库里反复出现,是整个抽象的入口。

`sdshdr5` 是个特殊设计:它**没有独立的 len 字段**,把字符串长度塞进 `flags` 的高 5 位(注意:是 `flags` 自己的高 5 位,不是另开一个字段)。位运算很紧凑:

- 写长度:`*fp = type | (initlen << SDS_TYPE_BITS);`,见 [sds.c:109](../../redis-8.0.2/src/sds.c#L109)(把长度左移 3 位塞进高 5 位,低 3 位拼上 type)。
- 读长度:`SDS_TYPE_5_LEN(f)` 就是 `(f)>>SDS_TYPE_BITS`,见 [sds.h:63](../../redis-8.0.2/src/sds.h#L63) 和 [sds.h:69](../../redis-8.0.2/src/sds.h#L69)(右移 3 位丢掉 type,剩下的就是长度)。

这样一个字节就装下了"类型 + 长度"两份信息,代价是 `sdshdr5` 只能表达 0~31 字节的字符串。

> **钉死这件事**:`sdshdr5` 用一个字节的低 3 位存 type、高 5 位存长度,把"元数据开销"压到 1 字节,这是 SDS 紧凑性的极限。但它的代价是没有 `alloc` 字段、记不住剩余空间,所以 Redis 几乎不用它(下节展开)。**一个设计承认自己的局限、并主动退化,比硬撑着用更可贵**——这是取向④"简单优先"在最小数据结构上的一次诚实自白。

## 4.5 直球讲技巧②:`__attribute__((packed))` 取消对齐——一个编译属性省下 5 字节

sdshdr8 的 header 字节数是 3(`len`1 + `alloc`1 + `flags`1)。但 C 编译器默认会给结构体做**对齐填充**(alignment padding):为了让 CPU 访问字段时一次就能读到(不跨缓存行、不跨字),编译器会按字段类型的对齐要求在字段之间塞空字节。如果不加 `__packed__`,sdshdr8 实际可能占 4 字节甚至更多。

看 [sds.h:29-34](../../redis-8.0.2/src/sds.h#L29) 上的编译属性:

```c
struct __attribute__ ((__packed__)) sdshdr8 {
    uint8_t len;
    uint8_t alloc;
    unsigned char flags;
    char buf[];
};
```

`__attribute__((packed))` 告诉 GCC/Clang:**这个结构体不要做任何对齐填充,字段一个挨一个紧密排布**。这样 sdshdr8 真的就只有 3 字节(1+1+1),sdshdr16 只有 5 字节(2+2+1)。

> **不这样会怎样(反面:不加 packed)**:如果不加 `__packed__`,编译器为了对齐会把 sdshdr8 后面塞 1 字节 padding,变成 4 字节;sdshdr16 会变成 6 字节(uint16_t 对齐到 2 字节边界,`flags` 后会补 1 字节);sdshdr32 会变成 12 字节(uint32_t 对齐到 4 字节)。**每个键多花 1~3 字节 padding,一亿个键就是 100~300 MB 的纯浪费**——而这本应是 Redis 数据库的内存。`packed` 这一个编译属性,在海量小键场景下,等于白送了几百 MB。

代价是什么?是**非对齐访问**。CPU 读一个没对齐的 `uint16_t` 可能要分两次读、或者走慢路径。但 SDS 的 header 字段(`len`/`alloc`)只在创建和扩容时写几次,绝大多数热度路径上读的是 `sdslen()` 的返回值(存在寄存器里)。**省下来的对齐 padding,远比对齐访问省的那一两个时钟周期值钱**。

```text
不加 packed(sdshdr8 假设默认对齐到 uint8_t,实际 padding 在更宽的类型上更明显):
┌─────┬─────┬─────┬─────┐
│ len │ alloc│flags│ pad │   ← 4 字节,padding 浪费 1 字节
└─────┴─────┴─────┴─────┘

加 packed(sdshdr8):
┌─────┬─────┬─────┐
│ len │ alloc│flags│        ← 3 字节,紧密相邻,无 padding
└─────┴─────┴─────┘
```

> **钉死这件事**:`__packed__` 取消结构体对齐,是 SDS 内存紧凑性的关键。它用"非对齐访问慢一点"这个**几乎察觉不到**的代价(CPU 访问 header 字段的频率远低于访问 buf),换来了 sdshdr8 省 1 字节、sdshdr16 省 1 字节、sdshdr32 省 3 字节的真金白银。在海量小键场景下,这是取向②"内存即数据库"在数据结构布局层的硬核兑现。

## 4.6 O(1) 取长度 + 二进制安全:就靠 len 字段

`sdshdr8/16/32/64` 都有一个 `len` 字段。`sdslen()` 直接读它,见 [sds.h:65-80](../../redis-8.0.2/src/sds.h#L65)。**取长度是 O(1)**——这是 SDS 解决 `char*` 痛点 1 的根本。`STRLEN key` 这条命令,无论 value 是 10 字节还是 10 MB,都是一次寄存器读。

二进制安全,是同样的逻辑:**所有判断边界、判断长度的代码,一律看 `len`,不看 `'\0'`**。看 `_sdsnewlen` 怎么填充内容,见 [sds.c:141-143](../../redis-8.0.2/src/sds.c#L141):

```c
/* sds.c:141-143 */
if (initlen && init)
    memcpy(s, init, initlen);   /* 按长度拷贝,内容里有 '\0' 也照搬 */
s[initlen] = '\0';              /* 末尾仍补一个 '\0',兼容 printf("%s") */
```

这里有个很巧的设计取舍:**末尾的 `'\0'` 仍然保留**,但它只是个"装饰"——让 sds 能当 `char*` 用、能传给所有 C 字符串函数。逻辑长度完全由 `len` 决定。所以一个内容是 `"foo\0bar\0baz"` 的 sds,`sdslen()` 返回 11,但 `printf("%s", s)` 只会打印 `foo`。**两套语义并存,各司其职**。源码注释说得很清楚:"the string is always null-terminated... However the string is binary safe and can contain `\0` characters in the middle, as the length is stored in the sds header",见 [sds.c:74-81](../../redis-8.0.2/src/sds.c#L74)。

> **不这样会怎样(反面:用 `'\0'` 判尾)**:如果 SDS 也用 `'\0'` 判长度,那么用户存一个 `"GET\0x00\0y00"`(假设这是个二进制协议帧)就会被截断成 `"GET"`——`STRLEN` 返回 3,`GET` 命令拿到的 value 也是 3 字节,数据直接丢。Redis 要做内存数据库,就**必须**能存任意字节流,包括 0 字节。这个硬约束逼出了"`len` 字段 + 末尾仍补 `\0`"这套两全方案——逻辑长度看 `len`,字符串兼容看 `\0`,两者不打架。

## 4.7 直球讲技巧③:预分配摊还——sdsMakeRoomFor 为什么翻倍

`char*` 改一次长度要 realloc 一次。如果你以"每次追加 1 字节"的方式从空串长到 1MB,用 `char*` 就要调 100 万次 realloc——O(N) 次系统调用。SDS 的核心优化是 `sdsMakeRoomFor`,见 [sds.c:223-274](../../redis-8.0.2/src/sds.c#L223):

```c
/* sds.c:223-274,精简 */
sds _sdsMakeRoomFor(sds s, size_t addlen, int greedy) {
    size_t avail = sdsavail(s);
    if (avail >= addlen) return s;          /* 剩余空间够,直接返回,不分配 */

    len = sdslen(s);
    reqlen = newlen = (len+addlen);
    if (greedy == 1) {
        if (newlen < SDS_MAX_PREALLOC)      /* sds.h:14: #define SDS_MAX_PREALLOC (1024*1024) */
            newlen *= 2;                    /* 长度 < 1MB:直接翻倍 */
        else
            newlen += SDS_MAX_PREALLOC;     /* 长度 >= 1MB:每次只多分 1MB */
    }
    type = sdsReqType(newlen);
    if (type == SDS_TYPE_5) type = SDS_TYPE_8;  /* 强制不用 type 5 */
    /* ...根据 type 是否变化,走 realloc 或 malloc+memcpy+free... */
    sdssetalloc(s, usable);                 /* 更新 alloc,但不动 len */
}
```

扩容策略是 SDS 最常被引用的一段:

- **`len + addlen < 1MB` 时,新空间直接翻倍。**
- **`len + addlen >= 1MB` 时,每次只多分 1MB。**

为什么翻倍能摊还 O(1)?这是一道经典的几何级数摊还分析题,值得算一遍。

### 摊还分析:翻倍为什么是 O(1) 均摊

假设你以"每次追加 1 字节"的方式,从一个空 SDS 长到 N 字节(N 是 2 的幂,比如 1024)。每次追加前都要先 `sdsMakeRoomFor` 保证有空间。

**反面:每次只多分 1 字节(不预分配)**

每次追加 1 字节,buffer 刚好满,必须 realloc 到 +1。第 k 次追加要 realloc,总 realloc 次数 = N。每次 realloc 都要 memcpy 当前内容(平均长度 N/2),所以**总拷贝字节数** = `1 + 2 + 3 + ... + N` = O(N²)。**每次追加均摊 O(N) 字节拷贝**——这是 `char*` 反复 `strcat` 的真实代价。

**正面:翻倍预分配**

每次满了就翻倍。capacity 序列是 `1, 2, 4, 8, ..., N`。每次翻倍时,要把旧内容 memcpy 到新 buffer。总拷贝字节数 = `1 + 2 + 4 + ... + N/2 + N` < `2N`。**N 次追加总共拷贝不到 2N 字节,每次追加均摊 O(1) 字节拷贝**。

差别是 N² vs N——当 N=1MB=1048576 时,反面要拷约 5×10¹¹ 字节,正面只拷约 2×10⁶ 字节,**差了 25 万倍**。这就是翻倍预分配的威力。

### 为什么是 1MB 这个分界:翻倍的代价在 MB 级反超

翻倍策略在小字符串上压倒性划算,但到 MB 级,账变了。考虑你现在有一个 4MB 的字符串,要再追加 1 字节。翻倍策略会让你 realloc 到 8MB——**多出来 4MB 全是预留**,内存峰值直接翻一番。如果你的 Redis 实例本来就用满了内存,这一下可能触发淘汰(key eviction)或者 OOM。

`SDS_MAX_PREALLOC = 1024*1024`(1MB,[sds.h:14](../../redis-8.0.2/src/sds.h#L14))就是这条分界线。1MB 以上改成"每次只多分 1MB",牺牲一点 realloc 次数(从 O(log N) 退化到 O(N/1MB)),换内存峰值可控。这个数不是数学推导出来的,是 Redis 作者在实践中调出来的——**"小段激进、大段保守"**的曲线,在 Redis 各处都能看到(dict 的 rehash 步进、listpack→quicklist 的转换阈值都是同款哲学)。

> **钉死这件事**:翻倍策略是经典摊还 O(1),它把"逐字节追加到 N"的总拷贝字节从 O(N²) 压到 O(N)。但翻倍的代价是"内存峰值翻番",在 MB 级这个代价开始反超收益,所以 SDS 在 1MB 处切换成"加 1MB"。**这条 1MB 分界不是性能优化,而是内存峰值治理**——取向②"内存即数据库"下,峰值翻番是不可接受的。

### 一个细节:greedy 参数

`sdsMakeRoomFor` 默认贪婪(多预留),`sdsMakeRoomForNonGreedy` 则只分到刚刚够,见 [sds.c:278-285](../../redis-8.0.2/src/sds.c#L278):

```c
/* sds.c:278-285 */
sds sdsMakeRoomFor(sds s, size_t addlen) {
    return _sdsMakeRoomFor(s, addlen, 1);              /* greedy=1:翻倍 */
}
sds sdsMakeRoomForNonGreedy(sds s, size_t addlen) {
    return _sdsMakeRoomFor(s, addlen, 0);              /* greedy=0:刚刚够 */
}
```

调用方知道"这次写完基本不会再长"时(比如一次性的 bulk 响应),可以走非贪婪版,省内存。这是把"是否预分配"这个决策权交还给调用方——**SDS 不替你做所有决定,只给你两套现成的策略**。

### 惰性释放:sdsclear 不 free

对应的还有**惰性释放**:`sdsclear`([sds.c:206-209](../../redis-8.0.2/src/sds.c#L206))把 `len` 置 0、把 `buf[0]` 置 `'\0'`,但**完全不释放底层内存**:

```c
/* sds.c:206-209 */
void sdsclear(sds s) {
    sdssetlen(s, 0);
    s[0] = '\0';
}
```

之前预分配的空间原封不动留着,`sdsavail()` 立刻就大。下次再追加,`sdsMakeRoomFor` 一看 `avail >= addlen`,直接返回不分配。这种"先留着,可能有用"的策略,对反复清空又重填的缓冲区(比如 `client->querybuf` 在每条命令处理后清空、又会被下一条命令填满)极其友好——**省下的不是几次 malloc,而是几次 malloc 触发的内存碎片**。

## 4.8 直球讲技巧④:`sdshdr5` 几乎不用——一个设计的自我退化

源码注释 [sds.h:23-24](../../redis-8.0.2/src/sds.h#L23) 明说:`sdshdr5` "is never used, we just access the flags byte directly. However is here to document the layout of type 5 SDS strings"——意思是 `sdshdr5` 在 Redis 内部默认不创建。原因是它**没有 `alloc` 字段,记不住剩余空间**,任何追加操作都得调 `sdsMakeRoomFor`,而 `sdsMakeRoomFor` 又得 realloc。

所以源码两处主动把它升级成 `sdshdr8`:

- **创建空串时**:`if (type == SDS_TYPE_5 && initlen == 0) type = SDS_TYPE_8;`,见 [sds.c:88](../../redis-8.0.2/src/sds.c#L88)。注释直说:"Empty strings are usually created in order to append. Use type 8 since type 5 is not good at this."
- **扩容时**:`if (type == SDS_TYPE_5) type = SDS_TYPE_8;`,见 [sds.c:250](../../redis-8.0.2/src/sds.c#L250)。注释:"Don't use type 5: the user is appending to the string and type 5 is not able to remember empty space, so sdsMakeRoomFor() must be called at every appending operation."

> **钉死这件事**:`sdshdr5` 在 SDS 里更多是"理论上存在"——它省到极致(1 字节头),但代价是记不住剩余空间、不能预分配。源码两处主动把它退化成 `sdshdr8`。**一个设计承认自己的局限、并在会出问题的地方主动降级,比硬撑着用更可贵**。`sdshdr5` 留在源码里,作用是"文档化 type 5 的字节布局"——给读者看,不给运行时用。

## 4.9 直球讲技巧⑤:`SDS_NOINIT`——跳过 memset 省一次清零

`_sdsnewlen` 里有一段关于 `memset` 的取舍,见 [sds.c:98-101](../../redis-8.0.2/src/sds.c#L98):

```c
/* sds.c:98-101 */
if (init==SDS_NOINIT)
    init = NULL;                              /* 跳过 memset,buffer 留未初始化 */
else if (!init)
    memset(sh, 0, hdrlen+initlen+1);          /* 默认:全清零 */
```

`SDS_NOINIT` 是个特殊的标记指针,定义在 [sds.c:21](../../redis-8.0.2/src/sds.c#L21):`const char *SDS_NOINIT = "SDS_NOINIT";`,声明在 [sds.h:15](../../redis-8.0.2/src/sds.h#L15)。调用方传 `SDS_NOINIT` 当 `init` 参数,意思是"这块 buffer 我马上会自己填满,你别帮我 memset 清零了"。

> **不这样会怎样(反面:每次都 memset)**:假设不提供 `SDS_NOINIT`,每次创建 SDS 都强制 memset。考虑网络层:客户端发来一个 1MB 的 bulk value,`querybuf` 要扩容到 1MB。`sdsMakeRoomFor` 调 `s_malloc_usable` 分配 1MB,如果默认 memset,就要把 1MB 全清零——`memset` 一字节能跑几个 GB/s,1MB 大约几百微秒。**紧接着下一行 `memcpy` 又把这 1MB 全填上客户端发来的真实数据**——刚才那次 memset 完全是白干。一次无谓的 1MB memset,在每秒几万次 bulk 的负载下,能多吃掉几个百分点的 CPU。

`SDS_NOINIT` 让调用方声明"这块 buffer 我马上会写满",分配器跳过清零,直接进入下一步的 `memcpy`。**只在"buffer 会被部分使用、剩下部分必须可预测"的场景才需要 memset**(比如后续会按 len 边界读取、不读越界就行,那部分留未初始化字节也无害)。

> **钉死这件事**:`SDS_NOINIT` 是把"是否清零"这个决策权交还给调用方的又一个例子。默认清零(安全、可预测),但允许调用方声明"我马上填满"来跳过这次 memset——在网络大 bulk、RDB 加载这种"分配完立刻 memcpy"的场景下省一次清零。**这是取向①"把耗时从主线程解放"在分配层的一个小切口**——不是大改,而是一个标记指针 + 一个 if 分支,省下的却是高频路径上的常数开销。

## 4.10 分配器抽象层:`sdsalloc.h`——为什么 Redis 自己包一层

SDS 的所有分配调用都走一层薄抽象,见 [sdsalloc.h:22-31](../../redis/8.0.2/src/sdsalloc.h):

```c
/* sdsalloc.h:22-31 */
#define s_malloc zmalloc
#define s_realloc zrealloc
#define s_trymalloc ztrymalloc
#define s_tryrealloc ztryrealloc
#define s_free zfree
#define s_malloc_usable zmalloc_usable
#define s_realloc_usable zrealloc_usable
#define s_trymalloc_usable ztrymalloc_usable
#define s_tryrealloc_usable ztryrealloc_usable
#define s_free_usable zfree_usable
```

`zmalloc` 是 Redis 自己的分配器封装(`zmalloc.h`/`zmalloc.c`),它在编译期可选地挂到 **jemalloc、tcmalloc、libc malloc** 之一(默认 Linux 用 jemalloc,MacOS 用系统 malloc)。SDS 不直接调 `malloc/free`,而是通过这一层宏,等于"借用"了 Redis 已经选好的分配器。

为什么要包这一层?两个原因:

**① 拿到 `usable`(分配器实际给了多少)。** 现代 malloc 实现(jemalloc/tcmalloc)按 size class 分配,你要 100 字节它给你 112 字节(对齐到 size class 边界)。`zmalloc_usable` 通过 `je_malloc_usable_size` 之类的接口,把这个"白送"的字节数告诉调用方。SDS 在 `_sdsnewlen` 里把这部分收进 `alloc` 字段([sds.c:104-106](../../redis-8.0.2/src/sds.c#L104)),**不浪费一个字节**:

```c
/* sds.c:104-106 */
usable = usable-hdrlen-1;
if (usable > sdsTypeMaxSize(type))
    usable = sdsTypeMaxSize(type);
/* 然后 sh->alloc = usable; */
```

**② 内存统计。** `zmalloc` 在每次分配时累加 `used_memory`,Redis 的 `INFO memory`、淘汰策略(`maxmemory`)都靠这个计数。SDS 走 `zmalloc` = 自动被统计进 Redis 的内存账本。如果 SDS 直接调 libc `malloc`,Redis 就不知道自己用了多少内存,淘汰策略失效。

> **钉死这件事**:SDS 不直接调 `malloc`,而是走 `sdsalloc.h` → `zmalloc`。这一层抽象换来了两件事:① 拿到分配器白送的 `usable` 字节,收进 `alloc` 不浪费;② 自动被统计进 Redis 的 `used_memory`,淘汰策略才能工作。**SDS 不是孤立的字符串库,它和 Redis 的内存治理是绑死的**——这是取向②"内存即数据库"在分配器层的体现。

## 4.11 sdsnewlen:创建一条 sds 的全流程

把上面几条串起来,看 `_sdsnewlen` 整体怎么工作,见 [sds.c:82-145](../../redis-8.0.2/src/sds.c#L82):

```c
/* sds.c:82-145,精简 */
sds _sdsnewlen(const void *init, size_t initlen, int trymalloc) {
    char type = sdsReqType(initlen);        /* 按长度挑最省的 header */
    if (type == SDS_TYPE_5 && initlen == 0) type = SDS_TYPE_8;  /* 空串升级到 8 */
    int hdrlen = sdsHdrSize(type);
    size_t usable;

    sh = trymalloc ? s_trymalloc_usable(hdrlen+initlen+1, &usable)
                   : s_malloc_usable(hdrlen+initlen+1, &usable);  /* 一次分配头+数据+终止符 */
    if (init==SDS_NOINIT)
        init = NULL;                          /* 跳过 memset */
    else if (!init)
        memset(sh, 0, hdrlen+initlen+1);
    s = (char*)sh + hdrlen;                   /* sds 指向 buf 起点,前面就是 header */
    fp = ((unsigned char*)s) - 1;             /* flags 永远在 buf 前一个字节 */
    usable = usable - hdrlen - 1;
    if (usable > sdsTypeMaxSize(type)) usable = sdsTypeMaxSize(type);
    /* ...按 type 填 flags / len / alloc... */
    if (initlen && init) memcpy(s, init, initlen);
    s[initlen] = '\0';                        /* 末尾补 '\0',兼容 printf */
    return s;
}
```

几个值得记住的细节:

- **`hdrlen+initlen+1` 一次性分配**,这个 `+1` 就是末尾的 `'\0'`。SDS 永远以 `'\0'` 结尾,哪怕内容是二进制。
- 用 `s_malloc_usable`(底层走 `zmalloc_usable` → jemalloc),分配器往往给得比你要的多,`usable` 把这部分"白送"的空间收进 `alloc`,**不浪费一个字节**。([sds.c:104-106](../../redis-8.0.2/src/sds.c#L104))
- `s = (char*)sh + hdrlen`——这就是 SDS 与 `char*` 兼容的全部秘密:对外返回的不是结构体指针,而是 buf 指针。**任何人拿到一个 sds,都可以当 `char*` 用**。

## 4.12 直球讲技巧⑥:embstr vs raw vs int——44 字节阈值是怎么算出来的

SDS 本身是裸字符串。但 Redis 的字符串值是包在 `robj`(Redis 对象)里的。`robj` 有三种字符串编码:

- **OBJ_ENCODING_RAW**:`robj` 和 `sds` 是**两次独立分配**。`robj->ptr` 指向一块单独的 sds 内存。
- **OBJ_ENCODING_EMBSTR**:`robj`、`sdshdr8`、`buf` 三件塞进**一次 `zmalloc`**,内存连续。
- **OBJ_ENCODING_INT**:值是数字时,直接把数字存进 `ptr`(指针位置塞 long long),不用 sds。

EMBSTR 是 SDS 设计上最巧妙的延伸。看 [object.c:72-94](../../redis-8.0.2/src/object.c#L72):

```c
/* object.c:72-94 */
robj *createEmbeddedStringObject(const char *ptr, size_t len) {
    robj *o = zmalloc(sizeof(robj)+sizeof(struct sdshdr8)+len+1);
    struct sdshdr8 *sh = (void*)(o+1);          /* sds 紧跟 robj 后面 */

    o->type = OBJ_STRING;
    o->encoding = OBJ_ENCODING_EMBSTR;
    o->ptr = sh+1;                              /* ptr 指向 buf(sdshdr8 后面) */
    o->refcount = 1;
    o->lru = 0;

    sh->len = len;
    sh->alloc = len;
    sh->flags = SDS_TYPE_8;
    if (ptr == SDS_NOINIT)
        sh->buf[len] = '\0';
    else if (ptr) {
        memcpy(sh->buf, ptr, len);
        sh->buf[len] = '\0';
    }
    return o;
}
```

阈值在 [object.c:102-108](../../redis-8.0.2/src/object.c#L102):

```c
/* object.c:96-108 */
/* The current limit of 44 is chosen so that the biggest string object
 * we allocate as EMBSTR will still fit into the 64 byte arena of jemalloc. */
#define OBJ_ENCODING_EMBSTR_SIZE_LIMIT 44
robj *createStringObject(const char *ptr, size_t len) {
    if (len <= OBJ_ENCODING_EMBSTR_SIZE_LIMIT)
        return createEmbeddedStringObject(ptr, len);
    else
        return createRawStringObject(ptr, len);
}
```

注释把答案直接写出来了:**44 字节是为了让整个分配恰好落进 jemalloc 的 64 字节 arena**。这笔账必须算清,因为它是 SDS 分档设计与 Redis 对象布局的一次联手:

### 算账:robj + sdshdr8 + buf + '\0' = 64

`robj` 结构体定义在 [server.h:1001-1009](../../redis-8.0.2/src/server.h#L1001):

```c
/* server.h:1001-1009 */
struct redisObject {
    unsigned type:4;
    unsigned encoding:4;
    unsigned lru:LRU_BITS;     /* LRU_BITS = 24,见 server.h:994 */
    int refcount;
    void *ptr;
};
```

字段大小:`type`(4 bit)+ `encoding`(4 bit)+ `lru`(24 bit)合起来 32 bit = 4 字节;`refcount`(int)= 4 字节;`ptr`(指针,64 位系统)= 8 字节。**`robj` = 4 + 4 + 8 = 16 字节**。

`sdshdr8`(packed)= 3 字节。buf = `len` 字节。终止符 `'\0'` = 1 字节。总分配 = `16 + 3 + len + 1` = `20 + len`。

让总分配恰好 = 64(填满 jemalloc 64-byte arena,不浪费一个字节):`20 + len = 64` → **len = 44**。这就是 `OBJ_ENCODING_EMBSTR_SIZE_LIMIT = 44` 的来历。

> **钉死这件事**:`OBJ_ENCODING_EMBSTR_SIZE_LIMIT = 44` 不是拍脑袋的数,而是 `robj(16) + sdshdr8(3) + buf(len) + '\0'(1) = 64`(jemalloc 64 字节 arena)反推出来的。EMBSTR 把三件东西塞进一次连续分配,有两个好处:① **一次 malloc 而不是两次**(RAW 是 robj 和 sds 各一次),省一次系统调用 + 减少内存碎片;② **robj、sdshdr8、buf 在同一缓存行**(64 字节恰好一个 cache line),顺序访问时 cache 友好。**EMBSTR 是 SDS 分档设计(`sdshdr8` 紧凑 3 字节头)和 Redis 对象布局(robj 16 字节)的一次联手——任何一个设计改了,这个 44 都得重算**。

```text
EMBSTR 内存布局(一次 zmalloc,连续 64 字节):
┌──────── robj (16B) ────────┬─ sdshdr8 (3B) ──┬── buf (≤44B) ──┬─ \0 ─┐
│ type/enc/lru │ refcount │ ptr ──→  │len│alloc│flags│ 用户数据… │  0  │
│   4B    │   4B    │  8B   │         │ 1 │  1  │  1  │ ≤44 字节  │ 1B  │
└──────────────────────────┴──────────┴─────┴─────┴────────────┴─────┘
                            ↑
                            ptr 指向 sdshdr8 的 buf 字段(sdshdr8 + 1 = buf)
                            (整个分配恰好填满 jemalloc 的 64-byte arena)

RAW 内存布局(两次 zmalloc):
┌── robj (16B) ──┐         另一块堆内存:
│ ... │ ptr ──────┼────→    ┌─ sdshdr8/16/... ─┬── buf ──┬─ \0 ─┐
└────────────────┘         │len│alloc│flags   │ 用户数据  │  0   │
                           └──────────────────┴──────────┴──────┘
两次 malloc + 两次缓存行跳跃(robj 和 sds 可能相距很远,cache 不友好)
```

## 4.13 `sdshdr5` 的位运算 vs `sdshdr8` 的字段访问:一个反面对比

把 `sdshdr5` 和 `sdshdr8` 放在一起看,能更清楚 SDS 的设计权衡。

**`sdshdr5` 读长度**(1 字节 header,位运算):

```c
/* sds.h:69 */
case SDS_TYPE_5:
    return SDS_TYPE_5_LEN(flags);   /* 即 (flags) >> 3,一次移位 */
```

**`sdshdr8` 读长度**(3 字节 header,字段访问):

```c
/* sds.h:70-71 */
case SDS_TYPE_8:
    return SDS_HDR(8,s)->len;       /* 即 ((sdshdr8*)((s)-3))->len,一次解引用 */
```

`sdshdr5` 看起来更紧凑(1 字节 vs 3 字节)、读长度更快(移位 vs 解引用),但它没有 `alloc` 字段。这意味着:

- `sdsavail(s)` 对 `sdshdr5` 永远返回 0([sds.h:85-87](../../redis-8.0.2/src/sds.h#L85))——因为它根本不知道分配了多少。
- 任何 `sdscat`/`sdscatlen` 操作,`sdshdr5` 必须走 realloc;`sdshdr8` 在 `alloc - len >= addlen` 时直接返回,零分配。

> **不这样会怎样(反面:全用 `sdshdr5`)**:假设 Redis 全用 `sdshdr5`,那么每次追加 1 字节都要 realloc。前面 4.7 节的摊还分析就作废了——预分配摊还 O(1) 的前提是有 `alloc` 字段记剩余空间,`sdshdr5` 没有。Redis 的网络缓冲(`querybuf`)、AOF 缓冲、复制 backlog,这些"反复追加"的场景全要崩。**省下的 2 字节 header,换来的是每次追加的 malloc——这是最典型的局部最优陷阱**。所以 Redis 只把 `sdshdr5` 留作"理论上存在",实际创建一律退化到 `sdshdr8`。

## 4.14 和 `std::string` 比,SDS 牺牲了什么、换来了什么

`std::string` 内部记 length、capacity,逻辑上跟 SDS 很像,而且有 SSO(Small String Optimization):短串把数据直接塞进对象本身,连堆都不上。但 `std::string` 是 C++ 对象,有拷贝/移动/析构的复杂语义,生命周期管理重。

SDS 是裸 `char*`,谁拿指针谁负责,**没有所有权概念**——这恰恰是 C 风格 API 的特点,简单、轻、跟所有 C 代码兼容。SDS 把"复杂"留给了人(调用方必须管 free),把"简单"给了代码路径(一次 memcpy、一次指针运算)。在 Redis 这种单线程、调用边界清晰的场景下,这种取舍很合算。

## 4.15 SDS 怎么进 Redis 对象

回到全局视角:Redis 对象 `robj` 的 `ptr` 字段就指向一个 sds。最直接的看法是 [object.c:65-67](../../redis-8.0.2/src/object.c#L65) 的 `createRawStringObject`:

```c
/* object.c:65-67 */
robj *createRawStringObject(const char *ptr, size_t len) {
    return createObject(OBJ_STRING, sdsnewlen(ptr, len));  /* o->ptr = sds */
}
```

`o->ptr` 就是一个 sds,也就是一个 `char*`。从这里你能看出 SDS 的"伪装"有多彻底:Redis 内部传指针、API 传指针,全都是 `char*`,只有在需要知道长度、需要扩容时,才往回退几字节找 header。SDS 是 Redis 数据世界的最小积木——而它对外的样子,就是 C 程序员最熟悉的 `char*`。

## 章末:主线回扣、五个为什么、往哪钻

### 主线回扣

这一章是**取向②(内存即数据库)和取向④(简单优先)在最小数据结构上的一次联手兑现**:

- **取向②(内存即数据库)**:Redis 的数据库就是内存里的一堆字符串。所以每一字节都得抠——五档 header 按 2 的幂分,**短串用 1~3 字节头,长串才上 17 字节头**;`__packed__` 取消对齐 padding,sdshdr8 真的只有 3 字节;分配器多给的 `usable` 空间收进 `alloc` 不浪费;EMBSTR 把 robj + sdshdr8 + buf 拼成一次分配,恰好填满 jemalloc 64 字节 arena;`SDS_NOINIT` 跳过 memset 省一次清零。**所有这些技巧,本质都是"为 Redis 特有的访问模式(海量小键、频繁追加)定制数据结构,换性能和内存"**。

- **取向④(简单优先)**:SDS 没搞复杂的引用计数、没搞写时复制、没搞多层 allocator 抽象(只有 `sdsalloc.h` 一层薄薄的宏)。它就是"`char*` 前面加个 header",能用一次 `memcpy` 就不用两次,能用一个位运算(`s[-1] & SDS_TYPE_MASK`)就不用一个 hash 查找,能用 `__packed__` 一个编译属性就不用手写字段排布。**复杂度都被刻意按住了,留在必须的地方**。

### 五个为什么

**Q1:为什么 SDS 要保留末尾的 `'\0'`,既然它已经用 `len` 判长度了?**

为了和整个 C 字符串生态无缝兼容。Redis 内部很多地方会把 sds 当 `char*` 用:日志格式化(`sdscatprintf`)、协议错误消息、和一些遗留的 `strcmp` 调用。如果去掉 `'\0'`,这些地方全要改成 SDS 专用函数,代码膨胀。**保留 `'\0'` 是 1 字节的代价,换来的是"任意时刻都能把 sds 当 `char*` 用"的便利**。而且 SDS 永远以 `'\0'` 结尾,但 `'\0'` 不计入 `len`——所以 `sdslen()` 报告的长度永远是真实数据长度,`'\0'` 只是尾巴上的"装饰",两者不打架。

**Q2:`sdshdr5` 既然几乎不用,为什么不直接删掉?**

两个原因。① 它**文档化了 type 5 的字节布局**——源码注释 [sds.h:23-24](../../redis-8.0.2/src/sds.h#L23) 明说"is here to document the layout of type 5 SDS strings"。读到这段代码的人能立刻明白"低 3 位 type、高 5 位长度"是什么样子,而不是只看一个宏 `SDS_TYPE_5_LEN(f) = (f)>>3`。② 它**保留了"未来某天可能用"的余地**。如果有一天 Redis 出现"只读、永不追加、极短"的字符串场景(比如某些静态键名),`sdshdr5` 就能上场,1 字节头。**留着不等于现在用,代码里有些设计是为未来的可能性留的口子**。

**Q3:翻倍预分配到 1MB 后改成"加 1MB",为什么是 1MB 而不是 512KB 或 2MB?**

这是个经验值,不是数学推导。`SDS_MAX_PREALLOC = 1024*1024` 是 Redis 作者在实践中调出来的。1MB 大约是"再翻一次(到 2MB)内存峰值就难以承受"的心理门槛。设太小(比如 64KB),大字符串的 realloc 次数会显著上升;设太大(比如 16MB),预分配的内存碎片会很严重。1MB 是个折中。**这种"小段激进、大段保守"的曲线,在 Redis 各处都能看到(dict 的 rehash 步进、listpack→quicklist 的转换阈值),是一条贯穿全局的工程哲学**。

**Q4:EMBSTR 为什么不可修改?修改了会怎样?**

`createEmbeddedStringObject` 创建的对象,encoding 标记为 `OBJ_ENCODING_EMBSTR`。这个对象是"只读"的——任何修改操作(`APPEND`、`SETRANGE`、`INCR` 把数字字符串变长)都会先把它转成 RAW 编码(`createStringObject` → `createRawStringObject`,两次独立分配),再修改。为什么?因为 EMBSTR 是一次连续分配,改它的内容意味着可能要扩容,扩容意味着 realloc,realloc 可能搬家——但搬家后 `robj` 和 `sds` 还得在一起,这就破坏了 EMBSTR 的"连续"语义。**所以 Redis 干脆规定:EMBSTR 不可修改,要改就先转 RAW**。这是把"连续分配的 cache 友好性"和"可修改性"做了二选一——绝大多数字符串值创建后只读,EMBSTR 命中率很高。

**Q5:`SDS_NOINIT` 跳过 memset,会不会读到旧数据(信息泄露)?**

不会读到"别人的"旧数据——`zmalloc` 拿到的内存是分配器给的,jemalloc 在 free 时通常不会把内容清零(为了性能),所以这块内存里可能有**上一个使用者**留下的字节。但 SDS 在 `_sdsnewlen` 里的逻辑是:`SDS_NOINIT` 跳过 memset,但**紧接着**`if (initlen && init) memcpy(s, init, initlen)` 会把 `initlen` 字节填上真实数据,然后 `s[initlen] = '\0'`。所以 buf 的 `[0, initlen)` 范围是真实数据,`[initlen]` 是 `'\0'`。**调用方传 `SDS_NOINIT` 时,自己负责保证"我马上会把 buf 写满到 initlen"**。至于 `initlen` 之后到 `alloc` 之间的空间(预分配的 `avail`),本来就是"未定义内容",调用方只能通过 `sdslen()` 访问 `[0, len)`,不会读到 `avail` 部分。所以没有信息泄露路径。

### 想继续深入往哪钻

- 想看 SDS 的全部 API(几十个 `sds*` 函数):读 [sds.h](../../redis-8.0.2/src/sds.h) 的全部声明 + [sds.c](../../redis-8.0.2/src/sds.c) 的实现。重点看 `sdscatprintf`([sds.c:527](../../redis-8.0.2/src/sds.c#L527),它用了 `vsnprintf` 两次的 trick 来处理变长格式化)、`sdssplitargs`([sds.c:991](../../redis-8.0.2/src/sds.c#L991),Redis 协议解析的底层)。
- 想看 jemalloc 的 size class 怎么定:读 jemalloc 源码的 `size_classes.h`(Redis 源码树里 deps/jemalloc/),能看到 8/16/32/48/64/80/96/...这一串 size class 边界。**64 字节 arena 的存在,正是 `OBJ_ENCODING_EMBSTR_SIZE_LIMIT = 44` 能算出来的前提**。
- 想看 RAW 编码什么时候触发:读 [object.c](../../redis-8.0.2/src/object.c) 的 `setCommand` → `setKey` → 可能触发 `decrRefCount` + 重新 `createStringObject` 的路径。`APPEND`/`SETRANGE` 命令里也有 EMBSTR→RAW 的转换。
- 想看 `SDS_NOINIT` 在网络层的实际使用:读 [networking.c](../../redis-8.0.2/src/networking.c) 里 `querybuf` 扩容的调用点,大 bulk 接收时会传 `SDS_NOINIT`。

### 引出下一章

SDS 给 Redis 铺好了第一块砖——一个又快又省又安全的字符串。但 Redis 的数据库不是一堆散落的字符串,而是**按 key 找 value**。一个 Redis 实例动辄上亿个 key,你怎么用一个 SDS 把它找出来?线性扫描肯定不行——你需要一个能 O(1) 平均、能扛海量键、能在 rehash 时不阻塞主线程的哈希表。

**那就是下一章的主角:`dict`。** 而 dict 的 key 和 value,几乎全是 SDS——SDS 是 dict 的最小积木。dict 解决的是"如何在内存里高效地按名字找东西",它会把本章讲到的"二进制安全"作为 key 比较的基石(`dictSdsKeyCompare` 直接用 `memcmp` 比较 SDS 内容),也会在 rehash 时复用 SDS 的 `sdsdup`、在扩容时复用 `sdsMakeRoomFor` 的同款"渐进式"哲学。SDS 给 Redis 铺好了第一块砖,dict 在这块砖上盖起了第一栋楼。

---

## 验证物:如何亲手确认本章的设计

> 说明:本书写作环境为 Windows,无法直接运行 redis-server(8.0 依赖 fork/epoll 等 Linux 特性)。以下 (1) gdb 断点脚本 (2) 源码常量锚点 (3) OBJECT ENCODING 观察项 均为可复现的精确指引,供读者在 Linux 环境(Ubuntu 22.04 / CentOS 8 等)对 redis-8.0.2 源码 `make no-opt`(Makefile 里 no-opt 目标会去掉 -O2 加 -g)编译后自行验证。**本书不附编造的运行输出**——凡未实跑的,只给脚本与预期观察变量,不写具体数值。

### 1. gdb 断点脚本

编译:`cd redis-8.0.2 && make no-opt`(带 -g)
启动:`gdb ./src/redis-server`

```gdb
(gdb) break sdslen              # sds.h:65(inline,需 -O0 才能断;或断调用方)
(gdb) break _sdsnewlen          # sds.c:82  创建 SDS 的总入口
(gdb) break _sdsMakeRoomFor     # sds.c:223 预分配扩容
(gdb) break sdsRemoveFreeSpace  # sds.c:293 回收空闲空间
(gdb) break sdsclear            # sds.c:206 惰性释放(len 置 0,不 free)
(gdb) break createEmbeddedStringObject  # object.c:72 EMBSTR 编码入口
(gdb) break createRawStringObject       # object.c:65 RAW 编码入口
(gdb) run --port 6379

# 另开终端 redis-cli 发命令,gdb 会在断点停下:
redis-cli SET foo bar                 # 触发 _sdsnewlen(创建 "foo" 和 "bar" 两个 sds)
redis-cli SET bigkey $(head -c 100 /dev/urandom | base64)   # 100 字节 value,仍走 EMBSTR
redis-cli SET bigkey2 $(head -c 200 /dev/urandom | base64)  # 200 字节 value,触发 RAW
redis-cli APPEND foo x                 # 触发 _sdsMakeRoomFor(预分配翻倍)

# 在 _sdsnewlen 断点处观察:
(gdb) print initlen                   # 预期:字符串长度(如 "foo" → 3)
(gdb) print type                      # 预期:0(SDS_TYPE_5)会被升级到 1(SDS_TYPE_8)
(gdb) print hdrlen                    # 预期:3(sdshdr8)
(gdb) print usable                    # 预期:分配器实际给的可用字节(>= initlen)
(gdb) finish                          # 走完 _sdsnewlen,拿到返回的 sds
(gdb) print (char*)$                  # 预期:buf 内容(如 "foo")
(gdb) print ((unsigned char*)$)[-1]   # 预期:flags 低 3 位 = 1(SDS_TYPE_8),即 flags=1

# 在 _sdsMakeRoomFor 断点处观察预分配:
(gdb) print addlen                    # 预期:追加的字节数
(gdb) print sdsavail(s)               # 预期:扩容前的剩余空间
(gdb) print sdslen(s)                 # 预期:扩容前的已用长度
(gdb) finish
(gdb) print sdsavail(s)               # 预期:扩容后的剩余空间(若 newlen<1MB,应为 newlen 的 ~2 倍 - 已用)
```

**预期观察**(基于源码 [sds.c:82-145](../../redis-8.0.2/src/sds.c#L82) 与 [sds.c:223-274](../../redis-8.0.2/src/sds.c#L223),本书未实跑):创建 "foo"(3 字节)会先算出 `SDS_TYPE_5`(`initlen=3 < 32`),但因 `initlen==0` 才升级的逻辑不触发,实际 `initlen=3≠0`,理论上保留 `SDS_TYPE_5`;但只要后续 `APPEND`,扩容时 [sds.c:250](../../redis-8.0.2/src/sds.c#L250) 会强制升级到 `SDS_TYPE_8`。

### 2. 源码常量锚点(带行号,从 redis-8.0.2 源码 Grep 核实)

| 常量/字段 | 位置 | 值/说明 |
|----------|------|---------|
| `SDS_MAX_PREALLOC` | sds.h:14 | `1024*1024`(1MB,翻倍/加 1MB 分界) |
| `SDS_NOINIT`(extern) | sds.h:15 | 跳过 memset 的标记指针 |
| `SDS_NOINIT`(定义) | sds.c:21 | `"SDS_NOINIT"` |
| `typedef char *sds` | sds.h:21 | sds 就是 `char*` |
| `sdshdr5/8/16/32/64` 结构体 | sds.h:25-52 | 五档 header,全 `__packed__` |
| `SDS_TYPE_5..64` | sds.h:54-58 | 0/1/2/3/4 |
| `SDS_TYPE_MASK` | sds.h:59 | 7(低 3 位取 type) |
| `SDS_TYPE_BITS` | sds.h:60 | 3 |
| `SDS_TYPE_5_LEN(f)` | sds.h:63 | `(f)>>3` |
| `sdslen()` | sds.h:65-80 | `s[-1] & SDS_TYPE_MASK` 入口 |
| `sdsavail()` | sds.h:82-106 | sdshdr5 恒返回 0 |
| `sdsReqType()` | sds.c:39-53 | 按长度挑 header 类型 |
| `sdsHdrSize()` | sds.c:23-37 | 按 type 返回 header 字节数 |
| `_sdsnewlen()` | sds.c:82-145 | 创建 SDS 总入口(含 SDS_NOINIT 分支) |
| 空串升级到 type 8 | sds.c:88 | `if (type == SDS_TYPE_5 && initlen == 0) type = SDS_TYPE_8` |
| `_sdsMakeRoomFor()` | sds.c:223-274 | 预分配扩容核心 |
| 扩容时升级 type 5 | sds.c:250 | `if (type == SDS_TYPE_5) type = SDS_TYPE_8` |
| `sdsclear()` | sds.c:206-209 | 惰性释放(len 置 0,不 free) |
| `OBJ_ENCODING_EMBSTR_SIZE_LIMIT` | object.c:102 | 44(对应 jemalloc 64B arena) |
| `createEmbeddedStringObject()` | object.c:72-94 | robj+sdshdr8+buf 一次分配 |
| `robj` 结构体 | server.h:1001-1009 | type/encoding/lru/refcount/ptr = 16 字节 |
| `LRU_BITS` | server.h:994 | 24(lru 位段宽度) |
| `sdsalloc.h` 宏映射 | sdsalloc.h:22-31 | `s_malloc → zmalloc` 等十组 |

### 3. OBJECT ENCODING 观察项(embstr/raw/int 切换)

> 本节操作需在 Linux 本地启动 redis-server 后进行。本书未实跑,仅列观察方法与预期编码。

```bash
# 启动 redis-server(默认配置即可)
redis-server --port 6379

# 观察 1:短字符串走 EMBSTR(≤44 字节)
redis-cli SET k1 "hello"
redis-cli OBJECT ENCODING k1
# 预期:embstr(5 字节 << 44,且一次分配填满 64 字节 arena)

# 观察 2:刚好 44 字节,仍是 EMBSTR
redis-cli SET k2 "$(head -c 44 /dev/zero | tr '\0' 'x')"
redis-cli OBJECT ENCODING k2
# 预期:embstr(边界值,robj16+sdshdr8_3+buf44+\0_1 = 64)

# 观察 3:45 字节,触发 RAW
redis-cli SET k3 "$(head -c 45 /dev/zero | tr '\0' 'x')"
redis-cli OBJECT ENCODING k3
# 预期:raw(超 44 字节,改用两次分配)

# 观察 4:数字走 INT 编码(不用 sds)
redis-cli SET k4 12345
redis-cli OBJECT ENCODING k4
# 预期:int(ptr 直接塞 long long,不分配 sds)

# 观察 5:EMBSTR 修改后转 RAW
redis-cli SET k5 "hello"            # embstr
redis-cli APPEND k5 "world"         # 修改 → 转 raw
redis-cli OBJECT ENCODING k5
# 预期:raw(EMBSTR 不可修改,任何写操作先转 RAW)

# 观察 6:STRLEN 是 O(1)(无论 value 多长,都是寄存器读)
redis-cli SET bigv "$(head -c 1000000 /dev/zero | tr '\0' 'x')"
redis-cli STRLEN bigv
# 预期:瞬间返回 1000000(sdslen 直接读 sdshdr32 的 len 字段,不扫描)
```

**预期观察**(基于源码 [object.c:102-108](../../redis-8.0.2/src/object.c#L102) 的阈值判断与 [sds.h:65-80](../../redis-8.0.2/src/sds.h#L65) 的 `sdslen`,本书未在本地实跑):EMBSTR/RAW 的切换点严格在 44 字节,这是 `robj(16) + sdshdr8(3) + buf + '\0'(1) = 64` 反推出来的。若你的 Redis 链接了不同 size class 的分配器(tcmalloc 或 libc malloc),边界可能略有不同,但 SDS 本身的 `44` 这个常量不变——它写死在源码里。
