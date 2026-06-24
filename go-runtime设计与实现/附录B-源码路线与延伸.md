# 附录 B · 《源码阅读路线与延伸》

> 这本书正文 21 章 + 附录 A 是"把 Go runtime 讲给你听";本附录换个姿势——给你**地图**和**工具箱**,让你自己接着啃、接着测、接着调。
>
> 读完这一附录,你应该能:
>
> 1. 拿到一份 `golang/go` 源码,知道**该从哪些文件读起、按什么顺序读**;
> 2. 用 `GODEBUG`、`runtime/trace`、`pprof`、`GOMAXPROCS` 这些**观测与调参工具**去看 runtime 在你自己的程序里到底在干什么;
> 3. 把每个调参旋钮(GOGC / GOMEMLIMIT / asyncpreemptoff 等)拧大拧小会发生什么,心里有数;
> 4. 把 Go runtime 和你**已经熟或即将遇到的其他 VM**(Java JVM、Erlang VM)摆在一张桌上对读,看清 Go 的取舍;
> 5. 沿着 Go 1.1 → 1.27 的**演进时间线**,理解为什么老资料里有些说法已经过时。
>
> 全书钉死在 Go 源码 commit `6d1bcd10`(2026-06),版本号 **go1.27**(见 [`go/src/internal/goversion/goversion.go`](../go/src/internal/goversion/goversion.go#L12):`const Version = 27`)。本附录所有行号、变量名、默认值都以此为准,逐一 Grep/Read 核实过,不凭记忆。

---

## 一、`src/runtime` 阅读地图

### 1.1 全景:本书各篇 → 源码文件的对应

下面这张表,把本书 8 篇正文对应的源码文件**串成一条阅读路线**。建议你按"篇"的顺序读——本书的篇序就是 runtime 的依赖序(先有调度器,channel/GC/内存才能各安其位)。

| 本书篇目 | 核心源码文件 | 看什么(一句话) | 对应章节 |
|---|---|---|---|
| 第 1 篇 GMP | `runtime2.go` | G/M/P/sudog/sched 结构定义、状态机字段 | P1-02 |
| 第 1 篇 GMP | `proc.go` | `schedule`/`findRunnable`/`runqget`/`runqput`/`runqsteal`/`entersyscall`/`exitsyscall`/`sysmon`/`schedtrace` | P1-03~07 |
| 第 1 篇 GMP | `asm_amd64.s` | `mcall`/`gogo`/`systemstack`/`asminit` 栈切换汇编 | P1-03, P1-06 |
| 第 1 篇 GMP | `preempt.go`、`signal_unix.go` | 异步抢占(`preemptM` 发 `SIGURG`)与信号处理 | P1-05 |
| 第 2 篇 channel | `chan.go` | `hchan` 结构、`chansend`/`chanrecv`/`closechan` | P2-08 |
| 第 2 篇 channel | `select.go` | `selectgo` 的乱序选 case、锁顺序 | P2-09 |
| 第 3 篇 内存 | `malloc.go` | `mallocgc` 入口、tiny allocator | P3-11 |
| 第 3 篇 内存 | `mcache.go`/`mcentral.go`/`mheap.go`/`mspan.go` | 三级缓存层级 | P3-10 |
| 第 3 篇 内存 | `mbitmap.go` | 类型位图(GC 扫描用) | P3-12 |
| 第 3 篇 内存 | `sizeclasses.go` | size class 表(代码生成,只读) | P3-10 |
| 第 4 篇 GC | `mgc.go` | `gcStart`/`gcMarkDone`/`gcSweep`、GC 触发条件 | P4-14 |
| 第 4 篇 GC | `mgcmark.go`/`mgcmark_greenteagc.go` | 并发标记主体、`gcAssistAlloc` | P4-13, P4-15 |
| 第 4 篇 GC | `mgcsweep.go` | 并发清扫、lazy sweep | P4-16 |
| 第 4 篇 GC | `mgcpacer.go` | GC 步调(pacer)、`readGOGC`/`readGOMEMLIMIT` | P4-14, P4-15 |
| 第 4 篇 GC | `mbw_amd64.s` | 写屏障汇编 stub(`gcWriteBarrier`) | P4-13 |
| 第 5 篇 栈 | `stack.go` | `newstack`/`copystack`/`shrinkstack` | P5-17 |
| 第 6 篇 netpoll | `netpoll.go`/`netpoll_epoll.go`(Linux) | `netpoll`/`netpollblock`、epoll 集成 | P6-18 |
| 第 6 篇 netpoll | `fd_unix.go`(在 `internal/poll`) | `conn.Read` → `poll_runtime_pollWait` 衔接 | P6-19 |
| 第 7 篇 sync | `sync/mutex.go`、`sync/runtime.go` | `Mutex` normal/starvation 模式 | P7-20 |
| 第 7 篇 sync | `runtime/sema.go` | 信号量实现 | P7-20 |
| 第 7 篇 sync | `runtime/time.go` | timer 四叉堆(`timers`/`addtimer`/`runtimer`) | P7-20 |
| 收尾 | `debug.go`(runtime 顶层)、`runtime/debug/garbage.go` | `SetGCPercent`/`SetMemoryLimit`/`SetMaxStack`/`SetPanicOnFault` | P8-21 |
| 工具入口 | `trace.go`(runtime 顶层) | `trace.Start`/`trace.Stop`,执行追踪 | 本附录② |
| 全局地基 | `runtime1.go`、`extern.go` | GODEBUG 变量定义与文档、`dbgvars` 表 | 本附录③ |

### 1.2 推荐阅读顺序(给"想从零啃 runtime"的人)

不要从 `runtime2.go` 第一行往下读——那是结构定义,没有上下文会像天书。按下面这个顺序,你会始终知道"我现在读的这段,在解决什么问题"。

**第一轮:建立坐标(G/M/P)**

1. [`runtime2.go`](../go/src/runtime/runtime2.go) 只看三件事:`type g struct`、`type m struct`、`type p struct`,以及 `type schedt struct`(全局调度器状态)。读字段时**只问一句**:"这个字段是被谁读写的、为什么放这儿"——本书 P1-02 已替你拆过一遍,这里只是回到源头对一遍。
2. [`proc.go`](../go/src/runtime/proc.go) 里的 `schedule()`(本书 P1-03 锚点)和 `findRunnable()`——这是 M 找下一个 G 的核心循环,理解了它,前面那些字段全活了。
3. [`asm_amd64.s`](../go/src/runtime/asm_amd64.s) 里的 `mcall`、`gogo`、`systemstack`——三段小汇编,把"栈怎么从 G1 切到 G2"具象化。**配合本书 P1-03 的逐行解释读**。

**第二轮:让 G 跑起来(调度执行)**

4. `proc.go` 里的 `runqget`/`runqput`/`runqsteal`/`runqgrab`——P 本地队列 + work-stealing(本书 P1-04)。
5. `proc.go` 里的 `entersyscall`/`exitsyscall`/`handoffp`——阻塞系统调用怎么不卡住 P(本书 P1-06)。
6. [`preempt.go`](../go/src/runtime/preempt.go) + `signal_unix.go` 里的 `preemptM`/`doSigPreempt`——异步抢占(本书 P1-05)。
7. `proc.go` 里的 `sysmon()`——后台监控线程兜底(本书 P1-07)。

**第三轮:让 G 通信与回收(channel + GC + 内存)**

8. [`chan.go`](../go/src/runtime/chan.go) 整个文件——本书 P2-08 已带你走过,这里挑 `chansend`/`chanrecv` 的快路径和慢路径各读一遍。
9. [`mgc.go`](../go/src/runtime/mgc.go) 里的 `gcStart`——GC 主流程(本书 P4-14)。
10. [`malloc.go`](../go/src/runtime/malloc.go) 里的 `mallocgc`——分配入口,串起 mcache → mcentral → mheap(本书 P3-10/11)。
11. [`stack.go`](../go/src/runtime/stack.go) 里的 `newstack`/`copystack`——可增长栈(本书 P5-17)。

**第四轮:让 G 不被 I/O 卡死(netpoll + timer)**

12. [`netpoll_epoll.go`](../go/src/runtime/netpoll_epoll.go)(Linux)或 `netpoll_kqueue.go`(macOS/BSD)——epoll/kqueue 怎么被 runtime 藏起来(本书 P6-18)。
13. `netpoll.go` 里的 `netpoll`/`netpollblock`/`netpollready`——阻塞唤醒循环(本书 P6-19)。
14. [`runtime/time.go`](../go/src/runtime/time.go) 里的 `timers` 四叉堆——定时器(本书 P7-20)。

> **一个提醒**:如果你只读一遍 `proc.go`,大概率读不完——这个文件 7000+ 行,是 runtime 里最大的几个之一。**用本书的章节当切片**,每次只读本书讲到的那个函数,读完回到本书对一遍解释,再继续。把"啃源码"拆成"啃函数"。

### 1.3 哪些文件先别碰(免得劝退)

- `runtime2.go` 整体很大,先只读 G/M/P/sudog/sched 五个结构,其余字段等用到时再查。
- `asm_*.s` 不要从头读到尾,只读本书 P1-03/P1-06 锚定的那几段(`mcall`/`gogo`/`systemstack`/`entersyscall`/`exitsyscall`)。
- `mbw_amd64.s`(写屏障汇编)和 `vlop_arm64.s` 等架构相关汇编,只在本书 P4-13 讲写屏障时配合读。
- `defs_*.go`、`os_*.go` 这些平台适配文件,除非你在做移植或排查系统调用,否则不需要读。

---

## 二、观测 runtime:GODEBUG、trace、pprof

读源码是"看设计",观测是"看运行"。Go 把 runtime 的内部状态**几乎全暴露了出来**——你只要知道开关在哪。

### 2.1 GODEBUG:零成本快照

`GODEBUG` 是一个环境变量,值是逗号分隔的 `name=val`。所有支持的开关定义在 [`runtime1.go`](../go/src/runtime/runtime1.go#L357) 的 `dbgvars` 数组里,文档在 [`extern.go`](../go/src/runtime/extern.go#L35)。下面四个是观测 runtime 最常用的:

| 变量 | 作用 | 典型用法 |
|---|---|---|
| `schedtrace=X` | 每 X 毫秒打印一行调度器快照 | `GODEBUG=schedtrace=1000 ./prog` |
| `scheddetail=1` | 配合 `schedtrace`,打印多行详细 P/M/G 状态 | `GODEBUG=schedtrace=1000,scheddetail=1 ./prog` |
| `gctrace=1` | 每次 GC 打印一行摘要 | `GODEBUG=gctrace=1 ./prog` |
| `asyncpreemptoff=1` | 关闭异步抢占(调试用,见③) | `GODEBUG=asyncpreemptoff=1 ./prog` |

#### schedtrace:调度器一行快照

`schedtrace` 的输出由 [`proc.go`](../go/src/runtime/proc.go#L6957) 的 `schedtrace()` 函数生成,格式如下:

```
SCHED 1003ms: gomaxprocs=8 idleprocs=5 threads=10 spinningthreads=0 needspinning=0 idlethreads=4 runqueue=0
```

逐字段读:

- `1003ms`:程序启动至今的毫秒数。
- `gomaxprocs=8`:当前 P 的数量(GOMAXPROCS)。
- `idleprocs=5`:空闲的 P(没活干的 P)——**这个值长期等于 gomaxprocs,说明你的程序并发度太低**;长期等于 0,说明 P 用满了,要么是 CPU 真的忙,要么是某个 G 在死循环没让出。
- `threads=10`:当前 M(OS 线程)总数。**这个数会大于 gomaxprocs**,因为阻塞在系统调用的 M 不算名额(P1-06 的 handoff)。
- `spinningthreads`:正在"自旋找活干"的 M——P1-04 讲过,这是 work-stealing 的发动机。
- `idlethreads=4`:空闲的 M(在等待被复用)。
- `runqueue=0`:全局 runq 里的 G 数——**这个值长期很大,说明 work-stealing 没跟上**,要么 gomaxprocs 不够,要么 G 都卡在某个 P 上。

> 一句话经验:**`idleprocs` 长期为 0 + `runqueue` 长期 > 0 = 调度器在喊"加 P 或减负载"**。

#### scheddetail:看每个 P/M/G

加 `scheddetail=1`,每个 P 会单独打一行,告诉你它的 `status`/`schedtick`/`syscalltick`/绑定的 M,以及本地 runq 的 head/tail(见 [`proc.go`](../go/src/runtime/proc.go#L6964))。这是排查"P 卡在某个 syscall 里"的最快工具——找那个 `status=_Psyscall` 持续不退出的 P。

#### gctrace:每次 GC 一行摘要

`gctrace=1` 的输出格式(见 [`extern.go`](../go/src/runtime/extern.go#L114-L135)):

```
gc 1 @2.1s 1%: 0.012+0.92+0.003 ms clock, 0.097+0.45/0.81/1.3 ms cpu, 4->4->2 MB, 5 MB goal, 0 MB stacks, 0 MB globals, 8 P
```

逐字段读:

- `gc 1`:第 1 次 GC。
- `@2.1s`:程序启动后 2.1 秒。
- `1%`:GC 占总时间的百分比——**长期 > 10% 就该调 GOGC 了**(见③)。
- `0.012+0.92+0.003 ms clock`:STW 扫描终止、并发标记、STW 标记终止三个阶段的**墙上时间**。
- `4->4->2 MB`:GC 开始堆、GC 结束堆、存活堆——**最后一个 2 MB 就是下次 GC 的触发基准**(GOGC 在它基础上算)。
- `5 MB goal`:下次 GC 的目标堆大小。
- `8 P`:用了 8 个 P。

> 这一行比 pprof 的 GC 视图更轻——你只想要"GC 在干啥",`gctrace=1` 足够。

#### 一组组合命令

```bash
# 只看调度
GODEBUG=schedtrace=1000 ./server

# 调度 + 详细
GODEBUG=schedtrace=1000,scheddetail=1 ./server

# 只看 GC
GODEBUG=gctrace=1 ./server

# 全开(排查疑难杂症)
GODEBUG=schedtrace=1000,scheddetail=1,gctrace=1 ./server 2>runtime.log
```

注意输出**走 stderr**(见 `extern.go` 注释 "emit a single line to standard error"),所以重定向要用 `2>`。

### 2.2 `runtime/trace` + `go tool trace`:可视化时间线

GODEBUG 是"日志",trace 是"录像"。`runtime/trace` 包把 G 的创建、阻塞、唤醒、GC 的每个阶段,**按时间轴**记录到文件,用 `go tool trace` 打开就是一个交互式时间线。

#### 最小样例

```go
package main

import (
    "os"
    "runtime/trace"
)

func main() {
    f, _ := os.Create("trace.out")
    defer f.Close()
    trace.Start(f)
    defer trace.Stop()

    // 你的业务代码
    work()
}

func work() {
    done := make(chan struct{})
    go func() {
        sum := 0
        for i := 0; i < 1e8; i++ {
            sum += i
        }
        close(done)
    }()
    <-done
}
```

跑起来,然后用浏览器打开:

```bash
go run main.go
go tool trace trace.out
```

浏览器会自动打开 `http://127.0.0.1:xxxx`。

#### 在 trace 里看什么

- **View trace**(主视图):横轴是时间,纵轴是每个 P(Proc N)和每个 M(OS Thread N)。**彩色块是 G 在跑,空白是 P 空闲**。一眼能看出"这个时间段所有 P 都空了"(说明业务在等 I/O)或"某个 P 被一个 G 长期霸占"(死循环或长计算没抢占点)。
- **Goroutine analysis**:每个 G 的运行时间、阻塞时间、网络等待时间——**排查"为什么这个 goroutine 跑得慢"从这里开始**。
- **Network blocking profile**:网络 I/O 阻塞的聚合视图——**配合本书 P6-19 看,你会看到 `conn.Read` 在 netpoll 上 park 了多久**。
- **Synchronization blocking profile**:Mutex/channel 的阻塞聚合——配合本书 P2-08/P7-20。
- **Syscall blocking profile**:系统调用阻塞聚合——配合本书 P1-06(看 handoff 是否生效)。
- **GC pause / GC mark/scan time**:GC 的两次 STW 和并发标记时长——配合本书 P4-14。

> 经验:`go tool trace` 看时间线找"毛刺"(G 突然阻塞几十毫秒)最直观;`pprof` 看累计找"热点"(CPU 大部分花在哪)。两者互补。

### 2.3 pprof:CPU / 堆 / 协程画像

pprof 不展开讲(那是另一本书的事),但三个和 runtime 最相关的画像点一下:

```go
import _ "net/http/pprof"

// 在某个 http.Server 上挂 pprof 路由
go func() {
    http.ListenAndServe("localhost:6060", nil)
}()
```

```bash
# CPU 画像(采样 30 秒)
go tool pprof http://localhost:6060/debug/pppof/profile?seconds=30

# 堆画像(当前分配)
go tool pprof http://localhost:6060/debug/pprof/heap

# 协程画像(所有 G 当前在哪——排查 G 泄漏的杀手锏)
go tool pprof http://localhost:6060/debug/pprof/goroutine
```

`goroutine` 画像是排查"G 泄漏"的第一工具——它会告诉你每个栈上有多少个 G 卡在同一个地方,几百个 G 卡在同一个 `<-chan` 或 `sync.Mutex.Lock`,就是泄漏的指纹。

### 2.4 GOMAXPROCS:看一眼,而不是猜

`runtime.NumCPU()` 给 CPU 核数,`runtime.GOMAXPROCS(0)` 给当前 GOMAXPROCS(传 0 是只读不改)。在容器里这俩**不一定相等**——见③的自动调整。

---

## 三、调参:GOMAXPROCS / GOGC / GOMEMLIMIT / asyncpreemptoff

每个旋钮都讲"调大调小会发生什么",而不是只列默认值。所有默认值均经源码核实。

### 3.1 GOMAXPROCS:并发度

**默认**:`NumCPU()`(见 [`proc.go`](../go/src/runtime/proc.go#L935-L947),从环境变量 `GOMAXPROCS` 读,否则用 `defaultGOMAXPROCS(numCPUStartup)`)。

**重要(go1.27 新增)**:从 1.27 起,runtime 默认开启 **自动 GOMAXPROCS**(`updatemaxprocs=1`,见 [`runtime1.go`](../go/src/runtime/runtime1.go#L390))。sysmon 每秒检查一次容器 CPU 配额,动态调整 GOMAXPROCS(见 [`proc.go`](../go/src/runtime/proc.go#L6637-L6640))。**这意味着在容器里,GOMAXPROCS 不再是固定的宿主机核数,而是容器 cgroup 分到的 CPU 数**——这是老资料最容易过时的一点。一旦你手动调过 `runtime.GOMAXPROCS(n)`,自动更新就关闭(见 [`proc.go`](../go/src/runtime/proc.go#L7066) 注释:"Setting GOMAXPROCS via a call to GOMAXPROCS disables automatic GOMAXPROCS updates")。

**调大的后果**:更多 P → 更多 G 可并行 → 但**线程数也会涨**(每个 P 可能要一个 M,加上阻塞在 syscall 的 M)。P 之间争抢全局锁(如 `sched.lock`)的开销上升,GC 的 STW 阶段需要停更多 P,串行部分变慢。**超过物理核数没有收益**,反而 cache miss 增加。

**调小的后果**:更少 P → 并行度低,CPU 用不满。但**对 I/O 密集型程序是好事**——一个 Web 服务器开 10000 个 goroutine,但 GOMAXPROCS=2 也能跑得很好,因为大部分 G 都 park 在 netpoll 上,真正占 CPU 的很少。

> 一句话:**CPU 密集调到等于核数,I/O 密集可以小于核数,几乎不要调大**。

### 3.2 GOGC:GC 触发比

**默认**:100(见 [`mgcpacer.go`](../go/src/runtime/mgcpacer.go#L1368-L1377) 的 `readGOGC`,解析失败回退 100,`off` 解析为 -1 表示完全禁用)。

**含义**:上次 GC 后存活堆为 `live`,下次 GC 触发在堆增长到 `live * (1 + GOGC/100)` 时。GOGC=100 就是"翻倍触发"(4MB 存活 → 8MB 触发)。

**调大(如 GOGC=200)**:堆要长到 3 倍存活才 GC → **GC 频率低,CPU 省,但内存用得多**。内存宽裕的离线批处理可以这么调。

**调小(如 GOGC=50)**:堆长到 1.5 倍就 GC → **内存省,但 GC 频繁,CPU 涨**。内存敏感的在线服务可以这么调,但要盯着 gctrace 看 GC 百分比别失控。

**GOGC=off(=-1)**:GC 完全禁用(见 [`mgcpacer.go`](../go/src/runtime/mgcpacer.go#L1361-L1363),设置后会 `gcWaitOnMark` 等当前 GC 跑完)。**只在短生命周期批处理或测试时用**——长跑进程会 OOM。

**GOGC=0**:特殊值,设 `heapMinimum=0`(见 [`mgcpacer.go`](../go/src/runtime/mgcpacer.go#L1343)),小堆也持续 GC,**只用于调试 GC 本身**。

> 协同见 3.3:**GOMEMLIMIT 给的是硬上限,GOGC 给的是软目标**,两者一起算下次 GC 何时触发。

### 3.3 GOMEMLIMIT:Go 1.19 的软内存上限

**默认**:`math.MaxInt64`(等于关闭,见 [`mgcpacer.go`](../go/src/runtime/mgcpacer.go#L1414-L1418),`readGOMEMLIMIT` 读环境变量,空或 `off` 都返回 MaxInt64)。1.19 引入。

**含义**:`GOMEMLIMIT=2GiB` 告诉 runtime"总内存别超过 2 GiB"。**注意单位是 IEC 二进制后缀**(`KiB`=2^10,`MiB`=2^20,见 [`extern.go`](../go/src/runtime/extern.go#L27-L30))——不是 GB(10^9)。值不正确会直接 `throw`(见 [`mgcpacer.go`](../go/src/runtime/mgcpacer.go#L1420-L1422))。

**它管什么、不管什么**:管 Go heap + runtime 管理的所有内存;**不管**二进制自身映射、CGO 分配的内存、OS 代管的缓冲(见 [`extern.go`](../go/src/runtime/extern.go#L23-L27) 的精确界定)。

**和 GOGC 的关系**:runtime 算下次 GC 触发点时,取 `min(GOGC 目标, GOMEMLIMIT)`(见 [`mgcpacer.go`](../go/src/runtime/mgcpacer.go#L1029-L1030) 的注释,assist 距离按这个目标算)。所以**GOMEMLIMIT 是硬天花板,GOGC 是平时该多省着用**。设了 GOMEMLIMIT=2GiB 但 GOGC=100,只要堆没靠近 2GiB,GC 还是按 GOGC=100 的节奏走;一旦逼近 2GiB,GC 会加速,即使牺牲 CPU。

**为什么 Go 1.19 才加**:容器(K8s)普及后,OOM Kill 变成头号杀手——以前 Go 程序只能靠 GOGC 间接控内存,但 GOGC 是"按比例"不是"按绝对值",容器限内存时控制不住。GOMEMLIMIT 就是给容器场景的**救命旋钮**。

**调小的后果**:GC 频率上升(assist 增多,见本书 P4-15),CPU 占比上升,**极端情况下 GC 可能吃掉 50%+ 的 CPU**——这就是 GOMEMLIMIT 设太低的代价,设它时务必**留出 10~20% 的安全垫**(比如容器限 2 GiB,GOMEMLIMIT 设 1.7 GiB,留给二进制和 OS buffer)。

### 3.4 asyncpreemptoff:关掉异步抢占

**默认**:0(开启,见 [`runtime1.go`](../go/src/runtime/runtime1.go#L359) 的 `dbgvars`,无默认值即 0)。

**含义**:`asyncpreemptoff=1` 关闭基于信号的异步抢占(见 [`extern.go`](../go/src/runtime/extern.go#L227-L232))。Go 1.14 引入异步抢占后(本书 P1-05),死循环 G 不再饿死其他 G。

**什么时候关**:几乎只在调试时关——比如你想验证"我这个 G 没有任何函数调用前奏,纯死循环会怎样",或者排查 GC 与异步抢占栈扫描的交互问题(见 `extern.go` 注释:"also disables the conservative stack scanning used for asynchronously preempted goroutines")。

**关掉的后果**:一个紧凑循环(没有函数调用的 `for i++`)会**独占 P 直到它自己让出**——其他 G 在该 P 上饿死,GC 也无法在合适时机抢占它(STW 时长可能暴涨)。**生产环境永远不要关**。

> 本书 P1-05 讲透了"凭什么能在任意点打断而不破坏 GC/栈"——如果你读了那一章还不放心,关掉 asyncpreemptoff 跑一遍你的程序,看 STW 时长变化,就会直观感到异步抢占的价值。

### 3.5 一张速查表

| 旋钮 | 默认值 | 调大(更激进) | 调小(更保守) | 何时动它 |
|---|---|---|---|---|
| GOMAXPROCS | NumCPU(自动调) | 浪费线程,cache miss 多 | 并行度低 | CPU 密集型几乎不动 |
| GOGC | 100 | 省 CPU,费内存 | 省内存,费 CPU | 看 gctrace 的 GC% |
| GOMEMLIMIT | MaxInt64(关) | 无意义 | OOM 解药,但 GC CPU 涨 | 容器部署必配 |
| asyncpreemptoff | 0(开) | — | 1 = 关抢占,只调试用 | 几乎不动 |

---

## 四、横向对照:Java JVM、Erlang VM

Go runtime 不是凭空诞生的——它和 Java JVM、Erlang BEAM 是"如何在语言层面托管并发"的三种顶级答案。看清它们的同与不同,Go 的取舍才显形。

### 4.1 Java JVM:重量级线程 + 多种 GC

Java 在 1.1 时代只有 OS 线程(`java.lang.Thread`),21 才有虚拟线程(Virtual Thread / Project Loom)——但 JVM 整体设计仍是**通用虚拟机**,runtime 不像 Go 那样深度嵌入语言语义。

| 维度 | Go runtime(go1.27) | Java JVM(JDK 21+) |
|---|---|---|
| 并发单元 | goroutine(2KB 起步栈,本书 P0-01) | Thread(OS 线程)+ Virtual Thread(21+,栈可挂起) |
| 调度 | GMP + work-stealing(P1-04) | JDK 21 前:ForkJoinPool;21 后:Virtual Thread 由 carrier thread 调度 |
| 抢占 | 异步抢占 SIGURG(P1-05) | Virtual Thread 在 park/unpark 点让出(协作式为主) |
| 网络 I/O | netpoll 集成 epoll(P6-18) | NIO + Selector(Virtual Thread 自动适配) |
| GC | 单一并发三色标记(本书 P4) | 多种可选:G1 / ZGC / Shenandoah / Epsilon / Serial / Parallel |
| 写屏障 | 混合写屏障(go1.8,本书 P4-13) | 各 GC 自带(ZGC 用读屏障,G1/Shenandoah 用写屏障) |
| 内存模型 | tcmalloc 风格三级缓存(本书 P3) | TLAB(Thread-Local Allocation Buffer)+ 堆分代/分区 |

**Go 的取舍**:单一 GC 而非多选——大部分服务用并发三色就够了,多 GC 选项是 JVM 的负担(选错就悲剧)。**Go 把"如何选 GC"这个决策从用户那里拿走了**。代价是极端场景(超大堆 + 超低延迟)不如 ZGC 灵活。

**Go 的优势**:goroutine 从一开始就是语言核心(`go func`),GMP 是 runtime 一等公民;Java 的并发优化是渐进打补丁(Thread → Executor → ForkJoin → Virtual Thread),底层仍是 OS 线程的 carrier 模型。

### 4.2 Erlang VM(BEAM):进程 + 抢占式 + GC

Erlang 的 BEAM 才是 Go runtime 真正的精神先驱——**"用海量轻量进程 + 抢占式调度 + per-process GC"换高并发**。

| 维度 | Go runtime | Erlang BEAM |
|---|---|---|
| 并发单元 | goroutine(共享内存) | process(完全隔离,无共享) |
| 通信 | channel(CSP,共享内存上的同步,本书 P2) | 消息传递(拷贝,无共享) |
| 调度 | GMP + work-stealing | reducer + scheduler,抢占式(按 reduction 计数,每 ~2000 次函数调用强制切换) |
| 抢占粒度 | 任意安全点(SIGURG,P1-05) | 任意函数调用点(更细,但纯 native 代码不抢占) |
| GC | 全堆并发三色(STW < 1ms) | **per-process GC**(每个进程独立回收,完全不阻塞别人) |
| 容错 | panic + recover | let it crash + 监督树(supervisor) |
| 内存模型 | 共享 + data race detector | 无共享(根本不可能 race) |

**BEAM 最猛的一点**:per-process GC。每个 Erlang process 自己的堆自己回收,**永远不会因为别人的 GC 卡住**——代价是进程间通信必须拷贝(消息不能共享指针)。这就是为什么 Erlang 适合电信级软实时(99.9999999% 可用性)。

**Go 为什么不抄 BEAM**:Go 选择了**共享内存 + channel 二者并存**,而不是 BEAM 的"无共享"。共享内存让数据密集型应用(数据库、缓存、计算)性能爆炸好,BEAM 那种拷贝开销对 Go 程序不可接受。代价是 Go 必须处理 data race(所以才需要 race detector、Mutex、写屏障、原子操作)——**复杂度从"通信"转移到了"内存可见性"**。

> **本书读者已经熟悉的对照点**:和 Tokio 对照(本书★章)——Tokio 是 Rust 的库级异步,Go runtime 是语言内置协程。三方放一起:**Go runtime = JVM 的"语言托管并发" + BEAM 的"轻量调度" + Tokio 的"现代 work-stealing"**,但谁都没全抄——**Go 选了共享内存 + 单 GC + 编译型**,这三条决定了它的全部取舍。

### 4.3 Go runtime 的独特取舍(五条)

把上面对照收敛成五条,这是理解"为什么 Go runtime 长这样"的钥匙:

1. **语言内置协程**:`go func` 是关键字不是库函数——编译器和 runtime 协作(G 创建、栈切换、抢占点插入都是编译器干的)。Tokio 的 `tokio::spawn` 是纯库。代价是 Go 编译器必须懂 runtime。
2. **混合写屏障(go1.8)**:既不是 Dijkstra 也不是 Yuasa,而是混合——栈在 GC 开始时重扫一次后就不再扫(本书 P4-13)。这是 STW 最小化的关键工程取舍。
3. **连续栈拷贝(go1.3+)**:G 栈不够就翻倍拷贝(本书 P5-17),而不是分段栈。和 Tokio 的 `Pin` 形成对照——Go 靠"调整所有指针"解决自引用,Tokio 靠"禁止移动"。前者省心智,后者零运行时开销。
4. **少量 M 驱动海量 G**:M(OS 线程)数远小于 G 数,靠 P 承上启下(本书 P1)。这是 goroutine 便宜的根本——不是 G 结构小,是**线程复用率高**。
5. **单一并发 GC**:不给用户选 GC 的权利,而是把一种做到极致(go1.5 并发标记 + go1.8 混合屏障 + go1.14 异步抢占)。JVM 给 7 种 GC 是因为 JVM 必须服务所有场景,Go 只服务它自己。

---

## 五、Go runtime 演进时间线

读老资料最大的坑——**很多说法在某个版本之后就不成立了**。下面这条线,是本书所有"为什么"的版本背景。每个里程碑都标了"老资料会怎么说、现在是否还成立"。

### 5.1 关键里程碑

| 版本 | 年份 | 关键变化 | 老资料过时点 |
|---|---|---|---|
| Go 1.0 | 2012 | G/M 调度器(无 P),全局锁,GC 是 STW mark-sweep | "Go 调度器是 GM 模型"——**1.1 后就不对了** |
| **Go 1.1** | 2013 | **引入 P**,从 GM 变 GMP;work-stealing | "Go 是 GM 模型" 全面过时 |
| Go 1.3 | 2014 | 并发调度(G 阻塞在系统调用时 handoff P);栈从分段栈改连续栈 | "Go 栈是分段栈" 过时(本书 P5-17) |
| Go 1.4 | 2014 | runtime 自己用 Go 重写(以前是 C);G 从 C 结构改 Go 结构 | "runtime 是 C 写的" 过时 |
| **Go 1.5** | 2015 | **并发 GC**(三色标记 + 写屏障,GC 停顿从几百 ms 降到 10ms 级);`GOMAXPROCS` 默认 = CPU 核数(以前默认 1) | "Go GC 是 STW 的" 全面过时;"GOMAXPROCS 默认 1" 过时 |
| Go 1.6 | 2016 | 写屏障从插入式改删除式(配合并发 GC 真稳定) | "Go 写屏障是 Dijkstra 插入式" 不准 |
| Go 1.7 | 2016 | sub-10ms GC,context 子包,SSA 后端 | — |
| **Go 1.8** | 2017 | **混合写屏障**(Dijkstra + Yuasa 混合,STW 时不再需要重扫全部栈) | "Go GC 要 STW 重扫栈" 过时(本书 P4-13) |
| Go 1.9 | 2017 | safe-point 在更多地方插入 | — |
| Go 1.10 | 2017 | 锁竞争优化,race detector 改进 | — |
| Go 1.12 | 2019 | 栈在 stackguard 上更稳;`madvise` 改 `MADV_FREE` 优先(Linux) | "Go 还内存用 MADV_DONTNEED" 不一定 |
| Go 1.13 | 2019 | 错误处理改进,`sync.Pool` 重写 | — |
| Go 1.14 | 2020 | **异步抢占**(基于 SIGURG 信号,死循环不再饿死别人);timer 改四叉堆(本书 P7-20) | "Go 调度是纯协作式,死循环会卡死" 过时;"timer 是二叉堆" 过时(本书 P7-20) |
| Go 1.15 | 2020 | GC 进一步降低分配开销 | — |
| **Go 1.18** | 2022 | **泛型**;race detector 默认开启更友好;`sync/atomic` 加泛型类型 | "Go 没有泛型" 过时 |
| **Go 1.19** | 2022 | **GOMEMLIMIT** 软内存上限(容器救命,见③);`atomic.Int32` 等类型化原子 | "Go 没法给容器设内存上限" 过时;"runtime 用 MaxInt64 表示无内存限制" 这条 1.19 引入 |
| Go 1.20 | 2023 | PGO(profile-guided optimization)预览;`errors.Join` | — |
| Go 1.21 | 2023 | `min`/`max`/`clear` 内置;PGO 正式;类型推断增强 | — |
| Go 1.22 | 2024 | `for` 循环变量每次迭代新变量(修了经典坑);PGO 增强;range over int/func | "Go for 循环变量是共享的" 过时(这是大头) |
| Go 1.23 | 2024 | range over func;`unique` 包;timer 不再受 GOMAXPROCS=1 影响精度 | — |
| Go 1.24 | 2025 | 泛型类型别名;`weak` 包;`runtime.AddCleanup`(替代部分 SetFinalizer);`sync.Map` 内部重写 | "SetFinalizer 是唯一清理钩子" 过时 |
| Go 1.25 | 2025 | container awareness 进一步;`decoratemappings` 默认开(Linux 内存映射带注释);trace 重写(exectracer2,GODEBUG `traceadvanceperiod` 相关) | "trace 是老格式" 部分过时 |
| Go 1.26 | 2026 | 内部优化 | — |
| **go1.27** | 2026 | **本书钉死版本**;**自动 GOMAXPROCS**(`updatemaxprocs` 默认开,见 [`runtime1.go`](../go/src/runtime/runtime1.go#L390),sysmon 每秒按容器 cgroup 调,见 [`proc.go`](../go/src/runtime/proc.go#L6637)) | **"GOMAXPROCS 永远 = NumCPU" 全面过时**(见③) |

### 5.2 给读者的提醒

1. **看到 1.4 以前的资料直接关掉**:runtime 那时还是 C 写的,和现在的 Go runtime 是两个东西。
2. **1.5(并发 GC)和 1.14(异步抢占)是分水岭**:老资料如果讲"Go GC 会停顿几百毫秒"或"Go 是协作式调度",它停在 1.4 之前。
3. **1.19(GOMEMLIMIT)是容器时代的开始**:容器部署 Go 必看。
4. **1.27(自动 GOMAXPROCS)是本书时点**:这是写本附录时最新也最容易让老资料过时的点——**任何"GOMAXPROCS 默认等于 NumCPU"的说法,在 go1.27 的容器里都不准了**。

### 5.3 怎么查你用的 Go 是哪个版本

```bash
go version
# 输出: go version go1.27 ...

# 看源码里的版本号
grep Version go/src/internal/goversion/goversion.go
```

每个版本的详细 release notes 在 `https://go.dev/doc/go1.X`,演进设计文档多在 `https://go.dev/design/XXXX`。

---

## 收尾:三件事,做完你就上路了

本附录是地图,不是旅程。读完它,真正把你送上路的就三件事:

1. **跑一遍 §2 的 GODEBUG 命令**——拿你自己写过的一个 Go 程序,加 `GODEBUG=schedtrace=1000,gctrace=1` 跑,对照 §2.1 的字段解释读一遍输出。这一步做完,你会第一次"看见"runtime 在你自己的程序里工作。
2. **生成一次 trace**——按 §2.2 的样例改你的程序,用 `go tool trace` 打开,找一处"奇怪"的阻塞(Goroutine analysis 里挑一个跑得慢的 G),追它的阻塞栈。这一步做完,你会从"看设计"转到"看运行"。
3. **挑一篇按 §1.2 的顺序读源码**——从 `runtime2.go` 的 G/M/P 开始,对照本书 P1-02 的字段解释,把每个字段的"为什么放这儿"自己问一遍。这一步做完,你已经具备独立啃 runtime 的能力。

剩下的就是重复——按本书的章节,一篇一篇把源码啃完。等到你能在脑子里放映出"一个 `go func` 怎么变成 G、怎么被 P 承接、怎么被 M 调度、怎么在 netpoll 上 park、怎么被 sysmon 唤醒、怎么被 GC 回收"的全过程,这本书就算读透了。

> 源码在 [`../go/src/runtime/`](../go/src/runtime/),版本 go1.27。地图给你了,路自己走。
