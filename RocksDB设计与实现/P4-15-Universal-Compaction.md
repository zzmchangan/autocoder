# 第 4 篇 · 第 15 章 · Universal Compaction

> **核心问题**:上一章我们讲完 Level Compaction,默认策略把一个 key 从 L0 一路逐层下压到 L6,理论上要被重写七八遍。这在机械硬盘时代没什么,可 SSD 时代海量写场景(TiKV 存海量 KV、时序数据库狂写日志、消息系统的 state store),写放大七八倍直接吃 SSD 的写入寿命,而且 Compaction 的 IO 把前台写拖到雪崩。RocksDB 怎么给这类"写放大敏感"的 workload 留一个出口?——答案就是本章的 **Universal Compaction**:它不再逐层下压,而是把"L0 风格的合并规则一路推广到所有层",让大小相近的 sorted run 直接合并,把写放大从"约等于层数"压到"约等于 log(总数据量/MemTable)"。代价是层数不规整、空间放大大(旧版本留得久)、读放大随 sorted run 数增长。一句话:**用空间放大换写放小**,这正是 SSD 海量写场景最划算的那笔账。

> **读完本章你会明白**:
> 1. Universal Compaction 的"sorted run"是什么概念,为什么它是"L0 风格一路推广到所有层",和 Level 的"层"根本不是一回事。
> 2. Universal 的触发到底有几路(关键!),`NeedsCompaction` 凭什么拍板"该合并了",为什么不是总纲简表里说的"size ratio / age 两路",而是更完整的六路触发。
> 3. ★ **size ratio 触发**怎么从一组 sorted run 里按"累计大小超过最大文件 size_ratio%"挑出待合并的连续段,`size_ratio` 默认值为什么是 1 而不是 0。
> 4. ★ **size amp 触发**怎么用 `max_size_amplification_percent`(默认 200)给空间放大设硬上限,以及它和"sorted run num 触发"(`max_read_amp` / `level0_file_num_compaction_trigger` 的关系)各管哪一头。
> 5. Universal 的写放大到底怎么算,为什么是 log 级别,以及为什么用它扛 SSD 海量写比 Level 省寿命。
> 6. `compaction_options_universal` 八个字段(`size_ratio` / `min_merge_width` / `max_merge_width` / `max_size_amplification_percent` / `compression_size_percent` / `max_read_amp` / `stop_style` / `allow_trivial_move`,外加两个 EXPERIMENTAL 的 `incremental` / `reduce_file_locking`)各自在调什么,以及什么时候该选 Universal。

> **如果一读觉得太难**:先只记住三件事——① Universal 的核心思想是"相似大小的 sorted run 直接合并",不逐层下压,所以一个 key 被重写的次数 ≈ log(数据量/MemTable),远少于 Level 的 ~7 层;② Universal 主要靠两路触发维持平衡:size ratio 触发(把累计大小超过最大文件 size_ratio% 的一组合并)和 size amp 触发(空间放大超过 max_size_amplification_percent% 就把所有新文件合到最老的 base 上);③ 代价是空间放大大、读放大大,适合"写多读少、能接受空间放大"的 SSD 海量写场景。

---

## 〇、一句话点破

> **Level Compaction 把一个 key 从 L0 逐层下压到 L6,每过一层就重写一遍,写放大 ≈ 层数 ~7;Universal Compaction 不逐层下压,而是把"L0 的合并规则一路推广到所有层"——大小相近的 sorted run 直接并成一个更大的 sorted run,一个 key 被重写的次数 ≈ log₂(总数据量 / MemTable 大小)。代价是层数不规整、空间放大大(旧版本留得久)、读放大多扫几个 sorted run。这笔账在 SSD 海量写场景最划算:写放大直接吃 SSD 寿命,Universal 把它压到 log 级别,等于给 SSD 续命。**

这是结论,不是理由。本章倒过来拆:先讲清 Universal 的"sorted run"是什么(它和 Level 的"层"不一样),再讲它的触发规则,然后讲它的写放大怎么算、为什么 SSD 海量写场景非它不可,最后讲 `compaction_options_universal` 八个旋钮各自在调什么。

---

## 一、动机:Level Compaction 在 SSD 海量写下撞什么墙

要理解 Universal 为什么存在,先回到 LevelDB/Level Compaction 的写放大这个老问题。

### 写放大是怎么堆出来的

回顾一下 Level Compaction 的写放大(详见 P4-14)。Level 把 SST 分成至多 7 层(L0~L6),每层是上一层大小的 10 倍(`max_bytes_for_level_multiplier` 默认 10)。一个 key 从 MemTable Flush 成 L0 SST 开始,会因为它所在的层"超了目标大小"而被合并到下一层:

```
L0(4MB) ──合并──> L1(10MB) ──> L2(100MB) ──> L3(1GB) ──> L4(10GB) ──> L5(100GB) ──> L6(1TB)
```

一个 key 如果最后落在 L6,那它从写入到稳定,理论上要被重写 7 次(L0→L1, L1→L2, …, L5→L6)。再加上 L0 内部合并、MemTable 的 WAL,实际写放大(SSD 实测)常达 **10~30 倍**——这是 LSM 的著名痛点,叫 write amplification。

> **钉死这件事**:Level Compaction 的写放大理论下界 ≈ 层数,实测常达 10~30 倍。读放大小(每层至多读一个文件,加上 Bloom 早退),空间放大小(层数规整、Compaction 勤)。所以 Level 是"读放大小、写放大大"的取舍,适合**读多写少**的 workload。

### SSD 海量写场景为什么撞墙

Level 这个取舍在机械硬盘时代没什么。机械硬盘的随机写慢,反正写放大是顺序写,顺序写多写几遍可以接受。但 SSD 时代不一样了,有两个新现实:

1. **SSD 的写入次数有限**(NAND 闪存的 P/E 周期,消费级 TLC 几百次、企业级 eMLC 几万次)。写放大 10 倍意味着同样的逻辑写入,SSD 物理写入 10 倍,寿命被吃掉 10 倍。这在海量写场景(TiKV 存全量 KV、Kafka Streams state store、时序数据库狂写日志、Cassandra 类系统的 commitLog + memtable)是直接的硬件成本:SSD 比预期更早报废,或者被迫用更贵的 optane / ZNS SSD。

2. **Compaction 的后台 IO 会挤占前台写延迟**。写放大 10 倍意味着后台要做 10 倍的合并 IO,这些 IO 跟前台写共享磁盘带宽。当写入速度上来,Compaction 跟不上,L0 文件堆积,触发 Write Stall(P5-17),前台写延迟雪崩。

工业界要的是:**能不能用多一点空间放大,换小一点的写放大?**——这是 LevelDB 给不了的选项(LevelDB 只有类 Level 一种 Compaction)。RocksDB 加 Universal,就是给"写放大敏感"的 workload 一个旋钮。

> **不这样会怎样**:如果 SSD 海量写场景只有 Level Compaction 可用,会出现两种结局——要么 SSD 寿命被写放大吃光(硬件成本爆炸),要么 Compaction 跟不上导致 L0 堆积、Write Stall 把前台写延迟拖到雪崩。这两者都是工业生产不可接受的。Universal Compaction 就是 RocksDB 给这条退路。

---

## 二、核心抽象:sorted run ——L0 风格一路推广

讲触发规则前,必须先讲清 Universal 最核心的抽象:**sorted run**。这个词在 Level Compaction 里几乎不出现,但在 Universal 里它是一切的基础。看不懂 sorted run,后面的触发规则全是天书。

### 什么是 sorted run

在 Universal Compaction 的视角下,RocksDB 不再把存储看成"L0 多个文件 + L1..L6 各一层有序文件",而是看成**一串 sorted run**(有序的文件集合),从新到旧排列。

一个 sorted run,要么是:

- **L0 里的一个文件**(L0 文件之间 key range 是允许重叠的,Flush 一次产出一个 L0 文件,所以每个 L0 文件本身就是一个 sorted run);
- **L1 及以上的某一层**(L1+ 的文件之间 key range 不重叠且整体有序,所以一整层合起来是一个 sorted run)。

源码里 `UniversalCompactionBuilder::CalculateSortedRuns`([`db/compaction/compaction_picker_universal.cc#L681-L729`](../rocksdb/db/compaction/compaction_picker_universal.cc#L681-L729))就是干这件事:它扫一遍所有层,把 L0 的每个文件当一个 sorted run,把 L1+ 的每一层当一个 sorted run,然后**按"从新到旧"的时间顺序**排成一个数组 `sorted_runs_`。

```cpp
// 简化示意,非源码原文(摘自 CalculateSortedRuns 的核心逻辑):
for (FileMetaData* f : vstorage.LevelFiles(0)) {
  // 每个 L0 文件是一个 sorted run
  ret.emplace_back(0, f, f->fd.GetFileSize(), f->compensated_file_size, ...);
}
for (int level = 1; level <= last_level; level++) {
  // L1+ 每一层的所有文件合起来是一个 sorted run
  // size 和 compensated_file_size 是该层所有文件的总和
  ...
  ret.emplace_back(level, nullptr, total_size, total_compensated_size, ...);
}
```

`SortedRun` 这个结构体本身([`compaction_picker_universal.cc#L72-L108`](../rocksdb/db/compaction/compaction_picker_universal.cc#L72-L108))藏着一个关键设计:

```cpp
struct SortedRun {
  int level;
  FileMetaData* file;  // L0 时指向那个文件;L1+ 时为 nullptr
  uint64_t size;
  uint64_t compensated_file_size;
  bool being_compacted;
  ...
};
```

注意 `file` 字段的注释:"`Will be null for level > 0. For level = 0, the sorted run is for this file.`"——这正是 sorted run 的两种身份:L0 的"单文件"和 L1+ 的"整层"。

### 为什么 sorted run 是"L0 风格一路推广"

这就是 Universal 和 Level 最根本的区别。

| 视角 | Level Compaction | Universal Compaction |
|---|---|---|
| 数据怎么组织 | L0(多文件可重叠)→ L1..L6(每层不重叠、整体有序、层间 10 倍) | 一串 sorted run(L0 每个文件 + L1+ 每层),从新到旧 |
| 合并规则 | L0→L1 把重叠文件合进去;L1→L2..Ln 是"层间下压",每次只合当前层超量的部分 | "相似大小的 sorted run 直接合并"(就是 L0 那种"几个文件并成一个"的规则,一路推广) |
| 一个 key 被重写次数 | ≈ 层数 ~7 | ≈ log₂(总数据量/MemTable) |
| 层数是否规整 | 规整(每层有固定 target size) | 不规整(层数随合并演化) |

可以这样直白地理解 Universal:

> **Level 的 L0 是个"特殊层"——里面的文件可以重叠,Flush 来一个就加一个,文件多了就把这一组合并成一个更大的文件推到 L1。Universal 干的事,就是把这个"L0 的合并规则"一路推广到所有层:每一层都像 L0,文件多了就把"大小相近"的一组并成一个更大的,一路滚下去。**

这种"相似大小文件直接合并"的风格有个学术名字,叫 **size-tiered compaction**(Cassandra / ScyllaDB 的 STCS 就是这种思路)。RocksDB 的 Universal 是 size-tiered 在 LSM-tree 里的具体实现。

### 配图:Level vs Universal 的视图对照

```
Level Compaction 的视图(层数规整、层间下压):

  L0: [f1][f2][f3]          ← 文件可重叠,Flush 来一个加一个
  L1: =====一条有序带=====   ← 10MB,被 L0 合并填进来
  L2: =====一条更长的带===== ← 100MB,被 L1 下压填进来
  L3: ====================
  ...
  L6: ============================== ← 1TB

  合并方向:L0→L1→L2→...→L6,逐层下压,一个 key 重写 ~7 次


Universal Compaction 的视图(一串 sorted run,相似大小直接合并):

  sorted_runs_ 数组(从新到旧,大小不必规整):
  ┌──────┐ ┌──────┐ ┌────────┐ ┌────────────┐ ┌────────┐
  │ SR0  │ │ SR1  │ │  SR2   │ │    SR3     │ │  SR4   │
  │ 4MB  │ │ 4MB  │ │  8MB   │ │   16MB     │ │ base   │
  │ (L0) │ │ (L0) │ │ (L1)   │ │   (L2)     │ │ (L3)   │
  └──────┘ └──────┘ └────────┘ └────────────┘ └────────┘
     新                                              旧

  合并规则:把"大小相近"的一组(sorted_runs_ 数组里相邻、累计大小
           超过最大文件的 size_ratio%)直接并成一个更大的 sorted run。
           不逐层下压,而是相似大小直接归并。
```

注意这个图里 SR0/SR1 是 L0 的两个文件(各 4MB),SR2 是 L1 整层(8MB,是之前合并出来的),SR3 是 L2 整层(16MB),SR4 是 base(最老的一层)。它们的大小不必遵守 Level 那种"每层 10 倍"的规整规则,而是随合并自然演化成"越老越大"的近似几何级数(因为相似大小的合并成更大的,更大的再合并成更更大的)。

> **钉死这件事**:理解 Universal 的起点是 sorted run——它把"L0 的合并规则一路推广到所有层"。Level 是"层数规整、逐层下压",Universal 是"一串 sorted run、相似大小直接归并"。这个抽象决定了 Universal 的一切后续行为(触发规则、写放大、空间放大)。

---

## 三、触发规则:Universal 到底有几路触发

这是本章最容易踩坑的地方。总纲简表里把 Universal 的触发说成"size ratio / age 两路",这是为了快速记忆的简化。但**实际源码里 Universal 的触发有完整的六路**,而且是按优先级顺序尝试的。看不懂这六路,你调 `compaction_options_universal` 时会完全不知道每个旋钮在管什么。

### `PickCompaction` 的六路优先级

Universal 的入口是 `UniversalCompactionPicker::PickCompaction`([`compaction_picker_universal.cc#L629-L641`](../rocksdb/db/compaction/compaction_picker_universal.cc#L629-L641)),它把活儿转给 `UniversalCompactionBuilder::PickCompaction`([`compaction_picker_universal.cc#L767-L868`](../rocksdb/db/compaction/compaction_picker_universal.cc#L767-L868))。后者是这个文件里最重要的函数,它的核心是这一段按优先级链式调用([`compaction_picker_universal.cc#L803-L812`](../rocksdb/db/compaction/compaction_picker_universal.cc#L803-L812)):

```cpp
Compaction* c = nullptr;

c = MaybePickPeriodicCompaction(c);                              // 路一
c = MaybePickSizeAmpCompaction(c, file_num_compaction_trigger);  // 路二
c = MaybePickCompactionToReduceSortedRunsBasedFileRatio(         // 路三
    c, file_num_compaction_trigger, ratio);
c = MaybePickCompactionToReduceSortedRuns(c,                     // 路四
    file_num_compaction_trigger, ratio);
c = MaybePickDeleteTriggeredCompaction(c);                       // 路五
c = MaybePickReadTriggeredCompaction(c);                         // 路六
```

这六个 `MaybePick*` 函数,每个都遵循同一个模式:**如果前面的 `c` 已经非空(已经选出来了),就直接返回;否则才尝试本路**。这意味着它们是**按优先级顺序短路**的——一旦某一路挑出了 compaction,后面的路就不会再尝试。

这六路的优先级和触发条件是:

| 路次 | 函数 | 触发条件 | 解决什么问题 |
|---|---|---|---|
| 一 | `MaybePickPeriodicCompaction` | 有文件被标记需要 periodic compaction(老文件没合并过) | 解决"老文件太久没合并、统计/读放大退化" |
| 二 | `MaybePickSizeAmpCompaction` | `sorted_runs_.size() >= file_num_compaction_trigger` 且空间放大超 `max_size_amplification_percent` | **空间放大硬上限**(本章重点之二) |
| 三 | `MaybePickCompactionToReduceSortedRunsBasedFileRatio` | sorted run 数够 + 有连续段累计大小超过最大文件 size_ratio% | **size ratio 触发**(本章重点之一) |
| 四 | `MaybePickCompactionToReduceSortedRuns` | sorted run 数超过 `max_read_amp`(或回退到 `level0_file_num_compaction_trigger`) | **sorted run 数上限触发**(读放大控制) |
| 五 | `MaybePickDeleteTriggeredCompaction` | 有文件因墓碑密度被 `CompactOnDeleteCollector` 标记 | 解决墓碑聚集 |
| 六 | `MaybePickReadTriggeredCompaction` | 有文件被读路径标记(读到老版本触发重写) | 解决热 key 读老版本 |

> **纠正一个常见误解**:很多人(包括一些 RocksDB 教程)把 Universal 的触发简化成"size ratio + age 两路"。这是不准确的。源码里实际是六路,而且总纲简表里说的"age 触发"——**RocksDB 源码里根本没有按"文件真实年龄(时间)"触发的逻辑**。被简称为"age"的,实际上是路四 `MaybePickCompactionToReduceSortedRuns`,它的真实语义是"sorted run 数量超过上限就强制合并最旧的",**用数量代理时间**(数据越老越在数组后面,数量越多说明越老的堆积越多)。这是简化记忆和源码真相之间的偏差,本章以源码真相为准。

下面把四条主路(二、三、四,以及和它们配合的 NeedsCompaction 判定)逐一拆透。路一/五/六是辅助路,简要交代。

### `NeedsCompaction`:Universal 怎么判断"该不该启动"

在讲具体每一路之前,先讲清 Universal 怎么判断"现在需不需要 compaction"。入口是 `UniversalCompactionPicker::NeedsCompaction`([`compaction_picker_universal.cc#L608-L624`](../rocksdb/db/compaction/compaction_picker_universal.cc#L608-L624)):

```cpp
bool UniversalCompactionPicker::NeedsCompaction(
    const VersionStorageInfo* vstorage) const {
  const int kLevel0 = 0;
  if (vstorage->CompactionScore(kLevel0) >= 1) {   // 主信号
    return true;
  }
  if (!vstorage->FilesMarkedForPeriodicCompaction().empty()) {  // 路一辅助
    return true;
  }
  if (!vstorage->FilesMarkedForCompaction().empty()) {          // 路五辅助
    return true;
  }
  if (!vstorage->ReadTriggeredCompactionFiles().empty()) {      // 路六辅助
    return true;
  }
  return false;
}
```

这里有个关键点:Universal 用 `CompactionScore(kLevel0)` 当主信号。但 Universal 的 L0 score 和 Level 的 L0 score **含义完全不同**!看 `VersionStorageInfo::ComputeCompactionScore`([`db/version_set.cc#L3974-L4069`](../rocksdb/db/version_set.cc#L3974-L4069))里 Universal 那段:

```cpp
if (compaction_style_ == kCompactionStyleUniversal) {
  // For universal compaction, we use level0 score to indicate
  // compaction score for the whole DB. Adding other levels as if
  // they are L0 files.
  for (int i = 1; i <= max_output_level; i++) {
    if (!files_[i].empty() && !files_[i][0]->being_compacted) {
      num_sorted_runs++;   // ★ 把 L1+ 每个非空层也算作一个 sorted run
    }
  }
}
...
score = static_cast<double>(num_sorted_runs) /
        mutable_cf_options.level0_file_num_compaction_trigger;
```

> **钉死这件事**:Universal 的 L0 score 公式是 `(L0 文件数 + L1+ 非空层数) / level0_file_num_compaction_trigger`。换句话说,Universal **把整个 DB 看成一串 sorted run,score 就是 sorted run 总数除以触发阈值**。这是 sorted run 抽象的源码铁证——Universal 在 score 计算这一层就已经把"L0 文件"和"L1+ 整层"一视同仁了。score >= 1 就触发 NeedsCompaction 返回 true。

这个公式还有一个重要含义:**`level0_file_num_compaction_trigger` 在 Universal 下不再是"L0 文件数"的阈值,而是"sorted run 总数"的阈值**(默认是 `1 << 30` 即 ~10 亿,需要用户显式调小,详见 `options.cc:505`)。这是 Universal 调参时最容易踩的坑——很多人以为它和 Level 一样管 L0 文件数,其实在 Universal 下它管的是全 DB 的 sorted run 数。

---

## 四、★ size ratio 触发:相似大小文件怎么挑出来合并

现在进本章第一个招牌技巧:**size ratio 触发**怎么从一串 sorted run 里挑出"该合并的连续段"。这是 Universal 区别于 Level 的灵魂——它定义了"什么时候相似大小的文件该合并"。

### 设计动机:为什么要按 size ratio 挑

回到 size-tiered 的核心思想:**相似大小的 sorted run 直接合并**。但"相似"是个模糊词——4MB 和 4MB 算相似,4MB 和 5MB 算吗?4MB 和 8MB 呢?

RocksDB 的设计是:**给一个百分比的容忍度,叫 `size_ratio`**(默认 1,见 [`universal_compaction.h#L27-L30`](../rocksdb/include/rocksdb/universal_compaction.h#L27-L30))。规则是:从最新的一组 sorted run 开始往旧方向扫,**如果"当前已挑中的累计大小" × (1 + size_ratio/100) 仍然大于下一个 sorted run 的大小,就把它也纳入这次合并;否则停止**。

直白说就是:**只要下一个 sorted run 没比"已挑中的累计大小大太多(超过 size_ratio%)",就继续把它并进来**。这样挑出来的连续段,就是一组"大小相近"的 sorted run,合并它们产出一个新的、更大的 sorted run。

> **LevelDB 是写死的**:LevelDB 只有一种 Compaction(类 Level),根本没有 size-tiered 这条路。这个 size ratio 触发规则是 RocksDB 独有的(为 SSD 海量写加的)。详见《LevelDB》Compaction 那章。

### 源码精解:`PickCompactionToReduceSortedRuns`

size ratio 触发的核心函数是 `UniversalCompactionBuilder::PickCompactionToReduceSortedRuns`([`compaction_picker_universal.cc#L908-L1137`](../rocksdb/db/compaction/compaction_picker_universal.cc#L908-L1137))。这个函数有点长,但核心逻辑就两层循环([`compaction_picker_universal.cc#L931-L1027`](../rocksdb/db/compaction/compaction_picker_universal.cc#L931-L1027)):

```cpp
// 简化示意,非源码原文(保留判定逻辑):
unsigned int min_merge_width = ...;  // 一次合并至少挑几个(默认 2)
unsigned int max_merge_width = ...;  // 一次合并至多挑几个(默认 UINT_MAX)

for (size_t loop = 0; loop < sorted_runs_.size(); loop++) {
  candidate_count = 0;

  // 跳过正在合并的 sorted run,挑出第一个候选作为起点
  for (sr = nullptr; loop < sorted_runs_.size(); loop++) {
    sr = &sorted_runs_[loop];
    if (!sr->being_compacted && !sr->level_has_marked_standalone_rangedel) {
      candidate_count = 1;
      break;
    }
    sr = nullptr;
  }

  uint64_t candidate_size = sr->compensated_file_size;  // 起点的"已累计大小"

  // 往旧方向扫,挑出连续的相似大小 sorted run
  for (size_t i = loop + 1;
       candidate_count < max_files_to_compact && i < sorted_runs_.size();
       i++) {
    const SortedRun* succeeding_sr = &sorted_runs_[i];
    if (succeeding_sr->being_compacted || ...) break;

    // ★ size ratio 判定的核心这一行 ★
    double sz = candidate_size * (100.0 + ratio) / 100.0;
    if (sz < static_cast<double>(succeeding_sr->size)) {
      break;  // 下一个比累计大小大太多,停止
    }

    // 根据 stop_style 更新 candidate_size
    if (stop_style == kCompactionStopStyleSimilarSize) {
      // SimilarSize:candidate_size 只记"上一个文件的大小",要求"两两相似"
      sz = (succeeding_sr->size * (100.0 + ratio)) / 100.0;
      if (sz < static_cast<double>(candidate_size)) break;
      candidate_size = succeeding_sr->compensated_file_size;
    } else {  // kCompactionStopStyleTotalSize(默认)
      // TotalSize:candidate_size 是"累计大小",只要累计仍大于下一个就继续
      candidate_size += succeeding_sr->compensated_file_size;
    }
    candidate_count++;
  }

  // 挑出来的连续段够 min_merge_width 个,就用它
  if (candidate_count >= min_merge_width) {
    start_index = loop;
    done = true;
    break;
  }
}
```

这里有两个关键设计点要单独拆透。

#### 设计点一:`size_ratio` 默认是 1,不是 0

很多教程把 `size_ratio` 说成"相似度百分比",让人以为是 0~100 的数。其实默认是 **1**(见 [`universal_compaction.h#L134`](../rocksdb/include/rocksdb/universal_compaction.h#L134):`size_ratio(1)`)。为什么是 1?

看判定公式 `sz = candidate_size * (100.0 + ratio) / 100.0`。当 ratio=1 时,sz = candidate_size × 1.01。意思是:**已挑中的累计大小只要再涨 1% 还能盖住下一个,就把它纳入**。这是个非常宽松的"相似"判定——它实际效果是"几乎任何连续的几个 sorted run 都会被挑出来合并",只有当某个 sorted run 比前面累计的大一截(超过 1%)时才停。

为什么默认这么宽松?因为 Universal 的哲学就是"勤合并、相似大小直接并"。size_ratio 越小,越勤合并(写放大大但 sorted run 数少、读放大小);size_ratio 越大,越懒合并(写放大小但 sorted run 数多、读放大大)。默认 1 是偏"勤合并"的取向。

> **不这样会怎样**:如果默认 size_ratio 是 0,那判定公式变成 `sz = candidate_size`,只要下一个 sorted run 比"已累计"大一点点(哪怕 1 字节),就停。这样挑出来的连续段会很短(经常只挑 2 个),合并次数频繁但每次合并量小,反而增加 compaction 调度开销。size_ratio=1 给了一点弹性,让一次合并能多挑几个,摊薄调度成本。

#### 设计点二:`stop_style` 决定 candidate_size 怎么更新

`stop_style` 有两个值(见 [`universal_compaction.h#L20-L23`](../rocksdb/include/rocksdb/universal_compaction.h#L20-L23)):

- **`kCompactionStopStyleTotalSize`(默认)**:candidate_size 是**累计大小**。判定是"累计大小 × (1+ratio%) 是否大于下一个"。这意味着只要累计滚得够大,就能一直把更大的 sorted run 并进来。结果是:**挑出来的连续段会比较长,合并产出的大 sorted run 会很大**。
- **`kCompactionStopStyleSimilarSize`**:candidate_size 只记**上一个挑中文件的大小**(不是累计)。判定是"上一个文件大小 × (1+ratio%) 是否大于下一个,且下一个 × (1+ratio%) 是否大于上一个"——双向判定,要求**两两大小相近**。结果是:**挑出来的连续段都是真正大小相近的 sorted run,不会因为累计滚大而吞掉更大的**。

这两种 stop_style 的取舍是:

| stop_style | 倾向 | 写放大 | 空间放大 | 适用 |
|---|---|---|---|---|
| TotalSize(默认) | 一次合并多、产出大文件 | 略大(吞了更大的) | 小(收敛快) | 多数场景 |
| SimilarSize | 一次合并少、产出相似大小 | 小(只并真正相近的) | 大(收敛慢) | 写放大极致敏感 |

源码里 `GetMaxNumFilesToCompactBasedOnMaxReadAmp`([`compaction_picker_universal.cc#L110-L168`](../rocksdb/db/compaction/compaction_picker_universal.cc#L110-L168))还有一段注释专门交代:SimilarSize 模式下 `max_read_amp` 的 auto-tune(=0)行为不实现,会回退到 `file_num_compaction_trigger`([`compaction_picker_universal.cc#L143-L146`](../rocksdb/db/compaction/compaction_picker_universal.cc#L143-L146))。这是因为 SimilarSize 模式下"层数大小规则"不规整,没法按 TotalSize 那套公式估算 max_run_size。

#### 配图:size ratio 触发挑文件的过程

```
sorted_runs_ 数组(从新到旧),假设 size_ratio=1, stop_style=TotalSize:

  下标:   0      1      2      3      4
         ┌────┐ ┌────┐ ┌─────┐ ┌─────┐ ┌─────┐
         │ 4M │ │ 4M │ │ 8M  │ │ 16M │ │base │
         └────┘ └────┘ └─────┘ └─────┘ └─────┘
           新                                    旧

外层循环 loop=0:
  起点 sr = sorted_runs_[0] (4M),candidate_size = 4M,candidate_count = 1

  内层循环 i=1:
    sz = 4M × 1.01 = 4.04M
    sorted_runs_[1].size = 4M,sz(4.04M) >= 4M ✓ 纳入
    TotalSize: candidate_size = 4M + 4M = 8M,candidate_count = 2

  内层循环 i=2:
    sz = 8M × 1.01 = 8.08M
    sorted_runs_[2].size = 8M,sz(8.08M) >= 8M ✓ 纳入
    TotalSize: candidate_size = 8M + 8M = 16M,candidate_count = 3

  内层循环 i=3:
    sz = 16M × 1.01 = 16.16M
    sorted_runs_[3].size = 16M,sz(16.16M) >= 16M ✓ 纳入
    TotalSize: candidate_size = 16M + 16M = 32M,candidate_count = 4

  内层循环 i=4:
    sz = 32M × 1.01 = 32.32M
    sorted_runs_[4].size = base(假设 32M),sz(32.32M) >= 32M ✓ 纳入
    candidate_count = 5

  → candidate_count(5) >= min_merge_width(2) ✓
  → 选定 [0,4] 这一段合并,产出一个新的 ~64M sorted run
```

注意 TotalSize 模式下,因为 candidate_size 累计滚雪球,几乎总能把后面的都吞进来——这就是为什么默认配置下,size ratio 触发经常一次性把几乎所有 sorted run 都合了。如果想让合并更"小步快跑",就用 SimilarSize 模式。

> **钉死这件事**:size ratio 触发的核心是"从最新方向往旧方向扫,只要累计大小 ×(1+size_ratio%) 还能盖住下一个就继续纳入"。`size_ratio` 默认 1(偏勤合并),`stop_style` 默认 TotalSize(candidate_size 累计,吞得多)。这两个旋钮决定了"相似大小文件合并"的具体行为。

---

## 五、★ size amp 触发 + sorted run num 触发:空间放大和读放大各一道护栏

size ratio 触发管"相似大小该不该并",但它有个盲区:**如果新数据源源不断地 Flush 成小 sorted run,而旧的 base 文件很大,size ratio 触发可能永远挑不到 base**(因为新文件累计大小滚不到 base 那么大)。这会导致旧版本一直堆积,空间放大失控。

为了堵这个盲区,Universal 加了第二道触发:**size amp 触发**。它直接给空间放大设硬上限。同时还有第三道:**sorted run num 触发**,给读放大设上限。这两道是 Universal 的两个安全网。

### size amp 触发:`max_size_amplification_percent` 默认 200

size amp 触发的入口是 `MaybePickSizeAmpCompaction`([`compaction_picker_universal.cc#L186-L201`](../rocksdb/db/compaction/compaction_picker_universal.cc#L186-L201)),核心函数是 `PickCompactionToReduceSizeAmp`([`compaction_picker_universal.cc#L1144-L1251`](../rocksdb/db/compaction/compaction_picker_universal.cc#L1144-L1251))。

它的逻辑用一个直觉的"空间放大"估算:**假设除了最老(base)的那个 sorted run,其他所有新文件都是空间放大**(因为它们里的 key 大部分在 base 里都有更新版本)。这个估算偏保守(高估空间放大),但简单且安全。公式([`compaction_picker_universal.cc#L1212-L1229`](../rocksdb/db/compaction/compaction_picker_universal.cc#L1212-L1229)):

```cpp
// 简化示意,非源码原文:
const uint64_t ratio = max_size_amplification_percent;  // 默认 200
const uint64_t base_sr_size = sorted_runs_[end_index].size;  // 最老 base 的大小
uint64_t candidate_size = 0;  // 所有比 base 新的 sorted run 累计大小

// 从 end_index 往新方向累加,得到 candidate_size
while (start_index > 0) { ... candidate_size += sr->compensated_file_size; ... }

// ★ 空间放大判定这一行 ★
// candidate_size * 100 < ratio * base_sr_size ?
if (candidate_size * 100 < ratio * base_sr_size) {
  // 空间放大还没超上限,不需要 size amp compaction
  return nullptr;
} else {
  // 超了!把 [start_index, end_index] 所有 sorted run 合并到 base 上
  return PickCompactionWithSortedRunRange(
      start_index, end_index, CompactionReason::kUniversalSizeAmplification);
}
```

`max_size_amplification_percent` 默认 **200**([`universal_compaction.h#L137`](../rocksdb/include/rocksdb/universal_compaction.h#L137)),注释见 [`universal_compaction.h#L38-L48`](../rocksdb/include/rocksdb/universal_compaction.h#L38-L48)。它的含义是:**100 字节的用户数据,最多占 100 + 200% = 300 字节物理空间**。换句话说,允许"新 sorted run 的总大小"最多是"base 大小"的 2 倍。

> **为什么默认是 200 不是别的**:200% 是个折中。太小(比如 25%)会让 size amp compaction 太频繁——每次都把所有新文件合到 base 上,等于退化成全量 compaction,写放大爆炸。太大(比如 1000%)又起不到限制作用,空间放大失控。200% 意味着物理空间最多是用户数据的 3 倍,对 SSD 海量写场景是个可接受的预算(TiKV 等系统实际调参经常就在 100~300 区间)。

注意 `PickCompactionToReduceSizeAmp` 还有个新特性 `incremental`([`universal_compaction.h#L107-L112`](../rocksdb/include/rocksdb/universal_compaction.h#L107-L112),EXPERIMENTAL,默认 false)。开启后,size amp compaction 不再一次性合全部,而是用滑窗找一段"扇出(fanout)最小"的部分来合,详见 `PickIncrementalForReduceSizeAmp`([`compaction_picker_universal.cc#L1253-L1486`](../rocksdb/db/compaction/compaction_picker_universal.cc#L1253-L1486))。注释说全量 size amp compaction 的 fanout 阈值是 `base_sr_size / candidate_size × 1.8`,超过这个就退回全量。这个特性是为解决"size amp compaction 一次性太大、把 compaction thread 占死"的问题,但仍 EXPERIMENTAL,本章不展开。

### sorted run num 触发:`max_read_amp` 给读放大设上限

size amp 触发管空间放大,但它有个副作用:**它只在"新文件累计大小 vs base 大小"超阈值时才触发,不直接管 sorted run 数量**。如果每个 sorted run 都很小(比如 MemTable 很小、Flush 频繁),可能 sorted run 数量爆炸(几百个)但累计大小还没超 size amp 阈值——这时读放大爆炸(一次 Get 要扫几百个 sorted run),但 size amp 触发不动作。

为了堵这个洞,Universal 有第三道触发:**sorted run num 触发**。入口是 `MaybePickCompactionToReduceSortedRuns`([`compaction_picker_universal.cc#L222-L260`](../rocksdb/db/compaction/compaction_picker_universal.cc#L222-L260)),它先调 `GetMaxNumFilesToCompactBasedOnMaxReadAmp`([`compaction_picker_universal.cc#L110-L168`](../rocksdb/db/compaction/compaction_picker_universal.cc#L110-L168))算出"该合并几个",再调 `PickCompactionToReduceSortedRuns(UINT_MAX, max_num_files_to_compact)`——注意这里第一个参数是 `UINT_MAX`,意思是**不看 size_ratio,只看数量**,从最旧方向强制合并。

`max_read_amp` 是关键旋钮(见 [`universal_compaction.h#L68-L96`](../rocksdb/include/rocksdb/universal_compaction.h#L68-L96)):

- **-1(默认)**:回退到 `level0_file_num_compaction_trigger` 当上限。
- **0**:auto-tune,根据 DB 当前大小、`size_ratio`、`write_buffer_size` 估算一个合理的 sorted run 数上限(只对 TotalSize stop_style 有效)。
- **N > 0**:sorted run 数上限就是 N。

auto-tune 的算法在 `GetMaxNumFilesToCompactBasedOnMaxReadAmp` 里([`compaction_picker_universal.cc#L132-L142`](../rocksdb/db/compaction/compaction_picker_universal.cc#L132-L142)):

```cpp
// 简化示意,非源码原文(max_read_amp=0 即 auto-tune 的逻辑):
max_num_runs = 1;
double cur_level_max_size = write_buffer_size;  // 从 MemTable 大小起步
double total_run_size = 0;
while (cur_level_max_size < max_run_size_) {
  total_run_size += cur_level_max_size;
  // 每层最大不超过"已有累计的 (1+size_ratio/100)"
  cur_level_max_size = (100.0 + ratio) / 100.0 * total_run_size;
  ++max_num_runs;
}
```

这段算法的直觉是:**从 MemTable 大小起步,假设每层是"已有累计大小的 (1+size_ratio/100)",一直滚到达到 DB 当前最大 sorted run 大小为止,滚了几层 max_num_runs 就是几**。这其实是反推"如果按 size_ratio 规则自然演化,需要几层才能容纳当前数据量"。这是个很优雅的设计——它让 sorted run 数上限和数据量自适应,不用用户手动调。

> **钉死这件事**:size amp 触发(默认 max_size_amplification_percent=200)和 sorted run num 触发(默认 max_read_amp=-1 回退到 level0_file_num_compaction_trigger)是 Universal 的两道安全网。前者堵"空间放大失控",后者堵"读放大爆炸(小文件数量爆炸)"。配合 size ratio 触发,三路共同维持 Universal 的平衡。

---

## 六、为什么 sound:写放大怎么算,为什么 SSD 适合

讲完了触发规则,现在回到最根本的问题:**Universal 凭什么把写放大压到 log 级别?这个 log 级别是怎么算出来的?**

这是本章最硬核的算账,也是为什么 Universal 适合 SSD 海量写的根本理由。

### 写放大的对数账:一个 key 被重写 log₂(N/M) 次

设数据库总数据量为 N(字节),MemTable 大小为 M(即每次 Flush 产出一个 M 大小的 sorted run)。问:一个 key 从写入到最终被合并成最大那个 base sorted run,平均要被重写几次?

在 Universal 下,合并规则是"相似大小的 sorted run 直接合并"。考虑理想情况(每次合并都把大小相近的两组并成一组):

```
初始:一串 M 大小的 sorted run,假设有 k = N/M 个
  [M][M][M][M][M][M][M][M]...   (k 个)

第一轮合并:每两个 M 并成一个 2M
  [2M][2M][2M][2M]...           (k/2 个)

第二轮合并:每两个 2M 并成一个 4M
  [4M][4M]...                   (k/4 个)

...

第 log₂(k) 轮:并成一个 N
  [N]                           (1 个)
```

一个特定的 key,在这一层层归并的过程中,**每次它所在的 sorted run 被选中合并,它就被重写一次**。在理想平衡(size-tiered 自然演化)下,一个 key 平均参与的合并轮数 ≈ **log₂(k) = log₂(N/M)**。

举个数:假设 N = 1TB,M = 64MB,那么 k = 1TB / 64MB = 16384,log₂(16384) = **14**。也就是说,一个 key 在 Universal 下平均被重写约 14 次。

对比 Level:Level 下一个 key 要被重写约**层数**次,7 层就是 ~7 次(但实测因为 L0 内部合并、Compaction 边界等常达 10~30 次)。

> **等一下,14 比 7 还大?Universal 写放大不是更小吗?**

这是最常见的误解!关键在于**这个 log 公式里的"M"**。在 Level 下,层数是固定的 ~7,但每层 target size 是 10 倍增长,所以 Level 能容纳的总数据量是 M × 10⁷ = 10⁷ × M(M 是 base level 大小)。**Level 用 7 层就能容纳 10⁷ × M 的数据,代价是层数固定 7**。

而 Universal 的层数(sorted run 数)不固定,它随数据量增长。当数据量小的时候,Universal 的 sorted run 数少,写放大比 Level 还小;当数据量大的时候,sorted run 数多,写放大接近 log₂(N/M)。

**但写放大小不小,还要看每次合并的"宽度"**。这里有个更精确的分析:

实际上,Universal 写放大的真正优势不在于"绝对次数比 Level 少",而在于:

1. **Level 的写放大是"逐层下压"——一个 key 每被下压一层就完整重写一次(读出来 + 写下去),所以是 ~层数 次**。而且 Level 的 L0→L1 合并宽度大(L0 的好几个文件 + L1 的重叠区),L1→L2 又是窄合并(只合超量部分),宽度不均。

2. **Universal 的合并是"宽度可控的相似大小合并"——每次合并的输入是大小相近的 sorted run,合并产出的大小 = 输入总和。一个 key 参与的合并次数 = 它被"卷进去"的次数**。

更准确的 Universal 写放大公式(工业界经验值)是:**WA ≈ log(N/M) / log(1 + size_ratio/100)**,但实际受 `max_size_amplification_percent` 触发的全量 compaction 影响,实测常在 **log₂(N/M) 量级**。

> **钉死这件事**:Universal 写放大的优势是"相似大小直接合并,不逐层下压"。一个 key 被重写的次数 ≈ log₂(N/M),且每次合并的宽度可控(相似大小)。Level 的写放大 ≈ 层数(固定 ~7),且每次合并宽度不均(L0→L1 宽、Ln→Ln+1 窄)。**当 N/M 远小于 2^7=128 时(M 相对 N 较大,即 MemTable 大或数据量小),Universal 写放大比 Level 小;当 N/M 很大时,两者接近,但 Universal 的合并模式更"宽且均匀"**,对 SSD 友好(顺序大块写,不频繁窄合并)。

### 为什么 SSD 海量写场景非 Universal 不可

把上面的算账落到 SSD 场景:

- SSD 的核心约束是**写入次数有限**(P/E 周期)。写放大直接 = 寿命消耗速度。
- SSD 海量写场景(TiKV、时序库、Kafka state store)的写入量是 GB/s 级别持续写入。一年累计写入 PB 级。
- 如果用 Level(写放大 ~10~30),1 PB 逻辑写 → 10~30 PB 物理写,直接吃掉 SSD 多倍寿命。
- 如果用 Universal(写放大在 N/M 合适时可压到 ~3~10,且合并模式更适合 SSD 的大块顺序写),物理写显著减少,SSD 寿命延长。

这就是为什么 **TiKV 的默认 Compaction 策略就是 Universal**(TiKV 的 RocksDB 配置里 CF 的 `compaction_style = kCompactionStyleUniversal`)。TiKV 存的是海量 KV,写入量巨大,用 Level 会把 SSD 写穿。

> **不这样会怎样**:如果 TiKV 用 Level Compaction,SSD 寿命会被写放大吃掉几倍,要么频繁换 SSD(硬件成本),要么 Compaction 跟不上导致写延迟雪崩(服务不稳定)。Universal 把写放大压下来,是 TiKV 这类"写放大敏感"系统能用 RocksDB 的前提。这正是 RocksDB 把 LevelDB 的"一种 Compaction"打开成"三种"的根本动机——给不同 workload 留出口。

### 代价诚实交代:空间放大和读放大

Universal 不是免费午餐,它的代价要诚实讲清:

1. **空间放大大**。因为 size ratio 触发只在"相似大小"时合并,旧版本(在 base 里)会留很久才被 size amp 触发合并。`max_size_amplification_percent=200` 意味着允许物理空间最多是用户数据的 3 倍。对比 Level(空间放大 ~10~30%),Universal 的空间放大显著更大。

2. **读放大大**。一次 Get 要扫的 sorted run 数 = sorted run 总数(没有 Level 那种"每层至多一个文件"的规整保证)。sorted run 数受 `max_read_amp` / `level0_file_num_compaction_trigger` 限制,但默认配置下可能有几十个。每个 sorted run 都要查 Bloom + Index + Data block,读放大显著大于 Level。这就是为什么 Universal 配 `max_sequential_skip_in_iterations` 和强 Bloom/Ribbon 尤为重要。

3. **Compaction 模式不规整**。Universal 的合并产出大小随演化而变,不规整,给缓存预热、文件管理带来挑战。

> **钉死这件事**:Universal 用空间放大(~3x)和读放大(扫多个 sorted run)换写放小(log 级)。它适合**写多读少、能接受空间放大**的场景。如果你的 workload 是读密集或空间敏感,用 Level(默认)更合适。这正是"读写放大三角"在 Compaction 策略选择上的体现。

---

## 七、技巧精解:两个最硬核的洞察

本章的技巧精解挑两个最硬核的点单独拆透:① size ratio 触发的"累计大小滚雪球"判定;② Universal 写放大的 log 账和为什么 sound。

### 洞察一:size ratio 判定的"累计滚雪球"为什么 sound

回看 size ratio 触发的核心判定([`compaction_picker_universal.cc#L983-L1010`](../rocksdb/db/compaction/compaction_picker_universal.cc#L983-L1010)):

```cpp
// TotalSize 模式(默认):
double sz = candidate_size * (100.0 + ratio) / 100.0;
if (sz < static_cast<double>(succeeding_sr->size)) break;
candidate_size += succeeding_sr->compensated_file_size;  // 累加!
```

这个判定的精妙在于 **candidate_size 是累计的**。它不是简单地比"上一个文件 vs 下一个文件",而是比"已挑中的所有文件总和 vs 下一个文件"。这意味着:

- 只要前面的累计大小够大,后面哪怕有个稍大的 sorted run 也会被吞进来。
- 这保证了挑出来的连续段是"够长"的(满足 `min_merge_width`),避免挑出只有 2 个的碎合并。
- 累计滚雪球一旦起步,会越滚越大,自然把"大小相近的一组"都吞掉,产出一个大 sorted run。

> **不这样会怎样(反面对比)**:如果判定改成"只比相邻两个文件"(朴素写法),那会出现两种坏情况。① 碎合并:每个 sorted run 都和邻居比,可能挑出一堆只有 2 个的合并,compaction 调度开销爆炸。② 漏合并:如果中间有个稍大的 sorted run,后面的小文件永远不会被挑到(因为"上一个 vs 下一个"过不去),导致它们堆积、读放大爆炸。**累计滚雪球**这个设计一举解决两个问题——它让合并自然向"大块、规整"演化,这正是 size-tiered 的精髓。

但要 sound,还得防止"滚雪球失控"(一次合并吞太多、compaction 太大卡死系统)。这就是 `max_merge_width`(默认 UINT_MAX,即不限)的用处——用户可以设小,限制一次合并最多挑几个 sorted run,防止单次 compaction 过大。源码里 `max_files_to_compact = std::min(max_merge_width, max_number_of_files_to_compact)`([`compaction_picker_universal.cc#L920-L921`](../rocksdb/db/compaction/compaction_picker_universal.cc#L920-L921))就是这道闸。

> **钉死这件事**:size ratio 触发的 sound 在于"累计滚雪球"——candidate_size 累计,让合并自然向大块演化,既避免碎合并又避免漏合并。`max_merge_width` 是防失控的闸。这两个配合,让 Universal 的合并既高效又可控。

### 洞察二:用 Level 扛 SSD 海量写会怎样(反面算账)

这是理解"为什么要 Universal"的最直接方式。我们做一个反面算账。

假设场景:TiKV 单 Region RocksDB,数据量 256MB(Region 默认大小),写入速度 100MB/s 持续。SSD 寿命 1 DWPD(每天写满一次,企业级典型)。

**用 Level Compaction**:

- 写放大 ~10(7 层 + L0 内部合并,实测常 10~30,取保守 10)。
- 100MB/s 逻辑写 → 1GB/s 物理写。
- 一天 86400 秒,物理写 = 1GB/s × 86400 = 86.4TB。
- SSD 容量假设 1TB,1 DWPD = 1TB/天。实际写了 86.4TB/天 ≈ **86 倍寿命消耗**。SSD 一天的寿命被一天吃完,~1/86 年 ≈ 4 天报废。

**用 Universal Compaction**:

- N = 256MB,M = 64MB(MemTable 默认),N/M = 4,log₂(4) = 2。写放大 ~2~3(加上 size amp 全量 compaction 偶发,实测 3~5)。
- 100MB/s 逻辑写 → 300MB/s 物理写。
- 一天物理写 = 300MB/s × 86400 = 25.9TB。
- SSD 1TB 1DWPD,实际 25.9TB/天 ≈ **26 倍寿命消耗**。~14 天报废。

(注:TiKV 实际通过多 Region 分摊、WAL 复用、compression 等进一步降低写放大,寿命可达数年。这里的算账是为了直观看写放大的倍数差。)

**结论**:同样 workload,Level 的写放大让 SSD 寿命消耗是 Universal 的 ~3 倍以上。在海量写场景,这个差距直接决定硬件成本(要不要换更贵的 SSD、要不要加更多副本分摊)。这就是为什么工业级写密集系统(TiKV、Cassandra、ScyllaDB)默认或推荐 size-tiered / Universal。

> **不这样会怎样**:如果只有 LevelDB 那种 Level Compaction,RocksDB 在 SSD 海量写场景根本没法用——SSD 寿命吃光、Compaction 跟不上。Universal 是 RocksDB 给这条退路,这是"把 LevelDB 的一种 Compaction 打开成三种"最典型的体现。

---

## 八、`compaction_options_universal` 八旋钮总账

到这里,我们已经把 Universal 的核心机制讲透了。最后用一个总账把 `CompactionOptionsUniversal` 的八个字段(加两个 EXPERIMENTAL)梳理清楚,每个字段对应前面哪一节、调它会怎样。这是本章的"调参地图",但你调参时一定要回到前面的源码理解,不要照着表盲调。

| 字段 | 默认值 | 调什么 | 调大/调小的影响 | 对应章节 |
|---|---|---|---|---|
| `size_ratio` | 1 | size ratio 触发的"相似"容忍度 | 调大:更懒合并、写放大↓、读放大↑;调小:更勤合并、写放大↑、读放大↓ | 第四节 |
| `min_merge_width` | 2 | 一次合并至少挑几个 sorted run | 调大:防止碎合并(每次至少挑 N 个);调小:更灵活 | 第四节 |
| `max_merge_width` | UINT_MAX | 一次合并至多挑几个 sorted run | 调大:单次合并可更大;调小:防单次 compaction 过大卡系统 | 第四节 |
| `max_size_amplification_percent` | 200 | size amp 触发的空间放大硬上限 | 调大:空间放大容忍度高、size amp compaction 少、写放大↓;调小:空间放大控制严、size amp compaction 频繁、写放大↑ | 第五节 |
| `compression_size_percent` | -1 | 老数据是否压缩(按占比判定) | -1:全按 compression type 配置;>0:占比超过该百分比的"老部分"不压缩(省 CPU 换空间) | (辅助) |
| `max_read_amp` | -1 | sorted run 数上限(读放大上限) | -1:回退到 level0_file_num_compaction_trigger;0:auto-tune;N:硬上限 N | 第五节 |
| `stop_style` | kCompactionStopStyleTotalSize | size ratio 触发的 candidate_size 更新方式 | TotalSize:累计滚雪球,合并多;SimilarSize:两两相似,合并少 | 第四节 |
| `allow_trivial_move` | false | 是否启用"不重叠文件直接搬移"(零写放大) | true:不重叠的 sorted run 直接移动层级,不重写(极致省写放大,但有正确性约束) | (辅助) |
| `incremental`(EXP) | false | size amp compaction 是否增量(滑窗扇出) | true:size amp compaction 不再全量,找最小扇出段合,防大 compaction 卡系统 | 第五节 |
| `reduce_file_locking`(EXP) | true | 是否在 bottom priority compaction 等待时减少文件锁 | true:减少锁冲突、改善 write stall,代价是低优先级线程负担增加 | (辅助) |

### 几个调参经验(基于源码语义,非盲调手册)

1. **写放大极致敏感**(SSD 寿命告急):`size_ratio` 调大(比如 5~10)让合并更懒、`stop_style` 用 SimilarSize 让合并更窄、`allow_trivial_move=true` 启用零写放大搬移。代价是空间放大和读放大上升,要配合强 Bloom/Ribbon 和充足的 Block Cache。

2. **空间放大敏感**(盘空间紧张):`max_size_amplification_percent` 调小(比如 25~50),让 size amp compaction 更勤。代价是写放大上升(频繁全量合并),Universal 的写放大优势会被削弱。

3. **读放大敏感**(点查延迟要求):`max_read_amp` 设个明确的小值(比如 10~20),强制 sorted run 数不超。代价是 sorted run num compaction 更频繁,写放大略升。

4. **大 DB 防 compaction 卡死**:开 `incremental=true`(EXPERIMENTAL),让 size amp compaction 不再全量。配合 `max_merge_width` 限制单次合并宽度。

5. **`level0_file_num_compaction_trigger` 在 Universal 下要单独调**!它不再是 L0 文件数阈值,而是 sorted run 总数阈值(影响 NeedsCompaction 和 max_read_amp=-1 的回退)。TiKV 等系统通常会把它调小(比如 4~10),让 Universal 勤检查。

> **钉死这件事**:Universal 的调参不是孤立拧一个个旋钮,而是在"写放大 / 空间放大 / 读放大"三角上挪点。每个旋钮动一个放大,另两个就反向动。调参前先想清楚你的 workload 在三角上想要哪个点,再回源码看每个旋钮怎么动这个点。

---

## 九、章末小结

### 回扣主线

本章是第 4 篇(Compaction)的第三站,服务**写路径**。Universal Compaction 是 RocksDB 给"写放大敏感"workload 的出口——它把 LevelDB 那种"逐层下压、写放大 ≈ 层数"的 Compaction,替换成"相似大小直接合并、写放大 ≈ log(N/M)"的 size-tiered 策略。

回到全书主线:**LevelDB 把 Compaction 写死成一种(类 Level),RocksDB 打开成三种(Level / Universal / FIFO),让你按 workload 在读写放大三角上选点**。Universal 就是那个"偏写放小、牺牲空间和读放大"的点,适合 SSD 海量写场景。

回扣二分法:本章讲的全部是**写路径**上的机制(Compaction 是写路径的收敛段)。读路径上,Universal 因为 sorted run 数多,需要配合更强的 Bloom/Ribbon(P2-08)和 Block Cache(P3-10)来压读放大——这是 Universal 的代价在读路径上的体现。

### LevelDB 写死,RocksDB 打开成旋钮

| 维度 | LevelDB(写死) | RocksDB Universal(旋钮) |
|---|---|---|
| Compaction 策略 | 只有类 Level 一种 | Universal 是三种之一(可选) |
| 合并规则 | 逐层下压,层数固定 ~7 | 相似大小直接合并,sorted run 抽象 |
| size ratio 容忍度 | 不存在(N/A) | `size_ratio`(默认 1) |
| 空间放大上限 | 不存在(由 Level 的规整层间接控制) | `max_size_amplification_percent`(默认 200) |
| sorted run 数上限 | 不存在 | `max_read_amp`(默认 -1,回退到 level0_file_num_compaction_trigger) |
| stop_style | 不存在 | TotalSize(默认) / SimilarSize |

### 五个为什么

1. **为什么 Universal 用空间放大换写放小?**——因为 SSD 海量写场景(SSD 寿命由写入次数决定),写放大直接吃寿命,是最贵的代价;而空间放大(SSD 容量)和读放大(可由 Bloom/Cache 压)相对便宜。这笔账在 SSD 海量写场景最划算。

2. **为什么 Universal 用 sorted run 而不是层当抽象?**——因为 size-tiered 的合并规则是"相似大小文件直接合并",不分 L0/L1/.../Ln 的层级。sorted run 抽象统一了"L0 文件"和"L1+ 整层",让合并规则可以一路推广。源码里 `ComputeCompactionScore` 把 L1+ 非空层也算进 `num_sorted_runs`(version_set.cc:4018-4027)就是这个抽象的铁证。

3. **为什么 size ratio 触发用"累计滚雪球"而不是"两两比较"?**——因为两两比较会导致碎合并(只挑 2 个)和漏合并(中间大文件挡路)。累计滚雪球让合并自然向大块演化,既高效又规整。详见第四节技巧精解。

4. **为什么 Universal 要配 size amp 触发和 sorted run num 触发两道安全网?**——因为 size ratio 触发有盲区:新数据源源不断时,小文件累计滚不到 base,base 里旧版本堆积(空间放大失控);或者小文件数量爆炸但累计大小没超阈值(读放大爆炸)。size amp 触发堵前者,sorted run num 触发堵后者。

5. **为什么 LevelDB 没有 Universal?**——因为 LevelDB 的设计假设是"单机、中等负载、SSD 友好度无所谓"。它不需要为 SSD 海量写优化。Universal 是 RocksDB 为工业级 SSD 海量写场景(它的主战场)加的策略,这是 LevelDB → RocksDB 演进最典型的"打开成旋钮"。

### 想继续深入往哪钻

- **想看 Universal 的完整六路触发**:读 [`db/compaction/compaction_picker_universal.cc`](../rocksdb/db/compaction/compaction_picker_universal.cc) 的 `UniversalCompactionBuilder::PickCompaction`(`#L767-L868`),按优先级链式调用六个 `MaybePick*`。本章拆了主路(二/三/四),辅助路(一 periodic / 五 delete / 六 read triggered)可对照源码注释自学。
- **想看 size-tiered 的学术背景**:搜 "Size-Tiered Compaction"(Cassandra / ScyllaDB 的 STCS),对照 RocksDB Universal 的异同。RocksDB 的 Universal 是 size-tiered 在 LSM-tree 里的具体实现,有 RocksDB 特有的 size_ratio / stop_style / max_size_amplification_percent 等细化。
- **想动手感受 Universal vs Level 的写放大差**:用 `db_bench`(附录 B)跑同一个写入 workload,分别用 `--compaction_style=level` 和 `--compaction_style=universal`,看 `rocksdb.bytes.written` 统计和 SSD 写入量的差。
- **想看 TiKV 怎么用 Universal**:读 TiKV 的 RocksDB 配置(engine_rocks),看 CF 的 `compaction_style` 默认值和 `compaction_options_universal` 的实战调参。TiKV 是 Universal 在生产的最典型案例。
- **想看 LevelDB 基线(承接锚点)**:LevelDB 只有一种类 Level 的 Compaction,见《LevelDB》Compaction 那章。本章对 LevelDB 一句带过,因为 LevelDB 没有 Universal。

### 引出下一章

我们讲完了 Level(默认,读放大小、写放大大)和 Universal(写放小、空间/读放大大)两种 Compaction。但还有一种场景没覆盖:**时序数据**——这类数据的特点是"只要最新的,旧的可以直接扔"。比如监控指标、日志,7 天前的数据没用了。对这种 workload,不管是 Level 还是 Universal 都在"努力收敛旧版本",可问题是旧版本根本不需要保留,收敛它纯属浪费。

那能不能干脆**不收敛,先进先出删旧**?这就是 FIFO Compaction 的思路——它不做合并收敛,而是当数据总量超阈值时直接删最老的文件。配合 `CompactionFilter`(在 compaction 时过滤/改 KV),还能实现 TTL、删旧版本等定制逻辑。下一章 P4-16,我们讲 FIFO Compaction 与 CompactionFilter——RocksDB 给"时序数据"的第三种 Compaction 策略。

> **下一章**:[P4-16 · FIFO Compaction 与 CompactionFilter](P4-16-FIFO-Compaction与CompactionFilter.md)
