# 第四章 · SkipList:MemTable 的骨架

> 篇:P1 写入的前台
> 主线呼应:上一章我们把 `(user_key, seq, type)` 编码成了一个可排序的 internal key,让"取最新版本"退化成"按序取第一个"。但这个排序的容器是什么?为什么不选红黑树、AVL 这些"标准平衡树"?这一章给出答案:MemTable 用跳表(SkipList),核心动机不是性能,而是**并发**——跳表能"写者加锁、读者无锁"。这是 LevelDB 全书唯一一处真正用了无锁并发的地方,也是 C++ 系统级技巧的高光。

## 核心问题

**为什么 MemTable 用跳表不用红黑树/AVL?——因为跳表天然支持"写者加锁、读者无锁"的单写多读并发。红黑树的旋转会破坏读者看到的一致性,要让读者安全就得加锁;而跳表的"新增节点 = 一次性挂一个原子指针",读者要么看到旧链、要么看到完整的新节点,绝不会看到半成品。这一条逼出了跳表,然后才是它的概率复杂度、几何分布层数这些次要的优雅。**

读完本章你会明白:

1. 为什么 MemTable 不用红黑树/AVL:平衡树的旋转会撕裂读者视图,跳表的"追加一个新节点"天然兼容并发读。
2. 跳表的概率结构怎么工作:每个节点有 `height` 层 next 指针,LevelDB 用 `kBranching = 4`(每层以 1/4 概率提升)生成几何分布的高度,期望高度 $O(\log n)$,查找/插入/迭代都 $O(\log n)$。
3. LevelDB 的 `Node` 怎么用 `std::atomic<Node*> next_[1]` 这个**尾随数组**(flexible array)承载多层指针,怎么用 placement new + Arena 把节点塞进内存。
4. **单写多读的无锁读凭什么不会数据竞争**:写者先 `new` 完整节点(各层 next 填好)再用 `release` store 挂到上一层,读者用 `acquire` load 读 next——release/acquire 建立同步,读者要么看到旧链、要么看到完整挂好的新节点,**绝不会看到半成品**。这是全书 C++ 技巧的高光。
5. "一把大锁换写侧简单 + 无锁换读侧吞吐"的精妙配合:MemTable 之所以能这么搞,是因为 LevelDB 用一把 `mutex_` 保证**同一时刻只有一个写者在 Insert**(见 P1-06 写组),读者则完全无锁。

> **如果一读觉得太难**:先只记住三件事——① MemTable 用跳表不用红黑树,核心动机是"读者无锁",红黑树的旋转会让读者看到中间态,跳表不会;② 跳表的每个节点有多层 next 指针,层数按 1/4 概率几何分布,期望高度 $O(\log n)$;③ `Node::next_` 是 `std::atomic<Node*>`,写者用 `release` store 挂新节点,读者用 `acquire` load 读,release/acquire 配对保证读者看不到半成品。剩下的内存序、`NoBarrier_*`、`max_height_` 这些细节,可以回头再读。

---

## 4.1 一句话点破

> **MemTable 用跳表不用红黑树,因为跳表"新增一个节点 = 在已有链上挂一个原子指针",读者无锁也不会撕裂;而红黑树插入要旋转、会改动多个已有节点,读者不加锁就会看到中间态。所以跳表胜出的不是性能,是并发友好——这是它被选为 MemTable 骨架的全部理由。**

这是结论,不是理由。本章倒过来拆:先看红黑树到底卡在哪儿,再看跳表怎么用一个简单的概率结构把 $O(\log n)$ 复杂度做出来,然后钻进 LevelDB 的 `Node` 结构和内存序,把"读者无锁凭什么 sound"这件事钉死。

---

## 4.2 为什么不用红黑树/AVL:并发是真正的痛点

### 提出问题

MemTable 是一个**内存中的有序容器**,要支持:

- **插入**:`MemTable::Add` 把 internal key 插进去。
- **查找**:`MemTable::Get` 给一个 key,Seek 到 >= 它的位置。
- **迭代**:读路径上 `MemTableIterator` 要按序遍历。

这听起来就是红黑树、AVL 树的标准用武之地——它们都是 $O(\log n)$ 的有序映射,工业实现成熟。**为什么 LevelDB 偏偏选了跳表?**

答案和"单条操作的性能"几乎无关。跳表、红黑树、AVL 在期望/均摊意义下都是 $O(\log n)$,常数差异可以忽略。真正的分野在**并发**。

### 不这样会怎样

LevelDB 的 MemTable **同时被读和写**:

- **写**:用户的 `Put` 把 internal key 插进 MemTable。
- **读**:用户的 `Get`,以及读路径上的 Iterator,要在 MemTable 里 Seek、Next。

如果 MemTable 用红黑树,会发生什么?

红黑树插入一个节点,要做**再平衡**(rebalancing):沿着插入路径往上,可能要做若干次**旋转**(left-rotate / right-rotate)和**重新染色**。一次旋转会改动**两个以上已有节点**的左/右孩子指针——比如一次左旋,会让节点 X 的右孩子变成它原来右孩子的左孩子,同时 X 的父节点要指向新的子树根。这些指针改动至少有**两到三处**,而且**它们必须"同时"完成**,否则树就处于一个不合法的中间态(某些节点从两个父亲指向它,或某些节点丢失)。

现在,想象一个读者在这个旋转进行的**中途**来读这棵树。它可能:

- 看到旧的父亲指针(指向旧的子树根),走进了即将被丢弃的子树。
- 看到新的父亲指针,但旧的孩子指针还没改完,跟着走到一个错位的节点。
- 看到一个内部节点的两个孩子指针指向同一个孩子(被旋转引用两次),导致循环。

**所有这些都是"撕裂读"(torn read)**——读者看到的是树的一个**永不存在的中间状态**。

> **反面对比(红黑树 + 无锁读者)**:要让红黑树的读者安全,常见做法是给整棵树加一把**读写锁**(`std::shared_mutex` 之类)。读者拿读锁(共享),写者拿写锁(独占)。但这意味着——**只要有一个写者在写,所有读者都得阻塞等写者完成**。在 LevelDB 里,MemTable 是热路径,读和写都非常频繁(写组、Get、Compaction 触发时的扫描都要访问 MemTable),让写阻塞所有读,前台吞吐直接崩。
>
> 还有一种更激进的做法是"手写无锁红黑树"(用 CAS 一层层改指针),但红黑树的旋转涉及多处改动,把旋转改成无锁 CAS 序列是出了名的难——学术界有几篇论文做这事,工业实现几乎没有。RocksDB 早期也没走这条路,而是和 LevelDB 一样用跳表。

跳表为什么赢?因为它的写**只新增一个节点、改 $O(\log n)$ 个前驱的 next 指针,而且每个 next 指针的改动是独立的原子写**。读者沿着 next 指针走,要么看到旧版本、要么看到新版本,**中间态不存在**。这就是跳表在并发上的天然优势。

> **钉死这件事**:MemTable 选跳表,**核心动机是并发友好,不是性能**。跳表插入只产生一个新节点 + $O(\log n)$ 个独立的 next 指针原子改;读者无锁也能看到一致视图。红黑树插入要旋转多处、撕裂读者视图,要安全就得读写锁,写阻塞所有读。这条动机搞不清,就会以为跳表只是"用概率换平衡树",错过它真正的价值。

---

## 4.3 跳表是什么:多层链表 + 几何分布高度

### 提出问题

跳表的原始论文(William Pugh, 1990)的标题就是 "Skip Lists: A Probabilistic Alternative to Balanced Trees"。它的思路:**既然维护一棵平衡树要旋转、要并发麻烦,那能不能用"概率"代替"平衡",换一种更简单的有序结构?**

### 跳表的结构

跳表就是一棵**多层的有序链表**。最底层(level 0)是一条完整的有序链表,包含所有节点;上面每一层都是下面一层的"稀疏索引",每个节点以某个概率"提升"到上一层。

ASCII 框图(一个高度 4 的跳表,5 个节点):

```
level 3:  head ----------------------------------------------> e -> nullptr
level 2:  head ----------------------> c --------------------> e -> nullptr
level 1:  head ------> a ------------> c ------> e -----------> nullptr
level 0:  head -> a -> b -> c -> d -> e -> f -> nullptr

  ↑每层是下层的一个"稀疏抽取";节点 a 高度 2,c/e 高度 3,其余高度 1
  ↑查找时从高层起,沿 next 走;遇到比 target 大的就下降一层
```

**查找 key x**:从最高层开始,沿 next 走,如果下一个节点 key 仍 < x,继续往前;否则下降一层。直到落到 level 0,这时所在节点的下一个节点就是 >= x 的位置。

```
查找 d:head --(level 3)--> e 不行(e > d),降到 level 2
       head --(level 2)--> c < d,走到 c
       c ----(level 2)--> e > d,降到 level 1
       c ----(level 1)--> e > d,降到 level 0
       c ----(level 0)--> d,找到
```

**插入 key x**:先按上面的方法找到每层的"前驱"(`prev[level]`),然后 `new` 一个新节点(高度由概率分布决定),在每一层把它插进 prev 和 next 之间。

复杂度取决于跳表的"高度分布"——也就是每个节点有多大概率长到第 $k$ 层。

### 4.3.1 几何分布高度:为什么 $O(\log n)$

跳表让每个节点的高度服从**几何分布**:每个节点高度至少为 1,有 $1/p$ 的概率多长一层($p$ 是"提升概率")。LevelDB 选 $p = 1/4$。

看 LevelDB 的 `RandomHeight`([db/skiplist.h:240-250](../leveldb/db/skiplist.h#L240-L250)):

```cpp
template <typename Key, class Comparator>
int SkipList<Key, Comparator>::RandomHeight() {
  // Increase height with probability 1 in kBranching
  static const unsigned int kBranching = 4;
  int height = 1;
  while (height < kMaxHeight && rnd_.OneIn(kBranching)) {
    height++;
  }
  assert(height > 0);
  assert(height <= kMaxHeight);
  return height;
}
```

`rnd_.OneIn(kBranching)` 来自 `util/random.h:51-53`:

```cpp
// Randomly returns true ~"1/n" of the time, and false otherwise.
// REQUIRES: n > 0
bool OneIn(int n) { return (Next() % n) == 0; }
```

含义直白:**以 $1/4$ 概率返回 true**。`RandomHeight` 一直循环,每轮以 $1/4$ 概率"加一层",直到失败或撞 `kMaxHeight = 12` 上限。

所以高度分布是:

| 高度 h | 概率 |
|-------|------|
| 1 | $3/4$ |
| 2 | $(1/4) \cdot (3/4)$ |
| 3 | $(1/4)^2 \cdot (3/4)$ |
| ... | ... |
| h | $(1/4)^{h-1} \cdot (3/4)$ |

期望高度 $E[h] = \sum_{h \geq 1} h \cdot P(h) = 1 / (1 - 1/4) = 4/3 \approx 1.33$。**每个节点平均只占 1.33 个 next 指针**,空间开销极小。

那查找复杂度呢?直观上,高层链表节点稀疏(几何分布),每个高层节点能"跳过"一大段底层链表。形式化地分析:在 level $k$ 上,相邻两个节点的底层距离期望是 $1/p = 4$ 个底层节点;从最高层往下走,每层期望前进 $O(1)$ 步(因为"下一个"以 $3/4$ 概率超过 target 让你下降,以 $1/4$ 概率继续走),总层数 $O(\log_{1/p} n) = O(\log n)$。所以查找、插入、删除都是 $O(\log n)$ 期望。

> **钉死这件事**:LevelDB 的跳表用 `kBranching = 4`,即每个节点以 $1/4$ 概率提升一层。这给出几何分布的高度,期望高度 $4/3$、期望查找/插入复杂度 $O(\log n)$。`kMaxHeight = 12` 是个保守上限($n \leq 4^{12} \approx 1600$ 万,MemTable 4MB 不会触顶)。$p = 1/4$ 比 Pugh 论文建议的 $p = 1/2$ 更保守,换更少的空间开销(每节点 1.33 指针 vs 2 指针),代价是稍高的常数因子。

### 4.3.2 为什么不用更复杂的概率结构

跳表的优雅在于**简单**:

- 不需要 B-tree 那种"分裂/合并"的复杂操作。
- 不需要红黑树那种"染色 + 旋转"的不变量维护。
- 插入就是"找位置 + new 节点 + 改 $O(\log n)$ 个 prev 指针"。

每个 next 指针的改动是**独立的、原子的**(`std::atomic<Node*>` 的 store),这为无锁读铺好了路——下一节专讲这件事。

---

## 4.4 Node 结构:尾随数组 + Arena 分配

### 提出问题

跳表的节点要有"height 个 next 指针"——但 height 是运行时才知道的,怎么在 C++ 里高效存这种"变长数组"?

### 不这样会怎样

朴素的两种做法:

1. **`std::vector<std::atomic<Node*>> next_`**:每个节点都带一个 vector,堆分配 + 容量管理 + 析构——慢,且每节点至少多 24 字节(vector 三指针)。
2. **固定 `Node* next_[kMaxHeight]`**:每个节点都开 12 个指针槽,但绝大多数节点高度是 1 或 2,12 个槽里 10 个浪费。

### 所以这样设计

LevelDB 用 C 风格的**尾随数组**(flexible array member)——`std::atomic<Node*> next_[1]`。看 [`db/skiplist.h:143-177`](../leveldb/db/skiplist.h#L143-L177):

```cpp
template <typename Key, class Comparator>
struct SkipList<Key, Comparator>::Node {
  explicit Node(const Key& k) : key(k) {}

  Key const key;

  // Accessors/mutators for links.  Wrapped in methods so we can
  // add the appropriate barriers as necessary.
  Node* Next(int n) {
    assert(n >= 0);
    // Use an 'acquire load' so that we observe a fully initialized
    // version of the returned Node.
    return next_[n].load(std::memory_order_acquire);
  }
  void SetNext(int n, Node* x) {
    assert(n >= 0);
    // Use a 'release store' so that anybody who reads through this
    // pointer observes a fully initialized version of the inserted node.
    next_[n].store(x, std::memory_order_release);
  }

  // No-barrier variants that can be safely used in a few locations.
  Node* NoBarrier_Next(int n) {
    assert(n >= 0);
    return next_[n].load(std::memory_order_relaxed);
  }
  void NoBarrier_SetNext(int n, Node* x) {
    assert(n >= 0);
    next_[n].store(x, std::memory_order_relaxed);
  }

 private:
  // Array of length equal to the node height.  next_[0] is lowest level link.
  std::atomic<Node*> next_[1];
};
```

注意两点:

1. **`next_[1]` 是个尾随数组**——声明 `[1]` 只是 C++ 的语法占位,真正分配时,`NewNode` 会**多分配 `height - 1` 个 `std::atomic<Node*>` 槽位**,把它们接在 `Node` 主体后面。这样每个节点正好用 height 个槽,无浪费。
2. **`Next` / `SetNext` 用 `acquire` load / `release` store**;**`NoBarrier_Next` / `NoBarrier_SetNext` 用 `relaxed`**。这两套 API 是无锁读的核心,下一节专讲。

`NewNode` 的实现([db/skiplist.h:179-185](../leveldb/db/skiplist.h#L179-L185)):

```cpp
template <typename Key, class Comparator>
typename SkipList<Key, Comparator>::Node* SkipList<Key, Comparator>::NewNode(
    const Key& key, int height) {
  char* const node_memory = arena_->AllocateAligned(
      sizeof(Node) + sizeof(std::atomic<Node*>) * (height - 1));
  return new (node_memory) Node(key);
}
```

直球解读:

1. **`sizeof(Node) + sizeof(std::atomic<Node*>) * (height - 1)`**:Node 主体(包含 key 和 next_[1] 的第一个槽)+ 额外 height - 1 个槽。注意 `next_[1]` 已经有 1 个槽了,所以额外要 height - 1 个。
2. **`arena_->AllocateAligned(...)`**:从 Arena 分配,且要求对齐(8 字节,64 位系统是指针大小)。**对齐对 `std::atomic` 的访问是必要的**——某些平台上的原子操作要求地址对齐,不对齐会撕裂或 trap。Arena 怎么做对齐,下一章 P1-05 详讲。
3. **`new (node_memory) Node(key)`**:placement new——在已分配的内存上构造 Node,不另分配。Node 构造函数只初始化 `key` 字段,`next_` 数组的各槽**还没初始化**——它们由 `Insert` 在挂载前显式 `SetNext` / `NoBarrier_SetNext`。

字节布局(一个高度 3 的 Node):

```
   ┌──────────────────┬──────────┬──────────────┬──────────────┬──────────────┐
   │  Key const key   │ atomic   │ atomic       │ atomic       │  (高度 3,    │
   │                  │ next_[0] │ next_[1]     │ next_[2]     │   额外 2 槽) │
   └──────────────────┴──────────┴──────────────┴──────────────┴──────────────┘
    ↑ sizeof(Node) 主体               ↑ 尾随数组,长度 = height
                                       (声明 [1],实际分配 height 个槽)
```

> **钉死这件事**:LevelDB 的 `Node` 用 C 风格尾随数组 `std::atomic<Node*> next_[1]`,每个节点正好用 height 个槽,无浪费。`NewNode` 用 placement new 在 Arena 对齐内存上构造,不另分配。这是 C 系统编程的典型技巧——在 C++ 里用 `std::variant`/`std::any`/`vector` 这些高开销容器都做不到这么省。

---

## 4.5 写者路径:Insert 怎么挂上去

### 提出问题

现在节点结构清楚了,我们看写者怎么把一个新 key 挂进跳表。这一步是无锁读的前置——读者要 sound,前提是写者**先把节点构造完整,再发布**。

### 真实源码

`Insert` 全文([db/skiplist.h:334-366](../leveldb/db/skiplist.h#L334-L366)):

```cpp
template <typename Key, class Comparator>
void SkipList<Key, Comparator>::Insert(const Key& key) {
  // TODO(opt): We can use a barrier-free variant of FindGreaterOrEqual()
  // here since Insert() is externally synchronized.
  Node* prev[kMaxHeight];
  Node* x = FindGreaterOrEqual(key, prev);

  // Our data structure does not allow duplicate insertion
  assert(x == nullptr || !Equal(key, x->key));

  int height = RandomHeight();
  if (height > GetMaxHeight()) {
    for (int i = GetMaxHeight(); i < height; i++) {
      prev[i] = head_;
    }
    // It is ok to mutate max_height_ without any synchronization
    // with concurrent readers.  A concurrent reader that observes
    // the new value of max_height_ will see either the old value of
    // new level pointers from head_ (nullptr), or a new value set in
    // the loop below.  In the former case the reader will
    // immediately drop to the next level since nullptr sorts after all
    // keys.  In the latter case the reader will use the new node.
    max_height_.store(height, std::memory_order_relaxed);
  }

  x = NewNode(key, height);
  for (int i = 0; i < height; i++) {
    // NoBarrier_SetNext() suffices since we will add a barrier when
    // we publish a pointer to "x" in prev[i].
    x->NoBarrier_SetNext(i, prev[i]->NoBarrier_Next(i));
    prev[i]->SetNext(i, x);
  }
}
```

逐步拆:

1. **`FindGreaterOrEqual(key, prev)`**:找到 >= key 的位置,同时把每层的前驱填进 `prev[]`。这一步是读操作,用 `acquire` load 读 next。
2. **`RandomHeight()`**:决定新节点高度。
3. **如果新高度 > 当前 `max_height_`**:把 head_ 填进多出来的那几层 prev(`head_->Next(i) == nullptr`),然后用 `relaxed` 把 `max_height_` 更新。注释解释为什么这里**不需要同步**——读者看到新的 max_height_ 后,要么看到 head_ 那些层的旧值 `nullptr`(立刻下降),要么看到新挂的节点,都 sound。这是设计上的妙处。
4. **`x = NewNode(key, height)`**:placement new 出新节点。
5. **核心挂载循环**:
   ```cpp
   x->NoBarrier_SetNext(i, prev[i]->NoBarrier_Next(i));   // 新节点的 next = 前驱的旧 next
   prev[i]->SetNext(i, x);                                // 前驱的 next = 新节点(release 发布)
   ```

**这两行的次序极重要**:**先把新节点的 next 设好,再把前驱的 next 指向新节点**。中间这一步——`NoBarrier_SetNext` 把新节点的 next_ 字段填好——发生在新节点**还没挂进任何链表之前**。然后 `prev[i]->SetNext(i, x)`(release store)是**真正的"发布"**——这一刻之后,任何走 prev[i] 链的读者都能看到 x。

> **钉死这件事**:`Insert` 的关键次序是"**先构造完节点内容,再发布**"。新节点 x 的 next 字段在它被前驱指向之前就已填好;前驱改 next 用 `release` store,这一刻是发布点。读者通过 `acquire` load 读 prev[i] 的 next,看到 x 时,x 的内容(包括 x 自己的 next)已经完整可见——这是无锁读不会撕裂的根。下一节专拆这件事。

---

## 4.6 读者路径:FindGreaterOrEqual 怎么无锁走

### 真实源码

`FindGreaterOrEqual`([db/skiplist.h:258-279](../leveldb/db/skiplist.h#L258-L279)):

```cpp
template <typename Key, class Comparator>
typename SkipList<Key, Comparator>::Node*
SkipList<Key, Comparator>::FindGreaterOrEqual(const Key& key,
                                              Node** prev) const {
  Node* x = head_;
  int level = GetMaxHeight() - 1;
  while (true) {
    Node* next = x->Next(level);                    // ← acquire load 读 next
    if (KeyIsAfterNode(key, next)) {
      // Keep searching in this list
      x = next;
    } else {
      if (prev != nullptr) prev[level] = x;
      if (level == 0) {
        return next;
      } else {
        // Switch to next list
        level--;
      }
    }
  }
}
```

逻辑直白:从最高层起,沿 `x->Next(level)` 走(acquire load),如果 next 比 key 小就前进一步,否则下降一层。落到 level 0 时,所在位置就是 >= key 的第一个节点。

注意:**整条路径上,读者没有任何锁**。它只通过 `Next(level)` 这个 acquire load 读 next 指针。下一节我们钉死:为什么这不会读到撕裂的中间态。

---

## 4.7 技巧精解:单写多读的无锁读,凭什么不会数据竞争

这一节是全书 C++ 技巧的高光。我们把"读者无锁凭什么 sound"拆到最深。

### 这个技巧在做什么

让 SkipList 支持**单写多读**(single-writer, multi-reader)的并发访问:**写者**调 `Insert`,无任何锁;**读者**调 `FindGreaterOrEqual` / `FindLessThan` / `FindLast` / `Iterator::Seek` 等,**完全不加锁**。读者不会读到撕裂的中间态、不会数据竞争(C++ 标准意义上的 data race,即 UB)。

### 用了什么手段

核心是三层:

1. **`std::atomic<Node*>`**:`Node::next_` 数组里每个槽都是 `std::atomic<Node*>`,读写都走原子操作,不会撕裂(单个指针的读写本身在主流平台上是不可分割的,但 C++ 标准要求 atomic 才能排除 data race)。
2. **`acquire` load + `release` store 配对**:读者用 `Next(n)`(acquire load),写者用 `SetNext(n, x)`(release store)。这对内存序在 C++ 内存模型里建立 happens-before 关系,保证"写者发布前的所有写入"对"看到发布的读者"全部可见。
3. **不变量守恒**:节点一旦挂进链表,`key` 字段不变(`Key const key`),next 字段只能由写者(单一写者,外部同步)改。读者看到的 next 永远指向"曾经存在过的合法节点",不会 use-after-free(节点从不删除,见 [db/skiplist.h:18-21](../leveldb/db/skiplist.h#L18-L21) 的不变量(1))。

### 为什么 sound:三步证明

#### 第一步:写者"先构造、再发布"

看 `Insert` 的核心挂载循环:

```cpp
x = NewNode(key, height);
for (int i = 0; i < height; i++) {
  x->NoBarrier_SetNext(i, prev[i]->NoBarrier_Next(i));   // ① 用 relaxed 设新节点的 next
  prev[i]->SetNext(i, x);                                 // ② 用 release 把新节点挂进链
}
```

**第 ① 步**,`NoBarrier_SetNext` 用 `relaxed`——这一步只填**新节点 x 自己的 next 字段**。此时 x 还没被任何前驱指向,任何读者都不会走到 x。所以 x 的 next 字段的初始化,**只有写者自己看得到**,无并发访问,relaxed 就够。

**第 ② 步**,`prev[i]->SetNext(i, x)` 用 `release`——这一步是**发布**。C++ 内存模型规定:**release store 之前的所有写入(包括 x 的 next 字段初始化、x 的 key 构造),在任何随后通过同一原子变量 acquire load 看到这次 store 的读者那里,全部可见,且不被重排到 store 之后**。

#### 第二步:读者 acquire load 看到完整节点

读者走 `x->Next(level)`,即 `next_[n].load(acquire)`。C++ 内存模型规定:**如果一次 acquire load 看到了一次 release store 写入的值,那么 store 之前的所有写入(在 store 那个线程里),对 load 这个线程全部可见**。

应用到跳表:

- 写者:`NewNode` 构造 x(填好 key、next)→ `prev[i]->SetNext(i, x)`(release 发布)。
- 读者:走 prev 链到 prev[i],`prev[i]->Next(i)`(acquire load),如果看到的是 x,那么——**x 的所有字段(key、各层 next)对读者全部可见**。

所以读者看到 x 时,x 已经是一个**完整初始化的节点**,绝不会是"next 字段还没填的半成品"。

#### 第三步:中间态不存在的数学保证

读者要么看到 prev[i] 的旧 next(指向 x 之前),走过去看不到 x;要么看到新 next(指向 x),走过去看到完整的 x。**不会有第三种状态**——因为 release store 是原子的,acquire load 看到的要么是旧值、要么是新值,**不存在中间值**。

这一点延伸到读路径全程:读者从 head_ 开始,逐层 acquire load,每一步要么走旧链、要么走新链。即便有多个写者插入(虽然 LevelDB 串行化到单写者,但即便放宽),读者最坏看到的是"某次插入已完成、某次还没开始"的某个一致快照——这就是"无锁读"的精确语义。

### 反面对比 1:朴素地用普通 `Node*`

```cpp
// 反面代码(数据竞争 + UB):
struct Node {
  Key key;
  Node* next_[1];   // 普通指针,非原子
};
```

读者 `x->next_[n]`、写者 `x->next_[n] = y` 同时发生——C++ 标准定义为 **data race**,程序行为**未定义**(UB)。在主流 x86 平台上,单指针读写虽然硬件层面原子,但:

1. **编译器可以重排**写者的 next 赋值和 key 构造(它看不到跨线程依赖),把"先填 key 后挂 next"重排成"先挂 next 后填 key"——读者就走上了看到半成品的道路。
2. **UB 就是 UB**,任何平台、任何编译器版本都不能担保正确。LevelDB 是要跨平台、长期维护的工业代码,不能依赖 UB。

### 反面对比 2:用 `relaxed` 内存序

```cpp
// 反面代码(relaxed 不能建立同步):
Node* Next(int n) { return next_[n].load(relaxed); }
void SetNext(int n, Node* x) { next_[n].store(x, relaxed); }
```

relaxed 原子操作**保证单变量本身的原子性**(不撕裂),但**不建立 happens-before**。所以写者的"NewNode 构造 x → prev[i]->SetNext(i, x)"这两步可以被 CPU 或编译器重排成任意次序——读者通过 acquire 之外的内存序看到 x 时,不能保证 x 的内容已构造完。

> **一个常见误区**:很多读者以为"x86 是强内存序(TSO),所以 relaxed 就够了"。**这是错的**。首先,LevelDB 要跨架构(ARM、PowerPC 都是弱内存序,relaxed 会撕裂);其次,即便 x86 的 TSO 也**不阻止编译器重排**——编译器看不到 relaxed 操作之间的依赖,完全可以把"写 key"和"写 next"重排。C++ 内存模型要求**至少 release/acquire** 才能跨线程建立 happens-before,这是标准层面的保证,不依赖任何具体 CPU。

### 反面对比 3:用一把大锁包住读写

```cpp
// 反面代码(吞吐崩):
Status MemTable::Add(...) {
  std::lock_guard<std::mutex> g(mu_);
  table_.Insert(...);
}
bool MemTable::Get(...) {
  std::lock_guard<std::mutex> g(mu_);
  // ... Seek、Next ...
}
```

正确,但:**只要有一个写者在 `Add`,所有读者的 `Get` 都阻塞**。在 LevelDB 里,MemTable 是写路径的落点(每次 Put 都插)、读路径的第一站(每次 Get 都查)。让写阻塞所有读,前台 Get 延迟直接被 Put 频率拉高,P99 难看。

LevelDB 的实际做法是"**一把大锁换写侧简单 + 无锁换读侧吞吐**":

- **写侧**:LevelDB 在 `DBImpl` 里用一把 `mutex_` 串行化所有写者(P1-06 写组详讲)。也就是说,**同一时刻只有一个写者在调 `SkipList::Insert`**。所以 SkipList 内部不需要再上锁——写写互斥已经被外部担保。注释明说([db/skiplist.h:11](../leveldb/db/skiplist.h#L11)):"Writes require external synchronization, most likely a mutex."
- **读侧**:读者完全无锁。读者之间的并发、读者与写者的并发,完全靠 `acquire/release` 内存序解决。

这是一个**精妙的分工**:外部锁解决"写写"互斥(简单的串行化),原子操作解决"读写"互斥(无锁地让读不阻塞写)。这是 LevelDB 全书唯一一处真正用了无锁并发的地方,被反复印证为"一把大锁 + 无锁读"的配合典范。

### sound 性的活证据:skiplist_test.cc 的并发测试

光看源码不够,要有测试佐证。`db/skiplist_test.cc` 的 `ConcurrentTest`(行 [152](../leveldb/db/skiplist_test.cc#L152) 起)正是一个写者 + 多读者的并发正确性验证。

关键代码 `WriteStep` 和 `ReadStep`([db/skiplist_test.cc:217-281](../leveldb/db/skiplist_test.cc#L217-L281)):

```cpp
// REQUIRES: External synchronization
void WriteStep(Random* rnd) {
  const uint32_t k = rnd->Next() % K;
  const intptr_t g = current_.Get(k) + 1;
  const Key key = MakeKey(k, g);
  list_.Insert(key);
  current_.Set(k, g);
}

void ReadStep(Random* rnd) {
  // Remember the initial committed state of the skiplist.
  State initial_state;
  for (int k = 0; k < K; k++) {
    initial_state.Set(k, current_.Get(k));
  }
  Key pos = RandomTarget(rnd);
  SkipList<Key, Comparator>::Iterator iter(&list_);
  iter.Seek(pos);
  while (true) {
    // ... 逐节点验证 iter 看到的状态在 initial_state 之后才被插入 ...
    ASSERT_LE(pos, current) << "should not go backwards";
    // ...
  }
}
```

注意几点:

1. **`WriteStep` 的注释 `REQUIRES: External synchronization`**:外部(主线程)担保写写互斥——这正是 LevelDB 用 `mutex_` 做的事。
2. **`ReadStep` 无任何锁**:直接 `iter.Seek`、`iter.Next`,内部全是 acquire load。
3. **`ASSERT_LE(pos, current) << "should not go backwards"`**:读者验证自己**不会读到回退**(中间态的标志之一)。如果无锁读会撕裂,这个 assert 会在多线程并发下偶发失败;`RunConcurrent` 跑 5 轮,每轮 1000 次迭代([db/skiplist_test.cc:342-366](../leveldb/db/skiplist_test.cc#L342-L366)),长期不挂——这就是 sound 性的活证据。

```cpp
// db/skiplist_test.cc:330-340 —— 读者线程
static void ConcurrentReader(void* arg) {
  TestState* state = reinterpret_cast<TestState*>(arg);
  Random rnd(state->seed_);
  int64_t reads = 0;
  state->Change(TestState::RUNNING);
  while (!state->quit_flag_.load(std::memory_order_acquire)) {
    state->t_.ReadStep(&rnd);    // ← 无锁读,反复跑
    ++reads;
  }
  state->Change(TestState::DONE);
}
```

这套测试,用真实的多线程(通过 `Env::Default()->Schedule` 起线程,见 [skiplist_test.cc:352](../leveldb/db/skiplist_test.cc#L352))反复验证:写者持续 Insert、读者持续 Seek/Next,**两边都 sound**。这是"无锁读不会撕裂"在工程层面的最终证明。

### 小结:三句话钉死

1. **`std::atomic<Node*>`** 排除单变量撕裂(C++ 层面不 UB)。
2. **release store / acquire load** 建立 happens-before,保证"先构造后发布"的次序对读者可见(读者看到 x 时,x 已完整初始化)。
3. **不变量"节点永不删除、key 永不改变"** 排除 use-after-free 和悬垂读,读者看到的 next 永远指向合法节点。

这三层叠加,`SkipList::Insert`(写者)+ `FindGreaterOrEqual`(读者)构成的无锁并发就是 sound 的。这是 LevelDB 全书最深的 C++ 技巧。

> **钉死这件事**:无锁读的 sound 性根植于 C++ 内存模型。`std::atomic` 排除单变量撕裂;`release/acquire` 排除跨变量的"半成品可见";不变量"节点不删、key 不变"排除 use-after-free。三条缺一不可。很多人看 SkipList 源码只注意"`std::atomic<Node*>` 不撕裂",错过 release/acquire 的 happens-before 才是核心——那一层才是"读者看不到半成品"的真正担保。

---

## 4.8 一个完整的插入例子(把全章串起来)

假设当前跳表有 key {1, 3, 5, 7, 9},高度分布如下(节点 1 高 1,3 高 2,5 高 3,7 高 2,9 高 1),`max_height_ = 3`:

```
head_ -> [level 2]------------------> 5 ----------> nullptr
head_ -> [level 1]--------> 3 ------> 5 ---> 7 ----> nullptr
head_ -> [level 0]> 1 ---> 3 -> 5 -> 6?  7 -> 9 ---> nullptr
                                       ↑
                                       要插入 key 6,height 2
```

**步骤 1:`FindGreaterOrEqual(6, prev)`**(读者路径):

```
level 2: head -> 5 < 6? 是,x=5
         5 -> nullptr,nullptr 不 < 6,下降到 level 1,prev[2]=5
level 1: 5 -> 7,7 > 6,下降到 level 0,prev[1]=5
level 0: 5 -> 7,7 > 6,返回 7,prev[0]=5
```

**步骤 2:`RandomHeight()` 返回 2**(假设)。

**步骤 3:height 2 <= max_height_ 3,无需调整 max_height_**。

**步骤 4:`NewNode(6, 2)`**:Arena 分配 `sizeof(Node) + sizeof(atomic<Node*>) * 1` 字节,placement new 构造。

**步骤 5:挂载循环**(i = 0, 1):

```cpp
i = 0:
  x->NoBarrier_SetNext(0, prev[0]->NoBarrier_Next(0));   // x.next[0] = 5 的旧 next = 7
  prev[0]->SetNext(0, x);                                 // 5.next[0] = x  (release 发布)
i = 1:
  x->NoBarrier_SetNext(1, prev[1]->NoBarrier_Next(1));   // x.next[1] = 5 的旧 next = 7
  prev[1]->SetNext(1, x);                                 // 5.next[1] = x  (release 发布)
```

**结果**:

```
head_ -> [level 2]------------------> 5 ----------------> nullptr
head_ -> [level 1]--------> 3 ------> 5 --> 6 ---> 7 ----> nullptr
head_ -> [level 0]> 1 ---> 3 -> 5 -> 6 -> 7 -> 9 -------> nullptr
```

在这个过程中,**并发读者的视图**:

- 在 `prev[0]->SetNext(0, x)`(release)之前:读者走 prev 链到 5,`Next(0)` 看到 7,继续走。看不到 x。
- 在 release store 之后:读者 `Next(0)` 通过 acquire load 看到 x——**x 的 next[0] = 7 已可见**(因为 release/acquire),读者走 x 的 next[0] 到 7,继续。

读者从不看到"5.next = x 但 x.next 未填"的中间态——这就是 sound 性的具体体现。

---

## 4.9 关于 prev 数组和 Prev() 的一个细节

SkipList 的 `Iterator::Prev()` 是"反向走",但跳表只有 next 指针、没有 prev 指针。LevelDB 的实现是**用 FindLessThan 重新搜索**([db/skiplist.h:210-219](../leveldb/db/skiplist.h#L210-L219)):

```cpp
template <typename Key, class Comparator>
inline void SkipList<Key, Comparator>::Iterator::Prev() {
  // Instead of using explicit "prev" links, we just search for the
  // last node that falls before key.
  assert(Valid());
  node_ = list_->FindLessThan(node_->key);
  if (node_ == list_->head_) {
    node_ = nullptr;
  }
}
```

注释直说:"Instead of using explicit 'prev' links, we just search for the last node that falls before key." —— **没有 prev 链,反向走就是 $O(\log n)$ 重新搜**。

> **为什么这么"慢"也接受**:MemTable 的读路径几乎全是**正向扫描**(Seek + Next),反向扫描(Prev)极少用——主要是某些 Iterator 用法偶尔需要。LevelDB 选择"省一组 prev 指针、反向走用搜索"的权衡,减少了节点内存(每个节点少 height 个原子指针),换反向走的 $O(\log n)$ 而非 $O(1)$。这是工程权衡:**热路径(正向)优先**。

---

## 章末小结

这一章讲清了 MemTable 的骨架——SkipList:

1. **为什么选跳表**:并发友好。红黑树插入要旋转,撕裂读者视图,要安全就得读写锁,写阻塞所有读。跳表插入只新增节点 + $O(\log n)$ 个独立的 next 原子改,读者无锁也能看到一致视图。
2. **概率结构**:`RandomHeight` 用 `kBranching = 4` 生成几何分布高度,期望高度 $4/3$,查找/插入 $O(\log n)$。`kMaxHeight = 12` 是保守上限。
3. **Node 结构**:尾随数组 `std::atomic<Node*> next_[1]` + Arena 对齐内存 + placement new,无浪费、无析构开销。
4. **单写多读的无锁读**:`std::atomic<Node*>` 排除撕裂;`release` store 发布 / `acquire` load 读取建立 happens-before,保证读者看到完整节点;不变量"节点不删、key 不变"排除 use-after-free。三层叠加,无锁读 sound。

回到主线:SkipList 服务**前台**(写秒回的落点、读的第一站)。它的无锁读让前台 Get 不被 Put 阻塞,这是 LevelDB 在"一把大锁换写侧简单 + 无锁换读侧吞吐"上的精妙配合。这一配合会在 P1-06 写组章再次回扣。

### 五个"为什么"清单

1. **为什么 MemTable 用跳表不用红黑树/AVL?** 核心动机是并发友好,不是性能。红黑树插入的旋转会撕裂读者视图,要么用读写锁(写阻塞所有读)、要么手写极难的无锁 CAS。跳表插入只新增节点 + $O(\log n)$ 个独立的 next 原子改,读者无锁就能看到一致视图。
2. **`kBranching = 4` 怎么来的?** 跳表原始论文(Pugh 1990)建议 $p \in [1/2, 1/4]$。LevelDB 选 $p = 1/4$,更保守,换更少的空间开销(每节点平均 $4/3 \approx 1.33$ 个 next 指针,vs $p=1/2$ 的 2 个)。代价是稍高的查找常数。
3. **为什么 `Node::next_[1]` 声明成 `[1]` 而不是 `[kMaxHeight]`?** 尾随数组(flexible array member)——声明 `[1]` 是 C++ 语法占位,实际分配时由 `NewNode` 多分配 `height - 1` 个槽。每个节点正好用 height 个槽,无浪费。若固定开 `[kMaxHeight=12]`,绝大多数节点(高度 1~2)会浪费 10+ 个槽。
4. **无锁读凭什么不会数据竞争(UB)?** 三层保证:① `std::atomic<Node*>` 排除单变量撕裂(C++ 标准层面不 UB);② `release` store + `acquire` load 建立 happens-before,保证读者通过发布点看到完整节点(包括 key 和所有 next);③ 不变量"节点永不删除、key 永不改变"排除 use-after-free。三层缺一不可。
5. **为什么写者能用 `NoBarrier_SetNext`(relaxed)填新节点,但前驱改 next 必须用 `SetNext`(release)?** 新节点在 `NoBarrier_SetNext` 时还没挂进任何链,只有写者自己看得见,无并发,relaxed 够。前驱改 next 是"发布"——这一刻读者才能看到新节点,必须 release 才能配合读者的 acquire load 建立 happens-before。这一对**先 NoBarrier 再 release 发布**是 sound 性的精确次序。

### 想继续深入往哪钻

- **`FindLessThan` / `FindLast`** 的实现([db/skiplist.h:281-320](../leveldb/db/skiplist.h#L281-L320)):和 `FindGreaterOrEqual` 同构,也是 acquire load。看这两段能进一步确认读者路径全程无锁。
- **`skiplist_test.cc::ConcurrentTest` 的 `ReadStep`**([db/skiplist_test.cc:226-281](../leveldb/db/skiplist_test.cc#L226-L281)):它怎么验证"读者看到的状态是某个写者已完成时刻的一致快照",这是理解无锁读 sound 性的活教材。
- **C++ 内存模型的基础**:建议读 cppreference 上 "std::memory_order" 一页,或者《C++ Concurrency in Action》(Anthony Williams)第 5 章。release/acquire 是这套模型的脊柱。
- **RocksDB 的跳表** 在这个基础上加了几样东西:多个 MemTable 并发、`InlineSkipList`(无锁的多写者版本)。但核心思想(尾随数组、release/acquire、不变量守恒)和 LevelDB 一致。看 RocksDB 的 `memtable/inlineskiplist.h` 是一个很好的延伸。
- **为什么不用 `std::shared_mutex` 读写锁?** 因为写阻塞所有读。`shared_mutex` 在写者持锁期间,新读者也要排队等(避免写者饿死),这在 MemTable 这种写读都热的场景下退化成互斥锁。

### 引出下一章

SkipList 把 internal key 排好序了、并发问题解决了。但**节点本身的内存从哪来**?`NewNode` 调 `arena_->AllocateAligned(...)`,`MemTable::Add` 调 `arena_.Allocate(...)` 把 key/value 字节落地——这个 Arena 是什么?为什么 LevelDB 不直接 `new`/`malloc` 每个节点、而是写一个自己的 Arena?为什么 Arena 没有 `Free` 方法、只进不出?下一章 P1-05,我们钻进 `util/arena.h` 和 `util/arena.cc`,讲清楚 bump allocator 的设计——为什么"只进不出"反而更快、更省。
