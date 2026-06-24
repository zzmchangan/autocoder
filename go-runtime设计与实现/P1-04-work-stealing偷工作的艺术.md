# 第四章 · work-stealing:偷工作的艺术

> 篇:第 1 篇 · GMP 调度器(全书地基,重头戏)
> 主线呼应:上一章我们把 `findRunnable` 的多级回退拆透了。回退序列走到第 6 级,空闲 P 会上演调度器里最精彩的一幕——**偷**。一个 P 手头的本地 runq 跑空了,它不躺平,而是主动溜到别的 P 的 runq 尾巴上,把对方**一半**的 G 扛回来自己跑。这一章我们钻进 [`runqsteal`](../go/src/runtime/proc.go#L7781)/[`runqgrab`](../go/src/runtime/proc.go#L7713),拆透两件事:**一是无锁环形 runq 凭什么能让两个 P 一个从 head 消费、一个从 tail 偷而不踩脚;二是为什么偷一半而不是偷一个**。这两个问题答清了,Go 调度器的负载均衡就基本盘住了。

## 核心问题

**空闲的 P 怎么从别的 P 偷一半 runq?无锁 runq(环形数组 + head/tail 原子 CAS)凭什么让两个 P 并发读写而不丢 G、不重复跑?为什么偷一半而不是偷一个?**

读完本章你会明白:

1. work-stealing 解决的本质问题:多 P 各有本地 runq,负载天生不均(有的 P 堆积,有的 P 空转),需要一个低开销的机制把堆积的活搬到空 P——靠谁搬、搬多少、用什么搬。
2. 一个本地 runq 怎么用**环形数组 + 两个原子游标(head/tail)+ 一个 runnext 插队槽**做到"owner 端 push/pop、thief 端偷一半"全程无锁,以及这套协作在内存序上凭什么 sound(不丢 G、不重复跑、不读到撕裂值)。
3. 为什么**偷一半**而非偷一个:偷一个会让"P 空了就偷一次"高频发生,而偷一半把"找活成本"摊到一堆 G 上,还顺带把负载往中间拉平。
4. `stealWork` 的"4 轮遍历 + 最后一轮才偷 runnext/timer"为什么这么排,以及那个看似奇怪的 `usleep(3)` 退让是什么意思。
5. Go 的 work-stealing 和 Tokio 的 work-stealing 是同一个思想的两套实现(Tokio 用 Chase-Lev 双端队列,Go 用 256 槽固定环形数组),取舍不同。

> 逃生阀:work-stealing 真正难的是**无锁并发正确性**(两个 P 同时改 head/tail)。如果你读到原子内存序那段觉得绕,先抓主干:**owner 一个人写 tail、thief 和 owner 都改 head,改之前都先 acquire 读、改完都 release 写**。这个"谁写哪个游标、配什么内存序"的对照表(见 4.4)是本章的钥匙,其余都是它的展开。

---

## 4.1 一句话点破

> **work-stealing 不是"我空了去拿别人的一个 G",而是"我空了去偷别人的一半"——偷一半而非偷一个,是为了把"找活"这个有代价的操作(读 head/tail、CAS、可能失败重试)摊到一批 G 上;而能放心地让两个 P 同时在一个环形数组上动手脚,靠的是 owner 独占 tail、thief 和 owner 共享 head、每个游标用 acquire 读 / release 写这套严格的内存序约定。**

这是结论,不是理由。本章倒过来拆:先看为什么需要偷(负载不均的物理根源),再看 runq 的数据结构为什么长成环形数组这样,然后拆 owner 端 push/pop 和 thief 端 grab 的并发协议凭什么 sound,最后讲为什么偷一半并和 Tokio 对照。

---

## 4.2 为什么需要偷:本地 runq 的代价是负载不均

第 2 章我们立起 P 的本地 runq 时,讲过它的好处:**owner P 独占 push/pop,无锁,缓存友好**。`go func` 创建的新 G 直接塞进当前 P 的本地 runq([`runqput`](../go/src/runtime/proc.go#L7529)),不碰全局锁。这是 Go 调度器能在 64 核机器上不让调度锁成为瓶颈的根本。

但本地 runq 有一个与生俱来的代价:**负载不均**。

设想一个 Web 服务器,8 个 P。请求到来时由 epoll 唤醒,被 netpoll 唤醒的 G 进入"被 netpoll 抢到的那个 P"的本地 runq。如果某一瞬间一大批请求恰好都落到少数几个 socket 上,而那几个 socket 又被同一个 P 的 netpoll 处理了——那这个 P 的本地 runq 会瞬间堆积几十上百个 G,而别的 P 的本地 runq 是空的。更普遍的是:**每个 P 上 G 的执行时长不同**(有的 G 跑微秒级,有的跑毫秒级),跑得快的 P 频繁空 runq,跑得慢的 P 堆积。

> **不这样会怎样**:如果不解决负载不均,会出现两类病态:(1)有的 P 空转(本地空、全局也空),CPU 闲置;(2)有的 P 堆积,G 排队等很久才轮到,延迟尖峰。一个"快 P"和一个"慢 P"同时存在,等于浪费了一半算力。理想状态是**所有 P 的 runq 长度大致相等**——这就是 work-stealing 要达到的目标。

work-stealing 的核心思想很朴素:**一个 P 本地 runq 空了,它主动去别的 P 那里搬一些活回来**。这和"中央调度器统一分配"形成对比:

| 策略 | 谁来搬活 | 代价 |
|---|---|---|
| **中央 push**(传统线程池) | 一个中央调度器看哪个队列空就往哪推 | 中央调度器是瓶颈,要全局锁 |
| **分布式 steal**(Go/Tokio) | 每个 P 自己空了就去偷 | 无中央瓶颈,但要无锁数据结构支撑 |

Go 选了分布式 steal。这就引出本章的两个核心问题:**数据结构怎么设计才能无锁偷?偷多少才划算?**

---

## 4.3 数据结构:256 槽环形数组 + 两个原子游标

先看 P 的 runq 长什么样([`runtime2.go`](../go/src/runtime/runtime2.go#L804-L820)):

```go
// src/runtime/runtime2.go#L804-L820(节选)
// Queue of runnable goroutines. Accessed without lock.
runqhead uint32
runqtail uint32
runq     [256]guintptr
// runnext, if non-nil, is a runnable G that was ready'd by
// the current G and should be run next ...
runnext guintptr
```

注释一句"Accessed without lock"是关键——这个队列**故意不加锁**,靠原子操作和访问规约保证 sound。结构极简:

- **`runq [256]guintptr`**:固定 256 槽的环形数组,每槽放一个 G 指针(`guintptr` 是绕过写屏障的指针类型,第 2 章讲过)。256 是编译期常量。
- **`runqhead uint32`**:消费者游标,指向下一个要取出的槽(从 head 端 pop)。
- **`runqtail uint32`**:生产者游标,指向下一个要写入的槽(从 tail 端 push)。
- **`runnext guintptr`**:插队槽,放"下一个优先跑"的 G(通常是被当前 G `ready` 唤醒的,inheritTime)。

用 ASCII 画出它的形状:

```
 一个 P 的本地 runq(无锁环形数组,256 槽,示意取 12 槽):

       head 端(owner pop / thief grab 都从这里推进)
        ↓
  ┌──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬──┐
  │g5│g6│g7│g8│  │  │  │  │  │  │g2│g3│g4│   ← tail 端(owner push)
  └──┴──┴──┴──┴──┴──┴──┴──┴──┴──┴──┴──┘
                        ↑               ↑
                     已消费区        待消费区[head, tail)
                   (runqhead 之前)  runqhead ≤ runqtail

  owner:  push 写 runq[tail%256], StoreRel(runqtail, tail+1)
          pop  读 runq[head%256],  CasRel(runqhead, head+1)
  thief:  grab 读 runq[head..head+n/2], CasRel(runqhead, head+n/2)
  (runnext 是独立的插队槽,不和 runq 数组混算)
```

为什么是这个形状?四个设计选择,每个都有 why:

### 4.3.1 为什么是环形数组,不是链表

链表(每个 G 带个 next 指针)看起来更灵活,为什么用固定 256 的数组?

> **不这样会怎样**:链表的两个坑:(1)**每入队一个 G 要分配一个节点**(或者复用 G 自身的 `schedlink` 字段),要么分配开销大,要么污染 G 的字段;(2)**链表节点散落在堆各处,缓存不友好**——偷一半时要逐节点跳指针,每跳一次可能一次 cache miss。

数组的好处:(1)零分配,256 槽在 P 结构里固定占位;(2)**连续内存,缓存友好**——偷一半时连续读 128 个槽,预取器会把后面几个 cache line 提前拉进来;(3)下标取模天然支持环形复用,head/tail 单调递增(永不回卷),比较大小直接相减,不用处理 wrap-around。

> **钉死这件事**:Go 的 runq 是个**有界环形数组**(256 槽)。这是"用固定空间换零分配 + 缓存友好"的典型权衡。代价是 256 槽满了要溢出——`runqputslow` 会把当前 runq 一半搬到全局 runq(见 4.3.3)。

### 4.3.2 为什么固定 256,不能动态扩容

256 是经验值,源码里是 `len(pp.runq)` 这个常量。它背后的考虑:

- **太小**(比如 16):runq 频繁满,频繁触发"搬到全局"的慢路径,全局锁竞争上升。
- **太大**(比如 4096):每个 P 的 runq 占几 KB,64 个 P 就占几十 KB,且 work-stealing 时偷一半要拷贝更多,局部性下降。

256 是个折中:够大,绝大多数程序不会触发慢路径;够小,缓存友好,P 结构不至于臃肿。

### 4.3.3 256 槽满了怎么办:runqputslow 搬一半去全局

runq 不可能无限大。当 push 发现 `t - h == 256`(满了),不能直接覆盖。Go 的处理是 [`runqputslow`](../go/src/runtime/proc.go#L7575):**把当前 runq 的一半搬到全局 runq**。

```go
// src/runtime/proc.go#L7575-L7611(节选)
func runqputslow(pp *p, gp *g, h, t uint32) bool {
    var batch [len(pp.runq)/2 + 1]*g   // 128+1 槽的临时数组
    // First, grab a batch from local queue.
    n := t - h
    n = n / 2                          // 抓一半
    if n != uint32(len(pp.runq)/2) {
        throw("runqputslow: queue is not full")
    }
    for i := uint32(0); i < n; i++ {
        batch[i] = pp.runq[(h+i)%uint32(len(pp.runq))].ptr()
    }
    if !atomic.CasRel(&pp.runqhead, h, h+n) {  // 先 CAS 推进 head
        return false                           // 失败说明 thief 也在动 head,重试
    }
    batch[n] = gp                       // 把新来的 gp 也加进 batch
    // ... 打乱 batch 顺序(link 成 gQueue)...
    lock(&sched.lock)
    globrunqputbatch(&q)                // 一把塞进全局 runq
    unlock(&sched.lock)
    return true
}
```

> **不这样会怎样**:如果满了直接 `lock(&sched.lock)` 把单个 gp 塞进全局 runq,那每次 runq 满都触发一次全局锁。把"搬一半 + 新 gp"打包成一批,一次性进全局锁——**把全局锁的代价摊到 129 个 G 上**。这和上一章 3.4.5 讲的 `globrunqgetbatch` 一次拿半批是对称的:**入队批量、出队也批量,全局锁只在批量边界出现**。

注意 `runqputslow` 第一步是 `CasRel(&pp.runqhead, h, h+n)` 推进 head——**这一步可能失败**(thief 也在 CAS head)。失败就返回 false,让 `runqput` 重试(因为 thief 偷走了一部分,runq 不再满了,可以走快路径)。这种"先试 CAS,失败就回到快路径"是 runq 全程的格调。

### 4.3.4 runnext:一个独立的插队槽

`runnext` 不在环形数组里,它是个**单槽**。它的语义是"下一个优先跑的 G"——通常是当前 G 用 `go func` 或 `ready` 唤醒的子 G。`runqget` 取 G 时**先看 runnext**,有就取它(且 `inheritTime = true`,共享当前时间片)。

为什么单开一个槽而不直接塞进 runq head?上一章 3.4.4 讲过:让"刚唤醒的 G 立刻接着跑",子 G 的栈和缓存还热,调度延迟最低。

runnext 在 work-stealing 里有个特殊地位:**它是最后一个被偷的**——只有 `stealWork` 的第 4 轮(最后一轮)才允许偷 runnext(见 4.5.1)。原因是偷 runnext 等于抢别人刚插队要跑的 G,代价大。

---

## 4.4 并发协议:owner 和 thief 凭什么不踩脚

现在到本章最硬的部分:两个 P 同时在这个环形数组上动手脚,凭什么不丢 G、不重复跑、不读到撕裂值?

先把"谁写哪个游标、配什么内存序"列成一张表,这是本章的钥匙:

| 操作 | 谁做 | 读哪个游标 | 写哪个游标 | 内存序 |
|---|---|---|---|---|
| `runqput` push | owner P 独占 | `LoadAcq(runqhead)` | `StoreRel(runqtail, t+1)` | head acquire, tail release |
| `runqget` pop | owner P 独占 | `LoadAcq(runqhead)` | `CasRel(runqhead, h+1)` | head acquire + release |
| `runqgrab` 偷 | thief P | `LoadAcq(head) + LoadAcq(tail)` | `CasRel(runqhead, h+n)` | head/tail 都 acquire 读, head release 写 |
| `runqsteal` 收尾 | thief P(在自己 P 上) | `LoadAcq(runqhead)` | `StoreRel(runqtail, t+n)` | head acquire, tail release |

四个核心观察:

1. **`runqtail` 只有 owner P 写**:thief 从来不写别人的 tail。thief 偷完后,把偷来的 G 塞进**自己的** runq,然后写**自己的** tail([`runqsteal`](../go/src/runtime/proc.go#L7796))。所以 tail 是 owner 单写者——`runqput` 里 `t := pp.runqtail` 是**普通读**,因为只有 owner 读自己的 tail。
2. **`runqhead` 是 owner 和 thief 共享的写点**:owner pop 时 `CasRel(runqhead, h+1)` 推进 head,thief grab 时 `CasRel(runqhead, h+n)` 也推进 head。两边都在 CAS head,**这就是竞争点**。
3. **所有读都用 acquire,所有写都用 release**:`LoadAcq` 保证读到最新的 head(看见 thief 的推进),`StoreRel`/`CasRel` 保证自己的写对其他 P 可见。这是 Go runtime 在弱内存架构(ARM/POWER)上需要的最小内存序,在 x86 上 acquire/release 是天然的(普通 load/store 就够),但 runtime 还是显式标注,保持可移植。
4. **owner 写 tail 用 StoreRel,CAS head 用 CasRel**:tail 是单写者,不需要 CAS;head 是多写者,必须 CAS 抢。

### 4.4.1 push:owner 写 tail 的细节

[`runqput`](../go/src/runtime/proc.go#L7529) 的 push 主体:

```go
// src/runtime/proc.go#L7558-L7570(节选)
retry:
    h := atomic.LoadAcq(&pp.runqhead)  // acquire 读 head,和 thief 的 CasRel 同步
    t := pp.runqtail                    // 普通读,owner 独占
    if t-h < uint32(len(pp.runq)) {
        pp.runq[t%uint32(len(pp.runq))].set(gp)      // 先写槽
        atomic.StoreRel(&pp.runqtail, t+1)           // 再 StoreRel 发布 tail
        return
    }
    if runqputslow(pp, gp, h, t) {
        return
    }
    goto retry
```

两个内存序技巧:

1. **先写槽,再 `StoreRel(tail)`**:写槽(`pp.runq[t%256].set(gp)`)和发布 tail(`StoreRel(runqtail, t+1)`)的顺序不能反。`StoreRel` 是个屏障:它保证**之前的写(写槽)在 tail 推进前对其他 P 可见**。thief 用 `LoadAcq(tail)` 读 tail,一旦读到 t+1,就一定能看见槽里那个 gp——**不会读到空槽或半写的槽**。

2. **`LoadAcq(runqhead)`**:push 为什么要读 head?为了算 `t - h`(还剩多少槽)。head 是 thief 也在改的,所以 acquire 读。但注意,push **不修改 head**——它只读。如果 push 时 `t - h >= 256`(满了),说明 thief 没及时偷走,push 走 `runqputslow` 搬一半去全局。

> **反面对比**:假设 push 用普通 store 写 tail(`runqtail = t+1`),不配 release 屏障。在弱内存架构上,CPU 可能重排"写槽"和"写 tail",让 tail 先变而槽还是旧值——thief 读到 tail=t+1 后去读槽,读到的是上一个 G 甚至空值。**G 就丢了**。`StoreRel`/`LoadAcq` 这对屏障堵的就是这个窗口。

### 4.4.2 pop:owner 读 head、CAS head

[`runqget`](../go/src/runtime/proc.go#L7649) 的 pop 主体(去掉 runnext 分支):

```go
// src/runtime/proc.go#L7659-L7669(节选)
for {
    h := atomic.LoadAcq(&pp.runqhead)   // acquire 读 head
    t := pp.runqtail                     // 普通读
    if t == h {
        return nil, false
    }
    gp := pp.runq[h%uint32(len(pp.runq))].ptr()   // 先读槽
    if atomic.CasRel(&pp.runqhead, h, h+1) {       // CAS 推进 head
        return gp, false
    }
    // CAS 失败,thief 改了 head,重试
}
```

注意 pop **不像 push 那样"先写后发布"**,而是**先读槽再 CAS head**。这是因为 pop 和 thief 都在改 head——必须用 CAS 抢:

- **读槽在前**:即使 CAS 失败(被 thief 抢了 head),读的 gp 也不影响正确性(丢了重读)。
- **CAS 在后**:`CasRel(head, h, h+1)` 是原子的——只有 head 仍然是 h 时才推进成 h+1。如果 thief 在这期间把 head 推进了,owner 的 CAS 失败,循环重试(重新 LoadAcq head)。

> **为什么 CAS 而不是 load-then-store**:如果 pop 用"load head → store head+1",会和 thief 的"load head → store head+n"撞车:两边都读到 h,都写自己算出的新值,后写的覆盖前写的——**G 被双重消费**(owner 取走了 G1,thief 也从同一区间偷走了 G1,两个 P 都跑 G1)。CAS 保证"只有 head 没被别人动过我才推进",堵掉双重消费。

### 4.4.3 grab:thief 同时读 head 和 tail,只改 head

[`runqgrab`](../go/src/runtime/proc.go#L7713) 是 thief 的核心。它要从受害者 p2 的 runq 偷一半:

```go
// src/runtime/proc.go#L7713-L7776(节选,去掉 runnext 偷分支)
func runqgrab(pp *p, batch *[256]guintptr, batchHead uint32, stealRunNextG bool) uint32 {
    for {
        h := atomic.LoadAcq(&pp.runqhead)   // acquire 读 head
        t := atomic.LoadAcq(&pp.runqtail)   // acquire 读 tail
        n := t - h
        n = n - n/2                          // 偷一半(向下取整)
        if n == 0 {
            // ... (runnext 偷分支,见 4.5.2)
            return 0
        }
        if n > uint32(len(pp.runq)/2) {      // 读到不一致的 h 和 t
            continue
        }
        for i := uint32(0); i < n; i++ {
            g := pp.runq[(h+i)%uint32(len(pp.runq))]
            batch[(batchHead+i)%uint32(len(batch))] = g   // 拷到 batch 数组
        }
        if atomic.CasRel(&pp.runqhead, h, h+n) {   // CAS 推进 head,提交偷
            return n
        }
        // CAS 失败,owner 改了 head,重试
    }
}
```

thief 做的事:

1. **acquire 读 head 和 tail**:注意 thief 读 tail 也要 acquire!因为 tail 是 owner 用 StoreRel 写的,thief 必须 acquire 读才能看见 owner 最新 push 的 G。
2. **算 n = (t-h) - (t-h)/2 = 一半**:向下取整的一半。比如 t-h=10,偷 5;t-h=3,偷 1(3 - 1 = 2?不,3 - 3/2 = 3 - 1 = 2,偷 2)。
3. **拷贝到 batch 数组**:thief 把偷的 n 个 G 指针拷到**自己的** `batch`(其实是 `pp.runq`,即自己的 runq 数组,见 [`runqsteal`](../go/src/runtime/proc.go#L7783) 调用)。
4. **CAS head 推进**:`CasRel(runqhead, h, h+n)`。这一步**提交偷**——只有 head 还是 h 才成功。如果 owner 这期间 pop 了(head 变成 h+1),CAS 失败,thief 重试(重新读 head/tail)。

> **`if n > uint32(len(pp.runq)/2) { continue }` 是什么**:这是**防读到撕裂的 h 和 t**。owner 写 head 和 tail 是两次独立的原子写,thief 读它们也是两次独立的原子读——在 thief 的视角里,可能读到"旧的 h 和新的 t"(owner 刚 push 了但 head 还没动),或者更糟的组合。`n > 128` 说明读到的 t-h 比 runq 容量一半还大,这不可能(满 runq 会触发 runqputslow 搬走一半,绝不会让 t-h 到 256),所以一定是读到了不一致快照——continue 重读。这是无锁算法里典型的"读到不一致就重读"模式。

### 4.4.4 steal 收尾:thief 在自己 P 上发 tail

[`runqsteal`](../go/src/runtime/proc.go#L7781) 把 grab 偷来的 G 落到自己的 runq:

```go
// src/runtime/proc.go#L7781-L7798
func runqsteal(pp, p2 *p, stealRunNextG bool) *g {
    t := pp.runqtail                                  // 自己 P 的 tail(owner 独占,普通读)
    n := runqgrab(p2, &pp.runq, t, stealRunNextG)     // 从 p2 偷,直接塞进 pp.runq[t..]
    if n == 0 {
        return nil
    }
    n--                                                // 留一个马上返回
    gp := pp.runq[(t+n)%uint32(len(pp.runq))].ptr()
    if n == 0 {
        return gp                                      // 只偷到 1 个,直接返回它跑
    }
    h := atomic.LoadAcq(&pp.runqhead)                  // acquire 读自己的 head
    if t-h+n >= uint32(len(pp.runq)) {
        throw("runqsteal: runq overflow")              // 自己 runq 满了(不应该,因为刚空才偷)
    }
    atomic.StoreRel(&pp.runqtail, t+n)                 // StoreRel 发布自己的 tail
    return gp
}
```

注意几个巧妙之处:

1. **直接写进自己的 runq 数组**:`runqgrab(p2, &pp.runq, t, ...)` 把 batch 传成 `&pp.runq`(自己的 runq 数组),`batchHead` 传成 `t`(自己的 tail)。所以 thief 偷来的 G **直接落到自己 runq 的 tail 区**,不用二次拷贝。
2. **留一个马上返回**:`n--` 后取 `pp.runq[(t+n)%256]` 作为马上要跑的 G,**剩下的 n 个**通过 `StoreRel(runqtail, t+n)` 发布到自己 runq。这样 thief 既偷了一批,又立刻有一个能跑——**一次 steal 同时满足"立刻有活跑"和"本地 runq 不再空"两个目标**。
3. **`StoreRel` 发布 tail**:和 push 一样,先写槽(grab 已经写了),再 StoreRel 发布 tail,让别的 P(如果再来偷自己的)能看见。

> **钉死这件事**:整个 work-stealing 的 sound 性,靠的就是这四条规约——**owner 单写 tail、owner 和 thief 共享 head、所有读 acquire、所有写 release、head 用 CAS 抢**。这五条缺一不可:漏了 StoreRel,弱内存架构上会丢 G;漏了 CAS head,会双重消费;漏了 acquire 读,会读到陈旧游标。

---

## 4.5 stealWork:4 轮遍历,最后一轮才偷 runnext

数据结构和并发协议讲清了,现在看 thief 怎么调度它的偷活。[`stealWork`](../go/src/runtime/proc.go#L3843) 是 `findRunnable` 第 6 级调用的:

```go
// src/runtime/proc.go#L3843-L3910(节选)
func stealWork(now int64) (gp *g, inheritTime bool, rnow, pollUntil int64, newWork bool) {
    pp := getg().m.p.ptr()
    ranTimer := false

    const stealTries = 4
    for i := 0; i < stealTries; i++ {
        stealTimersOrRunNextG := i == stealTries-1                    // (A) 最后一轮才偷 timer 和 runnext

        for enum := stealOrder.start(cheaprand()); !enum.done(); enum.next() {   // (B) 随机顺序遍历所有 P
            if sched.gcwaiting.Load() {
                return nil, false, now, pollUntil, true                // GC 在 STW,有 GC 活可干
            }
            p2 := allp[enum.position()]
            if pp == p2 {
                continue                                                // 不偷自己
            }

            // 最后一轮才检查 timer
            if stealTimersOrRunNextG && timerpMask.read(enum.position()) {
                tnow, w, ran := p2.timers.check(now, nil)
                now = tnow
                if w != 0 && (pollUntil == 0 || w < pollUntil) {
                    pollUntil = w
                }
                if ran {
                    // timer 可能让本 P 的 runq 有了 G
                    if gp, inheritTime := runqget(pp); gp != nil {
                        return gp, inheritTime, now, pollUntil, ranTimer
                    }
                    ranTimer = true
                }
            }

            // 偷 p2 的 runq(最后一轮才允许偷 runnext)
            if !idlepMask.read(enum.position()) {                      // (C) p2 不是空闲的才偷
                if gp := runqsteal(pp, p2, stealTimersOrRunNextG); gp != nil {
                    return gp, false, now, pollUntil, ranTimer
                }
            }
        }
    }
    return nil, false, now, pollUntil, ranTimer
}
```

几个关键设计,逐个拆 why:

### 4.5.1 为什么 4 轮,最后一轮才偷 runnext/timer

`stealTries = 4`,外层循环 4 次。前 3 轮 `stealTimersOrRunNextG = false`,只偷 runq 主体;第 4 轮才设 true,允许偷 runnext 和 timer。

> **为什么这么排**:runq 主体的 G 是"普通的、随时可被偷的",代价低;runnext 是"owner 刚插队要立刻跑的子 G",偷它等于抢别人嘴里的肉,代价大;timer 是"时间敏感的",偷 timer 要顺带执行到期的 timer,可能触发更多 G ready,逻辑重。所以**先偷代价小的(runq 主体),偷不到再偷代价大的(runnext/timer)**。

这个分层和 `findRunnable` 的多级回退一脉相承:**总是先走代价低的路径,失败才升级**。前 3 轮遍历所有 P 的 runq 主体,如果都没偷到(整个系统都低负载),第 4 轮才肯动 runnext 和 timer——这是"实在没别的办法了才抢 runnext"的保守策略。

### 4.5.2 随机顺序遍历:coprime 算法

`stealOrder.start(cheaprand())` 返回一个枚举器,**按伪随机顺序遍历所有 P,不重复**。这是个很巧的算法([`randomOrder`](../go/src/runtime/proc.go#L8050)):

```go
// src/runtime/proc.go#L8048-L8091(节选注释)
// The algorithm is based on the fact that if we have X such that X and GOMAXPROCS
// are coprime, then a sequences of (i + X) % GOMAXPROCS gives the required enumeration.
type randomOrder struct {
    count    uint32
    coprimes []uint32           // 所有和 count 互质的数
}

func (ord *randomOrder) start(i uint32) randomEnum {
    return randomEnum{
        count: ord.count,
        pos:   i % ord.count,
        inc:   ord.coprimes[i/ord.count%uint32(len(ord.coprimes))],  // 随机选一个 coprime 当步长
    }
}

func (enum *randomEnum) next() {
    enum.i++
    enum.pos = (enum.pos + enum.inc) % enum.count   // 每次步进 inc
}
```

数学原理:如果步长 `inc` 和 `count`(GOMAXPROCS)互质,那么序列 `pos, pos+inc, pos+2*inc, ... (mod count)` 会**不重复地遍历完 0 到 count-1**。这是数论里的一个基本事实(等价于"互质模数下加法群是循环群")。

> **不这样会怎样**:如果 thief 总是按固定顺序(0, 1, 2, ..., N-1)遍历 P,那 P0 永远第一个被偷,P_N-1 永远最后被偷——**P0 的 runq 被偷空的概率最高,P_N-1 最低**,负载再次倾斜。随机顺序让每个 P 被偷的概率均等,这是 work-stealing 公平性的基础。

而这个算法的妙处在于**零分配、O(1) 状态**——枚举器只存 `i, count, pos, inc` 四个 uint32,不需要预先 shuffle 一个数组。`reset(count)` 在 `schedinit` 时算好所有 coprimes 存起来,运行时只是查表选步长。

### 4.5.3 不偷空闲 P:`idlepMask`

`if !idlepMask.read(enum.position())`——只偷那些"不是空闲"的 P。空闲 P 的 runq 当然是空的(它本地都没活才进 idle),偷它纯浪费。`idlepMask` 是个位图,P 进 idle 时置位,P 被唤醒时清位(第 3 章 `pidleput`/`acquirep` 维护)。

### 4.5.4 那个奇怪的 `usleep(3)`:偷 runnext 前的退让

回到 `runqgrab` 的 runnext 偷分支([proc.go#L7722-L7760](../go/src/runtime/proc.go#L7722)),有一段看起来很奇怪的代码:

```go
// src/runtime/proc.go#L7722-L7757(节选)
if next := pp.runnext; next != 0 {
    if pp.status == _Prunning {
        if mp := pp.m.ptr(); mp != nil {
            if gp := mp.curg; gp == nil || readgstatus(gp)&^_Gscan != _Gsyscall {
                // Sleep to ensure that pp isn't about to run the g
                // we are about to steal.
                if !osHasLowResTimer {
                    usleep(3)              // ★ 睡 3 微秒!
                } else {
                    osyield()              // 低精度定时器平台改用 yield
                }
            }
        }
    }
    if !pp.runnext.cas(next, 0) {
        continue
    }
    batch[batchHead%uint32(len(batch))] = next
    return 1
}
```

thief 要偷 runnext 时,先检查"owner 是不是正要跑这个 runnext":如果 owner 的 P 状态是 `_Prunning`、且当前 G 不是在 syscall(说明 owner 正在用户态跑代码),thief **睡 3 微秒再偷**。

> **为什么睡 3 微秒**:注释([proc.go#L7728-L7734](../go/src/runtime/proc.go#L7728))原话:"The important use case here is when the g running on pp ready()s another g and then almost immediately blocks. Instead of stealing runnext in this window, back off to give pp a chance to schedule runnext. This will avoid thrashing gs between different Ps." 翻译:典型的场景是"owner 的 G 刚 `ready` 了一个 G(塞进 runnext),马上自己要阻塞"。如果 thief 在这个窗口里立刻偷走 runnext,会发生**G 在 P 之间反复横跳**(owner 阻塞后唤醒又被偷回来),缓存局部性全毁。睡 3 微秒给 owner 一个机会自己跑 runnext——注释里提到一次同步 channel 收发约 50ns,3us 是 50 倍冗余。

这是 work-stealing 里一个典型的"**主动退让换缓存局部性**"的微优化。它牺牲一点点 thief 的延迟,换"刚 ready 的 G 留在 owner 那里跑"的概率——因为刚 ready 的 G 的栈和数据在 owner 的 cache 里是热的,搬走就冷了。

> **钉死这件事**:`usleep(3)` 不是 bug,是个**精心调过的 heuristics**。它揭示了 work-stealing 不是单纯的"尽快偷",而是"在偷的及时性和缓存局部性之间权衡"。Go 工程师用 3us 这个数(经验值,基于"同步 channel 50ns"的测量)做了一个偏保守的退让。

---

## 4.6 技巧精解:偷一半而不是偷一个

这一章最该单独拎出来讲的技巧,是那个看起来理所当然、实则经过严密论证的决定——**偷一半**。

### 4.6.1 朴素方案:偷一个

如果我们没读过 runtime 源码,凭直觉设计 work-stealing,大概率会写"偷一个":

```go
// 朴素方案(不是 Go 的实现)
func stealOneNaive(pp, p2 *p) *g {
    h := atomic.LoadAcq(&p2.runqhead)
    t := atomic.LoadAcq(&p2.runqtail)
    if t == h { return nil }
    gp := p2.runq[h%256].ptr()
    if atomic.CasRel(&p2.runqhead, h, h+1) {
        return gp
    }
    return nil  // CAS 失败重试或返回
}
```

这个方案**正确性上没问题**(同样满足 4.4 的并发规约),但会撞上一个墙:**偷的频率爆炸**。

设想一个场景:8 个 P,P0 堆积了 100 个 G,P1~P7 都空了。如果偷一个:

1. P1 偷走 1 个,P0 剩 99。
2. P2 偷走 1 个,P0 剩 98。
3. ...
4. 每个 thief 都要进一次 `stealWork`、遍历 `stealOrder`、CAS head。
5. P0 的 head 被疯狂 CAS 竞争——**7 个 thief 同时 CAS P0.runqhead**,CAS 失败率高,重试多。
6. P1 跑完那 1 个 G 又空了,再来偷——**循环往复,每个 G 都要触发一次 steal**。

统计上,要把 P0 的 100 个 G 均匀分到 8 个 P,大约要触发几百次 steal 操作,每次都要 CAS、可能重试。这就是**偷一个的墙:负载越不均,steal 频率越高,竞争越激烈**。

### 4.6.2 Go 的方案:偷一半

Go 的选择是 `n = (t-h) - (t-h)/2`——**偷一半**([`runqgrab`](../go/src/runtime/proc.go#L7718))。同样场景:

1. P1 偷走 50 个(100 的一半),P0 剩 50,P1 立刻有 50 个 G 干。
2. P2 偷走 P0 的 25 个,P0 剩 25。
3. P3 偷走 P0 的 12 个,P0 剩 13。
4. ...
5. 几轮之内,负载快速收敛到均衡。

**偷一半的精髓在两个收敛**:

- **负载收敛快**:指数级衰减,几轮就均衡。偷一个是对数级(线性)收敛,要偷很多次。
- **steal 频率低**:每次偷完,thief 自己 runq 也满了(或者半满),短期不会再偷。**一次 steal 的代价摊到 N/2 个 G 上**,均摊成本极低。

> **反面对比**:如果把"偷一个"的方案放到上面 100 个 G 的场景,每个 G 平均触发 ~1 次 steal(P0 的 G 几乎每个都要被偷一次才能离开 P0),7 个 thief 互相 CAS 竞争 P0.head,CAS 失败率高,重试开销大,且 P0 自己的 owner 也在 pop head——**head 成了热点的竞争点**。偷一半把 head 的 CAS 频率降到 1/50,竞争消失。

### 4.6.3 为什么是一半,不是三分之一或四分之一

"一半"不是任意选的,是 work-stealing 理论里的**经典结果**(见 MIT 6.172 课程和 Java ForkJoinPool 的设计文档)。理论上,偷一半能让队列长度的期望在 O(log N) 次 steal 内收敛到均衡(N 是初始堆积数);偷少于一半(比如 1/4)收敛慢,偷多于一半(比如 3/4)会让被偷的 owner 频繁空 runq,又得去偷别人——**振荡**。

Go 的注释([proc.go#L7778](../go/src/runtime/proc.go#L7778))只写了"Steal half of elements",没解释为什么一半,但这是个经过验证的经验值,和 Java 的 ForkJoinPool、Rust 的 Tokio、Cilk 的 work-first scheduler 都一致——**业界共识是偷一半**。

### 4.6.4 一个细节:偷一半为什么不会把 owner 偷空

你可能担心:owner 刚 push 了一批 G,thief 立刻偷一半,owner 岂不是经常被偷到空?

不会,因为两个保护:

1. **owner 也在 pop**:`runqget` 是从 head pop,thief 也是从 head grab。两者竞争 head,owner pop 走一个,thief 偷走一半,剩余的还会被 owner 继续消费。owner 不会"被偷到没活",因为只要 runq 非空,owner 总能 pop(它的 pop 和 thief 的 grab 是对 head 的公平竞争)。
2. **runq 满了会先搬一半去全局**:`runqputslow` 在 runq 满时主动把一半搬到全局 runq,这本身就是一个"自我均衡"——堆积的 G 不会一直在本地,会主动溢出到全局供别的 P 取。

所以偷一半是 sound 的:它快速收敛负载,又不会让 owner 频繁空转。

---

## 4.7 双璧对照《Tokio》:两套 work-stealing

★ Go 和 Tokio 都用 work-stealing,但数据结构和实现细节不同。对照如下:

| 维度 | Go runtime | Tokio(multi-thread scheduler) |
|---|---|---|
| 本地队列结构 | 256 槽固定环形数组 + 独立 runnext 槽 | Chase-Lev 双端队列(动态,基于 `crossbeam-deque`) |
| 偷的数量 | 偷一半(`n = (t-h)/2` 向下取整) | 偷一半(`Steal::success` 偷一个,但批量 steal 偷一半) |
| owner 端操作 | push/pop 都在 head/tail,head 用 CAS | push/pop 在一端(无锁),steal 在另一端(CAS) |
| 内存序 | 显式 `LoadAcq`/`StoreRel`/`CasRel` | Rust atomics 的 `Ordering::Acquire`/`Release`/`AcqRel` |
| 全局回退队列 | 全局 runq(`sched.runq`,加 `sched.lock`) | 全局注入队列(`inject`,加锁) |
| 随机遍历 | coprime 算法 O(1) 状态 | Fisher-Yates shuffle 或 rand 遍历 |

最核心的差异是**本地队列的数据结构**:Go 用固定 256 数组(简单、缓存友好、零分配,但满了要溢出),Tokio 用 Chase-Lev 双端队列(动态容量、经典无锁 work-stealing 数据结构,但实现更复杂、要处理 ABA 等)。两者都偷一半,都遵守"owner 单写一端、thief 从另一端 CAS 偷"的基本规约——**work-stealing 的思想是通用的,实现是各自的工程取舍**。

---

## 章末小结

这一章把 Go 调度器的负载均衡机制——work-stealing——拆透了。回到二分法,这一章服务**调度执行**:它讲清了"一个本地 runq 空了的 P,怎么主动从别的 P 偷一半 G 回来跑",这是 GMP 调度器在多核上做到负载均衡的核心手段。它和上一章 `findRunnable` 的第 6 级直接对接——`stealWork` 就是那一级的实现。

### 五个"为什么"清单

1. **为什么需要 work-stealing?** 本地 runq 天生负载不均(每个 P 上 G 的执行时长不同,请求分布不均)。没有 work-stealing,会出现"有的 P 堆积、有的 P 空转"的病态。work-stealing 让空 P 主动偷堆积 P 的活,把负载拉平。

2. **无锁 runq 凭什么让两个 P 并发读写不踩脚?** 五条规约:owner 单写 tail、owner 和 thief 共享 head、所有读 acquire、所有写 release、head 用 CAS 抢。漏了 StoreRel 弱内存架构上会丢 G,漏了 CAS head 会双重消费,漏了 acquire 读会读到陈旧游标。

3. **为什么偷一半而不是偷一个?** 偷一半让负载指数级收敛(几轮就均衡),且 steal 频率低(一次 steal 摊到 N/2 个 G 上)。偷一个是对数级收敛、steal 频率爆炸,会让 head 成为热点 CAS 竞争点。一半是 work-stealing 理论的经典结果(Cilk/ForkJoinPool/Tokio 都一致)。

4. **`runqgrab` 里 `if n > 128 { continue }` 是干什么?** 防止读到撕裂的 head/tail 快照。owner 写 head 和 tail 是两次独立原子写,thief 读也是两次独立原子读,可能读到不一致组合。t-h 大于 128 不可能(满 runq 会触发 runqputslow 搬走一半),所以一定是不一致快照,continue 重读。

5. **偷 runnext 前的 `usleep(3)` 是什么意思?** owner 的 G 刚 `ready` 一个 G(塞进 runnext)马上要阻塞时,thief 立刻偷走 runnext 会导致 G 在 P 间横跳、缓存变冷。睡 3 微秒给 owner 一个机会自己跑 runnext(同步 channel 收发约 50ns,3us 是 50 倍冗余)。这是"及时性 vs 缓存局部性"的微优化。

### 想继续深入往哪钻

- **源码文件**:本章主战场 [`../go/src/runtime/proc.go`](../go/src/runtime/proc.go) 的 `stealWork`@3843 / `runqsteal`@7781 / `runqgrab`@7713 / `runqget`@7649 / `runqput`@7529 / `runqputslow`@7575 / `runqempty`@7498;P 结构的 runq 字段在 [`../go/src/runtime/runtime2.go`](../go/src/runtime/runtime2.go#L804-L820) 的 L804-L820;`randomOrder`(coprime 遍历)在 [`../go/src/runtime/proc.go`](../go/src/runtime/proc.go#L8044-L8098)。把 `runqgrab` 和 `runqsteal` 配合读一遍,看 thief 怎么把偷来的 G 直接写进自己 runq 数组。
- **观测 work-stealing**:`GODEBUG=schedtrace=1000` 打印的每个 P 行的 `runqueue=` 字段是本地 runq 长度(`runqtail - runqhead`),长时间观察能看到各 P 的 runqueue 长度在 work-stealing 作用下趋于均衡。`go tool trace` 里能看到 "Proc start" / "Proc stop" 和 goroutine 在 P 之间的迁移。
- **work-stealing 理论**:Robert Blumofe 和 Charles Leiserson 1994 年的论文 "Scheduling Multithreaded Computations by Work Stealing" 是这套理论的奠基之作;MIT 6.172 课程有完整推导。Java 的 `ForkJoinPool`、Cilk 的 scheduler、Rust Tokio 都基于此。
- **测试**:`../go/src/runtime/` 下有些针对 runq 并发正确性的测试(搜 `runq` 相关 test),能看到 Go 怎么用并发压测验证无锁 runq 不丢 G。

### 引出下一章

我们讲透了 work-stealing,但有个问题一直挂着:**如果一个 G 跑一个死循环,不让出 P,work-stealing 还有什么用?** 别的 P 偷不走它的活(它没在 runq 里),它的 P 也调不出别的 G(它占着 P 不进 schedule)。Go 1.14 之前靠"函数前奏检查栈哨兵"的协作式抢占,死循环里不调函数就永远不让出。下一章我们钻进**异步抢占**——Go 怎么靠 `SIGURG` 信号在任何安全点打断一个死循环 G,凭什么这个打断不破坏 GC 和栈。

---

> 全书定位:第 4 章 / 第 1 篇 GMP 调度器(全书地基)。源码版本 Go 1.27(本地 master @ `6d1bcd10`,Version 常量见 `src/internal/goversion/goversion.go` 的 `const Version = 27`)。下一章:P1-05 异步抢占。
