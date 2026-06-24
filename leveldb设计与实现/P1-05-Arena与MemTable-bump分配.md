# 第五章 · Arena 与 MemTable:bump 分配

> 篇:P1 写入的前台
> 主线呼应:上一章我们讲清了 MemTable 的骨架——SkipList 怎么用尾随数组 + release/acquire 把"写者加锁、读者无锁"做出来。但骨架只是骨架,跳表节点要 `new` 出来、key 和 value 的字节要存下来,**这些内存从哪来**?为什么不直接 `malloc`/`new` 每个节点,而是写一个自己的 Arena?为什么 Arena 没有 `Free` 方法、只进不出?这一章讲清楚 MemTable 的内存来源——一个朴素到几乎不像数据结构的 bump allocator,以及它怎么把 SkipList + 引用计数 + InternalKeyComparator 装成一个完整的内存表。

## 核心问题

**MemTable 的所有内存(SkipList 节点 + key/value 字节)都来自 Arena,一个只进不出的 bump allocator。为什么不直接每节点 `new`/`delete`?因为 MemTable 的生命周期是"写满 → 冻结成 Immutable → 刷成 SSTable → 整体析构",中间永远不释放单个节点,所以 Arena 干脆不提供 `Free`,省掉 free 的簿记和碎片,只在 `~Arena` 整体释放所有块。这一条"只进不出"看起来浪费,实际上换来更快的分配(O(1) 指针前推)、更少的开销(无 per-node malloc)、更少的碎片,以及 SkipList 原子访问所需的天然对齐。**

读完本章你会明白:

1. Arena 是什么:bump allocator = 一块预分配的字节缓冲,`Allocate` 就是"指针前推",到块尾再开新块,无 per-node free。O(1) 分配,无碎片。
2. 为什么"只进不出"反而更快更省:MemTable 中间永不释放单个节点,整体回收。省掉 free 簿记和碎片,`~Arena` 一次性释放所有 `blocks_`。
3. 大块直通:`AllocateFallback` 里 `bytes > kBlockSize/4` 的分配直接 `new` 一个独立块挂进 `blocks_`,避免在小块里浪费。`AllocateAligned` 提供 8 字节对齐,为 SkipList 的原子访问铺路。
4. MemTable 怎么把 SkipList + Arena + InternalKeyComparator + 引用计数(`refs_`)装成一个完整的内存表,`MemTable::Add` 怎么把 internal key + value 字节 memcpy 进 Arena 再挂上 SkipList,`MemTable::Get` 怎么查。
5. 引用计数 `refs_`:谁引用(读迭代器、Immutable 切换),析构时机。

> **如果一读觉得太难**:先只记住三件事——① Arena 是 bump allocator,`Allocate` 就是"指针前推",到块尾开新块,`kBlockSize = 4096`;② Arena 没有 `Free`,只在 `~Arena` 整体释放,因为 MemTable 中间永不释放单节点,省 free 簿记和碎片;③ `MemTable::Add` 把 key + 8 字节 seq|type + value 拼成一段连续字节 memcpy 进 Arena,把这段字节的首地址作为 `const char*` 插进 `SkipList<const char*, KeyComparator>`。剩下的对齐、大块直通、引用计数细节,可以回头再读。

---

## 5.1 一句话点破

> **Arena 是 bump allocator,Allocate = 指针前推,无 Free,只在析构时整体释放所有块。MemTable 中间永不释放单节点(整体生命周期:写满→冻结→刷盘→析构),所以这种"只进不出"反而比 per-node malloc/free 更快、更省、更少碎片——这是 LSM"只追加不原地改"在内存层的回响。**

这是结论,不是理由。本章倒过来拆:先看 per-node malloc 撞上什么墙,再看 Arena 怎么用最朴素的"指针前推"把这些墙全拆掉,然后钻进对齐、大块直通、引用计数这些工程细节,最后把 MemTable 整体结构画出来。

---

## 5.2 为什么不 per-node malloc:碎片、锁、慢

### 提出问题

MemTable 的内存需求长什么样?假设一个 internal key 平均 30 字节、value 平均 100 字节,加上长度前缀和 tag,一条记录约 140 字节。一个 4MB 的 MemTable 大约装 3 万条记录,**就是 3 万次节点分配**。

如果直接 `new`/`malloc` 每条记录:

- 每次 `malloc` 是一次堆分配(进 libc 的 arena,可能上 malloc 锁)。
- 每次 `free` 是一次堆回收(同样可能上锁、整理 bins)。
- malloc 的元数据开销(per-alignment chunk header,通常 8~16 字节/块)。
- **碎片**:3 万次 malloc/free 后,堆里满是小块洞,大块进不来。

### 不这样会怎样

**反面对比(per-node malloc/free)**:假设 LevelDB 真的用 `new char[encoded_len]` 分配每条 MemTable entry,用 `delete[]` 在 entry 被覆盖/删除时释放:

1. **每次 `Put` 都触发一次 malloc**。一次 batch 写 1000 条,触发 1000 次堆分配。libc malloc 在多线程下要上 per-arena 锁,即便无竞争也有非零开销(数十 ns/次)。这 1000 次 malloc,直接在写路径上付出几十微秒。
2. **碎片化**:MemTable entry 大小高度不齐(短的几十字节、长的几 KB),`malloc` 把它们混进堆,长时间运行后堆碎片化严重,**实际内存占用远大于有效数据**。
3. **delete 时机难题**:LSM 不做原地更新,一条 key 改了又改、删了又写,旧版本什么时候能释放?要等 Compaction 把它们归并掉——但 MemTable 里的 entry 是刷盘后才整体丢弃的,**中间根本不释放单条**。所以即便有 `delete`,也只能等到 MemTable 整体回收才用得上——那就干脆不做 per-node free 了。
4. **SkipList 原子访问的对齐**:`std::atomic<Node*>` 在某些平台上要求对齐访问(x86 上虽不强制,但不对齐有性能惩罚)。`malloc` 默认按 `alignof(max_align_t)` 对齐(通常 16 字节),够用但"够用"之外还有冗余。LevelDB 想要精确控制对齐,自己分配更直接。

> **钉死这件事**:MemTable 的内存使用模式是"**写时密集分配、整体一次性释放、中间永不释放单块**"。这种模式最适合 bump allocator——一个指针前推,无簿记、无碎片、无锁。

---

## 5.3 Arena:bump allocator 直球讲

### Arena 的结构

Arena 就是"**一块块 `kBlockSize` 大小的缓冲 + 一个当前位置指针**"。看 [`util/arena.h:16-53`](../leveldb/util/arena.h#L16-L53):

```cpp
class Arena {
 public:
  Arena();
  Arena(const Arena&) = delete;
  Arena& operator=(const Arena&) = delete;
  ~Arena();

  // Return a pointer to a newly allocated memory block of "bytes" bytes.
  char* Allocate(size_t bytes);

  // Allocate memory with the normal alignment guarantees provided by malloc.
  char* AllocateAligned(size_t bytes);

  // Returns an estimate of the total memory usage of data allocated
  // by the arena.
  size_t MemoryUsage() const {
    return memory_usage_.load(std::memory_order_relaxed);
  }

 private:
  char* AllocateFallback(size_t bytes);
  char* AllocateNewBlock(size_t block_bytes);

  // Allocation state
  char* alloc_ptr_;
  size_t alloc_bytes_remaining_;

  // Array of new[] allocated memory blocks
  std::vector<char*> blocks_;

  // Total memory usage of the arena.
  std::atomic<size_t> memory_usage_;
};
```

整个类的状态就四个字段:

- `alloc_ptr_`:当前块的"已经分配到哪里"指针(下一个可分配的位置)。
- `alloc_bytes_remaining_`:当前块还剩多少字节。
- `blocks_`:所有 `new[]` 出来的块的首地址 vector(用于析构时整体释放)。
- `memory_usage_`:累计分配的字节数(供 `ApproximateMemoryUsage` 用,原子,因为可能被多个线程读)。

### Allocate:bump 的核心

`Allocate` 的实现**在头文件里,inline**([util/arena.h:55-67](../leveldb/util/arena.h#L55-L67))——因为是热路径,inline 才够快:

```cpp
inline char* Arena::Allocate(size_t bytes) {
  // The semantics of what to return are a bit messy if we allow
  // 0-byte allocations, so we disallow them here (we don't need
  // them for our internal use).
  assert(bytes > 0);
  if (bytes <= alloc_bytes_remaining_) {
    char* result = alloc_ptr_;
    alloc_ptr_ += bytes;
    alloc_bytes_remaining_ -= bytes;
    return result;
  }
  return AllocateFallback(bytes);
}
```

逻辑简单到没有水分:

1. 如果**当前块还够**:返回 `alloc_ptr_`,然后 `alloc_ptr_ += bytes`,`alloc_bytes_remaining_ -= bytes`。**就这一行指针前推,就是 bump 的全部**。O(1),无 malloc、无锁、无元数据。
2. 如果**当前块不够**:调 `AllocateFallback`。

字节布局示意(一次 `Allocate(30)` 在一块 4096 字节的块里,假设 `alloc_ptr_` 起初在偏移 100):

```
块 (4096 字节,通过 new char[4096] 分配):
 ┌─────────────────┬────────────────────────┬──────────────────┐
 │  已用 100 字节   │ 本次分配 30 字节        │ 剩 3966 字节      │
 │ (之前的 entry)   │ ← Allocate 的返回值     │ (alloc_bytes_     │
 │                  │                         │   remaining_)     │
 └─────────────────┴────────────────────────┴──────────────────┘
                    ↑
                  alloc_ptr_ 前推 30 字节到这个位置
```

### AllocateFallback:不够时的两条路

`AllocateFallback`([util/arena.cc:20-36](../leveldb/util/arena.cc#L20-L36)):

```cpp
char* Arena::AllocateFallback(size_t bytes) {
  if (bytes > kBlockSize / 4) {
    // Object is more than a quarter of our block size.  Allocate it separately
    // to avoid wasting too much space in leftover bytes.
    char* result = AllocateNewBlock(bytes);
    return result;
  }

  // We waste the remaining space in the current block.
  alloc_ptr_ = AllocateNewBlock(kBlockSize);
  alloc_bytes_remaining_ = kBlockSize;

  char* result = alloc_ptr_;
  alloc_ptr_ += bytes;
  alloc_bytes_remaining_ -= bytes;
  return result;
}
```

两条路:

1. **大块直通**(`bytes > kBlockSize / 4 = 1024` 字节):直接 `new char[bytes]` 开一个独立块,挂进 `blocks_`。**为什么不塞进当前块?** 因为当前块剩 < 1024,把 1024+ 字节的 entry 塞进去,剩下的就不够再装别的 entry,空间浪费。注释明说:"avoid wasting too much space in leftover bytes"。所以大块单独开,**当前块的剩余空间就直接丢弃**(`alloc_ptr_` / `alloc_bytes_remaining_` 不变)——下次小块分配还能用。
2. **小块新开**:当前块不够装但 bytes <= 1024,就新开一个 `kBlockSize`(4096)块,**旧块的剩余空间直接浪费**(不再用了)。新块用 `alloc_ptr_ = new_block; alloc_ptr_ += bytes;` 前推。

注意**旧块的剩余空间被丢弃**——这是 bump allocator 的"特性",不是 bug。理论上更精细的 allocator 可以维护一个"剩余块链表"复用,但 LevelDB 不做——保持简单,且 `kBlockSize = 4096` 时单块浪费最多 ~4KB,相对 4MB 的 MemTable 总量微不足道(<0.1%)。

### AllocateNewBlock:实际分配 + 计账

`AllocateNewBlock`([util/arena.cc:58-64](../leveldb/util/arena.cc#L58-L64)):

```cpp
char* Arena::AllocateNewBlock(size_t block_bytes) {
  char* result = new char[block_bytes];
  blocks_.push_back(result);
  memory_usage_.fetch_add(block_bytes + sizeof(char*),
                          std::memory_order_relaxed);
  return result;
}
```

三件事:

1. **`new char[block_bytes]`**:真正调 libc 的 `new[]`,从堆要一块。这一步才上 malloc 锁、才有开销——但**整个 MemTable 生命周期里只调 ~1024 次**(4MB / 4KB),不是每条 entry 调一次。
2. **`blocks_.push_back(result)`**:把块首地址记下来,析构时遍历 `blocks_` 释放。
3. **`memory_usage_.fetch_add(block_bytes + sizeof(char*))`**:累计 `memory_usage_`。注意这里加的是 `block_bytes + sizeof(char*)`——多算一个指针大小,是为了**把 `blocks_` vector 里存这个块地址的槽位开销也算进去**,这样 `ApproximateMemoryUsage` 报出来的值更接近真实占用。

### ~Arena:整体回收

析构极简([util/arena.cc:14-18](../leveldb/util/arena.cc#L14-L18)):

```cpp
Arena::~Arena() {
  for (size_t i = 0; i < blocks_.size(); i++) {
    delete[] blocks_[i];
  }
}
```

**遍历 `blocks_`,每块 `delete[]`**。没有 per-entry 的回收——所有的 entry 都在 `blocks_` 里的某一块中,块释放了 entry 也就释放了。这是"只进不出"在析构时的体现:**O(块数) 释放,不是 O(entry 数)**。

> **钉死这件事**:Arena 的核心数据结构 = "一个指针(alloc_ptr_) + 一个剩余字节计数 + 一个块指针 vector"。`Allocate` 就是"够就指针前推,不够就开新块",`~Arena` 就是"遍历 vector 全部 delete"。**没有 Free、没有 per-entry 回收**,这是 LSM"只追加、中间不原地改、整体一次性回收"在内存层的字面体现。

---

## 5.4 为什么"只进不出"反而更快更省

### 提出问题

直觉上,"没有 Free"听起来像浪费——分配的内存不能复用,只会越攒越多。但 LevelDB 偏偏就这么做,而且事实证明这比 per-node malloc/free **更快、更省**。为什么?

### 不这样会怎样

先看"只进不出"换掉了什么:

1. **没有 per-entry free 的开销**:不用维护 free list、不用合并相邻空闲块、不用扫 bins。每个 entry 不需要任何元数据(对比 malloc 的 chunk header,8~16 字节/块)。
2. **没有碎片**:所有 entry 紧密排列在一块块连续内存里,无空洞。`Allocate` 永远返回可用地址,无需"找一块够大的空闲"。
3. **没有锁**:`Allocate` 只读写 `alloc_ptr_` / `alloc_bytes_remaining_`,无共享数据结构(只要单写者——MemTable 的写被外部 `mutex_` 串行化,见 P1-04)。`memory_usage_` 是原子但只 `fetch_add`,无竞争路径。
4. **分配 O(1)**:就一次加法 + 比较常数次。

**反面对比(per-node malloc/free)**:

| 维度 | Arena (bump) | per-node malloc/free |
|------|--------------|----------------------|
| 分配耗时 | ~1ns(指针前推,inline) | ~30-100ns(libc malloc,可能上锁) |
| Per-entry 元数据 | 0 | 8-16 字节(chunk header) |
| 碎片 | 无(紧密排列) | 严重(小块洞遍地) |
| Free 开销 | 0(整体 delete[]) | ~30-100ns/次 |
| 多线程 | 单写者无锁 | malloc 锁竞争 |

为什么"只进不出"在这里反而 work?**因为 MemTable 的生命周期匹配这个模式**:

- MemTable 写满 ~4MB → 冻结成 Immutable → 后台刷成 SSTable → 析构。
- **整个生命周期里,中间所有 entry 都不会被单独释放**(LSM 不原地改,旧版本要等刷盘 + Compaction 后整体丢弃)。
- 只有 MemTable 整体析构,所有内存才一起回收——这正是 `~Arena` 的语义。

所以"只进不出"不是缺陷,是**和 MemTable 使用模式精确匹配的设计**。给一个会频繁 per-entry free 的数据结构(比如一个 LRU cache),bump allocator 就不合适;给 MemTable,它就是最优解。

> **钉死这件事**:Arena 之所以"只进不出"反而更快更省,根在于 MemTable 的使用模式——**中间永不释放单块,整体一次性回收**。这种模式下,bump allocator 用"O(1) 指针前推 + 整体 delete[]"击败了 per-node malloc/free 的所有开销(锁、元数据、碎片)。这是 LSM"只追加不原地改"哲学在内存分配层的字面回响。

---

## 5.5 AllocateAligned:为原子访问铺路

### 提出问题

除了 `Allocate`,Arena 还提供一个 `AllocateAligned`,**SkipList::NewNode** 用的就是它(P1-04 看过)。它和 `Allocate` 的区别是什么?

### 真实源码

`AllocateAligned`([util/arena.cc:38-56](../leveldb/util/arena.cc#L38-L56)):

```cpp
char* Arena::AllocateAligned(size_t bytes) {
  const int align = (sizeof(void*) > 8) ? sizeof(void*) : 8;
  static_assert((align & (align - 1)) == 0,
                "Pointer size should be a power of 2");
  size_t current_mod = reinterpret_cast<uintptr_t>(alloc_ptr_) & (align - 1);
  size_t slop = (current_mod == 0 ? 0 : align - current_mod);
  size_t needed = bytes + slop;
  char* result;
  if (needed <= alloc_bytes_remaining_) {
    result = alloc_ptr_ + slop;
    alloc_ptr_ += needed;
    alloc_bytes_remaining_ -= needed;
  } else {
    // AllocateFallback always returned aligned memory
    result = AllocateFallback(bytes);
  }
  assert((reinterpret_cast<uintptr_t>(result) & (align - 1)) == 0);
  return result;
}
```

逐步拆:

1. **`align = (sizeof(void*) > 8) ? sizeof(void*) : 8`**:64 位系统 `sizeof(void*) = 8`,所以 `align = 8`;某些罕见平台(如某些RV64 with 128-bit tagged pointers)可能更大,取 `sizeof(void*)`。**至少 8 字节对齐**。
2. **`current_mod = alloc_ptr_ & (align - 1)`**:`align - 1 = 7`(8 字节对齐下),`current_mod` 是 `alloc_ptr_` 当前偏离 8 字节边界的字节数(0~7)。
3. **`slop = (current_mod == 0 ? 0 : align - current_mod)`**:要补的字节数,让 `alloc_ptr_ + slop` 落在下一个 8 字节边界上。
4. **`needed = bytes + slop`**:实际从当前块消耗的字节数(含对齐 padding)。
5. **够装**:`result = alloc_ptr_ + slop`,前推 `needed` 字节。
6. **不够装**:走 `AllocateFallback`——注释说"AllocateFallback always returned aligned memory",为什么对齐?因为 `new char[block_bytes]` 在主流实现里返回的内存至少按 `alignof(max_align_t)`(通常 16 字节)对齐,自然满足 8 字节要求。所以 fallback 返回的地址天然对齐。
7. **`assert((reinterpret_cast<uintptr_t>(result) & (align - 1)) == 0)`**:开发期断言返回地址确实 8 字节对齐。

### 为什么 SkipList 要对齐

SkipList 的 `NewNode` 用 `AllocateAligned`:

```cpp
char* const node_memory = arena_->AllocateAligned(
    sizeof(Node) + sizeof(std::atomic<Node*>) * (height - 1));
return new (node_memory) Node(key);
```

`Node` 的第一个字段是 `Key const key`(对 `MemTable` 是 `const char* key`,8 字节),后面是 `std::atomic<Node*> next_[1]`。**`std::atomic<T>` 在主流平台上要求 `T` 自然对齐**——`std::atomic<Node*>` 要求 8 字节对齐(64 位)。如果 Node 起始地址不对齐,next_[0] 的原子访问就可能撕裂(某些 ARM/PowerPC 平台会 trap,x86 上有性能惩罚)。

> **钉死这件事**:`AllocateAligned` 保证返回地址至少 8 字节对齐,这是 `std::atomic<Node*>` 在所有平台上 sound 的前提。P1-04 讲 SkipList 的无锁读,前提之一就是节点的原子字段都正确对齐——这一条由 Arena 的 `AllocateAligned` 担保。

---

## 5.6 MemTable:把 SkipList + Arena + 引用计数装起来

### 提出问题

现在 Arena 讲清了,我们看 MemTable 怎么把 Arena + SkipList + InternalKeyComparator + 引用计数装成一个完整的内存表。

### MemTable 的结构

看 [`db/memtable.h:20-83`](../leveldb/db/memtable.h#L20-L83):

```cpp
class MemTable {
 public:
  // MemTables are reference counted.  The initial reference count
  // is zero and the caller must call Ref() at least once.
  explicit MemTable(const InternalKeyComparator& comparator);

  // Increase reference count.
  void Ref() { ++refs_; }

  // Drop reference count.  Delete if no more references exist.
  void Unref() {
    --refs_;
    assert(refs_ >= 0);
    if (refs_ <= 0) {
      delete this;
    }
  }

  // ...

 private:
  friend class MemTableIterator;
  friend class MemTableBackwardIterator;

  struct KeyComparator {
    const InternalKeyComparator comparator;
    explicit KeyComparator(const InternalKeyComparator& c) : comparator(c) {}
    int operator()(const char* a, const char* b) const;
  };

  typedef SkipList<const char*, KeyComparator> Table;

  ~MemTable();  // Private since only Unref() should be used to delete it

  KeyComparator comparator_;
  int refs_;
  Arena arena_;
  Table table_;
};
```

四个字段:

1. **`comparator_`**(`KeyComparator`):包了一层 `InternalKeyComparator`,把 SkipList 需要的"两个 key 比大小"适配到 internal key 上(下面 5.6.2 详讲)。
2. **`refs_`**(`int`):引用计数。`Ref()` 加、`Unref()` 减,归零时 `delete this`。注意析构函数是 **private**——只能通过 `Unref()` 触发,强制用户走引用计数路径。
3. **`arena_`**(`Arena`):bump allocator,所有 entry 字节的实际存放地。
4. **`table_`**(`Table` = `SkipList<const char*, KeyComparator>`):跳表本身,**存的 key 类型是 `const char*`**——指向 Arena 里某段字节的首地址。

### 5.6.1 SkipList 的 key 是 `const char*`,不是 `std::string`

注意 `typedef SkipList<const char*, KeyComparator> Table;`——SkipList 模板参数 `Key = const char*`。

为什么?因为 MemTable entry 的实际内容(key + value)是一段**变长字节流**,要存在 Arena 里。SkipList 只存"指向这段字节流首地址的指针"。比较两条 entry 时,要按 internal key 比——但 internal key 在这段字节流的某个偏移上,需要"解出"它。这就是 `KeyComparator` 的工作。

### 5.6.2 KeyComparator:从字节流解出 internal key

`KeyComparator::operator()`([db/memtable.cc:28-34](../leveldb/db/memtable.cc#L28-L34)):

```cpp
int MemTable::KeyComparator::operator()(const char* aptr,
                                        const char* bptr) const {
  // Internal keys are encoded as length-prefixed strings.
  Slice a = GetLengthPrefixedSlice(aptr);
  Slice b = GetLengthPrefixedSlice(bptr);
  return comparator.Compare(a, b);
}
```

`GetLengthPrefixedSlice`([db/memtable.cc:14-19](../leveldb/db/memtable.cc#L14-L19)):

```cpp
static Slice GetLengthPrefixedSlice(const char* data) {
  uint32_t len;
  const char* p = data;
  p = GetVarint32Ptr(p, p + 5, &len);  // +5: we assume "p" is not corrupted
  return Slice(p, len);
}
```

逻辑直白:

1. entry 字节流的前缀是 `varint32`(变长 32 位整数,1~5 字节)编码的 internal_key 长度。
2. `GetVarint32Ptr` 解出长度,跳过长度前缀,返回指向 internal key 本体的指针。
3. 构造一个 `Slice(internal_key_body, internal_key_len)`。
4. 委托给 `InternalKeyComparator::Compare`(P1-03 讲过,先比 user_key 升序、再比 seq|type 降序)。

所以 MemTable entry 的字节布局是:

```
MemTable entry 的字节布局(在 Arena 里的一段连续内存):
 ┌─────────────────┬──────────────────────────────────┬────────────────┐
 │ varint32        │ internal_key 字节流               │ varint32 + value│
 │ internal_key 长度│ (user_key + 8 字节 seq|type)     │ (长度 + 值)     │
 │ (1~5 字节)       │                                  │                │
 └─────────────────┴──────────────────────────────────┴────────────────┘
  ↑
  SkipList 存的 `const char* key` 指向这里
```

SkipList 比较两条 entry 时,通过 `KeyComparator` 把 `const char*` 解出 internal_key 的 `Slice`,再交给 `InternalKeyComparator`。**P1-03 的降序排序就在这里发挥作用**——同 user_key 的多版本,新版本天然排前面。

### 5.6.3 引用计数 refs_:谁引用、何时析构

`refs_` 的初值是 0(注释明说 "The initial reference count is zero and the caller must call Ref() at least once")。谁会 `Ref()`?

- **`DBImpl` 持有的当前 `mem_`**:Open 时 Ref 一次。
- **冻结成 Immutable 时**:`imm_` 接管,refs_ 不变(已经 Ref 过)。
- **读 Iterator**:读路径上拿到 MemTable 的 Iterator 时,对应 MemTable 会 Ref 一次(保证 Iterator 活着期间 MemTable 不被析构)。
- **后台刷盘**:刷 Immutable 时也要保证 Immutable 不被读 Iterator 释放。

`Unref()` 的逻辑极简:

```cpp
void Unref() {
  --refs_;
  assert(refs_ >= 0);
  if (refs_ <= 0) {
    delete this;
  }
}
```

减到 0 就 `delete this`。`delete this` 触发 `~MemTable()`(private 析构)→ `~Arena()`(释放所有 blocks_)→ `~Table table_`(SkipList 字段,无独立资源)。这就是 MemTable 的整体回收路径。

> **钉死这件事**:MemTable 用引用计数管理生命周期——`Ref()` 加、`Unref()` 减到零就 `delete this`。析构链是 `~MemTable → ~Arena → 释放所有 blocks_`,这就是"只进不出"在回收端的体现:**整个 MemTable 一次性释放,不是 per-entry**。引用计数让多个持有者(读 Iterator、Immutable 切换、刷盘)安全共享,最后一个释放的人关灯。

---

## 5.7 MemTable::Add:写一条 entry 的全流程

### 真实源码

`MemTable::Add`([db/memtable.cc:76-100](../leveldb/db/memtable.cc#L76-L100)):

```cpp
void MemTable::Add(SequenceNumber s, ValueType type, const Slice& key,
                   const Slice& value) {
  // Format of an entry is concatenation of:
  //  key_size     : varint32 of internal_key.size()
  //  key bytes    : char[internal_key.size()]
  //  tag          : uint64((sequence << 8) | type)
  //  value_size   : varint32 of value.size()
  //  value bytes  : char[value.size()]
  size_t key_size = key.size();
  size_t val_size = value.size();
  size_t internal_key_size = key_size + 8;
  const size_t encoded_len = VarintLength(internal_key_size) +
                             internal_key_size + VarintLength(val_size) +
                             val_size;
  char* buf = arena_.Allocate(encoded_len);
  char* p = EncodeVarint32(buf, internal_key_size);
  std::memcpy(p, key.data(), key_size);
  p += key_size;
  EncodeFixed64(p, (s << 8) | type);
  p += 8;
  p = EncodeVarint32(p, val_size);
  std::memcpy(p, value.data(), val_size);
  assert(p + val_size == buf + encoded_len);
  table_.Insert(buf);
}
```

逐步拆:

1. **算总长度** `encoded_len`:`internal_key_size = user_key.size() + 8`(P1-03 讲过的 8 字节 seq|type 尾部)。
2. **`arena_.Allocate(encoded_len)`**:从 Arena 分配这一段连续字节,返回首地址 `buf`。**这是 entry 的最终存放地**,Arena 拥有这块内存。
3. **拼字节流**:
   - `EncodeVarint32(buf, internal_key_size)`:写 varint32 长度前缀。
   - `std::memcpy(p, key.data(), key_size)`:写 user_key 字节。
   - `EncodeFixed64(p, (s << 8) | type)`:写 8 字节 seq|type 尾部(P1-03 讲过的小端打包)。**注意 `(s << 8) | type` 是 inline 计算,没有先调 `PackSequenceAndType`——直接位运算**。这是热路径的优化。
   - `EncodeVarint32(p, val_size)`:写 value 长度前缀。
   - `std::memcpy(p, value.data(), val_size)`:写 value 字节。
4. **`table_.Insert(buf)`**:把 `buf`(Arena 里的首地址)作为 `const char*` 插进 SkipList。

注意几个关键点:

- **Slice 字节在这里"落地"**:P1-02 讲过 Slice 不拥有内存,从用户 API 一路零拷贝流到 `MemTable::Add`,**在这里做一次 memcpy 进 Arena**。这次 memcpy 是必须的——Arena 拥有这块内存,SkipList 后续访问的都是 Arena 内存,与用户的 Slice 生命周期彻底解耦。
- **Arena 担保生命周期**:SkipList 存的 `const char*` 指向 Arena 内部,Arena 在 MemTable 析构时才释放。所以 SkipList 的 Iterator 访问 entry 时,只要 MemTable 还活着,entry 字节就有效。
- **`table_.Insert(buf)`** 调 SkipList 的 Insert(P1-04 讲过:先 `FindGreaterOrEqual` 找位置、`NewNode` 构造节点、release store 发布)。**新节点的 `key` 字段就是 `buf`**——也就是说 SkipList 节点的 `key` 不存字节流副本,只存指针。

> **钉死这件事**:`MemTable::Add` 把 user_key + 8 字节 seq|type + value 拼成一段连续字节 memcpy 进 Arena,把这段字节的首地址作为 `const char*` 插进 SkipList。整个 entry 的生命周期由 Arena 管,**没有 per-entry new/delete**。这是 LSM 写路径的核心一环——一次 `Put` 字面落地为"Arena 里一段紧凑字节 + SkipList 里一个节点"。

---

## 5.8 MemTable::Get:读一条 entry 的全流程

### 真实源码

`MemTable::Get`([db/memtable.cc:102-136](../leveldb/db/memtable.cc#L102-L136)):

```cpp
bool MemTable::Get(const LookupKey& key, std::string* value, Status* s) {
  Slice memkey = key.memtable_key();
  Table::Iterator iter(&table_);
  iter.Seek(memkey.data());
  if (iter.Valid()) {
    // entry format is:
    //    klength  varint32
    //    userkey  char[klength]
    //    tag      uint64
    //    vlength  varint32
    //    value    char[vlength]
    // Check that it belongs to same user key.  We do not check the
    // sequence number since the Seek() call above should have skipped
    // all entries with overly large sequence numbers.
    const char* entry = iter.key();
    uint32_t key_length;
    const char* key_ptr = GetVarint32Ptr(entry, entry + 5, &key_length);
    if (comparator_.comparator.user_comparator()->Compare(
            Slice(key_ptr, key_length - 8), key.user_key()) == 0) {
      // Correct user key
      const uint64_t tag = DecodeFixed64(key_ptr + key_length - 8);
      switch (static_cast<ValueType>(tag & 0xff)) {
        case kTypeValue: {
          Slice v = GetLengthPrefixedSlice(key_ptr + key_length);
          value->assign(v.data(), v.size());
          return true;
        }
        case kTypeDeletion:
          *s = Status::NotFound(Slice());
          return true;
      }
    }
  }
  return false;
}
```

逐步拆:

1. **构造 LookupKey**:调用方传入 `LookupKey`(P1-03 讲过,内含 memtable_key = `varint(internal_key_len) + internal_key`,seq 设为 snapshot seq,type 设为 `kValueTypeForSeek`)。
2. **`iter.Seek(memkey.data())`**:在 SkipList 里 Seek——P1-04 讲过的 `FindGreaterOrEqual`,acquire load 链路。Seek 落到 >= lookup key 的第一个 entry(由于 InternalKeyComparator 的降序,这就是同 user_key 中 seq <= snapshot 的最新版本)。
3. **校验 user_key 一致**:Seek 后第一个 entry 可能 user_key 不同(目标 key 不存在),用 user_comparator 比一遍。
4. **解 tag 判 type**:
   - `kTypeValue`:解出 value,**`value->assign(v.data(), v.size())` 拷一份到出参**(P1-02 讲过的"Slice 指着 MemTable 内部,跨边界必须拷")。
   - `kTypeDeletion`:返回 `Status::NotFound`(墓碑语义),`Get` 返回 true 表示"找到了删除标记"。
5. **user_key 不一致**:返回 false,告诉调用方"MemTable 里没有这个 key",调用方继续查 Immutable、SSTable。

注意几个细节:

- **注释 "We do not check the sequence number since the Seek() call above should have skipped all entries with overly large sequence numbers"**:Seek 用 lookup key(含 snapshot seq),由于降序,所有 seq > snapshot 的版本天然排在前、Seek 跳过它们。所以 Seek 后第一个 entry 的 seq <= snapshot,无需再校验 seq。
- **`value->assign(...)` 是必须的拷贝**:`Slice v` 指着 MemTable 内部 Arena 的内存。调用方拿到 value 后,MemTable 可能被冻结、被刷盘、被析构——Slice 立刻悬垂。把字节拷进 `std::string* value`,所有权彻底转移。这是 P1-02 讲过的"Slice 跨边界必须 owning 化"的具体落地。
- **Get 不上锁**:P1-04 讲过 SkipList 的读者无锁。MemTable::Get 只是包了一层 Iterator,内部全是 acquire load。所以 **MemTable::Get 不阻塞写者的 MemTable::Add**——这就是前台 Get 不被 Put 阻塞的字面实现。

---

## 5.9 技巧精解:bump allocator + 不释放单块

### 这个技巧在做什么

让 MemTable 的每一条 entry 分配都是 O(1)(指针前推),无 per-entry 元数据、无碎片、无锁。整体析构是 O(块数),不是 O(entry 数)。

### 用了什么手段

Arena 维护一个 `alloc_ptr_`(当前指针)和 `alloc_bytes_remaining_`(当前块剩余),`Allocate` 就是"够就指针前推,不够就开新块"。每块 `kBlockSize = 4096` 字节,新块通过 `new char[kBlockSize]` 一次性开,挂进 `blocks_` vector。`~Arena` 遍历 `blocks_` 全部 `delete[]`。

### 为什么 sound

1. **MemTable 使用模式匹配 bump**:MemTable 的生命周期是"写满→冻结→刷盘→析构",**中间永不释放单 entry**。所以 Arena 不提供 `Free`,没有"复用空闲块"的需求。这个 design 决定是和 LSM"只追加不原地改"的字面对应——如果 LSM 是原地更新,MemTable 里会有 entry 被覆盖释放,就需要 free 机制;正因为不是,bump allocator 才是最优解。

2. **大块直通的合理性**:`AllocateFallback` 里 `bytes > kBlockSize/4 = 1024` 走独立块,避免在小块里浪费。阈值 1024 不是随便选的——一个 4096 字节块剩 < 1024 时,塞一个 > 1024 的 entry 进去,剩余空间不够装绝大多数小 entry(典型 entry ~140 字节,< 1024),所以那块剩余空间几乎是死的。直接开独立块,旧块剩余空间虽丢弃但 < 1024,微不足道。

3. **`memory_usage_` 是原子的**:`AllocateNewBlock` 里 `memory_usage_.fetch_add(...)`。为什么?因为 `MemoryUsage()` 会被读路径调用(`MemTable::ApproximateMemoryUsage` 判断是否要触发 Compaction),而写者在另一个上下文(`AllocateNewBlock`)。两者可能并发。所以 `memory_usage_` 是 `std::atomic<size_t>`,**用 relaxed 内存序**——为什么 relaxed 够?因为 `ApproximateMemoryUsage` 只是估算(用于触发 Compaction 的启发式),不需要精确同步,读到稍旧的值也无所谓(下一刻再判一次即可)。注释里有个 TODO([util/arena.h:49-51](../leveldb/util/arena.h#L49-L51))问"其他字段不用原子 OK 吗?"——答案是 OK,因为 `alloc_ptr_` / `alloc_bytes_remaining_` / `blocks_` 只被写者(外部同步)访问,读者只读 `memory_usage_`。

4. **对齐的 sound 性**:`AllocateAligned` 保证返回地址至少 8 字节对齐,为 SkipList 的 `std::atomic<Node*>` 提供对齐担保。fallback 路径(走 `new char[]`)天然对齐(主流实现 `alignof(max_align_t) = 16`),fast path 用 `slop` 填充保证对齐。

### 反面对比 1:per-node new/delete

见 5.2 节。每次 Put 一次 malloc、每次覆盖/删除一次 free——慢、有碎片、有锁竞争。

### 反面对比 2:维护"空闲块链表"

```cpp
// 反面代码(更精细但更复杂):
class Arena {
  std::vector<std::pair<void*, size_t>> free_blocks_;  // 维护空闲块
 public:
  void Free(void* p, size_t n) { free_blocks_.push_back({p, n}); }
  char* Allocate(size_t n) {
    // 在 free_blocks_ 里找一块够大的,找不到再 new
  }
};
```

这本质上是手写 malloc。问题是:

- **MemTable 根本不调 Free**(中间不释放单块),所以 free_blocks_ 永远是空的——这套机制是死的。
- 即便有 Free(假设 LSM 改成原地更新),维护 free list、合并相邻空闲块、扫 bins——全部是开销和复杂度。
- LevelDB 选最简单的 bump,是因为它精确匹配 MemTable 的使用模式。**给错数据结构(如 LRU cache),bump 就不合适,要换带 free 的 allocator**。

### 反面对比 3:用 std::allocator / std::pmr

```cpp
// 反面代码(标准化但不够直接):
struct MemTableEntry { /* ... */ };
std::pmr::monotonic_buffer_resource mbr;
std::pmr::polymorphic_allocator<MemTableEntry> alloc(&mbr);
// 用 alloc 分配 entry
```

C++17 的 `std::pmr::monotonic_buffer_resource` 本质上就是 bump allocator。**为什么不直接用?** 两个原因:

1. **LevelDB 是 2011 年的代码**,C++17 那时还没出。
2. **Arena 的具体细节(`kBlockSize`、对齐、大块直通、`memory_usage_` 计账)是 LevelDB 特有的**——`pmr` 的 monotonic_buffer_resource 行为不同(它默认按 `max_align_t` 对齐,没有大块直通的优化)。LevelDB 需要这些细节,自己写 60 行代码最直接。

> **钉死这件事**:Arena 是一个**和 MemTable 使用模式精确匹配**的 bump allocator。它"只进不出"不是缺陷,是 LSM"只追加不原地改、整体一次性回收"在内存层的字面回响。换成任何带 free 的 allocator(per-node malloc、pmr with pool、free list)都是过度设计——MemTable 根本不 free,要 free 机制干嘛?这是工程上"less is more"的典型范例。

---

## 章末小结

这一章讲清了 MemTable 的内存来源——Arena + MemTable 装配:

1. **Arena 是 bump allocator**:`Allocate` = "够就指针前推,不够就开新块(`kBlockSize = 4096`)",`~Arena` = "遍历 `blocks_` 全部 delete[]"。无 per-entry free,无碎片,O(1) 分配。
2. **"只进不出"反而更快更省**:MemTable 中间永不释放单 entry,整体一次性回收,所以 bump 精确匹配使用模式。省掉 free 簿记、碎片、锁竞争。
3. **大块直通 + 对齐**:`bytes > kBlockSize/4` 走独立块,避免小块浪费;`AllocateAligned` 保证 8 字节对齐,为 SkipList 的 `std::atomic<Node*>` 提供对齐担保。
4. **MemTable 装配**:SkipList(存 `const char*`)+ Arena(存字节流)+ KeyComparator(从字节流解出 internal key,委托给 InternalKeyComparator)+ 引用计数 `refs_`。`Add` 拼字节流 memcpy 进 Arena 再插 SkipList;`Get` 用 LookupKey Seek,读到的 value 拷一份到出参。
5. **引用计数 `refs_`**:`Ref()` 加、`Unref()` 减到零就 `delete this`。析构链 `~MemTable → ~Arena → 全块 delete[]`,整体一次性回收。

回到主线:MemTable 是**前台**(写秒回的落点、读的第一站)。Arena 让写路径的内存分配几乎零开销(指针前推),SkipList 让写和读不互斥(无锁读)。这两件事合起来,把"前台吞吐"撑到了极致。MemTable 写满 ~4MB 后会被冻结成 Immutable,后台刷成 SSTable——那是第 4 篇 Compaction 的事。

### 五个"为什么"清单

1. **为什么 MemTable 不 per-node malloc/free?** MemTable 中间永不释放单 entry(LSM 不原地改,旧版本要等刷盘 + Compaction 整体丢弃),所以 per-node free 用不上。bump allocator 用 O(1) 指针前推 + 整体 delete[] 击败 per-node malloc 的所有开销(锁、元数据、碎片)。
2. **Arena 为什么没有 `Free` 方法?** 因为 MemTable 不需要单 entry 释放。Arena 在 `~Arena` 时一次性释放所有 `blocks_`,这是"只进不出"在回收端的体现。如果给一个会频繁释放单块的数据结构(如 LRU cache),Arena 不合适——它和 MemTable 的使用模式精确匹配。
3. **`AllocateFallback` 里 `bytes > kBlockSize/4 = 1024` 的阈值怎么来的?** 一个 4096 字节块剩 < 1024 时,塞一个 > 1024 的 entry 进去,剩余空间不够装绝大多数小 entry(典型 ~140 字节),所以那块剩余几乎是死的。直接开独立块,旧块剩余丢弃但 < 1024,微不足道。这个阈值是空间利用率和独立块数量的权衡。
4. **`AllocateAligned` 为什么 8 字节对齐?** `std::atomic<Node*>` 在主流平台要求 8 字节对齐(64 位指针)。不对齐会在 ARM/PowerPC 上 trap、在 x86 上有性能惩罚。SkipList 的 `NewNode` 用 `AllocateAligned` 拿到对齐内存,placement new 构造 Node,保证 next_ 数组的原子访问 sound。
5. **MemTable 的引用计数 `refs_` 谁在用?** `DBImpl` 的 `mem_` / `imm_` 持有时 Ref;读 Iterator 拿 MemTable Iterator 时 Ref(保证 Iterator 活着期间 MemTable 不析构);刷盘过程中 Ref(保证 Immutable 不被读 Iterator 释放)。`Unref` 减到零就 `delete this`,析构链 `~MemTable → ~Arena → 全块 delete[]`。这是"多个持有者共享、最后一个关灯"的标准引用计数模式。

### 想继续深入往哪钻

- **Arena 的 `memory_usage_` 用 `relaxed` 原子,其他字段不用原子**([util/arena.h:49-51](../leveldb/util/arena.h#L49-L51) 的 TODO):为什么 sound?因为 `alloc_ptr_` / `alloc_bytes_remaining_` / `blocks_` 只被写者(外部同步的 Insert 路径)访问,读者只读 `memory_usage_` 做"是否触发 Compaction"的估算,读稍旧值无所谓。这是"只在跨线程字段上加最小同步"的工程范例。
- **`MemTable::Add` 里 `(s << 8) | type` 直接位运算**,不调 `PackSequenceAndType`:这是热路径优化,把 P1-03 讲的编码内联到这里。看 [memtable.cc:94](../leveldb/db/memtable.cc#L94)。
- **`MemTableIterator` 的实现**([db/memtable.cc:46-72](../leveldb/db/memtable.cc#L46-L72)):它把 SkipList 的 Iterator 包成 `Iterator` 虚基类(P3-11 Iterator 抽象章详讲)。注意 `Seek(const Slice& k)` 用 `EncodeKey(&tmp_, k)` 把 Slice 编码成 SkipList 期望的"长度前缀 + 字节流"格式——这就是 SkipList 比较时 `GetLengthPrefixedSlice` 的对偶。
- **RocksDB 的 MemTable**:在 LevelDB 基础上加了多种实现(默认 skiplist,也有 hash skiplist、vector 等),Arena 也更精细(支持 NUMA、huge page)。但核心思想(bump + 不释放单块)和 LevelDB 一致。
- **`std::pmr::monotonic_buffer_resource`(C++17)**:标准库版的 bump allocator。对比 Arena,看哪些细节(`kBlockSize`、大块直通、`memory_usage_`)是 LevelDB 特有的工程权衡。

### 引出下一章

单次 `Put` 已经能写进 MemTable 了:API 调 `WriteBatch::Put`,WriteBatch 把操作打包成字节流,然后写 WAL + 插 MemTable。**但多线程并发 `Put` 时,LevelDB 怎么把 N 个写者的 batch 合并成 1 次 WAL 追加 + 1 次 MemTable 插入?** 这就是下一章 P1-06 的主题——**写组(group commit)**:leader 代表整组写、follower 干等的批处理模式,以及为什么这个设计能把写入吞吐拉满。这一章也回扣 P1-04 提到的"一把大锁 `mutex_` 保证同一时刻只有一个写者在 Insert"——写组正是这把大锁的协作机制。
