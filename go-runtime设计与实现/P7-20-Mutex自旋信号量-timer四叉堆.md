# 第二十章 · Mutex 自旋+信号量、timer 四叉堆

> 篇:第 7 篇 · sync 原语与 timer(收束篇)
> 主线呼应:前面六章把调度器、channel、内存、GC、栈、netpoll 逐一拆开。这一章收束到两个天天写、却很少往里看一眼的日常原语——`sync.Mutex` 和 `time.Sleep`。一句 `mu.Lock()`、一行 `time.Sleep(time.Second)`,背后是 Go 把"自旋/睡眠的取舍"和"四叉堆调度"两件事做精了。它们都是上一章 `findRunnable` 多级回退里被反复点到的"工作源":`sync.Mutex` 的等待者靠信号量 `runtime_Semrelease` 唤醒进 runq;timer 的到期靠 `checkTimers` 在 `findRunnable` 末尾被调用,把睡到点的 G `ready` 出来。所以这一章是第 3 章"调度循环"的延伸:那两个原语怎么往调度器里"塞活"和"取活"。

## 核心问题

**`sync.Mutex` 怎么先用自旋再加锁(normal 模式换吞吐、starvation 模式防饿死)?底层信号量(`runtime_SemacquireMutex`/`Semrelease`)是怎么把"阻塞一个 G"这件事交给调度器的?`time.Sleep`/`After` 的 timer 又凭什么用四叉堆——为什么是四叉而不是二叉?**

读完本章你会明白:

1. `Mutex` 的 `state` 这一个 `int32` 怎么同时编码"锁位 / 唤醒位 / 饥饿位 / 等待者计数",以及快路径凭什么只是一条 `CAS`(0 → `mutexLocked`)。
2. normal / starvation 双模式为什么这么设计:normal 自旋换吞吐、starvation 直接 handoff 防 1ms 尾延迟;`starvationThresholdNs = 1e6` 这个阈值的来历。
3. 自旋为什么不是"空转烧 CPU":`runtime_canSpin` 的三条护栏(多核、有别的忙 P、本地 runq 空),以及 `runtime_doSpin` 真正执行的 `PAUSE` 指令。
4. 信号量在 runtime 里长什么样:251 个桶的 `semtable`、按地址哈希分桶的 `semaRoot`、treap(树堆)组织同类等待者、`nwait` 原子计数防"误叫醒"。
5. timer 用四叉堆(`timerHeapN = 4`)的取舍:为什么不是二叉(堆高度、缓存友好、父指针计算),以及 `siftUp`/`siftDown` 怎么操作。

> 逃生阀:`lockSlow` 那段 `for` 循环里 `old`/`new`/CAS/`awoke` 几个变量来回切,第一眼看像天书。别慌——拆开看它每轮只干三件事:**判断能不能自旋**、**算出"我想把 state 改成什么样"**、**CAS 抢一次,抢到要么进临界区要么去睡**。把握住这三步的循环,所有 `|=` `&^=` 的位操作就只是"在 new 里把对应位摆好"。四叉堆的 `siftUp`/`siftDown` 比二叉堆只是把"两个子"换成"四个子",逻辑同构。

---

## 20.1 一句话点破

> **`sync.Mutex` 是"快路径 CAS + 慢路径自旋 + 信号量 park"的三段式:大多数锁没人抢,一条 CAS 拿走;抢了就先自旋几轮赌"持有者马上放"(省一次 park/wake 的上下文切换);自旋还不行才 park 到信号量上,把线程让出去跑别的 G。timer 则是一个"按到期时间排最小堆"的调度小天地,Go 选了四叉堆——堆更扁、缓存更友好、sift 更便宜。**

这是结论,不是理由。本章倒过来拆:先看 `Mutex.state` 这一个 int32 怎么塞下所有信息,再拆快路径、自旋、信号量 park 三段;然后看 normal/starvation 双模式凭什么在这两段间切换;最后钻 timer 四叉堆的算法和它跟调度器(`checkTimers`/`wakeTime`)的接口。

---

## 20.2 `Mutex.state`:一个 int32 塞下一台状态机

[`Mutex`](../go/src/internal/sync/mutex.go#L20-L23) 的全部字段就两个:

```go
// src/internal/sync/mutex.go#L20-L23
type Mutex struct {
    state int32
    sema  uint32
}
```

(对外暴露的 [`sync.Mutex`](../go/src/sync/mutex.go#L30-L34) 包了一层,把实现委托给 `internal/sync.Mutex`;真正的逻辑在 internal 版里,这也是 1.21 之后拆出来的"包内包",为了打破 `sync` 依赖 `runtime` 的循环导入。)

`sema` 是底层信号量地址,等会儿讲。`state` 这一个 int32 被切成四段:

```go
// src/internal/sync/mutex.go#L25-L29
const (
    mutexLocked = 1 << iota // 第 0 位:锁住没有
    mutexWoken              // 第 1 位:有人被唤醒过(或正在自旋),Unlock 时别再叫人
    mutexStarving           // 第 2 位:进入饥饿模式
    mutexWaiterShift = iota // = 3,等待者计数从第 3 位开始
)
```

画出来:

```
  state (int32) 位布局
  31                              3   2          1          0
 ┌──────────────────────────────┬─────┬──────────┬──────────┬──────────┐
 │  waiter 计数(等待中的 G 数) │star │  woken   │  (未用)  │  locked  │
 │        (最多 ~5 亿)          │ ving│          │          │          │
 └──────────────────────────────┴─────┴──────────┴──────────┴──────────┘
  mutexWaiterShift = 3             2       1          (1<<1)      1<<0
```

> **所以这样设计**:把"锁位、唤醒位、饥饿位、等待者数"四个独立信息压进一个 int32,好处是**所有这些状态可以用一次 CAS 原子地改**。这是 Mutex 性能的根基:`Unlock` 时它要同时做"清锁位 + 减等待者 + 置唤醒位",如果分成四个字段,要么加锁、要么四次 CAS(中间会丢一致性),都远比一次 CAS 慢。一个 int32 当一台状态机,是把"无锁"做出来的前提。

> **钉死这件事**:`state` 是 Mutex 全部并发控制的"总账本"。任何想改它的人,都先读 `old := m.state`,在本地算出 `new`,然后 `atomic.CompareAndSwapInt32(&m.state, old, new)` 抢一次——抢到才算数,抢不到说明别人改了,重读 `old` 再来。这套"读-算-CAS"的乐观并发,贯穿快路径和慢路径。

---

## 20.3 快路径:一条 CAS 拿走锁

[Lock](../go/src/internal/sync/mutex.go#L61-L71) 第一行就是快路径:

```go
// src/internal/sync/mutex.go#L61-L71
func (m *Mutex) Lock() {
    // Fast path: grab unlocked mutex.
    if atomic.CompareAndSwapInt32(&m.state, 0, mutexLocked) {
        if race.Enabled {
            race.Acquire(unsafe.Pointer(m))
        }
        return
    }
    // Slow path (outlined so that the fast path can be inlined)
    m.lockSlow()
}
```

快路径的语义是:**"如果当前 state 完全是 0(没锁、没等待者、没饥饿),就把它 CAS 成 `mutexLocked`。"** 一条原子指令,拿到锁。`race.Acquire` 是给 race detector 用的(不开就是空操作),可以忽略。

注意 CAS 的预期值是 `0`,不是 `mutexLocked`。这是个细节:**只有"完全干净"的 state 才走快路径**。如果锁被持有但没人等(`state == mutexLocked`),或者锁空着但有等待者在排队(`state == 某个 waiter 计数`),都会 CAS 失败落到慢路径。这保证快路径只在"无竞争"的最理想情况下命中——而现实里大多数 `Lock()` 都是无竞争的(临界区短、并发不高),所以快路径命中率极高,这就是 Mutex 便宜的根本。

> **不这样会怎样**:如果每次 `Lock` 都直接进慢路径(自旋、查等待者、可能 park),那无竞争场景下也要付一遍"读 state、判饥饿、CAS、可能进 sema"的开销。把最理想的 CAS 单拎出来 inline 进调用者,Go 把"无竞争 Lock"压到了几条指令。这是教科书级的"快路径分离"(fast path outlined slow path)。

---

## 20.4 慢路径一:自旋——赌持有者马上放

落到 [lockSlow](../go/src/internal/sync/mutex.go#L95-L182) 之后,第一件事不是马上 park,而是问"能不能自旋":

```go
// src/internal/sync/mutex.go#L101-L116(节选)
old := m.state
for {
    // Don't spin in starvation mode, ownership is handed off to waiters
    // so we won't be able to acquire the mutex anyway.
    if old&(mutexLocked|mutexStarving) == mutexLocked && runtime_canSpin(iter) {
        // Active spinning makes sense.
        // Try to set mutexWoken flag to inform Unlock
        // to not wake other blocked goroutines.
        if !awoke && old&mutexWoken == 0 && old>>mutexWaiterShift != 0 &&
            atomic.CompareAndSwapInt32(&m.state, old, old|mutexWoken) {
            awoke = true
        }
        runtime_doSpin()
        iter++
        old = m.state
        continue
    }
    ... // 后面是 park 的逻辑
}
```

自旋的前提有两道:

1. `old&(mutexLocked|mutexStarving) == mutexLocked`:锁是"被持有 + 没进饥饿模式"。饥饿模式下不自旋——因为饥饿模式是 handoff,自旋也抢不到(见 20.5)。
2. `runtime_canSpin(iter)`:[runtime 那侧的护栏](../go/src/runtime/proc.go#L7983-L8002),不是"想转就转"。

`runtime_canSpin` 在 runtime 里实现([proc.go#L7989-L8002](../go/src/runtime/proc.go#L7989),这里贴 Go 1.27 的 `internal_sync_runtime_canSpin`):

```go
// src/runtime/proc.go#L7989-L8002(节选,即 internal_sync_runtime_canSpin)
func internal_sync_runtime_canSpin(i int) bool {
    // sync.Mutex is cooperative, so we are conservative with spinning.
    // Spin only few times and only if running on a multicore machine and
    // GOMAXPROCS>1 and there is at least one other running P and local runq is empty.
    if i >= active_spin || numCPUStartup <= 1 || gomaxprocs <= sched.npidle.Load()+sched.nmspinning.Load()+1 {
        return false
    }
    if p := getg().m.p.ptr(); !runqempty(p) {
        return false
    }
    return true
}
```

注释里那句话是钥匙:"**sync.Mutex is cooperative, so we are conservative with spinning**"——和 runtime 内部那个用 `mutex`(全大写那个,见 `runtime2.go` 的 `lock` 字段)的 spin 不同,runtime 内部锁可以"passive spin"(等的时候让别的 G 跑),但 `sync.Mutex` 是协作式的,自旋期间这个 G 占着 P 啥也不干,所以**必须保守**。三条护栏:

- `i >= active_spin`(=`4`,见 [lock_spinbit.go#L54](../go/src/runtime/lock_spinbit.go#L54)):最多自旋 4 轮。
- `numCPUStartup <= 1`:单核机器不转——单核上自旋等于让持有锁的那个 G 拿不到 CPU 去释放锁,纯粹浪费。
- `gomaxprocs <= sched.npidle.Load()+sched.nmspinning.Load()+1`:**当前没有"另一个忙 P 能去释放锁"**时不转。这是核心——自旋的意义是"赌别的 P 上的 G 马上放锁",如果别的 P 都闲着或都在自旋(没人能去放锁),自旋就是空想。
- 本地 runq 非空时不转:本地有活干,不如先去跑那个活,park 自己让 CPU 干正事。

`runtime_doSpin` 真正干的是 `procyield(active_spin_cnt)`(`active_spin_cnt = 30`):

```go
// src/runtime/proc.go#L8004-L8008(注释 + 函数,函数起于 L8006)
//go:linkname internal_sync_runtime_doSpin internal/sync.runtime_doSpin
//go:nosplit
func internal_sync_runtime_doSpin() {
    procyield(active_spin_cnt)
}
```

`procyield` 在 amd64 上调 [procyieldAsm](../go/src/runtime/asm_amd64.s#L832-L841):

```asm
// src/runtime/asm_amd64.s#L832-L841
TEXT runtime·procyieldAsm(SB),NOSPLIT,$0-0
    MOVL    cycles+0(FP), AX        // AX = 30
    TESTL   AX, AX
    JZ      done
again:
    PAUSE                          // x86 的 PAUSE 指令
    SUBL    $1, AX
    JNZ     again                  // 循环 30 次
done:
    RET
```

`PAUSE` 不是 `NOP`。在 x86 上,`PAUSE` 给 CPU 一个提示:"这段是自旋等待循环"——CPU 会:(a) 暂时把流水线的内存序乱序降下来,减少分支预测惩罚;(b) 在超线程(SMT)的核上,把执行资源让给另一个硬件线程。换句话说,**`PAUSE` 让"自旋"对同核另一个线程的危害降到最低**。

> **技巧**:自旋时还有个配套动作——`if !awoke && old&mutexWoken == 0 ... CAS(old, old|mutexWoken)`。这是**抢着把 `mutexWoken` 位置 1**,告诉将来 `Unlock` 的人:"已经有自旋者醒着了,你 `Unlock` 时别去 `Semrelease` 叫醒另一个 park 的 G"。为什么?因为既然有自旋者,Unlock 后那个自旋者大概率立刻拿到锁,再叫醒一个 park 的 G 反而是浪费(它醒来发现锁又被抢了,又得 park)。这是个把"自旋"和"信号量唤醒"两个机制解耦的协调位。

> **不这样会怎样**(注释 [mutex.go#L31-L40](../go/src/internal/sync/mutex.go#L31) 直接说了):如果完全没有自旋,锁一旦被持有,等待者立刻 park。持有者释放后要 `Semrelease` 把等待者 `ready`、塞回 runq、等 `schedule` 把它取出来——这一套 park/wake 的开销(几百纳秒到几微秒)对"临界区极短、马上就放"的锁是巨大浪费。自旋几轮赌"持有者马上放",命中了就省掉整个 park/wake。但反过来,如果不加护栏地无限自旋(像 Java 老版本 `synchronized` 那样),高并发长临界区会把 CPU 烧穿——所以 Go 才加那三条护栏。自旋是个**精确权衡**,不是"想转就转"。

---

## 20.5 双模式:normal vs starvation

讲清自旋之后,关键来了——Go 的 Mutex 不是单一策略,它有**两种模式**,会根据"等待者等了多久"动态切换。这是 Go 1.9 加进来的(`[proposal 13099](https://go.dev/issue/13099)`),为了堵一个老问题:**纯 normal 模式下,新到的 G 因为已经在 CPU 上,会反复抢过刚被唤醒的等待者,导致等待者被无限延迟(尾延迟爆炸)**。

注释 [mutex.go#L31-L55](../go/src/internal/sync/mutex.go#L31) 把两个模式的语义写得很清楚,这里直白重述:

- **normal 模式**(默认):等待者按 FIFO 入队,但被唤醒后**不直接拥有锁**,要和新到的 G 抢。新到的 G 因为已在 CPU 上有优势,等待者经常抢输——抢输就重新排队。这样吞吐高(已经在 CPU 上的 G 立刻干活),但长等待者可能被反复插队。
- **starvation 模式**:某个等待者等了超过 `1ms`(`starvationThresholdNs = 1e6`),Mutex 切到饥饿模式。此后 `Unlock` **直接把所有权 handoff 给队首等待者**,新到的 G 不抢、不自旋、直接排队。饥饿模式下尾延迟有保证。

退出条件([mutex.go#L48-L54](../go/src/internal/sync/mutex.go#L48)):等待者拿到锁后,如果(a)它是最后一个等待者,或(b)它等的时间不到 1ms,就切回 normal。

切换在哪里发生?在 `lockSlow` 慢路径里,有两个关键点:

**切到饥饿**(在 park 之前算 `new` 时,[mutex.go#L129-L131](../go/src/internal/sync/mutex.go#L129)):

```go
// The current goroutine switches mutex to starvation mode.
// But if the mutex is currently unlocked, don't do the switch.
if starving && old&mutexLocked != 0 {
    new |= mutexStarving
}
```

其中 `starving` 在每次从 `SemacquireMutex` 醒来后更新([mutex.go#L150](../go/src/internal/sync/mutex.go#L150)):

```go
starving = starving || runtime_nanotime()-waitStartTime > starvationThresholdNs
```

——一旦累计等过 1ms,`starving` 就置 true,下一轮 CAS 就把 `mutexStarving` 位也置上。

**退出饥饿**(被 handoff 唤醒后,[mutex.go#L152-L171](../go/src/internal/sync/mutex.go#L152)):

```go
if old&mutexStarving != 0 {
    // ownership was handed off to us but mutex is in somewhat
    // inconsistent state: mutexLocked is not set and we are still
    // accounted as waiter. Fix that.
    if old&(mutexLocked|mutexWoken) != 0 || old>>mutexWaiterShift == 0 {
        throw("sync: inconsistent mutex state")
    }
    delta := int32(mutexLocked - 1<<mutexWaiterShift)   // 加锁位, 减一个等待者
    if !starving || old>>mutexWaiterShift == 1 {
        // Exit starvation mode.
        delta -= mutexStarving                            // 顺便清饥饿位
    }
    atomic.AddInt32(&m.state, delta)
    break
}
```

注意这里有个反直觉:**饥饿模式下被唤醒时,`mutexLocked` 位并没有被 Unlock 设上**——Unlock 在饥饿分支只调 `runtime_Semrelease(&m.sema, true, 2)`(`handoff=true`,见 20.6),把"权力"直接交给等待者,等待者自己用 `atomic.AddInt32` 把 `mutexLocked` 加回去。注释里那句 "mutexLocked is not set and we are still accounted as waiter. Fix that." 说的是这个:被 handoff 的等待者醒来时,state 是"没锁但还记着我是个等待者"的中间态,要自己一次性把锁位加上、等待者计数减掉。

> **钉死这件事**:饥饿模式的 handoff 是 Mutex 设计里最巧妙的一笔。它打破了"Unlock 只是把锁放下、谁抢到算谁的"的传统语义,变成"Unlock 直接把锁递给队首等待者"。代价是:handoff 期间 `Unlock` 的 G 要 `goyield`(让出时间片给被 handoff 的 G 立刻跑),有点像一次同步上下文切换。但这个代价只在"已经饿惨了"的时候才付——大多数时候还是 normal 模式的便宜。Go 用"双模式 + 1ms 阈值"在吞吐和公平之间画了条动态线。

---

## 20.6 慢路径二:park 到信号量

自旋失败(或进了饥饿模式),就走信号量 park。看 `lockSlow` 里 CAS 成功后的分支([mutex.go#L140-L177](../go/src/internal/sync/mutex.go#L140)):

```go
// src/internal/sync/mutex.go#L140-L150(节选)
if atomic.CompareAndSwapInt32(&m.state, old, new) {
    if old&(mutexLocked|mutexStarving) == 0 {
        break // CAS 抢到了, 且锁本来是空的, 直接到手
    }
    // If we were already waiting before, queue at the front of the queue.
    queueLifo := waitStartTime != 0
    if waitStartTime == 0 {
        waitStartTime = runtime_nanotime()
    }
    runtime_SemacquireMutex(&m.sema, queueLifo, 2)
    ...
}
```

`runtime_SemacquireMutex` 是个 `//go:linkname` 到 runtime 的桥([internal/sync/runtime.go#L21](../go/src/internal/sync/runtime.go#L21)),runtime 侧的实现是 [internal_sync_runtime_SemacquireMutex](../go/src/runtime/sema.go#L93-L96),它转调 [semacquire1](../go/src/runtime/sema.go#L146):

```go
// src/runtime/sema.go#L142-L201(节选)
func semacquire1(addr *uint32, lifo bool, profile semaProfileFlags, skipframes int, reason waitReason) {
    gp := getg()
    if gp != gp.m.curg { throw("semacquire not on the G stack") }

    // Easy case.
    if cansemacquire(addr) {     // 再试一次原子减
        return
    }
    // Harder case: enqueue itself as a waiter, sleep.
    s := acquireSudog()          // 复用一个 sudog(等会儿讲)
    root := semtable.rootFor(addr)
    ...
    for {
        lockWithRank(&root.lock, lockRankRoot)
        root.nwait.Add(1)        // ★ 先把自己记进 nwait
        if cansemacquire(addr) { // 再试一次, 防丢唤醒
            root.nwait.Add(-1)
            unlock(&root.lock)
            break
        }
        root.queue(addr, s, lifo)             // 入队
        goparkunlock(&root.lock, reason, traceBlockSync, 4+skipframes)  // ★ park
        if s.ticket != 0 || cansemacquire(addr) {
            break
        }
    }
    ...
    releaseSudog(s)
}
```

这一段是 Go 把"阻塞一个 G"交给调度器的标准姿势,有几个值得拆的点:

**`cansemacquire(addr)`** 是对 `*addr`(也就是 `m.sema`)做 `atomic.Cas(addr, v, v-1)`([sema.go#L291-L301](../go/src/runtime/sema.go#L291))。信号量是个 `uint32` 计数器——`Semrelease` 把它 +1,`cansemacquire` 抢着 -1。**注意 `m.sema` 平时是 0**,只有 `Unlock` 调 `Semrelease` 把它 +1 之后,等待者才能 -1 成功。所以信号量语义是:"有几个 token 就能唤醒几个等待者"。

**关键防丢唤醒协议**:`semacquire1` 在 park 前做了三件事——`root.nwait.Add(1)` → `cansemacquire` 重试 → `root.queue` → `goparkunlock`。这个顺序不能乱。`nwait` 是个原子计数,**`Semrelease` 看到 `nwait == 0` 就直接 return 不去 dequeue**([sema.go#L214-L216](../go/src/runtime/sema.go#L214))。所以等待者必须先 `nwait++` 再去查能不能抢——这样如果 `Semrelease` 在这两步之间发生,它会看到 `nwait > 0`,会去 `dequeue` 把这个等待者叫醒,不会丢。

**`goparkunlock`**:这一句是"释放 `root.lock` + `gopark`"的原子组合。`gopark` 把当前 G 的状态从 `_Grunning` 改成 `_Gwaiting`,然后 `mcall` 切回 g0,在 g0 上调 `schedule()` 去跑别的 G(这是第 3 章 3.5 讲过的栈切换)。当前 G 就这么"挂在" sudog `s` 里,被链在 `root.treap` 上,等别人 `readyWithTime(s)` 把它 `goready` 回 `_Grunnable` 塞进 runq。这整套——`gopark`/`goready`/`mcall`/`schedule`——是上一章拆过的调度循环,这里只是它的一个调用点。

**`lifo` 参数**:`queueLifo := waitStartTime != 0`。第一次 park 用 FIFO(`lifo=false`,塞队尾),被唤醒后再 park 用 LIFO(`lifo=true`,塞队首)。这是饥饿模式的配套——等得久的等待者被叫醒后如果又没抢到,塞回队首而不是队尾,避免被新人无限插队。

> **钉死这件事**:信号量是 Mutex 和调度器之间的"接口层"。Mutex 自己不直接 park G,它把"我要让一个 G 等这个 `sema`"这件事委托给 runtime 的 `semacquire1`,后者用 `gopark` 把 G 交出去、`goready` 把 G 接回来。这种分层让 `sync.Mutex` 既能用纯 Go 写位操作(`state`/CAS/自旋),又能无缝接入 runtime 的调度器——`gopark`/`goready` 是它们之间的契约。

---

## 20.7 Unlock:放锁、唤醒、handoff

[Unlock](../go/src/internal/sync/mutex.go#L187-L200) 也有快慢路径:

```go
// src/internal/sync/mutex.go#L187-L200
func (m *Mutex) Unlock() {
    ...
    // Fast path: drop lock bit.
    new := atomic.AddInt32(&m.state, -mutexLocked)
    if new != 0 {
        // Outlined slow path to allow inlining the fast path.
        m.unlockSlow(new)
    }
}

func (m *Mutex) unlockSlow(new int32) {
    if (new+mutexLocked)&mutexLocked == 0 {
        fatal("sync: unlock of unlocked mutex")   // 重复 Unlock 直接 fatal(不是 panic)
    }
    if new&mutexStarving == 0 {
        old := new
        for {
            if old>>mutexWaiterShift == 0 || old&(mutexLocked|mutexWoken|mutexStarving) != 0 {
                return                              // 没等待者 / 已有人被叫醒 / 又被锁了, 啥也不做
            }
            new = (old - 1<<mutexWaiterShift) | mutexWoken
            if atomic.CompareAndSwapInt32(&m.state, old, new) {
                runtime_Semrelease(&m.sema, false, 2)   // ★ handoff=false
                return
            }
            old = m.state
        }
    } else {
        // Starving mode: handoff mutex ownership to the next waiter
        runtime_Semrelease(&m.sema, true, 2)        // ★ handoff=true
    }
}
```

快路径就是 `atomic.AddInt32(&m.state, -mutexLocked)` 清锁位。如果 `new == 0`(没等待者、没饥饿),完事——快路径。否则进 `unlockSlow`。

`unlockSlow` 的 normal 分支([mutex.go#L207-L225](../go/src/internal/sync/mutex.go#L207))有几个细节:

- **`old>>mutexWaiterShift == 0`**:没等待者,不用叫人。
- **`old&(mutexLocked|mutexWoken|mutexStarving) != 0`**:锁又被别人拿了(自旋者抢到了)、或已经有人被叫醒(`mutexWoken` 位被自旋者置上了)、或别人切了饥饿模式——任何一种都意味着"不用我再叫人了",直接 return。这就是 20.4 讲的"`mutexWoken` 协调位"在 Unlock 这一侧的消费。
- 通过这些检查后,CAS 把等待者计数 -1、置 `mutexWoken`(被叫醒的 G 在拿到锁后会清掉它),然后 `runtime_Semrelease(&m.sema, false, 2)`。

饥饿分支就一句:`runtime_Semrelease(&m.sema, true, 2)`——`handoff=true`,把 token 直接递给队首等待者,而且不在 state 里置 `mutexLocked`(等待者醒来自己置,见 20.5)。

`Semrelease` 在 runtime 里是 [semrelease1](../go/src/runtime/sema.go#L207-L289):

```go
// src/runtime/sema.go#L207-L289(节选)
func semrelease1(addr *uint32, handoff bool, skipframes int) {
    root := semtable.rootFor(addr)
    atomic.Xadd(addr, 1)                          // ★ 先把 sema +1

    // Easy case: no waiters?
    if root.nwait.Load() == 0 {
        return                                    // 没等待者, +1 完事
    }

    // Harder case: search for a waiter and wake it.
    lockWithRank(&root.lock, lockRankRoot)
    if root.nwait.Load() == 0 {
        unlock(&root.lock); return                // 双查, 防竞争
    }
    s, t0, tailtime := root.dequeue(addr)         // 从 treap 取一个等待者
    if s != nil {
        root.nwait.Add(-1)
    }
    unlock(&root.lock)
    if s != nil {
        ...
        if handoff && cansemacquire(addr) {
            s.ticket = 1                          // ★ 给等待者一个"免抢"票
        }
        readyWithTime(s, 5+skipframes)            // ★ goready(s.g)
        if s.ticket == 1 && getg().m.locks == 0 && getg() != getg().m.g0 {
            // Direct G handoff: 让被 handoff 的 G 立刻跑
            goyield()
        }
    }
}
```

几个 sound 点:

- **先 `atomic.Xadd(addr, 1)` 再查 `nwait`**:顺序不能反。`semacquire1` 那侧是"先 `nwait++` 再 `cansemacquire`",这侧是"先 `addr++` 再查 `nwait`"——两边交叉的 StoreLoad 让"加 token"和"加等待者"互不漏(这就是经典的 Dekker 风格握手,Go 用 atomic 的 acq/rel 在 x86 上自动满足)。
- **双查 `nwait`**:第一次无锁查(`Load`),如果 0 直接 return,省一次锁;拿到锁后再查一次,防"第一次查和拿锁之间 nwait 变了"。
- **`handoff=true` 时设 `s.ticket = 1`**:这是给被 handoff 的等待者一张"免抢票"。`semacquire1` 醒来后检查 `s.ticket != 0` 直接 break(不用再去 `cansemacquire` 抢),因为它已经被"钦定"了。配套的 `goyield()` 让当前 G 让出 P,被 handoff 的 G 立刻在 runnext 上跑——这就是饥饿模式"低尾延迟"的物理实现。
- **`goyield` 的护栏**:`getg().m.locks == 0 && getg() != getg().m.g0`。持锁时不能 yield(yield 会进调度器,持 runtime 锁进调度器是 `throw`);在 g0 上不能 yield(g0 不能被调度走)。这两个护栏保证 handoff 只在"安全可调度"时做。

> **不这样会怎样**:如果 `Semrelease` 不先 `addr++` 就去叫醒等待者,等待者醒来 `cansemacquire` 发现 `addr == 0` 抢不到,又得 park——白白唤醒一次。如果不在 `nwait == 0` 时 early return,每次 `Unlock` 都去 `lock(&root.lock)` 进 treap 找人,无等待者场景下 Unlock 也要付锁代价。这两个优化让"无竞争 Unlock"保持在 `atomic.Add` 一条指令。

---

## 20.8 信号量的真身:251 个桶 + treap

最后看一眼信号量在 runtime 里的全局结构,这是 Mutex/RWMutex/WaitGroup/channel 的底层共用件。在 [sema.go#L40-L58](../go/src/runtime/sema.go#L40):

```go
// src/runtime/sema.go#L40-L58
type semaRoot struct {
    lock  mutex
    treap *sudog        // root of balanced tree of unique waiters.
    nwait atomic.Uint32 // Number of waiters. Read w/o the lock.
}

var semtable semTable

const semTabSize = 251

type semTable [semTabSize]struct {
    root semaRoot
    pad  [cpu.CacheLinePadSize - unsafe.Sizeof(semaRoot{})]byte
}

func (t *semTable) rootFor(addr *uint32) *semaRoot {
    return &t[(uintptr(unsafe.Pointer(addr))>>3)%semTabSize].root
}
```

这套结构的设计目标:**支持任意多个独立的信号量,且不同信号量的等待者队列彼此不抢锁**。

- **251 个桶**:251 是质数(避免和用户地址模式相关)。每个 `Mutex.sema` 地址按 `>>3`(对齐到 8 字节)模 251 落到一个桶。`sema` 是 `uint32`,所以 8 字节对齐天然满足。
- **每个桶独立 `semaRoot.lock`**:`Mutex` A 的等待者操作和 `Mutex` B 的等待者操作(只要落到不同桶)互不加锁。桶平均后,锁竞争被分摊到 251 个锁上。
- **`pad` 缓存行对齐**:`pad [cpu.CacheLinePadSize - ...]byte`——让相邻两个 `semaRoot` 不落在同一个缓存行。否则两个高竞争 Mutex 的 `nwait` 在同一个缓存行里会触发"false sharing"(假共享):一个改 `nwait` 让另一个 CPU 核的缓存行失效,纯属浪费。这是经典的缓存行对齐技巧(全系列内存分配器那本讲过)。

- **`treap`**:每个桶里所有等待者按"等待的地址(`addr`)排成二叉搜索树",但同一地址的多个等待者(sudog)用 `waitlink` 串成一个链表,挂在 treap 的那个节点上([sema.go#L370-L383](../go/src/runtime/sema.go#L370))。为什么用 treap(树堆)而不是普通链表或 map?因为 treap 是"按地址排序 + 按 `ticket` 随机化堆序"的平衡树——既能 O(log n) 找到某地址的等待者,又能自动保持平衡(`s.ticket = cheaprand() | 1` 当堆优先级,插入时往上旋转,见 [queue](../go/src/runtime/sema.go#L381-L394))。treap 的优势是实现比红黑树简单,期望平衡。

> **钉死这件事**:信号量在 runtime 里不是"一个全局锁 + 一个全局链表",而是**251 个独立桶 + 每桶一棵 treap**。这套结构让 Go 的所有锁原语(`sync.Mutex`/`RWMutex`/`WaitGroup`,channel 的 recvq/sendq 也用 sudog)共用一套底层,且高并发下锁竞争被摊薄。`Mutex.sema` 这个 `uint32` 字段,本质是"在 251 桶里找到我那棵 treap"的哈希 key。

---

## 20.9 timer:四叉堆的算法

讲完锁,看 timer。`time.Sleep(ns)` 的 runtime 入口是 [timeSleep](../go/src/runtime/time.go#L335-L370):

```go
// src/runtime/time.go#L335-L370(节选)
func timeSleep(ns int64) {
    if ns <= 0 {
        return
    }
    gp := getg()
    t := gp.timer
    if t == nil {
        t = new(timer)
        t.init(goroutineReady, gp)        // f=goroutineReady, arg=gp 自己
        ...
        gp.timer = t
    }
    when := now + ns
    gp.sleepWhen = when
    ...
    gopark(resetForSleep, nil, waitReasonSleep, traceBlockSleep, 1)  // ★ park
}

func goroutineReady(arg any, _ uintptr, _ int64) {
    goready(arg.(*g), 0)                   // 到期时被调用, 把 gp ready
}
```

注意一个反直觉:**`timeSleep` 先 `gopark` 把自己挂起,真正往堆里塞 timer 是在 `resetForSleep` 里(park 之后)**。为什么?注释 [time.go#L372-L375](../go/src/runtime/time.go#L372) 解释:"如果 sleep 很短、G 又很多,P 可能在 `gopark` 之前就跑 `goroutineReady` 唤醒一个还没 park 的 G"——会出乱子。所以顺序是:先 park(把 G 状态翻成 `_Gwaiting`),park 完成的回调 `resetForSleep` 里再 `t.reset(when, 0)` 把 timer 入堆。这是 park 和 timer 之间的一个微妙顺序约束。

每个 P 有自己的 [`timers`](../go/src/runtime/time.go#L136-L165):

```go
// src/runtime/time.go#L136-L165(节选)
type timers struct {
    mu mutex
    heap []timerWhen         // 按 heap[i].when 排序的最小堆
    len  atomic.Uint32       // len(heap) 的原子副本
    zombies atomic.Int32     // 待删的 timer 数
    ...
    minWhenHeap atomic.Int64 // 堆顶 when, wakeTime 用
    minWhenModified atomic.Int64  // 被 modify 但堆里还没更新的下界
}

type timerWhen struct {
    timer *timer
    when  int64
}

const timerHeapN = 4   // ★ 四叉堆
```

每个 P 一个 `timers`(挂 `p.timers` 字段),timer 在堆里按 `when` 排最小堆(堆顶 `heap[0]` 最先到期)。**为什么每个 P 一份堆,而不是全局一个堆?** 这又是个并发优化:全局一个堆要全局锁,每 P 一份堆只在跨 P 操作(`addtimer` 时 G 被调度到别的 P)才要协调,平时本 P 加/删 timer 是无竞争的。

四叉堆的算法在 [siftUp](../go/src/runtime/time.go#L1317-L1337) / [siftDown](../go/src/runtime/time.go#L1341-L1376):

```go
// src/runtime/time.go#L1317-L1337
func (ts *timers) siftUp(i int) {
    heap := ts.heap
    ...
    tw := heap[i]
    for i > 0 {
        p := int(uint(i-1) / timerHeapN)   // ★ 父节点 = (i-1)/4
        if !tw.less(heap[p]) {
            break
        }
        heap[i] = heap[p]
        i = p
    }
    if heap[i].timer != tw.timer {
        heap[i] = tw
    }
}

// src/runtime/time.go#L1341-L1376(节选)
func (ts *timers) siftDown(i int) {
    heap := ts.heap
    n := len(heap)
    ...
    tw := heap[i]
    for {
        leftChild := i*timerHeapN + 1      // ★ 左子 = i*4+1
        if leftChild >= n {
            break
        }
        w := tw
        c := -1
        for j, tw := range heap[leftChild:min(leftChild+timerHeapN, n)] {  // 在最多 4 个子里找最小
            if tw.less(w) {
                w = tw
                c = leftChild + j
            }
        }
        if c < 0 {
            break
        }
        heap[i] = heap[c]
        i = c
    }
    if heap[i].timer != tw.timer {
        heap[i] = tw
    }
}
```

加 timer 是 [addHeap](../go/src/runtime/time.go#L455-L472):`append` 到末尾,然后 `siftUp(len-1)` 上浮。删堆顶是 [deleteMin](../go/src/runtime/time.go#L522-L540):把末尾换到 `heap[0]`,然后 `siftDown(0)` 下沉。

> **为什么是四叉堆,而不是二叉堆?** 这是个值得专门讲一句的取舍,也是本章的核心技巧之一——见 20.11 技巧精解。

---

## 20.10 timer 和调度器的接口:`checkTimers`

timer 不是独立线程在跑,它**寄生在调度循环里**。第 3 章 3.4.9 讲过,`findRunnable` 末尾 M 决定 park 之前,会用 `pollUntil = checkTimersNoP(...)` 算出"下一个 timer 什么时候到期",然后用这个值当 `netpoll(delay)` 的阻塞超时。`checkTimers` 在 [time.go#L982-L1045](../go/src/runtime/time.go#L982):

```go
// src/runtime/time.go#L982-L1045(节选)
func (ts *timers) check(now int64, bubble *synctestBubble) (rnow, pollUntil int64, ran bool) {
    next := ts.wakeTime()              // ★ 原子读 minWhenHeap / minWhenModified
    if next == 0 {
        return now, 0, false           // 没 timer
    }
    if now == 0 { now = nanotime() }
    ...
    if now < next && !force {
        return now, next, false        // 还没到点, 把 next 报给 netpoll 当 delay
    }

    ts.lock()
    if len(ts.heap) > 0 {
        ts.adjust(now, false)
        for len(ts.heap) > 0 {
            if tw := ts.run(now, bubble); tw != 0 {   // ★ 跑到期 timer
                if tw > 0 { pollUntil = tw }
                break
            }
        }
    }
    ...
}
```

几个机制:

- **`wakeTime` 用两个原子字段算**:`minWhenHeap`(堆顶的 when)和 `minWhenModified`(被 `timerModified` 标记、但堆里位置还没更新的 timer 的下界),取 min。这样**别的 G 改了 timer(比如 `time.Reset`),不用立刻锁 `ts.mu` 改堆**——只 CAS 更新 `minWhenModified`,本 P 的 `checkTimers` 下次进来再批量 `adjust`。把"高频小改"和"低频批处理"解耦,减少锁竞争。
- **`ts.run` 跑到期 timer**:`for` 循环不断从堆顶取(`heap[0]`),如果到期了就 `deleteMin` + 调 `t.f`(`goroutineReady` 把睡的 G `goready`),直到堆顶未到期或堆空。
- **`pollUntil` 报给调用者**:`findRunnable` 拿到 `pollUntil` 后,把它当 `netpoll(delay)` 的超时——这样**M 阻塞在 epoll 上,最多睡到下一个 timer 到期就醒**。timer 和 netpoll 共用一个睡眠点,不需要 timer 单独开线程轮询。

> **钉死这件事**:timer 不是"后台有个线程每毫秒扫一遍堆",而是"寄生在 P 的调度循环里"。每条 M 进 `findRunnable` 末尾,顺手 `checkTimers` 一把——有到期就跑,没到期就把"下一个到期时间"交给 `netpoll` 当超时。Go 的 timer 精度取决于调度器多久进一次 `findRunnable`(通常亚毫秒),且 timer 触发要等某个 P 空下来跑到 `checkTimers`——这就是为什么 `time.Sleep` 不是"实时精确",而是"尽力而为"。

---

## 20.11 技巧精解

这一章最硬核的两个技巧:**Mutex 的双模式切换**和**timer 的四叉堆**。

### 技巧一:Mutex 双模式——动态在吞吐和公平间切线

单模式的 Mutex 都会撞墙:

- **纯 normal(自旋+竞争)**:吞吐高(已在 CPU 上的 G 抢到就跑,无 park/wake),但**长等待者被新来的 G 反复插队,尾延迟爆炸**(Go 1.9 之前的老 Mutex 就是这个病)。
- **纯 starvation(handoff)**:公平,但**每次 Unlock 都要 `goyield` 让出时间片给被 handoff 的 G,等于强制一次上下文切换**——低竞争场景白白付出 park/wake 代价,吞吐掉一大截。

Go 的解法是**让两种模式共存,按"等待者已经等了多久"动态切换**:

```
 等待时间
   ↑
   │
   │  ← starvation(等 ≥ 1ms): handoff, 保尾延迟
   │  ────────────────────────  ← 阈值 starvationThresholdNs = 1e6
   │  ← normal(等 < 1ms): 自旋+竞争, 保吞吐
   │
   └──────────────────────────────→
```

切换点在两个地方(`lockSlow` 里 `starving` 变量更新 + CAS 置 `mutexStarving` 位;`Unlock` 看到饥饿位直接 `Semrelease(handoff=true)`)。退出点在被 handoff 的等待者拿到锁后(如果它自己等得不长或后面没人等了)。

这个设计的精妙处在于:**正常负载下几乎一直在 normal 模式(便宜),只有真的饿惨了才付 starvation 的代价(贵但保公平)**。阈值 1ms 是个经验值——它对应"几百次 park/wake 的累计开销",意思是"等这么久,已经远超一次切换的代价了,值得切到 handoff"。

> **反面对比**:Java 的 `synchronized` 走的是另一种自适应(偏向锁 → 轻量级锁 → 重量级锁),按"竞争激烈程度"升级,但它的"重量级锁"是 park/wake 的传统 monitor,没有显式的"按等待时间切公平"这一档。Go 的"双模式 + 时间阈值"是更直接地针对"尾延迟"这个具体痛点设计的——Go 1.9 加这个特性时,官方 benchmark 显示 P99 延迟降了一个数量级。这是把"公平性"当成可量化的延迟指标来工程化,而不是当作道德口号。

### 技巧二:为什么 timer 用四叉堆而不是二叉堆

timer 堆本质是个**最小堆**(堆顶是最早到期的 timer)。最小堆的常见实现是二叉堆(每个节点 2 个子),Go 偏偏用 `timerHeapN = 4` 的四叉堆。这不是拍脑袋,是有具体理由的:

**理由一:堆高度更低**。N 个节点的 d 叉堆,高度是 `log_d(N)`。N=10000 时:
- 二叉堆:`log_2(10000) ≈ 13.3`
- 四叉堆:`log_4(10000) ≈ 6.6`

`siftUp`/`siftDown` 的时间复杂度是 O(高度 × 每层比较次数)。二叉堆每层 1 次比较,总 ~13 次比较;四叉堆每层最多 3 次比较(在 4 个子里找最小),总 `6.6 × 3 ≈ 20` 次比较——**比较次数反而更多**?那为什么还用四叉?

**理由二(关键):缓存友好**。`siftDown` 在每层要访问 `i*d+1` 到 `i*d+d` 这 `d` 个子节点。二叉堆访问 2 个子(跨度 1),四叉堆访问 4 个子(跨度 3)。但**关键是父子的跨度**:

```
 二叉堆(父子跨度 2i):
 heap[0] 的子在 [1, 2],跨 1 个槽
 heap[1] 的子在 [3, 4]
 → 树高 13,每次 sift 要访问 13 个分散的位置

 四叉堆(父子跨度 4i):
 heap[0] 的子在 [1,2,3,4],连续 4 个槽
 heap[1] 的子在 [5,6,7,8],也连续
 → 树高 6.6,每次 sift 只访问 ~7 个位置,且每层的 d 个子是连续的
```

四叉堆每层的 `d` 个子**在内存里连续**(从 `i*4+1` 到 `i*4+4`),一次缓存行加载(64 字节,正好装 ~5 个 `timerWhen`,每个 16 字节)就能把一层全读进来。整体访问的**缓存行数**显著少于二叉堆(后者每层一个子就要一次潜在的 cache miss)。在现代 CPU 上,缓存 miss 比多几次比较贵一两个数量级——这就是 d 叉堆(典型 d=4)在堆场景里胜过二叉堆的根因。

**理由三:`parent = (i-1)/d` 计算更省**。二叉堆 `parent = (i-1)/2` 要一次移位,四叉堆 `parent = (i-1)/4` 也是一次移位(`>> 2`)——持平。但 Go 的源码写的是 `int(uint(i-1) / timerHeapN)`([time.go#L1327](../go/src/runtime/time.go#L1327) 和 [time.go#L1244](../go/src/runtime/time.go#L1244)),编译器把常量 `timerHeapN=4` 优化成移位,零开销。

**为什么不是更大的 d(比如 8 叉、16 叉)?** 因为 d 太大,每层要扫的子变多,比较次数线性增长(每层 d-1 次比较),且"在 d 个子里找最小"的循环本身有开销。d=4 是个经验最优:堆高度减半、缓存友好、比较次数增长可控。事实上 Linux 内核的 timer 也用类似的思路(它用的是层级时间轮,但小规模定时器管理常选 d=4 的堆)。

> **反面对比**:朴素地用二叉堆,Go 1.10 之前 timer 确实是四叉堆的"前身"——更早版本是个 64 路的 bucket 数组(每个桶管一个 64 位字的一位,见老 `time.go`),后来才演进成四叉堆。Tokio 用的是 hashed timing wheel(层级时间轮,O(1) 插入/到期),适合海量短定时器;Go 选四叉堆是因为 timer 数量通常不大(几千到几十万),堆的 O(log n) 完全够用,且实现简单、缓存局部性好。两种设计各有适用域——见双璧对照。

---

## ★ 双璧对照《Tokio》

| 维度 | Go runtime | Tokio |
|---|---|---|
| 互斥锁 | `sync.Mutex`(自旋 + 信号量 + park;normal/starvation 双模式) | `tokio::sync::Mutex`(纯 async,await 时不占线程;无自旋无 starvation) |
| 锁的实现层次 | runtime + sync 包协作(`gopark`/`goready` 接调度器) | 纯库,Future::poll 返回 Pending + waker 唤醒 |
| 定时器数据结构 | 每 P 一个四叉堆(`timerHeapN = 4`) | hashed timing wheel(层级时间轮) |
| 定时器精度 | 依赖调度器进 `checkTimers` 的频率(亚毫秒) | wheel 的 tick 粒度(可配,通常毫秒级) |
| 大量短定时器 | 四叉堆 O(log n) 插入/弹出,wheel 在海量场景更优 | wheel O(1) 插入/到期,海量短定时器更省 |

Go 的 Mutex 因为是"阻塞式 API + runtime 让出",必须靠自旋+信号量把"阻塞的 G 不阻塞 M"做出来;Tokio 的 Mutex 是 async 的,`await` 时天然让出,不需要 runtime 介入。Go 的 timer 因为寄生在调度循环里,堆够用且实现简单;Tokio 的 timer 面向"海量并发任务的超时",层级时间轮的 O(1) 在百万级定时器时占优。同目标(管定时),两套解,各适其域。

---

## 章末小结

这一章把两个天天用的原语拆到底。回到二分法:**Mutex 和 timer 服务"调度执行"**——它们都依赖调度器:`Mutex` 的 park 走 `gopark` 把 G 交给调度器、`Unlock` 的唤醒走 `goready` 把 G 塞回 runq;timer 的到期靠 `checkTimers` 在 `findRunnable` 末尾被调用、靠 `netpoll` 的超时把 M 唤醒。它们不是"绕过调度器的旁路",而是"长在调度器上的两个口子"。

### 五个"为什么"清单

1. **为什么 `Mutex.state` 一个 int32 塞下锁位/唤醒位/饥饿位/等待者数?** 所有状态一次 CAS 原子改,这是无锁快路径的前提。分成多字段就得加锁或多次 CAS,慢得多。
2. **为什么 Mutex 要自旋,且有 `runtime_canSpin` 三条护栏?** 自旋赌"持有者马上放"省 park/wake 切换,但无条件自旋会烧穿 CPU。三条护栏(多核、有别的忙 P、本地 runq 空)保证只在"自旋有意义"时自旋。`PAUSE` 指令降超线程危害。
3. **为什么有 normal/starvation 双模式?** 纯 normal 尾延迟爆炸(新 G 反复插队),纯 starvation 吞吐掉(每次 Unlock 强制切换)。双模式 + 1ms 阈值动态切线:正常负载便宜,真饿惨了才付 handoff 代价。
4. **信号量为什么是 251 桶 + treap?** 251 桶摊锁竞争(缓存行对齐防 false sharing),treap 按 addr 排序 + ticket 随机化堆序,O(log n) 找等待者且自动平衡。所有锁原语共用。
5. **timer 为什么用四叉堆,每个 P 一份?** 四叉堆高度低、缓存友好(每层 4 子连续);每 P 一份堆避免全局锁。timer 寄生在 `checkTimers` 里,和 netpoll 共用一个睡眠点,不需要独立线程。

### 想继续深入往哪钻

- **源码文件**:Mutex 主体 [`../go/src/internal/sync/mutex.go`](../go/src/internal/sync/mutex.go)(`Lock`@61 / `lockSlow`@95 / `Unlock`@187 / `unlockSlow`@202);对外封装 [`../go/src/sync/mutex.go`](../go/src/sync/mutex.go);自旋护栏 [`../go/src/runtime/proc.go`](../go/src/runtime/proc.go) 的 `internal_sync_runtime_canSpin`@7983 / `doSpin`@8004;`PAUSE` 汇编 [`../go/src/runtime/asm_amd64.s`](../go/src/runtime/asm_amd64.s#L832)。信号量 [`../go/src/runtime/sema.go`](../go/src/runtime/sema.go)(`semacquire1`@146 / `semrelease1`@207 / `semaRoot.queue`@304 / `semtable`@51)。timer [`../go/src/runtime/time.go`](../go/src/runtime/time.go)(`timeSleep`@335 / `addHeap`@455 / `deleteMin`@522 / `check`@982 / `siftUp`@1317 / `siftDown`@1341 / `timerHeapN`@1305)。
- **观测**:`go tool trace` 能看到每次 `Mutex` 阻塞的 `traceBlockSync` 事件和等待时长(P99 长就是 starvation 触发了);`runtime/pprof` 的 mutex profile(`pprof.Lookup("mutex")`)能看到锁竞争热点。timer 用 `GODEBUG=schedtrace=1000` 看 P 的 timer 数。
- **延伸**:Go 1.9 加 starvation 模式的原始 proposal [issue 13099](https://go.dev/issue/13099);Go 1.14 把 timer 从全局堆改成每 P 堆的 [issue 64242](https://go.dev/issue/64242) 系列;treap 论文 [Aragon & Seidel](https://faculty.washington.edu/aragon/pubs/rst89.pdf)(sema.go 注释里引用);Dijkstra 经典信号量论文。
- **对照第 3 章**:`findRunnable` 末尾 `checkTimersNoP` 和 `netpoll(delay)` 的衔接(3.4.9),是本章 timer 触发的入口;`gopark`/`goready` 的机制(3.5 `mcall`/`gogo`),是本章 Mutex park 的底层。

### 引出下一章

这是全书第 20 章,也是最后一章正文。Mutex 和 timer 把"sync 原语怎么往调度器上接"讲清了——它们都是 `gopark`/`goready` 这对调度器接口的使用者。下一章(第 21 章)是收束:我们把全书 20 章串成一张"Go runtime 哲学"的总图——语言内置 vs 库级、协作+抢占混合、有 GC 的取舍、分层缓存换无锁快路径、STW 最小化的执念——并给一张**双璧对照总表**,把 Go runtime 和 Tokio 钉在一起,作为这本书和《Tokio》的合订索引。

---

> 全书定位:第 20 章 / 第 7 篇 sync 原语与 timer(收束篇)。源码版本 Go 1.27(本地 master @ `6d1bcd10`,Version 常量见 `src/internal/goversion/goversion.go`)。本章源码核对:`sync.Mutex` 实现已迁到 `internal/sync/mutex.go`(对外 `sync.Mutex` 只是 `isync.Mutex` 的薄封装,1.21 之后);`runtime_canSpin`/`doSpin` 在 1.27 拆成 `internal_sync_runtime_canSpin`/`sync_runtime_canSpin` 两层(linkname 兼容);`active_spin=4` / `active_spin_cnt=30` 在 `lock_spinbit.go`(不是老的 `lock_sema.go`);timer 在 1.27 是新 `timer`/`timers` 结构(`type timers struct` + `siftUp`/`siftDown` 方法 + `timerHeapN=4` 常量),不是更早版本的 `addtimer`/`deltimer`/`runtimer` 全局函数版。下一章:P8-21 哲学 + 双璧总表。
