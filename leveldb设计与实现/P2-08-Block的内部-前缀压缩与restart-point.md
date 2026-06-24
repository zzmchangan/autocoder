# 第八章 · Block 的内部:前缀压缩与 restart point

> 篇:P2 持久化的第一道:SSTable 的格式
> 主线呼应:上一章(P2-07)讲了 SSTable 的四级布局——data block、meta block、metaindex block、index block 都是同一套"积木"(block),只是装的内容不同。这一章钻进这块积木的**内部**:一个 block 里的几十条 KV 怎么紧凑排列?相邻 key 大量重复的前缀怎么压掉?压掉之后又怎么保住二分查找的能力?这是 LevelDB 空间压缩的核心手段之一,也是 block 这套"通用积木"的内部秘密。

## 核心问题

**一个 block 里的相邻 KV,key 的前缀往往大量重复(尤其同 user_key 的多版本、或相近 user_key)。朴素存完整 key 太浪费。LevelDB 用"共享前缀压缩":每条 entry 只存"与前一条共享多少字节 + 不共享部分 + value"。但纯前缀压缩无法随机访问(读第 i 条要解压前 i-1 条),于是每隔 16 条存一个完整 key 的 restart point,让 block 仍能在 restart point 之间二分查找。这是"压缩省空间"与"二分要求随机访问"这对矛盾的精妙折中。**

读完本章你会明白:

1. 为什么相邻 internal key 的前缀大量重复(P1-03 讲过同 user_key 多版本,这一章看它在 SSTable 里的具体表现),朴素存完整 key 会浪费多少空间。
2. 共享前缀压缩的 entry 格式:`shared_bytes(varint32) + non_shared_bytes(varint32) + value_length(varint32) + key_delta + value`,以及 `shared_bytes == 0` 标记 restart point 的约定。
3. restart point 怎么解决"压缩后无法随机访问":每 16 条(`kBlockRestartInterval = 16`)存一个完整 key,二分先在 restart 数组里定位、再线性扫 ≤16 条。复杂度 O(log n / log 16) + O(16)。
4. `index_block` 为什么把 restart_interval 设成 1(每条都是 restart)——因为 index 是纯随机 Seek 的目录,不做前缀压缩,二分最快。
5. `DecodeEntry` 的快路径(三值都 <128 时各 1 字节)和 `Block::Iter::Seek` 的二分逻辑,源码逐行拆。

> **如果一读觉得太难**:先只记住三件事——① block 里每条 entry 不存完整 key,只存"和前一条共享多少字节 + 不共享部分";② 每 16 条存一个完整 key 的 restart point,block 尾部存 restart point 的 offset 数组;③ 读时先二分 restart 数组定位区间,再在区间内线性扫 ≤16 条。剩下的字节级细节可以回头再读。

---

## 8.1 一句话点破

> **相邻 key 共享前缀,LevelDB 不重复存——每条 entry 只记"共享多少 + 不共享部分"。但纯前缀压缩把"读第 i 条"变成"要解压前 i-1 条",随机访问 O(n)。折中:每 16 条存一个完整 key 当"锚"(restart point),block 尾部存这些锚的 offset。二分先在锚数组里定位区间,再线性扫 ≤16 条——压缩 + 二分两不误。**

这是结论,不是理由。本章倒过来拆:先看朴素存完整 key 为什么不行,再看纯前缀压缩为什么也不行,最后看 restart point 怎么把这两边的代价都压到最小。

---

## 8.2 为什么 block 要做前缀压缩

### 提出问题

P2-07 讲过,一个 data block 默认 4KB,里面排着几十条 KV。这些 KV 是**按 internal key 排序**的(P1-03 讲过,internal key = user_key + 8 字节 seq|type,排序时先比 user_key 升序、再比 seq 降序)。

排序后的 KV,key 的前缀大量重复。两种典型场景:

1. **同 user_key 多版本**:`Put("user:1001", v1)`、`Put("user:1001", v2)`、`Put("user:1001", v3)`——三条记录的 user_key 都是 `"user:1001"`,只有尾部 8 字节 seq|type 不同。如果各自存完整 internal key,前 9 字节 `"user:1001"` 重复存三遍。
2. **相近 user_key**:`"user:1001"`、`"user:1002"`、`"user:1003"`——前 5 字节 `"user:"` 完全一样,只有后 4 字节不同。海量小 key 的场景(比如按 id 编号的监控指标),这种前缀重复非常普遍。

量化一下:假设 100 万条 key,平均 user_key 20 字节,其中 15 字节是公共前缀。朴素存,100 万 × 20 = 20MB 的 key 数据;前缀压缩后,理论上 100 万 × 5(不共享部分)= 5MB,**省 75%**。在一个 4MB 的 SSTable 里,这个差距就是几 MB vs 几十 MB。

### 不这样会怎样

> **反面对比(每条存完整 key)**:block 体积膨胀,同样数量的 KV 要切更多 block,文件更大,读 block 的 I/O 更多。尤其多版本场景(同 user_key 反复改),旧版本白白占着 key 前缀的空间——这些空间本来可以让 block 多装几条 KV,减少 block 数量、减少 index entry、减少读 I/O。朴素存完整 key 是**空间放大**的一个直接来源。

> **反面对比(全局字典压缩)**:建一个"前缀 → 字典序号"的全局字典,每条 entry 只存序号。能 work,但要额外维护字典、字典本身占空间、字典改了所有 entry 都得改。LevelDB 不要这种复杂度。

所以 LevelDB 选择**相邻 entry 之间做共享前缀压缩**——只和前一条比、共享多少存多少,简单且高效。

---

## 8.3 共享前缀压缩的 entry 格式

### 所以这样设计

`block_builder.cc` 顶部注释把格式说得很清楚,看 [`table/block_builder.cc:5-27`](../leveldb/table/block_builder.cc#L5-L27):

```
// BlockBuilder generates blocks where keys are prefix-compressed:
//
// When we store a key, we drop the prefix shared with the previous
// string.  This helps reduce the space requirement significantly.
// Furthermore, once every K keys, we do not apply the prefix
// compression and store the entire key.  We call this a "restart
// point".  The tail end of the block stores the offsets of all of
// the restart points, and can be used to do a binary search when looking
// for a particular key.  Values are stored as-is (without compression)
// immediately following the corresponding key.
//
// An entry for a particular key-value pair has the form:
//     shared_bytes: varint32
//     unshared_bytes: varint32
//     value_length: varint32
//     key_delta: char[unshared_bytes]
//     value: char[value_length]
// shared_bytes == 0 for restart points.
//
// The trailer of the block has the form:
//     restarts: uint32[num_restarts]
//     num_restarts: uint32
// restarts[i] contains the offset within the block of the ith restart point.
```

每条 entry 的字节布局(ASCII 框图):

```
   一条 entry(连续字节,无对齐):
   ┌──────────────┬────────────────┬──────────────┬──────────────────┬────────────────┐
   │ shared_bytes │ non_shared_bytes│ value_length │  key_delta       │     value      │
   │  (varint32)  │   (varint32)    │  (varint32)  │ (non_shared 字节) │ (value_length) │
   └──────────────┴────────────────┴──────────────┴──────────────────┴────────────────┘
    ↑                                                              ↑
    和前一条 key 共享的字节数                                         本 entry 完整 value(不压缩)
    (restart point 必为 0)
```

三个 varint32 头 + key 的增量部分 + 完整 value。几个关键点:

1. **`shared_bytes`**:这条 entry 和**前一条 entry**的完整 key 共享了多少前缀字节。注意是和前一条比,不是和某个全局前缀。
2. **`non_shared_bytes`**:本 entry 的 key 中,不与前一条共享的部分长度。`shared + non_shared = 本 entry key 的完整长度`。
3. **`key_delta`**:key 的"不共享部分"原始字节。读 entry 时,`完整 key = 前一条完整 key 的前 shared 字节 + key_delta`。
4. **`value`**:直接追加,不压缩(value 可能是任意二进制,通用压缩交给 snappy/zstd 在 block 整体层面做,见 P2-07 的 `WriteBlock`)。
5. **restart point 的 entry**:`shared_bytes == 0`,`key_delta` 就是完整 key。这是"不压缩"的标记。

block 的尾部(ASCII 框图):

```
   block 整体(连续字节):
   ┌──────────────────────────────────────┬────────────────────────┬──────────────┐
   │   entry 0 | entry 1 | ... | entry M   │ restarts[0..num-1]      │ num_restarts │
   │   (所有 entry 顺序排列)                │ (uint32 × num,小端)      │   (uint32)   │
   └──────────────────────────────────────┴────────────────────────┴──────────────┘
    ↑                                      ↑                          ↑
    block 数据                              restart point 的 offset 数组  数组长度
```

`restarts[i]` 是第 i 个 restart point 在 block 内的 byte offset。`num_restarts` 是数组长度。block 的最后 4 字节永远是 `num_restarts`(固定 uint32,读它就知道有多少 restart point,进而知道 restart 数组从哪开始)。

### 8.3.1 `BlockBuilder::Add`:怎么写一条 entry

看 [`table/block_builder.cc:71-105`](../leveldb/table/block_builder.cc#L71-L105) 的 `Add`:

```cpp
void BlockBuilder::Add(const Slice& key, const Slice& value) {
  Slice last_key_piece(last_key_);
  assert(!finished_);
  assert(counter_ <= options_->block_restart_interval);
  assert(buffer_.empty()  // No values yet?
         || options_->comparator->Compare(key, last_key_piece) > 0);   // :76 —— key 必须严格大于 last_key(保证有序)
  size_t shared = 0;
  if (counter_ < options_->block_restart_interval) {                   // :78 —— 还没到 restart 间隔
    // See how much sharing to do with previous string
    const size_t min_length = std::min(last_key_piece.size(), key.size());
    while ((shared < min_length) && (last_key_piece[shared] == key[shared])) {
      shared++;                                                        // :82 —— 逐字节比,算共享前缀长度
    }
  } else {
    // Restart compression
    restarts_.push_back(buffer_.size());                               // :86 —— 记录 restart point offset
    counter_ = 0;                                                      // :87 —— 重置计数器
    // shared 保持 0,不压缩
  }
  const size_t non_shared = key.size() - shared;                       // :89

  // Add "<shared><non_shared><value_size>" to buffer_
  PutVarint32(&buffer_, shared);                                       // :92
  PutVarint32(&buffer_, non_shared);                                   // :93
  PutVarint32(&buffer_, value.size());                                 // :94

  // Add string delta to buffer_ followed by value
  buffer_.append(key.data() + shared, non_shared);                     // :97 —— 只追加不共享部分
  buffer_.append(value.data(), value.size());                          // :98 —— value 全量追加

  // Update state
  last_key_.resize(shared);                                            // :101 —— 更新 last_key_:前 shared 字节保留
  last_key_.append(key.data() + shared, non_shared);                   // :102 —— 追加新的 non_shared
  assert(Slice(last_key_) == key);                                     // :103 —— 断言:此时 last_key_ == 本 entry 的 key
  counter_++;
}
```

逻辑直球:

1. **算共享前缀**(:78-83):如果还没到 restart 间隔(`counter_ < block_restart_interval`),和 `last_key_` 逐字节比,算出 `shared`。
2. **restart point**(:84-88):如果到了 restart 间隔,记录当前 buffer 偏移进 `restarts_` 数组,`counter_` 清零,`shared` 保持 0(本条 entry 不压缩)。
3. **写三个 varint32 头**(:92-94):`shared`、`non_shared`、`value_length`。
4. **写 key_delta 和 value**(:97-98):只追加 key 的不共享部分 + 完整 value。
5. **更新 last_key_**(:101-102):`resize(shared)` 保留前缀、`append(non_shared)` 追加增量。这一步是 O(non_shared),不是 O(key.size())——因为 shared 字节已经在 `last_key_` 里,不用动。

注意 :101-102 这个细节——`last_key_.resize(shared)` 把 `last_key_` 截到前 `shared` 字节,然后 append 新的 non_shared 部分。**为什么这样写而不直接 `last_key_ = key`?** 因为 `resize + append` 在 shared 占大头时只拷贝 non_shared 字节(几字节),而 `last_key_ = key` 要拷贝整个 key(几十字节)。这是热路径的 micro-optimization,百万次 Add 累计可观。

### 8.3.2 `BlockBuilder::Finish`:写 restart 数组

block 写完所有 entry 后,`Finish` 把 restart 数组追加到末尾,看 [`table/block_builder.cc:61-69`](../leveldb/table/block_builder.cc#L61-L69):

```cpp
Slice BlockBuilder::Finish() {
  // Append restart array
  for (size_t i = 0; i < restarts_.size(); i++) {
    PutFixed32(&buffer_, restarts_[i]);                                // :64 —— 每个 restart offset 用 4 字节小端
  }
  PutFixed32(&buffer_, restarts_.size());                              // :66 —— 最后写 num_restarts
  finished_ = true;
  return Slice(buffer_);
}
```

`PutFixed32` 是 4 字节小端 uint32(见 [`util/coding.h:54-62`](../leveldb/util/coding.h#L54-L62))。注意 restart offset **不用 varint**,而用**固定 4 字节**——为什么?因为 restart offset 是"block 内偏移",最大也就 4KB 量级(4 字节能表达 4GB,绰绰有余),且 restart 数组要支持**随机访问**(二分时 `restarts[mid]` 直接读),固定长度才能 O(1) 索引。varint 解码有分支,二分时每步都要解,慢。

构造函数里有个细节,看 [`table/block_builder.cc:40-44`](../leveldb/table/block_builder.cc#L40-L44):

```cpp
BlockBuilder::BlockBuilder(const Options* options)
    : options_(options), restarts_(), counter_(0), finished_(false) {
  assert(options->block_restart_interval >= 1);
  restarts_.push_back(0);  // First restart point is at offset 0          // :43 —— block 开头(block 第 0 字节)永远是第一个 restart point
}
```

**第一个 restart point 永远在 offset 0**(block 第一条 entry 的位置)。这意味着 block 的第一条 entry 的 `shared_bytes` 一定是 0(没有前一条可比),且它天然是个 restart point。这个约定让二分逻辑的边界情况简化——永远至少有一个 restart point。

### 8.3.3 一个具体例子

假设往 block 里依次加这 4 条 KV(用 user_key 简化示意,internal key 多 8 字节 seq|type 尾,原理一样):

```
entry 0:  key = "apple",       value = "v1"   ← restart point 0(offset 0)
entry 1:  key = "application", value = "v2"   ← 同 restart 区间(假设 interval=16)
entry 2:  key = "apply",       value = "v3"
entry 3:  key = "banana",      value = "v4"
```

逐条看编码(假设 block_restart_interval = 16,所以这 4 条都在第一个 restart 区间内):

| entry | shared | non_shared | key_delta | value | 说明 |
|-------|--------|-----------|-----------|-------|------|
| 0 ("apple") | 0 | 5 | "apple" | "v1" | 第一条,restart point,shared=0 |
| 1 ("application") | 5 | 6 | "cation" | "v2" | 和 "apple" 共享前 5 字节 "apple",不共享 "cation" |
| 2 ("apply") | 4 | 2 | "y" | "v3" | 和 "application" 共享前 4 字节 "appl",不共享 "y" |
| 3 ("banana") | 0 | 6 | "banana" | "v4" | 和 "apply" 无共享前缀("b" != "a"),shared=0 |

注意 entry 3——`"banana"` 和前一条 `"apply"` 第一个字节就不同(`"b" != "a"`),所以 `shared = 0`,但它**不是 restart point**(因为 `counter_` 还没到 16)。读它时,要先知道前一条 `"apply"` 的完整 key,然后 `完整 key = 前 0 字节 + "banana" = "banana"`。这一点很关键:**shared=0 不等于 restart point**。restart point 由 `counter_ == block_restart_interval` 决定,shared 是不是 0 由实际前缀决定。读 entry 时,只有 restart point 的 entry 才能"独立解码",非 restart entry 即使 shared=0 也得先知道前一条的位置。

### 8.3.4 restart point 的间隔:`kBlockRestartInterval = 16`

真实默认值在 [`include/leveldb/options.h:106`](../leveldb/include/leveldb/options.h#L106):

```cpp
int block_restart_interval = 16;
```

**16 条 entry 一个 restart point**。这个数字怎么来的?是个经验权衡:

- **太小(比如 4)**:restart point 太密,完整 key 存太多,压缩收益小。
- **太大(比如 256)**:restart point 太稀疏,二分后线性扫描的区间太长(最坏扫 256 条),读慢。
- **16**:二分后最坏扫 16 条(每条解 varint + 拼 key,微秒级),压缩收益仍可观(每 16 条才存一个完整 key)。16 是 LevelDB 作者经验选的,RocksDB 也沿用。

可以配置(用户自己设 `options.block_restart_interval`),`BlockBuilder` 构造时 `assert(options->block_restart_interval >= 1)` 保证至少 1。

> **钉死这件事**:block_restart_interval = 16 是"压缩率"和"查询延迟"的平衡点。读 block 时,二分 restart 数组(O(log(num_restarts))),定位到某个 restart 区间,再线性扫 ≤16 条——总复杂度 O(log(num_restarts) + 16)。对 4KB block 来说,num_restarts 也就几十个,log 后 5~6 步二分 + 16 步线性,极快。

---

## 8.4 读 block:restart point 怎么让二分查找回来

### 提出问题

纯前缀压缩有个致命问题——**随机访问退化成 O(n)**。读第 i 条 entry,要知道前一条的完整 key(因为本条 key = 前 shared 字节 + key_delta),前一条又要知道更前一条……一路回溯到 block 开头。100 万条 entry 的 block,读最后一条要解压 999,999 条。完全不可用。

restart point 就是来解决这个的。

### 不这样会怎样

> **反面对比(纯前缀压缩,不设 restart)**:写很紧凑,但读一条 key 要从 block 开头线性扫到目标位置。4KB block 几十条 entry 还能忍(线性扫 50 条),但如果有人把 block_size 调到 64KB(RocksDB 常见配置),几千条 entry 线性扫就明显慢了。而且二分根本做不了——二分要求"任意位置可读",纯前缀压缩做不到。读路径的核心是 Seek + Next,Seek 必须支持随机定位,否则退化成全扫。

### 所以这样设计

restart point 的核心思想:**压缩是局部的,每隔 K 条"重启"一次**。每 K 条 entry 里,第一条(restart point)的 `shared_bytes == 0`,自带完整 key,可以独立解码。这样 block 就被 restart point 切成了若干"压缩段",每段内最多 K 条 entry。

二分查找的流程:

1. **先在 restart 数组里二分**:restart 数组是固定 4 字节一条的 offset 数组,可以 O(1) 索引 `restarts[mid]`。二分找到"最后一个 restart point,它的完整 key < target"。
2. **定位到这个 restart point**:从它的 offset 开始,它自带完整 key,能独立解码。
3. **从这个 restart point 往后线性扫**:最多扫 K-1 条(本段剩下的),边扫边维护"当前完整 key"(shared + non_shared 拼出来),直到找到第一个 key >= target。

复杂度:O(log(num_restarts)) + O(K)。对默认 K=16、num_restarts 几十个的 block,总共几十次比较,微秒级。

这个逻辑的真实实现在 `Block::Iter::Seek`,看 [`table/block.cc:164-227`](../leveldb/table/block.cc#L164-L227):

```cpp
  void Seek(const Slice& target) override {
    // Binary search in restart array to find the last restart point
    // with a key < target
    uint32_t left = 0;
    uint32_t right = num_restarts_ - 1;
    int current_key_compare = 0;

    if (Valid()) {
      // 如果已经在扫描,用当前位置做起点(优化:target 在当前 key 之后时直接往后找)
      current_key_compare = Compare(key_, target);
      if (current_key_compare < 0) {
        left = restart_index_;                          // :178 —— 当前 key 比 target 小,left 提到当前 restart 区间
      } else if (current_key_compare > 0) {
        right = restart_index_;                         // :180 —— 当前 key 比 target 大,right 压到当前 restart 区间
      } else {
        return;                                         // :183 —— 正好就在当前 key
      }
    }

    while (left < right) {
      uint32_t mid = (left + right + 1) / 2;             // :188 —— 向上取整(因为 left = mid 而不是 mid-1)
      uint32_t region_offset = GetRestartPoint(mid);     // :189 —— restarts[mid] 的 offset
      uint32_t shared, non_shared, value_length;
      const char* key_ptr =
          DecodeEntry(data_ + region_offset, data_ + restarts_, &shared,
                      &non_shared, &value_length);
      if (key_ptr == nullptr || (shared != 0)) {         // :194 —— restart point 的 shared 必须是 0(自校验)
        CorruptionError();
        return;
      }
      Slice mid_key(key_ptr, non_shared);                // :198 —— restart point 自带完整 key = key_delta(non_shared 字节)
      if (Compare(mid_key, target) < 0) {
        // Key at "mid" is smaller than "target".  Therefore all
        // blocks before "mid" are uninteresting.
        left = mid;                                      // :202 —— mid key < target,left 提到 mid
      } else {
        // Key at "mid" is >= "target".  Therefore all blocks at or
        // after "mid" are uninteresting.
        right = mid - 1;                                 // :206 —— mid key >= target,right 压到 mid-1
      }
    }

    // We might be able to use our current position within the restart block.
    // This is true if we determined the key we desire is in the current block
    // and is after than the current key.
    assert(current_key_compare == 0 || Valid());
    bool skip_seek = left == restart_index_ && current_key_compare < 0;   // :214 —— 优化:如果二分结果还是当前区间且 target 在当前 key 之后,跳过 SeekToRestartPoint
    if (!skip_seek) {
      SeekToRestartPoint(left);                          // :216 —— 定位到 left 这个 restart point
    }
    // Linear search (within restart block) for first key >= target
    while (true) {
      if (!ParseNextKey()) {                              // :220 —— 解下一条 entry(维护 key_ 和 value_)
        return;
      }
      if (Compare(key_, target) >= 0) {                  // :223 —— 找到第一个 key >= target,停
        return;
      }
    }
  }
```

几个关键细节:

1. **二分的对象是 restart 数组**(:187-208),不是所有 entry。restart 数组是固定 4 字节一条,`GetRestartPoint(mid)` 直接 `DecodeFixed32(data_ + restarts_ + mid*4)`(见 `block.cc:100-103`),O(1)。
2. **restart point 的 key 能独立解码**(:191-198):`DecodeEntry` 解出 shared/non_shared/value_length,因为 restart point 的 shared 必为 0,所以 `key_delta`(non_shared 字节)就是完整 key。注意 :194 的 `shared != 0` 校验——如果某个号称 restart point 的 entry 解出来 shared 不是 0,说明文件损坏,报 Corruption。
3. **二分向上取整**(:188 `(left + right + 1) / 2`):因为 `left = mid`(不是常见的 `left = mid + 1`),要避免死循环。这是二分查找的经典写法细节,当 `left = mid` 时必须向上取整。
4. **二分找到的是"最后一个 restart point,其 key < target"**:这样从这个 restart point 开始往后扫,一定能覆盖到 target(因为下一个 restart point 的 key >= target,target 不在那个区间)。
5. **线性扫描**(:219-226):从 restart point 开始,`ParseNextKey` 一条条解,维护 `key_`(完整 key,通过 shared + non_shared 拼),直到 `key_ >= target`。最多扫 K-1 = 15 条(本 restart 区间剩下的)。

### 8.4.1 `ParseNextKey`:维护完整 key

`ParseNextKey` 是读 entry 的核心,看 [`table/block.cc:250-277`](../leveldb/table/block.cc#L250-L277):

```cpp
  bool ParseNextKey() {
    current_ = NextEntryOffset();                                // :251 —— 下一条 entry 的 offset
    const char* p = data_ + current_;
    const char* limit = data_ + restarts_;  // Restarts come right after data
    if (p >= limit) {
      // No more entries to return.  Mark as invalid.
      current_ = restarts_;
      restart_index_ = num_restarts_;
      return false;
    }

    // Decode next entry
    uint32_t shared, non_shared, value_length;
    p = DecodeEntry(p, limit, &shared, &non_shared, &value_length);
    if (p == nullptr || key_.size() < shared) {                  // :264 —— key_(前一条完整 key)必须至少有 shared 字节
      CorruptionError();
      return false;
    } else {
      key_.resize(shared);                                        // :268 —— 截到前 shared 字节(保留前缀)
      key_.append(p, non_shared);                                 // :269 —— 追加 non_shared(本条的不共享部分)
      value_ = Slice(p + non_shared, value_length);               // :270 —— value 紧跟在 key_delta 后
      while (restart_index_ + 1 < num_restarts_ &&
             GetRestartPoint(restart_index_ + 1) < current_) {
        ++restart_index_;                                         // :273 —— 如果跨过了下一个 restart point,更新 restart_index_
      }
      return true;
    }
  }
```

注意 :268-269 这两行——**完整 key = `key_.resize(shared)` 保留前缀 + `append(non_shared)` 追加增量**。这和 `BlockBuilder::Add` 的 :101-102 是**镜像操作**。写时把完整 key 拆成"前缀 + 增量"存,读时把"前缀(从上一条 key 来)+ 增量(从 entry 来)"拼回完整 key。

:264 的 `key_.size() < shared` 校验很重要——如果 entry 声称共享了 N 字节,但当前 `key_`(上一条完整 key)连 N 字节都没有,说明文件损坏(比如第一条 entry 的 shared 不该 > 0),报 Corruption。

:271-274 的 `restart_index_` 维护:扫描过程中,如果当前 entry 的 offset 越过了下一个 restart point,就推进 `restart_index_`。这个维护是为了支持 `Prev()`(反向遍历)和 Seek 的优化(`skip_seek`)。

---

## 8.5 `DecodeEntry` 的快路径:varint 的 micro-optimization

读 entry 第一步是解三个 varint32(shared、non_shared、value_length)。这三个值在很多场景下都很小:

- `shared`:相邻 key 共享前缀,通常几十字节以内。
- `non_shared`:不共享部分,几字节到几十字节。
- `value_length`:value 大小,小 value 几十字节,大 value 可能 KB 级(但常见场景偏小)。

varint32 编码规则:0~127 用 1 字节,128~16383 用 2 字节……所以**三个值都 < 128 时,各 1 字节,共 3 字节**。这是绝大多数 entry 的实际情况。

`DecodeEntry` 为此设了快路径,看 [`table/block.cc:55-75`](../leveldb/table/block.cc#L55-L75):

```cpp
static inline const char* DecodeEntry(const char* p, const char* limit,
                                      uint32_t* shared, uint32_t* non_shared,
                                      uint32_t* value_length) {
  if (limit - p < 3) return nullptr;                             // :58 —— 至少 3 字节才能走快路径
  *shared = reinterpret_cast<const uint8_t*>(p)[0];
  *non_shared = reinterpret_cast<const uint8_t*>(p)[1];
  *value_length = reinterpret_cast<const uint8_t*>(p)[2];
  if ((*shared | *non_shared | *value_length) < 128) {           // :62 —— 三个值高 7 位都为 0(< 128),快路径
    // Fast path: all three values are encoded in one byte each
    p += 3;
  } else {
    // 慢路径:逐个 GetVarint32Ptr 解(可能有某个值 >= 128,用了多字节 varint)
    if ((p = GetVarint32Ptr(p, limit, shared)) == nullptr) return nullptr;
    if ((p = GetVarint32Ptr(p, limit, non_shared)) == nullptr) return nullptr;
    if ((p = GetVarint32Ptr(p, limit, value_length)) == nullptr) return nullptr;
  }

  if (static_cast<uint32_t>(limit - p) < (*non_shared + *value_length)) {
    return nullptr;                                              // :72 —— 剩余字节不够装 key_delta + value,损坏
  }
  return p;                                                      // :74 —— 返回 key_delta 起点
}
```

快路径的精妙:

1. **先直接读 3 字节**(:59-61),假设三个值各 1 字节。
2. **`(*shared | *non_shared | *value_length) < 128`**(:62):三个值按位或,如果结果 < 128,说明三个值各自都 < 128(因为 varint 的"还有后续"标志是高位 bit7,值 < 128 意味着 bit7 = 0,即单字节编码)。这是一个**用一次比较代替三次比较**的位运算技巧。
3. 快路径命中,`p += 3` 直接跳过 3 字节,返回。
4. 慢路径才逐个 `GetVarint32Ptr`(它内部也有"第一个字节高位 0 就 1 字节返回"的快路径,见 `coding.h:108-118`)。

> **钉死这件事**:这个快路径是 LevelDB 热路径的典型 micro-optimization。读 block 时,每条 entry 都要调 `DecodeEntry`,4KB block 几十条 entry、一次范围扫描可能几千条。绝大多数 entry 三个值都 < 128,快路径把"解三个 varint"压缩成"读 3 字节 + 一次比较",省下两次分支和两次 `GetVarint32Ptr` 调用。编译器还能把 `p[0]/p[1]/p[2]` 优化成一次 3 字节 load。这种"为常见 case 设快路径"的思路,贯穿 LevelDB 的 varint、SkipList、Arena 实现。

---

## 8.6 反向遍历(`Prev`):restart point 的另一个用处

前缀压缩让"往前走"(`Next`)很自然——`ParseNextKey` 一条条解就行。但"往后走"(`Prev`)就麻烦了——本条 entry 只记了"和前一条共享多少",要知道前一条得回头,要知道前一条的前一条再回头……纯前缀压缩下 `Prev` 退化成 O(n^2)。

restart point 怎么救?看 [`table/block.cc:143-162`](../leveldb/table/block.cc#L143-L162) 的 `Prev`:

```cpp
  void Prev() override {
    assert(Valid());

    // Scan backwards to a restart point before current_
    const uint32_t original = current_;
    while (GetRestartPoint(restart_index_) >= original) {        // :148 —— 往前找第一个 offset < original 的 restart point
      if (restart_index_ == 0) {
        // No more entries
        current_ = restarts_;
        restart_index_ = num_restarts_;
        return;
      }
      restart_index_--;
    }

    SeekToRestartPoint(restart_index_);                          // :158 —— 定位到这个 restart point(它的 key 能独立解码)
    do {
      // Loop until end of current entry hits the start of original entry
    } while (ParseNextKey() && NextEntryOffset() < original);    // :161 —— 从 restart point 往后扫,直到"下一条"就是 original
  }
```

逻辑:

1. 找到当前 entry 所在 restart 区间的**前一个 restart point**(:147-156)。
2. `SeekToRestartPoint` 定位到它(:158)。
3. 从它开始 `ParseNextKey` 往后扫(:159-161),直到"下一条 entry 的 offset 就是 original"(即扫到 original 的前一条为止)。

复杂度:找 restart point O(num_restarts)(线性扫 restart 数组,最坏几十步)+ 从 restart point 扫到 original 之前 O(K)(最多 16 条)。总共 O(num_restarts + K)。

这比纯前缀压缩的 O(n^2) 好太多,但比 `Next` 的 O(1) 还是慢。所以 LevelDB 的反向遍历天然比正向慢——这是前缀压缩的代价之一。如果你需要大量反向扫描,可能要考虑别用 block 内部迭代器(或者 RocksDB 的 `BlockBasedTableOptions::data_block_index_type`,可以配二分友好的 index)。

> **钉死这件事**:restart point 不只服务于 Seek 的二分,也服务于 Prev 的"回到已知完整 key 的锚点"。它是前缀压缩 block 里唯一"自带完整 key、能独立解码"的位置——任何需要"从头开始解码"的操作(Seek 二分定位、Prev 回溯、SeekToFirst),都要先落到一个 restart point 上。restart point 是前缀压缩 block 的"坐标系原点"。

---

## 8.7 index block 的特殊配置:restart_interval = 1

P2-07 提过,index block 的 `block_restart_interval = 1`(每条都是 restart,不做前缀压缩)。这一章能讲清为什么了。

看 [`table/table_builder.cc:35`](../leveldb/table/table_builder.cc#L35) 和 [`table/table_builder.cc:90`](../leveldb/table/table_builder.cc#L90):

```cpp
    index_block_options.block_restart_interval = 1;              // :35 —— index block 每条都是 restart point
```

**为什么 index block 不做前缀压缩?**

1. **index block 的访问模式是纯随机 Seek**:读任意 key,都要先在 index block 里 Seek 一次。不像 data block,Seek 之后可能还要顺序 Next 扫一段。
2. **每条都是 restart = 每条 entry 自带完整 key = 二分时不用解压前缀**:`Block::Iter::Seek` 的二分是在 restart 数组里二分,如果每条都是 restart,那二分的粒度就是每条 entry,等价于直接在所有 entry 上二分,O(log n) 精确二分。如果 restart_interval=16,二分只能定位到 16 条的区间,还要线性扫最多 15 条。
3. **index entry 数量远少于 data**:每 4KB data block 才有一条 index entry,1GB 库也就几十万条 index entry。每条 entry 多存几字节完整 key(index key 通常短,几字节),总体积可接受。换来的 Seek 加速(精确二分 vs 区间二分 + 线性扫)在 index 这种"每次 Get 都 Seek"的热点上非常值。

对比 data block:

| 属性 | data block | index block |
|------|-----------|-------------|
| restart_interval | 16(默认) | 1 |
| 访问模式 | Seek + 顺序 Next | 纯随机 Seek |
| 压缩收益 | 高(相邻 key 前缀重复多) | 低(index key 已用 `FindShortestSeparator` 缩短,前缀重复少) |
| Seek 复杂度 | O(log(num_restarts) + 16) | O(log(num_entries))(精确二分) |
| entry 数量 | 多(4KB 几十条) | 少(每 4KB data 一条) |

> **钉死这件事**:data block 和 index block 用同一套 `BlockBuilder`,但配置不同——data 要压缩(index 不要),data 允许 Seek 后线性扫(index 要精确二分)。这是"通用积木 + 按场景调参"的设计典范。P2-09 的 metaindex block 也是用 `BlockBuilder`,但通常只有一两条 entry,参数无所谓。

---

## 8.8 技巧精解:共享前缀压缩 + restart point 的折中

这一章的硬核技巧就是**共享前缀压缩 + restart point** 这对组合。它解决的核心矛盾是"**压缩要求相邻相关、随机访问要求彼此独立**"。

### 这个技巧在做什么

让一个 block 同时满足两个看似矛盾的要求:

1. **紧凑存储**:相邻 key 共享前缀,只存增量。
2. **随机访问**:读任意 key 能 O(log n + K) 定位,不用从头扫。

### 用了什么手段

1. **写时**(BlockBuilder::Add):每条 entry 和前一条比,记 `shared/non_shared/key_delta`。每 K 条(K=16)存一个 restart point(shared=0,完整 key)。
2. **block 尾**:存 restart point 的 offset 数组(固定 4 字节一条)+ num_restarts。
3. **读时**(Block::Iter::Seek):二分 restart 数组(固定长度,O(1) 索引)定位区间 → 落到 restart point → 线性扫 ≤K 条 → 找到目标。

### 为什么 sound

1. **压缩是局部的,边界是已知的**:restart point 是"压缩段"的边界,它的 shared=0,完整 key 自带,能独立解码。这让"从任意 restart point 开始解码"成为可能——这是随机访问的基础。
2. **restart 数组用固定 4 字节,不用 varint**:restart offset 要在二分里 O(1) 索引(`restarts[mid]`),varint 解码有分支,固定长度才能直接 `DecodeFixed32(data_ + restarts_ + mid*4)`。这是"二分友好的数据布局"的标准选择。
3. **K=16 是经验权衡**:压缩收益(每 16 条存一个完整 key)和查询延迟(二分后扫 16 条)的平衡。RocksDB 也用 16,说明这个值普适。
4. **第一个 restart point 永远在 offset 0**(BlockBuilder 构造函数 `restarts_.push_back(0)`):这保证了 block 开头永远有个能独立解码的锚,SeekToFirst 直接从 offset 0 开始,边界情况简化。
5. **DecodeEntry 快路径**:三值都 <128 时各 1 字节,把"解三个 varint"压缩成"读 3 字节 + 一次位运算判断"。这是热路径的 micro-optimization。

### 反面对比 1:每条存完整 key

见 8.2 节。空间膨胀,尤其多版本场景(同 user_key 几十个版本,key 前缀全重复存)。4KB block 装的 entry 变少,block 数量变多,index entry 变多,读 I/O 变多。空间放大的直接来源。

### 反面对比 2:纯前缀压缩,不设 restart

读一条 key 要从 block 开头线性扫到目标,O(n)。4KB block 几十条还能忍,但:

- block_size 调大(64KB)时,几千条 entry 线性扫明显慢。
- 二分根本做不了——二分要求"任意位置可读",纯前缀压缩的 entry i 依赖 entry i-1,不能随机跳。
- Seek 退化成全扫,读路径(第 3 篇 P3-13)的核心操作就废了。

### 反面对比 3:用全局字典压缩

建一个"前缀 → 字典序号"的全局字典。能压缩更多,但:

- 字典本身占空间,且要随 block 一起存(放哪?怎么版本化?)。
- 字典改了,所有引用它的 entry 都得改(immutable 文件改不了)。
- 解码要查字典,缓存不友好(字典可能不在 L1)。
- 复杂度大增,收益(比相邻前缀压缩多省几个百分点)不抵代价。

LevelDB 选择"相邻 entry 共享前缀",是最简单的局部压缩——不需要字典、不需要全局状态、解码只用前一条的 key(在迭代器里已经维护着)。简单到极致,收益却可观。

### 反面对比 4:把 K 设成 1(每条都是 restart,等于不压缩)

这就退化成 index block 的配置(:8.7 节)。data block 不选这个,因为:

- data block 相邻 key 前缀重复多(尤其多版本),不压缩太浪费。
- data block 的访问模式允许 Seek 后线性扫(范围查询、归并扫描),K=16 的线性扫开销可接受。
- index block 才选 K=1,因为它是纯随机 Seek 的目录,精确二分比压缩更重要。

> **钉死这件事**:共享前缀压缩 + restart point 是"局部压缩 + 锚点二分"的经典组合。它把"压缩"和"随机访问"这对矛盾,用"每 K 条一个不压缩的锚"这个简单约定化解。K=16 是经验值,既拿到了大部分压缩收益,又把查询延迟控制在 O(log n + 16)。这个设计被 RocksDB、Pebble、甚至其他存储引擎广泛沿用,是 LSM block 格式的事实标准。

---

## 章末小结

这一章讲清了 block 内部的前缀压缩与 restart point:

1. **共享前缀压缩**:相邻 entry 只存 `shared + non_shared + key_delta + value`,shared 是与前一条 key 共享的字节数。压缩率在多版本、相近 key 场景可达 50%~75%。
2. **restart point**:每 `kBlockRestartInterval = 16` 条存一个完整 key(shared=0),block 尾部存 restart offset 数组(固定 4 字节一条)+ num_restarts。
3. **读路径**:二分 restart 数组(O(log(num_restarts)))+ 线性扫 ≤16 条(O(K))。总复杂度 O(log(num_restarts) + 16)。
4. **DecodeEntry 快路径**:三个 varint 都 <128 时各 1 字节,3 字节 + 一次位运算搞定,热路径 micro-optimization。
5. **index block restart_interval=1**:纯随机 Seek 的目录,每条都是 restart,精确二分,不做前缀压缩。data block restart_interval=16,压缩优先。

回到主线:这一章服务**前台**(读路径要解 block;前缀压缩直接削空间放大——同样数量 KV 占更少 block、读更少 I/O)。block 这套"通用积木"的内部,是 LevelDB 空间效率的核心——四级布局(data/meta/metaindex/index)都复用它,前缀压缩让每个 block 装下更多 KV,restart point 让压缩后的 block 仍能快速 Seek。读一条 key 的完整路径(footer → index → data),每一步都依赖 block 内部的这套机制。

### 五个"为什么"清单

1. **为什么 block 要做前缀压缩?** 相邻 internal key 前缀大量重复(同 user_key 多版本、相近 user_key)。朴素存完整 key 是空间放大的直接来源。前缀压缩可省 50%~75% 的 key 体积,让 4KB block 装更多 KV、减少 block 数量、减少 index entry、减少读 I/O。
2. **restart point 的间隔为什么是 16?** 经验权衡。太小(restart 太密)压缩收益小;太大(restart 太稀疏)二分后线性扫区间太长。16 是"压缩率"和"查询延迟"的甜点,RocksDB 也沿用。可配(`options.block_restart_interval`),`assert >= 1`。
3. **为什么 restart offset 用固定 4 字节 uint32,不用 varint?** restart 数组要在二分里 O(1) 索引(`restarts[mid]`)。varint 解码有分支,固定长度才能 `DecodeFixed32(data_ + restarts_ + mid*4)` 直接读。二分友好的数据布局要求元素定长。
4. **为什么 index block 的 restart_interval=1(每条都是 restart)?** index block 是纯随机 Seek 的目录,每次 Get 都要在它里 Seek。每条都是 restart = 每条自带完整 key = 二分时不用解压前缀 = 精确 O(log n) 二分。data block 才用 16(Seek 后常顺序扫,压缩优先)。
5. **纯前缀压缩(不设 restart)为什么不行?** 读第 i 条 entry 要知道前一条完整 key(本条 key = 前 shared 字节 + key_delta),前一条又要更前一条……一路回溯到 block 开头,O(n)。二分根本做不了(二分要求任意位置可读)。Seek 退化成全扫,读路径核心操作废了。restart point 是"压缩"和"随机访问"这对矛盾的解药。

### 想继续深入往哪钻

- **官方注释**:[`table/block_builder.cc`](../leveldb/table/block_builder.cc) 顶部 5-27 行的注释是格式权威,本章的 entry 布局都以它为准。读它胜过任何二手资料。
- **`Block::Iter` 的完整实现**:[`table/block.cc:77-278`](../leveldb/table/block.cc#L77-L278) 是一个完整的、支持 Seek/SeekToFirst/SeekToLast/Next/Prev 的迭代器。重点看 Seek 的二分(:164-227)和 ParseNextKey 的 key 维护(:250-277)。
- **`FindShortestSeparator` 怎么缩短 index key**:[`include/leveldb/comparator.h`](../leveldb/include/leveldb/comparator.h) 的 `Comparator::FindShortestSeparator`,默认 `BytewiseComparator` 实现在 `util/comparator.cc`。这是 index block 能用短 key 的根。
- **block 压缩(snappy/zstd)**:block 的"整体压缩"在 P2-07 的 `WriteBlock` 讲过(type 字段 + CRC)。前缀压缩是 entry 级,snappy/zstd 是 block 级,两者叠加。
- **RocksDB 怎么扩展了 block 格式**:RocksDB 加了 `data_block_index_type`(可配二分索引类型)、`hash_index`(在 restart 数组上加一层 hash,加速 Seek)、`partitioned_index`(把大 index block 切小)。但"前缀压缩 + restart point"的骨架没变。

### 引出下一章

block 存得紧凑了——前缀压缩 + restart point 让 4KB block 装下更多 KV,空间放大被削掉一块。但读一条 key,仍然要**先把整个 block 读进内存、解压、二分**。能不能在读 block 之前先问一句:"这个 block 八成有没有我的 key?" 如果答案大概率没有,就根本不读这个 block,省一次 I/O。这就是下一章 P2-09 的事——**filter block(布隆过滤器)**:它在 data block 之上加一道"挡查询"的关卡,用 k 个 hash 把"这个 key 大概在不在"的判断做到 O(1),把绝大多数无效查询挡在读 data block 之前。这是 LevelDB 削读放大的关键武器。
