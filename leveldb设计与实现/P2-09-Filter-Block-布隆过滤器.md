# 第九章 · Filter Block:布隆过滤器

> 篇:P2 持久化的第一道:SSTable 的格式
> 主线呼应:上一章(P2-08)钻进了 block 内部,讲清了"4KB 的 data block 怎么紧凑存 KV、怎么二分查找"。但读一条 key 时,把目标 data block **整个读进内存、解压、二分**,本身就是一次磁盘 I/O——如果这个 block 里**根本没有这条 key**,这次 I/O 就白做了。LSM 的读放大(P0-01 立的三笔账之一)很大一块就出在这:一个 key 的最新版本可能散在多层多个 SSTable,逐个打开 data block 去找,大部分都是"没有"。这一章讲 LevelDB 怎么在读 data block **之前**先用一道极便宜的关卡挡掉这些无效查询——**布隆过滤器(Bloom Filter)**。它用极小空间(每 key 10 bit)把"这个 block 大概有没有这个 key"的判断做到 O(1),削掉读放大最痛的一刀。

## 核心问题

**读一条 key,在把 data block 读进内存之前,先问一句"这个 block 八成有没有它"——这道关卡用什么做?LevelDB 的答案是布隆过滤器:k 个 hash 函数把 key 映射到 m 位的 bit 数组里的 k 个位置,全为 1 才"可能有"(允许误判)、有任一 0 就"肯定没有"(绝不漏判)。它用每 key 10 bit 的空间换 ~1% 的误判率,把绝大多数无效查询挡在读 data block 之前。**

读完本章你会明白:

1. 布隆过滤器的数学原理:为什么"全 1 才可能有、任一 0 就肯定没有"是单向的(只有 false positive,没有 false negative);误判率 `p ≈ (1 - e^(-kn/m))^k`,最优 `k = (m/n) * ln2`。
2. LevelDB 的 `BloomFilterPolicy` 怎么用 **DoubleHashing**——k 个 hash 函数实际只算 **1 个** `BloomHash`,再从中拆出 `delta = (h>>17)|(h<<15)` 当"第二个伪 hash",用 `g_i = h + i*delta` 衍生出 k 个位置。这是 Kirsch-Mitzenmacher 技巧在工程上的极致简化。
3. 默认 `bits_per_key = 10` → `k = 6`(误判率约 1%)的数学来历,以及 bits/key、k、误判率三者的权衡表。
4. filter block 的布局:每 **2KB** 文件偏移生成一个独立 filter,末尾用 `offset array + lg_lg + lg` 基数定位。为什么是 2KB 而不是 4KB(和 data block 对齐)?
5. `FilterBlockBuilder`(写)和 `FilterBlockReader`(读)的源码逐行拆;`Table::InternalGet` 怎么在读 data block 前先调 `filter->KeyMayMatch` 剪枝。

> **如果一读觉得太难**:先只记住三件事——① 布隆过滤器是一个**允许误判、绝不漏判**的概率结构,用每 key 10 bit 换 ~1% 的"假阳性",把"肯定没有"的查询挡掉;② LevelDB 实际**只算 1 个 hash**,通过位运算衍生出 k 个位置(DoubleHashing),省了 k-1 次 hash 计算;③ filter block **每 2KB 文件偏移**生成一个独立 filter,读哪个 data block 就只查它对应的那一个 filter。剩下的数学和字节级细节可以回头再读。

---

## 9.1 一句话点破

> **布隆过滤器是一道"肯定没有就别读"的关卡——用每 key 10 bit 的极小空间,挡掉绝大多数无效的 data block 读。它允许误判(说"可能有"其实可能没有,~1%),但绝不漏判(说"没有"就真没有)。LevelDB 用 DoubleHashing 把 k 个 hash 函数压成 1 个 hash + 一次位运算衍生,把这道关卡做到极致便宜。**

这是结论,不是理由。本章倒过来拆:先看读放大为什么痛,再看布隆的数学怎么把"判断在不在这件事"压到 O(1) 空间,然后看 LevelDB 怎么用 1 个 hash 骗过"需要 k 个独立 hash"的要求,最后钉死 filter block 的字节布局。

---

## 9.2 读放大最痛的一刀:无效的 data block 读

### 提出问题

P0-01 立过"三笔放大",读放大是其中最直接影响用户感知的那一笔。展开一下它具体痛在哪:

一次 `Get(k)`,理想情况下应该"一下子找到"。但 LSM 的结构决定了 k 的最新版本**可能散落在**:

- MemTable(还没刷)
- Immutable MemTable(正刷未完)
- level-0 的若干 SSTable(文件间 key range 可能重叠,每个都得查)
- level-1 ~ level-6 的 SSTable(每层 key range 不重叠,但同层可能多个文件、跨层都要查)

对一个 100GB 的库,L0 可能有 4~8 个 SSTable,L1 到 L6 加起来可能有几百个 SSTable。一次 `Get` 最坏要在每个相关 SSTable 里查一遍。**而"在某个 SSTable 里查一遍",具体动作是:在 index block 里 Seek → 定位到某个 data block 的 BlockHandle → 把这个 data block 整个读进内存 → 在 block 里二分。**

关键痛点来了:**这次 data block 读,大多数情况下是白读的**。因为 k 这一次只在一个地方(它的最新版本所在的那个 SSTable 的那个 data block),其它所有"被 index 指出来可能含 k 的 data block",读进来都是空的。一次磁盘 I/O(或者一次 block cache miss 后的磁盘 I/O)就这么浪费了。

读放大的实体代价,绝大部分就是这些"白读的 data block"。

### 不这样会怎样

> **反面对比(没有布隆,朴素地读)**:假设 LevelDB 没有任何提前剪枝机制,一次 `Get` 就老老实实按"index → data block → 二分"的路径查每个相关 SSTable。对 100GB 库、k 散在 L3 某个文件,最坏要读 L0 的几个 + L1~L6 每层若干个 data block,加起来十几次磁盘 I/O,每次 ~1ms(机械盘),用户感知到几十毫秒的延迟。这在 LSM 是不可接受的——B-tree 同样场景只要 3~4 次 I/O(树高)。**没有布隆的 LSM,读放大在随机读场景会被 B-tree 完爆。**

所以必须有一道"在读 data block 之前先剪掉肯定没有的"关卡。理想要求:

1. **极快**:这道关卡本身不能比读 data block 还慢,否则没意义。最好 O(1),几微秒。
2. **极小**:它的空间开销要远小于 data block 本身。否则不如直接读 data block。最好每 key 几 bit。
3. **单向**:可以误判("说有其实没有",只是多读一次 data block,代价有限),但不能漏判("说没有其实有"——这会查不到数据,正确性事故)。**允许 false positive,绝不允许 false negative。**

这三条要求,正好指向一个 1970 年的老结构——布隆过滤器。

---

## 9.3 布隆过滤器的数学:bit 数组 + k 个 hash

### 所以这样设计

布隆过滤器的核心想法直球到不能再直球:

1. 准备一个 **m 位的 bit 数组**(初始全 0)。
2. 准备 **k 个独立的 hash 函数** `h_1, h_2, ..., h_k`,每个都把 key 映射到 `[0, m)` 范围内。
3. **插入一个 key** `x`:对每个 `i`,算 `h_i(x)`,把 bit 数组的第 `h_i(x)` 位置 1。一共置 k 个位(可能重复,所以是"至多 k 个位")。
4. **查询一个 key** `x`:对每个 `i`,算 `h_i(x)`,看 bit 数组的第 `h_i(x)` 位置。
   - 如果 **k 个位置全为 1**:**说"可能有"**(可能误判——这些位可能是别的 key 置上的)。
   - 如果 **有任一为 0**:**说"肯定没有"**(因为这个 key 当初插入时一定把所有 k 个位置都置 1 了,现在有 0 说明它没插过——绝不漏判)。

ASCII 框图,m = 16,k = 3,插入两个 key:

```
   bit 数组(m = 16 位,初始全 0):

   插入 key "alice":h_1=3, h_2=7, h_3=12  →  置位 3, 7, 12

     下标: 0 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15
     位:   0 0 0 1 0 0 0 1 0 0  0  0  1  0  0  0
                 ↑           ↑              ↑
                h_1         h_2            h_3

   再插入 key "bob":h_1=5, h_2=12, h_3=15  →  置位 5, 12, 15(12 已置,不变)

     下标: 0 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15
     位:   0 0 0 1 0 1 0 1 0 0  0  0  1  0  0  1
                 ↑   ↑       ↑              ↑  ↑
               alice bob    alice          alice&bob  bob

   查询 "alice":h_1=3,h_2=7,h_3=12,位 3,7,12 全为 1 → "可能有"(✓ 真的在)
   查询 "dave":h_1=2,h_2=9,h_3=14,位 2 = 0      → "肯定没有"(✓ 真的不在)
   查询 "eve": h_1=5,h_2=7,h_3=15,位 5,7,15 全 1 → "可能有"(✗ 误判!eve 不在)
                                                          ↑ 这就是 false positive
```

关键性质:

- **绝不漏判(false negative = 0)**:如果 x 真在集合里,它的 k 个位当初一定都置过 1(且 bit 数组只能从 0 变 1,不会回 0),所以查询时这 k 个位一定还是 1,一定"说可能有"。
- **可能误判(false positive > 0)**:如果 x 不在集合里,它的 k 个 hash 位置可能恰好都被别的 key 置过 1,于是查询时"误以为可能有"。这个概率就是误判率 `p`。

### 9.3.1 误判率公式:bits/key、k 怎么权衡

布隆过滤器的全部数学就一张表。设:

- `n` = 要插入的 key 数量
- `m` = bit 数组长度(bits)
- `k` = hash 函数个数
- `m/n` = 每个 key 摊到的 bit 数(bits/key)

推导(只给直觉,不堆数学):一次 hash 把某一位置 1 的概率是 `1/m`,**没**置 1 的概率是 `1 - 1/m`。插入 n 个 key、每个 key k 次 hash,某一**没**被置 1 的概率约 `(1 - 1/m)^(kn) ≈ e^(-kn/m)`。所以某一**位是 1**的概率是 `1 - e^(-kn/m)`。查询一个不在集合的 key,它的 k 个 hash 位**全是 1**的概率就是:

```
   p = (1 - e^(-kn/m))^k
```

这就是误判率公式。两个有用的推论:

1. **给定 `m/n`,最优 `k = (m/n) * ln2`**。直觉:hash 太少(k 小),每个 key 没占够位,留下大量 0 位,查询时不容易全 1(误判低)但每个 key 信息量不足;hash 太多(k 大),bit 数组被填得太满,大部分位都是 1,查询时容易"全 1"误判。中间有个最优点,数学上正好是 `(m/n) * ln2 ≈ (m/n) * 0.693`。
2. **给定 `m/n` 和最优 `k`,误判率约 `p ≈ (0.6185)^(m/n)`**。这是个非常简洁的近似——每多 1 bit/key,误判率乘 0.6185(约 0.6 倍)。

bits/key vs 误判率的真值表(用最优 k):

| bits/key (m/n) | 最优 k (`(m/n)*0.693` 取整) | 误判率 p (近似) |
|---------------|----------------------------|----------------|
| 6             | 4                          | ~8.6%          |
| 7             | 5                          | ~5.3%          |
| 8             | 6                          | ~3.3%          |
| 9             | 6                          | ~2.0%          |
| **10**(LevelDB 默认) | **6**                  | **~1.1%**      |
| 11            | 8                          | ~0.68%         |
| 12            | 8                          | ~0.42%         |
| 14            | 10                         | ~0.16%         |
| 16            | 11                         | ~0.06%         |

> **钉死这件事**:bits/key 每加 1,误判率约乘 0.6185。这是"空间换误判率"的边际收益曲线:**前几 bit 收益最大**(6→10 bit 把误判率从 8.6% 压到 1%),**后面越加越平**(14→16 bit 只把 0.16% 压到 0.06%)。LevelDB 默认 10 bit/key,正好卡在"误判率 ~1%"这个甜点——再加 bit 收益递减、空间浪费。这个数字是 LevelDB 作者经验拍板,RocksDB 也沿用。

---

## 9.4 LevelDB 的实现:`BloomFilterPolicy`

讲完数学,看 LevelDB 源码怎么实现。整个布隆过滤器的实现只在 **92 行的 [`util/bloom.cc`](../leveldb/util/bloom.cc)** 里,小而精。

### 9.4.1 `FilterPolicy` 抽象基类:策略模式

布隆只是 LevelDB "过滤器策略"的一种。LevelDB 把"过滤"抽象成一个基类 [`FilterPolicy`](../leveldb/include/leveldb/filter_policy.h#L27-L52):

```cpp
class LEVELDB_EXPORT FilterPolicy {
 public:
  virtual ~FilterPolicy();
  virtual const char* Name() const = 0;                                   // filter_policy.h:35

  // 把 keys[0..n-1] 这 n 个 key 的 filter 追加到 *dst
  virtual void CreateFilter(const Slice* keys, int n, std::string* dst) const = 0;   // :43

  // 查 key 是否可能在 filter 里(允许误判,绝不漏判)
  virtual bool KeyMayMatch(const Slice& key, const Slice& filter) const = 0;          // :51
};
```

两个纯虚函数:

- **`CreateFilter`**:给一批 key,生成一段 filter 数据(追加到 `*dst`)。这是"建索引"。
- **`KeyMayMatch`**:给一个 key 和一段 filter 数据,返回"可能/不可能"。这是"查索引"。

为什么搞成抽象基类?这是经典的**策略模式**:LevelDB 允许用户传自己的 `FilterPolicy`(比如想做 ribbon filter、cuckoo filter、或者基于 prefix 的自定义过滤),只要实现这两个方法。`options.filter_policy = NewBloomFilterPolicy(10)` 是默认选择,但用户可以替换。`Name()` 方法是**前向兼容**的钥匙——不同的 filter 编码格式用不同的 Name,读老文件时按 Name 找对应的 filter(metaindex block 里 key 就是 `"filter." + Name`,见 P2-07)。

`NewBloomFilterPolicy(int bits_per_key)` 是工厂函数,返回一个 `BloomFilterPolicy*`([`util/bloom.cc:88-90`](../leveldb/util/bloom.cc#L88-L90)):

```cpp
const FilterPolicy* NewBloomFilterPolicy(int bits_per_key) {
  return new BloomFilterPolicy(bits_per_key);
}
```

### 9.4.2 构造:从 bits_per_key 算 k

看 [`util/bloom.cc:17-24`](../leveldb/util/bloom.cc#L17-L24):

```cpp
class BloomFilterPolicy : public FilterPolicy {
 public:
  explicit BloomFilterPolicy(int bits_per_key) : bits_per_key_(bits_per_key) {
    // We intentionally round down to reduce probing cost a little bit
    k_ = static_cast<size_t>(bits_per_key * 0.69);  // 0.69 =~ ln(2)   // bloom.cc:21
    if (k_ < 1) k_ = 1;                                                // :22
    if (k_ > 30) k_ = 30;                                              // :23
  }
```

`k_ = bits_per_key * 0.69`,注释明说 `0.69 =~ ln(2)`——这正是 9.3.1 节推导的最优 `k = (m/n) * ln2`。

几个细节值得注意:

1. **0.69 是向下取整**(`static_cast<size_t>` 是截断)。注释解释 `"We intentionally round down to reduce probing cost a little bit"`——略微偏向**少 hash 几次**,因为 k 越大每次查询/插入都要做 k 次 hash,k 是性能开销。少做一次 hash 换来的延迟降低,比略微升高的误判率更值。
2. **k 限制在 [1, 30]**:`k < 1` 提到 1(极端情况至少 hash 一次);`k > 30` 砍到 30。**30 这个上限和 `KeyMayMatch` 里的 `if (k > 30) return true` 配套**——因为 filter 末尾 1 字节存 k(见 9.4.3),这字节最大值 255,但 LevelDB 只允许 k ≤ 30,超出就当"这是别的编码格式",保守地返回 true(可能命中,不剪枝)。这是前向兼容的兜底。
3. 默认 `bits_per_key = 10` → `k_ = int(10 * 0.69) = 6`。对应误判率约 1.1%(查 9.3.1 表)。

### 9.4.3 `CreateFilter`:把 n 个 key 写进 bit 数组

这是写入侧的核心。看 [`util/bloom.cc:28-54`](../leveldb/util/bloom.cc#L28-L54):

```cpp
  void CreateFilter(const Slice* keys, int n, std::string* dst) const override {
    // Compute bloom filter size (in both bits and bytes)
    size_t bits = n * bits_per_key_;                                      // :30

    // For small n, we can see a very high false positive rate.  Fix it
    // by enforcing a minimum bloom filter length.
    if (bits < 64) bits = 64;                                             // :34 —— 强制最小 64 位

    size_t bytes = (bits + 7) / 8;                                        // :36 —— 向上取整到字节
    bits = bytes * 8;                                                     // :37 —— bits 重新对齐到 8 的倍数

    const size_t init_size = dst->size();
    dst->resize(init_size + bytes, 0);                                    // :40 —— 在 dst 后扩 bytes 字节,全 0
    dst->push_back(static_cast<char>(k_));  // Remember # of probes in filter  // :41 —— 最后 1 字节存 k
    char* array = &(*dst)[init_size];                                     // :42 —— 拿到这段 filter 的起始指针
    for (int i = 0; i < n; i++) {
      // Use double-hashing to generate a sequence of hash values.
      // See analysis in [Kirsch,Mitzenmacher 2006].
      uint32_t h = BloomHash(keys[i]);                                    // :46 —— 算 1 个 hash
      const uint32_t delta = (h >> 17) | (h << 15);  // Rotate right 17 bits  // :47 —— 衍生 delta
      for (size_t j = 0; j < k_; j++) {
        const uint32_t bitpos = h % bits;                                 // :49 —— 第 j 个 hash 位置
        array[bitpos / 8] |= (1 << (bitpos % 8));                         // :50 —— 置位
        h += delta;                                                       // :51 —— h ← h + delta,准备下一次
      }
    }
  }
```

逐步拆:

1. **算 filter 大小**(:30-37):`bits = n * bits_per_key_`,但至少 64 位(防 n 太小时误判率爆炸——比如 n=1、bits=10 时,k=6 个 hash 全砸在 10 个位里,bit 数组几乎被填满,误判率近 100%)。然后向上取整到 8 的倍数(因为按字节分配)。
2. **扩 dst,清 0,末尾存 k**(:39-41):`dst->resize(init_size + bytes, 0)` 把 filter 区域全 0 初始化。**最后 1 字节存 k**(本 filter 用的 hash 次数)——这是为了让 reader 能从 filter 数据本身读到 k,而不需要额外参数。这意味着**同一个库可以用不同 k 的 filter**(比如改了 `bits_per_key` 后新老 SSTable 共存),reader 自动适配。
3. **对每个 key,k 次置位**(:43-53):
   - 算 `h = BloomHash(key)`(:46)——**唯一一次真 hash**。
   - 算 `delta = (h >> 17) | (h << 15)`(:47)——从 h 里**衍生**出第二个"伪 hash"。
   - 循环 k 次:用 `h % bits` 算位置(:49)、置位(:50)、`h += delta`(:51)——**下次循环用的 h 是这次加上 delta**。

这就是 DoubleHashing 的全部魔法:**k 次 hash 位置,实际只算了 1 次 `BloomHash`**。下一节技巧精解专门拆透它。

### 9.4.4 `KeyMayMatch`:查 key 在不在

查询侧是 `CreateFilter` 的镜像,看 [`util/bloom.cc:56-80`](../leveldb/util/bloom.cc#L56-L80):

```cpp
  bool KeyMayMatch(const Slice& key, const Slice& bloom_filter) const override {
    const size_t len = bloom_filter.size();
    if (len < 2) return false;                                           // :58 —— 空 filter 直接 false

    const char* array = bloom_filter.data();
    const size_t bits = (len - 1) * 8;                                   // :61 —— bits 数 = (filter 总长 - 1) * 8

    // Use the encoded k so that we can read filters generated by
    // bloom filters created using different parameters.
    const size_t k = array[len - 1];                                     // :65 —— 从末尾 1 字节读 k
    if (k > 30) {                                                        // :66
      // Reserved for potentially new encodings for short bloom filters.
      // Consider it a match.
      return true;                                                       // :69 —— k > 30 当作"别的编码",保守返回 true
    }

    uint32_t h = BloomHash(key);                                         // :72
    const uint32_t delta = (h >> 17) | (h << 15);                        // :73
    for (size_t j = 0; j < k; j++) {
      const uint32_t bitpos = h % bits;                                  // :75
      if ((array[bitpos / 8] & (1 << (bitpos % 8))) == 0) return false;  // :76 —— 任一位为 0,直接 false
      h += delta;                                                        // :77
    }
    return true;                                                         // :79 —— 全为 1,返回 true(可能误判)
  }
```

和 `CreateFilter` 完美对称:

1. **空 filter 返回 false**(:58):filter 长度 < 2(没数据或只有 k 那 1 字节),里面不可能有任何 key,直接 false。
2. **从末尾读 k**(:65):和 `CreateFilter` 末尾存 k 对应。注释说 `"Use the encoded k so that we can read filters generated by bloom filters created using different parameters"`——reader 不依赖任何外部参数,filter 数据自带 k。
3. **k > 30 兜底**(:66-69):如果 filter 末尾字节 > 30,说明这不是当前编码格式(可能是未来的新格式),保守返回 true(当"可能命中",不剪枝,正常读 data block)。这是前向兼容。
4. **k 次检查**(:74-78):同样的 DoubleHashing 衍生 k 个位置,**只要有一个位是 0,立即返回 false**——这就是"绝不漏判"的体现(只要任一位是 0,这个 key 当初肯定没插过)。**全为 1 才返回 true**(可能误判)。

注意一个 micro-optimization:`CreateFilter` 是 k 次都置位(无条件),而 `KeyMayMatch` 是**短路**的——任一位是 0 立即返回。对"key 不在"的常见 case(布隆的最大用武之地),平均只要查 1~2 次就能确定 false,比无脑查完 k 次快得多。

### 9.4.5 `BloomHash`:那个唯一的 hash

那个被反复用的 `BloomHash` 是什么?看 [`util/bloom.cc:13-15`](../leveldb/util/bloom.cc#L13-L15):

```cpp
static uint32_t BloomHash(const Slice& key) {
  return Hash(key.data(), key.size(), 0xbc9f1d34);
}
```

调的是 `util/hash.cc` 的 `Hash`,种子固定 `0xbc9f1d34`。`Hash` 函数本身是类 Murmur hash,看 [`util/hash.cc:22-53`](../leveldb/util/hash.cc#L22-L53):

```cpp
uint32_t Hash(const char* data, size_t n, uint32_t seed) {
  // Similar to murmur hash
  const uint32_t m = 0xc6a4a793;
  const uint32_t r = 24;
  const char* limit = data + n;
  uint32_t h = seed ^ (n * m);                       // hash.cc:27

  while (limit - data >= 4) {                        // :30 —— 一次吞 4 字节
    uint32_t w = DecodeFixed32(data);
    data += 4;
    h += w;
    h *= m;
    h ^= (h >> 16);
  }

  switch (limit - data) {                            // :39 —— 处理剩余 1~3 字节
    case 3: h += static_cast<uint8_t>(data[2]) << 16; FALLTHROUGH_INTENDED;
    case 2: h += static_cast<uint8_t>(data[1]) << 8;  FALLTHROUGH_INTENDED;
    case 1: h += static_cast<uint8_t>(data[0]);
            h *= m; h ^= (h >> r); break;
  }
  return h;
}
```

这是经典的**乘加 + 旋转 hash**(Murmur 变种),4 字节一步,质量足够好(分布均匀,雪崩效应够),速度比 MD5/SHA 快两个数量级。对布隆来说够用——布隆不需要密码学级别的 hash,只需要分布均匀。

> **钉死这件事**:整个 `BloomHash` + `Hash` 是个 O(n)(n 是 key 长度)的快速 hash,每次 ~几十纳秒。布隆的 k 次"hash"实际只调它**一次**,剩下 k-1 次都是 `h += delta` 这种几纳秒的整数加法。这是布隆查询能做到亚微秒级的关键。

---

## 9.5 filter block 的布局:每 2KB 一个 filter

讲完了单个 filter 怎么生成和查询,现在看整个 filter block 怎么组织。布隆的理论是"一个集合一个 filter",但一个 SSTable 里有几十个 data block,是不是整张文件的所有 key 共用一个 filter?

不是。原因:如果整张文件共用一个 filter,查询一个 key 时要查的是"整个文件的所有 key"组成的集合——filter 会很大(几 MB),而且任何一次查询都得把整个大 filter 读进内存(或者至少随机访问它的多个位,跨多页)。这就违背了"极小空间、极快查询"的初衷。

LevelDB 的做法是**分片**:每 **2KB 文件偏移**生成一个独立的小 filter,查询时只查目标 data block 对应的那一个 filter。

### 9.5.1 分片粒度:为什么是 2KB

看 [`table/filter_block.cc:14-16`](../leveldb/table/filter_block.cc#L14-L16):

```cpp
// Generate new filter every 2KB of data
static const size_t kFilterBaseLg = 11;
static const size_t kFilterBase = 1 << kFilterBaseLg;   // = 2048 字节
```

`kFilterBase = 2048` 字节 = 2KB。**每 2KB 文件偏移,独立一个 filter**。注意是"文件偏移",不是"data block 序号"——一个 4KB 的 data block 跨了 2 个 2KB 区间,就会涉及 2 个 filter(每个 filter 覆盖这个 data block 在该 2KB 区间内的那些 key)。

**为什么是 2KB,不是和 data block 一样的 4KB?**

- 2KB 比 4KB 更细:一个 data block 的 key 分到 2 个 filter 里,每个 filter 更小(查询时读的 filter 数据更少)。
- 2KB 是"两个 data block 的偏移单位":假设 data block 默认 4KB,2KB 的分片让 filter 和 data block **不对齐**,但这恰好让一个 filter 平均覆盖半个 data block——查询一个 key 时,只要查"这个 key 所在 data block 在文件偏移 X,X 落在第 X/2KB 个 filter 里"那一个 filter 就行(细节见 9.5.3 的 `KeyMayMatch`)。
- 历史原因:LevelDB 作者选择 2KB,RocksDB 沿用。这个值是"filter 粒度"和"filter 数量(决定 offset array 大小)"的平衡。

### 9.5.2 filter block 的字节布局

整个 filter block 从头到尾长这样(ASCII 框图):

```
   filter block(连续字节,在 SSTable 里紧跟着所有 data block 之后):

   ┌────────────────────────┬────────────────────────┬──────┬────────────────────────┬──────────────────┬─────────┬───────────┐
   │  filter 0              │  filter 1              │ ...  │  filter N-1             │ offset array     │ array_  │ base_lg_  │
   │  (覆盖文件偏移 [0, 2KB)) │  (覆盖 [2KB, 4KB))      │      │  (覆盖 [2K(N-1), 2KN))   │ (uint32 × N+1)   │ offset  │ (1 byte) │
   │  内含 k_ 末字节         │  内含 k_ 末字节         │      │  内含 k_ 末字节          │                  │ (uint32)│  = 11     │
   └────────────────────────┴────────────────────────┴──────┴────────────────────────┴──────────────────┴─────────┴───────────┘
    ↑                                                                                                          ↑           ↑
    result_.data() 起始                                                                                          last_word   最后 1 字节
                                                                                                              (offset array 起点)   (倒数第 5 字节起)
```

几个关键点:

1. **每个 filter**(`filter 0`、`filter 1`、...)是一段独立的 bit 数组,末尾 1 字节存 k(见 9.4.3)。它由 `CreateFilter` 生成。
2. **offset array**:每个 filter 在 filter block 内的起始 offset,用 `uint32` 小端存。共 `N+1` 个 entry(N 个 filter 起始 + 1 个"末尾",用于算最后一个 filter 的长度)。
3. **array_offset**(`uint32`,小端):offset array 自己在 filter block 里的起始 offset。reader 读它就知道 offset array 从哪开始。
4. **base_lg_**(1 字节):分片粒度的 log2,固定 11(`= log2(2048)`)。reader 读它就知道分片粒度。

整个 filter block 的**末尾 5 字节**是固定的:`array_offset(4) + base_lg_(1)`。reader 从末尾倒着读这 5 字节,就能定位整个 filter block 的结构。

### 9.5.3 写:`FilterBlockBuilder` 怎么攒

`FilterBlockBuilder` 是个状态机,接口是 `(StartBlock AddKey*)* Finish`。看 [`table/filter_block.cc:18-49`](../leveldb/table/filter_block.cc#L18-L49):

```cpp
FilterBlockBuilder::FilterBlockBuilder(const FilterPolicy* policy)
    : policy_(policy) {}

void FilterBlockBuilder::StartBlock(uint64_t block_offset) {           // :21
  uint64_t filter_index = (block_offset / kFilterBase);                 // :22 —— 这个 offset 落在第几个 2KB 区间
  assert(filter_index >= filter_offsets_.size());
  while (filter_index > filter_offsets_.size()) {                       // :24 —— 如果跨入了新区间,把之前的 key 攒成 filter
    GenerateFilter();
  }
}

void FilterBlockBuilder::AddKey(const Slice& key) {                     // :29
  Slice k = key;
  start_.push_back(keys_.size());                                       // :31 —— 记 key 起点
  keys_.append(k.data(), k.size());                                     // :32 —— 拼到扁平 buffer
}

Slice FilterBlockBuilder::Finish() {                                    // :35
  if (!start_.empty()) {
    GenerateFilter();                                                   // :37 —— 把最后一批没生成的 key 收尾
  }

  // Append array of per-filter offsets
  const uint32_t array_offset = result_.size();                         // :41 —— offset array 起点的 offset
  for (size_t i = 0; i < filter_offsets_.size(); i++) {
    PutFixed32(&result_, filter_offsets_[i]);                           // :43 —— 每个 filter 起点 offset(uint32 小端)
  }

  PutFixed32(&result_, array_offset);                                   // :46 —— offset array 自己的 offset
  result_.push_back(kFilterBaseLg);  // Save encoding parameter in result  // :47 —— 分片粒度
  return Slice(result_);
}
```

逻辑:

- **`StartBlock(block_offset)`**:`TableBuilder::Flush` 在写完一个 data block 后会调它([`table/table_builder.cc:137`](../leveldb/table/table_builder.cc#L137)),传当前文件偏移 `r->offset`。它根据 `block_offset / 2048` 算出当前落在第几个 2KB 区间,如果跨入了新区间,就把之前攒的 key 调 `GenerateFilter` 生成一个 filter。
- **`AddKey(key)`**:`TableBuilder::Add` 每加一条 KV 时调([`table/table_builder.cc:112`](../leveldb/table/table_builder.cc#L112)),把 key 拼到扁平 buffer `keys_`,`start_` 记每个 key 的起点。
- **`Finish()`**:`TableBuilder::Finish` 写 filter block 时调(`table_builder.cc:223`),先把最后一批 key 生成 filter,然后追加 offset array + array_offset + base_lg_。

`GenerateFilter` 是把当前攒的一批 key 喂给 `policy_->CreateFilter` 的桥,看 [`table/filter_block.cc:51-75`](../leveldb/table/filter_block.cc#L51-L75):

```cpp
void FilterBlockBuilder::GenerateFilter() {
  const size_t num_keys = start_.size();
  if (num_keys == 0) {
    // Fast path if there are no keys for this filter
    filter_offsets_.push_back(result_.size());                          // :55 —— 空 filter 也要记 offset
    return;
  }

  // Make list of keys from flattened key structure
  start_.push_back(keys_.size());  // Simplify length computation        // :60 —— 哨兵,简化长度计算
  tmp_keys_.resize(num_keys);
  for (size_t i = 0; i < num_keys; i++) {
    const char* base = keys_.data() + start_[i];
    size_t length = start_[i + 1] - start_[i];                          // :64 —— 用下一个 key 起点算当前 key 长度
    tmp_keys_[i] = Slice(base, length);
  }

  // Generate filter for current set of keys and append to result_.
  filter_offsets_.push_back(result_.size());                            // :69 —— 记这个 filter 的起点 offset
  policy_->CreateFilter(&tmp_keys_[0], static_cast<int>(num_keys), &result_);  // :70 —— 真正生成 filter

  tmp_keys_.clear();                                                    // :72 —— 清空,准备下一批
  keys_.clear();
  start_.clear();
}
```

注意 :60 的 `start_.push_back(keys_.size())`——加一个"哨兵"在最后,让 :64 的 `start_[i+1] - start_[i]` 对最后一个 key 也能算出长度,不用特判。这是个干净的小技巧。

### 9.5.4 读:`FilterBlockReader` 怎么查

reader 侧的构造在 [`table/filter_block.cc:77-88`](../leveldb/table/filter_block.cc#L77-L88):

```cpp
FilterBlockReader::FilterBlockReader(const FilterPolicy* policy,
                                     const Slice& contents)
    : policy_(policy), data_(nullptr), offset_(nullptr), num_(0), base_lg_(0) {
  size_t n = contents.size();
  if (n < 5) return;  // 1 byte for base_lg_ and 4 for start of offset array  // :81
  base_lg_ = contents[n - 1];                                           // :82 —— 末尾 1 字节是 base_lg_
  uint32_t last_word = DecodeFixed32(contents.data() + n - 5);          // :83 —— 倒数第 5 字节起的 4 字节是 array_offset
  if (last_word > n - 5) return;                                        // :84 —— 自校验:offset array 起点不能越界
  data_ = contents.data();                                              // :85
  offset_ = data_ + last_word;                                          // :86 —— offset_ 指向 offset array 起点
  num_ = (n - 5 - last_word) / 4;                                       // :87 —— offset array 里有多少个 uint32 entry
}
```

从末尾倒着解析:`base_lg_` 1 字节、`array_offset` 4 字节,然后 offset array 在 `data_ + array_offset` 开始,有 `(n - 5 - array_offset) / 4` 个 entry。

查询的核心,看 [`table/filter_block.cc:90-104`](../leveldb/table/filter_block.cc#L90-L104):

```cpp
bool FilterBlockReader::KeyMayMatch(uint64_t block_offset, const Slice& key) {
  uint64_t index = block_offset >> base_lg_;                            // :91 —— 用文件偏移算 filter 下标(block_offset / 2048)
  if (index < num_) {
    uint32_t start = DecodeFixed32(offset_ + index * 4);                // :93 —— 第 index 个 filter 起点
    uint32_t limit = DecodeFixed32(offset_ + index * 4 + 4);            // :94 —— 第 index+1 个 filter 起点(算长度用)
    if (start <= limit && limit <= static_cast<size_t>(offset_ - data_)) {
      Slice filter = Slice(data_ + start, limit - start);               // :96 —— 切出这个 filter 的字节
      return policy_->KeyMayMatch(key, filter);                         // :97 —— 调 BloomFilterPolicy::KeyMayMatch
    } else if (start == limit) {
      // Empty filters do not match any keys
      return false;                                                     // :100 —— 空 filter 不匹配任何 key
    }
  }
  return true;  // Errors are treated as potential matches               // :103 —— 任何异常都保守返回 true
}
```

注意几个细节:

1. **入口参数是 `block_offset`,不是 filter index**(:90):caller 传的是 data block 的文件偏移(`Table::InternalGet` 传 `handle.offset()`,见 9.6 节)。reader 自己用 `block_offset >> base_lg_`(等价于 `/ 2048`)算出 filter 下标。位运算代替除法,更快。
2. **用两个相邻 offset 算 filter 长度**(:93-94):`filter[index]` 的起点是 `offset_[index]`,终点是 `offset_[index+1]`,长度 `limit - start`。这就是为什么 offset array 要有 `N+1` 个 entry(最后一个用于算第 N 个 filter 的长度)。
3. **任何异常保守返回 true**(:103):越界、损坏、空 filter(如果非 start==limit 的其它 case)——一律当"可能命中",不剪枝,正常读 data block。这是**正确性优先于性能**:布隆是优化,不是必需,坏了不能影响正确性,只能退化到"不剪枝"。

### 9.5.5 怎么读进内存:Table::ReadFilter

filter block 在 `Table::Open` 时并不立即读,而是**懒加载**——只有 `options.filter_policy != nullptr` 时才读。看 [`table/table.cc:82-132`](../leveldb/table/table.cc#L82-L132) 的 `ReadMeta` 和 `ReadFilter`:

```cpp
void Table::ReadMeta(const Footer& footer) {
  if (rep_->options.filter_policy == nullptr) {
    return;  // Do not need any metadata                                       // :84
  }
  ...
  // 按 footer.metaindex_handle() 读 metaindex block
  Block* meta = new Block(contents);
  Iterator* iter = meta->NewIterator(BytewiseComparator());
  std::string key = "filter.";
  key.append(rep_->options.filter_policy->Name());                             // :102 —— "filter.leveldb.BuiltinBloomFilter2"
  iter->Seek(key);                                                             // :103 —— 在 metaindex 里找
  if (iter->Valid() && iter->key() == Slice(key)) {
    ReadFilter(iter->value());                                                 // :105 —— 找到了,按 value(一个 BlockHandle)读 filter block
  }
  ...
}

void Table::ReadFilter(const Slice& filter_handle_value) {
  Slice v = filter_handle_value;
  BlockHandle filter_handle;
  if (!filter_handle.DecodeFrom(&v).ok()) return;
  ...
  BlockContents block;
  if (!ReadBlock(rep_->file, opt, filter_handle, &block).ok()) return;         // :125 —— 按 BlockHandle 读 filter block
  if (block.heap_allocated) {
    rep_->filter_data = block.data.data();  // Will need to delete later       // :129 —— 记下原始数据指针(析构时要 delete)
  }
  rep_->filter = new FilterBlockReader(rep_->options.filter_policy, block.data);  // :131 —— 构造 reader
}
```

打开 SSTable 时,如果配了 filter_policy:读 metaindex → 找到 `"filter." + Name` 这条 entry → 拿到 filter block 的 BlockHandle → 读 filter block → 构造 `FilterBlockReader`,挂在 `rep_->filter` 上。**整个 filter block 一次性读进内存**(它通常很小,几 KB ~ 几十 KB),常驻 Table 对象的生命周期。

> **钉死这件事**:filter block 的读策略和 data block **不同**:data block 是按需、走 block cache、用完 release(见下一章 P2-10);filter block 是**打开文件时一次性全读、常驻内存**。为什么?因为 filter block 小(整个文件的 key 才几 KB filter),常驻代价低;而 data block 大、数量多,必须靠缓存按需。这是"小而常用全读、大而稀疏按需"的典型取舍。

---

## 9.6 怎么用上:`Table::InternalGet` 的剪枝

讲完了 filter block 怎么写、怎么读,最后看它在真实读路径里怎么用。这是 P3-13 读路径全流程的预告,但这里先点破关键一步。

看 [`table/table.cc:214-242`](../leveldb/table/table.cc#L214-L242) 的 `Table::InternalGet`(一次 `Get` 在某个 SSTable 内部的查找):

```cpp
Status Table::InternalGet(const ReadOptions& options, const Slice& k, void* arg,
                          void (*handle_result)(void*, const Slice&, const Slice&)) {
  Status s;
  Iterator* iiter = rep_->index_block->NewIterator(rep_->options.comparator);  // :218 —— 在 index block 上建迭代器
  iiter->Seek(k);                                                              // :219 —— 在 index 里二分定位 k
  if (iiter->Valid()) {
    Slice handle_value = iiter->value();                                       // :221 —— 拿到目标 data block 的 BlockHandle 编码
    FilterBlockReader* filter = rep_->filter;
    BlockHandle handle;
    if (filter != nullptr && handle.DecodeFrom(&handle_value).ok() &&
        !filter->KeyMayMatch(handle.offset(), k)) {                            // :225 —— ★关键:布隆剪枝★
      // Not found                                                              // :226 —— 布隆说"没有",直接跳过,不读 data block
    } else {
      Iterator* block_iter = BlockReader(this, options, iiter->value());       // :228 —— 布隆说"可能有"(或没布隆),才读 data block
      block_iter->Seek(k);                                                     // :229 —— 在 data block 里二分
      if (block_iter->Valid()) {
        (*handle_result)(arg, block_iter->key(), block_iter->value());         // :231 —— 找到,回调通知 caller
      }
      s = block_iter->status();
      delete block_iter;
    }
  }
  ...
}
```

**第 225 行是布隆过滤器在 LevelDB 里的唯一用法**。流程:

1. `iiter->Seek(k)`:在 index block 里二分,找到"可能含 k 的 data block"的 BlockHandle(它的 `offset` 就是 data block 在文件里的起点)。
2. **`filter->KeyMayMatch(handle.offset(), k)`**:用 data block 的 offset 算出它在第几个 2KB 区间(见 9.5.4),取那个 filter,查 k 在不在。
   - 返回 false:**布隆说"肯定没有"**——直接跳过(`// Not found` 分支),**不调 `BlockReader`,不读 data block,不走 cache**。一次磁盘 I/O 被挡掉了。
   - 返回 true:**布隆说"可能有"(可能误判)**——调 `BlockReader` 读 data block,在 block 里二分,看 k 真在不在。

这就是布隆的全部价值:**第 226 行的那个 `// Not found` 分支,挡掉了一次 data block 读**。对 LSM 这种"一个 key 散在多层、大部分 SSTable 不含它"的场景,绝大多数 `KeyMayMatch` 调用都返回 false,**绝大多数 data block 读被挡掉**。这就是削读放大的关键一刀。

> **钉死这件事**:布隆在 LevelDB 里**只在一处**起作用——`Table::InternalGet` 第 225 行。但它就是读放大之战的关键一战:对一次 `Get`,假设要查 10 个 SSTable,每个 SSTable 内部一次 `InternalGet`。没布隆的话,10 次 data block 读;有布隆(~1% 误判)的话,平均只有 ~1 次真读(那个真含 k 的 SSTable),其余 9 次被布隆挡掉。**读放大从 10 降到 1,这是布隆给 LSM 的最大礼物。**

---

## 9.7 技巧精解:DoubleHashing + 误判率数学

这一章有两个硬核技巧:**DoubleHashing(1 个 hash 衍生 k 个)** 和 **bits/key、k、误判率的数学权衡**。前者是工程优化,后者是参数选择的根据。

### 技巧精解 1:DoubleHashing——k 个 hash 用 1 个真 hash 衍生

**这个技巧在做什么**:让"需要 k 个独立 hash 函数"的布隆过滤器,实际只算 **1 个** hash 函数,剩下 k-1 次"hash"用整数加法和位运算衍生,大幅降低每次查询/插入的计算开销。

**朴素布隆的痛点**:布隆的理论要求 k 个**独立**的 hash 函数。最直白的实现就是真的准备 k 个 hash(比如 `h_1 = MD5(key)`, `h_2 = SHA1(key)`, `h_3 = CRC32(key)`, ...)。但:

1. **设计 k 个高质量独立 hash 函数很难**——密码学 hash 太慢,非密码学 hash 互相之间容易相关(不独立),相关 hash 会让"两个 hash 落到同一位"的概率上升,误判率高于理论。
2. **每次查询算 k 次 hash 太贵**——一次 Murmur hash ~几十纳秒,k=6 就 200+ 纳秒,而布隆的目标是"亚百纳秒级"。hash 计算本身成了瓶颈。

**Kirsch-Mitzenmacher 2006 的技巧**:用 **2 个**独立 hash `h_1(x)` 和 `h_2(x)`,线性组合出 k 个"伪独立" hash:

```
   g_i(x) = h_1(x) + i * h_2(x)    (i = 0, 1, ..., k-1)
```

论文证明:这组 `g_i` 在大部分实用场景下,误判率渐进等价于 k 个真正独立 hash。即"2 个 hash 衍生 k 个"≈"k 个独立 hash",数学上几乎不损失。

**LevelDB 更省:只用 1 个 hash**。看 [`util/bloom.cc:46-53`](../leveldb/util/bloom.cc#L46-L53) 的循环:

```cpp
      uint32_t h = BloomHash(keys[i]);                                    // :46 —— 真 hash,只这一次
      const uint32_t delta = (h >> 17) | (h << 15);  // Rotate right 17 bits  // :47 —— 从 h 里转出 delta
      for (size_t j = 0; j < k_; j++) {
        const uint32_t bitpos = h % bits;                                 // :49 —— 用 h 当第 j 个 hash
        array[bitpos / 8] |= (1 << (bitpos % 8));
        h += delta;                                                       // :51 —— h ← h + delta
      }
```

LevelDB 把 Kirsch-Mitzenmacher 的"2 个 hash"再压一步:**只算 1 个 `BloomHash`**,然后从中**位运算拆**出一个 `delta` 当"第二个伪 hash":

```
   delta = (h >> 17) | (h << 15)    —— 这是 h 循环右移 17 位(32 位无符号)
```

然后 `g_j = h + j * delta`(等价地,循环里 `h += delta` 累加):

```
   g_0 = h
   g_1 = h + delta
   g_2 = h + 2*delta
   ...
   g_{k-1} = h + (k-1)*delta
```

每个 `g_j % bits` 就是第 j 个 hash 位置。

**为什么 `delta = (h >> 17) | (h << 15)` 是个"好"的衍生**:

1. **它把 h 的高 17 位移到低位、低 15 位移到高位**——循环右移 17 位(等价于循环左移 15 位)。这是一种**双射变换**(一对一,可逆),不会丢信息。
2. **循环右移让 delta 和 h 在"高位字节"和"低位字节"上错开**——`h + j*delta` 累加时,高位和低位互相"打散",让 k 个 `g_j % bits` 位置在 bit 数组里分布得比较开,接近独立 hash 的效果。
3. **17 是个"经验数"**——选 17 让高位和低位大致各占一半(15/17),Delta 的两个"半"都参与累加,分布均匀。换成 16 也类似,17 是作者选的。

**反面对比 1(真的算 k 个独立 hash)**:每次查询算 6 次 Murmur(假设 k=6),每次 ~30ns,共 180ns。而 DoubleHashing 算 1 次 Murmur(30ns)+ 5 次加法 + 模运算(~10ns),共 ~40ns,**快 4 倍多**。在 LevelDB 这种"亚微秒级延迟"目标下,这是显著差距。

**反面对比 2(朴素取模 hash 衍生,不做位运算打散)**:比如 `delta = h % 7`(随便选个小质数)。`g_j = h + j*7`——每次只加 7,在 bit 数组里 k 个位置会挤在一起(差 7、14、21...),失去独立性,误判率明显高于理论。`delta = (h >> 17) | (h << 15)` 让 delta 是一个"和 h 几乎无关的大数",累加时位置才分布得开。

**反面对比 3(用 2 个独立 hash 严格按论文)**:`g_j = h1 + j*h2`,要算两次 Murmur。LevelDB 进一步压到 1 次,代价是 `h2` 不是真"第二个独立 hash"而是从 `h1` 派生——理论上略有相关性,实际误判率略高于理论(但不显著,LevelDB 作者显然测过,觉得可以接受)。这是"实用最优"和"理论最优"的工程取舍。

> **钉死这件事**:DoubleHashing 是 LevelDB 把布隆做到"亚微秒查询"的核心手段。它用一次 hash + k 次"加法 + 模运算"代替 k 次 hash,把计算开销降到 1/k。配合 `KeyMayMatch` 的短路退出(任一位是 0 立即返回),"key 不在"的常见 case 平均只查 1~2 次就结束,实际开销远低于最坏 k 次。这是 LSM 削读放大的关键工程优化。

### 技巧精解 2:bits/key、k、误判率的权衡表

**这个技巧在做什么**:给一个目标误判率,怎么选 `bits_per_key` 和 `k`?LevelDB 选了 `bits_per_key=10, k=6, p≈1%`,这个组合凭什么?

**核心数学**(9.3.1 节已给推导):

```
   p = (1 - e^(-kn/m))^k
   最优 k = (m/n) * ln2 ≈ (m/n) * 0.693
   最优 k 下:p ≈ (0.6185)^(m/n)
```

把这两条合起来,给定 `bits_per_key = m/n`,LevelDB 的 `k_` 和理论误判率:

| bits_per_key | LevelDB k_ (`int(x*0.69)`) | 理论最优 k (`x*0.693`) | 理论 p (`0.6185^x`) | LevelDB 实际 p(因为 k 向下取整) |
|--------------|----------------------------|------------------------|---------------------|----------------------------------|
| 6            | 4                          | 4.16                   | ~8.6%               | ~9.0%                            |
| 8            | 5                          | 5.54                   | ~3.3%               | ~3.5%                            |
| 9            | 6                          | 6.24                   | ~2.0%               | ~2.2%                            |
| **10**(默认) | **6**                      | 6.93                   | **~1.1%**           | **~1.1%**                        |
| 12           | 8                          | 8.32                   | ~0.42%              | ~0.45%                           |
| 14           | 9 (`int(14*0.69)=9`)       | 9.70                   | ~0.16%              | ~0.18%                           |
| 20           | 13                         | 13.86                  | ~0.0067%            | ~0.008%                          |

(注:LevelDB `int(x*0.69)` 用的是 0.69 不是 0.693,差别可忽略。)

**权衡的几个直觉**:

1. **bits/key 前几 bit 收益最大**。从 6→10 bit,误判率从 9% 压到 1.1%(压了 8 倍);从 10→14 bit,误判率从 1.1% 压到 0.18%(压 6 倍);从 14→20 bit,只压 27 倍但代价是空间翻倍。**边际收益递减**——曲线是指数衰减,前期陡、后期平。
2. **LevelDB 选 10 bit/key 的理由**:1% 误判率是个甜点——99% 的无效查询被挡掉,空间开销 10 bit/key = 1.25 字节/key,对平均 key 几十字节的场景,只占 key 体积的 ~2%。再加 bit 收益递减,不值。
3. **k 向下取整的小代价**:LevelDB `k_ = int(x*0.69)`,比理论最优 `(x*0.693)` 略小,误判率比理论略高(~0.1~0.3 个百分点)。换来的是每次查询少 hash 一次(性能提升)。这是"略微牺牲误判率换性能"的工程取舍。

**反面对比 1(bits/key 选太小,比如 4)**:误判率 ~20%,五次查询就有一次误判,挡无效查询的效率太低,LSM 读放大没被压下去,布隆的意义打折。

**反面对比 2(bits/key 选太大,比如 30)**:误判率 ~10^-6,但 filter 体积 30 bit/key = 3.75 字节/key,占 key 体积 ~10%,filter block 显著变大,**filter block 常驻内存的开销**上升(见 9.5.5)。对一个 100GB 库,filter block 总大小可能从几十 MB 涨到几百 MB。**空间收益和内存代价失衡**。

**反面对比 3(用 1 个 hash 即 k=1)**:误判率 `p = 1 - e^(-n/m)`,在 m/n=10 时约 63%——大部分查询都误判,布隆形同虚设。k 必须足够大才能压低误判率。

**反面对比 4(固定 k,不管 bits/key)**:有人可能想"k=10 总比 k=6 好"。错。给定 bits/key,k 不是越大越好——`k = (m/n) * ln2` 是最优点。k 太大时,bit 数组被填得太满(`e^(-kn/m)` 接近 0,`1 - e^(-kn/m)` 接近 1),查询时几乎任何 k 个位都"全是 1",误判率反而上升。这是布隆数学最反直觉的一点。

> **钉死这件事**:`bits_per_key=10, k=6, p≈1%` 是 LevelDB 经过权衡的甜点。它的数学根据是最优 `k = (m/n) * ln2` 和 `p ≈ 0.6185^(m/n)`,它的工程根据是"1% 误判率下 99% 无效查询被挡、空间开销可接受"。如果你要改这个参数(比如对延迟敏感场景想要 0.1% 误判率),`options.filter_policy = NewBloomFilterPolicy(15)` 就行——`BloomFilterPolicy` 构造函数自动算出 k=10。

---

## 9.8 章末小结

这一章讲清了 SSTable 四级布局里的 meta block——filter block(布隆过滤器):

1. **布隆的数学**:bit 数组 + k 个 hash,"全 1 才可能有(可能误判)、任一 0 就肯定没有(绝不漏判)"。误判率 `p ≈ (1 - e^(-kn/m))^k`,最优 `k = (m/n) * ln2`,最优 k 下 `p ≈ 0.6185^(m/n)`。
2. **DoubleHashing**:k 个 hash 实际只算 1 个 `BloomHash`,从中拆出 `delta = (h>>17)|(h<<15)`,用 `g_j = h + j*delta` 衍生 k 个位置。查询从 k 次 hash 降到 1 次 hash + k 次加法,**快 ~4 倍**。
3. **默认参数**:`bits_per_key=10, k=6`,误判率 ~1%。bits/key 每加 1,p 乘 0.6185(边际收益递减)。10 是甜点,RocksDB 沿用。
4. **filter block 布局**:每 2KB 文件偏移生成一个独立 filter,末尾 `offset array + array_offset + base_lg_`。reader 用 `block_offset >> base_lg_` 算 filter 下标。
5. **常驻内存**:filter block 在 `Table::Open` 时一次性读进、常驻 `rep_->filter`,不走 block cache(因为小而常用)。`Table::InternalGet` 在读 data block 前调 `KeyMayMatch(handle.offset(), k)` 剪枝——**这是布隆在 LevelDB 里唯一用法,但它挡掉的是 LSM 读放大最痛的一刀**。

回到主线:这一章服务**前台**——布隆过滤器是读路径上的第一道剪枝,把"肯定没有"的 data block 读挡掉,直接削读放大。它的设计处处体现"用小空间换大收益":10 bit/key 换 99% 无效查询被挡,DoubleHashing 换 4 倍查询加速,2KB 分片换按需小 filter 查询。这是 LevelDB "前台要快"哲学在 filter block 上的具体兑现。

### 五个"为什么"清单

1. **为什么布隆过滤器是单向的(允许 false positive,绝不允许 false negative)?** 由它的机制决定:插入只置 1(bit 只能 0→1),所以"插过的 key 它的 k 个位一定还是 1"——查询时绝不会漏判。但别的 key 可能恰好把某 key 的 k 个位都置过 1,于是"没插过却全 1"——可能误判。这是 bit 数组"只置 1 不置 0"的直接后果,也是布隆的设计取舍(允许误判换空间)。
2. **为什么 LevelDB 默认 `bits_per_key = 10`?** 数学上这是"误判率 ~1%"的甜点(`0.6185^10 ≈ 1.1%`),99% 的无效查询被挡掉,空间开销 1.25 字节/key 只占 key 体积 ~2%。再加 bit 收益递减(曲线指数衰减),不值。RocksDB 也沿用 10。
3. **为什么 `k_ = bits_per_key * 0.69` 向下取整,不向上取整?** 工程取舍:向下取整少 hash 一次(性能),代价是误判率略升(可接受)。注释明说 `"We intentionally round down to reduce probing cost a little bit"`。每 query 少 1 次 hash 在亚微秒级延迟目标下显著。
4. **DoubleHashing 的 `delta = (h >> 17) | (h << 15)` 凭什么能代替"第二个独立 hash"?** Kirsch-Mitzenmacher 2006 证明:`g_j = h_1 + j*h_2` 近似 k 个独立 hash。LevelDB 进一步把 `h_2` 用 `h_1` 的循环右移 17 位代替——理论上 `h_1` 和 `delta` 略相关,但循环右移让"高位字节"和"低位字节"错开,实际分布足够均匀,误判率只略高于理论。省了 1 次 hash 计算。
5. **filter block 为什么每 2KB 分片,不和 data block 一样 4KB?** 2KB 比 4KB 更细,每个 filter 更小(查询读的字节更少);不对齐 data block 让一个 filter 平均覆盖半个 data block,粒度合适。2KB 是 LevelDB 经验值,RocksDB 也沿用。

### 想继续深入往哪钻

- **原始论文**:Kirsch and Mitzenmacher, "Less Hashing, Same Performance: Building a Better Bloom Filter", 2006。DoubleHashing 的理论基础,证明 `g_j = h_1 + j*h_2` 近似 k 个独立 hash。免费能搜到 PDF。
- **LevelDB 官方文档**:[`doc/table_format.md`](../leveldb/doc/table_format.md) 的 "Filter Meta Block" 一节,有 filter block 布局的最权威描述(2KB 分片、offset array、base_lg)。
- **完整源码**:[`util/bloom.cc`](../leveldb/util/bloom.cc)(92 行,布隆核心)、[`table/filter_block.cc`](../leveldb/table/filter_block.cc)(分片逻辑)、[`util/hash.cc`](../leveldb/util/hash.cc)(Murmur hash 实现)。三个文件加起来不到 200 行,一遍能读完。
- **RocksDB 怎么扩展**:RocksDB 加了 ribbon filter、cuckoo filter、partitioned filter(filter block 也分片成多个 block,避免大 filter block 全读)。但底层"布隆 + DoubleHashing"的骨架没变。
- **布隆的变种**:Counting Bloom(支持删除,每 bit 改成几 bit 的 counter,空间换功能)、Cuckoo Filter(用 cuckoo hash 表存 fingerprint,支持删除、查询更快)。LevelDB 不需要删除(SSTable immutable,filter 一次性生成),所以用最朴素的布隆。

### 引出下一章

到这一章,第 2 篇(SSTable 格式)的四级布局已经讲清:data block(P2-08 前缀压缩)、filter block(本章布隆)、index block(P2-07/08)、footer(P2-07)。但还有一个根本问题没回答:**这些 block 是怎么"组装"成一个 SSTable 文件的?** MemTable 刷盘时,谁在调 `data_block.Add`、谁在调 `filter_block.AddKey`、谁在算 `FindShortestSeparator`、谁在最后调 `Finish` 写 footer?反过来,**打开一个 SSTable 文件时**,谁在读 footer、读 index、懒加载 data block?这就是下一章 P2-10 的事——**TableBuilder 与 Table**:构建侧的 `Add → Flush → Finish` 状态机,打开侧的 `Open` + **TwoLevelIterator 懒加载**(上层 index 迭代器二分定位、下层 data block 按需读、走 block cache)。这是第 2 篇的收官,也是通往第 3 篇(读取)的桥梁。
