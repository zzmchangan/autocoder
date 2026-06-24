# 第 28 章 · VFS:统一的"文件"幻觉

> **前置**:你需要先读过**内存篇第 11 章《page cache》**(理由:VFS 的读写最终都会落到 page cache,那一章讲清了它的内存面,本章讲它的文件面,两边拼起来才是完整的)。

**本章核心问题**:硬盘、键盘、网卡、管道、/proc……它们物理上根本不是一回事,为什么内核能让市民觉得"它们都是文件"?这层"一切皆文件"的统一幻觉,到底是怎么骗出来的?

**读完本章你会明白**:
- VFS 的四个核心对象(superblock / inode / dentry / file)各自管什么、为什么必须有四个;
- 为什么一张"操作表"(函数指针表)就能让 VFS 挂上几百种互不相干的文件系统;
- 一次 `read()` 是怎么从 `fd` 一路摸到具体文件系统的实现函数的。

> **如果一读觉得太难**:先只记住三件事——① "一切皆文件"靠四张卡片(superblock/inode/dentry/file)实现;② 每张卡片背后挂一张操作表(函数指针),`read` 实际调谁,取决于表里填了谁;③ inode 是"档案本身",dentry 是"档案的名字",file 是"市民手里这次打开的借阅条"。其余细节第二遍配合源码再抠。

---

## 一、不统一会怎样:市民程序得会说几十种方言

先别急着看 VFS 多优雅,先想清楚**没有它会多惨**。

设想内核不搞统一抽象,每种存储设备直接暴露自己的接口给市民:

- 硬盘(ext4)提供 `ext4_read_block(dev, sector, len)`;
- U 盘(vfat)提供 `fat_read_file(path)`;
- 网络盘(NFS)提供 `nfs_remote_read(host, path)`;
- 键盘是个字符设备,提供 `tty_read_chars()`;
- /proc 里的进程信息,内核得专门提供 `proc_read_status(pid)`;
- 管道、socket……各一套。

那市民程序怎么办?写个 `cat`,你得先判断"这是个 ext4 文件还是 /proc 还是设备",然后调对应的函数。移植一个 `ls` 到别的文件系统,核心逻辑得重写。更糟的是,新出一个文件系统(比如 btrfs),**所有现存的市民程序都不认识它**——除非每个程序都更新一遍。

这就是"不统一"的灾难:**市民程序和具体存储设备死死绑死,任何一边变化都得改另一边**。整个生态会僵在原地。

所以内核必须站在中间,做一件事:

> **给市民程序一套统一的、稳定的接口(open/read/write/close);不管底下是什么设备,市民都用这套接口。底下千变万化,市民无感。**

这套"统一的接口 + 把它适配到各种底层设备"的中间层,就是 **VFS(Virtual File System,虚拟文件系统)**。它造的就是内核的**第三个幻觉:统一的文件世界**。

---

## 二、VFS 的骗术:四张抽象卡片 + 一张操作表

VFS 怎么把千差万别的东西统一成"文件"?靠**四个抽象对象(卡片)**,每个对象背后挂**一张操作表(函数指针表)**。

我们用档案馆的比喻一个一个讲清楚,它们为什么是四个、不能多也不能少。

### 1. superblock:整个档案馆的户口本

一个文件系统(比如一个 ext4 分区)挂载上来,内核就给它建一个 `superblock`。它记录**这个文件系统整体的元信息**:有多大、块多大、根目录在哪、是什么类型(ext4? nfs?)。

比喻里它是**档案馆的户口本/总账**——它描述的是"整个仓库"而不是某一份档案。

它的关键字段([include/linux/fs.h](../linux-6.14/include/linux/fs.h#L1315-L1328)):

```c
struct super_block {
	struct list_head	s_list;		/* Keep this first */
	dev_t			s_dev;		/* search index; _not_ kdev_t */
	unsigned char		s_blocksize_bits;
	unsigned long		s_blocksize;
	loff_t			s_maxbytes;	/* Max file size */
	struct file_system_type	*s_type;
	const struct super_operations	*s_op;
	...
	unsigned long		s_magic;
	struct dentry		*s_root;
	...
	void			*s_fs_info;	/* Filesystem private info */
```

注意两个关键指针:

- `s_type`:它是什么文件系统(ext4、vfat、nfs……)。
- `s_op`:**指向 `super_operations`——superblock 自己的操作表**(怎么分配/释放一个 inode、怎么写超级块自己……)。这是 VFS"多态"的入口之一。
- `s_fs_info`:这个文件系统的"私有信息"(ext4 会把 `ext4_sb_info` 塞这儿,第 29 章会讲)。
- `s_root`:这棵目录树的**根 dentry**。整个文件系统的所有文件,都是从这个根长出来的。

### 2. inode:档案本身(注意,它没有名字!)

`inode`(index node)是**一份档案本身的元信息**:多大、谁建的、什么时候改过、数据放在磁盘哪些块上。

这里有个反直觉、但极其重要的点,务必记住:

> **inode 只描述"这份档案是什么样",它根本不包含文件名。**

为什么?因为同一个档案可以有**多个名字**(硬链接)——`/etc/hosts` 和 `/tmp/myhosts` 可以指向同一个 inode。如果名字存在 inode 里,一个档案就只能有一个名字了。所以 Linux 的设计是:**名字归"目录"(dentry)管,inode 只管"档案本身"**。这是 Unix 五十年没动过的经典设计。

看 inode 的核心字段([include/linux/fs.h](../linux-6.14/include/linux/fs.h#L671-L717)):

```c
struct inode {
	umode_t			i_mode;        /* 文件类型 + 权限(普通文件?目录?设备?) */
	...
	kuid_t			i_uid;
	kgid_t			i_gid;
	...
	const struct inode_operations	*i_op;   /* ← inode 的操作表 */
	struct super_block	*i_sb;              /* 我属于哪个文件系统 */
	struct address_space	*i_mapping;       /* ← 我的 page cache 账本(第30章) */
	...
	unsigned long		i_ino;              /* 我的 inode 号(全文件系统唯一) */
	...
	dev_t			i_rdev;             /* 若我是设备文件,真实设备号 */
	loff_t			i_size;             /* 我多大 */
	time64_t		i_atime_sec;        /* 访问时间 */
	time64_t		i_mtime_sec;        /* 修改时间 */
	time64_t		i_ctime_sec;        /* 元数据修改时间 */
	...
	blkcnt_t		i_blocks;           /* 我占了多少块 */
```

注意几个关键指针:

- `i_op`:**inode 操作表**(create/link/unlink/lookup……创建、改名、删除文件时调)。
- `i_sb`:这个 inode 属于哪个 superblock(哪个文件系统)。
- `i_mapping`:指向 `address_space`——**这个文件的 page cache 账本**(第 30 章专讲)。
- `i_ino`:inode 号。在同一文件系统内,一个 inode 号唯一确定一份档案。

### 3. dentry:档案的"名字卡",目录就是一堆 dentry

既然 inode 不含名字,那 `/etc/hosts` 这个路径怎么对应到 inode?靠 **dentry(directory entry)**。

dentry 是**文件名 ↔ inode 的映射**。它把"名字"和"档案本身"连起来:

- `d_name`:这个文件的名字(比如 `hosts`)。
- `d_inode`:它指向的 inode(档案本身)。
- `d_parent`:它的父目录(比如 `etc`)。

看 dentry 的核心字段([include/linux/dcache.h](../linux-6.14/include/linux/dcache.h#L91-L98)):

```c
struct dentry {
	...
	unsigned int d_flags;
	seqcount_spinlock_t d_seq;
	struct hlist_bl_node d_hash;	/* lookup hash list */
	struct dentry *d_parent;	/* parent directory */
	struct qstr d_name;            /* ← 我叫什么名字 */
	struct inode *d_inode;	/* Where the name belongs to - NULL is negative */
	...
```

一个目录,本质就是**一堆 dentry**:目录 `/etc` 里有一张"hosts → inode#123""passwd → inode#456"……的卡片堆。`ls` 列目录,就是把这张卡片堆里所有 dentry 的名字念出来。

dentry 是 VFS 的"目录缓存"(dcache)。路径解析 `/etc/hosts` 时,内核从根 dentry 出发,逐段(`etc` → 找到 etc 的 dentry → 在它的子项里找 `hosts`)往下走,沿途的 dentry 都缓存着,**下次再访问同样的路径就飞快**。

> 一个微妙点:inode 是"档案本身",可以没有 dentry(没名字的文件,比如用 `open` 后立刻 `unlink` 的临时文件,inode 还在,但已经没名字了);dentry 也可以暂时没有 inode(叫 negative dentry——"这个名字查过了,确实不存在",缓存这个否定结果,下次同样查询直接返回"没有")。

### 4. file:市民手里那张"借阅条"

前面三个对象(superblock/inode/dentry)描述的都是**档案本身**,和"谁在用"无关。但一次具体的 `open` 不一样:

- 我 `open("/etc/hosts", O_RDONLY)`,你 `open("/etc/hosts", O_WRONLY)`——我们打开的是**同一个 inode**,但我的"当前读到哪了"和你的"当前写到哪了"是各自独立的。

这个"一次具体打开的状态",就是 `struct file`。它在档案馆比喻里是**市民手里那张借阅条**:借的是同一本书(inode),但每人一张条,记录各自的进度、权限。

看 file 的核心字段([include/linux/fs.h](../linux-6.14/include/linux/fs.h#L1094-L1113)):

```c
struct file {
	file_ref_t			f_ref;
	spinlock_t			f_lock;
	fmode_t				f_mode;       /* 读模式?写模式? */
	const struct file_operations	*f_op; /* ← 这次打开用的操作表 */
	struct address_space		*f_mapping;  /* ← page cache 账本(= inode->i_mapping) */
	void				*private_data;
	struct inode			*f_inode;     /* 我打开的是哪个 inode */
	unsigned int			f_flags;      /* O_RDONLY / O_NONBLOCK ... */
	...
	loff_t				f_pos;        /* ← 我当前读到/写到第几个字节 */
```

注意 `f_op`:**这次打开操作表**。`f_pos`:**当前文件偏移**——这是属于"这次打开"而不是"文件本身"的,所以放在 file 而不是 inode。

而市民程序拿到的,其实是 file 的一个**编号 fd(file descriptor)**。`fd` 是个整数,在进程的 `files_struct` 里是个指针数组的下标,`fd = 3` 就指向 `files[3]` 这个 `struct file *`。

### 四张卡片一句话总结

| 对象 | 比喻 | 管什么 | 和"谁在用"有关吗 |
|------|------|--------|------------------|
| superblock | 档案馆户口本 | 整个文件系统 | 无 |
| inode | 档案本身 | 这份文件多大、数据在哪 | 无 |
| dentry | 名字卡 | 文件名 ↔ inode | 无 |
| file | 借阅条 | 一次打开的进度/模式 | **有** |

为什么要分四个?因为它们的生命周期和关注点不同:**档案本身(inode)和名字(dentry)分开**,才能支持硬链接;**档案本身和某次打开(file)分开**,才能多人同时读同一个文件而进度互不干扰。这是 Unix 经过几十年沉淀的精巧分层。

---

## 三、关键:每张卡片背后那张"操作表"(多态的来源)

到这里你可能会问:VFS 定义了四个统一的 struct,但 ext4 和 NFS 的实现完全不同,VFS 怎么知道该调谁的代码?

答案就是每个对象里那个**指向操作表的指针**:

- `super_block->s_op`:指向 `struct super_operations`;
- `inode->i_op`:指向 `struct inode_operations`;
- `inode->i_mapping->a_ops`:指向 `struct address_space_operations`(读写页时用,第 30 章);
- `file->f_op`:指向 `struct file_operations`(read/write/llseek/mmap……时用)。

这些"操作表"长什么样?就是**一整张函数指针表**。看 `file_operations`([include/linux/fs.h](../linux-6.14/include/linux/fs.h#L2131-L2150)):

```c
struct file_operations {
	struct module *owner;
	fop_flags_t fop_flags;
	loff_t (*llseek) (struct file *, loff_t, int);
	ssize_t (*read) (struct file *, char __user *, size_t, loff_t *);
	ssize_t (*write) (struct file *, const char __user *, size_t, loff_t *);
	ssize_t (*read_iter) (struct kiocb *, struct iov_iter *);
	ssize_t (*write_iter) (struct kiocb *, struct iov_iter *);
	...
	int (*mmap) (struct file *, struct vm_area_struct *);
	int (*open) (struct inode *, struct file *);
	...
	int (*fsync) (struct file *, loff_t, loff_t, int datasync);
```

这就是一张表,每一格是一个函数指针(`read`、`write`、`llseek`……)。

**每个文件系统在挂载/创建对象时,把自己的函数填进这张表**。比如 ext4 会把 `ext4_file_read_iter` 填到 `read_iter` 那格;NFS 会把 `nfs_file_read` 填进去;一个设备驱动会把自己的 `xxx_read` 填进去。

于是 VFS 的代码可以这样写(伪代码):

```c
ssize_t vfs_read(...) {
    ...
    ret = file->f_op->read(file, buf, count, pos);   // 调谁?取决于 f_op 指向哪张表
    ...
}
```

`file->f_op->read`——这一句,就是整个"一切皆文件"骗术的核心。**VFS 只管"调 `f_op` 表里的 `read`",至于 `read` 具体执行什么代码,取决于表里填了谁**。ext4 填的就是 ext4 的读、设备驱动填的就是驱动的读、/proc 填的就是内核现造的读。

> 这其实就是 C 语言实现**面向对象多态**的标准手法。VFS 把"接口"(操作表)和"实现"(各文件系统填表)解耦——这正是它能挂上几百种互不相干文件系统的根本原因。新出一个 btrfs,只要它按规矩填好这几张表,VFS 和所有现存市民程序立刻就能用,不用改一行。

---

## 四、串起来:一次 `read()` 在 VFS 里走了一遍

把前面三节拼起来,看一次 `read(fd, buf, 4096)` 在 VFS 这一层发生了什么。

市民发起 `read` 系统调用,进到内核的 `ksys_read` → `vfs_read`。看 `vfs_read` 的真实代码([fs/read_write.c](../linux-6.14/fs/read_write.c#L545-L573)):

```c
ssize_t vfs_read(struct file *file, char __user *buf, size_t count, loff_t *pos)
{
	ssize_t ret;

	if (!(file->f_mode & FMODE_READ))
		return -EBADF;
	if (!(file->f_mode & FMODE_CAN_READ))
		return -EINVAL;
	if (unlikely(!access_ok(buf, count)))
		return -EFAULT;

	ret = rw_verify_area(READ, file, pos, count);
	if (ret)
		return ret;
	if (count > MAX_RW_COUNT)
		count =  MAX_RW_COUNT;

	if (file->f_op->read)
		ret = file->f_op->read(file, buf, count, pos);
	else if (file->f_op->read_iter)
		ret = new_sync_read(file, buf, count, pos);
	else
		ret = -EINVAL;
	...
}
```

逐段看它干了什么:

1. **权限检查**:这个 file 是不是以可读模式打开的?(`f_mode & FMODE_READ`)不是,直接返回 `EBADF`。市民不能越权。
2. **地址检查**:`access_ok` 确认市民给的 `buf` 地址是合法的用户态地址(否则内核往一个非法地址写会崩——这是边界篇讲的"不能信市民"的体现)。
3. **范围检查**:`rw_verify_area`,确认要读的区间合法(比如对加锁的文件区域检查锁)。
4. **派发**——这是最关键的一句:

   ```c
   if (file->f_op->read)
       ret = file->f_op->read(file, buf, count, pos);
   else if (file->f_op->read_iter)
       ret = new_sync_read(file, buf, count, pos);
   ```

   `file->f_op->read`——**调操作表里的 read**。这个 file 是 ext4 文件打开的,`f_op` 指向 ext4 的表,这里就执行 ext4 的读;是设备文件,就执行驱动的读。VFS 自己一行具体读写都没写,它只负责"按规矩派发"。

注意现代文件系统大多用 `read_iter`(异步、支持缓冲迭代器)而不是老的 `read`,所以走的是 `new_sync_read` → 最终 `file->f_op->read_iter`。但派发的本质一样:**通过 `f_op` 表多态分派**。

> 顺带一提:`f_op`、`f_mapping`(= `inode->i_mapping`)都指向了"下一层"——这预示着 `read` 真正的活儿,会先去 page cache(下一站,第 30 章)找,而不是立刻冲去磁盘。

---

## 五、关键源码精读:四个对象怎么挂在一起

`vfs_read` 展示了 `file → f_op` 的派发。我们再把四个对象的连接关系在源码层面串一遍,这是理解 VFS 的"骨架":

```
   市民: fd = 3
              │
              │  (进程的 files_struct->fdt[fd])
              ▼
          struct file ──f_inode──► struct inode ──i_sb──► struct super_block
              │                       │                        │
              │f_op                   │i_op                    │s_op
              ▼                       ▼                        ▼
        file_operations       inode_operations         super_operations
        (read/write/...)      (lookup/create/...)      (alloc_inode/...)

          struct file ──f_mapping──► struct address_space ◄──i_mapping── struct inode
                                       │
                                       │a_ops
                                       ▼
                               address_space_operations
                               (readpage/writepage/...)

   struct dentry ──d_inode──► struct inode
              │
              d_name="hosts", d_parent=<etc 的 dentry>
```

几个要点,对照源码记牢:

1. **`file->f_inode`** 和 **`file->f_mapping`**:file 指向它打开的 inode,以及 inode 的 address_space(缓存账本)。这两个是 file 最常用的"出口"。
2. **`inode->i_sb`**:任何对象都能顺到它所在的 superblock(即所在的文件系统)。`i_sb->s_type` 告诉你这是 ext4 还是别的。
3. **`dentry->d_inode`**:名字卡指向档案本身。路径解析时,内核靠一连串 dentry 的 `d_parent`/`d_children` 在目录树里导航。
4. **每个对象一张操作表**:这是 VFS"多态"的全部机制。你在源码里看到 `xxx->yy_op->zz(...)`,就知道这是 VFS 在"按规矩派发"给具体文件系统。

理解了这张图,你就理解了 VFS 的全部骨架——后面四章(ext4/page cache/块设备/写回)其实都是在讲:**这些操作表里某个函数,真正实现时干了什么**。

---

## 六、本章小结

用档案馆的比喻回顾本章:

- **档案馆(VFS)** 给市民一套统一接口(open/read/write/close),市民不用认识几十种存储设备的方言——这是内核卖的**第三个幻觉:统一的文件世界**。
- 这套幻觉靠**四张卡片**实现:superblock(档案馆户口本)、inode(档案本身,**没名字**)、dentry(名字卡,目录就是一堆它)、file(市民的借阅条,**带进度**)。分四个,是因为档案本身/名字/某次打开,生命周期和关注点不同。
- 真正的"多态"来自**每张卡片背后的操作表(函数指针表)**。VFS 只管"调表里的 read",具体执行谁的代码,看表里填了谁。ext4 填 ext4 的,设备填驱动的,/proc 填内核现造的。这就是 C 语言版的面向对象多态,也是 VFS 能挂上百种文件系统的根本。

回到全书主线:VFS 是**造幻觉那侧**(第三个幻觉)的核心机制。但请注意——本章自始至终,VFS 都没真碰过磁盘。它只管"派发"。真正把数据摆上磁盘、真正去仓库取货的,是具体文件系统(下一章 ext4)+ page cache(第 30 章)+ 块设备层(第 31 章)。

> **想继续深入**:看 [fs/super.c](../linux-6.14/fs/super.c) 的 `mount` 流程怎么建 superblock;看 [fs/namei.c](../linux-6.14/fs/namei.c) 的 `path_lookup` 怎么逐段解析路径、构建 dentry 链;看任一具体文件系统(如 [fs/ext4/file.c](../linux-6.14/fs/ext4/file.c))怎么填 `file_operations` 那张表。填表的过程,就是一个文件系统"接入 VFS"的过程。
>
> **下一章**:VFS 的卡片和操作表都是空的框架——谁来填?第 29 章我们请出 Linux 上最主流的 ext4,看它怎么把磁盘上的字节布局成 block group,怎么把磁盘上的 `ext4_inode` 读成内存里的 `struct inode`,然后把操作表填上。
