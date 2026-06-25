# 第 6 篇 · 第 21 章 · 性能:slice、arena、零拷贝、压缩

> **核心问题**:gRPC core 要让一次方法调用穿过十几个 filter、跨过 chttp2 transport、变成 HTTP/2 帧、再在另一端穿回来,中间还可能要压缩、要拆批、要分片。一条 1MB 的消息,从应用层塞进 stub,到对面 handler 收到,这一路至少要经过十次"传递"。如果每次传递都把那 1MB 字节老老实实拷一遍,十次就是十次拷贝、十次内存分配——光是这个开销,就足以让 gRPC 在高吞吐场景下垮掉。那 gRPC core 到底怎么做到,让消息和元数据在 filter 栈里**几乎不拷字节**?一次调用用到的几十个临时对象(metadata、message、promise 状态),又怎么做到**分配极快、还不用一个个回收**?压缩、连接复用这些"看得见的"优化之外,gRPC 性能的根,究竟扎在哪几个"看不见的"数据结构里?

> **读完本章你会明白**:
> 1. 为什么 gRPC core 把一切"字节缓冲"统一抽象成 **slice**——一个 32 字节定长、自带原子引用计数的载体,以及它凭什么让"传消息只挪指针不拷字节"。
> 2. 为什么一次 call 的所有临时对象都从 **arena** 分配——一个 bump pointer、无 per-free、call 结束整体丢弃的内存池,以及它凭什么比"每个对象 malloc/free"快一个量级。
> 3. slice 和 arena 怎么**配合**:slice 持引用计数让字节零拷贝,arena 让承载这些 slice 的容器(metadata_batch、message、promise)分配快且不用逐个回收。
> 4. 压缩(gzip/deflate)什么时候压、什么时候不压,以及 gRPC 为什么**当前版本没有 zstd**(只有 zlib 系)。
> 5. 为什么 arena"不回收单块"在语义上是 sound 的——call 生命周期清晰,整体丢弃不会泄漏。

> **如果一读觉得太难**:先只记住三件事——① 一切字节缓冲都是 **slice**,传 slice = 传指针 + 引用计数 +1,不拷字节;② 一次 call 的临时对象都从 **arena** 顺序分配,call 结束整块丢弃,没有 per-free;③ 这两件加起来,就是 gRPC 在 filter 栈里做到零拷贝和低开销分配的根。

---

## 〇、一句话点破

> **gRPC core 的性能根,扎在两个数据结构上:slice 用原子引用计数让字节在 filter 栈里零拷贝地传递,arena 用 bump pointer 让一次 call 的几十个临时对象分配极快、整体回收、不逐个 free。**

这是结论,不是理由。本章倒过来拆:先讲清"朴素地每个对象 malloc/free、每次传递都拷字节"会撞上什么墙,再讲 slice 的引用计数怎么破拷贝墙、arena 的 bump pointer 怎么破分配墙,最后讲这两件怎么配合、压缩怎么叠上去。三章二分法归属:**协议 / 框架(招牌)**——这一章讲的不是某个协议细节或某个框架机制,而是横跨协议层(消息变成 DATA 帧)和框架层(filter 栈传递)的**性能基础设施**,是 gRPC core 又快又省的根。

---

## 一、为什么必须先讲反例:朴素方案的两堵墙

要理解 slice 和 arena 的妙处,得先看清它们在反击什么。我们把 gRPC core 的真实工作场景摆出来,然后问:如果用最朴素的方式做,会怎样?

### 场景:一条消息穿十层 filter

回忆 P3-11 filter stack:一次 gRPC 调用,客户端要穿过一串 filter——鉴权 filter、压缩 filter、message_size filter、census/otel filter、retry filter、client_channel filter……可能十几个。每个 filter 都要"看到"这条 message(以及它携带的 metadata_batch)。服务端同理,收到的 message 也要穿过一串 filter 才到 handler。

这十几个 filter,不是串成一个调用链就完事——它们很多是**异步**的(比如 retry 要等重试结果、census 要等 span end)。在经典 callback 架构里,这导致层层回调嵌套;在新的 Promise 架构(P3-11、P3-12 讲的 call spine)里,这变成一条 promise 流水线。但不管哪种架构,**message 这条字节流,物理上要从 filter A 流到 filter B、再到 filter C**。

### 第一堵墙:拷贝墙

朴素做法:每次 filter 之间传 message,都拷一遍字节。

```
  应用层 message (1MB)
   │ 拷贝
   ▼
  filter A (鉴权)        ← 拷贝1:1MB
   │ 拷贝
   ▼
  filter B (压缩)        ← 拷贝2:1MB
   │ 拷贝
   ▼
  filter C (message_size) ← 拷贝3:1MB
   │ ...
   ▼ (十层 filter,十次拷贝)
  chttp2 transport
```

一条 1MB 的消息穿十层 filter,就是 10 次 1MB 的 memcpy + 10 次 1MB 的 malloc + 10 次 1MB 的 free。在海量并发调用下(比如每秒 10 万次调用,平均消息 100KB),光是 filter 栈里的拷贝,每秒就要搬运**几十 GB** 的字节、做几十万次分配释放。CPU 全花在 memcpy 和 malloc 上了。

### 第二堵墙:分配墙

朴素做法:每个临时对象(metadata_batch、message、每个 filter 的 per-call 状态、每个 promise 的可调用体)都 `new` 一个,用完 `delete`。

一次调用要 new 几十个对象。每个 new/delete 的开销包括:向系统申请内存(可能 syscall)、维护 heap 元数据、可能的锁竞争(glibc malloc 的 arena 锁)、**内存碎片**(频繁 new/delete 会让 heap 布满碎片,cache 不友好、RSS 膨胀)。几十个对象 × 百万 QPS = 每秒上亿次 new/delete。这还没算碎片化带来的间接开销。

> **不这样会怎样**:朴素方案的世界,是"每传一层 filter 拷一遍字节 + 每个临时对象独立 new/delete"。这套在低并发下感觉不到,但放到 gRPC core 的真实负载(海量并发、消息可能不小、filter 栈不短)下,拷贝和分配的开销会淹没业务逻辑的 CPU。**slice 和 arena,就是分别破这两堵墙的。**

---

## 二、slice:用原子引用计数破拷贝墙

### slice 是什么:一个 32 字节的"字节缓冲句柄"

gRPC core 里,一切"字节缓冲"——一条 message 的 payload、一个 header 的 key/value、一段 HTTP/2 帧的数据——都被统一抽象成 **slice**。它不是裸的 `char*` + `length`,而是一个**自带引用计数**的定长结构。

slice 的 C 定义在 `include/grpc/impl/slice_type.h:62-74`(注意,不在 `slice.h`——`slice.h` 是 C++ 包装类):

```c
// include/grpc/impl/slice_type.h:62-74
struct grpc_slice {
  struct grpc_slice_refcount* refcount;          // L63 引用计数指针
  union grpc_slice_data {                        // L64
    struct grpc_slice_refcounted {               // L65 refcount != null 时
      size_t length;
      uint8_t* bytes;
    } refcounted;
    struct grpc_slice_inlined {                  // L69 refcount == null 时(内联)
      uint8_t length;
      uint8_t bytes[GRPC_SLICE_INLINED_SIZE];    // L71 ~23 字节
    } inlined;
  } data;
};
```

整个 `grpc_slice` 是个 32 字节的定长结构,可以按值拷贝传递。它的核心是那个 `refcount` 指针,指向一个 `grpc_slice_refcount` 对象——这就是引用计数的真身。

### 引用计数真身:grpc_slice_refcount

`src/core/lib/slice/slice_refcount.h:28` 的完整定义(原文,逐字一致):

```cpp
// src/core/lib/slice/slice_refcount.h:28
struct grpc_slice_refcount {
 public:
  typedef void (*DestroyerFn)(grpc_slice_refcount*);          // L30

  static grpc_slice_refcount* NoopRefcount() {                 // L32
    return reinterpret_cast<grpc_slice_refcount*>(1);          // L33 哨兵值 1
  }

  explicit grpc_slice_refcount(DestroyerFn destroyer_fn)       // L43
      : destroyer_fn_(destroyer_fn) {}

  void Ref(grpc_core::DebugLocation location) {                // L46
    auto prev_refs = ref_.fetch_add(1, std::memory_order_relaxed);   // L47 原子 +1,relaxed
    ...
  }
  void Unref(grpc_core::DebugLocation location) {              // L52
    auto prev_refs = ref_.fetch_sub(1, std::memory_order_acq_rel);  // L53 原子 -1,acq_rel
    ...
    if (prev_refs == 1) {                                      // L57 我是最后一个
      destroyer_fn_(this);                                     // L58 触发释放
    }
  }

  bool IsUnique() const { return ref_.load(...) == 1; }        // L65

 private:
  std::atomic<size_t> ref_{1};          // L68 原子计数,初值 1
  DestroyerFn destroyer_fn_ = nullptr;  // L69 降为 0 时调用
};
```

这套引用计数有三个细节值得钉死:

**细节一:纯原子操作,无锁无 CAS**。`Ref` 是 `fetch_add(1, relaxed)`(L47)——一次原子加,无锁。`Unref` 是 `fetch_sub(1, acq_rel)`(L53)——一次原子减,然后判断 `prev == 1`(L57,意思是"减之前是 1,减完是 0,我是最后一个")。**没有 CAS 循环、没有自旋锁**。这是引用计数能做到极快的关键:热路径上就是一条原子指令。

**细节二:内存序的精妙**。为什么 Ref 用 relaxed 而 Unref 用 acq_rel?因为 Ref 只是"多一个引用者",不涉及释放,relaxed 够了;但 Unref 在"我是最后一个"时要释放内存,必须保证"之前所有引用者对这块内存的写"都对 destructor 可见——acq_rel 的 release 语义保证之前的写不会重排到 unref 之后,acquire 语义保证 destructor 看到所有写。这套内存序配对,是无锁引用计数做到**正确**(sound)的关键。

**细节三:多态靠函数指针,不靠虚函数**。注意 `DestroyerFn destroyer_fn_`(L69)是个函数指针,不是 C++ 虚函数。这意味着 `grpc_slice_refcount` 是个 POD-like 结构,没有 vtable 开销。不同的 slice 类型(从 malloc 来的、从静态字符串来的、从 `std::string` move 来的)有不同的 destroyer,靠这个函数指针实现多态。看真实的派生类(都在 `slice.cc`,通过持有不同 destroyer 实现):

- `NewSliceRefcount`(slice.cc:55)——`grpc_slice_new_with_user_data` 用,destroyer 调用户回调。
- `MovedStringSliceRefCount`(slice.cc:130)——持有 `std::string&&`,destroyer delete 那个 string。
- `grpc_slice_malloc`(slice.cc:222)——placement new 一个裸 refcount,destroyer 是 `delete[]`。

### 三种 slice:内联、引用计数、静态

slice 的 `refcount` 指针有三种取值,对应三种 slice(`slice_type.h:62-74`):

```
   grpc_slice (32 字节定长)
   ┌─────────────────────────────────────────────┐
   │ refcount 指针                                 │
   ├─────────────────────────────────────────────┤
   │ union data {                                  │
   │   refcounted {length, bytes*}  ← refcount!=null 时用
   │   inlined   {length, bytes[23]} ← refcount==null 时用
   │ }                                             │
   └─────────────────────────────────────────────┘

   refcount 的三种取值:
   ┌────────────────┬────────────────────────────────────┐
   │ nullptr (0)    │ 内联 slice:数据直接塞在 bytes[23] 里,无引用计数
   │                │ 小数据(短 header 值)用,拷贝即拷字节
   ├────────────────┼────────────────────────────────────┤
   │ NoopRefcount(1)│ 静态 slice:指向永不释放的外部内存(如字面量字符串)
   │                │ FromStaticString 用,Ref/Unref 是空操作
   ├────────────────┼────────────────────────────────────┤
   │ 真实对象指针    │ 引用计数 slice:动态分配的字节,带原子引用计数
   │                │ 大数据(message payload)用,这是零拷贝的主角
   └────────────────┴────────────────────────────────────┘
```

**内联 slice**(refcount == null):数据很短(≤ 23 字节,比如短 header 值 `"5S"`、`"grpc"`),直接塞进 `bytes[GRPC_SLICE_INLINED_SIZE]`,拷贝 slice 就是拷这 23 字节,无引用计数、无堆分配。这是为小数据优化的:小数据做引用计数反而比直接拷还慢。

**静态 slice**(refcount == `NoopRefcount()` 即 `(grpc_slice_refcount*)1`):指向永不释放的外部内存,比如字面量字符串 `"content-type"`。`NoopRefcount()` 返回哨兵值 1,Ref/Unref 对它做任何事都没意义(永远不会释放),所以 `FromStaticString`(`slice.h:226`)直接用这个哨兵。

**引用计数 slice**(refcount 指向真实对象):这是大数据(message payload)用的。字节动态分配,带一个 `grpc_slice_refcount`,传 slice 只 `Ref()`(+1),不拷字节。这是零拷贝的主角。

> **钉死这件事**:slice 把"字节缓冲"分成三档——**小数据内联(免分配)、静态数据哨兵(免计数)、大数据引用计数(免拷贝)**。三档各有最优策略,这是 gRPC 在"字节缓冲"这个最底层抽象上做到的精细化。一个 `grpc_slice` 32 字节定长,按值传递,refcount 指针决定它走哪一档。

### 零拷贝的核心:Ref 只 +1,Unref 到 0 才释放

现在看零拷贝到底怎么做到的。假设 filter A 要把一个引用计数 slice 传给 filter B(两个 filter 都要用这块字节):

```cpp
// filter A 持有 slice s
grpc_slice s = ...;  // refcount 指向 R,R->ref_ == 1

// 传给 filter B(零拷贝!)
grpc_slice s_copy = s;                    // 按值拷贝 32 字节的 grpc_slice 结构
s_copy.refcount->Ref();                   // 关键:R->ref_ 原子 +1,变成 2
// 现在 s 和 s_copy 的 bytes 指向同一块内存!

// filter B 用 s_copy,filter A 还能用 s,共享同一份字节
// ...

// filter A 用完
s.refcount->Unref();                      // R->ref_ -1,变成 1(不是 0,不释放)

// filter B 用完
s_copy.refcount->Unref();                 // R->ref_ -1,变成 0,destroyer 触发,释放字节
```

**整个过程中,字节一字未动。** 传递 slice = 拷贝 32 字节的 `grpc_slice` 结构 + 一次原子 `fetch_add(1)`。10 层 filter 传 10 次,就是 10 次原子加,字节还是那一块。这就是零拷贝。

> **不这样会怎样**:如果没有引用计数,朴素做法是每传一层 filter `memcpy` 一遍字节(第一堵墙),或者用 `shared_ptr<vector<uint8_t>>` 这种重对象(每次拷贝要动 shared_ptr 的控制块,比 slice 重)。slice 的设计,是把"引用计数"和"字节缓冲"焊在一起,做成一个 32 字节的轻量结构,Ref/Unref 就一条原子指令,做到极致轻。

### 子串切片:连指针偏移都不拷字节

零拷贝还有一个更精细的场景:**从一个大 slice 切一段出来当子串**。比如 gRPC framing(P2-08)要把一条 message 从一个大缓冲里切出来,或者 HPACK 解码要从一段缓冲里切出一个 header 值。这时用 `sub_no_ref`(`src/core/lib/slice/slice.cc:242`):

```cpp
// src/core/lib/slice/slice.cc:242-266 (sub_no_ref 关键行)
static grpc_slice sub_no_ref(grpc_slice source, size_t begin, size_t end) {
  ...
  subset.refcount = source.refcount;                          // L253 复用父 slice 的 refcount
  subset.data.refcounted.bytes = source.data.refcounted.bytes + begin;  // L255 指针偏移
  subset.data.refcounted.length = end - begin;                // L256 改长度
  return subset;
}
```

**只改指针和长度,字节一字不动。** 子串 slice 和父 slice 共享同一块内存、同一个 refcount。多个子串切片可以指向同一个大缓冲的不同区间,彼此零干扰、零拷贝。这在解析协议(HTTP/2 帧、gRPC framing)时极其有用——一大块网络缓冲读进来,切出各种 header 值、message 体,全程不拷字节。

> **钉死这件事**:slice 的零拷贝,根在 `grpc_slice_refcount` 这个原子引用计数 + 函数指针 destroyer。传 slice 是 `Ref()`(+1),用完 `Unref()`(-1),到 0 才释放。子串切片是复用父 refcount + 指针偏移。**10 层 filter 传一条 1MB 消息,字节还是那 1MB,只多了 10 次原子加。** 这是 gRPC core 在 filter 栈里不拷字节的根。

---

## 三、arena:用 bump pointer 破分配墙

slice 解决了"字节不拷",但 message、metadata_batch、每个 filter 的 per-call 状态、每个 promise 的可调用体——这些**对象本身**还是要分配的。第二堵墙(分配墙)怎么破?答案是 arena。

### arena 是什么:一次 call 一个内存池

gRPC core 的设计是:**一次 call,配一个 arena**。这次 call 用到的所有临时对象(metadata_batch、message、promise 状态、filter 的 call 数据),都从这个 arena 分配。call 结束,整个 arena 一次性丢弃,所有对象同时消失。

这条路是从 channel 开始的。`src/core/lib/surface/channel.cc:62-71` 里,每个 channel 持有一个 per-channel 的 `CallArenaAllocator`:

```cpp
// src/core/lib/surface/channel.cc:62-71 (Channel 构造,简化示意)
Channel::Channel(...)
    : ...
      call_arena_allocator_(MakeRefCounted<CallArenaAllocator>(
          channel_args.GetObject<ResourceQuota>()
              ->memory_quota()
              ->CreateMemoryOwner(),       // 从 resource_quota 拿配额
          1024)),                           // 初始估计大小 1024 字节
      memory_allocator_(&call_arena_allocator_->allocator()) {}
```

每次建 call,channel 调 `call_arena_allocator_->MakeArena()` 拿一个新 `Arena`,传给 call。`CallArenaAllocator`(`src/core/call/call_arena_allocator.h:71-87`)继承自 `ArenaFactory`,核心方法就是:

```cpp
// src/core/call/call_arena_allocator.h:71-87
class CallArenaAllocator final : public ArenaFactory {
  RefCountedPtr<Arena> MakeArena() override {                          // L77
    return Arena::Create(call_size_estimator_.CallSizeEstimate(), Ref());  // L78
  }
  void FinalizeArena(Arena* arena) override;                           // L81
 private:
  CallSizeEstimator call_size_estimator_;                              // L86
};
```

注意 `CallSizeEstimator`(L86)——它根据历史 call 的实际内存用量,估计下一次 call 该给 arena 分配多大初始空间(`CallSizeEstimate()`)。`FinalizeArena`(`call_arena_allocator.cc:23-25`)在 arena 释放时回调,用 `arena->TotalUsedBytes()` 更新估计值(带 hysteresis 防抖动)。**所以 arena 的大小是自适应的**:先给个估计值(默认 1024),然后根据真实用量动态调整,既不太大(浪费)也不太小(频繁溢出)。

### 底层 Arena:bump pointer,无 freelist

真正干活的 `Arena` 类在 `src/core/lib/resource_quota/arena.h:156`。它的分配核心 `Alloc`(`arena.h:176-184`),是我见过的最干净的内存分配器之一:

```cpp
// src/core/lib/resource_quota/arena.h:176-184 (Alloc,逐字原文)
void* Alloc(size_t size) {
  size = GPR_ROUND_UP_TO_ALIGNMENT_SIZE(size);                        // L177 对齐
  size_t begin = total_used_.fetch_add(size, std::memory_order_relaxed);  // L178 原子 bump
  if (begin + size <= initial_zone_size_) {                           // L179 还在 zone 0 内
    return reinterpret_cast<char*>(this) + begin;                     // L180 this + 偏移
  } else {
    return AllocZone(size);                                           // L182 溢出→新 zone
  }
}
```

这就是一个**纯 bump pointer(顺序分配)**:

1. 把请求大小对齐(L177)。
2. 原子地把 `total_used_` 加上这个大小,拿到之前的偏移 `begin`(L178)——一次 `fetch_add`,无锁。
3. 如果还在初始 zone(zone 0)范围内(L179),直接返回 `arena 基地址 + 偏移`(L180)。
4. 否则溢出了,分配一个新 zone(`AllocZone`,L182)。

**没有 freelist、没有合并、没有查找。** 分配就是"指针往前挪一下"。这比 malloc 的"维护 freelist、查找合适块、可能合并"快得多——尤其在海量小对象分配时(一次 call 几十个对象),arena 的 bump pointer 几乎是零开销。

```
   Arena 内存布局(zone 0 + 溢出 zone 链)
   ┌──────────────────────────────────────────────────────┐
   │ Arena 对象头(this)                                     │
   │  - initial_zone_size_: zone 0 大小                     │
   │  - total_used_: 当前 bump 偏移(原子)                  │
   │  - last_zone_: 溢出 zone 链尾指针                      │
   ├──────────────────────────────────────────────────────┤
   │ zone 0:bump pointer 顺序分配                            │
   │ ┌──────────┬──────────┬──────────┬─────────┬────────┐ │
   │ │ metadata │ message  │ promise  │ filter  │ (剩余) │ │
   │ │ _batch   │ 对象     │ callable │ call数据│ 空闲    │ │
   │ └──────────┴──────────┴──────────┴─────────┴────────┘ │
   │  ← total_used_ 指这里                                   │
   ├──────────────────────────────────────────────────────┤
   │ 溢出 zone 链(逆序单向链表,zone 0 装不下时分配)         │
   │   Zone_n ← Zone_{n-1} ← ... ← Zone_1                   │
   └──────────────────────────────────────────────────────┘
```

溢出 zone 由 `AllocZone`(`arena.cc:107-125`)分配,串成逆序单向链表(`Zone { Zone* prev; }`,`arena.h:332-334`)。arena.cc:108-112 的注释明确说:**zone 0 里没用完的空间是浪费的**("unused space in the initial zone is wasted")——这是 bump pointer 的代价,但比起"为每个对象维护 freelist"的开销,这点浪费完全值。

### 无 per-free:call 结束整体丢弃

arena 最反直觉的设计是:**没有 free 单个对象的 API**。只有整体 `Arena::~Arena`(`arena.cc:50`)/ `Arena::Destroy`(`arena.cc:102-105`),一次性释放所有 zone。

这就是"无 per-free"——一次 call 用了几十个对象,没有一个对象被单独 free。call 结束,整个 arena 析构,所有 zone(包括 zone 0 和溢出 zone 链)一次性还给 `memory_quota`(`arena.cc:57-58` 的 `allocator().Release(total_allocated_)`)。

```cpp
// arena.h:19-23 的头注释(原文)
// "Allows very fast allocation of memory, but that memory cannot be freed
//  until the arena as a whole is freed."
```

> **不这样会怎样**:朴素方案是每个对象 `new`/`delete`。一次 call 几十个对象,就是几十次 malloc + 几十次 free,每次 free 还要维护 heap 元数据、可能触发碎片整理。arena 把这几十次 malloc 压成"指针挪几十次"(每次一条原子指令),把几十次 free 压成"call 结束一次大释放"。在百万 QPS 下,这是几十倍的开销差。

### 为什么"无 per-free"是 sound 的:call 生命周期清晰

"不 free 单个对象"听起来像内存泄漏。但它 sound,因为 **call 的生命周期是清晰、有限的**:一次 call 从发起(`call_arena_allocator_->MakeArena()`)到结束(call 完成或取消,arena 析构),这段时间内所有从 arena 分配的对象都活着、都有效;call 一结束,它们一起死。中间没有"某个对象先死了、但 arena 还活着"的情况——如果有,那这个对象不该从 arena 分配。

关键约束是:**只有生命周期 ≤ call 的对象,才能从 arena 分配**。这正好覆盖了 gRPC 里绝大多数临时对象:metadata_batch(call 级)、message(call 级,在 call 内被消费)、promise 状态(call 级)、filter 的 per-call 数据(call 级)。这些对象本来就和 call 同生共死,用 arena 管理是天然的。

那 slice 呢?注意 slice 的字节缓冲**不一定**从 arena 分配——它可能从 arena 分配(`Arena::New` 一段),也可能是引用计数对象(独立 malloc)。引用计数的 slice 不受 arena 生命周期约束,它靠自己的 ref/unref 管理。这是 gRPC 内存模型的精妙分工:**arena 管 call 级对象的生命周期,slice 引用计数管跨 call / 跨 filter 的字节缓冲**。两者正交。

### ManagedNew:arena 回收时自动析构

有个细节:有些对象有非 trivial 的析构(比如持 `std::string` 的 promise callable),不能简单丢弃。arena 提供 `ManagedNew<T>`(`arena.h:201-206`):

```cpp
// src/core/lib/resource_quota/arena.h:201-206
template <typename T, typename... Args>
T* ManagedNew(Args&&... args) {
  auto* p = New<ManagedNewImpl<T>>(std::forward<Args>(args)...);  // 从 arena 分配
  p->Link(&managed_new_head_);                                    // 串到析构链表
  return &p->t;
}
```

ManagedNew 分配的对象被串到 `managed_new_head_` 链表(`arena.h:383`)。arena 析构时,`DestroyManagedNewObjects`(`arena.cc:89-100`)/ `Destroy`(`arena.cc:102-105`)遍历这个链表,统一调每个对象的析构。**这是 arena 唯一的"逐个处理"——但只在 arena 整体释放时做一次,不是每次 free**。所以它仍然保留了"无 per-free"的核心优势。

> **钉死这件事**:arena 是"一次 call 一个内存池,bump pointer 分配,call 结束整体丢弃"。它凭什么比 per-object malloc/free 快一个量级?因为① 分配是一条原子 `fetch_add`(无 freelist 查找);② 释放是一次大回收(无逐个 free);③ 自适应大小(`CallSizeEstimator`)减少溢出。它 sound 的前提是"只有 call 级对象进 arena",这个约束在 gRPC 里天然成立。

---

## 四、slice 和 arena 的配合:message 怎么在 filter 栈里传

slice 和 arena 不是孤立的两件事,它们在 gRPC core 里**配合**着用。最好的例子是 message 在 filter 栈里的传递。

### Message:一个持 SliceBuffer 的对象

`src/core/call/message.h:36-66` 的 `Message` 类(原文):

```cpp
// src/core/call/message.h:36-66
class Message {
 public:
  Message(SliceBuffer payload, uint32_t flags)                        // L40
      : payload_(std::move(payload)), flags_(flags) {}
  uint32_t flags() const { return flags_; }                           // L45
  SliceBuffer* payload() { return &payload_; }                        // L47 返回内部 SliceBuffer
  Arena::PoolPtr<Message> Clone() const {                             // L50
    return Arena::MakePooled<Message>(payload_.Copy(), flags_);       // L51 深拷贝(但 slice 仍不拷字节)
  }
 private:
  SliceBuffer payload_;                                              // L62 持一个 SliceBuffer
  uint32_t flags_ = 0;                                               // L63
};
using MessageHandle = Arena::PoolPtr<Message>;                        // L66 消息句柄
```

`Message` 内部持一个 `SliceBuffer`(`src/core/lib/slice/slice_buffer.h:54`),而 SliceBuffer 是一批 slice 的数组(`include/grpc/impl/slice_type.h:80-96`)。所以一条 message 的物理结构是:

```
   MessageHandle (= PoolPtr<Message>,unique_ptr 语义)
        │ 指向
        ▼
   Message 对象(从 arena 分配)
        │ 持有
        ▼
   SliceBuffer(一批 slice 的数组)
        │ 包含
        ▼
   [slice1, slice2, slice3, ...]   ← 每个 slice 是 {refcount, length, bytes*}
        │ bytes 指向
        ▼
   真正的字节缓冲(message payload,引用计数)
```

### 在 filter 栈里传:零拷贝 + 移动语义

message 以 `MessageHandle`(`= Arena::PoolPtr<Message>`,unique_ptr 语义,**移动**而非共享)在 filter 栈里传递。每个 filter 有 `OnClientToServerMessage(MessageHandle, ...)` / `OnServerToClientMessage(...)` 钩子,接收一个 MessageHandle、返回一个 MessageHandle。

传递时:move MessageHandle 只挪 `Message*` 指针(不拷 Message 对象,也不拷 payload)。如果某个 filter 要"看一眼但不改"message,它直接读 `message->payload()`,拿到 SliceBuffer 指针,读里面的 slice——全程零拷贝。

如果某个 filter 要**改写** message(比如压缩 filter 把 payload 压缩),它怎么做?看 `compression_filter.cc:103` 的 `CompressMessage`:它调 `message->payload()` 拿到 SliceBuffer,压缩后 `tmp.Swap(payload)`(`compression_filter.cc:140`)。`Swap` 只是交换 SliceBuffer 内部的几个指针(base_slices、count、length),**不拷字节**。压缩后的新 slice 当然是新分配的(压缩产生了新字节),但原始 message 的字节切片仍然靠引用计数管理。

> **钉死这件事**:slice 和 arena 的分工是——**arena 管"对象"的生命周期(Message、metadata_batch、promise),slice 引用计数管"字节"的所有权(payload)**。message 在 filter 栈里传,move MessageHandle 挪对象指针(arena 管),payload 字节靠 slice 引用计数(不拷)。两个机制正交,合起来做到"对象分配快、字节零拷贝"。

### 一个反例的完整对比

把朴素方案和 gRPC 方案完整对比一下,看一条 1MB message 穿 10 层 filter 的开销:

| 操作 | 朴素(per-malloc + 每次拷字节) | gRPC(arena + slice 引用计数) |
|------|------------------------------|------------------------------|
| 分配 Message 对象 | `new Message` 1 次(含 heap 开销) | `arena->New<Message>` 1 次(fetch_add) |
| 分配 payload 缓冲 | `new uint8_t[1MB]` 1 次 | `grpc_slice_malloc(1MB)` 1 次 |
| filter 间传递 10 次 | 每次拷贝 message + 拷贝 1MB 字节 | 每次 move MessageHandle + slice Ref(+1) |
| 切分子串(解析时) | 每个子串 memcpy | `sub_no_ref` 指针偏移 |
| call 结束回收 | 每个对象 `delete` + free 1MB | arena 整体丢弃 + slice unref 到 0 释放 |
| **10 层 filter 总字节拷贝** | **10 MB** | **0 字节** |
| **10 层 filter 总分配次数** | 几十次 malloc | 几十次 fetch_add |

光"10 层 filter 不拷字节"这一项,在 100KB 平均消息、10 万 QPS 的负载下,每秒就省下 **1 GB** 的 memcpy。这就是 slice + arena 的合力价值。

---

## 五、压缩:用 CPU 换带宽

零拷贝和 arena 解决的是"gRPC 内部不浪费 CPU"。但字节终究要过网络,网络带宽是另一道瓶颈。gRPC 提供压缩,用 CPU 换带宽。

### 压缩 filter 在哪

压缩 filter 在 `src/core/ext/filters/http/message_compress/compression_filter.{h,cc}`(目录下就这两个文件)。它分三个类(`compression_filter.h`):

- `ChannelCompression`(L65-119):核心 engine,持有算法配置,提供 `CompressMessage`/`DecompressMessage`/`HandleOutgoingMetadata`/`HandleIncomingMetadata`。
- `ClientCompressionFilter`(L121-180):客户端 filter。
- `ServerCompressionFilter`(L182-239):服务端 filter。

底层压缩实现在 `src/core/lib/compression/message_compress.{h,cc}`(注意在 `lib/compression/`,不在 `ext/`)。

### 一个重要修正:当前版本只有 gzip/deflate,没有 zstd

这里必须诚实交代一个源码事实,纠正一个常见误解:**当前 commit(1.83.0-dev)只支持 `GRPC_COMPRESS_DEFLATE` 和 `GRPC_COMPRESS_GZIP`,没有 zstd。** 看真实的算法枚举(`include/grpc/impl/compression_types.h:61-67`):

```c
// include/grpc/impl/compression_types.h:61-67
typedef enum {
  GRPC_COMPRESS_NONE = 0,   // L62
  GRPC_COMPRESS_DEFLATE,    // L63
  GRPC_COMPRESS_GZIP,       // L64
  /* TODO(ctiller): snappy */  // L65 ← TODO 的是 snappy,不是 zstd
  GRPC_COMPRESS_ALGORITHMS_COUNT  // L66
} grpc_compression_algorithm;
```

注意 L65 的 TODO 提的是 **snappy**,不是 zstd。许多资料(尤其讲新 gRPC 的)会说"gRPC 支持 gzip/zstd",那是更晚 commit 才加的 zstd;1.83.0-dev 这个版本,zstd 还没进来。所有压缩实现都基于 **zlib**(`message_compress.cc:26 #include <zlib.h>`):`grpc_msg_compress`(`message_compress.cc:169`)dispatch,DEFLATE→`zlib_compress(...,0)`,GZIP→`zlib_compress(...,1)`(L159-161),差别在 zlib 的 window bits 加 16 就是 gzip 格式(L104)。

### 什么时候压、什么时候不压

`ChannelCompression::CompressMessage`(`compression_filter.cc:103-155`)的决策逻辑:

```cpp
// src/core/ext/filters/http/message_compress/compression_filter.cc:116-119
if (algorithm == GRPC_COMPRESS_NONE || !enable_compression_ ||
    (flags & (GRPC_WRITE_NO_COMPRESS | GRPC_WRITE_INTERNAL_COMPRESS))) {
  return message;   // 不压:算法 NONE / 禁用压缩 / 调用方显式标记不压
}
```

三个"不压"的条件:① 算法是 NONE;② 压缩被全局禁用;③ 调用方显式标了 `GRPC_WRITE_NO_COMPRESS`。

然后尝试压缩(`grpc_msg_compress`,`compression_filter.cc:123`)。**关键**:压缩后如果反而变大,就放弃压缩、发原始数据。判断在 `zlib_compress` 内部(`message_compress.cc:107`):`output->length < input->length` 才算压成功了;否则返回 0,filter 据此不设 `GRPC_WRITE_INTERNAL_COMPRESS` flag,发未压缩数据。这自动过滤掉"小数据压了反而大"的情况——**没有显式的最小尺寸阈值**,靠"压缩后变大就回退"自然兜底。

### 压缩怎么协商:grpc-encoding 头

发送方在 `HandleOutgoingMetadata`(`compression_filter.cc:200-211`)里设两个头:

```cpp
// compression_filter.cc:200-211 (简化)
auto algorithm = outgoing_metadata.Take(GrpcInternalEncodingRequest())
                    .value_or(default_compression_algorithm());      // 拿算法
outgoing_metadata.Set(GrpcAcceptEncodingMetadata(),                 // 广播我支持的所有算法
                      enabled_compression_algorithms());
if (algorithm != GRPC_COMPRESS_NONE) {
  outgoing_metadata.Set(GrpcEncodingMetadata(), algorithm);          // 告诉对方我用了哪个
}
```

`grpc-encoding`(`GrpcEncodingMetadata`,metadata_batch.h:235)告诉对方"这条消息我压成了什么格式",`grpc-accept-encoding`(`GrpcAcceptEncodingMetadata`,L254)列出"我支持解压哪些格式"。接收方在 `HandleIncomingMetadata`(L213-228)读 `grpc-encoding` 决定怎么解压。

> **钉死这件事**:压缩是"用 CPU 换带宽"的可选项。gRPC 的策略是:① 默认不压(可配置);② 算法协商靠 grpc-encoding/grpc-accept-encoding 两个头;③ 压了反而变大就回退发原始数据(无显式阈值,靠自然兜底);④ 当前版本只有 zlib 系(deflate/gzip),zstd 是后续 commit 才加的,别信老资料说有 zstd。

---

## 六、连接与流复用:性能的另一面

零拷贝、arena、压缩,是 gRPC core 内部的优化。还有一类"看得见的"优化,在协议层,前面章节讲过,这里串一下:

- **HTTP/2 多路复用**(P2-05、P2-06):一条 TCP 连接跑海量调用,省连接、省握手。这是 gRPC 高并发的根,比"每次调用开一条连接"省几个数量级的连接数。
- **HPACK 头部压缩**(P2-07):重复头部压到几乎零字节,省带宽。
- **流量控制 + BDP 估计**(P2-09):按带宽时延积自适应调窗,既不淹不饿。
- **连接复用**(P4-14 SubChannel 复用池):多个 channel 共享同一条后端连接。

这些和 slice/arena 一起,构成了 gRPC 性能的完整图景:**协议层省带宽省连接,框架层靠 slice/arena 省拷贝省分配**。理解 gRPC 为什么快,这两面缺一不可。

---

## 七、技巧精解:两个最硬的性能技巧

本章最硬的两个技巧,我们已经讲过原理。这里再单独钉死它们的"为什么 sound"。

### 技巧一:slice 引用计数的正确性

零拷贝靠引用计数,引用计数靠原子操作。为什么这套是 sound 的(不会内存泄漏、不会 use-after-free)?

**Ref 的正确性**:`fetch_add(1, relaxed)`。多个线程同时 Ref,原子加保证计数正确递增,relaxed 序足够(因为 Ref 不涉及释放,不需要和其他操作建立 happens-before)。

**Unref 的正确性**:`fetch_sub(1, acq_rel)`,然后 `if (prev == 1) destroyer_fn_(this)`。这里有个经典的无锁引用计数问题:**会不会两个线程都看到 prev == 1、都触发 destroyer?** 不会。因为 `fetch_sub` 是原子的,如果两个线程都做 `fetch_sub`,那它们的 `prev` 必然不同(一个减之前是 2,另一个减之前是 1)。**只有那个 `prev == 1` 的线程(减之前是 1,减完是 0)才是"最后一个"**,只有它触发 destroyer。这是原子 `fetch_sub` 的语义保证。

**acq_rel 的必要性**:假设线程 A 写了字节(改了 slice 内容),然后 unref;线程 B ref 后读字节。必须保证 A 的写在 B 的读之前可见。acq_rel 的 release 部分(A 的 unref)保证 A 之前的写不会被重排到 unref 之后;acquire 部分(任何后续的 ref/读)保证看到这些写。这套内存序配对,是引用计数做到正确可见性的关键。

**子串切片的正确性**:`sub_no_ref` 复用父 refcount,子串和父 slice 共享同一个引用计数对象。父 slice unref 只是 -1,子串还持有引用(refcount 不会到 0);只有所有父和子串都 unref,refcount 才到 0,才释放字节。所以子串切片不会 use-after-free。

> **钉死这件事**:slice 引用计数 sound 的根,是① 原子 fetch_add/fetch_sub 保证计数正确;② 只有 prev==1 的那个 unref 触发释放(原子操作保证唯一性);③ acq_rel 内存序保证写可见性;④ 子串复用父 refcount,所有引用都 unref 才释放。这套是 lock-free 引用计数的标准范式,gRPC 把它焊进了 slice 这个最基本的缓冲抽象。

### 技巧二:arena 无 per-free 的正确性

"不 free 单个对象"听起来危险,但它 sound,因为 call 生命周期清晰。这里钉死它的几个约束:

**约束一:只有 call 级对象进 arena**。一个对象要进 arena 分配,前提是它的生命周期 ≤ call。metadata_batch、message、promise 状态、filter 的 per-call 数据,都是 call 内创建、call 内消费、call 结束就不再需要。如果一个对象要跨 call 存活(比如某个共享的配置缓存),它绝对不能进 arena——它该用普通 `RefCounted` 或独立管理。这个约束在 gRPC 里由代码结构保证:arena 是 call 创建时分配的,只有 call 内的代码能拿到 arena(`GetContext<Arena>()`)。

**约束二:arena 析构时所有引用都已消失**。call 结束前,所有 filter 都已经跑完(或被取消),所有 promise 都已经 resolve/reject,所有 MessageHandle 都已经销毁。call 结束的那一刻,arena 里没有任何活的对象引用。然后 arena 析构,整体释放。如果某个 arena 对象的引用逃出了 call(比如被异步任务持有,call 结束后还在用),那就 use-after-free——这违反约束一,gRPC 的代码结构保证不会发生。

**约束三:ManagedNew 兜底析构**。对于有非 trivial 析构的对象(持 std::string 等),用 ManagedNew 而不是 New。ManagedNew 把对象串到 `managed_new_head_` 链表,arena 析构时统一调析构。这保证即使"无 per-free",有析构副作用的对象(比如要释放自己持有的堆内存)也能被正确清理。

**约束四:slice 不受 arena 约束**。引用计数的 slice 字节缓冲,可能从 arena 分配(`Arena::New` 一段当 slice 用),也可能独立 malloc。如果从 arena 分配,它的 refcount 在 arena 析构时一起没了——但这没问题,因为 arena 析构时 call 已结束,没人还持有这个 slice。如果独立 malloc,slice 靠自己的 ref/unref 管理,和 arena 无关。**两套生命周期正交**,不会打架。

> **钉死这件事**:arena 无 per-free sound 的根,是① 只有 call 级对象进 arena(生命周期 ≤ call);② call 结束时所有引用已消失;③ ManagedNew 兜底非 trivial 析构;④ slice 引用计数和 arena 正交。这套设计把"分配极快"和"不泄漏"统一起来,前提是严格遵守"call 级对象进 arena"的约束——gRPC 的代码结构天然保证了这一点。

---

## 八、一个源码演进的诚实交代:MakePooled 的过渡状态

写这一章时,有一个源码细节必须诚实交代,免得读者读源码时困惑。

前面提到 `MessageHandle = Arena::PoolPtr<Message>`(`message.h:66`),按理 `PoolPtr` 应该是从 arena 分配的对象。但当前 commit 的真实情况是:**`Arena::MakePooled`(`arena.h:268`)/ `NewPooled`(`arena.h:294`)的 deleter 实际是 `delete`,不是 arena 回收**。看 `arena.h:222-226`、`arena.h:285-287` 的注释,它明说这是 promise-based filter 迁移过程中的**临时 hack**——这些 pooled 对象目前其实是堆分配 + delete,不是真 arena 分配。

这是个过渡状态:gRPC 正在把 filter 从经典 callback 架构迁到 Promise 架构,迁移过程中有些对象(如 Message)暂时用 `MakePooled`(实际 new/delete),等迁移完成会改成真 arena 分配。**真正的 arena 分配是 `Alloc`/`New`/`ManagedNew`**(本章讲的核心),这些是稳定的、无 per-free 的。读源码时看到 `MakePooled` 别误解成"这就是 arena 分配"——它是过渡。

> **钉死这件事**:当前 1.83.0-dev 的 `Arena::MakePooled` 实际是 `new/delete`(过渡 hack,注释承认),不是真 arena 分配。真 arena 分配是 `Alloc`/`New`/`ManagedNew`(bump pointer,无 per-free)。这是 gRPC 架构演进中的中间状态,读源码要分清。Message 目前走 MakePooled(堆),metadata_batch/promise 状态走真 arena。

---

## 九、章末小结

### 回扣主线

本章服务二分法的**协议 / 框架(招牌)**那一面。它讲的不是某个协议规则或某个治理机制,而是横跨协议层(message 变 DATA 帧)和框架层(filter 栈传递)的**性能基础设施**。gRPC core 能在跨语言、跨网络、穿十几个 filter 的情况下还快,根就扎在这一章讲的两个数据结构上:

1. **slice**:把一切字节缓冲统一成 32 字节定长、自带原子引用计数的载体。传 slice = 传指针 + 原子 +1,不拷字节。这是 filter 栈零拷贝的根。
2. **arena**:一次 call 一个内存池,bump pointer 分配,call 结束整体丢弃。分配是一条原子 `fetch_add`,释放是一次大回收,无 per-free。这是 call 内低开销分配的根。

两者正交配合:**arena 管 call 级对象的生命周期,slice 引用计数管字节缓冲的所有权**。再加上压缩(CPU 换带宽)和连接复用(协议层),构成 gRPC 性能的完整图景。

### 五个为什么

1. **为什么 gRPC 把一切字节缓冲抽象成 slice?**——因为 slice 把"引用计数"和"字节缓冲"焊在一起,做成 32 字节定长结构,Ref/Unref 就一条原子指令,做到极致轻;传 slice 是 +1 不是拷字节,这是 filter 栈零拷贝的根。
2. **为什么 slice 的引用计数用 relaxed/acq_rel 而不是 seq_cst?**——Ref 用 relaxed(不需要全局序,只原子加就够);Unref 用 acq_rel(要保证"最后一个 unref 触发释放"时之前的写都对 destructor 可见)。这是 lock-free 引用计数性能和正确性的平衡。
3. **为什么 arena 用 bump pointer 而不是 freelist?**——bump pointer 分配是一条原子 `fetch_add`,无查找;freelist 要维护、查找、合并,开销大。bump pointer 的代价是"无 per-free、zone 剩余空间浪费",但 call 内对象分配的频率远高于这个代价。
4. **为什么 arena 不 free 单个对象是 sound 的?**——因为只有 call 级对象进 arena(生命周期 ≤ call),call 结束时所有引用已消失,ManagedNew 兜底非 trivial 析构,slice 引用计数和 arena 正交。四个约束保证不泄漏不 use-after-free。
5. **为什么当前版本没有 zstd?**——zstd 是后续 commit 才加的,1.83.0-dev 只有 zlib 系(deflate/gzip);compression_types.h:65 的 TODO 提的是 snappy 不是 zstd。读老资料说有 zstd 是错的。

### 想继续深入往哪钻

- 想看 slice 引用计数全貌:读 `src/core/lib/slice/slice_refcount.h`(73 行,极短)。
- 想看 slice 的三种类型和子串切片:读 `src/core/lib/slice/slice.cc` 的 `sub_no_ref`(L242)、`grpc_slice_malloc`(L222)、各派生 refcount 类。
- 想看 arena 的 bump pointer:读 `src/core/lib/resource_quota/arena.h` 的 `Alloc`(L176)和 `arena.cc` 的 `AllocZone`(L107)。
- 想看 arena 怎么和 call 绑定:读 `src/core/lib/surface/channel.cc:62-71` 的 channel 构造 + `src/core/call/call_arena_allocator.h:71`。
- 想看压缩决策:读 `src/core/ext/filters/http/message_compress/compression_filter.cc:103` 的 `CompressMessage`。
- 想看 slice 在 transport 里怎么用:读 `src/core/ext/transport/chttp2/transport/`(P2-06~P2-09 已拆)。

### 引出下一章

性能基础设施讲完了,gRPC core 在框架层和协议层的优化都串了起来。最后一章 P6-22,我们看 gRPC 怎么和外部生态衔接:client 怎么不依赖 Envoy sidecar,自己内置 xDS client,动态收到控制面下发的路由、负载均衡、熔断配置——以及这套机制怎么和 Envoy/Istio 互通。这是本书的最后一站,也是通往《Envoy 设计与实现》那本的接口。

> **下一章**:[P6-22 · xDS 与服务网格](P6-22-xDS与服务网格.md)
