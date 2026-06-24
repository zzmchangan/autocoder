# 第 11 章 · page cache:文件 IO 的"内存面"

> 进程读写文件,数据其实从不在磁盘和进程之间直接搬——中间永远隔着一层"大楼里的房间"。这一章讲的是:这层中转货架(page cache)为什么必要,以及脏页、写回、回收如何在这个货架上博弈。

---

## 章首 · 核心问题

到此为止,我们一直在讲"物理房间怎么切、虚拟幻象怎么造"。但有一个最日常的现象,我们还没解释:

> 你写一段程序,第一次读某个文件,慢;**第二次再读同一个文件,快得离谱**。为什么?

或者更工程化的问题:

> 进程 `read()` 一个文件,数据是怎么从磁盘跑到进程内存里的?写一个文件,数据又经历了什么?为什么明明机器只有 16GB 内存,你却常看到"几乎全被占满、free 只剩几百 MB",但系统照样飞快?

答案全部指向同一个东西:**page cache(页缓存)**。本章的核心困惑是:

> **磁盘和内存之间,为什么一定要有一层"文件数据的内存缓存"?这层缓存是怎么和前面讲的物理页管理、page fault、回收搅在一起的?为什么 Linux 宁可让内存看起来"满着",也不让它闲着?**

**读完本章你会明白:**

- page cache 是什么:磁盘文件内容在大楼房间里的一份"暂存副本",是文件读写的**真正战场**;
- 为什么每个文件要有一个 `address_space`——它就是"这个文件在内存里的那张页表(目录)";
- 为什么内核这几年要把 `page` 升级成 `folio`(一句话:大小不再绑死 4KB);
- 读路径三步走:查缓存 → 没有就 readahead 预读 → 等数据到位;
- 写路径的真相:**写,不是写磁盘,而是写内存里的副本(变"脏")**,再择机写回;
- 脏页(dirty)、写回(writeback)、回收(reclaim)这个**三角关系**,以及它们如何共同支撑"内存当磁盘用"。

一句话概括本章在大楼比喻里的位置:**磁盘是大楼外面的远端仓库,取一趟要几分钟。page cache 是大楼一层的中转货架:进程要的文件货物,先从仓库搬一批到货架上;以后谁再要同样的货,直接从货架拿,不用再去仓库。改过的货先在货架上改(贴个"待送回"的脏标签),攒一阵子统一送回仓库。**

> **如果一读觉得太难**:先只记住三件事——
> ① **读文件先查 page cache,命中就直接拿,没命中才去磁盘,这就是"第二次快"的全部原因**;
> ② **写文件只是把内存里的副本改脏(dirty),真正的写盘(writeback)是另一步、择机批量做的**;
> ③ **`address_space` = 一个文件在 page cache 里的"目录",里面用一棵 xarray 树按页号挂缓存的页**。
>
> 其余细节(folio 的大页演进、writeback 的节流算法、回收三角)第二遍配合 `/proc/meminfo` 再抠,不影响理解后续章节。

---

## 一、为什么文件读第二次快得多:缓存的动机

### 先算一笔账

磁盘有多慢?一块 SSD 一次随机读大约几十微秒、吞吐几 GB/s;机械硬盘更惨,一次寻道几毫秒。而内存呢?一次访问几十纳秒,吞吐几十 GB/s。**磁盘比内存慢上万到上百万倍。**

这个鸿沟意味着:如果每次 `read()` 都老老实实去磁盘搬数据,你的程序会被磁盘拖死。可现实是,程序读文件几乎总是**带局部性的**:

- 同一个文件,刚才读过,**等会儿还会再读**(时间局部性);
- 读第 100 字节,**大概率接着读 200、300 字节**(空间局部性);
- 临时文件、配置文件、可执行代码,**被反复加载**。

### 不这样会怎样

设想没有 page cache 的世界:

- `cat` 一个文件两次 → 两次都去磁盘,第二次明明数据刚到过内存,却又要重搬一遍,浪费到离谱;
- 多个进程读同一个文件 → 每个进程都从磁盘独立拉一份,磁盘被打爆;
- 进程 `mmap` 一个文件(把文件映射进虚拟地址,见第 10 章)→ 每次访问没在内存的页都触发一次磁盘 IO,卡顿肉眼可见。

### 所以这样设计:在大楼里给文件开中转货架

内核的做法是:**所有文件读写,都强制经过内存**。具体说,磁盘文件的"一页内容"会被搬进一个**物理页**,这个页挂在 page cache 里。于是:

- 进程 `read()` → 先看 page cache 有没有这一页,**有(命中)**就把数据从这页 copy 到进程的用户缓冲区,**完全不碰磁盘**;
- **没有(未命中)**→ 才去磁盘读,读进来顺手放进 page cache(顺便多读几页,见"预读"),再 copy 给进程;
- 下次再读这页 → 命中了,飞快。

> **比喻**:磁盘是远端仓库,取一趟要很久。物业在一楼大厅摆了个**中转货架(page cache)**。第一次有人要某批货,物业派人去仓库取回来、**摆在货架上**再给住户。第二次同样的货,直接从货架拿——秒到。改过的货先在货架上改、贴个"脏"标签,等货架快满或空闲时,物业统一把脏货送回仓库。

这就是 `read` 第二次快的根本原因:**数据已经在内存里了,根本没去碰磁盘。**

> **小白补一句**:你平时看到的"内存几乎满了"现象,绝大部分就是 page cache。Linux 的态度是:**空闲的房间不如拿来当货架**,反正闲着也是闲着;等真有进程要内存、房间不够了,货架上的"干净货"(没改过的副本)可以随时丢掉腾地方(见第六节)。所以 `free` 很低不代表不健康——`free -h` 里那块 `buff/cache` 就是货架。

---

## 二、`address_space`:每个文件一个"内存面"

现在我们知道要把文件内容缓存在内存页里了。下一个问题:这些缓存页**怎么组织**?总不能乱堆。

### 不这样会怎样

进程说"我要读文件 F 的第 100 页",内核必须**快速回答**:这一页在不在缓存里?如果在,在哪间房间?如果没有任何索引结构,你只能**遍历所有缓存页**逐个比对——O(N) 查找,根本不能用。

更糟的是,一个文件可能被**多个进程、多种方式**访问(有人 `read`,有人 `mmap`,有人正在写)。这些访问都要找到"同一份缓存",否则就出现"各读各的、数据不一致"的灾难。

### 所以这样设计:每个文件一棵缓存树

内核给每个**可缓存、可映射的对象**(普通文件、块设备、shmem 临时文件……)挂一个 `struct address_space`。它就是"这个文件在 page cache 里的目录":

```c
struct address_space {
    struct inode        *host;          /* 宿主:通常是文件对应的 inode */
    struct xarray       i_pages;        /* 按"页号"挂缓存页的核心树 */
    struct rw_semaphore invalidate_lock;
    ...
    unsigned long       nrpages;        /* 这个文件目前在缓存里有几页 */
    pgoff_t             writeback_index;/* 下次写回从哪个位置开始 */
    const struct address_space_operations *a_ops;  /* 文件系统提供的操作表 */
    unsigned long       flags;
    errseq_t            wb_err;
    ...
};
```

([include/linux/fs.h:502-522](../linux-6.14/include/linux/fs.h#L502-L522),**简化示意**:保留核心字段与源码原注释,省略锁与 NUMA/THP 相关字段)

读这个结构,几个关键字段:

- **`i_pages`** 是一棵 **xarray**(xarray 是早期 radix-tree 的现代升级版,一种按整数键高效查找的树)。键是**页号**(`pgoff_t`,文件内第几页),值是缓存的 `folio`。"文件第 100 页在不在缓存里?" → 在 `i_pages` 里查键 100,O(1)~O(log) 搞定。
- **`host`** 指向文件的 inode——`address_space` 归属的"主人"。
- **`a_ops`** 是**操作表**(下一节详讲),由具体文件系统(ext4、xfs……)填好,告诉 page cache 层"真要去读盘/写盘时,该调哪个函数"。
- **`nrpages`** 是这个文件当前缓存了多少页——`/proc/meminfo` 里那一大坨 page cache,就是所有 `address_space` 的 `nrpages` 加总。

> **小白补一句**:`struct address_space` 这个名字容易误导——它和"虚拟地址空间"没关系。它其实应该叫"page cache index"之类。它**只服务于"文件内容缓存"这一件事**。记住:`address_space` = 一个文件在 page cache 里的目录树。

> **为什么用树(xarray)而不是数组?** 因为一个 1TB 的文件有 2.6 亿页,真给每个文件开个 2.6 亿项的数组,光索引就比内存大。树结构只存"实际缓存了的那几页"的路径,**稀疏且省内存**——这跟第 8 章多级页表"只为用到的地址建表"是同一种思想。

### `a_ops`:page cache 和具体文件系统之间的"插头"

page cache 这一层是**通用的**(它不关心你是 ext4 还是 xfs),但真正"读盘/写盘"的活是文件系统特定的。两者怎么解耦?靠 `a_ops`:

```c
struct address_space_operations {
    int (*writepage)(struct page *page, struct writeback_control *wbc);
    int (*read_folio)(struct file *, struct folio *);
    int (*writepages)(struct address_space *, struct writeback_control *);
    bool (*dirty_folio)(struct address_space *, struct folio *);
    void (*readahead)(struct readahead_control *);
    int (*write_begin)(struct file *, struct address_space *mapping,
                loff_t pos, unsigned len, struct folio **foliop, void **fsdata);
    int (*write_end)(struct file *, struct address_space *mapping,
                loff_t pos, unsigned len, unsigned copied,
                struct folio *folio, void **fsdata);
    ...
};
```

([include/linux/fs.h:434-476](../linux-6.14/include/linux/fs.h#L434-L476),**简化示意**:省略 `bmap`、`migrate_folio`、swap、invalidate 等回调,保留与本章读写路径相关的方法)

这张表就是一组**函数指针**:page cache 层需要"把这页从磁盘读进来",就调 `a_ops->read_folio`;需要"把这脏页写回磁盘",就调 `a_ops->writepage`。具体怎么读怎么写,由 ext4/xfs 各自实现。**page cache 只管"在内存里怎么组织、何时读何时写",磁盘细节交给文件系统**——经典的分层解耦。

---

## 三、`folio`:为什么要把 `page` 升级

你会在源码里看到 page cache 里存的不是 `struct page`,而是 `struct folio`。这一节解释这个相对新(5.x 之后大规模推进)的变化。

### 不这样会怎样

历史上,page cache 和整个内存管理的最小单位都是 `struct page`,代表**一页(4KB)**。可是硬件和需求在演进:

- 现代 CPU 支持 **2MB / 1GB 大页**,TLB 命中率更高、页表更省;
- 大内存服务器、数据库希望"一次管理一大块连续内存",而不是一页一页数;
- 存储 IO 也希望一次搬一大块,减少中断和开销。

问题来了:`struct page` 这个类型,内部有个标志位表示"我其实是几个连续页组成的大页的 head"。于是代码里到处是"`if (这是 head page) { 当大页处理 } else { 当普通页处理 }`"——**每个调用点都得判一次**,忘了就出 bug。这种"一个类型两种含义"的设计,是 bug 温床。

### 所以这样设计:`folio` = "明确知道自己多大"的一整块

`struct folio` 的核心改进就一句话:**它显式、确定地代表"一段连续的、2 的幂大小的内存",不再像 page 那样含糊**。源码注释把它的定义写得很清楚:

```c
/*
 * A folio is a physically, virtually and logically contiguous set
 * of bytes.  It is a power-of-two in size, and it is aligned to that
 * same power-of-two.  It is at least as large as %PAGE_SIZE.
 */
struct folio {
    ...
};
```

([include/linux/mm_types.h:316-324](../linux-6.14/include/linux/mm_types.h#L316-L324))

一个 folio 可能就是 1 个 4KB 页,也可能是 2MB 大页(= 512 个连续页捆在一起)。**关键是:拿到一个 `folio *`,你不用再猜它是大是小,`folio_size(folio)` 直接告诉你**。所有操作(加锁、计数、引用)都以 folio 为单位,语义统一。

> **为什么要费这么大劲改?** 因为 page cache 是大页(THP for fs、大块 IO)最自然的受益者:一份大文件,如果缓存能用 2MB 的 folio 而不是 512 个 4KB 的 page,**页表项少 512 倍、TLB 友好 512 倍、IO 一次搬一大块**。但要享受这个好处,代码层就得有一个"天生就是大块"的类型。`folio` 就是为这个目标做的底层重构——它把"page 永远是 4KB"这个历史假设拆掉,让整条链路都能处理"任意 2 的幂大小的块"。

> **小白一句话**:`page` 是"一页(4KB)",`folio` 是"一整块(≥4KB,可能是大页)"。page cache 现在以 folio 为单位管理。本书后面提到"缓存页",你心里可以把它当 folio。这个重构是渐进的,你会在代码里看到两者并存——但方向是 folio。

---

## 四、读路径:查缓存 → 预读 → 等数据

现在我们跟一次真实的 `read()` 走,看 page cache 怎么工作。入口在 [filemap_read()](../linux-6.14/mm/filemap.c#L2664)。

### 一次 read 的骨架

```c
ssize_t filemap_read(struct kiocb *iocb, struct iov_iter *iter,
        ssize_t already_read)
{
    struct file *filp = iocb->ki_filp;
    struct address_space *mapping = filp->f_mapping;   /* 这个文件的缓存目录 */
    ...
    do {
        ...
        error = filemap_get_pages(iocb, iter->count, &fbatch, false);  /* ① 把需要的页弄进缓存 */
        ...
        for (i = 0; i < folio_batch_count(&fbatch); i++) {
            struct folio *folio = fbatch.folios[i];
            ...
            copied = copy_folio_to_iter(folio, offset, bytes, iter);    /* ② 从缓存页 copy 到用户缓冲 */
            ...
        }
        ...
    } while (iov_iter_count(iter) && iocb->ki_pos < isize && !error);
    ...
}
```

([mm/filemap.c:2664-2777](../linux-6.14/mm/filemap.c#L2664-L2777),**简化示意**:省略 `iocb` 标志判断、`writably_mapped`、`flush_dcache` 等细节,保留"取页 → copy"主循环;`①` `②` 为本书所加讲解标注,源码原文无)

两步:**① 把要读的页弄进 page cache;② 从缓存页把数据 copy 到用户空间**。第 ② 步是纯内存 copy,不碰磁盘。**慢就慢在第 ① 步——如果页没在缓存里,得去磁盘搬。** 关键看 `filemap_get_pages`:

```c
static int filemap_get_pages(struct kiocb *iocb, size_t count,
        struct folio_batch *fbatch, bool need_uptodate)
{
    ...
    filemap_get_read_batch(mapping, index, last_index - 1, fbatch);   /* ① 先查缓存,把已有的页收进 batch */
    if (!folio_batch_count(fbatch)) {
        DEFINE_READAHEAD(ractl, filp, &filp->f_ra, mapping, index);
        ...
        page_cache_sync_ra(&ractl, last_index - index);               /* ② 一个都没有 → 触发预读:去磁盘读一批 */
        filemap_get_read_batch(mapping, index, last_index - 1, fbatch);/*    再查一次 */
    }
    ...
    folio = fbatch->folios[folio_batch_count(fbatch) - 1];
    if (folio_test_readahead(folio)) {
        err = filemap_readahead(iocb, filp, mapping, folio, last_index); /* ③ 异步预读窗口扩展 */
        ...
    }
    if (!folio_test_uptodate(folio)) {
        ...
        err = filemap_update_page(iocb, mapping, count, folio, need_uptodate); /* ④ 等正在读的页读完 */
        ...
    }
    ...
}
```

([mm/filemap.c:2563-2619](../linux-6.14/mm/filemap.c#L2563-L2619),**简化示意**:省略 NOIO/NOWAIT/DONTCACHE 等标志处理与 `AOP_TRUNCATED_PAGE` 重试,保留"查缓存 → 预读 → 等"主流程)

这段是 page cache 读路径的精髓,对应"不这样会怎样 → 所以这样设计":

1. **`filemap_get_read_batch`**:在 `i_pages` 里查,把范围内**已经在缓存**的页收进 batch。这一步命中就什么都不用做——直接进第 ② 步 copy。**这就是"第二次读快"的代码所在。**

2. **`page_cache_sync_ra`(sync readahead,同步预读)**:如果一个页都没有,触发预读。注意不是"只读你要的那一页",而是**推测你会接着读,一次性从磁盘读进来一批**。为什么?见下面的"预读"小节。

3. **`filemap_readahead`**:如果命中的最后一页恰好标记了"这是预读读进来的",就**继续扩大预读窗口**——读得越多越快,说明局部性强,再预读多点。

4. **`filemap_update_page`**:如果页正在被读(别的 IO 刚把它标记成"正在读"但还没读完),就**等它读完**(`uptodate` 标志)。

### 预读(readahead):为什么不老老实实读一页

这是一个非常体现"为什么"的点。

> **不这样会怎样**:如果每次未命中都只读你要的那一页,那读一个 100 页的文件就是 100 次磁盘往返——每次寻道、每次中断,慢到哭。而磁盘的特性是**顺序读吞吐远高于随机读**:一次连续读 16 页,比读 16 次单页快得多。

> **所以这样设计**:既然你刚读了第 N 页,大概率马上要读 N+1、N+2……那内核**赌**一把,趁这次去磁盘,**顺手多读几页**进 page cache。赌对了,你下次读 N+1 时直接命中,白赚一次磁盘往返;赌错了(你没接着读),多读的页占点缓存,反正缓存满了能回收。**用"可能浪费的一点预读"换"大概率省下的几十次磁盘 IO",稳赚。**

预读窗口是**自适应**的:内核记录每个文件描述符的"预读历史",如果你一直顺序读,窗口越来越大(读到几十、上百页);如果你随机跳着读,窗口缩到最小,避免无谓预读。

> **比喻**:物业去仓库取货,住户要"第 100 号货"。物业想:上次他拿了 99,这次 100,下次大概率 101、102……于是**一次从仓库搬 100~120 号**回来摆上货架。住户下次要 101,直接从货架拿。物业赌的就是"住户通常顺序取货"。

---

## 五、写路径:写,其实是"把内存改脏"

读路径已经把 page cache 讲清楚了。写路径会给你一个**反直觉但极其关键**的认知:

> **进程 `write()` 一个文件,数据并没有写进磁盘——它写进了 page cache 里的内存页,然后这个页被标记成"脏(dirty)"。真正的写盘,是另一回事。**

### 写路径骨架

入口 `generic_perform_write`([filemap.c:4074](../linux-6.14/mm/filemap.c#L4074)):

```c
ssize_t generic_perform_write(struct kiocb *iocb, struct iov_iter *i)
{
    ...
    struct address_space *mapping = file->f_mapping;
    const struct address_space_operations *a_ops = mapping->a_ops;
    ...
    do {
        ...
        balance_dirty_pages_ratelimited(mapping);                 /* ① 检查脏页是否太多,必要时阻塞/触发写回 */

        ...
        status = a_ops->write_begin(file, mapping, pos, bytes,    /* ② 找/建目标页,加锁,准备写 */
                        &folio, &fsdata);
        ...
        copied = copy_folio_from_iter_atomic(folio, offset, bytes, i); /* ③ 把用户数据 copy 进缓存页 */
        ...
        status = a_ops->write_end(file, mapping, pos, bytes, copied,  /* ④ 收尾:标记脏页 */
                        folio, fsdata);
        ...
    } while (...);
    ...
}
```

([mm/filemap.c:4074-4130](../linux-6.14/mm/filemap.c#L4074-L4130),**简化示意**:省略 `chunk` 回退、信号、`flush_dcache` 等,保留"节流 → write_begin → copy → write_end"主循环;`①`~`④` 为本书所加讲解标注,源码原文无)

四步里,**真正"写"的动作是第 ③ 步 `copy_folio_from_iter_atomic`——但它 copy 的目的地是内存里的缓存页,不是磁盘。** 第 ④ 步 `write_end` 之后,这个页被打上**脏标记**(`PAGECACHE_TAG_DIRTY`),表示"我和磁盘上的版本不一致了,得找机会写回"。

### 不这样会怎样:为什么"写"要先落到内存

设想"每次 write 都立刻写盘":

- 进程 `write(fd, buf, 1)` 写 1 字节 → 立刻一次磁盘往返。可进程常常是"一个字节一个字节地写"或者"密集小写",每次都写盘,磁盘被小 IO 打爆,慢到无法用;
- 而且**磁盘的最小写入单位通常是一个扇区/一个块**(512B~4KB),你写 1 字节它也得读出整个块、改那 1 字节、写回整个块——开销根本不在那 1 字节上。

### 所以这样设计:在内存里攒着,批量写回

写路径的设计是:**写到 page cache(内存)就立刻返回**——对进程来说"写完了",飞快。真正的写盘(**writeback**)交给内核**择机、批量**做:

- 内核有专门的**写回线程**(flusher / `kworker`,per bdi),周期性或被脏页比例触发,把一批脏页一次性 `writepage` 写回磁盘;
- 这样:**进程的写极快(纯内存)**;**磁盘 IO 被合并成大批量**,吞吐最高;**断电风险**靠 fsync 等显式同步来兜底(你真要落盘,调 fsync 强制写回并等待)。

> **比喻**:住户改货,不立刻送回远端仓库(那要跑一趟),而是**在货架上改、贴个"脏"标签**。物业定期巡货架,把所有脏标签的货**集中一趟送回仓库**。住户觉得"改完了"(瞬间),物业也省了无数趟跑腿。代价是:如果大楼突然停电(断电),货架上的脏货还没送回仓库就会丢——所以重要货物住户可以喊一声"立刻送回并等确认"(fsync)。

### 脏页怎么标记:`folio_mark_dirty`

```c
bool folio_mark_dirty(struct folio *folio)
{
    struct address_space *mapping = folio_mapping(folio);

    if (likely(mapping)) {
        ...
        return mapping->a_ops->dirty_folio(mapping, folio);   /* 调文件系统的脏标记实现 */
    }

    return noop_dirty_folio(mapping, folio);
}
```

([mm/page-writeback.c:2887-2909](../linux-6.14/mm/page-writeback.c#L2887-L2909))

它最终调 `a_ops->dirty_folio`,在 xarray 上给这个页打上 `PAGECACHE_TAG_DIRTY` 标记。注意 fs.h 里这几个标记是 xarray 的"标签(marks)":

```c
/* XArray tags, for tagging dirty and writeback pages in the pagecache. */
#define PAGECACHE_TAG_DIRTY     XA_MARK_0
#define PAGECACHE_TAG_WRITEBACK XA_MARK_1
#define PAGECACHE_TAG_TOWRITE   XA_MARK_2
```

([include/linux/fs.h:529-532](../linux-6.14/include/linux/fs.h#L529-L532))

**为什么要用 xarray 的 mark 而不是遍历树?** 因为写回线程要回答"这个文件有哪些脏页?"——有了 `PAGECACHE_TAG_DIRTY` 这个 mark,内核能在 xarray 上**直接按 mark 跳着找**,不用遍历每个节点。`PAGECACHE_TAG_WRITEBACK` 标记"正在写回",`TOWRITE` 标记"这次打算写回"。三个 mark 支撑了脏页、写回、回收的全部分类操作。

### 节流:`balance_dirty_pages_ratelimited`

回到写路径第 ① 步 `balance_dirty_pages_ratelimited`。这是写路径里的"刹车":

> **不这样会怎样**:如果写进程只管往 page cache 灌、内核慢慢写回,那当写入速度远超磁盘写回速度时,脏页会**无限堆积**,直到把内存塞满。然后一旦触发大规模写回,进程被长时间卡住;更糟的是断电时丢一大片。

> **所以这样设计**:每次写之前,按一定频率检查"本设备的脏页比例"。超过阈值就**让写进程停下来等(或者逼它去帮忙写回)**,把脏页比例压回去。这就是 `balance_dirty_pages`([page-writeback.c:1834](../linux-6.14/mm/page-writeback.c#L1834))做的事——**写快了就踩刹车,保证脏页量可控**。`_ratelimited` 后缀表示"不是每次写都查,而是按速率采样检查",省开销。

这是写路径的**负反馈**:写得太猛 → 脏页涨 → 节流 → 写进程慢下来 → 脏页降 → 解除节流。系统因此稳定。

---

## 六、三角关系:脏页 / 写回 / 回收

到这里,page cache 上其实站着三个角色,它们的关系是本章最难也最关键的部分。

### 三个角色

- **脏页(dirty)**:在内存里改过、还没写回磁盘的页。它**不能直接丢**——丢了改动就没了。
- **写回(writeback)**:把脏页的内容真正写到磁盘的过程。写回完成后,脏页变成"干净页"。
- **回收(reclaim)**:内存紧张时,把 page cache 里**不再需要的页**腾出来给别的用途(详见第 12 章)。

### 三角怎么博弈

```
                 进程 write
                     │
                     ▼
              ┌──────────────┐
              │  脏页 (dirty) │ ◄── 打 PAGECACHE_TAG_DIRTY
              └──────┬───────┘
        writeback    │     reclaim 来敲门
        线程搬走     │
                     ▼
              ┌──────────────┐
              │ WRITEBACK 中  │ ── 写回完成 ──┐
              └──────────────┘              ▼
                                  ┌──────────────┐
              ┌──────────────┐    │   干净页      │ ◄── 内容 == 磁盘
              │  (丢掉即可)   │ ◄──│ (clean)      │   reclaim 可直接丢
              └──────────────┘    └──────────────┘
```

关键规则:

1. **回收干净页 = 直接丢**:干净页的内容和磁盘一致,丢了下次需要时从磁盘重读即可,**零数据风险**。这是回收的首选目标。
2. **回收脏页 = 先写回再丢**:脏页不能直接丢(会丢改动),必须先触发 writeback 把它变干净,然后才能回收。所以脏页是回收的"贵"目标。
3. **WRITEBACK 中的页 = 等它写完**:正在写回的页,回收时要么等它写完,要么跳过。

> **这就是 page cache 能"当磁盘用"还不出乱子的根本**:回收时**严格区分脏/干净**,干净页随便丢、脏页必先落盘。数据安全永远优先于腾房速度。

### 主动写回 vs 被动写回

写回有两个触发时机:

- **周期性/后台**:flusher 线程定期醒来,把存活超过一定时间的脏页写回(避免脏页"老"到断电丢太多);
- **阈值触发**:脏页比例到高水位(`balance_dirty_pages` 的刹车,或全局内存紧张),立刻大规模写回。

这两个机制配合,让脏页量在"平时少量积攒、压力下快速消化"之间动态平衡。

---

## 七、为什么"用空闲内存当缓存",而不是让内存闲着

这是 Linux 内存哲学最直白、也最常被误解的一条。

### 不这样会怎样

设想一个"洁癖"系统:文件读进来用完立刻从 page cache 丢掉,内存时刻保持大量 `free`。

- 第二次读同一个文件 → cache miss,又得去磁盘,前面所有优化白费;
- 内存里那大片 `free` → 闲着,**没产生任何价值**;
- 进程要内存时,确实不用等回收(这是唯一好处),但代价是持续性的磁盘 IO 放大。

### 所以这样设计:能缓存就缓存,直到被需要才让

Linux 的哲学是:**空闲的房间不如拿来当货架**。

- 有空闲内存 → 照单全收地缓存文件页(`buff/cache` 涨到很大);
- 进程要内存、空闲不够了 → **从货架上挑页腾地方**:干净页直接丢、脏页先写回;
- 因为回收是"按需"的,`free` 看起来很低但**不代表内存紧张**——只要 cache 充足、能快速腾退,系统就健康。

> **这就是为什么 `free -h` 里 `free` 很低但你不必慌**:那不是内存泄漏,是 page cache 在干活。真正该看的是 `available`(可腾退的总量)和 swap 是否被大量使用。

> **比喻**:物业的准则:**楼里的空房间绝不让它真空着**。有住户的货就往里摆(缓存)。等真有新住户要房间,物业再从货架上挑"最久没碰的、干净的"货撤掉腾房。空着 = 浪费;摆满 = 物尽其用。

这个"贪婪缓存 + 按需回收"的策略,把内存利用率推到极致:**同一份物理内存,既是磁盘的高速缓存,又能在需要时立刻变成进程的私有房间**——两种身份按需切换。

---

## 关键源码精读:读路径 `filemap_read` 与写路径 `generic_perform_write`

本章最值得对照的两条路径,我们把它们的"骨架 ↔ 设计"再对一遍。

### 1. 读:`filemap_read` 的命中与未命中

核心是 `filemap_get_pages` 的"查 → 预读 → 等"三段式([filemap.c:2563-2619](../linux-6.14/mm/filemap.c#L2563-L2619))。对应的设计:

| 代码动作 | 对应设计 | 解决的痛点 |
|---|---|---|
| `filemap_get_read_batch` 查 `i_pages` | 命中即返回 | 第二次读不碰磁盘 |
| `page_cache_sync_ra` 预读一批 | 顺序访问时一次读多页 | 省磁盘往返、利用顺序 IO 高吞吐 |
| `filemap_update_page` 等 `uptodate` | 等正在进行的 IO | 避免读到半成品页 |

> **一个易被忽略的细节**:`filemap_read` 里对命中的页会调 `folio_mark_accessed`([filemap.c:2730](../linux-6.14/mm/filemap.c#L2730))。它更新这一页的"访问时间"——这正是第 12 章 LRU 回收判断"谁该被丢"的依据。**读路径在悄悄给回收路径喂数据**:你访问过的页,回收时优先保留。

### 2. 写:`generic_perform_write` 的"写到内存 + 标脏 + 节流"

核心四步([filemap.c:4074-4130](../linux-6.14/mm/filemap.c#L4074-L4130))对应的设计:

| 代码动作 | 对应设计 | 解决的痛点 |
|---|---|---|
| `balance_dirty_pages_ratelimited` | 脏页节流刹车 | 防止脏页无限堆积 |
| `a_ops->write_begin` | 找/建缓存页并加锁 | 保证写入原子性 |
| `copy_folio_from_iter_atomic` | **写到内存**(不是磁盘) | 写极快 + IO 可合并 |
| `a_ops->write_end` | 标脏(`PAGECACHE_TAG_DIRTY`) | 等待择机批量写回 |

> **把读、写两条路径并排看,你会发现 page cache 的统一模式**:**进程永远只和内存打交道**(读从缓存 copy 出、写到缓存 copy 进),磁盘 IO 是内核在背后悄悄做的"补货/回货"。这就是"文件 IO 的内存面"这个名字的来历——进程看到的"文件",其实是内存里的一面缓存,磁盘只在 cache miss 和 writeback 时才登场。

### 3. `__filemap_get_folio`:查不到就建一个

还有一个底层原语值得认识:`__filemap_get_folio`([filemap.c:1898](../linux-6.14/mm/filemap.c#L1898))——"给我这个页号的缓存页,没有就按需创建":

```c
struct folio *__filemap_get_folio(struct address_space *mapping, pgoff_t index,
        fgf_t fgp_flags, gfp_t gfp)
{
    struct folio *folio;

repeat:
    folio = filemap_get_entry(mapping, index);   /* 查 i_pages */
    ...
    if (!folio)
        goto no_page;
    ...
no_page:
    if (!folio && (fgp_flags & FGP_CREAT)) {     /* 调用方要求"没有就建" */
        ...
        /* 分配一个新 folio,加进 i_pages */
    }
    ...
}
```

([mm/filemap.c:1898-1957](../linux-6.14/mm/filemap.c#L1898-L1957),**简化示意**:省略锁、order 计算、`FGP_LOCK`/`FGP_WRITE` 分支,保留"查不到 + FGP_CREAT 就建"主逻辑)

这个"查不到就建"的模式,正是 page fault 触发文件页读取的底层入口——第 9 章 page fault 处理 `mmap` 的文件页时,最终也会走到这里:**fault → 查 page cache → 没有就建页并触发 readpage → 填充数据**。所以 page cache 既是 `read/write` 的中转,也是 `mmap` 的靠山。

---

## 章末小结

回到大楼比喻:

> 磁盘是远端仓库,取一趟很慢。物业在一楼大厅摆了个**中转货架(page cache)**。住户要某批货(`read`),物业先看货架——有就直接给(命中,秒到);没有就去仓库**一次搬一批回来**(预读,赌住户会顺序取货),摆上货架再给。住户改货(`write`),物业**只在货架上改、贴个"脏"标签**,瞬间返回;物业的写回工定期或被脏标签堆满时,把脏货**集中一趟送回仓库**(writeback)。要是货架上货太多、新住户要房间,物业就挑**干净的货(和仓库一致)直接撤掉**腾房,脏的则先送回仓库再撤。物业的准则:**空房间绝不让它空着**,能摆货就摆货,等真要腾房再腾。

### 本章在全书主线中的位置

记住导言的二分法:**物理一侧管"切房间",虚拟一侧管"造幻觉"**。本章在哪一侧?

**严格说,page cache 横跨两侧,但本质是"物理房间的另一种用途"。** 它把物理页(物理侧的房间)拿来当**磁盘文件的内存缓存**——房间还是那些房间,只是装的东西从"进程私有数据"变成了"文件内容的副本"。它和前面几章的关系是:

- **往下**看:page cache 的每一个页,都是**从伙伴系统要来的物理页**(第 6 章),只是这个页被登记进了某个 `address_space` 的 `i_pages` 树;
- **往上**看:它服务虚拟侧——进程 `read`/`write`/`mmap` 文件时,数据在 page cache 里中转;`mmap` 的文件页通过 page fault(第 9 章)和 page cache 衔接;
- **往后**看:page cache 是第 12 章**回收的主战场**——"干净文件页直接丢、脏页写回再丢、匿名页 swap"这三种策略里,前两种都是针对 page cache 的。

也就是说:**page cache 是物理房间"兼职"当磁盘中转货架的机制,是物理侧和虚拟侧、内存和磁盘之间的交汇点。** 理解了它,你就理解了"为什么内存看起来总是满的""为什么文件读第二次快""为什么断电会丢未保存的数据"这一连串现象。

### 五个"为什么"清单

1. **为什么读文件第二次快**:第一次把磁盘内容搬进了 page cache(内存),第二次直接命中、不碰磁盘——内存比磁盘快上万倍。
2. **为什么每个文件要有 `address_space`**:它是"这个文件在 page cache 里的目录树(xarray)",让"第 N 页在不在缓存"能 O(log) 查到,且多个进程共享同一份缓存。
3. **为什么写只是"标脏"而不是立刻写盘**:小写频繁、磁盘最小单位大、批量写回吞吐高——所以写到内存就返回,真正的写盘(writeback)是另一步、择机批量做,靠 fsync 兜底落盘。
4. **为什么有 `balance_dirty_pages` 这个刹车**:防止写太快导致脏页无限堆积(断电丢数据、回收卡顿),用负反馈把脏页比例压在阈值内。
5. **为什么内存宁可"满着"也要拿去当缓存**:空闲房间闲着是浪费,当货架有价值;真要房间时干净页随时可丢、脏页写回可丢——所以 `free` 低 ≠ 不健康,`available` 才是关键。

### 想继续深入,该往哪钻

- **看读路径全貌**:精读 [filemap_read()](../linux-6.14/mm/filemap.c#L2664) → [filemap_get_pages()](../linux-6.14/mm/filemap.c#L2563) → [__filemap_get_folio()](../linux-6.14/mm/filemap.c#L1898),跟着"查缓存 → 预读 → 等数据"走一遍。
- **看预读算法**:精读 `page_cache_sync_ra` / `page_cache_async_ra`(在 [mm/readahead.c](../linux-6.14/mm/readahead.c)),看预读窗口怎么自适应扩大缩小。
- **看写回总管**:精读 [balance_dirty_pages()](../linux-6.14/mm/page-writeback.c#L1834) 和 `wb_workfn`(写回线程的工作函数),看脏页阈值怎么算、怎么节流。
- **看 folio 的全貌**:读 [struct folio](../linux-6.14/include/linux/mm_types.h#L324) 和它的访问器,理解 `folio` / `page` 的关系,以及 `Documentation/` 下关于 folio 迁移的设计文档。
- **可观测面**:`free -h` 看 `buff/cache` 和 `available`;`cat /proc/meminfo` 看 `Cached:`、`Dirty:`、`Writeback:`;`cat /proc/vmstat | grep -E 'pgmajfault|nr_dirty|nr_writeback|writeback'`;`vmsatat`、`iostat` 配合看缓存命中率。

> 中转货架运转起来了,读写文件都飞快。可货架再大也有限——当住户(进程)疯狂占地、连货架都得腾退还挤不下时,内核就得**决定牺牲谁**:谁的房子被收、谁被请出大楼?——翻到**第 12 章 · 回收、swap 与 OOM:内存不够时怎么办**。
