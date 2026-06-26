# 第 1 篇 · 第 4 章 · Buffer 与零拷贝

> **核心问题**:P1-03 里讲清楚了 dispatcher 怎么把"socket 可读"这件事变成回调喂给 filter chain——可那个回调被喂进去的,到底是什么?一条流量从 listener 进来、穿过几十个 filter、再从 upstream 出去,这几百字节、几兆字节、几吉字节的字节流,**在 filter 之间用什么容器传**?最朴素的答案是用一个 `std::string`、或者一个大 `char[]`——但这个答案,在"单机几十万 QPS、每条流量穿十几个 filter"的 Envoy 场景下,会撞上一堵高得离谱的墙。Envoy 的答案是:**把字节流做成一串分片(slice),filter 之间传这串分片的所有权(指针搬移),而不是 copy 字节**——这就是 Buffer。本章拆这件事,以及它怎么和 watermark 背压一起,撑住数据面的吞吐和内存。

> **读完本章你会明白**:
> 1. 为什么 Envoy 不用一个连续的 `std::string` / `char[]` 装字节流,而是把它做成一串 `Slice` 分片——朴素连续数组在 filter 之间 copy、在 socket 读写两端对齐上,会撞什么墙。
> 2. filter 之间传 Buffer 是 **move(所有权搬移)** 而不是 copy:为什么 move 是 O(1)、为什么 sound(slice 的 `unique_ptr` 所有权清晰、 drain tracker 随字节一起搬),小切片为什么有个 512 字节的合流阈值。
> 3. **真正从内核到 filter 的零拷贝**:`reserveForRead()` + `readv()` + `commit()` 这套机制,怎么让内核把字节直接写进 Envoy 的 slice 里(而不是先 read 到一个临时 `char[]` 再 memcpy 进 buffer);`io_uring` 多拍读怎么更进一步用 `BufferFragment` 接管内核 buffer pool 的生命周期。
> 4. **Watermark 背压**:Buffer 不能无界涨——下游慢、上游快时,Buffer 涨到 high watermark 就反压上游停读(callbacks 通知),跌到 low watermark 再恢复。为什么这是"内存爆掉"和"全链路阻塞"之间唯一的解。
> 5. `linearize` 这个偶尔才付的代价:为什么 Envoy 默认不把 slice 铺平成连续内存,只在 codec / TLS / Lua 这些真的需要连续内存的地方才做一次 copy。

> **如果一读觉得太难**:先只记住三件事——① **Buffer = slice 链(其实是 slice 环形队列),不是连续数组**;② **filter 之间传 Buffer 是 move 不是 copy**(所有权搬移,小切片 512 字节以下才合流);③ **Buffer 涨到 high watermark 反压上游、跌到 low watermark 恢复**。剩下的都是这三件事的"为什么"和"怎么做的"。

---

## 〇、一句话点破

> **Envoy 的字节流不是一坨连续内存,而是一串 `Slice`;filter 之间传的是这串 slice 的所有权(move),不是字节内容(copy);真正读 socket 时,内核直接把字节写进 slice 里,Envoy 一字节都不拷——这套,把"海量流量穿十几个 filter"这件事的 CPU 成本,从"每过一层就 memcpy 一次"压到了"全程近乎零拷贝"。**

这是结论,不是理由。本章倒过来拆:先讲一个连续数组在 filter 之间传为什么会撞墙,再讲 slice 分片怎么破这道墙;然后讲真正的零拷贝(readv 直写 slice);再讲 Buffer 不能无界涨、watermark 怎么做背压;最后讲 `linearize` 这个偶尔才付的代价。

---

## 一、为什么不能用一个 std::string 装字节流

P1-03 结尾我们说:dispatcher 把"socket 可读"变成回调,回调里干的事就是 `transport_socket_->doRead(*read_buffer_)`——把字节读进一个 `read_buffer_`。这个 `read_buffer_` 是什么类型?它是一个 `Buffer::Instance`。为什么不直接是个 `std::string`?

要回答这个问题,先看朴素方案会撞什么墙。

### 不这样会怎样:连续数组在 filter chain 里撞的三堵墙

假设我们用 `std::string`(或 `std::vector<char>`,本质都是一段连续内存)装整条字节流。一条 HTTP 请求进 Envoy,要穿过 listener filter → network filter → HCM → http filter → router → upstream 连接池,少说七八层 filter。每过一层,filter 都要对字节流做点事:看看头部、改改路径、加个鉴权头、压缩 body……

**第一堵墙:每过一层 filter 就整段 memcpy。** 如果每个 filter 都拿自己的 `std::string`、把上一层的 `std::string` copy 过来再处理,那一条 1MB 的 body,穿 10 层 filter 就是 10 次 memcpy、10MB 的内存读写。在单机百万 QPS 的 Envoy 场景下,光 memcpy 的 CPU 就够把机器打满。这还没算每次 copy 都要 `new` 一块新内存——分配器的锁竞争、cache miss、内存抖动会接踵而至。

**第二堵墙:头部解析要在中间插入、在前面 prepend,连续数组挪不动。** HTTP 处理里,filter 经常要"在字节流前面插一段"(比如 HCM 解析完头部要在 body 前面插一个改造后的 header),或者"在中间切一段出来"(codec 解析一行)。连续数组要在中间插东西,得把后面所有字节都往后挪——O(n) 的代价。在前面 prepend 要么重新分配 + 整段复制,要么预留空间(但预留多少又是个难题,流式数据进来大小未知)。

**第三堵墙:socket 读写和 buffer 的对齐。** 这个最致命。一次 `read()` 内核给你 N 个字节(可能 64 字节,也可能 64KB,内核说了算),你追加到 `std::string` 末尾。但 `std::string` 末尾未必有 N 字节空间(capacity 不够就得 realloc + 整段 copy)。更糟的是:高效 IO 要用 `readv`/`writev`——一次系统调用读写多段不连续内存(scatter-gather IO)。`std::string` 是一段连续内存,根本喂不进 `readv` 的 `iovec` 数组(只能塞一个 `iovec`,丧失了 scatter-gather 的能力)。这意味着:`std::string` 模型下,要么牺牲 IO 效率(只能 read 单段),要么在 IO 边界做额外的 copy/拼接。

> **不这样会怎样**:一个真实量级——Envoy 单 worker 在生产环境轻松处理几万 RPS、每条请求平均几 KB 到几百 KB。如果 buffer 是连续 `std::string`、每过一层 filter 都 memcpy:一条 10KB 请求穿 8 层 filter = 80KB 的 memcpy;每秒 5 万 RPS = 每秒 4GB 的纯内存拷贝。这还只是单 worker、还没算 malloc/free 的开销。**memcpy 在这种场景下不是"小开销",是会把 CPU 吃光的头号杀手。** 任何"每过一层 filter 就 copy 一遍"的设计,在生产级代理里都是不可接受的。

### 所以这样设计:把字节流做成一串 Slice 分片

Envoy 的答案是:**不把字节流装进一段连续内存,而是把它切成若干个 `Slice`,串成一个队列。** 每个 `Slice` 是一小段连续字节(默认 16KB,见 [`Slice::default_slice_size_`](../envoy/source/common/buffer/buffer_impl.h#L345)),多个 slice 拼起来逻辑上就是完整的字节流。

```
   一个 Buffer::Instance,逻辑上是一串 Slice(分片)
   ┌──────────┐  ┌──────────────┐  ┌─────────┐  ┌───────────┐
   │ Slice 0  │  │   Slice 1    │  │ Slice 2 │  │  Slice 3  │
   │ 16 KB    │  │   12 KB      │  │ 16 KB   │  │  3 KB     │
   │ "GET /a" │  │ "HTTP/1.1.." │  │ <body>  │  │ <chunk>   │
   └──────────┘  └──────────────┘  └─────────┘  └───────────┘
        ▲                                              ▲
        │                                              │
     drain 从前往后吞                          add/read 从后往后追加
   (读过的字节被 drain,                      (新来的字节追加到末尾,
    slice 整个弹掉)                           可能复用旧 slice 的尾部空间,
                                                也可能开新 slice)
```

先看一个 `Slice` 内部是什么样。源码注释里的布局图很说明问题([`buffer_impl.h#L22-L36`](../envoy/source/common/buffer/buffer_impl.h#L22-L36)):

```
                   |<- dataSize() ->|<- reservableSize() ->|
   +-----------------+----------------+----------------------+
   | Drained         | Data           | Reservable           |
   | Unused space    | Usable content | New content can be   |
   | (former Data,   |                | added with reserve() |
   |  now recycled)  |                | /commit()/append()   |
   +-----------------+----------------+----------------------+
   ^                 ^                ^                      ^
   base_             base_+data_      base_+reservable_      base_+capacity_
```

一个 slice 是一段连续内存(`base_` 到 `base_+capacity_`),被两个偏移量切成三段:

- **Data 段**(中间):真正的有效字节,`dataSize() = reservable_ - data_`。
- **Reservable 段**(右边):可以继续往后追加新字节的空闲空间,`reservableSize() = capacity_ - reservable_`。
- **Drained 段**(左边):已经被 `drain()` 丢掉的字节,空间可以回收。

> **钉死这件事**:`Slice` 的三段式布局,是它"既能在头部 O(1) drain、又能在尾部 O(1) append"的根。drain 不搬移数据,只挪 `data_` 偏移量([`Slice::drain`](../envoy/source/common/buffer/buffer_impl.h#L176-L185));append 也只挪 `reservable_` 偏移量([`Slice::append`](../envoy/source/common/buffer/buffer_impl.h#L261-L271))。**没有任何字节被 memcpy,纯指针算术。** 这才是 slice 分片相对连续数组的根本优势:头尾都是 O(1)。

这串 slice 不是用链表串的。当前实现是一个 `SliceDeque`——一个**环形队列(ring buffer)**,内部先内联 8 个 slot(避免小 buffer 还要 heap 分配),超了再扩到外部的 `unique_ptr<Slice[]>`(见 [`SliceDeque`](../envoy/source/common/buffer/buffer_impl.h#L426-L570),`InlineRingCapacity = 8` 在 [`buffer_impl.h#L533`](../envoy/source/common/buffer/buffer_impl.h#L533))。

> **注:此处与部分老资料/总纲草稿不符**。Envoy 早期(2018 年前后)的 Buffer 实现是基于 libevent 的 `evbuffer`,内部是真正的链表;后来重写成自家的 `Slice` + 自家 `SliceDeque`。如果你读到的资料说"Buffer 是 slice 链表",那是**老版本过时印象**。当前 master(`df2c77d`,1.39.0-dev)是 `Slice` + `SliceDeque`(环形队列),源码注释明确说"为什么不直接用 `std::deque`"——因为 benchmark 显示 `std::deque` 太慢、追不上老的 evbuffer 实现(见 [`SliceDeque` 类注释](../envoy/source/common/buffer/buffer_impl.h#L418-L425))。环形队列比链表 cache 友好、比 `std::deque` 分配少,这是实测调出来的选型。

为什么用环形队列而不是链表?链表每加一个 slice 要 `new` 一个链表节点(分配开销 + 指针跳转 cache 不友好);环形队列把 slice 直接放在连续数组里,`InlineRingCapacity=8` 意味着小 buffer(8 个 slice 以内)连 heap 都不用碰,全在栈/对象内联里。**这是一个用数据局部性换性能的典型选择**,和《内存分配器》那本讲的"快路径(fast path)要 cache 友好"是同一类思路。

> **钉死这件事**:Buffer = 一串 `Slice`(放在一个环形队列 `SliceDeque` 里)。每个 slice 是一小段连续内存(默认 16KB,按 4KB 页对齐,见 [`Slice::sliceSize`](../envoy/source/common/buffer/buffer_impl.h#L353-L357))。逻辑上整条字节流 = 所有 slice 的 Data 段拼接。drain 从前面弹、add 从后面追加,都是 O(1) 指针操作。

---

## 二、filter 之间传 Buffer 是 move,不是 copy

slice 分片解决了"单 buffer 内部头尾操作 O(1)"。但 filter chain 的核心问题是:**buffer 要从一层 filter 传到下一层 filter**——这才是"每过一层就 copy"那堵墙的真正所在。

### 不这样会怎样:每过一个 filter 就 copy 一遍

回到第一节的反例。一条 1MB 的 body 穿过鉴权 → 限流 → fault → router 四层 filter,如果每层都把自己的 buffer copy 过来再处理,就是 4 次 1MB 的 memcpy。

即便用 slice 分片,如果 filter 间传递还是"copy 所有 slice 的内容",那只不过是把"copy 一段大连续内存"换成"copy N 段小连续内存"——总字节数没变,memcpy 的成本没省下来。

更微妙的是:filter 经常只读不改(鉴权 filter 只看头部有没有合法 token,不碰 body)。如果这种"只读 filter"也要 copy 一遍 buffer 才能用,那 90% 的 filter 都在白做 memcpy。这在性能上完全不可接受。

### 所以这样设计:filter 之间传 slice 的所有权(move)

Envoy 的答案是:**filter 之间传 Buffer 用 `move()`,搬的是 slice 的所有权(slice 里的 `unique_ptr` 转移),不是字节内容。** 这是 C++ move 语义在 buffer 上的应用。

看 [`OwnedImpl::move(Instance& rhs)`](../envoy/source/common/buffer/buffer_impl.cc#L334-L347):

```cpp
// (简化示意,保留关键逻辑,非源码逐字)
void OwnedImpl::move(Instance& rhs) {
  OwnedImpl& other = static_cast<OwnedImpl&>(rhs);
  while (!other.slices_.empty()) {
    const uint64_t slice_size = other.slices_.front().dataSize();
    coalesceOrAddSlice(std::move(other.slices_.front()));  // 搬所有权
    other.length_ -= slice_size;
    other.slices_.pop_front();                              // 源 buffer 弹掉
  }
  other.postProcess();
}
```

`std::move(other.slices_.front())` 把源 buffer 的第一个 slice **整个搬**到目标 buffer——slice 内部的 `unique_ptr<uint8_t[]> storage_` 转移所有权(`unique_ptr` 的 move 就是改指针、源置空),`base_`/`data_`/`reservable_` 这些偏移量纯赋值。**全程没有 memcpy 一个字节,纯指针和整数的搬移。** 这就是 O(1) 的 move。

那 `coalesceOrAddSlice` 是什么?这是"小切片合流"的优化,值得单独看一眼([`buffer_impl.cc#L312-L332`](../envoy/source/common/buffer/buffer_impl.cc#L312-L332)):

```cpp
// (简化示意,保留关键判断,非源码逐字)
constexpr uint64_t CopyThreshold = 512;   // buffer_impl.cc:19

void OwnedImpl::coalesceOrAddSlice(Slice&& other_slice) {
  const uint64_t slice_size = other_slice.dataSize();
  // 满足以下四个条件,才把 other_slice 的内容 memcpy 进现有 slice 尾部(合流):
  // 1. other_slice 可合流(不是外部不可变 fragment);
  // 2. 目标 buffer 非空;
  // 3. other_slice 内容 < 512 字节(小);
  // 4. 现有 slice 尾部有足够空闲空间。
  if (other_slice.canCoalesce() && !slices_.empty() && slice_size < CopyThreshold &&
      slices_.back().reservableSize() >= slice_size) {
    addImpl(other_slice.data(), slice_size);              // memcpy 小段
    other_slice.transferDrainTrackersTo(slices_.back());  // drain tracker 跟过去
  } else {
    other_slice.maybeChargeAccount(account_);
    slices_.emplace_back(std::move(other_slice));         // 大 slice:搬所有权
    length_ += slice_size;
  }
}
```

这里有个精妙的取舍:**小切片(< 512 字节)合流(copy 进上一个 slice),大切片搬所有权(move)。** 为什么?

- **大 slice**:搬所有权是 O(1) 的,memcpy 一段 16KB 字节要花时间——move 完胜。
- **小 slice**:如果总是搬所有权,buffer 里会积累大量只有几十字节的小 slice。slice 多了有两个坏处:① 每次 `getRawSlices`/`search`/`linearize` 要遍历更多 slice;② 每个 slice 至少占一个 slot(16KB capacity),碎片化浪费内存。所以**小切片干脆 memcpy 进上一个 slice 的尾部空间,消化掉**——512 字节的 memcpy 在现代 CPU 上是纳秒级,远比"多维护一个 slice 对象"划算。

这个 512 字节的阈值是 benchmark 调出来的(源码注释:[`buffer_impl.cc#L15-L19`](../envoy/source/common/buffer/buffer_impl.cc#L15-L19) 明确说"This size has been determined to be optimal from running the //test/integration:http_benchmark benchmark tests")。**这是 Envoy 性能调优里"该 copy 时 copy、该 move 时 move"的典型例子——不教条地追求"零拷贝",而是在 copy 的成本和 slice 碎片化的成本之间找平衡。**

> **钉死这件事**:filter 之间传 Buffer 是 `move()`——搬 slice 的所有权(`unique_ptr` 转移 + 偏移量赋值),不 memcpy 字节。只有小于 512 字节的小切片才会被 memcpy 合流进上一个 slice(避免碎片化)。这是 Buffer 设计的核心性能支柱。

### 为什么 move 是 sound:所有权清晰,drain tracker 随字节搬

move 这么搬,会不会出问题?比如搬过去之后,源 buffer 还以为它拥有这些字节、去 free 一遍(double free)?或者某个 filter 注册了"这些字节被 drain 时通知我"的回调(drain tracker),move 之后回调还能正确触发吗?

不会。关键在 `Slice` 内部的所有权设计是清晰的:

- **`storage_` 是 `unique_ptr<uint8_t[]>`**(见 [`Slice::storage_`](../envoy/source/common/buffer/buffer_impl.h#L378)):RAII 管理内存。move 时 `std::move(rhs.storage_)` 转移所有权,源的 `storage_` 变 `nullptr`——源 slice 析构时 `unique_ptr` 是空、什么都不 free。**double free 不可能。**
- **`base_` 指向 `storage_.get()`**(当 slice 拥有自己的存储时):move 时 `base_ = rhs.base_; rhs.base_ = nullptr`——源 slice 被清空,不会误访问。
- **drain tracker 随字节一起搬**:`Slice` 持有一个 `std::list<std::function<void()>> drain_trackers_`(见 [`Slice::drain_trackers_`](../envoy/source/common/buffer/buffer_impl.h#L391)),move 时整个 list 也 `std::move` 过去([`Slice` move 构造](../envoy/source/common/buffer/buffer_impl.h#L98-L112) 第 104 行)。这意味着"这些字节被 drain 时通知谁"这个约定,跟着字节一起走到哪个 buffer 都成立——**不管字节被 move 到下游哪个 filter,drain tracker 最终会在真正消费掉这些字节的那个 buffer 里触发。**

```
   move 的所有权搬移(大 slice 场景,无 memcpy):

   源 buffer                          目标 buffer
   ┌───────────┐                      ┌───────────┐
   │ slices_:  │   move(rhs)          │ slices_:  │
   │ [Slice A] │ ───────────────────▶ │ [Slice A] │  ◀── unique_ptr 转移
   │ [Slice B] │                      │ [Slice B] │      base_/data_/reservable_ 赋值
   │           │                      │           │      drain_trackers_ 整个 list move
   └───────────┘                      └───────────┘
   源的 Slice A/B 被 pop_front,
   storage_ 已转走 → 析构是 no-op,
   不会 double free

   length_ 同步更新(源 -= ,目标 +=)
```

`Buffer::Instance` 接口文档说得很直白([`buffer.h#L150-L156`](../envoy/envoy/buffer/buffer.h#L150-L156)):"drain tracker 跟着字节走,直到最后一字节从所有 buffer 里 drain 掉才触发"。这个语义在 move 模型下天然 sound——因为 tracker 和字节绑定在同一个 slice 里,字节去哪 tracker 去哪。

> **钉死这件事**:move 是 sound 的,因为 ① slice 用 `unique_ptr` 管内存,move 转移所有权、源自动置空,不可能 double free;② drain tracker 是 slice 的成员、move 时整组搬走,"字节被消费时通知谁"这个约定永远跟随字节。**所有权清晰、回调随字节走,这是 move 能安全用在 filter chain 上的两个前提。**

---

## 三、真正从内核到 filter 的零拷贝

前两节讲的是"filter 之间传 buffer 不 copy"。但字节流还有个源头:**它从内核 socket buffer 怎么进 Envoy 的 buffer**?这一段如果还要 copy 一次(read 到临时 `char[]` 再 memcpy 进 slice),那"filter 间零拷贝"省下的成本,在源头又赊了回去。

### 不这样会怎样:read 到临时缓冲再封装

朴素的 read 流程是这样的:

```
   朴素 read(有一次多余 copy):
   内核 socket buffer
        │  read(tmp[], N)
        ▼
   临时 char tmp[N]  ◀── 第一次 copy(内核→用户态,不可避免)
        │  buffer.add(tmp, n)
        ▼
   Buffer 的 slice   ◀── 第二次 copy(临时数组→buffer,多余!)
```

这里第二次 copy 是多余的:我们从内核读到了 `tmp[]`,只是为了把它喂进 buffer,又 memcpy 了一遍。这次 memcpy 在高 QPS 下同样是 CPU 杀手。

### 所以这样设计:reserveForRead + readv + commit,内核直写 slice

Envoy 的 `IoSocketHandle::read` 是这么做的([`io_socket_handle_impl.cc#L98-L111`](../envoy/source/common/network/io_socket_handle_impl.cc#L98-L111)):

```cpp
// (简化示意,保留关键三步,非源码逐字)
Api::IoCallUint64Result IoSocketHandleImpl::read(Buffer::Instance& buffer, ...) {
  Buffer::Reservation reservation = buffer.reserveForRead();        // ① 预留 slice 的可写空间
  Api::IoCallUint64Result result = readv(
      std::min(reservation.length(), max_length),
      reservation.slices(), reservation.numSlices());               // ② readv 直写进 slice 空间
  uint64_t bytes_to_commit = result.ok() ? result.return_value_ : 0;
  reservation.commit(bytes_to_commit);                              // ③ 提交(挪 reservable_ 偏移)
  return result;
}
```

这三步是零拷贝的灵魂:

1. **`reserveForRead()`**:在 buffer 末尾的 slice 里预留一段可写空间(返回一个 `Reservation`,里面是一组 `RawSlice`,每个 `RawSlice` = `{可写地址, 长度}`)。这段空间**已经属于 buffer 的 slice 了**(要么复用尾 slice 的 Reservable 段,要么开新 slice),只是还没填字节。注意 [`OwnedImpl::reserveForRead`](../envoy/source/common/buffer/buffer_impl.cc#L384-L386) 默认预留 `MAX_SLICES_(8) × default_slice_size_(16KB) = 128KB`,一次能吃一大口。

2. **`readv(...)`**:`readv` 是 POSIX 的 scatter read——一次系统调用把内核字节**直接写进多段不连续的用户态内存**(`iovec` 数组)。这里把 `Reservation` 里的 `RawSlice` 数组直接当 `iovec` 用(见 [`io_socket_handle_impl.cc#L113-L132`](../envoy/source/common/network/io_socket_handle_impl.cc#L113-L132) 的 `writev` 对应实现,`readv` 同构)。**内核的字节直接落进 buffer 的 slice 里,中间没有任何临时缓冲、没有 memcpy。**

3. **`commit(bytes_to_commit)`**:告诉 buffer"我实际写了这么多字节",buffer 把对应 slice 的 `reservable_` 偏移量往后挪(纯指针算术),这些字节就成了 buffer 的正式 Data 段。

```
   零拷贝 read(内核直写 slice,无多余 copy):

   内核 socket buffer
        │  readv(iovec[], n)   ◀── iovec 直接指向 slice 的 Reservable 段
        ▼
   Buffer 的 slice 的 Reservable 段  ◀── 唯一一次 copy(内核→用户态,不可避免)
        │  commit(n)           ◀── 挪 reservable_ 偏移,纯指针算术
        ▼
   这些字节现在是 buffer 的正式 Data 段
```

> **钉死这件事**:零拷贝不是"真的零次 copy"——内核到用户态那一次 copy(`readv` 由 DMA + CPU 完成)是物理上不可避免的(除非用 kernel-bypass / DPDK)。Envoy 的"零拷贝"指的是:**消除了"读到一个临时数组、再 memcpy 进 buffer"那一次多余的软件 copy**。字节从内核直接落进 buffer 自己的 slice 里。这才是零拷贝的真正含义,和 Nginx 的 `recv` 到临时缓冲、libevent `evbuffer_add` 是同一类优化。

### Reservation 这个设计:为什么把"预留"和"提交"拆成两步

你可能会问:为什么 `read` 要拆成 `reserveForRead` + `readv` + `commit` 三步,而不是一个 `read(buffer)` 黑盒搞定?

因为**"这次 read 到底读了多少字节"这件事,只有 `readv` 返回之后才知道**(内核说了算——可能 0 字节、可能 64 字节、可能 64KB)。如果是一个黑盒 `read(buffer)`,内部就得先 read 到一个临时缓冲、再根据实际读到的字节数 add 进 buffer——又回到了"多余 copy"的老路。

拆成两步后:**先 reserve 一大片可能被写的空间(这时还不知道实际写多少)→ 内核 readv 直写这片空间 → commit 时只承认实际写的字节数(把 `reservable_` 挪那么多)**。多 reserve 的空间不浪费(还在 slice 的 Reservable 段里,下次 read 还能用),少 commit 的字节不丢失(实际写了多少就承认多少)。

这就是 `Reservation` 类([`buffer.h#L590-L655`](../envoy/envoy/buffer/buffer.h#L590-L655))存在的根本理由:**它把"IO 边界的不确定性(实际读写多少由内核决定)"和"buffer 内部状态的一致性(reservable_ 偏移必须准确)"解耦了**。这是个很优雅的设计——值得在技巧精解里再拆一遍。

### 更进一步:io_uring 的 BufferFragment,接管内核 buffer pool 的生命周期

Envoy 新版的 `io_uring` 支持([`io_uring_worker_impl.cc#L515-L539`](../envoy/source/common/io/io_uring_worker_impl.cc#L515-L539))更进一步:多拍读(multishot read)用内核预注册的 buffer pool,读到的数据**直接在内核 buffer 里**,Envoy 连那唯一一次内核→用户态的 copy 都省了。这时 slice 怎么指向它?

```cpp
// (简化示意,非源码逐字)
Buffer::BufferFragment* fragment = new Buffer::BufferFragmentImpl(
    pool->getBuffer(buffer_id),  // 内核 buffer pool 里的某块
    data_length,
    [pool](const void* data, size_t, const Buffer::BufferFragmentImpl* self) {
      pool->releaseBuffer(data);  // 字节被 drain 完,归还内核 buffer
      delete self;
    });
read_buf_.addBufferFragment(*fragment);  // 加入 buffer,但不 copy 字节
```

这就是 `Slice` 的另一种形态——**不可变 slice,指向 Envoy 不拥有的外部内存**。看 [`Slice` 的 BufferFragment 构造](../envoy/source/common/buffer/buffer_impl.h#L91-L96):

```cpp
// (简化示意,非源码逐字)
Slice(BufferFragment& fragment)
    : capacity_(fragment.size()), storage_(nullptr),       // storage_ 为空 → 不拥有
      base_(const_cast<uint8_t*>(fragment.data())),         // base_ 指向外部内存
      reservable_(fragment.size()) {
  releasor_ = [&fragment]() { fragment.done(); };           // drain 时回调归还
}
```

这种 slice:① `storage_ == nullptr`(不拥有内存);② `isMutable()` 返回 false(不可变,因为是外部的,Envoy 不能改);③ `canCoalesce()` 返回 false(不能 memcpy 合流进别的 slice——因为它的析构会触发 `releasor_` 归还外部内存,合流就把这个归还逻辑丢了);④ drain 完 / slice 析构时,`releasor_()` 被调用,通知外部"这块内存我用完了,你可以回收了"。

这是真正的"零拷贝"——从内核 buffer pool 一直到 filter chain,字节都待在原始的那块内存里,Envoy 只是挂了个引用 + 归还回调。`addBufferFragment` 这个 API 在 HTTP/1 codec(把编解码的临时 slice 直接挂进 buffer,见 [`codec_impl.cc#L331`](../envoy/source/common/http/http1/codec_impl.cc#L331))、ALTS transport、QUIC、gRPC slice 都有用到——凡是"字节本来就在某块外部内存里、没必要再 copy"的场景,都走这条路。

> **钉死这件事**:Envoy 的零拷贝有两层——① 软件零拷贝(`reserveForRead` + `readv` + `commit`),消除"读到临时数组再 memcpy 进 buffer"的多余 copy;② 硬件零拷贝(`BufferFragment` 接管外部内存,如 `io_uring` 内核 buffer pool),连内核→用户态那次 copy 都省。**`Slice` 通过 `storage_` 是否为空,优雅地统一了"自己拥有的内存"和"引用的外部内存"两种 slice。**

---

## 四、Watermark 背压:Buffer 不能无界涨

前三节讲的 Buffer,有个隐含假设:它想涨多大就涨多大。但生产环境里,Buffer 不能无界涨。为什么?

### 不这样会怎样:Buffer 无界涨,内存爆

代理的核心矛盾是:**上下游速度不匹配**。下游慢了(比如后端处理慢、或下游 TCP 窗口小),上游还在源源不断地推字节进来。如果代理无脑把上游推来的字节都收下、堆在 Buffer 里,Buffer 就会一直涨——直到把内存吃光,OOM(Out Of Memory)被内核杀掉。

这在长连接场景尤其致命:一个 gRPC stream,上游客户端慢慢推、下游服务端慢慢处理,Buffer 里堆了几百 MB 都没排出去。再来几个这样的连接,Envoy 自己就被内存压垮了——**代理本该是"转发"的角色,却变成了"缓存",而且是无界缓存**。

更糟的是:如果 Envoy 因为内存压力挂了,它承载的所有连接(可能是几万个)全部断掉,故障从一个慢后端扩散到整个网格。这是代理设计里必须堵死的单点。

### 所以这样设计:high/low watermark + 反压回调

Envoy 的解法是 **Watermark(水位)**:给每个 Buffer 设两个阈值——**high watermark(高水位)** 和 **low watermark(低水位)**。Buffer 涨过 high watermark 时,触发"反压上游"的回调(让上游别再推);跌回 low watermark 时,触发"恢复上游"的回调(可以继续推了)。

```
   Watermark 三段式(高水位 = H,低水位 = H/2):

   内存
    ▲
    │      ┌─────────  overflow watermark (可选,触发 reset stream)
    │      │
    │      │         ──── H (high watermark):涨到这里 → above_high_watermark_()
    │      │              反压上游(停读 / 通知 onAboveWriteBufferHighWatermark)
    │   ┌──┴──┐
    │   │     │  ◀── 正常区间:Buffer 在 H/2 和 H 之间,无回调
    │   │     │
    │   └──┬──┘
    │      │         ──── H/2 (low watermark):跌到这里 → below_low_watermark_()
    │      │              恢复上游(继续读 / 通知 onBelowWriteBufferLowWatermark)
    │      │
    │      └─────────
    │
    └────────────────────────────────▶ 时间 / 字节数
```

注意一个细节:**low watermark 不是独立配置的,而是硬编码成 high watermark 的一半**。看 [`WatermarkBuffer::setWatermarks`](../envoy/source/common/buffer/watermark_buffer.cc#L119-L132):

```cpp
// (简化示意,非源码逐字)
void WatermarkBuffer::setWatermarks(uint64_t high_watermark,
                                    uint32_t overflow_watermark_multiplier) {
  low_watermark_  = high_watermark / 2;                          // ★ 硬编码 H/2
  high_watermark_ = high_watermark;
  overflow_watermark_ = overflow_watermark_multiplier * high_watermark;  // 可选,默认 0 关闭
  checkHighAndOverflowWatermarks();
  checkLowWatermark();
}
```

为什么 low = high/2、而不是 high 的 90%?这是为了避免**抖动(thrashing)**:如果 low 紧挨着 high(比如 low = 0.9 × high),那 Buffer 刚跌下 high 一点点就触发"恢复上游",上游一推又立刻涨回 high 触发"反压"——来回抖动,回调被高频触发,系统在 watermark 边界来回震荡。low = high/2 留了一个足够宽的缓冲带(hysteresis,迟滞),Buffer 在这个带子里涨涨跌跌都不触发回调,只有真正跨越 high 或真正跌穿 high/2 才动作一次。**这是控制论里迟滞(hysteresis)的经典应用,和恒温器、施密特触发器是同一类思想。**

### 回调的边沿触发:只在状态翻转时调一次

watermark 回调不是"每次 buffer 变化都检查",而是**边沿触发(edge-triggered)——只在状态翻转的那一次调用**。看 [`checkHighAndOverflowWatermarks`](../envoy/source/common/buffer/watermark_buffer.cc#L144-L161):

```cpp
// (简化示意,非源码逐字)
void WatermarkBuffer::checkHighAndOverflowWatermarks() {
  if (high_watermark_ == 0 || OwnedImpl::length() <= high_watermark_) {
    return;   // 没涨过 high,什么都不做
  }
  if (!above_high_watermark_called_) {       // ★ 关键:只在"还没触发过"时调
    above_high_watermark_called_ = true;
    above_high_watermark_();                  // 反压上游,调一次
  }
  // overflow(可选):buffer 涨到 overflow_watermark,reset 这条 stream
  if (overflow_watermark_ != 0 && !above_overflow_watermark_called_ &&
      OwnedImpl::length() > overflow_watermark_) {
    above_overflow_watermark_called_ = true;
    above_overflow_watermark_();              // reset,只调一次,不重置
  }
}
```

关键是 `above_high_watermark_called_` 这个布尔标志([`watermark_buffer.h#L79`](../envoy/source/common/buffer/watermark_buffer.h#L79)):它记录"上一次状态"。只要它还是 true(已经触发过反压),那 buffer 继续涨(从 H 涨到 2H、3H)都不会再调 `above_high_watermark_()`——上游早就被反压了,再喊一遍没意义。只有当 buffer 跌回 low watermark、`checkLowWatermark` 把这个标志清成 false 之后([`watermark_buffer.cc#L134-L142`](../envoy/source/common/buffer/watermark_buffer.cc#L134-L142)),下次再涨过 high 才会重新触发。

```cpp
// (简化示意,非源码逐字)
void WatermarkBuffer::checkLowWatermark() {
  if (!above_high_watermark_called_ ||                // 从没触发过 high,不用恢复
      (high_watermark_ != 0 && OwnedImpl::length() > low_watermark_)) {
    return;                                           // 还没跌到 low,不恢复
  }
  above_high_watermark_called_ = false;               // 清标志,允许下次 high 再触发
  below_low_watermark_();                             // 恢复上游,调一次
}
```

> **钉死这件事**:watermark 是**边沿触发**:buffer 从"低于 high"翻到"高于 high"调一次反压、从"高于 low"翻到"低于 low"调一次恢复,中间的涨涨跌跌不触发。这避免了回调被高频触发(每次 add/drain 都喊一遍),也避免了在边界抖动。`above_high_watermark_called_` 这个标志就是状态机的状态位。

### 反压怎么传到上游:read disable 与 onAboveWriteBufferHighWatermark

watermark 回调最终要落到"真的让上游少推字节"这个动作上。Envoy 有两种主要机制:

- **下游读 buffer 涨过 high → 停止从 socket 读**。最直接的反压:既然我处理不过来,我就不读了。socket buffer 在内核里涨,内核 TCP 拥塞控制自然会让对端(上游客户端)慢下来——TCP 自带流控(window-based),你 ACK 慢了,对端 send 窗口就缩。**这是零成本的反压——利用 TCP 内建流控,Envoy 只要"不读"就够了。**
- **上游写 buffer(downstream 连接的 send buffer)涨过 high → 通知 connection callback**。看 [`ConnectionImplBase::onFilterAboveHighWatermark`](../envoy/source/common/network/connection_impl_base.cc#L89-L98):它调 `onAboveWriteBufferHighWatermark()`,通知 HCM 等 connection callbacks"我的写 buffer 满了"——HCM 据此可以暂停从 upstream 读(形成全链路反压:downstream 慢 → downstream 写 buffer 满 → 通知 HCM → HCM 让 upstream 连接暂停读 → upstream 慢)。

```
   全链路反压(downstream 慢 → 反压传到 upstream):

   downstream 客户端 ──(慢读)──▶ Envoy downstream 连接
                                       │ downstream 写 buffer 涨过 high
                                       ▼
                                  onAboveWriteBufferHighWatermark
                                       │ 通知 HCM
                                       ▼
                                  HCM 暂停从 upstream 读
                                       │
                                       ▼
   upstream 后端 ◀──(Envoy 不读,socket buffer 涨)── Envoy upstream 连接
   后端 TCP 窗口缩,send 慢下来 ◀── 内核 TCP 流控
```

这是代理做"背压(backpressure)"的标准姿势:**速度不匹配时,把"慢"这个信号沿着连接链路反向往上游传,而不是自己吃下去撑爆内存。** 没有这个机制,代理就是个无界队列,迟早 OOM;有了它,代理才是个"忠实转发"的角色——下游多快,上游就多快,全程不堆积。

> **钉死这件事**:watermark 不是孤立的内存限制,它通过 callback 串到 connection 层、再到 HCM、再到对端连接,形成**全链路反压**。本质是"下游慢,这个信号要能反向往上游传"。没有这套,代理就是个无界队列;有了它,代理才是"忠实转发"——下游多快,上游就多快。

### BufferMemoryAccount:更全局的内存账本

单个 buffer 的 watermark 解决了"单 buffer 不能无界涨"。但 Envoy 还有个更全局的问题:**整个 Envoy 进程的所有 buffer 加起来,也不能无界涨**。一个下游慢,可能它的 buffer 才几百 KB;但如果同时有一万个这样的下游连接,每个 buffer 都涨到接近 high watermark,总和也是几十 GB——单 buffer watermark 管不住这种"全局累积"。

这就是 [`BufferMemoryAccount`](../envoy/envoy/buffer/buffer.h#L107-L138) 和 [`WatermarkBufferFactory`](../envoy/source/common/buffer/watermark_buffer.h#L194-L231) 存在的理由:每个 stream 可以绑一个 account,slice 创建时 `charge(capacity)`、销毁时 `credit(capacity)`(见 [`Slice` 构造](../envoy/source/common/buffer/buffer_impl.h#L59-L66) 和 [`callAndClearDrainTrackersAndCharges`](../envoy/source/common/buffer/buffer_impl.h#L319-L329))。account 跨多个 buffer 累加这个 stream 真正占的内存。

当 overload manager 检测到全局内存压力时,它调 [`WatermarkBufferFactory::resetAccountsGivenPressure`](../envoy/source/common/buffer/watermark_buffer.cc#L203-L239)——按"内存类(memory class,2 的幂次桶)"从大到小,重置占内存最多的那些 stream(`resetDownstream()`,直接 reset 这条流)。这是个**"挑大鱼"的过载保护**:与其让所有 stream 都慢,不如直接 reset 掉少数几个占内存最多的,把内存还给其他流。这个机制和 overload manager(P4-15 详讲)配合,是 Envoy 在极端过载下的最后一道防线。

> **钉死这件事**:单 buffer 有 watermark(防单 buffer 无界涨),全局有 `BufferMemoryAccount` + `WatermarkBufferFactory`(防所有 buffer 加起来无界涨)。后者按"内存类"分桶,overload 时挑最大的几个 stream reset——这是"挑大鱼"式过载保护,和"一刀切拒绝"比,能在保住大多数流的同时释放内存。

---

## 五、linearize:偶尔才付的代价

前面讲了 slice 分片、move、零拷贝、watermark——Buffer 默认是"不连续的 slice 序列"。但有些场景**真的需要一段连续内存**:TLS 库(`SSL_write`)要连续 buffer、某些二进制协议解析要连续字段、Lua filter 要把 body 喂给 Lua 字符串。

### 不这样会怎样:每个需要连续内存的地方都自己做拼接

如果 Buffer 不提供"铺平"的接口,每个需要连续内存的 filter 都得自己写:`std::string s; for (slice : slices) s.append(slice.data, slice.size);`——重复代码、且每个 filter 各自 copy 一份。更糟的是,这种 copy 是隐性的(filter 看起来只是"用了一下 buffer",背后却 memcpy 了整段)。

### 所以这样设计:linearize 显式付一次 copy

Envoy 提供 [`linearize(uint32_t size)`](../envoy/source/common/buffer/buffer_impl.cc#L289-L310):把 buffer 前 `size` 字节铺平成一段连续内存,返回这段内存的指针。

```cpp
// (简化示意,非源码逐字)
void* OwnedImpl::linearize(uint32_t size) {
  RELEASE_ASSERT(size <= length(), "...");
  if (slices_.empty()) return nullptr;
  if (slices_[0].dataSize() < size) {
    // 前 size 字节跨越了多个 slice,需要 copy 到一个新的大 slice
    Slice new_slice{size, account_};
    Slice::Reservation reservation = new_slice.reserve(size);
    copyOut(0, size, reservation.mem_);   // ★ memcpy 前 size 字节到新 slice
    new_slice.commit(reservation);
    drainImpl(size);                       // 把原来的前 size 字节 drain 掉
    slices_.emplace_front(std::move(new_slice));
    length_ += size;
  }
  return slices_.front().data();           // 返回连续内存指针
}
```

注意它的精妙:**只有当前 `size` 字节已经连续(都在第一个 slice 里)时,`linearize` 是 O(0) 的——直接返回 `slices_[0].data()`**。只有当 `size` 跨越了 slice 边界、第一个 slice 装不下时,才真的 memcpy 到一个新 slice 里。

这是默认情况下**不付 copy 代价**的关键:大多数 filter 读字节时,读的量小于一个 slice(16KB),本来就在第一个 slice 里、不需要 linearize。只有那些确实需要跨 slice 连续内存的场景(codec 解析一个跨 slice 的字段、TLS 要连续 buffer),才付这次 copy。

看源码里谁在调 `linearize`,就知道哪些场景"真的需要连续内存":

- **TLS**:[`ssl_socket.cc#L357`](../envoy/source/common/tls/ssl_socket.cc#L357) `SSL_write(write_buffer.linearize(bytes_to_write), ...)`——BoringSSL 的 `SSL_write` 要连续 buffer。
- **Thrift / Mongo / 二进制协议 codec**:见 [`thrift_proxy` 多处](../envoy/source/extensions/filters/network/thrift_proxy/)、[`mongo_proxy/bson_impl.cc`](../envoy/source/extensions/filters/network/mongo_proxy/bson_impl.cc#L39)——解析固定长度的协议字段。
- **Lua filter**:[`lua_filter.cc#L474`](../envoy/source/extensions/filters/http/lua/lua_filter.cc#L474)——Lua 字符串需要连续内存。
- **SSE 解析**:[`sse_parser.h`](../envoy/source/common/http/sse/sse_parser.h#L30)、HTTP/2 codec 等。

这些都是"协议解析 / 加密"类场景——它们要连续内存是因为底层库(OpenSSL/BoringSSL、Lua VM)或协议格式(固定字段跨字节)要求。**Envoy 的态度是:不为了这些少数场景把整个 buffer 都做成连续(那会牺牲 add/drain 的 O(1)),而是在这些场景局部 linearize、付一次 copy。** 这是"快路径(fast path)和慢路径(slow path)分离"的典型设计——和《内存分配器》那本的 fast/slow path 是同一思路。

> **钉死这件事**:`linearize` 是个**显式的、局部的、偶尔才付的 copy**。默认 Buffer 是 slice 序列(不连续),只有 TLS/codec/Lua 这些真的需要连续内存的场景才 linearize 一段。这是"不为少数场景拖累多数场景"的取舍——快路径(filter 间 move、零拷贝 read)要极致,慢路径(偶尔的 linearize)付得起。

---

## 六、技巧精解:两个最硬核的实现细节

正文把 Buffer 的全貌拆了一遍。这里挑两个最硬核的实现细节,配真实源码 + 反面对比,单独拆透。

### 技巧一:reserveForRead 的"预留可能被写的空间",为什么比"读到临时缓冲再 add"快几个数量级

这个技巧是零拷贝 read 的核心,但它"为什么 sound、为什么快"很容易被读者一带而过。我们对照一个反例拆。

**反例:读到临时缓冲再 add(朴素实现)**

```cpp
// 反例(朴素,非 Envoy 实现)
Api::IoCallUint64Result naiveRead(Buffer::Instance& buffer, ...) {
  char tmp[65536];                                  // 临时缓冲
  ssize_t n = ::recv(fd, tmp, sizeof(tmp), 0);      // 内核 → tmp(copy 1)
  if (n > 0) buffer.add(tmp, n);                    // tmp → buffer(copy 2,多余!)
  return ...;
}
```

这个反例的两个问题:① **多余 copy**——`tmp` 到 buffer 的 memcpy 完全没必要(我们本可以让内核直接写进 buffer 的 slice);② **临时缓冲大小是个赌博**——开 64KB,但 slice 尾部可能只有 2KB 空间,add 时还得开新 slice;或者开 2KB,但内核一次能给你 64KB,得分多次 read。

**Envoy 的做法:reserveForRead 预留 slice 空间,readv 直写**

看 [`OwnedImpl::reserveWithMaxLength`](../envoy/source/common/buffer/buffer_impl.cc#L388-L440) 的核心逻辑(简化):

```cpp
// (简化示意,非源码逐字)
Reservation OwnedImpl::reserveWithMaxLength(uint64_t max_length) {
  Reservation reservation = Reservation::bufferImplUseOnlyConstruct(*this);
  // 第一步:尽量复用尾 slice 的 Reservable 段
  uint64_t reservable_size = slices_.empty() ? 0 : slices_.back().reservableSize();
  if (reservable_size >= max_length || reservable_size >= default_slice_size_ / 8) {
    RawSlice slice = slices_.back().reserve(...);    // 在现有 slice 尾部预留
    reservation_slices.push_back(slice);
    bytes_remaining -= slice.len_;
  }
  // 第二步:不够再开新 slice(默认 16KB 一个,最多 8 个 = 128KB)
  while (bytes_remaining != 0 && reservation_slices.size() < Reservation::MAX_SLICES_) {
    Slice::SizedStorage storage = slices_owner->newStorage();   // 可能从 free list 复用!
    reservation_slices.push_back({storage.mem_.get(), size});
    bytes_remaining -= ...;
  }
  ...
}
```

关键有两点:

1. **优先复用尾 slice 的 Reservable 段**:上一次 read 写到一半的 slice,尾部还有空闲空间(`reservableSize()`)。只要这空间够大(>= 2KB,即 16KB/8),就直接在它尾部预留——不开新 slice、不分配新内存。**这是"空间复用"——slice 的三段式布局在这里发挥了作用:Data 段后面天然就是可写的 Reservable 段。**
2. **不够再开新 slice,且从 thread-local free list 复用**:看 [`OwnedImplReservationSlicesOwnerMultiple`](../envoy/source/common/buffer/buffer_impl.h#L764-L809),它有个 [`thread_local free_list_`](../envoy/source/common/buffer/buffer_impl.h#L808):slice 用完归还(析构时 [`~OwnedImplReservationSlicesOwnerMultiple`](../envoy/source/common/buffer/buffer_impl.h#L769-L778) 把 `StoragePtr` push 回 free list),下次 newStorage 先从 free list 拿。**这是 thread-local 缓存——和 P1-02 讲的 thread-local 无锁归并是同一思路:每个 worker 自己的 free list,无锁、cache 友好。**

对比一下两者的内存分配次数:

| 场景 | 朴素实现 | Envoy reserveForRead |
|------|---------|---------------------|
| 临时缓冲 | 栈上 `char[65536]`(浪费栈、或堆分配) | 无临时缓冲,直接用 slice 空间 |
| slice 内存 | add 时若空间不够,`new` 新 slice | 优先复用尾 slice;不够从 thread-local free list 拿(无 `new`) |
| memcpy | 2 次(内核→tmp,tmp→buffer) | 1 次(内核→slice,直接写) |

**省下的不仅是 1 次 memcpy,还有大量的 `new`/`delete`**——在百万 QPS 下,这是 malloc 锁竞争、cache miss、内存碎片的总和。free list 复用让 slice 内存几乎是"零分配"——这就是为什么 Envoy 的 Buffer 能扛住极端吞吐。

> **钉死这件事**:`reserveForRead` 的精妙不在 readv 本身,而在 ① 优先复用尾 slice 的 Reservable 段(三段式布局的红利);② 新 slice 从 thread-local free list 拿(P1-02 thread-local 无锁思路的复用);③ 消除了临时缓冲这次多余 copy。**这是"快路径要 cache 友好、要零分配"原则在 buffer 层的完美落地**——和《内存分配器》那本的 thread-local cache 是同一类技巧。

### 技巧二:Watermark 的迟滞(low = high/2)与边沿触发,为什么能避免抖动

这个技巧看起来简单("low 设成 high 的一半嘛"),但它背后的控制论思想、以及"不这么写会怎样"的反例,值得拆透。

**反例:low 紧挨 high(比如 low = 0.9 × high)**

假设 high = 100MB,low = 90MB。下游慢,buffer 从 89MB 涨到 100MB,触发 `above_high_watermark_()`——反压上游。上游停了,buffer 慢慢被下游消费,跌到 89MB——触发 `below_low_watermark_()`——恢复上游。上游又推,buffer 又涨回 100MB——又反压……

这就是**抖动(thrashing)**:buffer 在 89~100MB 之间快速震荡,`above_high_watermark_` 和 `below_low_watermark_` 被高频来回调用。每次调用都是个 callback,可能触发 connection 层的状态变化(注册/注销读事件)、HCM 的暂停/恢复——这些都不是免费的。高频抖动下,CPU 大量消耗在 watermark 回调上,反而拖慢了真正的转发。

更糟的是:如果反压上游的机制是 `read disable`(停止从 socket 读),高频抖动意味着频繁 enable/disable 读事件——epoll 的 `EPOLL_CTL_MOD` 是系统调用,频繁调同样不便宜。

**Envoy 的做法:low = high/2,留足迟滞带**

```cpp
// watermark_buffer.cc:127
low_watermark_ = high_watermark / 2;
```

high = 100MB 时,low = 50MB。buffer 从 50MB 涨到 100MB(涨了 50MB)才触发反压;从 100MB 跌到 50MB(跌了 50MB)才触发恢复。中间这 50MB 的带子里,buffer 涨涨跌跌都不触发回调。**这是控制论里的迟滞(hysteresis)——和恒温器(温度到 25°C 才停热、跌到 20°C 才重新加热)、施密特触发器是同一思想。**

迟滞带越宽,抖动越不可能——代价是 buffer 可能在这个带子里"囤积"较多字节(最多到 high)。Envoy 选 high/2 是个经验值:既够宽(防抖动),又不至于太宽(buffer 平时囤太多浪费内存)。

**配合边沿触发(`above_high_watermark_called_` 标志),抖动被双重抑制:**

- 边沿触发:同一个状态(已经在 high 之上)下,buffer 继续涨不会重复触发反压。
- 迟滞:要触发"恢复",buffer 必须真的跌穿 low(不是蹭一下 high 就算)。

两者叠加,watermark 回调的触发频率被压到最低——只在"状态真正翻转"时调一次。这是生产级流控的标配,所有正经的背压机制(TCP 拥塞控制、gRPC flow control、Kafka consumer)都有类似的迟滞设计。

> **钉死这件事**:watermark 的 low = high/2 不是拍脑袋,是**迟滞(hysteresis)**——防抖动的经典控制论手段。配合边沿触发(`above_high_watermark_called_` 标志只记"上次状态"),watermark 回调只在状态真翻转时调一次。**这是"流控不能高频抖动"这个普遍原则在 Envoy buffer 层的具体落地。**

---

## 七、架构演进:从 evbuffer 到 Slice + SliceDeque

诚实交代一下 Buffer 实现的演进史(它影响你读源码):

1. **早期(2016~2019):基于 libevent 的 evbuffer**。Envoy 一开始重度依赖 libevent,Buffer 内部就是 `evbuffer`——一个链表 of `evbuffer_chain`(每段连续内存)。`evbuffer_add`/`evbuffer_drain`/`evbuffer_search` 都是 libevent 提供的。这个阶段"Buffer 是链表"的说法是对的。

2. **中期(2019~2021):自家 Slice + 自家 deque**。Envoy 把 libevent 的 buffer 抽象剥掉,自己写了 `Slice` 类和 `SliceDeque`(环形队列)。为什么?`evbuffer` 是通用库,有些 Envoy 特殊需求(watermark、fragment 的 releasor 回调、drain tracker)在 evbuffer 上 hack 不优雅。自家实现可以量身定做——benchmark 显示 `std::deque` 都追不上老 evbuffer,于是写了 ring buffer([`SliceDeque` 注释](../envoy/source/common/buffer/buffer_impl.h#L418-L425) 明说了这点)。

3. **近期(2021~):Watermark 统一 + BufferMemoryAccount + io_uring fragment**。`WatermarkBuffer` 作为 `OwnedImpl` 的子类(见 [`watermark_buffer.h#L24`](../envoy/source/common/buffer/watermark_buffer.h#L24))统一了带 watermark 的 buffer;`BufferMemoryAccount` 引入了跨 buffer 的全局内存账本;`io_uring` 多拍读通过 `BufferFragment` 实现了真正的外部内存零拷贝。`OwnedImpl` 本身甚至有个 [`TODO(antoniovicente)` 注释](../envoy/source/common/buffer/buffer_impl.h#L700-L701)说要"merge OwnedImpl 和 WatermarkBuffer"——这个合并还没做完。

> **注:此处和老资料不符**。大量博客(尤其 2019 年前的)讲 Envoy Buffer 还说是 `evbuffer` 链表——那是**老版本过时印象**。读本书时的当前 master(`df2c77d`,1.39.0-dev),Buffer 是 `Slice` + `SliceDeque`(环形队列),不再是 libevent `evbuffer`。读源码以 `source/common/buffer/buffer_impl.{h,cc}` 为准。

另外一个演进点是 **io_uring**:Envoy 已经有 `io_uring` 的实验性支持([`source/common/io/`](../envoy/source/common/io/) 和 [`source/common/network/io_uring_socket_handle_impl.{h,cc}`](../envoy/source/common/network/io_uring_socket_handle_impl.h))。io_uring 的零拷贝(多拍读 + buffer pool)是比传统 `readv` 更进一步的优化——前文讲 `BufferFragment` 接管内核 buffer pool 就是这个路径。但 io_uring 在 Envoy 里还不是默认(默认仍是 epoll/libevent + readv),生产部署需显式开启。涉及处(P2-05 listener、P6-22 扩展)会再提。

---

## 八、章末小结

### 回扣主线

本章是第 1 篇(地基)的第三章,也是数据面地基的最后一环。回扣全书二分法:

- **本章服务数据面**。Buffer 是 filter chain 上流转字节流的**容器**——没有它,filter 之间没法高效地传字节(每过一层就 copy,性能崩塌);有了它,filter 间 move、零拷贝 read,数据面才能扛住百万 QPS。它是 P1-03 dispatcher 喂进来的字节"用什么装"的答案,也是后面所有 filter(listener filter / network filter / HCM / http filter / router)操作的直接对象。
- **承接 P1-03**:P1-03 讲 dispatcher 怎么把"socket 可读"变成回调喂给 filter;本章讲这个回调被喂进来的字节流**在 filter 间用什么容器传**——从"dispatcher 喂字节"一句话接过来。
- **引出 P2-05**:从下一章开始,我们真正进入"一条流量的旅程"的第一站——Listener。Listener 接受连接、把连接分给 worker,然后字节流就开始穿 filter chain 了。Buffer 就是这趟旅程里 filter 之间传递的"货物"——P2-05 起,我们会看到这个货物怎么从内核 socket 一路穿到 router、再到 upstream。

### 五个为什么

1. **为什么 Buffer 用一串 Slice 分片,而不是连续的 std::string?**——连续数组在 filter 间 copy、在头尾插入/删除都是 O(n);slice 分片让 drain(头)和 add(尾)都是 O(1),且天然适配 `readv`/`writev` 的 scatter-gather IO。

2. **为什么 filter 之间传 Buffer 是 move 不是 copy?**——海量流量穿十几个 filter,每层 copy 会让 memcpy 吃光 CPU。move 搬 slice 的所有权(`unique_ptr` 转移 + 偏移量赋值),O(1) 完成;小切片(<512B)才合流 memcpy(防碎片化)。sound 的前提是 `unique_ptr` 所有权清晰 + drain tracker 随字节搬。

3. **为什么 read 要拆成 reserveForRead + readv + commit 三步?**——"实际读了多少字节"只有 readv 返回后才知道。拆成两步:先 reserve 一大片可能被写的空间(slice 的 Reservable 段或新 slice),内核 readv 直写这片空间,commit 时只承认实际写的字节数(挪 reservable_ 偏移)。消除了"读到临时数组再 memcpy 进 buffer"的多余 copy。

4. **为什么 Buffer 要有 watermark?为什么 low = high/2?**——上下游速度不匹配时,无界 buffer 会 OOM。watermark(high/low)配合反压回调(停读 / 通知 connection),把"下游慢"这个信号反向往上游传——全链路背压。low = high/2 是迟滞(hysteresis),防回调高频抖动;边沿触发(`above_high_watermark_called_` 标志)进一步压低触发频率。

5. **为什么有 linearize?为什么默认不 linearize?**——少数场景(TLS、二进制 codec、Lua)真的需要连续内存。Envoy 默认保持 slice 序列(快路径要 O(1) add/drain),只在这些场景局部 linearize、付一次 copy。这是"不为少数场景拖累多数场景"的快/慢路径分离。

### 想继续深入往哪钻

- **Buffer 源码**:[`source/common/buffer/buffer_impl.{h,cc}`](../envoy/source/common/buffer/buffer_impl.h) 是核心,`Slice`/`SliceDeque`/`OwnedImpl` 都在这里;[`watermark_buffer.{h,cc}`](../envoy/source/common/buffer/watermark_buffer.h) 是带 watermark 的子类;接口在 [`envoy/buffer/buffer.h`](../envoy/envoy/buffer/buffer.h)。
- **零拷贝 read 全路径**:`IoSocketHandle::read`([`io_socket_handle_impl.cc#L98-L111`](../envoy/source/common/network/io_socket_handle_impl.cc#L98-L111))→ `RawBufferSocket::doRead`([`raw_buffer_socket.cc#L16-L49`](../envoy/source/common/network/raw_buffer_socket.cc#L16-L49))→ `ConnectionImpl` 里 `transport_socket_->doRead(*read_buffer_)`。顺着这条链能看到字节怎么从内核进 buffer、再被 filter 消费。
- **watermark 反压全链路**:[`connection_impl_base.cc#L89-L110`](../envoy/source/common/network/connection_impl_base.cc#L89-L110) 的 `onFilterAboveHighWatermark`/`onFilterBelowLowWatermark` → HCM 的 `onAboveWriteBufferHighWatermark` → 暂停 upstream 读。P3-08 HCM 会接着拆这条链。
- **io_uring 零拷贝**:[`source/common/io/io_uring_worker_impl.cc#L515-L539`](../envoy/source/common/io/io_uring_worker_impl.cc#L515-L539) 看多拍读怎么用 `BufferFragmentImpl` 接管内核 buffer pool。
- **对照其他系统**:Buffer 的 slice 分片 + move,和《LevelDB》的 WriteBatch(slice 化的写批)、《内存分配器》的 thread-local free list、《gRPC》的 Slice(absl::cord 风格的分片字符串)是同一类思想——分片 + 引用/所有权,而非连续 + copy。本书不讲它们,只讲 Envoy Buffer;感兴趣可去翻那几本。

### 引出下一章

第 1 篇(地基)到这里讲完了:线程模型(P1-02)+ 事件引擎(P1-03)+ Buffer(P1-04)——Envoy 数据面的三大支柱。从此往后,我们要真正跟着一条流量走它的旅程。下一章 P2-05,我们从旅程的第一站开始:**Listener——Envoy 怎么监听端口、怎么用 SO_REUSEPORT 把连接分给 worker、怎么 drain 优雅下线**。Buffer 这个"货物",就从 listener 接受的连接里开始它的旅程。

> **下一章**:[P2-05 · Listener:监听端口,接受连接](P2-05-Listener-监听端口接受连接.md)
