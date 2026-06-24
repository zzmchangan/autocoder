# 第十六讲 · sweep:清扫与回收

> 篇:第 4 篇 · 并发 GC:三色标记(支撑地基)
> 主线呼应:上一讲我们把并发标记阶段的"标得完"讲透了——后台 25% CPU 加上 mark assist 反压,堆涨多快 GC 进度都咬得住。但标记完只把活对象认了出来,垃圾还实打实地占着堆。这一讲回答的是另一半:**标记完了,垃圾怎么回收?** 答案就是 sweep(清扫)。runtime 不会在 STW 里扫一遍堆——那违背亚毫秒承诺;它把清扫摊到"业务运行期间",靠两条路一起干:一个低优先级后台 goroutine `bgsweep` 悠着扫,以及分配路径上的"按需清扫"(谁要 span 谁顺手扫几个)。这一讲的全部精巧,都在 lazy sweep 这个设计里:为什么清扫不在 STW、allocBits 和 gcmarkBits 怎么交接、sweepgen 双缓冲怎么让"上一轮还在扫"和"下一轮已经开始标"不打架、清扫一个 mspan 为什么只需要一次 CAS。

## 核心问题

**标记完之后,堆里那些没人指向的对象怎么回收?sweep 凭什么不在 STW 里、还能让分配者随时拿到干净可用的 span?**

读完本章你会明白:

1. sweep 的两个层次:**对象回收器**(object reclaimer,在 mspan 内部把没人标的 slot 认成自由)和 **span 回收器**(span reclaimer,把整页没人用的 mspan 还给 mheap)。两者最终都汇到 `mspan.sweep` 这一个函数,差别在驱动方式——后台批量扫 vs 分配路径按需扫。
2. lazy sweep 的本质:清扫不在 GC 周期里单独占一段 STW,而是**摊到下一次分配**。`mcentral.cacheSpan` 要 span 时,先从"已清扫"列表拿,没有就顺手扫一个"未清扫"的;`deductSweepCredit` 在大块分配前按比例补扫。GC 周期结束只翻一下 `sweepgen`,真正的清扫发生在业务跑的时候。
3. allocBits/gcmarkBits 的交接:`gcmarkBits`(本轮标记谁活着)在清扫时变成新的 `allocBits`(下一轮分配时谁算已占),`gcmarkBits` 自己换一份清零的新图。这一步把"标记结果"无损翻译成"分配视图",下一轮 GC 重新累积标记。
4. sweepgen 双缓冲:`mheap_.sweepgen` 每次 GC 加 2,mspan 的 `sweepgen` 有五态(`h-2`=待扫、`h-1`=扫中、`h`=扫完、`h+1`=缓存未扫、`h+3`=缓存已扫);mcentral 的 `partial[2]`/`full[2]` 两个 spanSet 按 `sweepgen/2%2` 切换角色,让上一轮的清扫和下一轮的标记可以**时间上重叠**而不串台。
5. 为什么这套机制 sound:每个 mspan 的清扫靠 sweepgen 的 CAS 串行化(一次只有一个 sweeper 拿到所有权),清扫里改 allocBits/gcmarkBits 在 sweepgen 推进前对别的执行流不可见(它们看到的还是旧图);周期切换在 STW 里 `mheap_.sweepgen += 2` 是全局屏障点,保证"翻页"瞬间所有 mspan 状态一致。

> **逃生阀**:如果只想记一句话,记住——**sweep = 把 gcmarkBits(谁活着)翻译成 allocBits(谁占用),把没人占的 slot(和没人占的整页)还给分配器,全程不在 STW 里,靠 lazy sweep + sweepgen 双缓冲让"扫上一轮"和"标下一轮"并发不冲突**。`mspan.sweep` 是单一汇合点,后台 `bgsweep` 和分配路径 `cacheSpan` 都调它,靠 sweepgen 的 CAS 保证一个 mspan 同时只有一个清扫者。

---

## 16.1 一句话点破

> **朴素想法是"标记完 → STW 扫一遍堆 → 把垃圾清掉 → 放业务"。Go 不这么干——标记完只在 STW 里翻一下代纪(sweepgen += 2),真正的清扫发生在业务恢复运行之后:后台 goroutine 悠着扫,分配路径要 span 时顺手扫。这样清扫的工作量被摊到"业务运行期间"的空闲 CPU 上,GC 周期结束的停顿就只剩翻代纪那一下(亚毫秒)。代价是清扫可能拖到下一个 GC 周期才开始甚至还没扫完——但 sweepgen 双缓冲保证"上一轮没扫完"和"下一轮已经开始标"在数据结构上各占一格,互不踩踏。**

这是结论,不是理由。本章倒过来拆:先看朴素"STW 全量清扫"会撞什么墙,再看 sweep 的两层结构(对象回收器 + span 回收器),然后拆 lazy sweep 的两个驱动源(bgsweep + cacheSpan/deductSweepCredit),接着拆 allocBits/gcmarkBits 交接和 sweepgen 五态双缓冲,最后把最硬的两个技巧(lazy sweep 的摊销、sweepgen CAS 串行化)单独拆透。

---

## 16.2 朴素方案:STW 全量清扫为什么会拖垮停顿

并发 GC 的标记阶段(第 14、15 讲)把"哪些对象活着"认了出来——每个活对象在它的 `gcmarkBits` 里被置了 1。下一步逻辑上很直白:**把 gcmarkBits 是 0 的对象(垃圾)回收,内存还给分配器**。问题是这件事在什么时候做。

最朴素的方案是:**标记完 → 进入一段 STW → 在 STW 里把整个堆扫一遍,认垃圾、清 bitmap、归还页 → 放业务**。这就是早期 Go(Go 1.3 之前)的 STW 清扫,也是 Java 早期分代 GC 的常见做法。

> **不这样会怎样**:朴素 STW 清扫的停顿和**堆大小成正比**。一个 10 GB 堆,扫一遍 bitmap(每 8 字节对象 1 bit,bitmap 约 150 MB)要遍历几十万 mspan、改几十万 bitmap 指针、归还几百万页,毫秒级根本压不下来——几百毫秒到秒级都常见。堆越大停顿越长,这和 Go "亚毫秒 STW"的承诺直接冲突。标记可以并发(第 14 讲的 mark worker + assist),但清扫如果放 STW,就把它从并发 GC 退化回了 STW GC,前面所有"标记并发"的努力都白费。

Go 从 1.5 开始把清扫也搬出 STW:**标记完只在 STW 里翻一下代纪(`mheap_.sweepgen += 2`),真正的清扫在业务恢复运行后慢慢做**。这条路径在 [`mgc.go:1404`](../go/src/runtime/mgc.go#L1404) 启动:

```go
// src/runtime/mgc.go #L1402-L1405(在 mark termination 的 STW 里)
// marking is complete so we can turn the write barrier off
setGCPhase(_GCoff)
stwSwept = gcSweep(work.mode)
```

`gcSweep` 本身极短 [`mgc.go:2065`](../go/src/runtime/mgc.go#L2065):

```go
// src/runtime/mgc.go #L2065-L2081(骨架)
func gcSweep(mode gcMode) bool {
    assertWorldStopped()

    if gcphase != _GCoff {
        throw("gcSweep being done but phase is not GCoff")
    }

    lock(&mheap_.lock)
    mheap_.sweepgen += 2          // 翻代纪:全部 mspan 瞬间从"扫完"变"待扫"
    sweep.active.reset()
    mheap_.pagesSwept.Store(0)
    ...
    unlock(&mheap_.lock)

    sweep.centralIndex.clear()
    ...
}
```

注意 `gcSweep` 干的全部"重活"就是:**`mheap_.sweepgen += 2` 一次原子加,reset 几个计数器,clear centralIndex**。它不扫任何 mspan、不改任何 bitmap。这就是 sweep 阶段在 STW 里付出的几乎全部代价——一次原子加 + 几次计数器清零,亚微秒级。真正的清扫被推迟到 STW 之外。

> **钉死这件事**:Go 的 sweep 在 STW 里只做一件事——翻代纪(`sweepgen += 2`),把"这一轮已标记"的快照封存,把"待清扫"的位图交给并发清扫者。清扫本身(b遍历 bitmap、归还页)是纯异步的,和业务跑在同一个世界里。这是 sweep 能做到零额外 STW 的物理基础。

但"翻完代纪就放业务"带来一个新问题:**业务一放出来就会分配内存,而堆上还满是上一轮没清扫的垃圾,分配者拿什么 span?** 这就是 lazy sweep 要回答的——清扫必须能在业务运行时被**按需触发**,谁要 span 谁就顺手扫。

---

## 16.3 sweep 的两层结构:对象回收器与 span 回收器

[`mgcsweep.go`](../go/src/runtime/mgcsweep.go) 开头的文件注释把整个 sweep 拆得很清楚 [`mgcsweep.go:7-23`](../go/src/runtime/mgcsweep.go#L7-L23):

```
// The sweeper consists of two different algorithms:
//
// * The object reclaimer finds and frees unmarked slots in spans. It
//   can free a whole span if none of the objects are marked, but that
//   isn't its goal. This can be driven either synchronously by
//   mcentral.cacheSpan for mcentral spans, or asynchronously by
//   sweepone, which looks at all the mcentral lists.
//
// * The span reclaimer looks for spans that contain no marked objects
//   and frees whole spans. ... The entry point for this is
//   mheap_.reclaim and it's driven by a sequential scan of the page
//   marks bitmap in the heap arenas.
//
// Both algorithms ultimately call mspan.sweep, which sweeps a single
// heap span.
```

两层,各自的目标和驱动方式不同:

| | 对象回收器(object reclaimer) | span 回收器(span reclaimer) |
|---|---|---|
| **目标** | 在一个 mspan 内部,把没标记的 slot 认成自由(可能整 span 都自由) | 找到整页都没人标的 mspan,把整 span 还给 mheap |
| **粒度** | per-slot(一个 mspan 内部) | per-span(整页 8 KB 起) |
| **驱动** | `mcentral.cacheSpan`(分配路径同步)+ `sweepone`(后台异步) | `mheap_.reclaim`(大对象分配路径扫描 page bitmap) |
| **入口** | 都汇到 [`mspan.sweep`](../go/src/runtime/mgcsweep.go#L505) | 也走 `mspan.sweep` |

为什么分两层?因为**两种回收的紧迫性不同**:

- **slot 级回收(对象回收器)**最频繁——小对象分配是程序里最高频的操作,分配者随时要从 mcentral 拿"有自由 slot 的 span",所以这个回收必须**和分配同节奏**,谁分配谁顺手扫。这就是 lazy sweep 的主战场。
- **span 级回收(span 回收器)**只在"整页都空了、可以还给 mheap 给大对象用"时才有意义。大对象分配走 mheap 直接要页,这时 mheap 才需要扫 page bitmap 找完全空的页归还。这是为大对象分配服务的,频率低得多。

两层最终都汇到同一个 `mspan.sweep`(`mgcsweep.go:505`),它一次性把"清 slot bitmap + 判断整 span 是否全空 + 该归还页就归还"全干了。所以从源码看,核心就一个函数;两个"算法"的差别只在**谁来调用它、调多少次**。

> **钉死这件事**:sweep 不是"扫一遍堆"这种朴素动作,而是"按 mspan 为单位、按需触发"的一组调用。每个 mspan 被扫一次,扫完它要么还在用(slot 部分自由,回 partial 列表)、要么全空回 mheap、要么全满回 full 列表。驱动源有两个(分配路径 + 后台 goroutine),靠 sweepgen CAS 保证不重复扫。

---

## 16.4 lazy sweep 的两个驱动源:谁要 span 谁扫

lazy sweep 的精髓是"清扫不主动,按需触发"。两个驱动源:

### 驱动一:后台 goroutine `bgsweep`

Go runtime 在初始化时就起了一个后台 goroutine 专门扫 [`mgc.go:211`](../go/src/runtime/mgc.go#L211):

```go
// src/runtime/mgc.go #L207-L216
// gcenable is called after the bulk of the runtime initialization ...
// It kicks off the background sweeper goroutine, the background
// scavenger goroutine, and enables GC.
func gcenable() {
    c := make(chan int, 2)
    go bgsweep(c)
    go bgscavenge(c)
    <-c
    ...
}
```

`bgsweep` 的循环 [`mgcsweep.go:272`](../go/src/runtime/mgcsweep.go#L272):

```go
// src/runtime/mgcsweep.go #L281-L325(骨架)
for {
    // bgsweep attempts to be a "low priority" goroutine by intentionally
    // yielding time. It's OK if it doesn't run, because goroutines allocating
    // memory will sweep and ensure that all spans are swept before the next
    // GC cycle. ...
    const sweepBatchSize = 10
    nSwept := 0
    for sweepone() != ^uintptr(0) {
        nSwept++
        if nSwept%sweepBatchSize == 0 {
            goschedIfBusy()
        }
    }
    ...
    lock(&sweep.lock)
    if !isSweepDone() {
        unlock(&sweep.lock)
        goschedIfBusy()
        continue
    }
    sweep.parked = true
    goparkunlock(&sweep.lock, waitReasonGCSweepWait, traceBlockGCSweep, 1)
}
```

注意三件事:

1. **"low priority"自述**:注释明说 bgsweep "attempts to be a low priority goroutine by intentionally yielding time"。它抢不到 CPU 没关系——分配路径的按需清扫会兜底("goroutines allocating memory will sweep and ensure that all spans are swept before the next GC cycle")。bgsweep 的定位是"业务空闲时把清扫提前做完,省得分配时才扫造成延迟",它是优化项不是必需项。
2. **批量 + 让出**:`sweepBatchSize = 10`,扫 10 个 mspan 才 `goschedIfBusy()` 一次。注释解释了为什么不是扫一个就让一次——扫一个 mspan 只要 ~30 ns,频繁 `Gosched` 会产生海量 trace 事件(曾经占 50%),而且调度本身有开销。攒 10 个再让,平衡了"不抢业务"和"不让调度开销反吃吞吐"。
3. **扫完就 park**:`sweepone()` 返回 `^uintptr(0)`(没东西可扫了),`isSweepDone()` 也确认扫完,就 `goparkunlock` park 起来,等下一个 GC 周期 `gcSweep` 时被唤醒(通过 `sweep.active.reset()` 之后的状态变化)。

### 驱动二:分配路径按需清扫(`mcentral.cacheSpan` + `deductSweepCredit`)

这才是 lazy sweep 真正的主战场。当一个小对象分配从 mcache 没拿到(本地缓存空了),会上提到 mcentral 要 span,这时 [`mcentral.cacheSpan`](../go/src/runtime/mcentral.go#L82) 干的第一件事就是"先扫再拿":

```go
// src/runtime/mcentral.go #L82-L85
func (c *mcentral) cacheSpan() *mspan {
    // Deduct credit for this span allocation and sweep if necessary.
    spanBytes := uintptr(gc.SizeClassToNPages[c.spanclass.sizeclass()]) * pageSize
    deductSweepCredit(spanBytes, 0)
    ...
```

`deductSweepCredit` 是 proportional sweep(按比例清扫)的核心 [`mgcsweep.go:913`](../go/src/runtime/mgcsweep.go#L913):

```go
// src/runtime/mgcsweep.go #L913-L966(骨架)
// deductSweepCredit is the core of the "proportional sweep" system.
func deductSweepCredit(spanBytes uintptr, callerSweepPages uintptr) {
    if mheap_.sweepPagesPerByte == 0 {
        // Proportional sweep is done or disabled.
        return
    }
    ...
retry:
    sweptBasis := mheap_.pagesSweptBasis.Load()
    live := gcController.heapLive.Load()
    liveBasis := mheap_.sweepHeapLiveBasis
    newHeapLive := spanBytes
    if liveBasis < live {
        newHeapLive += uintptr(live - liveBasis)
    }
    pagesTarget := int64(mheap_.sweepPagesPerByte*float64(newHeapLive)) - int64(callerSweepPages)
    for pagesTarget > int64(mheap_.pagesSwept.Load()-sweptBasis) {
        if sweepone() == ^uintptr(0) {
            mheap_.sweepPagesPerByte = 0
            break
        }
        if mheap_.pagesSweptBasis.Load() != sweptBasis {
            // Sweep pacing changed. Recompute debt.
            goto retry
        }
    }
}
```

这段是 mark assist(第 15 讲)在清扫侧的镜像——**把"清扫进度"和"分配字节"按比例耦合**。`sweepPagesPerByte` 是 `gcPaceSweeper` 算出来的"每分配 1 字节要扫多少页"的比例 [`mgcsweep.go:1010`](../go/src/runtime/mgcsweep.go#L1010):

```go
// src/runtime/mgcsweep.go #L982-L1017(节选)
func gcPaceSweeper(trigger uint64) {
    ...
    pagesSwept := mheap_.pagesSwept.Load()
    pagesInUse := mheap_.pagesInUse.Load()
    sweepDistancePages := int64(pagesInUse) - int64(pagesSwept)
    if sweepDistancePages <= 0 {
        mheap_.sweepPagesPerByte = 0
    } else {
        mheap_.sweepPagesPerByte = float64(sweepDistancePages) / float64(heapDistance)
        mheap_.sweepHeapLiveBasis = heapLiveBasis
        mheap_.pagesSweptBasis.Store(pagesSwept)
    }
}
```

物理含义:清扫必须在"堆从当前 heapLive 涨到下一次 GC trigger"这段距离里,把所有在用页扫完。把"剩余要扫的页数"除以"剩余堆字节配额",得到 `sweepPagesPerByte`。分配者每分配 1 字节,就欠 sweep `sweepPagesPerByte` 页的债——`deductSweepCredit` 在分配前补扫(`pagesTarget > pagesSwept - sweptBasis` 就 `sweepone()`)。

> **不这样会怎样**:如果分配路径不按比例清扫,只靠 bgsweep 悠着扫,会发生两件坏事:(a) 分配猛时 bgsweep 跟不上,堆里堆满"未清扫"的 mspan,分配者从 mcentral 拿 span 时不得不就地扫一个,造成**分配延迟尖刺**;(b) 更糟的是,下一个 GC 周期触发时(`finishsweep_m` 在 mark 开始前的 STW 里调用 [`mgc.go:852`](../go/src/runtime/mgc.go#L852))还没扫完,就得在 STW 里把剩下的扫完——这就退化回了 STW 清扫。proportional sweep 把"扫的速率"绑在"分配的速率"上,**谁分得多谁扫得多**,保证在下一个 GC 周期到来前清扫必然完成。

这和第 15 讲的 mark assist 是**完全同构**的设计——都是"把 GC 工作和分配速率耦合,谁分配谁出力"。区别只是:mark assist 防的是"标不完堆涨过 heapGoal",proportional sweep 防的是"扫不完拖到下一周期 STW"。两者合起来,把 GC 的"标记"和"清扫"两半都钉死在分配速率上,保证 GC 永远跟得上业务。

### `cacheSpan` 里真正的"顺手扫"

`deductSweepCredit` 只是按比例补扫一遍(`sweepone` 全局扫)。`cacheSpan` 自己还会**就地扫一个未清扫的 span 拿来用** [`mcentral.go:112`](../go/src/runtime/mcentral.go#L112):

```go
// src/runtime/mcentral.go #L112-L162(骨架)
// Try partial swept spans first.
sg := mheap_.sweepgen
if s = c.partialSwept(sg).pop(); s != nil {
    goto havespan
}

sl = sweep.active.begin()
if sl.valid {
    // Now try partial unswept spans.
    for ; spanBudget >= 0; spanBudget-- {
        s = c.partialUnswept(sg).pop()
        if s == nil {
            break
        }
        if s, ok := sl.tryAcquire(s); ok {
            // We got ownership of the span, so let's sweep it and use it.
            s.sweep(true)
            sweep.active.end(sl)
            goto havespan
        }
        // We failed to get ownership ... ignore it.
    }
    // Now try full unswept spans, sweeping them ...
    for ; spanBudget >= 0; spanBudget-- {
        s = c.fullUnswept(sg).pop()
        ...
        if s, ok := sl.tryAcquire(s); ok {
            s.sweep(true)
            freeIndex := s.nextFreeIndex()
            if freeIndex != s.nelems {
                s.freeindex = freeIndex
                sweep.active.end(sl)
                goto havespan
            }
            c.fullSwept(sg).push(s.mspan)
        }
    }
    sweep.active.end(sl)
}
```

`cacheSpan` 的找 span 优先级是一条**四级回退链**(和第 3 讲 `findRunnable` 的多级回退同构):

1. **partialSwept(已清扫且有自由 slot)**:直接 pop 拿走,零清扫成本——这是最理想路径,说明 bgsweep 或上一次分配已经把 span 扫好了。
2. **partialUnswept(未清扫但有自由 slot)**:pop 出来,`tryAcquire` 抢所有权(靠 sweepgen CAS),抢到就 `s.sweep(true)` 就地扫,扫完直接用。
3. **fullUnswept(未清扫且原本全满)**:pop 出来扫,扫完如果有 slot 自由了(原来全满,标记后有些死了)就用,没有就推回 fullSwept。
4. **都没有 → `c.grow()`** 找 mheap 要新页。

注意 `spanBudget = 100` 这个阈值 [`mcentral.go:107`](../go/src/runtime/mcentral.go#L107):最多扫 100 个 span 还找不到就用 `c.grow()` 直接要新页。注释说"By setting this to 100, we limit the space overhead to 1%"——意思是"宁可多分配一点新 span(空间开销 1%),也别让分配者在扫 span 上卡太久"。这是**延迟 vs 空间**的取舍:lazy sweep 的本意是省 STW,但不能为了省空间让分配延迟失控。

> **钉死这件事**:lazy sweep 不是单一机制,而是"后台悠着扫 + 分配按比例补扫 + 就地扫一个拿来用"三层叠加。三层各有分工:bgsweep 是基线(业务闲时提前扫),proportional sweep 是反压(分配猛时强制补扫),`cacheSpan` 就地扫是兜底(拿 span 顺便扫)。三层共同保证"分配者永远能很快拿到干净 span,同时清扫不会拖到下一个 GC 周期"。

---

## 16.5 allocBits 与 gcmarkBits 的交接:把"标记结果"翻译成"分配视图"

sweep 最核心的一步,是把本轮标记的结果(`gcmarkBits`)翻译成下一轮分配的依据(`allocBits`)。这一步在 `mspan.sweep` 里 [`mgcsweep.go:695`](../go/src/runtime/mgcsweep.go#L695):

```go
// src/runtime/mgcsweep.go #L695-L706
// gcmarkBits becomes the allocBits.
// get a fresh cleared gcmarkBits in preparation for next GC
s.allocBits = s.gcmarkBits
s.gcmarkBits = newMarkBits(uintptr(s.nelems))

// refresh pinnerBits if they exists
if s.pinnerBits != nil {
    s.refreshPinnerBits()
}

// Initialize alloc bits cache.
s.refillAllocCache(0)
```

要理解这三行,先得看清 allocBits 和 gcmarkBits 各自的语义(字段注释在 [`mheap.go:468-492`](../go/src/runtime/mheap.go#L468-L492)):

- **allocBits**:分配视图。"这个 slot 是否已被分配"。分配者用它找自由 slot——`allocBits[n]==0` 表示 slot n 自由可分。
- **gcmarkBits**:标记视图。"这个 slot 在本轮 GC 里是否被标为活"。GC 标记阶段往里写 1。

两者的关系是这样的:**本轮 GC 开始时,gcmarkBits 是空的(全 0),allocBits 是上一轮清扫完的状态(记录"分配视图"里谁占着)。标记阶段把所有活对象的 gcmarkBits 置 1。标记结束时,gcmarkBits 的 1 就是"活对象"——也就是"下一轮开始时,这些 slot 仍然占用"。所以 gcmarkBits 天然就是下一轮的 allocBits。**

这就是 `s.allocBits = s.gcmarkBits` 的含义——**指针赋值,把标记图直接当成新的分配图**。一次赋值,O(1),没有遍历。`gcmarkBits` 自己换成一份新的清零图(`newMarkBits`),准备下一轮标记往里写。

> **不这样会怎样**:朴素实现会"扫一遍 gcmarkBits,把 1 的 slot 在 allocBits 里也置 1"。这是 O(nelems) 的遍历,而且要改 bitmap 字节。对一个几万元素的 mspan,这就是几千次内存写——乘以整个堆的 mspan 数,就是朴素 STW 清扫的代价。Go 的 O(1) 指针赋值把这个开销直接归零:标记阶段已经把"活对象"信息写进了 gcmarkBits,清扫只需把它的"角色"从"标记图"改成"分配图",物理数据不用动。

这里有个反直觉的点:**allocBits 和 gcmarkBits 是两份独立的 bitmap,物理上各占一块内存,清扫时只是交换指针**。为什么不复用同一份?因为**它们在时间上重叠**——本轮 GC 还在标记(往 gcmarkBits 写)的时候,分配路径还在用 allocBits 找自由 slot 分配新对象。如果共用一份,标记写 1 会和分配读/写抢同一块内存,而且新分配的对象(本轮 GC 期间分配的)需要被当作"已分配"对待(它在 allocBits 里是 1),但又不能被本轮 GC 当成"上一轮就活的"去追溯标记(那样会漏标它指向的对象)——写屏障会单独处理新分配对象(第 13 讲),不靠 gcmarkBits。两份图分开,语义清晰,且并发安全。

字段注释 [`mheap.go:468-492`](../go/src/runtime/mheap.go#L468-L492) 还提到一个细节:这些 bitmap 的底层内存来自**三个 arena(free / next / current / previous)**,按 GC 周期轮转——`finishsweep_m` 把 previous 移到 free、current 移到 previous、next 移到 current [`mgcsweep.go:226`](../go/src/runtime/mgcsweep.go#L226)。这是为了避免每轮 GC 都新分配 bitmap 内存(那会反过来触发 GC),而是预先准备好三套,轮换使用。注释说"The pointer arithmetic is done by hand instead of using arrays to avoid bounds checks along critical performance paths"——bitmap 是分配热路径上的高频访问,手写指针算术省掉数组越界检查。

> **钉死这件事**:sweep 把 gcmarkBits 变 allocBits 是**一次指针赋值**,不是遍历。这是 lazy sweep 能做到"清扫一个 mspan 只要 ~30 ns"的关键之一——重活(标记)在标记阶段干完了,清扫只是"改角色"。两份 bitmap 物理分离,让"标记中"和"分配中"可以并发不踩踏。

---

## 16.6 sweepgen 双缓冲:让"扫上一轮"和"标下一轮"并发不串台

lazy sweep 把清扫摊到了业务运行期间,这带来一个尖锐的并发问题:**清扫可能拖到下一个 GC 周期都没完。下一轮 GC 的标记阶段开始了,上一轮的清扫还在跑——两者会不会互相踩踏?** 比如:标记阶段读 gcmarkBits 想知道"这个对象本轮活不活",结果清扫刚把它换成新的清零图,标记就读到错的。

答案就是 sweepgen(清扫代纪)。它是 mspan 上的一个 uint32 字段 [`mheap.go:502`](../go/src/runtime/mheap.go#L502),配合 `mheap_.sweepgen` 全局代纪,用一个**五态状态机**精确描述每个 mspan 在清扫周期里的位置。字段注释 [`mheap.go:494-501`](../go/src/runtime/mheap.go#L494-L501) 把五态列得很清楚:

```
// sweep generation:
// if sweepgen == h->sweepgen - 2, the span needs sweeping
// if sweepgen == h->sweepgen - 1, the span is currently being swept
// if sweepgen == h->sweepgen,     the span is swept and ready to use
// if sweepgen == h->sweepgen + 1, the span was cached before sweep began and is still cached, and needs sweeping
// if sweepgen == h->sweepgen + 3, the span was swept and then cached and is still cached
// h->sweepgen is incremented by 2 after every GC
```

设当前 `mheap_.sweepgen = S`(每个 GC 周期 +2,所以 S 总是偶数):

| mspan.sweepgen | 含义 | 谁能碰它 |
|---|---|---|
| `S - 2` | 待清扫(needs sweeping) | 清扫者可 CAS 抢 |
| `S - 1` | 正在清扫(being swept) | 只有持有所有权的那个清扫者 |
| `S` | 已清扫,可用(swept) | 分配者可自由用 |
| `S + 1` | 缓存在 mcache 里,未清扫 | mcache uncache 时回归待扫 |
| `S + 3` | 缓存在 mcache 里,已清扫 | mcache uncache 时回归可用 |

`S+1` 和 `S+3` 这两个"缓存"态是给 mcache 用的——P 本地的 mcache 持有 span 时,这些 span 不在 mcentral 的列表里,清扫者扫不到它们;mcache 释放(uncache)时,根据它当前的 sweepgen 状态回归到合适的列表。

### 抢所有权的 CAS

清扫一个 mspan 的第一步是"抢所有权",靠 sweepgen 的 CAS 串行化 [`mgcsweep.go:342`](../go/src/runtime/mgcsweep.go#L342):

```go
// src/runtime/mgcsweep.go #L342-L355
// tryAcquire attempts to acquire sweep ownership of span s.
func (l *sweepLocker) tryAcquire(s *mspan) (sweepLocked, bool) {
    if !l.valid {
        throw("use of invalid sweepLocker")
    }
    // Check before attempting to CAS.
    if atomic.Load(&s.sweepgen) != l.sweepGen-2 {
        return sweepLocked{}, false
    }
    // Attempt to acquire sweep ownership of s.
    if !atomic.Cas(&s.sweepgen, l.sweepGen-2, l.sweepGen-1) {
        return sweepLocked{}, false
    }
    return sweepLocked{s}, true
}
```

一次 CAS 把 sweepgen 从 `S-2`(待扫)推到 `S-1`(扫中)。CAS 成功的唯一性保证了**同一个 mspan 同时只有一个清扫者**——bgsweep 和分配路径的 `cacheSpan` 同时盯上同一个 mspan,只有一个 CAS 成功,另一个失败走开。这就是为什么 `mspan.sweep` 内部改 allocBits/gcmarkBits 不需要额外加锁——它已经独占了所有权。

清扫完,在 `mspan.sweep` 末尾 [`mgcsweep.go:728`](../go/src/runtime/mgcsweep.go#L728):

```go
// src/runtime/mgcsweep.go #L708-L728
// The span must be in our exclusive ownership until we update sweepgen,
// check for potential races.
if state := s.state.get(); state != mSpanInUse || s.sweepgen != sweepgen-1 {
    ...
    throw("mspan.sweep: bad span state after sweep")
}
...
// We need to set s.sweepgen = h.sweepgen only when all blocks are swept,
// because of the potential for a concurrent free/SetFinalizer.
//
// But we need to set it before we make the span available for allocation
// (return it to heap or mcentral), because allocation code assumes that a
// span is already swept if available for allocation.
//
// Serialization point.
// At this point the mark bits are cleared and allocation ready
// to go so release the span.
atomic.Store(&s.sweepgen, sweepgen)
```

注释点破了一个微妙的不变式:**sweepgen 必须在"bitmap 都换好了"之后、"span 可被分配"之前推进到 S**。因为分配代码假设"能从列表里 pop 出来的 span 一定是已清扫的"(allocBits 已经是新的)。如果先 push 再改 sweepgen,另一个线程 pop 出来时 bitmap 还没换好,就用到了旧 allocBits——可能把已死的对象当活对象分配,造成 use-after-free。

`sweepgen = S` 是一个 **release 屏障点**:之前的所有改动(allocBits/gcmarkBits 交换、freeindex 重置、specials 处理)对之后看到 `sweepgen == S` 的分配者可见。这是 Go runtime 用"原子变量状态 + release 语义"做并发同步的典型手法——和 GMP 里 `atomicstatus` 的状态机同源。

### mcentral 的双 spanSet:角色轮换

光有 mspan 级的 sweepgen 还不够——清扫者得知道"哪些 mspan 待扫"。这就是 mcentral 里 `partial[2]`/`full[2]` 两个 spanSet 的作用。字段注释 [`mcentral.go:26-45`](../go/src/runtime/mcentral.go#L26-L45) 说得很清楚:

```
// partial and full contain two mspan sets: one of swept in-use
// spans, and one of unswept in-use spans. These two trade
// roles on each GC cycle. The unswept set is drained either by
// allocation or by the background sweeper in every GC cycle,
// so only two roles are necessary.
//
// sweepgen is increased by 2 on each GC cycle, so the swept
// spans are in partial[sweepgen/2%2] and the unswept spans are in
// partial[1-sweepgen/2%2]. ...
```

`partial[sweepgen/2%2]` 是已清扫的,`partial[1 - sweepgen/2%2]` 是未清扫的。`sweepgen` 每次 GC 加 2,所以 `sweepgen/2%2` 在 0 和 1 之间交替——两个 spanSet **轮流当"已扫"和"未扫"**。具体方法 [`mcentral.go:59-79`](../go/src/runtime/mcentral.go#L59-L79):

```go
// src/runtime/mcentral.go #L59-L79
func (c *mcentral) partialUnswept(sweepgen uint32) *spanSet {
    return &c.partial[1-sweepgen/2%2]
}

func (c *mcentral) partialSwept(sweepgen uint32) *spanSet {
    return &c.partial[sweepgen/2%2]
}

func (c *mcentral) fullUnswept(sweepgen uint32) *spanSet {
    return &c.full[1-sweepgen/2%2]
}

func (c *mcentral) fullSwept(sweepgen uint32) *spanSet {
    return &c.full[sweepgen/2%2]
}
```

这就是"双缓冲"的物理实现。两套列表,一套装上一轮(或本轮)的待扫 span,一套装已扫 span,GC 周期切换时不用搬数据,只是"角色对调"——`sweepgen += 2` 之后,原本是"已扫"的那套自动变成"未扫"(因为 `sweepgen/2%2` 翻转了),原本"未扫"的自动变成"已扫"(上一轮扫完的现在又算待扫了,不对——准确说是:上一轮没扫完留在 unswept 里的,在 `finishsweep_m` 里被强制扫掉重置 [`mgcsweep.go:256`](../go/src/runtime/mgcsweep.go#L256))。

> **不这样会怎样**:如果只有一个 spanSet,每轮 GC 要么"清空它重新装"(那要遍历所有 span 移到新列表,O(N) 操作),要么"就地标记哪些扫了哪些没扫"(那要额外的标记位,且并发改标记位要加锁)。双缓冲用"角色轮换"把这件事变成 O(1)——翻一下 `sweepgen`,两个列表的角色对调,数据不动。这是无锁化思路的延伸:**与其在共享数据上加锁,不如用代纪让两个版本的数据并存,各取各的**。

### 双缓冲怎么让"扫上一轮"和"标下一轮"并发

现在回答本节开头的问题。假设 GC 周期 N 的清扫还没完(bgsweep 还在扫 N 轮的 unswept 列表),周期 N+1 已经开始标记。这时:

- 周期 N 的清扫者读的是 `partial[1 - S/2%2]`(N 轮的 unswept),里面 span 的 sweepgen 是 `S-2` 或 `S-1`。
- 周期 N+1 的标记阶段**不碰 sweepgen**,也不碰 allocBits/gcmarkBits 的轮换——标记只往"current" arena 的 gcmarkBits 写 1。它读 sweepgen 只是为了 `greyobject` 时跳过已死的 span(整个 span 没活对象就跳过)。
- `finishsweep_m` 在周期 N+1 的 mark 开始前的 STW 里被调用 [`mgc.go:852`](../go/src/runtime/mgc.go#L852),它强制把剩余的 N 轮 unswept span 全扫完 [`mgcsweep.go:231`](../go/src/runtime/mgcsweep.go#L231),然后 `reset` 所有 unswept 缓冲 [`mgcsweep.go:255`](../go/src/runtime/mgcsweep.go#L255)。**周期 N+1 的标记开始时,周期 N 的清扫必然已经完成**——这是 STW 提供的硬保证。

所以"扫上一轮"和"标下一轮"在时间上其实**不会真正重叠**——`finishsweep_m` 这个 STW 屏障点把两者隔开了。双缓冲的真正价值是:**周期 N 的清扫可以和周期 N 的业务分配并发**(清扫者改的是 unswept 列表里的 span,分配者从 swept 列表拿 span,各取各的列表,不踩踏),以及**周期 N 的清扫可以一直拖到周期 N+1 的 mark 开始前**(中间业务正常跑,bgsweep 悠着扫,分配路径按需扫,不阻塞)。

> **钉死这件事**:sweepgen 双缓冲解决的是"清扫和分配并发"的问题,不是"清扫和下一轮标记并发"——后者被 `finishsweep_m` 的 STW 屏障隔开了。双缓冲让"待扫 span"和"可用 span"在数据结构上分居两套列表,清扫者和分配者各取各的,无锁并发;周期切换时翻一下 `sweepgen` 让角色对调,O(1) 完成。

---

## 16.7 `mspan.sweep` 干的活:从 specials 到归还

把前面几节拼起来,完整看一遍 `mspan.sweep`(`mgcsweep.go:505`)干的事。它是所有清扫路径(bgsweep 的 sweepone、cacheSpan 的就地扫、ensureSwept)的单一汇合点。按顺序:

1. **入口断言** [`mgcsweep.go:508`](../go/src/runtime/mgcsweep.go#L508):`gp.m.locks == 0 && gp.m.mallocing == 0 && gp != gp.m.g0` 就 throw。清扫必须在"不可抢占段"(`m.locks > 0`)里跑——`sweepone` 一开始就 `gp.m.locks++` [`mgcsweep.go:364`](../go/src/runtime/mgcsweep.go#L364),`cacheSpan` 的调用者(mallocgc)也持锁。为什么?**清扫中途不能被 GC 抢占**——如果清扫改 bitmap 改到一半,GC 触发了(读 sweepgen 发现还在扫),状态就乱了。`m.locks > 0` 阻止抢占,保证清扫原子完成。

2. **处理 specials(finalizer/profiling/weak handle)** [`mgcsweep.go:553`](../go/src/runtime/mgcsweep.go#L553):遍历 span 上的 special 记录(按 offset 排序的链表)。对没标记的对象,看它有没有 finalizer——有就**把它重新标记为活**(`mbits.setMarkedNonAtomic()`,L568),把 finalizer 入队执行(对象这次不能死,要等 finalizer 跑完下一轮再死)。这是 `runtime.SetFinalizer` 的实现核心:finalizer 让一个"逻辑上已死"的对象在本轮被强行续命。没有 finalizer 的死对象,它的 special 记录被一起释放。

3. **调试/检测钩子** [`mgcsweep.go:618`](../go/src/runtime/mgcsweep.go#L618):`racefree`/`msanfree`/`asanpoison`/`clobberfree`(把死对象内存写成 0xdeadbeef 帮检测 use-after-free)。这些只在开启对应检测工具时跑。

4. **inline mark bits 迁移** [`mgcsweep.go:655`](../go/src/runtime/mgcsweep.go#L655):`moveInlineMarks`——某些大对象的 mark bits 是 inline 存的(不是独立 bitmap),需要单独迁移。

5. **僵尸检测** [`mgcsweep.go:659`](../go/src/runtime/mgcsweep.go#L659):检查"标记了但 allocBits 说它自由"的对象(zombie)——这通常意味着用户代码用了 `unsafe.Pointer` 把 uintptr 转回指针(中间 GC 把对象当垃圾回收了,结果后来又被"复活"指向)。发现就 `reportZombies` throw [`mgcsweep.go:859`](../go/src/runtime/mgcsweep.go#L859)。这是 GC soundness 的运行时自检。

6. **统计 + 重置** [`mgcsweep.go:678`](../go/src/runtime/mgcsweep.go#L678):`countAlloc()` 数活对象数(`nalloc`),`nfreed = allocCount - nalloc`。`allocCount = nalloc`、`freeindex = 0` 重置分配游标。

7. **allocBits/gcmarkBits 交接** [`mgcsweep.go:695`](../go/src/runtime/mgcsweep.go#L695):(16.5 节详述)。

8. **推进 sweepgen** [`mgcsweep.go:728`](../go/src/runtime/mgcsweep.go#L728):(16.6 节详述),release 屏障。

9. **归还** [`mgcsweep.go:730`](../go/src/runtime/mgcsweep.go#L730) 起:分情况——
   - 小对象 span(`sizeclass != 0`):全空(`nalloc == 0`)就 `mheap_.freeSpan(s)` 还给 mheap [`mgcsweep.go:788`](../go/src/runtime/mgcsweep.go#L788);全满(`nalloc == nelems`)进 `fullSwept`;部分满进 `partialSwept` [`mgcsweep.go:792`](../go/src/runtime/mgcsweep.go#L792)。
   - 大对象 span(`sizeclass == 0`):整 span 就一个大对象,死了(`nfreed != 0`)就 `mheap_.freeSpan(s)` 还给 mheap [`mgcsweep.go:834`](../go/src/runtime/mgcsweep.go#L834)。
   - `sweepone` 拿到 `s.sweep(false)` 返回 true(整 span 还给堆了),把页数加到 `reclaimCredit` [`mgcsweep.go:400`](../go/src/runtime/mgcsweep.go#L400)——这是给大对象分配的"页回收信用"。

`mheap_.freeSpan` 是把整 span 的页归还给 mheap 的 page allocator [`mheap.go:1657`](../go/src/runtime/mheap.go#L1657),mheap 再决定是留着复用还是交给 scavenger 真正释放给 OS(那是另一套机制,scavenge,见 [`mgcscavenge.go`](../go/src/runtime/mgcscavenge.go))。注意 `sweepone` 末尾在清扫全完成时会 `scavenger.ready()` [`mgcsweep.go:445`](../go/src/runtime/mgcsweep.go#L445) 唤醒 scavenger——清扫完了正好有页可以 scavenger。

> **钉死这件事**:`mspan.sweep` 是清扫的唯一汇合点,它一次完成"处理 finalizer → 检测 zombie → 交换 bitmap → 推进 sweepgen → 归还页"全套动作。整个过程在 `m.locks > 0` 的不可抢占段里原子完成,靠 sweepgen 的 CAS 保证独占。这就是为什么清扫可以和分配并发——每个 mspan 的清扫是一个不可分割的临界区,跨 mspan 之间无共享状态。

---

## 16.8 activeSweep:并发清扫的完成检测

bgsweep 怎么知道"全扫完了可以 park 了"?多个清扫者(bgsweep + 几个分配路径的 sweepone)并发跑,怎么知道最后一个扫完?这就是 `activeSweep` 结构 [`mgcsweep.go:126`](../go/src/runtime/mgcsweep.go#L126) 干的事。它把一个 uint32 拆成两部分 [`mgcsweep.go:119`](../go/src/runtime/mgcsweep.go#L119):

```go
// src/runtime/mgcsweep.go #L119-L136
const sweepDrainedMask = 1 << 31

type activeSweep struct {
    // state is divided into two parts.
    //
    // The top bit (masked by sweepDrainedMask) is a boolean
    // value indicating whether all the sweep work has been
    // drained from the queue.
    //
    // The rest of the bits are a counter, indicating the
    // number of outstanding concurrent sweepers.
    state atomic.Uint32
}
```

- **高位(bit 31)**:`drained` 标志,表示"队列里没活可干了"(`nextSpanForSweep` 扫到尾)。
- **低 31 位**:正在跑的清扫者计数。

每个清扫者开始干之前 `begin()` [`mgcsweep.go:148`](../go/src/runtime/mgcsweep.go#L148),干完 `end()` [`mgcsweep.go:162`](../go/src/runtime/mgcsweep.go#L162):

```go
// src/runtime/mgcsweep.go #L148-L158
func (a *activeSweep) begin() sweepLocker {
    for {
        state := a.state.Load()
        if state&sweepDrainedMask != 0 {
            return sweepLocker{mheap_.sweepgen, false}   // 已 drained,没活
        }
        if a.state.CompareAndSwap(state, state+1) {
            return sweepLocker{mheap_.sweepgen, true}     // 计数 +1,拿锁
        }
    }
}
```

`begin` 用 CAS 把计数 +1,同时检查 drained 位——已 drained 就返回 invalid locker(调用者知道没活可干了)。`end` 把计数 -1,如果"减到只剩 drained 位"(即 `state-1 == sweepDrainedMask`,意思是计数归零且 drained 已置位),说明**自己是最后一个清扫者,且队列已空**——这时触发清扫完成的收尾(打印 pacer trace、`gcCleanups.flush()`) [`mgcsweep.go:172`](../go/src/runtime/mgcsweep.go#L172)。

`markDrained` [`mgcsweep.go:193`](../go/src/runtime/mgcsweep.go#L193) 在 `sweepone` 发现 `nextSpanForSweep` 返回 nil 时调用,把 drained 位置 1。它返回 true 表示"我是置位的那一个"。

这套机制的本质是:**用一个原子 uint32 同时跟踪"还有没有活"和"还有几个清扫者在跑",最后一个跑完的负责收尾**。和 sync.WaitGroup 同源——但这里把"完成标志"和"计数"塞进一个原子变量,用 CAS 同时更新,避免两个变量之间的 race。

> **钉死这件事**:`activeSweep` 解决的是"并发清扫的完成检测"——多个清扫者异步跑,谁也不知道自己是不是最后一个。用"计数 + drained 标志"塞一个 uint32,靠 CAS 保证"减到零且 drained"这个事件恰好被一个清扫者观测到,由它做收尾。这是 lock-free 完成检测的标准范式。

---

## 16.9 技巧精解:lazy sweep 的摊销 + sweepgen CAS 串行化

这一章最硬的两个技巧,挑出来配源码对比拆透。

### 技巧一:lazy sweep——把清扫摊到分配路径,反面对比 STW 全量清扫

lazy sweep 的核心反直觉点是:**GC 周期结束时不扫任何东西,只翻代纪;真正的清扫发生在业务运行期间,谁要 span 谁顺手扫**。这是把"清扫"这个动作从"GC 阶段"搬到了"分配阶段"。

它依赖三个机制叠加:

1. **代纪翻转是 O(1)**:`gcSweep` 在 STW 里只做 `mheap_.sweepgen += 2` + reset 计数器 [`mgc.go:2073`](../go/src/runtime/mgc.go#L2073),不扫任何 mspan。这是 STW 代价亚微秒的物理基础。
2. **代纪翻转瞬间所有 mspan 变"待扫"**:因为判定条件是 `mspan.sweepgen == h.sweepgen - 2`,h 加 2 之后,原本 `== h` 的(已扫)全部变成 `== h-2`(待扫)。一次原子加,全堆状态翻转。
3. **清扫按需触发**:`mcentral.cacheSpan` 要 span 时先扫 [`mcentral.go:118`](../go/src/runtime/mcentral.go#L118),`deductSweepCredit` 按比例补扫 [`mgcsweep.go:913`](../go/src/runtime/mgcsweep.go#L913),bgsweep 后台悠着扫 [`mgcsweep.go:272`](../go/src/runtime/mgcsweep.go#L272)。

> **反面对比**:朴素 STW 清扫会怎样?假设 10 GB 堆,~150 GB bitmap(每 8 字节对象 1 bit,加对齐),~百万级 mspan。STW 里要:遍历所有 mspan、对每个 mspan 跑 `mspan.sweep`(改 bitmap、统计、归还页)。单 mspan ~30 ns,百万级就是 ~30 ms 起步,加上归还页的 mheap 锁争用,实测往往是几百毫秒到秒级。这和 Go 的亚毫秒 STW 承诺差三个数量级。

lazy sweep 把这个 ~30 ms+ 的总工作量**摊到了整个 GC 间隔**(通常几十毫秒到几秒)里——bgsweep 每次扫 10 个就让出,分配路径每次按比例补扫几个,加起来在下一个 GC 周期到来前(`finishsweep_m` 之前的 STW)必然扫完。每个清扫动作都很小(~30 ns/mspan),分散在业务运行的间隙,业务几乎感知不到。

这和第 15 讲的 mark assist 是**镜像设计**:

| | mark assist | lazy sweep |
|---|---|---|
| 把什么摊到分配路径 | 标记工作(扫描对象) | 清扫工作(扫 mspan) |
| 摊的依据 | `assistWorkPerByte`(每字节欠多少扫描) | `sweepPagesPerByte`(每字节欠多少页清扫) |
| 触发点 | `deductAssistCredit` in mallocgc | `deductSweepCredit` in cacheSpan |
| 反压目标 | 防堆越过 heapGoal | 防清扫拖到下一周期 STW |
| 后台基线 | 25% CPU mark worker | bgsweep 低优先级 goroutine |

两者合起来,把 GC 的标记和清扫两半都钉死在分配速率上,谁分配谁出力,GC 永远跟得上业务。

### 技巧二:sweepgen CAS 串行化——一个 mspan 同时只有一个清扫者

多个清扫者(bgsweep + 多个 P 上的 cacheSpan)可能同时盯上同一个待扫 mspan。怎么保证不重复扫、不并发改 bitmap?答案是 sweepgen 的 CAS [`mgcsweep.go:351`](../go/src/runtime/mgcsweep.go#L351):

```go
// src/runtime/mgcsweep.go #L347-L354
// Check before attempting to CAS.
if atomic.Load(&s.sweepgen) != l.sweepGen-2 {
    return sweepLocked{}, false
}
// Attempt to acquire sweep ownership of s.
if !atomic.Cas(&s.sweepgen, l.sweepGen-2, l.sweepGen-1) {
    return sweepLocked{}, false
}
return sweepLocked{s}, true
```

一次 CAS 把 `S-2 → S-1`。CAS 的原子性保证**只有一个清扫者成功**,其他都失败走开(在 `cacheSpan` 里就是跳过这个 span 找下一个,在 `sweepone` 里也是 continue)。成功的那个独占所有权,在 `mspan.sweep` 内部自由改 bitmap、改 freeindex、归还页——**全程不加锁**,因为它已经独占了。

> **不这样会怎样**:朴素的实现可能是给每个 mcentral 的 spanSet 加一把锁,清扫者 pop span 时持锁、扫完放回时持锁。这有两个问题:(a) spanSet 是高频结构(每次小对象分配都走 cacheSpan),一把全局锁会成为瓶颈;(b) 锁的粒度是 spanSet(整个 size class),一个清扫者扫某个 span 时会阻塞同一 size class 的所有分配。Go 的解法把锁粒度降到**单个 mspan**——用 sweepgen 的 CAS 当"per-mspan 锁",无数据结构开销(就是 mspan 上已有的一个 uint32),且不阻塞 spanSet 的其他操作(pop/push 是 lock-free 的 spanSet)。这是"**用状态机的 CAS 当锁,替代显式锁结构**"的典型——和 GMP 的 `atomicstatus` 状态机、channel 的 hchan 锁内外分工同源。

更妙的是那个"先 Load 再 CAS"的两步 [`mgcsweep.go:347`](../go/src/runtime/mgcsweep.go#L347):先 `Load` 检查 sweepgen 是不是 `S-2`,不是直接返回 false(省一次 CAS)。这是**快路径优化**——大多数 mspan 在大多数时刻不是 `S-2`(要么已扫要么在扫),先 Load 过滤掉这些,CAS 只在真正可能成功时才发。CAS 比 Load 贵(LOCK 前缀,全 cache line 同步),这个过滤在高频路径上省下可观的开销。

这两个技巧合起来,回答了 sweep 最核心的工程问题:**怎么让"清扫整个堆"这件 O(堆大小) 的工作,既不在 STW 里(否则停顿爆炸),又能在并发下安全地摊到分配路径上(否则数据竞争)?** 答案是——代纪翻转 O(1) 翻状态,清扫按需触发摊到分配,per-mspan 的 sweepgen CAS 保证并发安全,双 spanSet 让"待扫"和"可用"分居两套列表无锁并发。

---

## 16.10 技巧精解补:为什么 lazy sweep sound ——并发下不会漏扫、不会重扫、不会撕裂

lazy sweep 让清扫和分配、和下一轮标记并发跑,听起来很危险。这一节专门回答 soundness。

**不会漏扫**:`finishsweep_m` 在每个 GC 周期的 mark 开始前(STW 里)强制把剩余 unswept span 全扫完 [`mgcsweep.go:239`](../go/src/runtime/mgcsweep.go#L239):

```go
// src/runtime/mgcsweep.go #L231-L249
func finishsweep_m() {
    assertWorldStopped()

    // Sweeping must be complete before marking commences, so
    // sweep any unswept spans. If this is a concurrent GC, there
    // shouldn't be any spans left to sweep, so this should finish
    // instantly. If GC was forced before the concurrent sweep
    // finished, there may be spans to sweep.
    for sweepone() != ^uintptr(0) {
    }

    // Make sure there aren't any outstanding sweepers left.
    ...
    if sweep.active.sweepers() != 0 {
        throw("active sweepers found at start of mark phase")
    }

    // Reset all the unswept buffers, which should be empty.
    ...
    for i := range mheap_.central {
        c := &mheap_.central[i].mcentral
        c.partialUnswept(sg).reset()
        c.fullUnswept(sg).reset()
    }
    ...
}
```

注释明说:"Sweeping must be complete before marking commences"。如果并发清扫还没完(比如 GC 被强制提前触发),这里在 STW 里兜底扫完——所以下一轮标记开始时,**上一轮清扫必然 100% 完成**。`sweep.active.sweepers() != 0` 的 throw 是自检:STW 里不应该还有清扫者在跑(它们应该都被 STW 挂起了),如果有就是 bug。proportional sweep 的设计目标就是让 `finishsweep_m` 的这个 for 循环"瞬间完成"(平常没东西可扫),只有 GC 被异常提前触发时才会真扫——这是兜底,不是常态。

**不会重扫**:每个 mspan 的清扫靠 sweepgen CAS 串行化(16.6 节),一次只有一个清扫者把 `S-2 → S-1`。扫完推进到 `S`,这个 mspan 在本轮就再也不会被扫(它的 sweepgen 不再是 `S-2`)。即使两个清扫者同时盯上,只有一个 CAS 成功。

**不会撕裂 bitmap**:`mspan.sweep` 改 allocBits/gcmarkBits 在 `m.locks > 0` 的不可抢占段里(16.7 节),且在推进 sweepgen(`atomic.Store(&s.sweepgen, sweepgen)`)之前完成。sweepgen 的 store 是 release 屏障——分配者用 `atomic.Load(&s.sweepgen)` 看到 `S` 时(acquire 语义),必定能看到前面所有 bitmap 改动。所以**分配者要么看到"完全扫好的新 bitmap",要么看到"还没扫的旧 bitmap",不会看到半改的中间态**。

**不会和标记冲突**:标记阶段(周期 N+1)只往"current arena"的 gcmarkBits 写 1,不碰 allocBits。而清扫(周期 N)改的是上一轮的 allocBits/gcmarkBits——这两套 bitmap 在物理上是不同的 arena(previous/current),标记写的和清扫改的不是同一块内存。`finishsweep_m` 的 STW 把两者隔开(周期 N 清扫完才进周期 N+1 标记),arena 轮转(`finishsweep_m` 里 `nextMarkBitArenaEpoch()` [`mgcsweep.go:269`](../go/src/runtime/mgcsweep.go#L269))在 STW 里原子完成。

> **钉死这件事**:lazy sweep 的 soundness 靠四个机制叠加——`finishsweep_m` 的 STW 屏障保证不漏扫,sweepgen CAS 保证不重扫,`m.locks > 0` + release/acquire 屏障保证不撕裂,arena 轮转 + STW 隔离保证不和标记冲突。这套机制没有发明新的并发原语,全是"原子变量状态机 + 不可抢占段 + STW 屏障"的组合——和 mark assist 复用写屏障、GMP 复用 atomicstatus 是同一套工程哲学:**用已有的同步原语组合出 sound 的并发,而不是为每个新机制造新原语**。

---

## 章末小结

这一讲是第 4 篇(GC)的收尾章。我们没有碰新的 GC 算法(三色 + 写屏障第 13 讲、并发阶段第 14 讲、mark assist 第 15 讲都讲完了),只讲**清扫的工程**:标记完了,垃圾怎么回收。回扣全书二分法:本章服务的是**支撑地基**——GC 是 GMP 调度和阻塞唤醒能持续跑的前提,而"标记结果能被无损翻译成可分配内存、清扫不阻塞业务"是这套地基能持续供给内存的硬指标。lazy sweep 把 O(堆大小) 的清扫工作摊到业务运行期间,让 GC 周期结束的 STW 只剩翻代纪那一下(亚微秒),是 Go GC 能做到亚毫秒停顿的最后一环——和第 14 讲的"标记并发"、第 15 讲的"assist 反压"合起来,凑齐了并发 GC 的三块拼图。

本章立起了六样东西:

1. **sweep 的两层结构**:对象回收器(per-slot,认自由 slot)和 span 回收器(per-span,整页归还 mheap),两者都汇到 `mspan.sweep` 单一入口。
2. **lazy sweep 的三个驱动源**:bgsweep(后台低优先级悠着扫)、proportional sweep(`deductSweepCredit` 按比例补扫)、`cacheSpan` 就地扫(要 span 顺手扫一个)。三层共同保证"分配者随时拿到干净 span,清扫不拖到下一周期"。
3. **allocBits/gcmarkBits 交接**:标记图(谁活着)在清扫时**一次指针赋值**变成分配图(谁占用),gcmarkBits 换清零新图。两份 bitmap 物理分离,让标记和分配并发不踩踏。
4. **sweepgen 五态双缓冲**:`S-2/S-1/S/S+1/S+3` 五态状态机 + mcentral 的 `partial[2]`/`full[2]` 角色轮换,让"待扫"和"可用"分居两套列表,周期切换 O(1) 翻角色。
5. **activeSweep 完成检测**:一个 uint32 塞"drained 标志 + 清扫者计数",最后一个清扫者负责收尾。
6. **soundness**:`finishsweep_m` STW 屏障防漏扫,sweepgen CAS 防重扫,`m.locks > 0` + release/acquire 防撕裂,arena 轮转防标记冲突。

### 五个"为什么"清单

1. **为什么 sweep 在 STW 里只翻代纪(`sweepgen += 2`),不真的扫?** 因为清扫是 O(堆大小) 的工作,放 STW 里会让停顿和堆成正比(几百毫秒到秒级),违背亚毫秒承诺。翻代纪是 O(1) 的原子加,瞬间把所有 mspan 状态从"已扫"翻成"待扫",真正的清扫摊到业务运行期间。这是"把重活搬出 STW"在第 14 讲标记阶段之外的另一半实践。

2. **allocBits 和 gcmarkBits 为什么是两份独立 bitmap,不复用一份?** 因为它们在时间上重叠——本轮标记还在往 gcmarkBits 写时,分配路径还在用 allocBits 找自由 slot。共用一份会让标记写和分配读写抢同一块内存。两份分开,语义清晰(allocBits=分配视图,gcmarkBits=标记视图),且清扫时一次指针赋值就能交接,O(1)。

3. **`partial[2]`/`full[2]` 为什么要双 spanSet 角色轮换?** 让"待扫 span"和"已扫 span"在数据结构上分居两套列表,清扫者从 unswept 列表 pop,分配者从 swept 列表 pop,各取各的无锁并发。周期切换时翻 `sweepgen`(每次 +2)让 `sweepgen/2%2` 翻转,两个列表角色对调,数据不动,O(1)。朴素方案(单列表 + 标记位)要么 O(N) 搬数据要么加锁,双缓冲都用代纪绕开了。

4. **`mcentral.cacheSpan` 为什么要扫了 span 再用,不直接要新 span?** 直接要新 span(`c.grow()`)会持续扩张堆(每个 size class 都囤一堆半满 span),空间浪费。lazy sweep 的本意是"复用已有 span 的自由 slot",`cacheSpan` 优先扫 partialUnswept 拿现成 slot。但也不能无限扫——`spanBudget = 100` 限制了最多扫 100 个,扫不到就要新页,注释说"limit the space overhead to 1%"。这是延迟(扫 span 时间)和空间(多分配新页)的精算取舍。

5. **`finishsweep_m` 在每个 GC 周期 mark 开始前的 STW 里强制扫完剩余 span,这会不会又退化成 STW 清扫?** 常态下不会——proportional sweep 的设计目标就是让周期 N 的清扫在周期 N+1 到来前必然完成,所以 `finishsweep_m` 的 `for sweepone() != ^uintptr(0)` 循环"瞬间完成"(没东西可扫)。只有 GC 被异常提前触发(比如手动 `runtime.GC()` 或 OOM 边缘)时,这里才会真扫——这是兜底保险,不是常态路径。它的存在保证了 soundness(标记开始前必然扫完),代价是极端情况下有一点 STW,但常态零开销。

### 想继续深入往哪钻

- **源码文件**:本章主战场 [`../go/src/runtime/mgcsweep.go`](../go/src/runtime/mgcsweep.go)(`gcSweep` 在 mgc.go:2065、`bgsweep` #L272、`sweepone` #L359、`tryAcquire` #L342、`mspan.sweep` #L505、`deductSweepCredit` #L913、`gcPaceSweeper` #L982、`activeSweep` #L126、`finishsweep_m` #L231)、[`../go/src/runtime/mcentral.go`](../go/src/runtime/mcentral.go)(`cacheSpan` #L82、`partialUnswept`/`partialSwept` #L59/#L65)、[`../go/src/runtime/mheap.go`](../go/src/runtime/mheap.go)(mspan 结构体 #L422、sweepgen 五态注释 #L494-L501、`mheap_.sweepgen` #L73、`freeSpan` #L1657)、[`../go/src/runtime/mgc.go`](../go/src/runtime/mgc.go)(`gcSweep` 调用点 #L1404、`gcenable` 起 bgsweep #L211、`finishsweep_m` 调用 #L852)。
- **scavenge(归还页给 OS)**:本章讲的 `mheap_.freeSpan` 只是把页还给 mheap 的 page allocator(可复用),并不一定释放给 OS。真正把闲置页 `madvise(MADV_FREE)`/`MADV_DONTNEED` 还给 OS 的是 scavenger,源码在 [`../go/src/runtime/mgcscavenge.go`](../go/src/runtime/mgcscavenge.go)。sweep 完 `scavenger.ready()` 唤醒它 [`mgcsweep.go:445`](../go/src/runtime/mgcsweep.go#L445)。scavenge 是另一套独立的"页回收 → OS"机制,和 sweep(对象/slot 回收)正交,值得单独钻。设计文档看 [golang/proposal#46794 GC pacer redesign](https://github.com/golang/proposal/blob/master/design/44167-gc-pacer-redesign.md)(涵盖 sweep pacing)。
- **观测 sweep**:`GODEBUG=gctrace=1` 打印的每轮 GC 行里,"SWEEP" 阶段时间就是从 `gcSweep` 到 `finishsweep_m` 之间(下一轮开始)的并发清扫时间;`debug.gcpacertrace > 0` 会在清扫完成时打印 "pacer: sweep done at heap size X MB; allocated Y MB during sweep; swept Z pages at W pages/byte" [`mgcsweep.go:176`](../go/src/runtime/mgcsweep.go#L176),能看到 proportional sweep 的实际速率。`go tool trace` 里能看 bgsweep goroutine(`waitReasonGCSweepWait`)的活动和每个 `s.sweep(false)` 的 `GCSweepSpan` trace 事件。
- **finalizer 的代价**:本章 `mspan.sweep` 处理 specials 时,有 finalizer 的死对象会被续命一轮——这意味着 `SetFinalizer` 不仅延迟回收,还可能让一个"该死"的对象活两轮 GC。高频 SetFinalizer 会显著拖慢 sweep(specials 链表遍历)和增加堆压力。Go 团队一直不鼓励 finalizer,官方建议用 `runtime.AddCleanup`(Go 1.24+,更轻量,不续命对象)。源码对比 `_KindSpecialFinalizer` vs `_KindSpecialCleanup` 在 [`mfinal.go`](../go/src/runtime/mfinal.go)。
- **sweepgen 五态里的 `S+1`/`S+3`(缓存态)**:这两个态是给 mcache 的——P 本地 mcache 持有的 span 不在 mcentral 列表里,uncache 时根据 sweepgen 状态回归。涉及 `mcache.refill`/`releaseall`,源码在 [`mcache.go`](../go/src/runtime/mcache.go)。要彻底搞清"span 怎么在 mcache 和 mcentral 之间流动",得结合第 10 讲(mspan/mcache/mcentral/mheap 层级)一起看。

### 引出下一章

讲完 sweep,第 4 篇(并发 GC)的四块拼图就齐了:三色 + 写屏障(第 13 讲)防漏标、并发四阶段(第 14 讲)最小化 STW、mark assist(第 15 讲)反压分配、lazy sweep(本章)摊销清扫。一个对象从被分配、被标记、到被清扫回收的全闭环走完了。GC 这块地基能持续供给内存、几乎不阻塞业务。接下来第 5 篇转向另一块地基——**栈**:goroutine 的栈为什么能从 2KB 起步按需增长?栈拷贝怎么处理栈上指针?为什么 Go 选择"连续栈拷贝"而不是 segmented stack(链式栈)?下一讲 P5-17《可增长栈与栈拷贝》拆 `newstack`/`copystack`/`shrinkstack`,看 Go 怎么让一个 goroutine 的栈真正"轻"起来——以及它和 Tokio 的 `Pin` 怎么用两种完全不同的姿势解决"自引用数据结构不能移动"这同一个根本难题。

---

> 全书定位:第 16 章 / 第 4 篇 并发 GC(支撑地基)。源码版本 Go 1.27(本地 master @ `6d1bcd10`,`src/internal/govversion/govversion.go` 的 `const Version = 27`)。下一章:P5-17 可增长栈与栈拷贝(标 ★ 双璧对照 Tokio Pin)。
>
> 源码事实修正:① 任务锚点给的 `sweep`/`sweepone`/`lazy sweep 衔接` 均在 [`mgcsweep.go`](../go/src/runtime/mgcsweep.go),但实际入口 `gcSweep`(翻代纪 `sweepgen += 2`)在 [`mgc.go:2065`](../go/src/runtime/mgc.go#L2065),由 mark termination 的 STW 在 `mgc.go:1404` 调用——任务锚点未提这个跨文件入口。② mspan 结构体不在 mspan.go(该文件不存在),而在 [`mheap.go:422`](../go/src/runtime/mheap.go#L422);sweepgen 五态注释在 `mheap.go:494-501`,allocBits/gcmarkBits 三 arena 轮转注释在 `mheap.go:468-492`。③ `mheap_.sweepgen` 全局代纪字段在 `mheap.go:73`(注释明说 "written during STW")。④ mcentral 的双 spanSet 角色轮换(`partial[sweepgen/2%2]` vs `partial[1-sweepgen/2%2]`)在 `mcentral.go:26-45` 注释 + `mcentral.go:59-79` 方法,任务锚点未提这个双缓冲机制,但它是 lazy sweep 能并发的核心。⑤ `finishsweep_m`(`mgcsweep.go:231`)在每个 GC 周期 mark 开始前的 STW(`mgc.go:852`)强制扫完剩余 span + reset unswept 缓冲,是 soundness 的兜底,任务锚点未提。⑥ `mspan.sweep`(`mgcsweep.go:505`)是单一汇合点,干 specials/zombie 检测/bitmap 交接/sweepgen 推进/归还全套;allocBits/gcmarkBits 交接是 `s.allocBits = s.gcmarkBits; s.gcmarkBits = newMarkBits(nelems)`(`mgcsweep.go:695-698`)一次指针赋值 O(1)。⑦ proportional sweep 的 `sweepPagesPerByte` 由 `gcPaceSweeper`(`mgcsweep.go:982`)在 STW 里算,`deductSweepCredit`(`mgcsweep.go:913`)在 `cacheSpan` 入口(`mcentral.go:85`)按比例补扫,是 mark assist 在清扫侧的镜像。⑧ bgsweep 是 `gcenable`(`mgc.go:211`)起的后台 goroutine,`sweepBatchSize = 10` 批量扫 + `goschedIfBusy` 让出;定位是 "low priority"(注释 `mgcsweep.go:282`),分配路径的 lazy sweep 会兜底。⑨ per-mspan 的并发安全靠 `tryAcquire`(`mgcsweep.go:342`)的 sweepgen CAS(`S-2 → S-1`)+ `m.locks > 0` 不可抢占段 + sweepgen store 的 release 屏障(`mgcsweep.go:728`),不是显式锁。⑩ activeSweep(`mgcsweep.go:126`)用一个 uint32 高位当 drained 标志、低 31 位当清扫者计数,完成检测靠 CAS,是 lock-free WaitGroup 范式。
