# 第三章 · InternalKey 与 SequenceNumber:多版本键的编码

> 篇:P1 写入的前台
> 主线呼应:上一章我们立起了 API 三基石——`Slice`(零拷贝)、`Status`(成功零开销)、`Comparator`(键序虚基类)。但 Comparator 比较的"键",在 LevelDB 内部其实**不是**用户传入的 user_key,而是一个被打包过的 internal key。这一章讲清楚这个打包:为什么不能只存 user_key?怎么把 `(user_key, sequence_number, type)` 塞进一个**可排序**的字节串,让 LSM 多版本 + MVCC 快照全部退化成"按序取第一个"的简单规则。

## 核心问题

**同一个 key 被改了又改、删了又写,内存里要同时存它好几个版本,怎么区分新旧?LevelDB 的答案是把 `(user_key, seq, type)` 三个东西编码进一个可比的字节串——internal key——让比较器先比 user_key 升序、再按 seq 降序,于是"同 user_key 中最新版本天然排最前"。这一条编码规则,是 LSM 多版本和 MVCC 快照共同的根。**

读完本章你会明白:

1. 为什么不能只存 user_key(改和删的多个版本会冲突,无法分辨新旧);墓碑(tombstone)是 `kTypeDeletion`,正常值是 `kTypeValue`——这两个常量的真实值是什么(`db/dbformat.h` 里定义),它们怎么和 seq 一起打包。
2. internal key 的字节布局:user_key 变长 + 8 字节尾部 `seq(56位) | type(8位)` 小端打包。凭什么 type 只占 8 位、seq 占 56 位,凭什么这样能装下 `(1<<56 - 1)` 个序号几乎不溢出。
3. `InternalKeyComparator::Compare` 怎么做"先按 user_key 升序、再按 seq|type 降序"的两段比较;凭什么降序让"最新版本排最前",反过来会怎样。
4. 这一套编码怎么让"取最新版本"在 MemTable、SSTable、Compaction、Snapshot 四个场景里**零额外逻辑**地退化成"取排序后第一个"。

> **如果一读觉得太难**:先只记住三件事——① LevelDB 内部存的不是 user_key,而是 `user_key + 8字节(seq|type)` 的 internal key;② 这 8 字节里 seq 占高 56 位、type 占低 8 位,小端编码;③ 比较时同 user_key 内部按 seq 降序排,所以最新版本天然排最前,读时拿第一个就走。剩下的细节是"凭什么这么编码这么比",可以回头再读。

---

## 3.1 一句话点破

> **LevelDB 不存 user_key,它存 internal_key = user_key ‖ (seq << 8 | type)。比较器先比 user_key 升序、再比这 8 字节降序——于是同一个 user_key 的所有版本里,seq 最大者(也就是最新版本)天然排最前。读取只要按 internal key 顺序扫到第一个,就拿到了正确结果。**

这是结论,不是理由。本章倒过来拆:先看"只存 user_key"会撞上什么墙,再看 LevelDB 怎么用"打包编码 + 降序比较"这两个手段把多版本问题变成纯排序问题,最后钉死 type 的 8 位编码与 seq 的 56 位上限。

---

## 3.2 为什么不能只存 user_key

### 提出问题

P0-01 我们讲过:LSM 不做原地更新,一条 `Put(k, v1)` 进来,只是把 `(k, v1)` 加进 MemTable;后来 `Put(k, v2)` 又进来,再加一条 `(k, v2)`;再后来 `Delete(k)` 进来,加一条墓碑。**同一个 key k,在 LSM 里可能有 N 条记录共存**——这是"只追加"换写入吞吐的必然结果。

那问题来了:

1. 读 k 的时候,**到底该返回 v2 还是 v1**?怎么区分新旧?
2. `Delete(k)` 之后,这条墓碑怎么标记?它和 v1、v2 是同一个 user_key k,凭什么识别"这是删除"?
3. MemTable、SSTable、Snapshot 都需要"在某个时间点看到的 k 是什么值",怎么表达"时间点"?

### 不这样会怎样

**如果 MemTable 里只存 user_key → value,且不记任何"版本"信息**,那:

- 改一次 k,v1 还在、v2 又叠在上面。读的时候按"取最后一条"勉强能区分顺序,但 MemTable 是 SkipList,顺序是按 key 排的,不是按写入顺序排的——同一个 k 在 SkipList 里只能存一份。所以必须扩展 key 才能让"同一个 user_key 的多个版本"共存。
- 删除?你得有个特殊标记,但这特殊标记又是 user_key 上不带的。
- 快照(Consistent Snapshot)?你得知道"这一刻 k 的值是哪个版本",没有版本号就没法实现 MVCC。

> **反面对比**:假设 MemTable 的 SkipList key 就是 `std::string`(user_key),`Put(k, v1)` 然后 `Put(k, v2)` 会发生什么?SkipList 的 `Insert` 把同 key 视为已存在(或覆盖、或拒绝),总之**第二个 Put 没法落地**——因为比较器看不出"这是另一个版本"。LSM 的"只追加多版本"地基直接塌了。所以 key 必须扩展,且扩展的部分必须能让比较器区分版本。

### 所以这样设计

LevelDB 给每一条记录附加两个东西:

1. **`sequence`(序列号)**:一个全局递增的 64 位整数(实际只用 56 位,见后),每次 `WriteBatch` 写入时分配一批连续的 seq 号。每条记录的 seq 唯一标识"这是第几号版本"。
2. **`type`(类型)**:`kTypeValue`(普通值)或 `kTypeDeletion`(墓碑)。两种类型,只需 1 位理论上就够,但 LevelDB 给了 8 位(为未来扩展留余量)。

看 [`db/dbformat.h:51-67`](../leveldb/db/dbformat.h#L51-L67):

```cpp
// Value types encoded as the last component of internal keys.
// DO NOT CHANGE THESE ENUM VALUES: they are embedded in the on-disk
// data structures.
enum ValueType { kTypeDeletion = 0x0, kTypeValue = 0x1 };
// kValueTypeForSeek defines the ValueType that should be passed when
// constructing a ParsedInternalKey object for seeking to a particular
// sequence number (since we sort sequence numbers in decreasing order
// and the value type is embedded as the low 8 bits in the sequence
// number in internal keys, we need to use the highest-numbered
// ValueType, not the lowest).
static const ValueType kValueTypeForSeek = kTypeValue;

typedef uint64_t SequenceNumber;

// We leave eight bits empty at the bottom so a type and sequence#
// can be packed together into 64-bits.
static const SequenceNumber kMaxSequenceNumber = ((0x1ull << 56) - 1);
```

> **钉死这件事(本书对总纲/部分二手资料的修正)**:LevelDB 的真实常量是 **`kTypeDeletion = 0x0`,`kTypeValue = 0x1`**——**删除的值更小,普通值的值更大**。一些二手资料(包括我们总纲的早期版本)误写成"`kTypeValue=1, kTypeDeletion=2`",**这是错的**。源码 `db/dbformat.h:54` 一行字面量摆着,毋庸置疑。这个顺序不是无关紧要的——下一节讲 `kValueTypeForSeek = kTypeValue` 时,你会看到它就是依赖"type 大的排在 seq 同样的位置之后",所以 `kTypeValue` 必须是最大的那个 ValueType。

`sequence` 是全局递增的,`kMaxSequenceNumber = (1<<56) - 1`,也就是 seq 最多有 $2^{56}$ 个(约 $7.2 \times 10^{16}$)。这是个天文数字——即便每秒写 1 亿条,够用 22 年。所以实际中 seq 永远不会溢出,LevelDB 的代码里也没有任何"seq 回绕"的处理(默认假设它够大)。

把这两件东西和 user_key 打包到一起,就成了 internal key——下一节讲怎么打包。

---

## 3.3 Internal key 的字节布局:尾部追加 8 字节

### 提出问题

我们要把 `(user_key, seq, type)` 三个东西编码成一个**可排序的字节串**(因为 MemTable/SSTable 全是按 key 排序的),并且排序后满足两条规则:

1. **同 user_key 内部,seq 大者排前面**(最新版本优先)。
2. **不同 user_key 之间,user_comparator 怎么定就怎么定**(默认按字节升序)。

怎么编码才能让现有比较器(`memcmp` 之类的逐字节比)天然产出这个顺序?

### 不这样会怎样

**朴素方案 A:存一个三元组 `(user_key, seq, type)`,自己写一个三段比较函数。** 能 work,但麻烦——所有存 key 的基础设施(SkipList、SSTable 的 block、Iterator 的归并)都得改成"三元组比较"。后面 P2-08 前缀压缩也做不了(它假设 key 是可比的字节串)。

> **反面对比(三元组版)**:SkipList 的模板参数变成 `SkipList<std::tuple<Slice, uint64_t, uint8_t>>`,比较器是自定义 lambda。SSTable 的 block builder 也得改——它假设 key 是字节串、按字节比。整套基础设施改动巨大,而且 `Slice`/`memcmp` 这些零拷贝优势都用不上。

**朴素方案 B:把 seq 和 type 拼成 8 字节,放在 user_key 前面。** 也能 work,但这样比较时先比 seq|type,只有 seq 相同时才比 user_key——同一条记录的"key 排序"会乱掉,SkipList 没法按 user_key 顺序遍历。

### 所以这样设计

LevelDB 的方案非常干净——**把 seq|type 编码成 8 字节,追加到 user_key 尾部**。看 [`db/dbformat.h:80-83`](../leveldb/db/dbformat.h#L80-L83) 和 [`db/dbformat.cc:15-24`](../leveldb/db/dbformat.cc#L15-L24):

```cpp
inline size_t InternalKeyEncodingLength(const ParsedInternalKey& key) {
  return key.user_key.size() + 8;              // user_key 长度 + 8 字节尾部
}

// db/dbformat.cc:15-19(静态函数,文件内可见)
static uint64_t PackSequenceAndType(uint64_t seq, ValueType t) {
  assert(seq <= kMaxSequenceNumber);
  assert(t <= kValueTypeForSeek);
  return (seq << 8) | t;                       // seq 占高 56 位,type 占低 8 位
}

// db/dbformat.cc:21-24
void AppendInternalKey(std::string* result, const ParsedInternalKey& key) {
  result->append(key.user_key.data(), key.user_key.size());
  PutFixed64(result, PackSequenceAndType(key.sequence, key.type));
}
```

字节布局(ASCII 框图):

```
一条 internal key 的字节布局(总长 = user_key.size() + 8):
 ┌──────────────────────────┬──────────────────────────────────┐
 │      user_key (变长)      │  seq(56b, 高位)  ‖  type(8b,低位)  │
 │                          │        小端 8 字节(uint64)         │
 └──────────────────────────┴──────────────────────────────────┘
  ↑                          ↑
  字节 [0 .. len-9]           字节 [len-8 .. len-1]
```

8 字节尾部的内部布局(把 64 位看成一个数):

```
        64 位 tag = (seq << 8) | type,小端编码成 8 字节:
 ┌───────────────────────────────────────┬─────┐
 │              seq (高 56 位)             │type │
 │                                         │(8位)│
 └───────────────────────────────────────┴─────┘
   bit [63 .. 8]                              bit [7..0]
```

几个关键细节:

1. **`PackSequenceAndType(seq, t) = (seq << 8) | t`**。seq 左移 8 位腾出低 8 位,type 塞进去。
2. **`PutFixed64`** 把这个 uint64 以**小端**写进 8 字节(详见 [`util/coding.h:64-76`](../leveldb/util/coding.h#L64-L76) 的 `EncodeFixed64`)。小端意味着低字节在前——type 在最低字节,所以磁盘上**第一个字节就是 type**。
3. **`InternalKeyEncodingLength = user_key.size() + 8`**:固定 8 字节尾部,长度可从 internal key 长度反推 user_key 长度。

为什么 type 8 位、seq 56 位?

- **type 8 位**:目前只有 `kTypeDeletion` 和 `kTypeValue` 两个值,理论上 1 位够,但 LevelDB 给了 8 位——留出 $2^8 = 256$ 种类型的扩展空间(RocksDB 后来确实加了好几种 type,如 `kTypeMerge`、`kTypeBlobIndex` 等,这就是为什么这 8 位设计是对的)。
- **seq 56 位**:`kMaxSequenceNumber = (1 << 56) - 1 ≈ 7.2e16`,够大。8 位让给 type 之后,seq 仍有 56 位,实际不溢出。如果给 type 更多位(比如 16),seq 就只有 48 位($2.8 \times 10^{14}$),按每秒 1 亿写仍能用 80 年,但 LevelDB 选了"seq 56 + type 8"这个分配,留够 seq 余量。
- **8 字节尾,正好 64 位**:对齐机器字长,`DecodeFixed64` 一次 8 字节 load 就能读出来(编译器优化到一条 `mov`)。

### 3.3.1 解码:ParseInternalKey

解码就是把 8 字节尾 `DecodeFixed64` 出来,右移 8 位拿 seq,低 8 位拿 type,看 [`db/dbformat.h:171-181`](../leveldb/db/dbformat.h#L171-L181):

```cpp
inline bool ParseInternalKey(const Slice& internal_key,
                             ParsedInternalKey* result) {
  const size_t n = internal_key.size();
  if (n < 8) return false;                                 // 至少要有 8 字节尾
  uint64_t num = DecodeFixed64(internal_key.data() + n - 8);  // 读最后 8 字节
  uint8_t c = num & 0xff;                                  // 低 8 位 = type
  result->sequence = num >> 8;                             // 高 56 位 = seq
  result->type = static_cast<ValueType>(c);
  result->user_key = Slice(internal_key.data(), n - 8);    // 前 n-8 字节 = user_key
  return (c <= static_cast<uint8_t>(kTypeValue));          // type 必须合法
}
```

注意 `return (c <= kTypeValue)`——这是个轻量的完整性校验。如果 type 不是 0 或 1(可能是文件损坏),返回 false,让上层报 Corruption。这个返回值上层会判(比如 `WriteBatch::Iterate` 里读 tag 时会判 tag 合法性)。

### 3.3.2 一个具体的字节例子

假设 user_key = `"hello"`(5 字节),seq = `0x00000000000003`(3),type = `kTypeValue`(0x01)。

打包:

```
PackSequenceAndType(3, 1) = (3 << 8) | 1 = 0x0000000000000301
小端编码成 8 字节(低字节在前): 01 03 00 00 00 00 00 00
                                       ↑  ↑
                                       type=1  seq 低字节=3(其余 0)
```

完整的 internal key(13 字节):

```
 h  e  l  l  o | 01 03 00 00 00 00 00 00
↑ user_key     ↑ 尾 8 字节(小端)
```

再比如 `Delete("hello")` 在 seq=3 时:

```
PackSequenceAndType(3, 0) = 0x0000000000000300
小端: 00 03 00 00 00 00 00 00
完整 internal key:  h e l l o | 00 03 00 00 00 00 00 00
                                ↑ type=0(kTypeDeletion)
```

> **钉死这件事**:internal key 的编码是**追加 8 字节**到 user_key 尾部。这 8 字节小端存一个 uint64,高 56 位是 seq、低 8 位是 type。比较 internal key 时,逐字节比和"先比 user_key 再比 seq|type"在结果上**等价**(因为 user_key 长度相等时,8 字节尾就紧接其后)。下一节讲比较器怎么利用这一点。

---

## 3.4 InternalKeyComparator:两段比较,先升后降

### 提出问题

internal key 字节串是装好了,但**比较规则**不是简单的 memcmp——我们要:

1. **user_key 升序**(委托给 user_comparator,默认按字节升序)。
2. **同 user_key 内部,seq|type 降序**(seq 大的排前面,这样最新版本在最前)。

### 不这样会怎样

**朴素方案:直接 memcmp 整个 internal key。** 问题:两条 internal key 如果 user_key 相同、seq 不同,memcmp 比 8 字节尾——`seq=3` 的尾是 `01 03 00...`,`seq=5` 的尾是 `01 05 00...`,memcmp 会判 seq=3 < seq=5,即 seq 小的排前面。**这正好是升序,与"最新版本排前面"的要求相反。**

> **反面对比(两个都升序)**:假设比较器对 seq|type 段也用升序。那同一个 user_key 的版本里,seq 最小(最早)的版本排最前,seq 最大(最新)的版本排最后。一次 `Get(k)`,Seek(k) 落到最早的版本上,要往前一路扫过 N 个旧版本才到最新——读放大爆炸,且代码复杂(还要在 SkipList 里反向遍历同 user_key)。完全错乱。

### 所以这样设计

`InternalKeyComparator` 在比较 seq|type 段时,**主动反转比较结果**,让大的排前面。看真实源码 [`db/dbformat.cc:47-63`](../leveldb/db/dbformat.cc#L47-L63):

```cpp
int InternalKeyComparator::Compare(const Slice& akey, const Slice& bkey) const {
  // Order by:
  //    increasing user key (according to user-supplied comparator)
  //    decreasing sequence number
  //    decreasing type (though sequence# should be enough to disambiguate)
  int r = user_comparator_->Compare(ExtractUserKey(akey), ExtractUserKey(bkey));
  if (r == 0) {
    const uint64_t anum = DecodeFixed64(akey.data() + akey.size() - 8);  // a 的 seq|type
    const uint64_t bnum = DecodeFixed64(bkey.data() + bkey.size() - 8);  // b 的 seq|type
    if (anum > bnum) {
      r = -1;                  // ← a 的 seq 更大,反而判 a "更小"(排前面)
    } else if (anum < bnum) {
      r = +1;                  // ← a 的 seq 更小,反而判 a "更大"(排后面)
    }
  }
  return r;
}
```

`ExtractUserKey` 是去掉 8 字节尾,看 [`db/dbformat.h:95-98`](../leveldb/db/dbformat.h#L95-L98):

```cpp
inline Slice ExtractUserKey(const Slice& internal_key) {
  assert(internal_key.size() >= 8);
  return Slice(internal_key.data(), internal_key.size() - 8);
}
```

逻辑直球:

1. 先按 user_key 比(用 user_comparator)。
2. 如果 user_key 不同,直接返回 user_key 比较结果(升序)。
3. 如果 user_key 相同,读出两边的 8 字节 seq|type,比大小——**符号反转**:anum 大的反判 -1。

为什么反转?因为我们要降序。`Compare(a, b)` 的语义是:返回 `<0` 表示 `a < b`(a 排前),返回 `>0` 表示 `a > b`(a 排后)。如果 anum 大于 bnum(seq 更大、版本更新),我们要 a 排前,所以返回 `-1`。这就是源码里那个看似别扭的 `anum > bnum → r = -1` 的真相。

注释里还有一句"decreasing type (though sequence# should be enough to disambiguate)"——同 user_key 同 seq 的情况下,按 type 降序。这种 case 极罕见(seq 一般不会撞),但代码顺带处理了:它把整个 seq|type 当成一个 64 位数一起比,而不是分开比 seq 再比 type。这也解释了 `kValueTypeForSeek = kTypeValue` 的设计——seek 时用最大的 type,确保 seek 落点在"同 seq 的所有 type 之前",见 3.5 节。

> **钉死这件事**:`InternalKeyComparator` 是 user_comparator 的**装饰器**——它先调用 user_comparator 比 user_key 部分,再叠加一段降序比较。这一段降序是 LSM 多版本能成立的**唯一原因**:同 user_key 的版本里,最新的排最前,读时取第一个就走,无需任何额外逻辑。

### 3.4.1 一个排序例子

user_key="a" 的三个版本:seq=3/value, seq=2/value, seq=1/deletion。
编码成 internal key(尾部 8 字节,假设 type 在低字节):

```
internal key for ("a", seq=3, kTypeValue=1):  "a" | 01 03 00 00 00 00 00 00
internal key for ("a", seq=2, kTypeValue=1):  "a" | 01 02 00 00 00 00 00 00
internal key for ("a", seq=1, kTypeDeletion=0): "a" | 00 01 00 00 00 00 00 00
```

按 `InternalKeyComparator::Compare` 排序:

1. user_key 都是 "a",都相等,进入 seq|type 段比较。
2. ("a", 3) 的 anum = 0x0301,("a", 2) 的 bnum = 0x0201。anum > bnum → r=-1,前者排前。
3. ("a", 2) 的 anum = 0x0201,("a", 1) 的 bnum = 0x0100。anum > bnum → r=-1,前者排前。

最终顺序(从前到后):

```
("a", seq=3, value)  ← 最新
("a", seq=2, value)
("a", seq=1, deletion)  ← 最老(墓碑)
```

一次 `Get("a")`,Seek 落到 `("a", seq=3, value)`,读到 value,返回。**根本不会扫到 seq=2 和 seq=1**——这就是 LSM 多版本读的全部魔法,降序比较 + 取第一个。

```mermaid
flowchart LR
    subgraph 同 user_key="a" 的版本
        A["('a', seq=3, value)"] --> B["('a', seq=2, value)"]
        B --> C["('a', seq=1, deletion)"]
    end
    Get["Get('a')"] -.Seek.-> A
    A -.返回.-> Out["value @ seq=3"]
    style A fill:#dbeafe
    style Out fill:#dcfce7
```

---

## 3.5 这一套编码怎么让"取最新版本"零逻辑退化

### 提出问题

`InternalKeyComparator` 让"最新版本排最前",那读、写、Compaction、Snapshot 这几个场景怎么利用这一点?

### 所以这样设计

**写**:`MemTable::Add` 把 `(user_key, seq, type, value)` 直接编码成 internal key 存进 Arena,然后插入 SkipList([memtable.cc:76-100](../leveldb/db/memtable.cc#L76-L100))。SkipList 用 `InternalKeyComparator` 排序,自动把同 user_key 的新版本排在前面。

**读**:`Get("k")` 时,LevelDB 构造一个**比所有真实版本都"新"的 lookup key**,Seek 到这个点,然后往后走,直到遇到第一个 user_key != "k" 的 entry,期间第一个有效版本就是要的答案。看 [`db/dbformat.h:184-216`](../leveldb/db/dbformat.h#L184-L216) 的 `LookupKey`:

```cpp
class LookupKey {
 public:
  LookupKey(const Slice& user_key, SequenceNumber sequence);
  // ...
  Slice memtable_key() const { return Slice(start_, end_ - start_); }
  Slice internal_key() const { return Slice(kstart_, end_ - kstart_); }
  Slice user_key() const { return Slice(kstart_, end_ - kstart_ - 8); }
 private:
  //    klength  varint32               <-- start_
  //    userkey  char[klength]          <-- kstart_
  //    tag      uint64
  //                                    <-- end_
  const char* start_;
  const char* kstart_;
  const char* end_;
  char space_[200];  // Avoid allocation for short keys
};
```

构造函数 [`db/dbformat.cc:117-134`](../leveldb/db/dbformat.cc#L117-L134):

```cpp
LookupKey::LookupKey(const Slice& user_key, SequenceNumber s) {
  size_t usize = user_key.size();
  size_t needed = usize + 13;  // A conservative estimate
  char* dst;
  if (needed <= sizeof(space_)) {
    dst = space_;                                  // ← 短键:内联 space_[200],零堆分配
  } else {
    dst = new char[needed];
  }
  start_ = dst;
  dst = EncodeVarint32(dst, usize + 8);            // klength = user_key + 8
  kstart_ = dst;
  std::memcpy(dst, user_key.data(), usize);        // user_key
  dst += usize;
  EncodeFixed64(dst, PackSequenceAndType(s, kValueTypeForSeek));  // ← 用 kValueTypeForSeek!
  dst += 8;
  end_ = dst;
}
```

注意最后一行——`PackSequenceAndType(s, kValueTypeForSeek)`,**type 用的是 `kValueTypeForSeek`(=kTypeValue=1),不是某个真实 type**。这就是为什么前面强调"`kTypeValue` 必须是最大的 ValueType"——seek 时把 type 设成最大值,确保:

- 任何真实的 entry(seq=s, type=value)的 tag ≤ seek tag。
- 任何真实的 entry(seq=s, type=deletion)的 tag < seek tag(deletion 的 type=0 < 1)。
- 所以 Seek 后第一个 entry,要么是 (s, type=value) 这个最新版本,要么是 seq < s 的旧版本(继续往后找)。

读时调用方拿到当前快照的 sequence(Snapshot 章 P7-21 详讲),构造 LookupKey,Seek,然后 SkipList/MergingIterator 自然往后扫,遇到第一个 user_key 相同且 seq <= snapshot seq 的 entry 就是答案。**这一整个机制没有为"找最新版本"写任何特殊代码**,全是"降序比较 + 取第一个"的天然结果。

**Compaction**:归并时,遇到同 user_key 的多个版本,最新版本在最前。归并输出时,新版本先写,旧版本(被覆盖的)丢弃,墓碑只有在所有更深层都没同 user_key 时才丢弃(详见 P4-16)。整个判断依赖"版本按 internal key 顺序排"——编码保证了这一点。

**Snapshot**:一次 `GetSnapshot()` 只是返回当前的 `sequence`,后续读时把 lookup key 的 seq 设成这个 snapshot seq,所有 > snapshot seq 的版本天然被 Seek 跳过(它们排得比 lookup key 还前,但 user_key 不同,Seek 落不到它们上)。**快照隔离的实现,零额外存储**,只是个 sequence 比较。

---

## 3.6 技巧精解

这一章技巧精解挑两个:internal key 的尾部追加编码(为什么这么编最经济),以及 InternalKeyComparator 的两段比较(为什么降序是 LSM 多版本成立的关键)。

### 技巧精解 1:尾部追加编码 + seq|type 8 字节打包

**这个技巧在做什么**:把 `(user_key, seq, type)` 三元组编码进一个**单一可比的字节串**,让所有"按 key 排序"的基础设施(SkipList、SSTable block、归并)零改动复用。

**用了什么手段**:在 user_key 尾部追加 8 字节,这 8 字节小端存 `(seq << 8) | type`。`PackSequenceAndType(seq, t)` 一行位运算搞定。

**为什么 sound**:

1. **8 字节对齐 64 位机器字长**:`DecodeFixed64` 一次 load 就能读出来,编译器优化到一条 `mov`(`util/coding.h:91-103` 注释明说)。如果用 7 字节(56 位)就要字节拼凑,慢且容易写错。
2. **小端编码跨平台一致**:LevelDB 钉死 little-endian(`util/coding.h:6` 顶部注释明说),无视宿主机字节序。这样写出来的 SSTable 在任何机器上读出来都一致——这是 LevelDB 文件可移植性的底层保证。
3. **追加在尾部而非头部**:这让 user_key 在 internal key 的前缀,`ExtractUserKey` 只是一次 Slice 构造(`Slice(data, len - 8)`,O(1)),无需拷贝。如果编码在头部,提取 user_key 还要先跳过 8 字节,且比较时 user_key 不在前缀位置,memcmp 不能直接复用。
4. **type 在最低字节**:小端编码下,8 字节尾的第一个字节就是 type。这样 `ParseInternalKey` 解码 type 时只需读一个字节,某些快路径甚至能不解 seq 就判出 type。
5. **seq 56 位,type 8 位**:`kMaxSequenceNumber = (1<<56) - 1 ≈ 7.2e16`,够大;type 8 位有 256 种,够未来扩展。RocksDB 后来加了 `kTypeMerge`、`kTypeBlobIndex` 等,8 位完全够用。

**反面对比 1(三元组版)**:见 3.3 节。SkipList/SSTable 全套基础设施要改,`Slice` 零拷贝优势用不上,前缀压缩做不了。

**反面对比 2(把 seq|type 放头部)**:`ExtractUserKey` 不再是 O(1) Slice 构造,需要偏移。比较时,memcmp 整个字节串会先比 seq|type(因为在前),只有 seq 相同才比 user_key——这跟我们要的"先 user_key 后 seq"完全相反,比较器要写得很绕。

**反面对比 3(用 ASCII 文本编码 seq)**:比如把 seq 编码成可读的 "0000000000000003" 字符串,追加到 user_key 后。能 work,但浪费空间(16 字节 vs 8 字节),且 memcmp 大端字符串排序方向和小端 uint64 不一致,要额外反转。LevelDB 选了二进制小端,最经济最一致。

> **钉死这件事**:internal key 编码的精妙在于——**把多版本问题变成纯排序问题**。本来"找最新版本"是个状态查询(要存元数据、要写逻辑判断),LevelDB 通过巧妙编码,把它退化成"按 internal key 排序取第一个"。所有数据结构(SkipList、SSTable)都用同一套排序基础设施,无需知道"版本"这个概念。这是 LSM + MVCC 的根。

### 技巧精解 2:InternalKeyComparator 的两段比较(先升后降)

**这个技巧在做什么**:让"同 user_key 的最新版本排最前"这个语义,通过一个简单的比较函数实现。

**用了什么手段**:比较函数分两段——user_key 段用 user_comparator 升序比较;seq|type 段把 8 字节尾 `DecodeFixed64` 出来,**符号反转地比较**(anum > bnum 时返回 -1)。

**为什么 sound**:

1. **user_key 段委托给 user_comparator**:这保证了 InternalKeyComparator 在 user_key 比较上完全和 user_comparator 一致——用户自定义的 Comparator(比如按 uint64 比)在 internal key 上也工作。这是装饰器模式的典型用法。
2. **seq|type 作为一个 64 位数一起比**:不分两段(seq 一段、type 一段),而是合成 `(seq << 8) | type` 一起比。这样同 seq 时,按 type 降序排(type 大的在前),刚好匹配 `kValueTypeForSeek = kTypeValue`——seek 时用 kTypeValue 这个最大 type,保证 seek 落点在所有真实 entry 之前。
3. **符号反转的语义**:返回 `-1` 表示 a 排前,返回 `+1` 表示 a 排后。我们想让 seq 大的排前,所以 anum(seq 大) 时返回 -1。这一行代码 `if (anum > bnum) r = -1;` 看着反直觉,但就是降序的标准写法。
4. **同 user_key 内部全等**:`anum == bnum` 时 r 保持 0,视为相等。理论上 seq 全局唯一不会撞,但代码兜底处理了相等 case。

**反面对比 1(两段都升序)**:见 3.4 节。最新版本排最后,Get 要扫完所有旧版本才到最新,读放大爆炸。

**反面对比 2(只用 seq,不用 type)**:如果只比 seq 不比 type,那 seek 时设 type=kTypeValue 没意义。实际上同 seq 的 entry type 可能不同(罕见但合法),需要 type 也参与比较才能完整定义序。LevelDB 选择把 seq 和 type 打包成一个 64 位数一起比,简洁且正确。

**反面对比 3(用自定义 lambda 而非继承 Comparator)**:`InternalKeyComparator` 继承 `Comparator`,意味着它可以替换 user_comparator 用在任何需要 `Comparator*` 的地方(SkipList 模板、SSTable builder)。如果改成自定义函数对象,基础设施要改,装饰器叠加(InternalKeyComparator 之上还可以再包)也做不了。

> **钉死这件事**:`InternalKeyComparator` 是 user_comparator 的装饰器,两段比较"先升后降"是 LSM 多版本成立的关键。**这一行 `if (anum > bnum) r = -1;` 看着反直觉,但它是"最新版本排最前"的全部魔法**——没有这一行反转,LSM 的多版本读就退化成扫遍所有版本。

---

## 章末小结

这一章讲清了 LevelDB 的多版本编码:

1. **internal key 字节布局**:`user_key` + 8 字节尾(小端 uint64 = `(seq << 8) | type`)。
2. **type 的真实值**:`kTypeDeletion = 0`,`kTypeValue = 1`——注意删除更小、值更大,这是 `kValueTypeForSeek = kTypeValue` 设计的依据(总目录二手资料常写错)。
3. **seq 56 位,type 8 位**:`kMaxSequenceNumber = (1<<56)-1 ≈ 7.2e16`,够大;type 256 种,够扩展。
4. **InternalKeyComparator 两段比较**:user_key 升序(委托 user_comparator)+ seq|type 降序(符号反转)。最新版本天然排最前。

回到主线:多版本属于**前台**(服务于"读拿到正确最新值"),也是 MVCC 快照的根(第 7 篇 P7-21 详讲)。这一章没有进入 MemTable 内部结构,但 internal key 是 MemTable 里真正存的"键"——下一章讲 MemTable 用什么数据结构把这些 internal key 排好序、还能"写者加锁、读者无锁"。

### 五个"为什么"清单

1. **为什么 LevelDB 不存 user_key 而存 internal key?** 同一个 user_key 会有多个版本(改了又改、删了又写),要区分新旧必须给每条记录附 seq+type。把 (user_key, seq, type) 打包成可比字节串,多版本问题就退化成纯排序问题。
2. **type 真实值是什么,为什么删除=0、值=1?** 源码 `db/dbformat.h:54` 明写:`kTypeDeletion = 0x0, kTypeValue = 0x1`。删除更小,这样 seek 时用最大的 type(kValueTypeForSeek=kTypeValue)能保证落点在所有真实 entry 之前。**注意:总纲早期版本误写为 `kTypeValue=1, kTypeDeletion=2`,已修正。**
3. **为什么 type 8 位、seq 56 位?** type 目前两个值,理论 1 位够,给 8 位(256 种)留扩展空间——RocksDB 后来加了 kTypeMerge 等。seq 56 位(`kMaxSequenceNumber ≈ 7.2e16`),每秒 1 亿写够用 22 年,实际不溢出。
4. **为什么 seq|type 段降序、user_key 段升序?** user_key 升序是用户期望的(SkipTable/SSTable 按 user_key 顺序遍历)。seq 降序是为了让最新版本排最前——读时 Seek 落到最新版本,取第一个就走,无需扫旧版本。反转这一行(`anum > bnum → r = -1`)是 LSM 多版本成立的关键。
5. **Snapshot 怎么靠这一套实现?** GetSnapshot 只是返回当前 sequence,读时把 lookup key 的 seq 设成 snapshot seq,所有 > snapshot seq 的版本天然被 Seek 跳过(它们 user_key 相同但 seq 更大,排得比 lookup key 还前,Seek 落不到)。零额外存储,纯 seq 比较——见 P7-21。

### 想继续深入往哪钻

- `InternalKey` 类的字段和 `SetFrom`/`Encode`/`DecodeFrom`/`user_key` 方法,见 [`db/dbformat.h:134-164`](../leveldb/db/dbformat.h#L134-L164)。它就是把 internal key 字节串包进一个 owning 的 std::string,提供 RAII 包装。
- `LookupKey` 的小对象优化 `char space_[200]`,见 [`db/dbformat.h:215`](../leveldb/db/dbformat.h#L215)。短键(总长 ≤ 200 字节)直接内联,无需堆分配——这是热路径的 micro-optimization。
- `InternalKeyComparator::FindShortestSeparator` 和 `FindShortSuccessor` 怎么为 SSTable 的 index block 服务,见 [`db/dbformat.cc:65-97`](../leveldb/db/dbformat.cc#L65-L97)。P2-08 前缀压缩章详讲。
- `kValueTypeForSeek` 的设计意图,源码注释 [`db/dbformat.h:55-61`](../leveldb/db/dbformat.h#L55-L61) 写得很清楚——它是 seek 时的"边界标记",必须用最大的 ValueType。
- RocksDB 在 type 8 位里加了哪些新类型(`kTypeMerge`、`kTypeBlobIndex` 等),展示了 LevelDB 这 8 位设计的前瞻性。

### 引出下一章

internal key 是 MemTable 里真正存的"键"——`(user_key, seq, type)` 打包好,可排序。但 MemTable 用什么数据结构把这些 internal key 排好序、还能让"写者加锁、读者无锁"地高并发?这就是下一章 SkipList 的事:为什么不用红黑树(红黑树并发读要锁,跳表能原子读),几何概率层数怎么来的(每个节点 $1/4$ 概率提升一层),`std::atomic<Node*>` + 合适内存序凭什么能让读者无锁、不读到撕裂的指针。SkipList 是 MemTable 的骨架,也是 LevelDB 唯一一处真正用了无锁并发的核心代码。
