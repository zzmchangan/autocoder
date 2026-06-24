# 第十五篇 · bbolt 之一:单文件 B+tree 与 mmap

> 篇:P4 backend 与 bbolt · 存储底座
> 主线呼应:这一章是**应用层**的纵深,也是全书第一次真正钻进存储引擎的骨架。上一章我们钻了 backend,但它从头到尾把 bbolt 的 `Tx` 当黑盒——"一个写事务 Commit 会付 COW + `fdatasync`"。可 bbolt 内部到底长什么样?它怎么把一个数据库塞进**一个文件**?为什么所有数据都按**定长页(page)**切?**B+tree** 的 branch/leaf 在磁盘上是什么形状?读为什么走 **mmap 只读映射**而写不走?这一章把这些"存储底座的骨架"立起来,为下一章 P4-16 的 COW 写事务铺好地基。

## 核心问题

**bbolt 这个底座,为什么把整个数据库塞进一个文件?为什么所有数据(无论 meta、索引、key/value)都切成定长的页(page,通常是 4096 字节的 OS 页)?B+tree 的 branch node 和 leaf node 在一页里怎么布局?读为什么用 mmap 把文件映射进内存——这凭什么比 `read()` 系统调用快?**

读完本章你会明白:

1. **单文件 + 定长页** 这两条设计,凭什么让"分配、对齐、定位、崩溃恢复"这四件事都变得简单——页是 bbolt 里一切操作的基本单位。
2. bbolt 的 **B+tree** 在磁盘上长什么样:branch page 装的是"key → 子页 pgid"的路由表,leaf page 装的是真正有序的 key/value;一页就是一个定长的磁盘结构,overflow 页用来放大对象。
3. **mmap 只读映射** 凭什么让读快:文件直接进进程地址空间,读不用 `read()` 拷贝、不用进内核态,OS 按需 page-in;写却**不走 mmap**——这是 bbolt 崩溃一致性的关键,下一章详讲。
4. 文件头的**四页布局**(meta0/meta1/freelist/空 leaf root),以及为什么 meta 要有两份(txid 双缓冲,崩溃恢复的根基)。

> **如果一读觉得太难**:先只记住三件事——① bbolt 把整库放一个文件,按定长页(= OS 页)切,页号 pgid 就是"偏移/pageSize";② 数据组织成一棵 B+tree,branch 页装路由、leaf 页装 key/value,二分查找;③ 读把文件 mmap 只读映射进内存,省掉 `read()` 的内核→用户拷贝,所以读快、写走另一条路。这三个钉死了,再回头读细节。

---

## 15.1 一句话点破

> **bbolt 是个单文件 B+tree:整个数据库就是一个普通文件,内部按定长页(通常是 4096 字节的 OS 页)切成一片一片,每一页用页号 pgid 编址,偏移 = pgid × pageSize,所以给定页号就能直接定位到字节偏移,不用任何额外索引。数据组织成一棵 B+tree——branch 页装"key → 子页 pgid"的路由,leaf 页装真正有序的 key/value,所有查找都是从 root 出发、每层二分、一路下沉到 leaf。读路径把整个文件用 mmap 只读映射进进程地址空间,读一个 key 就是顺着 mmap 指针访问内存,OS 按需把对应页从磁盘 page-in 进来,省掉了 `read()` 系统调用和内核→用户的拷贝——这是 bbolt 读快的根本。**

这是结论,不是理由。本章倒过来拆:先看 bbolt 为什么选"单文件 + 定长页"这套骨架(不这样会怎样),再钻进一页的内部结构(branch/leaf 在字节层面怎么排),然后顺着 B+tree 从 root 走到 leaf 看一次查找怎么完成,最后拆 mmap 为什么让读便宜、而写偏不走 mmap。

本章只讲**读路径和静态结构**——单文件、定长页、B+tree、mmap 读。**写怎么落盘**(COW 复制页、meta 翻转、freelist 回收)是下一章 P4-16 的主菜,本章只在必要时点一句"写靠 COW,下章讲"。

---

## 15.2 为什么是单文件 + 定长页:存储底座的两条骨架

先回答最根本的问题:bbolt 为什么把整个数据库塞进**一个文件**,而且所有数据都切成**定长**的页?

设想两个反面:

**反面一:多文件**(像某些早期 KV,一个 key range 一个文件,或者 data/index 分开成两个文件)。
- 管理复杂:文件数量随数据增长而膨胀,inode 压力大,目录扫描慢。
- 跨文件的事务一致性难做:一次写要改 data 文件、index 文件,两个文件的 `fsync` 不是原子的,中间崩溃就可能 data 和 index 对不上。
- 文件之间的空间复用难:一个文件空了,别的地方缺页,你没法跨文件挪。

**反面二:变长记录**(像日志结构存储,每条记录多长就写多长)。
- 崩溃恢复难:一条记录写到一半崩了,你怎么知道这条是完整的?要靠 checksum、length prefix 等额外机制。
- 空间回收难:删一条变长记录,留下一个不规则的洞,新记录未必塞得进去,碎片化严重。
- 定位难:给定一个 key,它在文件里的偏移是多少?得靠另一层索引(WAL 之外再加一层 SSTable/offset 表),绕一圈。

> **不这样会怎样**:多文件让事务原子性难做(跨文件 fsync 不原子)、管理复杂;变长记录让崩溃恢复和空间回收都变难。一个"想简单、想崩溃一致好做"的存储底座,自然要避开这两条。

bbolt 的选择是反过来的——**单文件 + 定长页**:

```go
// bbolt/db.go:38-157 (DB 结构,简化示意,只列与本章相关的字段)
type DB struct {
    // ...
    file     *os.File               // 整库就这一个文件
    dataref  []byte                  // mmap 返回的 byte slice(只读)
    data     *[common.MaxMapSize]byte // 上面那个 slice 转成的定长数组指针
    datasz   int                     // 实际映射大小
    meta0    *common.Meta            // 指向 page 0 的 meta(双缓冲之一)
    meta1    *common.Meta            // 指向 page 1 的 meta(双缓冲之二)
    pageSize int                     // 页大小,默认 = OS 页(4096)
    // ...
}
```

整库一个 `*os.File`,所有数据都在里头。文件内部按 `pageSize` 切成一片一片,每一片叫一页(page),用一个 64 位整数 `pgid`(`Pgid` 类型)编号:

```
                 一个 bbolt 文件(整库就这一个文件)
 ┌────────┬────────┬────────┬────────┬────────┬────────┬──────┬────────┐
 │ page 0 │ page 1 │ page 2 │ page 3 │ page 4 │ page 5 │ ...  │ page N │
 │ meta0  │ meta1  │freelist│  root  │ branch │  leaf  │      │  leaf  │
 │        │        │        │  leaf  │  (B+   │ (key/  │      │        │
 │        │        │        │ (空)   │ tree)  │ value) │      │        │
 └────────┴────────┴────────┴────────┴────────┴────────┴──────┴────────┘
  ←────────────────── 每个 page = pageSize 字节(通常 4096)──────────────────→

  字节偏移 = pgid × pageSize      (bbolt/db.go:1131: pos := id * db.pageSize)
```

> **所以这样设计**:单文件让"一次事务的多个改动"都在同一个文件里,一次 `fdatasync` 就能把它们一起落盘(下一章你会看到,bbolt 写事务提交靠的就是"把 dirty pages `writeAt` 进同一个文件 + 一次 `fdatasync`"),跨文件一致性难题被绕开了。定长页让定位变成一行算术 `偏移 = pgid × pageSize`,任何页都能 O(1) 找到;也让分配/回收变成"以页为单位,要么整页给你要么整页收回",不存在碎片;更让**崩溃恢复**简化到页级——一页要么完整写进去了、要么没写(配合 meta 页双缓冲,下一章讲),不会出现"半页"这种说不清的状态。

这两条选择(单文件 + 定长页)是 bbolt 一切设计的地基。后面你会看到:页是**分配的基本单位**(要新页就按页要)、**对齐的基本单位**(每页起始地址 = pgid × pageSize,天然对齐)、**崩溃恢复的基本单位**(COW 整页复制、meta 整页翻转)、**B+tree 节点的基本单位**(一个 B+tree 节点就装在一页或多页里)。bbolt 内部几乎所有操作,粒度都是"页"。

> **钉死这件事**:bbolt 把"页"当成一个一等公民。`Pgid`(`bbolt/internal/common/page.go:29`,就是 `uint64`)是整库的地址单位,给定 pgid,`db.page(id)`([db.go:1130](../bbolt/db.go#L1130))一行算术就能拿到这页的指针。理解了"一切以页为单位",bbolt 的所有机制(COW、freelist、meta 双缓冲)都有了一个统一的视角。

---

## 15.3 页是什么:Page 头与 branch/leaf 元素

既然一切以页为单位,那"一页"在字节层面长什么样?这一节把页的内部结构钉死。

bbolt 的页分四种,用一个 16 位的 `flags` 字段区分([page.go:18-23](../bbolt/internal/common/page.go#L18-L23)):

```go
// bbolt/internal/common/page.go:18-23
const (
    BranchPageFlag   = 0x01   // 分支页:装"key → 子页 pgid"路由
    LeafPageFlag     = 0x02   // 叶子页:装真正的 key/value
    MetaPageFlag     = 0x04   // 元数据页:整库的"目录"(root/freelist/txid)
    FreelistPageFlag = 0x10   // 空闲页表:记录哪些页是空闲可分配的
)
```

每一页的**开头 16 字节**是一个定长的**页头(Page)**,无论这页是什么类型,头都一样:

```go
// bbolt/internal/common/page.go:31-36
type Page struct {
    id       Pgid    // 8 字节:这一页的页号(自指,便于校验)
    flags    uint16  // 2 字节:这一页是 branch/leaf/meta/freelist 哪一种
    count    uint16  // 2 字节:这一页里装了多少个元素(branch/leaf 是子节点数)
    overflow uint32  // 4 字节:这一页"溢出"了几页(放不下一个的大对象用)
}
// PageHeaderSize = unsafe.Sizeof(Page{}) = 16 字节  (page.go:10)
```

页头之后,不同类型的页装不同内容:

- **meta 页**:头之后是一个 `Meta` 结构(15.4 节详讲),描述整库当前状态。
- **branch 页**:头之后是一组 `branchPageElement`,每个元素是"key → 子页 pgid"。
- **leaf 页**:头之后是一组 `leafPageElement`,每个元素是一个 key/value 对。
- **freelist 页**:头之后是一组 `Pgid`,记录空闲页号。

注意源码里有个**容易踩的命名坑**(本章要修正的一个印象):任务描述里把页内元素叫"leafPageElement / branchPageElement",听起来像公开类型,但源码里它们其实是**小写私有**结构体([page.go:225](../bbolt/internal/common/page.go#L225)、[page.go:261](../bbolt/internal/common/page.go#L261)),外部通过 `Page.BranchPageElement(i)` / `Page.LeafPageElements()` 这些方法访问。这样设计是为了页内元素的内存布局严格可控(用 `unsafe.Pointer` 偏移定位),不被外部直接改坏。

### branch page 内部:路由表

一个 branch 页装的是"key → 子页 pgid"的路由项。每一项长这样:

```go
// bbolt/internal/common/page.go:225-229
type branchPageElement struct {
    pos   uint32   // key 在页内的字节偏移(相对元素自己)
    ksize uint32   // key 的字节长度
    pgid  Pgid     // 子页的页号
}
// BranchPageElementSize = unsafe.Sizeof(branchPageElement{}) = 16 字节  (page.go:14)
```

一个 branch 页里,先是一连串定长的 `branchPageElement`(16 字节一个,按 key 升序排),然后是这些 key 的实际字节内容(挨个紧凑排)。`pos` 指向 key 字节,让 `Key()` 方法能用 `unsafe` 切出 key 的 slice([page.go:256-258](../bbolt/internal/common/page.go#L256-L258))。

**注意:branch 元素没有 value**,只有 key 和子页 pgid——它纯粹是路由,不存数据。这是 B+tree(不是 B-tree)的特征:**只有 leaf 装数据,branch 只负责导航**。

### leaf page 内部:key/value 对

一个 leaf 页装的是真正的 key/value 对。每一项:

```go
// bbolt/internal/common/page.go:261-266
type leafPageElement struct {
    flags uint32   // 标志位(子 bucket 用 BucketLeafFlag=0x01 标记)
    pos   uint32   // key+value 在页内的字节偏移(相对元素自己)
    ksize uint32   // key 字节长度
    vsize uint32   // value 字节长度
}
// LeafPageElementSize = unsafe.Sizeof(leafPageElement{}) = 16 字节  (page.go:15)
```

leaf 比 branch 多了 `flags`(标记子 bucket)和 `vsize`(value 长度)。key 和 value 紧挨着存在元素后面,`Key()` / `Value()` 用 `pos` + `ksize`/`vsize` 切出([page.go:310-321](../bbolt/internal/common/page.go#L310-L321))。

把一页的布局画出来(以一个装了 3 个 key/value 的 leaf 页为例):

```
   一个 leaf page(共 pageSize 字节,例如 4096)
 ┌──────────────────────────────────────────────────────────────────────┐
 │ Page 头 (16 字节)                                                     │
 │   id=Pgid  flags=LeafPageFlag(0x02)  count=3  overflow=0             │
 ├──────────────────────────────────────────────────────────────────────┤
 │ leafPageElement[0] (16 字节): flags/pos/ksize/vsize  → 指向下方 key0 │
 │ leafPageElement[1] (16 字节): flags/pos/ksize/vsize  → 指向下方 key1 │
 │ leafPageElement[2] (16 字节): flags/pos/ksize/vsize  → 指向下方 key2 │
 ├──────────────────────────────────────────────────────────────────────┤
 │ key0 字节 │ value0 字节 │ key1 字节 │ value1 字节 │ key2 字节 │ value2 │
 │           │            │          │            │          │        │
 │                                                                      │
 │  ... 剩余空间(直到页尾) ...                                          │
 └──────────────────────────────────────────────────────────────────────┘
   元素表(定长,按 key 升序)在前,key/value 字节紧凑排在后面
   二分查找时只在元素表里二分(每项 16 字节,缓存友好),找到再按 pos 取 key/value
```

branch 页结构几乎一样,只是元素是 `branchPageElement`(无 value),元素表里每项指向一个子页 pgid。

### overflow:一页装不下怎么办

有些 value 很大(比如 etcd 里一个大配置项,或 bbolt 自己存一个大的子 bucket 树)。一页 4096 字节装不下怎么办?用 `overflow` 字段:一页声明自己"溢出"了 N 页,实际占 `1 + overflow` 页连续空间。分配时 `db.allocate` 一次性给 `count` 个连续页([db.go:1174](../bbolt/db.go#L1174) `p.SetOverflow(uint32(count - 1))`),读时 `db.page(id)` 还是按起始页号定位,后面的 overflow 页在地址上是连续的,直接顺着指针访问就行。这让 bbolt 既能高效存小 key/value(一页装一大批),也能容纳大对象(用 overflow 页),不必为"大 value"特殊处理。

> **钉死这件事**:bbolt 的页有一个统一的 16 字节头(`Page{id, flags, count, overflow}`),头之后按页类型装不同元素:branch 装 `branchPageElement{pos, ksize, pgid}`(路由)、leaf 装 `leafPageElement{flags, pos, ksize, vsize}`(数据)。元素定长(都是 16 字节)、按 key 升序排在前,key/value 字节紧凑排在后,这种"定长索引 + 变长 payload"的布局,让二分查找只在 16 字节一项的元素表里做,缓存友好,定位快。overflow 字段让一页能扩展成多页,容纳大对象。

---

## 15.4 meta 页:整库的"目录"

讲 B+tree 之前,先看一眼文件头那两页特殊的页——**meta 页**。它不长这样,后面 B+tree 的 root 指针就无处安放。

meta 页在文件最开头,**page 0 和 page 1 都是 meta 页**(双缓冲)。一个 meta 页就是一个 `Meta` 结构([meta.go:12-22](../bbolt/internal/common/meta.go#L12-L22)):

```go
// bbolt/internal/common/meta.go:12-22
type Meta struct {
    magic    uint32    // 固定魔数 0xED0CDAED,标识"这是个 bbolt 文件"
    version  uint32    // 格式版本(当前 = 2)
    pageSize uint32    // 这个库用的页大小(写到 meta 里,跨平台能读出来)
    flags    uint32    // 保留标志位
    root     InBucket  // 整库根 bucket 指向哪个页(InBucket{root pgid, sequence})
    freelist Pgid      // freelist 页的页号
    pgid     Pgid      // 高水位线:下一个可分配的页号(已用页数)
    txid     Txid      // 写这个 meta 的事务 id(单调递增)
    checksum uint64    // 整个 meta 的 fnv64a 校验和
}
```

几个字段的意义:

- **`root.root`**:整库根 bucket 的根页号。所有查找从这里出发——这就是 B+tree 的"入口"。
- **`freelist`**:freelist 页的页号(空闲页表,下一章详讲)。
- **`pgid`**:高水位线,即"下一个可分配的页号"。要新页就从这里往下分配,bbolt 据此知道文件要长到多大。
- **`txid`**:写这个 meta 的事务 id,**单调递增**。这是 meta 双缓冲的关键(下一章用),本章先记住:**两个 meta 页交替写,txid 大的那个是当前最新的**。
- **`checksum`**:整个 meta 的校验和(`Sum64` 用 fnv64a 算除了 checksum 字段以外的所有字段,[meta.go:61-65](../bbolt/internal/common/meta.go#L61-L65))。bbolt 启动时 `Validate()`([meta.go:25-34](../bbolt/internal/common/meta.go#L25-L34))校验 magic、version、checksum,任何一个对不上就认为这个 meta 页损坏。

### 文件头的四页:bbolt 初始化时写了什么

bbolt `Open` 一个全新文件时,会调 [`db.init()`](../bbolt/db.go#L646) 在文件头写下 4 页,把骨架立起来([db.go:646-689](../bbolt/db.go#L646-L689)):

```go
// bbolt/db.go:646-689 (简化示意,保留关键步骤)
func (db *DB) init() error {
    buf := make([]byte, db.pageSize*4)       // 4 页缓冲
    for i := 0; i < 2; i++ {                  // page 0、page 1:两个 meta 页
        p := db.pageInBuffer(buf, common.Pgid(i))
        p.SetId(common.Pgid(i))
        p.SetFlags(common.MetaPageFlag)
        m := p.Meta()
        m.SetMagic(common.Magic)
        m.SetVersion(common.Version)
        m.SetPageSize(uint32(db.pageSize))
        m.SetFreelist(2)                       // freelist 在 page 2
        m.SetRootBucket(common.NewInBucket(3, 0)) // 根 bucket 的根在 page 3
        m.SetPgid(4)                           // 高水位:下一个可分配是 page 4
        m.SetTxid(common.Txid(i))              // meta0 的 txid=0, meta1 的 txid=1
        m.SetChecksum(m.Sum64())
    }
    // page 2:一个空的 freelist 页
    p := db.pageInBuffer(buf, common.Pgid(2))
    p.SetId(2); p.SetFlags(common.FreelistPageFlag); p.SetCount(0)
    // page 3:一个空的 leaf 页,作为根 bucket 的初始 root
    p = db.pageInBuffer(buf, common.Pgid(3))
    p.SetId(3); p.SetFlags(common.LeafPageFlag); p.SetCount(0)
    // 整个缓冲一次写进文件,再 fdatasync
    db.ops.writeAt(buf, 0)
    fdatasync(db)
    return nil
}
```

一个新 bbolt 文件的固定开头:

| 页号 | 类型 | 内容 |
|------|------|------|
| page 0 | meta0 | 整库目录(txid=0) |
| page 1 | meta1 | 整库目录副本(txid=1) |
| page 2 | freelist | 空闲页表(初始为空) |
| page 3 | leaf | 根 bucket 的初始 root(空 leaf) |
| page 4+ | (未分配) | 后续按需分配 |

注意 **meta 页有两份**——page 0 和 page 1。这是 bbolt 崩溃恢复的根基(下一章详讲):每次写事务提交,轮流写其中一个 meta 页(`p.id = Pgid(m.txid % 2)`,[meta.go:51](../bbolt/internal/common/meta.go#L51)),写完后 `fdatasync`。如果写到一半崩溃,另一个 meta 页还是上一份完整的,启动时 [`db.meta()`](../bbolt/db.go#L1141) 取 txid 更大且 `Validate()` 通过的那份,就能恢复到上一个一致状态。本章不展开 COW 写流程,只把"meta 在文件头有两份"这个事实钉住。

### 启动时怎么找到 meta

bbolt `Open` 会把文件 mmap 只读映射进来(15.6 节详讲),然后**直接取 page 0 和 page 1 作为两个 meta**([db.go:539-540](../bbolt/db.go#L539-L540)):

```go
// bbolt/db.go:539-540 (在 mmap() 末尾)
db.meta0 = db.page(0).Meta()
db.meta1 = db.page(1).Meta()
err0 := db.meta0.Validate()
err1 := db.meta1.Validate()
if err0 != nil && err1 != nil { return err0 }  // 两个都坏才报错
```

之后任何读操作要拿到"当前 meta",调 [`db.meta()`](../bbolt/db.go#L1141):

```go
// bbolt/db.go:1141-1162 (简化示意)
func (db *DB) meta() *common.Meta {
    metaA := db.meta0
    metaB := db.meta1
    if db.meta1.Txid() > db.meta0.Txid() {   // 取 txid 更大的那个
        metaA, metaB = db.meta1, meta0
    }
    if err := metaA.Validate(); err == nil { // 且 Validate 通过
        return metaA
    } else if err := metaB.Validate(); err == nil {
        return metaB                          // 最新那个坏了,退回上一个
    }
    panic("bolt.DB.meta(): invalid meta pages")
}
```

> **钉死这件事**:文件头前两页永远是 meta 页(meta0、meta1),两个交替写,txid 大且校验通过的那个是当前有效 meta。`db.meta()` 这几行就是 bbolt 崩溃恢复的入口——哪怕正在写的那份 meta 写坏了,还有另一份完整的兜底。为什么这么设计能 work、COW 写时怎么保证"总有一份完整",是下一章 P4-16 的核心。

---

## 15.5 B+tree:branch 导航、leaf 存数据

有了"页"这个基本单位,bbolt 把所有 key/value 组织成一棵 **B+tree**。这一节讲清三件事:① 为什么是 B+tree 不是别的;② B+tree 在 bbolt 里怎么落地(branch/leaf 怎么连);③ 一次查找/遍历怎么走。

### 为什么是 B+tree

存储引擎选 B+tree,是因为它同时满足三个刚需:

- **有序**:key 按字节序排,range 扫描天然高效(给个起点 key,顺着叶子链一路扫)。
- **磁盘友好**:树很矮(分支因子大,一个 branch 页能装成百上千个路由项),一次查找从 root 到 leaf 通常 2~4 层 page-in,IO 次数少。
- **增删平衡**:插入/删除引发节点分裂/合并,自动保持树平衡,不会退化成链表。

> **不这样会怎样**:用哈希表(O(1) 查找,但**无序**,range 扫描要全表)、用跳表(内存里好用,磁盘 IO 不友好)、用裸日志(只能追加,查询要全扫)都不行。B+tree 是"磁盘上的有序字典"这个需求里,工程上最成熟的答案。LevelDB 用 LSM-tree(另一种答案,写多读少更优,但读要查多层 SSTable),bbolt 选 B+tree(读优、range 优,代价是写要 COW + 平衡),这是两种存储引擎哲学的分野——etcd 是"读多写少"的配置中心,选 bbolt(B+tree)正合适。

### branch 与 leaf 怎么连:每个 Bucket 是一棵 B+tree

bbolt 里,**每个 Bucket 就是一棵独立的 B+tree**([bucket.go:30-44](../bbolt/bucket.go#L30-L44)):

```go
// bbolt/bucket.go:30-44 (简化示意)
type Bucket struct {
    *common.InBucket                    // 内嵌 InBucket{root Pgid, sequence uint64}
    tx       *Tx                        // 所属事务
    buckets  map[string]*Bucket         // 子 bucket 缓存
    page     *common.Page               // inline bucket 用:小 bucket 直接内联在父页的 value 里
    rootNode *node                      // 写事务物化的 root node(读事务为 nil,直接用 page)
    nodes    map[common.Pgid]*node      // 写事务的 node 缓存
    FillPercent float64                 // 分裂时的填充率(默认 0.5)
}
```

`InBucket.root` 是这棵 B+tree 的根页号。根页是 leaf 还是 branch,取决于数据量——数据少时整棵树就一个 leaf 页(root 直接是 leaf);数据多了,leaf 分裂,root 变成 branch,下面挂着多个 leaf。

一棵多层的 bbolt B+tree 长这样:

```
  meta.root.root = pgid 12  (整库根 bucket 的根在 page 12)
                              │
                              ▼
   ┌────────────────────── page 12 (branch, root) ───────────────────────┐
   │ Page 头: flags=Branch, count=3                                       │
   │ branchElement[0]: key="apple"  pgid=20   (子页 page 20)             │
   │ branchElement[1]: key="mango"  pgid=35   (子页 page 35)             │
   │ branchElement[2]: key="pear"   pgid=48   (子页 page 48)             │
   └───────────────────────────┬─────────────────────────────────────────┘
        key < "apple"           │ key ∈ ["apple","mango")      key >= "mango"
              ┌─────────────────┼─────────────────────────┐
              ▼                 ▼                           ▼
   ┌── page 20 (leaf) ──┐  ┌── page 35 (leaf) ──┐   ┌── page 48 (leaf) ──┐
   │ key="apple" val=.. │  │ key="mango" val=.. │   │ key="pear"  val=.. │
   │ key="avocado"      │  │ key="melon"        │   │ key="peach"        │
   │ key="banana"       │  │ key="orange"       │   │ key="plum"         │
   └────────────────────┘  └────────────────────┘   └────────────────────┘
        (所有数据只在 leaf;branch 只存"第一个 key + 子页 pgid"作路由)
```

注意几个 B+tree 的关键性质(和 B-tree 对比):

1. **数据只在 leaf**:branch 页的元素**没有 value**,只有 key 和子页 pgid。所有真正的 key/value 都在 leaf 页。
2. **branch 的 key 是子页里"最小 key"的副本**:比如 `key="apple" pgid=20`,意味着 page 20 里所有 key 都 ≥ "apple"(其实 page 20 的第一个 key 就是 "apple")。这是 B+tree 路由的方式。
3. **leaf 之间没有显式链表**(这点和教科书 B+tree 不同):bbolt 的 leaf 页之间**不存兄弟指针**,range 扫描靠 cursor 沿着 branch 页的索引顺序回溯(15.5 节末讲)。

### 一次查找怎么走:Cursor 的二分下沉

读一个 key 怎么找到它的 value?用 `Bucket.Cursor()`([bucket.go:74-83](../bbolt/bucket.go#L74-L83))拿到一个 cursor,然后 `Seek`。Cursor 内部持有一个"栈"(`stack []elemRef`),记录从 root 一路下沉经过的每一层:

```go
// bbolt/cursor.go:22-25
type Cursor struct {
    bucket *Bucket
    stack  []elemRef   // 查找路径,每层一个 elemRef
}
// bbolt/cursor.go:412-416
type elemRef struct {
    page  *common.Page   // 读事务:指向 mmap 里的页
    node  *node          // 写事务:指向物化的 node(二选一)
    index int            // 在这一层停在第几个元素
}
```

一次 `Seek(key)` 的核心是 [`cursor.search`](../bbolt/cursor.go#L283),它从 root 开始,递归下沉([cursor.go:283-302](../bbolt/cursor.go#L283-L302),简化示意):

```go
// bbolt/cursor.go:283-302 (简化示意)
func (c *Cursor) search(key []byte, pgId common.Pgid) {
    p, n := c.bucket.pageNode(pgId)           // 取这一页:读用 page,写用 node
    e := elemRef{page: p, node: n}
    c.stack = append(c.stack, e)              // 入栈
    if e.isLeaf() {
        c.nsearch(key)                         // leaf:在叶子页内二分
        return
    }
    if n != nil { c.searchNode(key, n); return } // 写事务物化了 node:在 inodes 二分
    c.searchPage(key, p)                        // 读事务:在 branch 页元素里二分
}
```

branch 页内的二分在 [`searchPage`](../bbolt/cursor.go#L324)(简化示意):

```go
// bbolt/cursor.go:324-345 (简化示意)
func (c *Cursor) searchPage(key []byte, p *common.Page) {
    inodes := p.BranchPageElements()          // 取 branch 页所有路由项
    index := sort.Search(int(p.Count()), func(i int) bool {
        return bytes.Compare(inodes[i].Key(), key) != -1
    })
    if !exact && index > 0 { index-- }        // 找到第一个 >= key 的;若没有精确匹配,退一格
    c.stack[len(c.stack)-1].index = index
    c.search(key, inodes[index].Pgid())        // 递归下沉到子页
}
```

`sort.Search` 是 Go 标准库的二分查找。注意这里有个**微妙的 `-1`**:B+tree 的 branch 路由项 key 是子页"第一个 key"的副本,查找时要找"第一个 ≤ key 的路由项"(它的子页里才可能有这个 key)。`sort.Search` 找的是"第一个 >= key 的",所以找到后若不是精确匹配,要退一格([cursor.go:315-317、338-340](../bbolt/cursor.go#L315-L317))。这是个容易踩的边界,源码里 TODO 注释也承认这里写得"hacky"。

下沉到 leaf 后,在 leaf 页元素里再二分一次([`nsearch`](../bbolt/cursor.go#L348)):

```go
// bbolt/cursor.go:348-367 (简化示意)
func (c *Cursor) nsearch(key []byte) {
    e := &c.stack[len(c.stack)-1]
    inodes := e.page.LeafPageElements()       // leaf 页所有 key/value
    index := sort.Search(int(e.page.Count()), func(i int) bool {
        return bytes.Compare(inodes[i].Key(), key) != -1
    })
    e.index = index                            // 停在第一个 >= key 的位置
}
```

至此 `Seek` 完成:cursor 停在 leaf 页里某个位置,`keyValue()` 返回那一项的 key/value。

### range 扫描:Next 怎么走

`Next()`([cursor.go:215-246](../bbolt/cursor.go#L215-L246))的逻辑值得单独看,因为它解释了 bbolt B+tree **为什么不用 leaf 链表**:

```go
// bbolt/cursor.go:215-246 (简化示意)
func (c *Cursor) next() (key, value []byte, flags uint32) {
    for {
        // 从栈顶往下找:哪一层还能往右挪一格
        var i int
        for i = len(c.stack) - 1; i >= 0; i-- {
            elem := &c.stack[i]
            if elem.index < elem.count()-1 {  // 这一层还没到末尾
                elem.index++
                break
            }
        }
        if i == -1 { return nil, nil, 0 }     // 全部层都到末尾,遍历完
        // 从第 i 层开始,重新下沉到新一层的第一个 leaf 元素
        c.stack = c.stack[:i+1]
        c.goToFirstElementOnTheStack()
        return c.keyValue()
    }
}
```

这揭示了一个关键事实:**bbolt 的 leaf 页之间不存兄弟指针,range 扫描靠栈回溯**。当你在一个 leaf 页里扫到末尾,cursor 退回上一层 branch(branch 的 index 往右挪一格),再顺着新的 branch 路由下沉到下一个 leaf 的第一个元素。整棵树没有显式的 leaf 链表,遍历的"下一页"信息全部隐含在 branch 页的索引顺序里。

> **钉死这件事**:bbolt 的 B+tree,branch 页装"key → 子页 pgid"路由(无 value),leaf 页装真正有序的 key/value。查找 = 从 root 出发,每层在 branch 元素里二分,找到子页号递归下沉,直到 leaf 页再二分一次。range 扫描靠 cursor 的栈回溯——没有 leaf 兄弟指针,全靠 branch 索引顺序。`sort.Search` 二分 + `unsafe.Pointer` 偏移取元素,让一次查找在内存里就是几次比较 + 几次指针跳转,极快。配合下一节的 mmap,这些指针跳转直接发生在映射进进程的内存里,连系统调用都不用。

---

## 15.6 mmap 只读映射:读凭什么这么快

前面四节都在讲"页"和"B+tree"的静态结构。这一节回答本章最后一个核心问题:**读为什么用 mmap,凭什么比 `read()` 快**?

### mmap 是什么

mmap(Memory Map)是 Unix 的一个系统调用,它**把文件映射进进程的虚拟地址空间**。映射之后,进程访问这块地址,就等于访问文件的对应字节——内核负责在背后按需把文件的页从磁盘读进物理内存(page-in),对进程透明。

bbolt 在 `Open` 时,把整个文件 mmap 只读映射进来([db.go:456](../bbolt/db.go#L456)、[bolt_unix.go:55-74](../bbolt/bolt_unix.go#L55-L74),Linux/Unix 实现):

```go
// bbolt/bolt_unix.go:55-74 (Linux/Unix 的 mmap 实现)
func mmap(db *DB, sz int) error {
    // 关键 1:PROT_READ —— 只读映射,写会触发 SEGV
    b, err := unix.Mmap(int(db.file.Fd()), 0, sz, syscall.PROT_READ, syscall.MMAP_SHARED|db.MmapFlags)
    if err != nil { return err }
    // 关键 2:MADV_RANDOM —— 告诉内核访问模式是随机(B+tree 查找不是顺序),别预读太多
    err = unix.Madvise(b, syscall.MADV_RANDOM)
    if err != nil && err != syscall.ENOSYS { return fmt.Errorf("madvise: %s", err) }
    // 关键 3:把 mmap 的 byte slice 转成一个"长度 = MaxMapSize"的数组指针
    //         这样后面 db.page(id) 用下标访问,边界检查由 MaxMapSize 兜底
    db.dataref = b
    db.data = (*[common.MaxMapSize]byte)(unsafe.Pointer(&b[0]))
    db.datasz = sz
    return nil
}
```

三个值得停下来看的点:

1. **`PROT_READ` 只读**:mmap 这块内存只有读权限。任何写操作会触发段错误(SEGV)。这是 bbolt **读路径走 mmap、写路径不走 mmap** 的根源——读快靠 mmap,但写不能在 mmap 上直接改(下一节详讲为什么)。
2. **`MADV_RANDOM`**:`madvise` 提示内核这块内存的访问模式是**随机**(B+tree 查找跳跃式访问),让内核别做激进的预读(预读对随机访问是浪费)。
3. **`db.data = (*[MaxMapSize]byte)(unsafe.Pointer(&b[0]))`**:`MaxMapSize` 在 amd64 上是 `0xFFFFFFFFFFFF`(256TB,[bolt_amd64.go:4](../bbolt/internal/common/bolt_amd64.go#L4))。bbolt 把 mmap 的 slice 转成一个 256TB 的数组指针,这样 `db.page(id)` 就能写成 `&db.data[id*pageSize]`——一个数组下标,Go 的边界检查用 MaxMapSize 兜底(实际永远访问不到 256TB)。

### 读一个 key 怎么走完 mmap 全程

把前面几节串起来,读 `Get(key)` 在 bbolt 里的完整路径:

```
  cursor.Seek(key)
      │
      ▼
  1. db.meta()                              取当前有效 meta(15.4 节)
      │   → metaA = meta0/meta1 里 txid 更大且 Validate 通过的那个
      │   → metaA.root.root = 整库根 bucket 的根页号
      ▼
  2. bucket.RootPage() → pgid               拿到根 bucket 的 root pgid
      │
      ▼
  3. cursor.search(key, rootPgid)           从 root 开始下沉(15.5 节)
      │
      │  对每一层:
      │   ┌─ bucket.pageNode(pgId)
      │   │     ↓ 读路径用 page(不物化 node)
      │   │   p = db.page(pgId)             ← 这一步直接访问 mmap
      │   │     pos := pgId * pageSize      (db.go:1131)
      │   │     return (*Page)(&db.data[pos])
      │   │     ↓
      │   │   p 就是 mmap 里这页的指针(没拷贝!)
      │   │
      │   ├─ searchPage: 在 p.BranchPageElements() 里二分
      │   │     ↓ 这些元素也在 mmap 里(没拷贝!)
      │   │   inodes[i].Key() 用 unsafe.Pointer 偏移切出 key slice
      │   │     ↓
      │   │   找到子页 pgid,递归 search
      │   └─ 直到 leaf 层,nsearch 在 leaf 元素里二分
      │
      ▼
  4. keyValue() 返回 key/value slice        ← 这些字节也在 mmap 里
                                              整个过程零拷贝、零系统调用(除可能的 page-in)
```

**关键:整条读路径,没有任何一次 `read()` 系统调用,也没有任何一次内核→用户的字节拷贝。** 所有指针操作都发生在 mmap 映射进来的那块虚拟内存里。操作系统在背后按需把访问到的页从磁盘 page-in 到物理内存(第一次访问某页会触发缺页中断,内核读盘;之后再访问就是内存访问)。

### mmap 读凭什么比 read() 快

来对比一下"用 `read()` 系统调用读"和"用 mmap 读":

| 维度 | `read()` 系统调用 | mmap 只读映射 |
|------|-------------------|---------------|
| 每次读的开销 | 进内核态、内核读页 cache、拷贝到用户 buffer、返用户态 | 第一次访问触发缺页中断(内核读盘),之后纯内存访问 |
| 内核→用户拷贝 | 有(内核 page cache → 用户 buffer) | 无(用户直接看 page cache) |
| 重复读同一页 | 每次 `read()` 都要进内核(哪怕页已在 cache) | 第一次 page-in,之后就是访问内存 |
| 适用场景 | 一次性大块读 | 随机、频繁、小块读(B+tree 查找正是这种) |

> **不这样会怎样**:如果 bbolt 读路径用 `read()`——
> - 每读一页(B+tree 一层)都要一次 `read()` 系统调用:系统调用开销(进/出内核态,几十到几百 ns)+ 拷贝开销。
> - B+tree 查找是**随机跳跃式**访问(从 root 跳到某个 branch、再跳到某个 leaf),`read()` 的预读毫无用处,反而每次都拷一整页。
> - 同一页被反复读(root 页每次查找都要访问),每次 `read()` 都重做一遍内核→用户拷贝。
>
> mmap 把这一切省掉:文件就是进程地址空间里的一段内存,指针直接指过去,内核在背后按需填页。读 B+tree 就像在内存里走一棵树,快得自然。

### 为什么 mmap 通常是"只读"——写的另一条路

注意 bbolt 的 mmap 是 `PROT_READ` **只读**的。写不能在 mmap 上做(写了 SEGV)。那写怎么落盘?

bbolt 的写路径**完全不走 mmap**,而是用 `db.file.WriteAt(buf, offset)`([db.go:151](../bbolt/db.go#L151) `ops.writeAt` 字段)把 dirty pages 直接按字节偏移写进文件。写完再 `fdatasync`。下一章 P4-16 会详讲这套 COW 写流程,这里先点一句为什么:

> **写不走 mmap,是因为 mmap 写要靠 `PROT_WRITE` + 修改内存触发内核回写,但内核回写的时机不可控**(脏页什么时候落盘由内核决定,bbolt 没法在崩溃一致性关键点上强保证证已落盘)。bbolt 需要的是"我显式 `writeAt` + 显式 `fdatasync`",把落盘时机完全握在自己手里——这是崩溃一致性的要求。所以 mmap 只用来读,写走另一条 `WriteAt` + `fdatasync` 的路。写路径的具体细节(COW 复制页、meta 双缓冲翻转、freelist 回收),是下一章的主菜。

> **钉死这件事**:bbolt 读路径把整个文件 mmap 只读映射进进程地址空间,读一个 key 就是顺着 mmap 指针在 B+tree 上走几层,OS 按需 page-in,零系统调用、零内核→用户拷贝——这是 bbolt 读快的根本。写路径**不走 mmap**(用 `WriteAt` + `fdatasync` 显式控制落盘),因为崩溃一致性要求"何时落盘"完全可控,mmap 写的内核回写时机满足不了这个要求。读走 mmap、写走 WriteAt,是 bbolt 在"读快"和"写崩溃一致"之间的明确分工。

---

## 15.7 技巧精解:单文件 + 定长页 + mmap 只读映射

本章挑三个最硬核的设计单独拆透。

### 技巧一:单文件 + 定长页,让"定位/分配/恢复"都简化到页级

**它解决什么问题**:存储引擎要回答四个问题——① 给我一个 key,它在磁盘哪里?② 我要存新数据,空间从哪来?③ 不同大小的对象怎么共处?④ 崩溃了怎么知道哪些是完整的、哪些是半成品?这四个问题如果各自有各自的答案,系统会变得极其复杂。

**bbolt 的手段**:用"单文件 + 定长页"这一个约定,把四个问题统一回答:

1. **定位**:`偏移 = pgid × pageSize`。给定 pgid,一行算术拿到字节偏移,不用任何额外索引。`db.page(id)`([db.go:1130-1133](../bbolt/db.go#L1130))就两行:
   ```go
   func (db *DB) page(id common.Pgid) *common.Page {
       pos := id * common.Pgid(db.pageSize)
       return (*common.Page)(unsafe.Pointer(&db.data[pos]))
   }
   ```
2. **分配**:要空间就按"几页"要。`db.allocate(txid, count)`([db.go:1165](../bbolt/db.go#L1165))从 freelist 拿 `count` 个连续空闲页,或从高水位线 `meta.pgid` 往下分配新的。一页一页地分,没有变长记录的碎片问题。
3. **共处**:小对象一页装很多(一个 leaf 页能装几百个 key/value),大对象用 `overflow` 占多页。页是统一单位,大小对象都按页管理。
4. **恢复**:一页要么完整、要么没写(配合 meta 双缓冲和 `fdatasync`,下一章详讲)。没有"半条记录"这种说不清的状态,崩溃后 `db.meta()` 取校验通过的那份 meta 即可恢复。

**反面对比**:如果不用这套约定——
- **多文件**:跨文件事务的 `fsync` 不原子,崩溃可能 data 和 index 对不上;管理多个文件的 inode、增长、截断都复杂。
- **变长记录**:删一条留个不规则的洞,新记录未必塞得下,碎片严重;一条记录写一半崩了,无法判断完整性,要靠 checksum + length prefix 额外机制。
- **多层索引**(像 LSM-tree):读要查多层 SSTable,写要 compaction,系统复杂度高一个量级。

**这个手段妙在哪**:把四个看似独立的问题(定位、分配、共处、恢复),用一个统一的"页"概念一并解决。整库的地址空间就是 `[0, pgid × pageSize)`,任何东西都在这个一维空间里有一个"页号"。后续所有机制(COW 整页复制、freelist 整页回收、meta 整页翻转)都建立在这个统一单位上,设计极大简化。这是"用对的基本单位,把复杂度降一个量级"的典型。

### 技巧二:mmap 只读映射,把读的拷贝和系统调用都省掉

**它解决什么问题**:B+tree 的查找是**随机跳跃式**访问(从 root 跳到某个 branch,再跳到某个 leaf),而且**同一页会被反复访问**(root 页每次查找都要访问)。这种访问模式,如果用 `read()` 系统调用读,每次都要进/出内核态 + 内核→用户拷贝,开销叠在每层 B+tree 上,读会慢得不可接受。

**bbolt 的手段**:Open 时把整个文件 mmap 只读映射进进程地址空间([bolt_unix.go:55-74](../bbolt/bolt_unix.go#L55-L74))。映射之后:
- 读一页 = 访问 `db.data[pgid × pageSize]`,一次指针解引用,没有系统调用。
- 读到的字节直接就是内核 page cache 里的内容(共享映射),没有内核→用户拷贝。
- OS 在背后按需 page-in:第一次访问某页触发缺页中断、内核读盘;之后再访问就是纯内存操作。
- `MADV_RANDOM` 提示内核访问是随机的,别浪费预读。

**反面对比**:如果读用 `read()`——
- 每层 B+tree 一次 `read()`:系统调用开销(几十到几百 ns)+ 内核→用户拷贝(哪怕页已在 page cache 也要拷)。
- root 页被反复读,每次 `read()` 都重做一遍拷贝。
- `read()` 的预读对随机访问无用,反而占内存。

**这个手段妙在哪**:
1. **零拷贝**:用户进程直接看内核 page cache,数据在物理内存里只有一份(内核的 page cache),用户访问这块地址就是访问 page cache,没有第二次拷贝。
2. **零系统调用**:读路径不再调 `read()`,指针操作全在用户态。系统调用只在 page-in(缺页中断,由内核处理,对进程透明)时发生,而且每页只发生一次。
3. **天然配合 B+tree**:B+tree 的"每页一个节点"和 mmap 的"按页映射"完美契合,`db.page(id)` 一行算术拿指针,顺着指针走 B+tree 就像走内存里的链表。
4. **只读的精妙**:bbolt 故意只映射成 `PROT_READ`,写走另一条路。为什么?因为 mmap 写的内核回写时机不可控,bbolt 需要显式 `WriteAt` + `fdatasync` 控制落盘时机(崩溃一致性的硬要求)。读走 mmap、写走 WriteAt,分工明确。

**反面对比的另一面——mmap 的代价**:不是没有。mmap 的代价是:
- **地址空间占用**:64 位下问题不大(amd64 `MaxMapSize=256TB`),32 位下 `MaxMapSize=2GB`([bolt_386.go:4](../bbolt/internal/common/bolt_386.go#L4)),大库映射不下——这是 bbolt 在 32 位平台上的硬限制。
- **page fault 抖动**:冷启动时大量缺页中断,有延迟尖刺。bbolt 提供 `MmapFlags = MAP_POPULATE` 选项([db.go:84](../bbolt/db.go#L84) 注释提到),让 Linux 启动时预读所有页,削平尖刺(代价是启动慢、占内存)。
- **remap 开销**:文件增长时要重新 mmap(旧的 unmap、新的 map),[`db.mmap`](../bbolt/db.go#L456) 拿 `mmaplock` 写锁保护这个过程。频繁增长会有抖动,bbolt 用 `AllocSize`([db.go:105](../bbolt/db.go#L105))一次多分配些空间,减少 remap 频率。

这些代价都是可控的、且远小于"每次读都 `read()`"的开销,所以 bbolt 选 mmap 读路径。

> **钉死这件事**:bbolt 的读快,根本上是 mmap 的功劳——文件映射进进程地址空间,读 B+tree 就是顺着指针走内存,零系统调用、零拷贝,OS 按需 page-in。这是 bbolt 作为"读多写少"的 etcd 底座,能把读延迟做到内存级别的关键。理解了 mmap 读路径,你就理解了为什么 bbolt 读那么快——以及为什么写偏不走 mmap(崩溃一致性要求显式控制落盘)。

---

## 章末小结

这一章我们钻进了 bbolt 的**静态骨架**——单文件、定长页、B+tree、mmap 只读映射。我们没有碰写路径(COW、meta 翻转、freelist 回收,那是下一章 P4-16 的主菜),只把"bbolt 这个底座本身长什么样、读路径怎么走"这两件事钉死了。

回到全书二分法:bbolt 服务**应用层**这一面——它是 mvcc 之下、磁盘之上的最后一层,把 raft apply 出来的 mvcc 写(经 backend 攒批后)真正持久化到一个文件里,并提供高效的有序读写。共识(Raft)管"不丢不乱",bbolt 管"不丢不乱的数据,怎么高效地存在一个文件里、又怎么高效地读回来"。具体到本章,bbolt 靠四件事把"高效存 + 高效读"做出来:

1. **单文件 + 定长页**:整库一个文件,内部按 OS 页切,页号 pgid 就是偏移单位,定位/分配/恢复都简化到页级。
2. **统一的 Page 头**:16 字节头 + branch/leaf/meta/freelist 不同元素,定长索引 + 变长 payload,二分查找缓存友好。
3. **B+tree 组织**:branch 装"key → 子页 pgid"路由、leaf 装有序 key/value,从 root 二分下沉,数据全在 leaf。
4. **mmap 只读映射**:文件映射进进程地址空间,读零系统调用、零拷贝,OS 按需 page-in。

### 五个"为什么"清单

1. **为什么 bbolt 把整库放一个文件,还要切成定长页?** 单文件让一次事务的改动都在同一文件里、一次 `fdatasync` 一起落盘,绕开跨文件一致性难题;定长页让定位变成 `偏移 = pgid × pageSize` 一行算术,让分配/回收以页为单位无碎片,让崩溃恢复简化到页级(一页要么完整要么没写)。这两条是 bbolt 一切设计的地基。

2. **branch 页和 leaf 页在字节层面有什么区别?** 两者都有 16 字节 Page 头,但元素不同:branch 元素是 `branchPageElement{pos, ksize, pgid}`(**16 字节,无 value**,只装路由),leaf 元素是 `leafPageElement{flags, pos, ksize, vsize}`(**16 字节,有 value**,装数据)。元素表定长排在前、key/value 字节紧凑排在后,二分只在元素表做。

3. **文件头那四页是什么,meta 为什么有两份?** page 0/1 是两个 meta 页(meta0/meta1),page 2 是 freelist,page 3 是根 bucket 的初始空 leaf。meta 有两份是为了崩溃恢复——每次写事务轮流写其中一个(`p.id = txid % 2`),写坏了还有另一份,启动时 `db.meta()` 取 txid 更大且 `Validate()` 通过的那份(下一章详讲 COW 怎么保证总有一份完整)。

4. **bbolt 的 B+tree 查找怎么走,range 扫描为什么不用 leaf 链表?** 查找 = `cursor.search` 从 root 出发,每层在 branch 元素里 `sort.Search` 二分找到子页号、递归下沉,到 leaf 再二分一次。range 扫描靠 cursor 的栈回溯——leaf 页不存兄弟指针,扫到 leaf 末尾就退回 branch、branch index 右挪、再下沉到下一个 leaf 的首元素。全靠 branch 索引顺序,没有显式 leaf 链表。

5. **mmap 凭什么让读快,写为什么偏不走 mmap?** mmap 把文件只读映射进进程地址空间(`PROT_READ` + `MAP_SHARED` + `MADV_RANDOM`),读一页 = 一次指针解引用 `db.data[pgid × pageSize]`,零系统调用、零内核→用户拷贝,OS 按需 page-in——这是 bbolt 读快的根本。写偏不走 mmap,因为崩溃一致性要求显式控制落盘时机(`WriteAt` + `fdatasync`),mmap 写的内核回写时机不可控。读走 mmap、写走 WriteAt,是明确分工。

### 想继续深入往哪钻

- 想看页的真实字节布局,读 [`bbolt/internal/common/page.go`](../bbolt/internal/common/page.go) 的 `Page`/`branchPageElement`/`leafPageElement` 和它们的 `Key()`/`Value()` 方法——这套 `unsafe.Pointer` 偏移取字节的写法是 bbolt 内存布局的根基。
- 想看 mmap 在不同平台的实现,对比 [`bbolt/bolt_unix.go`](../bbolt/bolt_unix.go)(`PROT_READ` + `MADV_RANDOM`)和 [`bbolt/bolt_windows.go`](../bbolt/bolt_windows.go)(Windows 的 `CreateFileMapping`),以及 `MaxMapSize` 在 [`internal/common/bolt_*.go`](../bbolt/internal/common/) 各平台的差异(amd64 256TB、386/arm 2GB)。
- 想理解 B+tree 的分裂/合并(rebalance/spill),读 [`bbolt/node.go`](../bbolt/node.go) 的 `split`/`spill`/`rebalance`,那是写事务把改动固化进 B+tree 的核心——不过这部分和 COW 紧绑,下一章讲。
- 想看 bbolt 官方怎么描述自己的设计,读 [`bbolt/doc.go`](../bbolt/doc.go),里头有 bbolt 作者写的"单文件、mmap、COW、B+tree"设计综述,和本章对照读会有新体会。

### 引出下一章

这一章我们把 bbolt 的**读路径和静态结构**钉死了——单文件、定长页、B+tree、mmap 只读映射。但留了一个大问题没回答:**写怎么落盘**?bbolt 是怎么做到"写事务改了 B+tree,但读事务看到的始终是一致快照、不被写打断"?meta 页双缓冲凭什么保证崩溃时总有一份完整?freelist 怎么回收被删的页?下一章 P4-16,我们钻进 bbolt 之二:**COW 事务 + freelist**——看这个底座怎么在"读不被写挡"和"崩溃不丢"之间,走出 Copy-On-Write 这条精妙的路。
