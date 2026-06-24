# 第 30 章 · page cache 再访:文件数据的内存面

> **前置**:你需要先读过**内存篇第 11 章《page cache》**(理由:那一章已经把 page cache 作为"磁盘文件的内存缓存"讲清了它的内存管理面——脏页、回收、和 LRU 的关系。本章站在**文件系统**的视角再走一遍,讲清"一次 read/write 怎么穿过它"。两边拼起来才是完整的 page cache)。同时你需要读过**第 28、29 章**,知道 `struct file → inode → address_space` 这条链。

**本章核心问题**:内存篇讲过 page cache 是"磁盘文件的内存缓存",这一章要换到文件系统视角看清楚——市民一次 `read`/`write`,到底是怎么穿过 page cache 的?它和普通的(匿名)内存到底什么关系?

**读完本章你会明白**:
- `address_space` 这个对象为什么是"文件系统和内存管理的接头点",它的 `i_pages` 这棵 xarray 在干什么;
- 读路径:`filemap_fault` 如何"先查前台、没货再下仓库",以及 readahead(预读)为什么"顺手多读"反而更快;
- 写路径:`write_begin`/`write_end` 的"借页—拷贝—标记脏"三段式,为什么写比读复杂得多;
- page cache 里的页和进程的匿名页有什么本质区别(这是理解回收篇的前置)。

> **如果一读觉得太难**:先只记住三件事——① `address_space` 是"一个文件一份页缓存账本",`host` 指回 inode;② 读就是"查账本,有就给、没有就触发预读把货从磁盘搬来";③ 写就是"借一页、把数据拷进去、标记成脏(等以后写回)",**write 之后数据还没真到磁盘**。其余细节第二遍配合源码再抠。

---

## 一、不缓存会怎样:`ls` 都得卡半秒

先想清楚没有 page cache 的灾难。

市民 `cat /etc/hosts` 第一次读,内核去磁盘取了 4KB。好,数据回来了。五秒后市民又 `cat /etc/hosts`——**如果没缓存,内核又得去磁盘取一遍这同样的 4KB**。

磁盘有多慢?一次随机 4KB 读,机械硬盘要几毫秒(寻道 + 旋转延迟),SSD 也要几十到几百微秒。而内存访问只要几十纳秒。**磁盘比内存慢十万倍**。如果每次读文件都老老实实下仓库,系统的文件操作会慢到没法用:`ls` 一个目录都得卡半秒,`grep` 一个文件得几秒。

所以内核几乎是无条件地做了一个决定:

> **凡是读进内存的文件页,都先留在内存里一份(只要内存还够),下次再读直接给,别再下仓库。**

这份"留在内存里的文件页",就是 **page cache**(页缓存)。它是文件系统的**前台快取柜**——既然下仓库那么贵,能少下一次是一次。

> 这也回答了内存篇的一个伏笔:"为什么 Linux 倾向于把空闲内存全用来当缓存,而不是让它闲着?"——因为内存闲着也是闲着,拿来缓存文件,几乎所有的二次访问都飞快,这是稳赚的买卖。内存紧张时再回收这些页(内存篇第 12 章讲过),不亏。

---

## 二、address_space:一个文件一份"页缓存账本"

page cache 不是一锅乱炖的页,而是**按文件组织**的:每个文件有自己的缓存账本,这个账本就是 `address_space`。

看它的核心字段([include/linux/fs.h](../linux-6.14/include/linux/fs.h#L502-L522)):

```c
struct address_space {
	struct inode		*host;          /* ← 这个账本属于哪个文件(inode) */
	struct xarray		i_pages;       /* ← 真正的账本:文件偏移 → folio */
	struct rw_semaphore	invalidate_lock;
	gfp_t			gfp_mask;
	atomic_t		i_mmap_writable;
	...
	struct rb_root_cached	i_mmap;       /* ← 谁把这个文件 mmap 进了地址空间 */
	unsigned long		nrpages;       /* ← 我缓存了多少页 */
	pgoff_t			writeback_index;
	const struct address_space_operations *a_ops;  /* ← 操作表(读写页靠它) */
	unsigned long		flags;
	errseq_t			wb_err;
	...
} __attribute__((aligned(sizeof(long)))) __randomize_layout;

/* XArray tags, for tagging dirty and writeback pages in the pagecache. */
#define PAGECACHE_TAG_DIRTY	XA_MARK_0
#define PAGECACHE_TAG_WRITEBACK	XA_MARK_1
#define PAGECACHE_TAG_TOWRITE	XA_MARK_2
```

几个关键点:

- `host`:这个账本属于哪个 inode(哪个文件)。文件和账本一一对应。
- `i_pages`:**真正的账本——一棵 xarray**(一种基数树/稀疏数组)。key 是"文件里的第几页"(`pgoff_t`),value 是那一页的 folio。"读文件第 100 字节" → 换算成"文件第 0 页" → 在 `i_pages` 里查第 0 项 → 拿到 folio。
- `nrpages`:缓存了多少页。
- `a_ops`:**address_space 操作表**——读写页的函数(`read_folio`/`writepage`/`write_begin`/`write_end` 等)都挂在这儿。注意这是**具体文件系统填的**(ext4 有 ext4 的一套),page cache 的通用代码通过它"回调"到具体文件系统。
- 三个 tag(`DIRTY`/`WRITEBACK`/`TOWRITE`):在 xarray 上打标记,**快速回答"哪些页是脏的、哪些正在写回"**——写回篇(第 32 章)会用到。

`address_space` 是全篇最重要的对象之一,因为它是**文件系统和内存管理的接头点**:

- 往文件系统一侧看:它挂在 `inode->i_mapping`,描述一个文件。
- 往内存一侧看:它持有这个文件所有的缓存页(folio),这些页是内存管理的对象(可回收、有 LRU、有标记)。

> 上一章 ext4 的 `ext4_file_read_iter` 把读"甩"给通用代码,甩到的就是 `address_space` 这一层。所有文件系统的读写,最终都收拢到 `address_space` 这本账上。

---

## 三、读路径:查账本,有就给,没货就触发预读

市民 `read(fd, buf, 4096)`,穿过 VFS(`vfs_read` → `f_op->read_iter`)后,落到 `generic_file_read_iter` 这类通用读函数。它的核心逻辑极简:**就是查 `address_space->i_pages` 这本账**。

我们看一个更典型的读场景——**mmap 一个文件然后访问它**,这会触发 `filemap_fault`([mm/filemap.c](../linux-6.14/mm/filemap.c#L3360-L3389)):

```c
vm_fault_t filemap_fault(struct vm_fault *vmf)
{
	int error;
	struct file *file = vmf->vma->vm_file;
	struct file *fpin = NULL;
	struct address_space *mapping = file->f_mapping;
	struct inode *inode = mapping->host;
	pgoff_t max_idx, index = vmf->pgoff;
	struct folio *folio;
	...

	max_idx = DIV_ROUND_UP(i_size_read(inode), PAGE_SIZE);
	if (unlikely(index >= max_idx))
		return VM_FAULT_SIGBUS;          /* ① 越界:文件没这么大 */

	/*
	 * Do we have something in the page cache already?
	 */
	folio = filemap_get_folio(mapping, index);   /* ② 查账本 */
	if (likely(!IS_ERR(folio))) {
		/*
		 * We found the page, so try async readahead before waiting for
		 * the lock.
		 */
		if (!(vmf->flags & FAULT_FLAG_TRIED))
			fpin = do_async_mmap_readahead(vmf, folio);  /* ③ 顺手预读 */
		...
```

逐段看:

1. **越界检查**:`index >= max_idx` → 你要的页超过文件大小,返回 `SIGBUS`(内存篇 page fault 章讲过,这是"访问了文件末尾之外"的合法报错)。
2. **查账本**:`filemap_get_folio(mapping, index)`——在 `i_pages` 里查"文件第 index 页在不在前台快取柜"。
3. **命中**:页在 → 几乎什么都不用做,把 folio 映射进进程页表,直接返回。**这一刻磁盘根本没参与**,这就是二次访问飞快的原因。
4. **没命中(miss)**:往下走(省略的部分),触发真正的读——把页从磁盘搬进 page cache。

### 预读(readahead):既然下仓库,不如一次取足

注意第 3 步那个 `do_async_mmap_readahead`——**页明明已经命中了,为什么还要 readahead?** 这是 page cache 设计里非常聪明的一笔:

> 市民访问文件通常是**顺序的**(读完第 0 页大概率接着读第 1、2、3 页)。既然你已经命中了第 0 页,说明这个文件正在被顺序读——**那就趁现在异步把后面几页也提前搬进来**,等市民真要第 1 页时,前台已经有了,不用再等磁盘。

这就是**预读(readahead)**。它赌的是"顺序访问"这个统计规律。预读由 [mm/readahead.c](../linux-6.14/mm/readahead.c) 的 `page_cache_sync_ra` 等函数驱动,预读窗口会**自适应扩大**:连续命中,窗口加倍;预读的页没用上(乱序访问),窗口缩小甚至停止。

预读是 page cache 把"慢磁盘"伪装成"快内存"的关键手段之一——**不是减少下仓库的次数(该下还得下),而是把多次小 IO 合并成一次大 IO,而且提前在后台做,市民几乎感觉不到延迟**。

---

## 四、写路径:借页—拷贝—标记脏(为什么写比读复杂)

读是"查账本、给页"。写就复杂多了,因为写**牵扯到"什么时候真正落盘"**这个大问题(第 32 章专讲)。

我们看通用写函数 `generic_perform_write`([mm/filemap.c](../linux-6.14/mm/filemap.c#L4074-L4103))的核心循环:

```c
ssize_t generic_perform_write(struct kiocb *iocb, struct iov_iter *i)
{
	struct file *file = iocb->ki_filp;
	loff_t pos = iocb->ki_pos;
	struct address_space *mapping = file->f_mapping;
	const struct address_space_operations *a_ops = mapping->a_ops;
	...
	ssize_t written = 0;

	do {
		struct folio *folio;
		size_t offset;		/* Offset into folio */
		size_t bytes;		/* Bytes to write to folio */
		size_t copied;		/* Bytes copied from user */
		void *fsdata = NULL;
		...
		balance_dirty_pages_ratelimited(mapping);   /* ① 写太多就限流 */

		if (fatal_signal_pending(current)) {
			status = -EINTR;
			break;
		}

		status = a_ops->write_begin(file, mapping, pos, bytes,
						&folio, &fsdata);    /* ② 借页 */
		...
```

写的三段式:

1. **限流**:`balance_dirty_pages_ratelimited`——如果系统脏页太多了,这里**让写进程等一下**,逼着写回线程赶紧把脏页落盘。这是防止"市民疯狂写,内存被脏页撑爆"的保险(第 32 章细讲)。
2. **借页(`write_begin`)**:文件系统(ext4)提供一个 `write_begin` 实现。它的活是:**在 page cache 里把目标页准备好**——页不在就分配一页塞进账本;若是部分写(只写页内一段),还可能要先把这页从磁盘读上来(否则未写的那段是垃圾)。借出来后,这页属于你写了。
3. **拷贝**:把市民用户态的数据拷进这页(`iov_iter_copy_from_user_folio` 一类)。
4. **标记脏(`write_end`)**:文件系统的 `write_end` 把这页**标记为脏**(`PAGECACHE_TAG_DIRTY`),设置好时间戳,解锁。然后——**`write` 系统调用就返回了**。

注意第 4 步最关键的一点:

> **写完返回时,数据只到了 page cache,根本没到磁盘。** 这页是脏的,躺在内存里。真正落盘是"后来的事"(写回线程,第 32 章)。

这就是为什么"写比读复杂"——写牵扯到"什么时候落盘"这个权衡:立即落盘太慢,完全延迟又会丢一大堆数据、内存被脏页撑爆。整个第 32 章就是讲这个平衡。读则没有这个负担:读出来的页本来就是磁盘的副本,丢了重读就行。

`write_begin`/`write_end` 是 `address_space_operations` 里的两个回调([include/linux/fs.h](../linux-6.14/include/linux/fs.h#L446-L451)),具体怎么实现是文件系统的事:

```c
	int (*write_begin)(struct file *, struct address_space *mapping,
				loff_t pos, unsigned len,
				struct folio **foliop, void **fsdata);
	int (*write_end)(struct file *, struct address_space *mapping,
				loff_t pos, unsigned len, unsigned copied,
				struct folio *folio, void **fsdata);
```

ext4 的 `ext4_write_begin`/`ext4_write_end` 会在这两步之间夹一个**日志事务**(journaling,第 32 章)——这是 ext4 保证掉电安全的秘诀,读路径完全不需要。

---

## 五、page cache 里的页 vs 匿名页:回收篇的伏笔

page cache 里这些文件页,和进程的"匿名页"(malloc 出来、和任何文件无关的页,内存篇讲过)有什么区别?这个区别是理解内存回收(内存篇第 12 章)的关键:

| | 文件页(file-backed) | 匿名页(anonymous) |
|---|---|---|
| 有没有"原件" | 有,原件在磁盘文件里 | 没有,只在内存里 |
| 干净页能不能直接丢 | **能**,丢了重读就行 | 不能 |
| 脏页怎么回收 | 写回磁盘,再丢 | 写到 swap 区 |
| 谁的 `address_space` | 文件的 `address_space` | 进程自己的(swap cache) |

这就是内存篇回收章那个"三类页"分类的根源:**文件页有原件,匿名页没有**。所以内存紧张时,干净的文件页直接扔(最便宜);脏文件页写回再扔;匿名页只能 swap。page cache 这一章正好解释了"为什么文件页可以那么便宜地回收"——因为它本质上只是磁盘文件的一份缓存副本,丢了能重建。

---

## 六、关键源码精读:`address_space_operations` 这张回调表

我们最后聚焦看 `address_space_operations`([include/linux/fs.h](../linux-6.14/include/linux/fs.h#L434-L451))——它是 page cache 通用代码和具体文件系统之间的"接口契约":

```c
struct address_space_operations {
	int (*writepage)(struct page *page, struct writeback_control *wbc);
	int (*read_folio)(struct file *, struct folio *);

	/* Write back some dirty pages from this mapping. */
	int (*writepages)(struct address_space *, struct writeback_control *);

	/* Mark a folio dirty.  Return true if this dirtied it */
	bool (*dirty_folio)(struct address_space *, struct folio *);

	void (*readahead)(struct readahead_control *);

	int (*write_begin)(struct file *, struct address_space *mapping,
				loff_t pos, unsigned len,
				struct folio **foliop, void **fsdata);
	int (*write_end)(struct file *, struct address_space *mapping,
				loff_t pos, unsigned len, unsigned copied,
				struct folio *folio, void **fsdata);
	...
```

看这张表的设计哲学:

- `read_folio`/`readahead`:**怎么把磁盘上的页读进 page cache**。这是 miss 时的回调。
- `write_begin`/`write_end`:**怎么把一次写准备好/收尾**(借页、可能先读、标记脏)。
- `writepage`/`writepages`:**怎么把脏页写回磁盘**(写回时用,第 32 章)。
- `dirty_folio`:**怎么把一页标记脏**。

**page cache 的通用代码(`filemap.c`/`filemap_fault`/`generic_perform_write`)不关心磁盘长什么样**,它只管"查账本、调回调"。磁盘的事(ext4 的 extent、块号),全在文件系统填的这些回调里。这是 VFS"操作表多态"思想在 page cache 这一层的一模一样的复刻——**又一处"接口与实现解耦"**。

> 这也是为什么 Linux 能把"页缓存"做成一个**通用、统一**的机制:不管底下是 ext4、NFS、还是 tmpfs,只要它们填好这套 `a_ops`,就能享受 page cache 带来的全部好处(缓存、预读、回收、写回)。

---

## 七、本章小结

用档案馆的比喻回顾本章:

- page cache 是档案馆的**前台快取柜**:凡是读进来的文件页都留一份,二次访问直接给,避免老往地下仓库跑(磁盘比内存慢十万倍)。
- **`address_space`** 是"一个文件一份页缓存账本",`host` 指回 inode,`i_pages` 这棵 xarray 是"文件偏移 → folio"的账。它是**文件系统和内存管理的接头点**——往文件一侧是 `inode->i_mapping`,往内存一侧是缓存的 folio 们。
- **读路径**:`filemap_fault` 先查账本,命中直接给(磁盘不参与);没命中才触发真正的读。预读(readahead)赌"顺序访问",顺手把后面的页异步搬进来,让仓库 IO 合并、提前,市民几乎无感。
- **写路径**:`write_begin`(借页,可能先读)→ 拷数据 → `write_end`(标记脏)。**写返回时数据只到内存,没到磁盘**——这为下一章的写回埋下伏笔。
- **文件页 vs 匿名页**:文件页有磁盘原件,丢了能重读,回收便宜;匿名页没原件,只能 swap。这是内存篇回收章三类页的根源。

回到全书主线:本章是**造幻觉那侧**(文件=统一的读写)和**管资源那侧**(内存)的真正交汇点。`address_space` 这一个对象,同时属于文件系统(inode 一侧)和内存管理(folio 一侧)——它是全书唯一一个让两大子系统直接握手的结构,理解了它,你就理解了"为什么文件 IO 和内存管理这么纠缠"。

但请注意:到本章为止,我们说的"真正读磁盘/写磁盘"还都是黑箱——`read_folio`/`writepage` 到底怎么把页搬来搬去?那 4KB 是怎么变成一次磁盘 IO 的?这正是下一章的主题。

> **想继续深入**:看 [mm/filemap.c](../linux-6.14/mm/filemap.c) 的 `__filemap_get_folio`(怎么在 xarray 里查/插页)、`filemap_fault` 全文(读路径的完整状态机);看 [mm/readahead.c](../linux-6.14/mm/readahead.c) 的 `page_cache_sync_ra`/`page_cache_async_ra`(预读窗口怎么自适应)。
>
> **下一章**:第 31 章,page cache miss 或写回时,真正发起的"去磁盘取/存"是怎么一回事?我们钻进块设备层,看 bio → request 怎么排队、合并、调度,最后递到磁盘驱动手里。
