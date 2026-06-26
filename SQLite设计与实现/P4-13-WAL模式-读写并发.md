# 第 4 篇 · 第 13 章 · WAL 模式:读写并发

> **核心问题**:上一章 rollback journal 的硬伤是——写事务一开始就要把整个数据库**独占**(拿 RESERVED→PENDING→EXCLUSIVE 锁链),在写事务提交前,任何读连接都得排队干等。这在"读多写少"的 Web/嵌入式场景里让人抓狂(一个长写事务能把所有读堵死)。WAL(Write-Ahead Logging)模式就是来治这个病的:**写的事先把新内容追加到一旁的 WAL 文件,完全不碰主数据库文件;读的时候同时读主库 + WAL。** 这样写可以一直往后追加,读可以一直读老的快照,互不打扰——**读不阻塞写、写不阻塞读**。可凭什么能做到?多读一页要怎么知道"WAL 里有没有这页更新的版本、在第几帧"?wal-index(共享内存)是怎么让 reader O(1) 定位页版本的?WAL 长大了怎么办(checkpoint 四种模式)?为什么 WAL 已经这么强了,SQLite 却仍是**单写者**(同一时刻只准一个 writer)?

> **读完本章你会明白**:
> 1. **为什么 WAL 能让读不阻塞写、写不阻塞读**——writer 只往 WAL 追加 frame、不覆盖主库;reader 拿一份"我看到这一帧为止"的快照(read-mark),writer 往后追加新 frame 不影响老 reader;这正是 MVCC-like 的版本快照思想,但要轻量得多。
> 2. **wal-index(共享内存 `xxx.db-shm`)凭什么让 reader O(1) 定位每页最新版本**——它是一张"页号→WAL 帧号"的哈希表 + 一份只增不减的页号数组,reader 一次哈希 + 线性探测就拿到帧号,**不用扫整个 WAL**;没有它,WAL 大起来后读会慢到不可用。
> 3. **WAL 的二进制格式**(WAL header 32 字节 + 每帧 24 字节 frame header + 页数据;magic + salt + fibonacci 反序校验和),以及为什么是"新内容"(重做日志方向),和 rollback journal 记"旧内容"恰好相反。
> 4. **checkpoint 的四种模式**(PASSIVE/FULL/RESTART/TRUNCATE)各自做什么、什么时候用、为什么 checkpoint 不能覆盖"还有 reader 在用"的旧 frame。
> 5. **为什么 SQLite 即便有了 WAL 仍是单写者**——WAL 是 append-only,多 writer 同时追加会乱、校验和链会断;这是"用单写换读并行"的精明取舍。
> 6. **WAL vs rollback journal 的对照表**(并发/性能/适用场景/文件数/网络盘),以及一句承《MySQL·InnoDB》redo:同是"改前先记日志",但 SQLite WAL 记新内容、InnoDB redo 记物理逻辑操作,SQLite 无 undo 靠 WAL 版本快照读。

> **如果一读觉得太难**:先记住三件事——① 写的事先记到一旁的 WAL(`.db-wal`)、不动主库;② 读时主库 + WAL 一起读,reader 各看各的快照所以不互相挡;③ WAL 攒多了就 checkpoint(把 WAL 内容搬回主库)。其余的 wal-index 哈希、salt、checkpoint 四模式都是为了让这三件事**又快又正确**的工程细节。

---

## 〇、一句话点破

> **WAL 的全部魔法,可以浓缩成一句:writer 改页时,把"新内容 + 页号 + commit 标记"追加成一个 frame 到 WAL 文件、完全不碰主库;reader 各持一份"我看到第 N 帧为止"的快照(read-mark),读一页时先查 wal-index(共享内存里的页号→帧号哈希表)决定从主库还是 WAL 拿——于是写可以一直追加,读可以一直读老快照,互不挡。**

这是结论,不是理由。本章倒过来拆:先讲 rollback journal 为什么挡路(承上一章),再讲 WAL 的核心三件套(只追加 WAL、共享内存 wal-index、read-mark 快照),然后把二进制格式、校验和、checkpoint 四模式、单写者这四个为什么一个个钉死,最后做一张 WAL vs rollback journal 对照表收口。

---

## 一、为什么需要 WAL:rollback journal 挡在哪

要理解 WAL,先看清它要解决的病。上一章讲的 rollback journal(默认模式)的提交协议是这样的:

1. 改一页**之前**,先把这页的**原内容**写进 `xxx.db-journal` 文件(记旧值,留待回滚);
2. 拿 PENDING→EXCLUSIVE 锁,**独占**整个数据库;
3. 把改过的页写回主库 `.db`;
4. fsync 主库;
5. 删/清 journal(提交完成,旧值不再需要)。

这套协议**简单可靠**(crash 后用 journal 把页改回去就行),但有一个致命短板:**第 2 步一拿 EXCLUSIVE,整个数据库就被这个写连接独占了,在它提交之前,任何别的读连接都拿不到 SHARED 锁,只能排队干等。** 一个写得慢的事务(比如批量插入、或 fsync 卡了几百毫秒),会把所有读全堵住。

> **不这样会怎样**:设想一个典型的 Web 应用——大量并发读请求 + 偶尔的写。用 rollback journal 模式时,只要一个写事务在进行,所有读都被挡在门外;写事务越长,读堆积越严重。在"读多写少"的场景(这正是 SQLite 最常见的用法——配置存储、缓存索引、本地数据),这个短板尤其扎眼。**WAL 就是为治这个病生的。**

WAL 模式(`PRAGMA journal_mode=WAL`,3.7+ 引入,2010 年)反过来做:

1. 改一页时,**不写主库**,而是把这页的**新内容 + 页号**追加成一个 frame 到 `xxx.db-wal`;
2. 一个事务结束(COMMIT)时,追加一个带 commit 标记的 frame,并更新共享内存 wal-index 里的 `mxFrame`;
3. 读的时候,reader 先查 wal-index:"这页在 WAL 里有没有更新的版本?如果有,在第几帧?",有就从 WAL 读那帧、没有就从主库读;
4. WAL 攒多了,后台 checkpoint 把 WAL 里已提交的 frame **搬回主库**。

关键差别:rollback journal **改前记旧值、改时写主库**(写时独占);WAL **改时记新值、改时不碰主库**(写时不独占)。就这一条差别,把"写时独占"换成了"读不阻塞写、写不阻塞读"。

> **钉死这件事**:WAL 不是 rollback journal 的小改,而是**把日志的方向整个反过来**——rollback journal 记旧值(用于回滚),WAL 记新值(用于读 + 重做)。这个"方向反过来"是理解后面一切(读怎么读、checkpoint 怎么搬、crash 怎么恢复)的总开关。承《MySQL·InnoDB》:InnoDB 的 redo 也是记新值(物理逻辑操作,用于重做),思想同源;只是 SQLite 嵌入式、无 undo,靠 WAL 的版本快照做 MVCC-like 的并发读,比 InnoDB 的 undo 版本链简单得多。

---

## 二、核心三件套:WAL 文件、wal-index、read-mark

WAL 模式能"读不阻塞写、写不阻塞读",靠的是三件套配合。一个个拆。

### 2.1 第一件套:WAL 文件(`.db-wal`),只追加、不覆盖主库

writer 改一页时,干的事是:**把这一页的新内容,加上页号,包装成一个 frame,追加到 WAL 文件的末尾**。主库 `.db` 文件在写事务期间**一个字节都不动**。

```
   rollback journal 模式(改前记旧值、改时写主库):
     改页 P:  ① journal 写 P 的旧值  ② 主库写 P 的新值  (写时独占)

   WAL 模式(改时记新值、改时不碰主库):
     改页 P:  ① WAL 追加一个 frame(P 的新值 + 页号 P)  (写时不碰主库)
```

一个 WAL 文件长这样(WAL header 32 字节 + 一串 frame,每个 frame = 24 字节 frame header + 一页数据):

```
   ┌──────────────────────────────────────────────────────────────────┐
   │ xxx.db-wal                                                        │
   │ ┌──────────────────────────────────────────────────────────────┐ │
   │ │ WAL header (32 字节)                                          │ │
   │ │   0:  magic      0x377f0682 或 0x377f0683(决定校验和字节序)│ │
   │ │   4:  版本       3007000                                     │ │
   │ │   8:  页大小     如 4096                                     │ │
   │ │  12:  checkpoint 序号 nCkpt                                  │ │
   │ │  16:  salt-1     随机数,每次 checkpoint 递增                 │ │
   │ │  20:  salt-2     随机数,每次 checkpoint 重置                 │ │
   │ │  24:  checksum-1 (前 24 字节的校验和)                        │ │
   │ │  28:  checksum-2                                              │ │
   │ └──────────────────────────────────────────────────────────────┘ │
   │ ┌──────────────────────────────────────────────────────────────┐ │
   │ │ frame 1: 24B header + 1 页                                   │ │
   │ │   header: 页号 | 提交后 db 页数(0=非提交帧) | salt1 | salt2 │ │
   │ │            | cksum1 | cksum2                                  │ │
   │ │   data:   4096 字节页内容                                    │ │
   │ └──────────────────────────────────────────────────────────────┘ │
   │ ┌──────────────────────────────────────────────────────────────┐ │
   │ │ frame 2 ...                                                    │ │
   │ └──────────────────────────────────────────────────────────────┘ │
   │ ...frame N (commit frame:nTruncate>0 表示事务提交)               │
   └──────────────────────────────────────────────────────────────────┘
   定义见 src/wal.c:WAL_HDRSIZE=32 (#L480)、WAL_FRAME_HDRSIZE=24 (#L477)、
                  WAL_MAGIC=0x377f0682 (#L491),格式注释 wal.c#L34-L98。
```

注意三个关键设计:

- **每个 frame 带 salt**:salt-1、salt-2 从 WAL header 复制下来。每次 checkpoint 重启 WAL 后,salt 会变(下面 2.3 讲为什么)。reader 验 frame 时先看 salt 对不对——salt 不对,说明这帧是上一轮 WAL 的残骸,作废。
- **每个 frame 带校验和**:而且是**链式**校验和——第 N 帧的校验和,是把"WAL header + 前 N-1 帧的全部内容 + 第 N 帧的前 8 字节"一起算出来的。这意味着任何一帧被改、损坏,后面所有帧的校验和都对不上。crash 恢复时,reader 从头验校验和,第一个验不过的帧之后的全部丢弃。
- **commit frame**:一个事务的最后一帧,frame header 的第 4 字节(`nTruncate`)非零,记录"提交后数据库该有多少页"。reader 只认"后面跟着 commit frame 或自己就是 commit frame"的帧——一个事务写到一半 crash(没 commit frame),恢复时整个丢弃。

> **所以这样设计**:writer 只追加 WAL、不碰主库,这是"读不阻塞写"的物理基础——既然写不覆盖主库里 reader 正在读的字节,reader 自然不怕被写冲掉。commit 只是"往 WAL 追加一个 commit frame + 更新 wal-index 的 mxFrame",不需要独占主库。

### 2.2 第二件套:wal-index(共享内存 `.db-shm`),让 reader O(1) 定位页版本

光有 WAL 文件还不够。reader 读一页 P 时,面临一个问题:**P 在 WAL 里可能被改过(有更新的版本),也可能没改过;改过的话,可能改了好几次(WAL 里有多帧都是 P),我要的是"我看到的快照范围内、最后一次提交的那帧"。** 怎么找?

朴素的办法是**扫整个 WAL**:从最后一帧往前扫,找到第一帧页号等于 P 的,就是它的最新版本。但 WAL 可能几 MB 甚至几十 MB(典型场景),每次读一页都全扫一遍,读性能直接崩。

> **不这样会怎样**:没有 wal-index,reader 每读一页都得扫整个 WAL。WAL 越大扫得越久——一个几 MB 的 WAL,一次读可能要扫几千帧。SQLite 的设计目标之一是"读快",这个朴素方案直接毁了 WAL 的实用价值。wal.c 文件头注释明确写了这段动机:"because frames for page P can appear anywhere within the WAL, the reader has to scan the entire WAL ... that scan can be slow, and read performance suffers. To overcome this problem, a separate data structure called the wal-index is maintained."(wal.c#L119-L125)。

**wal-index 就是来解决这个的。** 它是一块**共享内存**(shared memory),在默认 unix/windows 实现里是一个用 mmap 映射的文件,名字叫 `xxx.db-shm`(所以也叫 shm 文件)。它记录两样东西:

1. **WalIndexHdr**:WAL 的元数据——`mxFrame`(WAL 里最后一帧的帧号)、`nPage`(数据库页数)、`aSalt`(两个 salt)、页大小、校验和等。reader 一进来先读这个,知道"WAL 现在有多长"。
2. **页号→帧号的哈希表**:这是核心。对 WAL 里**每一帧**,记录"这帧对应数据库哪一页"。reader 要读页 P 时,哈希一下 P,顺着哈希链找,几步就能拿到"P 在 WAL 里最新一帧是第几帧"。

wal-index 的 136 字节头部布局(SQLite 官方画的,见 wal.c#L403-L466 注释):

```
   xxx.db-shm 的前 136 字节(头部):
   ┌─────────────────────────────────────────────────────────┐
   │   0: WalIndexHdr 第一份拷贝 (48 字节)                    │ \
   │      iVersion | (pad) | iChange | isInit | bigEndCksum   │  │  两份相同拷贝,
   │      | szPage | mxFrame | nPage | aFrameCksum | aSalt    │  │  reader 比对哪份
   │      | aCksum                                             │  │  有效(防写到一半
   │  48: WalIndexHdr 第二份拷贝 (48 字节, 内容应与第一份一致) │  │  crash)
   ├─────────────────────────────────────────────────────────┤  │
   │  96: WalCkptInfo                                          │  │
   │      nBackfill         (已 checkpoint 回主库的帧数)        │  │
   │      aReadMark[5]      (5 个 reader 的读快照标记)         │  │  共 136 字节
   │      aLock[8]          (8 个锁字节, 见 2.3)              │  │
   │      nBackfillAttempted (checkpoint 试图回填到的帧数)     │  │
   ├─────────────────────────────────────────────────────────┤  │
   │ 120: 8 个锁字节 (Write|Ckpt|Rcvr|Rd0|Rd1|Rd2|Rd3|Rd4)    │ /  ← 锁都在 shm 上
   └─────────────────────────────────────────────────────────┘
   定义:WalIndexHdr (wal.c#L321-L333)、WalCkptInfo (wal.c#L394-L401)。

   头部之后,是若干个"索引块",每块 32KB(WALINDEX_PGSZ, wal.c#L627-L629),
   每块包含:
   ┌─────────────────────────────────────────────────────────┐
   │  页号数组 aPgno[]:4096 个 u32, 每个是某帧对应的数据库页号 │
   │     (第一块只有 4062 个, 因为前 136 字节被头部占了)        │
   │  哈希表 aHash[]:8192 个 u16(ht_slot), 开放寻址          │
   │     每个 slot 存"在 aPgno 里的 1-based 下标"(即帧在本块的相对位置)│
   └─────────────────────────────────────────────────────────┘
   HASHTABLE_NPAGE=4096、HASHTABLE_NPAGE_ONE=4062、HASHTABLE_NSLOT=8192(wal.c#L615-L624)。
```

为什么 aHash 有 8192 个 slot、aPgno 只有 4096 个条目?**哈希表装填因子固定不超过 0.5**——这样开放寻址的冲突次数期望是 1(几何分布),reader 几乎一步命中。这是个经典的"用空间换时间"的取舍:多花一倍内存(哈希表是页号数组的一半大),换 reader 的 O(1) 定位。

reader 查一页 P 的流程(`walFindFrame`,wal.c#L3525-L3627):

1. 从自己持有的 `mxFrame`(快照上限)算出可能涉及的索引块范围;
2. 从最后一块往前,对每块哈希 `walHash(P) = (P * 383) & 8191`(wal.c#L1138),顺着冲突链线性探测,找到第一个 slot;
3. 读 slot 里的值 → 换算成帧号 → 查 aPgno 确认页号真的是 P(过滤哈希冲突)→ 且这帧在自己快照范围内 → 记下;
4. 一旦在某块找到了,就 break(那块里的就是最新版本)。

这一套下来,**reader 找一页的 WAL 版本,平均常数次内存访问**,不用扫整个 WAL。这就是 wal-index 的全部价值。

> **钉死这件事**:wal-index 是共享内存,不是普通文件内容——它 mmap 映射到所有连接的地址空间,大家看到同一份。这意味着所有 reader 共享同一份页号→帧号索引,writer 追加新 frame 时也往这份索引里追加新条目。**这就是"读不阻塞写、写不阻塞读"的索引基础**:reader 查老快照范围内的索引、writer 往索引末尾追加新条目,两者操作的是不同区域,互不干扰。也正因为它依赖共享内存,**SQLite 的 WAL 模式在网络上(NFS/SMB)用不了**——网络盘不能可靠地共享内存(wal.c#L129-L133 明确说了)。这是 WAL 的一个重要限制。

### 2.3 第三件套:read-mark,reader 各看各的快照

光有 WAL 和 wal-index 还不够回答"凭什么读不阻塞写"。关键在 read-mark。

reader 开始一个读事务时(`walTryBeginRead`,wal.c#L3020 起,核心选择逻辑 wal.c#L3169-L3269),干两件事:

1. **拿一个 read-mark 槽位**:wal-index 头部有 `aReadMark[5]` 五个槽位(`WAL_NREADER=5`,wal.c#L299),每个槽位存一个帧号,意思是"持这个槽位的 reader,看到的快照上限是这个帧号"。reader 会挑一个"最接近 `mxFrame`、但不超过"的槽位(通常是最新那个),拿它的 `SHARED` 锁(锁字节 `WAL_READ_LOCK(i)`,wal.c#L298)。
2. **记下自己的快照**:`pWal->hdr.mxFrame` = 这个 read-mark 的值。从此**这个 reader 读所有页,都以这个 mxFrame 为上限**——WAL 里帧号超过 mxFrame 的,它一律不认。

> **所以这样设计(读不阻塞写的核心)**:writer 往后追加新 frame、更新 wal-index 里的(全局)mxFrame;但 reader **自己缓存了一份老的 mxFrame**(它的 read-mark 快照),它只认这个老值。writer 追加多少新 frame,跟这个 reader 无关——reader 看到的永远是"它开始读那一刻"的数据库快照。**这就是 MVCC-like 的版本快照读**,和 InnoDB 用 undo 版本链做 MVCC 思想同源,但实现轻量得多(SQLite 无 undo,直接用 WAL 里的多版本 + read-mark 截断)。

> **写不阻塞读的核心**:writer 只追加 WAL、不覆盖主库,也不覆盖 wal-index 里老 reader 用到的索引区域(只往末尾追加新条目)。所以 reader 读主库的页、读 wal-index 的老条目,都不会被 writer 的写冲掉。这就是"写不阻塞读"。

特殊地,`aReadMark[0]` 是个占位符——持 `WAL_READ_LOCK(0)` 的 reader 表示"我完全忽略 WAL,所有数据直接从主库读"。这种 reader 不挡 checkpoint(下面 2.4 讲为什么这很重要)。

为什么 read-mark 有 5 个槽位而不是 1 个?因为**同时可能有多个 reader 看不同时点的快照**(有的 reader 开得早、看老快照;有的开得晚、看新快照)。每个 reader 占一个槽位,各自标各自的 mxFrame 上限。5 个是个折中——够大多数并发场景用,不够时 SQLite 会尝试抢占/复用(WAL 模式默认 reader 不阻塞,但 read-mark 槽位有限,极端情况下会 `WAL_RETRY`)。

### 2.4 checkpointer 不能覆盖"还有 reader 在用"的旧 frame

读不阻塞写,那 checkpoint 呢?checkpoint 是把 WAL 里已提交的 frame 搬回主库。如果某个 reader 还在用一个老快照(它的 read-mark 指向 WAL 中段的某帧),checkpoint 能不能把那帧之前(含)的 frame 都搬回主库、然后清掉?

**不能。** 因为一旦搬回主库 + WAL 重启,那个老 reader 再读这些页时,会从主库读——可主库已经被 checkpoint 改成新版本了,老 reader 就读到了它不该看到的新数据,快照被破坏。

所以 checkpoint 有一个硬约束:`mxSafeFrame`(能安全回填的最大帧号)= **所有在用 read-mark 的最小值**。任何帧号超过 `mxSafeFrame` 的,一律不能回填,必须留在 WAL 里给老 reader 用(`walCheckpoint`,wal.c#L2228-L2255):

```c
    mxSafeFrame = pWal->hdr.mxFrame;          // 先取 WAL 最大帧
    for(i=1; i<WAL_NREADER; i++){
      u32 y = AtomicLoad(pInfo->aReadMark+i);  // 看每个 reader 的快照
      if( mxSafeFrame>y ){
        // 有 reader 的快照比当前的小, checkpoint 最多只能回填到 y
        rc = walBusyLock(..., WAL_READ_LOCK(i), 1);
        if( rc==SQLITE_OK ){
          // 这个 read-mark 没人持(抢到了), 可以推进它
          AtomicStore(pInfo->aReadMark+i, iMark);
          ...
        }else if( rc==SQLITE_BUSY ){
          mxSafeFrame = y;    // 有 reader 占着, 收紧回填上限
          xBusy = 0;
        }
      }
    }
```

这段就是 checkpoint 和 reader 协调的核心:**checkpoint 回填到 `min(所有在用 read-mark)` 为止,再往后就停**,给老 reader 留着。这就是为什么持 `WAL_READ_LOCK(0)` 的 reader(reader 读主库、忽略 WAL)**不挡 checkpoint**——它的 read-mark 不在 `aReadMark[1..4]` 里,不计入 mxSafeFrame。

> **钉死这件事**:WAL 模式的"读不阻塞写、写不阻塞读"不是无条件的——checkpoint 会和"看老快照的 reader"互相制约(checkpoint 不能覆盖老 reader 还在用的 frame;老 reader 持着锁,checkpoint 就只能回填一部分)。这正是 PASSIVE 模式默认存在的理由:它不强求回填完所有帧,能回填多少算多少,不挡 reader 也不挡 writer。

---

## 三、WAL 写一页:writer 怎么追加 frame

现在从 writer 的视角看一次写。假设一个事务要改页 P。

### 3.1 拿 WRITE_LOCK(单写者)

writer 一开始先调 `sqlite3WalBeginWriteTransaction`(wal.c#L3699-L3746),核心就一行:

```c
  rc = walLockExclusive(pWal, WAL_WRITE_LOCK, 1);   // 拿排他写锁
  if( rc ){
    return rc;   // 拿不到(SQLITE_BUSY),说明已有 writer 在写
  }
  pWal->writeLock = 1;
```

`WAL_WRITE_LOCK` 是锁字节 0(wal.c#L294),**全数据库同一时刻只有一个 writer 能拿到它**——这就是 WAL 模式**单写者**的物理实现(后面第五节专门讲为什么必须单写者)。

拿到写锁后,writer 还会检查"自己开始读之后,有没有别人写过了"(wal.c#L3734-L3738):如果有(memcmp wal-index header),返回 `SQLITE_BUSY_SNAPSHOT`——因为如果这时让这个 writer 写,它会基于老快照改,和别人的新提交冲突("fork")。

### 3.2 改页 → 追加 frame

事务进行中,writer 改一页 P(pager 把 P 标脏、加进 dirty list)。事务提交时(`pagerWalFrames`,pager.c#L3225-L3267 → `sqlite3WalFrames` → `walFrames`,wal.c#L4038 起),干两件事:

1. **遍历 dirty 页列表**,对每个脏页调 `walEncodeFrame` 把它编码成一个 frame,追加到 WAL 文件:

```c
static void walEncodeFrame(Wal *pWal, u32 iPage, u32 nTruncate, u8 *aData, u8 *aFrame){
  ...
  sqlite3Put4byte(&aFrame[0], iPage);       // 帧头:页号
  sqlite3Put4byte(&aFrame[4], nTruncate);   // 帧头:提交后 db 页数(非提交帧=0)
  memcpy(&aFrame[8], pWal->hdr.aSalt, 8);   // 帧头:salt(从 WAL header 复制)
  walChecksumBytes(nativeCksum, aFrame, 8, aCksum, aCksum);  // 链式校验和:
  walChecksumBytes(nativeCksum, aData, pWal->szPage, aCksum, aCksum); // 先前帧+本帧
  sqlite3Put4byte(&aFrame[16], aCksum[0]);  // 帧头:校验和
  sqlite3Put4byte(&aFrame[20], aCksum[1]);
}
```

2. **每个 frame 写完后,更新 wal-index 的索引**(`walIndexAppend`,wal.c#L1301-L1345):往当前索引块的 aPgno 末尾写页号、往 aHash 插一个 slot 指向它。这样 frame 一旦写进 WAL + 索引更新,reader 就能查到。

最后一个 frame 是 commit frame(`nTruncate` 非 0,表示"提交后 db 该有多少页")。commit frame 写完 + fsync WAL 后,writer 更新 wal-index 的 `mxFrame`(让新 reader 能看到这次提交)。

> **关键点 1:commit 不写主库**。整个事务期间,主库 `.db` 一个字节都没动。COMMIT 在 WAL 模式下的物理意义是:**"往 WAL 追加一个 commit frame + 更新 wal-index 的 mxFrame"**——仅此而已。这就比 rollback journal 的"COMMIT 要 fsync 主库"快得多(尤其在小事务、频繁提交的场景)。

> **关键点 2:writer 可以在中途把脏页"溢出(spill)"到 WAL**。如果一个事务改的页太多、pager 缓存放不下,pager 会把一部分脏页**提前**写进 WAL(非 commit frame,`isCommit=0`),腾出缓存。这些帧 wal-index 也会索引,但因为后面没 commit frame,reader 不会认它们(见第四节 reader 算法)。这是 WAL 模式独有的内存管理手段——rollback journal 做不到(它必须独占写、不能中途溢出)。

### 3.3 一个 frame 在磁盘上的位置

frame N 在 WAL 文件里的字节偏移(`walFrameOffset`,wal.c#L498-L500):

```
   offset(N) = WAL_HDRSIZE + (N-1) * (szPage + WAL_FRAME_HDRSIZE)
             = 32 + (N-1) * (4096 + 24)   // 假设页大小 4096
```

reader 读 frame N 的页数据时,直接 `sqlite3OsRead(pWalFd, pOut, szPage, offset(N) + 24)`(跳过 24 字节帧头),见 `sqlite3WalReadFrame`(wal.c#L3658-L3673)。一次定位读取,O(1)。

---

## 四、WAL 读一页:reader 怎么定位 + 读

读一页 P 的全流程,在 pager 层是 `readDbPage`(pager.c#L3067-L3091):

```c
static int readDbPage(PgHdr *pPg){
  Pager *pPager = pPg->pPager;
  ...
  if( pagerUseWal(pPager) ){
    rc = sqlite3WalFindFrame(pPager->pWal, pPg->pgno, &iFrame);  // 查 wal-index
    if( rc ) return rc;
  }
  if( iFrame ){
    rc = sqlite3WalReadFrame(pPager->pWal, iFrame, ...);  // 从 WAL 读这帧
  }else{
    i64 iOffset = (pPg->pgno-1)*(i64)pPager->pageSize;
    rc = sqlite3OsRead(pPager->fd, ..., pPager->pageSize, iOffset);  // 从主库读
  }
}
```

三步:

1. **查 wal-index**(`sqlite3WalFindFrame`,wal.c#L3640,内部调 `walFindFrame`,wal.c#L3525):"页 P 在我这个 reader 的快照范围内,WAL 里有它的帧吗?在第几帧?"返回 `iFrame`(0 表示没有)。
2. **有 → 从 WAL 读**(`sqlite3WalReadFrame`,wal.c#L3658):直接定位读。
3. **没有 → 从主库读**:页 P 在 WAL 里没改过,意味着它的最新版本还在主库里(主库没被 writer 动过),直接读主库。

这里 reader 只读主库或 WAL 二选一,从不写。多个 reader 可以同时读同一份 WAL + 主库,互不挡。writer 这时可能在追加新 frame,但那些新帧的帧号 > reader 的 mxFrame,reader 在 `walFindFrame` 里用 `iFrame<=iLast` 过滤掉(wal.c#L3591),看不到。

> **wal-index 是 reader 的命门,也是 WAL 性能的命门**。这一节的细节(哈希表怎么用、为什么装填因子 0.5)我们放第六节"技巧精解"专门拆透,这里先记住结论:reader 找一页平均常数次内存访问,不扫整个 WAL。

---

## 五、为什么 WAL 仍是单写者

WAL 既然这么强(读不阻塞写),那它是不是也允许多个 writer 并发写?**不。** SQLite 的 WAL 模式,**同一时刻仍然只允许一个 writer**(`sqlite3WalBeginWriteTransaction` 必须 `walLockExclusive(WAL_WRITE_LOCK)` 独占,wal.c#L3724)。为什么?

> **不这样会怎样(假设允许多 writer 同时写 WAL)**:多 writer 同时往 WAL 文件**末尾追加** frame,有两个会立刻爆炸的问题:

1. **WAL 文件的末尾位置会乱**:writer A 和 writer B 都算出"下一帧该写到 offset X",都往 X 写,A 的帧覆盖 B 的帧,数据损坏。要解决就得给"末尾位置"加锁,但加锁后两个 writer 还是串行的,跟单写者没本质区别,反而多了协调开销。
2. **链式校验和会断**:第 N 帧的校验和依赖前 N-1 帧的全部内容。如果 A 和 B 交错写,A 写第 5 帧时算的校验和包含了 B 还没写完的第 4 帧——这帧一旦不完整,A 的校验和就是错的。crash 恢复时整条校验和链从第 4 帧开始全断。
3. **wal-index 的更新会乱**:两个 writer 同时往索引追加条目,谁的 aPgno 在前、aHash 谁先插,无法协调。

> **所以这样设计**:SQLite 干脆**单写者**——整个数据库同一时刻只准一个 writer 持 WRITE_LOCK。这个 writer 独占 WAL 的追加端、独占 wal-index 的写入端,事情就简单了:**WAL 是 append-only 的、wal-index 的更新是单线程的**,校验和链、页号顺序、哈希表插入都不会乱。代价是写并发 = 1,但换来了:
> - 实现极简(不用复杂的并发控制);
> - 写事务之间天然串行,不存在"两个 writer 互相改同一页"的冲突;
> - 嵌入式场景(单 App 内的写本来就少)这个代价可以接受。

这和 InnoDB 的多 writer(靠 undo + MVCC + 行锁支持高并发写)是不同的取舍——SQLite 是"**牺牲写并发,换读并发 + 实现简单**"。对一个嵌入式数据库,这个取舍划算。承《MySQL·InnoDB》:InnoDB 的多版本并发靠 undo 版本链 + 事务 ID + ReadView,复杂得多;SQLite 无 undo,reader 的"多版本"直接就是 WAL 里的同一页的不同帧,read-mark 做截断就完了。

> **钉死这件事**:WAL 解决的是"读写并发"(多 reader + 一个 writer 互不挡),不是"写写并发"。SQLite 永远是单写者——这是它和 InnoDB(多 writer)的根本差异之一,也是为什么 SQLite 不适合"高并发写"场景(那种场景该用 MySQL/PG)。

---

## 六、技巧精解:两个最硬核的技巧

这一节挑两个最硬核的技巧单独拆透:**(A) wal-index 哈希表让 reader O(1) 定位页版本**;**(B) read-mark + 链式校验和让"读不阻塞写、写不阻塞读、crash 不丢"三件事同时成立**。

### 技巧 A:wal-index 的"页号数组 + 开放寻址哈希表"双结构

reader 要回答的问题:"页 P 在 WAL 里(我的快照范围内)最新一帧是第几帧?"wal-index 用了一个**双结构**的精妙设计来回答它。

#### 双结构长什么样

每个 32KB 索引块里,有两段紧挨着的内存:

```
   一个 32KB 索引块(WALINDEX_PGSZ = 4096*4 + 8192*2 = 32768 字节):
   ┌──────────────────────────────────────────────────────┐
   │ aPgno[4096] (u32 数组, 16384 字节)                     │  ← 页号数组
   │   aPgno[k] = 这个块第 (k+1) 帧对应的数据库页号         │     (一帧一条,
   │   (第一块只有 aPgno[0..4061], 前 136B 被头部占)         │      按帧顺序排)
   ├──────────────────────────────────────────────────────┤
   │ aHash[8192] (u16 数组, ht_slot, 16384 字节)            │  ← 哈希表
   │   aHash[i] = 一个 aPgno 的 1-based 下标(0=空)         │     (开放寻址)
   └──────────────────────────────────────────────────────┘
   块 i 索引的帧范围:[iZero+1, iZero+4096](或第一块 [1, 4062])。
```

#### writer 怎么写(`walIndexAppend`,wal.c#L1301-L1345)

writer 追加一个 frame(页号 iPage、帧号 iFrame)时:

```c
static int walIndexAppend(Wal *pWal, u32 iFrame, u32 iPage){
  ...
  rc = walHashGet(pWal, walFramePage(iFrame), &sLoc);  // 定位到对应索引块
  if( rc==SQLITE_OK ){
    int iKey;
    int idx = iFrame - sLoc.iZero;          // 本帧在本块的 1-based 位置
    ...
    sLoc.aPgno[(idx-1)&(HASHTABLE_NPAGE-1)] = iPage;   // ① 写页号数组
    for(iKey=walHash(iPage); sLoc.aHash[iKey]; iKey=walNextHash(iKey)){
      ...                                   // ② 沿哈希冲突链找空 slot(开放寻址)
    }
    AtomicStore(&sLoc.aHash[iKey], (ht_slot)idx);       // ③ slot 存"在 aPgno 的位置"
  }
}
```

哈希函数(wal.c#L1138-L1144)简洁到极致:

```c
static int walHash(u32 iPage){
  return (iPage*HASHTABLE_HASH_1) & (HASHTABLE_NSLOT-1);   // (P * 383) & 8191
}
static int walNextHash(int iPriorHash){
  return (iPriorHash+1)&(HASHTABLE_NSLOT-1);               // 冲突就下一个
}
```

`383` 是个素数(`HASHTABLE_HASH_1`,wal.c#L616),做乘法散列;`8191` 是 mask(`HASHTABLE_NSLOT=8192` 是 2 的幂)。开放寻址(线性探测 +1)。

#### reader 怎么查(`walFindFrame`,wal.c#L3574-L3605)

```c
  for(iHash=walFramePage(iLast); iHash>=iMinHash; iHash--){   // 从最后一块往前
    rc = walHashGet(pWal, iHash, &sLoc);
    nCollide = HASHTABLE_NSLOT;
    iKey = walHash(pgno);                          // 哈希页号
    while( (iH = AtomicLoad(&sLoc.aHash[iKey]))!=0 ){   // 沿冲突链走
      u32 iFrame = iH + sLoc.iZero;
      if( iFrame<=iLast                            // ① 在我快照范围内
       && iFrame>=pWal->minFrame
       && sLoc.aPgno[(iH-1)&(HASHTABLE_NPAGE-1)]==pgno  // ② 页号真是 P(过滤冲突)
      ){
        iRead = iFrame;                            // 找到了, 记下
      }
      if( (nCollide--)==0 ) return SQLITE_CORRUPT_BKPT;   // 防死循环
      iKey = walNextHash(iKey);                    // 下一 slot
    }
    if( iRead ) break;                             // 这块找到了, 不再往前
  }
```

注意三个细节,每一个都不是随手写的:

- **从最后一块往前找**(`iHash--`):同一页可能在多个块里都有(改了好几次),最新版本帧号最大,所以**从后往前、第一块里找到的就是最新**。这避免了"找到所有版本再取最大"。
- **`iFrame<=iLast`**:reader 只认自己快照范围(`iLast = pWal->hdr.mxFrame` = read-mark)内的帧。writer 这时可能已经追加了更靠后的新帧(它的 slot 也在 aHash 里),但帧号 > iLast,被过滤掉——这就是**读不阻塞写**(reader 读老快照、writer 加新帧,索引区域共享但 reader 用条件过滤隔离)。
- **`AtomicLoad`**:读 aHash 用原子读,因为 writer 可能正在并发写这个 slot。32 位/16 位原子读不会撕裂,reader 拿到的要么是旧值(0 或老 idx)要么是新值,都是合法的。

#### 反面对比:没有 wal-index 会怎样

如果不用这个双结构,reader 要找页 P 的最新帧,只能**从 WAL 最后一帧往前线性扫**,每帧读 24 字节帧头看页号。WAL 几 MB 时(几千帧),一次读可能要扫几千次磁盘/页缓存 IO。更糟的是 reader 每读一页都要扫——一个 SELECT 读 1000 页,就是 100 万次扫描。**WAL 直接没法用。**

有了 wal-index,reader 平均 O(1) 定位(哈希一次 + 常数次探测)。这是 WAL 从"理论上可行"变成"工程上可用"的关键工程。代码注释里 SQLite 作者把这段动机写得清清楚楚(wal.c#L119-L125)。

> **钉死这件事**:wal-index 的双结构(页号数组 + 装填因子 0.5 的开放寻址哈希表)是个小而精的设计——页号数组按帧顺序排(给 checkpoint 用,见技巧 B 后半),哈希表给 reader 快速查。两者共用一块 32KB 共享内存,reader/writer 各自只碰自己那一侧(reader 查、writer 追加),通过 `AtomicLoad`/`AtomicStore` + reader 的 `iFrame<=iLast` 过滤做到无锁并发。这是 WAL"读不阻塞写"在索引层面的实现。

### 技巧 B:read-mark + 链式校验和,三件事同时成立

WAL 模式要同时保证三件互相牵扯的事:**① 读不阻塞写、② 写不阻塞读、③ crash 不丢**。这听起来矛盾——要让 reader 和 writer 并发读写同一份 WAL,又要 crash 后能正确恢复。SQLite 用两个精巧手段把它们同时钉死。

#### 手段 1:read-mark 实现"读写不互相挡"

前面第二节讲了 read-mark 的作用,这里拆它的精妙处。每个 reader 在 `aReadMark[1..4]` 里占一个槽位(持 `WAL_READ_LOCK(i)` 的 SHARED 锁),槽位值就是它的快照上限 mxFrame。

**为什么这套能"读不阻塞写"**:writer 拿的是 `WAL_WRITE_LOCK`(锁字节 0)的 EXCLUSIVE,reader 拿的是 `WAL_READ_LOCK(i)`(锁字节 3+i)的 SHARED。**这是两把不同的锁**,互不冲突——writer 拿写锁不需要等 reader 放读锁,reader 拿读锁不需要等 writer 放写锁。锁定义见 wal.c#L294-L299:

```c
#define WAL_WRITE_LOCK         0       // writer 排他
#define WAL_ALL_BUT_WRITE      1
#define WAL_CKPT_LOCK          1       // checkpoint 排他
#define WAL_RECOVER_LOCK       2       // 恢复排他
#define WAL_READ_LOCK(I)       (3+(I)) // reader 共享(I=0..4)
#define WAL_NREADER            (SQLITE_SHM_NLOCK-3)  // =5(SQLITE_SHM_NLOCK=8)
```

**为什么 checkpoint 会被老 reader 挡**:checkpoint 拿 `WAL_CKPT_LOCK`(锁字节 1)的 EXCLUSIVE。但更重要的是,它在回填前要算 `mxSafeFrame = min(在用 aReadMark)`(wal.c#L2228),不能回填超过任何老 reader 快照的帧。这保证老 reader 之后还能从 WAL 读到它快照范围内的版本(checkpoint 没覆盖掉)。

> **不这样会怎样**:如果 checkpoint 不管 reader 的 read-mark、直接把 WAL 全回填 + 重启,那么一个还在用老快照的 reader,下次读那些页时会从主库读——可主库已经被 checkpoint 改成最新版本了,reader 读到它不该看的新数据,快照隔离被破坏。**read-mark 的存在,就是让 checkpoint 知道"我最多能回填到哪,再往后有老 reader 在用"。** 这是"读不阻塞写"和"快照一致性"能同时成立的协调机制。

#### 手段 2:链式校验和 + salt 实现"crash 不丢"

WAL 的每个 frame 都带校验和,而且校验和是**链式**的——第 N 帧的校验和,把"WAL header + 前 N-1 帧 + 第 N 帧前 8 字节"全算进去(wal.c#L83-L87 注释、`walEncodeFrame` wal.c#L985-L986):

```c
  walChecksumBytes(nativeCksum, aFrame, 8, aCksum, aCksum);     // 前 8 字节
  walChecksumBytes(nativeCksum, aData, pWal->szPage, aCksum, aCksum);  // + 页数据
```

校验和算法本身也巧妙(wal.c#L79-L87):用 fibonacci 反序加权——`s0 += x[i] + s1; s1 += x[i+1] + s0;`。这种加权方式让**任何一个字节的改动都会影响后续所有项的校验和**,且对"插入/删除"敏感,防止 WAL 中间被篡改或错位。

**这套链式校验和 + salt 怎么实现 crash 不丢**:

1. **crash 时 WAL 可能写到一半**(最后一个 frame 不完整)。crash 恢复时,reader 从 WAL 第一帧开始,逐帧验校验和(`walDecodeFrame`,wal.c#L1000-L1053):salt 对不对、校验和对不对。第一个验不过的帧,**它和它之后的所有帧全部丢弃**(认为是 crash 时没写完的)。
2. **WAL 重启(checkpoint 后)会换 salt**:每次 WAL restart,`salt-1` 递增、`salt-2` 重新随机(wal.c#L95-L98 注释、`walRestartLog` wal.c#L3875-L3915)。新 salt 写进 WAL header。这样**老 frame(老 salt)和新 frame(新 salt)绝不会混在一起被当成有效**——一个 frame 的 salt 必须和当前 WAL header 的 salt 一致才算数。crash 后即使磁盘上残留老 frame,也不会被误认。
3. **commit frame 标记事务边界**:只有带 commit frame(`nTruncate>0`)的事务才算提交。crash 恢复时,reader 找到最后一个有效 commit frame,它之前的全部保留、之后的全丢。一个事务如果 crash 在写 commit frame 之前,整个事务的帧会被丢弃(虽然它们校验和可能都对,但后面没 commit frame)。

> **钉死这件事**:WAL 的"读不阻塞写、写不阻塞读、crash 不丢"三件事,是靠**read-mark(读写锁分离 + 快照截断)+ 链式校验和(检测损坏帧)+ salt(防老帧混入)+ commit frame(标事务边界)**四个机制协同实现的。每一个机制单独看都不复杂,组合起来却精确地解决了并发 + 持久化的难题。这是 SQLite 工程美学的典范——**用最简单的机制,覆盖最棘手的正确性问题**。

---

## 七、checkpoint 四模式:WAL 攒大了怎么办

writer 一直往 WAL 追加,WAL 会越来越大。读虽然快(有 wal-index),但 WAL 越大,reader 开始读时要重建/校验的索引越多,且老版本 frame 堆积占空间。所以 WAL 要定期 **checkpoint**——把 WAL 里已提交的 frame **搬回主库**,腾出 WAL 空间。

checkpoint 有四种模式(`SQLITE_CHECKPOINT_*`,定义见 sqlite.h.in#L10126-L10160):

| 模式 | 做什么 | 拿什么锁 | 阻不阻塞 reader | 阻不阻塞 writer | 典型用途 |
|------|--------|---------|----------------|----------------|---------|
| **PASSIVE**(默认,自动 checkpoint 用它) | 尽量回填:把 `min(在用 read-mark)` 以内的已提交 frame 搬回主库,搬不动(有 reader 占着)就停,搬多少算多少 | CKPT 锁(排他) | **不阻塞** reader | **不阻塞** writer(不拿 WRITE_LOCK) | 自动 checkpoint(`sqlite3_wal_autocheckpoint`,默认每 1000 帧触发一次,见下) |
| **FULL** | 先像 PASSIVE 一样回填;若没回填完,**调用 busy handler 等所有 reader 退出**,然后拿 WRITE_LOCK 排他,把剩余 frame 全部回填完,再 fsync 主库 | CKPT 锁 + WRITE 锁(排他) | 不阻塞新 reader,但要等老 reader 退出 | **阻塞** writer(持 WRITE_LOCK 期间) | 手动 `sqlite3_wal_checkpoint_v2(db, 0, FULL, ...)`,要把 WAL 全清又不太急 |
| **RESTART** | 在 FULL 基础上,回填完后**还要等所有 reader 退出**(busy handler),然后**重启 WAL**(换 salt、mxFrame 归零),让下一个 writer 从 WAL 开头写 | CKPT 锁 + WRITE 锁 + 所有 READ_LOCK(1..4) 排他 | **阻塞**持老快照的 reader(等它们退出) | 阻塞 writer | 想让 WAL 文件物理缩小到开头、下一个事务重头写 |
| **TRUNCATE** | 在 RESTART 基础上,**把 WAL 文件物理截断到 0 字节**(`sqlite3OsTruncate(pWalFd, 0)`,wal.c#L2384) | 同 RESTART | 同 RESTART | 阻塞 writer | 想彻底回收 WAL 磁盘空间(最彻底,但最重) |
| **NOOP** | 什么都不回填,只为了取 `pnLog`/`pnCkpt` 两个统计值 | 不拿锁 | 不阻塞 | 不阻塞 | 只想查 WAL 状态 |

(锁行为综合自 wal.c#L2353-L2389 和 sqlite.h.in#L10126-L10160。)

#### 几个关键点

1. **PASSIVE 是唯一不阻塞任何人的模式**:它只拿 CKPT 锁(防别的 checkpoint 并发)、不拿 WRITE_LOCK、不等 reader。回填到 `mxSafeFrame = min(在用 read-mark)` 就停,没回填完也不强求。**默认的自动 checkpoint 用的就是 PASSIVE**——这就是为什么"后台自动 checkpoint"你几乎感觉不到(它不挡你读写)。

2. **自动 checkpoint 的触发**:`main.c#L3655` 默认 `sqlite3_wal_autocheckpoint(db, SQLITE_DEFAULT_WAL_AUTOCHECKPOINT)`,`SQLITE_DEFAULT_WAL_AUTOCHECKPOINT = 1000`(sqliteLimit.h#L168-L169)。意思是:**每次事务提交后,如果 WAL 帧数 ≥ 1000,就自动跑一次 PASSIVE checkpoint。** 这个阈值可调(`PRAGMA wal_autocheckpoint`)。设成 0 关掉自动 checkpoint,完全手动控制。

3. **FULL/RESTART/TRUNCATE 的递进关系**:每一档都比前一档多做一步——
   - FULL = PASSIVE 回填 + 等老 reader 退出 + 回填剩余;
   - RESTART = FULL + 等 reader 退出 + 重启 WAL(换 salt);
   - TRUNCATE = RESTART + 物理截断 WAL 文件到 0。
   越往后越彻底(WAL 越小),但也越重(要等更多人、要拿更多锁)。代码里 `eMode>=SQLITE_CHECKPOINT_RESTART` 的分支(wal.c#L2363-L2388)处理 RESTART/TRUNCATE 的"重启 WAL"部分,`eMode==SQLITE_CHECKPOINT_TRUNCATE` 再多一步 `sqlite3OsTruncate`(wal.c#L2384)。

4. **checkpoint 的写顺序保证 crash 不丢**:`walCheckpoint` 先 fsync WAL、再把 frame 内容写进主库、再 fsync 主库(wal.c#L89-L93 注释,代码在 wal.c#L2290 起)。两个 fsync 当写屏障——WAL 的内容一定先于主库的改动落盘。这样即使 checkpoint 写主库写到一半 crash,主库里那些被改了一半的页,可以从 WAL(已 fsync、完整)重新 replay 恢复。承《MySQL·InnoDB》:这和 InnoDB 的"redo 先于数据页落盘"(双 fsync 顺序)是同一条 ACID 原则。

> **钉死这件事**:checkpoint 四模式不是平级的选项,而是**从严到松的一组递进**——从"尽量回填不挡人"(PASSIVE)到"彻底清空 WAL 截断文件"(TRUNCATE)。默认的 PASSIVE + 自动阈值 1000 帧,对绝大多数应用是最优解:你啥都不用管,WAL 不会无限涨,读写都不被挡。需要更激进控制(比如定期收缩 WAL 文件)时,才手动跑 RESTART/TRUNCATE。

---

## 八、WAL vs rollback journal:一张总对照表

把 WAL 和上一章的 rollback journal 放一起对照,两者的取舍一目了然:

| 维度 | **rollback journal**(默认,P4-12) | **WAL**(P4-13) |
|------|-----------------------------------|-----------------|
| **记什么** | 改页**之前**记页的**旧值**(用于回滚) | 改页时记页的**新值**(用于读 + 重做) |
| **方向** | undo 方向(crash 后把页改回旧值) | redo 方向(crash 后用新值重做) |
| **改时碰主库吗** | **碰**——journal 写旧值后,改主库 | **不碰**——只追加 WAL,主库写时不动 |
| **写时锁** | RESERVED→PENDING→**EXCLUSIVE**,写时**独占**整个 db | 只持 **WRITE_LOCK**(单写者),不独占读 |
| **读时锁** | SHARED,但写事务持 EXCLUSIVE 时**拿不到**、排队等 | SHARED(read-mark),**和写锁不冲突**,随时拿 |
| **并发** | 读写互斥(写时读全堵) | **读不阻塞写、写不阻塞读**(单写者 + 多 reader) |
| **COMMIT 的物理动作** | fsync 主库(写时已改主库) | fsync WAL + 更新 wal-index(不碰主库) |
| **小事务提交速度** | 慢(要 fsync 主库) | **快**(只 fsync WAL,顺序写) |
| **crash 恢复** | 用 journal 把页改回旧值(回滚未提交事务) | 重放 WAL 到最后一个 commit frame(重做已提交事务) |
| **文件数** | db + journal(临时,提交后删/清) | db + **db-wal**(持续) + **db-shm**(共享内存,临时可重建) |
| **网络盘(NFS/SMB)** | **支持**(只用文件锁) | **不支持**(要共享内存,网络盘不能可靠共享) |
| **只读访问** | 要拿 SHARED,和写互斥 | reader 可直接读,不挡写 |
| **WAL/自动 checkpoint** | 无 | 有(默认 PASSIVE,每 1000 帧自动) |
| **适用场景** | 写少、简单、要网络盘、老兼容 | **读多写少、要并发读、本地盘**(现代首选) |

**怎么选**:

- **绝大多数现代应用**:用 **WAL**(本地 App、Web 后端读多写少)。`PRAGMA journal_mode=WAL;` 一行搞定,读写并发立即起来。
- **必须用网络盘**:只能用 rollback journal(WAL 要共享内存,网络盘不行)。
- **写极多、不在乎读被挡**:rollback journal 也行(但 SQLite 单写者,写多本来就不是它的强项,那种场景该上 MySQL/PG)。

> **承《MySQL·InnoDB》**:这张表里的"方向"那一行最值得钉死——rollback journal 是 **undo**(记旧值,回滚),WAL 是 **redo**(记新值,重做)。InnoDB 同时有 redo(重做已提交)和 undo(回滚未提交 + MVCC),两者都要;SQLite 嵌入式简化:**WAL 模式下只 redo、无 undo**(回滚靠"事务没 commit frame 就丢弃其帧",MVCC 靠 WAL 多版本 + read-mark)。这就是为什么 SQLite 的并发模型比 InnoDB 简单那么多——它用"WAL 多版本"一招,同时当了 redo 和 MVCC 两份用。代价是单写者(无 undo 就没法多 writer 协调)。

---

## 九、章末小结

### 回扣主线

本章服务全书二分法的**"存储与事务"**这一面——具体是其中的"事务与并发"环节。WAL 是 SQLite 在"保证 ACID"这条线上,**为了解决 rollback journal 的写时独占短板**而引入的第二套提交协议。它和 rollback journal、pager、B-tree 一起,构成 SQLite 的存储与事务全景:

```
   一条 UPDATE 在 WAL 模式下的旅程:
   VDBE 执行 opcode → B-tree 改页(页在 pager 缓存里变脏)
     → 事务提交: pagerWalFrames → sqlite3WalFrames → walEncodeFrame
     → 脏页编码成 frame 追加到 .db-wal + walIndexAppend 更新 shm 索引
     → commit frame 写完 + fsync WAL + 更新 wal-index mxFrame  (COMMIT 完成, 主库没动)
     → 后台: WAL 攒到 1000 帧, 自动 PASSIVE checkpoint 把 frame 搬回主库
   一条 SELECT 在 WAL 模式下的旅程:
   reader 开始读事务 → walTryBeginRead 拿 read-mark(快照 mxFrame)
     → 读一页: readDbPage → sqlite3WalFindFrame(查 shm 哈希表)
       → 帧在 WAL? 从 WAL 读 : 从主库读
     → 读完不受 writer 影响(writer 追加的新帧 > 我的 mxFrame, 我看不见)
```

WAL 把"改前记日志"(rollback journal)换成了"改时记日志"(WAL),把"写时独占"换成了"单写者 + 多 reader 快照",把"COMMIT fsync 主库"换成了"COMMIT fsync WAL(顺序写更快)"。这是 SQLite 在并发性上最重要的一次进化(3.7+,2010)。

### 五个为什么

1. **为什么 WAL 能读不阻塞写、写不阻塞读?**——writer 只追加 WAL、不碰主库;reader 各持一份 read-mark 快照(老的 mxFrame),writer 追加的新帧帧号 > reader 的 mxFrame,被 `iFrame<=iLast` 过滤;writer 持 WRITE_LOCK、reader 持 READ_LOCK,是两把不同的锁,互不挡。
2. **为什么 reader 能 O(1) 定位每页最新版本,不用扫整个 WAL?**——wal-index(共享内存 shm)里维护"页号→帧号"的哈希表(装填因子 0.5,开放寻址),reader 一次哈希 + 常数次探测就拿到帧号,代码见 `walFindFrame`(wal.c#L3525)。
3. **为什么 WAL 仍是单写者?**——WAL 是 append-only,多 writer 同时追加会让"末尾位置"和"链式校验和"都乱(a 帧覆盖 b 帧、校验和链断裂、wal-index 插入乱序);单写者换来了实现极简 + 写事务天然串行。这是"牺牲写并发换读并发 + 简单"的取舍。
4. **为什么 checkpoint 不能覆盖"还有 reader 在用"的旧 frame?**——那个 reader 持老快照(read-mark 指向 WAL 中段),如果 checkpoint 把它快照范围内的 frame 搬回主库 + WAL 重启,reader 再读这些页会从主库读到被 checkpoint 改成的新版本,快照隔离被破坏。所以 checkpoint 算 `mxSafeFrame = min(在用 read-mark)`,只回填到这为止。
5. **为什么 SQLite WAL 比 InnoDB redo 简单那么多?**——SQLite 嵌入式、无 undo,WAL 一招兼当 redo(重做已提交)+ MVCC(多版本快照读);InnoDB 是 C/S、多 writer,要 undo(回滚未提交 + MVCC 版本链)+ redo(重做)+ 行锁,复杂得多。SQLite 用单写者 + WAL 多版本,把并发模型极大简化了。

### 想继续深入往哪钻

- **源码**:
  - `src/wal.c` 是本章主角(4645 行),文件头注释(wal.c#L1-L260)把 WAL/wal-index 格式、reader 算法、checkpoint 协议讲得极其清楚,是 SQLite 官方最好的 WAL 文档。
  - 关键函数:`walEncodeFrame`/`walDecodeFrame`(帧编解码,wal.c#L969/L1000)、`walIndexAppend`/`walFindFrame`(索引读写,wal.c#L1301/L3525)、`walCheckpoint`(回填,wal.c#L2199)、`walTryBeginRead`(reader 取快照,wal.c#L3110 起)、`sqlite3WalBeginWriteTransaction`(writer 取锁,wal.c#L3699)、`walRestartLog`(WAL 重启,wal.c#L3875)。
  - `src/pager.c`:pager 怎么接 WAL——`pagerWalFrames`(写路径,pager.c#L3225)、`readDbPage`(读路径,pager.c#L3067)、`sqlite3PagerOpenWal`(开 WAL 模式,pager.c#L7677)。
- **官方文档**:SQLite 官方 "Write-Ahead Logging"(https://www.sqlite.org/wal.html)讲并发模型和 checkpoint 模式,和本章对照读;"Atomic Commit In SQLite" 讲 WAL/rollback journal 的提交协议。
- **动手感受**:`sqlite3 test.db` → `PRAGMA journal_mode=WAL;` → 一边开事务写、一边另一个连接读,验证读不被挡;`PRAGMA wal_checkpoint;` 看回填了多少帧;`ls -la` 看 `.db-wal`/`.db-shm` 文件大小变化。

### 引出下一章

我们搞清楚了 WAL 怎么让读写并发,以及它的帧格式、wal-index、checkpoint 四模式。但还差最后一块拼图:**如果系统 crash 了(WAL 写到一半、或 checkpoint 写主库写到一半),重启时 SQLite 怎么把数据库恢复到一致状态?** WAL 模式和 rollback journal 模式的 recovery 协议各自是什么?这一切怎么总结成 ACID 的完整保证?下一章 P4-14,我们从 **crash recovery 与 ACID 总结** 收口第 4 篇,把"存储与事务"这一面彻底钉死。

> **下一章**:[P4-14 · crash recovery 与 ACID](P4-14-crash-recovery与ACID.md)

---

> **承接指路**:本章在"存储与事务"线上承接《MySQL·InnoDB》的 **redo log** 章节(同是"改前先记日志"思想,但 SQLite WAL 记新值用于读+重做、InnoDB redo 记物理逻辑操作用于重做,SQLite 无 undo 靠 WAL 版本快照做 MVCC)。本章对照讲、不重复 InnoDB redo/undo 细节。回滚对照见上一章 P4-12 rollback journal。
