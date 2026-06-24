# 第 31 章 · 块设备层:bio、request 与 I/O 调度

> **前置**:你需要先读过**第 30 章《page cache》**(理由:page cache 没命中要发起真正的磁盘读、写回脏页要发起真正的磁盘写——这两个"真正"就是本章的主角。块设备层是 page cache 往下、磁盘驱动往上的那一层)。

**本章核心问题**:page cache 没命中、或写回脏页时,真正的"去磁盘取/存"是怎么发起的?这个请求一路怎么排队、合并、调度,最后才递到磁盘驱动手里?为什么现代 NVMe 能跑满带宽?

**读完本章你会明白**:
- `bio`(文件系统发的原始 I/O 单元)和 `request`(blk-mq 把 bio 攒成的大单)为什么是两层;
- 不合并会怎样:每次 4KB 一个请求,磁盘寻道忙死;
- blk-mq 的多硬件队列为什么能让多核"谁也不抢锁";
- plug(插头)机制怎么把同一进程的连续小 I/O 先攒在本地再下放。

> **如果一读觉得太难**:先只记住三件事——① 文件系统发的叫 bio,块设备层把一堆相邻 bio 合并成 request 再交给磁盘;② 合并是为了减少磁盘 IO 次数(磁盘最贵的是寻道/排队,不是数据量);③ 现代 blk-mq 每个核一条硬件队列,谁也不用抢锁,这是 NVMe 高吞吐的根基。其余细节第二遍配合源码再抠。

---

## 一、不合并会怎样:每次 4KB 一个请求,磁盘寻道忙死

还是先想灾难。

市民顺序写一个 1MB 的文件。文件系统把它拆成 256 个 4KB 的写(page cache 一页一页),假设现在写回——如果块设备层傻乎乎地**每个 4KB 都立刻发一个独立请求给磁盘**,会发生什么?

机械硬盘一次 IO 的成本主要是**寻道 + 旋转延迟**(把磁头挪到正确的磁道、等正确扇区转过来),光这两步就要几毫秒,**真正读写 4KB 数据反而只要几十微秒**。也就是说,磁盘 IO 的成本里,**"挪到位"占 90% 以上,"搬数据"占不到 10%**。

于是 256 个独立 4KB 请求,每个都得挪一次磁头 → 256 次几毫秒的等待 → 1MB 写完要一两秒。可如果把这 256 个相邻的请求**合并成一个 1MB 的大请求**,磁头只挪一次,1MB 数据一口气搬完 → 只要几十毫秒。**差了几十倍。**

这就是块设备层存在的第一理由:**把零散的小 I/O 合并、攒批、按物理顺序排好,让磁盘少做无谓的挪动**。哪怕 SSD 没有机械寻道,合并也照样重要——它减少了请求队列的深度、减少了每个请求的固定开销、让磁盘控制器能更高效地调度闪存通道。

所以内核在文件系统和磁盘驱动之间,夹了一层**块设备层(block layer)**,它就是仓库的**调度室**:接单、排队、合并、调度,最后才让叉车(驱动)出动。

---

## 二、两层模型:bio 和 request

块设备层的核心是两个对象,代表 I/O 处理的两个阶段。

### bio:文件系统发出的"原始运货单"

`bio`(block I/O)是**文件系统/页面回收代码发起的最原始的 I/O 单元**。看它的关键字段([include/linux/blk_types.h](../linux-6.14/include/linux/blk_types.h#L214-L235)):

```c
struct bio {
	struct bio		*bi_next;	/* request queue link */
	struct block_device	*bi_bdev;          /* 这个 bio 发给哪个块设备 */
	blk_opf_t		bi_opf;		/* bottom bits REQ_OP, top bits ... */
	unsigned short		bi_flags;
	...
	blk_status_t		bi_status;
	atomic_t		__bi_remaining;

	struct bvec_iter	bi_iter;          /* 指向要读/写哪些扇区、哪些页 */
	...
	bio_end_io_t		*bi_end_io;       /* 完成时的回调 */
	void			*bi_private;
	...
```

- `bi_bdev`:发给哪个块设备(哪块磁盘)。
- `bi_opf`:操作 + 标志(`REQ_OP_READ`/`REQ_OP_WRITE`/`REQ_OP_FLUSH`/`REQ_NOWAIT`…)。
- `bi_iter`:**真正描述"读/写哪些数据"——扇区号、长度,以及一串指向内存页的 `bio_vec`**。一个 bio 可以跨多个不连续的内存页(bvec 数组),但通常对应一段磁盘连续区间。
- `bi_end_io`:**完成回调**——磁盘把这个 bio 做完了,内核回调这个函数通知(异步完成的关键)。

bio 的语义是"**请把磁盘这段扇区,读到这些内存页**(或反向写)"。它**还没有排队、没有合并**,是 I/O 的"原材料"。

### request:blk-mq 把 bio 攒成的"大宗运货单"

`request` 是**经过排队、合并后的 I/O 单位**。看它的关键字段([include/linux/blk-mq.h](../linux-6.14/include/linux/blk-mq.h#L102-L125)):

```c
struct request {
	struct request_queue	*q;
	struct blk_mq_ctx	*mq_ctx;
	struct blk_mq_hw_ctx	*mq_hctx;

	blk_opf_t cmd_flags;		/* op and common flags */
	req_flags_t rq_flags;

	int tag;
	...
	unsigned int __data_len;	/* total data len */
	sector_t __sector;		/* sector cursor */    /* ← 当前处理到哪个扇区 */

	struct bio *bio;                                /* ← 这个 request 挂了一串 bio */
	struct bio *biotail;

	union {
		struct list_head queuelist;              /* ← 排在哪个队列里 */
		struct request *rq_next;
	};
	...
```

关键点:

- `bio` / `biotail`:**一个 request 可以挂多个 bio**——这就是合并。多个相邻的 bio(扇区连续)被串进同一个 request,变成一个大 I/O。
- `__sector` / `__data_len`:这个 request 起始扇区和总长度(可能远大于单个 bio)。
- `mq_ctx` / `mq_hctx`:**这个 request 属于哪个软件队列(ctx)、哪个硬件队列(hctx)**——blk-mq 多队列的核心,下面专讲。

> 所以两层模型的本质:**bio 是"散客的零单",request 是"调度室把零单合并、排好队后的大单"**。磁盘驱动最终拿到的是 request(里面挂着一串 bio),一个个 bio 依次处理,处理完回调通知。

---

## 三、一条 bio 的旅程:`submit_bio` → blk-mq

看一条 bio 从文件系统出发,到被排进队列的全过程。入口是 `submit_bio`([block/blk-core.c](../linux-6.14/block/blk-core.c#L899-L910)):

```c
void submit_bio(struct bio *bio)
{
	if (bio_op(bio) == REQ_OP_READ) {
		task_io_account_read(bio->bi_iter.bi_size);
		count_vm_events(PGPGIN, bio_sectors(bio));
	} else if (bio_op(bio) == REQ_OP_WRITE) {
		count_vm_events(PGPGOUT, bio_sectors(bio));
	}

	bio_set_ioprio(bio);
	submit_bio_noacct(bio);
}
```

`submit_bio` 自己只做点记账(统计读/写扇区数,这就是 `iostat` 那些数据的来源)和设 I/O 优先级,然后把 bio 转给 `submit_bio_noacct`([block/blk-core.c#L770-L773](../linux-6.14/block/blk-core.c#L770-L773)):

```c
void submit_bio_noacct(struct bio *bio)
{
	struct block_device *bdev = bio->bi_bdev;
	struct request_queue *q = bdev_get_queue(bdev);
	...
```

它拿到目标块设备的 `request_queue`(这个磁盘的"调度室入口"),做一些检查(NOWAIT 支持、只读保护、越界检查……),最后把 bio 交给 **blk-mq**(现代块设备层的核心):

```c
blk_mq_submit_bio(bio);
```

`blk_mq_submit_bio`([block/blk-mq.c#L3056-L3093](../linux-6.14/block/blk-mq.c#L3056-L3093))是合并、排队真正发生的地方:

```c
void blk_mq_submit_bio(struct bio *bio)
{
	struct request_queue *q = bdev_get_queue(bio->bi_bdev);
	struct blk_plug *plug = current->plug;       /* ← 当前进程的"插头" */
	const int is_sync = op_is_sync(bio->bi_opf);
	struct blk_mq_hw_ctx *hctx;
	unsigned int nr_segs;
	struct request *rq;
	...

	/*
	 * If the plug has a cached request for this queue, try to use it.
	 */
	rq = blk_mq_peek_cached_request(plug, q, bio->bi_opf);   /* ① 先看插头里有没有能合并的 */
	...
	if (!rq) {
		if (unlikely(bio_queue_enter(bio)))
			return;
	}

	/* 走到这里,要么复用了缓存的 request,要么新建一个 */
	...
```

这里的两个关键概念——**plug(插头)**和**合并**——我们下面专讲。它最终的目的是:**把 bio 尽量塞进一个已存在的 request(合并),塞不进就新建一个 request,然后把它排进硬件队列,等待下发给驱动**。

---

## 四、合并(merge):块设备层省事的核心

`blk_mq_submit_bio` 里那条 `blk_mq_peek_cached_request`,以及随后的逻辑,核心就是干一件事:**这条 bio 能不能和已有的 request 合并?**

合并有两种:

- **前合并(back merge)**:新 bio 的扇区正好接在某个 request 的尾巴后面(`bio 起始扇区 == request 末尾扇区`)→ 串进去,`__data_len` 变大。
- **前向合并(front merge)**:新 bio 的扇区正好在某个 request 的开头之前 → 接在前面。

合并的收益直接而巨大:**N 个 4KB 请求合成 1 个 N×4KB 请求,磁盘只处理一次**。顺序 IO(大文件读写)几乎总能命中合并,所以顺序 IO 吞吐远高于随机 IO——这就是"为什么数据库要顺序写日志、为什么 `dd` 顺序比随机快得多"的根本原因。

> 合并也有限度:如果 bio 们扇区不连续(随机 IO),没法合并,只能各排各的。这就是为什么**随机 4K 性能(IOPS)是 SSD 最难啃的指标**——它测的就是"完全没法合并"时的裸性能。

---

## 五、blk-mq:为什么多硬件队列能跑满 NVMe

老式块设备层(2.6 时代)是**单队列**模型:整个磁盘只有一个请求队列,所有核提交 bio 都得抢同一把队列锁。核少时没事,核一多(SMP 服务器几十个核),**锁竞争成了瓶颈**——大家排队抢锁,反而比磁盘还慢。

blk-mq(block multi-queue,多队列)就是为解决这个问题诞生的。它把队列分成两层:

```
   CPU 核 0     CPU 核 1     CPU 核 2   ...   CPU 核 N
     │            │            │               │
     ▼            ▼            ▼               ▼
  [软件队列]  [软件队列]   [软件队列]  ...  [软件队列]    ← 每核一个,基本无锁
     └────────┬───┴────────┬───┘               │
              │  软件队列按硬件队列数哈希分发    │
              ▼                                ▼
        [硬件队列 0]        [硬件队列 1]  ... [硬件队列 M]  ← 真正递给驱动
```

- **软件队列(software queue,`blk_mq_ctx`)**:每个 CPU 核一个,提交 bio 时只往自己核的队列塞,**几乎不用抢锁**(核内操作)。
- **硬件队列(hardware queue,`blk_mq_hw_ctx`)**:数量通常等于磁盘的硬件队列数(NVMe 可以有上千个)。软件队列按硬件队列数哈希分发,最后由硬件队列递给磁盘驱动。

> 这样设计的好处:多核提交 I/O 时,大部分时间在自己核的软件队列里玩,**互不打扰**。锁竞争从"全机一把"降到"几乎无锁"。这是现代 NVMe 能跑出几百万 IOPS、几十 GB/s 带宽的**软件根基**——没有 blk-mq,多核再强也喂不饱磁盘。

对应到 request 结构里:`mq_ctx`(软件队列)+ `mq_hctx`(硬件队列),每个 request 都标着自己该排进哪两个队列。`blk_mq_run_hw_queue`([block/blk-mq.c#L2320](../linux-6.14/block/blk-mq.c#L2320))就是把某个硬件队列里攒的 request 真正下发给驱动的动作。

---

## 六、plug(插头):把同一进程的小 IO 先攒在本地

回到 `blk_mq_submit_bio` 里那个 `current->plug`——这是 blk-mq 的另一个关键优化:**plug(插头)**。

设想一个进程连续提交 10 个相邻的 bio。如果每个 bio 一来就立刻排进软件队列、立刻尝试下发,那这 10 个 bio 可能分成好几次下发,**前几个错过了后面几个的合并机会**。

plug 的做法:**进程在批量提交 I/O 前,先 `blk_start_plug`("插上插头"),这之后它提交的 bio/request 先攒在一个本地的 plug 列表里,不下发**。等这批 I/O 提交完,`blk_finish_plug`("拔插头"),把攒的一整批一次性冲进软件队列——这时候合并能发现"哦原来这 10 个是连着的",一次性合成一个大 request,效率拉满。

看 `blk_start_plug`([block/blk-core.c#L1161](../linux-6.14/block/blk-core.c#L1161))——它就是把 plug 挂到 `current->plug` 上(每个进程一个,在自己的 task_struct 里)。`struct blk_plug`([include/linux/blkdev.h#L1034](../linux-6.14/include/linux/blkdev.h#L1034))就是那个本地攒单列表。

> 内存管理里 page cache 回收一大批脏页、文件系统写一大批数据,都会先 plug 再批量提交。这本质上是**把"按页提交"的零碎节奏,聚合成"按批提交"的节奏**,给合并留出最大空间。又一个"用 batching 换效率"的经典设计。

---

## 七、I/O 调度器:在 blk-mq 之上,还能再排一次序

blk-mq 本身只管"多队列 + 合并"。在这个基础上,内核还可以挂一个 **I/O 调度器(scheduler)**,对 request 再做一层排序/合并。常见的有 `mq-deadline`(按截止时间排序,防饿死)、`bfq`(按进程公平分配带宽,适合桌面)、`kyber`(简单自适应)、`none`(直接下发,适合 NVMe,本身硬件队列已经够多)。

调度器解决的核心问题是:**当 request 比磁盘能处理的多时,按什么顺序下发?** 纯按到达顺序(FIFO)可能让某个请求饿死;按物理扇区顺序(电梯算法)吞吐高但可能饿死远端请求。调度器在这些目标间权衡。

> 现代 NVMe 因为硬件队列多、本身延迟低,常配 `none`(不调度)——blk-mq 的合并 + 多队列已经够好,调度器反而成开销。老式 SATA/机械盘则更需要 deadline/bfq 这类调度。**这是"软件复杂度该匹配硬件能力"的典型例子**:硬件强了,软件反而要简化。

---

## 八、关键源码精读:`blk_mq_submit_bio` 的合并入口

我们把 `blk_mq_submit_bio` 开头那句"先看插头里有没有能合并的"再放大看([block/blk-mq.c#L3056-L3093](../linux-6.14/block/blk-mq.c#L3056-L3093)):

```c
void blk_mq_submit_bio(struct bio *bio)
{
	struct request_queue *q = bdev_get_queue(bio->bi_bdev);
	struct blk_plug *plug = current->plug;       /* ← 当前进程插头 */
	...
	struct request *rq;
	...

	/*
	 * If the plug has a cached request for this queue, try to use it.
	 */
	rq = blk_mq_peek_cached_request(plug,q, bio->bi_opf);  /* ① 先在插头里找 */
	...
	if (!rq) {
		if (unlikely(bio_queue_enter(bio)))         /* ② 没找到,新建 */
			return;
	}
	...
```

这一小段浓缩了块设备层的设计哲学:

1. **`current->plug`**:这个 bio 的提交者当前是否"插着插头"在攒单。大多数批量场景都插着。
2. **`blk_mq_peek_cached_request`**:在插头缓存的 request 里,找有没有**扇区相邻、能合并**的。有 → 把 bio 串进去(`__data_len` 变大),**这是合并发生的地方**。
3. **找不到 → 新建 request**:`bio_queue_enter` 走分配路径,把这条 bio 包成一个新的 request,排进软件队列。

随后的代码(此处省略)会把这个 request 真正排进 `mq_ctx`(软件队列),并在合适时机(拔插头、队列满、超时)调用 `blk_mq_run_hw_queue` 把软件队列的 request 哈希分发到硬件队列、下发给驱动。**至此,bio 走完了它在块设备层的旅程,进入驱动的世界**。

> 把这条链画出来:**文件系统 bio → `submit_bio` → `submit_bio_noacct` → `blk_mq_submit_bio`(合并/插头)→ 软件队列 → 硬件队列 → 驱动 → 磁盘**。这条链上的每一环,都是为"让磁盘少做无谓的挪动、让多核互不干扰"服务的。

---

## 九、本章小结

用仓库的比喻回顾本章:

- 块设备层是仓库的**调度室**:接单、排队、合并、调度,最后让叉车(驱动)出动。它夹在 page cache(上层)和磁盘驱动(下层)之间。
- **两层模型**:文件系统发的叫 **bio**(零散的原始运货单);调度室把相邻 bio 合并、攒批后,包成 **request**(大宗运货单)。一个 request 挂一串 bio。磁盘最终处理的是 request。
- **合并**是调度室省事的核心:把 N 个相邻 4KB 合成一个 N×4KB 的大单,磁盘只挪一次磁头。顺序 IO 几乎总能合并(所以快),随机 IO 没法合并(所以 IOPS 难提)。
- **blk-mq(多队列)**:每核一个软件队列(无锁)+ 若干硬件队列,让多核提交 IO 时互不抢锁。这是 NVMe 高吞吐的软件根基。老式单队列在多核下会被锁竞争拖垮。
- **plug(插头)**:进程批量提交前先 plug,把 bio 先攒在本地,拔插头时一次性冲进队列,给合并留最大空间。
- **I/O 调度器**(可选)在 blk-mq 之上再排一次序,权衡吞吐 vs 公平。硬件强(NVMe)常配 `none`,硬件弱(机械盘)更需要 deadline/bfq。

回到全书主线:本章是**管资源那侧**(对付又慢又笨的磁盘)的核心。前面 VFS、ext4、page cache 都在做"抽象和缓存",一旦真的要碰磁盘(慢的那个现实),所有这些抽象都得靠块设备层把请求"榨干再榨干"地喂给磁盘——合并、攒批、多队列、插头,每一个都是被"磁盘慢"这个现实逼出来的。

但还有一个问题没回答:**写出去的数据,什么时候、怎么真正落盘?万一写到一半掉电怎么办?** 这正是最后一章的主题——写回与日志。

> **想继续深入**:看 [block/blk-merge.c](../linux-6.14/block/blk-merge.c)(bio/request 合并的具体逻辑);看 [block/blk-mq.c](../linux-6.14/block/blk-mq.c) 的 `blk_mq_run_hw_queue`/`blk_mq_dispatch_rq_list`(硬件队列怎么下发);看 [block/elevator.c](../linux-6.14/block/elevator.c) 和各调度器(`block/mq-deadline.c`/`block/bfq-iosched.c`)。
>
> **下一章**:第 32 章,收官。讲清楚脏页什么时候写回、journaling(日志)怎么保证掉电不把档案改坏。读完,本篇从 VFS 到磁盘的完整闭环就合上了。
