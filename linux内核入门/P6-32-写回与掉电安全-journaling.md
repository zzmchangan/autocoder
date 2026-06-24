# 第 32 章 · 写回与掉电安全:脏页怎么回家、journaling 怎么保不丢

> **前置**:你需要先读过**第 30 章《page cache》**(理由:第 30 章讲清了"写只到 page cache、标记成脏就返回",这一章就回答"那个脏什么时候、怎么真正落盘")和**第 31 章《块设备层》**(理由:写回就是发起一堆 bio/request 给磁盘,要走块设备层那条路)。

**本章核心问题**:写文件只是把页标记成脏,真正落盘是"后来的事"。那这个"后来"到底是谁、什么时候、按什么规则把脏页写回?更关键的——万一写到一半掉电,文件系统怎么保证不把档案改坏?

**读完本章你会明白**:
- 为什么不能"立即写回"也不能"无限延迟",内核靠什么在两者间找平衡;
- 谁来写回、什么时候写回、三种触发方式;
- journaling(日志)的核心思想:"先记流水、再改账",为什么这能扛掉电;
- ext4 的 ordered/journal/writeback 三种模式各自拿什么换什么。

> **如果一读觉得太难**:先只记住三件事——① 脏页攒到一定比例就由后台写回线程(kworker/wb)异步刷盘,写得太猛还会被 `balance_dirty_pages` 限流(让写进程等);② 掉电的危险在于"改一半"——元数据和数据不一致会让文件系统损坏;③ journaling 的解法是"先把要做的改动记进日志,再真改;掉电重启后照日志补完或撤销",ext4 默认用 ordered 模式(数据先落盘、再记元数据日志)。

---

## 一、为什么不能立即写回,也不能无限延迟

第 30 章我们看到:市民 `write` 一页,只是把它在 page cache 里标记成脏就返回了,**数据根本没到磁盘**。这就引出一个根本的权衡:**这些脏页,到底什么时候真正写回磁盘?**

两个极端都不可取:

### 极端一:立即写回(每次 write 同步刷盘)

市民写一个字节,内核立刻发起一次磁盘 IO 把这页刷下去。

- 灾难:**慢到没法用**。磁盘 IO 是毫秒级,市民每秒写不了几百次,程序像卡死。
- 而且 page cache 的所有好处(合并、攒批)全没了。

### 极端二:无限延迟(永远不主动写回,只在内存满时才写)

脏页一直赖在内存,能拖就拖。

- 灾难一:**掉电丢一大堆数据**。机器突然断电,内存里所有没落盘的脏页全没了——可能丢掉几十秒的数据。
- 灾难二:**内存被脏页撑爆**。脏页占着物理内存不还,新分配没内存用。
- 灾难三:**崩溃时不一致**。万一系统崩溃,磁盘上的文件系统元数据可能处于"改一半"的不一致状态,下次挂载都挂不上。

所以正确答案是**中间路线**:

> **延迟写回,但有限度——攒一批、按比例、定时地异步刷盘;同时给写进程限流,不让脏页堆积失控。**

这个"中间路线"的具体实现,就是本章的前半部分(writeback)和后半部分(journaling)。前者解决"什么时候写回",后者解决"写回了怎么保证不掉电不损坏"。

---

## 二、谁来写回:三种触发

Linux 的写回由**后台写回线程**(per-BDI 的 `bdi_writeback`,通常表现为 `flush-x:y` / `kworker` 内核线程)承担,它跑的是 `wb_workfn`([fs/fs-writeback.c#L2326](../linux-6.14/fs/fs-writeback.c#L2326))这个工作队列函数。它什么时候被唤醒去写回?有三种触发,内核用一个枚举记录原因([include/linux/backing-dev-defs.h#L44-L50](../linux-6.14/include/linux/backing-dev-defs.h#L44-L50)):

```c
enum wb_reason {
	WB_REASON_BACKGROUND,
	WB_REASON_VMSCAN,
	WB_REASON_SYNC,
	WB_REASON_PERIODIC,
	WB_REASON_LAPTOP_TIMER,
	WB_REASON_FS_FREE_SPACE,
	...
};
```

逐个看:

1. **`WB_REASON_PERIODIC`(周期性写回)**:内核有个定时器(默认 5 秒一次),定期唤醒写回线程,把存在了一段时间的脏页刷下去。**这是"别让脏页老赖着"的基本盘**,保证哪怕系统很闲,脏页也不会无限堆积——掉电最多丢最近 5 秒的数据。
2. **`WB_REASON_BACKGROUND`(后台写回)**:当**脏页总量超过一个"背景阈值"**(`background threshold`,通常约内存的 10%)时,唤醒写回线程开始后台刷盘。这是"脏页有点多了,赶紧清一清"。
3. **`WB_REASON_VMSCAN`(内存回收触发)**:内存紧张、回收线程(kswapd/直接回收)需要腾内存时,脏页不能直接扔(那是数据),必须**先写回再回收**。这是"内存逼着你写"。
4. **`WB_REASON_SYNC`(`sync` 系统调用)**:用户主动 `sync`,要求把所有脏页立刻落盘。强制、等待完成。
5. 还有笔记本模式、FS 空间不足等次要触发。

> 这三种触发(定时 / 比例 / 回收)互相配合:**平时定时刷保安全,脏多了比例触发清库存,内存紧了回收逼着刷**。覆盖了所有需要写回的场景。

### 写回的核心:`wb_writeback` → `__writeback_single_inode`

写回线程被唤醒后,真正干活的链是 `wb_workfn` → `wb_writeback`([fs/fs-writeback.c#L2099](../linux-6.14/fs/fs-writeback.c#L2099)) → `__writeback_single_inode`([fs/fs-writeback.c#L1669-L1680](../linux-6.14/fs/fs-writeback.c#L1669-L1680)):

```c
__writeback_single_inode(struct inode *inode, struct writeback_control *wbc)
{
	struct address_space *mapping = inode->i_mapping;
	long nr_to_write = wbc->nr_to_write;
	unsigned dirty;
	int ret;

	WARN_ON(!(inode->i_state & I_SYNC));

	trace_writeback_single_inode_start(inode, wbc, nr_to_write);

	ret = do_writepages(mapping, wbc);   /* ← 调 address_space 的 writepages */
```

关键一句是 `do_writepages(mapping, wbc)`——它**回调具体文件系统的 `a_ops->writepages`**(第 30 章讲过这张操作表)。ext4 的 `ext4_writepages` 会把这个 inode 的脏页,组织成 bio,经块设备层(第 31 章)发给磁盘。

> 又一处"通用代码 + 文件系统回调"的解耦:写回的**调度逻辑**(谁、何时、写多少)是通用的,但**具体怎么把脏页变成磁盘 IO**是文件系统的事。ext4 的 `ext4_writepages` 里还会夹带 journaling 的逻辑——这就引出后半章。

---

## 三、限流:`balance_dirty_pages` 防止脏页失控

光靠后台线程异步写还不够。设想一百个进程疯狂 `write`,产生脏页的速度远超磁盘写回的速度——脏页会指数级堆积,内存很快撑爆,最后一大波数据随时可能丢。

内核对此有个**同步限流**机制:写进程自己在写的时候,会顺手检查"脏页是不是太多了",太多就**自己停下来等**。这个检查点就是第 30 章写路径里见过的 `balance_dirty_pages_ratelimited`([mm/page-writeback.c#L2148-L2152](../linux-6.14/mm/page-writeback.c#L2148-L2152)):

```c
void balance_dirty_pages_ratelimited(struct address_space *mapping)
{
	balance_dirty_pages_ratelimited_flags(mapping, 0);
}
```

它的内部(命名 `balance_dirty_pages`)逻辑大致是:

- 算当前系统/这个设备的**脏页比例**和**阈值**(dirty threshold)。
- 脏页 < 背景阈值:啥也不做,交给后台线程慢慢刷。
- 背景阈值 ≤ 脏页 < 设定阈值:唤醒后台写回线程(让它开始干活),但写进程继续写。
- **脏页 ≥ 设定阈值:写进程睡眠等待**,直到脏页降下来才让继续写。

这就是关键:**当脏页超过上限,不是后台线程在背锅,而是写进程自己被限流**。这保证了脏页比例永远在一个可控范围内,不会失控。`vmstat` 里 `dirty` 字段、`/proc/sys/vm/dirty_ratio`、`dirty_background_ratio` 这些就是调控这套机制的旋钮。

> 这是一种经典的**反压(backpressure)**:下游(磁盘写回)跟不上,就反过来让上游(写进程)慢下来,而不是让中间缓冲(脏页)无限膨胀。内存篇回收章的"kswapd 跟不上就直接回收"也是同一种思路。**系统里凡是"生产快、消费慢"的地方,几乎都有反压**。

---

## 四、掉电的真正危险:改一半,档案就坏了

写回解决了"什么时候落盘",但还有一个更阴险的问题:**万一写回的过程本身被打断(掉电、崩溃),会怎样?**

举个具体的灾难场景。文件系统要把"文件 A 从 1KB 变成 4KB"这件事落盘,它至少要改磁盘上的**两样东西**:

1. **数据**:新分配一个数据块,写进文件 A 的新内容。
2. **元数据**:更新文件 A 的 inode(`i_size` 从 1KB 改 4KB),更新块位图(那个新块标记为已用)。

假设掉电发生在**这两步之间**:

- 如果只写了数据、没更新元数据:新数据块写了但 inode 不知道 → 那个块成了**孤儿块**(占了空间,没文件认领)→ 空间泄漏。问题不大,fsck 能修。
- 如果**只更新了元数据(说"这个块归文件 A"),但那个块的数据还没写** → 文件 A 现在指向一个**装着别人旧数据的块** → 你读文件 A 读到的是垃圾,甚至可能是别的文件的内容。**这是安全漏洞**。
- 更糟的:如果掉电发生在**更新某个关键元数据(比如目录项、inode 表)的中间**——磁盘上一个 sector 只写了一半(旧的一半新的一半),文件系统的**元数据结构本身就损坏了** → 下次挂载,内核看到不自洽的元数据,**拒绝挂载,整个分区不可用**。

这才是掉电真正的恐怖之处:**不是丢数据那么简单,而是把文件系统本身搞坏,导致全盘不可用**。老文件系统(ext2)就是这种命运——掉电后必须跑一遍 `fsck`,慢且不一定修得全。

---

## 五、journaling 的核心思想:先记流水,再改账

怎么扛住"改一半"的危险?答案来自一个生活化的朴素智慧:

> **会计改账时,如果改动很大(牵涉好几笔),她不会直接在账本上涂改——而是先在旁边的"流水簿"上记下"我要怎么改"(第 X 笔:把 A 改成 B,把 C 改成 D),等流水记全了、确认无误,再去账本上真改。万一改到一半出事,拿出流水簿照着重做或撤销就行。**

这就是 **journaling(日志/日志式文件系统)**的全部思想。ext4、xfs、btrfs 都是 journaling 文件系统(ext4 用的是 jbd2 这个日志模块)。流程:

```
   市民 write
       │
       ▼
   page cache 标脏(数据先攒内存)
       │
       ▼  写回时:
   ┌───────────────────────────────────────┐
   │ 第 0 步:开一个 journal 事务(handle)  │
   │         jbd2_journal_start(...)        │
   ├───────────────────────────────────────┤
   │ 第 1 步:把"这次要改的元数据"先写进    │
   │         日志区(journal)——记流水     │
   ├───────────────────────────────────────┤
   │ 第 2 步:数据 + 元数据真正写到磁盘     │
   │         正确位置                       │
   ├───────────────────────────────────────┤
   │ 第 3 步:事务提交(commit),在日志里  │
   │         标记"这笔改完了"               │
   └───────────────────────────────────────┘
```

掉电恢复时,文件系统挂载会**重放(replay)日志**:凡是日志里"记了但没标完成"的改动,撤销;凡是"标了完成"的改动,重做一遍确保真落盘。这样,文件系统的元数据**永远处于自洽状态**——要么是改动前的样子,要么是改动后的样子,绝不会是"改一半"。

开日志事务的入口是 `jbd2_journal_start`([fs/jbd2/transaction.c#L542-L547](../linux-6.14/fs/jbd2/transaction.c#L542-L547)):

```c
handle_t *jbd2_journal_start(journal_t *journal, int nblocks)
{
	return jbd2__journal_start(journal, nblocks, 0, 0, GFP_NOFS, 0, 0);
}
```

文件系统(ext4)在改元数据前调用它,拿到一个 `handle`(事务句柄),随后的元数据改动都挂在这个 handle 下;最后由 `jbd2_journal_commit_transaction`([fs/jbd2/commit.c#L348](../linux-6.14/fs/jbd2/commit.c#L348))提交事务,把日志刷盘、标记完成。

> 注意:journaling **主要保护的是元数据**(inode、位图、目录项),因为元数据损坏会让全盘不可用。文件**内容**本身丢了,通常只是丢这一份文件的数据,不致命——所以很多模式(下面的 ordered/writeback)只日志元数据,不日志数据。

---

## 六、ext4 的三种模式:拿什么换什么

要不要把**文件数据本身**也记进日志?这是性能 vs 安全的权衡。ext4 提供三种 `data=` 模式(挂载选项,对应 [fs/ext4/ext4.h#L1207-L1208](../linux-6.14/fs/ext4/ext4.h#L1207-L1208) 的挂载标志):

```c
#define EXT4_MOUNT_ORDERED_DATA		0x00800	/* Flush data before commit */
#define EXT4_MOUNT_WRITEBACK_DATA	0x00C00	/* No data ordering */
```

| 模式 | 数据记日志吗 | 数据 vs 元数据顺序 | 安全性 | 性能 |
|------|------------|------------------|--------|------|
| **`data=ordered`**(默认) | 否 | **数据必须先于元数据落盘** | 高(不会读到旧数据) | 中(主流选择) |
| **`data=writeback`** | 否 | 不保证顺序 | 较低(可能读到旧数据/垃圾) | **最高** |
| **`data=journal`** | **是**(数据也进日志) | 数据进日志,天然有序 | **最高**(数据也不丢) | **最低**(写两遍) |

逐个理解:

### `data=ordered`(默认,最常用)

- 数据**不进日志**(省得写两遍)。
- 但**强制顺序**:元数据提交进日志前,这个文件**对应的脏数据页必须先落盘**。这就是 `__writeback_single_inode` 那个注释说的 "Make sure to wait on the data before writing out the metadata"([fs/fs-writeback.c#L1682-L1688](../linux-6.14/fs/fs-writeback.c#L1682-L1688))——写元数据前先等数据写完。
- **保证**:掉电后,绝不会出现"inode 说这个块是我的,但这块还是别人的旧数据"——因为元数据记录这个块归属之前,这个块的数据一定已经写好了。
- 代价:数据写要等,性能比 writeback 略低。**但这是大多数场景安全与性能的最佳折中,所以是默认**。

### `data=writeback`(最快)

- 数据**不进日志**,也**不保证数据先于元数据落盘**。
- **可能的问题**:掉电后可能读到旧数据(文件 A 的新内容没写,但 inode 已经指向了那个块——块里是上一个人的旧数据)。元数据本身有日志保护不会坏,但**数据可能不一致**。
- 适用:不在乎偶尔丢一点数据一致性的高性能场景(某些数据库自己有更高层的保护)。

### `data=journal`(最安全)

- **数据本身也进日志**——每个数据块改两遍(一遍进日志区,一遍进数据区)。
- 最安全:掉电后连数据内容都能恢复。
- 最慢:写放大严重。适用:极小但极重要的文件系统(比如某些嵌入式)。

> 选哪种,本质是回答一个问题:**"你能容忍掉电后读到旧数据/丢一点数据吗?"** 不能 → ordered(默认);能、要极致性能 → writeback;完全不能丢、性能无所谓 → journal。这是文件系统留给管理员的一个"安全 vs 性能"旋钮,没有标准答案,看场景。

---

## 七、关键源码精读:写回路径里的"先数据后元数据"

我们把 `__writeback_single_inode` 里那个关键的"等数据写完再写元数据"放大看([fs/fs-writeback.c#L1680-L1690](../linux-6.14/fs/fs-writeback.c#L1680-L1690)):

```c
	ret = do_writepages(mapping, wbc);          /* ① 先把脏数据页写下去 */

	/*
	 * Make sure to wait on the data before writing out the metadata.
	 * This is important for filesystems that modify metadata on data
	 * I/O completion. We don't do it for sync(2) writeback because it has a
	 * separate, external IO completion path and ->sync_fs for guaranteeing
	 * inode metadata is written back correctly.
	 */
	if (wbc->sync_mode == WB_SYNC_ALL && !wbc->for_sync) {
		int err = filemap_fdatawait(mapping);   /* ② 等数据真的落盘 */
```

这两步浓缩了 ordered 模式的精髓:

1. **`do_writepages`**:通过文件系统的 `writepages` 回调,把这个 inode 的**脏数据页**经块设备层写下去。
2. **`filemap_fdatawait`**:**等待这些数据 IO 真正完成**(落盘)。在 ordered 模式下,ext4 就是在这里确保"数据落盘"这件事发生后,才允许对应的**元数据**进入日志事务并提交。

这一前一后的等待,就是 ordered 模式"用顺序换安全"在源码里的体现。它没有把数据塞进日志(那太贵),而是靠**强制顺序**(数据先、元数据后)达到几乎等同的安全性——这是一个非常漂亮的工程权衡:**不付"写两遍"的全价,只付"等一下顺序"的低价,就拿到 90% 的安全保障**。

> 而 journal 模式则相反:它把数据也塞进日志事务,`do_writepages` 时连带数据一起记流水——付全价,换全保。ordered 和 journal 的源码差异,主要就在"哪些页进 journal handle、哪些只进数据区"这一层。

---

## 八、本章小结 + 本篇收官

用档案馆的比喻回顾本章:

- **写回**是脏页"回家"的过程。它走中间路线:**攒一批、按比例、定时地异步刷盘**(周期 5 秒 / 脏页超背景阈值 / 内存回收三种触发),绝不"立即写"(太慢)也不"无限拖"(会丢、会撑爆)。后台写回线程 `wb_workfn` 通过 `wb_writeback → __writeback_single_inode → 文件系统的 writepages` 把脏页经块设备层发给磁盘。
- **限流 `balance_dirty_pages`** 是反压:脏页超上限,写进程自己睡觉等,防止脏页失控堆积。
- **掉电的真正危险**不是丢数据,而是**改一半导致元数据不自洽**,全盘不可用。
- **journaling(日志)**的解法:**先记流水、再改账**。改动先写进日志区,再落真盘;掉电后重放日志,保证元数据永远自洽。ext4 用 jbd2 模块实现,`jbd2_journal_start` 开事务,`jbd2_journal_commit_transaction` 提交。
- **ext4 三种模式**:`ordered`(默认,数据先于元数据落盘,安全/性能折中)、`writeback`(最快,可能读到旧数据)、`journal`(最安全,数据也进日志,最慢)。拿什么换什么,看场景。

---

### 本篇全景回顾

走完第 6 篇五章,我们现在能把"市民一次 `cat /etc/hosts`"从头到尾完整说清了:

1. **VFS**(第 28 章):市民的 `read` 顺 `fd → file → inode` 摸到档案,靠操作表多态分派——这是"一切皆文件"幻觉的实现。
2. **ext4**(第 29 章):inode 真身在磁盘上,ext4 把磁盘布局成 block group,把磁盘 `ext4_inode` 翻译成内存 `struct inode`,填好操作表——这是幻觉落到真实硬件。
3. **page cache**(第 30 章):读先问前台快取柜(`address_space`),命中直接给;没命中触发预读。写借页—拷贝—标记脏,返回时还没落盘。
4. **块设备层**(第 31 章):真要下仓库,bio 合并成 request,blk-mq 多队列 + plug 攒批,让慢磁盘少做无谓挪动。
5. **写回 + journaling**(第 32 章):脏页后台异步刷盘 + 写进程限流;改动先记日志再落盘,扛住掉电。

回到全书主线:**本篇是"造幻觉那侧"(第三个幻觉:统一的文件世界)和"管资源那侧"(对付又慢又笨还会掉电的磁盘)交汇得最深的一篇**。你在这里见到了全书最完整的工程权衡链——每一个机制(VFS 抽象、page cache 缓存、bio 合并、写回限流、journaling),都是被三个现实逼出来的:**磁盘慢、会掉电、长得不一样**。当你以后遇到任何存储相关的设计觉得"为什么搞这么复杂",回来对照这三个现实,总能找到答案。

> 内核卖的三大幻觉,到此全部讲完:**独占 CPU**(进程篇)、**独占内存**(内存篇)、**统一的文件世界**(本篇)。一个市民程序活在这三层幻觉里,安心地写代码,完全不知道底下市政府忙成什么样——而你现在,已经能看穿每一层幻觉背后的全部机制了。
>
> **想继续深入**:写回看 [mm/page-writeback.c](../linux-6.14/mm/page-writeback.c) 的 `balance_dirty_pages`、[fs/fs-writeback.c](../linux-6.14/fs/fs-writeback.c) 的 `wb_writeback`/`writeback_sb_inodes`;journaling 看 [fs/jbd2/transaction.c](../linux-6.14/fs/jbd2/transaction.c)(开/结事务)、[fs/jbd2/commit.c](../linux-6.14/fs/jbd2/commit.c)(提交事务、刷日志)、[fs/ext4/inode.c](../linux-6.14/fs/ext4/inode.c) 的 `ext4_writepages`(看它怎么和 jbd2 事务配合、怎么按 ordered 模式排数据/元数据顺序)。
